"""用户自己录的姿势：存取，以及它在控制内核里真的会触发。

匹配算法本身在 tests/test_pose_template.py 里测。这里测的是接得对不对——录下来
能不能存住、阈值和停留时间是不是真的起作用、改了设置会不会立刻生效，以及最要紧的
一条：它走的是和内置姿势同一条绑定通路，而不是另开一条。
"""

from __future__ import annotations

import json
import time

import pytest

from custom_poses import (
    DEFAULT_DWELL_FRAMES,
    DEFAULT_THRESHOLD,
    MAX_POSES,
    CustomPoseError,
    CustomPoseStore,
)

# tests/ 不是包，和 head_test_support 一样直接按模块名导入。
from test_pose_template import T_POSE, pose


@pytest.fixture
def store(tmp_path):
    return CustomPoseStore(tmp_path / "custom_poses.json")


# --- 存取 ---------------------------------------------------------------------

def test_capture_stores_a_reusable_template(store):
    entry = store.capture(T_POSE, "双手平举")
    assert entry["id"] == "custom1"
    assert entry["name"] == "双手平举"
    assert entry["threshold"] == DEFAULT_THRESHOLD
    assert entry["dwell_frames"] == DEFAULT_DWELL_FRAMES

    # 重新打开要能读回来——这是"录一次就一直能用"的前提。
    again = CustomPoseStore(store.path)
    assert [p["id"] for p in again.poses] == ["custom1"]
    assert again.evaluate(T_POSE)["custom1"]["score"] == pytest.approx(1.0)


def test_ids_never_collide_with_the_built_in_pose(store):
    """内置的 hands_cross 和自定义姿势共用 pose.* 命名空间。"""
    for _ in range(3):
        store.capture(T_POSE)
    ids = [p["id"] for p in store.poses]
    assert ids == ["custom1", "custom2", "custom3"]
    assert "hands_cross" not in ids


def test_a_deleted_id_is_reused_rather_than_left_as_a_hole(store):
    store.capture(T_POSE)
    store.capture(T_POSE)
    store.remove("custom1")
    assert store.capture(T_POSE)["id"] == "custom1"


def test_capturing_a_body_it_cannot_see_is_refused(store):
    """看不清时给一个模板，比拒绝更糟：之后每次比对都是错的。"""
    hidden = {name: {**point, "score": 0.1} for name, point in T_POSE.items()}
    with pytest.raises(CustomPoseError, match="看不清"):
        store.capture(hidden)
    assert store.poses == []


def test_there_is_a_limit(store):
    for _ in range(MAX_POSES):
        store.capture(T_POSE)
    with pytest.raises(CustomPoseError, match="最多"):
        store.capture(T_POSE)


@pytest.mark.parametrize("field, value, message", [
    ("threshold", 1.5, "相似度阈值"),
    ("threshold", "abc", "相似度阈值"),
    ("dwell_frames", 0, "停留帧数"),
    ("dwell_frames", 500, "停留帧数"),
    ("name", "   ", "名字不能为空"),
])
def test_bad_settings_are_refused_with_a_readable_message(store, field, value, message):
    store.capture(T_POSE)
    with pytest.raises(CustomPoseError, match=message):
        store.update("custom1", **{field: value})


def test_a_corrupt_file_does_not_stop_the_app(tmp_path):
    """姿势文件坏了，最多是姿势没了，不能让程序起不来。"""
    path = tmp_path / "custom_poses.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    broken = CustomPoseStore(path)
    assert broken.poses == []
    assert "读取失败" in broken.last_error
    # 还能继续用：录一个新的会把文件覆盖掉。
    broken.capture(T_POSE)
    assert json.loads(path.read_text(encoding="utf-8"))["poses"]


def test_status_leaves_the_template_out(store):
    """模板是十几个浮点数，界面用不上，发过去只是噪声。"""
    store.capture(T_POSE, "平举")
    item = store.status()[0]
    assert "template" not in item
    assert item["trigger"] == "pose.custom1"


# --- 比对 ---------------------------------------------------------------------

def test_evaluate_reports_a_hit_only_above_the_threshold(store):
    store.capture(T_POSE)
    store.update("custom1", threshold=0.95)

    assert store.evaluate(T_POSE)["custom1"]["hit"] is True
    # 抖动 10 度约 93%，在 0.95 之下。
    assert store.evaluate(pose(left_arm=100, right_arm=80))["custom1"]["hit"] is False


def test_a_disabled_pose_is_skipped_entirely(store):
    store.capture(T_POSE)
    store.update("custom1", enabled=False)
    assert store.evaluate(T_POSE) == {}


def test_an_invisible_body_is_reported_as_such_not_as_zero_similarity(store):
    """"看不清"和"很不像"是两回事，界面要能分开显示。"""
    store.capture(T_POSE)
    result = store.evaluate({name: {**p, "score": 0.1} for name, p in T_POSE.items()})
    assert result["custom1"]["visible"] is False
    assert result["custom1"]["hit"] is False


# --- 和控制内核接起来 ----------------------------------------------------------

@pytest.fixture
def kernel():
    from control_kernel import ControlKernel
    from output_backend import OutputManager

    import pathlib
    return ControlKernel(OutputManager(pathlib.Path(".")))


def feed(kernel, body, frames: int = 10) -> None:
    for _ in range(frames):
        kernel.handle_pose_map("test", body, width=640, height=480)
        time.sleep(0.004)


def test_a_captured_pose_actually_fires_in_the_kernel(kernel, store):
    """整条链路：录 -> 摆同一个姿势 -> 内核认出来 -> 出现在 pose_active。

    出现在 pose_active 是关键，因为 _dispatch_controls_locked 就是从那里按
    pose.<id> 去查绑定的——也就是说自定义姿势自动获得了按游戏映射、冲突检查、
    紧急停止一起松开这些已有行为，不需要另写。
    """
    store.capture(T_POSE, "平举")
    kernel.configure_custom_poses(store)

    feed(kernel, T_POSE, 10)
    assert "custom1" in kernel.pose_active
    assert kernel.custom_pose_scores["custom1"] == pytest.approx(1.0)

    feed(kernel, pose(), 10)
    assert "custom1" not in kernel.pose_active


def test_dwell_frames_really_delay_the_trigger(kernel, store):
    store.capture(T_POSE)
    store.update("custom1", dwell_frames=20)
    kernel.configure_custom_poses(store)

    feed(kernel, T_POSE, 10)
    assert "custom1" not in kernel.pose_active, "还没停够就触发了"
    feed(kernel, T_POSE, 15)
    assert "custom1" in kernel.pose_active


def test_changing_settings_restarts_the_dwell_count(kernel, store):
    """阈值和停留时间定义的就是"什么算触发"，改了它们不该沿用旧的判定结果。

    沿用的话，新设置要等到下次松开才生效，用户会以为没保存。
    """
    store.capture(T_POSE)
    kernel.configure_custom_poses(store)
    feed(kernel, T_POSE, 10)
    assert "custom1" in kernel.pose_active

    store.update("custom1", dwell_frames=20)
    kernel.configure_custom_poses(store)
    feed(kernel, T_POSE, 10)
    assert "custom1" not in kernel.pose_active


def test_disabling_a_pose_releases_it(kernel, store):
    """禁用时不释放，那个键会一直停在按下状态。"""
    store.capture(T_POSE)
    kernel.configure_custom_poses(store)
    feed(kernel, T_POSE, 10)
    assert "custom1" in kernel.pose_active

    store.update("custom1", enabled=False)
    kernel.configure_custom_poses(store)
    feed(kernel, T_POSE, 10)
    assert "custom1" not in kernel.pose_active


def test_a_broken_template_does_not_stop_recognition(kernel, store):
    """一个坏模板不该让内置姿势和别的自定义姿势一起停摆。"""
    store.capture(T_POSE)
    store.poses[0]["template"] = {"schema": "坏的"}
    kernel.configure_custom_poses(store)
    feed(kernel, T_POSE, 10)  # 不抛异常就算通过
    assert "custom1" not in kernel.pose_active


def test_a_custom_pose_uses_the_same_binding_lookup_as_a_built_in(kernel, store):
    """绑定查的是 control_bindings["pose.<id>"]，和 hands_cross 走同一个函数。"""
    store.capture(T_POSE)
    kernel.configure_custom_poses(store)
    kernel.configure_bindings({"poses": {
        "custom1": {"action": {"type": "gamepad", "target": "Y", "behavior": "tap"}},
    }})
    binding = kernel._effective_binding_locked("pose.custom1")
    assert binding["action"]["target"] == "Y"
