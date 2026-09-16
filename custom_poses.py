"""用户自己录的姿势：存取，和实时比对。

模板本身和比对算法在 ``motioncontrol_shared.pose_template`` 里（纯 stdlib，云端也
能导入）。这里是桌面这一侧：文件放哪、怎么原子写、每个姿势自己的阈值和停留时间，
以及把比对结果交给控制内核。

## 为什么用现成的 pose.* 通路

识别出来的姿势不走新路：它进 ``ControlKernel.pose_active``，和内置的 hands_cross
一样，然后由 ``_dispatch_controls_locked`` 按 ``pose.<id>`` 去查绑定。这意味着自定义
姿势自动获得了整套已有的东西——按游戏分别映射、映射冲突检查、绑定界面、云端同步、
紧急停止时一起松开。新开一条通路能省下的代码远不如要补回来的那些。

所以 id 必须和内置的不冲突，``_next_id`` 统一加 ``custom`` 前缀。

## 停留时间

内核的 ``_set_pose_debounced(ident, raw, on_frames, off_frames)`` 本来就是"连续
多少帧成立才算数"，也就是停留。所以这里只存帧数，判定交给内核，不另写一份计时器
——两份计时器迟早会对不上。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from motioncontrol_shared.pose_template import build_template, compare

SCHEMA = "motioncontrol.custom_poses.v1"

# 一个姿势要连续成立多少帧才触发。按 30fps 算，5 帧约 0.17 秒——足够滤掉挥手
# 路过时的一瞬间，又不至于让人觉得迟钝。
DEFAULT_DWELL_FRAMES = 5

# 默认阈值。同一个姿势握住时通常在 0.93 以上，换一个姿势在 0.80 以下（见
# tests/test_pose_template.py），0.88 落在中间偏严的位置：宁可让人多摆一下，
# 也不要在玩到一半时突然误触发一个键。
DEFAULT_THRESHOLD = 0.88

MAX_POSES = 24
MAX_NAME_CHARS = 20


class CustomPoseError(Exception):
    """带给用户看的中文消息。"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class CustomPoseStore:
    """``custom_poses.json`` 的读写。

    线程安全交给调用方：桌面这边所有配置写入都在 ``PROFILE_UPDATE_LOCK`` 下进行，
    这里再加一把锁只会多一个死锁的机会。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.poses: list[dict] = []
        self.last_error = ""
        self._load()

    # --- 读写 ---------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏文件不该让程序起不来
            self.last_error = f"自定义姿势读取失败：{exc}"
            return
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            self.last_error = "自定义姿势文件版本不认识，已忽略"
            return
        self.poses = [p for p in data.get("poses", []) if _valid(p)]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema": SCHEMA, "poses": self.poses},
                             ensure_ascii=False, indent=2)
        # 原子写：崩溃或断电时不会留下半个文件，而半个 JSON 会让下次启动直接
        # 丢掉所有已录的姿势。
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.path)

    # --- 增删改 -------------------------------------------------------------

    def _next_id(self) -> str:
        used = {p["id"] for p in self.poses}
        index = 1
        while f"custom{index}" in used:
            index += 1
        return f"custom{index}"

    def capture(self, pose_map: dict, name: str = "") -> dict:
        """把当前这一帧存成一个新姿势。"""
        if len(self.poses) >= MAX_POSES:
            raise CustomPoseError(f"自定义姿势最多 {MAX_POSES} 个，先删掉一些")
        template = build_template(pose_map)
        if template is None:
            raise CustomPoseError(
                "看不清你的身体。需要能同时看到两肩和两髋，"
                "站远一点让上半身完整入画再试。")

        clean = str(name).strip()[:MAX_NAME_CHARS]
        entry = {
            "id": self._next_id(),
            "name": clean or f"姿势 {len(self.poses) + 1}",
            "template": template,
            "threshold": DEFAULT_THRESHOLD,
            "dwell_frames": DEFAULT_DWELL_FRAMES,
            "enabled": True,
            "created_at": _now_iso(),
        }
        self.poses.append(entry)
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
            entry["dwell_frames"] = int(_clamp(changes["dwell_frames"], 1, 90, "停留帧数"))
        if "enabled" in changes:
            entry["enabled"] = bool(changes["enabled"])
        self._save()
        return entry

    def remove(self, pose_id: str) -> bool:
        before = len(self.poses)
        self.poses = [p for p in self.poses if p["id"] != pose_id]
        if len(self.poses) != before:
            self._save()
            return True
        return False

    def get(self, pose_id: str) -> dict:
        for entry in self.poses:
            if entry["id"] == pose_id:
                return entry
        raise CustomPoseError(f"找不到这个姿势：{pose_id}")

    # --- 比对 ---------------------------------------------------------------

    def evaluate(self, pose_map: dict) -> dict[str, dict]:
        """当前这一帧对上每个姿势的程度。

        返回 ``{id: {"score": 0~1, "hit": bool, "weakest": [...]}}``。``hit`` 只表示
        "这一帧够像了"，连续多少帧才算触发由内核的去抖决定——判定只应该有一处。
        """
        results: dict[str, dict] = {}
        for entry in self.poses:
            if not entry.get("enabled", True):
                continue
            result = compare(entry["template"], pose_map)
            if result is None:
                results[entry["id"]] = {"score": 0.0, "hit": False, "visible": False}
                continue
            results[entry["id"]] = {
                "score": result["score"],
                "hit": result["score"] >= float(entry.get("threshold", DEFAULT_THRESHOLD)),
                "visible": True,
                "segments": result["segments"],
            }
        return results

    def status(self) -> list[dict]:
        """给界面看的列表。不含模板本身——那是几十个浮点数，界面用不上。"""
        return [{
            "id": entry["id"],
            "name": entry["name"],
            "threshold": entry.get("threshold", DEFAULT_THRESHOLD),
            "dwell_frames": entry.get("dwell_frames", DEFAULT_DWELL_FRAMES),
            "enabled": bool(entry.get("enabled", True)),
            "created_at": entry.get("created_at", ""),
            "trigger": f"pose.{entry['id']}",
        } for entry in self.poses]


def _valid(entry) -> bool:
    return (isinstance(entry, dict)
            and isinstance(entry.get("id"), str) and entry["id"]
            and isinstance(entry.get("template"), dict))


def _clamp(value, low: float, high: float, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CustomPoseError(f"{label}必须是数字") from None
    if not low <= number <= high:
        raise CustomPoseError(f"{label}要在 {low} 和 {high} 之间")
    return number
