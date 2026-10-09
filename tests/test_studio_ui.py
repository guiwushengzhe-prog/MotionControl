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
                picture = state.get("picture", PNG)
                self.send_header("Content-Length", str(len(picture) if original else 0))
                self.end_headers()
                if original:
                    self.wfile.write(picture)
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
                    state["config"].update(body)
                    state["revision"] += 1
                    self.respond({"config": state["config"]})
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


def test_composite_preview_crop_cancel_and_recording_reuse_selected_screen(browser, studio_server):
    base, state = studio_server
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(FAKE_CAPTURE)
    ready(page, base)
    page.click("#studioScreenBtn")
    page.wait_for_function("document.getElementById('studioViewLabel').textContent==='合成预览' && document.getElementById('studioStartBtn').textContent==='开始录制'")
    page.wait_for_function("document.getElementById('studioCanvas').getContext('2d').getImageData(1100,600,1,1).data[0]===255")
    pixel = page.evaluate("Array.from(document.getElementById('studioCanvas').getContext('2d').getImageData(640,360,1,1).data)")
    # 视频流经过颜色转换，允许单个通道的舍入误差。
    assert all(abs(actual - expected) <= 2 for actual, expected in zip(pixel, [10, 132, 255, 255]))
    assert not any(path == "/api/studio/recording/start" for path, _ in state["posts"])
    page.click("#studioCropBtn")
    page.fill("#studioCropleft", "20")
    page.locator("#studioCropleft").dispatch_event("change")
    page.click("#studioCropCancel")
    assert not any("screen_crop" in body for path, body in state["posts"] if path == "/api/studio/config")
    page.click("#studioCropBtn")
    page.fill("#studioCropleft", "20")
    page.locator("#studioCropleft").dispatch_event("change")
    page.click("#studioCropApply")
    page.wait_for_function("!document.getElementById('studioCropDialog').open")
    assert state["config"]["screen_crop"] == {"x": .2, "y": 0, "width": .8, "height": 1}
    page.click("#studioStartBtn")
    page.wait_for_selector("#studioRecordingBadge:not([hidden])")
    assert page.evaluate("captureRequests.length") == 1
    page.wait_for_timeout(1100)
    page.click("#studioStopBtn")
    page.wait_for_function("document.getElementById('studioStatus').textContent.includes('录制完成')")
    assert state["chunks"]
    assert page.evaluate("syntheticTracks.every(track=>track.readyState==='ended')")
    assert errors == []
    page.close()


def test_cartoon_presets_save_real_image_and_overlay_uses_person_crop(browser, studio_server):
    base, state = studio_server
    page = browser.new_page()
    ready(page, base)
    page.select_option("#studioFace", "avatar")
    page.click('[data-avatar-preset="fox"]')
    page.wait_for_function("document.querySelector('[data-avatar-preset=fox]').getAttribute('aria-pressed')==='true'")
    page.wait_for_function("document.getElementById('studioAvatarStatus').textContent.includes('卡通')")
    assert state["config"]["avatar_preset"] == "fox"
    assert state["config"]["avatar_data_url"].startswith("data:image/png;base64,")
    # 直播页复用人物裁切，透明背景不变成不透明黑块。
    state["config"].update(face="original", portrait_crop={"x": .25, "y": 0, "width": .5, "height": 1})
    page.goto(base + "/studio-overlay.html")
    page.wait_for_function("document.getElementById('portrait').width===2 && document.getElementById('portrait').height===4")
    assert page.evaluate("document.getElementById('portrait').getContext('2d').getImageData(0,0,1,1).data[3]") == 255
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


def test_camera_zoom_keeps_viewport_clips_overflow_and_is_saved_for_overlay(browser, studio_server):
    base, state = studio_server
    # 横向红绿蓝三段，放大后固定竖屏框只留下中间的绿色。
    row = b"\0" + b"\xff\0\0\xff" * 60 + b"\0\xff\0\xff" * 60 + b"\0\0\xff\xff" * 60
    state["picture"] = (b"\x89PNG\r\n\x1a\n"
                        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 180, 100, 8, 6, 0, 0, 0))
                        + _png_chunk(b"IDAT", zlib.compress(row * 100)) + _png_chunk(b"IEND", b""))
    page = browser.new_page()
    ready(page, base)
    page.select_option("#studioLayout", "portrait")
    page.click("#studioPreviewBtn")
    page.wait_for_function("document.getElementById('studioPlaceholder').hidden")
    assert page.evaluate("Array.from(document.getElementById('studioCanvas').getContext('2d').getImageData(20,100,1,1).data)") == [16, 16, 20, 255]
    page.locator("#studioZoom").evaluate("el=>{el.value='325';el.dispatchEvent(new Event('input',{bubbles:true}))}")
    assert page.locator("#studioZoomValue").text_content() == "325%"
    assert page.evaluate("[document.getElementById('studioCanvas').width,document.getElementById('studioCanvas').height]") == [720, 1280]
    assert page.evaluate("Array.from(document.getElementById('studioCanvas').getContext('2d').getImageData(20,100,1,1).data)") == [0, 255, 0, 255]
    page.locator("#studioZoom").dispatch_event("change")
    page.wait_for_function("document.getElementById('studioPlaceholder').hidden")
    assert state["config"]["portrait_zoom"] == 325
    page.reload()
    page.wait_for_function("!document.getElementById('studioZoom').disabled")
    assert page.locator("#studioZoom").input_value() == "325"
    # 合成时放大只发生在人物框内，不能覆盖人物框以外的游戏像素。
    result = page.evaluate("""async()=>{
      const {compose}=await import('/js/studio-compose.js');
      const screen=document.createElement('canvas'),person=document.createElement('canvas'),out=document.createElement('canvas');
      screen.width=out.width=1280;screen.height=out.height=720;person.width=180;person.height=100;
      const s=screen.getContext('2d'),p=person.getContext('2d'),o=out.getContext('2d');
      s.fillStyle='#123456';s.fillRect(0,0,1280,720);
      for(const [x,color] of [[0,'red'],[60,'lime'],[120,'blue']]){p.fillStyle=color;p.fillRect(x,0,60,100)}
      compose(o,screen,person,1280,720,{size:30,position:'bottom-right',portrait_zoom:325});
      return [[870,600],[1000,600],[1265,600]].map(([x,y])=>Array.from(o.getImageData(x,y,1,1).data));
    }""")
    assert result == [[18, 52, 86, 255], [0, 255, 0, 255], [18, 52, 86, 255]]
    page.goto(base + "/studio-overlay.html")
    page.wait_for_function("document.getElementById('portrait').width===180 && document.getElementById('portrait').getContext('2d').getImageData(0,0,1,1).data[1]===255")
    assert page.evaluate("document.getElementById('portrait').height") == 100
    page.close()


def test_zoom_drag_clamps_saves_and_center_fill_shortcuts_work(browser, studio_server):
    base, state = studio_server
    row = b"\0" + b"\xff\0\0\xff" * 60 + b"\0\xff\0\xff" * 60 + b"\0\0\xff\xff" * 60
    state["picture"] = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 180, 100, 8, 6, 0, 0, 0))
                        + _png_chunk(b"IDAT", zlib.compress(row * 100)) + _png_chunk(b"IEND", b""))
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    ready(page, base)
    page.select_option("#studioLayout", "portrait")
    page.click("#studioPreviewBtn")
    page.wait_for_function("!document.getElementById('studioFill').disabled")
    page.click("#studioFill")
    page.wait_for_function("document.getElementById('studioCanvas').classList.contains('is-draggable')")
    assert state["config"]["portrait_zoom"] == 320
    rect = page.locator("#studioCanvas").bounding_box()
    x, y = rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 500, y, steps=5)
    page.mouse.up()
    page.wait_for_function("!document.getElementById('studioCenter').disabled && document.getElementById('studioPlaceholder').hidden")
    assert state["config"]["portrait_pan"] == {"x": 1, "y": 0}
    assert page.evaluate("Array.from(document.getElementById('studioCanvas').getContext('2d').getImageData(20,640,1,1).data)") == [255, 0, 0, 255]
    page.reload()
    page.wait_for_function("!document.getElementById('studioCenter').disabled")
    page.click("#studioPreviewBtn")
    page.wait_for_function("document.getElementById('studioPlaceholder').hidden")
    page.click("#studioCenter")
    page.wait_for_function("document.getElementById('studioCenter').disabled && document.getElementById('studioPlaceholder').hidden")
    assert state["config"]["portrait_pan"] == {"x": 0, "y": 0}
    assert state["config"]["portrait_zoom"] == 320, "居中不改变放大比例"
    page.click("#studioZoomOut")
    page.wait_for_function("document.getElementById('studioZoomValue').textContent==='295%' && document.getElementById('studioPlaceholder').hidden")
    page.click("#studioZoomIn")
    page.wait_for_function("document.getElementById('studioZoomValue').textContent==='320%' && document.getElementById('studioPlaceholder').hidden")
    # 直播画面沿用保存的偏移，不申请设备或修改设置。
    state["config"]["portrait_pan"] = {"x": 1, "y": 0}
    posts = len(state["posts"])
    page.goto(base + "/studio-overlay.html")
    page.wait_for_function("document.getElementById('portrait').width===180 && document.getElementById('portrait').getContext('2d').getImageData(0,50,1,1).data[0]===255")
    assert all(path == "/api/studio/video-demand" and body["enabled"] is False for path, body in state["posts"][posts:])
    assert errors == []
    page.close()
