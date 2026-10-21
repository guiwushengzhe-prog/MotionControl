"""首页和悬浮绘制使用同一映射条件，缩放时保持视频画幅。"""

import copy

import pytest

from test_web_optimization import desktop, mock_browser  # noqa: F401


REGION = "user_a1234567"
RECT = {"x1": .2, "x2": .6, "y1": .2, "y2": .6}


def runtime(api, *, binding=None, mapped=None, width=640, height=480):
    state = copy.deepcopy(api.runtime)
    state["camera"].update(running=True, frames=100, last_frame_age_ms=0)
    zone = {"rect": RECT, "custom": True, "name": "自定义区域", "pressed": True, "recognized": True}
    if mapped is not None:
        zone["mapped"] = mapped
    state["kernel"].update(width=width, height=height, pose={"nose": {"x": .5, "y": .3, "score": .9}},
                            zones={REGION: zone}, effective_bindings={f"zone.{REGION}": binding} if binding else {})
    return state


def render(page, state):
    page.evaluate("state => import('/js/play.js').then(m => m.renderKernelState(state, true))", state)


@pytest.mark.parametrize("binding", [None, {"action": {}}, {"disabled": True, "action": {"type": "keyboard", "target": "Q"}},
                                     {"action": {"type": "macro", "target": "missing"}}])
def test_main_preview_hides_unmapped_regions_even_from_old_runtime(desktop, binding):
    render(desktop.page, runtime(desktop.api, binding=binding))
    assert desktop.page.locator(f'#viewer .zone[data-zone="{REGION}"]').is_hidden()


def test_backend_mapping_status_wins_over_stale_binding_and_overlay_does_not_draw_it(desktop):
    page = desktop.page
    state = runtime(desktop.api, binding={"action": {"type": "keyboard", "target": "Q"}}, mapped=False)
    render(page, state)
    page.evaluate("""async()=>{
      const m=await import('/js/play.js');
      const c=document.createElement('canvas');c.style.cssText='position:fixed;width:420px;height:315px';document.body.append(c);
      const ctx=c.getContext('2d'),old=ctx.fillText.bind(ctx);window.zoneText=[];
      ctx.fillText=(...args)=>{zoneText.push(args[0]);old(...args)};
      m.overlay.win={closed:false};m.overlay.canvas=c;m.overlay.ctx=ctx;
    }""")
    render(page, state)
    assert page.locator(f'#viewer .zone[data-zone="{REGION}"]').is_hidden()
    assert page.evaluate("!zoneText.includes('Q') && !zoneText.includes('未映射')")


@pytest.mark.parametrize("size", [(1280, 720), (480, 640)])
def test_preview_and_overlay_keep_real_frame_aspect_after_resize(desktop, size):
    page = desktop.page
    state = runtime(desktop.api, binding={"action": {"type": "keyboard", "target": "Q"}}, mapped=True,
                    width=size[0], height=size[1])
    page.evaluate("""async()=>{
      const m=await import('/js/play.js'),c=document.createElement('canvas');
      c.style.cssText='position:fixed;width:420px;height:315px';document.body.append(c);
      m.overlay.win={closed:false};m.overlay.canvas=c;m.overlay.ctx=c.getContext('2d');
    }""")
    render(page, state)
    for viewport in ({"width": 1100, "height": 800}, {"width": 700, "height": 900}):
        page.set_viewport_size(viewport)
        render(page, state)
        dimensions = page.evaluate("""async()=>{
          const m=await import('/js/play.js'),v=viewer.getBoundingClientRect(),c=canvas.getBoundingClientRect();
          return {viewer:v.width/v.height,canvas:c.width/c.height,pixels:canvas.width/canvas.height,
                  overlay:m.overlay.canvas.width/m.overlay.canvas.height};
        }""")
        for ratio in dimensions.values():
            assert ratio == pytest.approx(size[0] / size[1], rel=.01)
