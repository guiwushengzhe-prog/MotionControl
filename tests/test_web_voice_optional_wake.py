"""浏览器检查可选前缀的实际布局、保存和已映射口令清单，不采集截图。"""
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from test_web_optimization import ORIGIN, desktop
from test_web_ui import browser as mock_browser


def prepare(desktop, prefix=""):
    api, page = desktop.api, desktop.page
    api.voice.update(wake_word=prefix, wake_system_commands=False, system_wake_word="体感",
                     emergency_stop_phrases=["体感紧急停止"], custom_emergency_stop_phrases=[],
                     builtin_emergency_phrase="体感紧急停止",
                     mappings=[{"phrase": "跳跃", "type": "keyboard", "target": "SPACE", "behavior": "tap"}])

    def save(route):
        body = route.request.post_data_json
        api.requests.append((urlparse(route.request.url).path, body))
        api.voice.update(body)
        api.voice["system_wake_word"] = api.voice["wake_word"] if api.voice.get("wake_system_commands") else "体感"
        api.voice["builtin_emergency_phrase"] = api.voice["system_wake_word"] + "紧急停止"
        api.fulfill(route, api.voice)

    page.route(ORIGIN + "/api/voice/config", save)
    page.evaluate("async()=>{const v=await import('/js/voice.js');await v.refreshVoice();v.renderVoiceRows(v.voiceRowsFromStatus(v.voice.status));}")
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="voice"]')


@pytest.mark.parametrize("prefix", ["", "体感", "小助手请听我的游戏指令"])
@pytest.mark.parametrize("width", [1280, 560])
def test_empty_short_and_long_prefixes_do_not_overlap_or_overflow(desktop, prefix, width):
    page = desktop.page
    page.set_viewport_size({"width": width, "height": 920})
    prepare(desktop, prefix)
    expect(page.locator('#wakeWord')).to_have_value(prefix)
    general_prefix = page.locator('#voiceRows .voice-prefix')
    if prefix:
        expect(general_prefix).to_be_visible()
        expect(general_prefix).to_have_text(prefix)
    else:
        expect(general_prefix).to_be_hidden()
    expect(page.locator('#personalVoicePanel .voice-prefix[data-scope="builtin"]')).to_have_text("体感")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    boxes = page.locator('#voiceRows .voice-phrase-wrap').evaluate("el=>{const p=el.querySelector('.voice-prefix').getBoundingClientRect(),i=el.querySelector('input').getBoundingClientRect(),b=el.getBoundingClientRect();return {p:{x:p.x,y:p.y,r:p.right,b:p.bottom},i:{x:i.x,y:i.y,r:i.right,b:i.bottom},b:{x:b.x,r:b.right}}}")
    assert boxes["i"]["x"] >= boxes["b"]["x"] - 1
    assert boxes["i"]["r"] <= boxes["b"]["r"] + 1
    if prefix:
        p, i = boxes["p"], boxes["i"]
        assert p["r"] <= i["x"] + 1 or p["b"] <= i["y"] + 1


def test_clearing_prefix_and_toggling_system_scope_updates_actual_examples(desktop):
    page, api = desktop.page, desktop.api
    prepare(desktop, "小助手")
    page.locator('#wakeWord').fill("")
    page.locator('#wakeWord').blur()
    page.wait_for_function("personalVoiceStatus.textContent.includes('直接说')")
    assert api.voice["wake_word"] == ""
    expect(page.locator('#voiceRows .voice-prefix')).to_be_hidden()
    page.locator('#wakeSystemCommands').check()
    page.wait_for_function("voiceSystemWakeDescription.textContent.includes('内置口令也直接说')")
    assert api.voice["wake_system_commands"]
    expect(page.locator('#personalVoicePanel .voice-prefix[data-scope="builtin"]')).to_be_hidden()
    page.locator('#wakeWord').fill("小助手请听我的游戏指令")
    page.locator('#wakeWord').blur()
    page.wait_for_function("voiceSystemWakeDescription.textContent.includes('小助手请听我的游戏指令紧急停止')")
    expect(page.locator('#wakeSystemCommands')).to_be_checked()


def test_shared_behavior_is_selectable_and_system_actions_show_execute_once(desktop):
    page, api = desktop.page, desktop.api
    prepare(desktop)
    select = page.locator('#voiceRows .voice-behavior')
    expect(select).to_be_enabled()
    assert select.locator('option').all_text_contents() == ["点一下", "持续按住", "松开"]
    select.select_option("hold")
    page.wait_for_function("voiceSaveStatus.textContent.includes('已保存')")
    assert api.voice["mappings"][0]["behavior"] == "hold"
    select.select_option("release")
    page.wait_for_function("voiceSaveStatus.textContent.includes('已保存')")
    page.wait_for_function("!document.querySelector('#voiceRows .voice-behavior').disabled")
    assert api.voice["mappings"][0]["behavior"] == "release"
    page.locator('#voiceRows .voice-type').select_option("system")
    expect(select).to_be_hidden()
    expect(page.locator('#voiceRows .voice-system-behavior')).to_have_text("执行一次")
    expect(page.locator('#voiceRows .voice-system-behavior')).to_be_visible()


def test_command_dialog_shows_only_mapped_game_and_shared_phrases(desktop):
    page, api = desktop.page, desktop.api
    prepare(desktop)
    api.commands = [
        {"id": "system.emergency_stop", "phrase": "体感紧急停止", "system_fixed": True, "scope": "builtin", "label": "紧急停止"},
        {"id": "game.profile_slot_01", "phrase": "爬绳", "scope": "game", "effective_action": {"type": "keyboard", "target": "C", "behavior": "hold"}},
        {"id": "game.profile_slot_02", "phrase": "未映射口令", "scope": "game", "effective_action": None},
        {"id": "game.profile_slot_03", "phrase": "关闭口令", "scope": "game", "effective_action": {"type": "none", "target": "C"}},
        {"id": "shared.0", "phrase": "跳跃", "scope": "shared", "effective_action": {"type": "keyboard", "target": "SPACE", "behavior": "tap"}},
        {"id": "shared.1", "phrase": "尚未选输出", "scope": "shared", "effective_action": {"type": "keyboard", "target": ""}},
    ]
    page.click('nav [data-view="play"]')
    page.click('#voiceCommandsBtn')
    expect(page.locator('#voiceCommandsMask')).to_be_visible()
    grid = page.locator('#voiceCommandGrid')
    assert grid.locator('.voice-command-card').count() == 3
    expect(grid).to_contain_text("爬绳")
    expect(grid).to_contain_text("持续按住")
    expect(grid).to_contain_text("跳跃")
    assert "未映射口令" not in grid.inner_text()
    assert "尚未选输出" not in grid.inner_text()
    assert "体感跳跃" not in grid.inner_text()
