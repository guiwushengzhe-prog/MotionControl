"""语音识别保持安静，成功口令沿用首页最近触发；接口全隔离。"""
from playwright.sync_api import expect

from test_web_optimization import desktop, mock_browser  # noqa: F401


def test_voice_recognition_does_not_replace_manual_feedback_or_popup(desktop):
    page, api = desktop.page, desktop.api
    page.evaluate("async()=>{(await import('/js/core.js')).notice('操作未完成：模拟故障');window.voiceNoticeChanges=[];new MutationObserver(items=>voiceNoticeChanges.push(...items)).observe(notice,{subtree:true,childList:true,characterData:true,attributes:true});}")
    for index, (action, executed) in enumerate((("wake", None), ("keyboard:V", False), ("keyboard:V", True)), 1):
        api.voice.update(commands_heard=index, last_final="药", last_action=action,
                         last_executed=executed, last_error="output disabled" if executed is False else "")
        page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoice()}")
        expect(page.locator("#notice")).to_have_text("操作未完成：模拟故障")
    assert page.evaluate("voiceNoticeChanges.length") == 0
    assert not page.locator("#rangeLog button").count(), "识别状态本身不能伪造成功触发"


def test_successful_voice_is_in_home_history_once_per_execution(desktop):
    page, api = desktop.page, desktop.api
    event = {"at": 99, "trigger": "voice.shared.药", "label": "药",
             "action": {"type": "keyboard", "target": "V", "behavior": "tap"}}
    api.runtime["kernel"]["recent_triggers"] = [event]
    api.voice.update(commands_heard=1, last_final="药", last_action="keyboard:V", last_executed=True)
    page.evaluate("async()=>{const p=await import('/js/play.js'),v=await import('/js/voice.js');for(let i=0;i<3;i++){await p.refreshKernel();await v.refreshVoice()}}")
    expect(page.locator("#recentCard")).to_be_visible()
    expect(page.locator("#rangeLog button")).to_have_count(1)
    expect(page.locator("#rangeLog button")).to_contain_text("药 → V")
    api.runtime["kernel"]["recent_triggers"].append({**event, "at": 99.5})
    page.evaluate("async()=>{const p=await import('/js/play.js');await p.refreshKernel();await p.refreshKernel()}")
    expect(page.locator("#rangeLog button")).to_have_count(2)


def test_voice_status_read_failure_still_explains_the_error(desktop):
    page, api = desktop.page, desktop.api
    page.route("**/api/voice/status", lambda route: api.fulfill(route, {"error": "模拟读取失败"}, 503))
    assert page.evaluate("async()=>await (await import('/js/voice.js')).refreshVoice()") is False
    expect(page.locator("#voiceStatus")).to_have_text("语音状态无法确认")
    expect(page.locator("#voicePill")).to_have_text("未确认")
