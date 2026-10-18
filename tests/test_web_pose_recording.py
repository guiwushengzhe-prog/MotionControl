"""骨骼录制首页状态的浏览器检查；所有请求均拦截，不接触真实摄像头。"""
import pytest
from playwright.sync_api import expect

from test_web_optimization import desktop, mock_browser


def recording_api(desktop, *, reject_start=False):
    page, api = desktop.page, desktop.api

    def handle(route):
        body = route.request.post_data_json if route.request.method == "POST" else None
        api.requests.append(("/api/pose/record", body))
        if body and body.get("cancel"):
            api.recording.update(state="cancelled", remaining_s=0, frames=0)
        elif body:
            if reject_start:
                api.fulfill(route, {"error": "录制器暂不可用"}, 503)
                return
            assert body == {"delay_s": 3, "duration_s": 15}
            api.recording.update(state="waiting", remaining_s=3, frames=0)
        api.fulfill(route, {"recording": api.recording})

    page.route("**/api/pose/record", handle)


def open_recorder(page):
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')
    expect(page.locator('#poseRecordBtn')).to_be_enabled()


def test_start_goes_home_and_keeps_countdown_until_saved(desktop):
    page, api = desktop.page, desktop.api
    recording_api(desktop)
    open_recorder(page)
    page.click('#poseRecordBtn')
    expect(page.locator('#playPanel')).to_be_visible()
    expect(page.locator('#devicesPanel')).to_be_hidden()
    expect(page.locator('#homePoseRecordStatus')).to_have_text('准备倒计时 3.0 秒')
    expect(page.locator('#homePoseRecordCancelBtn')).to_be_visible()
    api.recording.update(remaining_s=2.2)
    expect(page.locator('#homePoseRecordStatus')).to_have_text('准备倒计时 2.2 秒')
    api.recording.update(state='recording', remaining_s=12.4, frames=39)
    expect(page.locator('#homePoseRecordStatus')).to_have_text('录制剩余 12.4 秒 · 已 39 帧')
    api.recording.update(state='saving', remaining_s=0, frames=225)
    expect(page.locator('#homePoseRecordStatus')).to_have_text('正在保存…')
    expect(page.locator('#homePoseRecordCancelBtn')).to_be_hidden()
    expect(page.locator('#poseRecordBtn')).to_be_disabled()
    api.recording.update(state='done')
    expect(page.locator('#homePoseRecordStatus')).to_have_text('已保存 225 帧')
    expect(page.locator('#poseRecordBtn')).to_be_enabled()
    open_recorder(page)
    expect(page.locator('#poseRecordStatus')).to_have_text('已保存 225 帧')
    # 倒计时使用单独的首页状态，未打开校准或自定义动作倒计时。
    expect(page.locator('#calibrationOverlay')).not_to_be_visible()


@pytest.mark.parametrize('phase', ['waiting', 'recording'])
def test_home_cancel_uses_same_recorder_and_stays_home(desktop, phase):
    page, api = desktop.page, desktop.api
    recording_api(desktop)
    open_recorder(page)
    page.click('#poseRecordBtn')
    api.recording.update(state=phase)
    page.click('#homePoseRecordCancelBtn')
    expect(page.locator('#homePoseRecordStatus')).to_have_text('已取消')
    expect(page.locator('#playPanel')).to_be_visible()
    expect(page.locator('#homePoseRecordCancelBtn')).to_be_hidden()
    expect(page.locator('#poseRecordCancelBtn')).to_be_disabled()
    assert ('/api/pose/record', {'cancel': True}) in api.requests


def test_failed_start_stays_in_settings_with_error(desktop):
    page = desktop.page
    recording_api(desktop, reject_start=True)
    open_recorder(page)
    page.click('#poseRecordBtn')
    expect(page.locator('#poseRecordStatus')).to_have_text('录制器暂不可用')
    expect(page.locator('#devicesPanel')).to_be_visible()
    expect(page.locator('#homePoseRecorder')).to_be_hidden()


def test_reload_recovers_active_recording_on_home_without_restarting(desktop):
    page, api = desktop.page, desktop.api
    recording_api(desktop)
    api.recording.update(state='recording', remaining_s=8.6, frames=96)
    api.requests.clear()
    page.reload()
    expect(page.locator('#homePoseRecordStatus')).to_have_text('录制剩余 8.6 秒 · 已 96 帧')
    expect(page.locator('#homePoseRecorder')).to_be_visible()
    expect(page.locator('#homePoseRecordCancelBtn')).to_be_visible()
    assert not any(path == '/api/pose/record' and body is not None for path, body in api.requests)


def test_read_from_before_start_cannot_replace_active_countdown(desktop):
    page, api = desktop.page, desktop.api
    recording_api(desktop)
    open_recorder(page)
    held = []

    def hold_read(route):
        if route.request.method == 'GET':
            held.append(route)
        else:
            route.fallback()

    page.route('**/api/pose/record', hold_read)
    page.evaluate("void import('/js/diagnostics.js').then(m=>m.refreshPoseRecord())")
    page.wait_for_function('true')
    assert held
    page.click('#poseRecordBtn')
    expect(page.locator('#homePoseRecordStatus')).to_have_text('准备倒计时 3.0 秒')
    api.fulfill(held[0], {'recording': {'state': 'idle', 'frames': 0, 'remaining_s': 0}})
    page.wait_for_function("document.querySelector('#homePoseRecorder').hidden === false")
    expect(page.locator('#homePoseRecordStatus')).to_have_text('准备倒计时 3.0 秒')
    expect(page.locator('#poseRecordBtn')).to_be_disabled()


@pytest.mark.parametrize('terminal', ['done', 'cancelled'])
def test_success_or_cancel_result_hides_after_four_seconds_and_stays_in_settings(desktop, terminal):
    page, api = desktop.page, desktop.api
    recording_api(desktop)
    open_recorder(page)
    page.click('#poseRecordBtn')
    page.clock.install()
    api.recording.update(state=terminal, remaining_s=0, frames=225 if terminal == 'done' else 0)
    page.evaluate("import('/js/diagnostics.js').then(m=>m.refreshPoseRecord())")
    label = '已保存 225 帧' if terminal == 'done' else '已取消'
    expect(page.locator('#homePoseRecordStatus')).to_have_text(label)
    expect(page.locator('#homePoseRecorder')).to_be_visible()
    page.clock.fast_forward(3000)
    expect(page.locator('#homePoseRecorder')).to_be_visible()
    page.clock.fast_forward(1100)
    expect(page.locator('#homePoseRecorder')).to_be_hidden()
    expect(page.locator('#poseRecordStatus')).to_have_text(label)
    # 终态轮询已经停止，自动收起无须等待下一次状态刷新；再读取也不能重新弹出。
    page.evaluate("import('/js/diagnostics.js').then(m=>m.refreshPoseRecord())")
    expect(page.locator('#homePoseRecorder')).to_be_hidden()
    page.reload()
    expect(page.locator('#poseRecordStatus')).to_have_text(label)
    expect(page.locator('#homePoseRecorder')).to_be_hidden()


@pytest.mark.parametrize(('terminal', 'error', 'label'), [
    ('done', '', '未采集到骨骼数据，请开始识别后重试'),
    ('error', '骨骼数据保存失败', '骨骼数据保存失败'),
])
def test_empty_recording_or_error_remains_visible_after_timeout_and_reload(desktop, terminal, error, label):
    page, api = desktop.page, desktop.api
    recording_api(desktop)
    open_recorder(page)
    page.click('#poseRecordBtn')
    page.clock.install()
    api.recording.update(state=terminal, remaining_s=0, frames=0, error=error)
    page.evaluate("import('/js/diagnostics.js').then(m=>m.refreshPoseRecord())")
    expect(page.locator('#homePoseRecordStatus')).to_have_text(label)
    page.clock.fast_forward(5000)
    expect(page.locator('#homePoseRecorder')).to_be_visible()
    expect(page.locator('#poseRecordStatus')).to_have_text(label)
    expect(page.locator('#homePoseRecordCancelBtn')).to_be_hidden()
    page.reload()
    expect(page.locator('#homePoseRecordStatus')).to_have_text(label)
    expect(page.locator('#homePoseRecorder')).to_be_visible()
