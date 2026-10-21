// 新手大卡片试调。动作选项和速度只在明确确认时保存，试用始终暂停系统输出。
import {$,clamp,configurationOperation,post,setAttribute,setProperty,setStyle,setText} from './core.js';
import {invalidateKernelRequests,setOutput} from './devices.js';
import {kernelReceivedAt,kernelState,refreshKernel,renderKernelState,sourceHasFreshFrame,zoneEditMode} from './play.js';
import {head,output} from './state.js';
import {handMouseConfig,reloadViewControlState} from './view-control.js';
import {composedSpeed,renderSpeedSummary,speedText} from './view-speed.js';
import {TuningNavigation} from './tuning-navigation.js';

const choices=[
  {id:'turn',name:'左右转头',icon:'↔',body:'脸朝左或右，控制左右视角',detail:'复用当前转头方案；首次选择用模型简化方案'},
  {id:'roll_tilt',name:'头部侧倾',icon:'◴',body:'头向肩膀歪，回正就停止',detail:'适合保持眼睛朝着屏幕'},
  {id:'right',name:'右手握拳',icon:'✊',body:'握拳后左右移动，松开就停止',detail:'握拳位置作为临时中心'},
  {id:'left',name:'左手握拳',icon:'✊',body:'握拳后左右移动，松开就停止',detail:'握拳位置作为临时中心'},
];
const dialog=$('#viewTuning'),nav=new TuningNavigation();
let stage='choose',index=0,choice=null,value=58,busy=false,opening=false,frame=0;
let navSource='tilt',navSign=1,baseline=null,baselineSamples=[],baselineAt=0;
let trialUntil=0,position=0,lastTick=0,stopPending=false,session=0;
let choiceAlgorithm='roll_tilt',returnFocus=null;
let navigationApproximate=false;
const isHand=()=>['left','right'].includes(choice?.id);
const limits=()=>isHand()?{min:10,max:160,step:5}:{min:20,max:200,step:2};

function resetNavigation(){nav.reset();baseline=null;baselineSamples=[];baselineAt=0;setStyle($('#tuningConfirmFill'),'width','0%')}
function menuItems(){return stage==='choose'?choices:[
  {id:'adjust',name:'再调速度',icon:'＋',body:'保留当前方式，继续逐格调节'},
  {id:'finish',name:'完成试调',icon:'✓',body:'设置已保存，回到体感页面'},
  ...(!isHand()?[{id:'calibrate',name:'校准正前方',icon:'◎',body:'保持自然姿势，重新确定视角中心'}]:[]),
  {id:'choose',name:'换个方式',icon:'↔',body:'比较转头、侧倾和握拳'},
]}

function paintCards(){
  const cards=$('#tuningCards');
  cards.replaceChildren(...menuItems().map((item,i)=>{
    const button=document.createElement('button');button.type='button';button.className='tuning-card';
    button.dataset.choice=item.id;button.setAttribute('aria-pressed',String(i===index));
    const icon=document.createElement('span');icon.className='tuning-card-icon';icon.textContent=item.icon;
    const title=document.createElement('strong');title.textContent=item.name;
    const body=document.createElement('span');body.textContent=item.body;
    const small=document.createElement('small');small.textContent=item.detail||'偏转选中 · 回正停留确认';
    button.append(icon,title,body,small);button.addEventListener('click',()=>{index=i;void confirm()});
    return button;
  }));
}
function paintSelection(){
  document.querySelectorAll('#tuningCards .tuning-card').forEach((card,i)=>setAttribute(card,'aria-pressed',String(i===index)));
  $('#tuningCards').children[index]?.scrollIntoView({block:'nearest',inline:'center',behavior:'smooth'});
}
function paintValue(){
  setText($('#tuningMethodName'),choice?.name||'');setText($('#tuningSpeedValue'),value+(isHand()?'':'%'));
  const slider=$('#tuningSpeed'),range=limits();Object.assign(slider,range);slider.value=value;
  const result=composedSpeed(isHand()?Math.min(1,value/100):value/100,{...output.server,mode:output.mode});
  setText($('#tuningSpeedSummary'),`结合当前输出倍率，左右最高 ${speedText(result,output.mode)}。回正停留后应用并试用。`);
}
function showStage(next){
  stage=next;index=0;resetNavigation();setText($('#tuningError'),'');
  setProperty($('#tuningCards'),'hidden',!['choose','review'].includes(stage));
  setProperty($('#tuningAdjust'),'hidden',stage!=='adjust');setProperty($('#tuningTrial'),'hidden',stage!=='trial');
  setProperty($('.tuning-nav'),'hidden',stage==='trial');
  const labels={choose:'01 / 选方式',adjust:'02 / 逐格调速',trial:'03 / 试手感',review:'04 / 留下顺手的设置',calibrating:'校准正前方'};
  setText($('#tuningStageLabel'),labels[stage]);
  setText($('#tuningInstruction'),stage==='adjust'?'向左逐格减慢，向右逐格加快；回正后试一试':stage==='trial'?'现在只试手感，动作不会选择菜单':stage==='review'?'试完了，选择下一步':'选一个你觉得自然的动作');
  if(['choose','review'].includes(stage))paintCards();
  if(stage==='adjust')paintValue();
  if(stage==='trial'){trialUntil=performance.now()+10000;position=0;lastTick=0}
}

async function operation(action){
  if(busy)return;const generation=session;busy=true;resetNavigation();setText($('#tuningError'),'');
  dialog.classList.add('saving');
  try{await action(generation)}catch(error){if(generation===session)setText($('#tuningError'),error.message||'没有保存成功，请重试')}
  finally{if(generation===session){busy=false;dialog.classList.remove('saving');resetNavigation()}}
}

async function selectChoice(){
  await operation(async generation=>{
    choice=choices[index];
    choiceAlgorithm=choice.id==='turn'?(['hfhpe','hfhpe_simple','gesture_v188'].includes(head.horizontalAlgorithm)?(head.horizontalAlgorithm==='gesture_v188'?'head_turn':head.horizontalAlgorithm):'hfhpe_simple'):choice.id;
    const vertical=$('#viewVerticalSource').value;
    await configurationOperation(async()=>{
      // 明确选中方式后才保存；打开试调不会替用户更换方案或速度。
      invalidateKernelRequests();
      try{renderKernelState(await post('/api/view-control',{horizontal:choiceAlgorithm,vertical}),true)}finally{invalidateKernelRequests()}
    });
    await reloadViewControlState();
    if(generation!==session||!dialog.open)return;
    value=isHand()?Number(handMouseConfig.sensitivity||70):head.sensitivityX;showStage('adjust');
  });
}

async function applySpeed(){
  await operation(async generation=>{
    await configurationOperation(async()=>{
      invalidateKernelRequests();
      try{
        if(isHand())await post('/api/hand-mouse/config',{sensitivity:value});
        else renderKernelState(await post('/api/head/config',{sensitivity_x:value}),true);
      }finally{invalidateKernelRequests()}
    });
    await reloadViewControlState();renderSpeedSummary();
    if(generation===session&&dialog.open)showStage('trial');
  });
}

async function confirm(){
  if(busy)return;
  if(stage==='choose')return selectChoice();
  if(stage==='adjust')return applySpeed();
  if(stage==='review'){
    const next=menuItems()[index].id;
    if(next==='finish')close();
    else if(next==='calibrate')await operation(async()=>{
      renderKernelState(await post('/api/head/calibration/start',{}));showStage('calibrating');
    });
    else showStage(next);
  }
}

function move(delta){
  if(stage==='adjust'){const {min,max,step}=limits();value=clamp(value+delta*step,min,max);paintValue()}
  else if(['choose','review'].includes(stage)){index=clamp(index+delta,0,menuItems().length-1);paintSelection()}
}

function navigationSignal(){
  const hs=kernelState?.head||{};
  navigationApproximate=false;
  if(navSource==='turn'){
    const value=hs.hfhpe_estimate_valid?hs.hfhpe_yaw_deg:hs.estimate_valid?hs.raw_yaw:null;
    if(value==null||!Number.isFinite(Number(value)))return null;
    // 旧骨骼转头是无量纲比例，沿用控制器0.34对应18度的菜单尺度，不冒充实测角度。
    navigationApproximate=!hs.hfhpe_estimate_valid&&hs.raw_yaw_units==='ratio';
    return Number(value)*(navigationApproximate?18/.34:1);
  }
  // 模型转头方案不更新侧倾控制器；仍可从同帧眼线独立读取菜单侧倾，不额外运行模型。
  const pose=kernelState?.pose||{},left=pose.left_eye,right=pose.right_eye;
  if(!left||!right||Number(left.score??left.visibility??0)<.5||Number(right.score??right.visibility??0)<.5)return null;
  const points=[left,right].sort((a,b)=>a.x-b.x),w=kernelState.width||640,h=kernelState.height||480;
  return Math.atan2((points[1].y-points[0].y)*h,(points[1].x-points[0].x)*w)*180/Math.PI;
}

function tick(now){
  if(!dialog.open)return;
  frame=requestAnimationFrame(tick);
  const elapsed=now-kernelReceivedAt;
  const fresh=!document.hidden&&kernelReceivedAt>0&&elapsed<1000&&sourceHasFreshFrame()
    &&kernelState?.pose_age_ms!=null&&Number(kernelState.pose_age_ms)+elapsed<1000;
  if(output.enabled){
    if(!stopPending){stopPending=true;void setOutput(false).catch(error=>setText($('#tuningError'),error.message)).finally(()=>{stopPending=false})}
    resetNavigation();return;
  }
  if(busy)return;
  if(stage==='calibrating'){
    const hs=kernelState?.head||{};
    if(!fresh){setText($('#tuningError'),'等待新画面，恢复后继续校准');return}
    if(hs.calibrating)setText($('#tuningInstruction'),hs.notice||'看向屏幕中心，保持自然姿势');
    else if(hs.horizontal_calibrated??hs.calibrated)showStage('trial');
    else{showStage('review');setText($('#tuningError'),'尚未完成校准，可以重新选择“校准正前方”')}
    return;
  }
  if(stage==='trial'){
    const hs=kernelState?.head||{};
    const x=fresh?Number(hs.output_x||0):0;
    const speed=composedSpeed(x,{...output.server,mode:output.mode});
    const dt=lastTick?Math.min(.05,(now-lastTick)/1000):0;lastTick=now;
    position=(position+speed*dt*(output.mode==='gamepad'?160:.15))%560;
    setStyle($('#tuningHorizon'),'transform',`translateX(${-position}px)`);
    const block=hs.horizontal_block_message||hs.estimate_error;
    const handState=hs.hand_mouse?.axes?.horizontal;
    setText($('#tuningTrialStatus'),!fresh?'等待新骨骼画面':isHand()&&!handState?.engaged?'握拳后左右移动试试':block&&!isHand()?block:`当前 ${speedText(speed,output.mode)}${x<0?' · 向左':x>0?' · 向右':' · 已回中'}`);
    setText($('#tuningTrialTimer'),`${Math.max(0,Math.ceil((trialUntil-now)/1000))} 秒后返回选择；系统控制保持暂停`);
    if(now>=trialUntil)showStage('review');return;
  }
  const signal=fresh?navigationSignal():null;
  if(signal===null){resetNavigation();setText($('#tuningDirection'),fresh?'眼鼻暂时没认清，请看向屏幕':'等待新骨骼画面');return}
  if(baseline===null){
    if(!baselineAt)baselineAt=now;
    baselineSamples.push(signal);
    if(now-baselineAt>=700){
      const values=[...baselineSamples].sort((a,b)=>a-b);
      if(values.at(-1)-values[0]<=3){baseline=values[Math.floor(values.length/2)];nav.reset()}
      else{baselineAt=now;baselineSamples=[]}
    }
    setText($('#tuningDirection'),'看向屏幕，保持自然姿势');return;
  }
  const angle=navSign*(signal-baseline),event=nav.step(angle,now);
  setText($('#tuningDirection'),event.waiting?'先回正':event.tier?`${angle<0?'向左':'向右'} · ${navigationApproximate?'等效 ':''}${Math.abs(angle).toFixed(0)}°`:'已回正');
  setText($('#tuningTier'),(event.tier?['','慢档 · 一次一格','中档 · 连续逐格','快档 · 连续逐格'][event.tier]:'小于 8° 不移动 · 8° 慢 · 15° 中 · 24° 快')+(navigationApproximate?' · 菜单等效角度':''));
  setStyle($('#tuningConfirmFill'),'width',`${(event.progress||0)*100}%`);
  if(event.delta)move(event.delta);
  if(event.confirm)void confirm();
}

export async function openViewTuning(){
  if(dialog.open||opening)return;
  if(zoneEditMode)throw new Error('先保存或取消区域调整，再开始试调');
  returnFocus=document.activeElement;
  // 完成暂停确认才出现可操作的试调界面，不自动开始设备或系统控制。
  opening=true;
  try{
    await setOutput(false);await configurationOperation(async()=>{});await refreshKernel();await reloadViewControlState();
    session++;busy=false;navSource=['roll_tilt','head_responsive'].includes(head.horizontalAlgorithm)?'tilt':'turn';
    navSign=1;dialog.showModal();showStage('choose');paintNav();frame=requestAnimationFrame(tick);
  }finally{opening=false}
}

function close(){
  if(busy){setText($('#tuningError'),'正在保存，完成后即可关闭');return}
  session++;cancelAnimationFrame(frame);dialog.close();resetNavigation();returnFocus?.focus({preventScroll:true});
}
function paintNav(){
  setAttribute($('#tuningNavTilt'),'aria-pressed',String(navSource==='tilt'));
  setAttribute($('#tuningNavTurn'),'aria-pressed',String(navSource==='turn'));
  setText($('#tuningNavInvert'),navSign===1?'反转选择方向':'选择方向已反转');
}
export function initViewTuning(){
  $('#openViewTuning').addEventListener('click',()=>openViewTuning().catch(error=>setText($('#viewControlStatus'),error.message)));
  $('#closeViewTuning').addEventListener('click',close);dialog.addEventListener('cancel',event=>{event.preventDefault();close()});
  for(const [id,source] of [['tuningNavTilt','tilt'],['tuningNavTurn','turn']])$('#'+id).addEventListener('click',()=>{navSource=source;resetNavigation();paintNav()});
  $('#tuningNavInvert').addEventListener('click',()=>{navSign*=-1;resetNavigation();paintNav()});
  $('#tuningTry').addEventListener('click',()=>void applySpeed());
  $('#tuningEndTrial').addEventListener('click',()=>showStage('review'));
  $('#tuningSpeed').addEventListener('input',event=>{value=Number(event.target.value);paintValue();nav.reset()});
  dialog.addEventListener('keydown',event=>{
    if(busy||event.target.matches('input'))return;
    if(['ArrowLeft','ArrowRight','Enter'].includes(event.key)&&stage!=='trial'){
      event.preventDefault();if(event.key==='Enter')void confirm();else move(event.key==='ArrowLeft'?-1:1);
    }
  });
  document.addEventListener('visibilitychange',()=>{if(dialog.open)resetNavigation()});
}
