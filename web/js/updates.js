// 版本与更新：更了没有、成没成、这一版多了什么。
// 功能更新（更新日志里写了新增、变更、移除）打开界面时弹一次说明；只修问题、
// 系统维护的不弹，「？」菜单底部照样看得到版本和一个简短的更新状态。
import {$,api,notice,post,setClass,setProperty,setText} from './core.js';

let state=null,pollTimer=0,deferTimer=0,shownNotes='';
const READY_KEY='motioncontrol_update_ready_notified';

function remembered(key){try{return localStorage.getItem(key)||''}catch{return ''}}
function remember(key,value){try{localStorage.setItem(key,value)}catch{}}

const RECENT_ROLLBACK_S=3*24*3600;

// 菜单底部只说一个短词：「已是最新」「新版下次打开生效」。出问题的细节放进悬停提示。
export function updateStatusText(s,now=Date.now()/1000){
  if(!s.updatable)return {text:'从源码运行'};
  if(s.state==='checking'||s.state==='unknown')return {text:'检查中…'};
  if(s.staged)return {text:s.staged.version&&s.staged.version!==s.version?`新版 ${s.staged.version} 下次打开生效`:'更新下次打开生效'};
  const last=(s.events||[]).at(-1);
  if(last?.type==='rolled_back'&&now-last.at<RECENT_ROLLBACK_S)return {text:'上次更新没装上，已退回',warn:true};
  if(s.state==='current'||s.state==='none')return {text:'已是最新'};
  if(s.state==='unsigned'||s.state==='refused')return {text:'更新包有问题，未下载',title:s.detail||s.error||''};
  return {text:'暂时没能检查更新',title:s.error||''};
}

// 条目里的 **粗体** 照样加粗；不用 innerHTML，日志里的字一律当文字。
function appendInline(parent,text){
  for(const part of String(text).split(/(\*\*[^*]+\*\*)/)){
    if(!part)continue;
    if(part.startsWith('**')&&part.endsWith('**')){const strong=document.createElement('strong');strong.textContent=part.slice(2,-2);parent.appendChild(strong)}
    else parent.appendChild(document.createTextNode(part.replace(/`/g,'')));
  }
}

function busy(){return !!document.querySelector('dialog[open]')||!$('#tour')?.hidden}

export function openUpdateNotes(){
  const notes=state?.notes;if(!notes)return;
  setText($('#updateNotesTitle'),`已更新到 ${notes.version}`);
  const body=$('#updateNotesBody');body.replaceChildren();
  for(const release of notes.releases){
    const block=document.createElement('section');block.className='update-release';
    if(notes.releases.length>1){const head=document.createElement('h3');head.textContent=release.date?`${release.version} · ${release.date}`:release.version;block.appendChild(head)}
    for(const section of release.sections||[]){
      if(section.title){const title=document.createElement('h4');title.textContent=section.title;block.appendChild(title)}
      if(section.intro){const intro=document.createElement('p');appendInline(intro,section.intro);block.appendChild(intro)}
      if(section.items?.length){
        const list=document.createElement('ul');
        for(const item of section.items){const li=document.createElement('li');appendInline(li,item);list.appendChild(li)}
        block.appendChild(list);
      }
    }
    body.appendChild(block);
  }
  const dialog=$('#updateNotesDialog');
  if(!dialog.open)dialog.showModal();
}

function render(s){
  state=s;
  if(s.version)setText($('#appVersion'),s.version);
  const status=updateStatusText(s),el=$('#updateStatus');
  setText(el,status.text);setClass(el,'warn',!!status.warn);
  if(el)el.title=status.title||'';
  setProperty($('#updateCheckBtn'),'hidden',!s.updatable||s.state==='checking');
  const featureReady=s.staged?.kind==='feature';
  // 只有功能更新才在「？」上点一个提示；系统维护安静进行。
  setClass($('#helpBtn'),'has-update',!!s.notes||featureReady);
  if(featureReady&&remembered(READY_KEY)!==s.staged.version){
    remember(READY_KEY,s.staged.version);notice(`新版本 ${s.staged.version} 已下载（有新功能），下次打开软件时生效`);
  }
  if(s.notes&&shownNotes!==s.notes.version){
    clearTimeout(deferTimer);
    // 教学或别的对话框开着时先不抢，等它们关了再弹。
    if(busy())deferTimer=setTimeout(()=>render(state),3000);
    else{shownNotes=s.notes.version;openUpdateNotes()}
  }
  clearTimeout(pollTimer);
  if(s.state==='checking')pollTimer=setTimeout(()=>void refreshUpdates(),1500);
}

export async function refreshUpdates(){
  const data=await api('/api/app-update');
  render(data);
  return data;
}

let initialized=false;
export function initUpdates(){
  if(initialized)return;initialized=true;
  $('#updateCheckBtn').addEventListener('click',async event=>{
    event.stopPropagation();
    try{render(await post('/api/app-update/check',{}))}catch(error){notice(error.message)}
  });
  $('#closeUpdateNotesBtn').addEventListener('click',()=>$('#updateNotesDialog').close());
  // 关掉（按钮或 Esc）就算看过，下次不再弹。
  $('#updateNotesDialog').addEventListener('close',()=>{
    const version=state?.notes?.version;if(!version)return;
    post('/api/app-update/seen',{version}).then(render).catch(()=>{});
  });
  $('#helpBtn').addEventListener('click',()=>{void refreshUpdates().catch(()=>{})});
}
