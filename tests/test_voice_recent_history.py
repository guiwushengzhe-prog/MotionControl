"""语音记录只认执行成功；模拟分发，不采集设备或发送系统输出。"""
import copy

import pytest

from test_configuration_coordination import application  # noqa: F401


def test_failed_or_raised_voice_output_never_enters_recent_history(application, monkeypatch):
    app = application
    command = {"type": "keyboard", "target": "V", "phrase": "吃药"}
    monkeypatch.setattr(app.OUTPUT, "execute_voice_action", lambda action: {"executed": False, "reason": "output disabled"})
    assert not app.execute_voice_action(command)["executed"]
    assert not app.KERNEL.recent_triggers

    def fail(action):
        raise RuntimeError("模拟输出故障")

    monkeypatch.setattr(app.OUTPUT, "execute_voice_action", fail)
    with pytest.raises(RuntimeError, match="模拟输出故障"):
        app.execute_voice_action(command)
    assert not app.KERNEL.recent_triggers


def test_successful_game_system_override_is_recorded_once_with_effective_action(application, monkeypatch):
    app = application
    app.KERNEL.configure_bindings({"voice": {"game.profile_slot_01": {
        "action": {"type": "system", "target": "OUTPUT.STOP", "behavior": "tap"}}}})
    monkeypatch.setattr(app, "execute_system_target", lambda *args, **kwargs: {"executed": True})
    command = {"command_id": "game.profile_slot_01", "type": "keyboard", "target": "F1", "phrase": "暂停控制"}
    assert app.execute_voice_action(command)["executed"]
    events = list(app.KERNEL.recent_triggers)
    assert len(events) == 1
    assert events[0]["label"] == "暂停控制"
    assert events[0]["trigger"] == "voice.game.profile_slot_01"
    assert events[0]["action"] == {"type": "system", "target": "OUTPUT.STOP", "behavior": "tap"}


def test_voice_emergency_stop_keeps_its_single_record(application, monkeypatch):
    app = application
    monkeypatch.setattr(app, "emergency_stop_all", lambda: {"executed": True})
    assert app.execute_voice_action({"type": "system", "target": "EMERGENCY_STOP", "phrase": "紧急停止"})["executed"]
    assert len(app.KERNEL.recent_triggers) == 1
    assert app.KERNEL.recent_triggers[-1]["label"] == "紧急停止"


@pytest.mark.parametrize("scope", ["shared", "game"])
@pytest.mark.parametrize("executed", [False, True])
def test_queued_voice_mode_records_only_after_successful_switch(application, monkeypatch, scope, executed):
    app, queued = application, []
    command = {"type": "mode", "target": "NEXT", "phrase": "切换模式"}
    if scope == "game":
        app.KERNEL.configure_bindings({"voice": {"game.profile_slot_01": {"action": {
            "type": "mode", "target": "NEXT", "behavior": "tap"}}}})
        command.update(command_id="game.profile_slot_01", type="keyboard", target="F1")
    monkeypatch.setattr(app.SYSTEM_COMMANDS, "submit", lambda action: queued.append(copy.deepcopy(action)) or {"executed": False, "queued": True})
    monkeypatch.setattr(app, "switch_game_mode", lambda target: {"executed": executed})
    assert app.execute_voice_action(command)["queued"]
    assert not app.KERNEL.recent_triggers, "排队成功还不是切换成功"
    assert len(queued) == 1
    assert app._execute_queued_command(queued[0])["executed"] is executed
    events = list(app.KERNEL.recent_triggers)
    assert len(events) == int(executed)
    if executed:
        assert events[0]["label"] == "切换模式"
        assert events[0]["action"]["type"] == "mode"
        assert events[0]["action"]["target"] == "NEXT"


def test_body_mode_command_is_not_recorded_again_as_voice(application, monkeypatch):
    app = application
    action = {"type": "mode", "target": "NEXT", "trigger": "pose.hands_cross"}
    app.KERNEL.note_trigger("pose.hands_cross", action, label="双手交叉")
    monkeypatch.setattr(app, "switch_game_mode", lambda target: {"executed": True})
    assert app.execute_voice_action(action)["executed"]
    assert len(app.KERNEL.recent_triggers) == 1
    assert app.KERNEL.recent_triggers[-1]["trigger"] == "pose.hands_cross"
