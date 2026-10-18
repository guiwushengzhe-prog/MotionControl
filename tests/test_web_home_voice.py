"""设备就绪时保持简洁；麦克风故障单独定位，不覆盖摄像头状态；不采集截图。"""
import pytest

from test_web_ui import browser, server, ui, sync_api


def _camera_ready(ui, **changes):
    endpoint = ui.base + "/api/kernel/status"
    runtime = ui.page.request.get(endpoint).json()
    runtime["body_mode"] = "computer"
    runtime.setdefault("camera", {}).update(running=True, picture_black=False,
                                             frames=1, last_frame_age_ms=0)
    runtime["camera"].update(changes)
    ui.page.route(endpoint, lambda route: route.fulfill(json=runtime))
    ui.page.evaluate("async()=>{await (await import('/js/play.js')).refreshKernel()}")


def _voice_status(ui, **changes):
    endpoint = ui.base + "/api/voice/status"
    status = ui.page.request.get(endpoint).json()
    status.update(connected=True, source_kind="computer", available=True,
                  model_ready=True, audio_ready=True, audio_alive=True,
                  last_error="")
    status.update(changes)
    ui.page.route(endpoint, lambda route: route.fulfill(json=status))
    ui.page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoice()}")


@pytest.mark.parametrize("width", [1280, 400])
def test_device_ready_is_quiet_and_voice_commands_stay_by_camera(ui, width):
    page = ui.page
    page.set_viewport_size({"width": width, "height": 820})
    _camera_ready(ui)
    _voice_status(ui)
    step = page.locator('.checklist [data-step="camera"]')
    sync_api.expect(step.locator("b")).to_have_text("设备")
    sync_api.expect(step).to_have_class("step done")
    sync_api.expect(page.locator("#cameraStepSub")).to_have_text("已就绪")
    sync_api.expect(page.locator("#cameraStepSub")).to_have_attribute("title", "摄像头和麦克风已就绪")
    sync_api.expect(page.locator("#cameraStepGo")).to_be_hidden()
    assert page.locator(".control-voice, .home-voice").count() == 0
    sync_api.expect(page.locator(".viewer-foot .viewer-tools #voiceCommandsBtn")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.click("#voiceCommandsBtn")
    sync_api.expect(page.locator("#voiceCommandsMask")).to_be_visible()
    assert ui.errors == []


def test_microphone_error_is_lightweight_and_jumps_to_audio_device(ui):
    page = ui.page
    _camera_ready(ui)
    _voice_status(ui)
    was_disabled = page.locator("#mainActionBtn").is_disabled()
    page.unroute(ui.base + "/api/voice/status")
    _voice_status(ui, connected=False, last_error="麦克风已被其他程序占用")
    sync_api.expect(page.locator("#cameraStepSub")).to_have_text("摄像头已就绪")
    sync_api.expect(page.locator("#voiceStepSub")).to_contain_text("麦克风已被其他程序占用")
    sync_api.expect(page.locator('.checklist [data-step="voice"]')).to_have_class("step warn")
    assert page.locator("#mainActionBtn").is_disabled() == was_disabled, "麦克风异常不额外阻止控制"
    page.click("#voiceStepGo")
    sync_api.expect(page.locator('.settings-pane[data-pane="devices"]')).to_be_visible()
    assert page.evaluate("['audioSource','audioDevice'].includes(document.activeElement.id)")
    assert ui.errors == []


def test_voice_read_failure_clears_device_ready_and_recovers(ui):
    page = ui.page
    endpoint = ui.base + "/api/voice/status"
    _camera_ready(ui)
    _voice_status(ui)
    sync_api.expect(page.locator("#cameraStepSub")).to_have_text("已就绪")
    page.unroute(endpoint)
    page.route(endpoint, lambda route: route.fulfill(status=503, json={"error": "读取失败"}))
    page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoice()}")
    sync_api.expect(page.locator("#cameraStepSub")).to_have_text("摄像头已就绪")
    sync_api.expect(page.locator("#voiceStepSub")).to_contain_text("语音状态无法确认")
    sync_api.expect(page.locator('.checklist [data-step="voice"]')).to_have_class("step warn")
    page.unroute(endpoint)
    _voice_status(ui)
    sync_api.expect(page.locator("#cameraStepSub")).to_have_text("已就绪")
    sync_api.expect(page.locator('.checklist [data-step="voice"]')).to_be_hidden()
    assert ui.errors == []
