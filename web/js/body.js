// 设置 → 身体识别：量身、区域怎么算按下、录我的动作、原地踏步。
import {$,escapeHtml,flashStatus,notice,post} from './core.js';
import {setOutput} from './devices.js';
import {profileTriggers} from './labels.js';
import {kernelState,renderKernelState,zoneEditMode} from './play.js';
import {runAction,tutorial} from './shell.js';
import {handMouseConfig} from './view-control.js';

// 量身：开始前先停掉游戏控制，和校准一样——人要挥手、伸脚、跳，别让游戏里跟着乱按。
export async function zoneFit(action,body={}){
  try{
    if(action==='start'||action==='reset')await setOutput(false);
    renderKernelState(await post('/api/zones/fit/'+action,body));
    if(action==='reset')notice('区域已恢复默认，固定或跟随方式保持原样。');
  }catch(e){notice((action==='reset'?'恢复默认失败：':'量身没开始：')+(e?.message||e))}
}

// 开着握拳控制的是哪几只手。量身的握拳那一步只量这几只。
export function fistHands(){
  if(!handMouseConfig.enabled)return[];
  return[...new Set(['horizontal','vertical'].map(axis=>handMouseConfig[axis+'_hand']).filter(hand=>hand==='left'||hand==='right'))];
}

// 设置页「量身」那一块：量没量过、哪天量的。
export function renderZoneFit(k=kernelState||{}){
  const fit=k.zone_fit,status=$('#zoneFitStatus');if(!fit||!status)return;
  const day=t=>{const d=new Date(Number(t)*1000);return`${d.getMonth()+1}月${d.getDate()}日`};
  const zones=!fit.custom?'区域：默认大小':fit.measured_at_unix?`区域：${day(fit.measured_at_unix)}按你的身体量过`:'区域：按你的身体量过';
  const hands=fistHands();
  const grip=!hands.length?'握拳控制没开':fit.grip_measured_at_unix?`握拳：${day(fit.grip_measured_at_unix)}量过`:'握拳：还没量过，认不准就量一下';
  const text=`${zones}${k.zones_frozen?' · 圈固定，量身可直接更新':''} · ${grip}`;if(status.textContent!==text)status.textContent=text;
  $('#zoneFitResetRow').hidden=!fit.custom;
  $('#zoneFitGripRow').hidden=!hands.length;
}

// 定住了没有、区域怎么算按下：电脑那边说了算，这里只照着画。
export function renderZoneFreeze(k=kernelState||{}){
  const frozen=!!k.zones_frozen;
  const follow=$('#followZonesBtn');if(follow)follow.hidden=!frozen||zoneEditMode;
  const adjust=$('#adjustZonesBtn');
  if(adjust){const text=frozen?'调整定住的区域':'挪动区域';if(adjust.textContent!==text)adjust.textContent=text}
  const mode=$('#zoneTriggerMode');
  if(mode&&k.zone_trigger_mode&&document.activeElement!==mode&&mode.value!==k.zone_trigger_mode)mode.value=k.zone_trigger_mode;
  for(const [id,key] of [['verticalDeadzone','deadzone']]){
    const input=$('#'+id),value=Number(k.vertical_look?.[key]);
    if(!input||!Number.isFinite(value)||document.activeElement===input)continue;
    const percent=String(Math.round(value*100));
    if(input.value!==percent){input.value=percent;$('#'+id+'Value').textContent=percent+'%'}
  }
  renderBodyDescs();
}

// 二选一的设置底下一句话，跟着选的那个变。
const ZONE_MODE_DESC={smart:'分辨是故意伸进框，还是做动作时顺路扫过；最多多等 0.25 秒',simple:'进框就按、出框就松；反应最快，也最容易误按'};

const MARCH_DESC={legacy:'稳，停步稍慢',responsive:'交替抬脚时提前响应，停步更快'};

export function renderBodyDescs(){
  const mode=$('#zoneTriggerMode')?.value||'smart';
  const desc=$('#zoneTriggerDesc');if(desc&&desc.textContent!==ZONE_MODE_DESC[mode])desc.textContent=ZONE_MODE_DESC[mode]||'';
  // 「录我的动作」只对智能判定有用；选了进去就按，这一块就收起来。
  $('#zoneTriggerSettings')?.classList.toggle('simple-mode',mode==='simple');
  const march=$('#marchDesc'),text=MARCH_DESC[$('#marchAlgorithm')?.value]||'';if(march&&march.textContent!==text)march.textContent=text;
  syncSegSelects();
}

// 二选一的下拉换成分段按钮。下拉还在（藏着），读写、存盘的代码照旧用它。
function syncSegSelects(){
  for(const select of document.querySelectorAll('select.seg-select')){
    let seg=select.nextElementSibling?.classList.contains('seg')?select.nextElementSibling:null;
    if(!seg){
      seg=document.createElement('div');seg.className='seg';seg.setAttribute('role','radiogroup');seg.setAttribute('aria-label',select.getAttribute('aria-label')||'');
      for(const option of select.options){
        const button=document.createElement('button');button.type='button';button.dataset.value=option.value;button.textContent=option.textContent;
        button.addEventListener('click',()=>{if(select.disabled||select.value===option.value)return;select.value=option.value;select.dispatchEvent(new Event('change',{bubbles:true}));renderBodyDescs()});
        seg.append(button);
      }
      select.after(seg);
    }
    for(const button of seg.children){button.setAttribute('aria-pressed',String(button.dataset.value===select.value));button.disabled=select.disabled}
  }
}

// 「录我的动作」：开始前先停掉游戏控制，和量身一样——人要做一整套动作，别让游戏里跟着乱按。
export async function intentAction(action,body={}){
  try{
    if(action==='start')await setOutput(false);
    renderKernelState(await post('/api/intent/'+action,body));
  }catch(e){notice('录我的动作：'+(e?.message||e))}
}

const ZONE_BODY_CN={leftHand:'左手框',rightHand:'右手框',headJump:'头顶框',leftFoot:'左脚框',rightFoot:'右脚框'};

// 设置页那一块：录过几项、还差哪几项、后台算到哪了、体检报告。
export function renderIntent(k=kernelState||{}){
  const items=k.intent_items,status=$('#intentStatus');if(!items||!status)return;
  const learning=k.zone_learning||{},rec=k.intent_recording||{};
  const all=items.all||[],missing=items.missing||[],done=all.length-missing.length;
  const missingActions=all.filter(item=>item.kind==='action'&&missing.includes(item.key)).map(item=>item.name);
  let text=!done?'还没录过，智能判定先按动作说明来分'
    :`录过 ${done} / ${all.length} 项`+(missingActions.length?`；还没录：${missingActions.slice(0,4).join('、')}${missingActions.length>4?' 等':''}`:'');
  if(rec.active)text='正在录…';
  else if(rec.saving)text='正在保存…';
  else if(learning.state==='computing')text+=' · 正在按现在的框重新算…';
  else if(learning.state==='error')text+=' · 算的时候出错了：'+learning.error;
  if(status.textContent!==text)status.textContent=text;
  const btn=$('#intentRecordBtn');if(btn){const label=!done?'录我的动作':missing.length?'补录没录的':'重新录一遍';if(btn.textContent!==label)btn.textContent=label}
  renderIntentReport(learning.report);
}

function renderIntentReport(report){
  const box=$('#intentReport'),body=$('#intentReportBody');if(!box||!body)return;
  const key=JSON.stringify(report||null);if(box.dataset.key===key)return;box.dataset.key=key;
  box.hidden=!report;if(!report){body.replaceChildren();return}
  const sum=report.summary||{},pct=v=>v==null?'—':Math.round(v*100)+'%',ms=v=>v==null?'—':v+' 毫秒';
  const rows=[];
  const line=(cells,bad)=>`<tr${bad?' class="bad"':''}>${cells.map(c=>`<td>${c}</td>`).join('')}</tr>`;
  for(const item of report.actions||[]){
    const miss=Object.entries(item.misfires||{}).map(([zone,n])=>`${ZONE_BODY_CN[zone]||zone} ${n} 次`);
    rows.push(line([escapeHtml(item.name),`做了 ${item.reps} 遍`,miss.length?'误按 '+miss.join('、'):'没误按'],miss.length));
  }
  for(const item of report.zones||[]){
    rows.push(line([(ZONE_BODY_CN[item.zone]||item.zone)+(item.kind==='tap'?'（快速点）':''),`故意按 ${item.attempts} 次`,
      `按出 ${item.pressed} 次`+(item.missed?`，漏 ${item.missed} 次`:'')+` · 慢 ${ms(item.p50_ms)}`],item.missed));
  }
  const idle=Object.entries(report.idle||{}).map(([zone,n])=>`${ZONE_BODY_CN[zone]||zone} ${n} 次`);
  if(idle.length)rows.push(line(['随便动动','',`误按 ${idle.join('、')}：框离站着的位置太近，量一下身`],true));
  body.innerHTML=`<p>用你录的动作、按这个游戏现在的绑定，用智能判定重放了一遍：做动作时误按 ${pct(sum.misfire_rate)}，故意按漏掉 ${pct(sum.miss_rate)}，按下平均比「进去就按」慢 ${ms(sum.p50_ms)}（最慢的一成 ${ms(sum.p95_ms)}）。</p>`
    +(rows.length?`<table>${rows.join('')}</table>`:'<p>这个游戏里没有绑了键、又录过的框和动作。</p>')
    +'<p class="fineprint">只算这个游戏里绑了键的框和动作。样本少的时候一两次就是很大的百分比，看次数比看百分比准。</p>';
}

// 没录过的动作老是误按框：提示去录那一个。点「先不用」这次打开页面就不再提示它。
const misfireDismissed=new Set();

export function renderMisfireHint(k=kernelState||{}){
  const box=$('#misfireHint');if(!box)return;
  const hint=k.zone_misfire_hint;
  const show=!!hint&&!misfireDismissed.has(hint.trigger)&&!k.intent_recording?.active;
  box.hidden=!show;if(!show)return;
  const name=(profileTriggers().find(t=>t.key===hint.trigger)||{}).name||hint.trigger;
  const zones=(hint.zones||[]).map(zone=>ZONE_BODY_CN[zone]||zone).join('、');
  const text=`做「${name}」时误按了 ${zones} ${hint.count} 次。这个动作还没录过，录一下，智能判定就知道它会扫过哪里。`;
  const span=box.querySelector('span');if(span.textContent!==text)span.textContent=text;
  box.dataset.trigger=hint.trigger;
}

$('#misfireHintGo')?.addEventListener('click',e=>{
  const trigger=$('#misfireHint').dataset.trigger;
  tutorial.openLesson('record',e.currentTarget,{keys:['action:'+trigger]});
});

$('#misfireHintDismiss')?.addEventListener('click',()=>{misfireDismissed.add($('#misfireHint').dataset.trigger);renderMisfireHint()});

$('#intentRecordBtn')?.addEventListener('click',e=>{
  const items=kernelState?.intent_items||{},missing=items.missing||[],all=items.all||[];
  // 录过一部分：只补没录的。全录过：整套重来。
  const keys=missing.length&&missing.length<all.length?missing:null;
  tutorial.openLesson('record',e.currentTarget,{keys});
});

$('#marchAlgorithm')?.addEventListener('change',e=>runAction(async()=>{
  const select=e.target,status=$('#marchSaveStatus');select.disabled=true;
  try{
    renderKernelState(await post('/api/march/config',{algorithm:select.value}));
    flashStatus(status,'已保存');
  }catch(error){
    select.value=kernelState?.march_algorithm==='responsive'?'responsive':'legacy';
    status.textContent='未保存：'+error.message;throw error;
  }finally{select.disabled=false}
}));

$('#zoneTriggerMode')?.addEventListener('change',e=>runAction(async()=>{
  const status=$('#zoneTriggerStatus');
  try{
    renderKernelState(await post('/api/zones/trigger-mode',{mode:e.target.value}));
    flashStatus(status,e.target.value==='simple'?'已改成进去就按':'已改成智能');
  }catch(error){status.textContent='没改成：'+error.message;renderZoneFreeze();throw error}
}));
