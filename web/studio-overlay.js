// 此页只读处理结果；不申请摄像头、不改变配置、不启动视频发送。
import {readStudioFrame} from './js/studio-frame.js';
const canvas=document.querySelector('#portrait'),ctx=canvas.getContext('2d');
let generation=0;
function clear(){ctx.clearRect(0,0,canvas.width,canvas.height)}
async function next(){
  const epoch=++generation;
  try{
    const bitmap=await readStudioFrame();
    if(!bitmap){clear();return}
    if(epoch!==generation){bitmap.close();return}
    canvas.width=bitmap.width;canvas.height=bitmap.height;
    clear();ctx.drawImage(bitmap,0,0);bitmap.close();
  }catch{clear()}
  finally{setTimeout(next,100)}
}
void next();
window.addEventListener('pagehide',()=>{generation++;clear()});
