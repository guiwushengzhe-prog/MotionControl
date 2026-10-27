import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.gunmouse import EventGate, GunMouseBridge, PACKET, normalize_config
from motioncontrol_shared.profile_schema import normalize_overrides


def packet(kind=0, seq=0, ms=1000, session=10, epoch=7, flags=3):
    return PACKET.pack(1, kind, flags, session, seq, ms, 2000, epoch)


def test_only_fresh_confirmed_events_and_no_reconnect_replay():
    gate = EventGate(7)
    assert gate.accept(packet(1, 8), 10) is None
    assert not gate.accept(packet(seq=8), 10)['hit']
    assert gate.accept(packet(1, 9, 1100), 10.1)['hit']
    assert not gate.accept(packet(1, 9, 1100), 10.11)['hit']
    assert gate.accept(packet(1, 10, 1200), 11) is None  # 停顿后迟到
    assert gate.accept(packet(seq=10, ms=1200), 11) is None  # 积压心跳不能洗新后续旧动作
    assert gate.accept(packet(1, 11, 1210), 11.01) is None
    assert not gate.accept(packet(seq=10, ms=2000), 11)['hit']
    assert gate.accept(packet(1, 11, 2100), 11.1)['hit']
    assert gate.accept(packet(1, 12, 2200, epoch=6), 11.2) is None
    assert gate.accept(packet(1, 0, 50, session=11), 11.3) is None
    assert not gate.accept(packet(seq=0, ms=60, session=11), 11.31)['hit']
    assert gate.accept(packet(1, 1, 160, session=11), 11.41)['hit']
    assert gate.accept(b'bad', 12) is None


def test_event_counter_and_uptime_wrap_without_false_reset():
    gate = EventGate(7)
    gate.accept(packet(seq=0xffffffff, ms=0xfffffff0), 10)
    assert gate.accept(packet(1, 0, 84), 10.1)['hit']
    assert gate.accept(packet(1, 0xffffffff, 50), 10.2) is None
    assert not gate.accept(packet(1, 1, 184, flags=0), 10.2)['hit']


class Output:
    enabled = True
    def __init__(self):
        self.executed = []
        self.released = []
    def execute_action(self, action):
        self.executed.append(action)
        return {'executed': self.enabled}
    def clear_source(self, source):
        self.released.append(source)
    def active_voice_triggers(self):
        return set()
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_binding_master_switch_release_unbind_and_game_change():
    output = Output()
    kernel = ControlKernel(output, persist=False)
    try:
        assert not kernel.fire_gunmouse_punch()['executed']
        assert not kernel.recent_triggers
        assert 'motion.gunmouse_punch' in dict(kernel._intent_actions_locked())
        kernel.configure_bindings({'motions': {'gunmouse_punch': {'action': {'type': 'keyboard', 'target': 'F'}}}})
        output.enabled = False
        assert not kernel.fire_gunmouse_punch()['executed']
        output.enabled = True
        assert kernel.fire_gunmouse_punch()['executed']
        first = output.executed[-1]
        assert first['target'] == 'F' and first['nonblocking']
        assert first['behavior'] == 'tap'
        kernel.release_gunmouse_punch()
        assert first['source'] in output.released
        kernel.configure_bindings({'motions': {'gunmouse_punch': {'action': {'type': 'mouse_button', 'target': 'RIGHT'}}}})
        kernel.fire_gunmouse_punch()
        second = output.executed[-1]
        assert second['source'] != first['source']
        kernel.switch_bindings({'motions': {'gunmouse_punch': {'disabled': True}}})
        assert second['source'] in output.released
        assert not kernel.fire_gunmouse_punch()['executed']
    finally:
        kernel.close()


def test_config_uses_existing_persistence_and_rejects_partial_edits(monkeypatch):
    kernel = ControlKernel(Output())
    bridge = GunMouseBridge(kernel)
    monkeypatch.setattr(bridge, 'start', lambda: None)
    try:
        config = {'enabled': True, 'address': '12:34:56:78:90:AB', 'strength_mg': 1950, 'cooldown_ms': 650}
        bridge.configure(config)
        previous = bridge.status()['config']
        with pytest.raises(ValueError):
            bridge.configure({'strength_mg': 5000, 'enabled': False})
        assert bridge.status()['config'] == previous
        reloaded = ControlKernel(Output())
        try:
            assert GunMouseBridge(reloaded).status()['config'] == config
        finally:
            reloaded.close()
        assert kernel.settings_persistence_status()['state'] == 'ready'
    finally:
        bridge.close()
        kernel.close()


def test_failed_save_keeps_applied_config_and_common_retry_recovers(monkeypatch):
    from motioncontrol import settings_persistence
    kernel = ControlKernel(Output())
    bridge = GunMouseBridge(kernel)
    monkeypatch.setattr(bridge, 'start', lambda: None)
    try:
        with monkeypatch.context() as patch:
            def denied(*args, **kwargs):
                raise PermissionError('文件被占用')
            patch.setattr(settings_persistence, 'replace_documents', denied)
            bridge.configure({'cooldown_ms': 800})
            assert bridge.status()['config']['cooldown_ms'] == 800
            assert kernel.settings_persistence_status()['state'] != 'ready'
        assert kernel.retry_settings_save(force=True)['state'] == 'ready'
        reloaded = ControlKernel(Output())
        try:
            assert GunMouseBridge(reloaded).status()['config']['cooldown_ms'] == 800
        finally:
            reloaded.close()
    finally:
        bridge.close()
        kernel.close()


@pytest.mark.parametrize('value', [{'strength_mg': True}, {'cooldown_ms': 0}, {'enabled': 'yes'}, {'address': 'not-a-device'}, {'unknown': 1}])
def test_invalid_config(value):
    with pytest.raises(ValueError):
        normalize_config(value)


def test_game_binding_uses_shared_schema_and_is_always_a_single_tap():
    result = normalize_overrides({'motion.gunmouse_punch': {'action': {'type': 'mouse_button', 'target': 'LEFT', 'behavior': 'hold'}}})
    assert result['motion.gunmouse_punch']['action']['behavior'] == 'tap'
    with pytest.raises(ValueError, match='飞鼠甩拳'):
        normalize_overrides({'motion.gunmouse_punch': {'action': {'type': 'system', 'target': 'OUTPUT.START'}}})


@pytest.mark.parametrize('hot_update', [False, True])
def test_notification_session_is_event_driven_and_always_releases(monkeypatch, hot_update):
    from motioncontrol.gunmouse import CONFIG, EVENT, STATUS
    kernel = ControlKernel(Output(), persist=False)
    kernel.configure_bindings({'motions': {'gunmouse_punch': {'action': {'type': 'keyboard', 'target': 'F'}}}})
    bridge = GunMouseBridge(kernel)
    config = normalize_config({'enabled': True, 'address': '12:34:56:78:90:AB'})
    bridge._config = config
    monkeypatch.setattr(bridge, 'start', lambda: None)
    operations = []
    class Client:
        is_connected = True
        services = SimpleNamespace(get_characteristic=lambda _: object())
        reads = 0
        def __init__(self, *args, **kwargs):
            assert kwargs['winrt']['use_cached_services'] is False
            operations.append(('open',))
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.is_connected = False
        async def read_gatt_char(self, char):
            operations.append(('read', char))
            if char == STATUS:
                return json.dumps({'project': 'gunmouse'}).encode()
            epoch = int.from_bytes(self.command[6:], 'little')
            self.reads += 1
            self.callback(None, packet(epoch=epoch))
            self.callback(None, packet(1, 1, 1001, epoch=epoch))
            self.callback(None, packet(1, 1, 1001, epoch=epoch))
            if hot_update and self.reads == 1:
                asyncio.get_running_loop().call_soon(lambda: bridge.configure({'strength_mg': 2100}))
            else:
                bridge._stop.set()
            return self.command
        async def write_gatt_char(self, char, data, response):
            operations.append(('write', char, bytes(data)))
            self.command = bytes(data)
        async def start_notify(self, char, callback): self.callback = callback
        async def stop_notify(self, char): operations.append(('unsubscribe', char))
    monkeypatch.setitem(sys.modules, 'bleak', SimpleNamespace(BleakClient=Client))
    monkeypatch.setitem(sys.modules, 'bleak.backends', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'bleak.backends.device', SimpleNamespace(BLEDevice=lambda *a: object()))
    try:
        asyncio.run(bridge._session(config, 0))
        hits = 2 if hot_update else 1
        assert bridge.status()['received'] == bridge.status()['executed'] == hits
        assert len(kernel.output.executed) == hits
        assert kernel.output.executed[0]['source'] in kernel.output.released
        assert [op for op in operations if op[0] == 'read'] == [('read', STATUS)] + [('read', CONFIG)] * hits
        assert sum(op[0] == 'open' for op in operations) == 1
        assert operations[-2][2][1] == 0
        assert operations[-1] == ('unsubscribe', EVENT)
    finally:
        bridge.close()
        kernel.close()


@pytest.mark.parametrize('missing', ['status', 'event', 'config', 'disconnect'])
def test_partial_windows_discovery_retries_and_disconnect_never_stays_ready(monkeypatch, missing):
    from motioncontrol.gunmouse import CONFIG, EVENT, STATUS
    kernel = ControlKernel(Output(), persist=False)
    bridge = GunMouseBridge(kernel)
    config = normalize_config({'enabled': True, 'address': '12:34:56:78:90:AB'})
    bridge._config = config
    connections, reads = [], []
    class Client:
        is_connected = True
        def __init__(self, *args, **kwargs):
            self.number = len(connections)
            assert kwargs['winrt']['use_cached_services'] == (self.number == 1 and missing != 'disconnect')
            connections.append(self)
            self.disconnected = kwargs['disconnected_callback']
            absent = {'status': STATUS, 'event': EVENT, 'config': CONFIG}.get(missing)
            self.services = SimpleNamespace(get_characteristic=lambda char: None
                if self.number == 0 and char == absent else object())
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.is_connected = False
        async def read_gatt_char(self, char):
            reads.append((self.number, char))
            if char == STATUS:
                return json.dumps({'project': 'gunmouse', 'diag': {'build': '3.3.0'}}).encode()
            epoch = int.from_bytes(self.command[6:], 'little')
            self.callback(None, packet(epoch=epoch))
            if self.number == 0 and missing == 'disconnect':
                self.disconnected(self)
            else:
                bridge._stop.set()
            return self.command
        async def write_gatt_char(self, char, data, response): self.command = bytes(data)
        async def start_notify(self, char, callback): self.callback = callback
        async def stop_notify(self, char): pass
    sleep = asyncio.sleep
    observed = []
    async def quick_sleep(delay):
        if delay == .25:
            observed.append(bridge.status())
            assert not observed[-1]['connected']
            assert observed[-1]['state'] == 'disconnected'
            assert '重新连接' in observed[-1]['error']
        await sleep(0)
    monkeypatch.setattr(asyncio, 'sleep', quick_sleep)
    monkeypatch.setitem(sys.modules, 'bleak', SimpleNamespace(BleakClient=Client))
    monkeypatch.setitem(sys.modules, 'bleak.backends', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'bleak.backends.device', SimpleNamespace(BLEDevice=lambda *a: object()))
    try:
        asyncio.run(bridge._worker())
        assert len(connections) == 2
        assert observed
        if missing == 'status':
            assert (0, STATUS) not in reads  # 缺失接口时不再调用导致截图异常的读取。
        assert (1, CONFIG) in reads
        assert not bridge.status()['connected']
        assert bridge.status()['state'] != 'ready'
        assert bridge.status()['heartbeat_age_ms'] is None
        assert kernel.output.executed == []
    finally:
        bridge.close()
        kernel.close()
