from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from motioncontrol.profile_audit import audit_library, write_audit_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit MotionControl offline Game Profile library")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    report = audit_library(args.root)
    path = write_audit_report(args.root, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Audit report: {path}")
    # Audit is diagnostic only. It deliberately does not gate a partial library.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
