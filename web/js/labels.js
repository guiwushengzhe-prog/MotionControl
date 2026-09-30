// 按键、触发器、动作的名字和写法。圈上、卡片上、大字里都从这里取，说法才一致。
import {customPoses,poseLibrary} from './library.js';
import {kernelState} from './play.js';
import {voiceCatalog,voiceCommandNames,voiceCommandPhrases} from './voice.js';

// The browser is a display/configuration client. Pose inference, zones,
// action debouncing, head control and watchdog timing live in Python.
// MediaPipe Pose 33 canonical names and connections. The service emits
// normalized points in raw camera orientation; the display mirror below is
// applied to preview, skeleton and zones together, never to kernel input.
export const EDGES = [
  ['nose','left_eye_inner'],['left_eye_inner','left_eye'],['left_eye','left_eye_outer'],['left_eye_outer','left_ear'],
  ['nose','right_eye_inner'],['right_eye_inner','right_eye'],['right_eye','right_eye_outer'],['right_eye_outer','right_ear'],
  ['mouth_left','mouth_right'],['left_shoulder','right_shoulder'],['left_shoulder','left_elbow'],['left_elbow','left_wrist'],
  ['left_wrist','left_pinky'],['left_wrist','left_index'],['left_wrist','left_thumb'],['right_shoulder','right_elbow'],
  ['right_elbow','right_wrist'],['right_wrist','right_pinky'],['right_wrist','right_index'],['right_wrist','right_thumb'],
  ['left_shoulder','left_hip'],['right_shoulder','right_hip'],['left_hip','right_hip'],['left_hip','left_knee'],
  ['left_knee','left_ankle'],['left_ankle','left_heel'],['left_heel','left_foot_index'],['left_ankle','left_foot_index'],
  ['right_hip','right_knee'],['right_knee','right_ankle'],['right_ankle','right_heel'],['right_heel','right_foot_index'],
  ['right_ankle','right_foot_index'],
];

export const BODY_ZONES = {leftHand:{body:'左手',button:'X',parts:['leftHandUpper','leftHandLower']},rightHand:{body:'右手',button:'B',parts:['rightHandUpper','rightHandLower']},leftFoot:{body:'左脚',button:'LB'},rightFoot:{body:'右脚',button:'RB'},headJump:{body:'头顶'},lookGate:{label:'上下视角',body:'左手放这里',button:null,gate:true}};

// 圈上写的那个字以前是写死的，和真实映射悄悄对不上——头顶那块一直显示 'A'。
// 后来改成读配置，结果错到了另一边：配置里没 zone.headJump 这一条时它写「未映射」，
// 可内核有一层内置兜底，它照样按 A。于是界面说没绑、游戏里却有反应。
//
// 现在读内核算好的 effective_bindings（见 bindingsForDisplay）——真会按下去的那份。
export function actionKeyText(action){
  if(!action)return null;
  const type=String(action.type||''),target=Array.isArray(action.target)?action.target.map(item=>String(item||'').toUpperCase()).filter(Boolean).join('+'):String(action.target||'').toUpperCase();
  if(!type||!target)return null;
  // 宏的编号对人没有意义，圈上和卡片上要写它的名字。
  if(type==='macro')return macroName(action.target);
  if(type==='voice_release')return `松开「${voiceCommandPhrases(action.target).join('、')}」`;
  if(type==='keyboard')return target;
  if(type==='system')return SYSTEM_TARGET_NAMES.get(target)||target;
  if(type==='mouse_button')return({LEFT:'左键',RIGHT:'右键',MIDDLE:'中键',X1:'侧键1',X2:'侧键2'}[target]||target);
  if(type==='mouse_wheel')return target.includes('UP')?'滚轮↑':target.includes('DOWN')?'滚轮↓':target;
  if(type==='gamepad_axis')return target.endsWith('UP')?'摇杆↑':target.endsWith('DOWN')?'摇杆↓':target.endsWith('LEFT')?'摇杆←':target.endsWith('RIGHT')?'摇杆→':target;
  return target;
}

// 语音按住的键要单独说出来。它和姿势按住不是一回事：姿势按住是看得见的——手还
// 交叉着、腿还抬着，人自己知道；语音按住是隐形的，三十秒前说了一句「保持左肩键」，
// 之后忘了，游戏开始不对劲，人只会以为是误触或者软件坏了，想不到去说「松开」。
export function voiceLatchText(status){
  const list=status?.voice_latches||[];
  if(!list.length)return null;
  const keys=list.map((item)=>actionKeyText(item)||item.target).filter(Boolean);
  return keys.length?`语音按住 ${keys.join('、')}`:null;
}

// 界面上显示"按的是哪个键"时，读的必须是内核算好的那份。
//
// control_bindings 是配置，而区域和动作还有一层内置兜底：配置里没有
// zone.headJump 时它照样按 A。直接读配置会写成「未映射」，而人在游戏里
// 明明被按了一个键——这种"界面说没绑、实际有反应"最难查，两边都不报错。
export function bindingsForDisplay(){
  return kernelState?.effective_bindings||kernelState?.control_bindings||{};
}

// 一个触发器现在绑的是什么。圈上、姿势卡片上、开始页的大字里都用它，写法才一致。
export function triggerKeyLabel(triggerKey){
  const binding=bindingsForDisplay()[triggerKey];
  const text=binding&&!binding.disabled?actionKeyText(binding.action):null;
  return text||'未映射';
}

// 绑没绑键。没绑的做了也不按任何键，所以除了开始页的触发显示，哪里都不显示它触发了——
// 亮一下只会让人以为它起作用了。
export function triggerMapped(triggerKey){return triggerKeyLabel(triggerKey)!=='未映射'}

// 手部一块圈里有上下两个绑定，所以它的标注天然是两个键，写成 "Y / X"。
export function zoneKeyLabel(id,def){
  const all=bindingsForDisplay();
  // 内核按合并后的 zone.leftHand 这些分发，effective_bindings 里也是它；以前只查上下两个
  // 旧编号，绑好了的手区在画面上照样写「未映射」。旧编号只在内核没报合并编号时兜底。
  const ids=all['zone.'+id]?[id]:(def.parts||[id]);
  const texts=ids.map((one)=>{const b=all['zone.'+one];return b&&!b.disabled?actionKeyText(b.action):null;}).filter(Boolean);
  return texts.length?texts.join(' / '):'未映射';
}

export const BASE_PROFILE_TRIGGERS=[
  {key:'zone.leftHand',group:'zones',id:'leftHand',name:'左手区'},
  {key:'zone.rightHand',group:'zones',id:'rightHand',name:'右手区'},
  {key:'zone.leftFoot',group:'zones',id:'leftFoot',name:'左脚区'},
  {key:'zone.rightFoot',group:'zones',id:'rightFoot',name:'右脚区'},
  {key:'zone.headJump',group:'zones',id:'headJump',name:'头顶区'},
  {key:'motion.march',group:'motions',id:'march',name:'原地踏步'},
  {key:'motion.calf_back',group:'motions',id:'calf_back',name:'小腿向后抬起'},
];

// 本机的动作库：自带的原地踏步、小腿向后抬起，加上从官方动作库下载的。下载的那些
// 也是能绑键的触发器，由 profileTriggers 并进来；没下载的在映射表里没有这一行。

export let poseLibraryNames={cloud:{}};

export const MOTION_CONFLICT_GROUPS=[
  {ids:['jumping_jack','hands_up'],label:'开合跳与双手举过头'},
];

export const MOTION_CONFLICT_NAMES={march:'原地踏步',calf_back:'小腿向后抬起',squat:'下蹲',hands_up:'双手举过头',jumping_jack:'开合跳',side_step_jack:'侧步开合'};

export const ACTION_TYPE_LABELS={keyboard:'键盘',mouse_button:'鼠标按键',mouse_wheel:'鼠标滚轮',gamepad:'手柄按键',gamepad_trigger:'手柄扳机',gamepad_axis:'左摇杆',macro:'键盘宏',voice_release:'松开口令按住的键',system:'系统功能'};

// 输出类型下拉分三组，一眼找到在哪一类。
export const ACTION_TYPE_GROUPS=[['键盘鼠标',['keyboard','mouse_button','mouse_wheel']],['手柄',['gamepad','gamepad_trigger','gamepad_axis']],['其他',['macro','voice_release','system']]];

// 宏库。每一处映射的下拉都从这里取，所以只在增删改之后刷一次，不跟着状态轮询走。
export const macroLibrary={items:[],limits:null};

export function macroById(id){return macroLibrary.items.find(item=>item.id===String(id||'').toLowerCase())||null}

function macroName(id){const found=macroById(id);return found?found.name:'宏已丢失'}

export const TARGET_LABELS={LEFT:'左键',RIGHT:'右键',MIDDLE:'中键',X1:'侧键 1',X2:'侧键 2',SCROLL_UP:'向上滚',SCROLL_DOWN:'向下滚',LT:'LT',RT:'RT',L3:'L3',R3:'R3',DPAD_UP:'十字键上',DPAD_DOWN:'十字键下',DPAD_LEFT:'十字键左',DPAD_RIGHT:'十字键右',START:'Start',BACK:'Back',LS_UP:'左摇杆上',LS_DOWN:'左摇杆下',LS_LEFT:'左摇杆左',LS_RIGHT:'左摇杆右'};

// The dispatcher rejects anything outside this set, so offer the list instead
// of a free text field whose typos can only surface as a silent no-op in game.
export const GAMEPAD_STICK_TARGETS=['LS_UP','LS_DOWN','LS_LEFT','LS_RIGHT'];

export const GAMEPAD_TRIGGER_TARGETS=['LT','RT'];

// 映射表「系统功能」能选的，按这个顺序列。电脑那边 profile_schema.BINDING_SYSTEM_TARGETS
// 是准，这里只管名字和顺序；那边没有的不列。
export const BINDING_SYSTEM_TARGETS=[['ZONES.MOVE_HERE','区域挪到我这里'],['ZONES.FREEZE_TOGGLE','定住区域 / 恢复跟随'],['ZONES.FREEZE','定住区域'],['ZONES.FOLLOW','区域恢复跟随'],['HEAD.CENTER','视角回正'],['OUTPUT.TOGGLE','开始 / 停止输出'],['OUTPUT.START','开始输出'],['OUTPUT.STOP','停止输出']];

export const VOICE_SYSTEM_TARGETS=[['EMERGENCY_STOP','紧急停止'],['OUTPUT.START','开始输出'],['OUTPUT.STOP','停止输出'],['OUTPUT.TOGGLE','开始 / 停止输出'],['HEAD.CENTER','视角回正'],['HEAD_CALIBRATION_START','开始校准'],['ZONES.MOVE_HERE','区域挪到我这里'],['ZONES.FREEZE','定住区域'],['ZONES.FOLLOW','区域恢复跟随'],['ZONES.FREEZE_TOGGLE','定住区域 / 恢复跟随'],['POSE.RECORD','录一个新姿势'],['POSE.ADD_FRAME','给刚录的动作再加一个姿势'],['POSE.CANCEL','取消录制倒计时']];

export const SYSTEM_TARGET_NAMES=new Map([...VOICE_SYSTEM_TARGETS,...BINDING_SYSTEM_TARGETS,['HEAD.CALIBRATE','开始校准']]);

export function profileTriggers(){
  // The stable slots are the one editable voice group. Their command IDs stay
  // unchanged for older phones while the spoken phrase lives in the profile.
  const voiceTriggers=voiceCatalog
    .filter(item=>!item.system_fixed&&String(item.id||'').startsWith('game.profile_slot_'))
    .map(item=>({
      key:`voice.${item.id}`,group:'voice',id:item.id,
      slot:String(item.id||'').startsWith('game.profile_slot_'),
      name:`口令 ${Number(String(item.id||'').replace('game.profile_slot_',''))}`,phrase:item.phrase,tapOnly:false,
      defaultBinding:item.default_action?{label:item.label,action:item.default_action}:null,
    }));
  // 用户自己录的姿势并进同一份触发器列表，于是它们自动出现在映射界面里，
  // 和内置姿势用同一套编辑、保存、按游戏区分的逻辑。不并进来的话，录完的姿势
  // 在界面上根本没地方绑键。
  const customPoseTriggers=customPoses.map(item=>({
    key:`pose.${item.id}`,group:'poses',id:item.id,
    name:`自定义 · ${item.name}`,tapOnly:false,
  }));
  const downloadedTriggers=poseLibrary.filter(item=>item.source==='cloud').map(item=>({
    key:item.trigger,group:item.group==='pose'?'poses':'motions',id:item.id,name:item.name,
  }));
  return [...BASE_PROFILE_TRIGGERS,...downloadedTriggers,...customPoseTriggers,...voiceTriggers];
}

export function targetLabel(action){
  if(!action)return '未映射';
  if(action.type==='macro')return macroName(action.target);
  if(action.type==='voice_release')return voiceCommandNames(action.target).join('、');
  const t=String(action.target||'').toUpperCase();
  if(action.type==='system')return SYSTEM_TARGET_NAMES.get(t)||t;
  if(action.type==='keyboard')return t;
  if(action.type==='gamepad'){const values=Array.isArray(action.target)?action.target: String(action.target||'').split('+');return values.map(v=>TARGET_LABELS[String(v).toUpperCase()]||String(v).toUpperCase()).join('+')}
  if(action.type==='gamepad_trigger')return t;
  return TARGET_LABELS[t]||t;
}

function bindingLabel(binding){
  if(!binding||binding.disabled)return '—';
  return String(binding.label||targetLabel(binding.action)||'—');
}
