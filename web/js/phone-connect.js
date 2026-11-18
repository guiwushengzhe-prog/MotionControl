import {$,api,notice,post,setProperty,setText} from './core.js';
import {setSource} from './devices.js';
import {runAction} from './shell.js';

const panel=$('#phoneConnectPanel'),image=$('#phoneConnectImage');
const cameraButton=$('#phoneUseCameraBtn'),firewallButton=$('#phoneFirewallRetry');
const knownKey='motioncontrol-phone-connected',offeredKey='motioncontrol-phone-code-offered';
let lastStatus={},addressKey='',request=0;
let loadingCode=false,codeError='',switchingCamera=false,cameraError='',firewallError='';
let offerCamera=true;

function remembered(key){try{return localStorage.getItem(key)==='1'}catch{return false}}
function remember(key){try{localStorage.setItem(key,'1')}catch{}}
export function phoneCameraConnected(status){return !!(status.mobile_pose_connected||status.phone_ignored)}
function anyPhoneConnected(status){
  return phoneCameraConnected(status)||!!(status.handheld_connected||status.mobile_voice_connected);
}

function renderPhoneConnection(){
  if(!panel.open)return;
  const status=lastStatus;
  const cameraConnected=phoneCameraConnected(status);
  const connected=anyPhoneConnected(status);
  const usingPhone=status.body_mode==='phone'&&status.body_enabled!==false;
  let message='等待手机扫码';
  if(cameraConnected)message='手机摄像头已连接';
  else if(status.handheld_connected)message='手机手持输入已连接';
  else if(status.mobile_voice_connected)message='手机麦克风已连接';
  else if(status.unpaired_phone)message='手机还没配对，请扫码';
  else if(usingPhone)message='等待手机摄像头连接';
  if(switchingCamera)message='正在切换摄像头…';
  if(loadingCode&&!connected&&!switchingCamera)message='正在生成二维码…';
  setText($('#phoneConnectStatus'),cameraError||codeError||message);
  setText($('#closePhoneConnectBtn'),connected?'完成':'关闭');
  setProperty(cameraButton,'hidden',!offerCamera||(usingPhone&&!switchingCamera&&!cameraError));
  setProperty(cameraButton,'disabled',switchingCamera);
  setProperty($('#phoneConnectRetry'),'hidden',!codeError);

  const firewall=status.firewall||{};
  const permission=firewallError||firewall.message;
  setProperty($('#phoneFirewallRow'),'hidden',!permission||connected||
    (!firewallError&&['ready','unsupported','unchecked','local'].includes(firewall.state)));
  setText($('#phoneFirewallStatus'),permission||'');
  setProperty(firewallButton,'hidden',!firewallError&&!['missing','error'].includes(firewall.state));
}

async function loadPhoneCode(){
  const token=++request;
  loadingCode=true;codeError='';
  renderPhoneConnection();
  try{
    const result=await api('/api/phone-connect');
    if(token!==request||!panel.open)return;
    if(!result.svg)throw new Error('二维码暂时不可用');
    image.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(result.svg);
    image.hidden=false;
  }catch(error){
    if(token!==request||!panel.open)return;
    image.hidden=true;
    codeError=error.message||'二维码暂时不可用';
  }finally{
    if(token===request){loadingCode=false;renderPhoneConnection()}
  }
}

export function openPhoneCode({offerCameraAction=true}={}){
  if(panel.open)return;
  remember(offeredKey);
  offerCamera=offerCameraAction;
  cameraError='';firewallError='';
  addressKey=JSON.stringify(lastStatus.phone_ws_urls||[]);
  panel.showModal();
  void loadPhoneCode();
}

export function offerPhoneCode(options){
  if(!anyPhoneConnected(lastStatus)&&!remembered(knownKey)&&!remembered(offeredKey)&&
      !document.querySelector('dialog[open]'))openPhoneCode(options);
}

for(const id of ['phoneConnectBtn','homePhoneConnectBtn']){
  $('#'+id).addEventListener('click',openPhoneCode);
}
panel.addEventListener('close',()=>{
  if(panel.open)return;
  request++;
  image.hidden=true;
  image.removeAttribute('src');
});
$('#phoneConnectRetry').addEventListener('click',()=>void loadPhoneCode());
cameraButton.addEventListener('click',()=>void runAction(async()=>{
  cameraError='';switchingCamera=true;
  renderPhoneConnection();
  try{await setSource('phone',true)}
  catch(error){
    cameraError=error.message||'切换手机摄像头失败';
    if(!panel.open)notice(cameraError);
  }finally{
    switchingCamera=false;
    renderPhoneConnection();
    if(panel.open&&cameraButton.hidden)$('#closePhoneConnectBtn').focus();
  }
}));
firewallButton.addEventListener('click',async()=>{
  firewallButton.disabled=true;
  firewallError='';
  try{
    const result=await post('/api/phone-connect/firewall',{});
    if(result.firewall)lastStatus={...lastStatus,firewall:result.firewall};
  }catch(error){firewallError=error.message}
  finally{
    firewallButton.disabled=false;
    renderPhoneConnection();
  }
});

export function syncPhoneCode(status){
  lastStatus=status;
  if(anyPhoneConnected(status)&&!remembered(knownKey))remember(knownKey);
  if(!panel.open)return;
  const key=JSON.stringify(status.phone_ws_urls||[]);
  if(key!==addressKey){
    addressKey=key;
    void loadPhoneCode();
  }else renderPhoneConnection();
}
