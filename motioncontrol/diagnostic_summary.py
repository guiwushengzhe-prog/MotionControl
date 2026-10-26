"""只导出明确允许的状态；不打包配置、日志内容、语音或画面。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import sys

from .startup_logs import history


def _numbers(data, keys):
    return {key: value for key in keys if type(value := data.get(key)) in (int, float)
            and math.isfinite(value)}


def _states(data, keys):
    return {key: value for key in keys if isinstance(value := data.get(key), bool)}


def build_summary(*, version, source_revision, program_root, data_root, performance, camera,
                  settings, profiles, output_enabled, voice, studio):
    metrics = _numbers(performance, ("capture_fps", "inference_fps", "inference_avg_ms", "inference_p95_ms",
        "total_latency_ms", "pose_frame_age_ms", "dropped_frames", "skipped_frames", "process_cpu_percent",
        "cpu_logical_processors", "process_memory_mb", "pose_cpu_threads", "hand_cpu_threads", "preview_fps",
        "preview_encode_avg_ms", "preview_encode_p95_ms", "preview_jpeg_avg_bytes"))
    metrics["stages_ms"] = _numbers(performance.get("inference_stages_ms") or {},
        ("prepare", "pose", "head", "hands", "depth_dispatch", "control"))
    comparison = performance.get("depth_comparison") or {}
    result = (comparison.get("results") or {}).get("mocapnet_personal") or {}
    model = (comparison.get("models") or {}).get("mocapnet_personal") or {}
    lean = {**_states(comparison, ("active", "control_requested")),
        **_numbers(result, ("inference_avg_ms", "inference_p95_ms", "latency_ms", "fps",
                           "gpu_allocated_mb", "gpu_reserved_mb", "age_ms")),
        "available": model.get("available") is True, "missing_resources": len(model.get("issues") or []),
        "cpu_only": result.get("device") == "cpu", "has_error": bool(comparison.get("error"))}
    # 只保留固定状态名；异常消息可能包含用户路径、口令或第三方服务返回值。
    valid_states = {"ready", "disabled", "pending", "error", "blocked"}
    configuration = {name: {"state": data.get("state") if data.get("state") in valid_states else "unknown",
                           "has_error": bool(data.get("error"))}
                     for name, data in (("settings", settings), ("games", profiles))}
    devices = {**_states(camera, ("running", "depth_supported", "depth_enabled", "depth_active",
                                  "picture_black", "preview_mirrored")),
        "backend": camera.get("backend") if camera.get("backend") in {"kinect", "dshow", "msmf", "opencv"} else "other",
        "resolution": _numbers(camera.get("resolution") or {}, ("width", "height")),
        "has_error": bool(camera.get("last_error") or camera.get("depth_error"))}
    return {"schema": "motioncontrol.diagnostic-summary.v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "version": version, "source_revision": source_revision, "program_root": str(Path(program_root)),
        "user_data_root": str(Path(data_root)), "windows": platform.platform(), "python": sys.version.split()[0],
        "logical_processors": os.cpu_count(), "configuration": configuration, "camera": devices,
        "performance": metrics, "monocular_lean": lean, "output_enabled": bool(output_enabled),
        "voice": {"running": voice.get("running") is True, "has_error": bool(voice.get("error"))},
        "studio": _states(studio, ("recording", "live")), "startup_history": history(Path(data_root) / "logs"),
        "log_write_failed": bool(getattr(sys.stdout, "failed", False) or getattr(sys.stderr, "failed", False)),
        "contents": "仅状态数值和程序/数据位置；不包含账户凭证、配对密钥、口令文本、配置正文、日志正文或画面"}


def encode_summary(**kwargs):
    return json.dumps(build_summary(**kwargs), ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
