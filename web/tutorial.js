import {VIEW_CONTROL_CONTENT} from './view-control-guide.js';

/** 新手教学：直接在正式界面上带着做。
 *
 *  最早是一页文字，后来是弹窗里另画的动画，都脱离了真界面。这一版把正式界面压暗，
 *  只留这一步要点的那一块亮着；暗处点不了——正常人本来也不会去点一块黑的地方，
 *  点了只会走岔。按钮是人自己点的真按钮，判定读的是真状态。
 *
 *  卡片上只说「做什么」，不解释为什么：一行大字是动作，最多一行小字是这一刻最
 *  可能卡住的地方。小字不是写死的——同样是接摄像头，这台电脑一个摄像头都没有、
 *  有三个、手机已经在传画面，要做的事完全不同，所以每一步的 guide 都先看环境。
 *
 *  这个模块不碰网络：状态快照由 app.js 给。需要主动问一下环境的（扫摄像头），
 *  也走 app.js 里和「扫描摄像头」按钮同一个函数。
 */

// v2：教学改成在正式界面上带着做，看过旧版的人也该再被带一遍。
const STORE = 'motioncontrol_tutorial_v2';
const TICK_MS = 250;
const $ = id => document.getElementById(id);
const clamp = (value, low, high) => Math.min(Math.max(value, low), Math.max(low, high));
const NAV_PLAY = 'nav [data-view="play"]';
const NAV_DEVICES = 'nav [data-view="devices"]';

// 设置面板里方案叫什么，就照原样说什么；名字只写在一处。
function schemeLabel(value) {
  return (VIEW_CONTROL_CONTENT.horizontal.find(item => item.value === value) || {}).label || value;
}

// 接上画面：每一步都可能先卡在这里，所以单独拿出来。返回 null 表示画面已经好了。
// 顺序就是排查顺序：手机其实已经在传 → 电脑认不了人 → 电脑有几个摄像头 → 开没开成。
function connectGuide(s, memo, now) {
  const repick = memo.repick && !(s.cameraRunning && s.cameraIndex !== memo.repickFrom);
  if (!repick) memo.repick = false;
  if (s.cameraReady && !repick) { memo.noFrameSince = 0; return null; }

  if (s.view !== 'devices') {
    return s.view === 'play'
      ? {target: '.checklist [data-go="devices"]', say: repick ? '点「去连接」，换一个摄像头' : '点「去连接」'}
      : {target: NAV_DEVICES, say: '点「通用设置」'};
  }

  if (s.sourcePick === 'phone') {
    if (s.source !== 'phone') return {target: '#sourceStartBtn', say: '点「连接并开始识别」'};
    return {
      target: ['#poseSource', '#mobileStatus'], say: '打开手机上的 MotionControl',
      hint: `选「固定摄像头」，点「连接并开始」 · ${s.usbTether ? '数据线已接通' : '手机和电脑连同一个 WiFi'}`,
    };
  }

  // 下面都是「电脑摄像头」。
  if (s.phoneStreaming) return {target: '#poseSource', say: '来源改成「手机摄像头」', hint: '手机已经在传画面了'};
  if (s.modelOk === false) return {target: '#poseSource', say: '来源改成「手机摄像头」', hint: '这台电脑的识别模型没装好，电脑摄像头认不出人'};
  if (repick) return {target: ['#cameraDeviceRow', '#sourceStartBtn'], say: '换一个摄像头，点「连接并开始识别」'};

  if (s.cameraRunning) {
    memo.noFrameSince = memo.noFrameSince || now;
    return now - memo.noFrameSince > 5000
      ? {target: ['#cameraDeviceRow', '#sourceStartBtn'], say: '这个摄像头没有画面，换一个', hint: '换好再点「连接并开始识别」'}
      : {target: '#cameraDeviceRow', say: '等画面出来…'};
  }
  memo.noFrameSince = 0;

  // 还不知道这台电脑有几个摄像头：先扫一遍，和「扫描摄像头」按钮是同一件事。
  // 扫的时候不亮「连接」——扫描正在挨个打开设备，这时候去连会撞上。
  if (s.scan === 'idle') return {target: '#cameraDeviceRow', say: '正在找这台电脑的摄像头…', run: 'scanCameras'};
  if (s.scan === 'running') return {target: '#cameraDeviceRow', say: '正在找这台电脑的摄像头…'};
  if (s.cameraError) {
    return s.scanCount > 1
      ? {target: ['#cameraDeviceRow', '#sourceStartBtn'], say: '换一个摄像头再连', hint: '刚才那个打不开'}
      : {target: ['#poseSource', '#sourceStartBtn'], say: '摄像头打不开', hint: '关掉正在用摄像头的软件再连；没有摄像头就改用「手机摄像头」'};
  }
  if (s.scan === 'done' && s.scanCount === 0) {
    return {target: ['#poseSource', '#cameraScanBtn'], say: '没找到摄像头', hint: '被别的软件占着就关掉它再点「扫描摄像头」；没有就改用「手机摄像头」'};
  }
  if (s.scanCount > 1) {
    return {target: ['#cameraDeviceRow', '#sourceStartBtn'], say: '选一个摄像头，点「连接并开始识别」', hint: `找到 ${s.scanCount} 个；画面里不是你就换一个`};
  }
  return {target: '#sourceStartBtn', say: '点「连接并开始识别」', hint: s.scanCount === 1 ? '找到 1 个摄像头' : ''};
}

// 人在不在画面里、站得够不够远。只看头和双肩：程序第一次定位也只要这三样。
function standGuide(s, memo, now) {
  if (s.view !== 'play') return {target: NAV_PLAY, say: '回到「开始」页'};
  if (!s.posed) {
    memo.lostSince = memo.lostSince || now;
    const long = now - memo.lostSince > 8000;
    return {
      target: '#viewer', say: '站到镜头前',
      hint: long ? '还是认不出：开灯，别背对窗户' : '头和两个肩膀都要进画面',
      choice: long && s.scanCount > 1 ? {label: '画面里不是我，换摄像头', run: 'repick'} : null,
    };
  }
  memo.lostSince = 0;
  if (!s.headShoulders) return {target: '#viewer', say: '往后退一点', hint: '头和两个肩膀都要进画面'};
  return null;
}

// 左右三种方案，动作完全不同；读数的正负号程序里是「右为正」。
// 换边要先回正：程序里从一边直接甩到另一边，读数会先归零。
const MOVES = {
  roll_tilt: {left: '头往左肩歪', right: '头往右肩歪', back: side => `回正，再往${side}肩歪`, hint: '脸还朝着屏幕，歪住别动', more: '再歪大一点'},
  head_turn: {left: '向左转头', right: '向右转头', back: side => `回正，再向${side}转头`, hint: '转住别动', more: '再转大一点'},
  hand: {left: '握拳，往左移', right: '握拳，往右移', back: side => `松开，再握拳往${side}移`, hint: '松开就停', more: '再移远一点'},
};

const STEPS = [
  {
    id: 'stand',
    name: '站进画面',
    hold: 1.5,
    doneSay: '✓ 认出来了',
    guide(s, memo, now) {
      return connectGuide(s, memo, now) || standGuide(s, memo, now)
        || {target: '#viewer', say: '稳住别动', ready: true};
    },
    check: s => ({ok: s.posed && s.headShoulders}),
  },
  {
    id: 'look',
    name: '左右转视角',
    doneSay: '✓ 左右都会了',
    guide(s, memo, now) {
      if (s.horizontal === 'off') {
        return s.view !== 'devices'
          ? {target: NAV_DEVICES, say: '点「通用设置」', hint: '左右视角现在是关着的'}
          : {target: '#viewHorizontalSource', say: `「左右控制」选「${schemeLabel('roll_tilt')}」`};
      }
      const before = connectGuide(s, memo, now) || standGuide(s, memo, now);
      if (before) return before;
      // 程序的校准只做一件事：记住你正视屏幕时的样子。所以这里只叫人看屏幕，
      // 左右歪头是校准完成之后的事。
      if (s.calibrating) return {target: '#calBtn', say: '看着屏幕中心，别动'};
      if (!s.calibrated) {
        return {target: '#calBtn', say: '看向屏幕中心，点「站好并校准」', hint: s.calibrationNote || '别看摄像头；倒计时完之前别动'};
      }
      const move = MOVES[s.horizontal] || MOVES.hand;
      const want = memo.left ? 'right' : 'left';
      const other = want === 'left' ? 'right' : 'left';
      const x = s.outputX;
      const toward = Number.isFinite(x) && (want === 'left' ? -x : x);
      const hint = s.guardBlocked ? '身子别晃，只动头'
        : toward >= 8 && toward < 35 ? move.more
        : move.hint;
      return {
        target: ['#viewer', '#headStatus'], focus: '#headStatus', ready: true, hint,
        say: memo[other] ? move.back(want === 'left' ? '左' : '右') : move[want],
      };
    },
    check(s, memo, ready) {
      const x = s.outputX;
      if (ready && Number.isFinite(x)) {
        if (x <= -35) memo.left = true;
        if (x >= 35) memo.right = true;
      }
      return {ok: !!(memo.left && memo.right), targets: [{label: '← 左', hit: !!memo.left}, {label: '右 →', hit: !!memo.right}]};
    },
  },
];

export function createTutorial(actions = {}) {
  const root = $('tour');
  if (!root) return {open() {}, close() {}, seen: () => true};
  const spot = $('tourSpot'), card = $('tourCard'), dock = document.querySelector('.command-dock');
  const blocks = [...root.querySelectorAll('.tour-block')];

  let index = 0, memos = {}, done = new Set(), skipped = new Set(), ran = new Set();
  let holdSince = 0, doneAt = 0, advance = 0, timer = 0, frame = 0, moveTimer = 0, nudgeTimer = 0;
  let active = false, returnFocus = null, targetKey = '', targetEls = [], focusEl = null, layoutKey = '', chipsKey = '', choice = null;

  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || '{}');
    done = new Set(Array.isArray(saved.done) ? saved.done : []);
    skipped = new Set(Array.isArray(saved.skipped) ? saved.skipped : []);
  } catch { /* 存不上就每次从头开始，不值得为此挡住教学 */ }

  function save() {
    try {
      localStorage.setItem(STORE, JSON.stringify({done: [...done], skipped: [...skipped], seen: 1}));
    } catch { /* 隐私模式下写不了，忽略 */ }
  }

  // 只在文字真的变了时才写：卡片每 250ms 刷一次，整段重写会让读屏软件一直念。
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

  function place(el, left, top, width, height) {
    Object.assign(el.style, {left: `${left}px`, top: `${top}px`, width: `${Math.max(0, width)}px`, height: `${Math.max(0, height)}px`});
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
    const key = [r ? [r.left, r.top, r.right, r.bottom].map(Math.round).join() : '-', vw, vh, cw, ch, Math.round(left), Math.round(top), side, Math.round(arrow)].join('|');
    if (key === layoutKey) return;
    layoutKey = key;
    spot.hidden = !r;
    root.classList.toggle('no-target', !r);
    // 亮框四周各一块透明挡板：暗处点不了，亮框里点得到下面的真按钮。
    const [topBlock, rightBlock, bottomBlock, leftBlock] = blocks;
    if (r) {
      place(spot, r.left, r.top, r.right - r.left, r.bottom - r.top);
      place(topBlock, 0, 0, vw, r.top);
      place(bottomBlock, 0, r.bottom, vw, vh - r.bottom);
      place(leftBlock, 0, r.top, r.left, r.bottom - r.top);
      place(rightBlock, r.right, r.top, vw - r.right, r.bottom - r.top);
    } else {
      place(topBlock, 0, 0, vw, vh);
      for (const block of [rightBlock, bottomBlock, leftBlock]) place(block, 0, 0, 0, 0);
    }
    Object.assign(card.style, {left: `${left}px`, top: `${top}px`});
    card.dataset.side = side;
    card.style.setProperty('--arrow', `${arrow}px`);
  }

  // 点到暗处：不责怪，只把人的眼睛拉回亮着的那一块。
  function nudge() {
    for (const el of [spot, card]) el.classList.remove('nudge');
    void card.offsetWidth;
    for (const el of [spot, card]) el.classList.add('nudge');
    clearTimeout(nudgeTimer);
    nudgeTimer = setTimeout(() => { for (const el of [spot, card]) el.classList.remove('nudge'); }, 650);
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
      chip.textContent = target.label + (target.hit ? ' ✓' : '');
      return chip;
    }));
  }

  function render(step, guide, result, ok) {
    setText('tourStep', `新手教学 · ${index + 1}/${STEPS.length} · ${step.name}`);
    setText('tourSay', ok ? step.doneSay : guide.say);
    $('tourSay').classList.toggle('done', ok);
    const hint = ok ? '' : guide.hint || '';
    setText('tourHint', hint);
    $('tourHint').hidden = !hint;
    // 目标格子只在真正开始做动作时才出现：校准时就摆出「← 左 右 →」，人会以为现在就该歪头。
    renderChips(guide.ready || ok ? result.targets || [] : []);

    const need = Number(step.hold || 0);
    $('tourHold').hidden = !need || !guide.ready || ok;
    if (!$('tourHold').hidden) {
      const held = holdSince ? (performance.now() - holdSince) / 1000 : 0;
      $('tourHoldFill').style.width = `${Math.min(100, held / need * 100)}%`;
    }
    spot.classList.toggle('done', ok);

    choice = ok ? null : guide.choice || null;
    $('tourChoiceBtn').hidden = !choice;
    if (choice) setText('tourChoiceBtn', choice.label);
    $('tourSkipBtn').hidden = ok;
    $('tourNextBtn').hidden = true;
  }

  function renderFinish() {
    setTarget(null);
    setText('tourStep', '新手教学');
    const count = STEPS.filter(step => done.has(step.id)).length;
    setText('tourSay', count === STEPS.length ? '✓ 学会了' : `学了 ${count} / ${STEPS.length} 步`);
    $('tourSay').classList.toggle('done', count === STEPS.length);
    setText('tourHint', '想再练，点顶上的「新手教学」');
    $('tourHint').hidden = false;
    for (const id of ['tourTargets', 'tourHold', 'tourSkipBtn', 'tourChoiceBtn']) $(id).hidden = true;
    spot.classList.remove('done');
    $('tourNextBtn').hidden = false;
  }

  function tick() {
    if (!active) return;
    if (index >= STEPS.length) { renderFinish(); return; }
    const state = actions.state?.();
    if (!state) return;
    const step = STEPS[index];
    const memo = memos[step.id] || (memos[step.id] = {});
    const now = performance.now();
    const guide = step.guide(state, memo, now);
    // 需要主动问一下环境的（比如扫摄像头），每次打开教学只问一次。
    if (guide.run && !ran.has(guide.run)) {
      ran.add(guide.run);
      actions[guide.run]?.();
    }
    setTarget(guide.target, guide.focus);
    const result = step.check(state, memo, !!guide.ready);

    // 做到了就不再往回判：一次抖动不该把刚亮起来的判定又灭掉。
    let ok = !!doneAt;
    if (!ok) {
      const good = !!guide.ready && result.ok, need = Number(step.hold || 0);
      if (good) { if (!holdSince) holdSince = now; } else holdSince = 0;
      ok = good && (!need || (now - holdSince) / 1000 >= need);
      if (ok) {
        doneAt = now;
        done.add(step.id);
        skipped.delete(step.id);
        save();
        advance = setTimeout(() => go(index + 1), 1300);
      }
    }
    render(step, guide, result, ok);
  }

  function go(i) {
    clearTimeout(advance);
    advance = 0;
    index = clamp(i, 0, STEPS.length);
    holdSince = 0;
    doneAt = 0;
    // 重进一步就重来一次：上一轮打中的目标不能替这一轮算数。
    if (index < STEPS.length) memos[STEPS[index].id] = {};
    save();
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
    ran = new Set();
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

  for (const block of blocks) block.addEventListener('click', nudge);
  // 「跳过教学」是给老玩家的：一下关掉，以后也不再自己弹出来。
  $('tourCloseBtn').addEventListener('click', close);
  $('tourNextBtn').addEventListener('click', close);
  $('tourSkipBtn').addEventListener('click', () => {
    if (index >= STEPS.length) return;
    skipped.add(STEPS[index].id);
    save();
    go(index + 1);
  });
  $('tourChoiceBtn').addEventListener('click', () => {
    if (!choice || index >= STEPS.length) return;
    // 目前唯一的选择是「画面里不是我」：记下现在是哪个摄像头，回设备页换一个，
    // 换成别的序号并且开起来了才算换完。
    if (choice.run === 'repick') {
      const memo = memos[STEPS[index].id] || (memos[STEPS[index].id] = {});
      const state = actions.state?.() || {};
      memo.repick = true;
      memo.repickFrom = state.cameraIndex;
      memo.lostSince = 0;
    }
    tick();
  });

  return {
    open,
    close,
    // 从没打开过才自动弹一次。跳过、关掉、学完，都算看过了。
    seen: () => { try { return !!JSON.parse(localStorage.getItem(STORE) || '{}').seen; } catch { return true; } },
  };
}
