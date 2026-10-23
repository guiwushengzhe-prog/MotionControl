"""实验开关接入持久化、单路控制和现有游戏绑定，不使用真实设备。"""
import copy
import json
import time

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.intent_library import _NullOutput
from motioncontrol_shared import pose_library


class Output(_NullOutput):
    def __init__(self):
        self.actions = []

    def set_action_holds(self, holds, **kwargs):
        self.actions = copy.deepcopy(holds)


@pytest.fixture
def kernel():
    instance = ControlKernel(Output())
    yield instance
    instance.close()


def test_default_off_does_not_write_configuration_and_switches_survive_restart(kernel):
    assert not kernel._general_settings_path().exists()
    assert not kernel.sensitivity_experiments["lean_forward"]
    kernel._update_lean_motions_locked(time.monotonic())
    assert not kernel._general_settings_path().exists()
    kernel.configure_sensitivity_experiments({"head_hold": True, "first_step": True, "body_source": "depth", "lean_forward": True})
    assert kernel.head_controller.hfhpe_hold
    assert kernel.status()["sensitivity_experiments"]["body"]["source"] == "depth"
    restored = ControlKernel(Output())
    try:
        assert restored.sensitivity_experiments == kernel.sensitivity_experiments
        assert restored.head_controller.hfhpe_hold
        assert "hfhpe_hold" not in restored.head_controller.profile_document()["params"]
    finally:
        restored.close()


@pytest.mark.parametrize("changes", [{"head_hold": "false"}, {"body_source": "auto"}, {"unknown": True}])
def test_invalid_config_has_no_file_or_runtime_side_effect(kernel, changes):
    previous = dict(kernel.sensitivity_experiments)
    with pytest.raises((ValueError, TypeError)):
        kernel.configure_sensitivity_experiments(changes)
    assert kernel.sensitivity_experiments == previous
    assert not kernel._general_settings_path().exists()


def test_failed_save_retains_the_previous_runtime_and_file(kernel, monkeypatch):
    kernel.configure_sensitivity_experiments({"compare": True})
    previous = kernel._general_settings_path().read_bytes()
    def fail(*args):
        raise OSError("磁盘不可写")
    monkeypatch.setattr("motioncontrol.control_kernel.atomic_bytes", fail)
    with pytest.raises(OSError):
        kernel.configure_sensitivity_experiments({"head_hold": True})
    assert not kernel.head_controller.hfhpe_hold
    assert not kernel.sensitivity_experiments["head_hold"]
    assert kernel._general_settings_path().read_bytes() == previous


def test_new_lean_actions_require_explicit_bindings(kernel):
    kernel.configure_bindings({"motions": {"march": {"action": {"type": "keyboard", "target": "W", "behavior": "hold"}},
                                           "calf_back": {"action": {"type": "keyboard", "target": "Z", "behavior": "hold"}}}})
    effective = kernel.effective_bindings()
    assert not set(effective).intersection({"motion." + ident for ident in ("lean_left", "lean_right", "lean_back", "lean_forward")})
    assert effective["motion.calf_back"]["action"]["target"] == "Z"
    kernel.configure_bindings({"motions": {"march": {"action": {"type": "gamepad_axis", "target": "LS_UP"}},
                                           "lean_left": {"action": {"type": "keyboard", "target": "Q"}},
                                           "lean_forward": {"action": {"type": "keyboard", "target": "UP"}},
                                           "lean_right": {"disabled": True}}})
    assert kernel.effective_bindings()["motion.lean_left"]["action"]["target"] == "Q"
    assert "motion.lean_right" not in kernel.effective_bindings()
    assert "motion.lean_back" not in kernel.effective_bindings()
    assert kernel.effective_bindings()["motion.lean_forward"]["action"]["target"] == "UP"
    kernel.configure_bindings({"motions": {"march": {"action": {"type": "keyboard", "target": "F"}}}})
    assert "motion.lean_back" not in kernel.effective_bindings()
    assert all(pose_library.get(ident) for ident in ("lean_left", "lean_right", "lean_back", "lean_forward"))


def test_forward_disable_releases_existing_motion_and_old_settings_default_off(kernel, monkeypatch):
    kernel.configure_sensitivity_experiments({"lean_forward": True})
    kernel.motion_active.add("lean_forward")
    kernel.configure_sensitivity_experiments({"lean_forward": False})
    assert "lean_forward" not in kernel.motion_active
    from motioncontrol.sensitivity_experiments import load
    assert not load({"lean_back": True})["lean_forward"]


def test_forward_switch_survives_recording_snapshot_and_old_replay_defaults_off(kernel):
    from motioncontrol.intent_library import make_replay_kernel
    kernel.configure_sensitivity_experiments({"lean_forward": True})
    snapshot = kernel.replay_snapshot()
    replay = make_replay_kernel(snapshot)
    try:
        assert replay.sensitivity_experiments["lean_forward"]
    finally:
        replay.close()
    del snapshot["sensitivity_experiments"]["lean_forward"]
    replay = make_replay_kernel(snapshot)
    try:
        assert not replay.sensitivity_experiments["lean_forward"]
    finally:
        replay.close()


def test_legacy_source_is_preserved_but_independent_axis_choices_survive_body_source_updates(kernel):
    from motioncontrol.sensitivity_experiments import load
    legacy = load({"body_source": "depth"})
    assert legacy["side_source"] == legacy["fore_aft_source"] == "depth"
    kernel.configure_sensitivity_experiments({"body_source": "depth"})
    assert kernel.sensitivity_experiments["side_source"] == "depth"
    kernel.configure_sensitivity_experiments({"side_source": "model"})
    kernel.configure_sensitivity_experiments({"body_source": "model"})
    assert kernel.sensitivity_experiments["side_source"] == "model"
    assert kernel.sensitivity_experiments["fore_aft_source"] == "depth"
    restored = ControlKernel(Output())
    try:
        assert restored.sensitivity_experiments == kernel.sensitivity_experiments
        body = restored.status()["sensitivity_experiments"]["body"]
        assert body["source"] == "mixed"
        assert body["side_source"] == "model" and body["fore_aft_source"] == "depth"
    finally:
        restored.close()


def test_back_suppresses_only_forward_and_can_combine_with_sideways(kernel, monkeypatch):
    kernel.configure_bindings({"motions": {
        "march": {"action": {"type": "keyboard", "target": "W", "behavior": "hold"}},
        "lean_back": {"action": {"type": "keyboard", "target": "S", "behavior": "hold"}},
    }})
    monkeypatch.setattr(kernel._body_lean, "update", lambda *args, **kwargs: {"lean_right", "lean_back"})
    kernel.motion_active = {"march", "jump"}
    kernel._update_lean_motions_locked(time.monotonic())
    assert kernel.motion_active == {"jump", "lean_right", "lean_back"}
    kernel.configure_sensitivity_experiments({"lean_strafe": True, "lean_back": True})
    kernel.configure_sensitivity_experiments({"lean_strafe": False, "lean_back": False})
    assert kernel.motion_active == {"jump"}


def test_monocular_holds_and_releases_without_depth_and_rejects_old_revision(kernel):
    kernel.configure_sensitivity_experiments({"fore_aft_source": "monocular", "lean_forward": True, "lean_back": True})
    kernel.configure_bindings({"motions": {"march": {"action": {"type": "keyboard", "target": "W", "behavior": "hold"}}}})
    at = time.monotonic()
    revision = kernel._monocular_neutral_revision
    for angle, expected in [(-12., "lean_forward"), (12., "lean_back"), (0., None)]:
        for _ in range(12):
            at += .05
            kernel.pose_sample_at = at
            kernel.publish_monocular_lean({"valid": True, "calibrated": True, "sample_at": at, "angle_deg": angle}, revision)
            kernel._update_lean_motions_locked(at)
        assert kernel.motion_active.intersection({"lean_forward", "lean_back"}) == ({expected} if expected else set())
    assert kernel.latest_depth_body is None
    kernel.publish_monocular_lean({"valid": True, "calibrated": True, "sample_at": at, "angle_deg": -12.}, revision)
    kernel._update_lean_motions_locked(at)
    kernel.pose_sample_at = at + .4
    kernel._update_lean_motions_locked(at + .4)
    assert not kernel.motion_active.intersection({"lean_forward", "lean_back"})
    kernel.start_lean_neutral()
    kernel.publish_monocular_lean({"valid": True, "calibrated": True, "sample_at": at + .4, "angle_deg": -12.}, revision)
    assert kernel._latest_monocular_lean is None
    assert kernel.sensitivity_experiments["side_source"] == kernel.sensitivity_experiments["body_source"] == "model"


def test_manual_depth_selection_reports_missing_and_clears_first_step_state(kernel):
    kernel.configure_march_algorithm("responsive")
    kernel.configure_sensitivity_experiments({"first_step": True, "body_source": "depth", "lean_back": True})
    state = kernel.status()["sensitivity_experiments"]
    assert state["body"]["source"] == state["first_step_source"] == "depth"
    assert not state["first_step_ready"]
    assert not state["body"]["sources"]["depth"]["valid"]
    assert not state["body"]["active"]
    assert not kernel.motion_active


def test_delayed_phone_sample_cannot_drive_experiments_while_status_says_stale(kernel, monkeypatch):
    kernel.configure_sensitivity_experiments({"lean_strafe": True})
    now = time.monotonic()
    kernel.pose_sample_at = now - .4
    kernel.latest_model_pose = {"test": "old pose"}
    observed = []
    def update(image, world, depth, width, height, at, **kwargs):
        observed.append((image, world, depth))
        return {"lean_left"} if image else set()
    monkeypatch.setattr(kernel._body_lean, "update", update)
    kernel._update_lean_motions_locked(now)
    assert observed == [(None, None, None)]
    assert not kernel.motion_active


def test_home_calibration_resets_all_body_sources_and_rejects_old_monocular_results(kernel):
    kernel.configure_sensitivity_experiments({"side_source": "model", "fore_aft_source": "monocular",
                                             "lean_strafe": True, "lean_forward": True, "lean_back": True})
    previous_config = copy.deepcopy(kernel.sensitivity_experiments)
    previous_revision = kernel._monocular_neutral_revision
    kernel.motion_active.update(("jump", "lean_forward", "lean_left"))
    kernel.motion_raw.update(lean_forward=True, lean_left=True)
    kernel.publish_monocular_lean({"valid": True, "calibrated": True, "angle_deg": -15,
                                   "sample_at": time.monotonic()}, previous_revision)
    state = kernel.start_calibration()
    assert state["head"]["calibrating"]
    assert state["head"]["calibrates_body"]
    assert kernel.sensitivity_experiments == previous_config
    assert kernel._monocular_neutral_revision == previous_revision + 1
    assert state["sensitivity_experiments"]["monocular"] is None
    assert kernel.motion_active == {"jump"}
    assert not any(kernel.motion_raw[key] for key in ("lean_left", "lean_right", "lean_forward", "lean_back"))
    for source in ("model", "depth"):
        status = state["sensitivity_experiments"]["body"]["sources"][source]
        assert status["side_calibrating"] and status["back_calibrating"]
        assert status["active"] == []
    kernel.publish_monocular_lean({"valid": True, "angle_deg": -15}, previous_revision)
    assert kernel._latest_monocular_lean is None
    kernel.start_lean_neutral()
    assert kernel._monocular_neutral_revision == previous_revision + 2


@pytest.mark.parametrize("trigger", ["motion.lean_forward", "motion.lean_back", "motion.future_action",
                                     "pose.future_pose", "zone.headJump"])
def test_unbound_profile_controls_never_inherit_or_output(trigger, kernel):
    kernel.configure_motions([{"id": "lean_forward", "enabled": True, "type": "keyboard", "target": "W"}])
    kernel.configure_bindings({"motions": {"march": {"action": {"type": "keyboard", "target": "W"}}}})
    prefix, ident = trigger.split(".", 1)
    {"motion": kernel.motion_active, "pose": kernel.pose_active}.get(prefix, set()).add(ident)
    if prefix == "zone":
        kernel.zone_state[ident]["pressed"] = True
    kernel._dispatch_controls_locked(time.monotonic())
    assert trigger not in kernel.effective_bindings()
    assert not kernel._trigger_mapped_locked(trigger)
    assert not kernel._mapped_triggers_locked({trigger})
    assert trigger not in kernel._bound_action_triggers_locked()
    assert not any(action["id"] == trigger for action in kernel.output.actions)


def test_explicit_new_action_binding_is_shared_by_dispatch_and_zone_conflicts(kernel):
    kernel.configure_bindings({"motions": {"lean_forward": {"action": {"type": "keyboard", "target": "W"}}}})
    kernel.motion_active.add("lean_forward")
    kernel._dispatch_controls_locked(time.monotonic())
    assert kernel._mapped_triggers_locked({"motion.lean_forward"}) == {"motion.lean_forward"}
    assert "motion.lean_forward" in kernel._bound_action_triggers_locked()
    assert kernel.output.actions == [{"id": "motion.lean_forward", "action": {"type": "keyboard", "target": "W", "behavior": "hold"}}]


def test_head_only_recenter_keeps_body_neutral_revision(kernel):
    before = kernel._monocular_neutral_revision
    kernel.set_current_center()
    assert kernel.head_controller.calibrating
    assert not kernel.status()["head"]["calibrates_body"]
    assert kernel._monocular_neutral_revision == before


def test_missing_macro_is_not_mapped_for_actions_output_or_phone_feedback(kernel):
    kernel.configure_bindings({"motions": {"lean_forward": {"action": {"type": "macro", "target": "missing"}}}})
    kernel.motion_active.add("lean_forward")
    kernel._dispatch_controls_locked(time.monotonic())
    assert "motion.lean_forward" not in kernel.effective_bindings()
    assert "motion.lean_forward" not in kernel._bound_action_triggers_locked()
    assert not kernel._mapped_triggers_locked({"motion.lean_forward"})
    assert not kernel.output.actions
