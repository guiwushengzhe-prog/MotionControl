"""前后响应分别调节，保留真实倾角、回正滞回及深度原有门槛。"""
import json

import pytest

from motioncontrol.body_lean import BodyLeanController
from motioncontrol.sensitivity_experiments import DEFAULTS, configure
from motioncontrol.lean_comparison_recording import LeanComparisonRecorder
from test_body_lean import poses, feed


def test_small_backward_lean_enters_holds_and_releases_with_original_angle():
    controller = BodyLeanController()
    def feed(angle, at):
        state = {"valid": True, "calibrated": True, "sample_at": at, "angle_deg": angle}
        return controller.update(None, None, None, 640, 360, at, fore_aft_source="monocular",
            forward=True, back=True, monocular=state, monocular_forward_on_deg=6, monocular_back_on_deg=3)
    for i in range(10):
        active = feed(3.8, i * .1)
    assert active == {"lean_back"}
    assert controller.status(.9)["sources"]["monocular"]["back_deg"] == pytest.approx(3.8)
    for i in range(10, 16):
        active = feed(2.5, i * .1)
    assert active == {"lean_back"}, "回正释放门槛沿用进入门槛的原比例，临界处不反复跳动"
    for i in range(16, 23):
        active = feed(0, i * .1)
    assert not active
    for i in range(23, 33):
        active = feed(-6.5, i * .1)
    assert active == {"lean_forward"}
    assert controller.update(None, None, None, 640, 360, 4, fore_aft_source="monocular", forward=True, back=True) == set()


@pytest.mark.parametrize("value", [True, False, "3", 0, 1.9, 15.1, float("nan"), float("inf")])
def test_invalid_threshold_does_not_enter_configuration(value):
    with pytest.raises(ValueError):
        configure(DEFAULTS, {"monocular_back_on_deg": value})


def test_recording_keeps_original_seven_degree_state_and_actual_response_threshold(tmp_path):
    recorder = LeanComparisonRecorder(tmp_path)
    recorder.start()
    meta = {"sequence": 1, "sample_at": 1., "signature": ["camera"], "neutral_revision": 1, "teacher": 11.,
        "lean_comparison": {"token": recorder.token, "model": "mocapnet_personal", "depth_sample_at": 1.,
                            "forward_on_deg": 6., "back_on_deg": 3.}}
    recorder.observe(meta, {"valid": True, "calibrated": True, "angle_deg": 3.8}, 0., finished=1.02, revision=1)
    latest = recorder.status()["latest"]
    assert latest["agree"] is True
    assert latest["monocular"]["original_state"] == "neutral"
    assert latest["monocular"]["state"] == "back"
    assert latest["monocular"]["angle_deg"] == 3.8
    recorder.stop()
    rows = [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]
    assert rows[1]["thresholds"]["monocular_back_on_deg"] == 3.


def test_monocular_threshold_does_not_lower_depth_or_model_threshold():
    controller = BodyLeanController()
    options = dict(back=True, forward=True, fore_aft_source="depth", compare=True,
                   monocular_forward_on_deg=6, monocular_back_on_deg=3)
    feed(controller, poses(), 10., **options)
    assert feed(controller, poses(back=3.8), 11.4, count=8, **options) == set()
    assert feed(controller, poses(back=9), 11.7, count=8, **options) == {"lean_back"}
