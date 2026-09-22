from motioncontrol.hold_chain import (
    CHARGING,
    CONTINUE,
    DEFAULT_ACTION_CHAIN,
    IDLE,
    PREPARE,
    RELEASED,
    HoldChain,
)


def config(**updates):
    value = dict(DEFAULT_ACTION_CHAIN)
    value["chains"] = [dict(DEFAULT_ACTION_CHAIN["chains"][0])]
    value.update({"enabled": True, **updates})
    return value


def signals(start=False, squat=False):
    return {
        "zone.headJump": start,
        "motion.squat": squat,
        "motion.stand": not squat,
    }


def test_default_disabled_is_idle_and_has_no_hold():
    chain = HoldChain()
    result = chain.update(10.0, signals(start=True))
    assert result.state == IDLE
    assert result.hold is False


def test_head_jump_enters_prepare_then_becomes_ordinary_release():
    chain = HoldChain(config(prepare_s=0.16, continue_window_s=0.30))
    assert chain.update(1.0, signals(start=True)).state == PREPARE
    assert chain.update(1.10, signals(start=True)).hold is True
    assert chain.update(1.17, signals(start=True)).state == CONTINUE
    assert chain.update(1.45, signals(start=True)).state == CONTINUE
    assert chain.update(1.47, signals(start=True)).state == RELEASED
    assert chain.update(1.48, signals()).state == IDLE


def test_squat_during_prepare_and_continue_enters_charging():
    chain = HoldChain(config(prepare_s=0.16, continue_window_s=0.30))
    chain.update(2.0, signals(start=True))
    result = chain.update(2.08, signals(start=True, squat=True))
    assert result.state == CHARGING and result.hold is True
    # The stand edge immediately releases the held jump.
    result = chain.update(2.20, signals(start=True, squat=False))
    assert result.state == RELEASED and result.hold is False


def test_squat_in_post_prepare_window_restarts_hold():
    chain = HoldChain(config(prepare_s=0.10, continue_window_s=0.30))
    chain.update(3.0, signals(start=True))
    assert chain.update(3.11, signals(start=True)).state == CONTINUE
    result = chain.update(3.20, signals(start=True, squat=True))
    assert result.state == CHARGING and result.hold is True
    assert chain.update(3.70, signals(start=True, squat=True)).hold is True


def test_repeated_trigger_does_not_restart_until_start_leaves():
    chain = HoldChain(config())
    chain.update(4.0, signals(start=True))
    chain.update(4.20, signals(start=True))
    assert chain.update(4.60, signals(start=True)).state == RELEASED
    assert chain.update(4.70, signals(start=True)).state == RELEASED
    assert chain.update(4.71, signals()).state == IDLE
    assert chain.update(4.80, signals(start=True)).state == PREPARE


def test_reset_releases_and_rearms_at_any_frame_rate():
    chain = HoldChain(config(prepare_s=0.12, continue_window_s=0.20))
    chain.update(5.0, signals(start=True))
    assert chain.update(5.125, signals(start=True)).hold is False
    chain.reset()
    assert chain.result().state == IDLE and chain.result().hold is False
    assert chain.update(5.126, signals(start=True)).state == PREPARE


def test_bad_configuration_is_disabled_safely():
    chain = HoldChain({"enabled": True, "chains": "not-a-list"})
    assert chain.enabled is False
    assert chain.update(1.0, signals(start=True)).hold is False


def test_custom_declarative_chain_uses_different_signals():
    custom = {
        "enabled": True,
        "prepare_s": 0.1,
        "continue_window_s": 0.2,
        "chains": [{
            "id": "gate_hold_release",
            "start": "zone.gate",
            "continue_condition": "motion.arm",
            "sustain_condition": "motion.arm",
            "end_condition": "motion.release",
            "hold_action": "zone.gate",
        }],
    }
    chain = HoldChain(custom)
    assert chain.update(1.0, {"zone.gate": True}).state == PREPARE
    assert chain.update(1.11, {"zone.gate": True}).state == CONTINUE
    result = chain.update(1.15, {"zone.gate": True, "motion.arm": True})
    assert result.state == CHARGING and result.hold_action == "zone.gate"
    assert chain.update(1.20, {"zone.gate": True, "motion.release": True}).state == RELEASED
