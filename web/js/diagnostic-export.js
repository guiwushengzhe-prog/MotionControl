import {$,notice} from './core.js';
import {downloadFile} from './download.js';

const exportButton=$('#diagnosticExportBtn');
const helpButton=$('#helpDiagnosticExportBtn');
const status=$('#diagnosticExportStatus');

async function exportDiagnostics(event){
  if(exportButton.disabled)return;
  const fromHelp=event.currentTarget===helpButton;
  exportButton.disabled=true;
  helpButton.setAttribute('aria-disabled','true');
  status.textContent='正在生成诊断摘要…';
  if(fromHelp)notice(status.textContent);
  try{
    await downloadFile('/api/diagnostics/export',{fallbackName:'MotionControl-诊断摘要.json'});
    status.textContent='摘要已生成，请在浏览器下载列表中查看。';
  }catch(error){
    status.textContent=error.name==='TimeoutError'?'生成超时，请稍后重试':
      error instanceof TypeError?'本地服务无响应，请检查连接后重试':error.message||'摘要未能生成，请重试';
  }finally{
    exportButton.disabled=false;
    helpButton.removeAttribute('aria-disabled');
    if(fromHelp)notice(status.textContent);
  }
}

exportButton.addEventListener('click',exportDiagnostics);
helpButton.addEventListener('click',exportDiagnostics);
