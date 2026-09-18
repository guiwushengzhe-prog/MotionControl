"""Hand a model directory to the phone over the local link.

The phone needs the Chinese speech model to recognise commands offline, and it
used to carry its own copy: 41.5 MB of the app's 82 MB download, for files
byte-identical to ones already sitting on the PC it is about to pair with.
Shipping the same model twice made the app expensive to distribute for no
benefit, and the phone is always paired with a PC anyway -- that is the whole
product.

A manifest plus individual files, rather than one archive:

*Resuming is free.*  Fourteen files with their own digests, so an interrupted
download continues from the next missing file instead of starting a 44 MB
archive again.

*Corruption is precise.*  A failed digest names the file that is wrong rather
than condemning the whole download.

*No archive has to exist.*  The PC has the model unpacked, because that is the
form its own recogniser loads. Zipping it on demand would mean either holding
68 MB in memory or producing an archive whose bytes -- and therefore whose
digest -- depend on when it was built.

Path traversal is not handled by sanitising the request.  A requested path is
served only if it appears in the manifest this process built by walking the
directory itself, so anything not in that list simply does not exist.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1 << 20

# 签名文件自己不进清单：它签的就是那份清单，进去就成了循环。
SIGNATURE_NAME = ".signature"

# Joins the manifest's fields. A NUL cannot occur in a path or a hex digest,
# so no combination of field values can be mistaken for a different listing.
_SEPARATOR = chr(0)


class ModelShare:
    """One model directory, offered to the phone as a manifest plus files."""

    def __init__(self, name: str, root: Path | None,
                 skip: tuple[str, ...] = ()) -> None:
        self.name = str(name)
        self.root = Path(root).resolve() if root else None
        # 相对路径前缀，命中的不进清单。手机的网页包目录里还躺着 25 MB 的模型和
        # WASM，那些永远从 APK 读，不该出现在要下载的列表里。
        self.skip = tuple(skip)
        # Digest cache keyed by (size, mtime_ns): hashing 68 MB on every
        # manifest request would make an idle phone expensive to have around.
        self._digests: dict[str, tuple[int, int, str]] = {}

    def _files(self) -> list[Path]:
        if self.root is None or not self.root.is_dir():
            return []
        # Sorted by the relative path the phone will see, not by Path order:
        # Path comparison is case-insensitive on Windows and case-sensitive on
        # Linux, which would hand out a manifest whose order depends on which
        # machine is serving it.
        found = []
        for item in self.root.rglob("*"):
            if not item.is_file():
                continue
            relative = item.relative_to(self.root).as_posix()
            if relative.startswith(self.skip) or relative == SIGNATURE_NAME:
                continue
            found.append(item)
        return sorted(found, key=lambda item: item.relative_to(self.root).as_posix())

    def _digest(self, path: Path, stat) -> str:
        key = path.as_posix()
        cached = self._digests.get(key)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return cached[2]
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        value = digest.hexdigest()
        self._digests[key] = (stat.st_size, stat.st_mtime_ns, value)
        return value

    def manifest(self) -> dict:
        """What the phone needs to fetch, and how to know it arrived intact.

        ``digest`` identifies this exact set of files.  Without it the phone
        can only ask "do I have a model", which is the wrong question the day
        the model is replaced by a better one: it would keep using the copy it
        downloaded once and never notice.  With it, one cheap request tells the
        phone whether what it has is still what the PC is offering.
        """
        files = []
        total = 0
        for path in self._files():
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append({
                "path": path.relative_to(self.root).as_posix(),
                "size": stat.st_size,
                "sha256": self._digest(path, stat),
            })
            total += stat.st_size
        # Over the listing rather than the bytes: the per-file digests are
        # already the bytes' identity, and this stays cheap to recompute.
        summary = hashlib.sha256()
        for item in files:
            summary.update(_SEPARATOR.join(
                (item["path"], str(item["size"]), item["sha256"], "")
            ).encode("utf-8"))
        return {
            "name": self.name,
            "available": bool(files),
            "digest": summary.hexdigest() if files else "",
            # 没签名就是空的。手机拿到空签名会拒绝安装——宁可不更新，也不能装
            # 一份来路不明的代码。
            **self.signature(),
            "files": files,
            "total_bytes": total,
        }

    def signature(self) -> dict:
        """The detached signature for this listing, if one was made."""
        if self.root is None:
            return {}
        path = self.root / SIGNATURE_NAME
        if not path.is_file() or path.stat().st_size > 8192:
            return {}
        try:
            import json
            signed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        payload = signed.get("payload")
        signature = signed.get("signature")
        if not isinstance(payload, str) or not isinstance(signature, str):
            return {}
        return {"payload": payload, "signature": signature}

    def resolve(self, relative: str) -> Path | None:
        """The file for a requested path, or None if it is not one we offer.

        Membership in the manifest is the check.  A path that is not in the
        list the server built by walking its own directory does not resolve,
        whatever it contains -- no separate defence against ``..`` is needed
        because nothing outside that walk can ever match.
        """
        if self.root is None:
            return None
        wanted = str(relative).replace("\\", "/").strip("/")
        for path in self._files():
            if path.relative_to(self.root).as_posix() == wanted:
                return path
        return None
