import json
import urllib.request

from test_web_ui import _go, browser, server, ui  # noqa: F401


def test_real_connection_code_renders_and_closes_when_new_phone_connects(ui):
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
    assert page.locator("#phoneConnectPanel").is_hidden()
    assert ui.errors == []


def test_first_phone_selection_offers_once_and_manual_close_stays_closed(ui):
    page = ui.page
    _go(page, "devices")
    page.click('#settingsNav [data-pane="devices"]')
    page.select_option("#poseSource", "phone")
    page.wait_for_selector("#phoneConnectPanel:not([hidden])")
    page.click("#closePhoneConnectBtn")
    page.evaluate("async()=>{const d=await import('/js/devices.js');d.renderInputStatus(d.inputStatus)}")
    assert page.locator("#phoneConnectPanel").is_hidden()
    assert ui.errors == []
