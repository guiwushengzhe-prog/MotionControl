"""校准主按钮保留原操作，重设正前方收进同处菜单。"""
from urllib.parse import urlparse

from test_web_optimization import ORIGIN, desktop, mock_browser  # noqa: F401


def control_posts(api):
    return [(path, body) for path, body in api.requests if body is not None
            and (path.startswith("/api/head/calibration/") or path == "/api/output/config")]


def prepare(desktop, *, calibrated=True):
    api, page = desktop.api, desktop.page
    api.runtime["camera"].update(running=True, frames=100, last_frame_age_ms=0)
    api.runtime["kernel"]["pose"] = {"nose": {"x": .5, "y": .3, "score": .95}}
    api.hand["config"].update(enabled=False, horizontal_hand="off", vertical_hand="off")
    api.runtime["kernel"]["head"].update(
        enabled=True, calibrated=calibrated, horizontal_calibrated=calibrated,
        calibrating=False, noise_tilt_deg=.1,
    )

    def calibration(route):
        path = urlparse(route.request.url).path
        api.requests.append((path, route.request.post_data_json))
        if path.endswith("/start"):
            api.runtime["kernel"]["head"].update(calibrating=True, calibrates_body=True, notice="正在校准")
        elif path.endswith("/cancel"):
            api.runtime["kernel"]["head"].update(calibrating=False, notice="校准已取消")
        api.fulfill(route, api.runtime)

    page.route(ORIGIN + "/api/head/calibration/**", calibration)
    page.evaluate("async()=>{const view=await import('/js/view-control.js');await view.reloadViewControlState()}")
    page.locator("#calBtn").wait_for(state="visible")


def test_calibration_menu_location_dismissal_and_narrow_view(desktop):
    prepare(desktop)
    page = desktop.page
    assert page.locator("#centerBtn").count() == 1
    assert page.locator(".viewer-tools #centerBtn").count() == 0
    assert page.locator('#calMenu #centerBtn[role="menuitem"]').count() == 1
    assert page.locator('[data-step="cal"] #calBtn').count() == 1
    button, menu = page.locator("#calMenuBtn"), page.locator("#calMenu")
    assert button.get_attribute("aria-controls") == "calMenu"
    assert menu.is_hidden()
    button.click()
    assert menu.is_visible() and button.get_attribute("aria-expanded") == "true"
    page.keyboard.press("Escape")
    assert menu.is_hidden() and button.get_attribute("aria-expanded") == "false"
    button.click()
    page.locator("#readyTitle").click()
    assert menu.is_hidden()
    button.click()
    button.click()
    assert menu.is_hidden()
    page.set_viewport_size({"width": 390, "height": 844})
    button.click()
    width, viewport = page.evaluate("[document.documentElement.scrollWidth,innerWidth]")
    assert width <= viewport + 1
    rect = menu.bounding_box()
    assert rect and rect["x"] >= -1 and rect["x"] + rect["width"] <= viewport + 1


def test_recenter_menu_sends_only_recenter_and_closes(desktop):
    prepare(desktop)
    page, api = desktop.page, desktop.api
    page.locator("#calMenuBtn").click()
    api.requests.clear()
    with page.expect_response(lambda response: response.url.endswith("/api/head/calibration/center")):
        page.locator("#centerBtn").click()
    posts = control_posts(api)
    assert posts == [("/api/head/calibration/center", {})]
    assert page.locator("#calMenu").is_hidden()


def test_primary_calibration_pauses_output_before_start_and_disables_menu(desktop):
    prepare(desktop)
    page, api = desktop.page, desktop.api
    assert page.locator("#calBtn").inner_text() == "重新校准"
    api.requests.clear()
    with page.expect_response(lambda response: response.url.endswith("/api/head/calibration/start")):
        page.locator("#calBtn").click()
    page.wait_for_function("calBtn.textContent==='取消校准'")
    assert "身体" in page.locator("#calibrationPrompt").inner_text()
    posts = control_posts(api)
    assert posts == [("/api/output/config", {"enabled": False}), ("/api/head/calibration/start", {})]
    assert page.locator("#calMenuBtn").is_disabled()
    assert page.locator("#centerBtn").is_disabled()
    assert page.locator("#calMenu").is_hidden()


def test_uncalibrated_state_requires_primary_calibration(desktop):
    prepare(desktop, calibrated=False)
    page, api = desktop.page, desktop.api
    assert page.locator("#calBtn").inner_text() == "站好并校准"
    assert page.locator("#calBtn").is_enabled()
    assert page.locator("#calMenuBtn").is_disabled()
    assert page.locator("#centerBtn").is_disabled()
    assert page.locator("#calMenu").is_hidden()
    api.runtime["kernel"]["head"].update(calibrated=True, horizontal_calibrated=True)
    page.evaluate("async()=>{const view=await import('/js/view-control.js');await view.reloadViewControlState()}")
    assert page.locator("#calBtn").inner_text() == "重新校准"
    assert page.locator("#calMenuBtn").is_enabled()
    assert page.locator("#centerBtn").is_enabled()


def test_calibration_actions_remain_accessible_while_controlling(desktop):
    prepare(desktop)
    page, api = desktop.page, desktop.api
    api.output["enabled"] = True
    page.evaluate("import('/js/devices.js').then(m=>m.refreshOutput())")
    page.locator("#runCalibrationActions #calMenuBtn").wait_for(state="visible")
    assert page.locator("#calBtn").count() == 1
    page.locator("#calMenuBtn").click()
    api.requests.clear()
    with page.expect_response(lambda response: response.url.endswith("/api/head/calibration/center")):
        page.locator("#centerBtn").click()
    assert api.output["enabled"]
    assert control_posts(api) == [("/api/head/calibration/center", {})]
    api.output["enabled"] = False
    page.evaluate("import('/js/devices.js').then(m=>m.refreshOutput())")
    page.locator('[data-step="cal"] #calBtn').wait_for(state="visible")
