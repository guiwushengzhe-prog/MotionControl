"""用户自己录的动作：存取，和实时比对。

一个动作由**一帧或多帧**关键姿势组成：

* 一帧 = 静态姿势。摆成那样并保持住，就一直触发（像按住一个键）。
* 多帧 = 连贯动作。按顺序依次做出每一帧，最后一帧完成时触发一下（像按一下键）。

比对算法在 ``motioncontrol_shared.pose_template`` 里（纯 stdlib，云端也能导入）。
这里是桌面这一侧：文件放哪、怎么原子写、每个动作的阈值和节奏，以及把结果交给控
制内核。

## 为什么走现成的 pose.* 通路

识别出来的动作进 ``ControlKernel.pose_active``，和内置的 hands_cross 一样，然后由
``_dispatch_controls_locked`` 按 ``pose.<id>`` 去查绑定。这意味着它自动获得了整套
已有的东西——按游戏分别映射、映射冲突检查、绑定界面、云端同步、紧急停止时一起
松开。新开一条通路能省下的代码远不如要补回来的那些。

所以 id 必须和内置的不冲突，``_next_id`` 统一加 ``custom`` 前缀。

## 判定为什么全在这里

早先的版本把"够不够像"放在这里、把"保持了几帧"交给内核的去抖，两边各管一半。
多帧动作一来这就站不住了：每一步都有自己的计时，还有步与步之间的超时，硬拆成
两处等于让两份状态互相猜对方到哪一步了。所以现在这里输出的 ``hit`` 就是最终判定，
内核只保留松开方向的去抖（少数几帧的抖动不至于让键闪断）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from motioncontrol_shared.pose_template import build_preview, build_template, compare

SCHEMA = "motioncontrol.custom_poses.v1"

# 一帧姿势要连续成立多少帧才算数。按 30fps 算，5 帧约 0.17 秒——足够滤掉挥手路过
# 时的一瞬间，又不至于让人觉得迟钝。
DEFAULT_DWELL_FRAMES = 5

# 默认阈值。同一个姿势握住时通常在 0.93 以上，换一个姿势在 0.80 以下（见
# tests/test_pose_template.py），0.88 落在中间偏严的位置：宁可让人多摆一下，也不要
# 在玩到一半时突然误触发一个键。
DEFAULT_THRESHOLD = 0.88

# 多帧动作里，从一步走到下一步最多给多久。2 秒足够从容做完一个动作，又短到不会把
# 十秒前偶然摆过的姿势算进来。
DEFAULT_STEP_WINDOW_S = 2.0

# 连贯动作完成时，触发状态保持多久。动作是"经过"不是"停住"，完成的那一瞬间人已经
# 在往下一个姿势去了；不保持一小段，绑定还没来得及产生一次真正的按键就结束了。
SEQUENCE_FIRE_HOLD_S = 0.20

MAX_POSES = 24
MAX_FRAMES = 6
MAX_NAME_CHARS = 20


class CustomPoseError(Exception):
    """带给用户看的中文消息。"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class CustomPoseStore:
    """``custom_poses.json`` 的读写，以及实时比对的状态机。

    线程安全交给调用方：桌面这边所有配置写入都在 ``PROFILE_UPDATE_LOCK`` 下进行，
    这里再加一把锁只会多一个死锁的机会。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.poses: list[dict] = []
        self.last_error = ""
        # 每个动作走到第几步了。纯运行时状态，不落盘——程序重启时人已经不在那个
        # 姿势上了，恢复一个半截的动作只会造成一次莫名其妙的触发。
        self._progress: dict[str, dict] = {}
        self._load()

    # --- 读写 ---------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏文件不该让程序起不来
            self.last_error = f"自定义动作读取失败：{exc}"
            return
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            self.last_error = "自定义动作文件版本不认识，已忽略"
            return
        self.poses = [entry for entry in map(_migrate, data.get("poses", [])) if entry]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema": SCHEMA, "poses": self.poses},
                             ensure_ascii=False, indent=2)
        # 原子写：崩溃或断电时不会留下半个文件，而半个 JSON 会让下次启动直接丢掉
        # 所有已录的动作。
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.path)

    # --- 增删改 -------------------------------------------------------------

    def _next_id(self) -> str:
        used = {entry["id"] for entry in self.poses}
        index = 1
        while f"custom{index}" in used:
            index += 1
        return f"custom{index}"

    @staticmethod
    def _frame_from(pose_map: dict) -> tuple[dict, dict | None]:
        template = build_template(pose_map)
        if template is None:
            raise CustomPoseError(
                "看不清你的身体。需要能同时看到两肩和两髋，"
                "站远一点让上半身完整入画再试。")
        return template, build_preview(pose_map)

    def capture(self, pose_map: dict, name: str = "") -> dict:
        """把当前这一帧存成一个新动作的第一帧。"""
        if len(self.poses) >= MAX_POSES:
            raise CustomPoseError(f"自定义动作最多 {MAX_POSES} 个，先删掉一些")
        template, preview = self._frame_from(pose_map)

        clean = str(name).strip()[:MAX_NAME_CHARS]
        entry = {
            "id": self._next_id(),
            "name": clean or f"动作 {len(self.poses) + 1}",
            # 一帧或多帧。单帧就是静态姿势，多帧就要按顺序依次做出来。
            "frames": [template],
            # 录下来那一瞬间的骨架，只用于显示。模板里只有方向向量，画不出人形，
            # 而用户要靠看图认出这是哪个动作——名字记不住那么多。
            "previews": [preview],
            "threshold": DEFAULT_THRESHOLD,
            "dwell_frames": DEFAULT_DWELL_FRAMES,
            "step_window_s": DEFAULT_STEP_WINDOW_S,
            "enabled": True,
            "created_at": _now_iso(),
        }
        self.poses.append(entry)
        self._save()
        return entry

    def append_frame(self, pose_id: str, pose_map: dict) -> dict:
        """给一个已有动作再加一帧，把它变成（或延长）连贯动作。"""
        entry = self.get(pose_id)
        if len(entry["frames"]) >= MAX_FRAMES:
            raise CustomPoseError(f"一个动作最多 {MAX_FRAMES} 个姿势")
        template, preview = self._frame_from(pose_map)
        entry["frames"].append(template)
        entry["previews"].append(preview)
        self._reset(pose_id)
        self._save()
        return entry

    def remove_frame(self, pose_id: str, index: int) -> dict:
        entry = self.get(pose_id)
        if len(entry["frames"]) <= 1:
            raise CustomPoseError("至少要留一个姿势。想删掉整个动作请用删除。")
        if not 0 <= index < len(entry["frames"]):
            raise CustomPoseError("没有这个姿势")
        entry["frames"].pop(index)
        entry["previews"].pop(index)
        self._reset(pose_id)
        self._save()
        return entry

    def update(self, pose_id: str, **changes) -> dict:
        entry = self.get(pose_id)
        if "name" in changes:
            clean = str(changes["name"]).strip()[:MAX_NAME_CHARS]
            if not clean:
                raise CustomPoseError("名字不能为空")
            entry["name"] = clean
        if "threshold" in changes:
            entry["threshold"] = _clamp(changes["threshold"], 0.50, 0.999, "相似度阈值")
        if "dwell_frames" in changes:
            entry["dwell_frames"] = int(_clamp(changes["dwell_frames"], 1, 90, "保持时间"))
        if "step_window_s" in changes:
            entry["step_window_s"] = _clamp(changes["step_window_s"], 0.3, 10.0, "每步时限")
        if "enabled" in changes:
            entry["enabled"] = bool(changes["enabled"])
        # 这些值定义的就是"什么算触发"，改了它们之后还沿用旧的进度和计数，等于新设置
        # 要等下一次做完整个动作才生效——用户会以为没保存。
        self._reset(pose_id)
        self._save()
        return entry

    def remove(self, pose_id: str) -> bool:
        before = len(self.poses)
        self.poses = [entry for entry in self.poses if entry["id"] != pose_id]
        if len(self.poses) != before:
            self._progress.pop(pose_id, None)
            self._save()
            return True
        return False

    def get(self, pose_id: str) -> dict:
        for entry in self.poses:
            if entry["id"] == pose_id:
                return entry
        raise CustomPoseError(f"找不到这个动作：{pose_id}")

    # --- 比对 ---------------------------------------------------------------

    def _reset(self, pose_id: str) -> None:
        self._progress.pop(pose_id, None)

    def _state(self, pose_id: str) -> dict:
        state = self._progress.get(pose_id)
        if state is None:
            state = self._progress[pose_id] = {"step": 0, "held": 0,
                                               "deadline": 0.0, "fire_until": 0.0}
        return state

    def evaluate(self, pose_map: dict, now: float | None = None) -> dict[str, dict]:
        """当前这一帧对上每个动作的程度，以及它是否触发。

        返回 ``{id: {"score", "hit", "visible", "step", "steps"}}``。``hit`` 就是最终
        判定，调用方不需要再数帧——见模块开头"判定为什么全在这里"。
        """
        now = time.monotonic() if now is None else float(now)
        results: dict[str, dict] = {}
        for entry in self.poses:
            if not entry.get("enabled", True):
                self._reset(entry["id"])
                continue
            results[entry["id"]] = self._evaluate_one(entry, pose_map, now)
        return results

    def _evaluate_one(self, entry: dict, pose_map: dict, now: float) -> dict:
        frames = entry["frames"]
        state = self._state(entry["id"])
        threshold = float(entry.get("threshold", DEFAULT_THRESHOLD))
        multi = len(frames) > 1

        # 连贯动作完成后的保持窗口。人这时已经在往下一个姿势去了，所以不再比对，
        # 只是把触发状态维持住，好让绑定产生一次真正的按键。
        if multi and now < state["fire_until"]:
            return {"score": 1.0, "hit": True, "visible": True,
                    "step": 0, "steps": len(frames)}

        step = min(state["step"], len(frames) - 1)
        result = compare(frames[step], pose_map)
        if result is None:
            # 看不清：不推进也不清零。人走出画面一瞬间就把做了一半的动作作废，
            # 会让边缘位置根本做不成动作。超时那条会兜住真正的中断。
            if multi and state["step"] > 0 and now > state["deadline"]:
                state.update(step=0, held=0)
            return {"score": 0.0, "hit": False, "visible": False,
                    "step": state["step"], "steps": len(frames)}

        score = result["score"]
        if score < threshold:
            state["held"] = 0
            if multi and state["step"] > 0 and now > state["deadline"]:
                state["step"] = 0
            return {"score": score, "hit": False, "visible": True,
                    "step": state["step"], "steps": len(frames),
                    "segments": result["segments"]}

        # 够像了。
        if step < len(frames) - 1:
            # 中间帧：碰到就往下走，不要求保持——动作是经过，不是停住。
            state.update(step=step + 1, held=0, deadline=now + float(
                entry.get("step_window_s", DEFAULT_STEP_WINDOW_S)))
            return {"score": score, "hit": False, "visible": True,
                    "step": state["step"], "steps": len(frames),
                    "segments": result["segments"]}

        # 最后一帧：这里才要求保持够久。
        state["held"] += 1
        if state["held"] < int(entry.get("dwell_frames", DEFAULT_DWELL_FRAMES)):
            return {"score": score, "hit": False, "visible": True,
                    "step": state["step"], "steps": len(frames),
                    "segments": result["segments"]}

        if multi:
            # 连贯动作触发一下就回到起点，否则人保持在最后那个姿势不动会一直触发。
            state.update(step=0, held=0, fire_until=now + SEQUENCE_FIRE_HOLD_S)
        return {"score": score, "hit": True, "visible": True,
                "step": 0 if multi else state["step"], "steps": len(frames),
                "segments": result["segments"]}

    def status(self) -> list[dict]:
        """给界面看的列表。

        不含模板本身——那是十几个浮点数，界面用不上。但含 previews：那是用户认出
        "这是哪个动作"的唯一凭据，几百字节，值得发。
        """
        return [{
            "id": entry["id"],
            "name": entry["name"],
            "previews": entry.get("previews", []),
            "frames": len(entry["frames"]),
            "threshold": entry.get("threshold", DEFAULT_THRESHOLD),
            "dwell_frames": entry.get("dwell_frames", DEFAULT_DWELL_FRAMES),
            "step_window_s": entry.get("step_window_s", DEFAULT_STEP_WINDOW_S),
            "enabled": bool(entry.get("enabled", True)),
            "created_at": entry.get("created_at", ""),
            "trigger": f"pose.{entry['id']}",
            # 走到第几步了。界面靠它把当前那一帧高亮出来，用户才知道动作断在哪。
            "step": self._progress.get(entry["id"], {}).get("step", 0),
        } for entry in self.poses]


def _migrate(entry) -> dict | None:
    """读进来的一条，补成当前的形状。

    连贯动作之前，一条记录只有单数的 template/preview。那些文件还在用户目录里，
    所以读的时候补成一帧的 frames，而不是让它们变成读不出来的坏数据。
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
        return None
    if "frames" not in entry and isinstance(entry.get("template"), dict):
        entry["frames"] = [entry.pop("template")]
        entry["previews"] = [entry.pop("preview", None)]
    frames = entry.get("frames")
    if not isinstance(frames, list) or not frames or not all(
            isinstance(frame, dict) for frame in frames):
        return None
    previews = entry.get("previews")
    if not isinstance(previews, list) or len(previews) != len(frames):
        entry["previews"] = [None] * len(frames)
    return entry


def _clamp(value, low: float, high: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CustomPoseError(f"{label}必须是数字") from None
    if not low <= number <= high:
        raise CustomPoseError(f"{label}要在 {low} 和 {high} 之间")
    return number
