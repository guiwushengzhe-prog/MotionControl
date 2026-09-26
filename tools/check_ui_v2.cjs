// Offline browser checks: every service endpoint is simulated; no camera or output is opened.
//
// Windows 上用本机的 Edge；别的系统（或者想换浏览器）设 PW_CHROMIUM_PATH 指向一个
// Chromium，例如：
//   PW_CHROMIUM_PATH=/opt/pw-browsers/chromium-1194/chrome-linux/chrome \
//   NODE_PATH=/opt/node22/lib/node_modules node tools/check_ui_v2.cjs
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, 'output', 'playwright', 'v2');
fs.mkdirSync(artifacts, { recursive: true });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const zones = Object.fromEntries(['leftHand','rightHand','leftFoot','rightFoot','headJump','lookGate'].map((id,i)=>{const cx=.2+(i%3)*.25,cy=.25+Math.floor(i/3)*.35;return [id,{x1:cx-.07,x2:cx+.07,y1:cy-.07,y2:cy+.07}]}));
const state = {
  selected:'game-a',saved:{},output:{enabled:false,mode:'mouse',mouse_available:true,mouse_speed_x:960,gamepad_gain:1.6},
  camera:false,source:'computer',phone:false,offline:false,failSource:true,failSave:false,failStop:false,failStart:false,
  saveDelay:0,saving:0,maxSaving:0,saveCalls:[],selectCalls:[],performanceCalls:0,failHead:false,
  zones:{rects:zones,frozen:false},
  head:{algorithm:'pnp',horizontal_algorithm:'classic',enabled:true,calibrated:true,sensitivity_x:58,sensitivity_y:46,deadzone:.1},
  // 智能判定和「录我的动作」：框现在的判断状态、疑似误按提示、录到哪了、体检报告。
  zonePhase:{},misfire:null,intentCalls:[],
  intent:{active:false,state:'idle',steps:[],saving:false,error:''},
};
const intentItems={all:[{key:'idle',kind:'idle',name:'随便动动'},{key:'press:leftHand',kind:'press',name:'左手框'},
  {key:'tap:leftHand',kind:'tap',name:'左手框（快速）'},{key:'action:motion.hands_up',kind:'action',name:'双手举过头'},
  {key:'action:motion.jumping_jack',kind:'action',name:'开合跳'}],
  recorded:['idle','press:leftHand','tap:leftHand'],missing:['action:motion.hands_up','action:motion.jumping_jack']};
const report={actions:[{trigger:'motion.hands_up',name:'双手举过头',reps:5,misfires:{}}],
  zones:[{zone:'leftHand',kind:'press',attempts:5,pressed:5,missed:0,p50_ms:0,p95_ms:33}],idle:{},
  summary:{misfire_rate:0,miss_rate:0,p50_ms:0,p95_ms:33,action_reps:5,press_attempts:5}};
// 动作库：程序自带两个，另外两个当作已经从官方动作库下载了。只给界面用得到的字段。
const libraryItem=(id,name,group,source)=>({id,name,group,source,trigger:`${group==='pose'?'pose':'motion'}.${id}`,
  how:'',revision:1,ratings:{intensity:2,recognition:4,difficulty:2},body_parts:{legs:3},demo:{frame_s:.5,frames:[]}});
const poseLibrary=[libraryItem('march','原地踏步','motion','builtin'),libraryItem('calf_back','小腿向后抬起','motion','builtin'),
  libraryItem('jumping_jack','开合跳','motion','cloud'),libraryItem('hands_up','双手举过头','motion','cloud')];
const commands=[
  {id:'output.stop',phrase:'体感停止',label:'停止控制',system_fixed:true,kind:'system'},
  {id:'game.map',phrase:'打开地图',label:'地图',kind:'game',default_action:{type:'keyboard',target:'M'},effective_action:{type:'keyboard',target:'M'}},
];
function profile(){
  const overrides=state.saved[state.selected]||{};
  const bindings={zones:{leftHand:{action:{type:'gamepad',target:'X',behavior:'hold'}}},motions:{},poses:{},voice:{}};
  for(const [key,value] of Object.entries(overrides)){
    const [prefix,...parts]=key.split('.');
    bindings[{zone:'zones',motion:'motions',pose:'poses',voice:'voice'}[prefix]][parts.join('.')]=value||{disabled:true};
  }
  return {id:state.selected,selected_id:state.selected,name:state.selected==='game-a'?'示例游戏甲':'示例游戏乙',bindings,overrides};
}
function runtime(){
  return {body_mode:state.source,camera:{running:state.camera},kernel:{width:640,height:480,
    pose:state.camera?{nose:{x:.5,y:.25,score:1},left_shoulder:{x:.35,y:.4,score:1},right_shoulder:{x:.65,y:.4,score:1}}:null,
    zones:Object.fromEntries(Object.entries(state.zones.rects).map(([id,rect])=>[id,{rect,...(state.zonePhase[id]||{})}])),
    zones_frozen:state.zones.frozen,head:state.head,zone_trigger_mode:'smart',
    zone_overlaps:{leftHand:{triggers:['motion.hands_up'],rates:{'motion.hands_up':{hits:4,reps:5,source:'recorded'}},yields:true,with_motion:false}},
    zone_misfire_hint:state.misfire,intent_recording:state.intent,intent_items:intentItems,
    zone_learning:{state:'ready',error:'',report,took_s:1.2,snippets:40}}};
}
(async()=>{
  const executablePath=process.env.PW_CHROMIUM_PATH;
  const browser=await chromium.launch(executablePath?{executablePath,headless:true}:{channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1366,height:768}});
  // 新手教程第一次打开会盖一层遮罩，挡住所有点击。这里测的不是教程，当作看过了。
  await page.addInitScript(()=>localStorage.setItem('motioncontrol_tutorial_v2',
    JSON.stringify({done:[],skipped:[],seen:1,offered:true})));
  // 键盘键位框是只读的按键录入框（app.js 的 makeKeyCaptureInput）：点进去，再按键。
  const pressKeys=async(input,combo)=>{await input.click();await page.keyboard.press(combo)};
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.route('http://motioncontrol.test/**',async route=>{
    const request=route.request(),url=new URL(request.url()),endpoint=url.pathname;
    if(!endpoint.startsWith('/api/')){
      const file=path.join(root,'web',endpoint==='/'?'index.html':endpoint.slice(1));
      return route.fulfill({path:file});
    }
    if(state.offline)return route.abort();
    const body=request.postDataJSON()||{};
    const respond=data=>route.fulfill({json:data});
    const fail=message=>route.fulfill({status:400,json:{ok:false,error:message}});
    switch(endpoint){
      case '/api/kernel/status':return respond(runtime());
      case '/api/models':return respond({models:[{available:false}]});
      case '/api/input/status':return respond({mobile_pose_connected:state.phone,phone_ws_urls:['ws://192.0.2.1:8765/ws/input']});
      case '/api/input/source':
        if(state.failSource)return fail('模拟摄像头启动失败');
        state.camera=body.enabled&&body.source==='computer';state.source=body.source;return respond(runtime());
      case '/api/output-status':return respond(state.output);
      case '/api/output/config':
        if(body.enabled&&state.failStart)return fail('模拟输出开启失败');
        Object.assign(state.output,body);return respond(state.output);
      case '/api/output/stop':
        if(state.failStop)return fail('模拟停止无响应');
        state.output.enabled=false;return respond(state.output);
      case '/api/output/xinput':return respond({enabled:false,connected_users:[]});
      case '/api/output/actions':return respond({actions:{keyboard:{free_text:true},gamepad:{targets:['A','B','X','Y','LB']},mouse_button:{targets:['LEFT','RIGHT']}}});
      case '/api/pose/library':return respond({library:poseLibrary,rating_names:{intensity:'运动强度',recognition:'识别度',difficulty:'上手难度'},body_part_names:{legs:'腿部'},cloud_names:{}});
      case '/api/voice/status':return respond({connected:false,model_ready:false,mappings:[]});
      case '/api/voice/commands':return respond({commands});
      case '/api/voice/config':return respond({mappings:body.mappings});
      case '/api/camera/config':return respond({preference:'auto'});
      case '/api/camera/preview.jpg':return route.fulfill({status:204,body:''});
      case '/api/game-profiles/catalog':return respond({games:[{id:'game-a',name:'示例游戏甲'},{id:'game-b',name:'示例游戏乙'}],count:2});
      case '/api/game-profiles/selected':return respond({profile:profile()});
      case '/api/game-profiles/select':state.selectCalls.push(body.id);state.selected=body.id;return respond({profile:profile()});
      case '/api/game-profiles/overrides':
        state.saving++;state.maxSaving=Math.max(state.maxSaving,state.saving);
        await delay(state.saveDelay);state.saving--;
        state.saveCalls.push(body);
        if(state.failSave)return fail('模拟磁盘写入失败');
        if(body.profile_id!==state.selected)return route.fulfill({status:409,json:{ok:false,error:'目标游戏不匹配'}});
        state.saved[body.profile_id]=body.overrides;return respond({profile:profile()});
      case '/api/zones/freeze':state.zones.frozen=body.frozen!==false;return respond(runtime());
      case '/api/zones/frozen':state.zones.rects={...state.zones.rects,...body.rects};return respond(runtime());
      case '/api/zones/move-here':state.zones.frozen=true;return respond(runtime());
      case '/api/intent/start':state.intentCalls.push(['start',body.keys||null]);
        state.intent={active:true,state:'preparing',remaining_s:3,index:0,phase:'ready',steps:[],saving:false,error:''};return respond(runtime());
      case '/api/intent/cancel':state.intentCalls.push(['cancel']);state.intent={...state.intent,active:false,state:'cancelled'};return respond(runtime());
      case '/api/head/config':
        if(state.failHead)return fail('模拟头控设置失败');
        Object.assign(state.head,body);return respond(runtime());
      case '/api/head/calibration/start':state.head.calibrating=true;return respond(runtime());
      case '/api/head/calibration/cancel':state.head.calibrating=false;return respond(runtime());
      case '/api/performance':state.performanceCalls++;return respond({capture_fps:30});
      default:return fail('未模拟接口：'+endpoint);
    }
  });
  try{
    await page.goto('http://motioncontrol.test/');
    await page.getByText('示例游戏甲',{exact:true}).first().waitFor();
    await page.locator('#mainActionBtn').click();
    await page.getByText('模拟摄像头启动失败',{exact:true}).waitFor();
    assert.equal(state.output.enabled,false);
    state.failSource=false;
    await page.locator('#mainActionBtn').click();
    await page.waitForFunction(()=>document.querySelector('#mainActionBtn').textContent==='开始游戏控制');
    await page.locator('#mainActionBtn').click();
    await page.waitForFunction(()=>document.querySelector('#mainActionBtn').textContent==='暂停游戏控制');
    state.failStop=true;await page.locator('#stopBtn').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('尚未确认停止'));
    state.failStop=false;await page.locator('#stopBtn').click();
    await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('已紧急停止'));
    state.failStart=true;await page.locator('#mainActionBtn').click();
    await page.getByText('模拟输出开启失败',{exact:true}).waitFor();state.failStart=false;

    await page.locator('[data-view=games]').click();
    // 键位格自己也带 data-trigger（停住语音按住要知道是哪一行），只认整行。
    const row=page.locator('.binding-row[data-trigger="zone.leftHand"]');
    await row.locator('.binding-type').selectOption('keyboard');
    state.failSave=true;await pressKeys(row.locator('.binding-target'),'Control+KeyM');
    await page.locator('#retryProfileSaveBtn').waitFor({state:'visible'});
    await page.locator('#profileSelect').selectOption('game-b');await page.locator('#profileApplyBtn').click();
    await page.waitForFunction(()=>!document.querySelector('#mappingFields').disabled);
    assert.equal(state.selected,'game-a');assert.equal(await row.locator('.binding-target').inputValue(),'CTRL+M');
    state.failSave=false;await page.locator('#retryProfileSaveBtn').click();
    await page.waitForFunction(()=>document.querySelector('#profileSaveStatus').textContent==='已自动保存');
    state.saveDelay=600;
    await pressKeys(row.locator('.binding-target'),'Control+KeyK');await delay(400);
    await pressKeys(row.locator('.binding-target'),'Control+KeyL');
    await page.locator('#profileApplyBtn').click();
    await page.waitForFunction(()=>document.querySelector('#currentGameName').textContent==='示例游戏乙');
    assert.equal(state.saved['game-a']['zone.leftHand'].action.target,'CTRL+L');
    assert.equal(state.maxSaving,1);
    await page.locator('#profileSelect').selectOption('game-a');await page.locator('#profileApplyBtn').click();
    await page.waitForFunction(()=>document.querySelector('#currentGameName').textContent==='示例游戏甲');
    assert.equal(await row.locator('.binding-target').inputValue(),'CTRL+L');
    await page.reload();await row.waitFor({state:'attached'});
    await page.locator('[data-view=games]').click();assert.equal(await row.locator('.binding-target').inputValue(),'CTRL+L');
    // 下载来的动作默认不在映射表里：在动作库卡片上点「加到映射」才进来。
    for(const id of ['jumping_jack','hands_up'])await page.locator(`.pose-library-item[data-id="${id}"] .custom-pose-key`).click();
    const jumping=page.locator('.binding-row[data-trigger="motion.jumping_jack"] .binding-type');
    await jumping.selectOption('gamepad');
    assert.equal(await page.locator('.binding-row[data-trigger="motion.hands_up"] .binding-type').isDisabled(),true);
    await page.locator('#profileSaveStatus').getByText('已自动保存').waitFor();
    // A second client changed games: retain the draft, then explicitly restore its target.
    state.selected='game-b';
    await pressKeys(row.locator('.binding-target'),'Control+KeyP');
    await page.getByRole('button',{name:'重新选择本游戏并保存草稿'}).waitFor();
    assert.equal(state.selected,'game-b');
    await page.locator('#retryProfileSaveBtn').click();
    await page.waitForFunction(()=>document.querySelector('#profileSaveStatus').textContent==='已自动保存',null,{timeout:5000});
    assert.equal(state.selected,'game-a');
    assert.equal(state.saved['game-a']['zone.leftHand'].action.target,'CTRL+P');

    await page.locator('[data-view=play]').click();
    await page.locator('#adjustZonesBtn').click();
    await page.locator('#zoneEditBar').waitFor({state:'visible'});
    // 挪动区域先把跟随框定住；取消回到跟着走，框不变。
    assert.equal(state.zones.frozen,true);
    const oldX1=state.zones.rects.leftHand.x1;
    await page.locator('[data-zone=leftHand]').focus();await page.keyboard.press('ArrowLeft');
    await page.locator('#cancelLiveZonesBtn').click();
    await page.locator('#zoneEditBar').waitFor({state:'hidden'});
    assert.equal(state.zones.frozen,false);assert.equal(state.zones.rects.leftHand.x1,oldX1);
    await page.locator('#adjustZonesBtn').click();await page.locator('#zoneEditBar').waitFor({state:'visible'});
    await page.locator('[data-zone=leftHand]').focus();await page.keyboard.press('ArrowLeft');
    await page.locator('#saveLiveZonesBtn').click();await page.locator('#zoneEditBar').waitFor({state:'hidden'});
    // 画面是镜像的：往屏幕左边挪，原始坐标变大。
    assert.equal(state.zones.frozen,true);assert.ok(state.zones.rects.leftHand.x1>oldX1);
    await page.locator('#voiceCommandsBtn').click();assert.equal(await page.locator('#voiceCommandsMask').isVisible(),true);
    await page.keyboard.press('Escape');assert.equal(await page.locator('#voiceCommandsMask').isVisible(),false);
    await page.locator('#calBtn').click();
    await page.locator('#calibrationOverlay').waitFor({state:'visible'});
    await page.keyboard.press('Escape');await page.locator('#calibrationOverlay').waitFor({state:'hidden'});
    await page.locator('[data-view=devices]').click();
    await page.locator('#poseSource').selectOption('phone');await delay(350);
    assert.equal(await page.locator('#poseSource').inputValue(),'phone');
    state.source='phone';state.phone=false;await delay(1100);
    assert.match(await page.locator('#mobileStatus').textContent(),/未连接/);
    state.phone=true;await delay(1100);
    assert.match(await page.locator('#mobileStatus').textContent(),/已连接/);
    state.source='computer';
    state.failHead=true;
    // 头控细调收在「更多设置」里，展开才看得见、按得动。
    await page.locator('.view-control-more > summary').click();
    await page.locator('#speedX').focus();await page.keyboard.press('ArrowRight');
    await page.locator('#retryHeadBtn').waitFor({state:'visible'});await delay(350);
    assert.equal(await page.locator('#speedX').inputValue(),'60');
    state.failHead=false;await page.locator('#retryHeadBtn').click();
    await page.waitForFunction(()=>document.querySelector('#headSaveStatus').textContent==='已自动保存');
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(artifacts,'devices.png'),fullPage:true});
    await page.locator('[data-view=games]').click();await page.screenshot({path:path.join(artifacts,'games.png'),fullPage:true});
    await page.locator('[data-view=play]').click();

    // 智能判定：判断中的框是黄的（系统功能底下有稳住的进度条），判定扫过的闪红。
    state.zonePhase={leftHand:{phase:'pending',progress:.5},rightHand:{phase:'swept'}};
    await page.locator('.zone[data-zone="leftHand"].pending').waitFor({state:'attached'});
    await page.locator('.zone[data-zone="rightHand"].swept').waitFor({state:'attached'});
    assert.equal(await page.locator('.zone[data-zone="leftHand"]').evaluate(el=>el.style.getPropertyValue('--progress')),'50%');
    await page.locator('#viewer').screenshot({path:path.join(artifacts,'zone-phases.png')});
    state.zonePhase={};
    await page.waitForFunction(()=>!document.querySelector('.zone.pending'));
    // 没录过的动作老误按框：提示去录，点了直接打开教学里「录我的动作」那一课。
    state.misfire={trigger:'motion.hands_up',zones:['leftHand'],count:3};
    await page.locator('#misfireHint').waitFor({state:'visible'});
    assert.match(await page.locator('#misfireHint span').textContent(),/左手框 3 次/);
    await page.locator('#misfireHintGo').click();
    await page.locator('#tourStep').filter({hasText:'录我的动作'}).waitFor();
    await page.locator('#tourCloseBtn').click();
    await page.locator('#misfireHintDismiss').click();
    await page.locator('#misfireHint').waitFor({state:'hidden'});
    // 映射表：框那几行有「做动作时也要按」，头顶默认勾上；下面一句说哪些动作会扫过它。
    await page.locator('[data-view=games]').click();
    const leftWith=page.locator('.binding-row[data-trigger="zone.leftHand"] .zone-with-motion-box');
    assert.equal(await leftWith.isChecked(),false);
    assert.equal(await page.locator('.binding-row[data-trigger="zone.headJump"] .zone-with-motion-box').isChecked(),true);
    await page.locator('.binding-row[data-trigger="zone.leftHand"] .zone-conflict-note').waitFor({state:'visible'});
    assert.match(await page.locator('.binding-row[data-trigger="zone.leftHand"] .zone-conflict-note').textContent(),/录的 5 次里扫过 4 次.*让路不按/);
    const savesBefore=state.saveCalls.length;
    await leftWith.check();
    for(let i=0;i<40&&state.saveCalls.length===savesBefore;i++)await delay(50);
    assert.equal(state.saveCalls.at(-1).overrides['zone.leftHand'].with_motion,true);
    await page.locator('.binding-row[data-trigger="zone.leftHand"]').screenshot({path:path.join(artifacts,'zone-with-motion.png')});
    // 通用设置：录过几项、还差哪几项，体检报告。
    await page.locator('[data-view=devices]').click();
    assert.match(await page.locator('#intentStatus').textContent(),/录过 3 \/ 5 项；还没录：双手举过头、开合跳/);
    assert.equal(await page.locator('#intentRecordBtn').textContent(),'补录没录的');
    await page.locator('#intentReport > summary').click();
    assert.match(await page.locator('#intentReportBody').textContent(),/做动作时误按 0%/);
    await page.locator('#zoneTriggerSettings').screenshot({path:path.join(artifacts,'zone-trigger-settings.png')});
    await page.locator('#intentRecordBtn').click();
    await page.locator('#tourStep').filter({hasText:'录我的动作'}).waitFor();
    await page.locator('#tourCloseBtn').click();
    await page.locator('[data-view=play]').click();

    for(const [width,height] of [[1920,1080],[1366,768],[1024,768]]){
      await page.setViewportSize({width,height});
      await page.evaluate(()=>window.scrollTo(0,0));
      for(const id of ['mainActionBtn','stopBtn']){
        const box=await page.locator('#'+id).boundingBox();assert.ok(box.y>=0&&box.y+box.height<=height,id);
      }
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.screenshot({path:path.join(artifacts,`play-${width}.png`)});
    }
    assert.equal(state.performanceCalls,0);
    state.offline=true;
    await page.waitForFunction(()=>document.querySelector('#serviceStatus').textContent.includes('服务失联'));
    assert.equal(await page.locator('#mainActionBtn').isDisabled(),true);
    state.offline=false;
    await page.waitForFunction(()=>document.querySelector('#serviceStatus').textContent==='本地服务已连接');
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({result:'passed',saveRequests:state.saveCalls.length,maxConcurrentSaves:state.maxSaving,screenshots:artifacts},null,2));
  }catch(error){
    console.error(await page.evaluate(()=>({notice:document.querySelector('#notice')?.textContent,
      save:document.querySelector('#profileSaveStatus')?.textContent,
      locked:document.querySelector('#mappingFields')?.disabled})));
    console.error({selected:state.selected,selects:state.selectCalls,requests:state.saveCalls,errors});
    throw error;
  }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
