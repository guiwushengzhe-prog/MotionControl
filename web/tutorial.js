import {VIEW_CONTROL_CONTENT} from './view-control-guide.js';

/** 新手教学：直接在正式界面上带着做。
 *
 *  最早是一页文字；上一版改成弹窗里另画一段演示动画。两种都脱离了真界面：在弹窗
 *  里看懂了，关掉之后照样不知道真按钮在哪、真读数看哪一行。所以这一版不画任何
 *  替身——把正式界面压暗，只留这一步要用的那一块亮着，旁边一张小卡片说做什么。
 *  按钮是人自己去点的真按钮，判定读的是真状态，画面里是他自己的骨架。
 *
 *  指引跟着状态走：没画面就指「去连接」，到了设备页就指「连接并开始识别」，没校准
 *  就指「站好并校准」。人被带着走一遍的，就是他以后每次都要走的那条路。
 *
 *  这个模块不碰网络：状态快照由 app.js 给，那些变量本来就被现有的轮询刷新着。
 *  亮框不挡点击，人随时可以去点别处——这是带路，不是锁屏。
 */

const STORE = 'motioncontrol_tutorial_v1';
const TICK_MS = 250;
const $ = id => document.getElementById(id);
const clamp = (value, low, high) => Math.min(Math.max(value, low), Math.max(low, high));

// 视角那步的说明直接取「视角控制」里的那份。同一件事只写一处，改了设置面板就
// 等于改了教学，不会再出现两边说法对不上。
function axisText(value) {
  return (VIEW_CONTROL_CONTENT.horizontal.find(item => item.value === value) || {}).description || '';
}

// guide(state) 说此刻该亮哪一块、卡片上写什么。ready 表示前置条件都齐了、真正
// 在做这一步的动作——只有这时判定才算数，否则人还在去连接的路上计时就开始走了。
const STEPS = [
  {
    id: 'stand',
    name: '站进画面',
    goal: '先让它认出你这个人',
    condition: '「开始」页的画面里认出人体，并且稳住 1.5 秒',
    praise: '认出来了。以后画面上有骨架，就说明它看得见你。',
    hold: 1.5,
    guide: state => {
      if (!state.cameraReady) {
        if (state.view !== 'devices') {
          return {target: '.checklist [data-go="devices"]', text: '现在还没有画面。点亮着的「去连接」，到设备页把摄像头接上。'};
        }
        return {target: ['#poseSource', '#sourceStartBtn'], text: '摄像头来源选好（笔记本自带的就行），然后点「连接并开始识别」。'};
      }
      if (state.view !== 'play') {
        return {target: '[data-view="play"]', text: '画面接上了。点「开始」回主页，看左边那块实时画面。'};
      }
      return {
        target: '#viewer', ready: true,
        text: '退到能看见头和两个肩膀的地方，通常是两米外。认出来之后，画面上会画出你的骨架。',
      };
    },
    check: state => ({
      ok: state.posed,
      detail: state.posed ? '看见你了，稳住别走开' : '画面里还没找到人：别逆光，太暗就开灯',
    }),
  },
  {
    id: 'look',
    name: '左右转视角',
    goal: '把视角推到左边，再推到右边',
    condition: '画面下方那行读数，左右各推到一次 35% 以上',
    praise: '左右都推到了。以后视角不听话，先看这行读数。',
    guide: state => {
      if (state.horizontal === 'off') {
        if (state.view !== 'devices') {
          return {target: '[data-view="devices"]', text: '左右视角现在是关着的。到「通用设置」里的「视角控制」把它打开。'};
        }
        return {target: '#viewHorizontalSource', text: '在「左右控制」里选一个方案，推荐「头部侧倾」。'};
      }
      if (state.view !== 'play') {
        return {target: '[data-view="play"]', text: '回「开始」页练这一步。'};
      }
      if (state.calibrating) {
        return {target: '#calBtn', text: '正在校准：看着屏幕中心，别动。'};
      }
      if (!state.calibrated) {
        return {target: '#calBtn', text: '先让它记住哪边是正前方：站直、脸朝屏幕中心，点「站好并校准」。说「开始校准」也行。'};
      }
      // 画面比屏幕高时，读数那一行最要紧——这一步教的就是看它。
      return {
        target: ['#viewer', '#headStatus'], focus: '#headStatus', ready: true,
        text: `${axisText(state.horizontal)}边做边看画面下方那行读数。`,
      };
    },
    check: (state, memo, ready) => {
      const x = state.outputX;
      if (ready && Number.isFinite(x)) {
        if (x <= -35) memo.left = true;
        if (x >= 35) memo.right = true;
      }
      return {
        ok: !!(memo.left && memo.right),
        detail: Number.isFinite(x) ? `现在：${x < 0 ? '左' : '右'} ${Math.abs(x).toFixed(0)}%` : '还没有读数',
        targets: [{label: '向左推到 35%', hit: !!memo.left}, {label: '向右推到 35%', hit: !!memo.right}],
      };
    },
  },
];

export function createTutorial(actions = {}) {
  const root = $('tour');
  if (!root) return {open() {}, close() {}, seen: () => true};
  const spot = $('tourSpot'), card = $('tourCard'), dock = document.querySelector('.command-dock');

  let index = 0, reached = 0, memos = {}, done = new Set(), skipped = new Set();
  let holdSince = 0, doneAt = 0, advance = 0, timer = 0, frame = 0, moveTimer = 0;
  let active = false, returnFocus = null, targetKey = '', targetEls = [], focusEl = null, layoutKey = '', chipsKey = '';

  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || '{}');
    done = new Set(Array.isArray(saved.done) ? saved.done : []);
    skipped = new Set(Array.isArray(saved.skipped) ? saved.skipped : []);
    reached = clamp(Number(saved.reached) || 0, 0, STEPS.length - 1);
  } catch { /* 存不上就每次从头开始，不值得为此挡住教学 */ }

  function save() {
    try {
      localStorage.setItem(STORE, JSON.stringify({done: [...done], skipped: [...skipped], reached, seen: 1}));
    } catch { /* 隐私模式下写不了，忽略 */ }
  }

  // 只在文字真的变了时才写：这张卡片每 250ms 刷一次，整段重写会让读屏软件一直念。
  function setText(id, text) {
    const el = $(id);
    if (el.textContent !== text) el.textContent = text;
  }

  function setTarget(target, focus) {
    const key = JSON.stringify([target || null, focus || null]);
    if (key === targetKey) return;
    targetKey = key;
    targetEls = (target ? [].concat(target) : []).map(sel => document.querySelector(sel)).filter(Boolean);
    focusEl = (focus && document.querySelector(focus)) || targetEls[0] || null;
    // 换目标时亮框滑过去，眼睛才跟得上；平时不带过渡，否则滚动页面时框会拖在后面。
    spot.classList.add('moving');
    clearTimeout(moveTimer);
    moveTimer = setTimeout(() => spot.classList.remove('moving'), 420);
    reveal();
  }

  // 把亮着的那块滚进视野。顶栏是吸顶的，会盖住页面上方一截，所以「看得见」要从
  // 顶栏下沿算起。整块放得下就整块露出来；放不下（画面比屏幕高）就保证 focus 那
  // 一块露出来，它本身也太高时对齐它的上沿——人头和肩膀在上面。
  function reveal() {
    const box = targetRect();
    if (!box || !focusEl || targetEls.every(el => el.closest('.command-dock'))) return;
    const covered = dock ? dock.getBoundingClientRect().bottom : 0, m = 16, room = innerHeight - covered - 2 * m;
    const f = focusEl.getBoundingClientRect();
    let delta = 0;
    if (box.bottom - box.top <= room) {
      if (box.top < covered + m) delta = box.top - covered - m;
      else if (box.bottom > innerHeight - m) delta = box.bottom - innerHeight + m;
    } else if (f.height > room || f.top < covered + m) {
      delta = f.top - covered - m;
    } else if (f.bottom > innerHeight - m) {
      delta = f.bottom - innerHeight + m;
    }
    if (Math.abs(delta) > 4) window.scrollBy({top: delta, behavior: 'smooth'});
  }

  function targetRect() {
    let box = null;
    for (const el of targetEls) {
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) continue; // 在另一页里藏着
      box = box ? {left: Math.min(box.left, r.left), top: Math.min(box.top, r.top), right: Math.max(box.right, r.right), bottom: Math.max(box.bottom, r.bottom)}
        : {left: r.left, top: r.top, right: r.right, bottom: r.bottom};
    }
    return box;
  }

  // 每帧跟一次位置：顶栏是吸顶的、页面会滚，亮框得贴着真元素走。只有数字变了才写样式。
  function layout() {
    frame = requestAnimationFrame(layout);
    const vw = innerWidth, vh = innerHeight, m = 12, gap = 16, pad = 8;
    const t = targetRect();
    const r = t && {left: t.left - pad, top: t.top - pad, right: t.right + pad, bottom: t.bottom + pad};
    const cw = card.offsetWidth, ch = card.offsetHeight;
    let left, top, side = 'none', arrow = 0;
    if (!r || vw < 760) {
      left = Math.max(m, (vw - cw) / 2);
      top = vh - ch - m;
    } else {
      const midY = clamp((r.top + r.bottom) / 2 - ch / 2, m, vh - ch - m), atX = clamp(r.left, m, vw - cw - m);
      const pick = [
        ['right', r.right + gap, midY, r.right + gap + cw <= vw - m],
        ['left', r.left - gap - cw, midY, r.left - gap - cw >= m],
        ['below', atX, r.bottom + gap, r.bottom + gap + ch <= vh - m],
        ['above', atX, r.top - gap - ch, r.top - gap - ch >= m],
      ].find(option => option[3]);
      if (pick) [side, left, top] = pick;
      else { left = vw - cw - m; top = vh - ch - m; }
      arrow = side === 'right' || side === 'left'
        ? clamp((r.top + r.bottom) / 2 - top, 22, ch - 22)
        : clamp((r.left + r.right) / 2 - left, 22, cw - 22);
    }
    const key = [r ? [r.left, r.top, r.right, r.bottom].map(Math.round).join() : '-', Math.round(left), Math.round(top), side, Math.round(arrow)].join('|');
    if (key === layoutKey) return;
    layoutKey = key;
    spot.hidden = !r;
    root.classList.toggle('no-target', !r);
    if (r) Object.assign(spot.style, {left: `${r.left}px`, top: `${r.top}px`, width: `${r.right - r.left}px`, height: `${r.bottom - r.top}px`});
    Object.assign(card.style, {left: `${left}px`, top: `${top}px`});
    card.dataset.side = side;
    card.style.setProperty('--arrow', `${arrow}px`);
  }

  function renderDots() {
    $('tourDots').replaceChildren(...STEPS.map((step, i) => {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = (done.has(step.id) ? '✓ ' : `${i + 1} `) + step.name;
      btn.classList.toggle('active', i === index);
      btn.classList.toggle('done', done.has(step.id));
      btn.disabled = i > reached;
      btn.addEventListener('click', () => go(i));
      li.append(btn);
      return li;
    }));
  }

  function renderChips(targets) {
    const box = $('tourTargets');
    box.hidden = !targets.length;
    const key = JSON.stringify(targets);
    if (key === chipsKey) return;
    chipsKey = key;
    box.replaceChildren(...targets.map(target => {
      const chip = document.createElement('span');
      chip.className = 'tour-chip' + (target.hit ? ' hit' : '');
      chip.textContent = (target.hit ? '✓ ' : '') + target.label;
      return chip;
    }));
  }

  function render(step, guide, result, ok) {
    setText('tourStep', `新手教学 · 第 ${index + 1} 步，共 ${STEPS.length} 步`);
    setText('tourGoal', step.goal);
    setText('tourText', ok ? step.praise : guide.text);
    setText('tourCondition', step.condition);
    $('tourCheck').hidden = false;
    renderChips(result.targets || []);

    const need = Number(step.hold || 0);
    $('tourHold').hidden = !need || !guide.ready || ok;
    if (!$('tourHold').hidden) {
      const held = holdSince ? (performance.now() - holdSince) / 1000 : 0;
      $('tourHoldFill').style.width = `${Math.min(100, held / need * 100)}%`;
    }
    const detail = guide.ready && !ok ? result.detail || '' : '';
    setText('tourDetail', detail);
    $('tourDetail').hidden = !detail;
    $('tourDone').hidden = !ok;
    spot.classList.toggle('done', ok);

    $('tourSkipBtn').hidden = ok || done.has(step.id);
    const next = $('tourNextBtn');
    setText('tourNextBtn', index === STEPS.length - 1 ? '学完了' : '下一步');
    next.disabled = !(ok || done.has(step.id) || skipped.has(step.id));
  }

  function renderFinish() {
    setTarget(null);
    const count = STEPS.filter(step => done.has(step.id)).length;
    setText('tourStep', '新手教学 · 结束');
    setText('tourGoal', count === STEPS.length ? '这几步都做到了' : `做到了 ${count} / ${STEPS.length} 步`);
    setText('tourText', '以后随时能从页面顶部的「新手教学」再走一遍，想练哪一步点下面哪一步。装驱动、手机当摄像头这些打开页面之前的事，在《新手指南》里。');
    for (const id of ['tourCheck', 'tourTargets', 'tourHold', 'tourDetail', 'tourDone', 'tourSkipBtn']) $(id).hidden = true;
    spot.classList.remove('done');
    setText('tourNextBtn', '关闭');
    $('tourNextBtn').disabled = false;
  }

  function tick() {
    if (!active) return;
    if (index >= STEPS.length) { renderFinish(); return; }
    const state = actions.state?.();
    if (!state) return;
    const step = STEPS[index];
    const memo = memos[step.id] || (memos[step.id] = {});
    const guide = step.guide(state);
    setTarget(guide.target, guide.focus);
    const result = step.check(state, memo, !!guide.ready);

    // 做到了就不再往回判：一次抖动不该把刚亮起来的判定又灭掉。
    let ok = !!doneAt;
    if (!ok) {
      const good = !!guide.ready && result.ok, need = Number(step.hold || 0), now = performance.now();
      if (good) { if (!holdSince) holdSince = now; } else holdSince = 0;
      ok = good && (!need || (now - holdSince) / 1000 >= need);
      if (ok) {
        doneAt = now;
        done.add(step.id);
        skipped.delete(step.id);
        reached = clamp(index + 1, reached, STEPS.length - 1);
        save();
        renderDots();
        advance = setTimeout(() => go(index + 1), 1600);
      }
    }
    render(step, guide, result, ok);
  }

  function go(i) {
    clearTimeout(advance);
    advance = 0;
    index = clamp(i, 0, STEPS.length);
    reached = Math.max(reached, Math.min(STEPS.length - 1, index));
    holdSince = 0;
    doneAt = 0;
    // 重进一步就重来一次：上一轮打中的目标不能替这一轮算数。
    if (index < STEPS.length) memos[STEPS[index].id] = {};
    save();
    renderDots();
    tick();
  }

  function onKey(event) {
    // 校准那种正式弹窗开着时，Esc 是它的，不能顺手把教学也关了。
    if (event.key === 'Escape' && !document.querySelector('dialog[open]')) close();
  }

  function open(source) {
    returnFocus = source || null;
    // 从第一步还没做、也没跳过的地方接着；全都过了就从头再走一遍。
    const next = STEPS.findIndex(step => !done.has(step.id) && !skipped.has(step.id));
    index = next === -1 ? 0 : next;
    active = true;
    root.hidden = false;
    targetKey = '';
    layoutKey = '';
    go(index);
    clearInterval(timer);
    timer = setInterval(tick, TICK_MS);
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(layout);
    document.addEventListener('keydown', onKey);
  }

  function close() {
    active = false;
    clearInterval(timer);
    cancelAnimationFrame(frame);
    clearTimeout(advance);
    advance = 0;
    root.hidden = true;
    document.removeEventListener('keydown', onKey);
    save();
    returnFocus?.focus?.();
  }

  $('tourCloseBtn').addEventListener('click', close);
  $('tourNextBtn').addEventListener('click', () => (index >= STEPS.length ? close() : go(index + 1)));
  $('tourSkipBtn').addEventListener('click', () => {
    if (index >= STEPS.length) return;
    skipped.add(STEPS[index].id);
    save();
    go(index + 1);
  });

  return {
    open,
    close,
    // 从没打开过才自动弹一次。跳过、关掉、学完，都算看过了。
    seen: () => { try { return !!JSON.parse(localStorage.getItem(STORE) || '{}').seen; } catch { return true; } },
  };
}
