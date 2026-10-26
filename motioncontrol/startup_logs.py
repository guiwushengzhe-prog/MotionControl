"""桌面运行记录限量保留；记录失败不能阻止识别或退出。"""
from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import threading

MAX_BYTES = 1024 * 1024
BACKUPS = 4
NAMES = ("startup.stdout.log", "startup.stderr.log", "bootstrap.stdout.log", "bootstrap.stderr.log")


def _tail(path, limit):
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        return stream.read(limit)


def rotate(path, *, limit=MAX_BYTES, backups=BACKUPS):
    """只用于自身已关闭的日志；每次启动保留上一轮末尾及少量历史。"""
    path = Path(path)
    for index in range(backups, 0, -1):
        source = path if index == 1 else path.with_name(path.name + "." + str(index - 1))
        target = path.with_name(path.name + "." + str(index))
        if source.is_file():
            if source.stat().st_size > limit:
                content = _tail(source, limit)
                source.write_bytes(content)
            os.replace(source, target)


class BoundedLog(io.TextIOBase):
    def __init__(self, path, fallback, *, limit=MAX_BYTES, backups=BACKUPS):
        self.path, self.fallback = Path(path), fallback
        self.limit, self.backups = limit, backups
        self._lock = threading.RLock()
        self._stream = None
        self.failed = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rotate(self.path, limit=limit, backups=backups)
            self._stream = self.path.open("ab", buffering=0)
        except OSError:
            self.failed = True

    @property
    def encoding(self):
        return "utf-8"

    def fileno(self):
        # 原生库仍沿用启动时的输出句柄，避免改变其控制台约定。
        return self.fallback.fileno()

    def write(self, value):
        with self._lock:
            if self.closed:
                raise ValueError("运行记录已关闭")
            if self.failed:
                return self.fallback.write(value)
            try:
                step = max(1, min(self.limit // 4, 4096))
                for start in range(0, len(value), step):
                    chunk = value[start:start + step].encode("utf-8", errors="replace")
                    if self._stream.tell() + len(chunk) > self.limit:
                        self._stream.close()
                        rotate(self.path, limit=self.limit, backups=self.backups)
                        self._stream = self.path.open("ab", buffering=0)
                    self._stream.write(chunk)
                return len(value)
            except OSError:
                self.failed = True
                return self.fallback.write(value)

    def flush(self):
        with self._lock:
            if self._stream is not None and not self._stream.closed:
                self._stream.flush()

    def close(self):
        with self._lock:
            if self._stream is not None and not self._stream.closed:
                self._stream.close()
            super().close()


def install(directory):
    directory = Path(directory)
    sys.stdout = BoundedLog(directory / NAMES[0], sys.stdout)
    sys.stderr = BoundedLog(directory / NAMES[1], sys.stderr)


def history(directory):
    """摘要只读已知日志的大小和时间，不读取内容或任意文件。"""
    result = []
    for name in NAMES:
        for index in range(BACKUPS + 1):
            path = Path(directory) / (name + ("." + str(index) if index else ""))
            try:
                stat = path.stat()
            except OSError:
                continue
            result.append({"name": path.name, "bytes": stat.st_size, "modified_at": stat.st_mtime})
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", type=Path, required=True)
    args = parser.parse_args()
    args.prepare.mkdir(parents=True, exist_ok=True)
    for name in NAMES[2:]:
        try:
            rotate(args.prepare / name)
        except OSError:
            print("上次原生启动记录暂时无法归档，继续启动。", file=sys.stderr)
