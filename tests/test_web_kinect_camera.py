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
        api.fulfill(route, state)
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
