// 页面外壳：页签、菜单、白天/夜间、教学、事件绑定、启动。
import {createTutorial} from '../tutorial.js';
import {fistHands,intentAction,renderBodyDescs,zoneFit} from './body.js';
import {cloudRefresh,loadCloudEndpoint} from './cloud.js';
import {$,api,autosaver,clamp,notice,post} from './core.js';
import {emergencyStop,emergencyStops,inputStatus,refreshAudioDevices,refreshCameraConfig,refreshInput,refreshOutput,refreshPerformance,refreshStereo,refreshXinput,renderCameraDevices,renderCameraRotation,renderInputStatus,renderOutput,renderStereo,setOutput,setSource,setXinputMerge,setXinputMotionLeft,stereoState,syncCameraDeviceRow} from './devices.js';
import {cancelPoseRecord,refreshPoseRecord,refreshTriggerRecord,startPoseRecord} from './diagnostics.js';
import {BODY_ZONES,actionKeyText,profileTriggers,zoneKeyLabel} from './labels.js';
import {refreshCustomPoses,refreshPoseLibrary} from './library.js';
import {refreshMacros} from './macros.js';
import {addCustomGame,applySelectedProfile,changeGameLaunchMode,lastProfileQuery,profileApplies,profileSwitching,refreshProfile,removeCustomGame,renameCustomGame,resetProfileBindings,retryProfileBindings,scheduleProfileAutoSave,searchProfiles,showMapTab,syncMotionConflictChoices,syncVoiceReleaseChoices,toggleGameLaunchMode,updateMapCounts} from './mapping.js';
import {cameraInfo,cameraRunning,cancelLiveZones,centerHead,currentPoseMap,endLiveZoneDrag,followZones,kernelState,moveLiveZoneDrag,moveZonesHere,nudgeRect,openLiveZoneEditor,overlay,rectEdit,refreshKernel,refreshPreview,renderKernelState,renderMainStatus,saveLiveZones,sessionStarted,sourceMode,startCalibration,startLiveZoneDrag,toggleOverlay,zoneEditMode} from './play.js';
import {S,cameraScan,gameProfile,head,output,profileDirty} from './state.js';
import {handMouseConfig,initViewControl,pushHeadConfig,refreshHandMouse,reloadViewControlState,renderViewControl,saveHandMouseFields,saveViewControlAxis,setViewControlBusy,syncControlLabels} from './view-control.js';
import {addVoiceRow,refreshVoice,refreshVoiceCommands,renderVoiceRows,saveVoiceMappings,voice,voiceInputReady,voiceRowsFromStatus} from './voice.js';

let modelAvailable=false;
export let actionBusy=false;
export let currentView='play';
export let profileReady=false;
export let profileLoading=false;

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
  // 头控左右的读数上限是左右灵敏度；握拳的上限是握拳灵敏度/100；抬头低头的上限是
  // 上下灵敏度/100。
  const handMax=Math.min(1,Math.max(.01,Number(handMouseConfig.sensitivity??hand.config?.sensitivity??70)/100));
  const hLevel=horizontalHand?(finite(axes.horizontal?.output)?Number(axes.horizontal.output)/handMax:null)
    :(finite(hs.output_x)?Number(hs.output_x)/Math.max(1,Number(hs.sensitivity_x||head.sensitivityX||58)):null);
  const pitchMax=clamp(Number(hs.sensitivity_y||head.sensitivityY||46)/100,.15,1);
  const vLevel=verticalHand?(finite(axes.vertical?.output)?Number(axes.vertical.output)/handMax:null)
    :(head.verticalLookEnabled&&finite(hs.output_y)?Number(hs.output_y)/pitchMax:null);
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
    cameraRunning,cameraIndex:cameraInfo?.camera_index??S.cameraIndex,cameraError:cameraRunning?'':String(cameraInfo?.last_error||''),
    // 模型路径随第一次状态一起来；还没来之前是「不知道」，不能当成「没装」。
    modelOk:cameraInfo?!!cameraInfo.model_path:null,
    scan:cameraScan.state,scanCount:cameraScan.count,
    phoneStreaming:phoneLive||!!inputStatus.phone_ignored,usbTether:!!inputStatus.usb_tether?.present,
    posed:!!currentPoseMap,headShoulders:seen('nose')&&seen('left_shoulder')&&seen('right_shoulder'),
    // 握拳控左右不需要头部中心，这一点和「视角控制」那块的判断保持一致。
    calibrated:horizontalHand?true:!!(hs.horizontal_calibrated??hs.calibrated),
    calibrating:!!hs.calibrating,calibrationNote:String(hs.notice||''),
    horizontal:horizontalHand||(head.enabled?(['roll_tilt','head_responsive'].includes(head.horizontalAlgorithm)?head.horizontalAlgorithm:'head_turn'):'off'),
    hLevel,hHandState:String(axes.horizontal?.state||''),guardBlocked:!!hs.horizontal_paused_by_body_motion,
    vertical:verticalHand||(head.verticalLookEnabled?'head':'off'),
    vLevel,vHandState:String(axes.vertical?.state||''),gateActive:!!k.vertical_gate_active,
    zones,
    outputEnabled:!!output.enabled,driverMissing:output.mode==='gamepad'&&output.server?.vigembus_running===false,
    stops:emergencyStops,
    // 「量身」：电脑那边量到哪一步了；握拳量哪几只手；固定区域时量的是跟随那一套。
    fit:k.zone_fit||{},fistHands:fistHands(),zonesFrozen:!!k.zones_frozen,
    // 「录我的动作」：电脑那边录到哪一项了；全部要录的有哪些。
    intent:k.intent_recording||{},intentItems:k.intent_items||{},
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
    gamePickerOpen:!$('#gamePicker')?.hidden,
  };
}

async function handleMainAction(){
  if(!sessionStarted&&!inputStatus.handheld_connected&&!voiceInputReady){
    await setSource($('#poseSource').value,true);
    return;
  }
  await setOutput(!output.enabled);
}

export async function runAction(action){
  if(actionBusy)return;
  actionBusy=true;renderMainStatus();
  try{await action()}catch(error){notice(error.message||'操作失败，请重试')}
  finally{actionBusy=false;renderMainStatus()}
}

function bind(id,action){$('#'+id).addEventListener('click',()=>runAction(action))}

export function showView(view){
  // 「动作测试」已经并进开始页：画面上大字显示触发，右边列最近触发。
  if(view==='range')view='play';
  if(zoneEditMode&&view!=='play'){notice('先保存或取消区域调整');return}
  currentView=view;
  document.querySelectorAll('[data-panel]').forEach(el=>el.hidden=el.dataset.panel!==view);
  document.querySelectorAll('[data-view]').forEach(el=>{
    if(el.dataset.view===view)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');
  });
  closeMenus();
  if(view==='games')setTimeout(()=>showTipOnce('gameMenu',$('#gameMenuBtn'),'恢复默认按键、管理员权限启动在这里'),400);
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

export async function init(){
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
    refreshInput(),refreshAudioDevices(),refreshXinput(),refreshVoice(),refreshVoiceCommands(),refreshCameraConfig(),refreshPoseLibrary(),refreshTriggerRecord(),
    reloadViewControlState().then(()=>{setViewControlBusy(false);renderViewControl(true)}),
    api('/api/models').then(data=>{
      modelAvailable=!!data.models?.[0]?.available;
      if(data.version)$('#appVersion').textContent=data.version;
      $('#modelStatus').textContent=modelAvailable?'':'这台电脑的人体识别模型用不了，只能用手机摄像头';
    }),
  ]);
  if(results.some(result=>result.status==='rejected'))notice('部分设备信息尚未读取，可继续使用已连接的输入');
  await loadProfiles();renderVoiceRows(voiceRowsFromStatus(voice.status));renderBodyDescs();
  if(currentView==='play')setTimeout(()=>showTipOnce('help',$('#helpBtn'),'新手教学、白天 / 夜间在这里'),1200);
  poll(refreshKernel,250);poll(async()=>{await refreshInput();await refreshOutput();await refreshVoice()},900);
  poll(refreshXinput,1500,()=>currentView==='devices');
  // 帧数写在开始页的画面角上，诊断细节在设置里；这两页开着才刷新。
  poll(refreshPerformance,1500,()=>currentView==='devices'||currentView==='play');
  poll(refreshStereo,300,()=>currentView==='devices');
  poll(refreshPreview,150);
}

export async function loadProfiles(){
  if(profileLoading||profileReady)return;
  profileLoading=true;
  try{await refreshVoiceCommands();await refreshProfile();profileReady=true}
  catch(error){$('#profileMeta').textContent='游戏配置尚未读取，将自动重试：'+error.message}
  finally{profileLoading=false}
}

export const headSaver=autosaver(pushHeadConfig,'headSaveStatus','retryHeadBtn',value=>{S.headDirty=value});

const voiceSaver=autosaver(saveVoiceMappings,'voiceSaveStatus','retryVoiceBtn');

document.querySelectorAll('[data-view],[data-go]').forEach(el=>el.addEventListener('click',()=>showView(el.dataset.view||el.dataset.go)));

bind('mainActionBtn',handleMainAction);

bind('sourceStartBtn',()=>setSource($('#poseSource').value,true));

bind('sourceStopBtn',()=>setSource(sourceMode,false));

$('#audioSource').addEventListener('change',e=>runAction(async()=>{
  const value=e.target.value;
  const result=await post('/api/input/source',{audio_source:value});
  S.audioSource=result.audio_source||value;S.audioMode=result.audio_mode||'waiting';
  renderInputStatus(result);
  notice(value==='phone'?'已选择手机麦克风，等待手机连接':'已选择电脑麦克风');
}));

$('#audioDevice').addEventListener('change',e=>runAction(async()=>{
  const value=e.target.value;
  const result=await post('/api/input/audio-device',{audio_device:value||null});
  renderInputStatus(result);
  notice(value?'已切换电脑音频输入设备':'已恢复系统默认音频输入设备');
}));

bind('overlayBtn',toggleOverlay);

for(const [id,key,digits] of [['handMouseSensitivity','sensitivity',0],['handMouseDeadzone','deadzone',2]]){
  $('#'+id).addEventListener('input',e=>{$('#'+id+'Value').textContent=Number(e.target.value).toFixed(digits)});
  $('#'+id).addEventListener('change',e=>void saveHandMouseFields({[key]:Number(e.target.value)}));
}

for(const axis of ['horizontal','vertical']){
  $('#view'+(axis==='horizontal'?'Horizontal':'Vertical')+'Source').addEventListener('change',()=>void saveViewControlAxis(axis));
}

// 教学只指路不代劳：连接、校准都由人去点真按钮。它自己只会做一件事——扫一遍
// 摄像头，好知道该建议什么。
export const tutorial=createTutorial({state:tutorialState,scanCameras,
  zoneFitStart:()=>zoneFit('start'),zoneFitGripOnly:()=>zoneFit('start',{body:false}),
  zoneFitSkip:()=>zoneFit('skip'),zoneFitCancel:()=>zoneFit('cancel'),
  intentStart:keys=>intentAction('start',keys?{keys}:{}),intentSkip:()=>intentAction('skip'),
  intentCancel:()=>intentAction('cancel'),reveal:revealTargets});

$('#zoneFitBtn').addEventListener('click',e=>tutorial.openLesson('fit',e.currentTarget));

$('#zoneFitGripBtn').addEventListener('click',e=>tutorial.openLesson('fit',e.currentTarget,{gripOnly:true}));

bind('zoneFitResetBtn',async()=>{renderKernelState(await post('/api/zones/fit/reset',{}));notice('区域已恢复默认大小。')});

$('#tutorialBtn').addEventListener('click',()=>{closeMenus();tutorial.open($('#helpBtn'))});

bind('poseRecordBtn',startPoseRecord);

bind('poseRecordCancelBtn',cancelPoseRecord);

$('#profileSearch').addEventListener('keydown',e=>{if(e.key==='Enter'){clearTimeout(profileSearchTimer);void runAction(searchProfiles)}});

let profileSearchTimer=0;

$('#profileSearch').addEventListener('input',()=>{clearTimeout(profileSearchTimer);profileSearchTimer=setTimeout(()=>searchProfiles().catch(error=>notice(error.message)),250)});

// 选游戏只有一个列表：点哪个就换成哪个。
$('#profileResults').addEventListener('click',event=>{
  const item=event.target.closest('.game-result');if(!item||item.disabled||profileSwitching)return;
  $('#profileSelect').value=item.dataset.id;
  void runAction(async()=>{await applySelectedProfile();togglePanel('switchGameBtn','gamePicker',false)});
});

bind('resetProfileBindingsBtn',resetProfileBindings);

bind('profileLaunchBtn',toggleGameLaunchMode);
bind('adminRestartBtn',()=>changeGameLaunchMode(true));

bind('customGameAddBtn',addCustomGame);

bind('customGameRenameBtn',renameCustomGame);

bind('customGameRemoveBtn',removeCustomGame);

$('#customGameName').addEventListener('keydown',e=>{if(e.key==='Enter')void runAction(addCustomGame)});

$('#customGameAppid').addEventListener('keydown',e=>{if(e.key==='Enter')void runAction(addCustomGame)});

bind('retryProfileSaveBtn',retryProfileBindings);

for(const event of ['input','change'])$('#profileBindingRows').addEventListener(event,e=>{syncMotionConflictChoices();if(e.target.closest('.binding-row[data-trigger^="voice."]'))syncVoiceReleaseChoices();scheduleProfileAutoSave(e)});

bind('adjustZonesBtn',openLiveZoneEditor);
bind('followZonesBtn',followZones);
bind('zoneEditFollowBtn',followZones);
bind('saveLiveZonesBtn',saveLiveZones);
bind('cancelLiveZonesBtn',cancelLiveZones);

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

// 抬头低头的中心稳定区。在「设置 → 视角」里，拖完就存。
for(const [id,key] of [['verticalDeadzone','deadzone']]){
  const input=$('#'+id);if(!input)continue;
  input.addEventListener('input',()=>{$('#'+id+'Value').textContent=input.value+'%'});
  input.addEventListener('change',()=>runAction(async()=>{renderKernelState(await post('/api/vertical-look',{[key]:Number(input.value)/100}))}));
}

$('#stopBtn').addEventListener('click',emergencyStop);

bind('calBtn',async()=>{await setOutput(false);await startCalibration()});
bind('centerBtn',centerHead);

bind('calibrationCancel',startCalibration);

$('#calibrationOverlay').addEventListener('cancel',e=>{e.preventDefault();void runAction(startCalibration)});

$('#voiceCommandsBtn').addEventListener('click',()=>$('#voiceCommandsMask').showModal());

$('#closeVoiceCommandsBtn').addEventListener('click',()=>$('#voiceCommandsMask').close());

$('#poseSource').addEventListener('change',()=>{
  S.desiredSource=$('#poseSource').value;$('#phoneGuide').open=S.desiredSource==='phone';
  syncCameraDeviceRow();renderInputStatus(inputStatus);
  notice('已选'+(S.desiredSource==='phone'?'手机摄像头':'电脑摄像头')+'，点「连接」生效');
});

$('#cameraDevice').addEventListener('change',e=>runAction(async()=>{
  const data=await post('/api/camera/config',{index:Number(e.target.value)});
  S.cameraIndex=Number(data.camera_index??e.target.value);
  notice('已选摄像头 '+S.cameraIndex+'，点「连接」看画面对不对');
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
    status.textContent=n?`找到 ${n} 个，选一个再点「连接」`
      :'一个也没找到：可能被别的软件占着，或者改用手机摄像头';
  }catch(e){Object.assign(cameraScan,{state:'failed',error:String(e?.message||e)});if(status)status.textContent='扫描失败：'+cameraScan.error}
}

bind('cameraScanBtn',scanCameras);

// 开启要启动电脑摄像头和识别模型，十几秒都正常。
bind('stereoToggleBtn',async()=>{
  if(!stereoState?.enabled)$('#stereoStatus').textContent='正在启动电脑摄像头…';
  renderStereo(await post('/api/stereo',{enabled:!stereoState?.enabled},60000));
});

bind('stereoCalibrateBtn',async()=>{
  const collecting=stereoState?.state==='collecting';
  renderStereo(await post('/api/stereo/calibrate',collecting?{cancel:true}:{duration_s:30}));
});

bind('copyPhoneUrlBtn',async()=>{await navigator.clipboard.writeText($('#phoneWsUrl').value);notice('连接地址已复制')});

$('#cameraRotation').addEventListener('change',e=>runAction(async()=>{
  renderCameraRotation(await post('/api/camera/config',{rotation:e.target.value}));
  notice(e.target.value==='auto'?'已改为自动转正：看到人以后会自己判断方向':'画面方向已保存，立即生效');
}));

$('#cameraBackend').addEventListener('change',e=>runAction(async()=>{
  await post('/api/camera/config',{backend:e.target.value});notice('采集方式已保存，下次连接时生效');
}));

$('#outputMode').addEventListener('change',e=>runAction(async()=>{
  ++S.outputEpoch;renderOutput(await post('/api/output/config',{mode:e.target.value}));await refreshXinput();
}));

$('#xinputMerge').addEventListener('change',()=>runAction(setXinputMerge));

$('#xinputMotionLeft').addEventListener('change',()=>runAction(setXinputMotionLeft));

$('#strength').addEventListener('input',()=>{$('#strengthValue').textContent=$('#strength').value+'%'});

$('#strength').addEventListener('change',()=>runAction(async()=>{
  const gain=Number($('#strength').value)/100;++S.outputEpoch;
  renderOutput(await post('/api/output/config',{mouse_speed_x:600*gain,mouse_speed_y:450*gain,gamepad_gain:gain}));
}));

for(const id of ['headAlgorithm','verticalExclusive','bodyMotionGuard','deadzone','speedX','speedY','invertY']){
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

/* --- 顶栏菜单、白天/夜间、设置分类、本游戏页签 ------------------------------------ */
// 第一次用新界面时，指一下收在小按钮里的东西在哪：看过一次、点过那个按钮，就不再出现。
const TIPS_KEY='motioncontrol_tips_seen';

function tipsSeen(){try{return new Set(JSON.parse(localStorage.getItem(TIPS_KEY)||'[]'))}catch{return new Set()}}

function markTipSeen(id){try{const seen=tipsSeen();seen.add(id);localStorage.setItem(TIPS_KEY,JSON.stringify([...seen]))}catch{}}

let activeTip=null;

function showTipOnce(id,anchor,text){
  if(activeTip||tipsSeen().has(id)||!anchor||!$('#tour').hidden)return;
  if(!anchor.getBoundingClientRect().width)return;
  const tip=document.createElement('div');tip.className='coach-tip';tip.setAttribute('role','status');
  const words=document.createElement('span');words.textContent=text;
  const ok=document.createElement('button');ok.type='button';ok.className='btn';ok.textContent='知道了';
  tip.append(words,ok);document.body.append(tip);
  const place=()=>{
    const box=anchor.getBoundingClientRect();
    const left=Math.max(12,Math.min(box.right-tip.offsetWidth+12,innerWidth-tip.offsetWidth-12));
    tip.style.top=`${box.bottom+10}px`;tip.style.left=`${left}px`;
    tip.style.setProperty('--arrow-x',`${box.left+box.width/2-left}px`);
  };
  const close=()=>{markTipSeen(id);tip.remove();activeTip=null;removeEventListener('resize',place);removeEventListener('scroll',place,true);anchor.removeEventListener('click',close)};
  place();activeTip={id,close};
  ok.addEventListener('click',close);anchor.addEventListener('click',close);
  addEventListener('resize',place);addEventListener('scroll',place,true);
}

function closeMenus(except){
  document.querySelectorAll('.menu').forEach(menu=>{
    if(menu===except)return;
    menu.hidden=true;menu.parentElement?.querySelector('[aria-haspopup]')?.setAttribute('aria-expanded','false');
  });
}

for(const [buttonId,menuId] of [['helpBtn','helpMenu'],['gameMenuBtn','gameMenu']]){
  const button=$('#'+buttonId),menu=$('#'+menuId);
  button.addEventListener('click',event=>{event.stopPropagation();const open=menu.hidden;closeMenus(menu);menu.hidden=!open;button.setAttribute('aria-expanded',String(open))});
  menu.addEventListener('click',event=>{if(event.target.closest('.menu-item'))closeMenus()});
}

document.addEventListener('click',event=>{if(!event.target.closest('.menu-wrap'))closeMenus()});

document.addEventListener('keydown',event=>{if(event.key==='Escape')closeMenus()});

// 白天（月白）/ 夜间（石墨）。默认跟着系统；手动选的记在这台电脑的浏览器里。
const THEME_KEY='motioncontrol_theme';

function applyTheme(choice){
  const fixed=choice==='light'||choice==='dark';
  if(fixed)document.documentElement.dataset.theme=choice;else delete document.documentElement.dataset.theme;
  try{if(fixed)localStorage.setItem(THEME_KEY,choice);else localStorage.removeItem(THEME_KEY)}catch{}
  document.querySelectorAll('#themeSeg [data-theme-choice]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.themeChoice===(fixed?choice:'auto'))));
}

document.querySelectorAll('#themeSeg [data-theme-choice]').forEach(button=>button.addEventListener('click',event=>{event.stopPropagation();applyTheme(button.dataset.themeChoice)}));

applyTheme(document.documentElement.dataset.theme||'auto');

// 设置页左边的分类，一次只看一类。
export function showSettingsPane(name){
  const known=[...document.querySelectorAll('.settings-pane')].some(pane=>pane.dataset.pane===name);
  const pane=known?name:'devices';
  document.querySelectorAll('#settingsNav [data-pane]').forEach(button=>button.setAttribute('aria-current',String(button.dataset.pane===pane)));
  document.querySelectorAll('.settings-pane').forEach(section=>{section.hidden=section.dataset.pane!==pane});
  if(pane==='lab')void refreshPoseRecord();
}

document.querySelectorAll('#settingsNav [data-pane]').forEach(button=>button.addEventListener('click',()=>{showSettingsPane(button.dataset.pane);window.scrollTo(0,0)}));

// 本游戏：区域 / 身体动作 / 口令三个页签。
document.querySelectorAll('#mapTabs [data-tab]').forEach(button=>button.addEventListener('click',()=>showMapTab(button.dataset.tab)));

$('#addGameVoiceBtn').addEventListener('click',()=>{
  const row=document.querySelector('.binding-group[data-group="voice"] .binding-row[hidden]');if(!row)return;
  row.hidden=false;row.closest('.binding-group-rows')?.querySelector('.profile-empty')?.remove();
  updateMapCounts();row.querySelector('.voice-trigger-phrase')?.focus();
});

// 动作库能收起来：绑好了就不用每次都看一墙卡片。
const LIBRARY_KEY='motioncontrol_library_open';

export function setLibraryOpen(open){
  $('#libraryBody').hidden=!open;
  $('#libToggle').setAttribute('aria-expanded',String(open));$('#libToggle').textContent=open?'收起':'展开';
  try{localStorage.setItem(LIBRARY_KEY,open?'1':'0')}catch{}
}

$('#libToggle').addEventListener('click',()=>setLibraryOpen($('#libraryBody').hidden));

try{if(localStorage.getItem(LIBRARY_KEY)==='0')setLibraryOpen(false)}catch{}

// 「换游戏」「云端配置」两块平时收着，点了才展开，一次只开一块。
export function togglePanel(buttonId,panelId,open){
  const panel=$('#'+panelId),button=$('#'+buttonId);
  const next=open??panel.hidden;panel.hidden=!next;button.setAttribute('aria-expanded',String(next));
  return next;
}

$('#switchGameBtn').addEventListener('click',()=>{
  if(!togglePanel('switchGameBtn','gamePicker'))return;
  togglePanel('cloudOpenBtn','cloudPanel',false);$('#profileSearch').focus();void runAction(searchProfiles);
});

$('#cloudOpenBtn').addEventListener('click',()=>{
  if(!togglePanel('cloudOpenBtn','cloudPanel'))return;
  togglePanel('switchGameBtn','gamePicker',false);void cloudRefresh();
});

// 新手教学要指的东西可能收在别处：设置的另一类、本游戏的另一个页签、收起的动作库、
// 没展开的换游戏面板。先把它亮出来，教学的亮框才找得到它。
function revealTargets(elements){
  for(const el of elements||[]){
    const pane=el.closest?.('.settings-pane');if(pane?.hidden)showSettingsPane(pane.dataset.pane);
    const group=el.closest?.('.binding-group');if(group?.hidden)showMapTab(group.dataset.group);
    if(el.closest?.('#libraryBody')&&$('#libraryBody').hidden)setLibraryOpen(true);
    if(el.closest?.('#gamePicker')&&$('#gamePicker').hidden){togglePanel('switchGameBtn','gamePicker',true);void runAction(searchProfiles)}
  }
}
