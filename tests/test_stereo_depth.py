"""双目深度：用人体关键点标定电脑 + 手机两台相机，再三角化出手往前伸了多少。

合成一个真值已知的场景（两台相机的焦距、相对位置、时间差都已知），从运行时的两个
入口喂帧，检查：标定解回了真值、标定存盘后重启还能用、手往前推时读数变大、
挪了相机（画面尺寸变了）就不再输出。
"""

import json
import math
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol import stereo_solver as solver  # noqa: E402
from motioncontrol.stereo_depth import StereoDepth  # noqa: E402

PC_SIZE, PHONE_SIZE = (480, 640), (480, 864)
F_PC, F_PHONE = 480.0, 700.0
LAG_S = 0.06  # 手机帧进内核时比真实拍摄晚 60 ms


def _look_at(centre, target, roll=0.08):
    z = target - centre
    z /= np.linalg.norm(z)
    x = np.cross([0, 1, 0], z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return cv2.Rodrigues(np.array([0, 0, roll]))[0] @ np.vstack([x, y, z])


PHONE_CENTRE = np.array([-0.69, -0.02, 0.72]) * 76.0  # 厘米，电脑相机坐标
R_PHONE = _look_at(PHONE_CENTRE, np.array([0.0, -10.0, 150.0]))
T_PHONE = -R_PHONE @ PHONE_CENTRE


def body(t: float, push: float | None = None) -> dict[str, np.ndarray]:
    """约 150 cm 外活动的上半身（厘米）。push 给定时双手伸到身前 push 厘米处。"""
    root = np.array([20 * math.sin(0.4 * t), 8 * math.sin(0.23 * t), 150 + 30 * math.sin(0.31 * t)])
    yaw = 0.4 * math.sin(0.17 * t)
    ry = cv2.Rodrigues(np.array([0, yaw, 0]))[0]
    pts = {
        "left_shoulder": np.array([18.0, -45, 0]), "right_shoulder": np.array([-18.0, -45, 0]),
        "left_hip": np.array([12.0, 5, 0]), "right_hip": np.array([-12.0, 5, 0]),
    }
    for side, sign, phase in (("left", 1, 0.0), ("right", -1, 1.7)):
        shoulder = pts[f"{side}_shoulder"]
        if push is None:
            a = 1.2 * math.sin(0.9 * t + phase)
            b = 0.8 * math.sin(1.3 * t + 2 * phase)
            up = np.array([sign * 28 * math.sin(0.5 + 0.3 * b), 16.8 * math.cos(a), -28 * math.sin(a)])
            elbow = shoulder + up / np.linalg.norm(up) * 28
            fore = np.array([sign * 5, -10 * math.cos(b), -25 * math.sin(b) - 10])
            wrist = elbow + fore / np.linalg.norm(fore) * 25
        else:
            # 手腕放在肩前 push 厘米（-z 朝电脑相机），上臂 28、前臂 25 不变，手肘自然下垂。
            wrist = shoulder + np.array([0.0, 20.0 if push < 45 else 12.0, -push])
            d = np.linalg.norm(wrist - shoulder)
            u = (wrist - shoulder) / d
            along = (d * d + 28 * 28 - 25 * 25) / (2 * d)
            down = np.array([0.0, 1.0, 0.0]) - u[1] * u
            elbow = shoulder + u * along + down / np.linalg.norm(down) * math.sqrt(max(0.0, 28 * 28 - along * along))
        pts[f"{side}_elbow"], pts[f"{side}_wrist"] = elbow, wrist
    return {k: ry @ v + root for k, v in pts.items()}


def to_pose_map(points: dict, rotation, t, f, size, rng, noise) -> dict:
    out = {}
    for name, x in points.items():
        c = rotation @ x + t
        u = f * c[0] / c[2] + size[0] / 2 + rng.normal(0, noise)
        v = f * c[1] / c[2] + size[1] / 2 + rng.normal(0, noise)
        out[name] = {"x": u / size[0], "y": v / size[1], "z": 0.0, "score": 0.95}
    return out


def feed(stereo: StereoDepth, start: float, seconds: float, rng, noise=1.0):
    """电脑 30 帧、手机 12 帧交错喂入；返回最后一个时刻。"""
    events = [(start + i / 30, "pc") for i in range(int(seconds * 30))]
    events += [(start + 0.04 + i / 12, "phone") for i in range(int(seconds * 12))]
    for when, kind in sorted(events):
        if kind == "pc":
            pose = to_pose_map(body(when - start), np.eye(3), np.zeros(3), F_PC, PC_SIZE, rng, noise)
            stereo.observe_pc(pose, *PC_SIZE, when)
        else:
            pose = to_pose_map(body(when - start), R_PHONE, T_PHONE, F_PHONE, PHONE_SIZE, rng, noise * 1.5)
            stereo.observe_phone(pose, *PHONE_SIZE, when + LAG_S)
    return start + seconds


def calibrated(tmp_path, rng) -> StereoDepth:
    stereo = StereoDepth(tmp_path / "stereo.json")
    start = 1000.0
    feed(stereo, start, 0.5, rng)
    stereo.start_calibration(20, now=start + 0.5)
    end = feed(stereo, start + 0.5, 21, rng)
    deadline = time.monotonic() + 120
    while stereo.state == "solving" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert stereo.state == "ready", stereo.message
    stereo.end_time = end
    return stereo


@pytest.fixture(scope="module")
def stereo_and_dir(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("stereo")
    return calibrated(tmp, np.random.default_rng(3)), tmp


def test_calibration_recovers_the_true_geometry(stereo_and_dir):
    cal = stereo_and_dir[0].calibration
    assert cal["f_pc"] == pytest.approx(F_PC, rel=0.05)
    assert cal["f_phone"] == pytest.approx(F_PHONE, rel=0.05)
    true_axis = math.degrees(math.acos(R_PHONE[2, 2]))
    assert cal["optical_axis_angle_deg"] == pytest.approx(true_axis, abs=2.0)
    # 手机帧晚到 60 ms：手机时刻 + offset = 电脑时刻，offset 应约为 -60 ms
    assert cal["phone_to_pc_offset_ms"] == pytest.approx(-LAG_S * 1000, abs=25)
    assert cal["holdout"]["reprojection_median_px"] < 3.0


def test_pushing_hands_forward_raises_the_reading(stereo_and_dir):
    stereo, _ = stereo_and_dir
    rng = np.random.default_rng(5)
    t = stereo.end_time + 1.0
    readings = []
    for push in (10.0, 30.0, 50.0):
        # 同一姿势连着喂几帧电脑画面，再来一帧手机
        points = body(0.0, push=push)
        for k in range(4):
            pose = to_pose_map(points, np.eye(3), np.zeros(3), F_PC, PC_SIZE, rng, 0.5)
            stereo.observe_pc(pose, *PC_SIZE, t + k / 30)
        phone = to_pose_map(points, R_PHONE, T_PHONE, F_PHONE, PHONE_SIZE, rng, 0.5)
        result = stereo.observe_phone(phone, *PHONE_SIZE, t + 2 / 30 + LAG_S)
        assert result["valid"], result
        readings.append(result["hands"]["left"]["forward"])
        t += 1.0
    assert readings[0] < readings[1] < readings[2]
    # 肩宽 36 cm：伸 50 cm 约 1.39 个肩宽
    assert readings[2] == pytest.approx(50 / 36, abs=0.1)


def test_hanging_or_spreading_the_arms_is_not_read_as_pushing(stereo_and_dir):
    """只算水平朝前的量：手垂在身侧、平伸到两侧都读 0。"""
    stereo, _ = stereo_and_dir
    rng = np.random.default_rng(17)
    t = stereo.end_time + 5.0
    for direction in ("down", "side"):
        points = body(0.0, push=30.0)
        for side, sign in (("left", 1.0), ("right", -1.0)):
            axis = np.array([0.0, 1.0, 0.0]) if direction == "down" else np.array([sign, 0.0, 0.0])
            points[f"{side}_elbow"] = points[f"{side}_shoulder"] + 28 * axis
            points[f"{side}_wrist"] = points[f"{side}_shoulder"] + 53 * axis
        for k in range(3):
            stereo.observe_pc(to_pose_map(points, np.eye(3), np.zeros(3), F_PC, PC_SIZE, rng, 0.5), *PC_SIZE, t + k / 30)
        result = stereo.observe_phone(to_pose_map(points, R_PHONE, T_PHONE, F_PHONE, PHONE_SIZE, rng, 0.5),
                                      *PHONE_SIZE, t + 1 / 30 + LAG_S)
        for side in ("left", "right"):
            assert result["hands"][side]["forward"] == pytest.approx(0.0, abs=0.1), (direction, result)
        t += 1.0


def test_a_phone_frame_waits_for_the_matching_computer_frame(stereo_and_dir):
    """手机帧先到、同一时刻的电脑帧还在识别：先挂起，电脑帧一到就算，不丢这一帧。"""
    stereo, _ = stereo_and_dir
    rng = np.random.default_rng(11)
    t = stereo.end_time + 20.0
    points = body(0.0, push=30.0)
    def pc(k):
        stereo.observe_pc(to_pose_map(points, np.eye(3), np.zeros(3), F_PC, PC_SIZE, rng, 0.5), *PC_SIZE, t + k / 30)

    for k in range(3):
        pc(k)
    stereo.latest = None
    phone = to_pose_map(points, R_PHONE, T_PHONE, F_PHONE, PHONE_SIZE, rng, 0.5)
    # 手机这一帧拍在电脑第 2.5 帧的时刻，电脑第 3 帧还没识别完
    assert stereo.observe_phone(phone, *PHONE_SIZE, t + 2.5 / 30 + LAG_S) is None
    assert stereo.latest is None
    pc(3)
    pc(4)
    assert stereo.latest["valid"] and "left" in stereo.latest["hands"]


def test_status_carries_both_views_for_the_panel_sketch(stereo_and_dir):
    stereo, _ = stereo_and_dir
    views = stereo.status()["views"]
    assert set(views) == {"pc", "phone"}
    assert views["pc"]["size"] == list(PC_SIZE) and views["phone"]["size"] == list(PHONE_SIZE)
    x, y, score = views["phone"]["points"]["left_wrist"]
    assert 0 <= x <= 1 and 0 <= y <= 1 and score == pytest.approx(0.95)


def test_a_hand_out_of_the_computer_view_says_why(stereo_and_dir):
    stereo, _ = stereo_and_dir
    rng = np.random.default_rng(13)
    t = stereo.end_time + 30.0
    points = body(0.0, push=30.0)
    pc = to_pose_map(points, np.eye(3), np.zeros(3), F_PC, PC_SIZE, rng, 0.5)
    pc["left_wrist"]["score"] = 0.1
    for k in range(3):
        stereo.observe_pc(pc, *PC_SIZE, t + k / 30)
    result = stereo.observe_phone(to_pose_map(points, R_PHONE, T_PHONE, F_PHONE, PHONE_SIZE, rng, 0.5),
                                  *PHONE_SIZE, t + 1 / 30 + LAG_S)
    assert "left" not in result["hands"] and "手腕" in result["missing"]["left"]
    assert "right" in result["hands"]


def test_the_calibration_survives_a_restart(stereo_and_dir):
    _, tmp = stereo_and_dir
    again = StereoDepth(tmp / "stereo.json")
    assert again.state == "ready" and again.calibration["ok"]


def test_a_moved_camera_is_not_trusted(stereo_and_dir):
    stereo, _ = stereo_and_dir
    rng = np.random.default_rng(7)
    t = stereo.end_time + 40.0
    points = body(0.0)
    for k in range(3):
        stereo.observe_pc(to_pose_map(points, np.eye(3), np.zeros(3), F_PC, (640, 480), rng, 0.5), 640, 480, t + k / 30)
    result = stereo.observe_phone(to_pose_map(points, R_PHONE, T_PHONE, F_PHONE, PHONE_SIZE, rng, 0.5),
                                  *PHONE_SIZE, t + 1 / 30 + LAG_S)
    assert not result["valid"] and "重新标定" in result["reason"]


def test_a_saved_calibration_that_missed_the_arms_is_not_used(stereo_and_dir, tmp_path):
    """早期合格线松，放进来过电脑摄像头拍不全手臂的标定；读回来时要再把一次关。"""
    data = json.loads((stereo_and_dir[1] / "stereo.json").read_text(encoding="utf-8"))
    data["fit"]["bones"]["right_forearm"]["n"] = 12
    path = tmp_path / "stereo.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    stereo = StereoDepth(path)
    assert stereo.calibration is None and stereo.state == "uncalibrated"
    assert "右前臂" in stereo.message


def test_calibration_fails_when_the_computer_cannot_see_the_arms(tmp_path):
    stereo = StereoDepth(tmp_path / "stereo.json")
    rng = np.random.default_rng(21)
    start = 7000.0

    def pc(when):
        pose = to_pose_map(body(when - start), np.eye(3), np.zeros(3), F_PC, PC_SIZE, rng, 1.0)
        for name in ("left_elbow", "left_wrist", "right_elbow", "right_wrist"):
            pose[name]["score"] = 0.1
        stereo.observe_pc(pose, *PC_SIZE, when)

    for i in range(15):
        pc(start + i / 30)
    stereo.start_calibration(12, now=start + 0.5)
    events = [(start + 0.5 + i / 30, "pc") for i in range(13 * 30)]
    events += [(start + 0.54 + i / 12, "phone") for i in range(13 * 12)]
    for when, kind in sorted(events):
        if kind == "pc":
            pc(when)
        else:
            phone = to_pose_map(body(when - start), R_PHONE, T_PHONE, F_PHONE, PHONE_SIZE, rng, 1.5)
            stereo.observe_phone(phone, *PHONE_SIZE, when + LAG_S)
    deadline = time.monotonic() + 60
    while stereo.state == "solving" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert stereo.state == "failed" and stereo.calibration is None
    assert "手臂太少" in stereo.message


def test_calibration_needs_the_computer_camera(tmp_path):
    stereo = StereoDepth(tmp_path / "stereo.json")
    with pytest.raises(ValueError):
        stereo.start_calibration(30, now=5.0)


def test_too_little_movement_fails_without_losing_the_old_calibration(stereo_and_dir, tmp_path):
    """人站着不动时，几何解不出来——不能拿一份坏标定顶掉好的。"""
    stereo = StereoDepth(stereo_and_dir[1] / "stereo.json")
    rng = np.random.default_rng(9)
    start = 5000.0
    still = body(0.0)
    for i in range(30):
        stereo.observe_pc(to_pose_map(still, np.eye(3), np.zeros(3), F_PC, PC_SIZE, rng, 1.0), *PC_SIZE, start + i / 30)
    stereo.start_calibration(10, now=start + 1)
    for i in range(int(11 * 30)):
        when = start + 1 + i / 30
        stereo.observe_pc(to_pose_map(still, np.eye(3), np.zeros(3), F_PC, PC_SIZE, rng, 1.0), *PC_SIZE, when)
        if i % 3 == 0:
            stereo.observe_phone(to_pose_map(still, R_PHONE, T_PHONE, F_PHONE, PHONE_SIZE, rng, 1.5),
                                 *PHONE_SIZE, when + LAG_S)
    deadline = time.monotonic() + 60
    while stereo.state == "solving" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert stereo.state == "ready"
    assert stereo.calibration["ok"]
    assert "没通过" in stereo.message


def test_points_from_pose_keeps_missing_joints_missing():
    points = solver.points_from_pose({"left_shoulder": {"x": 0.5, "y": 0.25, "score": 0.9}}, 480, 640)
    assert points[0].tolist() == [240.0, 160.0, 0.9]
    assert np.isnan(points[1]).all()
