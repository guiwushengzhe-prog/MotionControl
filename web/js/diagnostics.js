// 设置 → 实验与诊断：骨骼录制、触发录制。
import {$,api,notice,positionPopup,post} from './core.js';

// --- skeleton recording ---------------------------------------------------
// Polls only while something is actually happening, so an idle settings page
// is not making a request every second for nothing.
let poseRecordTimer=null,lastPoseRecordState='';
let poseRecordBusy=false;

function renderPoseRecord(state){
  if(!state)return;
  const label={
    idle:'未录制',waiting:`倒计时 ${Number(state.remaining_s||0).toFixed(1)} 秒`,
    recording:`录制中 ${Number(state.remaining_s||0).toFixed(1)} 秒 · 已 ${state.frames} 帧`,
    saving:'正在保存…',cancelled:'已取消',
    done:`已保存 ${state.frames} 帧`,
    error:state.error||'录制失败',
  }[state.state]||state.state;
  $('#poseRecordStatus').textContent=label;
  if(state.state==='done'&&lastPoseRecordState!=='done')void refreshRecordings();
  lastPoseRecordState=state.state;
  const busy=state.state==='waiting'||state.state==='recording'||state.state==='saving';
  $('#poseRecordBtn').disabled=busy;
  if(busy&&poseRecordTimer==null){
    poseRecordTimer=window.setInterval(()=>void refreshPoseRecord(),400);
  }else if(!busy&&poseRecordTimer!=null){
    window.clearInterval(poseRecordTimer);poseRecordTimer=null;
  }
}

export async function refreshPoseRecord(){
  if(poseRecordBusy)return;
  poseRecordBusy=true;
  try{const data=await api('/api/pose/record');renderPoseRecord(data.recording)}catch{}
  finally{poseRecordBusy=false}
}

// 姿态点、视频和原始深度分别显示，方便判断要清理哪一类。
function formatBytes(n){return n>=1048576?`${(n/1048576).toFixed(1)} MB`:`${Math.max(1,Math.round(n/1024))} KB`}
export async function refreshRecordings(){
  try{
    const data=await api('/api/recordings');
    $('#recordingsRow').hidden=!data.count&&!data.bytes;
    const parts=[];
    for(const [key,name] of [['pose','姿态点'],['video','视频'],['depth','深度']]){
      const group=data[key];
      if(group?.bytes||group?.count)parts.push(`${name} ${group.count} 段 · ${formatBytes(group.bytes)}`);
    }
    if(data.other?.bytes)parts.push(`其他 ${formatBytes(data.other.bytes)}`);
    $('#recordingsSummary').textContent=parts.length?parts.join('； '):(data.count?`${data.count} 段 · ${formatBytes(data.bytes)}`:'');
  }catch{}
}

let triggerRecordSaving=false,triggerRecordChoices=[],triggerRecordState=null,triggerRecordChoiceKey='',lastSavedClips=-1;

export function renderTriggerRecord(state){
  if(!state||triggerRecordSaving)return;
  triggerRecordState=state;
  const config=state.config||{},selected=config.triggers||[];
  $('#triggerRecordEnabled').checked=!!config.enabled;
  const names=new Map(triggerRecordChoices.map(item=>[item.key,item.name]));
  $('#triggerRecordSummary').textContent=selected.length?selected.map(key=>names.get(key)||key).join('、'):'选择动作';
  const choices=[...triggerRecordChoices,...selected.filter(key=>!names.has(key)).map(key=>({key,name:key+'（暂不可用）'}))];
  const signature=JSON.stringify(choices);
  const menu=$('#triggerRecordChoices');
  if(signature!==triggerRecordChoiceKey){
    triggerRecordChoiceKey=signature;menu.replaceChildren();
    for(const choice of choices){
      const option=document.createElement('button');option.type='button';option.className='trigger-record-option';
      option.dataset.trigger=choice.key;option.textContent=choice.name;option.setAttribute('role','option');menu.append(option);
    }
  }
  for(const option of menu.children){const chosen=selected.includes(option.dataset.trigger);option.classList.toggle('selected',chosen);option.setAttribute('aria-selected',String(chosen))}
  const phase={off:'未开启',waiting:selected.length?'监听中，等待选中触发':'请先选择要保存的触发',recording:'正在录制选中触发',tail:'保留收尾，等待相邻片段',error:state.error||'保存失败'}[state.state]||state.state;
  $('#triggerRecordStatus').textContent=phase+` · 本次已保存 ${state.saved_clips||0} 段`+(state.saving?' · 正在保存':'');
  if((state.saved_clips||0)!==lastSavedClips){lastSavedClips=state.saved_clips||0;void refreshRecordings()}
  // 不摆路径：存在 C 盘的用户目录里，路径又长又吓人。要找就点「打开文件夹」。
  $('#triggerRecordFile').textContent=state.file?'最近一段已保存':'';
}

export async function refreshTriggerRecord(){
  const data=await api('/api/pose/trigger-recording');
  triggerRecordChoices=data.choices||[];renderTriggerRecord(data.recording);
}

async function saveTriggerRecord(payload){
  if(triggerRecordSaving)return;
  triggerRecordSaving=true;$('#triggerRecordEnabled').disabled=true;
  for(const option of $('#triggerRecordChoices').children)option.disabled=true;
  try{const data=await post('/api/pose/trigger-recording',payload);triggerRecordState=data.recording}
  catch(error){notice('录制设置未保存：'+error.message)}
  finally{
    triggerRecordSaving=false;$('#triggerRecordEnabled').disabled=false;
    renderTriggerRecord(triggerRecordState);for(const option of $('#triggerRecordChoices').children)option.disabled=false;
  }
}

$('#triggerRecordEnabled').addEventListener('change',()=>void saveTriggerRecord({enabled:$('#triggerRecordEnabled').checked}));

$('#triggerRecordChoices').addEventListener('click',event=>{
  const option=event.target.closest('.trigger-record-option');if(!option||triggerRecordSaving)return;
  const key=option.dataset.trigger,current=triggerRecordState?.config||{},selected=current.triggers||[];
  const next=selected.includes(key)?selected.filter(item=>item!==key):[...selected,key];
  void saveTriggerRecord({triggers:next,enabled:!!current.enabled&&next.length>0});
});

function placeTriggerRecordMenu(){
  const picker=$('#triggerRecordPicker');if(!picker.open)return;
  const rect=$('#triggerRecordSummary').getBoundingClientRect(),menu=$('#triggerRecordChoices');
  menu.style.width=rect.width+'px';positionPopup($('#triggerRecordSummary'),menu);
}

$('#triggerRecordPicker').addEventListener('toggle',()=>{if($('#triggerRecordPicker').open){placeTriggerRecordMenu();void refreshTriggerRecord().catch(error=>notice(error.message))}});

window.addEventListener('resize',placeTriggerRecordMenu);
document.addEventListener('scroll',placeTriggerRecordMenu,true);
window.visualViewport?.addEventListener('resize',placeTriggerRecordMenu);
window.visualViewport?.addEventListener('scroll',placeTriggerRecordMenu);

document.addEventListener('click',event=>{if(!event.target.closest('#triggerRecordPicker'))$('#triggerRecordPicker').open=false});

document.addEventListener('keydown',event=>{if(event.key==='Escape')$('#triggerRecordPicker').open=false});

export async function startPoseRecord(){
  try{
    const data=await post('/api/pose/record',{delay_s:3,duration_s:15});
    renderPoseRecord(data.recording);
  }catch(error){$('#poseRecordStatus').textContent=error.message||'无法开始录制'}
}

export async function cancelPoseRecord(){
  try{
    const data=await post('/api/pose/record',{cancel:true});
    renderPoseRecord(data.recording);
  }catch(error){$('#poseRecordStatus').textContent=error.message||'取消失败'}
}
