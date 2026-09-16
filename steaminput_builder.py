from __future__ import annotations

"""SteamInputDB -> MotionControl offline Game Profile builder.

Network access is used only at build time.  Runtime remains fully offline and
lazy-loads compact normalized JSON profiles.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time
from typing import Iterable
from urllib import parse, request

from motioncontrol_shared.profile_schema import normalize_action
from motioncontrol_shared.profile_versions import CATALOG_SCHEMA, SCHEMA
from steam_vdf import ParsedBinding, binding_summary, extract_physical_bindings


API_BASE = "https://api.steaminputdb.com"
TARGET_PROFILE_COUNT = 300  # progress target only; never a build-failure gate.

ZONE_FROM_PHYSICAL = {
    "button_y": "leftHandUpper",
    "button_x": "leftHandLower",
    "button_b": "rightHandUpper",
    "button_a": "rightHandLower",
    "left_bumper": "leftFoot",
    "right_bumper": "rightFoot",
}

ZONE_LABELS = {
    "leftHandUpper": "左手上区",
    "leftHandLower": "左手下区",
    "rightHandUpper": "右手上区",
    "rightHandLower": "右手下区",
    "leftFoot": "左脚区",
    "rightFoot": "右脚区",
}

VOICE_SLOT_IDS = [f"game.profile_slot_{i:02d}" for i in range(1, 13)]

# Only labels with clear body semantics may claim an automatic motion.  All
# other deterministic outputs remain available through neutral voice slots;
# do not infer game meaning from a bare key name.
MOTION_SEMANTICS = [
    ("march", ("run", "sprint", "jog", "move forward", "accelerate", "跑", "冲刺", "前进", "加速")),
    ("calf_back", ("dodge", "evade", "roll", "dash", "闪避", "躲避", "翻滚")),
    ("squat", ("crouch", "duck", "sneak", "下蹲", "蹲", "潜行")),
    ("hands_up", ("block", "guard", "parry", "defend", "格挡", "防御", "招架")),
]


def _action_key(action: dict) -> tuple[str, str]:
    return str(action.get("type", "")), str(action.get("target", ""))


def _semantic_motion(label: str) -> str | None:
    text = str(label or "").casefold()
    if not text:
        return None
    for motion_id, keywords in MOTION_SEMANTICS:
        if any(str(keyword).casefold() in text for keyword in keywords):
            return motion_id
    return None


class SteamInputDBError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeedGame:
    appid: int
    name: str
    profile_id: str = ""
    priority: bool = False


@dataclass
class BuildResult:
    seed: SeedGame
    usable: bool
    profile: dict | None
    reason: str = ""
    candidate: dict | None = None


class SteamInputDBClient:
    def __init__(self, base_url: str = API_BASE, *, timeout: float = 20.0, user_agent: str = "MotionControl-ProfileBuilder/0.9.7") -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = max(2.0, float(timeout))
        self.user_agent = user_agent

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except Exception as exc:
            raise SteamInputDBError(f"SteamInputDB request failed: {url}: {exc}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise SteamInputDBError(f"SteamInputDB returned invalid JSON: {url}") from exc
        if not isinstance(parsed, dict):
            raise SteamInputDBError(f"SteamInputDB returned non-object JSON: {url}")
        return parsed

    def app_info(self, appid: int) -> dict:
        qs = parse.urlencode({"app_id": int(appid), "official_configs": "true", "controller_support": "true"})
        return self._json("GET", "/v1/steam/appinfo?" + qs)

    def search_configs(self, appid: int, game_name: str, *, limit: int = 25) -> list[dict]:
        payload = {
            # AppID is the authoritative selector. A title text query can
            # accidentally filter valid layouts when Steam/SteamInputDB uses a
            # localized, legacy or renamed title (for example GTA editions).
            "query_text": "",
            "limit": max(1, min(100, int(limit))),
            "page": 0,
            "filter": {"app_id": str(int(appid))},
            "rank": {"by": "total_playtime"},
            "include": {"votes": True, "tags": True},
            "raw": False,
        }
        data = self._json("POST", "/v1/search/configs", payload)
        items = data.get("items", [])
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def file_details(self, file_id: int) -> dict:
        qs = parse.urlencode({"file_id": int(file_id), "playtime_stats": 30, "raw": "false"})
        return self._json("GET", "/v1/steam/filedetails?" + qs)

    def download_vdf(self, url: str) -> str:
        url = str(url).strip()
        if not url.lower().startswith(("https://", "http://")):
            raise SteamInputDBError("config file URL is missing or invalid")
        req = request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "text/plain,*/*"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read(2_000_000)
        except Exception as exc:
            raise SteamInputDBError(f"failed to download VDF: {exc}") from exc
        for encoding in ("utf-8-sig", "utf-8", "cp1252"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")


def _candidate_metric(candidate: dict) -> tuple:
    official = 1 if candidate.get("official") else 0
    playtime = int(candidate.get("lifetime_playtime_seconds") or candidate.get("playtime_seconds") or 0)
    subscriptions = int(candidate.get("subscriptions") or 0)
    votes = candidate.get("votes") if isinstance(candidate.get("votes"), dict) else {}
    up = int(votes.get("up") or 0)
    down = int(votes.get("down") or 0)
    vote_score = float(votes.get("score") or 0.0)
    # A tiny Bayesian-like quality term prevents one isolated upvote from beating
    # a config with materially more real playtime/downloads.
    vote_confidence = (up + 1.0) / (up + down + 2.0)
    return (official, playtime, subscriptions, vote_confidence, vote_score, up)


def rank_candidates(candidates: Iterable[dict]) -> list[dict]:
    return sorted((dict(x) for x in candidates if isinstance(x, dict)), key=_candidate_metric, reverse=True)


def _slug(text: str) -> str:
    text = str(text).strip().casefold()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:48] or "game"


def _profile_id(seed: SeedGame) -> str:
    return seed.profile_id.strip() or f"steam-{seed.appid}-{_slug(seed.name)}"


def _candidate_from_official(app_info: dict) -> list[dict]:
    official = app_info.get("official_configs")
    if not isinstance(official, dict):
        return []
    out = []
    for controller_type, file_id in official.items():
        try:
            fid = int(file_id)
        except Exception:
            continue
        if fid <= 0:
            continue
        out.append({
            "official": True,
            "file_id": fid,
            "controller_type": str(controller_type),
            "title": "Official Steam Input",
        })
    return out


def collect_candidates(client: SteamInputDBClient, seed: SeedGame, *, community_limit: int = 25) -> list[dict]:
    candidates: list[dict] = []
    try:
        candidates.extend(_candidate_from_official(client.app_info(seed.appid)))
    except Exception:
        # Official metadata is preferred, not mandatory. Community layouts can
        # still produce a valid deterministic profile.
        pass
    try:
        for item in client.search_configs(seed.appid, seed.name, limit=community_limit):
            candidate = dict(item)
            candidate["official"] = False
            candidates.append(candidate)
    except Exception:
        if not candidates:
            raise
    # Deduplicate the same Steam published file before ranking.
    dedup: dict[str, dict] = {}
    for item in candidates:
        key = str(item.get("file_id") or item.get("file_url") or json.dumps(item, sort_keys=True, default=str))
        old = dedup.get(key)
        if old is None or _candidate_metric(item) > _candidate_metric(old):
            dedup[key] = item
    return rank_candidates(dedup.values())


def _ensure_candidate_details(client: SteamInputDBClient, candidate: dict) -> dict:
    candidate = dict(candidate)
    if candidate.get("file_url"):
        return candidate
    file_id = candidate.get("file_id")
    if not file_id:
        return candidate
    details = client.file_details(int(file_id))
    for key, value in details.items():
        if value is not None:
            candidate[key] = value
    candidate.setdefault("official", bool(details.get("official", candidate.get("official", False))))
    return candidate


def profile_from_vdf(seed: SeedGame, vdf_text: str, candidate: dict) -> dict:
    physical = extract_physical_bindings(vdf_text)
    zones: dict[str, dict] = {}
    rejected: dict[str, str] = {}
    for physical_name, zone_id in ZONE_FROM_PHYSICAL.items():
        binding: ParsedBinding | None = physical.get(physical_name)
        if binding is None:
            rejected[physical_name] = "binding not found"
            continue
        if binding.action is None:
            rejected[physical_name] = binding.reason or "binding not convertible"
            continue
        try:
            action = normalize_action(binding.action)
        except Exception as exc:
            rejected[physical_name] = str(exc)
            continue
        # Zones represent an occupancy state. Wheel is intrinsically an impulse;
        # MotionControl will fire it once when the zone is entered.
        if action["type"] == "mouse_wheel":
            action["behavior"] = "tap"
        zones[zone_id] = {
            "label": binding.label or ZONE_LABELS.get(zone_id, zone_id),
            "action": action,
        }

    # Allocate additional exact outputs after the six primary Zones.  A body
    # motion is used only when Steam's own label clearly describes a matching
    # body intent. Unknown/low-frequency functions go to stable voice slots.
    motions: dict[str, dict] = {}
    poses: dict[str, dict] = {}
    voice: dict[str, dict] = {}
    used_actions = {
        _action_key(binding["action"])
        for binding in zones.values()
        if isinstance(binding.get("action"), dict)
    }
    voice_index = 0
    for physical_name, binding in physical.items():
        if physical_name in ZONE_FROM_PHYSICAL or binding.action is None:
            continue
        try:
            action = normalize_action(binding.action)
        except Exception:
            continue
        action["behavior"] = "tap"
        key = _action_key(action)
        if key in used_actions:
            continue
        label = str(binding.label or "").strip()
        motion_id = _semantic_motion(label)
        if motion_id and motion_id not in motions:
            motions[motion_id] = {"label": label or motion_id, "action": {**action, "behavior": "hold"}}
            used_actions.add(key)
            continue
        if voice_index < len(VOICE_SLOT_IDS):
            slot_id = VOICE_SLOT_IDS[voice_index]
            voice_index += 1
            display = label or f"游戏功能 {voice_index} · {action.get('target', '')}"
            voice[slot_id] = {"label": display, "action": action}
            used_actions.add(key)

    coverage = len(zones)
    source = {
        "kind": "steaminputdb",
        "official": bool(candidate.get("official")),
        "file_id": candidate.get("file_id"),
        "controller_type": candidate.get("controller_type"),
        "controller_type_nice": candidate.get("controller_type_nice"),
        "layout_title": candidate.get("title"),
        "subscriptions": candidate.get("subscriptions"),
        "lifetime_playtime_seconds": candidate.get("lifetime_playtime_seconds"),
        "votes": candidate.get("votes") if isinstance(candidate.get("votes"), dict) else {},
    }
    profile = {
        "schema": SCHEMA,
        "id": _profile_id(seed),
        "name": seed.name,
        "appid": seed.appid,
        "source": source,
        "quality": {
            "usable": coverage >= 4,
            "zone_coverage": coverage,
            "zone_total": len(ZONE_FROM_PHYSICAL),
            "coverage_ratio": round(coverage / len(ZONE_FROM_PHYSICAL), 3),
            "rejected": rejected,
            "parser": binding_summary(physical),
            "motion_count": len(motions),
            "voice_count": len(voice),
        },
        "bindings": {"zones": zones, "motions": motions, "poses": poses, "voice": voice},
    }
    return profile


def build_one(client: SteamInputDBClient, seed: SeedGame, *, community_limit: int = 25) -> BuildResult:
    try:
        candidates = collect_candidates(client, seed, community_limit=community_limit)
    except Exception as exc:
        return BuildResult(seed, False, None, f"candidate lookup failed: {exc}")
    if not candidates:
        return BuildResult(seed, False, None, "no Steam Input candidates")

    reasons: list[str] = []
    # Try ranked candidates until one actually parses into a useful profile.
    for raw_candidate in candidates:
        try:
            candidate = _ensure_candidate_details(client, raw_candidate)
            file_url = candidate.get("file_url")
            if not file_url:
                reasons.append(f"file {candidate.get('file_id')}: missing file_url")
                continue
            vdf = client.download_vdf(str(file_url))
            profile = profile_from_vdf(seed, vdf, candidate)
            if profile["quality"]["usable"]:
                return BuildResult(seed, True, profile, candidate=candidate)
            reasons.append(
                f"file {candidate.get('file_id')}: only {profile['quality']['zone_coverage']}/6 zones convertible"
            )
        except Exception as exc:
            reasons.append(f"file {raw_candidate.get('file_id')}: {exc}")
    return BuildResult(seed, False, None, "; ".join(reasons[-5:]) or "no usable candidate")


def existing_profile_appids(root: Path) -> set[int]:
    """Return AppIDs that already have a valid catalog-backed offline profile."""
    root = Path(root)
    library = root / "game_profiles"
    catalog_path = library / "catalog.json"
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if data.get("schema") != CATALOG_SCHEMA or not isinstance(data.get("games"), list):
        return set()
    out: set[int] = set()
    for item in data["games"]:
        if not isinstance(item, dict):
            continue
        try:
            appid = int(item.get("appid"))
        except Exception:
            continue
        rel = str(item.get("profile", "")).replace("\\", "/").strip("/")
        if appid <= 0 or not rel or ".." in rel.split("/"):
            continue
        if (library / rel).is_file():
            out.add(appid)
    return out


def load_seeds(path: Path) -> list[SeedGame]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("games") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("seed file must be a list or an object with games[]")
    out: list[SeedGame] = []
    seen: set[int] = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        try:
            appid = int(raw.get("appid"))
        except Exception:
            continue
        name = str(raw.get("name", "")).strip()
        if appid <= 0 or not name or appid in seen:
            continue
        seen.add(appid)
        out.append(SeedGame(appid, name, str(raw.get("id", "")).strip(), bool(raw.get("priority"))))
    return out


def save_library(root: Path, results: Iterable[BuildResult], *, target_count: int = TARGET_PROFILE_COUNT) -> dict:
    root = Path(root)
    library = root / "game_profiles"
    profiles_dir = library / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)

    # Batch building is intentionally incremental. Keep every previously valid
    # catalog entry, then replace only entries produced again in this batch.
    catalog_path = library / "catalog.json"
    existing_by_id: dict[str, dict] = {}
    try:
        old = json.loads(catalog_path.read_text(encoding="utf-8"))
        if old.get("schema") == CATALOG_SCHEMA and isinstance(old.get("games"), list):
            for item in old["games"]:
                if not isinstance(item, dict):
                    continue
                ident = str(item.get("id", "")).strip()
                profile_rel = str(item.get("profile", "")).replace("\\", "/").strip("/")
                if not ident or not profile_rel or ".." in profile_rel.split("/"):
                    continue
                profile_path = library / profile_rel
                # Do not preserve catalog ghosts whose profile file disappeared.
                if profile_path.is_file():
                    existing_by_id[ident] = dict(item)
    except Exception:
        pass

    batch_success = 0
    failures: list[dict] = []
    touched_ids: set[str] = set()
    for result in results:
        profile_id = _profile_id(result.seed)
        touched_ids.add(profile_id)
        if result.usable and result.profile is not None:
            profile = result.profile
            filename = f"{profile['id']}.json"
            (profiles_dir / filename).write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
            previous = existing_by_id.get(profile["id"], {})
            item = {
                "id": profile["id"],
                "name": profile["name"],
                "appid": profile["appid"],
                "profile": f"profiles/{filename}",
                "source": "steaminputdb",
                # Human verification is higher-value evidence than a later
                # automated refresh. Never erase it just because SteamInputDB
                # produced a newer normalized profile.
                "verified": bool(previous.get("verified", False)),
                "zone_coverage": profile.get("quality", {}).get("zone_coverage", 0),
                "official": bool(profile.get("source", {}).get("official")),
            }
            if result.seed.priority or previous.get("priority"):
                item["priority"] = True
            if previous.get("verification") is not None:
                item["verification"] = previous["verification"]
            existing_by_id[profile["id"]] = item
            batch_success += 1
        else:
            # A failed refresh must not delete a previously working profile. The
            # failure belongs in the report and can be retried in a later batch.
            failures.append({"appid": result.seed.appid, "name": result.seed.name, "reason": result.reason})

    builtin = [x for ident, x in existing_by_id.items() if ident == "generic-xbox"]
    generated = sorted(
        (x for ident, x in existing_by_id.items() if ident != "generic-xbox"),
        key=lambda x: (str(x.get("name", "")).casefold(), int(x.get("appid") or 0)),
    )
    catalog_games = builtin + generated
    catalog = {"schema": CATALOG_SCHEMA, "count": len(catalog_games), "games": catalog_games}
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    total_generated = len(generated)
    report = {
        "schema": "motioncontrol.game_profile_build_report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batch_usable": batch_success,
        "batch_failed": len(failures),
        "library_generated_count": total_generated,
        "library_count": len(catalog_games),
        "target_count": int(target_count),
        "target_progress": round(min(1.0, total_generated / max(1, int(target_count))), 4),
        "target_met": total_generated >= int(target_count),
        "build_gate": False,
        "failures": failures,
    }
    (library / "build_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
