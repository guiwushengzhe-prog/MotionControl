from __future__ import annotations

"""Offline Game Profile library audit for MotionControl.

This module never blocks runtime or the builder. It produces a deterministic
quality/integrity report so a partial library can be used while gaps remain
visible and retryable.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Iterable

from motioncontrol_shared.profile_schema import normalize_bindings
from motioncontrol_shared.profile_versions import CATALOG_SCHEMA, SCHEMA
from steam_seed_catalog import PRIORITY_GAMES


EXPECTED_ZONE_IDS = {
    "leftHandUpper",
    "leftHandLower",
    "rightHandUpper",
    "rightHandLower",
    "leftFoot",
    "rightFoot",
}


@dataclass(frozen=True)
class AuditIssue:
    severity: str
    code: str
    message: str
    profile_id: str = ""
    appid: int | None = None

    def as_dict(self) -> dict:
        out = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.profile_id:
            out["profile_id"] = self.profile_id
        if self.appid is not None:
            out["appid"] = self.appid
        return out


def _priority_index(priority_games: Iterable[dict] | None = None) -> dict[int, str]:
    source = PRIORITY_GAMES if priority_games is None else priority_games
    out: dict[int, str] = {}
    for item in source:
        try:
            appid = int(item.get("appid"))
        except Exception:
            continue
        name = str(item.get("name", "")).strip()
        if appid > 0:
            out[appid] = name or str(appid)
    return out


def audit_library(root: Path, *, priority_games: Iterable[dict] | None = None) -> dict:
    root = Path(root)
    library = root / "game_profiles"
    catalog_path = library / "catalog.json"
    issues: list[AuditIssue] = []
    priority = _priority_index(priority_games)

    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception as exc:
        catalog = {}
        issues.append(AuditIssue("error", "catalog_unreadable", f"catalog.json unreadable: {exc}"))

    games = catalog.get("games", []) if isinstance(catalog, dict) else []
    if catalog.get("schema") != CATALOG_SCHEMA or not isinstance(games, list):
        issues.append(AuditIssue("error", "catalog_schema", "catalog.json schema/games is invalid"))
        games = []

    seen_ids: set[str] = set()
    seen_appids: dict[int, str] = {}
    valid_profiles = 0
    generated_profiles = 0
    verified_profiles = 0
    full_zone_profiles = 0
    usable_zone_profiles = 0
    by_appid: dict[int, dict] = {}

    for raw in games:
        if not isinstance(raw, dict):
            issues.append(AuditIssue("warning", "catalog_entry_type", "non-object catalog entry ignored"))
            continue
        ident = str(raw.get("id", "")).strip()
        name = str(raw.get("name", "")).strip()
        rel = str(raw.get("profile", "")).replace("\\", "/").strip("/")
        try:
            appid = int(raw["appid"]) if raw.get("appid") is not None else None
        except Exception:
            appid = None
            issues.append(AuditIssue("warning", "appid_invalid", "catalog AppID is invalid", ident))

        if not ident or not name or not rel or ".." in rel.split("/"):
            issues.append(AuditIssue("error", "catalog_entry_invalid", "catalog entry id/name/profile is invalid", ident, appid))
            continue
        if ident in seen_ids:
            issues.append(AuditIssue("error", "duplicate_profile_id", f"duplicate profile id: {ident}", ident, appid))
            continue
        seen_ids.add(ident)
        if appid is not None:
            if appid in seen_appids:
                issues.append(AuditIssue("warning", "duplicate_appid", f"AppID also used by {seen_appids[appid]}", ident, appid))
            else:
                seen_appids[appid] = ident

        path = library / rel
        if not path.is_file():
            issues.append(AuditIssue("error", "profile_missing", f"profile file missing: {rel}", ident, appid))
            continue
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(AuditIssue("error", "profile_unreadable", f"cannot read profile JSON: {exc}", ident, appid))
            continue
        if profile.get("schema") != SCHEMA:
            issues.append(AuditIssue("error", "profile_schema", "profile schema mismatch", ident, appid))
            continue
        try:
            normalized = normalize_bindings(profile.get("bindings"))
        except Exception as exc:
            issues.append(AuditIssue("error", "profile_binding_invalid", f"binding validation failed: {exc}", ident, appid))
            continue

        valid_profiles += 1
        if str(raw.get("source", "")) != "builtin":
            generated_profiles += 1
        if bool(raw.get("verified")):
            verified_profiles += 1

        zone_ids = set(normalized.get("zones", {}))
        zone_coverage = len(zone_ids & EXPECTED_ZONE_IDS)
        if zone_coverage >= 4:
            usable_zone_profiles += 1
        else:
            issues.append(AuditIssue("warning", "zone_coverage_low", f"only {zone_coverage}/6 standard zones mapped", ident, appid))
        if zone_coverage == len(EXPECTED_ZONE_IDS):
            full_zone_profiles += 1

        if appid is not None:
            by_appid[appid] = {
                "id": ident,
                "name": name,
                "verified": bool(raw.get("verified")),
                "official": bool(raw.get("official")),
                "zone_coverage": zone_coverage,
            }

    priority_status: list[dict] = []
    for appid, expected_name in priority.items():
        entry = by_appid.get(appid)
        if entry is None:
            priority_status.append({"appid": appid, "name": expected_name, "present": False, "verified": False, "zone_coverage": 0})
            issues.append(AuditIssue("warning", "priority_missing", f"priority game missing: {expected_name}", appid=appid))
        else:
            priority_status.append({"appid": appid, "name": expected_name, "present": True, **entry})
            if not entry["verified"]:
                issues.append(AuditIssue("info", "priority_unverified", f"priority game still needs human verification: {expected_name}", entry["id"], appid))

    severity_counts = {level: sum(1 for issue in issues if issue.severity == level) for level in ("error", "warning", "info")}
    report = {
        "schema": "motioncontrol.game_profile_audit.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build_gate": False,
        "summary": {
            "catalog_entries": len(games),
            "valid_profiles": valid_profiles,
            "generated_profiles": generated_profiles,
            "verified_profiles": verified_profiles,
            "usable_zone_profiles": usable_zone_profiles,
            "full_zone_profiles": full_zone_profiles,
            "priority_total": len(priority_status),
            "priority_present": sum(1 for item in priority_status if item["present"]),
            "priority_verified": sum(1 for item in priority_status if item["verified"]),
            "issues": severity_counts,
        },
        "priority_games": priority_status,
        "issues": [issue.as_dict() for issue in issues],
    }
    return report


def write_audit_report(root: Path, report: dict) -> Path:
    path = Path(root) / "game_profiles" / "audit_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
