"""输入法确认后才校验和保存口令；使用隔离接口，不采集音频或打开窗口。"""

import copy

from test_web_optimization import ORIGIN, desktop, mock_browser  # noqa: F401


def prepare(desktop):
    page, api = desktop.page, desktop.api
    api.voice.update(wake_word="", mappings=[{"phrase": "跳跃", "type": "keyboard", "target": "SPACE", "behavior": "tap"}])

    def save(route):
        body = route.request.post_data_json
        api.requests.append(("/api/voice/config", copy.deepcopy(body)))
        api.voice.update(body)
        api.fulfill(route, api.voice)

    page.route(ORIGIN + "/api/voice/config", save)
    page.evaluate("async()=>{const v=await import('/js/voice.js');await v.refreshVoice();v.renderVoiceRows(v.voiceRowsFromStatus(v.voice.status))}")
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="voice"]')
    page.wait_for_timeout(350)
    api.requests.clear()


def composing(page, selector, text):
    page.locator(selector).focus()
    page.locator(selector).evaluate("""(input,text)=>{
      input.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));
      input.value=text;
      input.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true,inputType:'insertCompositionText',data:text}));
    }""", text)


def commit(page, selector, text):
    page.locator(selector).evaluate("""(input,text)=>{
      input.value=text;
      input.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true,data:text}));
      input.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:false,inputType:'insertText',data:text}));
    }""", text)


def test_composing_pinyin_is_neither_checked_nor_saved(desktop):
    page, api = desktop.page, desktop.api
    prepare(desktop)
    checked = []

    def check(route):
        phrases = route.request.post_data_json["phrases"]
        checked.extend(phrases)
        api.fulfill(route, {"available": True, "results": [{"unheard": list(text) if text.isascii() else []} for text in phrases]})

    page.route(ORIGIN + "/api/voice/check", check)
    selector = "#voiceRows .voice-phrase"
    composing(page, selector, "dakaibeibao")
    page.wait_for_timeout(700)
    assert checked == []
    assert not any(path == "/api/voice/config" for path, body in api.requests)
    assert page.locator(selector).input_value() == "dakaibeibao"
    commit(page, selector, "打开背包")
    page.wait_for_function("voiceSaveStatus.textContent.includes('已保存')")
    assert "打开背包" in checked
    assert "dakaibeibao" not in checked
    assert api.voice["mappings"][0]["phrase"] == "打开背包"
    assert page.locator(selector).input_value() == "打开背包"
    assert not page.locator("#voiceRows .voice-phrase-tip:visible").count()


def test_late_english_warning_cannot_replace_committed_chinese(desktop):
    page, api = desktop.page, desktop.api
    prepare(desktop)
    held = {}

    def check(route):
        phrases = route.request.post_data_json["phrases"]
        if "beibao" in phrases:
            held["old"] = route
        else:
            api.fulfill(route, {"available": True, "results": [{"unheard": []} for text in phrases]})

    page.route(ORIGIN + "/api/voice/check", check)
    selector = "#voiceRows .voice-phrase"
    page.locator(selector).fill("beibao")
    page.wait_for_timeout(280)
    assert "old" in held
    composing(page, selector, "beibaoxiang")
    commit(page, selector, "背包箱")
    page.wait_for_timeout(300)
    api.fulfill(held["old"], {"available": True, "results": [{"unheard": ["b", "e"]}]})
    page.wait_for_timeout(100)
    assert not page.locator("#voiceRows .voice-phrase-tip:visible").count()
    assert page.locator(selector).input_value() == "背包箱"


def test_wake_word_composition_does_not_save_intermediate_change(desktop):
    page, api = desktop.page, desktop.api
    prepare(desktop)
    composing(page, "#wakeWord", "xiaozhu")
    page.locator("#wakeWord").dispatch_event("change")
    page.wait_for_timeout(450)
    assert not any(path == "/api/voice/config" for path, body in api.requests)
    commit(page, "#wakeWord", "小助手")
    page.locator("#wakeWord").dispatch_event("change")
    page.locator("#wakeWord").blur()
    page.wait_for_function("personalVoiceStatus.textContent.includes('已保存')")
    assert api.voice["wake_word"] == "小助手"
    assert all(body.get("wake_word") != "xiaozhu" for path, body in api.requests if path == "/api/voice/config")


def test_game_phrase_rename_saves_only_committed_chinese(desktop):
    page, api = desktop.page, desktop.api
    prepare(desktop)
    api.commands = [{"id": "game.profile_slot_01", "scope": "game", "phrase": "开门", "default_action": {"type": "keyboard", "target": "E", "behavior": "tap"}}]
    api.profile["bindings"]["voice"]["game.profile_slot_01"] = {"phrase": "开门", "action": {"type": "keyboard", "target": "E", "behavior": "tap"}}
    page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoiceCommands();await (await import('/js/mapping.js')).refreshProfile()}")
    page.click('nav [data-view="games"]')
    page.click('#mapTabs [data-tab="voice"]')
    selector = '.binding-row[data-trigger="voice.game.profile_slot_01"] .voice-trigger-phrase'
    api.requests.clear()
    composing(page, selector, "dakaimen")
    page.wait_for_timeout(600)
    assert not any(path == "/api/game-profiles/overrides" for path, body in api.requests)
    commit(page, selector, "打开门")
    page.wait_for_function("profileSaveStatus.textContent.includes('已保存')")
    assert api.profile["overrides"]["voice.game.profile_slot_01"]["phrase"] == "打开门"
    assert page.locator(selector).input_value() == "打开门"
