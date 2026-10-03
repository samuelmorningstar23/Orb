"""Residency-aware model fallback (services/model_router).

The operator can evict the 27B from the header AI panel to reclaim ~17 GB.
Without a router, the next Sage turn asks Ollama for the 27B anyway and pays a
~80 s transparent reload — silently undoing the reclaim. These tests pin the
routing rules that replace that behaviour.
"""
import asyncio

import pytest

from services import model_router


@pytest.fixture(autouse=True)
def _clear_cache():
    model_router.invalidate_residency_cache()
    yield
    model_router.invalidate_residency_cache()


def _resident(*names):
    """Patch the residency probe to report exactly `names`."""
    async def _fake(timeout: float = 2.0):
        return frozenset(names)
    return _fake


def test_uses_preferred_when_resident(monkeypatch):
    monkeypatch.setattr(model_router, "resident_models", _resident("big:27b", "small:4b"))
    monkeypatch.setattr(model_router, "MODEL_FALLBACK_ENABLED", True)
    choice = asyncio.run(model_router.select_chat_model("big:27b", "small:4b"))
    assert choice.model == "big:27b"
    assert choice.downgraded is False


def test_falls_back_when_preferred_evicted(monkeypatch):
    monkeypatch.setattr(model_router, "resident_models", _resident("small:4b"))
    monkeypatch.setattr(model_router, "MODEL_FALLBACK_ENABLED", True)
    choice = asyncio.run(model_router.select_chat_model("big:27b", "small:4b"))
    assert choice.model == "small:4b"
    assert choice.downgraded is True
    assert choice.preferred == "big:27b"


def test_falls_back_even_when_fallback_also_cold(monkeypatch):
    """A cold 4B loads in seconds; a cold 27B costs ~80s. Downgrading is still
    the better answer for the clinician who is waiting."""
    monkeypatch.setattr(model_router, "resident_models", _resident("some-other:1b"))
    monkeypatch.setattr(model_router, "MODEL_FALLBACK_ENABLED", True)
    choice = asyncio.run(model_router.select_chat_model("big:27b", "small:4b"))
    assert choice.model == "small:4b"
    assert choice.downgraded is True


def test_unknown_residency_never_downgrades(monkeypatch):
    """An unreachable Ollama must not silently demote a clinician's answer."""
    monkeypatch.setattr(model_router, "resident_models", _resident())  # empty
    monkeypatch.setattr(model_router, "MODEL_FALLBACK_ENABLED", True)
    choice = asyncio.run(model_router.select_chat_model("big:27b", "small:4b"))
    assert choice.model == "big:27b"
    assert choice.downgraded is False


def test_disabled_flag_pins_preferred(monkeypatch):
    monkeypatch.setattr(model_router, "resident_models", _resident("small:4b"))
    monkeypatch.setattr(model_router, "MODEL_FALLBACK_ENABLED", False)
    choice = asyncio.run(model_router.select_chat_model("big:27b", "small:4b"))
    assert choice.model == "big:27b"
    assert choice.downgraded is False


def test_identical_tiers_never_downgrade(monkeypatch):
    """A single-model deployment (ambient == primary) has nothing to fall back to."""
    monkeypatch.setattr(model_router, "resident_models", _resident())
    monkeypatch.setattr(model_router, "MODEL_FALLBACK_ENABLED", True)
    choice = asyncio.run(model_router.select_chat_model("solo:27b", "solo:27b"))
    assert choice.model == "solo:27b"
    assert choice.downgraded is False


def test_residency_probe_survives_unreachable_engine(monkeypatch):
    """resident_models swallows transport errors and reports 'unknown' (empty)."""
    class _Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise OSError("connection refused")
    monkeypatch.setattr(model_router.httpx, "AsyncClient", _Boom)
    assert asyncio.run(model_router.resident_models()) == frozenset()


# ── ambient_tier_evicted — the guard that keeps background AI features from
#    resurrecting an operator-evicted tier (the unload button "not working") ──

def _probe(result):
    async def _fake(timeout: float = 2.0):
        return result
    return _fake


def test_ambient_guard_skips_when_tier_known_absent(monkeypatch):
    """Engine reachable, tier not resident → evicted: ambient work must skip."""
    monkeypatch.setattr(model_router, "probe_residency", _probe(frozenset({"big:27b"})))
    assert asyncio.run(model_router.ambient_tier_evicted("small:4b")) is True


def test_ambient_guard_runs_when_tier_resident(monkeypatch):
    monkeypatch.setattr(model_router, "probe_residency", _probe(frozenset({"small:4b"})))
    assert asyncio.run(model_router.ambient_tier_evicted("small:4b")) is False


def test_ambient_guard_never_skips_on_unknown_residency(monkeypatch):
    """Unreachable engine (probe None) must not drop optional work on a guess."""
    monkeypatch.setattr(model_router, "probe_residency", _probe(None))
    assert asyncio.run(model_router.ambient_tier_evicted("small:4b")) is False


def test_ambient_guard_skips_when_nothing_loaded(monkeypatch):
    """Reachable engine with an empty ps (everything evicted) is a KNOWN state:
    background work stays paused instead of reloading a tier."""
    monkeypatch.setattr(model_router, "probe_residency", _probe(frozenset()))
    assert asyncio.run(model_router.ambient_tier_evicted("small:4b")) is True


def test_probe_distinguishes_unreachable_from_empty(monkeypatch):
    """probe_residency: None = unreachable; frozenset() = reachable-and-empty.
    resident_models collapses both to empty for its callers."""
    class _Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise OSError("connection refused")
    monkeypatch.setattr(model_router.httpx, "AsyncClient", _Boom)
    assert asyncio.run(model_router.probe_residency()) is None
    assert asyncio.run(model_router.resident_models()) == frozenset()
