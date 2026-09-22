"""键盘宏库：``key_macros.json`` 的读写，以及给运行时用的展开。

宏库是**全局**的，不跟游戏走。理由就是用户提的那句"建一次，其他地方直接选用"：
跟着游戏存的话，换个游戏就得重建一遍，那和多写几个键没区别。

校验规则一条都不在这里，全在 ``motioncontrol_shared.macro_schema``。这边只管
文件在哪、怎么原子写、编号怎么发。云端将来要认宏时导入的是同一份规则，不会出现
电脑存得进去、云端存不进去这种事。

## 展开为什么在用的时候做，不在存的时候做

一条宏可以引用另一条。如果存盘时就把引用内联掉，被引用那条以后改了，引用它的那条
还停在旧内容上——而界面上它看起来仍然写着"包含：三连击"。所以盘上永远存**引用**，
每次要跑之前才展开。展开很便宜（最多几十步），而"改一处到处生效"是引用这个功能
本身的意义。

存盘时仍然要走一遍展开，但那是为了**拒绝**：转圈、套太深、跑起来超过十秒的，当场
说清楚不让存。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from motioncontrol_shared.macro_schema import (
    MAX_MACROS,
    expand_steps,
    macro_summary,
    normalize_macro,
    normalize_macro_library,
    steps_duration_ms,
)

SCHEMA = "motioncontrol.key_macros.v1"


class MacroError(Exception):
    pass


class MacroStore:
    """线程安全交给调用方，和 CustomPoseStore 一样：桌面这边所有配置写入都在
    ``PROFILE_UPDATE_LOCK`` 下，这里再加一把锁只会多一个死锁的机会。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.macros: list[dict] = []
        self.last_error = ""
        self._load()

    # --- 读写 ---------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏文件不该让程序起不来
            self.last_error = f"键盘宏读取失败：{exc}"
            return
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            self.last_error = "键盘宏文件版本不认识，已忽略"
            return
        try:
            self.macros = normalize_macro_library(data).get("macros", [])
        except ValueError as exc:
            # 存盘时已经校验过，还能读出问题就是文件被手改过或损坏了。丢掉比带着
            # 一份会转圈的宏库跑起来安全——那种宏一触发就是无限展开。
            self.last_error = f"键盘宏文件有问题，已忽略：{exc}"
            self.macros = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema": SCHEMA, "macros": self.macros},
                             ensure_ascii=False, indent=2)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        os.replace(temp, self.path)

    # --- 查 -----------------------------------------------------------------

    def by_id(self) -> dict[str, dict]:
        return {macro["id"]: macro for macro in self.macros}

    def get(self, macro_id: str) -> dict | None:
        macro_id = str(macro_id or "").strip().lower()
        for macro in self.macros:
            if macro["id"] == macro_id:
                return macro
        return None

    def expanded(self, macro_id: str) -> list[dict]:
        """一条宏跑起来实际要按的那串键。引用失效时返回空表，不抛。

        不抛是故意的：这条路上的调用方是输出后端，它在按键的热路径上。引用丢了
        就什么都不按——一个不响的绑定，用户在界面上看得见「宏已丢失」；抛异常则会
        把整组绑定的刷新一起打断，那才是真的坏。
        """
        try:
            return expand_steps(self.by_id(), str(macro_id or "").strip().lower())
        except ValueError:
            return []

    def repeats(self, macro_id: str) -> bool:
        macro = self.get(macro_id)
        return bool(macro and macro.get("repeat"))

    def status(self) -> list[dict]:
        lookup = self.by_id()
        return [macro_summary(macro, lookup) for macro in self.macros]

    def detail(self) -> list[dict]:
        """界面要的完整内容：定义加上算好的展开信息。"""
        lookup = self.by_id()
        out = []
        for macro in self.macros:
            summary = macro_summary(macro, lookup)
            out.append({**macro, **summary})
        return out

    # --- 增删改 -------------------------------------------------------------

    def _next_id(self) -> str:
        used = {macro["id"] for macro in self.macros}
        while True:
            # 随机而不是递增：递增编号在"删掉第 2 条再建一条"之后会把新宏发成
            # 老编号，于是旧配置里指向已删宏的绑定会突然指到一条毫不相干的新宏上。
            # 那是最难查的一类 bug——它不报错，只是按错键。
            candidate = "m" + uuid.uuid4().hex[:8]
            if candidate not in used:
                return candidate

    def _commit(self, macros: list[dict]) -> None:
        """校验整库再落盘。单条校验不够：转圈只有看全库才看得出来。"""
        try:
            normalized = normalize_macro_library({"macros": macros})
        except ValueError as exc:
            raise MacroError(str(exc)) from exc
        self.macros = normalized["macros"]
        self._save()
        self.last_error = ""

    def create(self, name: str = "", steps=None, repeat: bool = False) -> dict:
        if len(self.macros) >= MAX_MACROS:
            raise MacroError(f"最多 {MAX_MACROS} 条宏，先删掉几条")
        macro_id = self._next_id()
        raw = {
            "id": macro_id,
            "name": (str(name).strip() or self._next_name()),
            "repeat": bool(repeat),
            "steps": steps or [{"type": "keyboard", "target": "SPACE"}],
        }
        try:
            macro = normalize_macro(raw)
        except ValueError as exc:
            raise MacroError(str(exc)) from exc
        self._commit([*self.macros, macro])
        return self.get(macro_id)

    def _next_name(self) -> str:
        used = {macro["name"] for macro in self.macros}
        index = 1
        while f"宏 {index}" in used:
            index += 1
        return f"宏 {index}"

    def update(self, macro_id: str, **changes) -> dict:
        macro_id = str(macro_id or "").strip().lower()
        current = self.get(macro_id)
        if current is None:
            raise MacroError("找不到这条宏")
        raw = dict(current)
        for field in ("name", "repeat", "steps"):
            if field in changes and changes[field] is not None:
                raw[field] = changes[field]
        try:
            macro = normalize_macro(raw)
        except ValueError as exc:
            raise MacroError(str(exc)) from exc
        self._commit([macro if item["id"] == macro_id else item for item in self.macros])
        return self.get(macro_id)

    def remove(self, macro_id: str) -> bool:
        macro_id = str(macro_id or "").strip().lower()
        if self.get(macro_id) is None:
            return False
        # 别的宏还引用着它就不让删。删掉再展开会直接报"找不到被引用的宏"，那时候
        # 用户已经不记得刚才删了什么了。当场说清楚哪几条在用它，比事后报错有用。
        users = [macro["name"] for macro in self.macros
                 if macro["id"] != macro_id
                 and any(step["type"] == "macro" and step["target"] == macro_id
                         for step in macro["steps"])]
        if users:
            raise MacroError("「" + "」「".join(users) + "」还在用这条宏，先把它们改掉")
        self._commit([item for item in self.macros if item["id"] != macro_id])
        return True

    # --- 给绑定用 -----------------------------------------------------------

    def describe(self, macro_id: str) -> dict:
        """一条绑定指向的宏长什么样。丢了也要给出个东西，界面照实说。"""
        macro = self.get(macro_id)
        if macro is None:
            return {"id": str(macro_id or ""), "name": "", "missing": True,
                    "repeat": False, "step_count": 0, "duration_ms": 0}
        steps = self.expanded(macro["id"])
        return {
            "id": macro["id"],
            "name": macro["name"],
            "missing": False,
            "repeat": bool(macro.get("repeat")),
            "step_count": len(steps),
            "duration_ms": steps_duration_ms(steps),
        }
