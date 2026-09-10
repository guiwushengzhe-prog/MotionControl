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
let profileAutoSaveTimer=null;
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
const ACTION_TYPE_LABELS={keyboard:'键盘',mouse_button:'鼠标按键',mouse_wheel:'鼠标滚轮',gamepad:'Xbox 按键',gamepad_trigger:'Xbox 扳机',gamepad_axis:'Xbox 左摇杆'};
const TARGET_LABELS={LEFT:'左键',RIGHT:'右键',MIDDLE:'中键',X1:'侧键 1',X2:'侧键 2',SCROLL_UP:'向上滚',SCROLL_DOWN:'向下滚',LT:'LT',RT:'RT',L3:'L3',R3:'R3',DPAD_UP:'十字键上',DPAD_DOWN:'十字键下',DPAD_LEFT:'十字键左',DPAD_RIGHT:'十字键右',START:'Start',BACK:'Back',LS_UP:'左摇杆上',LS_DOWN:'左摇杆下',LS_LEFT:'左摇杆左',LS_RIGHT:'左摇杆右'};
const voice={status:null};
const overlay={win:null,canvas:null,ctx:null};
const perfUi={latest:null,renderTimes:[],previewBusy:false,previewTimer:null};
const scene={status:{},zones:{},vertical:{},selected:'',drag:null};
let zoneEditMode=false,zoneEditBackup=null,liveZoneDrag=null;
const SCENE_LABELS={lookGate:'下巴左侧 · 左腕视角门',leftHand:'左手触发区 · X',rightHand:'右手触发区 · B',leftFoot:'左脚侧抬区 · LB',rightFoot:'右脚侧抬区 · RB',headJump:'头顶跳跃区 · A',leftHandUpper:'旧左手上区',leftHandLower:'旧左手下区',rightHandUpper:'旧右手上区',rightHandLower:'旧右手下区'};
const LEGACY_ZONE_ALIASES={leftHand:['leftHandUpper','leftHandLower'],rightHand:['rightHandUpper','rightHandLower']};
const SCENE_EDIT_ZONE_IDS=['leftHand','rightHand','leftFoot','rightFoot','headJump','lookGate'];
const COMMON_VOICE_IDS=['output.start','output.stop','scene.capture','head.calibrate','scene.rematch','head.center'];
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
function notice(t){$('#notice').textContent=t;$('#notice').style.display=t?'block':'none'}
async function api(path,opt){const r=await fetch(path,opt);if(!r.ok)throw new Error(`${r.status} ${await r.text()}`);return r.json()}
async function post(path,data){return api(path,{method:'POST',headers:{'Content-Type':'application/json; charset=utf-8'},body:JSON.stringify(data)})}

function visualPoint(p){return{x:1-p.x,y:p.y}}
function draw(map){
  ctx.setTransform(1,0,0,1,0,0);ctx.clearRect(0,0,canvas.width,canvas.height);if(!map)return;
  ctx.strokeStyle='#55ddff';ctx.fillStyle='#fff';ctx.lineWidth=3;
  for(const[a,b]of EDGES){const p=map[a],q=map[b];if(!p||!q||p.score<.3||q.score<.3)continue;ctx.beginPath();ctx.moveTo(p.x*canvas.width,p.y*canvas.height);ctx.lineTo(q.x*canvas.width,q.y*canvas.height);ctx.stroke()}
  for(const p of Object.values(map)){if(p.score<.3)continue;ctx.beginPath();ctx.arc(p.x*canvas.width,p.y*canvas.height,3,0,Math.PI*2);ctx.fill()}
}
function renderKernelZones(zones={}){
  for(const[id,def]of Object.entries(BODY_ZONES)){
    const el=document.querySelector(`.zone[data-zone="${id}"]`),state=zones[id];if(!el)continue;
    const active=!zoneEditMode&&!!state?.pressed;
    el.classList.toggle('active',active);
    const editCircle=zoneEditMode?scene.zones?.[id]:null;
    const circle=editCircle||state?.circle;
    el.classList.toggle('circle-shape',!!circle&&!def.gate);
    el.innerHTML=def.gate
      ?`<strong>上下视角</strong><small>${active?'已开启':'左手放这里'}</small>`
      :`<strong>${def.label}</strong><small>${def.body}</small>`;
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
  if(!active){layer.hidden=true;return}
  layer.hidden=false;
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
  const el=$('#mainActionStatus');if(!el)return;
  if(!sessionStarted){el.textContent='先启动识别；确认人体和区域正常后，再开始游戏控制';return}
  if(!currentPoseMap){el.textContent='正在找玩家 · 请让全身进入画面';return}
  if(!sceneConfigured){el.textContent=output.enabled?'正在控制游戏 · 体感区域尚未完成定位，头控、动作、语音和手机输入仍可用':'识别已就绪 · 体感区域尚未完成定位，头控、动作、语音和手机输入仍可用';return}
  el.textContent=output.enabled?'正在控制游戏 · F9 可随时紧急停止':'识别已就绪 · 可以开始游戏控制';
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
  if(hs.algorithm){
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
  const camera=runtime?.camera||{};cameraRunning=!!camera.running;if(sourceMode==='computer')sessionStarted=cameraRunning;$('#poseSource').value=sourceMode;$('#cameraBtn').disabled=sourceMode==='phone';$('#cameraBtn').textContent=sourceMode==='phone'?'手机姿态由本地服务接收':(cameraRunning?'停止本地摄像头':'启动本地摄像头');
  if(cameraPreview){
    const showPreview=sourceMode==='computer'&&cameraRunning;
    cameraPreview.style.display=showPreview?'block':'none';
    if(!showPreview) cameraPreview.removeAttribute('src');
  }
  $('#cameraPill').textContent=(sourceMode==='phone'||cameraRunning)?'摄像头 ✓':'摄像头';$('#cameraPill').className='pill '+(sourceMode==='phone'||cameraRunning?'ok':'bad');
  // phonePill is owned by renderInputStatus (/api/input/status); kernel status has no transport state.
  $('#posePill').textContent=currentPoseMap?'人体 ✓':'人体';$('#posePill').className='pill '+(currentPoseMap?'ok':'bad');const gateActive=!!k.vertical_gate_active;const verticalSource=String(hs.verticalLookSource||hs.vertical_look_source||k.vertical_look?.source||'hand')==='head'?'头部':'右手';const gateStatus=$('#lookGateStatus');if(gateStatus){const paused=!!hs.horizontal_paused_by_vertical_gate;gateStatus.textContent=gateActive?`上下视角已开启 · ${verticalSource}控制上下${paused?' · 左右暂停':''}`:'上下视角待机 · 左手放入绿色区域开启';gateStatus.className='look-gate-status '+(gateActive?'active':'')}renderOverlay(currentPoseMap);renderMainStatus();
  const main=$('#mainActionBtn');if(main){main.textContent=!sessionStarted?'开始体感':(output.enabled?'停止游戏控制':'开始游戏控制');main.className=`btn ${output.enabled?'danger':'primary'} main-action-btn`}
}
function renderInputStatus(status){const connected=!!(status?.mobile_pose_connected||status?.handheld_connected),pill=$('#mobileStatus');pill.textContent=connected?'手机已连接':'手机未连接';pill.className='pill '+(connected?'ok':'bad');const top=$('#phonePill');if(top){top.textContent=connected?'手机 ✓':'手机';top.className='pill '+(connected?'ok':'optional')}const field=$('#phoneWsUrl');if(field)field.value=status?.phone_ws_urls?.[0]||'连接服务器后显示'}
async function refreshKernel(){try{renderKernelState(await api('/api/kernel/status'))}catch{}}
async function refreshInput(){try{renderInputStatus(await api('/api/input/status?brief=1'))}catch{}}

function bindingFor(trigger){
  const items=gameProfile.selected?.bindings?.[trigger.group]||{};
  if(Object.prototype.hasOwnProperty.call(items,trigger.id))return items[trigger.id];
  if(trigger.group==='zones'){
    for(const alias of LEGACY_ZONE_ALIASES[trigger.id]||[]){if(Object.prototype.hasOwnProperty.call(items,alias))return items[alias]}
  }
  return trigger.defaultBinding||null;
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
  const sourceName=source.kind==='manual'?'人工':(source.kind==='steaminputdb'?'SteamInputDB':(source.kind==='builtin'?'内置':'离线库'));
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
  const id=$('#profileSelect').value;if(!id)return;
  const data=await post('/api/game-profiles/select',{id});
  gameProfile.selected=data.profile;gameProfile.overrides={};await refreshVoiceCommands();renderProfileHeader();renderProfileBindingRows();
  notice(`已切换游戏：${gameProfile.selected?.name||id}`);
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
  if(!gameProfile.selected){box.innerHTML='<div class="profile-empty">还没有可编辑的游戏 Profile。</div>';return}
  const triggers=profileTriggers();
  const groups=[
    {id:'zones',title:'Zone 圈',help:'手或脚进入固定圈时触发',filter:t=>t.group==='zones',open:true},
    {id:'body',title:'身体动作',help:'识别到动作时触发；左右腿交叉已移除',filter:t=>t.group==='motions'||t.group==='poses',open:true},
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
      type.addEventListener('change',()=>{fillTargetControl(target,type.value,'');fillBehaviorControl(behavior,trigger,type.value,'hold')});
      row.append(name,type,target,behavior);rows.appendChild(row);
    }
    details.append(summary,help,rows);box.appendChild(details);
  }
}
function readProfileOverrides(){
  const overrides={};
  for(const trigger of profileTriggers()){
    const row=document.querySelector(`.binding-row[data-trigger="${trigger.key}"]`);if(!row)continue;
    const type=row.querySelector('.binding-type')?.value||'';
    if(!type){overrides[trigger.key]=null;continue}
    const target=String(row.querySelector('.binding-target')?.value||'').trim().toUpperCase();
    if(!target)throw new Error(`${trigger.name} 还没有选择具体键位`);
    const behavior=trigger.tapOnly||type==='mouse_wheel'?'tap':(row.querySelector('select.binding-behavior')?.value||'hold');
    overrides[trigger.key]={action:{type,target,behavior}};
  }
  return overrides;
}
async function saveProfileBindings(){
  if(profileAutoSaveTimer){clearTimeout(profileAutoSaveTimer);profileAutoSaveTimer=null}
  const data=await post('/api/game-profiles/overrides',{overrides:readProfileOverrides()});
  gameProfile.selected=data.profile;gameProfile.overrides=gameProfile.selected?.overrides||{};await refreshVoiceCommands();renderProfileHeader();renderProfileBindingRows();
  notice('当前游戏的体感映射已保存。');
}
function scheduleProfileAutoSave(){
  if(profileAutoSaveTimer)clearTimeout(profileAutoSaveTimer);
  const button=$('#saveProfileBindingsBtn');if(button)button.textContent='正在自动保存…';
  profileAutoSaveTimer=setTimeout(async()=>{
    profileAutoSaveTimer=null;
    try{
      const data=await post('/api/game-profiles/overrides',{overrides:readProfileOverrides()});
      gameProfile.selected=data.profile;gameProfile.overrides=gameProfile.selected?.overrides||{};
      await refreshVoiceCommands();renderProfileHeader();
      if(button)button.textContent='映射已自动保存';
      notice('映射已自动保存并立即生效。');
    }catch(e){
      if(button)button.textContent='保存当前游戏映射';
      notice('自动保存失败：'+(e?.message||e));
    }
  },350);
}
async function resetProfileBindings(){
  const data=await post('/api/game-profiles/overrides',{overrides:{}});
  gameProfile.selected=data.profile;gameProfile.overrides={};await refreshVoiceCommands();renderProfileHeader();renderProfileBindingRows();
  notice('已恢复这个游戏的内置默认映射。');
}

function formatPerf(value,suffix=''){return value===null||value===undefined||value===''?'—':`${value}${suffix}`}
function renderPerformance(data){
  if(!data)return;
  perfUi.latest=data;
  const summary=`FPS ${formatPerf(data.capture_fps)} / 推理 ${formatPerf(data.inference_fps)} · 延迟 ${formatPerf(data.total_latency_ms,' ms')} · 人体 ${formatPerf(data.recent_humans)}`;
  $('#perfSummary').textContent=summary;
  const resolution=data.camera_resolution||{};
  const resolutionText=typeof resolution==='object'?`${resolution.width||0}×${resolution.height||0}`:String(resolution);
  const lines=[
    `来源：${data.source||'—'} · 模型：${data.model||'—'}`,
    `分辨率：${resolutionText} · 后端：${data.backend_name||data.backend||'—'} · 请求：${formatPerf(data.requested_fps)} FPS`,
    `采集 FPS：${formatPerf(data.capture_fps)}（实际：${formatPerf(data.actual_capture_fps)}） · 推理 FPS：${formatPerf(data.inference_fps)}`,
    `推理平均/P95：${formatPerf(data.inference_avg_ms,' ms')} / ${formatPerf(data.inference_p95_ms,' ms')}`,
    `姿态年龄/总延迟：${formatPerf(data.pose_frame_age_ms,' ms')} / ${formatPerf(data.total_latency_ms,' ms')}`,
    `网络 FPS/年龄：${formatPerf(data.network_fps)} / ${formatPerf(data.network_age_ms,' ms')}`,
    `网页渲染 FPS：${formatPerf(perfUi.renderTimes.length?measureRenderFps() : null)} · 跳帧：${data.dropped_frames??0} / 跳过：${data.skipped_frames??0}`,
    `最近人体数：${formatPerf(data.recent_humans)} · 预览：${data.preview_ready?'已就绪':'—'}（${formatPerf(data.preview_fps)} FPS，编码 ${formatPerf(data.preview_encode_avg_ms,' ms')} / P95 ${formatPerf(data.preview_encode_p95_ms,' ms')}）`,
  ];
  $('#perfDetails').textContent=lines.join('\n');
}
function measureRenderFps(){
  const now=performance.now();perfUi.renderTimes=perfUi.renderTimes.filter(t=>now-t<2000);
  if(perfUi.renderTimes.length<2)return null;
  return Math.round((perfUi.renderTimes.length-1)/((perfUi.renderTimes.at(-1)-perfUi.renderTimes[0])/1000));
}
async function refreshPerformance(){try{renderPerformance(await api('/api/performance'))}catch{}}
async function refreshCameraConfig(){try{const data=await api('/api/camera/config');const select=$('#cameraBackend');if(select&&data.preference)select.value=data.preference}catch{}}
async function refreshPreview(){
  if(!cameraPreview||perfUi.previewBusy||sourceMode!=='computer'||!cameraRunning||document.visibilityState!=='visible')return;
  perfUi.previewBusy=true;
  try{
    const response=await fetch(`/api/camera/preview.jpg?t=${Date.now()}`,{cache:'no-store'});
    if(!response.ok)return;
    const blob=await response.blob(),url=URL.createObjectURL(blob),old=cameraPreview.src;
    cameraPreview.onload=()=>{perfUi.renderTimes.push(performance.now());if(old?.startsWith('blob:'))URL.revokeObjectURL(old)};
    cameraPreview.src=url;
  }catch{}
  finally{perfUi.previewBusy=false}
}

function outputPayload(enabled=output.enabled){const gain=clamp(output.strength,60,300)/100;return{mode:output.mode,enabled,mouse_speed_x:600*gain,mouse_speed_y:450*gain,gamepad_gain:gain,xinput_merge_enabled:output.xinputEnabled,xinput_motion_left_enabled:output.xinputMotionLeft,physical_xinput_user:output.xinputUser}}
function renderXinputStatus(s=output.xinputStatus){const select=$('#xinputMerge'),line=$('#xinputStatus');if(!select||!line)return;const users=Array.isArray(s?.connected_users)?s.connected_users:[];const current=s?.enabled&&s?.selected_user!==null&&s?.selected_user!==undefined?String(s.selected_user):'';const values=[['','关闭体感合流']];for(const user of users)values.push([String(user),`物理手柄 ${Number(user)+1}`]);if(current&&!values.some(([v])=>v===current))values.push([current,`手柄 ${Number(current)+1}（未连接）`]);const keep=current&&values.some(([v])=>v===current);select.replaceChildren(...values.map(([value,label])=>{const o=document.createElement('option');o.value=value;o.textContent=label;return o}));select.value=keep?current:(s?.enabled?'':'');output.xinputEnabled=!!s?.enabled;output.xinputUser=s?.selected_user??null;output.xinputMotionLeft=!!s?.motion_left_enabled;renderXinputMotionLeft();line.textContent=!s?.enabled?'未启用物理手柄合流':(s?.connected?`物理手柄已合流 · 手柄 ${Number(s.active_user??s.selected_user)+1}`:'已启用，等待物理手柄连接');if(s?.last_error)line.textContent+=' · '+s.last_error;line.className='statusline '+(s?.connected?'ok':'')}
function renderXinputMotionLeft(){const box=$('#xinputMotionLeft');if(box){box.checked=output.xinputMotionLeft;box.disabled=!output.xinputEnabled||output.mode!=='gamepad'}}
async function setXinputMotionLeft(){try{output.server=await post('/api/output/xinput',{motion_left_enabled:!!$('#xinputMotionLeft')?.checked});renderOutput(output.server);await refreshXinput();notice(output.xinputMotionLeft?'体感左摇杆合成已开启，双方输入相加':'已关闭体感左摇杆合成')}catch(e){notice('左摇杆合成设置失败：'+(e?.message||e));await refreshXinput()}}
async function refreshXinput(){try{output.xinputStatus=await api('/api/output/xinput');renderXinputStatus(output.xinputStatus)}catch{}}
async function setXinputMerge(){const select=$('#xinputMerge');const value=select?.value||'';try{const data=await post('/api/output/xinput',{enabled:!!value,user:value===''?null:Number(value)});output.server=data;renderOutput(data);await refreshXinput();notice(value?`已选择物理手柄 ${Number(value)+1}；${output.xinputMotionLeft?'体感按键与左摇杆合成已开启':'体感只叠加手柄按键'}`:'已关闭物理手柄合流')}catch(e){notice('物理手柄合流失败：'+(e?.message||e));await refreshXinput()}}
function renderOutput(s=output.server){const on=!!(s?.enabled??output.enabled);output.enabled=on;output.mode=s?.mode||output.mode;$('#outputMode').value=output.mode;$('#outputPill').textContent=on?'输出 ✓':'输出';$('#outputPill').className='pill '+(on?'ok':'');$('#outputBtn').textContent=on?'关闭输出 F8':'开启输出 F8';const main=$('#mainActionBtn');if(main){main.textContent=!sessionStarted?'开始体感':(on?'停止游戏控制':'开始游戏控制');main.className=`btn ${on?'danger':'primary'} main-action-btn`}renderMainStatus();if(!s){$('#backendStatus').textContent='正在检查输出后端…';return}$('#backendStatus').textContent=`${s.mouse_available?'鼠标可用':'鼠标不可用'} · ${s.gamepad_connected?'Xbox 已连接':'Xbox 未连接'}`+(s.xinput_merge_active?(s.xinput_connected?' · 物理手柄已合流':' · 等待物理手柄'):'')+(s.last_error?' · '+s.last_error:'');if(s.xinput_merge_enabled!==undefined){output.xinputEnabled=!!s.xinput_merge_enabled;output.xinputUser=s.xinput_selected_user??null;output.xinputMotionLeft=!!s.xinput_motion_left_enabled;renderXinputMotionLeft()}}
async function refreshOutput(){try{output.server=await api('/api/output-status');renderOutput(output.server)}catch{}}
async function setOutput(enabled){try{output.server=await post('/api/output/config',outputPayload(enabled));renderOutput(output.server)}catch(e){notice('输出开启失败：'+(e?.message||e));renderOutput()}}
async function emergencyStop(show=true){try{output.server=await post('/api/output/stop',{})}catch{}output.enabled=false;renderOutput(output.server);if(show)notice(output.xinputEnabled?'体感已停止，物理手柄继续透传。':'本地服务已停止所有输出。')}

async function setSource(source,enabled=true){try{const result=await post('/api/input/source',{source,enabled});sourceMode=source;sessionStarted=!!enabled;renderKernelState(result);await refreshInput();notice('输入源已切换：'+(source==='phone'?'手机摄像头':'电脑摄像头'))}catch(e){notice('输入源切换失败：'+(e?.message||e));await refreshKernel()}}
async function toggleLocalCamera(){if(sourceMode==='phone')return;try{const enabled=!cameraRunning;const result=await post('/api/input/source',{source:'computer',enabled});sessionStarted=enabled;renderKernelState(result)}catch(e){notice('本地摄像头操作失败：'+(e?.message||e));await refreshKernel()}}
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
async function handleMainAction(){try{
  if(!sessionStarted){await setSource(sourceMode||'computer',true);await ensureInitialSceneLayout();notice(sceneConfigured?'体感识别已开始，6 个区域已就绪；游戏输出仍关闭。':'体感识别已开始；游戏输出仍关闭。');return}
  await setOutput(!output.enabled)
}catch(e){notice('主操作失败：'+(e?.message||e))}}
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
  if(map){octx.strokeStyle='rgba(80,220,255,.92)';octx.lineWidth=Math.max(2,w/260);octx.fillStyle='rgba(255,255,255,.96)';for(const[a,b]of EDGES){const p=map[a],q=map[b];if(!p||!q||p.score<.3||q.score<.3)continue;const vp=visualPoint(p),vq=visualPoint(q);octx.beginPath();octx.moveTo(vp.x*w,vp.y*h);octx.lineTo(vq.x*w,vq.y*h);octx.stroke()}for(const p of Object.values(map)){if(p.score<.3)continue;const vp=visualPoint(p);octx.beginPath();octx.arc(vp.x*w,vp.y*h,Math.max(2.2,w/190),0,Math.PI*2);octx.fill()}}
  drawOverlayZones(octx,w,h,kernelState?.zones||{});const buttons=kernelState?.buttons||[],motions=kernelState?.motions||[];const gate=!!kernelState?.vertical_gate_active;const text=gate?'上下视角已开启':(buttons.length?`区域 ${buttons.join('+')}`:(motions.length?`动作 ${motions.join('+')}`:(map?'未触发':'未识别人体')));octx.fillStyle='rgba(0,0,0,.62)';octx.fillRect(0,h-Math.max(25,h/10),w,Math.max(25,h/10));octx.fillStyle='#fff';octx.font=`600 ${Math.max(12,Math.round(w/32))}px system-ui,sans-serif`;octx.textAlign='left';octx.textBaseline='alphabetic';octx.fillText(`${output.enabled?'输出开':'输出关'} · ${text}`,Math.max(7,w/70),h-Math.max(7,h/70))
}

async function toggleOverlay(){if(overlay.win&&!overlay.win.closed){try{overlay.win.close()}catch{}overlay.win=null;overlay.canvas=null;overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗';return}if(!window.documentPictureInPicture?.requestWindow){notice('当前浏览器不支持置顶游戏悬浮窗。');return}try{const pip=await window.documentPictureInPicture.requestWindow({width:420,height:315});pip.document.title='MotionControl';pip.document.body.style.cssText='margin:0;overflow:hidden;background:#050608;width:100vw;height:100vh';const c=pip.document.createElement('canvas');c.width=640;c.height=480;c.style.cssText='display:block;width:100vw;height:100vh;object-fit:contain;background:#050608';pip.document.body.appendChild(c);overlay.win=pip;overlay.canvas=c;overlay.ctx=c.getContext('2d');pip.addEventListener('pagehide',()=>{overlay.win=overlay.canvas=overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗'},{once:true});$('#overlayBtn').textContent='关闭悬浮';renderOverlay(currentPoseMap)}catch(e){notice('悬浮窗启动失败：'+(e?.message||e))}}


function addVoiceRow(mapping={phrase:'',type:'keyboard',target:''}){const row=document.createElement('div');row.className='voice-row';const phrase=document.createElement('input');phrase.className='voice-phrase';phrase.placeholder='说：例如 地图';phrase.value=mapping.phrase||'';const type=document.createElement('select');type.className='voice-type';for(const[value,label]of[['keyboard','键盘/组合键'],['gamepad','Xbox 键'],['system','系统命令']]){const o=document.createElement('option');o.value=value;o.textContent=label;type.appendChild(o)}type.value=mapping.type||'keyboard';const target=document.createElement('input');target.className='voice-target';target.value=mapping.target||'';const remove=document.createElement('button');remove.type='button';remove.className='btn voice-remove';remove.textContent='删';remove.addEventListener('click',()=>{row.remove();if(!$('#voiceRows').children.length)addVoiceRow()});row.append(phrase,type,target,remove);$('#voiceRows').appendChild(row)}
function readVoiceMappings(){const rows=[...document.querySelectorAll('.voice-row')],items=[],old=new Map((voice.status?.mappings||[]).map(m=>[m.phrase,m]));for(const row of rows){const phrase=row.querySelector('.voice-phrase').value.trim(),type=row.querySelector('.voice-type').value,target=row.querySelector('.voice-target').value.trim();if(!phrase&&!target)continue;if(!phrase||!target)throw new Error('语音命令必须同时填写“说什么”和“输出什么”');const item={phrase,type,target},previous=old.get(phrase);if(previous?.synonyms?.length)item.synonyms=[...previous.synonyms];items.push(item)}return items}
function renderVoiceRows(items){$('#voiceRows').replaceChildren();for(const m of items||[])addVoiceRow(m);if(!$('#voiceRows').children.length)addVoiceRow()}
function renderVoiceStatus(s=voice.status){
  if(!s)return;voice.status=s;const has=!!s.model_ready,connected=!!s.connected;const isSingleKws=String(s.recognizer_mode||'').includes('single_stage')||String(s.recognizer_mode||'').includes('kws');
  $('#voiceMode').textContent=has?(isSingleKws?`短语识别 · ${s.supported_count||0} 条`:`语音 · ${s.supported_count||0} 条`):'未就绪';$('#voiceMode').className='pill '+(has?'ok':'warn');
  const pcOk=connected&&s.source_kind==='computer'&&s.available&&s.model_ready&&s.audio_ready&&(s.audio_alive||s.stream_alive);const phoneOk=connected&&s.source_kind!=='computer';const ready=pcOk||phoneOk;
  $('#voicePill').textContent=ready?'语音 ✓':(connected?'语音准备中':'语音');$('#voicePill').className='pill '+(ready?'ok':(connected?'warn':'optional'));
  $('#voiceBtn').textContent='语音由本地服务接收';$('#voiceBtn').disabled=true;
  const phrase=String(s.last_command||s.final||'').trim();$('#voiceStatus').textContent=phrase?`已识别：${phrase}`:(ready?'直接说完整口令，例如“体感截图”':'语音尚未准备好');
  const modelPath=s.model_path||s.command_model_path||'—';const mp=$('#voiceModelPath');if(mp){mp.textContent='模型：'+modelPath;mp.title=modelPath}
  const diag=$('#voiceDiagnostic');if(diag){diag.textContent=[`模式：${s.recognizer_mode||'—'}`,`词条：${s.supported_count??'—'}`,`模型：${modelPath}`,`音频：${s.audio_ready?'ready':'not ready'} / ${s.audio_alive||s.stream_alive?'alive':'idle'}`,`RMS：${Number(s.rms||0).toFixed(0)} · bytes：${s.bytes_received||0}`,`最后命令：${phrase||'—'}`,`错误：${s.last_error||'—'}`].join('\n')}
}

async function saveVoiceMappings(){const s=await post('/api/voice/config',{mappings:readVoiceMappings()});voice.status=s;renderVoiceStatus(s);return s}
async function refreshVoice(){try{voice.status=await api('/api/voice/status');renderVoiceStatus(voice.status)}catch{}}
function voiceActionLabel(action){if(!action)return '当前游戏未启用';if(action.type==='system')return '系统功能 · '+(action.target||'');return `${ACTION_TYPE_LABELS[action.type]||action.type} · ${targetLabel(action)}`}
function renderVoiceCommandCard(command){const card=document.createElement('div');card.className='voice-command-card';card.setAttribute('role','listitem');const phrase=document.createElement('div');phrase.textContent=command.phrase||'';const label=document.createElement('small');label.textContent=command.system_fixed?`${command.label||''} · 系统固定`:`${command.label||''} · ${voiceActionLabel(command.effective_action)}`;card.append(phrase,label);return card}
function renderVoiceCommandCatalog(commands){voiceCatalog=Array.isArray(commands)?commands:[];const countBtn=$('#voiceCommandsBtn');if(countBtn)countBtn.textContent=`查看全部 ${voiceCatalog.length} 条语音指令`;const common=$('#commonVoiceCommands'),full=$('#voiceCommandGrid');if(!common||!full)return;common.replaceChildren();full.replaceChildren();const byId=new Map(voiceCatalog.map(item=>[item.id,item]));for(const id of COMMON_VOICE_IDS){const item=byId.get(id);if(item)common.appendChild(renderVoiceCommandCard(item))}const isProfile=c=>String(c.id||'').startsWith('game.profile_slot_');const groups=[['系统与体感控制',c=>c.kind==='system'],['常规游戏操作',c=>String(c.id||'').startsWith('game.')&&!isProfile(c)],['当前游戏补充功能',c=>isProfile(c)],['菜单操作',c=>!String(c.id||'').startsWith('game.')&&c.kind!=='system']];for(const[name,filter]of groups){const items=voiceCatalog.filter(filter);if(!items.length)continue;const section=document.createElement('section');section.className='voice-group';const h=document.createElement('h3');h.textContent=name;const grid=document.createElement('div');grid.className='voice-command-grid full';for(const item of items)grid.appendChild(renderVoiceCommandCard(item));section.append(h,grid);full.appendChild(section)}}
async function refreshVoiceCommands(){try{const data=await api('/api/voice/commands');renderVoiceCommandCatalog(data.commands||[])}catch{renderVoiceCommandCatalog([])}}

function syncControlLabels(){head.algorithm=$('#headAlgorithm').value;const horizontalAlgorithm=$('#headHorizontalAlgorithm')?.value;head.horizontalAlgorithm=['classic','gesture_v153','frozen22','gesture_v188'].includes(horizontalAlgorithm)?horizontalAlgorithm:'classic';head.verticalLookSource=$('#verticalLookSource')?.value==='head'?'head':'hand';head.verticalExclusive=!!$('#verticalExclusive')?.checked;head.bodyMotionGuard=$('#bodyMotionGuard')?.checked!==false;head.deadzone=Number($('#deadzone').value)/100;head.sensitivityX=Number($('#speedX').value);head.sensitivityY=Number($('#speedY').value);head.enabled=$('#headEnable').checked;head.invertX=$('#invertX').checked;head.invertY=$('#invertY').checked;document.querySelectorAll('.head-vertical-setting').forEach(el=>el.style.setProperty('display',head.verticalLookSource==='head'?'block':'none','important'));$('#deadzoneValue').textContent=Math.round(head.deadzone*100)+'%';$('#speedXValue').textContent=head.sensitivityX+'%';$('#speedYValue').textContent=head.sensitivityY+'%';output.strength=Number($('#strength').value);$('#strengthValue').textContent=output.strength+'%'}
async function pushHeadConfig(){const previousHorizontalAlgorithm=head.horizontalAlgorithm;syncControlLabels();try{renderKernelState(await post('/api/head/config',{algorithm:head.algorithm,horizontal_algorithm:head.horizontalAlgorithm,deadzone:head.deadzone,sensitivity_x:head.sensitivityX,sensitivity_y:head.sensitivityY,enabled:head.enabled,invert_x:head.invertX,invert_y:head.invertY,vertical_look_source:head.verticalLookSource,vertical_exclusive:head.verticalExclusive,body_motion_guard:head.bodyMotionGuard}));if(sceneConfigured&&scene.zones&&Object.keys(scene.zones).length){scene.vertical={...scene.vertical,source:head.verticalLookSource,verticalLookSource:head.verticalLookSource,exclusive_axes:head.verticalExclusive,body_motion_guard:head.bodyMotionGuard};await post('/api/scene/layout',{zones:scene.zones,vertical_look:scene.vertical})}if(head.horizontalAlgorithm!==previousHorizontalAlgorithm)notice('横向头控已切换，需要重新执行头控校准。')}catch(e){if($('#headHorizontalAlgorithm')&&previousHorizontalAlgorithm)$('#headHorizontalAlgorithm').value=previousHorizontalAlgorithm;head.horizontalAlgorithm=previousHorizontalAlgorithm;notice('头控设置保存失败：'+(e?.message||e))}}
function sceneResultText(st){const r=st?.last_result||{};const bits=[r.message||''];if(Number.isFinite(r.confidence))bits.push('可信度 '+Math.round(r.confidence*100)+'%');if(r.matches)bits.push('匹配点 '+r.matches);if(Number.isFinite(r.inlier_ratio))bits.push('内点 '+Math.round(r.inlier_ratio*100)+'%');if(Number.isFinite(r.reprojection_error_px))bits.push('误差 '+r.reprojection_error_px+'px');if(Number.isFinite(r.rotation_deg))bits.push('旋转 '+r.rotation_deg+'°');return bits.filter(Boolean).join(' · ')}
function renderSceneEditor(st){scene.status=st||{};scene.zones=structuredClone(st?.zones||{});scene.vertical=structuredClone(st?.vertical_look||{});head.verticalLookSource=(scene.vertical.source==='head'||scene.vertical.verticalLookSource==='head')?'head':'hand';head.verticalExclusive=!!scene.vertical.exclusive_axes;if($('#verticalLookSource'))$('#verticalLookSource').value=head.verticalLookSource;if($('#verticalExclusive'))$('#verticalExclusive').checked=head.verticalExclusive;const configured=!!st?.configured;sceneConfigured=configured;$('#sceneEditor').hidden=!configured;$('#sceneTools').hidden=!configured;$('#sceneStatus').textContent=configured?(st.adapted?'本次已手动重新匹配并锁定':'已载入参考布局；本次没有自动适配'):'尚未记录参考场景';$('#sceneMetrics').textContent=sceneResultText(st);const image=$('#sceneReference');if(configured&&st.reference_image_url){image.src=st.reference_image_url+'?t='+Date.now()}const select=$('#sceneZoneSelect');select.replaceChildren();for(const id of SCENE_EDIT_ZONE_IDS){if(!scene.zones[id])continue;const o=document.createElement('option');o.value=id;o.textContent=SCENE_LABELS[id]||id;select.appendChild(o)}if(!scene.zones[scene.selected]||!SCENE_EDIT_ZONE_IDS.includes(scene.selected))scene.selected=SCENE_EDIT_ZONE_IDS.find(id=>scene.zones[id])||'';select.value=scene.selected;renderSceneEditableZones();syncSceneTools();syncControlLabels();renderMainStatus()}
function renderSceneEditableZones(){const layer=$('#sceneEditorZones');layer.replaceChildren();for(const id of SCENE_EDIT_ZONE_IDS){const z=scene.zones[id];if(!z)continue;const el=document.createElement('div');el.className='scene-edit-zone'+(id==='lookGate'?' gate':'');el.dataset.id=id;el.textContent=SCENE_LABELS[id]||id;const r=Number(z.r)||.07;el.style.left=((Number(z.cx)-r)*100)+'%';el.style.top=((Number(z.cy)-r)*100)+'%';el.style.width=(2*r*100)+'%';el.style.height=(2*r*100)+'%';el.style.fontSize='11px';el.addEventListener('pointerdown',startSceneDrag);layer.appendChild(el)}const line=$('#sceneVerticalCenter');const cy=Number(scene.vertical.center_y??.5);line.style.top=(cy*100)+'%';line.hidden=!scene.status?.configured}
function startSceneDrag(e){e.preventDefault();scene.selected=e.currentTarget.dataset.id;$('#sceneZoneSelect').value=scene.selected;syncSceneTools();scene.drag={id:scene.selected};e.currentTarget.setPointerCapture?.(e.pointerId)}
function updateSceneDrag(e){if(!scene.drag)return;const rect=$('#sceneEditorZones').getBoundingClientRect();const z=scene.zones[scene.drag.id];if(!z)return;z.cx=clamp((e.clientX-rect.left)/rect.width,0,1);z.cy=clamp((e.clientY-rect.top)/rect.height,0,1);renderSceneEditableZones()}
function endSceneDrag(){scene.drag=null}
function syncSceneTools(){const z=scene.zones[scene.selected];if(z){$('#sceneRadius').value=Number(z.r||.07)*100;$('#sceneRadiusValue').textContent=(Number(z.r||.07)*100).toFixed(1)+'%'}$('#sceneVerticalRange').value=Number(scene.vertical.range_y||.18)*100;$('#sceneVerticalRangeValue').textContent=(Number(scene.vertical.range_y||.18)*100).toFixed(0)+'%';$('#sceneVerticalDeadzone').value=Number(scene.vertical.deadzone||.1)*100;$('#sceneVerticalDeadzoneValue').textContent=(Number(scene.vertical.deadzone||.1)*100).toFixed(0)+'%'}
async function refreshScene(){try{renderSceneEditor(await api('/api/scene/status'))}catch(e){$('#sceneStatus').textContent='场景状态读取失败：'+e.message}}
async function openLiveZoneEditor(){
  try{
    if(output.enabled)await setOutput(false);
    if(!sceneConfigured&&!(await ensureInitialSceneLayout())){notice('先识别到头和双肩，建立 6 个区域后再调整；首次定位不要求全身入镜。');return}
    await refreshScene();
    if(!sceneConfigured||!Object.keys(scene.zones||{}).length){notice('当前还没有可调整的固定区域。');return}
    zoneEditBackup=structuredClone(scene.zones);zoneEditMode=true;
    viewer.classList.add('zone-editing');$('#zoneEditBar')?.classList.add('open');
    renderKernelZones(kernelState?.zones||{});
    notice('调整模式：直接在摄像头画面上拖动圆圈。调整期间游戏输出已关闭。');
  }catch(e){notice('打开区域调整失败：'+(e?.message||e))}
}
function closeLiveZoneEditor(){zoneEditMode=false;liveZoneDrag=null;zoneEditBackup=null;viewer.classList.remove('zone-editing');$('#zoneEditBar')?.classList.remove('open');renderKernelZones(kernelState?.zones||{})}
function startLiveZoneDrag(e){
  if(!zoneEditMode)return;const el=e.currentTarget,id=el?.dataset?.zone;if(!id||!scene.zones?.[id])return;
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
  try{const r=await post('/api/scene/layout',{zones:scene.zones,vertical_look:scene.vertical});renderSceneEditor(r);closeLiveZoneEditor();notice('体感区域位置已保存。')}catch(e){notice('保存区域失败：'+(e?.message||e))}
}
function cancelLiveZones(){if(zoneEditBackup)scene.zones=structuredClone(zoneEditBackup);closeLiveZoneEditor();notice('已取消区域调整。')}

async function captureScene(){try{const r=await post('/api/scene/capture',{});if(r.pending){notice('已请求手机发送一张场景截图，请保持站位。')}else{notice('参考场景已记录，可以回到电脑调整圆圈。')}await refreshScene()}catch(e){notice('记录场景失败：'+e.message)}}
async function rematchScene(){try{const r=await post('/api/scene/rematch',{});if(r.pending)notice('已请求手机截图用于重新匹配，请保持游戏站位。');else notice('本次场景重新匹配成功，区域已锁定。');await refreshScene()}catch(e){notice('重新匹配失败：'+e.message);await refreshScene()}}
async function saveScene(){try{const r=await post('/api/scene/layout',{zones:scene.zones,vertical_look:scene.vertical});renderSceneEditor(r);notice('固定空间区域已保存。')}catch(e){notice('保存区域失败：'+e.message)}}
async function init(){try{const d=await api('/api/models');modelAvailable=!!d.models?.[0]?.available;if(!modelAvailable)notice('本地服务未找到摄像头模型')}catch(e){notice('服务器连接失败：'+e.message)}syncControlLabels();await refreshKernel();await refreshInput();await refreshOutput();await refreshXinput();await refreshVoice();await refreshVoiceCommands();try{await refreshProfile()}catch(e){notice('游戏 Profile 读取失败：'+(e?.message||e))}await refreshCameraConfig();await refreshPerformance();await refreshScene();renderVoiceRows(voice.status?.mappings||[]);setInterval(refreshKernel,250);setInterval(refreshInput,700);setInterval(refreshOutput,700);setInterval(refreshXinput,700);setInterval(refreshVoice,900);setInterval(refreshPerformance,700);setInterval(refreshPreview,150)}

$('#profileSearchBtn')?.addEventListener('click',()=>searchProfiles().catch(e=>notice('搜索游戏失败：'+(e?.message||e))));
$('#profileSearch')?.addEventListener('keydown',e=>{if(e.key==='Enter')searchProfiles().catch(err=>notice('搜索游戏失败：'+(err?.message||err)))});
$('#profileApplyBtn')?.addEventListener('click',()=>applySelectedProfile().catch(e=>notice('切换游戏失败：'+(e?.message||e))));
$('#saveProfileBindingsBtn')?.addEventListener('click',()=>saveProfileBindings().catch(e=>notice('保存映射失败：'+(e?.message||e))));
$('#resetProfileBindingsBtn')?.addEventListener('click',()=>resetProfileBindings().catch(e=>notice('恢复映射失败：'+(e?.message||e))));
$('#profileBindingRows')?.addEventListener('change',e=>{
  const control=e.target?.closest?.('.binding-type,.binding-target,.binding-behavior');if(!control)return;
  if(control.classList.contains('binding-type')&&gameProfile.actions?.[control.value]?.free_text)return;
  scheduleProfileAutoSave();
});
$('#profileBindingRows')?.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target?.classList?.contains('binding-target'))e.target.blur()});
$('#cameraBtn').addEventListener('click', toggleLocalCamera);
$('#mainActionBtn').addEventListener('click', handleMainAction);
$('#overlayBtn').addEventListener('click', toggleOverlay);
$('#sceneCaptureBtn').addEventListener('click',captureScene);
$('#sceneRematchBtn').addEventListener('click',rematchScene);
$('#sceneCaptureSettingsBtn').addEventListener('click',captureScene);
$('#sceneRematchSettingsBtn').addEventListener('click',rematchScene);
$('#sceneSaveBtn').addEventListener('click',saveScene);
$('#sceneZoneSelect').addEventListener('change',e=>{scene.selected=e.target.value;syncSceneTools()});
$('#adjustZonesBtn')?.addEventListener('click',()=>void openLiveZoneEditor());
$('#saveLiveZonesBtn')?.addEventListener('click',()=>void saveLiveZones());
$('#cancelLiveZonesBtn')?.addEventListener('click',cancelLiveZones);
document.querySelectorAll('.zone').forEach(el=>{el.addEventListener('pointerdown',startLiveZoneDrag);el.addEventListener('pointermove',moveLiveZoneDrag);el.addEventListener('pointerup',endLiveZoneDrag);el.addEventListener('pointercancel',endLiveZoneDrag)});
$('#sceneRadius').addEventListener('input',e=>{const z=scene.zones[scene.selected];if(z){z.r=Number(e.target.value)/100;syncSceneTools();renderSceneEditableZones()}});
$('#sceneVerticalRange').addEventListener('input',e=>{scene.vertical.range_y=Number(e.target.value)/100;syncSceneTools()});
$('#sceneVerticalDeadzone').addEventListener('input',e=>{scene.vertical.deadzone=Number(e.target.value)/100;syncSceneTools()});
window.addEventListener('pointermove',updateSceneDrag);
window.addEventListener('pointerup',endSceneDrag);
$('#sceneVerticalCenter').addEventListener('pointerdown',e=>{e.preventDefault();const move=ev=>{const rect=$('#sceneEditor').getBoundingClientRect();scene.vertical.center_y=clamp((ev.clientY-rect.top)/rect.height,0,1);renderSceneEditableZones()};const up=()=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up)};window.addEventListener('pointermove',move);window.addEventListener('pointerup',up)});
$('#outputBtn').addEventListener('click', () => setOutput(!output.enabled));
$('#stopBtn').addEventListener('click', () => emergencyStop(true));
$('#calBtn').addEventListener('click', startCalibration);
$('#centerBtn').addEventListener('click', centerHead);
$('#calibrationCancel')?.addEventListener('click', startCalibration);
$('#poseSource').addEventListener('change', e => setSource(e.target.value, true));
$('#cameraBackend').addEventListener('change', async e => {
  try {
    await post('/api/camera/config', {backend: e.target.value});
    notice('采集后端已保存；下次启动摄像头生效。');
  } catch (error) {
    notice('采集后端切换失败：' + (error?.message || error));
    await refreshCameraConfig();
  }
});
$('#outputMode').addEventListener('change', async () => {
  output.mode = $('#outputMode').value;
  if(output.mode!=='gamepad')output.xinputEnabled=false;
  try {
    output.server = await post('/api/output/config', outputPayload(output.enabled));
    renderOutput(output.server);
    await refreshXinput();
  } catch (e) {
    notice('输出模式切换失败：' + (e?.message || e));
  }
});
$('#xinputMerge')?.addEventListener('change', () => void setXinputMerge());
$('#xinputMotionLeft')?.addEventListener('change', () => void setXinputMotionLeft());
$('#strength').addEventListener('input', syncControlLabels);
$('#strength').addEventListener('change', () => post('/api/output/config', outputPayload(output.enabled)).then(r => {
  output.server = r;
  renderOutput(r);
}).catch(e => notice(e.message)));
for (const id of ['headAlgorithm','headHorizontalAlgorithm','verticalLookSource','verticalExclusive','bodyMotionGuard','deadzone','speedX','speedY']) {
  $('#' + id).addEventListener('change', pushHeadConfig);
}
$('#headEnable').addEventListener('change', pushHeadConfig);
$('#invertX').addEventListener('change', pushHeadConfig);
$('#invertY').addEventListener('change', pushHeadConfig);
$('#addVoiceBtn').addEventListener('click', () => addVoiceRow());
$('#saveVoiceBtn').addEventListener('click', () => saveVoiceMappings()
  .then(() => notice('语音词表已保存。'))
  .catch(e => notice('保存失败：' + (e?.message || e))));
$('#voiceCommandsBtn')?.addEventListener('click',()=>{$('#voiceCommandsMask')?.classList.add('open');$('#voiceCommandsMask')?.setAttribute('aria-hidden','false')});
$('#closeVoiceCommandsBtn')?.addEventListener('click',()=>{$('#voiceCommandsMask')?.classList.remove('open');$('#voiceCommandsMask')?.setAttribute('aria-hidden','true')});
$('#voiceCommandsMask')?.addEventListener('click',e=>{if(e.target===$('#voiceCommandsMask'))$('#closeVoiceCommandsBtn')?.click()});
$('#settingsBtn').addEventListener('click', () => {
  $('#settingsMask').classList.add('open');
  $('#settingsMask').setAttribute('aria-hidden', 'false');
});
$('#closeSettingsBtn').addEventListener('click', () => {
  $('#settingsMask').classList.remove('open');
  $('#settingsMask').setAttribute('aria-hidden', 'true');
});
$('#settingsMask').addEventListener('click', e => {
  if (e.target === $('#settingsMask')) $('#closeSettingsBtn').click();
});
window.addEventListener('beforeunload', () => {
  try { overlay.win?.close(); } catch {}
});
init();
