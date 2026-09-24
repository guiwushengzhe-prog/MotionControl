"""键盘宏的校验规则。纯函数，不碰文件、不碰环境，云端在 Linux 上也能 import。

一条宏是一串有先后的按键步骤。步骤里可以放键盘、鼠标、手柄，也可以直接放
**另一条已经建好的宏**——这是它和"多写几个键"最大的区别：常用的一段只建一次，
别的宏引用它，改一处到处生效。

引用带来的唯一真问题是**转圈**：A 里放 B，B 里又放 A，展开就永远展不完。所以这里
的核心不是格式检查，而是：

1. 存盘之前就把整张引用图走一遍，有环直接拒绝，并说出是哪几条在转圈；
2. 展开之后的步数和总时长都封顶——没有环也可能一层套一层套出几千步。

步骤默认一个接一个。勾了 ``with_prev`` 的一步和上一步**同一瞬间按下**，比如按住
SHIFT 的同时点鼠标左键。连在一起的这几步叫"一组"：各按各的时长、各等各的间隔，
整组里最晚结束的那个结束了，才走下一步。同一种设备的几个键其实写成一步
（``CTRL+W``）也能一起按，但那样只能一起松开，也没法跨键盘、鼠标、手柄。

封顶的数字不是随便定的。超过十秒的宏，人早就在做别的动作了而宏还在按键，那种毛病
事后没人查得出来是宏干的，只会觉得"软件抽风"。宁可存不进去，也不要让它存进去。
"""

from __future__ import annotations

import re

from .profile_schema import normalize_action

# 一条宏自己最多写多少步。再多就该拆成两条互相引用，那样界面上也看得懂。
MAX_STEPS = 12
# 展开（把引用的宏内联进来）之后的上限。没有环也可能一层套一层套出很多步。
MAX_EXPANDED_STEPS = 64
# 引用能套多深。四层已经没人能在脑子里跟住了。
MAX_DEPTH = 4
# 整条宏跑完最多多久。超过这个长度就是上面说的"人已经走开了宏还在按"。
MAX_TOTAL_MS = 10_000
MIN_HOLD_MS, MAX_HOLD_MS = 10, 1000
MIN_GAP_MS, MAX_GAP_MS = 0, 1000
MAX_MACROS = 64
MAX_NAME_LEN = 20

DEFAULT_HOLD_MS = 60
DEFAULT_GAP_MS = 40

MACRO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")

# 步骤里能放什么：所有已有的输出类型，外加 "macro"（引用另一条宏）。
# 复用 profile_schema 的类型名，是为了整个项目只有一套词汇——界面上那个下拉、
# 绑定里存的 type、宏步骤里的 type 说的是同一件事，不用来回翻译。
STEP_TYPES = {"keyboard", "mouse_button", "mouse_wheel", "gamepad", "gamepad_trigger", "gamepad_axis", "macro"}


def _compact(text: str) -> str:
    return " ".join(str(text or "").split())


def normalize_step(raw) -> dict:
    """校验一步。引用类型只检查 id 长得对不对，存不存在要在整库层面才知道。"""
    if not isinstance(raw, dict):
        raise ValueError("宏的每一步必须是一个对象")
    step_type = str(raw.get("type", "")).strip().lower()
    if step_type in {"nested", "ref", "macro_ref"}:
        step_type = "macro"
    if step_type not in STEP_TYPES:
        raise ValueError(f"宏里不支持这种步骤：{step_type or '(空)'}")

    gap_ms = _as_ms(raw.get("gap_ms", DEFAULT_GAP_MS), MIN_GAP_MS, MAX_GAP_MS, "间隔")

    with_prev = bool(raw.get("with_prev", False))

    if step_type == "macro":
        target = str(raw.get("target", "")).strip().lower()
        if not MACRO_ID_RE.match(target):
            raise ValueError(f"引用的宏编号不对：{target or '(空)'}")
        if with_prev:
            # 引用的是一整串有先后的步骤，没法"和上一步同时"只按下一下。
            raise ValueError("「跑另一条宏」这一步不能和上一步同时按")
        # 引用步没有"按住多久"——按多久由被引用的那条宏自己的步骤决定。
        return {"type": "macro", "target": target, "gap_ms": gap_ms}

    # behavior 在宏里没有意义：每一步都是按下、等一会、松开。交给 normalize_action
    # 去认键名，然后把它那份 behavior 丢掉。
    action = normalize_action({"type": step_type, "target": raw.get("target", "")}, default_behavior="tap")
    hold_ms = _as_ms(raw.get("hold_ms", DEFAULT_HOLD_MS), MIN_HOLD_MS, MAX_HOLD_MS, "按住时长")
    if action["type"] == "mouse_wheel":
        # 滚轮是一下就完的事，没有"按住"。写多少都一样，统一成 0 免得看着像能调。
        hold_ms = 0
    step = {"type": action["type"], "target": action["target"], "hold_ms": hold_ms, "gap_ms": gap_ms}
    if with_prev:
        # 只在勾上时才写，没勾的步骤和以前存的一模一样。
        step["with_prev"] = True
    return step


def _as_ms(value, low: int, high: int, what: str) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        raise ValueError(f"{what}必须是数字")
    if number < low or number > high:
        raise ValueError(f"{what}要在 {low} 到 {high} 毫秒之间，收到 {number}")
    return number


def normalize_macro(raw) -> dict:
    """校验一条宏本身。不看它引用的宏存不存在——那是整库的事。"""
    if not isinstance(raw, dict):
        raise ValueError("宏必须是一个对象")
    macro_id = str(raw.get("id", "")).strip().lower()
    if not MACRO_ID_RE.match(macro_id):
        raise ValueError(f"宏编号不对：{macro_id or '(空)'}")
    name = _compact(raw.get("name", ""))
    if not name:
        raise ValueError("宏要有名字，不然在下拉里认不出来")
    if len(name) > MAX_NAME_LEN:
        raise ValueError(f"宏的名字最多 {MAX_NAME_LEN} 个字")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError(f"「{name}」一步都没有，存了也不会按任何键")
    if len(steps_raw) > MAX_STEPS:
        raise ValueError(f"「{name}」有 {len(steps_raw)} 步，最多 {MAX_STEPS} 步")
    steps = [normalize_step(step) for step in steps_raw]
    # 第一步前面没有东西可以"同时"。删掉原来的第一步之后第二步就顶上来了，
    # 这时它身上的勾没有意义，直接去掉，不值得为这个拒绝保存。
    steps[0].pop("with_prev", None)
    for index in range(1, len(steps)):
        if steps[index].get("with_prev") and steps[index - 1]["type"] == "macro":
            raise ValueError(f"「{name}」第 {index + 1} 步不能和上一步同时按：上一步是另一条宏")
    return {
        "id": macro_id,
        "name": name,
        # 这条宏默认是跑一遍还是按住时反复跑。绑定的"方式"跟着它走。
        "repeat": bool(raw.get("repeat", False)),
        "steps": steps,
    }


def expand_steps(macros: dict[str, dict], macro_id: str, *, _chain: tuple[str, ...] = ()) -> list[dict]:
    """把引用内联掉，得到一条只剩实际按键的平铺步骤表。

    ``_chain`` 是从最外层到当前这条的路径，转圈时靠它把环原样说出来——只说
    "有循环引用"，用户在十几条宏里根本找不到是哪两条。
    """
    if macro_id in _chain:
        # 用名字不用编号。“mfeb7debb → m7efaa1c3”对看的人等于没说，而这句话存不进去时
        # 是用户唯一能看到的东西。
        path = [*_chain[_chain.index(macro_id):], macro_id]
        loop = " → ".join(f"「{macros[step]['name']}」" if step in macros else step for step in path)
        raise ValueError(f"宏转圈了：{loop}")
    macro = macros.get(macro_id)
    if macro is None:
        raise ValueError(f"找不到被引用的宏：{macro_id}")
    if len(_chain) >= MAX_DEPTH:
        raise ValueError(f"宏套得太深了，最多 {MAX_DEPTH} 层")

    out: list[dict] = []
    for step in macro["steps"]:
        if step["type"] != "macro":
            out.append(dict(step))
            continue
        inner = expand_steps(macros, step["target"], _chain=(*_chain, macro_id))
        if not inner:
            continue
        out.extend(inner)
        # 引用步自己的间隔加在被引用那段的最后一组后面：读起来就是
        # "跑完这段，歇一会，再往下"。最后一组是同时按的几步时，每一步都加，
        # 整组的结束时间才正好往后推这么多。
        if step["gap_ms"]:
            last = len(out) - 1
            while last > 0 and out[last].get("with_prev"):
                last -= 1
            for index in range(last, len(out)):
                out[index] = {**out[index], "gap_ms": min(MAX_GAP_MS, out[index]["gap_ms"] + step["gap_ms"])}
        if len(out) > MAX_EXPANDED_STEPS:
            raise ValueError(f"展开之后超过 {MAX_EXPANDED_STEPS} 步了")
    if len(out) > MAX_EXPANDED_STEPS:
        raise ValueError(f"展开之后超过 {MAX_EXPANDED_STEPS} 步了")
    return out


def step_groups(steps: list[dict]) -> list[list[dict]]:
    """按"同时按下"切成组：每组从一个没勾 ``with_prev`` 的步开始。"""
    groups: list[list[dict]] = []
    for step in steps:
        if groups and step.get("with_prev"):
            groups[-1].append(step)
        else:
            groups.append([step])
    return groups


def steps_duration_ms(steps: list[dict]) -> int:
    # 同一组一起开始，最晚结束的那个说了算。
    return sum(
        max(int(step.get("hold_ms", 0)) + int(step.get("gap_ms", 0)) for step in group)
        for group in step_groups(steps)
    )


def normalize_macro_library(raw) -> dict:
    """校验整份宏库：编号唯一、名字唯一、引用存在、不转圈、展开后不超限。"""
    if raw is None:
        raw = {}
    if isinstance(raw, list):
        raw = {"macros": raw}
    if not isinstance(raw, dict):
        raise ValueError("宏库必须是一个对象")
    items = raw.get("macros", [])
    if not isinstance(items, list):
        raise ValueError("macros 必须是一个数组")
    if len(items) > MAX_MACROS:
        raise ValueError(f"最多 {MAX_MACROS} 条宏")

    macros: dict[str, dict] = {}
    names: dict[str, str] = {}
    order: list[str] = []
    for item in items:
        macro = normalize_macro(item)
        if macro["id"] in macros:
            raise ValueError(f"宏编号重复了：{macro['id']}")
        # 名字是用户在下拉里唯一能看见的东西。两条同名等于让人抓阄。
        key = macro["name"].lower()
        if key in names:
            raise ValueError(f"已经有一条叫「{macro['name']}」的宏了")
        names[key] = macro["id"]
        macros[macro["id"]] = macro
        order.append(macro["id"])

    for macro_id in order:
        steps = expand_steps(macros, macro_id)
        total = steps_duration_ms(steps)
        if total > MAX_TOTAL_MS:
            raise ValueError(
                f"「{macros[macro_id]['name']}」跑完要 {total / 1000:.1f} 秒，"
                f"超过 {MAX_TOTAL_MS // 1000} 秒上限"
            )

    return {"macros": [macros[macro_id] for macro_id in order]}


def macro_summary(macro: dict, macros: dict[str, dict] | None = None) -> dict:
    """界面和手机都要的一份简介：名字、几步、跑多久、是不是循环。"""
    lookup = macros if macros is not None else {macro["id"]: macro}
    try:
        steps = expand_steps(lookup, macro["id"])
        broken = None
    except ValueError as exc:
        steps = []
        broken = str(exc)
    return {
        "id": macro["id"],
        "name": macro["name"],
        "repeat": bool(macro.get("repeat")),
        # 叫 step_count 而不是 steps：界面那边把简介盖在宏本体上用，叫 steps
        # 就会把那份真的步骤表换成一个数字，步骤编辑器直接画不出来。
        "step_count": len(macro.get("steps", [])),
        "expanded_steps": len(steps),
        "duration_ms": steps_duration_ms(steps),
        "error": broken,
    }
