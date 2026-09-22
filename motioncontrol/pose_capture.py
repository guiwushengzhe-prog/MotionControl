"""录姿势的倒计时。

## 为什么在这里，不在网页里

「录下当前姿势」这个按钮天生就该能用嘴按：人站在镜头前几米外摆姿势，够不着鼠标。
而语音是电脑这边处理的——倒计时留在网页里的话，口令触发的那一次就得让服务端反过来
指挥页面，于是同一件事有两套倒计时，迟早对不上。

挪过来之后按钮和口令走的是同一条路：都只是"把倒计时设上"，到点了这里自己拍。页面
只负责把剩几秒显示出来。

## 为什么是单独一个文件

真正拍照那一步要碰内核、碰姿势库、碰配置锁，全是 server.py 里的东西。但**倒计时
本身**——设上、取消、到点、被新的一次顶掉——和那些无关，所以拍照用回调传进来。

这不是为了好看：写在 server.py 里的时候，它一行都测不到，于是我漏掉一个 import
都没人发现，直到真去点那个按钮。
"""

from __future__ import annotations

import threading
import time

# 默认准备时间。人要从电脑前走到镜头前摆好，五秒是实测下来不慌不忙的长度。
DEFAULT_POSE_DELAY_S = 5
MIN_POSE_DELAY_S, MAX_POSE_DELAY_S = 1, 30


class PoseCaptureTimer:
    """倒计时状态机。``capture`` 是到点时调用的那个回调。"""

    def __init__(self, capture, *, clock=time.monotonic) -> None:
        self._capture = capture
        self._clock = clock
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.purpose = ""
        self.pose_id = ""
        self.name = ""
        self.ends_at = 0.0
        self.message = ""
        self.last_pose: dict | None = None

    def status(self) -> dict:
        with self._lock:
            remaining = max(0.0, self.ends_at - self._clock()) if self.ends_at else 0.0
            return {
                "counting": bool(self.ends_at and remaining > 0.0),
                "remaining_s": round(remaining, 1),
                "purpose": self.purpose,
                "pose_id": self.pose_id,
                "name": self.name,
                "message": self.message,
            }

    def arm(self, *, purpose: str, delay_s: float, name: str = "", pose_id: str = "") -> dict:
        purpose = str(purpose or "capture").strip().lower()
        if purpose not in {"capture", "frame"}:
            raise ValueError("purpose must be capture or frame")
        if purpose == "frame" and not pose_id:
            raise ValueError("还没有可以加姿势的动作，先录一个")
        try:
            delay = float(delay_s)
        except (TypeError, ValueError):
            delay = DEFAULT_POSE_DELAY_S
        delay = max(MIN_POSE_DELAY_S, min(MAX_POSE_DELAY_S, delay))
        with self._lock:
            # 已经在倒计时就先停掉旧的那条。两条线程先后到点会拍两张，而人只摆了
            # 一个姿势——多出来的那一条要等他自己发现再删。
            self._cancel.set()
            self._cancel = threading.Event()
            cancel = self._cancel
            self.purpose = purpose
            self.pose_id = str(pose_id or "")
            self.name = str(name or "")
            self.ends_at = self._clock() + delay
            self.message = ""
            self._thread = threading.Thread(
                target=self._run, args=(cancel, delay), name="pose-capture-timer", daemon=True)
            self._thread.start()
        return self.status()

    def cancel(self, message: str = "已取消") -> dict:
        with self._lock:
            counting = bool(self.ends_at and self.ends_at > self._clock())
            self._cancel.set()
            self.ends_at = 0.0
            if counting:
                self.message = message
        return self.status()

    def wait(self, timeout: float = 5.0) -> None:
        """等这一轮跑完。只有测试用得上；正常流程谁也不等它。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self, cancel: threading.Event, delay: float) -> None:
        if cancel.wait(delay):
            return
        with self._lock:
            # 被新的一次顶掉了：那一轮有自己的线程，这一条直接退。
            if cancel is not self._cancel:
                return
            purpose, pose_id, name = self.purpose, self.pose_id, self.name
            self.ends_at = 0.0
        try:
            entry = self._capture(purpose, pose_id, name)
        except Exception as exc:  # noqa: BLE001 - 定时线程里抛出去没人接得住
            with self._lock:
                self.message = str(exc)
            return
        with self._lock:
            self.last_pose = entry
            self.message = f"已录「{entry.get('name', '')}」" if entry else "没录成"
