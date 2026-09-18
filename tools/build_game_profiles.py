from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from motioncontrol.steaminput_builder import (
    SteamInputDBClient,
    TARGET_PROFILE_COUNT,
    build_one,
    existing_profile_appids,
    load_seeds,
    save_library,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build MotionControl offline Game Profiles from SteamInputDB")
    parser.add_argument("seed_file", type=Path, help="JSON seed list: [{appid,name}, ...] or {games:[...]}")
    parser.add_argument("--root", type=Path, default=ROOT, help="MotionControl project root")
    parser.add_argument("--limit", type=int, default=0, help="Only process first N pending seeds (0 = all)")
    parser.add_argument("--community-limit", type=int, default=25, help="SteamInputDB community candidates per game")
    parser.add_argument("--delay", type=float, default=0.12, help="Polite delay between games")
    parser.add_argument("--target-count", type=int, default=TARGET_PROFILE_COUNT, help="Progress target only; does not fail the build")
    parser.add_argument("--checkpoint-every", type=int, default=10, help="Persist progress after this many processed games")
    parser.add_argument("--refresh-existing", action="store_true", help="Re-query games that already have a valid offline Profile")
    args = parser.parse_args()

    seeds = load_seeds(args.seed_file)
    if not seeds:
        print("No valid seeds found", file=sys.stderr)
        return 2

    existing = existing_profile_appids(args.root) if not args.refresh_existing else set()
    pending = [seed for seed in seeds if seed.appid not in existing]
    skipped_existing = len(seeds) - len(pending)
    if args.limit > 0:
        pending = pending[: args.limit]

    if not pending:
        report = save_library(args.root, [], target_count=args.target_count)
        run_report = dict(report)
        run_report.update({
            "schema": "motioncontrol.game_profile_build_run.v1",
            "run_finished_at": datetime.now(timezone.utc).isoformat(),
            "run_processed": 0,
            "run_usable": 0,
            "run_failed": 0,
            "run_skipped_existing": skipped_existing,
            "run_failures": [],
            "build_gate": False,
        })
        report_path = Path(args.root) / "game_profiles" / "build_run_report.json"
        report_path.write_text(json.dumps(run_report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(run_report, ensure_ascii=False, indent=2))
        return 0

    client = SteamInputDBClient()
    checkpoint_every = max(1, int(args.checkpoint_every))
    checkpoint: list = []
    run_failures: list[dict] = []
    run_usable = 0
    last_report: dict = {}

    for index, seed in enumerate(pending, start=1):
        result = build_one(client, seed, community_limit=args.community_limit)
        checkpoint.append(result)
        state = "OK" if result.usable else "SKIP"
        detail = "" if result.usable else f" - {result.reason}"
        print(f"[{index}/{len(pending)}] {state} {seed.appid} {seed.name}{detail}", flush=True)
        if result.usable:
            run_usable += 1
        else:
            run_failures.append({"appid": seed.appid, "name": seed.name, "reason": result.reason})

        if len(checkpoint) >= checkpoint_every or index == len(pending):
            last_report = save_library(args.root, checkpoint, target_count=args.target_count)
            checkpoint.clear()
            print(
                f"[checkpoint] library={last_report['library_generated_count']} "
                f"target={last_report['target_count']} target_met={last_report['target_met']}",
                flush=True,
            )
        if index < len(pending) and args.delay > 0:
            time.sleep(args.delay)

    run_report = dict(last_report)
    run_report.update({
        "schema": "motioncontrol.game_profile_build_run.v1",
        "run_finished_at": datetime.now(timezone.utc).isoformat(),
        "run_processed": len(pending),
        "run_usable": run_usable,
        "run_failed": len(run_failures),
        "run_skipped_existing": skipped_existing,
        "run_failures": run_failures,
        "build_gate": False,
    })
    report_path = Path(args.root) / "game_profiles" / "build_run_report.json"
    report_path.write_text(json.dumps(run_report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(run_report, ensure_ascii=False, indent=2))
    # Deliberately no global minimum-profile gate. Partial libraries are valid,
    # persisted at checkpoints, and resumed by skipping existing good profiles.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
