from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import platform
import subprocess
import threading
import time
from pathlib import Path


_UNSET = object()


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class MouseOutput:
    """Windows relative mouse, mouse-button and wheel output using SendInput."""

    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_XDOWN = 0x0080
    MOUSEEVENTF_XUP = 0x0100
    MOUSEEVENTF_WHEEL = 0x0800
    INPUT_MOUSE = 0
    WHEEL_DELTA = 120
    XBUTTON1 = 0x0001
    XBUTTON2 = 0x0002

    def __init__(self) -> None:
        self.available = os.name == "nt"
        self.last_error: str | None = None
        self.pressed: set[str] = set()

    def _send(self, flags: int, *, dx: int = 0, dy: int = 0, data: int = 0) -> bool:
        if not self.available:
            raise RuntimeError("鼠标输出仅支持 Windows")
        event = _INPUT()
        event.type = self.INPUT_MOUSE
        event.mi = _MOUSEINPUT(int(dx), int(dy), int(data), int(flags), 0, None)
        sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
        if sent != 1:
            self.last_error = f"SendInput failed: {ctypes.get_last_error()}"
            raise RuntimeError(self.last_error)
        self.last_error = None
        return True

    def move(self, dx: int, dy: int = 0) -> bool:
        if dx == 0 and dy == 0:
            return False
        try:
            return self._send(self.MOUSEEVENTF_MOVE, dx=dx, dy=dy)
        except Exception as exc:
            self.last_error = str(exc)
            return False

    @staticmethod
    def normalize_button(button: str) -> str:
        aliases = {"LEFT_BUTTON": "LEFT", "RIGHT_BUTTON": "RIGHT", "MIDDLE_BUTTON": "MIDDLE", "MOUSE4": "X1", "MOUSE5": "X2"}
        value = str(button).strip().upper()
        return aliases.get(value, value)

    def set_button(self, button: str, pressed: bool) -> None:
        button = self.normalize_button(button)
        if button not in {"LEFT", "RIGHT", "MIDDLE", "X1", "X2"}:
            raise ValueError(f"不支持的鼠标按键：{button}")
        if pressed == (button in self.pressed):
            return
        if button == "LEFT":
            flags, data = (self.MOUSEEVENTF_LEFTDOWN if pressed else self.MOUSEEVENTF_LEFTUP), 0
        elif button == "RIGHT":
            flags, data = (self.MOUSEEVENTF_RIGHTDOWN if pressed else self.MOUSEEVENTF_RIGHTUP), 0
        elif button == "MIDDLE":
            flags, data = (self.MOUSEEVENTF_MIDDLEDOWN if pressed else self.MOUSEEVENTF_MIDDLEUP), 0
        else:
            flags = self.MOUSEEVENTF_XDOWN if pressed else self.MOUSEEVENTF_XUP
            data = self.XBUTTON1 if button == "X1" else self.XBUTTON2
        self._send(flags, data=data)
        if pressed:
            self.pressed.add(button)
        else:
            self.pressed.discard(button)

    def wheel(self, direction: str, notches: int = 1) -> None:
        direction = str(direction).strip().upper()
        if direction not in {"SCROLL_UP", "SCROLL_DOWN"}:
            raise ValueError(f"不支持的滚轮方向：{direction}")
        amount = self.WHEEL_DELTA * max(1, min(10, int(notches)))
        if direction == "SCROLL_DOWN":
            amount = -amount
        # mouseData is an unsigned DWORD in the INPUT struct; preserve the signed 32-bit wheel delta.
        self._send(self.MOUSEEVENTF_WHEEL, data=ctypes.c_ulong(amount & 0xFFFFFFFF).value)

    def release_all(self) -> None:
        for button in tuple(self.pressed):
            try:
                self.set_button(button, False)
            except Exception:
                self.pressed.discard(button)


KEY_CODES = {
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{str(code): ord(str(code)) for code in range(10)},
    "SPACE": 0x20, "ENTER": 0x0D, "ESC": 0x1B, "TAB": 0x09,
    "SHIFT": 0x10, "CTRL": 0x11, "ALT": 0x12, "WIN": 0x5B,
    "BACKSPACE": 0x08, "DELETE": 0x2E, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    **{f"F{i}": 0x6F + i for i in range(1, 13)},
}


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYINPUTUNION(ctypes.Union):
    # INPUT is a union of mouse/keyboard/hardware. Keeping MOUSEINPUT here
    # preserves the native Windows INPUT structure size for SendInput.
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _KEYINPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _KEYINPUTUNION)]


class KeyboardOutput:
    """Small SendInput keyboard backend for voice single keys and combos."""

    INPUT_KEYBOARD = 1
    MAPVK_VK_TO_VSC = 0
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_SCANCODE = 0x0008
    EXTENDED_KEYS = {
        "WIN", "DELETE", "HOME", "END", "PAGEUP", "PAGEDOWN",
        "LEFT", "UP", "RIGHT", "DOWN",
    }

    def __init__(self, *, user32=None) -> None:
        self._user32 = user32
        if self._user32 is None and os.name == "nt":
            self._user32 = ctypes.windll.user32
        self.available = self._user32 is not None
        self.pressed: set[str] = set()
        self.last_error: str | None = None

    @staticmethod
    def normalize(key: str) -> str:
        aliases = {"CONTROL": "CTRL", "WINDOWS": "WIN", "RETURN": "ENTER", "DEL": "DELETE"}
        value = str(key).strip().upper()
        return aliases.get(value, value)

    def set_key(self, key: str, pressed: bool) -> None:
        key = self.normalize(key)
        if pressed == (key in self.pressed):
            return
        code = KEY_CODES.get(key)
        if code is None:
            raise ValueError(f"不支持的键盘键：{key}")
        if not self.available:
            raise RuntimeError("键盘输出仅支持 Windows")
        scan_code = int(self._user32.MapVirtualKeyW(code, self.MAPVK_VK_TO_VSC))
        if scan_code <= 0:
            raise RuntimeError(f"无法取得键盘扫描码：{key}")
        flags = self.KEYEVENTF_SCANCODE
        if key in self.EXTENDED_KEYS:
            flags |= self.KEYEVENTF_EXTENDEDKEY
        if not pressed:
            flags |= self.KEYEVENTF_KEYUP
        event = _KEYINPUT()
        event.type = self.INPUT_KEYBOARD
        # Games commonly consume physical scan codes through Raw Input or
        # DirectInput.  A virtual-key-only SendInput event can work in desktop
        # apps yet be ignored by a game, which is why Xbox mappings appeared to
        # work while an otherwise valid W mapping did not.
        event.ki = _KEYBDINPUT(0, scan_code, flags, 0, None)
        sent = self._user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_KEYINPUT))
        if sent != 1:
            self.last_error = f"SendInput failed: {key}"
            raise RuntimeError(self.last_error)
        if pressed:
            self.pressed.add(key)
        else:
            self.pressed.discard(key)
        self.last_error = None

    def tap_combo(self, combo: str, hold_seconds: float = 0.06) -> None:
        keys = [self.normalize(x) for x in str(combo).split("+") if str(x).strip()]
        if not keys or len(keys) > 4:
            raise ValueError(f"键盘组合键格式错误：{combo}")
        for key in keys:
            if key not in KEY_CODES:
                raise ValueError(f"不支持的键盘键：{key}")
        pressed: list[str] = []
        try:
            for key in keys:
                self.set_key(key, True)
                pressed.append(key)
            time.sleep(max(0.02, min(0.20, float(hold_seconds))))
        finally:
            for key in reversed(pressed):
                try:
                    self.set_key(key, False)
                except Exception:
                    pass

    def release_all(self) -> None:
        for key in tuple(self.pressed):
            try:
                self.set_key(key, False)
            except Exception:
                self.pressed.discard(key)


VIGEM_ERROR_NONE = 0x20000000

XUSB_GAMEPAD_BUTTONS = {
    "DPAD_UP": 0x0001,
    "DPAD_DOWN": 0x0002,
    "DPAD_LEFT": 0x0004,
    "DPAD_RIGHT": 0x0008,
    "START": 0x0010,
    "BACK": 0x0020,
    "L3": 0x0040,
    "R3": 0x0080,
    "LB": 0x0100,
    "RB": 0x0200,
    "A": 0x1000,
    "B": 0x2000,
    "X": 0x4000,
    "Y": 0x8000,
}

GAMEPAD_AXES = {
    "LS_UP": (0.0, 1.0),
    "LS_DOWN": (0.0, -1.0),
    "LS_LEFT": (-1.0, 0.0),
    "LS_RIGHT": (1.0, 0.0),
}


class XUSB_REPORT(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_GAMEPAD(ctypes.Structure):
    """Windows XInput physical-gamepad state (原始物理手柄状态)."""

    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_uint32),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


XINPUT_ERROR_DEVICE_NOT_CONNECTED = 1167


class XInputReader:
    """Small standard-library XInput reader; it never creates another pad."""

    USER_SLOTS = tuple(range(4))

    def __init__(self, loader=None) -> None:
        self._get_state = None
        self.backend_name: str | None = None
        self.last_error: str | None = None
        if os.name != "nt":
            return
        load = loader or ctypes.WinDLL
        for name in ("xinput1_4.dll", "xinput9_1_0.dll", "xinput1_3.dll"):
            try:
                dll = load(name)
                fn = dll.XInputGetState
                fn.argtypes = (ctypes.c_uint, ctypes.POINTER(XINPUT_STATE))
                fn.restype = ctypes.c_uint
                self._get_state = fn
                self.backend_name = name
                break
            except Exception as exc:
                self.last_error = str(exc)

    @property
    def available(self) -> bool:
        return self._get_state is not None

    @staticmethod
    def _button_names(mask: int) -> set[str]:
        return {
            name for name, value in XUSB_GAMEPAD_BUTTONS.items()
            if int(mask) & int(value)
        }

    @staticmethod
    def _axis(value: int) -> float:
        value = int(value)
        return max(-1.0, min(1.0, value / 32768.0 if value < 0 else value / 32767.0))

    def read(self, user_index: int) -> dict | None:
        if self._get_state is None:
            return None
        state = XINPUT_STATE()
        try:
            code = int(self._get_state(int(user_index), ctypes.byref(state)))
        except Exception as exc:
            self.last_error = str(exc)
            return None
        if code != 0:
            if code != XINPUT_ERROR_DEVICE_NOT_CONNECTED:
                self.last_error = f"XInputGetState failed: 0x{code:08X}"
            return None
        pad = state.Gamepad
        return {
            "user_index": int(user_index),
            "packet_number": int(state.dwPacketNumber),
            "raw_report": bytes(pad),
            "buttons": self._button_names(pad.wButtons),
            "left_trigger": max(0.0, min(1.0, int(pad.bLeftTrigger) / 255.0)),
            "right_trigger": max(0.0, min(1.0, int(pad.bRightTrigger) / 255.0)),
            "left_x": self._axis(pad.sThumbLX),
            "left_y": self._axis(pad.sThumbLY),
            "right_x": self._axis(pad.sThumbRX),
            "right_y": self._axis(pad.sThumbRY),
        }


VIGEM_ERRORS = {
    0xE0000001: "ViGEmBus driver not found",
    0xE0000002: "no free virtual gamepad slot",
    0xE0000008: "ViGEmBus version mismatch",
    0xE0000009: "ViGEmBus access denied",
}


def _vigem_check(code: int, operation: str) -> None:
    code &= 0xFFFFFFFF
    if code != VIGEM_ERROR_NONE:
        raise RuntimeError(f"{operation}: {VIGEM_ERRORS.get(code, f'ViGEm error 0x{code:08X}')}")


def _service_running() -> bool:
    if os.name != "nt":
        return False
    try:
        cp = subprocess.run(
            ["sc.exe", "query", "ViGEmBus"],
            capture_output=True,
            text=True,
            timeout=1.5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return cp.returncode == 0 and "RUNNING" in (cp.stdout or "").upper()
    except Exception:
        return False


def find_vigemclient(root: Path) -> Path | None:
    candidates: list[Path] = []
    env = os.environ.get("VIGEMCLIENT_DLL", "").strip().strip('"')
    if env:
        candidates.append(Path(env))
    config = root / "config" / "vigemclient_dll.txt"
    if config.exists():
        try:
            text = config.read_text(encoding="utf-8-sig").strip().strip('"')
            if text:
                candidates.append(Path(text))
        except OSError:
            pass
    candidates.extend(
        [
            root / "native" / "x64" / "ViGEmClient.dll",
            Path(r"F:\switch\motionbridge\outputs\native\x64\ViGEmClient.dll"),
            Path(r"F:\switch\motionbridge\motionbridge\outputs\native\x64\ViGEmClient.dll"),
            Path(r"F:\switch\motionbridge\outputs\native\ViGEmClient.dll"),
        ]
    )
    for path in candidates:
        try:
            if path.is_file():
                return path.resolve()
        except OSError:
            continue
    return None


class VX360Gamepad:
    """Minimal Xbox 360 right-stick output through ViGEmClient + ViGEmBus."""

    def __init__(self, dll_path: Path) -> None:
        if platform.system() != "Windows" or platform.architecture()[0] != "64bit":
            raise RuntimeError("Xbox output requires 64-bit Windows")
        if not dll_path.is_file():
            raise RuntimeError(f"ViGEmClient.dll not found: {dll_path}")
        self._identity_reader = XInputReader()
        self._users_before_attach = {i for i in XInputReader.USER_SLOTS if self._identity_reader.read(i) is not None}
        self._identified_user = None
        self._dll = ctypes.CDLL(str(dll_path))
        self._bind()
        self._client = self._dll.vigem_alloc()
        self._target = None
        if not self._client:
            raise RuntimeError("vigem_alloc returned null")
        try:
            _vigem_check(self._dll.vigem_connect(self._client), "vigem_connect")
            self._target = self._dll.vigem_target_x360_alloc()
            if not self._target:
                raise RuntimeError("vigem_target_x360_alloc returned null")
            _vigem_check(self._dll.vigem_target_add(self._client, self._target), "vigem_target_add")
            self.report = XUSB_REPORT()
            self.update()
        except Exception:
            self.close()
            raise

    def _bind(self) -> None:
        dll = self._dll
        dll.vigem_alloc.argtypes = ()
        dll.vigem_alloc.restype = ctypes.c_void_p
        dll.vigem_free.argtypes = (ctypes.c_void_p,)
        dll.vigem_free.restype = None
        dll.vigem_connect.argtypes = (ctypes.c_void_p,)
        dll.vigem_connect.restype = ctypes.c_uint
        dll.vigem_disconnect.argtypes = (ctypes.c_void_p,)
        dll.vigem_disconnect.restype = None
        dll.vigem_target_x360_alloc.argtypes = ()
        dll.vigem_target_x360_alloc.restype = ctypes.c_void_p
        dll.vigem_target_add.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        dll.vigem_target_add.restype = ctypes.c_uint
        dll.vigem_target_remove.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        dll.vigem_target_remove.restype = ctypes.c_uint
        dll.vigem_target_free.argtypes = (ctypes.c_void_p,)
        dll.vigem_target_free.restype = None
        dll.vigem_target_x360_update.argtypes = (ctypes.c_void_p, ctypes.c_void_p, XUSB_REPORT)
        dll.vigem_target_x360_update.restype = ctypes.c_uint

    def xinput_user_index(self) -> int:
        # Some ViGEmBus versions report 0 even when this target occupies slot 1.
        # Identify the single new XInput slot created by this attachment instead.
        connected = {i for i in XInputReader.USER_SLOTS if self._identity_reader.read(i) is not None}
        if self._identified_user is not None:
            if self._identified_user in connected:
                return self._identified_user
            raise RuntimeError("虚拟手柄已断开，请重新启动输出服务")
        added = connected - self._users_before_attach
        if len(added) != 1:
            raise RuntimeError("暂时无法唯一确认虚拟手柄编号，请保持其他手柄连接状态不变")
        self._identified_user = added.pop()
        return self._identified_user

    def set_merged_report(self, state: dict, names) -> None:
        raw = state.get("raw_report")
        if raw is not None:
            self.report = XUSB_REPORT.from_buffer_copy(raw)
        else:
            self.report = XUSB_REPORT()
            for field, key in (("sThumbLX", "left_x"), ("sThumbLY", "left_y"),
                               ("sThumbRX", "right_x"), ("sThumbRY", "right_y")):
                value = max(-1.0, min(1.0, float(state.get(key, 0))))
                setattr(self.report, field, round(value * (32768 if value < 0 else 32767)))
            self.report.bLeftTrigger = round(float(state.get("left_trigger", 0)) * 255)
            self.report.bRightTrigger = round(float(state.get("right_trigger", 0)) * 255)
        # Preserve every physical bit, and publish the complete chord in one report.
        for name in names:
            self.report.wButtons |= XUSB_GAMEPAD_BUTTONS[name]
        self.update()

    def set_left_stick(self, x: float, y: float = 0.0) -> None:
        x = max(-1.0, min(1.0, float(x)))
        y = max(-1.0, min(1.0, float(y)))
        self.report.sThumbLX = round(x * 32767)
        self.report.sThumbLY = round(y * 32767)
        self.update()

    def set_right_stick(self, x: float, y: float = 0.0) -> None:
        """Set virtual Xbox right stick. Frontend y follows mouse semantics: +y = look down."""
        x = max(-1.0, min(1.0, float(x)))
        y = max(-1.0, min(1.0, float(y)))
        self.report.sThumbRX = round(x * 32767)
        # XInput right-stick +Y is up, while mouse +Y is down. Invert here so
        # the same head-control signal feels the same in mouse and gamepad modes.
        self.report.sThumbRY = round(-y * 32767)
        self.update()

    def set_right_stick_raw(self, x: float, y: float = 0.0) -> None:
        """Copy an XInput right stick without applying mouse-coordinate inversion."""
        x = max(-1.0, min(1.0, float(x)))
        y = max(-1.0, min(1.0, float(y)))
        self.report.sThumbRX = round(x * 32767)
        self.report.sThumbRY = round(y * 32767)
        self.update()

    def set_right_x(self, value: float) -> None:
        self.set_right_stick(value, 0.0)

    def set_buttons(self, names) -> None:
        mask = 0
        for name in names:
            key = str(name).upper()
            if key not in XUSB_GAMEPAD_BUTTONS:
                raise ValueError(f"unsupported Xbox button: {name}")
            mask |= XUSB_GAMEPAD_BUTTONS[key]
        self.report.wButtons = mask
        self.update()

    def set_triggers(self, left: float = 0.0, right: float = 0.0) -> None:
        """Set the analogue LT/RT values used by a handheld sensor source."""
        self.report.bLeftTrigger = round(max(0.0, min(1.0, float(left))) * 255)
        self.report.bRightTrigger = round(max(0.0, min(1.0, float(right))) * 255)
        self.update()

    def reset(self) -> None:
        self.report = XUSB_REPORT()
        self.update()

    def update(self) -> None:
        if self._client and self._target:
            _vigem_check(
                self._dll.vigem_target_x360_update(self._client, self._target, self.report),
                "vigem_target_x360_update",
            )

    def close(self) -> None:
        target, client = getattr(self, "_target", None), getattr(self, "_client", None)
        self._target = None
        self._client = None
        if target and client:
            try:
                self._dll.vigem_target_remove(client, target)
            finally:
                self._dll.vigem_target_free(target)
        if client:
            self._dll.vigem_disconnect(client)
            self._dll.vigem_free(client)


class OutputManager:
    """Head-view output manager with watchdog zeroing for virtual sticks."""

    def __init__(self, root: Path, mouse=None, keyboard=None, xinput_reader=None) -> None:
        self.root = root
        self.mouse = mouse or MouseOutput()
        self.keyboard = keyboard or KeyboardOutput()
        self.mode = "mouse"
        self.enabled = False
        self.mouse_speed_x = 600.0
        self.mouse_speed_y = 450.0
        self.gamepad_gain = 1.6
        self.last_error: str | None = None
        self.last_update = 0.0
        self.last_value = 0.0
        self.last_x = 0.0
        self.last_y = 0.0
        self._mouse_residual_x = 0.0
        self._mouse_residual_y = 0.0
        self._pad: VX360Gamepad | None = None
        self._vigem_dll = find_vigemclient(root)
        self._last_service_check = 0.0
        self._service_is_running = False
        self.last_buttons: tuple[str, ...] = ()
        self._button_sources: dict[str, set[str]] = {"zones": set()}
        self._keyboard_sources: dict[str, set[str]] = {}
        self._mouse_button_sources: dict[str, set[str]] = {}
        self._left_stick_sources: dict[str, tuple[float, float]] = {}
        self._trigger_sources: dict[str, tuple[float, float]] = {}
        # Physical XInput is sampled and merged into this same virtual report.
        # It is deliberately opt-in so the ordinary mouse/virtual-pad paths do
        # not change for existing users.
        self._xinput_reader = xinput_reader or XInputReader()
        self._xinput_merge_enabled = False
        self._xinput_selected_user: int | None = None
        self._xinput_active_user: int | None = None
        self._xinput_state: dict | None = None
        self._xinput_source: str | None = None
        self._xinput_connected_users: tuple[int, ...] = ()
        self._xinput_last_poll = 0.0
        self._xinput_last_error: str | None = None
        self._xinput_poll_interval = 1.0 / 60.0
        self.last_button_update = 0.0
        self.last_hold_update = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch_loop, daemon=True)
        self._watchdog.start()
        self._xinput_thread = threading.Thread(target=self._xinput_loop, name="xinput-physical-merge", daemon=True)
        self._xinput_thread.start()

    @property
    def vigem_dll(self) -> Path | None:
        return self._vigem_dll

    def _vigembus_running_cached(self) -> bool:
        now = time.monotonic()
        if now - self._last_service_check > 2.0:
            self._service_is_running = _service_running()
            self._last_service_check = now
        return self._service_is_running

    def _ensure_pad(self) -> VX360Gamepad:
        if self._pad is not None:
            return self._pad
        dll = self.vigem_dll
        if dll is None:
            raise RuntimeError("ViGEmClient.dll 未找到；请使用 MotionBridge 已有的 ViGEmClient.dll 或配置 config/vigemclient_dll.txt")
        self._pad = VX360Gamepad(dll)
        return self._pad

    def _xinput_merge_active_locked(self) -> bool:
        return bool(self._xinput_merge_enabled and self.mode == "gamepad")

    @staticmethod
    def _normalize_xinput_user(value) -> int | None:
        if value is None or value == "":
            return None
        try:
            user = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("手柄编号必须是 0 到 3") from exc
        if user not in XInputReader.USER_SLOTS:
            raise ValueError("XInput 手柄索引必须是 0 到 3")
        return user

    def _clear_physical_xinput_locked(self) -> None:
        if self._xinput_source:
            self._button_sources.pop(self._xinput_source, None)
        self._xinput_state = None
        self._xinput_active_user = None
        self._xinput_source = None
        self._refresh_buttons_locked()
        self._refresh_left_stick_locked()
        self._refresh_triggers_locked()
        self._refresh_right_stick_locked()

    def _apply_xinput_state_locked(self, user: int | None, state: dict | None) -> None:
        old_source = self._xinput_source
        if old_source and (state is None or old_source != f"xinput:{user}"):
            self._button_sources.pop(old_source, None)
        if state is None:
            self._xinput_state = None
            self._xinput_active_user = None
            self._xinput_source = None
        else:
            source = f"xinput:{int(user)}"
            self._xinput_state = dict(state)
            self._xinput_active_user = int(user)
            self._xinput_source = source
            self._button_sources[source] = set(state.get("buttons", set()))
        self._refresh_buttons_locked()
        self._refresh_left_stick_locked()
        self._refresh_triggers_locked()
        self._refresh_right_stick_locked()

    def _xinput_loop(self) -> None:
        while not self._stop.wait(self._xinput_poll_interval):
            with self._lock:
                if self._stop.is_set():
                    return
                try:
                    virtual = self._pad.xinput_user_index() if self._pad is not None else None
                    reader = self._xinput_reader
                    connected = {}
                    for user in getattr(reader, "USER_SLOTS", XInputReader.USER_SLOTS):
                        if user == virtual:
                            continue
                        state = reader.read(user)
                        if state is not None:
                            connected[int(user)] = state
                    self._xinput_connected_users = tuple(sorted(connected))
                    self._xinput_last_poll = time.monotonic()
                    self._xinput_last_error = getattr(reader, "last_error", None)
                    if self._xinput_merge_active_locked():
                        chosen = self._xinput_selected_user
                        self._apply_xinput_state_locked(chosen, connected.get(chosen))
                except Exception as exc:
                    self._xinput_last_error = str(exc)
                    self._xinput_connected_users = ()
                    if self._xinput_merge_active_locked():
                        self._apply_xinput_state_locked(None, None)

    def configure_xinput_merge(self, *, enabled: bool | None = None, user=_UNSET) -> dict:
        """Configure one physical XInput slot to merge into the virtual pad."""
        with self._lock:
            if user is not _UNSET:
                normalized = self._normalize_xinput_user(user)
                if normalized != self._xinput_selected_user:
                    self._clear_physical_xinput_locked()
                self._xinput_selected_user = normalized
            if enabled is not None:
                requested = bool(enabled)
                if requested and self._xinput_selected_user is None:
                    raise ValueError("请明确选择一个物理手柄")
                if requested and self.mode != "gamepad":
                    # The merge has one virtual Xbox report by definition.  A
                    # mouse-mode session cannot silently consume the physical
                    # controller, so enabling it promotes the mode explicitly.
                    self._zero_locked(force_physical=True)
                    self.mode = "gamepad"
                if requested and not self._xinput_merge_enabled:
                    self._ensure_pad()
                if requested and not self._xinput_merge_enabled:
                    self._zero_locked(force_physical=True)
                self._xinput_merge_enabled = requested
                if not requested:
                    self._clear_physical_xinput_locked()
            if self._xinput_merge_enabled:
                self._ensure_pad()
            return self.status()

    def xinput_status(self) -> dict:
        with self._lock:
            reader = self._xinput_reader
            state = self._xinput_state or {}
            return {
                "enabled": bool(self._xinput_merge_enabled),
                "active": self._xinput_merge_active_locked(),
                "selected_user": self._xinput_selected_user,
                "active_user": self._xinput_active_user,
                "connected_users": list(self._xinput_connected_users),
                "connected": self._xinput_state is not None,
                "backend": getattr(reader, "backend_name", None),
                "available": bool(getattr(reader, "available", False)),
                "last_poll_age_ms": round((time.monotonic() - self._xinput_last_poll) * 1000.0) if self._xinput_last_poll else None,
                "last_error": self._xinput_last_error,
                "buttons": sorted(str(x) for x in state.get("buttons", set())),
                "left_stick": {"x": round(float(state.get("left_x", 0.0)), 4), "y": round(float(state.get("left_y", 0.0)), 4)},
                "right_stick": {"x": round(float(state.get("right_x", 0.0)), 4), "y": round(float(state.get("right_y", 0.0)), 4)},
                "triggers": {"left": round(float(state.get("left_trigger", 0.0)), 4), "right": round(float(state.get("right_trigger", 0.0)), 4)},
            }

    def set_config(self, *, mode: str | None = None, enabled: bool | None = None,
                   mouse_speed: float | None = None, mouse_speed_x: float | None = None, mouse_speed_y: float | None = None,
                   gamepad_gain: float | None = None, xinput_merge_enabled: bool | None = None,
                   physical_xinput_user=_UNSET) -> dict:
        with self._lock:
            if mode is not None:
                if mode not in {"mouse", "gamepad"}:
                    raise ValueError("mode must be mouse or gamepad")
                if mode != self.mode:
                    self._zero_locked(force_physical=True)
                    self.mode = mode
                    if mode != "gamepad":
                        self._xinput_merge_enabled = False
                        self._clear_physical_xinput_locked()
                # Create the virtual Xbox controller as soon as gamepad mode is selected,
                # even before output is enabled. This gives games a chance to enumerate it.
                if self.mode == "gamepad":
                    self._ensure_pad()
            if mouse_speed is not None:
                v = max(100.0, min(4000.0, float(mouse_speed)))
                self.mouse_speed_x = v
                self.mouse_speed_y = v
            if mouse_speed_x is not None:
                self.mouse_speed_x = max(80.0, min(3000.0, float(mouse_speed_x)))
            if mouse_speed_y is not None:
                self.mouse_speed_y = max(60.0, min(2500.0, float(mouse_speed_y)))
            if gamepad_gain is not None:
                self.gamepad_gain = max(0.1, min(3.0, float(gamepad_gain)))
            if xinput_merge_enabled is not None or physical_xinput_user is not _UNSET:
                self.configure_xinput_merge(enabled=xinput_merge_enabled, user=physical_xinput_user)
            if enabled is not None:
                if enabled and self.mode == "gamepad":
                    self._ensure_pad()
                self.enabled = bool(enabled)
                if not self.enabled:
                    self._zero_locked()
            self.last_error = None
            return self.status()

    def toggle(self) -> dict:
        try:
            return self.set_config(enabled=not self.enabled)
        except Exception as exc:
            self.enabled = False
            self.last_error = str(exc)
            return self.status()

    def apply(self, x: float, y: float = 0.0) -> None:
        now = time.monotonic()
        with self._lock:
            x = max(-1.0, min(1.0, float(x)))
            y = max(-1.0, min(1.0, float(y)))
            dt = now - self.last_update if self.last_update else 1 / 30
            dt = max(0.0, min(0.08, dt))
            self.last_update = now
            self.last_value = x
            self.last_x = x
            self.last_y = y
            if not self.enabled:
                return
            try:
                if self._xinput_merge_active_locked():
                    # The physical controller owns both sticks and triggers in
                    # merge mode. Motion input is intentionally button-only.
                    return
                if self.mode == "mouse":
                    # X has already been assigned by the labeled
                    # left/center/right head anchors in ControlKernel.  Keep
                    # this boundary source/mirror agnostic: no second global
                    # sign flip belongs here.
                    amount_x = x * self.mouse_speed_x * dt + self._mouse_residual_x
                    amount_y = y * self.mouse_speed_y * dt + self._mouse_residual_y
                    dx = int(amount_x)
                    dy = int(amount_y)
                    self._mouse_residual_x = amount_x - dx
                    self._mouse_residual_y = amount_y - dy
                    if dx or dy:
                        self.mouse.move(dx, dy)
                        if self.mouse.last_error:
                            raise RuntimeError(self.mouse.last_error)
                else:
                    self._ensure_pad().set_right_stick(x * self.gamepad_gain, y * self.gamepad_gain)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self.enabled = False
                self._zero_locked()

    def _refresh_buttons_locked(self) -> None:
        physical = set()
        if self._xinput_merge_active_locked() and self._xinput_state is not None:
            physical = set(self._xinput_state.get("buttons", set()))
        motion = set()
        if self.enabled:
            motion = set().union(*[
                values for source, values in self._button_sources.items()
                if source != self._xinput_source
            ]) if self._button_sources else set()
        names = tuple(sorted(physical | motion))
        if self._xinput_merge_active_locked() and self._pad is not None:
            self._pad.set_merged_report(self._xinput_state or {}, names)
        elif names or self._pad is not None:
            self._ensure_pad().set_buttons(names)
        self.last_buttons = names

    @staticmethod
    def _combo_keys(target: str) -> set[str]:
        keys = {KeyboardOutput.normalize(x) for x in str(target).split("+") if str(x).strip()}
        if not keys or len(keys) > 4:
            raise ValueError(f"键盘组合键格式错误：{target}")
        invalid = [x for x in keys if x not in KEY_CODES]
        if invalid:
            raise ValueError("不支持的键盘键：" + ", ".join(sorted(invalid)))
        return keys

    def _refresh_keyboard_locked(self) -> None:
        desired = set().union(*self._keyboard_sources.values()) if self.enabled and not self._xinput_merge_active_locked() and self._keyboard_sources else set()
        current = set(self.keyboard.pressed)
        for key in sorted(current - desired, reverse=True):
            self.keyboard.set_key(key, False)
        for key in sorted(desired - current):
            self.keyboard.set_key(key, True)

    def _refresh_mouse_buttons_locked(self) -> None:
        desired = set().union(*self._mouse_button_sources.values()) if self.enabled and not self._xinput_merge_active_locked() and self._mouse_button_sources else set()
        current = set(getattr(self.mouse, "pressed", set()))
        for button in sorted(current - desired):
            self.mouse.set_button(button, False)
        for button in sorted(desired - current):
            self.mouse.set_button(button, True)

    def _refresh_left_stick_locked(self) -> None:
        if self._xinput_merge_active_locked():
            return  # _refresh_buttons_locked publishes the complete merged report.
        if self._xinput_merge_active_locked() and self._xinput_state is not None:
            x = float(self._xinput_state.get("left_x", 0.0))
            y = float(self._xinput_state.get("left_y", 0.0))
        else:
            x = y = 0.0
        if self.enabled and not self._xinput_merge_active_locked():
            for sx, sy in self._left_stick_sources.values():
                x += sx
                y += sy
        x = max(-1.0, min(1.0, x))
        y = max(-1.0, min(1.0, y))
        if (x or y) or self._pad is not None:
            self._ensure_pad().set_left_stick(x, y)

    def _refresh_triggers_locked(self) -> None:
        if self._xinput_merge_active_locked():
            return  # _refresh_buttons_locked publishes the complete merged report.
        if self._xinput_merge_active_locked() and self._xinput_state is not None:
            left = float(self._xinput_state.get("left_trigger", 0.0))
            right = float(self._xinput_state.get("right_trigger", 0.0))
        else:
            left = right = 0.0
        if self.enabled and not self._xinput_merge_active_locked():
            for source_left, source_right in self._trigger_sources.values():
                left = max(left, source_left)
                right = max(right, source_right)
        if (left or right) or self._pad is not None:
            setter = getattr(self._ensure_pad(), "set_triggers", None)
            if setter is not None:
                setter(left, right)

    def _refresh_right_stick_locked(self) -> None:
        if self._xinput_merge_active_locked():
            return  # _refresh_buttons_locked publishes the complete merged report.
        if self._pad is None:
            return
        try:
            if self._xinput_merge_active_locked() and self._xinput_state is not None:
                setter = getattr(self._pad, "set_right_stick_raw", None)
                if setter is not None:
                    setter(self._xinput_state.get("right_x", 0.0), self._xinput_state.get("right_y", 0.0))
                else:
                    # Test doubles and legacy adapters may only expose the
                    # semantic setter; preserve the physical sign as far as
                    # that adapter permits.
                    self._pad.set_right_stick(self._xinput_state.get("right_x", 0.0), -self._xinput_state.get("right_y", 0.0))
            elif self.mode == "gamepad":
                setter = getattr(self._pad, "set_right_stick_raw", None)
                if setter is not None:
                    setter(0.0, 0.0)
                else:
                    self._pad.set_right_stick(0.0, 0.0)
        except Exception as exc:
            self.last_error = str(exc)

    @staticmethod
    def _gamepad_targets(target) -> set[str]:
        if isinstance(target, (list, tuple, set)):
            parts = [str(item).strip().upper() for item in target]
        else:
            parts = [part.strip().upper() for part in str(target).replace(",", "+").split("+")]
        parts = [part for part in parts if part]
        if not parts:
            raise ValueError("Xbox 按键不能为空")
        invalid = [part for part in parts if part not in XUSB_GAMEPAD_BUTTONS]
        if invalid:
            raise ValueError("不支持的 Xbox 按键：" + ", ".join(sorted(set(invalid))))
        return set(parts)

    def set_holds(self, holds, source_group: str = "motions") -> dict:
        """Replace one group's continuous keyboard/gamepad/left-stick holds."""
        prefix = str(source_group) + ":"
        with self._lock:
            for key in [k for k in self._button_sources if k.startswith(prefix)]:
                self._button_sources.pop(key, None)
            for key in [k for k in self._keyboard_sources if k.startswith(prefix)]:
                self._keyboard_sources.pop(key, None)
            for key in [k for k in self._left_stick_sources if k.startswith(prefix)]:
                self._left_stick_sources.pop(key, None)
            for item in holds or []:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("id", "")).strip()
                action_type = str(item.get("type", "")).strip().lower()
                target = item.get("target", "")
                if not isinstance(target, (list, tuple, set)):
                    target = str(target).strip().upper()
                if not ident or not target:
                    continue
                source = prefix + ident
                if action_type == "gamepad":
                    self._button_sources[source] = self._gamepad_targets(target)
                elif action_type == "keyboard":
                    if self._xinput_merge_active_locked():
                        continue
                    self._keyboard_sources[source] = self._combo_keys(target)
                elif action_type == "gamepad_axis":
                    if self._xinput_merge_active_locked():
                        continue
                    if target not in GAMEPAD_AXES:
                        raise ValueError(f"不支持的 Xbox 摇杆方向：{target}")
                    self._left_stick_sources[source] = GAMEPAD_AXES[target]
                else:
                    raise ValueError(f"不支持的持续输出类型：{action_type}")
            self.last_hold_update = time.monotonic()
            try:
                self._refresh_buttons_locked()
                self._refresh_keyboard_locked()
                self._refresh_mouse_buttons_locked()
                self._refresh_left_stick_locked()
                self._refresh_triggers_locked()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self.enabled = False
                self._zero_locked()
                raise
            return self.status()

    def set_action_holds(self, holds, source_group: str = "controls") -> dict:
        """Replace one trigger group's continuous outputs using the unified action schema.

        Supported hold actions: keyboard, mouse_button, gamepad buttons, LT/RT and
        left-stick cardinal directions. mouse_wheel is intentionally rejected because
        a wheel is an impulse and must never run continuously while a zone is occupied.
        """
        prefix = str(source_group) + ":"
        with self._lock:
            for store in (self._button_sources, self._keyboard_sources, self._mouse_button_sources, self._left_stick_sources, self._trigger_sources):
                for key in [k for k in store if k.startswith(prefix)]:
                    store.pop(key, None)
            for item in holds or []:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("id", "")).strip()
                action = item.get("action") if isinstance(item.get("action"), dict) else item
                action_type = str(action.get("type", "")).strip().lower()
                target = action.get("target", "")
                if not isinstance(target, (list, tuple, set)):
                    target = str(target).strip().upper()
                if not ident or not target:
                    continue
                source = prefix + ident
                if action_type in {"gamepad", "gamepad_button", "xinput_button"}:
                    self._button_sources[source] = self._gamepad_targets(target)
                elif action_type == "keyboard":
                    if self._xinput_merge_active_locked():
                        continue
                    self._keyboard_sources[source] = self._combo_keys(target)
                elif action_type == "mouse_button":
                    if self._xinput_merge_active_locked():
                        continue
                    if target not in {"LEFT", "RIGHT", "MIDDLE", "X1", "X2"}:
                        raise ValueError(f"不支持的鼠标按键：{target}")
                    self._mouse_button_sources[source] = {target}
                elif action_type == "gamepad_axis":
                    if self._xinput_merge_active_locked():
                        continue
                    if target not in GAMEPAD_AXES:
                        raise ValueError(f"不支持的 Xbox 摇杆方向：{target}")
                    self._left_stick_sources[source] = GAMEPAD_AXES[target]
                elif action_type == "gamepad_trigger":
                    if self._xinput_merge_active_locked():
                        continue
                    if target == "LT":
                        self._trigger_sources[source] = (1.0, 0.0)
                    elif target == "RT":
                        self._trigger_sources[source] = (0.0, 1.0)
                    else:
                        raise ValueError(f"不支持的 Xbox 扳机：{target}")
                elif action_type == "mouse_wheel":
                    if self._xinput_merge_active_locked():
                        continue
                    raise ValueError("鼠标滚轮只能使用 tap，不能作为持续 hold")
                else:
                    raise ValueError(f"不支持的持续输出类型：{action_type}")
            self.last_hold_update = time.monotonic()
            try:
                self._refresh_buttons_locked()
                self._refresh_keyboard_locked()
                self._refresh_mouse_buttons_locked()
                self._refresh_left_stick_locked()
                self._refresh_triggers_locked()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self.enabled = False
                self._zero_locked()
                raise
            return self.status()

    def set_sensor_state(self, source: str, buttons, *, left_trigger: float = 0.0,
                         right_trigger: float = 0.0, stick_x: float = 0.0,
                         stick_y: float = 0.0) -> dict:
        """Replace one handheld phone's current buttons, triggers and left stick."""
        source = str(source).strip()
        if not source:
            raise ValueError("sensor source must not be empty")
        names = {str(x).upper() for x in (buttons or [])}
        invalid = [x for x in names if x not in XUSB_GAMEPAD_BUTTONS]
        if invalid:
            raise ValueError("unsupported Xbox buttons: " + ", ".join(sorted(invalid)))
        left = max(0.0, min(1.0, float(left_trigger)))
        right = max(0.0, min(1.0, float(right_trigger)))
        x = max(-1.0, min(1.0, float(stick_x)))
        y = max(-1.0, min(1.0, float(stick_y)))
        with self._lock:
            self._button_sources[source] = names
            self._left_stick_sources[source] = (x, y)
            self._trigger_sources[source] = (left, right)
            try:
                self._refresh_buttons_locked()
                self._refresh_left_stick_locked()
                self._refresh_triggers_locked()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self.enabled = False
                self._zero_locked()
                raise
            return self.status()

    def clear_source(self, source: str) -> dict:
        """Release all output contributed by one remote source immediately."""
        source = str(source)
        with self._lock:
            self._button_sources.pop(source, None)
            self._keyboard_sources.pop(source, None)
            self._mouse_button_sources.pop(source, None)
            self._left_stick_sources.pop(source, None)
            self._trigger_sources.pop(source, None)
            try:
                self._refresh_buttons_locked()
                self._refresh_keyboard_locked()
                self._refresh_mouse_buttons_locked()
                self._refresh_left_stick_locked()
                self._refresh_triggers_locked()
            except Exception as exc:
                self.last_error = str(exc)
                self.enabled = False
                self._zero_locked()
                raise
            return self.status()

    def set_buttons(self, buttons, source: str = "zones") -> dict:
        with self._lock:
            names = {str(x).upper() for x in (buttons or [])}
            invalid = [x for x in names if x not in XUSB_GAMEPAD_BUTTONS]
            if invalid:
                raise ValueError("unsupported Xbox buttons: " + ", ".join(sorted(invalid)))
            self._button_sources[str(source)] = names
            if source == "zones":
                self.last_button_update = time.monotonic()
            try:
                self._refresh_buttons_locked()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                self.enabled = False
                self._zero_locked()
                raise
            return self.status()

    def tap_gamepad(self, button: str, duration: float = 0.10, source: str | None = None) -> None:
        buttons = self._gamepad_targets(button)
        source = str(source).strip() if source else f"voice-{time.monotonic_ns()}"
        with self._lock:
            if not self.enabled:
                return
            self._button_sources[source] = buttons
            self._refresh_buttons_locked()
        time.sleep(max(0.04, min(0.25, float(duration))))
        with self._lock:
            self._button_sources.pop(source, None)
            self._refresh_buttons_locked()

    def tap_keyboard(self, combo: str, duration: float = 0.06, source: str | None = None) -> None:
        source = str(source).strip() if source else f"voice-keyboard-{time.monotonic_ns()}"
        keys = self._combo_keys(combo)
        with self._lock:
            if not self.enabled or self._xinput_merge_active_locked():
                return
            self._keyboard_sources[source] = keys
            self._refresh_keyboard_locked()
        time.sleep(max(0.02, min(0.20, float(duration))))
        with self._lock:
            self._keyboard_sources.pop(source, None)
            self._refresh_keyboard_locked()

    def _release_later(self, source: str, duration: float) -> None:
        timer = threading.Timer(max(0.02, min(0.30, float(duration))), lambda: self.clear_source(source))
        timer.daemon = True
        timer.start()

    def execute_action(self, action: dict) -> dict:
        """Execute one discrete action without blocking the pose/control thread."""
        if not isinstance(action, dict):
            raise ValueError("action must be an object")
        with self._lock:
            if not self.enabled:
                return {"executed": False, "reason": "output disabled"}
        action_type = str(action.get("type", "")).strip().lower()
        aliases = {"gamepad_button": "gamepad", "xinput_button": "gamepad", "mouse": "mouse_button", "wheel": "mouse_wheel"}
        action_type = aliases.get(action_type, action_type)
        target = action.get("target", "")
        if not isinstance(target, (list, tuple, set)):
            target = str(target).strip().upper()
        source = str(action.get("source", "")).strip() or f"pulse:{action_type}:{time.monotonic_ns()}"
        duration = float(action.get("duration", 0.08))
        nonblocking = bool(action.get("nonblocking", False))
        # Preserve the existing voice/API contract: ordinary keyboard/gamepad taps
        # are complete when execute_action returns. Pose edges opt into the timer
        # path so the camera/control thread never sleeps.
        with self._lock:
            merge_active = self._xinput_merge_active_locked()
        if merge_active and action_type not in {"gamepad", "gamepad_button", "xinput_button"}:
            return {"executed": False, "reason": "冰原狼2合流只允许 Xbox 按键输出"}
        if action_type in {"gamepad_button", "xinput_button"}:
            action_type = "gamepad"
        if not nonblocking and action_type == "gamepad":
            self.tap_gamepad(target, duration=duration, source=source)
            return {"executed": True, "action": f"{action_type}:{target}"}
        if not nonblocking and action_type == "keyboard":
            self.tap_keyboard(target, duration=duration, source=source)
            return {"executed": True, "action": f"{action_type}:{target}"}
        with self._lock:
            if not self.enabled or (self._xinput_merge_active_locked() and action_type != "gamepad"):
                return {"executed": False, "reason": "输出已关闭或合流仅允许手柄按钮"}
            if action_type == "gamepad":
                self._button_sources[source] = self._gamepad_targets(target)
                self._refresh_buttons_locked()
            elif action_type == "keyboard":
                self._keyboard_sources[source] = self._combo_keys(target)
                self._refresh_keyboard_locked()
            elif action_type == "mouse_button":
                if target not in {"LEFT", "RIGHT", "MIDDLE", "X1", "X2"}:
                    raise ValueError(f"不支持的鼠标按键：{target}")
                self._mouse_button_sources[source] = {target}
                self._refresh_mouse_buttons_locked()
            elif action_type == "gamepad_axis":
                if target not in GAMEPAD_AXES:
                    raise ValueError(f"不支持的 Xbox 摇杆方向：{target}")
                self._left_stick_sources[source] = GAMEPAD_AXES[target]
                self._refresh_left_stick_locked()
            elif action_type == "gamepad_trigger":
                if target == "LT":
                    self._trigger_sources[source] = (1.0, 0.0)
                elif target == "RT":
                    self._trigger_sources[source] = (0.0, 1.0)
                else:
                    raise ValueError(f"不支持的 Xbox 扳机：{target}")
                self._refresh_triggers_locked()
            elif action_type == "mouse_wheel":
                if target not in {"SCROLL_UP", "SCROLL_DOWN"}:
                    raise ValueError(f"不支持的滚轮方向：{target}")
                self.mouse.wheel(target, int(action.get("notches", 1)))
                return {"executed": True, "action": f"{action_type}:{target}"}
            else:
                raise ValueError(f"不支持的输出类型：{action_type}")
            self.last_error = None
        self._release_later(source, duration)
        return {"executed": True, "action": f"{action_type}:{target}"}

    def _clear_motion_locked(self) -> None:
        self.last_value = 0.0
        self.last_x = 0.0
        self.last_y = 0.0
        physical = {}
        if self._xinput_merge_active_locked() and self._xinput_source:
            physical[self._xinput_source] = set(self._xinput_state.get("buttons", set()) if self._xinput_state else set())
        self._button_sources = physical or {"zones": set()}
        self._keyboard_sources = {}
        self._mouse_button_sources = {}
        self._left_stick_sources = {}
        self._trigger_sources = {}
        self.keyboard.release_all()
        release_mouse = getattr(self.mouse, "release_all", None)
        if release_mouse is not None:
            release_mouse()
        self._mouse_residual_x = 0.0
        self._mouse_residual_y = 0.0
        self._refresh_buttons_locked()
        self._refresh_left_stick_locked()
        self._refresh_triggers_locked()
        self._refresh_right_stick_locked()

    def _zero_locked(self, *, force_physical: bool = False) -> None:
        if self._xinput_merge_active_locked() and not force_physical:
            self._clear_motion_locked()
            return
        self.last_value = 0.0
        self.last_x = 0.0
        self.last_y = 0.0
        self.last_buttons = ()
        self._button_sources = {"zones": set()}
        self._keyboard_sources = {}
        self._mouse_button_sources = {}
        self._left_stick_sources = {}
        self._trigger_sources = {}
        self.keyboard.release_all()
        release_mouse = getattr(self.mouse, "release_all", None)
        if release_mouse is not None:
            release_mouse()
        self._mouse_residual_x = 0.0
        self._mouse_residual_y = 0.0
        if self._pad is not None:
            try:
                self._pad.reset()
            except Exception as exc:
                self.last_error = str(exc)


    def emergency_stop(self) -> dict:
        with self._lock:
            self.enabled = False
            self._zero_locked()
            return self.status()

    def _watch_loop(self) -> None:
        while not self._stop.wait(0.05):
            with self._lock:
                now = time.monotonic()
                if self.enabled and self.mode == "gamepad" and not self._xinput_merge_active_locked() and self.last_update and now - self.last_update > 0.25:
                    # Only center the stick; button watchdog below is independent.
                    if self._pad is not None:
                        try:
                            self._pad.set_right_stick(0.0, 0.0)
                        except Exception as exc:
                            self.last_error = str(exc)
                if self._button_sources.get("zones") and self.last_button_update and now - self.last_button_update > 0.40:
                    try:
                        self._button_sources["zones"] = set()
                        self._refresh_buttons_locked()
                    except Exception as exc:
                        self.last_error = str(exc)
                if self.last_hold_update and now - self.last_hold_update > 0.45:
                    prefixes = ("motions:", "controls:")
                    changed = False
                    for store in (self._button_sources, self._keyboard_sources, self._mouse_button_sources, self._left_stick_sources, self._trigger_sources):
                        for key in [k for k in store if k.startswith(prefixes)]:
                            store.pop(key, None); changed = True
                    if changed:
                        try:
                            self._refresh_buttons_locked()
                            self._refresh_keyboard_locked()
                            self._refresh_mouse_buttons_locked()
                            self._refresh_left_stick_locked()
                            self._refresh_triggers_locked()
                        except Exception as exc:
                            self.last_error = str(exc)

    def status(self) -> dict:
        dll = self.vigem_dll
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "mouse_available": bool(self.mouse.available),
            "keyboard_available": bool(self.keyboard.available),
            "mouse_speed_x": round(self.mouse_speed_x, 1),
            "mouse_speed_y": round(self.mouse_speed_y, 1),
            "gamepad_gain": round(self.gamepad_gain, 3),
            "vigem_dll": str(dll) if dll else None,
            "vigembus_running": self._vigembus_running_cached(),
            "gamepad_connected": self._pad is not None,
            "xinput_merge_enabled": bool(self._xinput_merge_enabled),
            "xinput_merge_active": self._xinput_merge_active_locked(),
            "xinput_selected_user": self._xinput_selected_user,
            "xinput_active_user": self._xinput_active_user,
            "xinput_connected": self._xinput_state is not None,
            "xinput_connected_users": list(self._xinput_connected_users),
            "xinput_backend": getattr(self._xinput_reader, "backend_name", None),
            "physical_buttons": sorted(str(x) for x in (self._xinput_state or {}).get("buttons", set())),
            "physical_left_stick": {
                "x": round(float((self._xinput_state or {}).get("left_x", 0.0)), 4),
                "y": round(float((self._xinput_state or {}).get("left_y", 0.0)), 4),
            },
            "physical_right_stick": {
                "x": round(float((self._xinput_state or {}).get("right_x", 0.0)), 4),
                "y": round(float((self._xinput_state or {}).get("right_y", 0.0)), 4),
            },
            "physical_triggers": {
                "left": round(float((self._xinput_state or {}).get("left_trigger", 0.0)), 4),
                "right": round(float((self._xinput_state or {}).get("right_trigger", 0.0)), 4),
            },
            "xinput_last_error": self._xinput_last_error,
            "last_value": round(self.last_value, 4),
            "last_x": round(self.last_x, 4),
            "last_y": round(self.last_y, 4),
            "buttons": list(self.last_buttons),
            "keyboard_holds": sorted(set().union(*self._keyboard_sources.values())) if self._keyboard_sources else [],
            "mouse_button_holds": sorted(set().union(*self._mouse_button_sources.values())) if self._mouse_button_sources else [],
            "left_stick_holds": list(self._left_stick_sources.keys()),
            "trigger_holds": list(self._trigger_sources.keys()),
            "last_error": self.last_error,
        }

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self.enabled = False
            self._xinput_merge_enabled = False
            self._zero_locked(force_physical=True)
            self._clear_physical_xinput_locked()
            if self._pad is not None:
                try:
                    self._pad.close()
                finally:
                    self._pad = None


class GlobalHotkeys:
    """F8 toggles output; F9 always performs an emergency stop."""

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    VK_F8 = 0x77
    VK_F9 = 0x78

    def __init__(self, output: OutputManager, emergency_stop=None) -> None:
        self.output = output
        self.emergency_stop = emergency_stop or output.emergency_stop
        self.available = False
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None

    def start(self) -> None:
        if os.name != "nt" or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            self._thread_id = kernel32.GetCurrentThreadId()
            ok8 = bool(user32.RegisterHotKey(None, 8001, 0, self.VK_F8))
            ok9 = bool(user32.RegisterHotKey(None, 8002, 0, self.VK_F9))
            self.available = ok8 and ok9
            if not self.available:
                self.last_error = "F8/F9 全局热键注册失败，可能被其他程序占用"
            msg = ctypes.wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == self.WM_HOTKEY:
                    if msg.wParam == 8001:
                        self.output.toggle()
                    elif msg.wParam == 8002:
                        self.emergency_stop()
            if ok8:
                user32.UnregisterHotKey(None, 8001)
            if ok9:
                user32.UnregisterHotKey(None, 8002)
        except Exception as exc:
            self.last_error = str(exc)
            self.available = False

    def status(self) -> dict:
        return {"available": self.available, "last_error": self.last_error}

    def close(self) -> None:
        if os.name == "nt" and self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass
