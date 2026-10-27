"""飞鼠既有管理命令的校验与确认规则，不增加固件协议。"""
import math

CONTROL = "a38b0002-62da-4c13-9f07-908d8b921571"
DETAILS = "a38b0005-62da-4c13-9f07-908d8b921571"
ACTIONS = {"refresh", "always", "invert_x", "invert_y", "sensitivity", "joystick",
           "joystick_speed", "joystick_center", "touch", "calibrate_gyro", "calibrate_touch"}


def number(value, low, high, label):
    if type(value) not in (int, float) or not low < value < high or not math.isfinite(value):
        raise ValueError(f"{label}须大于{low}、小于{high}")
    return value


def validate_operation(action, value):
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("不支持这项设备操作")
    if action in {"always", "invert_x", "invert_y", "joystick"} and type(value) is not bool:
        raise ValueError("开关必须是开或关")
    if action == "sensitivity":
        number(value, 0, 1000, "体感灵敏度")
    if action == "joystick_speed":
        number(value, 0, 20000, "摇杆速度")
    if action == "touch":
        if not isinstance(value, dict) or set(value) != {"press", "release"}:
            raise ValueError("请同时填写触摸开启和关闭比例")
        number(value["press"], .1, 50, "开启比例")
        number(value["release"], .05, value["press"], "关闭比例")


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get("project") != "gunmouse":
        raise ValueError("未读到飞鼠状态，请刷新重试")
    d = snapshot.get("diag")
    def finite(values):
        return all(type(value) in (int, float) and math.isfinite(value) for value in values)
    if (not isinstance(d, dict)
            or not finite(d.get(key) for key in ("base", "raw", "tp", "tr", "noise", "invx", "invy"))
            or not 0 < d["tr"] < d["tp"]):
        raise ValueError("设备触摸或方向回报不完整，请刷新重试")
    extra = snapshot.get("details")
    if extra is not None:
        joy = extra.get("joy") if isinstance(extra, dict) else None
        if (not isinstance(joy, dict) or extra.get("project") != "gunmouse" or extra.get("schema") != 1
                or not finite(extra.get(key) for key in ("seq", "sens", "imu_ready"))
                or not finite(joy.get(key) for key in ("enabled", "ready", "rev", "speed"))):
            raise ValueError("设备扩展回报不完整，请刷新重试")


def command_plan(action, value, snapshot):
    """每条命令配套设备执行序号及目标值；不重发不确定已执行的命令。"""
    validate_operation(action, value)
    d, extra = snapshot["diag"], snapshot.get("details") or {}
    if action == "refresh":
        return []
    if snapshot.get("ota") != 0:
        raise ValueError("设备正在升级或报告通信错误，暂不能修改设置")
    if not isinstance(extra.get("seq"), int):
        raise ValueError("此固件没有操作确认回报，请先更新设备")
    seq = extra["seq"]
    plans = []
    def add(command, **expected):
        nonlocal seq
        seq = (seq + 1) & 0xffffffff
        plans.append((command, {"seq": seq, **expected}))
    if action in {"invert_x", "invert_y"}:
        key = "invx" if action == "invert_x" else "invy"
        if bool(d[key]) != value:
            add(key, **{key: int(value)})
    elif action in {"always", "joystick"}:
        key = "always" if action == "always" else "joy"
        add(f"{'imu' if action == 'always' else 'joy'} {int(value)}", **{key: int(value)})
    elif action in {"sensitivity", "joystick_speed"}:
        key = "sens" if action == "sensitivity" else "jspeed"
        add(f"{key} {value:g}", **{key: value})
    elif action == "touch":
        p, r = value["press"], value["release"]
        commands = [(f"tp {p:.2f}", {"tp": p / 100}), (f"tr {r:.2f}", {"tr": r / 100})]
        # 固件每次写入都会修正非法门限；降低开启值时先降低关闭值。
        if p <= d["tr"] * 100:
            commands.reverse()
        for command, expected in commands:
            add(command, **expected)
    else:
        if action == "joystick_center":
            if not extra.get("joy", {}).get("enabled"):
                raise ValueError("摇杆已停用，请先核对接线并启用")
            add("jcenter", jrev=(extra["joy"]["rev"] + 1) & 0xffffffff)
        else:
            add("cal" if action == "calibrate_gyro" else "tcal")
    return plans


def matches(snapshot, expected):
    details = snapshot.get("details") or {}
    joy = details.get("joy") or {}
    fields = {**snapshot["diag"], "seq": details.get("seq"), "always": details.get("always"),
              "joy": joy.get("enabled"), "jrev": joy.get("rev"),
              "jspeed": joy.get("speed"), "sens": details.get("sens")}
    return all(type(fields.get(key)) in (int, float) and abs(fields[key] - value) < .001
               for key, value in expected.items())
