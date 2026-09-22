"""一个游戏的全部配置，合成一份。

以前一份「我的 GTA5 配置」要分成两个包发出去，别人也要分两次装——而按键映射和身体
动作本来就是同一件事：都是"在这个游戏里，我这么玩"。

里面**没有**唤醒词、急停口令，也没有任何跟机器走的东西。那些是发布者个人的，装到
别人机器上只会把人家原来的换掉（见 tests/test_pose_voice_and_triggers.py）。
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest

from motioncontrol_shared.canonical import canonicalize
from motioncontrol_shared.describe import describe
from motioncontrol_shared.profile_versions import GAME_BUNDLE_SCHEMA

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


def bundle(**overrides):
    doc = {
        "game_id": "steam-3240220-grand-theft-auto-v-enhanced",
        "overrides": {
            "zone.leftHand": {"action": {"type": "gamepad", "target": "Y", "behavior": "hold"}},
        },
        "motions": [
            {"id": "march", "name": "原地踏步", "enabled": True,
             "type": "gamepad_axis", "target": "LS_UP"},
        ],
    }
    doc.update(overrides)
    return doc


def test_a_bundle_carries_one_game_its_keys_and_its_motions():
    data = canonicalize("game_bundle", bundle()).data
    assert data["schema"] == GAME_BUNDLE_SCHEMA
    assert data["game_id"] == "steam-3240220-grand-theft-auto-v-enhanced"
    assert data["overrides"]["zone.leftHand"]["action"]["target"] == "Y"
    assert [motion["id"] for motion in data["motions"]] == ["march"]


def test_a_bundle_must_say_which_game_it_is_for():
    """一份不说是哪个游戏的方案，收到的人无从下手。"""
    with pytest.raises(ValueError, match="game_id"):
        canonicalize("game_bundle", bundle(game_id=""))


def test_a_bundle_never_carries_personal_settings():
    """唤醒词是发布者自己的习惯。混进来就会把接收方的换掉。"""
    data = canonicalize("game_bundle", bundle(wake_word="体感", emergency_stop_phrases=["停"])).data
    assert set(data) == {"schema", "game_id", "overrides", "motions"}


def test_motions_go_through_the_same_rules_as_before():
    """开合跳和双手过头顶不能同时映射。合成一份不是放宽规则的借口。"""
    with pytest.raises(ValueError):
        canonicalize("game_bundle", bundle(motions=[
            {"id": "jumping_jack", "name": "开合跳", "enabled": True,
             "type": "gamepad", "target": "A"},
            {"id": "hands_up", "name": "双手过头", "enabled": True,
             "type": "gamepad", "target": "Y"},
        ]))


def test_bindings_go_through_the_same_rules_as_before():
    with pytest.raises(ValueError):
        canonicalize("game_bundle", bundle(overrides={
            "zone.leftHand": {"action": {"type": "gamepad", "target": "Z"}}}))


def test_two_bundles_with_the_same_content_hash_the_same():
    """键的顺序不一样不该算成两个版本。"""
    first = canonicalize("game_bundle", bundle())
    second = canonicalize("game_bundle", {
        "motions": bundle()["motions"],
        "overrides": bundle()["overrides"],
        "game_id": bundle()["game_id"],
    })
    assert first.sha256 == second.sha256


def test_a_bundle_with_no_motions_is_fine():
    """只改了按键、没碰身体动作的人也该能分享。"""
    data = canonicalize("game_bundle", bundle(motions=[])).data
    assert data["motions"] == []


def test_the_summary_shows_keys_and_motions_together():
    data = canonicalize("game_bundle", bundle()).data
    summary = describe("game_bundle", data)
    assert summary["kind"] == "game_bundle"
    assert "1 条按键绑定" in summary["headline"] and "1 个身体动作" in summary["headline"]
    assert summary["game_id"] == bundle()["game_id"]
    assert summary["motions"][0]["action"] == "左摇杆 向上"


def test_the_cloud_accepts_the_new_type():
    from cloud.app.schemas import DocType

    assert "game_bundle" in typing.get_args(DocType)


def test_the_old_types_still_work():
    """已经传上去的那些还得能下载和安装。改成开不了的废文件是最坏的一种升级。"""
    from cloud.app.schemas import DocType

    assert {"profile_selection", "motion_mappings", "voice_mappings"} <= set(typing.get_args(DocType))


def test_installing_a_bundle_goes_through_the_normal_entry_points():
    """不能为了"一次装两样"另开一条写盘的路——校验和冲突检查都在那两个入口里。"""
    start = SERVER.index("def _install_game_bundle")
    block = SERVER[start:start + 1400]
    assert "PROFILES.set_overrides" in block
    assert "save_motion_config" in block
    assert "KERNEL.configure_bindings" in block, "装完没有把新绑定推进内核"


def test_the_desktop_knows_how_to_install_a_bundle():
    assert 'elif remote.doc_type == "game_bundle":' in SERVER
    assert "_install_game_bundle(remote.document)" in SERVER


def test_the_preview_says_which_game_a_bundle_is_for():
    """装之前要能看清楚它会动哪个游戏。"""
    start = SERVER.index('"games": sorted(remote.document.get("overrides_by_profile"')
    assert 'game_bundle' in SERVER[start:start + 400]


def test_the_upload_page_can_combine_the_two_files():
    page = (ROOT / "cloud" / "web" / "src" / "pages" / "MyConfigs.vue").read_text(encoding="utf-8")
    assert "combineIntoBundle" in page
    assert GAME_BUNDLE_SCHEMA in page, "合成出来的文档要带上 schema，否则服务端认不出版本"
    assert "bundleGames" in page, "要让人选是哪个游戏——一份选择文件里装着所有游戏"
