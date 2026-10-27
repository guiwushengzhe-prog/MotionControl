"""飞鼠甩拳通知桥；设备端判动作，电脑端只验新鲜度并走统一绑定。"""
from __future__ import annotations

import asyncio
import copy
from concurrent.futures import Future, TimeoutError as FutureTimeout
import json
import logging
import queue
import re
import secrets
import struct
import threading
import time
from .gunmouse_management import CONTROL, DETAILS, command_plan, matches, validate_operation, validate_snapshot

SERVICE = "a38b0001-62da-4c13-9f07-908d8b921571"
STATUS = "a38b0004-62da-4c13-9f07-908d8b921571"
EVENT = "a38b0006-62da-4c13-9f07-908d8b921571"
CONFIG = "a38b0007-62da-4c13-9f07-908d8b921571"
DEFAULTS = {"enabled": False, "connection_enabled": False, "address": "", "strength_mg": 1800, "cooldown_ms": 500}
PACKET = struct.Struct("<BBHIIIHH")
PORTAL_ORIGIN = "https://guiwu-aware.icu"
PORTAL_ROUTES = frozenset({"/api/gunmouse/status", "/api/gunmouse/config", "/api/gunmouse/devices"})
logger = logging.getLogger(__name__)


class IncompleteServices(ConnectionError):
    """Windows 本次交给应用的接口表不完整，需要换方式重新发现。"""


def request_origin_allowed(origin, port):
    return origin is None or origin in {PORTAL_ORIGIN, f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def normalize_config(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("飞鼠设置包含不认识的项目")
    result = {**DEFAULTS, **raw}
    if type(result["enabled"]) is not bool or type(result["connection_enabled"]) is not bool:
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
        self._last_packet_at = None
        self._session_event = None
        self._requests = queue.Queue(maxsize=4)
        self._device_snapshot = None
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
                    "dropped": self._dropped, "last_strength_mg": self._last_strength,
                    "heartbeat_age_ms": round((time.monotonic() - self._last_packet_at) * 1000)
                    if self._connected and self._last_packet_at is not None else None,
                    "device": copy.deepcopy(self._device_snapshot)}

    def configure(self, updates):
        if not isinstance(updates, dict):
            raise ValueError("飞鼠设置必须是一组项目")
        with self._config_lock:
            return self._configure(updates)

    def _configure(self, updates):
        # 与其它实时偏好一致：先应用，保存失败由统一提示/限次补存处理。
        with self._lock:
            config = normalize_config({**self._config, **updates})
            if config["address"] != self._config["address"]:
                self._connected = False
                self._device_snapshot = None
                self._session_event = None
                self._fail_requests()
            self._config = config
            self._revision += 1
            self._error = ""
            if not (config["enabled"] or config["connection_enabled"]) or not config["address"]:
                self._connected = False
                self._session_event = None
                self._fail_requests()
                self._state = "select_device" if not config["address"] and (config["enabled"] or config["connection_enabled"]) else "off"
            else:
                self._state = "applying" if self._connected else "connecting"
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
        self._fail_requests()
        self.kernel.release_gunmouse_punch()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _run(self):
        asyncio.run(self._worker())

    def manage(self, action, value=None, *, timeout=7):
        validate_operation(action, value)
        future = Future()
        with self._lock:
            if not self._connected or self._stop.is_set():
                raise ValueError("请先连接设备管理")
            request = (future, action, value, self._session_event, time.monotonic() + timeout)
            try:
                self._requests.put_nowait(request)
            except queue.Full:
                raise ValueError("设备正在处理操作，请稍后再试") from None
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            raise ValueError("未收到设备确认，请刷新核对；本次操作不会自动重发") from None

    def _fail_requests(self):
        while True:
            try:
                future, *_ = self._requests.get_nowait()
            except queue.Empty:
                break
            if future.set_running_or_notify_cancel():
                future.set_exception(ValueError("设备连接已改变，未完成的操作已取消"))

    async def _read_device(self, client):
        snapshot = json.loads(bytes(await client.read_gatt_char(STATUS)))
        if snapshot.get("project") != "gunmouse" or not isinstance(snapshot.get("diag"), dict):
            raise ValueError("未读到完整的设备状态，请刷新重试")
        if client.services.get_characteristic(DETAILS):
            details = json.loads(bytes(await client.read_gatt_char(DETAILS)))
            if details.get("project") != "gunmouse" or details.get("schema") != 1:
                raise ValueError("设备扩展状态不完整，请刷新重试")
            snapshot["details"] = details
        validate_snapshot(snapshot)
        snapshot["read_at_ms"] = round(time.time() * 1000)
        return snapshot

    async def _service_request(self, client, session):
        try:
            future, action, value, requested_session, deadline = self._requests.get_nowait()
        except queue.Empty:
            return
        if not future.set_running_or_notify_cancel():
            return
        try:
            if (session is not requested_session or self._session_event is not session or session.is_set() or self._stop.is_set()
                    or time.monotonic() >= deadline):
                raise ValueError("设备连接已改变，操作已取消")
            async def perform():
                snapshot = await self._read_device(client)
                plans = command_plan(action, value, snapshot)
                for command, expected in plans:
                    if (future.cancelled() or session.is_set() or self._stop.is_set()
                            or self._session_event is not session):
                        raise ValueError("操作已取消，未继续发送")
                    await client.write_gatt_char(CONTROL, ("CMD " + command).encode(), response=True)
                    for _ in range(8):
                        await asyncio.sleep(.15)
                        snapshot = await self._read_device(client)
                        if matches(snapshot, expected):
                            break
                    else:
                        raise ValueError("命令已送达，但未收到一致回报；请刷新核对，不会自动重发")
                with self._lock:
                    if self._session_event is not session or session.is_set():
                        raise ValueError("设备已断开，请重新连接后刷新")
                    self._device_snapshot = snapshot
                return snapshot
            snapshot = await asyncio.wait_for(perform(), timeout=max(.01, deadline - time.monotonic()))
            if not future.done():
                future.set_result(snapshot)
        except Exception as exc:
            if not future.done():
                message = str(exc) if isinstance(exc, ValueError) else "设备操作中断，请刷新核对；不会自动重发"
                future.set_exception(ValueError(message))

    async def _management_only(self, client, config, revision, gone, *, old_firmware=False):
        with self._lock:
            self._connected = True
            self._state = "firmware_required" if old_firmware and config["enabled"] else "off"
        while not self._stop.is_set() and not gone.is_set() and revision == self._revision:
            await self._service_request(client, gone)
            await asyncio.sleep(.1)

    async def _worker(self):
        use_cached_services = False
        while not self._stop.is_set():
            with self._lock:
                config, revision = dict(self._config), self._revision
            if not (config["enabled"] or config["connection_enabled"]) or not config["address"]:
                with self._lock:
                    self._state = "select_device" if not config["address"] else "off"
                await asyncio.sleep(.25)
                continue
            try:
                await self._session(config, revision, use_cached_services=use_cached_services)
                use_cached_services = False
            except ImportError:
                with self._lock:
                    self._state, self._error = "unavailable", "未安装飞鼠蓝牙组件，请修复运行环境"
            except TimeoutError:
                with self._lock:
                    self._state, self._error = "disconnected", "飞鼠连接或心跳超时，正在自动重新连接；请确认设备已开机并在电脑附近"
            except IncompleteServices as exc:
                # 某些 Windows 会话本次直接发现不完整，但系统已保存的接口表可用。
                # 断开本次管理会话后低频重试，不清除配对、不轮询惯性数据。
                use_cached_services = not use_cached_services
                with self._lock:
                    self._state, self._error = "disconnected", str(exc)
            except Exception as exc:
                logger.warning("飞鼠蓝牙连接失败：%s", exc)
                with self._lock:
                    self._state, self._error = "disconnected", "飞鼠蓝牙连接未完成，正在自动重新连接；请保持设备开机"
            finally:
                self.kernel.release_gunmouse_punch()
                self._fail_requests()
                with self._lock:
                    self._connected = False
                    self._last_packet_at = None
                    self._device_snapshot = None
                    if self._state in {"ready", "waiting", "calibrating", "applying"}:
                        self._state, self._error = "disconnected", "飞鼠已断开，正在自动重新连接"
            # 断线低频重试，无扫描、无动作队列。
            for _ in range(20):
                if self._stop.is_set() or revision != self._revision:
                    break
                await asyncio.sleep(.25)

    async def _session(self, config, revision, *, use_cached_services=False):
        from bleak import BleakClient
        from bleak.backends.device import BLEDevice
        epoch = secrets.randbelow(65535) + 1
        gate = EventGate(epoch)
        gone = asyncio.Event()
        loop = asyncio.get_running_loop()
        def disconnected(_):
            def mark():
                gone.set()
                with self._lock:
                    if self._session_event is not gone:
                        return
                    self._connected, self._last_packet_at = False, None
                    self._device_snapshot = None
                    self._fail_requests()
                    if revision == self._revision and not self._stop.is_set():
                        self._state, self._error = "disconnected", "飞鼠已断开，正在自动重新连接"
                    self.kernel.release_gunmouse_punch()
            loop.call_soon_threadsafe(mark)
        device = BLEDevice(config["address"], "GunMouse SuperMini", None)
        with self._lock:
            self._state, self._error = "connecting", ""
            self._last_packet_at = None
            self._session_event = gone
        async with BleakClient(device, pair=True, timeout=20,
                               disconnected_callback=disconnected,
                               winrt={"use_cached_services": use_cached_services}) as client:
            if self._stop.is_set() or revision != self._revision:
                return
            if not client.services.get_characteristic(STATUS):
                raise IncompleteServices("飞鼠蓝牙接口尚未完整就绪，正在自动重新连接；请保持设备开机")
            identity = json.loads(bytes(await client.read_gatt_char(STATUS)))
            if identity.get("project") != "gunmouse":
                raise ValueError("设备身份不匹配")
            with self._lock:
                self._device_snapshot = {**identity, "read_at_ms": round(time.time() * 1000)}
            if not config["enabled"]:
                await self._management_only(client, config, revision, gone)
                return
            if not client.services.get_characteristic(EVENT) or not client.services.get_characteristic(CONFIG):
                build = re.search(r"\b(\d+)\.(\d+)\.(\d+)", str(identity.get("diag", {}).get("build", "")))
                if use_cached_services or build is None or tuple(map(int, build.groups())) >= (3, 3, 0):
                    raise IncompleteServices("飞鼠甩拳接口尚未完整就绪，正在自动重新连接；请保持设备开机")
                with self._lock:
                    self._state, self._error = "firmware_required", "当前固件尚无甩拳接口，需更新到3.3版；原鼠标仍可使用"
                await self._management_only(client, config, revision, gone, old_firmware=True)
                return

            def notified(_, data):
                now = time.monotonic()
                with self._lock:
                    if (self._stop.is_set() or revision != self._revision or gone.is_set()
                            or self._session_event is not gone):
                        return
                    packet = gate.accept(data, now)
                    if packet is None:
                        self._dropped += 1
                        return
                    self._last_packet_at = now
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
                if revision == self._revision and not gone.is_set():
                    self._connected, self._error = True, ""
                    if gate.seen_at is None:
                        self._state = "waiting"
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
                    await self._service_request(client, gone)
                    await asyncio.sleep(.1)
            finally:
                # 不依赖设备接受停用，电脑端先释放；断线后不尝试写入。
                self.kernel.release_gunmouse_punch()
                if client.is_connected and not gone.is_set():
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
