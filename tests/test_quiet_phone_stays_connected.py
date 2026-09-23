"""画面里没人，不等于没连上电脑。

手机只在画面里有人时才发姿态帧；人一离开，它就一帧都不发了。以前电脑这边会在那之后
一秒钟主动断开连接，于是手机显示「未连接电脑」、1.5 秒后重连、还是没人、又断——在
用户眼里就是"人一离开画面就断线"，而这两件事本来毫无关系。

根子在 Python 的 socket.SocketIO（handler.rfile 底下那层）：**超时过一次，以后每次
读都直接报 "cannot read from timed out object"**。这个连接设了 1 秒超时是为了保护
发送，读那边却一超时就把自己弄坏了。

这里的测试用的是真 socket，不是替身——要验的恰恰是 socket 本身的脾气。
"""

from __future__ import annotations

import socket
import threading
import time
from types import SimpleNamespace

import pytest

from motioncontrol.input_bridge import WebSocketPeer


def masked_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """客户端发来的帧必须带掩码（RFC 6455），和手机上的浏览器发的一模一样。"""
    assert len(payload) < 126
    mask = b"\x11\x22\x33\x44"
    body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + body


@pytest.fixture()
def pair():
    server, client = socket.socketpair()
    server.settimeout(0.2)
    handler = SimpleNamespace(connection=server, rfile=server.makefile("rb"),
                              wfile=server.makefile("wb"))
    peer = WebSocketPeer(handler)
    yield peer, client
    for sock in (server, client):
        try:
            sock.close()
        except OSError:
            pass


def test_a_quiet_moment_is_only_a_timeout(pair):
    """两帧之间对方没说话：抛 socket.timeout，外面的循环接着等，不算出错。"""
    peer, _ = pair
    with pytest.raises(socket.timeout):
        peer.recv()


def test_the_phone_can_still_talk_after_being_quiet(pair):
    """这就是那个 bug 本身：安静过一次之后，下一帧必须还读得到。

    以前这里拿到的是 OSError("cannot read from timed out object")，电脑于是断开连接。
    """
    peer, client = pair
    for _ in range(3):  # 安静好几秒——人离开画面就是这样
        with pytest.raises(socket.timeout):
            peer.recv()
    client.sendall(masked_frame(b'{"type":"hello"}'))
    opcode, payload = peer.recv()
    assert opcode == 0x1
    assert payload == b'{"type":"hello"}'


def test_a_frame_split_across_a_pause_is_not_lost(pair):
    """一帧读到一半对方停了一下：已经收到的那半个字节一个都不能丢。"""
    peer, client = pair
    frame = masked_frame(b'{"type":"pose"}')

    def slow_sender():
        client.sendall(frame[:3])
        time.sleep(0.5)  # 比超时长
        client.sendall(frame[3:])

    threading.Thread(target=slow_sender, daemon=True).start()
    opcode, payload = peer.recv()
    assert payload == b'{"type":"pose"}'


def test_two_frames_in_one_packet_are_both_read(pair):
    """30 帧每秒时，一次 recv 常常拿到不止一帧。第二帧要留在缓冲里，不能被丢掉。"""
    peer, client = pair
    client.sendall(masked_frame(b'{"n":1}') + masked_frame(b'{"n":2}'))
    assert peer.recv()[1] == b'{"n":1}'
    assert peer.recv()[1] == b'{"n":2}'


def test_a_closed_phone_is_still_noticed(pair):
    """修超时不能把"对方真走了"也吞掉。"""
    peer, client = pair
    client.close()
    with pytest.raises(ConnectionError):
        peer.recv()


def test_a_frame_stuck_forever_is_eventually_given_up(pair, monkeypatch):
    """一帧卡在半路永远不来，不能让这条线程永远挂着。"""
    peer, client = pair
    monkeypatch.setattr(WebSocketPeer, "MID_FRAME_PATIENCE_S", 0.6)
    client.sendall(masked_frame(b'{"x":1}')[:3])
    with pytest.raises(ConnectionError):
        peer.recv()


def test_python_really_does_poison_the_file_object():
    """钉住根因本身。哪天 Python 改了这个脾气，这条会红——那时候可以回头看看
    _read 那套绕法还需不需要。"""
    server, client = socket.socketpair()
    try:
        server.settimeout(0.1)
        stream = server.makefile("rb")
        with pytest.raises(socket.timeout):
            stream.read(2)
        client.sendall(b"hi")
        with pytest.raises(OSError, match="timed out"):
            stream.read(2)
    finally:
        server.close()
        client.close()
