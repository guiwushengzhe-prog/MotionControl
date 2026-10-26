"""模型位置诊断沿用现有页面；编辑草稿、取消和失败不误报成功。"""
from test_web_optimization import desktop, mock_browser


def state(root="I:\\原依赖", *, missing=True, revision=100):
    return {"revision": revision, "state": "off", "config": {"max_fps": 15}, "resource_root": root,
        "models": {"mocapnet_personal": {"available": not missing,
            "issues": [{"name": "个人倾角读出", "path": root + "\\readout.json", "reason": "文件不可用"}] if missing else []}}}


def open_resources(page, value):
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')
    page.evaluate("async value=>(await import('/js/depth-comparison.js')).renderDepthComparison(value)", value)


def test_resource_issues_are_specific_collapsed_and_do_not_overwrite_a_new_folder(desktop):
    page = desktop.page
    value = state()
    open_resources(page, value)
    assert not page.locator('#modelResourceDetails').evaluate('el=>el.open')
    assert '个人倾角读出' in page.locator('#monocularLeanModelNote').text_content()
    page.click('#modelResourceDetails summary')
    assert 'readout.json' in page.locator('#modelResourceIssues').inner_text()
    page.fill('#modelResourceFolder', 'D:\\新依赖')
    page.evaluate("async value=>(await import('/js/depth-comparison.js')).renderDepthComparison(value)", value)
    assert page.locator('#modelResourceFolder').input_value() == 'D:\\新依赖'


def test_cancelled_system_picker_never_updates_the_registry(desktop):
    page, api = desktop.page, desktop.api
    open_resources(page, state())
    requests = []
    def picker(route):
        requests.append(route.request.url)
        api.fulfill(route, {"folder": ""})
    page.route('**/api/model-resources/choose', picker)
    page.click('#modelResourceDetails summary')
    page.click('#modelResourceChoose')
    page.wait_for_function("modelResourceStatus.textContent.includes('已取消')")
    assert len(requests) == 1
    assert page.locator('#modelResourceFolder').input_value() == 'I:\\原依赖'


def test_position_update_uses_chosen_folder_once_and_reports_verified_result(desktop):
    page, api = desktop.page, desktop.api
    open_resources(page, state())
    received = []
    def update(route):
        received.append(route.request.post_data_json)
        api.fulfill(route, {**state('D:\\新依赖', missing=False, revision=101), "message": "试加载通过，位置已保存"})
    page.route('**/api/model-resources/relocate', update)
    page.click('#modelResourceDetails summary')
    page.fill('#modelResourceFolder', 'D:\\新依赖')
    page.click('#modelResourceCheck')
    page.wait_for_function("modelResourceStatus.textContent.includes('位置已保存')")
    assert received == [{"folder": 'D:\\新依赖'}]
    assert page.locator('#modelResourceIssues li').count() == 0


def test_mismatched_model_stays_editable_and_does_not_claim_saved(desktop):
    page, api = desktop.page, desktop.api
    open_resources(page, state())
    page.route('**/api/model-resources/relocate', lambda route: api.fulfill(route, {"error": "模型权重与原登记不相符，原位置未改动"}, 400))
    page.click('#modelResourceDetails summary')
    page.fill('#modelResourceFolder', 'D:\\错误副本')
    page.click('#modelResourceCheck')
    page.wait_for_function("modelResourceStatus.textContent.includes('不相符')")
    assert not page.locator('#modelResourceCheck').is_disabled()
    assert page.locator('#modelResourceFolder').input_value() == 'D:\\错误副本'
    assert '位置已保存' not in page.locator('#modelResourceStatus').inner_text()
