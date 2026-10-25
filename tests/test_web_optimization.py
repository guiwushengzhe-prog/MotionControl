"""Browser regressions with real desktop modules and an isolated, mocked local API.

No Windows runtime, camera, model, or user configuration is required. Requests are
intercepted in the browser, including deliberately delayed mutation responses.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from motioncontrol.version import VERSION

playwright = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://motioncontrol.test"


class LocalApi:
    def __init__(self):
        self.requests = []
        self.held_catalog = {}
        self.hold_catalog = False
        self.hold_overrides = False
        self.held_overrides = None
        self.hold_output = False
        self.held_output = None
        self.fail_view = False
        self.fail_hand_reads = 0
        self.fail_audio_reads = 0
        self.fail_overrides = False
        self.override_failure_status = 503
        self.hold_models = False
        self.macros = []
        self.custom_poses = []
        self.hold_libraries = False
        self.held_libraries = {}
        self.recording = {"state": "idle", "frames": 0, "remaining_s": 0}
        self.hand = {"config": {"enabled": True, "horizontal_hand": "right",
                                 "vertical_hand": "off", "sensitivity": 70, "deadzone": .1}}
        self.runtime = {"body_mode": "computer", "camera": {"running": False},
                        "kernel": {"width": 640, "height": 480, "now": 100,
                                   "head": {"algorithm": "pnp", "horizontal_algorithm": "roll_tilt",
                                            "enabled": False, "calibrated": False,
                                            "sensitivity_x": 58, "sensitivity_y": 46},
                                   "pose": None, "zones": {}, "vertical_look": {"enabled": False}}}
        self.profile = {"id": "generic", "selected_id": "generic", "name": "Default game",
                        "source": {"verified": True}, "overrides": {},
                        "bindings": {"zones": {"leftHand": {"action": {"type": "gamepad", "target": "X"}}},
                                     "motions": {}, "poses": {}, "voice": {}}}
        self.output = {"enabled": False, "mode": "mouse", "mouse_speed_x": 600,
                       "gamepad_gain": 1, "gamepad_connected": True}
        self.input = {"audio_source": "computer", "audio_mode": "computer", "phone_ws_urls": [],
                      "voice": {"connected": True}}
        self.voice = {"available": True, "model_ready": True, "connected": True,
                      "audio_ready": True, "wake_word": "体感", "mappings": []}
        self.commands = []
        self.shutting_down = False

    @staticmethod
    def fulfill(route, data, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps({"ok": status < 400, **data}))

    def handle(self, route):
        url = urlparse(route.request.url)
        path = url.path
        if not path.startswith("/api/"):
            file = ROOT / "web" / ("index.html" if path == "/" else path.lstrip("/"))
            content_type = "text/css" if file.suffix == ".css" else "text/javascript" if file.suffix == ".js" else "text/html"
            route.fulfill(path=str(file), content_type=content_type)
            return
        if path == "/api/data-backup/preview":
            self.requests.append((path, None))
            self.fulfill(route, {"preview": {"token": "a" * 64, "same_data": True, "created_at": "2026-10-26T08:00:00Z",
                "games": 6, "custom_games": 1, "macros": 2, "poses": 1,
                "files": [{"name": "game_profile_selection.json", "bytes": 2048}]}})
            return
        body = route.request.post_data_json if route.request.method == "POST" else None
        self.requests.append((path, body))
        if path == "/api/shutdown":
            self.shutting_down = True
            self.fulfill(route, {"shutting_down": True, "pid": 123})
            return
        if path == "/api/data-backup/restore":
            self.shutting_down = True
            self.fulfill(route, {"pending": True, "shutting_down": True, "pid": 123})
            return
        if path == "/api/health":
            if self.shutting_down:
                route.abort("connectionrefused")
            else:
                self.fulfill(route, {"app": "MotionControl", "pid": 123})
            return
        if path == "/api/game-profiles/catalog":
            query = parse_qs(url.query).get("q", [""])[0]
            if self.hold_catalog and query:
                self.held_catalog[query] = route
                return
            data = {"games": [{"id": "generic", "name": "Default game"}], "count": 1}
        elif path == "/api/kernel/status": data = self.runtime
        elif path == "/api/hand-mouse/config":
            if self.fail_hand_reads:
                self.fail_hand_reads -= 1
                self.fulfill(route, {"error": "temporarily unavailable"}, 503)
                return
            data = {"hand_mouse": self.hand}
        elif path == "/api/view-control":
            if self.fail_view:
                self.fulfill(route, {"error": "rejected"}, 400)
                return
            self.hand["config"].update(enabled=body["horizontal"] in ("left", "right") or body["vertical"] in ("left", "right"),
                                       horizontal_hand=body["horizontal"] if body["horizontal"] in ("left", "right") else "off",
                                       vertical_hand=body["vertical"] if body["vertical"] in ("left", "right") else "off")
            self.runtime["kernel"]["head"].update(enabled=body["horizontal"] not in ("left", "right", "off"),
                                                 horizontal_algorithm=body["horizontal"])
            self.runtime["kernel"]["vertical_look"]["enabled"] = body["vertical"] == "head"
            data = {**self.runtime, "hand_mouse": self.hand}
        elif path == "/api/game-profiles/selected": data = {"profile": self.profile}
        elif path == "/api/game-profiles/select":
            if self.override_failure_status == 409:
                self.fail_overrides = False
            data = {"profile": self.profile}
        elif path == "/api/output/actions":
            data = {"actions": {"gamepad": {"targets": ["A", "B", "X", "Y"]},
                                "keyboard": {"free_text": True}, "voice_release": {}}}
        elif path == "/api/game-profiles/overrides":
            if self.fail_overrides:
                self.fulfill(route, {"error": "draft could not be saved"}, self.override_failure_status)
                return
            self.profile["overrides"] = body["overrides"]
            for key, value in body["overrides"].items():
                group, name = key.split(".", 1)
                groups = {"zone": "zones", "motion": "motions", "pose": "poses", "voice": "voice"}
                if group in groups:
                    self.profile["bindings"][groups[group]][name] = value if value is not None else {"disabled": True}
            if self.hold_overrides:
                self.held_overrides = route
                return
            data = {"profile": self.profile}
        elif path == "/api/output/config":
            self.output.update(body)
            if self.hold_output:
                self.held_output = (route, copy.deepcopy(self.output))
                return
            data = self.output
        elif path == "/api/output/stop":
            self.output["enabled"] = False
            data = self.output
        elif path == "/api/input/stop":
            self.output["enabled"] = False
            self.runtime["camera"].update(running=False, lifecycle="stopped")
            self.input["body_enabled"] = False
            self.voice.update(connected=False, audio_ready=False)
            self.input["voice"] = self.voice
            data = {**self.runtime, "output": self.output, "recognition_stopped": True, "stop_error": ""}
        elif path == "/api/input/source":
            self.runtime["body_mode"] = body["source"]
            self.runtime["camera"]["running"] = body["source"] == "computer" and body.get("enabled", True)
            self.input["body_enabled"] = body.get("enabled", True)
            self.output["enabled"] = False
            data = self.runtime
        elif path == "/api/output-status": data = self.output
        elif path == "/api/input/status": data = self.input
        elif path == "/api/voice/status": data = self.voice
        elif path == "/api/voice/commands": data = {"commands": self.commands}
        elif path == "/api/voice/check": data = {"available": False}
        elif path == "/api/pose/custom":
            if self.hold_libraries:
                self.held_libraries[path] = route
                return
            data = {"poses": self.custom_poses, "scores": {}}
        elif path == "/api/macros":
            if self.hold_libraries:
                self.held_libraries[path] = route
                return
            data = {"macros": self.macros}
        elif path == "/api/pose/library": data = {"library": []}
        elif path == "/api/models":
            if self.hold_models:
                return
            data = {"models": [{"available": True}], "version": VERSION}
        elif path == "/api/audio/devices":
            if self.fail_audio_reads:
                self.fail_audio_reads -= 1
                self.fulfill(route, {"error": "audio scan unavailable"}, 503)
                return
            data = {"devices": []}
        elif path == "/api/camera/config":
            if body and "device" in body:
                self.runtime["camera"].update(running=True, camera_device=body["device"],
                                             camera_index=int(body["device"]))
                self.input["body_enabled"] = True
                self.runtime["body_mode"] = "computer"
            data = {"camera_index": self.runtime["camera"].get("camera_index", 0),
                    "camera_device": self.runtime["camera"].get("camera_device", "0"),
                    "rotation": "auto", **self.runtime}
        elif path == "/api/pose/trigger-recording": data = {"choices": [], "recording": {"config": {}, "state": "off"}}
        elif path == "/api/pose/record": data = {"recording": self.recording}
        elif path == "/api/recordings": data = {"count": 1 if self.recording["state"] == "done" else 0, "bytes": 64}
        elif path == "/api/cloud/status": data = {"reachable": True, "endpoint": "https://example.test"}
        elif path == "/api/cloud/browse": data = {"profiles": [{"id": "cloud-1", "title": "Shared setup", "doc_type": "game_bundle"}]}
        elif path == "/api/cloud/install":
            self.profile["name"] = "Installed game"
            data = {"installed": {"title": "Shared setup", "doc_type": "game_bundle", "revision_no": 1, "sha256": "a" * 64}}
        else: data = {}
        self.fulfill(route, data)


@pytest.fixture(scope="module")
def mock_browser():
    with playwright.sync_playwright() as p:
        executable = shutil.which("chromium") or shutil.which("google-chrome")
        try:
            browser = p.chromium.launch(executable_path=executable, args=["--no-sandbox"])
        except Exception as exc:
            try:
                browser = p.chromium.launch(channel="chrome", args=["--no-sandbox"])
            except Exception:
                pytest.skip(f"Browser unavailable: {exc}")
        yield browser
        browser.close()


@pytest.fixture
def desktop(mock_browser):
    api = LocalApi()
    context = mock_browser.new_context(viewport={"width": 1280, "height": 820}, device_scale_factor=2)
    context.add_init_script("localStorage.setItem('motioncontrol_tutorial_v2', JSON.stringify({seen:1,offered:true,done:[],skipped:[]}));localStorage.setItem('motioncontrol_tips_seen', JSON.stringify(['help','gameMenu']));")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.route(ORIGIN + "/**", api.handle)
    page.goto(ORIGIN)
    page.wait_for_function("document.querySelector('#currentGameName').textContent === 'Default game'")
    page.wait_for_function("!document.querySelector('#viewHorizontalSource').disabled")
    yield SimpleNamespace(page=page, api=api, errors=errors)
    assert errors == []
    context.close()


def test_top_game_name_opens_picker_directly_and_focuses_search(desktop):
    page, api = desktop.page, desktop.api
    page.click('.game-chip[data-open-game-picker="true"]')
    assert page.locator('#gamesPanel').is_visible()
    assert page.locator('#gamePicker').is_visible()
    assert page.locator('#profileSearch').evaluate('(input)=>document.activeElement===input')
    assert page.locator('#switchGameBtn').get_attribute('aria-expanded') == 'true'
    assert page.locator('.game-chip[data-open-game-picker="true"]').get_attribute('aria-expanded') == 'true'
    page.click('#switchGameBtn')
    assert page.locator('#gamePicker').is_hidden()
    assert page.locator('.game-chip[data-open-game-picker="true"]').get_attribute('aria-expanded') == 'false'
    page.click('.game-chip[data-open-game-picker="true"]')
    assert page.locator('#gamePicker').is_visible()


def test_search_keeps_newest_results_when_old_request_arrives_last(desktop):
    page, api = desktop.page, desktop.api
    api.hold_catalog = True
    page.evaluate("async()=>{const m=await import('/js/mapping.js');profileSearch.value='Apex';window.oldSearch=m.searchProfiles();}")
    page.wait_for_timeout(50)
    page.evaluate("async()=>{const m=await import('/js/mapping.js');profileSearch.value='Elden';window.newSearch=m.searchProfiles();}")
    page.wait_for_timeout(50)
    api.fulfill(api.held_catalog["Elden"], {"games": [{"id": "elden", "name": "Elden Ring"}], "count": 1})
    page.wait_for_function("profileResults.textContent.includes('Elden Ring')")
    api.fulfill(api.held_catalog["Apex"], {"games": [{"id": "apex", "name": "Apex Legends"}], "count": 1})
    page.evaluate("Promise.all([window.oldSearch,window.newSearch])")
    assert page.locator("#profileSearch").input_value() == "Elden"
    assert "Elden Ring" in page.locator("#profileResults").inner_text()


def test_empty_search_removes_stale_game_choices_and_can_search_again(desktop):
    page, api = desktop.page, desktop.api
    assert page.locator("#profileResults .game-result").count() == 1
    api.hold_catalog = True
    page.evaluate("async()=>{const m=await import('/js/mapping.js');profileSearch.value='未收录';window.emptySearch=m.searchProfiles();}")
    page.wait_for_timeout(50)
    api.fulfill(api.held_catalog["未收录"], {"games": [], "count": 0})
    page.evaluate("window.emptySearch")
    assert page.locator("#profileResults .game-result").count() == 0
    assert "没搜到" in page.locator("#profileResultsInfo").text_content()
    api.hold_catalog = False
    page.evaluate("async()=>{const m=await import('/js/mapping.js');profileSearch.value='';await m.searchProfiles();}")
    assert page.locator("#profileResults .game-result").count() == 1
    assert not any(path == "/api/game-profiles/select" for path, _ in api.requests)


@pytest.mark.parametrize("rejected", [False, True])
def test_view_control_uses_one_complete_request_and_truthful_failure(desktop, rejected):
    page, api = desktop.page, desktop.api
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="view"]')
    api.fail_view = rejected
    api.requests.clear()
    page.select_option("#viewHorizontalSource", "roll_tilt")
    page.wait_for_function("!viewHorizontalSource.disabled && !viewControlStatus.textContent.includes('正在保存')")
    # Phrase validation uses POST for a read; it is not a configuration mutation.
    mutations = [(path, body) for path, body in api.requests if body is not None and path != "/api/voice/check"]
    assert mutations == [("/api/view-control", {"horizontal": "roll_tilt", "vertical": "off"})]
    if rejected:
        assert page.locator("#viewHorizontalSource").input_value() == "right"
        assert "已读取当前设置" in page.locator("#viewControlStatus").inner_text()
        assert "已恢复" not in page.locator("#viewControlStatus").inner_text()
    else:
        assert page.locator("#viewHorizontalSource").input_value() == "roll_tilt"


def test_cloud_install_waits_for_mapping_draft_to_finish_saving(desktop):
    page, api = desktop.page, desktop.api
    page.click('[data-view="games"]')
    api.hold_overrides = True
    row = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    row.locator('.binding-target-box select').select_option("Y")
    page.click("#cloudOpenBtn")
    page.wait_for_selector("#cloudList button")
    page.click("#cloudList button")
    page.wait_for_function("mappingFields.disabled")
    page.wait_for_timeout(50)
    assert api.held_overrides is not None
    assert not any(path == "/api/cloud/install" for path, _ in api.requests)
    api.fulfill(api.held_overrides, {"profile": api.profile})
    page.wait_for_function("currentGameName.textContent === 'Installed game'")
    assert api.profile["bindings"]["zones"]["leftHand"]["action"]["target"] == "Y"
    page.wait_for_function("!mappingFields.disabled")


def test_emergency_stop_wins_over_late_output_settings_response(desktop):
    page, api = desktop.page, desktop.api
    api.output["enabled"] = True
    page.evaluate("async()=>{const d=await import('/js/devices.js');await d.refreshOutput()}")
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="view"]')
    api.hold_output = True
    page.select_option("#outputMode", "gamepad")
    page.wait_for_timeout(50)
    assert api.held_output is not None
    page.click("#mainActionBtn")
    # 等暂停确认后再放回旧请求，验证旧设置不能覆盖暂停结果。
    page.wait_for_function("notice.textContent.includes('控制已暂停')")
    assert "暂停" not in page.locator("#mainActionBtn").inner_text()
    route, old_data = api.held_output
    api.fulfill(route, old_data)
    page.wait_for_function("notice.textContent.includes('中断')")
    assert "暂停" not in page.locator("#mainActionBtn").inner_text()
    assert api.output["enabled"] is False


def test_conflict_button_keeps_focus_and_hidden_canvas_does_not_draw(desktop):
    page, api = desktop.page, desktop.api
    api.input.update(phone_ignored=True, mobile_pose_connected=True)
    page.evaluate("async()=>{const d=await import('/js/devices.js');await d.refreshInput();window.conflictButton=setupConflicts.querySelector('button');conflictButton.focus();const p=await import('/js/play.js');await p.refreshKernel();}")
    assert page.evaluate("document.activeElement === window.conflictButton && setupConflicts.querySelector('button') === window.conflictButton")
    page.click('[data-view="games"]')
    calls = page.evaluate("async()=>{const c=canvas.getContext('2d'),original=c.clearRect;let count=0;c.clearRect=function(...args){count++;return original.apply(this,args)};const p=await import('/js/play.js');await p.refreshKernel();c.clearRect=original;return count}")
    assert calls == 0


def test_canvas_budget_and_popup_fit_the_visible_viewport(desktop):
    page = desktop.page
    assert page.evaluate("canvas.width*canvas.height <= 1600000")
    page.set_viewport_size({"width": 390, "height": 700})
    geometry = page.evaluate("""async()=>{
      const {positionPopup}=await import('/js/core.js');
      document.documentElement.style.fontSize='26px';
      const anchor=document.createElement('button'),popup=document.createElement('div');
      anchor.style.cssText='position:fixed;top:650px;left:300px;width:60px;height:30px';
      popup.className='zone-point-menu';popup.style.height='200px';
      document.body.append(anchor,popup);positionPopup(anchor,popup);
      const r=popup.getBoundingClientRect();const result={left:r.left,right:r.right,top:r.top,bottom:r.bottom};anchor.remove();popup.remove();return result;
    }""")
    assert geometry["left"] >= 8
    assert geometry["right"] <= 382
    assert geometry["top"] >= 8
    assert geometry["bottom"] <= 692


def test_failed_draft_blocks_install_without_poisoning_the_operation_queue(desktop):
    page, api = desktop.page, desktop.api
    page.click('[data-view="games"]')
    api.fail_overrides = True
    row = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    row.locator('.binding-target-box select').select_option("Y")
    page.click("#cloudOpenBtn")
    page.click("#cloudList button")
    page.wait_for_function("cloudStatus.textContent.includes('draft could not be saved')")
    assert not any(path == "/api/cloud/install" for path, _ in api.requests)
    assert row.locator('.binding-target-box select').input_value() == "Y"
    assert page.locator("#mappingFields").is_enabled()
    api.fail_overrides = False
    page.click("#cloudList button")
    page.wait_for_function("currentGameName.textContent === 'Installed game'")


def test_initial_view_failure_recovers_while_optional_models_request_is_pending(desktop):
    page, api = desktop.page, desktop.api
    api.fail_hand_reads = 1
    api.fail_audio_reads = 1
    api.hold_models = True
    before_audio_reads = sum(path == "/api/audio/devices" for path, _ in api.requests)
    page.reload()
    page.wait_for_function("currentGameName.textContent === 'Default game'")
    page.wait_for_function("!mainActionBtn.disabled")
    page.wait_for_function("!viewHorizontalSource.disabled", timeout=10000)
    assert page.locator("#viewHorizontalSource").input_value() == "right"
    page.wait_for_timeout(2700)
    assert sum(path == "/api/audio/devices" for path, _ in api.requests) >= before_audio_reads + 2


def test_conflicted_mapping_retry_reselects_before_saving_the_same_draft(desktop):
    page, api = desktop.page, desktop.api
    page.click('[data-view="games"]')
    api.fail_overrides = True
    api.override_failure_status = 409
    row = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    row.locator('.binding-target-box select').select_option("Y")
    page.wait_for_function("!retryProfileSaveBtn.hidden && retryProfileSaveBtn.textContent.includes('合并')")
    page.click("#retryProfileSaveBtn")
    page.wait_for_function("retryProfileSaveBtn.hidden && profileSaveStatus.textContent === '已保存'")
    paths = [path for path, _ in api.requests if path in ("/api/game-profiles/select", "/api/game-profiles/overrides")]
    assert paths[-2:] == ["/api/game-profiles/select", "/api/game-profiles/overrides"]
    assert api.profile["bindings"]["zones"]["leftHand"]["action"]["target"] == "Y"
    assert row.locator('.binding-target-box select').input_value() == "Y"
    assert page.locator("#mappingFields").is_enabled()


@pytest.mark.parametrize("method,status,expected_calls", [("GET", 503, 2), ("GET", 403, 1), ("POST", 503, 1)])
def test_transient_retry_is_bounded_and_never_repeats_mutations(desktop, method, status, expected_calls):
    page, seen = desktop.page, []

    def reply(route):
        seen.append(route.request.method)
        desktop.api.fulfill(route, {"error": "临时不可用"} if len(seen) == 1 else {"value": 42},
                            status if len(seen) == 1 else 200)

    page.route(ORIGIN + "/api/recovery-test", reply)
    result = page.evaluate("""async method=>{
        const {api}=await import('/js/core.js');
        try{return await api('/api/recovery-test',{method})}catch(error){return {error:error.message,status:error.status}}
    }""", method)
    assert len(seen) == expected_calls
    assert result == ({"ok": True, "value": 42} if expected_calls == 2 else {"error": "临时不可用", "status": status})


def test_clean_page_follows_reloaded_game_without_writing_back(desktop):
    page, api = desktop.page, desktop.api
    api.profile["configuration_revision"] = "before"
    page.evaluate("async()=>{const m=await import('/js/mapping.js');await m.refreshProfile()}")
    api.profile["name"] = "自动恢复的游戏"
    api.profile["bindings"]["zones"]["leftHand"]["action"]["target"] = "Y"
    api.profile["configuration_revision"] = "after"
    page.evaluate("async()=>{const m=await import('/js/mapping.js');m.syncGameProfile({revision:'after'})}")
    page.wait_for_function("currentGameName.textContent === '自动恢复的游戏'")
    assert page.locator('.binding-row[data-trigger="zone.leftHand"] .binding-target-box select').input_value() == "Y"
    assert not any(path == "/api/game-profiles/overrides" for path, _ in api.requests)


def test_background_change_preserves_draft_and_retry_merges_latest_other_binding(desktop):
    page, api = desktop.page, desktop.api
    api.profile["configuration_revision"] = "before"
    page.evaluate("async()=>{const m=await import('/js/mapping.js');await m.refreshProfile()}")
    page.click('[data-view="games"]')
    row = page.locator('.binding-row[data-trigger="zone.leftHand"]')
    row.locator('.binding-target-box select').select_option("Y")
    other = {"action": {"type": "gamepad", "target": "B", "behavior": "hold"}}
    api.profile["overrides"]["zone.rightHand"] = other
    api.profile["bindings"]["zones"]["rightHand"] = other
    api.profile["configuration_revision"] = "after"
    page.evaluate("async()=>{const m=await import('/js/mapping.js');m.syncGameProfile({revision:'after'})}")
    page.wait_for_timeout(500)
    assert row.locator('.binding-target-box select').input_value() == "Y"
    assert not any(path == "/api/game-profiles/overrides" for path, _ in api.requests)
    assert "未保存的修改已保留" in page.locator("#profileSaveStatus").text_content()
    page.click("#retryProfileSaveBtn")
    page.wait_for_function("profileSaveStatus.textContent === '已保存'")
    assert api.profile["overrides"]["zone.rightHand"] == other
    assert api.profile["bindings"]["zones"]["leftHand"]["action"]["target"] == "Y"
    writes = [body for path, body in api.requests if path == "/api/game-profiles/overrides"]
    assert len(writes) == 1 and writes[0]["expected_revision"] == "after"


def test_delayed_background_read_cannot_undo_a_finished_game_selection(desktop):
    page, api = desktop.page, desktop.api
    api.profile["configuration_revision"] = "initial"
    api.profile["mode"] = {"id": None, "name": "默认"}
    page.evaluate("async()=>{await (await import('/js/mapping.js')).refreshProfile()}")
    old = copy.deepcopy(api.profile)
    held = []
    page.route(ORIGIN + "/api/game-profiles/selected", lambda route: held.append(route))
    page.evaluate("async()=>{(await import('/js/mapping.js')).syncGameProfile({revision:'background'})}")
    page.wait_for_timeout(50)
    assert len(held) == 1
    api.profile.update(name="用户刚选的游戏", configuration_revision="own-selection")
    page.evaluate("async()=>{await (await import('/js/mapping.js')).applySelectedProfile()}")
    assert page.locator("#currentGameName").text_content() == "用户刚选的游戏"
    api.fulfill(held[0], {"profile": old})
    page.wait_for_timeout(150)
    assert page.locator("#currentGameName").text_content() == "用户刚选的游戏"


def pending_settings(**changes):
    return {"state": "pending", "instance": "settings-a", "generation": 1, "version": 2,
            "revision": 1, "saved_revision": 0, "attempts": 1, "retry_limit": 4,
            "automatic_retry": True, "error": "配置文件被占用，尚未保存", **changes}


def edit_head_speed(page, value=82):
    page.evaluate("value=>{speedX.value=String(value);speedX.dispatchEvent(new Event('input',{bubbles:true}))}", value)


def test_pending_settings_show_truthful_status_and_background_save_clears_draft(desktop):
    page, api = desktop.page, desktop.api
    calls = []
    state = pending_settings()

    def save(route):
        calls.append(route)
        api.runtime["kernel"]["head"]["sensitivity_x"] = route.request.post_data_json["sensitivity_x"]
        api.runtime["settings_persistence"] = state
        api.fulfill(route, {"error": state["error"], "applied": True, "settings_persistence": state}, 503)

    page.route(ORIGIN + "/api/head/config", save)
    edit_head_speed(page)
    page.wait_for_function("headSaveStatus.textContent.includes('等待自动保存')")
    assert page.locator("#settingsSaveWarning").is_visible()
    assert page.evaluate("async()=>{const m=await import('/js/shell.js');return m.headSaver.pending()}")
    api.runtime["settings_persistence"] = pending_settings(state="ready", version=3, saved_revision=1, error="", automatic_retry=False)
    page.wait_for_function("settingsSaveWarning.hidden && headSaveStatus.textContent==='已保存'")
    assert not page.evaluate("async()=>{const m=await import('/js/shell.js');return m.headSaver.pending()}")
    assert len(calls) == 1


def test_late_save_error_cannot_restore_an_error_already_recovered(desktop):
    page, api, held = desktop.page, desktop.api, []
    page.route(ORIGIN + "/api/head/config", lambda route: held.append(route))
    edit_head_speed(page)
    page.wait_for_timeout(700)
    assert len(held) == 1
    ready = pending_settings(state="ready", version=3, saved_revision=1, error="", automatic_retry=False)
    page.evaluate("async state=>(await import('/js/core.js')).updateSettingsPersistence(state)", ready)
    api.fulfill(held[0], {"applied": True, "error": "旧的保存失败", "settings_persistence": pending_settings()}, 503)
    page.wait_for_function("headSaveStatus.textContent==='已保存'")
    assert page.locator("#settingsSaveWarning").is_hidden()
    assert not page.evaluate("async()=>{const m=await import('/js/shell.js');return m.headSaver.pending()}")


def test_manual_retry_saves_pending_settings_without_repeating_the_setting_change(desktop):
    page, api, saves, retries = desktop.page, desktop.api, [], []
    error = pending_settings(state="error", attempts=4, automatic_retry=False)

    def save(route):
        saves.append(route)
        api.fulfill(route, {"applied": True, "error": error["error"], "settings_persistence": error}, 503)

    def retry(route):
        retries.append(route.request.post_data_json)
        api.fulfill(route, {"settings_persistence": pending_settings(state="ready", version=3, saved_revision=1, error="")})

    page.route(ORIGIN + "/api/head/config", save)
    page.route(ORIGIN + "/api/settings/retry", retry)
    edit_head_speed(page)
    page.wait_for_function("!settingsSaveWarning.hidden && headSaveStatus.textContent==='已应用，尚未保存'")
    page.click("#retrySettingsSaveBtn")
    page.wait_for_function("settingsSaveWarning.hidden && headSaveStatus.textContent==='已保存'")
    assert retries == [{"repair": False}] and len(saves) == 1


def test_new_process_cannot_claim_an_old_unsaved_draft_has_been_saved(desktop):
    page, api = desktop.page, desktop.api
    state = pending_settings()
    page.route(ORIGIN + "/api/head/config", lambda route: api.fulfill(route, {"applied": True, "error": state["error"], "settings_persistence": state}, 503))
    edit_head_speed(page)
    page.wait_for_function("headSaveStatus.textContent.includes('等待自动保存')")
    ready = pending_settings(state="ready", instance="new-process", generation=2, version=1, saved_revision=100, error="")
    page.evaluate("async state=>(await import('/js/core.js')).updateSettingsPersistence(state)", ready)
    page.evaluate("async state=>(await import('/js/core.js')).updateSettingsPersistence(state)", pending_settings(version=99))
    assert page.locator("#settingsSaveWarning").is_hidden()
    assert page.evaluate("async()=>{const m=await import('/js/shell.js');return m.headSaver.pending()}")
    assert "服务已重启" in page.locator("#headSaveStatus").text_content()
    page.route(ORIGIN + "/api/head/config", lambda route: api.fulfill(route, {**api.runtime, "settings_persistence": {**ready, "version": 2}}))
    page.evaluate("retryHeadBtn.click()")
    page.wait_for_function("headSaveStatus.textContent==='已保存'")
    assert not page.evaluate("async()=>{const m=await import('/js/shell.js');return m.headSaver.pending()}")


def test_setting_receipt_can_finish_while_an_unrelated_library_still_needs_repair(desktop):
    page, api = desktop.page, desktop.api
    state = pending_settings(state="blocked", error="动作库需要恢复", components=[
        {"instance": "head-file", "state": "pending", "saved_revision": 0},
        {"instance": "pose-file", "state": "blocked", "saved_revision": 0}])
    receipt = {**pending_settings(), "targets": [{"instance": "head-file", "revision": 1}]}
    page.route(ORIGIN + "/api/head/config", lambda route: api.fulfill(route, {
        "applied": True, "error": "头部设置尚未保存", "settings_persistence": state, "settings_write": receipt}, 503))
    edit_head_speed(page)
    page.wait_for_function("headSaveStatus.textContent.includes('等待自动保存')")
    state["version"] = 3
    state["components"][0].update(state="ready", saved_revision=1)
    page.evaluate("async state=>(await import('/js/core.js')).updateSettingsPersistence(state)", state)
    page.wait_for_function("headSaveStatus.textContent==='已保存'")
    assert page.locator("#settingsSaveWarning").is_visible()
    assert "动作库需要恢复" in page.locator("#settingsSaveWarning").text_content()
    assert not page.evaluate("async()=>{const m=await import('/js/shell.js');return m.headSaver.pending()}")


def test_exit_waits_for_mapping_save_and_then_stops_page_controls(desktop):
    page, api = desktop.page, desktop.api
    page.click('[data-view="games"]')
    api.hold_overrides = True
    page.locator('.binding-row[data-trigger="zone.leftHand"] .binding-target-box select').select_option("Y")
    page.click("#helpBtn")
    page.click("#exitAppBtn")
    page.wait_for_function("mappingFields.disabled")
    page.wait_for_timeout(50)
    assert api.held_overrides is not None
    assert not any(path == "/api/shutdown" for path, _ in api.requests)
    api.fulfill(api.held_overrides, {"profile": api.profile})
    page.wait_for_function("exitTitle.textContent === 'MotionControl 已退出'")
    assert api.profile["bindings"]["zones"]["leftHand"]["action"]["target"] == "Y"
    assert sum(path == "/api/shutdown" for path, _ in api.requests) == 1
    assert page.evaluate("document.querySelector('main').inert && mainActionBtn.disabled")
    assert page.locator("#serviceStatus").inner_text() == "本地服务已关闭"
    before = len(api.requests)
    message = page.evaluate("async () => {const c=await import('/js/core.js');try{await c.api('/api/kernel/status')}catch(e){return e.message}}")
    assert "退出流程" in message and len(api.requests) == before


def test_unsaved_failure_keeps_original_page_open_and_does_not_exit(desktop):
    page, api = desktop.page, desktop.api
    api.fail_overrides = True
    page.click('[data-view="games"]')
    page.locator('.binding-row[data-trigger="zone.leftHand"] .binding-target-box select').select_option("Y")
    page.click("#helpBtn")
    page.click("#exitAppBtn")
    page.wait_for_function("!notice.hidden && notice.textContent.includes('draft could not be saved')")
    assert not any(path == "/api/shutdown" for path, _ in api.requests)
    assert not page.evaluate("exitDialog.open || document.querySelector('main').inert")


def test_repeated_exit_is_one_request_and_new_instance_is_not_reported_as_closed(desktop):
    page, api = desktop.page, desktop.api
    page.route(ORIGIN + "/api/health", lambda route: api.fulfill(route, {"app": "MotionControl", "pid": 456}))
    page.evaluate("async () => {const m=await import('/js/lifecycle.js');await Promise.all([m.exitApplication(()=>{}),m.exitApplication(()=>{})])}")
    page.wait_for_function("exitTitle.textContent === '原程序已退出'")
    assert sum(path == "/api/shutdown" for path, _ in api.requests) == 1
    assert page.locator("#exitCheckBtn").inner_text() == "连接新程序"
    assert "检测到新程序" in page.locator("#serviceStatus").inner_text()


def test_recording_must_finish_before_exit(desktop):
    page, api = desktop.page, desktop.api
    api.recording.update(state="recording", remaining_s=20)
    page.evaluate("async () => {const d=await import('/js/diagnostics.js');await d.refreshPoseRecord()}")
    page.click("#helpBtn")
    page.click("#exitAppBtn")
    page.wait_for_function("!notice.hidden && notice.textContent.includes('先结束录制')")
    assert not any(path == "/api/shutdown" for path, _ in api.requests)


@pytest.mark.parametrize("response", ["busy", "unreadable"])
def test_shutdown_does_not_mistake_busy_or_invalid_health_for_finished(desktop, response):
    page, api = desktop.page, desktop.api
    closed = False

    def health(route):
        if closed:
            route.abort("connectionrefused")
        else:
            route.fulfill(status=503 if response == "busy" else 200,
                          content_type="application/json", body='{}' if response == "busy" else '{partial')

    page.route(ORIGIN + "/api/health", health)
    page.click("#helpBtn")
    page.click("#exitAppBtn")
    page.wait_for_function("exitDialog.open")
    page.wait_for_timeout(650)
    assert page.locator("#exitTitle").inner_text() == "正在退出 MotionControl"
    closed = True
    page.wait_for_function("exitTitle.textContent === 'MotionControl 已退出'")
    assert sum(path == "/api/shutdown" for path, _ in api.requests) == 1


def test_touch_narrow_windows_and_all_settings_panes_stay_within_width(mock_browser):
    api = LocalApi()
    api.macros = [{"id": "macro_a", "name": "Long keyboard macro", "steps": [{"type": "keyboard", "target": "W", "duration_ms": 100}]}]
    context = mock_browser.new_context(viewport={"width": 390, "height": 700}, has_touch=True, is_mobile=True, device_scale_factor=3)
    context.add_init_script("localStorage.setItem('motioncontrol_tutorial_v2', JSON.stringify({seen:1,offered:true}));localStorage.setItem('motioncontrol_tips_seen', JSON.stringify(['help','gameMenu']));")
    page = context.new_page()
    page.route(ORIGIN + "/**", api.handle)
    page.goto(ORIGIN)
    page.wait_for_function("currentGameName.textContent === 'Default game'")
    for width in (320, 390, 768):
        page.set_viewport_size({"width": width, "height": 700})
        for view in ("play", "games", "devices"):
            page.click(f'nav [data-view="{view}"]')
            panes = ("devices", "view", "voice", "lab") if view == "devices" else (None,)
            for pane in panes:
                if pane:
                    page.click(f'#settingsNav [data-pane="{pane}"]')
                if pane == "lab":
                    page.evaluate("stereoViews.hidden=false;for(const c of stereoViews.querySelectorAll('canvas')){c.width=640;c.height=360}")
                measured = page.evaluate("({width:document.documentElement.scrollWidth,inner:innerWidth})")
                assert measured["width"] <= width + 1 and measured["inner"] <= width + 1, (width, view, pane, measured)
    assert page.evaluate("getComputedStyle(mainActionBtn).minHeight") == "44px"
    context.close()


def test_status_starts_while_libraries_are_pending_and_mapping_waits_for_them(desktop):
    page, api = desktop.page, desktop.api
    api.hold_libraries = True
    api.macros = [{"id": "macro_a", "name": "Loaded macro", "steps": []}]
    api.custom_poses = [{"id": "custom_a", "name": "Loaded pose", "frames": 1, "enabled": True, "threshold": .8}]
    api.profile["bindings"]["poses"]["custom_a"] = {"action": {"type": "keyboard", "target": "W"}}
    page.reload()
    page.wait_for_function("!mainActionBtn.disabled")
    assert page.locator("#currentGameName").inner_text() == "正在读取…"
    assert page.locator(".binding-row").count() == 0
    api.hold_libraries = False
    api.fulfill(api.held_libraries["/api/pose/custom"], {"poses": api.custom_poses})
    api.fulfill(api.held_libraries["/api/macros"], {"macros": api.macros})
    page.wait_for_function("currentGameName.textContent === 'Default game'")
    assert page.locator('.binding-row[data-trigger="pose.custom_a"]').count() == 1
    assert page.evaluate("import('/js/labels.js').then(m=>m.macroLibrary.items[0].name)") == "Loaded macro"


def test_old_configuration_snapshot_cannot_replace_newer_view_settings(desktop):
    page, api = desktop.page, desktop.api
    latest = copy.deepcopy(api.runtime)
    latest["config_revision"] = 2
    latest["kernel"]["head"]["sensitivity_x"] = 88
    older = copy.deepcopy(latest)
    older["config_revision"] = 1
    older["kernel"]["head"]["sensitivity_x"] = 20
    value = page.evaluate("""async ({latest,older})=>{const p=await import('/js/play.js');const s=await import('/js/state.js');p.renderKernelState(latest,true);p.renderKernelState(older,true);return s.head.sensitivityX;}""", {"latest": latest, "older": older})
    assert value == 88


def test_recording_remains_busy_until_background_save_is_done(desktop):
    page, api = desktop.page, desktop.api
    api.recording.update(state="saving", frames=20)
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')
    page.wait_for_function("poseRecordBtn.disabled")
    assert "正在保存" in page.locator("#poseRecordStatus").inner_text()
    api.requests.clear()
    page.evaluate("import('/js/diagnostics.js').then(m=>m.refreshPoseRecord())")
    assert not any(path == "/api/recordings" for path, _ in api.requests)
    api.recording["state"] = "done"
    page.evaluate("import('/js/diagnostics.js').then(m=>m.refreshPoseRecord())")
    page.wait_for_function("!poseRecordBtn.disabled && !recordingsRow.hidden")


def test_release_menu_keeps_keyboard_focus_after_selecting_an_option(desktop):
    page, api = desktop.page, desktop.api
    hold = {"type": "keyboard", "target": "W", "behavior": "hold"}
    release = {"type": "voice_release", "target": ["game.profile_slot_1"]}
    api.commands = [{"id": "game.profile_slot_1", "phrase": "hold", "default_action": hold, "effective_action": hold},
                    {"id": "game.profile_slot_2", "phrase": "release", "default_action": release, "effective_action": release}]
    api.profile["bindings"]["voice"].update({"game.profile_slot_1": {"phrase": "hold", "action": hold},
                                             "game.profile_slot_2": {"phrase": "release", "action": release}})
    page.reload()
    page.wait_for_function("currentGameName.textContent === 'Default game'")
    page.click('[data-view="games"]')
    page.click('#mapTabs [data-tab="voice"]')
    button = page.locator('.binding-row[data-trigger="voice.game.profile_slot_2"] .voice-release-picker-button')
    button.click()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.classList.contains('voice-release-option')")
    page.keyboard.press("Escape")
    assert button.evaluate("el=>document.activeElement === el")
