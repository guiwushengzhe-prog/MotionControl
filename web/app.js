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
const BODY_ZONES = {leftHandUpper:{label:'Y',button:'Y'},leftHandLower:{label:'X',button:'X'},rightHandUpper:{label:'B',button:'B'},rightHandLower:{label:'A',button:'A'},leftFoot:{label:'LB',button:'LB'},rightFoot:{label:'RB',button:'RB'}};

let currentPoseMap=null, kernelState=null, sourceMode='computer', cameraRunning=false, modelAvailable=false;
const output={enabled:false,mode:'mouse',strength:160,server:null};
const head={algorithm:'pnp',deadzone:.10,sensitivityX:58,sensitivityY:46,enabled:true,invertX:false,invertY:false};
const motion={config:[]};
const voice={status:null};
const overlay={win:null,canvas:null,ctx:null};
const perfUi={latest:null,renderTimes:[],previewBusy:false,previewTimer:null};

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
    el.classList.toggle('active',!!state?.pressed);el.textContent=def.label;
    const rect=state?.rect;if(!rect){el.style.display='none';continue}
    // CSS applies the fixed display mirror to preview, skeleton and zones as
    // one group. Do not mirror this rectangle a second time in JavaScript.
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
function renderKernelState(runtime){
  kernelState=runtime?.kernel||runtime||{};sourceMode=runtime?.body_mode||sourceMode;const k=kernelState;
  const frameWidth=Number(k.width)||640,frameHeight=Number(k.height)||480;
  currentPoseMap=k.pose||null;canvas.width=frameWidth;canvas.height=frameHeight;viewer.style.aspectRatio=`${frameWidth}/${frameHeight}`;draw(currentPoseMap);renderKernelZones(k.zones||{});
  const keys=new Set(k.buttons||[]);for(const id of ['A','B','X','Y','LB','RB'])$(`#pad${id}`)?.classList.toggle('active',keys.has(id));
  $('#buttonStatus').textContent=keys.size?'身体区域：'+[...keys].join(' + '):(currentPoseMap?'身体区域：未触发':'身体区域：等待人体');
  const active=new Set(k.motions||[]),chips={march:['#motionMarch','踏步'],calf_back:['#motionCalf','小腿向后'],squat:['#motionSquat','下蹲'],hands_up:['#motionHands','双手过头']};
  for(const[id,[sel]]of Object.entries(chips))$(sel)?.classList.toggle('active',active.has(id));
  $('#motionStatus').textContent=active.size?'动作：'+[...active].map(id=>chips[id]?.[1]||id).join(' + '):'动作：未触发';
  const hs=k.head||{};
  if(Number.isFinite(hs.output_x)){
    const algo=hs.algorithm==='ratio'?'比例':'PnP';
    $('#headStatus').textContent=hs.calibrated?`头控 ${algo} · X ${Number(hs.output_x).toFixed(0)}% · Y ${Number(hs.output_y).toFixed(0)}%`:'头控：等待校准，可说“开始校准”';
  }
  if(hs.calibrated!==undefined){
    $('#calBtn').textContent=hs.calibrating?'取消校准':(hs.calibrated?'重新校准':'开始校准');
    $('#calStatus').textContent=hs.calibrating?(hs.notice||hs.quality||'正在校准'):(hs.notice||hs.quality||'等待校准，可说“开始校准”');
    $('#calStatus').title=hs.estimate_error||'';
    renderCalibrationOverlay(hs);
  }
  if(hs.algorithm){
    $('#headAlgorithm').value=hs.algorithm;
    $('#deadzone').value=Math.round(Number(hs.deadzone||.10)*100);
    $('#speedX').value=Number(hs.sensitivity_x||58);$('#speedY').value=Number(hs.sensitivity_y||46);
    $('#headEnable').checked=!!hs.enabled;$('#invertX').checked=!!hs.invert_x;$('#invertY').checked=!!hs.invert_y;syncControlLabels();
  }
  const camera=runtime?.camera||{};cameraRunning=!!camera.running;$('#poseSource').value=sourceMode;$('#cameraBtn').disabled=sourceMode==='phone';$('#cameraBtn').textContent=sourceMode==='phone'?'手机姿态由本地服务接收':(cameraRunning?'停止本地摄像头':'启动本地摄像头');
  if(cameraPreview){
    const showPreview=sourceMode==='computer'&&cameraRunning;
    cameraPreview.style.display=showPreview?'block':'none';
    if(!showPreview) cameraPreview.removeAttribute('src');
  }
  $('#cameraPill').textContent=sourceMode==='phone'?'手机姿态源':(cameraRunning?'本地摄像头运行':'本地摄像头未启动');$('#cameraPill').className='pill '+(sourceMode==='phone'||cameraRunning?'ok':'bad');
  $('#posePill').textContent=currentPoseMap?'人体已识别':'未识别人体';$('#posePill').className='pill '+(currentPoseMap?'ok':'bad');renderOverlay(currentPoseMap);
}
function renderInputStatus(status){const connected=!!(status?.mobile_pose_connected||status?.handheld_connected),pill=$('#mobileStatus');pill.textContent=connected?'手机已连接':'手机未连接';pill.className='pill '+(connected?'ok':'bad');const field=$('#phoneWsUrl');if(field)field.value=status?.phone_ws_urls?.[0]||'连接服务器后显示'}
async function refreshKernel(){try{renderKernelState(await api('/api/kernel/status'))}catch{}}
async function refreshInput(){try{renderInputStatus(await api('/api/input/status'))}catch{}}

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
    `最近人体数：${formatPerf(data.recent_humans)} · 预览：${data.preview_ready?'已就绪':'—'}`,
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

function outputPayload(enabled=output.enabled){const gain=clamp(output.strength,60,300)/100;return{mode:output.mode,enabled,mouse_speed_x:600*gain,mouse_speed_y:450*gain,gamepad_gain:gain}}
function renderOutput(s=output.server){const on=!!(s?.enabled??output.enabled);output.enabled=on;output.mode=s?.mode||output.mode;$('#outputMode').value=output.mode;$('#outputPill').textContent=on?'输出开启':'输出关闭';$('#outputPill').className='pill '+(on?'ok':'bad');$('#outputBtn').textContent=on?'关闭输出 F8':'开启输出 F8';if(!s){$('#backendStatus').textContent='正在检查输出后端…';return}$('#backendStatus').textContent=`${s.mouse_available?'鼠标可用':'鼠标不可用'} · ${s.gamepad_connected?'Xbox 已连接':'Xbox 未连接'}`+(s.last_error?' · '+s.last_error:'')}
async function refreshOutput(){try{output.server=await api('/api/output-status');renderOutput(output.server)}catch{}}
async function setOutput(enabled){try{output.server=await post('/api/output/config',outputPayload(enabled));renderOutput(output.server)}catch(e){notice('输出开启失败：'+(e?.message||e));renderOutput()}}
async function emergencyStop(show=true){try{output.server=await post('/api/output/stop',{})}catch{}output.enabled=false;renderOutput(output.server);if(show)notice('本地服务已停止所有输出。')}

async function setSource(source,enabled=true){try{const result=await post('/api/input/source',{source,enabled});sourceMode=source;renderKernelState(result);await refreshInput();notice('输入源已切换：'+(source==='phone'?'手机摄像头':'电脑摄像头'))}catch(e){notice('输入源切换失败：'+(e?.message||e));await refreshKernel()}}
async function toggleLocalCamera(){if(sourceMode==='phone')return;try{const result=await post('/api/input/source',{source:'computer',enabled:!cameraRunning});renderKernelState(result)}catch(e){notice('本地摄像头操作失败：'+(e?.message||e));await refreshKernel()}}
async function startCalibration(){const running=!!kernelState?.head?.calibrating;try{renderKernelState(await post(running?'/api/head/calibration/cancel':'/api/head/calibration/start',{}));notice(running?'校准已取消':'校准已开始：看向游戏屏幕中心，保持自然姿势')}catch(e){notice('中心设置失败：'+(e?.message||e))}}

function renderOverlay(map=currentPoseMap){if(!overlay.win||overlay.win.closed||!overlay.canvas||!overlay.ctx)return;const c=overlay.canvas,octx=overlay.ctx,w=c.width,h=c.height;octx.setTransform(1,0,0,1,0,0);octx.clearRect(0,0,w,h);octx.fillStyle='#050608';octx.fillRect(0,0,w,h);if(map){octx.strokeStyle='rgba(80,220,255,.92)';octx.lineWidth=Math.max(2,w/260);octx.fillStyle='rgba(255,255,255,.96)';for(const[a,b]of EDGES){const p=map[a],q=map[b];if(!p||!q||p.score<.3||q.score<.3)continue;const vp=visualPoint(p),vq=visualPoint(q);octx.beginPath();octx.moveTo(vp.x*w,vp.y*h);octx.lineTo(vq.x*w,vq.y*h);octx.stroke()}for(const p of Object.values(map)){if(p.score<.3)continue;const vp=visualPoint(p);octx.beginPath();octx.arc(vp.x*w,vp.y*h,Math.max(2.2,w/190),0,Math.PI*2);octx.fill()}}const buttons=kernelState?.buttons||[],motions=kernelState?.motions||[];const text=buttons.length?`区域 ${buttons.join('+')}`:(motions.length?`动作 ${motions.join('+')}`:(map?'未触发':'未识别人体'));octx.fillStyle='rgba(0,0,0,.62)';octx.fillRect(0,h-Math.max(25,h/10),w,Math.max(25,h/10));octx.fillStyle='#fff';octx.font=`600 ${Math.max(12,Math.round(w/32))}px system-ui,sans-serif`;octx.fillText(`${output.enabled?'输出开':'输出关'} · ${text}`,Math.max(7,w/70),h-Math.max(7,h/70))}
async function toggleOverlay(){if(overlay.win&&!overlay.win.closed){try{overlay.win.close()}catch{}overlay.win=null;overlay.canvas=null;overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗';return}if(!window.documentPictureInPicture?.requestWindow){notice('当前浏览器不支持置顶游戏悬浮窗。');return}try{const pip=await window.documentPictureInPicture.requestWindow({width:420,height:315});pip.document.title='MotionControl';pip.document.body.style.cssText='margin:0;overflow:hidden;background:#050608;width:100vw;height:100vh';const c=pip.document.createElement('canvas');c.width=640;c.height=480;c.style.cssText='display:block;width:100vw;height:100vh;object-fit:contain;background:#050608';pip.document.body.appendChild(c);overlay.win=pip;overlay.canvas=c;overlay.ctx=c.getContext('2d');pip.addEventListener('pagehide',()=>{overlay.win=overlay.canvas=overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗'},{once:true});$('#overlayBtn').textContent='关闭悬浮';renderOverlay(currentPoseMap)}catch(e){notice('悬浮窗启动失败：'+(e?.message||e))}}

function renderMotionRows(items){motion.config=items||[];const box=$('#motionRows');box.replaceChildren();for(const m of motion.config){const row=document.createElement('div');row.className='motion-row';row.dataset.id=m.id;const enabled=document.createElement('input');enabled.type='checkbox';enabled.className='motion-enabled';enabled.checked=!!m.enabled;const name=document.createElement('div');name.className='motion-name';name.textContent=m.name;const type=document.createElement('select');type.className='motion-type';for(const[v,t]of[['keyboard','键盘'],['gamepad','Xbox 按键'],['gamepad_axis','Xbox 左摇杆']]){const o=document.createElement('option');o.value=v;o.textContent=t;type.appendChild(o)}type.value=m.type;const target=document.createElement('input');target.type='text';target.className='motion-target';target.value=m.target||'';target.placeholder=type.value==='gamepad_axis'?'LS_UP / LS_DOWN / LS_LEFT / LS_RIGHT':(type.value==='gamepad'?'A / B / X / Y / LB / RB':'W / SPACE / CTRL+W');row.append(enabled,name,type,target);box.appendChild(row)}}
function readMotionRows(){const items=[];for(const row of document.querySelectorAll('.motion-row')){const old=motion.config.find(x=>x.id===row.dataset.id),enabled=row.querySelector('.motion-enabled').checked,type=row.querySelector('.motion-type').value,target=row.querySelector('.motion-target').value.trim().toUpperCase();if(enabled&&!target)throw new Error(`${old?.name||row.dataset.id} 已启用但没有设置输出`);items.push({id:row.dataset.id,name:old?.name||'',enabled,type,target})}return items}
async function refreshMotionConfig(){try{renderMotionRows((await api('/api/motion/config')).motions||[])}catch(e){notice('动作设置读取失败：'+e.message)}}
async function saveMotionConfig(){const s=await post('/api/motion/config',{motions:readMotionRows()});renderMotionRows(s.motions||[]);return s}

function addVoiceRow(mapping={phrase:'',type:'keyboard',target:''}){const row=document.createElement('div');row.className='voice-row';const phrase=document.createElement('input');phrase.className='voice-phrase';phrase.placeholder='说：例如 地图';phrase.value=mapping.phrase||'';const type=document.createElement('select');type.className='voice-type';for(const[value,label]of[['keyboard','键盘/组合键'],['gamepad','Xbox 键']]){const o=document.createElement('option');o.value=value;o.textContent=label;type.appendChild(o)}type.value=mapping.type||'keyboard';const target=document.createElement('input');target.className='voice-target';target.value=mapping.target||'';const remove=document.createElement('button');remove.type='button';remove.className='btn voice-remove';remove.textContent='删';remove.addEventListener('click',()=>{row.remove();if(!$('#voiceRows').children.length)addVoiceRow()});row.append(phrase,type,target,remove);$('#voiceRows').appendChild(row)}
function readVoiceMappings(){const rows=[...document.querySelectorAll('.voice-row')],items=[],old=new Map((voice.status?.mappings||[]).map(m=>[m.phrase,m]));for(const row of rows){const phrase=row.querySelector('.voice-phrase').value.trim(),type=row.querySelector('.voice-type').value,target=row.querySelector('.voice-target').value.trim();if(!phrase&&!target)continue;if(!phrase||!target)throw new Error('语音命令必须同时填写“说什么”和“输出什么”');const item={phrase,type,target},previous=old.get(phrase);if(previous?.synonyms?.length)item.synonyms=[...previous.synonyms];items.push(item)}return items}
function renderVoiceRows(items){$('#voiceRows').replaceChildren();for(const m of items||[])addVoiceRow(m);if(!$('#voiceRows').children.length)addVoiceRow()}
function renderVoiceStatus(s=voice.status){if(!s)return;voice.status=s;const has=!!s.model_ready&&Number(s.supported_count||0)>0;const connected=!!s.connected;$('#voiceMode').textContent=has?(s.recognizer_mode==='grammar'?`有限词表 ${s.supported_count}`:`开放识别 ${s.supported_count}`):'未就绪';$('#voiceMode').className='pill '+(has?'ok':'warn');$('#voicePill').textContent=connected?(s.source_kind==='computer'?(s.audio_ready?'电脑语音运行':'电脑语音未就绪'):'手机语音源'):'语音未连接';$('#voicePill').className='pill '+(connected?'ok':'bad');$('#voiceBtn').textContent='语音由本地服务接收';$('#voiceBtn').disabled=true;const modelPath=s.model_path||'models/vosk-model-small-cn-0.22';const mp=$('#voiceModelPath');if(mp){mp.textContent='模型：'+modelPath;mp.title=modelPath}const parts=[];if(s.source_kind==='computer'&&s.bytes_received)parts.push(`电脑音频 ${s.bytes_received} 字节 RMS ${Number(s.rms||0).toFixed(0)}`);if(s.partial)parts.push(`听到：${s.partial}`);if(s.final)parts.push(`识别：${s.final}`);if(s.last_command)parts.push(`命令：${s.last_command}`);if(s.last_error)parts.push(s.last_error);if(!parts.length)parts.push(s.model_ready?'等待本地麦克风或手机语音文本。':'未找到 Vosk 中文模型。');$('#voiceStatus').textContent=parts.join(' · ')}
async function saveVoiceMappings(){const s=await post('/api/voice/config',{mappings:readVoiceMappings()});voice.status=s;renderVoiceStatus(s);return s}
async function refreshVoice(){try{voice.status=await api('/api/voice/status');renderVoiceStatus(voice.status)}catch{}}

function syncControlLabels(){head.algorithm=$('#headAlgorithm').value;head.deadzone=Number($('#deadzone').value)/100;head.sensitivityX=Number($('#speedX').value);head.sensitivityY=Number($('#speedY').value);head.enabled=$('#headEnable').checked;head.invertX=$('#invertX').checked;head.invertY=$('#invertY').checked;$('#deadzoneValue').textContent=Math.round(head.deadzone*100)+'%';$('#speedXValue').textContent=head.sensitivityX+'%';$('#speedYValue').textContent=head.sensitivityY+'%';output.strength=Number($('#strength').value);$('#strengthValue').textContent=output.strength+'%'}
async function pushHeadConfig(){syncControlLabels();try{renderKernelState(await post('/api/head/config',{algorithm:head.algorithm,deadzone:head.deadzone,sensitivity_x:head.sensitivityX,sensitivity_y:head.sensitivityY,enabled:head.enabled,invert_x:head.invertX,invert_y:head.invertY}))}catch(e){notice('头控设置保存失败：'+(e?.message||e))}}
async function init(){try{const d=await api('/api/models');modelAvailable=!!d.models?.[0]?.available;if(!modelAvailable)notice('本地服务未找到 MediaPipe Full task：'+(d.model_root||'I:\\MotionControl-Pose-Models\\models'))}catch(e){notice('服务器连接失败：'+e.message)}syncControlLabels();await refreshKernel();await refreshInput();await refreshOutput();await refreshVoice();await refreshMotionConfig();await refreshCameraConfig();await refreshPerformance();renderVoiceRows(voice.status?.mappings||[]);setInterval(refreshKernel,250);setInterval(refreshInput,700);setInterval(refreshOutput,700);setInterval(refreshVoice,900);setInterval(refreshPerformance,700);setInterval(refreshPreview,150)}

$('#cameraBtn').addEventListener('click', toggleLocalCamera);
$('#overlayBtn').addEventListener('click', toggleOverlay);
$('#outputBtn').addEventListener('click', () => setOutput(!output.enabled));
$('#stopBtn').addEventListener('click', () => emergencyStop(true));
$('#calBtn').addEventListener('click', startCalibration);
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
  try {
    output.server = await post('/api/output/config', outputPayload(output.enabled));
    renderOutput(output.server);
  } catch (e) {
    notice('输出模式切换失败：' + (e?.message || e));
  }
});
$('#strength').addEventListener('input', syncControlLabels);
$('#strength').addEventListener('change', () => post('/api/output/config', outputPayload(output.enabled)).then(r => {
  output.server = r;
  renderOutput(r);
}).catch(e => notice(e.message)));
for (const id of ['headAlgorithm','deadzone','speedX','speedY']) {
  $('#' + id).addEventListener('change', pushHeadConfig);
}
$('#headEnable').addEventListener('change', pushHeadConfig);
$('#invertX').addEventListener('change', pushHeadConfig);
$('#invertY').addEventListener('change', pushHeadConfig);
$('#addVoiceBtn').addEventListener('click', () => addVoiceRow());
$('#saveVoiceBtn').addEventListener('click', () => saveVoiceMappings()
  .then(() => notice('语音词表已保存。'))
  .catch(e => notice('保存失败：' + (e?.message || e))));
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
$('#saveMotionBtn').addEventListener('click', () => saveMotionConfig()
  .then(() => notice('四个动作映射已保存。'))
  .catch(e => notice('动作设置保存失败：' + (e?.message || e))));
window.addEventListener('beforeunload', () => {
  try { overlay.win?.close(); } catch {}
});
init();
