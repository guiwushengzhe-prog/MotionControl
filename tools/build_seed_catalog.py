from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from steam_seed_catalog import SteamTopSellerClient, collect_top_seller_seeds, write_seed_catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Steam game seed catalog for MotionControl")
    parser.add_argument("--output", type=Path, default=ROOT / "game_profiles" / "seeds_catalog.json")
    parser.add_argument("--count", type=int, default=500, help="Desired seed count; progress target only")
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args()

    games, report = collect_top_seller_seeds(SteamTopSellerClient(), count=max(1, args.count), max_pages=max(1, args.max_pages))
    write_seed_catalog(args.output, games, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Wrote {len(games)} seeds to {args.output}")
    # Do not enforce an arbitrary minimum. Partial results remain useful and can
    # be expanded later without discarding them.
    return 0 if games else 2


if __name__ == "__main__":
    raise SystemExit(main())
