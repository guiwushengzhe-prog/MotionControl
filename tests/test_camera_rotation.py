"""电脑摄像头画面横躺时自动转正。

笔记本立起来用、摄像头侧装时，人在画面里是横着的。模型照样认得出关键点，但所有
"上下"判定（举手、踏步、区域框、头控）都会错。所以用认出来的关键点判断人朝哪边
立着，四个方向里挑一个转正；判定为正以后锁住，侧身弯腰不会把画面转走。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol.control_kernel import CameraUnavailable, ControlKernel, NativeCameraService  # noqa: E402


class Output:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture()
def kernel():
    k = ControlKernel(Output())
    try:
        yield k
    finally:
        k.close()


def upright_pose() -> dict[str, tuple[float, float]]:
    """640×480 画面里直立坐着的人（像素），髋部出画。"""
    return {"nose": (320, 150), "left_shoulder": (380, 260), "right_shoulder": (260, 260)}


def rotate_point(x: float, y: float, w: int, h: int, turns_cw: int) -> tuple[float, float, int, int]:
    for _ in range(turns_cw % 4):
        x, y, w, h = h - y, x, h, w
    return x, y, w, h


def pose_map(points: dict[str, tuple[float, float]], w: int, h: int, turns_cw: int) -> tuple[dict, int, int]:
    out = {}
    for name, (x, y) in points.items():
        rx, ry, rw, rh = rotate_point(x, y, w, h, turns_cw)
        out[name] = {"x": rx / rw, "y": ry / rh, "z": 0.0, "score": 0.99}
    _, _, rw, rh = rotate_point(0, 0, w, h, turns_cw)
    return out, rw, rh


@pytest.mark.parametrize("turns", [0, 1, 2, 3])
def test_each_lying_direction_is_turned_back_upright(turns):
    """画面被顺时针转了 turns 个 90°，就要再顺时针转 4 - turns 个才正。"""
    pose, w, h = pose_map(upright_pose(), 640, 480, turns)
    assert NativeCameraService._upright_turns(pose, w, h) == (-turns) % 4


def test_the_laptop_on_this_desk_needs_counterclockwise():
    """实测：这台笔记本摄像头原始画面里，鼻子在肩膀中点的右边。"""
    pose = {
        "nose": {"x": 0.534, "y": 0.565, "score": 0.99},
        "left_shoulder": {"x": 0.340, "y": 0.818, "score": 0.99},
        "right_shoulder": {"x": 0.361, "y": 0.384, "score": 0.99},
    }
    turns = NativeCameraService._upright_turns(pose, 640, 480)
    assert NativeCameraService.QUARTER_TURNS["ccw"] == turns


def test_a_half_tilted_body_is_not_a_vote():
    """侧身 45° 左右不足以判断方向，宁可不投。"""
    pose = {
        "nose": {"x": 0.6, "y": 0.3, "score": 0.99},
        "left_shoulder": {"x": 0.5, "y": 0.5, "score": 0.99},
        "right_shoulder": {"x": 0.4, "y": 0.4, "score": 0.99},
    }
    assert NativeCameraService._upright_turns(pose, 480, 480) is None


def test_auto_rotation_turns_then_locks(kernel, isolated_user_data):
    camera = NativeCameraService(kernel)
    assert camera.rotation == "auto" and camera.applied_rotation == "none"
    lying, w, h = pose_map(upright_pose(), 640, 480, 1)
    with camera._lock:
        for _ in range(NativeCameraService.ROTATION_VOTE_FRAMES):
            camera._vote_rotation_locked(lying, w, h)
    assert camera.applied_rotation == "ccw"
    assert not camera._rotation_locked

    upright, w, h = pose_map(upright_pose(), 480, 640, 0)
    with camera._lock:
        for _ in range(NativeCameraService.ROTATION_VOTE_FRAMES):
            camera._vote_rotation_locked(upright, w, h)
    assert camera._rotation_locked

    # 下次启动直接从判出的方向起步。
    again = ControlKernel(Output())
    try:
        assert NativeCameraService(again).applied_rotation == "ccw"
    finally:
        again.close()


def test_a_fixed_rotation_is_used_as_is_and_remembered(kernel, isolated_user_data):
    NativeCameraService(kernel).set_rotation("cw")
    again = ControlKernel(Output())
    try:
        camera = NativeCameraService(again)
        assert (camera.rotation, camera.applied_rotation) == ("cw", "cw")
    finally:
        again.close()


def test_a_nonsense_rotation_is_refused(kernel):
    with pytest.raises(CameraUnavailable):
        NativeCameraService(kernel).set_rotation("45")
