from motioncontrol.control_kernel import ControlKernel


class Output:
    def __init__(self):
        self.control_holds = []

    def set_action_holds(self, holds, source_group="controls"):
        if source_group == "controls":
            self.control_holds = list(holds)

    def set_buttons(self, *args, **kwargs):
        pass

    def set_holds(self, *args, **kwargs):
        pass

    def apply(self, *args, **kwargs):
        pass


def enabled_config():
    return {
        "enabled": True,
        "prepare_s": 0.16,
        "continue_window_s": 0.30,
        "chains": [{
            "id": "headJump_squat_stand",
            "start": "zone.headJump",
            "continue_condition": "motion.squat",
            "sustain_condition": "motion.squat",
            "end_condition": "motion.stand",
            "hold_action": "zone.headJump",
        }],
    }


def test_kernel_chain_uses_headjump_binding_and_never_uses_keyboard_injection(isolated_user_data):
    output = Output()
    kernel = ControlKernel(output)
    try:
        kernel.configure_bindings({
            "zones": {"headJump": {"action": {"type": "gamepad", "target": "A", "behavior": "tap"}}},
        })
        # Legacy mode remains direct and unchanged.
        kernel.zone_state["headJump"]["pressed"] = True
        kernel._dispatch_controls_locked(1.0)
        assert output.control_holds == []  # tap binding is an edge action in legacy mode
        kernel.configure_bindings({
            "zones": {"headJump": {"action": {"type": "gamepad", "target": "A", "behavior": "tap"}}},
        })
        kernel.configure_action_chain(enabled_config())
        kernel.action_chain_result = kernel.action_chain.update(2.0, {"zone.headJump": True, "motion.squat": False, "motion.stand": True})
        kernel._dispatch_controls_locked(2.0)
        assert output.control_holds[0]["action"]["target"] == "A"
        assert output.control_holds[0]["action"]["behavior"] == "hold"

        # Prepare expiry releases the chain; a squat in the continuation window
        # re-enters charging and keeps the same mapped A action held.
        kernel.action_chain_result = kernel.action_chain.update(2.20, {"zone.headJump": True, "motion.squat": False, "motion.stand": True})
        kernel._dispatch_controls_locked(2.20)
        assert output.control_holds == []
        kernel.action_chain_result = kernel.action_chain.update(2.25, {"zone.headJump": True, "motion.squat": True, "motion.stand": False})
        kernel._dispatch_controls_locked(2.25)
        assert output.control_holds[0]["id"] == "zone.headJump"
        kernel.action_chain_result = kernel.action_chain.update(2.30, {"zone.headJump": True, "motion.squat": False, "motion.stand": True})
        kernel._dispatch_controls_locked(2.30)
        assert output.control_holds == []

        status = kernel.status()
        assert status["action_chain"]["state"] == "RELEASED"
        assert status["action_chain"]["enabled"] is True
    finally:
        kernel.close()


def test_action_chain_configuration_persists_in_user_settings(isolated_user_data):
    first = ControlKernel(Output())
    first.configure_action_chain(enabled_config())
    first.close()
    second = ControlKernel(Output())
    try:
        assert second.action_chain.enabled is True
        assert second.status()["action_chain"]["config"]["prepare_s"] == 0.16
    finally:
        second.close()
