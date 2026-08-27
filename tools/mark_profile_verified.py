from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _find_entry(games: list[dict], selector: str) -> dict:
    selector = str(selector).strip()
    if not selector:
        raise ValueError("profile selector must not be empty")
    numeric = int(selector) if selector.isdigit() else None
    matches = []
    for item in games:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")) == selector:
            matches.append(item)
            continue
        if numeric is not None:
            try:
                if int(item.get("appid")) == numeric:
                    matches.append(item)
            except Exception:
                pass
    if not matches:
        raise KeyError(f"profile not found: {selector}")
    if len({str(x.get('id')) for x in matches}) > 1:
        raise ValueError(f"selector is ambiguous: {selector}")
    return matches[0]


def set_verified(root: Path, selector: str, *, verified: bool, note: str = "", verifier: str = "human") -> dict:
    catalog_path = Path(root) / "game_profiles" / "catalog.json"
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    games = data.get("games")
    if not isinstance(games, list):
        raise ValueError("invalid game catalog")
    item = _find_entry(games, selector)
    item["verified"] = bool(verified)
    if verified:
        item["verification"] = {
            "by": str(verifier).strip() or "human",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "note": str(note).strip(),
        }
    else:
        item.pop("verification", None)
    catalog_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(item)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mark one MotionControl Game Profile as human verified")
    parser.add_argument("selector", help="Steam AppID or profile id")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--note", default="", help="Short human verification note")
    parser.add_argument("--by", default="human", help="Verifier label")
    parser.add_argument("--unverify", action="store_true", help="Clear human verification")
    args = parser.parse_args()
    try:
        item = set_verified(args.root, args.selector, verified=not args.unverify, note=args.note, verifier=args.by)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(item, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
