// 动作库：自带、下载、自己录的动作卡片，官方动作库。
import {api,configurationOperation,isVisible,notice,post} from './core.js';
import {MOTION_CONFLICT_NAMES,poseLibraryNames,profileTriggers,triggerMapped} from './labels.js';
import {libraryKeyLabel,renderProfileBindingRows,revealBindingRow,saveProfileBindings,showMapTab} from './mapping.js';
import {kernelState} from './play.js';
import {setLibraryOpen} from './shell.js';
import {S,gameProfile} from './state.js';

export let poseLibrary=[];
export let customPoses=[];

/* --- 自定义姿势 ---------------------------------------------------------
 * 摆一个姿势录下来，之后做出同样的动作就触发。
 *
 * 识别出来的姿势走的是和内置 hands_cross 同一条通路（pose.<id>），所以它们自动
 * 出现在上面的映射列表里，按游戏分别绑键、冲突检查、紧急停止一起松开——全是现成
 * 的。这里只管录制和调参。
 */
const customPoseListEl = document.getElementById('customPoseList');

const customPoseStatusEl = document.getElementById('customPoseStatus');

function customPoseSay(text, kind = '') {
  if (!customPoseStatusEl) return;
  customPoseStatusEl.textContent = text;
  customPoseStatusEl.className = kind === 'error' ? 'statusline error' : 'statusline';
}

export async function refreshCustomPoses({ rebuild = true } = {}) {
  try {
    if(rebuild)return await configurationOperation(async()=>{
      const success=await refreshCustomPoses({rebuild:false});
      if(success)renderProfileBindingRows();
      return success;
    });
    const data = await api('/api/pose/custom');
    customPoses = data.poses || [];
    S.customPoseScores = data.scores || {};
    // 上次选的准备时间。不放回去的话，每次打开都回到默认值，下一次点按钮又把默认
    // 值存回服务端——记住就等于没记。
    const delay = document.getElementById('customPoseDelay');
    if (delay && data.delay_s && [...delay.options].some(option => option.value === String(data.delay_s))) {
      delay.value = String(data.delay_s);
    }
    renderCustomPoses();
    // 触发器列表变了，映射界面要重建才能看到新姿势。轮询刷新分数时不重建，
    // 否则用户正在编辑的那一行会被冲掉。
    return true;
  } catch (error) {
    customPoseSay(error.message, 'error');
    return false;
  }
}

/** 倒计时期间可以取消——按错了不用等它数完。 */
/**
 * 倒计时现在归服务端。这里只负责"把倒计时设上"和"把剩几秒画出来"。
 *
 * 为什么挪走：这个按钮天生该能用嘴按——人站在镜头前几米外摆姿势，够不着鼠标。而
 * 语音是电脑那边处理的。倒计时留在这里的话，口令触发的那一次就得让服务端反过来
 * 指挥页面，于是同一件事两套倒计时，迟早对不上。
 *
 * 结果：不管是点按钮还是说口令，走的都是同一条路，页面上看到的也是同一个数字。
 */
async function scheduleCapture(purpose, extra = {}) {
  const delay = Number(document.getElementById('customPoseDelay')?.value || 5);
  try {
    const data = await post('/api/pose/custom/schedule', { purpose, delay_s: delay, ...extra });
    paintPoseCountdown(data.pose_capture);
  } catch (error) {
    customPoseSay(error.message, 'error');
  }
}

async function cancelCapture() {
  try {
    paintPoseCountdown((await post('/api/pose/custom/cancel', {})).pose_capture);
  } catch (error) {
    customPoseSay(error.message, 'error');
  }
}

function applyPoses(data) {
  customPoses = data.poses || [];
  renderCustomPoses();
  renderProfileBindingRows();
}

async function captureCustomPose() {
  if (poseCountdownActive) { await cancelCapture(); return; }
  const nameInput = document.getElementById('customPoseName');
  await scheduleCapture('capture', { name: nameInput?.value || '' });
  if (nameInput) nameInput.value = '';
}

async function appendCustomPoseFrame(item) {
  if (poseCountdownActive) { await cancelCapture(); return; }
  await scheduleCapture('frame', { id: item.id });
}

let poseCountdownActive = false;

let poseCaptureMessage = '';

/**
 * 把服务端那份倒计时画出来。数字放大显示在按钮上：这时候人站在几米外，小字看不见。
 *
 * 只认状态、不自己计时——自己再数一遍就是第二套倒计时，和服务端那套迟早差开。
 */
export function paintPoseCountdown(state) {
  const button = document.getElementById('customPoseCaptureBtn');
  if (!button) return;
  const counting = !!state?.counting;
  const purpose = String(state?.purpose || 'capture');
  const wasActive = poseCountdownActive;
  poseCountdownActive = counting;

  if (counting) {
    const left = Math.max(1, Math.ceil(Number(state.remaining_s) || 0));
    if (purpose === 'capture') {
      const recorder = document.getElementById('customPoseRecorder');
      if (recorder) recorder.hidden = false;
      button.classList.add('counting');
      button.textContent = String(left);
    } else {
      button.classList.remove('counting');
      button.textContent = '录下当前姿势';
    }
    // 给哪一条加姿势，就让那一条的按钮自己数，不然人不知道拍的是哪个动作。
    for (const row of document.querySelectorAll('.custom-pose')) {
      const add = row.querySelector('.pose-add');
      if (!add) continue;
      const mine = purpose === 'frame' && row.dataset.id === String(state.pose_id || '');
      add.classList.toggle('counting', mine);
      add.textContent = mine ? String(left) : '再加一个姿势';
    }
    customPoseSay(`${left} 秒后拍下当前姿势，摆好别动（再点一次或说「取消」都能停）`);
    return;
  }

  button.classList.remove('counting');
  button.textContent = '录下当前姿势';
  for (const add of document.querySelectorAll('.pose-add')) {
    add.classList.remove('counting');
    add.textContent = '再加一个姿势';
  }
  // 拍完了（或者被取消了）才去取新的姿势列表。轮询每 250ms 一次，不加这个判断
  // 就是每秒四次白跑一趟。
  const message = String(state?.message || '');
  if (wasActive || (message && message !== poseCaptureMessage)) {
    poseCaptureMessage = message;
    const failed = message.includes('没') || message.includes('失败');
    if (message) customPoseSay(message, failed ? 'error' : '');
    // 录好了，录制面板合上：新录的动作已经出现在卡片里了。
    if (wasActive && purpose === 'capture' && !failed) setRecorderOpen(false);
    void refreshCustomPoses();
  }
}

async function removeCustomPoseFrame(item, index) {
  try {
    await configurationOperation(async()=>{applyPoses(await post('/api/pose/custom/frame/remove', { id: item.id, index }))});
  } catch (error) {
    customPoseSay(error.message, 'error');
  }
}

/** 录制瞬间的骨架，画成一个小人。用户靠它认出这是哪个姿势。 */
function poseThumbnail(preview) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 100 100');
  svg.classList.add('pose-thumb');
  if (!preview || !preview.points) {
    svg.classList.add('empty');
    return svg;
  }
  const at = name => {
    const p = preview.points[name];
    return p ? [p[0] * 90 + 5, p[1] * 90 + 5] : null;
  };
  for (const [a, b] of preview.bones || []) {
    const from = at(a), to = at(b);
    if (!from || !to) continue;
    const line = document.createElementNS(NS, 'line');
    line.setAttribute('x1', from[0]); line.setAttribute('y1', from[1]);
    line.setAttribute('x2', to[0]); line.setAttribute('y2', to[1]);
    svg.appendChild(line);
  }
  for (const name of Object.keys(preview.points)) {
    const at_ = at(name);
    const dot = document.createElementNS(NS, 'circle');
    dot.setAttribute('cx', at_[0]); dot.setAttribute('cy', at_[1]);
    // 头稍大一点，一眼能看出人是正着还是倒着。
    dot.setAttribute('r', name === 'nose' ? 4 : 2.4);
    svg.appendChild(dot);
  }
  return svg;
}

async function updateCustomPose(id, changes) {
  try {
    await configurationOperation(async()=>{
    const data = await post('/api/pose/custom/update', Object.assign({ id }, changes));
    customPoses = data.poses || [];
    renderCustomPoses();
    renderProfileBindingRows();
    });
  } catch (error) {
    customPoseSay(error.message, 'error');
  }
}

async function removeCustomPose(item) {
  if (!confirm('删除「' + item.name + '」？绑在它上面的按键映射也会失效。')) return;
  try {
    await configurationOperation(async()=>{
    const data = await post('/api/pose/custom/remove', { id: item.id });
    customPoses = data.poses || [];
    customPoseSay('已删除「' + item.name + '」');
    renderCustomPoses();
    renderProfileBindingRows();
    });
  } catch (error) {
    customPoseSay(error.message, 'error');
  }
}

function customPoseSlider(labelText, input, format) {
  const label = document.createElement('label');
  const value = document.createElement('span');
  value.className = 'custom-pose-value';
  value.textContent = format(Number(input.value));
  input.addEventListener('input', () => { value.textContent = format(Number(input.value)); });
  label.append(labelText, input, value);
  return label;
}

// 展开过「调整」的自己录的动作，重画之后还展开着。
const expandedPoses = new Set();

/* 自己录的动作和别的动作放在同一排卡片里，正面长得一样：示范（录下来的第一个姿势）、
 * 名字、键位、一句怎么做。像到多少才算、保持多久、再加一个姿势、删除这些，收在卡片
 * 的「调整」里——录完一般不用动。 */
function renderCustomPoses() {
  if (!customPoseListEl) return;
  const scores = document.getElementById('customPoseScoresBtn');
  if (scores) scores.hidden = !customPoses.length;
  customPoseListEl.replaceChildren();

  for (const item of customPoses) {
    const card = document.createElement('div');
    card.className = 'pose-library-item custom-pose';
    card.dataset.id = item.id;

    const demo = document.createElement('div');
    demo.className = 'pose-demo';
    demo.appendChild(poseThumbnail((item.previews || [])[0]));

    const name = document.createElement('span');
    name.className = 'pose-library-name';
    name.textContent = item.name;
    // 绑的是哪个键。只显示，不在这里改——同一个东西两处能改，就一定会有一处
    // 是旧的。点它跳到上面那张表里对应的一行，改在那边。
    const bound = document.createElement('button');
    bound.type = 'button';
    bound.className = 'custom-pose-key';
    bound.textContent = libraryKeyLabel('pose.' + item.id);
    bound.title = '点一下去绑键、改键';
    bound.addEventListener('click', () => revealBindingRow('pose.' + item.id));
    const head = document.createElement('div');
    head.className = 'pose-library-head';
    head.append(name, bound);

    const how = document.createElement('div');
    how.className = 'pose-library-how';
    how.textContent = item.frames > 1 ? `${item.frames} 个姿势，按顺序做完按一下` : '摆着这个姿势就一直按';

    // 实时相似度。没有它，调「像到」只能靠猜。平时藏着，点「显示相似度」才出来。
    const meter = document.createElement('div');
    meter.className = 'custom-pose-meter';
    const fill = document.createElement('div');
    fill.className = 'custom-pose-fill';
    const readout = document.createElement('span');
    readout.className = 'custom-pose-score';
    meter.append(fill, readout);

    const source = document.createElement('div');
    source.className = 'pose-library-source';
    const origin = document.createElement('span');
    origin.textContent = item.enabled ? '自己录' : '自己录 · 已停用';
    const adjust = document.createElement('button');
    adjust.type = 'button';
    adjust.className = 'btn link';
    adjust.textContent = '调整';
    const open = expandedPoses.has(item.id);
    adjust.setAttribute('aria-expanded', String(open));
    source.append(origin, adjust);

    const panel = document.createElement('div');
    panel.className = 'custom-pose-panel';
    panel.hidden = !open;
    adjust.addEventListener('click', () => {
      const next = panel.hidden;
      panel.hidden = !next;
      adjust.setAttribute('aria-expanded', String(next));
      if (next) expandedPoses.add(item.id); else expandedPoses.delete(item.id);
    });

    const rename = document.createElement('input');
    rename.className = 'custom-pose-name field';
    rename.value = item.name;
    rename.maxLength = 20;
    rename.setAttribute('aria-label', '名字');
    rename.addEventListener('change', () => updateCustomPose(item.id, { name: rename.value }));

    // 关键帧一排。多于一帧就是连贯动作，要按顺序依次做出来。
    const strip = document.createElement('div');
    strip.className = 'pose-strip';
    (item.previews || []).forEach((preview, index) => {
      if (index) {
        const arrow = document.createElement('span');
        arrow.className = 'pose-arrow';
        arrow.textContent = '→';
        strip.appendChild(arrow);
      }
      const cell = document.createElement('div');
      cell.className = 'pose-cell';
      // 当前等着的那一帧高亮：动作断在哪一步，用户一眼能看见。
      if (item.frames > 1 && index === item.step) cell.classList.add('awaiting');
      cell.appendChild(poseThumbnail(preview));
      if (item.frames > 1) {
        const drop = document.createElement('button');
        drop.className = 'pose-drop';
        drop.type = 'button';
        drop.textContent = '×';
        drop.title = `删掉第 ${index + 1} 个姿势`;
        drop.addEventListener('click', () => removeCustomPoseFrame(item, index));
        cell.appendChild(drop);
      }
      strip.appendChild(cell);
    });
    const addFrame = document.createElement('button');
    addFrame.className = 'btn pose-add';
    addFrame.type = 'button';
    addFrame.textContent = '再加一个姿势';
    addFrame.title = '摆好下一个姿势再点。几个姿势要按顺序做完才触发';
    addFrame.addEventListener('click', () => appendCustomPoseFrame(item));
    strip.appendChild(addFrame);

    const threshold = document.createElement('input');
    threshold.type = 'range';
    threshold.min = '50'; threshold.max = '99'; threshold.step = '1';
    threshold.value = String(Math.round(item.threshold * 100));
    threshold.addEventListener('change', () =>
      updateCustomPose(item.id, { threshold: Number(threshold.value) / 100 }));

    const dwell = document.createElement('input');
    dwell.type = 'range';
    dwell.min = '1'; dwell.max = '60'; dwell.step = '1';
    dwell.value = String(item.dwell_frames);
    dwell.addEventListener('change', () =>
      updateCustomPose(item.id, { dwell_frames: Number(dwell.value) }));

    const tools = document.createElement('div');
    tools.className = 'custom-pose-tools';
    tools.append(
      customPoseSlider('像到', threshold, v => v + '% 才算'),
      // 帧数对用户没有意义，换算成秒。30fps 是相机的常见帧率。
      customPoseSlider(item.frames > 1 ? '最后一个保持' : '保持', dwell, v => (v / 30).toFixed(2) + ' 秒'));
    if (item.frames > 1) {
      const window_ = document.createElement('input');
      window_.type = 'range';
      window_.min = '3'; window_.max = '100'; window_.step = '1';  // 0.3 ~ 10 秒
      window_.value = String(Math.round(item.step_window_s * 10));
      window_.addEventListener('change', () =>
        updateCustomPose(item.id, { step_window_s: Number(window_.value) / 10 }));
      tools.append(customPoseSlider('每步最多', window_, v => (v / 10).toFixed(1) + ' 秒'));
    }

    const enabled = document.createElement('label');
    enabled.className = 'custom-pose-enabled';
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.className = 'switch';
    toggle.checked = item.enabled;
    toggle.addEventListener('change', () => updateCustomPose(item.id, { enabled: toggle.checked }));
    enabled.append('启用', toggle);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn link danger';
    remove.textContent = '删除';
    remove.addEventListener('click', () => removeCustomPose(item));

    const actions = document.createElement('div');
    actions.className = 'custom-pose-actions';
    actions.append(enabled, remove);

    const hint = document.createElement('p');
    hint.className = 'fineprint';
    hint.textContent = '「像到」设得比摆好时的相似度略低一点；「保持」用来滤掉路过的动作。';

    panel.append(rename, strip, tools, hint, actions);
    card.append(demo, poseCardBody(head, how, meter), source, panel);
    customPoseListEl.appendChild(card);
  }
  paintCustomPoseScores();
}

// 「录一个自己的动作」那张卡片：点开录制面板，录完或者点收起就合上。
function setRecorderOpen(open) {
  const recorder = document.getElementById('customPoseRecorder');
  if (!recorder) return;
  recorder.hidden = !open;
  document.getElementById('customPoseOpenBtn')?.setAttribute('aria-expanded', String(open));
  if (open) {
    recorder.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    document.getElementById('customPoseName')?.focus();
  }
}

document.getElementById('customPoseOpenBtn')?.addEventListener('click', () =>
  setRecorderOpen(document.getElementById('customPoseRecorder').hidden));

document.getElementById('customPoseCloseBtn')?.addEventListener('click', () => setRecorderOpen(false));

/** 只改数字和进度条，不重建 DOM——每秒重建会把用户正在拖的滑块打断。 */
export function paintCustomPoseScores() {
  if (!isVisible(customPoseListEl)) return;
  const active = new Set(kernelState?.poses_active || []);
  for (const row of customPoseListEl.querySelectorAll('.custom-pose')) {
    const id = row.dataset.id;
    const item = customPoses.find(p => p.id === id);
    const score = Number(S.customPoseScores[id] ?? 0);
    const fill = row.querySelector('.custom-pose-fill');
    const readout = row.querySelector('.custom-pose-score');
    if (fill) fill.style.width = Math.round(score * 100) + '%';
    if (readout) readout.textContent = Math.round(score * 100) + '%';
    row.classList.toggle('hit', !!(item && score >= item.threshold));
    row.classList.toggle('firing', active.has(id));
    // 键位标签跟着一起刷。上面改了绑定，这里得马上跟上，不然就是两处各说各的。
    const bound = row.querySelector('.custom-pose-key');
    if (bound) {
      const label = libraryKeyLabel('pose.' + id);
      if (bound.textContent !== label) bound.textContent = label;
      bound.classList.toggle('add', label === '加到映射' || label === '没绑键');
    }
  }
}

/** 相似度条的开关。做动作的人在几米外，够不着鼠标——所以是走开前按一下的开关，
 *  不是 hover 或 focus 那种"手在鼠标上"才成立的触发。 */
document.getElementById('customPoseScoresBtn')?.addEventListener('click', event => {
  const on = customPoseListEl.classList.toggle('show-scores');
  event.currentTarget.classList.toggle('on', on);
  event.currentTarget.textContent = on ? '隐藏相似度' : '显示相似度';
});

document.getElementById('customPoseCaptureBtn')?.addEventListener('click', captureCustomPose);

/* --- 动作库 ---------------------------------------------------------------
 * 做好的身体动作，每个配一个一直在做示范的火柴人。名字、怎么做、示范、星级、会扫过
 * 哪些圈都是电脑那边 motioncontrol_shared/pose_library.py 给的，这里只画。
 *
 * 程序只自带原地踏步、小腿向后抬起；别的动作在下面的「官方动作库」里，下载了才认得
 * 出来（识别规则跟着动作一起下载，电脑那边验过签名才装）。
 */
const poseLibraryEl = document.getElementById('poseLibraryList');

const poseCloudEl = document.getElementById('poseCloudList');

let ratingNames = {intensity: '运动强度', recognition: '识别度', difficulty: '上手难度'};

let bodyPartNames = {legs: '腿部', glutes: '臀部', core: '核心', arms: '手臂', shoulders: '肩背'};

export async function refreshPoseLibrary({rebuild=true}={}) {
  if (!poseLibraryEl) return;
  if(rebuild&&gameProfile.selected)return configurationOperation(async()=>{
    await refreshPoseLibrary({rebuild:false});renderProfileBindingRows();
  });
  const data = await api('/api/pose/library');
  poseLibrary = data.library || [];
  ratingNames = data.rating_names || ratingNames;
  bodyPartNames = data.body_part_names || bodyPartNames;
  poseLibraryNames.cloud = {...poseLibraryNames.cloud, ...(data.cloud_names || {})};
  if (data.error) poseCloudSay(data.error, 'error');
  renderPoseLibrary();
  // 映射表可能在动作库读回来之前就画好了，那时下载的动作还不在触发器里。
  if(rebuild&&gameProfile.selected&&poseLibrary.some(item=>item.source==='cloud'))await configurationOperation(async()=>{renderProfileBindingRows()});
  paintPoseMissingNotice();
}

/** 下载或删掉一个动作之后：映射表里多一行或少一行。先把没存的改动落盘，再重画。 */
async function afterPoseLibraryChange(library) {
  poseLibrary = library || poseLibrary;
  renderPoseLibrary();
  await saveProfileBindings();
  renderProfileBindingRows();
  paintPoseMissingNotice();
  if (poseCloudItems.length) renderPoseCloud();
}

/** 示范的几帧叠在一起，一次只露一帧；换帧由下面那个计时器做。 */
function poseDemo(demo) {
  const box = document.createElement('div');
  box.className = 'pose-demo';
  box.dataset.frameMs = String(Math.round((Number(demo?.frame_s) || 0.5) * 1000));
  (demo?.frames || []).forEach((frame, index) => {
    const svg = poseThumbnail(frame);
    if (index) svg.classList.add('off');
    box.appendChild(svg);
  });
  return box;
}

// 一个计时器管所有示范。这一页没打开时什么都不做：看不见的动画白费电。
setInterval(() => {
  if (!poseLibraryEl?.offsetParent) return;
  const now = performance.now();
  for (const box of document.querySelectorAll('#poseLibraryPanel .pose-demo')) {
    const frames = box.children;
    if (frames.length < 2 || now < Number(box.dataset.next || 0)) continue;
    const at = (Number(box.dataset.at || 0) + 1) % frames.length;
    [...frames].forEach((svg, index) => svg.classList.toggle('off', index !== at));
    box.dataset.at = String(at);
    box.dataset.next = String(now + Number(box.dataset.frameMs));
  }
}, 80);

/** 星级：三项 1~5 星，锻炼部位各自打星。一个动作卡片上都是同一套写法。 */
const RATING_SHORT = {intensity: '强度', recognition: '识别', difficulty: '难度'};

function poseRatings(item) {
  const box = document.createElement('div');
  box.className = 'pose-ratings';
  for (const [key, label] of Object.entries(ratingNames)) {
    const count = Number(item.ratings?.[key] || 0);
    if (!count) continue;
    const row = document.createElement('div');
    row.className = 'pose-rating';
    row.title = `${label}：${count} / 5`;
    const name = document.createElement('span');
    name.textContent = RATING_SHORT[key] || label;
    const dots = document.createElement('span');
    dots.className = 'pose-dots';
    for (let i = 1; i <= 5; i++) { const dot = document.createElement('i'); if (i <= count) dot.className = 'on'; dots.append(dot); }
    row.append(name, dots);
    box.appendChild(row);
  }
  const parts = Object.entries(item.body_parts || {}).sort((a, b) => b[1] - a[1]);
  if (parts.length) {
    const row = document.createElement('div');
    row.className = 'pose-parts';
    row.textContent = '练 ' + parts.map(([key]) => bodyPartNames[key] || key).join(' · ');
    row.title = '锻炼部位，越靠前练得越多';
    box.appendChild(row);
  }
  return box;
}

/** 示范右边那一栏：名字、怎么做、星级竖着排。 */
function poseCardBody(...parts) {
  const body = document.createElement('div');
  body.className = 'pose-library-body';
  body.append(...parts);
  return body;
}

function renderPoseLibrary() {
  if (!poseLibraryEl) return;
  poseLibraryEl.replaceChildren();
  for (const item of poseLibrary) {
    const card = document.createElement('div');
    card.className = 'pose-library-item';
    card.dataset.id = item.id;

    const name = document.createElement('span');
    name.className = 'pose-library-name';
    name.textContent = item.name;
    // 绑的是哪个键。和自定义动作一样只显示，点它跳到映射表里那一行去改。
    const bound = document.createElement('button');
    bound.type = 'button';
    bound.className = 'custom-pose-key';
    bound.title = '点一下跳到上面的按键映射';
    bound.addEventListener('click', () => revealBindingRow(item.trigger));
    const head = document.createElement('div');
    head.className = 'pose-library-head';
    head.append(name, bound);

    const how = document.createElement('div');
    how.className = 'pose-library-how';
    how.textContent = item.how;

    // 从哪来：自带的删不掉；下载的写第几版，能删。
    const source = document.createElement('div');
    source.className = 'pose-library-source';
    if (item.source === 'cloud') {
      source.append(`官方 · 第 ${item.revision} 版`);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn pose-library-remove';
      remove.textContent = '删除';
      remove.addEventListener('click', () => removePoseAction(item));
      source.append(remove);
    } else {
      source.textContent = '自带';
    }
    // 会扫过哪些框不写在卡片上：绑了键以后，那个框「更多」里会写。
    card.append(poseDemo(item.demo), poseCardBody(head, how, poseRatings(item)), source);
    poseLibraryEl.appendChild(card);
  }
  paintPoseLibrary();
}

async function removePoseAction(item) {
  const bound = triggerMapped(item.trigger);
  const warning = bound ? '这个游戏里它绑着键，删掉后那一行会失效，直到重新下载。' : '以后要用再从官方动作库下载。';
  if (!confirm(`删掉「${item.name}」？${warning}`)) return;
  try {
    await configurationOperation(async()=>{
    const data = await post('/api/pose/remove', { id: item.id });
    poseCloudSay(`已删掉「${item.name}」`);
    await afterPoseLibraryChange(data.library);
    });
  } catch (error) { poseCloudSay(error.message, 'error'); }
}

/* --- 官方动作库 -----------------------------------------------------------
 * 云端官方发布的动作。点「下载」，电脑那边从云端取回动作文件、验过签名装上，本机
 * 动作库和映射表里就多了它。列表只在点开时读取，缓存过期时在可见面板里跟进刷新。
 */
let poseCloudItems = [];
let poseCloudCache={},poseCloudRequest=0,poseCloudPoll=null,poseCloudFingerprint='';
let poseCloudLocalLibrary=poseLibrary;

function poseCloudSay(text, kind = '') {
  const el = document.getElementById('poseCloudStatus');
  // 官方动作库那一块收着的时候（比如在卡片上删了一个动作），话说在页面底下的提示里。
  if (!el || document.getElementById('poseCloudPanel')?.hidden) { if (text) notice(text); return; }
  el.textContent = text;
  el.className = kind === 'error' ? 'statusline error' : 'statusline';
}

// 「官方动作库」那张卡片：点开才去云端读列表，读到的动作用和上面一样的卡片列出来。
async function openPoseCloud() {
  const panel = document.getElementById('poseCloudPanel');
  const button = document.getElementById('poseCloudBtn');
  if (panel) panel.hidden = false;
  button?.setAttribute('aria-expanded', 'true');
  await refreshPoseCloud();
}

async function refreshPoseCloud(background=false) {
  const panel=document.getElementById('poseCloudPanel');
  const request=++poseCloudRequest;
  const localLibrary=poseLibrary;
  clearTimeout(poseCloudPoll);poseCloudPoll=null;
  if(!background)poseCloudSay('正在读官方动作库…');
  try {
    const data = await api('/api/pose/cloud', { timeoutMs: 20000 });
    if(request!==poseCloudRequest||panel?.hidden)return;
    poseCloudItems = data.actions || [];
    poseCloudCache=data.cache||{};
    poseCloudLocalLibrary=localLibrary;
    ratingNames = data.rating_names || ratingNames;
    bodyPartNames = data.body_part_names || bodyPartNames;
    for (const item of poseCloudItems) poseLibraryNames.cloud[item.id] = item.name;
    renderPoseCloud();
    paintPoseMissingNotice();
    if(poseCloudCache.refreshing&&isVisible(panel))poseCloudPoll=setTimeout(()=>{
      if(request===poseCloudRequest&&isVisible(panel))void refreshPoseCloud(true);
    },650);
  } catch (error) {
    if(request===poseCloudRequest&&!panel?.hidden)poseCloudSay(error.message, 'error');
  }
}

function closePoseCloud() {
  ++poseCloudRequest;clearTimeout(poseCloudPoll);poseCloudPoll=null;
  const panel = document.getElementById('poseCloudPanel');
  if (panel) panel.hidden = true;
  document.getElementById('poseCloudBtn')?.setAttribute('aria-expanded', 'false');
}

function renderPoseCloud() {
  if (!poseCloudEl) return;
  if(poseCloudLocalLibrary!==poseLibrary){
    // Local install/remove responses own installation state. A cloud list
    // started before that change must not restore its previous revision.
    ++poseCloudRequest;clearTimeout(poseCloudPoll);poseCloudPoll=null;
    for(const item of poseCloudItems){
      item.installed_revision=poseLibrary.find(local=>local.id===item.id)?.revision||0;
      item.update_available=Boolean(item.installed_revision)&&Number(item.revision)>Number(item.installed_revision);
    }
    poseCloudLocalLibrary=poseLibrary;
  }
  const fresh = poseCloudItems.filter(item => !item.installed_revision).length;
  const updated=Number(poseCloudCache.updated_at);
  const stamp=Number.isFinite(updated)&&updated>0?` · 更新于 ${new Date(updated*1000).toLocaleString()}`:'';
  const cacheStatus=poseCloudCache.offline?' · 云端暂不可用，显示上次获取的结果':poseCloudCache.refreshing?' · 正在后台检查更新':'';
  const summary=poseCloudItems.length
    ? (fresh ? `${fresh} 个还没下载` : '官方的动作都下载了')
    : '暂时还没有发布的动作';
  poseCloudSay(summary+cacheStatus+stamp,poseCloudCache.offline?'error':'');
  // 卡片上那句话跟着变：还有几个能下载。
  const hint = document.getElementById('poseCloudHint');
  if (hint && poseCloudItems.length) hint.textContent = fresh ? `还有 ${fresh} 个动作可以下载` : '官方的动作都下载了';
  // 已经下载、也没有新版的不再列一遍：上面的动作库里已经有它们了。
  const wanted = poseCloudItems.filter(item => !item.installed_revision || item.update_available);
  poseCloudEl.hidden = !wanted.length;
  const fingerprint=JSON.stringify(wanted);
  if(fingerprint===poseCloudFingerprint)return;
  poseCloudFingerprint=fingerprint;
  poseCloudEl.replaceChildren();
  for (const item of wanted) {
    const card = document.createElement('div');
    card.className = 'pose-library-item';
    card.dataset.id = item.id;
    const name = document.createElement('span');
    name.className = 'pose-library-name';
    name.textContent = item.name;
    const head = document.createElement('div');
    head.className = 'pose-library-head';
    head.append(name);
    const how = document.createElement('div');
    how.className = 'pose-library-how';
    how.textContent = item.how;
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'btn';
    if (!item.installed_revision) {
      action.classList.add('primary');
      action.textContent = '下载';
    } else if (item.update_available) {
      action.textContent = `更新到第 ${item.revision} 版`;
    } else {
      action.textContent = '已下载';
      action.disabled = true;
    }
    action.addEventListener('click', () => {
      ++poseCloudRequest;clearTimeout(poseCloudPoll);poseCloudPoll=null;
      void installPoseAction(item, action);
    });
    const source = document.createElement('div');
    source.className = 'pose-library-source';
    source.append(`官方 · 第 ${item.revision} 版`, action);
    card.append(poseDemo(item.demo), poseCardBody(head, how, poseRatings(item)), source);
    poseCloudEl.appendChild(card);
  }
}

document.getElementById('poseCloudCloseBtn')?.addEventListener('click', closePoseCloud);

async function installPoseAction(item, button) {
  button.disabled = true;
  poseCloudSay(`正在下载「${item.name}」…`);
  try {
    await configurationOperation(async()=>{
    const data = await post('/api/pose/cloud/install', { id: item.id }, 20000);
    item.installed_revision = item.revision;
    item.update_available = false;
    poseCloudSay(`「${item.name}」已下载，点它卡片上的「加到映射」绑键`);
    await afterPoseLibraryChange(data.library);
    });
  } catch (error) {
    button.disabled = false;
    poseCloudSay(error.message, 'error');
  }
}

document.getElementById('poseCloudBtn')?.addEventListener('click', () => {
  if (document.getElementById('poseCloudPanel')?.hidden) openPoseCloud(); else closePoseCloud();
});

/** 这份配置绑了还没下载的动作：那几行现在不会触发。说清楚是哪几个、去哪下载。
 *  不自动下载——以后要和账号、收费一起考虑。 */
export function paintPoseMissingNotice() {
  const box = document.getElementById('poseMissingNotice');
  if (!box) return;
  const known = new Set(profileTriggers().map(trigger => trigger.key));
  const missing = [];
  for (const [group, prefix] of [['motions', 'motion'], ['poses', 'pose']]) {
    const items = gameProfile.selected?.bindings?.[group] || {};
    for (const [id, binding] of Object.entries(items)) {
      if (!binding || binding.disabled || !binding.action?.target) continue;
      if (id.startsWith('custom') || known.has(`${prefix}.${id}`)) continue;
      missing.push(poseLibraryNames.cloud[id] || MOTION_CONFLICT_NAMES[id] || id);
    }
  }
  box.hidden = !missing.length;
  const text = missing.length
    ? `这份配置用到了还没下载的动作：${missing.join('、')}，现在不会触发`
    : '';
  const label = box.querySelector('span');
  if (label && label.textContent !== text) label.textContent = text;
}

document.getElementById('poseMissingGo')?.addEventListener('click', () => {
  showMapTab('body');
  setLibraryOpen(true);
  document.getElementById('poseCloudPanel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (!poseCloudItems.length) openPoseCloud();
});

/** 只改键位和提醒，不重建。 */
export function paintPoseLibrary() {
  if (!isVisible(poseLibraryEl)) return;
  let used = 0;
  for (const card of poseLibraryEl.querySelectorAll('.pose-library-item')) {
    const item = poseLibrary.find(entry => entry.id === card.dataset.id);
    if (!item) continue;
    const bound = card.querySelector('.custom-pose-key');
    const label = libraryKeyLabel(item.trigger);
    if (bound) {
      if (bound.textContent !== label) bound.textContent = label;
      bound.classList.toggle('add', label === '加到映射' || label === '没绑键');
    }
    if (triggerMapped(item.trigger)) used++;
  }
  const count = document.getElementById('libraryCount');
  const text = `${poseLibrary.length} 个${used ? ` · ${used} 个在用` : ''}`;
  if (count && count.textContent !== text) count.textContent = text;
}
