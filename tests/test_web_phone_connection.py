import json
import urllib.request

from test_web_ui import _go, browser, server, ui  # noqa: F401


def test_real_connection_code_is_visible_and_confirms_new_phone(ui):
    page = ui.page
    _go(page, "devices")
    page.click('#settingsNav [data-pane="devices"]')
    assert page.locator("#phoneConnectPanel").is_hidden()
    page.click("#phoneConnectBtn")
    page.wait_for_function("phoneConnectImage.complete && phoneConnectImage.naturalWidth > 0")
    assert page.locator("#phoneConnectPanel").is_visible()
    with urllib.request.urlopen(ui.base + "/api/phone-connect") as response:
        code = json.load(response)
    assert len(code["payload"]["instance"]) == 12
    with urllib.request.urlopen(ui.base + "/api/input/status?brief=1") as response:
        status = json.load(response)
    assert all(address["port"] == status["server_port"] for address in code["payload"]["candidates"])
    with urllib.request.urlopen(f'http://127.0.0.1:{status["server_port"]}/api/models') as response:
        identity = json.load(response)
    assert identity["instance"] == code["payload"]["instance"]
    page.evaluate("""async()=>{const d=await import('/js/devices.js');d.renderInputStatus({...d.inputStatus,
      mobile_pose_connected:true,mobile_pose_sources:[{device_id:'new-phone',connected:true}]})}""")
    page.wait_for_function("phoneConnectStatus.textContent === '手机摄像头已连接'")
    assert page.locator("#phoneConnectPanel").is_visible()
    assert page.locator("#closePhoneConnectBtn").inner_text() == "完成"
    page.click("#closePhoneConnectBtn")
    assert page.locator("#phoneConnectPanel").is_hidden()
    assert ui.errors == []


def test_first_phone_selection_offers_once_and_manual_close_stays_closed(ui):
    page = ui.page
    _go(page, "devices")
    page.click('#settingsNav [data-pane="devices"]')
    page.locator('#cameraSettings > summary').click()
    page.select_option("#poseSource", "phone")
    page.wait_for_selector("#phoneConnectPanel[open]")
    page.click("#closePhoneConnectBtn")
    page.evaluate("async()=>{const d=await import('/js/devices.js');d.renderInputStatus(d.inputStatus)}")
    assert page.locator("#phoneConnectPanel").is_hidden()
    assert ui.errors == []


def test_an_unpaired_phone_reports_status_without_interrupting_settings(ui):
    """未配对的手机只更新设备状态，用户点扫码才显示连接码。"""
    page = ui.page
    _go(page, "devices")
    page.click('#settingsNav [data-pane="devices"]')
    assert page.locator("#phoneConnectPanel").is_hidden()
    render = "async(flag)=>{const d=await import('/js/devices.js');d.renderInputStatus({...d.inputStatus,unpaired_phone:flag})}"
    page.evaluate(render, True)
    assert page.locator('[data-peripheral-status="phone"]').inner_text() == "手机待配对"
    assert page.locator("#phoneConnectPanel").is_hidden()
    page.click("#phoneConnectBtn")
    page.wait_for_function("phoneConnectStatus.textContent.includes('还没配对')")
    page.click("#closePhoneConnectBtn")
    page.evaluate(render, True)
    assert page.locator("#phoneConnectPanel").is_hidden()
    assert ui.errors == []
