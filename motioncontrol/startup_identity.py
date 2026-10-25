"""启动前核对已加载程序和实际配置，避免复用路径相同的旧后台。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def source_revision(root):
    root = Path(root)
    files = [root / "server.py"]
    for name in ("motioncontrol", "motioncontrol_shared"):
        files.extend(sorted((root / name).rglob("*.py")))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()[:24]


def startup_snapshot(root):
    from .game_profiles import GameProfileStore
    from .user_paths import user_data_root
    store = GameProfileStore(Path(root))
    return {"source_revision": source_revision(root), "configuration_revision": store.revision(),
            "selected_id": store.selected_id(), "user_data_root": str(user_data_root().resolve())}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(startup_snapshot(args.root), ensure_ascii=False))
    except (OSError, ValueError) as error:
        print("启动配置核对失败，原文件已保留：" + str(error))
        raise SystemExit(2)
