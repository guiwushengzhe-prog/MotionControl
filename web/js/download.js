// 共用本地文件导出，下载失败不改变运行中的设置。
export async function downloadFile(url,{timeout=30000,fallbackName='MotionControl.json'}={}){
  const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',signal:AbortSignal.timeout(timeout)});
  if(!response.ok){const data=await response.json();throw new Error(data.error||'文件未能生成，请重试')}
  const blob=await response.blob(),objectURL=URL.createObjectURL(blob),link=document.createElement('a');
  link.href=objectURL;link.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1]||fallbackName;
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(objectURL),1000);
}
