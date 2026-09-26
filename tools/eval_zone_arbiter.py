"""拿「录我的动作」录下来的数据，量区域判定的误按率、漏按率和延迟。

    python tools/eval_zone_arbiter.py                       # 智能和进去就按各算一遍
    python tools/eval_zone_arbiter.py --set T_MAX_S=0.2     # 改一个阈值再算
    python tools/eval_zone_arbiter.py --dir 别人发来的目录 --json

zone_arbiter 里的阈值是按常识定的初值。改之前先在这里跑，改完再跑，数字不能变差。

默认把每个框、每个动作都当作绑了键（--profile 用这台电脑现在的设置，但只按录的
东西算）：评的是判定本身，不是某一个游戏的配置。框的位置、大小、定没定住、量身
结果都照这台电脑现在的设置。

- 误按率：做动作的那几遍里，框被按下的次数 ÷ 做的遍数（按「次」算，不按帧算）。
- 漏按率：故意按框的那几次里，一直到手出框都没按下的比例。
- 延迟：从身体进框那一帧到按下那一帧，毫秒，P50 / P95。「进去就按」恒为 0。
时间全用录的时间戳，不数帧。样本少的时候一两次就是很大的百分比，先看次数。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from motioncontrol import zone_arbiter  # noqa: E402
from motioncontrol.control_kernel import ControlKernel, RUNTIME_BODY_ZONES  # noqa: E402
from motioncontrol.custom_poses import CustomPoseStore  # noqa: E402
from motioncontrol.intent_library import _NullOutput, analyse  # noqa: E402
from motioncontrol.intent_recording import IntentRecordingStore  # noqa: E402
from motioncontrol.pose_downloads import PoseActionStore  # noqa: E402
from motioncontrol.user_paths import user_data_root, user_path  # noqa: E402
from motioncontrol_shared import pose_library  # noqa: E402

ZONE_CN = {"leftHand": "左手框", "rightHand": "右手框", "headJump": "头顶框", "leftFoot": "左脚框", "rightFoot": "右脚框"}


def repo_pose_docs() -> list[dict]:
    """仓库里 cloud/official_poses/ 的官方动作（开发时用，不验签名）。"""
    folder = ROOT / "cloud" / "official_poses"
    return [pose_library.normalize_action(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(folder.glob("*.json")) if path.name != "signatures.json"]


def live_snapshot(all_bound: bool, repo_poses: bool = False) -> dict:
    """这台电脑现在的设置：框、量身、下载的动作、自己录的动作。"""
    docs = repo_pose_docs() if repo_poses else PoseActionStore(user_path("pose_actions")).docs()
    pose_library.register(docs)
    kernel = ControlKernel(_NullOutput())
    try:
        kernel.configure_pose_actions(docs)
        kernel.configure_custom_poses(CustomPoseStore(user_path("custom_poses")))
        snapshot = kernel.replay_snapshot()
        if all_bound:
            bindings = {f"zone.{zone}": {"action": {"type": "gamepad", "target": "A", "behavior": "hold"}}
                        for zone in RUNTIME_BODY_ZONES}
            for trigger, _name in kernel._intent_actions_locked():
                bindings[trigger] = {"action": {"type": "gamepad", "target": "Y", "behavior": "hold"}}
            snapshot["control_bindings"] = bindings
        return snapshot
    finally:
        kernel.close()


def pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def show(mode: str, report: dict) -> None:
    summary = report["summary"]
    print(f"\n== {'智能' if mode == 'smart' else '进去就按'} ==")
    print(f"做动作 {summary['action_reps']} 遍，误按率 {pct(summary['misfire_rate'])}；"
          f"故意按 {summary['press_attempts']} 次，漏按率 {pct(summary['miss_rate'])}；"
          f"延迟 P50 {summary['p50_ms']} 毫秒，P95 {summary['p95_ms']} 毫秒")
    for item in report["actions"]:
        misfires = "、".join(f"{ZONE_CN.get(zone, zone)} {n}" for zone, n in item["misfires"].items()) or "0"
        print(f"  {item['name']:<10} {item['reps']:>2} 遍  误按 {misfires}")
    for item in report["zones"]:
        kind = "快速点" if item["kind"] == "tap" else "按"
        print(f"  {ZONE_CN.get(item['zone'], item['zone'])}{kind:<4} {item['attempts']:>2} 次  "
              f"漏 {item['missed']}  P50 {item['p50_ms']} ms  P95 {item['p95_ms']} ms")
    if report["idle"]:
        print("  随便动动时误按：" + "、".join(f"{ZONE_CN.get(z, z)} {n}" for z, n in report["idle"].items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=None, help="录的数据在哪（默认这台电脑的 intent_recordings）")
    ap.add_argument("--mode", choices=("smart", "simple", "both"), default="both")
    ap.add_argument("--profile", action="store_true", help="不把全部框和动作当作绑了键（按现在的设置，一般什么都没绑）")
    ap.add_argument("--set", action="append", default=[], metavar="名字=值",
                    help="改 zone_arbiter 里的一个阈值，比如 T_MAX_S=0.2，可以给多次")
    ap.add_argument("--repo-poses", action="store_true", help="动作用仓库里的官方动作，不用这台电脑下载的")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    for item in args.set:
        name, _, value = item.partition("=")
        if not hasattr(zone_arbiter, name):
            ap.error(f"zone_arbiter 里没有 {name}")
        setattr(zone_arbiter, name, float(value))

    store = IntentRecordingStore(Path(args.dir) if args.dir else user_data_root() / "intent_recordings")
    if not store.index():
        print(f"{store.directory} 里没有录的数据。先在程序里「录我的动作」。", file=sys.stderr)
        return 1
    snapshot = live_snapshot(all_bound=not args.profile, repo_poses=args.repo_poses)
    modes = ("smart", "simple") if args.mode == "both" else (args.mode,)
    results = {}
    for mode in modes:
        result = analyse({**snapshot, "zone_trigger_mode": mode}, store, mode=mode)
        results[mode] = {"report": result["report"], "rates": result["rates"], "snippets": len(result["bank"])}
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    first = results[modes[0]]
    print(f"录的数据：{store.directory}，{first['snippets']} 次进框")
    for trigger, zones in sorted(first["rates"].items()):
        crossed = "、".join(f"{ZONE_CN.get(z, z)} {v['hits']}/{v['reps']}" for z, v in zones.items()) or "不扫过任何框"
        print(f"  {trigger}: {crossed}")
    for mode in modes:
        if results[mode]["report"]:
            show(mode, results[mode]["report"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
