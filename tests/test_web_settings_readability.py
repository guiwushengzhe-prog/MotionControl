"""设置的灰色控件、诊断分列与运动记录编辑；不连接设备或使用个人记录。"""
import pytest
from playwright.sync_api import expect
from test_web_optimization import desktop, mock_browser  # noqa: F401


FITNESS = {"status": "finished", "session_counted": True, "active_seconds": 600, "steps": 1234,
    "estimated_kcal": 49.3, "level": 2, "streak": 3, "experience": 150,
    "profile": {"weight_kg": 70, "age": None, "sex": None, "primary_goal": "minutes",
                "goal_active_minutes": 30, "goal_steps": 3000, "goal_kcal": 200},
    "today": {"active_seconds": 600, "steps": 1234, "estimated_kcal": 49.3}}


@pytest.mark.parametrize("theme,width", [("dark", 1280), ("light", 1280), ("dark", 390)])
def test_fitness_summary_history_and_profile_remain_readable_and_editable(desktop, theme, width):
    page, api = desktop.page, desktop.api
    page.evaluate("theme=>document.documentElement.dataset.theme=theme", theme)
    page.set_viewport_size({"width": width, "height": 900})
    page.route("**/api/fitness/state", lambda r: api.fulfill(r, FITNESS))
    page.route("**/api/fitness/history", lambda r: api.fulfill(r, {"sessions": [
        {"started_at_ms": 1792713600000, "active_seconds": 600, "steps": 1234, "estimated_kcal": 49.3}]}))
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="fitness"]')
    expect(page.locator('#fitnessSteps')).to_have_text('1,234')
    assert page.locator('#fitnessHistorySection').get_attribute('open') is None
    expect(page.locator('#fitnessHistory')).to_be_hidden()
    page.locator('#fitnessHistorySection>summary').click()
    expect(page.locator('#fitnessHistory')).to_be_visible()
    expect(page.locator('#fitnessHistory .fitness-history-values')).to_contain_text('49.3')
    page.locator('#fitnessHistorySection>summary').click()
    expect(page.locator('#fitnessHistory')).to_be_hidden()
    assert page.locator('.fitness-details:has(#fitnessProfile)').get_attribute('open') is None
    page.locator('.fitness-details:has(#fitnessProfile)>summary').click()
    expect(page.locator('#fitnessWeight')).to_have_value('70')
    saved = []
    def save(route):
        saved.append(route.request.post_data_json)
        api.fulfill(route, FITNESS)
    page.route('**/api/fitness/control', save)
    page.locator('#fitnessWeight').fill('72')
    page.get_by_role('button', name='保存身体数据与目标', exact=True).click()
    page.wait_for_timeout(50)
    assert saved[0]['profile']['weight_kg'] == 72
    assert saved[0]['profile']['age'] is None
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    assert not desktop.errors


def test_diagnostics_use_fields_and_neutral_buttons_keep_keyboard_focus(desktop):
    page = desktop.page
    page.click('[data-view="devices"]')
    page.click('#settingsNav [data-pane="lab"]')
    page.evaluate('''async()=>{const {renderRuntimeDiagnostics:r}=await import('/js/runtime-diagnostics.js');
      r({running:true,actual_capture_fps:15,captured_frames:200,processed_frames:180,inference_p95_ms:21},
        {head:{enabled:true,horizontal_algorithm:'hfhpe_simple',raw_tilt_deg:5.2,horizontal_block_message:'处于稳定区'}},{},{});
    }''')
    assert page.locator('#diagHead .diag-field').count() > 5
    expect(page.locator('#diagHead')).to_contain_text('5.20°')
    expect(page.locator('#diagHead')).to_contain_text('处于稳定区')
    page.click('#settingsNav [data-pane="voice"]')
    button = page.locator('#commandsPanel .btn.link').first
    colors = button.evaluate('''node=>{const probe=document.createElement('span');probe.style.color='var(--text)';node.append(probe);
      const result={color:getComputedStyle(node).color,text:getComputedStyle(probe).color};probe.remove();return result}''')
    assert colors['color'] == colors['text']
    assert not desktop.errors
