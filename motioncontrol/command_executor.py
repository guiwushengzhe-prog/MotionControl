"""Bounded, ordered system commands that can be invalidated by stop/switch."""
from __future__ import annotations

from collections import deque
import copy
import threading


class CommandExecutor:
    def __init__(self, execute, *, capacity: int = 32) -> None:
        self._execute = execute
        self._capacity = capacity
        self._condition = threading.Condition()
        self._pending = deque()
        self._generation = 0
        self._closed = False
        self.last_error = ""
        self._thread = threading.Thread(target=self._run, name="motion-system-commands", daemon=True)
        self._thread.start()

    def submit(self, action: dict) -> dict:
        with self._condition:
            if self._closed or len(self._pending) >= self._capacity:
                return {"executed": False, "queued": False, "reason": "系统操作正在处理，请稍后重试"}
            item = copy.deepcopy(action)
            item["_command_generation"] = self._generation
            self._pending.append(item)
            self._condition.notify()
        return {"executed": False, "queued": True}

    def invalidate(self) -> None:
        with self._condition:
            self._generation += 1
            self._pending.clear()

    def is_current(self, generation) -> bool:
        with self._condition:
            return not self._closed and generation == self._generation

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._closed)
                if self._closed:
                    return
                action = self._pending.popleft()
            if not self.is_current(action["_command_generation"]):
                continue
            try:
                self._execute(action)
            except Exception as exc:
                self.last_error = str(exc)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._generation += 1
            self._pending.clear()
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
