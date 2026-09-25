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

    python tools/stage_release.py --target "$(python tools/release_paths.py pc_dir)"
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
    # phone_web 归 stage_phone_web 管，它不在这个脚本的 import 图里。不排除的话
    # 这里每次都把它整个当成陈年文件删掉，下一步再重建，于是「内容没变就留着
    # 签名」永远不成立：每跑一次 stage 就得重签一次，而忘了重签不会报错，只有
    # 真手机去更新时才拒绝。
    phone_web_root = app / "phone_web"
    if app.is_dir():
        for existing in sorted(app.rglob("*")):
            if existing.is_dir() or "__pycache__" in existing.parts:
                continue
            if phone_web_root == existing or phone_web_root in existing.parents:
                continue
            if existing not in wanted:
                stale.append(existing)
    # 包顶层那几个文件也要查。原来这里只扫 app/，所以 release/ 里删掉或改名的
    # 东西会永远留在包里，而报告还写着 "0 stale"——已经踩过两次：请先看.txt
    # 改名成 README.txt 之后旧的还在，新手指南.html 不再随包发布之后旧的也还在。
    # 只看顶层文件：models/、python/、native/ 这些子目录不由这个脚本管。
    if target.is_dir():
        for existing in sorted(target.iterdir()):
            if existing.is_file() and existing not in wanted:
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
    """Copy the phone's built web app into the release. Returns (files, bytes).

    The signature is not a source file, so the sweep below used to delete it on
    every run -- including runs that changed nothing.  That turned "stage, sign,
    stage again for any reason, zip" into a release the phone silently refuses,
    and nothing says so until someone tries to update a real phone.

    So: unchanged content keeps its signature, changed content loses it.  A
    signature over bytes that have since moved is worse than none -- it would
    fail verification on the phone rather than at the moment it went stale.
    """
    # 放在 app/ 里面，不是它的同级。电脑端自更新换的就是整个 app/，放外面的东西
    # 一辈子不会被换——网页包 2.0.1 修好了也只能到下一次重装发布包才到人手里，而
    # 「网页包能热更」这句承诺当初（3a04a03）说的就是让电脑端顺路把它带上。
    # find_phone_web 本来就优先找 app/phone_web，所以装了旧版的人更新之后，他们
    # 那条一直是死的通道反而会跟着活过来。
    root = target / "app" / "phone_web"
    signature_name = ".signature"
    wanted = {}
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if relative.startswith(PHONE_WEB_SKIP) or relative.endswith(".map"):
            continue
        if relative == signature_name:
            # 源目录自己也可能被签过：开发时电脑端直接从 switch/mobile/dist 供包
            # 给手机，那份就得签。但它签的是 dist 的清单，不是这份发布包的，抄过
            # 来永远对不上。更糟的是它和目标那份的签发时间不同，于是每次都判成
            # "内容变了"，上面那条"没变就留着签名"彻底失效——每一次 restage 都把
            # 刚签好的签名删掉，而删完不报错，只有真手机去更新时才拒绝。
            continue
        wanted[relative] = path
    keep = {signature_name}
    changed = False
    for existing in sorted(root.rglob("*"), reverse=True):
        if existing.is_file():
            relative = existing.relative_to(root).as_posix()
            if relative not in wanted and relative not in keep:
                existing.unlink()
                # 少一个文件同样改变了这份包，签名一样作废。漏掉这一条，删文件的
                # 那种改动就会留着一个签的是旧清单的签名。
                changed = True
        elif existing.is_dir() and not any(existing.iterdir()):
            existing.rmdir()
    total = 0
    for relative, path in wanted.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or destination.read_bytes() != path.read_bytes():
            shutil.copy2(path, destination)
            changed = True
        total += path.stat().st_size
    if changed:
        (root / ".signature").unlink(missing_ok=True)
    return len(wanted), total


def check_guide_pdf(target: Path) -> str:
    """包里那份 PDF，是不是照当前 docs/新手指南.md 生成的。

    PDF 是生成物，源文件改了它不会自己跟着变。忘了重新生成的话，玩家拿到的是
    旧指南——而这种错没人会发现，直到有人照着旧步骤做不通来问你。

    比的是修改时间，不是内容哈希：PDF 里没有地方能干净地塞一个来源标记，而
    "源文件比产物新" 恰好就是"改了忘了重新生成"的样子。
    """
    source = ROOT / "docs" / "新手指南.md"
    pdf = target / "新手指南.pdf"
    if not pdf.is_file():
        return "包里没有新手指南，跑：python tools/build_guide_html.py"
    if not source.is_file():
        return "找不到指南源文件，无法核对"
    if source.stat().st_mtime > pdf.stat().st_mtime + 1:
        return ("指南 PDF 比源文件旧了。跑：" + chr(10)
                + "     python tools/build_guide_html.py")
    return "新手指南是最新的"


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
    from motioncontrol_shared.model_share import ModelShare, SIGNATURE_NAME

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
        print("  " + check_phone_web_signature(target / "app" / "phone_web"))
    else:
        print(f"WARNING: 找不到手机网页包 {phone_web}，发布包里不会带更新用的那一份")

    print("  " + check_vigem_installer(target))
    print("  " + check_guide_pdf(target))

    print()
    print("staged. Now verify the bundled interpreter has every runtime dependency:")
    print(f'  python tools/check_runtime_deps.py --bundle "{target / "python"}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
