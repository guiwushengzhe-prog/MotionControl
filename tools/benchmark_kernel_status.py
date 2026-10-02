"""Measure detached status snapshots without camera, inference, or OS output.

Run with the project Python: python tools/benchmark_kernel_status.py
Use --kernel-file to compare an exported baseline control_kernel.py with the
same dependencies and sample configuration. This is a microbenchmark, not an
estimate of capture or game frame rate.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pstats
import statistics
import sys
import tempfile
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-file", type=Path)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    from motioncontrol_shared import pose_library
    from motioncontrol.game_profiles import GameProfileStore
    from test_control_kernel import FakeOutput, pose_map

    if args.kernel_file:
        spec = importlib.util.spec_from_file_location("motioncontrol.control_kernel_benchmark", args.kernel_file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        ControlKernel = module.ControlKernel
    else:
        from motioncontrol.control_kernel import ControlKernel

    previous = pose_library.registered()
    docs = [pose_library.normalize_action(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted((root / "cloud" / "official_poses").glob("*.json"))
            if path.name != "signatures.json"]
    pose_library.register(docs)
    original_monotonic, original_wall_clock = time.monotonic, time.time
    # Keep age, calibration and notice fields identical across compared runs.
    time.monotonic = lambda: 1000.0
    time.time = lambda: 1700000000.0
    with tempfile.TemporaryDirectory(prefix="mc-status-bench-") as folder:
        os.environ["MOTIONCONTROL_USER_DIR"] = folder
        kernel = ControlKernel(FakeOutput(), persist=False)
        kernel._stop.set()
        kernel._thread.join(timeout=1.0)
        kernel.configure_bindings(GameProfileStore(root).effective_profile()["bindings"])
        pose = pose_map(hands_up=False)
        kernel.handle_pose_map("mobile_pose:benchmark", pose, return_status=False)
        results = {}
        try:
            methods = {"status": kernel.status, "effective_bindings": kernel.effective_bindings,
                       "general_settings_payload": kernel.general_settings_payload,
                       "runtime_zones": kernel.runtime_zones}
            for name, method in methods.items():
                for _ in range(100):
                    method()
                durations = []
                for _ in range(args.iterations):
                    began = time.perf_counter_ns()
                    method()
                    durations.append((time.perf_counter_ns() - began) / 1_000_000)
                durations.sort()
                results[name] = {"mean_ms": statistics.mean(durations),
                                 "median_ms": statistics.median(durations),
                                 "p95_ms": durations[min(len(durations) - 1, int(len(durations) * .95))]}
            result = {"iterations": args.iterations, "downloaded_actions": len(docs),
                      "bindings": len(kernel.control_bindings), "results": results,
                      "kernel_file": str(args.kernel_file or root / "motioncontrol" / "control_kernel.py"),
                      "scope": "Synthetic Python snapshot cost; no camera, inference, OS output or game FPS"}
            result["snapshot_sha256"] = {
                name: hashlib.sha256(json.dumps(method(), sort_keys=True, ensure_ascii=False)
                                     .replace(folder, "<USER_DIR>").encode("utf-8")).hexdigest()
                for name, method in methods.items()
            }
            print(json.dumps(result, indent=2))
            if args.output:
                args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            if args.profile:
                profiler = cProfile.Profile()
                profiler.runcall(lambda: [kernel.status() for _ in range(args.iterations)])
                pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(18)
        finally:
            kernel.close()
            pose_library.register(previous.values())
            time.monotonic, time.time = original_monotonic, original_wall_clock


if __name__ == "__main__":
    main()
