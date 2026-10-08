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
        api.runtime.update(body_mode=source, camera={"running": False, "backend": backend})
        result = page.evaluate("""async runtime=>{
          const p=await import('/js/play.js');
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
