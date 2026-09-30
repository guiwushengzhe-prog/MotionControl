"""双目标定与三角化的数学部分：只用 numpy + OpenCV，不碰线程和文件。

用人体本身当标定物：两台相机同时看同一个人，同名关节在两边画面的像素坐标就是对应点。
解出两台相机的焦距、相对旋转、基线方向，以及两路时间戳之间的差。

两台相机都对着人时，光轴近乎相交——这正是"仅凭两视图几何定不住焦距"的退化情形。
所以把"骨长不随动作变化"作为软约束加进去。左右臂等长不参与拟合，留作独立检验。

主点固定在画面中心、忽略镜头畸变；平移只有方向（基线长度 = 1），尺度用肩宽表达。
"""

from __future__ import annotations

import math

import cv2
import numpy as np

JOINTS = (
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
)
_J = {name: index for index, name in enumerate(JOINTS)}
FIT_BONES = {
    "left_upper_arm": (_J["left_shoulder"], _J["left_elbow"]),
    "right_upper_arm": (_J["right_shoulder"], _J["right_elbow"]),
    "left_forearm": (_J["left_elbow"], _J["left_wrist"]),
    "right_forearm": (_J["right_elbow"], _J["right_wrist"]),
    "shoulders": (_J["left_shoulder"], _J["right_shoulder"]),
}
SYMMETRY = (("left_upper_arm", "right_upper_arm"), ("left_forearm", "right_forearm"))
BONE_WEIGHT_PX = 30.0  # 骨长偏离 10% 约相当于 3 像素重投影误差
MIN_SCORE = 0.6
MAX_INTERP_GAP_MS = 80.0

# 标定合格线。实测一次 45 秒标定：留出重投影中位 3.35 像素、左右上臂比 0.98。
MAX_HOLDOUT_MEDIAN_PX = 6.0
MIN_IN_FRONT = 0.95
SYMMETRY_RANGE = (0.8, 1.25)
MIN_POINTS = 300
# 手腕在手机画面里的活动范围（标准差 / 画面长边）至少这么大。
# 实测：45 秒正常活动 0.066，静止不动约 0.002。
MIN_WRIST_SPREAD = 0.03
# 每条臂骨两台相机同时拍到的帧数、骨长稳健变异系数。实测好的 45 秒标定每条 ~400 帧、
# 变异 ≤0.1；电脑摄像头拍不全手臂的那次只有 28 帧、前臂变异 0.85，焦距解成了一半。
MIN_ARM_FRAMES = 60
MAX_BONE_CV = 0.2
ARM_LABELS = {
    "left_upper_arm": "左上臂", "right_upper_arm": "右上臂",
    "left_forearm": "左前臂", "right_forearm": "右前臂",
}


def wrist_spread(points: np.ndarray, size: tuple[int, int]) -> float:
    spreads = []
    for name in ("left_wrist", "right_wrist"):
        p = points[:, _J[name]]
        p = p[np.nan_to_num(p[:, 2]) >= MIN_SCORE]
        if len(p) >= 10:
            spreads.append(float(np.max(np.std(p[:, :2], axis=0))) / max(size))
    return max(spreads, default=0.0)


def points_from_pose(pose_map: dict | None, width: float, height: float) -> np.ndarray:
    """姿态字典 -> (关节数, 3) 的像素坐标 + 置信度；缺的点为 NaN。"""
    out = np.full((len(JOINTS), 3), np.nan)
    if not pose_map:
        return out
    for index, name in enumerate(JOINTS):
        point = pose_map.get(name)
        if not point:
            continue
        x, y = point.get("x"), point.get("y")
        score = point.get("score", point.get("visibility", 0.0))
        if x is None or y is None:
            continue
        out[index] = (float(x) * width, float(y) * height, float(score or 0.0))
    return out


class StereoModel:
    """参数向量：[f_pc, f_phone, 旋转向量(3), 基线方向两角]。约定 X_phone = R·X_pc + t。"""

    def __init__(self, pc_size: tuple[int, int], phone_size: tuple[int, int]):
        self.w1, self.h1 = pc_size
        self.w2, self.h2 = phone_size

    @staticmethod
    def unpack(p: np.ndarray) -> tuple[float, float, np.ndarray, np.ndarray]:
        rotation = cv2.Rodrigues(np.asarray(p[2:5], float).reshape(3, 1))[0]
        a, b = p[5], p[6]
        t = np.array([math.cos(a) * math.cos(b), math.cos(a) * math.sin(b), math.sin(a)])
        return float(p[0]), float(p[1]), rotation, t

    @staticmethod
    def pack(f1: float, f2: float, rotation: np.ndarray, t: np.ndarray) -> np.ndarray:
        t = t / np.linalg.norm(t)
        return np.concatenate([
            [f1, f2], cv2.Rodrigues(rotation)[0].ravel(),
            [math.asin(float(np.clip(t[2], -1, 1))), math.atan2(t[1], t[0])],
        ])

    def projections(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        f1, f2, rotation, t = self.unpack(p)
        return projection_matrices(f1, f2, rotation, t, (self.w1, self.h1), (self.w2, self.h2))


def projection_matrices(f1, f2, rotation, t, pc_size, phone_size) -> tuple[np.ndarray, np.ndarray]:
    k1 = np.array([[f1, 0, pc_size[0] / 2], [0, f1, pc_size[1] / 2], [0, 0, 1.0]])
    k2 = np.array([[f2, 0, phone_size[0] / 2], [0, f2, phone_size[1] / 2], [0, 0, 1.0]])
    return (k1 @ np.hstack([np.eye(3), np.zeros((3, 1))]),
            k2 @ np.hstack([np.asarray(rotation), np.asarray(t).reshape(3, 1)]))


def triangulate(pa: np.ndarray, pb: np.ndarray, xa: np.ndarray, xb: np.ndarray) -> np.ndarray:
    """线性三角化（DLT），一次处理多点。"""
    a = np.stack([
        xa[:, 0:1] * pa[2] - pa[0], xa[:, 1:2] * pa[2] - pa[1],
        xb[:, 0:1] * pb[2] - pb[0], xb[:, 1:2] * pb[2] - pb[1]], axis=1)
    x = np.linalg.svd(a)[2][:, -1]
    return x[:, :3] / x[:, 3:4]


def project(p: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    h = x @ p[:, :3].T + p[:, 3]
    return h[:, :2] / h[:, 2:3], h[:, 2]


def interpolate(times_ms: np.ndarray, frames: np.ndarray, query_ms: np.ndarray) -> np.ndarray:
    """在相邻两帧之间线性插值；两侧帧都可信且间隔不超过 MAX_INTERP_GAP_MS 才有效。"""
    out = np.full((len(query_ms), frames.shape[1], 2), np.nan)
    if len(times_ms) < 2:
        return out
    j = np.searchsorted(times_ms, query_ms)
    valid = (j > 0) & (j < len(times_ms))
    j = np.clip(j, 1, len(times_ms) - 1)
    t0, t1 = times_ms[j - 1], times_ms[j]
    valid &= (t1 - t0) <= MAX_INTERP_GAP_MS
    w = ((query_ms - t0) / np.maximum(t1 - t0, 1e-9))[:, None, None]
    a, b = frames[j - 1], frames[j]
    ok = valid[:, None] & (a[..., 2] >= MIN_SCORE) & (b[..., 2] >= MIN_SCORE)
    xy = a[..., :2] * (1 - w) + b[..., :2] * w
    out[ok] = xy[ok]
    return out


def _correspondences(pc_xy, phone, pc_size, phone_size):
    ok = np.isfinite(pc_xy[..., 0]) & (np.nan_to_num(phone[..., 2]) >= MIN_SCORE)
    ok &= (pc_xy[..., 0] > 0) & (pc_xy[..., 0] < pc_size[0]) & (pc_xy[..., 1] > 0) & (pc_xy[..., 1] < pc_size[1])
    ok &= (phone[..., 0] > 0) & (phone[..., 0] < phone_size[0]) & (phone[..., 1] > 0) & (phone[..., 1] < phone_size[1])
    frames, joints = np.nonzero(ok)
    return frames, joints, pc_xy[frames, joints], phone[frames, joints, :2]


def _bone_pairs(frames: np.ndarray, joints: np.ndarray) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    index = {(int(f), int(j)): i for i, (f, j) in enumerate(zip(frames, joints))}
    pairs = {}
    for name, (a, b) in FIT_BONES.items():
        ia, ib = [], []
        for f in np.unique(frames):
            if (f, a) in index and (f, b) in index:
                ia.append(index[(f, a)])
                ib.append(index[(f, b)])
        pairs[name] = (np.array(ia, int), np.array(ib, int))
    return pairs


def _residuals(model, p, x1, x2, bones):
    pa, pb = model.projections(p)
    x = triangulate(pa, pb, x1, x2)
    r1, _ = project(pa, x)
    r2, _ = project(pb, x)
    reproj = np.hstack([r1 - x1, r2 - x2])
    extra = []
    for ia, ib in (bones or {}).values():
        if len(ia) >= 10:
            length = np.linalg.norm(x[ia] - x[ib], axis=1)
            med = np.median(length)
            extra.append(BONE_WEIGHT_PX * (length - med) / max(med, 1e-9))
    return reproj, (np.concatenate(extra) if extra else np.zeros(0))


def _fit(model, p, x1, x2, bones, fix_focal, iters=80):
    """带 Huber 权重的 Levenberg-Marquardt；只有 7 个参数，三维点每次按当前参数重新三角化。"""
    free = [k for k in range(p.size) if not (fix_focal and k < 2)]

    def weighted(q, w_r, w_b):
        r, b = _residuals(model, q, x1, x2, bones)
        return np.concatenate([(r * w_r[:, None]).ravel(), b * w_b])

    lam = 1e-3
    for _ in range(iters):
        r, b = _residuals(model, p, x1, x2, bones)
        e = np.linalg.norm(r, axis=1)
        s = 1.4826 * np.median(e) + 1e-9
        w_r = np.sqrt(np.where(e < 2 * s, 1.0, 2 * s / np.maximum(e, 1e-12)))
        if b.size:
            s_b = 1.4826 * np.median(np.abs(b)) + 1e-9
            w_b = np.sqrt(np.where(np.abs(b) < 2 * s_b, 1.0, 2 * s_b / np.maximum(np.abs(b), 1e-12)))
        else:
            w_b = b
        v = weighted(p, w_r, w_b)
        jac = np.zeros((v.size, len(free)))
        for col, k in enumerate(free):
            d = np.zeros_like(p)
            d[k] = 1e-3 * abs(p[k]) if k < 2 else 1e-6
            jac[:, col] = (weighted(p + d, w_r, w_b) - weighted(p - d, w_r, w_b)) / (2 * d[k])
        h = jac.T @ jac
        g = jac.T @ v
        cost = v @ v
        while True:
            step = np.zeros_like(p)
            step[free] = np.linalg.solve(h + lam * (np.diag(np.diag(h)) + 1e-9 * np.eye(len(free))), -g)
            trial = weighted(p + step, w_r, w_b)
            if trial @ trial < cost:
                p = p + step
                lam = max(lam / 4, 1e-9)
                break
            lam *= 5
            if lam > 1e10:
                return p
        if np.linalg.norm(step) < 1e-9 * (1 + np.linalg.norm(p)):
            break
    return p


def _essential_init(model, f1, f2, x1, x2):
    n1 = (x1 - [model.w1 / 2, model.h1 / 2]) / f1
    n2 = (x2 - [model.w2 / 2, model.h2 / 2]) / f2
    e, inliers = cv2.findEssentialMat(n1, n2, np.eye(3), cv2.LMEDS)
    if e is None or e.shape != (3, 3):
        return None, math.inf
    _, rotation, t, _ = cv2.recoverPose(e, n1, n2, np.eye(3), mask=inliers.copy())
    h1 = np.column_stack([n1, np.ones(len(n1))])
    h2 = np.column_stack([n2, np.ones(len(n2))])
    ex1 = h1 @ e.T
    etx2 = h2 @ e
    num = np.sum(h2 * ex1, axis=1) ** 2
    den = ex1[:, 0] ** 2 + ex1[:, 1] ** 2 + etx2[:, 0] ** 2 + etx2[:, 1] ** 2
    err = np.sqrt(num / np.maximum(den, 1e-18)) * f1
    return StereoModel.pack(f1, f2, rotation, t.ravel()), float(np.median(err))


def _evaluate(model, p, frames, joints, x1, x2) -> dict:
    pa, pb = model.projections(p)
    x = triangulate(pa, pb, x1, x2)
    r1, z1 = project(pa, x)
    r2, z2 = project(pb, x)
    e = np.maximum(np.linalg.norm(r1 - x1, axis=1), np.linalg.norm(r2 - x2, axis=1))
    lengths = {}
    for name, (ia, ib) in _bone_pairs(frames, joints).items():
        if len(ia) >= 5:
            length = np.linalg.norm(x[ia] - x[ib], axis=1)
            med = float(np.median(length))
            lengths[name] = {"n": int(len(ia)), "median": med,
                             "robust_cv": float(1.4826 * np.median(np.abs(length - med)) / med)}
    symmetry = {f"{a}/{b}": lengths[a]["median"] / lengths[b]["median"]
                for a, b in SYMMETRY if a in lengths and b in lengths}
    return {
        "points": int(len(e)),
        "reprojection_median_px": float(np.median(e)) if len(e) else math.inf,
        "reprojection_p90_px": float(np.percentile(e, 90)) if len(e) else math.inf,
        "in_front_ratio": float(np.mean((z1 > 0) & (z2 > 0))) if len(e) else 0.0,
        "bones": lengths,
        "left_right_ratio": symmetry,
    }


def _torso_up(model, p, frames, joints, x1, x2) -> list[float] | None:
    """标定期间躯干"上"方向（电脑相机坐标）：髋中点指向肩中点的中位方向。

    人是站着标定的，它近似重力反方向。用它定"前"，手抬高放低就不会被算成往前推。
    """
    pa, pb = model.projections(p)
    x = triangulate(pa, pb, x1, x2)
    need = [_J[n] for n in ("left_shoulder", "right_shoulder", "left_hip", "right_hip")]
    ups = []
    for f in np.unique(frames):
        pts = {int(joints[i]): x[i] for i in np.flatnonzero(frames == f)}
        if all(j in pts for j in need):
            v = (pts[need[0]] + pts[need[1]]) / 2 - (pts[need[2]] + pts[need[3]]) / 2
            if np.linalg.norm(v) > 0:
                ups.append(v / np.linalg.norm(v))
    if len(ups) < 20:
        return None
    u = np.median(np.array(ups), axis=0)
    return (u / np.linalg.norm(u)).tolist()


def acceptance_reasons(result: dict) -> list[str]:
    """标定可不可信。新算出来的和读回来的旧标定文件都过这一关。"""
    fit, hold = result.get("fit") or {}, result.get("holdout") or {}
    reasons = []
    error = hold.get("reprojection_median_px", math.inf)
    if error > MAX_HOLDOUT_MEDIAN_PX:
        reasons.append(f"留出数据重投影误差 {error:.1f} 像素，超过 {MAX_HOLDOUT_MEDIAN_PX:.0f}")
    if hold.get("in_front_ratio", 0.0) < MIN_IN_FRONT:
        reasons.append("部分关节被算到了相机背后")
    bones = fit.get("bones") or {}
    short = [label for name, label in ARM_LABELS.items() if bones.get(name, {}).get("n", 0) < MIN_ARM_FRAMES]
    if short:
        reasons.append("两台相机同时拍到的手臂太少（" + "、".join(short) + "）：电脑摄像头和手机都要能拍到双手")
    shaky = [label for name, label in ARM_LABELS.items()
             if name in bones and label not in short and bones[name].get("robust_cv", math.inf) > MAX_BONE_CV]
    if shaky:
        reasons.append("算出来的" + "、".join(shaky) + "长度忽长忽短")
    for ratio in (fit.get("left_right_ratio") or {}).values():
        if not SYMMETRY_RANGE[0] <= ratio <= SYMMETRY_RANGE[1]:
            reasons.append(f"左右臂长度比 {ratio:.2f} 不合理")
    shoulders = result.get("shoulder_width")
    if not shoulders or not math.isfinite(shoulders) or shoulders <= 0:
        reasons.append("没有算出肩宽")
    return reasons


def solve(
    pc_times_ms: np.ndarray, pc_points: np.ndarray, pc_size: tuple[int, int],
    phone_times_ms: np.ndarray, phone_points: np.ndarray, phone_size: tuple[int, int],
    offset_range_ms: tuple[float, float] = (-300.0, 200.0),
) -> dict:
    """由一段两路同步的关键点解出双目参数。返回 dict，ok=False 时 reason 说明原因。

    时间约定：手机时刻 + offset_ms = 电脑时刻。
    """
    order = np.argsort(pc_times_ms)
    pc_times_ms, pc_points = np.asarray(pc_times_ms)[order], np.asarray(pc_points)[order]
    phone_points = np.asarray(phone_points)
    # 人不动时所有对应点挤在一个小刚体上，几何根本定不住，拟合出来的数只是巧合。
    spread = wrist_spread(phone_points, phone_size)
    if spread < MIN_WRIST_SPREAD:
        return {"ok": False, "reason": "动作太少：标定时要活动双臂（往前推、往两侧伸、举高）"}
    model = StereoModel(pc_size, phone_size)
    f1 = 0.8 * max(pc_size)
    f2 = 0.8 * max(phone_size)

    def at_offset(s):
        pc_xy = interpolate(pc_times_ms, pc_points, np.asarray(phone_times_ms) + s)
        return _correspondences(pc_xy, phone_points, pc_size, phone_size)

    scan = []
    for s in np.arange(offset_range_ms[0], offset_range_ms[1] + 1e-9, 10.0):
        frames, _, x1, x2 = at_offset(float(s))
        if len(np.unique(frames)) >= 20:
            scan.append((float(s), _essential_init(model, f1, f2, x1, x2)[1]))
    if not scan:
        return {"ok": False, "reason": "两台相机没有同时拍到足够的肩、肘、腕"}
    best = min(scan, key=lambda v: v[1])[0]
    fine = []
    for s in np.arange(best - 15, best + 15.1, 2.5):
        frames, _, x1, x2 = at_offset(float(s))
        fine.append((float(s), _essential_init(model, f1, f2, x1, x2)[1]))
    offset = min(fine, key=lambda v: v[1])[0]

    frames, joints, x1, x2 = at_offset(offset)
    if len(x1) < MIN_POINTS:
        return {"ok": False, "reason": f"双侧同时可见的关节点只有 {len(x1)} 个，至少要 {MIN_POINTS} 个"}
    t_rel = np.asarray(phone_times_ms)[frames] - float(np.min(phone_times_ms))
    holdout = (np.floor(t_rel / 3000.0) % 4) == 3  # 每 12 秒留出 3 秒，只用来检验
    fit_idx = ~holdout
    p0, _ = _essential_init(model, f1, f2, x1[fit_idx], x2[fit_idx])
    if p0 is None:
        return {"ok": False, "reason": "无法从对应点估计相对姿态"}
    bones = _bone_pairs(frames[fit_idx], joints[fit_idx])
    p = _fit(model, p0, x1[fit_idx], x2[fit_idx], bones, fix_focal=True)
    p = _fit(model, p, x1[fit_idx], x2[fit_idx], bones, fix_focal=False)
    f1, f2, rotation, t = StereoModel.unpack(p)

    fit_report = _evaluate(model, p, frames[fit_idx], joints[fit_idx], x1[fit_idx], x2[fit_idx])
    hold_report = _evaluate(model, p, frames[holdout], joints[holdout], x1[holdout], x2[holdout])
    shoulders = fit_report["bones"].get("shoulders", {}).get("median")
    result = {
        "pc_size": list(pc_size),
        "phone_size": list(phone_size),
        "f_pc": f1,
        "f_phone": f2,
        "rotation_phone_from_pc": rotation.tolist(),
        "translation_phone_from_pc": t.tolist(),
        "phone_to_pc_offset_ms": offset,
        "shoulder_width": shoulders,
        "bone_medians": {k: v["median"] for k, v in fit_report["bones"].items()},
        "optical_axis_angle_deg": math.degrees(math.acos(float(np.clip(rotation[2, 2], -1, 1)))),
        "torso_up_pc": _torso_up(model, p, frames[fit_idx], joints[fit_idx], x1[fit_idx], x2[fit_idx]),
        "fit": fit_report,
        "holdout": hold_report,
    }
    reasons = acceptance_reasons(result)
    return {"ok": not reasons, "reason": "；".join(reasons), **result}
