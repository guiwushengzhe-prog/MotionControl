import {VIEW_CONTROL_CONTENT} from './view-control-guide.js';
import {createTutorial} from './tutorial.js';

const $ = s => document.querySelector(s);
const canvas = $('#canvas');
const ctx = canvas.getContext('2d');
const viewer = $('#viewer');
const cameraPreview = $('#cameraPreview');

// The browser is a display/configuration client. Pose inference, zones,
// action debouncing, head control and watchdog timing live in Python.
// MediaPipe Pose 33 canonical names and connections. The service emits
// normalized points in raw camera orientation; the display mirror below is
// applied to preview, skeleton and zones together, never to kernel input.
const EDGES = [
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
const BODY_ZONES = {leftHand:{body:'左手',button:'X',parts:['leftHandUpper','leftHandLower']},rightHand:{body:'右手',button:'B',parts:['rightHandUpper','rightHandLower']},leftFoot:{body:'左脚',button:'LB'},rightFoot:{body:'右脚',button:'RB'},headJump:{body:'头顶'},lookGate:{label:'上下视角',body:'左手放这里',button:null,gate:true}};
// 圈上写的那个字以前是写死的，和真实映射悄悄对不上——头顶那块一直显示 'A'。
// 后来改成读配置，结果错到了另一边：配置里没 zone.headJump 这一条时它写「未映射」，
// 可内核有一层内置兜底，它照样按 A。于是界面说没绑、游戏里却有反应。
//
// 现在读内核算好的 effective_bindings（见 bindingsForDisplay）——真会按下去的那份。
function actionKeyText(action){
  if(!action)return null;
  const type=String(action.type||''),target=Array.isArray(action.target)?action.target.map(item=>String(item||'').toUpperCase()).filter(Boolean).join('+'):String(action.target||'').toUpperCase();
  if(!type||!target)return null;
  // 宏的编号对人没有意义，圈上和卡片上要写它的名字。
  if(type==='macro')return macroName(action.target);
  if(type==='voice_release')return `停「${voiceCommandPhrases(action.target).join('、')}」`;
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
function voiceLatchText(status){
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
function bindingsForDisplay(){
  return kernelState?.effective_bindings||kernelState?.control_bindings||{};
}
// 一个触发器现在绑的是什么。圈上、姿势卡片上、动作测试页里都用它，写法才一致。
function triggerKeyLabel(triggerKey){
  const binding=bindingsForDisplay()[triggerKey];
  const text=binding&&!binding.disabled?actionKeyText(binding.action):null;
  return text||'未映射';
}
// 绑没绑键。没绑的做了也不按任何键，所以除了「动作测试」页，哪里都不显示它触发了——
// 亮一下只会让人以为它起作用了。
function triggerMapped(triggerKey){return triggerKeyLabel(triggerKey)!=='未映射'}
// 跳到映射表里的那一行并高亮。组可能是折叠的，得先展开，否则滚过去是一片空。
function revealBindingRow(triggerKey){
  // 通用口令和内置口令不在本游戏的映射表里：前者在「通用设置」，后者改不了。
  if(triggerKey.startsWith('voice.shared.')){showView('devices');document.getElementById('personalVoicePanel')?.scrollIntoView({behavior:'smooth',block:'start'});return}
  if(triggerKey.startsWith('voice.')&&!triggerKey.startsWith('voice.game.profile_slot_')){notice('这是内置口令，不能改键');return}
  // 从「开始」页点过来的话，映射表所在的页签还藏着——藏着的东西滚不过去，
  // 也高亮不出来。先切过去再找。
  if(currentView!=='games')showView('games');
  // 没绑键的身体动作平时不在表里（见 shownBodyRows），点过来就是要绑它，现加一行。
  const row=document.querySelector(`.binding-row[data-trigger="${triggerKey}"]`)||addBodyRow(triggerKey);
  if(!row){notice('这个动作还没出现在映射表里，刷新一下页面再试');return}
  row.closest('details')?.setAttribute('open','');
  row.scrollIntoView({behavior:'smooth',block:'center'});
  row.classList.add('just-found');
  setTimeout(()=>row.classList.remove('just-found'),1600);
  row.querySelector('select')?.focus();
}
// 手部一块圈里有上下两个绑定，所以它的标注天然是两个键，写成 "Y / X"。
function zoneKeyLabel(id,def){
  const all=bindingsForDisplay();
  // 内核按合并后的 zone.leftHand 这些分发，effective_bindings 里也是它；以前只查上下两个
  // 旧编号，绑好了的手区在画面上照样写「未映射」。旧编号只在内核没报合并编号时兜底。
  const ids=all['zone.'+id]?[id]:(def.parts||[id]);
  const texts=ids.map((one)=>{const b=all['zone.'+one];return b&&!b.disabled?actionKeyText(b.action):null;}).filter(Boolean);
  return texts.length?texts.join(' / '):'未映射';
}

let currentPoseMap=null, kernelState=null, sourceMode='computer', cameraIndex=0, cameraRunning=false, modelAvailable=false, sessionStarted=false;
// 摄像头的完整状态和最近一次扫描结果。新手教学要按这些判断「这台电脑现在能开什么」。
let cameraInfo=null;const cameraScan={state:'idle',count:0,error:''};
// 急停真的被按了几次。教学的最后一步要认的是急停，不是随便哪种关掉输出。
let emergencyStops=0;
// 换过几次游戏、最后一次搜的是什么。教「换成我要玩的游戏」时，要认的是真的换成了。
let profileApplies=0,lastProfileQuery='';
const output={enabled:false,mode:'mouse',strength:160,server:null,xinputEnabled:false,xinputMotionLeft:false,xinputUser:null,xinputStatus:null};
const head={algorithm:'pnp',horizontalAlgorithm:'roll_tilt',deadzone:.10,sensitivityX:58,sensitivityY:46,enabled:true,invertY:false,verticalLookSource:'hand',verticalLookEnabled:false,verticalExclusive:false,bodyMotionGuard:false};
let handMouseConfig={enabled:true,horizontal_hand:'off',vertical_hand:'left'},lastTurnAlgorithm='gesture_v188',viewControlSaving=false,viewControlReady=false;
const gameProfile={catalog:[],selected:null,actions:{},overrides:{}};
let profileAutoSaveTimer=null,profileFlight=null,profileRevision=0,profileSwitching=false,profileConflict=false;
const profileDirty=new Set();
let serviceReady=false,actionBusy=false,outputEpoch=0,kernelEpoch=0,inputStatus={},headDirty=false;
let currentView='play',desiredSource=null,profileReady=false,profileLoading=false;
let kernelConnected=false,outputConnected=false,voiceInputReady=false;
const BASE_PROFILE_TRIGGERS=[
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
let poseLibrary=[];
let poseLibraryNames={cloud:{}};
const MOTION_CONFLICT_GROUPS=[
  {ids:['jumping_jack','hands_up'],label:'开合跳与双手举过头'},
];
const MOTION_CONFLICT_NAMES={march:'原地踏步',calf_back:'小腿向后抬起',squat:'下蹲',hands_up:'双手举过头',jumping_jack:'开合跳',side_step_jack:'侧步开合'};
const ACTION_TYPE_LABELS={keyboard:'键盘',mouse_button:'鼠标按键',mouse_wheel:'鼠标滚轮',gamepad:'Xbox 按键',gamepad_trigger:'Xbox 扳机',gamepad_axis:'Xbox 左摇杆',macro:'键盘宏',voice_release:'停住语音按住',system:'系统功能'};
// 宏库。每一处映射的下拉都从这里取，所以只在增删改之后刷一次，不跟着状态轮询走。
const macroLibrary={items:[],limits:null};
function macroById(id){return macroLibrary.items.find(item=>item.id===String(id||'').toLowerCase())||null}
function macroName(id){const found=macroById(id);return found?found.name:'宏已丢失'}
const TARGET_LABELS={LEFT:'左键',RIGHT:'右键',MIDDLE:'中键',X1:'侧键 1',X2:'侧键 2',SCROLL_UP:'向上滚',SCROLL_DOWN:'向下滚',LT:'LT',RT:'RT',L3:'L3',R3:'R3',DPAD_UP:'十字键上',DPAD_DOWN:'十字键下',DPAD_LEFT:'十字键左',DPAD_RIGHT:'十字键右',START:'Start',BACK:'Back',LS_UP:'左摇杆上',LS_DOWN:'左摇杆下',LS_LEFT:'左摇杆左',LS_RIGHT:'左摇杆右'};
// The dispatcher rejects anything outside this set, so offer the list instead
// of a free text field whose typos can only surface as a silent no-op in game.
const GAMEPAD_STICK_TARGETS=['LS_UP','LS_DOWN','LS_LEFT','LS_RIGHT'];
const GAMEPAD_TRIGGER_TARGETS=['LT','RT'];
// 映射表「系统功能」能选的，按这个顺序列。电脑那边 profile_schema.BINDING_SYSTEM_TARGETS
// 是准，这里只管名字和顺序；那边没有的不列。
const BINDING_SYSTEM_TARGETS=[['ZONES.MOVE_HERE','区域挪到我这里'],['ZONES.FREEZE_TOGGLE','定住区域 / 恢复跟随'],['ZONES.FREEZE','定住区域'],['ZONES.FOLLOW','区域恢复跟随'],['HEAD.CENTER','视角回正'],['OUTPUT.TOGGLE','开始 / 停止输出'],['OUTPUT.START','开始输出'],['OUTPUT.STOP','停止输出']];
const VOICE_SYSTEM_TARGETS=[['EMERGENCY_STOP','紧急停止'],['OUTPUT.START','开始输出'],['OUTPUT.STOP','停止输出'],['OUTPUT.TOGGLE','开始 / 停止输出'],['HEAD.CENTER','视角回正'],['HEAD_CALIBRATION_START','开始校准'],['ZONES.MOVE_HERE','区域挪到我这里'],['ZONES.FREEZE','定住区域'],['ZONES.FOLLOW','区域恢复跟随'],['ZONES.FREEZE_TOGGLE','定住区域 / 恢复跟随'],['POSE.RECORD','录一个新姿势'],['POSE.ADD_FRAME','给刚录的动作再加一个姿势'],['POSE.CANCEL','取消录制倒计时']];
const SYSTEM_TARGET_NAMES=new Map([...VOICE_SYSTEM_TARGETS,...BINDING_SYSTEM_TARGETS,['HEAD.CALIBRATE','开始校准']]);
const voice={status:null};
function currentVoiceWakeWord(status=voice.status){
  const wake=String(status?.wake_word||'体感').trim();
  return wake||'体感';
}
function renderVoiceGuide(status=voice.status){
  const wake=currentVoiceWakeWord(status),example=`${wake}地图`;
  const wakeHint=$('#voiceWakeWordHint');
  if(wakeHint)wakeHint.textContent=wake;
  const wakeExample=$('#voiceWakeExample');
  if(wakeExample)wakeExample.textContent=example;
  document.querySelectorAll('.voice-prefix').forEach(item=>item.textContent=wake);
  document.querySelectorAll('.voice-trigger-phrase').forEach(input=>{
    input.placeholder='完整口令';
    input.title=`说出的完整口令，前面加「${wake}」`;
  });
}
/* 语音模型的词表里没有的字，写进口令就永远听不到——模型只是悄悄丢掉，不报错。所以
 * 在输入框底下照实说是哪个字。几个框一起出现时攒成一次请求。 */
const voiceCheckQueue=new Set();let voiceCheckTimer=0;
function voiceCheckText(input){
  // 通用口令那一栏只填唤醒词后面的部分，实际说的是整句。
  return (input.classList.contains('voice-phrase')?currentVoiceWakeWord():'')+String(input.value||'').trim();
}
function voicePhraseTip(input){
  if(!input.voiceTip){
    const tip=document.createElement('small');tip.className='voice-phrase-tip';tip.hidden=true;input.voiceTip=tip;
    // 通用口令一行是网格，提示放在行尾、单独占满一行，不挤乱几栏。
    const row=input.closest('.voice-row');if(row)row.append(tip);else input.after(tip);
  }
  return input.voiceTip;
}
function queueVoiceCheck(input){voiceCheckQueue.add(input);clearTimeout(voiceCheckTimer);voiceCheckTimer=setTimeout(flushVoiceCheck,250)}
async function flushVoiceCheck(){
  const inputs=[...voiceCheckQueue].filter(input=>input.isConnected);voiceCheckQueue.clear();
  if(!inputs.length)return;
  const texts=inputs.map(voiceCheckText);
  let data;try{data=await post('/api/voice/check',{phrases:texts})}catch{return}
  if(!data.available)return;
  inputs.forEach((input,index)=>{
    if(voiceCheckText(input)!==texts[index])return;  // 查的时候又改了，下一轮再说
    const chars=data.results?.[index]?.unheard||[],tip=voicePhraseTip(input);
    tip.hidden=!chars.length;
    tip.textContent=chars.length?`${chars.map(char=>`「${char}」`).join('')}语音认不出，这句说了也听不到，换个说法`:'';
  });
}
function watchVoicePhrase(input){input.addEventListener('input',()=>queueVoiceCheck(input));queueVoiceCheck(input)}
let customPoses=[];
let customPoseScores={};
const overlay={win:null,canvas:null,ctx:null};
const perfUi={previewBusy:false};
let zoneEditMode=false,liveZoneDrag=null;
// 「挪动区域」：把跟随框定住再拖。框的坐标和内核一样是原始画面（没镜像）的比例。
const RECT_EDIT_ZONE_IDS=['leftHand','rightHand','leftFoot','rightFoot','headJump','lookGate'];
const RECT_EDIT_MIN=.02;
const rectEdit={rects:{},backup:{},selected:'',wasFrozen:false};
const LEGACY_ZONE_ALIASES={leftHand:['leftHandUpper','leftHandLower'],rightHand:['rightHandUpper','rightHandLower']};
let voiceCatalog=[];

function profileTriggers(){
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

const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
function notice(text){$('#notice').textContent=text;$('#notice').hidden=!text}
async function api(path,opt={}){
  let response;
  // 8s suits local calls. A cloud call is a download plus an apply on the far
  // side of the internet, so callers may ask for longer rather than being told
  // the local service is unresponsive when it is the network that is slow.
  const {timeoutMs=8000,...init}=opt;
  try{response=await fetch(path,{...init,signal:AbortSignal.timeout(timeoutMs)})}
  catch{throw new Error('本地服务无响应，请检查连接后重试')}
  let data;
  try{data=await response.json()}catch{throw new Error('服务响应无法读取')}
  if(!response.ok||data.ok===false){
    const error=new Error(data.error||`服务请求失败（${response.status}）`);
    error.status=response.status;throw error;
  }
  return data;
}
async function post(path,data,timeoutMs){return api(path,{method:'POST',headers:{'Content-Type':'application/json; charset=utf-8'},body:JSON.stringify(data),timeoutMs})}

function draw(map,target=ctx,w=canvas.width,h=canvas.height,mirror=false){
  target.save();target.setTransform(1,0,0,1,0,0);target.clearRect(0,0,w,h);
  if(mirror){target.translate(w,0);target.scale(-1,1)}
  target.strokeStyle='#55ddff';target.fillStyle='#fff';target.lineWidth=3;
  if(map){
    for(const [a,b] of EDGES){
      const p=map[a],q=map[b];if(!p||!q||p.score<.3||q.score<.3)continue;
      target.beginPath();target.moveTo(p.x*w,p.y*h);target.lineTo(q.x*w,q.y*h);target.stroke();
    }
    for(const p of Object.values(map)){
      if(p.score<.3)continue;
      target.beginPath();target.arc(p.x*w,p.y*h,3,0,Math.PI*2);target.fill();
    }
  }
  target.restore();
}
function renderKernelZones(zones={}){
  for(const[id,def]of Object.entries(BODY_ZONES)){
    const el=document.querySelector(`.zone[data-zone="${id}"]`),state=zones[id];if(!el)continue;
    if(def.gate&&!kernelState?.vertical_look?.enabled){el.style.display='none';continue}
    const active=!zoneEditMode&&!!state?.pressed;
    el.classList.toggle('active',active);
    el.querySelector('strong').textContent=def.gate?'上下视角':zoneKeyLabel(id,def);
    el.querySelector('small').textContent=def.gate?(active?'已开启':'左手放这里'):def.body;
    el.tabIndex=zoneEditMode?0:-1;
    el.setAttribute('aria-label',def.body+'区域，方向键移动，Shift 加方向键改大小');
    const rect=zoneEditMode?rectEdit.rects[id]:state?.rect;
    el.classList.toggle('selected',zoneEditMode&&rectEdit.selected===id);
    if(!rect){el.style.display='none';continue}
    el.style.display='grid';el.style.left=(rect.x1*100)+'%';el.style.top=(rect.y1*100)+'%';el.style.width=((rect.x2-rect.x1)*100)+'%';el.style.height=((rect.y2-rect.y1)*100)+'%';
  }
}

function renderCalibrationOverlay(hs={}){
  const layer=$('#calibrationOverlay');if(!layer)return;
  const active=!!hs.calibrating,notice=String(hs.notice||hs.calibration_notice_text||'');
  if(!active){if(layer.open)layer.close();return}
  if(!layer.open)layer.showModal();
  const stage=$('#calibrationStage'),prompt=$('#calibrationPrompt'),countdown=$('#calibrationCountdown');
  const bar=$('#calibrationProgressBar'),detail=$('#calibrationDetail'),cancel=$('#calibrationCancel');
  const phase=String(hs.center_phase||'prepare');
  const phaseName={prepare:'准备',collect:'记录自然中心'}[phase]||'设置中心';
  cancel.style.display='block';stage.textContent=phaseName;
  prompt.textContent=notice||'看向游戏屏幕中心，保持自然站姿/坐姿';
  const remaining=Math.max(0,Number(hs.center_remaining_s||0));
  const valid=Number(hs.center_valid_s||0),required=Math.max(.1,Number(hs.center_required_s||2.2));
  const samples=Number(hs.center_sample_count||0),targetSamples=Math.max(1,Number(hs.center_target_samples||32));
  const invalid=Number(hs.center_invalid_count||0),rejected=Number(hs.center_rejected_count||0);
  if(phase==='prepare')detail.textContent='说完后稍等一下，让说话造成的头部/嘴部动作结束';
  else detail.textContent=`采集 ${Math.min(valid,required).toFixed(1)} / ${required.toFixed(1)} 秒 · 有效样本 ${samples} / ${targetSamples} · 无效 ${invalid} · 忽略明显跳点 ${rejected}`;
  countdown.textContent=remaining.toFixed(1);
  const timeProgress=Math.min(1,valid/required),sampleProgress=Math.min(1,samples/targetSamples);
  const progress=phase==='collect'?(.12+.88*Math.min(timeProgress,sampleProgress)):.08;
  bar.style.width=`${Math.max(0,Math.min(100,progress*100))}%`;
}
function renderMainStatus(){
  serviceReady=kernelConnected&&outputConnected;
  const main=$('#mainActionBtn');
  main.disabled=!serviceReady||actionBusy||zoneEditMode;
  main.textContent=output.enabled?'暂停游戏控制':(!sessionStarted&&!inputStatus.handheld_connected&&!voiceInputReady?'连接设备':'开始游戏控制');
  main.classList.toggle('running',!!output.enabled);
  $('#serviceStatus').textContent=serviceReady?'本地服务已连接':'服务失联 · 当前状态无法确认';
  $('#serviceStatus').classList.toggle('online',serviceReady);$('#serviceStatus').classList.toggle('offline',!serviceReady);
  const missing=[];
  if(!currentPoseMap)missing.push('人体未识别：区域和身体动作不可用');
  $('#mainActionStatus').textContent=!serviceReady?'请检查本地服务；紧急停止可继续重试':
    zoneEditMode?'区域调整中 · 体感输出已关闭':
    (output.enabled?'正在控制游戏':'游戏控制已暂停')+(missing.length?' · '+missing.join('；'):' · 可以开玩');
  $('#hint').textContent=currentPoseMap?'区域亮起表示动作已触发':'请让头部和双肩入镜；脚部动作需要脚部入镜';
  renderConflicts();
}

// 两个设置各自都合法，合起来却什么都不做。玩家看不出区别——功能开着、读数在跳、
// 就是没反应。所以在"开始"那一页点名，并把能一键改的那一下也给出来。
function mergeOwnsSticks(){return output.xinputEnabled&&output.mode==='gamepad'}
function setupConflicts(){
  const hand=kernelState?.head?.hand_mouse||{};
  const items=[];
  if(inputStatus.phone_ignored)
    items.push(['手机在传画面，但来源选的是电脑摄像头，手机的画面没有用上。','改用手机',()=>setSource('phone',true)]);
  if(mergeOwnsSticks()&&hand.enabled)
    items.push(['物理手柄合流占着两个摇杆，手控鼠标不会动。','关掉合流',async()=>{$('#xinputMerge').value='';await setXinputMerge()}]);
  const verticalHand=hand.config?.vertical_hand??hand.vertical_hand;
  if((hand.config?.enabled??hand.enabled)&&verticalHand!=='off'&&head.verticalLookEnabled&&head.verticalLookSource==='hand')
    items.push(['手控鼠标握拳时会接管视角，与单独的上下视角控制同时开启可能相互干扰。','关掉上下视角',
      async()=>{const s=$('#verticalLookSource');if(s){s.value='off';s.dispatchEvent(new Event('change',{bubbles:true}))}}]);
  return items;
}
function renderConflicts(){
  const box=$('#setupConflicts');
  if(!box)return;
  const items=setupConflicts();
  box.hidden=!items.length;
  box.replaceChildren(...items.map(([text,label,action])=>{
    const row=document.createElement('div');row.className='conflict';
    const words=document.createElement('span');words.textContent=text;row.append(words);
    if(label){
      const fix=document.createElement('button');fix.className='btn';fix.textContent=label;
      fix.addEventListener('click',()=>runAction(action));row.append(fix);
    }
    return row;
  }));
}
function renderKernelState(runtime,force=false){
  kernelState=runtime?.kernel||runtime||{};sourceMode=runtime?.body_mode||sourceMode;const k=kernelState;
  // 录姿势的倒计时在服务端，按钮和口令触发的是同一个。这里只负责画出来。
  paintPoseCountdown(runtime?.pose_capture);
  renderTriggerLive();
  renderRange();
  const frameWidth=Number(k.width)||640,frameHeight=Number(k.height)||480;
  currentPoseMap=k.pose||null;if(canvas.width!==frameWidth||canvas.height!==frameHeight){canvas.width=frameWidth;canvas.height=frameHeight}viewer.style.aspectRatio=`${frameWidth}/${frameHeight}`;viewer.style.setProperty('--frame-ratio',String(frameWidth/frameHeight));draw(currentPoseMap);renderKernelZones(k.zones||{});renderZoneFit(k);renderZoneFreeze(k);
  const zonePad={leftHand:'#padX',rightHand:'#padB',leftFoot:'#padLB',rightFoot:'#padRB',headJump:'#padA'};
  const activeZones=[];for(const trigger of BASE_PROFILE_TRIGGERS.filter(t=>t.group==='zones')){const pressed=!!k.zones?.[trigger.id]?.pressed;$(zonePad[trigger.id])?.classList.toggle('active',pressed);if(pressed)activeZones.push(trigger.name)}
  $('#buttonStatus').textContent=activeZones.length?'身体区域：'+activeZones.join(' + '):(currentPoseMap?'身体区域：未触发':'身体区域：等待人体');
  // 没绑键的动作做了也不按键，这一页上不亮它；认没认出来去「动作测试」页看。
  const active=new Set((k.motions||[]).filter(id=>triggerMapped('motion.'+id)));
  for(const chip of document.querySelectorAll('#triggerChips .trigger-chip'))chip.classList.toggle('active',chip.dataset.group==='motion'?active.has(chip.dataset.id):false);
  // 自定义姿势的相似度跟着主状态一起来，不另开一路轮询。
  customPoseScores=k.custom_pose_scores||{};paintCustomPoseScores();paintPoseLibrary();
  const poses=new Set((k.poses_active||[]).filter(id=>triggerMapped('pose.'+id)));
  for(const chip of document.querySelectorAll('#triggerChips .trigger-chip[data-group="pose"]'))chip.classList.toggle('active',poses.has(chip.dataset.id));
  const actionName=id=>poseLibrary.find(item=>item.id===id)?.name||profileTriggers().find(t=>t.id===id)?.name||id;
  const statusParts=[];if(active.size)statusParts.push('动作：'+[...active].map(actionName).join(' + '));if(poses.size)statusParts.push('动作：'+[...poses].map(actionName).join(' + '));
  $('#motionStatus').textContent=statusParts.join(' · ')||'动作：未触发';
  const hs=k.head||{};
  const guardVersion=String(hs.body_motion_guard_version||k.body_motion_guard_version||'未上报');
  const guardEnabled=hs.body_motion_guard_enabled??k.body_motion_guard_enabled??false;
  const guardActive=hs.body_motion_guard_active??k.body_motion_guard_active;
  const guardBlocked=!!hs.horizontal_paused_by_body_motion;
  const guardReason=String(hs.body_motion_guard_veto_reason||'');
  const guardReasonLabel={early:'提前抑制',postburst:'动作后抑制',persistent:'持续防晃'}[guardReason]||'输出抑制';
  const guardStatus=$('#bodyMotionGuardStatus');
  if(guardStatus){
    guardStatus.textContent=guardEnabled===false?`防晃 ${guardVersion} · 已关闭`:guardBlocked?`防晃 ${guardVersion} · ${guardReasonLabel} · 左右视角已稳定`:guardActive?`防晃 ${guardVersion} · 监测中 · 当前未拦截左右视角`:`防晃 ${guardVersion} · 已启用 · 待机`;
    guardStatus.classList.toggle('active',guardBlocked&&guardEnabled!==false);
  }
  if(Number.isFinite(hs.output_x)){
    const usesHand=hs.hand_mouse?.enabled&&['left','right'].includes(hs.hand_mouse?.config?.horizontal_hand);
    const method=usesHand?'握拳':hs.horizontal_algorithm==='roll_tilt'?'侧倾':'转头';
    const horizontalCalibrated=usesHand||(hs.horizontal_calibrated??hs.calibrated);
    $('#headStatus').textContent=!usesHand&&hs.enabled===false?'左右视角已关闭':horizontalCalibrated?(!usesHand&&guardBlocked?'身体动作中 · 左右视角已稳定':`${method} · 左右 ${Number(hs.output_x).toFixed(0)}%`):'头控：等待中心，可说「体感开始校准」';
  }
  if(hs.calibrated!==undefined){
    $('#calBtn').textContent=hs.calibrating?'取消校准':'站好并校准';
    $('#calStatus').textContent=hs.calibrating?(hs.notice||hs.quality||'正在校准'):(hs.notice||hs.quality||'等待校准，可说「开始校准」');
    const missingPoints=(hs.frozen22_missing_points||[]).join('、');
    $('#calStatus').title=[hs.estimate_error,missingPoints&&'缺少关键点：'+missingPoints].filter(Boolean).join(' · ');
    renderCalibrationOverlay(hs);
  }
  if(hs.algorithm&&(force||(!headDirty&&!document.activeElement?.closest('#headSettings,#advancedSettings')))){
    $('#headAlgorithm').value=hs.algorithm;
    const horizontalAlgorithm=String(hs.horizontal_algorithm||'roll_tilt');
    head.horizontalAlgorithm=['gesture_v153','frozen22','gesture_v188','roll_tilt'].includes(horizontalAlgorithm)?horizontalAlgorithm:'gesture_v188';
    if(head.horizontalAlgorithm!=='roll_tilt')lastTurnAlgorithm=head.horizontalAlgorithm;
    if($('#headHorizontalAlgorithm'))$('#headHorizontalAlgorithm').value=head.horizontalAlgorithm;
    if($('#rollTiltHint'))$('#rollTiltHint').hidden=head.horizontalAlgorithm!=='roll_tilt';
    const verticalLookSource=String(hs.verticalLookSource||hs.vertical_look_source||k.vertical_look?.source||'off');
    head.verticalLookSource=verticalLookSource==='head'?'head':'hand';
    head.verticalLookEnabled=verticalLookSource!=='off'&&(k.vertical_look?.enabled??hs.vertical_look_enabled??true)!==false;
    head.verticalExclusive=!!(k.vertical_look?.exclusive_axes??hs.vertical_exclusive_axes);
    head.bodyMotionGuard=k.vertical_look?.body_motion_guard===true;
    if($('#verticalLookSource'))$('#verticalLookSource').value=head.verticalLookEnabled?head.verticalLookSource:'off';
    if($('#verticalExclusive'))$('#verticalExclusive').checked=head.verticalExclusive;
    if($('#bodyMotionGuard'))$('#bodyMotionGuard').checked=head.bodyMotionGuard;
    document.querySelectorAll('.head-vertical-setting').forEach(el=>el.style.setProperty('display',head.verticalLookSource==='head'?'block':'none','important'));
    $('#deadzone').value=Math.round(Number(hs.deadzone||.10)*100);
    $('#speedX').value=Number(hs.sensitivity_x||58);$('#speedY').value=Number(hs.sensitivity_y||46);
    $('#headEnable').checked=!!hs.enabled;$('#invertY').checked=!!hs.invert_y;syncControlLabels();renderViewControl();
  }
  const camera=runtime?.camera||{running:cameraRunning};
  cameraRunning=!!camera.running;if(runtime?.camera)cameraInfo=runtime.camera;
  sessionStarted=sourceMode==='phone'?true:cameraRunning;
  if(desiredSource===null)$('#poseSource').value=sourceMode;
  syncCameraDeviceRow();

  if(cameraPreview){
    const showPreview=sourceMode==='computer'&&cameraRunning;
    cameraPreview.hidden=!(showPreview&&cameraPreview.complete&&cameraPreview.naturalWidth);
    if(!showPreview&&cameraPreview.hasAttribute('src')){
      URL.revokeObjectURL(cameraPreview.src);cameraPreview.removeAttribute('src');
    }
  }
  $('#cameraPill').textContent=(sourceMode==='phone'?inputStatus.mobile_pose_connected:cameraRunning)?'摄像头 ✓':'摄像头';$('#cameraPill').className='pill '+(sourceMode==='phone'||cameraRunning?'ok':'bad');
  // phonePill is owned by renderInputStatus (/api/input/status); kernel status has no transport state.
  $('#posePill').textContent=currentPoseMap?'人体 ✓':'人体';$('#posePill').className='pill '+(currentPoseMap?'ok':'bad');const gateActive=!!k.vertical_gate_active;const verticalSource=String(hs.verticalLookSource||hs.vertical_look_source||k.vertical_look?.source||'hand')==='head'?'头部':'右手';const gateStatus=$('#lookGateStatus');if(gateStatus){gateStatus.hidden=!k.vertical_look?.enabled;const paused=!!hs.horizontal_paused_by_vertical_gate;gateStatus.textContent=head.verticalLookEnabled?(gateActive?`上下视角已开启 · ${verticalSource}控制上下${paused?' · 左右暂停':''}`:'上下视角待机 · 左手放入绿色区域开启'):'上下视角已关闭';gateStatus.className='look-gate-status '+(gateActive?'active':'')}renderOverlay(currentPoseMap);renderMainStatus();

}
function renderInputStatus(status){
  inputStatus=status||{};
  const connected=!!(status.mobile_pose_connected||status.handheld_connected);
  for(const id of ['mobileStatus','phonePill']){
    $('#'+id).textContent=connected?(status.mobile_pose_connected?'手机摄像头已连接':'手机手持端已连接'):'手机未连接';
    $('#'+id).className='pill '+(connected?'ok':'bad');
  }
  // 手机慢下来只有两种可能：模型退回了 CPU，或者这台机器就这么快。光看帧率分
  // 不出来，所以把 delegate 和每帧耗时一起摆出来。
  const phone=(status.mobile_pose_sources||[]).find(item=>item.active)||(status.mobile_pose_sources||[])[0];
  const perf=$('#phonePerf');
  if(perf){
    const ms=Number(phone?.inference_ms);
    const parts=[];
    if(phone?.delegate)parts.push(phone.delegate==='CPU'?'跑在 CPU（慢一倍，GPU 不可用时的退路）':'跑在 GPU');
    if(Number.isFinite(ms)&&ms>0)parts.push(`每帧 ${Math.round(ms)} ms · 约 ${Math.round(1000/ms)} 帧/秒`);
    perf.textContent=parts.join(' · ');
  }
  // 数据线插没插、网络共享开没开，服务端本来就知道，以前只是没说出来。而这正是
  // 有线连不上时唯一的分岔：没开网络共享的话，线插得再紧也没有一条能通的路。
  const tether=$('#usbTetherStatus');
  if(tether){
    const usb=status.usb_tether||{};
    tether.textContent=usb.present
      ? `数据线已接通（${usb.adapter||'USB'}）${usb.carries_internet?'，这台电脑正在走手机的网':''}`
      : '没检测到数据线。用 Wi-Fi 连可以不管；插了线还这样，说明手机上的「USB 网络共享」没打开。';
    tether.className='statusline'+(usb.present?'':' warn');
  }
  const field=$('#phoneWsUrl'),urls=status.phone_ws_urls||[];
  if(document.activeElement!==field&&JSON.stringify(urls)!==field.dataset.urls){
    field.dataset.urls=JSON.stringify(urls);
    field.replaceChildren(...urls.map(url=>new Option(url,url)));
  }
  renderMainStatus();
}
async function refreshKernel(){
  const epoch=kernelEpoch;
  try{
    const runtime=await api('/api/kernel/status');
    kernelConnected=true;
    if(epoch===kernelEpoch)renderKernelState(runtime);
    if(!profileReady&&!profileLoading)void loadProfiles();
  }catch{kernelConnected=false;renderMainStatus()}
}
async function refreshInput(){
  try{renderInputStatus(await api('/api/input/status?brief=1'))}
  catch{renderInputStatus({});$('#mobileStatus').textContent='设备状态无法确认'}
}
function bindingFor(trigger){
  const items=gameProfile.selected?.bindings?.[trigger.group]||{};
  if(Object.prototype.hasOwnProperty.call(items,trigger.id))return items[trigger.id];
  if(trigger.group==='zones'){
    for(const alias of LEGACY_ZONE_ALIASES[trigger.id]||[]){if(Object.prototype.hasOwnProperty.call(items,alias))return items[alias]}
  }
  return trigger.defaultBinding||null;
}
function selectedMotionIdsFromRows(){
  const selected=new Set();
  document.querySelectorAll('.binding-row[data-trigger^="motion."]').forEach(row=>{
    if(row.querySelector('.binding-type')?.value)selected.add(String(row.dataset.trigger).slice('motion.'.length));
  });
  return selected;
}
function motionConflictsForSelection(selected){
  return MOTION_CONFLICT_GROUPS
    .map(group=>group.ids.filter(id=>selected.has(id)))
    .filter(active=>active.length>1);
}
function motionConflictText(conflicts){
  return conflicts.map(group=>group.map(id=>MOTION_CONFLICT_NAMES[id]||id).join('、')).join('；');
}
function syncMotionConflictChoices(){
  const selected=selectedMotionIdsFromRows();
  const states=new Map();
  const stateFor=id=>{let state=states.get(id);if(!state){state={blockedBy:new Set(),conflictWith:new Set()};states.set(id,state)}return state};
  for(const group of MOTION_CONFLICT_GROUPS){
    const active=group.ids.filter(id=>selected.has(id));
    if(active.length>1){
      for(const id of active){for(const other of active){if(other!==id)stateFor(id).conflictWith.add(other)}}
    }else if(active.length===1){
      for(const id of group.ids){if(id!==active[0])stateFor(id).blockedBy.add(active[0])}
    }
  }
  // 所有身体动作，包括从官方动作库下载的：开合跳、双手举过头都是下载来的，只看程序
  // 自带的那两个，这里的互斥就形同虚设。
  for(const trigger of profileTriggers().filter(item=>item.group==='motions')){
    const row=document.querySelector(`.binding-row[data-trigger="${trigger.key}"]`);if(!row)continue;
    const state=states.get(trigger.id)||{blockedBy:new Set(),conflictWith:new Set()};
    const select=row.querySelector('.binding-type');if(!select)continue;
    const blocked=state.blockedBy.size>0&&!selected.has(trigger.id);
    select.disabled=blocked;
    select.title=blocked?`与 ${[...state.blockedBy].map(id=>MOTION_CONFLICT_NAMES[id]||id).join('、')} 冲突，先取消该动作`:'';
    row.classList.toggle('motion-conflict-blocked',blocked);
    row.classList.toggle('motion-conflict-error',state.conflictWith.size>0);
    const note=row.querySelector('.motion-conflict-note');
    if(note){
      if(state.conflictWith.size){note.hidden=false;note.textContent=`冲突：与 ${[...state.conflictWith].map(id=>MOTION_CONFLICT_NAMES[id]||id).join('、')} 只能选一个`}
      else if(blocked){note.hidden=false;note.textContent=`已禁用：与 ${[...state.blockedBy].map(id=>MOTION_CONFLICT_NAMES[id]||id).join('、')} 冲突`}
      else{note.hidden=true;note.textContent=''}
    }
  }
}
function targetLabel(action){
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
function syncProfileZoneLabels(){
  const zonePad={leftHand:'#padX',rightHand:'#padB',leftFoot:'#padLB',rightFoot:'#padRB',headJump:'#padA'};
  for(const trigger of BASE_PROFILE_TRIGGERS.filter(t=>t.group==='zones')){
    // The region is read at a glance mid-game, so show the button by itself.
    // A profile's descriptive label ("左脚区 · LB") belongs in the mapping list;
    // here it only shrinks the part that matters. The body part stays in <small>.
    const binding=bindingFor(trigger);
    const label=binding&&!binding.disabled?targetLabel(binding.action):'—';
    if(BODY_ZONES[trigger.id])BODY_ZONES[trigger.id].label=label;
    const el=$(zonePad[trigger.id]);if(el)el.textContent=label;
  }
  renderKernelZones(kernelState?.zones||{});
}
function profileMetaText(profile){
  if(!profile)return '未选择游戏';
  const appid=profile.appid||profile.steam_appid||'';
  // 自己加的游戏不是「实验配置」——那句话说的是自动生成的那两百个没人试过，而
  // 这一个是他自己建的，本来就该自己调，说它"实验"只会让人以为是软件出的问题。
  if(profile.source==='custom'){
    return ['我自己加的', appid?'AppID '+appid:'没填 AppID',
            '底档 '+(profile.base||'generic-xbox')].join(' · ');
  }
  const source=profile.source||{};
  const verified=source.verified?' · 已验证':' · 实验配置';
  const sourceName=source.kind==='manual'?'人工':(source.kind==='steaminputdb'?'社区配置库':(source.kind==='builtin'?'内置':'离线库'));
  const bindings=profile.bindings||{};
  const zones=Object.keys(bindings.zones||{}).length;
  const motions=Object.keys(bindings.motions||{}).length+Object.keys(bindings.poses||{}).length;
  const voice=voiceCatalog.filter(item=>!item.system_fixed&&!String(item.id||'').startsWith('game.profile_slot_')&&item.effective_action).length;
  const coverage=`${zones} 区域 · ${motions} 动作 · ${voice} 语音`;
  return `${appid?`Steam ${appid} · `:''}${sourceName}${verified} · ${coverage}`;
}
function renderProfileHeader(){
  const p=gameProfile.selected;
  renderOutputMix();
  // 改名和删除只对自己加的那些有意义：内置那两百个删不得也改不得。
  const mine=p?.source==='custom';
  $('#customGameActions').hidden=!mine;
  $('#profileGameName').textContent=p?.name||'未选择游戏';
  $('#currentGameName').textContent=p?.name||'未选择游戏';
  $('#profileMeta').textContent=profileMetaText(p);
  $('#profileUnverified').hidden=!p||Boolean(p.source?.verified);
  syncProfileZoneLabels();
}
function renderProfileCatalog(games){
  gameProfile.catalog=Array.isArray(games)?games:[];
  const select=$('#profileSelect'),selectedId=gameProfile.selected?.selected_id||gameProfile.selected?.id||'';
  select.replaceChildren();
  if(!gameProfile.catalog.length){const o=document.createElement('option');o.value='';o.textContent='没有匹配的游戏';select.appendChild(o);return}
  // Hand-verified profiles come first: of ~200 shipped profiles only a
  // handful have actually been played, and a flat alphabetical list makes
  // an auto-generated one look as official as a tested one.
  // 自己加的排在最前，比「已验证」还靠前：会来翻这个列表的人多半就是为了找自己
  // 加的那个，而内置那两百个可以搜。服务端的 list_games 已经这么排了，前端又按
  // verified 重排一遍，等于把它推回两百条里去。
  const rank=g=>g.source==='custom'?2:(g.verified?1:0);
  const ordered=[...gameProfile.catalog].sort((a,b)=>rank(b)-rank(a)||String(a.name).localeCompare(String(b.name),'zh'));
  for(const g of ordered){
    const o=document.createElement('option');o.value=g.id;
    // 「实验」说的是没人试过的自动生成配置。自己加的不属于那一类，标错了会让人
    // 以为是软件给的半成品，而不是他自己刚建的空白档。
    const tail=g.source==='custom'?' · 我加的':(g.verified?'':' · 实验');
    o.textContent=`${g.verified?'✓ ':''}${g.name}${g.appid?` · ${g.appid}`:''}${tail}`;
    select.appendChild(o);
  }
  if(gameProfile.catalog.some(g=>g.id===selectedId))select.value=selectedId;
}
async function searchProfiles(){
  const q=$('#profileSearch').value.trim();
  const data=await api('/api/game-profiles/catalog'+(q?'?q='+encodeURIComponent(q):''));
  lastProfileQuery=q;
  renderProfileCatalog(data.games||[]);
  $('#profileMeta').textContent=`离线库 ${Number(data.library_count||data.count||0)} 款 · 当前显示 ${Number(data.count||0)} 款`;
}
async function refreshProfile(){
  const [selected,actions]=await Promise.all([api('/api/game-profiles/selected'),api('/api/output/actions')]);
  gameProfile.selected=selected.profile||null;gameProfile.actions=actions.actions||{};gameProfile.overrides=gameProfile.selected?.overrides||{};
  renderProfileHeader();renderProfileBindingRows();await searchProfiles();renderProfileHeader();
}
// ---- 自己加的游戏 ---------------------------------------------------------
// 内置目录只有两百个，而且是从 Steam 榜单生成的，漏掉很正常。以前搜不到就只能
// 借用别人的坑位：界面上一直显示着错的游戏名，第二个未收录的游戏就没地方放。
//
// 加完直接选中它。会来加游戏的人就是为了马上用它——加完还要自己再去下拉框里找
// 一遍，等于把一件事拆成两件。
async function addCustomGame(){
  const name=$('#customGameName').value.trim();
  if(!name){notice('先给这个游戏起个名字');return}
  await profileOperation(async()=>{
    const data=await post('/api/game-profiles/custom/add',
      {name,appid:$('#customGameAppid').value});
    $('#customGameName').value='';$('#customGameAppid').value='';
    const picked=await post('/api/game-profiles/select',{id:data.game.id});
    gameProfile.selected=picked.profile;gameProfile.overrides=picked.profile.overrides||{};++profileApplies;
    // searchProfiles 会把 #profileMeta 写成库统计，所以头部要排在它后面重画一次。
    await refreshVoiceCommands();renderProfileBindingRows();await searchProfiles();renderProfileHeader();
    notice(`已添加并切换到「${data.game.name}」。按键在下面自己绑。`);
  });
}
async function renameCustomGame(){
  const current=gameProfile.selected;
  if(current?.source!=='custom')return;
  const name=prompt('改成什么名字？按键映射不会丢，它是按编号存的。',current.name);
  if(name===null)return;
  await profileOperation(async()=>{
    const data=await post('/api/game-profiles/custom/rename',{id:current.id,name});
    gameProfile.selected={...current,name:data.game.name};
    await searchProfiles();renderProfileHeader();
    notice(`已改名为「${data.game.name}」`);
  });
}
async function removeCustomGame(){
  const current=gameProfile.selected;
  if(current?.source!=='custom')return;
  if(!confirm(`删掉「${current.name}」？给它调的按键映射会一起删掉，恢复不了。`))return;
  await profileOperation(async()=>{
    // 删的是正在用的那个，服务端会退回通用档并把新绑定推给内核和手机，所以这里
    // 要用它返回的那份，不能继续显示一个已经不存在的游戏。
    const data=await post('/api/game-profiles/custom/remove',{id:current.id});
    gameProfile.selected=data.profile;gameProfile.overrides=data.profile.overrides||{};
    await refreshVoiceCommands();renderProfileBindingRows();await searchProfiles();renderProfileHeader();
    notice(`已删掉，当前游戏退回「${data.profile.name}」`);
  });
}

async function applySelectedProfile(){
  const id=$('#profileSelect').value;if(!id||profileSwitching)return;
  await profileOperation(async()=>{
    await saveProfileBindings();
    const data=await post('/api/game-profiles/select',{id});
    gameProfile.selected=data.profile;++profileApplies;
    gameProfile.overrides=data.profile.overrides||{};
    await refreshVoiceCommands();renderProfileHeader();renderProfileBindingRows();
    notice(`已切换游戏：${data.profile.name}`);
  });
}
async function profileOperation(operation){
  profileSwitching=true;$('#mappingFields').disabled=true;
  for(const id of ['profileApplyBtn','resetProfileBindingsBtn','profileSelect','customGameAddBtn','customGameRenameBtn','customGameRemoveBtn'])$('#'+id).disabled=true;
  try{await operation()}
  finally{
    profileSwitching=false;$('#mappingFields').disabled=false;
    for(const id of ['profileApplyBtn','resetProfileBindingsBtn','profileSelect','customGameAddBtn','customGameRenameBtn','customGameRemoveBtn'])$('#'+id).disabled=false;
  }
}
function makeTypeSelect(binding){
  const sel=document.createElement('select');sel.className='binding-type';
  const none=document.createElement('option');none.value='';none.textContent='不映射';sel.appendChild(none);
  for(const type of Object.keys(ACTION_TYPE_LABELS)){if(!gameProfile.actions?.[type])continue;const o=document.createElement('option');o.value=type;o.textContent=ACTION_TYPE_LABELS[type];sel.appendChild(o)}
  sel.value=binding?.disabled?'':(binding?.action?.type||'');return sel;
}
// 「停住语音按住」能停哪几条：本游戏口令里现在设成持续按住的那些（循环的宏也算，
// 它同样要另一个动作才停得下来）。读的是表里的当前状态而不是存下来的配置——刚把
// 口令 1 改成持续按住，别的行马上就该能选它，不用先等保存。自己停自己没有意义，
// 所以排除本行。
function voiceCommandId(triggerKey){return String(triggerKey||'').replace(/^voice\./,'')}
function voiceCommandPhrase(id){
  id=voiceCommandId(id);
  const row=document.querySelector(`.binding-row[data-trigger="voice.${id}"] .voice-trigger-phrase`);
  const binding=bindingsForDisplay()[`voice.${id}`];
  return row?.value.trim()||binding?.phrase||voiceCatalog.find(item=>item.id===id)?.phrase||id;
}
function voiceCommandName(id){
  id=voiceCommandId(id);
  const slot=id.startsWith('game.profile_slot_')?`口令 ${Number(id.replace('game.profile_slot_',''))}`:'';
  const phrase=voiceCommandPhrase(id);
  return slot&&phrase!==id?`${slot}「${phrase}」`:slot||phrase;
}
function voiceCommandIds(value){
  const values=Array.isArray(value)?value:[value];
  return values.map(voiceCommandId).filter(Boolean);
}
function voiceCommandPhrases(value){return voiceCommandIds(value).map(voiceCommandPhrase).filter(Boolean)}
function voiceCommandNames(value){return voiceCommandIds(value).map(voiceCommandName).filter(Boolean)}
function voiceReleaseTargetIds(select){return [...(select?.selectedOptions||[])].map(option=>voiceCommandId(option.value)).filter(Boolean)}
function voiceHoldChoices(excludeKey=''){
  const out=[];
  for(const row of document.querySelectorAll('.binding-row[data-trigger^="voice."]')){
    if(row.dataset.trigger===excludeKey)continue;
    const type=row.querySelector('.binding-type')?.value||'';
    if(!type||type==='voice_release')continue;
    const behavior=row.querySelector('select.binding-behavior')?.value||row.querySelector('.binding-behavior')?.dataset.value;
    if(behavior==='hold')out.push(voiceCommandId(row.dataset.trigger));
  }
  return out;
}
function fillVoiceReleaseSelect(select,excludeKey,value){
  const previous=voiceReleaseTargetIds(select);
  const want=voiceCommandIds(value==null?previous:value);
  const choices=voiceHoldChoices(excludeKey);
  select.multiple=true;
  select.title='可多选：按住 Ctrl 再点选多条口令';
  select.replaceChildren();
  for(const id of choices){const o=document.createElement('option');o.value=id;o.textContent=voiceCommandName(id);o.selected=want.includes(id);select.appendChild(o)}
  // 指着的那条已经不是持续按住了，照实写出来，不偷偷换成别的一条。
  for(const id of want.filter(item=>!choices.includes(item))){const o=document.createElement('option');o.value=id;o.textContent=`${voiceCommandName(id)}（已不是持续按住）`;o.selected=true;select.appendChild(o)}
  if(!select.options.length){const o=document.createElement('option');o.value='';o.textContent='先把一条本游戏口令设成「持续按住」';o.selected=true;select.appendChild(o)}
  select.size=Math.min(4,Math.max(2,select.options.length));
  select.disabled=!choices.length&&!want.length;
}
// 口令改了说法、改成或不再是持续按住，所有「停住语音按住」的下拉框和选项都跟着变。
function syncVoiceReleaseChoices(){
  for(const row of document.querySelectorAll('#profileBindingRows .binding-row')){
    const typeSel=row.querySelector('.binding-type');if(!typeSel)continue;
    const option=[...typeSel.options].find(o=>o.value==='voice_release');
    if(option){
      const none=!voiceHoldChoices(row.dataset.trigger).length;
      option.disabled=none&&typeSel.value!=='voice_release';
      option.textContent=none?`${ACTION_TYPE_LABELS.voice_release}（没有持续按住的口令）`:ACTION_TYPE_LABELS.voice_release;
    }
    const select=row.querySelector('select.voice-release-target');
    if(select)fillVoiceReleaseSelect(select,row.dataset.trigger,voiceReleaseTargetIds(select));
  }
}
// 键盘键位框：点进去按一下，就换成刚按的那个键。以前是普通文本框，原来写着 SPACE，
// 想换成 D 一按就成了 SPACED，还存不进去。按住几个一起按是组合键（按住 CTRL 再按 W
// 就是 CTRL+W），全部松开才算定下来——宏那边一提交就整条重画，按到一半就提交的话，
// 手还没松框就没了。键名对的是电脑那边 profile_schema.KEYBOARD_KEYS 那一套，那套以外
// 的键（小键盘、分号这些）按了只提示，原来的值不动。
const KEY_CODE_NAMES={Space:'SPACE',Enter:'ENTER',NumpadEnter:'ENTER',Escape:'ESC',Tab:'TAB',
  ShiftLeft:'SHIFT',ShiftRight:'SHIFT',ControlLeft:'CTRL',ControlRight:'CTRL',AltLeft:'ALT',AltRight:'ALT',MetaLeft:'WIN',MetaRight:'WIN',
  Backspace:'BACKSPACE',Delete:'DELETE',Home:'HOME',End:'END',PageUp:'PAGEUP',PageDown:'PAGEDOWN',
  ArrowLeft:'LEFT',ArrowUp:'UP',ArrowRight:'RIGHT',ArrowDown:'DOWN'};
const KEY_MODIFIERS=['CTRL','SHIFT','ALT','WIN'];
function keyNameFromCode(code){
  if(/^Key[A-Z]$/.test(code))return code.slice(3);
  if(/^Digit[0-9]$/.test(code))return code.slice(5);
  if(/^F([1-9]|1[0-2])$/.test(code))return code;
  return KEY_CODE_NAMES[code]||null;
}
// 提示贴在框下面。页顶那条通知栏在映射表、宏这里往往已经滚出屏幕了。
function keyCaptureTip(input,text){
  document.querySelector('.key-capture-tip')?.remove();
  const tip=document.createElement('div');tip.className='key-capture-tip';tip.textContent=text;
  const r=input.getBoundingClientRect();tip.style.left=`${r.left+window.scrollX}px`;tip.style.top=`${r.bottom+window.scrollY+4}px`;
  document.body.appendChild(tip);setTimeout(()=>tip.remove(),1800);
}
function makeKeyCaptureInput(className,value=''){
  const input=document.createElement('input');input.className=className+' key-capture';input.type='text';
  // 只读才不会被输入法接走：开着中文输入法按 D，普通文本框会先弹候选框。
  input.readOnly=true;input.value=value;input.placeholder='点这里，再按键';
  input.title='点一下，再按想要的键；按住 CTRL 再按 W 就是 CTRL+W。浏览器自己占着的组合键（比如 CTRL+W 会关掉页面）录不进来';
  let chord=[],before='';const held=new Set();
  const settle=()=>{
    held.clear();if(!chord.length)return;chord=[];
    if(input.value!==before){input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}))}
  };
  input.addEventListener('keydown',e=>{
    // 不往外传：F9 是全局急停，这里按 F9 是想绑 F9。
    e.preventDefault();e.stopPropagation();if(e.repeat)return;
    const name=keyNameFromCode(e.code);
    if(!name){keyCaptureTip(input,`「${e.code.startsWith('Numpad')?'小键盘 '+e.key:e.key}」这个键还不支持，换一个`);return}
    if(!chord.length)before=input.value;
    held.add(e.code);
    // 修饰键也看事件上的标志：远程桌面、按键精灵这类发来的 CTRL+S 可能只有带着
    // ctrlKey 的 S，前面没有单独的一下 CTRL。
    const flags=[['ctrlKey','CTRL'],['shiftKey','SHIFT'],['altKey','ALT'],['metaKey','WIN']].filter(([flag])=>e[flag]).map(([,key])=>key);
    const adding=[...flags,name].filter((key,at,all)=>!chord.includes(key)&&all.indexOf(key)===at);
    if(!adding.length)return;
    if(chord.length+adding.length>4){keyCaptureTip(input,'组合键最多 4 个键');return}
    chord.push(...adding);
    input.value=[...KEY_MODIFIERS.filter(k=>chord.includes(k)),...chord.filter(k=>!KEY_MODIFIERS.includes(k))].join('+');
  });
  input.addEventListener('keyup',e=>{e.preventDefault();e.stopPropagation();held.delete(e.code);if(!held.size)settle()});
  // 按 WIN 会弹开始菜单，松开那一下不一定回得到这里；焦点一走就按已经按下的算。
  input.addEventListener('blur',settle);
  return input;
}
function fillTargetControl(container,type,value='',comboLeadMs=80,comboLeadExplicit=false){
  container.replaceChildren();if(!type)return;
  const meta=gameProfile.actions?.[type]||{};
  if(type==='gamepad'){
    const select=document.createElement('select');select.className='binding-target';
    for(const key of meta.targets||['A','B','X','Y','LB','RB','L3','R3','START','BACK','DPAD_UP','DPAD_DOWN','DPAD_LEFT','DPAD_RIGHT']){const option=document.createElement('option');option.value=key;option.textContent=TARGET_LABELS[key]||key;select.appendChild(option)}
    const custom=document.createElement('option');custom.value='__combo__';custom.textContent='组合键…';select.appendChild(custom);
    const raw=Array.isArray(value)?value.join('+'):String(value||'A');
    // Tick the parts instead of typing "LB+LS_UP": the valid names are a fixed
    // set, and a typo here only surfaces as a rejected save. Stick directions
    // are offered alongside the buttons because a combo may drive both.
    const chosen=new Set(raw.split('+').map(part=>part.trim().toUpperCase()).filter(Boolean));
    const picker=document.createElement('div');picker.className='combo-picker';
    const combo=document.createElement('input');combo.type='hidden';
    const hasStick=()=>[...picker.querySelectorAll('input:checked')].some(box=>GAMEPAD_STICK_TARGETS.includes(box.value));
    const sync=()=>{combo.value=[...picker.querySelectorAll('input:checked')].map(box=>box.value).join('+');if(select.value==='__combo__')leadBox.hidden=!hasStick()};
    const comboTargets=meta.combo_targets||[...(meta.targets||[]),...GAMEPAD_STICK_TARGETS,...GAMEPAD_TRIGGER_TARGETS];
    for(const key of [...new Set(comboTargets)]){
      const label=document.createElement('label');const box=document.createElement('input');
      box.type='checkbox';box.value=key;box.checked=chosen.has(key);box.addEventListener('change',sync);
      label.append(box,document.createTextNode(TARGET_LABELS[key]||key));picker.appendChild(label);
    }
    const leadBox=document.createElement('label');leadBox.className='combo-lead-box';leadBox.textContent='按键领先摇杆';
    const lead=document.createElement('input');lead.type='range';lead.className='combo-lead-ms';lead.min='0';lead.max='200';lead.step='5';lead.value=String(Math.max(0,Math.min(200,Number(comboLeadMs)||0)));
    lead.dataset.explicit=comboLeadExplicit?'1':'0';
    const leadValue=document.createElement('span');leadValue.className='combo-lead-value';leadValue.textContent=`${lead.value} 毫秒`;
    lead.addEventListener('input',()=>{lead.dataset.touched='1';leadValue.textContent=`${lead.value} 毫秒`});leadBox.append(lead,leadValue);
    const update=()=>{const isCombo=select.value==='__combo__';select.className=isCombo?'binding-gamepad-select':'binding-target';combo.className=isCombo?'binding-target':'';picker.hidden=!isCombo;leadBox.hidden=!isCombo||!hasStick()};
    select.value=[...select.options].some(o=>o.value===raw)?raw:'__combo__';
    sync();select.addEventListener('change',update);update();container.append(select,picker,combo,leadBox);return;
  }
  if(type==='voice_release'){
    const select=document.createElement('select');select.className='binding-target voice-release-target';
    fillVoiceReleaseSelect(select,container.dataset.trigger||'',value);
    container.appendChild(select);return;
  }
  if(type==='system'){
    const select=document.createElement('select');select.className='binding-target';
    const allowed=new Set(meta.targets||BINDING_SYSTEM_TARGETS.map(([id])=>id));
    for(const[id,name]of BINDING_SYSTEM_TARGETS){if(!allowed.has(id))continue;const o=document.createElement('option');o.value=id;o.textContent=name;select.appendChild(o)}
    const want=String(value||'').toUpperCase();
    if(want&&[...select.options].some(o=>o.value===want))select.value=want;
    container.appendChild(select);return;
  }
  if(type==='macro'){
    const select=document.createElement('select');select.className='binding-target';
    if(!macroLibrary.items.length){
      const o=document.createElement('option');o.value='';o.textContent='还没有宏——到「通用设置 · 键盘宏」里建一条';
      select.appendChild(o);select.disabled=true;container.appendChild(select);return;
    }
    for(const macro of macroLibrary.items){const o=document.createElement('option');o.value=macro.id;o.textContent=macro.repeat?`${macro.name}（循环）`:macro.name;select.appendChild(o)}
    const want=String(value||'').toLowerCase();
    // 指向一条已经删掉的宏时，照实说。默默换成第一条会让人以为自己记错了。
    if(want&&![...select.options].some(o=>o.value===want)){const o=document.createElement('option');o.value=want;o.textContent='宏已丢失';select.appendChild(o)}
    select.value=want||macroLibrary.items[0].id;
    container.appendChild(select);return;
  }
  if(meta.free_text){container.appendChild(makeKeyCaptureInput('binding-target',Array.isArray(value)?value.join('+'):(value||'')));return}
  const select=document.createElement('select');select.className='binding-target';
  for(const target of meta.targets||[]){const o=document.createElement('option');o.value=target;o.textContent=TARGET_LABELS[target]||target;select.appendChild(o)}
  if(value&&[...select.options].some(o=>o.value===value))select.value=value;container.appendChild(select);
}
function fillBehaviorControl(container,trigger,type,value,macroId){
  container.replaceChildren();
  if(type==='macro'){
    // 「跑一遍还是按住时循环」是宏自己的设定，在宏库那边改。同一个东西两处能改，
    // 就一定会有一处是旧的，所以这里只显示结果。
    const macro=macroById(macroId);
    const mode=macro?.repeat?'hold':'tap';
    const text=!macro?'—':macro.repeat?'按住时循环':'触发时跑一遍';
    if(trigger.group!=='voice'){
      const span=document.createElement('span');span.className='binding-behavior';
      span.dataset.value=mode;span.textContent=text;container.appendChild(span);return;
    }
    // 循环的宏靠另一个动作停住：那个动作选「停住语音按住」，再选这句口令。
    const span=document.createElement('span');span.className='binding-behavior';
    span.dataset.value=mode;span.textContent=text;container.appendChild(span);return;
  }
  if(type==='voice_release'){const span=document.createElement('span');span.className='binding-behavior';span.textContent='触发时停住一次';span.dataset.value='tap';container.appendChild(span);return}
  if(type==='system'){const span=document.createElement('span');span.className='binding-behavior';span.textContent='触发时执行一次';span.dataset.value='tap';container.appendChild(span);return}
  if(trigger.tapOnly||type==='mouse_wheel'){const span=document.createElement('span');span.className='binding-behavior';span.textContent='进入时触发一次';span.dataset.value='tap';container.appendChild(span);return}
  if(!type){const span=document.createElement('span');span.className='binding-behavior';span.textContent='—';span.dataset.value='hold';container.appendChild(span);return}
  const sel=document.createElement('select');sel.className='binding-behavior';
  for(const[v,t]of (trigger.group==='voice'?[['tap','点按'],['hold','持续按住']]:[['hold','保持动作时持续'],['tap','进入时触发一次']])){const o=document.createElement('option');o.value=v;o.textContent=t;sel.appendChild(o)}
  // Poses default to a single edge trigger on the server, so show that rather
  // than "hold" while a pose has no binding yet.
  const edgeDefault=trigger.group==='voice'||trigger.group==='poses';
  sel.value=trigger.group==='voice'?(['tap','hold'].includes(value)?value:'tap'):(value==='tap'||(!value&&edgeDefault)?'tap':'hold');container.appendChild(sel);
}
// 身体动作那一组只列这个游戏用着的：配置里绑了键的（默认就绑了原地踏步、小腿向后
// 抬起两个），加上这次从动作库点进来的。其余的在下面「动作库」「自定义动作」里挑，
// 点卡片上的「加到映射」就进来——十来行「不映射」堆在表里，要找的那一行反而看不见。
// 列出来过的行在同一个游戏里一直留着：改成「不映射」那一下就消失，人会以为没改上。
const shownBodyRows={profile:null,keys:new Set()};
function isBodyTrigger(trigger){return trigger.group==='motions'||trigger.group==='poses'}
function bodyRowWanted(trigger){
  const binding=bindingFor(trigger);
  return !!(binding&&!binding.disabled)||shownBodyRows.keys.has(trigger.key);
}
// 卡片上那个键位按钮：绑了写键，表里有这一行但没绑写「未映射」，表里还没有写「加到映射」。
function libraryKeyLabel(triggerKey){
  const label=triggerKeyLabel(triggerKey);
  return label==='未映射'&&!shownBodyRows.keys.has(triggerKey)?'加到映射':label;
}
// 旧的「松开同一语音按键」：那句口令自己又选了一遍同样的键。按键对上哪条持续按住
// 的口令，就显示成「停住语音按住 · 那条口令」；存的还是旧写法，改这一行时才换成新的。
function legacyReleaseTarget(action){
  const same=(a,b)=>{const norm=x=>(Array.isArray(x)?x:String(x||'').split('+')).map(v=>String(v).trim().toUpperCase()).filter(Boolean).sort().join('+');return norm(a)===norm(b)};
  const bindings=bindingsForDisplay();
  for(const[key,binding]of Object.entries(bindings)){
    const other=binding?.action;
    if(!key.startsWith('voice.')||binding?.disabled||!other||other.behavior!=='hold')continue;
    if(other.type===action.type&&(other.type==='macro'?String(other.target).toLowerCase()===String(action.target).toLowerCase():same(other.target,action.target)))return voiceCommandId(key);
  }
  return '';
}
function buildBindingRow(trigger){
  const binding=bindingFor(trigger);
  let action=binding?.disabled?null:binding?.action;
  if(action?.behavior==='release')action={type:'voice_release',target:legacyReleaseTarget(action),behavior:'tap'};
  const row=document.createElement('div');row.className='binding-row';row.dataset.trigger=trigger.key;
  const name=document.createElement('div');name.className='trigger-name';name.textContent=trigger.name;
  if(trigger.group==='voice'){
    const phrase=document.createElement('input');phrase.className='voice-trigger-phrase';phrase.type='text';phrase.value=binding?.phrase||trigger.phrase||'';phrase.placeholder='例如：体感地图';phrase.title='说出的完整口令';name.replaceChildren(document.createTextNode(trigger.name),phrase);watchVoicePhrase(phrase);
  }
  const type=makeTypeSelect(action?{action}:binding);
  const target=document.createElement('div');target.className='binding-target-box';target.dataset.trigger=trigger.key;fillTargetControl(target,type.value,action?.target??'',action?.combo_stick_lead_ms??80,action?.combo_stick_lead_ms!=null);
  const pickedMacro=()=>target.querySelector('.binding-target')?.value||'';
  const behavior=document.createElement('div');behavior.className='binding-behavior-box';fillBehaviorControl(behavior,trigger,type.value,action?.behavior||(trigger.group==='voice'?'tap':'hold'),action?.target||'');
  type.addEventListener('change',()=>{fillTargetControl(target,type.value,'',80);fillBehaviorControl(behavior,trigger,type.value,trigger.group==='voice'?'tap':'hold',pickedMacro());syncMotionConflictChoices()});
  // 换了另一条宏，「跑一遍还是循环」要跟着那条宏重算——那一格写的必须是现在
  // 选中这条的，否则界面说一套、实际跑另一套。
  target.addEventListener('change',()=>{if(type.value==='macro')fillBehaviorControl(behavior,trigger,'macro',behavior.querySelector('.binding-behavior')?.value,pickedMacro())});
  row.append(name,type,target,behavior);
  row.querySelectorAll('input,select').forEach(control=>control.setAttribute('aria-label',trigger.name+' '+(control.className.includes('type')?'输出类型':'键位或触发方式')));
  if(trigger.group==='motions'){const note=document.createElement('div');note.className='motion-conflict-note';note.hidden=true;row.appendChild(note)}
  return row;
}
function syncBodyGroup(){
  const details=document.querySelector('.binding-group[data-group="body"]');if(!details)return;
  const hidden=profileTriggers().filter(t=>isBodyTrigger(t)&&!shownBodyRows.keys.has(t.key)).length;
  details.querySelector('summary').textContent=`身体动作 · ${details.querySelectorAll('.binding-row').length} 项`;
  const more=details.querySelector('.binding-group-more');more.hidden=!hidden;
  more.querySelector('span').textContent=`还有 ${hidden} 个动作没放进来：在下面「动作库」「自定义动作」的卡片上点「加到映射」`;
}
// 从动作库、自定义动作、动作测试点过来，而表里还没有这一行：现加一行，不整表重画——
// 重画会冲掉别的行里还没存的改动。
function addBodyRow(triggerKey){
  const trigger=profileTriggers().find(t=>t.key===triggerKey&&isBodyTrigger(t));
  const rows=document.querySelector('.binding-group[data-group="body"] .binding-group-rows');
  if(!trigger||!rows)return null;
  const row=buildBindingRow(trigger);rows.appendChild(row);shownBodyRows.keys.add(trigger.key);
  syncBodyGroup();syncMotionConflictChoices();syncVoiceReleaseChoices();
  return row;
}
function renderProfileBindingRows(){
  const box=$('#profileBindingRows');if(!box)return;box.replaceChildren();
  if(!gameProfile.selected){box.innerHTML='<div class="profile-empty">还没有可编辑的游戏配置。</div>';return}
  const profileId=gameProfile.selected.selected_id||gameProfile.selected.id;
  if(shownBodyRows.profile!==profileId){shownBodyRows.profile=profileId;shownBodyRows.keys.clear()}
  const triggers=profileTriggers();
  const groups=[
    {id:'zones',title:'身体区域',help:'手、脚或头部进入对应区域时触发',filter:t=>t.group==='zones',open:true},
    {id:'body',title:'身体动作',help:'识别到动作时触发；开合跳与双手举过头不能同时映射',filter:isBodyTrigger,open:true},
    {id:'voice',title:'本游戏口令',help:'只在这个游戏里生效，会跟着配置一起分享。说法不能和通用口令、内置口令重复。',filter:t=>t.group==='voice',open:false},
  ];
  for(const group of groups){
    const all=triggers.filter(group.filter);if(!all.length)continue;
    const items=group.id==='body'?all.filter(bodyRowWanted):all;
    const details=document.createElement('details');details.className='binding-group';details.dataset.group=group.id;details.open=group.open;
    const summary=document.createElement('summary');summary.textContent=`${group.title} · ${items.length} 项`;
    const help=document.createElement('div');help.className='binding-group-help';help.textContent=group.help;
    const rows=document.createElement('div');rows.className='binding-group-rows';
    for(const trigger of items)rows.appendChild(buildBindingRow(trigger));
    details.append(summary,help,rows);box.appendChild(details);
    if(group.id==='body'){
      for(const trigger of items)shownBodyRows.keys.add(trigger.key);
      const more=document.createElement('div');more.className='binding-group-more';
      const go=document.createElement('button');go.type='button';go.className='btn';go.textContent='去动作库';
      go.addEventListener('click',()=>$('#poseLibraryPanel')?.scrollIntoView({behavior:'smooth',block:'start'}));
      more.append(document.createElement('span'),go);details.appendChild(more);
      syncBodyGroup();
    }
  }
  syncMotionConflictChoices();
  syncVoiceReleaseChoices();
  paintPoseMissingNotice();
}
function readProfileOverrides(){
  const overrides=structuredClone(gameProfile.overrides);
  for(const trigger of profileTriggers()){
    if(!profileDirty.has(trigger.key))continue;
    const row=document.querySelector(`.binding-row[data-trigger="${trigger.key}"]`);if(!row)continue;
    const type=row.querySelector('.binding-type')?.value||'';
    if(!type){overrides[trigger.key]=null;continue}
    let target;
    if(type==='voice_release'){
      const ids=voiceReleaseTargetIds(row.querySelector('.voice-release-target')).map(id=>id.toLowerCase());
      target=ids.length===1?ids[0]:ids;
    }else{
      const raw=String(row.querySelector('.binding-target')?.value||'').trim();
      target=type==='macro'?raw.toLowerCase():raw.toUpperCase();
    }
    if(!target||(Array.isArray(target)&&!target.length))throw new Error(type==='macro'?`${trigger.name} 还没有选择要跑哪条宏`:type==='voice_release'?`${trigger.name} 还没有选择要停住哪条口令`:`${trigger.name} 还没有选择具体键位`);
    const behavior=trigger.tapOnly||type==='mouse_wheel'||type==='voice_release'||type==='system'?'tap':(row.querySelector('select.binding-behavior')?.value||row.querySelector('.binding-behavior')?.dataset.value||'hold');
    const override={action:{type,target,behavior}};
    const comboLead=row.querySelector('.combo-lead-ms');
    if(type==='gamepad'&&comboLead&&!comboLead.closest('.combo-lead-box')?.hidden&&(comboLead.dataset.explicit==='1'||comboLead.dataset.touched==='1')){
      override.action.combo_stick_lead_ms=Math.max(0,Math.min(200,Number(comboLead.value)||0));
    }
    if(trigger.group==='voice'){
      const phrase=row.querySelector('.voice-trigger-phrase')?.value.trim();
      if(!phrase)throw new Error(`${trigger.name} 还没有填写触发词`);
      override.phrase=phrase;
    }
    overrides[trigger.key]=override;
  }
  const conflicts=motionConflictsForSelection(selectedMotionIdsFromRows());
  if(conflicts.length)throw new Error(`动作冲突：${motionConflictText(conflicts)}。开合跳与双手举过头只能选择一个`);
  return overrides;
}
async function saveProfileBindings(){
  clearTimeout(profileAutoSaveTimer);
  if(profileFlight){await profileFlight;return saveProfileBindings()}
  if(!profileDirty.size)return;
  const id=gameProfile.selected.selected_id||gameProfile.selected.id,revision=profileRevision;
  $('#profileSaveStatus').textContent='正在保存…';$('#retryProfileSaveBtn').hidden=true;
  profileFlight=(async()=>{
    const data=await post('/api/game-profiles/overrides',{profile_id:id,overrides:readProfileOverrides()});
    gameProfile.selected=data.profile;gameProfile.overrides=data.profile.overrides||{};
    if(revision===profileRevision)profileDirty.clear();
    renderProfileHeader();await refreshVoiceCommands();
  })();
  try{await profileFlight}
  catch(error){
    profileConflict=error.status===409;
    $('#profileSaveStatus').textContent='保存失败，草稿已保留：'+error.message;
    $('#retryProfileSaveBtn').textContent=profileConflict?'重新选择本游戏并保存草稿':'重试保存映射';
    $('#retryProfileSaveBtn').hidden=false;throw error;
  }finally{profileFlight=null}
  if(profileDirty.size)return saveProfileBindings();
  profileConflict=false;
  $('#profileSaveStatus').textContent='已自动保存';
}
async function retryProfileBindings(){
  if(!profileConflict)return saveProfileBindings();
  await profileOperation(async()=>{
    const id=gameProfile.selected.selected_id||gameProfile.selected.id;
    await post('/api/game-profiles/select',{id});
    profileConflict=false;
    await saveProfileBindings();
  });
}
function scheduleProfileAutoSave(event){
  // Text inputs already saved their input event; blur must not restart a failed save
  // or move its retry button between pointer-down and pointer-up.  A checkbox has
  // no input event to rely on, so its change is the only signal it ever sends.
  if(event?.type==='change'&&event.target.matches('input:not([type=checkbox])'))return;
  const row=event?.target.closest('.binding-row');if(!row||profileSwitching)return;
  profileDirty.add(row.dataset.trigger);profileRevision++;
  clearTimeout(profileAutoSaveTimer);
  $('#profileSaveStatus').textContent='有待保存的修改';
  profileAutoSaveTimer=setTimeout(()=>saveProfileBindings().catch(()=>{}),350);
}
async function resetProfileBindings(){
  if(profileSwitching)return;
  await profileOperation(async()=>{
    await saveProfileBindings();
    const profile_id=gameProfile.selected.selected_id||gameProfile.selected.id;
    const data=await post('/api/game-profiles/overrides',{profile_id,overrides:{}});
    gameProfile.selected=data.profile;gameProfile.overrides={};
    // 恢复默认就是回到默认那几行，这次手动加进来的没绑的动作一起收回动作库。
    shownBodyRows.keys.clear();
    await refreshVoiceCommands();renderProfileHeader();renderProfileBindingRows();
    $('#profileSaveStatus').textContent='当前游戏已恢复默认';
  });
}
function formatPerf(value,suffix=''){return value===null||value===undefined||value===''?'—':`${value}${suffix}`}
function renderPerformance(data){
  $('#perfSummary').textContent='实时诊断 · 仅在本面板展开时刷新';
  $('#perfDetails').textContent=[
    `采集帧率：${formatPerf(data.capture_fps)} · 推理帧率：${formatPerf(data.inference_fps)}`,
    `总延迟：${formatPerf(data.total_latency_ms,' 毫秒')} · 推理平均：${formatPerf(data.inference_avg_ms,' 毫秒')}`,
    `预览：${data.preview_ready?'已就绪':'未就绪'} · 丢帧：${data.dropped_frames??0}`,
  ].join('\n');
}

async function refreshPerformance(){try{renderPerformance(await api('/api/performance'))}catch{}}
async function refreshCameraConfig(){try{const data=await api('/api/camera/config');const select=$('#cameraBackend');if(select&&data.preference)select.value=data.preference;renderCameraDevices(null,data.camera_index)}catch{}}

// 开机不扫。挨个序号去开摄像头要好几秒，而绝大多数人只有一个，不该为了那个
// 下拉框每次启动都等一遍。所以先只把"现在用的是第几个"摆出来，真要换的人点
// 一下扫描，列表才填满。
function renderCameraDevices(devices,current){
  const select=$('#cameraDevice');if(!select)return;
  if(current!==undefined&&current!==null)cameraIndex=Number(current);
  const list=devices&&devices.length?devices:[{index:cameraIndex,width:0,height:0}];
  select.innerHTML=list.map(d=>{
    const size=d.width&&d.height?` · ${d.width}×${d.height}`:'';
    return `<option value="${d.index}">摄像头 ${d.index}${size}</option>`;
  }).join('');
  select.value=String(cameraIndex);
  if(select.value!==String(cameraIndex))select.selectedIndex=0;
}
function syncCameraDeviceRow(){
  const computer=($('#poseSource')?.value||'computer')==='computer';
  for(const id of ['cameraDeviceRow','cameraScanRow']){const el=$('#'+id);if(el)el.hidden=!computer}
}
// --- hand mouse -----------------------------------------------------------
// The fist thresholds shipped as estimates rather than measurements, so the
// live reading sits next to them: open the hand, read the number, close it,
// read again, then put the thresholds between the two.
//
// Which reading, and therefore which pair of sliders, depends on what the
// phone is sending.  With finger joints the number is how far the fingertips
// reach past their knuckles; without them it is the coarse fingertip spread.
// Showing both pairs at once would leave the player tuning whichever one
// happens to do nothing.
function fillViewControlOptions(){
  for(const [id,items] of [['viewHorizontalSource',VIEW_CONTROL_CONTENT.horizontal],['viewVerticalSource',VIEW_CONTROL_CONTENT.vertical]]){
    const select=$('#'+id);select.replaceChildren(...items.map(item=>new Option(item.label,item.value)));
  }
}
function viewChoice(items,value){return items.find(item=>item.value===value)||items.at(-1)}
function renderViewControl(force=false){
  if(viewControlSaving||(!force&&document.activeElement?.matches('#viewHorizontalSource,#viewVerticalSource')))return;
  const horizontalHand=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.horizontal_hand)?handMouseConfig.horizontal_hand:null;
  const verticalHand=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.vertical_hand)?handMouseConfig.vertical_hand:null;
  const horizontal=horizontalHand||(head.enabled?(head.horizontalAlgorithm==='roll_tilt'?'roll_tilt':'head_turn'):'off');
  const vertical=verticalHand||(head.verticalLookEnabled?'legacy':'off');
  $('#viewVerticalSource option[value="legacy"]').hidden=!head.verticalLookEnabled;
  $('#viewHorizontalSource').value=horizontal;$('#viewVerticalSource').value=vertical;
  const horizontalInfo=viewChoice(VIEW_CONTROL_CONTENT.horizontal,horizontal),verticalInfo=viewChoice(VIEW_CONTROL_CONTENT.vertical,vertical);
  const rows=[['左右：'+horizontalInfo.label,horizontalInfo.description],['上下：'+verticalInfo.label,verticalInfo.description]];
  if(head.verticalLookEnabled)rows.push(['更多设置',VIEW_CONTROL_CONTENT.legacyVerticalNote]);
  $('#viewControlDescription').replaceChildren(...rows.map(([title,text])=>{const p=document.createElement('p');const b=document.createElement('b');b.textContent=title+' ';p.append(b,text);return p}));
  const status=$('#viewControlStatus');if(viewControlReady&&status.textContent==='正在读取当前设置…')status.textContent='当前设置已读取';
}
// 教学只看它自己那几件事，所以单独凑一份快照而不是把整个 kernelState 丢过去：
// 判定写在 tutorial.js 里，字段名要是换了这边会直接报错，而不是悄悄一直不亮。
// 教学只看它自己那几件事，所以单独凑一份快照而不是把整个 kernelState 丢过去：
// 判定写在 tutorial.js 里，字段名要是换了这边会直接报错，而不是悄悄一直不亮。
// 教学只看它自己那几件事，所以单独凑一份快照而不是把整个 kernelState 丢过去：
// 判定写在 tutorial.js 里，字段名要是换了这边会直接报错，而不是悄悄一直不亮。
function tutorialState(){
  const hs=kernelState?.head||{},k=kernelState||{},pose=currentPoseMap||{};
  const hand=hs.hand_mouse||{},axes=hand.axes||{};
  const handOf=axis=>handMouseConfig.enabled&&['left','right'].includes(handMouseConfig[axis+'_hand'])?handMouseConfig[axis+'_hand']:null;
  const horizontalHand=handOf('horizontal'),verticalHand=handOf('vertical');
  // 「有画面」要真的有帧在来，不是摄像头开关打开了就算。
  const frameAge=cameraInfo?.last_frame_age_ms;
  const computerLive=cameraRunning&&Number(cameraInfo?.frames)>0&&frameAge!=null&&frameAge<3000;
  const phoneLive=!!inputStatus.mobile_pose_connected;
  const seen=name=>Number(pose[name]?.score??pose[name]?.visibility??0)>=.5;
  const finite=v=>Number.isFinite(Number(v))&&v!==null&&v!==undefined;
  // 各方案的读数量纲不一样，这里统一折算成 -1…1（满量程）再交给教学：
  // 头控左右的读数上限是左右灵敏度；握拳的上限是握拳灵敏度/100；老的上下方案
  // 用头时上限是上下灵敏度/100，用手时本来就是 -1…1。
  const handMax=Math.min(1,Math.max(.01,Number(handMouseConfig.sensitivity??hand.config?.sensitivity??70)/100));
  const hLevel=horizontalHand?(finite(axes.horizontal?.output)?Number(axes.horizontal.output)/handMax:null)
    :(finite(hs.output_x)?Number(hs.output_x)/Math.max(1,Number(hs.sensitivity_x||head.sensitivityX||58)):null);
  const legacyMax=head.verticalLookSource==='head'?clamp(Number(hs.sensitivity_y||head.sensitivityY||46)/100,.15,1):1;
  const vLevel=verticalHand?(finite(axes.vertical?.output)?Number(axes.vertical.output)/handMax:null)
    :(head.verticalLookEnabled&&finite(hs.output_y)?Number(hs.output_y)/legacyMax:null);
  const events=k.recent_triggers||[],last=events[events.length-1];
  const lastTrigger=last?{key:String(last.trigger||''),at:Number(last.at),keyText:actionKeyText(last.action)||'',
    name:(profileTriggers().find(t=>t.key===String(last.trigger||''))||{}).name||String(last.trigger||'')}:null;
  const vs=voice.status||{};
  // 语音为什么不能用，只分成人能对付的几种：模型不在、麦克风打不开、麦克风没声音；状态还没回来时算「准备中」。
  const voiceIssue=!voice.status?'starting':!vs.available||!vs.model_ready?'model'
    :!vs.connected||!vs.audio_ready?'mic':!(vs.audio_alive||vs.stream_alive)?'silent':'starting';
  const zones=Object.entries(BODY_ZONES).filter(([,def])=>!def.gate).map(([id,def])=>{
    const z=k.zones?.[id]||{},key=zoneKeyLabel(id,def);
    return {id,body:def.body,key:key==='未映射'?'':key,shown:!!z.rect,pressed:!!z.pressed};
  });
  return {
    view:currentView,source:sourceMode,sourcePick:$('#poseSource')?.value||sourceMode,
    cameraReady:sourceMode==='phone'?phoneLive:computerLive,
    cameraRunning,cameraIndex:cameraInfo?.camera_index??cameraIndex,cameraError:cameraRunning?'':String(cameraInfo?.last_error||''),
    // 模型路径随第一次状态一起来；还没来之前是「不知道」，不能当成「没装」。
    modelOk:cameraInfo?!!cameraInfo.model_path:null,
    scan:cameraScan.state,scanCount:cameraScan.count,
    phoneStreaming:phoneLive||!!inputStatus.phone_ignored,usbTether:!!inputStatus.usb_tether?.present,
    posed:!!currentPoseMap,headShoulders:seen('nose')&&seen('left_shoulder')&&seen('right_shoulder'),
    // 握拳控左右不需要头部中心，这一点和「视角控制」那块的判断保持一致。
    calibrated:horizontalHand?true:!!(hs.horizontal_calibrated??hs.calibrated),
    calibrating:!!hs.calibrating,calibrationNote:String(hs.notice||''),
    horizontal:horizontalHand||(head.enabled?(head.horizontalAlgorithm==='roll_tilt'?'roll_tilt':'head_turn'):'off'),
    hLevel,hHandState:String(axes.horizontal?.state||''),guardBlocked:!!hs.horizontal_paused_by_body_motion,
    vertical:verticalHand||(head.verticalLookEnabled?'legacy':'off'),
    vLevel,vHandState:String(axes.vertical?.state||''),gateActive:!!k.vertical_gate_active,legacySource:head.verticalLookSource,
    zones,
    outputEnabled:!!output.enabled,driverMissing:output.mode==='gamepad'&&output.server?.vigembus_running===false,
    stops:emergencyStops,
    // 「量身」：电脑那边量到哪一步了；握拳量哪几只手；固定区域时量的是跟随那一套。
    fit:k.zone_fit||{},fistHands:fistHands(),zonesFrozen:!!k.zones_frozen,
    // 「做了动作，游戏没反应」：最后打中的是什么、按的哪个键；时间用内核自己的钟比。
    kernelNow:Number(k.now),lastTrigger,
    // 「按键和我的游戏对不上」
    profileApplies,searchedFor:lastProfileQuery,catalogCount:gameProfile.catalog.length,
    gameName:gameProfile.selected?.name||'未选择游戏',
    // 自己加的游戏 source 是字符串 'custom'，不是 {verified}；它不该被说成「没人试过」。
    gameKind:gameProfile.selected?.source==='custom'?'custom':gameProfile.selected?.source?.verified?'verified':'auto',
    // 「手忙不过来，想用嘴说」：听到几句用计数比（同一句说两遍文字不变）；老版本服务没有
    // 这个数，就退回比最后一句。
    voiceReady:voiceInputReady,voiceIssue,voiceHeard:String(vs.commands_heard??vs.last_command??''),
    stopPhrase:(vs.emergency_stop_phrases||[])[0]||'体感紧急停止',voiceListOpen:!!$('#voiceCommandsMask')?.open,
  };
}
function initViewControl(){
  fillViewControlOptions();renderViewControl();
  // 弹不弹、弹哪个由教学自己定：从没看过就带着做基础；基础走完后的下一次打开，让人挑一次别的。
  tutorial.autoOpen();
}
function renderHandMouse(state){
  if(!state)return;
  const c=state.config||{};
  handMouseConfig={...handMouseConfig,...c};
  // 合流模式下摇杆归物理手柄，手控鼠标接不上任何东西——勾着不起作用比灰着更糟。
  const blocked=mergeOwnsSticks();
  $('#handMouseEnabled').disabled=blocked||viewControlSaving;
  $('#handMouseEnabled').checked=Boolean(c.enabled);
  renderOutputMix();
  $('#handMouseHorizontalHand').value=c.horizontal_hand||'off';
  $('#handMouseVerticalHand').value=c.vertical_hand||'left';
  for(const [id,value] of [['handMouseSensitivity',c.sensitivity],['handMouseDeadzone',c.deadzone],['handMouseClose',c.fist_close],['handMouseOpen',c.fist_open],['handMouseCurlClose',c.curl_close],['handMouseCurlOpen',c.curl_open]]){
    if(value!==undefined)$('#'+id).value=value;
  }
  $('#handMouseSensitivityValue').textContent=Number(c.sensitivity||0).toFixed(0);
  $('#handMouseDeadzoneValue').textContent=Number(c.deadzone||0).toFixed(2);
  $('#handMouseCloseValue').textContent=Number(c.fist_close||0).toFixed(2);
  $('#handMouseOpenValue').textContent=Number(c.fist_open||0).toFixed(2);
  $('#handMouseCurlCloseValue').textContent=Number(c.curl_close||0).toFixed(2);
  $('#handMouseCurlOpenValue').textContent=Number(c.curl_open||0).toFixed(2);
  const readings=Object.values(state.axes||{});
  const byHand=readings.some(s=>s.grip_source==='hand');
  $('#handMouseCurlRow').hidden=!byHand;
  $('#handMouseSpreadRow').hidden=readings.length>0&&readings.every(s=>s.grip_source==='hand');
  const reading=byHand
    ?(state.curl==null?'看不到手':`手指伸展 ${Number(state.curl).toFixed(2)}`)
    :(state.spread==null?'看不到手':`张开度 ${Number(state.spread).toFixed(3)}`);
  const axisLabel=(axis,name)=>{const s=state.axes?.[axis];if(!s)return '';const hand={left:'左手',right:'右手',off:'关闭'}[s.hand]||'关闭';const phase=({disabled:'未启用',idle:'待机',open:'手张开',engaged:'已握拳',moving:'握拳移动中',opened:'刚松开',lost:'看不到手'})[s.state]||s.state;const measure=s.grip_source==='hand'?`手指伸展 ${Number(s.curl).toFixed(2)}`:s.spread==null?'看不到手':`张开度 ${Number(s.spread).toFixed(3)}`;return `${name}：${hand} · ${phase} · ${measure}`;};
  const label={disabled:'未启用',idle:'待机',open:'手张开',engaged:'已握拳',moving:'握拳移动中',opened:'刚松开',lost:'看不到手'}[state.state]||state.state;
  $('#handMouseStatus').textContent=blocked?'物理手柄合流占着摇杆，手控鼠标用不了':(c.enabled
    ?(state.axes?`${axisLabel('horizontal','水平')}；${axisLabel('vertical','垂直')}`:`${label} · ${reading} · 输出 ${Number(state.output_x||0).toFixed(2)} / ${Number(state.output_y||0).toFixed(2)}`)
    :'未启用');
  renderViewControl();
}
// --- skeleton recording ---------------------------------------------------
// Polls only while something is actually happening, so an idle settings page
// is not making a request every second for nothing.
let poseRecordTimer=null;
function renderPoseRecord(state){
  if(!state)return;
  const label={
    idle:'未录制',waiting:`倒计时 ${state.remaining_s.toFixed(1)} 秒`,
    recording:`录制中 ${state.remaining_s.toFixed(1)} 秒 · 已 ${state.frames} 帧`,
    saving:'正在保存…',cancelled:'已取消',
    done:`已保存 ${state.frames} 帧 → ${state.file}`,
    error:state.error||'录制失败',
  }[state.state]||state.state;
  $('#poseRecordStatus').textContent=label;
  const busy=state.state==='waiting'||state.state==='recording'||state.state==='saving';
  $('#poseRecordBtn').disabled=busy;
  if(busy&&poseRecordTimer==null){
    poseRecordTimer=window.setInterval(()=>void refreshPoseRecord(),400);
  }else if(!busy&&poseRecordTimer!=null){
    window.clearInterval(poseRecordTimer);poseRecordTimer=null;
  }
}
async function refreshPoseRecord(){try{const data=await api('/api/pose/record');renderPoseRecord(data.recording)}catch{}}
async function startPoseRecord(){
  try{
    const data=await post('/api/pose/record',{delay_s:3,duration_s:15});
    renderPoseRecord(data.recording);
  }catch(error){$('#poseRecordStatus').textContent=error.message||'无法开始录制'}
}
async function cancelPoseRecord(){
  try{
    const data=await post('/api/pose/record',{cancel:true});
    renderPoseRecord(data.recording);
  }catch(error){$('#poseRecordStatus').textContent=error.message||'取消失败'}
}
async function refreshHandMouse(){try{const data=await api('/api/hand-mouse/config');renderHandMouse(data.hand_mouse)}catch{}}
async function reloadViewControlState(){
  const [runtime,handData]=await Promise.all([api('/api/kernel/status'),api('/api/hand-mouse/config')]);
  renderKernelState(runtime,true);renderHandMouse(handData.hand_mouse);viewControlReady=true;
}
function setViewControlBusy(busy){
  viewControlSaving=busy;
  document.querySelectorAll('#headSettings input,#headSettings select,#headHorizontalAlgorithm').forEach(el=>el.disabled=busy);
  for(const id of ['viewHorizontalSource','viewVerticalSource'])$('#'+id).disabled=busy||!viewControlReady;
  $('#handMouseEnabled').disabled=busy||mergeOwnsSticks();
}
async function postViewHead(payload){return post('/api/head/config',payload)}
async function saveHandMouseFields(payload){
  if(viewControlSaving)return;
  setViewControlBusy(true);
  $('#handMouseSaveStatus').textContent='正在保存…';
  try{
    const data=await post('/api/hand-mouse/config',payload);
    renderHandMouse(data.hand_mouse);
    $('#handMouseSaveStatus').textContent='已保存';
  }catch(error){
    let refreshed=true;try{await reloadViewControlState()}catch{refreshed=false}
    $('#handMouseSaveStatus').textContent='保存失败，'+(refreshed?'已读取当前设置：':'暂时无法读取当前设置：')+(error.message||'请重试');
  }finally{setViewControlBusy(false);renderViewControl(true)}
}
async function saveViewControlAxis(axis){
  if(viewControlSaving||!viewControlReady)return;
  if(headSaver.pending()){notice('请等当前设置保存完成');renderViewControl(true);return}
  const desiredHorizontal=$('#viewHorizontalSource').value,desiredVertical=$('#viewVerticalSource').value;
  const currentHorizontal=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.horizontal_hand)?handMouseConfig.horizontal_hand:'off';
  const currentVertical=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.vertical_hand)?handMouseConfig.vertical_hand:'off';
  setViewControlBusy(true);headDirty=true;$('#viewControlStatus').textContent='正在保存…';
  try{
    if(axis==='horizontal'){
      if(desiredHorizontal==='roll_tilt'||desiredHorizontal==='head_turn'){
        if(currentHorizontal!=='off')await post('/api/hand-mouse/config',{horizontal_hand:'off',enabled:Boolean(handMouseConfig.enabled&&currentVertical!=='off')});
        await post('/api/head/config',{enabled:true,horizontal_algorithm:desiredHorizontal==='roll_tilt'?'roll_tilt':lastTurnAlgorithm});
      }else if(desiredHorizontal==='left'||desiredHorizontal==='right'){
        if(head.enabled)await post('/api/head/config',{enabled:false});
        await post('/api/hand-mouse/config',{enabled:true,horizontal_hand:desiredHorizontal,vertical_hand:currentVertical});
      }else{
        if(currentHorizontal!=='off')await post('/api/hand-mouse/config',{horizontal_hand:'off',enabled:Boolean(handMouseConfig.enabled&&currentVertical!=='off')});
        if(head.enabled)await post('/api/head/config',{enabled:false});
      }
    }else if(desiredVertical==='legacy'){
      $('#headSettings .view-control-more').open=true;
      $('#verticalLookSource').focus();
    }else if(desiredVertical==='left'||desiredVertical==='right'){
      if(head.verticalLookEnabled)await postViewHead({vertical_look_source:'off'});
      await post('/api/hand-mouse/config',{enabled:true,vertical_hand:desiredVertical,horizontal_hand:currentHorizontal});
    }else{
      if(currentVertical!=='off')await post('/api/hand-mouse/config',{vertical_hand:'off',enabled:Boolean(handMouseConfig.enabled&&currentHorizontal!=='off')});
      if(head.verticalLookEnabled)await postViewHead({vertical_look_source:'off'});
    }
    await reloadViewControlState();$('#viewControlStatus').textContent='已保存';
  }catch(error){
    let refreshed=true;try{await reloadViewControlState()}catch{refreshed=false}
    $('#viewControlStatus').textContent=`保存失败，${refreshed?'已恢复服务器当前设置':'当前设置无法重新读取'}：${error.message||'请重试'}`;
  }finally{setViewControlBusy(false);headDirty=false;renderViewControl(true)}
}
async function saveLegacyVertical(){
  if(viewControlSaving)return;
  const source=$('#verticalLookSource').value,currentVertical=['left','right'].includes(handMouseConfig.vertical_hand)?handMouseConfig.vertical_hand:'off';
  setViewControlBusy(true);headDirty=true;$('#viewControlStatus').textContent='正在保存…';
  try{
    if(source!=='off'&&currentVertical!=='off')await post('/api/hand-mouse/config',{vertical_hand:'off',enabled:Boolean(handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.horizontal_hand))});
    await postViewHead({vertical_look_source:source});
    await reloadViewControlState();$('#viewControlStatus').textContent='已保存';
  }catch(error){
    let refreshed=true;try{await reloadViewControlState()}catch{refreshed=false}
    $('#viewControlStatus').textContent=`保存失败，${refreshed?'已恢复服务器当前设置':'当前设置无法重新读取'}：${error.message||'请重试'}`;
  }finally{setViewControlBusy(false);headDirty=false;renderViewControl(true)}
}
async function saveAdvancedHandAxis(axis){
  const field=axis==='horizontal'?'horizontal_hand':'vertical_hand',value=$('#handMouse'+(axis==='horizontal'?'Horizontal':'Vertical')+'Hand').value;
  if(value!=='off'){
    $('#'+(axis==='horizontal'?'viewHorizontalSource':'viewVerticalSource')).value=value;
    await saveViewControlAxis(axis);return;
  }
  const other=axis==='horizontal'?handMouseConfig.vertical_hand:handMouseConfig.horizontal_hand;
  await saveHandMouseFields({[field]:'off',enabled:Boolean(handMouseConfig.enabled&&['left','right'].includes(other))});
}
async function saveHeadEnabled(){
  if(viewControlSaving)return;
  setViewControlBusy(true);headDirty=true;$('#headSaveStatus').textContent='正在保存…';
  try{
    await post('/api/head/config',{enabled:$('#headEnable').checked});
    await reloadViewControlState();$('#headSaveStatus').textContent='已保存';
  }catch(error){
    try{await reloadViewControlState()}catch{}
    $('#headSaveStatus').textContent='保存失败，已重新读取当前设置：'+(error.message||'请重试');
  }finally{setViewControlBusy(false);headDirty=false;renderViewControl(true)}
}
async function refreshPreview(){
  if(!cameraPreview||perfUi.previewBusy||sourceMode!=='computer'||!cameraRunning||currentView!=='play'||document.visibilityState!=='visible')return;
  perfUi.previewBusy=true;
  try{
    const response=await fetch(`/api/camera/preview.jpg?t=${Date.now()}`,{cache:'no-store',signal:AbortSignal.timeout(3000)});
    if(!response.ok)return;
    const blob=await response.blob(),url=URL.createObjectURL(blob),old=cameraPreview.src;
    cameraPreview.onload=()=>{cameraPreview.hidden=false;if(old?.startsWith('blob:'))URL.revokeObjectURL(old)};
    cameraPreview.onerror=()=>{cameraPreview.hidden=true;URL.revokeObjectURL(url);if(old?.startsWith('blob:'))URL.revokeObjectURL(old)};
    cameraPreview.src=url;
  }catch{}
  finally{perfUi.previewBusy=false}
}


function renderXinputStatus(s=output.xinputStatus){if(document.activeElement?.closest('#outputSettings'))return;const select=$('#xinputMerge'),line=$('#xinputStatus');if(!select||!line)return;const users=Array.isArray(s?.connected_users)?s.connected_users:[];const current=s?.enabled&&s?.selected_user!==null&&s?.selected_user!==undefined?String(s.selected_user):'';const values=[['','关闭体感合流']];for(const user of users)values.push([String(user),`物理手柄 ${Number(user)+1}`]);if(current&&!values.some(([v])=>v===current))values.push([current,`手柄 ${Number(current)+1}（未连接）`]);const keep=current&&values.some(([v])=>v===current);select.replaceChildren(...values.map(([value,label])=>{const o=document.createElement('option');o.value=value;o.textContent=label;return o}));select.value=keep?current:(s?.enabled?'':'');output.xinputEnabled=!!s?.enabled;output.xinputUser=s?.selected_user??null;output.xinputMotionLeft=!!s?.motion_left_enabled;renderXinputMotionLeft();line.textContent=!s?.enabled?'未启用物理手柄合流':(s?.connected?`物理手柄已合流 · 手柄 ${Number(s.active_user??s.selected_user)+1}`:'已启用，等待物理手柄连接');if(s?.last_error)line.textContent+=' · '+s.last_error;line.className='statusline '+(s?.connected?'ok':'')}
function renderXinputMotionLeft(){const box=$('#xinputMotionLeft');if(box){box.checked=output.xinputMotionLeft;box.disabled=!output.xinputEnabled||output.mode!=='gamepad'}}
async function setXinputMotionLeft(){try{output.server=await post('/api/output/xinput',{motion_left_enabled:!!$('#xinputMotionLeft')?.checked});renderOutput(output.server);await refreshXinput();notice(output.xinputMotionLeft?'体感左摇杆合成已开启，双方输入相加':'已关闭体感左摇杆合成')}catch(e){notice('左摇杆合成设置失败：'+(e?.message||e));await refreshXinput()}}
async function refreshXinput(){try{output.xinputStatus=await api('/api/output/xinput');renderXinputStatus(output.xinputStatus)}catch{}}
async function setXinputMerge(){const select=$('#xinputMerge');const value=select?.value||'';try{const data=await post('/api/output/xinput',{enabled:!!value,user:value===''?null:Number(value)});output.server=data;renderOutput(data);await refreshXinput();notice(value?`已选择物理手柄 ${Number(value)+1}；${output.xinputMotionLeft?'体感按键与左摇杆合成已开启':'体感只叠加手柄按键'}`:'已关闭物理手柄合流')}catch(e){notice('物理手柄合流失败：'+(e?.message||e));await refreshXinput()}}
function renderOutput(s=output.server){
  if(!s)return;
  outputConnected=true;
  output.server=s;output.enabled=!!s.enabled;output.mode=s.mode||output.mode;
  if(document.activeElement!==$('#outputMode'))$('#outputMode').value=output.mode;
  if(document.activeElement!==$('#strength')){
    const gain=output.mode==='gamepad'?s.gamepad_gain:Number(s.mouse_speed_x)/600;
    if(Number.isFinite(gain)&&gain>0)$('#strength').value=Math.round(gain*100);
  }
  $('#strengthValue').textContent=$('#strength').value+'%';
  $('#outputPill').textContent=output.enabled?'游戏控制已开启':'游戏控制已暂停';
  $('#outputPill').className='pill '+(output.enabled?'ok':'');
  $('#outputStatus').textContent=(voiceLatchText(s)?voiceLatchText(s)+' · 说松开才会放 · ':'')+`${s.mouse_available?'鼠标可用':'鼠标不可用'} · ${s.gamepad_connected?'虚拟手柄已连接':'虚拟手柄未连接'}`+(s.last_error?' · '+s.last_error:'');
  if(s.xinput_merge_enabled!==undefined){
    output.xinputEnabled=!!s.xinput_merge_enabled;output.xinputUser=s.xinput_selected_user??null;
    output.xinputMotionLeft=!!s.xinput_motion_left_enabled;renderXinputMotionLeft();
  }
  renderOutputMix();
  renderMainStatus();
}

// 一台电脑上只能有一套"当前输入设备"。体感这边一半出鼠标、一半出 Xbox，游戏
// 就会在两种按键提示之间来回跳；而全都出 Xbox 的时候，右摇杆推不动桌面鼠标，
// 握拳看上去像是坏了。两种都不报错，只是怎么弄都不对——所以得写出来。
function gamepadBindingCount(){
  const bindings=gameProfile.selected?.bindings||{};let n=0;
  for(const group of ['zones','poses','motions','voice']){
    for(const item of Object.values(bindings[group]||{})){
      const type=String(item?.action?.type||'');
      if(type.startsWith('gamepad')||type==='xinput_button')n++;
    }
  }
  return n;
}
function renderOutputMix(){
  const el=$('#outputMixWarn');if(!el)return;
  const handMouseOn=!!$('#handMouseEnabled')?.checked;
  const pads=gamepadBindingCount();
  let text='';
  if(output.mode==='gamepad'&&handMouseOn){
    text='视角输出是 Xbox 右摇杆，握拳控制的是摇杆，鼠标不会动。想让握拳控制鼠标，把视角输出改成「鼠标视角」。';
  }else if(output.mode==='mouse'&&pads>0){
    text=`视角走鼠标，但这个游戏还有 ${pads} 个动作输出 Xbox 按键，游戏会在手柄和键鼠提示之间来回切换。把视角输出改成 Xbox，或把这些动作改成键鼠按键。`;
  }
  el.hidden=!text;el.textContent=text;
}
async function refreshOutput(){
  const epoch=outputEpoch;
  try{const data=await api('/api/output-status');if(epoch===outputEpoch)renderOutput(data)}
  catch{$('#outputPill').textContent='控制状态无法确认';outputConnected=false;renderMainStatus()}
}
async function setOutput(enabled){
  if(enabled&&zoneEditMode)throw new Error('请先保存或取消区域调整');
  const epoch=++outputEpoch;
  const result=await post('/api/output/config',{enabled});
  if(epoch!==outputEpoch){
    if(enabled)await emergencyStop();
    throw new Error('操作已被紧急停止中断');
  }
  ++outputEpoch;
  renderOutput(result);
  if(result.enabled!==enabled)throw new Error(enabled?'服务未确认开启控制':'尚未确认停止');
}
async function emergencyStop(){
  ++outputEpoch;++kernelEpoch;++emergencyStops;
  try{
    const result=await post('/api/output/stop',{});
    if(result.enabled!==false)throw new Error('服务尚未确认');
    ++outputEpoch;renderOutput(result);notice(output.xinputEnabled?'体感已停止，实体手柄继续透传。':'游戏控制已紧急停止。');
  }catch(error){
    $('#outputPill').textContent='尚未确认停止';
    notice('尚未确认停止：'+error.message+'。请重试紧急停止。');
  }
}
async function setSource(source,enabled=true){
  await setOutput(false);
  const epoch=++kernelEpoch;
  const result=await post('/api/input/source',{source,enabled});
  if(epoch!==kernelEpoch)throw new Error('操作已中断');
  if(enabled&&source==='computer'&&!result.camera?.running){
    // 没有摄像头的电脑在这里是死路：报一句"无法打开"然后没有下文。所以失败时
    // 直接把另外两条出路说出来——换一个摄像头，或者改用手机。
    throw new Error((result.camera?.last_error||'摄像头启动失败')
      +'。没有摄像头的话，把「摄像头来源」改成手机摄像头；有好几个的话，点「扫描摄像头」换一个试试。');
  }
  ++kernelEpoch;desiredSource=source;
  renderKernelState(result);await refreshInput();
  notice(enabled?(source==='phone'?'已选择手机摄像头，等待手机连接':'摄像头识别已启动，游戏控制保持暂停'):'识别已停止');
}

async function handleMainAction(){
  if(!sessionStarted&&!inputStatus.handheld_connected&&!voiceInputReady){
    await setSource($('#poseSource').value,true);
    return;
  }
  await setOutput(!output.enabled);
}
async function startCalibration(){const running=!!kernelState?.head?.calibrating;try{renderKernelState(await post(running?'/api/head/calibration/cancel':'/api/head/calibration/start',{}));notice(running?'校准已取消':'校准已开始：看向游戏屏幕中心，保持自然姿势')}catch(e){notice('中心设置失败：'+(e?.message||e))}}
// 量身：开始前先停掉游戏控制，和校准一样——人要挥手、伸脚、跳，别让游戏里跟着乱按。
async function zoneFit(action,body={}){
  try{
    if(action==='start')await setOutput(false);
    renderKernelState(await post('/api/zones/fit/'+action,body));
  }catch(e){notice('量身没开始：'+(e?.message||e))}
}
// 开着握拳控制的是哪几只手。量身的握拳那一步只量这几只。
function fistHands(){
  if(!handMouseConfig.enabled)return[];
  return[...new Set(['horizontal','vertical'].map(axis=>handMouseConfig[axis+'_hand']).filter(hand=>hand==='left'||hand==='right'))];
}
// 设置页「量身」那一块：量没量过、哪天量的。
function renderZoneFit(k=kernelState||{}){
  const fit=k.zone_fit,status=$('#zoneFitStatus');if(!fit||!status)return;
  const day=t=>{const d=new Date(Number(t)*1000);return`${d.getMonth()+1}月${d.getDate()}日`};
  const zones=!fit.custom?'区域：默认大小':fit.measured_at_unix?`区域：${day(fit.measured_at_unix)}按你的身体量过`:'区域：按你的身体量过';
  const hands=fistHands();
  const grip=!hands.length?'握拳控制没开':fit.grip_measured_at_unix?`握拳：${day(fit.grip_measured_at_unix)}量过`:'握拳：还没量过，认不准就量一下';
  const text=`${zones} · ${grip}`;if(status.textContent!==text)status.textContent=text;
  $('#zoneFitResetBtn').hidden=!fit.custom;
  $('#zoneFitGripBtn').hidden=!hands.length;
}
// 定住了没有、区域怎么算按下：电脑那边说了算，这里只照着画。
function renderZoneFreeze(k=kernelState||{}){
  const frozen=!!k.zones_frozen;
  const follow=$('#followZonesBtn');if(follow)follow.hidden=!frozen||zoneEditMode;
  const adjust=$('#adjustZonesBtn');
  if(adjust){const text=frozen?'调整定住的区域':'挪动区域';if(adjust.textContent!==text)adjust.textContent=text}
  const mode=$('#zoneTriggerMode');
  if(mode&&k.zone_trigger_mode&&document.activeElement!==mode&&mode.value!==k.zone_trigger_mode)mode.value=k.zone_trigger_mode;
  for(const [id,key] of [['verticalRange','range_y'],['verticalDeadzone','deadzone']]){
    const input=$('#'+id),value=Number(k.vertical_look?.[key]);
    if(!input||!Number.isFinite(value)||document.activeElement===input)continue;
    const percent=String(Math.round(value*100));
    if(input.value!==percent){input.value=percent;$('#'+id+'Value').textContent=percent+'%'}
  }
}
$('#zoneTriggerMode')?.addEventListener('change',e=>runAction(async()=>{
  const status=$('#zoneTriggerStatus');
  try{
    renderKernelState(await post('/api/zones/trigger-mode',{mode:e.target.value}));
    status.textContent=e.target.value==='simple'?'已改成进去就按':'已改成防误触';
  }catch(error){status.textContent='没改成：'+error.message;renderZoneFreeze();throw error}
}));
async function centerHead(){try{renderKernelState(await post('/api/head/calibration/center',{}));notice('视角中心已更新。')}catch(e){notice('视角回正失败：'+(e?.message||e))}}

function drawOverlayZones(octx,w,h,zones={}){
  for(const[id,def]of Object.entries(BODY_ZONES)){
    const state=zones[id];if(!state)continue;const active=!!state.pressed,isGate=!!def.gate;
    octx.save();octx.lineWidth=Math.max(2,w/220);octx.strokeStyle=isGate?(active?'#62ff91':'#62d982'):(active?'#ff5966':'rgba(255,255,255,.78)');octx.fillStyle=isGate?(active?'rgba(45,210,95,.30)':'rgba(30,150,75,.15)'):(active?'rgba(255,70,80,.26)':'rgba(0,0,0,.12)');if(isGate&&!active)octx.setLineDash([Math.max(4,w/100),Math.max(3,w/140)]);
    let x=0,y=0,ww=0,hh=0;const r=state.rect;
    if(r){x=(1-Number(r.x2))*w;y=Number(r.y1)*h;ww=(Number(r.x2)-Number(r.x1))*w;hh=(Number(r.y2)-Number(r.y1))*h}
    if(ww<=0||hh<=0){octx.restore();continue}octx.beginPath();if(isGate)octx.roundRect(x,y,ww,hh,Math.max(8,w/70));else octx.roundRect(x,y,ww,hh,Math.max(6,w/90));octx.fill();octx.stroke();octx.setLineDash([]);octx.fillStyle='#fff';octx.font=`800 ${Math.round(Math.max(11,Math.min(Math.min(ww,hh)*.34,w/9)))}px system-ui,sans-serif`;octx.textAlign='center';octx.textBaseline='middle';octx.fillText(isGate?(active?'上下视角 已开启':'上下视角'):zoneKeyLabel(id,def),x+ww/2,y+hh/2);octx.restore();
  }
}
function renderOverlay(map=currentPoseMap){
  if(!overlay.win||overlay.win.closed||!overlay.canvas||!overlay.ctx)return;const c=overlay.canvas,octx=overlay.ctx,w=c.width,h=c.height;octx.setTransform(1,0,0,1,0,0);octx.clearRect(0,0,w,h);octx.fillStyle='#050608';octx.fillRect(0,0,w,h);
  draw(map,octx,w,h,true);
  drawOverlayZones(octx,w,h,kernelState?.zones||{});const buttons=kernelState?.buttons||[],motions=kernelState?.motions||[];const gate=!!kernelState?.vertical_gate_active;const latch=voiceLatchText(output);const text=gate?'上下视角已开启':(buttons.length?`区域 ${buttons.join('+')}`:(motions.length?`动作 ${motions.join('+')}`:(map?'未触发':'未识别人体')));octx.fillStyle=latch?'rgba(70,32,0,.78)':'rgba(0,0,0,.62)';octx.fillRect(0,h-Math.max(25,h/10),w,Math.max(25,h/10));octx.fillStyle=latch?'#ffc46b':'#fff';octx.font=`600 ${Math.max(12,Math.round(w/32))}px system-ui,sans-serif`;octx.textAlign='left';octx.textBaseline='alphabetic';octx.fillText(latch?`${latch} · 说松开才会放`:`${output.enabled?'输出开':'输出关'} · ${text}`,Math.max(7,w/70),h-Math.max(7,h/70))
}

async function toggleOverlay(){if(overlay.win&&!overlay.win.closed){try{overlay.win.close()}catch{}overlay.win=null;overlay.canvas=null;overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗';return}if(!window.documentPictureInPicture?.requestWindow){notice('当前浏览器不支持置顶游戏悬浮窗。');return}try{const pip=await window.documentPictureInPicture.requestWindow({width:420,height:315});pip.document.title='MotionControl';pip.document.body.style.cssText='margin:0;overflow:hidden;background:#050608;width:100vw;height:100vh';const c=pip.document.createElement('canvas');c.width=640;c.height=480;c.style.cssText='display:block;width:100vw;height:100vh;object-fit:contain;background:#050608';pip.document.body.appendChild(c);overlay.win=pip;overlay.canvas=c;overlay.ctx=c.getContext('2d');pip.addEventListener('pagehide',()=>{overlay.win=overlay.canvas=overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗'},{once:true});$('#overlayBtn').textContent='关闭悬浮';renderOverlay(currentPoseMap)}catch(e){notice('悬浮窗启动失败：'+(e?.message||e))}}


function addVoiceRow(mapping={phrase:'',type:'keyboard',target:''}){const row=document.createElement('div');row.className='voice-row';const phrase=document.createElement('input');phrase.className='voice-phrase';phrase.placeholder='动作词，例如 地图';phrase.title='这里填写唤醒词后面的动作词';phrase.value=mapping.phrase||'';const phraseBox=document.createElement('div');phraseBox.className='voice-phrase-wrap';const prefix=document.createElement('span');prefix.className='voice-prefix';prefix.textContent=currentVoiceWakeWord();phraseBox.append(prefix,phrase);const type=document.createElement('select');type.className='voice-type';for(const[value,label]of[['keyboard','键盘/组合键'],['gamepad','Xbox 键'],['system','系统命令']]){const o=document.createElement('option');o.value=value;o.textContent=label;type.appendChild(o)}type.value=mapping.type||'keyboard';const target=document.createElement('span');target.className='voice-target-cell';const fillVoiceTarget=value=>{
  if(type.value==='system'){target.replaceChildren();const sel=document.createElement('select');sel.className='binding-target';for(const[v,t]of VOICE_SYSTEM_TARGETS){const o=document.createElement('option');o.value=v;o.textContent=t;sel.appendChild(o)}if([...sel.options].some(o=>o.value===value))sel.value=value;target.appendChild(sel);return}
  // Voice rows can be built before the action catalog arrives.  Gamepad falls
  // back to its own built-in key list, but keyboard is only free text because
  // the catalog says so, and without it the picker would come out empty.
  if(type.value==='keyboard'&&!gameProfile.actions?.keyboard){target.replaceChildren(makeKeyCaptureInput('binding-target',value||''));return}
  fillTargetControl(target,type.value,value);
};fillVoiceTarget(mapping.target||'');const remove=document.createElement('button');remove.type='button';remove.className='btn voice-remove';remove.textContent='删除';remove.addEventListener('click',()=>{row.remove();if(!$('#voiceRows').children.length)addVoiceRow()});const behavior=document.createElement('select');behavior.className='voice-behavior';behavior.setAttribute('aria-label','语音动作方式');const legacyBehavior={hold:'持续按住（旧设置）',release:'松开（旧设置）'}[mapping.behavior];for(const[value,label]of [['tap','点按'],...(legacyBehavior?[[mapping.behavior,legacyBehavior]]:[])]){const option=document.createElement('option');option.value=value;option.textContent=label;behavior.appendChild(option)}behavior.value=mapping.behavior||'tap';behavior.title=legacyBehavior?'通用口令不再新设持续按住、松开。旧的照常生效；要按住某个键，放到「本游戏」的口令里，那边能选用哪个动作停住它':'通用口令只能点按。要按住某个键，放到「本游戏」的口令里设持续按住';const syncBehavior=()=>{behavior.disabled=type.value==='system'||behavior.options.length<2;if(type.value==='system')behavior.value='tap'};type.addEventListener('change',()=>{fillVoiceTarget(target.querySelector('.binding-target')?.value||'');syncBehavior()});syncBehavior();row.append(phraseBox,type,target,behavior,remove);$('#voiceRows').appendChild(row);watchVoicePhrase(phrase)}
function readVoiceMappings(){const rows=[...document.querySelectorAll('.voice-row')],items=[],old=new Map((voice.status?.mappings||[]).map(m=>[m.phrase,m]));for(const row of rows){const phrase=row.querySelector('.voice-phrase').value.trim(),type=row.querySelector('.voice-type').value,target=(row.querySelector('.binding-target')?.value||'').trim();if(!phrase&&!target)continue;if(!phrase||!target)throw new Error('每条口令都要填「说什么」和「输出什么」');const behavior=type==='system'?'tap':row.querySelector('.voice-behavior').value;const item={phrase,type,target,behavior},previous=old.get(phrase);if(previous?.synonyms?.length)item.synonyms=[...previous.synonyms];items.push(item)}return items}
function renderVoiceRows(items){$('#voiceRows').replaceChildren();for(const m of items||[])addVoiceRow(m);if(!$('#voiceRows').children.length)addVoiceRow()}
function renderVoiceStatus(s=voice.status){
  if(!s)return;voice.status=s;renderVoiceGuide(s);const has=!!s.model_ready,connected=!!s.connected;const isSingleKws=String(s.recognizer_mode||'').includes('single_stage')||String(s.recognizer_mode||'').includes('kws');
  $('#voiceMode').textContent=has?(isSingleKws?`短语识别 · ${s.supported_count||0} 条`:`语音 · ${s.supported_count||0} 条`):'未就绪';$('#voiceMode').className='pill '+(has?'ok':'warn');
  const pcOk=connected&&s.source_kind==='computer'&&s.available&&s.model_ready&&s.audio_ready&&(s.audio_alive||s.stream_alive);const phoneOk=connected&&s.source_kind!=='computer';const ready=pcOk||phoneOk;voiceInputReady=ready;
  $('#voicePill').textContent=ready?'语音 ✓':(connected?'语音准备中':'语音');$('#voicePill').className='pill '+(ready?'ok':(connected?'warn':'optional'));
  const phrase=String(s.last_final||s.final||s.last_command||'').trim();
  let voiceText=ready?'语音已就绪':'语音尚未准备好';
  if(phrase){
    if(s.last_action==='wake')voiceText=`已听到唤醒词「${currentVoiceWakeWord(s)}」，请再说动作口令`;
    else if(s.last_executed===true)voiceText=`已听到「${phrase}」 · 电脑已执行`;
    else if(s.last_executed===false)voiceText=`已听到「${phrase}」 · 电脑未执行${s.last_error?`：${s.last_error}`:''}`;
    else voiceText=`已听到「${phrase}」 · 电脑执行状态尚未确认`;
  }
  $('#voiceStatus').textContent=voiceText;
  const modelPath=s.model_path||s.command_model_path||'—';const mp=$('#voiceModelPath');if(mp){mp.textContent='模型：'+modelPath;mp.title=modelPath}
  renderPersonalVoice(s);
  const clash=$('#voiceConflicts');if(clash){const list=s.phrase_conflicts||[];clash.hidden=!list.length;clash.textContent=list.length?`${list.join('；')}。同名的只有一条会生效，改掉其中一条的说法。`:''}
  // 换游戏、装别人的配置带进来的口令没经过输入框，这里兜底照实说。
  const unheard=$('#voiceUnheard');if(unheard){const list=s.unheard||[];unheard.hidden=!list.length;unheard.textContent=list.length?`这几句口令里有语音认不出的字，说了也听不到：${list.slice(0,6).map(item=>`${item.phrase}（${(item.chars||[]).join('、')}）`).join('；')}${list.length>6?` 等 ${list.length} 句`:''}。换个说法。`:''}
  const diag=$('#voiceDiagnostic');if(diag){diag.textContent=[`模式：${s.recognizer_mode||'—'}`,`词条：${s.supported_count??'—'}`,`模型：${modelPath}`,`音频：${s.audio_ready?'已准备':'未准备'} / ${s.audio_alive||s.stream_alive?'运行中':'空闲'}`,`音量：${Number(s.rms||0).toFixed(0)} · 字节：${s.bytes_received||0}`,`最后听到：${phrase||'—'}`,`电脑执行：${s.last_executed===true?'已执行':s.last_executed===false?'未执行':'未确认'}`,`错误：${s.last_error||'—'}`].join('\n')}
}

/* 急停口令在界面上就是通用口令里的一行：输出选「系统命令 → 紧急停止」。存的时候
 * 还是分开存——它们在自己那一份里，不跟配置分享出去，装别人的配置也冲不掉；
 * 听到了也走急停那条最快的路，不经过"游戏控制开没开"。
 * 存的写法带默认唤醒词「体感」，这样改了唤醒词，它们跟着一起换。 */
const EMERGENCY_TARGET='EMERGENCY_STOP',DEFAULT_WAKE='体感',BUILT_IN_STOP='紧急停止';
const isEmergencyRow=item=>item.type==='system'&&item.target===EMERGENCY_TARGET;
function voiceRowsFromStatus(s){
  const wake=s?.wake_word||DEFAULT_WAKE;
  const stops=(s?.emergency_stop_phrases||[])
    .map(phrase=>String(phrase).startsWith(wake)?String(phrase).slice(wake.length):String(phrase))
    .filter(phrase=>phrase&&phrase!==BUILT_IN_STOP)
    .map(phrase=>({phrase,type:'system',target:EMERGENCY_TARGET,behavior:'tap'}));
  return [...stops,...(s?.mappings||[])];
}
async function saveVoiceMappings(){
  const rows=readVoiceMappings();
  const s=await post('/api/voice/config',{mappings:rows.filter(item=>!isEmergencyRow(item)),
    emergency_stop_phrases:rows.filter(isEmergencyRow).map(item=>DEFAULT_WAKE+item.phrase)});
  voice.status=s;renderVoiceStatus(s);return s;
}
// 唤醒词只属于你：不跟游戏走、也不跟配置分享出去。
function renderPersonalVoice(status){
  const wake=$('#wakeWord');
  // 正在输入就不覆盖。语音状态 0.9 秒刷一次，不让开就会把手里打一半的字抹掉。
  if(!wake||document.activeElement===wake)return;
  const next=status?.wake_word||'';
  if(wake.value!==next){wake.value=next;queueVoiceCheck(wake)}
}
async function saveWakeWord(){
  const say=(text,kind='')=>{const el=$('#personalVoiceStatus');if(el){el.textContent=text;el.className=kind==='error'?'statusline error':'statusline'}};
  const value=String($('#wakeWord').value||'').trim();
  if(!value||value===voice.status?.wake_word)return;
  try{
    // mappings 要原样带上：configure 是整份替换，不带等于把口令全删了。
    const s=await post('/api/voice/config',{mappings:voice.status?.mappings||[],wake_word:value});
    voice.status=s;renderVoiceStatus(s);say('唤醒词已保存');
    // 通用口令整句是「唤醒词+后半句」，唤醒词换了要重新查。
    document.querySelectorAll('.voice-phrase').forEach(queueVoiceCheck);
  }catch(error){say(error.message,'error')}
}
document.getElementById('wakeWord')?.addEventListener('change',saveWakeWord);
if($('#wakeWord'))watchVoicePhrase($('#wakeWord'));
async function refreshVoice(){try{voice.status=await api('/api/voice/status');renderVoiceStatus(voice.status)}catch{voiceInputReady=false;$('#voiceStatus').textContent='语音状态无法确认'}}
function voiceActionLabel(action){if(!action)return '当前游戏未启用';if(action.type==='system')return '系统功能 · '+(SYSTEM_TARGET_NAMES.get(action.target)||action.target||'');if(action.type==='voice_release')return `${ACTION_TYPE_LABELS.voice_release} · ${voiceCommandNames(action.target).join('、')}`;return `${ACTION_TYPE_LABELS[action.type]||action.type} · ${targetLabel(action)} · ${{tap:'点按',hold:'持续按住',release:'松开'}[action.behavior||'tap']||'点按'}`}
function renderVoiceCommandCard(command){const card=document.createElement('div');card.className='voice-command-card';card.setAttribute('role','listitem');const phrase=document.createElement('div');phrase.textContent=command.phrase||'';const label=document.createElement('small');label.textContent=command.system_fixed?(command.label||''):[command.label,voiceActionLabel(command.effective_action)].filter(Boolean).join(' · ');card.append(phrase,label);return card}
function renderVoiceCommandCatalog(commands){
  voiceCatalog=Array.isArray(commands)?commands:[];
  const full=$('#voiceCommandGrid');full.replaceChildren();
  const wake=voice.status?.wake_word||'体感';
  const shared=voiceRowsFromStatus(voice.status).map(item=>({
    phrase:wake+item.phrase,label:'',effective_action:{type:item.type,target:item.target,behavior:item.behavior||'tap'},
  }));
  for (const [name,items] of [
    ['内置口令 · 不能改',voiceCatalog.filter(item=>item.system_fixed)],
    ['本游戏口令',voiceCatalog.filter(item=>String(item.id||'').startsWith('game.profile_slot_')&&item.effective_action)],
    ['通用口令 · 所有游戏',shared],
  ]){
    if(!items.length)continue;
    const section=document.createElement('section');section.className='voice-group';
    const title=document.createElement('h3');title.textContent=name;
    const grid=document.createElement('div');grid.className='voice-command-grid';
    for(const item of items)grid.append(renderVoiceCommandCard(item));
    section.append(title,grid);full.append(section);
  }
}
async function refreshVoiceCommands(){try{const data=await api('/api/voice/commands');renderVoiceCommandCatalog(data.commands||[])}catch{renderVoiceCommandCatalog([])}}

function syncControlLabels(){head.algorithm=$('#headAlgorithm').value;const horizontalAlgorithm=$('#headHorizontalAlgorithm')?.value;head.horizontalAlgorithm=['gesture_v153','frozen22','gesture_v188','roll_tilt'].includes(horizontalAlgorithm)?horizontalAlgorithm:'gesture_v188';if($('#rollTiltHint'))$('#rollTiltHint').hidden=head.horizontalAlgorithm!=='roll_tilt';const pickedVertical=$('#verticalLookSource')?.value;head.verticalLookEnabled=pickedVertical!=='off';if(head.verticalLookEnabled)head.verticalLookSource=pickedVertical==='head'?'head':'hand';head.verticalExclusive=!!$('#verticalExclusive')?.checked;head.bodyMotionGuard=!!$('#bodyMotionGuard')?.checked;head.deadzone=Number($('#deadzone').value)/100;head.sensitivityX=Number($('#speedX').value);head.sensitivityY=Number($('#speedY').value);head.enabled=$('#headEnable').checked;head.invertY=$('#invertY').checked;document.querySelectorAll('.head-vertical-setting').forEach(el=>el.style.setProperty('display',head.verticalLookSource==='head'?'block':'none','important'));$('#deadzoneValue').textContent=Math.round(head.deadzone*100)+'%';$('#speedXValue').textContent=head.sensitivityX+'%';$('#speedYValue').textContent=head.sensitivityY+'%';output.strength=Number($('#strength').value);$('#strengthValue').textContent=output.strength+'%'}
async function pushHeadConfig(){
  syncControlLabels();
  renderKernelState(await post('/api/head/config',{
    algorithm:head.algorithm,horizontal_algorithm:head.horizontalAlgorithm,deadzone:head.deadzone,
    sensitivity_x:head.sensitivityX,sensitivity_y:head.sensitivityY,enabled:head.enabled,
    invert_y:head.invertY,vertical_look_source:head.verticalLookEnabled?head.verticalLookSource:'off',
    vertical_exclusive:head.verticalExclusive,body_motion_guard:head.bodyMotionGuard,
  }));
}
function autosaver(save,statusId,retryId,onDirty=()=>{}){
  let revision=0,saved=0,flight=null,timer;
  const status=$('#'+statusId),retry=$('#'+retryId);
  async function flush(){
    clearTimeout(timer);
    if(flight){await flight;return flush()}
    if(saved===revision)return;
    const version=revision;status.textContent='正在保存…';retry.hidden=true;
    flight=save();
    try{await flight;saved=version}
    catch(error){status.textContent='保存失败，修改已保留：'+error.message;retry.hidden=false;throw error}
    finally{flight=null}
    if(saved!==revision)return flush();
    onDirty(false);status.textContent='已自动保存';
  }
  retry.addEventListener('click',()=>flush().catch(()=>{}));
  return {
    dirty(){revision++;onDirty(true);status.textContent='有待保存的修改';clearTimeout(timer);timer=setTimeout(()=>flush().catch(()=>{}),500)},
    flush,pending:()=>saved!==revision,
  };
}
async function openLiveZoneEditor(){
  if(zoneEditMode)return;
  await setOutput(false);
  // 区域平时跟着人走。先把它定在现在的位置再拖——一直在动的东西没法拖。
  // 想让它重新跟着走，点「恢复跟随」。
  await openFrozenZoneEditor();
}
function frozenRectsFrom(zones){
  const out={};
  for(const id of RECT_EDIT_ZONE_IDS){const r=zones?.[id]?.rect;if(r)out[id]={x1:+r.x1,y1:+r.y1,x2:+r.x2,y2:+r.y2}}
  return out;
}
async function openFrozenZoneEditor(){
  const wasFrozen=!!kernelState?.zones_frozen;
  if(!wasFrozen)renderKernelState(await post('/api/zones/freeze',{frozen:true}));
  const rects=frozenRectsFrom(kernelState?.zones);
  if(!Object.keys(rects).length)throw new Error('还没看到人，没有框可以定住：先让头和双肩入镜');
  Object.assign(rectEdit,{rects,backup:structuredClone(rects),wasFrozen});
  if(!rects[rectEdit.selected])rectEdit.selected=Object.keys(rects)[0];
  zoneEditMode=true;viewer.classList.add('zone-editing','rect-editing');$('#zoneEditBar').hidden=false;
  renderKernelZones(kernelState?.zones||{});renderZoneFreeze();renderMainStatus();
  document.querySelector(`.zone[data-zone="${rectEdit.selected}"]`)?.focus();
  notice(wasFrozen?'拖框移动，拖四个角改大小，改完点「保存区域」。':'框已定住，不再跟着你走。拖框移动，拖四个角改大小，改完点「保存区域」。');
}
function closeLiveZoneEditor(){
  zoneEditMode=false;liveZoneDrag=null;
  viewer.classList.remove('zone-editing','rect-editing');$('#zoneEditBar').hidden=true;
  renderKernelZones(kernelState?.zones||{});renderZoneFreeze();renderMainStatus();$('#adjustZonesBtn').focus();
}
// 显示用的框（镜像过的，左边就是屏幕左边）和原始坐标互换。拖的时候全在显示坐标里算。
const rectToDisplay=r=>({left:1-r.x2,right:1-r.x1,top:r.y1,bottom:r.y2});
const rectFromDisplay=d=>({x1:1-d.right,x2:1-d.left,y1:d.top,y2:d.bottom});
function startRectDrag(e,el,id){
  rectEdit.selected=id;
  const corner=e.target?.closest?.('.zone-handle')?.dataset.corner||'';
  const box=viewer.getBoundingClientRect();
  const at={x:(e.clientX-box.left)/Math.max(1,box.width),y:(e.clientY-box.top)/Math.max(1,box.height)};
  e.preventDefault();el.setPointerCapture?.(e.pointerId);
  liveZoneDrag={id,pointerId:e.pointerId,corner,from:at,start:rectToDisplay(rectEdit.rects[id])};
  renderKernelZones(kernelState?.zones||{});
}
function moveRectDrag(e){
  const drag=liveZoneDrag,box=viewer.getBoundingClientRect();
  const x=clamp((e.clientX-box.left)/Math.max(1,box.width),0,1),y=clamp((e.clientY-box.top)/Math.max(1,box.height),0,1);
  const d={...drag.start};
  if(!drag.corner){
    // 整个框平移，碰到画面边就停。
    const w=d.right-d.left,h=d.bottom-d.top;
    d.left=clamp(drag.start.left+x-drag.from.x,0,1-w);d.right=d.left+w;
    d.top=clamp(drag.start.top+y-drag.from.y,0,1-h);d.bottom=d.top+h;
  }else{
    if(drag.corner.includes('w'))d.left=clamp(x,0,d.right-RECT_EDIT_MIN);
    if(drag.corner.includes('e'))d.right=clamp(x,d.left+RECT_EDIT_MIN,1);
    if(drag.corner.includes('n'))d.top=clamp(y,0,d.bottom-RECT_EDIT_MIN);
    if(drag.corner.includes('s'))d.bottom=clamp(y,d.top+RECT_EDIT_MIN,1);
  }
  rectEdit.rects[drag.id]=rectFromDisplay(d);
  renderKernelZones(kernelState?.zones||{});
}
function nudgeRect(id,key,shift){
  const r=rectEdit.rects[id];if(!r)return;
  const d=rectToDisplay(r),step=.01;
  const dx=key==='ArrowLeft'?-step:key==='ArrowRight'?step:0,dy=key==='ArrowUp'?-step:key==='ArrowDown'?step:0;
  if(shift){
    // Shift：往右、往下是变大，往左、往上是变小（动的是右边和下边）。
    d.right=clamp(d.right+dx,d.left+RECT_EDIT_MIN,1);d.bottom=clamp(d.bottom+dy,d.top+RECT_EDIT_MIN,1);
  }else{
    const w=d.right-d.left,h=d.bottom-d.top;
    d.left=clamp(d.left+dx,0,1-w);d.right=d.left+w;d.top=clamp(d.top+dy,0,1-h);d.bottom=d.top+h;
  }
  rectEdit.rects[id]=rectFromDisplay(d);renderKernelZones(kernelState?.zones||{});
}
function startLiveZoneDrag(e){
  if(!zoneEditMode)return;const el=e.currentTarget,id=el?.dataset?.zone;
  if(id&&rectEdit.rects[id])startRectDrag(e,el,id);
}
function moveLiveZoneDrag(e){
  if(zoneEditMode&&liveZoneDrag&&e.pointerId===liveZoneDrag.pointerId)moveRectDrag(e);
}
function endLiveZoneDrag(e){if(!liveZoneDrag)return;if(e?.pointerId!==undefined&&liveZoneDrag.pointerId!==e.pointerId)return;liveZoneDrag=null}
async function saveLiveZones(){
  await setOutput(false);
  renderKernelState(await post('/api/zones/frozen',{rects:rectEdit.rects}));
  closeLiveZoneEditor();notice('区域已保存，会一直定在这里。想让它重新跟着你走，点「恢复跟随」。');
}
async function cancelLiveZones(){
  await setOutput(false);
  // 这次是点「挪动区域」才定住的，取消就回到跟着走；本来就定住的，回到拖之前的样子。
  renderKernelState(rectEdit.wasFrozen
    ?await post('/api/zones/frozen',{rects:rectEdit.backup})
    :await post('/api/zones/freeze',{frozen:false}));
  closeLiveZoneEditor();notice(rectEdit.wasFrozen?'已取消区域调整。':'已取消，区域继续跟着你走。');
}

// 定住的跟随框放开，重新跟着人走。拖过的大小位置不留：下次定住按那时的位置重新定。
async function followZones(){
  await setOutput(false);
  renderKernelState(await post('/api/zones/freeze',{frozen:false}));
  if(zoneEditMode)closeLiveZoneEditor();
  notice('区域恢复跟随，重新跟着你走。');
}
// 区域挪到我这里：没定住就在现在的位置定住；定住了就整组框按人现在站的位置搬过来，
// 拖过的大小和相对位置不变。语音、动作绑的「系统功能 → 区域挪到我这里」是同一件事。
async function moveZonesHere(){
  renderKernelState(await post('/api/zones/move-here',{}));
  if(zoneEditMode){
    rectEdit.rects=frozenRectsFrom(kernelState?.zones);rectEdit.wasFrozen=true;
    renderKernelZones(kernelState?.zones||{});
  }
  notice('区域已挪到你现在的位置。');
}
async function runAction(action){
  if(actionBusy)return;
  actionBusy=true;renderMainStatus();
  try{await action()}catch(error){notice(error.message||'操作失败，请重试')}
  finally{actionBusy=false;renderMainStatus()}
}
function bind(id,action){$('#'+id).addEventListener('click',()=>runAction(action))}
function showView(view){
  if(zoneEditMode&&view!=='play'){notice('请先保存或取消区域调整');return}
  currentView=view;
  document.querySelectorAll('[data-panel]').forEach(el=>el.hidden=el.dataset.panel!==view);
  document.querySelectorAll('[data-view]').forEach(el=>{
    if(el.dataset.view===view)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');
  });
  const tab=document.querySelector(`[data-view="${view}"]`);
  $('#viewHint').textContent=tab?.dataset.viewHint||'';
  if(view==='devices'){void refreshXinput();void refreshHandMouse();void refreshPoseRecord()}
  if(view==='games')loadCloudEndpoint().catch(()=>{});
  window.scrollTo(0,0);
}
function poll(task,delay,enabled=()=>true){
  async function next(){
    try{if(enabled())await task()}catch{}
    setTimeout(next,document.hidden?Math.max(delay,1500):delay);
  }
  void next();
}
async function init(){
  initViewControl();setViewControlBusy(false);syncControlLabels();
  // 自定义姿势和宏库要排在最前面，比 refreshKernel 还早。
  //
  // 原因不显眼：refreshKernel 里有一句 void loadProfiles()，它不被 await，会自己跑去
  // 建映射行。那一刻这两份要是还没到，建出来的表就是缺的：录过的姿势没有对应的
  // 行，每个「键盘宏」下拉都写着“还没有宏”。东西明明在，页面上却说没有。
  //
  // 这是个旧毛病，只是之前靠时机碰对的次数多——中间多一次 await 就会碰错。
  await Promise.all([refreshCustomPoses({rebuild:false}), refreshMacros({rebuild:false})]);
  await refreshKernel();await refreshOutput();
  const results=await Promise.allSettled([
    refreshInput(),refreshXinput(),refreshVoice(),refreshVoiceCommands(),refreshCameraConfig(),refreshPoseLibrary(),
    reloadViewControlState().then(()=>{setViewControlBusy(false);renderViewControl(true)}),
    api('/api/models').then(data=>{
      modelAvailable=!!data.models?.[0]?.available;
      $('#modelStatus').textContent=modelAvailable?'电脑人体模型可用':'电脑人体模型不可用；手机输入、实体手柄不受此项影响';
    }),
  ]);
  if(results.some(result=>result.status==='rejected'))notice('部分设备信息尚未读取，可继续使用已连接的输入');
  await loadProfiles();renderVoiceRows(voiceRowsFromStatus(voice.status));
  poll(refreshKernel,250);poll(async()=>{await refreshInput();await refreshOutput();await refreshVoice()},900);
  poll(refreshXinput,1500,()=>currentView==='devices');
  poll(refreshPerformance,1500,()=>currentView==='devices'&&$('#advancedSettings').open&&$('#performancePanel').open);
  poll(refreshPreview,150);
}
async function loadProfiles(){
  if(profileLoading||profileReady)return;
  profileLoading=true;
  try{await refreshVoiceCommands();await refreshProfile();profileReady=true}
  catch(error){$('#profileMeta').textContent='游戏配置尚未读取，将自动重试：'+error.message}
  finally{profileLoading=false}
}
const headSaver=autosaver(pushHeadConfig,'headSaveStatus','retryHeadBtn',value=>{headDirty=value});
const voiceSaver=autosaver(saveVoiceMappings,'voiceSaveStatus','retryVoiceBtn');
document.querySelectorAll('[data-view],[data-go]').forEach(el=>el.addEventListener('click',()=>showView(el.dataset.view||el.dataset.go)));
bind('mainActionBtn',handleMainAction);
bind('sourceStartBtn',()=>setSource($('#poseSource').value,true));
bind('sourceStopBtn',()=>setSource(sourceMode,false));
bind('overlayBtn',toggleOverlay);
for(const [id,key] of [['handMouseEnabled','enabled'],['handMouseSensitivity','sensitivity'],['handMouseDeadzone','deadzone'],['handMouseClose','fist_close'],['handMouseOpen','fist_open'],['handMouseCurlClose','curl_close'],['handMouseCurlOpen','curl_open']]){
  $('#'+id).addEventListener('change',e=>void saveHandMouseFields({[key]:key==='enabled'?e.target.checked:Number(e.target.value)}));
}
for(const axis of ['horizontal','vertical']){
  $('#view'+(axis==='horizontal'?'Horizontal':'Vertical')+'Source').addEventListener('change',()=>void saveViewControlAxis(axis));
  $('#handMouse'+(axis==='horizontal'?'Horizontal':'Vertical')+'Hand').addEventListener('change',()=>void saveAdvancedHandAxis(axis));
}
$('#verticalLookSource').addEventListener('change',()=>void saveLegacyVertical());
$('#headEnable').addEventListener('change',()=>void saveHeadEnabled());
// 教学只指路不代劳：连接、校准都由人去点真按钮。它自己只会做一件事——扫一遍
// 摄像头，好知道该建议什么。
const tutorial=createTutorial({state:tutorialState,scanCameras,
  zoneFitStart:()=>zoneFit('start'),zoneFitGripOnly:()=>zoneFit('start',{body:false}),
  zoneFitSkip:()=>zoneFit('skip'),zoneFitCancel:()=>zoneFit('cancel')});
$('#zoneFitBtn').addEventListener('click',e=>tutorial.openLesson('fit',e.currentTarget));
$('#zoneFitGripBtn').addEventListener('click',e=>tutorial.openLesson('fit',e.currentTarget,{gripOnly:true}));
bind('zoneFitResetBtn',async()=>{renderKernelState(await post('/api/zones/fit/reset',{}));notice('区域已恢复默认大小。')});
for(const id of ['tutorialBtn','tutorialSettingsBtn'])$('#'+id).addEventListener('click',e=>tutorial.open(e.currentTarget));
bind('poseRecordBtn',startPoseRecord);
bind('poseRecordCancelBtn',cancelPoseRecord);
bind('profileSearchBtn',searchProfiles);
$('#profileSearch').addEventListener('keydown',e=>{if(e.key==='Enter')void runAction(searchProfiles)});
bind('profileApplyBtn',applySelectedProfile);bind('resetProfileBindingsBtn',resetProfileBindings);
bind('customGameAddBtn',addCustomGame);
bind('customGameRenameBtn',renameCustomGame);
bind('customGameRemoveBtn',removeCustomGame);
$('#customGameName').addEventListener('keydown',e=>{if(e.key==='Enter')void runAction(addCustomGame)});
$('#customGameAppid').addEventListener('keydown',e=>{if(e.key==='Enter')void runAction(addCustomGame)});
bind('retryProfileSaveBtn',retryProfileBindings);
for(const event of ['input','change'])$('#profileBindingRows').addEventListener(event,e=>{syncMotionConflictChoices();if(e.target.closest('.binding-row[data-trigger^="voice."]'))syncVoiceReleaseChoices();scheduleProfileAutoSave(e)});
bind('adjustZonesBtn',openLiveZoneEditor);bind('followZonesBtn',followZones);bind('zoneEditFollowBtn',followZones);bind('saveLiveZonesBtn',saveLiveZones);bind('cancelLiveZonesBtn',cancelLiveZones);
bind('zoneMoveHereBtn',moveZonesHere);
document.querySelectorAll('.zone').forEach(el=>{
  // 定住的框四个角上的把手，拖它改大小。只在编辑定住的框时显示（见 .rect-editing）。
  for(const corner of ['nw','ne','sw','se']){const handle=document.createElement('span');handle.className='zone-handle';handle.dataset.corner=corner;handle.setAttribute('aria-hidden','true');el.appendChild(handle)}
  for(const [event,handler] of [['pointerdown',startLiveZoneDrag],['pointermove',moveLiveZoneDrag],['pointerup',endLiveZoneDrag],['pointercancel',endLiveZoneDrag]])el.addEventListener(event,handler);
  el.addEventListener('keydown',e=>{
    if(!zoneEditMode||!e.key.startsWith('Arrow'))return;
    e.preventDefault();
    rectEdit.selected=el.dataset.zone;nudgeRect(el.dataset.zone,e.key,e.shiftKey);
  });
});
// 上下视角的范围、死区。原来在参考场景的编辑栏里，现在归通用设置，拖完就存。
for(const [id,key] of [['verticalRange','range_y'],['verticalDeadzone','deadzone']]){
  const input=$('#'+id);if(!input)continue;
  input.addEventListener('input',()=>{$('#'+id+'Value').textContent=input.value+'%'});
  input.addEventListener('change',()=>runAction(async()=>{renderKernelState(await post('/api/vertical-look',{[key]:Number(input.value)/100}))}));
}
$('#stopBtn').addEventListener('click',emergencyStop);
bind('calBtn',async()=>{await setOutput(false);await startCalibration()});bind('centerBtn',centerHead);
bind('calibrationCancel',startCalibration);
$('#calibrationOverlay').addEventListener('cancel',e=>{e.preventDefault();void runAction(startCalibration)});
$('#voiceCommandsBtn').addEventListener('click',()=>$('#voiceCommandsMask').showModal());
$('#closeVoiceCommandsBtn').addEventListener('click',()=>$('#voiceCommandsMask').close());
$('#poseSource').addEventListener('change',()=>{
  desiredSource=$('#poseSource').value;$('#phoneGuide').open=desiredSource==='phone';
  syncCameraDeviceRow();
  notice('已选择'+(desiredSource==='phone'?'手机摄像头':'电脑摄像头')+'，点「连接并开始识别」生效');
});
$('#cameraDevice').addEventListener('change',e=>runAction(async()=>{
  const data=await post('/api/camera/config',{index:Number(e.target.value)});
  cameraIndex=Number(data.camera_index??e.target.value);
  notice('已选择摄像头 '+cameraIndex+'，点「连接并开始识别」看看画面对不对');
}));
// 按钮和新手教学用的是同一个扫描：结果记在 cameraScan 里，教学据此判断这台电脑有几个摄像头。
async function scanCameras(){
  if(cameraScan.state==='running')return;
  cameraScan.state='running';
  const status=$('#cameraScanStatus');if(status)status.textContent='正在逐个尝试，可能要几秒…';
  try{
    const data=await api('/api/camera/devices',{timeoutMs:60000});
    renderCameraDevices(data.devices,data.camera_index);
    const n=(data.devices||[]).length;
    Object.assign(cameraScan,{state:'done',count:n,error:''});
    if(!status)return;
    status.textContent=n?`找到 ${n} 个。选一个，连接之后看画面里是不是你。`
      :'一个也没找到。可能没有摄像头，或者被别的软件占着；也可以把来源改成手机摄像头。';
  }catch(e){Object.assign(cameraScan,{state:'failed',error:String(e?.message||e)});if(status)status.textContent='扫描失败：'+cameraScan.error}
}
bind('cameraScanBtn',scanCameras);
bind('copyPhoneUrlBtn',async()=>{await navigator.clipboard.writeText($('#phoneWsUrl').value);notice('连接地址已复制')});
$('#cameraBackend').addEventListener('change',e=>runAction(async()=>{
  await post('/api/camera/config',{backend:e.target.value});notice('采集方式已保存，下次连接时生效');
}));
$('#outputMode').addEventListener('change',e=>runAction(async()=>{
  ++outputEpoch;renderOutput(await post('/api/output/config',{mode:e.target.value}));await refreshXinput();
}));
$('#xinputMerge').addEventListener('change',()=>runAction(setXinputMerge));
$('#xinputMotionLeft').addEventListener('change',()=>runAction(setXinputMotionLeft));
$('#strength').addEventListener('input',()=>{$('#strengthValue').textContent=$('#strength').value+'%'});
$('#strength').addEventListener('change',()=>runAction(async()=>{
  const gain=Number($('#strength').value)/100;++outputEpoch;
  renderOutput(await post('/api/output/config',{mouse_speed_x:600*gain,mouse_speed_y:450*gain,gamepad_gain:gain}));
}));
for(const id of ['headAlgorithm','headHorizontalAlgorithm','verticalExclusive','bodyMotionGuard','deadzone','speedX','speedY','invertY']){
  $('#'+id).addEventListener('input',()=>{headSaver.dirty();syncControlLabels()});
  $('#'+id).addEventListener('change',()=>{headSaver.dirty();syncControlLabels()});
}
$('#addVoiceBtn').addEventListener('click',()=>addVoiceRow());
$('#voiceRows').addEventListener('input',()=>voiceSaver.dirty());
$('#voiceRows').addEventListener('change',()=>voiceSaver.dirty());
$('#voiceRows').addEventListener('click',e=>{if(e.target.closest('.voice-remove'))voiceSaver.dirty()});
window.addEventListener('keydown',e=>{
  if(e.key==='F9'){e.preventDefault();void emergencyStop()}
  if(e.key==='Escape'&&zoneEditMode)void runAction(cancelLiveZones);
});
window.addEventListener('beforeunload',e=>{
  if(profileDirty.size||headSaver.pending()||voiceSaver.pending()||zoneEditMode){e.preventDefault();e.returnValue=''}
  try{overlay.win?.close()}catch{}
});
init();

/* --- 云端配置 -----------------------------------------------------------
 * Browsing and installing configs other people have published.
 *
 * Nothing else on this page depends on any of it. The cloud is optional, and
 * when it is unreachable this section says so and everything else -- camera,
 * zones, voice, the whole controller -- carries on exactly as before. That is
 * why every call here is in its own try/catch and none of them run at startup.
 */
const cloudStatusEl = document.getElementById('cloudStatus');
const cloudListEl = document.getElementById('cloudList');
const cloudRefreshBtn = document.getElementById('cloudRefreshBtn');

function cloudSay(text, kind = '') {
  if (!cloudStatusEl) return;
  cloudStatusEl.textContent = text;
  cloudStatusEl.className = kind === 'error' ? 'statusline error' : 'statusline';
}

/** 云端地址，从服务端读一次，用来拼「在网站上打开」的链接。 */
let cloudEndpoint = '';

async function cloudRefresh() {
  if (!cloudListEl) return;
  cloudSay('正在连接云端…');
  cloudListEl.innerHTML = '';
  try {
    const status = await api('/api/cloud/status', { timeoutMs: 12000 });
    cloudEndpoint = status.endpoint || '';
    if (!status.reachable) {
      cloudSay(`连不上 ${status.endpoint}：${status.error || '未知原因'}`, 'error');
      return;
    }

    // 只查当前这个游戏。桌面端要回答的问题是"我现在玩的这个游戏有什么现成配置"，
    // 不是"云端一共有什么"——后者配置一多就是一堵墙，那是网站该干的事。
    // 服务端把没有游戏的配置（动作映射、语音映射）也算作与当前游戏相关：它们对
    // 每个游戏都适用，筛掉等于藏起最该出现的那几份。
    const gameId = gameProfile.selected?.selected_id || gameProfile.selected?.id || '';
    const gameName = gameProfile.selected?.name || gameId;
    const { profiles } = await post('/api/cloud/browse', { game_id: gameId }, 15000);
    if (!profiles.length) {
      cloudSay(`《${gameName}》还没有人公开分享配置。`);
      return;
    }
    cloudSay(`《${gameName}》· ${profiles.length} 份`);
    for (const item of profiles) cloudListEl.appendChild(cloudRow(item));
  } catch (error) {
    cloudSay(error.message, 'error');
  }
}

/** 只要地址。以前只有「查看分享」会读它，于是没先点那个就点「打开网站」，只会说没连上。 */
async function loadCloudEndpoint() {
  if (cloudEndpoint) return cloudEndpoint;
  const status = await api('/api/cloud/status', { timeoutMs: 12000 });
  cloudEndpoint = status.endpoint || '';
  return cloudEndpoint;
}

/** 在系统浏览器里打开网站上的某一页。详情、浏览全部、上传都在那边。 */
async function openOnSite(path = '') {
  if (cloudEndpoint) { window.open(cloudEndpoint + path, '_blank', 'noopener'); return; }
  // 地址还没读到：先趁这一下点击开一个空窗口，读到了再跳过去。等读完再开的话，
  // 那一下点击已经过期，浏览器会把它当弹窗拦掉。
  const win = window.open('about:blank', '_blank');
  try {
    const endpoint = await loadCloudEndpoint();
    if (!endpoint) throw new Error('没有配置云端地址');
    if (win) { win.opener = null; win.location.href = endpoint + path; }
    else window.open(endpoint + path, '_blank', 'noopener');
  } catch (error) {
    win?.close();
    cloudSay(error.message || '读不到云端地址', 'error');
  }
}

const CLOUD_DOC_NAMES = {
  profile_selection: '游戏映射',
  motion_mappings: '动作映射',
  voice_mappings: '语音映射',
};

function cloudRow(item) {
  const row = document.createElement('div');
  row.className = 'profile-bar cloud-row';

  const label = document.createElement('div');
  label.className = 'cloud-row-label';
  // 标题就是去网站看详情的入口——这里只放够认出它的信息，绑了哪些键、改了什么
  // 都在网站上，塞进这个小面板只会两边都说不清楚。
  const name = document.createElement('a');
  name.href = '#';
  name.className = 'cloud-row-title';
  name.textContent = item.title;
  name.title = '在网站上查看这份配置的详细内容';
  name.addEventListener('click', event => {
    event.preventDefault();
    openOnSite(`/config/${item.id}`);
  });
  const meta = document.createElement('span');
  meta.className = 'muted';
  const parts = [CLOUD_DOC_NAMES[item.doc_type] || item.doc_type, item.owner_name];
  // 没有游戏的那两种是通用配置，标出来，否则在"当前游戏"的列表里看着突兀。
  parts.push(item.game_name || '所有游戏通用');
  if (item.current_version) parts.push(`v${item.current_version.revision_no}`);
  meta.textContent = parts.join(' · ');
  label.append(name, meta);

  const button = document.createElement('button');
  button.className = 'btn primary';
  button.textContent = '安装';
  button.addEventListener('click', () => cloudInstall(item, button));

  row.append(label, button);
  return row;
}

async function cloudInstall(item, button) {
  // The config in hand may carry bindings for many games; installing someone's
  // shared setup should not replace the user's whole library, so when the
  // publisher named a game only that game's mappings are taken.
  const scope = item.game_id ? `《${item.game_name || item.game_id}》的映射` : '整份配置';
  if (!confirm(
      `安装「${item.title}」？\n\n` +
      `会应用${scope}，并切换到该游戏。\n` +
      `你现在的配置会先备份到用户目录的 cloud_backup 下，随时可以拿回来。`)) return;

  button.disabled = true;
  const original = button.textContent;
  button.textContent = '安装中…';
  cloudSay('正在下载并校验…');
  try {
    const result = await post('/api/cloud/install', {
      profile_id: item.id,
      game_id: item.game_id || '',
    }, 30000);
    const installed = result.installed;
    cloudSay(
      `已安装「${installed.title}」v${installed.revision_no}` +
      `（校验值 ${installed.sha256.slice(0, 12)}）` +
      (result.backup ? `，原配置已备份到 ${result.backup}` : ''));
    // Re-read the panels the install changed, so the page shows what is now
    // actually loaded rather than what was there before.
    // Re-read whichever panel the install changed, so the page shows what is
    // now actually loaded rather than what was there a moment ago.
    try {
      if (installed.doc_type === 'voice_mappings') {
        await refreshVoice();
        renderVoiceRows(voiceRowsFromStatus(voice.status));
      } else {
        // Motions are not a panel of their own: they are the motion.* rows of
        // the game profile, so refreshing the profile covers them too.
        await refreshProfile();
      }
    } catch { /* the install succeeded; a stale panel is not worth an error */ }
  } catch (error) {
    cloudSay(`安装失败：${error.message}`, 'error');
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

cloudRefreshBtn?.addEventListener('click', cloudRefresh);

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

async function refreshCustomPoses({ rebuild = true } = {}) {
  try {
    const data = await api('/api/pose/custom');
    customPoses = data.poses || [];
    customPoseScores = data.scores || {};
    // 上次选的准备时间。不放回去的话，每次打开都回到默认值，下一次点按钮又把默认
    // 值存回服务端——记住就等于没记。
    const delay = document.getElementById('customPoseDelay');
    if (delay && data.delay_s && [...delay.options].some(option => option.value === String(data.delay_s))) {
      delay.value = String(data.delay_s);
    }
    renderCustomPoses();
    // 触发器列表变了，映射界面要重建才能看到新姿势。轮询刷新分数时不重建，
    // 否则用户正在编辑的那一行会被冲掉。
    if (rebuild) renderProfileBindingRows();
  } catch (error) {
    customPoseSay(error.message, 'error');
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
function paintPoseCountdown(state) {
  const button = document.getElementById('customPoseCaptureBtn');
  if (!button) return;
  const counting = !!state?.counting;
  const purpose = String(state?.purpose || 'capture');
  const wasActive = poseCountdownActive;
  poseCountdownActive = counting;

  if (counting) {
    const left = Math.max(1, Math.ceil(Number(state.remaining_s) || 0));
    if (purpose === 'capture') {
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
    if (message) customPoseSay(message, message.includes('没') || message.includes('失败') ? 'error' : '');
    void refreshCustomPoses();
  }
}

async function removeCustomPoseFrame(item, index) {
  try {
    applyPoses(await post('/api/pose/custom/frame/remove', { id: item.id, index }));
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
    const data = await post('/api/pose/custom/update', Object.assign({ id }, changes));
    customPoses = data.poses || [];
    renderCustomPoses();
    renderProfileBindingRows();
  } catch (error) {
    customPoseSay(error.message, 'error');
  }
}

async function removeCustomPose(item) {
  if (!confirm('删除「' + item.name + '」？绑在它上面的按键映射也会失效。')) return;
  try {
    const data = await post('/api/pose/custom/remove', { id: item.id });
    customPoses = data.poses || [];
    customPoseSay('已删除「' + item.name + '」');
    renderCustomPoses();
    renderProfileBindingRows();
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

function renderCustomPoses() {
  if (!customPoseListEl) return;
  const hint = document.getElementById('customPoseHint');
  if (hint) hint.hidden = !customPoses.length;
  customPoseListEl.replaceChildren();

  for (const item of customPoses) {
    const row = document.createElement('div');
    row.className = 'custom-pose';
    row.dataset.id = item.id;

    const name = document.createElement('input');
    name.className = 'custom-pose-name';
    name.value = item.name;
    name.maxLength = 20;
    name.addEventListener('change', () => updateCustomPose(item.id, { name: name.value }));

    // 实时相似度。没有它，用户调阈值只能靠猜。
    const meter = document.createElement('div');
    meter.className = 'custom-pose-meter';
    const fill = document.createElement('div');
    fill.className = 'custom-pose-fill';
    const readout = document.createElement('span');
    readout.className = 'custom-pose-score';
    meter.append(fill, readout);

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

    const enabled = document.createElement('label');
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.checked = item.enabled;
    toggle.addEventListener('change', () => updateCustomPose(item.id, { enabled: toggle.checked }));
    enabled.append(toggle, '启用');

    const remove = document.createElement('button');
    remove.className = 'btn';
    remove.textContent = '删除';
    remove.addEventListener('click', () => removeCustomPose(item));

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

    // 绑的是哪个键。只显示，不在这里改——同一个东西两处能改，就一定会有一处
    // 是旧的。点它跳到上面那张表里对应的一行，改在那边。
    const bound = document.createElement('button');
    bound.type = 'button';
    bound.className = 'custom-pose-key';
    bound.textContent = libraryKeyLabel('pose.' + item.id);
    bound.title = '点一下跳到上面的按键映射';
    bound.addEventListener('click', () => revealBindingRow('pose.' + item.id));

    const head = document.createElement('div');
    head.className = 'custom-pose-head';
    head.append(name, meter, bound);

    const tools = document.createElement('div');
    tools.className = 'custom-pose-tools';
    tools.append(
      customPoseSlider('像到 ', threshold, v => v + '% 才算'),
      // 帧数对用户没有意义，换算成秒。30fps 是相机的常见帧率。
      customPoseSlider(item.frames > 1 ? '最后一个姿势保持 ' : '保持 ', dwell,
                       v => (v / 30).toFixed(2) + ' 秒'));
    if (item.frames > 1) {
      const window_ = document.createElement('input');
      window_.type = 'range';
      window_.min = '3'; window_.max = '100'; window_.step = '1';  // 0.3 ~ 10 秒
      window_.value = String(Math.round(item.step_window_s * 10));
      window_.addEventListener('change', () =>
        updateCustomPose(item.id, { step_window_s: Number(window_.value) / 10 }));
      tools.append(customPoseSlider('每步最多 ', window_, v => (v / 10).toFixed(1) + ' 秒'));
    }
    tools.append(enabled, remove);

    row.append(strip, head, tools);
    customPoseListEl.appendChild(row);
  }
  paintCustomPoseScores();
}

/** 只改数字和进度条，不重建 DOM——每秒重建会把用户正在拖的滑块打断。 */
function paintCustomPoseScores() {
  if (!customPoseListEl) return;
  const active = new Set(kernelState?.poses_active || []);
  for (const row of customPoseListEl.querySelectorAll('.custom-pose')) {
    const id = row.dataset.id;
    const item = customPoses.find(p => p.id === id);
    const score = Number(customPoseScores[id] ?? 0);
    const fill = row.querySelector('.custom-pose-fill');
    const readout = row.querySelector('.custom-pose-score');
    if (fill) fill.style.width = Math.round(score * 100) + '%';
    if (readout) readout.textContent = Math.round(score * 100) + '%';
    row.classList.toggle('hit', !!(item && score >= item.threshold));
    row.classList.toggle('firing', active.has(id));
    // 键位标签跟着一起刷。上面改了绑定，这里得马上跟上，不然就是两处各说各的。
    const bound = row.querySelector('.custom-pose-key');
    if (bound) bound.textContent = libraryKeyLabel('pose.' + id);
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
const ZONE_NAMES_CN = {leftHand: '左手区', rightHand: '右手区', leftFoot: '左脚区', rightFoot: '右脚区', headJump: '头顶区'};
let ratingNames = {intensity: '运动强度', recognition: '识别度', difficulty: '上手难度'};
let bodyPartNames = {legs: '腿部', glutes: '臀部', core: '核心', arms: '手臂', shoulders: '肩背'};

async function refreshPoseLibrary() {
  if (!poseLibraryEl) return;
  const data = await api('/api/pose/library');
  poseLibrary = data.library || [];
  ratingNames = data.rating_names || ratingNames;
  bodyPartNames = data.body_part_names || bodyPartNames;
  poseLibraryNames.cloud = {...poseLibraryNames.cloud, ...(data.cloud_names || {})};
  if (data.error) poseCloudSay(data.error, 'error');
  renderPoseLibrary();
  // 映射表可能在动作库读回来之前就画好了，那时下载的动作还不在触发器里。
  if (gameProfile.selected && poseLibrary.some(item => item.source === 'cloud')) renderProfileBindingRows();
  paintPoseMissingNotice();
}

/** 下载或删掉一个动作之后：映射表里多一行或少一行。先把没存的改动落盘，再重画。 */
async function afterPoseLibraryChange(library) {
  poseLibrary = library || poseLibrary;
  renderPoseLibrary();
  try { await saveProfileBindings(); } catch { /* 存不上那边自己会报，这里不抢话 */ }
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
function poseRatings(item) {
  const box = document.createElement('div');
  box.className = 'pose-ratings';
  const stars = count => '★'.repeat(count) + '☆'.repeat(Math.max(0, 5 - count));
  for (const [key, label] of Object.entries(ratingNames)) {
    const count = Number(item.ratings?.[key] || 0);
    if (!count) continue;
    const row = document.createElement('div');
    row.className = 'pose-rating';
    row.title = `${label} ${count} 星（满分 5 星）`;
    const name = document.createElement('span');
    name.textContent = label;
    const value = document.createElement('span');
    value.className = 'pose-stars';
    value.textContent = stars(count);
    row.append(name, value);
    box.appendChild(row);
  }
  const parts = Object.entries(item.body_parts || {}).sort((a, b) => b[1] - a[1]);
  if (parts.length) {
    const row = document.createElement('div');
    row.className = 'pose-parts';
    row.textContent = '锻炼：' + parts.map(([key, count]) => `${bodyPartNames[key] || key} ${'★'.repeat(count)}`).join(' · ');
    row.title = '锻炼部位，星越多练得越多';
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

// 「开始」页画面下面那排小标签：本机动作库里有什么就列什么，做着的那个亮起来。
// 名字太长的几个用短一点的叫法，一排放得下。
const TRIGGER_CHIP_NAMES = {march: '踏步', calf_back: '小腿后抬', hands_up: '双手过头'};
function renderTriggerChips() {
  const box = document.getElementById('triggerChips');
  if (!box) return;
  box.replaceChildren(...poseLibrary.map(item => {
    const chip = document.createElement('span');
    chip.className = 'trigger-chip';
    chip.dataset.id = item.id;
    chip.dataset.group = item.group;
    chip.textContent = TRIGGER_CHIP_NAMES[item.id] || item.name;
    return chip;
  }));
}

function renderPoseLibrary() {
  renderTriggerChips();
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
      source.append(`官方动作库 · 第 ${item.revision} 版`);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn pose-library-remove';
      remove.textContent = '删除';
      remove.addEventListener('click', () => removePoseAction(item));
      source.append(remove);
    } else {
      source.textContent = '程序自带';
    }
    card.append(poseDemo(item.demo), poseCardBody(head, how, poseRatings(item)), source);

    // 做这个动作会扫过哪些圈。两边都绑了键时，下面那行 paintPoseLibrary 会写清楚怎么让。
    if ((item.passes_zones || []).length) {
      const zones = document.createElement('div');
      zones.className = 'pose-library-zones';
      zones.textContent = '做的时候会经过：' + item.passes_zones.map(zone => ZONE_NAMES_CN[zone] || zone).join('、');
      card.append(zones);
    }
    poseLibraryEl.appendChild(card);
  }
  paintPoseLibrary();
}

async function removePoseAction(item) {
  const bound = triggerMapped(item.trigger);
  const warning = bound ? '这个游戏里它绑着键，删掉后那一行会失效，直到重新下载。' : '以后要用再从官方动作库下载。';
  if (!confirm(`删掉「${item.name}」？${warning}`)) return;
  try {
    const data = await post('/api/pose/remove', { id: item.id });
    poseCloudSay(`已删掉「${item.name}」`);
    await afterPoseLibraryChange(data.library);
  } catch (error) { poseCloudSay(error.message, 'error'); }
}

/* --- 官方动作库 -----------------------------------------------------------
 * 云端官方发布的动作。点「下载」，电脑那边从云端取回动作文件、验过签名装上，本机
 * 动作库和映射表里就多了它。列表只在点开时读一次，不跟着状态轮询走。
 */
let poseCloudItems = [];

function poseCloudSay(text, kind = '') {
  const el = document.getElementById('poseCloudStatus');
  if (!el) return;
  el.textContent = text;
  el.className = kind === 'error' ? 'statusline error' : 'statusline';
}

async function openPoseCloud() {
  const button = document.getElementById('poseCloudBtn');
  if (button) button.disabled = true;
  poseCloudSay('正在读官方动作库…');
  try {
    const data = await api('/api/pose/cloud', { timeoutMs: 20000 });
    poseCloudItems = data.actions || [];
    ratingNames = data.rating_names || ratingNames;
    bodyPartNames = data.body_part_names || bodyPartNames;
    for (const item of poseCloudItems) poseLibraryNames.cloud[item.id] = item.name;
    const fresh = poseCloudItems.filter(item => !item.installed_revision).length;
    poseCloudSay(poseCloudItems.length
      ? (fresh ? `官方动作库里有 ${poseCloudItems.length} 个动作，${fresh} 个还没下载。` : '官方动作库里的动作都下载了。')
      : '官方动作库里暂时还没有发布的动作。');
    renderPoseCloud();
    paintPoseMissingNotice();
  } catch (error) {
    poseCloudSay(error.message, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

function renderPoseCloud() {
  if (!poseCloudEl) return;
  poseCloudEl.hidden = !poseCloudItems.length;
  poseCloudEl.replaceChildren();
  for (const item of poseCloudItems) {
    const card = document.createElement('div');
    card.className = 'pose-library-item';
    card.dataset.id = item.id;
    const name = document.createElement('span');
    name.className = 'pose-library-name';
    name.textContent = item.name;
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
    action.addEventListener('click', () => installPoseAction(item, action));
    const head = document.createElement('div');
    head.className = 'pose-library-head';
    head.append(name, action);
    const how = document.createElement('div');
    how.className = 'pose-library-how';
    how.textContent = item.how;
    card.append(poseDemo(item.demo), poseCardBody(head, how, poseRatings(item)));
    poseCloudEl.appendChild(card);
  }
}

async function installPoseAction(item, button) {
  button.disabled = true;
  poseCloudSay(`正在下载「${item.name}」…`);
  try {
    const data = await post('/api/pose/cloud/install', { id: item.id }, 20000);
    item.installed_revision = item.revision;
    item.update_available = false;
    poseCloudSay(`「${item.name}」已下载。在上面它的卡片上点「加到映射」就能绑键。`);
    await afterPoseLibraryChange(data.library);
  } catch (error) {
    button.disabled = false;
    poseCloudSay(error.message, 'error');
  }
}

document.getElementById('poseCloudBtn')?.addEventListener('click', openPoseCloud);

/** 这份配置绑了还没下载的动作：那几行现在不会触发。说清楚是哪几个、去哪下载。
 *  不自动下载——以后要和账号、收费一起考虑。 */
function paintPoseMissingNotice() {
  const box = document.getElementById('poseMissingNotice');
  if (!box) return;
  const known = new Set(profileTriggers().map(trigger => trigger.key));
  const missing = [];
  for (const [group, prefix] of [['motions', 'motion'], ['poses', 'pose']]) {
    const items = gameProfile.selected?.bindings?.[group] || {};
    for (const [id, binding] of Object.entries(items)) {
      if (!binding || binding.disabled || !binding.action?.target) continue;
      if (id.startsWith('custom') || known.has(`${prefix}.${id}`)) continue;
      missing.push(poseLibraryNames.cloud[id] || id);
    }
  }
  box.hidden = !missing.length;
  const text = missing.length
    ? `这份配置用到了还没下载的动作：${missing.join('、')}。${missing.length > 1 ? '它们' : '它'}现在不会触发，先到下面的「官方动作库」下载。`
    : '';
  const label = box.querySelector('span');
  if (label && label.textContent !== text) label.textContent = text;
}
document.getElementById('poseMissingGo')?.addEventListener('click', () => {
  document.getElementById('poseCloudPanel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (!poseCloudItems.length) openPoseCloud();
});

/** 圈给动作让路时说的那句话。电脑那边算好哪些圈在让、让谁，这里只翻译成人话。 */
function zoneYieldText(trigger) {
  const overlaps = kernelState?.zone_overlaps || {};
  const delayed = [], paused = [], sharing = [];
  for (const [zone, info] of Object.entries(overlaps)) {
    if (!(info.triggers || []).includes(trigger)) continue;
    const name = ZONE_NAMES_CN[zone] || zone;
    if (!info.yields) sharing.push(name);
    else if (info.delay) delayed.push(name);
    else paused.push(name);
  }
  const wait = Math.round(Number(kernelState?.zone_yield_s || 0.25) * 100) / 100;
  const parts = [];
  if (delayed.length) parts.push(`${delayed.join('、')}也绑了键：做这个动作时不按，平时会晚 ${wait} 秒按下，免得误触`);
  if (paused.length) parts.push(`${paused.join('、')}也绑了键：做这个动作时不按`);
  if (sharing.length) parts.push(`${sharing.join('、')}也绑了键：做这个动作时会一起按到`);
  return parts.join('；');
}

/** 只改键位和提醒，不重建。 */
function paintPoseLibrary() {
  if (!poseLibraryEl) return;
  for (const card of poseLibraryEl.querySelectorAll('.pose-library-item')) {
    const item = poseLibrary.find(entry => entry.id === card.dataset.id);
    if (!item) continue;
    const bound = card.querySelector('.custom-pose-key');
    const label = libraryKeyLabel(item.trigger);
    if (bound && bound.textContent !== label) bound.textContent = label;
    let warn = card.querySelector('.pose-library-warn');
    const text = triggerMapped(item.trigger) ? zoneYieldText(item.trigger) : '';
    if (text && !warn) {
      warn = document.createElement('div');
      warn.className = 'pose-library-warn';
      card.append(warn);
    }
    if (warn) {
      warn.hidden = !text;
      if (warn.textContent !== text) warn.textContent = text;
    }
  }
  paintZoneYieldNotice();
}

/** 映射表上方那一行：绑了会互相碰到的动作和圈时，在绑键的地方就说清楚。 */
function paintZoneYieldNotice() {
  const box = document.getElementById('zoneYieldNotice');
  if (!box) return;
  const lines = poseLibrary
    .filter(item => triggerMapped(item.trigger))
    .map(item => {
      const text = zoneYieldText(item.trigger);
      return text ? `「${item.name}」和${text}` : '';
    })
    .filter(Boolean);
  const text = lines.join('\n');
  box.hidden = !text;
  if (box.textContent !== text) box.textContent = text;
}
document.getElementById('cloudSiteBtn')?.addEventListener('click', () => openOnSite('/'));

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
  ['keyboard', '键盘'], ['mouse_button', '鼠标按键'], ['mouse_wheel', '鼠标滚轮'],
  ['gamepad', 'Xbox 按键'], ['gamepad_trigger', 'Xbox 扳机'], ['gamepad_axis', 'Xbox 左摇杆'],
  ['macro', '跑另一条宏'],
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

async function refreshMacros({ rebuild = true } = {}) {
  try {
    const data = await api('/api/macros');
    macroLibrary.items = data.macros || [];
    macroLibrary.limits = data.limits || null;
    if (data.error) macroSay(data.error, 'error');
  } catch (error) {
    macroSay(error.message, 'error');
    return;
  }
  renderMacros();
  if (rebuild) await rebuildBindingRowsAfterMacroChange();
}

/** 宏改了，上面每一行映射里的下拉、以及「跑一遍还是循环」那一格都要跟着变。
 *  重画之前先把还没存的编辑落盘，否则会把用户手上的草稿抹掉。 */
async function rebuildBindingRowsAfterMacroChange() {
  try { await saveProfileBindings(); } catch { /* 存不上那边自己会报，这里不抢话 */ }
  renderProfileBindingRows();
}

async function macroWrite(route, body) {
  const data = await post('/api/macros/' + route, body);
  macroLibrary.items = data.macros || [];
  renderMacros();
  await rebuildBindingRowsAfterMacroChange();
  return data;
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

/* --- 触发实况 -----------------------------------------------------------
 * 「我刚才那个动作到底有没有生效、按的是哪个键」——这件事以前没地方看。
 *
 * 光靠轮询状态是看不见的：区域按下去十几毫秒就松开，姿势是边沿触发，语音更是说完
 * 就完。所以服务端记一份最近触发过什么（control_kernel.recent_triggers），这里
 * 只负责把它画出来。
 *
 * 它就放在映射表正上方，不另开一页：看到「左手区 → Y」不对，往下一眼就是改它的
 * 那一行。点一条还能直接跳过去。两件事本来就是同一件事，分两个地方只会让人来回找。
 */
// 默认只给绑了键的；「动作测试」页要的是认出来的全部，传 all。区域的 pressed 本身
// 就不含没绑键的，那一页要看 recognized。
function activeTriggerKeys({all = false} = {}) {
  const k = kernelState || {};
  const out = new Set();
  for (const [id, zone] of Object.entries(k.zones || {})) {
    if (all ? (zone?.recognized ?? zone?.pressed) : zone?.pressed) out.add('zone.' + id);
  }
  for (const id of k.motions || []) out.add('motion.' + id);
  for (const id of k.poses_active || []) out.add('pose.' + id);
  return all ? out : new Set([...out].filter(triggerMapped));
}

function agoText(seconds) {
  if (seconds < 0.8) return '刚刚';
  if (seconds < 60) return `${Math.round(seconds)} 秒前`;
  return `${Math.round(seconds / 60)} 分钟前`;
}

/** 触发之后高亮多久。太短了人还没把视线从镜头挪回屏幕就已经灭了。 */
const TRIGGER_FLASH_S = 1.2;

function renderTriggerLive() {
  // 页上可能有两块：「开始」那页一块，映射表上方一块。两块写同一份东西。
  //
  // 为什么要两块：你站在摄像头前做动作时人在「开始」页，而改键在「本游戏」页。
  // 只放映射表旁边的话，等你切过去，刚才那一下已经过去了。
  const boxes = [...document.querySelectorAll('.trigger-live')].filter(box => !box.hidden);
  const names = new Map(profileTriggers().map(item => [item.key, item.name]));
  const bindings = bindingsForDisplay();
  const now = Number(kernelState?.now) || 0;
  // 记录里的动作是触发那一刻绑的键；没绑的那几条不在这里显示，只在「动作测试」页。
  const events = (kernelState?.recent_triggers || []).filter(event => actionKeyText(event.action));
  const active = [...activeTriggerKeys()].filter(key => names.has(key));

  for (const box of boxes) {
    const nowEl = box.querySelector('.trigger-live-now');
    if (nowEl) {
      // 现在按着的写大字：这时候人站在几米外，小字看不见。
      nowEl.textContent = active.length
        ? active.map(key => `${names.get(key)} → ${actionKeyText(bindings[key]?.action) || '未映射'}`).join('　')
        : '还没有触发';
      nowEl.classList.toggle('idle', !active.length);
    }

    const log = box.querySelector('.trigger-live-log');
    if (!log) continue;
    log.replaceChildren();
    for (const event of events.slice(-6).reverse()) {
      const key = String(event.trigger || '');
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'trigger-live-item';
      row.textContent = `${agoText(Math.max(0, now - Number(event.at || 0)))} · `
        + `${event.label || names.get(key) || key} → ${actionKeyText(event.action) || '未映射'}`;
      row.title = '点一下跳到它的映射那一行';
      row.addEventListener('click', () => revealBindingRow(key));
      log.appendChild(row);
    }
    if (!events.length) {
      const empty = document.createElement('span');
      empty.className = 'fineprint';
      empty.textContent = '做个动作或者说句口令，这里会记下来。';
      log.appendChild(empty);
    }
  }

  // 每一行自己亮。一直按着的那些常亮，点一下就过的那些闪一下——后者没有这个
  // 闪，在 250ms 的轮询里根本抓不到。
  const fresh = new Set(events.filter(event => now - Number(event.at || 0) <= TRIGGER_FLASH_S)
                              .map(event => String(event.trigger || '')));
  const held = activeTriggerKeys();
  for (const row of document.querySelectorAll('.binding-row')) {
    const key = row.dataset.trigger;
    row.classList.toggle('firing', held.has(key));
    row.classList.toggle('just-fired', !held.has(key) && fresh.has(key));
  }
}

/* --- 动作测试 ---------------------------------------------------------------
 * 站到镜头前做个动作，看打中了什么、按的是哪个键。
 *
 * 它单独一个页签，不挤在映射表旁边。两个原因：
 *
 * 一是你做动作的时候人在几米外，字必须大。挤在编辑器旁边的那种一行小字，走近了
 * 才看得清，而走近了就做不成动作了。
 * 二是"试"和"改"是两回事。试的时候要看的是"这一下到底认出来没有"，改的时候要看的
 * 是一整张表。混在一起两边都憋屈。
 *
 * 靶子墙上把没映射的也列出来，这是整个页面最有用的一条信息：动作亮了、键位写着
 * 「未映射」，说明识别是好的，只是没绑——而这两种情况在游戏里的表现完全一样，
 * 都是"我做了但没反应"。
 */

/** 打中之后大字停留多久。太短了人还没把视线从镜头挪回屏幕就已经没了。 */
const RANGE_HIT_HOLD_S = 3.0;
/** 靶子亮多久。比大字短，因为连着做动作时它要跟得上。 */
const RANGE_FLASH_S = 0.8;

const RANGE_GROUPS = [
  { key: 'zones', name: '身体区域' },
  { key: 'motions', name: '身体动作' },
  { key: 'poses', name: '自定义动作' },
  { key: 'voice', name: '本游戏口令' },
];

let rangeTargetKeys = '';

function rangeTriggers() {
  const bindings = bindingsForDisplay();
  return profileTriggers().filter(item => {
    // 身体那三类全列出来——"动作认出来了但没绑键"正是这一页要让人看见的。
    if (item.group !== 'voice') return true;
    // 语音有二十多条，全铺上去就成了一面墙。只列绑了键的。
    const binding = bindings[item.key];
    return !!(binding && !binding.disabled && binding.action);
  });
}

function buildRangeTargets(triggers) {
  const wall = document.getElementById('rangeTargets');
  if (!wall) return;
  wall.replaceChildren();
  for (const group of RANGE_GROUPS) {
    const items = triggers.filter(item => item.group === group.key);
    if (!items.length) continue;
    const block = document.createElement('div');
    block.className = 'range-group';
    const title = document.createElement('div');
    title.className = 'range-group-title';
    title.textContent = group.name;
    const grid = document.createElement('div');
    grid.className = 'range-grid';
    for (const item of items) {
      const target = document.createElement('button');
      target.type = 'button';
      target.className = 'range-target';
      target.dataset.trigger = item.key;
      target.title = '点一下去改它的键';
      const name = document.createElement('span');
      name.className = 'range-target-name';
      name.textContent = item.name;
      const key = document.createElement('b');
      key.className = 'range-target-key';
      target.append(name, key);
      target.addEventListener('click', () => revealBindingRow(item.key));
      grid.appendChild(target);
    }
    block.append(title, grid);
    wall.appendChild(block);
  }
}

function renderRange() {
  const panel = document.getElementById('rangePanel');
  if (!panel || panel.hidden) return;

  const triggers = rangeTriggers();
  // 只在靶子本身变了的时候重建。每 250ms 重建一次的话，鼠标压根点不中。
  const signature = triggers.map(item => item.key).join('|');
  if (signature !== rangeTargetKeys) {
    rangeTargetKeys = signature;
    buildRangeTargets(triggers);
  }

  const names = new Map(triggers.map(item => [item.key, item.name]));
  const bindings = bindingsForDisplay();
  const now = Number(kernelState?.now) || 0;
  const events = kernelState?.recent_triggers || [];
  const held = activeTriggerKeys({all: true});

  const warn = document.getElementById('rangeOutputWarn');
  // 输出关着的时候这一页照样亮——这正是人要排查的那种情况，所以说清楚。
  if (warn) warn.hidden = !!output.enabled;

  // 大字：正按着的优先，其次是刚打中的那一下。
  const holding = [...held].filter(key => names.has(key));
  const latest = events.length ? events[events.length - 1] : null;
  const since = latest ? Math.max(0, now - Number(latest.at || 0)) : Infinity;
  const what = document.getElementById('rangeHitWhat');
  const when = document.getElementById('rangeHitWhen');
  const hit = document.getElementById('rangeHit');
  if (holding.length) {
    what.textContent = holding
      .map(key => `${names.get(key)} → ${actionKeyText(bindings[key]?.action) || '未映射'}`)
      .join('　');
    when.textContent = '正按着';
    hit.className = 'range-hit on';
  } else if (latest && since <= RANGE_HIT_HOLD_S) {
    const key = String(latest.trigger || '');
    what.textContent = `${latest.label || names.get(key) || key} → ${actionKeyText(latest.action) || '未映射'}`;
    when.textContent = agoText(since);
    hit.className = 'range-hit on';
  } else if (latest) {
    const key = String(latest.trigger || '');
    what.textContent = `${latest.label || names.get(key) || key} → ${actionKeyText(latest.action) || '未映射'}`;
    when.textContent = agoText(since);
    hit.className = 'range-hit';
  } else {
    what.textContent = '站到镜头前，做个动作试试';
    when.textContent = '';
    hit.className = 'range-hit';
  }

  // 靶子：按着的常亮，刚打中的闪一下。没绑键的写「未映射」并且压暗。
  const fresh = new Set(events.filter(event => now - Number(event.at || 0) <= RANGE_FLASH_S)
                              .map(event => String(event.trigger || '')));
  for (const target of document.querySelectorAll('.range-target')) {
    const key = target.dataset.trigger;
    const text = actionKeyText(bindings[key]?.action);
    target.querySelector('.range-target-key').textContent = text || '未映射';
    target.classList.toggle('unmapped', !text);
    target.classList.toggle('on', held.has(key));
    target.classList.toggle('flash', !held.has(key) && fresh.has(key));
  }

  const log = document.getElementById('rangeLog');
  if (!log) return;
  log.replaceChildren();
  if (!events.length) {
    const empty = document.createElement('span');
    empty.className = 'fineprint';
    empty.textContent = '还没打中过。';
    log.appendChild(empty);
    return;
  }
  for (const event of events.slice(-8).reverse()) {
    const key = String(event.trigger || '');
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'range-log-item';
    row.textContent = `${agoText(Math.max(0, now - Number(event.at || 0)))} · `
      + `${event.label || names.get(key) || key} → ${actionKeyText(event.action) || '未映射'}`;
    row.title = '点一下去改它的键';
    row.addEventListener('click', () => revealBindingRow(key));
    log.appendChild(row);
  }
}
