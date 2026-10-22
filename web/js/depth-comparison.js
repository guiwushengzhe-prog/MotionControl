// 深度实验复用性能刷新；只运行所选候选，结果按模型保留，来源和尺寸逐项可见。
import {$,api,configurationOperation,isVisible,post,setProperty,setText} from './core.js';

let current=null,busy=false,lastSequence=null,operationError='';
const fmt=(value,unit='',digits=1)=>value!=null&&Number.isFinite(Number(value))?`${Number(value).toFixed(digits)}${unit}`:'—';
const names={dav2_metric_s:'V2-S 室内米制',vda_metric_s:'VDA-S 米制流式',zipdepth:'ZipDepth 相对逆深度'};

export function renderDepthComparison(state){
  if(!state||(state.revision!=null&&current?.revision!=null&&state.revision<current.revision))return;current=state;
  const config=state.config||{},result=state.results?.[config.model]||{},comparison=result.comparison||{};
  if(!busy){
    setProperty($('#depthComparisonEnabled'),'checked',!!config.enabled);
    for(const [id,key] of [['depthComparisonModel','model'],['depthComparisonSize','input_size'],['depthComparisonFps','max_fps']])setProperty($('#'+id),'value',String(config[key]??''));
  }
  for(const id of ['depthComparisonEnabled','depthComparisonModel','depthComparisonSize','depthComparisonFps'])setProperty($('#'+id),'disabled',busy);
  const stateLabel={off:'未开启',waiting:'等待电脑摄像头画面',loading:'正在加载模型',running:'对比运行中',stale:'等待新结果',error:'对比未完成'}[state.state]||'读取中';
  setText($('#depthComparisonStatus'),`${stateLabel}${result.device_name?' · '+result.device_name:''}${state.error?' · '+state.error:''}${operationError?' · '+operationError:''}`);
  setText($('#depthComparisonNote'),config.model==='zipdepth'?'相对逆深度需用Kinect校准尺度；尺度固定后，再用新帧验证。':'米制输出直接对照Kinect表面距离；不修改当前身体控制来源。');
  setProperty($('#depthComparisonCalibrate'),'hidden',config.model!=='zipdepth');
  setProperty($('#depthComparisonCalibrate'),'disabled',busy||state.state!=='running'||!comparison.reference_points||comparison.calibrating);
  setProperty($('#depthComparisonReset'),'disabled',busy||state.state!=='running');
  setText($('#depthComparisonCalibration'),comparison.calibration_message||'');
  setText($('#depthComparisonTiming'),`本轮实际 ${fmt(result.fps,' 帧/秒')} · 平均 ${fmt(result.inference_avg_ms,' 毫秒')} · 95%单帧不超过 ${fmt(result.inference_p95_ms,' 毫秒')} · 画面到结果 ${fmt(result.latency_ms,' 毫秒')}`);
  const ready=config.enabled&&state.preview_ready&&state.state==='running'&&result.age_ms<1500;
  setProperty($('#depthComparisonView'),'hidden',!ready);
  const previewKey=`${state.revision}:${config.model}:${state.sequence}`;
  if(ready&&isVisible($('#depthComparisonPanel'))&&lastSequence!==previewKey){lastSequence=previewKey;$('#depthComparisonPreview').src=`/api/depth-comparison/preview?seq=${encodeURIComponent(lastSequence)}`}
  const rows=Object.entries(names).map(([id,name])=>{
    const row=state.results?.[id]||{},m=row.comparison||{},shape=row.input_shape;
    return [name+(id===config.model?' · 当前':row.inference_avg_ms!=null?' · 上次':''),state.models?.[id]?.available?'已准备':'未准备',row.inference_avg_ms==null?(row.warmup_remaining?'正在预热':'尚未运行'):`${fmt(row.fps)}帧/秒 · ${fmt(row.inference_avg_ms)}毫秒`,shape?`${shape[1]}×${shape[0]}`:'—',fmt(m.mae_cm,'厘米'),fmt(m.p95_cm,'厘米'),fmt(m.stationary_jitter_cm,'厘米'),String(m.validation_samples??0),fmt(row.gpu_reserved_mb,'MB')];
  });
  const tbody=$('#depthComparisonRows');
  if(tbody.dataset.value!==JSON.stringify(rows)){
    tbody.dataset.value=JSON.stringify(rows);tbody.replaceChildren(...rows.map(values=>{const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=value;tr.append(td)}return tr}));
  }
  setText($('#depthComparisonPoints'),comparison.points?.length?comparison.points.map(p=>`${({left_shoulder:'左肩',right_shoulder:'右肩',left_hip:'左髋',right_hip:'右髋',left_knee:'左膝',right_knee:'右膝',left_ankle:'左踝',right_ankle:'右踝',left_elbow:'左肘',right_elbow:'右肘',left_wrist:'左腕',right_wrist:'右腕',head:'头部',neck:'颈部',spine_mid:'躯干',spine_base:'骨盆',spine_shoulder:'肩中点'})[p.name]||'表面参考点'}：Kinect ${fmt(p.z,'米',3)} · 模型 ${fmt(p.model_m,'米',3)} · 相差 ${fmt(p.error_cm,'厘米')}`).join('\n'):'当前没有可靠的同人同步Kinect表面参考；仍可查看模型画面与耗时。');
}

async function save(changes){
  if(busy)return;busy=true;operationError='';renderDepthComparison(current);
  try{await configurationOperation(async()=>renderDepthComparison(await post('/api/depth-comparison',changes)))}
  catch(error){operationError='操作未完成：'+error.message;try{current=await api('/api/depth-comparison')}catch{}}
  finally{busy=false;renderDepthComparison(current)}
}

$('#depthComparisonEnabled')?.addEventListener('change',event=>void save({enabled:event.target.checked}));
for(const [id,key] of [['depthComparisonModel','model'],['depthComparisonSize','input_size'],['depthComparisonFps','max_fps']])$('#'+id)?.addEventListener('change',event=>void save({[key]:key==='model'?event.target.value:Number(event.target.value)}));
$('#depthComparisonCalibrate')?.addEventListener('click',()=>void save({action:'calibrate'}));
$('#depthComparisonReset')?.addEventListener('click',()=>void save({action:'reset'}));
