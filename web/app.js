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

let currentPoseMap=null, kernelState=null, sourceMode='phone', cameraRunning=false, modelAvailable=false, sessionStarted=false, sceneConfigured=false, scenePreparing=false;
const output={enabled:false,mode:'gamepad',strength:160,server:null,xinputEnabled:false,xinputMotionLeft:false,xinputUser:null,xinputStatus:null};
const head={algorithm:'pnp',horizontalAlgorithm:'gesture_v188',deadzone:.10,sensitivityX:58,sensitivityY:46,enabled:true,invertX:false,invertY:false,verticalLookSource:'hand',verticalExclusive:false,bodyMotionGuard:true};
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
  {key:'zone.headJump',group:'zones',id:'headJump',name:'头顶跳跃区'},
  {key:'motion.march',group:'motions',id:'march',name:'原地踏步'},
  {key:'motion.calf_back',group:'motions',id:'calf_back',name:'小腿向后'},
  {key:'motion.squat',group:'motions',id:'squat',name:'下蹲'},
  {key:'motion.hands_up',group:'motions',id:'hands_up',name:'双手举过头'},
  {key:'motion.jumping_jack',group:'motions',id:'jumping_jack',name:'开合跳'},
  {key:'motion.side_step_jack',group:'motions',id:'side_step_jack',name:'侧步开合'},
  {key:'motion.cross_knee_elbow',group:'motions',id:'cross_knee_elbow',name:'提膝碰对侧肘'},
  {key:'pose.hands_cross',group:'poses',id:'hands_cross',name:'双手交叉'},
];
const MOTION_CONFLICT_GROUPS=[
  {ids:['jumping_jack','hands_up'],label:'开合跳与双手过头'},
];
const MOTION_CONFLICT_NAMES={march:'原地踏步',calf_back:'小腿向后',squat:'下蹲',hands_up:'双手过头',jumping_jack:'开合跳',side_step_jack:'侧步开合'};
const ACTION_TYPE_LABELS={keyboard:'键盘',mouse_button:'鼠标按键',mouse_wheel:'鼠标滚轮',gamepad:'Xbox 按键',gamepad_trigger:'Xbox 扳机',gamepad_axis:'Xbox 左摇杆'};
const TARGET_LABELS={LEFT:'左键',RIGHT:'右键',MIDDLE:'中键',X1:'侧键 1',X2:'侧键 2',SCROLL_UP:'向上滚',SCROLL_DOWN:'向下滚',LT:'LT',RT:'RT',L3:'L3',R3:'R3',DPAD_UP:'十字键上',DPAD_DOWN:'十字键下',DPAD_LEFT:'十字键左',DPAD_RIGHT:'十字键右',START:'Start',BACK:'Back',LS_UP:'左摇杆上',LS_DOWN:'左摇杆下',LS_LEFT:'左摇杆左',LS_RIGHT:'左摇杆右'};
// The dispatcher rejects anything outside this set, so offer the list instead
// of a free text field whose typos can only surface as a silent no-op in game.
const GAMEPAD_STICK_TARGETS=['LS_UP','LS_DOWN','LS_LEFT','LS_RIGHT'];
const VOICE_SYSTEM_TARGETS=[['OUTPUT.START','开始输出'],['OUTPUT.STOP','停止输出'],['HEAD.CENTER','视角回正'],['HEAD_CALIBRATION_START','开始校准'],['SCENE.CAPTURE_REFERENCE','记录参考场景'],['SCENE.REMATCH','重新匹配场景']];
const voice={status:null};
let customPoses=[];
let customPoseScores={};
const overlay={win:null,canvas:null,ctx:null};
const perfUi={previewBusy:false};
const scene={status:{},zones:{},vertical:{},selected:''};
let zoneEditMode=false,zoneEditBackup=null,liveZoneDrag=null;
const LEGACY_ZONE_ALIASES={leftHand:['leftHandUpper','leftHandLower'],rightHand:['rightHandUpper','rightHandLower']};
const SCENE_EDIT_ZONE_IDS=Object.keys(BODY_ZONES);
let voiceCatalog=[];

function profileTriggers(){
  // The spare slots stay in the 'voice' group: that name selects the
  // tap/hold/release control and addresses the saved bindings.  Only the
  // display splits them out, through a flag the group filters read.
  const voiceTriggers=voiceCatalog
    .filter(item=>!item.system_fixed)
    .map(item=>({
      key:`voice.${item.id}`,group:'voice',id:item.id,
      slot:String(item.id||'').startsWith('game.profile_slot_'),
      name:`语音 · ${item.phrase}`,tapOnly:false,
      defaultBinding:item.default_action?{label:item.label,action:item.default_action}:null,
    }));
  // 用户自己录的姿势并进同一份触发器列表，于是它们自动出现在映射界面里，
  // 和内置姿势用同一套编辑、保存、按游戏区分的逻辑。不并进来的话，录完的姿势
  // 在界面上根本没地方绑键。
  const customPoseTriggers=customPoses.map(item=>({
    key:`pose.${item.id}`,group:'poses',id:item.id,
    name:`自定义 · ${item.name}`,tapOnly:false,
  }));
  return [...BASE_PROFILE_TRIGGERS,...customPoseTriggers,...voiceTriggers];
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
  // 自定义姿势的相似度跟着主状态一起来，不另开一路轮询。
  customPoseScores=k.custom_pose_scores||{};paintCustomPoseScores();
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
    const horizontalAlgorithm=String(hs.horizontal_algorithm||'gesture_v188');
    head.horizontalAlgorithm=['gesture_v153','frozen22','gesture_v188'].includes(horizontalAlgorithm)?horizontalAlgorithm:'gesture_v188';
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
  const ordered=[...gameProfile.catalog].sort((a,b)=>(b.verified?1:0)-(a.verified?1:0)||String(a.name).localeCompare(String(b.name),'zh'));
  for(const g of ordered){const o=document.createElement('option');o.value=g.id;o.textContent=`${g.verified?'✓ ':''}${g.name}${g.appid?` · ${g.appid}`:''}${g.verified?'':' · 实验'}`;select.appendChild(o)}
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
    // Tick the parts instead of typing "LB+LS_UP": the valid names are a fixed
    // set, and a typo here only surfaces as a rejected save. Stick directions
    // are offered alongside the buttons because a combo may drive both.
    const chosen=new Set(raw.split('+').map(part=>part.trim().toUpperCase()).filter(Boolean));
    const picker=document.createElement('div');picker.className='combo-picker';
    const combo=document.createElement('input');combo.type='hidden';
    const sync=()=>{combo.value=[...picker.querySelectorAll('input:checked')].map(box=>box.value).join('+')};
    for(const key of [...(meta.targets||[]),...GAMEPAD_STICK_TARGETS]){
      const label=document.createElement('label');const box=document.createElement('input');
      box.type='checkbox';box.value=key;box.checked=chosen.has(key);box.addEventListener('change',sync);
      label.append(box,document.createTextNode(TARGET_LABELS[key]||key));picker.appendChild(label);
    }
    sync();
    const update=()=>{const isCombo=select.value==='__combo__';select.className=isCombo?'binding-gamepad-select':'binding-target';combo.className=isCombo?'binding-target':'';picker.hidden=!isCombo};
    select.value=[...select.options].some(o=>o.value===raw)?raw:'__combo__';
    select.addEventListener('change',update);update();container.append(select,picker,combo);return;
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
  for(const[v,t]of (trigger.group==='voice'?[['tap','点按'],['hold','持续按住'],['release','松开同一语音按键']]:[['hold','保持动作时持续'],['tap','进入时触发一次']])){const o=document.createElement('option');o.value=v;o.textContent=t;sel.appendChild(o)}
  // Poses default to a single edge trigger on the server, so show that rather
  // than "hold" while a pose has no binding yet.
  const edgeDefault=trigger.group==='voice'||trigger.group==='poses';
  sel.value=trigger.group==='voice'?(['tap','hold','release'].includes(value)?value:'tap'):(value==='tap'||(!value&&edgeDefault)?'tap':'hold');container.appendChild(sel);
}
function renderProfileBindingRows(){
  const box=$('#profileBindingRows');if(!box)return;box.replaceChildren();
  if(!gameProfile.selected){box.innerHTML='<div class="profile-empty">还没有可编辑的游戏配置。</div>';return}
  const triggers=profileTriggers();
  const groups=[
    {id:'zones',title:'身体区域',help:'手、脚或头部进入对应区域时触发',filter:t=>t.group==='zones',open:true},
    {id:'body',title:'身体动作',help:'识别到动作时触发；开合跳与双手过头顶不能同时映射',filter:t=>t.group==='motions'||t.group==='poses',open:true},
    {id:'voice',title:'语音',help:'说出完整口令后触发一次；系统安全口令不可改',filter:t=>t.group==='voice'&&!t.slot,open:false},
    {id:'voiceSlots',title:'语音 · 备用口令',help:'口令固定但动作随你指派，手机麦克风也认得；下方“自定义口令”可自己起名，但只有电脑麦克风能识别',filter:t=>t.group==='voice'&&t.slot,open:false},
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
      const behavior=document.createElement('div');behavior.className='binding-behavior-box';fillBehaviorControl(behavior,trigger,type.value,action?.behavior||(trigger.group==='voice'?'tap':'hold'));
      type.addEventListener('change',()=>{fillTargetControl(target,type.value,'');fillBehaviorControl(behavior,trigger,type.value,trigger.group==='voice'?'tap':'hold');syncMotionConflictChoices()});
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
// --- hand mouse -----------------------------------------------------------
// The fist thresholds shipped as estimates rather than measurements, so the
// live spread reading sits next to them: open the hand, read the number, close
// it, read again, then put the thresholds between the two.
function renderHandMouse(state){
  if(!state)return;
  const c=state.config||{};
  $('#handMouseEnabled').checked=Boolean(c.enabled);
  $('#handMouseHand').value=c.hand||'right';
  $('#handMouseInvertX').checked=Boolean(c.invert_x);
  for(const [id,value] of [['handMouseSensitivity',c.sensitivity],['handMouseDeadzone',c.deadzone],['handMouseClose',c.fist_close],['handMouseOpen',c.fist_open]]){
    if(value!==undefined)$('#'+id).value=value;
  }
  $('#handMouseSensitivityValue').textContent=Number(c.sensitivity||0).toFixed(0);
  $('#handMouseDeadzoneValue').textContent=Number(c.deadzone||0).toFixed(2);
  $('#handMouseCloseValue').textContent=Number(c.fist_close||0).toFixed(2);
  $('#handMouseOpenValue').textContent=Number(c.fist_open||0).toFixed(2);
  const spread=state.spread==null?'看不到手':Number(state.spread).toFixed(3);
  const label={disabled:'未启用',idle:'待机',open:'手张开',engaged:'已握拳',moving:'握拳移动中',opened:'刚松开',lost:'看不到手'}[state.state]||state.state;
  $('#handMouseStatus').textContent=c.enabled
    ?`${label} · 张开度 ${spread} · 输出 ${Number(state.output_x||0).toFixed(2)} / ${Number(state.output_y||0).toFixed(2)}`
    :'未启用';
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
async function saveHandMouse(){
  const payload={
    enabled:$('#handMouseEnabled').checked,
    hand:$('#handMouseHand').value,
    invert_x:$('#handMouseInvertX').checked,
    sensitivity:Number($('#handMouseSensitivity').value),
    deadzone:Number($('#handMouseDeadzone').value),
    fist_close:Number($('#handMouseClose').value),
    fist_open:Number($('#handMouseOpen').value),
  };
  $('#handMouseSaveStatus').textContent='正在保存…';
  try{
    const data=await post('/api/hand-mouse/config',payload);
    renderHandMouse(data.hand_mouse);
    $('#handMouseSaveStatus').textContent='已保存';
  }catch(error){
    $('#handMouseSaveStatus').textContent=error.message||'保存失败';
  }
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
function confirmFixedZones(){
  const layer=$('#fixedZonesMask');if(!layer)return Promise.resolve(true);
  // Esc closes with the default value, so the switch needs an explicit confirm.
  layer.returnValue='cancel';layer.showModal();
  return new Promise(resolve=>layer.addEventListener('close',()=>resolve(layer.returnValue==='confirm'),{once:true}));
}
async function ensureInitialSceneLayout(){
  if(scenePreparing)return false;
  try{
    const current=await api('/api/scene/status');renderSceneEditor(current);if(current?.configured)return true;
    // Recording a scene permanently leaves the body-relative zones behind, so
    // it must be a deliberate choice rather than a side effect of starting.
    if(!(await confirmFixedZones())){notice('已取消。区域继续跟随身体，随时可以再点“调整区域位置”。');return false}
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
    if(ww<=0||hh<=0){octx.restore();continue}octx.beginPath();if(isGate)octx.roundRect(x,y,ww,hh,Math.max(8,w/70));else if(c)octx.ellipse(x+ww/2,y+hh/2,ww/2,hh/2,0,0,Math.PI*2);else octx.roundRect(x,y,ww,hh,Math.max(6,w/90));octx.fill();octx.stroke();octx.setLineDash([]);octx.fillStyle='#fff';octx.font=`800 ${Math.round(Math.max(11,Math.min(Math.min(ww,hh)*.34,w/9)))}px system-ui,sans-serif`;octx.textAlign='center';octx.textBaseline='middle';octx.fillText(isGate?(active?'上下视角 已开启':'上下视角'):def.label,x+ww/2,y+hh/2);octx.restore();
  }
}
function renderOverlay(map=currentPoseMap){
  if(!overlay.win||overlay.win.closed||!overlay.canvas||!overlay.ctx)return;const c=overlay.canvas,octx=overlay.ctx,w=c.width,h=c.height;octx.setTransform(1,0,0,1,0,0);octx.clearRect(0,0,w,h);octx.fillStyle='#050608';octx.fillRect(0,0,w,h);
  draw(map,octx,w,h,true);
  drawOverlayZones(octx,w,h,kernelState?.zones||{});const buttons=kernelState?.buttons||[],motions=kernelState?.motions||[];const gate=!!kernelState?.vertical_gate_active;const text=gate?'上下视角已开启':(buttons.length?`区域 ${buttons.join('+')}`:(motions.length?`动作 ${motions.join('+')}`:(map?'未触发':'未识别人体')));octx.fillStyle='rgba(0,0,0,.62)';octx.fillRect(0,h-Math.max(25,h/10),w,Math.max(25,h/10));octx.fillStyle='#fff';octx.font=`600 ${Math.max(12,Math.round(w/32))}px system-ui,sans-serif`;octx.textAlign='left';octx.textBaseline='alphabetic';octx.fillText(`${output.enabled?'输出开':'输出关'} · ${text}`,Math.max(7,w/70),h-Math.max(7,h/70))
}

async function toggleOverlay(){if(overlay.win&&!overlay.win.closed){try{overlay.win.close()}catch{}overlay.win=null;overlay.canvas=null;overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗';return}if(!window.documentPictureInPicture?.requestWindow){notice('当前浏览器不支持置顶游戏悬浮窗。');return}try{const pip=await window.documentPictureInPicture.requestWindow({width:420,height:315});pip.document.title='MotionControl';pip.document.body.style.cssText='margin:0;overflow:hidden;background:#050608;width:100vw;height:100vh';const c=pip.document.createElement('canvas');c.width=640;c.height=480;c.style.cssText='display:block;width:100vw;height:100vh;object-fit:contain;background:#050608';pip.document.body.appendChild(c);overlay.win=pip;overlay.canvas=c;overlay.ctx=c.getContext('2d');pip.addEventListener('pagehide',()=>{overlay.win=overlay.canvas=overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗'},{once:true});$('#overlayBtn').textContent='关闭悬浮';renderOverlay(currentPoseMap)}catch(e){notice('悬浮窗启动失败：'+(e?.message||e))}}


function addVoiceRow(mapping={phrase:'',type:'keyboard',target:''}){const row=document.createElement('div');row.className='voice-row';const phrase=document.createElement('input');phrase.className='voice-phrase';phrase.placeholder='说：例如 地图';phrase.value=mapping.phrase||'';const type=document.createElement('select');type.className='voice-type';for(const[value,label]of[['keyboard','键盘/组合键'],['gamepad','Xbox 键'],['system','系统命令']]){const o=document.createElement('option');o.value=value;o.textContent=label;type.appendChild(o)}type.value=mapping.type||'keyboard';const target=document.createElement('span');target.className='voice-target-cell';const fillVoiceTarget=value=>{
  if(type.value==='system'){target.replaceChildren();const sel=document.createElement('select');sel.className='binding-target';for(const[v,t]of VOICE_SYSTEM_TARGETS){const o=document.createElement('option');o.value=v;o.textContent=t;sel.appendChild(o)}if([...sel.options].some(o=>o.value===value))sel.value=value;target.appendChild(sel);return}
  // Voice rows can be built before the action catalog arrives.  Gamepad falls
  // back to its own built-in key list, but keyboard is only free text because
  // the catalog says so, and without it the picker would come out empty.
  if(type.value==='keyboard'&&!gameProfile.actions?.keyboard){const input=document.createElement('input');input.className='binding-target';input.type='text';input.placeholder='例如 W / SPACE / CTRL+W';input.value=value||'';target.replaceChildren(input);return}
  fillTargetControl(target,type.value,value);
};fillVoiceTarget(mapping.target||'');const remove=document.createElement('button');remove.type='button';remove.className='btn voice-remove';remove.textContent='删';remove.addEventListener('click',()=>{row.remove();if(!$('#voiceRows').children.length)addVoiceRow()});const behavior=document.createElement('select');behavior.className='voice-behavior';behavior.setAttribute('aria-label','语音动作方式');for(const[value,label]of [['tap','点按'],['hold','持续按住'],['release','松开']]){const option=document.createElement('option');option.value=value;option.textContent=label;behavior.appendChild(option)}behavior.value=mapping.behavior||'tap';const syncBehavior=()=>{behavior.disabled=type.value==='system';if(behavior.disabled)behavior.value='tap'};type.addEventListener('change',()=>{fillVoiceTarget(target.querySelector('.binding-target')?.value||'');syncBehavior()});syncBehavior();row.append(phrase,type,target,behavior,remove);$('#voiceRows').appendChild(row)}
function readVoiceMappings(){const rows=[...document.querySelectorAll('.voice-row')],items=[],old=new Map((voice.status?.mappings||[]).map(m=>[m.phrase,m]));for(const row of rows){const phrase=row.querySelector('.voice-phrase').value.trim(),type=row.querySelector('.voice-type').value,target=(row.querySelector('.binding-target')?.value||'').trim();if(!phrase&&!target)continue;if(!phrase||!target)throw new Error('语音命令必须同时填写“说什么”和“输出什么”');const behavior=type==='system'?'tap':row.querySelector('.voice-behavior').value;const item={phrase,type,target,behavior},previous=old.get(phrase);if(previous?.synonyms?.length)item.synonyms=[...previous.synonyms];items.push(item)}return items}
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
function voiceActionLabel(action){if(!action)return '当前游戏未启用';if(action.type==='system')return '系统功能 · '+(action.target||'');return `${ACTION_TYPE_LABELS[action.type]||action.type} · ${targetLabel(action)} · ${{tap:'点按',hold:'持续按住',release:'松开'}[action.behavior||'tap']||'点按'}`}
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

function syncControlLabels(){head.algorithm=$('#headAlgorithm').value;const horizontalAlgorithm=$('#headHorizontalAlgorithm')?.value;head.horizontalAlgorithm=['gesture_v153','frozen22','gesture_v188'].includes(horizontalAlgorithm)?horizontalAlgorithm:'gesture_v188';head.verticalLookSource=$('#verticalLookSource')?.value==='head'?'head':'hand';head.verticalExclusive=!!$('#verticalExclusive')?.checked;head.bodyMotionGuard=$('#bodyMotionGuard')?.checked!==false;head.deadzone=Number($('#deadzone').value)/100;head.sensitivityX=Number($('#speedX').value);head.sensitivityY=Number($('#speedY').value);head.enabled=$('#headEnable').checked;head.invertX=$('#invertX').checked;head.invertY=$('#invertY').checked;document.querySelectorAll('.head-vertical-setting').forEach(el=>el.style.setProperty('display',head.verticalLookSource==='head'?'block':'none','important'));$('#deadzoneValue').textContent=Math.round(head.deadzone*100)+'%';$('#speedXValue').textContent=head.sensitivityX+'%';$('#speedYValue').textContent=head.sensitivityY+'%';output.strength=Number($('#strength').value);$('#strengthValue').textContent=output.strength+'%'}
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
  if(view==='devices'){void refreshXinput();void refreshHandMouse();void refreshPoseRecord()}
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
  // 先拿姿势列表再建映射行：触发器列表要包含自定义姿势，否则录过的姿势
  // 在映射界面里没有对应的一行。
  await refreshCustomPoses({rebuild:false});
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
for(const id of ['handMouseEnabled','handMouseHand','handMouseInvertX','handMouseSensitivity','handMouseDeadzone','handMouseClose','handMouseOpen']){
  const el=$('#'+id);
  if(el)el.addEventListener('change',()=>void saveHandMouse());
}
bind('poseRecordBtn',startPoseRecord);
bind('poseRecordCancelBtn',cancelPoseRecord);
bind('profileSearchBtn',searchProfiles);
$('#profileSearch').addEventListener('keydown',e=>{if(e.key==='Enter')void runAction(searchProfiles)});
bind('profileApplyBtn',applySelectedProfile);bind('resetProfileBindingsBtn',resetProfileBindings);
bind('retryProfileSaveBtn',retryProfileBindings);
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
$('#fixedZonesConfirm').addEventListener('click',()=>$('#fixedZonesMask').close('confirm'));
$('#fixedZonesCancel').addEventListener('click',()=>$('#fixedZonesMask').close('cancel'));
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

/** 在系统浏览器里打开网站上的某一页。详情、浏览全部、上传都在那边。 */
function openOnSite(path = '') {
  if (!cloudEndpoint) { cloudSay('还没连上云端', 'error'); return; }
  window.open(cloudEndpoint + path, '_blank', 'noopener');
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
        renderVoiceRows(voice.status?.mappings || []);
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
    renderCustomPoses();
    // 触发器列表变了，映射界面要重建才能看到新姿势。轮询刷新分数时不重建，
    // 否则用户正在编辑的那一行会被冲掉。
    if (rebuild) renderProfileBindingRows();
  } catch (error) {
    customPoseSay(error.message, 'error');
  }
}

/** 倒计时期间可以取消——按错了不用等它数完。 */
let customPoseCountdown = null;

/**
 * 倒数几秒再执行。人要从电脑前走到镜头前摆好姿势，点完立刻拍等于拍到一个走路的
 * 背影。数字在按钮上放大显示：这时候人站在几米外，小字看不见。
 *
 * 返回 false 表示被取消了。
 */
async function withCountdown(button, label, action) {
  if (customPoseCountdown) {  // 再点一次 = 取消
    clearTimeout(customPoseCountdown);
    customPoseCountdown = null;
    document.querySelectorAll('.counting').forEach(el => {
      el.classList.remove('counting');
      el.textContent = el.dataset.label || el.textContent;
    });
    customPoseSay('已取消');
    return false;
  }

  const seconds = Number(document.getElementById('customPoseDelay')?.value || 3);
  button.dataset.label = label;
  button.classList.add('counting');

  const finished = await new Promise(resolve => {
    let left = seconds;
    const tick = () => {
      if (!button.classList.contains('counting')) { resolve(false); return; }
      if (left <= 0) { customPoseCountdown = null; resolve(true); return; }
      button.textContent = String(left);
      customPoseSay(`${left} 秒后拍下当前姿势，摆好别动（再点一次取消）`);
      left -= 1;
      customPoseCountdown = setTimeout(tick, 1000);
    };
    tick();
  });

  button.classList.remove('counting');
  button.textContent = label;
  if (!finished) return false;

  button.disabled = true;
  customPoseSay('正在读取当前姿势…');
  try {
    await action();
  } catch (error) {
    customPoseSay(error.message, 'error');
  } finally {
    button.disabled = false;
  }
  return true;
}

function applyPoses(data) {
  customPoses = data.poses || [];
  renderCustomPoses();
  renderProfileBindingRows();
}

async function captureCustomPose() {
  const button = document.getElementById('customPoseCaptureBtn');
  const nameInput = document.getElementById('customPoseName');
  await withCountdown(button, '录下当前姿势', async () => {
    const data = await post('/api/pose/custom/capture', { name: nameInput.value || '' });
    nameInput.value = '';
    applyPoses(data);
    customPoseSay('已录「' + data.pose.name + '」。下面是拍到的骨架，不对就删掉重录。'
      + '想做成连续动作，摆好下一个姿势再点「再加一个姿势」。');
  });
}

async function appendCustomPoseFrame(item, button) {
  await withCountdown(button, '再加一个姿势', async () => {
    const data = await post('/api/pose/custom/frame', { id: item.id });
    applyPoses(data);
    customPoseSay(`「${data.pose.name}」现在有 ${data.pose.frames} 个姿势，`
      + '要按顺序依次做出来才会触发。');
  });
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
    addFrame.title = '摆好下一个姿势再点。做完的动作要按顺序依次做出来才触发';
    addFrame.addEventListener('click', () => appendCustomPoseFrame(item, addFrame));
    strip.appendChild(addFrame);

    const head = document.createElement('div');
    head.className = 'custom-pose-head';
    head.append(name, meter);

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

document.getElementById('cloudSiteBtn')?.addEventListener('click', () => openOnSite('/'));
