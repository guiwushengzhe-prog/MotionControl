import ctypes
import time

from output_backend import KeyboardOutput, OutputManager, XUSB_GAMEPAD_BUTTONS, _KEYINPUT


class Mouse:
    available=True; last_error=None
    def __init__(self): self.pressed=set(); self.wheels=[]
    def move(self,dx,dy=0): return True
    def set_button(self,button,pressed):
        if pressed:self.pressed.add(button)
        else:self.pressed.discard(button)
    def wheel(self,direction,notches=1): self.wheels.append((direction,notches))
    def release_all(self): self.pressed.clear()


class Keyboard:
    available=True; last_error=None
    def __init__(self): self.pressed=set()
    def set_key(self,key,pressed):
        if pressed:self.pressed.add(key)
        else:self.pressed.discard(key)
    def release_all(self): self.pressed.clear()


class Pad:
    def __init__(self): self.buttons=(); self.left_stick=(0,0); self.triggers=(0,0)
    def set_buttons(self,names): self.buttons=tuple(names)
    def set_left_stick(self,x,y=0): self.left_stick=(x,y)
    def set_right_stick(self,x,y=0): pass
    def set_triggers(self,l,r): self.triggers=(l,r)
    def reset(self): self.buttons=(); self.left_stick=(0,0); self.triggers=(0,0)
    def close(self): pass


class FakeUser32:
    def __init__(self): self.events=[]
    def MapVirtualKeyW(self,code,mode): return {ord('W'):0x11,0x26:0x48}.get(int(code),0x1e)
    def SendInput(self,count,event_ptr,size):
        event=ctypes.cast(event_ptr,ctypes.POINTER(_KEYINPUT)).contents
        self.events.append((event.ki.wVk,event.ki.wScan,event.ki.dwFlags))
        return 1


def manager(tmp_path):
    mouse=Mouse(); keyboard=Keyboard(); out=OutputManager(tmp_path,mouse=mouse,keyboard=keyboard); pad=Pad(); out._pad=pad; out.set_config(enabled=True)
    return out,mouse,keyboard,pad


def test_extended_xbox_buttons_include_stick_clicks():
    assert XUSB_GAMEPAD_BUTTONS["L3"] == 0x0040
    assert XUSB_GAMEPAD_BUTTONS["R3"] == 0x0080


def test_keyboard_uses_game_compatible_scan_codes():
    user32=FakeUser32(); keyboard=KeyboardOutput(user32=user32)
    keyboard.set_key('W',True); keyboard.set_key('W',False)
    keyboard.set_key('UP',True); keyboard.set_key('UP',False)
    assert user32.events[0] == (0,0x11,KeyboardOutput.KEYEVENTF_SCANCODE)
    assert user32.events[1] == (0,0x11,KeyboardOutput.KEYEVENTF_SCANCODE|KeyboardOutput.KEYEVENTF_KEYUP)
    assert user32.events[2] == (0,0x48,KeyboardOutput.KEYEVENTF_SCANCODE|KeyboardOutput.KEYEVENTF_EXTENDEDKEY)
    assert user32.events[3] == (0,0x48,KeyboardOutput.KEYEVENTF_SCANCODE|KeyboardOutput.KEYEVENTF_EXTENDEDKEY|KeyboardOutput.KEYEVENTF_KEYUP)


def test_unified_holds_drive_mouse_trigger_axis_and_button(tmp_path):
    out,mouse,_,pad=manager(tmp_path)
    try:
        out.set_action_holds([
            {"id":"zone.a","action":{"type":"mouse_button","target":"X1"}},
            {"id":"zone.b","action":{"type":"gamepad_trigger","target":"RT"}},
            {"id":"motion.march","action":{"type":"gamepad_axis","target":"LS_UP"}},
            {"id":"zone.c","action":{"type":"gamepad","target":"L3"}},
        ])
        assert mouse.pressed == {"X1"}
        assert pad.triggers == (0.0,1.0)
        assert pad.left_stick == (0.0,1.0)
        assert "L3" in pad.buttons
        out.set_action_holds([])
        assert not mouse.pressed and pad.triggers==(0.0,0.0) and pad.left_stick==(0.0,0.0) and "L3" not in pad.buttons
    finally: out.close()


def test_unified_holds_drive_keyboard_mapping(tmp_path):
    out,_,keyboard,_=manager(tmp_path)
    try:
        out.set_action_holds([{"id":"zone.a","action":{"type":"keyboard","target":"W"}}])
        assert keyboard.pressed == {"W"}
        out.set_action_holds([])
        assert keyboard.pressed == set()
    finally: out.close()


def test_wheel_is_single_impulse_and_cannot_be_hold(tmp_path):
    out,mouse,_,_=manager(tmp_path)
    try:
        result=out.execute_action({"type":"mouse_wheel","target":"SCROLL_UP"})
        assert result["executed"] is True and mouse.wheels==[("SCROLL_UP",1)]
        try:
            out.set_action_holds([{"id":"bad","action":{"type":"mouse_wheel","target":"SCROLL_UP"}}])
        except ValueError:
            pass
        else:
            raise AssertionError("wheel hold must be rejected")
    finally: out.close()


def test_nonblocking_pose_tap_releases_later(tmp_path):
    out,_,keyboard,_=manager(tmp_path)
    try:
        started=time.monotonic(); out.execute_action({"type":"keyboard","target":"SPACE","duration":.04,"nonblocking":True}); elapsed=time.monotonic()-started
        assert elapsed < .025 and "SPACE" in keyboard.pressed
        time.sleep(.08)
        assert "SPACE" not in keyboard.pressed
    finally: out.close()


def test_gamepad_combo_may_mix_buttons_and_stick(tmp_path):
    """Buttons and the stick are separate pad channels, so one combo drives both.

    "LB+LS_UP" used to be rejected outright: the validator only accepted button
    names, which made a bumper-plus-direction binding impossible to express even
    though the output side has always held the two independently.
    """
    out, _mouse, _keyboard, pad = manager(tmp_path)
    try:
        out.set_holds([{'id': 'hands_cross', 'type': 'gamepad', 'target': ['LB', 'LS_UP']}])
        assert pad.buttons == ('LB',)
        assert pad.left_stick == (0.0, 1.0)

        # Directions sum across a combo and across triggers, exactly as separate
        # axis holds already did.
        out.set_holds([
            {'id': 'hands_cross', 'type': 'gamepad', 'target': ['LB', 'LS_UP']},
            {'id': 'squat', 'type': 'gamepad', 'target': 'A+LS_LEFT'},
        ])
        assert sorted(pad.buttons) == ['A', 'LB']
        assert pad.left_stick == (-1.0, 1.0)

        out.set_holds([])
        assert pad.buttons == ()
        assert pad.left_stick == (0.0, 0.0)
    finally:
        out.close()


def test_voice_hold_and_release_cover_both_halves_of_a_mixed_combo(tmp_path):
    out, _mouse, _keyboard, pad = manager(tmp_path)
    try:
        out.execute_voice_action({'type': 'gamepad', 'target': 'LB+LS_UP', 'behavior': 'hold', 'source': 'voice'})
        assert pad.buttons == ('LB',)
        assert pad.left_stick == (0.0, 1.0)
        out.execute_voice_action({'type': 'gamepad', 'target': 'LB+LS_UP', 'behavior': 'release', 'source': 'voice'})
        assert pad.buttons == ()
        assert pad.left_stick == (0.0, 0.0)
    finally:
        out.close()


def test_a_lone_direction_still_belongs_to_the_axis_type():
    import pytest
    from game_profiles import normalize_action
    with pytest.raises(ValueError):
        normalize_action({'type': 'gamepad', 'target': 'LS_UP'})
    with pytest.raises(ValueError):
        normalize_action({'type': 'gamepad', 'target': 'LB+NOPE'})
