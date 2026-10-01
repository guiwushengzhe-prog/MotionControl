"""Give native model loaders a path they can actually open.

Vosk's loader is a C++ layer that opens files through the narrow-character
Windows API, so a model under a path containing Chinese characters fails with
"Folder ... does not contain model files" even though the folder is right there
and complete.  Verified on this machine: the same directory loads from an ASCII
path and fails from a path containing 电脑端.  It is a known Vosk issue, not
something specific to this project.

That matters here more than it would elsewhere.  The users are on Chinese
Windows and will put the app in D:\\游戏\\MotionControl or under C:\\Users\\张三,
and the only symptom is one line of English in a console window they were told
not to close, with PC-side voice control silently dead.

Three tiers, in order:

1. The path is already ASCII -- use it.
2. Ask Windows for the 8.3 short name.  When it exists it is pure ASCII and
   costs nothing.
3. Mirror the model once into a directory whose path is ASCII, and load from
   there.

Tier 3 is not a rare fallback -- measured on this machine, it is the usual
outcome.  Two things defeat tier 2:

* 8.3 generation can be disabled per volume, and Microsoft's documentation says
  not to assume a short name exists.
* Even with it enabled, Windows does not necessarily generate one for a
  non-ASCII component.  ``D:\\游戏\\MotionControl-shorttest`` shortens to
  ``D:\\游戏\\MOTION~1``: the ASCII part got a short name, 游戏 did not, so the
  path is still unopenable.  That is precisely the shape users will have.

Tier 2 is kept because when it does work it is free.  Tier 3 is what actually
carries the case.  Spending ~66 MB of disk once is a far better trade than
voice recognition that silently does not work.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

_SENTINEL = ".motioncontrol-mirror-complete"


def is_ascii_path(path: Path | str) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def windows_short_path(path: Path) -> Path | None:
    """The 8.3 short name, or None when the volume does not keep one."""
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes

    GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
    GetShortPathNameW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.LPWSTR,
                                  ctypes.wintypes.DWORD]
    GetShortPathNameW.restype = ctypes.wintypes.DWORD

    source = str(path)
    needed = GetShortPathNameW(source, None, 0)
    if not needed:
        return None
    buffer = ctypes.create_unicode_buffer(needed)
    if not GetShortPathNameW(source, buffer, needed):
        return None
    result = buffer.value
    # With 8.3 generation off, Windows returns the long name unchanged rather
    # than failing, so "it returned something" is not success.
    return None if result == source else Path(result)


def _cache_candidates() -> list[Path]:
    candidates = []
    for variable, suffix in (("LOCALAPPDATA", "MotionControl"),
                             ("PUBLIC", "MotionControl"),
                             ("ProgramData", "MotionControl"),
                             ("SystemDrive", "MotionControl-cache")):
        value = os.environ.get(variable, "").strip()
        if value:
            candidates.append(Path(value + os.sep if variable == "SystemDrive" else value) / suffix)
    candidates.append(Path("C:/MotionControl-cache"))
    return candidates


def ascii_cache_root() -> Path | None:
    """A writable directory whose own path contains no non-ASCII characters.

    %LOCALAPPDATA% is tried first but cannot be assumed: on a machine whose
    user is named 张三 it is C:\\Users\\张三\\AppData\\Local, which is exactly
    the problem being solved.  C:\\Users\\Public and C:\\ProgramData keep ASCII
    filesystem names on localized Windows -- the translated names seen in
    Explorer are display names, not paths.
    """
    for candidate in _cache_candidates():
        if not is_ascii_path(candidate):
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_bytes(b"")
            probe.unlink()
            return candidate / "model-cache"
        except OSError:
            continue
    return None


def _fingerprint(source: Path) -> str:
    """Cheap identity for a model directory: layout, not contents.

    Hashing 42 MB on every start would be worse than the problem.  Name, size
    and mtime of every file is enough to notice a model that was replaced or
    only partly copied.
    """
    digest = hashlib.sha256()
    digest.update(str(source).encode("utf-8", "surrogatepass"))
    for path in sorted(source.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest.update(str(path.relative_to(source)).encode("utf-8", "surrogatepass"))
            digest.update(f"{stat.st_size}:{int(stat.st_mtime)}".encode("ascii"))
    return digest.hexdigest()[:16]


def mirror_to_ascii(source: Path, cache_root: Path | None = None) -> Path | None:
    """Copy *source* under an ASCII path once, and return that path."""
    root = cache_root if cache_root is not None else ascii_cache_root()
    if root is None:
        return None
    fingerprint = _fingerprint(source)
    target = root / f"{fingerprint}-{_ascii_name(source.name)}"
    if (target / _SENTINEL).is_file():
        _prune_other_mirrors(target)
        return target

    staging = target.with_name(target.name + ".partial")
    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        staging.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, staging)
        # The sentinel goes in last, so an interrupted copy is never mistaken
        # for a usable model on the next run.
        (staging / _SENTINEL).write_text(fingerprint, encoding="ascii")
        staging.rename(target)
        _prune_other_mirrors(target)
        return target
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return None


def _drop_mirrors_of(source: Path) -> None:
    """This install opens the model in place, so any mirror of it is dead weight.

    Mirrors left by an earlier install in a Chinese path would otherwise stay
    on C: for good.  If such an install is still in use it mirrors again on its
    next start.  Only existing cache folders are looked at -- nothing is created.
    """
    suffix = _ascii_name(source.name)
    for candidate in _cache_candidates():
        root = candidate / "model-cache"
        try:
            entries = list(root.iterdir()) if root.is_dir() else []
        except OSError:
            continue
        for entry in entries:
            name = entry.name.removesuffix(".partial")
            if entry.is_dir() and "-" in name and name.split("-", 1)[1] == suffix:
                shutil.rmtree(entry, ignore_errors=True)


def _prune_other_mirrors(keep: Path) -> None:
    """Delete earlier mirrors of the same model; only the one in use is kept.

    The fingerprint includes the source path and file times, so every new
    install location or re-extracted version mirrored the model again and the
    old copies were never removed.  Measured on the developer machine: six
    copies, 390 MB on C: for one 66 MB model.  A mirror that another copy of the
    program still has loaded cannot be deleted on Windows; ignore_errors leaves
    it, and that program simply mirrors again on its next start.
    """
    suffix = keep.name.split("-", 1)[1] if "-" in keep.name else keep.name
    try:
        siblings = list(keep.parent.iterdir())
    except OSError:
        return
    for entry in siblings:
        name = entry.name.removesuffix(".partial")
        if entry != keep and entry.is_dir() and name.split("-", 1)[-1] == suffix:
            shutil.rmtree(entry, ignore_errors=True)


def _ascii_name(name: str) -> str:
    """The directory name itself has to be ASCII too."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name)
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii").strip("-")
    return cleaned or "model"


def resolve_loadable_model_path(source: Path) -> tuple[Path, str]:
    """Return (path a native loader can open, which tier produced it)."""
    source = Path(source)
    if is_ascii_path(source):
        _drop_mirrors_of(source)
        return source, "direct"

    short = windows_short_path(source)
    if short is not None and is_ascii_path(short):
        _drop_mirrors_of(source)
        return short, "short-path"

    mirrored = mirror_to_ascii(source)
    if mirrored is not None:
        return mirrored, "mirror"

    # Nothing worked.  Hand back the original so the caller reports the real
    # loader error rather than a path error invented here.
    return source, "unavailable"
