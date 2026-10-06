"""手机配对钥匙：藏在连接二维码里，扫过一次码的手机以后每次连接自动带上。

以前 /ws/input 谁都能连：同一个 WiFi 里的任何设备不用扫码，直接连 8765 就能发
按键；加了网络鼠标之后还能动鼠标、点左右键。2.3.0 去掉过一版要人手输配对码的
设计（没人用）。这一版不需要人做任何事：钥匙在二维码里，扫码就是配对，之后不再
确认、不再输入。

从本机地址来的连接不要钥匙：数据线（adb reverse）连上来的手机就是这个地址，插着线
本身就说明是这台电脑的主人；本机上的程序本来就能做任何事。

钥匙存在用户目录（设备配置，永不同步）。删掉 pairing_key.txt 就等于让所有手机重新
扫一次码——手机丢了的时候用得上。
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
from pathlib import Path

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
# 手机那边原样存、原样带回来；128 位随机数，猜不到。
KEY_BYTES = 16


class PairingKey:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._key: str | None = None

    def key(self) -> str:
        """这台电脑的钥匙；第一次用到时生成并存下。"""
        with self._lock:
            if self._key is None:
                self._key = self._load() or self._create()
            return self._key

    def accepts(self, candidate: object) -> bool:
        if not isinstance(candidate, str) or not KEY_RE.match(candidate):
            return False
        return hmac.compare_digest(candidate.encode("ascii"), self.key().encode("ascii"))

    def _load(self) -> str | None:
        try:
            saved = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return saved if KEY_RE.match(saved) else None

    def _create(self) -> str:
        made = secrets.token_urlsafe(KEY_BYTES)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(made, encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            # 存不下就只在这次运行里有效：扫过码的手机下次启动要再扫一次，但不会
            # 因此谁都能连。
            pass
        return made
