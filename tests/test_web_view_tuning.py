"""免鼠标试调只用隔离页面和模拟接口，不输出系统控制。"""
from test_web_optimization import desktop, mock_browser  # noqa: F401


def open_tuning(desktop):
    page = desktop.page
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="view"]')
    page.click('#openViewTuning')
    page.wait_for_function('viewTuning.open')


def test_composed_speed_uses_real_backend_limits_and_separate_axis_gain(desktop):
    result = desktop.page.evaluate("""async()=>{
      const {composedSpeed:s}=await import('/js/view-speed.js');
      const mouse={mode:'mouse',mouse_speed_x:1500,mouse_speed_y:1125};
      const pad={mode:'gamepad',gamepad_gain:2.5};
      return [s(1,mouse),s(.4,mouse),s(.4,mouse,'y'),s(1,pad),s(.4,pad),s(.2,pad),s(2,pad),s(-.2,pad),s(5,mouse)];
    }""")
    assert result == [1500, 600, 450, 1, 1, .5, 1, -.5, 3000]


def test_output_is_in_somatic_with_composed_speed_and_large_cards(desktop):
    page = desktop.page
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="view"]')
    assert page.locator('#outputSettings').evaluate("el=>el.closest('.settings-pane').dataset.pane") == 'view'
    desktop.api.output.update(mode='gamepad', gamepad_gain=2.5)
    page.evaluate("()=>import('/js/devices.js').then(m=>m.refreshOutput())")
    assert '推杆' in page.locator('#composedSpeedSummary').inner_text()
    assert '游戏自身' in page.locator('#composedSpeedSummary').inner_text()
    open_tuning(desktop)
    assert page.locator('#tuningCards .tuning-card').count() == 4
    assert page.locator('#tuningCards').evaluate('el=>el.scrollWidth <= el.clientWidth + 4')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.locator('#viewTuning').evaluate('el=>el.getBoundingClientRect().width <= innerWidth')


def test_navigation_steps_three_rates_returns_center_and_rejects_stale(desktop):
    result = desktop.page.evaluate("""async()=>{
      const {TuningNavigation:N}=await import('/js/tuning-navigation.js');const n=new N();
      const r=[];r.push(n.step(0,0));r.push(n.step(0,500));r.push(n.step(9,600));
      r.push(n.step(9,800));r.push(n.step(9,1300));r.push(n.step(16,1650));r.push(n.step(27,1810));
      r.push(n.step(7.9,1815));r.push(n.step(8,1820));r.push(n.step(0,1900));r.push(n.step(0,2799));r.push(n.step(0,2800));
      r.push(n.step(27,3000));r.push(n.step(0,3500,{valid:false}));return r;
    }""")
    assert result[0]['waiting']
    assert result[2] == {'delta': 1, 'tier': 1}
    assert 'delta' not in result[3]
    assert result[4]['delta'] == 1
    assert result[5] == {'delta': 1, 'tier': 2}
    assert result[6] == {'delta': 1, 'tier': 3}
    assert 'delta' not in result[8], '8度边界抖动不能绕过重复间隔'
    assert not result[10].get('confirm')
    assert result[11]['confirm']
    assert result[12]['waiting']
    assert result[13]['waiting']


def test_open_only_pauses_output_and_close_does_not_apply_a_scheme(desktop):
    desktop.api.output['enabled'] = True
    desktop.api.requests.clear()
    open_tuning(desktop)
    desktop.page.click('#closeViewTuning')
    mutations = [(path, body) for path, body in desktop.api.requests if body is not None]
    assert ('/api/output/config', {'enabled': False}) in mutations
    assert not any(path in ('/api/view-control', '/api/head/config', '/api/hand-mouse/config', '/api/input/source') for path, body in mutations)
    assert desktop.api.output['enabled'] is False


def test_keyboard_steps_draft_and_applies_only_speed_on_confirm(desktop):
    page, api = desktop.page, desktop.api
    calls = []

    def save_head(route):
        calls.append(route.request.post_data_json)
        api.runtime['kernel']['head'].update(route.request.post_data_json)
        api.fulfill(route, api.runtime)

    page.route('**/api/head/config', save_head)
    open_tuning(desktop)
    page.locator('#viewTuning').press('ArrowRight')
    page.locator('#viewTuning').press('Enter')
    page.wait_for_function("!tuningAdjust.hidden && !viewTuning.classList.contains('saving')")
    assert page.locator('#tuningMethodName').inner_text() == '头部侧倾'
    before = float(page.locator('#tuningSpeed').input_value())
    page.locator('#viewTuning').press('ArrowRight')
    assert float(page.locator('#tuningSpeed').input_value()) == before + 2
    assert not calls
    page.click('#tuningTry')
    page.wait_for_function('!tuningTrial.hidden')
    assert calls == [{'sensitivity_x': before + 2}]
    assert not any(body and body.get('enabled') is True for path, body in api.requests if path == '/api/output/config')
    page.click('#tuningEndTrial')
    page.locator('#viewTuning').press('ArrowRight')
    page.locator('#viewTuning').press('Enter')
    assert not page.locator('#viewTuning').evaluate('el=>el.open')


def test_failed_selection_keeps_choice_screen_and_explains_failure(desktop):
    desktop.api.fail_view = True
    open_tuning(desktop)
    desktop.page.click('#tuningCards [data-choice="roll_tilt"]')
    desktop.page.wait_for_function("!viewTuning.classList.contains('saving')")
    assert not desktop.page.locator('#tuningCards').is_hidden()
    assert desktop.page.locator('#tuningAdjust').is_hidden()
    assert desktop.page.locator('#tuningError').inner_text()


def test_head_gesture_moves_one_card_then_neutral_confirms_without_mouse(desktop):
    import math

    page, api = desktop.page, desktop.api
    api.runtime['camera'].update(running=True, frames=10, last_frame_age_ms=0)
    api.runtime['kernel'].update(pose_age_ms=0, pose={
        'nose': {'x': .5, 'y': .4, 'score': .99},
        'left_eye': {'x': .45, 'y': .3, 'score': .99},
        'right_eye': {'x': .55, 'y': .3, 'score': .99}})
    open_tuning(desktop)
    page.wait_for_timeout(1350)
    api.runtime['kernel']['pose']['right_eye']['y'] += math.tan(math.radians(10)) * 64 / 480
    page.evaluate("()=>import('/js/play.js').then(m=>m.refreshKernel())")
    page.wait_for_function("document.querySelector('#tuningCards [aria-pressed=true]').dataset.choice==='roll_tilt'")
    api.runtime['kernel']['pose']['right_eye']['y'] = .3
    page.evaluate("()=>import('/js/play.js').then(m=>m.refreshKernel())")
    page.wait_for_function("!tuningAdjust.hidden && !viewTuning.classList.contains('saving')")
    assert page.locator('#tuningMethodName').inner_text() == '头部侧倾'
    assert any(path == '/api/view-control' and body['horizontal'] == 'roll_tilt' for path, body in api.requests)


def test_hand_speed_saves_the_hand_only_and_shows_its_axis_preview(desktop):
    page, api = desktop.page, desktop.api
    payloads = []

    def save_hand(route):
        payload = route.request.post_data_json
        if payload is not None:
            payloads.append(payload)
            api.hand['config'].update(payload)
        api.fulfill(route, {'hand_mouse': api.hand})

    page.route('**/api/hand-mouse/config', save_hand)
    open_tuning(desktop)
    page.click('#tuningCards [data-choice="right"]')
    page.wait_for_function("!tuningAdjust.hidden && !viewTuning.classList.contains('saving')")
    before = float(page.locator('#tuningSpeed').input_value())
    page.locator('#viewTuning').press('ArrowRight')
    page.click('#tuningTry')
    page.wait_for_function('!tuningTrial.hidden')
    assert payloads == [{'sensitivity': before + 5}]
    assert not any(path == '/api/head/config' and body is not None for path, body in api.requests)


def test_legacy_ratio_turn_navigates_with_menu_equivalent_angle(desktop):
    page, api = desktop.page, desktop.api
    api.runtime['camera'].update(running=True, frames=10, last_frame_age_ms=0)
    api.runtime['kernel'].update(pose_age_ms=0, pose={'nose': {'x': .5, 'y': .4, 'score': .99}})
    api.runtime['kernel']['head'].update(estimate_valid=True, raw_yaw=0, raw_yaw_units='ratio', hfhpe_estimate_valid=False)
    open_tuning(desktop)
    page.click('#tuningNavTurn')
    page.wait_for_timeout(1350)
    # 控制器的0.34比例参考对应18度；不能把0.19比例当成0.19度而永远不移动。
    api.runtime['kernel']['head']['raw_yaw'] = 10 * .34 / 18
    page.evaluate("()=>import('/js/play.js').then(m=>m.refreshKernel())")
    page.wait_for_function("document.querySelector('#tuningCards [aria-pressed=true]').dataset.choice==='roll_tilt'")
    assert '等效' in page.locator('#tuningDirection').inner_text()
    api.runtime['kernel']['head']['raw_yaw'] = 0
    page.evaluate("()=>import('/js/play.js').then(m=>m.refreshKernel())")
    page.wait_for_function("!tuningAdjust.hidden && !viewTuning.classList.contains('saving')")
    assert page.locator('#tuningMethodName').inner_text() == '头部侧倾'
