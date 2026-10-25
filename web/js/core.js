// 不依赖任何别的模块的小工具：取元素、调接口、底部提示。

export const $ = s => document.querySelector(s);
let serviceClosing=false;
document.addEventListener('motioncontrol:shutdown-state',()=>{serviceClosing=true});
let settingsPersistence=null,settingsSignature='';

export function updateSettingsPersistence(state){
  if(!state?.state)return;
  if(settingsPersistence&&((state.generation??0)<(settingsPersistence.generation??0)
    ||(state.instance===settingsPersistence.instance&&(state.version??0)<(settingsPersistence.version??0))))return;
  const signature=JSON.stringify([state.instance,state.version,state.state,state.revision,state.saved_revision,state.attempts,state.error,state.message]);
  if(signature===settingsSignature)return;
  settingsSignature=signature;settingsPersistence=state;
  const banner=$('#settingsSaveWarning'),button=$('#retrySettingsSaveBtn');
  if(banner){
    banner.hidden=['ready','disabled'].includes(state.state);
    if(!banner.hidden){
      banner.querySelector('span').textContent=state.state==='blocked'
        ?`${state.error}。可恢复备份，或保存当前设置；原件会另外保留。`
        :`${state.error}。${state.automatic_retry?`正在自动重试（${state.attempts}/${state.retry_limit}）`:'自动重试已停止，可重新保存。'}`;
      button.textContent=state.state==='blocked'?'保存当前设置':'重新保存';
    }
  }
  document.dispatchEvent(new CustomEvent('motioncontrol:settings-persistence',{detail:state}));
}

export function retrySettingsSave({allowRepair=false}={}){
  return post('/api/settings/retry',{repair:allowRepair&&settingsPersistence?.state==='blocked'});
}

export const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));

// 状态轮询只写变化的字段，保留现有节点、焦点和正在编辑的值。
export function setText(el,value){if(el&&el.textContent!==String(value))el.textContent=String(value)}
export function setProperty(el,name,value){if(el&&el[name]!==value)el[name]=value}
export function setAttribute(el,name,value){if(el&&el.getAttribute(name)!==String(value))el.setAttribute(name,String(value))}
export function setClass(el,name,enabled){if(el&&el.classList.contains(name)!==!!enabled)el.classList.toggle(name,!!enabled)}
const appliedStyles=new WeakMap();
export function setStyle(el,name,value){
  if(!el)return;
  const requested=String(value),current=el.style.getPropertyValue(name),values=appliedStyles.get(el)||new Map(),previous=values.get(name);
  // CSS 会规范化小数等字符串；比较上次实际写入的值，避免同一坐标反复触发布局。
  if(previous?.requested===requested&&previous.applied===current)return;
  if(current!==requested)el.style.setProperty(name,requested);
  values.set(name,{requested,applied:el.style.getPropertyValue(name)});appliedStyles.set(el,values);
}
export function syncChildren(parent,children){
  const focused=parent.contains(document.activeElement)?document.activeElement:null;
  children.forEach((child,index)=>{if(parent.children[index]!==child)parent.insertBefore(child,parent.children[index]||null)});
  while(parent.children.length>children.length)parent.lastElementChild.remove();
  if(focused?.isConnected&&document.activeElement!==focused)focused.focus({preventScroll:true});
}

// 配置切换、安装和库更新共用一条队列。先存草稿，再重建编辑区；急停不走此队列。
let configurationTail=Promise.resolve();
export let configurationBusy=false;
const draftFlushers=new Set();
export function registerDraftFlusher(flush){draftFlushers.add(flush)}

export function configurationOperation(operation,{skipDrafts=[]}={}){
  const execute=async()=>{
    const controls=[...document.querySelectorAll('#mappingFields,#personalVoicePanel input,#personalVoicePanel select,#personalVoicePanel button,#headSettings input,#headSettings select,#sensitivityExperiments input,#sensitivityExperiments select,#sensitivityExperiments button')];
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
  if(popup.classList.contains('voice-release-picker-menu'))setStyle(popup,'width',rect.width+'px');
  setStyle(popup,'max-width',Math.max(0,width-2*margin)+'px');
  const actualWidth=popup.getBoundingClientRect().width;
  setStyle(popup,'left',clamp(rect.left,left+margin,Math.max(left+margin,left+width-actualWidth-margin))+'px');
  const below=Math.max(0,top+height-rect.bottom-gap-margin),above=Math.max(0,rect.top-top-gap-margin);
  setStyle(popup,'max-height',Math.max(0,Math.max(above,below))+'px');
  setStyle(popup,'bottom','auto');
  setStyle(popup,'top',(below>=above?rect.bottom+gap:Math.max(top+margin,rect.top-gap-popup.getBoundingClientRect().height))+'px');
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
  if(serviceClosing)throw new Error('程序已进入退出流程，请重新打开后再操作');
  // 8s suits local calls. A cloud call is a download plus an apply on the far
  // side of the internet, so callers may ask for longer rather than being told
  // the local service is unresponsive when it is the network that is slow.
  const {timeoutMs=8000,...init}=opt;
  const signal=AbortSignal.timeout(timeoutMs),readOnly=['GET','HEAD'].includes((init.method||'GET').toUpperCase());
  for(let attempt=0;attempt<2;attempt++){
    let response,data,error;
    try{response=await fetch(path,{cache:'no-store',...init,signal})}
    catch{error=new Error('本地服务无响应，请检查连接后重试')}
    if(response){
      try{data=await response.json()}catch{error=new Error('服务响应无法读取')}
      if(data?.settings_persistence)updateSettingsPersistence(data.settings_persistence);
      if(!error&&(!response.ok||data.ok===false)){
        error=new Error(data.error||`服务请求失败（${response.status}）`);error.status=response.status;
        error.applied=data.applied===true;error.persistence=data.settings_write||data.settings_persistence;
      }
    }
    if(!error)return data;
    // 只重试无副作用的读取，共用原来的总超时；保存、添加、删除不能重复执行。
    const transient=!response||[502,503,504].includes(response.status);
    if(!readOnly||attempt||!transient||signal.aborted)throw error;
    await new Promise(resolve=>setTimeout(resolve,250));
  }
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
  let revision=0,saved=0,flight=null,timer,pendingWrite=null;
  const status=$('#'+statusId),retry=$('#'+retryId);
  function acknowledgePending(state){
    if(state&&pendingWrite&&state.instance!==pendingWrite.instance){
      status.textContent='服务已重启，修改尚未保存';retry.hidden=false;return false;
    }
    if(!state||!pendingWrite||state.instance!==pendingWrite.instance)return false;
    const committed=pendingWrite.targets?.length
      ?pendingWrite.targets.every(target=>state.components?.some(part=>part.instance===target.instance&&part.saved_revision>=target.revision))
      :state.state==='ready'&&state.saved_revision>=pendingWrite.revision;
    if(!committed)return false;
    saved=Math.max(saved,pendingWrite.localVersion);pendingWrite=null;
    if(saved===revision){retry.hidden=true;onDirty(false);flashStatus(status,'已保存')}
    return true;
  }
  async function flush(){
    clearTimeout(timer);
    if(flight){await flight;return flush()}
    if(saved===revision)return;
    if(pendingWrite&&pendingWrite.localVersion===revision)throw pendingWrite.error;
    const version=revision;status.textContent='正在保存…';retry.hidden=true;
    flight=save();
    try{await flight;saved=version;pendingWrite=null}
    catch(error){
      pendingWrite=error.applied&&error.persistence?.instance
        ?{localVersion:version,instance:error.persistence.instance,revision:error.persistence.revision,targets:error.persistence.targets,error}:null;
      if(!acknowledgePending(settingsPersistence)){
        status.textContent=pendingWrite?(error.persistence.automatic_retry?'已应用，等待自动保存…':'已应用，尚未保存'):'没保存上：'+error.message;
        retry.hidden=false;throw error;
      }
    }
    finally{flight=null}
    if(saved!==revision)return flush();
    onDirty(false);flashStatus(status,'已保存');
  }
  document.addEventListener('motioncontrol:settings-persistence',event=>{
    acknowledgePending(event.detail);
  });
  retry.addEventListener('click',()=>{
    const retryPending=pendingWrite&&pendingWrite.localVersion===revision&&pendingWrite.instance===settingsPersistence?.instance;
    if(pendingWrite&&pendingWrite.instance!==settingsPersistence?.instance)pendingWrite=null;
    const task=retryPending?retrySettingsSave():flush();
    task.catch(error=>{status.textContent='没保存上：'+error.message});
  });
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
