import {renderRecognitionPerformance} from './recognition-models.js';
// 设置 → 设备：摄像头、手机、麦克风、游戏输出。
import {$,api,formatPerf,notice,post} from './core.js';
import {EDGES,voiceLatchText} from './labels.js';
import {cameraRunning,renderKernelState,renderMainStatus,sourceMode,zoneEditMode} from './play.js';
import {runAction} from './shell.js';
import {S,gameProfile,output} from './state.js';
import {handMouseConfig} from './view-control.js';
import {syncPhoneCode} from './phone-connect.js';

let audioDevice='';
// 急停真的被按了几次。教学的最后一步要认的是急停，不是随便哪种关掉输出。
export const CAMERA_REQUEST_TIMEOUT=90000;
export let emergencyStops=0;
export let kernelEpoch=0;
export let inputStatus={};
export let outputConnected=false;
export function invalidateKernelRequests(){return ++kernelEpoch}

export function renderInputStatus(status){
  inputStatus=status||{};
  const modeSource=['computer','phone'].includes(status.audio_mode)?status.audio_mode:null;
  S.audioSource=status.audio_source||modeSource||S.audioSource||'computer';
  if(status.audio_device!==undefined)audioDevice=status.audio_device===null?'':String(status.audio_device);
  S.audioMode=status.audio_mode||'waiting';
  const audioSelect=$('#audioSource');
  if(audioSelect&&document.activeElement!==audioSelect)audioSelect.value=S.audioSource;
  const deviceSelect=$('#audioDevice');
  if(deviceSelect&&document.activeElement!==deviceSelect&&Array.from(deviceSelect.options).some(option=>option.value===audioDevice))deviceSelect.value=audioDevice;
  const deviceRow=$('#audioDeviceRow');if(deviceRow)deviceRow.hidden=S.audioSource!=='computer';
  // 麦克风好好的就不说话；没就绪才在那一组底下说一句。
  const audioStatus=$('#audioStatus');
  if(audioStatus){
    const voice=status.voice||{};
    const ok=S.audioMode===S.audioSource&&voice.connected;
    audioStatus.textContent=ok?'':(S.audioSource==='phone'?'等手机连上，用手机的麦克风':'电脑麦克风还没打开');
  }
  const connected=!!(status.mobile_pose_connected||status.handheld_connected);
  const mobile=$('#mobileStatus');
  mobile.textContent=connected?(status.mobile_pose_connected?'已连接':'手持端已连接'):'没连上';
  mobile.className='tag '+(connected?'ok':'warn');
  // 保留一个轻量入口，未选择手机时也能连接新设备。
  const phoneGroup=$('#phoneGroup');
  if(phoneGroup)phoneGroup.hidden=false;
  syncPhoneCode(status,($('#poseSource')?.value||sourceMode)==='phone'||S.audioSource==='phone');
  // 手机慢下来只有两种可能：模型退回了 CPU，或者这台机器就这么快。退回 CPU 才值得说。
  const phone=(status.mobile_pose_sources||[]).find(item=>item.active)||(status.mobile_pose_sources||[])[0];
  const perf=$('#phonePerf');
  if(perf)perf.textContent=phone?.delegate==='CPU'?'手机在用 CPU 识别，比 GPU 慢一倍':'';
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

export async function refreshInput(){
  const epoch=S.inputEpoch;
  try{const data=await api('/api/input/status?brief=1');if(epoch===S.inputEpoch)renderInputStatus(data);return true}
  catch{if(epoch===S.inputEpoch){renderInputStatus({});$('#mobileStatus').textContent='状态未知'}return false}
}

export async function updateInputConfig(path,payload){
  const epoch=++S.inputEpoch;
  const result=await post(path,payload);
  if(epoch!==S.inputEpoch)return;
  ++S.inputEpoch;renderInputStatus(result);return result;
}

function renderAudioDevices(devices,current){
  const select=$('#audioDevice');if(!select)return;
  const selected=current===null||current===undefined?'':String(current);
  const options=[new Option('系统默认设备','')];
  for(const item of (devices||[])){
    const suffix=item.supports_16k?'': '（不支持 16kHz）';
    const option=new Option(`${item.index} · ${item.name}${suffix}`,String(item.index));
    option.disabled=!item.supports_16k;
    options.push(option);
  }
  select.replaceChildren(...options);
  audioDevice=selected;
  select.value=selected;
  if(select.value!==selected)select.value='';
}

export async function refreshAudioDevices(){
  try{const data=await api('/api/audio/devices');renderAudioDevices(data.devices,data.audio_device);return true}catch{return false}
}

function renderPerformance(data){
  renderRecognitionPerformance(data);
  // 画面左上角只写一个数：每秒认几帧。毫秒、丢帧、CPU 这些排查问题才看，放在「实验与诊断」。
  // 手机那边不报推理帧率，用每秒收到几帧代替——人看到的就是这个。
  const fps=Number(data.inference_fps??data.network_fps);
  const live=sourceMode==='phone'?!!inputStatus.mobile_pose_connected:cameraRunning;
  const black=sourceMode==='computer'&&live&&data.picture_black;
  const hud=$('#fpsHud');
  if(hud){
    const show=black||(live&&Number.isFinite(fps)&&fps>0);hud.hidden=!show;
    if(show){const text=black?'画面全黑':`${Math.round(fps)} 帧/秒`;if(hud.textContent!==text)hud.textContent=text;hud.classList.toggle('slow',!!black||fps<15)}
  }
  const line=$('#recognitionStatus');
  if(line)line.textContent=S.sourceConnecting?'正在启动摄像头，请稍候…':black?'摄像头画面全黑':live?(Number.isFinite(fps)&&fps>0?`识别中 · ${Math.round(fps)} 帧/秒`:'识别中'):'未开始';
  if(line&&!S.sourceConnecting&&live&&sourceMode==='computer'){if(!black&&data.depth_active)line.textContent+=' · 深度已启用';else if(!black&&data.depth_error)line.textContent+=' · '+data.depth_error}
  $('#perfSummary').textContent='识别';
  $('#perfDetails').textContent=[
    `采集帧率：${formatPerf(data.capture_fps)} · 推理帧率：${formatPerf(data.inference_fps)}${data.network_fps!=null?` · 手机传来：${formatPerf(data.network_fps)}`:''}`,
    `总延迟：${formatPerf(data.total_latency_ms,' 毫秒')} · 推理平均：${formatPerf(data.inference_avg_ms,' 毫秒')}`,
    `预览：${data.preview_ready?'已就绪':'未就绪'} · 丢帧：${data.dropped_frames??0} · 本程序占 CPU：${formatPerf(data.process_cpu_percent,'%')}`,
  ].join('\n');
}

export async function refreshPerformance(){try{renderPerformance(await api('/api/performance'))}catch{}}

export let stereoState=null;

export function renderStereo(data){
  stereoState=data;
  const toggle=$('#stereoToggleBtn'),calibrate=$('#stereoCalibrateBtn');
  toggle.textContent=data.enabled?'关闭双目':'开启双目';
  const busy=data.state==='collecting'||data.state==='solving';
  calibrate.textContent=data.state==='collecting'?`取消标定（还剩 ${Math.ceil(data.remaining_s||0)} 秒）`:'标定（30 秒）';
  calibrate.disabled=!data.enabled||data.state==='solving';
  let status;
  if(!data.enabled)status=data.body_mode==='phone'?'未开启':'未开启：双目以手机为主画面，先把摄像头来源切到手机';
  else if(!data.pc_camera_running)status=data.pc_camera_error?'电脑摄像头打不开：'+data.pc_camera_error:'已开启：在「设备」里点「开始识别」，电脑摄像头会一起打开';
  else if(data.message&&(busy||data.state==='failed'||/标定/.test(data.message)))status=data.message;
  else if(!data.calibrated)status='还没有标定：站到平时玩的位置，点「标定」，然后活动双臂 30 秒';
  else status='运行中';
  $('#stereoStatus').textContent=status;
  const cal=data.calibration;
  $('#stereoQuality').textContent=cal
    ?`标定于 ${cal.calibrated_at||'—'} · 检验误差 ${cal.holdout_error_px} 像素 · 两台相机夹角 ${cal.optical_axis_angle_deg}°`:'';
  const latest=data.latest,hands=latest?.hands||{},missing=latest?.missing||{};
  const fmt=side=>hands[side]?`${hands[side].forward>=0?'+':''}${hands[side].forward.toFixed(2)}`
    :(missing[side]?`—（${missing[side]}）`:'—');
  const views=data.views||{};$('#stereoViews').hidden=$('#stereoLegend').hidden=!data.enabled;
  if(data.enabled){drawStereoView($('#stereoPcView'),views.pc,views.phone,data);drawStereoView($('#stereoPhoneView'),views.phone,views.pc,data)}
  $('#stereoReadout').textContent=data.enabled&&data.calibrated
    ?(latest?.valid?`手往前伸（肩宽为 1）：左 ${fmt('left')} · 右 ${fmt('right')}`:`等待双目数据：${latest?.reason||'两台相机都要看到你'}`):'';
}

// 两台相机各自看到的骨架。双目用到的关节按"两台都看到 / 只有这台看到"上色，
// 哪只手没读数、该挪哪台相机，一眼就知道。
function drawStereoView(el,view,other,data){
  const H=360,[w0,h0]=view?.size?.[0]&&view?.size?.[1]?view.size:[3,4],W=Math.round(H*w0/h0);
  if(el.width!==W||el.height!==H){el.width=W;el.height=H}
  const c=el.getContext('2d');c.fillStyle='#050608';c.fillRect(0,0,W,H);
  const fresh=v=>v&&v.age_ms<=1000,pts=view?.points||{};
  const blank=!fresh(view)?'没有画面':(Object.keys(pts).length?'':'看不到人');
  if(blank){c.fillStyle='#6f7d8c';c.font='26px sans-serif';c.textAlign='center';c.fillText(blank,W/2,H/2);return}
  c.strokeStyle='rgba(85,221,255,.5)';c.lineWidth=3;
  for(const[a,b]of EDGES){const p=pts[a],q=pts[b];if(!p||!q||p[2]<.3||q[2]<.3)continue;c.beginPath();c.moveTo(p[0]*W,p[1]*H);c.lineTo(q[0]*W,q[1]*H);c.stroke()}
  const min=data.min_score||.6,seen=p=>p&&p[2]>=min,otherPts=fresh(other)?other.points||{}:{};
  for(const name of data.joints||[]){
    const p=pts[name];if(!seen(p))continue;
    c.fillStyle=seen(otherPts[name])?'#4cc38a':'#e3a553';c.beginPath();c.arc(p[0]*W,p[1]*H,8,0,Math.PI*2);c.fill();
  }
}

export async function refreshStereo(){try{renderStereo(await api('/api/stereo'))}catch{}}

export async function refreshCameraConfig(){try{const data=await api('/api/camera/config');const select=$('#cameraBackend');if(select&&data.preference)select.value=data.preference;renderCameraRotation(data);renderCameraDevices(null,data.camera_index,data);renderCameraDepth(data);return true}catch{return false}}

const ROTATION_LABELS={none:'不旋转',cw:'顺时针 90°',ccw:'逆时针 90°','180':'180°'};

// 自动模式下把此刻实际转的方向写在选项里，免得人以为"自动"什么都没做。
export function renderCameraRotation(data){
  const select=$('#cameraRotation');if(!select||!data?.rotation)return;
  select.value=data.rotation;
  const auto=select.querySelector('option[value="auto"]');
  if(auto)auto.textContent=data.rotation==='auto'&&data.applied_rotation&&data.applied_rotation!=='none'
    ?`自动转正（当前${ROTATION_LABELS[data.applied_rotation]}）`:'自动转正';
}

// 开机不扫。挨个序号去开摄像头要好几秒，而绝大多数人只有一个，不该为了那个
// 下拉框每次启动都等一遍。所以先只把"现在用的是第几个"摆出来，真要换的人点
// 一下扫描，列表才填满。
let cameraDevices=[];
export function renderCameraDevices(devices,current,data={}){
  const select=$('#cameraDevice');if(!select)return;
  if(current!==undefined&&current!==null)S.cameraIndex=Number(current);
  S.cameraDevice=String(data.camera_device??S.cameraDevice??S.cameraIndex);
  if(Array.isArray(devices))cameraDevices=[...devices];
  const list=[...cameraDevices];
  if(!list.some(d=>String(d.id??d.index)===S.cameraDevice))
    list.unshift(S.cameraDevice.startsWith('kinect2:')?{id:S.cameraDevice,name:'微软 Kinect'}:{index:S.cameraIndex,name:data.camera_name,picture_state:data.picture_black?'black':undefined});
  select.replaceChildren(...list.map(d=>{
    const option=document.createElement('option');option.value=String(d.id??d.index);
    const size=d.width&&d.height?' · '+d.width+'×'+d.height:'';
    const name=d.name||'摄像头 '+d.index;
    const state=d.picture_state==='black'?' · 画面全黑'
      :['unavailable','no_frames'].includes(d.picture_state)?' · 暂无画面':'';
    option.textContent=name+(d.virtual&&!name.includes('虚拟')?'（虚拟）':'')+size+state;return option;
  }));
  select.value=S.cameraDevice;
}

export function renderCameraDepth(data){
  if(data?.depth_supported!==undefined)S.cameraDepthSupported=!!data.depth_supported;
  if(data?.depth_enabled!==undefined)S.cameraDepthEnabled=!!data.depth_enabled;
  const box=$('#cameraDepth');if(box)box.checked=S.cameraDepthEnabled!==false;
  syncCameraDeviceRow();
}

export function syncCameraDeviceRow(){
  const computer=($('#poseSource')?.value||'computer')==='computer';
  for(const id of ['cameraDeviceRow','cameraScanRow','cameraRotationRow']){const el=$('#'+id);if(el)el.hidden=!computer}
  const row=$('#cameraDepthRow');if(row)row.hidden=!(computer&&S.cameraDepthSupported);
}

function setCameraConnecting(connecting){
  S.sourceConnecting=connecting;
  for(const id of ['poseSource','cameraDevice','cameraDepth','sourceStartBtn','cameraScanBtn']){
    const el=$('#'+id);if(el)el.disabled=connecting;
  }
  if(connecting)$('#recognitionStatus').textContent='正在启动摄像头，请稍候…';
  renderMainStatus();
}

export async function selectCamera(device){
  const resume=output.enabled;
  await setOutput(false);
  const epoch=++kernelEpoch;
  ++S.inputEpoch;setCameraConnecting(true);
  try{
    const result=await post('/api/camera/config',{device},CAMERA_REQUEST_TIMEOUT);
    if(epoch!==kernelEpoch)throw new Error('相机切换已被停止');
    if(!result.camera?.running)throw new Error(result.camera?.last_error||'摄像头启动失败');
    S.desiredSource='computer';
    renderCameraDevices(null,result.camera_index,result);renderCameraDepth(result);
    renderKernelState(result);await refreshInput();
    if(epoch!==kernelEpoch)throw new Error('相机切换已被停止');
    if(resume)await setOutput(true);
    notice('摄像头已切换，识别已开始');
  }finally{
    setCameraConnecting(false);await refreshCameraConfig();await refreshPerformance();
  }
}

function renderXinputStatus(s=output.xinputStatus){if(document.activeElement?.closest('#outputSettings'))return;const select=$('#xinputMerge'),line=$('#xinputStatus');if(!select||!line)return;const users=Array.isArray(s?.connected_users)?s.connected_users:[];const current=s?.enabled&&s?.selected_user!==null&&s?.selected_user!==undefined?String(s.selected_user):'';const values=[['','关闭']];for(const user of users)values.push([String(user),`手柄 ${Number(user)+1}`]);if(current&&!values.some(([v])=>v===current))values.push([current,`手柄 ${Number(current)+1}（未连接）`]);const keep=current&&values.some(([v])=>v===current);select.replaceChildren(...values.map(([value,label])=>{const o=document.createElement('option');o.value=value;o.textContent=label;return o}));select.value=keep?current:(s?.enabled?'':'');output.xinputEnabled=!!s?.enabled;output.xinputUser=s?.selected_user??null;output.xinputMotionLeft=!!s?.motion_left_enabled;renderXinputMotionLeft();line.textContent=!s?.enabled?'':(s?.connected?`已合流 · 手柄 ${Number(s.active_user??s.selected_user)+1}`:'等手柄连上');if(s?.last_error)line.textContent+=(line.textContent?' · ':'')+s.last_error;line.className='sub'+(s?.enabled&&!s?.connected?' warn':'')}

function renderXinputMotionLeft(){const box=$('#xinputMotionLeft');if(box){box.checked=output.xinputMotionLeft;box.disabled=!output.xinputEnabled||output.mode!=='gamepad'}const row=$('#xinputMotionLeftRow');if(row)row.hidden=!output.xinputEnabled}

export async function setXinputMotionLeft(){try{await updateOutputConfig({motion_left_enabled:!!$('#xinputMotionLeft')?.checked},'/api/output/xinput');await refreshXinput();notice(output.xinputMotionLeft?'体感左摇杆合成已开启，双方输入相加':'已关闭体感左摇杆合成')}catch(e){notice('左摇杆合成设置失败：'+(e?.message||e));await refreshXinput()}}

export async function refreshXinput(){const epoch=S.outputEpoch;try{const data=await api('/api/output/xinput');if(epoch!==S.outputEpoch)return;output.xinputStatus=data;renderXinputStatus(data)}catch{}}

export async function setXinputMerge(){const select=$('#xinputMerge');const value=select?.value||'';try{await updateOutputConfig({enabled:!!value,user:value===''?null:Number(value)},'/api/output/xinput');await refreshXinput();notice(value?`已选择物理手柄 ${Number(value)+1}；${output.xinputMotionLeft?'体感按键与左摇杆合成已开启':'体感只叠加手柄按键'}`:'已关闭物理手柄合流')}catch(e){notice('物理手柄合流失败：'+(e?.message||e));await refreshXinput()}}

export function renderOutput(s=output.server){
  if(!s)return;
  outputConnected=true;
  output.server=s;output.enabled=!!s.enabled;output.mode=s.mode||output.mode;
  if(document.activeElement!==$('#outputMode'))$('#outputMode').value=output.mode;
  if(document.activeElement!==$('#strength')){
    const gain=output.mode==='gamepad'?s.gamepad_gain:Number(s.mouse_speed_x)/600;
    if(Number.isFinite(gain)&&gain>0)$('#strength').value=Math.round(gain*100);
  }
  $('#strengthValue').textContent=$('#strength').value+'%';
  // 一切正常就不说；鼠标用不了、手柄没接上、语音还按着键，才在「游戏输出」底下点名。
  const problems=[];
  if(voiceLatchText(s))problems.push(voiceLatchText(s)+'，说松开才会放');
  if(s.mouse_available===false)problems.push('鼠标输出用不了');
  if(output.mode==='gamepad'&&!s.gamepad_connected)problems.push('虚拟手柄没接上');
  if(s.last_error)problems.push(s.last_error);
  $('#outputStatus').textContent=problems.join(' · ');
  if(s.xinput_merge_enabled!==undefined){
    output.xinputEnabled=!!s.xinput_merge_enabled;output.xinputUser=s.xinput_selected_user??null;
    output.xinputMotionLeft=!!s.xinput_motion_left_enabled;renderXinputMotionLeft();
  }
  renderMainStatus();
}

// 一台电脑上只能有一套"当前输入设备"。体感这边一半出鼠标、一半出 Xbox，游戏
// 就会在两种按键提示之间来回跳；而全都出 Xbox 的时候，右摇杆推不动桌面鼠标，
// 握拳看上去像是坏了。两种都不报错，所以要说一声——但只在人改的那一下说：
// 握拳加一堆手柄键的时候两个选项各有代价，原来挂一条常驻的黄条，怎么选都消不掉。
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

/** 改完视角输出或握拳以后提醒一次；没有要说的就不说。 */
export function noteOutputMix(){
  const handMouseOn=!!handMouseConfig.enabled&&(['left','right'].includes(handMouseConfig.horizontal_hand)||['left','right'].includes(handMouseConfig.vertical_hand));
  const pads=gamepadBindingCount();
  if(output.mode==='gamepad'&&handMouseOn)notice('视角走 Xbox 右摇杆：握拳推的是右摇杆，桌面上的鼠标不会动。');
  else if(output.mode==='mouse'&&pads>0)notice(`有 ${pads} 个动作是手柄键，视角走鼠标时，游戏的按键提示可能在键鼠和手柄之间来回切换。`);
}

export async function refreshOutput(){
  const epoch=S.outputEpoch;
  try{const data=await api('/api/output-status');if(epoch===S.outputEpoch)renderOutput(data);return true}
  catch{if(epoch===S.outputEpoch){outputConnected=false;renderMainStatus()}return false}
}

export async function updateOutputConfig(payload,path='/api/output/config'){
  const epoch=++S.outputEpoch;
  const result=await post(path,payload);
  if(epoch!==S.outputEpoch)throw new Error('操作已被紧急停止中断');
  ++S.outputEpoch;renderOutput(result);return result;
}

export async function setOutput(enabled){
  if(enabled&&zoneEditMode)throw new Error('请先保存或取消区域调整');
  const epoch=++S.outputEpoch;
  const result=await post('/api/output/config',{enabled});
  if(epoch!==S.outputEpoch){
    if(enabled)await emergencyStop();
    throw new Error('操作已被紧急停止中断');
  }
  ++S.outputEpoch;
  renderOutput(result);
  if(result.enabled!==enabled)throw new Error(enabled?'服务未确认开启控制':'尚未确认停止');
}

export async function emergencyStop(){
  ++S.outputEpoch;++kernelEpoch;++S.inputEpoch;++emergencyStops;
  try{
    const result=await post('/api/input/stop',{},CAMERA_REQUEST_TIMEOUT);
    if(result.output?.enabled!==false)throw new Error('服务尚未确认');
    ++S.outputEpoch;renderOutput(result.output);renderKernelState(result);
    await refreshInput();await refreshPerformance();
    notice('识别已停止，所有体感输出已松开');
  }catch(error){
    notice('还没确认停下：'+error.message+'。再按一次 F9');
  }
}

export async function setSource(source,enabled=true){
  await setOutput(false);
  const epoch=++kernelEpoch;
  ++S.inputEpoch;
  const selectedAudio=$('#audioSource')?.value||S.audioSource||'computer';
  let result;
  setCameraConnecting(enabled);
  try{
    result=await post('/api/input/source',{source,enabled,audio_source:selectedAudio},CAMERA_REQUEST_TIMEOUT);
    if(epoch!==kernelEpoch)throw new Error('操作已被停止');
  }finally{setCameraConnecting(false)}
  if(epoch!==kernelEpoch)throw new Error('操作已被停止');
  if(enabled&&source==='computer'&&!result.camera?.running){
    // 没有摄像头的电脑在这里是死路：报一句"无法打开"然后没有下文。所以失败时
    // 直接把另外两条出路说出来——换一个摄像头，或者改用手机。
    throw new Error((result.camera?.last_error||'摄像头启动失败')
      +'。没有摄像头就把来源改成手机；有好几个就点「扫描」换一个。');
  }
  ++kernelEpoch;++S.inputEpoch;S.desiredSource=source;
  renderKernelState(result);await refreshInput();
  notice(enabled?(source==='phone'?'已切到手机摄像头，等手机连上':'摄像头连上了'):'识别已停止');
}
