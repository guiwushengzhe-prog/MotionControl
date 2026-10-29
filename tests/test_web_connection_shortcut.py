"""手机入口定位二维码，电脑入口直接开始识别；不启动真实采集。"""
import pytest
expect = pytest.importorskip("playwright.sync_api").expect

from test_web_optimization import ORIGIN, desktop
from test_web_ui import browser as mock_browser


@pytest.mark.parametrize("previous_pane", ["voice", "lab", "view"])
def test_phone_shortcut_reveals_qr_below_fold(desktop, previous_pane):
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
    expect(page.locator('#settingsNav [data-pane="devices"]')).to_have_attribute("aria-current", "true")
    expect(page.locator('#phoneConnectPanel')).to_be_visible()
    page.wait_for_function("phoneConnectImage.getAttribute('src')?.startsWith('data:image/svg+xml')")
    page.wait_for_function("phoneConnectPanel.getBoundingClientRect().top >= 60 && phoneConnectPanel.getBoundingClientRect().top < innerHeight-100")
    assert page.evaluate("document.activeElement.id") == "phoneConnectPanel"
    assert page.evaluate("scrollY") > 1000
    # 再点也保持展开，不会因为复用了按钮而把扫码面板收起。
    page.click('nav [data-view="play"]')
    page.click('#cameraStepGo')
    expect(page.locator('#phoneConnectPanel')).to_be_visible()


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
