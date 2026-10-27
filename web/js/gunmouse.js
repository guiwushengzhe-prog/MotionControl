// 模式参数走通用设置，按键走原游戏映射，不另设绑定副本或状态轮询。
import {$,api,autosaver,post,setText} from './core.js';
import {revealBindingRow} from './mapping.js';
import {showView} from './shell.js';
import {renderKernelState} from './play.js';
import {renderPeripheralState} from './peripherals.js';

let current=null,scanning=false,managementBusy=false,lastAutoRead=null,lastAddress=null;
const hardwareDirty=new Set();
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
  if(lastAddress!==status.config?.address){lastAddress=status.config?.address;hardwareDirty.clear();lastAutoRead=null;}
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
  const connected=status.connected===true,snapshot=status.device,d=snapshot?.diag,details=snapshot?.details;
  if(!connected)lastAutoRead=null;
  setText($('#gunmouseConnectionStatus'),connected?'已连接设备管理':status.error||states[status.state]||'等待连接');
  const punch=!status.config?.enabled?'甩拳关闭':status.state==='ready'?'甩拳就绪':status.state==='calibrating'?'甩拳校准中':'甩拳准备中';
  renderPeripheralState('gunmouse',connected?'已连接':status.error?'连接异常':status.state==='connecting'?'正在连接':status.config?.address?'未连接':'未添加',{
    device:status.config?.address?$('#gunmouseDevice').selectedOptions[0]?.textContent||'枪型飞鼠':'未选择设备',
    punch:punch.replace(/^甩拳/,''),count:`${status.received||0} 次`,
    strength:status.last_strength_mg?`${(status.last_strength_mg/1000).toFixed(2)} 倍重力`:'—',
  });
  $('#gunmouseConnectBtn').hidden=connected;
  $('#gunmouseDisconnectBtn').hidden=!connected;
  $('#gunmouseConnectBtn').disabled=connected||managementBusy;
  $('#gunmouseDisconnectBtn').disabled=!connected||managementBusy;
  $('#gunmouseRefreshBtn').disabled=!connected||managementBusy;
  $('#gunmouseHardwareControls').disabled=!connected||!details||managementBusy;
  const summary=connected&&d?[`固件 ${d.build||d.fw||'—'}`,details?`陀螺仪${details.imu_ready?'就绪':'校准中'}`:'',details?.power?.mv>0?`采样电压 ${(details.power.mv/1000).toFixed(2)} 伏`:''].filter(Boolean).join(' · '):'等待设备回报';
  setText($('#gunmouseDeviceSummary'),summary);
  if(connected&&d&&details){
    const values={gunmouseAlways:!!details.always,gunmouseInvertX:!!d.invx,gunmouseInvertY:!!d.invy,
      gunmouseSensitivity:details.sens,gunmouseTouchPress:(d.tp*100).toFixed(2),gunmouseTouchRelease:(d.tr*100).toFixed(2),
      gunmouseJoystick:!!details.joy?.enabled,gunmouseJoystickSpeed:details.joy?.speed};
    for(const [id,value] of Object.entries(values)){
      const node=$('#'+id);if(hardwareDirty.has(id)||document.activeElement===node)continue;
      if(node.type==='checkbox')node.checked=value;else node.value=value??'';
    }
    const offset=Math.max(d.base*d.tp,d.noise*8);
    setText($('#gunmouseTouchStatus'),`实际开启 ${Math.round(d.base+offset)} · 关闭 ${Math.round(d.base+offset*d.tr/d.tp)} · ${d.ready?(d.touch?'已触摸':'未触摸'):'基线准备中'}`);
    const joy=details.joy||{};
    setText($('#gunmouseJoystickStatus'),joy.enabled?(joy.ready?`已启用 · 中心 ${(joy.center||[]).join(' / ')}`:'已启用，中心异常，暂停输出'):'已停用');
    $('#gunmouseCenterBtn').disabled=!joy.enabled||managementBusy;
  }
  if(connected&&!details&&snapshot&&$('#gunmouseSettings').open&&!lastAutoRead&&!managementBusy){
    lastAutoRead=true;void management('refresh');
  }
}

async function management(action,value,fields=[]){
  if(managementBusy)return;
  managementBusy=true;renderGunMouse(current);setText($('#gunmouseOperationStatus'),'正在读取设备…');
  try{
    const data=await post('/api/peripherals/gunmouse/operation',{action,...(value===undefined?{}:{value})});
    for(const id of fields)hardwareDirty.delete(id);
    renderGunMouse(data.gunmouse);
    const text=action==='refresh'?'设备状态已刷新':action==='calibrate_gyro'?'设备已确认校准命令，请保持静止约3秒':action==='calibrate_touch'?'设备已确认重取基线，请保持未触摸约半秒':'设备已确认设置';
    setText($('#gunmouseOperationStatus'),text);
  }catch(error){setText($('#gunmouseOperationStatus'),error.message)}
  finally{managementBusy=false;renderGunMouse(current)}
}
async function connection(enabled){
  if(managementBusy)return;
  if(enabled&&!$('#gunmouseDevice').value){
    setText($('#gunmouseOperationStatus'),'请先选择飞鼠');$('#gunmouseSettings').open=true;$('#gunmouseDevice').focus();return;
  }
  managementBusy=true;
  setText($('#gunmouseOperationStatus'),'');
  try{
    await save.flush();
    const data=await post('/api/gunmouse/config',enabled?{connection_enabled:true,address:$('#gunmouseDevice').value}:{connection_enabled:false,enabled:false});
    renderKernelState(data);
  }catch(error){setText($('#gunmouseOperationStatus'),error.message)}
  finally{managementBusy=false;renderGunMouse(current)}
}
$('#gunmouseConnectBtn').addEventListener('click',()=>connection(true));
$('#gunmouseDisconnectBtn').addEventListener('click',()=>connection(false));
$('#gunmouseRefreshBtn').addEventListener('click',()=>management('refresh'));
$('#gunmouseHardware').addEventListener('toggle',()=>{if($('#gunmouseHardware').open&&current?.connected)void management('refresh')});
const hardwareActions={gunmouseAlways:'always',gunmouseInvertX:'invert_x',gunmouseInvertY:'invert_y',gunmouseJoystick:'joystick'};
for(const [id,action] of Object.entries(hardwareActions))$('#'+id).addEventListener('change',()=>{
  hardwareDirty.add(id);void management(action,$('#'+id).checked,[id]);
});
for(const id of ['gunmouseSensitivity','gunmouseTouchPress','gunmouseTouchRelease','gunmouseJoystickSpeed'])$('#'+id).addEventListener('input',()=>hardwareDirty.add(id));
$('#gunmouseSensitivitySave').addEventListener('click',()=>management('sensitivity',Number($('#gunmouseSensitivity').value),['gunmouseSensitivity']));
$('#gunmouseJoystickSpeedSave').addEventListener('click',()=>management('joystick_speed',Number($('#gunmouseJoystickSpeed').value),['gunmouseJoystickSpeed']));
$('#gunmouseTouchSave').addEventListener('click',()=>management('touch',{press:Number($('#gunmouseTouchPress').value),release:Number($('#gunmouseTouchRelease').value)},['gunmouseTouchPress','gunmouseTouchRelease']));
for(const [id,action] of [['gunmouseCenterBtn','joystick_center'],['gunmouseGyroCalBtn','calibrate_gyro'],['gunmouseTouchCalBtn','calibrate_touch']])$('#'+id).addEventListener('click',()=>management(action));
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
