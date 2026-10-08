// 电脑与手机读取同一份绝对累计；轮询不会重新累计或产生重复步数。
import {api, notice, setText, isVisible} from './core.js';

const pane=document.getElementById('pane-fitness');
if(pane){
  pane.innerHTML=`<h2>运动记录</h2>
    <p class="muted">体感游戏中的身体活动自动记录。热量按动作和体重估算，原地踏步记为步数。</p>
    <div class="fitness-card"><div class="fitness-heading"><h3>这次运动</h3><span id="fitnessStatus">未开始</span></div>
      <div class="fitness-metrics"><div><strong id="fitnessMinutes">0.0</strong><span>活动分钟</span></div><div><strong id="fitnessSteps">0</strong><span>步数</span></div><div><strong id="fitnessKcal">0.0</strong><span>估算活动千卡</span></div></div>
      <div class="fitness-actions"><button id="fitnessStart" class="primary">开始记录</button><button id="fitnessPause" hidden>暂停记录</button><button id="fitnessFinish" hidden>结束这次</button></div>
      <p id="fitnessError" class="muted" role="status"></p>
    </div>
    <div class="fitness-card"><div class="fitness-heading"><h3>每日目标</h3><span id="fitnessLevel">等级 1</span></div>
      <p id="fitnessToday"></p><progress id="fitnessProgress" max="1" value="0" aria-label="每日目标进度"></progress><p id="fitnessCheckins" class="muted"></p>
      <form id="fitnessProfile"><div class="fitness-fields">
        <label>体重（千克）<input name="weight_kg" type="number" min="20" max="300" step="0.1" required></label>
        <label>每日主目标<select name="primary_goal"><option value="minutes">活动分钟</option><option value="steps">步数</option><option value="kcal">估算活动热量</option></select></label>
        <label>活动分钟目标<input name="goal_active_minutes" type="number" min="1" max="300" required></label>
        <label>步数目标<input name="goal_steps" type="number" min="1" max="100000" required></label>
        <label>活动千卡目标<input name="goal_kcal" type="number" min="1" max="5000" required></label>
      </div><button type="submit">保存目标</button></form>
      <p class="muted">默认体重为70千克，请按实际体重修改。达到主目标自动打卡；已完成的打卡会保留。</p>
    </div>
    <div class="fitness-card"><h3>最近运动</h3><div id="fitnessHistory" class="fitness-history">还没有运动记录</div></div>`;
  const style=document.createElement('style');style.textContent=`#pane-fitness .fitness-card{background:var(--card,#1c1c1e);border:1px solid var(--line,#333);border-radius:18px;padding:20px;margin:18px 0}#pane-fitness .fitness-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}#pane-fitness h3{margin:0 0 12px}#pane-fitness .fitness-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:12px 0 22px}#pane-fitness .fitness-metrics div{display:flex;flex-direction:column;gap:6px}#pane-fitness strong{font-size:30px}#pane-fitness .fitness-metrics span{font-size:13px;color:var(--muted,#aaa)}#pane-fitness .fitness-actions{display:flex;gap:12px;flex-wrap:wrap}#pane-fitness .fitness-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:15px;margin:18px 0}#pane-fitness label{display:flex;flex-direction:column;gap:8px}#pane-fitness input,#pane-fitness select{width:100%;box-sizing:border-box}#pane-fitness progress{width:100%;height:12px;accent-color:#0a84ff}#pane-fitness .fitness-history-row{display:flex;justify-content:space-between;gap:14px;padding:13px 0;border-bottom:1px solid var(--line,#333);flex-wrap:wrap}#pane-fitness .muted{color:var(--muted,#aaa);line-height:1.6}@media(max-width:480px){#pane-fitness .fitness-card{padding:15px}#pane-fitness strong{font-size:24px}}`;document.head.append(style);
  const el=id=>document.getElementById(id),form=el('fitnessProfile');
  let state={},editing=false,busy=false,lastHistory='',lastHistoryAt=0;
  const number=n=>Number.isFinite(Number(n))?Number(n):0;
  const minutes=n=>(number(n)/60).toFixed(1);
  function render(data){
    state=data;
    const running=data.status==='active',paused=data.status==='paused';
    setText(el('fitnessStatus'),running?'记录中':paused?'已暂停':data.status==='finished'?'已结束':'未开始');
    setText(el('fitnessMinutes'),minutes(data.active_seconds));setText(el('fitnessSteps'),number(data.steps).toLocaleString());setText(el('fitnessKcal'),number(data.estimated_kcal).toFixed(1));
    el('fitnessStart').hidden=running;setText(el('fitnessStart'),paused?'继续记录':'开始记录');el('fitnessPause').hidden=!running;el('fitnessFinish').hidden=!(running||paused);
    const profile=data.profile||{},today=data.today||{},goal=profile.primary_goal||'minutes';
    const actual=goal==='steps'?number(today.steps):goal==='kcal'?number(today.estimated_kcal):number(today.active_seconds)/60;
    const target=number(profile[{minutes:'goal_active_minutes',steps:'goal_steps',kcal:'goal_kcal'}[goal]])||1;
    el('fitnessProgress').value=Math.min(1,actual/target);
    setText(el('fitnessToday'),`今天活动 ${minutes(today.active_seconds)} 分钟 · ${number(today.steps)} 步 · 估算 ${number(today.estimated_kcal).toFixed(1)} 千卡`);
    setText(el('fitnessLevel'),`等级 ${number(data.level)||1} · ${number(data.experience)} 经验`);
    setText(el('fitnessCheckins'),`目标进度 ${Math.min(100,Math.floor(actual/target*100))}% · 连续打卡 ${number(data.streak)} 天 · 累计 ${(data.checkins||[]).length} 天`);
    setText(el('fitnessError'),data.error|| (running?'请保持体感识别开启；暂停和无有效身体画面时不补算。':''));
    if(!editing&&!form.contains(document.activeElement))for(const field of form.elements)if(field.name&&profile[field.name]!==undefined)field.value=profile[field.name];
  }
  async function control(action,extra={}){
    if(busy)return;busy=true;
    try{render(await api('/api/fitness/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,session_id:state.session_id,...extra})}));}
    catch(error){notice(error.message)}finally{busy=false}
  }
  el('fitnessStart').onclick=()=>control(state.status==='paused'?'resume':'start');el('fitnessPause').onclick=()=>control('pause');el('fitnessFinish').onclick=()=>control('finish');
  form.addEventListener('input',()=>{editing=true});
  form.addEventListener('submit',async event=>{event.preventDefault();const profile=Object.fromEntries([...new FormData(form)].map(([key,value])=>[key,key==='primary_goal'?value:Number(value)]));await control('profile',{profile});editing=false;});
  async function poll(){
    if(!isVisible(pane))return;
    try{
      render(await api('/api/fitness/state'));
      if(Date.now()-lastHistoryAt<5000)return;lastHistoryAt=Date.now();
      const result=await api('/api/fitness/history'),rows=(result.sessions||[]).slice(-12).reverse(),signature=JSON.stringify(rows);
      if(signature===lastHistory)return;lastHistory=signature;
      const container=el('fitnessHistory');container.replaceChildren();
      if(!rows.length){container.textContent='还没有运动记录';return}
      for(const row of rows){const item=document.createElement('div');item.className='fitness-history-row';const date=document.createElement('span'),summary=document.createElement('span');date.textContent=new Date(row.started_at_ms).toLocaleString('zh-CN');summary.textContent=`${minutes(row.active_seconds)} 分钟 · ${number(row.steps)} 步 · 估算 ${number(row.estimated_kcal).toFixed(1)} 千卡`;item.append(date,summary);container.append(item)}
    }catch(error){setText(el('fitnessError'),error.message)}
  }
  setInterval(()=>{void poll()},1000);document.querySelector('#settingsNav [data-pane="fitness"]')?.addEventListener('click',()=>{setTimeout(()=>{void poll()},0)});
}
