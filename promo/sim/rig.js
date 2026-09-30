// 模拟关键点：一个会动的三维小人，投影成 MediaPipe Pose 的 33 个点。
//
// 为什么不直接在屏幕上摆二维点：宣传片里要转头、要踏步、以后还要给双目深度做
// 演示。二维点摆出来的转头只是鼻子左右平移，耳朵不会被挡住，看着就假；换一个机位
// 更是得重摆一遍。所以这里先搭一个有骨长、有关节角的三维身体，再用针孔相机拍它——
// 转头时远侧耳朵自然被挡住，站远一点自然变小，另一台相机也是同一个身体。
//
// 坐标约定和 MediaPipe 一致：
//   世界系  X 指向人的左手边，Y 向上，Z 从人指向摄像头（米）。
//   图像系  x 向右、y 向下，按宽高归一化到 0~1；不镜像，所以人的左肩 x 更大。
//   z       以两髋中点为 0，越小越靠近镜头，量纲和 x 大致相同。
// 界面显示时一般会镜像，那是显示层的事，这里不管。
//
// 纯 ES module，不依赖任何包：浏览器里渲染画面用它，Node 里假装手机发帧也用它。

export const LANDMARK_NAMES = [
  'nose', 'left_eye_inner', 'left_eye', 'left_eye_outer',
  'right_eye_inner', 'right_eye', 'right_eye_outer', 'left_ear', 'right_ear',
  'mouth_left', 'mouth_right', 'left_shoulder', 'right_shoulder',
  'left_elbow', 'right_elbow', 'left_wrist', 'right_wrist',
  'left_pinky', 'right_pinky', 'left_index', 'right_index',
  'left_thumb', 'right_thumb', 'left_hip', 'right_hip',
  'left_knee', 'right_knee', 'left_ankle', 'right_ankle',
  'left_heel', 'right_heel', 'left_foot_index', 'right_foot_index',
];
export const L = Object.fromEntries(LANDMARK_NAMES.map((name, i) => [name, i]));

// MediaPipe 的 POSE_CONNECTIONS，原样照抄。
export const BONES = [
  [0, 1], [1, 2], [2, 3], [3, 7], [0, 4], [4, 5], [5, 6], [6, 8], [9, 10],
  [11, 12], [11, 13], [13, 15], [15, 17], [15, 19], [15, 21], [17, 19],
  [12, 14], [14, 16], [16, 18], [16, 20], [16, 22], [18, 20],
  [11, 23], [12, 24], [23, 24], [23, 25], [24, 26], [25, 27], [26, 28],
  [27, 29], [28, 30], [29, 31], [30, 32], [27, 31], [28, 32],
];

// 一个 1.75 米的人。数字取自常见人体比例表，够像就行，不追求解剖精确。
export const BODY = {
  pelvisHeight: 0.95,
  torso: 0.52,          // 髋中点到肩中点
  shoulderHalf: 0.19,
  hipHalf: 0.10,
  headUp: 0.19,         // 肩中点到两耳中点
  upperArm: 0.30,
  forearm: 0.26,
  thigh: 0.45,
  shin: 0.43,
};

// 站着不动时的关节角（度）。所有动作都是在这组数上改。
export const NEUTRAL = Object.freeze({
  rootX: 0, rootZ: 0, bounce: 0, yaw: 0, roll: 0, pitch: 0, shrug: 0,
  headYaw: 0, headPitch: 0, headRoll: 0,
  lArmAbd: 9, lArmFlex: 2, lElbow: 12, lFist: 0,
  rArmAbd: 9, rArmFlex: 2, rElbow: 12, rFist: 0,
  lHipFlex: 0, lHipAbd: 3, lKnee: 2,
  rHipFlex: 0, rHipAbd: 3, rKnee: 2,
});

// ---- 向量小工具 -----------------------------------------------------------

const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const mul = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a) => { const n = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / n, a[1] / n, a[2] / n]; };
const rad = (deg) => deg * Math.PI / 180;

function rotX(deg) { const c = Math.cos(rad(deg)), s = Math.sin(rad(deg)); return [[1, 0, 0], [0, c, -s], [0, s, c]]; }
function rotY(deg) { const c = Math.cos(rad(deg)), s = Math.sin(rad(deg)); return [[c, 0, s], [0, 1, 0], [-s, 0, c]]; }
function rotZ(deg) { const c = Math.cos(rad(deg)), s = Math.sin(rad(deg)); return [[c, -s, 0], [s, c, 0], [0, 0, 1]]; }
function mm(a, b) {
  return a.map((row) => [0, 1, 2].map((j) => row[0] * b[0][j] + row[1] * b[1][j] + row[2] * b[2][j]));
}
function mv(m, v) { return [dot(m[0], v), dot(m[1], v), dot(m[2], v)]; }

// 绕任意轴转（Rodrigues）。肘和膝是铰链，用它最省事。
function rotateAbout(v, axis, deg) {
  const a = norm(axis), c = Math.cos(rad(deg)), s = Math.sin(rad(deg));
  return add(add(mul(v, c), mul(cross(a, v), s)), mul(a, dot(a, v) * (1 - c)));
}

// ---- 身体 ------------------------------------------------------------------

function arm(shoulder, R, side, abd, flex, elbow, fist) {
  // side = +1 左臂，-1 右臂。先前举（flex）再侧举（abd）。
  const place = mm(rotZ(side * abd), rotX(-flex));
  const upper = mv(R, mv(place, [0, -1, 0]));
  // 铰链轴：手臂自然下垂时是身体左右方向，弯肘让前臂往前抬。
  const hinge = mv(R, mv(place, [-1, 0, 0]));
  const fore = rotateAbout(upper, hinge, elbow);
  const e = add(shoulder, mul(upper, BODY.upperArm));
  const w = add(e, mul(fore, BODY.forearm));
  // 手：握拳时三个指尖收回到手腕附近——产品里就是靠这个距离认拳头的。
  const out = norm(cross(fore, hinge));        // 手背朝外的方向
  const reach = 1 - 0.55 * fist;
  const lateral = mul(hinge, side * 0.022);
  const index = add(add(w, mul(fore, 0.085 * reach)), mul(lateral, -1));
  const pinky = add(add(w, mul(fore, 0.072 * reach)), lateral);
  const thumb = add(add(add(w, mul(fore, 0.045 * reach)), mul(lateral, -1.6)), mul(out, 0.012));
  return { e, w, index, pinky, thumb };
}

function leg(hip, R, side, flex, abd, knee) {
  const place = mm(rotZ(side * abd), rotX(-flex));
  const thighDir = mv(R, mv(place, [0, -1, 0]));
  const hinge = mv(R, mv(place, [1, 0, 0]));
  const shinDir = rotateAbout(thighDir, hinge, knee);
  const k = add(hip, mul(thighDir, BODY.thigh));
  const a = add(k, mul(shinDir, BODY.shin));
  // 脚掌和小腿大致垂直、朝前。
  const footDir = rotateAbout(shinDir, hinge, -90);
  const heel = add(add(a, mul(shinDir, 0.055)), mul(footDir, -0.045));
  const toe = add(add(a, mul(shinDir, 0.06)), mul(footDir, 0.16));
  return { k, a, heel, toe };
}

// 脸上的点：在头的坐标系里（x 左、y 上、z 朝前），原点是两耳中点。
const FACE = {
  nose: [0, -0.02, 0.105],
  left_eye_inner: [0.016, 0.02, 0.092], left_eye: [0.033, 0.022, 0.088], left_eye_outer: [0.048, 0.02, 0.078],
  right_eye_inner: [-0.016, 0.02, 0.092], right_eye: [-0.033, 0.022, 0.088], right_eye_outer: [-0.048, 0.02, 0.078],
  left_ear: [0.077, 0, -0.005], right_ear: [-0.077, 0, -0.005],
  mouth_left: [0.025, -0.055, 0.088], mouth_right: [-0.025, -0.055, 0.088],
};

/** 关节角 → 33 个世界坐标点（米），外加每个脸部点的朝向（算可见度用）。 */
export function skeleton3d(pose) {
  const p = { ...NEUTRAL, ...pose };
  const pelvis = [p.rootX, BODY.pelvisHeight + p.bounce, p.rootZ];
  const Ryaw = rotY(p.yaw);
  const R = mm(Ryaw, mm(rotZ(-p.roll), rotX(p.pitch)));
  const pts = new Array(33);
  const normals = {};

  const shoulderC = add(pelvis, mv(R, [0, BODY.torso + p.shrug, 0]));
  const ls = add(shoulderC, mv(R, [BODY.shoulderHalf, 0, 0]));
  const rs = add(shoulderC, mv(R, [-BODY.shoulderHalf, 0, 0]));
  const lh = add(pelvis, mv(Ryaw, [BODY.hipHalf, 0, 0]));
  const rh = add(pelvis, mv(Ryaw, [-BODY.hipHalf, 0, 0]));
  pts[L.left_shoulder] = ls; pts[L.right_shoulder] = rs;
  pts[L.left_hip] = lh; pts[L.right_hip] = rh;

  const la = arm(ls, R, +1, p.lArmAbd, p.lArmFlex, p.lElbow, p.lFist);
  const ra = arm(rs, R, -1, p.rArmAbd, p.rArmFlex, p.rElbow, p.rFist);
  pts[L.left_elbow] = la.e; pts[L.left_wrist] = la.w;
  pts[L.left_index] = la.index; pts[L.left_pinky] = la.pinky; pts[L.left_thumb] = la.thumb;
  pts[L.right_elbow] = ra.e; pts[L.right_wrist] = ra.w;
  pts[L.right_index] = ra.index; pts[L.right_pinky] = ra.pinky; pts[L.right_thumb] = ra.thumb;

  const ll = leg(lh, Ryaw, +1, p.lHipFlex, p.lHipAbd, p.lKnee);
  const rl = leg(rh, Ryaw, -1, p.rHipFlex, p.rHipAbd, p.rKnee);
  pts[L.left_knee] = ll.k; pts[L.left_ankle] = ll.a; pts[L.left_heel] = ll.heel; pts[L.left_foot_index] = ll.toe;
  pts[L.right_knee] = rl.k; pts[L.right_ankle] = rl.a; pts[L.right_heel] = rl.heel; pts[L.right_foot_index] = rl.toe;

  const Rh = mm(R, mm(rotY(p.headYaw), mm(rotX(-p.headPitch), rotZ(-p.headRoll))));
  const headC = add(shoulderC, mv(R, [0, BODY.headUp, 0.01]));
  for (const [name, local] of Object.entries(FACE)) {
    pts[L[name]] = add(headC, mv(Rh, local));
    normals[L[name]] = norm(mv(Rh, add(local, [0, 0, 0.03])));
  }
  return { pts, normals, headCenter: headC, headForward: mv(Rh, [0, 0, 1]) };
}

// ---- 相机 ------------------------------------------------------------------

/**
 * 针孔相机。默认是竖着架在人正前方 2.6 米、离地 1 米的手机。
 * vfov 是竖直视场角（度）；width/height 只用来算横向焦距和告诉接收方分辨率。
 */
export function makeCamera({
  position = [0, 1.0, 2.6], target = [0, 0.92, 0], vfov = 62, width = 720, height = 1280,
} = {}) {
  const f = norm(sub(target, position));
  const r = norm(cross(f, [0, 1, 0]));
  const u = cross(r, f);
  const fy = 0.5 / Math.tan(rad(vfov) / 2);
  const fx = fy * height / width;
  return { position, f, r, u, fx, fy, width, height };
}

// 平滑的伪随机抖动：同一时刻同一个点永远给同一个值，渲染可以重跑。
function hash(n) { const s = Math.sin(n * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); }
function smoothNoise(t, seed) {
  const i = Math.floor(t), fr = t - i, u = fr * fr * (3 - 2 * fr);
  return (hash(i + seed * 97.3) * (1 - u) + hash(i + 1 + seed * 97.3) * u) * 2 - 1;
}

/**
 * 拍一张：返回 MediaPipe 格式的 33 个点 {x, y, z, visibility}。
 * jitter 是图像上的抖动幅度（归一化单位），真人模型大约 0.002~0.004。
 */
export function project(skel, cam, { t = 0, jitter = 0 } = {}) {
  const { pts, normals } = skel;
  const hipMid = mul(add(pts[L.left_hip], pts[L.right_hip]), 0.5);
  const hipDepth = dot(sub(hipMid, cam.position), cam.f);
  return pts.map((P, i) => {
    const d = sub(P, cam.position);
    const depth = dot(d, cam.f);
    let x = 0.5 + cam.fx * dot(d, cam.r) / depth;
    let y = 0.5 - cam.fy * dot(d, cam.u) / depth;
    const z = (depth - hipDepth) * cam.fx / hipDepth;
    if (jitter) {
      x += jitter * smoothNoise(t * 9 + i * 1.7, i);
      y += jitter * smoothNoise(t * 9 + i * 2.3, i + 50);
    }
    let visibility = 0.985;
    if (normals[i]) {
      // 脸上的点背对镜头就看不见了：转头时远侧的耳朵、眼角会掉可见度。
      const toCam = norm(sub(cam.position, P));
      const facing = dot(normals[i], toCam);
      visibility = Math.max(0.05, Math.min(0.995, 0.55 + facing * 0.9));
    }
    if (x < -0.02 || x > 1.02 || y < -0.02 || y > 1.02) visibility = Math.min(visibility, 0.3);
    return { x, y, z, visibility };
  });
}

// ---- 动作脚本 --------------------------------------------------------------
//
// 一段动作 = 一个 clip：{from, to, fade, kind, ...}。每个 clip 在自己的时间段里
// 以 0→1→0 的权重把关节角拉向它想要的值，所以动作之间是自然过渡的，不会跳帧。
//
//   kind 'keys'  关键帧：{keys: {headYaw: [[0, 0], [0.4, 30], ...]}}，时间相对 clip 起点
//   kind 'march' 原地踏步：{hz}，每秒几个完整步态周期（左右各一步）

const ease = (x) => x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x);

function sampleKeys(track, local) {
  if (local <= track[0][0]) return track[0][1];
  for (let i = 1; i < track.length; i += 1) {
    const [t1, v1] = track[i];
    if (local <= t1) {
      const [t0, v0] = track[i - 1];
      return v0 + (v1 - v0) * ease((local - t0) / Math.max(1e-6, t1 - t0));
    }
  }
  return track[track.length - 1][1];
}

function idle(t) {
  const breathe = Math.sin(t * 2 * Math.PI * 0.25);
  return {
    shrug: 0.006 * breathe,
    roll: 0.8 * Math.sin(t * 0.9),
    headYaw: 2 * Math.sin(t * 0.7),
    headPitch: 1.5 * Math.sin(t * 0.5 + 1),
    lArmAbd: NEUTRAL.lArmAbd + 1.5 * breathe,
    rArmAbd: NEUTRAL.rArmAbd + 1.5 * breathe,
  };
}

/** 原地踏步的一帧。phase 以步态周期计，返回关节角和"哪只脚刚落地"。 */
export function marchPose(local, hz = 0.95) {
  const ph = local * hz * 2 * Math.PI;
  const s = Math.sin(ph);
  const lift = (v) => Math.pow(Math.max(0, v), 0.8);
  const lUp = lift(s), rUp = lift(-s);
  return {
    lHipFlex: 82 * lUp, lKnee: 100 * lUp,
    rHipFlex: 82 * rUp, rKnee: 100 * rUp,
    lArmFlex: 28 * rUp - 14 * lUp, rArmFlex: 28 * lUp - 14 * rUp,
    lElbow: 40, rElbow: 40,
    bounce: 0.022 * Math.abs(s) - 0.01,
    roll: 3 * s,
  };
}

/** 在 t 时刻，按脚本算出关节角。 */
export function poseAt(t, clips) {
  const pose = { ...NEUTRAL, ...idle(t) };
  for (const clip of clips) {
    const fade = clip.fade ?? 0.3;
    if (t < clip.from - fade || t > clip.to + fade) continue;
    const w = ease((t - (clip.from - fade)) / fade) * ease(((clip.to + fade) - t) / fade);
    if (w <= 0) continue;
    const local = t - clip.from;
    let target = {};
    if (clip.kind === 'keys') {
      for (const [name, track] of Object.entries(clip.keys)) target[name] = sampleKeys(track, local);
    } else if (clip.kind === 'march') {
      target = marchPose(Math.max(0, local), clip.hz);
    }
    for (const [name, value] of Object.entries(target)) {
      pose[name] = pose[name] + (value - pose[name]) * w;
    }
  }
  return pose;
}

/** 一步到位：t 时刻、按脚本、用这台相机拍出来的 33 个点。 */
export function landmarksAt(t, clips, cam = makeCamera(), opts = {}) {
  return project(skeleton3d(poseAt(t, clips)), cam, { t, ...opts });
}
