"""顶栏连接提示、长名称与窄窗口不遮住操作入口，不连接真实设备。"""
import pytest
from test_web_optimization import desktop, mock_browser


@pytest.mark.parametrize("width,font_size", [(1920, 16), (1280, 16), (1100, 16), (1024, 16), (720, 16), (390, 16), (1280, 20)])
def test_disconnected_header_keeps_navigation_and_controls_separate(desktop, width, font_size):
    page = desktop.page
    page.set_viewport_size({"width": width, "height": 900})
    page.route('**/api/kernel/status', lambda route: route.abort('connectionrefused'))
    page.route('**/api/output-status', lambda route: route.abort('connectionrefused'))
    measured = page.evaluate('''async size=>{
      await (await import('/js/play.js')).refreshKernel();
      await (await import('/js/devices.js')).refreshOutput();
      document.documentElement.style.fontSize=size+'px';
      currentGameName.textContent='和平精英 · 很长的游戏名称';
      currentModeName.textContent='自定义常用模式';
      const selectors=['.brand','.command-dock nav','.game-chip:not(.mode-chip)','.mode-chip','#mainActionBtn','#helpBtn'];
      const boxes=selectors.map(selector=>{const r=document.querySelector(selector).getBoundingClientRect();
        return {selector,x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height}});
      const status=serviceStatus.getBoundingClientRect(),header=document.querySelector('.command-dock').getBoundingClientRect();
      const textLines=['.game-chip-label','#currentGameName','#currentModeName','#mainActionLabel'].flatMap(selector=>
        [...document.querySelectorAll(selector)].filter(el=>el.getClientRects().length).map(el=>{
          const range=document.createRange();range.selectNodeContents(el);
          const rects=[...range.getClientRects()];
          return {selector,tops:rects.map(r=>r.top)};
        }));
      const singleLine=textLines.every(line=>Math.max(...line.tops)-Math.min(...line.tops)<2);
      return {boxes,singleLine,textLines,statusTop:status.top,headerBottom:header.bottom,width:innerWidth,
        scrollWidth:document.documentElement.scrollWidth,disabled:mainActionBtn.disabled,message:serviceStatus.textContent};
    }''', font_size)
    assert measured['singleLine'], measured
    assert measured['statusTop'] >= measured['headerBottom'] - 1, measured
    assert measured['disabled'] and '服务断开' in measured['message']
    assert measured['scrollWidth'] <= width + 1
    boxes = measured['boxes']
    for i, box in enumerate(boxes):
        assert box['x'] >= -1 and box['right'] <= width + 1, (width, box)
        assert box['width'] > 15 and box['height'] > 15, (width, box)
        for other in boxes[i + 1:]:
            overlap = min(box['right'], other['right']) - max(box['x'], other['x']) > 1 and min(box['bottom'], other['bottom']) - max(box['y'], other['y']) > 1
            assert not overlap, (width, box, other)

    page.unroute('**/api/kernel/status')
    page.unroute('**/api/output-status')
    recovered = page.evaluate('''async()=>{
      await (await import('/js/play.js')).refreshKernel();
      await (await import('/js/devices.js')).refreshOutput();
      return {hidden:getComputedStyle(serviceStatus).display==='none',disabled:mainActionBtn.disabled};
    }''')
    assert recovered == {'hidden': True, 'disabled': False}
