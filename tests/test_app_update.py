"""电脑端自更新：验签、防降级、跑不起来自己退回去。

这条通道落地的是要执行的代码。谁能把清单递到这台机器面前，谁就决定它执行什么
——"它是从我服务器来的"是假设，不是论证。所以这里钉的全是"拒绝"的那一半：没
签名不装、签名对不上不装、比手上这份旧不装、路径想跳出暂存目录不装。

另一半是退回：签名只证明包是发布者发的，不证明它跑得起来。一个能让服务起不来
的改动照样签得好好的，而修它的补丁要走的正是这条通道。
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from motioncontrol import app_update  # noqa: E402

pytest.importorskip("cryptography")


@pytest.fixture()
def keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public


@pytest.fixture()
def trust(monkeypatch, keypair):
    """让模块信任这套测试密钥，而不是编译进去的那把真钥匙。"""
    import base64

    _, public = keypair
    monkeypatch.setattr(app_update, "PUBLIC_KEY_B64",
                        base64.b64encode(public).decode("ascii"))
    return keypair[0]


def sign(private, digest: str, issued_at: float) -> dict:
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    payload = json.dumps({"digest": digest, "issued_at": int(issued_at)},
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = private.sign(payload, ec.ECDSA(hashes.SHA256()))
    return {"payload": base64.b64encode(payload).decode("ascii"),
            "signature": base64.b64encode(signature).decode("ascii")}


def test_a_correctly_signed_manifest_is_accepted(trust):
    manifest = {"digest": "abc", **sign(trust, "abc", 1000)}
    assert app_update.verify(manifest, 0) == 1000


def test_an_unsigned_manifest_is_refused(trust):
    """不验签的第一版会让所有已装机器永远接受不验签的包。"""
    assert app_update.verify({"digest": "abc"}, 0) == -1


def test_a_signature_from_the_wrong_key_is_refused(trust, keypair):
    from cryptography.hazmat.primitives.asymmetric import ec

    stranger = ec.generate_private_key(ec.SECP256R1())
    assert app_update.verify({"digest": "abc", **sign(stranger, "abc", 1000)}, 0) == -1


def test_a_signature_for_other_contents_is_refused(trust):
    """签的是别的一份包，不能拿来给这一份背书。"""
    manifest = {"digest": "这一份", **sign(trust, "另一份", 1000)}
    assert app_update.verify(manifest, 0) == -1


def test_a_tampered_signature_is_refused(trust):
    manifest = {"digest": "abc", **sign(trust, "abc", 1000)}
    blob = list(manifest["signature"])
    blob[10] = "A" if blob[10] != "A" else "B"
    manifest["signature"] = "".join(blob)
    assert app_update.verify(manifest, 0) == -1


def test_an_older_bundle_cannot_be_pushed_back(trust):
    """自己的旧包也是签对的。没有时间戳，中间人可以把上个月那版递过来。"""
    manifest = {"digest": "old", **sign(trust, "old", 500)}
    assert app_update.verify(manifest, 1000) == -1
    assert app_update.verify(manifest, 400) == 500


# --- 换包与退回 --------------------------------------------------------------

def make_install(root: Path, marker: str = "v1") -> Path:
    app = root / "app"
    (app / "motioncontrol").mkdir(parents=True)
    (app / "server.py").write_text(marker, encoding="utf-8")
    return app


def stage(root: Path, marker: str, digest: str = "d2") -> Path:
    staging = root / app_update.STAGING_NAME
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "server.py").write_text(marker, encoding="utf-8")
    (staging / app_update.ISSUED_MARKER).write_text("2000", encoding="utf-8")
    (staging / app_update.COMPLETE_MARKER).write_text(digest, encoding="utf-8")
    return staging


def test_nothing_staged_changes_nothing(tmp_path):
    app = make_install(tmp_path)
    assert app_update.promote(app) == "nothing-staged"
    assert (app / "server.py").read_text(encoding="utf-8") == "v1"


def test_a_staged_bundle_is_swapped_in(tmp_path):
    app = make_install(tmp_path)
    stage(tmp_path, "v2")
    assert app_update.promote(app) == "promoted"
    assert (app / "server.py").read_text(encoding="utf-8") == "v2"
    assert (tmp_path / app_update.BOOT_MARKER).is_file(), "没留记号就失去了退回的能力"


def test_a_half_downloaded_bundle_is_not_installed(tmp_path):
    """没有完成记号 = 下了一半。一个文件一个文件地装上去比不装更糟。"""
    app = make_install(tmp_path)
    staging = tmp_path / app_update.STAGING_NAME
    staging.mkdir()
    (staging / "server.py").write_text("半截", encoding="utf-8")
    assert app_update.promote(app) == "nothing-staged"
    assert (app / "server.py").read_text(encoding="utf-8") == "v1"


def test_a_bundle_that_never_booted_rolls_itself_back(tmp_path):
    """签名不证明它跑得起来，而修它的补丁要走的正是这条通道。"""
    app = make_install(tmp_path)
    stage(tmp_path, "会白屏的那一版")
    app_update.promote(app)
    assert (app / "server.py").read_text(encoding="utf-8") == "会白屏的那一版"

    # 服务没能跑到 boot_ok，记号还在。下一次启动：
    assert app_update.promote(app) == "rolled-back"
    assert (app / "server.py").read_text(encoding="utf-8") == "v1", "没退回去"


def test_a_bundle_that_booted_stays(tmp_path):
    app = make_install(tmp_path)
    stage(tmp_path, "v2")
    app_update.promote(app)
    app_update.boot_ok(app)
    assert not (tmp_path / app_update.BOOT_MARKER).exists()

    assert app_update.promote(app) == "nothing-staged"
    assert (app / "server.py").read_text(encoding="utf-8") == "v2", "站住了的版本被退回了"


def test_boot_ok_clears_the_backup(tmp_path):
    """留着上一份会让发布目录一直占着双份。"""
    app = make_install(tmp_path)
    stage(tmp_path, "v2")
    app_update.promote(app)
    assert (tmp_path / "app_previous").is_dir()
    app_update.boot_ok(app)
    assert not (tmp_path / "app_previous").exists()


# --- 下载 --------------------------------------------------------------------

class FakeServer:
    """一个假的更新源，不起真 HTTP。"""

    def __init__(self, files: dict[str, bytes], private, issued_at=2000,
                 sign_with=None, digest=None):
        import hashlib
        self.blobs = files
        listing = []
        for path in sorted(files):
            listing.append({"path": path, "size": len(files[path]),
                            "sha256": hashlib.sha256(files[path]).hexdigest()})
        self.digest = digest or app_update._listing_digest(listing)
        self.manifest = {
            "name": "pc-app", "available": True, "digest": self.digest,
            "files": listing, "total_bytes": sum(len(b) for b in files.values()),
            **(sign(sign_with or private, self.digest, issued_at)),
        }

    def fetch(self, url: str, timeout: float) -> bytes:
        if url.endswith(app_update.MANIFEST_PATH):
            return json.dumps(self.manifest).encode("utf-8")
        import urllib.parse
        wanted = urllib.parse.unquote(url.split("path=", 1)[1])
        return self.blobs[wanted]


def install_server(monkeypatch, server):
    monkeypatch.setattr(app_update, "_fetch", server.fetch)


def test_a_good_bundle_lands_in_staging(tmp_path, trust, monkeypatch):
    app = make_install(tmp_path)
    server = FakeServer({"server.py": b"new", "motioncontrol/a.py": b"aaa"}, trust)
    install_server(monkeypatch, server)

    result = app_update.check_and_stage(app)
    assert result["state"] == "ready", result
    staging = tmp_path / app_update.STAGING_NAME
    assert (staging / "server.py").read_bytes() == b"new"
    assert (staging / "motioncontrol" / "a.py").read_bytes() == b"aaa"
    assert (staging / app_update.COMPLETE_MARKER).read_text(encoding="utf-8") == server.digest


def test_an_unsigned_bundle_is_not_downloaded(tmp_path, trust, monkeypatch):
    """验签排在下载之前：一个字节都不该为没签名的包花出去。"""
    app = make_install(tmp_path)
    server = FakeServer({"server.py": b"new"}, trust)
    server.manifest.pop("signature")
    install_server(monkeypatch, server)

    assert app_update.check_and_stage(app)["state"] == "unsigned"
    assert not (tmp_path / app_update.STAGING_NAME).exists(), "没验过就已经开始下了"


def test_a_file_whose_contents_do_not_match_is_refused(tmp_path, trust, monkeypatch):
    app = make_install(tmp_path)
    server = FakeServer({"server.py": b"new"}, trust)
    server.blobs["server.py"] = "被换掉的内容".encode("utf-8")
    install_server(monkeypatch, server)

    assert app_update.check_and_stage(app)["state"] == "failed"
    assert not (tmp_path / app_update.STAGING_NAME).exists(), "坏了一半的暂存目录留下了"


@pytest.mark.parametrize("evil", ["../逃出去.py", "/绝对路径.py", "a/../../跑了.py"])
def test_a_path_that_escapes_staging_is_refused(tmp_path, trust, monkeypatch, evil):
    """清单是别人给的数据，路径不能当真。"""
    app = make_install(tmp_path)
    server = FakeServer({evil: b"x"}, trust)
    install_server(monkeypatch, server)

    assert app_update.check_and_stage(app)["state"] == "failed"
    assert not (tmp_path / "逃出去.py").exists()
    assert not (tmp_path / "跑了.py").exists()


def test_an_absurdly_large_bundle_is_refused(tmp_path, trust, monkeypatch):
    """程序本体只有 2 MB 出头。与其下完再发现不对，不如一开始就不下。"""
    app = make_install(tmp_path)
    server = FakeServer({"server.py": b"new"}, trust)
    server.manifest["total_bytes"] = app_update.MAX_TOTAL_BYTES + 1
    install_server(monkeypatch, server)

    assert app_update.check_and_stage(app)["state"] == "refused"


def test_the_same_bundle_is_not_downloaded_twice(tmp_path, trust, monkeypatch):
    app = make_install(tmp_path)
    server = FakeServer({"server.py": b"new"}, trust)
    install_server(monkeypatch, server)
    (app / app_update.COMPLETE_MARKER).write_text(server.digest, encoding="utf-8")

    assert app_update.check_and_stage(app)["state"] == "current"


def test_a_server_that_is_down_is_not_an_error(tmp_path, trust, monkeypatch):
    """更新失败绝不该拦住玩游戏。"""
    def explode(url, timeout):
        raise OSError("连不上")

    app = make_install(tmp_path)
    monkeypatch.setattr(app_update, "_fetch", explode)
    assert app_update.check_and_stage(app)["state"] == "failed"
