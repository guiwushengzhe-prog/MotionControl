import time

from motioncontrol.output_backend import OutputManager


class FakeReader:
    USER_SLOTS = (0, 1, 2, 3)
    available = True
    backend_name = "fake-xinput"
    last_error = None

    def __init__(self, state=None):
        self.state = state

    def read(self, user):
        if int(user) != 0 or self.state is None:
            return None
        return dict(self.state)


class Pad:
    def __init__(self):
        self.buttons = ()
        self.left_stick = (0.0, 0.0)
        self.right_stick = (0.0, 0.0)
        self.triggers = (0.0, 0.0)

    def xinput_user_index(self):
        return 3

    def set_merged_report(self, state, names, motion_left=(0.0, 0.0)):
        self.set_buttons(names)
        self.left_stick = tuple(max(-1.0, min(1.0, state.get(key, 0) + motion))
                                for key, motion in zip(('left_x', 'left_y'), motion_left))
        self.right_stick = (state.get('right_x', 0), state.get('right_y', 0))
        self.triggers = (state.get('left_trigger', 0), state.get('right_trigger', 0))

    def set_buttons(self, names):
        self.buttons = tuple(sorted(names))

    def set_left_stick(self, x, y=0.0):
        self.left_stick = (float(x), float(y))

    def set_right_stick(self, x, y=0.0):
        self.right_stick = (float(x), float(y))

    def set_right_stick_raw(self, x, y=0.0):
        self.right_stick = (float(x), float(y))

    def set_triggers(self, left, right):
        self.triggers = (float(left), float(right))

    def reset(self):
        self.buttons = ()
        self.left_stick = (0.0, 0.0)
        self.right_stick = (0.0, 0.0)
        self.triggers = (0.0, 0.0)

    def close(self):
        pass


def _state(**overrides):
    value = {
        "user_index": 0,
        "packet_number": 1,
        "buttons": {"A", "DPAD_LEFT", "R3"},
        "left_trigger": 0.6,
        "right_trigger": 0.8,
        "left_x": 0.2,
        "left_y": -0.3,
        "right_x": -0.4,
        "right_y": 0.5,
    }
    value.update(overrides)
    return value


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _manager(tmp_path, reader):
    out = OutputManager(tmp_path, xinput_reader=reader)
    out._pad = Pad()
    out.configure_xinput_merge(enabled=True, user=0)
    return out


def test_physical_xinput_full_report_is_forwarded(tmp_path):
    reader = FakeReader(_state())
    out = _manager(tmp_path, reader)
    try:
        assert _wait_until(lambda: out.xinput_status()["connected"])
        assert set(out._pad.buttons) == {"A", "DPAD_LEFT", "R3"}
        assert out._pad.left_stick == (0.2, -0.3)
        assert out._pad.right_stick == (-0.4, 0.5)
        assert out._pad.triggers == (0.6, 0.8)
    finally:
        out.close()


def test_motion_button_or_and_emergency_stop_preserve_physical(tmp_path):
    reader = FakeReader(_state(buttons={"A"}))
    out = _manager(tmp_path, reader)
    try:
        assert _wait_until(lambda: out.xinput_status()["connected"])
        out.set_config(enabled=True)
        out.set_buttons({"A", "B"}, source="zones")
        assert set(out._pad.buttons) == {"A", "B"}
        out.set_buttons(set(), source="zones")
        assert set(out._pad.buttons) == {"A"}
        out.set_buttons({"B"}, source="zones")
        out.emergency_stop()
        assert set(out._pad.buttons) == {"A"}
        assert out.status()["enabled"] is False
    finally:
        out.close()


def test_motion_chord_releases_without_releasing_physical_same_button(tmp_path):
    reader = FakeReader(_state(buttons={"A"}))
    out = _manager(tmp_path, reader)
    try:
        assert _wait_until(lambda: out.xinput_status()["connected"])
        out.set_config(enabled=True)
        out.set_action_holds([{"id": "pose.spell", "action": {"type": "gamepad", "target": "LB+A"}}])
        assert set(out._pad.buttons) == {"A", "LB"}
        out.set_action_holds([])
        assert set(out._pad.buttons) == {"A"}
        reader.state = None
        assert _wait_until(lambda: not out.xinput_status()["connected"])
        assert set(out._pad.buttons) == set()
    finally:
        out.close()


def test_motion_apply_cannot_overwrite_physical_right_stick(tmp_path):
    reader = FakeReader(_state(right_x=0.25, right_y=-0.75))
    out = _manager(tmp_path, reader)
    try:
        assert _wait_until(lambda: out.xinput_status()["connected"])
        out.set_config(enabled=True)
        out.apply(1.0, 1.0)
        assert out._pad.right_stick == (0.25, -0.75)
    finally:
        out.close()


def test_raw_report_preserves_every_axis_bit_and_atomic_chord():
    import ctypes
    from motioncontrol.output_backend import VX360Gamepad, XINPUT_GAMEPAD, XUSB_GAMEPAD_BUTTONS
    pad = object.__new__(VX360Gamepad)
    frames = []
    pad.update = lambda: frames.append(bytes(pad.report))
    raw = XINPUT_GAMEPAD(0x1001, 17, 254, -32768, 32767, -12345, 12345)
    pad.set_merged_report({"raw_report": bytes(raw)}, {"A", "LB"})
    assert len(frames) == 1
    assert frames[0][2:] == bytes(raw)[2:]
    assert pad.report.wButtons == (raw.wButtons | XUSB_GAMEPAD_BUTTONS["LB"])
    assert ctypes.sizeof(raw) == 12


def test_pose_watchdog_clears_motion_only(tmp_path):
    out = _manager(tmp_path, FakeReader(_state(buttons={"A"})))
    try:
        assert _wait_until(lambda: out.xinput_status()["connected"])
        out.set_config(enabled=True)
        out.set_buttons({"B"}, source="zones")
        out.set_action_holds([{"id":"spell", "action":{"type":"gamepad", "target":"LB+Y"}}])
        assert set(out._pad.buttons) == {"A", "B", "LB", "Y"}
        assert _wait_until(lambda: set(out._pad.buttons) == {"A"})
        assert out._pad.right_stick == (-0.4, 0.5)
        assert out.xinput_status()["connected"]
    finally:
        out.close()


def test_multiple_motion_sources_release_independently(tmp_path):
    out = _manager(tmp_path, FakeReader(_state(buttons={"A"})))
    try:
        assert _wait_until(lambda: out.xinput_status()["connected"])
        out.set_config(enabled=True)
        out.set_action_holds([{"id":"one", "action":{"type":"gamepad", "target":"LB+A"}}], source_group="zones")
        out.set_action_holds([{"id":"two", "action":{"type":"gamepad", "target":"LB+Y"}}], source_group="controls")
        out.set_action_holds([], source_group="zones")
        assert set(out._pad.buttons) == {"A", "LB", "Y"}
        out.set_action_holds([], source_group="controls")
        assert set(out._pad.buttons) == {"A"}
    finally:
        out.close()


def test_discovery_runs_before_enable_and_excludes_virtual(tmp_path):
    class AllSlots(FakeReader):
        def read(self, user):
            return _state(user_index=user)
    out = OutputManager(tmp_path, xinput_reader=AllSlots())
    out._pad = Pad()
    try:
        assert _wait_until(lambda: out.xinput_status()["connected_users"] == [0, 1, 2])
        assert not out.xinput_status()["enabled"]
        out.configure_xinput_merge(enabled=True, user=3)
        assert _wait_until(lambda: out.xinput_status()["last_poll_age_ms"] is not None)
        assert not out.xinput_status()["connected"]
    finally:
        out.close()


def test_merge_blocks_keyboard_mouse_and_motion_axes(tmp_path):
    out = _manager(tmp_path, FakeReader(_state()))
    try:
        assert _wait_until(lambda: out.xinput_status()["connected"])
        out.set_config(enabled=True)
        for kind, target in (("keyboard", "W"), ("mouse_button", "LEFT"), ("mouse_wheel", "SCROLL_UP"), ("gamepad_axis", "LS_UP"), ("gamepad_trigger", "RT")):
            assert not out.execute_action({"type":kind,"target":target})["executed"]
        out.set_action_holds([{"id":"move", "action":{"type":"gamepad_axis","target":"LS_UP"}}])
        assert out._pad.left_stick == (0.2, -0.3)
        assert out._pad.triggers == (0.6, 0.8)
        out.set_config(enabled=False)
        assert out._pad.right_stick == (-0.4, 0.5)
    finally:
        out.close()


def test_chord_normalization_survives_json_roundtrip():
    import json
    from motioncontrol_shared.profile_schema import normalize_action
    action = normalize_action({"type":"gamepad", "target":"LB+A"})
    restored = json.loads(json.dumps(action))
    assert OutputManager._gamepad_targets(restored["target"]) == {"LB", "A"}


def test_switch_to_mouse_disables_merge_and_clears_physical_state(tmp_path):
    out = _manager(tmp_path, FakeReader(_state()))
    try:
        assert _wait_until(lambda: out.xinput_status()["connected"])
        out.set_config(mode="mouse")
        assert not out.xinput_status()["enabled"]
        assert not out.xinput_status()["connected"]
        assert not out._pad.buttons
    finally:
        out.close()


def test_virtual_identity_uses_new_slot_instead_of_incorrect_driver_zero():
    from motioncontrol.output_backend import VX360Gamepad
    class Reader:
        def read(self, user):
            return {} if user in (0, 1) else None
    pad = object.__new__(VX360Gamepad)
    pad._identity_reader = Reader()
    pad._users_before_attach = {0}
    pad._identified_user = None
    assert pad.xinput_user_index() == 1


def test_virtual_identity_rejects_ambiguous_simultaneous_connections():
    import pytest
    from motioncontrol.output_backend import VX360Gamepad
    class Reader:
        def read(self, user):
            return {} if user in (0, 1, 2) else None
    pad = object.__new__(VX360Gamepad)
    pad._identity_reader = Reader()
    pad._users_before_attach = {0}
    pad._identified_user = None
    with pytest.raises(RuntimeError):
        pad.xinput_user_index()


class ReportPad(Pad):
    """使用生产整帧合成方法，模拟驱动提交而非单独写按钮/轴。"""
    def __init__(self):
        super().__init__()
        self.frames = []

    def set_merged_report(self, state, names, motion_left=(0.0, 0.0)):
        from motioncontrol.output_backend import VX360Gamepad
        VX360Gamepad.set_merged_report(self, state, names, motion_left)

    def update(self):
        from motioncontrol.output_backend import XUSB_GAMEPAD_BUTTONS
        self.frames.append(bytes(self.report))
        self.buttons = tuple(k for k, v in XUSB_GAMEPAD_BUTTONS.items() if self.report.wButtons & v)
        def axis(v):
            return v / (32768 if v < 0 else 32767)
        self.left_stick = (axis(self.report.sThumbLX), axis(self.report.sThumbLY))
        self.right_stick = (axis(self.report.sThumbRX), axis(self.report.sThumbRY))
        self.triggers = (self.report.bLeftTrigger / 255, self.report.bRightTrigger / 255)


def _report_manager(tmp_path, lx=0, ly=0):
    from motioncontrol.output_backend import XINPUT_GAMEPAD
    raw = bytes(XINPUT_GAMEPAD(0x1000, 17, 254, lx, ly, -12345, 23456))
    state = _state(raw_report=raw, buttons={'A'}, left_x=lx/(32768 if lx < 0 else 32767),
                   left_y=ly/(32768 if ly < 0 else 32767))
    out = OutputManager(tmp_path, xinput_reader=FakeReader(state))
    out._pad = ReportPad()
    out.configure_xinput_merge(enabled=True, user=0, motion_left_enabled=True)
    assert _wait_until(lambda: out.xinput_status()['connected'])
    out.set_config(enabled=True)
    return out, raw


def test_symmetric_axes_orthogonal_buttons_and_source_release(tmp_path):
    out, raw = _report_manager(tmp_path, lx=16384)
    try:
        out.set_action_holds([
            {'id':'move', 'action':{'type':'gamepad_axis','target':'LS_UP'}},
            {'id':'spell', 'action':{'type':'gamepad','target':'LB+A'}},
        ])
        assert out._pad.report.sThumbLX == 16384
        assert out._pad.report.sThumbLY == 32767
        assert set(out._pad.buttons) == {'A','LB'}
        assert out._pad.frames[-1][2:4] == raw[2:4]
        assert out._pad.frames[-1][8:] == raw[8:]
        assert out._xinput_state['raw_report'] == raw
        out.set_holds([{'id':'second', 'type':'gamepad_axis','target':'LS_UP'}])
        out.set_action_holds([])
        assert out._pad.report.sThumbLY == 32767
        assert set(out._pad.buttons) == {'A'}
        out.set_holds([])
        assert out._pad.frames[-1] == raw
    finally:
        out.close()


def test_symmetric_opposite_partial_and_same_direction(tmp_path):
    out, raw = _report_manager(tmp_path, lx=-16384, ly=-32768)
    try:
        out.set_holds([{'id':'x','type':'gamepad_axis','target':'LS_RIGHT'},
                       {'id':'y','type':'gamepad_axis','target':'LS_UP'}])
        assert out._pad.report.sThumbLX == 16384  # -0.5 + 1 = 0.5
        assert out._pad.report.sThumbLY == 0
        out.set_holds([{'id':'y','type':'gamepad_axis','target':'LS_DOWN'}])
        assert out._pad.report.sThumbLX == -16384
        assert out._pad.report.sThumbLY == -32768
        out.set_holds([])
        assert out._pad.frames[-1] == raw
    finally:
        out.close()


def test_symmetric_positive_limit_and_motion_only(tmp_path):
    out, raw = _report_manager(tmp_path, lx=24000)
    try:
        out.set_holds([{'id':'x','type':'gamepad_axis','target':'LS_RIGHT'}])
        assert out._pad.report.sThumbLX == 32767
        out._xinput_reader.state = None
        assert _wait_until(lambda: not out.xinput_status()['connected'])
        assert out._pad.report.sThumbLX == 32767
        out.set_holds([])
        assert out._pad.report.sThumbLX == 0
    finally:
        out.close()


def test_symmetric_discrete_axis_refreshes_full_report_and_releases(tmp_path):
    out, raw = _report_manager(tmp_path, lx=-32768)
    try:
        out.set_buttons({'LB'}, source='spell')
        result = out.execute_action({'type':'gamepad_axis','target':'LS_UP', 'duration':0.2})
        assert result['executed']
        assert out._pad.report.sThumbLY == 32767
        assert out._pad.report.sThumbLX == -32768
        assert set(out._pad.buttons) == {'A','LB'}
        assert _wait_until(lambda: out._pad.report.sThumbLY == 0)
        assert set(out._pad.buttons) == {'A','LB'}
        out.clear_source('spell')
        assert out._pad.frames[-1] == raw
        for kind, target in [('keyboard','W'), ('mouse_button','LEFT'),
                             ('mouse_wheel','SCROLL_UP'), ('gamepad_trigger','RT')]:
            assert not out.execute_action({'type':kind,'target':target})['executed']
        out.apply(1, 1)
        assert out._pad.frames[-1] == raw
    finally:
        out.close()


def test_symmetric_stop_disable_watchdog_preserve_physical(tmp_path):
    out, raw = _report_manager(tmp_path, lx=-32768, ly=1)
    try:
        for stop in [out.emergency_stop, lambda:out.set_config(enabled=False)]:
            out.set_config(enabled=True)
            out.set_action_holds([{'id':'x','action':{'type':'gamepad_axis','target':'LS_RIGHT'}}])
            stop()
            assert out._pad.frames[-1] == raw
            assert not out._left_stick_sources
        out.set_config(enabled=True)
        out.set_action_holds([{'id':'x','action':{'type':'gamepad_axis','target':'LS_RIGHT'}}])
        assert _wait_until(lambda: out._pad.frames[-1] == raw)
        assert not out._left_stick_sources
    finally:
        out.close()


def test_symmetric_option_off_clears_sources_and_preserves_report(tmp_path):
    out, raw = _report_manager(tmp_path, lx=-32768, ly=1)
    try:
        out.set_holds([{'id':'x','type':'gamepad_axis','target':'LS_RIGHT'}])
        out.set_config(xinput_motion_left_enabled=False)
        assert out._pad.frames[-1] == raw
        assert not out._left_stick_sources
        assert not out.execute_action({'type':'gamepad_axis','target':'LS_UP'})['executed']
        out.set_holds([{'id':'x','type':'gamepad_axis','target':'LS_RIGHT'}])
        out.set_action_holds([{'id':'y','action':{'type':'gamepad_axis','target':'LS_UP'}}])
        assert out._pad.frames[-1] == raw
        assert not out._left_stick_sources
        out.configure_xinput_merge(motion_left_enabled=True)
        assert out._pad.frames[-1] == raw
        assert out.status()['xinput_motion_left_enabled']
        assert out.xinput_status()['motion_left_enabled']
    finally:
        out.close()


def test_ordinary_configuration_does_not_clear_left_axis(tmp_path):
    out = OutputManager(tmp_path, xinput_reader=FakeReader())
    out._pad = Pad()
    try:
        out.set_config(mode='gamepad', enabled=True)
        out.set_holds([{'id':'x','type':'gamepad_axis','target':'LS_RIGHT'}])
        out.set_config(xinput_merge_enabled=False, xinput_motion_left_enabled=False)
        assert out._pad.left_stick == (1.0, 0.0)
        assert out._left_stick_sources
        assert not out.xinput_status()['motion_left_enabled']
    finally:
        out.close()


def test_turning_the_merge_on_pulls_the_mode_back_to_gamepad(tmp_path):
    """默认输出已经是鼠标了，合流不能因此变成一个按了没反应的开关。

    合流按定义只有一份虚拟 Xbox 报文，鼠标模式下它无处可去。以前默认就是手柄，
    所以这条路从来没被走到过；现在默认是鼠标，开合流必须自己把模式带回去。
    """
    out = OutputManager(tmp_path, xinput_reader=FakeReader())
    out._pad = Pad()
    try:
        assert out.status()['mode'] == 'mouse'
        out.configure_xinput_merge(enabled=True, user=0)
        assert out.status()['mode'] == 'gamepad'
    finally:
        out.close()
