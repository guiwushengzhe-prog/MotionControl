// 完整退出与关闭网页分开；先保存草稿，收到确认后再停止页面轮询。
import {$,configurationOperation,post} from './core.js';
import {S} from './state.js';

let shutdownPid=null,checking=false,restorePending=false;
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
function markShutdown(state){S.shutdown=state;document.dispatchEvent(new Event('motioncontrol:shutdown-state'))}

async function checkShutdown(){
  if(checking)return;
  checking=true;
  const retry=$('#exitCheckBtn');retry.hidden=true;
  $('#exitStatus').textContent='正在释放摄像头、麦克风与游戏输出，请稍候…';
  try{
    const deadline=Date.now()+12000;
    while(Date.now()<deadline){
      try{
        const response=await fetch('/api/health',{cache:'no-store',signal:AbortSignal.timeout(1500)});
        if(response.ok){
          const data=await response.json();
          if(data.app==='MotionControl'&&Number.isInteger(data.pid)&&data.pid!==shutdownPid){
            markShutdown('replaced');
            $('#exitTitle').textContent='原程序已退出';
            $('#exitStatus').textContent='检测到新的 MotionControl 已启动，刷新页面即可连接。';
            retry.textContent='连接新程序';retry.hidden=false;retry.onclick=()=>location.reload();
            return;
          }
        }
      }catch(error){
        // 超时、忙碌响应或无法解析的内容都不能当成“已经退出”。
        if(error instanceof TypeError){
          markShutdown('stopped');
          $('#exitTitle').textContent=restorePending?'备份已准备恢复':'MotionControl 已退出';
          $('#exitStatus').textContent=restorePending?'程序已退出。请双击桌面 MotionControl 图标，下次启动会自动恢复配置；恢复前副本已保留。':'本地服务已关闭。可以关闭此页；再次使用时双击桌面图标。';
          return;
        }
      }
      await wait(500);
    }
    $('#exitStatus').textContent='退出尚未确认，部分设备可能仍在释放。可稍后重新核对。';
    retry.textContent='重新核对';retry.hidden=false;
  }finally{checking=false}
}

export function followApplicationExit(result,{restore=false}={}){
  if(result.shutting_down!==true||!Number.isInteger(result.pid))throw new Error('程序未确认退出，请重试');
  shutdownPid=result.pid;restorePending=restore;markShutdown('stopping');
  document.querySelector('main').inert=true;
  document.querySelector('header').inert=true;
  const dialog=$('#exitDialog');
  if(!dialog.open)dialog.showModal();
  void checkShutdown();
}

export async function exitApplication(canExit){
  if(S.shutdown)return;
  await configurationOperation(async()=>{
    if(S.shutdown)return;
    canExit();
    const result=await post('/api/shutdown',{});
    followApplicationExit(result);
  });
}

$('#exitCheckBtn').addEventListener('click',()=>void checkShutdown());
$('#exitDialog').addEventListener('cancel',event=>event.preventDefault());
