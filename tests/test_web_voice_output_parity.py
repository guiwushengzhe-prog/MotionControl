"""两处口令的完整下拉、保存及独立控件布局；不采集摄像头或麦克风。"""
import pytest
from playwright.sync_api import expect

from motioncontrol_shared.mapping_schema import shared_voice_command_id
from test_web_voice_optional_wake import prepare
from test_web_optimization import desktop
from test_web_ui import browser as mock_browser


def setup_rows(desktop):
    page, api = desktop.page, desktop.api
    api.profile["bindings"]["voice"] = {"game.profile_slot_01": {
        "phrase": "游戏跳跃", "action": {"type": "keyboard", "target": "SPACE", "behavior": "tap"}}}
    api.commands = [{"id": "game.profile_slot_01", "phrase": "游戏跳跃", "scope": "game",
                     "effective_action": api.profile["bindings"]["voice"]["game.profile_slot_01"]["action"]}]
    prepare(desktop)
    page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoiceCommands();const m=await import('/js/mapping.js');await m.refreshProfile();}")


@pytest.mark.parametrize("width", [1280, 560])
def test_both_menus_have_all_output_types_and_game_controls_are_separate(desktop, width):
    page = desktop.page
    page.set_viewport_size({"width": width, "height": 920})
    setup_rows(desktop)
    general = page.locator('#voiceRows .voice-type').evaluate("el=>[...el.options].map(o=>o.value)")
    page.click('nav [data-view="games"]')
    page.click('#mapTabs [data-tab="voice"]')
    row = page.locator('.binding-row[data-trigger="voice.game.profile_slot_01"]')
    game = row.locator('.binding-type').evaluate("el=>[...el.options].map(o=>o.value).filter(Boolean)")
    assert general == game == ["keyboard", "mouse_button", "mouse_wheel", "gamepad", "gamepad_trigger",
                               "gamepad_axis", "macro", "voice_release", "system"]
    box = row.locator('.binding-output').evaluate("el=>{const a=el.querySelector('.binding-type').getBoundingClientRect(),b=el.querySelector('.binding-target').getBoundingClientRect();return {gap:b.left-a.right,border:getComputedStyle(el).borderWidth,typeRadius:getComputedStyle(el.querySelector('.binding-type')).borderRadius}}")
    assert box["gap"] >= 6
    assert box["border"] == "0px"
    assert box["typeRadius"] != "0px"
    assert row.locator('.binding-behavior option').all_text_contents() == ["点一下", "持续按住", "松开"]
    row.locator('.binding-type').select_option('system')
    game_system = row.locator('.binding-target option').all_text_contents()
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="voice"]')
    page.locator('#voiceRows .voice-type').select_option('system')
    assert page.locator('#voiceRows .binding-target option').all_text_contents() == game_system
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert desktop.errors == []


def test_new_shared_outputs_save_and_reload_combo_lead_and_release_selections(desktop):
    page, api = desktop.page, desktop.api
    setup_rows(desktop)
    row = page.locator('#voiceRows .voice-row').first
    row.locator('.voice-type').select_option('mouse_button')
    row.locator('.binding-target').select_option('RIGHT')
    row.locator('.voice-behavior').select_option('hold')
    page.wait_for_function("voiceSaveStatus.textContent.includes('已保存')")
    assert api.voice["mappings"][0]["target"] == "RIGHT"
    assert api.voice["mappings"][0]["behavior"] == "hold"
    row.locator('.voice-type').select_option('gamepad')
    row.locator('select.binding-target').select_option('__combo__')
    row.locator('.combo-picker').evaluate("el=>{for(const b of el.querySelectorAll('input'))b.checked=['LB','LS_UP'].includes(b.value);el.querySelector('input').dispatchEvent(new Event('change',{bubbles:true}))}")
    row.locator('.combo-lead-ms').fill('35')
    row.locator('.combo-lead-ms').dispatch_event('change')
    page.wait_for_function("voiceSaveStatus.textContent.includes('已保存')")
    assert api.voice["mappings"][0]["combo_stick_lead_ms"] == 35
    page.evaluate("async()=>{const v=await import('/js/voice.js');v.renderVoiceRows([]);await v.refreshVoice();v.renderVoiceRows(v.voiceRowsFromStatus(v.voice.status));}")
    expect(row.locator('.combo-picker input[value="LB"]')).to_be_checked()
    expect(row.locator('.combo-picker input[value="LS_UP"]')).to_be_checked()
    expect(row.locator('.combo-lead-ms')).to_have_value('35')

    shared_id = shared_voice_command_id('持续鼠标')
    api.commands = [{"id": shared_id, "phrase": "持续鼠标", "scope": "shared",
                     "effective_action": {"type": "mouse_button", "target": "LEFT", "behavior": "hold"}}]
    page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoiceCommands();}")
    row.locator('.voice-type').select_option('voice_release')
    row.locator('.voice-release-picker-button').click()
    row.locator('.voice-release-option').filter(has_text='持续鼠标').click()
    page.wait_for_function("voiceSaveStatus.textContent.includes('已保存')")
    assert api.voice["mappings"][0]["target"] == shared_id
    assert api.voice["mappings"][0]["behavior"] == 'tap'
