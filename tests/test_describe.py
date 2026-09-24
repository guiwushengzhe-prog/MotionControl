"""配置描述，以及它引用的那些中文名有没有和桌面端对上。

描述是用户判断"要不要装这份配置"的依据，所以它必须准确。最容易悄悄变得不准确的
不是逻辑而是名字：桌面端改了一个区域的叫法，共享包这边没跟上，云端就会一直显示
一个已经不存在的名字，而且没有任何报错。下面前几条测试就是钉这个的。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from motioncontrol_shared.canonical import canonicalize
from motioncontrol_shared.describe import (
    MOTION_NAMES,
    POSE_NAMES,
    VOICE_COMMAND_NAMES,
    ZONE_NAMES,
    ZONE_RUNTIME_ALIASES,
    describe,
    describe_action,
    trigger_name,
)

REPO = Path(__file__).resolve().parent.parent

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="control_kernel imports Windows-only modules")


# --- 名字有没有和三处来源对上 ------------------------------------------------

def test_voice_command_names_match_the_shipped_catalog():
    """config/voice_commands_v094.json 是桌面端的语音指令目录。"""
    path = REPO / "config" / "voice_commands_v094.json"
    if not path.is_file():
        pytest.skip(f"{path} 不存在")
    data = json.loads(path.read_text(encoding="utf-8"))
    catalog = data.get("commands", data)
    expected = {item["id"]: item["label"] for item in catalog}
    assert VOICE_COMMAND_NAMES == expected


def test_trigger_names_match_the_desktop_ui():
    """web/app.js 的 BASE_PROFILE_TRIGGERS 是桌面界面上显示的那套名字。"""
    source = (REPO / "web" / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const BASE_PROFILE_TRIGGERS=\[(.*?)\];", source, re.S)
    assert block, "web/app.js 里找不到 BASE_PROFILE_TRIGGERS"
    pairs = re.findall(r"key:'(\w+)\.(\w+)'.*?name:'([^']+)'", block.group(1))
    assert pairs, "触发器表解析失败"

    tables = {"zone": ZONE_NAMES, "motion": MOTION_NAMES, "pose": POSE_NAMES}
    for prefix, ident, name in pairs:
        assert tables[prefix].get(ident) == name, f"{prefix}.{ident} 的名字对不上"


@windows_only
def test_every_body_zone_has_a_name():
    """control_kernel 认识的每一块区域都要能翻译，否则详情页会露出英文 id。"""
    from motioncontrol.control_kernel import BODY_ZONES, RUNTIME_BODY_ZONES, ZONE_ALIASES

    for zone in {*BODY_ZONES, *RUNTIME_BODY_ZONES}:
        assert zone in ZONE_NAMES, f"{zone} 没有中文名"
    assert ZONE_RUNTIME_ALIASES == ZONE_ALIASES


def test_every_motion_has_a_name():
    """冲突提示里会出现的每个 id 都要能翻译，否则详情页会露出英文。

    跨两张表查：hands_cross 在冲突规则里算动作，在界面上是姿势，两边都对——
    要断言的是"翻得出来"，不是"它属于哪一类"。

    名字本身不断言相等：motion_conflicts 叫 hands_up「双手过头」，web/app.js 叫
    「双手举过头」。那是桌面端两处源自己的不一致，不该由这条测试来裁决。
    """
    from motioncontrol_shared.motion_conflicts import MOTION_DISPLAY_NAMES

    known = {**MOTION_NAMES, **POSE_NAMES}
    missing = [ident for ident in MOTION_DISPLAY_NAMES if ident not in known]
    assert not missing, f"没有中文名：{missing}"


# --- 动作的说法 --------------------------------------------------------------

@pytest.mark.parametrize("action, expected", [
    ({"type": "gamepad", "target": "X", "behavior": "hold"}, "Xbox X 键 · 持续按住"),
    ({"type": "keyboard", "target": "ENTER", "behavior": "tap"}, "键盘 ENTER · 点按"),
    ({"type": "gamepad_axis", "target": "LS_UP", "behavior": "hold"}, "左摇杆 向上 · 持续按住"),
    ({"type": "mouse_button", "target": "LEFT", "behavior": "tap"}, "鼠标 左键 · 点按"),
    ({"type": "mouse_wheel", "target": "SCROLL_UP", "behavior": "tap"}, "滚轮 向上滚 · 点按"),
    # 系统命令翻成中文：屏幕前的人不该自己在脑子里翻译 OUTPUT.START。
    ({"type": "system", "target": "OUTPUT.START", "behavior": "tap"}, "开始输出 · 点按"),
    # 不认识的照旧原样显示，总比吞掉好。
    ({"type": "system", "target": "FUTURE.THING", "behavior": "tap"},
     "系统命令 FUTURE.THING · 点按"),
    ({"type": "gamepad", "target": ["LB", "LS_UP"], "behavior": "hold"},
     "Xbox LB+LS_UP 键 · 持续按住"),
])
def test_action_wording(action, expected):
    assert describe_action(action) == expected


def test_a_broken_action_does_not_raise():
    """描述用在详情页上，一条坏数据不该让整页打不开。"""
    assert describe_action(None) == "（无效）"
    assert describe_action("nonsense") == "（无效）"


def test_unknown_trigger_falls_back_to_its_id():
    assert trigger_name("zone.somethingNew") == "somethingNew"
    assert trigger_name("没有点号") == "没有点号"


# --- 整份文档 ----------------------------------------------------------------

def test_profile_selection_is_described_per_game():
    doc = canonicalize("profile_selection", {
        "schema": "motioncontrol.profile_selection.v2",
        "selected_id": "generic-xbox",
        "overrides_by_profile": {
            "generic-xbox": {
                "zone.leftHandUpper": {"action": {"type": "gamepad", "target": "X",
                                                  "behavior": "hold"}},
                "motion.march": {"action": {"type": "gamepad_axis", "target": "LS_UP",
                                            "behavior": "hold"}},
            },
        },
    }).data

    result = describe("profile_selection", doc)
    assert result["headline"] == "1 个游戏，共 2 条自定义绑定"
    game = result["games"][0]
    assert game["game_id"] == "generic-xbox"
    names = {group["name"] for group in game["groups"]}
    assert names == {"体感区域", "身体动作"}

    zone = next(g for g in game["groups"] if g["key"] == "zone")["items"][0]
    # 上传的是 zone.leftHandUpper，规范化时并成了 leftHand——手部现在是两块区域，
    # 历史 id 本来也只能通过内核的回退才生效。
    assert zone["trigger"] == "zone.leftHand"
    assert zone["name"] == "左手区"
    assert zone["action"] == "Xbox X 键 · 持续按住"


def test_a_disabled_binding_says_so():
    doc = canonicalize("profile_selection", {
        "schema": "motioncontrol.profile_selection.v2",
        "selected_id": "generic-xbox",
        "overrides_by_profile": {"generic-xbox": {"zone.leftFoot": None}},
    }).data
    item = describe("profile_selection", doc)["games"][0]["groups"][0]["items"][0]
    assert item["disabled"] and item["action"] == "已关闭"


def test_voice_triggers_use_the_command_label():
    doc = canonicalize("profile_selection", {
        "schema": "motioncontrol.profile_selection.v2",
        "selected_id": "generic-xbox",
        "overrides_by_profile": {
            "generic-xbox": {
                "voice.game.profile_slot_01": {"action": {"type": "keyboard", "target": "F",
                                                          "behavior": "tap"}},
            },
        },
    }).data
    item = describe("profile_selection", doc)["games"][0]["groups"][0]["items"][0]
    assert item["name"] == "当前游戏功能1"


def test_motion_and_voice_documents_are_described():
    motions = canonicalize("motion_mappings", {"motions": [
        {"id": "march", "name": "原地踏步", "enabled": True,
         "type": "gamepad_axis", "target": "LS_UP"},
        {"id": "squat", "name": "下蹲", "enabled": False, "type": "gamepad", "target": ""},
    ]}).data
    result = describe("motion_mappings", motions)
    assert result["headline"] == "2 个身体动作，启用 1 个"
    assert result["items"][0]["action"] == "左摇杆 向上"
    assert result["items"][1]["action"] == "未设置输出"

    voice = canonicalize("voice_mappings", {"wake_word": "体感", "mappings": [
        {"phrase": "开始", "type": "keyboard", "target": "ENTER", "synonyms": ["启动"]},
    ]}).data
    result = describe("voice_mappings", voice)
    assert result["headline"] == "1 条口令"
    assert result["items"][0]["synonyms"] == ["启动"]
    # 唤醒词不在可分享的配置里，预览里也不该出现——说了反而像在告诉人
    # "装了就会变成这个"。
    assert "唤醒词" not in result["headline"]
    assert "wake_word" not in result


def test_an_unknown_document_type_is_refused():
    with pytest.raises(ValueError, match="unknown document type"):
        describe("head_profile", {})


def test_the_real_configuration_describes_without_error():
    """本机真实配置——手写的小样本描述得再好也说明不了什么。"""
    path = REPO / "config" / "game_profile_selection.json"
    if not path.is_file():
        pytest.skip("本机没有真实配置")
    doc = canonicalize("profile_selection",
                       json.loads(path.read_text(encoding="utf-8"))).data
    result = describe("profile_selection", doc)
    assert result["games"], "真实配置描述出来是空的"
    for game in result["games"]:
        for group in game["groups"]:
            for item in group["items"]:
                assert item["name"], f"{item['trigger']} 没有名字"
                assert item["action"], f"{item['trigger']} 没有动作说明"


# --- 合并之前存下来的文档 ------------------------------------------------------

def _legacy_zone_doc(upper_target: str, lower_target: str) -> dict:
    """手工构造一份"合并之前"的文档。

    不能用 canonicalize()：它现在就会把历史 id 并掉，而这里要测的恰恰是那些在
    合并之前就存进去、并且因为版本不可变而永远保持旧形态的文档。
    """
    def binding(target):
        return {"action": {"type": "gamepad", "target": target, "behavior": "hold"}}
    return {
        "schema": "motioncontrol.profile_selection.v2",
        "selected_id": "generic-xbox",
        "overrides_by_profile": {"generic-xbox": {
            "zone.rightHandUpper": binding(upper_target),
            "zone.rightHandLower": binding(lower_target),
        }},
    }


def test_a_shadowed_legacy_binding_is_called_out():
    """上区和下区配了不同的键时，下区那条从来不触发——必须说出来。

    不说的话，用户会盯着一条永远不响应的绑定找原因。这不是假设：本机真实配置里
    右手上区是 B、下区是 A，那个 A 从来没生效过。
    """
    result = describe("profile_selection", _legacy_zone_doc("B", "A"))
    items = {i["name"]: i for i in result["games"][0]["groups"][0]["items"]}

    assert items["右手下区"]["shadowed_by"] == "右手上区"
    assert items["右手下区"]["shadowed_matters"] is True
    assert items["右手上区"]["shadows"] == ["右手下区"]
    # 两条都标出运行时落到哪块区域。
    assert items["右手上区"]["runtime_zone"] == "右手区"


def test_two_identical_legacy_bindings_are_not_alarming():
    """配的是同一个键时，覆盖不改变任何行为，不该报成问题。"""
    result = describe("profile_selection", _legacy_zone_doc("B", "B"))
    items = {i["name"]: i for i in result["games"][0]["groups"][0]["items"]}
    assert items["右手下区"]["shadowed_by"] == "右手上区"
    assert items["右手下区"]["shadowed_matters"] is False


def test_a_merged_document_has_nothing_to_annotate():
    """走过新版规范化的文档只剩两块手部区域，没有覆盖可言。"""
    doc = canonicalize("profile_selection", _legacy_zone_doc("B", "A")).data
    item = describe("profile_selection", doc)["games"][0]["groups"][0]["items"][0]
    assert item["name"] == "右手区"
    assert "shadowed_by" not in item and "runtime_zone" not in item
    # 留下来的是内核本来就在用的那条。
    assert item["action"] == "Xbox B 键 · 持续按住"
