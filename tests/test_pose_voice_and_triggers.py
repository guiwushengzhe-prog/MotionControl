"""录姿势能用嘴按、触发实况看得见、个人语音不跟着分享走。

三件事看着不相干，其实是同一个毛病的三面：**发生了什么，人看不见**。

* 录姿势的按钮只能用手点，而人正站在镜头前几米外摆姿势，够不着鼠标。
* 一个动作到底有没有触发、按的是哪个键，以前没地方看——区域按下去十几毫秒就松开，
  姿势是边沿触发，语音说完就完，盯着状态是抓不到的。
* 装一份别人分享的语音配置会顺手把自己的唤醒词换掉，而界面上没有任何提示。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from motioncontrol_shared.canonical import canonicalize
from motioncontrol_shared.describe import VOICE_SYSTEM_NAMES
from motioncontrol_shared.sync_allowlist import CLASSIFICATION, CLOUD_SYNC_ALLOWLIST
from motioncontrol_shared.user_layout import USER_DATA_FILES

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


# --- 个人语音不跟着分享走 ---------------------------------------------------

def test_the_wake_word_lives_in_its_own_file():
    assert "personal_voice" in USER_DATA_FILES
    assert CLASSIFICATION.get("personal_voice")


def test_the_wake_word_is_never_in_a_cloud_bundle():
    """分享语音配置是在分享"说什么话按什么键"，不是把自己的唤醒词装到别人机器上。"""
    assert "personal_voice" not in CLOUD_SYNC_ALLOWLIST


def test_a_shared_voice_document_has_no_wake_word():
    data = canonicalize("voice_mappings", {
        "wake_word": "别人的唤醒词",
        "emergency_stop_phrases": ["随便"],
        "mappings": [{"phrase": "开始", "type": "keyboard", "target": "A"}],
    }).data
    assert "wake_word" not in data
    assert "emergency_stop_phrases" not in data


def test_installing_a_shared_config_does_not_touch_the_wake_word():
    """装的人只会发现"我的唤醒词自己变了"，根本想不到是装配置装的。"""
    start = SERVER.index('elif remote.doc_type == "voice_mappings":')
    block = SERVER[start:start + 500]
    assert "wake_word=" not in block, "装别人的配置时还在覆盖唤醒词"
    assert "emergency_stop_phrases=" not in block


def test_the_personal_settings_have_their_own_place_on_the_page():
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="wakeWord"' in page
    assert "不分享" in page, "界面上要说清楚它不会跟着配置发出去"


def test_a_saved_wake_word_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(tmp_path))
    from motioncontrol.voice_backend import VoiceService

    first = VoiceService(ROOT, lambda action: {"executed": True})
    first.configure(first.mappings, wake_word="小助手")
    assert (tmp_path / "personal_voice.json").is_file()

    second = VoiceService(ROOT, lambda action: {"executed": True})
    assert second.wake_word == "小助手"


def test_an_old_installation_keeps_its_wake_word_when_the_file_splits(tmp_path, monkeypatch):
    """老安装里这两项还在 voice_mappings.json 里。搬家对用户必须是无感的。"""
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(tmp_path))
    (tmp_path / "voice_mappings.json").write_text(json.dumps({
        "wake_word": "老唤醒词",
        "emergency_stop_phrases": ["紧急停止", "快停下"],
        "mappings": [{"phrase": "开始", "type": "keyboard", "target": "A", "behavior": "tap"}],
    }, ensure_ascii=False), encoding="utf-8")

    from motioncontrol.voice_backend import VoiceService
    service = VoiceService(ROOT, lambda action: {"executed": True})
    assert service.wake_word == "老唤醒词"
    assert "快停下" in service.emergency_stop_phrases
    assert [item["phrase"] for item in service.mappings] == ["开始"]
    assert (tmp_path / "personal_voice.json").is_file(), "搬完要落盘，不然每次启动都搬一遍"


def test_the_built_in_phrases_follow_the_wake_word(tmp_path, monkeypatch):
    """内置口令表里写死的是默认唤醒词。改完唤醒词它们还卡在旧的上面，就等于截图、
    校准、开关输出全都叫不动了——而界面上还好端端地列着它们。"""
    monkeypatch.setenv("MOTIONCONTROL_USER_DIR", str(tmp_path))
    from motioncontrol.voice_backend import VoiceService

    service = VoiceService(ROOT, lambda action: {"executed": True})
    if not service.command_registry:
        pytest.skip("这台机器上没有内置口令表")
    service.configure(service.mappings, wake_word="小助手")
    phrases = service.grammar_phrases()
    assert any(phrase.startswith("小助手") for phrase in phrases)
    assert not any(phrase.startswith("体感") for phrase in phrases), "还有口令卡在旧唤醒词上"


# --- 录姿势能用嘴按 ---------------------------------------------------------

def test_a_custom_phrase_may_point_at_the_pose_commands():
    """校验器有一份系统命令白名单。不加进去的话，界面上选得到、一保存就被退回来，
    而那句报错跟用户做的事情对不上号。"""
    from motioncontrol_shared.mapping_schema import normalize_voice_mappings

    for target in ("POSE.RECORD", "POSE.ADD_FRAME", "POSE.CANCEL"):
        result = normalize_voice_mappings([
            {"phrase": "录一下", "type": "system", "target": target}])
        assert result[0]["target"] == target


def test_recording_a_pose_has_a_voice_command():
    for target in ("POSE.RECORD", "POSE.ADD_FRAME", "POSE.CANCEL"):
        assert target in VOICE_SYSTEM_NAMES, f"{target} 没有人看得懂的名字"
        assert f'"{target}"' in SERVER or f"'{target}'" in SERVER, f"{target} 没有人接"
    assert "POSE.RECORD" in APP_JS, "界面上没地方把口令指到它"


def test_the_shipped_phrase_list_and_the_generated_map_agree():
    """生成的那份是识别器真正读的。只改源文件，口令就永远不会被听见。"""
    registry = json.loads((ROOT / "config" / "voice_commands_v094.json").read_text(encoding="utf-8"))
    generated = json.loads(
        (ROOT / "config" / "generated_voice" / "voice_action_map.json").read_text(encoding="utf-8-sig"))
    ids = {item["id"] for item in registry["commands"]}
    assert {item["id"] for item in generated.values()} == ids
    assert {"pose.record", "pose.add_frame", "pose.cancel"} <= ids


def test_the_countdown_runs_on_the_server_not_in_the_page():
    """按钮和口令必须走同一条路。两套倒计时迟早对不上。"""
    assert "PoseCaptureTimer(_capture_custom_pose)" in SERVER
    assert '/api/pose/custom/schedule' in SERVER and '/api/pose/custom/schedule' in APP_JS
    assert "let customPoseCountdown" not in APP_JS, "页面里还留着第二套倒计时"


# --- 倒计时本身 -------------------------------------------------------------

def timer(captured, clock=None):
    from motioncontrol.pose_capture import PoseCaptureTimer

    def capture(purpose, pose_id, name):
        captured.append((purpose, pose_id, name))
        return {"name": name or "无名"}

    return PoseCaptureTimer(capture) if clock is None else PoseCaptureTimer(capture, clock=clock)


def test_the_countdown_fires_once_and_says_what_it_recorded():
    captured = []
    probe = timer(captured)
    probe.arm(purpose="capture", delay_s=1, name="双手交叉")
    assert probe.status()["counting"] is True
    probe.wait()
    assert captured == [("capture", "", "双手交叉")]
    assert "双手交叉" in probe.status()["message"]


def test_cancelling_means_nothing_is_recorded():
    captured = []
    probe = timer(captured)
    probe.arm(purpose="capture", delay_s=2)
    probe.cancel()
    probe.wait()
    assert captured == []
    assert probe.status()["counting"] is False
    assert probe.status()["message"] == "已取消"


def test_arming_again_replaces_the_first_countdown():
    """两条线程先后到点会拍两张，而人只摆了一个姿势——多出来那一条要等他
    自己发现再删。"""
    captured = []
    probe = timer(captured)
    probe.arm(purpose="capture", delay_s=1, name="第一次")
    probe.arm(purpose="capture", delay_s=1, name="第二次")
    probe.wait()
    import time as _time
    _time.sleep(0.4)
    assert captured == [("capture", "", "第二次")]


def test_adding_a_frame_needs_something_to_add_it_to():
    probe = timer([])
    with pytest.raises(ValueError, match="先录一个"):
        probe.arm(purpose="frame", delay_s=1)


def test_a_failed_capture_says_why_instead_of_dying_quietly():
    """定时线程里抛出去没人接得住。错误得留在状态里，页面才能原样转述。"""
    from motioncontrol.pose_capture import PoseCaptureTimer

    def capture(purpose, pose_id, name):
        raise ValueError("还没有看到人")

    probe = PoseCaptureTimer(capture)
    probe.arm(purpose="capture", delay_s=1)
    probe.wait()
    assert probe.status()["message"] == "还没有看到人"
    assert probe.status()["counting"] is False


def test_the_delay_is_clamped():
    from motioncontrol.pose_capture import MAX_POSE_DELAY_S, MIN_POSE_DELAY_S

    probe = timer([])
    assert probe.arm(purpose="capture", delay_s=9999)["remaining_s"] <= MAX_POSE_DELAY_S
    probe.cancel()
    assert probe.arm(purpose="capture", delay_s=-5)["remaining_s"] >= MIN_POSE_DELAY_S - 0.2
    probe.cancel()


def test_the_preparation_time_is_remembered():
    """语音触发时没人能替它去读界面上那个下拉框。"""
    assert 'remember_general_setting("pose_capture_delay_s"' in SERVER
    assert 'general_setting("pose_capture_delay_s"' in SERVER


def test_the_remembered_time_comes_back_to_the_page():
    """不送回去的话，界面每次打开都回到默认值，而下一次点按钮又把这个默认值
    存回服务端——记住就等于没记。"""
    assert '"delay_s": KERNEL.general_setting("pose_capture_delay_s"' in SERVER
    assert "data.delay_s" in APP_JS


# --- 触发实况 ---------------------------------------------------------------

def test_the_kernel_records_what_just_fired():
    from motioncontrol.control_kernel import ControlKernel

    class Output:
        def set_action_holds(self, holds, source_group="controls"):
            return {}

        def set_buttons(self, *a, **k):
            return {}

        def set_holds(self, *a, **k):
            return {}

        def apply(self, *a, **k):
            return {}

        def status(self):
            return {}

    kernel = ControlKernel(Output())
    kernel.note_trigger("voice.ui.menu", {"type": "keyboard", "target": "ESC", "behavior": "tap"})
    recent = kernel.status()["recent_triggers"]
    assert recent[-1]["trigger"] == "voice.ui.menu"
    assert recent[-1]["action"]["target"] == "ESC"
    assert "now" in kernel.status(), "没有服务端的钟，界面换算不出「几秒前」"


def test_the_log_does_not_grow_without_bound():
    """它是给人看的，不是日志。一直按着的时候每帧记一条会把有用的那几条冲光。"""
    from motioncontrol.control_kernel import ControlKernel

    class Output:
        def set_action_holds(self, *a, **k):
            return {}

        def set_buttons(self, *a, **k):
            return {}

        def set_holds(self, *a, **k):
            return {}

        def apply(self, *a, **k):
            return {}

        def status(self):
            return {}

    kernel = ControlKernel(Output())
    for index in range(200):
        kernel.note_trigger(f"zone.z{index}", None)
    assert len(kernel.status()["recent_triggers"]) <= 32


def test_only_the_rising_edge_is_recorded():
    """按住不放的时候每帧记一条，一秒就是三十条，真正有用的那几条立刻被顶掉。"""
    source = (ROOT / "motioncontrol" / "control_kernel.py").read_text(encoding="utf-8")
    start = source.index("def _dispatch_controls_locked")
    block = source[start:start + 3000]
    assert "active - self.trigger_previous" in block, "记的不是上升沿"


def test_the_kernel_reports_what_will_actually_fire():
    """区域和动作有一层内置兜底：配置里没有 zone.headJump 这一条时，它照样按 A。

    界面直接读 config 会把它写成「未映射」，而人在游戏里明明被按了一个键。
    这种"界面说没绑、实际有反应"最难查，因为两边都不报错。2026-09-22 就是在
    动作测试页上看见大字写着「头顶区 → A」、旁边的靶子写着「未映射」才发现的。
    """
    from motioncontrol.control_kernel import ControlKernel

    class Output:
        def set_action_holds(self, *a, **k):
            return {}

        def set_buttons(self, *a, **k):
            return {}

        def set_holds(self, *a, **k):
            return {}

        def apply(self, *a, **k):
            return {}

        def status(self):
            return {}

    kernel = ControlKernel(Output())
    kernel.configure_bindings({})
    status = kernel.status()
    assert "effective_bindings" in status, "内核没把真正会生效的那份报出来"
    effective = status["effective_bindings"]
    # 配置是空的，但内置兜底让区域照样按键。
    assert not status["control_bindings"], "前提变了：这一步应该是空配置"
    assert effective, "空配置下也该有兜底的区域绑定"
    assert any(key.startswith("zone.") for key in effective)


def test_the_page_shows_the_effective_binding_not_the_config():
    """圈上、姿势卡片上、动作测试页里都走同一个取法，否则三处会各说各的。"""
    # 只看真正的代码行：注释里提到 control_bindings 是在解释为什么不该读它。
    code = [line for line in APP_JS.splitlines() if not line.strip().startswith("//")]
    hits = [line.strip() for line in code if "control_bindings" in line]
    # 只允许一处：bindingsForDisplay 自己的兑底。别处再读一遍就是第二套规则，
    # 而两套规则早晚会说两样话。
    assert len(hits) == 1, f"还有地方直接读配置来显示键位：{hits}"
    assert "effective_bindings" in hits[0]


def test_the_range_is_its_own_tab_and_shows_the_hit_big():
    """人站在几米外做动作，小字等于没有。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-panel="range"' in page and 'data-view="range"' in page
    assert "renderRange" in APP_JS
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    hit = css[css.index(".range-hit-what"):css.index(".range-hit-what") + 120]
    size = float(re.search(r"font-size:([\d.]+)em", hit).group(1))
    assert size >= 2.0, f"命中那行字只有 {size}em，几米外看不见"


def test_an_unmapped_target_still_shows_up_in_the_range():
    """动作亮了、键位写着未映射，说明识别是好的只是没绑——这是这一页最有用的
    一条信息，因为它和"根本没识别出来"在游戏里长得一模一样。"""
    assert "'未映射'" in APP_JS
    assert "unmapped" in (ROOT / "web" / "app.css").read_text(encoding="utf-8")


def test_the_live_view_sits_next_to_the_editor():
    """看到「左手区 → Y」不对，往下一眼就是改它的那一行。分两个地方只会让人来回找。"""
    page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    live = page.index('id="triggerLive"')
    table = page.index('id="profileBindingRows"')
    assert live < table, "触发实况跑到映射表下面去了"
    assert "renderTriggerLive" in APP_JS
    assert "revealBindingRow" in APP_JS, "点一条要能跳到它的映射行"
