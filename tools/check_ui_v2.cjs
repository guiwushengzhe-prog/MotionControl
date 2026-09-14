// Offline browser checks: every service endpoint is simulated; no camera or output is opened.
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, 'output', 'playwright', 'v2');
fs.mkdirSync(artifacts, { recursive: true });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const zones = Object.fromEntries(['leftHand','rightHand','leftFoot','rightFoot','headJump','lookGate'].map((id,i)=>[id,{cx:.2+(i%3)*.25,cy:.25+Math.floor(i/3)*.35,r:.07}]));
const state = {
  selected:'game-a',saved:{},output:{enabled:false,mode:'mouse',mouse_available:true,mouse_speed_x:960,gamepad_gain:1.6},
  camera:false,source:'computer',phone:false,offline:false,failSource:true,failSave:false,failStop:false,failStart:false,
  saveDelay:0,saving:0,maxSaving:0,saveCalls:[],selectCalls:[],performanceCalls:0,failHead:false,
  scene:{configured:true,zones,vertical_look:{source:'hand',range_y:.18,deadzone:.1,center_y:.5}},
  head:{algorithm:'pnp',horizontal_algorithm:'classic',enabled:true,calibrated:true,sensitivity_x:58,sensitivity_y:46,deadzone:.1},
};
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
    zones:Object.fromEntries(Object.entries(state.scene.zones).map(([id,circle])=>[id,{circle}])),head:state.head}};
}
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1366,height:768}});
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
      case '/api/scene/status':return respond(state.scene);
      case '/api/scene/layout':state.scene={configured:true,zones:body.zones,vertical_look:body.vertical_look};return respond(state.scene);
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
    const row=page.locator('[data-trigger="zone.leftHand"]');
    await row.locator('.binding-type').selectOption('keyboard');
    state.failSave=true;await row.locator('.binding-target').fill('CTRL+M');
    await page.locator('#retryProfileSaveBtn').waitFor({state:'visible'});
    await page.locator('#profileSelect').selectOption('game-b');await page.locator('#profileApplyBtn').click();
    await page.waitForFunction(()=>!document.querySelector('#mappingFields').disabled);
    assert.equal(state.selected,'game-a');assert.equal(await row.locator('.binding-target').inputValue(),'CTRL+M');
    state.failSave=false;await page.locator('#retryProfileSaveBtn').click();
    await page.waitForFunction(()=>document.querySelector('#profileSaveStatus').textContent==='已自动保存');
    state.saveDelay=600;
    await row.locator('.binding-target').fill('CTRL+K');await delay(400);
    await row.locator('.binding-target').fill('CTRL+L');
    await page.locator('#profileApplyBtn').click();
    await page.waitForFunction(()=>document.querySelector('#currentGameName').textContent==='示例游戏乙');
    assert.equal(state.saved['game-a']['zone.leftHand'].action.target,'CTRL+L');
    assert.equal(state.maxSaving,1);
    await page.locator('#profileSelect').selectOption('game-a');await page.locator('#profileApplyBtn').click();
    await page.waitForFunction(()=>document.querySelector('#currentGameName').textContent==='示例游戏甲');
    assert.equal(await row.locator('.binding-target').inputValue(),'CTRL+L');
    await page.reload();await row.waitFor({state:'attached'});
    await page.locator('[data-view=games]').click();assert.equal(await row.locator('.binding-target').inputValue(),'CTRL+L');
    const jumping=page.locator('[data-trigger="motion.jumping_jack"] .binding-type');
    await jumping.selectOption('gamepad');
    assert.equal(await page.locator('[data-trigger="motion.hands_up"] .binding-type').isDisabled(),true);
    await page.locator('#profileSaveStatus').getByText('已自动保存').waitFor();
    // A second client changed games: retain the draft, then explicitly restore its target.
    state.selected='game-b';
    await row.locator('.binding-target').fill('CTRL+P');
    await page.getByRole('button',{name:'重新选择本游戏并保存草稿'}).waitFor();
    assert.equal(state.selected,'game-b');
    await page.locator('#retryProfileSaveBtn').click();
    await page.waitForFunction(()=>document.querySelector('#profileSaveStatus').textContent==='已自动保存',null,{timeout:5000});
    assert.equal(state.selected,'game-a');
    assert.equal(state.saved['game-a']['zone.leftHand'].action.target,'CTRL+P');

    await page.locator('[data-view=play]').click();
    await page.locator('#adjustZonesBtn').click();
    await page.locator('#zoneEditBar').waitFor({state:'visible'});
    const oldCx=state.scene.zones.leftHand.cx;
    await page.locator('[data-zone=leftHand]').focus();await page.keyboard.press('ArrowLeft');
    await page.locator('#cancelLiveZonesBtn').click();
    await page.locator('#zoneEditBar').waitFor({state:'hidden'});assert.equal(state.scene.zones.leftHand.cx,oldCx);
    await page.locator('#adjustZonesBtn').click();await page.locator('#zoneEditBar').waitFor({state:'visible'});
    await page.locator('[data-zone=leftHand]').focus();await page.keyboard.press('ArrowLeft');
    await page.locator('#saveLiveZonesBtn').click();await page.locator('#zoneEditBar').waitFor({state:'hidden'});
    assert.ok(state.scene.zones.leftHand.cx>oldCx);
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
    await page.locator('#speedX').focus();await page.keyboard.press('ArrowRight');
    await page.locator('#retryHeadBtn').waitFor({state:'visible'});await delay(350);
    assert.equal(await page.locator('#speedX').inputValue(),'60');
    state.failHead=false;await page.locator('#retryHeadBtn').click();
    await page.waitForFunction(()=>document.querySelector('#headSaveStatus').textContent==='已自动保存');
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(artifacts,'devices.png'),fullPage:true});
    await page.locator('[data-view=games]').click();await page.screenshot({path:path.join(artifacts,'games.png'),fullPage:true});
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
