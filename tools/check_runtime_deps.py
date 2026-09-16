"""Check that a Python environment has everything the PC app needs.

Run against the current interpreter, or against the portable release bundle:

    python tools/check_runtime_deps.py
    python tools/check_runtime_deps.py --bundle "build/release/.../python"

This exists because of one specific failure mode.  Most dependencies here fail
loudly at the point of use -- no OpenCV means the camera refuses to start, with
a message saying so.  ``cryptography`` does not: device_pairing catches the
ImportError and reports pairing as unavailable, which on a release with
``require_paired_devices`` turned on means the phone silently cannot connect at
all.  A packaging mistake would look like a protocol bug.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements-runtime.txt"

# Distribution name -> import name, where they differ.
IMPORT_NAMES = {
    "opencv-python": "cv2",
    "vosk": "vosk",
    "mediapipe": "mediapipe",
    "sounddevice": "sounddevice",
    "cryptography": "cryptography",
    "numpy": "numpy",
}

# Without these the app starts but a whole feature is quietly dead.
CRITICAL = {"cryptography"}


def requirements() -> list[str]:
    names = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.append(re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip())
    return names


def probe(python: str, names: list[str]) -> dict[str, str | None]:
    """Import each module in the target interpreter and report its version."""
    script = (
        "import importlib, json, sys\n"
        "out = {}\n"
        "for name in json.loads(sys.argv[1]):\n"
        "    try:\n"
        "        module = importlib.import_module(name)\n"
        "        out[name] = getattr(module, '__version__', 'unknown')\n"
        "    except Exception:\n"
        "        out[name] = None\n"
        "print(json.dumps(out))\n"
    )
    import json
    import os

    # Reproduce what 启动.bat does, or this reports the developer's own
    # packages instead of the bundle's: the embedded ._pth leaves "import site"
    # enabled, so without these the user's roaming site-packages join sys.path
    # and can shadow -- or appear to supply -- a bundled dependency.
    environment = dict(os.environ)
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)

    result = subprocess.run(
        [python, "-c", script, json.dumps(names)],
        capture_output=True, text=True, cwd=ROOT, env=environment,
    )
    if result.returncode != 0:
        raise SystemExit(f"could not probe {python}:\n{result.stderr}")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", help="path to a portable bundle's python directory")
    args = parser.parse_args()

    if args.bundle:
        python = str(Path(args.bundle) / "python.exe")
        if not Path(python).is_file():
            raise SystemExit(f"no python.exe under {args.bundle}")
    else:
        python = sys.executable

    distributions = requirements()
    modules = [IMPORT_NAMES.get(name, name.replace("-", "_")) for name in distributions]
    found = probe(python, modules)

    print(f"checking {python}")
    missing_critical = []
    missing_other = []
    for distribution, module in zip(distributions, modules):
        version = found[module]
        if version is None:
            mark = "MISSING"
            (missing_critical if distribution in CRITICAL else missing_other).append(distribution)
        else:
            mark = version
        print(f"  {distribution:<24} {mark}")

    if missing_critical:
        print()
        print("FAIL: missing dependencies that fail silently rather than loudly:",
              ", ".join(missing_critical))
        print("      Device pairing degrades to unavailable without cryptography, so a")
        print("      release with pairing enforced would refuse every phone.")
        return 1
    if missing_other:
        print()
        print("WARNING: missing:", ", ".join(missing_other))
        print("         These fail at the point of use with a clear message.")
        return 0
    print()
    print("OK: every runtime dependency is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
