"""实际浏览器验证完整配合/重置列表、保存与口令更新，不采集私人画面。"""
import pytest

from motioncontrol_shared.mapping_schema import shared_voice_command_id
from motioncontrol_shared.profile_schema import normalize_overrides
from test_alternate_body_action import CONDITION
from test_web_optimization import ORIGIN, desktop, mock_browser


@pytest.mark.parametrize("mode", ["with_trigger", "cycle"])
def test_all_mappable_groups_can_be_selected_saved_and_reloaded(desktop, mode):
    page, api = desktop.page, desktop.api
    shared_id = shared_voice_command_id("配合口令")
    api.profile["bindings"]["motions"]["march"] = {**CONDITION, "alternate_when": "zone.leftHand"}
    api.custom_poses = [{"id": "custom1", "name": "我的姿势", "threshold": .8}]
    api.commands = [
        {"id": "game.profile_slot_01", "phrase": "游戏口令", "scope": "game", "system_fixed": False},
        {"id": shared_id, "phrase": "体感配合口令", "scope": "shared", "system_fixed": False},
        {"id": "system.emergency_stop", "phrase": "紧急停止", "system_fixed": True},
    ]
    page.route(ORIGIN + "/api/pose/library", lambda route: api.fulfill(route, {"library": [
        {"id": "hands_cross", "trigger": "pose.hands_cross", "group": "pose", "source": "cloud", "name": "双手交叉"}]}))

    def save(route):
        body = route.request.post_data_json
        overrides = normalize_overrides(body["overrides"])
        api.profile["overrides"] = overrides
        for key, binding in overrides.items():
            if binding:
                group, ident = key.split(".", 1)
                api.profile["bindings"][{"zone": "zones", "motion": "motions", "pose": "poses", "voice": "voice"}[group]][ident] = binding
        api.fulfill(route, {"profile": api.profile})

    page.route(ORIGIN + "/api/game-profiles/overrides", save)
    page.evaluate("async()=>{const l=await import('/js/library.js');await l.refreshPoseLibrary({rebuild:false});await l.refreshCustomPoses({rebuild:false});await (await import('/js/voice.js')).refreshVoiceCommands();await (await import('/js/mapping.js')).refreshProfile();}")
    page.click('nav [data-view="games"]')
    page.click('#mapTabs [data-tab="body"]')
    row = page.locator('.binding-row[data-trigger="motion.march"]')
    row.locator('.binding-more').click()
    if mode == "cycle":
        row.locator('.binding-alternate-mode').select_option(mode)
    pick = row.locator('.binding-rule-trigger')
    values = pick.evaluate("el=>[...el.options].map(o=>o.value).filter(Boolean)")
    assert set(values) == {"zone.leftHand", "zone.rightHand", "zone.leftFoot", "zone.rightFoot", "zone.headJump",
                           "motion.calf_back", "motion.jump", "pose.hands_cross", "pose.custom1",
                           "voice.game.profile_slot_01", f"voice.{shared_id}"}
    assert pick.locator('optgroup').evaluate_all("els=>els.map(el=>el.label)") == ["触发圈", "身体动作", "姿势", "语音"]
    assert "体感配合口令" in pick.locator(f'option[value="voice.{shared_id}"]').inner_text()
    field = "reset_trigger" if mode == "cycle" else "alternate_when"
    for key in ("zone.leftHand", "pose.custom1", "voice.game.profile_slot_01", f"voice.{shared_id}"):
        pick.select_option(key)
        page.wait_for_function("key=>profileSaveStatus.textContent==='已保存' && document.querySelector('.binding-rule-trigger').value===key", arg=key)
        assert api.profile["overrides"]["motion.march"][field] == key
    page.evaluate("async()=>{await (await import('/js/mapping.js')).refreshProfile();}")
    assert row.locator('.binding-rule-trigger').input_value() == f"voice.{shared_id}"
    api.commands.append({"id": "game.profile_slot_02", "phrase": "新增口令", "system_fixed": False})
    page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoiceCommands();}")
    assert row.locator('.binding-rule-trigger option[value="voice.game.profile_slot_02"]').count() == 1
    assert row.locator('.binding-rule-trigger').input_value() == f"voice.{shared_id}"
