"""让手机喊一嗓子就能找到这台电脑，而不是把整个网段挨个敲一遍。

手机原来靠三件事找电脑：试上次记住的地址、试人工填的地址、扫自己所在的 /24。
三件都不可靠。实测记录在 WORKLOG.md：手机热点／USB 共享的网段会在一次会话里
**整个变掉**（10.246.192.18 → 10.57.177.18 → 10.245.40.245），记住的地址连网段
都不在了；而扫描对 /20、/16 的公司和学校网络直接放弃，那里等于没有发现。

改成 UDP 探询／应答之后，这两个问题一起消失：手机往自己每个接口的定向广播地址
发一个包，在这个链路上的电脑单播回一份地址表。**它不依赖任何记住的东西，也和
网段多大无关**——往 /16 的广播地址发一个包就触达整个 /16。

为什么是广播而不是 mDNS：mDNS 的应答也是组播，安卓要收组播必须拿 MulticastLock，
而那需要申请 CHANGE_WIFI_MULTICAST_STATE 权限。这里"广播问、单播答"，单播是明确
发给对方的帧，不受 WiFi 芯片省电过滤影响，于是手机端一个新权限都不用加。

一台常驻、无认证、回 JSON 的 UDP 服务就是一台潜在的 DDoS 反射器，所以下面第一条
硬约束是 **回包不得长于探询包**（探询必须填充到 512 字节）。放大系数不超过 1，
意味着从数学上它不可能被当成放大器用——伪造源地址打别人，受害者收到的字节数不会
超过攻击者自己发出去的。其余几条（源地址过滤、速率限制、固定回包形状）是纵深。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from collections import deque
from typing import Callable

# 探询包的前缀。固定字面量，不是校验和——它只用来让应答器无视扫到这个端口的
# 其他流量，不承担任何安全职责。
MAGIC_QUERY = b"MC-DISCOVER-1"
MAGIC_REPLY = "mc-here-1"

# 探询必须至少这么长（不够就填 0）。这是反放大的另一半：回包上限也是它，
# 于是放大系数天然 ≤ 1。
MIN_QUERY_BYTES = 512
MAX_REPLY_BYTES = 512

# 回包里最多带几条地址。多网卡机器地址可能很多，但手机只需要够它挑一条能通的。
MAX_CANDIDATES = 6

# 每收一个包就枚举一次网卡 = 一次系统调用，洪水下会变成 CPU 放大器。
CANDIDATE_TTL_S = 2.0

GLOBAL_RATE = 30            # 每秒总应答上限
PER_SOURCE_RATE = 5         # 每秒单个源地址上限
RATE_WINDOW_S = 1.0
# 源地址字典必须封顶。不封顶的话，伪造源 IP 的洪水会把它撑大成内存 DoS——
# 限速本身反而成了攻击面。
MAX_TRACKED_SOURCES = 256

NONCE_MAX_LEN = 16


def should_answer(source_ip: str) -> bool:
    """只回同一片局域网里的人。

    判据是"不是公网地址"而不是逐段白名单，因为要覆盖的段比想象的多：除了常见的
    10/172.16/192.168，还有运营商级 NAT 的 100.64.0.0/10（一些手机共享网络就落在
    那里）、以及 USB 共享没拿到 DHCP 时的链路本地 169.254.x——那恰恰是最需要被发现
    的场景。回环也保留，单测和本机诊断要用。
    """
    try:
        address = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    return not address.is_global


def _nonce_of(query: bytes) -> str | None:
    """探询里带的随机串，原样回显，让手机能把应答和自己的那一轮对上。

    回显是回包里唯一由对方决定的内容，所以它的长度和字符集都要卡死：否则它就是
    一个可以把回包撑大的旋钮，前面那条"回包不长于探询"的保证会被绕过。
    """
    body = query[len(MAGIC_QUERY):].split(b"\0", 1)[0]
    if not body:
        return ""
    try:
        parsed = json.loads(body.decode("utf-8", "strict"))
    except (ValueError, UnicodeDecodeError):
        return None
    nonce = parsed.get("nonce") if isinstance(parsed, dict) else None
    if nonce is None:
        return ""
    if not isinstance(nonce, str) or len(nonce) > NONCE_MAX_LEN:
        return None
    if not all(character in "0123456789abcdefABCDEF" for character in nonce):
        return None
    return nonce


def build_reply(query: bytes, *, candidates: list[dict], name: str, version: str,
                instance: str) -> bytes | None:
    """该不该答、答什么。纯函数，不碰 socket，所以能直接单测。

    返回 None 表示这个包不该被回应——魔数不对、太短、nonce 不合法，或者装不下。
    """
    if not query.startswith(MAGIC_QUERY):
        return None
    if len(query) < MIN_QUERY_BYTES:
        return None
    nonce = _nonce_of(query)
    if nonce is None:
        return None

    budget = min(MAX_REPLY_BYTES, len(query))
    # USB 排在前面（local_endpoints 已经按链路质量排好序），所以装不下时从尾部
    # 砍，砍掉的是较差的那些。
    trimmed = list(candidates[:MAX_CANDIDATES])
    while True:
        payload = {
            "magic": MAGIC_REPLY,
            "nonce": nonce,
            "name": name,
            "version": version,
            "port": trimmed[0]["port"] if trimmed else None,
            "instance": instance,
            "candidates": trimmed,
        }
        reply = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(reply) <= budget:
            break
        if not trimmed:
            return None
        trimmed.pop()

    # 发送前最后一道断言。上面的循环已经保证了它，但这条不变式值钱到应该有
    # 两道锁——它一旦破掉，这个服务就成了别人的放大器。
    if len(reply) > len(query):
        return None
    return reply


class _RateLimiter:
    """令牌不是按桶算的，是按窗口内的时间戳算——这样不用后台线程补令牌。"""

    def __init__(self, global_rate: int = GLOBAL_RATE,
                 per_source_rate: int = PER_SOURCE_RATE) -> None:
        self.global_rate = int(global_rate)
        self.per_source_rate = int(per_source_rate)
        self._global: deque[float] = deque()
        self._sources: dict[str, deque[float]] = {}

    def allow(self, source_ip: str, now: float) -> bool:
        self._prune(now)
        if len(self._global) >= self.global_rate:
            return False
        stamps = self._sources.get(source_ip)
        if stamps is None:
            if len(self._sources) >= MAX_TRACKED_SOURCES:
                # 满了就淘汰最久没说话的那个。宁可对某个源少限一次速，也不能让
                # 字典无限长。
                oldest = min(self._sources, key=lambda key: self._sources[key][-1])
                del self._sources[oldest]
            stamps = self._sources[source_ip] = deque()
        if len(stamps) >= self.per_source_rate:
            return False
        stamps.append(now)
        self._global.append(now)
        return True

    def _prune(self, now: float) -> None:
        edge = now - RATE_WINDOW_S
        while self._global and self._global[0] < edge:
            self._global.popleft()
        for key in list(self._sources):
            stamps = self._sources[key]
            while stamps and stamps[0] < edge:
                stamps.popleft()
            if not stamps:
                del self._sources[key]

    @property
    def tracked(self) -> int:
        return len(self._sources)


class DiscoveryResponder:
    """UDP 8765 上的应答器。和两个 HTTP 面并列，但它坏了不影响产品。

    TCP 8765 绑不上时 server.py 会直接退出，因为那是产品本身。发现只是让连接更
    省事，所以这里绑不上只记一条原因然后继续——"发现坏了"不该等于"软件坏了"。
    """

    def __init__(self, host: str, port: int, *,
                 candidates: Callable[[], list[dict]],
                 name: str, version: str, instance: str) -> None:
        self._host = host
        self._port = int(port)
        self._candidates = candidates
        self._name = name
        self._version = version
        self._instance = instance
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._limiter = _RateLimiter()
        self._cached: tuple[float, list[dict]] | None = None
        self.last_error: str | None = None
        self.answered = 0

    @property
    def bound(self) -> bool:
        return self._socket is not None

    @property
    def port(self) -> int:
        """实际绑到的端口。传 0 时测试要用它。"""
        if self._socket is None:
            return 0
        return int(self._socket.getsockname()[1])

    def start(self) -> bool:
        if self._socket is not None:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # 不设 SO_REUSEADDR：Windows 上 UDP 的这个选项允许别的进程抢走已经
            # 绑住的端口，和 POSIX 语义不同。宁可绑不上，也不要被劫持。
            # 必须绑 0.0.0.0：绑具体单播地址在 Windows 上收不到定向广播，
            # 而定向广播正是手机发现用的那一种。
            sock.bind(("0.0.0.0", self._port))
            sock.settimeout(0.5)
        except OSError as exc:
            self.last_error = str(exc)
            return False
        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="motion-discovery",
                                        daemon=True)
        self._thread.start()
        return True

    def close(self, timeout: float = 1.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
        sock, self._socket = self._socket, None
        if sock is not None:
            sock.close()

    def _fresh_candidates(self, now: float) -> list[dict]:
        if self._cached is not None and now - self._cached[0] < CANDIDATE_TTL_S:
            return self._cached[1]
        try:
            found = list(self._candidates())
        except Exception:
            found = []
        self._cached = (now, found)
        return found

    def _serve(self) -> None:
        sock = self._socket
        assert sock is not None
        while not self._stop.is_set():
            # 用超时轮询停止标志，而不是从别的线程 close 这个 socket——后者在
            # Windows 上是 race，偶尔会抛在 recvfrom 里。
            try:
                query, origin = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            now = time.monotonic()
            source_ip = origin[0]
            if not should_answer(source_ip):
                continue
            if not self._limiter.allow(source_ip, now):
                continue
            reply = build_reply(query,
                                candidates=self._fresh_candidates(now),
                                name=self._name, version=self._version,
                                instance=self._instance)
            if reply is None:
                continue
            try:
                sock.sendto(reply, origin)
            except OSError:
                continue
            self.answered += 1
