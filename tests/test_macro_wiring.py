"""键盘宏接到现有那几条路上：绑定、界面、推给手机。

宏本身的规则在 test_key_macros.py，跑起来的行为在 test_macro_output.py。这里管的
是"接对了没有"——每一条都对应一个只会安静失效、不会报错的接错法。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from motioncontrol.control_kernel import ControlKernel
from motioncontrol_shared.profile_schema import action_catalog, normalize_action
from motioncontrol_shared.sync_allowlist import CLASSIFICATION
from motioncontrol_shared.user_layout import USER_DATA_FILES

ROOT = Path(__file__).resolve().parent.parent


# --- 绑定认得宏 -------------------------------------------------------------

def test_a_binding_may_point_at_a_macro():
    action = normalize_action({"type": "macro", "target": "m1a2b3"}, default_behavior="tap")
    assert action == {"type": "macro", "target": "m1a2b3", "behavior": "tap"}


def test_a_macro_id_keeps_its_case():
    """别的类型一律转大写（键名本来就是大写）。宏编号是小写的，跟着转就再也认不
    出是哪条宏了——绑定还在，只是永远找不到那条宏。"""
    assert normalize_action({"type": "macro", "target": "M1A2B3"})["target"] == "m1a2b3"


def test_a_target_that_is_not_an_id_is_refused():
    with pytest.raises(ValueError, match="宏编号"):
        normalize_action({"type": "macro", "target": "CTRL+W"})


def test_the_action_catalog_offers_the_macro_type():
    """界面上那个下拉是照着这份目录建的。不在目录里，宏就没地方选。"""
    assert "macro" in action_catalog()


def test_the_macro_file_is_registered_and_classified():
    """两处都登记过，新文件才不会悄悄地既不存盘也不被同步规则覆盖。"""
    assert "key_macros" in USER_DATA_FILES
    assert "key_macros" in CLASSIFICATION


# --- 「跑一遍还是循环」跟着宏走 ---------------------------------------------

class Output:
    """只认 set_action_holds 的假输出。内核是鸭子类型调它的。"""

    def __init__(self):
        self.macros = None
        self.holds = []

    def configure_macros(self, store):
        self.macros = store

    def set_action_holds(self, holds, source_group="controls"):
        self.holds = list(holds)
        return {}

    def set_buttons(self, *args, **kwargs):
        return {}

    def set_holds(self, *args, **kwargs):
        return {}

    def apply(self, *args, **kwargs):
        return {}

    def status(self):
        return {}


class Library:
    def __init__(self, repeating: set[str]):
        self.repeating = repeating

    def expanded(self, macro_id):
        return [{"type": "keyboard", "target": "Q", "hold_ms": 10, "gap_ms": 0}]

    def repeats(self, macro_id):
        return str(macro_id) in self.repeating


def kernel_with(macros, bindings):
    kernel = ControlKernel(Output())
    kernel.configure_macros(macros)
    kernel.configure_bindings(bindings)
    return kernel


def binding(macro_id, behavior="hold"):
    return {"zones": {"leftHand": {"action": {"type": "macro", "target": macro_id,
                                              "behavior": behavior}}}}


def test_a_one_shot_macro_makes_its_binding_fire_once():
    kernel = kernel_with(Library(set()), binding("m1", behavior="hold"))
    assert kernel.control_bindings["zone.leftHand"]["action"]["behavior"] == "tap"


def test_a_repeating_macro_makes_its_binding_a_hold():
    kernel = kernel_with(Library({"m1"}), binding("m1", behavior="tap"))
    assert kernel.control_bindings["zone.leftHand"]["action"]["behavior"] == "hold"


def test_changing_the_macro_updates_bindings_that_already_exist():
    """用户在宏库里打开「循环」，已经绑好的那几处必须跟着变。不跟着变的话，界面上
    写着循环、实际只跑一遍，这种不一致查起来最费劲。"""
    kernel = kernel_with(Library(set()), binding("m1"))
    assert kernel.control_bindings["zone.leftHand"]["action"]["behavior"] == "tap"
    kernel.configure_macros(Library({"m1"}))
    assert kernel.control_bindings["zone.leftHand"]["action"]["behavior"] == "hold"


def test_a_voice_release_binding_is_left_alone():
    """语音的「松开」是一条停止指令，和宏本身循环不循环无关。被改掉就没法停了。"""
    kernel = kernel_with(Library({"m1"}), {
        "voice": {"stop": {"action": {"type": "macro", "target": "m1", "behavior": "release"}}}})
    assert kernel.control_bindings["voice.stop"]["action"]["behavior"] == "release"


def test_bindings_that_are_not_macros_keep_what_was_saved():
    kernel = kernel_with(Library({"m1"}), {
        "zones": {"leftHand": {"action": {"type": "gamepad", "target": "A", "behavior": "hold"}}}})
    assert kernel.control_bindings["zone.leftHand"]["action"]["behavior"] == "hold"


def test_the_library_reaches_the_output_backend():
    """内核自己不按键。宏库不传下去的话，绑定看着是好的，触发时什么都不会发生。"""
    kernel = ControlKernel(Output())
    library = Library(set())
    kernel.configure_macros(library)
    assert kernel.output.macros is library


# --- 推给手机 ---------------------------------------------------------------

def test_the_phone_payload_carries_the_macro_names():
    """手机上存的是编号。不带名字过去，圈上只能显示一串 m3f2a1。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    assert '"macros": MACROS.status()' in server


def test_editing_a_macro_pushes_the_new_config_to_the_phone():
    """手机不轮询，全靠电脑主动推。漏掉这一步，手机上的映射就一直是旧的。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    route = server.index('if route.startswith("/api/macros/")')
    after = server[route:route + 2600]
    assert "broadcast_control_config" in after, "改完宏没有推给手机"
    assert "KERNEL.configure_macros(MACROS)" in after, "改完宏没有重新装进内核"


def test_macro_edits_are_loopback_only():
    """和自定义姿势同一条规矩：手机可以看，改只能在这台电脑上。"""
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    route = server.index('if route.startswith("/api/macros/")')
    assert "_is_loopback" in server[route:route + 300]


# --- 界面 -------------------------------------------------------------------

def test_the_binding_editor_offers_macros_everywhere():
    """下拉是遍历 ACTION_TYPE_LABELS 建的，所以加进去就是四组触发器都有。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "macro:'键盘宏'" in app


def test_a_pose_card_shows_the_key_it_is_bound_to():
    """录完的姿势在卡片上看不出绑了什么，人得滚到上面那张表里一行行找。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "custom-pose-key" in app
    assert "revealBindingRow('pose.' + item.id)" in app


def test_the_pose_card_key_is_read_only():
    """同一个东西两处能改，就一定会有一处是旧的。卡片只显示，改在映射表里。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    start = app.index("const bound = document.createElement('button')")
    card = app[start:start + 500]
    assert "createElement('select')" not in card, "卡片上放了第二个能改的控件"


def test_the_zone_circles_and_the_pose_cards_use_the_same_wording():
    """两处各写一份的话，同一个绑定在圈上和卡片上会显示成两种说法。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "function triggerKeyLabel(" in app
    assert app.count("actionKeyText(binding.action)") >= 1


def test_the_page_no_longer_says_custom_phrases_are_desktop_only():
    """自定义口令现在跟着配置一起推给手机，手机自己按那份重建识别器——见
    tests/test_voice_hold.py。那句话是加推送之前留下的，留着就是在误导人。"""
    # 两个文件都要查。这句话实际写在 app.js 那个分组提示里，只查 index.html
    # 的话测试会绿着而界面上那句错话还在。
    for name in ("index.html", "app.js"):
        page = (ROOT / "web" / name).read_text(encoding="utf-8")
        assert "只有电脑麦克风能识别" not in page, name

def test_the_trigger_inputs_load_before_anything_can_render_rows():
    """自定义姿势和宏库要比 refreshKernel 还早拿到。

    refreshKernel 里有一句 void loadProfiles()，它**不被 await**，会自己跑去建
    映射行。那一刻这两份要是还没到，建出来的表就是缺的：录过的姿势根本没有对应
    的行（没地方绑键），每个「键盘宏」下拉都写着“还没有宏”。东西明明在，页面上却
    说没有——这种毛病不报错，只是看着像数据丢了。

    旧毛病，只是之前靠时机碰对的次数多。中间多一次 await 就会碰错，
    2026-09-22 真碰上了。
    """
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    init = app.index("async function init(){")
    poses = app.index("refreshCustomPoses({rebuild:false})", init)
    macros = app.index("refreshMacros({rebuild:false})", init)
    kernel = app.index("await refreshKernel()", init)
    assert poses < kernel, "自定义姿势排到 refreshKernel 后面去了"
    assert macros < kernel, "宏库排到 refreshKernel 后面去了"
