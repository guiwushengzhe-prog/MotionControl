"""手机喊一嗓子，这台电脑该不该答、答什么。

这个模块在局域网上常驻、不认证、回 JSON，也就是说它天生具备被当成 DDoS 放大器
的全部条件。所以下面用例里最重要的一条不是"能不能发现"，而是
**回包永远不长于探询包**——放大系数 ≤ 1 的时候，伪造源地址打别人这件事在数学上
就不成立了。其余几条（源地址过滤、限速、字典封顶）是纵深，单独哪一条失效都不该
让前面那条保证垮掉。
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from motioncontrol import discovery as dc
from motioncontrol.version import VERSION


def query(nonce: str = "a1b2", *, pad_to: int = dc.MIN_QUERY_BYTES,
          magic: bytes = dc.MAGIC_QUERY) -> bytes:
    body = magic + json.dumps({"nonce": nonce}).encode("utf-8")
    return body + b"\0" * max(0, pad_to - len(body))


def candidates(count: int = 2) -> list[dict]:
    made = [{"host": "10.119.231.59", "port": 8765, "kind": "usb"}]
    for index in range(1, count):
        made.append({"host": f"192.168.1.{index}", "port": 8765, "kind": "lan"})
    return made[:count]


def reply_of(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def build(raw: bytes | None = None, **kwargs) -> bytes | None:
    options = {"candidates": candidates(), "name": "PC", "version": VERSION,
               "instance": "a1b2c3d4e5f6", "pairing_required": False}
    options.update(kwargs)
    return dc.build_reply(query() if raw is None else raw, **options)


# --- 回包不长于探询包：这条塌了，整个模块就是别人的放大器 ------------------

@pytest.mark.parametrize("pad_to", [512, 700, 1024, 2048])
@pytest.mark.parametrize("count", [0, 1, 2, 6, 50])
def test_a_reply_is_never_longer_than_the_query(pad_to, count):
    asked = query(pad_to=pad_to)
    answer = build(asked, candidates=candidates(count) if count else [])
    if answer is None:
        return
    assert len(answer) <= len(asked)


def test_a_long_name_cannot_grow_the_reply_past_the_query():
    """机器名是外面来的，不该成为撑大回包的旋钮。"""
    asked = query()
    answer = build(asked, name="名" * 300)
    assert answer is None or len(answer) <= len(asked)


# --- 什么样的探询才值得回 --------------------------------------------------

def test_a_normal_query_is_answered():
    answer = build()
    assert answer is not None
    payload = reply_of(answer)
    assert payload["magic"] == dc.MAGIC_REPLY
    assert payload["version"] == VERSION
    assert payload["candidates"][0]["kind"] == "usb"


def test_a_short_query_is_ignored():
    """填充是反放大的一半。不够长就不是我们的协议，不回。"""
    assert build(query(pad_to=64)) is None


def test_a_foreign_magic_is_ignored():
    assert build(query(magic=b"SOMETHING-ELSE")) is None


@pytest.mark.parametrize("nonce", ["z" * 4, "a" * 17, "!!", "中文"])
def test_a_bogus_nonce_is_refused(nonce):
    assert build(query(nonce=nonce)) is None


def test_a_missing_nonce_still_answers():
    """没带 nonce 是合法的——手机只发一轮时不需要它。"""
    asked = dc.MAGIC_QUERY + b"\0" * dc.MIN_QUERY_BYTES
    answer = build(asked)
    assert answer is not None
    assert reply_of(answer)["nonce"] == ""


def test_the_nonce_comes_back_unchanged():
    answer = build(query(nonce="deadBEEF"))
    assert reply_of(answer)["nonce"] == "deadBEEF"


# --- 回包里有什么、没有什么 ------------------------------------------------

def test_too_many_candidates_are_trimmed_from_the_worse_end():
    answer = build(candidates=candidates(50))
    listed = reply_of(answer)["candidates"]
    assert len(listed) <= dc.MAX_CANDIDATES
    assert listed[0]["kind"] == "usb", "砍的应该是较差的链路，不是最好的那条"


def test_the_reply_carries_no_adapter_names():
    """网卡友好名可能被用户改成带个人信息的字符串，而且长度不可控。"""
    answer = build(candidates=[{"host": "10.0.0.2", "port": 8765, "kind": "usb",
                                "adapter": "张三的 USB 网卡"}])
    assert "张三" not in answer.decode("utf-8")


# --- 谁配收到回复 ----------------------------------------------------------

@pytest.mark.parametrize("source", ["192.168.1.5", "10.0.0.9", "172.16.3.1",
                                    "127.0.0.1", "169.254.7.7",
                                    # 运营商级 NAT。一些手机共享网络就落在这里，
                                    # 漏了它等于在那些手机上关掉自动发现。
                                    "100.64.3.9"])
def test_local_sources_are_answered(source):
    assert dc.should_answer(source) is True


@pytest.mark.parametrize("source", ["8.8.8.8", "1.1.1.1", "93.184.216.34",
                                    "不是地址", ""])
def test_outside_sources_are_not(source):
    assert dc.should_answer(source) is False


# --- 限速本身不能成为攻击面 ------------------------------------------------

def test_one_source_cannot_monopolise():
    limiter = dc._RateLimiter()
    now = 100.0
    allowed = [limiter.allow("192.168.1.5", now) for _ in range(dc.PER_SOURCE_RATE + 3)]
    assert allowed[:dc.PER_SOURCE_RATE] == [True] * dc.PER_SOURCE_RATE
    assert not any(allowed[dc.PER_SOURCE_RATE:])


def test_the_window_reopens():
    limiter = dc._RateLimiter()
    for _ in range(dc.PER_SOURCE_RATE):
        limiter.allow("192.168.1.5", 100.0)
    assert limiter.allow("192.168.1.5", 100.0) is False
    assert limiter.allow("192.168.1.5", 100.0 + dc.RATE_WINDOW_S + 0.01) is True


def test_spoofed_sources_cannot_grow_the_table():
    """伪造源 IP 的洪水如果能把字典撑大，限速就从防护变成了内存 DoS。"""
    limiter = dc._RateLimiter()
    for index in range(dc.MAX_TRACKED_SOURCES * 2):
        limiter.allow(f"10.1.{index // 256}.{index % 256}", 100.0)
    assert limiter.tracked <= dc.MAX_TRACKED_SOURCES


# --- 真 socket，但只在回环上：不碰局域网，也不碰防火墙 ----------------------

@pytest.fixture()
def responder():
    made = dc.DiscoveryResponder("0.0.0.0", 0, candidates=lambda: candidates(),
                                 name="测试机", version=VERSION,
                                 instance="0123456789ab")
    assert made.start() is True
    yield made
    made.close()


def test_it_answers_a_real_packet(responder):
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2.0)
    try:
        client.sendto(query(nonce="beef"), ("127.0.0.1", responder.port))
        raw, _ = client.recvfrom(2048)
    finally:
        client.close()
    payload = reply_of(raw)
    assert payload["magic"] == dc.MAGIC_REPLY
    assert payload["nonce"] == "beef"
    assert payload["name"] == "测试机"


def test_closing_stops_the_thread(responder):
    """关不干净的后台线程会让程序退不出去，而那是用户会遇到的。"""
    responder.close(timeout=2.0)
    alive = [thread for thread in threading.enumerate()
             if thread.name == "motion-discovery"]
    assert alive == []


def test_a_failed_bind_is_survivable():
    """发现是增强不是必需。端口被占时记下原因继续跑，不能拖垮整个程序。"""
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("0.0.0.0", 0))
    taken = holder.getsockname()[1]
    try:
        blocked = dc.DiscoveryResponder("0.0.0.0", taken, candidates=list,
                                        name="PC", version=VERSION, instance="x")
        assert blocked.start() is False
        assert blocked.bound is False
        assert blocked.last_error
    finally:
        holder.close()


def test_candidates_are_not_re_enumerated_per_packet(responder):
    """每个包一次网卡枚举 = 一次系统调用，洪水下会变成 CPU 放大器。"""
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return candidates()

    made = dc.DiscoveryResponder("0.0.0.0", 0, candidates=counted, name="PC",
                                 version=VERSION, instance="x")
    assert made.start() is True
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2.0)
    try:
        for _ in range(4):
            client.sendto(query(), ("127.0.0.1", made.port))
            client.recvfrom(2048)
    finally:
        client.close()
        made.close()
    assert calls["n"] == 1, f"缓存没生效，枚举了 {calls['n']} 次"
