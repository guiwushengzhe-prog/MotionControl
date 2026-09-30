// 录制：无头 Chromium 逐帧调用 renderAt(t)，截图喂给 ffmpeg，再混上合成的配乐。
//
//   node render/render.mjs                    三种风格都录，输出到 promo/out/
//   node render/render.mjs --style neon       只录一种
//   node render/render.mjs --stills 1,5.5,9   只截这几秒的静帧（调画面时用，快）
//
// 逐帧截图而不是实时录屏：实时录会掉帧、会受机器快慢影响；逐帧的话一帧就是一帧，
// 慢机器只是录得慢，成片完全一样。

import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { writeAudio } from './audio.mjs';

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)));
const OUT = join(ROOT, 'out');
const require = createRequire(import.meta.url);

function arg(name, fallback = null) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : fallback;
}

function loadPlaywright() {
  for (const id of ['playwright', '/opt/node22/lib/node_modules/playwright']) {
    try { return require(id); } catch { /* 试下一个 */ }
  }
  throw new Error('找不到 playwright：npm i -D playwright，或把全局安装的路径加进来');
}

function findFfmpeg() {
  if (process.env.FFMPEG) return process.env.FFMPEG;
  try { execFileSync('ffmpeg', ['-version'], { stdio: 'ignore' }); return 'ffmpeg'; } catch { /* 没装在 PATH 上 */ }
  // Windows 上一般叫 python 或 py，Linux/macOS 叫 python3
  for (const py of ['python3', 'python', 'py']) {
    try {
      return execFileSync(py, ['-c', 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())'], { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
    } catch { /* 这个名字不存在，或没装 imageio-ffmpeg */ }
  }
  throw new Error('找不到 ffmpeg：装一个放进 PATH，或设置 FFMPEG 环境变量');
}

const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.woff': 'font/woff', '.json': 'application/json', '.png': 'image/png' };

function serve(root) {
  const server = createServer((req, res) => {
    const path = normalize(join(root, decodeURIComponent(new URL(req.url, 'http://x').pathname)));
    if (!path.startsWith(root) || !existsSync(path) || statSync(path).isDirectory()) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'content-type': TYPES[extname(path)] || 'application/octet-stream' });
    res.end(readFileSync(path));
  });
  return new Promise((ok) => server.listen(0, '127.0.0.1', () => ok(server)));
}

async function openStage(browser, port, style) {
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  page.on('pageerror', (e) => console.error(`[${style}] 页面报错:`, e.message));
  await page.goto(`http://127.0.0.1:${port}/render/demo.html?style=${style}`);
  await page.waitForFunction(() => window.__ready === true, null, { timeout: 60000 });
  return page;
}

async function stills(browser, port, style, times) {
  const page = await openStage(browser, port, style);
  for (const t of times) {
    await page.evaluate((tt) => window.renderAt(tt), t);
    const file = join(OUT, 'stills', `${style}-${t.toFixed(2)}.png`);
    await page.screenshot({ path: file });
    console.log(file);
  }
  await page.close();
}

async function video(browser, port, style, ffmpeg) {
  const page = await openStage(browser, port, style);
  const meta = await page.evaluate(() => window.__meta);
  const frames = Math.round(meta.duration * meta.fps);
  const wav = join(OUT, `${style}.wav`);
  writeAudio(style, wav);
  const mp4 = join(OUT, `motioncontrol-style-${style}.mp4`);
  const ff = spawn(ffmpeg, [
    '-y', '-loglevel', 'error',
    '-f', 'image2pipe', '-framerate', String(meta.fps), '-c:v', 'mjpeg', '-i', '-',
    '-i', wav,
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '19', '-pix_fmt', 'yuv420p',
    '-c:a', 'aac', '-b:a', '192k', '-shortest', '-movflags', '+faststart', mp4,
  ], { stdio: ['pipe', 'inherit', 'inherit'] });
  const started = Date.now();
  for (let i = 0; i < frames; i += 1) {
    await page.evaluate((tt) => window.renderAt(tt), i / meta.fps);
    const jpg = await page.screenshot({ type: 'jpeg', quality: 95 });
    if (!ff.stdin.write(jpg)) await new Promise((ok) => ff.stdin.once('drain', ok));
    if (i % 90 === 0) console.log(`[${style}] ${i}/${frames} 帧`);
  }
  ff.stdin.end();
  await new Promise((ok, fail) => ff.on('close', (code) => (code === 0 ? ok() : fail(new Error(`ffmpeg 退出码 ${code}`)))));
  await page.close();
  console.log(`[${style}] 完成 ${mp4}（${((Date.now() - started) / 1000).toFixed(0)} 秒）`);
}

async function main() {
  mkdirSync(join(OUT, 'stills'), { recursive: true });
  const styles = arg('style') ? [arg('style')] : ['neon', 'clean', 'doodle'];
  const stillTimes = arg('stills');
  const { chromium } = loadPlaywright();
  const server = await serve(ROOT);
  const port = server.address().port;
  const browser = await chromium.launch();
  try {
    if (stillTimes) {
      const times = stillTimes.split(',').map(Number);
      await Promise.all(styles.map((s) => stills(browser, port, s, times)));
    } else {
      const ffmpeg = findFfmpeg();
      for (const s of styles) await video(browser, port, s, ffmpeg);
    }
  } finally {
    await browser.close();
    server.close();
  }
}

main().catch((e) => { console.error(e); process.exit(1); });
