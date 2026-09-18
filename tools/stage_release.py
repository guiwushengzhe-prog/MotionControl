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

    # release/ 里的东西原样搬到包的顶层。早先这里只认一个写死的文件名，
    # 结果改个名字旧的就永远留在包里——发布包必须能从零重建，不能靠谁
    # 记得手动放文件。AGPL 要求随源码一起分发许可证，LICENSE 就在这里。
    for source in sorted((ROOT / "release").rglob("*")):
        if source.is_dir():
            continue
        destination = target / source.relative_to(ROOT / "release")
        copies.append((source, destination))
        wanted.add(destination)

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


# 手机网页包里不进发布的部分：模型和 WASM 有 25 MB，它们留在 APK 里，手机永远
# 从 APK 读（见 WebUpdateRoutes）。进来的只有真正会变的那 200 KB。
PHONE_WEB_SKIP = ("models/", "wasm/")


def stage_phone_web(source: Path, target: Path) -> tuple[int, int]:
    """Copy the phone's built web app into the release. Returns (files, bytes)."""
    root = target / "phone_web"
    wanted = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if relative.startswith(PHONE_WEB_SKIP) or relative.endswith(".map"):
            continue
        wanted[relative] = path
    for existing in sorted(root.rglob("*"), reverse=True):
        if existing.is_file() and existing.relative_to(root).as_posix() not in wanted:
            existing.unlink()
        elif existing.is_dir() and not any(existing.iterdir()):
            existing.rmdir()
    total = 0
    for relative, path in wanted.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or destination.read_bytes() != path.read_bytes():
            shutil.copy2(path, destination)
        total += path.stat().st_size
    return len(wanted), total


def check_vigem_installer(target: Path) -> str:
    """驱动安装包还是不是官方那一个。

    THIRD_PARTY_NOTICE-ViGEmBus.txt 里一直记着官方安装包的 SHA256，但从来没有
    人拿它对过——记下来而不核对，等于只是写了一句好听的话。我们按 BSD-3 把
    别人的二进制原样转发给玩家，那就有责任保证转发的确实是原样那一份。
    """
    import hashlib
    import re

    notice = target / "THIRD_PARTY_NOTICE-ViGEmBus.txt"
    installer = target / "安装虚拟手柄驱动.exe"
    if not notice.is_file() or not installer.is_file():
        return "驱动安装包或它的第三方声明不见了"
    found = re.search(r"SHA256:\s*([0-9a-f]{64})", notice.read_text(encoding="utf-8"))
    if not found:
        return "第三方声明里没写 SHA256，无法核对"
    actual = hashlib.sha256(installer.read_bytes()).hexdigest()
    if actual != found.group(1):
        return f"驱动安装包和声明里的 SHA256 对不上：{actual}"
    return "驱动安装包是官方原件"


def check_phone_web_signature(bundle: Path) -> str:
    """签名对不对得上这一份包。

    每跑一次 npm run build，包的内容就变了，上一次的签名立刻作废——而手机会
    安静地拒绝，界面上什么都不说。开发时踩过两次，所以让打包这一步直接说出来。
    """
    sys.path.insert(0, str(ROOT))
    from model_share import ModelShare, SIGNATURE_NAME

    share = ModelShare("phone-web", bundle, skip=("models/", "wasm/"))
    manifest = share.manifest()
    if not manifest.get("payload"):
        return ("没有签名 —— 手机会拒绝这份包。跑：" + chr(10) +
                f'     python tools/sign_phone_web.py --bundle "{bundle}"')
    import base64
    import json as _json
    signed = _json.loads(base64.b64decode(manifest["payload"]))
    if signed.get("digest") != manifest["digest"]:
        return ("签名对不上这一份包（构建过但没重新签）—— 手机会拒绝。跑：" + chr(10) +
                f'     python tools/sign_phone_web.py --bundle "{bundle}"')
    return "签名有效"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="portable bundle root")
    parser.add_argument("--check", action="store_true", help="report only")
    parser.add_argument("--phone-web", default="../switch/mobile/dist",
                        help="手机端 npm run build 的产物目录")
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

    phone_web = Path(args.phone_web)
    if phone_web.is_dir():
        files, size = stage_phone_web(phone_web, target)
        print(f"phone_web: {files} 个文件 {size / 1024:.0f} KB（手机连上时自己来取）")
        print("  " + check_phone_web_signature(target / "phone_web"))
    else:
        print(f"WARNING: 找不到手机网页包 {phone_web}，发布包里不会带更新用的那一份")

    print("  " + check_vigem_installer(target))

    print()
    print("staged. Now verify the bundled interpreter has every runtime dependency:")
    print(f'  python tools/check_runtime_deps.py --bundle "{target / "python"}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
