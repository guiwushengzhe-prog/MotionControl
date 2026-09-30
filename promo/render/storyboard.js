// 风格 demo 的分镜：三种风格共用同一份时间线、同一套模拟关键点、同一套文案，
// 只换画法。这样比较的只是风格本身，不会因为某一版动作做得好一点而偏心。
//
// 浏览器（画面）和 Node（配乐、音效）都读这一份，所以画面上脚落地的那一帧和
// 音效里"咚"的那一下天然对齐。

import { marchPose } from '../sim/rig.js';

export const FPS = 30;
export const DURATION = 15.0;
export const WIDTH = 1920;
export const HEIGHT = 1080;

export const SCENES = {
  hook: [0.0, 2.4],        // 不用手柄，站起来用身体玩
  points: [2.4, 4.6],      // 33 个关键点，本地识别
  march: [4.6, 7.6],       // 原地踏步 → 前进
  head: [7.6, 10.2],       // 转头 → 转视角
  fist: [10.2, 12.6],      // 握拳 → 鼠标
  cta: [12.6, 15.0],       // 开源，一个摄像头就能玩
};

export const COPY = {
  hookA: '不用手柄',
  hookB: '站起来，用身体玩游戏',
  pointsA: '一个摄像头 · 33 个关键点',
  pointsB: '本地识别，画面不出手机',
  march: { from: '原地踏步', to: '前进', key: '左摇杆 ↑', caption: '原地踏步，角色就往前走' },
  head: { from: '转头', to: '转视角', key: '视角', caption: '头转向哪边，视角就转到哪边' },
  fist: { from: '右手握拳', to: '鼠标', key: '指针跟手', caption: '不开游戏也能试：握拳就是鼠标' },
  ctaTitle: 'MotionControl',
  ctaLine: '开源 · Windows · 一个摄像头就能玩',
  ctaUrl: 'github.com/guiwushengzhe-prog/MotionControl',
};

const MARCH_HZ = 0.95;
const MARCH_FROM = 4.95, MARCH_TO = 7.35;
const HEAD_KEYS = [[0, 0], [0.35, 32], [1.05, 32], [1.5, -32], [2.15, -32], [2.45, 0]];
const HEAD_FROM = 7.75;
const FIST_FROM = 10.35, FIST_TO = 12.45;

// 模拟关键点的动作脚本（rig.js 的 clip 格式）。
export const CLIPS = [
  { from: MARCH_FROM, to: MARCH_TO, fade: 0.25, kind: 'march', hz: MARCH_HZ },
  { from: HEAD_FROM, to: HEAD_FROM + 2.45, fade: 0.05, kind: 'keys', keys: { headYaw: HEAD_KEYS } },
  {
    from: FIST_FROM, to: FIST_TO, fade: 0.3, kind: 'keys',
    keys: {
      // 右手往身前侧方伸出、握拳，然后画个小圈：光标跟着走
      rArmFlex: [[0, 28], [0.5, 28], [0.9, 42], [1.3, 18], [1.7, 36], [2.1, 28]],
      rArmAbd: [[0, 58], [0.5, 58], [0.9, 48], [1.3, 70], [1.7, 52], [2.1, 58]],
      rElbow: [[0, 35]],
      rFist: [[0, 0], [0.25, 0], [0.4, 1]],
    },
  },
  {
    // 结尾挥手
    from: 13.1, to: 14.9, fade: 0.3, kind: 'keys',
    keys: {
      rArmAbd: [[0, 140]],
      rElbow: [[0, 30], [0.3, 55], [0.6, 25], [0.9, 55], [1.2, 25], [1.5, 55], [1.8, 30]],
      rArmFlex: [[0, 10]],
    },
  },
];

// ---- 游戏画面的状态 ----------------------------------------------------------
// 前进距离、视角朝向、鼠标位置都是对时间的积分。先按 240 Hz 算好一张表，渲染时
// 查表——这样任意一帧单独渲染结果都一样，可以并行、可以从中间重渲。

const STEP = 1 / 240;

// 原地踏步：左右脚各完成一步之后才开始往前走（产品里就是这么判的），
// 停步后很快停下。
function walkSpeed(t) {
  const start = MARCH_FROM + 1 / MARCH_HZ;
  const stop = MARCH_TO + 0.25;
  if (t < start || t > stop + 0.5) return 0;
  const up = Math.min(1, (t - start) / 0.35);
  const down = t > stop ? Math.max(0, 1 - (t - stop) / 0.5) : 1;
  return 6.5 * up * down;  // 游戏里的"米/秒"
}

function headYawAt(t) {
  const local = t - HEAD_FROM;
  if (local < 0 || local > 2.45) return 0;
  let v = HEAD_KEYS[0][1];
  for (let i = 1; i < HEAD_KEYS.length; i += 1) {
    const [t0, v0] = HEAD_KEYS[i - 1], [t1, v1] = HEAD_KEYS[i];
    if (local <= t1) {
      const x = Math.max(0, Math.min(1, (local - t0) / (t1 - t0)));
      v = v0 + (v1 - v0) * x * x * (3 - 2 * x);
      break;
    }
    v = v1;
  }
  return v;
}

// 头控是速度型的：偏过死区才转，偏得越多转得越快。
function viewRate(t) {
  const yaw = headYawAt(t);
  const dead = 8;
  if (Math.abs(yaw) < dead) return 0;
  return Math.sign(yaw) * Math.min(1, (Math.abs(yaw) - dead) / 22) * 62;  // 度/秒
}

const TABLE = (() => {
  const n = Math.ceil(DURATION / STEP) + 2;
  const dist = new Float64Array(n), yaw = new Float64Array(n);
  for (let i = 1; i < n; i += 1) {
    const t = i * STEP;
    dist[i] = dist[i - 1] + walkSpeed(t) * STEP;
    yaw[i] = yaw[i - 1] + viewRate(t) * STEP;
  }
  return { dist, yaw };
})();

function lookup(arr, t) {
  const x = Math.max(0, Math.min(arr.length - 2, t / STEP));
  const i = Math.floor(x), f = x - i;
  return arr[i] * (1 - f) + arr[i + 1] * f;
}

export function gameState(t) {
  return {
    distance: lookup(TABLE.dist, t),
    viewYaw: lookup(TABLE.yaw, t),          // 正 = 往左看
    speed: walkSpeed(t),
    turning: viewRate(t),
    headYaw: headYawAt(t),
    fistOn: t >= FIST_FROM + 0.4 && t <= FIST_TO,
  };
}

// ---- 事件：音效和画面强调都挂在这些时刻上 ------------------------------------

export function events() {
  const list = [
    { t: 0.15, type: 'hit' },
    { t: 0.9, type: 'hit' },
    { t: 2.4, type: 'whoosh' },
    { t: 4.6, type: 'whoosh' },
    { t: 12.6, type: 'whoosh' },
    { t: 12.9, type: 'logo' },
  ];
  // 33 个点逐个出现
  for (let i = 0; i < 33; i += 1) list.push({ t: pointAppearAt(i), type: 'pop', i });
  list.push({ t: 3.25, type: 'link' });
  // 脚落地：抬起量从正回到 0 的那一刻
  let prevL = 0, prevR = 0;
  for (let t = MARCH_FROM; t <= MARCH_TO; t += 1 / 480) {
    const p = marchPose(t - MARCH_FROM, MARCH_HZ);
    if (prevL > 1 && p.lHipFlex <= 1) list.push({ t, type: 'step', foot: 'left' });
    if (prevR > 1 && p.rHipFlex <= 1) list.push({ t, type: 'step', foot: 'right' });
    prevL = p.lHipFlex; prevR = p.rHipFlex;
  }
  list.push({ t: HEAD_FROM + 0.15, type: 'turn' });
  list.push({ t: HEAD_FROM + 1.2, type: 'turn' });
  list.push({ t: FIST_FROM + 0.4, type: 'grab' });
  return list.sort((a, b) => a.t - b.t);
}

// 点出现的顺序：从头到脚，看起来像是"一层层认出来"。
const REVEAL_ORDER = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32];
export function pointAppearAt(index) {
  const order = REVEAL_ORDER.indexOf(index);
  return 2.55 + order * 0.021;
}
export const BONES_FROM = 3.25, BONES_TO = 3.8;

export function sceneOf(t) {
  for (const [name, [a, b]] of Object.entries(SCENES)) if (t >= a && t < b) return name;
  return 'cta';
}
