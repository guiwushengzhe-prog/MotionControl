import re
from pathlib import Path

from motioncontrol.control_kernel import ControlKernel
from conftest import apply_layout


ROOT = Path(__file__).resolve().parents[1]


class Output:
    enabled = True

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def guard_pose(offset=0.0):
    result = {
        "left_shoulder": {"x": 0.40, "y": 0.30, "score": 1.0},
        "right_shoulder": {"x": 0.60, "y": 0.30, "score": 1.0},
        "left_hip": {"x": 0.44, "y": 0.55, "score": 1.0},
        "right_hip": {"x": 0.56, "y": 0.55, "score": 1.0},
    }
    for name, x, y in (
        ("left_elbow", .34, .42), ("right_elbow", .66, .42),
        ("left_wrist", .29, .53), ("right_wrist", .71, .53),
        ("left_ankle", .44, .92), ("right_ankle", .56, .92),
    ):
        result[name] = {"x": x + offset, "y": y - offset, "score": 1.0}
    return result


def test_new_kernel_defaults_body_motion_guard_to_off():
    kernel = ControlKernel(Output())
    try:
        assert kernel.body_motion_guard_enabled is False
        assert kernel.vertical_look["body_motion_guard"] is False
        state = kernel.status()
        assert state["body_motion_guard_enabled"] is False
        assert state["vertical_look"]["body_motion_guard"] is False
    finally:
        kernel.close()


def test_missing_guard_field_falls_back_to_off():
    kernel = ControlKernel(Output())
    try:
        apply_layout(kernel, {
            "zones": {}, "vertical_look": {"enabled": False},
        })
        assert kernel.body_motion_guard_enabled is False
    finally:
        kernel.close()


def test_the_guard_setting_survives_a_restart():
    """原来只存在参考场景文件里，没记录过场景的人每次重启都回到关。"""
    kernel = ControlKernel(Output())
    try:
        kernel.configure_head(body_motion_guard=True, vertical_exclusive=True)
    finally:
        kernel.close()
    again = ControlKernel(Output())
    try:
        assert again.body_motion_guard_enabled is True
        assert again.vertical_look["exclusive_axes"] is True
    finally:
        again.close()


def test_explicit_true_keeps_guard_with_vertical_look_disabled():
    kernel = ControlKernel(Output())
    try:
        apply_layout(kernel, {
            "zones": {},
            "vertical_look": {"enabled": False, "body_motion_guard": True},
        })
        assert kernel.vertical_look["enabled"] is False
        assert kernel.body_motion_guard_enabled is True

        now = 10.0
        kernel._update_body_motion_guard_locked(guard_pose(), now)
        now += 1 / 30
        kernel._update_body_motion_guard_locked(guard_pose(.03), now)
        assert kernel.body_motion_guard_active is True
        assert kernel._guard_horizontal_output_locked(.7, now) == 0.0
    finally:
        kernel.close()


def test_web_defaults_missing_guard_state_to_off():
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert re.search(r"bodyMotionGuard:false", app)
    assert "k.body_motion_guard_enabled??false" in app
    assert "k.vertical_look?.body_motion_guard===true" in app
    assert re.search(r'<input id="bodyMotionGuard" type="checkbox"', page)
    assert not re.search(r'<input[^>]*checked=""[^>]*id="bodyMotionGuard"', page)
