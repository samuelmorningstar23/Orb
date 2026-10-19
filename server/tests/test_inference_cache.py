"""Cache ownership, bounded memory and concurrency; no clinical behavior mocked in."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from services.inference_cache import InferenceCache


def test_capacity_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        InferenceCache(0)


def test_caller_mutation_never_changes_stored_predictions():
    cache = InferenceCache[dict[str, list[int]]]()
    first = cache.get_or_compute("a", lambda: {"risk": [1]})
    first["risk"].append(2)
    second = cache.get_or_compute("a", lambda: pytest.fail("duplicate compute"))
    assert second == {"risk": [1]}
    second["risk"].append(3)
    assert cache.get_or_compute("a", lambda: {}) == {"risk": [1]}


def test_lru_eviction_and_explicit_clear():
    cache = InferenceCache[int](2)
    assert cache.get_or_compute("a", lambda: 1) == 1
    cache.get_or_compute("b", lambda: 2)
    cache.get_or_compute("a", lambda: 9)
    cache.get_or_compute("c", lambda: 3)
    assert cache.get_or_compute("b", lambda: 4) == 4
    cache.clear()
    assert cache.get_or_compute("a", lambda: 5) == 5


def test_errors_are_not_cached_and_lock_is_released():
    cache = InferenceCache[int]()
    def fail():
        raise RuntimeError("synthetic failure")
    with pytest.raises(RuntimeError):
        cache.get_or_compute("a", fail)
    assert cache.get_or_compute("a", lambda: 7) == 7


def test_simultaneous_identical_requests_compute_once():
    cache = InferenceCache[int]()
    ready = Barrier(8)
    calls = []
    def request(_):
        ready.wait(timeout=3)
        return cache.get_or_compute("same", lambda: (calls.append(1), 42)[1])
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(request, range(8))) == [42] * 8
    assert calls == [1]
