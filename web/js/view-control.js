// 设置 → 体感 → 视角控制：左右、上下方式，头控和握拳的手感。
import {VIEW_CONTROL_CONTENT} from '../view-control-guide.js';
import {$,api,configurationBusy,configurationOperation,flashStatus,post,setProperty,setText} from './core.js';
import {invalidateKernelRequests,kernelEpoch,noteOutputMix} from './devices.js';
import {mergeOwnsSticks,renderKernelState} from './play.js';
import {tutorial} from './shell.js';
import {S,head,output} from './state.js';
import {initHeadCurve,renderHeadCurve} from './head-curve.js';

export let handMouseConfig={enabled:true,horizontal_hand:'off',vertical_hand:'left'};
let viewControlSaving=false;
let viewControlReady=false;
let viewControlRevision=0;
let viewControlReload=null;
let hfhpeStatus=null;

// --- hand mouse -----------------------------------------------------------
// The fist thresholds shipped as estimates rather than measurements, so the
// live reading sits next to them: open the hand, read the number, close it,
// read again, then put the thresholds between the two.
//
// Which reading, and therefore which pair of sliders, depends on what the
// phone is sending.  With finger joints the number is how far the fingertips
// reach past their knuckles; without them it is the coarse fingertip spread.
// Showing both pairs at once would leave the player tuning whichever one
// happens to do nothing.
function fillViewControlOptions(){
  for(const [id,items] of [['viewHorizontalSource',VIEW_CONTROL_CONTENT.horizontal],['viewVerticalSource',VIEW_CONTROL_CONTENT.vertical]]){
    const select=$('#'+id);select.replaceChildren(...items.map(item=>new Option(item.label,item.value)));
  }
}

function viewChoice(items,value){return items.find(item=>item.value===value)||items.at(-1)}

function horizontalDescription(choice){
  const description=viewChoice(VIEW_CONTROL_CONTENT.horizontal,choice).description;
  if(choice!=='hfhpe'||!hfhpeStatus||['ready','inactive'].includes(hfhpeStatus.state))return description;
  const message=hfhpeStatus.message||({loading:'正在加载转头模型…',error:'转头模型暂不可用，请重新启动识别后重试',unavailable:'缺少实验模型，请运行工具下载'})[hfhpeStatus.state];
  return message?`${description}。${message}`:description;
}

export function renderHfhpeStatus(state){
  hfhpeStatus=state||null;
  if($('#viewHorizontalSource')?.value==='hfhpe')setText($('#viewHorizontalDesc'),horizontalDescription('hfhpe'));
}

export function renderViewControl(force=false){
  if(viewControlSaving||(!force&&document.activeElement?.matches('#viewHorizontalSource,#viewVerticalSource')))return;
  const horizontalHand=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.horizontal_hand)?handMouseConfig.horizontal_hand:null;
  const verticalHand=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.vertical_hand)?handMouseConfig.vertical_hand:null;
  const horizontal=horizontalHand||(head.enabled?(['roll_tilt','head_responsive','hfhpe'].includes(head.horizontalAlgorithm)?head.horizontalAlgorithm:'head_turn'):'off');
  const vertical=verticalHand||(head.verticalLookEnabled?'head':'off');
  setProperty($('#viewHorizontalSource'),'value',horizontal);setProperty($('#viewVerticalSource'),'value',vertical);
  const verticalInfo=viewChoice(VIEW_CONTROL_CONTENT.vertical,vertical);
  setText($('#viewHorizontalDesc'),horizontalDescription(horizontal));
  setText($('#viewVerticalDesc'),verticalInfo.description);
  // 手感那几行只列眼下用得着的：用头就给头的，用握拳就给握拳的。
  const headOn=!horizontalHand&&horizontal!=='off',handOn=!!(horizontalHand||verticalHand);
  document.querySelectorAll('#headSettings .head-only').forEach(el=>setProperty(el,'hidden',!headOn));
  document.querySelectorAll('#headSettings .hand-only').forEach(el=>setProperty(el,'hidden',!handOn));
  // 抬头低头的手感只在选了它的时候列出来。
  setProperty($('#headPitchSettings'),'hidden',vertical!=='head');
  const status=$('#viewControlStatus');if(viewControlReady&&status.textContent==='正在读取当前设置…')status.textContent='';
  renderHeadCurve();
}

export function initViewControl(){
  fillViewControlOptions();initHeadCurve();renderViewControl();
  // 弹不弹、弹哪个由教学自己定：从没看过就带着做基础；基础走完后的下一次打开，让人挑一次别的。
  tutorial.autoOpen();
}

function renderHandMouse(state){
  if(!state)return;
  const c=state.config||{};
  handMouseConfig={...handMouseConfig,...c};
  // 合流模式下摇杆归物理手柄，手控鼠标接不上任何东西——勾着不起作用比灰着更糟。
  const blocked=mergeOwnsSticks();
  // 握拳的松紧阈值在「体感 → 身体识别 → 量身 → 只量握拳」测量。
  for(const [id,value] of [['handMouseSensitivity',c.sensitivity],['handMouseDeadzone',c.deadzone]]){
    if(value!==undefined&&document.activeElement!==$('#'+id))setProperty($('#'+id),'value',String(value));
  }
  setText($('#handMouseSensitivityValue'),Number(c.sensitivity||0).toFixed(0));
  setText($('#handMouseDeadzoneValue'),Number(c.deadzone||0).toFixed(2));
  const grip=c.grip_algorithm==='classic'?'classic':'steady';
  if(document.activeElement!==$('#handMouseGripAlgorithm'))setProperty($('#handMouseGripAlgorithm'),'value',grip);
  // 握没握拳、手张没张开，玩的时候画面上看得到，这里不再挂一行「左手待机」。
  // 只在握拳真用不了的时候说一句。
  const fistOn=!!c.enabled&&(['left','right'].includes(c.horizontal_hand)||['left','right'].includes(c.vertical_hand));
  const status=$('#handMouseStatus');
  setText(status,blocked?'实体手柄合流占着摇杆，握拳控制用不了':'');
  setProperty(status,'hidden',!(blocked&&fistOn));
  renderViewControl();
}

export async function refreshHandMouse(){
  if(viewControlSaving)return;
  const revision=viewControlRevision;
  try{const data=await api('/api/hand-mouse/config');if(revision===viewControlRevision&&!viewControlSaving)renderHandMouse(data.hand_mouse)}catch{}
}

export async function reloadViewControlState(){
  if(viewControlReload)return viewControlReload;
  const revision=viewControlRevision,epoch=kernelEpoch;
  viewControlReload=(async()=>{
    const [runtime,handData]=await Promise.all([api('/api/kernel/status'),api('/api/hand-mouse/config')]);
    if(revision!==viewControlRevision||epoch!==kernelEpoch)return;
    renderKernelState(runtime,true);renderHandMouse(handData.hand_mouse);viewControlReady=true;
  })();
  try{await viewControlReload}finally{viewControlReload=null;setViewControlBusy(viewControlSaving);renderViewControl()}
}

export async function ensureViewControlReady(){
  if(viewControlReady||viewControlSaving)return true;
  try{await reloadViewControlState();return viewControlReady}
  catch{ $('#viewControlStatus').textContent='暂时读不到当前设置，连接恢复后会自动重试';return false }
}

export function setViewControlBusy(busy){
  viewControlSaving=busy;
  document.querySelectorAll('#headSettings input,#headSettings select').forEach(el=>el.disabled=busy||configurationBusy);
  for(const id of ['viewHorizontalSource','viewVerticalSource'])$('#'+id).disabled=busy||configurationBusy||!viewControlReady;
}

export async function saveHandMouseFields(payload){
  if(viewControlSaving)return;
  const revision=++viewControlRevision;
  setViewControlBusy(true);
  $('#handMouseSaveStatus').textContent='正在保存…';
  try{
    const data=await configurationOperation(()=>post('/api/hand-mouse/config',payload));
    if(revision===viewControlRevision)renderHandMouse(data.hand_mouse);
    flashStatus($('#handMouseSaveStatus'),'已保存');
  }catch(error){
    let refreshed=true;try{await reloadViewControlState()}catch{refreshed=false}
    $('#handMouseSaveStatus').textContent='保存失败，'+(refreshed?'已读取当前设置：':'暂时无法读取当前设置：')+(error.message||'请重试');
  }finally{setViewControlBusy(false);renderViewControl(true)}
}

export async function saveViewControlAxis(axis){
  if(viewControlSaving||!viewControlReady)return;
  const desiredHorizontal=$('#viewHorizontalSource').value,desiredVertical=$('#viewVerticalSource').value;
  const revision=++viewControlRevision;
  setViewControlBusy(true);S.headDirty=true;$('#viewControlStatus').textContent='正在保存…';
  try{
    await configurationOperation(async()=>{
      const epoch=invalidateKernelRequests();
      try{
        const data=await post('/api/view-control',{horizontal:desiredHorizontal,vertical:desiredVertical});
        if(revision!==viewControlRevision)return;
        if(epoch===kernelEpoch)renderKernelState(data,true);
        renderHandMouse(data.hand_mouse);viewControlReady=true;
      }finally{invalidateKernelRequests()}
    });
    if(revision!==viewControlRevision)return;
    if(['left','right'].includes(axis==='horizontal'?desiredHorizontal:desiredVertical))noteOutputMix();
    flashStatus($('#viewControlStatus'),'已保存');
  }catch(error){
    let refreshed=true;try{await reloadViewControlState()}catch{refreshed=false}
    $('#viewControlStatus').textContent=`保存未确认，${refreshed?'已读取当前设置':'读不到当前设置'}：${error.message||'请重试'}`;
  }finally{setViewControlBusy(false);S.headDirty=false;renderViewControl(true)}
}

export function syncControlLabels(){head.algorithm='pnp';head.verticalExclusive=!!$('#verticalExclusive')?.checked;head.bodyMotionGuard=!!$('#bodyMotionGuard')?.checked;head.deadzone=Number($('#deadzone').value)/100;head.sensitivityX=Number($('#speedX').value);head.sensitivityY=Number($('#speedY').value);head.horizontalCurveGamma=Number($('#headCurveGamma').value);head.invertY=$('#invertY').checked;setText($('#deadzoneValue'),Math.round(head.deadzone*100)+'%');setText($('#speedXValue'),head.sensitivityX+'%');setText($('#speedYValue'),head.sensitivityY+'%');setText($('#headCurveGammaValue'),head.horizontalCurveGamma.toFixed(2));output.strength=Number($('#strength').value);setText($('#strengthValue'),output.strength+'%');renderHeadCurve()}

export async function pushHeadConfig(){
  syncControlLabels();
  const epoch=invalidateKernelRequests();
  try{
    const data=await post('/api/head/config',{
    algorithm:head.algorithm,horizontal_algorithm:head.horizontalAlgorithm,deadzone:head.deadzone,
    sensitivity_x:head.sensitivityX,sensitivity_y:head.sensitivityY,horizontal_curve_gamma:head.horizontalCurveGamma,enabled:head.enabled,
    invert_y:head.invertY,vertical_look_source:head.verticalLookEnabled?'head':'off',
    vertical_exclusive:head.verticalExclusive,body_motion_guard:head.bodyMotionGuard,
  });
    if(epoch===kernelEpoch)renderKernelState(data);
  }finally{invalidateKernelRequests()}
}
