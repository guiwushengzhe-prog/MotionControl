"""Recoverable, small multi-file configuration changes.

Callers keep their configuration lock and commit memory only after this context
finishes. The journal restores the previous files after an interrupted commit.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile


def atomic_bytes(path: Path, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _restore(entries: list[dict], root: Path) -> None:
    for entry in entries:
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("配置恢复路径不在用户数据目录中")
        content = entry["content"]
        if content is None:
            path.unlink(missing_ok=True)
        else:
            previous = base64.b64decode(content, validate=True)
            if path.is_file() and path.read_bytes() == previous:
                continue
            atomic_bytes(path, previous)


def recover(journal: Path) -> bool:
    journal = Path(journal)
    if not journal.is_file():
        return False
    data = json.loads(journal.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("files"), list):
        raise ValueError("配置恢复记录格式无法读取，记录已保留")
    _restore(data["files"], journal.parent)
    journal.unlink()
    return True


@contextmanager
def file_transaction(paths, journal: Path):
    journal = Path(journal)
    recover(journal)
    root = journal.parent.resolve()
    entries = []
    for raw in dict.fromkeys(map(Path, paths)):
        path = raw.resolve()
        entries.append({"path": str(path.relative_to(root)),
                        "content": base64.b64encode(path.read_bytes()).decode("ascii")
                        if path.is_file() else None})
    atomic_bytes(journal, json.dumps({"schema": 1, "files": entries}).encode("utf-8"))
    try:
        yield
        journal.unlink()
    except BaseException:
        # Leave the journal for startup recovery if restoration itself fails.
        _restore(entries, root)
        journal.unlink(missing_ok=True)
        raise


def replace_documents(documents: dict[Path, dict], journal: Path) -> None:
    encoded = {Path(path): json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
               for path, data in documents.items()}
    with file_transaction(encoded, journal):
        for path, content in encoded.items():
            atomic_bytes(path, content)
