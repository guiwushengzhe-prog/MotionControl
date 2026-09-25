import math

from motioncontrol.hand_anchor import HandAnchorTracker, pose_with_hand_anchors
from motioncontrol.control_kernel import ControlKernel, RUNTIME_BODY_ZONES


def p(x, y, score=0.9, z=0.0):
    return {"x": x, "y": y, "z": z, "score": score}


def arm_pose(side="left", *, wrist=(0.40, 0.50, 0.9), fingers=None, wrist_score=None):
    fingers = fingers or {
        "thumb": (0.42, 0.51, 0.8),
        "index": (0.43, 0.50, 0.8),
        "pinky": (0.41, 0.53, 0.8),
    }
    names = {
        "wrist": f"{side}_wrist",
        "thumb": f"{side}_thumb",
        "index": f"{side}_index",
        "pinky": f"{side}_pinky",
        "elbow": f"{side}_elbow",
        "shoulder": f"{side}_shoulder",
    }
    result = {
        names["shoulder"]: p(0.28 if side == "left" else 0.72, 0.35),
        names["elbow"]: p(0.34 if side == "left" else 0.66, 0.43),
        names["wrist"]: p(wrist[0], wrist[1], wrist_score if wrist_score is not None else wrist[2]),
    }
    for role, value in fingers.items():
        result[names[role]] = p(value[0], value[1], value[2])
    return result


def test_anchor_is_wrist_primary_and_rejects_remote_finger_outlier():
    pose = arm_pose(fingers={
        "thumb": (0.42, 0.51, 0.8),
        "index": (0.43, 0.50, 0.8),
        "pinky": (0.95, 0.95, 0.95),
    })
    tracker = HandAnchorTracker("left")
    state = tracker.update(pose, now=0.0)
    assert state["valid"] is True
    assert state["source"] == "observed"
    assert state["point"]["x"] < 0.48
    assert state["support"] == 3


def test_anchor_can_use_finger_support_when_wrist_is_missing():
    pose = arm_pose(wrist=(0.40, 0.50, 0.05), wrist_score=0.05)
    tracker = HandAnchorTracker("left")
    state = tracker.update(pose, now=0.0)
    assert state["valid"] is True
    assert state["source"] == "observed"
    assert state["wrist_observed"] is False
    assert math.hypot(state["point"]["x"] - 0.42, state["point"]["y"] - 0.51) < 0.04


def test_missing_anchor_uses_forearm_fallback_then_expires():
    tracker = HandAnchorTracker("left", fallback_ttl=0.25)
    first = arm_pose()
    tracker.update(first, now=0.0)
    missing = arm_pose(wrist=(0.4, 0.5, 0.05), wrist_score=0.05, fingers={
        "thumb": (0.4, 0.5, 0.05),
        "index": (0.4, 0.5, 0.05),
        "pinky": (0.4, 0.5, 0.05),
    })
    fallback = tracker.update(missing, now=0.10)
    assert fallback["valid"] is True
    assert fallback["fallback"] is True
    assert fallback["source"] in {"forearm_fallback", "arm_fallback", "predicted"}
    expired = tracker.update(missing, now=0.30)
    assert expired["valid"] is False
    assert expired["source"] == "missing"


def test_recapture_is_step_limited_and_raw_wrist_is_not_used_when_anchor_missing():
    tracker = HandAnchorTracker("right")
    tracker.update(arm_pose("right", wrist=(0.60, 0.50, 0.9)), now=0.0)
    missing = arm_pose("right", wrist=(0.60, 0.50, 0.05), wrist_score=0.05, fingers={
        "thumb": (0.60, 0.50, 0.05),
        "index": (0.60, 0.50, 0.05),
        "pinky": (0.60, 0.50, 0.05),
    })
    tracker.update(missing, now=0.10)
    recaptured = tracker.update(arm_pose("right", wrist=(0.92, 0.50, 0.9)), now=0.13)
    assert recaptured["valid"] is True
    assert recaptured["point"]["x"] < 0.80
    adapted = pose_with_hand_anchors({"right_wrist": p(0.92, 0.50, 0.9)}, {"right": None})
    assert adapted["right_wrist"]["score"] == 0.0


class _Output:
    def __init__(self):
        self.buttons = []
        self.holds = []

    def set_buttons(self, buttons, **_kwargs):
        self.buttons = list(buttons)

    def set_holds(self, holds, **_kwargs):
        self.holds = list(holds)

    def apply(self, *_args, **_kwargs):
        pass


def test_kernel_zones_consume_anchor_when_wrist_is_missing(monkeypatch):
    output = _Output()
    kernel = ControlKernel(output)
    # 手区要待够 HAND_DWELL_S 才按，所以帧之间得真的隔开时间。
    clock = [0.0]
    monkeypatch.setattr("motioncontrol.control_kernel.time.monotonic", lambda: clock[0])
    try:
        kernel.configure_scene_layout({
            "zones": {"leftHandUpper": {"shape": "circle", "cx": 0.20, "cy": 0.20, "r": 0.08}},
            "vertical_look": {"enabled": False},
        })
        pose = arm_pose("left", wrist=(0.20, 0.20, 0.05), wrist_score=0.05, fingers={
            "thumb": (0.20, 0.20, 0.8),
            "index": (0.21, 0.20, 0.8),
            "pinky": (0.20, 0.21, 0.8),
        })
        for _ in range(4):
            clock[0] += 1 / 30
            state = kernel.handle_pose_map("hand-anchor-test", pose, width=640, height=480)
        assert state["hand_anchor_version"] == "hand-anchor-v1"
        assert state["hand_anchors"]["left"]["wrist_observed"] is False
        assert state["zones"]["leftHandUpper"]["pressed"] is True
        # leftHandUpper is a compatibility alias now: the runtime merged the
        # two left-hand areas into one "leftHand" zone, which carries its own
        # default button.  Read it from the source rather than pinning a
        # literal -- that default has already moved once (Y -> X) and the point
        # of this test is that the finger anchor produced a press at all.
        assert RUNTIME_BODY_ZONES["leftHand"]["button"] in state["buttons"]
    finally:
        kernel.close()
