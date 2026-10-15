// 侧倾加速曲线：与头控使用同一个幂函数，只调整死区之外的速度。
import {$,clamp,setAttribute,setProperty,setText} from './core.js';
import {head} from './state.js';

const SPAN=18,CENTER_X=320,CENTER_Y=132,HALF_WIDTH=260,HALF_HEIGHT=108,HANDLE_AMOUNT=.6;
let latest={},plotSignature='',dragging=null;

function thresholds(){
  const noise=Math.max(0,Number(latest.noise_tilt_deg)||0),scale=clamp(Number(latest.motion_deadzone_scale)||1,1,1.2);
  if(head.horizontalAlgorithm==='head_responsive'){
    const stop=Math.max(3,SPAN*head.deadzone*1.5,3*noise)*scale;
    return {start:stop+Math.max(.45,.15*noise)*scale,stop};
  }
  const start=Math.max(2,SPAN*head.deadzone,3*noise)*scale;
  return {start,stop:start};
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
  setProperty(panel,'hidden',!visible);if(!visible)return;
  const gamma=clamp(Number(head.horizontalCurveGamma)||2,1,3),speed=Math.max(20,Number(head.sensitivityX)||58);
  const {start,stop}=thresholds(),shownStart=Math.min(SPAN,start);
  const minimum=SPAN*head.deadzone,noise=3*Math.max(0,Number(latest.noise_tilt_deg)||0);
  const noiseLimited=noise>Math.max(head.horizontalAlgorithm==='head_responsive'?3:2,minimum*(head.horizontalAlgorithm==='head_responsive'?1.5:1));
  setText($('#headThresholdStatus'),`${Math.round(head.deadzone*100)}% 是最低稳定区设置 → 当前约 ${start.toFixed(1)}° 起转${noiseLimited?'（当前受校准防抖门槛限制，小幅调整可能不改变角度）':'；滑杆越大，起转门槛越高'}。`);
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
    setText($('#headCurveSummary'),start>=SPAN?'当前稳定区已覆盖 18° 量程，图示范围内不会启动转向。':`横轴：侧倾角度 · 纵轴：转向速度。约 ${start.toFixed(1)}° 起转，18° 达到 ${speed}% 目标速度；灰色为起转前稳定区。`);
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
