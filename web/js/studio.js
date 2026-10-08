// 摄像头只取后端共享采集的处理结果；浏览器仅负责用户选择的屏幕、声音和合成编码。
import {readStudioFrame} from './studio-frame.js';
import {compose,FULL_CROP} from './studio-compose.js';
import {cropEditor} from './studio-crop.js';
const root=document.querySelector('#studioRoot');
const defaults={source:'computer',background:'original',face:'original',layout:'landscape',position:'bottom-right',size:30,capture:'window',system_audio:true,microphone:false,mirror:true,avatar_preset:'',screen_crop:FULL_CROP,portrait_crop:FULL_CROP};
const $=id=>document.getElementById(id);
const clientId=globalThis.crypto?.randomUUID?.()||String(Date.now())+'-'+Math.random();
let config={...defaults},loaded=false,preview=false,starting=false,recording=false,saving=false;
let frame=null,frameAt=0,frameEpoch=0,frameBusy=false,frameBlocked=0,frameTimer=0,stateTimer=0,renewAt=0;
let screenStream=null,micStream=null,canvasStream=null,audioContext=null,audioSources=[];
let screenVideo=null,recorder=null,recordingId=null,uploadTail=Promise.resolve(),pendingBytes=0,uploadError='',startedAt=0,drawTimer=0;
let saveTail=Promise.resolve(),liveState={},configSaveError='';

async function request(path,body,options={}){
  const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?undefined:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store',signal:AbortSignal.timeout(10000),...options});
  let data;
  try{data=await response.json()}catch{data={}}
  if(!response.ok||data.ok===false)throw new Error(data.error||data.detail||'本地服务未完成操作，请重试');
  return data;
}

function status(text){if($('studioStatus'))$('studioStatus').textContent=text}
function errorText(error){
  if(error?.name==='NotAllowedError'||error?.name==='AbortError')return '已取消或未获允许，没有开始录制';
  if(error?.name==='NotReadableError')return '无法读取所选画面，请检查窗口是否已关闭或被其他程序限制';
  if(error?.name==='NotFoundError')return '未找到可用的麦克风或屏幕';
  if(error?.name==='NotSupportedError')return '当前窗口不支持录制，请在电脑浏览器中打开录制页';
  if(error?.name==='InvalidStateError')return '请保持录制页在前台，重新点击选择画面';
  if(error?.name==='SecurityError')return '当前浏览器不允许读取画面，请在本机电脑浏览器打开录制页';
  if(error?.name==='TimeoutError')return '本地服务响应超时，请检查程序仍在运行';
  if(error?.name==='TypeError'&&/fetch|network|load/i.test(error.message||''))return '连接本地服务失败，请检查程序仍在运行';
  return error?.message||'操作失败，请重试';
}

function forgetFrame(){frameEpoch++;frame?.close();frame=null;frameAt=0;draw()}
function renderControls(){
  const fields={studioSource:'source',studioBackground:'background',studioFace:'face',studioLayout:'layout',studioPosition:'position',studioCapture:'capture',studioSize:'size'};
  for(const [id,key] of Object.entries(fields))$(id).value=String(config[key]??defaults[key]);
  $('studioSystemAudio').checked=!!config.system_audio;
  $('studioMicrophone').checked=!!config.microphone;
  $('studioSizeValue').textContent=config.size+'%';
  $('studioAvatarRow').hidden=config.face!=='avatar';
  $('studioAvatarStatus').textContent=config.avatar_data_url?'已保存头像':'尚未选择头像；暂用遮盖保护人脸';
  for(const button of root.querySelectorAll('[data-avatar-preset]'))button.setAttribute('aria-pressed',String(button.dataset.avatarPreset===config.avatar_preset));
  const canvas=$('studioCanvas'),portrait=config.layout==='portrait';
  canvas.width=portrait?720:1280;canvas.height=portrait?1280:720;
  $('studioPreviewBox').classList.toggle('portrait',portrait);
  draw();renderButtons();
}

function renderButtons(){
  const busy=starting||recording||saving;
  $('studioPreviewBtn').textContent=preview?'关闭人物预览':'开启人物预览';
  $('studioPreviewBtn').disabled=!loaded||starting||saving||recording;
  $('studioStartBtn').hidden=recording||saving;
  $('studioStartBtn').disabled=!loaded||starting||!screenCaptureSupported();
  $('studioStartBtn').textContent=screenStream?'开始录制':'选择画面并录制';
  $('studioScreenBtn').disabled=!loaded||busy||!screenCaptureSupported();
  $('studioScreenBtn').textContent=screenStream?'重新选择画面':'选择画面预览';
  $('studioReleaseScreenBtn').hidden=!screenStream||busy;
  $('studioCropBtn').disabled=!loaded||starting||saving||(!screenStream&&!frame);
  $('studioStopBtn').hidden=!recording&&!saving;
  $('studioStopBtn').disabled=saving;
  $('studioStopBtn').textContent=saving?'正在保存…':'停止并保存';
  $('studioRecordingBadge').hidden=!recording;
  for(const id of ['studioLayout','studioCapture','studioSystemAudio','studioMicrophone'])$(id).disabled=!loaded||busy;
  $('studioSystemAudio').disabled=!loaded||busy||!!screenStream;
  for(const id of ['studioSource','studioBackground','studioFace','studioPosition','studioSize','studioAvatar'])$(id).disabled=!loaded||starting||saving;
  for(const button of root.querySelectorAll('[data-avatar-preset]'))button.disabled=!loaded||starting||saving;
}

function screenCaptureSupported(){return !!navigator.mediaDevices?.getDisplayMedia&&typeof MediaRecorder!=='undefined'&&!!HTMLCanvasElement.prototype.captureStream}

function persist(patch){
  config={...config,...patch};frameBlocked++;forgetFrame();renderControls();
  const snapshot={...patch};
  const result=saveTail.then(()=>request('/api/studio/config',snapshot));
  saveTail=result.catch(error=>{status('设置未保存：'+errorText(error));throw error});
  // 保存失败不能让之后的设置一直卡住；本次帧继续隐藏，直到下次成功保存。
  saveTail=saveTail.catch(()=>{});
  result.then(()=>{configSaveError='';frameBlocked--;if(!frameBlocked)void getFrame()},error=>{configSaveError=errorText(error);frameBlocked--;forgetFrame();preview=false;void demand(false).catch(()=>{});if(recording){uploadError='人物设置保存失败，已停止录制';stopRecording()}renderButtons()});
  return result;
}

async function demand(enabled){
  await request('/api/studio/video-demand',{enabled,client_id:clientId});
  renewAt=Date.now();
}

async function togglePreview(){
  if(starting||recording||saving)return;
  $('studioPreviewBtn').disabled=true;
  try{
    await saveTail;if(configSaveError)throw new Error('请先重新保存人物设置：'+configSaveError);await demand(!preview);preview=!preview;
    if(!preview)forgetFrame();
    status(preview?'人物预览已开启；选择画面后才会录制':'人物预览已关闭');
    if(preview)void getFrame();
  }catch(error){status(errorText(error))}
  finally{renderButtons()}
}

async function getFrame(){
  if(frameBusy||frameBlocked||(!preview&&!recording))return;
  frameBusy=true;const epoch=frameEpoch;
  try{
    const bitmap=await readStudioFrame();
    if(!bitmap){if(epoch===frameEpoch)forgetFrame();return}
    if(epoch!==frameEpoch||frameBlocked||(!preview&&!recording)){bitmap.close();return}
    frame?.close();frame=bitmap;frameAt=Date.now();draw();
  }catch{if(epoch===frameEpoch)forgetFrame()}
  finally{frameBusy=false}
}

function draw(){
  const canvas=$('studioCanvas');if(!canvas)return;
  const ctx=canvas.getContext('2d'),w=canvas.width,h=canvas.height;
  const usableFrame=frame&&Date.now()-frameAt<1500&&!frameBlocked;
  compose(ctx,screenVideo?.readyState>=2?screenVideo:null,usableFrame?frame:null,w,h,config);
  $('studioViewLabel').textContent=screenStream?'合成预览':usableFrame?'人物预览':'合成预览';
  $('studioCropBtn').disabled=!loaded||starting||saving||(!screenStream&&!usableFrame);
  $('studioPlaceholder').hidden=!!screenStream||!!usableFrame;
  if(recording){
    const seconds=Math.floor((Date.now()-startedAt)/1000);
    $('studioElapsed').textContent=String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');
  }
}

function stopTracks(stream){stream?.getTracks().forEach(track=>track.stop())}
function releaseRecordingSources(){
  stopTracks(screenStream);stopTracks(micStream);stopTracks(canvasStream);
  screenStream=micStream=canvasStream=null;
  audioSources.forEach(source=>{try{source.disconnect()}catch{}});audioSources=[];
  if(audioContext){void audioContext.close().catch(()=>{});audioContext=null}
  if(screenVideo){screenVideo.pause();screenVideo.srcObject=null;screenVideo=null}
  clearInterval(drawTimer);drawTimer=0;draw();
}

async function acquireScreen(){
  // 浏览器要求在用户点击中直接发起选屏；只能共享用户选中的来源。
  const selected=await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:config.capture,frameRate:30},audio:!!config.system_audio,systemAudio:config.system_audio?'include':'exclude',windowAudio:config.system_audio?'system':'exclude',selfBrowserSurface:'exclude'});
  const videoTrack=selected.getVideoTracks()[0],video=document.createElement('video');
  try{
    if(!videoTrack)throw new Error('没有取得所选画面');
    video.muted=true;video.playsInline=true;video.srcObject=selected;await video.play();
    if(videoTrack.readyState==='ended')throw new Error('屏幕共享已结束，请重新选择');
  }catch(error){stopTracks(selected);video.srcObject=null;throw error}
  const previous=screenStream;
  if(screenVideo){screenVideo.pause();screenVideo.srcObject=null}
  screenStream=selected;screenVideo=video;stopTracks(previous);
  videoTrack.addEventListener('ended',()=>{
    if(screenStream!==selected)return;
    if(recording)stopRecording();
    else{releaseRecordingSources();renderButtons();status('所选画面已停止共享')}
  });
  clearInterval(drawTimer);drawTimer=setInterval(draw,1000/30);
  renderAudioStatus();draw();
  return videoTrack;
}

async function selectScreenPreview(){
  if(starting||recording||saving)return;
  starting=true;renderButtons();status('请选择要预览的窗口或屏幕…');
  const previous=screenStream;
  try{
    await acquireScreen();
    await saveTail;if(configSaveError)throw new Error('请先重新保存人物设置：'+configSaveError);
    await demand(true);preview=true;void getFrame();
    status('正在预览实际合成画面，尚未录制；可先裁切和调整人物位置');
  }catch(error){if(screenStream!==previous)releaseRecordingSources();status(errorText(error))}
  finally{starting=false;renderButtons()}
}

function pickMime(){
  for(const mime of ['video/webm;codecs=vp8,opus','video/webm;codecs=vp8','video/webm'])if(MediaRecorder.isTypeSupported(mime))return mime;
  throw new Error('当前浏览器不支持视频录制，请使用最新版电脑浏览器');
}

function renderAudioStatus(){
  const systemLive=!!config.system_audio&&screenStream?.getAudioTracks().some(track=>track.readyState==='live');
  const micLive=!!config.microphone&&micStream?.getAudioTracks().some(track=>track.readyState==='live');
  $('studioAudioStatus').textContent='电脑声音：'+(systemLive?'已收到':config.system_audio?'未收到，请在选取画面时勾选共享声音':'已关闭')+' · 麦克风：'+(micLive?'已收到':config.microphone?'未收到或已断开':'已关闭');
}

function enqueueChunk(blob){
  if(!blob?.size||!recordingId||uploadError)return;
  // 定时切片可能受系统休眠影响变大。拆分网络块，服务器仍顺序拼成同一视频。
  if(blob.size>16*1024*1024){for(let offset=0;offset<blob.size;offset+=8*1024*1024)enqueueChunk(blob.slice(offset,offset+8*1024*1024));return}
  pendingBytes+=blob.size;
  if(pendingBytes>16*1024*1024){uploadError='保存速度跟不上录制，已停止并保留收到的视频片段';pendingBytes-=blob.size;stopRecording();return}
  const id=recordingId;
  uploadTail=uploadTail.then(async()=>{
    if(uploadError&&uploadError!=='保存速度跟不上录制，已停止并保留收到的视频片段')return;
    const response=await fetch('/api/studio/recording/chunk?id='+encodeURIComponent(id),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:blob,signal:AbortSignal.timeout(15000)});
    if(!response.ok)throw new Error('视频片段未能保存');
  }).catch(error=>{uploadError=errorText(error);stopRecording()}).finally(()=>{pendingBytes-=blob.size});
}

async function finishRecording(){
  if(saving)return;
  recording=false;saving=true;renderButtons();releaseRecordingSources();status('正在保存已经收到的视频…');
  try{
    await uploadTail;
    const result=await request('/api/studio/recording/finish',{id:recordingId,interrupted:!!uploadError});
    $('studioSavedFile').textContent=result.path||result.file_name||'录制文件已保存';
    status(uploadError?'录制中断，已保存收到的视频：'+uploadError:'录制完成，已保存到本机');
  }catch(error){status('保存未完成：'+errorText(error)+'。已写入的文件仍保留在输出文件夹。')}
  finally{
    recorder=null;recordingId=null;saving=false;pendingBytes=0;
    if(!preview){forgetFrame();void demand(false).catch(()=>{})}
    renderButtons();
  }
}

function stopRecording(){
  if(recorder&&recorder.state!=='inactive'){recorder.stop();return}
  if(recordingId&&!saving)void finishRecording();
}

async function startRecording(){
  if(starting||recording||saving)return;
  if(!screenCaptureSupported()){status('请在浏览器中打开录制页');return}
  starting=true;renderButtons();status('请选择要录制的屏幕或游戏窗口…');
  uploadError='';pendingBytes=0;uploadTail=Promise.resolve();
  try{
    if(!screenStream)await acquireScreen();
    const videoTrack=screenStream.getVideoTracks()[0];
    if(!videoTrack)throw new Error('没有取得屏幕画面');
    if(config.microphone)micStream=await navigator.mediaDevices.getUserMedia({video:false,audio:{echoCancellation:true,noiseSuppression:true}});
    await saveTail;if(configSaveError)throw new Error('请先重新保存人物设置：'+configSaveError);await demand(true);
    if(videoTrack.readyState==='ended')throw new Error('屏幕共享已结束，没有开始录制');
    draw();canvasStream=$('studioCanvas').captureStream(30);
    const audioTracks=[];
    if(config.system_audio)audioTracks.push(...screenStream.getAudioTracks());
    if(config.microphone)audioTracks.push(...(micStream?.getAudioTracks()||[]));
    if(audioTracks.length){
      const Audio=window.AudioContext||window.webkitAudioContext;
      if(!Audio)throw new Error('当前浏览器无法混合录制声音');
      audioContext=new Audio();await audioContext.resume();
      const destination=audioContext.createMediaStreamDestination();
      for(const track of audioTracks){const source=audioContext.createMediaStreamSource(new MediaStream([track]));source.connect(destination);audioSources.push(source)}
      destination.stream.getAudioTracks().forEach(track=>canvasStream.addTrack(track));
    }
    renderAudioStatus();
    audioTracks.forEach(track=>track.addEventListener('ended',renderAudioStatus));
    const mime=pickMime();
    recorder=new MediaRecorder(canvasStream,{mimeType:mime,videoBitsPerSecond:4500000,audioBitsPerSecond:128000});
    const result=await request('/api/studio/recording/start',{mime_type:recorder.mimeType,layout:config.layout,width:$('studioCanvas').width,height:$('studioCanvas').height});
    recordingId=result.id;
    if(!recordingId)throw new Error('未能创建录制文件');
    if(videoTrack.readyState==='ended')throw new Error('屏幕共享已结束，没有开始录制');
    recorder.addEventListener('dataavailable',event=>enqueueChunk(event.data));
    recorder.addEventListener('stop',()=>void finishRecording());
    recorder.addEventListener('error',()=>{uploadError='视频编码失败';stopRecording()});
    recorder.start(1000);recording=true;startedAt=Date.now();
    clearInterval(drawTimer);drawTimer=setInterval(draw,1000/30);void getFrame();
    status('正在录制。停止后自动保存，体感控制可以继续使用。');
  }catch(error){
    releaseRecordingSources();
    if(recordingId){try{await request('/api/studio/recording/finish',{id:recordingId,interrupted:true})}catch{}recordingId=null}
    recorder=null;recording=false;if(!preview)void demand(false).catch(()=>{});
    if(error?.name==='NotSupportedError'||error?.name==='InvalidStateError')$('studioBrowserHint').hidden=false;
    status(errorText(error));
  }finally{starting=false;renderButtons()}
}

async function chooseAvatar(event){
  const file=event.target.files?.[0];if(!file)return;
  try{
    if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw new Error('请选择普通图片文件');
    if(file.size>8*1024*1024)throw new Error('头像图片请小于 8 兆字节');
    const bitmap=await createImageBitmap(file),canvas=document.createElement('canvas');
    const scale=Math.min(1,512/Math.max(bitmap.width,bitmap.height));canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale));
    canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();
    await persist({avatar_data_url:canvas.toDataURL('image/png'),avatar_preset:'',face:'avatar'});
    $('studioAvatarStatus').textContent='头像已保存';
  }catch(error){$('studioAvatarStatus').textContent=errorText(error)}
}

async function choosePreset(name){
  if(!['cat','fox','robot'].includes(name))return;
  try{
    const image=new Image();image.src='/assets/studio-'+name+'.svg';await image.decode();
    const canvas=document.createElement('canvas');canvas.width=canvas.height=320;
    canvas.getContext('2d').drawImage(image,0,0,320,320);
    await persist({avatar_data_url:canvas.toDataURL('image/png'),avatar_preset:name,face:'avatar'});
    $('studioAvatarStatus').textContent='卡通头像已保存，会跟随人脸';
  }catch(error){status(errorText(error))}
}

async function refreshState(){
  try{
    liveState=await request('/api/studio/state');
    if(!loaded){config={...defaults,...(liveState.config||{})};loaded=true;renderControls();status('未开启；不会自动录制')}
    else if(!frameBlocked&&config.mirror!==liveState.config?.mirror&&typeof liveState.config?.mirror==='boolean'){
      config.mirror=liveState.config.mirror;forgetFrame();
    }
    if(preview||recording){
      $('studioPortraitStatus').textContent=liveState.message||liveState.status_message||(!frame?'等待处理后的人物画面，请先连接所选体感摄像头':'');
      if(Date.now()-renewAt>10000)await demand(true);
    }else $('studioPortraitStatus').textContent='';
  }catch(error){if(!loaded)status('录制设置暂未读取：'+errorText(error))}
  finally{stateTimer=setTimeout(refreshState,1200)}
}

async function mount(){
  if(!root)return;
  try{
    const response=await fetch('/studio-panel.html',{cache:'no-store'});
    if(!response.ok)throw new Error('录制页面未能加载');
    root.innerHTML=await response.text();
    const supported=screenCaptureSupported();$('studioBrowserHint').hidden=supported;
    $('studioBrowserAltBtn').hidden=location.pathname==='/studio.html';
    const overlayUrl=new URL('/studio-overlay.html',location.href).href;
    $('studioOverlayUrl').value=overlayUrl;$('studioOverlayLink').href=overlayUrl;
    $('studioPreviewBtn').addEventListener('click',()=>void togglePreview());
    $('studioScreenBtn').addEventListener('click',()=>void selectScreenPreview());
    $('studioReleaseScreenBtn').addEventListener('click',()=>{releaseRecordingSources();renderButtons();status('画面预览已结束')});
    $('studioStartBtn').addEventListener('click',()=>void startRecording());
    $('studioStopBtn').addEventListener('click',stopRecording);
    for(const [id,key] of Object.entries({studioSource:'source',studioBackground:'background',studioFace:'face',studioLayout:'layout',studioPosition:'position',studioCapture:'capture',studioSize:'size'})){
      $(id).addEventListener('change',event=>void persist({[key]:key==='size'?Number(event.target.value):event.target.value}).catch(()=>{}));
    }
    $('studioSize').addEventListener('input',event=>{$('studioSizeValue').textContent=event.target.value+'%';config.size=Number(event.target.value);draw()});
    for(const [id,key] of [['studioSystemAudio','system_audio'],['studioMicrophone','microphone']])$(id).addEventListener('change',event=>void persist({[key]:event.target.checked}).catch(()=>{}));
    $('studioAvatar').addEventListener('change',event=>void chooseAvatar(event));
    for(const button of root.querySelectorAll('[data-avatar-preset]'))button.addEventListener('click',()=>void choosePreset(button.dataset.avatarPreset));
    cropEditor({source:kind=>kind==='screen'?(screenVideo?.readyState>=2?screenVideo:null):frame,config:()=>config,persist,status});
    $('studioOpenBrowserBtn').addEventListener('click',()=>void request('/api/studio/open-browser',{}).catch(error=>status(errorText(error))));
    $('studioBrowserAltBtn').addEventListener('click',()=>void request('/api/studio/open-browser',{}).catch(error=>status(errorText(error))));
    $('studioOpenFolderBtn').addEventListener('click',()=>void request('/api/studio/recording/open-folder',{}).catch(error=>status(errorText(error))));
    $('studioCopyOverlayBtn').addEventListener('click',async()=>{
      try{await navigator.clipboard.writeText(overlayUrl);status('直播人物画面地址已复制')}
      catch{$('studioOverlayUrl').select();status('请复制已选中的直播人物画面地址')}
    });
    renderControls();void refreshState();
    frameTimer=setInterval(()=>{if(frame&&Date.now()-frameAt>=1500)forgetFrame();void getFrame()},100);
    window.addEventListener('pagehide',()=>{
      clearTimeout(stateTimer);clearInterval(frameTimer);clearInterval(drawTimer);
      if(recorder?.state!=='inactive')try{recorder?.stop()}catch{}
      releaseRecordingSources();forgetFrame();
      // 浏览器退出时由后端保留已经写入的视频，尽力结束并释放此页人物请求。
      if(recordingId)navigator.sendBeacon?.('/api/studio/recording/finish',new Blob([JSON.stringify({id:recordingId,interrupted:true})],{type:'application/json'}));
      navigator.sendBeacon?.('/api/studio/video-demand',new Blob([JSON.stringify({enabled:false,client_id:clientId})],{type:'application/json'}));
    });
  }catch(error){root.textContent=errorText(error)}
}

void mount();
