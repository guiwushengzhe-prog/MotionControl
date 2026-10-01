// 开始页：画面、区域框、准备卡片、触发大字、挪动区域、悬浮窗。
import {renderIntent,renderMisfireHint,renderZoneFit,renderZoneFreeze} from './body.js';
import {$,agoText,api,clamp,notice,post} from './core.js';
import {inputStatus,kernelEpoch,outputConnected,setOutput,setSource,setXinputMerge,syncCameraDeviceRow} from './devices.js';
import {renderTriggerRecord} from './diagnostics.js';
import {BODY_ZONES,EDGES,actionKeyText,bindingsForDisplay,profileTriggers,triggerMapped,voiceLatchText,zoneKeyLabel} from './labels.js';
import {paintCustomPoseScores,paintPoseCountdown,paintPoseLibrary} from './library.js';
import {paintZoneConflictNotes,revealBindingRow} from './mapping.js';
import {actionBusy,currentView,loadProfiles,profileLoading,profileReady,runAction} from './shell.js';
import {S,gameProfile,head,output} from './state.js';
import {handMouseConfig,renderViewControl,syncControlLabels} from './view-control.js';
import {voiceInputReady} from './voice.js';

export let currentPoseMap=null;
export let kernelState=null;
export let sourceMode='computer';
export let cameraRunning=false;
export let sessionStarted=false;
// 摄像头的完整状态。新手教学要按它判断画面来没来。
export let cameraInfo=null;
let serviceReady=false;
let kernelConnected=false;
export let zoneEditMode=false;
let liveZoneDrag=null;

const canvas = $('#canvas');

const ctx = canvas.getContext('2d');

const viewer = $('#viewer');

const cameraPreview = $('#cameraPreview');

export const overlay={win:null,canvas:null,ctx:null};

const perfUi={previewBusy:false};

// 「挪动区域」：把跟随框定住再拖。框的坐标和内核一样是原始画面（没镜像）的比例。
const RECT_EDIT_ZONE_IDS=['leftHand','rightHand','leftFoot','rightFoot','headJump','lookGate'];

const RECT_EDIT_MIN=.02;

export const rectEdit={rects:{},backup:{},selected:'',wasFrozen:false};

function draw(map,target=ctx,w=canvas.width,h=canvas.height,mirror=false){
  target.save();target.setTransform(1,0,0,1,0,0);target.clearRect(0,0,w,h);
  if(mirror){target.translate(w,0);target.scale(-1,1)}
  target.strokeStyle='rgba(255,255,255,.82)';target.fillStyle='#fff';target.lineWidth=3;
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

export function renderKernelZones(zones={}){
  for(const[id,def]of Object.entries(BODY_ZONES)){
    const el=document.querySelector(`.zone[data-zone="${id}"]`),state=zones[id];if(!el)continue;
    if(def.gate&&!kernelState?.vertical_look?.enabled){el.style.display='none';continue}
    // 绿框是闸不是键：左手伸进去不亮。
    const active=!def.gate&&!zoneEditMode&&!!state?.pressed;
    el.classList.toggle('active',active);
    // 智能判定：判断中（黄）、判定是扫过（闪红）。系统功能要稳住，底下的条是稳住走到哪了。
    const phase=zoneEditMode?'idle':String(state?.phase||'idle');
    el.classList.toggle('pending',phase==='pending');el.classList.toggle('swept',phase==='swept');
    el.style.setProperty('--progress',Math.round(Number(state?.progress||0)*100)+'%');
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

export function renderMainStatus(){
  serviceReady=kernelConnected&&outputConnected;
  const main=$('#mainActionBtn');
  main.disabled=!serviceReady||actionBusy||zoneEditMode;
  main.textContent=output.enabled?'暂停控制':(!sessionStarted&&!inputStatus.handheld_connected&&!voiceInputReady?'连接设备':'开始控制');
  main.classList.toggle('running',!!output.enabled);main.classList.toggle('primary',!output.enabled);
  // 服务连着的时候什么都不说；断了才冒出来。
  $('#serviceStatus').textContent=serviceReady?'本地服务已连接':'服务断开，急停仍可重试';
  $('#serviceStatus').classList.toggle('online',serviceReady);$('#serviceStatus').classList.toggle('offline',!serviceReady);
  renderViewerMessage();
  renderReadiness();
  renderConflicts();
}

// 画面中间那句话只在没人、没画面时出现；有人站进来就让开。
function renderViewerMessage(){
  const hint=$('#hint');if(!hint)return;
  const live=sourceMode==='phone'?!!inputStatus.mobile_pose_connected:cameraRunning;
  const [title,detail]=!live?[sourceMode==='phone'?'等手机连上':'摄像头没连',sourceMode==='phone'?'在手机上打开 MotionControl，点「连接并开始」':'在「设置 → 设备」里点「连接」']
    :!currentPoseMap?['站到镜头前','头和双肩入镜就能开始']:['',''];
  hint.hidden=!title;
  const key=title+'|'+detail;if(hint.dataset.key===key)return;hint.dataset.key=key;
  hint.replaceChildren();if(!title)return;
  const b=document.createElement('b');b.textContent=title;const s=document.createElement('span');s.textContent=detail;hint.append(b,s);
}

// 开始页右边那张卡：差哪一步就点名哪一步，做好的只打个勾；控制开着时整张换成计时。
let runStartedAt=0;

function setStep(name,state,sub){
  const li=document.querySelector(`.checklist [data-step="${name}"]`);if(!li)return;
  li.classList.toggle('done',state==='done');li.classList.toggle('warn',state==='warn');
  const el=li.querySelector('.step-sub');if(el&&sub!==undefined&&el.textContent!==sub)el.textContent=sub;
}

function renderReadiness(){
  const hs=kernelState?.head||{};
  const phoneMode=sourceMode==='phone';
  const cameraOk=phoneMode?!!inputStatus.mobile_pose_connected:cameraRunning;
  const posed=!!currentPoseMap;
  const handHorizontal=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.horizontal_hand);
  const needCal=!handHorizontal&&head.enabled;
  const calibrated=!needCal||!!(hs.horizontal_calibrated??hs.calibrated);
  setStep('camera',cameraOk?'done':'warn',phoneMode?(cameraOk?'手机摄像头':'等手机连上'):(cameraOk?'电脑摄像头':'还没连接'));
  const go=$('#cameraStepGo');if(go)go.hidden=cameraOk;
  setStep('pose',posed?'done':(cameraOk?'warn':''),posed?'已识别':'站到镜头前，头和双肩入镜');
  setStep('game',gameProfile.selected?'done':'',gameProfile.selected?.name||'正在读取…');
  // 握拳管左右时不用校准头；这一步照样列着，写明用不着。
  const calStep=document.querySelector('.checklist [data-step="cal"]');
  if(calStep){calStep.classList.toggle('done',calibrated&&!hs.calibrating);calStep.classList.toggle('warn',!calibrated&&posed)}
  if(!needCal&&$('#calStatus'))$('#calStatus').textContent=handHorizontal?'握拳控制视角，不用校准':'左右视角关着，不用校准';
  const left=[cameraOk,posed,calibrated].filter(ok=>!ok).length;
  $('#readyTitle').textContent=left?`还差 ${left} 步`:'可以开始了';
  const running=!!output.enabled;
  $('#readyCard').hidden=running&&!setupConflicts().length;
  $('#runCard').hidden=!running;
  if(running){
    if(!runStartedAt)runStartedAt=Date.now();
    const sec=Math.floor((Date.now()-runStartedAt)/1000),pad=n=>String(n).padStart(2,'0');
    const text=sec>=3600?`${Math.floor(sec/3600)}:${pad(Math.floor(sec/60)%60)}:${pad(sec%60)}`:`${pad(Math.floor(sec/60))}:${pad(sec%60)}`;
    if($('#runElapsed').textContent!==text)$('#runElapsed').textContent=text;
    const sub=$('#runSub'),lost=!posed;
    sub.textContent=lost?'看不到人了，站回镜头前':`${gameProfile.selected?.name||''} · F9 随时停`;sub.classList.toggle('warn',lost);
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

// 画面右上角只在视角出问题时说一句：没校准、做动作时被防晃拦着。平时什么都不显示。
function renderViewHud(hs,guardBlocked){
  const hud=$('#headStatus');if(!hud)return;
  const usesHand=!!(hs.hand_mouse?.enabled&&['left','right'].includes(hs.hand_mouse?.config?.horizontal_hand));
  const headOn=!usesHand&&hs.enabled!==false;
  const calibrated=usesHand||(hs.horizontal_calibrated??hs.calibrated);
  const text=!currentPoseMap||!headOn?'':!calibrated?'视角未校准':guardBlocked?'做动作中，视角稳住':'';
  hud.hidden=!text;if(text&&hud.textContent!==text)hud.textContent=text;
}

export function renderKernelState(runtime,force=false){
  kernelState=runtime?.kernel||runtime||{};sourceMode=runtime?.body_mode||sourceMode;const k=kernelState;
  // 录姿势的倒计时在服务端，按钮和口令触发的是同一个。这里只负责画出来。
  paintPoseCountdown(runtime?.pose_capture);
  renderTriggerLive();
  renderRange();
  const marchSelect=$('#marchAlgorithm');
  renderTriggerRecord(k.trigger_recording);
  if(marchSelect&&!marchSelect.disabled&&document.activeElement!==marchSelect)marchSelect.value=k.march_algorithm==='responsive'?'responsive':'legacy';
  const frameWidth=Number(k.width)||640,frameHeight=Number(k.height)||480;
  currentPoseMap=k.pose||null;if(canvas.width!==frameWidth||canvas.height!==frameHeight){canvas.width=frameWidth;canvas.height=frameHeight}viewer.style.aspectRatio=`${frameWidth}/${frameHeight}`;viewer.style.setProperty('--frame-ratio',String(frameWidth/frameHeight));draw(currentPoseMap);renderKernelZones(k.zones||{});renderZoneFit(k);renderZoneFreeze(k);renderIntent(k);renderMisfireHint(k);paintZoneConflictNotes();
  // 区域按没按，画面里的框自己会亮；动作按没按，画面下面那排动作自己会亮（renderRange）。
  // 自定义姿势的相似度跟着主状态一起来，不另开一路轮询。
  S.customPoseScores=k.custom_pose_scores||{};paintCustomPoseScores();paintPoseLibrary();
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
  renderViewHud(hs,guardBlocked);
  if(hs.calibrated!==undefined){
    $('#calBtn').textContent=hs.calibrating?'取消校准':(hs.horizontal_calibrated??hs.calibrated)?'重新校准':'站好并校准';
    $('#calStatus').textContent=hs.calibrating?(hs.notice||hs.quality||'正在校准'):(hs.notice||hs.quality||'看着屏幕中心站好，约 8 秒');
    const missingPoints=(hs.frozen22_missing_points||[]).join('、');
    $('#calStatus').title=[hs.estimate_error,missingPoints&&'缺少关键点：'+missingPoints].filter(Boolean).join(' · ');
    renderCalibrationOverlay(hs);
  }
  if(hs.algorithm&&(force||(!S.headDirty&&!document.activeElement?.closest('#headSettings,[data-pane="lab"]')))){
    $('#headAlgorithm').value=hs.algorithm;
    const horizontalAlgorithm=String(hs.horizontal_algorithm||'roll_tilt');
    head.horizontalAlgorithm=['gesture_v188','roll_tilt','head_responsive'].includes(horizontalAlgorithm)?horizontalAlgorithm:'gesture_v188';
    head.verticalLookEnabled=k.vertical_look?.enabled===true;
    head.verticalExclusive=!!(k.vertical_look?.exclusive_axes??hs.vertical_exclusive_axes);
    head.bodyMotionGuard=k.vertical_look?.body_motion_guard===true;
    if($('#verticalExclusive'))$('#verticalExclusive').checked=head.verticalExclusive;
    if($('#bodyMotionGuard'))$('#bodyMotionGuard').checked=head.bodyMotionGuard;
    $('#deadzone').value=Math.round(Number(hs.deadzone||.10)*100);
    $('#speedX').value=Number(hs.sensitivity_x||58);$('#speedY').value=Number(hs.sensitivity_y||46);
    // 头控开没开由「左右」那个下拉决定（选头部方案 = 开，选握拳或关闭 = 关），这里只记下来。
    head.enabled=!!hs.enabled;$('#invertY').checked=!!hs.invert_y;syncControlLabels();renderViewControl();
  }
  const camera=runtime?.camera||{running:cameraRunning};
  cameraRunning=!!camera.running;if(runtime?.camera)cameraInfo=runtime.camera;
  sessionStarted=sourceMode==='phone'?true:cameraRunning;
  if(S.desiredSource===null)$('#poseSource').value=sourceMode;
  syncCameraDeviceRow();

  if(cameraPreview){
    const showPreview=sourceMode==='computer'&&cameraRunning;
    cameraPreview.hidden=!(showPreview&&cameraPreview.complete&&cameraPreview.naturalWidth);
    if(!showPreview&&cameraPreview.hasAttribute('src')){
      URL.revokeObjectURL(cameraPreview.src);cameraPreview.removeAttribute('src');
    }
  }
  // 真在识别才给「停止」。
  $('#sourceStopBtn').hidden=!(sourceMode==='phone'?!!inputStatus.mobile_pose_connected:cameraRunning);
  // 旧版上下视角（绿框）开着才有这一块。
  const gateActive=!!k.vertical_gate_active;const gateStatus=$('#lookGateStatus');if(gateStatus){gateStatus.hidden=!k.vertical_look?.enabled||!head.verticalLookEnabled||!currentPoseMap;const paused=!!hs.horizontal_paused_by_vertical_gate;const text=gateActive?`上下视角开${paused?' · 左右暂停':''}`:'左手放进绿框开上下视角';if(gateStatus.textContent!==text)gateStatus.textContent=text;gateStatus.className='hud hud-gate'+(gateActive?' active':'')}renderOverlay(currentPoseMap);renderMainStatus();

}

export async function refreshKernel(){
  const epoch=kernelEpoch;
  try{
    const runtime=await api('/api/kernel/status');
    kernelConnected=true;
    if(epoch===kernelEpoch)renderKernelState(runtime);
    if(!profileReady&&!profileLoading)void loadProfiles();
  }catch{kernelConnected=false;renderMainStatus()}
}

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
  for(const[id,def]of Object.entries(BODY_ZONES)){
    const state=zones[id];if(!state)continue;const active=!!state.pressed,isGate=!!def.gate,phase=String(state.phase||'idle');
    // 悬浮窗里按下一直是红的（游戏画面上最显眼）；判断中黄、扫过灰掉，免得和按下混。
    const stroke=active?'#ff5966':phase==='pending'?'#ffcc33':phase==='swept'?'rgba(255,255,255,.35)':'rgba(255,255,255,.78)';
    const fill=active?'rgba(255,70,80,.26)':phase==='pending'?'rgba(255,204,51,.22)':'rgba(0,0,0,.12)';
    octx.save();octx.lineWidth=Math.max(2,w/220);octx.strokeStyle=isGate?'#62d982':stroke;octx.fillStyle=isGate?'rgba(30,150,75,.15)':fill;if(isGate)octx.setLineDash([Math.max(4,w/100),Math.max(3,w/140)]);
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

export async function toggleOverlay(){if(overlay.win&&!overlay.win.closed){try{overlay.win.close()}catch{}overlay.win=null;overlay.canvas=null;overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗';return}if(!window.documentPictureInPicture?.requestWindow){notice('当前浏览器不支持置顶游戏悬浮窗。');return}try{const pip=await window.documentPictureInPicture.requestWindow({width:420,height:315});pip.document.title='MotionControl';pip.document.body.style.cssText='margin:0;overflow:hidden;background:#050608;width:100vw;height:100vh';const c=pip.document.createElement('canvas');c.width=640;c.height=480;c.style.cssText='display:block;width:100vw;height:100vh;object-fit:contain;background:#050608';pip.document.body.appendChild(c);overlay.win=pip;overlay.canvas=c;overlay.ctx=c.getContext('2d');pip.addEventListener('pagehide',()=>{overlay.win=overlay.canvas=overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗'},{once:true});$('#overlayBtn').textContent='关闭悬浮';renderOverlay(currentPoseMap)}catch(e){notice('悬浮窗启动失败：'+(e?.message||e))}}

export async function openLiveZoneEditor(){
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

export function nudgeRect(id,key,shift){
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

export function startLiveZoneDrag(e){
  if(!zoneEditMode)return;const el=e.currentTarget,id=el?.dataset?.zone;
  if(id&&rectEdit.rects[id])startRectDrag(e,el,id);
}

export function moveLiveZoneDrag(e){
  if(zoneEditMode&&liveZoneDrag&&e.pointerId===liveZoneDrag.pointerId)moveRectDrag(e);
}

export function endLiveZoneDrag(e){if(!liveZoneDrag)return;if(e?.pointerId!==undefined&&liveZoneDrag.pointerId!==e.pointerId)return;liveZoneDrag=null}

export async function saveLiveZones(){
  await setOutput(false);
  renderKernelState(await post('/api/zones/frozen',{rects:rectEdit.rects}));
  closeLiveZoneEditor();notice('区域已保存，会一直定在这里。想让它重新跟着你走，点「恢复跟随」。');
}

export async function cancelLiveZones(){
  await setOutput(false);
  // 这次是点「挪动区域」才定住的，取消就回到跟着走；本来就定住的，回到拖之前的样子。
  renderKernelState(rectEdit.wasFrozen
    ?await post('/api/zones/frozen',{rects:rectEdit.backup})
    :await post('/api/zones/freeze',{frozen:false}));
  closeLiveZoneEditor();notice(rectEdit.wasFrozen?'已取消区域调整。':'已取消，区域继续跟着你走。');
}

// 定住的跟随框放开，重新跟着人走。拖过的大小位置不留：下次定住按那时的位置重新定。
export async function followZones(){
  await setOutput(false);
  renderKernelState(await post('/api/zones/freeze',{frozen:false}));
  if(zoneEditMode)closeLiveZoneEditor();
  notice('区域恢复跟随，重新跟着你走。');
}

// 区域挪到我这里：没定住就在现在的位置定住；定住了就整组框按人现在站的位置搬过来，
// 拖过的大小和相对位置不变。语音、动作绑的「系统功能 → 区域挪到我这里」是同一件事。
export async function moveZonesHere(){
  renderKernelState(await post('/api/zones/move-here',{}));
  if(zoneEditMode){
    rectEdit.rects=frozenRectsFrom(kernelState?.zones);rectEdit.wasFrozen=true;
    renderKernelZones(kernelState?.zones||{});
  }
  notice('区域已挪到你现在的位置。');
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

function renderTriggerLive() {
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
const TRIGGER_CHIP_NAMES = {march: '踏步', calf_back: '小腿后抬', hands_up: '双手过头'};

let rangeTargetKeys = '';

let rangeLogKey = '';

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
  if (currentView !== 'play') return;
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
    hit.hidden = !text;
    if (text && text !== hit.dataset.text) {
      document.getElementById('rangeHitWhat').textContent = text;
      hit.classList.remove('on'); void hit.offsetWidth; hit.classList.add('on');
    }
    hit.dataset.text = text;
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
    target.classList.toggle('unmapped', !label);
    target.classList.toggle('on', held.has(key));
    target.classList.toggle('flash', !held.has(key) && fresh.has(key));
  }

  // 最近触发：有了才出现。
  const card = document.getElementById('recentCard');
  const log = document.getElementById('rangeLog');
  if (!card || !log) return;
  card.hidden = !events.length;
  if (!events.length) return;
  const recent = events.slice(-6).reverse();
  const logKey = recent.map(event => `${event.at}:${event.trigger}`).join('|') + '@' + Math.floor(now);
  if (logKey === rangeLogKey) return;
  rangeLogKey = logKey;
  log.replaceChildren();
  for (const event of recent) {
    const key = String(event.trigger || '');
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'range-log-item';
    row.textContent = `${agoText(Math.max(0, now - Number(event.at || 0)))} · `
      + `${event.label || names.get(key) || key} → ${keyText(event.action)}`;
    row.title = '点一下去改它的键';
    row.addEventListener('click', () => revealBindingRow(key));
    log.appendChild(row);
  }
}
