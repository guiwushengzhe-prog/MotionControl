// 深度实验复用性能刷新；只运行所选候选，结果按模型保留，来源和尺寸逐项可见。
import {$,api,configurationOperation,isVisible,post,setProperty,setText} from './core.js';

let current=null,busy=false,lastSequence=null,operationError='',kinectDistance=null;
const fmt=(value,unit='',digits=1)=>value!=null&&Number.isFinite(Number(value))?`${Number(value).toFixed(digits)}${unit}`:'—';
const names={dav2_metric_s:'V2-S 室内米制',vda_metric_s:'VDA-S 米制流式',zipdepth:'ZipDepth 相对逆深度'};
const pointNames={left_shoulder:'左肩',right_shoulder:'右肩',left_hip:'左髋',right_hip:'右髋',left_knee:'左膝',right_knee:'右膝',left_ankle:'左踝',right_ankle:'右踝',left_elbow:'左肘',right_elbow:'右肘',left_wrist:'左腕',right_wrist:'右腕',head:'头部',neck:'颈部',spine_mid:'躯干',pelvis:'骨盆',chest:'肩中点'};

function pointText(p,relative,metricReady){
  const raw=relative?`模型相对值 ${fmt(p.raw,'',5)}（越大越近） · `:'';
  const distance=p.model_m==null&&relative?(metricReady?'超出可靠尺度范围，未换算米数':'待尺度校准，尚无米数'):`${relative?'换算距离':'原始模型'} ${fmt(p.model_m,'米',3)} · 相差 ${fmt(p.error_cm,'厘米')}`;
  const calibrated=!relative&&p.calibrated_m!=null?` · 校正后 ${fmt(p.calibrated_m,'米',3)} · 相差 ${fmt(p.calibrated_error_cm,'厘米')}`:'';
  return `${pointNames[p.name]||'表面参考点'}：Kinect ${fmt(p.z,'米',3)} · ${raw}${distance}${calibrated}`;
}

export function renderDepthComparison(state,distance){
  if(distance!==undefined)kinectDistance=distance;
  if(!state||(state.revision!=null&&current?.revision!=null&&state.revision<current.revision))return;current=state;
  const config=state.config||{},result=state.results?.[config.model]||{},comparison=result.comparison||{};
  if(!busy){
    setProperty($('#depthComparisonEnabled'),'checked',!!config.enabled);
    setProperty($('#depthComparisonAutoScale'),'checked',config.auto_scale!==false);
    for(const [id,key] of [['depthComparisonModel','model'],['depthComparisonScope','input_scope'],['depthComparisonSize','input_size'],['depthComparisonFps','max_fps']])setProperty($('#'+id),'value',String(config[key]??(key==='input_scope'?'full':'')));
  }
  for(const id of ['depthComparisonEnabled','depthComparisonModel','depthComparisonScope','depthComparisonAutoScale','depthComparisonSize','depthComparisonFps'])setProperty($('#'+id),'disabled',busy);
  const stateLabel={off:'未开启',waiting:'等待电脑摄像头画面',waiting_person:'等待可靠的人体位置',loading:'正在加载模型',running:'对比运行中',stale:'等待新结果',error:'对比未完成'}[state.state]||'读取中';
  setText($('#depthComparisonStatus'),`${stateLabel}${result.device_name?' · '+result.device_name:''}${state.error?' · '+state.error:''}${operationError?' · '+operationError:''}`);
  setText($('#depthComparisonNote'),config.auto_scale!==false?'取得可靠Kinect参考后自动采集3秒尺度，再固定下来比较后续画面；原始误差单独显示。':config.model==='zipdepth'?'相对输出尚无米数，可点“重新校准尺度”进行固定尺度对照。':'使用模型原始米制输出；可单独校准一个固定尺度系数。');
  setText($('#depthComparisonReference'),kinectDistance?`Kinect当前躯干表面距离：${fmt(kinectDistance.surface_m,'米',3)} · 范围 ${fmt(kinectDistance.surface_min_m,'米',3)}～${fmt(kinectDistance.surface_max_m,'米',3)} · ${kinectDistance.reference_points}个可靠躯干点（原厂直接测量）`:'Kinect当前躯干距离：等待可靠的同人同步深度；下表保留各模型上次结果。');
  setProperty($('#depthComparisonCalibrate'),'hidden',false);
  setProperty($('#depthComparisonReframe'),'hidden',config.input_scope!=='person');
  setProperty($('#depthComparisonReframe'),'disabled',busy||!config.enabled);
  setProperty($('#depthComparisonCalibrate'),'disabled',busy||state.state!=='running'||!comparison.reference_points||comparison.calibrating);
  setProperty($('#depthComparisonReset'),'disabled',busy||state.state!=='running');
  const bias=comparison.bias_cm;
  setText($('#depthComparisonCalibration'),[comparison.calibration_message,comparison.calibrating?`还需 ${fmt(comparison.calibration_remaining_s,'秒')}`:'',bias!=null?`${config.model==='zipdepth'?'换算距离':'原始距离'}累计平均${bias>=0?'偏远':'偏近'} ${fmt(Math.abs(bias),'厘米')}`:''].filter(Boolean).join(' · '));
  setText($('#depthComparisonTiming'),`本轮实际 ${fmt(result.fps,' 帧/秒')} · 平均 ${fmt(result.inference_avg_ms,' 毫秒')} · 95%单帧不超过 ${fmt(result.inference_p95_ms,' 毫秒')} · 画面到结果 ${fmt(result.latency_ms,' 毫秒')}`);
  const ready=config.enabled&&state.preview_ready&&state.state==='running'&&result.age_ms<1500;
  setProperty($('#depthComparisonView'),'hidden',!ready);
  const previewKey=`${state.revision}:${config.model}:${state.sequence}`;
  if(ready&&isVisible($('#depthComparisonPanel'))&&lastSequence!==previewKey){lastSequence=previewKey;$('#depthComparisonPreview').src=`/api/depth-comparison/preview?seq=${encodeURIComponent(lastSequence)}`}
  const rows=Object.entries(names).map(([id,name])=>{
    const row=state.results?.[id]||{},m=row.comparison||{},shape=row.input_shape;
    const relative=id==='zipdepth',scaleReady=m.scale_ready??(relative&&m.metric_ready),missing=m.calibrating?'尺度校准中':scaleReady?'等待验证新帧':m.calibration_attempted?'尺度校准未通过':'待尺度校准';
    const metric=value=>value==null?(row.comparison?missing:'—'):fmt(value,'厘米');
    const original=relative?(row.comparison?'相对值，无米数':'—'):metric(m.raw_mae_cm??m.mae_cm);
    const corrected=m.calibrated_mae_cm??(relative?m.mae_cm:null),p95=m.calibrated_p95_cm??(relative?m.p95_cm:null);
    const scope=row.config?.input_scope==='person'?' · 人体':' · 全图';
    return [name+(id===config.model&&config.enabled?' · 当前':row.inference_avg_ms!=null?' · 上次':''),state.models?.[id]?.available?'已准备':'未准备',row.inference_avg_ms==null?(row.warmup_remaining?'正在预热':'尚未运行'):`${fmt(row.fps)}帧/秒 · ${fmt(row.inference_avg_ms)}毫秒`,shape?`${shape[1]}×${shape[0]}${scope}`:'—',original,metric(corrected),metric(p95),m.stationary_jitter_cm==null?(row.comparison?'等待稳定片段':'—'):fmt(m.stationary_jitter_cm,'厘米'),`${m.calibrated_validation_samples??m.validation_samples??0} / ${m.reference_points??'—'}`,fmt(row.gpu_reserved_mb,'MB')];
  });
  const tbody=$('#depthComparisonRows');
  if(tbody.dataset.value!==JSON.stringify(rows)){
    tbody.dataset.value=JSON.stringify(rows);tbody.replaceChildren(...rows.map(values=>{const tr=document.createElement('tr');for(const value of values){const td=document.createElement('td');td.textContent=value;tr.append(td)}return tr}));
  }
  setText($('#depthComparisonPoints'),comparison.points?.length?comparison.points.map(p=>pointText(p,config.model==='zipdepth',comparison.metric_ready)).join('\n'):'当前没有可靠的同人同步Kinect表面参考；仍可查看模型画面与耗时。');
}

async function save(changes){
  if(busy)return;busy=true;operationError='';renderDepthComparison(current);
  try{await configurationOperation(async()=>renderDepthComparison(await post('/api/depth-comparison',changes)))}
  catch(error){operationError='操作未完成：'+error.message;try{current=await api('/api/depth-comparison')}catch{}}
  finally{busy=false;renderDepthComparison(current)}
}

$('#depthComparisonEnabled')?.addEventListener('change',event=>void save({enabled:event.target.checked}));
$('#depthComparisonAutoScale')?.addEventListener('change',event=>void save({auto_scale:event.target.checked}));
for(const [id,key] of [['depthComparisonModel','model'],['depthComparisonScope','input_scope'],['depthComparisonSize','input_size'],['depthComparisonFps','max_fps']])$('#'+id)?.addEventListener('change',event=>void save({[key]:['model','input_scope'].includes(key)?event.target.value:Number(event.target.value)}));
$('#depthComparisonCalibrate')?.addEventListener('click',()=>void save({action:'calibrate'}));
$('#depthComparisonReset')?.addEventListener('click',()=>void save({action:'reset'}));
$('#depthComparisonReframe')?.addEventListener('click',()=>void save({action:'reframe'}));
