"""在保存的完整骨架上编辑触发点，与位置一同提交；取消不写盘，不截图。"""
from __future__ import annotations

import pytest

from test_web_ui import browser, server, ui, mode_ui, sync_api, _go, _new_mode, _set_region_key
from test_zone_editor_ui import _region, _open


def _point(page, name):
    return page.locator(f'#zoneReferencePoints button[data-point="{name}"]')


def _pressed(page):
    return set(page.locator('#zoneReferencePoints button[aria-pressed="true"]').evaluate_all(
        "items => items.map(item => item.dataset.point)"))


def _save(page):
    with page.expect_response(lambda response: response.url.endswith("/api/zones/layout")
                              and response.request.method == "POST") as pending:
        page.click("#saveLiveZonesBtn")
    assert pending.value.ok, pending.value.text()
    sync_api.expect(page.locator("#zoneEditBar")).to_be_hidden()
    return pending.value.request.post_data_json


def test_selected_region_is_isolated_and_default_points_follow_selection(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    region = _region(ui)
    zone = _open(ui, "leftHand")
    assert page.locator("#zoneReferenceZones .zone:visible").count() == 1
    assert _pressed(page) == {"left_wrist"}
    sync_api.expect(page.locator("#zoneEditTriggerStatus")).to_contain_text("左手腕")
    page.select_option("#zoneEditSelection", region["id"])
    assert page.locator("#zoneReferenceZones .zone:visible").count() == 1
    assert _pressed(page) == {"left_wrist", "right_wrist"}
    elbow_before = _point(page, "left_elbow").bounding_box()
    assert elbow_before
    zone = page.locator(f'#zoneReferenceZones .zone[data-zone="{region["id"]}"]')
    bounds = zone.bounding_box()
    assert bounds
    page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
    page.mouse.down()
    page.mouse.move(bounds["x"] + bounds["width"] / 2 + 16,
                    bounds["y"] + bounds["height"] / 2 + 8, steps=3)
    page.mouse.up()
    assert page.locator("#zoneReferenceZones .zone:visible").count() == 1
    elbow_after = _point(page, "left_elbow").bounding_box()
    assert elbow_after["x"] == pytest.approx(elbow_before["x"], abs=1)
    assert elbow_after["y"] == pytest.approx(elbow_before["y"], abs=1)
    page.select_option("#zoneEditSelection", "leftHand")
    assert _pressed(page) == {"left_wrist"}
    page.click("#cancelLiveZonesBtn")
    assert ui.errors == []


def test_point_click_toggles_only_draft_and_cancel_writes_nothing(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    region = _region(ui)
    before = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    _open(ui, region["id"])
    writes = []
    page.on("request", lambda request: writes.append(request.url)
            if request.method == "POST" and any(request.url.endswith(path) for path in (
                "/api/zones/layout", "/api/game-profiles/overrides", "/api/game-profiles/regions")) else None)
    elbow = _point(page, "left_elbow")
    sync_api.expect(elbow).to_have_attribute("aria-pressed", "false")
    elbow.click()
    sync_api.expect(elbow).to_have_attribute("aria-pressed", "true")
    elbow.click()
    sync_api.expect(elbow).to_have_attribute("aria-pressed", "false")
    elbow.click()
    # 眼、鼻的按钮点击区重叠时，真实鼠标坐标仍应命中距离最近的鼻子。
    for width in (1280, 390):
        page.set_viewport_size({"width": width, "height": 844})
        before_points = _pressed(page)
        _point(page, "nose").evaluate("el=>el.scrollIntoView({block:'center',behavior:'instant'})")
        nose = _point(page, "nose").bounding_box()
        assert nose
        for expected in (before_points | {"nose"}, before_points):
            page.mouse.click(nose["x"] + nose["width"] / 2, nose["y"] + nose["height"] / 2)
            assert _pressed(page) == expected
    page.click("#cancelLiveZonesBtn")
    sync_api.expect(page.locator("#zoneEditBar")).to_be_hidden()
    assert writes == []
    after = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    assert after["bindings"] == before["bindings"]
    assert after["regions"] == before["regions"]
    _open(ui, region["id"])
    assert "left_elbow" not in _pressed(page)
    page.click("#cancelLiveZonesBtn")
    assert ui.errors == []


def test_points_and_position_save_together_and_match_original_mapping_picker(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    region = _region(ui)
    initial = page.request.get(ui.base + "/api/zones/editor").json()
    zone = _open(ui, region["id"])
    _point(page, "left_elbow").click()
    zone.press("ArrowDown")
    sent = _save(page)
    assert sent["mode_id"] == initial["mode_id"]
    assert set(sent["trigger_choices"]) == {region["id"]}
    assert set(sent["trigger_choices"][region["id"]]["trigger_points"]) == {
        "left_wrist", "right_wrist", "left_elbow"}
    assert sent["rects"][region["id"]]["y1"] == pytest.approx(initial["rects"][region["id"]]["y1"] + .01)
    page.reload()
    page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    _go(page, "play")
    _open(ui, region["id"])
    assert _pressed(page) == {"left_wrist", "right_wrist", "left_elbow"}
    page.click("#cancelLiveZonesBtn")
    _go(page, "games")
    row = page.locator(f'.binding-row[data-trigger="zone.{region["id"]}"]')
    row.locator(".binding-more").click()
    picker = row.locator(".zone-point-picker")
    picker.locator("summary").click()
    selected = picker.locator(".zone-trigger-choices option").evaluate_all(
        "items => items.filter(item => item.selected).map(item => item.value)")
    assert set(selected) == {"left_wrist", "right_wrist", "left_elbow"}
    assert ui.errors == []


def test_point_save_is_scoped_to_active_game_mode(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    region = _region(ui)
    default = page.request.get(ui.base + "/api/game-profiles/selected?mode=DEFAULT").json()["profile"]["bindings"]
    _go(page, "games")
    mode_id = _new_mode(page, "触发点专用模式")
    _go(page, "play")
    _open(ui, region["id"])
    _point(page, "right_elbow").click()
    sent = _save(page)
    assert sent["mode_id"] == mode_id
    active = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    assert "right_elbow" in active["bindings"]["zones"][region["id"]]["trigger_points"]
    assert page.request.get(ui.base + "/api/game-profiles/selected?mode=DEFAULT").json()["profile"]["bindings"] == default
    assert ui.errors == []


def test_failed_point_save_keeps_draft_and_can_retry_on_narrow_screen(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    region = _region(ui)
    zone = _open(ui, region["id"])
    page.set_viewport_size({"width": 390, "height": 844})
    _point(page, "left_elbow").click()
    zone.press("ArrowDown")
    before = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    page.route("**/api/zones/layout", lambda route: route.fulfill(
        status=503, json={"ok": False, "error": "临时保存失败"}))
    with page.expect_response(lambda response: response.url.endswith("/api/zones/layout")) as pending:
        page.click("#saveLiveZonesBtn")
    assert pending.value.status == 503
    sync_api.expect(page.locator("#zoneEditBar")).to_be_visible()
    sync_api.expect(_point(page, "left_elbow")).to_have_attribute("aria-pressed", "true")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    after = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    assert after["bindings"] == before["bindings"]
    assert after["regions"] == before["regions"]
    page.unroute("**/api/zones/layout")
    sent = _save(page)
    assert "left_elbow" in sent["trigger_choices"][region["id"]]["trigger_points"]
    assert ui.errors == []


def test_open_waits_for_mapping_drafts_and_later_key_edit_keeps_saved_points(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    region = _region(ui)
    _go(page, "games")
    other = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    _set_region_key(page, other, "H")
    row = page.locator(f'.binding-row[data-trigger="zone.{region["id"]}"]')
    _set_region_key(page, row, "N")
    row.locator(".binding-more").click()
    picker = row.locator(".zone-point-picker")
    picker.locator("summary").click()
    picker.locator('button[data-mode="points"]').click()
    held = []

    def hold_first_save(route):
        if held:
            route.continue_()
        else:
            held.append(route)
            page.evaluate("window.__heldZonePointSave = true")

    page.route("**/api/game-profiles/overrides", hold_first_save)
    with page.expect_request(lambda request: request.url.endswith("/api/game-profiles/overrides")
                             and request.method == "POST"):
        picker.locator('.zone-point-option[data-point="left_elbow"]').click()
    page.wait_for_function("window.__heldZonePointSave === true")
    # 第一份点位保存尚未发往服务端时，另改一行键位并立即打开编辑器。
    other.locator(".binding-target").press("J")
    _go(page, "play")
    reads = []
    page.on("request", lambda request: reads.append(request.url)
            if request.method == "GET" and request.url.endswith("/api/zones/editor") else None)
    page.click("#adjustZonesBtn")
    sync_api.expect(page.locator("#zoneEditBar")).to_be_hidden()
    assert reads == [], "先等映射草稿保存，才读取编辑参考和触发点"
    held[0].continue_()
    sync_api.expect(page.locator("#zoneEditBar")).to_be_visible()
    page.select_option("#zoneEditSelection", region["id"])
    sync_api.expect(_point(page, "left_elbow")).to_have_attribute("aria-pressed", "true")
    _point(page, "right_elbow").click()
    _save(page)
    saved = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    assert saved["bindings"]["zones"]["leftHand"]["action"]["target"] == "J"
    _go(page, "games")
    other = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    _set_region_key(page, other, "K")
    after = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    assert after["bindings"]["zones"]["leftHand"]["action"]["target"] == "K"
    assert set(after["bindings"]["zones"][region["id"]]["trigger_points"]) == {
        "left_wrist", "right_wrist", "left_elbow", "right_elbow"}
    assert ui.errors == []
