// 配乐和音效：全部现场合成，不用任何素材文件，所以没有版权问题。
//
// 节拍不是随便定的：原地踏步每秒 0.95 个周期、一个周期两步，一步 0.526 秒，正好是
// 114 BPM。把节拍网格的起点对到第一步落地，踏步那一段每一脚都踩在鼓点上。
// 音效挂在 storyboard.events() 上，和画面用的是同一份时间表。

import { writeFileSync } from 'node:fs';
import { DURATION, events } from './storyboard.js';

const SR = 48000;
const N = Math.ceil(DURATION * SR);
const midi = (m) => 440 * Math.pow(2, (m - 69) / 12);

function mulberry(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let x = Math.imul(a ^ (a >>> 15), 1 | a);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
}

// 最简单的 biquad，够做高通/低通/带通
function biquad(type, freq, q = 0.707) {
  const w = 2 * Math.PI * freq / SR, c = Math.cos(w), s = Math.sin(w), a = s / (2 * q);
  let b0, b1, b2;
  if (type === 'lp') { b0 = (1 - c) / 2; b1 = 1 - c; b2 = (1 - c) / 2; }
  else if (type === 'hp') { b0 = (1 + c) / 2; b1 = -(1 + c); b2 = (1 + c) / 2; }
  else { b0 = a; b1 = 0; b2 = -a; }
  const a0 = 1 + a, a1 = -2 * c, a2 = 1 - a;
  let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  return (x) => {
    const y = (b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2) / a0;
    x2 = x1; x1 = x; y2 = y1; y1 = y;
    return y;
  };
}

class Mix {
  constructor() {
    this.L = new Float32Array(N); this.R = new Float32Array(N);
    this.sendL = new Float32Array(N); this.sendR = new Float32Array(N);
    this.rand = mulberry(42);
  }
  // 把一段声音放进混音：gen(i, tt) 给出第 i 个采样；pan -1 左 +1 右；send 是混响量
  put(t0, dur, gen, { gain = 1, pan = 0, send = 0.15 } = {}) {
    const start = Math.max(0, Math.round(t0 * SR));
    const len = Math.min(Math.round(dur * SR), N - start);
    const gl = gain * Math.cos((pan + 1) * Math.PI / 4), gr = gain * Math.sin((pan + 1) * Math.PI / 4);
    for (let i = 0; i < len; i += 1) {
      const v = gen(i, i / SR);
      const j = start + i;
      this.L[j] += v * gl; this.R[j] += v * gr;
      this.sendL[j] += v * gl * send; this.sendR[j] += v * gr * send;
    }
  }
  noise() { return this.rand() * 2 - 1; }

  // ---- 乐器 ----
  kick(t, gain = 1) {
    let ph = 0;
    this.put(t, 0.45, (i, tt) => {
      ph += 2 * Math.PI * (44 + 120 * Math.exp(-tt * 30)) / SR;
      return Math.sin(ph) * Math.exp(-tt * 8) * (tt < 0.002 ? tt / 0.002 : 1);
    }, { gain, send: 0.02 });
  }
  clap(t, gain = 1) {
    const f = biquad('bp', 1400, 1.2);
    this.put(t, 0.3, (i, tt) => {
      const burst = tt < 0.03 ? (Math.floor(tt / 0.01) % 2 === 0 ? 1 : 0.4) : Math.exp(-(tt - 0.03) * 20);
      return f(this.noise()) * burst * 2.2;
    }, { gain, send: 0.3 });
  }
  hat(t, gain = 1, open = false) {
    const f = biquad('hp', 7500);
    this.put(t, open ? 0.25 : 0.06, (i, tt) => f(this.noise()) * Math.exp(-tt * (open ? 14 : 70)), { gain, pan: 0.25, send: 0.05 });
  }
  shaker(t, gain = 1) {
    const f = biquad('bp', 6000, 0.8);
    this.put(t, 0.09, (i, tt) => f(this.noise()) * Math.sin(Math.PI * Math.min(1, tt / 0.09)) , { gain, pan: -0.2, send: 0.05 });
  }
  bass(t, note, dur, gain = 1) {
    const f = biquad('lp', 520, 1.1), hz = midi(note);
    let ph = 0;
    this.put(t, dur, (i, tt) => {
      ph = (ph + hz / SR) % 1;
      const env = Math.min(1, tt / 0.004) * Math.exp(-tt * 5) * (tt > dur - 0.02 ? (dur - tt) / 0.02 : 1);
      return f(ph * 2 - 1) * env;
    }, { gain, send: 0.02 });
  }
  pad(t, notes, dur, gain = 1, cutoff = 1400) {
    const f = biquad('lp', cutoff, 0.9);
    const phs = notes.flatMap(() => [0, 0.33, 0.66]);
    this.put(t, dur, (i, tt) => {
      let v = 0, k = 0;
      for (const n of notes) {
        for (const det of [-0.08, 0, 0.08]) {
          phs[k] = (phs[k] + midi(n + det) / SR) % 1; v += phs[k] * 2 - 1; k += 1;
        }
      }
      const env = Math.min(1, tt / 0.35) * Math.min(1, (dur - tt) / 0.4);
      return f(v / phs.length) * env;
    }, { gain, send: 0.4 });
  }
  mallet(t, note, gain = 1, pan = 0) {
    const hz = midi(note);
    this.put(t, 1.4, (i, tt) => {
      const a = Math.min(1, tt / 0.002);
      return a * (Math.sin(2 * Math.PI * hz * tt) * Math.exp(-tt * 3.2) + 0.35 * Math.sin(2 * Math.PI * hz * 4 * tt) * Math.exp(-tt * 14));
    }, { gain, pan, send: 0.35 });
  }
  // Karplus-Strong 拨弦：像尤克里里
  pluck(t, note, gain = 1, pan = 0, bright = 0.5) {
    const period = Math.max(2, Math.round(SR / midi(note)));
    const buf = new Float32Array(period);
    for (let i = 0; i < period; i += 1) buf[i] = this.noise();
    let idx = 0, last = 0;
    this.put(t, 1.6, () => {
      const cur = buf[idx];
      const next = buf[(idx + 1) % period];
      const v = (cur * (0.5 + bright * 0.5) + next * (0.5 - bright * 0.5)) * 0.996;
      buf[idx] = v; idx = (idx + 1) % period;
      last = cur;
      return last;
    }, { gain, pan, send: 0.25 });
  }
  blip(t, from, to, dur, gain = 1, pan = 0, wave = 'sine') {
    let ph = 0;
    this.put(t, dur, (i, tt) => {
      const hz = from * Math.pow(to / from, Math.min(1, tt / dur));
      ph = (ph + hz / SR) % 1;
      const v = wave === 'square' ? (ph < 0.5 ? 1 : -1) * 0.5 : Math.sin(2 * Math.PI * ph);
      return v * Math.min(1, tt / 0.003) * Math.exp(-tt / dur * 3);
    }, { gain, pan, send: 0.25 });
  }
  whoosh(t, dur = 0.45, gain = 1, lo = 300, hi = 4000) {
    // Chamberlin 状态变量滤波：中心频率可以逐采样扫，不会咔哒
    let low = 0, band = 0;
    this.put(t, dur, (i, tt) => {
      const fc = lo * Math.pow(hi / lo, tt / dur);
      const f = 2 * Math.sin(Math.PI * Math.min(fc, SR / 6) / SR);
      const high = this.noise() - low - 0.7 * band;
      band += f * high; low += f * band;
      return band * Math.sin(Math.PI * tt / dur) * 1.4;
    }, { gain, send: 0.3 });
  }
  crash(t, gain = 1) {
    const f = biquad('hp', 4000);
    this.put(t, 1.8, (i, tt) => f(this.noise()) * Math.exp(-tt * 2.4), { gain, send: 0.35 });
  }
}

// Schroeder 混响
function reverb(input, roomScale = 1) {
  const out = new Float32Array(N);
  const combs = [1557, 1617, 1491, 1422].map((d) => ({ buf: new Float32Array(Math.round(d * roomScale * SR / 44100)), i: 0 }));
  const aps = [225, 556].map((d) => ({ buf: new Float32Array(Math.round(d * SR / 44100)), i: 0 }));
  for (let n = 0; n < N; n += 1) {
    let s = 0;
    for (const c of combs) { const y = c.buf[c.i]; c.buf[c.i] = input[n] + y * 0.8; c.i = (c.i + 1) % c.buf.length; s += y; }
    s /= combs.length;
    for (const a of aps) { const y = a.buf[a.i]; const v = -s + y; a.buf[a.i] = s + y * 0.5; a.i = (a.i + 1) % a.buf.length; s = v; }
    out[n] = s;
  }
  return out;
}

// ---- 编曲 --------------------------------------------------------------------

const EVENTS = events();
const firstStep = EVENTS.find((e) => e.type === 'step').t;
const BEAT = 60 / 114;
const GRID0 = firstStep % BEAT;
const beatsUntil = (t) => Math.floor((t - GRID0) / BEAT);
const beatAt = (n) => GRID0 + n * BEAT;

function forBeats(from, to, fn) {
  for (let n = Math.max(0, beatsUntil(from) + 1); beatAt(n) < to; n += 1) fn(beatAt(n), n);
}

function neon(m) {
  const chords = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]; // Am F C G
  // 开场：两下重击 + 低频铺底 + 上升
  m.pad(0, [45, 52], 2.4, 0.22, 700);
  forBeats(2.3, 4.6, (t, n) => { m.hat(t, 0.18); m.hat(t + BEAT / 2, 0.12); });
  m.whoosh(3.6, 1.0, 0.35, 200, 6000);
  // 主段
  forBeats(4.5, 12.6, (t, n) => {
    m.kick(t, 0.95);
    if (n % 2 === 1) m.clap(t, 0.45);
    m.hat(t + BEAT / 2, 0.22, n % 4 === 3);
    const bar = Math.floor(n / 4) % 4;
    const root = chords[bar][0] - 24;
    m.bass(t, root, BEAT / 2 - 0.02, 0.5);
    m.bass(t + BEAT / 2, root + (n % 2 ? 12 : 7), BEAT / 2 - 0.02, 0.38);
    if (n % 4 === 0) m.pad(t, chords[bar], BEAT * 4, 0.16, 1800);
  });
  // 结尾
  m.pad(12.6, [57, 60, 64, 69], 2.4, 0.24, 2200);
  forBeats(12.6, 14.4, (t) => m.hat(t, 0.1));
}

function clean(m) {
  const chords = [[60, 64, 67, 71], [57, 60, 64, 67], [53, 57, 60, 64], [55, 59, 62, 67]]; // Cmaj7 Am7 Fmaj7 G
  m.pad(0.2, [48, 55, 64], 4.4, 0.12, 900);
  forBeats(0, 4.6, (t, n) => { if (n % 2 === 0) m.mallet(t, [72, 76, 79, 83][(n / 2) % 4], 0.16, 0.2); });
  forBeats(4.5, 12.6, (t, n) => {
    const bar = Math.floor(n / 4) % 4;
    if (n % 2 === 0) m.kick(t, 0.45);
    m.shaker(t + BEAT / 2, 0.18);
    const notes = chords[bar];
    m.mallet(t, notes[n % 4] + 12, 0.17, (n % 2 ? 0.3 : -0.3));
    if (n % 4 === 0) m.pad(t, notes.map((x) => x - 12), BEAT * 4, 0.1, 1100);
  });
  m.pad(12.6, [48, 55, 64, 71], 2.4, 0.14, 1200);
  [0, 1, 2, 3].forEach((k) => m.mallet(12.9 + k * 0.12, [72, 76, 79, 84][k], 0.2, -0.3 + k * 0.2));
}

function doodle(m) {
  const chords = [[60, 64, 67, 72], [55, 59, 62, 67], [57, 60, 64, 69], [53, 57, 60, 65]]; // C G Am F
  const strum = (t, notes, gain, up = false) => {
    const order = up ? [...notes].reverse() : notes;
    order.forEach((n, i) => m.pluck(t + i * 0.012, n, gain, -0.3 + i * 0.2, 0.3));
  };
  forBeats(0, 4.6, (t, n) => { if (n % 2 === 0) strum(t, chords[0], 0.14); });
  forBeats(4.5, 12.6, (t, n) => {
    const bar = Math.floor(n / 4) % 4;
    strum(t, chords[bar], 0.2);
    if (n % 2 === 0) strum(t + BEAT / 2, chords[bar], 0.12, true);
    if (n % 2 === 1) m.clap(t, 0.35);
    m.shaker(t + BEAT / 2, 0.12);
  });
  forBeats(12.6, 14.6, (t, n) => strum(t, chords[n % 2 ? 1 : 0], 0.16));
}

function sfx(m, style) {
  let popIndex = 0;
  const penta = [72, 74, 76, 79, 81, 84, 86, 88, 91, 93, 96];
  for (const e of EVENTS) {
    switch (e.type) {
      case 'hit':
        if (style === 'neon') { m.kick(e.t, 1); m.crash(e.t, 0.25); m.blip(e.t, 220, 55, 0.5, 0.25, 0, 'square'); }
        else if (style === 'clean') { m.kick(e.t, 0.5); m.mallet(e.t, 60, 0.25); }
        else { m.blip(e.t, 300, 900, 0.25, 0.3); m.clap(e.t, 0.3); }
        break;
      case 'whoosh':
        m.whoosh(e.t - 0.15, 0.4, style === 'clean' ? 0.15 : 0.3);
        break;
      case 'pop': {
        const note = penta[Math.min(penta.length - 1, Math.floor(popIndex / 3))];
        popIndex += 1;
        if (style === 'neon') m.blip(e.t, midi(note), midi(note + 12), 0.06, 0.06, (e.i % 2 ? 0.4 : -0.4), 'square');
        else if (style === 'clean') m.blip(e.t, midi(note), midi(note), 0.08, 0.07, (e.i % 2 ? 0.3 : -0.3));
        else m.blip(e.t, midi(note) * 0.8, midi(note) * 1.3, 0.07, 0.1, (e.i % 2 ? 0.3 : -0.3));
        break;
      }
      case 'link':
        m.blip(e.t, 300, 1200, 0.5, style === 'neon' ? 0.12 : 0.08, 0, style === 'neon' ? 'square' : 'sine');
        break;
      case 'step':
        if (style === 'neon') m.blip(e.t, 880, 440, 0.08, 0.1, e.foot === 'left' ? -0.5 : 0.5, 'square');
        else if (style === 'clean') m.blip(e.t, 520, 260, 0.1, 0.12, e.foot === 'left' ? -0.4 : 0.4);
        else { m.blip(e.t, 700, 700, 0.06, 0.2, e.foot === 'left' ? -0.4 : 0.4); m.blip(e.t, 1400, 1400, 0.03, 0.08); }
        break;
      case 'turn':
        m.whoosh(e.t - 0.05, 0.5, style === 'clean' ? 0.12 : 0.28, 500, 3000);
        break;
      case 'grab':
        m.blip(e.t, 2000, 2000, 0.02, 0.2);
        m.blip(e.t + 0.02, 600, 1200, 0.12, 0.14, 0.3);
        break;
      case 'logo':
        if (style === 'neon') { m.kick(e.t, 1); m.crash(e.t, 0.35); }
        else if (style === 'clean') m.kick(e.t, 0.35);
        else { m.clap(e.t, 0.4); m.blip(e.t, 400, 1600, 0.35, 0.2); }
        break;
      default:
        break;
    }
  }
}

export function renderAudio(style) {
  const m = new Mix();
  ({ neon, clean, doodle })[style](m);
  sfx(m, style);
  const room = style === 'clean' ? 1.3 : 1;
  const wl = reverb(m.sendL, room), wr = reverb(m.sendR, room * 1.03);
  let peak = 0;
  for (let i = 0; i < N; i += 1) {
    m.L[i] += wl[i] * 0.6; m.R[i] += wr[i] * 0.6;
    peak = Math.max(peak, Math.abs(m.L[i]), Math.abs(m.R[i]));
  }
  const scale = 0.9 / Math.max(peak, 1e-6);
  const fadeOut = Math.round(0.6 * SR);
  for (let i = 0; i < N; i += 1) {
    const fade = i > N - fadeOut ? (N - i) / fadeOut : 1;
    m.L[i] = Math.tanh(m.L[i] * scale * 1.1) * fade;
    m.R[i] = Math.tanh(m.R[i] * scale * 1.1) * fade;
  }
  return m;
}

export function writeAudio(style, path) {
  const m = renderAudio(style);
  const data = Buffer.alloc(44 + N * 4);
  data.write('RIFF', 0); data.writeUInt32LE(36 + N * 4, 4); data.write('WAVE', 8);
  data.write('fmt ', 12); data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(2, 22);
  data.writeUInt32LE(SR, 24); data.writeUInt32LE(SR * 4, 28); data.writeUInt16LE(4, 32); data.writeUInt16LE(16, 34);
  data.write('data', 36); data.writeUInt32LE(N * 4, 40);
  for (let i = 0; i < N; i += 1) {
    data.writeInt16LE(Math.round(Math.max(-1, Math.min(1, m.L[i])) * 32767), 44 + i * 4);
    data.writeInt16LE(Math.round(Math.max(-1, Math.min(1, m.R[i])) * 32767), 46 + i * 4);
  }
  writeFileSync(path, data);
  return path;
}
