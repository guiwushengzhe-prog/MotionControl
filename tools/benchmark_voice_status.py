"""Measure configuration-derived voice status reads without loading ASR models."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if args.iterations < 1 or args.rounds < 1:
        parser.error("iterations and rounds must be positive")
    with tempfile.TemporaryDirectory(prefix="motioncontrol-voice-benchmark-") as directory:
        os.environ["MOTIONCONTROL_USER_DIR"] = str(Path(directory) / "user")
        from motioncontrol.voice_backend import VoiceService

        root = Path(directory) / "app"
        target = root / "config" / "generated_voice"
        target.mkdir(parents=True)
        shutil.copy(ROOT / "config/generated_voice/voice_action_map.json", target)
        service = VoiceService(root, lambda action: {"executed": True})
        try:
            for _ in range(100):
                service.status()
            rounds = []
            for _ in range(args.rounds):
                start = time.perf_counter()
                for _ in range(args.iterations):
                    service.status()
                rounds.append((time.perf_counter() - start) * 1000 / args.iterations)
            print(json.dumps({
                "scenario": "configuration-only; no ASR or device input",
                "commands": len(service.command_registry),
                "iterations": args.iterations,
                "rounds_ms_per_read": rounds,
                "median_ms_per_read": statistics.median(rounds),
            }, ensure_ascii=False))
        finally:
            service.close()


if __name__ == "__main__":
    main()
