"""正式页面配合替代接口复现保存与键盘交互；不捕获真实画面。"""
import pytest
expect = pytest.importorskip('playwright.sync_api').expect
from test_web_optimization import desktop, mock_browser  # noqa: F401


def test_failed_recording_setting_is_included_in_next_successful_save(desktop):
    page, api = desktop.page, desktop.api
    saved = {'layout': 'landscape', 'resolution': '720'}
    writes = []
    def reply(route):
        patch = route.request.post_data_json
        writes.append(patch)
        if len(writes) == 1:
            api.fulfill(route, {'error': '模拟磁盘写入失败'}, status=500)
        else:
            saved.update(patch)
            api.fulfill(route, {'config': saved})
    page.route('**/api/studio/config', reply)
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="studio"]')
    page.select_option('#studioLayout', 'square')
    expect(page.locator('#studioStatus')).to_contain_text('设置未保存')
    page.select_option('#studioResolution', '1080')
    expect(page.locator('#studioStatus')).to_have_text('设置已保存')
    assert saved == {'layout': 'square', 'resolution': '1080'}
    assert writes[-1] == saved


def test_composition_enter_does_not_add_partial_game(desktop):
    page, api = desktop.page, desktop.api
    page.click('nav [data-view="games"]')
    page.evaluate("()=>{customGameName.value='中文';for(const options of [{isComposing:true},{keyCode:229}])customGameName.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,...options}))}")
    page.wait_for_timeout(200)
    assert not any('/custom/add' in path for path, _ in api.requests)
    page.evaluate("customGameName.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
    page.wait_for_timeout(200)
    assert any('/custom/add' in path for path, _ in api.requests)


def test_menu_keyboard_enters_moves_and_restores_focus(desktop):
    page, _ = desktop.page, desktop.api
    page.focus('#helpBtn')
    page.keyboard.press('ArrowDown')
    expect(page.locator('#tutorialBtn')).to_be_focused()
    page.keyboard.press('ArrowDown')
    expect(page.locator('#themeSeg button').first).to_be_focused()
    page.keyboard.press('End')
    expect(page.locator('#exitAppBtn')).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.locator('#helpMenu')).to_be_hidden()
    expect(page.locator('#helpBtn')).to_be_focused()
    page.keyboard.press('Enter')
    expect(page.locator('#tutorialBtn')).to_be_focused()
    page.keyboard.press('Tab')
    expect(page.locator('#helpMenu')).to_be_hidden()
    assert page.locator('#helpMenu').evaluate('node=>!node.contains(document.activeElement)')


def test_voice_dynamic_controls_have_accessible_names(desktop):
    page, _ = desktop.page, desktop.api
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="voice"]')
    expect(page.get_by_label('语音输出类型', exact=True)).to_be_visible()
    expect(page.get_by_label('语音按键或动作', exact=True)).to_be_visible()
