"""真实本机网页接口与新页面衔接；仅使用合成图、屏幕流和隔离用户数据。"""
from __future__ import annotations

import base64
import importlib.util
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from test_studio_ui import FAKE_CAPTURE, PNG, browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


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
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="fitness"]')
    page.wait_for_function("document.querySelector('#fitnessProfile [name=weight_kg]').value !== ''")
    page.click("#fitnessStart")
    page.wait_for_function("document.getElementById('fitnessStatus').textContent==='记录中'")
    session_id = app.FITNESS.state()["session_id"]
    # 可重复的踏步事件，只注入运动计数，不向电脑输出游戏按键。
    app.FITNESS.step_event("left", 10.)
    app.FITNESS.step_event("right", 10.4)
    page.wait_for_function("document.getElementById('fitnessSteps').textContent==='2'")
    page.click("#fitnessPause")
    page.wait_for_function("document.getElementById('fitnessStatus').textContent==='已暂停'")
    page.click("#fitnessStart")
    page.wait_for_function("document.getElementById('fitnessStatus').textContent==='记录中'")
    page.click("#fitnessFinish")
    page.wait_for_function("document.getElementById('fitnessStatus').textContent==='已结束'")
    page.wait_for_selector(".fitness-history-row", timeout=8000)
    assert "2 步" in page.locator("#fitnessHistory").inner_text()
    assert app.FITNESS.history()[-1]["session_id"] == session_id
    assert app.FITNESS.history()[-1]["steps"] == 2
    page.locator('#fitnessProfile [name="weight_kg"]').fill("63")
    page.click('#fitnessProfile button[type="submit"]')
    page.wait_for_function("document.querySelector('#fitnessProfile [name=weight_kg]').value==='63'")
    assert app.FITNESS.state()["profile"]["weight_kg"] == 63
    assert (user_dir / "fitness.json").is_file()
    page.click('#settingsNav [data-pane="studio"]')
    page.wait_for_selector("#studioStartBtn")
    assert page.locator("#studioRoot").is_visible()
    assert all(any(endpoint in url for _, url in fitness_calls) for endpoint in ("/api/fitness/state", "/api/fitness/control", "/api/fitness/history"))
    assert errors == []
    assert missing == []
    page.close()
