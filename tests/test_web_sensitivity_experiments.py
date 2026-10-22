"""灵敏度实验只用隔离页面与接口，不开启设备或真实输出。"""
from __future__ import annotations

import copy

import pytest

from test_web_optimization import desktop, mock_browser  # noqa: F401


def seed(desktop, *, hold_ready=True, step_ready=True):
    api, page = desktop.api, desktop.page
    api.hand["config"]["enabled"] = False
    page.evaluate("()=>import('/js/view-control.js').then(m=>m.refreshHandMouse())")
    api.runtime["config_revision"] = 10
    api.runtime["kernel"]["head"].update(enabled=hold_ready, horizontal_algorithm="hfhpe_simple" if hold_ready else "roll_tilt", hfhpe_hold=False)
    api.runtime["kernel"]["march_algorithm"] = "responsive" if step_ready else "legacy"
    api.runtime["kernel"]["sensitivity_experiments"] = {
        "config": {"head_hold": False, "first_step": False, "lean_strafe": False,
                   "lean_back": False, "body_source": "model", "compare": False},
        "body": {"source": "model", "active": {}, "sources": {
            "model": {"valid": True, "reason": "", "calibrated": True,
                      "side_valid": True, "side_calibrated": True, "back_valid": True, "back_calibrated": True,
                      "calibrating": False, "side_deg": -8.2, "back_deg": 3.4, "active": {"lean_left": True}},
            "depth": {"valid": False, "reason": "深度骨骼未连接", "calibrated": False,
                      "calibrating": False, "side_deg": None, "back_deg": None, "active": {}}}},
        "first_step_source": "model", "first_step_ready": True, "first_step_message": "首步即可响应"}
    page.evaluate("runtime=>import('/js/play.js').then(m=>m.renderKernelState(runtime,true))", copy.deepcopy(api.runtime))
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')
    return api.runtime["kernel"]["sensitivity_experiments"]


def install_save(desktop, callback=None):
    saved = []
    api, page = desktop.api, desktop.page

    def handler(route):
        body = route.request.post_data_json
        saved.append(body)
        if callback:
            callback(route, body)
            return
        experiment = api.runtime["kernel"]["sensitivity_experiments"]
        experiment["config"].update(body)
        if "body_source" in body:
            experiment["body"]["source"] = body["body_source"]
        api.runtime["kernel"]["head"]["hfhpe_hold"] = experiment["config"]["head_hold"]
        api.runtime["config_revision"] += 1
        api.fulfill(route, api.runtime)

    page.route("**/api/sensitivity-experiments", handler)
    return saved


def wait_saved(page):
    page.wait_for_function("!experimentLeanStrafe.disabled && !experimentSaveStatus.textContent.includes('正在保存')")


def test_default_off_and_independent_payloads_do_not_start_devices(desktop):
    page, api = desktop.page, desktop.api
    seed(desktop)
    saved = install_save(desktop)
    assert page.locator('#sensitivityExperiments').is_visible()
    for selector in ("experimentHeadHold", "experimentFirstStep", "experimentLeanStrafe", "experimentLeanBack", "experimentLeanForward", "experimentDepthLearning", "experimentCompare"):
        assert not page.locator("#" + selector).is_checked()
    for selector, key in [("experimentHeadHold", "head_hold"), ("experimentFirstStep", "first_step"),
                          ("experimentLeanStrafe", "lean_strafe"), ("experimentLeanBack", "lean_back"),
                          ("experimentLeanForward", "lean_forward"), ("experimentDepthLearning", "learn_from_depth")]:
        page.check("#" + selector)
        wait_saved(page)
        assert saved[-1] == {key: True}
    assert not any(path in ("/api/input/source", "/api/output/config", "/api/view-control", "/api/game-profiles/select") and body
                   for path, body in api.requests)
    assert not page.locator("#experimentComparison").is_visible()


def test_unavailable_sources_disable_only_their_own_switch(desktop):
    page = desktop.page
    seed(desktop, hold_ready=False, step_ready=False)
    assert page.locator("#experimentHeadHold").is_disabled()
    assert page.locator("#experimentFirstStep").is_disabled()
    assert "模型简化方案" in page.locator("#experimentHeadHoldNote").text_content()
    assert "灵敏踏步" in page.locator("#experimentFirstStepNote").text_content()
    assert page.locator("#experimentLeanStrafe").is_enabled()
    assert page.locator("#experimentLeanBack").is_enabled()


def test_compare_shows_selected_source_and_real_failure_without_dual_drive(desktop):
    page = desktop.page
    seed(desktop)
    saved = install_save(desktop)
    page.check("#experimentLeanStrafe")
    wait_saved(page)
    page.check("#experimentCompare")
    wait_saved(page)
    assert saved[-1] == {"compare": True}
    assert "模型骨骼 · 侧倾控制" in page.locator("#experimentModelReadout").text_content()
    assert "-8.2°" in page.locator("#experimentModelReadout").text_content()
    assert "触发：左移" in page.locator("#experimentModelReadout").text_content()
    assert "深度骨骼 · 仅对照" in page.locator("#experimentDepthReadout").text_content()
    page.select_option("#experimentBodySource", "depth")
    wait_saved(page)
    assert saved[-1] == {"body_source": "depth", "side_source": "model", "fore_aft_source": "model"}
    assert "侧倾 模型骨骼；前后倾 模型骨骼" in page.locator("#experimentBodyStatus").text_content()
    page.select_option("#experimentSideSource", "depth")
    wait_saved(page)
    assert saved[-1] == {"side_source": "depth"}
    assert "侧倾 深度骨骼；前后倾 模型骨骼" in page.locator("#experimentBodyStatus").text_content()
    assert "深度骨骼未连接" in page.locator("#experimentBodyStatus").text_content()
    assert "模型骨骼 · 仅对照" in page.locator("#experimentModelReadout").text_content()
    assert "深度骨骼 · 侧倾控制" in page.locator("#experimentDepthReadout").text_content()


def test_independent_side_and_fore_aft_sources_keep_first_step_and_report_each_control_axis(desktop):
    page = desktop.page
    seed(desktop)
    saved = install_save(desktop)
    page.check("#experimentLeanStrafe")
    wait_saved(page)
    page.check("#experimentLeanForward")
    wait_saved(page)
    page.select_option("#experimentForeAftSource", "depth")
    wait_saved(page)
    assert saved[-1] == {"fore_aft_source": "depth"}
    assert page.locator("#experimentBodySource").input_value() == "model"
    assert page.locator("#experimentSideSource").input_value() == "model"
    assert "侧倾 模型骨骼；前后倾 深度骨骼" in page.locator("#experimentBodyStatus").text_content()
    page.check("#experimentCompare")
    wait_saved(page)
    assert "模型骨骼 · 侧倾控制" in page.locator("#experimentModelReadout").text_content()
    assert "深度骨骼 · 前后倾控制" in page.locator("#experimentDepthReadout").text_content()


def test_monocular_panel_keeps_axis_choices_and_shows_calibration_hold_and_loss(desktop):
    page, api = desktop.page, desktop.api
    experiment = seed(desktop)
    saved = install_save(desktop)
    assert page.locator("#monocularLeanPanel").is_hidden()
    page.select_option("#experimentForeAftSource", "monocular")
    wait_saved(page)
    assert saved[-1] == {"fore_aft_source": "monocular"}
    assert page.locator("#experimentSideSource").input_value() == "model"
    assert page.locator("#monocularLeanPanel").is_visible()
    assert not page.locator("#experimentMonocularLearning").is_checked()
    experiment["monocular"] = {"valid": False, "calibrating": True, "remaining_s": 2.1}
    def refresh():
        api.runtime["config_revision"] += 1
        page.evaluate("runtime=>import('/js/play.js').then(m=>m.renderKernelState(runtime,true))", copy.deepcopy(api.runtime))
    refresh()
    assert "2.1 秒" in page.locator("#monocularLeanState").text_content()
    experiment["monocular"].update(valid=True, calibrating=False, state="forward", angle_deg=-12., sampled_regions=30,
                                   reason="几何估计", learning={"samples": 64, "reason": "等待后续独立验证"})
    refresh()
    assert "前倾" in page.locator("#monocularLeanState").text_content()
    assert "30 个胸腹区域" in page.locator("#monocularLeanDetail").text_content()
    assert float(page.locator("#monocularLeanStrength").get_attribute("value")) == .6
    assert "64 对" in page.locator("#monocularLearningNote").text_content()
    experiment["monocular"].update(valid=False, reason="单目深度结果已过时，等待新画面")
    refresh()
    assert "过时" in page.locator("#monocularLeanState").text_content()
    assert float(page.locator("#monocularLeanStrength").get_attribute("value")) == 0
    assert not any(path == "/api/input/source" and body is not None for path, body in api.requests)
    assert not any(path == "/api/output/config" and body and body.get("enabled") for path, body in api.requests)


def test_learning_shows_waiting_validation_and_missing_depth_without_claiming_unverified_success(desktop):
    page, api = desktop.page, desktop.api
    experiment = seed(desktop)
    saved = install_save(desktop)
    page.check("#experimentDepthLearning")
    wait_saved(page)
    assert saved[-1] == {"learn_from_depth": True}
    assert "等待可靠深度肩髋" in page.locator("#experimentDepthLearningNote").text_content()
    experiment["body"]["sources"]["depth"].update(back_valid=True, back_calibrated=True, back_calibrating=False)
    experiment["body"]["learning"] = {"active": False, "reason": "候选等待后续独立留验", "accepted_samples": 64}
    api.runtime["config_revision"] += 1
    page.evaluate("runtime=>import('/js/play.js').then(m=>m.renderKernelState(runtime,true))", copy.deepcopy(api.runtime))
    assert "独立留验" in page.locator("#experimentDepthLearningNote").text_content()
    assert "已启用个人校正" not in page.locator("#experimentDepthLearningNote").text_content()
    experiment["body"]["learning"].update(active=True, accepted_samples=100)
    experiment["body"]["sources"]["depth"]["back_valid"] = False
    api.runtime["config_revision"] += 1
    page.evaluate("runtime=>import('/js/play.js').then(m=>m.renderKernelState(runtime,true))", copy.deepcopy(api.runtime))
    note = page.locator("#experimentDepthLearningNote").text_content()
    assert "已启用个人校正" in note and "暂无可靠深度" in note and "100 对" in note


def test_rejected_save_restores_checked_and_busy_prevents_duplicate(desktop):
    page, api = desktop.page, desktop.api
    seed(desktop)
    held = []
    saved = install_save(desktop, lambda route, body: held.append(route))
    page.check("#experimentLeanBack")
    page.wait_for_function("experimentLeanBack.disabled")
    page.evaluate("experimentLeanBack.dispatchEvent(new Event('change',{bubbles:true}))")
    assert len(saved) == 1
    api.fulfill(held[0], {"error": "此设置暂不可用"}, 400)
    wait_saved(page)
    assert not page.locator("#experimentLeanBack").is_checked()
    assert "已读取当前设置" in page.locator("#experimentSaveStatus").text_content()


def test_hold_updates_source_label_description_and_old_state_cannot_overwrite(desktop):
    page, api = desktop.page, desktop.api
    seed(desktop)
    stale = copy.deepcopy(api.runtime)
    install_save(desktop)
    page.check("#experimentHeadHold")
    wait_saved(page)
    assert page.locator('#viewHorizontalSource option[value="hfhpe_simple"]').text_content() == "左右转头（模型·持续转向）"
    page.evaluate("runtime=>import('/js/play.js').then(m=>m.renderKernelState(runtime,true))", stale)
    assert page.locator("#experimentHeadHold").is_checked()
    page.click('#settingsNav [data-pane="view"]')
    assert "偏头时持续转向" in page.locator("#viewHorizontalDesc").text_content()
    page.click('#settingsNav [data-pane="lab"]')
    page.uncheck("#experimentHeadHold")
    wait_saved(page)
    assert page.locator('#viewHorizontalSource option[value="hfhpe_simple"]').text_content() == "左右转头（模型·简化回正）"


def test_neutral_posts_empty_body_and_shows_service_countdown(desktop):
    page, api = desktop.page, desktop.api
    experiment = seed(desktop)
    requests = []

    def reset(route):
        requests.append(route.request.post_data_json)
        experiment["body"]["sources"]["model"].update(calibrating=True, remaining_s=1.7)
        api.runtime["config_revision"] += 1
        api.fulfill(route, api.runtime)

    page.route("**/api/sensitivity-experiments/neutral", reset)
    page.click("#experimentNeutralBtn")
    page.wait_for_function("!experimentNeutralBtn.disabled")
    assert requests == [{}]
    assert "正在记录站姿，剩余 1.7 秒" in page.locator("#experimentBodyStatus").text_content()
