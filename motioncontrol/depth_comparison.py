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
from .depth_input import crop_references, person_rect
from .monocular_lean import MonocularLean, kinect_pitch
from .lean_comparison_recording import LeanComparisonRecorder
from .user_paths import user_data_root

MODELS = {
    "dav2_metric_s": {"name": "V2-S · 室内米制", "kind": "metric", "temporal": False},
    "vda_metric_s": {"name": "VDA-S · 米制流式", "kind": "metric", "temporal": True},
    "zipdepth": {"name": "ZipDepth · 相对逆深度", "kind": "relative_inverse", "temporal": False},
}
LEAN_MODELS = {"mocapnet_personal": {"name": "MocapNET · 处理器个人研究", "kind": "pose_features", "temporal": False}}
DEFAULTS = {"enabled": False, "model": "dav2_metric_s", "monocular_model": "mocapnet_personal", "input_size": 392, "max_fps": 15,
            "input_scope": "full", "auto_scale": True}


def validate_config(current, changes):
    if not isinstance(changes, dict) or set(changes) - set(DEFAULTS):
        raise ValueError("深度对比设置无效")
    value = {**DEFAULTS, **current, **changes}
    if not isinstance(value["enabled"], bool) or value["model"] not in MODELS or value["monocular_model"] not in {**MODELS, **LEAN_MODELS}:
        raise ValueError("请选择有效的深度模型与开关")
    if type(value["input_size"]) is not int or value["input_size"] not in (280, 392, 518):
        raise ValueError("请选择280、392或518的输入短边")
    if type(value["max_fps"]) is not int or value["max_fps"] not in (2, 5, 10, 15, 30):
        raise ValueError("请选择有效的对比帧率上限")
    if value["input_scope"] not in ("full", "person") or not isinstance(value["auto_scale"], bool):
        raise ValueError("请选择有效的取景范围与尺度校准开关")
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
        # 旧三模型对比已从界面退役，不能因历史保存的开关在后台继续计算。
        self.config["enabled"] = False
        if self.config["monocular_model"] not in LEAN_MODELS:
            self.config["monocular_model"] = "mocapnet_personal"
        self._runtime = user_data_root() / "depth_comparison_runtime.json"
        self._pending = None
        self._thread = None
        self._worker_model = None
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
        self._preview_demand_until = self._last_preview_at = 0.
        self._rect = self._source_signature = self._tracking_id = None
        self.monocular = MonocularLean(persist=bool(getattr(kernel, "_persist", False)))
        self._monocular_lock = threading.RLock()
        self._latencies, self._inference, self._times = deque(maxlen=120), deque(maxlen=120), deque(maxlen=120)
        self.lean_recording = LeanComparisonRecorder(user_data_root() / "recordings" / "前后倾对照")
        if getter("lean_comparison_recording", False) is True:
            try:
                self.lean_recording.start()
            except OSError as exc:
                self.lean_recording.error = "对照记录未能开始：" + str(exc)

    def installed(self):
        try:
            registry = json.loads(self._runtime.read_text(encoding="utf-8-sig"))
            python_ok = Path(registry["python"]).is_file()
        except (OSError, ValueError, KeyError, TypeError):
            registry, python_ok = {}, False
        models = registry.get("models", {})
        return {ident: {**info, "available": bool(python_ok and Path(models.get(ident, {}).get("source", "?")).is_dir()
                        and Path(models.get(ident, {}).get("checkpoint", "?")).is_file()
                        and (ident not in LEAN_MODELS or Path(models.get(ident, {}).get("readout", "?")).is_file()))}
                for ident, info in {**MODELS, **LEAN_MODELS}.items()}

    def _control_requested(self):
        return getattr(self.kernel, "sensitivity_experiments", {}).get("fore_aft_source") == "monocular"

    def _requested(self):
        return self.config["enabled"] or self._control_requested() or self.lean_recording.enabled

    def _selected_model(self, config=None):
        value = self.config if config is None else config
        return value["monocular_model"] if self._control_requested() or self.lean_recording.enabled else value["model"]

    def configure_recording(self, changes):
        if not isinstance(changes, dict) or set(changes) != {"enabled"} or type(changes["enabled"]) is not bool:
            raise ValueError("对照记录开关必须为开启或关闭")
        enabled = changes["enabled"]
        with self._lock:
            previous = self.lean_recording.enabled
            if enabled and not previous:
                self.lean_recording.start()
            try:
                remember = getattr(self.kernel, "remember_general_setting", None)
                if remember:
                    remember("lean_comparison_recording", enabled)
            except Exception:
                if not previous:
                    self.lean_recording.stop()
                raise
            if not enabled:
                self.lean_recording.stop()
            self._revision += 1
        return self.status()

    def configure(self, changes):
        with self._lock:
            proposed = validate_config(self.config, changes)
            if (proposed["enabled"] or self._control_requested()) and not self.installed()[self._selected_model(proposed)]["available"]:
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
        if action == "reframe":
            with self._lock:
                if not self._requested() or (self.config["input_scope"] != "person" and not self._control_requested()):
                    raise ValueError("请先开启人体范围对比")
            self.suspend()
            return self.status()
        with self._lock:
            if not self._requested() or self._state != "running":
                raise ValueError("请先开启对比并等待新画面")
            if action == "calibrate":
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
        if not self._requested():
            if self._thread is not None:
                self.suspend()
            return
        with self._lock:
            changed_model = self._worker_model is not None and self._worker_model != self._selected_model()
        if changed_model:
            self.suspend()
        now = time.monotonic()
        with self._condition:
            mono = self._control_requested() or self.lean_recording.enabled
            pose_only = self._selected_model() in LEAN_MODELS
            limit = max(15 if pose_only else 10, self.config["max_fps"]) if mono else self.config["max_fps"]
            if not self._requested() or self._state == "error" or now - self._last_submit < 1 / limit:
                return
            self._last_submit = now
            if signature != self._source_signature:
                self._source_signature = copy.deepcopy(signature)
                self._rect = self._tracking_id = None
            tracking_id = (depth_body or {}).get("tracking_id")
            if tracking_id is not None:
                if self._tracking_id is not None and tracking_id != self._tracking_id:
                    self._rect = None
                self._tracking_id = tracking_id
            height, width = frame.shape[:2]
            scope = "pose" if pose_only else "person" if mono else self.config["input_scope"]
            if scope == "person" and self._rect is None:
                self._rect = person_rect(pose, width, height)
                if self._rect is None:
                    self._state = "waiting_person"
                    return
            rect = self._rect if scope == "person" else (0, 0, width, height)
            x, y, w, h = rect
            references = [] if pose_only else crop_references(surface_references(depth_body, metadata, rotation), rect, width, height)
            if self._pending is not None:
                self._skipped += 1
            self._sequence += 1
            self._pending = ({"sequence": self._sequence, "width": w, "height": h,
                              "sample_at": captured_at, "signature": [signature, self._tracking_id, rect],
                              "frame_region": list(rect), "source_shape": [height, width],
                              "references": references}, None if pose_only else frame[y:y+h, x:x+w].copy())
            self._pending[0].update(lean=mono, input_scope=scope, effective_fps=limit, pose={n: {**p, "x": (p["x"] * width - x) / w, "y": (p["y"] * height - y) / h}
                for n, p in (pose or {}).items() if isinstance(p, dict) and "x" in p and "y" in p},
                teacher=kinect_pitch(depth_body), neutral_revision=getattr(self.kernel, "_monocular_neutral_revision", 0))
            if self.lean_recording.enabled:
                sensitivity = getattr(self.kernel, "sensitivity_experiments", {})
                self._pending[0]["lean_comparison"] = {"token": self.lean_recording.token,
                    "depth_sample_at": (depth_body or {}).get("sample_at"),
                    "tracking_id": tracking_id, "model": self._selected_model(),
                    "forward_on_deg": sensitivity.get("monocular_forward_on_deg", 7.),
                    "back_on_deg": sensitivity.get("monocular_back_on_deg", 7.)}
            if self._thread is None or not self._thread.is_alive():
                self._stop = threading.Event()
                self._state, self._error = "loading", ""
                generation, stop = self._generation, self._stop
                effective = {**self.config, "model": self._selected_model(), "input_scope": scope, "max_fps": limit}
                self._worker_model = effective["model"]
                self._thread = threading.Thread(target=self._run, args=(generation, stop, effective),
                                                name="depth-comparison", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def suspend(self):
        self.lean_recording.pause("识别暂停或来源更新，等待新画面")
        with self._condition:
            self._generation += 1
            self._revision += 1
            self._stop.set()
            process, thread = self._process, self._thread
            self._pending = self._preview = None
            self._preview_demand_until = self._last_preview_at = 0.
            self._process = self._thread = None
            self._worker_model = None
            self._calibrate = self._reset = False
            self._rect = self._source_signature = self._tracking_id = None
            self._state, self._error = "waiting" if self._requested() else "off", ""
            self._last_submit = 0
            self._latencies.clear()
            self._inference.clear()
            self._times.clear()
            self._condition.notify_all()
        if process is not None:
            owned_process.terminate_and_wait(process)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3)
        with self._monocular_lock:
            self.monocular.flush()
            self.monocular.reset()
        publish = getattr(self.kernel, "publish_monocular_lean", None)
        if publish:
            publish(None)

    def close(self):
        self.suspend()
        self.lean_recording.stop()

    def report_input_error(self, error):
        self.suspend()
        with self._lock:
            self._state, self._error = "error", str(error)
        publish = getattr(self.kernel, "publish_monocular_lean", None)
        if publish:
            publish({"valid": False, "calibrated": False, "sample_at": time.monotonic(), "reason": str(error)})

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
                module = "motioncontrol.mocap_lean_worker" if config["model"] in LEAN_MODELS else "motioncontrol.depth_comparison_worker"
                process = owned_process.popen([registry["python"], "-X", "utf8", "-m", module,
                    "--runtime", str(self._runtime), "--model", config["model"], "--size", str(config["input_size"]),
                    *(["--auto-scale"] if config["auto_scale"] else [])],
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
                processed, previous_signature = 0, None
                while not stop.is_set():
                    with self._condition:
                        while self._pending is None and not stop.is_set():
                            self._condition.wait(.3)
                        if stop.is_set() or generation != self._generation:
                            break
                        meta, frame = self._pending
                        self._pending = None
                        meta.update(calibrate=self._calibrate, reset=self._reset,
                                    preview=self._preview_requested(time.monotonic()))
                        self._calibrate = self._reset = False
                        if meta["signature"] != previous_signature:
                            self._latencies.clear()
                            self._inference.clear()
                            self._times.clear()
                            processed, previous_signature = 0, meta["signature"]
                    # 原厂标签仅供本进程的可选学习；推理进程不接触参考答案。
                    outgoing = {key: value for key, value in meta.items() if key not in ("teacher", "lean_comparison")}
                    write_packet(process.stdin, outgoing, b"" if frame is None else frame.tobytes())
                    result, preview = received.get(timeout=45)
                    if result.get("kind") != "frame":
                        raise RuntimeError(result.get("error", "深度计算没有返回有效帧"))
                    finished = time.monotonic()
                    if meta.get("lean"):
                        with self._monocular_lock:
                            if generation != self._generation or stop.is_set():
                                break
                            dimension = 42 if config["model"] in LEAN_MODELS else 7
                            indices = tuple(info["learning_feature_indices"]) if dimension == 42 else tuple(range(dimension))
                            if self.monocular.feature_dim != dimension or self.monocular.feature_indices != indices:
                                self.monocular.flush()
                                self.monocular = MonocularLean(persist=bool(getattr(self.kernel, "_persist", False)),
                                    feature_dim=dimension, feature_indices=indices)
                            self.monocular.set_context([config["model"], registry["models"][config["model"]].get("sha256"),
                                meta["signature"][0], meta["frame_region"], config["input_size"] if dimension == 7 else
                                registry["models"][config["model"]].get("readout_sha256"), 1])
                            experimental = getattr(self.kernel, "sensitivity_experiments", {})
                            lean = self.monocular.update(result.get("torso"), result["sample_at"],
                                reference=meta.get("teacher") if 0 <= finished - result["sample_at"] <= .25 else None,
                                learn=bool(experimental.get("learn_monocular")), revision=meta["neutral_revision"])
                        result["monocular_lean"] = lean
                        with self._lock:
                            if generation == self._generation and not stop.is_set():
                                if result.get("sequence") == meta["sequence"] and result.get("sample_at") == meta["sample_at"]:
                                    self.lean_recording.observe(meta, lean, self.monocular.neutral_reference,
                                        finished=finished, revision=getattr(self.kernel, "_monocular_neutral_revision", 0))
                                else:
                                    self.lean_recording.pause("模型结果与提交帧编号不匹配，未计入对照")
                    with self._condition:
                        if generation != self._generation or stop.is_set():
                            break
                        processed += 1
                        latency = max(0, (finished - result["sample_at"]) * 1000)
                        if processed > 2:
                            self._inference.append(result["inference_ms"])
                            self._latencies.append(latency)
                            self._times.append(finished)
                        result.update({"config": {**config, "input_scope": meta["input_scope"], "max_fps": meta["effective_fps"]}, **info, "kind": "frame", "received_at": finished,
                            "frame_region": meta["frame_region"], "source_shape": meta["source_shape"],
                            "warmup_remaining": max(0, 2 - processed), "processed_frames": processed,
                            "inference_avg_ms": round(sum(self._inference) / len(self._inference), 2) if self._inference else None,
                            "inference_p95_ms": self._percentile(self._inference),
                            "latency_ms": round(latency, 1), "latency_p95_ms": self._percentile(self._latencies),
                            "fps": round((len(self._times) - 1) / (self._times[-1] - self._times[0]), 2) if len(self._times) > 1 and self._times[-1] > self._times[0] else None})
                        self._results[config["model"]] = result
                        if preview:
                            self._preview = preview
                    if meta.get("lean") and generation == self._generation and not stop.is_set():
                        publish = getattr(self.kernel, "publish_monocular_lean", None)
                        if publish:
                            publish(lean, meta["neutral_revision"])
        except Exception as exc:
            with self._lock:
                if generation == self._generation and not stop.is_set():
                    self._state, self._error = "error", (str(exc) or "深度模型未能及时返回，请关闭后重试")
                    self.lean_recording.pause(self._error)
        finally:
            with self._monocular_lock:
                self.monocular.flush()
            if generation == self._generation:
                publish = getattr(self.kernel, "publish_monocular_lean", None)
                if publish:
                    publish({"valid": False, "calibrated": False, "sample_at": time.monotonic(),
                             "reason": self._error or "单目深度已停止"})
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
            self._preview_demand_until = time.monotonic() + 1.
            return self._preview if self._requested() else None

    def _preview_requested(self, now):
        if now >= self._preview_demand_until or now - self._last_preview_at < 1 / 6:
            return False
        self._last_preview_at = now
        return True

    def status(self):
        now = time.monotonic()
        with self._lock:
            results = copy.deepcopy(self._results)
            for row in results.values():
                row["age_ms"] = round((now - row.pop("received_at")) * 1000) if "received_at" in row else None
            state = self._state
            current = results.get(self._selected_model(), {})
            if state == "running" and current.get("age_ms") is not None and current["age_ms"] > 1500:
                state = "stale"
            return {"config": dict(self.config), "revision": self._revision, "state": state, "error": self._error,
                    "active": bool(self._requested()), "control_requested": self._control_requested(),
                    "active_model": self._selected_model(),
                    "models": self.installed(), "results": results, "skipped_frames": self._skipped,
                    "sequence": current.get("sequence"), "preview_ready": self._preview is not None,
                    "lean_recording": self.lean_recording.status(),
                    "reference": "Kinect 原始深度的关节附近表面采样，非关节中心"}
