// 不依赖任何别的模块的小工具：取元素、调接口、底部提示。

export const $ = s => document.querySelector(s);

export const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));

// 配置切换、安装和库更新共用一条队列。先存草稿，再重建编辑区；急停不走此队列。
let configurationTail=Promise.resolve();
export let configurationBusy=false;
const draftFlushers=new Set();
export function registerDraftFlusher(flush){draftFlushers.add(flush)}

export function configurationOperation(operation,{skipDrafts=[]}={}){
  const execute=async()=>{
    const controls=[...document.querySelectorAll('#mappingFields,#personalVoicePanel input,#personalVoicePanel select,#personalVoicePanel button,#headSettings input,#headSettings select')];
    const disabled=controls.map(el=>el.disabled);
    controls.forEach(el=>{el.disabled=true});
    configurationBusy=true;
    try{
      for(const flush of draftFlushers)if(!skipDrafts.includes(flush))await flush();
      return await operation();
    }finally{configurationBusy=false;controls.forEach((el,index)=>{if(el.isConnected)el.disabled=disabled[index]})}
  };
  const result=configurationTail.then(execute);
  configurationTail=result.catch(()=>{});
  return result;
}

export const isVisible=el=>!!el&&!document.hidden&&!el.closest('[hidden]');

// 真实可见视口也包含软键盘和页面缩放。宽度从弹层本身测，避免 rem 与 px 不一致。
export function positionPopup(anchor,popup){
  const viewport=window.visualViewport;
  const left=viewport?.offsetLeft||0,top=viewport?.offsetTop||0;
  const width=viewport?.width||innerWidth,height=viewport?.height||innerHeight;
  const rect=anchor.getBoundingClientRect(),margin=8,gap=5;
  if(popup.classList.contains('voice-release-picker-menu'))popup.style.width=rect.width+'px';
  popup.style.maxWidth=Math.max(0,width-2*margin)+'px';
  const actualWidth=popup.getBoundingClientRect().width;
  popup.style.left=clamp(rect.left,left+margin,Math.max(left+margin,left+width-actualWidth-margin))+'px';
  const below=Math.max(0,top+height-rect.bottom-gap-margin),above=Math.max(0,rect.top-top-gap-margin);
  popup.style.maxHeight=Math.max(0,Math.max(above,below))+'px';
  popup.style.bottom='auto';
  popup.style.top=(below>=above?rect.bottom+gap:Math.max(top+margin,rect.top-gap-popup.getBoundingClientRect().height))+'px';
}

export function fitCanvas(canvas,aspectRatio=4/3,maxPixels=1600000){
  const box=canvas.getBoundingClientRect();
  if(!box.width||!box.height)return false;
  const ratio=Number.isFinite(aspectRatio)&&aspectRatio>0?aspectRatio:4/3;
  const owner=canvas.ownerDocument?.defaultView||window;
  const dpr=Math.min(2,Math.max(1,owner.devicePixelRatio||1));
  const scale=Math.min(dpr,Math.sqrt(maxPixels/(box.width*box.width/ratio)));
  const width=Math.max(1,Math.floor(box.width*scale)),height=Math.max(1,Math.floor(width/ratio));
  if(canvas.width===width&&canvas.height===height)return false;
  canvas.width=width;canvas.height=height;return true;
}

// 提示浮在页面底部，几秒后自己消失——出了事才出现，事过了就走，不在页面上一直挂着。
// 长的多留一会儿，点一下也能关。
let noticeTimer=0;

export function notice(text){
  const el=$('#notice');clearTimeout(noticeTimer);
  if(!text){el.hidden=true;return}
  el.textContent=text;el.hidden=false;el.classList.remove('show');void el.offsetWidth;el.classList.add('show');
  noticeTimer=setTimeout(()=>{el.hidden=true},Math.min(9000,Math.max(3500,String(text).length*120)));
}

$('#notice').addEventListener('click',()=>notice(''));

export async function api(path,opt={}){
  let response;
  // 8s suits local calls. A cloud call is a download plus an apply on the far
  // side of the internet, so callers may ask for longer rather than being told
  // the local service is unresponsive when it is the network that is slow.
  const {timeoutMs=8000,...init}=opt;
  try{response=await fetch(path,{...init,signal:AbortSignal.timeout(timeoutMs)})}
  catch{throw new Error('本地服务无响应，请检查连接后重试')}
  let data;
  try{data=await response.json()}catch{throw new Error('服务响应无法读取')}
  if(!response.ok||data.ok===false){
    const error=new Error(data.error||`服务请求失败（${response.status}）`);
    error.status=response.status;throw error;
  }
  return data;
}

export async function post(path,data,timeoutMs){return api(path,{method:'POST',headers:{'Content-Type':'application/json; charset=utf-8'},body:JSON.stringify(data),timeoutMs})}

export function formatPerf(value,suffix=''){return value===null||value===undefined||value===''?'—':`${value}${suffix}`}

// 存好了说一句「已保存」，两秒后自己没了；出错的话留着。
export function flashStatus(el,text){
  if(!el)return;el.textContent=text;clearTimeout(el.flashTimer);
  el.flashTimer=setTimeout(()=>{if(el.textContent===text)el.textContent=''},2000);
}

export function escapeHtml(text){return String(text).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

export function autosaver(save,statusId,retryId,onDirty=()=>{}){
  let revision=0,saved=0,flight=null,timer;
  const status=$('#'+statusId),retry=$('#'+retryId);
  async function flush(){
    clearTimeout(timer);
    if(flight){await flight;return flush()}
    if(saved===revision)return;
    const version=revision;status.textContent='正在保存…';retry.hidden=true;
    flight=save();
    try{await flight;saved=version}
    catch(error){status.textContent='没保存上：'+error.message;retry.hidden=false;throw error}
    finally{flight=null}
    if(saved!==revision)return flush();
    onDirty(false);flashStatus(status,'已保存');
  }
  retry.addEventListener('click',()=>flush().catch(()=>{}));
  registerDraftFlusher(flush);
  return {
    dirty(){revision++;onDirty(true);status.textContent='正在保存…';clearTimeout(timer);timer=setTimeout(()=>flush().catch(()=>{}),500)},
    flush,pending:()=>saved!==revision,
  };
}

export function agoText(seconds) {
  if (seconds < 0.8) return '刚刚';
  if (seconds < 60) return `${Math.round(seconds)} 秒前`;
  return `${Math.round(seconds / 60)} 分钟前`;
}
