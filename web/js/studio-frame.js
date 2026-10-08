// 人物帧属于哪一版遮脸设置，绘制前再向服务核对；外部直播页也不能回显迟到的原脸。
export async function readStudioFrame(){
  const response=await fetch('/api/studio/frame.png?t='+Date.now(),{cache:'no-store',signal:AbortSignal.timeout(2500)});
  if(!response.ok||response.status===204)return null;
  const revision=response.headers.get('X-Studio-Revision');
  const blob=await response.blob();
  if(revision===null)return null;
  const stateResponse=await fetch('/api/studio/state',{cache:'no-store',signal:AbortSignal.timeout(2500)});
  if(!stateResponse.ok)return null;
  const state=await stateResponse.json(),current=state.status?.revision??state.portrait?.revision;
  if(current===undefined||String(current)!==revision)return null;
  return createImageBitmap(blob);
}
