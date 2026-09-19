"""开箱默认用电脑摄像头，而且记得上次选的是哪个。

以前默认是手机。对第一次打开这个软件的人来说这是错的：手机上还没装 APK，而
电脑摄像头就在那儿——默认值对着的是少数情况，多数人一进来先要改一个自己还不
知道含义的下拉框，改之前什么都不会发生。

但"默认电脑"会踩到两种人：一种电脑上根本没有摄像头，一种有好几个而对着自己的
不是 0 号。第一种人必须能把"手机"钉死，不能每次启动又被推回一个打不开的东西；
第二种人必须能换一个序号。这两条钉在下面。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol.control_kernel import (  # noqa: E402
    CameraUnavailable,
    ControlKernel,
    LocalControlRuntime,
    NativeCameraService,
)


class Output:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeCamera:
    """只要 LocalControlRuntime 用得到的那几个方法。"""

    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def status(self):
        return {"running": False}


@pytest.fixture()
def kernel():
    k = ControlKernel(Output())
    try:
        yield k
    finally:
        k.close()


def test_a_fresh_install_looks_at_the_computer(kernel):
    assert LocalControlRuntime(kernel, FakeCamera()).body_mode == "computer"


def test_choosing_the_phone_sticks_across_a_restart(kernel, isolated_user_data):
    """没有摄像头的电脑靠的就是这一条：选一次，以后都是手机。"""
    LocalControlRuntime(kernel, FakeCamera()).set_source("phone")

    again = ControlKernel(Output())
    try:
        assert LocalControlRuntime(again, FakeCamera()).body_mode == "phone"
    finally:
        again.close()


def test_the_choice_is_remembered_even_when_the_camera_will_not_open(kernel):
    """存盘要在开摄像头之前。

    开不起来也是一次有效的选择——真没有摄像头的人，正是要靠"选了电脑、失败、
    再改成手机"这条路把手机钉住的。要是失败就不存，他每次启动都得重来一遍。
    """
    class Refuses(FakeCamera):
        def start(self):
            raise CameraUnavailable("这台电脑没有摄像头")

    runtime = LocalControlRuntime(kernel, Refuses())
    with pytest.raises(CameraUnavailable):
        runtime.set_source("computer")
    assert kernel.general_setting("body_source") == "computer"


def test_a_rubbish_remembered_value_falls_back_to_the_default(kernel):
    kernel.remember_general_setting("body_source", "火星")
    assert LocalControlRuntime(kernel, FakeCamera()).body_mode == "computer"


def test_which_camera_survives_a_restart(kernel, isolated_user_data):
    """0 号未必是对着人的那个：外接摄像头、采集卡、OBS 虚拟摄像头都会插队。"""
    NativeCameraService(kernel).set_camera_index(2)

    again = ControlKernel(Output())
    try:
        assert NativeCameraService(again).camera_index == 2
    finally:
        again.close()


def test_an_explicit_index_still_wins(kernel):
    kernel.remember_general_setting("camera_index", 3)
    assert NativeCameraService(kernel, camera_index=0).camera_index == 0


@pytest.mark.parametrize("bad", [-1, 99, "左边那个"])
def test_a_nonsense_index_is_refused(kernel, bad):
    with pytest.raises(CameraUnavailable):
        NativeCameraService(kernel).set_camera_index(bad)


def test_changing_the_camera_drops_the_backend_picked_for_the_old_one(kernel):
    """那套后端和格式是上一个镜头探出来的，新镜头未必吃同一套。"""
    camera = NativeCameraService(kernel)
    camera.selected_backend = "dshow"
    camera.selected_fourcc = "MJPG"
    camera.set_camera_index(1)
    assert camera.selected_backend is None
    assert camera.selected_fourcc is None


def test_the_backend_config_says_which_camera(kernel):
    """界面上那个下拉框要知道现在选的是第几个，不然它只能一直显示 0。"""
    camera = NativeCameraService(kernel)
    camera.set_camera_index(1)
    config = camera.backend_config()
    assert config["camera_index"] == 1
    assert config["max_camera_index"] == NativeCameraService.MAX_CAMERA_INDEX


def test_a_kernel_without_the_settings_hooks_still_works(kernel):
    """测试里的内核可能是个假的。存不下一个序号，不该让摄像头开不起来。"""
    class Bare:
        pass

    camera = NativeCameraService(Bare())
    assert camera.camera_index == 0
    camera.set_camera_index(2)
    assert camera.camera_index == 2
