"""飞鼠甩拳通知桥；设备端判动作，电脑端只验新鲜度并走统一绑定。"""
from __future__ import annotations

import asyncio
import copy
import re
import secrets
import struct
import threading
import time

SERVICE = "a38b0001-62da-4c13-9f07-908d8b921571"
STATUS = "a38b0004-62da-4c13-9f07-908d8b921571"
EVENT = "a38b0006-62da-4c13-9f07-908d8b921571"
CONFIG = "a38b0007-62da-4c13-9f07-908d8b921571"
DEFAULTS = {"enabled": False, "address": "", "strength_mg": 1800, "cooldown_ms": 500}
PACKET = struct.Struct("<BBHIIIHH")


def normalize_config(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("飞鼠设置包含不认识的项目")
    result = {**DEFAULTS, **raw}
    if type(result["enabled"]) is not bool:
        raise ValueError("拳击开关必须是开或关")
    address = str(result["address"]).strip().upper()
    if address and not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
        raise ValueError("请选择已配对的枪型飞鼠")
    result["address"] = address
    for key, low, high, label in (("strength_mg", 1300, 3000, "触发强度"),
                                   ("cooldown_ms", 250, 2000, "冷却时间")):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{label}应在 {low} 至 {high} 之间")
    return result


class EventGate:
    """配置代号、启动代号、序号和设备时钟共同防止旧事件补发。"""
    def __init__(self, epoch):
        self.epoch = epoch
        self.session = self.sequence = self.uptime = self.anchor_ms = None
        self.anchor_at = self.seen_at = None

    def accept(self, data, now):
        if len(data) != PACKET.size:
            return None
        version, kind, flags, session, seq, uptime, strength, epoch = PACKET.unpack(data)
        if version != 1 or kind not in (0, 1) or epoch != self.epoch:
            return None
        if self.session != session:
            if kind != 0:
                return None
            self.session, self.sequence, self.uptime = session, seq, uptime
            self.anchor_ms, self.anchor_at, self.seen_at = uptime, now, now
            return {"hit": False, "ready": flags & 3 == 3, "strength_mg": strength}
        step = (uptime - self.uptime) & 0xffffffff
        if step >= 0x80000000:
            return None
        elapsed = ((uptime - self.anchor_ms) & 0xffffffff) / 1000.0
        age = now - self.anchor_at - elapsed
        if age > .25 or age < -.15:
            # 迟到心跳也不能重设时钟，否则它后面的积压动作会被误当成新动作。
            # 真实当前帧可沿原时钟继续；持续失步则由心跳超时重连取得新配置代号。
            return None
        advance = (seq - self.sequence) & 0xffffffff
        if advance >= 0x80000000:
            return None
        self.sequence, self.uptime, self.seen_at = seq, uptime, now
        if kind == 0:
            # 沿已经验证的设备时间推进基点，长期运行也能跨越毫秒计数回绕。
            self.anchor_ms, self.anchor_at = uptime, self.anchor_at + elapsed
        return {"hit": kind == 1 and 0 < advance < 0x80000000 and flags & 3 == 3,
                "ready": flags & 3 == 3, "strength_mg": strength}


class GunMouseBridge:
    def __init__(self, kernel):
        self.kernel = kernel
        self._lock = threading.RLock()
        self._config_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._revision = 0
        self._state = "off"
        self._error = ""
        self._connected = False
        self._received = self._executed = self._dropped = 0
        self._last_strength = 0
        try:
            self._config = normalize_config(kernel.general_setting("gunmouse", {}))
        except ValueError as exc:
            self._config = dict(DEFAULTS)
            self._error = str(exc)

    def status(self):
        with self._lock:
            return {"config": dict(self._config), "state": self._state,
                    "connected": self._connected, "error": self._error,
                    "received": self._received, "executed": self._executed,
                    "dropped": self._dropped, "last_strength_mg": self._last_strength}

    def configure(self, updates):
        if not isinstance(updates, dict):
            raise ValueError("飞鼠设置必须是一组项目")
        with self._config_lock:
            return self._configure(updates)

    def _configure(self, updates):
        # 与其它实时偏好一致：先应用，保存失败由统一提示/限次补存处理。
        with self._lock:
            config = normalize_config({**self._config, **updates})
            self._config = config
            self._revision += 1
            self._error = ""
            self._state = "applying" if config["enabled"] and self._connected else self._state
        self.kernel.release_gunmouse_punch()
        self.kernel.remember_general_setting("gunmouse", copy.deepcopy(config))
        self.start()
        return self.status()

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="gunmouse", daemon=True)
                self._thread.start()

    def close(self):
        self._stop.set()
        self.kernel.release_gunmouse_punch()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self):
        asyncio.run(self._worker())

    async def _worker(self):
        while not self._stop.is_set():
            with self._lock:
                config, revision = dict(self._config), self._revision
            if not config["enabled"] or not config["address"]:
                with self._lock:
                    self._state = "off" if not config["enabled"] else "select_device"
                await asyncio.sleep(.25)
                continue
            try:
                await self._session(config, revision)
            except ImportError:
                with self._lock:
                    self._state, self._error = "unavailable", "未安装飞鼠蓝牙组件，请修复运行环境"
            except TimeoutError:
                with self._lock:
                    self._state, self._error = "disconnected", "飞鼠连接或心跳超时，请确认设备已开机并在电脑附近"
            except Exception as exc:
                with self._lock:
                    self._state, self._error = "disconnected", f"飞鼠连接未完成：{exc}"
            finally:
                self.kernel.release_gunmouse_punch()
                with self._lock:
                    self._connected = False
            # 断线低频重试，无扫描、无动作队列。
            for _ in range(20):
                if self._stop.is_set() or revision != self._revision:
                    break
                await asyncio.sleep(.25)

    async def _session(self, config, revision):
        import json
        from bleak import BleakClient
        from bleak.backends.device import BLEDevice
        epoch = secrets.randbelow(65535) + 1
        gate = EventGate(epoch)
        gone = asyncio.Event()
        loop = asyncio.get_running_loop()
        device = BLEDevice(config["address"], "GunMouse SuperMini", None)
        with self._lock:
            self._state = "connecting"
        async with BleakClient(device, pair=True, timeout=20,
                               disconnected_callback=lambda _: loop.call_soon_threadsafe(gone.set),
                               winrt={"use_cached_services": False}) as client:
            if self._stop.is_set() or revision != self._revision:
                return
            identity = json.loads(bytes(await client.read_gatt_char(STATUS)))
            if identity.get("project") != "gunmouse":
                raise ValueError("设备身份不匹配")
            if not client.services.get_characteristic(EVENT) or not client.services.get_characteristic(CONFIG):
                with self._lock:
                    self._state, self._error = "firmware_required", "当前固件尚无甩拳接口，需更新到3.3版；原鼠标仍可使用"
                while not self._stop.is_set() and revision == self._revision and not gone.is_set():
                    await asyncio.sleep(.25)
                return

            def notified(_, data):
                now = time.monotonic()
                with self._lock:
                    if self._stop.is_set() or revision != self._revision or gone.is_set():
                        return
                    packet = gate.accept(data, now)
                    if packet is None:
                        self._dropped += 1
                        return
                    self._state = "ready" if packet["ready"] else "calibrating"
                    if not packet["ready"]:
                        self.kernel.release_gunmouse_punch()
                    if packet["hit"]:
                        self._received += 1
                        self._last_strength = packet["strength_mg"]
                        result = self.kernel.fire_gunmouse_punch()
                        self._executed += bool(result.get("executed"))

            await client.start_notify(EVENT, notified)
            command = struct.pack("<BBHHH", 1, 1, config["strength_mg"], config["cooldown_ms"], epoch)
            await client.write_gatt_char(CONFIG, command, response=True)
            if bytes(await client.read_gatt_char(CONFIG)) != command:
                raise ValueError("飞鼠未确认本次参数")
            with self._lock:
                self._connected, self._error, self._state = True, "", "waiting"
            started = renewed = time.monotonic()
            try:
                while not self._stop.is_set() and not gone.is_set():
                    now = time.monotonic()
                    if revision != self._revision:
                        with self._lock:
                            next_config, next_revision = dict(self._config), self._revision
                        if not next_config["enabled"] or next_config["address"] != config["address"]:
                            break
                        # 强度/冷却就地更新，不为滑条重连鼠标；换代号隔离旧通知。
                        config, revision = next_config, next_revision
                        epoch = secrets.randbelow(65535) + 1
                        gate = EventGate(epoch)
                        command = struct.pack("<BBHHH", 1, 1, config["strength_mg"], config["cooldown_ms"], epoch)
                        await client.write_gatt_char(CONFIG, command, response=True)
                        if bytes(await client.read_gatt_char(CONFIG)) != command:
                            raise ValueError("飞鼠未确认本次参数")
                        started = renewed = now
                    if now - (gate.seen_at or started) > 3:
                        raise TimeoutError("未收到飞鼠心跳，已释放甩拳按键")
                    if now - renewed >= 5:
                        await client.write_gatt_char(CONFIG, command, response=True)
                        renewed = now
                    await asyncio.sleep(.1)
            finally:
                # 不依赖设备接受停用，电脑端先释放；断线后不尝试写入。
                self.kernel.release_gunmouse_punch()
                if client.is_connected:
                    await client.write_gatt_char(CONFIG, bytes([1, 0]) + command[2:], response=True)
                    await client.stop_notify(EVENT)

    @staticmethod
    def paired_devices():
        async def read():
            from winrt.windows.devices.bluetooth import BluetoothLEDevice
            from winrt.windows.devices.enumeration import DeviceInformation
            selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
            devices = await DeviceInformation.find_all_async(selector, None)
            result = []
            for item in devices:
                if "gunmouse" not in item.name.lower():
                    continue
                addresses = re.findall(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", item.id.lower())
                if addresses:
                    result.append({"address": addresses[-1].upper(), "name": item.name})
            return result
        return asyncio.run(read())
