"""已有桌面页面中的对照开关与三状态统计；接口和个人目录隔离。"""
from test_web_optimization import desktop, mock_browser  # noqa: F401
from test_web_sensitivity_experiments import seed, install_save, wait_saved


def test_recording_switch_and_counts_keep_control_source_and_do_not_overflow(desktop):
    seed(desktop)
    page, api = desktop.page, desktop.api
    state = {"revision": 100, "config": {"max_fps": 15}, "models": {}, "results": {},
             "lean_recording": {"enabled": False}}
    saved = []
    def handler(route):
        body = route.request.post_data_json
        saved.append(body)
        state["revision"] += 1
        state["lean_recording"].update(enabled=body["enabled"], file="D:\\用户数据\\前后倾对照\\记录.jsonl",
            valid=20, agree=15, disagree=5, invalid=3, agreement_percent=75, mismatch_seconds=.3,
            matrix={"forward": {"forward": 2, "neutral": 3, "back": 0}},
            recent_disagreements=[{"monocular": "neutral", "depth": "forward", "seconds": .3, "frames": 5}])
        api.fulfill(route, state)
    page.route("**/api/lean-comparison-recording", handler)
    page.route("**/api/performance", lambda route: api.fulfill(route, {"depth_comparison": state}))
    page.evaluate("s=>import('/js/depth-comparison.js').then(m=>m.renderDepthComparison(s))", state)
    assert not page.locator("#leanComparisonRecording").is_checked()
    page.check("#leanComparisonRecording")
    page.wait_for_function("!leanComparisonRecording.disabled && leanComparisonRecording.checked")
    assert saved == [{"enabled": True}]
    assert "75.0%" in page.locator("#leanComparisonRecordingCounts").text_content()
    assert "不可比较 3" in page.locator("#leanComparisonRecordingCounts").text_content()
    assert "单目中立 / 深度前倾" in page.locator("#leanComparisonRecordingRecent").text_content()
    page.uncheck("#leanComparisonRecording")
    page.wait_for_function("!leanComparisonRecording.disabled && !leanComparisonRecording.checked")
    assert saved[-1] == {"enabled": False}
    assert "已关闭" in page.locator("#leanComparisonRecordingState").text_content()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    assert not any(path in ("/api/input/source", "/api/output/config", "/api/sensitivity-experiments") and body
                   for path, body in api.requests)
    assert not desktop.errors


def test_forward_and_backward_thresholds_save_separately(desktop):
    seed(desktop)
    saved = install_save(desktop)
    page = desktop.page
    for selector, value, key in [("#monocularForwardThreshold", "6", "monocular_forward_on_deg"),
                                  ("#monocularBackThreshold", "3", "monocular_back_on_deg")]:
        page.locator(selector).fill(value)
        page.locator(selector).dispatch_event('change')
        wait_saved(page)
        assert saved[-1] == {key: float(value)}
    assert page.locator('#monocularForwardThresholdValue').text_content() == '6.0°'
    assert page.locator('#monocularBackThresholdValue').text_content() == '3.0°'
