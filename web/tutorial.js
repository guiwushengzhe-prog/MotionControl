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
function schemeLabel(axis, value) {
  return (VIEW_CONTROL_CONTENT[axis].find(item => item.value === value) || {}).label || value;
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

// 推到自己那个方案满量程的六成算到位，动了但不到一成半就提示「再大一点」。
// 不能写死成读数 35%：头控的读数上限就是灵敏度（默认 58），调到 35 以下的人永远到
// 不了；握拳的输出又是另一套量纲。所以 app.js 先把两边都折算成 -1…1 再交过来。
const LEVEL = 0.6;
const MORE = 0.15;
const HAND = {left: '左手', right: '右手'};

// 头控左右两种方案，动作完全不同；读数的正负号程序里是「右为正」。
// 换边要先回正：程序里从一边直接甩到另一边，读数会先归零。
// 每一句都把结果带上（→ 视角左转）：只说「头往左肩歪」的话，人会以为还在配合校准，
// 意识不到歪头本身就是在转视角。
const HEAD_MOVES = {
  roll_tilt: {left: '头往左肩歪 → 视角左转', right: '头往右肩歪 → 视角右转', back: side => `回正，再往${side}肩歪 → 视角${side}转`,
    hint: '脸还朝着屏幕，歪住别动', more: '再歪大一点', done: '✓ 歪头就能转视角'},
  head_turn: {left: '向左转头 → 视角左转', right: '向右转头 → 视角右转', back: side => `回正，再向${side}转头 → 视角${side}转`,
    hint: '转住别动', more: '再转大一点', done: '✓ 转头就能转视角'},
};

// 虚拟鼠标：真输出这时是关着的，人感觉不到「歪头 = 动鼠标」。所以在画面上放一个
// 光标，照真鼠标的规矩走——读数就是速度，歪住就一直走，回正就停，不会自己弹回中间。
// 它只是画出来的，不发任何输出；游戏控制已经打开的话真鼠标自己会动，就不再画它。
function virtualCursor(s, axis, level) {
  return s.outputEnabled ? null : {axis, level, within: '#viewer'};
}

// 握拳控制：握住的那一刻记下手的位置，之后输出是「离那个位置多远」，松开就归零。
// 所以两个方向都能在一次握拳里做完，不用先松开。
function fistGuide(s, memo, now, {hand, state, level, want, words}) {
  const who = HAND[hand] || '手';
  // 做到的那一刻手可能已经松开了，所以每条分支都带上做到时要说的那句。
  const doneSay = words.done;
  // 手张着的时候虚拟鼠标也摆在那儿（不动）：人先看见它，握拳之后它一动就明白了。
  const cursor = virtualCursor(s, words.axis, state === 'engaged' || state === 'moving' ? level : 0);
  if (state === 'lost' || state === 'disabled') return {target: '#viewer', say: `${who}举到画面里`, hint: '手腕和手肘都要拍到', doneSay, cursor};
  if (state !== 'engaged' && state !== 'moving') {
    memo.openSince = memo.openSince || now;
    return {target: '#viewer', say: `${who}握拳`, hint: now - memo.openSince > 5000 ? '握紧一点' : '', doneSay, cursor};
  }
  memo.openSince = 0;
  const toward = Number.isFinite(level) ? (want === words.negative ? -level : level) : 0;
  return {
    target: '#viewer', ready: true, say: `握着${words[want]} → ${words.effect[want]}`, doneSay, cursor,
    hint: toward >= MORE && toward < LEVEL ? '再移远一点' : '松开就停',
  };
}

// 左右各推到一次、上下各推到一次，判定是同一个：一边到位就记住，两边都到才算。
function latch(memo, ready, level, negative, positive) {
  if (ready && Number.isFinite(level)) {
    if (level <= -LEVEL) memo[negative] = true;
    if (level >= LEVEL) memo[positive] = true;
  }
  return !!(memo[negative] && memo[positive]);
}

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
          : {target: '#viewHorizontalSource', say: `「左右控制」选「${schemeLabel('horizontal', 'roll_tilt')}」`};
      }
      const before = connectGuide(s, memo, now) || standGuide(s, memo, now);
      if (before) return before;
      const want = memo.left ? 'right' : 'left';
      if (HAND[s.horizontal]) {
        return fistGuide(s, memo, now, {hand: s.horizontal, state: s.hHandState, level: s.hLevel, want,
          words: {axis: 'x', left: '往左移', right: '往右移', negative: 'left', effect: {left: '视角左转', right: '视角右转'}, done: '✓ 握拳就能转视角'}});
      }
      // 程序的校准只做一件事：记住你正视屏幕时的样子。所以这里只叫人看屏幕，
      // 左右歪头是校准完成之后的事。
      if (s.calibrating) return {target: '#calBtn', say: '正在校准：看着屏幕中心，别动'};
      if (!s.calibrated) {
        return {target: '#calBtn', say: '先校准：看向屏幕中心，点「站好并校准」', hint: s.calibrationNote || '别看摄像头；倒计时完之前别动'};
      }
      const move = HEAD_MOVES[s.horizontal] || HEAD_MOVES.roll_tilt;
      const other = want === 'left' ? 'right' : 'left';
      const toward = Number.isFinite(s.hLevel) ? (want === 'left' ? -s.hLevel : s.hLevel) : 0;
      return {
        target: ['#viewer', '#headStatus'], focus: '#headStatus', ready: true, cursor: virtualCursor(s, 'x', s.hLevel),
        say: memo[other] ? move.back(want === 'left' ? '左' : '右') : move[want], doneSay: move.done,
        hint: s.guardBlocked ? '身子别晃，只动头' : toward >= MORE && toward < LEVEL ? move.more : move.hint,
      };
    },
    check(s, memo, ready) {
      const ok = latch(memo, ready, s.hLevel, 'left', 'right');
      return {ok, targets: [{label: '← 左', hit: !!memo.left}, {label: '右 →', hit: !!memo.right}]};
    },
  },
  {
    id: 'updown',
    name: '上下转视角',
    doneSay: '✓ 上下也会了',
    guide(s, memo, now) {
      if (s.vertical === 'off') {
        return s.view !== 'devices'
          ? {target: NAV_DEVICES, say: '点「通用设置」', hint: '上下视角现在是关着的'}
          : {target: '#viewVerticalSource', say: `「上下控制」选「${schemeLabel('vertical', 'left')}」`};
      }
      const before = connectGuide(s, memo, now) || standGuide(s, memo, now);
      if (before) return before;
      const want = memo.up ? 'down' : 'up';
      if (HAND[s.vertical]) {
        return fistGuide(s, memo, now, {hand: s.vertical, state: s.vHandState, level: s.vLevel, want,
          words: {axis: 'y', up: '往上移', down: '往下移', negative: 'up', effect: {up: '视角往上', down: '视角往下'}, done: '✓ 握拳就能上下看'}});
      }
      // 原有的上下方案：左手放进绿框才开闸，闸开着的时候右手（或者头）管上下。
      if (!s.gateActive) return {target: '#viewer', mark: '.zone[data-zone="lookGate"]', say: '左手伸进绿框'};
      const byHead = s.legacySource === 'head';
      const toward = Number.isFinite(s.vLevel) ? (want === 'up' ? -s.vLevel : s.vLevel) : 0;
      return {
        target: '#viewer', ready: true, cursor: virtualCursor(s, 'y', s.vLevel),
        say: want === 'up' ? (byHead ? '抬头 → 视角往上' : '右手往上抬 → 视角往上') : (byHead ? '低头 → 视角往下' : '右手往下放 → 视角往下'),
        hint: toward >= MORE && toward < LEVEL ? '再大一点' : '左手留在绿框里',
      };
    },
    check(s, memo, ready) {
      const ok = latch(memo, ready, s.vLevel, 'up', 'down');
      return {ok, targets: [{label: '↑ 上', hit: !!memo.up}, {label: '↓ 下', hit: !!memo.down}]};
    },
  },
  {
    id: 'zone',
    name: '区域按键',
    doneSay: '✓ 圈亮了，键就按下了',
    guide(s, memo, now) {
      const before = connectGuide(s, memo, now) || standGuide(s, memo, now);
      if (before) return before;
      const shown = s.zones.filter(zone => zone.shown);
      if (!shown.length) return {target: '#viewer', say: '站好，等画面里出现圆圈'};
      // 先教手：最好够、最好看。挑一个真的绑了键的；一个都没绑，也照样教，但得说清楚。
      const pick = ['leftHand', 'rightHand', 'headJump', 'leftFoot', 'rightFoot']
        .map(id => shown.find(zone => zone.id === id && zone.key)).find(Boolean) || shown[0];
      const how = {leftHand: '左手伸进', rightHand: '右手伸进', headJump: '手举过头，伸进', leftFoot: '左脚踩进', rightFoot: '右脚踩进'}[pick.id] || '伸进';
      return {
        target: '#viewer', mark: `.zone[data-zone="${pick.id}"]`, ready: true,
        say: `${how}「${pick.key || pick.body}」圈`,
        // 没绑键的圈伸进去也不会亮（亮 = 按下了一个键），这一步只能跳过。
        hint: pick.key ? '' : '这个游戏方案没给圈绑键，伸进去不会亮；这步可以跳过',
      };
    },
    check: s => ({ok: s.zones.some(zone => zone.pressed)}),
  },
  {
    id: 'start',
    name: '开始游戏控制',
    doneSay: '✓ 游戏控制开了',
    guide(s, memo, now) {
      const before = connectGuide(s, memo, now);
      if (before) return before;
      // 手柄输出靠虚拟手柄驱动；没装的话按钮点下去也不会有手柄，网页又装不了驱动。
      if (s.driverMissing) {
        return {target: '#mainActionBtn', say: '先装虚拟手柄驱动', hint: '双击程序文件夹里的「安装虚拟手柄驱动.exe」；装不了就这步先跳过'};
      }
      return {target: '#mainActionBtn', ready: true, say: '点「开始游戏控制」', hint: '打开后动作就真的生效，下一步教你停'};
    },
    check: s => ({ok: s.outputEnabled}),
  },
  {
    id: 'stop',
    name: '紧急停止',
    doneSay: '✓ 全停了',
    guide(s, memo) {
      if (memo.stopsBefore === undefined) memo.stopsBefore = s.stops;
      return {target: '#stopBtn', ready: true, say: '按 F9', hint: '或者点「紧急停止」'};
    },
    // 数的是急停真的被触发了几次，而不是输出关没关：点「暂停游戏控制」也会关，但那不是急停。
    check: (s, memo) => ({ok: s.stops > (memo.stopsBefore ?? s.stops)}),
  },
];

const NAV_GAMES = 'nav [data-view="games"]';
const NAV_RANGE = 'nav [data-view="range"]';

// 基础之外的三课，不在第一遍里教：第一遍只管「能玩起来」。它们各自住在自己那一页，
// 基础学完之后下次打开时让人挑。挑的时候看到的不是功能名，而是它解决的那个问题——
// 人不会想「我要学动作测试」，只会想「我做了动作怎么没反应」。
// 语音用不了时，人能做的只有这几件。发布包里带着语音模型，所以「模型不在」基本就是
// 安装包没解压完整、或者被杀毒软件拿走了——重新解压一遍能好；弄不好也不要紧，语音
// 不影响用身体玩，所以直说可以先跳过。中文路径读不了模型的问题程序自己会绕开，不用人管。
const VOICE_FIX = {
  model: {say: '重新解压一遍安装包', hint: '程序文件夹里少了语音模型。弄不好就点「换一个」，不影响用身体玩'},
  mic: {say: '插上麦克风', hint: '插了还不行：Windows 设置 → 隐私 → 麦克风，允许桌面应用使用'},
  silent: {say: '对着麦克风说句话', hint: '还是没反应：Windows 声音设置里换一个默认麦克风'},
  starting: {say: '语音准备中，等几秒', hint: ''},
};

// 量身：每一步叫人做什么。量的过程在电脑那边每帧跑，这里只看它量到哪一步了。
const FIT_SAY = {
  stand: {say: '站好，别动', hint: '就站在你平时玩的位置'},
  hands: {say: '两只手往两边抬起来，挥一挥', hint: '抬到平时伸手按键那么高'},
  leftFoot: {say: '左脚往左边伸出去，再收回来', hint: '像平时踢那一下'},
  rightFoot: {say: '右脚往右边伸出去，再收回来', hint: '像平时踢那一下'},
  jump: {say: '原地往上跳一下', hint: '和平时玩的时候跳得一样高'},
  leftOpen: {say: '左手张开，举在身前', hint: '手指伸直，停一下'},
  leftFist: {say: '左手握紧', hint: '握住停一下'},
  rightOpen: {say: '右手张开，举在身前', hint: '手指伸直，停一下'},
  rightFist: {say: '右手握紧', hint: '握住停一下'},
};
const FIT_ISSUE = {
  not_visible: '看不到你了：头、肩膀和胯都要进画面',
  feet_hidden: '脚拍不到：往后退一点，或者把摄像头放低',
  hand_hidden: '看不到这只手：举到画面里，手指也要拍到',
};
// 进度格子：张开和握紧算一格。
const FIT_CHIP = {stand: '站好', hands: '挥手', leftFoot: '左脚', rightFoot: '右脚', jump: '跳', leftFist: '左手握拳', rightFist: '右手握拳'};
const FIT_NAME = {leftHand: '左手区', rightHand: '右手区', leftFoot: '左脚区', rightFoot: '右脚区', headJump: '跳', leftGrip: '左手握拳', rightGrip: '右手握拳'};

function fitDoneSay(fit = {}) {
  const measured = fit.measured || [];
  const skipped = (fit.skipped || []).map(id => FIT_NAME[id] || id);
  if (!measured.length) return '一项都没量到，还是原来的';
  if (skipped.length) return `✓ 量好了 · ${skipped.join('、')}没量到，用原来的`;
  // 「只量握拳」那一轮没动圈，不能说圈放好了。
  return measured.some(id => !id.endsWith('Grip')) ? '✓ 量好了，圈按你的身体放好了' : '✓ 握拳量好了';
}

const EXTRAS = [
  {
    id: 'fit',
    name: '量身',
    problem: '动作圈不合适',
    doneSay: s => fitDoneSay(s.fit),
    guide(s, memo, now) {
      const fit = s.fit || {};
      // 点了「开始」之后电脑那边才有一轮在量；那之前看到的 done 是上一轮的，不算。
      if (fit.active) memo.started = true;
      if (memo.started && fit.state === 'done') return {target: '#viewer', ready: true, say: '✓ 量好了'};
      if (memo.started && fit.state === 'preparing') {
        const left = Math.max(1, Math.ceil(Number(fit.remaining_s) || 0));
        return {
          target: '#viewer', ready: true,
          say: '回到镜头前站好，倒计时结束才开始采集',
          hint: `准备倒计时：还剩 ${left} 秒`,
        };
      }
      if (memo.started && fit.active) {
        const words = FIT_SAY[fit.phase] || FIT_SAY.stand;
        return {
          target: '#viewer', ready: true, say: words.say, hint: FIT_ISSUE[fit.issue] || words.hint,
          // 站好那一步跳不过去：后面全靠它当基准。
          choice: fit.phase === 'stand' ? null : {label: '这一项跳过', run: 'zoneFitSkip'},
        };
      }
      memo.started = false;
      const before = connectGuide(s, memo, now) || standGuide(s, memo, now);
      if (before) return before;
      const gripOnly = memo.gripOnly && s.fistHands.length;
      const prepareHint = '点下后倒数 3 秒，点完回到镜头前；倒数结束才开始量身';
      return {
        target: '#viewer', say: '站到你平时玩的位置',
        hint: gripOnly ? `只量握拳：张开一次、握紧一次。${prepareHint}`
          : `${s.zonesFrozen ? '区域现在定住了；量完的大小要点「恢复跟随」才用得上，握拳照样生效' : '全身进画面，脚也要拍到。接下来挥手、伸脚、跳一下'}。${prepareHint}`,
        choice: {label: '点这里，3 秒后开始', run: gripOnly ? 'zoneFitGripOnly' : 'zoneFitStart'},
      };
    },
    check(s, memo) {
      const fit = s.fit || {};
      const phases = fit.phases || [];
      const at = fit.state === 'done' ? phases.length : Number(fit.phase_index) || 0;
      const targets = memo.started
        ? phases.map((id, i) => ({id, i})).filter(({id}) => FIT_CHIP[id]).map(({id, i}) => ({label: FIT_CHIP[id], hit: i < at}))
        : [];
      return {ok: !!memo.started && fit.state === 'done', targets};
    },
    // 量到一半离开这一课：电脑那边那一轮也不量了，什么都不改。
    leave: (s, memo) => { if (memo.started && s.fit?.active) return 'zoneFitCancel'; return null; },
  },
  {
    id: 'range',
    name: '动作测试',
    problem: '做了动作，游戏没反应',
    doneSay: '✓ 没反应时，先来这里试',
    guide(s, memo, now) {
      if (memo.since === undefined) memo.since = s.kernelNow;
      const last = s.lastTrigger;
      if (!memo.hit && last && Number.isFinite(memo.since) && last.at > memo.since) memo.hit = last;
      const hit = memo.hit;
      if (hit && s.view === 'games') {
        // 点靶子会跳到「本游戏」里它那一行；亮着那一行，人就知道键在哪改。
        return {target: `.binding-row[data-trigger="${hit.key}"]`, ready: true, say: '在这里改它按的键'};
      }
      if (s.view !== 'range') return {target: NAV_RANGE, say: '点「动作测试」'};
      if (!hit) {
        const before = connectGuide(s, memo, now);
        if (before) return before;
        if (!s.posed) return {target: '#rangeHit', say: '站到镜头前'};
        return {target: '#rangeHit', say: '做个动作试试', hint: '伸手进圈、踏步、下蹲都行'};
      }
      // 下面那一排格子里有它就指它；没有（比如一句没绑键的口令），就让人随便点一个——
      // 这一课要教的是「从这里点过去就能改键」，改哪一个不要紧。
      const tile = `.range-target[data-trigger="${hit.key}"]`;
      const here = !!document.querySelector(tile);
      return {
        target: here ? tile : '#rangeTargets',
        say: !here ? '点下面任意一个，去改它的键' : hit.keyText ? `点「${hit.name}」，去改它的键` : `「${hit.name}」没绑键，点它去绑`,
        hint: `刚认出：${hit.name} → ${hit.keyText || '未映射'}`,
      };
    },
    check: (s, memo) => ({ok: !!memo.hit && s.view === 'games'}),
  },
  {
    id: 'game',
    name: '选游戏',
    problem: '按键和我的游戏对不上',
    doneSay: s => ({custom: '✓ 加好了 · 按键在下面自己绑', verified: '✓ 换好了'}[s.gameKind] || '✓ 换好了 · 这份配置没人试过'),
    guide(s, memo) {
      if (memo.appliesBefore === undefined) memo.appliesBefore = s.profileApplies;
      if (s.view !== 'games') return {target: NAV_GAMES, say: '点「本游戏」', hint: `现在是「${s.gameName}」`};
      // 在这一页上怎么换成的都算——搜到选中也好，自己加一个也好——所以这几个状态都是 ready。
      if (!s.searchedFor) return {target: ['#profileSearch', '#profileSearchBtn'], ready: true, say: '搜你要玩的游戏', hint: `现在是「${s.gameName}」`};
      if (!s.catalogCount) return {target: '#customGameBox', ready: true, say: '搜不到就自己加一个', hint: '展开，填游戏名，点「添加」'};
      return {target: ['#profileSelect', '#profileApplyBtn'], ready: true, say: '选中它，点「使用这个游戏」', hint: `找到 ${s.catalogCount} 款`};
    },
    check: (s, memo) => ({ok: s.profileApplies > (memo.appliesBefore ?? s.profileApplies)}),
  },
  {
    id: 'voice',
    name: '语音口令',
    problem: '手忙不过来，想用嘴说',
    doneSay: '✓ 能说的都在这张表里',
    guide(s, memo) {
      if (memo.heardBefore === undefined) memo.heardBefore = s.voiceHeard;
      if (s.voiceHeard !== memo.heardBefore) memo.heard = true;
      if (s.view !== 'play') return {target: NAV_PLAY, say: '回到「开始」页'};
      if (!s.voiceReady) return {target: ['#voicePill', '#voiceStatus'], ...(VOICE_FIX[s.voiceIssue] || VOICE_FIX.starting)};
      // 先教急停那一句：它无害，而且是最该会的——手占着按不到 F9 的时候就靠它。
      if (!memo.heard) return {target: ['#voicePill', '#voiceStatus'], say: `说「${s.stopPhrase}」`, hint: '一口气说完'};
      return {target: '#voiceCommandsBtn', ready: true, say: '点「查看语音指令」', hint: '还能说哪些，都在这'};
    },
    check: (s, memo) => ({ok: !!memo.heard && s.voiceListOpen}),
  },
];

export function createTutorial(actions = {}) {
  const root = $('tour');
  if (!root) return {open() {}, close() {}, autoOpen() {}};
  const spot = $('tourSpot'), card = $('tourCard'), dock = document.querySelector('.command-dock');
  const blocks = [...root.querySelectorAll('.tour-block')];

  // mode：main 基础那一串；extra 其中一课；menu 让人挑；finish 基础学完。
  // only：这一课是从别处（设置页）直接点进来的，做完或不做了就关，不回菜单。
  let mode = 'main', index = 0, extra = null, only = false, memos = {}, done = new Set(), skipped = new Set(), ran = new Set();
  let seen = false, offered = false;
  let holdSince = 0, doneAt = 0, advance = 0, timer = 0, frame = 0, moveTimer = 0, nudgeTimer = 0;
  let active = false, returnFocus = null, targetKey = '', targetEls = [], focusEl = null, layoutKey = '', chipsKey = '', choice = null;
  let markEl = null, menuKey = '';
  // 虚拟鼠标：cursorWant 是这一刻 guide 要的（哪个方向、读数多少、在哪块画面里走），
  // cursorPos 是它在那块画面里的位置（0…1），cursorAt 是上一帧的时间。
  const cursorEl = $('tourCursor');
  let cursorWant = null, cursorPos = {x: .5, y: .5}, cursorAt = 0;

  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || '{}');
    done = new Set(Array.isArray(saved.done) ? saved.done : []);
    skipped = new Set(Array.isArray(saved.skipped) ? saved.skipped : []);
    seen = !!saved.seen;
    offered = !!saved.offered;
  } catch { /* 存不上就每次从头开始，不值得为此挡住教学 */ }

  function save() {
    try {
      localStorage.setItem(STORE, JSON.stringify({done: [...done], skipped: [...skipped], seen: 1, offered}));
    } catch { /* 隐私模式下写不了，忽略 */ }
  }

  // 基础里第一步还没做、也没跳过的；-1 表示基础已经走完了。
  const firstLeft = () => STEPS.findIndex(step => !done.has(step.id) && !skipped.has(step.id));

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

  // 亮框里如果还要指明是哪一个（画面里那几个区域圈），就给那个真元素本身加个标记，
  // 不另画箭头：圈跟着人走，箭头跟不上。
  function setMark(selector) {
    const el = selector ? document.querySelector(selector) : null;
    if (el === markEl) return;
    markEl?.classList.remove('tour-mark');
    markEl = el;
    markEl?.classList.add('tour-mark');
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
  // 读数是速度：满量程时一秒走过半块画面。每帧推一次，所以 250ms 才来一次的读数也能走得顺。
  const CURSOR_SPEED = 0.5;
  function moveCursor() {
    const want = active ? cursorWant : null;
    const box = want && document.querySelector(want.within)?.getBoundingClientRect();
    if (!box || !box.width) { cursorEl.hidden = true; cursorAt = 0; return; }
    const now = performance.now();
    const dt = cursorAt ? Math.min(0.05, (now - cursorAt) / 1000) : 0;
    cursorAt = now;
    const speed = (Number.isFinite(want.level) ? want.level : 0) * CURSOR_SPEED * dt;
    if (want.axis === 'x') cursorPos.x = clamp(cursorPos.x + speed, 0.04, 0.96);
    else cursorPos.y = clamp(cursorPos.y + speed, 0.06, 0.94);
    cursorEl.hidden = false;
    cursorEl.style.transform = `translate(${Math.round(box.left + cursorPos.x * box.width)}px, ${Math.round(box.top + cursorPos.y * box.height)}px)`;
  }

  function layout() {
    frame = requestAnimationFrame(layout);
    moveCursor();
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

  // 卡片的几种「状态」只差在哪几块露出来；集中在这里改，免得各处各藏各的。
  function show({say, sayDone = false, hint = '', targets = [], hold = null, skip = '', choiceLabel = '', next = '', menu = false, head}) {
    setText('tourStep', head);
    setText('tourSay', say);
    $('tourSay').classList.toggle('done', sayDone);
    setText('tourHint', hint);
    $('tourHint').hidden = !hint;
    renderChips(targets);
    $('tourHold').hidden = hold === null;
    if (hold !== null) $('tourHoldFill').style.width = `${Math.min(100, hold * 100)}%`;
    $('tourChoices').hidden = !menu;
    $('tourSkipBtn').hidden = !skip;
    if (skip) setText('tourSkipBtn', skip);
    $('tourChoiceBtn').hidden = !choiceLabel;
    if (choiceLabel) setText('tourChoiceBtn', choiceLabel);
    $('tourNextBtn').hidden = !next;
    if (next) setText('tourNextBtn', next);
    setText('tourCloseBtn', mode === 'main' || mode === 'extra' ? '跳过教学' : '关闭');
    spot.classList.toggle('done', sayDone);
  }

  function renderStep(step, guide, result, ok, state) {
    const need = Number(step.hold || 0);
    const doneText = guide.doneSay || (typeof step.doneSay === 'function' ? step.doneSay(state) : step.doneSay);
    choice = ok ? null : guide.choice || null;
    show({
      head: mode === 'main' ? `新手教学 · ${index + 1}/${STEPS.length} · ${step.name}` : `新手教学 · ${step.name}`,
      say: ok ? doneText : guide.say,
      sayDone: ok,
      hint: ok ? '' : guide.hint || '',
      // 目标格子只在真正开始做动作时才出现：校准时就摆出「← 左 右 →」，人会以为现在就该歪头。
      targets: guide.ready || ok ? result.targets || [] : [],
      hold: need > 0 && guide.ready && !ok ? (holdSince ? (performance.now() - holdSince) / 1000 / need : 0) : null,
      skip: ok ? '' : mode === 'main' ? '这步跳过' : only ? '不做了' : '换一个',
      choiceLabel: choice?.label || '',
    });
  }

  function renderMenu() {
    setTarget(null);
    setMark(null);
    const left = firstLeft();
    const basics = left === -1 ? '从头学怎么玩 ✓' : done.size || skipped.size ? `从头学怎么玩 · 接着第 ${left + 1} 步` : '从头学怎么玩';
    const items = [{id: 'main', label: basics}, ...EXTRAS.map(lesson => ({id: lesson.id, label: lesson.problem + (done.has(lesson.id) ? ' ✓' : '')}))];
    const key = JSON.stringify(items);
    if (key !== menuKey) {
      menuKey = key;
      $('tourChoices').replaceChildren(...items.map(item => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'tour-option' + (item.label.endsWith('✓') ? ' done' : '');
        btn.textContent = item.label;
        btn.addEventListener('click', () => {
          if (item.id === 'main') go(left === -1 ? 0 : left);
          else startExtra(EXTRAS.find(lesson => lesson.id === item.id));
        });
        return btn;
      }));
    }
    show({head: '新手教学', say: '想学哪个？', menu: true});
  }

  function renderFinish() {
    setTarget(null);
    setMark(null);
    const count = STEPS.filter(step => done.has(step.id)).length;
    show({
      head: '新手教学',
      say: count === STEPS.length ? '✓ 学会了' : `学了 ${count} / ${STEPS.length} 步`,
      sayDone: count === STEPS.length,
      // 其余几课不在这里接着弹：刚学完一串，人要的是去玩。下次打开时再让他挑。
      hint: '下次打开，还能学别的',
      next: '好',
    });
  }

  function tick() {
    if (!active) return;
    if (mode === 'menu') { renderMenu(); return; }
    if (mode === 'finish') { renderFinish(); return; }
    const step = mode === 'main' ? STEPS[index] : extra;
    const state = actions.state?.();
    if (!step || !state) return;
    const memo = memos[step.id] || (memos[step.id] = {});
    const now = performance.now();
    const guide = step.guide(state, memo, now);
    // 需要主动问一下环境的（比如扫摄像头），每次打开教学只问一次。
    if (guide.run && !ran.has(guide.run)) {
      ran.add(guide.run);
      actions[guide.run]?.();
    }
    setTarget(guide.target, guide.focus);
    setMark(doneAt ? null : guide.mark);
    // 换了一个方向（左右 → 上下）就从画面中间重新开始，免得上一步停在边上。
    const cursor = doneAt ? null : guide.cursor || null;
    if (cursor && cursorWant?.axis !== cursor.axis) cursorPos = {x: .5, y: .5};
    cursorWant = cursor;
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
        advance = setTimeout(() => (mode === 'main' ? go(index + 1) : only ? close() : showMenu()), 1300);
      }
    }
    renderStep(step, guide, result, ok, state);
  }

  function reset() {
    // 离开一课时让它收拾一下（比如量身量到一半，电脑那边那一轮也得停）。
    if (mode === 'extra' && extra?.leave) {
      const run = extra.leave(actions.state?.() || {}, memos[extra.id] || {});
      if (run) actions[run]?.();
    }
    cursorWant = null;
    clearTimeout(advance);
    advance = 0;
    holdSince = 0;
    doneAt = 0;
  }

  function go(i) {
    reset();
    index = clamp(i, 0, STEPS.length);
    mode = index >= STEPS.length ? 'finish' : 'main';
    // 重进一步就重来一次：上一轮打中的目标不能替这一轮算数。
    if (mode === 'main') memos[STEPS[index].id] = {};
    save();
    tick();
  }

  function startExtra(lesson, memo = {}) {
    reset();
    mode = 'extra';
    extra = lesson;
    memos[lesson.id] = memo;
    tick();
  }

  function showMenu() {
    reset();
    mode = 'menu';
    tick();
  }

  function onKey(event) {
    // 校准那种正式弹窗开着时，Esc 是它的，不能顺手把教学也关了。
    if (event.key === 'Escape' && !document.querySelector('dialog[open]')) close();
  }

  // 顶上的「新手教学」：基础一步都没碰过的人直接开始；碰过的先让他挑——可能是回来
  // 接着学，也可能是遇到了某个具体问题。
  function open(source, {menu = true, lesson = null, memo = {}} = {}) {
    returnFocus = source || null;
    active = true;
    ran = new Set();
    root.hidden = false;
    targetKey = '';
    layoutKey = '';
    menuKey = '';
    const fresh = !done.size && !skipped.size;
    const direct = lesson && EXTRAS.find(item => item.id === lesson);
    only = !!direct;
    if (direct) startExtra(direct, memo);
    else if (menu && !fresh) showMenu();
    else go(Math.max(0, firstLeft()));
    clearInterval(timer);
    timer = setInterval(tick, TICK_MS);
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(layout);
    document.addEventListener('keydown', onKey);
  }

  // 打开页面时自己弹不弹：从没看过就直接带着做基础；基础走完之后的下一次打开，让人
  // 挑一次别的——只挑这一次，之后就只在按钮上等着，不天天弹。
  function autoOpen() {
    if (!seen) { seen = true; open(null, {menu: false}); return; }
    if (firstLeft() === -1 && !offered && EXTRAS.some(lesson => !done.has(lesson.id))) {
      offered = true;
      save();
      open(null);
    }
  }

  function close() {
    active = false;
    clearInterval(timer);
    cancelAnimationFrame(frame);
    reset();
    only = false;
    root.hidden = true;
    setMark(null);
    document.removeEventListener('keydown', onKey);
    save();
    returnFocus?.focus?.();
  }

  for (const block of blocks) block.addEventListener('click', nudge);
  // 「跳过教学」是给老玩家的：一下关掉，以后也不再自己弹出来。
  $('tourCloseBtn').addEventListener('click', close);
  $('tourNextBtn').addEventListener('click', close);
  $('tourSkipBtn').addEventListener('click', () => {
    if (mode === 'extra') { if (only) close(); else showMenu(); return; }
    if (mode !== 'main') return;
    skipped.add(STEPS[index].id);
    save();
    go(index + 1);
  });
  $('tourChoiceBtn').addEventListener('click', () => {
    const step = mode === 'main' ? STEPS[index] : mode === 'extra' ? extra : null;
    if (!choice || !step) return;
    // 「画面里不是我」：记下现在是哪个摄像头，回设备页换一个，换成别的序号并且
    // 开起来了才算换完。其余的选择都是让 app.js 做一件事（比如量身的开始、跳过）。
    if (choice.run === 'repick') {
      const memo = memos[step.id] || (memos[step.id] = {});
      const state = actions.state?.() || {};
      memo.repick = true;
      memo.repickFrom = state.cameraIndex;
      memo.lostSince = 0;
    } else {
      actions[choice.run]?.();
    }
    tick();
  });

  // 从别处直接打开某一课，比如设置页的「量身」。memo 是这一课开场要知道的事。
  function openLesson(id, source, memo = {}) {
    open(source, {lesson: id, memo});
  }

  return {open, close, autoOpen, openLesson};
}
