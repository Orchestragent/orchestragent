"""Pacing for wide runs: a global token bucket for builder calls.

The AIMD limit decides how many tasks may generate at once; the bucket decides how
fast their calls may start, so a hundred ready tasks never burst past a provider's
request rate. Clock and sleep are injected so tests can run it without waiting.
"""

import threading
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self, per_minute: float, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.rate = per_minute / 60
        self.capacity = max(1.0, per_minute / 6)  # at most ten seconds' worth at once
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.stamp = clock()
        self.lock = threading.Lock()
        self.waited = 0.0  # seconds callers spent waiting, for the run metrics

    def take(self, cancelled: Callable[[], bool]) -> bool:
        """Wait for one token; False when `cancelled` turns true while waiting."""
        while True:
            if cancelled():
                return False
            with self.lock:
                now = self.clock()
                self.tokens = min(self.capacity,
                                  self.tokens + max(0.0, now - self.stamp) * self.rate)
                self.stamp = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
                pause = min(1.0, (1 - self.tokens) / self.rate)
                self.waited += pause
            if cancelled():
                return False
            self.sleep(pause)
