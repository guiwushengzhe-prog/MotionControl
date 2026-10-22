"""表面参考、尺度留验、可关闭队列与独立进程协议；不使用摄像头或用户配置。"""
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest

from motioncontrol.depth_comparison import DEFAULTS, DepthComparison, validate_config
from motioncontrol.depth_comparison_metrics import DepthMetrics, kinect_distance_summary, surface_references
from motioncontrol.depth_comparison_worker import read_packet, write_packet
from test_configuration_coordination import application  # noqa: F401
from test_profile_api_v2 import request_for


def reference(z=2, x=.5):
    return {"name": "left_shoulder", "x": x, "y": .5, "z": z}


def test_raw_surface_reference_is_distinct_from_skeleton_joint_center():
    body = {"matched_to_model": True, "body_valid": True, "points": {"left_shoulder": {
        "score": 1, "z": 2.3, "color_x": .3, "color_y": .5,
        "surface": {"z": 2, "color_x": .31, "color_y": .5}}}}
    points = surface_references(body, {"surface_age_ms": 15, "image_mirrored": True})
    assert points == [reference(z=2, x=.69)]
    assert surface_references(body, {"surface_age_ms": 81}) == []
    body["body_valid"] = False
    assert surface_references(body, {"surface_age_ms": 0}) == []


def test_native_torso_distance_uses_surface_metres_without_any_model():
    body = {"matched_to_model": True, "body_valid": True, "sample_at": 10, "surface_age_ms": 12,
            "points": {name: {"score": 1, "z": z + .1,
                "surface": {"z": z, "color_x": .5, "color_y": .5}}
                for name, z in (("left_shoulder", .7), ("right_shoulder", .9), ("left_wrist", .5))}}
    result = kinect_distance_summary(body, 10.05)
    assert result["surface_m"] == .8 and result["joint_m"] == .9
    assert result["reference_points"] == 2, "手在躯干前面时不能改变躯干参考距离"
    assert result["sample_age_ms"] == 50
    assert kinect_distance_summary(body, 10.3) is None
    body["surface_age_ms"] = 90
    assert kinect_distance_summary(body, 10.05) is None


def test_metric_error_uses_native_metres_and_counts_future_frames_without_alignment():
    metrics = DepthMetrics()
    result = metrics.evaluate(np.full((10, 10), 2.12), [reference()], now=0)
    assert result["mae_cm"] == result["p95_cm"] == 12
    assert result["validation_samples"] == 1
    result = metrics.evaluate(np.full((10, 10), 2.12), [], now=1)
    assert result["reference_points"] == 0
    assert result["validation_samples"] == 1


def test_nonfinite_and_zero_prediction_does_not_fabricate_distances():
    metrics = DepthMetrics()
    for value in (float("nan"), float("inf"), 0, -1):
        result = metrics.evaluate(np.full((10, 10), value), [reference()], now=0)
        assert result["reference_points"] == result["validation_samples"] == 0


def test_relative_calibration_is_frozen_and_calibration_frames_are_excluded():
    metrics = DepthMetrics(relative=True)
    depth = np.tile(np.linspace(.25, .6, 30), (20, 1))
    points = [{**reference(x=(i + .5) / 30), "name": str(i), "z": 1 / (2 * float(depth[10, i]) + .1)} for i in range(3, 27)]
    assert metrics.evaluate(depth, points, now=0)["mae_cm"] is None
    metrics.start_calibration(now=0)
    for at in (0, 1, 2, 3):
        result = metrics.evaluate(depth, points, now=at)
        assert result["validation_samples"] == 0
    assert result["metric_ready"] and not result["calibrating"]
    mapping = dict(metrics.mapping)
    result = metrics.evaluate(depth, points, now=4)
    assert result["mae_cm"] < .01
    displaced = [{**p, "z": p["z"] + .2} for p in points]
    result = metrics.evaluate(depth, displaced, now=5)
    assert result["mae_cm"] > 9
    assert metrics.mapping == mapping, "未来答案不能被拿来逐帧重缩放"


def test_failed_recalibration_keeps_previous_scale():
    metrics = DepthMetrics(relative=True)
    metrics.mapping = {"gain": 1, "offset": 0, "min": .1, "max": 1}
    expected = dict(metrics.mapping)
    metrics.start_calibration(now=0)
    for at in (0, 1, 2, 3):
        result = metrics.evaluate(np.full((10, 10), .5), [reference()] * 20, now=at)
    assert metrics.mapping == expected
    assert "参考不足" in result["calibration_message"]


def test_motion_is_not_counted_as_stationary_jitter():
    metrics = DepthMetrics()
    for i in range(8):
        z = 2 + i * .1
        result = metrics.evaluate(np.full((10, 10), z + .1), [reference(z)], now=i * .1)
    assert result["stationary_samples"] == 0
    for i in range(8):
        result = metrics.evaluate(np.full((10, 10), 2.1), [reference(2)], now=1 + i * .1)
    assert result["stationary_samples"] > 0
    assert result["stationary_jitter_cm"] == 0


def test_worker_packet_roundtrip_and_truncated_data():
    stream = io.BytesIO()
    write_packet(stream, {"name": "米制", "a": 1}, b"123")
    stream.seek(0)
    assert read_packet(stream) == ({"name": "米制", "a": 1}, b"123")
    with pytest.raises(EOFError):
        read_packet(io.BytesIO(b"short"))
    with pytest.raises(ValueError):
        read_packet(io.BytesIO(b"\xff" * 8))


@pytest.mark.parametrize("changes", [{"enabled": 1}, {"model": "unknown"}, {"input_size": 700}, {"max_fps": True},
                                     {"input_scope": "face"}, {"auto_scale": 1}, {"shell": "oops"}])
def test_configuration_rejects_unknown_or_invalid_values(changes):
    with pytest.raises(ValueError):
        validate_config(DEFAULTS, changes)


def test_disabled_comparison_never_copies_or_opens_a_frame():
    comparison = DepthComparison(SimpleNamespace())
    comparison.submit(object(), None, None, None, "none", 0, "test")
    assert comparison._pending is None and comparison._thread is None
    assert comparison.status()["state"] == "off"


def test_failed_persistence_does_not_change_live_config(monkeypatch):
    def failure(*_args):
        raise OSError("模拟保存失败")
    comparison = DepthComparison(SimpleNamespace(remember_general_setting=failure))
    with pytest.raises(OSError):
        comparison.configure({"input_size": 280})
    assert comparison.config == DEFAULTS


def test_reset_statistics_includes_timing_window():
    comparison = DepthComparison(SimpleNamespace())
    comparison.config["enabled"] = True
    comparison._state = "running"
    for window in (comparison._latencies, comparison._inference, comparison._times):
        window.append(5)
    comparison.action("reset")
    assert comparison._reset
    assert not comparison._latencies and not comparison._inference and not comparison._times


def test_latest_frame_queue_replaces_old_frame_and_shutdown_clears_it(monkeypatch):
    comparison = DepthComparison(SimpleNamespace())
    comparison.config["enabled"] = True
    comparison._thread = SimpleNamespace(is_alive=lambda: True, join=lambda **_: None)
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    monkeypatch.setattr("motioncontrol.depth_comparison.time.monotonic", lambda: 10)
    comparison.submit(frame, None, None, None, "none", 1, "source")
    comparison._last_submit = 0
    comparison.submit(frame + 1, None, None, None, "none", 2, "source")
    assert comparison._skipped == 1
    assert comparison._pending[0]["sample_at"] == 2
    assert comparison._pending[1][0, 0, 0] == 1
    comparison.suspend()
    assert comparison._pending is None and comparison.preview() is None


def test_comparison_api_does_not_touch_output_or_camera(application, monkeypatch):
    app, replies = application, []
    changes = []
    monkeypatch.setattr(app.RUNTIME.camera.depth_comparison, "configure", lambda body: changes.append(body) or {"config": body})
    monkeypatch.setattr(app.RUNTIME.camera, "start", lambda: pytest.fail("实验设置不能启动摄像头"))
    request = request_for(app, "/api/depth-comparison", replies)
    request._body = lambda: {"enabled": True}
    request.do_POST()
    assert changes == [{"enabled": True}]
    assert replies[-1] == (200, {"config": {"enabled": True}})
    request._is_loopback = lambda: False
    request.do_POST()
    assert replies[-1][0] == 403 and len(changes) == 1


def test_comparison_status_does_not_load_a_model_and_missing_resources_are_reported(application):
    app, replies = application, []
    request = request_for(app, "/api/depth-comparison", replies)
    request.do_GET()
    assert replies[-1][0] == 200
    assert replies[-1][1]["state"] == "off"
    assert all(not model["available"] for model in replies[-1][1]["models"].values())
    request._body = lambda: {"enabled": True}
    request.do_POST()
    assert replies[-1][0] == 400
    assert app.RUNTIME.camera.depth_comparison.config == DEFAULTS


@pytest.mark.parametrize("body_mode", ["computer", "phone"])
def test_regular_performance_poll_exposes_depth_experiment(application, body_mode):
    application.RUNTIME.body_mode = body_mode
    result = application.performance_snapshot()
    assert result["depth_comparison"]["state"] == "off"
    if body_mode == "computer":
        assert "hfhpe" in result


def test_native_fixed_scale_keeps_raw_error_and_never_learns_from_validation():
    metrics = DepthMetrics()
    depth = np.tile(np.linspace(2, 4, 40), (10, 1))
    points = [{**reference(float(depth[5, i]) / 2, x=(i + .5) / 40), "name": str(i)} for i in range(3, 37)]
    metrics.start_calibration(now=0)
    for at in (0, 1, 2, 3):
        result = metrics.evaluate(depth, points, now=at)
        assert result["validation_samples"] == result["calibrated_validation_samples"] == 0
    assert metrics.mapping == {"gain": .5}
    result = metrics.evaluate(depth, points, now=4)
    assert result["raw_mae_cm"] > 100 and result["calibrated_mae_cm"] == 0
    shifted = [{**p, "z": p["z"] + .2} for p in points]
    result = metrics.evaluate(depth, shifted, now=5)
    assert result["calibrated_mae_cm"] == 10
    assert metrics.mapping == {"gain": .5}
    assert result["points"][0]["model_m"] == round(float(depth[5, 3]), 4)
    assert result["points"][0]["calibrated_m"] == round(float(depth[5, 3]) / 2, 4)


def test_native_failed_recalibration_preserves_previous_fixed_scale():
    metrics = DepthMetrics()
    metrics.mapping = {"gain": .5}
    metrics.start_calibration(now=0)
    inconsistent = [{**reference(z=1 + i / 10), "name": str(i)} for i in range(20)]
    for at in (0, 1, 2, 3):
        result = metrics.evaluate(np.full((10, 10), 3), inconsistent, now=at)
    assert metrics.mapping == {"gain": .5}
    assert "不一致" in result["calibration_message"]


@pytest.mark.parametrize("direction", [-1, 1])
def test_relative_scale_rejects_reversed_or_almost_constant_depth(direction):
    metrics = DepthMetrics(relative=True)
    metrics.start_calibration(now=0)
    # 反向变化或者只有极弱相关的噪声，不能靠接近常数的拟合冒充深度。
    rng = np.random.default_rng(7)
    values = np.linspace(.04, .1, 200)
    inverse = .65 - values if direction < 0 else .65 + .01 * values + rng.normal(0, .02, len(values))
    samples = [(reference(z=float(1 / z)), float(x)) for x, z in zip(values, inverse)]
    for at in (0, 1, 2, 3):
        result = metrics.evaluate_samples(samples, now=at)
    assert not result["scale_ready"] and result["calibration_correlation"] < .5
    assert "未可靠跟随" in result["calibration_message"]
    assert metrics.evaluate_samples(samples, now=4)["validation_samples"] == 0
