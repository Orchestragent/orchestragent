from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestragent.orch_cache import FRESHNESS_SECONDS, MAX_SAMPLES, CachePolicy
from tests.conftest import Clock

KEY = ("codex", "model", "prefix")
USAGE: dict[str, object] = {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 50}


@pytest.fixture
def cache(clock: Clock) -> CachePolicy:
    return CachePolicy(clock)


@pytest.fixture
def record(cache: CachePolicy, clock: Clock) -> Callable[..., None]:
    def observe(duration: float = 2, *, key: tuple[str, str, str] = KEY,
                usage: dict[str, object] | None = None, outcome: str = "ok") -> None:
        cache.start("call", key)
        clock.now += duration
        cache.finish("call", USAGE if usage is None else usage, outcome)
    return observe


@pytest.fixture
def history(record: Callable[..., None]) -> None:
    for read, duration in [(0, 5)] * 3 + [(50, 2)] * 3 + [(0, 5)]:
        record(duration, usage={**USAGE, "cached_input_tokens": read})


@pytest.mark.parametrize("usage", [{}, {"input_tokens": 100}, {"output_tokens": 20}])
def test_absent_telemetry_never_waits(
    cache: CachePolicy, record: Callable[..., None], usage: dict[str, object],
) -> None:
    record(usage=usage)
    cache.start("pending", KEY)
    assert (cache.affinity(KEY), cache.delay(KEY), cache.snapshot()["samples"]) == (0, 0, 0)


@pytest.mark.parametrize("change", [
    {"cached_input_tokens": -1}, {"cached_input_tokens": True},
    {"cached_input_tokens": 50.0}, {"cached_input_tokens": float("nan")},
    {"cached_input_tokens": 101}, {"input_tokens": 0}, {"input_tokens": -1},
    {"total_tokens": 999}, {"cache_read_input_tokens": 30},
    {"cache_creation_input_tokens": -1},
    {"served_model": "different-model"},
    {"served_model": "model", "model": "different-model"},
])
def test_invalid_counters_do_not_establish_warmth(
    cache: CachePolicy, record: Callable[..., None], change: dict[str, object],
) -> None:
    record(usage={**USAGE, **change})
    assert (cache.affinity(KEY), cache.snapshot()["samples"]) == (0, 0)


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "exception:TimeoutError"])
def test_failed_calls_are_not_evidence(
    cache: CachePolicy, record: Callable[..., None], outcome: str,
) -> None:
    record(outcome=outcome)
    assert (cache.affinity(KEY), cache.snapshot()["in_flight"]) == (0, 0)


def test_real_hit_and_freshness(
    cache: CachePolicy, record: Callable[..., None], clock: Clock,
) -> None:
    record()
    assert cache.affinity(KEY) == 0.5
    clock.now += FRESHNESS_SECONDS
    cache.start("pending", KEY)
    assert (cache.affinity(KEY), cache.delay(KEY), cache.snapshot()["samples"]) == (0, 0, 0)


@pytest.mark.parametrize("key", [
    ("codex", "model", "other-prefix"), ("codex", "other-model", "prefix"),
    ("jev", "model", "prefix"),
])
def test_keys_are_independent(
    cache: CachePolicy, record: Callable[..., None], key: tuple[str, str, str],
) -> None:
    record()
    cache.start("pending", KEY)
    assert (cache.affinity(key), cache.delay(key)) == (0, 0)


@pytest.mark.usefixtures("history")
@pytest.mark.parametrize("elapsed,expected", [(0, 0), (4.7, 0), (4.8, 0.2), (5, 0), (6, 0)])
def test_measured_benefit_wait_is_bounded(
    cache: CachePolicy, clock: Clock, elapsed: float, expected: float,
) -> None:
    cache.start("pending", KEY)
    clock.now += elapsed
    assert cache.delay(KEY) == pytest.approx(expected)


@pytest.mark.usefixtures("history")
@pytest.mark.parametrize("change", [{"input_tokens": 101}, {"output_tokens": 21}])
def test_mixed_sizes_refuse_wait(
    cache: CachePolicy, record: Callable[..., None], clock: Clock, change: dict[str, object],
) -> None:
    record(5, usage={**USAGE, "cached_input_tokens": 0, **change})
    cache.start("pending", KEY)
    clock.now += 4.9
    assert cache.delay(KEY) == 0


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_invalid_duration(cache: CachePolicy, record: Callable[..., None], duration: float) -> None:
    record(duration)
    assert (cache.affinity(KEY), cache.snapshot()["in_flight"]) == (0, 0)


def test_zero_start_time_is_valid(
    cache: CachePolicy, record: Callable[..., None], clock: Clock,
) -> None:
    clock.now = 0
    record()
    assert cache.affinity(KEY) == 0.5


def test_bounded_history(cache: CachePolicy, record: Callable[..., None]) -> None:
    for _ in range(MAX_SAMPLES + 10):
        record(0.01)
    assert cache.snapshot()["samples"] == MAX_SAMPLES


def test_concurrent_accounting(cache: CachePolicy, clock: Clock) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: cache.start(str(index), KEY), range(40)))
        assert cache.snapshot()["in_flight"] == 40
        clock.now += 2
        list(pool.map(lambda index: cache.finish(str(index), USAGE), range(40)))
    assert cache.snapshot()["in_flight"] == 0
    assert cache.snapshot()["samples"] == 40


def test_duplicate_and_unknown_call_ids(cache: CachePolicy, clock: Clock) -> None:
    cache.start("same", KEY)
    clock.now += 1
    cache.start("same", ("codex", "model", "other"))
    cache.finish("unknown", USAGE)
    cache.finish("same", USAGE)
    cache.finish("same", USAGE)
    assert (cache.affinity(KEY), cache.snapshot()["samples"],
            cache.snapshot()["in_flight"]) == (0.5, 1, 0)


@pytest.mark.parametrize("age,expected", [(0, 0.5), (150, 0.25), (300, 0), (-10, 0)])
def test_replay_preserves_age(
    cache: CachePolicy, clock: Clock, age: float, expected: float,
) -> None:
    cache.start("pending", KEY)
    cache.observe(KEY, clock.now - age - 2, clock.now - age, USAGE)
    assert (cache.affinity(KEY), cache.snapshot()["in_flight"]) == (expected, 1)


def test_replay_completion_order(cache: CachePolicy, clock: Clock) -> None:
    cache.observe(KEY, clock.now - 5, clock.now, {**USAGE, "cached_input_tokens": 0})
    cache.observe(KEY, clock.now - 10, clock.now - 8, USAGE)
    assert cache.affinity(KEY) == 0


@pytest.mark.parametrize("started,finished", [
    (-1, 1), (0, 0), (10, 1), (float("nan"), 1), (0, float("inf")),
])
def test_replay_invalid_times(cache: CachePolicy, started: float, finished: float) -> None:
    cache.observe(KEY, started, finished, USAGE)
    assert cache.snapshot()["samples"] == 0


@pytest.mark.parametrize("cold_duration,hit_duration,samples", [(2, 5, 3), (5, 2, 2)])
def test_no_benefit_or_insufficient_samples(
    cache: CachePolicy, clock: Clock, record: Callable[..., None],
    cold_duration: float, hit_duration: float, samples: int,
) -> None:
    for _ in range(samples):
        record(hit_duration)
        record(cold_duration, usage={**USAGE, "cached_input_tokens": 0})
    cache.start("pending", KEY)
    clock.now += cold_duration - 0.1
    assert cache.delay(KEY) == 0


def test_messages_token_convention(cache: CachePolicy, record: Callable[..., None]) -> None:
    key = ("dsh", "model", "prefix")
    record(key=key, usage={"input_tokens": 50, "cache_read_input_tokens": 50,
                           "cache_creation_input_tokens": 0, "output_tokens": 20})
    assert cache.affinity(key) == 0.5
