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
    assert not any(path == '/api/input/source' and body is not None for path, body in api.requests)
    assert not any(path == '/api/output/config' and body and body.get('enabled') for path, body in api.requests)


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
