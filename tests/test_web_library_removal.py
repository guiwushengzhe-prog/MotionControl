"""真实浏览器验证动作库删除、离线恢复和同排底栏对齐；不采集真人画面。"""
import copy

import pytest

from motioncontrol.custom_poses import CustomPoseStore
from test_custom_poses import T_POSE
from test_web_ui import browser, mode_ui, server, ui, _go, sync_api


def body_library(page):
    _go(page, "games")
    page.click('#mapTabs [data-tab="body"]')
    sync_api.expect(page.locator("#poseLibraryPanel")).to_be_visible()
    if page.locator("#libraryBody").is_hidden():
        page.click("#libToggle")


@pytest.mark.parametrize("ident,key", [("march", "W"), ("calf_back", "S"), ("jump", "J")])
def test_builtin_card_delete_reload_and_offline_restore_preserve_mapping(mode_ui, ident, key):
    app, game_id = mode_ui
    page = app.page
    body_library(page)
    card = page.locator(f'#poseLibraryList .pose-library-item[data-id="{ident}"]')
    name = card.locator(".pose-library-name").inner_text()
    sync_api.expect(card.get_by_role("button", name="删除", exact=True)).to_be_visible()
    card.locator(".custom-pose-key").click()
    row = page.locator(f'.binding-row[data-trigger="motion.{ident}"]')
    row.locator(".binding-type").select_option("keyboard")
    with page.expect_response(lambda response: response.url.endswith("/api/game-profiles/overrides")
                              and response.request.method == "POST"):
        row.locator(".binding-target").press(key)
    page.wait_for_function("profileSaveStatus.textContent === '已保存'")
    before = page.request.get(app.base + "/api/game-profiles/selected").json()["profile"]["bindings"]["motions"][ident]
    assert before["action"]["target"] == key
    try:
        page.once("dialog", lambda dialog: dialog.accept())
        card.get_by_role("button", name="删除", exact=True).click()
        sync_api.expect(card).to_have_count(0)
        sync_api.expect(row).to_have_count(0)
        sync_api.expect(page.locator("#poseMissingNotice")).not_to_contain_text(name)
        page.reload()
        page.wait_for_function("currentGameName.textContent === '界面模式测试'")
        body_library(page)
        sync_api.expect(card).to_have_count(0)
        sync_api.expect(row).to_have_count(0)
        page.route(app.base + "/api/pose/cloud", lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"ok":false,"error":"测试断网"}'))
        page.click("#poseCloudBtn")
        restore = page.locator(f'#poseBuiltinRestore [data-id="{ident}"]').get_by_role("button", name="恢复", exact=True)
        sync_api.expect(restore).to_be_visible()
        restore.click()
        sync_api.expect(card).to_be_visible()
        sync_api.expect(card.locator(".custom-pose-key")).to_have_text(key)
        after = page.request.get(app.base + "/api/game-profiles/selected").json()["profile"]
        assert after["selected_id"] == game_id
        assert after["bindings"]["motions"][ident] == before
        assert app.errors == []
    finally:
        # 共用的临时服务还会被下一条测试使用，即使断言失败也恢复自带库。
        page.request.post(app.base + "/api/pose/builtin/restore", data={"id": ident})


def test_library_source_dividers_align_with_different_description_lengths(ui):
    page = ui.page

    def library(route):
        data = route.fetch().json()
        # 此例只核对三种说明长度和一张追加卡片，与动作目录总数无关。
        data["library"] = data["library"][:3]
        for index, item in enumerate(data["library"]):
            item["how"] = ["短说明", "这条动作说明需要换成两行来显示，说明长度不同也不能影响来源栏对齐。",
                           "稍长的说明。这一项有不同长度的内容。选择它之后仍然可以在来源栏直接删除。"][index]
        extra = copy.deepcopy(data["library"][0])
        extra.update(id="layout_squat", trigger="motion.layout_squat", name="布局测试动作", how="短说明", source="cloud", revision=1)
        data["library"].append(extra)
        route.fulfill(json=data)

    page.route(ui.base + "/api/pose/library", library)
    page.reload()
    page.wait_for_function("currentGameName.textContent !== '正在读取…'")
    body_library(page)
    cards = page.locator("#poseLibraryList > .pose-library-item")
    sync_api.expect(cards).to_have_count(4)
    positions = cards.evaluate_all("cards => cards.map(card => ({top:card.getBoundingClientRect().top, divider:card.querySelector('.pose-library-source').getBoundingClientRect().top}))")
    rows = {}
    for position in positions:
        rows.setdefault(round(position["top"]), []).append(position["divider"])
    assert any(len(row) > 1 for row in rows.values())
    for dividers in rows.values():
        assert max(dividers) - min(dividers) < 1
    assert ui.errors == []


def test_recorded_card_has_delete_without_opening_adjustments_and_stays_deleted(ui, tmp_path):
    page = ui.page
    store = CustomPoseStore(tmp_path / "recorded-test.json")
    store.capture(T_POSE, "测试自录动作")
    calls = []

    def read(route):
        route.fulfill(json={"ok": True, "poses": store.status(), "scores": {}})

    def remove(route):
        body = route.request.post_data_json
        calls.append(body["id"])
        route.fulfill(json={"ok": True, "removed": store.remove(body["id"]), "poses": store.status()})

    page.route(ui.base + "/api/pose/custom", read)
    page.route(ui.base + "/api/pose/custom/remove", remove)
    page.evaluate("async()=>{await (await import('/js/library.js')).refreshCustomPoses()}")
    body_library(page)
    card = page.locator('#customPoseList .custom-pose[data-id="custom1"]')
    sync_api.expect(card.locator(".custom-pose-panel")).to_be_hidden()
    sync_api.expect(card.get_by_role("button", name="删除", exact=True)).to_be_visible()
    page.once("dialog", lambda dialog: dialog.accept())
    card.get_by_role("button", name="删除", exact=True).click()
    sync_api.expect(card).to_have_count(0)
    assert calls == ["custom1"]
    assert CustomPoseStore(store.path).status() == []
    page.reload()
    page.wait_for_function("currentGameName.textContent !== '正在读取…'")
    body_library(page)
    sync_api.expect(card).to_have_count(0)
    assert ui.errors == []
