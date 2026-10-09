import {api} from './core.js';

// 标准蓝牙心率测量：支持单字节、双字节和佩戴接触状态。
export function parseHeartRate(value){
  if(!(value instanceof DataView)||value.byteLength<2)return null;
  const flags=value.getUint8(0),wide=!!(flags&1);
  if(wide&&value.byteLength<3||(flags&4)&&!(flags&2))return null;
  const bpm=wide?value.getUint16(1,true):value.getUint8(1);
  return bpm>=25&&bpm<=250?bpm:null;
}

export class ComputerHeartRate{
  constructor(onChange=()=>{}){
    this.onChange=onChange;this.session=null;this.message='';
    this.supported=window.isSecureContext&&!!navigator.bluetooth;
    window.addEventListener('pagehide',()=>this.disconnect('',true));
  }
  get active(){return !!this.session}
  get connected(){return !!this.session?.ready}
  get name(){return this.session?.device?.name||'手环'}
  changed(){this.onChange()}
  async connect(){
    if(this.active||!this.supported)return;
    const session={id:crypto.randomUUID(),device:null,characteristic:null,claimed:false,
      ready:false,lastSent:-Infinity,pending:undefined,sending:false,timer:0};
    this.session=session;this.message='正在连接…';this.changed();
    const current=()=>{if(this.session!==session)throw new Error('cancelled')};
    try{
      // 直接在点击事件里弹出浏览器选择器，不能先等待网页请求。
      session.device=await navigator.bluetooth.requestDevice({filters:[{services:['heart_rate']}]});current();
      const server=await session.device.gatt.connect();current();
      const service=await server.getPrimaryService('heart_rate');current();
      session.characteristic=await service.getCharacteristic('heart_rate_measurement');current();
      const state=await api('/api/fitness/heart-rate-input',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({source:'computer',reader_id:session.id})});
      session.claimed=true;session.revision=state.heart_rate_revision;session.instance=state.heart_rate_instance;current();
      session.onValue=event=>{
        session.pending=parseHeartRate(event.target.value);
        this.schedule(session);
      };
      session.onDisconnect=()=>{if(this.session===session)this.disconnect('手环已断开，重新连接即可继续读取')};
      session.characteristic.addEventListener('characteristicvaluechanged',session.onValue);
      session.device.addEventListener('gattserverdisconnected',session.onDisconnect);
      await session.characteristic.startNotifications();current();
      session.ready=true;this.message='';this.changed();
    }catch(error){
      if(this.session===session){
        this.session=null;
        this.message=error.name==='NotFoundError'?'未选择手环，请先在手环上打开心率广播':
          error.name==='NotAllowedError'||error.name==='SecurityError'?'浏览器未允许蓝牙连接，请检查权限后重试':
          '连接失败，请确认电脑蓝牙已开启，手环正在广播心率';
      }
    }finally{
      if(!session.ready||this.session!==session){this.release(session);this.changed()}
    }
  }
  schedule(session){
    if(this.session!==session||session.sending||session.timer||session.pending===undefined)return;
    // 只保留最新读数，每秒最多发送一次；失去佩戴接触立即清除当前值。
    const wait=session.pending===null?0:Math.max(0,1000-(performance.now()-session.lastSent));
    session.timer=setTimeout(()=>{session.timer=0;void this.send(session)},wait);
  }
  async send(session){
    if(this.session!==session||session.pending===undefined)return;
    const bpm=session.pending;session.pending=undefined;session.sending=true;session.lastSent=performance.now();
    try{
      const state=await api('/api/fitness/heart-rate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({bpm,reader_id:session.id})});
      if(this.session===session){
        this.sync(state);
        if(this.session===session&&!state.accepted)this.disconnect('心率读取来源已改变');
      }
    }catch{
      if(this.session===session)this.disconnect('后台连接已断开，手环读取已停止');
    }finally{session.sending=false;this.schedule(session)}
  }
  sync(state){
    const session=this.session;
    if(!session?.claimed)return;
    if(state.heart_rate_instance!==session.instance){this.disconnect('后台已重新启动，请重新连接手环');return}
    if(state.heart_rate_revision<session.revision)return;
    if(state.heart_rate_source!=='computer'||state.heart_rate_reader!==session.id)
      this.disconnect('心率已切换到其他读取来源');
  }
  release(session,closing=false){
    clearTimeout(session.timer);session.pending=undefined;
    if(session.characteristic&&session.onValue)
      session.characteristic.removeEventListener('characteristicvaluechanged',session.onValue);
    if(session.device&&session.onDisconnect)
      session.device.removeEventListener('gattserverdisconnected',session.onDisconnect);
    if(session.device?.gatt?.connected)session.device.gatt.disconnect();
    if(!session.claimed)return;
    const body=JSON.stringify({action:'release',reader_id:session.id});
    if(closing&&navigator.sendBeacon('/api/fitness/heart-rate-input',new Blob([body],{type:'application/json'})))return;
    void api('/api/fitness/heart-rate-input',{method:'POST',headers:{'Content-Type':'application/json'},body,keepalive:true}).catch(()=>{});
  }
  disconnect(message='',closing=false){
    const session=this.session;this.session=null;this.message=message;
    if(session)this.release(session,closing);
    this.changed();
  }
}
