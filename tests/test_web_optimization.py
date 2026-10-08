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
        body = route.request.post_data_json if route.request.method == "POST" else None
        self.requests.append((path, body))
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
                if group == "zone" and value:
                    self.profile["bindings"]["zones"][name] = value
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
            self.runtime["camera"]["running"] = False
            self.input["body_enabled"] = False
            self.voice.update(connected=False, audio_ready=False)
            self.input["voice"] = self.voice
            data = {**self.runtime, "output": self.output}
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
    api.hold_output = True
    page.select_option("#outputMode", "gamepad")
    page.wait_for_timeout(50)
    assert api.held_output is not None
    page.click("#stopBtn")
    page.wait_for_function("!mainActionBtn.textContent.includes('暂停')")
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
    page.wait_for_function("!retryProfileSaveBtn.hidden && retryProfileSaveBtn.textContent.includes('重新选择')")
    page.click("#retryProfileSaveBtn")
    page.wait_for_function("retryProfileSaveBtn.hidden && profileSaveStatus.textContent === '已保存'")
    paths = [path for path, _ in api.requests if path in ("/api/game-profiles/select", "/api/game-profiles/overrides")]
    assert paths[-2:] == ["/api/game-profiles/select", "/api/game-profiles/overrides"]
    assert api.profile["bindings"]["zones"]["leftHand"]["action"]["target"] == "Y"
    assert row.locator('.binding-target-box select').input_value() == "Y"
    assert page.locator("#mappingFields").is_enabled()


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
            panes = ("devices", "view", "body", "voice", "macro", "lab") if view == "devices" else (None,)
            for pane in panes:
                if pane:
                    page.click(f'#settingsNav [data-pane="{pane}"]')
                if pane == "lab":
                    page.evaluate("stereoViews.hidden=false;for(const c of stereoViews.querySelectorAll('canvas')){c.width=640;c.height=360}")
                measured = page.evaluate("({width:document.documentElement.scrollWidth,inner:innerWidth})")
                assert measured["width"] <= width + 1 and measured["inner"] <= width + 1, (width, view, pane, measured)
    assert page.evaluate("getComputedStyle(stopBtn).minHeight") == "44px"
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
