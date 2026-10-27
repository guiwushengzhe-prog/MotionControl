// 电脑与手机读取同一份绝对累计；轮询不会重新累计或产生重复步数。
import {api, notice, setText, isVisible} from './core.js';
import {ComputerHeartRate} from './heart-rate.js';

const pane=document.getElementById('pane-fitness');
if(pane){
  pane.innerHTML=`<h1 class="pane-title">运动记录</h1>
    <p class="pane-sub">查看体感游戏中的活动积累。热量按体重和动作估算。</p>
    <section class="group fitness-section"><h2 class="group-title">运动</h2><div class="fitness-card fitness-list">
    <details class="fitness-details" id="fitnessSessionSection"><summary><span>这次运动</span><span id="fitnessStatus">未开始</span></summary>
      <div class="fitness-metrics"><div><strong id="fitnessMinutes">0.0</strong><span>活动分钟</span></div><div><strong id="fitnessSteps">0</strong><span>步数</span></div><div><strong id="fitnessKcal">0.0</strong><span>估算活动千卡</span></div></div>
      <p class="muted">开始控制时自动记录，暂停控制或停止识别时自动保存本次。有效活动满 3 分钟才计入历史记录和每日累计。</p>
      <p id="fitnessError" class="muted" role="status"></p>
    </details>
    <details class="fitness-details" id="fitnessGoalSection"><summary><span>今日目标</span><span id="fitnessGoalSummary">完成 0%</span></summary><p id="fitnessLevel" class="muted">等级 1</p>
      <p id="fitnessToday"></p><progress id="fitnessProgress" max="1" value="0" aria-label="每日目标进度"></progress><p id="fitnessCheckins" class="muted"></p>
    </details>
    <details class="fitness-details" id="fitnessHistorySection"><summary><span>最近运动</span><span>最近12次</span></summary><div id="fitnessHistory" class="fitness-history">还没有运动记录</div></details>
    </div></section>
    <section class="group fitness-section"><h2 class="group-title">身体与设备</h2><div class="fitness-card fitness-list">
    <details class="fitness-details" id="fitnessHeartSection"><summary><span>手环心率</span><span id="fitnessHeartStatus">未收到心率</span></summary>
      <div class="fitness-metrics"><div><strong id="fitnessHeartNow">—</strong><span>当前 · 次/分</span></div><div><strong id="fitnessHeartAvg">—</strong><span>本次平均 · 次/分</span></div><div><strong id="fitnessHeartMax">—</strong><span>本次最高 · 次/分</span></div></div>
      <p class="muted">在手环上打开心率广播，再用电脑蓝牙连接，也可以通过手机读取。连接后可预览心率，开始控制后计入本次运动。</p>
      <div class="fitness-actions"><button class="btn" id="fitnessHeartConnect" type="button">连接手环</button><button class="btn" id="fitnessHeartDisconnect" type="button" hidden>断开</button><button class="btn" id="fitnessPhoneConnect" type="button">通过手机读取</button></div>
      <p id="fitnessHeartConnection" class="muted" role="status"></p>
    </details>
    <details class="fitness-details" id="fitnessProfileSection"><summary><span>身体数据与目标</span><span>体重、每日目标</span></summary>
      <form id="fitnessProfile"><div class="fitness-fields">
        <div class="row"><label class="row-main" for="fitnessWeight"><span class="name">体重</span><span class="sub">用于估算活动热量，单位千克</span></label><input id="fitnessWeight" name="weight_kg" type="number" min="20" max="300" step="0.1" required></div>
        <div class="row"><label class="row-main" for="fitnessAge"><span class="name">年龄</span><span class="sub">可选，结合心率估算热量</span></label><input id="fitnessAge" name="age" type="number" min="10" max="100" step="1" placeholder="不填"></div>
        <div class="row"><label class="row-main" for="fitnessSex">性别</label><select id="fitnessSex" name="sex"><option value="">不填</option><option value="male">男</option><option value="female">女</option></select></div>
        <div class="row"><label class="row-main" for="fitnessGoal">每日主目标</label><select id="fitnessGoal" name="primary_goal"><option value="minutes">活动分钟</option><option value="steps">步数</option><option value="kcal">估算活动热量</option></select></div>
        <div class="row"><label class="row-main" for="fitnessGoalMinutes">活动分钟目标</label><input id="fitnessGoalMinutes" name="goal_active_minutes" type="number" min="1" max="300" required></div>
        <div class="row"><label class="row-main" for="fitnessGoalSteps">步数目标</label><input id="fitnessGoalSteps" name="goal_steps" type="number" min="1" max="100000" required></div>
        <div class="row"><label class="row-main" for="fitnessGoalKcal">活动千卡目标</label><input id="fitnessGoalKcal" name="goal_kcal" type="number" min="1" max="5000" required></div>
      </div><div class="fitness-actions"><button class="btn" type="submit">保存身体数据与目标</button></div></form>
      <p class="muted">默认体重为70千克，请按实际体重修改。年龄、性别可以不填，读到手环心率时用来估热量。达到主目标自动打卡；已完成的打卡会保留。</p>
    </details>
    </div></section>
    <section class="group fitness-section"><h2 class="group-title">数据同步</h2><div class="fitness-card fitness-list">
    <details class="fitness-details" id="fitnessCloudSection"><summary><span>云端同步</span><span id="fitnessCloudState"></span></summary>
      <p id="fitnessCloudText" class="muted"></p>
      <div class="fitness-actions"><button class="btn" id="fitnessCloudLogin">登录账号</button><button class="btn" id="fitnessCloudCancel" hidden>取消</button><button class="btn" id="fitnessCloudLogout" hidden>退出登录</button></div>
    </details></div></section>`;
  const el=id=>document.getElementById(id),form=el('fitnessProfile');
  let state={},editing=false,busy=false,lastHistory='',lastHistoryAt=0;
  const heart=new ComputerHeartRate(renderHeartConnection);
  function renderHeartConnection(){
    el('fitnessHeartConnect').hidden=heart.active;
    el('fitnessHeartConnect').disabled=!heart.supported;
    el('fitnessHeartDisconnect').hidden=!heart.active;
    setText(el('fitnessHeartConnection'),heart.connected?`电脑蓝牙 · ${heart.name}`:
      heart.message||(!heart.supported?'当前浏览器不支持蓝牙连接，请用电脑上的 Chrome 或 Edge 浏览器打开，也可通过手机读取':
        state.heart_rate_source==='computer'?'电脑蓝牙 · 等待连接手环':'手机读取 · 在手机的运动记录中连接手环'));
  }
  renderHeartConnection();
  const number=n=>Number.isFinite(Number(n))?Number(n):0;
  // 向下显示，避免尚不足三分钟的活动提前显示为 3.0 分钟。
  const minutes=n=>(Math.floor(Math.max(0,number(n))/6)/10).toFixed(1);
  function render(data){
    if(state.session_id!==data.session_id||state.status!==data.status)lastHistoryAt=0;
    state=data;
    const running=data.status==='active',paused=data.status==='paused';
    const status=running?'记录中':paused?'已暂停':data.status==='finished'?(data.session_counted?'已保存':'不足 3 分钟，未计入'):'未开始';
    const sessionSummary=number(data.active_seconds)>0||number(data.steps)>0?` · ${minutes(data.active_seconds)} 分钟 · ${number(data.steps).toLocaleString()} 步`:'';
    setText(el('fitnessStatus'),status+sessionSummary);
    setText(el('fitnessMinutes'),minutes(data.active_seconds));setText(el('fitnessSteps'),number(data.steps).toLocaleString());setText(el('fitnessKcal'),number(data.estimated_kcal).toFixed(1));
    const profile=data.profile||{},today=data.today||{},goal=profile.primary_goal||'minutes';
    const actual=goal==='steps'?number(today.steps):goal==='kcal'?number(today.estimated_kcal):number(today.active_seconds)/60;
    const target=number(profile[{minutes:'goal_active_minutes',steps:'goal_steps',kcal:'goal_kcal'}[goal]])||1;
    el('fitnessProgress').value=Math.min(1,actual/target);
    setText(el('fitnessGoalSummary'),`完成 ${Math.min(100,Math.floor(actual/target*100))}% · 连续 ${number(data.streak)} 天`);
    setText(el('fitnessToday'),`今天活动 ${minutes(today.active_seconds)} 分钟 · ${number(today.steps)} 步 · 估算 ${number(today.estimated_kcal).toFixed(1)} 千卡`);
    setText(el('fitnessLevel'),`等级 ${number(data.level)||1} · ${number(data.experience)} 经验`);
    setText(el('fitnessCheckins'),`目标进度 ${Math.min(100,Math.floor(actual/target*100))}% · 连续打卡 ${number(data.streak)} 天 · 累计 ${(data.checkins||[]).length} 天`);
    const bpm=data.heart_rate,hasHeart=Number.isFinite(bpm)&&bpm>0;
    setText(el('fitnessHeartStatus'),hasHeart?`${Math.round(bpm)} 次/分`:'未收到心率');
    for(const [id,value]of [['fitnessHeartNow',bpm],['fitnessHeartAvg',data.hr_avg],['fitnessHeartMax',data.hr_max]])
      setText(el(id),Number.isFinite(value)&&value>0?Math.round(value):'—');
    renderHeartConnection();
    setText(el('fitnessError'),data.error||(running?'请保持体感识别开启；暂停和无有效身体画面时不补算。':''));
    if(!editing&&!form.contains(document.activeElement))for(const field of form.elements)if(field.name&&profile[field.name]!==undefined)field.value=profile[field.name]??'';
  }
  async function control(action,extra={}){
    if(busy)return;busy=true;
    try{render(await api('/api/fitness/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,session_id:state.session_id,...extra})}));}
    catch(error){notice(error.message)}finally{busy=false}
  }
  el('fitnessHeartConnect').onclick=()=>{void heart.connect()};
  el('fitnessHeartDisconnect').onclick=()=>heart.disconnect('手环已断开');
  el('fitnessPhoneConnect').onclick=async()=>{
    try{
      const data=await api('/api/fitness/heart-rate-input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:'phone'})});
      heart.disconnect();render(data);
    }catch(error){notice(error.message);return}
    document.querySelector('nav [data-view="devices"]')?.click();
    document.querySelector('#settingsNav [data-pane="devices"]')?.click();
    const button=document.getElementById('phoneConnectBtn');
    if(!isVisible(button))return;
    button.click();
    const panel=document.getElementById('phoneConnectPanel');
    panel.focus({preventScroll:true});panel.scrollIntoView({block:'start',behavior:'instant'});
  };
  el('homeFitnessGo')?.addEventListener('click',()=>{
    document.querySelector('[data-view="devices"]')?.click();
    document.querySelector('#settingsNav [data-pane="fitness"]')?.click();
  });
  function renderHome(data){
    const active=data.control_enabled===true;
    const today=data.today||{};
    const values=active?data:today;
    setText(el('homeFitnessTitle'),active?'这次运动':'今日运动');
    setText(el('homeFitnessMinutes'),minutes(values.active_seconds));
    setText(el('homeFitnessSteps'),number(values.steps).toLocaleString());
    setText(el('homeFitnessKcal'),number(values.estimated_kcal).toFixed(1));
    const hr=active&&data.heart_rate? ` · 心率 ${data.heart_rate} 次/分`:'';
    const profile=data.profile||{},goal=profile.primary_goal||'minutes';
    const actual=goal==='steps'?number(today.steps):goal==='kcal'?number(today.estimated_kcal):number(today.active_seconds)/60;
    const target=number(profile[{minutes:'goal_active_minutes',steps:'goal_steps',kcal:'goal_kcal'}[goal]])||1;
    setText(el('homeFitnessHint'),data.error||(active?'自动记录中'+hr:
      `今日目标 ${Math.min(100,Math.floor(actual/target*100))}%`));
  }
  form.addEventListener('input',()=>{editing=true});
  // 年龄、性别不填就是 null；别的都是数（主目标是字）。
  const fieldValue=(key,value)=>key==='primary_goal'?value:key==='sex'?(value||null):key==='age'?(value===''?null:Number(value)):Number(value);
  form.addEventListener('submit',async event=>{event.preventDefault();const profile=Object.fromEntries([...new FormData(form)].map(([key,value])=>[key,fieldValue(key,value)]));await control('profile',{profile});editing=false;});
  // 云端同步：登录在浏览器里完成（电脑这边打开网页、显示一个码），这里只看状态。
  const ago=seconds=>seconds<60?'刚刚':seconds<3600?`${Math.floor(seconds/60)} 分钟前`:`${Math.floor(seconds/3600)} 小时前`;
  function renderCloud(account){
    const state=account.state,sync=account.sync||{};
    setText(el('fitnessCloudState'),state==='signed_in'?`已登录 ${account.display_name||''}`:state==='waiting'?'等你在网页里允许':'未登录');
    const synced=sync.last_synced_at?`${ago(Date.now()/1000-sync.last_synced_at)}同步过`:'还没同步';
    setText(el('fitnessCloudText'),account.error||sync.error||(state==='signed_in'?`运动记录和身体数据存在云端，换电脑登录同一个账号就回来了。${synced}。`
      :state==='waiting'?`在打开的网页里登录，看到 ${account.user_code} 点「允许」。网页没打开？用浏览器打开 ${account.verification_uri}`
      :'登录后运动记录和身体数据存到云端，换电脑也在。只传每次锻炼的摘要，不传每秒的心率和画面。'));
    el('fitnessCloudLogin').hidden=state!=='signed_out';el('fitnessCloudCancel').hidden=state!=='waiting';el('fitnessCloudLogout').hidden=state!=='signed_in';
  }
  async function cloud(action){
    try{renderCloud(await api(`/api/cloud/account/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}))}
    catch(error){notice(error.message)}
  }
  el('fitnessCloudLogin').onclick=()=>cloud('login');el('fitnessCloudCancel').onclick=()=>cloud('cancel');el('fitnessCloudLogout').onclick=()=>cloud('logout');
  async function poll(){
    const showPane=isVisible(pane),showHome=isVisible(el('homeFitnessCard'));
    if(!showPane&&!showHome&&!heart.active)return;
    try{
      const data=await api('/api/fitness/state');
      heart.sync(data);
      if(showHome)renderHome(data);
      if(!showPane)return;
      render(data);
      renderCloud(await api('/api/cloud/account'));
      if(Date.now()-lastHistoryAt<5000)return;lastHistoryAt=Date.now();
      const result=await api('/api/fitness/history'),rows=(result.sessions||[]).slice(-12).reverse(),signature=JSON.stringify(rows);
      if(signature===lastHistory)return;lastHistory=signature;
      const container=el('fitnessHistory');container.replaceChildren();
      if(!rows.length){container.classList.add('fitness-empty');container.textContent='运动满3分钟后，本次记录会显示在这里。';return}
      container.classList.remove('fitness-empty');
      for(const row of rows){
        const item=document.createElement('div'),date=document.createElement('div'),time=document.createElement('time'),note=document.createElement('span'),values=document.createElement('div');
        item.className='fitness-history-row';date.className='fitness-history-date';values.className='fitness-history-values';
        const at=new Date(row.started_at_ms),dated=Number.isFinite(at.getTime());time.textContent=dated?at.toLocaleString('zh-CN',{month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'}):'时间未记录';if(dated)time.dateTime=at.toISOString();
        note.textContent=row.hr_avg!=null?`平均心率 ${Math.round(row.hr_avg)} 次/分`:'活动估算';date.append(time,note);
        for(const [value,label] of [[minutes(row.active_seconds),'分钟'],[number(row.steps).toLocaleString(),'步'],[number(row.estimated_kcal).toFixed(1),row.kcal_source==='heart_rate'?'心率估算千卡':'估算千卡']]){
          const metric=document.createElement('div'),strong=document.createElement('strong'),caption=document.createElement('span');strong.textContent=value;caption.textContent=label;metric.append(strong,caption);values.append(metric);
        }
        item.append(date,values);container.append(item);
      }
    }catch(error){if(heart.active)heart.disconnect('后台连接已断开，手环读取已停止');setText(el('fitnessError'),error.message)}
  }
  setInterval(()=>{void poll()},1000);document.querySelector('#settingsNav [data-pane="fitness"]')?.addEventListener('click',()=>{setTimeout(()=>{void poll()},0)});
}
