"""连续多选触发录制、过期读取和失败回退；不实际录制。"""

import copy

from test_web_optimization import ORIGIN, desktop, mock_browser  # noqa: F401


def prepare(desktop):
    page, api = desktop.page, desktop.api
    state = {"state": "waiting", "config": {"enabled": True, "triggers": [], "pre_s": 1., "post_s": 1.}, "saved_clips": 0}
    choices = [{"key": "motion.march", "name": "原地踏步"}, {"key": "motion.jump", "name": "跳跃"}, {"key": "pose.hands_cross", "name": "双手交叉"}]
    held = []

    def route(request):
        if request.request.method == "POST":
            payload = request.request.post_data_json
            held.append((request, copy.deepcopy(payload)))
        else:
            api.fulfill(request, {"recording": copy.deepcopy(state), "choices": choices})

    page.route(ORIGIN + "/api/pose/trigger-recording", route)
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')
    page.locator("#triggerRecordSummary").click()
    page.wait_for_function("triggerRecordChoices.children.length === 3")
    return state, held, choices


def test_quick_multiselect_is_queued_and_persists_reload(desktop):
    page, api = desktop.page, desktop.api
    state, held, choices = prepare(desktop)
    page.locator('[data-trigger="motion.march"].trigger-record-option').click()
    page.locator('[data-trigger="motion.jump"].trigger-record-option').click()
    page.locator('[data-trigger="pose.hands_cross"].trigger-record-option').click()
    assert page.locator("#triggerRecordChoices .selected").count() == 3
    assert page.locator("#triggerRecordPicker").evaluate("el=>el.open")
    assert len(held) == 1
    state["config"].update(held[0][1])
    api.fulfill(held[0][0], {"recording": copy.deepcopy(state)})
    page.wait_for_timeout(80)
    assert len(held) == 2
    assert held[1][1]["triggers"] == ["motion.march", "motion.jump", "pose.hands_cross"]
    state["config"].update(held[1][1])
    api.fulfill(held[1][0], {"recording": copy.deepcopy(state)})
    page.wait_for_timeout(50)
    page.reload()
    page.wait_for_function("currentGameName.textContent === 'Default game'")
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')
    page.locator("#triggerRecordSummary").click()
    page.wait_for_function("triggerRecordChoices.querySelectorAll('.selected').length === 3")
    assert state["config"]["enabled"]


def test_failed_latest_selection_returns_to_saved_choices(desktop):
    page, api = desktop.page, desktop.api
    state, held, choices = prepare(desktop)
    page.locator('[data-trigger="motion.jump"].trigger-record-option').click()
    assert page.locator("#triggerRecordChoices .selected").count() == 1
    api.fulfill(held[0][0], {"error": "保存失败"}, status=400)
    page.wait_for_function("triggerRecordChoices.querySelectorAll('.selected').length === 0")
    assert state["config"]["triggers"] == []


def test_late_read_during_save_cannot_unselect_committed_choices(desktop):
    page, api = desktop.page, desktop.api
    state, held, choices = prepare(desktop)
    stale = {}
    page.locator('[data-trigger="motion.jump"].trigger-record-option').click()

    def read(route):
        if route.request.method == "GET":
            stale["route"] = route
        else:
            held.append((route, route.request.post_data_json))

    page.route(ORIGIN + "/api/pose/trigger-recording", read)
    page.evaluate("()=>{void import('/js/diagnostics.js').then(m=>m.refreshTriggerRecord())}")
    page.wait_for_timeout(50)
    assert "route" in stale
    state["config"].update(held[0][1])
    api.fulfill(held[0][0], {"recording": copy.deepcopy(state)})
    page.wait_for_timeout(50)
    api.fulfill(stale["route"], {"recording": {**state, "config": {**state["config"], "triggers": []}}, "choices": choices})
    page.wait_for_timeout(50)
    assert page.locator("#triggerRecordChoices .selected").count() == 1
    assert "跳跃" in page.locator("#triggerRecordSummary").inner_text()
