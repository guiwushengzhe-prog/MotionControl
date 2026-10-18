"""识别提示与实际操作一致；全部使用模拟接口，不占用真实设备。"""
import copy

from playwright.sync_api import expect

from test_web_optimization import desktop, mock_browser


def render_running(desktop, *, stale=False):
    desktop.api.runtime["camera"].update(
        running=True, lifecycle="running", frames=30,
        last_frame_age_ms=9000 if stale else 0,
    )
    desktop.api.input["body_enabled"] = True
    desktop.page.evaluate("async()=>{await (await import('/js/play.js')).refreshKernel()}")


def test_stalled_camera_has_one_retry_and_waits_for_picture_before_pose(desktop):
    page, api = desktop.page, desktop.api
    render_running(desktop, stale=True)
    api.voice.update(audio_ready=False, last_error="input overflow")
    page.evaluate("async()=>{await (await import('/js/voice.js')).refreshVoice()}")
    expect(page.locator("#sourceStartBtn")).to_have_text("重试识别")
    expect(page.locator("#sourceStopBtn")).to_have_text("释放设备")
    expect(page.locator("#poseStepSub")).to_have_text("等待摄像头画面")
    expect(page.locator('[data-step="voice"]')).to_be_hidden()
    assert "input overflow" not in page.locator("#readyCard").inner_text()
    assert "站到镜头前" not in page.locator("#readyCard").inner_text()

    def reconnect(route):
        api.requests.append(("/api/input/source", route.request.post_data_json))
        api.runtime["camera"]["last_frame_age_ms"] = 0
        api.fulfill(route, api.runtime)

    page.route("**/api/input/source", reconnect)
    api.requests.clear()
    # 右侧写“重试识别”的入口应当直接执行，同一操作由后台先停再开。
    page.click("#cameraStepGo")
    expect(page.locator("#sourceStopBtn")).to_be_visible()
    expect(page.locator("#sourceStartBtn")).to_be_hidden()
    assert sum(path == "/api/input/source" for path, _ in api.requests) == 1
    assert not any(path == "/api/input/stop" for path, _ in api.requests)
    assert "识别已停止" not in page.locator("#notice").inner_text()


def test_stop_failure_is_not_success_and_only_offers_retry_stop(desktop):
    page, api = desktop.page, desktop.api
    render_running(desktop)

    def incomplete(route):
        api.fulfill(route, {
            **api.runtime, "output": {**api.output, "enabled": False},
            "recognition_stopped": False, "stop_error": "摄像头仍在释放中",
        })

    page.route("**/api/input/stop", incomplete)
    page.click("#sourceStopBtn")
    expect(page.locator("#notice")).to_contain_text("停止识别未完成")
    assert "识别已停止" not in page.locator("#notice").inner_text()
    expect(page.locator("#sourceStopBtn")).to_have_text("重试停止")
    expect(page.locator("#sourceStartBtn")).to_be_hidden()
    expect(page.locator("#mainActionBtn")).to_be_disabled()
    expect(page.locator("#hint b")).to_have_text("停止识别未完成")

    page.unroute("**/api/input/stop", incomplete)
    page.click("#sourceStopBtn")
    expect(page.locator("#notice")).to_contain_text("识别已停止，所有体感输出已松开")
    expect(page.locator("#sourceStartBtn")).to_have_text("开始识别")
    expect(page.locator("#sourceStopBtn")).to_be_hidden()
    expect(page.locator("#hint b")).to_have_text("识别已停止")
    expect(page.locator('[data-step="voice"]')).to_be_hidden()


def test_stale_poll_cannot_restore_running_after_confirmed_stop(desktop):
    page, api = desktop.page, desktop.api
    render_running(desktop)
    old_runtime = copy.deepcopy(api.runtime)
    held = []

    def delay_first_poll(route):
        if not held:
            held.append(route)
            page.evaluate("window.oldRecognitionPollHeld=true")
        else:
            # 停止之后的新轮询必须看到真实当前状态，只有这一次旧请求迟到。
            api.fulfill(route, api.runtime)

    page.route("**/api/kernel/status", delay_first_poll)
    with page.expect_request("**/api/kernel/status"):
        page.evaluate("async()=>{window.oldRecognitionPoll=(await import('/js/play.js')).refreshKernel()}")
    page.wait_for_function("window.oldRecognitionPollHeld===true")
    assert len(held) == 1
    page.click("#sourceStopBtn")
    expect(page.locator("#notice")).to_contain_text("识别已停止")
    api.fulfill(held[0], old_runtime)
    page.evaluate("window.oldRecognitionPoll")
    expect(page.locator("#sourceStartBtn")).to_have_text("开始识别")
    expect(page.locator("#sourceStopBtn")).to_be_hidden()
    expect(page.locator("#hint b")).to_have_text("识别已停止")


def test_starting_and_stopping_show_matching_disabled_action(desktop):
    page, api = desktop.page, desktop.api
    starts, stops = [], []
    page.route("**/api/input/source", lambda route: starts.append(route))
    page.click("#sourceStartBtn")
    expect(page.locator("#sourceStartBtn")).to_have_text("正在启动…")
    expect(page.locator("#sourceStartBtn")).to_be_disabled()
    expect(page.locator("#sourceStopBtn")).to_be_hidden()
    expect(page.locator("#poseStepSub")).to_have_text("等待摄像头画面")
    assert starts
    api.runtime["camera"].update(running=True, lifecycle="running", frames=1, last_frame_age_ms=0)
    api.input["body_enabled"] = True
    api.fulfill(starts[0], api.runtime)
    expect(page.locator("#sourceStopBtn")).to_be_visible()
    page.route("**/api/input/stop", lambda route: stops.append(route))
    page.click("#sourceStopBtn")
    expect(page.locator("#sourceStopBtn")).to_have_text("正在停止…")
    expect(page.locator("#sourceStopBtn")).to_be_disabled()
    expect(page.locator("#sourceStartBtn")).to_be_hidden()
    expect(page.locator("#hint b")).to_have_text("正在停止识别")
    assert stops
    api.runtime["camera"].update(running=False, lifecycle="stopped")
    api.input["body_enabled"] = False
    api.fulfill(stops[0], {**api.runtime, "output": api.output, "recognition_stopped": True})
    expect(page.locator("#sourceStartBtn")).to_have_text("开始识别")
    expect(page.locator("#sourceStartBtn")).to_be_enabled()


def test_retry_release_timeout_keeps_retry_stop_available(desktop):
    page, api = desktop.page, desktop.api
    render_running(desktop, stale=True)

    def timed_out(route):
        api.runtime["camera"]["lifecycle"] = "stopping"
        api.fulfill(route, {"error": "旧采集未完成释放", "recognition_stopped": False}, status=400)

    page.route("**/api/input/source", timed_out)
    page.click("#sourceStartBtn")
    expect(page.locator("#notice")).to_contain_text("旧采集未完成释放")
    page.evaluate("async()=>{await (await import('/js/play.js')).refreshKernel()}")
    expect(page.locator("#sourceStopBtn")).to_have_text("重试停止")
    expect(page.locator("#sourceStopBtn")).to_be_enabled()
    expect(page.locator("#sourceStartBtn")).to_be_hidden()
    expect(page.locator("#hint b")).to_have_text("停止识别未完成")
    page.click("#cameraStepGo")
    expect(page.locator("#sourceStartBtn")).to_have_text("开始识别")
    expect(page.locator("#sourceStopBtn")).to_be_hidden()
