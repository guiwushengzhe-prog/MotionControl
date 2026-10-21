"""首步实验的独立模型/深度抬脚信号；显式选择来源，不自动回退。"""
from __future__ import annotations

import math
import statistics

from .depth_march import DepthMarchLifts
from .zone_fit import ankle_y, body_frame


class ExperimentMarchLifts:
    def __init__(self):
        self.reset()

    def reset(self):
        self.source = "model"
        self.lifts = None
        self.message = "请双脚入镜并自然站稳约 0.25 秒"
        self.sources = {name: {"baseline": None, "history": [], "last_at": None,
                               "identity": None, "ready": False, "reason": self.message, "lifts": None}
                        for name in ("model", "depth")}

    @staticmethod
    def _model_measure(pose, width, height):
        names = [f"{side}_{part}" for side in ("left", "right") for part in ("shoulder", "hip", "ankle")]
        if not pose or pose.get("position_source") == "kinect" or any(
                (pose.get(name) or {}).get("position_source") == "kinect" for name in names):
            return None
        try:
            frame = body_frame(pose, width, height)
            ankles = [ankle_y(pose, side) for side in ("left", "right")]
            if frame is None or None in ankles:
                return None
            torso = abs(frame["hip"]["y"] - frame["shoulder"]["y"])
            return (ankles[1] - ankles[0]) / torso if torso >= .025 else None
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    def update(self, image_pose, depth_body, width, height, now, source="model"):
        if source not in self.sources:
            raise ValueError("踏步来源必须为 model 或 depth")
        changed = source != self.source
        self.source = source
        measured = {"model": self._model_measure(image_pose, width, height),
                    "depth": DepthMarchLifts._measure(depth_body)}
        for name, state in self.sources.items():
            value = measured[name]
            identity = depth_body.get("tracking_id") if name == "depth" and depth_body else None
            stale = state["last_at"] is not None and not 0 < now - state["last_at"] <= .25
            if not math.isfinite(now) or stale or identity != state["identity"]:
                state.update(baseline=None, history=[], last_at=None, identity=identity)
            state.update(ready=False, lifts=None)
            if value is None or not math.isfinite(value) or not math.isfinite(now):
                state["history"] = []
                state["reason"] = ("请让肩、髋和双脚入镜，使用未融合深度的模型姿态" if name == "model"
                                   else "深度骨骼未就绪，请开启深度并让双脚进入取景范围")
                continue
            state["last_at"] = now
            if state["baseline"] is None:
                state["history"].append((now, value))
                state["history"] = [(t, v) for t, v in state["history"] if now - t <= .4]
                values = [v for _, v in state["history"]]
                stable = (len(values) >= 3 and now - state["history"][0][0] >= .25
                          and max(values) - min(values) <= .025)
                neutral = statistics.median(values)
                if stable and abs(neutral) < .05:
                    state["baseline"] = neutral
                else:
                    state["reason"] = "请双脚落地并自然站稳约 0.25 秒，正在记录踏步基准"
                    continue
            lift = value - state["baseline"]
            state.update(ready=True, reason="已就绪", lifts={"left": lift, "right": -lift})
        selected = self.sources[source]
        self.lifts = dict(selected["lifts"]) if selected["ready"] and not changed else None
        self.message = "已切换踏步来源，下一帧开始使用" if changed and selected["ready"] else selected["reason"]
        return self.lifts

    def status(self):
        return {"source": self.source, "ready": self.lifts is not None, "message": self.message,
                "lifts": dict(self.lifts) if self.lifts is not None else None,
                "sources": {name: {"ready": state["ready"], "reason": state["reason"],
                                   "lifts": dict(state["lifts"]) if state["lifts"] is not None else None}
                            for name, state in self.sources.items()}}
