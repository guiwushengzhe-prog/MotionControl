from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import platform
import subprocess
import threading
import time
from pathlib import Path


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
    """Windows relative mouse output using SendInput."""

    MOUSEEVENTF_MOVE = 0x0001
    INPUT_MOUSE = 0

    def __init__(self) -> None:
        self.available = os.name == "nt"
        self.last_error: str | None = None

    def move(self, dx: int, dy: int = 0) -> bool:
        if not self.available or (dx == 0 and dy == 0):
            return False
        try:
            event = _INPUT()
            event.type = self.INPUT_MOUSE
            event.mi = _MOUSEINPUT(dx, dy, 0, self.MOUSEEVENTF_MOVE, 0, None)
            sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
            if sent != 1:
                self.last_error = f"SendInput failed: {ctypes.get_last_error()}"
                return False
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False


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
    KEYEVENTF_KEYUP = 0x0002

    def __init__(self) -> None:
        self.available = os.name == "nt"
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
        event = _KEYINPUT()
        event.type = self.INPUT_KEYBOARD
        event.ki = _KEYBDINPUT(code, 0, 0 if pressed else self.KEYEVENTF_KEYUP, 0, None)
        sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_KEYINPUT))
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

    def __init__(self, root: Path, mouse=None, keyboard=None) -> None:
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
        self._left_stick_sources: dict[str, tuple[float, float]] = {}
        self._trigger_sources: dict[str, tuple[float, float]] = {}
        self.last_button_update = 0.0
        self.last_hold_update = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch_loop, daemon=True)
        self._watchdog.start()

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

    def set_config(self, *, mode: str | None = None, enabled: bool | None = None,
                   mouse_speed: float | None = None, mouse_speed_x: float | None = None, mouse_speed_y: float | None = None, gamepad_gain: float | None = None) -> dict:
        with self._lock:
            if mode is not None:
                if mode not in {"mouse", "gamepad"}:
                    raise ValueError("mode must be mouse or gamepad")
                if mode != self.mode:
                    self._zero_locked()
                    self.mode = mode
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
                if self.mode == "mouse":
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
        names = tuple(sorted(set().union(*self._button_sources.values()))) if self.enabled else ()
        if names or self._pad is not None:
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
        desired = set().union(*self._keyboard_sources.values()) if self.enabled and self._keyboard_sources else set()
        current = set(self.keyboard.pressed)
        for key in sorted(current - desired, reverse=True):
            self.keyboard.set_key(key, False)
        for key in sorted(desired - current):
            self.keyboard.set_key(key, True)

    def _refresh_left_stick_locked(self) -> None:
        x = y = 0.0
        if self.enabled:
            for sx, sy in self._left_stick_sources.values():
                x += sx
                y += sy
        x = max(-1.0, min(1.0, x))
        y = max(-1.0, min(1.0, y))
        if (x or y) or self._pad is not None:
            self._ensure_pad().set_left_stick(x, y)

    def _refresh_triggers_locked(self) -> None:
        left = right = 0.0
        if self.enabled:
            for source_left, source_right in self._trigger_sources.values():
                left = max(left, source_left)
                right = max(right, source_right)
        if (left or right) or self._pad is not None:
            setter = getattr(self._ensure_pad(), "set_triggers", None)
            if setter is not None:
                setter(left, right)

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
                target = str(item.get("target", "")).strip().upper()
                if not ident or not target:
                    continue
                source = prefix + ident
                if action_type == "gamepad":
                    if target not in XUSB_GAMEPAD_BUTTONS:
                        raise ValueError(f"不支持的 Xbox 键：{target}")
                    self._button_sources[source] = {target}
                elif action_type == "keyboard":
                    self._keyboard_sources[source] = self._combo_keys(target)
                elif action_type == "gamepad_axis":
                    if target not in GAMEPAD_AXES:
                        raise ValueError(f"不支持的 Xbox 摇杆方向：{target}")
                    self._left_stick_sources[source] = GAMEPAD_AXES[target]
                else:
                    raise ValueError(f"不支持的持续输出类型：{action_type}")
            self.last_hold_update = time.monotonic()
            try:
                self._refresh_buttons_locked()
                self._refresh_keyboard_locked()
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
            self._left_stick_sources.pop(source, None)
            self._trigger_sources.pop(source, None)
            try:
                self._refresh_buttons_locked()
                self._refresh_keyboard_locked()
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
        button = str(button).upper()
        if button not in XUSB_GAMEPAD_BUTTONS:
            raise ValueError(f"不支持的 Xbox 键：{button}")
        source = str(source).strip() if source else f"voice-{time.monotonic_ns()}"
        with self._lock:
            if not self.enabled:
                return
            self._button_sources[source] = {button}
            self._refresh_buttons_locked()
        time.sleep(max(0.04, min(0.25, float(duration))))
        with self._lock:
            self._button_sources.pop(source, None)
            self._refresh_buttons_locked()

    def tap_keyboard(self, combo: str, duration: float = 0.06, source: str | None = None) -> None:
        source = str(source).strip() if source else f"voice-keyboard-{time.monotonic_ns()}"
        keys = self._combo_keys(combo)
        with self._lock:
            if not self.enabled:
                return
            self._keyboard_sources[source] = keys
            self._refresh_keyboard_locked()
        time.sleep(max(0.02, min(0.20, float(duration))))
        with self._lock:
            self._keyboard_sources.pop(source, None)
            self._refresh_keyboard_locked()

    def execute_action(self, action: dict) -> dict:
        if not isinstance(action, dict):
            raise ValueError("action must be an object")
        with self._lock:
            if not self.enabled:
                return {"executed": False, "reason": "output disabled"}
        action_type = str(action.get("type", "")).lower()
        target = str(action.get("target", "")).strip().upper()
        source = str(action.get("source", "")).strip() or None
        if action_type == "gamepad":
            self.tap_gamepad(target, source=source)
        elif action_type == "keyboard":
            self.tap_keyboard(target, source=source)
        else:
            raise ValueError(f"不支持的输出类型：{action_type}")
        return {"executed": True, "action": f"{action_type}:{target}"}

    def _zero_locked(self) -> None:
        self.last_value = 0.0
        self.last_x = 0.0
        self.last_y = 0.0
        self.last_buttons = ()
        self._button_sources = {"zones": set()}
        self._keyboard_sources = {}
        self._left_stick_sources = {}
        self._trigger_sources = {}
        self.keyboard.release_all()
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
                if self.enabled and self.mode == "gamepad" and self.last_update and now - self.last_update > 0.25:
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
                    prefixes = ("motions:",)
                    changed = False
                    for store in (self._button_sources, self._keyboard_sources, self._left_stick_sources):
                        for key in [k for k in store if k.startswith(prefixes)]:
                            store.pop(key, None); changed = True
                    if changed:
                        try:
                            self._refresh_buttons_locked()
                            self._refresh_keyboard_locked()
                            self._refresh_left_stick_locked()
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
            "last_value": round(self.last_value, 4),
            "last_x": round(self.last_x, 4),
            "last_y": round(self.last_y, 4),
            "buttons": list(self.last_buttons),
            "keyboard_holds": sorted(set().union(*self._keyboard_sources.values())) if self._keyboard_sources else [],
            "left_stick_holds": list(self._left_stick_sources.keys()),
            "trigger_holds": list(self._trigger_sources.keys()),
            "last_error": self.last_error,
        }

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self.enabled = False
            self._zero_locked()
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

    def __init__(self, output: OutputManager) -> None:
        self.output = output
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
                        self.output.emergency_stop()
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
