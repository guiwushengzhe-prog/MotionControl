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
    # 空目录现在默认开电脑来源；界面测试固定等手机，避免抢占真实摄像头和麦克风。
    (user / "general_settings.json").write_text(
        json.dumps({"body_source": "phone", "audio_source": "phone"}), encoding="utf-8")
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


@pytest.fixture
def mode_ui(ui):
    """每条模式测试用自己的游戏，结束后还原共用测试服务。"""
    page = ui.page
    selected = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]["selected_id"]
    made = page.request.post(ui.base + "/api/game-profiles/custom/add", data={"name": "界面模式测试"}).json()["game"]
    page.request.post(ui.base + "/api/game-profiles/select", data={"id": made["id"]})
    page.reload()
    page.wait_for_function("document.querySelector('#currentGameName').textContent === '界面模式测试'")
    _go(page, "games")
    yield ui, made["id"]
    page.request.post(ui.base + "/api/game-profiles/custom/remove", data={"id": made["id"]})
    page.request.post(ui.base + "/api/game-profiles/select", data={"id": selected})


def _new_mode(page, name):
    page.once("dialog", lambda dialog: dialog.accept(name))
    page.click("#addModeBtn")
    page.wait_for_function("name => document.querySelector('#currentModeName').textContent === name && document.querySelector('#modeTabs [aria-selected=true]').textContent.includes(name) && document.querySelector('#modeEditStatus').hidden", arg=name)
    return page.locator('#modeTabs [aria-selected="true"]').get_attribute("data-mode-id")


def _set_mode_key(page, key):
    row = page.locator('.binding-row[data-trigger="zone.rightHand"]')
    row.locator("select.binding-type").select_option("keyboard")
    with page.expect_response(lambda response: response.url.endswith("/api/game-profiles/overrides") and response.request.method == "POST"):
        row.locator(".binding-target").press(key)
    page.wait_for_function("document.querySelector('#profileSaveStatus').textContent === '已保存'")


def test_modes_create_copy_select_use_and_reload(mode_ui):
    ui, game_id = mode_ui
    page = ui.page
    default = page.request.get(ui.base + "/api/game-profiles/selected?mode=DEFAULT").json()["profile"]["bindings"]
    drive = _new_mode(page, "开车")
    _set_mode_key(page, "F")
    assert page.locator("#currentModeName").inner_text() == "开车", "新建模式立即使用"
    ride = _new_mode(page, "骑车")
    assert page.locator('.binding-row[data-trigger="zone.rightHand"] .binding-target').input_value() == "F", "复制当前编辑模式"
    _set_mode_key(page, "E")
    page.click(f'#modeTabs [data-mode-id="{drive}"]')
    page.wait_for_function("document.querySelector('.binding-row[data-trigger=\"zone.rightHand\"] .binding-target').value === 'F'")
    assert page.request.get(ui.base + "/api/kernel/status").json()["kernel"]["game_mode"]["id"] == drive
    page.click('#modeTabs [data-mode-id="DEFAULT"]')
    page.wait_for_function("document.querySelector('#modeTabs [data-mode-id=DEFAULT]').getAttribute('aria-selected') === 'true'")
    assert page.request.get(ui.base + "/api/game-profiles/selected?mode=DEFAULT").json()["profile"]["bindings"] == default
    _go(page, "play")
    page.click("#playModeBtn")
    page.wait_for_function("document.querySelector('#currentModeName').textContent === '开车'")
    assert page.locator("#runMode").text_content() == "模式：开车"
    assert page.request.get(ui.base + "/api/kernel/status").json()["kernel"]["game_mode"]["id"] == drive
    page.click("#playModeBtn")
    page.wait_for_function("document.querySelector('#currentModeName').textContent === '骑车'")
    page.reload()
    page.wait_for_function("document.querySelector('#currentGameName').textContent === '界面模式测试'")
    _go(page, "games")
    assert page.locator(f'#modeTabs [data-mode-id="{ride}"]').get_attribute("aria-selected") == "true"
    assert page.locator('#modeTabs [role="tab"]').count() == 3
    assert page.locator("#currentModeName").inner_text() == "骑车", "网页刷新继续显示并使用当前模式"
    assert page.locator('.binding-row[data-trigger="zone.rightHand"] .binding-target').input_value() == "E"
    # 换游戏再回来相当于运行状态重置，不会从上次的骑车模式继续。
    page.request.post(ui.base + "/api/game-profiles/select", data={"id": "generic-xbox"})
    page.request.post(ui.base + "/api/game-profiles/select", data={"id": game_id})
    page.wait_for_function("document.querySelector('#currentModeName').textContent === '默认'")
    assert ui.errors == []


def test_delete_motion_mapping_is_persistent_and_only_affects_selected_mode(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    default = page.request.get(ui.base + '/api/game-profiles/selected?mode=DEFAULT').json()['profile']['bindings']
    mode_id = _new_mode(page, '步行')
    page.click('#mapTabs [data-tab="body"]')
    row = page.locator('.binding-row[data-trigger="motion.march"]')
    row.locator('.binding-remove').click()
    sync_api.expect(row).to_have_count(0)
    deleted = page.request.get(ui.base + '/api/game-profiles/selected?mode=' + mode_id).json()['profile']
    assert deleted['overrides']['motion.march'] is None
    assert deleted['bindings']['motions']['march'] == {'disabled': True}
    assert page.request.get(ui.base + '/api/game-profiles/selected?mode=DEFAULT').json()['profile']['bindings'] == default
    card = page.locator('.pose-library-item[data-id="march"]')
    sync_api.expect(card).to_be_visible()
    sync_api.expect(card.locator('.custom-pose-key')).to_have_text('加到映射')
    page.reload()
    page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    _go(page, 'games')
    page.click(f'#modeTabs [data-mode-id="{mode_id}"]')
    page.wait_for_function("id => document.querySelector('#modeTabs [aria-selected=true]').dataset.modeId === id", arg=mode_id)
    page.click('#mapTabs [data-tab="body"]')
    sync_api.expect(row).to_have_count(0)
    card.locator('.custom-pose-key').click()
    sync_api.expect(row).to_be_visible()
    row.locator('.binding-type').select_option('keyboard')
    with page.expect_response(lambda response: response.url.endswith('/api/game-profiles/overrides') and response.request.method == 'POST'):
        row.locator('.binding-target').press('W')
    page.wait_for_function("profileSaveStatus.textContent === '已保存'")
    saved = page.request.get(ui.base + '/api/game-profiles/selected?mode=' + mode_id).json()['profile']
    assert saved['bindings']['motions']['march']['action']['target'] == 'W'
    assert ui.errors == []


def test_mode_targets_shared_voice_rename_delete_and_reset(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    mode_id = _new_mode(page, "开车")
    _set_mode_key(page, "F")
    page.click("#gameMenuBtn")
    page.click("#resetProfileBindingsBtn")
    page.wait_for_function("document.querySelector('.binding-row[data-trigger=\"zone.rightHand\"] .binding-type').value === 'gamepad'")
    row = page.locator('.binding-row[data-trigger="zone.rightHand"]')
    with page.expect_response(lambda response: response.url.endswith("/api/game-profiles/overrides") and response.request.method == "POST"):
        row.locator("select.binding-type").select_option("mode")
        row.locator(".binding-target").select_option(mode_id)
    assert row.locator(".binding-behavior").inner_text() == "切换一次"
    page.click("#modeMenuBtn")
    page.once("dialog", lambda dialog: dialog.accept("驾驶"))
    page.click("#renameModeBtn")
    page.wait_for_function("currentModeName.textContent === '驾驶' && modeEditStatus.hidden")
    sync_api.expect(row.locator(".binding-target")).to_contain_text("切到 驾驶")
    _go(page, "devices")
    page.click('[data-pane="voice"]')
    page.click("#addVoiceBtn")
    shared = page.locator("#voiceRows .voice-row").last
    shared.locator(".voice-type").select_option("mode")
    assert "切到 驾驶" in shared.locator(".binding-target").inner_text()
    assert "下一个模式" in shared.locator(".binding-target").inner_text()
    assert shared.locator(".binding-behavior").inner_text() == "切换一次"
    # 空草稿不提交，离开前清掉类型，防止干扰模式删除。
    shared.locator(".voice-type").select_option("keyboard")
    _go(page, "games")
    page.wait_for_function("document.querySelector('#currentModeName').textContent === '驾驶'")
    page.click("#modeMenuBtn")
    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#removeModeBtn")
    page.wait_for_function("document.querySelector('#modeTabs').children.length === 1")
    assert page.locator("#currentModeName").inner_text() == "默认"
    assert page.locator("#modeMenuBtn").is_hidden()
    assert ui.errors == []


def test_background_mode_switch_saves_old_draft_before_following(mode_ui):
    """后台切换遇到旧草稿在途，先存回旧模式，再显示当前模式，不能串键。"""
    ui, game_id = mode_ui
    page = ui.page
    drive = _new_mode(page, "开车")
    _set_mode_key(page, "F")
    ride = _new_mode(page, "骑车")
    _set_mode_key(page, "E")
    page.click(f'#modeTabs [data-mode-id="{drive}"]')
    row = page.locator('.binding-row[data-trigger="zone.rightHand"] .binding-target')
    sync_api.expect(row).to_have_value("F")
    pending = []
    page.route("**/api/game-profiles/overrides", lambda route: pending.append(route))
    with page.expect_request(lambda r: r.url.endswith("/api/game-profiles/overrides") and r.method == "POST"):
        row.press("G")
    switched = page.request.post(ui.base + "/api/game-profiles/mode", data={"target": ride, "profile_id": game_id})
    assert switched.ok
    page.wait_for_function("currentModeName.textContent === '骑车'")
    assert row.input_value() == "G", "旧草稿还没存完时保留原编辑内容"
    assert len(pending) == 1, "轮询不能重复排入保存请求"
    with page.expect_response(lambda r: r.url.endswith("/api/game-profiles/overrides") and r.request.method == "POST"):
        pending[0].continue_()
    page.unroute("**/api/game-profiles/overrides")
    page.wait_for_function("currentModeName.textContent === '骑车' && document.querySelector('#modeTabs [aria-selected=true]').textContent.includes('骑车') && modeEditStatus.hidden")
    sync_api.expect(row).to_have_value("E")
    old = page.request.get(ui.base + "/api/game-profiles/selected?mode=" + drive).json()["profile"]
    current = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    assert old["bindings"]["zones"]["rightHand"]["action"]["target"] == "G"
    assert current["mode"]["id"] == ride
    assert current["bindings"]["zones"]["rightHand"]["action"]["target"] == "E"
    assert ui.errors == []


def test_mode_selection_stops_when_draft_save_fails(mode_ui):
    """保存失败时不向后台切模式，重试成功后再允许切换。"""
    ui, _ = mode_ui
    page = ui.page
    drive = _new_mode(page, "开车")
    _set_mode_key(page, "F")
    changes = []
    page.on("request", lambda request: changes.append(request)
            if request.url.endswith("/api/game-profiles/mode") and request.method == "POST" else None)
    page.route("**/api/game-profiles/overrides", lambda route: route.fulfill(
        status=500, json={"ok": False, "error": "模拟保存失败"}))
    row = page.locator('.binding-row[data-trigger="zone.rightHand"] .binding-target')
    row.press("G")
    page.click('#modeTabs [data-mode-id="DEFAULT"]')
    sync_api.expect(page.locator("#profileSaveStatus")).to_contain_text("没保存上")
    assert changes == []
    assert page.request.get(ui.base + "/api/kernel/status").json()["kernel"]["game_mode"]["id"] == drive
    assert page.locator("#currentModeName").inner_text() == "开车"
    sync_api.expect(row).to_have_value("G")
    page.unroute("**/api/game-profiles/overrides")
    page.click("#retryProfileSaveBtn")
    page.wait_for_function("profileSaveStatus.textContent === '已保存'")
    page.click('#modeTabs [data-mode-id="DEFAULT"]')
    page.wait_for_function("currentModeName.textContent === '默认' && modeEditStatus.hidden")
    assert len(changes) == 1
    assert ui.errors == []


def _new_region(page):
    page.click('#mapTabs [data-tab="zones"]')
    with page.expect_response(lambda r: r.url.endswith("/api/game-profiles/regions") and r.request.method == "POST") as reply:
        page.click("#addRegionBtn")
    assert reply.value.ok
    region = reply.value.json()["profile"]["regions"][-1]
    sync_api.expect(page.locator('#zoneEditBar')).to_be_visible()
    sync_api.expect(page.locator(f'#zoneReferenceZones .zone[data-zone="{region["id"]}"]')).to_be_focused()
    page.click('#cancelLiveZonesBtn')
    _go(page, 'games')
    row = page.locator(f'.binding-row[data-trigger="zone.{region["id"]}"]')
    sync_api.expect(row).to_be_visible()
    return region, row


def _set_region_key(page, row, key):
    row.locator(".binding-type").select_option("keyboard")
    with page.expect_response(lambda r: r.url.endswith("/api/game-profiles/overrides") and r.request.method == "POST"):
        row.locator(".binding-target").press(key)
    page.wait_for_function("profileSaveStatus.textContent === '已保存'")


def test_region_mapping_delete_and_unified_add_controls(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    sync_api.expect(page.locator('#profileMeta')).to_be_hidden()
    sync_api.expect(page.locator('#modeEditStatus')).to_be_hidden()
    assert page.locator('#addModeBtn').evaluate("el=>getComputedStyle(el).borderRadius") == page.locator('#modeTabs button').first.evaluate("el=>getComputedStyle(el).borderRadius")
    add = page.locator('#addRegionBtn').bounding_box()
    mappings = page.locator('#profileBindingRows').bounding_box()
    assert abs(add['width'] - mappings['width']) < 2 and add['height'] < 80
    assert '独立区域可单独设键' not in page.locator('#zoneActions').inner_text()
    row = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    row.locator('.binding-remove').click()
    sync_api.expect(row).to_have_count(0)
    profile = page.request.get(ui.base + '/api/game-profiles/selected').json()['profile']
    assert profile['overrides']['zone.leftHand'] is None
    page.reload()
    page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    _go(page, 'games')
    sync_api.expect(row).to_have_count(0)
    page.click('#gameMenuBtn')
    page.click('#resetProfileBindingsBtn')
    sync_api.expect(row).to_be_visible()
    _set_region_key(page, row, 'H')
    page.reload()
    page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    _go(page, 'games')
    sync_api.expect(row.locator('.binding-target')).to_have_value('H')
    assert ui.errors == []


def test_game_voice_add_thirteenth_delete_and_reuse_slot(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    page.click('#mapTabs [data-tab="voice"]')
    sync_api.expect(page.locator('[data-tab-caption="voice"]')).to_have_count(0)
    assert page.locator('.binding-group[data-group="voice"] .binding-row:visible').count() == 12
    page.click('#addGameVoiceBtn')
    row = page.locator('.binding-row[data-trigger="voice.game.profile_slot_13"]')
    sync_api.expect(row).to_be_visible()
    row.locator('.voice-trigger-phrase').fill('快速整理背包')
    _set_region_key(page, row, 'T')
    commands = page.request.get(ui.base + '/api/voice/commands').json()['commands']
    assert any(item['id'] == 'game.profile_slot_13' and item['phrase'] == '快速整理背包' for item in commands)
    page.reload()
    page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    _go(page, 'games')
    page.click('#mapTabs [data-tab="voice"]')
    sync_api.expect(row.locator('.voice-trigger-phrase')).to_have_value('快速整理背包')
    sync_api.expect(row.locator('.binding-target')).to_have_value('T')
    first = page.locator('.binding-row[data-trigger="voice.game.profile_slot_01"]')
    first.locator('.binding-remove').click()
    sync_api.expect(first).to_be_hidden()
    page.reload()
    page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    _go(page, 'games')
    page.click('#mapTabs [data-tab="voice"]')
    sync_api.expect(first).to_be_hidden()
    page.click('#addGameVoiceBtn')
    sync_api.expect(first).to_be_visible()
    first.locator('.voice-trigger-phrase').fill('打开补给箱')
    _set_region_key(page, first, 'U')
    profile = page.request.get(ui.base + '/api/game-profiles/selected').json()['profile']
    assert profile['bindings']['voice']['game.profile_slot_01']['phrase'] == '打开补给箱'
    assert profile['bindings']['voice']['game.profile_slot_13']['action']['target'] == 'T'
    row.locator('.binding-remove').click()
    sync_api.expect(row).to_be_hidden()
    page.click('#addGameVoiceBtn')
    sync_api.expect(row).to_be_visible()
    row.locator('.voice-trigger-phrase').fill('快速补充弹药')
    _set_region_key(page, row, 'Y')
    profile = page.request.get(ui.base + '/api/game-profiles/selected').json()['profile']
    assert profile['bindings']['voice']['game.profile_slot_13']['phrase'] == '快速补充弹药'
    assert profile['bindings']['voice']['game.profile_slot_13']['action']['target'] == 'Y'
    assert ui.errors == []


def test_custom_region_add_map_rename_and_reload(mode_ui):
    """区域初始不绑键，能选完整骨骼点，名称和映射刷新仍在。"""
    ui, _ = mode_ui
    page = ui.page
    region, row = _new_region(page)
    assert region["id"].startswith("user_")
    assert row.locator(".binding-type").input_value() == ""
    _set_region_key(page, row, "K")
    row.locator(".binding-more").click()
    picker = row.locator(".zone-point-picker")
    picker.locator("summary").click()
    assert picker.locator(".zone-point-option").count() == 33
    assert set(picker.locator(".zone-trigger-choices option").evaluate_all("items => items.filter(x => x.selected).map(x => x.value)")) == {"left_wrist", "right_wrist"}
    picker.locator('[data-mode="points"]').click()
    with page.expect_response(lambda r: r.url.endswith("/api/game-profiles/overrides") and r.request.method == "POST"):
        picker.locator('.zone-point-option[data-point="nose"]').click()
    picker.locator("summary").click()
    with page.expect_response(lambda r: r.url.endswith("/api/game-profiles/regions") and r.request.method == "POST"):
        row.locator(".region-name-input").fill("加速区域")
    sync_api.expect(row.locator(".region-name-input")).to_have_value("加速区域")
    page.reload()
    page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    _go(page, "games")
    sync_api.expect(row.locator(".binding-target")).to_have_value("K")
    sync_api.expect(row.locator(".region-name-input")).to_have_value("加速区域")
    saved = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    assert saved["regions"] == [{**region, "name": "加速区域"}]
    assert set(saved["bindings"]["zones"][region["id"]]["trigger_points"]) == {"left_wrist", "right_wrist", "nose"}
    page.click('#mapTabs [data-tab="body"]')
    sync_api.expect(page.locator("#zoneActions")).to_be_hidden()
    page.click('#mapTabs [data-tab="zones"]')
    sync_api.expect(page.locator("#zoneActions")).to_be_visible()
    _go(page, "play")
    zone = page.locator(f'#viewer .zone[data-zone="{region["id"]}"]')
    sync_api.expect(zone).to_be_visible()
    sync_api.expect(zone.locator("small")).to_have_text("加速区域")
    page.click("#adjustZonesBtn")
    sync_api.expect(page.locator("#zoneEditBar")).to_be_visible()
    edit_zone = page.locator(f'#zoneReferenceZones .zone[data-zone="{region["id"]}"]')
    edit_zone.press("ArrowDown")
    with page.expect_response(lambda r: r.url.endswith("/api/zones/layout") and r.request.method == "POST"):
        page.click("#saveLiveZonesBtn")
    moved = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]["regions"][0]["rect"]
    assert moved["y1"] == pytest.approx(region["rect"]["y1"] + .01)
    assert moved["y2"] == pytest.approx(region["rect"]["y2"] + .01)
    page.reload()
    page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    page.click("#adjustZonesBtn")
    edit_zone.press("ArrowDown")
    page.click("#cancelLiveZonesBtn")
    restored = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]["regions"][0]["rect"]
    assert restored == moved
    assert ui.errors == []


def test_custom_region_shared_definition_separate_mode_keys_and_delete(mode_ui):
    """同游戏模式共用一个区域定义，按键独立，删区域则全模式删除。"""
    ui, _ = mode_ui
    page = ui.page
    region, row = _new_region(page)
    _set_region_key(page, row, "Q")
    drive = _new_mode(page, "驾驶")
    sync_api.expect(row.locator(".binding-target")).to_have_value("Q")
    _set_region_key(page, row, "E")
    default = page.request.get(ui.base + "/api/game-profiles/selected?mode=DEFAULT").json()["profile"]
    driving = page.request.get(ui.base + "/api/game-profiles/selected?mode=" + drive).json()["profile"]
    assert default["regions"] == driving["regions"] == [region]
    assert default["bindings"]["zones"][region["id"]]["action"]["target"] == "Q"
    assert driving["bindings"]["zones"][region["id"]]["action"]["target"] == "E"
    with page.expect_response(lambda r: r.url.endswith("/api/game-profiles/regions") and r.request.method == "POST"):
        row.locator(".region-name-input").fill("驾驶区")
    page.click('#modeTabs [data-mode-id="DEFAULT"]')
    sync_api.expect(row.locator(".binding-target")).to_have_value("Q")
    sync_api.expect(row.locator(".region-name-input")).to_have_value("驾驶区")
    confirmations = []
    def confirm_delete(dialog):
        confirmations.append(dialog.message)
        dialog.accept()
    page.once("dialog", confirm_delete)
    with page.expect_response(lambda r: r.url.endswith("/api/game-profiles/regions") and r.request.method == "POST"):
        row.locator(".region-remove").click()
    assert "所有模式" in confirmations[0]
    sync_api.expect(row).to_have_count(0)
    for mode in ("DEFAULT", drive):
        profile = page.request.get(ui.base + "/api/game-profiles/selected?mode=" + mode).json()["profile"]
        assert profile["regions"] == []
        assert region["id"] not in profile["bindings"]["zones"]
        assert "zone." + region["id"] not in profile["overrides"]
    assert ui.errors == []


def test_region_name_saves_before_mode_switch_and_blank_name_can_be_deleted(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    region, row = _new_region(page)
    mode = _new_mode(page, '移动')
    row.locator('.region-name-input').fill('补给区域')
    page.click('#modeTabs [data-mode-id="DEFAULT"]')
    page.wait_for_function("currentModeName.textContent === '默认'")
    sync_api.expect(row.locator('.region-name-input')).to_have_value('补给区域')
    saved = page.request.get(ui.base + '/api/game-profiles/selected?mode=' + mode).json()['profile']
    assert saved['regions'][0]['name'] == '补给区域'
    row.locator('.region-name-input').fill('')
    page.once('dialog', lambda dialog: dialog.accept())
    row.locator('.region-remove').click()
    sync_api.expect(row).to_have_count(0)
    assert page.request.get(ui.base + '/api/game-profiles/selected').json()['profile']['regions'] == []
    assert ui.errors == []


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
    for pane in ("view", "voice", "lab", "devices"):
        page.click(f'#settingsNav [data-pane="{pane}"]')
        visible = page.eval_on_selector_all(".settings-pane", "els => els.filter(el => !el.hidden).map(el => el.dataset.pane)")
        assert visible == [pane]
    page.click('#settingsNav [data-pane="voice"]')
    assert page.locator("#commandsPanel .pane-title").inner_text() == "语音和宏"
    assert page.locator("#personalVoicePanel").is_visible()
    assert page.locator("#macroPanel").is_visible()
    assert page.locator('#settingsNav [data-pane="macro"]').count() == 0
    assert page.locator('#settingsNav [data-pane="body"]').count() == 0
    assert page.locator('#settingsNav [data-pane="view"]').inner_text() == "体感"
    assert page.locator("#headAlgorithm, #cameraBackend").count() == 0
    page.click('#settingsNav [data-pane="devices"]')
    # 小入口始终可用，二维码按需展开。
    assert page.locator("#phoneGroup").is_visible()
    assert page.locator("#phoneConnectPanel").is_hidden()
    page.select_option("#poseSource", "phone")
    assert page.locator("#phoneGroup").is_visible()
    page.select_option("#poseSource", "computer")
    # 旧版上下视角只给还开着的人看。
    page.click('#settingsNav [data-pane="view"]')
    assert page.locator("#legacyVertical").is_hidden()
    assert ui.errors == []


def _fake_live_state(page, **kernel_fields) -> None:
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
        k["feet_in_view"] = True
        k["zones"] = {"rightHand": {"rect": {"x1": .2, "y1": .3, "x2": .38, "y2": .55}, "pressed": True},
                      "leftHand": {"rect": {"x1": .62, "y1": .3, "x2": .8, "y2": .55}}}
        k["motions"] = []
        k["recent_triggers"] = [{"trigger": "motion.march", "at": now - 0.3, "action": {"type": "keyboard", "target": "W"}}]
        k["head"] = {**k.get("head", {}), "output_x": 20, "sensitivity_x": 58, "calibrated": True,
                     "horizontal_calibrated": True, "enabled": True}
        k.update(kernel_fields)
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
    _fake_live_state(page, pose=None)
    sync_api.expect(page.locator("#hint")).to_be_visible()
    page.unroute("**/api/kernel/status")
    page.unroute("**/api/performance")
    _fake_live_state(page)
    page.wait_for_function("document.querySelector('#hint').hidden")
    page.wait_for_function("!document.querySelector('#fpsHud').hidden", timeout=6000)
    sync_api.expect(page.locator("#fpsHud")).to_have_text("30 帧/秒")
    page.wait_for_function("!document.querySelector('#rangeHit').hidden")
    assert "→" in page.locator("#rangeHitWhat").inner_text(), "触发了，画面上大字写按了什么"
    assert "游戏控制没开" in page.locator("#rangeHitWhen").inner_text()
    assert page.locator("#recentCard").is_visible(), "有触发才列最近触发"
    assert page.locator('.checklist [data-step="pose"]').get_attribute("class").find("done") >= 0
    assert page.locator('.zone[data-zone="rightHand"]').get_attribute("class").find("active") >= 0
    assert ui.errors == []


def test_the_start_page_says_when_the_feet_are_out_of_the_picture(ui):
    """脚踝出画时只显示简短提示，重新入镜后恢复已识别。"""
    page = ui.page
    bindings = {"motion.march": {"action": {"type": "keyboard", "target": "W"}}}
    _fake_live_state(page, feet_in_view=False, effective_bindings=bindings)
    step = page.locator('.checklist [data-step="pose"]')
    # 还没认出人时这一步也是警告样式，所以等的是提示文字本身。
    shows = "document.querySelector('.checklist [data-step=\"pose\"]').textContent.includes('%s')"
    page.wait_for_function(shows % "脚没入镜")
    assert "踏步和脚区认不出" not in step.inner_text()
    assert "warn" in step.get_attribute("class")
    page.unroute("**/api/kernel/status")
    _fake_live_state(page, feet_in_view=True, effective_bindings=bindings)
    page.wait_for_function(shows % "已识别")
    assert "done" in step.get_attribute("class")
    assert ui.errors == []


def test_the_fist_algorithm_can_be_switched_and_is_kept(ui):
    """视角 → 手感里选握拳识别方式：切到旧版就存下，刷新后还是旧版，说明跟着变。"""
    page = ui.page
    _go(page, "devices")
    page.click('#settingsNav [data-pane="view"]')
    select = page.locator("#handMouseGripAlgorithm")
    select.wait_for(state="visible")
    page.wait_for_function("!document.querySelector('#handMouseGripAlgorithm').disabled")
    assert select.input_value() == "steady"
    assert "深度" in page.locator("#handMouseGripDesc").inner_text()
    select.select_option("classic")
    page.wait_for_function("document.querySelector('#handMouseSaveStatus').textContent.includes('已保存')")
    saved = json.loads(_get(ui.base + "/api/hand-mouse/config"))["hand_mouse"]["config"]
    assert saved["grip_algorithm"] == "classic"
    page.reload()
    page.wait_for_function("document.querySelector('#currentGameName').textContent !== '正在读取…'")
    _go(page, "devices")
    page.click('#settingsNav [data-pane="view"]')
    page.wait_for_function("document.querySelector('#handMouseGripAlgorithm').value === 'classic'")
    assert "10 月 10 日之前" in page.locator("#handMouseGripDesc").inner_text()
    select.select_option("steady")
    page.wait_for_function("document.querySelector('#handMouseSaveStatus').textContent.includes('已保存')")
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


def test_head_speed_and_curve_save_and_survive_reload(ui):
    """速度上限和曲线都经真实设置接口保存，刷新不能回退。"""
    page = ui.page
    _go(page, "devices")
    page.click('#settingsNav [data-pane="view"]')
    source = page.locator("#viewHorizontalSource")
    sync_api.expect(source).to_be_enabled()
    with page.expect_response(lambda r: r.url.endswith("/api/view-control") and r.request.method == "POST"):
        source.select_option("roll_tilt")
    sync_api.expect(page.locator("#speedX")).to_be_enabled()
    assert page.locator("#speedX").get_attribute("max") == "200"
    with page.expect_response(lambda r: r.url.endswith("/api/head/config") and r.request.method == "POST"):
        page.locator("#speedX").press("End")
    assert page.request.get(ui.base + "/api/kernel/status").json()["kernel"]["head"]["sensitivity_x"] == 200
    gamma = page.locator("#headCurveGamma")
    assert (gamma.get_attribute("min"), gamma.get_attribute("max")) == ("1", "3")
    with page.expect_response(lambda r: r.url.endswith("/api/head/config") and r.request.method == "POST"):
        gamma.press("Home")
    linear = page.locator("#headCurveLine").get_attribute("d")
    assert linear and gamma.input_value() == "1"
    with page.expect_response(lambda r: r.url.endswith("/api/head/config") and r.request.method == "POST"):
        gamma.press("End")
    assert gamma.input_value() == "3"
    assert page.locator("#headCurveLine").get_attribute("d") != linear
    saved = page.request.get(ui.base + "/api/kernel/status").json()["kernel"]["head"]
    assert saved["sensitivity_x"] == 200 and saved["horizontal_curve_gamma"] == 3
    page.reload()
    page.wait_for_function("currentGameName.textContent !== '正在读取…'")
    _go(page, "devices")
    page.click('#settingsNav [data-pane="view"]')
    sync_api.expect(page.locator("#speedX")).to_have_value("200")
    sync_api.expect(gamma).to_have_value("3")
    assert ui.errors == []


def test_head_curve_handles_hide_for_legacy_and_fit_narrow_view(ui):
    """两侧控制点同步保存，拖动可改曲线；旧方案不显示无效的曲线设置。"""
    page = ui.page
    _go(page, "devices")
    page.click('#settingsNav [data-pane="view"]')
    source = page.locator("#viewHorizontalSource")
    sync_api.expect(source).to_be_enabled()
    with page.expect_response(lambda r: r.url.endswith("/api/view-control") and r.request.method == "POST"):
        source.select_option("head_responsive")
    left = page.locator('#headCurvePlot [data-curve-side="-1"]')
    right = page.locator('#headCurvePlot [data-curve-side="1"]')
    with page.expect_response(lambda r: r.url.endswith("/api/head/config") and r.request.method == "POST"):
        left.press("Home")
    sync_api.expect(right).to_have_attribute("aria-valuenow", "1.00")
    with page.expect_response(lambda r: r.url.endswith("/api/head/config") and r.request.method == "POST"):
        right.press("End")
    sync_api.expect(left).to_have_attribute("aria-valuenow", "3.00")
    before = page.locator("#headCurveLine").get_attribute("d")
    right.scroll_into_view_if_needed()
    point = right.bounding_box()
    assert point is not None
    x, y = point["x"] + point["width"] / 2, point["y"] + point["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    with page.expect_response(lambda r: r.url.endswith("/api/head/config") and r.request.method == "POST"):
        page.mouse.move(x, y - 15, steps=3)
        page.mouse.up()
    gamma = float(page.locator("#headCurveGamma").input_value())
    assert 1 <= gamma < 3
    assert page.locator("#headCurveLine").get_attribute("d") != before
    saved = page.request.get(ui.base + "/api/kernel/status").json()["kernel"]["head"]
    assert saved["horizontal_curve_gamma"] == pytest.approx(gamma)
    page.set_viewport_size({"width": 390, "height": 844})
    width = page.evaluate("[document.documentElement.scrollWidth, innerWidth]")
    assert width[0] <= width[1] + 1, f"视角曲线在窄屏上横向溢出：{width}"
    with page.expect_response(lambda r: r.url.endswith("/api/view-control") and r.request.method == "POST"):
        source.select_option("head_turn")
    sync_api.expect(page.locator("#headCurvePanel")).to_be_hidden()
    assert ui.errors == []
