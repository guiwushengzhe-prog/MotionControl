// 侧倾加速曲线：与头控使用同一个幂函数，只调整死区之外的速度。
import {$,clamp,setAttribute,setProperty,setText} from './core.js';
import {head} from './state.js';

const SPAN=18,CENTER_X=320,CENTER_Y=132,HALF_WIDTH=260,HALF_HEIGHT=108,HANDLE_AMOUNT=.6;
let latest={},plotSignature='',dragging=null;

function thresholds(){
  const noise=Math.max(0,Number(latest.noise_tilt_deg)||0),scale=clamp(Number(latest.motion_deadzone_scale)||1,1,1.2);
  const ready=!!(latest.horizontal_calibrated??latest.calibrated)&&latest.noise_tilt_deg!=null&&Number.isFinite(Number(latest.noise_tilt_deg));
  const responsive=head.horizontalAlgorithm==='head_responsive',floor=responsive?3:2,setting=SPAN*head.deadzone*(responsive?1.5:1);
  const limit=floor>=Math.max(setting,3*noise)-1e-9?'minimum':3*noise>=setting-1e-9?'noise':'setting';
  if(head.horizontalAlgorithm==='head_responsive'){
    const stop=Math.max(floor,setting,3*noise)*scale;
    return {start:stop+Math.max(.45,.15*noise)*scale,stop,ready,limit,scale};
  }
  const start=Math.max(floor,setting,3*noise)*scale;
  return {start,stop:start,ready,limit,scale};
}

function renderThresholdNote({start,stop,ready,limit,scale},visible){
  if(!visible){setText($('#headThresholdStatus'),'转动超过中心稳定区时开始转向；调大更稳，调小更灵敏。');return}
  if(!ready){setText($('#headThresholdStatus'),'校准后显示实际起转角度。');return}
  const angle=`侧倾超过 ${start.toFixed(1)}° 开始转向${head.horizontalAlgorithm==='head_responsive'?`，回到 ${stop.toFixed(1)}° 内停止`:''}`;
  const reason=limit==='noise'?'当前受校准防抖限制，继续调低滑杆不会更灵敏':limit==='minimum'?'已到最低起转角度':'调大更稳，调小更灵敏';
  setText($('#headThresholdStatus'),`${angle}；${reason}。${scale>1?'动作防抖暂时提高起转角度。':''}`);
}

export function horizontalCurveValue(angle,start,stop,gamma,speed){
  if(Math.abs(angle)<start)return 0;
  const amount=clamp((Math.abs(angle)-stop)/Math.max(1,SPAN-stop),0,1);
  return Math.sign(angle)*amount**gamma*speed;
}

export function renderHeadCurve(status){
  if(status)latest=status;
  const panel=$('#headCurvePanel');if(!panel)return;
  const choice=$('#viewHorizontalSource')?.value;
  const visible=['roll_tilt','head_responsive'].includes(choice||head.horizontalAlgorithm);
  const threshold=thresholds(),{start,stop,ready}=threshold;
  renderThresholdNote(threshold,visible);
  setProperty(panel,'hidden',!visible);if(!visible)return;
  const gamma=clamp(Number(head.horizontalCurveGamma)||2,1,3),speed=Math.max(20,Number(head.sensitivityX)||58);
  const shownStart=Math.min(SPAN,start);
  setText($('#headCurveGammaStatus'),gamma===1?'起转后，转动角度越大，速度按比例增加。':'小角度转得慢，大角度提速快；数值越大，前后差别越明显。最高速度不变。');
  setText($('#headCurveSummary'),!ready?'校准后显示实际起转角度；灰色为起转前稳定区预览。':start>=SPAN?'当前稳定区已覆盖 18° 量程，图示范围内不会启动转向。':`约 ${start.toFixed(1)}° 起转，18° 达到 ${speed}% 设定最高速度；灰色为起转前稳定区。`);
  const signature=JSON.stringify([gamma,speed,start,stop]);
  if(signature!==plotSignature){
    plotSignature=signature;
    const x=angle=>CENTER_X+angle/SPAN*HALF_WIDTH,y=value=>CENTER_Y-value/speed*HALF_HEIGHT;
    const path=[];
    for(let i=0;i<=240;i++){
      const angle=-SPAN+2*SPAN*i/240;
      path.push(`${i?'L':'M'}${x(angle).toFixed(2)} ${y(horizontalCurveValue(angle,start,stop,gamma,speed)).toFixed(2)}`);
    }
    setAttribute($('#headCurveLine'),'d',path.join(' '));
    setAttribute($('#headCurveDeadzone'),'x',x(-shownStart));
    setAttribute($('#headCurveDeadzone'),'width',2*shownStart/SPAN*HALF_WIDTH);
    setText($('#headCurveMax'),`${speed}%`);setText($('#headCurveMin'),`−${speed}%`);
    const angle=Math.max(start,stop+HANDLE_AMOUNT*Math.max(1,SPAN-stop));
    for(const handle of panel.querySelectorAll('[data-curve-side]')){
      const side=Number(handle.dataset.curveSide);
      setAttribute(handle,'cx',x(Math.min(SPAN,angle)*side));
      setAttribute(handle,'cy',y(horizontalCurveValue(angle*side,start,stop,gamma,speed)));
      setAttribute(handle,'aria-valuenow',gamma.toFixed(2));
      setAttribute(handle,'aria-valuetext',`弯曲程度 ${gamma.toFixed(2)}，左右同步`);
    }
  }
  const disabled=$('#headCurveGamma').disabled;
  setProperty($('#resetHeadCurve'),'disabled',disabled);
  for(const handle of panel.querySelectorAll('[data-curve-side]'))setAttribute(handle,'aria-disabled',disabled);
}

function changeCurve(value,commit=false){
  const input=$('#headCurveGamma');if(input.disabled)return;
  input.value=String(Math.round(clamp(value,1,3)*20)/20);
  input.dispatchEvent(new Event('input',{bubbles:true}));
  if(commit)input.dispatchEvent(new Event('change',{bubbles:true}));
}

export function initHeadCurve(){
  const plot=$('#headCurvePlot');if(!plot)return;
  const move=event=>{
    if(!dragging||event.pointerId!==dragging.pointerId)return;
    const matrix=plot.getScreenCTM();if(!matrix)return;
    const point=new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
    const amount=clamp(dragging.side*(CENTER_Y-point.y)/HALF_HEIGHT,HANDLE_AMOUNT**3,HANDLE_AMOUNT);
    changeCurve(Math.log(amount)/Math.log(HANDLE_AMOUNT));
  };
  plot.addEventListener('pointerdown',event=>{
    const handle=event.target.closest('[data-curve-side]');
    if(!handle||$('#headCurveGamma').disabled)return;
    event.preventDefault();handle.focus({preventScroll:true});
    dragging={pointerId:event.pointerId,side:Number(handle.dataset.curveSide)};
    handle.setPointerCapture(event.pointerId);
  });
  plot.addEventListener('pointermove',move);
  const finish=event=>{
    if(!dragging||event.pointerId!==dragging.pointerId)return;
    move(event);dragging=null;
    changeCurve(Number($('#headCurveGamma').value),true);
  };
  plot.addEventListener('pointerup',finish);plot.addEventListener('pointercancel',finish);
  plot.addEventListener('keydown',event=>{
    if(!event.target.matches('[data-curve-side]'))return;
    const value=Number($('#headCurveGamma').value),deltas={ArrowUp:-.05,ArrowDown:.05,ArrowLeft:-.05,ArrowRight:.05};
    if(event.key in deltas){event.preventDefault();changeCurve(value+deltas[event.key],true)}
    else if(event.key==='Home'||event.key==='End'){event.preventDefault();changeCurve(event.key==='Home'?1:3,true)}
  });
  $('#resetHeadCurve').addEventListener('click',()=>changeCurve(2,true));
}
