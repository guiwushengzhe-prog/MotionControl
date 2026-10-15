"""区域比例图的真实浏览器检查；测试服务使用临时用户目录和手机来源。"""
from __future__ import annotations

import pytest

from test_web_ui import browser, server, ui, mode_ui, sync_api, _go


def _region(ui):
    profile = ui.page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]
    data = ui.page.request.post(ui.base + "/api/game-profiles/regions", data={
        "op": "add", "profile_id": profile["selected_id"], "name": "比例图测试区",
        "rect": {"x1": .35, "x2": .55, "y1": .35, "y2": .55},
    }).json()
    assert data["ok"]
    ui.page.reload()
    ui.page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    _go(ui.page, "play")
    return data["region"]


def _open(ui, ident):
    ui.page.click("#adjustZonesBtn")
    sync_api.expect(ui.page.locator("#zoneEditBar")).to_be_visible()
    ui.page.select_option("#zoneEditSelection", ident)
    return ui.page.locator(f'#zoneReferenceZones .zone[data-zone="{ident}"]')


def test_add_region_without_full_reference_opens_fit_without_creating(mode_ui):
    ui, _ = mode_ui
    page = ui.page
    before = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]["regions"]
    page.route("**/api/zones/editor", lambda route: route.fulfill(json={"reference": {"ready": False}}))
    page.click("#addRegionBtn")
    sync_api.expect(page.locator("#tour")).to_be_visible()
    sync_api.expect(page.locator("#tourStep")).to_contain_text("量身")
    sync_api.expect(page.locator("#zoneEditBar")).to_be_hidden()
    after = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]["regions"]
    assert after == before
    page.click("#tourCloseBtn")
    assert ui.errors == []


def test_reference_edit_cancel_has_no_layout_write_or_freeze(mode_ui):
    ui, _ = mode_ui
    region = _region(ui)
    page = ui.page
    before = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]["regions"]
    writes = []
    page.on("request", lambda request: writes.append(request.url)
            if request.method == "POST" and "/api/zones/" in request.url else None)
    zone = _open(ui, region["id"])
    sync_api.expect(page.locator("#zoneEditHint")).to_contain_text("完整身体比例")
    assert page.locator("#zoneMoveHereBtn").count() == 0
    assert "调整时体感输出暂停" not in page.locator("#zoneEditBar").inner_text()
    zone.press("ArrowDown")
    page.select_option("#zoneEditPositionMode", "fixed")
    page.click("#cancelLiveZonesBtn")
    sync_api.expect(page.locator("#zoneEditBar")).to_be_hidden()
    assert writes == []
    after = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]["regions"]
    assert after == before
    assert ui.errors == []


@pytest.mark.parametrize("mirrored", [True, False])
def test_reference_keyboard_position_and_resize_preserve_mode(mode_ui, mirrored):
    ui, _ = mode_ui
    region = _region(ui)
    page = ui.page
    mirror = page.request.post(ui.base + "/api/studio/config", data={"mirror": mirrored})
    assert mirror.ok
    page.reload()
    page.wait_for_function("currentGameName.textContent === '界面模式测试'")
    _go(page, "play")
    initial = page.request.get(ui.base + "/api/zones/editor").json()
    zone = _open(ui, region["id"])
    original_mode = page.locator("#zoneEditPositionMode").input_value()
    zone.press("ArrowRight")
    zone.press("Shift+ArrowDown")
    with page.expect_response(lambda response: response.url.endswith("/api/zones/layout") and response.request.method == "POST") as response:
        page.click("#saveLiveZonesBtn")
    assert response.value.ok, response.value.text()
    sent = response.value.request.post_data_json
    old, moved = initial["rects"][region["id"]], sent["rects"][region["id"]]
    dx = -.01 if mirrored else .01
    assert moved["x1"] == pytest.approx(old["x1"] + dx)
    assert moved["x2"] == pytest.approx(old["x2"] + dx)
    assert moved["y1"] == pytest.approx(old["y1"])
    assert moved["y2"] == pytest.approx(old["y2"] + .01)
    assert sent["modes"][region["id"]] == original_mode
    assert sent["reference"]["base_rects"] == initial["reference"]["base_rects"]
    saved = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]["regions"]
    assert next(item for item in saved if item["id"] == region["id"])["mode"] == original_mode
    assert ui.errors == []


def test_custom_follow_choice_and_home_freeze_toggle(mode_ui):
    ui, _ = mode_ui
    region = _region(ui)
    page = ui.page
    zone = _open(ui, region["id"])
    page.select_option("#zoneEditPositionMode", "following")
    assert "following" in zone.get_attribute("class").split()
    with page.expect_response(lambda response: response.url.endswith("/api/zones/layout") and response.request.method == "POST") as response:
        page.click("#saveLiveZonesBtn")
    assert response.value.ok, response.value.text()
    saved = page.request.get(ui.base + "/api/game-profiles/selected").json()["profile"]["regions"]
    saved_region = next(item for item in saved if item["id"] == region["id"])
    assert saved_region["mode"] == "following"
    assert "follow" in saved_region
    sync_api.expect(page.locator("#followZonesBtn")).to_have_text("固定触发框")
    page.click("#followZonesBtn")
    sync_api.expect(page.locator("#followZonesBtn")).to_have_text("恢复跟随")
    assert page.request.get(ui.base + "/api/zones/editor").json()["modes"][region["id"]] == "fixed"
    page.click("#followZonesBtn")
    sync_api.expect(page.locator("#followZonesBtn")).to_have_text("固定触发框")
    assert page.request.get(ui.base + "/api/zones/editor").json()["modes"][region["id"]] == "following"
    assert ui.errors == []


def test_reference_pointer_move_corner_resize_and_narrow_layout(mode_ui):
    ui, _ = mode_ui
    region = _region(ui)
    page = ui.page
    zone = _open(ui, region["id"])
    initial = zone.bounding_box()
    assert initial
    page.mouse.move(initial["x"] + initial["width"] / 2, initial["y"] + initial["height"] / 2)
    page.mouse.down()
    assert page.locator("#zoneReferenceZones .zone:visible").count() == 1
    page.mouse.move(initial["x"] + initial["width"] / 2 + 18, initial["y"] + initial["height"] / 2 + 12, steps=4)
    page.mouse.up()
    assert page.locator("#zoneReferenceZones .zone:visible").count() == 1
    moved = zone.bounding_box()
    assert moved["x"] == pytest.approx(initial["x"] + 18, abs=1)
    assert moved["y"] == pytest.approx(initial["y"] + 12, abs=1)
    corner = zone.locator('[data-corner="se"]').bounding_box()
    assert corner
    page.mouse.move(corner["x"] + corner["width"] / 2, corner["y"] + corner["height"] / 2)
    page.mouse.down()
    assert page.locator("#zoneReferenceZones .zone:visible").count() == 1
    page.mouse.move(corner["x"] + corner["width"] / 2 + 15, corner["y"] + corner["height"] / 2 + 15, steps=4)
    page.mouse.up()
    resized = zone.bounding_box()
    assert resized["width"] > moved["width"] + 8
    assert resized["height"] > moved["height"] + 8
    with page.expect_response(lambda response: response.url.endswith("/api/zones/layout") and response.request.method == "POST") as response:
        page.click("#saveLiveZonesBtn")
    assert response.value.ok, response.value.text()
    _open(ui, region["id"])
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    sync_api.expect(page.locator("#zoneEditPositionMode")).to_be_visible()
    page.click("#cancelLiveZonesBtn")
    assert ui.errors == []
