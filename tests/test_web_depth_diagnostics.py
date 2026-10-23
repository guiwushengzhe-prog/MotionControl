"""轻量前后倾入口与通用运行诊断；旧三模型对比界面已退役。"""
from playwright.sync_api import expect
from test_web_optimization import desktop, mock_browser  # noqa: F401


def lab(page):
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')


def research_state(**changes):
    return {"config":{"enabled":False,"monocular_model":"mocapnet_personal","max_fps":15},
            "revision":100,"state":"off","control_requested":False,"active_model":"mocapnet_personal",
            "models":{"mocapnet_personal":{"available":True}},"results":{},**changes}


def test_old_three_model_comparison_is_removed_without_starting_capture(desktop):
    page=desktop.page
    lab(page)
    assert page.locator('#depthComparisonPanel').count()==0
    assert page.locator('#depthComparisonPreview').count()==0
    assert page.locator('#monocularLeanModel option').count()==1
    expect(page.locator('#monocularLeanModel')).to_contain_text('MocapNET')


def test_research_rate_only_saves_explicit_change_and_keeps_comparison_off(desktop):
    page,api=desktop.page,desktop.api
    state,changes=research_state(),[]
    def route(request):
        body=request.request.post_data_json
        if body is not None:
            changes.append(body)
            state['config'].update(body)
            state['revision']+=1
        api.fulfill(request,state)
    page.route('**/api/depth-comparison',route)
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))',state)
    lab(page)
    assert changes==[]
    with page.expect_response('**/api/depth-comparison'):
        page.select_option('#monocularLeanFps','30')
    expect(page.locator('#monocularLeanFps')).to_have_value('30')
    assert changes==[{"max_fps":30,"enabled":False,"monocular_model":"mocapnet_personal"}]


def test_research_runtime_shows_cost_without_depth_preview_and_preserves_newer_revision(desktop):
    page=desktop.page
    state=research_state(state='running',control_requested=True,results={'mocapnet_personal':
        {'fps':15,'inference_avg_ms':2.5,'inference_p95_ms':2.9,'latency_ms':18.4}})
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))',state)
    lab(page)
    expect(page.locator('#monocularLeanTiming')).to_contain_text('2.5毫秒')
    expect(page.locator('#monocularLeanTiming')).to_contain_text('仅处理器计算')
    expect(page.locator('#monocularLeanRuntime')).to_contain_text('前后倾识别中')
    older=research_state(revision=99,models={'mocapnet_personal':{'available':False}})
    page.evaluate('(s)=>import("/js/depth-comparison.js").then(m=>m.renderDepthComparison(s))',older)
    assert page.locator('#monocularLeanModel').is_enabled()


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
