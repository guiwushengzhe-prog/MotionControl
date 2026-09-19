"""The dependency boundary around motioncontrol_shared, enforced.

The arrow is ``desktop -> shared <- cloud`` and never ``cloud -> desktop``.
This matters concretely: output_backend.py imports ctypes.wintypes, which
raises on Linux, and it is reachable from server.py, control_kernel.py and
voice_backend.py.  If anything in the shared package grows an import into that
graph, the cloud stops importing on Linux -- but only at deploy time, not here.

This file is the *early warning*: a static scan that fails the moment a risky
import appears.  The real backstop is the Linux CI job, which imports the
package for real on Linux and runs the whole profile library through it.
Static analysis cannot see transitive or runtime imports; a real import can.
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
SHARED = REPO / "motioncontrol_shared"

# Anything that ships as a top-level module beside the package is desktop code.
# Deriving this instead of hard-coding it means a newly added desktop module is
# covered without anyone remembering to update a list.
#
# motioncontrol/ 要单独列出来：程序本体从根目录收进这个包之后，靠 glob("*.py")
# 推导出的名单只剩下 server 一个，这条规则就等于没有了——而它正是"在开发机上看
# 着没事、到服务器上启动时才炸"那一类问题唯一的防线。真撞上过：cloud 的更新接口
# 一度 import 了 motioncontrol_shared.model_share，测试全绿。
DESKTOP_MODULES = {
    path.stem for path in REPO.glob("*.py") if path.stem != "conftest"
} | {"motioncontrol"}

PLATFORM_MODULES = {"ctypes", "winreg", "msvcrt", "_winapi", "win32api", "win32con"}
SERVER_MODULES = {"fastapi", "sqlalchemy", "pydantic", "uvicorn", "alembic", "psycopg"}
RUNTIME_MODULES = {"cv2", "mediapipe", "numpy", "sounddevice", "vosk", "sherpa_onnx"}
FORBIDDEN = DESKTOP_MODULES | PLATFORM_MODULES | SERVER_MODULES | RUNTIME_MODULES


def _shared_sources():
    return sorted(SHARED.rglob("*.py"))


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import inside the package, which is fine.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


def test_shared_package_is_populated():
    # Guard against this whole file silently passing on an empty glob.
    assert len(_shared_sources()) >= 5


def test_shared_imports_nothing_forbidden():
    offenders = []
    for path in _shared_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for root in sorted(_imported_roots(tree) & FORBIDDEN):
            offenders.append(f"{path.relative_to(REPO)} imports {root}")
    assert not offenders, "motioncontrol_shared must stay importable on Linux: " + "; ".join(offenders)


def test_shared_reads_no_environment():
    """No os.environ / os.getenv anywhere in the shared package.

    A pure layout helper that resolves %LOCALAPPDATA% would import cleanly on
    Linux and still be wrong: it makes the shared rules depend on where the
    desktop happens to store files.  Path *shapes* may live here; resolving the
    base directory belongs to the desktop layer.
    """
    offenders = []
    for path in _shared_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} os.{node.attr}")
            elif isinstance(node, ast.Name) and node.id in {"getenv", "environ"}:
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {node.id}")
    assert not offenders, "shared code must not read the environment: " + "; ".join(offenders)


def test_shared_writes_no_files():
    """open(..., 'w'/'a'/'x') and the usual mutating pathlib helpers.

    "replace" is deliberately not in the generic set: str.replace is everywhere
    and a static check cannot tell the receiver apart from Path.replace, so it
    is matched only against an explicit os/shutil receiver below.
    """
    offenders = []
    mutators = {"write_text", "write_bytes", "mkdir", "unlink", "rename", "touch", "rmdir"}
    module_mutators = {"replace", "rename", "remove", "unlink", "mkdir", "makedirs",
                       "rmdir", "copy", "copy2", "copyfile", "copytree", "move", "rmtree"}
    for path in _shared_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)                     and func.value.id in {"os", "shutil"} and func.attr in module_mutators:
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} {func.value.id}.{func.attr}()")
            elif isinstance(func, ast.Attribute) and func.attr in mutators:
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno} .{func.attr}()")
            elif isinstance(func, ast.Name) and func.id == "open":
                mode = next((a for a in node.args[1:2]), None)
                if isinstance(mode, ast.Constant) and set(str(mode.value)) & set("wax+"):
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno} open(mode={mode.value!r})")
    assert not offenders, "shared code must not write files: " + "; ".join(offenders)


def test_shared_import_closure_is_stdlib_only():
    """Import every shared module for real and inspect what got pulled in.

    The AST scan sees only import statements it can name.  This runs the
    imports in a clean subprocess and checks the resulting sys.modules against
    sys.stdlib_module_names, so a transitive dependency -- a stdlib-looking
    module that itself reaches into something else -- has nowhere to hide.

    This is the same property the Linux CI job asserts; running it here means a
    violation shows up before the push rather than after.
    """
    import subprocess
    import sys

    # -I keeps user site-packages and PYTHONPATH out, which is the point; it
    # also drops cwd from sys.path, so the repo goes back on explicitly.
    script = (
        "import importlib, pkgutil, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import motioncontrol_shared as pkg\n"
        "for info in pkgutil.iter_modules(pkg.__path__):\n"
        "    importlib.import_module(f'{pkg.__name__}.{info.name}')\n"
        "allowed = set(sys.stdlib_module_names)\n"
        "extra = sorted(\n"
        "    name for name in sys.modules\n"
        "    if not name.startswith('_')\n"
        "    and name.split('.', 1)[0] not in allowed\n"
        "    and name.split('.', 1)[0] != 'motioncontrol_shared'\n"
        ")\n"
        "print('|'.join(extra))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script, str(REPO)],
        cwd=REPO, capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"shared package failed to import: {proc.stderr}"
    extra = [name for name in proc.stdout.strip().split("|") if name]
    assert not extra, f"shared package pulled in non-stdlib modules: {extra}"


def test_pairing_dependency_is_declared():
    """cryptography is the one dependency whose absence is silent.

    Everything else fails at the point of use with a message the user sees --
    no OpenCV means the camera refuses to start and says so.  device_pairing
    catches ImportError and reports pairing as merely unavailable, so on a
    build with require_paired_devices on, a packaging slip would look like
    every phone being rejected for protocol reasons.

    Only the declaration is checked here.  Whether this machine actually has it
    installed is a desktop-runtime question, and it lives in
    tests/test_runtime_deps.py -- this file is also run by the Linux CI job,
    which installs nothing on purpose, so asserting it here failed every push
    for reasons that had nothing to do with the boundary.
    """
    declared = (REPO / "requirements-runtime.txt").read_text(encoding="utf-8")
    assert "cryptography" in declared, "requirements-runtime.txt must declare cryptography"


# --- the other side of the arrow: cloud must not reach into the desktop ------

CLOUD = REPO / "cloud"

# The cloud is a server, so FastAPI and SQLAlchemy are exactly what it should
# import. What it must never touch is desktop code or a Windows API, because
# either one means the service cannot start on the Linux box it deploys to.
CLOUD_FORBIDDEN = DESKTOP_MODULES | PLATFORM_MODULES | RUNTIME_MODULES


# app_bundle/ 是部署时放进来的电脑端更新包——它是这个服务要发出去的载荷，不是
# 服务自己的代码。里面当然满是 cv2、ctypes 和 motioncontrol，那正是它该有的样子。
_NOT_CLOUD_CODE = {"__pycache__", "app_bundle"}


def _cloud_sources():
    return sorted(path for path in CLOUD.rglob("*.py")
                  if not _NOT_CLOUD_CODE & set(path.parts))


def test_cloud_package_is_populated():
    assert len(_cloud_sources()) >= 5, "cloud/ scan found nothing -- check the glob"


def test_cloud_imports_no_desktop_module():
    """`cloud -> desktop` is the direction that breaks the deployment.

    A desktop import here looks harmless on a developer's Windows machine and
    fails on the server, at start-up, in production. The shared package is the
    only thing the cloud may take from this repository.
    """
    offenders = []
    for path in _cloud_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for root in sorted(_imported_roots(tree) & CLOUD_FORBIDDEN):
            offenders.append(f"{path.relative_to(REPO)} imports {root}")
    assert not offenders, (
        "cloud/ may only import motioncontrol_shared from this repo: "
        + "; ".join(offenders))


def test_cloud_uses_the_shared_validator():
    """The cloud must not grow its own copy of the config rules.

    If it validated uploads itself, the two sides would drift and the symptom
    would be a config that uploads cleanly and then will not install.
    """
    sources = [path.read_text(encoding="utf-8") for path in _cloud_sources()]
    assert any("from motioncontrol_shared.canonical import canonicalize" in text
               for text in sources), "no cloud module imports canonicalize"
    # json.dumps of a stored document would bypass canonical serialisation.
    writers = [path.relative_to(REPO) for path, text in zip(_cloud_sources(), sources)
               if "json.dumps(" in text and "tests" not in path.parts]
    assert not writers, f"cloud must serialise through canonicalize(), not json.dumps: {writers}"
