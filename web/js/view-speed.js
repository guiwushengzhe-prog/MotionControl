// 同一归一化输入经过实际输出后端的限幅；鼠标单位是相对移动计数/秒。
import {$,setText} from './core.js';
import {head,output} from './state.js';

export function composedSpeed(input,{mode='mouse',mouse_speed_x=600,mouse_speed_y=450,gamepad_gain=1}={},axis='x'){
  const value=Number.isFinite(Number(input))?Number(input):0;
  if(mode==='gamepad')return Math.max(-1,Math.min(1,Math.max(-1,Math.min(1,value))*Number(gamepad_gain||0)));
  return Math.max(-2,Math.min(2,value))*Number((axis==='y'?mouse_speed_y:mouse_speed_x)||0);
}

export function speedText(value,mode){
  return mode==='gamepad'?`${Math.round(Math.abs(value)*100)}% 推杆`:`${Math.round(Math.abs(value)).toLocaleString('zh-CN')} 计数/秒`;
}

export function renderSpeedSummary(draftGain=null){
  const config={...(output.server||{}),mode:output.mode};
  if(draftGain!==null)Object.assign(config,{mouse_speed_x:600*draftGain,mouse_speed_y:450*draftGain,gamepad_gain:draftGain});
  const hx=Number($('#speedX')?.value||head.sensitivityX)/100;
  const handSetting=Number($('#handMouseSensitivity')?.value||70)/100;
  const hand=Math.min(1,handSetting);
  const headValue=composedSpeed(hx,config),handValue=composedSpeed(hand,config);
  const gain=config.mode==='gamepad'?Number(config.gamepad_gain||1):Number(config.mouse_speed_x||600)/600;
  const limit=config.mode==='gamepad'&&headValue>=1?'；已到满杆，提高倍率会更早达到满杆':'';
  setText($('#headComposedSpeed'),`头部 ${Math.round(hx*100)}%${config.mode==='gamepad'&&hx>1?'（摇杆输入先限制在满幅）':''} × 输出 ${Math.round(gain*100)}% → 最高 ${speedText(headValue,config.mode)}${limit}`);
  setText($('#handComposedSpeed'),`握拳 ${Math.round(handSetting*100)}%${handSetting>1?'（先限制在满幅）':''} × 输出 ${Math.round(gain*100)}% → 左右最高 ${speedText(handValue,config.mode)}，上下最高 ${speedText(composedSpeed(hand,config,'y'),config.mode)}`);
  const horizontal=$('#viewHorizontalSource')?.value||'off',vertical=$('#viewVerticalSource')?.value||'off';
  const label=value=>value==='left'?'左手':value==='right'?'右手':'头部';
  const parts=[];
  if(horizontal!=='off')parts.push(`左右（${label(horizontal)}）最高 ${speedText(['left','right'].includes(horizontal)?handValue:headValue,config.mode)}`);
  if(vertical!=='off')parts.push(`上下（${label(vertical)}）最高 ${speedText(composedSpeed(['left','right'].includes(vertical)?hand:Math.min(1,head.sensitivityY/100),config,'y'),config.mode)}`);
  setText($('#composedSpeedSummary'),(parts.join(' · ')||'视角控制已关闭')+'。游戏内转动快慢还受游戏自身的灵敏度设置影响。');
}
