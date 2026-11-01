"""教学沿当前设置分类与折叠卡片定位，不启动设备或录屏。"""
import pytest

expect = pytest.importorskip("playwright.sync_api").expect
from test_web_optimization import desktop, mock_browser  # noqa: F401


def open_lesson(page, lesson):
    page.evaluate("async lesson=>{const {tutorial}=await import('/js/shell.js');tutorial.openLesson(lesson)}", lesson)


def test_camera_teaching_opens_folded_settings_in_device_category(desktop):
    page, api = desktop.page, desktop.api
    api.runtime['camera'].update(running=True, frames=0, model_path='synthetic.task')
    page.wait_for_timeout(900)
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="fitness"]')
    open_lesson(page, 'fit')
    expect(page.locator('#tourSay')).to_contain_text('等画面出来')
    expect(page.locator('#cameraDeviceRow')).to_be_visible()
    assert page.locator('#cameraSettings').evaluate('node=>node.open')
    expect(page.locator('#settingsNav [data-pane="devices"]')).to_have_attribute('aria-current', 'true')
    assert not any(path == '/api/output/config' and body.get('enabled') is True for path, body in api.requests)


def test_phone_teaching_uses_scan_button_then_visible_connection_code(desktop):
    page, api = desktop.page, desktop.api
    page.route('**/api/phone-connect', lambda route: api.fulfill(route, {'svg': '<svg xmlns="http://www.w3.org/2000/svg"/>'}))
    page.add_init_script("localStorage.setItem('motioncontrol-phone-code-offered','1')")
    page.evaluate("localStorage.setItem('motioncontrol-phone-code-offered','1')")
    api.runtime['body_mode'] = 'phone'
    api.input['body_source'] = 'phone'
    page.wait_for_timeout(900)
    page.click('nav [data-view="devices"]')
    page.select_option('#poseSource', 'phone', force=True)
    open_lesson(page, 'fit')
    expect(page.locator('#tourSay')).to_contain_text('扫码连接')
    page.click('#phoneConnectBtn')
    expect(page.locator('#tourSay')).to_contain_text('用手机扫码')
    expect(page.locator('#phoneConnectPanel')).to_be_visible()
    assert page.locator('#phoneGroup').evaluate('node=>node.open')
    page.click('#tourCloseBtn')
    assert not any(path == '/api/output/config' and body.get('enabled') is True for path, body in api.requests)


def test_view_teaching_reveals_current_body_category(desktop):
    page, api = desktop.page, desktop.api
    api.hand['config']['enabled'] = False
    page.reload()
    page.wait_for_function("!document.querySelector('#viewHorizontalSource').disabled")
    page.click('nav [data-view="devices"]')
    open_lesson(page, 'fit')
    page.click('#tourCloseBtn')
    page.evaluate("async()=>{const {tutorial}=await import('/js/shell.js');tutorial.open(null,{menu:false})}")
    page.click('#tourSkipBtn')
    expect(page.locator('#viewHorizontalSource')).to_be_visible()
    expect(page.locator('#settingsNav [data-pane="view"]')).to_have_attribute('aria-current', 'true')


def test_recording_teaching_reveals_format_controls_without_starting_capture(desktop):
    page, api = desktop.page, desktop.api
    page.route('**/api/studio/state', lambda route: api.fulfill(route, {'config': {'layout': 'landscape', 'resolution': '1080'}}))
    page.click('nav [data-view="devices"]')
    open_lesson(page, 'studio')
    expect(page.locator('#tourSay')).to_have_text('选择画幅比例和录制分辨率')
    expect(page.locator('#studioResolution')).to_be_visible()
    expect(page.locator('#studioLayout')).to_be_visible()
    expect(page.locator('#tourHint')).to_contain_text('1920 × 1080')
    page.click('#tourChoiceBtn')
    expect(page.locator('#tourSay')).to_contain_text('选择画面并录制')
    expect(page.locator('#studioStartBtn')).to_be_visible()
    page.click('#tourNextBtn')
    expect(page.locator('#tour')).to_be_hidden()
    assert not any('/recording/start' in path or path == '/api/output/config' and body.get('enabled') is True
                   for path, body in api.requests)


def test_voice_teaching_follows_visible_microphone_and_model_panels(desktop):
    page, api = desktop.page, desktop.api
    api.runtime['camera'].update(running=True, frames=1, last_frame_age_ms=0)
    api.voice.update(source_kind='computer', connected=False)
    page.wait_for_timeout(1000)
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="fitness"]')
    open_lesson(page, 'voice')
    expect(page.locator('#tourSay')).to_have_text('选择要用的麦克风')
    expect(page.locator('#audioDeviceRow')).to_be_visible()
    assert page.locator('#microphoneSettings').evaluate('node=>node.open')
    api.voice['model_ready'] = False
    expect(page.locator('#tourSay')).to_have_text('查看语音模型状态')
    expect(page.locator('#voiceDiagnostic')).to_be_visible()
    expect(page.locator('#settingsNav [data-pane="lab"]')).to_have_attribute('aria-current', 'true')
    api.voice.update(model_ready=True, connected=True, audio_alive=True)
    expect(page.locator('#tourSay')).to_contain_text('回到「开始」页')
    page.click('nav [data-view="play"]')
    expect(page.locator('#tourSay')).to_contain_text('说「体感紧急停止」')
    expect(page.locator('#voiceCommandsBtn')).to_be_visible()
    api.voice['commands_heard'] = 1
    expect(page.locator('#tourSay')).to_have_text('点「查看口令」')
    page.click('#voiceCommandsBtn')
    expect(page.locator('#voiceCommandsMask')).to_be_visible()
    assert not any(path == '/api/output/config' and body.get('enabled') is True for path, body in api.requests)
