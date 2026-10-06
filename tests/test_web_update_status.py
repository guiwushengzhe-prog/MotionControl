"""真实浏览器：更新状态看得见；功能更新弹一次说明，系统维护不打扰。"""
import json
import time

import pytest
expect = pytest.importorskip("playwright.sync_api").expect

from test_web_optimization import ORIGIN, desktop  # noqa: F401
from test_web_ui import browser as mock_browser  # noqa: F401

NOTES = {"version": "1.9.0", "releases": [{"version": "1.9.0", "date": "2026-10-06", "kind": "feature", "sections": [
    {"title": "新增", "intro": "", "items": ["**扫码连接。** 手机扫一下就连上。", "<b>不当标签</b>"]}]}]}


def serve(page, api, payload, seen=None):
    def handle(route):
        if route.request.method == "POST":
            body = route.request.post_data_json or {}
            if seen is not None:
                seen.append((route.request.url.rsplit("/", 1)[-1], body))
            api.fulfill(route, {**payload, "notes": None})
        else:
            api.fulfill(route, payload)
    page.route(ORIGIN + "/api/app-update**", handle)
    page.evaluate("async()=>{await (await import('/js/updates.js')).refreshUpdates()}")


def base(**extra):
    return {"version": "1.9.0", "updatable": True, "state": "current", "staged": None, "events": [],
            "notes": None, "updated_at": None, **extra}


def test_a_feature_update_opens_its_notes_once_and_marks_them_seen(desktop):
    page, seen = desktop.page, []
    serve(page, desktop.api, base(notes=NOTES, events=[
        {"type": "updated", "from": "1.8.0", "to": "1.9.0", "at": 1_790_000_000}]), seen)
    dialog = page.locator("#updateNotesDialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#updateNotesTitle")).to_have_text("已更新到 1.9.0")
    expect(page.locator("#updateNotesBody strong")).to_have_text("扫码连接。")
    # 日志里的尖括号当文字显示，不会变成标签。
    expect(page.locator("#updateNotesBody li").nth(1)).to_have_text("<b>不当标签</b>")
    assert page.evaluate("helpBtn.classList.contains('has-update')")
    page.click("#closeUpdateNotesBtn")
    expect(dialog).to_be_hidden()
    page.wait_for_function("!helpBtn.classList.contains('has-update')")
    assert seen == [("seen", {"version": "1.9.0"})]
    page.click("#helpBtn")
    expect(page.locator("#updateStatus")).to_have_text("已是最新")


def test_a_maintenance_update_is_visible_in_the_menu_without_any_popup(desktop):
    page = desktop.page
    serve(page, desktop.api, base(version="1.9.1", events=[
        {"type": "updated", "from": "1.9.0", "to": "1.9.1", "at": 1_790_000_000}]))
    page.wait_for_timeout(300)
    expect(page.locator("#updateNotesDialog")).to_be_hidden()
    assert not page.evaluate("helpBtn.classList.contains('has-update')")
    assert page.locator("#notice").is_hidden()
    page.click("#helpBtn")
    expect(page.locator("#updateStatus")).to_have_text("已是最新")
    expect(page.locator("#appVersion")).to_have_text("1.9.1")


def test_downloaded_feature_update_is_announced_but_a_system_one_is_not(desktop):
    page = desktop.page
    serve(page, desktop.api, base(state="ready", staged={"version": "1.10.0", "kind": "system"}))
    assert page.locator("#notice").is_hidden()
    assert not page.evaluate("helpBtn.classList.contains('has-update')")
    page.click("#helpBtn")
    expect(page.locator("#updateStatus")).to_have_text("新版 1.10.0 下次打开生效")
    page.click("#helpBtn")
    page.unroute(ORIGIN + "/api/app-update**")
    serve(page, desktop.api, base(state="ready", staged={"version": "1.11.0", "kind": "feature"}))
    expect(page.locator("#notice")).to_contain_text("新版本 1.11.0 已下载（有新功能）")
    assert page.evaluate("helpBtn.classList.contains('has-update')")


def test_rollback_and_manual_check_are_reported_in_the_menu(desktop):
    page, seen = desktop.page, []
    serve(page, desktop.api, base(state="failed", error="连不上更新服务器", events=[
        {"type": "rolled_back", "from": "1.10.0", "to": "1.9.0", "at": time.time() - 60}]), seen)
    page.click("#helpBtn")
    expect(page.locator("#updateStatus")).to_have_text("上次更新没装上，已退回")
    expect(page.locator("#updateStatus")).to_have_class("warn")
    page.click("#updateCheckBtn")
    expect(page.locator("#helpMenu")).to_be_visible()
    assert seen[-1][0] == "check"


def test_source_checkout_hides_the_check_button(desktop):
    page = desktop.page
    serve(page, desktop.api, base(updatable=False, state="unknown"))
    page.click("#helpBtn")
    expect(page.locator("#updateStatus")).to_have_text("从源码运行")
    expect(page.locator("#updateCheckBtn")).to_be_hidden()
    assert json.loads(page.evaluate("JSON.stringify(document.querySelectorAll('dialog[open]').length)")) == 0


def test_a_failed_check_says_one_short_line_with_the_detail_on_hover(desktop):
    page = desktop.page
    serve(page, desktop.api, base(state="failed", error="连不上更新服务器", events=[
        {"type": "rolled_back", "from": "1.10.0", "to": "1.9.0", "at": 1_000_000_000}]))
    page.click("#helpBtn")
    expect(page.locator("#updateStatus")).to_have_text("暂时没能检查更新")
    assert page.get_attribute("#updateStatus", "title") == "连不上更新服务器"
    assert page.locator("#helpMenu .menu-update").count() == 1, "更新信息只占菜单底部一行"
