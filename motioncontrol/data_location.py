"""统一定位迁移后的用户目录；位置丢失时拒绝另建一套默认数据。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from motioncontrol.config_transaction import atomic_bytes
from motioncontrol_shared.user_layout import USER_DATA_FILES

REGISTRY_KEY = r"Software\MotionControl"
MARKER = ".motioncontrol-data.json"


def _read_location():
    if os.name != "nt":
        return {}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            raw, _ = winreg.QueryValueEx(key, "DataLocation")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data["path"], str) or not Path(data["path"]).is_absolute() or not isinstance(data["id"], str) or not data["id"]:
            raise ValueError()
        return data
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("数据位置记录无法读取，请通过桌面入口重新定位原数据文件夹") from error


def _write_location(data):
    if os.name == "nt":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            winreg.SetValueEx(key, "DataLocation", 0, winreg.REG_SZ, json.dumps(data, ensure_ascii=False))


def _known_local_appdata():
    # Windows 的正式文件夹位置优先于旧终端继承的环境变量。
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "Local AppData")
                return Path(os.path.expandvars(value))
        except FileNotFoundError:
            pass
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


def _marker_id(root):
    try:
        data = json.loads((root / MARKER).read_text(encoding="utf-8-sig"))
        identity = data.get("id")
        return identity if data.get("schema") == 1 and isinstance(identity, str) and identity else None
    except (OSError, ValueError, AttributeError):
        return None


def has_user_data(root):
    return any((root / name).is_file() for name in USER_DATA_FILES.values()) or (root / MARKER).is_file()


def find_user_data(candidates, remembered=None):
    """只检查明确位置与目录身份，不按最新修改时间猜哪份绑定属于用户。"""
    remembered = remembered or {}
    paths = [Path(remembered["path"])] if remembered.get("path") else []
    paths.extend(map(Path, candidates))
    paths = list(dict.fromkeys(path.resolve() for path in paths))
    for path in paths:
        if remembered.get("id"):
            if _marker_id(path) == remembered["id"]:
                return path
        elif has_user_data(path):
            return path
    if remembered:
        raise FileNotFoundError(f"找不到原数据目录：{remembered.get('path', '')}。请通过桌面入口选择迁移后的原文件夹；没有载入默认配置。")
    return paths[0]


def locate_user_data():
    candidates = [_known_local_appdata() / "MotionControl"]
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "MotionControl")
    return find_user_data(candidates, _read_location())


def remember_user_data(root, *, require_existing=False):
    root = Path(root).resolve()
    if require_existing and not has_user_data(root):
        raise ValueError("这个文件夹没有 MotionControl 的原配置，请选择包含游戏配置或校准文件的文件夹")
    root.mkdir(parents=True, exist_ok=True)
    identity = _marker_id(root)
    if (root / MARKER).exists() and not identity:
        raise ValueError("数据目录身份记录损坏，原记录已保留，请先恢复该记录")
    if not identity:
        identity = uuid.uuid4().hex
        atomic_bytes(root / MARKER, json.dumps({"schema": 1, "id": identity}).encode("utf-8"))
    record = {"schema": 1, "id": identity, "path": str(root)}
    _write_location(record)
    return root
