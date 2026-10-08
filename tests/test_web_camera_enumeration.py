"""实际网页验证设备名称、扫描保留选择及黑画面提示；不使用真实摄像头。"""
from playwright.sync_api import expect
from test_web_optimization import desktop
from test_web_ui import browser as mock_browser


def test_scan_keeps_selected_device_and_labels_sources(desktop):
    page, api = desktop.page, desktop.api
    api.runtime["camera"].update(camera_index=2, camera_device="2", running=False)
    page.evaluate("""async()=>{
      const d=await import('/js/devices.js');
      d.renderCameraDevices([{index:0,name:'内置摄像头'},
        {index:2,name:'直播伴侣虚拟摄像头',virtual:true,picture_state:'black'}],
        2,{camera_device:'2',camera_name:'直播伴侣虚拟摄像头'});
      await d.refreshCameraConfig();
    }""")
    # 配置刷新采用后台选择，而不是把扫描列表的第一项设为选中。
    expect(page.locator('#cameraDevice')).to_have_value('2')
    assert page.locator('#cameraDevice option').count() == 2
    expect(page.locator('#cameraDevice option[value="2"]')).to_have_text('直播伴侣虚拟摄像头 · 画面全黑')
    page.evaluate("""async()=>{
      const d=await import('/js/devices.js');
      d.renderCameraDevices([{index:0,name:'内置摄像头'},
        {index:2,name:'直播伴侣虚拟摄像头',virtual:true,picture_state:'black'}],
        2,{camera_device:'2'});
    }""")
    expect(page.locator('#cameraDevice')).to_have_value('2')
    assert desktop.errors == []


def test_black_picture_is_not_presented_as_a_ready_camera_or_one_fps(desktop):
    page, api = desktop.page, desktop.api
    api.runtime.update(body_mode='computer',
        camera={'running':True,'camera_index':2,'camera_name':'虚拟摄像头',
                'camera_virtual':True,'picture_black':True})
    page.route('**/api/performance', lambda route: api.fulfill(route,
        {'inference_fps':1,'capture_fps':1,'picture_black':True}))
    page.evaluate("""async()=>{
      const d=await import('/js/devices.js');
      await (await import('/js/play.js')).refreshKernel(); await d.refreshPerformance();
    }""")
    expect(page.locator('#fpsHud')).to_have_text('画面全黑')
    expect(page.locator('#hint')).to_contain_text('摄像头画面全黑')
    expect(page.locator('#hint')).to_contain_text('请启动虚拟摄像头的画面来源')
    expect(page.locator('.checklist [data-step="camera"]')).to_have_class('step warn')
    expect(page.locator('#recognitionStatus')).to_have_text('摄像头画面全黑')
    # 来源恢复供图后，提示恢复为正常帧率和正常找人的文案。
    api.runtime['camera']['picture_black'] = False
    page.route('**/api/performance', lambda route: api.fulfill(route,
        {'inference_fps':30,'capture_fps':30,'picture_black':False}))
    page.evaluate("""async()=>{
      const d=await import('/js/devices.js');
      await (await import('/js/play.js')).refreshKernel(); await d.refreshPerformance();
    }""")
    expect(page.locator('#fpsHud')).to_have_text('30 帧/秒')
    expect(page.locator('#hint')).to_contain_text('站到镜头前')
    assert desktop.errors == []
