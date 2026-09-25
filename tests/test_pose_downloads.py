"""从官方动作库下载动作：只装验得过签名的，读盘时再验一遍。

动作文件里有识别规则，装上就决定这台电脑把什么动作认成哪个键。所以和软件更新一样
只信一把公钥：下载时验，每次启动读盘时再验——本机的文件也可能被别的程序改过。
"""

from __future__ import annotations

import base64
import json

import pytest

from conftest import official_pose_docs
from motioncontrol.pose_downloads import PoseActionStore, PoseDownloadError, verify
from motioncontrol_shared.pose_library import canonical_bytes

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

KEY = ec.generate_private_key(ec.SECP256R1())
PUBLIC = KEY.public_key()


def signed(ident: str, key=KEY, change=None) -> tuple[str, str]:
    doc = next(doc for doc in official_pose_docs() if doc["id"] == ident)
    payload = canonical_bytes(doc)
    signature = key.sign(payload, ec.ECDSA(hashes.SHA256()))
    if change is not None:
        payload = change(payload)
    return base64.b64encode(payload).decode("ascii"), base64.b64encode(signature).decode("ascii")


def test_a_signed_action_is_installed_and_survives_a_restart(tmp_path):
    path = tmp_path / "pose_actions.json"
    store = PoseActionStore(path, public_key=PUBLIC)
    doc = store.install(*signed("jumping_jack"), expected_id="jumping_jack")
    assert doc["name"] == "开合跳"
    again = PoseActionStore(path, public_key=PUBLIC)
    assert [doc["id"] for doc in again.docs()] == ["jumping_jack"]
    assert again.revision("jumping_jack") == doc["revision"]


def test_a_changed_byte_breaks_the_signature():
    document, signature = signed("squat", change=lambda payload: payload.replace("下蹲".encode(), "上蹲".encode()))
    with pytest.raises(PoseDownloadError, match="签名验不过"):
        verify(document, signature, PUBLIC)


def test_a_file_signed_by_someone_else_is_refused():
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(PoseDownloadError, match="签名验不过"):
        verify(*signed("squat", key=other), PUBLIC)


def test_getting_a_different_action_than_asked_for_is_refused(tmp_path):
    store = PoseActionStore(tmp_path / "pose_actions.json", public_key=PUBLIC)
    with pytest.raises(PoseDownloadError, match="不是同一个"):
        store.install(*signed("squat"), expected_id="jumping_jack")
    assert store.docs() == []


def test_a_file_tampered_with_on_disk_is_dropped_at_startup(tmp_path):
    path = tmp_path / "pose_actions.json"
    store = PoseActionStore(path, public_key=PUBLIC)
    store.install(*signed("squat"))
    store.install(*signed("hands_up"))
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = json.loads(base64.b64decode(data["actions"][0]["document"]))
    raw["rule"]["when"] = ["<", 0, 1]  # 改成"永远成立"
    data["actions"][0]["document"] = base64.b64encode(canonical_bytes(raw)).decode("ascii")
    path.write_text(json.dumps(data), encoding="utf-8")
    again = PoseActionStore(path, public_key=PUBLIC)
    assert [doc["id"] for doc in again.docs()] == ["hands_up"]
    assert "验不过" in again.last_error


def test_removing_an_action_forgets_it(tmp_path):
    path = tmp_path / "pose_actions.json"
    store = PoseActionStore(path, public_key=PUBLIC)
    store.install(*signed("squat"))
    assert store.remove("squat") is True
    assert store.remove("squat") is False
    assert PoseActionStore(path, public_key=PUBLIC).docs() == []


def test_the_official_files_would_verify_against_the_desktop_key_once_signed():
    """仓库里的 signatures.json（如果已经签过）必须是电脑端内置那把公钥认得的。

    签名要用你本机的私钥（tools/sign_pose_library.py），这里没有私钥签不了；签过之后
    这条保证签名和电脑端对得上。
    """
    from pathlib import Path

    from motioncontrol.pose_downloads import _public_key

    signatures = Path(__file__).resolve().parent.parent / "cloud" / "official_poses" / "signatures.json"
    if not signatures.is_file():
        pytest.skip("官方动作库还没签名：跑 python tools/sign_pose_library.py")
    entries = json.loads(signatures.read_text(encoding="utf-8"))["actions"]
    docs = {doc["id"]: doc for doc in official_pose_docs()}
    for ident, entry in entries.items():
        payload = canonical_bytes(docs[ident])
        verify(base64.b64encode(payload).decode("ascii"), entry["signature"], _public_key())
