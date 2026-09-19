"""界面上得真的能加游戏。

后端做完了：增、删、改名、按 id 存绑定、从粘贴的商店链接里捞 AppID，十九条测试
都过。但「本游戏」那一页上一个入口都没有——功能只有接口，玩家碰不到。这种缺口
比报错难发现：跑测试全绿，读代码也全绿，只有真的去点那一页才知道。

所以这里钉的是页面和脚本，不是内核：那三个接口叫不叫得出来，按钮在不在，接的是
不是对的那一个。
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def page():
    return (ROOT / "web" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app():
    return (ROOT / "web" / "app.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("element", [
    "customGameName",      # 游戏名
    "customGameAppid",     # AppID，选填
    "customGameAddBtn",
    "customGameRenameBtn",
    "customGameRemoveBtn",
    "customGameActions",   # 改名/删除那一行，只在自己加的游戏上出现
])
def test_the_page_has_the_control(page, element):
    assert f'id="{element}"' in page, f"「本游戏」页上没有 {element}"


def test_the_add_box_says_why_it_is_there(page):
    """搜不到的人得知道还有这条路，否则他只会以为这个游戏不支持。"""
    box = page.split('id="customGameBox"', 1)[1].split("</details>", 1)[0]
    assert "搜不到" in box


@pytest.mark.parametrize("route", [
    "/api/game-profiles/custom/add",
    "/api/game-profiles/custom/rename",
    "/api/game-profiles/custom/remove",
])
def test_the_script_calls_the_route(app, route):
    assert route in app, f"页面从来不调用 {route}"


@pytest.mark.parametrize("button, handler", [
    ("customGameAddBtn", "addCustomGame"),
    ("customGameRenameBtn", "renameCustomGame"),
    ("customGameRemoveBtn", "removeCustomGame"),
])
def test_the_button_is_wired_to_its_handler(app, button, handler):
    assert re.search(rf"bind\('{button}',\s*{handler}\)", app), f"{button} 没接上 {handler}"


def test_the_server_serves_those_three_routes():
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    for route in ("/api/game-profiles/custom/add",
                  "/api/game-profiles/custom/rename",
                  "/api/game-profiles/custom/remove"):
        assert route in server


def test_removing_is_confirmed_first(app):
    """删掉会连按键映射一起删，恢复不了，所以不能一击必杀。"""
    body = app.split("async function removeCustomGame", 1)[1].split("async function", 1)[0]
    assert "confirm(" in body


def test_a_custom_game_is_not_labelled_experimental(app):
    """「实验」说的是那两百个自动生成、没人试过的配置。

    自己刚建的空白档标成「实验」，会让人以为是软件给了个半成品，而不是等着他
    自己调的东西。
    """
    assert "我加的" in app or "我自己加的" in app
