// 设置 → 实验与诊断：骨骼录制、触发录制。
import {$,api,notice,positionPopup,post} from './core.js';

// --- skeleton recording ---------------------------------------------------
// Polls only while something is actually happening, so an idle settings page
// is not making a request every second for nothing.
let poseRecordTimer=null,lastPoseRecordState='',poseRecordRevision=0;
let poseRecordBusy=false,poseRecordResultTimer=null;

function renderPoseRecord(state){
  if(!state)return;
  const label={
    idle:'未录制',waiting:`准备倒计时 ${Number(state.remaining_s||0).toFixed(1)} 秒`,
    recording:`录制剩余 ${Number(state.remaining_s||0).toFixed(1)} 秒 · 已 ${state.frames} 帧`,
    saving:'正在保存…',cancelled:'已取消',
    done:Number(state.frames)>0?`已保存 ${state.frames} 帧`:'未采集到骨骼数据，请开始识别后重试',
    error:state.error||'录制失败',
  }[state.state]||state.state;
  $('#poseRecordStatus').textContent=label;
  $('#homePoseRecordStatus').textContent=label;
  const busy=state.state==='waiting'||state.state==='recording'||state.state==='saving';
  const problem=state.state==='error'||(state.state==='done'&&!(Number(state.frames)>0));
  const result=state.state==='done'||state.state==='cancelled';
  const home=$('#homePoseRecorder');
  if(busy||problem||!result){
    window.clearTimeout(poseRecordResultTimer);poseRecordResultTimer=null;
    home.hidden=!busy&&!problem;
  }else if(state.state!==lastPoseRecordState){
    window.clearTimeout(poseRecordResultTimer);poseRecordResultTimer=null;
    home.hidden=!['waiting','recording','saving'].includes(lastPoseRecordState);
    if(!home.hidden)poseRecordResultTimer=window.setTimeout(()=>{home.hidden=true;poseRecordResultTimer=null},4000);
  }
  $('#homePoseRecorder').dataset.state=state.state;
  if(state.state==='done'&&lastPoseRecordState!=='done')void refreshRecordings();
  lastPoseRecordState=state.state;
  $('#poseRecordBtn').disabled=busy;
  const cancellable=state.state==='waiting'||state.state==='recording';
  $('#poseRecordCancelBtn').disabled=!cancellable;
  $('#homePoseRecordCancelBtn').hidden=!cancellable;
  if(busy&&poseRecordTimer==null){
    poseRecordTimer=window.setInterval(()=>void refreshPoseRecord(),400);
  }else if(!busy&&poseRecordTimer!=null){
    window.clearInterval(poseRecordTimer);poseRecordTimer=null;
  }
}

export async function refreshPoseRecord(){
  if(poseRecordBusy)return;
  poseRecordBusy=true;const revision=poseRecordRevision;
  try{const data=await api('/api/pose/record');if(revision===poseRecordRevision)renderPoseRecord(data.recording)}catch{}
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
let triggerRecordDraft=null,triggerRecordRevision=0;

export function renderTriggerRecord(state,local=false){
  if(!state||(triggerRecordSaving&&!local))return;
  if(!local)triggerRecordState=state;
  if(triggerRecordDraft)state={...state,config:triggerRecordDraft};
  const config=state.config||{},selected=config.triggers||[];
  $('#triggerRecordEnabled').checked=!!config.enabled;
  const names=new Map(triggerRecordChoices.map(item=>[item.key,item.name]));
  $('#triggerRecordSummary').textContent=selected.length?selected.map(key=>names.get(key)||key).join('、'):'选择动作（可多选）';
  $('#triggerRecordSummary').title='可同时选择多个动作，点已选项目可取消';
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
  const revision=triggerRecordRevision;
  const data=await api('/api/pose/trigger-recording');
  if(revision!==triggerRecordRevision)return;
  triggerRecordChoices=data.choices||[];renderTriggerRecord(data.recording);
}

async function saveTriggerRecord(payload){
  triggerRecordDraft={...(triggerRecordDraft||triggerRecordState?.config||{}),...payload};
  ++triggerRecordRevision;renderTriggerRecord(triggerRecordState,true);
  if(triggerRecordSaving)return;
  triggerRecordSaving=true;
  try{
    while(triggerRecordDraft){
      const revision=triggerRecordRevision,config={...triggerRecordDraft,triggers:[...(triggerRecordDraft.triggers||[])]};
      try{
        const data=await post('/api/pose/trigger-recording',config);triggerRecordState=data.recording;
        if(revision===triggerRecordRevision)triggerRecordDraft=null;
        ++triggerRecordRevision;
      }catch(error){
        if(revision===triggerRecordRevision){triggerRecordDraft=null;notice('录制设置未保存：'+error.message);break}
      }
    }
  }
  finally{
    triggerRecordSaving=false;renderTriggerRecord(triggerRecordState);
  }
}

$('#triggerRecordEnabled').addEventListener('change',()=>void saveTriggerRecord({enabled:$('#triggerRecordEnabled').checked}));

$('#triggerRecordChoices').addEventListener('click',event=>{
  const option=event.target.closest('.trigger-record-option');if(!option)return;
  const key=option.dataset.trigger,current=triggerRecordDraft||triggerRecordState?.config||{},selected=current.triggers||[];
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
  // 丢弃开始/取消前后仍在途中的旧状态，避免首页倒计时被覆盖。
  ++poseRecordRevision;
  try{
    const data=await post('/api/pose/record',{delay_s:3,duration_s:15});
    ++poseRecordRevision;
    renderPoseRecord(data.recording);
    return true;
  }catch(error){++poseRecordRevision;$('#poseRecordStatus').textContent=error.message||'无法开始录制';return false}
}

export async function cancelPoseRecord(){
  ++poseRecordRevision;
  try{
    const data=await post('/api/pose/record',{cancel:true});
    ++poseRecordRevision;
    renderPoseRecord(data.recording);
  }catch(error){++poseRecordRevision;const message=error.message||'取消失败';$('#poseRecordStatus').textContent=message;$('#homePoseRecordStatus').textContent=message}
}
