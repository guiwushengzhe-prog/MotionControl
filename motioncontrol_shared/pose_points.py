"""人体骨骼点的顺序和中文名称，供输入解析、配置校验与界面共用。"""

# 保持姿态模型的 33 点顺序，输入包按此顺序解析。
POSE_POINT_LABELS = {
    "nose": "鼻子",
    "left_eye_inner": "左眼内侧", "left_eye": "左眼", "left_eye_outer": "左眼外侧",
    "right_eye_inner": "右眼内侧", "right_eye": "右眼", "right_eye_outer": "右眼外侧",
    "left_ear": "左耳", "right_ear": "右耳",
    "mouth_left": "左嘴角", "mouth_right": "右嘴角",
    "left_shoulder": "左肩", "right_shoulder": "右肩",
    "left_elbow": "左肘", "right_elbow": "右肘",
    "left_wrist": "左手腕", "right_wrist": "右手腕",
    "left_pinky": "左小指", "right_pinky": "右小指",
    "left_index": "左食指", "right_index": "右食指",
    "left_thumb": "左拇指", "right_thumb": "右拇指",
    "left_hip": "左髋", "right_hip": "右髋",
    "left_knee": "左膝", "right_knee": "右膝",
    "left_ankle": "左脚踝", "right_ankle": "右脚踝",
    "left_heel": "左后跟", "right_heel": "右后跟",
    "left_foot_index": "左脚尖", "right_foot_index": "右脚尖",
}
MP_NAMES = list(POSE_POINT_LABELS)

# 人体模型的标准连接，只用于绘制背景骨架；用户可以连接任意两个点。
POSE_CONNECTIONS = tuple((MP_NAMES[a], MP_NAMES[b]) for a, b in (
    (0, 1), (1, 2), (2, 3), (3, 7), (0, 4), (4, 5), (5, 6), (6, 8), (9, 10),
    (11, 12), (11, 13), (13, 15), (15, 17), (15, 19), (15, 21), (17, 19),
    (12, 14), (14, 16), (16, 18), (16, 20), (16, 22), (18, 20),
    (11, 23), (12, 24), (23, 24), (23, 25), (24, 26), (25, 27), (26, 28),
    (27, 29), (28, 30), (29, 31), (30, 32), (27, 31), (28, 32),
))


def trigger_point_groups(points, segments) -> tuple:
    """独立点之间任选其一，连成一组的点必须全部同时进入。配置变化时调用。"""
    neighbors = {}
    for a, b in segments:
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)
    groups = [(point,) for point in dict.fromkeys(points) if point not in neighbors]
    seen = set()
    for start in neighbors:
        if start in seen:
            continue
        pending, members = [start], set()
        while pending:
            point = pending.pop()
            if point in members:
                continue
            members.add(point)
            pending.extend(neighbors[point] - members)
        seen.update(members)
        groups.append(tuple(point for point in MP_NAMES if point in members))
    return tuple(groups)
