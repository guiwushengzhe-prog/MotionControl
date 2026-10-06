import {$,api,notice,post,setProperty,setText} from './core.js';

let initialized=false,request=0,addressKey='',baseline=new Set(),lastStatus={},unpairedOffered=false;
const defaultHint='扫码后记住这台电脑，下次自动连接。',unpairedHint='有手机正在连这台电脑，但还没配对：用它扫一下这个码。';
const knownKey='motioncontrol-phone-connected',offeredKey='motioncontrol-phone-code-offered';
function remembered(key){try{return localStorage.getItem(key)==='1'}catch{return false}}
function remember(key){try{localStorage.setItem(key,'1')}catch{}}
function devices(status){return new Set(['mobile_pose_sources','handheld_sources','mobile_voice_sources'].flatMap(key=>(status[key]||[]).filter(item=>item.connected).map(item=>item.device_id||item.source_id)))}
export function closePhoneCode(){request++;$('#phoneConnectPanel').hidden=true;$('#phoneConnectImage').removeAttribute('src');addressKey=''}
async function loadPhoneCode(){
  const token=++request;
  try{
    const result=await api('/api/phone-connect');
    if(token!==request||$('#phoneConnectPanel').hidden)return;
    $('#phoneConnectImage').src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(result.svg);
    $('#phoneConnectHint').textContent=lastStatus.unpaired_phone?unpairedHint:defaultHint;
  }catch(error){if(token===request)$('#phoneConnectHint').textContent=error.message||'连接码暂时不可用，请重试'}
}
export function openPhoneCode(){
  remember(offeredKey);baseline=devices(lastStatus);
  $('#phoneConnectPanel').hidden=false;addressKey=JSON.stringify(lastStatus.phone_ws_urls||[]);
  void loadPhoneCode();
}
export function syncPhoneCode(status,phoneSelected){
  lastStatus=status;
  if(!initialized){
    initialized=true;
    $('#phoneConnectBtn').addEventListener('click',openPhoneCode);
    $('#closePhoneConnectBtn').addEventListener('click',closePhoneCode);
    $('#phoneFirewallRetry').addEventListener('click',async()=>{
      try{await post('/api/phone-connect/firewall',{});notice('请在 Windows 提示中确认连接权限')}catch(error){notice(error.message)}
    });
  }
  const connected=devices(status),panel=$('#phoneConnectPanel'),unpaired=!!status.unpaired_phone;
  if(connected.size)remember(knownKey);
  // 手机上写着「请扫码」的时候，电脑这边把码摆出来。同一阵只弹一次，关掉就不再弹。
  if(!unpaired)unpairedOffered=false;
  if(!panel.hidden){
    if([...connected].some(id=>!baseline.has(id)))closePhoneCode();
    else{const key=JSON.stringify(status.phone_ws_urls||[]);if(key!==addressKey){addressKey=key;void loadPhoneCode()}
      else if($('#phoneConnectImage').getAttribute('src'))setText($('#phoneConnectHint'),unpaired?unpairedHint:defaultHint)}
  }else if(unpaired&&!unpairedOffered&&$('#phoneConnectBtn').getClientRects().length){unpairedOffered=true;openPhoneCode()}
  else if(phoneSelected&&!connected.size&&!remembered(knownKey)&&!remembered(offeredKey)&&$('#phoneConnectBtn').getClientRects().length){openPhoneCode()}
  const firewall=status.firewall||{},retry=['missing','error'].includes(firewall.state);
  setProperty($('#phoneFirewallRow'),'hidden',!firewall.message||['ready','unsupported','unchecked'].includes(firewall.state));
  setText($('#phoneFirewallStatus'),firewall.message||'');
  setProperty($('#phoneFirewallRetry'),'hidden',!retry);
}
