// 本游戏：选游戏、按键映射的每一行、保存。
import {$,api,notice,post} from './core.js';
import {renderOutputMix} from './devices.js';
import {ACTION_TYPE_GROUPS,ACTION_TYPE_LABELS,BASE_PROFILE_TRIGGERS,BINDING_SYSTEM_TARGETS,BODY_ZONES,GAMEPAD_STICK_TARGETS,GAMEPAD_TRIGGER_TARGETS,MOTION_CONFLICT_GROUPS,MOTION_CONFLICT_NAMES,TARGET_LABELS,bindingsForDisplay,macroById,macroLibrary,profileTriggers,targetLabel,triggerKeyLabel} from './labels.js';
import {paintPoseMissingNotice} from './library.js';
import {kernelState,renderKernelZones} from './play.js';
import {currentView,showSettingsPane,showView,togglePanel} from './shell.js';
import {gameProfile,profileDirty} from './state.js';
import {isGameVoiceKey,normalizeGameVoicePhrase,refreshVoiceCommands,voiceCommandId,voiceCommandIds,voiceCommandName,voiceCommandNames,watchVoicePhrase} from './voice.js';

// 换过几次游戏、最后一次搜的是什么。教「换成我要玩的游戏」时，要认的是真的换成了。
export let profileApplies=0;
export let lastProfileQuery='';
let gameLaunch=null;
let profileAutoSaveTimer=null;
let profileFlight=null;
let profileRevision=0;
export let profileSwitching=false;
let profileConflict=false;

// 跳到映射表里的那一行并高亮。组可能是折叠的，得先展开，否则滚过去是一片空。
export function revealBindingRow(triggerKey){
  // 通用口令和内置口令不在本游戏的映射表里：前者在「设置 → 语音」，后者改不了。
  if(triggerKey.startsWith('voice.shared.')){showView('devices');showSettingsPane('voice');return}
  if(triggerKey.startsWith('voice.')&&!triggerKey.startsWith('voice.game.profile_slot_')){notice('这是内置口令，不能改键');return}
  // 从「开始」页点过来的话，映射表所在的页签还藏着——藏着的东西滚不过去，
  // 也高亮不出来。先切过去再找。
  if(currentView!=='games')showView('games');
  // 没绑键的身体动作平时不在表里（见 shownBodyRows），点过来就是要绑它，现加一行。
  const row=document.querySelector(`.binding-row[data-trigger="${triggerKey}"]`)||addBodyRow(triggerKey);
  if(!row){notice('这个动作还没出现在映射表里，刷新一下页面再试');return}
  row.hidden=false;showMapTab(row.closest('.binding-group')?.dataset.group||mapTab);
  row.scrollIntoView({behavior:'smooth',block:'center'});
  row.classList.add('just-found');
  setTimeout(()=>row.classList.remove('just-found'),1600);
  row.querySelector('select')?.focus();
}

const LEGACY_ZONE_ALIASES={leftHand:['leftHandUpper','leftHandLower'],rightHand:['rightHandUpper','rightHandLower']};

function bindingFor(trigger){
  const items=gameProfile.selected?.bindings?.[trigger.group]||{};
  if(Object.prototype.hasOwnProperty.call(items,trigger.id))return items[trigger.id];
  if(trigger.group==='zones'){
    for(const alias of LEGACY_ZONE_ALIASES[trigger.id]||[]){if(Object.prototype.hasOwnProperty.call(items,alias))return items[alias]}
  }
  return trigger.defaultBinding||null;
}

function selectedMotionIdsFromRows(){
  const selected=new Set();
  document.querySelectorAll('.binding-row[data-trigger^="motion."]').forEach(row=>{
    if(row.querySelector('.binding-type')?.value)selected.add(String(row.dataset.trigger).slice('motion.'.length));
  });
  return selected;
}

function motionConflictsForSelection(selected){
  return MOTION_CONFLICT_GROUPS
    .map(group=>group.ids.filter(id=>selected.has(id)))
    .filter(active=>active.length>1);
}

function motionConflictText(conflicts){
  return conflicts.map(group=>group.map(id=>MOTION_CONFLICT_NAMES[id]||id).join('、')).join('；');
}

export function syncMotionConflictChoices(){
  const selected=selectedMotionIdsFromRows();
  const states=new Map();
  const stateFor=id=>{let state=states.get(id);if(!state){state={blockedBy:new Set(),conflictWith:new Set()};states.set(id,state)}return state};
  for(const group of MOTION_CONFLICT_GROUPS){
    const active=group.ids.filter(id=>selected.has(id));
    if(active.length>1){
      for(const id of active){for(const other of active){if(other!==id)stateFor(id).conflictWith.add(other)}}
    }else if(active.length===1){
      for(const id of group.ids){if(id!==active[0])stateFor(id).blockedBy.add(active[0])}
    }
  }
  // 所有身体动作，包括从官方动作库下载的：开合跳、双手举过头都是下载来的，只看程序
  // 自带的那两个，这里的互斥就形同虚设。
  for(const trigger of profileTriggers().filter(item=>item.group==='motions')){
    const row=document.querySelector(`.binding-row[data-trigger="${trigger.key}"]`);if(!row)continue;
    const state=states.get(trigger.id)||{blockedBy:new Set(),conflictWith:new Set()};
    const select=row.querySelector('.binding-type');if(!select)continue;
    const blocked=state.blockedBy.size>0&&!selected.has(trigger.id);
    select.disabled=blocked;
    select.title=blocked?`与 ${[...state.blockedBy].map(id=>MOTION_CONFLICT_NAMES[id]||id).join('、')} 冲突，先取消该动作`:'';
    row.classList.toggle('motion-conflict-blocked',blocked);
    row.classList.toggle('motion-conflict-error',state.conflictWith.size>0);
    const note=row.querySelector('.motion-conflict-note');
    if(note){
      if(state.conflictWith.size){note.hidden=false;note.textContent=`冲突：与 ${[...state.conflictWith].map(id=>MOTION_CONFLICT_NAMES[id]||id).join('、')} 只能选一个`}
      else if(blocked){note.hidden=false;note.textContent=`已禁用：与 ${[...state.blockedBy].map(id=>MOTION_CONFLICT_NAMES[id]||id).join('、')} 冲突`}
      else{note.hidden=true;note.textContent=''}
    }
  }
}

function syncProfileZoneLabels(){
  const zonePad={leftHand:'#padX',rightHand:'#padB',leftFoot:'#padLB',rightFoot:'#padRB',headJump:'#padA'};
  for(const trigger of BASE_PROFILE_TRIGGERS.filter(t=>t.group==='zones')){
    // The region is read at a glance mid-game, so show the button by itself.
    // A profile's descriptive label ("左脚区 · LB") belongs in the mapping list;
    // here it only shrinks the part that matters. The body part stays in <small>.
    const binding=bindingFor(trigger);
    const label=binding&&!binding.disabled?targetLabel(binding.action):'—';
    if(BODY_ZONES[trigger.id])BODY_ZONES[trigger.id].label=label;
    const el=$(zonePad[trigger.id]);if(el)el.textContent=label;
  }
  renderKernelZones(kernelState?.zones||{});
}

function profileMetaText(profile){
  if(!profile)return '未选择游戏';
  const appid=profile.appid||profile.steam_appid||'';
  // 自己加的游戏不是「实验配置」——那句话说的是自动生成的那两百个没人试过，而
  // 这一个是他自己建的，本来就该自己调，说它"实验"只会让人以为是软件出的问题。
  if(profile.source==='custom')return appid?`自己加的 · Steam ${appid}`:'自己加的';
  const source=profile.source||{};
  return [appid?`Steam ${appid}`:'',source.verified?'已验证':''].filter(Boolean).join(' · ');
}

function renderProfileHeader(){
  const p=gameProfile.selected;
  renderOutputMix();
  // 改名和删除只对自己加的那些有意义：内置那两百个删不得也改不得。
  const mine=p?.source==='custom';
  document.querySelectorAll('.custom-only').forEach(el=>{el.hidden=!mine});
  $('#profileGameName').textContent=p?.name||'未选择游戏';
  $('#currentGameName').textContent=p?.name||'未选择游戏';
  $('#profileMeta').textContent=profileMetaText(p);
  // 「自动生成」说的是那两百个没人试过的；自己加的不算。
  $('#profileUnverified').hidden=!p||mine||Boolean(p.source?.verified);
  renderGameLaunch();
  syncProfileZoneLabels();
}

function renderGameLaunch(){
  const status=$('#profileLaunchStatus'),button=$('#profileLaunchBtn');
  // 这个游戏要管理员权限、现在却是普通权限：游戏里可能收不到按键，直接在本游戏页上说。
  const banner=$('#adminBanner');if(banner)banner.hidden=!(gameLaunch?.requires_admin&&!gameLaunch?.is_admin);
  if(!gameProfile.selected||!gameLaunch){status.textContent='';button.disabled=true;return}
  button.disabled=false;
  if(gameLaunch.is_admin){
    status.textContent=gameLaunch.requires_admin?'现在是管理员权限，下次也会用':'现在是管理员权限，下次普通启动';
    button.textContent=gameLaunch.requires_admin?'下次改用普通权限':'记住用管理员权限';
  }else{
    status.textContent=gameLaunch.requires_admin?'已记住用管理员权限，现在还是普通权限':'';
    button.textContent='用管理员权限重启';
  }
}

async function waitForAdminRestart(gameId){
  const deadline=Date.now()+30000;
  while(Date.now()<deadline){
    await new Promise(resolve=>setTimeout(resolve,500));
    try{
      const data=await api('/api/game-profiles/selected?refresh='+Date.now(),{timeoutMs:1200});
      if(data.launch?.is_admin&&data.profile?.selected_id===gameId){location.reload();return}
    }catch{}
  }
  throw new Error('管理员程序尚未启动，请检查 Windows 权限确认或重新打开程序');
}

export async function changeGameLaunchMode(admin){
  if(admin&&!gameLaunch?.is_admin)notice('请在 Windows 权限确认中选择“是”，程序随后会重新连接。');
  const data=await post('/api/game-profiles/launch-mode',{admin},120000);
  gameLaunch=data.launch;renderGameLaunch();
  if(data.restarting){
    notice('已确认管理员权限，正在重新连接…');
    await waitForAdminRestart(data.launch.game_id);
  }else notice(admin?'这个游戏下次启动仍会申请管理员权限。':'这个游戏下次将普通启动。');
}

export async function toggleGameLaunchMode(){
  await changeGameLaunchMode(!(gameLaunch?.requires_admin&&gameLaunch?.is_admin));
}

function renderProfileCatalog(games){
  gameProfile.catalog=Array.isArray(games)?games:[];
  const select=$('#profileSelect'),selectedId=gameProfile.selected?.selected_id||gameProfile.selected?.id||'';
  select.replaceChildren();
  if(!gameProfile.catalog.length){const o=document.createElement('option');o.value='';o.textContent='没有匹配的游戏';select.appendChild(o);return}
  // Hand-verified profiles come first: of ~200 shipped profiles only a
  // handful have actually been played, and a flat alphabetical list makes
  // an auto-generated one look as official as a tested one.
  // 自己加的排在最前，比「已验证」还靠前：会来翻这个列表的人多半就是为了找自己
  // 加的那个，而内置那两百个可以搜。服务端的 list_games 已经这么排了，前端又按
  // verified 重排一遍，等于把它推回两百条里去。
  const rank=g=>g.source==='custom'?2:(g.verified?1:0);
  const ordered=[...gameProfile.catalog].sort((a,b)=>rank(b)-rank(a)||String(a.name).localeCompare(String(b.name),'zh'));
  const results=$('#profileResults');results.replaceChildren();
  for(const g of ordered){
    const o=document.createElement('option');o.value=g.id;
    // 「实验」说的是没人试过的自动生成配置。自己加的不属于那一类，标错了会让人
    // 以为是软件给的半成品，而不是他自己刚建的空白档。
    const tail=g.source==='custom'?'自己加的':(g.verified?'已验证':'');
    o.textContent=`${g.name}${tail?` · ${tail}`:''}`;
    select.appendChild(o);
    // 选游戏只有一个列表：点哪个就换成哪个，不再「下拉选中 + 点使用」两步。
    const item=document.createElement('button');item.type='button';item.className='game-result';item.dataset.id=g.id;item.setAttribute('role','option');
    item.setAttribute('aria-selected',String(g.id===selectedId));
    const name=document.createElement('span');name.textContent=g.name;
    const meta=document.createElement('small');meta.textContent=[tail,g.appid?`Steam ${g.appid}`:''].filter(Boolean).join(' · ');
    item.append(name,meta);results.appendChild(item);
  }
  if(gameProfile.catalog.some(g=>g.id===selectedId))select.value=selectedId;
}

export async function searchProfiles(){
  const q=$('#profileSearch').value.trim();
  const data=await api('/api/game-profiles/catalog'+(q?'?q='+encodeURIComponent(q):''));
  lastProfileQuery=q;
  renderProfileCatalog(data.games||[]);
  const count=Number(data.count||0);
  $('#profileResultsInfo').textContent=q?(count?`找到 ${count} 款`:'没搜到，可以在下面自己加一个'):`共 ${Number(data.library_count||count)} 款，自己加的和验证过的排在前面`;
}

export async function refreshProfile(){
  const [selected,actions]=await Promise.all([api('/api/game-profiles/selected'),api('/api/output/actions')]);
  gameProfile.selected=selected.profile||null;gameProfile.actions=actions.actions||{};gameProfile.overrides=gameProfile.selected?.overrides||{};
  gameLaunch=selected.launch||null;
  gameProfile.zonePointLabels=actions.zone_point_labels||{};
  gameProfile.zoneSegments=actions.zone_segments||[];
  gameProfile.zoneDefaultPoints=actions.zone_default_points||{};
  renderProfileHeader();renderProfileBindingRows();await searchProfiles();renderProfileHeader();
}

// ---- 自己加的游戏 ---------------------------------------------------------
// 内置目录只有两百个，而且是从 Steam 榜单生成的，漏掉很正常。以前搜不到就只能
// 借用别人的坑位：界面上一直显示着错的游戏名，第二个未收录的游戏就没地方放。
//
// 加完直接选中它。会来加游戏的人就是为了马上用它——加完还要自己再去下拉框里找
// 一遍，等于把一件事拆成两件。
export async function addCustomGame(){
  const name=$('#customGameName').value.trim();
  if(!name){notice('先给这个游戏起个名字');return}
  await profileOperation(async()=>{
    const data=await post('/api/game-profiles/custom/add',
      {name,appid:$('#customGameAppid').value});
    $('#customGameName').value='';$('#customGameAppid').value='';
    const picked=await post('/api/game-profiles/select',{id:data.game.id});
    gameProfile.selected=picked.profile;gameProfile.overrides=picked.profile.overrides||{};++profileApplies;
    gameLaunch=picked.launch||null;
    // searchProfiles 会把 #profileMeta 写成库统计，所以头部要排在它后面重画一次。
    await refreshVoiceCommands();renderProfileBindingRows();await searchProfiles();renderProfileHeader();
    notice(`已添加并切换到「${data.game.name}」，按键在下面自己绑`);togglePanel('switchGameBtn','gamePicker',false);
  });
}

export async function renameCustomGame(){
  const current=gameProfile.selected;
  if(current?.source!=='custom')return;
  const name=prompt('改成什么名字？按键映射不会丢，它是按编号存的。',current.name);
  if(name===null)return;
  await profileOperation(async()=>{
    const data=await post('/api/game-profiles/custom/rename',{id:current.id,name});
    gameProfile.selected={...current,name:data.game.name};
    await searchProfiles();renderProfileHeader();
    notice(`已改名为「${data.game.name}」`);
  });
}

export async function removeCustomGame(){
  const current=gameProfile.selected;
  if(current?.source!=='custom')return;
  if(!confirm(`删掉「${current.name}」？给它调的按键映射会一起删掉，恢复不了。`))return;
  await profileOperation(async()=>{
    // 删的是正在用的那个，服务端会退回通用档并把新绑定推给内核和手机，所以这里
    // 要用它返回的那份，不能继续显示一个已经不存在的游戏。
    const data=await post('/api/game-profiles/custom/remove',{id:current.id});
    gameProfile.selected=data.profile;gameProfile.overrides=data.profile.overrides||{};
    gameLaunch=(await api('/api/game-profiles/selected')).launch||null;
    await refreshVoiceCommands();renderProfileBindingRows();await searchProfiles();renderProfileHeader();
    notice(`已删掉，当前游戏退回「${data.profile.name}」`);
  });
}

export async function applySelectedProfile(){
  const id=$('#profileSelect').value;if(!id||profileSwitching)return;
  await profileOperation(async()=>{
    await saveProfileBindings();
    const data=await post('/api/game-profiles/select',{id});
    gameProfile.selected=data.profile;++profileApplies;
    gameProfile.overrides=data.profile.overrides||{};
    gameLaunch=data.launch||null;
    await refreshVoiceCommands();renderProfileHeader();renderProfileBindingRows();
    notice(`已切换游戏：${data.profile.name}`);
    if(gameLaunch?.requires_admin&&!gameLaunch.is_admin)await changeGameLaunchMode(true);
  });
}

async function profileOperation(operation){
  const controls=()=>[...document.querySelectorAll('#profileResults button,#resetProfileBindingsBtn,#profileSelect,#customGameAddBtn,#customGameRenameBtn,#customGameRemoveBtn')];
  profileSwitching=true;$('#mappingFields').disabled=true;
  for(const el of controls())el.disabled=true;
  try{await operation()}
  finally{
    profileSwitching=false;$('#mappingFields').disabled=false;
    for(const el of controls())el.disabled=false;
  }
}

function makeTypeSelect(binding){
  const sel=document.createElement('select');sel.className='binding-type';
  const none=document.createElement('option');none.value='';none.textContent='不绑';sel.appendChild(none);
  const option=type=>{const o=document.createElement('option');o.value=type;o.textContent=ACTION_TYPE_LABELS[type];return o};
  for(const [label,types] of ACTION_TYPE_GROUPS){
    const group=document.createElement('optgroup');group.label=label;
    for(const type of types)if(gameProfile.actions?.[type])group.appendChild(option(type));
    if(group.children.length)sel.appendChild(group);
  }
  // 服务端以后新加、这里还没分组的输出类型，放在最后，不至于选不到。
  const grouped=new Set(ACTION_TYPE_GROUPS.flatMap(([,types])=>types));
  for(const type of Object.keys(ACTION_TYPE_LABELS))if(gameProfile.actions?.[type]&&!grouped.has(type))sel.appendChild(option(type));
  sel.value=binding?.disabled?'':(binding?.action?.type||'');return sel;
}

function voiceReleaseTargetIds(select){return [...(select?.selectedOptions||[])].map(option=>voiceCommandId(option.value)).filter(Boolean)}

let voiceReleasePickerEventsReady=false;

function setVoiceReleasePickerOpen(picker,open){
  picker.classList.toggle('open',open);
  const button=picker.querySelector('.voice-release-picker-button');
  if(button)button.setAttribute('aria-expanded',open?'true':'false');
}

function ensureVoiceReleasePickerEvents(){
  if(voiceReleasePickerEventsReady)return;
  document.addEventListener('click',event=>{
    const button=event.target.closest?.('.voice-release-picker-button');
    if(button){
      const picker=button.closest('.voice-release-picker');
      if(!picker||button.disabled)return;
      document.querySelectorAll('.voice-release-picker.open').forEach(item=>{if(item!==picker)setVoiceReleasePickerOpen(item,false)});
      setVoiceReleasePickerOpen(picker,!picker.classList.contains('open'));
      return;
    }
    const option=event.target.closest?.('.voice-release-option');
    if(option){
      event.preventDefault();
      const picker=option.closest('.voice-release-picker');
      const select=picker?.querySelector('select.voice-release-target');
      if(!picker||!select||select.disabled||option.disabled)return;
      const id=voiceCommandId(option.dataset.value);
      if(!id)return;
      const selected=voiceReleaseTargetIds(select);
      const next=selected.includes(id)?selected.filter(item=>item!==id):[...selected,id];
      [...select.options].forEach(item=>{item.selected=next.includes(voiceCommandId(item.value))});
      fillVoiceReleaseSelect(select,select.closest('.binding-target-box')?.dataset.trigger||'',next);
      select.dispatchEvent(new Event('change',{bubbles:true}));
      return;
    }
    if(!event.target.closest?.('.voice-release-picker')){
      document.querySelectorAll('.voice-release-picker.open').forEach(item=>setVoiceReleasePickerOpen(item,false));
    }
  });
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape')document.querySelectorAll('.voice-release-picker.open').forEach(item=>setVoiceReleasePickerOpen(item,false));
  });
  voiceReleasePickerEventsReady=true;
}

function updateVoiceReleasePicker(select){
  const picker=select?.closest('.voice-release-picker');
  if(!picker)return;
  const button=picker.querySelector('.voice-release-picker-button');
  const menu=picker.querySelector('.voice-release-picker-menu');
  const selected=voiceReleaseTargetIds(select);
  const labels=voiceCommandNames(selected);
  if(button){
    button.textContent=labels.length?labels.join('、'):(select.options.length&&select.options[0].value===''?select.options[0].textContent:'请选择要停住的口令');
    button.disabled=select.disabled;
    button.setAttribute('aria-label',labels.length?`已选：${labels.join('、')}`:'选择要停住的口令');
  }
  if(!menu)return;
  menu.replaceChildren();
  for(const option of select.options){
    const item=document.createElement('button');
    item.type='button';item.className='voice-release-option';item.dataset.value=option.value;
    item.textContent=option.textContent;item.disabled=!option.value||option.disabled;
    item.setAttribute('role','option');item.setAttribute('aria-selected',option.selected?'true':'false');
    item.classList.toggle('selected',option.selected);menu.appendChild(item);
  }
  if(!menu.children.length){const empty=document.createElement('div');empty.className='voice-release-empty';empty.textContent='没有可停住的口令';menu.appendChild(empty)}
}

function voiceHoldChoices(excludeKey=''){
  const out=[];
  for(const row of document.querySelectorAll('.binding-row[data-trigger^="voice."]')){
    if(row.dataset.trigger===excludeKey)continue;
    const type=row.querySelector('.binding-type')?.value||'';
    if(!type||type==='voice_release')continue;
    const behavior=row.querySelector('select.binding-behavior')?.value||row.querySelector('.binding-behavior')?.dataset.value;
    if(behavior==='hold')out.push(voiceCommandId(row.dataset.trigger));
  }
  return out;
}

function fillVoiceReleaseSelect(select,excludeKey,value){
  const previous=voiceReleaseTargetIds(select);
  const want=voiceCommandIds(value==null?previous:value);
  const choices=voiceHoldChoices(excludeKey);
  select.multiple=true;
  select.hidden=true;
  select.replaceChildren();
  for(const id of choices){const o=document.createElement('option');o.value=id;o.textContent=voiceCommandName(id);o.selected=want.includes(id);select.appendChild(o)}
  // 指着的那条已经不是持续按住了，照实写出来，不偷偷换成别的一条。
  for(const id of want.filter(item=>!choices.includes(item))){const o=document.createElement('option');o.value=id;o.textContent=`${voiceCommandName(id)}（已不是按住不放）`;o.selected=true;select.appendChild(o)}
  if(!select.options.length){const o=document.createElement('option');o.value='';o.textContent='先把一条口令设成「按住不放」';o.selected=true;select.appendChild(o)}
  select.disabled=!choices.length&&!want.length;
  updateVoiceReleasePicker(select);
}

// 口令改了说法、改成或不再是持续按住，所有「停住语音按住」的下拉框和选项都跟着变。
export function syncVoiceReleaseChoices(){
  for(const row of document.querySelectorAll('#profileBindingRows .binding-row')){
    const typeSel=row.querySelector('.binding-type');if(!typeSel)continue;
    const option=[...typeSel.options].find(o=>o.value==='voice_release');
    if(option){
      // 没有设成「按住不放」的口令时，这一项用不上，就不列出来（这一行正选着它的除外）。
      const none=!voiceHoldChoices(row.dataset.trigger).length;
      option.hidden=none&&typeSel.value!=='voice_release';option.disabled=option.hidden;
    }
    const select=row.querySelector('select.voice-release-target');
    if(select)fillVoiceReleaseSelect(select,row.dataset.trigger,voiceReleaseTargetIds(select));
  }
}

// 键盘键位框：点进去按一下，就换成刚按的那个键。以前是普通文本框，原来写着 SPACE，
// 想换成 D 一按就成了 SPACED，还存不进去。按住几个一起按是组合键（按住 CTRL 再按 W
// 就是 CTRL+W），全部松开才算定下来——宏那边一提交就整条重画，按到一半就提交的话，
// 手还没松框就没了。键名对的是电脑那边 profile_schema.KEYBOARD_KEYS 那一套，那套以外
// 的键（小键盘、分号这些）按了只提示，原来的值不动。
const KEY_CODE_NAMES={Space:'SPACE',Enter:'ENTER',NumpadEnter:'ENTER',Escape:'ESC',Tab:'TAB',
  ShiftLeft:'SHIFT',ShiftRight:'SHIFT',ControlLeft:'CTRL',ControlRight:'CTRL',AltLeft:'ALT',AltRight:'ALT',MetaLeft:'WIN',MetaRight:'WIN',
  Backspace:'BACKSPACE',Delete:'DELETE',Home:'HOME',End:'END',PageUp:'PAGEUP',PageDown:'PAGEDOWN',
  ArrowLeft:'LEFT',ArrowUp:'UP',ArrowRight:'RIGHT',ArrowDown:'DOWN'};

const KEY_MODIFIERS=['CTRL','SHIFT','ALT','WIN'];

function keyNameFromCode(code){
  if(/^Key[A-Z]$/.test(code))return code.slice(3);
  if(/^Digit[0-9]$/.test(code))return code.slice(5);
  if(/^F([1-9]|1[0-2])$/.test(code))return code;
  return KEY_CODE_NAMES[code]||null;
}

// 提示贴在框下面。页顶那条通知栏在映射表、宏这里往往已经滚出屏幕了。
function keyCaptureTip(input,text){
  document.querySelector('.key-capture-tip')?.remove();
  const tip=document.createElement('div');tip.className='key-capture-tip';tip.textContent=text;
  const r=input.getBoundingClientRect();tip.style.left=`${r.left+window.scrollX}px`;tip.style.top=`${r.bottom+window.scrollY+4}px`;
  document.body.appendChild(tip);setTimeout(()=>tip.remove(),1800);
}

export function makeKeyCaptureInput(className,value=''){
  const input=document.createElement('input');input.className=className+' key-capture';input.type='text';
  // 只读才不会被输入法接走：开着中文输入法按 D，普通文本框会先弹候选框。
  input.readOnly=true;input.value=value;input.placeholder='点这里，再按键';
  input.title='点一下，再按想要的键；按住 CTRL 再按 W 就是 CTRL+W。浏览器自己占着的组合键（比如 CTRL+W 会关掉页面）录不进来';
  let chord=[],before='';const held=new Set();
  const settle=()=>{
    held.clear();if(!chord.length)return;chord=[];
    if(input.value!==before){input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}))}
  };
  input.addEventListener('keydown',e=>{
    // 不往外传：F9 是全局急停，这里按 F9 是想绑 F9。
    e.preventDefault();e.stopPropagation();if(e.repeat)return;
    const name=keyNameFromCode(e.code);
    if(!name){keyCaptureTip(input,`「${e.code.startsWith('Numpad')?'小键盘 '+e.key:e.key}」这个键还不支持，换一个`);return}
    if(!chord.length)before=input.value;
    held.add(e.code);
    // 修饰键也看事件上的标志：远程桌面、按键精灵这类发来的 CTRL+S 可能只有带着
    // ctrlKey 的 S，前面没有单独的一下 CTRL。
    const flags=[['ctrlKey','CTRL'],['shiftKey','SHIFT'],['altKey','ALT'],['metaKey','WIN']].filter(([flag])=>e[flag]).map(([,key])=>key);
    const adding=[...flags,name].filter((key,at,all)=>!chord.includes(key)&&all.indexOf(key)===at);
    if(!adding.length)return;
    if(chord.length+adding.length>4){keyCaptureTip(input,'组合键最多 4 个键');return}
    chord.push(...adding);
    input.value=[...KEY_MODIFIERS.filter(k=>chord.includes(k)),...chord.filter(k=>!KEY_MODIFIERS.includes(k))].join('+');
  });
  input.addEventListener('keyup',e=>{e.preventDefault();e.stopPropagation();held.delete(e.code);if(!held.size)settle()});
  // 按 WIN 会弹开始菜单，松开那一下不一定回得到这里；焦点一走就按已经按下的算。
  input.addEventListener('blur',settle);
  return input;
}

export function fillTargetControl(container,type,value='',comboLeadMs=80,comboLeadExplicit=false){
  container.replaceChildren();if(!type)return;
  const meta=gameProfile.actions?.[type]||{};
  if(type==='gamepad'){
    const select=document.createElement('select');select.className='binding-target';
    for(const key of meta.targets||['A','B','X','Y','LB','RB','L3','R3','START','BACK','DPAD_UP','DPAD_DOWN','DPAD_LEFT','DPAD_RIGHT']){const option=document.createElement('option');option.value=key;option.textContent=TARGET_LABELS[key]||key;select.appendChild(option)}
    const custom=document.createElement('option');custom.value='__combo__';custom.textContent='组合键…';select.appendChild(custom);
    const raw=Array.isArray(value)?value.join('+'):String(value||'A');
    // Tick the parts instead of typing "LB+LS_UP": the valid names are a fixed
    // set, and a typo here only surfaces as a rejected save. Stick directions
    // are offered alongside the buttons because a combo may drive both.
    const chosen=new Set(raw.split('+').map(part=>part.trim().toUpperCase()).filter(Boolean));
    const picker=document.createElement('div');picker.className='combo-picker';
    const combo=document.createElement('input');combo.type='hidden';
    const hasStick=()=>[...picker.querySelectorAll('input:checked')].some(box=>GAMEPAD_STICK_TARGETS.includes(box.value));
    const sync=()=>{combo.value=[...picker.querySelectorAll('input:checked')].map(box=>box.value).join('+');if(select.value==='__combo__')leadBox.hidden=!hasStick()};
    const comboTargets=meta.combo_targets||[...(meta.targets||[]),...GAMEPAD_STICK_TARGETS,...GAMEPAD_TRIGGER_TARGETS];
    for(const key of [...new Set(comboTargets)]){
      const label=document.createElement('label');const box=document.createElement('input');
      box.type='checkbox';box.value=key;box.checked=chosen.has(key);box.addEventListener('change',sync);
      label.append(box,document.createTextNode(TARGET_LABELS[key]||key));picker.appendChild(label);
    }
    const leadBox=document.createElement('label');leadBox.className='combo-lead-box';leadBox.textContent='按键领先摇杆';
    const lead=document.createElement('input');lead.type='range';lead.className='combo-lead-ms';lead.min='0';lead.max='200';lead.step='5';lead.value=String(Math.max(0,Math.min(200,Number(comboLeadMs)||0)));
    lead.dataset.explicit=comboLeadExplicit?'1':'0';
    const leadValue=document.createElement('span');leadValue.className='combo-lead-value';leadValue.textContent=`${lead.value} 毫秒`;
    lead.addEventListener('input',()=>{lead.dataset.touched='1';leadValue.textContent=`${lead.value} 毫秒`});leadBox.append(lead,leadValue);
    const update=()=>{const isCombo=select.value==='__combo__';select.className=isCombo?'binding-gamepad-select':'binding-target';combo.className=isCombo?'binding-target':'';picker.hidden=!isCombo;leadBox.hidden=!isCombo||!hasStick()};
    select.value=[...select.options].some(o=>o.value===raw)?raw:'__combo__';
    sync();select.addEventListener('change',update);update();container.append(select,picker,combo,leadBox);return;
  }
  if(type==='voice_release'){
    ensureVoiceReleasePickerEvents();
    const picker=document.createElement('div');picker.className='voice-release-picker';
    const button=document.createElement('button');button.type='button';button.className='voice-release-picker-button';button.setAttribute('aria-haspopup','listbox');button.setAttribute('aria-expanded','false');
    const menu=document.createElement('div');menu.className='voice-release-picker-menu';menu.setAttribute('role','listbox');
    const select=document.createElement('select');select.className='binding-target voice-release-target';select.setAttribute('aria-hidden','true');
    picker.append(button,menu,select);
    fillVoiceReleaseSelect(select,container.dataset.trigger||'',value);
    container.appendChild(picker);return;
  }
  if(type==='system'){
    const select=document.createElement('select');select.className='binding-target';
    const allowed=new Set(meta.targets||BINDING_SYSTEM_TARGETS.map(([id])=>id));
    for(const[id,name]of BINDING_SYSTEM_TARGETS){if(!allowed.has(id))continue;const o=document.createElement('option');o.value=id;o.textContent=name;select.appendChild(o)}
    const want=String(value||'').toUpperCase();
    if(want&&[...select.options].some(o=>o.value===want))select.value=want;
    container.appendChild(select);return;
  }
  if(type==='macro'){
    const select=document.createElement('select');select.className='binding-target';
    if(!macroLibrary.items.length){
      const o=document.createElement('option');o.value='';o.textContent='还没有宏，到「设置 → 键盘宏」建一条';
      select.appendChild(o);select.disabled=true;container.appendChild(select);return;
    }
    for(const macro of macroLibrary.items){const o=document.createElement('option');o.value=macro.id;o.textContent=macro.repeat?`${macro.name}（循环）`:macro.name;select.appendChild(o)}
    const want=String(value||'').toLowerCase();
    // 指向一条已经删掉的宏时，照实说。默默换成第一条会让人以为自己记错了。
    if(want&&![...select.options].some(o=>o.value===want)){const o=document.createElement('option');o.value=want;o.textContent='宏已丢失';select.appendChild(o)}
    select.value=want||macroLibrary.items[0].id;
    container.appendChild(select);return;
  }
  if(meta.free_text){container.appendChild(makeKeyCaptureInput('binding-target',Array.isArray(value)?value.join('+'):(value||'')));return}
  const select=document.createElement('select');select.className='binding-target';
  for(const target of meta.targets||[]){const o=document.createElement('option');o.value=target;o.textContent=TARGET_LABELS[target]||target;select.appendChild(o)}
  if(value&&[...select.options].some(o=>o.value===value))select.value=value;container.appendChild(select);
}

function fillBehaviorControl(container,trigger,type,value,macroId){
  container.replaceChildren();
  if(type==='macro'){
    // 「跑一遍还是按住时循环」是宏自己的设定，在宏库那边改。同一个东西两处能改，
    // 就一定会有一处是旧的，所以这里只显示结果。
    const macro=macroById(macroId);
    const mode=macro?.repeat?'hold':'tap';
    const text=!macro?'—':macro.repeat?'按住时循环':'跑一遍';
    if(trigger.group!=='voice'){
      const span=document.createElement('span');span.className='binding-behavior';
      span.dataset.value=mode;span.textContent=text;container.appendChild(span);return;
    }
    // 循环的宏靠另一个动作停住：那个动作选「停住语音按住」，再选这句口令。
    const span=document.createElement('span');span.className='binding-behavior';
    span.dataset.value=mode;span.textContent=text;container.appendChild(span);return;
  }
  if(type==='voice_release'){const span=document.createElement('span');span.className='binding-behavior';span.textContent='松开一次';span.dataset.value='tap';container.appendChild(span);return}
  if(type==='system'){const span=document.createElement('span');span.className='binding-behavior';span.textContent='执行一次';span.dataset.value='tap';container.appendChild(span);return}
  if(trigger.tapOnly||type==='mouse_wheel'){const span=document.createElement('span');span.className='binding-behavior';span.textContent='点一下';span.dataset.value='tap';container.appendChild(span);return}
  if(!type){const span=document.createElement('span');span.className='binding-behavior';span.textContent='';span.dataset.value='hold';container.appendChild(span);return}
  const sel=document.createElement('select');sel.className='binding-behavior';
  sel.title=trigger.group==='voice'?'点一下：说一次按一下。按住不放：说完一直按着，直到另一句口令把它松开':'按住：动作做着（或在框里）就一直按着。点一下：开始时按一下';
  for(const[v,t]of (trigger.group==='voice'?[['tap','点一下'],['hold','按住不放']]:[['hold','按住'],['tap','点一下']])){const o=document.createElement('option');o.value=v;o.textContent=t;sel.appendChild(o)}
  // Poses default to a single edge trigger on the server, so show that rather
  // than "hold" while a pose has no binding yet.
  const edgeDefault=trigger.group==='voice'||trigger.group==='poses';
  sel.value=trigger.group==='voice'?(['tap','hold'].includes(value)?value:'tap'):(value==='tap'||(!value&&edgeDefault)?'tap':'hold');container.appendChild(sel);
}

// 身体动作那一组只列这个游戏用着的：配置里绑了键的（默认就绑了原地踏步、小腿向后
// 抬起两个），加上这次从动作库点进来的。其余的在下面「动作库」「自定义动作」里挑，
// 点卡片上的「加到映射」就进来——十来行「不映射」堆在表里，要找的那一行反而看不见。
// 列出来过的行在同一个游戏里一直留着：改成「不映射」那一下就消失，人会以为没改上。
const shownBodyRows={profile:null,keys:new Set()};

function isBodyTrigger(trigger){return trigger.group==='motions'||trigger.group==='poses'}

function bodyRowWanted(trigger){
  const binding=bindingFor(trigger);
  return !!(binding&&!binding.disabled)||shownBodyRows.keys.has(trigger.key);
}

// 卡片上那个键位按钮：绑了写键，表里有这一行但没绑写「未映射」，表里还没有写「加到映射」。
export function libraryKeyLabel(triggerKey){
  const label=triggerKeyLabel(triggerKey);
  return label==='未映射'?(shownBodyRows.keys.has(triggerKey)?'没绑键':'加到映射'):label;
}

// 旧的「松开同一语音按键」：那句口令自己又选了一遍同样的键。按键对上哪条持续按住
// 的口令，就显示成「停住语音按住 · 那条口令」；存的还是旧写法，改这一行时才换成新的。
function legacyReleaseTarget(action){
  const same=(a,b)=>{const norm=x=>(Array.isArray(x)?x:String(x||'').split('+')).map(v=>String(v).trim().toUpperCase()).filter(Boolean).sort().join('+');return norm(a)===norm(b)};
  const bindings=bindingsForDisplay();
  for(const[key,binding]of Object.entries(bindings)){
    const other=binding?.action;
    if(!key.startsWith('voice.')||binding?.disabled||!other||other.behavior!=='hold')continue;
    if(other.type===action.type&&(other.type==='macro'?String(other.target).toLowerCase()===String(action.target).toLowerCase():same(other.target,action.target)))return voiceCommandId(key);
  }
  return '';
}

function buildZonePointPicker(trigger,binding){
  const labels=gameProfile.zonePointLabels||{};if(!Object.keys(labels).length)return null;
  const ids=Object.keys(labels),order=new Map(ids.map((id,index)=>[id,index]));
  const edgeKey=(a,b)=>[a,b].sort((x,y)=>order.get(x)-order.get(y)).join('|');
  const picker=document.createElement('details');picker.className='zone-point-picker';
  const summary=document.createElement('summary');
  const menu=document.createElement('div');menu.className='zone-point-menu';menu.setAttribute('aria-label',trigger.name+' 触发点与连线');
  const choices=document.createElement('select');choices.multiple=true;choices.hidden=true;choices.className='zone-trigger-choices';
  choices.dataset.explicit=binding&&('trigger_points'in binding||'trigger_segments'in binding)?'1':'0';
  const points=new Set(binding?.trigger_points??gameProfile.zoneDefaultPoints?.[trigger.id]??[]);
  const segments=new Set((binding?.trigger_segments||[]).map(pair=>edgeKey(...pair)));
  let mode='lines',gesture=null;
  const tools=document.createElement('div');tools.className='zone-point-tools';
  const hint=document.createElement('div');hint.className='zone-point-heading';
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 300 340');svg.classList.add('zone-point-skeleton');svg.setAttribute('aria-label','人体骨骼点，按住直接拖动连线');
  // 图上所有 33 点都是可选端点，背景骨架只是定位参考。
  const xy=[[150,48],[126,30],[112,28],[98,32],[174,30],[188,28],[202,32],[78,42],[222,42],[134,66],[166,66],
    [94,102],[206,102],[62,150],[238,150],[54,202],[246,202],[32,232],[268,232],[54,242],[246,242],[78,220],[222,220],
    [112,204],[188,204],[106,257],[194,257],[102,304],[198,304],[90,323],[210,323],[65,321],[235,321]];
  const positions=Object.fromEntries(ids.map((id,index)=>[id,xy[index]]));
  const svgEl=(tag,attrs)=>{const el=document.createElementNS(svg.namespaceURI,tag);for(const[key,value]of Object.entries(attrs))el.setAttribute(key,value);return el};
  const line=(a,b)=>svgEl('line',{x1:positions[a][0],y1:positions[a][1],x2:positions[b][0],y2:positions[b][1]});
  const background=svgEl('g',{'class':'zone-skeleton-background'}),drawn=svgEl('g',{}),nodes=svgEl('g',{});
  for(const[a,b]of gameProfile.zoneSegments||[])background.appendChild(line(a,b));
  const preview=svgEl('line',{'class':'zone-drawing-preview',visibility:'hidden'});
  svg.append(background,drawn,preview,nodes);
  const list=document.createElement('div');list.className='zone-point-list';list.setAttribute('aria-label','全部人体骨骼点');
  const rules=document.createElement('div');rules.className='zone-point-rules';
  const edges=document.createElement('div');edges.className='zone-drawn-edges';
  const groups=()=>{
    const graph=new Map();for(const value of segments){const[a,b]=value.split('|');if(!graph.has(a))graph.set(a,new Set());if(!graph.has(b))graph.set(b,new Set());graph.get(a).add(b);graph.get(b).add(a)}
    const result=[...points].filter(id=>!graph.has(id)).map(id=>[id]),seen=new Set();
    for(const start of graph.keys()){if(seen.has(start))continue;const todo=[start],group=[];while(todo.length){const id=todo.pop();if(seen.has(id))continue;seen.add(id);group.push(id);todo.push(...graph.get(id))}result.push(group.sort((a,b)=>order.get(a)-order.get(b)))}
    return {result,linked:new Set(graph.keys())};
  };
  const cancelGesture=()=>{gesture=null;preview.setAttribute('visibility','hidden');menu.querySelectorAll('.drawing-start').forEach(el=>el.classList.remove('drawing-start'))};
  const changed=()=>{choices.dataset.explicit='1';sync();choices.dispatchEvent(new Event('change',{bubbles:true}))};
  const removePoint=id=>{points.delete(id);for(const value of [...segments])if(value.split('|').includes(id))segments.delete(value);changed()};
  const clickPoint=id=>{if(groups().linked.has(id))removePoint(id);else if(mode==='points'){points.has(id)?points.delete(id):points.add(id);changed()}};
  const localPoint=event=>new DOMPoint(event.clientX,event.clientY).matrixTransform(svg.getScreenCTM().inverse());
  const endPoint=event=>{
    const hit=document.elementFromPoint(event.clientX,event.clientY)?.closest('[data-point]');if(hit&&menu.contains(hit))return hit.dataset.point;
    if(!svg.contains(document.elementFromPoint(event.clientX,event.clientY)))return null;
    const p=localPoint(event);return ids.find(id=>Math.hypot(positions[id][0]-p.x,positions[id][1]-p.y)<=11)||null;
  };
  const pointerDown=(event,id)=>{
    if(mode!=='lines'||event.button!==0)return;
    cancelGesture();event.preventDefault();event.currentTarget.setPointerCapture(event.pointerId);
    gesture={id,pointer:event.pointerId};
    const[x,y]=positions[id];preview.setAttribute('x1',x);preview.setAttribute('y1',y);preview.setAttribute('x2',x);preview.setAttribute('y2',y);preview.setAttribute('visibility','visible');menu.querySelectorAll(`[data-point="${id}"]`).forEach(el=>el.classList.add('drawing-start'));
  };
  menu.addEventListener('pointermove',event=>{if(!gesture||event.pointerId!==gesture.pointer)return;const p=localPoint(event);preview.setAttribute('x2',p.x);preview.setAttribute('y2',p.y)});
  menu.addEventListener('pointerup',event=>{
    if(!gesture||event.pointerId!==gesture.pointer)return;
    const start=gesture.id,end=endPoint(event);cancelGesture();
    // 只在松手时决定终点；路径上经过的其他点不会加入。
    if(end&&end!==start){const value=edgeKey(start,end);segments.has(value)?segments.delete(value):segments.add(value);changed()}
  });
  menu.addEventListener('pointercancel',cancelGesture);
  const keyPoint=(event,id)=>{if(mode==='points'&&(event.key==='Enter'||event.key===' ')){event.preventDefault();clickPoint(id)}};
  for(const id of ids){
    const node=svgEl('g',{transform:`translate(${positions[id].join(' ')})`,'data-point':id,role:'button',tabindex:'0','aria-label':labels[id]});node.classList.add('zone-skeleton-point');
    node.append(svgEl('circle',{r:10,'class':'zone-point-hit'}),svgEl('circle',{r:5}));const title=svgEl('title',{});title.textContent=labels[id];node.append(title);nodes.append(node);
    const item=document.createElement('button');item.type='button';item.className='zone-point-option';item.textContent=labels[id];item.dataset.point=id;list.append(item);
    for(const el of [node,item]){el.addEventListener('pointerdown',event=>pointerDown(event,id));el.addEventListener('click',()=>{if(mode==='points')clickPoint(id)});if(el===node)el.addEventListener('keydown',event=>keyPoint(event,id))}
  }
  for(const[value,text]of [['points','选点'],['lines','连线']]){const button=document.createElement('button');button.type='button';button.textContent=text;button.dataset.mode=value;button.addEventListener('click',()=>{cancelGesture();mode=value;sync()});tools.append(button)}
  const sync=()=>{
    choices.replaceChildren();for(const value of [...points,...segments]){const option=document.createElement('option');option.value=value;option.selected=true;choices.append(option)}
    const {result,linked}=groups();
    const count=result.reduce((sum,group)=>sum+group.length,0);
    summary.textContent=(count>3?`${count} 个点${segments.size?' · 已连线':''}`:result.map(group=>group.map(id=>labels[id]).join('＋')).join(' / ')||'未选');
    summary.title=result.map(group=>group.map(id=>labels[id]).join('＋')+(group.length>1?'（全部同时进入）':'')).join('；')||'未选择触发点，这个区域不会触发';
    summary.setAttribute('aria-label',trigger.name+' '+summary.textContent);
    for(const item of menu.querySelectorAll('[data-point]')){
      const chosen=points.has(item.dataset.point)||linked.has(item.dataset.point);item.classList.toggle('selected',chosen);item.classList.toggle('linked',linked.has(item.dataset.point));item.setAttribute('aria-pressed',String(chosen));
    }
    tools.querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.mode===mode)));
    hint.textContent=mode==='points'?'点一下选择，再点取消。独立点任选其一。':'按住点直接拖线，松手选终点；点击亮线取消。';
    rules.textContent=result.length?result.map(group=>group.map(id=>labels[id]).join('＋')+(group.length>1?'：全部同时进入':'：进入即可')).join('；'):'未选触发点';
    drawn.replaceChildren();edges.replaceChildren();
    for(const value of segments){const[a,b]=value.split('|');const remove=()=>{segments.delete(value);changed()};const el=line(a,b);el.classList.add('zone-selected-line');el.setAttribute('tabindex','0');el.setAttribute('role','button');el.setAttribute('aria-label',`删除连线：${labels[a]}—${labels[b]}`);el.addEventListener('click',remove);el.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();remove()}});drawn.append(el);
      const button=document.createElement('button');button.type='button';button.textContent=`${labels[a]}—${labels[b]} ×`;button.title='删除这条连线';button.addEventListener('click',remove);edges.append(button);
    }
  };
  picker.positionMenu=()=>{
    const rect=summary.getBoundingClientRect(),width=Math.min(350,innerWidth*.8),below=innerHeight-rect.bottom-12,above=rect.top-12;
    menu.style.left=Math.max(8,Math.min(rect.left,innerWidth-width-8))+'px';
    menu.style.maxHeight=Math.max(120,Math.min(620,innerHeight*.75,Math.max(above,below)))+'px';
    menu.style.top=below>=above?rect.bottom+5+'px':'auto';menu.style.bottom=below>=above?'auto':innerHeight-rect.top+5+'px';
  };
  picker.addEventListener('toggle',()=>{if(picker.open)picker.positionMenu();else cancelGesture()});
  picker.addEventListener('keydown',event=>{if(event.key==='Escape')cancelGesture()});
  menu.append(tools,hint,svg,rules,edges,list);
  picker.append(summary,menu,choices);sync();return picker;
}

document.addEventListener('click',event=>{
  // 点删除后，该线/按钮已经离开文档；仍按点击开始时的路径判断是否点在面板内。
  const current=event.composedPath().find(el=>el.classList?.contains('zone-point-picker'));
  document.querySelectorAll('.zone-point-picker[open]').forEach(picker=>{if(picker!==current)picker.open=false});
});

document.addEventListener('keydown',event=>{
  if(event.key==='Escape')document.querySelectorAll('.zone-point-picker[open]').forEach(picker=>{picker.open=false;picker.querySelector('summary').focus()});
});

const positionZonePointMenus=()=>document.querySelectorAll('.zone-point-picker[open]').forEach(picker=>picker.positionMenu());

window.addEventListener('resize',positionZonePointMenus);

document.addEventListener('scroll',positionZonePointMenus,true);

function readZonePointChoices(row){
  const choices=row.querySelector('.zone-trigger-choices');
  if(!choices||choices.dataset.explicit!=='1')return {};
  const values=[...choices.selectedOptions].map(option=>option.value);
  return {trigger_points:values.filter(value=>!value.includes('|')),trigger_segments:values.filter(value=>value.includes('|')).map(value=>value.split('|'))};
}

function bodyRuleTriggerChoices(trigger){
  return profileTriggers().filter(item=>['zones','motions','poses'].includes(item.group)&&item.key!==trigger.key);
}

function buildExtraOutputRow(trigger,action,number,onRemove){
  const wrap=document.createElement('div');wrap.className='binding-sequence-action';
  const label=document.createElement('div');label.className='binding-alternate-label';
  const title=document.createElement('span');title.textContent=`第 ${number} 次`;
  label.appendChild(title);
  if(onRemove){
    const remove=document.createElement('button');remove.type='button';remove.className='binding-alternate-remove';remove.textContent='删除';
    remove.addEventListener('click',onRemove);label.appendChild(remove);
  }
  const type=makeTypeSelect({action});
  const target=document.createElement('div');target.className='binding-target-box';target.dataset.trigger=trigger.key;
  fillTargetControl(target,type.value,action.target,action.combo_stick_lead_ms??80,action.combo_stick_lead_ms!=null);
  const pickedMacro=()=>target.querySelector('.binding-target')?.value||'';
  const behavior=document.createElement('div');behavior.className='binding-behavior-box';
  fillBehaviorControl(behavior,trigger,type.value,action.behavior,action.target);
  type.addEventListener('change',()=>{fillTargetControl(target,type.value,'',80);fillBehaviorControl(behavior,trigger,type.value,'hold',pickedMacro());syncMotionConflictChoices()});
  target.addEventListener('change',()=>{if(type.value==='macro')fillBehaviorControl(behavior,trigger,'macro',behavior.querySelector('.binding-behavior')?.value,pickedMacro())});
  type.setAttribute('aria-label',trigger.name+` 第 ${number} 次输出类型`);
  target.setAttribute('aria-label',trigger.name+` 第 ${number} 次键位`);
  behavior.setAttribute('aria-label',trigger.name+` 第 ${number} 次方式`);
  // 输出类型和键位拼成一个控件，和主行一样。
  const output=document.createElement('div');output.className='binding-output';output.append(type,target);
  wrap.append(label,output,behavior);return wrap;
}

function buildAlternateActionRow(trigger,binding,onRemove){
  const wrap=document.createElement('div');wrap.className='binding-alternate';
  const header=document.createElement('div');header.className='binding-alternate-header';
  const title=document.createElement('strong');title.textContent='条件换键';
  const remove=document.createElement('button');remove.type='button';remove.className='binding-alternate-remove';remove.textContent='删除条件';
  remove.addEventListener('click',onRemove);header.append(title,remove);
  const rule=document.createElement('div');rule.className='binding-rule-controls';
  const mode=document.createElement('select');mode.className='binding-alternate-mode';mode.setAttribute('aria-label',trigger.name+' 第二输出规则');
  for(const [value,label] of [['with_trigger','配合另一个动作时'],['cycle','按次数轮流换']]){
    const option=document.createElement('option');option.value=value;option.textContent=label;mode.appendChild(option);
  }
  mode.value=binding?.alternate_mode||'with_trigger';
  const triggerPick=document.createElement('select');triggerPick.className='binding-rule-trigger';
  const help=document.createElement('div');help.className='binding-rule-help';
  rule.append(mode,triggerPick,help);
  const steps=document.createElement('div');steps.className='binding-sequence-actions';
  const second=buildExtraOutputRow(trigger,binding.alternate_action,2);steps.appendChild(second);
  const addStep=document.createElement('button');addStep.type='button';addStep.className='binding-sequence-add';addStep.textContent='＋ 加第 3 次';
  const relabel=()=>{
    steps.querySelectorAll('.binding-sequence-action').forEach((row,index)=>{
      row.querySelector('.binding-alternate-label span').textContent=`第 ${index+2} 次`;
    });
    addStep.textContent=`＋ 加第 ${steps.children.length+2} 次`;
  };
  for(const action of binding.extra_actions||[]){
    const row=buildExtraOutputRow(trigger,action,steps.children.length+2,()=>{
      row.remove();relabel();wrap.dispatchEvent(new Event('change',{bubbles:true}));
    });
    steps.appendChild(row);
  }
  relabel();
  addStep.addEventListener('click',()=>{
    let action;
    try{action=readBindingAction(steps.lastElementChild,trigger)}catch(error){notice(error.message);return}
    const row=buildExtraOutputRow(trigger,action,steps.children.length+2,()=>{
      row.remove();relabel();wrap.dispatchEvent(new Event('change',{bubbles:true}));
    });
    steps.appendChild(row);relabel();wrap.dispatchEvent(new Event('change',{bubbles:true}));
  });
  const refreshRule=selected=>{
    const cycle=mode.value==='cycle';
    triggerPick.replaceChildren();
    const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent=cycle?'选择重置用的区域或动作':'选择配合的动作';triggerPick.appendChild(placeholder);
    const candidates=bodyRuleTriggerChoices(trigger).filter(item=>cycle||isBodyTrigger(item));
    for(const item of candidates){const option=document.createElement('option');option.value=item.key;option.textContent=item.name;triggerPick.appendChild(option)}
    if(selected&&!candidates.some(item=>item.key===selected)){
      const missing=document.createElement('option');missing.value=selected;missing.textContent=`已不可用：${selected}`;triggerPick.appendChild(missing);
    }
    triggerPick.value=selected||'';
    triggerPick.setAttribute('aria-label',trigger.name+(cycle?' 重置区域或动作':' 配合动作'));
    help.textContent=cycle?'每触发一次换下一个键；碰到重置用的区域或动作，回到第一个。':'这个动作开始时正做着配合动作，就改按下面的键。';
    steps.firstElementChild.querySelector('.binding-alternate-label span').textContent=cycle?'第 2 次':'配合时';
    steps.querySelectorAll('.binding-sequence-action').forEach((row,index)=>{row.hidden=!cycle&&index>0});
    addStep.hidden=!cycle;
  };
  refreshRule(mode.value==='cycle'?binding.reset_trigger:binding.alternate_when);
  mode.addEventListener('change',()=>refreshRule(''));
  wrap.append(header,rule,steps,addStep);return wrap;
}

// 展开过「更多」的行，重画之后还展开着。
const expandedRows=new Set();

const CHEVRON_SVG='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';

function advRow(title,sub,control){
  const row=document.createElement('div');row.className='adv-row';
  const main=document.createElement('div');main.className='adv-main';
  const b=document.createElement('b');b.textContent=title;main.append(b);
  if(sub!==null){const small=document.createElement('small');small.textContent=sub;main.append(small)}
  row.append(main);if(control)row.append(control);return row;
}

function withMotionCaption(checked){return checked?'做动作时扫过也会按':'做动作时扫过不按，防误触'}

// 行名旁边的小标签：改过默认值的高级设置写在这里，不用展开也知道这一行动过。
function paintRowTags(row){
  const tags=row.querySelector('.binding-tags');if(!tags)return;
  const out=[];
  const box=row.querySelector('.zone-with-motion-box');
  if(box){const byDefault=JUMP_ZONE_IDS.has(String(row.dataset.trigger).slice(5));if(box.checked!==byDefault)out.push(box.checked?'扫过也按':'扫过不按')}
  if(row.querySelector('.zone-trigger-choices')?.dataset.explicit==='1')out.push('改过触发部位');
  const alternate=row.querySelector('.binding-alternate');
  if(alternate)out.push(alternate.querySelector('.binding-alternate-mode')?.value==='cycle'?'按次数换键':'配合动作换键');
  const key=out.join('|');if(tags.dataset.key===key)return;tags.dataset.key=key;
  tags.replaceChildren(...out.map(text=>{const tag=document.createElement('span');tag.className='tag info';tag.textContent=text;return tag}));
}

function buildBindingRow(trigger){
  const binding=bindingFor(trigger);
  let action=binding?.disabled?null:binding?.action;
  if(action?.behavior==='release')action={type:'voice_release',target:legacyReleaseTarget(action),behavior:'tap'};
  const row=document.createElement('div');row.className='binding-row';row.dataset.trigger=trigger.key;
  // 一行只露四样：叫什么、按哪个键、按住还是点一下、「更多」。
  const main=document.createElement('div');main.className='binding-main';
  const name=document.createElement('div');name.className='trigger-name';
  let phrase=null;
  if(trigger.group==='voice'){
    const game=isGameVoiceKey(trigger.key);
    phrase=document.createElement('input');phrase.className='voice-trigger-phrase';phrase.type='text';phrase.value=game?normalizeGameVoicePhrase(binding?.phrase||trigger.phrase||''):(binding?.phrase||trigger.phrase||'');phrase.placeholder=game?'说什么，例如：爬绳':'完整口令';phrase.title=game?'直接说这句，不用唤醒词':'说出的完整口令';name.append(phrase);watchVoicePhrase(phrase);
  }else name.textContent=trigger.name;
  const tags=document.createElement('span');tags.className='binding-tags';name.append(tags);
  const type=makeTypeSelect(action?{action}:binding);
  const target=document.createElement('div');target.className='binding-target-box';target.dataset.trigger=trigger.key;fillTargetControl(target,type.value,action?.target??'',action?.combo_stick_lead_ms??80,action?.combo_stick_lead_ms!=null);
  const pickedMacro=()=>target.querySelector('.binding-target')?.value||'';
  const behavior=document.createElement('div');behavior.className='binding-behavior-box';fillBehaviorControl(behavior,trigger,type.value,action?.behavior||(trigger.group==='voice'?'tap':'hold'),action?.target||'');
  type.addEventListener('change',()=>{fillTargetControl(target,type.value,'',80);fillBehaviorControl(behavior,trigger,type.value,trigger.group==='voice'?'tap':'hold',pickedMacro());syncMotionConflictChoices()});
  // 换了另一条宏，「跑一遍还是循环」要跟着那条宏重算——那一格写的必须是现在
  // 选中这条的，否则界面说一套、实际跑另一套。
  target.addEventListener('change',()=>{if(type.value==='macro')fillBehaviorControl(behavior,trigger,'macro',behavior.querySelector('.binding-behavior')?.value,pickedMacro())});
  const output=document.createElement('div');output.className='binding-output';output.append(type,target);
  main.append(name,output,behavior);row.append(main);
  row.querySelectorAll('input,select').forEach(control=>control.setAttribute('aria-label',trigger.name+' '+(control.className.includes('type')?'输出类型':'键位或触发方式')));
  if(phrase)phrase.setAttribute('aria-label',trigger.name+' 说法');
  if(trigger.group==='motions'){const note=document.createElement('div');note.className='motion-conflict-note';note.hidden=true;row.appendChild(note)}
  // 「更多」里放为特殊需要加的那些：触发部位、扫过也按、条件换键。新手用不着，默认收着。
  const adv=document.createElement('div');adv.className='binding-adv';let hasAdv=false;
  if(trigger.group==='zones'){
    const pointPicker=buildZonePointPicker(trigger,binding);
    if(pointPicker)adv.append(advRow('触发部位','身体哪个部位进框才算按下',pointPicker));
    // 「扫过也按」：没设过的，要跳才碰得到的框（头顶）默认是，别的默认不是——
    // 和电脑那边 _zone_with_motion_locked 同一条规则。
    const box=document.createElement('input');box.type='checkbox';box.className='switch zone-with-motion-box';
    box.checked=typeof binding?.with_motion==='boolean'?binding.with_motion:JUMP_ZONE_IDS.has(trigger.id);
    box.setAttribute('aria-label',trigger.name+' 动作扫过时也按');
    const item=advRow('动作扫过时也按',withMotionCaption(box.checked),box);
    box.addEventListener('change',()=>{item.querySelector('small').textContent=withMotionCaption(box.checked)});
    const note=document.createElement('div');note.className='zone-conflict-note';note.hidden=true;item.append(note);
    adv.append(item);hasAdv=true;
  }
  if(isBodyTrigger(trigger)){
    const add=document.createElement('button');add.type='button';add.className='binding-alternate-add';
    add.textContent='＋ 条件换键：配合另一个动作，或按次数换键';add.disabled=!type.value;
    const show=ruleBinding=>{
      const second=buildAlternateActionRow(trigger,ruleBinding,()=>{
        second.remove();add.hidden=false;row.dispatchEvent(new Event('change',{bubbles:true}));
      });
      adv.appendChild(second);add.hidden=true;
    };
    adv.appendChild(add);
    if(binding?.alternate_action)show(binding);
    add.addEventListener('click',()=>{
      let first;
      try{first=readBindingAction(row,trigger)}catch(error){notice(error.message);return}
      show({alternate_action:first});paintRowTags(row);
    });
    type.addEventListener('change',()=>{
      add.disabled=!type.value;
      if(!type.value){row.querySelector('.binding-alternate')?.remove();add.hidden=false}
    });
    hasAdv=true;
  }
  if(hasAdv){
    const more=document.createElement('button');more.type='button';more.className='binding-more';more.innerHTML=CHEVRON_SVG;
    more.title='更多设置';more.setAttribute('aria-label',trigger.name+' 更多设置');
    const open=expandedRows.has(trigger.key);more.setAttribute('aria-expanded',String(open));adv.hidden=!open;
    more.addEventListener('click',()=>{
      const next=adv.hidden;adv.hidden=!next;more.setAttribute('aria-expanded',String(next));
      if(next)expandedRows.add(trigger.key);else expandedRows.delete(trigger.key);
    });
    main.append(more);row.append(adv);
  }
  row.addEventListener('change',()=>paintRowTags(row));
  paintRowTags(row);
  return row;
}

// 要跳才碰得到的框。人在空中只停一瞬间，等不起，默认「扫过也按」。
const JUMP_ZONE_IDS=new Set(['headJump']);

// 框那几行「更多」里的一句话：哪些动作会扫过它、录的时候几次里扫过几次。
export function paintZoneConflictNotes(){
  const overlaps=kernelState?.zone_overlaps||{};
  for(const row of document.querySelectorAll('.binding-row[data-trigger^="zone."]')){
    const note=row.querySelector('.zone-conflict-note');if(!note)continue;
    const info=overlaps[row.dataset.trigger.slice(5)];
    const text=info?zoneConflictText(info):'';
    note.hidden=!text;if(note.textContent!==text)note.textContent=text;
  }
}

function zoneConflictText(info){
  const parts=(info.triggers||[]).map(trigger=>{
    const name=(profileTriggers().find(t=>t.key===trigger)||{}).name||trigger;
    const rate=info.rates?.[trigger]||{};
    return rate.source==='recorded'?`${name}（录的 ${rate.reps} 次扫过 ${rate.hits} 次）`:name;
  });
  return parts.length?`会扫过这里的动作：${parts.join('、')}`:'';
}

// 三个页签上的数字：这一栏现在有几行。
export function updateMapCounts(){
  for(const group of ['zones','body','voice']){
    const el=document.querySelector(`#mapTabs [data-count="${group}"]`);if(!el)continue;
    const n=[...document.querySelectorAll(`.binding-group[data-group="${group}"] .binding-row`)].filter(row=>!row.hidden).length;
    el.textContent=n?String(n):'';
  }
  const hiddenVoice=document.querySelector('.binding-group[data-group="voice"] .binding-row[hidden]');
  $('#addGameVoiceBtn').hidden=mapTab!=='voice'||!hiddenVoice;
}

function syncBodyGroup(){updateMapCounts()}

let mapTab='zones';

export function showMapTab(tab){
  mapTab=['zones','body','voice'].includes(tab)?tab:'zones';
  document.querySelectorAll('#mapTabs [data-tab]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.tab===mapTab)));
  document.querySelectorAll('#profileBindingRows .binding-group').forEach(group=>{group.hidden=group.dataset.group!==mapTab});
  $('#poseLibraryPanel').hidden=mapTab!=='body';
  document.querySelectorAll('[data-tab-caption]').forEach(el=>{el.hidden=el.dataset.tabCaption!==mapTab});
  updateMapCounts();
}

// 从动作库、自定义动作、开始页点过来，而表里还没有这一行：现加一行，不整表重画——
// 重画会冲掉别的行里还没存的改动。
function addBodyRow(triggerKey){
  const trigger=profileTriggers().find(t=>t.key===triggerKey&&isBodyTrigger(t));
  const rows=document.querySelector('.binding-group[data-group="body"] .binding-group-rows');
  if(!trigger||!rows)return null;
  rows.querySelector('.profile-empty')?.remove();
  const row=buildBindingRow(trigger);rows.appendChild(row);shownBodyRows.keys.add(trigger.key);
  syncBodyGroup();syncMotionConflictChoices();syncVoiceReleaseChoices();
  return row;
}

// 本游戏口令有十二个空位，没用的先不列：列十二行「不绑」，要找的那一行反而看不见。
function voiceSlotUsed(trigger){
  const binding=bindingFor(trigger);
  return !!(String(binding?.phrase||trigger.phrase||'').trim()||(binding&&!binding.disabled&&binding.action));
}

function emptyNote(text){const el=document.createElement('div');el.className='profile-empty';el.textContent=text;return el}

export function renderProfileBindingRows(){
  const box=$('#profileBindingRows');if(!box)return;box.replaceChildren();
  if(!gameProfile.selected){const group=document.createElement('div');group.className='binding-group';group.append(emptyNote('还没有可编辑的游戏配置。'));box.append(group);return}
  const profileId=gameProfile.selected.selected_id||gameProfile.selected.id;
  if(shownBodyRows.profile!==profileId){shownBodyRows.profile=profileId;shownBodyRows.keys.clear()}
  const triggers=profileTriggers();
  const groups=[
    {id:'zones',filter:t=>t.group==='zones',empty:'没有区域'},
    {id:'body',filter:isBodyTrigger,empty:'还没绑身体动作。在下面动作库里点「加到映射」。'},
    {id:'voice',filter:t=>t.group==='voice',empty:'还没有本游戏口令。'},
  ];
  for(const group of groups){
    const all=triggers.filter(group.filter);
    const items=group.id==='body'?all.filter(bodyRowWanted):all;
    const wrap=document.createElement('div');wrap.className='binding-group';wrap.dataset.group=group.id;
    const rows=document.createElement('div');rows.className='binding-group-rows';
    let shown=0;
    for(const trigger of items){
      const row=buildBindingRow(trigger);
      if(group.id==='voice'&&!voiceSlotUsed(trigger))row.hidden=true;else shown++;
      rows.appendChild(row);
    }
    if(!shown)rows.prepend(emptyNote(group.empty));
    wrap.append(rows);box.appendChild(wrap);
    if(group.id==='body')for(const trigger of items)shownBodyRows.keys.add(trigger.key);
  }
  showMapTab(mapTab);
  syncMotionConflictChoices();
  syncVoiceReleaseChoices();
  paintPoseMissingNotice();
}

function readBindingAction(container,trigger,ordinal=''){
  const type=container.querySelector('.binding-type')?.value||'';
  if(!type)return null;
  let target;
  if(type==='voice_release'){
    const ids=voiceReleaseTargetIds(container.querySelector('.voice-release-target')).map(id=>id.toLowerCase());
    target=ids.length===1?ids[0]:ids;
  }else{
    const raw=String(container.querySelector('.binding-target')?.value||'').trim();
    target=type==='macro'?raw.toLowerCase():raw.toUpperCase();
  }
  const name=trigger.name+(ordinal?`（${ordinal}）`:'');
  if(!target||(Array.isArray(target)&&!target.length))throw new Error(type==='macro'?`${name} 还没有选择要跑哪条宏`:type==='voice_release'?`${name} 还没有选择要停住哪条口令`:`${name} 还没有选择具体键位`);
  const behavior=trigger.tapOnly||type==='mouse_wheel'||type==='voice_release'||type==='system'?'tap':(container.querySelector('select.binding-behavior')?.value||container.querySelector('.binding-behavior')?.dataset.value||'hold');
  const action={type,target,behavior};
  const comboLead=container.querySelector('.combo-lead-ms');
  if(type==='gamepad'&&comboLead&&!comboLead.closest('.combo-lead-box')?.hidden&&(comboLead.dataset.explicit==='1'||comboLead.dataset.touched==='1')){
    action.combo_stick_lead_ms=Math.max(0,Math.min(200,Number(comboLead.value)||0));
  }
  return action;
}

function readProfileOverrides(){
  const overrides=structuredClone(gameProfile.overrides);
  for(const trigger of profileTriggers()){
    if(!profileDirty.has(trigger.key))continue;
    const row=document.querySelector(`.binding-row[data-trigger="${trigger.key}"]`);if(!row)continue;
    const type=row.querySelector('.binding-type')?.value||'';
    const pointChoices=trigger.group==='zones'?readZonePointChoices(row):{};
    if(!type){overrides[trigger.key]=Object.keys(pointChoices).length?{disabled:true,...pointChoices}:null;continue}
    const override={action:readBindingAction(row,trigger),...pointChoices};
    const alternate=row.querySelector('.binding-alternate');
    if(alternate){
      const mode=alternate.querySelector('.binding-alternate-mode')?.value;
      const selected=alternate.querySelector('.binding-rule-trigger')?.value;
      if(!selected)throw new Error(`${trigger.name} 还没有选择${mode==='cycle'?'重置区域或动作':'配合动作'}`);
      override.alternate_mode=mode;
      override.alternate_action=readBindingAction(alternate.querySelector('.binding-sequence-action'),trigger,'第 2 次');
      if(!override.alternate_action)throw new Error(`${trigger.name} 还没有设置另一输出的键位`);
      if(mode==='cycle'){
        override.reset_trigger=selected;
        override.extra_actions=[...alternate.querySelectorAll('.binding-sequence-action')].slice(1).map((row,index)=>{
          const action=readBindingAction(row,trigger,`第 ${index+3} 次`);
          if(!action)throw new Error(`${trigger.name} 第 ${index+3} 次还没有设置键位`);
          return action;
        });
      }else override.alternate_when=selected;
    }
    const withMotion=row.querySelector('.zone-with-motion-box');
    if(trigger.group==='zones'&&withMotion)override.with_motion=withMotion.checked;
    if(trigger.group==='voice'){
      const rawPhrase=row.querySelector('.voice-trigger-phrase')?.value.trim();
      const phrase=isGameVoiceKey(trigger.key)?normalizeGameVoicePhrase(rawPhrase):rawPhrase;
      if(!phrase)throw new Error(`${trigger.name} 还没有填写触发词`);
      override.phrase=phrase;
    }
    overrides[trigger.key]=override;
  }
  const conflicts=motionConflictsForSelection(selectedMotionIdsFromRows());
  if(conflicts.length)throw new Error(`动作冲突：${motionConflictText(conflicts)}。开合跳与双手举过头只能选择一个`);
  return overrides;
}

export async function saveProfileBindings(){
  clearTimeout(profileAutoSaveTimer);
  if(profileFlight){await profileFlight;return saveProfileBindings()}
  if(!profileDirty.size)return;
  const id=gameProfile.selected.selected_id||gameProfile.selected.id,revision=profileRevision;
  $('#profileSaveStatus').textContent='正在保存…';$('#retryProfileSaveBtn').hidden=true;
  profileFlight=(async()=>{
    const data=await post('/api/game-profiles/overrides',{profile_id:id,overrides:readProfileOverrides()});
    gameProfile.selected=data.profile;gameProfile.overrides=data.profile.overrides||{};
    if(revision===profileRevision)profileDirty.clear();
    renderProfileHeader();await refreshVoiceCommands();
  })();
  try{await profileFlight}
  catch(error){
    profileConflict=error.status===409;
    clearTimeout(profileSavedTimer);
    $('#profileSaveStatus').textContent='没保存上：'+error.message;$('#profileSaveStatus').classList.add('error');
    $('#retryProfileSaveBtn').textContent=profileConflict?'重新选择本游戏再保存':'重试保存';
    $('#retryProfileSaveBtn').hidden=false;throw error;
  }finally{profileFlight=null}
  if(profileDirty.size)return saveProfileBindings();
  profileConflict=false;
  // 存好了说一声就走，不在页头一直挂着。
  $('#profileSaveStatus').classList.remove('error');$('#profileSaveStatus').textContent='已保存';
  clearTimeout(profileSavedTimer);profileSavedTimer=setTimeout(()=>{if($('#profileSaveStatus').textContent==='已保存')$('#profileSaveStatus').textContent=''},1800);
}

let profileSavedTimer=0;

export async function retryProfileBindings(){
  if(!profileConflict)return saveProfileBindings();
  await profileOperation(async()=>{
    const id=gameProfile.selected.selected_id||gameProfile.selected.id;
    await post('/api/game-profiles/select',{id});
    profileConflict=false;
    await saveProfileBindings();
  });
}

export function scheduleProfileAutoSave(event){
  // Text inputs already saved their input event; blur must not restart a failed save
  // or move its retry button between pointer-down and pointer-up.  A checkbox has
  // no input event to rely on, so its change is the only signal it ever sends.
  if(event?.type==='change'&&event.target.matches('input:not([type=checkbox])'))return;
  const row=event?.target.closest('.binding-row');if(!row||profileSwitching)return;
  profileDirty.add(row.dataset.trigger);profileRevision++;
  clearTimeout(profileAutoSaveTimer);
  const pendingRule=row.querySelector('.binding-alternate .binding-rule-trigger');
  if(pendingRule&&!pendingRule.value){
    $('#profileSaveStatus').textContent='先选好配合的动作，才能保存';
    return;
  }
  $('#profileSaveStatus').textContent='正在保存…';
  profileAutoSaveTimer=setTimeout(()=>saveProfileBindings().catch(()=>{}),350);
}

export async function resetProfileBindings(){
  if(profileSwitching)return;
  await profileOperation(async()=>{
    await saveProfileBindings();
    const profile_id=gameProfile.selected.selected_id||gameProfile.selected.id;
    const data=await post('/api/game-profiles/overrides',{profile_id,overrides:{}});
    gameProfile.selected=data.profile;gameProfile.overrides={};
    // 恢复默认就是回到默认那几行，这次手动加进来的没绑的动作一起收回动作库。
    shownBodyRows.keys.clear();
    await refreshVoiceCommands();renderProfileHeader();renderProfileBindingRows();
    notice('已恢复这个游戏的默认按键');
  });
}
