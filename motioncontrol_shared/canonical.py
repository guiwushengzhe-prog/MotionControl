"""Deterministic serialisation: one byte sequence per document, everywhere.

A config uploaded from Windows and downloaded onto Linux has to compare equal
byte for byte, and its SHA-256 has to match on both sides.  That only holds if
one function owns every formatting decision, so this module exposes exactly one
public entry point and keeps the serialiser private behind it.

Three details are each load-bearing:

* **Bytes out, binary in.**  Writing through Python's text layer on Windows
  turns "\n" into "\r\n" unless the caller remembers ``newline=""``.  One
  forgotten keyword and the Windows export stops matching the Linux one.
* **allow_nan=False.**  ``NaN`` and ``Infinity`` are not JSON, yet Python emits
  them happily and they would silently take part in the hash.
* **sort_keys=True.**  Otherwise the same content hashes differently depending
  on dict insertion order.

Order matters as much as format: the hash is taken *after* validation and
normalisation, never over raw input.  That is enforced by shape here rather
than by convention -- ``_canonical_json_bytes`` is private, and the only way to
reach it is ``canonicalize()``, which normalises first.  A useful consequence:
two uploads differing only in key order, whitespace, or keys that normalisation
discards produce the same hash, so neither creates a pointless new version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from . import fitness_schema
from .mapping_schema import (
    normalize_motion_item,
    normalize_voice_mappings,
)
from .motion_conflicts import validate_motion_config
from .profile_schema import normalize_overrides
from .profile_versions import (
    GAME_BUNDLE_SCHEMA,
    MOTION_MAPPINGS_SCHEMA,
    SELECTION_SCHEMA,
    VOICE_MAPPINGS_SCHEMA,
    is_selection_v1,
    migrate_selection_v1_to_v2,
)


@dataclass(frozen=True)
class Canonical:
    """A validated document, its canonical bytes, and their digest."""

    data: dict
    payload: bytes
    sha256: str


def _canonical_json_bytes(data) -> bytes:
    """Private on purpose: only canonicalize() may reach this."""
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    return (text + "\n").encode("utf-8")


def _normalize_custom_game(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("自建游戏信息无效")
    ident, name, base = (raw.get(key) for key in ("id", "name", "base"))
    appid = raw.get("appid", "")
    if (not isinstance(ident, str) or not ident.startswith("custom-") or len(ident) > 80
            or not isinstance(name, str) or not name.strip() or len(name.strip()) > 80
            or not isinstance(base, str) or not base.strip() or len(base) > 80
            or not isinstance(appid, str) or len(appid) > 12 or (appid and not appid.isdigit())):
        raise ValueError("自建游戏信息无效")
    return {"id": ident, "name": name.strip(), "base": base.strip(), "appid": appid}


def _normalize_profile_selection(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("游戏配置格式无法读取")
    data = migrate_selection_v1_to_v2(raw) if is_selection_v1(raw) else raw
    if data.get("schema") != SELECTION_SCHEMA:
        raise ValueError(f"unsupported selection schema: {data.get('schema')!r}")
    selected_id = data.get("selected_id")
    if not isinstance(selected_id, str) or not selected_id.strip():
        raise ValueError("游戏映射数据无效：selected_id")
    by_profile = data.get("overrides_by_profile")
    if not isinstance(by_profile, dict):
        raise ValueError("游戏映射数据无效：overrides_by_profile")
    result = {
        "schema": SELECTION_SCHEMA,
        "selected_id": selected_id.strip(),
        "overrides_by_profile": {
            str(profile_id): normalize_overrides(overrides)
            for profile_id, overrides in by_profile.items()
        },
    }
    if "launch_mode_by_profile" in data:
        modes = data["launch_mode_by_profile"]
        if not isinstance(modes, dict) or any(
            not isinstance(game_id, str) or not game_id.strip() or not isinstance(mode, str) or mode not in {"normal", "admin"}
            for game_id, mode in modes.items()
        ):
            raise ValueError("游戏启动权限数据无效：launch_mode_by_profile")
        result["launch_mode_by_profile"] = {game_id.strip(): mode for game_id, mode in modes.items()}
    if "custom_games" in data:
        if not isinstance(data["custom_games"], list):
            raise ValueError("自建游戏列表无效")
        games = [_normalize_custom_game(item) for item in data["custom_games"]]
        if len({item["id"] for item in games}) != len(games):
            raise ValueError("自建游戏编号重复")
        result["custom_games"] = sorted(games, key=lambda item: item["id"])
    return result


def _require_schema(data, expected: str, label: str) -> None:
    """Accept a document with no schema as v1; reject a wrong one outright.

    The desktop has never written a schema field into these two files, so
    demanding one would make every existing installation's config unuploadable.
    A *present but unknown* value is a different matter: it means the file came
    from a newer build whose rules we do not have, and guessing at it is how a
    config gets silently downgraded.
    """
    schema = data.get("schema")
    if schema is not None and schema != expected:
        raise ValueError(f"unsupported {label} schema: {schema!r}")


def _normalize_motion_mappings(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("动作映射格式无法读取")
    _require_schema(raw, MOTION_MAPPINGS_SCHEMA, "motion mappings")
    items = raw.get("motions")
    if not isinstance(items, list):
        raise ValueError("动作映射数据无效：motions")
    motions = [normalize_motion_item(item) for item in items]
    seen = set()
    for motion in motions:
        if motion["id"] in seen:
            raise ValueError(f"动作 id 重复：{motion['id']}")
        seen.add(motion["id"])
    # Sorted, not as given -- see the ordering note in mapping_schema.
    motions.sort(key=lambda motion: motion["id"])
    validate_motion_config(motions)
    return {"schema": MOTION_MAPPINGS_SCHEMA, "motions": motions}


def _normalize_voice_mappings(raw) -> dict:
    """只留"说什么话按什么键"。唤醒词和急停口令不进可分享的配置。

    分享一份语音配置，分的是口令和它对应的键；唤醒词是发布者个人的习惯。
    以前它跟着一起走，于是下载安装一份别人的配置会把自己的唤醒词换掉——装的
    人只会发现"我的唤醒词自己变了"，想不到是装配置装的。

    旧文档里还带着这两项。这里**收下但丢掉**，不报错：报错会让每一份已经
    传上去的配置变成无法重传的废文件。
    """
    if not isinstance(raw, dict):
        raise ValueError("语音映射格式无法读取")
    _require_schema(raw, VOICE_MAPPINGS_SCHEMA, "voice mappings")
    return {
        "schema": VOICE_MAPPINGS_SCHEMA,
        # Order preserved: first match wins at recognition time.
        "mappings": normalize_voice_mappings(raw.get("mappings", [])),
    }


def _normalize_game_bundle(raw) -> dict:
    """一个游戏的全部配置，合成一份。

    以前一份「我的 GTA5 配置」要分成两个包发出去，别人也要分两次装——而按键映射和
    身体动作本来就是同一件事：都是"在这个游戏里，我这么玩"。

    里面没有唤醒词、没有急停口令，也没有任何跟机器走的东西。那些是发布者个人
    的，装到别人机器上只会把人家原来的换掉（见 _normalize_voice_mappings）。
    """
    if not isinstance(raw, dict):
        raise ValueError("游戏方案格式无法读取")
    _require_schema(raw, GAME_BUNDLE_SCHEMA, "game bundle")
    game_id = raw.get("game_id")
    if not isinstance(game_id, str) or not game_id.strip():
        raise ValueError("游戏方案必须说清楚是哪个游戏：game_id")
    motions_raw = raw.get("motions", [])
    if not isinstance(motions_raw, list):
        raise ValueError("游戏方案数据无效：motions")
    motions = [normalize_motion_item(item) for item in motions_raw]
    seen = set()
    for motion in motions:
        if motion["id"] in seen:
            raise ValueError(f"动作 id 重复：{motion['id']}")
        seen.add(motion["id"])
    motions.sort(key=lambda motion: motion["id"])
    validate_motion_config(motions)
    result = {
        "schema": GAME_BUNDLE_SCHEMA,
        "game_id": game_id.strip(),
        "overrides": normalize_overrides(raw.get("overrides", {})),
        "motions": motions,
    }
    if "launch_mode" in raw:
        if not isinstance(raw["launch_mode"], str) or raw["launch_mode"] not in {"normal", "admin"}:
            raise ValueError("游戏启动权限只能是普通或管理员")
        result["launch_mode"] = raw["launch_mode"]
    if "custom_game" in raw:
        game = _normalize_custom_game(raw["custom_game"])
        if game["id"] != result["game_id"]:
            raise ValueError("自建游戏编号与方案不一致")
        result["custom_game"] = game
    return result


# Every document type the canonical form knows about.  An unknown type is an
# error, not a pass-through: that is what keeps un-normalised data from ever
# reaching the hash.
_NORMALIZERS = {
    "profile_selection": _normalize_profile_selection,
    "motion_mappings": _normalize_motion_mappings,
    "voice_mappings": _normalize_voice_mappings,
    "game_bundle": _normalize_game_bundle,
}

# 不是可以上传、分享的配置，而是一个人自己的记录：运动记录的身体数据和每次锻炼的
# 摘要（规则在 fitness_schema）。单独放一张表，云端配置接口收的文档类型就还是上面那几种。
_RECORD_NORMALIZERS = {
    "fitness_profile": fitness_schema.normalize_profile,
    "fitness_session": fitness_schema.normalize_session,
}


def canonicalize(doc_type: str, raw) -> Canonical:
    """Validate, normalise, serialise, digest -- in that order, always."""
    try:
        normalizer = _NORMALIZERS.get(doc_type) or _RECORD_NORMALIZERS[doc_type]
    except KeyError:
        raise ValueError(f"unknown document type: {doc_type!r}") from None
    data = normalizer(raw)
    payload = _canonical_json_bytes(data)
    return Canonical(data=data, payload=payload, sha256=sha256(payload).hexdigest())
