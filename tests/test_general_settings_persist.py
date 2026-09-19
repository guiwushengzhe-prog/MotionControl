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

from motioncontrol.control_kernel import ControlKernel  # noqa: E402


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
    # 手控鼠标默认开着：第一次装完的人只有握拳看鼠标动不动这一个办法确认它活着。
    assert kernel.hand_mouse_controller.config["enabled"] is True


def test_someone_elses_key_is_not_wiped_by_the_next_save(home):
    """摄像头来源和第几个摄像头也存在这份文件里，存它们的不是内核。

    内核每次写盘都是整份覆盖。要是只写自己那两块，别人刚存进去的东西下一次
    改手控鼠标就没了——而且是悄悄没的：文件还在，值回到默认。
    """
    first = ControlKernel(Output())
    first.remember_general_setting("body_source", "phone")
    first.configure_hand_mouse({"sensitivity": 42})

    second = ControlKernel(Output())
    assert second.general_setting("body_source") == "phone"
    assert second.hand_mouse_controller.config["sensitivity"] == 42


def test_an_unknown_key_reads_back_as_its_default(home):
    kernel = ControlKernel(Output())
    assert kernel.general_setting("从来没存过的东西", "兜底") == "兜底"
