// 本地备份与恢复复用配置操作队列、错误提示及完整退出确认。
import {$,api,configurationOperation,post} from './core.js';
import {followApplicationExit} from './lifecycle.js';
import {downloadFile} from './download.js';

let preview=null,busy=false;
const controls=()=>['backupExportBtn','backupImportBtn','backupRestoreBtn','backupPreviewClose','backupRestoreMode'].map(id=>$('#'+id));
async function action(task,status=$('#backupStatus')){
  if(busy)return;
  busy=true;controls().forEach(control=>{control.disabled=true});status.textContent='正在处理…';
  try{await task(status)}
  catch(error){status.textContent=error.name==='TimeoutError'?'处理超时，请核对程序状态后重试':error instanceof TypeError?'本地服务无响应，请检查连接后重试':error.message||'操作未完成，请重试'}
  finally{busy=false;controls().forEach(control=>{control.disabled=false})}
}

$('#backupExportBtn').addEventListener('click',()=>void action(async status=>{
  await configurationOperation(async()=>{
    await downloadFile('/api/data-backup/export',{fallbackName:'MotionControl-配置备份.zip'});
    status.textContent='备份已生成，请在浏览器下载列表中查看。';
  });
}));

$('#backupImportBtn').addEventListener('click',()=>$('#backupFile').click());
$('#backupFile').addEventListener('change',()=>{
  const file=$('#backupFile').files?.[0];$('#backupFile').value='';
  if(!file)return;
  void action(async status=>{
    preview=null;
    if(file.size>32*1024*1024)throw new Error('请选择不超过32兆的配置备份包');
    const result=await api('/api/data-backup/preview',{method:'POST',headers:{'Content-Type':'application/zip'},body:file,timeoutMs:30000});
    preview=result.preview;
    if(!preview?.token)throw new Error('备份预览无法读取，请重新选择文件');
    $('#backupPreviewSummary').textContent=`${preview.games} 个游戏配置 · ${preview.custom_games} 个自建游戏 · ${preview.macros} 条宏 · ${preview.poses} 个自定义动作`;
    const time=new Date(preview.created_at);$('#backupPreviewDate').textContent=`备份时间：${Number.isNaN(time.valueOf())?'未知':time.toLocaleString()} · ${preview.files.length} 个文件`;
    $('#backupRestoreMode').value=preview.same_data?'full':'portable';
    $('#backupPreviewFiles').replaceChildren(...preview.files.map(file=>{const item=document.createElement('li');item.textContent=`${file.name} · ${(file.bytes/1024).toFixed(1)} 千字节`;return item}));
    $('#backupPreviewStatus').textContent='';$('#backupPreviewDialog').showModal();status.textContent='备份检查通过，请核对恢复内容。';
  });
});
$('#backupPreviewClose').addEventListener('click',()=>$('#backupPreviewDialog').close());
$('#backupPreviewDialog').addEventListener('cancel',event=>{if(busy)event.preventDefault()});
$('#backupRestoreBtn').addEventListener('click',()=>void action(async status=>{
  if(!preview)throw new Error('请先选择并检查备份');
  await configurationOperation(async()=>{
    const result=await post('/api/data-backup/restore',{token:preview.token,mode:$('#backupRestoreMode').value},30000);
    $('#backupPreviewDialog').close();status.textContent='备份已准备恢复，正在退出程序…';
    followApplicationExit(result,{restore:true});
  });
},$('#backupPreviewStatus')));
