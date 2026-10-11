"""首页控制、识别入口和整条动作映射删除的浏览器检查。"""
import pytest
from playwright.sync_api import expect

from test_web_optimization import desktop
from test_web_ui import browser as mock_browser


def test_top_control_and_f9_pause_without_stopping_camera(desktop):
    page, api = desktop.page, desktop.api
    page.click('#sourceStartBtn')
    expect(page.locator('#sourceStopBtn')).to_be_visible()
    assert api.output['enabled'] is False
    page.click('#mainActionBtn')
    expect(page.locator('#mainActionBtn')).to_have_class('btn cta running stop')
    expect(page.locator('#mainActionBtn kbd')).to_have_text('F9')
    assert page.locator('#stopBtn').count() == 0
    page.keyboard.press('F9')
    expect(page.locator('#mainActionLabel')).to_have_text('开始控制')
    assert api.runtime['camera']['running']
    assert api.voice['connected']
    assert not api.output['enabled']
    assert not any(path == '/api/input/stop' for path, _ in api.requests)
    page.click('#mainActionBtn')
    expect(page.locator('#mainActionLabel')).to_have_text('暂停控制')
    page.click('#sourceStopBtn')
    expect(page.locator('#sourceStartBtn')).to_be_visible()
    assert not api.runtime['camera']['running']
    assert not api.voice['connected']
    assert not api.output['enabled']


@pytest.mark.parametrize('trigger', ['motion.march', 'pose.custom_a'])
def test_delete_whole_mapping_retry_then_readd_keeps_action_library(desktop, trigger):
    page, api = desktop.page, desktop.api
    api.custom_poses = [{'id': 'custom_a', 'name': '自己的动作', 'frames': 1, 'enabled': True, 'threshold': .8}]
    group, ident = ('motions', 'march') if trigger.startswith('motion.') else ('poses', 'custom_a')
    api.profile['bindings'][group][ident] = {'action': {'type': 'keyboard', 'target': 'W', 'behavior': 'hold'}}
    page.reload()
    page.wait_for_function("currentGameName.textContent === 'Default game'")
    page.click('nav [data-view="games"]')
    page.click('#mapTabs [data-tab="body"]')
    row = page.locator(f'.binding-row[data-trigger="{trigger}"]')
    expect(row).to_be_visible()
    api.fail_overrides = True
    row.locator('.binding-remove').click()
    expect(page.locator('#notice')).to_contain_text('draft could not be saved')
    expect(row).to_be_visible()
    assert row.locator('.binding-target').input_value() == 'W'
    assert api.profile['bindings'][group][ident]['action']['target'] == 'W'
    api.fail_overrides = False
    row.locator('.binding-remove').click()
    expect(row).to_have_count(0)
    assert api.profile['bindings'][group][ident] == {'disabled': True}
    assert api.custom_poses[0]['id'] == 'custom_a'
    page.reload()
    page.wait_for_function("currentGameName.textContent === 'Default game'")
    page.click('nav [data-view="games"]')
    page.click('#mapTabs [data-tab="body"]')
    expect(row).to_have_count(0)
    # 库里的动作仍能再次加入；重新加入后是一条待设置键位的完整动作项。
    page.evaluate("async key => (await import('/js/mapping.js')).revealBindingRow(key)", trigger)
    expect(row).to_be_visible()
    assert row.locator('.binding-type').input_value() == ''
    assert row.locator('.binding-remove').count() == 1


def test_delete_unfinished_action_does_not_require_completing_its_condition(desktop):
    page, api = desktop.page, desktop.api
    page.click('nav [data-view="games"]')
    page.evaluate("async()=> (await import('/js/mapping.js')).revealBindingRow('motion.march')")
    row = page.locator('.binding-row[data-trigger="motion.march"]')
    row.locator('.binding-type').select_option('keyboard')
    row.locator('.binding-target').press('W')
    row.locator('.binding-more').click()
    row.locator('.binding-alternate-add').click()
    assert row.locator('.binding-rule-trigger').input_value() == ''
    row.locator('.binding-remove').click()
    expect(row).to_have_count(0)
    assert api.profile['overrides']['motion.march'] is None
