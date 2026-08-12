"""Small per-session abuse guard for a single service process."""

import hashlib
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, requests_per_minute=10, concurrent=2):
        self.requests_per_minute = requests_per_minute
        self.concurrent = concurrent
        self._requests = defaultdict(deque)
        self._active = defaultdict(int)

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode()).digest()

    def enter(self, token, now=None):
        now = time.monotonic() if now is None else now
        key = self._key(token)
        history = self._requests[key]
        while history and history[0] <= now - 60:
            history.popleft()
        if len(history) >= self.requests_per_minute or self._active[key] >= self.concurrent:
            return False
        history.append(now)
        self._active[key] += 1
        return True

    def exit(self, token):
        key = self._key(token)
        self._active[key] = max(0, self._active[key] - 1)
