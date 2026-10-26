import {$} from './core.js';
import {downloadFile} from './download.js';

$('#diagnosticExportBtn').addEventListener('click',async event=>{
  const button=event.currentTarget,status=$('#diagnosticExportStatus');
  if(button.disabled)return;
  button.disabled=true;status.textContent='正在生成诊断摘要…';
  try{
    await downloadFile('/api/diagnostics/export',{fallbackName:'MotionControl-诊断摘要.json'});
    status.textContent='摘要已生成，请在浏览器下载列表中查看。';
  }catch(error){status.textContent=error.name==='TimeoutError'?'生成超时，请稍后重试':error instanceof TypeError?'本地服务无响应，请检查连接后重试':error.message||'摘要未能生成，请重试'}
  finally{button.disabled=false}
});
