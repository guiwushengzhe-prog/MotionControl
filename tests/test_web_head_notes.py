"""头控说明取实际校准噪声、当前滑杆设置和动作倍率，不改头控算法。"""
import pytest

from test_web_optimization import desktop, mock_browser  # noqa: F401


def render(page, *, algorithm="roll_tilt", deadzone=.1, noise=0, scale=1,
           calibrated=True, gamma=2, speed=200):
    return page.evaluate("""async values=>{
      const {head}=await import('/js/state.js');
      const {renderHeadCurve}=await import('/js/head-curve.js');
      Object.assign(head,{horizontalAlgorithm:values.algorithm,deadzone:values.deadzone,
        horizontalCurveGamma:values.gamma,sensitivityX:values.speed});
      viewHorizontalSource.value=values.algorithm;
      renderHeadCurve({horizontal_calibrated:values.calibrated,noise_tilt_deg:values.noise,
        motion_deadzone_scale:values.scale});
      return {threshold:headThresholdStatus.textContent,gamma:headCurveGammaStatus.textContent,
        summary:headCurveSummary.textContent,path:headCurveLine.getAttribute('d')};
    }""", {"algorithm": algorithm, "deadzone": deadzone, "noise": noise,
             "scale": scale, "calibrated": calibrated, "gamma": gamma, "speed": speed})


@pytest.mark.parametrize("deadzone,noise,angle,reason", [
    (.2, 0, "3.6", "调大更稳，调小更灵敏"),
    (.07, 1.56, "4.7", "当前受校准防抖限制，继续调低滑杆不会更灵敏"),
    (.03, 0, "2.0", "已到最低起转角度"),
    # 设置恰好等于噪声门槛或最低值时，调低也不会降低起转角度。
    (.2, 1.2, "3.6", "当前受校准防抖限制，继续调低滑杆不会更灵敏"),
    (2 / 18, 0, "2.0", "已到最低起转角度"),
])
def test_threshold_note_names_the_actual_limiting_factor(desktop, deadzone, noise, angle, reason):
    notes = render(desktop.page, deadzone=deadzone, noise=noise)
    assert notes["threshold"] == f"侧倾超过 {angle}° 开始转向；{reason}。"
    assert notes["summary"] == f"约 {angle}° 起转，18° 达到 200% 设定最高速度；灰色为起转前稳定区。"
    assert "滑竿" not in notes["threshold"]


def test_responsive_note_distinguishes_start_stop_and_motion_increase(desktop):
    notes = render(desktop.page, algorithm="head_responsive", deadzone=.2, scale=1.1)
    assert notes["threshold"] == "侧倾超过 6.4° 开始转向，回到 5.9° 内停止；调大更稳，调小更灵敏。动作防抖暂时提高起转角度。"
    ordinary = render(desktop.page, algorithm="head_responsive", deadzone=.2)
    assert "5.9° 开始转向，回到 5.4° 内停止" in ordinary["threshold"]
    assert "动作防抖" not in ordinary["threshold"]


def test_uncalibrated_or_missing_noise_never_claims_an_actual_angle(desktop):
    for status in ({"calibrated": False, "noise": 1.56},
                   {"calibrated": True, "noise": None}):
        notes = render(desktop.page, **status)
        assert notes["threshold"] == "校准后显示实际起转角度。"
        assert notes["summary"] == "校准后显示实际起转角度；灰色为起转前稳定区预览。"
        assert "°" not in notes["threshold"] + notes["summary"]
    # 校准状态变化也要刷新说明，不能被相同曲线坐标的绘图缓存挡住。
    ready = render(desktop.page, noise=0)
    assert "2.0°" in ready["threshold"]


def test_gamma_note_and_slider_preview_follow_the_current_values(desktop):
    linear = render(desktop.page, deadzone=.2, gamma=1)
    assert linear["gamma"] == "起转后，转动角度越大，速度按比例增加。"
    curved = render(desktop.page, deadzone=.2, gamma=2)
    assert curved["gamma"] == "小角度转得慢，大角度提速快；数值越大，前后差别越明显。最高速度不变。"
    assert curved["path"] != linear["path"]
    assert curved["summary"] == linear["summary"]
    notes = desktop.page.evaluate("""()=>{
      deadzone.value='25';deadzone.dispatchEvent(new Event('input',{bubbles:true}));
      return {threshold:headThresholdStatus.textContent,summary:headCurveSummary.textContent};
    }""")
    assert "4.5°" in notes["threshold"]
    assert "4.5°" in notes["summary"]
