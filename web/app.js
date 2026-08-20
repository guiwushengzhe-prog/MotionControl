const $ = s => document.querySelector(s);
const video = $('#video');
const canvas = $('#canvas');
const ctx = canvas.getContext('2d');
const viewer = $('#viewer');

const MP_TASKS = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/vision_bundle.mjs';
const MP_WASM = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/wasm';
const MP_OFFICIAL_FULL = 'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task';
const MP_NAMES = ['nose','left_eye_inner','left_eye','left_eye_outer','right_eye_inner','right_eye','right_eye_outer','left_ear','right_ear','mouth_left','mouth_right','left_shoulder','right_shoulder','left_elbow','right_elbow','left_wrist','right_wrist','left_pinky','right_pinky','left_index','right_index','left_thumb','right_thumb','left_hip','right_hip','left_knee','right_knee','left_ankle','right_ankle','left_heel','right_heel','left_foot_index','right_foot_index'];
const EDGES = [['left_shoulder','right_shoulder'],['left_shoulder','left_elbow'],['left_elbow','left_wrist'],['right_shoulder','right_elbow'],['right_elbow','right_wrist'],['left_shoulder','left_hip'],['right_shoulder','right_hip'],['left_hip','right_hip'],['left_hip','left_knee'],['left_knee','left_ankle'],['right_hip','right_knee'],['right_knee','right_ankle']];

// Body-relative regions. They are anchored to stable torso/head/leg points, not to the triggering limb.
const BODY_ZONES = {
  leftHandUpper:{label:'Y',button:'Y',points:['left_wrist'],kind:'hand'},
  leftHandLower:{label:'X',button:'X',points:['left_wrist'],kind:'hand'},
  rightHandUpper:{label:'B',button:'B',points:['right_wrist'],kind:'hand'},
  rightHandLower:{label:'A',button:'A',points:['right_wrist'],kind:'hand'},
  leftFoot:{label:'LB',button:'LB',points:['left_ankle','left_heel','left_foot_index'],kind:'foot'},
  rightFoot:{label:'RB',button:'RB',points:['right_ankle','right_heel','right_foot_index'],kind:'foot'},
};
const zoneState = Object.fromEntries(Object.keys(BODY_ZONES).map(k=>[k,{inside:0,outside:0,pressed:false}]));
const zoneRects = {};
const motion={
  config:[],active:new Set(),lastSent:'',lastSend:0,sending:false,
  debounce:{march:{active:false,on:0,off:0},calf_back:{active:false,on:0,off:0},squat:{active:false,on:0,off:0},hands_up:{active:false,on:0,off:0}},
  step:{leftWas:false,rightWas:false,lastSide:'',lastAt:0,activeUntil:0}
};

let detector=null, stream=null, cameraRunning=false, switching=false, lastVideoTime=-1, currentPoseMap=null;
let modelAvailable=false, buttonSending=false, lastButtonSend=0, lastSentButtons='';

const head={
  calibrated:false, calibrating:false, stage:'', stageStarted:0, stageDuration:1500,
  centerYaw:[],centerPitch:[],leftYaw:[],rightYaw:[],upPitch:[],downPitch:[],torsoSamples:[],
  yaw0:0,yawLeft:0,yawRight:0,pitch0:0,pitchUp:0,pitchDown:0,torso0:NaN,
  rawYaw:NaN,rawPitch:NaN,normX:0,normY:0,filteredX:0,filteredY:0,outputX:0,outputY:0,
  deadzoneX:.08,deadzoneY:.12,gamma:2.2,maxPercentX:60,maxPercentY:45,
  enabled:true,invertX:false,invertY:false,lastUpdate:0,quality:'未校准'
};
const output={enabled:false,mode:'mouse',strength:160,lastSend:0,sending:false,server:null};
const voice={running:false,stream:null,ctx:null,source:null,processor:null,gain:null,chunks:[],pendingBytes:0,sending:false,status:null};
const overlay={win:null,canvas:null,ctx:null};

const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const mean=a=>a.length?a.reduce((x,y)=>x+y,0)/a.length:NaN;
function qtile(a,p){if(!a.length)return NaN;const b=[...a].sort((x,y)=>x-y);return b[Math.max(0,Math.min(b.length-1,Math.round((b.length-1)*p)))]}
const median=a=>qtile(a,.5);
function notice(t){$('#notice').textContent=t;$('#notice').style.display=t?'block':'none'}
async function api(path,opt){const r=await fetch(path,opt);if(!r.ok)throw new Error(`${r.status} ${await r.text()}`);return r.json()}
async function post(path,data){return api(path,{method:'POST',headers:{'Content-Type':'application/json; charset=utf-8'},body:JSON.stringify(data)})}
async function postBinary(path,data){return api(path,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:data})}

function midpoint(a,b){return{x:(a.x+b.x)/2,y:(a.y+b.y)/2}}
function distance(a,b){return Math.hypot(a.x-b.x,a.y-b.y)}
function torsoLength(map){
  const ls=map.left_shoulder,rs=map.right_shoulder,lh=map.left_hip,rh=map.right_hip;
  if(!ls||!rs||!lh||!rh||Math.min(ls.score,rs.score,lh.score,rh.score)<.3)return NaN;
  return distance(midpoint(ls,rs),midpoint(lh,rh));
}
function yawSignal(map){
  const nose=map.nose;if(!nose||nose.score<.35)return NaN;const vals=[];
  for(const [ln,rn] of [['left_ear','right_ear'],['left_eye','right_eye']]){
    const l=map[ln],r=map[rn];if(!l||!r||Math.min(l.score,r.score)<.35)continue;
    const dl=distance(nose,l),dr=distance(nose,r);if(dl>.003&&dr>.003)vals.push(Math.log((dl+1e-4)/(dr+1e-4)));
  }
  return vals.length?mean(vals):NaN;
}
function pitchSignal(map,torso){
  const nose=map.nose;if(!nose||nose.score<.35||!Number.isFinite(torso)||torso<.03)return NaN;
  const le=map.left_ear,re=map.right_ear;if(le&&re&&Math.min(le.score,re.score)>=.35)return (nose.y-midpoint(le,re).y)/torso;
  const l=map.left_eye,r=map.right_eye;if(l&&r&&Math.min(l.score,r.score)>=.35)return (nose.y-midpoint(l,r).y)/torso;
  return NaN;
}
function normalizeAxis(raw,center,negativeExtreme,positiveExtreme){
  if(!Number.isFinite(raw))return 0;const d=raw-center,neg=negativeExtreme-center,pos=positiveExtreme-center;
  if(d*pos>=0&&Math.abs(pos)>.005)return clamp(Math.abs(d/pos),0,1.5);
  if(d*neg>=0&&Math.abs(neg)>.005)return -clamp(Math.abs(d/neg),0,1.5);
  return 0;
}
function curveAxis(v,deadzone,maxPercent){
  const a=Math.abs(v);if(a<=deadzone)return 0;
  const u=clamp((a-deadzone)/Math.max(.001,1-deadzone),0,1);
  return Math.sign(v)*maxPercent*Math.pow(u,head.gamma);
}
function filterAxis(current,target,norm,deadzone,maxPercent){
  if(Math.abs(norm)<=deadzone)return 0;
  const intensity=clamp(Math.abs(target)/Math.max(1,maxPercent),0,1);
  const alpha=.10+.36*Math.sqrt(intensity);
  return current+alpha*(target-current);
}

function calibrationStage(now){
  const e=now-head.stageStarted,d=head.stageDuration;
  if(e<d)return'center';if(e<2*d)return'left';if(e<3*d)return'right';if(e<4*d)return'up';if(e<5*d)return'down';return'done';
}
function collectCalibration(rawYaw,rawPitch,torso,now){
  const stage=calibrationStage(now);head.stage=stage;if(stage==='done'){finishCalibration();return}
  if(stage==='center'){if(Number.isFinite(rawYaw))head.centerYaw.push(rawYaw);if(Number.isFinite(rawPitch))head.centerPitch.push(rawPitch);if(Number.isFinite(torso))head.torsoSamples.push(torso)}
  else if(stage==='left'&&Number.isFinite(rawYaw))head.leftYaw.push(rawYaw);
  else if(stage==='right'&&Number.isFinite(rawYaw))head.rightYaw.push(rawYaw);
  else if(stage==='up'&&Number.isFinite(rawPitch))head.upPitch.push(rawPitch);
  else if(stage==='down'&&Number.isFinite(rawPitch))head.downPitch.push(rawPitch);
  const labels={center:'正视',left:'左转',right:'右转',up:'抬头',down:'低头'};
  $('#calStatus').textContent='校准：'+labels[stage];
}
async function startCalibration(){
  if(!cameraRunning)await startCamera();if(!cameraRunning)return;
  head.calibrating=true;head.calibrated=false;head.stageStarted=performance.now();head.stage='center';
  for(const k of ['centerYaw','centerPitch','leftYaw','rightYaw','upPitch','downPitch','torsoSamples'])head[k]=[];
  head.filteredX=head.filteredY=head.outputX=head.outputY=0;$('#calBtn').disabled=true;$('#calStatus').textContent='校准：正视';
  notice('约 7.5 秒：正视 → 左转 → 右转 → 抬头 → 低头。某一方向区分不足也会降级继续使用。');
}
function finishCalibration(){
  head.calibrating=false;$('#calBtn').disabled=false;
  const yaw0=head.centerYaw.length?median(head.centerYaw):head.rawYaw;
  const pitch0=head.centerPitch.length?median(head.centerPitch):head.rawPitch;
  if(!Number.isFinite(yaw0)||!Number.isFinite(pitch0)){head.calibrated=false;$('#calStatus').textContent='未取得中心';notice('自动校准没有取得稳定中心。保持正视后点击“当前姿势设为中心”仍可使用。');return}
  let yl=head.leftYaw.length?median(head.leftYaw):NaN,yr=head.rightYaw.length?median(head.rightYaw):NaN;
  let pu=head.upPitch.length?median(head.upPitch):NaN,pd=head.downPitch.length?median(head.downPitch):NaN;
  const yawOK=Number.isFinite(yl)&&Number.isFinite(yr)&&(yl-yaw0)*(yr-yaw0)<0&&Math.min(Math.abs(yl-yaw0),Math.abs(yr-yaw0))>=.025;
  const pitchOK=Number.isFinite(pu)&&Number.isFinite(pd)&&(pu-pitch0)*(pd-pitch0)<0&&Math.min(Math.abs(pu-pitch0),Math.abs(pd-pitch0))>=.008;
  if(!yawOK){yl=yaw0-.10;yr=yaw0+.10}
  if(!pitchOK){const sign=Number.isFinite(pu)&&pu!==pitch0?Math.sign(pu-pitch0):-1;pu=pitch0+sign*.04;pd=pitch0-sign*.04}
  head.yaw0=yaw0;head.yawLeft=yl;head.yawRight=yr;head.pitch0=pitch0;head.pitchUp=pu;head.pitchDown=pd;
  if(head.torsoSamples.length)head.torso0=median(head.torsoSamples);
  const nx=head.centerYaw.map(v=>Math.abs(normalizeAxis(v,yaw0,yl,yr))),ny=head.centerPitch.map(v=>Math.abs(normalizeAxis(v,pitch0,pu,pd)));
  head.deadzoneX=yawOK&&nx.length?clamp(qtile(nx,.99)*1.8+.02,.06,.30):Number($('#deadX').value)/100;
  head.deadzoneY=pitchOK&&ny.length?clamp(qtile(ny,.99)*2+.025,.08,.34):Number($('#deadY').value)/100;
  $('#deadX').value=Math.round(head.deadzoneX*100);$('#deadY').value=Math.round(head.deadzoneY*100);syncControlLabels();
  head.calibrated=true;head.lastUpdate=0;head.filteredX=head.filteredY=0;
  const weak=[];if(!yawOK)weak.push('左右');if(!pitchOK)weak.push('上下');head.quality=weak.length?'降级：'+weak.join('、'):'自动完成';
  $('#calStatus').textContent=weak.length?'校准降级可用':'校准完成';notice(weak.length?'部分方向区分不足，已自动使用默认范围和当前手动死区，不阻止使用。':'');
}
function setCurrentCenter(){
  if(!Number.isFinite(head.rawYaw)||!Number.isFinite(head.rawPitch)){notice('当前还没有稳定的头部关键点。');return}
  head.yaw0=head.rawYaw;head.pitch0=head.rawPitch;head.yawLeft=head.yaw0-.10;head.yawRight=head.yaw0+.10;head.pitchUp=head.pitch0-.04;head.pitchDown=head.pitch0+.04;
  head.deadzoneX=Number($('#deadX').value)/100;head.deadzoneY=Number($('#deadY').value)/100;head.calibrated=true;head.quality='手动中心';head.lastUpdate=0;head.filteredX=head.filteredY=0;
  $('#calStatus').textContent='当前姿势为中心';notice('已用当前姿势作为中心；方向范围使用保守默认值，可直接测试。');
}

function updateHead(map,now){
  const torso=torsoLength(map),rawYaw=yawSignal(map),scale=head.calibrated&&Number.isFinite(head.torso0)?head.torso0:torso,rawPitch=pitchSignal(map,scale);
  head.rawYaw=rawYaw;head.rawPitch=rawPitch;if(head.calibrating)collectCalibration(rawYaw,rawPitch,torso,now);
  let x=head.calibrated?normalizeAxis(rawYaw,head.yaw0,head.yawLeft,head.yawRight):0;
  let y=head.calibrated?normalizeAxis(rawPitch,head.pitch0,head.pitchUp,head.pitchDown):0;
  if(head.invertX)x=-x;if(head.invertY)y=-y;head.normX=x;head.normY=y;
  const tx=head.enabled?curveAxis(x,head.deadzoneX,head.maxPercentX):0,ty=head.enabled?curveAxis(y,head.deadzoneY,head.maxPercentY):0;
  head.filteredX=filterAxis(head.filteredX,tx,x,head.deadzoneX,head.maxPercentX);head.filteredY=filterAxis(head.filteredY,ty,y,head.deadzoneY,head.maxPercentY);
  head.outputX=head.filteredX;head.outputY=head.filteredY;
  $('#headStatus').textContent=head.calibrated?`头控 ${head.quality} · X ${head.outputX.toFixed(0)}% · Y ${head.outputY.toFixed(0)}%`:'头控：等待校准';
  sendHead(now);
}

function outputPayload(enabled=output.enabled){
  const gain=clamp(output.strength,60,300)/100;
  return{mode:output.mode,enabled,mouse_speed_x:600*gain,mouse_speed_y:450*gain,gamepad_gain:gain};
}
async function setOutput(enabled){
  try{const r=await post('/api/output/config',outputPayload(enabled));output.server=r;renderOutput(r);if(enabled){sendButtons(true);sendMotionState(true)}}catch(e){output.enabled=false;notice('输出开启失败：'+(e?.message||e));renderOutput()}
}
async function emergencyStop(show=true){
  try{output.server=await post('/api/output/stop',{})}catch{}output.enabled=false;motion.lastSent='';renderOutput(output.server);if(show)notice('所有头控、区域、动作和手柄按键已停止。')
}
function renderOutput(s=output.server){
  const on=!!(s?.enabled??output.enabled);output.enabled=on;output.mode=s?.mode||output.mode;$('#outputMode').value=output.mode;
  $('#outputPill').textContent=on?'输出开启':'输出关闭';$('#outputPill').className='pill '+(on?'ok':'bad');$('#outputBtn').textContent=on?'关闭输出 F8':'开启输出 F8';
  if(!s){$('#backendStatus').textContent='正在检查输出后端…';return}
  const pad=s.gamepad_connected?'Xbox 已连接':'Xbox 未连接';const mouse=s.mouse_available?'鼠标可用':'鼠标不可用';
  $('#backendStatus').textContent=`${mouse} · ${pad}`+(s.last_error?' · '+s.last_error:'');
}
async function refreshOutput(){
  try{const was=output.enabled,s=await api('/api/output-status');output.server=s;renderOutput(s);if(!was&&output.enabled){sendButtons(true);sendMotionState(true)}}catch{}
}
function sendHead(now){
  if(!output.enabled||output.sending||now-output.lastSend<28)return;output.lastSend=now;output.sending=true;
  const ok=head.calibrated&&head.enabled&&!head.calibrating,x=ok?clamp(head.outputX/100,-1,1):0,y=ok?clamp(head.outputY/100,-1,1):0;
  post('/api/output/frame',{x,y}).then(r=>{output.server=r;if(r.last_error)renderOutput(r)}).catch(e=>{output.enabled=false;notice('头控输出失败：'+(e?.message||e));renderOutput()}).finally(()=>output.sending=false);
}

function visualPoint(p){return{x:$('#mirrorSelect').value==='yes'?1-p.x:p.x,y:p.y}}
function visualRect(r){return $('#mirrorSelect').value==='yes'?{x1:1-r.x2,x2:1-r.x1,y1:r.y1,y2:r.y2}:r}
function pxDistance(a,b,w=video.videoWidth||canvas.width||640,h=video.videoHeight||canvas.height||480){return Math.hypot((a.x-b.x)*w,(a.y-b.y)*h)}
function rectAt(cx,cy,wpx,hpx,iw,ih){const ww=wpx/iw,hh=hpx/ih;return{x1:clamp(cx-ww/2,0,1),x2:clamp(cx+ww/2,0,1),y1:clamp(cy-hh/2,0,1),y2:clamp(cy+hh/2,0,1)}}
function smoothRect(id,next){const prev=zoneRects[id];if(!prev){zoneRects[id]=next;return next}const a=.50;const out={};for(const k of ['x1','x2','y1','y2'])out[k]=prev[k]+a*(next[k]-prev[k]);zoneRects[id]=out;return out}
function hideZone(id){delete zoneRects[id];const el=document.querySelector(`.zone[data-zone="${id}"]`);if(el)el.style.display='none'}
function setZoneElement(id,r){const el=document.querySelector(`.zone[data-zone="${id}"]`);if(!el||!r)return;const v=visualRect(r);el.style.display='grid';el.style.left=(v.x1*100)+'%';el.style.top=(v.y1*100)+'%';el.style.width=((v.x2-v.x1)*100)+'%';el.style.height=((v.y2-v.y1)*100)+'%'}
function computeBodyZones(map){
  const iw=video.videoWidth||canvas.width||640,ih=video.videoHeight||canvas.height||480;
  const ls=map?.left_shoulder,rs=map?.right_shoulder,lh=map?.left_hip,rh=map?.right_hip,nose=map?.nose;
  if(!ls||!rs||!lh||!rh||Math.min(ls.score,rs.score,lh.score,rh.score)<.4){for(const id of Object.keys(BODY_ZONES))hideZone(id);return}
  const shoulder=midpoint(ls,rs),hip=midpoint(lh,rh),L=pxDistance(shoulder,hip,iw,ih);
  if(!Number.isFinite(L)||L<35){for(const id of Object.keys(BODY_ZONES))hideZone(id);return}
  const leftDir=Math.sign(ls.x-shoulder.x)||1,rightDir=Math.sign(rs.x-shoulder.x)||-leftDir;
  let headCenter=null;
  const le=map.left_ear,re=map.right_ear;if(le&&re&&Math.min(le.score,re.score)>=.35)headCenter=midpoint(le,re);else if(nose&&nose.score>=.35)headCenter=nose;
  if(headCenter){
    const handW=.36*L,handH=.30*L,out=.82*L,upper=-.14*L,lower=.22*L;
    const defs=[
      ['leftHandUpper',leftDir,upper],['leftHandLower',leftDir,lower],
      ['rightHandUpper',rightDir,upper],['rightHandLower',rightDir,lower],
    ];
    for(const [id,dir,dy] of defs){const r=rectAt(headCenter.x+dir*out/iw,headCenter.y+dy/ih,handW,handH,iw,ih);setZoneElement(id,smoothRect(id,r))}
  } else {for(const id of ['leftHandUpper','leftHandLower','rightHandUpper','rightHandLower'])hideZone(id)}
  const la=map.left_ankle,ra=map.right_ankle;
  if(la&&ra&&Math.max(la.score,ra.score)>=.4){
    const visibleFeet=[la,ra].filter(p=>p.score>=.4);const floorY=Math.max(...visibleFeet.map(p=>p.y));
    const footW=.42*L,footH=.38*L,out=.78*L,up=.50*L;
    for(const [id,sideHip,dir] of [['leftFoot',lh,leftDir],['rightFoot',rh,rightDir]]){
      const r=rectAt(sideHip.x+dir*out/iw,floorY-up/ih,footW,footH,iw,ih);setZoneElement(id,smoothRect(id,r));
    }
  } else {hideZone('leftFoot');hideZone('rightFoot')}
}
function pointInRect(p,r){return !!p&&!!r&&p.score>=.42&&p.x>=r.x1&&p.x<=r.x2&&p.y>=r.y1&&p.y<=r.y2}
function pressedZoneIds(){return Object.entries(zoneState).filter(([,s])=>s.pressed).map(([id])=>id)}
function pressedKeys(){return [...new Set(pressedZoneIds().map(id=>BODY_ZONES[id].button))].sort()}
function updateZoneVisual(){
  const pressed=new Set(pressedZoneIds()),keys=new Set(pressedKeys());
  for(const [id,def] of Object.entries(BODY_ZONES)){const el=document.querySelector(`.zone[data-zone="${id}"]`);if(el){el.classList.toggle('active',pressed.has(id));el.textContent=def.label}}
  for(const k of ['A','B','X','Y','LB','RB']){const el=$(`#pad${k}`);if(el)el.classList.toggle('active',keys.has(k))}
  $('#buttonStatus').textContent=keys.size?'身体区域：'+[...keys].join(' + '):(currentPoseMap?'身体区域：未触发':'身体区域：等待人体');
}
function updateZones(map){
  computeBodyZones(map);let changed=false;
  for(const [id,def] of Object.entries(BODY_ZONES)){
    const r=zoneRects[id],s=zoneState[id];let inside=false;
    if(r){for(const name of def.points){if(pointInRect(map?.[name],r)){inside=true;break}}}
    if(inside){s.inside++;s.outside=0;if(!s.pressed&&s.inside>=2){s.pressed=true;changed=true}}
    else{s.outside++;s.inside=0;if(s.pressed&&s.outside>=2){s.pressed=false;changed=true}}
  }
  if(changed)updateZoneVisual();
  if(changed||(pressedKeys().length&&performance.now()-lastButtonSend>140))sendButtons(changed);
}
async function sendButtons(force=false){
  const keys=pressedKeys(),sig=keys.join(',');if(buttonSending)return;if(!force&&sig===lastSentButtons&&performance.now()-lastButtonSend<140)return;
  buttonSending=true;lastButtonSend=performance.now();
  try{const r=await post('/api/output/buttons',{buttons:output.enabled?keys:[]});lastSentButtons=output.enabled?sig:'';output.server=r;if(r.last_error)renderOutput(r)}
  catch(e){notice('身体区域输出失败：'+(e?.message||e))}finally{buttonSending=false}
}
function clearZones(){for(const s of Object.values(zoneState)){s.inside=0;s.outside=0;s.pressed=false}for(const id of Object.keys(BODY_ZONES))hideZone(id);updateZoneVisual();void sendButtons(true)}

function angleAt(a,b,c){
  if(!a||!b||!c||Math.min(a.score,b.score,c.score)<.4)return NaN;
  const w=video.videoWidth||canvas.width||640,h=video.videoHeight||canvas.height||480;
  const ux=(a.x-b.x)*w,uy=(a.y-b.y)*h,vx=(c.x-b.x)*w,vy=(c.y-b.y)*h;
  const den=Math.hypot(ux,uy)*Math.hypot(vx,vy);if(den<1e-6)return NaN;
  return Math.acos(clamp((ux*vx+uy*vy)/den,-1,1))*180/Math.PI;
}
function pointsGood(map,names,min=.42){return names.every(n=>map?.[n]&&map[n].score>=min)}
function setMotionDebounced(id,raw,onFrames=3,offFrames=4){const s=motion.debounce[id];if(raw){s.on++;s.off=0;if(!s.active&&s.on>=onFrames)s.active=true}else{s.off++;s.on=0;if(s.active&&s.off>=offFrames)s.active=false}return s.active}
function updateMotionDetection(map,now){
  if(!map){for(const s of Object.values(motion.debounce)){s.active=false;s.on=s.off=0}motion.active.clear();renderMotionStatus();sendMotionState(true);return}
  const shoulder=pointsGood(map,['left_shoulder','right_shoulder'])?midpoint(map.left_shoulder,map.right_shoulder):null;
  const hip=pointsGood(map,['left_hip','right_hip'])?midpoint(map.left_hip,map.right_hip):null;
  const T=shoulder&&hip?Math.max(.025,Math.abs(hip.y-shoulder.y)):NaN;
  let handsRaw=false,squatRaw=false,calfRaw=false,marchRaw=false;
  if(Number.isFinite(T)&&pointsGood(map,['nose','left_shoulder','right_shoulder','left_elbow','right_elbow','left_wrist','right_wrist'])){
    handsRaw=map.left_wrist.y<map.nose.y-.06*T&&map.right_wrist.y<map.nose.y-.06*T&&map.left_elbow.y<map.left_shoulder.y+.08*T&&map.right_elbow.y<map.right_shoulder.y+.08*T;
  }
  const legGood=Number.isFinite(T)&&pointsGood(map,['left_hip','right_hip','left_knee','right_knee','left_ankle','right_ankle']);
  let leftAngle=NaN,rightAngle=NaN;
  if(legGood){
    leftAngle=angleAt(map.left_hip,map.left_knee,map.left_ankle);rightAngle=angleAt(map.right_hip,map.right_knee,map.right_ankle);
    const hipKnee=((map.left_knee.y-map.left_hip.y)+(map.right_knee.y-map.right_hip.y))/2;
    squatRaw=leftAngle<135&&rightAngle<135&&hipKnee<.84*T;
    const leftCalf=leftAngle<115&&(map.left_knee.y-map.left_hip.y)>.58*T&&(map.left_ankle.y-map.left_knee.y)<.58*T;
    const rightCalf=rightAngle<115&&(map.right_knee.y-map.right_hip.y)>.58*T&&(map.right_ankle.y-map.right_knee.y)<.58*T;
    calfRaw=!squatRaw&&(leftCalf||rightCalf);
    const leftLift=(map.right_knee.y-map.left_knee.y)>.16*T&&(map.right_ankle.y-map.left_ankle.y)>.10*T;
    const rightLift=(map.left_knee.y-map.right_knee.y)>.16*T&&(map.left_ankle.y-map.right_ankle.y)>.10*T;
    const event=(side)=>{if(side!==motion.step.lastSide&&now-motion.step.lastAt>=100&&now-motion.step.lastAt<=1150)motion.step.activeUntil=now+700;motion.step.lastSide=side;motion.step.lastAt=now};
    if(leftLift&&!motion.step.leftWas)event('L');if(rightLift&&!motion.step.rightWas)event('R');motion.step.leftWas=leftLift;motion.step.rightWas=rightLift;
    if(now-motion.step.lastAt>1200){motion.step.lastSide='';motion.step.activeUntil=0}
    marchRaw=!squatRaw&&!calfRaw&&now<motion.step.activeUntil;
  } else {motion.step.leftWas=motion.step.rightWas=false;motion.step.activeUntil=0}
  const active=new Set();
  if(setMotionDebounced('march',marchRaw,1,2))active.add('march');
  if(setMotionDebounced('calf_back',calfRaw,3,4))active.add('calf_back');
  if(setMotionDebounced('squat',squatRaw,3,4))active.add('squat');
  if(setMotionDebounced('hands_up',handsRaw,3,4))active.add('hands_up');
  const sig=[...active].sort().join(',');const changed=sig!==[...motion.active].sort().join(',');motion.active=active;renderMotionStatus();if(changed||active.size&&now-motion.lastSend>150)sendMotionState(changed);
}
function renderMotionStatus(){
  const mapIds={march:['#motionMarch','踏步'],calf_back:['#motionCalf','小腿向后'],squat:['#motionSquat','下蹲'],hands_up:['#motionHands','双手过头']};
  for(const [id,[sel]] of Object.entries(mapIds))$(sel)?.classList.toggle('active',motion.active.has(id));
  const names=[...motion.active].map(id=>mapIds[id]?.[1]||id);$('#motionStatus').textContent=names.length?'动作：'+names.join(' + '):'动作：未触发';
}
async function sendMotionState(force=false){
  const active=[...motion.active].sort(),sig=active.join(',');if(motion.sending)return;if(!force&&sig===motion.lastSent&&performance.now()-motion.lastSend<150)return;
  motion.sending=true;motion.lastSend=performance.now();try{await post('/api/motion/state',{active:output.enabled?active:[]});motion.lastSent=output.enabled?sig:''}catch(e){notice('动作输出失败：'+(e?.message||e))}finally{motion.sending=false}
}
function motionTargetPlaceholder(type){if(type==='gamepad_axis')return'LS_UP / LS_DOWN / LS_LEFT / LS_RIGHT';if(type==='gamepad')return'A / B / X / Y / LB / RB';return'W / SPACE / CTRL+W'}
function renderMotionRows(items){
  motion.config=items||[];const box=$('#motionRows');box.replaceChildren();
  for(const m of motion.config){const row=document.createElement('div');row.className='motion-row';row.dataset.id=m.id;
    const enabled=document.createElement('input');enabled.type='checkbox';enabled.className='motion-enabled';enabled.checked=!!m.enabled;
    const name=document.createElement('div');name.className='motion-name';name.textContent=m.name;
    const type=document.createElement('select');type.className='motion-type';for(const [v,t] of [['keyboard','键盘'],['gamepad','Xbox 按键'],['gamepad_axis','Xbox 左摇杆']]){const o=document.createElement('option');o.value=v;o.textContent=t;type.appendChild(o)}type.value=m.type;
    const target=document.createElement('input');target.type='text';target.className='motion-target';target.value=m.target||'';target.placeholder=motionTargetPlaceholder(type.value);type.addEventListener('change',()=>target.placeholder=motionTargetPlaceholder(type.value));
    row.append(enabled,name,type,target);box.appendChild(row)}
}
function readMotionRows(){const items=[];for(const row of document.querySelectorAll('.motion-row')){const old=motion.config.find(x=>x.id===row.dataset.id);const enabled=row.querySelector('.motion-enabled').checked,type=row.querySelector('.motion-type').value,target=row.querySelector('.motion-target').value.trim().toUpperCase();if(enabled&&!target)throw new Error(`${old?.name||row.dataset.id} 已启用但没有设置输出`);items.push({id:row.dataset.id,name:old?.name||'',enabled,type,target})}return items}
async function refreshMotionConfig(){try{const s=await api('/api/motion/config');renderMotionRows(s.motions||[])}catch(e){notice('动作设置读取失败：'+e.message)}}
async function saveMotionConfig(){const motions=readMotionRows(),s=await post('/api/motion/config',{motions});renderMotionRows(s.motions||motions);await sendMotionState(true);return s}

function overlayActive(){return !!(overlay.win&&!overlay.win.closed&&overlay.canvas&&overlay.ctx)}
function syncOverlayButton(){const b=$('#overlayBtn');if(b)b.textContent=overlayActive()?'关闭悬浮':'悬浮窗'}
function scheduleLoop(){
  try{if(overlayActive()){overlay.win.requestAnimationFrame(loop);return}}catch{}
  requestAnimationFrame(loop);
}
function renderOverlay(map=currentPoseMap){
  if(!overlayActive())return;
  const c=overlay.canvas,octx=overlay.ctx,w=c.width,h=c.height,mirror=$('#mirrorSelect').value==='yes';
  octx.save();octx.setTransform(1,0,0,1,0,0);octx.clearRect(0,0,w,h);octx.fillStyle='#050608';octx.fillRect(0,0,w,h);
  if(video.readyState>=2){
    if(mirror){octx.translate(w,0);octx.scale(-1,1)}
    try{octx.drawImage(video,0,0,w,h)}catch{}
    octx.restore();octx.save();
  }
  if(map){
    octx.strokeStyle='rgba(80,220,255,.92)';octx.lineWidth=Math.max(2,w/260);octx.fillStyle='rgba(255,255,255,.96)';
    for(const [a,b] of EDGES){const p=map[a],q=map[b];if(!p||!q||p.score<.3||q.score<.3)continue;const vp=visualPoint(p),vq=visualPoint(q);octx.beginPath();octx.moveTo(vp.x*w,vp.y*h);octx.lineTo(vq.x*w,vq.y*h);octx.stroke()}
    for(const p of Object.values(map)){if(p.score<.3)continue;const vp=visualPoint(p);octx.beginPath();octx.arc(vp.x*w,vp.y*h,Math.max(2.2,w/190),0,Math.PI*2);octx.fill()}
  }
  const pressedIds=new Set(pressedZoneIds()),pressed=new Set(pressedKeys());
  octx.textAlign='center';octx.textBaseline='middle';octx.font=`700 ${Math.max(15,Math.round(w/22))}px system-ui,sans-serif`;
  for(const [id,def] of Object.entries(BODY_ZONES)){const zr=zoneRects[id];if(!zr)continue;const z=visualRect(zr),x=z.x1*w,y=z.y1*h,zw=(z.x2-z.x1)*w,zh=(z.y2-z.y1)*h,active=pressedIds.has(id);octx.fillStyle=active?'rgba(245,64,76,.34)':'rgba(0,0,0,.16)';octx.strokeStyle=active?'#ff5965':'rgba(255,255,255,.65)';octx.lineWidth=Math.max(2,w/210);octx.setLineDash(def.kind==='foot'?[7,5]:[]);octx.fillRect(x,y,zw,zh);octx.strokeRect(x,y,zw,zh);octx.setLineDash([]);octx.fillStyle='#fff';octx.fillText(def.label,x+zw/2,y+zh/2)}
  for(const name of ['left_wrist','right_wrist','left_ankle','right_ankle']){const p=map?.[name];if(!p||p.score<.45)continue;const vp=visualPoint(p);octx.beginPath();octx.arc(vp.x*w,vp.y*h,Math.max(6,w/55),0,Math.PI*2);octx.fillStyle=pressed.size?'rgba(255,65,78,.92)':'rgba(55,230,150,.92)';octx.fill();octx.strokeStyle='#fff';octx.lineWidth=2;octx.stroke()}
  const actions=[...motion.active].map(id=>({march:'踏步',calf_back:'小腿向后',squat:'下蹲',hands_up:'双手过头'}[id]||id));
  const status=pressed.size?`区域 ${[...pressed].join('+')}`:(actions.length?`动作 ${actions.join('+')}`:(map?'未触发':'未识别人体'));
  octx.font=`600 ${Math.max(12,Math.round(w/32))}px system-ui,sans-serif`;octx.textAlign='left';octx.textBaseline='alphabetic';
  const text=`${output.enabled?'输出开':'输出关'} · ${status}`;const pad=Math.max(7,w/70),barH=Math.max(25,h/10);
  octx.fillStyle='rgba(0,0,0,.62)';octx.fillRect(0,h-barH,w,barH);octx.fillStyle='#fff';octx.fillText(text,pad,h-pad);
  octx.restore();
}
async function toggleOverlay(){
  if(overlayActive()){try{overlay.win.close()}catch{}overlay.win=overlay.canvas=overlay.ctx=null;syncOverlayButton();return}
  if(!window.documentPictureInPicture?.requestWindow){notice('当前浏览器不支持置顶游戏悬浮窗。请用新版 Edge / Chrome。');return}
  try{
    const pip=await window.documentPictureInPicture.requestWindow({width:420,height:315});
    pip.document.title='MotionControl';pip.document.body.style.cssText='margin:0;overflow:hidden;background:#050608;width:100vw;height:100vh';
    const c=pip.document.createElement('canvas');c.width=640;c.height=480;c.style.cssText='display:block;width:100vw;height:100vh;object-fit:contain;background:#050608';pip.document.body.appendChild(c);
    overlay.win=pip;overlay.canvas=c;overlay.ctx=c.getContext('2d');
    pip.addEventListener('pagehide',()=>{overlay.win=overlay.canvas=overlay.ctx=null;syncOverlayButton()},{once:true});
    syncOverlayButton();renderOverlay(currentPoseMap);
    if(!cameraRunning)await startCamera();
  }catch(e){overlay.win=overlay.canvas=overlay.ctx=null;syncOverlayButton();notice('悬浮窗启动失败：'+(e?.message||e))}
}

async function loadModel(){
  const mp=await import(MP_TASKS),vision=await mp.FilesetResolver.forVisionTasks(MP_WASM);
  const opts=url=>({baseOptions:{modelAssetPath:url,delegate:'GPU'},runningMode:'VIDEO',numPoses:1,minPoseDetectionConfidence:.35,minPosePresenceConfidence:.35,minTrackingConfidence:.35});
  try{return await mp.PoseLandmarker.createFromOptions(vision,opts('/api/model/mp-full'))}
  catch(e){
    const msg=String(e?.message||e);if(!/NormalizationOptions|image_preprocessing_graph|StartGraph failed/i.test(msg))throw e;
    const vision2=await mp.FilesetResolver.forVisionTasks(MP_WASM);notice('本地 Full task 元数据不兼容，已临时使用 Google 官方 Full task。');return mp.PoseLandmarker.createFromOptions(vision2,opts(MP_OFFICIAL_FULL));
  }
}
function normMp(r){const lm=r.landmarks?.[0];if(!lm)return null;const map={};lm.forEach((p,i)=>map[MP_NAMES[i]]={x:p.x,y:p.y,z:p.z??0,score:p.visibility??p.presence??1});return map}
function draw(map){ctx.clearRect(0,0,canvas.width,canvas.height);if(!map)return;ctx.strokeStyle='#55ddff';ctx.fillStyle='#fff';ctx.lineWidth=3;
  for(const[a,b]of EDGES){const p=map[a],q=map[b];if(!p||!q||p.score<.3||q.score<.3)continue;ctx.beginPath();ctx.moveTo(p.x*canvas.width,p.y*canvas.height);ctx.lineTo(q.x*canvas.width,q.y*canvas.height);ctx.stroke()}
  for(const p of Object.values(map)){if(p.score<.3)continue;ctx.beginPath();ctx.arc(p.x*canvas.width,p.y*canvas.height,3,0,Math.PI*2);ctx.fill()}
}
async function startCamera(){
  if(cameraRunning)return;try{
    if(!modelAvailable)throw new Error('I 盘未找到 pose_landmarker_full.task');switching=true;detector=await loadModel();switching=false;
    stream=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:640},height:{ideal:480},frameRate:{ideal:30}},audio:false});video.srcObject=stream;await video.play();canvas.width=video.videoWidth||640;canvas.height=video.videoHeight||480;
    cameraRunning=true;$('#cameraPill').textContent='摄像头运行';$('#cameraPill').className='pill ok';$('#cameraBtn').textContent='关闭摄像头';$('#hint').style.display='none';scheduleLoop();
  }catch(e){switching=false;notice('启动失败：'+(e?.message||e))}
}
function stopCamera(){
  cameraRunning=false;currentPoseMap=null;if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}video.srcObject=null;try{detector?.close?.()}catch{}detector=null;ctx.clearRect(0,0,canvas.width,canvas.height);clearZones();updateMotionDetection(null,performance.now());renderOverlay(null);
  $('#cameraPill').textContent='摄像头关闭';$('#cameraPill').className='pill bad';$('#posePill').textContent='未识别人体';$('#posePill').className='pill bad';$('#cameraBtn').textContent='启动摄像头';$('#hint').style.display='block';
}
async function loop(){
  if(!cameraRunning)return;if(video.readyState>=2&&video.currentTime!==lastVideoTime&&!switching){lastVideoTime=video.currentTime;const now=performance.now();try{
    const result=detector.detectForVideo(video,now),map=normMp(result);currentPoseMap=map;draw(map);updateZones(map);updateMotionDetection(map,now);if(map)updateHead(map,now);else{head.filteredX=head.filteredY=head.outputX=head.outputY=0;sendHead(now)};renderOverlay(map);
    $('#posePill').textContent=map?'人体已识别':'未识别人体';$('#posePill').className='pill '+(map?'ok':'bad');
  }catch(e){notice('推理失败：'+(e?.message||e))}}
  scheduleLoop();
}


function addVoiceRow(mapping={phrase:'',type:'keyboard',target:''}){
  const row=document.createElement('div');row.className='voice-row';
  const phrase=document.createElement('input');phrase.className='voice-phrase';phrase.placeholder='说：例如 地图';phrase.value=mapping.phrase||'';
  const type=document.createElement('select');type.className='voice-type';
  for(const [value,label] of [['keyboard','键盘/组合键'],['gamepad','Xbox 键']]){const o=document.createElement('option');o.value=value;o.textContent=label;type.appendChild(o)}
  type.value=mapping.type||'keyboard';
  const target=document.createElement('input');target.className='voice-target';target.value=mapping.target||'';
  const syncPlaceholder=()=>target.placeholder=type.value==='gamepad'?'A / B / X / Y / LB / RB':'M / CTRL+S / TAB';syncPlaceholder();type.addEventListener('change',syncPlaceholder);
  const remove=document.createElement('button');remove.type='button';remove.className='btn voice-remove';remove.textContent='删';remove.addEventListener('click',()=>{row.remove();if(!$('#voiceRows').children.length)addVoiceRow()});
  row.append(phrase,type,target,remove);$('#voiceRows').appendChild(row);
}
function readVoiceMappings(){
  const rows=[...document.querySelectorAll('.voice-row')],items=[];
  for(const row of rows){const phrase=row.querySelector('.voice-phrase').value.trim(),type=row.querySelector('.voice-type').value,target=row.querySelector('.voice-target').value.trim();
    if(!phrase&&!target)continue;if(!phrase||!target)throw new Error('语音命令必须同时填写“说什么”和“输出什么”');items.push({phrase,type,target});}
  return items;
}
function renderVoiceRows(items){$('#voiceRows').replaceChildren();for(const m of items||[])addVoiceRow(m);if(!$('#voiceRows').children.length)addVoiceRow()}
function renderVoiceStatus(s=voice.status){
  if(!s)return;voice.status=s;const has=Number(s.supported_count||0)>0;
  $('#voiceMode').textContent=has?(s.recognizer_mode==='grammar'?`有限词表 ${s.supported_count}`:`开放识别 ${s.supported_count}`):'未就绪';
  $('#voiceMode').className='pill '+(has?'ok':'warn');
  $('#voicePill').textContent=voice.running?(s.audio_alive?'语音运行':'麦克风已开'):'语音关闭';$('#voicePill').className='pill '+(voice.running?'ok':'bad');
  $('#voiceBtn').textContent=voice.running?'关闭麦克风':'开启麦克风';
  const modelPath=s.model_path||'F:\\switch\\motionbridge\\models\\vosk-model-small-cn-0.22';const mp=$('#voiceModelPath');if(mp){mp.textContent='模型：'+modelPath;mp.title=modelPath}
  const parts=[];
  if(s.last_command)parts.push(`已识别“${s.last_command}” → ${String(s.last_action||'').replace('keyboard:','').replace('gamepad:','Xbox ')}`);
  else if(s.last_partial)parts.push(`听到：${s.last_partial}`);else if(s.last_final)parts.push(`识别：${s.last_final}`);
  if(s.unsupported?.length)parts.push(`模型不支持：${s.unsupported.join('、')}`);
  if(s.last_error)parts.push(s.last_error);
  if(!parts.length)parts.push(s.model_path?'词表已就绪，开启麦克风后直接说命令。':'未找到 Vosk 中文模型。');
  $('#voiceStatus').textContent=parts.join(' · ');
}
async function saveVoiceMappings(){
  const mappings=readVoiceMappings();const s=await post('/api/voice/config',{mappings});voice.status=s;renderVoiceStatus(s);return s;
}
async function refreshVoice(){try{const s=await api('/api/voice/status');voice.status=s;renderVoiceStatus(s)}catch{}}
function downsamplePcm16(input,inputRate){
  const rate=16000;if(inputRate<=rate){const out=new Int16Array(input.length);for(let i=0;i<input.length;i++)out[i]=Math.max(-32768,Math.min(32767,Math.round(input[i]*32767)));return out}
  const ratio=inputRate/rate,length=Math.max(1,Math.floor(input.length/ratio)),out=new Int16Array(length);
  for(let i=0;i<length;i++){const a=Math.floor(i*ratio),b=Math.max(a+1,Math.min(input.length,Math.floor((i+1)*ratio)));let sum=0;for(let j=a;j<b;j++)sum+=input[j];const v=sum/(b-a);out[i]=Math.max(-32768,Math.min(32767,Math.round(v*32767)))}return out;
}
async function flushVoiceAudio(){
  if(voice.sending||!voice.running||!voice.chunks.length)return;voice.sending=true;const chunks=voice.chunks.splice(0);voice.pendingBytes=0;const size=chunks.reduce((n,c)=>n+c.byteLength,0),merged=new Uint8Array(size);let off=0;for(const c of chunks){merged.set(c,off);off+=c.byteLength}
  try{const s=await postBinary('/api/voice/audio',merged);voice.status=s;renderVoiceStatus(s)}catch(e){$('#voiceStatus').textContent='语音输入失败：'+(e?.message||e)}finally{voice.sending=false;if(voice.pendingBytes>=7000)void flushVoiceAudio()}
}
async function startVoice(){
  try{
    const s=await saveVoiceMappings();if(!s.mappings?.length)throw new Error('先添加至少一个语音命令');if(!s.available)throw new Error(s.last_error||'Vosk 语音识别未就绪');
    const ms=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
    const AC=window.AudioContext||window.webkitAudioContext,ac=new AC();await ac.resume();const source=ac.createMediaStreamSource(ms),processor=ac.createScriptProcessor(4096,1,1),gain=ac.createGain();gain.gain.value=0;
    source.connect(processor);processor.connect(gain);gain.connect(ac.destination);voice.stream=ms;voice.ctx=ac;voice.source=source;voice.processor=processor;voice.gain=gain;voice.running=true;voice.chunks=[];voice.pendingBytes=0;
    processor.onaudioprocess=e=>{if(!voice.running)return;const pcm=downsamplePcm16(e.inputBuffer.getChannelData(0),ac.sampleRate),bytes=new Uint8Array(pcm.buffer);voice.chunks.push(bytes);voice.pendingBytes+=bytes.byteLength;if(voice.pendingBytes>=7000)void flushVoiceAudio()};renderVoiceStatus(s);
  }catch(e){stopVoice();notice('语音启动失败：'+(e?.message||e))}
}
function stopVoice(){
  voice.running=false;voice.chunks=[];voice.pendingBytes=0;try{voice.processor&&(voice.processor.onaudioprocess=null,voice.processor.disconnect())}catch{}try{voice.source?.disconnect()}catch{}try{voice.gain?.disconnect()}catch{}if(voice.stream)voice.stream.getTracks().forEach(t=>t.stop());try{voice.ctx?.close()}catch{}voice.stream=voice.ctx=voice.source=voice.processor=voice.gain=null;renderVoiceStatus(voice.status||{});
}

function syncControlLabels(){
  head.deadzoneX=Number($('#deadX').value)/100;head.deadzoneY=Number($('#deadY').value)/100;head.gamma=Number($('#gamma').value);head.maxPercentX=Number($('#speedX').value);head.maxPercentY=Number($('#speedY').value);
  $('#deadXValue').textContent=Math.round(head.deadzoneX*100)+'%';$('#deadYValue').textContent=Math.round(head.deadzoneY*100)+'%';$('#gammaValue').textContent=head.gamma.toFixed(1);$('#speedXValue').textContent=head.maxPercentX+'%';$('#speedYValue').textContent=head.maxPercentY+'%';
  output.strength=Number($('#strength').value);$('#strengthValue').textContent=output.strength+'%';
}
async function init(){
  try{const d=await api('/api/models');modelAvailable=!!d.models?.[0]?.available;if(!modelAvailable)notice('未找到 MediaPipe Pose Full：'+(d.model_root||'I:\\MotionControl-Pose-Models\\models'))}catch(e){notice('服务器连接失败：'+e.message)}
  syncControlLabels();updateZoneVisual();renderMotionStatus();await refreshOutput();await refreshVoice();await refreshMotionConfig();renderVoiceRows(voice.status?.mappings||[]);setInterval(refreshOutput,700);setInterval(refreshVoice,900);
}

$('#cameraBtn').addEventListener('click',()=>cameraRunning?stopCamera():startCamera());
$('#overlayBtn').addEventListener('click',toggleOverlay);
$('#outputBtn').addEventListener('click',()=>setOutput(!output.enabled));
$('#stopBtn').addEventListener('click',()=>emergencyStop(true));
$('#calBtn').addEventListener('click',startCalibration);$('#centerBtn').addEventListener('click',setCurrentCenter);
$('#mirrorSelect').addEventListener('change',()=>{viewer.classList.toggle('mirror',$('#mirrorSelect').value==='yes');clearZones();renderOverlay(currentPoseMap)});
$('#outputMode').addEventListener('change',async()=>{output.mode=$('#outputMode').value;try{const r=await post('/api/output/config',outputPayload(output.enabled));output.server=r;renderOutput(r);sendButtons(true);sendMotionState(true)}catch(e){notice('输出模式切换失败：'+(e?.message||e))}});
$('#strength').addEventListener('input',syncControlLabels);$('#strength').addEventListener('change',()=>post('/api/output/config',outputPayload(output.enabled)).then(r=>{output.server=r;renderOutput(r)}).catch(e=>notice(e.message)));
for(const id of ['deadX','deadY','gamma','speedX','speedY'])$('#'+id).addEventListener('input',syncControlLabels);
$('#headEnable').addEventListener('change',()=>{head.enabled=$('#headEnable').checked;if(!head.enabled){head.filteredX=head.filteredY=head.outputX=head.outputY=0}});
$('#invertX').addEventListener('change',()=>head.invertX=$('#invertX').checked);$('#invertY').addEventListener('change',()=>head.invertY=$('#invertY').checked);
$('#addVoiceBtn').addEventListener('click',()=>addVoiceRow());
$('#saveVoiceBtn').addEventListener('click',()=>saveVoiceMappings().then(()=>notice('语音词表已保存。')).catch(e=>notice('保存失败：'+(e?.message||e))));
$('#voiceBtn').addEventListener('click',()=>voice.running?stopVoice():startVoice());
$('#settingsBtn').addEventListener('click',()=>{$('#settingsMask').classList.add('open');$('#settingsMask').setAttribute('aria-hidden','false')});
$('#closeSettingsBtn').addEventListener('click',()=>{$('#settingsMask').classList.remove('open');$('#settingsMask').setAttribute('aria-hidden','true')});
$('#settingsMask').addEventListener('click',e=>{if(e.target===$('#settingsMask'))$('#closeSettingsBtn').click()});
$('#saveMotionBtn').addEventListener('click',()=>saveMotionConfig().then(()=>notice('四个动作映射已保存。')).catch(e=>notice('动作设置保存失败：'+(e?.message||e))));
window.addEventListener('beforeunload',()=>{try{navigator.sendBeacon('/api/output/stop',new Blob(['{}'],{type:'application/json'}))}catch{}if(stream)stream.getTracks().forEach(t=>t.stop());try{overlay.win?.close()}catch{}stopVoice()});

init();
