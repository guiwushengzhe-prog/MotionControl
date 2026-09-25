"""Shared test setup.

The one thing that has to be global: user data now resolves through
%LOCALAPPDATA%\\MotionControl rather than the program folder, so without this a
test that constructs a GameProfileStore, a ControlKernel or a VoiceService
would read and overwrite the developer's own mappings, calibration and paired
devices. Pointing MOTIONCONTROL_USER_DIR at a per-test directory makes that
impossible rather than merely unlikely.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    """Give every test its own user-data directory."""
    root = tmp_path / "user-data"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(root))
    return root


def apply_layout(kernel, layout: dict) -> dict:
    """旧测试里 ``configure_scene_layout`` 的替身：圆圈换成定住的框，上下视角设置照搬。

    参考场景删了以后，固定在画面上的区域就是定住的跟随框；换法和升级时迁移旧场景
    文件用的是同一个函数（legacy_scene_to_frozen），这里顺带把它也测到了。和原来
    一样，没给上下视角设置就是关着。
    """
    from motioncontrol.control_kernel import legacy_scene_to_frozen

    rects, vertical, _anchor = legacy_scene_to_frozen(layout)
    kernel.configure_vertical_look(vertical if vertical else {"enabled": False})
    if rects:
        kernel.set_frozen_zones(rects)
    return kernel.status()


def official_pose_docs() -> list[dict]:
    """仓库里 cloud/official_poses/ 下的全部官方动作，校验过的。"""
    import json
    from pathlib import Path

    from motioncontrol_shared.pose_library import normalize_action

    folder = Path(__file__).resolve().parent.parent / "cloud" / "official_poses"
    return [normalize_action(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(folder.glob("*.json")) if path.name != "signatures.json"]


@pytest.fixture(autouse=True)
def official_pose_actions():
    """默认当作官方动作全都下载了。

    开合跳、下蹲这些以前是内置的，一大批测试建一个内核就直接拿它们来测。现在它们
    要下载才有；每个测试都先下载一遍只是噪音。要测"没下载就认不出"的，用
    ``no_downloaded_actions``。
    """
    from motioncontrol_shared import pose_library

    before = pose_library.registered()
    pose_library.register(official_pose_docs())
    yield
    pose_library.register(before.values())


@pytest.fixture()
def no_downloaded_actions(official_pose_actions):
    """这台电脑一个动作都没从云端下载过：只有原地踏步和小腿向后抬起。"""
    from motioncontrol_shared import pose_library

    pose_library.register([])
    yield
