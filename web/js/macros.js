// 设置 → 键盘宏。
import {api,configurationOperation,post} from './core.js';
import {TARGET_LABELS,macroLibrary} from './labels.js';
import {makeKeyCaptureInput,renderProfileBindingRows,saveProfileBindings} from './mapping.js';

/* --- 键盘宏 -------------------------------------------------------------
 * 一串有先后的按键，存成一条，起个名字，到处都能选。
 *
 * 宏库是全局的，不跟游戏走——要的就是"建一次，其他地方直接选用"。跟着游戏存的话，
 * 换个游戏就得重建一遍，那和多写几个键没区别。
 *
 * 校验全在电脑那边（motioncontrol_shared/macro_schema.py），这里不自己再判一遍：
 * 判两遍就是两套规则，早晚对不上。这边只负责把服务端说的话原样显示出来。
 */
const macroListEl = document.getElementById('macroList');

const macroStatusEl = document.getElementById('macroStatus');

const MACRO_STEP_TYPES = [
  ['keyboard', '键盘'], ['mouse_button', '鼠标'], ['mouse_wheel', '滚轮'],
  ['gamepad', '手柄'], ['gamepad_trigger', '手柄扳机'], ['gamepad_axis', '左摇杆'],
  ['macro', '另一条宏'],
];

const MACRO_STEP_TARGETS = {
  mouse_button: ['LEFT', 'RIGHT', 'MIDDLE', 'X1', 'X2'],
  mouse_wheel: ['SCROLL_UP', 'SCROLL_DOWN'],
  gamepad_trigger: ['LT', 'RT'],
  gamepad_axis: ['LS_UP', 'LS_DOWN', 'LS_LEFT', 'LS_RIGHT'],
  gamepad: ['A', 'B', 'X', 'Y', 'LB', 'RB', 'L3', 'R3', 'START', 'BACK',
            'DPAD_UP', 'DPAD_DOWN', 'DPAD_LEFT', 'DPAD_RIGHT'],
};

function macroSay(text, kind = '') {
  if (!macroStatusEl) return;
  macroStatusEl.textContent = text;
  macroStatusEl.className = kind === 'error' ? 'statusline error' : 'statusline';
}

export async function refreshMacros({ rebuild = true } = {}) {
  try {
    const data = await api('/api/macros');
    macroLibrary.items = data.macros || [];
    macroLibrary.limits = data.limits || null;
    if (data.error) macroSay(data.error, 'error');
  } catch (error) {
    macroSay(error.message, 'error');
    return false;
  }
  renderMacros();
  if (rebuild) await rebuildBindingRowsAfterMacroChange();
  return true;
}

/** 宏改了，上面每一行映射里的下拉、以及「跑一遍还是循环」那一格都要跟着变。
 *  重画之前先把还没存的编辑落盘，否则会把用户手上的草稿抹掉。 */
async function rebuildBindingRowsAfterMacroChange() {
  await saveProfileBindings();
  renderProfileBindingRows();
}

async function macroWrite(route, body) {
  return configurationOperation(async()=>{
  const data = await post('/api/macros/' + route, body);
  macroLibrary.items = data.macros || [];
  renderMacros();
  await rebuildBindingRowsAfterMacroChange();
  return data;
  });
}

async function addMacro() {
  const field = document.getElementById('macroName');
  try {
    await macroWrite('create', { name: field?.value || '' });
    if (field) field.value = '';
    macroSay('建好了。往下加步骤，然后到「本游戏 · 按键映射」里选它。');
  } catch (error) { macroSay(error.message, 'error'); }
}

async function updateMacro(id, changes) {
  try { await macroWrite('update', { id, ...changes }); macroSay('已保存'); }
  catch (error) {
    macroSay(error.message, 'error');
    // 服务端拒绝了就是什么都没改。界面上那个已经动过的控件必须退回去，否则它显示
    // 的是一个根本没存进去的值——下次打开又变回来，人会以为是软件丢了设置。
    await refreshMacros({ rebuild: false });
  }
}

async function removeMacro(macro) {
  if (!confirm(`删掉「${macro.name}」？用到它的按键映射会变成未映射。`)) return;
  try { await macroWrite('remove', { id: macro.id }); macroSay(`已删掉「${macro.name}」`); }
  catch (error) { macroSay(error.message, 'error'); }
}

function macroStepTargetControl(step, others) {
  const type = String(step.type || 'keyboard');
  if (type === 'macro') {
    const select = document.createElement('select');
    select.className = 'macro-step-target';
    if (!others.length) {
      const empty = document.createElement('option');
      empty.value = ''; empty.textContent = '没有别的宏可以放';
      select.appendChild(empty); select.disabled = true; return select;
    }
    for (const other of others) {
      const option = document.createElement('option');
      option.value = other.id; option.textContent = other.name; select.appendChild(option);
    }
    if (others.some(other => other.id === step.target)) select.value = step.target;
    return select;
  }
  if (type === 'keyboard') return makeKeyCaptureInput('macro-step-target', step.target || '');
  const select = document.createElement('select');
  select.className = 'macro-step-target';
  for (const key of MACRO_STEP_TARGETS[type] || []) {
    const option = document.createElement('option');
    option.value = key; option.textContent = TARGET_LABELS[key] || key; select.appendChild(option);
  }
  if (step.target && [...select.options].some(option => option.value === step.target)) select.value = step.target;
  return select;
}

function macroNumberField(label, value, low, high, unit) {
  const wrap = document.createElement('label');
  wrap.className = 'macro-step-ms';
  const input = document.createElement('input');
  input.type = 'number';
  input.min = String(low); input.max = String(high); input.step = '10';
  input.value = String(value);
  wrap.append(label, input, unit);
  return { wrap, input };
}

/** 把界面上那一排控件读回成一条宏的步骤表。整条一起提交，不做单步保存——服务端
 *  校验的是整条（转不转圈、总共多久都得看全貌），单步存等于把校验切碎。 */
function readMacroSteps(row) {
  const steps = [...row.querySelectorAll('.macro-step')].map(stepEl => {
    const step = {
      type: stepEl.querySelector('.macro-step-type').value,
      target: String(stepEl.querySelector('.macro-step-target')?.value || '').trim(),
    };
    const hold = stepEl.querySelector('.macro-step-hold');
    const gap = stepEl.querySelector('.macro-step-gap');
    if (hold) step.hold_ms = Number(hold.value);
    if (gap) step.gap_ms = Number(gap.value);
    if (stepEl.querySelector('.macro-step-with-prev')?.checked) step.with_prev = true;
    return step;
  });
  // 刚把某一步换成「跑另一条宏」时，它自己或下一步身上可能还留着「同时按」的勾。
  // 那个组合存不进去，勾也就跟着失效，与其让服务端拒绝整条，不如这里先去掉。
  steps.forEach((step, index) => {
    if (step.with_prev && (index === 0 || step.type === 'macro' || steps[index - 1].type === 'macro')) delete step.with_prev;
  });
  return steps;
}

function renderMacroStepBody(stepEl, step, others) {
  const body = stepEl.querySelector('.macro-step-body');
  body.replaceChildren();
  body.appendChild(macroStepTargetControl(step, others));
  const limits = macroLibrary.limits || {};
  const [holdLow, holdHigh] = limits.hold_ms || [10, 1000];
  const [gapLow, gapHigh] = limits.gap_ms || [0, 1000];
  // 引用另一条宏时没有「按住多久」——按多久由那条宏自己的步骤决定。滚轮也没有，
  // 它是一下就完的事。留一个不起作用的输入框只会让人以为它有用。
  if (step.type !== 'macro' && step.type !== 'mouse_wheel') {
    const hold = macroNumberField('按住 ', step.hold_ms ?? 60, holdLow, holdHigh, '毫秒');
    hold.input.classList.add('macro-step-hold');
    body.appendChild(hold.wrap);
  }
  const gap = macroNumberField('然后等 ', step.gap_ms ?? 40, gapLow, gapHigh, '毫秒');
  gap.input.classList.add('macro-step-gap');
  body.appendChild(gap.wrap);
}

function renderMacros() {
  if (!macroListEl) return;
  const hint = document.getElementById('macroHint');
  if (hint) hint.hidden = !macroLibrary.items.length;
  macroListEl.replaceChildren();

  for (const macro of macroLibrary.items) {
    const row = document.createElement('div');
    row.className = 'macro';
    row.dataset.id = macro.id;

    const name = document.createElement('input');
    name.className = 'macro-name';
    name.value = macro.name;
    name.maxLength = macroLibrary.limits?.name_chars || 20;
    name.addEventListener('change', () => updateMacro(macro.id, { name: name.value }));

    const repeat = document.createElement('label');
    repeat.className = 'macro-repeat';
    repeat.title = '关着就是触发一次跑一遍；打开就是动作保持着（或语音按住时）反复跑，松开才停';
    const repeatBox = document.createElement('input');
    repeatBox.type = 'checkbox';
    repeatBox.checked = !!macro.repeat;
    repeatBox.addEventListener('change', () => updateMacro(macro.id, { repeat: repeatBox.checked }));
    repeat.append(repeatBox, '按住时循环');

    const summary = document.createElement('span');
    summary.className = 'macro-summary';
    summary.textContent = macro.error
      ? macro.error
      : `${macro.expanded_steps} 步 · ${(macro.duration_ms / 1000).toFixed(2)} 秒`;
    if (macro.error) summary.classList.add('error');

    const head = document.createElement('div');
    head.className = 'macro-head';
    head.append(name, repeat, summary);

    // 自己不能引用自己，所以这一条不进可选列表。放进去只是让人建一条存不进去的宏。
    const others = macroLibrary.items.filter(item => item.id !== macro.id);
    const commit = () => updateMacro(macro.id, { steps: readMacroSteps(row) });

    const steps = document.createElement('div');
    steps.className = 'macro-steps';
    (macro.steps || []).forEach((step, index) => {
      const stepEl = document.createElement('div');
      stepEl.className = 'macro-step';

      const order = document.createElement('span');
      order.className = 'macro-step-order';
      order.textContent = String(index + 1);

      const type = document.createElement('select');
      type.className = 'macro-step-type';
      for (const [value, label] of MACRO_STEP_TYPES) {
        if (value === 'macro' && !others.length) continue;
        const option = document.createElement('option');
        option.value = value; option.textContent = label; type.appendChild(option);
      }
      type.value = step.type;
      // 换了类型，键位那一格里原来的值就没意义了（W 不是一个鼠标键）。重建再提交。
      type.addEventListener('change', () => {
        renderMacroStepBody(stepEl, { type: type.value }, others);
        commit();
      });

      const body = document.createElement('div');
      body.className = 'macro-step-body';

      const drop = document.createElement('button');
      drop.className = 'btn macro-step-drop';
      drop.type = 'button';
      drop.textContent = '×';
      drop.title = `删掉第 ${index + 1} 步`;
      drop.addEventListener('click', () => {
        const kept = readMacroSteps(row).filter((_, at) => at !== index);
        if (!kept.length) { macroSay('一条宏至少要有一步。整条不要了就点「删除」。', 'error'); return; }
        updateMacro(macro.id, { steps: kept });
      });

      stepEl.append(order, type, body);
      // 「和上一步同时按」：第一步前面没东西，「跑另一条宏」是一整串，都不给这个勾。
      const previous = index > 0 ? macro.steps[index - 1] : null;
      if (previous && previous.type !== 'macro' && step.type !== 'macro') {
        const together = document.createElement('label');
        together.className = 'macro-step-with';
        together.title = '勾上后这一步和上一步同一瞬间按下，比如按住 SHIFT 的同时点鼠标左键。'
          + '两步各按各的时长、各等各的间隔，都结束了才走下一步。';
        const togetherBox = document.createElement('input');
        togetherBox.type = 'checkbox';
        togetherBox.className = 'macro-step-with-prev';
        togetherBox.checked = !!step.with_prev;
        together.append(togetherBox, '和上一步同时按');
        stepEl.appendChild(together);
        if (step.with_prev) stepEl.classList.add('with-prev');
      }
      stepEl.appendChild(drop);
      renderMacroStepBody(stepEl, step, others);
      steps.appendChild(stepEl);
    });

    steps.addEventListener('change', event => {
      // 类型那一格自己会提交，这里只管键位和毫秒数，免得同一次改动提交两遍。
      if (!event.target.classList.contains('macro-step-type')) commit();
    });

    const addStep = document.createElement('button');
    addStep.className = 'btn';
    addStep.type = 'button';
    addStep.textContent = '再加一步';
    addStep.addEventListener('click', () =>
      updateMacro(macro.id, { steps: [...readMacroSteps(row), { type: 'keyboard', target: 'SPACE' }] }));

    const remove = document.createElement('button');
    remove.className = 'btn';
    remove.textContent = '删除';
    remove.addEventListener('click', () => removeMacro(macro));

    const tools = document.createElement('div');
    tools.className = 'macro-tools';
    tools.append(addStep, remove);

    row.append(head, steps, tools);
    macroListEl.appendChild(row);
  }
}

document.getElementById('macroAddBtn')?.addEventListener('click', addMacro);

document.getElementById('macroName')?.addEventListener('keydown', event => {
  if (event.key === 'Enter') addMacro();
});
