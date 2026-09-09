import time

from output_backend import OutputManager


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

    def set_merged_report(self, state, names):
        self.set_buttons(names)
        self.left_stick = (state.get('left_x', 0), state.get('left_y', 0))
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
    from output_backend import VX360Gamepad, XINPUT_GAMEPAD, XUSB_GAMEPAD_BUTTONS
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
    from game_profiles import normalize_action
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
