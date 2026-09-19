"""玩家自己添加的游戏。

内置目录只有两百个。以前搜不到的游戏只能占用别人的坑位：界面上一直显示着错的
游戏名，而且第二个未收录的游戏就没地方放了。

这些测试钉住的是几件"错了不会报错、只会悄悄丢东西"的事：绑定按 id 存所以改名
不能弄丢它、删游戏要连覆盖一起删干净、删掉正选中的那个要退回通用档而不是留一
个指向虚空的选择。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol.game_profiles import GameProfileStore  # noqa: E402


@pytest.fixture()
def store(isolated_user_data):
    return GameProfileStore(ROOT)


def test_a_custom_game_shows_its_own_name(store):
    """以前只能借用别人的坑，界面上就一直显示着错的游戏名。"""
    game = store.add_custom_game("黑神话悟空")
    store.select(game["id"])
    profile = store.effective_profile()
    assert profile["name"] == "黑神话悟空"
    assert profile["selected_id"] == game["id"]
    assert profile["source"] == "custom"


def test_two_custom_games_do_not_share_one_slot(store):
    """第二个未收录的游戏以前没地方放。"""
    first = store.add_custom_game("游戏甲")
    second = store.add_custom_game("游戏乙")
    assert first["id"] != second["id"]

    store.select(first["id"])
    store.set_overrides({"zone.leftHandUpper": {
        "action": {"type": "mouse_button", "target": "LEFT"}}})
    store.select(second["id"])
    store.set_overrides({"zone.leftHandUpper": {
        "action": {"type": "mouse_button", "target": "RIGHT"}}})

    store.select(first["id"])
    action = store.effective_profile()["bindings"]["zones"]["leftHandUpper"]["action"]
    assert action["target"] == "LEFT", "两个自定义游戏的按键互相覆盖了"


def test_renaming_keeps_the_bindings(store):
    """绑定按 id 存，改名不该让调好的按键失效。"""
    game = store.add_custom_game("打错的名字")
    store.select(game["id"])
    store.set_overrides({"zone.rightHandLower": {
        "action": {"type": "mouse_button", "target": "LEFT"}}})

    store.rename_custom_game(game["id"], "正确的名字")
    profile = store.effective_profile()
    assert profile["name"] == "正确的名字"
    assert profile["bindings"]["zones"]["rightHandLower"]["action"]["type"] == "mouse_button"


def test_removing_takes_its_overrides_with_it(store):
    """留下孤儿覆盖没有意义：那个 id 再也不会出现，却会一直被导出带走。"""
    game = store.add_custom_game("要删掉的")
    store.select(game["id"])
    store.set_overrides({"zone.leftHandUpper": {
        "action": {"type": "mouse_button", "target": "LEFT"}}})
    assert game["id"] in store._selection["overrides_by_profile"]

    store.remove_custom_game(game["id"])
    assert game["id"] not in store._selection["overrides_by_profile"]


def test_removing_the_selected_one_falls_back(store):
    """否则界面会指向一个已经不存在的游戏。"""
    game = store.add_custom_game("正在用的")
    store.select(game["id"])
    store.remove_custom_game(game["id"])
    assert store.effective_profile()["selected_id"] == "generic-xbox"


def test_it_survives_a_restart(store, isolated_user_data):
    game = store.add_custom_game("重启也要在", appid="2358720")
    store.select(game["id"])

    again = GameProfileStore(ROOT)
    names = [g["name"] for g in again.list_games()["games"] if g.get("source") == "custom"]
    assert "重启也要在" in names
    assert again.effective_profile()["name"] == "重启也要在"


@pytest.mark.parametrize("raw, expected", [
    ("2358720", "2358720"),
    ("  413150 ", "413150"),
    # 玩家会把整个商店链接粘进来。与其拒绝他，不如把数字捞出来——被打回去的人
    # 多半就不填了，而 AppID 是这条记录唯一能跨机器对上号的东西。
    ("https://store.steampowered.com/app/413150/Stardew_Valley/", "413150"),
    ("", ""),
    (None, ""),
    ("不是数字", ""),
])
def test_appid_is_taken_from_whatever_was_pasted(store, raw, expected):
    game = store.add_custom_game(f"游戏 {raw!r}", appid=raw)
    assert game["appid"] == expected


def test_duplicate_names_are_refused(store):
    store.add_custom_game("同一个名字")
    with pytest.raises(ValueError):
        store.add_custom_game("同一个名字")


@pytest.mark.parametrize("name", ["", "   ", "x" * 81])
def test_a_bad_name_is_refused(store, name):
    with pytest.raises(ValueError):
        store.add_custom_game(name)


def test_custom_games_come_first_in_the_list(store):
    """会去翻这个列表的人，多半就是为了找自己加的那个。"""
    store.add_custom_game("我加的")
    games = store.list_games()["games"]
    assert games[0]["name"] == "我加的"
    assert games[0]["source"] == "custom"


def test_a_broken_file_does_not_brick_the_controller(store, isolated_user_data):
    """一份坏掉的自定义游戏文件，不该让整个控制器起不来。"""
    (isolated_user_data / "custom_games.json").write_text("{ 这不是 json", encoding="utf-8")
    again = GameProfileStore(ROOT)
    assert again.list_games()["custom_count"] == 0
    assert again.effective_profile()["selected_id"]


def test_a_missing_base_profile_falls_back(store):
    """底档哪天不见了，也不能让一个自定义条目把控制器卡死。"""
    game = store.add_custom_game("底档会消失的", base="generic-xbox")
    store._custom["games"][0]["base"] = "根本不存在的档"
    profile = store.get_profile(game["id"])
    assert profile["name"] == "底档会消失的"
    assert profile["bindings"], "退回通用档之后仍然要有可用的绑定"
