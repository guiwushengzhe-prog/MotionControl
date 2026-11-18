"""从开始页和设备页点到完整二维码；不启动真实采集。"""
import pytest
expect = pytest.importorskip("playwright.sync_api").expect

from test_web_optimization import ORIGIN, desktop
from test_web_ui import browser as mock_browser


@pytest.mark.parametrize("previous_pane", ["voice", "lab", "view"])
def test_phone_shortcut_opens_qr_on_home(desktop, previous_pane):
    page, api = desktop.page, desktop.api
    api.runtime["body_mode"] = "phone"
    api.input["body_mode"] = "phone"
    page.route(ORIGIN + "/api/phone-connect", lambda route: api.fulfill(route, {
        "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="288" height="288"></svg>'}))
    page.evaluate("async()=>{localStorage.setItem('motioncontrol-phone-code-offered','1');await (await import('/js/devices.js')).refreshInput();await (await import('/js/play.js')).refreshKernel()}")
    page.click('nav [data-view="devices"]')
    page.click(f'#settingsNav [data-pane="{previous_pane}"]')
    page.evaluate("document.querySelector('#phoneGroup').style.marginTop='1200px'")
    page.click('nav [data-view="play"]')
    page.click('#cameraStepGo')
    expect(page.locator('#playPanel')).to_be_visible()
    expect(page.locator('#phoneConnectPanel')).to_be_visible()
    page.wait_for_function("phoneConnectImage.complete && phoneConnectImage.naturalWidth > 0")
    assert page.evaluate("document.activeElement.id") == "closePhoneConnectBtn"
    page.keyboard.press('Escape')
    expect(page.locator('#phoneConnectPanel')).to_be_hidden()
    expect(page.locator('#cameraStepGo')).to_be_focused()


def test_add_phone_shows_whole_code_and_selects_camera_in_place(desktop):
    page, api = desktop.page, desktop.api
    page.set_viewport_size({"width": 900, "height": 600})
    page.route(ORIGIN + "/api/phone-connect", lambda route: api.fulfill(route, {
        "svg": '<svg xmlns="http://www.w3.org/2000/svg" width="288" height="288"></svg>'}))
    page.click('nav [data-view="devices"]')
    page.click('#addPeripheralBtn')
    page.select_option('#addPeripheralType', 'phone')
    page.click('#addPeripheralContinue')
    expect(page.get_by_role('dialog', name='连接手机')).to_be_visible()
    page.wait_for_function("phoneConnectImage.complete && phoneConnectImage.naturalWidth > 0")
    assert page.evaluate("""() => {
        const rect=phoneConnectImage.getBoundingClientRect();
        return rect.top>=0 && rect.bottom<=innerHeight && scrollY===0;
    }""")
    assert api.runtime['body_mode'] == 'computer'
    page.click('#phoneUseCameraBtn')
    expect(page.locator('#phoneConnectStatus')).to_have_text('等待手机摄像头连接')
    assert api.runtime['body_mode'] == 'phone'
    assert not api.output['enabled']
    api.input['mobile_pose_connected'] = True
    expect(page.locator('#phoneConnectStatus')).to_have_text('手机摄像头已连接')
    page.click('#closePhoneConnectBtn')
    expect(page.locator('#phoneConnectPanel')).to_be_hidden()
    expect(page.locator('#addPeripheralBtn')).to_be_focused()
    assert desktop.errors == []


def test_computer_shortcut_starts_recognition_on_home_without_enabling_output(desktop):
    page, api = desktop.page, desktop.api
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="voice"]')
    page.click('nav [data-view="play"]')
    page.click('#cameraStepGo')
    expect(page.locator('#playPanel')).to_be_visible()
    expect(page.locator('#sourceStopBtn')).to_be_visible()
    assert api.runtime['camera']['running'] is True
    assert api.output['enabled'] is False
    assert sum(path == '/api/input/source' and body.get('source') == 'computer'
               for path, body in api.requests if body is not None) == 1
    assert page.locator('#viewer + .viewer-foot #sourceStartBtn').count() == 1
