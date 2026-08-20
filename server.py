from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from control_kernel import ControlKernel, LocalControlRuntime, NativeCameraService
from input_bridge import InputBridge
from output_backend import GAMEPAD_AXES, KEY_CODES, XUSB_GAMEPAD_BUTTONS, GlobalHotkeys, KeyboardOutput, OutputManager
from voice_backend import VoiceService

VERSION = "0.7.4"


def application_root() -> Path:
    """Return the source root or the portable executable directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = application_root()
WEB_DIR = ROOT / "web"
CONFIG_DIR = ROOT / "config"
DEFAULT_MODEL_ROOT = Path(r"I:\MotionControl-Pose-Models\models")
MODEL_RELATIVE = Path("mediapipe") / "pose_landmarker_full.task"

OUTPUT = OutputManager(ROOT)
KERNEL = ControlKernel(OUTPUT)
RUNTIME = LocalControlRuntime(KERNEL, NativeCameraService(KERNEL))
HOTKEYS = GlobalHotkeys(OUTPUT)
VOICE = VoiceService(
    ROOT,
    OUTPUT.execute_action,
    emergency_stop=OUTPUT.emergency_stop,
    clear_source=OUTPUT.clear_source,
)
INPUT_BRIDGE = InputBridge(OUTPUT, KERNEL, voice=VOICE)
MODEL_ROOT: Path | None = None
MODEL_PATH: Path | None = None
MOTION_CONFIG_FILE = CONFIG_DIR / "motion_mappings.json"
DEFAULT_MOTIONS = [
    {"id": "march", "name": "原地踏步", "enabled": False, "type": "gamepad_axis", "target": "LS_UP"},
    {"id": "calf_back", "name": "小腿向后（左/右）", "enabled": False, "type": "gamepad", "target": "B"},
    {"id": "squat", "name": "下蹲", "enabled": False, "type": "gamepad", "target": "X"},
    {"id": "hands_up", "name": "双手举过头顶", "enabled": False, "type": "gamepad", "target": "Y"},
]

def _normalize_motion_config(items):
    by_id = {str(x.get("id")): x for x in (items or []) if isinstance(x, dict)}
    out = []
    for default in DEFAULT_MOTIONS:
        src = by_id.get(default["id"], {})
        action_type = str(src.get("type", default["type"])).lower()
        if action_type not in {"keyboard", "gamepad", "gamepad_axis"}:
            action_type = default["type"]
        target = str(src.get("target", default["target"])).strip().upper()
        enabled = bool(src.get("enabled", default["enabled"]))
        if enabled and not target:
            raise ValueError(f"{default['name']} 已启用但没有设置输出")
        if target and action_type == "gamepad" and target not in XUSB_GAMEPAD_BUTTONS:
            raise ValueError(f"{default['name']} 的 Xbox 按键不支持：{target}")
        if target and action_type == "gamepad_axis" and target not in GAMEPAD_AXES:
            raise ValueError(f"{default['name']} 的摇杆方向不支持：{target}")
        if target and action_type == "keyboard":
            keys = [KeyboardOutput.normalize(x) for x in target.split("+") if x.strip()]
            if not keys or len(keys) > 4 or any(k not in KEY_CODES for k in keys):
                raise ValueError(f"{default['name']} 的键盘映射无效：{target}")
        out.append({
            "id": default["id"], "name": default["name"],
            "enabled": enabled,
            "type": action_type, "target": target,
        })
    return out

def load_motion_config():
    try:
        data = json.loads(MOTION_CONFIG_FILE.read_text(encoding="utf-8"))
        return _normalize_motion_config(data.get("motions", []))
    except Exception:
        return _normalize_motion_config([])

def save_motion_config(items):
    motions = _normalize_motion_config(items)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MOTION_CONFIG_FILE.write_text(json.dumps({"motions": motions}, ensure_ascii=False, indent=2), encoding="utf-8")
    return motions

MOTION_CONFIG = load_motion_config()
RUNTIME.configure_motions(MOTION_CONFIG)


def choose_model_root(cli_root: str | None) -> Path | None:
    candidates: list[Path] = []
    if cli_root:
        candidates.append(Path(cli_root))
    env = os.environ.get("POSE_MODEL_ROOT", "").strip().strip('"')
    if env:
        candidates.append(Path(env))
    # A frozen onedir build carries its own models beside the executable.
    # Prefer that copy over development-machine configuration paths.
    candidates.append(ROOT / "models")
    cfg = CONFIG_DIR / "model_root.txt"
    if cfg.exists():
        text = cfg.read_text(encoding="utf-8-sig").strip().strip('"')
        if text:
            configured = Path(text)
            candidates.append(configured if configured.is_absolute() else ROOT / configured)
    candidates.append(DEFAULT_MODEL_ROOT)
    for p in candidates:
        try:
            if p.is_dir():
                return p.resolve()
        except OSError:
            pass
    return None


def resolve_full_model(root: Path | None) -> Path | None:
    if root is None:
        return None
    direct = root / MODEL_RELATIVE
    if direct.is_file():
        return direct.resolve()
    try:
        return next((p.resolve() for p in root.rglob("pose_landmarker_full.task") if p.is_file()), None)
    except OSError:
        return None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data: dict, status: int = 200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return None

    def _serve_file(self, path: Path):
        if not path.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as f:
            while chunk := f.read(1024 * 1024):
                self.wfile.write(chunk)

    def _is_loopback(self) -> bool:
        host = str(self.client_address[0]).split("%", 1)[0]
        return host in {"127.0.0.1", "::1"} or host.startswith("127.")

    def do_GET(self):
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if route == "/ws/input":
            INPUT_BRIDGE.serve_websocket(self, parsed.query)
            return
        if route == "/api/models":
            available = bool(MODEL_PATH and MODEL_PATH.is_file())
            self._send_json({
                "version": VERSION,
                "model_root": str(MODEL_ROOT) if MODEL_ROOT else None,
                "models": [{
                    "id": "mp-full",
                    "name": "MediaPipe Pose Full",
                    "points": 33,
                    "input_size": "256×256",
                    "available": available,
                    "size_bytes": MODEL_PATH.stat().st_size if available else 0,
                }],
            })
            return
        if route == "/api/model/mp-full":
            if MODEL_PATH is None:
                self.send_error(404, "MediaPipe Full model unavailable")
            else:
                self._serve_file(MODEL_PATH)
            return
        if route == "/api/motion/config":
            self._send_json({"version": VERSION, "motions": MOTION_CONFIG})
            return
        if route == "/api/output-status":
            data = OUTPUT.status()
            data["hotkeys"] = HOTKEYS.status()
            self._send_json(data)
            return
        if route == "/api/input/status":
            data = INPUT_BRIDGE.status()
            data["runtime"] = RUNTIME.status()
            self._send_json(data)
            return
        if route == "/api/kernel/status":
            self._send_json(RUNTIME.status())
            return
        if route == "/api/voice/status":
            self._send_json(VOICE.status())
            return
        super().do_GET()

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/voice/audio":
            self._send_json({
                "ok": False,
                "error": "browser voice endpoint disabled; use the local computer microphone or /ws/input voice_text",
            }, 410)
            return

        body = self._body()
        if body is None:
            self._send_json({"ok": False, "error": "invalid json"}, 400)
            return
        if route == "/api/voice/config":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "voice config is loopback-only"}, 403)
                return
            try:
                self._send_json({
                    "ok": True,
                    **VOICE.configure(
                        body.get("mappings", []),
                        wake_word=body.get("wake_word") if "wake_word" in body else None,
                        emergency_stop_phrases=(
                            body.get("emergency_stop_phrases")
                            if "emergency_stop_phrases" in body else None
                        ),
                    ),
                })
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **VOICE.status()}, 400)
            return
        if route == "/api/motion/config":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "motion config is loopback-only"}, 403)
                return
            try:
                global MOTION_CONFIG
                MOTION_CONFIG = save_motion_config(body.get("motions", []))
                RUNTIME.configure_motions(MOTION_CONFIG)
                OUTPUT.set_holds([], source_group="motions")
                self._send_json({"ok": True, "motions": MOTION_CONFIG})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        if route == "/api/input/source":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "input source is loopback-only"}, 403)
                return
            try:
                source = str(body.get("source", "")).strip().lower()
                enabled = bool(body.get("enabled", True))
                # Close the mobile gate before stopping/starting the body source.
                # This keeps a phone frame from racing a source transition.
                INPUT_BRIDGE.set_body_mode("computer")
                INPUT_BRIDGE.clear_mobile_sources()
                VOICE.stop_local_microphone()
                VOICE.disconnect()
                if enabled:
                    data = RUNTIME.set_source(source, start_computer=True)
                else:
                    data = RUNTIME.stop_body()
                INPUT_BRIDGE.set_body_mode(source if enabled else "computer")
                if enabled and source == "computer":
                    voice_data = VOICE.start_local_microphone()
                else:
                    voice_data = VOICE.status()
                self._send_json({"ok": True, **data, "voice": voice_data})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **RUNTIME.status(), "voice": VOICE.status()}, 400)
            return
        if route == "/api/head/calibration/start":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "head calibration is loopback-only"}, 403)
                return
            try:
                KERNEL.start_calibration()
                self._send_json({"ok": True, **RUNTIME.status()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **RUNTIME.status()}, 400)
            return
        if route == "/api/head/calibration/center":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "head calibration is loopback-only"}, 403)
                return
            try:
                KERNEL.set_current_center()
                self._send_json({"ok": True, **RUNTIME.status()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **RUNTIME.status()}, 400)
            return
        if route == "/api/head/config":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "head config is loopback-only"}, 403)
                return
            try:
                KERNEL.configure_head(
                    deadzone_x=body.get("deadzone_x"), deadzone_y=body.get("deadzone_y"),
                    gamma=body.get("gamma"), max_percent_x=body.get("max_percent_x"),
                    max_percent_y=body.get("max_percent_y"), enabled=body.get("enabled"),
                    invert_x=body.get("invert_x"), invert_y=body.get("invert_y"),
                )
                self._send_json({"ok": True, **RUNTIME.status()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **RUNTIME.status()}, 400)
            return
        if route == "/api/motion/state":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "motion state is loopback-only"}, 403)
                return
            try:
                active = {str(x) for x in body.get("active", [])}
                holds = [m for m in MOTION_CONFIG if m.get("enabled") and m.get("id") in active and m.get("target")]
                data = OUTPUT.set_holds(holds, source_group="motions")
                self._send_json({"ok": True, "active": sorted(active), **data})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **OUTPUT.status()}, 400)
            return
        if not route.startswith("/api/output/"):
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        if not self._is_loopback():
            self._send_json({"ok": False, "error": "game output is loopback-only"}, 403)
            return
        try:
            if route == "/api/output/config":
                data = OUTPUT.set_config(
                    mode=body.get("mode"),
                    enabled=body.get("enabled") if "enabled" in body else None,
                    mouse_speed_x=body.get("mouse_speed_x"),
                    mouse_speed_y=body.get("mouse_speed_y"),
                    gamepad_gain=body.get("gamepad_gain"),
                )
            elif route == "/api/output/frame":
                OUTPUT.apply(float(body.get("x", 0.0)), float(body.get("y", 0.0)))
                data = OUTPUT.status()
            elif route == "/api/output/buttons":
                data = OUTPUT.set_buttons(body.get("buttons", []), source="zones")
            elif route == "/api/output/stop":
                data = OUTPUT.emergency_stop()
            else:
                self._send_json({"ok": False, "error": "not found"}, 404)
                return
            data["hotkeys"] = HOTKEYS.status()
            self._send_json({"ok": True, **data})
        except Exception as exc:
            OUTPUT.emergency_stop()
            self._send_json({"ok": False, "error": str(exc), **OUTPUT.status()}, 400)


def main():
    global MODEL_ROOT, MODEL_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0", help="监听地址；默认允许局域网手机连接")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model-root", default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    MODEL_ROOT = choose_model_root(args.model_root)
    MODEL_PATH = resolve_full_model(MODEL_ROOT)
    RUNTIME.configure_model(MODEL_PATH)
    INPUT_BRIDGE.configure_endpoint(args.host, args.port)
    print(f"MotionControl body zones + four motions + voice v{VERSION}")
    print("Model root:", MODEL_ROOT or "NOT FOUND")
    print("MediaPipe Full:", MODEL_PATH or "NOT FOUND")

    HOTKEYS.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{display_host}:{args.port}/"
    print("Open:", url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        OUTPUT.emergency_stop()
        VOICE.close()
        INPUT_BRIDGE.close()
        RUNTIME.close()
        HOTKEYS.close()
        OUTPUT.close()
        server.server_close()


if __name__ == "__main__":
    main()
