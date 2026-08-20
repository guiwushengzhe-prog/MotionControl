const $ = s => document.querySelector(s);
const canvas = $('#canvas');
const ctx = canvas.getContext('2d');
const viewer = $('#viewer');

// The browser is a display/configuration client. Pose inference, zones,
// action debouncing, head control and watchdog timing live in Python.
const EDGES = [['left_shoulder','right_shoulder'],['left_shoulder','left_elbow'],['left_elbow','left_wrist'],['right_shoulder','right_elbow'],['right_elbow','right_wrist'],['left_shoulder','left_hip'],['right_shoulder','right_hip'],['left_hip','right_hip'],['left_hip','left_knee'],['left_knee','left_ankle'],['right_hip','right_knee'],['right_knee','right_ankle']];
const BODY_ZONES = {leftHandUpper:{label:'Y',button:'Y'},leftHandLower:{label:'X',button:'X'},rightHandUpper:{label:'B',button:'B'},rightHandLower:{label:'A',button:'A'},leftFoot:{label:'LB',button:'LB'},rightFoot:{label:'RB',button:'RB'}};

let currentPoseMap=null, kernelState=null, sourceMode='computer', cameraRunning=false, modelAvailable=false;
const output={enabled:false,mode:'mouse',strength:160,server:null};
const head={deadzoneX:.08,deadzoneY:.12,gamma:2.2,maxPercentX:60,maxPercentY:45,enabled:true,invertX:false,invertY:false};
const motion={config:[]};
const voice={running:false,stream:null,ctx:null,source:null,processor:null,gain:null,chunks:[],pendingBytes:0,sending:false,status:null};
const overlay={win:null,canvas:null,ctx:null};

const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
function notice(t){$('#notice').textContent=t;$('#notice').style.display=t?'block':'none'}
async function api(path,opt){const r=await fetch(path,opt);if(!r.ok)throw new Error(`${r.status} ${await r.text()}`);return r.json()}
async function post(path,data){return api(path,{method:'POST',headers:{'Content-Type':'application/json; charset=utf-8'},body:JSON.stringify(data)})}
async function postBinary(path,data){return api(path,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:data})}

function visualPoint(p){return{x:$('#mirrorSelect').value==='yes'?1-p.x:p.x,y:p.y}}
function visualRect(r){return $('#mirrorSelect').value==='yes'?{x1:1-r.x2,x2:1-r.x1,y1:r.y1,y2:r.y2}:r}
function draw(map){
  ctx.clearRect(0,0,canvas.width,canvas.height);if(!map)return;
  ctx.strokeStyle='#55ddff';ctx.fillStyle='#fff';ctx.lineWidth=3;
  for(const[a,b]of EDGES){const p=map[a],q=map[b];if(!p||!q||p.score<.3||q.score<.3)continue;ctx.beginPath();ctx.moveTo(p.x*canvas.width,p.y*canvas.height);ctx.lineTo(q.x*canvas.width,q.y*canvas.height);ctx.stroke()}
  for(const p of Object.values(map)){if(p.score<.3)continue;ctx.beginPath();ctx.arc(p.x*canvas.width,p.y*canvas.height,3,0,Math.PI*2);ctx.fill()}
}
function renderKernelZones(zones={}){
  for(const[id,def]of Object.entries(BODY_ZONES)){
    const el=document.querySelector(`.zone[data-zone="${id}"]`),state=zones[id];if(!el)continue;
    el.classList.toggle('active',!!state?.pressed);el.textContent=def.label;
    const rect=state?.rect;if(!rect){el.style.display='none';continue}
    const v=visualRect(rect);el.style.display='grid';el.style.left=(v.x1*100)+'%';el.style.top=(v.y1*100)+'%';el.style.width=((v.x2-v.x1)*100)+'%';el.style.height=((v.y2-v.y1)*100)+'%';
  }
}
function renderKernelState(runtime){
  kernelState=runtime?.kernel||runtime||{};sourceMode=runtime?.body_mode||sourceMode;const k=kernelState;
  currentPoseMap=k.pose||null;canvas.width=Number(k.width)||640;canvas.height=Number(k.height)||480;draw(currentPoseMap);renderKernelZones(k.zones||{});
  const keys=new Set(k.buttons||[]);for(const id of ['A','B','X','Y','LB','RB'])$(`#pad${id}`)?.classList.toggle('active',keys.has(id));
  $('#buttonStatus').textContent=keys.size?'身体区域：'+[...keys].join(' + '):(currentPoseMap?'身体区域：未触发':'身体区域：等待人体');
  const active=new Set(k.motions||[]),chips={march:['#motionMarch','踏步'],calf_back:['#motionCalf','小腿向后'],squat:['#motionSquat','下蹲'],hands_up:['#motionHands','双手过头']};
  for(const[id,[sel]]of Object.entries(chips))$(sel)?.classList.toggle('active',active.has(id));
  $('#motionStatus').textContent=active.size?'动作：'+[...active].map(id=>chips[id]?.[1]||id).join(' + '):'动作：未触发';
  const hs=k.head||{};if(Number.isFinite(hs.output_x))$('#headStatus').textContent=hs.calibrated?`头控 ${hs.quality||''} · X ${Number(hs.output_x).toFixed(0)}% · Y ${Number(hs.output_y).toFixed(0)}%`:'头控：等待校准';
  if(hs.calibrated!==undefined)$('#calStatus').textContent=hs.calibrating?`校准：${hs.stage||''}`:(hs.calibrated?(hs.quality||'校准完成'):'未校准');
  if(Number.isFinite(hs.deadzone_x)){$('#deadX').value=Math.round(hs.deadzone_x*100);$('#deadY').value=Math.round(hs.deadzone_y*100);$('#gamma').value=hs.gamma;$('#speedX').value=hs.max_percent_x;$('#speedY').value=hs.max_percent_y;$('#headEnable').checked=!!hs.enabled;$('#invertX').checked=!!hs.invert_x;$('#invertY').checked=!!hs.invert_y;syncControlLabels()}
  const camera=runtime?.camera||{};cameraRunning=!!camera.running;$('#poseSource').value=sourceMode;$('#cameraBtn').disabled=sourceMode==='phone';$('#cameraBtn').textContent=sourceMode==='phone'?'手机姿态由本地服务接收':(cameraRunning?'停止本地摄像头':'启动本地摄像头');
  $('#cameraPill').textContent=sourceMode==='phone'?'手机姿态源':(cameraRunning?'本地摄像头运行':'本地摄像头未启动');$('#cameraPill').className='pill '+(sourceMode==='phone'||cameraRunning?'ok':'bad');
  $('#posePill').textContent=currentPoseMap?'人体已识别':'未识别人体';$('#posePill').className='pill '+(currentPoseMap?'ok':'bad');renderOverlay(currentPoseMap);
}
function renderInputStatus(status){const connected=!!(status?.mobile_pose_connected||status?.handheld_connected),pill=$('#mobileStatus');pill.textContent=connected?'手机已连接':'手机未连接';pill.className='pill '+(connected?'ok':'bad');const field=$('#phoneWsUrl');if(field)field.value=status?.phone_ws_urls?.[0]||'连接服务器后显示'}
async function refreshKernel(){try{renderKernelState(await api('/api/kernel/status'))}catch{}}
async function refreshInput(){try{renderInputStatus(await api('/api/input/status'))}catch{}}

function outputPayload(enabled=output.enabled){const gain=clamp(output.strength,60,300)/100;return{mode:output.mode,enabled,mouse_speed_x:600*gain,mouse_speed_y:450*gain,gamepad_gain:gain}}
function renderOutput(s=output.server){const on=!!(s?.enabled??output.enabled);output.enabled=on;output.mode=s?.mode||output.mode;$('#outputMode').value=output.mode;$('#outputPill').textContent=on?'输出开启':'输出关闭';$('#outputPill').className='pill '+(on?'ok':'bad');$('#outputBtn').textContent=on?'关闭输出 F8':'开启输出 F8';if(!s){$('#backendStatus').textContent='正在检查输出后端…';return}$('#backendStatus').textContent=`${s.mouse_available?'鼠标可用':'鼠标不可用'} · ${s.gamepad_connected?'Xbox 已连接':'Xbox 未连接'}`+(s.last_error?' · '+s.last_error:'')}
async function refreshOutput(){try{output.server=await api('/api/output-status');renderOutput(output.server)}catch{}}
async function setOutput(enabled){try{output.server=await post('/api/output/config',outputPayload(enabled));renderOutput(output.server)}catch(e){notice('输出开启失败：'+(e?.message||e));renderOutput()}}
async function emergencyStop(show=true){try{output.server=await post('/api/output/stop',{})}catch{}output.enabled=false;renderOutput(output.server);if(show)notice('本地服务已停止所有输出。')}

async function setSource(source,enabled=true){try{const result=await post('/api/input/source',{source,enabled});sourceMode=source;renderKernelState(result);await refreshInput();notice('输入源已切换：'+(source==='phone'?'手机摄像头':'电脑摄像头'))}catch(e){notice('输入源切换失败：'+(e?.message||e));await refreshKernel()}}
async function toggleLocalCamera(){if(sourceMode==='phone')return;try{const result=await post('/api/input/source',{source:'computer',enabled:!cameraRunning});renderKernelState(result)}catch(e){notice('本地摄像头操作失败：'+(e?.message||e));await refreshKernel()}}
async function startCalibration(){try{renderKernelState(await post('/api/head/calibration/start',{}));notice('本地头控校准已开始：正视、左转、右转、抬头、低头。')}catch(e){notice('校准启动失败：'+(e?.message||e))}}
async function setCurrentCenter(){try{renderKernelState(await post('/api/head/calibration/center',{}));notice('已把当前姿势设为本地头控中心。')}catch(e){notice('设置中心失败：'+(e?.message||e))}}

function renderOverlay(map=currentPoseMap){if(!overlay.win||overlay.win.closed||!overlay.canvas||!overlay.ctx)return;const c=overlay.canvas,octx=overlay.ctx,w=c.width,h=c.height;octx.setTransform(1,0,0,1,0,0);octx.clearRect(0,0,w,h);octx.fillStyle='#050608';octx.fillRect(0,0,w,h);if(map){octx.strokeStyle='rgba(80,220,255,.92)';octx.lineWidth=Math.max(2,w/260);octx.fillStyle='rgba(255,255,255,.96)';for(const[a,b]of EDGES){const p=map[a],q=map[b];if(!p||!q||p.score<.3||q.score<.3)continue;const vp=visualPoint(p),vq=visualPoint(q);octx.beginPath();octx.moveTo(vp.x*w,vp.y*h);octx.lineTo(vq.x*w,vq.y*h);octx.stroke()}for(const p of Object.values(map)){if(p.score<.3)continue;const vp=visualPoint(p);octx.beginPath();octx.arc(vp.x*w,vp.y*h,Math.max(2.2,w/190),0,Math.PI*2);octx.fill()}}const buttons=kernelState?.buttons||[],motions=kernelState?.motions||[];const text=buttons.length?`区域 ${buttons.join('+')}`:(motions.length?`动作 ${motions.join('+')}`:(map?'未触发':'未识别人体'));octx.fillStyle='rgba(0,0,0,.62)';octx.fillRect(0,h-Math.max(25,h/10),w,Math.max(25,h/10));octx.fillStyle='#fff';octx.font=`600 ${Math.max(12,Math.round(w/32))}px system-ui,sans-serif`;octx.fillText(`${output.enabled?'输出开':'输出关'} · ${text}`,Math.max(7,w/70),h-Math.max(7,h/70))}
async function toggleOverlay(){if(overlay.win&&!overlay.win.closed){try{overlay.win.close()}catch{}overlay.win=null;overlay.canvas=null;overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗';return}if(!window.documentPictureInPicture?.requestWindow){notice('当前浏览器不支持置顶游戏悬浮窗。');return}try{const pip=await window.documentPictureInPicture.requestWindow({width:420,height:315});pip.document.title='MotionControl';pip.document.body.style.cssText='margin:0;overflow:hidden;background:#050608;width:100vw;height:100vh';const c=pip.document.createElement('canvas');c.width=640;c.height=480;c.style.cssText='display:block;width:100vw;height:100vh;object-fit:contain;background:#050608';pip.document.body.appendChild(c);overlay.win=pip;overlay.canvas=c;overlay.ctx=c.getContext('2d');pip.addEventListener('pagehide',()=>{overlay.win=overlay.canvas=overlay.ctx=null;$('#overlayBtn').textContent='悬浮窗'},{once:true});$('#overlayBtn').textContent='关闭悬浮';renderOverlay(currentPoseMap)}catch(e){notice('悬浮窗启动失败：'+(e?.message||e))}}

function renderMotionRows(items){motion.config=items||[];const box=$('#motionRows');box.replaceChildren();for(const m of motion.config){const row=document.createElement('div');row.className='motion-row';row.dataset.id=m.id;const enabled=document.createElement('input');enabled.type='checkbox';enabled.className='motion-enabled';enabled.checked=!!m.enabled;const name=document.createElement('div');name.className='motion-name';name.textContent=m.name;const type=document.createElement('select');type.className='motion-type';for(const[v,t]of[['keyboard','键盘'],['gamepad','Xbox 按键'],['gamepad_axis','Xbox 左摇杆']]){const o=document.createElement('option');o.value=v;o.textContent=t;type.appendChild(o)}type.value=m.type;const target=document.createElement('input');target.type='text';target.className='motion-target';target.value=m.target||'';target.placeholder=type.value==='gamepad_axis'?'LS_UP / LS_DOWN / LS_LEFT / LS_RIGHT':(type.value==='gamepad'?'A / B / X / Y / LB / RB':'W / SPACE / CTRL+W');row.append(enabled,name,type,target);box.appendChild(row)}}
function readMotionRows(){const items=[];for(const row of document.querySelectorAll('.motion-row')){const old=motion.config.find(x=>x.id===row.dataset.id),enabled=row.querySelector('.motion-enabled').checked,type=row.querySelector('.motion-type').value,target=row.querySelector('.motion-target').value.trim().toUpperCase();if(enabled&&!target)throw new Error(`${old?.name||row.dataset.id} 已启用但没有设置输出`);items.push({id:row.dataset.id,name:old?.name||'',enabled,type,target})}return items}
async function refreshMotionConfig(){try{renderMotionRows((await api('/api/motion/config')).motions||[])}catch(e){notice('动作设置读取失败：'+e.message)}}
async function saveMotionConfig(){const s=await post('/api/motion/config',{motions:readMotionRows()});renderMotionRows(s.motions||[]);return s}

function addVoiceRow(mapping={phrase:'',type:'keyboard',target:''}){const row=document.createElement('div');row.className='voice-row';const phrase=document.createElement('input');phrase.className='voice-phrase';phrase.placeholder='说：例如 地图';phrase.value=mapping.phrase||'';const type=document.createElement('select');type.className='voice-type';for(const[value,label]of[['keyboard','键盘/组合键'],['gamepad','Xbox 键']]){const o=document.createElement('option');o.value=value;o.textContent=label;type.appendChild(o)}type.value=mapping.type||'keyboard';const target=document.createElement('input');target.className='voice-target';target.value=mapping.target||'';const remove=document.createElement('button');remove.type='button';remove.className='btn voice-remove';remove.textContent='删';remove.addEventListener('click',()=>{row.remove();if(!$('#voiceRows').children.length)addVoiceRow()});row.append(phrase,type,target,remove);$('#voiceRows').appendChild(row)}
function readVoiceMappings(){const rows=[...document.querySelectorAll('.voice-row')],items=[];for(const row of rows){const phrase=row.querySelector('.voice-phrase').value.trim(),type=row.querySelector('.voice-type').value,target=row.querySelector('.voice-target').value.trim();if(!phrase&&!target)continue;if(!phrase||!target)throw new Error('语音命令必须同时填写“说什么”和“输出什么”');items.push({phrase,type,target})}return items}
function renderVoiceRows(items){$('#voiceRows').replaceChildren();for(const m of items||[])addVoiceRow(m);if(!$('#voiceRows').children.length)addVoiceRow()}
function renderVoiceStatus(s=voice.status){if(!s)return;voice.status=s;const has=Number(s.supported_count||0)>0;$('#voiceMode').textContent=has?(s.recognizer_mode==='grammar'?`有限词表 ${s.supported_count}`:`开放识别 ${s.supported_count}`):'未就绪';$('#voiceMode').className='pill '+(has?'ok':'warn');$('#voicePill').textContent=voice.running?(s.audio_alive?'语音运行':'麦克风已开'):'语音关闭';$('#voicePill').className='pill '+(voice.running?'ok':'bad');$('#voiceBtn').textContent=voice.running?'关闭麦克风':'开启麦克风';const modelPath=s.model_path||'F:\\switch\\motionbridge\\models\\vosk-model-small-cn-0.22';const mp=$('#voiceModelPath');if(mp){mp.textContent='模型：'+modelPath;mp.title=modelPath}const parts=[];if(s.last_command)parts.push(`已识别“${s.last_command}” → ${String(s.last_action||'').replace('keyboard:','').replace('gamepad:','Xbox ')}`);else if(s.last_partial)parts.push(`听到：${s.last_partial}`);else if(s.last_final)parts.push(`识别：${s.last_final}`);if(s.unsupported?.length)parts.push(`模型不支持：${s.unsupported.join('、')}`);if(s.last_error)parts.push(s.last_error);if(!parts.length)parts.push(s.model_path?'词表已就绪，开启麦克风后直接说命令。':'未找到 Vosk 中文模型。');$('#voiceStatus').textContent=parts.join(' · ')}
async function saveVoiceMappings(){const s=await post('/api/voice/config',{mappings:readVoiceMappings()});voice.status=s;renderVoiceStatus(s);return s}
async function refreshVoice(){try{voice.status=await api('/api/voice/status');renderVoiceStatus(voice.status)}catch{}}
function downsamplePcm16(input,inputRate){const rate=16000;if(inputRate<=rate){const out=new Int16Array(input.length);for(let i=0;i<input.length;i++)out[i]=Math.max(-32768,Math.min(32767,Math.round(input[i]*32767)));return out}const ratio=inputRate/rate,length=Math.max(1,Math.floor(input.length/ratio)),out=new Int16Array(length);for(let i=0;i<length;i++){const a=Math.floor(i*ratio),b=Math.max(a+1,Math.min(input.length,Math.floor((i+1)*ratio)));let sum=0;for(let j=a;j<b;j++)sum+=input[j];out[i]=Math.max(-32768,Math.min(32767,Math.round(sum/(b-a)*32767)))}return out}
async function flushVoiceAudio(){if(voice.sending||!voice.running||!voice.chunks.length)return;voice.sending=true;const chunks=voice.chunks.splice(0),size=chunks.reduce((n,c)=>n+c.byteLength,0),merged=new Uint8Array(size);let off=0;for(const c of chunks){merged.set(c,off);off+=c.byteLength}try{voice.status=await postBinary('/api/voice/audio',merged);renderVoiceStatus(voice.status)}catch(e){$('#voiceStatus').textContent='语音输入失败：'+(e?.message||e)}finally{voice.sending=false;if(voice.pendingBytes>=7000)void flushVoiceAudio()}}
async function startVoice(){try{const s=await saveVoiceMappings();if(!s.mappings?.length)throw new Error('先添加至少一个语音命令');if(!s.available)throw new Error(s.last_error||'Vosk 语音识别未就绪');const ms=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});const AC=window.AudioContext||window.webkitAudioContext,ac=new AC();await ac.resume();const source=ac.createMediaStreamSource(ms),processor=ac.createScriptProcessor(4096,1,1),gain=ac.createGain();gain.gain.value=0;source.connect(processor);processor.connect(gain);gain.connect(ac.destination);voice.stream=ms;voice.ctx=ac;voice.source=source;voice.processor=processor;voice.gain=gain;voice.running=true;voice.chunks=[];voice.pendingBytes=0;processor.onaudioprocess=e=>{if(!voice.running)return;const bytes=new Uint8Array(downsamplePcm16(e.inputBuffer.getChannelData(0),ac.sampleRate).buffer);voice.chunks.push(bytes);voice.pendingBytes+=bytes.byteLength;if(voice.pendingBytes>=7000)void flushVoiceAudio()};renderVoiceStatus(s)}catch(e){stopVoice();notice('语音启动失败：'+(e?.message||e))}}
function stopVoice(){voice.running=false;voice.chunks=[];voice.pendingBytes=0;try{voice.processor&&(voice.processor.onaudioprocess=null,voice.processor.disconnect())}catch{}try{voice.source?.disconnect()}catch{}try{voice.gain?.disconnect()}catch{}if(voice.stream)voice.stream.getTracks().forEach(t=>t.stop());try{voice.ctx?.close()}catch{}voice.stream=voice.ctx=voice.source=voice.processor=voice.gain=null;renderVoiceStatus(voice.status||{})}

function syncControlLabels(){head.deadzoneX=Number($('#deadX').value)/100;head.deadzoneY=Number($('#deadY').value)/100;head.gamma=Number($('#gamma').value);head.maxPercentX=Number($('#speedX').value);head.maxPercentY=Number($('#speedY').value);head.enabled=$('#headEnable').checked;head.invertX=$('#invertX').checked;head.invertY=$('#invertY').checked;$('#deadXValue').textContent=Math.round(head.deadzoneX*100)+'%';$('#deadYValue').textContent=Math.round(head.deadzoneY*100)+'%';$('#gammaValue').textContent=head.gamma.toFixed(1);$('#speedXValue').textContent=head.maxPercentX+'%';$('#speedYValue').textContent=head.maxPercentY+'%';output.strength=Number($('#strength').value);$('#strengthValue').textContent=output.strength+'%'}
async function pushHeadConfig(){syncControlLabels();try{renderKernelState(await post('/api/head/config',{deadzone_x:head.deadzoneX,deadzone_y:head.deadzoneY,gamma:head.gamma,max_percent_x:head.maxPercentX,max_percent_y:head.maxPercentY,enabled:head.enabled,invert_x:head.invertX,invert_y:head.invertY}))}catch(e){notice('头控设置保存失败：'+(e?.message||e))}}
async function init(){try{const d=await api('/api/models');modelAvailable=!!d.models?.[0]?.available;if(!modelAvailable)notice('本地服务未找到 MediaPipe Full task：'+(d.model_root||'I:\\MotionControl-Pose-Models\\models'))}catch(e){notice('服务器连接失败：'+e.message)}syncControlLabels();await refreshKernel();await refreshInput();await refreshOutput();await refreshVoice();await refreshMotionConfig();renderVoiceRows(voice.status?.mappings||[]);setInterval(refreshKernel,250);setInterval(refreshInput,700);setInterval(refreshOutput,700);setInterval(refreshVoice,900)}

$('#cameraBtn').addEventListener('click', toggleLocalCamera);
$('#overlayBtn').addEventListener('click', toggleOverlay);
$('#outputBtn').addEventListener('click', () => setOutput(!output.enabled));
$('#stopBtn').addEventListener('click', () => emergencyStop(true));
$('#calBtn').addEventListener('click', startCalibration);
$('#centerBtn').addEventListener('click', setCurrentCenter);
$('#poseSource').addEventListener('change', e => setSource(e.target.value, true));
$('#mirrorSelect').addEventListener('change', () => {
  viewer.classList.toggle('mirror', $('#mirrorSelect').value === 'yes');
  void refreshKernel();
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
for (const id of ['deadX', 'deadY', 'gamma', 'speedX', 'speedY']) {
  $('#' + id).addEventListener('change', pushHeadConfig);
}
$('#headEnable').addEventListener('change', pushHeadConfig);
$('#invertX').addEventListener('change', pushHeadConfig);
$('#invertY').addEventListener('change', pushHeadConfig);
$('#addVoiceBtn').addEventListener('click', () => addVoiceRow());
$('#saveVoiceBtn').addEventListener('click', () => saveVoiceMappings()
  .then(() => notice('语音词表已保存。'))
  .catch(e => notice('保存失败：' + (e?.message || e))));
$('#voiceBtn').addEventListener('click', () => voice.running ? stopVoice() : startVoice());
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
  stopVoice();
  try { overlay.win?.close(); } catch {}
});
init();
