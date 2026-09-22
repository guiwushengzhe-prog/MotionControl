"""键盘宏：校验规则和宏库的读写。

这里最要紧的一条不是格式检查，是**转圈**。一条宏可以引用另一条，所以 A 里放 B、
B 里放 A 是用户点几下就能造出来的；展开它会永远展不完。所以存盘之前必须走一遍
整张引用图，有环当场拒绝——而且要说出是哪几条在转，不然十几条宏里没人找得到。

第二要紧的是**上限**。没有环也可能一层套一层套出几千步，或者一条宏跑完要一分钟。
那种宏的毛病事后查不出来：人早就在做别的动作了，它还在按键，看起来就是"软件抽风"。
"""

from __future__ import annotations

import json

import pytest

from motioncontrol.key_macros import MacroError, MacroStore
from motioncontrol_shared import macro_schema
from motioncontrol_shared.macro_schema import (
    expand_steps,
    normalize_macro,
    normalize_macro_library,
    normalize_step,
    steps_duration_ms,
)


def key(target, **extra):
    return {"type": "keyboard", "target": target, **extra}


def macro(macro_id, name, steps, repeat=False):
    return {"id": macro_id, "name": name, "repeat": repeat, "steps": steps}


# --- 一步 -------------------------------------------------------------------

def test_a_keyboard_step_keeps_its_combo():
    step = normalize_step(key("CTRL+W"))
    assert step == {"type": "keyboard", "target": "CTRL+W",
                    "hold_ms": macro_schema.DEFAULT_HOLD_MS,
                    "gap_ms": macro_schema.DEFAULT_GAP_MS}


def test_every_output_type_may_appear_in_a_step():
    """用户要的是"什么都能放"，所以手柄、鼠标、扳机都得认。"""
    for raw, expected in [
        ({"type": "mouse_button", "target": "left"}, "LEFT"),
        ({"type": "gamepad", "target": "a"}, "A"),
        ({"type": "gamepad_trigger", "target": "rt"}, "RT"),
        ({"type": "gamepad_axis", "target": "ls_up"}, "LS_UP"),
    ]:
        assert normalize_step(raw)["target"] == expected


def test_a_wheel_step_has_no_hold_time():
    """滚轮是一下就完的事。留着一个能调的"按住多久"只会让人以为它有用。"""
    step = normalize_step({"type": "mouse_wheel", "target": "SCROLL_UP", "hold_ms": 500})
    assert step["hold_ms"] == 0


def test_a_nested_step_only_carries_the_id():
    step = normalize_step({"type": "macro", "target": "m_inner", "gap_ms": 100})
    assert step == {"type": "macro", "target": "m_inner", "gap_ms": 100}
    assert "hold_ms" not in step, "按多久由被引用那条宏自己的步骤决定"


def test_timings_outside_the_range_are_refused():
    with pytest.raises(ValueError, match="按住时长"):
        normalize_step(key("W", hold_ms=5))
    with pytest.raises(ValueError, match="按住时长"):
        normalize_step(key("W", hold_ms=5000))
    with pytest.raises(ValueError, match="间隔"):
        normalize_step(key("W", gap_ms=-1))


def test_an_unknown_step_type_is_refused():
    with pytest.raises(ValueError):
        normalize_step({"type": "speak", "target": "hello"})


def test_a_typo_in_a_key_name_is_refused():
    """错的键名只会表现成游戏里什么都没发生，存盘时就得拦住。"""
    with pytest.raises(ValueError):
        normalize_step(key("WW"))


# --- 一条宏 -----------------------------------------------------------------

def test_a_macro_needs_a_name_and_at_least_one_step():
    with pytest.raises(ValueError, match="名字"):
        normalize_macro(macro("m1", "  ", [key("W")]))
    with pytest.raises(ValueError, match="一步都没有"):
        normalize_macro(macro("m1", "空的", []))


def test_a_macro_may_not_be_longer_than_the_limit():
    steps = [key("W") for _ in range(macro_schema.MAX_STEPS + 1)]
    with pytest.raises(ValueError, match="最多"):
        normalize_macro(macro("m1", "太长", steps))


def test_the_repeat_flag_lives_on_the_macro():
    """「跑一遍还是循环」是宏自己的属性，不是每条绑定各选一次。"""
    assert normalize_macro(macro("m1", "连击", [key("W")], repeat=True))["repeat"] is True
    assert normalize_macro(macro("m1", "连击", [key("W")]))["repeat"] is False


# --- 整库 -------------------------------------------------------------------

def test_two_macros_may_not_share_a_name():
    """名字是用户在下拉里唯一看得见的东西。两条同名等于让人抓阄。"""
    with pytest.raises(ValueError, match="已经有一条"):
        normalize_macro_library({"macros": [
            macro("m1", "三连击", [key("W")]),
            macro("m2", "三连击", [key("Q")]),
        ]})


def test_a_reference_to_a_macro_that_is_not_there_is_refused():
    with pytest.raises(ValueError, match="找不到被引用的宏"):
        normalize_macro_library({"macros": [
            macro("m1", "外层", [{"type": "macro", "target": "m_missing"}]),
        ]})


def test_a_macro_that_references_itself_is_refused():
    with pytest.raises(ValueError, match="转圈"):
        normalize_macro_library({"macros": [
            macro("m1", "自己", [{"type": "macro", "target": "m1"}]),
        ]})


def test_a_two_step_loop_is_refused_and_named():
    """光说"有循环引用"没用，得说出是哪几条——十几条宏里没人找得到。"""
    with pytest.raises(ValueError) as caught:
        normalize_macro_library({"macros": [
            macro("m1", "甲", [{"type": "macro", "target": "m2"}]),
            macro("m2", "乙", [{"type": "macro", "target": "m1"}]),
        ]})
    message = str(caught.value)
    # 说名字不说编号。“mfeb7debb → m7efaa1c3”对看的人等于没说，而这句话就是他唯一
    # 能看到的东西。
    assert "甲" in message and "乙" in message, f"环里那几条的名字要说出来：{message}"


def test_nesting_deeper_than_the_limit_is_refused():
    macros = [macro(f"m{i}", f"第{i}层", [{"type": "macro", "target": f"m{i + 1}"}])
              for i in range(macro_schema.MAX_DEPTH + 2)]
    macros.append(macro(f"m{macro_schema.MAX_DEPTH + 2}", "最里面", [key("W")]))
    with pytest.raises(ValueError, match="套得太深"):
        normalize_macro_library({"macros": macros})


def test_a_macro_that_takes_too_long_is_refused():
    """人早就在做别的动作了宏还在按键，那种毛病事后没人查得出来。"""
    long_steps = [key("W", hold_ms=1000, gap_ms=1000) for _ in range(macro_schema.MAX_STEPS)]
    with pytest.raises(ValueError, match="秒"):
        normalize_macro_library({"macros": [macro("m1", "太久", long_steps)]})


def test_expansion_inlines_the_referenced_steps():
    library = normalize_macro_library({"macros": [
        macro("inner", "里面", [key("Q"), key("E")]),
        macro("outer", "外面", [key("W"), {"type": "macro", "target": "inner"}, key("R")]),
    ]})
    macros = {item["id"]: item for item in library["macros"]}
    targets = [step["target"] for step in expand_steps(macros, "outer")]
    assert targets == ["W", "Q", "E", "R"]


def test_a_nested_steps_gap_lands_after_the_inlined_block():
    """读起来就是"跑完这段，歇一会，再往下"。"""
    library = normalize_macro_library({"macros": [
        macro("inner", "里面", [key("Q", gap_ms=10)]),
        macro("outer", "外面", [{"type": "macro", "target": "inner", "gap_ms": 200}, key("R")]),
    ]})
    macros = {item["id"]: item for item in library["macros"]}
    steps = expand_steps(macros, "outer")
    assert steps[0]["gap_ms"] == 210, "引用步的间隔要加在被引用那段的最后一步后面"


def test_the_total_is_measured_after_expansion_not_before():
    """三步的宏引用三步的宏是九步。只数自己写的那几步等于没有上限。"""
    library = normalize_macro_library({"macros": [
        macro("a", "甲", [key("Q"), key("W"), key("E")]),
        macro("b", "乙", [{"type": "macro", "target": "a"}, {"type": "macro", "target": "a"}]),
    ]})
    macros = {item["id"]: item for item in library["macros"]}
    steps = expand_steps(macros, "b")
    assert len(steps) == 6
    # 六步各自的时长，外加两个引用步自己的间隔——那两个间隔加在各自那段的末尾。
    assert steps_duration_ms(steps) == 6 * (
        macro_schema.DEFAULT_HOLD_MS + macro_schema.DEFAULT_GAP_MS
    ) + 2 * macro_schema.DEFAULT_GAP_MS


def test_the_expanded_step_count_is_capped():
    """没有环也可能一层套一层套出几千步。"""
    wide = [key("W") for _ in range(macro_schema.MAX_STEPS)]
    macros = [macro("a", "甲", wide)]
    macros.append(macro("b", "乙", [{"type": "macro", "target": "a"}] * macro_schema.MAX_STEPS))
    with pytest.raises(ValueError, match="超过"):
        normalize_macro_library({"macros": macros})


# --- 宏库文件 ---------------------------------------------------------------

def store(tmp_path) -> MacroStore:
    return MacroStore(tmp_path / "key_macros.json")


def test_a_new_library_starts_empty(tmp_path):
    assert store(tmp_path).macros == []


def test_create_update_remove_round_trips_through_the_file(tmp_path):
    first = store(tmp_path)
    made = first.create(name="三连击", steps=[key("Q"), key("W"), key("E")])
    first.update(made["id"], repeat=True)

    reopened = MacroStore(tmp_path / "key_macros.json")
    assert [item["name"] for item in reopened.macros] == ["三连击"]
    assert reopened.repeats(made["id"]) is True
    assert [step["target"] for step in reopened.expanded(made["id"])] == ["Q", "W", "E"]

    assert reopened.remove(made["id"]) is True
    assert MacroStore(tmp_path / "key_macros.json").macros == []


def test_a_macro_still_used_by_another_one_may_not_be_deleted(tmp_path):
    """删完再展开会报"找不到被引用的宏"，那时候用户已经不记得删了什么了。"""
    library = store(tmp_path)
    inner = library.create(name="里面", steps=[key("Q")])
    library.create(name="外面", steps=[{"type": "macro", "target": inner["id"]}])
    with pytest.raises(MacroError, match="外面"):
        library.remove(inner["id"])
    assert library.get(inner["id"]) is not None, "拒绝之后不能已经删掉了"


def test_removing_a_macro_that_is_not_there_is_not_an_error(tmp_path):
    assert store(tmp_path).remove("m_nope") is False


def test_ids_are_never_reused(tmp_path):
    """递增编号会把新宏发成刚删掉那条的编号，于是旧绑定突然指到一条毫不相干的
    新宏上——它不报错，只是按错键，属于最难查的一类。"""
    library = store(tmp_path)
    first = library.create(name="甲", steps=[key("Q")])
    library.remove(first["id"])
    second = library.create(name="乙", steps=[key("W")])
    assert second["id"] != first["id"]


def test_a_broken_file_is_ignored_instead_of_crashing_the_app(tmp_path):
    path = tmp_path / "key_macros.json"
    path.write_text("{ not json", encoding="utf-8")
    library = MacroStore(path)
    assert library.macros == []
    assert library.last_error


def test_a_file_that_loops_is_dropped_rather_than_loaded(tmp_path):
    """手改过的文件可能带环。带着一份会转圈的宏库跑起来，一触发就是无限展开。"""
    path = tmp_path / "key_macros.json"
    path.write_text(json.dumps({
        "schema": "motioncontrol.key_macros.v1",
        "macros": [
            macro("m1", "甲", [{"type": "macro", "target": "m2", "gap_ms": 0}]),
            macro("m2", "乙", [{"type": "macro", "target": "m1", "gap_ms": 0}]),
        ],
    }, ensure_ascii=False), encoding="utf-8")
    library = MacroStore(path)
    assert library.macros == []
    assert "转圈" in library.last_error


def test_a_rejected_edit_leaves_the_file_alone(tmp_path):
    library = store(tmp_path)
    made = library.create(name="三连击", steps=[key("Q")])
    with pytest.raises(MacroError):
        library.update(made["id"], steps=[key("NOPE")])
    reopened = MacroStore(tmp_path / "key_macros.json")
    assert [step["target"] for step in reopened.expanded(made["id"])] == ["Q"]


def test_a_missing_reference_expands_to_nothing_instead_of_raising(tmp_path):
    """这条路上的调用方是输出后端，它在按键的热路径上。抛异常会把整组绑定的刷新
    一起打断，那比什么都不按糟得多。"""
    library = store(tmp_path)
    assert library.expanded("m_nope") == []
    assert library.repeats("m_nope") is False


def test_describe_says_when_a_binding_points_at_a_deleted_macro(tmp_path):
    """界面靠它写「宏已丢失」。没有这个，那一行会显示一串编号。"""
    library = store(tmp_path)
    assert library.describe("m_nope")["missing"] is True
    made = library.create(name="三连击", steps=[key("Q"), key("W")])
    described = library.describe(made["id"])
    assert described["missing"] is False and described["name"] == "三连击"
    assert described["step_count"] == 2


def test_the_detail_view_keeps_the_real_step_list(tmp_path):
    """界面把简介盖在宏本体上用。简介里那个计数如果也叫 steps，就会把真的
    步骤表换成一个数字——步骤编辑器直接画不出来，而后端一点事没有。真踩过。"""
    library = store(tmp_path)
    library.create(name="三连击", steps=[key("Q"), key("W")])
    row = library.detail()[0]
    assert isinstance(row["steps"], list), "步骤表被简介盖成数字了"
    assert [item["target"] for item in row["steps"]] == ["Q", "W"]
    assert row["step_count"] == 2
