"""真实浏览器验证同一卡片的触发规则；不调用摄像头或游戏输出。"""
from urllib.parse import urlparse

import pytest
expect = pytest.importorskip("playwright.sync_api").expect

from motioncontrol.custom_poses import CustomPoseStore
from test_custom_poses import ARMS_UP, T_POSE
from test_web_optimization import ORIGIN, desktop
from test_web_ui import browser as mock_browser


@pytest.fixture
def recorded(desktop, tmp_path):
    store = CustomPoseStore(tmp_path / "custom_poses.json")
    store.capture(T_POSE, "测试动作")
    store.append_frame("custom1", ARMS_UP)
    api, page = desktop.api, desktop.page
    api.custom_poses = store.status()

    def update(route):
        body = route.request.post_data_json
        api.requests.append((urlparse(route.request.url).path, body))
        store.update(body["id"], match_mode=body["match_mode"])
        api.custom_poses = store.status()
        api.fulfill(route, {"poses": api.custom_poses})

    page.route(ORIGIN + "/api/pose/custom/update", update)
    page.evaluate("async()=>{await (await import('/js/library.js')).refreshCustomPoses()}")
    page.click('nav [data-view="games"]')
    page.click('#mapTabs [data-tab="body"]')
    yield desktop, store


def test_one_rule_applies_to_all_poses_and_one_mapping(recorded):
    desktop, store = recorded
    page = desktop.page
    card = page.locator('.custom-pose[data-id="custom1"]')
    card.get_by_role("button", name="调整", exact=True).click()
    expect(card.get_by_role("button", name="按顺序完成", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(card.locator('.pose-arrow')).to_have_text("→")
    card.get_by_role("button", name="任意一个即可", exact=True).click()
    expect(card.locator('.pose-library-how')).to_have_text("2 个姿势，任意一个即可")
    expect(card.locator('.pose-arrow')).to_have_text("或")
    assert card.locator('.pose-cell').count() == 2
    assert card.locator('.awaiting').count() == 0
    assert "每步最多" not in card.locator('.custom-pose-tools').inner_text()
    assert card.locator('input[type="checkbox"]').count() == 1  # 仅原来的启用开关
    assert card.locator('.pose-strip > .inline-form > .pose-add + .custom-pose-mode').count() == 1
    assert card.locator('.custom-pose-mode select').count() == 0
    add_box = card.locator('.pose-add').bounding_box()
    mode_box = card.locator('.custom-pose-mode').bounding_box()
    assert mode_box["x"] >= add_box["x"] + add_box["width"]
    assert abs((mode_box["y"] + mode_box["height"] / 2)
               - (add_box["y"] + add_box["height"] / 2)) < 2
    card.locator('.custom-pose-key').click()
    assert page.locator('.binding-row[data-trigger="pose.custom1"]').count() == 1
    assert page.locator('.custom-pose').count() == 1
    assert store.status()[0]["match_mode"] == "any"

    page.reload()
    page.click('nav [data-view="games"]')
    page.click('#mapTabs [data-tab="body"]')
    card.get_by_role("button", name="调整", exact=True).click()
    expect(card.get_by_role("button", name="任意一个即可", exact=True)).to_have_attribute("aria-pressed", "true")
    card.get_by_role("button", name="按顺序完成", exact=True).click()
    expect(card.locator('.pose-arrow')).to_have_text("→")
    expect(card.locator('.custom-pose-tools')).to_contain_text("每步最多")


def test_failed_save_restores_rule_instead_of_showing_unsaved_selection(recorded):
    desktop, store = recorded
    page = desktop.page
    page.route(ORIGIN + "/api/pose/custom/update",
               lambda route: desktop.api.fulfill(route, {"error": "保存失败"}, 400))
    card = page.locator('.custom-pose[data-id="custom1"]')
    card.get_by_role("button", name="调整", exact=True).click()
    card.get_by_role("button", name="任意一个即可", exact=True).click()
    expect(page.locator('#customPoseStatus')).to_have_text("保存失败")
    expect(card.get_by_role("button", name="按顺序完成", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(card.get_by_role("button", name="任意一个即可", exact=True)).to_be_enabled()
    assert store.status()[0]["match_mode"] == "sequence"
