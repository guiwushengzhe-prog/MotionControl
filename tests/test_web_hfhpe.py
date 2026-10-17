"""实验转头方式的浏览器交互；接口隔离，不打开窗口或使用摄像头。"""
from __future__ import annotations

import copy

from test_web_optimization import desktop, mock_browser  # noqa: F401


def open_view(page):
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="view"]')
    page.wait_for_function("!viewHorizontalSource.disabled")


def choose_hfhpe(desktop):
    page, api = desktop.page, desktop.api
    open_view(page)
    page.select_option("#viewHorizontalSource", "hfhpe")
    page.wait_for_function("!viewHorizontalSource.disabled && !viewControlStatus.textContent.includes('正在保存')")
    assert page.locator("#viewHorizontalSource").input_value() == "hfhpe"
    assert api.runtime["kernel"]["head"]["horizontal_algorithm"] == "hfhpe"


def test_experimental_turn_is_distinct_and_survives_reload(desktop):
    page, api = desktop.page, desktop.api
    choose_hfhpe(desktop)
    choices = page.locator("#viewHorizontalSource option")
    assert choices.all_text_contents() == ["头部侧倾", "侧倾＋转脸", "左右转头", "左右转头（实验）", "右手握拳", "左手握拳", "关闭"]
    mutations = [(path, body) for path, body in api.requests if path == "/api/view-control"]
    assert mutations[-1] == ("/api/view-control", {"horizontal": "hfhpe", "vertical": "off"})
    assert page.locator("#headCurvePanel").is_hidden()
    assert "彩色画面" in page.locator("#viewHorizontalDesc").text_content()
    page.reload()
    page.wait_for_function("currentGameName.textContent === 'Default game'")
    open_view(page)
    assert page.locator("#viewHorizontalSource").input_value() == "hfhpe"
    assert page.locator("#headCurvePanel").is_hidden()
    # 原左右转头仍映回旧策略，不被实验模式替代。
    page.select_option("#viewHorizontalSource", "head_turn")
    page.wait_for_function("!viewHorizontalSource.disabled && !viewControlStatus.textContent.includes('正在保存')")
    assert page.locator("#viewHorizontalSource").input_value() == "head_turn"
    assert [body for path, body in api.requests if path == "/api/view-control"][-1]["horizontal"] == "head_turn"


def test_saving_experiment_speed_keeps_the_hfhpe_algorithm(desktop):
    page, api = desktop.page, desktop.api
    choose_hfhpe(desktop)
    saved = []

    def save_head(route):
        body = route.request.post_data_json
        saved.append(body)
        api.runtime["kernel"]["head"].update(body)
        api.fulfill(route, api.runtime)

    page.route("**/api/head/config", save_head)
    with page.expect_response(lambda response: response.url.endswith("/api/head/config") and response.request.method == "POST"):
        page.locator("#speedX").press("End")
    page.wait_for_function("!viewControlStatus.textContent.includes('正在保存') && headSaveStatus.textContent.includes('已保存')")
    assert saved[-1]["horizontal_algorithm"] == "hfhpe"
    assert saved[-1]["sensitivity_x"] == 200
    assert page.locator("#viewHorizontalSource").input_value() == "hfhpe"
    assert page.locator("#headCurvePanel").is_hidden()


def test_experiment_only_reports_current_actionable_model_status(desktop):
    page, api = desktop.page, desktop.api
    choose_hfhpe(desktop)
    baseline = page.locator("#viewHorizontalDesc").text_content()
    for state, message in [
        ("loading", "正在加载实验转头模型"),
        ("error", "实验模型损坏，请重新下载"),
        ("unavailable", "手机只发送骨骼点，请在设备中切换电脑摄像头"),
        ("ready", "模型已就绪"),
    ]:
        api.runtime["camera"]["hfhpe"] = {"state": state, "message": message}
        page.evaluate("runtime=>import('/js/play.js').then(m=>m.renderKernelState(runtime,true))", copy.deepcopy(api.runtime))
        description = page.locator("#viewHorizontalDesc").text_content()
        if state == "ready":
            assert description == baseline
        else:
            assert message in description
        assert page.locator("#viewHorizontalSource").input_value() == "hfhpe"
        assert page.locator("#headCurvePanel").is_hidden()
    page.select_option("#viewHorizontalSource", "roll_tilt")
    page.wait_for_function("!viewHorizontalSource.disabled && !viewControlStatus.textContent.includes('正在保存')")
    assert page.locator("#headCurvePanel").is_visible()
    assert "实验" not in page.locator("#viewHorizontalDesc").text_content()


def test_rejected_experiment_selection_restores_the_actual_source(desktop):
    page, api = desktop.page, desktop.api
    open_view(page)
    api.fail_view = True
    page.select_option("#viewHorizontalSource", "hfhpe")
    page.wait_for_function("!viewHorizontalSource.disabled && viewControlStatus.textContent.includes('保存未确认')")
    assert page.locator("#viewHorizontalSource").input_value() == "right"
    assert "实验" not in page.locator("#viewHorizontalDesc").text_content()


def test_experiment_deadzone_uses_its_own_yaw_noise_and_no_tilt_curve(desktop):
    page = desktop.page
    choose_hfhpe(desktop)
    notes = page.evaluate("""async()=>{
      const {head}=await import('/js/state.js');
      const {renderHeadCurve}=await import('/js/head-curve.js');
      head.deadzone=.2;
      renderHeadCurve({hfhpe_calibrated:true,hfhpe_adaptive_deadzone_deg:1.5,
        motion_deadzone_scale:1,noise_tilt_deg:10});
      const first=headThresholdStatus.textContent;
      head.deadzone=.03;renderHeadCurve();
      const second=headThresholdStatus.textContent;
      renderHeadCurve({hfhpe_calibrated:false,hfhpe_adaptive_deadzone_deg:null});
      return {first,second,uncalibrated:headThresholdStatus.textContent};
    }""")
    assert notes["first"] == "设定死区 3.6°，自适应防抖死区 1.5°。实际死区 3.6°。"
    assert notes["second"] == "设定死区 0.5°，自适应防抖死区 1.5°。实际死区 1.5°。"
    assert notes["uncalibrated"] == "设定死区 0.5°；重新校准后显示自适应防抖和实际死区。"
    assert page.locator("#headCurvePanel").is_hidden()
