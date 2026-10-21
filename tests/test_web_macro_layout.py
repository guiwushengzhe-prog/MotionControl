"""宏标题说明在不同字体/视口下分行，输入法确认键不新建宏。"""

import pytest

from test_web_optimization import desktop, mock_browser  # noqa: F401


@pytest.mark.parametrize(("width", "font_size"), [(1280, 16), (720, 24), (560, 32)])
def test_macro_heading_description_and_create_field_do_not_overlap(desktop, width, font_size):
    page = desktop.page
    page.set_viewport_size({"width": width, "height": 1000})
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="voice"]')
    boxes = page.evaluate("""size=>{
      document.documentElement.style.fontSize=size+'px';
      const box=selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {top:r.top,bottom:r.bottom}};
      return {title:box('#macroPanel>.group-title'),description:box('#macroPanel>.pane-sub'),form:box('#macroPanel>.inline-form')};
    }""", font_size)
    assert boxes["title"]["bottom"] <= boxes["description"]["top"]
    assert boxes["description"]["bottom"] <= boxes["form"]["top"]


def test_ime_enter_does_not_create_macro(desktop):
    page, api = desktop.page, desktop.api
    page.click('nav [data-view="devices"]')
    page.click('#settingsNav [data-pane="voice"]')
    page.locator("#macroName").evaluate("input=>{input.value='ceshi';input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',keyCode:229,isComposing:true,bubbles:true}))}")
    page.wait_for_timeout(100)
    assert not any(path == "/api/macros/create" for path, body in api.requests)
