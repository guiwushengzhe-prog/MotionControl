"""Real desktop modules: public cached lists, background updates and live installs.

Uses only pytest, Playwright and the existing isolated browser API fixture.
"""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from test_web_optimization import ORIGIN, desktop, mock_browser


UPDATED = 1_700_000_000
PROFILE = {"id": "public", "title": "Saved public setup", "doc_type": "game_bundle"}


def cloud_routes(desktop, browse):
    state = SimpleNamespace(endpoint="https://one.test", requests=[], install_calls=0)

    def handle(route):
        url = urlparse(route.request.url)
        body = route.request.post_data_json if route.request.method == "POST" else None
        state.requests.append((url.path, parse_qs(url.query), body))
        if url.path == "/api/cloud/status":
            desktop.api.fulfill(route, {"endpoint": state.endpoint, "reachable": False})
        elif url.path == "/api/cloud/browse":
            browse(route, body)
        elif url.path == "/api/cloud/install":
            state.install_calls += 1
            desktop.api.fulfill(route, {"error": "live download unavailable"}, 502)
        else:
            desktop.api.fulfill(route, {"error": "unexpected cloud route"}, 404)

    desktop.page.route(ORIGIN + "/api/cloud/**", handle)
    return state


def open_cloud(page):
    page.click('[data-view="games"]')
    page.click("#cloudOpenBtn")


def wait_for_held(page, held):
    for _ in range(40):
        if held:
            return
        page.wait_for_timeout(50)
    assert held, "expected cloud request was not received"


def test_offline_snapshot_shows_true_update_time_and_install_fetches_live(desktop):
    page = desktop.page
    state = cloud_routes(desktop, lambda route, body: desktop.api.fulfill(route, {
        "profiles": [PROFILE], "cache": {"offline": True, "updated_at": UPDATED},
    }))
    open_cloud(page)
    page.wait_for_function("cloudStatus.textContent.includes('显示上次获取的结果')")
    stamp = page.evaluate("value=>new Date(value*1000).toLocaleString()", UPDATED)
    assert stamp in page.locator("#cloudStatus").inner_text()
    assert "Saved public setup" in page.locator("#cloudList").inner_text()
    status_queries = [query for path, query, _ in state.requests if path == "/api/cloud/status"]
    assert status_queries and all(query == {"endpoint_only": ["1"]} for query in status_queries)
    page.click("#cloudList button")
    page.wait_for_function("cloudStatus.textContent.includes('安装失败：live download unavailable')")
    assert state.install_calls == 1
    assert "Saved public setup" in page.locator("#cloudList").inner_text()
    assert page.locator("#cloudList button").is_enabled()


def test_background_refresh_keeps_rows_and_focus_until_new_data_arrives(desktop):
    page, held, calls = desktop.page, [], []

    def browse(route, body):
        calls.append(body)
        if len(calls) == 1:
            desktop.api.fulfill(route, {"profiles": [PROFILE], "cache": {"refreshing": True, "updated_at": UPDATED}})
        elif len(calls) == 2:
            held.append(route)
        else:
            desktop.api.fulfill(route, {"profiles": [{**PROFILE, "title": "New public setup"}], "cache": {"updated_at": UPDATED + 60}})

    cloud_routes(desktop, browse)
    open_cloud(page)
    page.wait_for_function("cloudStatus.textContent.includes('正在后台检查更新')")
    page.evaluate("window.previousCloudRow=cloudList.firstElementChild;cloudList.querySelector('button').focus()")
    page.wait_for_timeout(800)
    assert len(held) == 1
    assert "Saved public setup" in page.locator("#cloudList").inner_text()
    desktop.api.fulfill(held[0], {"profiles": [PROFILE], "cache": {"updated_at": UPDATED + 60}})
    page.wait_for_function("!cloudStatus.textContent.includes('正在后台检查更新')")
    assert page.evaluate("previousCloudRow===cloudList.firstElementChild && document.activeElement===cloudList.querySelector('button')")
    assert calls[1]["refresh"] is False
    page.click("#cloudRefreshBtn")
    page.wait_for_function("cloudList.textContent.includes('New public setup')")
    assert calls[-1]["refresh"] is True


def test_hidden_panel_stops_background_follow_up_requests(desktop):
    calls = []

    def browse(route, body):
        calls.append(body)
        desktop.api.fulfill(route, {"profiles": [PROFILE], "cache": {"refreshing": True, "updated_at": UPDATED}})

    cloud_routes(desktop, browse)
    open_cloud(desktop.page)
    desktop.page.wait_for_function("cloudStatus.textContent.includes('正在后台检查更新')")
    desktop.page.click("#cloudOpenBtn")
    desktop.page.wait_for_timeout(800)
    assert len(calls) == 1


def test_first_failure_is_visible_and_endpoint_change_clears_other_snapshot(desktop):
    page, offline = desktop.page, [False]

    def browse(route, body):
        if offline[0]:
            desktop.api.fulfill(route, {"error": "no successful cloud snapshot"}, 502)
        else:
            desktop.api.fulfill(route, {"profiles": [PROFILE], "cache": {"updated_at": UPDATED}})

    state = cloud_routes(desktop, browse)
    open_cloud(page)
    page.wait_for_function("cloudList.textContent.includes('Saved public setup')")
    state.endpoint, offline[0] = "https://two.test", True
    page.click("#cloudRefreshBtn")
    page.wait_for_function("cloudStatus.textContent.includes('no successful cloud snapshot')")
    assert page.locator("#cloudList").inner_text() == ""
    assert "还没有人公开分享" not in page.locator("#cloudStatus").inner_text()


def test_late_previous_game_response_does_not_replace_current_game(desktop):
    page, held = desktop.page, []

    def browse(route, body):
        if body["game_id"] == "generic":
            held.append(route)
        else:
            desktop.api.fulfill(route, {"profiles": [{**PROFILE, "title": "Second game setup"}], "cache": {"updated_at": UPDATED}})

    cloud_routes(desktop, browse)
    open_cloud(page)
    wait_for_held(page, held)
    page.evaluate("async()=>{const state=await import('/js/state.js');state.gameProfile.selected={id:'second',name:'Second game'};const cloud=await import('/js/cloud.js');await cloud.cloudRefresh();}")
    page.wait_for_function("cloudList.textContent.includes('Second game setup')")
    desktop.api.fulfill(held[0], {"profiles": [PROFILE], "cache": {"updated_at": UPDATED}})
    page.wait_for_timeout(50)
    assert "Second game setup" in page.locator("#cloudList").inner_text()
    assert "Saved public setup" not in page.locator("#cloudList").inner_text()


POSE = {"id": "public_pose", "trigger": "pose.public_pose", "name": "Public action",
        "how": "Raise both arms", "group": "body", "revision": 2, "source": "cloud",
        "ratings": {}, "body_parts": {}, "demo": {"frames": []}, "installed_revision": 0}


def open_pose_cloud(page):
    page.click('[data-view="games"]')
    page.click('#mapTabs [data-tab="body"]')
    page.click("#poseCloudBtn")


def test_offline_action_library_shows_cache_time_and_download_is_live(desktop):
    page, installs = desktop.page, []

    def handle(route):
        if urlparse(route.request.url).path.endswith("/install"):
            installs.append(route.request.post_data_json)
            desktop.api.fulfill(route, {"error": "signed download unavailable"}, 502)
        else:
            desktop.api.fulfill(route, {"actions": [POSE], "cache": {"offline": True, "updated_at": UPDATED}})

    page.route(ORIGIN + "/api/pose/cloud**", handle)
    open_pose_cloud(page)
    page.wait_for_function("poseCloudStatus.textContent.includes('显示上次获取的结果')")
    stamp = page.evaluate("value=>new Date(value*1000).toLocaleString()", UPDATED)
    assert stamp in page.locator("#poseCloudStatus").inner_text()
    page.click("#poseCloudList button")
    page.wait_for_function("poseCloudStatus.textContent.includes('signed download unavailable')")
    assert installs == [{"id": "public_pose"}]
    assert page.locator("#poseCloudList button").is_enabled()


def test_action_install_state_is_not_undone_by_older_background_reply(desktop):
    page, calls, held = desktop.page, [], []

    def handle(route):
        if urlparse(route.request.url).path.endswith("/install"):
            desktop.api.fulfill(route, {"library": [{**POSE, "installed_revision": 2}]})
        else:
            calls.append(route)
            if len(calls) == 1:
                desktop.api.fulfill(route, {"actions": [POSE], "cache": {"refreshing": True, "updated_at": UPDATED}})
            else:
                held.append(route)

    page.route(ORIGIN + "/api/pose/cloud**", handle)
    open_pose_cloud(page)
    page.wait_for_function("poseCloudStatus.textContent.includes('正在后台检查更新')")
    wait_for_held(page, held)
    page.click("#poseCloudList button")
    page.wait_for_function("poseCloudStatus.textContent.includes('官方的动作都下载了')")
    assert page.locator("#poseLibraryList").inner_text().count("Public action") == 1
    desktop.api.fulfill(held[0], {"actions": [POSE], "cache": {"updated_at": UPDATED + 60}})
    page.wait_for_timeout(50)
    assert page.locator("#poseCloudList").inner_text() == ""
    assert "官方的动作都下载了" in page.locator("#poseCloudStatus").inner_text()


def test_closing_action_panel_discards_late_reply_and_stops_follow_up(desktop):
    page, held = desktop.page, []
    page.route(ORIGIN + "/api/pose/cloud", lambda route: held.append(route))
    open_pose_cloud(page)
    wait_for_held(page, held)
    page.click("#poseCloudCloseBtn")
    desktop.api.fulfill(held[0], {"actions": [POSE], "cache": {"refreshing": True, "updated_at": UPDATED}})
    page.wait_for_timeout(800)
    assert len(held) == 1
    assert page.locator("#poseCloudList").inner_text() == ""
