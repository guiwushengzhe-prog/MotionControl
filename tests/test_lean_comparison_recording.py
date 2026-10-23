"""同帧三状态记录的有效性、暂停、持久化与 API；不访问个人数据或设备。"""
import json
from types import SimpleNamespace

import pytest

from motioncontrol.depth_comparison import DepthComparison
from motioncontrol.lean_comparison_recording import LeanComparisonRecorder
from test_configuration_coordination import application  # noqa: F401
from test_profile_api_v2 import request_for


def record(recorder, at, mono=-10., depth=-10., *, revision=1, depth_at=None, token=None, finished=None):
    meta = {"sequence": round(at * 100), "sample_at": at, "signature": ["camera", "person"],
            "neutral_revision": 1, "teacher": depth,
            "lean_comparison": {"token": recorder.token if token is None else token,
                "depth_sample_at": at if depth_at is None else depth_at, "model": "mocapnet_personal"}}
    state = {"valid": mono is not None, "calibrated": mono is not None, "angle_deg": mono}
    recorder.observe(meta, state, 0., finished=at + .04 if finished is None else finished, revision=revision)


def test_same_frame_states_and_mismatch_timeline_are_written_then_stop(tmp_path):
    r = LeanComparisonRecorder(tmp_path / "记录")
    assert not r.enabled and not r.directory.exists()
    r.start()
    record(r, 1., -10, -12)
    record(r, 1.1, 0, -12)
    record(r, 1.2, 0, -11)
    record(r, 1.3, 12, 11)
    status = r.status()
    assert (status["valid"], status["agree"], status["disagree"]) == (4, 2, 2)
    assert status["agreement_percent"] == 50
    assert status["mismatch_seconds"] == pytest.approx(.2)
    assert status["matrix"]["forward"] == {"forward": 1, "neutral": 2, "back": 0}
    assert status["recent_disagreements"][0]["frames"] == 2
    r.stop()
    content = r.path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in content.splitlines()]
    assert rows[0]["schema"] == "motioncontrol.lean_comparison.v1"
    assert rows[-1]["type"] == "summary" and rows[-1]["valid"] == 4
    record(r, 1.4)
    assert r.path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("options", [{"mono": None}, {"depth": None}, {"depth_at": 0.8}, {"revision": 2}, {"finished": 2.}])
def test_missing_calibrating_stale_or_old_revision_never_counts_as_neutral(tmp_path, options):
    r = LeanComparisonRecorder(tmp_path)
    r.start()
    record(r, 1., **options)
    status = r.status()
    assert status["invalid"] == 1 and status["valid"] == 0
    assert status["agreement_percent"] is None
    assert status["latest"]["reason"] and status["latest"]["agree"] is None
    r.stop()


def test_previous_session_and_stopped_time_cannot_pollute_new_statistics(tmp_path):
    r = LeanComparisonRecorder(tmp_path)
    r.start()
    old_token = r.token
    record(r, 1., 0, -12)
    r.pause("摄像头停止")
    record(r, 10., 0, -12)
    assert r.status()["mismatch_seconds"] == 0
    r.stop()
    r.start()
    record(r, 11., token=old_token)
    assert r.frames == 0
    record(r, 11.1)
    assert r.frames == 1
    r.stop()


def test_file_failure_only_disables_recording(tmp_path):
    r = LeanComparisonRecorder(tmp_path)
    r.start()
    r._stream.close()
    # 模拟磁盘写入错误，不让可选记录终止识别。
    class FailedStream:
        def write(self, *_):
            raise OSError("磁盘不可写")
        def close(self):
            pass
    r._stream = FailedStream()
    record(r, 1.)
    assert not r.enabled and "磁盘不可写" in r.error


def test_continuous_recording_rotates_files_and_keeps_cumulative_counts(tmp_path, monkeypatch):
    monkeypatch.setattr("motioncontrol.lean_comparison_recording.MAX_FILE_BYTES", 1)
    r = LeanComparisonRecorder(tmp_path)
    r.start()
    for at in (1., 1.1, 1.2):
        record(r, at)
    r.stop()
    files = list(tmp_path.rglob("*.jsonl"))
    assert len(files) == 4
    assert r.status()["valid"] == 3
    assert sum(json.loads(line).get("type") == "frame" for path in files
               for line in path.read_text(encoding="utf-8").splitlines()) == 3


def test_recording_survives_restart_without_changing_model_or_learning():
    saved = {}
    kernel = SimpleNamespace(general_setting=lambda key, default: saved.get(key, default),
                             remember_general_setting=lambda key, value: saved.update({key: value}))
    first = DepthComparison(kernel)
    first.configure_recording({"enabled": True})
    first.close()
    restored = DepthComparison(kernel)
    assert restored.lean_recording.enabled
    assert not restored._control_requested()
    assert not restored.config["enabled"]
    restored.configure_recording({"enabled": False})


def test_persistence_failure_preserves_recording_switch(monkeypatch):
    def fail(*_):
        raise OSError("保存失败")
    engine = DepthComparison(SimpleNamespace(remember_general_setting=fail))
    with pytest.raises(OSError):
        engine.configure_recording({"enabled": True})
    assert not engine.lean_recording.enabled


def test_worker_pairs_reference_after_prediction_without_sending_reference_to_model(tmp_path, monkeypatch):
    import io
    import threading
    from motioncontrol.depth_comparison_worker import write_packet
    kernel = SimpleNamespace(_monocular_neutral_revision=1,
        sensitivity_experiments={"fore_aft_source": "monocular", "learn_monocular": False})
    engine = DepthComparison(kernel)
    engine.lean_recording.start()
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"python": "unused", "models": {"mocapnet_personal": {}}}), encoding="utf-8")
    engine._runtime = runtime
    stop = threading.Event()
    kernel.publish_monocular_lean = lambda *_: stop.set()
    engine.monocular = SimpleNamespace(feature_dim=42, neutral_reference=0., flush=lambda: None,
        set_context=lambda *_: None, update=lambda *_args, **_kwargs: {"valid": True, "calibrated": True, "angle_deg": -12.})
    output = io.BytesIO()
    write_packet(output, {"kind": "ready"})
    write_packet(output, {"kind": "frame", "sample_at": 10., "sequence": 1, "inference_ms": 2., "torso": {}})
    output.seek(0)
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=output)
    monkeypatch.setattr("motioncontrol.depth_comparison.owned_process.popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr("motioncontrol.depth_comparison.owned_process.terminate_and_wait", lambda *_: None)
    monkeypatch.setattr("motioncontrol.depth_comparison.time.monotonic", lambda: 10.04)
    sent = []
    monkeypatch.setattr("motioncontrol.depth_comparison.write_packet", lambda _, meta, pixels: sent.append((meta, pixels)))
    engine._pending = ({"sequence": 1, "sample_at": 10., "signature": ["camera", "person"],
        "neutral_revision": 1, "teacher": -11., "lean": True, "input_scope": "pose", "effective_fps": 15,
        "frame_region": [0, 0, 640, 360], "source_shape": [360, 640],
        "lean_comparison": {"token": engine.lean_recording.token, "model": "mocapnet_personal", "depth_sample_at": 10.}}, None)
    engine._run(engine._generation, stop, {**engine.config, "model": "mocapnet_personal"})
    assert len(sent) == 1 and sent[0][1] == b""
    assert "teacher" not in sent[0][0] and "lean_comparison" not in sent[0][0]
    assert engine.lean_recording.status()["agree"] == 1
    engine.lean_recording.stop()


def test_recording_toggle_keeps_running_model_and_preserves_control_source(monkeypatch):
    saved = []
    kernel = SimpleNamespace(sensitivity_experiments={"fore_aft_source": "depth"},
        remember_general_setting=lambda key, value: saved.append((key, value)))
    engine = DepthComparison(kernel)
    monkeypatch.setattr(engine, "suspend", lambda: pytest.fail("记录开关不能重启现有模型"))
    engine.configure_recording({"enabled": True})
    assert engine._requested() and engine._selected_model() == "mocapnet_personal"
    assert kernel.sensitivity_experiments["fore_aft_source"] == "depth"
    assert saved == [("lean_comparison_recording", True)]
    engine.configure_recording({"enabled": False})
    assert not engine._requested()


def test_recording_api_validates_local_only_and_never_starts_camera_or_output(application, monkeypatch):
    app, replies = application, []
    monkeypatch.setattr(app.RUNTIME.camera, "start", lambda: pytest.fail("记录开关不能启动摄像头"))
    request = request_for(app, "/api/lean-comparison-recording", replies)
    request._body = lambda: {"enabled": True}
    request.do_POST()
    assert replies[-1][0] == 200 and replies[-1][1]["lean_recording"]["enabled"]
    assert app.KERNEL.general_setting("lean_comparison_recording", False) is True
    request._is_loopback = lambda: False
    request.do_POST()
    assert replies[-1][0] == 403
    request._is_loopback = lambda: True
    request._body = lambda: {"enabled": "true"}
    request.do_POST()
    assert replies[-1][0] == 400
    app.RUNTIME.camera.depth_comparison.configure_recording({"enabled": False})
