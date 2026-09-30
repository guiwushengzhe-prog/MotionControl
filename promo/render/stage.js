// 风格 demo 的画面：同一份分镜，三种画法。
//
// renderAt(t) 是纯函数——给定时刻就画出那一帧，不依赖上一帧。录制脚本逐帧调用它
// 截图，浏览器里 ?play=1 也能直接预览。

import { BONES, L, makeCamera, poseAt, project, skeleton3d } from '../sim/rig.js';
import * as SB from './storyboard.js';

const W = SB.WIDTH, H = SB.HEIGHT;
const CAM = makeCamera({ vfov: 46, target: [0, 0.88, 0] });

// ---- 小工具 ------------------------------------------------------------------

const clamp01 = (x) => Math.max(0, Math.min(1, x));
const lerp = (a, b, p) => a + (b - a) * p;
const prog = (t, t0, dur) => clamp01((t - t0) / dur);
const easeOut = (p) => 1 - Math.pow(1 - p, 3);
const easeInOut = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
const easeBack = (p) => { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(p - 1, 3) + c1 * Math.pow(p - 1, 2); };

function mulberry(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let x = Math.imul(a ^ (a >>> 15), 1 | a);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
}
const hash = (n) => { const s = Math.sin(n * 91.7 + 17.3) * 43758.5453; return s - Math.floor(s); };

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function text(ctx, str, x, y, { font, fill, align = 'left', baseline = 'alphabetic', spacing = 0, stroke, strokeWidth = 0, alpha = 1 }) {
  ctx.save();
  ctx.globalAlpha *= alpha;
  ctx.font = font;
  ctx.textAlign = align;
  ctx.textBaseline = baseline;
  ctx.letterSpacing = `${spacing}px`;
  if (stroke && strokeWidth) {
    ctx.lineJoin = 'round';
    ctx.lineWidth = strokeWidth;
    ctx.strokeStyle = stroke;
    ctx.strokeText(str, x, y);
  }
  if (fill) { ctx.fillStyle = fill; ctx.fillText(str, x, y); }
  ctx.restore();
}

function measure(ctx, str, font, spacing = 0) {
  ctx.save();
  ctx.font = font;
  ctx.letterSpacing = `${spacing}px`;
  const w = ctx.measureText(str).width;
  ctx.restore();
  return w;
}

// ---- 分镜推导出的东西：布局、关键点、游戏状态、强调脉冲 -------------------------

const EVENTS = SB.events();
function pulse(t, type, decay = 0.35) {
  let best = 0;
  for (const e of EVENTS) {
    if (e.type !== type || e.t > t) continue;
    best = Math.max(best, Math.exp(-(t - e.t) / decay * 3));
  }
  return best;
}
function lastEvent(t, type) {
  let found = null;
  for (const e of EVENTS) if (e.type === type && e.t <= t) found = e;
  return found;
}

const PANEL_H = 860, PANEL_W = Math.round(PANEL_H * 9 / 16);
const GAME = { x: 800, y: 110, w: 1040, h: 585 };

function layout(t) {
  // 摄像头画面的中心 x：先在右边亮相，踏步时让到左边给游戏画面腾位置，结尾回到右边。
  let cx = 1330;
  const toLeft = easeInOut(prog(t, 4.6, 0.55));
  const toRight = easeInOut(prog(t, 12.6, 0.6));
  cx = lerp(lerp(1330, 440, toLeft), 1400, toRight);
  const panelIn = easeOut(prog(t, 2.35, 0.45));
  const panel = { x: cx - PANEL_W / 2, y: (H - PANEL_H) / 2 + (1 - panelIn) * 40, w: PANEL_W, h: PANEL_H, alpha: panelIn };
  const gameIn = easeOut(prog(t, 4.75, 0.55)) * (1 - easeInOut(prog(t, 12.55, 0.4)));
  const game = { ...GAME, x: GAME.x + (1 - easeOut(prog(t, 4.75, 0.55))) * 260, alpha: gameIn };
  return { panel, game };
}

function landmarks(t) {
  return project(skeleton3d(poseAt(t, SB.CLIPS)), CAM, { t, jitter: 0.0012 });
}

function toScreen(lms, rect) {
  // 显示时镜像，和软件界面一样：人的右手在画面右边。
  return lms.map((p) => ({ x: rect.x + (1 - p.x) * rect.w, y: rect.y + p.y * rect.h, v: p.visibility }));
}

const FIST_GRAB = 10.75;
function cursorAt(t) {
  const now = landmarks(t)[L.right_wrist];
  const grab = landmarks(FIST_GRAB)[L.right_wrist];
  // 画面是镜像的，所以 x 取反；增益按这段动作的实际手腕行程定，保证光标不出框
  const clampC = (v) => Math.max(0.08, Math.min(0.92, v));
  return {
    x: clampC(0.5 - (now.x - grab.x) * 3.8),
    y: clampC(0.5 + (now.y - grab.y) * 10),
  };
}

// 每一幕在强调哪一组骨头
const GROUPS = {
  legs: new Set([23, 24, 25, 26, 27, 28, 29, 30, 31, 32]),
  head: new Set([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]),
  rightArm: new Set([12, 14, 16, 18, 20, 22]),
};
function activeGroup(t) {
  if (t >= 4.9 && t < 7.6) return GROUPS.legs;
  if (t >= 7.6 && t < 10.2) return GROUPS.head;
  if (t >= 10.2 && t < 12.6) return GROUPS.rightArm;
  return null;
}
const boneActive = (group, a, b) => group && group.has(a) && group.has(b);

// 头：两耳中点做圆心，耳距定半径。转头时鼻子相对圆心的偏移就是"朝向"。
function headGeom(pts) {
  const lEar = pts[L.left_ear], rEar = pts[L.right_ear], nose = pts[L.nose];
  const ls = pts[L.left_shoulder], rs = pts[L.right_shoulder];
  const cx = (lEar.x + rEar.x) / 2, cy = (lEar.y + rEar.y) / 2 - 8;
  // 用肩宽定头的大小：转头时耳距会变短，拿它定半径头会跟着缩
  const r = Math.hypot(ls.x - rs.x, ls.y - rs.y) * 0.3;
  const look = Math.max(-1, Math.min(1, (nose.x - cx) / (r * 0.6)));
  return { x: cx, y: cy, r, look };
}
const headBones = (a, b) => a <= 10 && b <= 10;

// 点和骨头的"识别"动画：点一个个冒出来，然后连线。
function revealState(t) {
  const pts = [];
  for (let i = 0; i < 33; i += 1) {
    const t0 = SB.pointAppearAt(i);
    pts.push(t < t0 ? 0 : t0 + 0.2 < t ? 1 : easeBack(prog(t, t0, 0.2)));
  }
  const bones = BONES.map((_, k) => easeOut(prog(t, SB.BONES_FROM + k * 0.012, 0.25)));
  return { pts, bones };
}

// 游戏画面的简易三维：地面网格 + 两排立柱，前进/转视角都是真算的。
const WORLD = (() => {
  const r = mulberry(7);
  const items = [];
  for (let k = 0; k < 40; k += 1) {
    for (const side of [-1, 1]) {
      items.push({ x: side * (3.2 + r() * 7), z: 5 + k * 5.5 + r() * 3, h: 1.6 + r() * 3.2, kind: Math.floor(r() * 3), seed: r() });
    }
  }
  return items;
})();

function gameCamera(rect, gs) {
  const yaw = gs.viewYaw * Math.PI / 180;
  const f = rect.w * 0.52;
  const horizon = rect.y + rect.h * 0.46;
  const cx = rect.x + rect.w / 2;
  const fwd = [-Math.sin(yaw), Math.cos(yaw)], right = [Math.cos(yaw), Math.sin(yaw)];
  return {
    f, horizon, cx, yaw,
    proj(x, y, z) {
      const rx = x, rz = z - gs.distance;
      const cxv = rx * right[0] + rz * right[1];
      const czv = rx * fwd[0] + rz * fwd[1];
      if (czv < 0.35) return null;
      return { x: cx + f * cxv / czv, y: horizon - f * (y - 1.7) / czv, d: czv, s: f / czv };
    },
    // 线段近平面裁剪后投影
    line(a, b) {
      const toCam = (p) => {
        const rx = p[0], rz = p[2] - gs.distance;
        return [rx * right[0] + rz * right[1], p[1], rx * fwd[0] + rz * fwd[1]];
      };
      let A = toCam(a), B = toCam(b);
      const near = 0.35;
      if (A[2] < near && B[2] < near) return null;
      if (A[2] < near || B[2] < near) {
        const k = (near - A[2]) / (B[2] - A[2]);
        const C = [A[0] + (B[0] - A[0]) * k, A[1] + (B[1] - A[1]) * k, near];
        if (A[2] < near) A = C; else B = C;
      }
      const P = (c) => ({ x: cx + f * c[0] / c[2], y: horizon - f * (c[1] - 1.7) / c[2] });
      return [P(A), P(B)];
    },
  };
}

function visibleObjects(gc, gs) {
  const out = [];
  for (const o of WORLD) {
    const base = gc.proj(o.x, 0, o.z);
    if (!base || base.d > 70) continue;
    const top = gc.proj(o.x, o.h, o.z);
    out.push({ ...o, base, top });
  }
  return out.sort((a, b) => b.base.d - a.base.d);
}

// ---- 公共的场景调度 ------------------------------------------------------------

function sceneCopy(t) {
  if (t < 7.6) return SB.COPY.march;
  if (t < 10.2) return SB.COPY.head;
  return SB.COPY.fist;
}
function chipAlpha(t) {
  const segs = [[4.95, 7.55], [7.65, 10.15], [10.25, 12.55]];
  for (const [a, b] of segs) {
    if (t >= a - 0.05 && t <= b + 0.05) return { p: easeBack(prog(t, a, 0.4)), out: 1 - prog(t, b - 0.15, 0.2), t0: a };
  }
  return null;
}
function chipPulse(t) {
  if (t < 7.6) return pulse(t, 'step', 0.4);
  if (t < 10.2) return Math.min(1, Math.abs(SB.gameState(t).turning) / 62);
  return SB.gameState(t).fistOn ? 0.55 + 0.45 * pulse(t, 'grab', 0.5) : 0;
}

function renderScene(ctx, t, S) {
  const lay = layout(t);
  const gs = SB.gameState(t);
  S.background(ctx, t, gs);

  // 开场大字
  if (t < 2.45) S.hook(ctx, t);

  // 33 个点的说明文字
  if (t >= 2.5 && t < 4.75) S.pointsText(ctx, t);

  // 游戏画面
  if (lay.game.alpha > 0.001) {
    const cursor = t >= FIST_GRAB - 0.3 && t < 12.6 ? cursorAt(Math.max(t, FIST_GRAB)) : null;
    S.game(ctx, lay.game, gs, t, cursor);
  }

  // 摄像头画面和骨架
  if (lay.panel.alpha > 0.001) {
    S.panel(ctx, lay.panel, t);
    const pts = toScreen(landmarks(t), lay.panel);
    const reveal = t < SB.BONES_TO + 0.6 ? revealState(t) : null;
    S.skeleton(ctx, pts, { t, reveal, active: activeGroup(t), rect: lay.panel });
  }

  // 动作 → 输出 的小标签和字幕
  const chip = chipAlpha(t);
  if (chip) {
    const copy = sceneCopy(t);
    S.chip(ctx, lay.game.x + lay.game.w / 2, 812, copy, { ...chip, pulse: chipPulse(t), t });
    S.caption(ctx, lay.game.x + lay.game.w / 2, 950, copy.caption, { ...chip, t });
  }

  if (t >= 12.75) S.cta(ctx, t);
  if (S.overlay) S.overlay(ctx, t, lay);
}

// ================================================================================
// 风格一：霓虹竞技
// ================================================================================

const NEON = (() => {
  const C = { bg: '#04060c', cyan: '#3fe6ff', mag: '#ff3ea5', lime: '#c6ff3d', text: '#eef7ff', dim: '#61708c' };
  const TITLE = '"ZCOOL QingKe HuangYou", "Noto Sans SC", sans-serif';
  const BODY = '"Noto Sans SC", sans-serif';
  const MONO = '"JetBrains Mono", monospace';
  let scan = null;

  function glowLine(ctx, a, b, color, width, blur) {
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineCap = 'round';
    ctx.shadowColor = color; ctx.shadowBlur = blur;
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }

  function glitchText(ctx, str, x, y, size, p, t, align = 'left') {
    const font = `400 ${size}px ${TITLE}`;
    const jitter = p < 1 ? (1 - p) : 0;
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    const off = 3 + jitter * 22;
    text(ctx, str, x - off, y, { font, fill: C.cyan, align, alpha: 0.75 * p });
    text(ctx, str, x + off, y, { font, fill: C.mag, align, alpha: 0.75 * p });
    ctx.restore();
    // 故障切片：入场的前几帧把字切成几条横向错位
    if (jitter > 0.05) {
      const w = measure(ctx, str, font) + 40;
      const left = align === 'center' ? x - w / 2 : x - 20;
      const r = mulberry(Math.floor(t * 30) + 11);
      for (let i = 0; i < 5; i += 1) {
        const sy = y - size + r() * size * 1.1, sh = 6 + r() * 18;
        ctx.save();
        ctx.beginPath(); ctx.rect(left, sy, w, sh); ctx.clip();
        text(ctx, str, x + (r() - 0.5) * 60 * jitter, y, { font, fill: C.text, align, alpha: p });
        ctx.restore();
      }
    } else {
      ctx.save();
      ctx.shadowColor = C.cyan; ctx.shadowBlur = 24;
      text(ctx, str, x, y, { font, fill: C.text, align, alpha: p });
      ctx.restore();
    }
  }

  return {
    name: 'neon',
    background(ctx, t, gs) {
      ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
      let g = ctx.createRadialGradient(260, 120, 0, 260, 120, 900);
      g.addColorStop(0, 'rgba(255,62,165,0.16)'); g.addColorStop(1, 'rgba(255,62,165,0)');
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
      g = ctx.createRadialGradient(1700, 980, 0, 1700, 980, 1000);
      g.addColorStop(0, 'rgba(63,230,255,0.14)'); g.addColorStop(1, 'rgba(63,230,255,0)');
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
      // 地面透视网格
      const hz = 760, vpx = W / 2;
      ctx.save();
      ctx.strokeStyle = 'rgba(63,230,255,0.13)'; ctx.lineWidth = 1.5;
      for (let i = -14; i <= 14; i += 1) {
        ctx.beginPath(); ctx.moveTo(vpx + i * 12, hz); ctx.lineTo(vpx + i * 260, H + 40); ctx.stroke();
      }
      const scroll = (t * 0.6 + gs.distance * 0.25) % 1;
      for (let k = 0; k < 12; k += 1) {
        const z = 1 + (k - scroll) * 1.0;
        if (z <= 0.2) continue;
        const y = hz + 320 / z;
        if (y > H) continue;
        ctx.globalAlpha = Math.min(1, (y - hz) / 120);
        ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
      }
      ctx.restore();
      // 漂浮的光点
      for (let i = 0; i < 70; i += 1) {
        const x = hash(i) * W, speed = 20 + hash(i + 7) * 60;
        const y = (hash(i + 3) * H - t * speed + H * 10) % H;
        ctx.fillStyle = i % 3 ? 'rgba(63,230,255,0.5)' : 'rgba(255,62,165,0.5)';
        ctx.fillRect(x, y, 2, 2);
      }
    },

    hook(ctx, t) {
      const out = 1 - prog(t, 2.2, 0.22);
      const p1 = prog(t, 0.15, 0.35), p2 = prog(t, 0.9, 0.35);
      ctx.save(); ctx.globalAlpha = out;
      if (p1 > 0) {
        text(ctx, 'NO CONTROLLER', W / 2, 330, { font: `700 30px ${MONO}`, fill: C.mag, align: 'center', spacing: 14, alpha: p1 });
        glitchText(ctx, SB.COPY.hookA, W / 2, 520, 190, p1, t, 'center');
      }
      if (p2 > 0) {
        text(ctx, SB.COPY.hookB, W / 2, 680, { font: `700 64px ${BODY}`, fill: C.cyan, align: 'center', spacing: 8, alpha: p2 });
        const w = 720 * easeOut(p2);
        ctx.save(); ctx.shadowColor = C.cyan; ctx.shadowBlur = 16;
        ctx.fillStyle = C.cyan; ctx.fillRect(W / 2 - w / 2, 720, w, 3); ctx.restore();
      }
      ctx.restore();
    },

    pointsText(ctx, t) {
      const p1 = prog(t, 2.6, 0.3), p2 = prog(t, 3.4, 0.3), out = 1 - prog(t, 4.45, 0.25);
      ctx.save(); ctx.globalAlpha = out;
      text(ctx, '> POSE TRACKING', 160, 330, { font: `700 26px ${MONO}`, fill: C.mag, spacing: 6, alpha: p1 });
      glitchText(ctx, '33 个关键点', 150, 480, 120, p1, t);
      text(ctx, '一个摄像头就够', 160, 570, { font: `700 48px ${BODY}`, fill: C.text, alpha: p1 });
      if (p2 > 0) {
        roundRect(ctx, 160, 630, 560, 76, 10);
        ctx.strokeStyle = C.cyan; ctx.lineWidth = 2; ctx.globalAlpha = out * p2; ctx.stroke(); ctx.globalAlpha = out;
        text(ctx, '● ' + SB.COPY.pointsB, 190, 680, { font: `500 34px ${BODY}`, fill: C.cyan, alpha: p2 });
        const n = Math.min(33, Math.floor(prog(t, 2.55, 0.75) * 33));
        text(ctx, `LANDMARKS ${String(n).padStart(2, '0')}/33   ON-DEVICE   0 FRAMES UPLOADED`, 160, 760, { font: `500 22px ${MONO}`, fill: C.dim, alpha: p2 });
      }
      ctx.restore();
    },

    panel(ctx, r, t) {
      ctx.save(); ctx.globalAlpha = r.alpha;
      ctx.fillStyle = 'rgba(8,16,30,0.72)'; ctx.fillRect(r.x, r.y, r.w, r.h);
      // 扫描线
      ctx.save(); ctx.beginPath(); ctx.rect(r.x, r.y, r.w, r.h); ctx.clip();
      const sy = r.y + ((t * 0.45) % 1) * r.h;
      const g = ctx.createLinearGradient(0, sy - 80, 0, sy);
      g.addColorStop(0, 'rgba(63,230,255,0)'); g.addColorStop(1, 'rgba(63,230,255,0.18)');
      ctx.fillStyle = g; ctx.fillRect(r.x, sy - 80, r.w, 80);
      ctx.restore();
      ctx.strokeStyle = C.cyan; ctx.lineWidth = 3; ctx.shadowColor = C.cyan; ctx.shadowBlur = 12;
      const k = 44;
      for (const [x, y, dx, dy] of [[r.x, r.y, 1, 1], [r.x + r.w, r.y, -1, 1], [r.x, r.y + r.h, 1, -1], [r.x + r.w, r.y + r.h, -1, -1]]) {
        ctx.beginPath(); ctx.moveTo(x + dx * k, y); ctx.lineTo(x, y); ctx.lineTo(x, y + dy * k); ctx.stroke();
      }
      ctx.shadowBlur = 0;
      ctx.strokeStyle = 'rgba(63,230,255,0.25)'; ctx.lineWidth = 1; ctx.strokeRect(r.x, r.y, r.w, r.h);
      const blink = Math.floor(t * 2) % 2 === 0;
      if (blink) { ctx.fillStyle = C.mag; ctx.beginPath(); ctx.arc(r.x + 28, r.y + 32, 7, 0, Math.PI * 2); ctx.fill(); }
      text(ctx, 'CAM 01 · 本地识别', r.x + 46, r.y + 40, { font: `500 20px ${MONO}`, fill: C.cyan });
      text(ctx, '33 PTS', r.x + r.w - 22, r.y + 40, { font: `700 20px ${MONO}`, fill: C.lime, align: 'right' });
      ctx.restore();
    },

    skeleton(ctx, pts, { t, reveal, active, rect }) {
      ctx.save(); ctx.globalAlpha = rect.alpha;
      BONES.forEach(([a, b], k) => {
        const pr = reveal ? reveal.bones[k] : 1;
        if (pr <= 0) return;
        const A = pts[a], B = pts[b];
        const end = { x: lerp(A.x, B.x, pr), y: lerp(A.y, B.y, pr) };
        const on = boneActive(active, a, b);
        const face = headBones(a, b);
        ctx.globalAlpha = rect.alpha * Math.min(A.v, B.v, 1) ** 0.5;
        glowLine(ctx, A, end, on ? C.mag : C.cyan, face ? 3 : on ? 7 : 5, on ? 26 : 14);
      });
      ctx.shadowBlur = 0;
      // 头部 HUD：一圈细线，转头时亮出朝向弧和角度
      const hg = headGeom(pts);
      const bonesP = reveal ? reveal.bones[0] : 1;
      if (bonesP > 0) {
        const headOn = active === GROUPS.head;
        ctx.globalAlpha = rect.alpha * bonesP;
        ctx.strokeStyle = headOn ? C.mag : 'rgba(63,230,255,0.55)'; ctx.lineWidth = 2;
        ctx.shadowColor = ctx.strokeStyle; ctx.shadowBlur = 10;
        ctx.beginPath(); ctx.arc(hg.x, hg.y, hg.r, 0, Math.PI * 2); ctx.stroke();
        if (headOn && Math.abs(hg.look) > 0.08) {
          const dir = Math.sign(hg.look);
          const mid = dir > 0 ? 0 : Math.PI;
          ctx.lineWidth = 6; ctx.strokeStyle = C.mag;
          ctx.beginPath(); ctx.arc(hg.x, hg.y, hg.r + 10, mid - 0.7 * Math.abs(hg.look), mid + 0.7 * Math.abs(hg.look)); ctx.stroke();
          ctx.shadowBlur = 0;
          text(ctx, `YAW ${dir > 0 ? '→' : '←'} ${Math.round(Math.abs(SB.gameState(t).headYaw))}°`, hg.x + dir * (hg.r + 24), hg.y + 8,
            { font: `700 22px ${MONO}`, fill: C.mag, align: dir > 0 ? 'left' : 'right' });
        }
        ctx.shadowBlur = 0;
      }
      pts.forEach((p, i) => {
        const s = reveal ? reveal.pts[i] : 1;
        if (s <= 0) return;
        ctx.globalAlpha = rect.alpha * Math.max(0.25, p.v);
        const on = active && active.has(i);
        const r = (i <= 10 ? 3.5 : 5.5) * s;
        ctx.fillStyle = on ? '#fff' : '#dff9ff';
        ctx.shadowColor = on ? C.mag : C.cyan; ctx.shadowBlur = 14;
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
        if (reveal && s < 1) {
          ctx.strokeStyle = C.lime; ctx.lineWidth = 2; ctx.shadowBlur = 0;
          ctx.beginPath(); ctx.arc(p.x, p.y, 6 + 18 * s, 0, Math.PI * 2); ctx.globalAlpha = rect.alpha * (1 - s) * 0.9; ctx.stroke();
        }
      });
      if (SB.gameState(t).fistOn) {
        const hand = pts[L.right_wrist], k = pulse(t, 'grab', 0.6);
        ctx.globalAlpha = rect.alpha; ctx.shadowBlur = 0;
        ctx.strokeStyle = C.lime; ctx.lineWidth = 3; ctx.setLineDash([6, 6]); ctx.lineDashOffset = -t * 40;
        ctx.beginPath(); ctx.arc(hand.x, hand.y, 26 + 14 * k, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
        text(ctx, 'FIST', hand.x + 36, hand.y - 26, { font: `700 20px ${MONO}`, fill: C.lime });
      }
      ctx.restore();
    },

    game(ctx, r, gs, t, cursor) {
      ctx.save(); ctx.globalAlpha = r.alpha;
      ctx.beginPath(); ctx.rect(r.x, r.y, r.w, r.h); ctx.clip();
      const gc = gameCamera(r, gs);
      let g = ctx.createLinearGradient(0, r.y, 0, gc.horizon);
      g.addColorStop(0, '#070a24'); g.addColorStop(1, '#2a0a3c');
      ctx.fillStyle = g; ctx.fillRect(r.x, r.y, r.w, gc.horizon - r.y);
      // 复古太阳：固定在世界的某个方向，转视角时会滑走
      const sunAng = (-8 * Math.PI / 180) + gc.yaw;
      const sx = gc.cx + gc.f * Math.tan(sunAng);
      const sr = 120;
      g = ctx.createLinearGradient(0, gc.horizon - sr, 0, gc.horizon);
      g.addColorStop(0, '#ffd23f'); g.addColorStop(1, C.mag);
      ctx.save(); ctx.beginPath(); ctx.arc(sx, gc.horizon, sr, Math.PI, 0); ctx.clip();
      ctx.fillStyle = g; ctx.fillRect(sx - sr, gc.horizon - sr, sr * 2, sr);
      ctx.fillStyle = '#2a0a3c';
      for (let i = 0; i < 6; i += 1) ctx.fillRect(sx - sr, gc.horizon - 12 - i * 16, sr * 2, 3 + i * 0.6);
      ctx.restore();
      ctx.fillStyle = '#05020f'; ctx.fillRect(r.x, gc.horizon, r.w, r.y + r.h - gc.horizon);
      ctx.lineWidth = 1.6;
      for (let x = -40; x <= 40; x += 2.5) {
        const seg = gc.line([x, 0, gs.distance], [x, 0, gs.distance + 160]);
        if (!seg) continue;
        ctx.strokeStyle = 'rgba(255,62,165,0.55)';
        ctx.beginPath(); ctx.moveTo(seg[0].x, seg[0].y); ctx.lineTo(seg[1].x, seg[1].y); ctx.stroke();
      }
      const z0 = Math.floor(gs.distance / 2.5) * 2.5;
      for (let z = z0; z < z0 + 90; z += 2.5) {
        const seg = gc.line([-60, 0, z], [60, 0, z]);
        if (!seg) continue;
        ctx.strokeStyle = 'rgba(63,230,255,0.45)';
        ctx.beginPath(); ctx.moveTo(seg[0].x, seg[0].y); ctx.lineTo(seg[1].x, seg[1].y); ctx.stroke();
      }
      // 霓虹立柱
      for (const o of visibleObjects(gc, gs)) {
        const w = Math.max(2, 0.5 * o.base.s);
        const fade = clamp01((70 - o.base.d) / 30);
        ctx.globalAlpha = r.alpha * fade;
        ctx.strokeStyle = o.kind === 1 ? C.mag : C.cyan; ctx.lineWidth = 2;
        ctx.shadowColor = ctx.strokeStyle; ctx.shadowBlur = 12;
        ctx.fillStyle = 'rgba(5,2,15,0.85)';
        ctx.fillRect(o.top.x - w / 2, o.top.y, w, o.base.y - o.top.y);
        ctx.strokeRect(o.top.x - w / 2, o.top.y, w, o.base.y - o.top.y);
        ctx.shadowBlur = 0;
      }
      ctx.globalAlpha = r.alpha;
      // 顶部罗盘：视角转了多少一眼可见
      const heading = -gs.viewYaw;
      ctx.fillStyle = 'rgba(4,6,12,0.55)'; ctx.fillRect(r.x + r.w / 2 - 260, r.y + 18, 520, 40);
      ctx.strokeStyle = C.cyan; ctx.lineWidth = 2;
      for (let d = -180; d <= 180; d += 10) {
        const x = r.x + r.w / 2 + (d - heading) * 6;
        if (x < r.x + r.w / 2 - 250 || x > r.x + r.w / 2 + 250) continue;
        ctx.globalAlpha = r.alpha * (d % 30 === 0 ? 1 : 0.5);
        ctx.beginPath(); ctx.moveTo(x, r.y + 50); ctx.lineTo(x, r.y + (d % 30 === 0 ? 30 : 40)); ctx.stroke();
      }
      ctx.globalAlpha = r.alpha;
      text(ctx, `${Math.round(((heading % 360) + 360) % 360)}°`, r.x + r.w / 2, r.y + 86, { font: `700 22px ${MONO}`, fill: C.lime, align: 'center' });
      // 准星和速度
      const cx = r.x + r.w / 2, cy = gc.horizon + 40;
      ctx.strokeStyle = C.lime; ctx.lineWidth = 2.5; ctx.shadowColor = C.lime; ctx.shadowBlur = 10;
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        ctx.beginPath(); ctx.moveTo(cx + dx * 10, cy + dy * 10); ctx.lineTo(cx + dx * 24, cy + dy * 24); ctx.stroke();
      }
      ctx.shadowBlur = 0;
      text(ctx, `SPD ${gs.speed.toFixed(1)}`, r.x + 28, r.y + r.h - 28, { font: `700 26px ${MONO}`, fill: gs.speed > 0.1 ? C.lime : C.dim });
      if (cursor) drawNeonCursor(ctx, r, cursor, t);
      ctx.restore();
      ctx.save(); ctx.globalAlpha = r.alpha;
      ctx.strokeStyle = C.cyan; ctx.lineWidth = 2; ctx.shadowColor = C.cyan; ctx.shadowBlur = 14;
      ctx.strokeRect(r.x, r.y, r.w, r.h);
      ctx.restore();
    },

    chip(ctx, cx, cy, copy, { p, out, pulse, t }) {
      ctx.save(); ctx.globalAlpha = clamp01(p) * out;
      const fontA = `700 38px ${BODY}`, fontB = `700 38px ${BODY}`;
      const wa = measure(ctx, copy.from, fontA) + 56, wb = measure(ctx, copy.to, fontB) + 56;
      const gap = 150, total = wa + gap + wb;
      const x0 = cx - total / 2, h = 76, y0 = cy - h / 2;
      roundRect(ctx, x0, y0, wa, h, 8); ctx.fillStyle = 'rgba(8,16,30,0.9)'; ctx.fill();
      ctx.strokeStyle = C.cyan; ctx.lineWidth = 2; ctx.stroke();
      text(ctx, copy.from, x0 + wa / 2, cy + 13, { font: fontA, fill: C.text, align: 'center' });
      // 流动箭头
      const ax = x0 + wa + 16, aw = gap - 32;
      ctx.strokeStyle = C.lime; ctx.lineWidth = 3; ctx.setLineDash([10, 8]); ctx.lineDashOffset = -t * 60;
      ctx.beginPath(); ctx.moveTo(ax, cy); ctx.lineTo(ax + aw - 14, cy); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = C.lime; ctx.beginPath(); ctx.moveTo(ax + aw, cy); ctx.lineTo(ax + aw - 18, cy - 11); ctx.lineTo(ax + aw - 18, cy + 11); ctx.fill();
      const bx = x0 + wa + gap;
      roundRect(ctx, bx, y0, wb, h, 8);
      ctx.fillStyle = `rgba(255,62,165,${0.12 + 0.5 * pulse})`; ctx.fill();
      ctx.shadowColor = C.mag; ctx.shadowBlur = 10 + 30 * pulse;
      ctx.strokeStyle = C.mag; ctx.lineWidth = 2 + 2 * pulse; ctx.stroke(); ctx.shadowBlur = 0;
      text(ctx, copy.to, bx + wb / 2, cy + 13, { font: fontB, fill: '#fff', align: 'center' });
      text(ctx, copy.key, bx + wb / 2, y0 - 14, { font: `700 20px ${MONO}`, fill: C.mag, align: 'center', spacing: 2 });
      ctx.restore();
    },

    caption(ctx, cx, cy, str, { p, out }) {
      ctx.save(); ctx.globalAlpha = clamp01(p) * out;
      text(ctx, str, cx, cy, { font: `500 38px ${BODY}`, fill: '#cfeaff', align: 'center', spacing: 2 });
      ctx.restore();
    },

    cta(ctx, t) {
      const p1 = prog(t, 12.85, 0.4), p2 = prog(t, 13.3, 0.35), p3 = prog(t, 13.7, 0.35);
      glitchText(ctx, SB.COPY.ctaTitle, 150, 470, 150, p1, t);
      text(ctx, SB.COPY.ctaLine, 160, 570, { font: `700 44px ${BODY}`, fill: C.cyan, alpha: p2, spacing: 3 });
      if (p3 > 0) {
        roundRect(ctx, 160, 630, measure(ctx, SB.COPY.ctaUrl, `500 28px ${MONO}`) + 48, 64, 8);
        ctx.save(); ctx.globalAlpha = p3; ctx.strokeStyle = C.mag; ctx.lineWidth = 2; ctx.stroke(); ctx.restore();
        text(ctx, SB.COPY.ctaUrl, 184, 672, { font: `500 28px ${MONO}`, fill: C.text, alpha: p3 });
      }
    },

    overlay(ctx, t) {
      if (!scan) {
        scan = document.createElement('canvas'); scan.width = 4; scan.height = 4;
        const s = scan.getContext('2d'); s.fillStyle = 'rgba(0,0,0,0.22)'; s.fillRect(0, 0, 4, 1);
      }
      ctx.save();
      ctx.fillStyle = ctx.createPattern(scan, 'repeat'); ctx.fillRect(0, 0, W, H);
      const v = ctx.createRadialGradient(W / 2, H / 2, H * 0.45, W / 2, H / 2, H * 1.05);
      v.addColorStop(0, 'rgba(0,0,0,0)'); v.addColorStop(1, 'rgba(0,0,0,0.55)');
      ctx.fillStyle = v; ctx.fillRect(0, 0, W, H);
      // 切场白闪
      for (const e of EVENTS) {
        if (e.type !== 'whoosh') continue;
        const k = 1 - prog(t, e.t, 0.12);
        if (t >= e.t && k > 0) { ctx.fillStyle = `rgba(63,230,255,${0.12 * k})`; ctx.fillRect(0, 0, W, H); }
      }
      ctx.restore();
    },
  };

  function drawNeonCursor(ctx, r, c, t) {
    const x = r.x + c.x * r.w, y = r.y + c.y * r.h;
    ctx.save();
    ctx.shadowColor = C.lime; ctx.shadowBlur = 16;
    ctx.fillStyle = C.lime; ctx.strokeStyle = '#051'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + 30, y + 26); ctx.lineTo(x + 14, y + 28); ctx.lineTo(x + 6, y + 42); ctx.closePath(); ctx.fill();
    ctx.restore();
  }
})();

// ================================================================================
// 风格二：极简产品
// ================================================================================

const CLEAN = (() => {
  const C = { bg: '#f3f2ee', ink: '#111214', grey: '#85888f', light: '#e3e2dc', blue: '#2f6bff', white: '#ffffff' };
  const SANS = '"Noto Sans SC", sans-serif';
  const MONO = '"JetBrains Mono", monospace';

  function card(ctx, x, y, w, h, r, alpha = 1) {
    ctx.save();
    ctx.globalAlpha *= alpha;
    ctx.shadowColor = 'rgba(20,24,40,0.10)'; ctx.shadowBlur = 60; ctx.shadowOffsetY = 24;
    roundRect(ctx, x, y, w, h, r); ctx.fillStyle = C.white; ctx.fill();
    ctx.restore();
  }

  function rise(ctx, str, x, y, font, fill, p, { align = 'left', spacing = 0 } = {}) {
    const e = easeOut(clamp01(p));
    text(ctx, str, x, y + (1 - e) * 36, { font, fill, align, spacing, alpha: e });
  }

  return {
    name: 'clean',
    background(ctx) {
      ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
      const g = ctx.createRadialGradient(W / 2, H * 0.4, 0, W / 2, H * 0.4, W * 0.7);
      g.addColorStop(0, 'rgba(255,255,255,0.8)'); g.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    },

    hook(ctx, t) {
      const out = 1 - prog(t, 2.15, 0.25);
      ctx.save(); ctx.globalAlpha = out;
      rise(ctx, SB.COPY.hookA, W / 2, 500, `900 150px ${SANS}`, C.ink, prog(t, 0.15, 0.6), { align: 'center', spacing: -4 });
      rise(ctx, SB.COPY.hookB, W / 2, 630, `500 60px ${SANS}`, C.grey, prog(t, 0.9, 0.6), { align: 'center', spacing: 2 });
      const p = easeOut(prog(t, 1.1, 0.6));
      ctx.fillStyle = C.blue; ctx.globalAlpha = out * p;
      ctx.beginPath(); ctx.arc(W / 2, 700, 7, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    },

    pointsText(ctx, t) {
      const out = 1 - prog(t, 4.45, 0.25);
      ctx.save(); ctx.globalAlpha = out;
      rise(ctx, '33', 160, 470, `900 200px ${SANS}`, C.blue, prog(t, 2.6, 0.6), { spacing: -6 });
      rise(ctx, '个身体关键点', 160, 560, `900 72px ${SANS}`, C.ink, prog(t, 2.75, 0.6), { spacing: -1 });
      rise(ctx, '一个摄像头就够。', 160, 640, `500 44px ${SANS}`, C.grey, prog(t, 2.95, 0.6));
      const p = prog(t, 3.4, 0.5);
      if (p > 0) {
        const e = easeOut(p);
        ctx.globalAlpha = out * e;
        roundRect(ctx, 160, 690, 500, 64, 32); ctx.fillStyle = C.white; ctx.fill();
        ctx.fillStyle = '#20c26b'; ctx.beginPath(); ctx.arc(196, 722, 8, 0, Math.PI * 2); ctx.fill();
        text(ctx, SB.COPY.pointsB, 220, 734, { font: `500 30px ${SANS}`, fill: C.ink });
      }
      ctx.restore();
    },

    panel(ctx, r, t) {
      ctx.save();
      card(ctx, r.x, r.y, r.w, r.h, 44, r.alpha);
      ctx.globalAlpha = r.alpha;
      roundRect(ctx, r.x + 20, r.y + 22, 150, 40, 20); ctx.fillStyle = '#f3f3f0'; ctx.fill();
      ctx.fillStyle = '#20c26b'; ctx.beginPath(); ctx.arc(r.x + 42, r.y + 42, 6, 0, Math.PI * 2); ctx.fill();
      text(ctx, '本地识别', r.x + 56, r.y + 51, { font: `500 22px ${SANS}`, fill: C.grey });
      // 脚下的影子
      ctx.fillStyle = 'rgba(17,18,20,0.07)';
      ctx.beginPath(); ctx.ellipse(r.x + r.w / 2, r.y + r.h * 0.845, r.w * 0.26, 16, 0, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    },

    skeleton(ctx, pts, { t, reveal, active, rect }) {
      ctx.save(); ctx.globalAlpha = rect.alpha;
      ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      // 头：实心圆，脸上的点画成白点；转头时鼻子那一侧亮一道蓝弧
      const hg = headGeom(pts);
      const headP = reveal ? reveal.bones[0] : 1;
      if (headP > 0) {
        const headOn = active === GROUPS.head;
        ctx.globalAlpha = rect.alpha * headP;
        ctx.fillStyle = headOn ? C.blue : C.ink;
        ctx.beginPath(); ctx.arc(hg.x, hg.y, hg.r, 0, Math.PI * 2); ctx.fill();
        // 脖子
        const ls = pts[L.left_shoulder], rs = pts[L.right_shoulder];
        ctx.strokeStyle = C.ink; ctx.lineWidth = 15;
        ctx.beginPath(); ctx.moveTo(hg.x, hg.y + hg.r * 0.8); ctx.lineTo((ls.x + rs.x) / 2, (ls.y + rs.y) / 2); ctx.stroke();
        if (headOn && Math.abs(hg.look) > 0.08) {
          const mid = hg.look > 0 ? 0 : Math.PI;
          ctx.strokeStyle = C.blue; ctx.lineWidth = 6;
          ctx.beginPath(); ctx.arc(hg.x, hg.y, hg.r + 14, mid - 0.65 * Math.abs(hg.look), mid + 0.65 * Math.abs(hg.look)); ctx.stroke();
        }
        ctx.globalAlpha = rect.alpha;
      }
      BONES.forEach(([a, b], k) => {
        const pr = reveal ? reveal.bones[k] : 1;
        if (pr <= 0) return;
        const A = pts[a], B = pts[b];
        const face = headBones(a, b);
        if (face && headP > 0.5) return;
        ctx.strokeStyle = boneActive(active, a, b) ? C.blue : C.ink;
        ctx.lineWidth = face ? 5 : 15;
        ctx.globalAlpha = rect.alpha * (face ? Math.min(A.v, B.v) : 1);
        ctx.beginPath(); ctx.moveTo(A.x, A.y); ctx.lineTo(lerp(A.x, B.x, pr), lerp(A.y, B.y, pr)); ctx.stroke();
      });
      pts.forEach((p, i) => {
        const s = reveal ? reveal.pts[i] : 1;
        if (s <= 0) return;
        const on = active && active.has(i);
        ctx.globalAlpha = rect.alpha * (i <= 10 ? Math.max(0.2, p.v) : 1);
        const r = (i <= 10 ? 4 : 6.5) * s;
        const bonesDrawn = !reveal || reveal.bones[0] > 0.5;
        ctx.fillStyle = bonesDrawn ? (i <= 10 ? 'rgba(255,255,255,0.9)' : C.white) : C.blue;
        ctx.strokeStyle = on ? C.blue : C.ink; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
        if (bonesDrawn && i > 10) ctx.stroke();
      });
      if (SB.gameState(t).fistOn) {
        const hand = pts[L.right_wrist], k = pulse(t, 'grab', 0.6);
        ctx.globalAlpha = rect.alpha;
        ctx.strokeStyle = C.blue; ctx.lineWidth = 4;
        ctx.beginPath(); ctx.arc(hand.x, hand.y, 24 + 16 * k, 0, Math.PI * 2); ctx.stroke();
        ctx.globalAlpha = rect.alpha * 0.15; ctx.fillStyle = C.blue; ctx.fill();
      }
      ctx.restore();
    },

    game(ctx, r, gs, t, cursor) {
      ctx.save();
      card(ctx, r.x, r.y, r.w, r.h, 36, r.alpha);
      ctx.globalAlpha = r.alpha;
      roundRect(ctx, r.x, r.y, r.w, r.h, 36); ctx.clip();
      const gc = gameCamera(r, gs);
      let g = ctx.createLinearGradient(0, r.y, 0, gc.horizon);
      g.addColorStop(0, '#dfe7f3'); g.addColorStop(1, '#f7f8fa');
      ctx.fillStyle = g; ctx.fillRect(r.x, r.y, r.w, gc.horizon - r.y);
      ctx.fillStyle = '#f4f4f1'; ctx.fillRect(r.x, gc.horizon, r.w, r.y + r.h - gc.horizon);
      ctx.strokeStyle = '#dcdcd5'; ctx.lineWidth = 2;
      for (let x = -40; x <= 40; x += 4) {
        const seg = gc.line([x, 0, gs.distance], [x, 0, gs.distance + 160]);
        if (!seg) continue;
        ctx.beginPath(); ctx.moveTo(seg[0].x, seg[0].y); ctx.lineTo(seg[1].x, seg[1].y); ctx.stroke();
      }
      const z0 = Math.floor(gs.distance / 4) * 4;
      for (let z = z0; z < z0 + 90; z += 4) {
        const seg = gc.line([-60, 0, z], [60, 0, z]);
        if (!seg) continue;
        ctx.beginPath(); ctx.moveTo(seg[0].x, seg[0].y); ctx.lineTo(seg[1].x, seg[1].y); ctx.stroke();
      }
      for (const o of visibleObjects(gc, gs)) {
        const w = Math.max(3, 0.8 * o.base.s);
        const fade = clamp01((70 - o.base.d) / 30);
        ctx.globalAlpha = r.alpha * fade;
        const top = o.top.y, h = o.base.y - top;
        ctx.fillStyle = 'rgba(17,18,20,0.06)';
        ctx.beginPath(); ctx.ellipse(o.base.x, o.base.y, w * 0.9, w * 0.22, 0, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = o.kind === 1 ? '#b7c9ef' : '#cfd4dd';
        roundRect(ctx, o.base.x - w / 2, top, w, h, Math.min(w / 2, 40)); ctx.fill();
        ctx.fillStyle = 'rgba(255,255,255,0.55)';
        roundRect(ctx, o.base.x - w / 2, top, w * 0.35, h, Math.min(w / 4, 20)); ctx.fill();
      }
      ctx.globalAlpha = r.alpha;
      // 顶部极简罗盘
      const heading = -gs.viewYaw;
      const cx = r.x + r.w / 2;
      ctx.strokeStyle = '#b9bcc3'; ctx.lineWidth = 2;
      for (let d = -180; d <= 180; d += 15) {
        const x = cx + (d - heading) * 5;
        if (Math.abs(x - cx) > 220) continue;
        ctx.globalAlpha = r.alpha * (1 - Math.abs(x - cx) / 240);
        ctx.beginPath(); ctx.moveTo(x, r.y + 40); ctx.lineTo(x, r.y + (d % 45 === 0 ? 24 : 32)); ctx.stroke();
      }
      ctx.globalAlpha = r.alpha;
      ctx.fillStyle = C.blue; ctx.beginPath(); ctx.moveTo(cx, r.y + 46); ctx.lineTo(cx - 7, r.y + 56); ctx.lineTo(cx + 7, r.y + 56); ctx.fill();
      // 准星
      const cy = gc.horizon + 40;
      ctx.strokeStyle = C.blue; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.arc(cx, cy, 12, 0, Math.PI * 2); ctx.stroke();
      // 移动状态
      if (gs.speed > 0.1) {
        roundRect(ctx, r.x + 28, r.y + r.h - 76, 150, 48, 24); ctx.fillStyle = C.white; ctx.fill();
        ctx.fillStyle = C.blue; ctx.beginPath(); ctx.arc(r.x + 54, r.y + r.h - 52, 7, 0, Math.PI * 2); ctx.fill();
        text(ctx, '前进中', r.x + 72, r.y + r.h - 42, { font: `700 26px ${SANS}`, fill: C.ink });
      }
      if (cursor) {
        const x = r.x + cursor.x * r.w, y = r.y + cursor.y * r.h;
        ctx.shadowColor = 'rgba(0,0,0,0.25)'; ctx.shadowBlur = 10; ctx.shadowOffsetY = 4;
        ctx.fillStyle = C.ink; ctx.strokeStyle = C.white; ctx.lineWidth = 3; ctx.lineJoin = 'round';
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + 28, y + 24); ctx.lineTo(x + 13, y + 26); ctx.lineTo(x + 5, y + 40); ctx.closePath(); ctx.stroke(); ctx.fill();
      }
      ctx.restore();
    },

    chip(ctx, cx, cy, copy, { p, out, pulse }) {
      ctx.save(); ctx.globalAlpha = clamp01(p) * out;
      const font = `700 38px ${SANS}`;
      const wa = measure(ctx, copy.from, font), wb = measure(ctx, copy.to, font);
      const keyFont = `500 22px ${SANS}`;
      const wk = measure(ctx, copy.key, keyFont) + 32;
      const total = 44 + wa + 110 + wb + 20 + wk + 36;
      const x0 = cx - total / 2, h = 84;
      const s = 1 + 0.02 * pulse;
      ctx.translate(cx, cy); ctx.scale(s, s); ctx.translate(-cx, -cy);
      card(ctx, x0, cy - h / 2, total, h, h / 2);
      let x = x0 + 44;
      text(ctx, copy.from, x, cy + 13, { font, fill: C.ink }); x += wa + 24;
      ctx.strokeStyle = '#c6c7cb'; ctx.lineWidth = 3; ctx.lineCap = 'round';
      ctx.beginPath(); ctx.moveTo(x, cy); ctx.lineTo(x + 58, cy); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x + 48, cy - 9); ctx.lineTo(x + 60, cy); ctx.lineTo(x + 48, cy + 9); ctx.stroke();
      x += 86;
      text(ctx, copy.to, x, cy + 13, { font, fill: C.blue }); x += wb + 20;
      roundRect(ctx, x, cy - 20, wk, 40, 10);
      ctx.fillStyle = pulse > 0.05 ? `rgba(47,107,255,${0.1 + 0.25 * pulse})` : '#f3f3f0'; ctx.fill();
      text(ctx, copy.key, x + wk / 2, cy + 8, { font: keyFont, fill: pulse > 0.3 ? C.blue : C.grey, align: 'center' });
      ctx.restore();
    },

    caption(ctx, cx, cy, str, { p, out }) {
      ctx.save(); ctx.globalAlpha = out;
      const e = easeOut(clamp01(prog(p, 0, 1)));
      text(ctx, str, cx, cy + (1 - e) * 16, { font: `500 36px ${SANS}`, fill: C.grey, align: 'center', alpha: e });
      ctx.restore();
    },

    cta(ctx, t) {
      rise(ctx, SB.COPY.ctaTitle, 150, 480, `900 128px ${SANS}`, C.ink, prog(t, 12.85, 0.6), { spacing: -4 });
      const p = easeOut(prog(t, 13.1, 0.5));
      const tw = measure(ctx, SB.COPY.ctaTitle, `900 128px ${SANS}`, -4);
      ctx.save(); ctx.globalAlpha = p; ctx.fillStyle = C.blue;
      ctx.beginPath(); ctx.arc(150 + tw + 18, 468, 14, 0, Math.PI * 2); ctx.fill(); ctx.restore();
      rise(ctx, SB.COPY.ctaLine, 156, 570, `500 44px ${SANS}`, C.grey, prog(t, 13.3, 0.6));
      const q = easeOut(prog(t, 13.65, 0.6));
      if (q > 0) {
        ctx.save(); ctx.globalAlpha = q;
        const w = measure(ctx, SB.COPY.ctaUrl, `500 26px ${MONO}`) + 56;
        roundRect(ctx, 156, 620, w, 60, 30); ctx.fillStyle = C.ink; ctx.fill();
        text(ctx, SB.COPY.ctaUrl, 184, 659, { font: `500 26px ${MONO}`, fill: C.white });
        ctx.restore();
      }
    },
  };
})();

// ================================================================================
// 风格三：手绘漫画
// ================================================================================

const DOODLE = (() => {
  const C = { paper: '#fbf1dc', ink: '#2a2320', yellow: '#ffd23f', coral: '#ff6b5e', teal: '#23b8a0', blue: '#4d8dff', pink: '#ff9ccb', green: '#7ccf6a' };
  const FUN = '"ZCOOL KuaiLe", "Noto Sans SC", sans-serif';
  const HAND = '"LXGW WenKai", "Noto Sans SC", sans-serif';
  const MONO = '"JetBrains Mono", monospace';
  let grain = null;

  // 线条"抖动"：每 1/8 秒换一次形状，手绘动画里常见的 boil 效果。
  const boil = (t) => Math.floor(t * 8);
  function wob(seed, t, amp) { return (hash(seed * 13.1 + boil(t) * 7.7) - 0.5) * 2 * amp; }

  function sketchLine(ctx, a, b, t, seed, width, color) {
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len;
    const bend = wob(seed, t, Math.min(6, len * 0.05));
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(a.x + wob(seed + 1, t, 1.5), a.y + wob(seed + 2, t, 1.5));
    ctx.quadraticCurveTo((a.x + b.x) / 2 + nx * bend, (a.y + b.y) / 2 + ny * bend, b.x + wob(seed + 3, t, 1.5), b.y + wob(seed + 4, t, 1.5));
    ctx.stroke();
  }

  function sketchRect(ctx, x, y, w, h, t, seed, width, color, fill) {
    const pts = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]].map(([px, py], i) => ({ x: px + wob(seed + i, t, 3), y: py + wob(seed + i + 9, t, 3) }));
    if (fill) {
      ctx.fillStyle = fill; ctx.beginPath(); ctx.moveTo(pts[0].x, pts[0].y);
      for (const p of pts.slice(1)) ctx.lineTo(p.x, p.y);
      ctx.closePath(); ctx.fill();
    }
    for (let i = 0; i < 4; i += 1) sketchLine(ctx, pts[i], pts[(i + 1) % 4], t, seed + i * 5, width, color);
  }

  function sketchCircle(ctx, x, y, r, t, seed, width, color, fill) {
    ctx.beginPath();
    for (let i = 0; i <= 24; i += 1) {
      const a = (i / 24) * Math.PI * 2.08;
      const rr = r + wob(seed + (i % 24), t, r * 0.05);
      const px = x + Math.cos(a) * rr, py = y + Math.sin(a) * rr;
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = 'round'; ctx.stroke();
  }

  // 综艺花字：粗描边 + 平面投影
  function popText(ctx, str, x, y, size, fill, p, { align = 'left', rot = -0.04, font = FUN, stroke = C.ink } = {}) {
    if (p <= 0) return;
    const s = easeBack(clamp01(p));
    ctx.save();
    ctx.translate(x, y); ctx.rotate(rot * (1 - clamp01(p)) + rot * 0.4); ctx.scale(s, s);
    const f = `400 ${size}px ${font}`;
    text(ctx, str, 7, 7, { font: f, fill: stroke, align, stroke, strokeWidth: size * 0.16, alpha: clamp01(p * 2) });
    text(ctx, str, 0, 0, { font: f, fill, align, stroke, strokeWidth: size * 0.16, alpha: clamp01(p * 2) });
    text(ctx, str, 0, 0, { font: f, fill, align, alpha: clamp01(p * 2) });
    ctx.restore();
  }

  function tape(ctx, x, y, rot) {
    ctx.save(); ctx.translate(x, y); ctx.rotate(rot);
    ctx.fillStyle = 'rgba(255,210,63,0.75)'; ctx.fillRect(-55, -16, 110, 32);
    ctx.restore();
  }

  function sticker(ctx, x, y, w, h, rot, fill, t, seed) {
    ctx.save(); ctx.translate(x + w / 2, y + h / 2); ctx.rotate(rot);
    ctx.fillStyle = C.ink; roundRect(ctx, -w / 2 + 8, -h / 2 + 8, w, h, 18); ctx.fill();
    roundRect(ctx, -w / 2, -h / 2, w, h, 18); ctx.fillStyle = fill; ctx.fill();
    ctx.strokeStyle = C.ink; ctx.lineWidth = 5; ctx.stroke();
    ctx.restore();
  }

  // 拟声字：挂在事件上，弹出来再淡掉
  const SOUNDS = { step: '咚', turn: '唰～', grab: '咔！' };

  return {
    name: 'doodle',
    background(ctx, t) {
      ctx.fillStyle = C.paper; ctx.fillRect(0, 0, W, H);
      if (!grain) {
        grain = document.createElement('canvas'); grain.width = 480; grain.height = 270;
        const g = grain.getContext('2d'); const img = g.createImageData(480, 270); const r = mulberry(3);
        for (let i = 0; i < img.data.length; i += 4) {
          const v = 120 + r() * 100; img.data[i] = v; img.data[i + 1] = v * 0.92; img.data[i + 2] = v * 0.8; img.data[i + 3] = r() * 38;
        }
        g.putImageData(img, 0, 0);
      }
      ctx.drawImage(grain, 0, 0, W, H);
      // 边角的小涂鸦
      const doodles = [[120, 150, 'star', C.yellow], [1790, 170, 'swirl', C.pink], [1770, 960, 'star', C.teal], [150, 960, 'swirl', C.yellow], [980, 70, 'dots', C.coral]];
      doodles.forEach(([x, y, kind, color], i) => {
        const bob = Math.sin(t * 2 + i) * 6;
        ctx.save(); ctx.translate(x, y + bob);
        if (kind === 'star') {
          ctx.beginPath();
          for (let k = 0; k <= 10; k += 1) {
            const a = -Math.PI / 2 + k * Math.PI / 5, rr = (k % 2 ? 16 : 38) + wob(i * 10 + k, t, 2);
            ctx.lineTo(Math.cos(a) * rr, Math.sin(a) * rr);
          }
          ctx.fillStyle = color; ctx.fill(); ctx.strokeStyle = C.ink; ctx.lineWidth = 4; ctx.lineJoin = 'round'; ctx.stroke();
        } else if (kind === 'swirl') {
          ctx.beginPath();
          for (let k = 0; k < 60; k += 1) { const a = k * 0.28, rr = 2 + k * 0.6; ctx.lineTo(Math.cos(a) * rr + wob(i + k, t, 0.8), Math.sin(a) * rr); }
          ctx.strokeStyle = color; ctx.lineWidth = 6; ctx.lineCap = 'round'; ctx.stroke();
        } else {
          for (let k = 0; k < 3; k += 1) { ctx.fillStyle = color; ctx.beginPath(); ctx.arc(k * 26 - 26, wob(i + k, t, 3), 7, 0, Math.PI * 2); ctx.fill(); }
        }
        ctx.restore();
      });
    },

    hook(ctx, t) {
      const out = 1 - prog(t, 2.2, 0.2);
      ctx.save(); ctx.globalAlpha = out;
      popText(ctx, SB.COPY.hookA, W / 2, 500, 180, C.coral, prog(t, 0.15, 0.45), { align: 'center' });
      const p2 = prog(t, 0.9, 0.45);
      if (p2 > 0) {
        // 马克笔划过的高亮
        const w = 900 * easeOut(clamp01(p2 * 1.4));
        ctx.save(); ctx.globalAlpha = out * 0.85; ctx.fillStyle = C.yellow;
        ctx.beginPath(); ctx.moveTo(W / 2 - 450, 600); ctx.lineTo(W / 2 - 450 + w, 590); ctx.lineTo(W / 2 - 450 + w, 660); ctx.lineTo(W / 2 - 450, 668); ctx.fill();
        ctx.restore();
        popText(ctx, SB.COPY.hookB, W / 2, 652, 72, C.ink, p2, { align: 'center', rot: 0.02, font: HAND, stroke: C.paper });
      }
      ctx.restore();
    },

    pointsText(ctx, t) {
      const out = 1 - prog(t, 4.45, 0.25);
      ctx.save(); ctx.globalAlpha = out;
      popText(ctx, '33', 170, 470, 200, C.yellow, prog(t, 2.6, 0.45));
      popText(ctx, '个关键点！', 170 + measure(ctx, '33', `400 200px ${FUN}`) + 40, 460, 88, C.ink, prog(t, 2.8, 0.45), { stroke: C.paper });
      const p = prog(t, 3.1, 0.4);
      if (p > 0) text(ctx, '一个摄像头就够，手机也行', 176, 560, { font: `700 44px ${HAND}`, fill: C.ink, alpha: clamp01(p * 2) });
      const q = prog(t, 3.45, 0.4);
      if (q > 0) {
        ctx.save(); ctx.globalAlpha = out * clamp01(q * 2);
        sticker(ctx, 172, 610, 560, 84, -0.025, C.pink, t, 5);
        ctx.translate(172 + 280, 652); ctx.rotate(-0.025);
        text(ctx, '画面不出手机，放心拍', 0, 14, { font: `400 40px ${FUN}`, fill: C.ink, align: 'center' });
        ctx.restore();
      }
      ctx.restore();
    },

    panel(ctx, r, t) {
      ctx.save(); ctx.globalAlpha = r.alpha;
      sketchRect(ctx, r.x, r.y, r.w, r.h, t, 40, 6, C.ink, 'rgba(255,255,255,0.65)');
      tape(ctx, r.x + 40, r.y + 6, -0.5); tape(ctx, r.x + r.w - 40, r.y + 6, 0.5);
      text(ctx, '手机镜头', r.x + r.w / 2, r.y + r.h - 22, { font: `400 28px ${FUN}`, fill: C.ink, align: 'center', alpha: 0.6 });
      // 地面
      sketchLine(ctx, { x: r.x + 40, y: r.y + r.h * 0.905 }, { x: r.x + r.w - 40, y: r.y + r.h * 0.905 }, t, 90, 4, 'rgba(42,35,32,0.35)');
      ctx.restore();
    },

    skeleton(ctx, pts, { t, reveal, active, rect }) {
      ctx.save(); ctx.globalAlpha = rect.alpha;
      // 头：两耳中点画个圆，比一堆脸上的线更像手绘小人
      const hg = headGeom(pts);
      const hx = hg.x, hy = hg.y, hr = hg.r;
      const bonesP = reveal ? reveal.bones[0] : 1;
      if (bonesP > 0) {
        ctx.globalAlpha = rect.alpha * bonesP;
        sketchCircle(ctx, hx, hy, hr, t, 70, 6, active === GROUPS.head ? C.coral : C.ink, '#fffaf0');
        // 眼睛跟着转头往一边看，比点的位移更一眼看懂
        const ex = hg.look * hr * 0.45;
        ctx.fillStyle = C.ink;
        for (const side of [-1, 1]) { ctx.beginPath(); ctx.ellipse(hx + ex + side * hr * 0.3, hy + 2, 4.5, 7, 0, 0, Math.PI * 2); ctx.fill(); }
        ctx.strokeStyle = C.ink; ctx.lineWidth = 3.5; ctx.lineCap = 'round';
        ctx.beginPath(); ctx.arc(hx + ex * 0.8, hy + hr * 0.35, hr * 0.22, 0.2 * Math.PI, 0.8 * Math.PI); ctx.stroke();
        const ls = pts[L.left_shoulder], rs = pts[L.right_shoulder];
        sketchLine(ctx, { x: hx, y: hy + hr }, { x: (ls.x + rs.x) / 2, y: (ls.y + rs.y) / 2 }, t, 71, 8, C.ink);
        ctx.globalAlpha = rect.alpha;
      }
      BONES.forEach(([a, b], k) => {
        if (headBones(a, b)) return;
        const pr = reveal ? reveal.bones[k] : 1;
        if (pr <= 0) return;
        const A = pts[a], B = pts[b];
        const on = boneActive(active, a, b);
        sketchLine(ctx, A, { x: lerp(A.x, B.x, pr), y: lerp(A.y, B.y, pr) }, t, k * 3, on ? 10 : 8, on ? C.coral : C.ink);
      });
      pts.forEach((p, i) => {
        const s = reveal ? reveal.pts[i] : 1;
        if (s <= 0) return;
        const face = i <= 10;
        if (face && bonesP > 0.5) return;
        ctx.globalAlpha = rect.alpha * (face ? Math.max(0.15, p.v) : 1);
        const on = active && active.has(i);
        const color = face ? C.ink : on ? C.yellow : [C.teal, C.yellow, C.pink][i % 3];
        ctx.fillStyle = color; ctx.strokeStyle = C.ink; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(p.x + wob(i, t, 1), p.y + wob(i + 40, t, 1), (face ? 4 : 8) * s, 0, Math.PI * 2);
        ctx.fill(); if (!face) ctx.stroke();
      });
      if (SB.gameState(t).fistOn) {
        const hand = pts[L.right_wrist];
        ctx.globalAlpha = rect.alpha;
        sketchCircle(ctx, hand.x, hand.y, 26 + 10 * pulse(t, 'grab', 0.6), t, 1200, 4, C.coral);
      }
      // 拟声字
      for (const type of ['step', 'turn', 'grab']) {
        const e = lastEvent(t, type);
        if (!e || t - e.t > 0.55 || e.t < 4.5) continue;
        const k = prog(t, e.t, 0.55);
        let x, y;
        if (type === 'step') { const foot = e.foot === 'left' ? pts[L.left_ankle] : pts[L.right_ankle]; x = foot.x + (e.foot === 'left' ? -70 : 70); y = foot.y - 10; }
        else if (type === 'turn') { x = hx + (hg.look < 0 ? -hr - 80 : hr + 80); y = hy + 20; }
        else { x = pts[L.right_wrist].x + 70; y = pts[L.right_wrist].y - 50; }
        ctx.save(); ctx.globalAlpha = rect.alpha * (1 - k * k);
        popText(ctx, SOUNDS[type], x, y - k * 30, type === 'step' ? 56 : 60, type === 'grab' ? C.yellow : C.coral, Math.min(1, k * 3), { align: 'center', stroke: C.ink });
        ctx.restore();
      }
      ctx.restore();
    },

    game(ctx, r, gs, t, cursor) {
      ctx.save(); ctx.globalAlpha = r.alpha;
      ctx.save();
      roundRect(ctx, r.x, r.y, r.w, r.h, 24); ctx.clip();
      const gc = gameCamera(r, gs);
      ctx.fillStyle = '#cfe7ff'; ctx.fillRect(r.x, r.y, r.w, gc.horizon - r.y);
      ctx.fillStyle = '#d8efc4'; ctx.fillRect(r.x, gc.horizon, r.w, r.y + r.h - gc.horizon);
      // 太阳和云：固定在世界方向上，转视角会滑走
      const sx = gc.cx + gc.f * Math.tan(-0.35 + gc.yaw);
      sketchCircle(ctx, sx, r.y + 110, 46, t, 300, 5, C.ink, C.yellow);
      for (let i = 0; i < 3; i += 1) {
        const ang = -0.9 + i * 0.75 + gc.yaw;
        const x = gc.cx + gc.f * Math.tan(ang);
        if (Math.abs(ang) > 1.3) continue;
        const y = r.y + 70 + (i % 2) * 60;
        for (const [dx, rr] of [[-40, 30], [0, 42], [40, 30]]) sketchCircle(ctx, x + dx, y, rr, t, 400 + i * 10 + dx, 4, C.ink, '#ffffff');
      }
      // 远山
      ctx.beginPath(); ctx.moveTo(r.x, gc.horizon);
      for (let k = 0; k <= 40; k += 1) {
        const ang = -1.4 + k * 0.07 + gc.yaw;
        const x = r.x + (k / 40) * r.w;
        const base = -1.4 + k * 0.07;
        ctx.lineTo(x, gc.horizon - 40 - 30 * Math.sin(base * 5 + gs.viewYaw * 0.087) - 18 * Math.sin(base * 11 + ang));
      }
      ctx.lineTo(r.x + r.w, gc.horizon); ctx.closePath();
      ctx.fillStyle = '#9fd28a'; ctx.fill(); ctx.strokeStyle = C.ink; ctx.lineWidth = 4; ctx.stroke();
      // 小路和虚线
      const roadL = gc.line([-2.2, 0, gs.distance], [-2.2, 0, gs.distance + 200]);
      const roadR = gc.line([2.2, 0, gs.distance], [2.2, 0, gs.distance + 200]);
      if (roadL && roadR) {
        ctx.fillStyle = '#f4d9a6';
        ctx.beginPath(); ctx.moveTo(roadL[0].x, roadL[0].y); ctx.lineTo(roadL[1].x, roadL[1].y); ctx.lineTo(roadR[1].x, roadR[1].y); ctx.lineTo(roadR[0].x, roadR[0].y); ctx.fill();
        sketchLine(ctx, roadL[0], roadL[1], t, 500, 5, C.ink);
        sketchLine(ctx, roadR[0], roadR[1], t, 510, 5, C.ink);
      }
      const z0 = Math.floor(gs.distance / 3) * 3;
      for (let z = z0; z < z0 + 60; z += 3) {
        const seg = gc.line([0, 0, z], [0, 0, z + 1.4]);
        if (seg) sketchLine(ctx, seg[0], seg[1], t, 600 + z, 5, '#ffffff');
      }
      // 棒棒糖树
      for (const o of visibleObjects(gc, gs)) {
        const fade = clamp01((60 - o.base.d) / 25);
        ctx.globalAlpha = r.alpha * fade;
        const trunkTop = gc.proj(o.x, o.h * 0.55, o.z);
        const crown = gc.proj(o.x, o.h * 0.8, o.z);
        if (!trunkTop || !crown) continue;
        const cr = Math.max(4, 0.9 * crown.s * (0.6 + o.seed * 0.3));
        sketchLine(ctx, o.base, trunkTop, t, 700 + o.z, Math.max(2, 0.14 * o.base.s), '#8a5a3c');
        sketchCircle(ctx, crown.x, crown.y, cr, t, 800 + Math.round(o.z), Math.max(2, 0.05 * crown.s), C.ink, o.kind === 1 ? C.pink : C.green);
      }
      ctx.globalAlpha = r.alpha;
      // 准星
      const cy = gc.horizon + 40;
      sketchCircle(ctx, gc.cx, cy, 16, t, 900, 4, C.coral);
      if (cursor) {
        const x = r.x + cursor.x * r.w, y = r.y + cursor.y * r.h;
        ctx.fillStyle = C.yellow; ctx.strokeStyle = C.ink; ctx.lineWidth = 5; ctx.lineJoin = 'round';
        ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + 34, y + 30); ctx.lineTo(x + 16, y + 32); ctx.lineTo(x + 6, y + 50); ctx.closePath(); ctx.fill(); ctx.stroke();
      }
      ctx.restore();
      sketchRect(ctx, r.x, r.y, r.w, r.h, t, 950, 6, C.ink);
      popText(ctx, gs.speed > 0.1 ? '走起来了！' : '游戏画面', r.x + 30, r.y + 64, 40, gs.speed > 0.1 ? C.yellow : '#ffffff', 1, { rot: -0.03 });
      ctx.restore();
    },

    chip(ctx, cx, cy, copy, { p, out, pulse, t }) {
      ctx.save(); ctx.globalAlpha = clamp01(p) * out;
      const font = `400 42px ${FUN}`;
      const wa = measure(ctx, copy.from, font) + 60, wb = measure(ctx, copy.to, font) + 60;
      const gap = 130, total = wa + gap + wb;
      const x0 = cx - total / 2, h = 82;
      const s = easeBack(clamp01(p));
      ctx.translate(cx, cy); ctx.scale(s, s); ctx.translate(-cx, -cy);
      sticker(ctx, x0, cy - h / 2, wa, h, -0.03, '#ffffff', t, 1);
      ctx.save(); ctx.translate(x0 + wa / 2, cy); ctx.rotate(-0.03);
      text(ctx, copy.from, 0, 15, { font, fill: C.ink, align: 'center' }); ctx.restore();
      // 手绘弯箭头
      const ax = x0 + wa + 22, bxEnd = x0 + wa + gap - 22;
      ctx.strokeStyle = C.ink; ctx.lineWidth = 6; ctx.lineCap = 'round';
      ctx.beginPath(); ctx.moveTo(ax, cy + 6); ctx.quadraticCurveTo((ax + bxEnd) / 2, cy - 30 + wob(3, t, 4), bxEnd, cy); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(bxEnd - 20, cy - 16); ctx.lineTo(bxEnd, cy); ctx.lineTo(bxEnd - 22, cy + 10); ctx.stroke();
      const bx = x0 + wa + gap;
      const bump = 1 + 0.12 * pulse;
      ctx.save(); ctx.translate(bx + wb / 2, cy); ctx.scale(bump, bump); ctx.translate(-(bx + wb / 2), -cy);
      sticker(ctx, bx, cy - h / 2, wb, h, 0.04, C.yellow, t, 2);
      ctx.translate(bx + wb / 2, cy); ctx.rotate(0.04);
      text(ctx, copy.to, 0, 15, { font, fill: C.ink, align: 'center' });
      ctx.restore();
      ctx.save(); ctx.translate(bx + wb - 10, cy - h / 2 - 10); ctx.rotate(0.12);
      const kw = measure(ctx, copy.key, `700 22px ${HAND}`) + 26;
      roundRect(ctx, -kw / 2, -18, kw, 36, 10); ctx.fillStyle = C.teal; ctx.fill(); ctx.strokeStyle = C.ink; ctx.lineWidth = 3; ctx.stroke();
      text(ctx, copy.key, 0, 8, { font: `700 22px ${HAND}`, fill: '#fff', align: 'center' });
      ctx.restore();
      ctx.restore();
    },

    caption(ctx, cx, cy, str, { p, out }) {
      ctx.save(); ctx.globalAlpha = clamp01(p) * out;
      const font = `700 40px ${HAND}`;
      const w = measure(ctx, str, font) + 60;
      ctx.translate(cx, cy - 12); ctx.rotate(0.01);
      ctx.fillStyle = 'rgba(255,255,255,0.8)'; ctx.fillRect(-w / 2, -34, w, 64);
      text(ctx, str, 0, 12, { font, fill: C.ink, align: 'center' });
      ctx.restore();
    },

    cta(ctx, t) {
      popText(ctx, 'MotionControl', 150, 470, 128, C.yellow, prog(t, 12.85, 0.45));
      const p = prog(t, 13.3, 0.4);
      if (p > 0) text(ctx, '开源！一个摄像头就能玩', 160, 570, { font: `400 52px ${FUN}`, fill: C.ink, alpha: clamp01(p * 2) });
      const p2 = prog(t, 13.5, 0.4);
      if (p2 > 0) text(ctx, 'Windows · 站着玩 · 顺便动一动', 162, 630, { font: `700 36px ${HAND}`, fill: '#6b5b50', alpha: clamp01(p2 * 2) });
      const q = prog(t, 13.8, 0.4);
      if (q > 0) {
        ctx.save(); ctx.globalAlpha = clamp01(q * 2);
        const w = measure(ctx, SB.COPY.ctaUrl, `500 26px ${MONO}`) + 50;
        sticker(ctx, 160, 670, w, 62, -0.015, '#ffffff', t, 9);
        ctx.translate(160 + w / 2, 701); ctx.rotate(-0.015);
        text(ctx, SB.COPY.ctaUrl, 0, 9, { font: `500 26px ${MONO}`, fill: C.ink, align: 'center' });
        ctx.restore();
      }
    },
  };
})();

const STYLES = { neon: NEON, clean: CLEAN, doodle: DOODLE };

export async function boot(canvas, styleName) {
  const S = STYLES[styleName] || NEON;
  const ctx = canvas.getContext('2d');
  const sample = Object.values(SB.COPY).map((v) => (typeof v === 'string' ? v : Object.values(v).join(''))).join('')
    + '0123456789 个身体关键点一个摄像头就够手机也行画面不出手机放心拍前进中走起来了游戏画面手机镜头本地识别开源！Windows站着玩顺便动一动咚唰～咔°';
  const fonts = [
    '900 40px "Noto Sans SC"', '700 40px "Noto Sans SC"', '500 40px "Noto Sans SC"', '400 40px "Noto Sans SC"',
    '400 40px "ZCOOL QingKe HuangYou"', '400 40px "ZCOOL KuaiLe"', '700 40px "LXGW WenKai"',
    '500 40px "JetBrains Mono"', '700 40px "JetBrains Mono"',
  ];
  await Promise.all(fonts.map((f) => document.fonts.load(f, sample)));
  await document.fonts.ready;
  window.renderAt = (t) => {
    ctx.save();
    ctx.clearRect(0, 0, W, H);
    renderScene(ctx, t, S);
    ctx.restore();
  };
  window.__meta = { fps: SB.FPS, duration: SB.DURATION, width: W, height: H, style: S.name };
  window.renderAt(0);
}
