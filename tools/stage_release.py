"""Stage the portable PC release from the repository, reproducibly.

The 2.0 bundle was assembled by hand, which is why its app/ directory still
carried motion_conflicts.py at the top level after that module moved into
motioncontrol_shared/, and why adding device_pairing.py or user_paths.py would
have been easy to forget.  Forgetting is not a loud failure: the app starts and
one feature is quietly dead.

So the file list is not maintained by hand either.  It is derived by walking
the import graph from server.py and keeping whatever resolves to a file in this
repository, which means a module added tomorrow is included tomorrow.  Anything
the graph does not reach -- dev tools, alternative speech backends, capture
scripts -- stays out.

    python tools/stage_release.py --target "build/release/电脑端/MotionControl-PC-2.0"
    python tools/stage_release.py --target ... --check   # report, change nothing

Model files, the bundled Python and the ViGEm DLL are not touched: they are
large, they rarely change, and they are not produced from this repository.
"""

from __future__ import annotations

import argparse
import ast
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINTS = ("server.py",)

# Directories copied wholesale.  config/ is deliberately absent: since 2.0.x the
# user's own files live in %LOCALAPPDATA%, and shipping a stale copy of them
# would give a fresh install someone else's mappings.
COPY_TREES = ("web", "game_profiles")

# Program configuration that is identical everywhere, so it comes from the
# repository.  Listed explicitly rather than globbed so a user-data file can
# never be shipped by accident -- the 2.0 bundle shipped one developer's
# motion_mappings.json and voice_mappings.json exactly that way.
CONFIG_FILES = (
    "voice_commands_v094.json",
    "scene_layout.example.json",
)

# Configuration that belongs to the bundle, not to the repository: it points at
# paths *inside* the release.  The repo's copies point at a developer's machine
# (model_root.txt says I:\MotionControl-Pose-Models\models, the release says
# ../models), so copying them over would break the release rather than update
# it.  These are preserved untouched, and only flagged if missing.
RELEASE_LOCAL_CONFIG = (
    "model_root.txt",
    "vosk_model_path.txt",
    "vigemclient_dll.txt",
)

# Shipped by mistake in 2.0: it pointed at a developer's absolute path
# (F:\MotionControl-App\models\sherpa-...) for a model the release does not
# even contain.  Harmless -- the app falls back to Vosk -- but it is a dev path
# in a user's download, so staging removes it.
REMOVE_FROM_CONFIG = ("sherpa_kws_model_path.txt",)


def local_module_files() -> set[Path]:
    """Every repository file reachable by import from the entry points."""
    seen: set[str] = set()
    files: set[Path] = set()
    queue = list(ENTRY_POINTS)

    def resolve(name: str) -> Path | None:
        module = ROOT / f"{name.replace('.', '/')}.py"
        if module.is_file():
            return module
        package = ROOT / name.replace(".", "/") / "__init__.py"
        return package if package.is_file() else None

    while queue:
        relative = queue.pop()
        if relative in seen:
            continue
        seen.add(relative)
        path = ROOT / relative
        if not path.is_file():
            continue
        files.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
                # "from pkg import mod" may name a submodule rather than a symbol.
                names += [f"{node.module}.{alias.name}" for alias in node.names]
            for name in names:
                target = resolve(name)
                if target is not None:
                    queue.append(str(target.relative_to(ROOT)).replace("\\", "/"))
    return files


def plan(target: Path) -> tuple[list[tuple[Path, Path]], list[Path]]:
    """(copies, stale) -- what to write, and what no longer belongs."""
    app = target / "app"
    copies: list[tuple[Path, Path]] = []
    wanted: set[Path] = set()

    for source in sorted(local_module_files()):
        destination = app / source.relative_to(ROOT)
        copies.append((source, destination))
        wanted.add(destination)

    for tree in COPY_TREES:
        for source in sorted((ROOT / tree).rglob("*")):
            if source.is_dir() or "__pycache__" in source.parts:
                continue
            destination = app / source.relative_to(ROOT)
            copies.append((source, destination))
            wanted.add(destination)

    for name in CONFIG_FILES:
        source = ROOT / "config" / name
        if source.is_file():
            destination = app / "config" / name
            copies.append((source, destination))
            wanted.add(destination)

    # Preserved, never overwritten -- see RELEASE_LOCAL_CONFIG.  Anything not
    # added to `wanted` is reported stale and removed, which is how
    # REMOVE_FROM_CONFIG entries leave the bundle.
    for name in RELEASE_LOCAL_CONFIG:
        if name not in REMOVE_FROM_CONFIG:
            wanted.add(app / "config" / name)

    readme = ROOT / "release" / "请先看.txt"
    if readme.is_file():
        copies.append((readme, target / readme.name))
        wanted.add(target / readme.name)

    stale = []
    if app.is_dir():
        for existing in sorted(app.rglob("*")):
            if existing.is_dir() or "__pycache__" in existing.parts:
                continue
            if existing not in wanted:
                stale.append(existing)
    return copies, stale


def stale_bytecode(target: Path) -> list[Path]:
    """__pycache__ directories left behind by running the app from the bundle.

    plan() skips them so that a hundred .pyc files do not drown the diff, but
    skipping is not the same as leaving them in the release. They are one
    developer machine's compiled output, they go stale the moment a source file
    changes, and nothing in the bundle needs them -- Python recreates whatever
    it wants on first run.
    """
    return sorted(path for path in (target / "app").rglob("__pycache__")
                  if path.is_dir())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="portable bundle root")
    parser.add_argument("--check", action="store_true", help="report only")
    args = parser.parse_args()

    target = Path(args.target)
    if not (target / "python").is_dir():
        raise SystemExit(f"{target} does not look like a portable bundle (no python/)")

    copies, stale = plan(target)

    missing_local = [name for name in RELEASE_LOCAL_CONFIG
                     if not (target / "app" / "config" / name).is_file()]
    if missing_local:
        print("WARNING: bundle-local config missing, the release will not work:",
              ", ".join(missing_local))

    bytecode = stale_bytecode(target)
    changed = [(s, d) for s, d in copies
               if not d.is_file() or d.read_bytes() != s.read_bytes()]
    print(f"{len(copies)} files in the release, {len(changed)} to update, "
          f"{len(stale)} stale, {len(bytecode)} __pycache__ to drop")
    for _source, destination in changed[:20]:
        print("  update", destination.relative_to(target))
    if len(changed) > 20:
        print(f"  ... and {len(changed) - 20} more")
    for path in stale:
        print("  remove", path.relative_to(target))
    for path in bytecode:
        print("  remove", path.relative_to(target))

    if args.check:
        return 1 if (changed or stale or bytecode) else 0

    for source, destination in changed:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for path in stale:
        path.unlink()
    for path in bytecode:
        shutil.rmtree(path, ignore_errors=True)

    print()
    print("staged. Now verify the bundled interpreter has every runtime dependency:")
    print(f'  python tools/check_runtime_deps.py --bundle "{target / "python"}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
