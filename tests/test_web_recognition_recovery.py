"""首页可真实重试采集；停帧时首页与悬浮窗均清掉旧区域，不截图。"""
import pytest
from playwright.sync_api import expect

from test_web_optimization import desktop
from test_web_ui import browser as mock_browser


def _refresh(page):
    page.evaluate("async()=>{await (await import('/js/play.js')).refreshKernel()}")


def test_settings_home_link_reveals_real_start_and_check_does_not_return_to_settings(desktop):
    page, api = desktop.page, desktop.api
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="devices"]')
    page.click('#recognitionHomeBtn')
    expect(page.locator('#playPanel')).to_be_visible()
    assert page.evaluate('document.activeElement.id') == 'sourceStartBtn'
    page.click('#cameraStepGo')
    expect(page.locator('#playPanel')).to_be_visible()
    assert page.evaluate('document.activeElement.id') == 'sourceStartBtn'
    page.click('#sourceStartBtn')
    expect(page.locator('#sourceStopBtn')).to_be_visible()
    assert any(path == '/api/input/source' and body['enabled'] for path, body in api.requests)
    assert api.output['enabled'] is False


def test_stalled_camera_retry_releases_then_reopens_on_home(desktop):
    page, api = desktop.page, desktop.api
    api.runtime['camera'] = {'running': True, 'frames': 20,
                             'last_frame_age_ms': 9000, 'last_error': '摄像头已停止供帧'}
    _refresh(page)
    expect(page.locator('#sourceStartBtn')).to_have_text('重试识别')
    expect(page.locator('#sourceStartBtn')).to_be_visible()
    expect(page.locator('#hint')).to_contain_text('摄像头已停止供帧')
    api.requests.clear()
    with page.expect_request('**/api/input/source'):
        page.click('#sourceStartBtn')
    page.wait_for_function("!document.querySelector('#sourceStartBtn').disabled")
    mutations = [(path, body) for path, body in api.requests if body is not None]
    paths = [path for path, _ in mutations]
    assert paths.index('/api/input/stop') < paths.index('/api/input/source')
    source = next(body for path, body in mutations if path == '/api/input/source')
    assert source['source'] == 'computer' and source['enabled'] is True
    assert api.output['enabled'] is False
    expect(page.locator('#playPanel')).to_be_visible()


def test_start_control_does_not_enable_output_until_a_real_camera_frame(desktop):
    page, api = desktop.page, desktop.api
    api.runtime['camera'] = {'running': False, 'frames': 0, 'last_frame_age_ms': None}
    _refresh(page)
    with page.expect_request('**/api/input/source'):
        page.click('#mainActionBtn')
    expect(page.locator('#notice')).to_contain_text('正在等待摄像头新画面')
    assert api.runtime['camera']['running'] is True
    assert api.output['enabled'] is False
    assert not any(path == '/api/output/config' and body.get('enabled')
                   for path, body in api.requests if body)


def _overlay(page, runtime):
    return page.evaluate("""async runtime=>{
      const p=await import('/js/play.js');
      const c=document.createElement('canvas'),ctx=c.getContext('2d'),marks={zones:[],labels:[],points:[]};
      ctx.roundRect=(...args)=>marks.zones.push(args);
      ctx.fillText=(text)=>marks.labels.push(text);
      ctx.arc=(...args)=>marks.points.push(args);
      Object.assign(p.overlay,{win:{closed:false},canvas:c,ctx});
      try{p.renderKernelState(runtime);return marks}
      finally{Object.assign(p.overlay,{win:null,canvas:null,ctx:null})}
    }""", runtime)


@pytest.mark.parametrize('state', ['stopped', 'stalled', 'black', 'no_person'])
def test_stale_zone_does_not_cover_missing_camera_or_person_message(desktop, state):
    page, api = desktop.page, desktop.api
    api.runtime['camera'] = {'running': state != 'stopped', 'frames': 20,
                             'last_frame_age_ms': 6000 if state == 'stalled' else 20,
                             'picture_black': state == 'black'}
    api.runtime['kernel'].update(
        pose=None if state == 'no_person' else {'nose': {'x': .5, 'y': .3, 'score': 1}},
        pose_age_ms=10,
        zones={'user_deadbeef': {'custom': True, 'name': '自定义区域3', 'pressed': True,
                                'rect': {'x1': .3, 'y1': .2, 'x2': .6, 'y2': .5}}})
    marks = _overlay(page, api.runtime)
    expect(page.locator('#hint')).to_be_visible()
    assert page.locator('#viewer .zone:visible').count() == 0
    expect(page.locator('#cameraPreview')).to_be_hidden()
    assert marks['zones'] == []
    assert marks['points'] == []
    assert not any('未映射' in text for text in marks['labels'])


def test_live_zone_label_and_visibility_match_home_and_overlay(desktop):
    page, api = desktop.page, desktop.api
    api.runtime['camera'] = {'running': True, 'frames': 20, 'last_frame_age_ms': 10}
    rect = {'x1': .3, 'y1': .2, 'x2': .6, 'y2': .5}
    api.runtime['kernel'].update(
        pose={'nose': {'x': .5, 'y': .3, 'score': 1}}, pose_age_ms=10,
        effective_bindings={'zone.user_deadbeef': {'action': {'type': 'keyboard', 'target': 'F'}}},
        zones={'user_deadbeef': {'custom': True, 'name': '自定义区域3', 'mode': 'following',
                                'rect': rect}, 'lookGate': {'rect': rect}})
    marks = _overlay(page, api.runtime)
    zone = page.locator('#viewer .zone[data-zone="user_deadbeef"]')
    expect(zone).to_be_visible()
    expect(zone.locator('strong')).to_have_text('F')
    expect(zone.locator('small')).to_have_text('自定义区域3')
    expect(zone).to_have_class('zone custom-zone following')
    expect(page.locator('#hint')).to_be_hidden()
    expect(page.locator('#viewer .zone[data-zone="lookGate"]')).to_be_hidden()
    assert len(marks['zones']) == 1
    assert 'F' in marks['labels'] and '自定义区域3' in marks['labels']
