"""Hand steering inside the control kernel: who owns the pointer, and the zones.

Two integration properties that the controller's own tests cannot cover,
because both are about how it shares the kernel with everything else:

* While the fist is closed the hand owns both mouse axes, taking them from head
  control and handing them straight back on release. Blending instead would
  make the pointer drift with the head while the player is trying to aim.

* The steering hand stops pressing its own zones. Without that, aiming across
  the screen mashes whatever buttons the hand flies through -- and the
  suppression has to take effect on the same frame the fist closes, which is
  why the controller updates at the top of the frame rather than in the head
  pass at the bottom.
"""

from __future__ import annotations

from control_kernel import ControlKernel


class _Output:
    """Records what the kernel pushes out, and stays out of the way."""

    enabled = True

    def __init__(self):
        self.applied = []
        self.holds = []
        self.buttons = []

    def apply(self, x, y=0.0):
        self.applied.append((round(float(x), 4), round(float(y), 4)))

    def set_action_holds(self, actions, source_group=None):
        self.holds.append(list(actions or []))

    def set_buttons(self, names):
        self.buttons.append(tuple(names or ()))

    def set_holds(self, actions, source_group=None):
        pass

    def clear_source(self, source):
        pass

    def status(self):
        return {}


def p(x, y, score=0.9):
    return {"x": x, "y": y, "z": 0.0, "score": score}


def body(*, hand="right", wrist=(0.75, 0.30), spread=0.5, forearm=0.18):
    """A whole-body pose with one hand's fingers at a chosen spread."""
    wx, wy = wrist
    tip = spread * forearm
    pose = {
        "nose": p(0.50, 0.12),
        "left_eye": p(0.47, 0.11), "right_eye": p(0.53, 0.11),
        "left_ear": p(0.44, 0.12), "right_ear": p(0.56, 0.12),
        "left_shoulder": p(0.40, 0.25), "right_shoulder": p(0.60, 0.25),
        "left_hip": p(0.43, 0.55), "right_hip": p(0.57, 0.55),
        "left_knee": p(0.43, 0.75), "right_knee": p(0.57, 0.75),
        "left_ankle": p(0.43, 0.92), "right_ankle": p(0.57, 0.92),
        "left_elbow": p(0.36, 0.40), "right_elbow": p(0.64, 0.40),
        "left_wrist": p(0.30, 0.30), "right_wrist": p(0.70, 0.30),
    }
    pose[f"{hand}_wrist"] = p(wx, wy)
    pose[f"{hand}_elbow"] = p(wx, wy + forearm)
    pose[f"{hand}_thumb"] = p(wx - tip, wy)
    pose[f"{hand}_index"] = p(wx, wy - tip)
    pose[f"{hand}_pinky"] = p(wx + tip, wy)
    return pose


def kernel_with_hand_mouse(**config):
    output = _Output()
    kernel = ControlKernel(output)
    kernel.hand_mouse_controller.configure({"enabled": True, "deadzone": 0.0, **config})
    return kernel, output


def feed(kernel, pose, frames=1):
    for _ in range(frames):
        kernel.handle_pose_map("hand-mouse-test", pose, width=640, height=480)


# --- output ownership -----------------------------------------------------


def test_closed_fist_drives_both_mouse_axes():
    kernel, output = kernel_with_hand_mouse(hand="right", sensitivity=100.0)
    feed(kernel, body(wrist=(0.70, 0.30), spread=0.2))   # closes, anchors here
    output.applied.clear()
    feed(kernel, body(wrist=(0.80, 0.40), spread=0.2))   # moves right and down

    assert output.applied, "the kernel must still be driving output"
    x, y = output.applied[-1]
    assert x > 0 and y > 0


def test_open_hand_leaves_the_pointer_to_head_control():
    kernel, output = kernel_with_hand_mouse(hand="right")
    feed(kernel, body(wrist=(0.70, 0.30), spread=0.7))
    assert kernel.hand_mouse_controller.engaged is False
    # Whatever head control decided is what went out; hand steering added
    # nothing of its own.
    assert kernel.head["hand_mouse"]["output_x"] == 0.0


def test_releasing_hands_the_pointer_straight_back():
    kernel, output = kernel_with_hand_mouse(hand="right", sensitivity=100.0)
    feed(kernel, body(wrist=(0.70, 0.30), spread=0.2))
    feed(kernel, body(wrist=(0.85, 0.45), spread=0.2))
    assert kernel.hand_mouse_controller.engaged is True

    feed(kernel, body(wrist=(0.85, 0.45), spread=0.8))   # open

    assert kernel.hand_mouse_controller.engaged is False
    assert kernel.head["hand_mouse"]["output_x"] == 0.0
    assert kernel.head["hand_mouse"]["output_y"] == 0.0


def test_status_is_exposed_for_the_ui():
    kernel, _output = kernel_with_hand_mouse(hand="left")
    feed(kernel, body(hand="left", wrist=(0.30, 0.30), spread=0.25))
    state = kernel.head["hand_mouse"]
    assert state["hand"] == "left"
    assert state["enabled"] is True
    # The live spread reading is what lets a user tune their own thresholds.
    assert state["spread"] is not None


# --- zone suppression -----------------------------------------------------


def test_steering_hand_stops_pressing_its_own_zones():
    kernel, _output = kernel_with_hand_mouse(hand="right")
    kernel.configure_bindings({"zones": {
        "rightHand": {"action": {"type": "gamepad", "target": "B"}},
    }})

    # Open hand parked in the zone: it presses, as it always did.
    feed(kernel, body(wrist=(0.75, 0.28), spread=0.7), frames=3)
    assert kernel.zone_state["rightHand"]["pressed"] is True

    # Same place, fist closed: steering now, so the button must let go.
    feed(kernel, body(wrist=(0.75, 0.28), spread=0.2), frames=3)
    assert kernel.zone_state["rightHand"]["pressed"] is False


def test_the_other_hand_keeps_its_zones():
    kernel, _output = kernel_with_hand_mouse(hand="right")
    kernel.configure_bindings({"zones": {
        "leftHand": {"action": {"type": "gamepad", "target": "Y"}},
    }})
    pose = body(hand="right", wrist=(0.75, 0.28), spread=0.2)
    pose["left_wrist"] = p(0.25, 0.28)   # parked in the left zone

    feed(kernel, pose, frames=3)

    assert kernel.hand_mouse_controller.engaged is True
    assert kernel.zone_state["leftHand"]["pressed"] is True


def test_feet_are_never_suppressed():
    """A bare startswith(hand) would also catch leftFoot/rightFoot."""
    kernel, _output = kernel_with_hand_mouse(hand="right")
    feed(kernel, body(wrist=(0.75, 0.28), spread=0.2))
    assert kernel.hand_mouse_controller.engaged is True
    assert kernel._hand_mouse_owns_zone("rightFoot") is False
    assert kernel._hand_mouse_owns_zone("leftFoot") is False
    assert kernel._hand_mouse_owns_zone("rightHand") is True


def test_look_gate_belongs_to_the_left_hand():
    """lookGate reads the left wrist, so left-hand steering has to release it."""
    kernel, _output = kernel_with_hand_mouse(hand="left")
    feed(kernel, body(hand="left", wrist=(0.30, 0.30), spread=0.2))
    assert kernel.hand_mouse_controller.engaged is True
    assert kernel._hand_mouse_owns_zone("lookGate") is True

    kernel.hand_mouse_controller.configure({"enabled": True, "hand": "right"})
    feed(kernel, body(hand="right", wrist=(0.70, 0.30), spread=0.2))
    assert kernel._hand_mouse_owns_zone("lookGate") is False


def test_nothing_is_suppressed_while_disabled():
    output = _Output()
    kernel = ControlKernel(output)
    kernel.configure_bindings({"zones": {
        "rightHand": {"action": {"type": "gamepad", "target": "B"}},
    }})
    feed(kernel, body(wrist=(0.75, 0.28), spread=0.1), frames=3)
    assert kernel.hand_mouse_controller.engaged is False
    assert kernel.zone_state["rightHand"]["pressed"] is True


# --- finger joints from the camera device ---------------------------------


def fingers(*, curl, wrist=(0.70, 0.30), reach=0.06):
    """21 个手部点，四根手指从手腕呈扇形张开，伸展倍数为 ``curl``。"""
    import math

    from hand_mouse_control import _FINGER_KNUCKLES, _FINGER_TIPS

    wx, wy = wrist
    points = [p(wx, wy) for _ in range(21)]
    for index, (knuckle, tip) in enumerate(zip(_FINGER_KNUCKLES, _FINGER_TIPS)):
        angle = math.radians(-90.0 + (index - 1.5) * 12.0)
        ux, uy = math.cos(angle), math.sin(angle)
        points[knuckle] = p(wx + ux * reach, wy + uy * reach)
        points[tip] = p(wx + ux * reach * curl, wy + uy * reach * curl)
    return points


def test_finger_joints_reach_the_fist_gate_through_the_kernel():
    """三个指尖读成握拳，手指关节说是摊开的——整条路走下来要听关节的。"""
    kernel, _ = kernel_with_hand_mouse(hand="right")
    kernel.handle_pose_map("hand-mouse-test", body(wrist=(0.70, 0.30), spread=0.1),
                           width=640, height=480,
                           hands={"right": fingers(curl=2.0)})
    status = kernel.hand_mouse_controller.status()
    assert status["engaged"] is False
    assert status["grip_source"] == "hand"


def test_the_kernel_only_reads_the_steering_hand():
    """左手摊开着不该影响右手控鼠标，否则两只手会互相顶。"""
    kernel, _ = kernel_with_hand_mouse(hand="right")
    kernel.handle_pose_map("hand-mouse-test", body(wrist=(0.70, 0.30), spread=0.1),
                           width=640, height=480,
                           hands={"left": fingers(curl=2.0, wrist=(0.30, 0.30))})
    status = kernel.hand_mouse_controller.status()
    assert status["grip_source"] == "pose"
    assert status["engaged"] is True


def test_losing_the_body_forgets_the_hands():
    """人走开之后留着上一帧的手会让下一次握拳从陈旧读数开始。"""
    kernel, _ = kernel_with_hand_mouse(hand="right")
    kernel.handle_pose_map("hand-mouse-test", body(spread=0.5), width=640, height=480,
                           hands={"right": fingers(curl=2.0)})
    assert kernel.latest_hands is not None
    kernel.handle_pose_map("another-source", body(spread=0.5), width=640, height=480)
    assert kernel.latest_hands is None
