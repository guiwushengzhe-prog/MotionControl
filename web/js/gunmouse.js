// 模式参数走通用设置，按键走原游戏映射，不另设绑定副本或状态轮询。
import {$,api,autosaver,post,setText} from './core.js';
import {revealBindingRow} from './mapping.js';
import {showView} from './shell.js';
import {renderKernelState} from './play.js';

let current=null,scanning=false;
const fields=()=>({enabled:$('#gunmouseEnabled').checked,address:$('#gunmouseDevice').value,
  strength_mg:Number($('#gunmouseStrength').value),cooldown_ms:Number($('#gunmouseCooldown').value)});
const save=autosaver(async()=>{const data=await post('/api/gunmouse/config',fields());if(data.config_revision!=null)renderKernelState(data);else current=data.gunmouse},'gunmouseSaveStatus','gunmouseSaveRetry');
const states={off:'拳击模式未开启',select_device:'请选择已配对的枪型飞鼠',connecting:'正在连接飞鼠…',
  waiting:'等待飞鼠确认',calibrating:'等待校准或设备就绪，请放稳飞鼠',ready:'甩拳已就绪',disconnected:'飞鼠已断开，甩拳按键已释放',
  applying:'正在应用甩拳参数…',
  firmware_required:'需要更新飞鼠固件',unavailable:'飞鼠组件尚未准备'};

function labels(){
  setText($('#gunmouseStrengthValue'),`${(Number($('#gunmouseStrength').value)/1000).toFixed(2)} 倍重力`);
  setText($('#gunmouseCooldownValue'),`${$('#gunmouseCooldown').value} 毫秒`);
}
export function renderGunMouse(status){
  if(!status)return;
  current=status;
  if(!save.pending()){
    const config=status.config||{};
    $('#gunmouseEnabled').checked=config.enabled===true;
    const device=$('#gunmouseDevice');
    if(config.address&&![...device.options].some(option=>option.value===config.address)){
      device.add(new Option(`枪型飞鼠 · ${config.address}`,config.address));
    }
    device.value=config.address||'';
    $('#gunmouseStrength').value=String(config.strength_mg??1800);
    $('#gunmouseCooldown').value=String(config.cooldown_ms??500);
    labels();
  }
  setText($('#gunmouseStatus'),status.error||states[status.state]||'等待飞鼠连接');
  setText($('#gunmouseCounts'),`识别 ${status.received||0} 次 · 已执行 ${status.executed||0} 次${status.last_strength_mg?` · 最近强度 ${(status.last_strength_mg/1000).toFixed(2)} 倍重力`:''}`);
}
for(const id of ['gunmouseEnabled','gunmouseDevice','gunmouseStrength','gunmouseCooldown']){
  $('#'+id).addEventListener('change',()=>{labels();save.dirty()});
}
for(const id of ['gunmouseStrength','gunmouseCooldown'])$('#'+id).addEventListener('input',labels);
$('#gunmouseScanBtn').addEventListener('click',async()=>{
  if(scanning)return;scanning=true;$('#gunmouseScanBtn').disabled=true;
  try{
    const data=await api('/api/gunmouse/devices',{timeoutMs:15000}),select=$('#gunmouseDevice'),previous=select.value;
    const devices=data.devices||[];select.replaceChildren(new Option('选择已配对设备',''));
    for(const device of devices)select.add(new Option(`${device.name} · ${device.address}`,device.address));
    if(previous&&![...select.options].some(option=>option.value===previous))select.add(new Option(`枪型飞鼠 · ${previous}`,previous));
    select.value=previous;
    if(!previous&&devices.length===1){select.value=devices[0].address;save.dirty()}
    if(!devices.length)setText($('#gunmouseStatus'),'没有找到已配对飞鼠，请先在 Windows 蓝牙设置中配对，再点查找');
  }catch(error){setText($('#gunmouseStatus'),error.message)}
  finally{scanning=false;$('#gunmouseScanBtn').disabled=false}
});
$('#gunmouseBindingBtn').addEventListener('click',()=>{showView('games');revealBindingRow('motion.gunmouse_punch')});
