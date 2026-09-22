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

from .mapping_schema import (
    normalize_motion_item,
    normalize_voice_mappings,
)
from .motion_conflicts import validate_motion_config
from .profile_schema import normalize_overrides
from .profile_versions import (
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
    return {
        "schema": SELECTION_SCHEMA,
        "selected_id": selected_id.strip(),
        "overrides_by_profile": {
            str(profile_id): normalize_overrides(overrides)
            for profile_id, overrides in by_profile.items()
        },
    }


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


# Every document type the canonical form knows about.  An unknown type is an
# error, not a pass-through: that is what keeps un-normalised data from ever
# reaching the hash.
_NORMALIZERS = {
    "profile_selection": _normalize_profile_selection,
    "motion_mappings": _normalize_motion_mappings,
    "voice_mappings": _normalize_voice_mappings,
}


def canonicalize(doc_type: str, raw) -> Canonical:
    """Validate, normalise, serialise, digest -- in that order, always."""
    try:
        normalizer = _NORMALIZERS[doc_type]
    except KeyError:
        raise ValueError(f"unknown document type: {doc_type!r}") from None
    data = normalizer(raw)
    payload = _canonical_json_bytes(data)
    return Canonical(data=data, payload=payload, sha256=sha256(payload).hexdigest())
