"""按方向分配手部控制；每只手只维护一份握拳、起点和失联状态。"""

from __future__ import annotations

from motioncontrol.hand_mouse_control import (
    DEFAULT_CONFIG as HAND_DEFAULTS, HANDS, HandMouseController, merge_config,
)

AXES = {"horizontal": "x", "vertical": "y"}
DEFAULT_CONFIG = {key: value for key, value in HAND_DEFAULTS.items() if key != "hand"}
DEFAULT_CONFIG.update(horizontal_hand="right", vertical_hand="left")


def normalize_config(current: dict | None, updates: dict | None) -> dict:
    config = dict(DEFAULT_CONFIG)
    config.update(current or {})
    changes = dict(updates or {})
    # 旧的单手选择迁为水平方向，另一只手负责垂直；其他参数原样保留。
    legacy = changes.pop("hand", None)
    if legacy is not None:
        if legacy not in HANDS:
            raise ValueError("控制手只能选择左手或右手")
        changes.setdefault("horizontal_hand", legacy)
        changes.setdefault("vertical_hand", "left" if legacy == "right" else "right")
    for key in changes:
        if key not in DEFAULT_CONFIG:
            raise ValueError(f"未知手控设置：{key}")
    config.update(changes)
    for axis in AXES:
        if config[f"{axis}_hand"] not in (*HANDS, "off"):
            raise ValueError("控制手只能选择左手、右手或关闭")
    # 握拳阈值等共用原有校验，不复制识别规则。
    scalar = {key: value for key, value in config.items() if key in HAND_DEFAULTS}
    validated = merge_config(None, scalar)
    config.update({key: value for key, value in validated.items() if key != "hand"})
    return config


class AxisHandMouseController:
    def __init__(self):
        self.hands = {hand: HandMouseController() for hand in HANDS}
        self.config = dict(DEFAULT_CONFIG)
        self.configure({})

    @property
    def engaged(self):
        return any(controller.engaged for controller in self.hands.values())

    def reset(self, now=None):
        for controller in self.hands.values():
            controller.reset(now)

    def configure(self, updates):
        config = normalize_config(self.config, updates)
        scalar = {key: value for key, value in config.items() if key in HAND_DEFAULTS}
        for hand, controller in self.hands.items():
            assigned = hand in (config["horizontal_hand"], config["vertical_hand"])
            controller.configure({**scalar, "hand": hand,
                                  "enabled": config["enabled"] and assigned})
        self.config = config
        return self.status()

    def update(self, pose_map, now, hand_points=None):
        points = hand_points or {}
        for hand, controller in self.hands.items():
            controller.update(pose_map, now, points.get(hand))
        return self.status()

    def owns_hand(self, hand):
        return self.hands[hand].engaged

    def tracking_request(self):
        hands = list(dict.fromkeys(self.config[f"{axis}_hand"] for axis in AXES
                                   if self.config[f"{axis}_hand"] in HANDS))
        return {"enabled": bool(self.config["enabled"] and hands),
                "hand": hands[0] if hands else "right", "hands": hands}

    def compose_output(self, x, y, state=None):
        """只接管分配给手的方向；例如握拳上下时保留头部左右。"""
        state = self.status() if state is None else state
        if state["engaged"]:
            if self.config["horizontal_hand"] != "off":
                x = float(state["output_x"])
            if self.config["vertical_hand"] != "off":
                y = float(state["output_y"])
        return x, y

    def status(self):
        hands = {hand: controller.status() for hand, controller in self.hands.items()}
        axes = {}
        for axis, coordinate in AXES.items():
            hand = self.config[f"{axis}_hand"]
            if hand == "off":
                axes[axis] = {"hand": "off", "enabled": False, "engaged": False, "state": "disabled", "output": 0.0}
            else:
                axes[axis] = {**hands[hand], "output": hands[hand][f"output_{coordinate}"]}
        # 保留既有诊断字段；完整读数按手上报，不能把两只手的握拳读数混在一起。
        primary = hands[self.tracking_request()["hand"]]
        return {**primary, "enabled": bool(self.config["enabled"]),
                "engaged": self.engaged, "config": dict(self.config),
                "output_x": axes["horizontal"]["output"],
                "output_y": axes["vertical"]["output"],
                "axes": axes, "hands": hands}
