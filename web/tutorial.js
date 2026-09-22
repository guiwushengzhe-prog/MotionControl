import {VIEW_CONTROL_CONTENT} from './view-control-guide.js';

/** 新手教学：把原来那页纯文字的「操作指导」换成跟着做的引导。
 *
 *  旧版是六条文字步骤。可是体感软件最难回答的那个问题——「我这个动作，它到底
 *  认出来没有」——文字回答不了：读完的人照样不知道自己做对没有，只能回界面接着
 *  乱试。所以这一版一次只教一个动作，配一段循环演示的动画，做到没做到直接读实时
 *  状态：动作对了，判定自己亮。人第一次确认「它看见我了」，就在这一下。
 *
 *  这个模块不碰网络也不碰全局状态：app.js 每 250ms 把一份状态快照喂进来，教学
 *  只负责判定和画面。真要动手的那几步（校准）走 actions 里的回调，仍旧是 app.js
 *  原来那条路径，不在这里另开一份。
 */

const STORE = 'motioncontrol_tutorial_v1';
const $ = id => document.getElementById(id);

// 每步一段循环演示，内联 SVG + CSS 动画：不引资源、不用 canvas，而且只有当前
// 这一步的图在 DOM 里，换步才重建——后台不会有几段动画一起空转。
const FIGURE = '<circle cx="160" cy="62" r="13"/><path d="M160 75v34M140 88l20-6 20 6M160 109l-13 30M160 109l13 30"/>';
// 视角那步共用这条标尺：左右各一个目标，中间一个游标。演示里游标走到哪，下面
// 那条实时读数就该走到哪——两边对得上，人才知道自己做的是不是同一件事。
const TRACK = '<path class="tut-track" d="M52 146h216"/>'
  + '<path class="tut-target tut-target-l" d="M60 139l7 7-7 7-7-7z"/>'
  + '<path class="tut-target tut-target-r" d="M260 139l7 7-7 7-7-7z"/>'
  + '<path class="tut-reticle" d="M160 132l9 14-9 14-9-14z"/>';

const SCENES = {
  stand:
    '<g class="tut-frame"><path d="M62 52V34h20M258 52V34h-20M62 128v18h20M258 128v18h-20"/></g>'
    + '<g class="tut-walk tut-ink">' + FIGURE + '</g>',
  headRoll:
    '<g class="tut-roll tut-ink"><circle cx="160" cy="54" r="26"/>'
    + '<path d="M134 47a7 8 0 1 0 0 15M186 47a7 8 0 1 1 0 15"/>'
    + '<circle class="tut-fill" cx="150" cy="50" r="3"/><circle class="tut-fill" cx="170" cy="50" r="3"/>'
    + '<path d="M151 64q9 7 18 0M160 80v18"/></g>'
    + '<path d="M126 112q34-16 68 0"/>' + TRACK,
  headTurn:
    '<g class="tut-yaw tut-ink"><circle cx="160" cy="54" r="26"/>'
    + '<path d="M134 47a7 8 0 1 0 0 15M186 47a7 8 0 1 1 0 15"/>'
    + '<g class="tut-face"><circle class="tut-fill" cx="150" cy="50" r="3"/>'
    + '<circle class="tut-fill" cx="170" cy="50" r="3"/><path d="M160 50v14l-7 4M151 72q9 6 18 0"/></g>'
    + '<path d="M160 80v18"/></g>'
    + '<path d="M126 112q34-16 68 0"/>' + TRACK,
  handMove:
    '<g class="tut-slide tut-ink"><rect x="134" y="64" width="52" height="44" rx="14"/>'
    + '<path d="M140 64q6-9 12 0q6-9 12 0q6-9 12 0"/><path d="M186 78h8a9 9 0 0 1 0 18h-8"/></g>'
    + TRACK,
  done:
    '<circle class="tut-ring" cx="160" cy="90" r="52"/>'
    + '<path class="tut-tick tut-tick-big" d="M128 92l22 24 46-56"/>',
};

// 视角那步的第一句话直接取「视角控制」里的那份说明。同一件事只写一处，改了设置
// 面板就等于改了教学，不会出现两边说法对不上的老毛病。
function axisText(items, value) {
  return (items.find(item => item.value === value) || {}).description || '';
}

const STEPS = [
  {
    id: 'stand',
    name: '站进画面',
    goal: '先让它认出你这个人',
    scene: () => 'stand',
    how: () => [
      '退到能看见头和两个肩膀的距离，通常是两米外。',
      '要用踏步、抬腿这类动作的话，脚也得拍得到——再退一点，或者把摄像头架高。',
      '逆光和太暗都会认不出来。认出来之后，左边画面上会画出骨架。',
    ],
    condition: '画面里认出人体，并且稳住 1.5 秒',
    hold: 1.5,
    check: state => ({
      ok: state.posed,
      detail: state.posed ? '看见你了'
        : state.cameraReady ? '画面里还没找到人'
        : '还没有画面：到「通用设置 → 输入来源」连上摄像头',
    }),
  },
  {
    id: 'look',
    name: '左右转视角',
    goal: '把视角推到左边，再推到右边',
    scene: state => ({roll_tilt: 'headRoll', head_turn: 'headTurn'}[state.horizontal] || 'handMove'),
    how: state => [
      axisText(VIEW_CONTROL_CONTENT.horizontal, state.horizontal),
      '下面那条标尺是实时读数：你往哪边推，游标就往哪边走。',
      '左右各推到一次就行。推过去之后读数会停在那儿，说明它在持续转——回正才停。',
    ].filter(Boolean),
    condition: '左右两边各推到一次（读数超过 35%）',
    skipWhen: state => state.horizontal === 'off'
      ? '当前设置把左右视角关掉了。想练这一步，先到「视角控制」里把左右选上。' : null,
    // 没校准的话读数没有基准，练也是白练。所以这一步自己把校准按钮带上，而不是
    // 把人赶回主界面自己找。
    action: state => state.calibrated && !state.calibrating ? null
      : {label: state.calibrating ? '取消校准' : '先校准一下', run: 'calibrate'},
    check: (state, memo) => {
      const x = state.outputX;
      if (state.calibrated && Number.isFinite(x)) {
        if (x <= -35) memo.left = true;
        if (x >= 35) memo.right = true;
      }
      return {
        ok: !!(memo.left && memo.right),
        detail: state.calibrating ? '正在校准，站好别动'
          : !state.calibrated ? '还没校准，读数没有基准'
          : Number.isFinite(x) ? `当前 ${x < 0 ? '左' : '右'} ${Math.abs(x).toFixed(0)}%` : '还没有读数',
        targets: [{label: '向左 35%', hit: !!memo.left}, {label: '向右 35%', hit: !!memo.right}],
        meter: state.calibrated && Number.isFinite(x) ? Math.max(-1, Math.min(1, x / 100)) : null,
      };
    },
  },
];

export function createTutorial(actions = {}) {
  const root = $('tutorialMask');
  if (!root) return {open() {}, close() {}, update() {}, isOpen: () => false, seen: () => true};

  let index = 0, reached = 0, memos = {}, done = new Set(), skipped = new Set();
  let holdSince = 0, doneAt = 0, advance = 0, sceneNow = '', opened = false, returnFocus = null;
  let lastState = null;

  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || '{}');
    done = new Set(Array.isArray(saved.done) ? saved.done : []);
    skipped = new Set(Array.isArray(saved.skipped) ? saved.skipped : []);
    reached = Math.min(STEPS.length - 1, Math.max(0, Number(saved.reached) || 0));
  } catch { /* 存不上就每次从头开始，不值得为此挡住教学 */ }

  function save() {
    try {
      localStorage.setItem(STORE, JSON.stringify({done: [...done], skipped: [...skipped], reached, seen: 1}));
    } catch { /* 隐私模式下写不了，忽略 */ }
  }

  function renderRail() {
    const rail = $('tutorialRail');
    rail.replaceChildren(...STEPS.map((step, i) => {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'tut-rail-item';
      const mark = document.createElement('span');
      mark.className = 'tut-rail-mark';
      mark.textContent = done.has(step.id) ? '✓' : skipped.has(step.id) ? '—' : String(i + 1);
      const name = document.createElement('span');
      name.textContent = step.name;
      btn.append(mark, name);
      btn.classList.toggle('done', done.has(step.id));
      btn.classList.toggle('skipped', skipped.has(step.id) && !done.has(step.id));
      btn.classList.toggle('active', i === index);
      btn.disabled = i > reached;
      btn.addEventListener('click', () => go(i));
      li.append(btn);
      return li;
    }));
    const count = STEPS.filter(step => done.has(step.id)).length;
    $('tutorialCount').textContent = `已完成 ${count} / ${STEPS.length}`;
    $('tutorialBarFill').style.width = `${Math.round(count / STEPS.length * 100)}%`;
  }

  function fillHow(lines) {
    $('tutorialHow').replaceChildren(...lines.map(text => {
      const li = document.createElement('li');
      li.textContent = text;
      return li;
    }));
  }

  function renderStage(state) {
    const step = STEPS[index];
    const scene = step.scene(state);
    // 动画只在换步或换方案时重建；每 250ms 重塞一次 innerHTML，演示会永远停在第一帧。
    if (scene === sceneNow) return;
    sceneNow = scene;
    $('tutorialArt').innerHTML =
      `<svg class="tut-svg" viewBox="0 0 320 168" aria-hidden="true">${SCENES[scene] || ''}</svg>`;
    $('tutorialKicker').textContent = `第 ${index + 1} 步 · 共 ${STEPS.length} 步`;
    $('tutorialGoal').textContent = step.goal;
    fillHow(step.how(state));
    $('tutorialCondition').textContent = step.condition;
  }

  function renderFoot(state, result, ok, blocked) {
    const step = STEPS[index];
    const targets = result.targets || [];
    const list = $('tutorialTargets');
    list.hidden = !targets.length;
    if (targets.length) {
      list.replaceChildren(...targets.map(target => {
        const chip = document.createElement('span');
        chip.className = 'tut-target-chip' + (target.hit ? ' hit' : '');
        chip.textContent = (target.hit ? '✓ ' : '') + target.label;
        return chip;
      }));
    }

    const meter = $('tutorialMeter');
    meter.hidden = !Number.isFinite(result.meter);
    if (!meter.hidden) $('tutorialNeedle').style.left = `${(0.5 + result.meter / 2) * 100}%`;

    const need = Number(step.hold || 0);
    const hold = $('tutorialHold');
    hold.hidden = !need || ok;
    if (!hold.hidden) {
      const held = holdSince ? (performance.now() - holdSince) / 1000 : 0;
      $('tutorialHoldFill').style.width = `${Math.min(100, held / need * 100)}%`;
    }

    $('tutorialDetail').textContent = blocked || result.detail || '';
    $('tutorialDone').hidden = !ok;

    const act = blocked || ok ? null : step.action?.(state);
    const actBtn = $('tutorialActionBtn');
    actBtn.hidden = !act;
    if (act) {
      actBtn.textContent = act.label;
      actBtn.dataset.run = act.run;
    }
    const last = index === STEPS.length - 1;
    const next = $('tutorialNextBtn');
    next.textContent = last ? '学完了' : '下一步';
    next.disabled = !(ok || done.has(step.id) || skipped.has(step.id));
    $('tutorialSkipBtn').hidden = ok || done.has(step.id);
    $('tutorialSkipBtn').textContent = blocked ? '跳过这一步' : '先跳过，以后再练';
  }

  function renderFinish() {
    sceneNow = 'done';
    const count = STEPS.filter(step => done.has(step.id)).length;
    $('tutorialArt').innerHTML =
      `<svg class="tut-svg" viewBox="0 0 320 168" aria-hidden="true">${SCENES.done}</svg>`;
    $('tutorialKicker').textContent = '教学结束';
    $('tutorialGoal').textContent = count === STEPS.length ? '这几步都做到了' : `做到了 ${count} / ${STEPS.length} 步`;
    fillHow([
      '随时能从页面顶部的「新手教学」再打开，想练哪一步点哪一步。',
      '装驱动、手机当摄像头、连不上怎么办，在《新手指南》里——那些是打开这个页面之前的事。',
      '玩起来之后记住一件事：F9 立刻切断所有输出。',
    ]);
    $('tutorialCondition').textContent = '—';
    for (const id of ['tutorialTargets', 'tutorialMeter', 'tutorialHold', 'tutorialActionBtn', 'tutorialSkipBtn', 'tutorialDone']) $(id).hidden = true;
    $('tutorialDetail').textContent = '';
    $('tutorialNextBtn').textContent = '关闭';
    $('tutorialNextBtn').disabled = false;
  }

  function go(i) {
    clearTimeout(advance);
    advance = 0;
    index = Math.max(0, Math.min(STEPS.length, i));
    reached = Math.max(reached, Math.min(STEPS.length - 1, index));
    holdSince = 0;
    doneAt = 0;
    sceneNow = '';
    // 重进一步就重来一次：上一轮打中的目标不能替这一轮算数。
    if (index < STEPS.length) memos[STEPS[index].id] = {};
    save();
    renderRail();
    if (index >= STEPS.length) { renderFinish(); return; }
    const state = lastState || actions.state?.();
    if (state) update(state);
  }

  function update(state) {
    lastState = state;
    if (!opened || index >= STEPS.length) return;

    const step = STEPS[index];
    const memo = memos[step.id] || (memos[step.id] = {});
    renderStage(state);

    const blocked = step.skipWhen ? step.skipWhen(state) : null;
    const result = blocked ? {ok: false, detail: blocked} : step.check(state, memo);

    // 做到了就不再往回判：一次抖动不该把刚亮起来的判定又灭掉。
    if (doneAt) { renderFoot(state, result, true, null); return; }

    const need = Number(step.hold || 0);
    const now = performance.now();
    if (result.ok) { if (!holdSince) holdSince = now; } else holdSince = 0;
    const ok = result.ok && (!need || (now - holdSince) / 1000 >= need);

    if (ok) {
      doneAt = now;
      done.add(step.id);
      skipped.delete(step.id);
      reached = Math.max(reached, Math.min(STEPS.length - 1, index + 1));
      save();
      renderRail();
      if (index < STEPS.length - 1) advance = setTimeout(() => go(index + 1), 1600);
    }
    renderFoot(state, result, ok, blocked);
  }

  function open(source) {
    returnFocus = source || null;
    opened = true;
    // 上次停在哪就从哪接着；已经学完了就从头再来，而不是直接弹结束页。
    if (index >= STEPS.length) index = 0;
    go(index);
    if (!root.open) root.showModal();
  }

  function close() {
    opened = false;
    clearTimeout(advance);
    advance = 0;
    save();
    if (root.open) root.close();
  }

  $('closeTutorialBtn').addEventListener('click', close);
  $('tutorialNextBtn').addEventListener('click', () => {
    if (index >= STEPS.length) { close(); return; }
    go(index + 1);
  });
  $('tutorialSkipBtn').addEventListener('click', () => {
    if (index >= STEPS.length) return;
    skipped.add(STEPS[index].id);
    save();
    go(index + 1);
  });
  $('tutorialActionBtn').addEventListener('click', event => {
    const run = event.currentTarget.dataset.run;
    if (run && typeof actions[run] === 'function') actions[run]();
  });
  root.addEventListener('cancel', event => { event.preventDefault(); close(); });
  root.addEventListener('close', () => { opened = false; returnFocus?.focus(); });

  return {
    open,
    close,
    update,
    isOpen: () => opened,
    // 从没打开过才自动弹一次。跳过、关掉、学完，都算看过了。
    seen: () => { try { return !!JSON.parse(localStorage.getItem(STORE) || '{}').seen; } catch { return true; } },
  };
}
