// 在完整来源的静态帧上编辑裁切，取消不会改变已保存设置。
import {FULL_CROP} from './studio-compose.js';
const $=id=>document.getElementById(id),clamp=(v,a,b)=>Math.max(a,Math.min(b,v));

export function cropEditor({source,config,persist,status}){
  const dialog=$('studioCropDialog'),box=$('studioCropArea'),selection=$('studioCropSelection');
  let target='portrait',bitmap=null,rect={...FULL_CROP},drag=null,generation=0;
  const key=()=>target==='screen'?'screen_crop':'portrait_crop';
  function render(){
    selection.style.left=rect.x*100+'%';selection.style.top=rect.y*100+'%';
    selection.style.width=rect.width*100+'%';selection.style.height=rect.height*100+'%';
    const edges={left:rect.x,top:rect.y,right:1-rect.x-rect.width,bottom:1-rect.y-rect.height};
    for(const [name,value] of Object.entries(edges))$('studioCrop'+name).value=(value*100).toFixed(1);
  }
  async function choose(next){
    const image=source(next);
    if(!image){status(next==='screen'?'请先选择画面预览，再裁切游戏画面':'请先开启人物预览，等画面出现后再裁切');return false}
    const epoch=++generation;
    const nextBitmap=await createImageBitmap(image);
    if(epoch!==generation){nextBitmap.close();return false}
    target=next;
    bitmap?.close();bitmap=nextBitmap;
    rect={...FULL_CROP,...config()[key()]};
    const canvas=$('studioCropCanvas');
    canvas.width=bitmap.width;canvas.height=bitmap.height;
    box.style.aspectRatio=bitmap.width+'/'+bitmap.height;
    box.style.width=Math.min(640,innerHeight*.5*bitmap.width/bitmap.height)+'px';
    canvas.getContext('2d').drawImage(bitmap,0,0);
    for(const button of dialog.querySelectorAll('[data-crop-source]'))button.setAttribute('aria-pressed',String(button.dataset.cropSource===target));
    render();return true;
  }
  $('studioCropBtn').onclick=async()=>{
    if(await choose(source('screen')?'screen':'portrait')&&!dialog.open)dialog.showModal();
  };
  for(const button of dialog.querySelectorAll('[data-crop-source]'))button.onclick=()=>void choose(button.dataset.cropSource);
  const pointer=event=>{
    const bounds=box.getBoundingClientRect();
    return {x:clamp((event.clientX-bounds.left)/bounds.width,0,1),y:clamp((event.clientY-bounds.top)/bounds.height,0,1)};
  };
  selection.addEventListener('pointerdown',event=>{
    if(event.button!==0)return;
    event.preventDefault();selection.setPointerCapture(event.pointerId);
    drag={at:pointer(event),rect:{...rect},corner:event.target.dataset.corner||''};
  });
  selection.addEventListener('pointermove',event=>{
    if(!drag)return;
    const point=pointer(event),dx=point.x-drag.at.x,dy=point.y-drag.at.y,r=drag.rect;
    if(!drag.corner){
      rect={...r,x:clamp(r.x+dx,0,1-r.width),y:clamp(r.y+dy,0,1-r.height)};
    }else{
      const left=drag.corner.includes('w')?clamp(r.x+dx,0,r.x+r.width-.05):r.x;
      const right=drag.corner.includes('e')?clamp(r.x+r.width+dx,r.x+.05,1):r.x+r.width;
      const top=drag.corner.includes('n')?clamp(r.y+dy,0,r.y+r.height-.05):r.y;
      const bottom=drag.corner.includes('s')?clamp(r.y+r.height+dy,r.y+.05,1):r.y+r.height;
      rect={x:left,y:top,width:right-left,height:bottom-top};
    }
    render();
  });
  selection.addEventListener('lostpointercapture',()=>{drag=null});
  selection.addEventListener('pointerup',event=>{selection.releasePointerCapture(event.pointerId);drag=null});
  for(const name of ['left','top','right','bottom'])$('studioCrop'+name).onchange=event=>{
    const value=Number(event.target.value)/100;if(!Number.isFinite(value)){render();return}
    const right=rect.x+rect.width,bottom=rect.y+rect.height;
    if(name==='left'){rect.x=clamp(value,0,right-.05);rect.width=right-rect.x}
    if(name==='right')rect.width=clamp(1-value,rect.x+.05,1)-rect.x;
    if(name==='top'){rect.y=clamp(value,0,bottom-.05);rect.height=bottom-rect.y}
    if(name==='bottom')rect.height=clamp(1-value,rect.y+.05,1)-rect.y;
    render();
  };
  $('studioCropReset').onclick=()=>{rect={...FULL_CROP};render()};
  $('studioCropCancel').onclick=()=>dialog.close();
  $('studioCropApply').onclick=async()=>{
    const button=$('studioCropApply');button.disabled=true;
    try{await persist({[key()]:{...rect}});dialog.close();status('裁切已应用，预览和录像使用同一范围')}
    catch{}finally{button.disabled=false}
  };
  dialog.addEventListener('close',()=>{generation++;bitmap?.close();bitmap=null;drag=null});
}
