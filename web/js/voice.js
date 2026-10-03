// 语音：唤醒词、口令检查、通用口令、口令列表。
import {$,api,configurationOperation,notice,post,setProperty,setText,syncChildren} from './core.js';
import {ACTION_TYPE_LABELS,SYSTEM_TARGET_NAMES,VOICE_SYSTEM_TARGETS,bindingsForDisplay,targetLabel} from './labels.js';
import {fillTargetControl,makeKeyCaptureInput} from './mapping.js';
import {gameProfile} from './state.js';

export let voiceInputReady=false;
export let voiceCatalog=[];

export const voice={status:null};
let voiceRevision=0;

function currentVoiceWakeWord(status=voice.status){
  return String(status?.wake_word??'').trim();
}

function systemVoiceWakeWord(status=voice.status){return String(status?.system_wake_word??(status?.wake_system_commands?currentVoiceWakeWord(status):'体感')).trim()}

export function isGameVoiceKey(key){return String(key||'').replace(/^voice\./,'').startsWith('game.profile_slot_')}

export function normalizeGameVoicePhrase(value){
  let phrase=String(value||'').trim();
  for(const prefix of [currentVoiceWakeWord(),'体感']){
    if(prefix&&phrase.startsWith(prefix)&&phrase.length>prefix.length)return phrase.slice(prefix.length).trim();
  }
  return phrase;
}

function renderVoiceGuide(status=voice.status){
  const wake=currentVoiceWakeWord(status),systemWake=systemVoiceWakeWord(status),example=`${wake}地图`;
  const wakeHint=$('#voiceWakeWordHint');
  setText(wakeHint,wake);
  const wakeExample=$('#voiceWakeExample');
  setText(wakeExample,example);
  setText($('#voiceWakeDescription'),wake?`通用口令先说「${wake}」，例如「${example}」`:'留空直接说通用口令，例如「地图」');
  setText($('#voiceSystemWakeDescription'),status?.wake_system_commands?(systemWake?`内置口令例如「${systemWake}紧急停止」`:'未填写唤醒词：内置口令也直接说，例如「紧急停止」'):'未勾选：内置口令仍先说「体感」。本游戏口令始终直接说。');
  document.querySelectorAll('.voice-prefix').forEach(item=>{const prefix=item.dataset.scope==='builtin'?systemWake:wake;setText(item,prefix);setProperty(item,'hidden',!prefix)});
  document.querySelectorAll('.voice-trigger-phrase').forEach(input=>{
    const game=isGameVoiceKey(input.closest('.binding-row')?.dataset.trigger);
    setProperty(input,'placeholder',game?'例如：爬绳':'完整口令');
    setProperty(input,'title',game?'直接说这句，不用唤醒词':`说出的完整口令，前面加「${wake}」`);
  });
}

/* 语音模型的词表里没有的字，写进口令就永远听不到——模型只是悄悄丢掉，不报错。所以
 * 在输入框底下照实说是哪个字。几个框一起出现时攒成一次请求。 */
const voiceCheckQueue=new Set();
let voiceCheckTimer=0;

function voiceCheckText(input){
  // 通用口令的可选前缀只在填写后拼上；本游戏口令直接检查填写的内容。
  return (input.classList.contains('voice-phrase')?currentVoiceWakeWord():'')+String(input.value||'').trim();
}

function voicePhraseTip(input){
  if(!input.voiceTip){
    const tip=document.createElement('small');tip.className='voice-phrase-tip';tip.hidden=true;input.voiceTip=tip;
    // 通用口令一行是网格，提示放在行尾、单独占满一行，不挤乱几栏。
    const row=input.closest('.voice-row');if(row)row.append(tip);else input.after(tip);
  }
  return input.voiceTip;
}

function queueVoiceCheck(input){voiceCheckQueue.add(input);clearTimeout(voiceCheckTimer);voiceCheckTimer=setTimeout(flushVoiceCheck,250)}

async function flushVoiceCheck(){
  const inputs=[...voiceCheckQueue].filter(input=>input.isConnected);voiceCheckQueue.clear();
  if(!inputs.length)return;
  const texts=inputs.map(voiceCheckText);
  let data;try{data=await post('/api/voice/check',{phrases:texts})}catch{return}
  if(!data.available)return;
  inputs.forEach((input,index)=>{
    if(voiceCheckText(input)!==texts[index])return;  // 查的时候又改了，下一轮再说
    const chars=data.results?.[index]?.unheard||[],tip=voicePhraseTip(input);
    tip.hidden=!chars.length;
    tip.textContent=chars.length?`${chars.map(char=>`「${char}」`).join('')}语音认不出，这句说了也听不到，换个说法`:'';
  });
}

export function watchVoicePhrase(input){input.addEventListener('input',()=>queueVoiceCheck(input));queueVoiceCheck(input)}

// 「停住语音按住」能停哪几条：本游戏口令里现在设成持续按住的那些（循环的宏也算，
// 它同样要另一个动作才停得下来）。读的是表里的当前状态而不是存下来的配置——刚把
// 口令 1 改成持续按住，别的行马上就该能选它，不用先等保存。自己停自己没有意义，
// 所以排除本行。
export function voiceCommandId(triggerKey){return String(triggerKey||'').replace(/^voice\./,'')}

function voiceCommandPhrase(id){
  id=voiceCommandId(id);
  const row=document.querySelector(`.binding-row[data-trigger="voice.${id}"] .voice-trigger-phrase`);
  const binding=bindingsForDisplay()[`voice.${id}`];
  const phrase=row?.value.trim()||binding?.phrase||voiceCatalog.find(item=>item.id===id)?.phrase||id;
  return isGameVoiceKey(id)?normalizeGameVoicePhrase(phrase):phrase;
}

export function voiceCommandName(id){
  id=voiceCommandId(id);
  const slot=id.startsWith('game.profile_slot_')?`口令 ${Number(id.replace('game.profile_slot_',''))}`:'';
  const phrase=voiceCommandPhrase(id);
  return slot&&phrase!==id?`${slot}「${phrase}」`:slot||phrase;
}

export function voiceCommandIds(value){
  const values=Array.isArray(value)?value:[value];
  return values.map(voiceCommandId).filter(Boolean);
}

export function voiceCommandPhrases(value){return voiceCommandIds(value).map(voiceCommandPhrase).filter(Boolean)}

export function voiceCommandNames(value){return voiceCommandIds(value).map(voiceCommandName).filter(Boolean)}

export function addVoiceRow(mapping={phrase:'',type:'keyboard',target:''}){const row=document.createElement('div');row.className='voice-row';const phrase=document.createElement('input');phrase.className='voice-phrase';phrase.placeholder='例：地图';phrase.title='填写要说的口令，唤醒词可留空';phrase.value=mapping.phrase||'';const phraseBox=document.createElement('div');phraseBox.className='voice-phrase-wrap';const prefix=document.createElement('span');prefix.className='voice-prefix';prefix.textContent=currentVoiceWakeWord();prefix.hidden=!currentVoiceWakeWord();phraseBox.append(prefix,phrase);const type=document.createElement('select');type.className='voice-type';for(const[value,label]of[['keyboard','键盘'],['gamepad','手柄'],['system','系统']]){const o=document.createElement('option');o.value=value;o.textContent=label;type.appendChild(o)}type.value=mapping.type||'keyboard';const target=document.createElement('span');target.className='voice-target-cell';const fillVoiceTarget=value=>{
  if(type.value==='system'){target.replaceChildren();const sel=document.createElement('select');sel.className='binding-target';for(const[v,t]of VOICE_SYSTEM_TARGETS){const o=document.createElement('option');o.value=v;o.textContent=t;sel.appendChild(o)}if([...sel.options].some(o=>o.value===value))sel.value=value;target.appendChild(sel);return}
  // Voice rows can be built before the action catalog arrives.  Gamepad falls
  // back to its own built-in key list, but keyboard is only free text because
  // the catalog says so, and without it the picker would come out empty.
  if(type.value==='keyboard'&&!gameProfile.actions?.keyboard){target.replaceChildren(makeKeyCaptureInput('binding-target',value||''));return}
  fillTargetControl(target,type.value,value);
};fillVoiceTarget(mapping.target||'');const remove=document.createElement('button');remove.type='button';remove.className='btn voice-remove';remove.textContent='删除';remove.addEventListener('click',()=>{row.remove();if(!$('#voiceRows').children.length)addVoiceRow()});const behavior=document.createElement('select');behavior.className='voice-behavior';behavior.setAttribute('aria-label','语音动作方式');for(const[value,label]of [['tap','点一下'],['hold','持续按住'],['release','松开']]){const option=document.createElement('option');option.value=value;option.textContent=label;behavior.appendChild(option)}behavior.value=mapping.behavior||'tap';behavior.title='点按一次、持续按住，或用另一句口令松开';const systemBehavior=document.createElement('span');systemBehavior.className='voice-system-behavior';systemBehavior.textContent='执行一次';const syncBehavior=()=>{const system=type.value==='system';behavior.disabled=system;behavior.hidden=system;systemBehavior.hidden=!system;if(system)behavior.value='tap'};type.addEventListener('change',()=>{fillVoiceTarget(target.querySelector('.binding-target')?.value||'');syncBehavior()});syncBehavior();row.append(phraseBox,type,target,behavior,systemBehavior,remove);$('#voiceRows').appendChild(row);watchVoicePhrase(phrase)}

function readVoiceMappings(){const rows=[...document.querySelectorAll('.voice-row')],items=[],old=new Map((voice.status?.mappings||[]).map(m=>[m.phrase,m]));for(const row of rows){const phrase=row.querySelector('.voice-phrase').value.trim(),type=row.querySelector('.voice-type').value,target=(row.querySelector('.binding-target')?.value||'').trim();if(!phrase&&!target)continue;if(!phrase||!target)throw new Error('每条口令都要填「说什么」和「输出什么」');const behavior=type==='system'?'tap':row.querySelector('.voice-behavior').value;const item={phrase,type,target,behavior},previous=old.get(phrase);if(previous?.synonyms?.length)item.synonyms=[...previous.synonyms];items.push(item)}return items}

export function renderVoiceRows(items){
  const rows=[...document.querySelectorAll('#voiceRows .voice-row')];
  const current=rows.map(row=>[row.querySelector('.voice-phrase').value,row.querySelector('.voice-type').value,row.querySelector('.binding-target')?.value||'',row.querySelector('.voice-behavior').value]);
  const desired=(items||[]).map(item=>[item.phrase||'',item.type||'keyboard',item.target||'',item.type==='system'?'tap':item.behavior||'tap']);
  if(rows.length&&JSON.stringify(current)===JSON.stringify(desired.length?desired:[['','keyboard','','tap']]))return;
  $('#voiceRows').replaceChildren();for(const m of items||[])addVoiceRow(m);if(!$('#voiceRows').children.length)addVoiceRow();
}

function renderVoiceStatus(s=voice.status){
  if(!s)return;voice.status=s;renderVoiceGuide(s);const has=!!s.model_ready,connected=!!s.connected;const isSingleKws=String(s.recognizer_mode||'').includes('single_stage')||String(s.recognizer_mode||'').includes('kws');
  setText($('#voiceMode'),has?(isSingleKws?`短语识别 · ${s.supported_count||0} 条`:`语音 · ${s.supported_count||0} 条`):'未就绪');setProperty($('#voiceMode'),'className','tag '+(has?'ok':'warn'));
  const pcOk=connected&&s.source_kind==='computer'&&s.available&&s.model_ready&&s.audio_ready&&(s.audio_alive||s.stream_alive);const phoneOk=connected&&s.source_kind!=='computer';const ready=pcOk||phoneOk;voiceInputReady=ready;
  setText($('#voicePill'),ready?'在听':(connected?'准备中':'没开'));setProperty($('#voicePill'),'className','tag '+(ready?'ok':(connected?'warn':'plain')));
  const partial=String(s.last_partial||s.partial||'').trim();
  const phrase=String(s.last_final||s.final||s.last_command||'').trim();
  // 卡片上只写怎么说；没准备好时写卡在哪。听到的那一句浮在页面底下，几秒后自己走。
  const voiceText=ready?(currentVoiceWakeWord(s)?`通用口令先说「${currentVoiceWakeWord(s)}」；本游戏口令直接说`:'通用、本游戏口令直接说')
    :!s.available||!has?'语音模型没装好':!connected?(s.source_kind==='phone'?'等手机连上':'麦克风没打开'):'麦克风没声音，或者还在准备';
  if($('#voiceStatus').textContent!==voiceText)$('#voiceStatus').textContent=voiceText;
  announceVoice(s,phrase);
  const modelPath=s.model_path||s.command_model_path||'—';const mp=$('#voiceModelPath');setText(mp,'模型：'+modelPath);setProperty(mp,'title',modelPath);
  renderPersonalVoice(s);
  const clash=$('#voiceConflicts');if(clash){const list=s.phrase_conflicts||[];setProperty(clash,'hidden',!list.length);setText(clash,list.length?`${list.join('；')}。同名的只有一条会生效，改掉其中一条的说法。`:'')}
  // 换游戏、装别人的配置带进来的口令没经过输入框，这里兜底照实说。
  const unheard=$('#voiceUnheard');if(unheard){const list=s.unheard||[];setProperty(unheard,'hidden',!list.length);setText(unheard,list.length?`这几句口令里有语音认不出的字，说了也听不到：${list.slice(0,6).map(item=>`${item.phrase}（${(item.chars||[]).join('、')}）`).join('；')}${list.length>6?` 等 ${list.length} 句`:''}。换个说法。`:'')}
  const diag=$('#voiceDiagnostic');setText(diag,[`模式：${s.recognizer_mode||'—'}`,`词条：${s.supported_count??'—'}`,`模型：${modelPath}`,`音频：${s.audio_ready?'已准备':'未准备'} / ${s.audio_alive||s.stream_alive?'运行中':'空闲'}`,`音量：${Number(s.rms||0).toFixed(0)} · 字节：${s.bytes_received||0}`,`实时识别：${partial||'—'}`,`最后完成：${phrase||'—'}`,`电脑执行：${s.last_executed===true?'已执行':s.last_executed===false?'未执行':'未确认'}`,`错误：${s.last_error||'—'}`].join('\n'));
}

/* 自定义急停沿用通用口令的可选前缀，但仍独立保存、不分享，且直接走急停路径。
 * 固定急停的前缀由“同时用于内置系统口令”决定。 */
const EMERGENCY_TARGET='EMERGENCY_STOP',BUILT_IN_STOP='紧急停止';

const isEmergencyRow=item=>item.type==='system'&&item.target===EMERGENCY_TARGET;

export function voiceRowsFromStatus(s){
  const wake=currentVoiceWakeWord(s);
  const stops=(s?.custom_emergency_stop_phrases??(s?.emergency_stop_phrases||[])
    .filter(phrase=>phrase!==(s?.builtin_emergency_phrase||`${systemVoiceWakeWord(s)}${BUILT_IN_STOP}`))
    .map(phrase=>wake&&String(phrase).startsWith(wake)?String(phrase).slice(wake.length):String(phrase)))
    .filter(Boolean)
    .map(phrase=>({phrase,type:'system',target:EMERGENCY_TARGET,behavior:'tap'}));
  return [...stops,...(s?.mappings||[])];
}

export async function saveVoiceMappings(){
  const rows=readVoiceMappings();
  const revision=++voiceRevision;
  const s=await post('/api/voice/config',{mappings:rows.filter(item=>!isEmergencyRow(item)),
    emergency_stop_phrases:rows.filter(isEmergencyRow).map(item=>item.phrase)});
  if(revision===voiceRevision){++voiceRevision;voice.status=s;renderVoiceStatus(s);await refreshVoiceCommands()}return s;
}

// 唤醒词只属于你：不跟游戏走、也不跟配置分享出去。
function renderPersonalVoice(status){
  const wake=$('#wakeWord');
  // 正在输入就不覆盖。语音状态 0.9 秒刷一次，不让开就会把手里打一半的字抹掉。
  if(!wake||document.activeElement===wake)return;
  const next=status?.wake_word??'';
  if(wake.value!==next){wake.value=next;queueVoiceCheck(wake)}
  setProperty($('#wakeSystemCommands'),'checked',!!status?.wake_system_commands);
}

async function saveWakeWord(){
  const say=(text,kind='')=>{const el=$('#personalVoiceStatus');if(el){el.textContent=text;el.className=kind==='error'?'statusline error':'statusline'}};
  const value=String($('#wakeWord').value||'').trim();
  const system=$('#wakeSystemCommands').checked;
  if(value===voice.status?.wake_word&&system===!!voice.status?.wake_system_commands)return;
  try{
    await configurationOperation(async()=>{
    // mappings 要原样带上：configure 是整份替换，不带等于把口令全删了。
    const revision=++voiceRevision;
    const s=await post('/api/voice/config',{mappings:voice.status?.mappings||[],wake_word:value,wake_system_commands:system});
    if(revision!==voiceRevision)return;
    ++voiceRevision;
    voice.status=s;renderVoiceStatus(s);await refreshVoiceCommands();say(value?'唤醒词已保存':'已保存：通用口令直接说');
    // 可选前缀改变后，重新检查实际说出的整句。
    document.querySelectorAll('.voice-phrase').forEach(queueVoiceCheck);
    });
  }catch(error){say(error.message,'error')}
}

document.getElementById('wakeWord')?.addEventListener('change',saveWakeWord);
document.getElementById('wakeSystemCommands')?.addEventListener('change',saveWakeWord);

if($('#wakeWord'))watchVoicePhrase($('#wakeWord'));

let voiceHeardKey=null;

function announceVoice(s,phrase){
  const key=[s.commands_heard??'',phrase,s.last_action||'',s.last_executed??''].join('|');
  if(voiceHeardKey===null){voiceHeardKey=key;return}
  if(key===voiceHeardKey)return;voiceHeardKey=key;
  if(!phrase)return;
  if(s.last_action==='wake')notice(`听到「${currentVoiceWakeWord(s)}」，接着说口令`);
  else if(s.last_executed===true)notice(`「${phrase}」✓`);
  else if(s.last_executed===false)notice(`听到「${phrase}」，没执行${s.last_error?`：${s.last_error}`:''}`);
}

export async function refreshVoice(){const revision=voiceRevision;try{const data=await api('/api/voice/status');if(revision===voiceRevision){voice.status=data;renderVoiceStatus(data)}return true}catch{if(revision===voiceRevision){voiceInputReady=false;$('#voiceStatus').textContent='语音状态无法确认'}return false}}

function voiceActionLabel(action){if(!action)return '当前游戏未启用';if(action.type==='system')return '系统功能 · '+(SYSTEM_TARGET_NAMES.get(action.target)||action.target||'');if(action.type==='voice_release')return `${ACTION_TYPE_LABELS.voice_release} · ${voiceCommandNames(action.target).join('、')}`;return `${ACTION_TYPE_LABELS[action.type]||action.type} · ${targetLabel(action)} · ${{tap:'点按',hold:'持续按住',release:'松开'}[action.behavior||'tap']||'点按'}`}

function renderVoiceCommandCard(command,card){
  if(!card){card=document.createElement('div');card.className='voice-command-card';card.setAttribute('role','listitem');card.append(document.createElement('div'),document.createElement('small'))}
  setText(card.firstElementChild,command.phrase||'');
  setText(card.lastElementChild,command.system_fixed?(command.label||''):[command.label,voiceActionLabel(command.effective_action)].filter(Boolean).join(' · '));
  return card;
}

const voiceCommandSections=new Map();

function renderVoiceCommandCatalog(commands){
  voiceCatalog=Array.isArray(commands)?commands:[];
  const full=$('#voiceCommandGrid'),sections=[];
  const mapped=action=>!!action&&action.type!=='none'&&!!action.type&&(Array.isArray(action.target)?action.target.length>0:!!String(action.target||'').trim());
  const shared=voiceCatalog.some(item=>item.scope)?voiceCatalog.filter(item=>item.scope==='shared'&&mapped(item.effective_action))
    :voiceRowsFromStatus(voice.status).filter(item=>mapped(item)).map(item=>({
      phrase:currentVoiceWakeWord()+item.phrase,label:'',effective_action:{type:item.type,target:item.target,behavior:item.behavior||'tap'},
    }));
  for (const [name,items] of [
    ['内置口令 · 不能改',voiceCatalog.filter(item=>item.system_fixed)],
    ['本游戏口令',voiceCatalog.filter(item=>String(item.id||'').startsWith('game.profile_slot_')&&mapped(item.effective_action))],
    ['通用口令 · 所有游戏',shared],
  ]){
    if(!items.length)continue;
    let group=voiceCommandSections.get(name);
    if(!group){
      const section=document.createElement('section');section.className='voice-group';
      const title=document.createElement('h3');title.textContent=name;
      const grid=document.createElement('div');grid.className='voice-command-grid';section.append(title,grid);
      group={section,grid,cards:new Map()};voiceCommandSections.set(name,group);
    }
    const cards=new Map(),rows=items.map((item,index)=>{
      const key=item.id||String(index),card=renderVoiceCommandCard(item,group.cards.get(key));cards.set(key,card);return card;
    });
    syncChildren(group.grid,rows);group.cards=cards;sections.push(group.section);
  }
  syncChildren(full,sections);
}

export async function refreshVoiceCommands(){try{const data=await api('/api/voice/commands');renderVoiceCommandCatalog(data.commands||[])}catch{renderVoiceCommandCatalog([])}}
