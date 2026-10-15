// 开始页：画面、区域框、准备卡片、触发大字、挪动区域、悬浮窗。
import {renderIntent,renderMisfireHint,renderZoneFit,renderZoneFreeze} from './body.js';
import {$,agoText,api,clamp,fitCanvas,isVisible,notice,post,setAttribute,setClass,setProperty,setStyle,setText,syncChildren} from './core.js';
import {inputStatus,kernelEpoch,outputConnected,setOutput,setSource,setXinputMerge,renderCameraDepth,syncCameraDeviceRow} from './devices.js';
import {renderTriggerRecord} from './diagnostics.js';
import {BODY_ZONES,EDGES,actionKeyText,bindingsForDisplay,bodyZones,profileTriggers,triggerMapped,voiceLatchText,zoneKeyLabel} from './labels.js';
import {paintCustomPoseScores,paintPoseCountdown,paintPoseLibrary} from './library.js';
import {paintZoneConflictNotes,revealBindingRow,renderCurrentMode} from './mapping.js';
import {actionBusy,currentView,loadProfiles,profileLoading,profileReady,runAction,showView,tutorial} from './shell.js';
import {S,gameProfile,head,output} from './state.js';
import {handMouseConfig,renderViewControl,syncControlLabels} from './view-control.js';
import {renderHeadCurve} from './head-curve.js';
import {voiceInputIssue,voiceInputReady,voiceInputTarget} from './voice.js';

export let currentPoseMap=null;
export let kernelState=null;
export let sourceMode='computer';
export let cameraRunning=false;
export let sessionStarted=false;
// 摄像头的完整状态。新手教学要按它判断画面来没来。
export let cameraInfo=null;
export let previewMirrored=true;
export function setPreviewMirror(enabled,revision){
  if(Number.isFinite(revision))latestConfigRevision=Math.max(latestConfigRevision,revision);
  previewMirrored=!!enabled;
  setClass(viewer,'unmirrored',!previewMirrored);
  setProperty($('#cameraMirror'),'checked',previewMirrored);
  renderOverlay();
}
let serviceReady=false;
let kernelConnected=false;
export let zoneEditMode=false;
let liveZoneDrag=null;
let latestRuntime=null;
let latestConfigRevision=-1;
let poseCaptureSignature='',zoneFreezeSignature='';

const canvas = $('#canvas');

const ctx = canvas.getContext('2d');

const viewer = $('#viewer');

const cameraPreview = $('#cameraPreview');
let mainPoseSignature='';
let headConfigSignature='';

// 主画布只画骨骼；区域与选中/拖动状态由独立 DOM 更新，不能跟着骨骼一起跳过。
function paintMainPose(){
  fitCanvas(canvas,(Number(kernelState?.width)||640)/(Number(kernelState?.height)||480));
  const points=Object.entries(currentPoseMap||{}).filter(([,point])=>!(point.score<.3)).map(([name,point])=>[name,point.x,point.y]);
  const signature=JSON.stringify([canvas.width,canvas.height,points]);
  if(signature===mainPoseSignature)return;
  mainPoseSignature=signature;draw(currentPoseMap);
}

export const overlay={win:null,canvas:null,ctx:null};

const perfUi={previewBusy:false};

// 编辑使用打开时的参考快照；保存时由内核保留跟随或固定方式。
const RECT_EDIT_ZONE_IDS=['leftHand','rightHand','leftFoot','rightFoot','headJump','lookGate'];

const RECT_EDIT_MIN=.02;

export const rectEdit={rects:{},selected:'',profileId:null,modes:{},reference:{},viewport:{x1:0,y1:0,x2:1,y2:1},width:640,height:480};

function draw(map,target=ctx,w=canvas.width,h=canvas.height,mirror=false){
  target.save();target.setTransform(1,0,0,1,0,0);target.clearRect(0,0,w,h);
  if(mirror){target.translate(w,0);target.scale(-1,1)}
  target.strokeStyle='rgba(255,255,255,.82)';target.fillStyle='#fff';target.lineWidth=Math.max(2,w/640*3);
  if(map){
    for(const [a,b] of EDGES){
      const p=map[a],q=map[b];if(!p||!q||p.score<.3||q.score<.3)continue;
      target.beginPath();target.moveTo(p.x*w,p.y*h);target.lineTo(q.x*w,q.y*h);target.stroke();
    }
    for(const p of Object.values(map)){
      if(p.score<.3)continue;
      target.beginPath();target.arc(p.x*w,p.y*h,Math.max(2,w/640*3),0,Math.PI*2);target.fill();
    }
  }
  target.restore();
}

export function renderKernelZones(zones={}){
  if(!isVisible(viewer))return;
  const definitions=bodyZones(zones),layer=viewer.querySelector('.zones');
  for(const element of layer.querySelectorAll('.custom-zone')){
    if(!definitions[element.dataset.zone])element.remove();
  }
  for(const [id,def] of Object.entries(definitions)){
    if(!def.custom||layer.querySelector(`[data-zone="${id}"]`))continue;
    const element=document.createElement('div');element.className='zone custom-zone';element.dataset.zone=id;
    element.append(document.createElement('strong'),document.createElement('small'));
    addZoneHandles(element);layer.append(element);
  }
  for(const[id,def]of Object.entries(definitions)){
    const el=document.querySelector(`.zone[data-zone="${id}"]`),state=zones[id];if(!el)continue;
    if(def.gate&&!kernelState?.vertical_look?.enabled){setStyle(el,'display','none');continue}
    const active=!!state?.pressed;
    setClass(el,'active',active);
    // 智能判定：判断中（黄）、判定是扫过（闪红）。系统功能要稳住，底下的条是稳住走到哪了。
    const phase=String(state?.phase||'idle');
    setClass(el,'pending',phase==='pending');setClass(el,'swept',phase==='swept');
    setStyle(el,'--progress',Math.round(Number(state?.progress||0)*100)+'%');
    setText(el.querySelector('strong'),def.gate?'上下视角':zoneKeyLabel(id,def));
    setText(el.querySelector('small'),def.gate?(active?'已开启':'左手放这里'):def.body);
    setProperty(el,'tabIndex',-1);
    setAttribute(el,'aria-label',def.body+'区域，方向键移动，Shift 加方向键改大小');
    const rect=state?.rect;
    setClass(el,'selected',false);
    if(!rect){setStyle(el,'display','none');continue}
    setStyle(el,'display','grid');setStyle(el,'left',(rect.x1*100)+'%');setStyle(el,'top',(rect.y1*100)+'%');setStyle(el,'width',((rect.x2-rect.x1)*100)+'%');setStyle(el,'height',((rect.y2-rect.y1)*100)+'%');
  }
}

export function addZoneHandles(element){
  for(const corner of ['nw','ne','sw','se']){
    const handle=document.createElement('span');handle.className='zone-handle';handle.dataset.corner=corner;handle.setAttribute('aria-hidden','true');element.append(handle);
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
  setStyle(cancel,'display','block');setText(stage,phaseName);
  setText(prompt,notice||'看向游戏屏幕中心，保持自然站姿/坐姿');
  const remaining=Math.max(0,Number(hs.center_remaining_s||0));
  const valid=Number(hs.center_valid_s||0),required=Math.max(.1,Number(hs.center_required_s||2.2));
  const samples=Number(hs.center_sample_count||0),targetSamples=Math.max(1,Number(hs.center_target_samples||32));
  const invalid=Number(hs.center_invalid_count||0),rejected=Number(hs.center_rejected_count||0);
  setText(detail,phase==='prepare'?'说完后稍等一下，让说话造成的头部/嘴部动作结束':`采集 ${Math.min(valid,required).toFixed(1)} / ${required.toFixed(1)} 秒 · 有效样本 ${samples} / ${targetSamples} · 无效 ${invalid} · 忽略明显跳点 ${rejected}`);
  setText(countdown,remaining.toFixed(1));
  const timeProgress=Math.min(1,valid/required),sampleProgress=Math.min(1,samples/targetSamples);
  const progress=phase==='collect'?(.12+.88*Math.min(timeProgress,sampleProgress)):.08;
  setStyle(bar,'width',`${Math.max(0,Math.min(100,progress*100))}%`);
}

export function renderMainStatus(){
  serviceReady=kernelConnected&&outputConnected;
  const main=$('#mainActionBtn');
  setProperty(main,'disabled',!output.enabled&&(!serviceReady||actionBusy||zoneEditMode));
  setText($('#mainActionLabel'),output.enabled?'暂停控制':'开始控制');
  setClass(main,'running',!!output.enabled);setClass(main,'primary',!output.enabled);setClass(main,'stop',!!output.enabled);
  // 服务连着的时候什么都不说；断了才冒出来。
  setText($('#serviceStatus'),S.sourceConnecting?'正在准备识别，请稍候…':serviceReady?'本地服务已连接':'服务断开，可按 F9 重试暂停控制');
  setClass($('#serviceStatus'),'online',serviceReady||!!S.sourceConnecting);setClass($('#serviceStatus'),'offline',!serviceReady&&!S.sourceConnecting);
  if(isVisible(viewer)){renderViewerMessage();renderReadiness();renderConflicts()}
}

document.addEventListener('motioncontrol:voice-state',renderMainStatus);

// 画面中间那句话只在没人、没画面时出现；有人站进来就让开。
function renderViewerMessage(){
  const hint=$('#hint');if(!hint)return;
  const live=sourceMode==='phone'?!!inputStatus.mobile_pose_connected:cameraRunning;
  const [title,detail]=S.sourceConnecting?['正在准备识别','相机启动可能需要几十秒，请稍候']:!live?[sourceMode==='phone'?'等手机连上':'摄像头没连',sourceMode==='phone'?'在手机上打开 MotionControl，点「连接并开始」':'点下方「开始识别」，或在「设置 → 设备」里选择摄像头']
    :sourceMode==='computer'&&cameraRunning&&cameraInfo.picture_black?['摄像头画面全黑',cameraInfo.camera_virtual?'请启动虚拟摄像头的画面来源，或换一个摄像头':'请检查镜头遮挡，或换一个摄像头']
    :!currentPoseMap?['站到镜头前','头和双肩入镜就能开始']:['',''];
  setProperty(hint,'hidden',!title);
  const key=title+'|'+detail;if(hint.dataset.key===key)return;hint.dataset.key=key;
  hint.replaceChildren();if(!title)return;
  const b=document.createElement('b');b.textContent=title;const s=document.createElement('span');s.textContent=detail;hint.append(b,s);
}

// 开始页右边那张卡：差哪一步就点名哪一步，做好的只打个勾；控制开着时整张换成计时。
let runStartedAt=0;

function setStep(name,state,sub){
  const li=document.querySelector(`.checklist [data-step="${name}"]`);if(!li)return;
  setClass(li,'done',state==='done');setClass(li,'warn',state==='warn');
  const el=li.querySelector('.step-sub');if(el&&sub!==undefined&&el.textContent!==sub)el.textContent=sub;
}

function renderReadiness(){
  const hs=kernelState?.head||{};
  const phoneMode=sourceMode==='phone';
  const cameraOk=phoneMode?!!inputStatus.mobile_pose_connected:cameraRunning&&!cameraInfo.picture_black;
  const posed=!!currentPoseMap;
  const handHorizontal=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.horizontal_hand);
  const needCal=!handHorizontal&&head.enabled;
  const calibrated=!needCal||!!(hs.horizontal_calibrated??hs.calibrated);
  const cameraIssue=phoneMode?'等手机摄像头连上':cameraRunning&&cameraInfo.picture_black?'摄像头画面全黑，请检查镜头或画面来源':String(cameraInfo?.last_error||'').trim()||'摄像头还没连接';
  const deviceText=cameraOk&&voiceInputReady?'已就绪'
    :[cameraOk?'摄像头已就绪':cameraIssue,voiceInputReady?'麦克风已就绪':`麦克风：${voiceInputIssue}`].join(' · ');
  setStep('camera',cameraOk&&voiceInputReady?'done':'warn',deviceText);
  setProperty($('#cameraStepSub'),'title',cameraOk&&voiceInputReady?'摄像头和麦克风已就绪':deviceText);
  const go=$('#cameraStepGo');setProperty(go,'hidden',cameraOk&&voiceInputReady);
  setAttribute(go,'data-resolve',cameraOk?voiceInputTarget:'camera');
  setText(go,cameraOk?(voiceInputTarget==='voice'?'检查语音':'检查麦克风'):'检查摄像头');
  // 脚踝出了画面，踏步和脚区就认不出抬脚。只在它们绑了键时提，不算进"还差几步"。
  const feetOut=posed&&kernelState?.feet_in_view===false&&['motion.march','zone.leftFoot','zone.rightFoot'].some(triggerMapped);
  const feetHint='脚没入镜';
  setStep('pose',posed&&!feetOut?'done':(cameraOk?'warn':''),posed?(feetOut?feetHint:'已识别'):'站到镜头前，头和双肩入镜');
  setStep('game',gameProfile.selected?'done':'',gameProfile.selected?.name||'正在读取…');
  // 握拳管左右时不用校准头；这一步照样列着，写明用不着。
  const calStep=document.querySelector('.checklist [data-step="cal"]');
  if(calStep){setClass(calStep,'done',calibrated&&!hs.calibrating);setClass(calStep,'warn',!calibrated&&posed)}
  if(!needCal)setText($('#calStatus'),handHorizontal?'握拳控制视角，不用校准':'左右视角关着，不用校准');
  const left=[cameraOk,posed,calibrated].filter(ok=>!ok).length;
  setText($('#readyTitle'),left?`还差 ${left} 步`:'可以开始了');
  const running=!!output.enabled;
  setProperty($('#readyCard'),'hidden',running&&!setupConflicts().length);
  setProperty($('#runCard'),'hidden',!running);
  if(running){
    if(!runStartedAt)runStartedAt=Date.now();
    const sec=Math.floor((Date.now()-runStartedAt)/1000),pad=n=>String(n).padStart(2,'0');
    const text=sec>=3600?`${Math.floor(sec/3600)}:${pad(Math.floor(sec/60)%60)}:${pad(sec%60)}`:`${pad(Math.floor(sec/60))}:${pad(sec%60)}`;
    if($('#runElapsed').textContent!==text)$('#runElapsed').textContent=text;
    const sub=$('#runSub'),lost=!posed;
    setText(sub,lost?'看不到人了，站回镜头前':feetOut?feetHint:`${gameProfile.selected?.name||''} · F9 暂停控制`);setClass(sub,'warn',lost||feetOut);
  }else runStartedAt=0;
}

// 两个设置各自都合法，合起来却什么都不做。玩家看不出区别——功能开着、读数在跳、
// 就是没反应。所以在"开始"那一页点名，并把能一键改的那一下也给出来。
export function mergeOwnsSticks(){return output.xinputEnabled&&output.mode==='gamepad'}

function setupConflicts(){
  const hand=kernelState?.head?.hand_mouse||{};
  const items=[];
  if(inputStatus.phone_ignored)
    items.push(['手机在传画面，但来源选的是电脑摄像头，手机的画面没有用上。','改用手机',()=>setSource('phone',true)]);
  if(mergeOwnsSticks()&&hand.enabled)
    items.push(['物理手柄合流占着两个摇杆，手控鼠标不会动。','关掉合流',async()=>{$('#xinputMerge').value='';await setXinputMerge()}]);
  return items;
}

let conflictSignature='';
function renderConflicts(){
  const box=$('#setupConflicts');
  if(!box)return;
  const items=setupConflicts();
  setProperty(box,'hidden',!items.length);
  const signature=items.map(([text,label])=>text+'|'+label).join('\n');
  if(signature===conflictSignature)return;
  conflictSignature=signature;
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

// 画面右上角只在视角出问题时说一句：没校准、做动作时被防晃拦着。平时什么都不显示。
function renderViewHud(hs,guardBlocked){
  const hud=$('#headStatus');if(!hud)return;
  const usesHand=!!(hs.hand_mouse?.enabled&&['left','right'].includes(hs.hand_mouse?.config?.horizontal_hand));
  const headOn=!usesHand&&hs.enabled!==false;
  const calibrated=usesHand||(hs.horizontal_calibrated??hs.calibrated);
  const text=!currentPoseMap||!headOn?'':!calibrated?'视角未校准':guardBlocked?'做动作中，视角稳住':'';
  setProperty(hud,'hidden',!text);if(text)setText(hud,text);
}

export function renderKernelState(runtime,force=false){
  if(runtime?.config_revision!=null){
    const revision=Number(runtime.config_revision);
    if(Number.isFinite(revision)){
      if(revision<latestConfigRevision)return;
      latestConfigRevision=revision;
    }
  }
  latestRuntime=runtime;
  if(typeof runtime?.preview_mirror==='boolean')setPreviewMirror(runtime.preview_mirror);
  kernelState=runtime?.kernel||runtime||{};sourceMode=runtime?.body_mode||sourceMode;const k=kernelState;
  renderCurrentMode(k.game_mode);
  currentPoseMap=k.pose||null;
  // 录姿势的倒计时在服务端，按钮和口令触发的是同一个。这里只负责画出来。
  const capture=runtime?.pose_capture,captureSignature=JSON.stringify([capture?.counting,capture?.purpose,capture?.remaining_s,capture?.pose_id,capture?.message]);
  if(capture?.counting||captureSignature!==poseCaptureSignature){poseCaptureSignature=captureSignature;paintPoseCountdown(capture)}
  renderTriggerLive();
  renderRange();
  const marchSelect=$('#marchAlgorithm');
  if(isVisible($('#triggerRecordStatus')))renderTriggerRecord(k.trigger_recording);
  if(marchSelect&&!marchSelect.disabled&&document.activeElement!==marchSelect)setProperty(marchSelect,'value',k.march_algorithm==='responsive'?'responsive':'legacy');
  const frameWidth=Number(k.width)||640,frameHeight=Number(k.height)||480;
  setStyle(viewer,'aspect-ratio',`${frameWidth} / ${frameHeight}`);setStyle(viewer,'--frame-ratio',String(frameWidth/frameHeight));
  if(isVisible(viewer)){paintMainPose();renderKernelZones(k.zones||{});if(k.zone_misfire_hint||!$('#misfireHint').hidden)renderMisfireHint(k)}
  if(isVisible($('#zoneFitStatus')))renderZoneFit(k);
  if(isVisible(viewer)||isVisible($('#zoneTriggerMode'))||isVisible($('#headSettings'))){
    const signature=JSON.stringify([k.zones_frozen,zoneEditMode,k.zone_trigger_mode,k.vertical_look?.deadzone,$('#zoneTriggerMode').value,$('#marchAlgorithm').value,$('#verticalDeadzone').value,document.activeElement===$('#zoneTriggerMode'),document.activeElement===$('#verticalDeadzone'),$('#zoneTriggerMode').disabled,$('#marchAlgorithm').disabled]);
    if(signature!==zoneFreezeSignature){zoneFreezeSignature=signature;renderZoneFreeze(k)}
  }
  if(isVisible($('#intentStatus')))renderIntent(k);
  if(currentView==='games'&&!document.hidden)paintZoneConflictNotes();
  // 区域按没按，画面里的框自己会亮；动作按没按，画面下面那排动作自己会亮（renderRange）。
  // 自定义姿势的相似度跟着主状态一起来，不另开一路轮询。
  S.customPoseScores=k.custom_pose_scores||{};if(isVisible($('#customPoseList')))paintCustomPoseScores();if(isVisible($('#poseLibraryList')))paintPoseLibrary();
  const hs=k.head||{};
  const guardEnabled=hs.body_motion_guard_enabled??k.body_motion_guard_enabled??false;
  const guardBlocked=!!hs.horizontal_paused_by_body_motion;
  const motionScale=Number(hs.motion_deadzone_scale)||1;
  const guardStatus=$('#bodyMotionGuardStatus');
  if(guardStatus){
    setText(guardStatus,guardEnabled===false?'防晃 · 已关闭':motionScale>1.01?`防晃 · 稳定区临时增加 ${Math.round((motionScale-1)*100)}% · 明显转头仍可转向`:'防晃 · 已开启 · 身体动起来时略增大稳定区');
    setClass(guardStatus,'active',motionScale>1.01&&guardEnabled!==false);
  }
  renderViewHud(hs,guardBlocked);
  if(hs.calibrated!==undefined){
    setText($('#calBtn'),hs.calibrating?'取消校准':(hs.horizontal_calibrated??hs.calibrated)?'重新校准':'站好并校准');
    const handHorizontal=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.horizontal_hand);
    if(hs.calibrating||(!handHorizontal&&hs.enabled))setText($('#calStatus'),hs.calibrating?(hs.notice||hs.quality||'正在校准'):(hs.notice||hs.quality||'看着屏幕中心站好，约 8 秒'));
    const missingPoints=(hs.frozen22_missing_points||[]).join('、');
    setProperty($('#calStatus'),'title',[hs.estimate_error,missingPoints&&'缺少关键点：'+missingPoints].filter(Boolean).join(' · '));
    renderCalibrationOverlay(hs);
  }
  if(hs.algorithm&&(force||(!S.headDirty&&!document.activeElement?.closest('#headSettings,[data-pane="lab"]')))){
    const signature=JSON.stringify([hs.algorithm,hs.horizontal_algorithm,hs.deadzone,hs.sensitivity_x,hs.sensitivity_y,hs.horizontal_curve_gamma,hs.enabled,hs.invert_y,k.vertical_look?.enabled,k.vertical_look?.exclusive_axes,hs.vertical_exclusive_axes,k.vertical_look?.body_motion_guard]);
    if(force||signature!==headConfigSignature){
    headConfigSignature=signature;
    const horizontalAlgorithm=String(hs.horizontal_algorithm||'roll_tilt');
    head.horizontalAlgorithm=['gesture_v188','roll_tilt','head_responsive'].includes(horizontalAlgorithm)?horizontalAlgorithm:'gesture_v188';
    head.verticalLookEnabled=k.vertical_look?.enabled===true;
    head.verticalExclusive=!!(k.vertical_look?.exclusive_axes??hs.vertical_exclusive_axes);
    head.bodyMotionGuard=k.vertical_look?.body_motion_guard===true;
    if($('#verticalExclusive'))$('#verticalExclusive').checked=head.verticalExclusive;
    if($('#bodyMotionGuard'))$('#bodyMotionGuard').checked=head.bodyMotionGuard;
    $('#deadzone').value=Math.round(Number(hs.deadzone||.10)*100);
    $('#speedX').value=Number(hs.sensitivity_x||58);$('#speedY').value=Number(hs.sensitivity_y||46);
    $('#headCurveGamma').value=Number(hs.horizontal_curve_gamma??2);
    // 头控开没开由「左右」那个下拉决定（选头部方案 = 开，选握拳或关闭 = 关），这里只记下来。
    head.enabled=!!hs.enabled;$('#invertY').checked=!!hs.invert_y;syncControlLabels();renderViewControl();
    }
  }
  renderHeadCurve(hs);
  const camera=runtime?.camera||{running:cameraRunning};
  cameraRunning=!!camera.running;if(runtime?.camera){cameraInfo=runtime.camera;renderCameraDepth(camera)}
  sessionStarted=sourceMode==='phone'?inputStatus.body_enabled!==false:cameraRunning;
  if(S.desiredSource===null)setProperty($('#poseSource'),'value',sourceMode);
  const computer=$('#poseSource').value==='computer';
  if(['cameraDeviceRow','cameraScanRow','cameraRotationRow'].some(id=>$('#'+id)?.hidden!==!computer))syncCameraDeviceRow();

  if(cameraPreview){
    const showPreview=sourceMode==='computer'&&cameraRunning;
    setProperty(cameraPreview,'hidden',!(showPreview&&cameraPreview.complete&&cameraPreview.naturalWidth));
    if(!showPreview&&cameraPreview.hasAttribute('src')){
      URL.revokeObjectURL(cameraPreview.src);cameraPreview.removeAttribute('src');
    }
  }
  const recognizing=sourceMode==='phone'?inputStatus.body_enabled!==false&&!!inputStatus.mobile_pose_connected:cameraRunning;
  setProperty($('#sourceStopBtn'),'hidden',!recognizing&&!S.sourceConnecting);
  setProperty($('#sourceStartBtn'),'hidden',recognizing||!!S.sourceConnecting);
  // 旧版上下视角（绿框）开着才有这一块。
  const gateActive=!!k.vertical_gate_active;const gateStatus=$('#lookGateStatus');if(gateStatus){setProperty(gateStatus,'hidden',!k.vertical_look?.enabled||!head.verticalLookEnabled||!currentPoseMap);const paused=!!hs.horizontal_paused_by_vertical_gate;const text=gateActive?`上下视角开${paused?' · 左右暂停':''}`:'左手放进绿框开上下视角';setText(gateStatus,text);setProperty(gateStatus,'className','hud hud-gate'+(gateActive?' active':''))}renderOverlay(currentPoseMap);renderMainStatus();

}

export async function refreshKernel(){
  const epoch=kernelEpoch;
  try{
    const runtime=await api('/api/kernel/status');
    if(epoch===kernelEpoch){kernelConnected=true;renderKernelState(runtime)}
    if(!profileReady&&!profileLoading)void loadProfiles();
    return true;
  }catch{if(epoch===kernelEpoch){kernelConnected=false;renderMainStatus()}return false}
}

export function renderVisibleState(){if(latestRuntime)renderKernelState(latestRuntime)}

if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>{
  if(isVisible(viewer))paintMainPose();
}).observe(viewer);
window.addEventListener('resize',()=>{if(isVisible(viewer))paintMainPose()});

export async function refreshPreview(){
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

export async function startCalibration(){const running=!!kernelState?.head?.calibrating;try{renderKernelState(await post(running?'/api/head/calibration/cancel':'/api/head/calibration/start',{}));notice(running?'校准已取消':'校准已开始：看向游戏屏幕中心，保持自然姿势')}catch(e){notice('中心设置失败：'+(e?.message||e))}}

export async function centerHead(){try{renderKernelState(await post('/api/head/calibration/center',{}));notice('视角中心已更新。')}catch(e){notice('视角回正失败：'+(e?.message||e))}}

function drawOverlayZones(octx,w,h,zones={}){
  for(const[id,def]of Object.entries(bodyZones(zones))){
    const state=zones[id];if(!state)continue;const active=!!state.pressed,isGate=!!def.gate,phase=String(state.phase||'idle');
    // 悬浮窗里按下一直是红的（游戏画面上最显眼）；判断中黄、扫过灰掉，免得和按下混。
    const stroke=active?'#ff5966':phase==='pending'?'#ffcc33':phase==='swept'?'rgba(255,255,255,.35)':'rgba(255,255,255,.78)';
    const fill=active?'rgba(255,70,80,.26)':phase==='pending'?'rgba(255,204,51,.22)':'rgba(0,0,0,.12)';
    octx.save();octx.lineWidth=Math.max(2,w/220);octx.strokeStyle=isGate?(active?'#62ff91':'#62d982'):stroke;octx.fillStyle=isGate?(active?'rgba(45,210,95,.30)':'rgba(30,150,75,.15)'):fill;if(isGate&&!active)octx.setLineDash([Math.max(4,w/100),Math.max(3,w/140)]);
    let x=0,y=0,ww=0,hh=0;const r=state.rect;
    if(r){x=(previewMirrored?1-Number(r.x2):Number(r.x1))*w;y=Number(r.y1)*h;ww=(Number(r.x2)-Number(r.x1))*w;hh=(Number(r.y2)-Number(r.y1))*h}
    if(ww<=0||hh<=0){octx.restore();continue}octx.beginPath();if(isGate)octx.roundRect(x,y,ww,hh,Math.max(8,w/70));else octx.roundRect(x,y,ww,hh,Math.max(6,w/90));octx.fill();octx.stroke();octx.setLineDash([]);octx.fillStyle='#fff';octx.font=`800 ${Math.round(Math.max(11,Math.min(Math.min(ww,hh)*.34,w/9)))}px system-ui,sans-serif`;octx.textAlign='center';octx.textBaseline='middle';octx.fillText(isGate?(active?'上下视角 已开启':'上下视角'):zoneKeyLabel(id,def),x+ww/2,y+hh/2);octx.restore();
  }
}

function renderOverlay(map=currentPoseMap){
  if(!overlay.win||overlay.win.closed||!overlay.canvas||!overlay.ctx)return;const c=overlay.canvas,octx=overlay.ctx;fitCanvas(c,4/3,500000);const w=c.width,h=c.height;octx.setTransform(1,0,0,1,0,0);octx.clearRect(0,0,w,h);octx.fillStyle='#050608';octx.fillRect(0,0,w,h);
  draw(map,octx,w,h,previewMirrored);
  drawOverlayZones(octx,w,h,kernelState?.zones||{});const buttons=kernelState?.buttons||[],motions=kernelState?.motions||[];const gate=!!kernelState?.vertical_gate_active;const latch=voiceLatchText(output);const text=gate?'上下视角已开启':(buttons.length?`区域 ${buttons.join('+')}`:(motions.length?`动作 ${motions.join('+')}`:(map?'未触发':'未识别人体')));octx.fillStyle=latch?'rgba(70,32,0,.78)':'rgba(0,0,0,.62)';octx.fillRect(0,h-Math.max(25,h/10),w,Math.max(25,h/10));octx.fillStyle=latch?'#ffc46b':'#fff';octx.font=`600 ${Math.max(12,Math.round(w/32))}px system-ui,sans-serif`;octx.textAlign='left';octx.textBaseline='alphabetic';octx.fillText(latch?`${latch} · 说松开才会放`:`${output.enabled?'输出开':'输出关'} · ${text}`,Math.max(7,w/70),h-Math.max(7,h/70))
}

export async function toggleOverlay(){if(overlay.win&&!overlay.win.closed){try{overlay.win.close()}catch{}overlay.win=null;overlay.canvas=null;overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗';return}if(!window.documentPictureInPicture?.requestWindow){notice('当前浏览器不支持置顶游戏悬浮窗。');return}try{const pip=await window.documentPictureInPicture.requestWindow({width:420,height:315});pip.document.title='MotionControl';pip.document.body.style.cssText='margin:0;overflow:hidden;background:#050608;width:100vw;height:100vh';const c=pip.document.createElement('canvas');c.width=640;c.height=480;c.style.cssText='display:block;width:100vw;height:100vh;object-fit:contain;background:#050608';pip.document.body.appendChild(c);overlay.win=pip;overlay.canvas=c;overlay.ctx=c.getContext('2d');pip.addEventListener('pagehide',()=>{overlay.win=overlay.canvas=overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗'},{once:true});$('#overlayBtn').textContent='关闭悬浮';renderOverlay(currentPoseMap)}catch(e){notice('悬浮窗启动失败：'+(e?.message||e))}}

export async function ensureZoneReference(){
  const data=await api('/api/zones/editor');
  if(data.reference?.ready!==false)return true;
  showView('play');tutorial.openLesson('fit',$('#adjustZonesBtn'),{needReference:true});return false;
}

export async function openLiveZoneEditor({selectedId=''}={}){
  if(zoneEditMode){if(typeof selectedId==='string'&&rectEdit.rects[selectedId])selectEditorRegion(selectedId);return}
  await setOutput(false);
  const data=await api('/api/zones/editor');
  if(data.reference?.ready===false){showView('play');tutorial.openLesson('fit',$('#adjustZonesBtn'),{needReference:true});return}
  const rects=structuredClone(data.rects||{});
  if(!Object.keys(rects).length)throw new Error('先在「本游戏 → 区域」添加区域');
  Object.assign(rectEdit,{rects,profileId:data.profile_id,modes:{...data.modes},reference:data.reference||{},width:data.width||640,height:data.height||480});
  rectEdit.selected=typeof selectedId==='string'&&rects[selectedId]?selectedId:Object.keys(rects)[0];
  fitReferenceViewport();zoneEditMode=true;$('#zoneEditBar').hidden=false;
  renderReferenceCanvas();renderEditorZones();renderZoneFreeze();renderMainStatus();
  $('#zoneEditBar').scrollIntoView({block:'center'});
  $('#zoneReferenceZones').querySelector(`[data-zone="${rectEdit.selected}"]`)?.focus({preventScroll:true});
}

function closeLiveZoneEditor(){
  zoneEditMode=false;liveZoneDrag=null;$('#zoneEditBar').hidden=true;
  renderZoneFreeze();renderMainStatus();$('#adjustZonesBtn').focus({preventScroll:true});
}

function fitReferenceViewport(){
  const points=[{x:0,y:0},{x:1,y:1},...Object.values(rectEdit.reference.pose||{})];
  for(const sector of rectEdit.reference.ranges||[])points.push(...(sector.points||[]));
  for(const r of Object.values(rectEdit.rects))points.push({x:r.x1,y:r.y1},{x:r.x2,y:r.y2});
  const valid=points.filter(p=>Number.isFinite(p.x)&&Number.isFinite(p.y));
  let x1=Math.min(...valid.map(p=>p.x)),x2=Math.max(...valid.map(p=>p.x)),y1=Math.min(...valid.map(p=>p.y)),y2=Math.max(...valid.map(p=>p.y));
  const pad=.06*Math.max(x2-x1,y2-y1);x1-=pad;x2+=pad;y1-=pad;y2+=pad;
  // 补齐到显示框比例，横向和纵向使用相同的实际像素尺度。
  const ratio=4/3*rectEdit.height/rectEdit.width,w=x2-x1,h=y2-y1;
  if(w/h<ratio){const extra=(h*ratio-w)/2;x1-=extra;x2+=extra}else{const extra=(w/ratio-h)/2;y1-=extra;y2+=extra}
  rectEdit.viewport={x1,x2,y1,y2};
}

function referencePoint(x,y){
  const v=rectEdit.viewport,px=(x-v.x1)/(v.x2-v.x1);
  return {x:previewMirrored?1-px:px,y:(y-v.y1)/(v.y2-v.y1)};
}

function rectToDisplay(r){
  const a=referencePoint(r.x1,r.y1),b=referencePoint(r.x2,r.y2);
  return {left:Math.min(a.x,b.x),right:Math.max(a.x,b.x),top:a.y,bottom:b.y};
}

function rectFromDisplay(d){
  const v=rectEdit.viewport,w=v.x2-v.x1,h=v.y2-v.y1;
  return {x1:clamp(v.x1+(previewMirrored?1-d.right:d.left)*w,0,1),x2:clamp(v.x1+(previewMirrored?1-d.left:d.right)*w,0,1),y1:clamp(v.y1+d.top*h,0,1),y2:clamp(v.y1+d.bottom*h,0,1)};
}

function renderReferenceCanvas(){
  const c=$('#zoneReferenceCanvas');fitCanvas(c,4/3);const g=c.getContext('2d'),w=c.width,h=c.height;
  const style=getComputedStyle(document.documentElement),ink=style.getPropertyValue('--text').trim()||'#d7e5f6';
  g.clearRect(0,0,w,h);g.lineWidth=Math.max(1,w/640*2);
  const frame=rectToDisplay({x1:0,y1:0,x2:1,y2:1});
  g.fillStyle='rgba(120,130,145,.06)';g.fillRect(frame.left*w,frame.top*h,(frame.right-frame.left)*w,(frame.bottom-frame.top)*h);
  g.strokeStyle='rgba(150,160,175,.45)';g.strokeRect(frame.left*w,frame.top*h,(frame.right-frame.left)*w,(frame.bottom-frame.top)*h);
  g.font=`${Math.max(11,w/55)}px system-ui`;g.fillStyle='#969ba4';g.fillText('量身取景范围',frame.left*w+7,frame.top*h+17);
  for(const sector of rectEdit.reference.ranges||[]){
    if(!sector.points?.length)continue;
    g.fillStyle=sector.kind==='hand'?'rgba(100,170,255,.08)':'rgba(82,202,166,.08)';
    g.strokeStyle=sector.kind==='hand'?'rgba(100,170,255,.5)':'rgba(82,202,166,.5)';g.setLineDash([5,5]);
    g.beginPath();sector.points.forEach((point,i)=>{const p=referencePoint(point.x,point.y);if(i)g.lineTo(p.x*w,p.y*h);else g.moveTo(p.x*w,p.y*h)});g.closePath();g.fill();g.stroke();
  }
  const pose=rectEdit.reference.pose||{};
  for(const [a,b] of EDGES){
    const p=pose[a],q=pose[b];if(!p||!q)continue;
    const x=referencePoint(p.x,p.y),y=referencePoint(q.x,q.y);
    g.setLineDash([]);g.strokeStyle=ink;g.lineWidth=Math.max(2,w/640*3);
    g.beginPath();g.moveTo(x.x*w,x.y*h);g.lineTo(y.x*w,y.y*h);g.stroke();
  }
  g.setLineDash([]);
  for(const p of Object.values(pose)){
    const at=referencePoint(p.x,p.y);g.fillStyle=ink;g.beginPath();g.arc(at.x*w,at.y*h,Math.max(2,w/200),0,Math.PI*2);g.fill();
  }
  $('#zoneEditHint').textContent=rectEdit.reference.ranges?.length?'按量身保存的身体比例调整；扇形为手脚侧举参考。':'按保存的完整身体比例调整；可重新量身补齐手脚侧举参考。';
}

function editorRegionName(id){return gameProfile.selected?.regions?.find(r=>r.id===id)?.name||bodyZones()[id]?.body||id}

function renderEditorZones(){
  if(!zoneEditMode)return;
  const layer=$('#zoneReferenceZones'),selection=$('#zoneEditSelection');
  for(const old of layer.querySelectorAll('.zone'))if(!rectEdit.rects[old.dataset.zone])old.remove();
  for(const [id,r] of Object.entries(rectEdit.rects)){
    let el=layer.querySelector(`[data-zone="${id}"]`);
    if(!el){el=document.createElement('div');el.className='zone';el.dataset.zone=id;el.tabIndex=0;el.append(document.createElement('strong'),document.createElement('small'));addZoneHandles(el);layer.append(el)}
    const name=editorRegionName(id),d=rectToDisplay(r);
    setAttribute(el,'aria-label',name+'，方向键移动，Shift 加方向键改大小');
    setText(el.querySelector('strong'),zoneKeyLabel(id,bodyZones()[id]||{body:name}));setText(el.querySelector('small'),name);
    setClass(el,'selected',rectEdit.selected===id);setClass(el,'following',rectEdit.modes[id]==='following');
    setStyle(el,'display',liveZoneDrag&&liveZoneDrag.id!==id?'none':'grid');setStyle(el,'left',d.left*100+'%');setStyle(el,'top',d.top*100+'%');setStyle(el,'width',(d.right-d.left)*100+'%');setStyle(el,'height',(d.bottom-d.top)*100+'%');
  }
  const ids=Object.keys(rectEdit.rects);
  if([...selection.options].map(o=>o.value).join()!==ids.join())selection.replaceChildren(...ids.map(id=>new Option(editorRegionName(id),id)));
  selection.value=rectEdit.selected;$('#zoneEditPositionMode').value=rectEdit.modes[rectEdit.selected]||'fixed';
}

export function selectEditorRegion(id){if(rectEdit.rects[id]){rectEdit.selected=id;renderEditorZones()}}

export function setEditorRegionMode(mode){
  if(!['fixed','following'].includes(mode))return;
  const ids=RECT_EDIT_ZONE_IDS.includes(rectEdit.selected)?Object.keys(rectEdit.rects).filter(id=>RECT_EDIT_ZONE_IDS.includes(id)):[rectEdit.selected];
  ids.forEach(id=>rectEdit.modes[id]=mode);renderEditorZones();
}

function startRectDrag(e,el,id){
  rectEdit.selected=id;
  const corner=e.target?.closest?.('.zone-handle')?.dataset.corner||'',box=$('#zoneReferenceViewer').getBoundingClientRect();
  const at={x:(e.clientX-box.left)/Math.max(1,box.width),y:(e.clientY-box.top)/Math.max(1,box.height)};
  e.preventDefault();el.focus({preventScroll:true});el.setPointerCapture?.(e.pointerId);
  liveZoneDrag={id,pointerId:e.pointerId,corner,from:at,start:rectToDisplay(rectEdit.rects[id])};renderEditorZones();
}

function constrainEditorRect(d){
  const bounds=rectToDisplay({x1:0,y1:0,x2:1,y2:1}),w=d.right-d.left,h=d.bottom-d.top;
  d.left=clamp(d.left,bounds.left,bounds.right-w);d.right=d.left+w;d.top=clamp(d.top,bounds.top,bounds.bottom-h);d.bottom=d.top+h;return d;
}

function moveRectDrag(e){
  const drag=liveZoneDrag,box=$('#zoneReferenceViewer').getBoundingClientRect(),bounds=rectToDisplay({x1:0,y1:0,x2:1,y2:1});
  const x=(e.clientX-box.left)/Math.max(1,box.width),y=(e.clientY-box.top)/Math.max(1,box.height),d={...drag.start};
  const minW=RECT_EDIT_MIN/(rectEdit.viewport.x2-rectEdit.viewport.x1),minH=RECT_EDIT_MIN/(rectEdit.viewport.y2-rectEdit.viewport.y1);
  if(!drag.corner){d.left+=x-drag.from.x;d.right+=x-drag.from.x;d.top+=y-drag.from.y;d.bottom+=y-drag.from.y;constrainEditorRect(d)}else{
    const dx=x-drag.from.x,dy=y-drag.from.y;
    if(drag.corner.includes('w'))d.left=clamp(drag.start.left+dx,bounds.left,d.right-minW);
    if(drag.corner.includes('e'))d.right=clamp(drag.start.right+dx,d.left+minW,bounds.right);
    if(drag.corner.includes('n'))d.top=clamp(drag.start.top+dy,bounds.top,d.bottom-minH);
    if(drag.corner.includes('s'))d.bottom=clamp(drag.start.bottom+dy,d.top+minH,bounds.bottom);
  }
  rectEdit.rects[drag.id]=rectFromDisplay(d);renderEditorZones();
}

export function nudgeRect(id,key,shift){
  const r=rectEdit.rects[id];if(!r)return;
  rectEdit.selected=id;const d=rectToDisplay(r),v=rectEdit.viewport,bounds=rectToDisplay({x1:0,y1:0,x2:1,y2:1});
  const dx=(key==='ArrowLeft'?-.01:key==='ArrowRight'?.01:0)/(v.x2-v.x1),dy=(key==='ArrowUp'?-.01:key==='ArrowDown'?.01:0)/(v.y2-v.y1);
  if(shift){d.right=clamp(d.right+dx,d.left+RECT_EDIT_MIN/(v.x2-v.x1),bounds.right);d.bottom=clamp(d.bottom+dy,d.top+RECT_EDIT_MIN/(v.y2-v.y1),bounds.bottom)}
  else{d.left+=dx;d.right+=dx;d.top+=dy;d.bottom+=dy;constrainEditorRect(d)}
  rectEdit.rects[id]=rectFromDisplay(d);renderEditorZones();
}

export function startLiveZoneDrag(e){
  if(!zoneEditMode)return;const el=e.target.closest('.zone'),id=el?.dataset?.zone;
  if(id&&rectEdit.rects[id])startRectDrag(e,el,id);
}

export function moveLiveZoneDrag(e){if(zoneEditMode&&liveZoneDrag&&e.pointerId===liveZoneDrag.pointerId)moveRectDrag(e)}
export function endLiveZoneDrag(e){if(!liveZoneDrag)return;if(e?.pointerId!==undefined&&liveZoneDrag.pointerId!==e.pointerId)return;liveZoneDrag=null;renderEditorZones()}

function acceptZoneLayout(data){
  if(data.profile)gameProfile.selected=data.profile;
  if(data.runtime)renderKernelState(data.runtime);else if(data.kernel)renderKernelState(data);else return refreshKernel();
}

export async function saveLiveZones(){
  if(rectEdit.profileId!==gameProfile.selected?.selected_id)throw new Error('游戏已经切换，请取消后重新调整区域');
  const data=await post('/api/zones/layout',{profile_id:rectEdit.profileId,rects:rectEdit.rects,modes:rectEdit.modes,reference:rectEdit.reference});
  await acceptZoneLayout(data);closeLiveZoneEditor();notice('区域已保存。');
}

export async function cancelLiveZones(){closeLiveZoneEditor()}

// 首页的蓝色按钮统一切换整组框；在编辑器中也可逐个指定自定义框。
export async function followZones(){
  await setOutput(false);
  const data=await api('/api/zones/editor'),mode=kernelState?.zones_frozen?'following':'fixed';
  const saved=await post('/api/zones/layout',{profile_id:data.profile_id,rects:data.rects,modes:Object.fromEntries(Object.keys(data.rects).map(id=>[id,mode])),reference:data.reference});
  await acceptZoneLayout(saved);notice(mode==='fixed'?'触发框已固定在画面上。':'触发框已恢复跟随。');
}

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
// 默认只给绑了键的；开始页的触发显示要的是认出来的全部，传 all。区域的 pressed 本身
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

/** 触发之后高亮多久。太短了人还没把视线从镜头挪回屏幕就已经灭了。 */
const TRIGGER_FLASH_S = 1.2;
const eventLogRows=new WeakMap();

// 事件不变时只更新已显示的年龄；新的事件插到前面，保留原按钮和焦点。
function renderEventLog(log,events,names,now,{className,empty='',unmapped='未映射',title}){
  const previous=eventLogRows.get(log)||new Map(),next=new Map(),counts=new Map();
  const rows=events.slice(-6).reverse().map(event=>{
    const trigger=String(event.trigger||''),base=JSON.stringify([event.at,trigger]);
    const occurrence=counts.get(base)||0;counts.set(base,occurrence+1);
    const key=base+':'+occurrence;
    let row=previous.get(key);
    if(!row){
      row=document.createElement('button');row.type='button';row.className=className;row.dataset.trigger=trigger;row.title=title;
      row.addEventListener('click',()=>revealBindingRow(row.dataset.trigger));
    }
    setText(row,`${agoText(Math.max(0,now-Number(event.at||0)))} · ${event.label||names.get(trigger)||trigger} → ${actionKeyText(event.action)||unmapped}`);
    next.set(key,row);return row;
  });
  if(!rows.length&&empty){
    const row=previous.get('empty')||document.createElement('span');row.className='fineprint';setText(row,empty);next.set('empty',row);rows.push(row);
  }
  syncChildren(log,rows);eventLogRows.set(log,next);
}

function renderTriggerLive() {
  if(currentView!=='games'||document.hidden)return;
  // 页上可能有两块：「开始」那页一块，映射表上方一块。两块写同一份东西。
  //
  // 为什么要两块：你站在摄像头前做动作时人在「开始」页，而改键在「本游戏」页。
  // 只放映射表旁边的话，等你切过去，刚才那一下已经过去了。
  const boxes = [...document.querySelectorAll('.trigger-live')].filter(box => !box.hidden);
  const names = new Map(profileTriggers().map(item => [item.key, item.name]));
  const bindings = bindingsForDisplay();
  const now = Number(kernelState?.now) || 0;
  // 记录里的动作是触发那一刻绑的键；没绑的那几条不在这里显示，只在开始页。
  const events = (kernelState?.recent_triggers || []).filter(event => actionKeyText(event.action));
  const active = [...activeTriggerKeys()].filter(key => names.has(key));

  for (const box of boxes) {
    const nowEl = box.querySelector('.trigger-live-now');
    if (nowEl) {
      // 现在按着的写大字：这时候人站在几米外，小字看不见。
      setText(nowEl,active.length
        ? active.map(key => `${names.get(key)} → ${actionKeyText(bindings[key]?.action) || '未映射'}`).join('　')
        : '还没有触发');
      setClass(nowEl,'idle',!active.length);
    }

    const log = box.querySelector('.trigger-live-log');
    if (!log) continue;
    renderEventLog(log,events,names,now,{className:'trigger-live-item',empty:'做个动作或者说句口令，这里会记下来。',title:'点一下跳到它的映射那一行'});
  }

  // 每一行自己亮。一直按着的那些常亮，点一下就过的那些闪一下——后者没有这个
  // 闪，在 250ms 的轮询里根本抓不到。
  const fresh = new Set(events.filter(event => now - Number(event.at || 0) <= TRIGGER_FLASH_S)
                              .map(event => String(event.trigger || '')));
  const held = activeTriggerKeys();
  for (const row of document.querySelectorAll('.binding-row')) {
    if(!isVisible(row))continue;
    const key = row.dataset.trigger;
    setClass(row,'firing',held.has(key));
    setClass(row,'just-fired',!held.has(key)&&fresh.has(key));
  }
}

/* --- 开始页上的触发显示（原来的「动作测试」页） ------------------------------------
 * 站到镜头前做个动作，看打中了什么、按的是哪个键。人在几米外，所以字必须大。
 *
 * 没绑键的动作也列出来，这是最有用的一条信息：动作亮了、键位写着「没绑」，说明识别
 * 是好的，只是没绑——而这两种情况在游戏里的表现完全一样，都是"我做了但没反应"。
 */

/* 大字停多久。太短了人还没把视线从镜头挪回屏幕就已经没了。 */
const RANGE_HIT_HOLD_S = 2.0;

/** 动作格子亮多久。比大字短，因为连着做动作时它要跟得上。 */
const RANGE_FLASH_S = 0.8;

/** 名字太长的几个用短一点的叫法，一排放得下。 */
const TRIGGER_CHIP_NAMES = {march: '踏步', calf_back: '小腿后抬', jump: '跳跃', hands_up: '双手过头'};

let rangeTargetKeys = '';

// 画面下面那排：本机认得的身体动作全列上。区域不列——画面里的框自己会亮。
// 画面下面那排：绑了键的身体动作。没绑的不列——它被认出来时，画面上的大字照样会
// 写「××× → 没绑键」。区域也不列：画面里的框自己会亮。
function rangeTriggers() {
  return profileTriggers().filter(item => (item.group === 'motions' || item.group === 'poses') && triggerMapped(item.key));
}

function buildRangeTargets(triggers) {
  const wall = document.getElementById('rangeTargets');
  if (!wall) return;
  wall.replaceChildren();
  for (const item of triggers) {
    const target = document.createElement('button');
    target.type = 'button';
    target.className = 'range-target';
    target.dataset.trigger = item.key;
    target.title = '点一下去改它的键';
    const name = document.createElement('span');
    name.className = 'range-target-name';
    name.textContent = TRIGGER_CHIP_NAMES[item.id] || String(item.name).replace(/^自定义 · /, '');
    const key = document.createElement('b');
    key.className = 'range-target-key';
    target.append(name, key);
    target.addEventListener('click', () => revealBindingRow(item.key));
    wall.appendChild(target);
  }
}

/* 「动作测试」并进了开始页：
 * - 做了动作、说了口令，画面上用大字写「原地踏步 → W」，几米外也看得清；
 * - 画面下面那排动作，认出来就亮——没绑键的也亮，键位写「没绑」，这正是
 *   「做了但游戏没反应」最常见的原因；
 * - 右边「最近触发」列出最近几次，点一条去改它的键。
 * 游戏控制没开时照样显示，大字下面补一句「游戏里不会按」。 */
function renderRange() {
  if (currentView !== 'play'||document.hidden) return;
  const triggers = rangeTriggers();
  // 只在格子本身变了的时候重建。每 250ms 重建一次的话，鼠标压根点不中。
  const signature = triggers.map(item => item.key).join('|');
  if (signature !== rangeTargetKeys) {
    rangeTargetKeys = signature;
    buildRangeTargets(triggers);
  }

  const names = new Map(profileTriggers().map(item => [item.key, item.name]));
  const bindings = bindingsForDisplay();
  const now = Number(kernelState?.now) || 0;
  const events = kernelState?.recent_triggers || [];
  const held = activeTriggerKeys({all: true});
  const keyText = action => actionKeyText(action) || '没绑键';

  // 大字：正按着的优先，其次是刚打中的那一下；都没有就不出现。
  const hit = document.getElementById('rangeHit');
  const holding = [...held].filter(key => names.has(key));
  const latest = events.length ? events[events.length - 1] : null;
  const since = latest ? Math.max(0, now - Number(latest.at || 0)) : Infinity;
  let text = '';
  if (holding.length) text = holding.map(key => `${names.get(key)} → ${keyText(bindings[key]?.action)}`).join('　');
  else if (latest && since <= RANGE_HIT_HOLD_S) {
    const key = String(latest.trigger || '');
    text = `${latest.label || names.get(key) || key} → ${keyText(latest.action)}`;
  }
  if (hit) {
    setProperty(hit,'hidden',!text);
    if (text && text !== hit.dataset.text) {
      document.getElementById('rangeHitWhat').textContent = text;
      hit.classList.remove('on'); void hit.offsetWidth; hit.classList.add('on');
    }
    setAttribute(hit,'data-text',text);
    const when = document.getElementById('rangeHitWhen');
    const note = text && !output.enabled ? '游戏控制没开，游戏里不会按' : '';
    if (when.textContent !== note) when.textContent = note;
  }

  // 动作格子：按着的常亮，刚打中的闪一下。没绑键的写「没绑」。
  const fresh = new Set(events.filter(event => now - Number(event.at || 0) <= RANGE_FLASH_S)
                              .map(event => String(event.trigger || '')));
  for (const target of document.querySelectorAll('.range-target')) {
    const key = target.dataset.trigger;
    const label = actionKeyText(bindings[key]?.action);
    const keyEl = target.querySelector('.range-target-key');
    const want = label || '没绑';
    if (keyEl.textContent !== want) keyEl.textContent = want;
    setClass(target,'unmapped',!label);
    setClass(target,'on',held.has(key));
    setClass(target,'flash',!held.has(key)&&fresh.has(key));
  }

  // 最近触发：有了才出现。
  const card = document.getElementById('recentCard');
  const log = document.getElementById('rangeLog');
  if (!card || !log) return;
  setProperty(card,'hidden',!events.length);
  renderEventLog(log,events,names,now,{className:'range-log-item',unmapped:'没绑键',title:'点一下去改它的键'});
}
