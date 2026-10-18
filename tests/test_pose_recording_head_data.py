"""头部录制保留同帧模型输入与控制结果，不打开相机或系统输入。"""
import json
import time

import pytest

from motioncontrol.control_kernel import ControlKernel
from test_control_kernel import FakeOutput, pose_map


def test_kernel_records_current_head_result_profile_world_and_loss(tmp_path):
    output = FakeOutput()
    output.enabled = False
    kernel = ControlKernel(output)
    try:
        controller = kernel.head_controller
        controller.configure(horizontal_algorithm="hfhpe", deadzone=.25)
        controller._hfhpe_calibration = {"version": 1, "center_yaw_deg": 6.4,
                                         "noise_yaw_deg": .16, "sample_count": 34}
        kernel.pose_recorder.directory = tmp_path / "recordings"
        kernel.pose_recorder.start(delay_s=0, duration_s=15)
        world = {"nose": {"x": .1, "y": .2, "z": -.3, "score": .9}}
        for yaw in (6.4, 12.0):
            kernel.handle_pose_map("computer_camera", pose_map(), width=640, height=360,
                                   world_pose=world, sample_at=time.monotonic(), return_status=False,
                                   head_pose={"source": "hfhpe", "model": "vitt128", "valid": True,
                                              "yaw_deg": yaw, "pitch_deg": 1., "roll_deg": 2.,
                                              "inference_ms": 8., "kappa": [9., 8., 7.]})
        # 切换为丢失帧不能静默漏掉，亦不能继承上一次有效角度。
        kernel.handle_pose_map("computer_camera", None, head_pose={"source": "hfhpe", "valid": False,
                                                                   "error": "头部丢失"}, return_status=False)
        assert kernel.pose_recorder.close(2)
        header, first, second, lost = map(json.loads, kernel.pose_recorder.last_path.read_text(encoding="utf-8").splitlines())
        profile = json.loads(header["snapshot"]["head_profile_json"])
        assert profile["hfhpe_calibration"]["center_yaw_deg"] == 6.4
        assert profile["params"]["horizontal_algorithm"] == "hfhpe"
        assert first["extra"]["head"]["hfhpe_yaw_deg"] == pytest.approx(6.4)
        assert second["extra"]["head"]["hfhpe_yaw_deg"] == pytest.approx(12.)
        assert second["extra"]["head_pose"]["yaw_deg"] == 12.
        assert second["extra"]["head_pose"]["model"] == "vitt128"
        assert second["extra"]["head_config"]["deadzone"] == .25
        assert second["world_pose"] == world
        assert "sample_t" in second
        assert not second["extra"]["output_enabled"]
        assert lost["pose"] == {}
        assert not lost["extra"]["head_pose"]["valid"]
        assert not lost["extra"]["head"]["hfhpe_estimate_valid"]
    finally:
        kernel.close()
