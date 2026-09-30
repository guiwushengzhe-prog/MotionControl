"""真在浏览器里点的界面测试。

别的界面测试是在 app.js / index.html 的源码里找字：改一句话它们就红，界面真坏了
（按钮点了没反应、脚本报错整页白掉）它们反而是绿的。这里起一个真的服务、开一个
真的浏览器，照人的用法去点，看页面上出来的是什么。

- 服务用一个空的临时用户目录，端口临时挑空闲的，和正在用的程序互不相干。
- 「有人站在镜头前、做了动作、控制开着」这些状态没法在测试机上真做出来，就拦下
  服务的状态接口，在真实返回上改几个字段再交给页面——页面这一侧的逻辑是真跑的。
- 没装 playwright、或者找不到能用的浏览器，整个文件跳过，不影响其它测试。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="桌面程序只在 Windows 上跑")

# 教学第一次打开会自动弹，压暗整页、挡住点击。标成看过，测试照常点。
TUTORIAL_SEEN = json.dumps({"done": [], "skipped": [], "seen": 1, "offered": True})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str, timeout: float = 2.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    user = tmp_path_factory.mktemp("ui-user")
    port, admin = _free_port(), _free_port()
    env = {**os.environ, "MOTIONCONTROL_USER_DIR": str(user), "PYTHONUTF8": "1"}
    log_path = user / "server.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "server.py"), "--host", "127.0.0.1",
         "--port", str(port), "--admin-port", str(admin), "--no-browser"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{admin}"
    deadline = time.time() + 120
    while True:
        if proc.poll() is not None:
            log.close()
            pytest.fail("服务没起来：\n" + log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
        try:
            _get(base + "/api/kernel/status")
            break
        except Exception:
            if time.time() > deadline:
                proc.kill()
                log.close()
                pytest.fail("服务两分钟还没响应")
            time.sleep(0.5)
    yield base
    try:
        _get(base + "/api/shutdown")
    except Exception:
        pass
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            chosen = p.chromium.launch()
        except Exception:
            try:
                # 没下载 Playwright 自带的浏览器时，用电脑上装的 Chrome。
                chosen = p.chromium.launch(channel="chrome")
            except Exception as exc:
                pytest.skip(f"没有能用的浏览器：{exc}")
        yield chosen
        chosen.close()


@pytest.fixture
def ui(browser, server):
    context = browser.new_context(viewport={"width": 1280, "height": 820}, color_scheme="dark")
    # 第一次用时的指引气泡也标成看过，免得它挡在按钮旁边；它自己另有一条测试。
    tips_seen = json.dumps(json.dumps(["help", "gameMenu"]))
    context.add_init_script(
        f"try{{localStorage.setItem('motioncontrol_tutorial_v2', {json.dumps(TUTORIAL_SEEN)});"
        f"localStorage.setItem('motioncontrol_tips_seen', {tips_seen})}}catch{{}}")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    # 接口返回 4xx 时浏览器会记一条「Failed to load resource」，那是服务在说「不行」，
    # 不是页面脚本坏了。这里只收脚本自己的错。
    page.on("console", lambda msg: errors.append(f"console: {msg.text}")
            if msg.type == "error" and "Failed to load resource" not in msg.text else None)
    page.goto(server + "/")
    page.wait_for_function("document.querySelector('#currentGameName').textContent !== '正在读取…'")
    yield SimpleNamespace(page=page, errors=errors, base=server)
    context.close()


def _go(page, view: str) -> None:
    page.click(f'nav [data-view="{view}"]')
    page.wait_for_selector(f'[data-panel="{view}"]:not([hidden])')


def test_the_page_runs_without_script_errors(ui):
    ui.page.wait_for_timeout(1500)  # 让轮询跑几轮
    assert ui.errors == []
    tabs = ui.page.locator("nav [data-view]")
    assert tabs.count() == 3, "三个页签：开始、本游戏、设置"
    assert ui.page.locator('[data-view="range"]').count() == 0, "动作测试已经并进开始页"


def test_each_tab_shows_its_own_page(ui):
    for view in ("games", "devices", "play"):
        _go(ui.page, view)
        visible = ui.page.eval_on_selector_all("[data-panel]", "els => els.filter(el => !el.hidden).map(el => el.dataset.panel)")
        assert visible == [view]
    assert ui.errors == []


def test_day_and_night_can_be_forced_and_are_remembered(ui):
    page = ui.page
    page.click("#helpBtn")
    page.click('[data-theme-choice="light"]')
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.reload()
    page.wait_for_selector("#helpBtn")
    assert page.evaluate("document.documentElement.dataset.theme") == "light", "选了白天，刷新后还要是白天"
    page.click("#helpBtn")
    page.click('[data-theme-choice="auto"]')
    assert page.evaluate("document.documentElement.dataset.theme || ''") == ""
    assert page.evaluate("localStorage.getItem('motioncontrol_theme')") is None


def test_advanced_options_stay_folded_until_asked_and_changes_save(ui):
    page = ui.page
    _go(page, "games")
    page.wait_for_selector('.binding-row[data-trigger="zone.leftHand"]')
    row = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    assert row.locator(".binding-adv").is_hidden(), "高级选项默认收着"
    # 先绑一个手柄键（输出类型和键位拼在一个控件里），再改高级选项。
    row.locator("select.binding-type").select_option("gamepad")
    row.locator(".binding-target-box select").select_option("Y")
    row.locator(".binding-more").click()
    assert row.locator(".binding-adv").is_visible()
    box = row.locator(".zone-with-motion-box")
    assert not box.is_checked(), "左手区默认扫过不按"
    box.click()
    # 改过默认值的，名字旁边挂一个小标签，不用展开也看得到。
    assert "扫过也按" in row.locator(".binding-tags").inner_text()
    page.wait_for_function("document.querySelector('#profileSaveStatus').textContent.includes('已保存')", timeout=10000)
    page.reload()
    _go(page, "games")
    page.wait_for_selector('.binding-row[data-trigger="zone.leftHand"]')
    saved = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    assert saved.locator(".zone-with-motion-box").is_checked(), "刷新以后改动还在：真存进去了"
    assert saved.locator(".binding-target-box select").input_value() == "Y"
    assert "扫过也按" in saved.locator(".binding-tags").inner_text()
    assert ui.errors == []


def test_the_mapping_tabs_and_the_library(ui):
    page = ui.page
    _go(page, "games")
    page.click('#mapTabs [data-tab="body"]')
    assert page.locator('.binding-group[data-group="body"]').is_visible()
    assert page.locator('.binding-group[data-group="zones"]').is_hidden()
    assert page.locator("#poseLibraryPanel").is_visible(), "动作库放在身体动作下面"
    page.click("#libToggle")
    assert page.locator("#libraryBody").is_hidden()
    page.reload()
    _go(page, "games")
    page.click('#mapTabs [data-tab="body"]')
    assert page.locator("#libraryBody").is_hidden(), "收起来的动作库刷新后还收着"
    page.click("#libToggle")
    page.click('#mapTabs [data-tab="voice"]')
    assert page.locator("#poseLibraryPanel").is_hidden()
    assert ui.errors == []


def test_changing_game_is_one_searchable_list(ui):
    page = ui.page
    _go(page, "games")
    assert page.locator("#gamePicker").is_hidden()
    page.click("#switchGameBtn")
    page.wait_for_selector("#profileResults .game-result")
    assert page.locator("#profileResults .game-result").count() > 10
    page.fill("#profileSearch", "Apex")
    page.wait_for_function("[...document.querySelectorAll('#profileResults .game-result')].every(b => /apex/i.test(b.textContent))")
    first = page.locator("#profileResults .game-result").first
    name = first.locator("span").inner_text()
    first.click()
    page.wait_for_function(f"document.querySelector('#currentGameName').textContent === {json.dumps(name)}")
    assert page.locator("#gamePicker").is_hidden(), "换好就收起"
    assert ui.errors == []


def test_settings_sections_and_contextual_groups(ui):
    page = ui.page
    _go(page, "devices")
    for pane in ("view", "body", "voice", "macro", "lab", "devices"):
        page.click(f'#settingsNav [data-pane="{pane}"]')
        visible = page.eval_on_selector_all(".settings-pane", "els => els.filter(el => !el.hidden).map(el => el.dataset.pane)")
        assert visible == [pane]
    # 手机那一组：没选手机、手机也没连，就不出现。
    assert page.locator("#phoneGroup").is_hidden()
    page.select_option("#poseSource", "phone")
    assert page.locator("#phoneGroup").is_visible()
    page.select_option("#poseSource", "computer")
    # 旧版上下视角只给还开着的人看。
    page.click('#settingsNav [data-pane="view"]')
    assert page.locator("#legacyVertical").is_hidden()
    assert ui.errors == []


def _fake_live_state(page) -> None:
    """在真实返回上改几个字段：有人、右手在框里、刚踏了一步、摄像头开着。"""
    def point(x, y):
        return {"x": x, "y": y, "score": 0.99}
    pose = {name: point(x, y) for name, (x, y) in {
        "nose": (.5, .22), "left_eye": (.485, .2), "right_eye": (.515, .2), "left_ear": (.47, .21), "right_ear": (.53, .21),
        "left_shoulder": (.43, .34), "right_shoulder": (.57, .34), "left_elbow": (.4, .47), "right_elbow": (.6, .47),
        "left_wrist": (.39, .58), "right_wrist": (.61, .58), "left_hip": (.46, .62), "right_hip": (.54, .62),
        "left_knee": (.46, .78), "right_knee": (.54, .78), "left_ankle": (.46, .93), "right_ankle": (.54, .93),
    }.items()}

    def kernel(route):
        response = route.fetch()
        data = response.json()
        k = data.get("kernel", data)
        now = float(k.get("now") or 0)
        k["pose"] = pose
        k["zones"] = {"rightHand": {"rect": {"x1": .2, "y1": .3, "x2": .38, "y2": .55}, "pressed": True},
                      "leftHand": {"rect": {"x1": .62, "y1": .3, "x2": .8, "y2": .55}}}
        k["motions"] = []
        k["recent_triggers"] = [{"trigger": "motion.march", "at": now - 0.3, "action": {"type": "keyboard", "target": "W"}}]
        k["head"] = {**k.get("head", {}), "output_x": 20, "sensitivity_x": 58, "calibrated": True,
                     "horizontal_calibrated": True, "enabled": True}
        data["body_mode"] = "computer"
        data["camera"] = {**(data.get("camera") or {}), "running": True}
        route.fulfill(response=response, json=data)

    def performance(route):
        response = route.fetch()
        data = response.json()
        data["inference_fps"] = 29.8
        route.fulfill(response=response, json=data)

    page.route("**/api/kernel/status", kernel)
    page.route("**/api/performance", performance)


def test_the_start_page_follows_what_is_happening(ui):
    page = ui.page
    # 没人没画面：画面中间一句话告诉人该干什么。
    assert page.locator("#hint").is_visible()
    _fake_live_state(page)
    page.wait_for_function("document.querySelector('#hint').hidden")
    page.wait_for_function("!document.querySelector('#fpsHud').hidden", timeout=6000)
    assert page.locator("#fpsHud").inner_text() == "30 帧/秒", "画面角上只写帧数"
    page.wait_for_function("!document.querySelector('#rangeHit').hidden")
    assert "→" in page.locator("#rangeHitWhat").inner_text(), "触发了，画面上大字写按了什么"
    assert "游戏控制没开" in page.locator("#rangeHitWhen").inner_text()
    assert page.locator("#recentCard").is_visible(), "有触发才列最近触发"
    assert page.locator('.checklist [data-step="pose"]').get_attribute("class").find("done") >= 0
    assert page.locator('.zone[data-zone="rightHand"]').get_attribute("class").find("active") >= 0
    assert ui.errors == []


def test_nothing_scrolls_sideways_on_a_narrow_window(ui):
    page = ui.page
    page.set_viewport_size({"width": 390, "height": 844})
    for view in ("play", "games", "devices"):
        _go(page, view)
        width = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
        assert width[0] <= width[1] + 1, f"{view} 页在窄屏上横向溢出：{width}"


def test_the_tutorial_opens_from_the_help_menu(ui):
    page = ui.page
    page.click("#helpBtn")
    page.click("#tutorialBtn")
    page.wait_for_selector("#tour:not([hidden])")
    assert page.locator("#tourSay").inner_text().strip(), "教学卡片上有一句要做的事"
    page.click("#tourCloseBtn")
    assert page.locator("#tour").is_hidden()
    assert ui.errors == []


def test_first_use_tips_point_at_the_small_menus(browser, server):
    """「？」和「⋯」里放着重要的东西（新手教学、恢复默认按键、管理员权限），第一次用时指一下。"""
    context = browser.new_context(viewport={"width": 1280, "height": 820})
    context.add_init_script(
        f"try{{localStorage.setItem('motioncontrol_tutorial_v2', {json.dumps(TUTORIAL_SEEN)})}}catch{{}}")
    page = context.new_page()
    try:
        page.goto(server + "/")
        tip = page.locator(".coach-tip")
        tip.wait_for(timeout=10000)
        assert "新手教学" in tip.inner_text()
        page.click("#helpBtn")  # 点了它指的那个按钮，气泡就走
        assert page.locator(".coach-tip").count() == 0
        page.keyboard.press("Escape")
        page.click('nav [data-view="games"]')
        page.locator(".coach-tip").wait_for(timeout=5000)
        assert "恢复默认按键" in page.locator(".coach-tip").inner_text()
        page.click(".coach-tip .btn")
        page.reload()
        page.click('nav [data-view="games"]')
        page.wait_for_timeout(1500)
        assert page.locator(".coach-tip").count() == 0, "看过一次就不再出现"
    finally:
        context.close()
