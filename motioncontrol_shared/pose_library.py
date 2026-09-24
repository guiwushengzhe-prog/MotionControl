"""动作库：内置的身体动作，每个都有名字、怎么做、火柴人示范。

新手不用自己录，打开就能看到"这个动作长什么样"，照着做，直接绑键。

## 怎么认

全部由 control_kernel 里的规则认，示范只是给人看的。试过把"静态"的几个换成录制
模板（和自定义动作同一套比对，只比肢体方向），2026-09-25 拿真人录像回放：双手
举过头 6 次只认出 1 次，提膝碰对侧肘做和不做时的相似度搅在一起。一个标准姿势套
不住每个人的做法——举手时手肘弯成什么样、脚分多开，人人不同；而正对镜头时，往前
往后的动作在画面上只是"变短"，方向几乎不变。录制模板适合录你自己，不适合当所有人
的标准。

## 会扫过哪些圈

``passes_zones`` 是做这个动作时身体会经过的跟随区域（同一批录像里量出来的）。
举双手时手从两侧往上走，正好扫过两边的手区；这躲不开——"侧挥手进手区"和"两手从
侧面举过头"走的是同一条路。两边都绑了键时，动作做着的时候那几个圈不按；
``sweeps_first`` 的（动作认出来之前就先扫过圈的）圈平时还要晚一点按（见
control_kernel 的 ZONE_YIELD_S）。界面在绑键时提醒一句。头顶区不在让的范围：开合
跳本身就是在跳，让它晚按等于跳不起来，只提醒。

## 触发名不变

每个动作的触发名还是原来的（motion.hands_up、pose.hands_cross……），已有的按键映射、
别人分享的配置都不用迁移。

## 名字只在这里写

describe.py 和 motion_conflicts.py 的中文名都从这里取；web/app.js 那份由
tests/test_describe.py 断言和这里一致。

## 坐标

火柴人按"正对摄像头、没镜像"的画面坐标写：x 向右、y 向下，人的左手在画面右边。
往前往后的动作（踏步、下蹲、小腿向后抬起）用侧面画，正面看不出来。
"""

from __future__ import annotations

from .pose_template import PREVIEW_BONES, PREVIEW_POINTS

HAND_ZONES = ("leftHand", "rightHand")
FOOT_ZONES = ("leftFoot", "rightFoot")


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

LEGS_APART = dict(left_knee=(0.620, 0.690), right_knee=(0.380, 0.690),
                  left_ankle=(0.690, 0.880), right_ankle=(0.310, 0.880))

LIBRARY: tuple[dict, ...] = (
    {
        "id": "march", "group": "motion", "name": "原地踏步",
        "how": "左右脚轮流抬起来，像原地走路。抬第一步就算",
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
        # 脚是在动作认出来之后才碰到脚区的：做着的时候不按就够了，平时不用晚按。
        "passes_zones": FOOT_ZONES, "sweeps_first": False,
        "frame_s": 0.55,
        "frames": (SIDE, _figure(SIDE, left_knee=(0.515, 0.700), left_ankle=(0.660, 0.600))),
    },
    {
        "id": "squat", "group": "motion", "name": "下蹲",
        "how": "屁股往后坐，两个膝盖都弯下去",
        "passes_zones": (),
        "frame_s": 0.60,
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
        "how": "两只手一起举过头顶，单手不算",
        # 手从两侧往上抬，还没举到头顶（动作还没认出来）就先扫过了手区。
        "passes_zones": HAND_ZONES, "sweeps_first": True,
        "frame_s": 0.60,
        "frames": (STAND, _figure(STAND, left_elbow=(0.620, 0.090), right_elbow=(0.380, 0.090),
                                  left_wrist=(0.625, -0.040), right_wrist=(0.375, -0.040))),
    },
    {
        "id": "jumping_jack", "group": "motion", "name": "开合跳",
        "how": "跳起来两脚分开、两手举过头，再合上",
        "passes_zones": HAND_ZONES + ("headJump",), "sweeps_first": True,
        "frame_s": 0.45,
        "frames": (STAND, _figure(STAND, left_elbow=(0.660, 0.100), right_elbow=(0.340, 0.100),
                                  left_wrist=(0.720, -0.010), right_wrist=(0.280, -0.010),
                                  **LEGS_APART)),
    },
    {
        "id": "side_step_jack", "group": "motion", "name": "侧步开合",
        "how": "一脚往旁边迈开，两手侧平举，再收回",
        "passes_zones": HAND_ZONES + FOOT_ZONES, "sweeps_first": True,
        "frame_s": 0.50,
        "frames": (STAND, _figure(STAND, left_elbow=(0.715, 0.225), right_elbow=(0.285, 0.225),
                                  left_wrist=(0.850, 0.225), right_wrist=(0.150, 0.225),
                                  **LEGS_APART)),
    },
    {
        "id": "cross_knee_elbow", "group": "motion", "name": "提膝碰对侧肘",
        "how": "抬起一侧膝盖，用另一边的手肘去碰，左右交替",
        # 手抱到头后时会扫过手区；膝盖抬起时脚有时往外甩，会碰到脚区。
        "passes_zones": HAND_ZONES + FOOT_ZONES, "sweeps_first": True,
        "frame_s": 0.50,
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
        "passes_zones": (),
        "frame_s": 0.60,
        "frames": (STAND, _figure(STAND, left_elbow=(0.620, 0.380), right_elbow=(0.380, 0.380),
                                  left_wrist=(0.455, 0.300), right_wrist=(0.545, 0.300))),
    },
)

BY_ID = {entry["id"]: entry for entry in LIBRARY}
MOTION_NAMES = {entry["id"]: entry["name"] for entry in LIBRARY if entry["group"] == "motion"}
POSE_NAMES = {entry["id"]: entry["name"] for entry in LIBRARY if entry["group"] == "pose"}


def trigger_of(entry: dict) -> str:
    return f"{entry['group']}.{entry['id']}"


def zone_crossers(zone: str) -> tuple[str, ...]:
    """做起来会扫过这个圈的那些动作的触发名。"""
    return tuple(trigger_of(entry) for entry in LIBRARY if zone in entry["passes_zones"])


def sweeps_first(trigger: str) -> bool:
    """这个动作是不是在认出来之前就先扫过圈。是的话圈平时也要晚一点按。"""
    entry = BY_ID.get(trigger.split(".", 1)[-1])
    return bool(entry and entry.get("sweeps_first"))


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
    """给界面的：名字、怎么做、示范、会扫过哪些圈。云端将来也能用它画同样的火柴人。"""
    return [{
        "id": entry["id"],
        "trigger": trigger_of(entry),
        "group": entry["group"],
        "name": entry["name"],
        "how": entry["how"],
        "passes_zones": list(entry["passes_zones"]),
        "demo": _demo(entry),
    } for entry in LIBRARY]
