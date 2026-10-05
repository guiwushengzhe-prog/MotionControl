// 版本与更新：更了没有、成没成、这一版多了什么。
// 功能更新（更新日志里写了新增、变更、移除）打开界面时弹一次说明；只修问题、
// 系统维护的不弹，「？」菜单里照样看得到版本、更新时间和上次检查的结果。
import {$,api,notice,post,setClass,setProperty,setText} from './core.js';

let state=null,pollTimer=0,deferTimer=0,shownNotes='';
const READY_KEY='motioncontrol_update_ready_notified';

function remembered(key){try{return localStorage.getItem(key)||''}catch{return ''}}
function remember(key,value){try{localStorage.setItem(key,value)}catch{}}
function day(seconds){if(!seconds)return '';const date=new Date(seconds*1000);return `${date.getMonth()+1}月${date.getDate()}日`}

export function updateStatusText(s){
  if(!s.updatable)return '从源码运行，不自动更新';
  if(s.staged)return s.staged.version&&s.staged.version!==s.version
    ?`新版本 ${s.staged.version} 已下载，下次打开软件时生效`:'更新已下载，下次打开软件时生效';
  if(s.state==='checking'||s.state==='unknown')return '正在检查更新…';
  if(s.state==='current'||s.state==='none')return '已是最新版本';
  if(s.state==='unsigned')return '服务器上的更新包签名不对，没有下载';
  if(s.state==='refused')return '更新包大小异常，没有下载';
  if(s.state==='cancelled')return '更新没下完，下次打开软件时接着下';
  return `检查更新失败${s.error?`：${s.error}`:''}`;
}

export function updateEventText(s){
  const last=(s.events||[]).at(-1);
  if(!last)return '';
  if(last.type==='rolled_back')return `${day(last.at)}的更新没能启动，已自动退回原来的版本`;
  if(last.type==='updated')return last.from&&last.from!==last.to?`${day(last.at)}已从 ${last.from} 更新到 ${last.to}`:`${day(last.at)}已安装更新`;
  return '';
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
  setText($('#updateStatus'),updateStatusText(s));
  const event=updateEventText(s),last=(s.events||[]).at(-1);
  setText($('#updateEvent'),event);setProperty($('#updateEvent'),'hidden',!event);
  setClass($('#updateEvent'),'warn',last?.type==='rolled_back');
  setProperty($('#updateCheckBtn'),'hidden',!s.updatable);
  setProperty($('#updateCheckBtn'),'disabled',s.state==='checking');
  setProperty($('#updateNotesBtn'),'hidden',!s.notes);
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
  $('#updateNotesBtn').addEventListener('click',()=>openUpdateNotes());
  $('#closeUpdateNotesBtn').addEventListener('click',()=>$('#updateNotesDialog').close());
  // 关掉（按钮或 Esc）就算看过，下次不再弹。
  $('#updateNotesDialog').addEventListener('close',()=>{
    const version=state?.notes?.version;if(!version)return;
    post('/api/app-update/seen',{version}).then(render).catch(()=>{});
  });
  $('#helpBtn').addEventListener('click',()=>{void refreshUpdates().catch(()=>{})});
}
