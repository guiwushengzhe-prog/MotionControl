"""可关闭的单目深度实验：一个候选、一个待处理帧，独立进程不阻塞体感内核。"""
from __future__ import annotations

from collections import deque
import copy
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from . import owned_process
from .depth_comparison_metrics import surface_references
from .depth_comparison_worker import read_packet, write_packet
from .user_paths import user_data_root

MODELS = {
    "dav2_metric_s": {"name": "V2-S · 室内米制", "kind": "metric", "temporal": False},
    "vda_metric_s": {"name": "VDA-S · 米制流式", "kind": "metric", "temporal": True},
    "zipdepth": {"name": "ZipDepth · 相对逆深度", "kind": "relative_inverse", "temporal": False},
}
DEFAULTS = {"enabled": False, "model": "dav2_metric_s", "input_size": 392, "max_fps": 10}


def validate_config(current, changes):
    if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
        raise ValueError("深度对比设置无效")
    value = {**DEFAULTS, **current, **changes}
    if not isinstance(value["enabled"], bool) or value["model"] not in MODELS:
        raise ValueError("请选择有效的深度模型与开关")
    if type(value["input_size"]) is not int or value["input_size"] not in (280, 392, 518):
        raise ValueError("请选择280、392或518的输入短边")
    if type(value["max_fps"]) is not int or value["max_fps"] not in (2, 5, 10, 15, 30):
        raise ValueError("请选择有效的对比帧率上限")
    return value


class DepthComparison:
    def __init__(self, kernel):
        self.kernel = kernel
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        getter = getattr(kernel, "general_setting", lambda _key, default: default)
        try:
            self.config = validate_config(DEFAULTS, getter("depth_comparison", {}))
        except (ValueError, TypeError):
            self.config = dict(DEFAULTS)
        self._runtime = user_data_root() / "depth_comparison_runtime.json"
        self._pending = None
        self._thread = None
        self._stop = threading.Event()
        self._process = None
        self._generation = 0
        self._revision = int(time.monotonic() * 1_000_000)
        self._state, self._error = "waiting" if self.config["enabled"] else "off", ""
        self._last_submit = 0.
        self._sequence = self._skipped = 0
        self._calibrate = self._reset = False
        self._results = {}
        self._preview = None
        self._latencies, self._inference, self._times = deque(maxlen=120), deque(maxlen=120), deque(maxlen=120)

    def installed(self):
        try:
            registry = json.loads(self._runtime.read_text(encoding="utf-8-sig"))
            python_ok = Path(registry["python"]).is_file()
        except (OSError, ValueError, KeyError, TypeError):
            registry, python_ok = {}, False
        models = registry.get("models", {})
        return {ident: {**info, "available": bool(python_ok and Path(models.get(ident, {}).get("source", "?")).is_dir()
                        and Path(models.get(ident, {}).get("checkpoint", "?")).is_file())} for ident, info in MODELS.items()}

    def configure(self, changes):
        with self._lock:
            proposed = validate_config(self.config, changes)
            if proposed["enabled"] and not self.installed()[proposed["model"]]["available"]:
                raise ValueError("该模型尚未准备好，请先运行深度对比准备工具")
            remember = getattr(self.kernel, "remember_general_setting", None)
            if remember:
                remember("depth_comparison", proposed)
            changed = proposed != self.config
            self.config = proposed
        if changed:
            self.suspend()
        return self.status()

    def action(self, action):
        with self._lock:
            if not self.config["enabled"] or self._state != "running":
                raise ValueError("请先开启对比并等待新画面")
            if action == "calibrate" and self.config["model"] == "zipdepth":
                self._calibrate = True
            elif action == "reset":
                self._reset = True
                self._latencies.clear()
                self._inference.clear()
                self._times.clear()
            else:
                raise ValueError("对比操作无效")
        return self.status()

    def submit(self, frame, pose, depth_body, metadata, rotation, captured_at, signature):
        if not self.config["enabled"]:
            return
        now = time.monotonic()
        with self._condition:
            if not self.config["enabled"] or self._state == "error" or now - self._last_submit < 1 / self.config["max_fps"]:
                return
            self._last_submit = now
            if self._pending is not None:
                self._skipped += 1
            self._sequence += 1
            self._pending = ({"sequence": self._sequence, "width": frame.shape[1], "height": frame.shape[0],
                              "sample_at": captured_at, "signature": signature,
                              "references": surface_references(depth_body, metadata, rotation)}, frame.copy())
            if self._thread is None or not self._thread.is_alive():
                self._stop = threading.Event()
                self._state, self._error = "loading", ""
                generation, stop = self._generation, self._stop
                self._thread = threading.Thread(target=self._run, args=(generation, stop, dict(self.config)),
                                                name="depth-comparison", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def suspend(self):
        with self._condition:
            self._generation += 1
            self._revision += 1
            self._stop.set()
            process, thread = self._process, self._thread
            self._pending = self._preview = None
            self._process = self._thread = None
            self._calibrate = self._reset = False
            self._state, self._error = "waiting" if self.config["enabled"] else "off", ""
            self._last_submit = 0
            self._latencies.clear()
            self._inference.clear()
            self._times.clear()
            self._condition.notify_all()
        if process is not None:
            owned_process.terminate_and_wait(process)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)

    def close(self):
        self.suspend()

    def report_input_error(self, error):
        self.suspend()
        with self._lock:
            self._state, self._error = "error", str(error)

    def _run(self, generation, stop, config):
        process = None
        reader = None
        try:
            registry = json.loads(self._runtime.read_text(encoding="utf-8-sig"))
            root = Path(__file__).resolve().parents[1]
            logs = user_data_root() / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            with (logs / "depth-comparison-worker.log").open("ab", buffering=0) as stderr:
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                process = owned_process.popen([registry["python"], "-X", "utf8", "-m", "motioncontrol.depth_comparison_worker",
                    "--runtime", str(self._runtime), "--model", config["model"], "--size", str(config["input_size"])],
                    cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, creationflags=flags)
                with self._lock:
                    if generation != self._generation or stop.is_set():
                        return
                    self._process = process
                received = queue.Queue(maxsize=2)
                def read_output():
                    try:
                        while not stop.is_set():
                            received.put(read_packet(process.stdout), timeout=2)
                    except Exception as exc:
                        try:
                            received.put(({"kind": "error", "error": str(exc) or "深度计算进程已退出"}, b""), timeout=1)
                        except queue.Full:
                            pass
                    finally:
                        try:
                            received.put_nowait(({"kind": "error", "error": "深度计算已停止"}, b""))
                        except queue.Full:
                            pass
                reader = threading.Thread(target=read_output, name="depth-comparison-reader", daemon=True)
                reader.start()
                info, _ = received.get(timeout=90)
                if info.get("kind") != "ready":
                    raise RuntimeError(info.get("error", "模型未能加载"))
                with self._condition:
                    if generation != self._generation or stop.is_set():
                        return
                    self._state = "running"
                    self._results[config["model"]] = {"config": config, **info}
                processed = 0
                while not stop.is_set():
                    with self._condition:
                        while self._pending is None and not stop.is_set():
                            self._condition.wait(.3)
                        if stop.is_set() or generation != self._generation:
                            break
                        meta, frame = self._pending
                        self._pending = None
                        meta.update(calibrate=self._calibrate, reset=self._reset)
                        self._calibrate = self._reset = False
                    write_packet(process.stdin, meta, frame.tobytes())
                    result, preview = received.get(timeout=45)
                    if result.get("kind") != "frame":
                        raise RuntimeError(result.get("error", "深度计算没有返回有效帧"))
                    finished = time.monotonic()
                    with self._condition:
                        if generation != self._generation or stop.is_set():
                            break
                        processed += 1
                        latency = max(0, (finished - result["sample_at"]) * 1000)
                        if processed > 2:
                            self._inference.append(result["inference_ms"])
                            self._latencies.append(latency)
                            self._times.append(finished)
                        result.update({"config": config, **info, "kind": "frame", "received_at": finished,
                            "warmup_remaining": max(0, 2 - processed), "processed_frames": processed,
                            "inference_avg_ms": round(sum(self._inference) / len(self._inference), 2) if self._inference else None,
                            "inference_p95_ms": self._percentile(self._inference),
                            "latency_ms": round(latency, 1), "latency_p95_ms": self._percentile(self._latencies),
                            "fps": round((len(self._times) - 1) / (self._times[-1] - self._times[0]), 2) if len(self._times) > 1 and self._times[-1] > self._times[0] else None})
                        self._results[config["model"]] = result
                        self._preview = preview
        except Exception as exc:
            with self._lock:
                if generation == self._generation and not stop.is_set():
                    self._state, self._error = "error", (str(exc) or "深度模型未能及时返回，请关闭后重试")
        finally:
            if process is not None:
                owned_process.terminate_and_wait(process)
                for stream in (process.stdin, process.stdout):
                    try:
                        stream.close()
                    except OSError:
                        pass
            if reader:
                reader.join(timeout=1)
            with self._lock:
                if generation == self._generation:
                    self._process = None

    @staticmethod
    def _percentile(values):
        ordered = sorted(values)
        return round(ordered[min(len(ordered) - 1, math.ceil(.95 * len(ordered)) - 1)], 2) if ordered else None

    def preview(self):
        with self._lock:
            return self._preview if self.config["enabled"] else None

    def status(self):
        now = time.monotonic()
        with self._lock:
            results = copy.deepcopy(self._results)
            for row in results.values():
                row["age_ms"] = round((now - row.pop("received_at")) * 1000) if "received_at" in row else None
            state = self._state
            current = results.get(self.config["model"], {})
            if state == "running" and current.get("age_ms") is not None and current["age_ms"] > 1500:
                state = "stale"
            return {"config": dict(self.config), "revision": self._revision, "state": state, "error": self._error,
                    "models": self.installed(), "results": results, "skipped_frames": self._skipped,
                    "sequence": current.get("sequence"), "preview_ready": self._preview is not None,
                    "reference": "Kinect 原始深度的关节附近表面采样，非关节中心"}
