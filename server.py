from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
import queue
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# 换包不在这里做。server.py 就住在要被换掉的那个目录里，而启动时的工作目录也是
# 它——Windows 不允许删除或改名自己所在的目录，退回时会删到一半然后失败，把安装
# 掏空。真撞过一次，测试里 app/ 变成了空的。
#
# 所以换包交给 app/ 外面的 launcher.py，它的工作目录在外面。这里只负责在两个
# 监听都起来之后确认"这一份跑起来了"。
_APP_DIR = Path(__file__).resolve().parent

from motioncontrol.cloud_client import CloudClient, CloudError, backup_user_data
from motioncontrol.custom_poses import CustomPoseError, CustomPoseStore
from motioncontrol.key_macros import MacroError, MacroStore
from motioncontrol.pose_capture import DEFAULT_POSE_DELAY_S, PoseCaptureTimer
from motioncontrol.control_kernel import ControlKernel, LocalControlRuntime, NativeCameraService
from motioncontrol.input_bridge import InputBridge
from motioncontrol.game_profiles import GameProfileStore, ProfileSelectionChanged
from motioncontrol_shared import macro_schema, pose_library
from motioncontrol_shared.describe import trigger_name
from motioncontrol_shared.profile_schema import action_catalog
from motioncontrol_shared.motion_conflicts import motion_conflict_payload, validate_motion_config
from motioncontrol.output_backend import GAMEPAD_AXES, KEY_CODES, XUSB_GAMEPAD_BUTTONS, GlobalHotkeys, KeyboardOutput, OutputManager, _UNSET
from motioncontrol_shared.model_share import ModelShare
from motioncontrol.voice_backend import SYSTEM_HEAD_CALIBRATION_START, VoiceService, find_vosk_model
from motioncontrol.scene_layout import SceneLayoutManager
from motioncontrol.discovery import DiscoveryResponder
from motioncontrol.user_paths import migrate_legacy_user_data, user_data_root, user_path

# 版本号只有一处，在 motioncontrol/version.py。这里不再写数字：写了就会有第二个
# 数字要记得跟着改，而漏改一次是看不出来的——界面和文件名各说各的。
from motioncontrol.version import VERSION  # noqa: E402

# 这次启动之后查更新的结果，界面上要显示。
UPDATE_STATE: dict = {"state": "unknown"}


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

# User data moved out of the program folder in 2.0.x.  Run the one-time copy
# before anything below constructs, because SceneLayoutManager, GameProfileStore
# and VoiceService all read their files at import time -- migrating afterwards
# would silently hand the user defaults on their first upgraded launch.
_MIGRATED = migrate_legacy_user_data(ROOT)
if _MIGRATED:
    print("已从旧版程序目录迁移用户数据：" + "、".join(_MIGRATED))
print("用户数据目录：", user_data_root())

OUTPUT = OutputManager(ROOT)
KERNEL = ControlKernel(OUTPUT)
RUNTIME = LocalControlRuntime(KERNEL, NativeCameraService(KERNEL))
SCENE = SceneLayoutManager(ROOT)
if SCENE.session:
    KERNEL.configure_scene_layout(SCENE.session)
PROFILE_UPDATE_LOCK = threading.RLock()
PROFILES = GameProfileStore(ROOT)
KERNEL.configure_bindings(PROFILES.effective_profile().get("bindings", {}))


def _apply_effective_profile() -> dict:
    profile = PROFILES.effective_profile()
    KERNEL.configure_bindings(profile.get("bindings", {}))
    VOICE.configure_profile_bindings(profile.get("bindings", {}))
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
        SCENE.capture_reference(frame, pose, KERNEL.zone_rects,
                                vertical_preferences=KERNEL.vertical_look)
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
    result = OUTPUT.emergency_stop()
    _broadcast_game_output_state()
    return result


def _broadcast_game_output_state() -> None:
    bridge = globals().get("INPUT_BRIDGE")
    broadcaster = getattr(bridge, "broadcast_game_output_state", None)
    if callable(broadcaster):
        try:
            broadcaster()
        except Exception:
            pass


HOTKEYS = GlobalHotkeys(OUTPUT, emergency_stop=emergency_stop_all)


def _note_voice_trigger(action: dict) -> None:
    command_id = str(action.get("command_id", "")).strip()
    # 说出口的那句话。通用口令没有编号，以前记成 "voice.M"——两句都按 M 的口令
    # 分不开，界面上也只能显示代号。
    phrase = str(action.get("phrase", "")).strip()
    if command_id:
        trigger = f"voice.{command_id}"
    elif phrase:
        trigger = f"voice.shared.{phrase}"
    else:
        trigger = "voice." + str(action.get("target", "")).strip()
    binding = KERNEL.control_bindings.get(trigger) if command_id else None
    if isinstance(binding, dict) and not binding.get("disabled") and isinstance(binding.get("action"), dict):
        noted = binding["action"]
    else:
        noted = {"type": str(action.get("type", "")), "target": action.get("target", ""),
                 "behavior": str(action.get("behavior", "tap"))}
    KERNEL.note_trigger(trigger, noted, label=phrase or None)


def voice_emergency_stop() -> dict:
    """口令急停。先停，再补记一笔：急停不经过 execute_voice_action（它要最快），
    不记的话动作测试页和手机头顶都看不见"刚才那句急停听到了"。"""
    result = emergency_stop_all()
    try:
        KERNEL.note_trigger("voice.system.emergency_stop",
                            {"type": "system", "target": "EMERGENCY_STOP", "behavior": "tap"},
                            label="紧急停止")
    except Exception:
        pass
    return result


def execute_voice_action(action: dict) -> dict:
    """Keep system voice commands at the local control-kernel boundary.

    A selected game Profile may override a stable voice command id.  The
    recognizer and command vocabulary remain unchanged; only the final action
    is selected at this boundary.
    """
    # 语音不走内核那条分发路，所以在这里补一笔"刚才触发了什么"。界面上的触发实况
    # 靠它才看得见语音——口令说完就完，轮询状态是抓不到的。
    _note_voice_trigger(action)
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
                    return OUTPUT.execute_voice_action(mapped)
            if command_id.startswith("game.profile_slot_"):
                return {"executed": False, "reason": "当前游戏未设置这条备用语音"}
        return OUTPUT.execute_voice_action(action)
    target = str(action.get("target", "")).strip().upper()
    # Output start/stop
    if target == "OUTPUT.START":
        result = OUTPUT.set_config(enabled=True)
        _broadcast_game_output_state()
        return {"executed": True, **result}
    if target == "OUTPUT.STOP":
        result = OUTPUT.set_config(enabled=False)
        _broadcast_game_output_state()
        return {"executed": True, **result}
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
    # 录自定义姿势。这个按钮天生该能用嘴按：人站在镜头前几米外摆姿势，够不着鼠标。
    if target in {"POSE.RECORD", "POSE.ADD_FRAME"}:
        delay = KERNEL.general_setting("pose_capture_delay_s", DEFAULT_POSE_DELAY_S)
        if target == "POSE.RECORD":
            return {"executed": True, "pose_capture": POSE_TIMER.arm(purpose="capture", delay_s=delay)}
        pose_id = _newest_pose_id()
        if not pose_id:
            return {"executed": False, "reason": "还没有录过动作，先说一次「录姿势」"}
        return {"executed": True,
                "pose_capture": POSE_TIMER.arm(purpose="frame", delay_s=delay, pose_id=pose_id)}
    if target == "POSE.CANCEL":
        return {"executed": True, "pose_capture": POSE_TIMER.cancel()}
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
    emergency_stop=voice_emergency_stop,
    clear_source=OUTPUT.clear_source,
)
VOICE.configure_profile_bindings(PROFILES.effective_profile().get("bindings", {}))
# main() 里装上。放在这里只是为了让收尾那段能无条件 close 它。
DISCOVERY: DiscoveryResponder | None = None


def _instance_id() -> str:
    """这台电脑的标识，随机生成一次后存下来。

    双网卡的电脑会从两个接口各回一份应答，手机拿这个把它们认成同一台——不然
    界面上会冒出两台电脑让人选，而它们其实是一台。
    """
    import uuid

    path = user_path("discovery_instance")
    try:
        saved = path.read_text(encoding="utf-8").strip()
        if len(saved) == 12 and all(c in "0123456789abcdef" for c in saved):
            return saved
    except OSError:
        pass
    made = uuid.uuid4().hex[:12]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(made, encoding="utf-8")
    except OSError:
        pass   # 存不下就每次换一个，去重退化但不影响连接
    return made


INPUT_BRIDGE = InputBridge(OUTPUT, KERNEL, voice=VOICE)
INPUT_BRIDGE.configure_scene_snapshot_handler(_scene_snapshot_from_phone)

def find_phone_web(root: Path) -> Path | None:
    """手机的网页包在哪：发布包里带着，开发时用隔壁仓库的构建产物。

    现在它在 app/ 里，也就是 root/phone_web 这一条，排在最前面。放这里是为了跟着
    电脑端自更新走：自更新换的就是整个 app/，放在外面的东西一辈子不会被换，网页
    包修好了也只能等下一次重装发布包。

    root.parent 那一条留着，是给按老布局装出去的那批人：他们的 phone_web 在 app/
    的同级。更新会把这个新 server.py 换进去，那一刻两个位置都可能有东西，app/ 里
    那份是新的，所以它排在前面。

    这两条少了任何一条，后果都是不报错的：manifest 返回 0 个文件，手机每次都以为
    自己已经是最新的，更新通道等于不存在。2.0 的包就是这样发出去的——server.py
    从顶层搬进 app/ 之后没人改这里。
    """
    candidates = [root / "phone_web", root.parent / "phone_web"]
    configured = os.environ.get("PHONE_WEB_DIR", "").strip().strip('"')
    if configured:
        candidates.insert(0, Path(configured))
    # 本机新手机工作树优先于旧 switch：两者同时存在时，不能把旧网页热更回手机。
    # 发布包里 phone_web 排在前面，不受开发目录影响。
    candidates.extend([
        root.parent / "MC-switch" / "mobile" / "dist",
        root.parent / "switch" / "mobile" / "dist",
    ])
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _phone_control_payload() -> dict:
    profile = PROFILES.effective_profile()
    scene = SCENE.status()
    return {
        "type": "control_config_v1",
        "version": VERSION,
        "game": {"id": profile.get("id"), "name": profile.get("name"), "appid": profile.get("appid")},
        "bindings": profile.get("bindings", {}),
        # 真正会生效的那份。区域有一层内置兜底：配置里没有 zone.headJump 时它照样按 A。
        # 手机只看 bindings 的话会写「未映射」，而游戏里明明有反应。
        "effective_bindings": KERNEL.effective_bindings(),
        # 绑定里的宏是按编号引用的，手机手上没有宏库就只能显示一串编号。带上名字和
        # 步数，圈上才写得出「三连击」。只在配置变化时推一次，不是实时数据。
        "macros": MACROS.status(),
        "zones": scene.get("zones", {}),
        "vertical_look": scene.get("vertical_look", {}),
        # The phone builds its own constrained grammar from this.  Sending it
        # keeps one list authoritative: a phrase added on the desktop is heard
        # by the phone microphone too, without shipping a new build.
        "voice_phrases": VOICE.grammar_phrases(),
        # 电脑能被连到的所有地址，有线在前。手机自己试，谁答应用谁——地址一变
        # 就连不上，是这个项目里最常见的一种"坏了"。
        "server_candidates": INPUT_BRIDGE.server_candidates(),
        # 手部模型是手机上的第二次推理，实测要花掉一成帧率，所以只有真的在用手
        # 控鼠标时才让它跑。手也一起告诉它：设备按这只手的手腕裁图，裁哪里是这
        # 边说了算的，就不存在把左右手认反的问题。
        "hand_tracking": _hand_tracking_request(),
    }


def _hand_tracking_request() -> dict:
    return KERNEL.hand_mouse_controller.tracking_request()

provider = getattr(INPUT_BRIDGE, "configure_control_config_provider", None)
if provider is not None:
    provider(_phone_control_payload)
# 用户自己录的姿势。内核不碰文件，所以 store 在这里建、装进去。
CUSTOM_POSES = CustomPoseStore(user_path("custom_poses"))
KERNEL.configure_custom_poses(CUSTOM_POSES)
if CUSTOM_POSES.last_error:
    print(CUSTOM_POSES.last_error)
# 用户自己建的键盘宏。全局一份，不跟游戏走——建一次，哪个游戏、哪个动作都能直接选。
MACROS = MacroStore(user_path("key_macros"))
KERNEL.configure_macros(MACROS)
if MACROS.last_error:
    print(MACROS.last_error)


def _capture_custom_pose(purpose: str, pose_id: str, name: str) -> dict:
    """倒计时到点时真正拍的那一步。计时本身在 motioncontrol.pose_capture 里。"""
    with PROFILE_UPDATE_LOCK:
        # 取一小段时间的中位数而不是单帧：单帧的关键点会抖，抖出来的模板会让之后
        # 每一次比对都偏一点。和按钮那条路用的是同一个取样。
        snapshot = KERNEL.stable_pose_snapshot(window_s=0.40, min_samples=3)
        if not snapshot:
            raise ValueError("还没有看到人。先让摄像头拍到你，再录姿势。")
        if purpose == "capture":
            entry = CUSTOM_POSES.capture(snapshot, name)
        else:
            entry = CUSTOM_POSES.append_frame(pose_id, snapshot)
        KERNEL.configure_custom_poses(CUSTOM_POSES)
    broadcaster = getattr(INPUT_BRIDGE, "broadcast_control_config", None)
    if broadcaster is not None:
        try:
            broadcaster(_phone_control_payload())
        except Exception:
            pass
    return _custom_pose_out(entry)


def _newest_pose_id() -> str:
    """语音说「再加一个姿势」时加到哪一条：最近动过的那条。

    没有别的合理答案——人刚录完一个动作，接着说"再加一个"，说的一定是它。
    """
    poses = CUSTOM_POSES.status()
    return str(poses[-1]["id"]) if poses else ""


POSE_TIMER = PoseCaptureTimer(_capture_custom_pose)

# 触发状态往手机推：内核放进队列，这条线程取出来发。
#
# 中间必须隔一个队列。内核那个回调是在**控制线程、而且在锁里**被调用的，往 socket
# 写是会阻塞的操作——一个网络变慢的手机就能把识别线程卡住，表现出来是掉帧，而没人
# 会把掉帧和"手机上那个显示"联系起来。
#
# 队列很浅而且满了就丢。这是显示用的状态，不是指令：丢掉一次的代价是手机上少闪一
# 下，而攒着不丢的代价是内存一直涨、并且显示越来越滞后。
_TRIGGER_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=8)


def _queue_trigger_state(payload: dict) -> None:
    """内核调的就是这个。必须是放下就走，不能阻塞。"""
    try:
        _TRIGGER_QUEUE.put_nowait(payload)
    except queue.Full:
        pass


def _name_trigger_state(payload: dict) -> dict:
    """补上人看得懂的名字。内核只给 id，名字表在 describe 里。"""
    out = dict(payload)
    for key in ("held", "fired"):
        out[key] = [
            {**item, "name": trigger_name(str(item.get("id", "")))}
            for item in payload.get(key, [])
        ]
    return out


def _trigger_pump() -> None:
    while True:
        payload = _TRIGGER_QUEUE.get()
        try:
            INPUT_BRIDGE.broadcast_trigger_state(_name_trigger_state(payload))
        except Exception:  # noqa: BLE001 - 推送失败不该让这条线程死掉
            pass


threading.Thread(target=_trigger_pump, name="trigger-pump", daemon=True).start()
KERNEL.configure_trigger_listener(_queue_trigger_state)

MODEL_ROOT: Path | None = None
MODEL_PATH: Path | None = None
# 电脑自己的识别器加载的就是这个目录，手机要的是同一份。找不到也不报错：这台
# 机器没配语音，手机那边会看到 available 为假，然后照实说，而不是装死。
VOICE_MODEL = ModelShare("vosk-model-small-cn-0.22", find_vosk_model(ROOT))
# 手机的网页包，随 PC 发布包一起分发。手机侧改动有一半只动这 200 KB，让它跟着
# 电脑走就不用为此发新 APK，也不用你的服务器出流量。
# 只有发布包里才有；从仓库直接跑时这里是空的，手机照旧用 APK 自带的那份。
PHONE_WEB = ModelShare("phone-web", find_phone_web(ROOT), skip=("models/", "wasm/"))
MOTION_CONFIG_FILE = user_path("motion_mappings")
DEFAULT_MOTIONS = [
    {"id": "march", "name": "原地踏步", "enabled": False, "type": "gamepad_axis", "target": "LS_UP"},
    {"id": "calf_back", "name": "小腿向后抬起", "enabled": False, "type": "gamepad", "target": "B"},
    {"id": "squat", "name": "下蹲", "enabled": False, "type": "gamepad", "target": "X"},
    {"id": "hands_up", "name": "双手举过头顶", "enabled": False, "type": "gamepad", "target": "Y"},
    {"id": "jumping_jack", "name": "开合跳", "enabled": False, "type": "gamepad", "target": "A"},
    {"id": "side_step_jack", "name": "侧步开合", "enabled": False, "type": "gamepad", "target": "B"},
    {"id": "cross_knee_elbow", "name": "提膝碰对侧肘", "enabled": False, "type": "gamepad", "target": "X"},
]



# --- cloud ------------------------------------------------------------------
#
# Everything here is optional and nothing local depends on it. If the cloud is
# unreachable, or was never configured, the controller works exactly as it does
# now -- these routes fail and no other code path notices.

CLOUD_ENDPOINT_FILE = user_path("cloud_endpoint")
# 必须和 cloud/deploy/bootstrap.sh 的 SITE_ORIGIN 一致。这两处曾经不一致过：
# 子域名定下来之前这里先写了 config.，定的却是 motioncontrol.，于是桌面端一直
# 报 getaddrinfo failed——域名根本没解析。tests/test_cloud_client.py 现在会核对。
DEFAULT_CLOUD_ENDPOINT = "https://motioncontrol.guiwu-aware.icu"


def cloud_endpoint() -> str:
    """The cloud this installation talks to, from a one-line text file."""
    try:
        configured = CLOUD_ENDPOINT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        configured = ""
    return configured or DEFAULT_CLOUD_ENDPOINT


def set_cloud_endpoint(url: str) -> str:
    # Construct the client first: it rejects anything that is not an http(s)
    # URL, so an unusable address is never written to disk.
    client = CloudClient(url)
    CLOUD_ENDPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLOUD_ENDPOINT_FILE.write_text(client.base, encoding="utf-8")
    return client.base


def _install_profile_selection(document: dict, game_id: str | None) -> dict:
    """Apply a downloaded game-mapping document through the normal code path.

    Nothing here writes a config file. It calls the same ``select`` and
    ``set_overrides`` the local UI calls, so the base profile is resolved, the
    merge is validated, and the motion-conflict rule runs -- all of which a
    direct write would skip.

    With *game_id*, only that one game's overrides are taken and the rest of the
    user's games are left alone. That is what installing a shared config
    actually means: someone published their Uncharted bindings, not their whole
    library. Without it the entire document is adopted.
    """
    by_profile = document.get("overrides_by_profile", {})
    if game_id:
        if game_id not in by_profile:
            raise ValueError(f"这份配置里没有 {game_id} 的映射")
        wanted = {game_id: by_profile[game_id]}
        final_selection = game_id
    else:
        wanted = by_profile
        final_selection = str(document.get("selected_id", "")) or None

    applied = []
    for profile_id, overrides in wanted.items():
        # set_overrides only accepts the currently selected profile, so each
        # game is selected before its overrides are written. Both steps
        # validate; neither touches the file directly.
        PROFILES.select(profile_id)
        PROFILES.set_overrides(overrides, profile_id=profile_id)
        applied.append(profile_id)

    if final_selection:
        PROFILES.select(final_selection)
    profile = PROFILES.effective_profile()
    with VOICE._lock:
        VOICE._release_locked(VOICE.source_id)
        KERNEL.configure_bindings(profile.get("bindings", {}))
        VOICE.configure_profile_bindings(profile.get("bindings", {}))
    return {"applied_games": applied, "profile": profile}


def _install_game_bundle(document: dict) -> dict:
    """装一份「游戏方案」：那个游戏的按键绑定，加上身体动作。

    走的是和分开那两种完全一样的入口（PROFILES.set_overrides / save_motion_config），
    所以校验、冲突检查、写盘都是现成的一份，不存在"包里的走了另一条路"。
    """
    global MOTION_CONFIG
    game_id = str(document.get("game_id", "")).strip()
    if not game_id:
        raise ValueError("这份方案没说是哪个游戏")
    PROFILES.select(game_id)
    PROFILES.set_overrides(document.get("overrides", {}), profile_id=game_id)
    MOTION_CONFIG = save_motion_config(document.get("motions", []))
    KERNEL.configure_motions(MOTION_CONFIG)
    OUTPUT.set_holds([], source_group="motions")
    profile = PROFILES.effective_profile()
    with VOICE._lock:
        VOICE._release_locked(VOICE.source_id)
        KERNEL.configure_bindings(profile.get("bindings", {}))
    return {"applied_games": [game_id], "profile": profile, "motions": MOTION_CONFIG}


def _install_cloud_config(remote, game_id: str | None) -> dict:
    """Back up, stop output, apply. In that order, and under the profile lock."""
    with PROFILE_UPDATE_LOCK:
        backup = backup_user_data(user_data_root())
        # Applying a config rebinds every control at once. Releasing whatever is
        # currently held first means a key that was down under the old mapping
        # cannot stay down forever under the new one.
        OUTPUT.emergency_stop()
        _broadcast_game_output_state()

        if remote.doc_type == "profile_selection":
            result = _install_profile_selection(remote.document, game_id)
        elif remote.doc_type == "motion_mappings":
            global MOTION_CONFIG
            MOTION_CONFIG = save_motion_config(remote.document.get("motions", []))
            KERNEL.configure_motions(MOTION_CONFIG)
            OUTPUT.set_holds([], source_group="motions")
            result = {"motions": MOTION_CONFIG}
        elif remote.doc_type == "game_bundle":
            # 一份包里就是一个游戏的全部：按键绑定加身体动作。两样一起装，
            # 不用让人先装一个再找另一个。
            result = _install_game_bundle(remote.document)
        elif remote.doc_type == "voice_mappings":
            # 只装口令映射。唤醒词和急停口令是装的人自己的，旧文档里带了
            # 也不能拿——拿了就是把他的唤醒词换成发布者的，他只会觉得软件坏了。
            result = {"voice": VOICE.configure(remote.document.get("mappings", []))}
        else:
            raise ValueError(f"不支持的配置类型：{remote.doc_type}")

        broadcaster = getattr(INPUT_BRIDGE, "broadcast_control_config", None)
        if broadcaster is not None:
            broadcaster(_phone_control_payload())

    return {
        "ok": True,
        "installed": {
            "title": remote.title,
            "owner": remote.owner_name,
            "doc_type": remote.doc_type,
            "revision_no": remote.revision_no,
            "sha256": remote.sha256,
        },
        "backup": str(backup) if backup else None,
        **result,
    }

def _custom_pose_out(entry: dict) -> dict:
    """给界面的单个姿势。模板本身不发——那是十几个浮点数，界面用不上。"""
    return next(item for item in CUSTOM_POSES.status() if item["id"] == entry["id"])


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
            "synonyms": list(item.get("synonyms", []) or []),
            "kind": kind,
            "system_fixed": kind == "system",
            "editable_phrase": str(item.get("id", "")).startswith("game.profile_slot_"),
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
    validate_motion_config(out)
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
    # 原子写入：异常退出或掉电时不会留下半个动作配置文件。
    temp = MOTION_CONFIG_FILE.with_suffix(MOTION_CONFIG_FILE.suffix + ".tmp")
    temp.write_text(json.dumps({"motions": motions}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, MOTION_CONFIG_FILE)
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


class _BaseHandler(SimpleHTTPRequestHandler):
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

    def end_headers(self):
        # Assets under WEB_DIR are served by the base handler, which sends only
        # Last-Modified.  Browsers then apply heuristic caching and can keep
        # serving a stale app.js/app.css after an edit, so a change appears not
        # to have taken effect until a forced reload.  Everything here comes off
        # the local disk, so there is nothing to gain by caching it.
        if not any(line.lower().startswith(b"cache-control") for line in (self._headers_buffer or [])):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

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


    def _try_model_route(self, route: str) -> bool:
        """Serve the two routes both planes expose.  Returns True if handled.

        These are read-only and carry no user data, so exposing them on the LAN
        plane costs nothing.  README says the phone runs MediaPipe locally and
        very likely ships its own model, but the Android app lives in another
        repo -- keeping these reachable means device onboarding cannot break if
        it turns out to fetch one over HTTP.
        """
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
            return True
        if route == "/api/model/mp-full":
            if MODEL_PATH is None:
                self.send_error(404, "MediaPipe Full model unavailable")
            else:
                self._serve_file(MODEL_PATH)
            return True
        # 中文语音模型。手机以前自己背一份 41.5 MB 的副本，占了安装包的一半，
        # 而那些文件跟这台电脑上的逐字节一样——手机本来就要连着一台电脑，让它
        # 从电脑取就行，谁的流量都不用花。
        if route == "/api/model/voice-cn":
            self._send_json({"version": VERSION, **VOICE_MODEL.manifest()})
            return True
        if route == "/api/model/voice-cn/file":
            wanted = parse_qs(urlparse(self.path).query).get("path", [""])[0]
            path = VOICE_MODEL.resolve(wanted)
            if path is None:
                self.send_error(404, "voice model file unavailable")
            else:
                self._serve_file(path)
            return True
        if route == "/api/bundle/phone-web":
            self._send_json({"version": VERSION, **PHONE_WEB.manifest()})
            return True
        if route == "/api/bundle/phone-web/file":
            wanted = parse_qs(urlparse(self.path).query).get("path", [""])[0]
            path = PHONE_WEB.resolve(wanted)
            if path is None:
                self.send_error(404, "phone web bundle file unavailable")
            else:
                self._serve_file(path)
            return True
        return False


class AdminHandler(_BaseHandler):
    """The loopback plane: the local web UI and every /api/* route.

    Bound to 127.0.0.1, so the LAN cannot reach any of this regardless of what
    the individual route handlers check.  The _is_loopback() guards below stay
    as defence in depth -- the listening address is the first boundary, this
    class is the second, and those checks are the third.
    """

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
        if route == "/api/app-update":
            # 只读。界面拿它显示"已经下好，下次启动生效"，好让人知道重启一次
            # 是有意义的——否则更新会安静地躺在那里，直到某天碰巧重启。
            self._send_json({"version": VERSION, **UPDATE_STATE,
                             })
            return
        if route == "/api/output/actions":
            self._send_json({"version": VERSION, "actions": action_catalog()})
            return
        if self._try_model_route(route):
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
        if route == "/api/motion/config":
            self._send_json({"version": VERSION, "motions": MOTION_CONFIG})
            return
        if route == "/api/motion/conflicts":
            self._send_json({"version": VERSION, "groups": motion_conflict_payload()})
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
            # 录姿势的倒计时挂在这里，因为这是 250ms 轮询的那一份——人站在几米外
            # 盯着屏幕等数字，一秒刷一次都嫌慢。
            self._send_json({"version": VERSION, "pose_capture": POSE_TIMER.status(),
                             **RUNTIME.status()})
            return
        if route == "/api/action-chain/config":
            self._send_json({"version": VERSION, **KERNEL.status()["action_chain"]})
            return
        if route == "/api/performance":
            data = performance_snapshot()
            data["version"] = VERSION
            self._send_json(data)
            return
        if route == "/api/camera/config":
            self._send_json(RUNTIME.camera_backend_config())
            return
        if route == "/api/camera/devices":
            # 一个一个去开，几秒钟起步，所以它是"点了才扫"而不是随状态轮询。
            try:
                self._send_json({"ok": True, **RUNTIME.list_cameras()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
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
        if route == "/api/macros":
            self._send_json({
                "version": VERSION,
                "macros": MACROS.detail(),
                "limits": {
                    "max": macro_schema.MAX_MACROS,
                    "steps": macro_schema.MAX_STEPS,
                    "expanded_steps": macro_schema.MAX_EXPANDED_STEPS,
                    "depth": macro_schema.MAX_DEPTH,
                    "total_ms": macro_schema.MAX_TOTAL_MS,
                    "hold_ms": [macro_schema.MIN_HOLD_MS, macro_schema.MAX_HOLD_MS],
                    "gap_ms": [macro_schema.MIN_GAP_MS, macro_schema.MAX_GAP_MS],
                    "name_chars": macro_schema.MAX_NAME_LEN,
                },
                "running": [item["macro"] for item in KERNEL.output.status().get("macros_running", [])],
                "error": MACROS.last_error,
            })
            return
        if route == "/api/pose/library":
            # 动作库：名字、怎么做、火柴人示范、会扫过哪些圈。固定数据，界面只在
            # 打开时读一次。
            self._send_json({"version": VERSION, "library": pose_library.library_payload()})
            return
        if route == "/api/pose/custom":
            self._send_json({
                "version": VERSION,
                "poses": CUSTOM_POSES.status(),
                # 实时相似度：界面靠它给出"现在像不像"的即时反馈，没有这个，
                # 用户调阈值只能靠猜。
                "scores": dict(KERNEL.custom_pose_scores),
                "active": sorted(KERNEL.pose_active),
                # 上次选的准备时间。不带回去的话，界面每次打开都回到默认值，而下一
                # 次点按钮又会把这个默认值存回来——记住等于没记。
                "delay_s": KERNEL.general_setting("pose_capture_delay_s", DEFAULT_POSE_DELAY_S),
                "capture": POSE_TIMER.status(),
                "limits": {"max": 24, "name_chars": 20},
            })
            return
        if route == "/api/pose/record":
            self._send_json({"version": VERSION, "recording": KERNEL.pose_recorder.status()})
            return
        if route == "/api/hand-mouse/config":
            self._send_json({"version": VERSION,
                             "hand_mouse": KERNEL.hand_mouse_controller.status()})
            return
        if route == "/api/cloud/status":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "cloud is loopback-only"}, 403)
                return
            endpoint = cloud_endpoint()
            payload = {"version": VERSION, "endpoint": endpoint, "reachable": False}
            try:
                payload["health"] = CloudClient(endpoint).health()
                payload["reachable"] = True
            except CloudError as exc:
                # Not reachable is a normal state, not a failure of this
                # request: the cloud is optional and the UI says so.
                payload["error"] = str(exc)
            self._send_json(payload)
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
        if route.startswith("/api/macros/"):
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "macros are loopback-only"}, 403)
                return
            try:
                with PROFILE_UPDATE_LOCK:
                    macro = None
                    if route == "/api/macros/create":
                        macro = MACROS.create(name=body.get("name", ""),
                                              steps=body.get("steps"),
                                              repeat=bool(body.get("repeat", False)))
                    elif route == "/api/macros/update":
                        macro = MACROS.update(str(body.get("id", "")),
                                              name=body.get("name"),
                                              steps=body.get("steps"),
                                              repeat=body.get("repeat"))
                    elif route == "/api/macros/remove":
                        if not MACROS.remove(str(body.get("id", ""))):
                            self._send_json({"ok": False, "error": "找不到这条宏"}, 404)
                            return
                    else:
                        self._send_json({"ok": False, "error": "not found"}, 404)
                        return
                    # 「跑一遍还是循环」是宏自己的属性，改了它，所有指向它的绑定的
                    # 方式都要跟着重算——这一步在内核里，所以必须重新装一次宏库。
                    KERNEL.configure_macros(MACROS)
                broadcaster = getattr(INPUT_BRIDGE, "broadcast_control_config", None)
                if broadcaster is not None:
                    broadcaster(_phone_control_payload())
                self._send_json({"ok": True, "macro": macro, "macros": MACROS.detail()})
            except MacroError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        if route.startswith("/api/pose/custom/"):
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "custom poses are loopback-only"}, 403)
                return
            try:
                if route == "/api/pose/custom/schedule":
                    # 按钮和口令走同一条路：都只是把倒计时设上，到点了服务端自己拍。
                    # 准备时间顺手记住——语音触发时没人能替它去读界面上那个下拉框。
                    delay = body.get("delay_s", DEFAULT_POSE_DELAY_S)
                    KERNEL.remember_general_setting("pose_capture_delay_s", float(delay))
                    purpose = str(body.get("purpose", "capture"))
                    self._send_json({"ok": True, "pose_capture": POSE_TIMER.arm(
                        purpose=purpose, delay_s=delay,
                        name=str(body.get("name", "")), pose_id=str(body.get("id", "")))})
                    return
                if route == "/api/pose/custom/cancel":
                    self._send_json({"ok": True, "pose_capture": POSE_TIMER.cancel()})
                    return
                with PROFILE_UPDATE_LOCK:
                    if route == "/api/pose/custom/capture":
                        # 取一小段时间的中位数而不是单帧：单帧的关键点会抖，
                        # 抖出来的模板会让之后每一次比对都偏一点。
                        snapshot = KERNEL.stable_pose_snapshot(window_s=0.40, min_samples=3)
                        if not snapshot:
                            self._send_json({"ok": False,
                                             "error": "还没有看到人。先让摄像头拍到你，再录姿势。"}, 400)
                            return
                        entry = CUSTOM_POSES.capture(snapshot, str(body.get("name", "")))
                        KERNEL.configure_custom_poses(CUSTOM_POSES)
                        self._send_json({"ok": True, "pose": _custom_pose_out(entry),
                                         "poses": CUSTOM_POSES.status()})
                    elif route == "/api/pose/custom/frame":
                        # 给已有动作再加一帧，把它变成（或延长）连贯动作。
                        snapshot = KERNEL.stable_pose_snapshot(window_s=0.40, min_samples=3)
                        if not snapshot:
                            self._send_json({"ok": False,
                                             "error": "还没有看到人。先让摄像头拍到你，再加姿势。"}, 400)
                            return
                        entry = CUSTOM_POSES.append_frame(str(body.get("id", "")), snapshot)
                        KERNEL.configure_custom_poses(CUSTOM_POSES)
                        self._send_json({"ok": True, "pose": _custom_pose_out(entry),
                                         "poses": CUSTOM_POSES.status()})
                    elif route == "/api/pose/custom/frame/remove":
                        entry = CUSTOM_POSES.remove_frame(str(body.get("id", "")),
                                                          int(body.get("index", -1)))
                        KERNEL.configure_custom_poses(CUSTOM_POSES)
                        self._send_json({"ok": True, "pose": _custom_pose_out(entry),
                                         "poses": CUSTOM_POSES.status()})
                    elif route == "/api/pose/custom/update":
                        changes = {k: body[k] for k in
                                   ("name", "threshold", "dwell_frames",
                                    "step_window_s", "enabled") if k in body}
                        entry = CUSTOM_POSES.update(str(body.get("id", "")), **changes)
                        KERNEL.configure_custom_poses(CUSTOM_POSES)
                        self._send_json({"ok": True, "pose": _custom_pose_out(entry),
                                         "poses": CUSTOM_POSES.status()})
                    elif route == "/api/pose/custom/remove":
                        removed = CUSTOM_POSES.remove(str(body.get("id", "")))
                        KERNEL.configure_custom_poses(CUSTOM_POSES)
                        self._send_json({"ok": True, "removed": removed,
                                         "poses": CUSTOM_POSES.status()})
                    else:
                        self._send_json({"ok": False, "error": "not found"}, 404)
                        return
                broadcaster = getattr(INPUT_BRIDGE, "broadcast_control_config", None)
                if broadcaster is not None:
                    broadcaster(_phone_control_payload())
            except CustomPoseError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        if route.startswith("/api/cloud/"):
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "cloud is loopback-only"}, 403)
                return
            try:
                if route == "/api/cloud/endpoint":
                    self._send_json({"ok": True,
                                     "endpoint": set_cloud_endpoint(str(body.get("url", "")))})
                elif route == "/api/cloud/browse":
                    client = CloudClient(cloud_endpoint())
                    self._send_json({"ok": True, "profiles": client.browse(
                        doc_type=str(body.get("doc_type", "")),
                        game_id=str(body.get("game_id", "")))})
                elif route == "/api/cloud/preview":
                    remote = CloudClient(cloud_endpoint()).fetch(
                        str(body.get("profile_id", "")), str(body.get("version_id", "")))
                    # Deliberately does not apply anything: the UI shows what
                    # would change and the user confirms before it happens.
                    self._send_json({"ok": True, "preview": {
                        "title": remote.title, "owner": remote.owner_name,
                        "doc_type": remote.doc_type, "revision_no": remote.revision_no,
                        "sha256": remote.sha256, "game_id": remote.game_id,
                        "games": sorted(remote.document.get("overrides_by_profile", {}))
                                 if remote.doc_type == "profile_selection"
                                 else ([remote.document.get("game_id", "")]
                                       if remote.doc_type == "game_bundle" else []),
                    }})
                elif route == "/api/cloud/install":
                    remote = CloudClient(cloud_endpoint()).fetch(
                        str(body.get("profile_id", "")), str(body.get("version_id", "")))
                    self._send_json(_install_cloud_config(
                        remote, str(body.get("game_id", "")) or None))
                else:
                    self._send_json({"ok": False, "error": "not found"}, 404)
            except CloudError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 502)
            except ProfileSelectionChanged as exc:
                self._send_json({"ok": False, "error": str(exc)}, 409)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
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
        if route in {"/api/game-profiles/custom/add",
                     "/api/game-profiles/custom/remove",
                     "/api/game-profiles/custom/rename"}:
            # 和 select / overrides 同样只接受本机：这些都在写用户数据，局域网上
            # 的手机没有理由能改它。
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "custom games are loopback-only"}, 403)
                return
            try:
                with PROFILE_UPDATE_LOCK:
                    if route.endswith("/add"):
                        game = PROFILES.add_custom_game(
                            str(body.get("name", "")),
                            base=str(body.get("base") or "generic-xbox"),
                            appid=body.get("appid"))
                        self._send_json({"ok": True, "game": game,
                                         "catalog": PROFILES.list_games()})
                        return
                    if route.endswith("/rename"):
                        game = PROFILES.rename_custom_game(
                            str(body.get("id", "")), str(body.get("name", "")))
                        self._send_json({"ok": True, "game": game,
                                         "catalog": PROFILES.list_games()})
                        return
                    # 删除可能把当前选中的游戏删掉，那会换一份绑定，所以要和
                    # select 一样把新的推给内核和手机，否则玩家手上还是旧映射。
                    profile = PROFILES.remove_custom_game(str(body.get("id", "")))
                    with VOICE._lock:
                        VOICE._release_locked(VOICE.source_id)
                        KERNEL.configure_bindings(profile.get("bindings", {}))
                        VOICE.configure_profile_bindings(profile.get("bindings", {}))
                    broadcaster = getattr(INPUT_BRIDGE, "broadcast_control_config", None)
                    if broadcaster is not None:
                        broadcaster(_phone_control_payload())
                self._send_json({"ok": True, "profile": profile,
                                 "catalog": PROFILES.list_games()})
            except KeyError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 404)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        if route in {"/api/game-profiles/select", "/api/game-profiles/overrides"}:
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "game profile changes are loopback-only"}, 403)
                return
            try:
                with PROFILE_UPDATE_LOCK:
                    if route.endswith("/select"):
                        profile = PROFILES.select(str(body.get("id", "")))
                    else:
                        profile = PROFILES.set_overrides(body.get("overrides", {}), profile_id=body.get("profile_id"),
                                                         check=VOICE.check_profile_phrases)
                    with VOICE._lock:
                        VOICE._release_locked(VOICE.source_id)
                        KERNEL.configure_bindings(profile.get("bindings", {}))
                        VOICE.configure_profile_bindings(profile.get("bindings", {}))
                    broadcaster = getattr(INPUT_BRIDGE, "broadcast_control_config", None)
                    if broadcaster is not None:
                        broadcaster(_phone_control_payload())
                self._send_json({"ok": True, "profile": profile})
            except ProfileSelectionChanged as exc:
                self._send_json({"ok": False, "error": str(exc)}, 409)
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
                # Audio remains usable if the body model or camera cannot start.
                # Each input reports its own readiness; a body failure is still an error.
                if enabled and source == "computer":
                    try:
                        voice_data = VOICE.start_local_microphone()
                    except Exception as exc:
                        voice_data = {**VOICE.status(), "last_error": str(exc)}
                else:
                    voice_data = VOICE.status()
                if enabled:
                    data = RUNTIME.set_source(source, start_computer=True)
                else:
                    data = RUNTIME.stop_body()
                INPUT_BRIDGE.set_body_mode(source if enabled else "computer")
                self._send_json({"ok": True, **data, "voice": voice_data})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **RUNTIME.status(), "voice": VOICE.status()}, 400)
            return
        if route == "/api/camera/config":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "camera config is loopback-only"}, 403)
                return
            try:
                if "index" in body or "camera_index" in body:
                    data = RUNTIME.configure_camera_index(body.get("index", body.get("camera_index")))
                else:
                    data = RUNTIME.configure_camera_backend(body.get("backend", body.get("preference", "auto")))
                self._send_json({"ok": True, **data})
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
        # 量身定区域：开始、跳过这一项、不量了、恢复默认大小。和校准一样只给本机。
        if route in ("/api/zones/fit/start", "/api/zones/fit/skip", "/api/zones/fit/cancel", "/api/zones/fit/reset"):
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "zone fit is loopback-only"}, 403)
                return
            action = route.rsplit("/", 1)[1]
            try:
                if action == "start":
                    KERNEL.start_zone_fit(body=body.get("body", True) is not False)
                elif action == "skip":
                    KERNEL.skip_zone_fit_phase()
                elif action == "cancel":
                    KERNEL.cancel_zone_fit()
                else:
                    KERNEL.reset_zone_fit()
                self._send_json({"ok": True, **RUNTIME.status()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), **RUNTIME.status()}, 400)
            return
        if route == "/api/pose/record":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "pose recording is loopback-only"}, 403)
                return
            try:
                if body.get("cancel"):
                    status = KERNEL.pose_recorder.cancel()
                else:
                    status = KERNEL.pose_recorder.start(
                        delay_s=body.get("delay_s"), duration_s=body.get("duration_s"))
                self._send_json({"ok": True, "recording": status})
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        if route == "/api/hand-mouse/config":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "hand mouse config is loopback-only"}, 403)
                return
            try:
                status = KERNEL.configure_hand_mouse(body)
                # 开关和左右手都写在这份配置里，改完必须马上推给手机，否则它要么
                # 白跑一次推理，要么该跑的时候没跑。
                broadcaster = getattr(INPUT_BRIDGE, "broadcast_control_config", None)
                if broadcaster is not None:
                    broadcaster(_phone_control_payload())
                self._send_json({"ok": True, "hand_mouse": status})
            except (ValueError, TypeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
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
        if route == "/api/action-chain/config":
            if not self._is_loopback():
                self._send_json({"ok": False, "error": "action chain config is loopback-only"}, 403)
                return
            try:
                data = KERNEL.configure_action_chain(body.get("action_chain", body))
                self._send_json({"ok": True, "action_chain": data["action_chain"]})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc), "action_chain": KERNEL.status()["action_chain"]}, 400)
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
                    xinput_motion_left_enabled=body.get("xinput_motion_left_enabled"),
                    physical_xinput_user=(body.get("physical_xinput_user") if "physical_xinput_user" in body else _UNSET),
                )
                _broadcast_game_output_state()
            elif route == "/api/output/xinput":
                data = OUTPUT.configure_xinput_merge(
                    enabled=body.get("enabled") if "enabled" in body else None,
                    motion_left_enabled=body.get("motion_left_enabled"),
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
            _broadcast_game_output_state()
            self._send_json({"ok": False, "error": str(exc), **OUTPUT.status()}, 400)


class DeviceHandler(_BaseHandler):
    """The LAN plane: the phone's WebSocket, and deliberately almost nothing else.

    Routing here is a whitelist, not a chain of guards.  Adding a route to the
    admin plane later cannot accidentally expose it to the LAN, because this
    class does not inherit that chain -- both planes share only _BaseHandler's
    plumbing.  That is the point of the split: reachability becomes a property
    of the listening socket instead of something every new handler has to
    remember to check.

    Note what this does *not* fix.  Binding the account and config APIs to
    loopback stops a LAN device from reading them, but /ws/input itself
    accepts any device on the same network: handle_sensor() feeds
    output.set_sensor_state() directly, so a forged sensor_frame is real
    gamepad input.  That is a deliberate trade -- device pairing was removed
    because nobody used it.
    """

    def do_GET(self):
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if route == "/ws/input":
            INPUT_BRIDGE.serve_websocket(self, parsed.query)
            return
        if self._try_model_route(route):
            return
        self.send_error(404)

    def do_HEAD(self):
        self.send_error(404)

    def do_POST(self):
        self.send_error(404)


def _enable_default_xinput_merge() -> None:
    """Merge the physical pad on startup when exactly one is plugged in.

    The merge needs an explicit slot and promotes the output to gamepad mode,
    so it stays off when nothing is connected or when the choice is ambiguous;
    the settings page remains the way to change it afterwards.
    """
    try:
        users = OUTPUT.status().get("xinput_connected_users") or []
        if len(users) != 1:
            return
        # Body motion must drive the left stick too, or the merge silently drops
        # the stick half of a mixed combo and leaves only its buttons.
        OUTPUT.configure_xinput_merge(enabled=True, user=int(users[0]), motion_left_enabled=True)
        print(f"物理手柄合流已默认开启：手柄 {int(users[0]) + 1}（体感可驱动左摇杆）")
    except Exception as exc:  # A missing pad must never stop the service.
        print("物理手柄合流未开启：", exc)


def _boot_ok_and_check() -> None:
    """服务真的起来了，然后在后台看一眼有没有新版本。

    查更新放后台线程：服务器不通、或者慢，都不该让启动卡在那里。任何失败都只是
    "这次没更新"。
    """
    try:
        from motioncontrol import app_update
    except Exception:  # noqa: BLE001
        return
    try:
        app_update.boot_ok(_APP_DIR)
    except Exception:  # noqa: BLE001
        pass

    def look() -> None:
        try:
            result = app_update.check_and_stage(_APP_DIR)
        except Exception:  # noqa: BLE001
            return
        global UPDATE_STATE
        UPDATE_STATE = result
        if result.get("state") == "ready":
            print("有新版本已经下好，下次启动生效。")

    threading.Thread(target=look, name="motion-app-update", daemon=True).start()


def main():
    global MODEL_ROOT, MODEL_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0", help="监听地址；默认允许局域网手机连接")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--admin-port", type=int, default=8766,
                    help="本机管理面端口；只监听 127.0.0.1，局域网无法访问")
    ap.add_argument("--model-root", default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    MODEL_ROOT = choose_model_root(args.model_root)
    MODEL_PATH = resolve_full_model(MODEL_ROOT)
    RUNTIME.configure_model(MODEL_PATH)
    INPUT_BRIDGE.configure_endpoint(args.host, args.port)
    _enable_default_xinput_merge()
    print(f"MotionControl 2.0 · body zones + motions + voice · v{VERSION}")
    print("Model root:", MODEL_ROOT or "NOT FOUND")
    print("MediaPipe Full:", MODEL_PATH or "NOT FOUND")

    def _listen(host: str, port: int, handler, label: str) -> ThreadingHTTPServer:
        try:
            return ThreadingHTTPServer((host, port), handler)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in {98, 10048}:
                print(
                    f"启动失败：{label}端口 {port} 已被占用，可能已有 MotionControl 实例在运行。"
                    f" 请关闭旧实例或改用 --port/--admin-port；当前进程不会结束其他进程。",
                    file=sys.stderr,
                    flush=True,
                )
                raise SystemExit(2) from exc
            raise

    # Two planes, two trust levels.  8765 faces the LAN because the phone has to
    # reach it; 8766 never leaves this machine, so the browser UI and every
    # /api/* route are unreachable from the network by construction.  The
    # browser loads from 8766 and calls 8766, so both stay same-origin and no
    # CORS is involved anywhere.
    device_server = _listen(args.host, args.port, DeviceHandler, "设备接入面")
    admin_server = _listen("127.0.0.1", args.admin_port, AdminHandler, "本机管理面")

    # 第三个面，UDP，同一个端口号。手机往自己所在链路的广播地址喊一声，这里单播
    # 回一份地址表——不用它的话手机只能挨个敲网段，而共享网络的网段会整个变。
    # 和上面两个不同：绑不上只记一条原因继续跑。发现是让连接省事，不是产品本身。
    global DISCOVERY
    DISCOVERY = DiscoveryResponder(
        args.host, args.port,
        candidates=INPUT_BRIDGE.server_candidates,
        name=socket.gethostname(), version=VERSION, instance=_instance_id())
    if not DISCOVERY.start():
        print(f"手机自动发现未启用：{DISCOVERY.last_error}；手机仍可用地址连接。")

    HOTKEYS.start()
    url = f"http://127.0.0.1:{args.admin_port}/"
    print("Open:", url)
    print(f"手机接入（仅 /ws/input）：{args.host}:{args.port}")
    perf_stop = threading.Event()
    perf_thread = threading.Thread(target=performance_logger, args=(perf_stop,), name="motion-performance-log", daemon=True)
    perf_thread.start()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    device_thread = threading.Thread(
        target=device_server.serve_forever, name="motion-device-plane", daemon=True)
    device_thread.start()

    # 两个监听都起来了，这一份就算站住了：清掉启动记号，不然下次启动会以为上次
    # 崩了然后把包退回去。也顺手去查一次有没有新版本，下到暂存目录等下次启动。
    _boot_ok_and_check()

    try:
        admin_server.serve_forever()
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
        device_server.shutdown()
        device_thread.join(timeout=2.0)
        device_server.server_close()
        admin_server.server_close()
        if DISCOVERY is not None:
            DISCOVERY.close()


if __name__ == "__main__":
    main()
