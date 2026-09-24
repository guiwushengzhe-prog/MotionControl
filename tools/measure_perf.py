"""按固定方法量一次性能：从正在运行的 MotionControl 读数，不改任何设置。

用法（程序开着、画面已经连上、人站在画面里全身入镜）：

    python tools/measure_perf.py              # 先等 10 秒，再每秒采一次、采 60 秒
    python tools/measure_perf.py --seconds 30 --warmup 5

采完打印一段摘要，最后一行是表格行，原样贴进 docs/性能.md 的「现在的水平」。

为什么要脚本：界面上的数是最近一百多帧的瞬时值，看一眼、截一张图，碰上哪一秒就是哪
一秒，两次之间没法比。这里固定预热时长、采样时长和取法（中位数、P95 的中位数），同一
台手机、同一个场景量两次，数应该差不多；差得多，才说明真有变化。

只读：只用 GET 请求，不开关摄像头、不动输出、不改配置。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if path.startswith("/api/") else body


def median(values):
    values = [float(v) for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def low(values, fraction=0.05):
    """P5：最差的那一小段时间是多少。帧率看的是「掉得多狠」，不是平均。"""
    values = sorted(float(v) for v in values if v is not None)
    if not values:
        return None
    return round(values[max(0, int(len(values) * fraction) - 1)], 1)


def show(value, unit=""):
    return "—" if value is None else f"{value}{unit}"


def git_head() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:  # noqa: BLE001 - 没装 git 也照样量
        return "?"


def same_code(base: str) -> bool:
    """正在跑的是不是这个文件夹里的代码。不是的话量出来的数记到哪个版本上都不对。"""
    try:
        served = get(base, "/").replace("\r\n", "\n")
        local = (ROOT / "web" / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        return served == local
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="按固定方法量一次 MotionControl 的性能")
    ap.add_argument("--base", default="http://127.0.0.1:8766")
    ap.add_argument("--warmup", type=float, default=10.0, help="开始记之前先等几秒（默认 10）")
    ap.add_argument("--seconds", type=float, default=60.0, help="采多久（默认 60）")
    ap.add_argument("--note", default="", help="写进表格备注，比如「换了手机：型号」「手机在充电」「逆光」；连接方式脚本自己记")
    args = ap.parse_args()

    try:
        first = get(args.base, "/api/performance")
    except Exception as exc:  # noqa: BLE001
        print(f"连不上 {args.base}：{exc}\n先把 MotionControl 打开、画面连上再量。", file=sys.stderr)
        return 2
    if not first.get("running"):
        print("画面还没连上（performance.running = false），先连上摄像头再量。", file=sys.stderr)
        return 2
    if not same_code(args.base):
        print("提醒：正在跑的页面和这个文件夹里的不一样——量出来的数不是这份代码的。", file=sys.stderr)

    source = first.get("source")
    print(f"来源：{source}，先等 {args.warmup:.0f} 秒，再采 {args.seconds:.0f} 秒……", file=sys.stderr)
    time.sleep(args.warmup)

    samples, phones = [], []
    start_seq = end_seq = None
    started = time.monotonic()
    while time.monotonic() - started < args.seconds:
        tick = time.monotonic()
        try:
            perf = get(args.base, "/api/performance")
            samples.append(perf)
            if source == "phone":
                status = get(args.base, "/api/input/status?brief=1")
                active = next((s for s in status.get("mobile_pose_sources", []) if s.get("active")), None)
                if active:
                    phones.append(active)
                    seq = active.get("sequence")
                    if isinstance(seq, int):
                        start_seq = seq if start_seq is None else start_seq
                        end_seq = seq
        except Exception as exc:  # noqa: BLE001 - 一次没取到不要紧，少一个点
            print(f"  这一秒没取到：{exc}", file=sys.stderr)
        time.sleep(max(0.0, 1.0 - (time.monotonic() - tick)))
    elapsed = time.monotonic() - started

    if not samples:
        print("一个点都没采到。", file=sys.stderr)
        return 1

    humans = sum(1 for s in samples if (s.get("recent_humans") or 0) >= 1) / len(samples)
    skipped = (samples[-1].get("skipped_frames") or 0) - (samples[0].get("skipped_frames") or 0)
    dropped = (samples[-1].get("dropped_frames") or 0) - (samples[0].get("dropped_frames") or 0)
    res = samples[-1].get("camera_resolution") or {}
    resolution = f"{res.get('width', '?')}×{res.get('height', '?')}" if isinstance(res, dict) else str(res)
    infer_avg = median(s.get("inference_avg_ms") for s in samples)
    infer_p95 = median(s.get("inference_p95_ms") for s in samples)

    if source == "phone":
        fps_key = "network_fps"
        last = phones[-1] if phones else {}
        # 走数据线还是 WiFi 会影响延迟和稳定性，得记下来；按数据线有没有接通判断，不靠人填。
        try:
            tether = (get(args.base, "/api/input/status?brief=1").get("usb_tether") or {}).get("present")
        except Exception:  # noqa: BLE001
            tether = None
        link = "数据线" if tether else "WiFi" if tether is False else "连接方式未知"
        device = f"手机 {last.get('delegate') or '?'} · 模型输入 {last.get('inference_side') or '?'} · {link}"
        # 手机自己每帧加一的序号：整段的平均发送帧率，不受 120 帧窗口影响。
        sent = round((end_seq - start_seq) / elapsed, 1) if start_seq is not None and end_seq is not None and elapsed > 0 else None
        extra = f"手机发出 {show(sent, ' 帧/秒')}（按序号算）"
    else:
        fps_key = "inference_fps"
        device = f"电脑 {samples[-1].get('backend_name') or samples[-1].get('backend') or '?'}"
        extra = (f"采集 {show(median(s.get('actual_capture_fps') for s in samples), ' 帧/秒')}，"
                 f"端到端 {show(median(s.get('total_latency_ms') for s in samples), ' ms')}")
    fps_mid = median(s.get(fps_key) for s in samples)
    fps_low = low(s.get(fps_key) for s in samples)

    print()
    print(f"来源          {source}（{device}，{resolution}）")
    print(f"实际帧率      中位 {show(fps_mid)} 帧/秒，最差 5% {show(fps_low)} 帧/秒  [{fps_key}]")
    print(f"推理耗时      平均 {show(infer_avg)} ms，P95 {show(infer_p95)} ms（各取整段的中位数）")
    print(f"其他          {extra}；跳帧 {skipped}，丢帧 {dropped}")
    print(f"画面里有人    {humans:.0%} 的时间（低于 90% 这次不算数，重测）")
    print(f"采样          {len(samples)} 个点，{elapsed:.0f} 秒")
    print()
    row = (f"| {datetime.now():%Y-%m-%d} | {git_head()} | {source} | {device} | {resolution} | "
           f"{show(fps_mid)}（最差 {show(fps_low)}） | {show(infer_avg)} / {show(infer_p95)} | "
           f"{humans:.0%} | {skipped} | {args.note or '—'} |")
    print(row)
    return 0 if humans >= 0.9 else 3


if __name__ == "__main__":
    sys.exit(main())
