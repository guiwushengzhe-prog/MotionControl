"""录制生命周期用临时文件与假时钟验证，不采集真人或屏幕。"""

from pathlib import Path

import pytest

from motioncontrol.studio import StudioService


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Portrait:
    def __init__(self):
        self.enabled = False
        self.closed = False
        self.config = {}
        self.revision = 0

    def update(self, config):
        self.config = dict(config)
        self.revision += 1

    def set_enabled(self, enabled):
        self.enabled = enabled

    def status(self):
        return {"ready": self.enabled, "revision": self.revision, "reason": None}

    def frame_snapshot_png(self):
        return (b"processed picture" if self.enabled else None), self.revision

    def close(self):
        self.closed = True


class Bridge:
    def __init__(self):
        self.enabled = False

    def set_studio_video(self, enabled):
        self.enabled = enabled


@pytest.fixture
def factory(tmp_path):
    services = []

    def make(clock, folder="studio"):
        portrait, bridge = Portrait(), Bridge()
        service = StudioService(portrait, bridge, tmp_path / folder, clock=clock, background=False)
        services.append(service)
        return service, portrait, bridge

    yield make
    for service in services:
        service.close()


def test_multiple_leases_keep_phone_video_until_last_lease_closes_or_expires(factory):
    clock = Clock()
    service, portrait, bridge = factory(clock)
    service.update({"source": "phone"})
    service.demand(True, "preview-a")
    service.demand(True, "preview-b")
    service.demand(False, "preview-a")
    assert portrait.enabled and bridge.enabled
    # 后台清扫独立于状态/画面请求，关闭页面后仍能清理过期租约。
    clock.advance(31)
    service._sweep()
    assert not portrait.enabled
    assert not bridge.enabled
    assert service.frame_png() is None


def test_close_stops_demand_and_keeps_received_interrupted_video(factory):
    clock = Clock()
    service, portrait, bridge = factory(clock)
    service.update({"source": "phone"})
    service.demand(True, "preview")
    item = service.start_recording({"mime_type": "video/webm"})
    received = b"\x1a\x45\xdf\xa3saved fragment"
    service.append_recording(item["id"], received)
    service.close()
    assert portrait.closed and not portrait.enabled and not bridge.enabled
    saved = service.state()["recordings"][0]
    assert saved["finished"] and saved["interrupted"]
    assert Path(saved["path"]).read_bytes() == received


def test_recording_validates_first_fragment_appends_in_order_and_rejects_after_finish(factory):
    service, _, _ = factory(Clock())
    item = service.start_recording({"mime_type": "video/webm;codecs=vp8,opus"})
    with pytest.raises(ValueError, match="首段"):
        service.append_recording(item["id"], b"invalid first fragment")
    assert Path(item["path"]).read_bytes() == b""
    first, second = b"\x1a\x45\xdf\xa3header", b"following media bytes"
    service.append_recording(item["id"], first)
    result = service.append_recording(item["id"], second)
    assert result["bytes"] == len(first + second)
    finished = service.finish_recording(item["id"])
    assert finished["finished"] and not finished["interrupted"]
    assert Path(finished["path"]).read_bytes() == first + second
    with pytest.raises(ValueError, match="已经结束"):
        service.append_recording(item["id"], b"late data")
    assert service.start_recording({"mime_type": "video/webm"})["id"] != item["id"]


def test_idle_recording_times_out_after_last_fragment_and_does_not_block_new_recording(factory):
    clock = Clock()
    service, _, _ = factory(clock)
    item = service.start_recording({})
    first = b"\x1a\x45\xdf\xa3partial"
    service.append_recording(item["id"], first)
    clock.advance(119)
    service._sweep()
    assert not service.state()["recordings"][0]["finished"]
    service.append_recording(item["id"], b"still recording")
    clock.advance(119)
    service._sweep()
    assert not service.state()["recordings"][0]["finished"]
    clock.advance(2)
    service._sweep()
    abandoned = service.state()["recordings"][0]
    assert abandoned["finished"] and abandoned["interrupted"]
    assert Path(abandoned["path"]).read_bytes() == first + b"still recording"
    assert service.start_recording({})["id"] != item["id"]


def test_saved_configuration_survives_restart_without_starting_video(factory):
    clock = Clock()
    service, _, _ = factory(clock)
    expected = {"source": "phone", "background": "transparent", "face": "mask",
                "layout": "portrait", "size": 45, "system_audio": False, "microphone": True}
    service.update(expected)
    service.demand(True, "preview")
    service.close()
    restored, portrait, bridge = factory(clock)
    state = restored.state()
    assert all(state["config"][key] == value for key, value in expected.items())
    assert not portrait.enabled and not bridge.enabled
    assert restored.frame_png() is None
