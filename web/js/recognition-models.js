import {$,api,notice,post} from './core.js';

export function renderRecognitionPerformance(data){
  if(data.source&&data.source!=='computer')return;
  const status=$('#poseModelStatus'),gesture=$('#gestureModelStatus');
  if(status&&data.actual_model){
    const name=data.actual_model==='heavy'?'高精度模型':'完整模型';
    status.textContent=data.running===false?'下次识别按所选模型运行':data.model_preference==='auto'?`${name} · ${data.auto_model_reason}`:`使用${name}`;
  }
  if(gesture&&data.hand_model_state){
    gesture.textContent=data.hand_model_state==='ready'?'已加载 · 只识别配置需要的手'
      :data.hand_model_state==='error'?'手势模型暂不可用：'+(data.hand_model_error||'请检查模型文件')
      :'需要握拳等手势功能时自动加载';
  }
}

export async function refreshRecognitionModels(){
  try{
    const data=await api('/api/recognition/models'),select=$('#poseModelSelect');
    if(document.activeElement!==select)select.value=data.model_preference||'auto';
    for(const key of ['full','heavy']){
      const option=select.querySelector(`option[value="${key}"]`);
      option.disabled=!data.models?.find(model=>model.id==='mp-'+key)?.available;
    }
    $('#sendHeavyModelBtn').disabled=!data.models?.find(model=>model.id==='mp-heavy')?.available;
    renderRecognitionPerformance(data);return true;
  }catch{return false}
}

export async function selectPoseModel(preference){
  const select=$('#poseModelSelect');select.disabled=true;
  try{renderRecognitionPerformance(await post('/api/recognition/models',{preference},90000));}
  finally{select.disabled=false;await refreshRecognitionModels()}
}

export async function sendHeavyModel(){
  const button=$('#sendHeavyModelBtn');button.disabled=true;
  try{await post('/api/recognition/models/send',{});notice('已通知手机接收高精度模型；接收完成后，手机会显示模型选择。');}
  finally{button.disabled=false}
}
