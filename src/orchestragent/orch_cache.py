"""Small, process-local cache hints; observed freshness is not a provider TTL.

Affinity only breaks scheduling ties. Delays require comparable measured benefit;
unknown telemetry never serializes work. No price or cache-lifetime assumptions.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import cast

from orchestragent.orch_usage import count, tokens
from orchestragent.serialization import normalize

Key = tuple[str, str, str]
FRESHNESS_SECONDS = 300.0
MAX_WAIT_SECONDS = 0.25
MAX_SAMPLES = 128
MIN_SAMPLES = 3


@dataclass
class _Flight:
    key: Key
    started: float | None
    cold: bool


@dataclass
class _Observation:
    key: Key
    finished: float
    duration: float
    incoming: int
    outgoing: int
    read: int


@dataclass
class _Snapshot:
    in_flight: int
    samples: int
    warm_keys: int
    waits_recommended: int
    freshness_seconds: float = FRESHNESS_SECONDS
    max_wait_seconds: float = MAX_WAIT_SECONDS


class CachePolicy:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}
        self._samples: deque[_Observation] = deque(maxlen=MAX_SAMPLES)
        self._waits = 0

    def _now(self) -> float | None:
        now = self._clock()
        return now if math.isfinite(now) and now >= 0 else None

    def _recent(self, key: Key, now: float) -> list[_Observation]:
        return sorted((sample for sample in self._samples
                       if sample.key == key and 0 <= now - sample.finished < FRESHNESS_SECONDS),
                      key=lambda sample: sample.finished)

    def _affinity(self, key: Key, now: float) -> float:
        samples = self._recent(key, now)
        if not samples:
            return 0.0
        latest = samples[-1]
        return latest.read / latest.incoming * (1 - (now - latest.finished) / FRESHNESS_SECONDS)

    def start(self, call_id: str, key: Key) -> None:
        with self._lock:
            now = self._now()
            self._flights.setdefault(
                call_id, _Flight(key, now, now is None or self._affinity(key, now) == 0),
            )

    def finish(self, call_id: str, usage: dict[str, object], outcome: str = "ok") -> None:
        with self._lock:
            flight = self._flights.pop(call_id, None)
            now = self._now()
        if flight is not None and flight.started is not None and now is not None:
            self.observe(flight.key, flight.started, now, usage, outcome)

    def observe(self, key: Key, started_at: float, finished_at: float,
                usage: dict[str, object], outcome: str = "ok") -> None:
        """Replay measured receipts without resetting their age or changing in-flight calls."""
        with self._lock:
            now = self._now()
            if (now is None or not math.isfinite(started_at) or not math.isfinite(finished_at)
                    or started_at < 0 or finished_at <= started_at or outcome != "ok"
                    or not 0 <= now - finished_at < FRESHNESS_SECONDS):
                return
            if any(name in usage and usage[name] != key[1]
                   for name in ("served_model", "model")):
                return
            counters = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens")
            if any(name in usage and count(usage, name) is None for name in counters):
                return
            if ("cached_input_tokens" in usage and "cache_read_input_tokens" in usage
                    and usage["cached_input_tokens"] != usage["cache_read_input_tokens"]):
                return
            measured = tokens(key[0], usage)
            incoming, outgoing, read = (measured.input_tokens, measured.output_tokens,
                                        measured.cache_read_tokens)
            if (measured.issues or incoming is None or incoming <= 0
                    or outgoing is None or read is None):
                return
            self._samples.append(_Observation(
                key, finished_at, finished_at - started_at, incoming, outgoing, read,
            ))

    def affinity(self, key: Key) -> float:
        with self._lock:
            now = self._now()
            return self._affinity(key, now) if now is not None else 0.0

    def delay(self, key: Key) -> float:
        """Wait only near measured cold completion with conservative latency benefit.

        Exact token-count matching deliberately refuses mixed-size histories: the
        API has no size prediction for the pending call. These are hints, never a
        promise that a request has populated a provider cache.
        """
        with self._lock:
            now = self._now()
            if now is None or self._affinity(key, now) > 0:
                return 0.0
            samples = self._recent(key, now)
            if len({(sample.incoming, sample.outgoing) for sample in samples}) != 1:
                return 0.0
            cold = [sample.duration for sample in samples if sample.read == 0]
            hit = [sample.duration for sample in samples if sample.read > 0]
            if len(cold) < MIN_SAMPLES or len(hit) < MIN_SAMPLES:
                return 0.0
            benefit = min(cold) - max(hit)
            waits = [max(cold) - (now - flight.started) for flight in self._flights.values()
                     if flight.key == key and flight.cold and flight.started is not None
                     and now >= flight.started]
            valid = [wait for wait in waits if 0 < wait <= MAX_WAIT_SECONDS and benefit > wait]
            if not valid:
                return 0.0
            self._waits += 1
            return min(valid)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = self._now()
            samples = [sample for sample in self._samples if now is not None
                       and 0 <= now - sample.finished < FRESHNESS_SECONDS]
            keys = {sample.key for sample in samples}
            state = _Snapshot(
                len(self._flights), len(samples),
                sum(self._affinity(key, now) > 0 for key in keys) if now is not None else 0,
                self._waits,
            )
            return cast(dict[str, object], normalize(asdict(state)))
