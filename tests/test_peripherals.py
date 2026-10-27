import asyncio
import copy
import json
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from motioncontrol.gunmouse import CONFIG, PACKET, GunMouseBridge, STATUS, normalize_config
from motioncontrol.gunmouse_management import CONTROL, DETAILS, command_plan, matches, validate_operation
from motioncontrol.peripherals import device_catalog


def device():
    return {'project': 'gunmouse', 'ota': 0,
            'diag': {'build': '3.3.0', 'invx': 0, 'invy': 1, 'tp': .035, 'tr': .03,
                     'base': 31000, 'noise': 25, 'raw': 31005, 'ready': 1, 'touch': 0},
            'details': {'project': 'gunmouse', 'schema': 1, 'seq': 7, 'cmd': '', 'always': 1,
                        'sens': 64, 'imu_ready': 1, 'power': {'mv': 3900, 'charge': -1},
                        'joy': {'enabled': 0, 'ready': 0, 'speed': 1200, 'rev': 2,
                                'raw': [0, 0], 'center': [2048, 2048]}}}


def test_existing_device_values_and_safe_touch_order_are_confirmed():
    before = device()
    assert command_plan('invert_x', False, before) == []
    plan = command_plan('touch', {'press': 2, 'release': 1.5}, before)
    assert [command for command, _ in plan] == ['tr 1.50', 'tp 2.00']
    actual = copy.deepcopy(before)
    actual['details']['seq'] = 8
    actual['diag']['tr'] = .015
    assert matches(actual, plan[0][1])
    actual['details']['seq'] = 9
    actual['diag']['tp'] = .02
    assert matches(actual, plan[1][1])
    actual['details']['seq'] = 8
    assert not matches(actual, plan[1][1])  # 目标相同但命令没有被设备接受不能称成功。
    with pytest.raises(ValueError, match='摇杆已停用'):
        command_plan('joystick_center', None, before)


@pytest.mark.parametrize('action,value', [('reset', None), ('sensitivity', True),
    ('sensitivity', 1000), ('joystick_speed', float('nan')), ('always', 1),
    ('touch', {'press': 2, 'release': 3}), ('touch', {'press': 3}), ([], None)])
def test_bad_management_commands_are_rejected_before_device_access(action, value):
    with pytest.raises(ValueError):
        validate_operation(action, value)


class Kernel:
    def __init__(self, enabled=False):
        self.config = normalize_config({'enabled': enabled, 'connection_enabled': True,
                                        'address': '12:34:56:78:90:AB'})
        self.keys = 0
    def general_setting(self, *args): return self.config
    def release_gunmouse_punch(self): pass
    def fire_gunmouse_punch(self): self.keys += 1; return {'executed': False}


@pytest.mark.parametrize('punch_enabled', [False, True])
def test_management_uses_one_connection_without_background_status_poll(monkeypatch, punch_enabled):
    import sys
    kernel = Kernel(punch_enabled)
    bridge = GunMouseBridge(kernel)
    snapshot, operations = device(), []
    class Client:
        is_connected = True
        services = SimpleNamespace(get_characteristic=lambda _: object())
        def __init__(self, *args, **kwargs): operations.append(('connect',))
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.is_connected = False
        async def read_gatt_char(self, char):
            operations.append(('read', char))
            if char == CONFIG:
                epoch = int.from_bytes(self.command[6:], 'little')
                self.callback(None, PACKET.pack(1, 0, 3, 5, 0, 1000, 0, epoch))
                return self.command
            return json.dumps(snapshot['details'] if char == DETAILS else snapshot).encode()
        async def write_gatt_char(self, char, data, response):
            if char == CONFIG:
                self.command = bytes(data)
                operations.append(('punch_config', data))
                return
            assert char == CONTROL
            operations.append(('command', data))
            assert data == b'CMD sens 96'
            snapshot['details'].update(sens=96, seq=8, cmd='sens')
        async def start_notify(self, char, callback): self.callback = callback
        async def stop_notify(self, char): pass
    monkeypatch.setitem(sys.modules, 'bleak', SimpleNamespace(BleakClient=Client))
    monkeypatch.setitem(sys.modules, 'bleak.backends', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'bleak.backends.device', SimpleNamespace(BLEDevice=lambda *a: object()))
    async def run():
        task = asyncio.create_task(bridge._worker())
        try:
            for _ in range(100):
                if bridge.status()['connected']: break
                await asyncio.sleep(.001)
            assert bridge.status()['connected']
            if not punch_enabled:
                assert operations == [('connect',), ('read', STATUS)]
            baseline = list(operations)
            await asyncio.sleep(.2)
            assert operations == baseline
            result = await asyncio.to_thread(bridge.manage, 'sensitivity', 96)
            assert result['details']['sens'] == 96
            count = len(operations)
            await asyncio.sleep(.2)
            assert len(operations) == count
            assert sum(op[0] == 'connect' for op in operations) == 1
            assert sum(op[0] == 'command' for op in operations) == 1
            assert kernel.keys == 0
        finally:
            bridge._stop.set()
            await asyncio.wait_for(task, 2)
    asyncio.run(run())


def test_old_or_expired_management_work_is_not_sent():
    bridge = GunMouseBridge(Kernel())
    old, new = asyncio.Event(), asyncio.Event()
    bridge._connected, bridge._session_event = True, new
    future = Future()
    bridge._requests.put_nowait((future, 'sensitivity', 96, old, float('inf')))
    asyncio.run(bridge._service_request(None, new))
    with pytest.raises(ValueError, match='取消'):
        future.result()
    with pytest.raises(ValueError, match='不会自动重发'):
        bridge.manage('sensitivity', 96, timeout=.001)
    asyncio.run(bridge._service_request(None, new))  # 过期任务不触碰蓝牙。
    assert bridge._requests.empty()
    bridge.close()


def test_catalog_only_describes_shared_existing_controls_and_is_not_mutable():
    catalog = device_catalog()
    assert {item['id'] for item in catalog['types']} == {'camera', 'gunmouse', 'phone', 'microphone'}
    assert len({item['id'] for item in catalog['types']}) == len(catalog['types'])
    assert all(item['target'] and item['add_control'] for item in catalog['types'])
    catalog['types'][0]['name'] = 'changed'
    assert device_catalog()['types'][0]['name'] == '摄像头'
