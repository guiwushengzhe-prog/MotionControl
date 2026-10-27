from test_web_optimization import desktop, mock_browser


def test_punch_binding_uses_existing_picker_and_settings_save(desktop):
    page, api = desktop.page, desktop.api
    status = {'config': {'enabled': False, 'address': '', 'strength_mg': 1800, 'cooldown_ms': 500},
              'state': 'off', 'received': 0, 'executed': 0}
    saved = []
    def configure(route):
        saved.append(route.request.post_data_json)
        status['config'] = saved[-1]
        api.fulfill(route, {'gunmouse': status})
    page.route('**/api/gunmouse/config', configure)
    page.route('**/api/gunmouse/devices', lambda route: api.fulfill(route, {'devices': [
        {'address': '12:34:56:78:90:AB', 'name': 'GunMouse SuperMini'}]}))
    page.evaluate("async status=>{(await import('/js/gunmouse.js')).renderGunMouse(status);(await import('/js/shell.js')).showView('devices')}", status)
    page.click('#gunmouseScanBtn')
    page.wait_for_function("gunmouseDevice.value === '12:34:56:78:90:AB'")
    page.check('#gunmouseEnabled')
    page.locator('#gunmouseStrength').fill('2100')
    page.locator('#gunmouseStrength').dispatch_event('change')
    page.locator('#gunmouseCooldown').fill('750')
    page.locator('#gunmouseCooldown').dispatch_event('change')
    page.wait_for_function("gunmouseSaveStatus.textContent === '已保存'")
    assert saved[-1] == {'enabled': True, 'address': '12:34:56:78:90:AB', 'strength_mg': 2100, 'cooldown_ms': 750}
    page.click('#gunmouseBindingBtn')
    row = page.locator('.binding-row[data-trigger="motion.gunmouse_punch"]')
    assert row.is_visible()
    types = row.locator('select.binding-type')
    assert types.input_value() == ''
    assert types.locator('option[value="keyboard"]').count() == 1
    # 补齐正式接口的键鼠元数据，继续用同一个绑定编辑器。
    page.evaluate("async()=>{const s=await import('/js/state.js');s.gameProfile.actions={keyboard:{free_text:true},mouse_button:{targets:['LEFT','RIGHT','MIDDLE']},gamepad_axis:{targets:['LS_UP','LS_DOWN']}};(await import('/js/mapping.js')).renderProfileBindingRows()}")
    page.evaluate("async()=>{(await import('/js/mapping.js')).revealBindingRow('motion.gunmouse_punch')}")
    types = row.locator('select.binding-type')
    values = types.locator('option').evaluate_all('(items)=>items.map(item=>item.value)')
    assert 'keyboard' in values and 'mouse_button' in values and 'gamepad_axis' in values
    assert 'system' not in values and 'macro' not in values
    types.select_option('keyboard')
    row.locator('.binding-target').click()
    row.locator('.binding-target').press('f')
    page.wait_for_function("!!document.querySelector('.binding-row[data-trigger=\"motion.gunmouse_punch\"] .binding-behavior')")
    assert row.locator('.binding-behavior').inner_text() == '点一下'
    page.wait_for_timeout(650)
    writes = [body for path, body in api.requests if path == '/api/game-profiles/overrides']
    assert writes[-1]['overrides']['motion.gunmouse_punch']['action']['target'] == 'F'
