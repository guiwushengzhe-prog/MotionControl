from __future__ import annotations

from control_kernel import ControlKernel
from vertical_hand_control import VerticalHandController


CONFIG = {"point": "right_wrist", "range_y": 0.18, "deadzone": 0.10}


def _point(x: float, y: float, score: float = 1.0) -> dict:
    return {"x": x, "y": y, "z": 0.0, "score": score}


def _pose(wrist_y: float = 0.50, elbow_y: float = 0.43, shoulder_y: float = 0.32) -> dict:
    """A compact but sufficiently rich pose for v160's geometry witnesses."""
    return {
        "left_shoulder": _point(0.38, shoulder_y),
        "right_shoulder": _point(0.62, shoulder_y),
        "left_hip": _point(0.42, 0.60),
        "right_hip": _point(0.58, 0.60),
        "right_elbow": _point(0.66, elbow_y),
        "right_wrist": _point(0.72, wrist_y),
    }


def test_v160_public_contract_anchor_motion_and_loss_release():
    controller = VerticalHandController()

    initial = controller.status()
    assert initial["version"].startswith("vertical-hand-v160-")
    assert initial["output"] == 0.0

    # v160 deliberately requires both a sample count and a short stable time
    # window; this prevents a gate transition or one frame from becoming the
    # neutral hand position.
    for index in range(13):
        state = controller.update(_pose(), index / 30.0, CONFIG)
    assert state["anchor_rel_y"] is not None
    assert state["anchor_samples"] == 8
    assert state["active"] is False

    outputs = []
    for index, wrist_y in enumerate((0.47, 0.44, 0.40, 0.35, 0.30), start=1):
        state = controller.update(
            _pose(wrist_y=wrist_y, elbow_y=0.43 - (0.50 - wrist_y) * 0.3),
            0.50 + index / 30.0,
            CONFIG,
        )
        outputs.append(state["output"])
    assert any(value < -0.05 for value in outputs)
    assert all(-1.0 <= value <= 1.0 for value in outputs)

    # A missing pose must release this source immediately, even though the
    # candidate keeps only a short internal reacquisition history.
    lost = controller.update({}, 0.70, CONFIG)
    assert lost["output"] == 0.0
    assert lost["active"] is False


def test_v160_requires_stable_anchor_and_reset_restarts_collection():
    controller = VerticalHandController()

    # Six samples are not enough before the 0.40 s minimum; alternating
    # samples also fail the stationary/dispersion check.
    for index, wrist_y in enumerate((0.45, 0.55, 0.45, 0.55, 0.45, 0.55, 0.45, 0.55, 0.45, 0.55, 0.45, 0.55)):
        state = controller.update(_pose(wrist_y=wrist_y), index / 30.0, CONFIG)
    assert state["anchor_rel_y"] is None
    assert state["output"] == 0.0

    controller.reset(1.0)
    reset_state = controller.status()
    assert reset_state["anchor_rel_y"] is None
    assert reset_state["anchor_samples"] == 0
    assert reset_state["filter_last_at"] == 1.0


def test_v160_optional_scale_defaults_do_not_change_legacy_config_contract():
    controller = VerticalHandController()
    config = {"point": "right_wrist", "range_y": 0.18, "deadzone": 0.10}
    for index in range(13):
        state = controller.update(_pose(), index / 30.0, config)
    assert state["anchor_rel_y"] is not None
    assert state["camera_scale_ratio"] == 1.0


class _KernelOutput:
    enabled = False

    def set_buttons(self, *_args, **_kwargs):
        pass

    def set_holds(self, *_args, **_kwargs):
        pass

    def apply(self, *_args, **_kwargs):
        pass

    def clear_source(self, *_args, **_kwargs):
        pass


def test_kernel_refreshes_v160_anchor_alias_after_reset():
    kernel = ControlKernel(_KernelOutput())
    try:
        old_alias = kernel.vertical_anchor_samples
        kernel._reset_vertical_hand_locked(1.0)
        assert kernel.vertical_anchor_samples is kernel.vertical_hand_controller.anchor_samples
        assert kernel.vertical_anchor_samples is not old_alias
        assert len(kernel.vertical_anchor_samples) == 0
    finally:
        kernel.close()
