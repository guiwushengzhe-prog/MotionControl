from __future__ import annotations

"""Deterministic Steam seed catalog generation for MotionControl.

The seed list is not a mapping database.  It only chooses which games the
SteamInputDB builder should attempt.  No AI is involved in selecting controls.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib import parse, request


PRIORITY_GAMES = [
    {"appid": 1174180, "name": "Red Dead Redemption 2", "priority": True},
    {"appid": 271590, "name": "Grand Theft Auto V Legacy", "priority": True},
    {"appid": 3240220, "name": "Grand Theft Auto V Enhanced", "priority": True},
    {"appid": 1293830, "name": "Forza Horizon 4", "priority": True},
    {"appid": 1551360, "name": "Forza Horizon 5", "priority": True},
    {"appid": 2358720, "name": "Black Myth: Wukong", "priority": True},
]


class SteamSearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._current: dict | None = None
        self._capture_title = False
        self._title_parts: list[str] = []

    @staticmethod
    def _classes(attrs: dict[str, str]) -> set[str]:
        return {x for x in str(attrs.get("class", "")).split() if x}

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = {str(k): str(v or "") for k, v in attrs_list}
        if tag == "a" and "search_result_row" in self._classes(attrs):
            raw_appid = attrs.get("data-ds-appid", "")
            # Some rows can contain comma-separated package/app ids. Use the
            # first positive app id only when it is unambiguous.
            match = re.match(r"\s*(\d+)", raw_appid)
            if not match:
                href = attrs.get("href", "")
                match = re.search(r"/app/(\d+)(?:/|$)", href)
            if match:
                self._current = {"appid": int(match.group(1)), "name": ""}
                self._title_parts = []
            else:
                self._current = None
        elif tag == "span" and self._current is not None and "title" in self._classes(attrs):
            self._capture_title = True
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_title and self._current is not None:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._capture_title:
            self._capture_title = False
            if self._current is not None:
                self._current["name"] = " ".join("".join(self._title_parts).split())
        elif tag == "a" and self._current is not None:
            if self._current.get("appid") and self._current.get("name"):
                self.rows.append(dict(self._current))
            self._current = None
            self._capture_title = False
            self._title_parts = []


def parse_search_html(html: str) -> list[dict]:
    parser = SteamSearchParser()
    parser.feed(html)
    return parser.rows


class SteamTopSellerClient:
    def __init__(self, *, timeout: float = 20.0, user_agent: str = "MotionControl-SeedBuilder/0.9.7") -> None:
        self.timeout = max(2.0, float(timeout))
        self.user_agent = user_agent

    def page(self, page: int) -> list[dict]:
        params = {
            "filter": "topsellers",
            "ignore_preferences": "1",
            "category1": "998",  # games
            "os": "win",
            "page": max(1, int(page)),
        }
        url = "https://store.steampowered.com/search/?" + parse.urlencode(params)
        req = request.Request(url, headers={"User-Agent": self.user_agent, "Accept-Language": "en-US,en;q=0.8"})
        with request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read(4_000_000)
        return parse_search_html(raw.decode("utf-8", errors="replace"))


def merge_seed_games(priority: list[dict], discovered: list[dict], *, limit: int = 500) -> list[dict]:
    out: list[dict] = []
    seen: set[int] = set()
    for raw in [*priority, *discovered]:
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
        item = {"appid": appid, "name": name}
        if raw.get("priority"):
            item["priority"] = True
        out.append(item)
        if limit > 0 and len(out) >= limit:
            break
    return out


def collect_top_seller_seeds(client: SteamTopSellerClient, *, count: int = 500, max_pages: int = 20) -> tuple[list[dict], dict]:
    discovered: list[dict] = []
    page_stats: list[dict] = []
    for page in range(1, max(1, int(max_pages)) + 1):
        try:
            rows = client.page(page)
            page_stats.append({"page": page, "rows": len(rows), "ok": True})
        except Exception as exc:
            page_stats.append({"page": page, "rows": 0, "ok": False, "error": str(exc)})
            # One bad page does not invalidate earlier useful seed data.
            continue
        discovered.extend(rows)
        merged = merge_seed_games(PRIORITY_GAMES, discovered, limit=count)
        if count > 0 and len(merged) >= count:
            return merged, {"pages": page_stats, "requested": count, "actual": len(merged), "target_met": True, "build_gate": False}
        if not rows:
            break
    merged = merge_seed_games(PRIORITY_GAMES, discovered, limit=count)
    return merged, {"pages": page_stats, "requested": count, "actual": len(merged), "target_met": count <= 0 or len(merged) >= count, "build_gate": False}


def write_seed_catalog(path: Path, games: list[dict], report: dict | None = None) -> None:
    payload = {
        "schema": "motioncontrol.game_profile_seeds.v1",
        "source": "steam_top_sellers_windows_plus_priority",
        "count": len(games),
        "games": games,
    }
    if report is not None:
        payload["collection"] = report
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
