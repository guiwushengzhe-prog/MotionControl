"""自定义姿势：把一帧骨架变成可比对的模板，以及比对它。

目前所有身体动作都是写死的——踏步、下蹲、开合跳这七个。用户想要别的，只能等人
去写一个新的识别器。这个模块换一条路：让用户摆一个姿势、拍下来存成模板，之后实
时比对。产品能识别什么，就不再由代码里有几个 if 决定。

## 为什么比的是方向而不是坐标

直接比 33 个关键点的坐标是行不通的。往左挪半米、站远一点、换个高矮不同的人，坐
标全变，而姿势没变。所以这里做三件事把无关的变化去掉：

* **躯干坐标系**——原点取两髋中点，"上"取髋中点指向两肩中点。位置消失了。
* **单位方向向量**——每段肢体只保留方向，不保留长度。体型和距离消失了。
* **躯干自身的倾斜单独记**——否则"站直举手"和"整个人歪 45 度举手"会算成同一个
  姿势，因为在躯干坐标系里它们确实一样。它该是一个特征，不该被消掉。

## 分数怎么算

每段算方向的夹角误差，再按 ``1 - 误差 / FULL_SCALE_ERROR`` 折成 0~1，取加权平均。

满量程取 60 度而不是 180 度，这一条是调出来的。按 180 度算的话，"双手平举"和
"双手下垂"能得 78 分——因为八段里腿占四段、两边都没动，光靠"大部分部位相同"就
把分顶上去了，而那恰恰不是我们想问的问题。改成 60 度之后，真人握住同一个姿势的
帧间抖动（三到八度）还在九成以上，换一个姿势直接掉到三成以下，中间留出了能定阈值
的空隙。

超过满量程就归零，不给负分：已经完全不像了，再区分"多不像"没有意义，反而会让一段
离谱的肢体把别的段拖成负数。

## 二维

用的是图像平面里的 x、y，不用 z。MediaPipe 的 z 是相对深度，噪声大到会让同一个姿
势在相邻两帧之间差出十几个百分点——那种抖动会让阈值没法定。代价是"手臂前伸"和
"手臂侧展"在正对镜头时区分不开；实际用起来这两个在画面上的投影长度差很多，加权
时低置信度的段本来就会被压低，所以影响比想象的小。

纯 stdlib，没有 numpy：这个包要能在 Linux 上被云端导入，而且云端将来要校验用户
上传的姿势模板。
"""

from __future__ import annotations

import math

SCHEMA = "motioncontrol.pose_template.v1"

# 每段肢体：名字、起点、终点。只取四肢——躯干的形状由坐标系本身表达了。
SEGMENTS: tuple[tuple[str, str, str], ...] = (
    ("left_upper_arm", "left_shoulder", "left_elbow"),
    ("left_forearm", "left_elbow", "left_wrist"),
    ("right_upper_arm", "right_shoulder", "right_elbow"),
    ("right_forearm", "right_elbow", "right_wrist"),
    ("left_thigh", "left_hip", "left_knee"),
    ("left_shin", "left_knee", "left_ankle"),
    ("right_thigh", "right_hip", "right_knee"),
    ("right_shin", "right_knee", "right_ankle"),
)

# 建立躯干坐标系必须有的四个点。缺一个就没有可比的基准，这时宁可说"看不清"，
# 也不要给一个看起来像模像样的数字。
FRAME_POINTS = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")

# MediaPipe 的可见度。低于这个值的点，它自己都不确定在哪。
MIN_SCORE = 0.5

# 误差到这个角度就算完全不像。取值理由见模块开头的"分数怎么算"。
FULL_SCALE_ERROR = math.radians(60.0)

# 躯干倾斜在总分里的份量。给两份而不是一份：整个人歪过去是一眼就能看出的差别，
# 按一份算的话歪 30 度只掉两个百分点，等于没记。
TILT_WEIGHT = 2.0


def _xy(pose: dict, name: str) -> tuple[float, float] | None:
    point = pose.get(name)
    if not isinstance(point, dict):
        return None
    try:
        x = float(point["x"])
        y = float(point["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return x, y


def _score(pose: dict, name: str) -> float:
    point = pose.get(name)
    if not isinstance(point, dict):
        return 0.0
    try:
        return max(0.0, min(1.0, float(point.get("score", 0.0))))
    except (TypeError, ValueError):
        return 0.0


def _normalise(vector: tuple[float, float]) -> tuple[float, float] | None:
    length = math.hypot(*vector)
    # 太短说明两点几乎重合，方向是噪声放大出来的，不是姿势。
    if length < 1e-6:
        return None
    return vector[0] / length, vector[1] / length


def _torso_frame(pose: dict):
    """返回 (right, up, ok)：躯干坐标系的两个基向量。"""
    corners = {name: _xy(pose, name) for name in FRAME_POINTS}
    if any(point is None for point in corners.values()):
        return None
    if any(_score(pose, name) < MIN_SCORE for name in FRAME_POINTS):
        return None

    hip = ((corners["left_hip"][0] + corners["right_hip"][0]) / 2.0,
           (corners["left_hip"][1] + corners["right_hip"][1]) / 2.0)
    shoulder = ((corners["left_shoulder"][0] + corners["right_shoulder"][0]) / 2.0,
                (corners["left_shoulder"][1] + corners["right_shoulder"][1]) / 2.0)

    # 图像坐标 y 向下，所以肩在髋的上方意味着 y 更小，这个差值自然指向屏幕上方。
    up = _normalise((shoulder[0] - hip[0], shoulder[1] - hip[1]))
    if up is None:
        return None
    # 垂直于 up。方向的正负无所谓——模板和实时帧用的是同一个定义，会相互抵消。
    right = (-up[1], up[0])
    return right, up


def build_template(pose: dict) -> dict | None:
    """把一帧骨架变成模板。看不清就返回 None，不返回一个糊弄人的结果。"""
    frame = _torso_frame(pose)
    if frame is None:
        return None
    right, up = frame

    segments: dict[str, list[float]] = {}
    for name, start, end in SEGMENTS:
        head = _xy(pose, start)
        tail = _xy(pose, end)
        if head is None or tail is None:
            continue
        if min(_score(pose, start), _score(pose, end)) < MIN_SCORE:
            continue
        direction = _normalise((tail[0] - head[0], tail[1] - head[1]))
        if direction is None:
            continue
        # 投影到躯干坐标系。这一步之后，人站在画面哪里、离镜头多远都不影响结果。
        segments[name] = [
            direction[0] * right[0] + direction[1] * right[1],
            direction[0] * up[0] + direction[1] * up[1],
        ]

    # 少于三段就没什么可比的了——两段肢体相同的姿势太多。
    if len(segments) < 3:
        return None

    return {
        "schema": SCHEMA,
        "segments": segments,
        # 躯干相对画面竖直方向的倾斜，弧度。单独记，见模块开头。
        "torso_tilt": math.atan2(up[0], -up[1]),
    }


def compare(template: dict, pose: dict) -> dict | None:
    """比对一帧和一个模板。

    返回 ``{"score": 0..1, "segments": {段名: 0..1}, "matched": 参与比对的段数}``，
    看不清则返回 None。逐段的分数一并给出来，是为了能回答"哪里不像"——只给一个
    总分，用户对着 62% 是没法调整的。
    """
    if not isinstance(template, dict) or template.get("schema") != SCHEMA:
        return None
    wanted = template.get("segments")
    if not isinstance(wanted, dict) or not wanted:
        return None

    live = build_template(pose)
    if live is None:
        return None

    per_segment: dict[str, float] = {}
    total = weight = 0.0
    for name, expected in wanted.items():
        actual = live["segments"].get(name)
        if actual is None or not isinstance(expected, (list, tuple)) or len(expected) != 2:
            continue
        cosine = max(-1.0, min(1.0, expected[0] * actual[0] + expected[1] * actual[1]))
        similarity = _from_error(math.acos(cosine))
        per_segment[name] = similarity
        total += similarity
        weight += 1.0

    if weight == 0.0:
        return None

    # 躯干倾斜和四肢用同一把尺子，这样"歪了 30 度"和"胳膊偏了 30 度"扣一样的分。
    tilt_delta = abs(_wrap(live["torso_tilt"] - float(template.get("torso_tilt", 0.0))))
    tilt_similarity = _from_error(tilt_delta)
    total += tilt_similarity * TILT_WEIGHT
    weight += TILT_WEIGHT

    return {
        "score": total / weight,
        "segments": per_segment,
        "torso_tilt": tilt_similarity,
        "matched": len(per_segment),
    }


def _from_error(error: float) -> float:
    """角度误差折成 0~1 的相似度。超过满量程归零，不给负分。"""
    return max(0.0, 1.0 - error / FULL_SCALE_ERROR)


def _wrap(radians: float) -> float:
    """折到 (-pi, pi]，免得 179 度和 -179 度被当成差 358 度。"""
    return (radians + math.pi) % (2 * math.pi) - math.pi


def weakest_segments(result: dict, limit: int = 3) -> list[tuple[str, float]]:
    """最不像的几段，用来告诉用户该动哪里。"""
    segments = result.get("segments") if isinstance(result, dict) else None
    if not isinstance(segments, dict):
        return []
    return sorted(segments.items(), key=lambda item: item[1])[:limit]


# --- 录下来那一瞬间长什么样 ----------------------------------------------------
#
# 模板本身只有方向向量，画不出人形——那是刻意的，位置和体型必须被消掉才能比对。
# 但用户需要认出"这个是哪个姿势"，光看名字不够。所以另存一份用于显示的骨架点。
#
# 存点而不是存照片：摄像头在手机上时，电脑这边根本没有画面；骨架点则一定有，
# 而且只有几百字节，跟着配置走到别的机器上也画得出来。

PREVIEW_POINTS = (
    "nose",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
    "left_hip", "right_hip", "left_knee", "right_knee",
    "left_ankle", "right_ankle",
)

# 连线。放在共享包里，桌面和云端画出来的是同一个人形。
PREVIEW_BONES = (
    ("left_shoulder", "right_shoulder"),
    ("left_shoulder", "left_hip"), ("right_shoulder", "right_hip"),
    ("left_hip", "right_hip"),
    ("left_shoulder", "left_elbow"), ("left_elbow", "left_wrist"),
    ("right_shoulder", "right_elbow"), ("right_elbow", "right_wrist"),
    ("left_hip", "left_knee"), ("left_knee", "left_ankle"),
    ("right_hip", "right_knee"), ("right_knee", "right_ankle"),
)


def build_preview(pose: dict) -> dict | None:
    """录制瞬间的骨架，缩放到 0~1 的框里，只用于显示。

    按可见点的外接矩形等比缩放居中：人站在画面哪个角落、占多大，缩略图里都一样大。
    不等比的话，站远时人会被拉成一条细线。
    """
    points: dict[str, tuple[float, float]] = {}
    for name in PREVIEW_POINTS:
        position = _xy(pose, name)
        if position is not None and _score(pose, name) >= MIN_SCORE:
            points[name] = position
    if len(points) < 4:
        return None

    xs = [x for x, _ in points.values()]
    ys = [y for _, y in points.values()]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    # 等比：取长边当基准，短边居中。
    span = max(width, height, 1e-6)
    offset_x = (span - width) / 2.0
    offset_y = (span - height) / 2.0

    return {
        "points": {name: [round((x - min(xs) + offset_x) / span, 4),
                          round((y - min(ys) + offset_y) / span, 4)]
                   for name, (x, y) in points.items()},
        "bones": [list(bone) for bone in PREVIEW_BONES
                  if bone[0] in points and bone[1] in points],
    }
