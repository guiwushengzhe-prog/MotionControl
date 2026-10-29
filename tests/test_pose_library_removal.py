"""本地动作库删除与恢复：持久化、实时松键及回放保持同一可用性。"""
import copy

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol.intent_library import make_replay_kernel
from motioncontrol.pose_downloads import PoseActionStore, PoseDownloadError
from motioncontrol_shared import pose_library
from test_alternate_body_action import Output
from test_configuration_coordination import application
from test_game_modes import get, post
from test_minimal_controls import _standing_pose


@pytest.mark.parametrize("ident", ["march", "calf_back", "jump"])
def test_builtin_removal_and_offline_restore_survive_restart(tmp_path, ident):
    path = tmp_path / "pose_actions.json"
    store = PoseActionStore(path)
    assert store.remove(ident)
    assert not store.remove(ident)
    again = PoseActionStore(path)
    assert again.disabled_builtin == {ident}
    again.restore_builtin(ident)
    assert PoseActionStore(path).disabled_builtin == set()
    with pytest.raises(PoseDownloadError, match="不是自带"):
        again.restore_builtin("squat")


def test_failed_builtin_write_keeps_current_availability(tmp_path, monkeypatch):
    store = PoseActionStore(tmp_path / "pose_actions.json")
    store.remove("jump")
    monkeypatch.setattr(store, "_save", lambda: (_ for _ in ()).throw(OSError("写入失败")))
    with pytest.raises(OSError):
        store.remove("march")
    assert store.disabled_builtin == {"jump"}
    with pytest.raises(OSError):
        store.restore_builtin("jump")
    assert store.disabled_builtin == {"jump"}


@pytest.mark.parametrize("ident", ["march", "calf_back", "jump"])
def test_removing_builtin_releases_its_hold_preserves_other_keys_and_saved_mapping(ident):
    output = Output()
    kernel = ControlKernel(output)
    bindings = {"motions": {ident: {"action": {"type": "keyboard", "target": "W", "behavior": "hold"}}},
                "zones": {"leftHand": {"action": {"type": "keyboard", "target": "E", "behavior": "hold"}}}}
    try:
        kernel.configure_bindings(bindings)
        with kernel._lock:
            kernel.motion_active = {ident}
            kernel.motion_raw[ident] = True
            kernel.zone_state["leftHand"]["pressed"] = True
            kernel._dispatch_controls_locked(1.)
        assert {item["action"]["target"] for item in output.holds} >= {"W", "E"}
        saved = copy.deepcopy(kernel.control_bindings)
        kernel.configure_pose_actions([], [ident])
        assert ident not in kernel.motion_active
        assert not kernel.motion_raw[ident]
        assert {item["action"]["target"] for item in output.holds} == {"E"}
        assert kernel.control_bindings == saved
        assert f"motion.{ident}" not in kernel.effective_bindings()
        assert f"motion.{ident}" not in {key for key, _ in kernel._intent_actions_locked()}
        kernel.configure_pose_actions([])
        assert f"motion.{ident}" in kernel.effective_bindings()
        assert not any(item["id"] == f"motion.{ident}" for item in output.holds)
    finally:
        kernel.close()


def test_disabled_builtins_are_filtered_in_new_kernels_and_recording_replay():
    pose_library.register([], ["march", "jump"])
    assert {item["id"] for item in pose_library.library_payload()} == {
        'calf_back', 'lean_left', 'lean_right', 'lean_forward', 'lean_back', 'gunmouse_punch'}
    kernel = ControlKernel(Output())
    replay = None
    try:
        assert kernel.disabled_builtin_actions == {"march", "jump"}
        replay = make_replay_kernel(kernel.replay_snapshot())
        assert replay.disabled_builtin_actions == {"march", "jump"}
        for current in (kernel, replay):
            with current._lock:
                current.width, current.height = 640, 480
                current._update_motion_locked(_standing_pose(), 1.)
            assert not current.motion_raw["march"]
            assert not current.motion_raw["jump"]
    finally:
        kernel.close()
        if replay is not None:
            replay.close()


def test_builtin_removal_and_restore_handlers_need_no_network_and_keep_game_mappings(application, monkeypatch):
    app = application
    app.PROFILES.set_overrides({"motion.march": {"action": {"type": "keyboard", "target": "W", "behavior": "hold"}}})
    app._apply_effective_profile()
    saved = copy.deepcopy(app.PROFILES.effective_profile()["bindings"])
    monkeypatch.setattr(app, "CloudClient", lambda *args: (_ for _ in ()).throw(AssertionError("不应联网")))
    status, data = post(app, "/api/pose/remove", {"id": "march"})
    assert status == 200 and data["ok"]
    assert "march" not in {item["id"] for item in data["library"]}
    assert {item["id"] for item in data["removed_builtin"]} == {"march"}
    assert app.KERNEL.disabled_builtin_actions == {"march"}
    assert PoseActionStore(app.POSE_ACTIONS.path).disabled_builtin == {"march"}
    assert app.PROFILES.effective_profile()["bindings"] == saved
    status, listing = get(app, "/api/pose/library")
    assert status == 200 and listing["removed_builtin"] == data["removed_builtin"]
    status, restored = post(app, "/api/pose/builtin/restore", {"id": "march"})
    assert status == 200 and restored["removed_builtin"] == []
    assert "march" in {item["id"] for item in restored["library"]}
    assert not app.KERNEL.disabled_builtin_actions
    assert app.PROFILES.effective_profile()["bindings"] == saved
    assert post(app, "/api/pose/builtin/restore", {"id": "squat"})[0] == 400
