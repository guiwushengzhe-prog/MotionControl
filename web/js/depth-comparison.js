// 沿用现有性能轮询，只显示单目前后倾；旧三模型对比面板已移除。
import {$,api,configurationOperation,post,setProperty,setText} from './core.js';

let current=null,busy=false,recordingBusy=false,operationError='';
const fmt=(value,unit='')=>value!=null&&Number.isFinite(Number(value))?`${Number(value).toFixed(1)}${unit}`:'—';
const stateName=value=>({forward:'前倾',neutral:'中立',back:'后倾'})[value]||'无有效结果';

function renderRecording(state){
  const record=state.lean_recording||{},latest=record.latest;
  if(!recordingBusy)setProperty($('#leanComparisonRecording'),'checked',record.enabled===true);
  setProperty($('#leanComparisonRecording'),'disabled',recordingBusy);
  setProperty($('#leanComparisonRecordingPanel'),'hidden',!record.enabled&&!record.file&&!record.error);
  const fresh=latest&&Number.isFinite(Number(latest.sample_at))&&state.results?.mocapnet_personal?.age_ms<1500;
  const label=record.enabled?(fresh&&latest.comparable?
    `正在记录 · 单目${stateName(latest.monocular?.state)} / 深度${stateName(latest.depth?.state)} · ${latest.agree?'一致':'不一致'}`:
    `正在记录 · ${fresh&&latest.reason?latest.reason:'等待新画面与可靠深度参考'}`):'记录已关闭，文件已保留';
  setText($('#leanComparisonRecordingState'),record.error||label);
  setText($('#leanComparisonRecordingCounts'),`同帧有效 ${record.valid||0} 帧 · 一致 ${fmt(record.agreement_percent,'%')} · 不一致 ${record.disagree||0} 帧 / ${fmt(record.mismatch_seconds,'秒')} · 不可比较 ${record.invalid||0} 帧`);
  setText($('#leanComparisonRecordingClasses'),['forward','neutral','back'].map(key=>{
    const row=record.matrix?.[key]||{},total=Object.values(row).reduce((sum,n)=>sum+(Number(n)||0),0);
    return `深度${stateName(key)}：一致 ${row[key]||0} / ${total}`;
  }).join(' · '));
  setText($('#leanComparisonRecordingRecent'),record.recent_disagreements?.length?
    '最近不一致：'+record.recent_disagreements.slice(0,3).map(item=>`单目${stateName(item.monocular)} / 深度${stateName(item.depth)}（${fmt(item.seconds,'秒')}，${item.frames}帧）`).join('；'):'尚无不一致片段');
  setText($('#leanComparisonRecordingFile'),record.file?`记录文件：${record.file}`:`保存目录：${record.directory||'用户数据目录'}`);
}

export function renderDepthComparison(state){
  if(!state||(state.revision!=null&&current?.revision!=null&&state.revision<current.revision))return;
  current=state;
  renderRecording(state);
  const config=state.config||{},selected='mocapnet_personal',result=state.results?.[selected]||{};
  if(!busy){
    setProperty($('#monocularLeanModel'),'value',selected);
    setProperty($('#monocularLeanFps'),'value',String(Math.max(15,Number(config.max_fps)||15)));
  }
  const installed=state.models?.[selected]?.available===true;
  setProperty($('#monocularLeanModel'),'disabled',busy||!installed);
  setProperty($('#monocularLeanFps'),'disabled',busy);
  setText($('#monocularLeanModelNote'),installed?'复用已有骨骼点，仅处理器计算；先站直3秒，再做前倾回正、后倾回正':'个人研究模型尚未准备，请先完成本机模型登记');
  const active=state.control_requested&&state.active_model===selected;
  const label={off:'未开启',waiting:'等待电脑摄像头',waiting_person:'等待可靠人体骨骼',loading:'正在加载个人研究模型',running:'前后倾识别中',stale:'等待新结果',error:'识别未完成'}[state.state]||'等待状态';
  setText($('#monocularLeanRuntime'),[active?label:'将“前后倾来源”选择为单目前后倾后启用',state.error,operationError].filter(Boolean).join(' · '));
  setText($('#monocularLeanTiming'),active?`实际 ${fmt(result.fps,'帧/秒')} · 模型平均 ${fmt(result.inference_avg_ms,'毫秒')} · 95%单帧不超过 ${fmt(result.inference_p95_ms,'毫秒')} · 画面到结果 ${fmt(result.latency_ms,'毫秒')} · 仅处理器计算`:'' );
}

async function save(changes){
  if(busy)return;
  busy=true;operationError='';renderDepthComparison(current);
  try{await configurationOperation(async()=>renderDepthComparison(await post('/api/depth-comparison',{...changes,enabled:false,monocular_model:'mocapnet_personal'})))}
  catch(error){operationError='操作未完成：'+error.message;try{current=await api('/api/depth-comparison')}catch{}}
  finally{busy=false;renderDepthComparison(current)}
}

$('#monocularLeanModel')?.addEventListener('change',()=>void save({}));
$('#monocularLeanFps')?.addEventListener('change',event=>void save({max_fps:Number(event.target.value)}));
$('#leanComparisonRecording')?.addEventListener('change',async event=>{
  if(recordingBusy)return;
  const enabled=event.target.checked;
  recordingBusy=true;operationError='';renderDepthComparison(current);
  try{await configurationOperation(async()=>renderDepthComparison(await post('/api/lean-comparison-recording',{enabled})))}
  catch(error){operationError='对照记录设置未完成：'+error.message;try{current=await api('/api/depth-comparison')}catch{}}
  finally{recordingBusy=false;renderDepthComparison(current)}
});
