"""本机生成连接二维码；电脑编号用于重连选机，不是认证凭据。"""
from __future__ import annotations

import json

try:
    import qrcode
    import qrcode.image.svg
except ImportError:
    # 程序更新只换 app/，不换随包的 Python。加二维码之前发出的便携包里没有 qrcode，
    # 这里硬导入会让 server.py 起不来、更新被当成坏包退回。缺了就只是不出图。
    qrcode = None


def connection_code(instance: str, name: str, candidates: list[dict], port: int) -> dict:
    addresses = [{"host": "127.0.0.1", "port": port, "kind": "usb"}]
    seen = {("127.0.0.1", port)}
    for item in candidates:
        key = (item["host"], int(item["port"]))
        if key not in seen:
            addresses.append({"host": key[0], "port": key[1], "kind": item.get("kind", "lan")})
            seen.add(key)
    payload = {"type": "motioncontrol-connect", "version": 1,
               "instance": instance, "name": name, "candidates": addresses[:5]}
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if qrcode is None:
        return {"payload": payload, "text": text, "svg": None}
    qr = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathFillImage,
                     error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    return {"payload": payload, "text": text, "svg": qr.to_string(encoding="unicode")}
