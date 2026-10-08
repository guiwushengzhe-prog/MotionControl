"""通用扫描区分设备身份与画面；不打开真人摄像头。"""
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from motioncontrol.control_kernel import NativeCameraService


class Kernel:
    def general_setting(self, key, default=None):
        return default


class Capture:
    def __init__(self, frames=(), opened=True):
        self.frames = iter(frames)
        self.opened = opened
        self.released = False

    def isOpened(self):
        return self.opened

    def read(self):
        frame = next(self.frames, None)
        return frame is not None, frame

    def release(self):
        self.released = True


@pytest.fixture
def camera(monkeypatch):
    monkeypatch.setattr(NativeCameraService, "_camera_catalog", staticmethod(lambda **_: {}))
    monkeypatch.setattr("motioncontrol.control_kernel.kinect_camera.list_devices", lambda: [])
    return NativeCameraService(Kernel())


def scan(camera, monkeypatch, catalog, captures):
    opened = []
    monkeypatch.setattr(camera, "_camera_catalog", lambda **_: catalog)
    def open_capture(index, api):
        opened.append(index)
        return captures[index]
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(VideoCapture=open_capture, CAP_DSHOW=700))
    return camera.list_cameras()["devices"], opened


def test_named_scan_distinguishes_black_unavailable_static_and_warming(camera, monkeypatch):
    black = np.zeros((8, 12, 3), dtype=np.uint8)
    static = np.full((8, 12, 3), 80, dtype=np.uint8)
    catalog = {i: {"index": i, "name": name} for i, name in enumerate(
        ["镜头遮挡", "设备被占用", "静止画面", "刚启动的摄像头"])}
    captures = {
        0: Capture([black] * 3), 1: Capture(opened=False),
        2: Capture([static] * 3), 3: Capture([black, static, static]),
    }
    devices, opened = scan(camera, monkeypatch, catalog, captures)
    assert opened == [0, 1, 2, 3]  # 不盲探不存在的 4、5 号。
    assert [d["picture_state"] for d in devices] == ["black", "unavailable", "ready", "ready"]
    assert devices[0]["picture_black"] is True
    assert devices[2]["sampled_frames"] == 3
    assert devices[3]["picture_black"] is False
    assert all(c.released for c in captures.values())


def test_no_frames_and_invalid_frames_are_kept_but_not_claimed_ready(camera, monkeypatch):
    captures = {0: Capture([None] * 3), 1: Capture([np.empty((0, 0, 3))] * 3)}
    catalog = {i: {"index": i, "name": "摄像头" + str(i)} for i in captures}
    devices, _ = scan(camera, monkeypatch, catalog, captures)
    assert [d["picture_state"] for d in devices] == ["no_frames", "no_frames"]
    assert all(d["sampled_frames"] == 0 for d in devices)
    assert all(c.released for c in captures.values())


def test_virtual_camera_is_valid_when_it_has_a_picture(camera, monkeypatch):
    frame = np.full((8, 12, 3), 80, dtype=np.uint8)
    devices, _ = scan(camera, monkeypatch, {2: {"index": 2, "name": "虚拟摄像头", "virtual": True}},
                      {2: Capture([frame] * 3)})
    assert devices[0]["virtual"]
    assert devices[0]["picture_state"] == "ready"


def test_active_camera_is_not_reopened(camera, monkeypatch):
    camera._session = object()
    camera.picture_black = True
    camera.capture_width, camera.capture_height = 640, 480
    devices, opened = scan(camera, monkeypatch, {0: {"index": 0, "name": "内置摄像头"}}, {})
    assert opened == []
    assert devices[0]["name"] == "内置摄像头"
    assert devices[0]["picture_state"] == "black"
    assert devices[0]["in_use"]


def test_kinect_duplicate_is_removed_without_removing_other_video_sources(camera, monkeypatch):
    monkeypatch.setattr("motioncontrol.control_kernel.kinect_camera.list_devices",
                        lambda: [{"id": "kinect2:test", "name": "微软 Kinect"}])
    frame = np.full((8, 12, 3), 80, dtype=np.uint8)
    catalog = {0: {"index": 0, "name": "内置摄像头"},
               1: {"index": 1, "name": "微软 Kinect 普通视频接口", "kinect_v2": True}}
    devices, opened = scan(camera, monkeypatch, catalog, {0: Capture([frame] * 3)})
    assert opened == [0]
    assert [d.get("id", d.get("index")) for d in devices] == [0, "kinect2:test"]


def test_empty_windows_catalog_does_not_probe_random_indices(camera, monkeypatch):
    devices, opened = scan(camera, monkeypatch, {}, {})
    assert devices == opened == []


def test_enumeration_failure_retains_legacy_scan(camera, monkeypatch):
    frame = np.full((8, 12, 3), 80, dtype=np.uint8)
    devices, opened = scan(camera, monkeypatch, None,
        {i: Capture([frame] * 3) if i == 2 else Capture(opened=False) for i in range(6)})
    assert opened == list(range(6))
    assert [d["index"] for d in devices] == [2]


def test_compatibility_backend_does_not_claim_an_unverified_numeric_name(camera, monkeypatch):
    monkeypatch.setattr(camera, "_camera_catalog", lambda **_: {0: {"name": "直接采集设备"}})
    camera.selected_backend = "msmf"
    assert camera._camera_identity()["camera_name"] is None
