"""登录失败限流（设计文档 8 / 11.1 / A4）。

单 IP 在 ``login_window_sec`` 内失败次数达到 ``login_max_attempts`` 后，
后续登录请求直接返回 429（错误码 E1005），直到窗口内失败记录过期或
管理员手工重置。成功登录会清空该 IP 的失败记录。
"""

from __future__ import annotations

import threading
import time
from collections import deque


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_sec: int) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须 ≥ 1")
        if window_sec < 1:
            raise ValueError("window_sec 必须 ≥ 1")
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._failures.get(key)
        if bucket is None:
            bucket = deque()
            self._failures[key] = bucket
        while bucket and now - bucket[0] > self.window_sec:
            bucket.popleft()
        return bucket

    def is_blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._prune(key, now)
            return len(bucket) >= self.max_attempts

    def retry_after(self, key: str) -> int:
        """返回还需等待的秒数，用于 ``Retry-After`` 响应头。"""

        now = time.monotonic()
        with self._lock:
            bucket = self._prune(key, now)
            if len(bucket) < self.max_attempts:
                return 0
            return max(1, int(self.window_sec - (now - bucket[0])))

    def record_failure(self, key: str) -> int:
        """记录一次失败，返回窗口内累计失败次数。"""

        now = time.monotonic()
        with self._lock:
            bucket = self._prune(key, now)
            bucket.append(now)
            return len(bucket)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset_all(self) -> None:
        with self._lock:
            self._failures.clear()

    def failures(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            return len(self._prune(key, now))
