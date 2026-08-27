import time

from output_backend import OutputManager, XUSB_GAMEPAD_BUTTONS


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


def manager(tmp_path):
    mouse=Mouse(); keyboard=Keyboard(); out=OutputManager(tmp_path,mouse=mouse,keyboard=keyboard); pad=Pad(); out._pad=pad; out.set_config(enabled=True)
    return out,mouse,keyboard,pad


def test_extended_xbox_buttons_include_stick_clicks():
    assert XUSB_GAMEPAD_BUTTONS["L3"] == 0x0040
    assert XUSB_GAMEPAD_BUTTONS["R3"] == 0x0080


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
