"""原有设备页的 Kinect 下拉和普通样式开关；不启动真实采集。"""
from playwright.sync_api import expect
from test_web_optimization import desktop
from test_web_ui import browser as mock_browser


def test_kinect_select_depth_toggle_and_regular_camera_ui(desktop):
    page, api = desktop.page, desktop.api
    state = {"camera_index": 0, "camera_device": "0", "depth_supported": False,
             "depth_enabled": False, "preference": "dshow", "rotation": "none"}
    requests = []
    def camera(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            requests.append(body)
            if "device" in body:
                state["camera_device"] = body["device"]
                state["depth_supported"] = body["device"].startswith("kinect2:")
                state["depth_enabled"] = state["depth_supported"]
            if "depth_enabled" in body:
                state["depth_enabled"] = body["depth_enabled"]
        if route.request.method == "POST" and "device" in route.request.post_data_json:
            api.runtime["camera"] = {**state, "running": True}
        api.fulfill(route, {**state, **api.runtime})
    page.route("**/api/camera/config", camera)
    page.goto("http://motioncontrol.test")
    page.click('nav [data-view="devices"]')
    page.locator('#cameraSettings > summary').click()
    page.click('#settingsNav [data-pane="devices"]')
    expect(page.locator('#cameraDepthRow')).to_be_hidden()
    page.evaluate("""async()=>{const d=await import('/js/devices.js');
      d.renderCameraDevices([{index:0,width:640,height:480},{id:'kinect2:camera-one',name:'微软 Kinect',depth_supported:true}],0,{camera_device:'0'});}""")
    page.select_option('#cameraDevice', 'kinect2:camera-one')
    expect(page.locator('#cameraDepthRow')).to_be_visible()
    expect(page.locator('#cameraDepth')).to_be_checked()
    assert page.locator('#cameraDepth').get_attribute('class') == 'switch'
    page.locator('#cameraDepth').uncheck()
    expect(page.locator('#cameraDepth')).not_to_be_checked()
    assert any(r.get('depth_enabled') is False for r in requests)
    page.evaluate("async()=>{await (await import('/js/devices.js')).refreshCameraConfig();}")
    expect(page.locator('#cameraDepth')).not_to_be_checked()
    page.evaluate("""async()=>{const d=await import('/js/devices.js');
      d.renderCameraDevices([{index:0},{id:'kinect2:camera-one',name:'微软 Kinect'}],0,{camera_device:'kinect2:camera-one'});}""")
    page.select_option('#cameraDevice', '0')
    expect(page.locator('#cameraDepthRow')).to_be_hidden()
    assert {'device':'kinect2:camera-one'} in requests
    assert {'device':'0'} in requests
    assert desktop.errors == []


def test_kinect_regions_and_editing_keep_original_display_direction(desktop):
    page, api = desktop.page, desktop.api
    # 反向在采集侧完成；区域显示、悬浮窗与编辑保持原来的规则。
    rect = {"x1": .2, "x2": .35, "y1": .3, "y2": .5}
    api.runtime["kernel"].update(
        pose={"right_wrist": {"x": .25, "y": .4, "score": 1}},
        zones={"rightHand": {"rect": rect, "pressed": True}},
    )
    for source, backend, mirrored in (
        ("computer", "kinect", True),
        ("computer", "dshow", True),
        ("phone", "kinect", True),
        ("computer", "kinect", True),
    ):
        api.runtime.update(body_mode=source, camera={"running": True, "backend": backend,
                                                   "frames": 1, "last_frame_age_ms": 0})
        result = page.evaluate("""async runtime=>{
          const p=await import('/js/play.js');
          (await import('/js/devices.js')).renderInputStatus({mobile_pose_connected:runtime.body_mode==='phone'});
          const c=document.createElement('canvas'),ctx=c.getContext('2d');
          const marks={points:[],zones:[]};
          const arc=ctx.arc.bind(ctx),roundRect=ctx.roundRect.bind(ctx);
          ctx.arc=(x,y,...args)=>{
            const m=ctx.getTransform();marks.points.push((m.a*x+m.e)/c.width);
            return arc(x,y,...args);
          };
          ctx.roundRect=(x,y,...args)=>{marks.zones.push(x/c.width);return roundRect(x,y,...args)};
          Object.assign(p.overlay,{win:{closed:false},canvas:c,ctx});
          p.renderKernelState(runtime);
          document.querySelector('#cameraPreview').hidden=false;
          const layers=['#cameraPreview','#canvas','.zones','.zone[data-zone="rightHand"]'];
          marks.layers=layers.map(s=>{
            const t=getComputedStyle(document.querySelector(s)).transform;
            return t==='none'?1:new DOMMatrix(t).a;
          });
          const r=structuredClone(runtime.kernel.zones.rightHand.rect);
          p.rectEdit.rects.rightHand=r;
          p.nudgeRect('rightHand','ArrowRight',false);
          marks.movedX=p.rectEdit.rects.rightHand.x1;
          p.nudgeRect('rightHand','ArrowRight',true);
          marks.resized=p.rectEdit.rects.rightHand;
          Object.assign(p.overlay,{win:null,canvas:null,ctx:null});
          return marks;
        }""", api.runtime)
        sign = -1 if mirrored else 1
        assert result["layers"] == [sign] * 4
        assert abs(result["points"][0] - (.75 if mirrored else .25)) < 1e-6
        assert abs(result["zones"][0] - (.65 if mirrored else .2)) < 1e-6
        assert abs(result["movedX"] - (.19 if mirrored else .21)) < 1e-6
        assert abs(result["resized"]["x1"] - (.18 if mirrored else .21)) < 1e-6
        assert abs(result["resized"]["x2"] - (.34 if mirrored else .37)) < 1e-6
    assert desktop.errors == []


def test_resource_shortage_banner_names_the_cause_and_reuses_settings(desktop):
    page, api = desktop.page, desktop.api
    # 资源持续不够时在开始页提醒；按钮直接走设置页同一个开关。
    api.runtime["camera"] = {"running": True, "depth_supported": True, "depth_enabled": True}
    perf = {"running": True, "capture_fps": 15, "inference_fps": 9, "actual_model": "heavy",
            "camera_timing": {"color_frame_interval_ms": 66.67},
            "resource_hint": {"kind": "inference", "actions": ["standard_model", "disable_depth"]}}
    requests = []
    page.route("**/api/performance", lambda route: api.fulfill(route, perf))
    def camera(route):
        if route.request.method == "POST":
            requests.append(route.request.post_data_json)
        api.fulfill(route, {**api.runtime["camera"], "camera_device": "kinect2:one",
                            "depth_enabled": {"depth_enabled": False} not in requests})
    page.route("**/api/camera/config", camera)
    def models(route):
        if route.request.method == "POST":
            requests.append(route.request.post_data_json)
        api.fulfill(route, {"model_preference": "full", "models": [{"id": "mp-full", "available": True},
                                                                   {"id": "mp-heavy", "available": True}]})
    page.route("**/api/recognition/models", models)
    banner = page.locator("#resourceHint")
    expect(banner).to_be_visible(timeout=6000)
    expect(banner).to_contain_text("识别 9 帧/秒，跟不上摄像头的 15 帧/秒")
    expect(banner).to_contain_text("建议识别精度改用标准、关闭深度，或关闭其他占用处理器的程序")
    page.click("#resourceHintModel")
    expect(banner).to_be_hidden()
    page.wait_for_function("document.querySelector('#poseModelSelect').value==='full'")
    assert {"preference": "full"} in requests

    # 同一种情况点过就不再弹；恢复后换成采集被拖慢，重新提醒并只给关闭深度。
    page.wait_for_timeout(1700)
    expect(banner).to_be_hidden()
    perf["resource_hint"] = None
    page.evaluate("async()=>{await (await import('/js/devices.js')).refreshPerformance()}")
    perf.update(capture_fps=3, inference_fps=3, actual_model="full",
                camera_timing={"color_frame_interval_ms": 33.33},
                resource_hint={"kind": "capture", "actions": ["disable_depth"]})
    page.evaluate("async()=>{await (await import('/js/devices.js')).refreshPerformance()}")
    expect(banner).to_contain_text("摄像头只收到 3 帧/秒（相机可出 30 帧），多半是显卡被游戏占满")
    expect(banner).to_contain_text("建议关闭深度，或在游戏里限制帧率")
    expect(page.locator("#resourceHintModel")).to_be_hidden()
    page.click("#resourceHintDepth")
    expect(banner).to_be_hidden()
    expect(page.locator("#cameraDepth")).not_to_be_checked()
    assert {"depth_enabled": False} in requests
    assert desktop.errors == []


def test_starved_kinect_service_offers_admin_boost(desktop):
    page, api = desktop.page, desktop.api
    api.runtime["camera"] = {"running": True, "depth_supported": True, "depth_enabled": False}
    perf = {"running": True, "capture_fps": 2, "inference_fps": 2, "actual_model": "full",
            "capture_decoder": "kinect_service", "kinect_service_boosted": False,
            "camera_timing": {"color_frame_interval_ms": 66.67},
            "resource_hint": {"kind": "capture", "actions": ["boost_kinect_service"]}}
    page.route("**/api/performance", lambda route: api.fulfill(route, perf))
    boosts = []
    def boost(route):
        boosts.append(route.request.method)
        perf.update(kinect_service_boosted=True, resource_hint=None)
        api.fulfill(route, {"boosted": True})
    page.route("**/api/camera/kinect-service-priority", boost)
    banner = page.locator("#resourceHint")
    expect(banner).to_be_visible(timeout=6000)
    expect(banner).to_contain_text("摄像头只收到 2 帧/秒（相机可出 15 帧）")
    expect(banner).to_contain_text("建议提高 Kinect 服务优先级（需要管理员权限），或在游戏里限制帧率，或改用只占处理器的普通摄像头")
    expect(page.locator("#resourceHintModel")).to_be_hidden()
    expect(page.locator("#resourceHintDepth")).to_be_hidden()
    page.click("#resourceHintBoost")
    expect(page.locator("#notice")).to_contain_text("已提高 Kinect 服务优先级", timeout=6000)
    assert boosts == ["POST"]
    expect(banner).to_be_hidden(timeout=6000)
    assert desktop.errors == []


def test_regular_camera_decoder_choice_follows_runtime_support(desktop):
    page, api = desktop.page, desktop.api
    state = {"camera_index": 0, "camera_device": "0", "depth_supported": False, "depth_enabled": False,
             "preference": "auto", "rotation": "none", "decoder": "cpu", "gpu_decode_supported": False}
    posts = []
    def camera(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            posts.append(body)
            state.update(body)
        api.fulfill(route, {**state, **api.runtime})
    page.route("**/api/camera/config", camera)
    page.goto("http://motioncontrol.test")
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="devices"]')
    page.evaluate("async()=>{await (await import('/js/devices.js')).refreshCameraConfig()}")
    page.locator('#cameraSettings > summary').click()
    expect(page.locator("#cameraDecoderRow")).to_be_visible()
    expect(page.locator("#cameraDecoder")).to_have_value("cpu")
    expect(page.locator('#cameraDecoder option[value="gpu"]')).to_be_disabled()
    expect(page.locator("#cameraDecoderStatus")).to_contain_text("不含显卡解码")
    state["gpu_decode_supported"] = True
    page.evaluate("async()=>{await (await import('/js/devices.js')).refreshCameraConfig()}")
    expect(page.locator('#cameraDecoder option[value="gpu"]')).to_be_enabled()
    page.select_option("#cameraDecoder", "gpu")
    expect(page.locator("#cameraDecoderStatus")).to_contain_text("显卡加速")
    assert {"decoder": "gpu"} in posts
    # Kinect 画面由原厂服务处理，不显示这个选项。
    state.update(camera_device="kinect2:one", depth_supported=True)
    page.evaluate("async()=>{await (await import('/js/devices.js')).refreshCameraConfig()}")
    expect(page.locator("#cameraDecoderRow")).to_be_hidden()
    assert desktop.errors == []


def test_kinect_capture_boost_switch(desktop):
    page, api = desktop.page, desktop.api
    state = {"camera_index": 0, "camera_device": "kinect2:one", "depth_supported": True, "depth_enabled": False,
             "preference": "auto", "rotation": "none", "decoder": "cpu", "gpu_decode_supported": False,
             "kinect_boost_always": False, "kinect_service_boosted": False}
    posts = []
    page.route("**/api/camera/config", lambda route: api.fulfill(route, {**state, **api.runtime}))
    def boost(route):
        posts.append(route.request.post_data_json)
        state.update(kinect_boost_always=posts[-1]["always"], kinect_service_boosted=posts[-1]["always"])
        api.fulfill(route, state)
    page.route("**/api/camera/kinect-service-priority", boost)
    page.goto("http://motioncontrol.test")
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="devices"]')
    page.evaluate("async()=>{await (await import('/js/devices.js')).refreshCameraConfig()}")
    page.locator('#cameraSettings > summary').click()
    expect(page.locator("#cameraBoostRow")).to_be_visible()
    expect(page.locator("#cameraDecoderRow")).to_be_hidden()
    expect(page.locator("#cameraBoost")).not_to_be_checked()
    expect(page.locator("#cameraBoostStatus")).to_contain_text("只在被拖慢过的游戏里申请")
    page.locator("#cameraBoost").check()
    expect(page.locator("#notice")).to_contain_text("已提高 Kinect 采集优先级", timeout=6000)
    expect(page.locator("#cameraBoostStatus")).to_contain_text("已提高")
    expect(page.locator("#cameraBoost")).to_be_checked()
    expect(page.locator("#cameraBoost")).to_be_enabled()
    page.locator("#cameraBoost").uncheck()
    expect(page.locator("#notice")).to_contain_text("已关闭并恢复 Kinect 默认优先级", timeout=6000)
    assert posts == [{"always": True}, {"always": False}]
    # 普通摄像头不显示采集提权。
    state.update(camera_device="0", depth_supported=False)
    page.evaluate("async()=>{await (await import('/js/devices.js')).refreshCameraConfig()}")
    expect(page.locator("#cameraBoostRow")).to_be_hidden()
    assert desktop.errors == []
