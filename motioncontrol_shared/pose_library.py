"""动作库：每个动作都有名字、怎么做、火柴人示范、星级，以及它会扫过哪些圈。

新手不用自己录，打开就能看到"这个动作长什么样"，照着做，直接绑键。

## 内置的两个，和从云端下载的

程序自带的只有**原地踏步**和**小腿向后抬起**。它俩要跟着一次抬脚从抬起看到落下
（抬到多高、膝盖动没动），是写在 control_kernel 里的代码，写在这里的只有给人看的
那部分。

别的动作都在云端的**官方动作库**里（仓库里的 cloud/official_poses/），用户下载了才有。
下载回来的是一份动作文件：名字、怎么做、示范、星级，外加**识别规则**——规则是数据，
由 pose_rules 解释，不是代码（为什么见 pose_rules 的说明）。没下载的动作在这台电脑
上看不到，也认不出来。

所以这里的"当前有哪些动作"是运行时决定的：内置两个，加上宿主登记进来的
（``register``）。电脑端登记本机装了的，云端登记全部官方动作。名字、会扫过哪些圈
这些查询都按这份登记来答。

## 星级

每个动作三项星级，都是 1~5 星：运动强度、识别度（在镜头前认得准不准）、上手难度。
另外按部位打星：腿部、臀部、核心、手臂、肩背，只列用得上的部位。星级是给人挑动作
用的，不影响识别。

## 会扫过哪些圈

``passes_zones`` 是做这个动作时身体会经过的跟随区域（真人录像里量出来的）。举双手时
手从两侧往上走，正好扫过两边的手区；这躲不开。两边都绑了键时，框的智能判定会把这
个动作当成「可能扫过」来防（见 zone_arbiter）。

这只是**没录过时的兜底**：用户在「录我的动作」里录过这个动作的话，扫没扫过、几次里
扫过几次，按他自己的录像对着现在的框算（intent_library），以录的为准。

``sweeps_first``（动作认出来之前就先扫过框）以前用来决定框要不要一律晚按，智能判定
不需要它了。字段还收着，因为已经签过名发出去的动作文件里带着它。

## 触发名不变

每个动作的触发名还是原来的（motion.hands_up、pose.hands_cross……），已有的按键映射、
别人分享的配置都不用迁移。

## 坐标

火柴人按"正对摄像头、没镜像"的画面坐标写：x 向右、y 向下，人的左手在画面右边。
往前往后的动作（踏步、下蹲、小腿向后抬起）用侧面画，正面看不出来。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

from .pose_rules import RuleError, normalize_rule
from .pose_template import PREVIEW_BONES, PREVIEW_POINTS

SCHEMA = "motioncontrol.pose_action.v1"

HAND_ZONES = ("leftHand", "rightHand")
FOOT_ZONES = ("leftFoot", "rightFoot")
ZONES = HAND_ZONES + FOOT_ZONES + ("headJump",)

RATING_NAMES = {"intensity": "运动强度", "recognition": "识别度", "difficulty": "上手难度"}
BODY_PART_NAMES = {"legs": "腿部", "glutes": "臀部", "core": "核心", "arms": "手臂", "shoulders": "肩背"}
STARS = (1, 5)

# 官方动作的编号。custom 开头的留给自己录的动作（custom_poses 发的是 custom1、custom2……），
# 两边都走 pose.<id>，撞上了就分不清是谁。
ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
MAX_NAME_LEN = 12
MAX_HOW_LEN = 60
MAX_FRAMES = 8


def _figure(base: dict | None = None, **moved: tuple[float, float]) -> dict:
    points = dict(base or {})
    points.update(moved)
    return points


# 侧面站立，脸朝画面左边。两边的点错开一点，看得出是两条腿。
# 躯干 0.28，大腿、小腿各 0.20，上臂 0.14，前臂 0.13。
SIDE = {
    "nose": (0.470, 0.100),
    "left_shoulder": (0.505, 0.220), "right_shoulder": (0.495, 0.220),
    "left_elbow": (0.505, 0.360), "right_elbow": (0.495, 0.360),
    "left_wrist": (0.495, 0.490), "right_wrist": (0.485, 0.490),
    "left_hip": (0.505, 0.500), "right_hip": (0.495, 0.500),
    "left_knee": (0.505, 0.700), "right_knee": (0.495, 0.700),
    "left_ankle": (0.505, 0.900), "right_ankle": (0.495, 0.900),
}

BUILTIN: tuple[dict, ...] = (
    {
        "id": "march", "group": "motion", "name": "原地踏步",
        "how": "左右脚轮流抬起来，像原地走路。抬第一步就算",
        "ratings": {"intensity": 2, "recognition": 5, "difficulty": 1},
        "body_parts": {"legs": 3, "glutes": 1, "core": 1},
        "passes_zones": (),
        "frame_s": 0.32,
        "frames": (
            _figure(SIDE, left_knee=(0.390, 0.580), left_ankle=(0.420, 0.770),
                    right_elbow=(0.450, 0.350), right_wrist=(0.400, 0.450)),
            SIDE,
            _figure(SIDE, right_knee=(0.380, 0.580), right_ankle=(0.410, 0.770),
                    left_elbow=(0.460, 0.350), left_wrist=(0.410, 0.450)),
            SIDE,
        ),
    },
    {
        "id": "calf_back", "group": "motion", "name": "小腿向后抬起",
        "how": "膝盖不动，一只脚往后抬到膝盖那么高，左右脚都行",
        "ratings": {"intensity": 2, "recognition": 4, "difficulty": 2},
        "body_parts": {"legs": 3, "glutes": 1},
        # 脚是在动作认出来之后才碰到脚区的：做着的时候不按就够了，平时不用晚按。
        "passes_zones": FOOT_ZONES, "sweeps_first": False,
        "frame_s": 0.55,
        "frames": (SIDE, _figure(SIDE, left_knee=(0.515, 0.700), left_ankle=(0.660, 0.600))),
    },
)
BUILTIN_IDS = frozenset(entry["id"] for entry in BUILTIN)


# --- 官方动作文件 ------------------------------------------------------------

def _stars(value, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not STARS[0] <= value <= STARS[1]:
        raise ValueError(f"{what}要是 {STARS[0]}~{STARS[1]} 星的整数")
    return value


def _number(value, low: float, high: float, what: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{what}必须是数字")
    if not low <= float(value) <= high:
        raise ValueError(f"{what}要在 {low} 到 {high} 之间")
    return float(value)


def normalize_action(raw) -> dict:
    """校验一份官方动作文件。云端发布前、电脑端安装前各跑一遍，同一套规则。"""
    if not isinstance(raw, dict):
        raise ValueError("动作文件必须是一个对象")
    if raw.get("schema") != SCHEMA:
        raise ValueError("动作文件的格式版本不认识")
    ident = str(raw.get("id", ""))
    if not ACTION_ID_RE.match(ident) or ident.startswith("custom"):
        raise ValueError(f"动作编号不对：{ident or '(空)'}")
    if ident in BUILTIN_IDS:
        raise ValueError(f"「{ident}」是程序自带的动作，不能从云端装")
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("revision 必须是正整数")
    group = raw.get("group")
    if group not in ("motion", "pose"):
        raise ValueError("group 只能是 motion 或 pose")
    name = " ".join(str(raw.get("name", "")).split())
    how = " ".join(str(raw.get("how", "")).split())
    if not name or len(name) > MAX_NAME_LEN:
        raise ValueError(f"名字要有，最多 {MAX_NAME_LEN} 个字")
    if not how or len(how) > MAX_HOW_LEN:
        raise ValueError(f"「怎么做」要有，最多 {MAX_HOW_LEN} 个字")

    ratings_raw = raw.get("ratings")
    if not isinstance(ratings_raw, dict) or set(ratings_raw) != set(RATING_NAMES):
        raise ValueError("星级要有且只有：" + "、".join(RATING_NAMES.values()))
    ratings = {key: _stars(ratings_raw[key], RATING_NAMES[key]) for key in RATING_NAMES}
    parts_raw = raw.get("body_parts")
    if not isinstance(parts_raw, dict) or not parts_raw:
        raise ValueError("至少要写一个锻炼部位")
    unknown = sorted(set(parts_raw) - set(BODY_PART_NAMES))
    if unknown:
        raise ValueError("不认识的锻炼部位：" + "、".join(unknown))
    body_parts = {key: _stars(parts_raw[key], BODY_PART_NAMES[key]) for key in BODY_PART_NAMES if key in parts_raw}

    zones = raw.get("passes_zones", [])
    if not isinstance(zones, list) or any(zone not in ZONES for zone in zones):
        raise ValueError("passes_zones 里有不认识的区域")

    debounce = raw.get("debounce")
    if (not isinstance(debounce, list) or len(debounce) != 2
            or any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 10 for n in debounce)):
        raise ValueError("debounce 要写成 [按下要几帧, 松开要几帧]，各 1~10")

    demo = raw.get("demo")
    if not isinstance(demo, dict):
        raise ValueError("要有示范")
    frames_raw = demo.get("frames")
    if not isinstance(frames_raw, list) or not 2 <= len(frames_raw) <= MAX_FRAMES:
        raise ValueError(f"示范要有 2~{MAX_FRAMES} 帧，才看得出在动")
    frames = []
    for frame in frames_raw:
        if not isinstance(frame, dict) or set(frame) != set(PREVIEW_POINTS):
            raise ValueError("示范的每一帧都要把 13 个点画全")
        points = {}
        for joint in PREVIEW_POINTS:
            xy = frame[joint]
            if not isinstance(xy, list) or len(xy) != 2:
                raise ValueError("示范里的点要写成 [x, y]")
            points[joint] = [_number(xy[0], -1.0, 2.0, "示范坐标"), _number(xy[1], -1.0, 2.0, "示范坐标")]
        frames.append(points)

    try:
        rule = normalize_rule(raw.get("rule"))
    except RuleError as exc:
        raise ValueError(f"识别规则不对：{exc}") from None

    out = {
        "schema": SCHEMA, "id": ident, "revision": revision, "group": group,
        "name": name, "how": how, "ratings": ratings, "body_parts": body_parts,
        "passes_zones": list(zones), "debounce": list(debounce),
        "demo": {"frame_s": _number(demo.get("frame_s", 0.5), 0.1, 3.0, "每帧时长"), "frames": frames},
        "rule": rule,
    }
    if zones:
        out["sweeps_first"] = bool(raw.get("sweeps_first", False))
    if group == "motion":
        timing = raw.get("risk_timing")
        if not isinstance(timing, list) or len(timing) != 2:
            raise ValueError("身体动作要写 risk_timing：[认定要多久, 放下要多久]，单位秒")
        out["risk_timing"] = [_number(timing[0], 0.0, 1.0, "risk_timing"), _number(timing[1], 0.0, 1.0, "risk_timing")]
        out["claims_lift"] = bool(raw.get("claims_lift", False))
        out["blocks_steps"] = bool(raw.get("blocks_steps", False))
        if out["claims_lift"] and not rule.get("sides"):
            raise ValueError("claims_lift 的动作要左右分开认（sides）：踏步要知道是哪只脚")
    elif raw.get("claims_lift") or raw.get("blocks_steps") or "risk_timing" in raw:
        raise ValueError("claims_lift、blocks_steps、risk_timing 只对身体动作有意义")
    return out


def canonical_bytes(doc: dict) -> bytes:
    """签名签的就是这串字节。云端原样发出去，电脑端对着它验签，再解析。"""
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --- 登记 -------------------------------------------------------------------

_registered: dict[str, dict] = {}


def register(docs) -> None:
    """换掉整份登记：电脑端给本机装了的，云端给全部官方动作。都要先过 normalize_action。"""
    _registered.clear()
    for doc in docs:
        _registered[doc["id"]] = doc


def registered() -> dict[str, dict]:
    return dict(_registered)


def _entry_from_doc(doc: dict) -> dict:
    return {
        "id": doc["id"], "group": doc["group"], "name": doc["name"], "how": doc["how"],
        "ratings": doc["ratings"], "body_parts": doc["body_parts"],
        "passes_zones": tuple(doc["passes_zones"]), "sweeps_first": doc.get("sweeps_first", False),
        "frame_s": doc["demo"]["frame_s"],
        "frames": tuple({name: tuple(xy) for name, xy in frame.items()} for frame in doc["demo"]["frames"]),
        "source": "cloud", "revision": doc["revision"],
    }


def entries() -> list[dict]:
    """现在有的动作：内置的在前，登记进来的按登记顺序。"""
    return [{**entry, "source": "builtin"} for entry in BUILTIN] + [_entry_from_doc(doc) for doc in _registered.values()]


def get(ident: str) -> dict | None:
    return next((entry for entry in entries() if entry["id"] == ident), None)


class _Names(Mapping):
    """某一组动作的 编号 → 名字。跟着登记走，不是导入那一刻的快照。"""

    def __init__(self, group: str):
        self.group = group

    def _table(self) -> dict[str, str]:
        return {entry["id"]: entry["name"] for entry in entries() if entry["group"] == self.group}

    def __getitem__(self, key):
        return self._table()[key]

    def __iter__(self):
        return iter(self._table())

    def __len__(self):
        return len(self._table())


MOTION_NAMES = _Names("motion")
POSE_NAMES = _Names("pose")


def trigger_of(entry: dict) -> str:
    return f"{entry['group']}.{entry['id']}"


def zone_crossers(zone: str) -> tuple[str, ...]:
    """做起来会扫过这个圈的那些动作的触发名。"""
    return tuple(trigger_of(entry) for entry in entries() if zone in entry["passes_zones"])



def demo_payload(frames, frame_s: float) -> dict:
    """示范的几帧，按所有帧一起的外接框缩到 0~1——各帧单独缩的话人会一跳一跳的。"""
    xs = [x for frame in frames for name, (x, _y) in frame.items() if name in PREVIEW_POINTS]
    ys = [y for frame in frames for name, (_x, y) in frame.items() if name in PREVIEW_POINTS]
    left, top = min(xs), min(ys)
    width, height = max(xs) - left, max(ys) - top
    span = max(width, height, 1e-6)
    offset_x, offset_y = (span - width) / 2.0, (span - height) / 2.0
    return {
        "frames": [
            {"points": {name: [round((x - left + offset_x) / span, 4), round((y - top + offset_y) / span, 4)]
                        for name, (x, y) in frame.items() if name in PREVIEW_POINTS},
             "bones": [list(bone) for bone in PREVIEW_BONES]}
            for frame in frames
        ],
        "frame_s": float(frame_s),
    }


def entry_payload(entry: dict) -> dict:
    """给界面的一个动作：名字、怎么做、示范、星级、会扫过哪些圈、从哪来。"""
    return {
        "id": entry["id"],
        "trigger": trigger_of(entry),
        "group": entry["group"],
        "name": entry["name"],
        "how": entry["how"],
        "ratings": dict(entry["ratings"]),
        "body_parts": dict(entry["body_parts"]),
        "passes_zones": list(entry["passes_zones"]),
        "source": entry.get("source", "builtin"),
        "revision": entry.get("revision", 0),
        "demo": demo_payload(entry["frames"], entry.get("frame_s", 0.5)),
    }


def doc_payload(doc: dict) -> dict:
    return entry_payload(_entry_from_doc(doc))


def library_payload() -> list[dict]:
    return [entry_payload(entry) for entry in entries()]
