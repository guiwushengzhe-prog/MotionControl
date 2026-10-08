"""录制页在真实浏览器里使用合成屏幕流；绝不打开真人摄像头或私人屏幕。"""
from __future__ import annotations

import base64
import json
import struct
import threading
import time
import zlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
WEB = Path(__file__).resolve().parents[1] / "web"
def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


PNG = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0))
       + _png_chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff\xff" * 4) * 4)) + _png_chunk(b"IEND", b""))


@pytest.fixture
def studio_server():
    state = {"config": {"face": "original", "background": "transparent"}, "posts": [], "chunks": [], "frame_delay": 0, "fail_config": False, "revision": 1}

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB), **kwargs)

        def log_message(self, *args):
            pass

        def respond(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/studio/state":
                self.respond({"config": state["config"], "status": {"revision": state["revision"]}})
            elif self.path.startswith("/api/studio/frame.png"):
                original = state["config"]["face"] == "original"
                revision = state["revision"]
                time.sleep(state["frame_delay"])
                # 已经发起的旧帧可能比配置操作更晚到，前端也必须拦住。
                self.send_response(200 if original else 204)
                self.send_header("Content-Type", "image/png")
                self.send_header("X-Studio-Revision", str(revision))
                self.send_header("Content-Length", str(len(PNG) if original else 0))
                self.end_headers()
                if original:
                    self.wfile.write(PNG)
            else:
                super().do_GET()

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path.startswith("/api/studio/recording/chunk"):
                state["chunks"].append(raw)
                self.respond({"ok": True})
                return
            body = json.loads(raw or b"{}")
            state["posts"].append((self.path, body))
            if self.path == "/api/studio/config":
                if state["fail_config"]:
                    self.respond({"error": "设置保存失败"}, 500)
                else:
                    state["config"] = body
                    state["revision"] += 1
                    self.respond({"config": body})
            elif self.path == "/api/studio/recording/start":
                self.respond({"id": "synthetic-video", "file_name": "synthetic.webm"})
            elif self.path == "/api/studio/recording/finish":
                self.respond({"path": "测试输出/synthetic.webm", "bytes": sum(map(len, state["chunks"]))})
            else:
                self.respond({"ok": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as automation:
        try:
            instance = automation.chromium.launch()
        except Exception:
            try:
                instance = automation.chromium.launch(channel="msedge")
            except Exception as exc:
                pytest.skip(f"没有可用的测试浏览器：{exc}")
        yield instance
        instance.close()


FAKE_CAPTURE = """
window.syntheticTracks=[];window.captureRequests=[];
Object.defineProperty(navigator.mediaDevices,'getDisplayMedia',{value:async options=>{
  window.captureRequests.push(options);
  const c=document.createElement('canvas');c.width=320;c.height=180;
  const ctx=c.getContext('2d');ctx.fillStyle='#0a84ff';ctx.fillRect(0,0,320,180);
  const stream=c.captureStream(10);window.syntheticTracks.push(...stream.getTracks());return stream;
}});
Object.defineProperty(navigator.mediaDevices,'getUserMedia',{value:async options=>{
  if(options.video!==false)throw new Error('禁止测试打开摄像头');
  const audio=new AudioContext(),oscillator=audio.createOscillator(),destination=audio.createMediaStreamDestination();
  oscillator.connect(destination);oscillator.start();window.syntheticAudio=audio;
  window.syntheticTracks.push(...destination.stream.getTracks());return destination.stream;
}});
"""


def ready(page, base):
    page.goto(base + "/studio.html")
    page.wait_for_function("!document.getElementById('studioFace').disabled")


def test_cancel_never_creates_recording(browser, studio_server):
    base, state = studio_server
    page = browser.new_page()
    page.add_init_script("Object.defineProperty(navigator.mediaDevices,'getDisplayMedia',{value:async()=>{throw new DOMException('cancel','NotAllowedError')}})")
    ready(page, base)
    page.click("#studioStartBtn")
    page.wait_for_function("document.getElementById('studioStatus').textContent.includes('没有开始录制')")
    assert not any(path == "/api/studio/recording/start" for path, _ in state["posts"])
    page.close()


def test_synthetic_recording_streams_chunks_and_releases_sources(browser, studio_server, tmp_path):
    base, state = studio_server
    page = browser.new_page()
    page.add_init_script(FAKE_CAPTURE)
    ready(page, base)
    page.check("#studioMicrophone")
    page.click("#studioStartBtn")
    page.wait_for_selector("#studioRecordingBadge:not([hidden])")
    page.wait_for_timeout(2400)
    assert len(state["chunks"]) >= 1, "录制进行期间已经写入片段，而非停止后才上传"
    assert page.evaluate("captureRequests[0].audio") is True
    page.click("#studioStopBtn")
    page.wait_for_function("document.getElementById('studioStatus').textContent.includes('录制完成')")
    assert page.evaluate("syntheticTracks.every(track=>track.readyState==='ended')")
    assert any(path == "/api/studio/recording/finish" for path, _ in state["posts"])
    video = b"".join(state["chunks"])
    assert video[:4] == b"\x1aE\xdf\xa3", "已得到编码视频文件头"
    assert all(len(chunk) <= 16 * 1024 * 1024 for chunk in state["chunks"])
    # 实际编码、实际回放，画面始终为合成色块。
    encoded = base64.b64encode(video).decode()
    playable = page.evaluate("""async data=>{
      const bytes=Uint8Array.from(atob(data),c=>c.charCodeAt(0));
      const url=URL.createObjectURL(new Blob([bytes],{type:'video/webm'}));
      const video=document.createElement('video');video.src=url;video.muted=true;
      await new Promise((resolve,reject)=>{video.onloadeddata=resolve;video.onerror=reject});
      await video.play();const size=[video.videoWidth,video.videoHeight];video.pause();URL.revokeObjectURL(url);return size;
    }""", encoded)
    assert playable == [1280, 720]
    page.close()


def test_old_original_frame_is_hidden_after_face_change(browser, studio_server):
    base, state = studio_server
    page = browser.new_page()
    ready(page, base)
    page.click("#studioPreviewBtn")
    page.wait_for_function("document.getElementById('studioPlaceholder').hidden")
    state["frame_delay"] = .5
    page.wait_for_timeout(150)
    page.select_option("#studioFace", "mask")
    page.wait_for_timeout(800)
    assert page.evaluate("document.getElementById('studioPlaceholder').hidden") is False
    assert page.evaluate("Array.from(document.getElementById('studioCanvas').getContext('2d').getImageData(640,360,1,1).data)") == [16, 16, 20, 255]
    page.close()


def test_overlay_reads_without_enabling_camera(browser, studio_server):
    base, state = studio_server
    state["config"]["face"] = "mask"
    page = browser.new_page()
    page.goto(base + "/studio-overlay.html")
    page.wait_for_timeout(500)
    assert state["posts"] == [], "直播浏览器来源不能写配置或启用视频"
    assert page.evaluate("document.getElementById('portrait').getContext('2d').getImageData(0,0,1,1).data[3]") == 0
    page.close()


def test_overlay_rejects_old_revision_after_external_face_change(browser, studio_server):
    base, state = studio_server
    page = browser.new_page()
    page.goto(base + "/studio-overlay.html")
    page.wait_for_function("document.getElementById('portrait').getContext('2d').getImageData(0,0,1,1).data[3]===255")
    state["frame_delay"] = .4
    page.wait_for_timeout(180)
    state["config"]["face"] = "mask"
    state["revision"] += 1
    page.wait_for_timeout(700)
    assert page.evaluate("document.getElementById('portrait').getContext('2d').getImageData(0,0,1,1).data[3]") == 0
    assert state["posts"] == []
    page.close()


def test_config_failure_does_not_reveal_or_resume_portrait(browser, studio_server):
    base, state = studio_server
    page = browser.new_page()
    ready(page, base)
    page.click("#studioPreviewBtn")
    page.wait_for_function("document.getElementById('studioPlaceholder').hidden")
    state["fail_config"] = True
    page.select_option("#studioFace", "mask")
    page.wait_for_function("document.getElementById('studioStatus').textContent.includes('未保存')")
    page.click("#studioPreviewBtn")
    page.wait_for_function("document.getElementById('studioStatus').textContent.includes('重新保存')")
    assert page.evaluate("document.getElementById('studioPlaceholder').hidden") is False
    page.close()
