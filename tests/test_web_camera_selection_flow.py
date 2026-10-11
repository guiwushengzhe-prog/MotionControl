"""通过真实网页交互验证选择即识别、暂停、停止及迟到请求；不连接真实摄像头。"""
import pytest
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
    page.click('nav [data-view="play"]')
    expect(page.locator("#sourceStopBtn")).to_be_visible()
    expect(page.locator("#sourceStartBtn")).to_be_hidden()
    expect(page.locator("#mainActionLabel")).to_have_text("开始控制")
    assert api.runtime["camera"]["running"]
    assert api.output["enabled"] is False
    page.click("#mainActionBtn")
    expect(page.locator("#mainActionLabel")).to_have_text("暂停控制")
    # 控制中可选回另一台相机，完成后继续控制。
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="devices"]')
    with page.expect_response(lambda r: "/api/camera/config" in r.url and r.request.method == "POST"):
        page.select_option("#cameraDevice", "0")
    page.wait_for_function("!cameraDevice.disabled")
    expect(page.locator("#mainActionLabel")).to_have_text("暂停控制")
    assert api.runtime["camera"]["camera_device"] == "0"
    assert page.locator("#cameraDevice option").count() == 2
    page.click("#mainActionBtn")
    expect(page.locator("#mainActionLabel")).to_have_text("开始控制")
    assert api.runtime["camera"]["running"]
    page.click('nav [data-view="play"]')
    page.click("#sourceStopBtn")
    expect(page.locator("#sourceStartBtn")).to_be_visible()
    expect(page.locator("#sourceStopBtn")).to_be_hidden()
    assert not api.runtime["camera"]["running"]
    assert api.output["enabled"] is False
    assert api.input["body_enabled"] is False


def test_f9_prevents_resume_when_camera_selection_response_arrives_late(desktop):
    page, api = devices_page(desktop), desktop.api
    page.click("#mainActionBtn")
    expect(page.locator("#mainActionLabel")).to_have_text("暂停控制")
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
    expect(page.locator("#mainActionLabel")).to_have_text("开始控制")
    assert api.output["enabled"] is False
    assert api.runtime["camera"]["running"]
    starts = [(p, b) for p, b in api.requests if p == "/api/output/config" and b.get("enabled")]
    assert len(starts) == 1


@pytest.mark.parametrize('ready_input', ['voice', 'handheld', 'none'])
def test_first_control_click_connects_and_enables_and_source_choice_applies(desktop, ready_input):
    page, api = desktop.page, desktop.api
    api.voice.update(connected=ready_input=='voice', audio_ready=ready_input=='voice')
    api.input["voice"] = {"connected": ready_input=='voice'}
    api.input["handheld_connected"] = ready_input=='handheld'
    page.evaluate("""async()=>{await (await import('/js/voice.js')).refreshVoice();
      await (await import('/js/devices.js')).refreshInput()}""")
    page.click("#mainActionBtn")
    expect(page.locator("#mainActionLabel")).to_have_text("暂停控制")
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


def test_camera_start_failure_does_not_enable_output(desktop):
    page, api = desktop.page, desktop.api
    page.route('**/api/input/source', lambda route: api.fulfill(route, {'error': '选中的摄像头无法启动'}, status=400))
    page.click('#mainActionBtn')
    expect(page.locator('#notice')).to_contain_text('选中的摄像头无法启动')
    expect(page.locator('#mainActionLabel')).to_have_text('开始控制')
    assert not api.output['enabled']
    assert not any(path=='/api/output/config' and body.get('enabled') for path, body in api.requests)


def test_stop_while_first_camera_start_is_pending_never_enables_output(desktop):
    page, api = desktop.page, desktop.api
    held=[]
    page.route('**/api/input/source', lambda route: held.append(route))
    page.click('#mainActionBtn')
    page.wait_for_function("document.getElementById('serviceStatus').textContent.includes('正在准备识别')")
    assert held
    page.keyboard.press('F9')
    api.fulfill(held[0], {'body_mode':'computer','camera':{'running':True},'kernel':api.runtime['kernel']})
    page.wait_for_function("!document.getElementById('mainActionBtn').disabled")
    expect(page.locator('#mainActionLabel')).to_have_text('开始控制')
    assert not api.output['enabled']
    assert not any(path=='/api/output/config' and body.get('enabled') for path, body in api.requests)
