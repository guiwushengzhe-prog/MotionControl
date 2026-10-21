// 实验配置沿用主状态和配置队列，不单独轮询，也不自动开启设备。
import {$,api,configurationBusy,configurationOperation,flashStatus,post,setProperty,setText} from './core.js';
import {invalidateKernelRequests,kernelEpoch} from './devices.js';
import {renderKernelState} from './play.js';

const DEFAULTS={head_hold:false,first_step:false,lean_strafe:false,lean_back:false,body_source:'model',compare:false};
const SWITCHES={head_hold:'experimentHeadHold',first_step:'experimentFirstStep',lean_strafe:'experimentLeanStrafe',lean_back:'experimentLeanBack',compare:'experimentCompare'};
const SOURCE_NAMES={model:'模型骨骼',depth:'深度骨骼'};
let current=null,saving=false,lastKernel={};

function activeText(value){
  const keys=Array.isArray(value)?value:Object.entries(value||{}).filter(([,on])=>on===true).map(([key])=>key);
  return keys.map(key=>({lean_left:'左移',lean_right:'右移',lean_back:'后退',left:'左移',right:'右移',back:'后退'})[key]).filter(Boolean).join('、')||'未触发';
}
function angle(value){return value!=null&&Number.isFinite(Number(value))?`${Number(value).toFixed(1)}°`:'—'}
function sourceReason(source){
  if(source?.side_valid&&source.side_calibrated&&source.back_calibrating)return '侧倾可用，正在记录后倾站姿';
  if(source?.back_valid&&source.back_calibrated&&source.side_calibrating)return '后倾可用，正在记录侧倾站姿';
  if(source?.calibrating)return `正在记录站姿${source.remaining_s!=null?`，剩余 ${Math.max(0,Number(source.remaining_s)||0).toFixed(1)} 秒`:''}`;
  if(source?.valid!==true)return source?.reason||'暂无有效骨骼';
  if(source?.calibrated!==true)return source?.reason||'尚未记录站姿，点“重设站姿”';
  return source?.reason||'';
}
function render(){
  const config={...DEFAULTS,...current?.config},head=lastKernel.head||{};
  const holdAvailable=head.enabled===true&&head.horizontal_algorithm==='hfhpe_simple';
  const stepAvailable=lastKernel.march_algorithm==='responsive';
  for(const [key,id] of Object.entries(SWITCHES)){
    const input=$('#'+id);
    if(!saving)setProperty(input,'checked',config[key]===true);
    setProperty(input,'disabled',saving||configurationBusy||!current||(key==='head_hold'&&!holdAvailable)||(key==='first_step'&&!stepAvailable));
  }
  if(!saving)setProperty($('#experimentBodySource'),'value',config.body_source==='depth'?'depth':'model');
  for(const id of ['experimentBodySource','experimentNeutralBtn'])setProperty($('#'+id),'disabled',saving||configurationBusy||!current);
  setText($('#experimentHeadHoldNote'),!holdAvailable?'先在“体感 → 视角”选择左右转头的模型简化方案':config.head_hold?'偏头时持续转向，回到中心停止':'开启后偏头停住仍持续转向；回到中心停止');
  setText($('#experimentFirstStepNote'),!stepAvailable?'先在“体感 → 身体识别”选择灵敏踏步':current?.first_step_message||'首步即可响应，不等待连续踏步');
  const body=current?.body||{},source=body.source||config.body_source,reading=body.sources?.[source],reason=sourceReason(reading);
  const bodyEnabled=config.lean_strafe||config.lean_back;
  setText($('#experimentBodyStatus'),!current?'等待当前状态…':`${bodyEnabled?'控制来源':'待用来源'}：${SOURCE_NAMES[source]||'未确定'} · ${reason||(bodyEnabled?activeText(body.active??reading?.active):'身体走位未开启')}`);
  setProperty($('#experimentComparison'),'hidden',config.compare!==true);
  for(const [key,id] of [['model','experimentModelReadout'],['depth','experimentDepthReadout']]){
    const item=body.sources?.[key],issue=sourceReason(item);
    const content=[`${SOURCE_NAMES[key]}${bodyEnabled&&source===key?' · 控制来源':' · 仅对照'}`,`侧倾 ${angle(item?.side_deg)} · 后倾 ${angle(item?.back_deg)}`,`触发：${activeText(item?.active)}`,issue||'骨骼有效，站姿已记录'];
    const target=$('#'+id);
    if(target&&target.textContent!==content.join(''))target.replaceChildren(...content.map((text,index)=>{const node=document.createElement(index===0?'strong':'span');node.textContent=text;return node}));
  }
}

export function renderSensitivityExperiments(kernel){
  lastKernel=kernel||{};
  current=kernel?.sensitivity_experiments||null;
  render();
}

async function save(payload,neutral=false){
  if(saving||configurationBusy||!current){render();return}
  saving=true;render();setText($('#experimentSaveStatus'),neutral?'正在重设站姿…':'正在保存…');
  try{
    await configurationOperation(async()=>{
      const epoch=invalidateKernelRequests();
      try{
        const runtime=await post(neutral?'/api/sensitivity-experiments/neutral':'/api/sensitivity-experiments',payload);
        if(epoch===kernelEpoch)renderKernelState(runtime,true);
      }finally{invalidateKernelRequests()}
    });
    flashStatus($('#experimentSaveStatus'),neutral?'已请求记录站姿':'已保存');
  }catch(error){
    let restored=false;
    try{renderKernelState(await api('/api/kernel/status'),true);restored=true}catch{}
    setText($('#experimentSaveStatus'),`保存未确认，${restored?'已读取当前设置':'连接恢复后将恢复实际设置'}：${error.message||'请重试'}`);
  }finally{saving=false;render()}
}

for(const [key,id] of Object.entries(SWITCHES))$('#'+id)?.addEventListener('change',event=>void save({[key]:event.target.checked}));
$('#experimentBodySource')?.addEventListener('change',event=>void save({body_source:event.target.value}));
$('#experimentNeutralBtn')?.addEventListener('click',()=>void save({},true));
