"""Fetching configs from the cloud, on the desktop side.

This is the half that runs on a player's machine, so it has the constraints the
desktop always has: no pip, no requests, standard library only. It talks to the
cloud over ``urllib`` and is deliberately ignorant of how the cloud stores
anything -- it asks for public configs and downloads bytes.

Three rules it follows, and the reason for each:

**Verify the digest before doing anything with the bytes.** The cloud reports a
``canonical_sha256`` alongside every version and serves the version's bytes
unchanged. Checking it here is not paranoia about the network -- TLS covers
that -- it is what makes "this file is the file that was published" something
the desktop knows rather than assumes, including when the download came over
plain http from a self-hosted instance.

**Validate locally anyway.** The cloud already ran the shared validator, and
this runs it again. The cloud is a service the desktop trusts to be available,
not one it trusts to be correct: a bug or a compromise there must not be able
to put a config into someone's controller that the local rules would reject.
It is also the same validator, so a disagreement is a bug worth finding.

**Never write a config file directly.** Applying goes through the same code
path the local UI uses, so every existing validator, lock and refresh runs.
That path lives in server.py; this module only prepares and hands over.

Timeouts are short and every failure is a plain exception with a Chinese
message, because the one thing that must never happen is the desktop hanging on
a cloud that is down. Local control does not depend on this module at all.
"""

from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from motioncontrol_shared.canonical import canonicalize
from motioncontrol_shared.sync_allowlist import CLOUD_SYNC_ALLOWLIST
from motioncontrol_shared.user_layout import USER_DATA_FILES

# Short enough that a dead server is an error rather than a freeze. The desktop
# is a real-time controller; a request that blocks the admin thread for thirty
# seconds is indistinguishable from a crash to whoever is using it.
CONNECT_TIMEOUT_S = 6.0
DOWNLOAD_TIMEOUT_S = 15.0

# Roughly 20x the largest real config. The cloud enforces its own cap; this one
# stops a hostile or broken server from streaming until memory runs out.
MAX_DOWNLOAD_BYTES = 256 * 1024

USER_AGENT = "MotionControl-Desktop/2.0"


class CloudError(Exception):
    """Anything that went wrong talking to the cloud, with a message for the user."""


@dataclass(frozen=True)
class RemoteConfig:
    """One downloaded version, already verified and validated."""

    profile_id: str
    version_id: str
    doc_type: str
    revision_no: int
    title: str
    owner_name: str
    game_id: str | None
    sha256: str
    document: dict


def _request(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Read one byte past the cap so an oversized body is detected
            # rather than silently truncated into something that fails to parse.
            payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
        except Exception:
            pass
        raise CloudError(detail or f"云端返回 {exc.code}") from None
    except urllib.error.URLError as exc:
        raise CloudError(f"连不上云端：{exc.reason}") from None
    except TimeoutError:
        raise CloudError("云端响应超时") from None
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise CloudError("云端返回的内容过大")
    return payload


def _json(url: str, timeout: float = CONNECT_TIMEOUT_S):
    try:
        return json.loads(_request(url, timeout).decode("utf-8"))
    except json.JSONDecodeError:
        raise CloudError("云端返回的不是有效的 JSON") from None


class CloudClient:
    """Read-only access to one cloud instance's public configs.

    Read-only on purpose for now: uploading needs an account, and account
    login on the desktop is the authorization-code flow that has not been built
    yet. Browsing and installing public configs needs no account at all, so it
    works today and gains nothing from waiting.
    """

    def __init__(self, base_url: str):
        base = str(base_url or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise CloudError("云端地址必须以 http:// 或 https:// 开头")
        self.base = base

    @property
    def api(self) -> str:
        return f"{self.base}/api/v1"

    def health(self) -> dict:
        return _json(f"{self.api}/health")

    def browse(self, doc_type: str = "", game_id: str = "", limit: int = 30) -> list[dict]:
        query = {"limit": str(limit)}
        if doc_type:
            query["doc_type"] = doc_type
        if game_id:
            query["game_id"] = game_id
        data = _json(f"{self.api}/public/profiles?{urllib.parse.urlencode(query)}")
        if not isinstance(data, list):
            raise CloudError("云端返回的列表格式不对")
        return data

    def profile(self, profile_id: str) -> dict:
        return _json(f"{self.api}/profiles/{urllib.parse.quote(profile_id)}")

    def versions(self, profile_id: str) -> list[dict]:
        data = _json(f"{self.api}/profiles/{urllib.parse.quote(profile_id)}/versions")
        if not isinstance(data, list):
            raise CloudError("云端返回的版本列表格式不对")
        return data

    def fetch(self, profile_id: str, version_id: str = "") -> RemoteConfig:
        """Download one version, check its digest, and validate it locally."""
        profile = self.profile(profile_id)
        doc_type = str(profile.get("doc_type", ""))
        if doc_type not in CLOUD_SYNC_ALLOWLIST:
            raise CloudError(f"不支持的配置类型：{doc_type}")

        version = None
        if version_id:
            version = next((item for item in self.versions(profile_id)
                            if item.get("id") == version_id), None)
            if version is None:
                raise CloudError("找不到这个版本")
        else:
            version = profile.get("current_version")
            if not isinstance(version, dict):
                raise CloudError("这个配置还没有任何版本")

        payload = _request(
            f"{self.api}/profiles/{urllib.parse.quote(profile_id)}"
            f"/versions/{urllib.parse.quote(version['id'])}/download",
            DOWNLOAD_TIMEOUT_S)

        expected = str(version.get("canonical_sha256", ""))
        actual = sha256(payload).hexdigest()
        if not expected or actual != expected:
            raise CloudError(
                "下载的内容和云端记录的校验值不一致，已中止。\n"
                f"  云端记录 {expected[:16] or '(缺失)'}\n"
                f"  实际内容 {actual[:16]}")

        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CloudError("下载的配置不是有效的 JSON") from None

        # The cloud validated this already. Running the same rules again is the
        # point: a cloud that has been tampered with must not be able to hand
        # the controller something the local rules would refuse.
        try:
            local = canonicalize(doc_type, document)
        except ValueError as exc:
            raise CloudError(f"这份配置在本地校验不通过：{exc}") from None
        if local.sha256 != actual:
            raise CloudError(
                "本地规范化的结果和云端不一致，说明两边的规则版本对不上。\n"
                "请更新电脑端后再试。")

        return RemoteConfig(
            profile_id=profile_id,
            version_id=str(version["id"]),
            doc_type=doc_type,
            revision_no=int(version.get("revision_no", 0)),
            title=str(profile.get("title", "")),
            owner_name=str(profile.get("owner_name", "")),
            game_id=profile.get("game_id"),
            sha256=actual,
            document=local.data,
        )


def backup_user_data(user_root: Path) -> Path | None:
    """Copy the syncable files aside before anything overwrites them.

    Only the three files a cloud install can touch are copied. Backing up the
    whole directory would also copy scene_reference.jpg, which is a photo and
    far larger than everything else put together, and which nothing here can
    change anyway.

    Returns the backup directory, or None when there was nothing to back up.
    """
    user_root = Path(user_root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = user_root / "cloud_backup" / stamp
    copied = 0
    for key in sorted(CLOUD_SYNC_ALLOWLIST):
        source = user_root / USER_DATA_FILES[key]
        if source.is_file():
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination / source.name)
            copied += 1
    return destination if copied else None
