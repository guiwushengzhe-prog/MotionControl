"""「排查问题」最下面那一行：录下的数据有几段、多大。界面上不摆路径，只给这两个数。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import recordings_summary  # noqa: E402


def test_nothing_recorded_yet(tmp_path):
    assert recordings_summary(tmp_path / "recordings") == {"count": 0, "bytes": 0}


def test_each_jsonl_is_one_clip_and_every_file_counts_toward_size(tmp_path):
    folder = tmp_path / "recordings"
    (folder / "triggered").mkdir(parents=True)
    (folder / "pose-1.jsonl").write_bytes(b"x" * 1000)
    (folder / "triggered" / "clip-1.jsonl").write_bytes(b"y" * 500)
    (folder / "triggered" / "index.json").write_bytes(b"{}")
    assert recordings_summary(folder) == {"count": 2, "bytes": 1502}
