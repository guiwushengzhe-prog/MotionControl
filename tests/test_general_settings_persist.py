"""「通用设置」里不跟游戏走的那几项，必须能跨重启活下来。

原来一项都活不下来：手控鼠标压根没写过盘；上下视角只写在 scene_layout.json
里，而没定位过区域的人根本没有那个文件。于是每次启动都悄悄回到默认值，而界面
上看不出任何异常——玩家只会觉得「我明明开过」，然后怀疑是自己记错了。

这种"设置自己变回去"的毛病，比报错难查得多，所以这里用真的重建一次内核来钉。
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from control_kernel import ControlKernel  # noqa: E402


class Output:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture()
def home(isolated_user_data):
    """一个干净的用户数据目录，两次内核共用它，就像重启一次。

    用 conftest 那个 autouse 的 isolated_user_data，而不是自己改 LOCALAPPDATA：
    它设的是 MOTIONCONTROL_USER_DIR，优先级更高，自己另设一个只会被它盖掉。
    """
    return isolated_user_data


def settings_file(home: Path) -> Path:
    return home / "general_settings.json"


def test_hand_mouse_survives_a_restart(home):
    first = ControlKernel(Output())
    first.configure_hand_mouse({"enabled": True, "hand": "left", "sensitivity": 55})
    assert settings_file(home).is_file(), "改完就该落盘，不该等到退出"

    second = ControlKernel(Output())
    config = second.hand_mouse_controller.config
    assert config["enabled"] is True
    assert config["hand"] == "left"


def test_vertical_look_off_survives_a_restart(home):
    first = ControlKernel(Output())
    first.configure_head(vertical_look_source="off")
    assert first.vertical_look["enabled"] is False

    second = ControlKernel(Output())
    assert second.vertical_look["enabled"] is False, "关掉之后重启不该自己又开回来"
    assert second._gate_available() is False


def test_which_side_survives_too(home):
    first = ControlKernel(Output())
    first.configure_head(vertical_look_source="head")

    second = ControlKernel(Output())
    assert second.vertical_look["enabled"] is True
    assert second.vertical_look["source"] == "head"


def test_off_then_on_again_across_restarts(home):
    ControlKernel(Output()).configure_head(vertical_look_source="off")
    middle = ControlKernel(Output())
    assert middle.vertical_look["enabled"] is False

    middle.configure_head(vertical_look_source="hand")
    last = ControlKernel(Output())
    assert last.vertical_look["enabled"] is True
    assert last.vertical_look["source"] == "hand"


def test_a_broken_file_does_not_stop_the_program(home):
    path = settings_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 json", encoding="utf-8")

    kernel = ControlKernel(Output())          # 不该抛
    assert kernel.vertical_look["enabled"] is True, "读不出来就用默认值"


def test_a_file_of_the_wrong_shape_is_ignored(home):
    path = settings_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(["不是字典"]), encoding="utf-8")

    kernel = ControlKernel(Output())
    assert kernel.vertical_look["enabled"] is True


def test_no_file_at_all_is_normal(home):
    assert not settings_file(home).exists()
    kernel = ControlKernel(Output())
    assert kernel.vertical_look["enabled"] is True
    assert kernel.hand_mouse_controller.config["enabled"] is False
