"""真实浏览器检查教学衔接；不采集摄像头、不启用游戏输出。"""
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_web_optimization import ORIGIN, LocalApi
from test_web_ui import browser as mock_browser

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def lesson_page(mock_browser):
    context = mock_browser.new_context(viewport={'width': 1280, 'height': 820})
    context.add_init_script("localStorage.setItem('motioncontrol_tutorial_v2', JSON.stringify({seen:1,offered:true}));")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route(ORIGIN + '/**', LocalApi().handle)
    # 同一份页面和教学模块，省去与本测试无关的摄像头、设备轮询。
    html = re.sub(r'<script\b[^>]*>.*?</script>', '',
                  (ROOT / 'web/index.html').read_text(encoding='utf-8'), flags=re.S)
    page.route(ORIGIN + '/teaching-test.html', lambda route: route.fulfill(body=html, content_type='text/html'))
    page.goto(ORIGIN + '/teaching-test.html')
    page.evaluate("""async()=>{
      const {createTutorial}=await import('/tutorial.js');
      window.teachingState={view:'play',cameraReady:true,posed:true,headShoulders:true,
        fistHands:['left'],zonesFrozen:true,kernelNow:100,fit:{state:'idle'},zones:[],
        horizontal:'off',vertical:'off'};
      window.teachingCalls=[];
      const start=kind=>{teachingCalls.push(kind);teachingState.fit={active:true,state:'preparing',remaining_s:3};};
      window.testTutorial=createTutorial({state:()=>teachingState,
        zoneFitStart:()=>start('full'),zoneFitGripOnly:()=>start('grip'),
        zoneFitCancel:()=>teachingCalls.push('cancel')});
    }""")
    yield page
    assert errors == []
    context.close()


def test_fixed_fit_result_stays_visible_and_does_not_ask_to_unfreeze(lesson_page):
    page = lesson_page
    page.evaluate("testTutorial.openLesson('fit')")
    expect(page.locator('#tourHint')).to_contain_text('量到的圈会直接更新，并继续固定')
    expect(page.locator('#tourChoiceBtn')).to_contain_text('3 秒后开始')
    page.click('#tourChoiceBtn')
    expect(page.locator('#tourHint')).to_contain_text('还剩 3 秒')
    page.evaluate("""teachingState.fit={active:false,state:'done',regions_mode:'fixed',
      measured:['rightHand','leftGrip'],skipped:[],preserved_zones:['leftHand'],grip_applied:['curl_close']};""")
    expect(page.locator('#tourSay')).to_contain_text('仍保持固定')
    expect(page.locator('#tourSay')).to_contain_text('左手区使用自选触发点，保留原区域')
    expect(page.locator('#tourNextBtn')).to_have_text('完成')
    page.wait_for_timeout(1500)
    expect(page.locator('#tour')).to_be_visible()
    assert '恢复跟随' not in page.locator('#tour').inner_text()
    assert page.evaluate('teachingCalls') == ['full']
    page.click('#tourNextBtn')
    expect(page.locator('#tour')).to_be_hidden()


def test_grip_shortcut_only_starts_grip_and_closing_cancels(lesson_page):
    page = lesson_page
    page.evaluate("testTutorial.openLesson('fit',null,{gripOnly:true})")
    expect(page.locator('#tourHint')).to_contain_text('只量握拳')
    page.click('#tourChoiceBtn')
    expect(page.locator('#tourHint')).to_contain_text('还剩 3 秒')
    page.click('#tourCloseBtn')
    assert page.evaluate('teachingCalls') == ['grip', 'cancel']


@pytest.mark.parametrize('groups,words', [
    ([['鼻子']], '让鼻子进入'),
    ([['左手腕', '右手腕'], ['左手肘']], '让左手腕和右手腕，或左手肘进入'),
])
def test_zone_teaching_uses_current_points_and_simultaneous_groups(lesson_page, groups, words):
    page = lesson_page
    page.evaluate("""groups=>{
      teachingState.zones=[{id:'headJump',body:'头顶',key:'A',shown:true,pressed:false,triggerGroups:groups}];
      testTutorial.open(null,{menu:false});
    }""", groups)
    for _ in range(4):
        page.click('#tourSkipBtn')  # 站进画面、左右、量身、上下
    expect(page.locator('#tourSay')).to_contain_text(words)
    assert '手举过头' not in page.locator('#tourSay').inner_text()
    if len(groups[0]) > 1:
        expect(page.locator('#tourHint')).to_contain_text('同时进圈')


def test_first_teaching_reuses_saved_measurements(lesson_page):
    page = lesson_page
    page.evaluate("""()=>{
      teachingState.fit={state:'idle',measured_at_unix:100,grip_measured_at_unix:100};
      testTutorial.open(null,{menu:false});
    }""")
    page.click('#tourSkipBtn')
    page.click('#tourSkipBtn')
    expect(page.locator('#tourSay')).to_contain_text('沿用上次量身')
    expect(page.locator('#tourNextBtn')).to_have_text('继续')
    assert page.evaluate('teachingCalls') == []
    page.click('#tourNextBtn')
    expect(page.locator('#tourStep')).to_contain_text('上下转视角')
