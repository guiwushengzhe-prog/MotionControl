"""通过真实网页交互验证选择即识别、暂停、停止及迟到请求；不连接真实摄像头。"""
from playwright.sync_api import expect
from test_web_optimization import desktop
from test_web_ui import browser as mock_browser


def devices_page(desktop):
    page = desktop.page
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="devices"]')
    page.evaluate("""async()=>{
      const d=await import('/js/devices.js');
      d.renderCameraDevices([{index:0},{index:1}],0,{camera_device:'0'});
    }""")
    return page


def test_select_starts_automatically_and_pause_keeps_recognizing(desktop):
    page, api = devices_page(desktop), desktop.api
    page.select_option("#cameraDevice", "1")
    expect(page.locator("#sourceStopBtn")).to_be_visible()
    expect(page.locator("#sourceStartBtn")).to_be_hidden()
    expect(page.locator("#mainActionBtn")).to_have_text("开始控制")
    assert api.runtime["camera"]["running"]
    assert api.output["enabled"] is False
    page.click("#mainActionBtn")
    expect(page.locator("#mainActionBtn")).to_have_text("暂停控制")
    # 控制中可选回另一台相机，完成后继续控制。
    with page.expect_response(lambda r: "/api/camera/config" in r.url and r.request.method == "POST"):
        page.select_option("#cameraDevice", "0")
    page.wait_for_function("!cameraDevice.disabled")
    expect(page.locator("#mainActionBtn")).to_have_text("暂停控制")
    assert api.runtime["camera"]["camera_device"] == "0"
    assert page.locator("#cameraDevice option").count() == 2
    page.click("#mainActionBtn")
    expect(page.locator("#mainActionBtn")).to_have_text("开始控制")
    assert api.runtime["camera"]["running"]
    page.click("#stopBtn")
    expect(page.locator("#sourceStartBtn")).to_be_visible()
    expect(page.locator("#sourceStopBtn")).to_be_hidden()
    assert not api.runtime["camera"]["running"]
    assert api.output["enabled"] is False
    assert api.input["body_enabled"] is False


def test_f9_prevents_resume_when_camera_selection_response_arrives_late(desktop):
    page, api = devices_page(desktop), desktop.api
    page.click("#mainActionBtn")
    expect(page.locator("#mainActionBtn")).to_have_text("暂停控制")
    held = []
    def delayed(route):
        if route.request.method == "POST":
            held.append(route)
        else:
            api.fulfill(route, {"camera_index": 0, "camera_device": "0"})
    page.route("**/api/camera/config", delayed)
    page.select_option("#cameraDevice", "1")
    page.wait_for_function("cameraDevice.disabled")
    assert held
    page.keyboard.press("F9")
    expect(page.locator("#sourceStartBtn")).to_be_hidden()  # 切换请求尚未结束。
    api.fulfill(held[0], {
        "camera_index": 1, "camera_device": "1", "body_mode": "computer",
        "camera": {"running": True, "camera_device": "1"}, "kernel": api.runtime["kernel"],
    })
    page.wait_for_function("!cameraDevice.disabled")
    expect(page.locator("#mainActionBtn")).to_have_text("开始控制")
    assert api.output["enabled"] is False
    assert not api.runtime["camera"]["running"]
    starts = [(p, b) for p, b in api.requests if p == "/api/output/config" and b.get("enabled")]
    assert len(starts) == 1


def test_first_control_click_connects_and_enables_and_source_choice_applies(desktop):
    page, api = desktop.page, desktop.api
    api.voice.update(connected=False, audio_ready=False)
    api.input["voice"] = {"connected": False}
    page.evaluate("""async()=>{await (await import('/js/voice.js')).refreshVoice();
      await (await import('/js/devices.js')).refreshInput()}""")
    page.click("#mainActionBtn")
    expect(page.locator("#mainActionBtn")).to_have_text("暂停控制")
    assert api.runtime["camera"]["running"]
    assert api.output["enabled"]
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="devices"]')
    with page.expect_response(lambda r: "/api/input/source" in r.url and r.request.method == "POST"):
        page.select_option("#poseSource", "phone")
    page.wait_for_function("!poseSource.disabled")
    assert api.runtime["body_mode"] == "phone"
    assert api.input["body_enabled"]
    assert not api.runtime["camera"]["running"]
    assert api.output["enabled"] is False
