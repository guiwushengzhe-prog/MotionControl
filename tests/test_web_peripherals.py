import copy

from test_web_optimization import desktop, mock_browser
from test_peripherals import device
from motioncontrol.gunmouse import normalize_config
from motioncontrol.peripherals import device_catalog


def test_add_device_reuses_camera_and_paired_device_controls_and_hardware_readback(desktop):
    page, api = desktop.page, desktop.api
    status = {'config': normalize_config({'enabled': False, 'connection_enabled': True,
                                         'address': '12:34:56:78:90:AB'}),
              'state': 'off', 'connected': True, 'received': 0, 'executed': 0,
              'device': {**device(), 'read_at_ms': 100}}
    operations = []
    api.runtime['gunmouse'] = status
    page.route('**/api/peripherals/catalog', lambda route: api.fulfill(route, device_catalog()))
    page.route('**/api/gunmouse/devices', lambda route: api.fulfill(route, {'devices': [
        {'address': status['config']['address'], 'name': 'GunMouse SuperMini'}]}))
    def operation(route):
        body = route.request.post_data_json
        operations.append(body)
        if body['action'] == 'sensitivity':
            if body['value'] < 1:
                api.fulfill(route, {'error': '体感灵敏度须大于0'}, 400)
                return
            status['device']['details']['sens'] = body['value']
        if body['action'] == 'always':
            status['device']['details']['always'] = int(body['value'])
        api.fulfill(route, {'gunmouse': status})
    page.route('**/api/peripherals/gunmouse/operation', operation)
    page.evaluate("async()=>{await(await import('/js/peripherals.js')).refreshPeripheralCatalog();(await import('/js/shell.js')).showView('devices')}")
    assert page.locator('[data-peripheral]').count() == 4
    assert page.locator('#peripheralList > details').count() == 4
    assert page.locator('#peripheralList details[open]').count() == 0
    assert page.locator('#peripheralList button[data-peripheral]').count() == 0
    summary=page.locator('#cameraSettings > summary')
    assert '⌄' in summary.evaluate("node=>getComputedStyle(node,'::after').content")
    summary.focus();summary.press('Enter')
    assert page.locator('#cameraSettings').evaluate('(node)=>node.open')
    summary.press('Space')
    assert not page.locator('#cameraSettings').evaluate('(node)=>node.open')
    assert status['config']['address'] not in page.locator('#gunmouseSettings > summary').inner_text()
    assert not page.locator('#gunmouseSettings').evaluate('(node)=>node.open')
    page.click('#addPeripheralBtn')
    assert page.locator('#addPeripheralType option').count() == 4
    page.select_option('#addPeripheralType', 'gunmouse')
    assert page.locator('#addPeripheralBluetooth').is_visible()
    page.click('#addPeripheralContinue')
    page.wait_for_function("gunmouseSettings.open && gunmouseDevice.value === '12:34:56:78:90:AB'")
    assert not page.locator('#gunmouseHardware').evaluate('(node)=>node.open')
    page.locator('#gunmouseHardware > summary').click()
    page.wait_for_function("!gunmouseHardwareControls.disabled")
    assert page.locator('#gunmouseAlways').is_checked()
    assert not page.locator('#gunmouseJoystick').is_checked()
    page.wait_for_function("gunmouseOperationStatus.textContent === '设备状态已刷新'")
    assert all(item['action'] == 'refresh' for item in operations)
    page.fill('#gunmouseSensitivity', '96')
    page.evaluate("async()=>{await(await import('/js/peripherals.js')).refreshPeripheralCatalog()}")
    assert page.locator('#gunmouseHardware').evaluate('(node)=>node.open')
    page.wait_for_timeout(550)
    assert page.input_value('#gunmouseSensitivity') == '96'
    page.click('#gunmouseSensitivitySave')
    page.wait_for_function("gunmouseOperationStatus.textContent === '设备已确认设置'")
    assert operations[-1] == {'action': 'sensitivity', 'value': 96}
    page.uncheck('#gunmouseAlways')
    page.wait_for_function("gunmouseOperationStatus.textContent === '设备已确认设置' && !gunmouseAlways.checked")
    assert operations[-1] == {'action': 'always', 'value': False}
    page.fill('#gunmouseSensitivity', '-4')
    page.click('#gunmouseSensitivitySave')
    page.wait_for_function("gunmouseOperationStatus.textContent.includes('须大于0')")
    assert page.input_value('#gunmouseSensitivity') == '-4'
    for width in (390, 1280):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    offline = copy.deepcopy(status)
    offline.update(connected=False, device=None, state='disconnected')
    api.runtime['gunmouse'] = offline
    page.wait_for_function('gunmouseHardwareControls.disabled && gunmouseRefreshBtn.disabled')
    assert '等待设备回报' in page.locator('#gunmouseDeviceSummary').inner_text()
    page.click('#addPeripheralBtn')
    page.select_option('#addPeripheralType', 'camera')
    page.click('#addPeripheralContinue')
    page.wait_for_function("cameraScanStatus.textContent.includes('找到')")
    assert any(path == '/api/camera/devices' for path, _ in api.requests)
    assert not any(path == '/api/output' for path, _ in api.requests)
    assert not desktop.errors
