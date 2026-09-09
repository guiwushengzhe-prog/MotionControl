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
from urllib.parse import parse_qs, unquote, urlparse

from control_kernel import ControlKernel, LocalControlRuntime, NativeCameraService
from input_bridge import InputBridge
from game_profiles import GameProfileStore, action_catalog
from output_backend import GAMEPAD_AXES, KEY_CODES, XUSB_GAMEPAD_BUTTONS, GlobalHotkeys, KeyboardOutput, OutputManager, _UNSET
from voice_backend import SYSTEM_HEAD_CALIBRATION_START, VoiceService
from scene_layout import SceneLayoutManager

# Product version. 1.00 is the first productized stable UI/UX release.
VERSION = "1.00"


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
# MediaPipe Tasks 1.0.x rejects the older formal task's missing normalization
# metadata. This versioned sibling is a known-good local copy; the original
# MODEL_RELATIVE file remains untouched and is only the fallback when the
# compatibility copy is absent.
MODEL_COMPAT_RELATIVE = Path("mediapipe") / "pose_landmarker_full_compatible_075.task"

OUTPUT = OutputManager(ROOT)
KERNEL = ControlKernel(OUTPUT)
RUNTIME = LocalControlRuntime(KERNEL, NativeCameraService(KERNEL))
SCENE = SceneLayoutManager(ROOT)
if SCENE.session:
    KERNEL.configure_scene_layout(SCENE.session)
PROFILES = GameProfileStore(ROOT)
KERNEL.configure_bindings(PROFILES.effective_profile().get("bindings", {}))


def _apply_effective_profile() -> dict:
    profile = PROFILES.effective_profile()
    KERNEL.configure_bindings(profile.get("bindings", {}))
    return profile


def _scene_apply_current() -> dict:
    if SCENE.session:
        KERNEL.configure_scene_layout(SCENE.session)
    status = SCENE.status()
    bridge = globals().get("INPUT_BRIDGE")
    payload_fn = globals().get("_phone_control_payload")
    if bridge is not None and callable(payload_fn):
        broadcaster = getattr(bridge, "broadcast_control_config", None)
        if broadcaster is not None:
            try:
                broadcaster(payload_fn())
            except Exception:
                pass
    return status


def _scene_capture_with_frame(frame, purpose: str) -> dict:
    # Scene authoring is deliberately low-frequency.  Use a recent multi-frame
    # median pose here so one MediaPipe jump cannot place all seven regions in
    # the wrong location.  Gameplay itself still uses the newest frame.
    pose = KERNEL.stable_pose_snapshot(window_s=0.90, min_samples=6) or KERNEL.latest_pose
    if purpose == "capture":
        SCENE.capture_reference(frame, pose, KERNEL.zone_rects)
    elif purpose == "rematch":
        result = SCENE.rematch(frame, pose)
        if not result.get("last_result", {}).get("ok"):
            raise ValueError(result.get("last_result", {}).get("message") or "场景重新匹配失败")
    else:
        raise ValueError("unknown scene purpose")
    return _scene_apply_current()


def _scene_capture_local(purpose: str) -> dict:
    frame = RUNTIME.camera.latest_frame()
    if frame is None:
        raise RuntimeError("当前电脑摄像头没有可用画面")
    return _scene_capture_with_frame(frame, purpose)


def _scene_snapshot_from_phone(jpeg: bytes, purpose: str, device_id: str) -> dict:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("处理手机截图需要 OpenCV 与 NumPy") from exc
    frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("手机返回的 JPEG 无法解码")
    result = _scene_capture_with_frame(frame, purpose)
    return {"ok": True, "scene": result}


def emergency_stop_all() -> dict:
    KERNEL.cancel_calibration("紧急停止")
    return OUTPUT.emergency_stop()


HOTKEYS = GlobalHotkeys(OUTPUT, emergency_stop=emergency_stop_all)


def execute_voice_action(action: dict) -> dict:
    """Keep system voice commands at the local control-kernel boundary.

    A selected game Profile may override a stable voice command id.  The
    recognizer and command vocabulary remain unchanged; only the final action
    is selected at this boundary.
    """
    if str(action.get("type", "")).lower() != "system":
        command_id = str(action.get("command_id", "")).strip()
        if command_id:
            binding = KERNEL.control_bindings.get(f"voice.{command_id}")
            if isinstance(binding, dict):
                if binding.get("disabled"):
                    return {"executed": False, "reason": "当前游戏未启用这条语音"}
                if isinstance(binding.get("action"), dict):
                    mapped = dict(binding["action"])
                    mapped["source"] = action.get("source", "voice")
                    return OUTPUT.execute_action(mapped)
            if command_id.startswith("game.profile_slot_"):
                return {"executed": False, "reason": "当前游戏未设置这条备用语音"}
        return OUTPUT.execute_action(action)
    target = str(action.get("target", "")).strip().upper()
    # Output start/stop
    if target == "OUTPUT.START":
        return {"executed": True, **OUTPUT.set_config(enabled=True)}
    if target == "OUTPUT.STOP":
        return {"executed": True, **OUTPUT.set_config(enabled=False)}
    # Head center
    if target == "HEAD.CENTER":
        KERNEL.set_current_center()
        return {"executed": True}
    # Head calibrate (both naming conventions)
    if target in {"HEAD.CALIBRATE", SYSTEM_HEAD_CALIBRATION_START}:
        if not VOICE.source_is_active(action.get("voice_source_id")):
            return {"executed": False, "reason": "语音源已断开，未执行头控校准"}
        if RUNTIME.body_mode == "computer":
            if not RUNTIME.camera.status().get("running"):
                return {"executed": False, "reason": "电脑身体源未启动，未执行头控校准"}
        elif RUNTIME.body_mode == "phone":
            if not KERNEL.status().get("active_body_source"):
                return {"executed": False, "reason": "手机身体源尚未提供姿态，未执行头控校准"}
        else:
            return {"executed": False, "reason": "当前没有可用身体源，未执行头控校准"}
        RUNTIME.start_calibration()
        return {"executed": True, "system_action": target}
    # Scene capture/rematch
    if target in {"SCENE.CAPTURE_REFERENCE", "SCENE.REMATCH"}:
        purpose = "capture" if target.endswith("CAPTURE_REFERENCE") else "rematch"
        if RUNTIME.body_mode == "phone":
            data = INPUT_BRIDGE.request_scene_snapshot(purpose)
            return {"executed": True, **data}
        data = _scene_capture_local(purpose)
        return {"executed": True, "scene": data}
    return {"executed": False, "reason": f"不支持的系统语音命令：{target}"}


VOICE = VoiceService(
    ROOT,
    execute_voice_action,
    emergency_stop=emergency_stop_all,
    clear_source=OUTPUT.clear_source,
)
INPUT_BRIDGE = InputBridge(OUTPUT, KERNEL, voice=VOICE)
INPUT_BRIDGE.configure_scene_snapshot_handler(_scene_snapshot_from_phone)

def _phone_control_payload() -> dict:
    profile = PROFILES.effective_profile()
    scene = SCENE.status()
    return {
        "type": "control_config_v1",
        "version": VERSION,
        "game": {"id": profile.get("id"), "name": profile.get("name"), "appid": profile.get("appid")},
        "bindings": profile.get("bindings", {}),
        "zones": scene.get("zones", {}),
        "vertical_look": scene.get("vertical_look", {}),
    }

provider = getattr(INPUT_BRIDGE, "configure_control_config_provider", None)
if provider is not None:
    provider(_phone_control_payload)
MODEL_ROOT: Path | None = None
MODEL_PATH: Path | None = None
MOTION_CONFIG_FILE = CONFIG_DIR / "motion_mappings.json"
DEFAULT_MOTIONS = [
    {"id": "march", "name": "原地踏步", "enabled": False, "type": "gamepad_axis", "target": "LS_UP"},
    {"id": "calf_back", "name": "小腿向后（左/右）", "enabled": False, "type": "gamepad", "target": "B"},
    {"id": "squat", "name": "下蹲", "enabled": False, "type": "gamepad", "target": "X"},
    {"id": "hands_up", "name": "双手举过头顶", "enabled": False, "type": "gamepad", "target": "Y"},
    {"id": "jumping_jack", "name": "开合跳", "enabled": False, "type": "gamepad", "target": "A"},
    {"id": "side_step_jack", "name": "侧步开合", "enabled": False, "type": "gamepad", "target": "B"},
    {"id": "cross_knee_elbow", "name": "提膝碰对侧肘", "enabled": False, "type": "gamepad", "target": "X"},
]


def _effective_voice_catalog_action(item: dict, voice_bindings: dict) -> dict | None:
    if str(item.get("kind", "")) == "system":
        return {"type": "system", "target": str(item.get("default_target", "")), "behavior": "tap"}
    ident = str(item.get("id", ""))
    binding = voice_bindings.get(ident) if isinstance(voice_bindings, dict) else None
    if isinstance(binding, dict):
        if binding.get("disabled"):
            return None
        action = binding.get("action")
        return dict(action) if isinstance(action, dict) else None
    # Numbered slots are compatibility vocabulary, not active F-key commands.
    if ident.startswith("game.profile_slot_"):
        return None
    return {
        "type": str(item.get("kind", "")),
        "target": str(item.get("default_target", "")),
        "behavior": "tap",
    }


def voice_command_catalog() -> dict:
    """Return the recognizer's exact phrases and what each does right now."""
    profile = PROFILES.effective_profile() if "PROFILES" in globals() else {"bindings": {}}
    voice_bindings = profile.get("bindings", {}).get("voice", {})
    commands = []
    # Use the same registry VoiceService actually parses.  Keeping a second UI
    # catalog here allowed a phrase to be shown even when the recognizer did
    # not own it, or to display a stale default after the vocabulary changed.
    for item in VOICE.command_registry.values():
        if not isinstance(item, dict) or not item.get("phrase"):
            continue
        kind = str(item.get("kind", ""))
        commands.append({
            "id": str(item.get("id", "")),
            "label": str(item.get("label", item.get("phrase", ""))),
            "phrase": str(item.get("phrase", "")),
            "kind": kind,
            "system_fixed": kind == "system",
            "default_action": {
                "type": kind,
                "target": str(item.get("default_target", "")),
                "behavior": "tap",
            },
            "effective_action": _effective_voice_catalog_action(item, voice_bindings),
        })
    return {"version": VERSION, "count": len(commands), "commands": commands}

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
    # Explicit config in model_root.txt takes priority over the default local
    # models directory, which may only contain Vosk (not MediaPipe pose).
    cfg = CONFIG_DIR / "model_root.txt"
    if cfg.exists():
        text = cfg.read_text(encoding="utf-8-sig").strip().strip('"')
        if text:
            configured = Path(text)
            candidates.append(configured if configured.is_absolute() else ROOT / configured)
    candidates.append(ROOT / "models")
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
    compatible = root / MODEL_COMPAT_RELATIVE
    if compatible.is_file():
        return compatible.resolve()
    direct = root / MODEL_RELATIVE
    if direct.is_file():
        return direct.resolve()
    try:
        return next((p.resolve() for p in root.rglob("pose_landmarker_full.task") if p.is_file()), None)
    except OSError:
        return None


def performance_snapshot() -> dict:
    """Combine the active source's machine-readable camera/input metrics."""
    if RUNTIME.body_mode == "phone":
        data = INPUT_BRIDGE.performance()
        # Keep the schema stable when the active source is a phone; camera
        # backend fields are intentionally unknown rather than fabricated.
        data.setdefault("backend", None)
        data.setdefault("backend_name", None)
        data.setdefault("requested_fps", None)
        data.setdefault("actual_capture_fps", None)
        return data
    return RUNTIME.performance()


def _format_perf(value, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value}{suffix}"


def performance_line() -> str:
    data = performance_snapshot()
    resolution = data.get("camera_resolution") or {}
    if isinstance(resolution, dict):
        resolution_text = f"{resolution.get('width', 0)}x{resolution.get('height', 0)}"
    else:
        resolution_text = str(resolution)
    return (
        "PERF "
        f"source={data.get('source', '-')} "
        f"res={resolution_text} "
        f"backend={data.get('backend_name') or data.get('backend', '-')} "
        f"requested={_format_perf(data.get('requested_fps'), 'fps')} "
        f"capture={_format_perf(data.get('capture_fps'), 'fps')} "
        f"actual_capture={_format_perf(data.get('actual_capture_fps'), 'fps')} "
        f"infer={_format_perf(data.get('inference_fps'), 'fps')} "
        f"infer_ms={_format_perf(data.get('inference_avg_ms'), 'ms')} "
        f"p95={_format_perf(data.get('inference_p95_ms'), 'ms')} "
        f"age={_format_perf(data.get('pose_frame_age_ms'), 'ms')} "
        f"latency={_format_perf(data.get('total_latency_ms'), 'ms')} "
        f"humans={data.get('recent_humans', '-')} "
        f"drop={data.get('dropped_frames', 0)} skip={data.get('skipped_frames', 0)}"
    )


def performance_logger(stop_event: threading.Event) -> None:
    quiet_ticks = 0
    while not stop_event.wait(5.0):
        data = performance_snapshot()
        active = bool(data.get("running")) or bool(data.get("preview_ready")) or data.get("recent_humans", 0)
        if not active:
            quiet_ticks += 1
            if quiet_ticks % 3:
                continue
        else:
            quiet_ticks = 0
        print(performance_line(), flush=True)


class Handler(SimpleHTTPRequestHandler):
    # The UI polls several small status resources.  Persistent HTTP/1.1
    # connections avoid a new TCP handshake/TIME_WAIT entry for every poll.
    # All JSON/image responses below provide Content-Length; WebSocket upgrade
    # explicitly closes the HTTP connection before taking over the socket.
    protocol_version = "HTTP/1.1"

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
        if route == "/api/shutdown":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "shutdown is loopback-only"}, 403)
                return
            self._send_json({"ok": True, "version": VERSION, "shutting_down": True})
            # HTTPServer.shutdown must be called from another thread so this
            # request can finish sending its acknowledgement first.
            threading.Thread(target=self.server.shutdown, name="motion-shutdown", daemon=True).start()
            return
        if route == "/ws/input":
            INPUT_BRIDGE.serve_websocket(self, parsed.query)
            return
        if route == "/api/game-profiles/catalog":
            query = parse_qs(parsed.query).get("q", [""])[0]
            self._send_json({"version": VERSION, **PROFILES.list_games(query)})
            return
        if route == "/api/game-profiles/selected":
            self._send_json({"version": VERSION, "profile": PROFILES.effective_profile()})
            return
        if route == "/api/game-profiles/profile":
            profile_id = parse_qs(parsed.query).get("id", [""])[0]
            try:
                self._send_json({"version": VERSION, "profile": PROFILES.get_profile(profile_id)})
            except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 404)
            return
        if route == "/api/output/actions":
            self._send_json({"version": VERSION, "actions": action_catalog()})
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
        if route == "/api/camera/preview.jpg":
            preview = RUNTIME.latest_preview()
            if not preview:
                self.send_error(404, "camera preview unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(preview)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(preview)
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
        if route == "/api/output/xinput":
            self._send_json({"version": VERSION, **OUTPUT.xinput_status()})
            return
        if route == "/api/input/status":
            data = INPUT_BRIDGE.status()
            # Keep the historical full response by default.  The browser only
            # needs bridge/source fields while polling, so ?brief=1 avoids a
            # second full Runtime/Kernel snapshot (including the 33-point pose)
            # on every input-status tick.
            brief = parse_qs(parsed.query).get("brief", ["0"])[0].strip().lower()
            if brief not in {"1", "true", "yes"}:
                data["runtime"] = RUNTIME.status()
            data["version"] = VERSION
            self._send_json(data)
            return
        if route == "/api/kernel/status":
            self._send_json({"version": VERSION, **RUNTIME.status()})
            return
        if route == "/api/performance":
            data = performance_snapshot()
            data["version"] = VERSION
            self._send_json(data)
            return
        if route == "/api/camera/config":
            self._send_json(RUNTIME.camera_backend_config())
            return
        if route == "/api/voice/status":
            self._send_json({"version": VERSION, **VOICE.status()})
            return
        if route == "/api/voice/commands":
            self._send_json(voice_command_catalog())
            return
        if route == "/api/scene/status":
            self._send_json({"version": VERSION, **SCENE.status()})
            return
        if route == "/api/scene/reference.jpg":
            if not SCENE.reference_path.is_file():
                self.send_error(404, "scene reference unavailable")
                return
            self._serve_file(SCENE.reference_path)
            return
        super().do_GET()

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/voice/audio":
            self._send_json({
                "ok": False,
                "error": "browser voice endpoint disabled; use the local computer microphone or /ws/input voice_command(command_id)",
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
        if route in {"/api/game-profiles/select", "/api/game-profiles/overrides"}:
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "game profile changes are loopback-only"}, 403)
                return
            try:
                if route.endswith("/select"):
                    profile = PROFILES.select(str(body.get("id", "")))
                else:
                    profile = PROFILES.set_overrides(body.get("overrides", {}))
                KERNEL.configure_bindings(profile.get("bindings", {}))
                broadcaster = getattr(INPUT_BRIDGE, "broadcast_control_config", None)
                if broadcaster is not None:
                    broadcaster(_phone_control_payload())
                self._send_json({"ok": True, "profile": profile})
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
        if route == "/api/camera/config":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "camera config is loopback-only"}, 403)
                return
            try:
                self._send_json({"ok": True, **RUNTIME.configure_camera_backend(body.get("backend", body.get("preference", "auto")))})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **RUNTIME.camera_backend_config()}, 400)
            return
        if route == "/api/head/calibration/start":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "head calibration is loopback-only"}, 403)
                return
            try:
                RUNTIME.start_calibration()
                self._send_json({"ok": True, **RUNTIME.status()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **RUNTIME.status()}, 400)
            return
        if route == "/api/head/calibration/cancel":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "head calibration is loopback-only"}, 403)
                return
            self._send_json({"ok": True, **KERNEL.cancel_calibration("用户取消")})
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
                    algorithm=body.get("algorithm"),
                    deadzone=body.get("deadzone"),
                    sensitivity_x=body.get("sensitivity_x"),
                    sensitivity_y=body.get("sensitivity_y"),
                    enabled=body.get("enabled"),
                    invert_x=body.get("invert_x"), invert_y=body.get("invert_y"),
                    horizontal_algorithm=body.get("horizontal_algorithm"),
                    vertical_look_source=body.get("vertical_look_source", body.get("verticalLookSource")),
                    vertical_exclusive=body.get("vertical_exclusive", body.get("exclusive_axes")),
                    body_motion_guard=body.get("body_motion_guard"),
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
        if route in {"/api/scene/capture", "/api/scene/rematch", "/api/scene/layout"}:
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "scene config is loopback-only"}, 403)
                return
            try:
                if route == "/api/scene/layout":
                    data = SCENE.update_reference_layout(body)
                    _scene_apply_current()
                    self._send_json({"ok": True, **data})
                else:
                    purpose = "capture" if route.endswith("capture") else "rematch"
                    if RUNTIME.body_mode == "phone":
                        data = INPUT_BRIDGE.request_scene_snapshot(purpose)
                        self._send_json({"ok": True, **data, "scene": SCENE.status()})
                    else:
                        data = _scene_capture_local(purpose)
                        self._send_json({"ok": True, **data})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **SCENE.status()}, 400)
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
                    xinput_merge_enabled=(body.get("xinput_merge_enabled") if "xinput_merge_enabled" in body else None),
                    physical_xinput_user=(body.get("physical_xinput_user") if "physical_xinput_user" in body else _UNSET),
                )
            elif route == "/api/output/xinput":
                data = OUTPUT.configure_xinput_merge(
                    enabled=body.get("enabled") if "enabled" in body else None,
                    user=(body.get("user") if "user" in body else body.get("physical_xinput_user")) if ("user" in body or "physical_xinput_user" in body) else _UNSET,
                )
            elif route == "/api/output/frame":
                OUTPUT.apply(float(body.get("x", 0.0)), float(body.get("y", 0.0)))
                data = OUTPUT.status()
            elif route == "/api/output/buttons":
                data = OUTPUT.set_buttons(body.get("buttons", []), source="zones")
            elif route == "/api/output/stop":
                data = emergency_stop_all()
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
    print(f"MotionControl 1.00 · body zones + motions + voice · v{VERSION}")
    print("Model root:", MODEL_ROOT or "NOT FOUND")
    print("MediaPipe Full:", MODEL_PATH or "NOT FOUND")

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in {98, 10048}:
            print(
                f"启动失败：端口 {args.port} 已被占用，可能已有 MotionControl 实例在运行。"
                f" 请关闭旧实例或改用 --port；当前进程不会结束其他进程。",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(2) from exc
        raise
    HOTKEYS.start()
    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{display_host}:{args.port}/"
    print("Open:", url)
    perf_stop = threading.Event()
    perf_thread = threading.Thread(target=performance_logger, args=(perf_stop,), name="motion-performance-log", daemon=True)
    perf_thread.start()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        perf_stop.set()
        perf_thread.join(timeout=1.0)
        OUTPUT.emergency_stop()
        VOICE.close()
        INPUT_BRIDGE.close()
        RUNTIME.close()
        HOTKEYS.close()
        OUTPUT.close()
        server.server_close()


if __name__ == "__main__":
    main()
