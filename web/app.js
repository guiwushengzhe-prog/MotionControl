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
const BODY_ZONES = {leftHand:{label:'X',body:'左手',button:'X'},rightHand:{label:'B',body:'右手',button:'B'},leftFoot:{label:'LB',body:'左脚',button:'LB'},rightFoot:{label:'RB',body:'右脚',button:'RB'},headJump:{label:'A',body:'头顶跳跃',button:'A'},lookGate:{label:'上下视角',body:'左手放这里',button:null,gate:true}};

let currentPoseMap=null, kernelState=null, sourceMode='computer', cameraRunning=false, modelAvailable=false, sessionStarted=false, sceneConfigured=false, scenePreparing=false;
const output={enabled:false,mode:'mouse',strength:160,server:null,xinputEnabled:false,xinputMotionLeft:false,xinputUser:null,xinputStatus:null};
const head={algorithm:'pnp',horizontalAlgorithm:'classic',deadzone:.10,sensitivityX:58,sensitivityY:46,enabled:true,invertX:false,invertY:false,verticalLookSource:'hand',verticalExclusive:false,bodyMotionGuard:true};
const gameProfile={catalog:[],selected:null,actions:{},overrides:{}};
let profileAutoSaveTimer=null,profileFlight=null,profileRevision=0,profileSwitching=false;
const profileDirty=new Set();
let serviceReady=false,actionBusy=false,outputEpoch=0,kernelEpoch=0,inputStatus={},headDirty=false;
let currentView='play',desiredSource=null,profileReady=false,profileLoading=false;
let kernelConnected=false,outputConnected=false,voiceInputReady=false;
const BASE_PROFILE_TRIGGERS=[
  {key:'zone.leftHand',group:'zones',id:'leftHand',name:'左手区'},
  {key:'zone.rightHand',group:'zones',id:'rightHand',name:'右手区'},
  {key:'zone.leftFoot',group:'zones',id:'leftFoot',name:'左脚区'},
  {key:'zone.rightFoot',group:'zones',id:'rightFoot',name:'右脚区'},
  {key:'zone.headJump',group:'zones',id:'headJump',name:'头顶跳跃区'},
  {key:'motion.march',group:'motions',id:'march',name:'原地踏步'},
  {key:'motion.calf_back',group:'motions',id:'calf_back',name:'小腿向后'},
  {key:'motion.squat',group:'motions',id:'squat',name:'下蹲'},
  {key:'motion.hands_up',group:'motions',id:'hands_up',name:'双手举过头'},
  {key:'motion.jumping_jack',group:'motions',id:'jumping_jack',name:'开合跳'},
  {key:'motion.side_step_jack',group:'motions',id:'side_step_jack',name:'侧步开合'},
  {key:'motion.cross_knee_elbow',group:'motions',id:'cross_knee_elbow',name:'提膝碰对侧肘'},
  {key:'pose.hands_cross',group:'poses',id:'hands_cross',name:'双手交叉',tapOnly:true},
];
const MOTION_CONFLICT_GROUPS=[
  {ids:['jumping_jack','hands_up'],label:'开合跳与双手过头'},
];
const MOTION_CONFLICT_NAMES={march:'原地踏步',calf_back:'小腿向后',squat:'下蹲',hands_up:'双手过头',jumping_jack:'开合跳',side_step_jack:'侧步开合'};
const ACTION_TYPE_LABELS={keyboard:'键盘',mouse_button:'鼠标按键',mouse_wheel:'鼠标滚轮',gamepad:'Xbox 按键',gamepad_trigger:'Xbox 扳机',gamepad_axis:'Xbox 左摇杆'};
const TARGET_LABELS={LEFT:'左键',RIGHT:'右键',MIDDLE:'中键',X1:'侧键 1',X2:'侧键 2',SCROLL_UP:'向上滚',SCROLL_DOWN:'向下滚',LT:'LT',RT:'RT',L3:'L3',R3:'R3',DPAD_UP:'十字键上',DPAD_DOWN:'十字键下',DPAD_LEFT:'十字键左',DPAD_RIGHT:'十字键右',START:'Start',BACK:'Back',LS_UP:'左摇杆上',LS_DOWN:'左摇杆下',LS_LEFT:'左摇杆左',LS_RIGHT:'左摇杆右'};
const voice={status:null};
const overlay={win:null,canvas:null,ctx:null};
const perfUi={previewBusy:false};
const scene={status:{},zones:{},vertical:{},selected:''};
let zoneEditMode=false,zoneEditBackup=null,liveZoneDrag=null;
const LEGACY_ZONE_ALIASES={leftHand:['leftHandUpper','leftHandLower'],rightHand:['rightHandUpper','rightHandLower']};
const SCENE_EDIT_ZONE_IDS=Object.keys(BODY_ZONES);
let voiceCatalog=[];

function profileTriggers(){
  const voiceTriggers=voiceCatalog
    .filter(item=>!item.system_fixed&&!String(item.id||'').startsWith('game.profile_slot_'))
    .map(item=>({
      key:`voice.${item.id}`,group:'voice',id:item.id,
      name:`语音 · ${item.phrase}`,tapOnly:true,
      defaultBinding:item.default_action?{label:item.label,action:item.default_action}:null,
    }));
  return [...BASE_PROFILE_TRIGGERS,...voiceTriggers];
}

const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
function notice(text){$('#notice').textContent=text;$('#notice').hidden=!text}
async function api(path,opt={}){
  let response;
  try{response=await fetch(path,{...opt,signal:AbortSignal.timeout(8000)})}
  catch{throw new Error('本地服务无响应，请检查连接后重试')}
  let data;
  try{data=await response.json()}catch{throw new Error('服务响应无法读取')}
  if(!response.ok||data.ok===false)throw new Error(data.error||`服务请求失败（${response.status}）`);
  return data;
}
async function post(path,data){return api(path,{method:'POST',headers:{'Content-Type':'application/json; charset=utf-8'},body:JSON.stringify(data)})}

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
    const active=!zoneEditMode&&!!state?.pressed;
    el.classList.toggle('active',active);
    const editCircle=zoneEditMode?scene.zones?.[id]:null;
    const circle=editCircle||state?.circle;
    el.classList.toggle('circle-shape',!!circle&&!def.gate);
    el.querySelector('strong').textContent=def.gate?'上下视角':def.label;
    el.querySelector('small').textContent=def.gate?(active?'已开启':'左手放这里'):def.body;
    el.tabIndex=zoneEditMode?0:-1;
    el.setAttribute('aria-label',def.body+'区域，方向键移动');
    if(circle&&Number.isFinite(Number(circle.cx))&&Number.isFinite(Number(circle.cy))&&Number.isFinite(Number(circle.r))){
      const r=Number(circle.r),cx=Number(circle.cx),cy=Number(circle.cy);
      el.style.display='grid';el.style.left=((cx-r)*100)+'%';el.style.top=((cy-r)*100)+'%';el.style.width=(2*r*100)+'%';el.style.height=(2*r*100)+'%';
      continue;
    }
    const rect=state?.rect;
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
  $('#serviceStatus').textContent=serviceReady?'本地服务已连接':'服务失联 · 当前状态无法确认';
  const missing=[];
  if(!currentPoseMap)missing.push('人体未识别：区域和身体动作不可用');
  else if(!sceneConfigured)missing.push('区域未定位：固定区域不可用');
  $('#mainActionStatus').textContent=!serviceReady?'请检查本地服务；紧急停止可继续重试':
    zoneEditMode?'区域调整中 · 体感输出已关闭':
    (output.enabled?'正在控制游戏':'游戏控制已暂停')+(missing.length?' · '+missing.join('；'):' · 可以开玩');
  $('#hint').textContent=currentPoseMap?'区域亮起表示动作已触发':'请让头部和双肩入镜；脚部动作需要脚部入镜';
}
function renderKernelState(runtime){
  kernelState=runtime?.kernel||runtime||{};sourceMode=runtime?.body_mode||sourceMode;const k=kernelState;
  const frameWidth=Number(k.width)||640,frameHeight=Number(k.height)||480;
  currentPoseMap=k.pose||null;if(canvas.width!==frameWidth||canvas.height!==frameHeight){canvas.width=frameWidth;canvas.height=frameHeight}viewer.style.aspectRatio=`${frameWidth}/${frameHeight}`;draw(currentPoseMap);renderKernelZones(k.zones||{});
  const zonePad={leftHand:'#padX',rightHand:'#padB',leftFoot:'#padLB',rightFoot:'#padRB',headJump:'#padA'};
  const activeZones=[];for(const trigger of BASE_PROFILE_TRIGGERS.filter(t=>t.group==='zones')){const pressed=!!k.zones?.[trigger.id]?.pressed;$(zonePad[trigger.id])?.classList.toggle('active',pressed);if(pressed)activeZones.push(trigger.name)}
  $('#buttonStatus').textContent=activeZones.length?'身体区域：'+activeZones.join(' + '):(currentPoseMap?'身体区域：未触发':'身体区域：等待人体');
  const active=new Set(k.motions||[]),chips={march:['#motionMarch','踏步'],calf_back:['#motionCalf','小腿向后'],squat:['#motionSquat','下蹲'],hands_up:['#motionHands','双手过头'],jumping_jack:['#motionJumpingJack','开合跳'],side_step_jack:['#motionSideStepJack','侧步开合'],cross_knee_elbow:['#motionCrossKneeElbow','提膝碰对侧肘']};
  for(const[id,[sel]]of Object.entries(chips))$(sel)?.classList.toggle('active',active.has(id));
  const poses=new Set(k.poses_active||[]),poseChips={hands_cross:'#poseHandsCross'};
  for(const[id,sel]of Object.entries(poseChips))$(sel)?.classList.toggle('active',poses.has(id));
  const statusParts=[];if(active.size)statusParts.push('动作：'+[...active].map(id=>chips[id]?.[1]||id).join(' + '));if(poses.size)statusParts.push('动作：'+[...poses].map(id=>BASE_PROFILE_TRIGGERS.find(t=>t.id===id)?.name||id).join(' + '));
  $('#motionStatus').textContent=statusParts.join(' · ')||'动作：未触发';
  const hs=k.head||{};
  const guardVersion=String(hs.body_motion_guard_version||k.body_motion_guard_version||'未上报');
  const guardEnabled=hs.body_motion_guard_enabled??k.body_motion_guard_enabled;
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
    const algo=hs.algorithm==='ratio'?'比例':'PnP';
    const horizontalCalibrated=hs.horizontal_calibrated??hs.calibrated;
    $('#headStatus').textContent=horizontalCalibrated?(guardBlocked?'身体动作中 · 左右视角已稳定':`头控 ${algo} · 水平 ${Number(hs.output_x).toFixed(0)}%`):'头控：等待中心，可说“体感开始校准”';
  }
  if(hs.calibrated!==undefined){
    $('#calBtn').textContent=hs.calibrating?'取消校准':'站好并校准';
    $('#calStatus').textContent=hs.calibrating?(hs.notice||hs.quality||'正在校准'):(hs.notice||hs.quality||'等待校准，可说“开始校准”');
    $('#calStatus').title=hs.estimate_error||'';
    renderCalibrationOverlay(hs);
  }
  if(hs.algorithm&&!headDirty&&!document.activeElement?.closest('#headSettings,#advancedSettings')){
    $('#headAlgorithm').value=hs.algorithm;
    const horizontalAlgorithm=String(hs.horizontal_algorithm||'classic');
    head.horizontalAlgorithm=['classic','gesture_v153','frozen22','gesture_v188'].includes(horizontalAlgorithm)?horizontalAlgorithm:'classic';
    if($('#headHorizontalAlgorithm'))$('#headHorizontalAlgorithm').value=head.horizontalAlgorithm;
    head.verticalLookSource=String(hs.verticalLookSource||hs.vertical_look_source||k.vertical_look?.source||'hand')==='head'?'head':'hand';
    head.verticalExclusive=!!(k.vertical_look?.exclusive_axes??hs.vertical_exclusive_axes);
    head.bodyMotionGuard=k.vertical_look?.body_motion_guard!==false;
    if($('#verticalLookSource'))$('#verticalLookSource').value=head.verticalLookSource;
    if($('#verticalExclusive'))$('#verticalExclusive').checked=head.verticalExclusive;
    if($('#bodyMotionGuard'))$('#bodyMotionGuard').checked=head.bodyMotionGuard;
    document.querySelectorAll('.head-vertical-setting').forEach(el=>el.style.setProperty('display',head.verticalLookSource==='head'?'block':'none','important'));
    $('#deadzone').value=Math.round(Number(hs.deadzone||.10)*100);
    $('#speedX').value=Number(hs.sensitivity_x||58);$('#speedY').value=Number(hs.sensitivity_y||46);
    $('#headEnable').checked=!!hs.enabled;$('#invertX').checked=!!hs.invert_x;$('#invertY').checked=!!hs.invert_y;syncControlLabels();
  }
  const camera=runtime?.camera||{running:cameraRunning};
  cameraRunning=!!camera.running;
  sessionStarted=sourceMode==='phone'?true:cameraRunning;
  if(desiredSource===null)$('#poseSource').value=sourceMode;

  if(cameraPreview){
    const showPreview=sourceMode==='computer'&&cameraRunning;
    cameraPreview.hidden=!(showPreview&&cameraPreview.complete&&cameraPreview.naturalWidth);
    if(!showPreview&&cameraPreview.hasAttribute('src')){
      URL.revokeObjectURL(cameraPreview.src);cameraPreview.removeAttribute('src');
    }
  }
  $('#cameraPill').textContent=(sourceMode==='phone'?inputStatus.mobile_pose_connected:cameraRunning)?'摄像头 ✓':'摄像头';$('#cameraPill').className='pill '+(sourceMode==='phone'||cameraRunning?'ok':'bad');
  // phonePill is owned by renderInputStatus (/api/input/status); kernel status has no transport state.
  $('#posePill').textContent=currentPoseMap?'人体 ✓':'人体';$('#posePill').className='pill '+(currentPoseMap?'ok':'bad');const gateActive=!!k.vertical_gate_active;const verticalSource=String(hs.verticalLookSource||hs.vertical_look_source||k.vertical_look?.source||'hand')==='head'?'头部':'右手';const gateStatus=$('#lookGateStatus');if(gateStatus){const paused=!!hs.horizontal_paused_by_vertical_gate;gateStatus.textContent=gateActive?`上下视角已开启 · ${verticalSource}控制上下${paused?' · 左右暂停':''}`:'上下视角待机 · 左手放入绿色区域开启';gateStatus.className='look-gate-status '+(gateActive?'active':'')}renderOverlay(currentPoseMap);renderMainStatus();

}
function renderInputStatus(status){
  inputStatus=status||{};
  const connected=!!(status.mobile_pose_connected||status.handheld_connected);
  for(const id of ['mobileStatus','phonePill']){
    $('#'+id).textContent=connected?(status.mobile_pose_connected?'手机摄像头已连接':'手机手持端已连接'):'手机未连接';
    $('#'+id).className='pill '+(connected?'ok':'bad');
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
  for(const trigger of BASE_PROFILE_TRIGGERS.filter(item=>item.group==='motions')){
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
  const t=String(action.target||'').toUpperCase();
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
    const binding=bindingFor(trigger),label=bindingLabel(binding);
    if(BODY_ZONES[trigger.id])BODY_ZONES[trigger.id].label=label;
    const el=$(zonePad[trigger.id]);if(el)el.textContent=label;
  }
  renderKernelZones(kernelState?.zones||{});
}
function profileMetaText(profile){
  if(!profile)return '未选择游戏';
  const appid=profile.appid||profile.steam_appid||'';
  const source=profile.source||{};
  const verified=source.verified?' · 已核验':'';
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
  $('#profileGameName').textContent=p?.name||'未选择游戏';
  $('#currentGameName').textContent=p?.name||'未选择游戏';
  $('#profileMeta').textContent=profileMetaText(p);
  syncProfileZoneLabels();
}
function renderProfileCatalog(games){
  gameProfile.catalog=Array.isArray(games)?games:[];
  const select=$('#profileSelect'),selectedId=gameProfile.selected?.selected_id||gameProfile.selected?.id||'';
  select.replaceChildren();
  if(!gameProfile.catalog.length){const o=document.createElement('option');o.value='';o.textContent='没有匹配的游戏';select.appendChild(o);return}
  for(const g of gameProfile.catalog){const o=document.createElement('option');o.value=g.id;o.textContent=`${g.name}${g.appid?` · ${g.appid}`:''}`;select.appendChild(o)}
  if(gameProfile.catalog.some(g=>g.id===selectedId))select.value=selectedId;
}
async function searchProfiles(){
  const q=$('#profileSearch').value.trim();
  const data=await api('/api/game-profiles/catalog'+(q?'?q='+encodeURIComponent(q):''));
  renderProfileCatalog(data.games||[]);
  $('#profileMeta').textContent=`离线库 ${Number(data.library_count||data.count||0)} 款 · 当前显示 ${Number(data.count||0)} 款`;
}
async function refreshProfile(){
  const [selected,actions]=await Promise.all([api('/api/game-profiles/selected'),api('/api/output/actions')]);
  gameProfile.selected=selected.profile||null;gameProfile.actions=actions.actions||{};gameProfile.overrides=gameProfile.selected?.overrides||{};
  renderProfileHeader();renderProfileBindingRows();await searchProfiles();renderProfileHeader();
}
async function applySelectedProfile(){
  const id=$('#profileSelect').value;if(!id||profileSwitching)return;
  await profileOperation(async()=>{
    await saveProfileBindings();
    const data=await post('/api/game-profiles/select',{id});
    gameProfile.selected=data.profile;
    gameProfile.overrides=data.profile.overrides||{};
    await refreshVoiceCommands();renderProfileHeader();renderProfileBindingRows();
    notice(`已切换游戏：${data.profile.name}`);
  });
}
async function profileOperation(operation){
  profileSwitching=true;$('#mappingFields').disabled=true;
  for(const id of ['profileApplyBtn','resetProfileBindingsBtn','profileSelect'])$('#'+id).disabled=true;
  try{await operation()}
  finally{
    profileSwitching=false;$('#mappingFields').disabled=false;
    for(const id of ['profileApplyBtn','resetProfileBindingsBtn','profileSelect'])$('#'+id).disabled=false;
  }
}
function makeTypeSelect(binding){
  const sel=document.createElement('select');sel.className='binding-type';
  const none=document.createElement('option');none.value='';none.textContent='不映射';sel.appendChild(none);
  for(const type of Object.keys(ACTION_TYPE_LABELS)){if(!gameProfile.actions?.[type])continue;const o=document.createElement('option');o.value=type;o.textContent=ACTION_TYPE_LABELS[type];sel.appendChild(o)}
  sel.value=binding?.disabled?'':(binding?.action?.type||'');return sel;
}
function fillTargetControl(container,type,value=''){
  container.replaceChildren();if(!type)return;
  const meta=gameProfile.actions?.[type]||{};
  if(type==='gamepad'){
    const select=document.createElement('select');select.className='binding-target';
    for(const key of meta.targets||['A','B','X','Y','LB','RB','L3','R3','START','BACK','DPAD_UP','DPAD_DOWN','DPAD_LEFT','DPAD_RIGHT']){const option=document.createElement('option');option.value=key;option.textContent=TARGET_LABELS[key]||key;select.appendChild(option)}
    const custom=document.createElement('option');custom.value='__combo__';custom.textContent='组合键…';select.appendChild(custom);
    const raw=Array.isArray(value)?value.join('+'):String(value||'A');
    const combo=document.createElement('input');combo.type='text';combo.placeholder='例如 LB+A';combo.value=raw.includes('+')?raw:'';
    const update=()=>{const isCombo=select.value==='__combo__';select.className=isCombo?'binding-gamepad-select':'binding-target';combo.className=isCombo?'binding-target':'';combo.hidden=!isCombo};
    select.value=[...select.options].some(o=>o.value===raw)?raw:'__combo__';
    select.addEventListener('change',update);update();container.append(select,combo);return;
  }
  if(meta.free_text){const input=document.createElement('input');input.className='binding-target';input.type='text';input.placeholder=meta.placeholder||'例如 W / SPACE / CTRL+W';input.value=Array.isArray(value)?value.join('+'):(value||'');container.appendChild(input);return}
  const select=document.createElement('select');select.className='binding-target';
  for(const target of meta.targets||[]){const o=document.createElement('option');o.value=target;o.textContent=TARGET_LABELS[target]||target;select.appendChild(o)}
  if(value&&[...select.options].some(o=>o.value===value))select.value=value;container.appendChild(select);
}
function fillBehaviorControl(container,trigger,type,value){
  container.replaceChildren();
  if(trigger.tapOnly||type==='mouse_wheel'){const span=document.createElement('span');span.className='binding-behavior';span.textContent='进入时触发一次';span.dataset.value='tap';container.appendChild(span);return}
  if(!type){const span=document.createElement('span');span.className='binding-behavior';span.textContent='—';span.dataset.value='hold';container.appendChild(span);return}
  const sel=document.createElement('select');sel.className='binding-behavior';
  for(const[v,t]of [['hold','保持动作时持续'],['tap','进入时触发一次']]){const o=document.createElement('option');o.value=v;o.textContent=t;sel.appendChild(o)}
  sel.value=value==='tap'?'tap':'hold';container.appendChild(sel);
}
function renderProfileBindingRows(){
  const box=$('#profileBindingRows');if(!box)return;box.replaceChildren();
  if(!gameProfile.selected){box.innerHTML='<div class="profile-empty">还没有可编辑的游戏配置。</div>';return}
  const triggers=profileTriggers();
  const groups=[
    {id:'zones',title:'身体区域',help:'手、脚或头部进入对应区域时触发',filter:t=>t.group==='zones',open:true},
    {id:'body',title:'身体动作',help:'识别到动作时触发；开合跳与双手过头顶不能同时映射',filter:t=>t.group==='motions'||t.group==='poses',open:true},
    {id:'voice',title:'语音',help:'说出完整口令后触发一次；系统安全口令不可改',filter:t=>t.group==='voice',open:false},
  ];
  for(const group of groups){
    const items=triggers.filter(group.filter);if(!items.length)continue;
    const details=document.createElement('details');details.className='binding-group';details.open=group.open;
    const summary=document.createElement('summary');summary.textContent=`${group.title} · ${items.length} 项`;
    const help=document.createElement('div');help.className='binding-group-help';help.textContent=group.help;
    const rows=document.createElement('div');rows.className='binding-group-rows';
    for(const trigger of items){
      const binding=bindingFor(trigger),action=binding?.disabled?null:binding?.action;
      const row=document.createElement('div');row.className='binding-row';row.dataset.trigger=trigger.key;
      const name=document.createElement('div');name.className='trigger-name';name.textContent=trigger.name;
      const type=makeTypeSelect(binding);
      const target=document.createElement('div');target.className='binding-target-box';fillTargetControl(target,type.value,action?.target||'');
      const behavior=document.createElement('div');behavior.className='binding-behavior-box';fillBehaviorControl(behavior,trigger,type.value,action?.behavior||'hold');
      type.addEventListener('change',()=>{fillTargetControl(target,type.value,'');fillBehaviorControl(behavior,trigger,type.value,'hold');syncMotionConflictChoices()});
      row.append(name,type,target,behavior);
      row.querySelectorAll('input,select').forEach(control=>control.setAttribute('aria-label',trigger.name+' '+(control.className.includes('type')?'输出类型':'键位或触发方式')));
      if(trigger.group==='motions'){const note=document.createElement('div');note.className='motion-conflict-note';note.hidden=true;row.appendChild(note)}
      rows.appendChild(row);
    }
    details.append(summary,help,rows);box.appendChild(details);
  }
  syncMotionConflictChoices();
}
function readProfileOverrides(){
  const overrides=structuredClone(gameProfile.overrides);
  for(const trigger of profileTriggers()){
    if(!profileDirty.has(trigger.key))continue;
    const row=document.querySelector(`.binding-row[data-trigger="${trigger.key}"]`);if(!row)continue;
    const type=row.querySelector('.binding-type')?.value||'';
    if(!type){overrides[trigger.key]=null;continue}
    const target=String(row.querySelector('.binding-target')?.value||'').trim().toUpperCase();
    if(!target)throw new Error(`${trigger.name} 还没有选择具体键位`);
    const behavior=trigger.tapOnly||type==='mouse_wheel'?'tap':(row.querySelector('select.binding-behavior')?.value||'hold');
    overrides[trigger.key]={action:{type,target,behavior}};
  }
  const conflicts=motionConflictsForSelection(selectedMotionIdsFromRows());
  if(conflicts.length)throw new Error(`动作冲突：${motionConflictText(conflicts)}。开合跳与双手过头顶只能选择一个`);
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
    $('#profileSaveStatus').textContent='保存失败，草稿已保留：'+error.message;
    $('#retryProfileSaveBtn').hidden=false;throw error;
  }finally{profileFlight=null}
  if(profileDirty.size)return saveProfileBindings();
  $('#profileSaveStatus').textContent='已自动保存';
}
function scheduleProfileAutoSave(event){
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
async function refreshCameraConfig(){try{const data=await api('/api/camera/config');const select=$('#cameraBackend');if(select&&data.preference)select.value=data.preference}catch{}}
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
  $('#outputStatus').textContent=`${s.mouse_available?'鼠标可用':'鼠标不可用'} · ${s.gamepad_connected?'虚拟手柄已连接':'虚拟手柄未连接'}`+(s.last_error?' · '+s.last_error:'');
  if(s.xinput_merge_enabled!==undefined){
    output.xinputEnabled=!!s.xinput_merge_enabled;output.xinputUser=s.xinput_selected_user??null;
    output.xinputMotionLeft=!!s.xinput_motion_left_enabled;renderXinputMotionLeft();
  }
  renderMainStatus();
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
  ++outputEpoch;++kernelEpoch;
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
  if(enabled&&source==='computer'&&!result.camera?.running)throw new Error(result.camera?.last_error||'摄像头启动失败');
  ++kernelEpoch;desiredSource=source;
  renderKernelState(result);await refreshInput();
  notice(enabled?(source==='phone'?'已选择手机摄像头，等待手机连接':'摄像头识别已启动，游戏控制保持暂停'):'识别已停止');
}

const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function waitForPose(timeoutMs=8000){const deadline=Date.now()+timeoutMs;while(Date.now()<deadline){try{const r=await api('/api/kernel/status'),k=r?.kernel||r;if(k?.pose)return true}catch{}await sleep(250)}return false}
async function ensureInitialSceneLayout(){
  if(scenePreparing)return false;
  try{
    const current=await api('/api/scene/status');renderSceneEditor(current);if(current?.configured)return true;
    scenePreparing=true;renderMainStatus();notice('首次使用：请站到正常游戏位置，正在自动定位 6 个体感区域…');
    if(!(await waitForPose(8000))){notice('还没有识别到头和双肩。请站到镜头前后再点“开始游戏控制”。首次定位不要求全身入镜。');return false}
    const result=await post('/api/scene/capture',{});
    if(result?.configured){renderSceneEditor(result);notice('6 个体感区域已自动定位。');return true}
    if(result?.scene?.configured){renderSceneEditor(result.scene);notice('6 个体感区域已自动定位。');return true}
    if(result?.pending){
      const deadline=Date.now()+8000;while(Date.now()<deadline){await sleep(300);const st=await api('/api/scene/status');renderSceneEditor(st);if(st?.configured){notice('手机场景已收到，6 个体感区域已自动定位。');return true}}
      notice('正在等待手机返回场景截图。输出保持关闭，可稍后再点一次。');return false;
    }
    const st=await api('/api/scene/status');renderSceneEditor(st);return !!st?.configured;
  }catch(e){notice('首次区域定位尚未完成：'+(e?.message||e));return false}
  finally{scenePreparing=false;renderMainStatus()}
}
async function handleMainAction(){
  if(!sessionStarted&&!inputStatus.handheld_connected&&!voiceInputReady){
    await setSource($('#poseSource').value,true);
    if(sourceMode==='computer')await ensureInitialSceneLayout();
    return;
  }
  await setOutput(!output.enabled);
}
async function startCalibration(){const running=!!kernelState?.head?.calibrating;try{renderKernelState(await post(running?'/api/head/calibration/cancel':'/api/head/calibration/start',{}));notice(running?'校准已取消':'校准已开始：看向游戏屏幕中心，保持自然姿势')}catch(e){notice('中心设置失败：'+(e?.message||e))}}
async function centerHead(){try{renderKernelState(await post('/api/head/calibration/center',{}));notice('视角中心已更新。')}catch(e){notice('视角回正失败：'+(e?.message||e))}}

function drawOverlayZones(octx,w,h,zones={}){
  for(const[id,def]of Object.entries(BODY_ZONES)){
    const state=zones[id];if(!state)continue;const active=!!state.pressed,isGate=!!def.gate;
    octx.save();octx.lineWidth=Math.max(2,w/220);octx.strokeStyle=isGate?(active?'#62ff91':'#62d982'):(active?'#ff5966':'rgba(255,255,255,.78)');octx.fillStyle=isGate?(active?'rgba(45,210,95,.30)':'rgba(30,150,75,.15)'):(active?'rgba(255,70,80,.26)':'rgba(0,0,0,.12)');if(isGate&&!active)octx.setLineDash([Math.max(4,w/100),Math.max(3,w/140)]);
    let x=0,y=0,ww=0,hh=0;const c=state.circle,r=state.rect;
    if(c){const radius=Number(c.r)||0;ww=2*radius*w;hh=2*radius*h;x=(1-Number(c.cx)-radius)*w;y=(Number(c.cy)-radius)*h}
    else if(r){x=(1-Number(r.x2))*w;y=Number(r.y1)*h;ww=(Number(r.x2)-Number(r.x1))*w;hh=(Number(r.y2)-Number(r.y1))*h}
    if(ww<=0||hh<=0){octx.restore();continue}octx.beginPath();if(isGate)octx.roundRect(x,y,ww,hh,Math.max(8,w/70));else if(c)octx.ellipse(x+ww/2,y+hh/2,ww/2,hh/2,0,0,Math.PI*2);else octx.roundRect(x,y,ww,hh,Math.max(6,w/90));octx.fill();octx.stroke();octx.setLineDash([]);octx.fillStyle='#fff';octx.font=`800 ${Math.max(10,Math.round(w/46))}px system-ui,sans-serif`;octx.textAlign='center';octx.textBaseline='middle';octx.fillText(isGate?(active?'上下视角 已开启':'上下视角'):def.label,x+ww/2,y+hh/2);octx.restore();
  }
}
function renderOverlay(map=currentPoseMap){
  if(!overlay.win||overlay.win.closed||!overlay.canvas||!overlay.ctx)return;const c=overlay.canvas,octx=overlay.ctx,w=c.width,h=c.height;octx.setTransform(1,0,0,1,0,0);octx.clearRect(0,0,w,h);octx.fillStyle='#050608';octx.fillRect(0,0,w,h);
  draw(map,octx,w,h,true);
  drawOverlayZones(octx,w,h,kernelState?.zones||{});const buttons=kernelState?.buttons||[],motions=kernelState?.motions||[];const gate=!!kernelState?.vertical_gate_active;const text=gate?'上下视角已开启':(buttons.length?`区域 ${buttons.join('+')}`:(motions.length?`动作 ${motions.join('+')}`:(map?'未触发':'未识别人体')));octx.fillStyle='rgba(0,0,0,.62)';octx.fillRect(0,h-Math.max(25,h/10),w,Math.max(25,h/10));octx.fillStyle='#fff';octx.font=`600 ${Math.max(12,Math.round(w/32))}px system-ui,sans-serif`;octx.textAlign='left';octx.textBaseline='alphabetic';octx.fillText(`${output.enabled?'输出开':'输出关'} · ${text}`,Math.max(7,w/70),h-Math.max(7,h/70))
}

async function toggleOverlay(){if(overlay.win&&!overlay.win.closed){try{overlay.win.close()}catch{}overlay.win=null;overlay.canvas=null;overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗';return}if(!window.documentPictureInPicture?.requestWindow){notice('当前浏览器不支持置顶游戏悬浮窗。');return}try{const pip=await window.documentPictureInPicture.requestWindow({width:420,height:315});pip.document.title='MotionControl';pip.document.body.style.cssText='margin:0;overflow:hidden;background:#050608;width:100vw;height:100vh';const c=pip.document.createElement('canvas');c.width=640;c.height=480;c.style.cssText='display:block;width:100vw;height:100vh;object-fit:contain;background:#050608';pip.document.body.appendChild(c);overlay.win=pip;overlay.canvas=c;overlay.ctx=c.getContext('2d');pip.addEventListener('pagehide',()=>{overlay.win=overlay.canvas=overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗'},{once:true});$('#overlayBtn').textContent='关闭悬浮';renderOverlay(currentPoseMap)}catch(e){notice('悬浮窗启动失败：'+(e?.message||e))}}


function addVoiceRow(mapping={phrase:'',type:'keyboard',target:''}){const row=document.createElement('div');row.className='voice-row';const phrase=document.createElement('input');phrase.className='voice-phrase';phrase.placeholder='说：例如 地图';phrase.value=mapping.phrase||'';const type=document.createElement('select');type.className='voice-type';for(const[value,label]of[['keyboard','键盘/组合键'],['gamepad','Xbox 键'],['system','系统命令']]){const o=document.createElement('option');o.value=value;o.textContent=label;type.appendChild(o)}type.value=mapping.type||'keyboard';const target=document.createElement('input');target.className='voice-target';target.value=mapping.target||'';const remove=document.createElement('button');remove.type='button';remove.className='btn voice-remove';remove.textContent='删';remove.addEventListener('click',()=>{row.remove();if(!$('#voiceRows').children.length)addVoiceRow()});row.append(phrase,type,target,remove);$('#voiceRows').appendChild(row)}
function readVoiceMappings(){const rows=[...document.querySelectorAll('.voice-row')],items=[],old=new Map((voice.status?.mappings||[]).map(m=>[m.phrase,m]));for(const row of rows){const phrase=row.querySelector('.voice-phrase').value.trim(),type=row.querySelector('.voice-type').value,target=row.querySelector('.voice-target').value.trim();if(!phrase&&!target)continue;if(!phrase||!target)throw new Error('语音命令必须同时填写“说什么”和“输出什么”');const item={phrase,type,target},previous=old.get(phrase);if(previous?.synonyms?.length)item.synonyms=[...previous.synonyms];items.push(item)}return items}
function renderVoiceRows(items){$('#voiceRows').replaceChildren();for(const m of items||[])addVoiceRow(m);if(!$('#voiceRows').children.length)addVoiceRow()}
function renderVoiceStatus(s=voice.status){
  if(!s)return;voice.status=s;const has=!!s.model_ready,connected=!!s.connected;const isSingleKws=String(s.recognizer_mode||'').includes('single_stage')||String(s.recognizer_mode||'').includes('kws');
  $('#voiceMode').textContent=has?(isSingleKws?`短语识别 · ${s.supported_count||0} 条`:`语音 · ${s.supported_count||0} 条`):'未就绪';$('#voiceMode').className='pill '+(has?'ok':'warn');
  const pcOk=connected&&s.source_kind==='computer'&&s.available&&s.model_ready&&s.audio_ready&&(s.audio_alive||s.stream_alive);const phoneOk=connected&&s.source_kind!=='computer';const ready=pcOk||phoneOk;voiceInputReady=ready;
  $('#voicePill').textContent=ready?'语音 ✓':(connected?'语音准备中':'语音');$('#voicePill').className='pill '+(ready?'ok':(connected?'warn':'optional'));
  const phrase=String(s.last_command||s.final||'').trim();$('#voiceStatus').textContent=phrase?`已识别：${phrase}`:(ready?'直接说完整口令，例如“体感截图”':'语音尚未准备好');
  const modelPath=s.model_path||s.command_model_path||'—';const mp=$('#voiceModelPath');if(mp){mp.textContent='模型：'+modelPath;mp.title=modelPath}
  const diag=$('#voiceDiagnostic');if(diag){diag.textContent=[`模式：${s.recognizer_mode||'—'}`,`词条：${s.supported_count??'—'}`,`模型：${modelPath}`,`音频：${s.audio_ready?'已准备':'未准备'} / ${s.audio_alive||s.stream_alive?'运行中':'空闲'}`,`音量：${Number(s.rms||0).toFixed(0)} · 字节：${s.bytes_received||0}`,`最后命令：${phrase||'—'}`,`错误：${s.last_error||'—'}`].join('\n')}
}

async function saveVoiceMappings(){const s=await post('/api/voice/config',{mappings:readVoiceMappings()});voice.status=s;renderVoiceStatus(s);return s}
async function refreshVoice(){try{voice.status=await api('/api/voice/status');renderVoiceStatus(voice.status)}catch{voiceInputReady=false;$('#voiceStatus').textContent='语音状态无法确认'}}
function voiceActionLabel(action){if(!action)return '当前游戏未启用';if(action.type==='system')return '系统功能 · '+(action.target||'');return `${ACTION_TYPE_LABELS[action.type]||action.type} · ${targetLabel(action)}`}
function renderVoiceCommandCard(command){const card=document.createElement('div');card.className='voice-command-card';card.setAttribute('role','listitem');const phrase=document.createElement('div');phrase.textContent=command.phrase||'';const label=document.createElement('small');label.textContent=command.system_fixed?`${command.label||''} · 系统固定`:`${command.label||''} · ${voiceActionLabel(command.effective_action)}`;card.append(phrase,label);return card}
function renderVoiceCommandCatalog(commands){
  voiceCatalog=Array.isArray(commands)?commands:[];
  const full=$('#voiceCommandGrid');full.replaceChildren();
  const visible=voiceCatalog.filter(item=>!String(item.id||'').startsWith('game.profile_slot_')||item.effective_action);
  for(const [name,items] of [
    ['系统口令 · 所有游戏通用',visible.filter(item=>item.system_fixed)],
    ['当前游戏口令',visible.filter(item=>!item.system_fixed)],
  ]){
    const section=document.createElement('section');section.className='voice-group';
    const title=document.createElement('h3');title.textContent=name;
    const grid=document.createElement('div');grid.className='voice-command-grid';
    for(const item of items)grid.append(renderVoiceCommandCard(item));
    section.append(title,grid);full.append(section);
  }
}
async function refreshVoiceCommands(){try{const data=await api('/api/voice/commands');renderVoiceCommandCatalog(data.commands||[])}catch{renderVoiceCommandCatalog([])}}

function syncControlLabels(){head.algorithm=$('#headAlgorithm').value;const horizontalAlgorithm=$('#headHorizontalAlgorithm')?.value;head.horizontalAlgorithm=['classic','gesture_v153','frozen22','gesture_v188'].includes(horizontalAlgorithm)?horizontalAlgorithm:'classic';head.verticalLookSource=$('#verticalLookSource')?.value==='head'?'head':'hand';head.verticalExclusive=!!$('#verticalExclusive')?.checked;head.bodyMotionGuard=$('#bodyMotionGuard')?.checked!==false;head.deadzone=Number($('#deadzone').value)/100;head.sensitivityX=Number($('#speedX').value);head.sensitivityY=Number($('#speedY').value);head.enabled=$('#headEnable').checked;head.invertX=$('#invertX').checked;head.invertY=$('#invertY').checked;document.querySelectorAll('.head-vertical-setting').forEach(el=>el.style.setProperty('display',head.verticalLookSource==='head'?'block':'none','important'));$('#deadzoneValue').textContent=Math.round(head.deadzone*100)+'%';$('#speedXValue').textContent=head.sensitivityX+'%';$('#speedYValue').textContent=head.sensitivityY+'%';output.strength=Number($('#strength').value);$('#strengthValue').textContent=output.strength+'%'}
async function pushHeadConfig(){
  syncControlLabels();
  renderKernelState(await post('/api/head/config',{
    algorithm:head.algorithm,horizontal_algorithm:head.horizontalAlgorithm,deadzone:head.deadzone,
    sensitivity_x:head.sensitivityX,sensitivity_y:head.sensitivityY,enabled:head.enabled,
    invert_x:head.invertX,invert_y:head.invertY,vertical_look_source:head.verticalLookSource,
    vertical_exclusive:head.verticalExclusive,body_motion_guard:head.bodyMotionGuard,
  }));
  if(sceneConfigured){
    const vertical={...scene.status.vertical_look,source:head.verticalLookSource,verticalLookSource:head.verticalLookSource,
      exclusive_axes:head.verticalExclusive,body_motion_guard:head.bodyMotionGuard};
    scene.status=await post('/api/scene/layout',{zones:scene.status.zones,vertical_look:vertical});
  }
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
function renderSceneEditor(st,replaceDraft=false){
  scene.status=st||{};sceneConfigured=!!st?.configured;
  if(!zoneEditMode||replaceDraft){scene.zones=structuredClone(st?.zones||{});scene.vertical=structuredClone(st?.vertical_look||{})}
  $('#sceneStatus').textContent=sceneConfigured?'区域已定位；保存后生效':'请先让头部和双肩入镜，记录参考位置';
  const select=$('#sceneZoneSelect');select.replaceChildren();
  for(const id of SCENE_EDIT_ZONE_IDS){if(scene.zones[id])select.append(new Option(BODY_ZONES[id].body,id))}
  if(!scene.zones[scene.selected])scene.selected=select.value;
  select.value=scene.selected;syncSceneTools();renderKernelZones(kernelState?.zones||{});renderMainStatus();
}




function syncSceneTools(){const z=scene.zones[scene.selected];if(z){$('#sceneRadius').value=Number(z.r||.07)*100;$('#sceneRadiusValue').textContent=(Number(z.r||.07)*100).toFixed(1)+'%'}$('#sceneVerticalCenter').value=Number(scene.vertical.center_y??.5)*100;$('#sceneVerticalRange').value=Number(scene.vertical.range_y||.18)*100;$('#sceneVerticalRangeValue').textContent=(Number(scene.vertical.range_y||.18)*100).toFixed(0)+'%';$('#sceneVerticalDeadzone').value=Number(scene.vertical.deadzone||.1)*100;$('#sceneVerticalDeadzoneValue').textContent=(Number(scene.vertical.deadzone||.1)*100).toFixed(0)+'%'}
async function refreshScene(){try{renderSceneEditor(await api('/api/scene/status'))}catch(e){$('#sceneStatus').textContent='场景状态读取失败：'+e.message}}
async function openLiveZoneEditor(){
  if(zoneEditMode)return;
  await setOutput(false);
  if(!sceneConfigured&&!(await ensureInitialSceneLayout()))return;
  renderSceneEditor(await api('/api/scene/status'));
  if(!sceneConfigured)throw new Error('尚未建立区域，请让头部和双肩入镜');
  zoneEditBackup={zones:structuredClone(scene.zones),vertical:structuredClone(scene.vertical)};
  zoneEditMode=true;viewer.classList.add('zone-editing');$('#zoneEditBar').hidden=false;
  renderKernelZones(kernelState?.zones||{});renderMainStatus();$('#sceneZoneSelect').focus();
}
function closeLiveZoneEditor(){
  zoneEditMode=false;liveZoneDrag=null;zoneEditBackup=null;
  viewer.classList.remove('zone-editing');$('#zoneEditBar').hidden=true;
  renderKernelZones(kernelState?.zones||{});renderMainStatus();$('#adjustZonesBtn').focus();
}
function startLiveZoneDrag(e){
  if(!zoneEditMode)return;const el=e.currentTarget,id=el?.dataset?.zone;if(!id||!scene.zones?.[id])return;
  scene.selected=id;$('#sceneZoneSelect').value=id;syncSceneTools();
  e.preventDefault();el.setPointerCapture?.(e.pointerId);liveZoneDrag={id,pointerId:e.pointerId};moveLiveZoneDrag(e);
}
function moveLiveZoneDrag(e){
  if(!zoneEditMode||!liveZoneDrag)return;const z=scene.zones?.[liveZoneDrag.id];if(!z)return;
  const rect=viewer.getBoundingClientRect(),r=Number(z.r)||.07;
  const displayX=clamp((e.clientX-rect.left)/Math.max(1,rect.width),0,1);
  const displayY=clamp((e.clientY-rect.top)/Math.max(1,rect.height),0,1);
  // The preview layer is mirrored for natural selfie interaction. Scene data
  // remains raw/unmirrored, so convert display X exactly once here.
  z.cx=clamp(1-displayX,r,1-r);z.cy=clamp(displayY,r,1-r);
  renderKernelZones(kernelState?.zones||{});
}
function endLiveZoneDrag(e){if(!liveZoneDrag)return;if(e?.pointerId!==undefined&&liveZoneDrag.pointerId!==e.pointerId)return;liveZoneDrag=null}
async function saveLiveZones(){
  await setOutput(false);
  const result=await post('/api/scene/layout',{zones:scene.zones,vertical_look:scene.vertical});
  renderSceneEditor(result,true);closeLiveZoneEditor();notice('体感区域已保存。');
}
async function cancelLiveZones(){
  // Capture/rematch updates the server immediately; restore the full saved layout on cancel.
  await setOutput(false);
  if(zoneEditBackup){
    const result=await post('/api/scene/layout',{zones:zoneEditBackup.zones,vertical_look:zoneEditBackup.vertical});
    renderSceneEditor(result,true);
  }
  closeLiveZoneEditor();notice('已取消区域调整。');
}

async function updateScene(purpose){
  await setOutput(false);
  const result=await post('/api/scene/'+purpose,{});
  let status=result.scene||result;
  if(result.pending){
    const previous=JSON.stringify(scene.status),deadline=Date.now()+8000;
    do{await sleep(300);status=await api('/api/scene/status')}
    while(Date.now()<deadline&&JSON.stringify(status)===previous);
    if(JSON.stringify(status)===previous)throw new Error('尚未收到手机参考位置，请稍后重试');
  }
  if(status.last_result?.ok===false)throw new Error(status.last_result.message||'定位未成功');
  renderSceneEditor(status,true);notice('位置已更新，可继续调整或取消。');
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
  if(view==='devices')void refreshXinput();
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
  syncControlLabels();
  await refreshKernel();await refreshOutput();
  const results=await Promise.allSettled([
    refreshInput(),refreshXinput(),refreshVoice(),refreshVoiceCommands(),refreshCameraConfig(),refreshScene(),
    api('/api/models').then(data=>{
      modelAvailable=!!data.models?.[0]?.available;
      $('#modelStatus').textContent=modelAvailable?'电脑人体模型可用':'电脑人体模型不可用；手机输入、实体手柄不受此项影响';
    }),
  ]);
  if(results.some(result=>result.status==='rejected'))notice('部分设备信息尚未读取，可继续使用已连接的输入');
  await loadProfiles();renderVoiceRows(voice.status?.mappings||[]);
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
bind('profileSearchBtn',searchProfiles);
$('#profileSearch').addEventListener('keydown',e=>{if(e.key==='Enter')void runAction(searchProfiles)});
bind('profileApplyBtn',applySelectedProfile);bind('resetProfileBindingsBtn',resetProfileBindings);
bind('retryProfileSaveBtn',saveProfileBindings);
for(const event of ['input','change'])$('#profileBindingRows').addEventListener(event,e=>{syncMotionConflictChoices();scheduleProfileAutoSave(e)});
bind('adjustZonesBtn',openLiveZoneEditor);bind('saveLiveZonesBtn',saveLiveZones);bind('cancelLiveZonesBtn',cancelLiveZones);
bind('sceneCaptureBtn',()=>updateScene('capture'));bind('sceneRematchBtn',()=>updateScene('rematch'));
$('#sceneZoneSelect').addEventListener('change',e=>{scene.selected=e.target.value;syncSceneTools()});
document.querySelectorAll('.zone').forEach(el=>{
  for(const [event,handler] of [['pointerdown',startLiveZoneDrag],['pointermove',moveLiveZoneDrag],['pointerup',endLiveZoneDrag],['pointercancel',endLiveZoneDrag]])el.addEventListener(event,handler);
  el.addEventListener('keydown',e=>{
    if(!zoneEditMode||!e.key.startsWith('Arrow'))return;
    e.preventDefault();const z=scene.zones[el.dataset.zone];if(!z)return;
    const amount=e.shiftKey?.02:.005;
    if(e.key==='ArrowLeft')z.cx+=amount;if(e.key==='ArrowRight')z.cx-=amount;
    if(e.key==='ArrowUp')z.cy-=amount;if(e.key==='ArrowDown')z.cy+=amount;
    z.cx=clamp(z.cx,z.r,1-z.r);z.cy=clamp(z.cy,z.r,1-z.r);renderKernelZones(kernelState?.zones||{});
  });
});
$('#sceneRadius').addEventListener('input',e=>{
  const z=scene.zones[scene.selected];if(!z)return;
  z.r=Number(e.target.value)/100;z.cx=clamp(z.cx,z.r,1-z.r);z.cy=clamp(z.cy,z.r,1-z.r);
  syncSceneTools();renderKernelZones(kernelState?.zones||{});
});
for(const [id,key] of [['sceneVerticalRange','range_y'],['sceneVerticalDeadzone','deadzone'],['sceneVerticalCenter','center_y']]){
  $('#'+id).addEventListener('input',e=>{scene.vertical[key]=Number(e.target.value)/100;syncSceneTools()});
}
$('#stopBtn').addEventListener('click',emergencyStop);
bind('calBtn',async()=>{await setOutput(false);await startCalibration()});bind('centerBtn',centerHead);
bind('calibrationCancel',startCalibration);
$('#calibrationOverlay').addEventListener('cancel',e=>{e.preventDefault();void runAction(startCalibration)});
$('#voiceCommandsBtn').addEventListener('click',()=>$('#voiceCommandsMask').showModal());
$('#closeVoiceCommandsBtn').addEventListener('click',()=>$('#voiceCommandsMask').close());
$('#poseSource').addEventListener('change',()=>{
  desiredSource=$('#poseSource').value;$('#phoneGuide').open=desiredSource==='phone';
  notice('已选择'+(desiredSource==='phone'?'手机摄像头':'电脑摄像头')+'，点击“连接并开始识别”应用');
});
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
for(const id of ['headAlgorithm','headHorizontalAlgorithm','verticalLookSource','verticalExclusive','bodyMotionGuard','deadzone','speedX','speedY','headEnable','invertX','invertY']){
  $('#'+id).addEventListener('input',()=>{headSaver.dirty();syncControlLabels()});
  $('#'+id).addEventListener('change',()=>{headSaver.dirty();syncControlLabels()});
}
$('#addVoiceBtn').addEventListener('click',()=>addVoiceRow());
for(const event of ['input','change'])$('#voiceRows').addEventListener(event,()=>voiceSaver.dirty());
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
