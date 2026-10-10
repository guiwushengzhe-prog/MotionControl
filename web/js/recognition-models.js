import {$,api,notice,post} from './core.js';

let heavyModel,phoneConnected=false,sending=false;
function renderPhoneModel(){
  const button=$('#sendHeavyModelBtn'),status=$('#phoneModelStatus');
  if(!button||!status)return;
  button.disabled=sending||!phoneConnected||!heavyModel?.available;
  const size=heavyModel?.size_bytes?` · ${(heavyModel.size_bytes/1024/1024).toFixed(1)} MB`:'';
  status.textContent=sending?'正在通知手机下载…':heavyModel===undefined?'正在读取模型信息…':!heavyModel?.available?'电脑尚未安装高精度模型':
    phoneConnected?'按需下载，完成后在手机选择'+size:'连接手机后可添加'+size;
}
export function renderPhoneModelState(status){
  phoneConnected=!!(status.mobile_pose_connected||status.handheld_connected);renderPhoneModel();
}

export function renderRecognitionPerformance(data){
  if(data.source&&data.source!=='computer')return;
  const status=$('#poseModelStatus'),gesture=$('#gestureModelStatus');
  if(status&&data.actual_model){
    const name=data.actual_model==='heavy'?'高精度模型':'标准模型';
    status.textContent=data.running===false?'下次识别按所选模型运行':data.model_preference==='auto'?`${name} · ${data.auto_model_reason}`:`使用${name}`;
  }
  if(gesture&&data.hand_model_state){
    gesture.textContent=data.hand_model_state==='native'?'使用摄像头原厂张手、握拳识别'
      :data.hand_model_state==='ready'?'已加载 · 只识别配置需要的手'
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
    heavyModel=data.models?.find(model=>model.id==='mp-heavy')||null;renderPhoneModel();
    renderRecognitionPerformance(data);return true;
  }catch{return false}
}

export async function selectPoseModel(preference){
  const select=$('#poseModelSelect');select.disabled=true;
  try{renderRecognitionPerformance(await post('/api/recognition/models',{preference},90000));}
  finally{select.disabled=false;await refreshRecognitionModels()}
}

export async function sendHeavyModel(){
  if(sending)return;sending=true;renderPhoneModel();
  try{await post('/api/recognition/models/send',{});notice('已通知手机下载高精度模型；完成后可在手机选择，默认模型不变。');}
  finally{sending=false;renderPhoneModel()}
}
