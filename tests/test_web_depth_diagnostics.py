"""真实界面模块配替代接口；不打开正式页面，不截图或采集设备。"""
import copy

from playwright.sync_api import expect
from test_web_optimization import desktop, mock_browser  # noqa: F401


def depth_state():
    return {"config": {"enabled": False, "model": "dav2_metric_s", "input_size": 392, "max_fps": 10},
            "revision": 100, "state": "off", "models": {name: {"available": True} for name in ("dav2_metric_s", "vda_metric_s", "zipdepth")},
            "results": {}, "preview_ready": False}


def lab(page):
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')


def test_three_candidates_and_controls_only_save_explicit_choice(desktop):
    page, api = desktop.page, desktop.api
    state, changes = depth_state(), []
    def route(request):
        body = request.request.post_data_json
        if body is not None:
            changes.append(body)
            state['config'].update(body)
            state['revision'] += 1
        api.fulfill(request, state)
    page.route('**/api/depth-comparison', route)
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))', state)
    lab(page)
    assert page.locator('#depthComparisonModel option').count() == 3
    page.select_option('#depthComparisonModel', 'zipdepth')
    expect(page.locator('#depthComparisonCalibrate')).to_be_visible()
    assert changes == [{'model': 'zipdepth'}]
    page.check('#depthComparisonEnabled')
    expect(page.locator('#depthComparisonEnabled')).to_be_checked()
    assert changes[-1] == {'enabled': True}
    with page.expect_response(lambda r: r.url.endswith('/api/depth-comparison') and r.request.method == 'POST'):
        page.select_option('#depthComparisonScope', 'person')
    expect(page.locator('#depthComparisonReframe')).to_be_visible()
    assert changes[-1] == {'input_scope': 'person'}
    with page.expect_response(lambda r: r.url.endswith('/api/depth-comparison') and r.request.method == 'POST'):
        page.uncheck('#depthComparisonAutoScale')
    assert changes[-1] == {'auto_scale': False}
    assert not any(path == '/api/input/source' and body is not None for path, body in api.requests)
    assert not any(path == '/api/output/config' and body and body.get('enabled') for path, body in api.requests)


def test_monocular_runs_one_shared_model_and_keeps_original_comparison_preference(desktop):
    page, api, state, changes = desktop.page, desktop.api, depth_state(), []
    state.update(active=True, control_requested=True, active_model='vda_metric_s', state='running')
    state['config'].update(model='zipdepth', monocular_model='vda_metric_s')
    state['results']['vda_metric_s'] = {'inference_avg_ms': 33, 'comparison': {}}
    def route(request):
        body = request.request.post_data_json
        changes.append(body)
        state['config'].update(body)
        state['active_model'] = state['config']['monocular_model']
        state['revision'] += 1
        api.fulfill(request, state)
    page.route('**/api/depth-comparison', route)
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))', state)
    lab(page)
    assert page.locator('#depthComparisonModel').input_value() == 'vda_metric_s'
    assert page.locator('#depthComparisonScope').input_value() == 'person'
    assert page.locator('#depthComparisonScope').is_disabled()
    expect(page.locator('#depthComparisonRows')).to_contain_text('VDA-S 米制流式 · 当前')
    expect(page.locator('#depthComparisonNote')).to_contain_text('不依赖Kinect距离校准')
    assert page.locator('#depthComparisonReframe').is_enabled()
    with page.expect_response(lambda r: r.url.endswith('/api/depth-comparison') and r.request.method == 'POST'):
        page.select_option('#depthComparisonModel', 'dav2_metric_s')
    assert changes == [{'monocular_model': 'dav2_metric_s'}]
    assert state['config']['model'] == 'zipdepth'


def test_real_results_keep_units_dimensions_and_missing_reference_explicit(desktop):
    page = desktop.page
    state = depth_state()
    state['results']['dav2_metric_s'] = {'input_shape': [392, 700], 'fps': 9.8, 'inference_avg_ms': 16,
        'gpu_reserved_mb': 700, 'comparison': {'reference_points': 0, 'validation_samples': 0}}
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))', state)
    lab(page)
    expect(page.locator('#depthComparisonRows')).to_contain_text('700×392')
    expect(page.locator('#depthComparisonRows')).to_contain_text('16.0毫秒')
    expect(page.locator('#depthComparisonRows')).to_contain_text('700.0MB')
    page.locator('#depthComparisonPoints').evaluate('el=>el.closest("details").open=true')
    expect(page.locator('#depthComparisonPoints')).to_contain_text('没有可靠')
    assert page.locator('#depthComparisonView').is_hidden()


def test_relative_model_shows_uncalibrated_values_instead_of_blank_metres(desktop):
    page, state = desktop.page, depth_state()
    state['config'].update(model='zipdepth', enabled=True)
    state['state'] = 'running'
    state['results']['zipdepth'] = {'inference_avg_ms': 23.2, 'comparison': {
        'metric_ready': False, 'reference_points': 11, 'validation_samples': 0,
        'points': [{'name': 'chest', 'z': .8, 'raw': .08682, 'model_m': None, 'error_cm': None}]}}
    page.evaluate('''async s=>{const m=await import('/js/depth-comparison.js');m.renderDepthComparison(s,
      {surface_m:.8,surface_min_m:.7,surface_max_m:.9,reference_points:6});}''', state)
    lab(page)
    expect(page.locator('#depthComparisonRows')).to_contain_text('待尺度校准')
    expect(page.locator('#depthComparisonRows')).to_contain_text('0 / 11')
    expect(page.locator('#depthComparisonReference')).to_contain_text('0.800米')
    page.locator('#depthComparisonPoints').evaluate('el=>el.closest("details").open=true')
    expect(page.locator('#depthComparisonPoints')).to_contain_text('模型相对值 0.08682')
    expect(page.locator('#depthComparisonPoints')).to_contain_text('尚无米数')
    state['results']['zipdepth']['comparison'].update(metric_ready=True, mae_cm=12.3, p95_cm=18.5,
        stationary_jitter_cm=1.1, validation_samples=9, bias_cm=12.3)
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))', state)
    expect(page.locator('#depthComparisonRows')).to_contain_text('12.3厘米')
    expect(page.locator('#depthComparisonCalibration')).to_contain_text('偏远 12.3厘米')


def test_failed_save_restores_checkbox_and_retains_visible_reason(desktop):
    page, api = desktop.page, desktop.api
    state = depth_state()
    def route(request):
        api.fulfill(request, {'error': '模拟资源缺失'} if request.request.method == 'POST' else state,
                    400 if request.request.method == 'POST' else 200)
    page.route('**/api/depth-comparison', route)
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))', state)
    lab(page)
    page.check('#depthComparisonEnabled')
    expect(page.locator('#depthComparisonEnabled')).not_to_be_checked()
    expect(page.locator('#depthComparisonStatus')).to_contain_text('模拟资源缺失')


def test_old_snapshot_does_not_restore_old_model_choice(desktop):
    state = depth_state()
    newer = copy.deepcopy(state)
    newer['revision'] += 1
    newer['config']['model'] = 'zipdepth'
    desktop.page.evaluate('async s=>{const m=await import("/js/depth-comparison.js");m.renderDepthComparison(s[0]);m.renderDepthComparison(s[1])}', [newer, state])
    assert desktop.page.locator('#depthComparisonModel').input_value() == 'zipdepth'


def test_preview_first_request_starts_demand_and_hidden_panel_stops_requests(desktop):
    page, api, state, seen = desktop.page, desktop.api, depth_state(), []
    state.update(active=True, state='running', sequence=1)
    state['config']['enabled'] = True
    state['results']['dav2_metric_s'] = {'age_ms': 0, 'comparison': {}}
    def preview(route):
        seen.append(route.request.url)
        route.fulfill(status=204)
    page.route('**/api/depth-comparison/preview?*', preview)
    lab(page)
    with page.expect_response(lambda r: '/api/depth-comparison/preview?' in r.url):
        page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))', state)
    assert len(seen) == 1
    assert page.locator('#depthComparisonView').is_hidden()
    page.click('#settingsNav [data-pane="view"]')
    state['sequence'] = 2
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))', state)
    assert len(seen) == 1


def test_runtime_details_show_head_hands_actions_output_and_latency(desktop):
    page = desktop.page
    page.evaluate('''async()=>{const {renderRuntimeDiagnostics:r}=await import('/js/runtime-diagnostics.js');r(
      {running:true,actual_capture_fps:30,captured_frames:200,processed_frames:180,inference_p95_ms:21,
       hfhpe:{state:'ready',model:'vitt128',inference_ms:8.1},hand_model_state:'native'},
      {head:{horizontal_algorithm:'roll_tilt',enabled:true,horizontal_calibrated:true,raw_tilt_deg:5.2,
             horizontal_block_message:'处于稳定区',output_x:.2,hand_mouse:{axes:{horizontal:{hand:'right',engaged:true,output:.3}}}},
       motions:['lean_forward'],zone_trigger_mode:'simple',recent_triggers:[{label:'旧记录'},{label:'左手区'}],
       zones:{leftHand:{rect:{},mapped:true,pressed:true},leftHandUpper:{rect:{},mapped:true,pressed:true},hidden:{mapped:false}},
       sensitivity_experiments:{body:{side_source:'model',fore_aft_source:'depth'}}},
      {enabled:false,mode:'mouse',server:{last_x:.2,last_y:0,keyboard_holds:['W']}},{body_mode:'computer'}); }''')
    lab(page)
    expect(page.locator('#diagHead')).to_contain_text('5.20°')
    expect(page.locator('#diagHead')).to_contain_text('处于稳定区')
    expect(page.locator('#diagHands')).to_contain_text('握拳已接管')
    expect(page.locator('#diagActions')).to_contain_text('身体前倾')
    expect(page.locator('#diagActions')).to_contain_text('已映射区域：1 个 · 当前点亮 1 个')
    expect(page.locator('#diagActions')).to_contain_text('触发方式 灵敏')
    expect(page.locator('#diagOutput')).to_contain_text('暂停')
    expect(page.locator('#diagOutput')).to_contain_text('左手区')


def test_voice_diagnostic_translates_rejection_and_exposes_audio_queue(desktop):
    desktop.api.voice.update(recognizer_mode='vosk_constrained_grammar', source_kind='computer',
        last_final='药', last_executed=False, last_error='output disabled', audio_queue_depth=3,
        audio_dropped_frames=4, audio_stream_resets=2)
    desktop.page.evaluate('()=>import("/js/voice.js").then(m=>m.refreshVoice())')
    lab(desktop.page)
    expect(desktop.page.locator('#voiceDiagnostic')).to_contain_text('控制输出已暂停')
    expect(desktop.page.locator('#voiceDiagnostic')).to_contain_text('待处理音频：3 块')
    expect(desktop.page.locator('#voiceDiagnostic')).not_to_contain_text('output disabled')
    assert '模型：' not in desktop.page.locator('#voiceDiagnostic').inner_text()


def test_raw_and_frozen_calibrated_errors_are_separate_and_failure_is_explicit(desktop):
    page, state = desktop.page, depth_state()
    state['results']['dav2_metric_s'] = {'comparison': {'raw_mae_cm': 120, 'calibrated_mae_cm': 9.5,
        'calibrated_p95_cm': 25.7, 'calibrated_validation_samples': 300, 'reference_points': 20,
        'scale_ready': True, 'calibration_attempted': True}}
    state['results']['zipdepth'] = {'comparison': {'calibration_attempted': True, 'metric_ready': False,
        'scale_ready': False, 'reference_points': 23}}
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))', state)
    lab(page)
    row = page.locator('#depthComparisonRows tr').nth(0)
    expect(row.locator('td').nth(4)).to_have_text('120.0厘米')
    expect(row.locator('td').nth(5)).to_have_text('9.5厘米')
    expect(row.locator('td').nth(6)).to_have_text('25.7厘米')
    expect(row.locator('td').nth(8)).to_have_text('300 / 20')
    expect(page.locator('#depthComparisonRows tr').nth(2)).to_contain_text('尺度校准未通过')
