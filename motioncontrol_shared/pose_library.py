"""动作库：内置的身体动作，每个都有名字、怎么做、火柴人示范。

新手不用自己录，打开就能看到"这个动作长什么样"，直接绑键。

## 两种认法

* **模板**：和"自定义动作"同一套比对（motioncontrol_shared.pose_template），只比
  肢体方向。示范里那一帧就是模板——示范和判定是同一份数据，不会对不上。每个人
  "像到多少才算"可以自己调。
* **代码**：模板认不了的。模板只看方向、故意不看长短，而正对摄像头时，往前往后
  的动作在画面上只是"变短"（下蹲、小腿向后抬起）；两脚分开也只差二十来度，站着
  举手就能到 0.86，和真开合跳分不开；踏步要看左右节奏，一帧姿势表达不了。这些
  继续由 control_kernel 里的规则认，示范只用来给人看。

## 触发名不变

每个动作的触发名还是原来的（motion.hands_up、pose.hands_cross……）。换的只是背后
怎么认，已有的按键映射、别人分享的配置都不用迁移。

## 名字只在这里写

describe.py 和 motion_conflicts.py 的中文名都从这里取；web/app.js 那份由
tests/test_describe.py 断言和这里一致。

## 坐标

火柴人按"正对摄像头、没镜像"的画面坐标写：x 向右、y 向下，人的左手在画面右边，
和实时骨架是同一个坐标系，所以模板动作能直接从示范帧生成模板。只用来看的示范
（下蹲、小腿向后抬起）用侧面，那样才看得出往后。
"""

from __future__ import annotations

from .pose_template import PREVIEW_BONES, PREVIEW_POINTS, SEGMENTS, build_template, compare

ARM_SEGMENTS = ("left_upper_arm", "left_forearm", "right_upper_arm", "right_forearm")

# 模板动作除了总分够，关心的每一段还都得大致到位（和示范差不到 45 度）。只看平均
# 的话，两段全对、两段全错也能拿到 0.67：单手举会被认成双手举过头。
SEGMENT_FLOOR = 0.25


def _figure(base: dict | None = None, **moved: tuple[float, float]) -> dict:
    points = dict(base or {})
    points.update(moved)
    return points


# 正面站立。躯干 0.28，大腿、小腿各 0.20，上臂 0.14，前臂 0.13。
STAND = {
    "nose": (0.500, 0.100),
    "left_shoulder": (0.575, 0.220), "right_shoulder": (0.425, 0.220),
    "left_elbow": (0.600, 0.360), "right_elbow": (0.400, 0.360),
    "left_wrist": (0.610, 0.490), "right_wrist": (0.390, 0.490),
    "left_hip": (0.550, 0.500), "right_hip": (0.450, 0.500),
    "left_knee": (0.555, 0.700), "right_knee": (0.445, 0.700),
    "left_ankle": (0.560, 0.900), "right_ankle": (0.440, 0.900),
}

# 侧面站立，脸朝画面左边。两边的点错开一点，看得出是两条腿。
SIDE = {
    "nose": (0.470, 0.100),
    "left_shoulder": (0.505, 0.220), "right_shoulder": (0.495, 0.220),
    "left_elbow": (0.505, 0.360), "right_elbow": (0.495, 0.360),
    "left_wrist": (0.495, 0.490), "right_wrist": (0.485, 0.490),
    "left_hip": (0.505, 0.500), "right_hip": (0.495, 0.500),
    "left_knee": (0.505, 0.700), "right_knee": (0.495, 0.700),
    "left_ankle": (0.505, 0.900), "right_ankle": (0.495, 0.900),
}

HANDS_UP = _figure(STAND,
                   left_elbow=(0.620, 0.090), right_elbow=(0.380, 0.090),
                   left_wrist=(0.625, -0.040), right_wrist=(0.375, -0.040))

LEGS_APART = dict(left_knee=(0.620, 0.690), right_knee=(0.380, 0.690),
                  left_ankle=(0.690, 0.880), right_ankle=(0.310, 0.880))

LIBRARY: tuple[dict, ...] = (
    {
        "id": "march", "group": "motion", "name": "原地踏步",
        "how": "左右膝盖轮流抬起来，像原地走路。抬第一步就算",
        # 侧面画：正面画抬膝只看得出腿短了一截，看不出在抬腿。
        "detector": "code", "frame_s": 0.32,
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
        "how": "膝盖不动，一只脚往后抬起来，左右脚都行",
        "detector": "code", "frame_s": 0.55,
        "frames": (SIDE, _figure(SIDE, left_knee=(0.515, 0.700), left_ankle=(0.660, 0.600))),
    },
    {
        "id": "squat", "group": "motion", "name": "下蹲",
        "how": "屁股往后坐，两个膝盖都弯下去",
        "detector": "code", "frame_s": 0.60,
        "frames": (
            SIDE,
            _figure(SIDE,
                    nose=(0.440, 0.260), left_shoulder=(0.485, 0.370), right_shoulder=(0.475, 0.370),
                    left_elbow=(0.420, 0.440), right_elbow=(0.410, 0.440),
                    left_wrist=(0.310, 0.430), right_wrist=(0.300, 0.430),
                    left_hip=(0.600, 0.620), right_hip=(0.590, 0.620),
                    left_knee=(0.440, 0.700), right_knee=(0.430, 0.700)),
        ),
    },
    {
        "id": "hands_up", "group": "motion", "name": "双手举过头",
        "how": "两只手一起举过头顶",
        # 0.55：两手举成很开的 V 字（约 0.56）也算，抱头（0.51）和侧平举（0.33）不算。
        "detector": "template", "key_frame": 1, "focus": ARM_SEGMENTS, "threshold": 0.55,
        "frame_s": 0.60,
        "frames": (STAND, HANDS_UP),
    },
    {
        "id": "jumping_jack", "group": "motion", "name": "开合跳",
        "how": "跳起来两脚分开、两手举过头，再合上",
        "detector": "code", "frame_s": 0.45,
        "frames": (STAND, _figure(STAND, left_elbow=(0.660, 0.100), right_elbow=(0.340, 0.100),
                                  left_wrist=(0.720, -0.010), right_wrist=(0.280, -0.010),
                                  **LEGS_APART)),
    },
    {
        "id": "side_step_jack", "group": "motion", "name": "侧步开合",
        "how": "一脚往旁边迈开，两手侧平举，再收回",
        "detector": "code", "frame_s": 0.50,
        "frames": (STAND, _figure(STAND, left_elbow=(0.715, 0.225), right_elbow=(0.285, 0.225),
                                  left_wrist=(0.850, 0.225), right_wrist=(0.150, 0.225),
                                  **LEGS_APART)),
    },
    {
        "id": "cross_knee_elbow", "group": "motion", "name": "提膝碰对侧肘",
        "how": "抬起一侧膝盖，用另一边的手肘去碰，左右交替",
        "detector": "code", "frame_s": 0.50,
        "frames": (
            _figure(STAND, left_knee=(0.520, 0.540), left_ankle=(0.560, 0.720),
                    right_elbow=(0.495, 0.470), right_wrist=(0.440, 0.360),
                    left_elbow=(0.660, 0.150), left_wrist=(0.560, 0.090)),
            STAND,
            _figure(STAND, right_knee=(0.480, 0.540), right_ankle=(0.440, 0.720),
                    left_elbow=(0.505, 0.470), left_wrist=(0.560, 0.360),
                    right_elbow=(0.340, 0.150), right_wrist=(0.440, 0.090)),
            STAND,
        ),
    },
    {
        "id": "hands_cross", "group": "pose", "name": "双手交叉",
        "how": "两只小臂在胸前交叉成 X",
        "detector": "code", "frame_s": 0.60,
        "frames": (STAND, _figure(STAND, left_elbow=(0.620, 0.380), right_elbow=(0.380, 0.380),
                                  left_wrist=(0.455, 0.300), right_wrist=(0.545, 0.300))),
    },
)

BY_ID = {entry["id"]: entry for entry in LIBRARY}
MOTION_NAMES = {entry["id"]: entry["name"] for entry in LIBRARY if entry["group"] == "motion"}
POSE_NAMES = {entry["id"]: entry["name"] for entry in LIBRARY if entry["group"] == "pose"}
TEMPLATE_IDS = tuple(entry["id"] for entry in LIBRARY if entry["detector"] == "template")

THRESHOLD_RANGE = (0.50, 0.99)


def trigger_of(entry: dict) -> str:
    return f"{entry['group']}.{entry['id']}"


def _pose_map(points: dict) -> dict:
    return {name: {"x": x, "y": y, "score": 1.0} for name, (x, y) in points.items()}


def library_template(entry_id: str) -> dict | None:
    """模板动作的模板：示范里那一帧，只留关心的那几段。"""
    entry = BY_ID.get(entry_id)
    if entry is None or entry["detector"] != "template":
        return None
    template = build_template(_pose_map(entry["frames"][entry["key_frame"]]))
    if template is None:
        return None
    focus = set(entry.get("focus") or (name for name, _a, _b in SEGMENTS))
    template["segments"] = {name: value for name, value in template["segments"].items() if name in focus}
    return template


def match(template: dict, pose_map: dict, threshold: float) -> dict | None:
    """一帧对上一个模板动作的程度。看不清返回 None。

    ``score`` 给界面显示相似度，``hit`` 是判定：总分过线，而且每一段都不离谱。
    """
    result = compare(template, pose_map)
    if result is None:
        return None
    segments = result["segments"]
    whole = len(segments) == len(template.get("segments") or {})
    hit = whole and result["score"] >= threshold and min(segments.values(), default=0.0) >= SEGMENT_FLOOR
    return {"score": result["score"], "hit": bool(hit)}


def default_threshold(entry_id: str) -> float | None:
    entry = BY_ID.get(entry_id)
    return float(entry["threshold"]) if entry and entry["detector"] == "template" else None


def _demo(entry: dict) -> dict:
    """示范的几帧，按所有帧一起的外接框缩到 0~1——各帧单独缩的话人会一跳一跳的。"""
    frames = entry["frames"]
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
        "frame_s": float(entry.get("frame_s", 0.5)),
    }


def library_payload() -> list[dict]:
    """给界面的：名字、怎么做、示范、怎么认。云端将来也能用它画同样的火柴人。"""
    return [{
        "id": entry["id"],
        "trigger": trigger_of(entry),
        "group": entry["group"],
        "name": entry["name"],
        "how": entry["how"],
        "detector": entry["detector"],
        "threshold": default_threshold(entry["id"]),
        "demo": _demo(entry),
    } for entry in LIBRARY]
