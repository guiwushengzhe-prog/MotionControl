"""动作识别规则：写成数据的规则，和解释它的引擎。

官方动作库里的动作是从云端下载的，识别规则跟着一起下载。规则因此必须是**数据**，
不能是代码：下载一段 Python 回来执行，等于谁能把文件递到这台机器面前，谁就决定它
执行什么。签名能证明文件是官方发的，但不该把"能执行任意代码"这件事押在一把私钥
永远不丢上。所以规则只能用这里认得的几种运算拼起来：取关节坐标、算距离和角度、
比大小、与或非、左右对称。引擎就这么多，写不出循环，也碰不到文件和网络。

## 规则长什么样

表达式是嵌套的数组，第一个元素是运算名::

    ["<", ["y", "left_wrist"], ["-", ["y", "nose"], ["*", 0.06, ["torso"]]]]

读作"左手腕比鼻子高出 0.06 个躯干"。画面坐标 y 向下，所以"高"是 y 小。

能用的运算见 ``_OPS``。点可以是关节名（``left_knee``），也可以是两肩中点
``shoulder_mid``、两髋中点 ``hip_mid``。

## 单位

长度一律按**躯干**算：两肩中点到两髋中点的竖直距离。离镜头远近、个子高矮都会让
画面里的长度变，躯干跟着同比例变，除一下就消掉了。``lat`` 是左右方向的坐标：两肩
正中为 0，左肩 -0.5、右肩 +0.5，镜像不镜像结果都一样。

## 左右对称

``"sides": true`` 的规则写一遍、左右各算一次，任一边成立就算。规则里用
``{side}_knee``、``{other}_elbow`` 这样的写法，算左边时 ``{side}`` 是 left、
``{other}`` 是 right，算右边时反过来。每边各自的结果也交出去，原地踏步要用它判断
"这一下抬腿算不算踏步"（见 ``claims_lift``）。

## 和内核原来那几段代码逐帧一致

这里的规则是从 control_kernel 里原来写死的判断一条条搬过来的。躯干的算法、
``lat``、``dist``、``angle`` 的定义都照抄原来的，测试拿同一批骨架喂两边，要求结果
完全相同（tests/test_pose_rules.py）。改这里的定义之前先想清楚：已经发出去的规则
是按这套定义写的。

纯标准库：云端在 Linux 上也要导入它来校验规则。
"""

from __future__ import annotations

import math

# 规则语言的版本。加新运算要升这个数；电脑端遇到比自己新的规则就拒绝安装，
# 提示先更新，而不是装上一条跑不了的规则。
ENGINE_VERSION = 1

# MediaPipe 的 33 个点。规则只能引用这些名字。
JOINTS = (
    "nose", "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear", "mouth_left", "mouth_right",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_pinky", "right_pinky",
    "left_index", "right_index", "left_thumb", "right_thumb",
    "left_hip", "right_hip", "left_knee", "right_knee",
    "left_ankle", "right_ankle", "left_heel", "right_heel",
    "left_foot_index", "right_foot_index",
)
DERIVED = ("shoulder_mid", "hip_mid")
_SIDED = {name[len("left_"):] for name in JOINTS if name.startswith("left_")}

# 躯干要用的四个点至少要这么清楚。和内核原来的默认门槛一样。
TORSO_MIN_SCORE = 0.42
# 算角度时三个点里最低的那个要到这个分数，否则角度当作看不清。照抄内核。
ANGLE_MIN_SCORE = 0.4

# 防止一条规则写得离谱：再复杂的动作也用不了这么多。
MAX_NODES = 400
MAX_DEPTH = 24

# 运算名 → (最少参数, 最多参数)。None 表示不限。
_OPS: dict[str, tuple[int, int | None]] = {
    "x": (1, 1), "y": (1, 1), "lat": (1, 1),
    "torso": (0, 0),
    "dist": (2, 2), "angle": (3, 3),
    "+": (2, None), "-": (2, 2), "*": (2, None), "/": (2, 2),
    "abs": (1, 1), "min": (2, None), "max": (2, None),
    "<": (2, 2), ">": (2, 2), "<=": (2, 2), ">=": (2, 2),
    "and": (1, None), "or": (1, None), "not": (1, 1),
    "if": (3, 3),
    "rule": (1, 1),
}
_POINT_OPS = {"x", "y", "lat"}


class RuleError(ValueError):
    pass


# --- 校验 -------------------------------------------------------------------

def _check_point(name, sided: bool) -> None:
    if not isinstance(name, str):
        raise RuleError(f"这里要一个关节名，收到 {name!r}")
    if name in DERIVED or name in JOINTS:
        return
    for prefix in ("{side}_", "{other}_"):
        if name.startswith(prefix):
            if not sided:
                raise RuleError(f"{name}：只有左右对称的规则（sides）才能用 {prefix}")
            if name[len(prefix):] not in _SIDED:
                raise RuleError(f"不认识的关节：{name}")
            return
    raise RuleError(f"不认识的关节：{name}")


def check_expression(expr, *, sided: bool, known_rules: set[str] | None = None) -> int:
    """检查一个表达式能不能跑，返回节点数。不能跑就抛 RuleError，说清楚哪里不对。"""
    count = 0

    def walk(node, depth: int) -> None:
        nonlocal count
        count += 1
        if count > MAX_NODES:
            raise RuleError(f"规则太长了，最多 {MAX_NODES} 个节点")
        if depth > MAX_DEPTH:
            raise RuleError(f"规则套得太深了，最多 {MAX_DEPTH} 层")
        if isinstance(node, bool):
            raise RuleError("规则里不能直接写 true/false")
        if isinstance(node, (int, float)):
            if not math.isfinite(float(node)):
                raise RuleError("规则里的数字必须是有限的")
            return
        if not isinstance(node, list) or not node or not isinstance(node[0], str):
            raise RuleError(f"看不懂这一段：{node!r}")
        op, args = node[0], node[1:]
        if op not in _OPS:
            raise RuleError(f"不认识的运算：{op}")
        low, high = _OPS[op]
        if len(args) < low or (high is not None and len(args) > high):
            raise RuleError(f"{op} 的参数个数不对")
        if op in _POINT_OPS:
            _check_point(args[0], sided)
            return
        if op == "dist":
            _check_point(args[0], sided)
            _check_point(args[1], sided)
            return
        if op == "angle":
            for arg in args:
                _check_point(arg, sided)
            return
        if op == "rule":
            if not isinstance(args[0], str) or not args[0]:
                raise RuleError("rule 后面要跟另一个动作的编号")
            if known_rules is not None and args[0] not in known_rules:
                # 引用了别的动作，但那个动作不一定装了：不装就当它没做，不算错。
                pass
            return
        for arg in args:
            walk(arg, depth + 1)

    walk(expr, 0)
    return count


def normalize_rule(raw) -> dict:
    """校验一条识别规则，返回规范化后的副本。"""
    if not isinstance(raw, dict):
        raise RuleError("识别规则必须是一个对象")
    engine = raw.get("engine", ENGINE_VERSION)
    if not isinstance(engine, int) or isinstance(engine, bool) or engine < 1:
        raise RuleError("engine 必须是正整数")
    if engine > ENGINE_VERSION:
        raise RuleError(f"这条规则要引擎第 {engine} 版，这台电脑只有第 {ENGINE_VERSION} 版，请先更新电脑端")
    sided = bool(raw.get("sides", False))

    needs = []
    for group in raw.get("needs", []) or []:
        if not isinstance(group, dict):
            raise RuleError("needs 的每一项必须是对象")
        points = group.get("points")
        if not isinstance(points, list) or not points:
            raise RuleError("needs 的每一项要列出关节")
        for name in points:
            if name not in JOINTS:
                raise RuleError(f"needs 里不认识的关节：{name}")
        score = group.get("min_score", TORSO_MIN_SCORE)
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0.0 <= float(score) <= 1.0:
            raise RuleError("min_score 要在 0 到 1 之间")
        needs.append({"points": list(points), "min_score": float(score)})

    if "when" not in raw:
        raise RuleError("规则要有 when")
    total = check_expression(raw["when"], sided=sided)
    out = {"engine": engine, "sides": sided, "needs": needs, "when": raw["when"]}
    if "attempt" in raw:
        if not sided:
            raise RuleError("attempt 只能用在左右对称的规则里")
        total += check_expression(raw["attempt"], sided=True)
        out["attempt"] = raw["attempt"]
    if "score" in raw:
        total += check_expression(raw["score"], sided=False)
        if sided:
            raise RuleError("左右对称的规则不能有 score")
        out["score"] = raw["score"]
    if total > MAX_NODES:
        raise RuleError(f"规则太长了，最多 {MAX_NODES} 个节点")
    return out


def rule_dependencies(rule: dict, own: str | None = None) -> set[str]:
    """这条规则要先算出哪些别的规则。score 里引用自己不算：那是取自己这一帧的结果。"""
    found: set[str] = set()

    def walk(node, skip: str | None) -> None:
        if isinstance(node, list) and node:
            if node[0] == "rule":
                if str(node[1]) != skip:
                    found.add(str(node[1]))
                return
            for arg in node[1:]:
                walk(arg, skip)

    for key in ("when", "attempt"):
        if key in rule:
            walk(rule[key], None)
    if "score" in rule:
        walk(rule["score"], own)
    return found


def evaluation_order(rules: dict[str, dict]) -> list[str]:
    """先算被引用的，再算引用它的。转圈就拒绝。"""
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(ident: str, chain: tuple[str, ...]) -> None:
        if state.get(ident) == 2:
            return
        if state.get(ident) == 1:
            raise RuleError("动作规则互相引用转圈了：" + " → ".join((*chain, ident)))
        state[ident] = 1
        for dep in sorted(rule_dependencies(rules[ident], ident)):
            if dep in rules:
                visit(dep, (*chain, ident))
        state[ident] = 2
        order.append(ident)

    for ident in sorted(rules):
        visit(ident, ())
    return order


# --- 求值 -------------------------------------------------------------------

def _score(point) -> float:
    if not isinstance(point, dict):
        return 0.0
    value = point.get("score", point.get("visibility", point.get("presence", 0.0)))
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def points_good(pose_map: dict, names, minimum: float) -> bool:
    return all(name in pose_map and _score(pose_map[name]) >= minimum for name in names)


class Frame:
    """一帧骨架加上所有规则共用的量（躯干、两个中点）。"""

    def __init__(self, pose_map: dict, width: float, height: float):
        self.pose = pose_map
        self.width = float(width)
        self.height = float(height)
        self.mid: dict[str, dict] = {}
        if points_good(pose_map, ("left_shoulder", "right_shoulder"), TORSO_MIN_SCORE):
            self.mid["shoulder_mid"] = _midpoint(pose_map["left_shoulder"], pose_map["right_shoulder"])
        if points_good(pose_map, ("left_hip", "right_hip"), TORSO_MIN_SCORE):
            self.mid["hip_mid"] = _midpoint(pose_map["left_hip"], pose_map["right_hip"])
        if "shoulder_mid" in self.mid and "hip_mid" in self.mid:
            self.torso = max(0.025, abs(self.mid["hip_mid"]["y"] - self.mid["shoulder_mid"]["y"]))
        else:
            self.torso = math.nan

    def point(self, name: str, side: str | None) -> dict | None:
        if side is not None:
            other = "right" if side == "left" else "left"
            name = name.replace("{side}", side).replace("{other}", other)
        if name in self.mid:
            return self.mid[name]
        return self.pose.get(name)


def _midpoint(a: dict, b: dict) -> dict:
    return {"x": (float(a["x"]) + float(b["x"])) / 2.0, "y": (float(a["y"]) + float(b["y"])) / 2.0}


def _lateral(frame: Frame, point: dict) -> float:
    """左右方向的坐标：两肩正中为 0，左肩约 -0.5、右肩约 +0.5，镜像不变。"""
    ls, rs = frame.pose.get("left_shoulder"), frame.pose.get("right_shoulder")
    if ls is None or rs is None:
        return math.nan
    span = float(rs["x"]) - float(ls["x"])
    width = max(1e-5, abs(span))
    sign = 1.0 if span >= 0.0 else -1.0
    mid = (float(ls["x"]) + float(rs["x"])) * 0.5
    return (float(point["x"]) - mid) * sign / width


def _evaluate(node, frame: Frame, side: str | None, results: dict[str, dict]):
    if isinstance(node, (int, float)):
        return float(node)
    op, args = node[0], node[1:]
    if op in ("x", "y"):
        point = frame.point(args[0], side)
        return float(point[op]) if point is not None else math.nan
    if op == "lat":
        point = frame.point(args[0], side)
        return _lateral(frame, point) if point is not None else math.nan
    if op == "torso":
        return frame.torso
    if op == "dist":
        a, b = frame.point(args[0], side), frame.point(args[1], side)
        if a is None or b is None or not math.isfinite(frame.torso):
            return math.nan
        dx = (float(a["x"]) - float(b["x"])) * frame.width
        dy = (float(a["y"]) - float(b["y"])) * frame.height
        return math.hypot(dx, dy) / max(1e-6, frame.torso * frame.height)
    if op == "angle":
        a, b, c = (frame.point(arg, side) for arg in args)
        if a is None or b is None or c is None:
            return math.nan
        if min(_score(a), _score(b), _score(c)) < ANGLE_MIN_SCORE:
            return math.nan
        ux, uy = (float(a["x"]) - float(b["x"])) * frame.width, (float(a["y"]) - float(b["y"])) * frame.height
        vx, vy = (float(c["x"]) - float(b["x"])) * frame.width, (float(c["y"]) - float(b["y"])) * frame.height
        denominator = math.hypot(ux, uy) * math.hypot(vx, vy)
        if denominator < 1e-6:
            return math.nan
        cosine = max(-1.0, min(1.0, (ux * vx + uy * vy) / denominator))
        return math.degrees(math.acos(cosine))
    if op == "rule":
        return bool(results.get(args[0], {}).get("raw", False))
    if op == "and":
        return all(_truthy(_evaluate(arg, frame, side, results)) for arg in args)
    if op == "or":
        return any(_truthy(_evaluate(arg, frame, side, results)) for arg in args)
    if op == "not":
        return not _truthy(_evaluate(args[0], frame, side, results))
    if op == "if":
        return (_evaluate(args[1], frame, side, results)
                if _truthy(_evaluate(args[0], frame, side, results))
                else _evaluate(args[2], frame, side, results))
    values = [_evaluate(arg, frame, side, results) for arg in args]
    if op in ("<", ">", "<=", ">="):
        a, b = float(values[0]), float(values[1])
        # nan 和谁比都不成立：看不清的点不会让规则成立。
        if op == "<":
            return a < b
        if op == ">":
            return a > b
        if op == "<=":
            return a <= b
        return a >= b
    numbers = [float(value) for value in values]
    if op == "+":
        return sum(numbers)
    if op == "-":
        return numbers[0] - numbers[1]
    if op == "*":
        product = 1.0
        for number in numbers:
            product *= number
        return product
    if op == "/":
        return numbers[0] / numbers[1] if numbers[1] != 0.0 else math.nan
    if op == "abs":
        return abs(numbers[0])
    if op == "min":
        return min(numbers)
    if op == "max":
        return max(numbers)
    raise RuleError(f"不认识的运算：{op}")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return bool(value) and math.isfinite(float(value))


def evaluate_rules(rules: dict[str, dict], pose_map: dict, width: float, height: float,
                   order: list[str] | None = None) -> dict[str, dict]:
    """这一帧每条规则的结果。

    每条返回 ``{"raw": bool}``；左右对称的另有 ``sides``（每边成不成立）和
    ``attempts``（每边是不是"差一点"，只有写了 attempt 的规则才有）；写了 score 的
    另有 ``score``（保留三位小数）。躯干看不清时所有规则都不成立——没有躯干就没有
    单位。
    """
    frame = Frame(pose_map, width, height)
    results: dict[str, dict] = {}
    for ident in order if order is not None else evaluation_order(rules):
        rule = rules[ident]
        result: dict = {"raw": False}
        ready = math.isfinite(frame.torso) and all(
            points_good(pose_map, group["points"], group["min_score"]) for group in rule.get("needs", []))
        if rule.get("sides"):
            result["sides"] = {"left": False, "right": False}
            if "attempt" in rule:
                result["attempts"] = {"left": False, "right": False}
            if ready:
                for side in ("left", "right"):
                    result["sides"][side] = _truthy(_evaluate(rule["when"], frame, side, results))
                    if "attempt" in rule:
                        result["attempts"][side] = _truthy(_evaluate(rule["attempt"], frame, side, results))
                result["raw"] = any(result["sides"].values())
        elif ready:
            result["raw"] = _truthy(_evaluate(rule["when"], frame, None, results))
        # 先登记，score 里可以用 ["rule", 自己] 取这一帧自己成不成立。
        results[ident] = result
        if "score" in rule:
            result["score"] = 0.0
            if ready:
                value = _evaluate(rule["score"], frame, None, results)
                value = float(value)
                result["score"] = round(value, 3) if math.isfinite(value) else 0.0
    return results
