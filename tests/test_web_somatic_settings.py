"""体感设置合并后的真实浏览器操作；仅使用临时用户目录，不打开摄像头。"""
from test_web_ui import browser, server, ui, _go, sync_api


def test_model_names_match_choices_actual_status_and_diagnostics(ui):
    page = ui.page
    _go(page, "devices")
    page.click('#settingsNav [data-pane="view"]')
    select = page.locator("#poseModelSelect")
    sync_api.expect(select).to_be_enabled()
    assert select.locator("option").all_text_contents() == ["自动（推荐）", "标准", "高精度"]
    page.route("**/api/recognition/models", lambda route: route.fulfill(json={
        "model_preference": "auto", "actual_model": "full", "running": True,
        "auto_model_reason": "标准模型更适合当前摄像头帧率", "models": [
            {"id": "mp-full", "available": True}, {"id": "mp-heavy", "available": True}]}))
    page.evaluate("import('/js/recognition-models.js').then(m=>m.refreshRecognitionModels())")
    sync_api.expect(select).to_have_value("auto")
    sync_api.expect(page.locator("#poseModelStatus")).to_have_text("当前：标准 · 标准模型更适合当前摄像头帧率")
    for preference, actual, running, expected in [
        ("auto", "heavy", True, "当前：高精度"),
        ("full", "full", True, "当前：标准"),
        ("heavy", "full", False, "下次识别使用：高精度"),
        ("auto", "full", False, "下次识别时自动选择"),
    ]:
        page.evaluate("data=>import('/js/recognition-models.js').then(m=>m.renderRecognitionPerformance(data))",
                      {"model_preference": preference, "actual_model": actual, "running": running})
        sync_api.expect(page.locator("#poseModelStatus")).to_have_text(expected)
    page.route("**/api/performance", lambda route: route.fulfill(json={
        "actual_model": "heavy", "inference_stages_ms": {"pose": 12}}))
    page.evaluate("import('/js/devices.js').then(m=>m.refreshPerformance())")
    sync_api.expect(page.locator("#perfDetails")).to_contain_text("当前身体模型：高精度")
    assert ui.errors == []


def test_somatic_view_and_body_model_save_on_one_page_and_reload(ui):
    page = ui.page
    _go(page, "devices")
    nav = page.locator('#settingsNav [data-pane="view"]')
    sync_api.expect(nav).to_have_text("体感")
    assert page.locator('#settingsNav [data-pane="body"]').count() == 0
    nav.click()
    sync_api.expect(page.locator("#headSettings")).to_be_visible()
    sync_api.expect(page.locator("#bodySettings")).to_be_visible()
    source = page.locator("#viewHorizontalSource")
    sync_api.expect(source).to_be_enabled()
    with page.expect_response(lambda response: response.url.endswith("/api/view-control")
                              and response.request.method == "POST"):
        source.select_option("head_responsive")
    models = page.request.get(ui.base + "/api/recognition/models").json()
    assert any(model["id"] == "mp-full" and model["available"] for model in models["models"])
    model = page.locator("#poseModelSelect")
    with page.expect_response(lambda response: response.url.endswith("/api/recognition/models")
                              and response.request.method == "POST"):
        model.select_option("full")
    sync_api.expect(model).to_be_enabled()
    assert page.request.get(ui.base + "/api/recognition/models").json()["model_preference"] == "full"
    assert page.request.get(ui.base + "/api/kernel/status").json()["kernel"]["head"]["horizontal_algorithm"] == "head_responsive"
    page.reload()
    page.wait_for_function("currentGameName.textContent !== '正在读取…'")
    _go(page, "devices")
    nav.click()
    sync_api.expect(source).to_have_value("head_responsive")
    sync_api.expect(model).to_have_value("full")
    assert page.locator(".settings-pane:not([hidden])").get_attribute("id") == "somaticSettings"
    assert ui.errors == []


def test_legacy_body_entry_opens_somatic_and_focuses_body_group(ui):
    page = ui.page
    page.set_viewport_size({"width": 390, "height": 844})
    _go(page, "devices")
    page.click('#settingsNav [data-pane="voice"]')
    page.evaluate("import('/js/shell.js').then(shell=>shell.showSettingsPane('body'))")
    sync_api.expect(page.locator('#settingsNav [data-pane="view"]')).to_have_attribute("aria-current", "true")
    sync_api.expect(page.locator("#bodySettings")).to_be_focused()
    assert page.locator("#bodySettings").evaluate("el=>{const box=el.getBoundingClientRect();return box.top>=0&&box.top<innerHeight}")
    assert ui.errors == []
