// 不依赖任何别的模块的小工具：取元素、调接口、底部提示。

export const $ = s => document.querySelector(s);

export const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));

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
