"""官方动作库：官方发布的动作，谁都能看、谁都能下载。

和配置分享是两回事。配置是用户传上来的，存在数据库里；官方动作是仓库里
cloud/official_poses/ 下的文件，随部署上来，接口只读。电脑端只自带原地踏步和小腿
向后抬起，别的动作都从这里下载——识别规则就在动作文件里，下载了才认得出。

只发布签了名、而且签完没再改过的动作（见 tools/sign_pose_library.py）。这里不验签
名本身——验签是电脑端的事，它只信自己内置的那把公钥，不信服务器；这里只核对内容的
sha256 和签名记录对得上，免得发出去一个电脑端注定不认的文件。

发出去的字节就是签名签的那串（pose_library.canonical_bytes），电脑端拿它原样验签。
"""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException, status

from motioncontrol_shared import pose_library
from motioncontrol_shared.pose_rules import ENGINE_VERSION

router = APIRouter(prefix="/pose-library", tags=["pose-library"])

OFFICIAL_DIR = Path(__file__).resolve().parent.parent.parent / "official_poses"
SIGNATURES_NAME = "signatures.json"
SIGNATURES_SCHEMA = "motioncontrol.pose_signatures.v1"


@lru_cache(maxsize=1)
def catalog() -> dict:
    """{"published": {id: 发布信息}, "all": [全部校验得过的动作]}。部署时读一次。"""
    folder = OFFICIAL_DIR
    signatures: dict = {}
    path = folder / SIGNATURES_NAME
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") == SIGNATURES_SCHEMA:
            signatures = dict(data.get("actions", {}))
    published: dict[str, dict] = {}
    everything: list[dict] = []
    for file in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        if file.name == SIGNATURES_NAME:
            continue
        try:
            doc = pose_library.normalize_action(json.loads(file.read_text(encoding="utf-8")))
        except (ValueError, json.JSONDecodeError):
            # 坏文件不发布，也不让整个服务起不来。发布前的测试会先把它拦住。
            continue
        everything.append(doc)
        payload = pose_library.canonical_bytes(doc)
        digest = hashlib.sha256(payload).hexdigest()
        entry = signatures.get(doc["id"])
        if not isinstance(entry, dict) or entry.get("sha256") != digest or not entry.get("signature"):
            continue
        published[doc["id"]] = {"doc": doc, "payload": payload, "sha256": digest,
                                "signature": str(entry["signature"])}
    # 云端要把别人配置里的「开合跳」说成开合跳：名字按全部官方动作登记，不只发布了的。
    pose_library.register(everything)
    return {"published": published, "all": everything}


@router.get("")
async def list_actions() -> dict:
    """列表：名字、怎么做、示范、星级、会扫过哪些圈。浏览用，不含识别规则。"""
    # 身体动作在前、姿势在后；同一类里从容易上手的排起。
    published = sorted(catalog()["published"].values(), key=lambda item: (
        item["doc"]["group"] != "motion", item["doc"]["ratings"]["difficulty"],
        -item["doc"]["ratings"]["intensity"], item["doc"]["id"]))
    return {
        "engine": ENGINE_VERSION,
        "rating_names": pose_library.RATING_NAMES,
        "body_part_names": pose_library.BODY_PART_NAMES,
        "actions": [{**pose_library.doc_payload(item["doc"]), "sha256": item["sha256"]}
                    for item in published],
    }


@router.get("/{action_id}")
async def download(action_id: str) -> dict:
    """一个动作的完整文件（含识别规则）和它的签名。"""
    item = catalog()["published"].get(action_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "官方动作库里没有这个动作")
    return {
        "id": action_id,
        "revision": item["doc"]["revision"],
        "sha256": item["sha256"],
        "document": base64.b64encode(item["payload"]).decode("ascii"),
        "signature": item["signature"],
    }


# 启动时就读一次：配置详情页要把别人配置里的 motion.jumping_jack 说成「开合跳」，
# 不能等到有人先打开官方动作库。
catalog()
