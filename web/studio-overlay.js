// 此页只读处理结果；不申请摄像头、不改变配置、不启动视频发送。
import {readStudioFrame} from './js/studio-frame.js';
import {contained,FULL_CROP,sourceSize} from './js/studio-compose.js';
const canvas=document.querySelector('#portrait'),ctx=canvas.getContext('2d');
let generation=0;
function clear(){ctx.clearRect(0,0,canvas.width,canvas.height)}
async function next(){
  const epoch=++generation;
  try{
    let config={};
    const bitmap=await readStudioFrame({onState:state=>{config=state.config||{}}});
    if(!bitmap){clear();return}
    if(epoch!==generation){bitmap.close();return}
    const crop=config.portrait_crop||FULL_CROP,[w,h]=sourceSize(bitmap,crop);
    canvas.width=Math.max(1,Math.round(w));canvas.height=Math.max(1,Math.round(h));
    clear();contained(ctx,bitmap,0,0,canvas.width,canvas.height,crop,config.portrait_zoom);bitmap.close();
  }catch{clear()}
  finally{setTimeout(next,100)}
}
void next();
window.addEventListener('pagehide',()=>{generation++;clear()});
