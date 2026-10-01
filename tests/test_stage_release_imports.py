"""发布包按 import 图收文件。相对 import 原来整个被跳过，2026-10-01 那次的包少了
roll_tilt_control.py 和 responsive_head_control.py，一启动就 ModuleNotFoundError——
仓库里跑一切正常，只有发布出去的那份是坏的。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.stage_release import local_module_files  # noqa: E402


def _staged() -> set[str]:
    return {path.relative_to(ROOT).as_posix() for path in local_module_files()}


def test_relative_imports_are_followed():
    staged = _staged()
    assert "motioncontrol/roll_tilt_control.py" in staged
    assert "motioncontrol/responsive_head_control.py" in staged


def test_every_relative_import_of_a_staged_file_is_staged_too():
    staged = _staged()
    missing = []
    for relative in staged:
        path = ROOT / relative
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            base = path.parent
            for _ in range(node.level - 1):
                base = base.parent
            stems = [node.module] if node.module else [alias.name for alias in node.names]
            for stem in stems:
                candidate = base / f"{stem.replace('.', '/')}.py"
                if candidate.is_file() and candidate.relative_to(ROOT).as_posix() not in staged:
                    missing.append(f"{relative} -> {candidate.relative_to(ROOT).as_posix()}")
    assert not missing, missing
