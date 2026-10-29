"""就绪状态省略说明，接口失败仍要显示原因。"""
from test_web_ui import browser, server, ui, sync_api


def test_ready_voice_hides_redundant_copy_but_read_failure_stays_visible(ui):
    page = ui.page
    endpoint = ui.base + '/api/voice/status'
    status = page.request.get(endpoint).json()
    status.update(connected=True, source_kind='phone', model_ready=True, available=True)
    page.route(endpoint, lambda route: route.fulfill(json=status))
    page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoice()}")
    sync_api.expect(page.locator('#voicePill')).to_have_text('在听')
    sync_api.expect(page.locator('#voiceStatus')).to_be_hidden()
    page.unroute(endpoint)
    page.route(endpoint, lambda route: route.fulfill(status=503, json={'error': '暂时无法读取'}))
    page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoice()}")
    # 摄像头和语音各有自己的检查项；先提供新画面，才展示语音问题。
    page.route(ui.base + '/api/kernel/status', lambda route: route.fulfill(json={
        'body_mode': 'computer', 'camera': {'running': True, 'frames': 10, 'last_frame_age_ms': 0},
        'kernel': {},
    }))
    page.evaluate("async()=>{await (await import('/js/play.js')).refreshKernel()}")
    sync_api.expect(page.locator('#cameraStepSub')).to_have_text('摄像头已就绪')
    sync_api.expect(page.locator('.checklist [data-step="voice"]')).to_be_visible()
    sync_api.expect(page.locator('#voiceStepSub')).to_have_text('语音状态无法确认')
    sync_api.expect(page.locator('#voiceStatus')).to_have_text('语音状态无法确认')
    assert ui.errors == []
