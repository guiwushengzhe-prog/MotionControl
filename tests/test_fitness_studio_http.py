"""真实本机网页接口与新页面衔接；仅使用合成图、屏幕流和隔离用户数据。"""
from __future__ import annotations

import base64
import importlib.util
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from test_studio_ui import FAKE_CAPTURE, PNG, browser  # noqa: F401
from test_output_actions_v097 import Mouse, Keyboard, Pad

ROOT = Path(__file__).resolve().parents[1]


def test_combined_commands_and_optional_phone_model(browser, app_http, tmp_path, monkeypatch):
    from motioncontrol.recognition_models import RecognitionModels
    app, base, _ = app_http
    (tmp_path / "pose_landmarker_heavy.task").write_bytes(b"synthetic model")
    app.RECOGNITION_MODELS = RecognitionModels(tmp_path)
    connected, sent = [False], []
    original_status = app.INPUT_BRIDGE.status
    monkeypatch.setattr(app.INPUT_BRIDGE, "status", lambda **kw: {
        **original_status(**kw), "mobile_pose_connected": connected[0]})
    monkeypatch.setattr(app.INPUT_BRIDGE, "broadcast_control_config", lambda config: sent.append(config))
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("localStorage.setItem('motioncontrol_tutorial_v2',JSON.stringify({seen:1,offered:true}));")
    page.goto(base + "/")
    page.click('nav [data-view="devices"]')
    page.locator("#phoneModelDetails summary").click()
    page.wait_for_function("phoneModelStatus.textContent.includes('连接手机后可添加')")
    assert page.locator("#sendHeavyModelBtn").is_disabled()
    connected[0] = True
    page.wait_for_function("!sendHeavyModelBtn.disabled")
    page.click("#sendHeavyModelBtn")
    page.wait_for_function("document.body.textContent.includes('默认模型不变')")
    assert sent[-1]["pose_model_offer"]["id"] == "mp-heavy"
    page.click('#settingsNav [data-pane="voice"]')
    assert page.locator("#commandsPanel .pane-title").inner_text() == "语音和宏"
    assert page.locator("#personalVoicePanel").is_visible() and page.locator("#macroPanel").is_visible()
    page.locator("#macroName").fill("连续按键")
    page.click("#macroAddBtn")
    page.wait_for_function("[...macroList.querySelectorAll('.macro-name')].some(input=>input.value==='连续按键')")
    assert errors == []
    page.close()


class SyntheticPortrait:
    """替换采集来源，保留真实StudioService和网页处理器。"""
    def __init__(self):
        self.config, self.enabled, self.revision = {}, False, 1

    def update(self, config):
        self.config = dict(config)
        self.revision += 1

    def set_enabled(self, enabled):
        self.enabled = enabled

    def status(self):
        return {"ready": self.enabled, "enabled": self.enabled, "revision": self.revision,
                "reason": "" if self.enabled else "预览未开启"}

    def frame_snapshot_png(self):
        return (PNG if self.enabled else None), self.revision

    def close(self):
        self.enabled = False


@pytest.fixture
def app_http(isolated_user_data, monkeypatch):
    # 导入真实路由，故意不调用main：不启动麦克风、摄像头或设备发现服务。
    spec = importlib.util.spec_from_file_location("fitness_studio_http_test_server", ROOT / "server.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    app.OUTPUT.mouse, app.OUTPUT.keyboard, app.OUTPUT._pad = Mouse(), Keyboard(), Pad()
    # 选择未连接的手机模式；网页启停控制绝不能顺带打开真实摄像头。
    app.RUNTIME.body_mode = "phone"
    app.INPUT_BRIDGE.set_body_mode("phone")
    app.INPUT_BRIDGE.set_body_enabled(True)
    monkeypatch.setattr(app.RUNTIME.camera, "start", lambda: pytest.fail("测试不允许启动摄像头"))
    original = app.STUDIO.portrait
    original.close()
    portrait = SyntheticPortrait()
    portrait.update(app.STUDIO.config)
    app.STUDIO.portrait = portrait
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.AdminHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, f"http://127.0.0.1:{server.server_port}", isolated_user_data
    server.shutdown()
    thread.join()
    server.server_close()
    app.STUDIO.close()
    app.FITNESS.close()
    app.SYSTEM_COMMANDS.close()
    app.INPUT_BRIDGE.close()
    app.VOICE.close()
    app.RUNTIME.close()
    app.OUTPUT.close()


def test_real_studio_http_writes_playable_synthetic_video(browser, app_http):
    app, base, user_dir = app_http
    page = browser.new_page()
    errors, missing = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: missing.append(response.url) if response.status == 404 else None)
    page.add_init_script(FAKE_CAPTURE)
    page.goto(base + "/studio.html")
    page.wait_for_function("!document.getElementById('studioFace').disabled")
    page.click("#studioStartBtn")
    page.wait_for_selector("#studioRecordingBadge:not([hidden])")
    page.wait_for_timeout(1300)
    files = list((user_dir / "recordings" / "studio").glob("*.webm"))
    assert len(files) == 1 and files[0].stat().st_size > 0, "真实服务在录制中已把片段写入隔离目录"
    page.click("#studioStopBtn")
    page.wait_for_function("document.getElementById('studioStatus').textContent.includes('录制完成')")
    assert str(files[0]) in page.locator("#studioSavedFile").inner_text()
    assert app.STUDIO.state()["recordings"][0]["finished"] is True
    encoded = base64.b64encode(files[0].read_bytes()).decode()
    size = page.evaluate("""async data=>{
      const blob=new Blob([Uint8Array.from(atob(data),c=>c.charCodeAt(0))],{type:'video/webm'});
      const video=document.createElement('video'),url=URL.createObjectURL(blob);video.src=url;video.muted=true;
      await new Promise((resolve,reject)=>{video.onloadeddata=resolve;video.onerror=reject});
      await video.play();const result=[video.videoWidth,video.videoHeight];video.pause();URL.revokeObjectURL(url);return result;
    }""", encoded)
    assert size == [1280, 720]
    assert page.evaluate("syntheticTracks.every(track=>track.readyState==='ended')")
    assert errors == []
    assert missing == []
    page.close()


def test_real_main_page_fitness_controls_and_imports(browser, app_http):
    app, base, user_dir = app_http
    page = browser.new_page()
    errors, missing, fitness_calls = [], [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: missing.append(response.url) if response.status == 404 else None)
    page.on("request", lambda request: fitness_calls.append((request.method, request.url)) if "/api/fitness/" in request.url else None)
    page.add_init_script("localStorage.setItem('motioncontrol_tutorial_v2',JSON.stringify({done:[],skipped:[],seen:1,offered:true}));localStorage.setItem('motioncontrol_tips_seen',JSON.stringify(['help','gameMenu']));")
    page.goto(base + "/")
    page.wait_for_function("!document.getElementById('mainActionBtn').disabled")
    page.click("#mainActionBtn")
    page.wait_for_function("document.getElementById('homeFitnessTitle').textContent==='这次运动'")
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="fitness"]')
    page.wait_for_function("document.querySelector('#fitnessProfile [name=weight_kg]').value !== ''")
    page.wait_for_function("document.getElementById('fitnessStatus').textContent==='记录中'")
    assert page.locator("#fitnessHeartStatus").inner_text() == "未收到心率"
    app.FITNESS.heart_rate({"bpm": 112})
    page.wait_for_function("document.getElementById('fitnessHeartNow').textContent==='112'")
    assert page.locator("#fitnessHeartAvg").inner_text() == "112"
    assert page.locator("#fitnessHeartMax").inner_text() == "112"
    assert page.locator("#fitnessStart").count() == 0
    assert page.locator("#fitnessPause").count() == 0
    session_id = app.FITNESS.state()["session_id"]
    # 可重复的踏步事件，只注入运动计数，不向电脑输出游戏按键。
    app.FITNESS.step_event("left", 10.)
    app.FITNESS.step_event("right", 10.4)
    page.wait_for_function("document.getElementById('fitnessSteps').textContent==='2'")
    page.click("#mainActionBtn")
    page.wait_for_function("document.getElementById('fitnessStatus').textContent==='已暂停'")
    app.FITNESS.heart_rate({"bpm": 120})  # 暂停时可以预览，不累计平均值。
    page.wait_for_function("fitnessHeartNow.textContent==='120'")
    assert page.locator("#fitnessHeartAvg").inner_text() == "112"
    page.click("#mainActionBtn")
    page.wait_for_function("document.getElementById('fitnessStatus').textContent==='记录中'")
    assert app.FITNESS.state()["session_id"] == session_id
    page.click("#mainActionBtn")
    page.wait_for_function("document.getElementById('fitnessStatus').textContent==='已暂停'")
    page.click("#fitnessFinish")
    page.wait_for_function("document.getElementById('fitnessStatus').textContent==='已结束'")
    page.wait_for_function("document.getElementById('fitnessHistory').textContent.includes('2 步')", timeout=8000)
    assert "2 步" in page.locator("#fitnessHistory").inner_text()
    assert app.FITNESS.history()[-1]["session_id"] == session_id
    assert app.FITNESS.history()[-1]["steps"] == 2
    page.locator('#fitnessProfile [name="weight_kg"]').fill("63")
    page.click('#fitnessProfile button[type="submit"]')
    page.wait_for_function("document.querySelector('#fitnessProfile [name=weight_kg]').value==='63'")
    assert app.FITNESS.state()["profile"]["weight_kg"] == 63
    assert (user_dir / "fitness.json").is_file()
    page.click("#fitnessPhoneConnect")
    page.wait_for_function("!document.getElementById('phoneConnectPanel').hidden")
    assert page.locator('#phoneConnectPanel').is_visible()
    assert page.locator('#phoneConnectPanel').evaluate("el=>el===document.activeElement")
    assert app.RUNTIME.body_mode == "phone"
    assert not app.RUNTIME.camera.running
    page.click('#settingsNav [data-pane="studio"]')
    page.wait_for_selector("#studioStartBtn")
    assert page.locator("#studioRoot").is_visible()
    assert all(any(endpoint in url for _, url in fitness_calls) for endpoint in ("/api/fitness/state", "/api/fitness/control", "/api/fitness/history"))
    assert errors == []
    assert missing == []
    page.close()


def test_device_mirror_updates_all_view_layers_and_persists(browser, app_http):
    app, base, _ = app_http
    page = browser.new_page()
    page.add_init_script("localStorage.setItem('motioncontrol_tutorial_v2',JSON.stringify({done:[],skipped:[],seen:1,offered:true}));")
    page.goto(base + "/")
    page.click('nav [data-view="devices"]')
    page.wait_for_function("!document.getElementById('cameraMirror').disabled")
    page.uncheck("#cameraMirror")
    page.wait_for_function("document.getElementById('viewer').classList.contains('unmirrored')")
    assert app.STUDIO.config["mirror"] is False
    runtime = page.evaluate("async()=>await (await fetch('/api/kernel/status')).json()")
    assert runtime["preview_mirror"] is False
    assert page.evaluate("getComputedStyle(document.getElementById('canvas')).transform") == "none"
    assert page.evaluate("getComputedStyle(document.querySelector('.zones')).transform") == "none"
    # 屏幕上向右挪区域，非镜像时原坐标增大；镜像时原坐标减小。
    # 只改隔离浏览器里的编辑草稿，不向服务保存，也不触发游戏输出。
    original_rect = {"x1": .15, "y1": .25, "x2": .35, "y2": .45}
    unmirrored = page.evaluate("""async rect=>{
      const play=await import('/js/play.js');play.rectEdit.rects.leftHand={...rect};
      play.nudgeRect('leftHand','ArrowRight',false);return play.rectEdit.rects.leftHand;
    }""", original_rect)
    assert unmirrored["x1"] == pytest.approx(.16)
    assert unmirrored["x2"] == pytest.approx(.36)
    page.reload()
    page.click('nav [data-view="devices"]')
    page.wait_for_function("!document.getElementById('cameraMirror').checked")
    page.check("#cameraMirror")
    page.wait_for_function("!document.getElementById('viewer').classList.contains('unmirrored')")
    assert app.STUDIO.config["mirror"] is True
    mirrored = page.evaluate("""async rect=>{
      const play=await import('/js/play.js');play.rectEdit.rects.leftHand={...rect};
      play.nudgeRect('leftHand','ArrowRight',false);return play.rectEdit.rects.leftHand;
    }""", original_rect)
    assert mirrored["x1"] == pytest.approx(.14)
    assert mirrored["x2"] == pytest.approx(.34)
    page.click('nav [data-view="play"]')
    page.click("#homeFitnessGo")
    assert page.locator("#pane-fitness").is_visible()
    page.close()
