// 设置 → 视角：左右、上下方案，头控和握拳的手感。
import {VIEW_CONTROL_CONTENT} from '../view-control-guide.js';
import {$,api,flashStatus,notice,post} from './core.js';
import {renderOutputMix} from './devices.js';
import {mergeOwnsSticks,renderKernelState} from './play.js';
import {headSaver,tutorial} from './shell.js';
import {S,head,output} from './state.js';

export let handMouseConfig={enabled:true,horizontal_hand:'off',vertical_hand:'left'};
let viewControlSaving=false;
let viewControlReady=false;

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

export function renderViewControl(force=false){
  if(viewControlSaving||(!force&&document.activeElement?.matches('#viewHorizontalSource,#viewVerticalSource')))return;
  const horizontalHand=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.horizontal_hand)?handMouseConfig.horizontal_hand:null;
  const verticalHand=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.vertical_hand)?handMouseConfig.vertical_hand:null;
  const horizontal=horizontalHand||(head.enabled?(['roll_tilt','head_responsive'].includes(head.horizontalAlgorithm)?head.horizontalAlgorithm:'head_turn'):'off');
  const vertical=verticalHand||(head.verticalLookEnabled?'head':'off');
  $('#viewHorizontalSource').value=horizontal;$('#viewVerticalSource').value=vertical;
  const horizontalInfo=viewChoice(VIEW_CONTROL_CONTENT.horizontal,horizontal),verticalInfo=viewChoice(VIEW_CONTROL_CONTENT.vertical,vertical);
  $('#viewHorizontalDesc').textContent=horizontalInfo.description;
  $('#viewVerticalDesc').textContent=verticalInfo.description;
  // 手感那几行只列眼下用得着的：用头就给头的，用握拳就给握拳的。
  const headOn=!horizontalHand&&horizontal!=='off',handOn=!!(horizontalHand||verticalHand);
  document.querySelectorAll('#headSettings .head-only').forEach(el=>{el.hidden=!headOn});
  document.querySelectorAll('#headSettings .hand-only').forEach(el=>{el.hidden=!handOn});
  // 抬头低头的手感只在选了它的时候列出来。
  $('#headPitchSettings').hidden=vertical!=='head';
  const status=$('#viewControlStatus');if(viewControlReady&&status.textContent==='正在读取当前设置…')status.textContent='';
}

export function initViewControl(){
  fillViewControlOptions();renderViewControl();
  // 弹不弹、弹哪个由教学自己定：从没看过就带着做基础；基础走完后的下一次打开，让人挑一次别的。
  tutorial.autoOpen();
}

function renderHandMouse(state){
  if(!state)return;
  const c=state.config||{};
  handMouseConfig={...handMouseConfig,...c};
  // 合流模式下摇杆归物理手柄，手控鼠标接不上任何东西——勾着不起作用比灰着更糟。
  const blocked=mergeOwnsSticks();
  renderOutputMix();
  // 握拳的松紧阈值不在这里手调了：「身体识别 → 量身 → 只量握拳」量出来的就是这两个值。
  for(const [id,value] of [['handMouseSensitivity',c.sensitivity],['handMouseDeadzone',c.deadzone]]){
    if(value!==undefined&&document.activeElement!==$('#'+id))$('#'+id).value=value;
  }
  $('#handMouseSensitivityValue').textContent=Number(c.sensitivity||0).toFixed(0);
  $('#handMouseDeadzoneValue').textContent=Number(c.deadzone||0).toFixed(2);
  const readings=Object.values(state.axes||{});
  const byHand=readings.some(s=>s.grip_source==='hand');
  const reading=byHand
    ?(state.curl==null?'看不到手':`手指伸展 ${Number(state.curl).toFixed(2)}`)
    :(state.spread==null?'看不到手':`张开度 ${Number(state.spread).toFixed(3)}`);
  const axisLabel=(axis,name)=>{const s=state.axes?.[axis];if(!s)return '';const hand={left:'左手',right:'右手',off:'关闭'}[s.hand]||'关闭';const phase=({disabled:'未启用',idle:'待机',open:'手张开',engaged:'已握拳',moving:'握拳移动中',opened:'刚松开',lost:'看不到手'})[s.state]||s.state;const measure=s.grip_source==='hand'?`手指伸展 ${Number(s.curl).toFixed(2)}`:s.spread==null?'看不到手':`张开度 ${Number(s.spread).toFixed(3)}`;return `${name}：${hand} · ${phase} · ${measure}`;};
  const label={disabled:'未启用',idle:'待机',open:'手张开',engaged:'已握拳',moving:'握拳移动中',opened:'刚松开',lost:'看不到手'}[state.state]||state.state;
  // 握着没握着，看得到手时才说；一直「看不到手」就不说。
  const phaseName={idle:'待机',open:'手张开',engaged:'已握拳',moving:'握拳移动中',opened:'刚松开',lost:'看不到手'};
  const live=Object.values(state.axes||{}).filter(s=>['left','right'].includes(s.hand)&&s.state!=='disabled');
  const seen=live.filter(s=>s.state!=='lost');
  $('#handMouseStatus').textContent=blocked?'实体手柄合流占着摇杆，握拳控制用不了'
    :c.enabled&&seen.length?[...new Map(seen.map(s=>[s.hand,`${s.hand==='left'?'左手':'右手'}${phaseName[s.state]||s.state}`])).values()].join(' · '):'';
  renderViewControl();
}

export async function refreshHandMouse(){try{const data=await api('/api/hand-mouse/config');renderHandMouse(data.hand_mouse)}catch{}}

export async function reloadViewControlState(){
  const [runtime,handData]=await Promise.all([api('/api/kernel/status'),api('/api/hand-mouse/config')]);
  renderKernelState(runtime,true);renderHandMouse(handData.hand_mouse);viewControlReady=true;
}

export function setViewControlBusy(busy){
  viewControlSaving=busy;
  document.querySelectorAll('#headSettings input,#headSettings select').forEach(el=>el.disabled=busy);
  for(const id of ['viewHorizontalSource','viewVerticalSource'])$('#'+id).disabled=busy||!viewControlReady;
}

async function postViewHead(payload){return post('/api/head/config',payload)}

export async function saveHandMouseFields(payload){
  if(viewControlSaving)return;
  setViewControlBusy(true);
  $('#handMouseSaveStatus').textContent='正在保存…';
  try{
    const data=await post('/api/hand-mouse/config',payload);
    renderHandMouse(data.hand_mouse);
    flashStatus($('#handMouseSaveStatus'),'已保存');
  }catch(error){
    let refreshed=true;try{await reloadViewControlState()}catch{refreshed=false}
    $('#handMouseSaveStatus').textContent='保存失败，'+(refreshed?'已读取当前设置：':'暂时无法读取当前设置：')+(error.message||'请重试');
  }finally{setViewControlBusy(false);renderViewControl(true)}
}

export async function saveViewControlAxis(axis){
  if(viewControlSaving||!viewControlReady)return;
  if(headSaver.pending()){notice('请等当前设置保存完成');renderViewControl(true);return}
  const desiredHorizontal=$('#viewHorizontalSource').value,desiredVertical=$('#viewVerticalSource').value;
  const currentHorizontal=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.horizontal_hand)?handMouseConfig.horizontal_hand:'off';
  const currentVertical=handMouseConfig.enabled&&['left','right'].includes(handMouseConfig.vertical_hand)?handMouseConfig.vertical_hand:'off';
  setViewControlBusy(true);S.headDirty=true;$('#viewControlStatus').textContent='正在保存…';
  try{
    if(axis==='horizontal'){
      if(['roll_tilt','head_turn','head_responsive'].includes(desiredHorizontal)){
        if(currentHorizontal!=='off')await post('/api/hand-mouse/config',{horizontal_hand:'off',enabled:Boolean(handMouseConfig.enabled&&currentVertical!=='off')});
        await post('/api/head/config',{enabled:true,horizontal_algorithm:desiredHorizontal==='head_turn'?'gesture_v188':desiredHorizontal});
      }else if(desiredHorizontal==='left'||desiredHorizontal==='right'){
        if(head.enabled)await post('/api/head/config',{enabled:false});
        await post('/api/hand-mouse/config',{enabled:true,horizontal_hand:desiredHorizontal,vertical_hand:currentVertical});
      }else{
        if(currentHorizontal!=='off')await post('/api/hand-mouse/config',{horizontal_hand:'off',enabled:Boolean(handMouseConfig.enabled&&currentVertical!=='off')});
        if(head.enabled)await post('/api/head/config',{enabled:false});
      }
    }else if(desiredVertical==='head'){
      if(currentVertical!=='off')await post('/api/hand-mouse/config',{vertical_hand:'off',enabled:Boolean(handMouseConfig.enabled&&currentHorizontal!=='off')});
      await postViewHead({vertical_look_source:'head'});
    }else if(desiredVertical==='left'||desiredVertical==='right'){
      if(head.verticalLookEnabled)await postViewHead({vertical_look_source:'off'});
      await post('/api/hand-mouse/config',{enabled:true,vertical_hand:desiredVertical,horizontal_hand:currentHorizontal});
    }else{
      if(currentVertical!=='off')await post('/api/hand-mouse/config',{vertical_hand:'off',enabled:Boolean(handMouseConfig.enabled&&currentHorizontal!=='off')});
      if(head.verticalLookEnabled)await postViewHead({vertical_look_source:'off'});
    }
    await reloadViewControlState();flashStatus($('#viewControlStatus'),'已保存');
  }catch(error){
    let refreshed=true;try{await reloadViewControlState()}catch{refreshed=false}
    $('#viewControlStatus').textContent=`没保存上，${refreshed?'已恢复原来的设置':'读不到当前设置'}：${error.message||'请重试'}`;
  }finally{setViewControlBusy(false);S.headDirty=false;renderViewControl(true)}
}

export function syncControlLabels(){head.algorithm=$('#headAlgorithm').value;head.verticalExclusive=!!$('#verticalExclusive')?.checked;head.bodyMotionGuard=!!$('#bodyMotionGuard')?.checked;head.deadzone=Number($('#deadzone').value)/100;head.sensitivityX=Number($('#speedX').value);head.sensitivityY=Number($('#speedY').value);head.invertY=$('#invertY').checked;$('#deadzoneValue').textContent=Math.round(head.deadzone*100)+'%';$('#speedXValue').textContent=head.sensitivityX+'%';$('#speedYValue').textContent=head.sensitivityY+'%';output.strength=Number($('#strength').value);$('#strengthValue').textContent=output.strength+'%'}

export async function pushHeadConfig(){
  syncControlLabels();
  renderKernelState(await post('/api/head/config',{
    algorithm:head.algorithm,horizontal_algorithm:head.horizontalAlgorithm,deadzone:head.deadzone,
    sensitivity_x:head.sensitivityX,sensitivity_y:head.sensitivityY,enabled:head.enabled,
    invert_y:head.invertY,vertical_look_source:head.verticalLookEnabled?'head':'off',
    vertical_exclusive:head.verticalExclusive,body_motion_guard:head.bodyMotionGuard,
  }));
}
