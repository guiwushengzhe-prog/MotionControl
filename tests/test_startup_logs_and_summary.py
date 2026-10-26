import io
import json
from pathlib import Path
from types import SimpleNamespace

from motioncontrol.diagnostic_summary import build_summary
from motioncontrol.startup_logs import BoundedLog, history, rotate
from test_configuration_coordination import application
from test_settings_persistence import local_api


def test_long_running_logs_and_startup_history_are_bounded_and_readable(tmp_path):
    path = tmp_path / "startup.stdout.log"
    for run in range(8):
        with BoundedLog(path, io.StringIO(), limit=256, backups=2) as log:
            for _ in range(20):
                log.write(f"第{run}轮中文🙂\n" * 10)
        logs = list(tmp_path.glob("startup.stdout.log*"))
        assert len(logs) <= 3
        assert all(item.stat().st_size <= 256 for item in logs)
        assert all(item.read_text(encoding="utf-8") for item in logs)
    assert str(run) in path.read_text(encoding="utf-8")
    entries = history(tmp_path)
    assert entries and all(set(item) == {"name", "bytes", "modified_at"} for item in entries)


def test_oversized_old_log_keeps_only_the_tail(tmp_path):
    path = tmp_path / "bootstrap.stderr.log"
    path.write_bytes(b"old" * 500 + b"last failure")
    rotate(path, limit=64, backups=2)
    assert not path.exists()
    assert path.with_suffix(".log.1").read_bytes().endswith(b"last failure")
    assert path.with_suffix(".log.1").stat().st_size == 64


def test_log_failure_keeps_original_output_available(tmp_path):
    blocked = tmp_path / "file-instead-of-directory"
    blocked.write_text("keep")
    fallback = io.StringIO()
    with BoundedLog(blocked / "startup.stdout.log", fallback) as log:
        assert log.failed
        assert log.write("识别继续") == 4
    assert fallback.getvalue() == "识别继续"


def test_summary_excludes_private_nested_content_and_arbitrary_files(tmp_path):
    secret = "PRIVATE_CREDENTIAL_AND_TRANSCRIPT"
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "startup.stderr.log").write_text(secret)
    (logs / "account.json").write_text(secret)
    private = {"error": secret, "token": secret, "transcript": secret}
    summary = build_summary(version="2", source_revision="build", program_root=tmp_path, data_root=tmp_path,
        performance={**private, "capture_fps": 30, "total_latency_ms": 8, "inference_avg_ms": float("nan"),
            "depth_comparison": {**private, "results": {"mocapnet_personal": {**private, "device": "cpu",
                "inference_avg_ms": 2.5, "monocular_lean": private, "gpu_reserved_mb": 0}}}},
        camera={**private, "running": True, "camera_device": secret, "backend": "kinect", "pose": private},
        settings={**private, "state": "ready"}, profiles={**private, "state": "pending"},
        output_enabled=False, voice=private, studio=private)
    text = json.dumps(summary, allow_nan=False)
    assert secret not in text and "account.json" not in text
    assert summary["performance"]["capture_fps"] == 30
    assert "inference_avg_ms" not in summary["performance"]
    assert summary["monocular_lean"]["cpu_only"]
    assert summary["startup_history"][0]["bytes"] == len(secret)


def test_summary_api_does_not_capture_images_or_change_output(local_api, monkeypatch):
    app, request = local_api
    app.OUTPUT.enabled = True
    before = app.PROFILES.configuration_document()
    monkeypatch.setattr(app, "_backup_configuration", lambda: (_ for _ in ()).throw(AssertionError("不能导出配置")))
    status, summary = request("/api/diagnostics/export", {})
    assert status == 200 and summary["schema"] == "motioncontrol.diagnostic-summary.v1"
    assert summary["output_enabled"] and app.OUTPUT.enabled
    assert app.PROFILES.configuration_document() == before
    app.OUTPUT.enabled = False


def test_page_and_log_share_one_resource_sample(application, monkeypatch):
    import sys
    app = application
    now, calls = [0.], []
    process = SimpleNamespace(cpu_percent=lambda _: calls.append("cpu") or 200,
        memory_info=lambda: SimpleNamespace(rss=128 * 1024 ** 2))
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=lambda: process, cpu_count=lambda: 4, Error=OSError))
    monkeypatch.setattr(app.time, "monotonic", lambda: now[0])
    initial = app.process_resources()
    assert initial == {"process_cpu_percent": None, "process_memory_mb": 128.}
    now[0] = .1
    assert app.process_resources() == initial and calls == ["cpu"]
    now[0] = 1.2
    assert app.process_resources()["process_cpu_percent"] == 50.
    assert app.process_resources()["process_cpu_percent"] == 50. and calls == ["cpu", "cpu"]
    monkeypatch.setattr(app, "performance_snapshot", lambda: (_ for _ in ()).throw(AssertionError("记录不能再次取快照")))
    assert "30fps" in app.performance_line({"capture_fps": 30})
