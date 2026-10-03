"""Background LLM work must stay OFF the interactive tier (Lever B).

Orb's llm_queue is ONE global, non-preemptive slot, held for a job's whole
generation, so a clinician's Sage turn waits behind whatever background job is
in it. Measured on the appliance 2026-07-31:

    ward-brief narrative        10.9 s on medgemma:27b -> 2.2 s on medgemma:4b
    expected-course projection  30.1 s on medgemma:27b -> 4.3 s on medgemma:4b

The sprint benchmark caught a clinician sitting 31 s behind exactly one
background job; that is the wait these routing rules cut.

(The KV-prefix-eviction rationale was tested against this stack — Ollama 0.32.5,
llama-server -np 1 — and did NOT reproduce: an intervening background prompt on
the 27B left an exact-match prompt state restorable in ~80-230 ms, and Sage gets
no partial-prefix reuse to protect anyway. Queue time is the real win.)

These tests pin the routing rules:
  * ward narrator + expected-course projection generate on the AMBIENT tier;
  * they ask for the context the ambient runner is already loaded at (4096).
    Ollama keys a runner by model+num_ctx, so a mismatched value would reload
    the 4B on every job — trading a queue-time win for a reload cost;
  * when the ambient tier is not resident they SKIP — there is no spill lane
    back onto the interactive model;
  * Vigil's per-alert recommendation garnish is (still) ambient;
  * signed-note extraction deliberately stays on the PRIMARY tier, because it
    is the one background path that writes vitals/medications to the chart
    autonomously. That exception is asserted here so it cannot be "tidied up"
    into a downgrade by accident.
"""
import asyncio
import json

import pytest

BIG = "big:27b"      # stands in for the interactive tier (LLM_MODEL)
SMALL = "small:4b"   # stands in for the ambient tier (AMBIENT_LLM_MODEL)

# The context the ambient runner is loaded at (main.py warmup, Vigil's garnish,
# sage_ambient_service, and `ollama ps` all agree). Every ambient caller must ask
# for this exact value or Ollama spawns a second runner for the same model.
AMBIENT_CTX = 4096

_SNAPSHOT = {
    "census": 12,
    "risk_distribution": {"critical": 1, "elevated": 2, "moderate": 3, "stable": 6},
    "watchlist": [
        {"patient_id": "P001", "name": "Test Patient", "bed": "GM-102",
         "ward": "General Medicine", "level": "critical", "score": 88},
    ],
    "recent_events": [],
    "active_alerts": 1,
    "as_of": "2026-07-31T00:00:00+00:00",
}

_INPUTS = {
    "name": "Test Patient",
    "diagnosis": "Community-acquired pneumonia",
    "age": 64,
    "sex": "M",
    "comorbidities": ["Type 2 diabetes"],
}

_PROJECTION_JSON = json.dumps({
    "expected_los_days": 5,
    "phases": [
        {"name": "Stabilization", "focus": "Oxygen, IV antibiotics", "typical_days": "Day 0-1"},
        {"name": "Active Treatment", "focus": "Monitor response", "typical_days": "Day 1-3"},
        {"name": "Discharge Readiness", "focus": "Confirm stability", "typical_days": "~Day 5"},
    ],
    "discharge_criteria": ["Afebrile >24h"],
    "rationale": "Typical CAP course.",
})


class _Capture:
    """Collects every JSON body posted to Ollama so the `model` field can be asserted."""

    def __init__(self):
        self.payloads = []

    @property
    def models(self):
        return [p.get("model") for p in self.payloads]


def _fake_httpx(capture, response_json, status_code=200):
    """A stand-in for the `httpx` module that records posts instead of making them."""
    import httpx as _real

    class _Resp:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return response_json

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **kwargs):
            capture.payloads.append(json or {})
            return _Resp()

    class _Mod:
        AsyncClient = _Client
        Timeout = _real.Timeout
        ConnectError = _real.ConnectError
        ConnectTimeout = _real.ConnectTimeout

    return _Mod


def _residency(*names):
    async def _fake(timeout: float = 2.0):
        return frozenset(names)
    return _fake


def _route(monkeypatch, *resident):
    """Point the router at synthetic tiers with a known residency set."""
    from services import model_router
    monkeypatch.setattr(model_router, "LLM_MODEL", BIG)
    monkeypatch.setattr(model_router, "AMBIENT_LLM_MODEL", SMALL)
    monkeypatch.setattr(model_router, "MODEL_FALLBACK_ENABLED", True)
    monkeypatch.setattr(model_router, "probe_residency", _residency(*resident))
    model_router.invalidate_residency_cache()


def _free_queue(monkeypatch):
    """The single LLM slot is always immediately available in these tests."""
    from services.llm_queue_service import llm_queue

    async def _acquire(priority=0, timeout=120.0):
        return True

    monkeypatch.setattr(llm_queue, "acquire", _acquire)
    monkeypatch.setattr(llm_queue, "release", lambda: None)


# ── Ward narrator ────────────────────────────────────────────────────────────

def test_ward_narrator_generates_on_the_ambient_tier(monkeypatch):
    from services import sage_narrator_service as narrator_mod

    _route(monkeypatch, BIG, SMALL)
    monkeypatch.setattr(narrator_mod, "AMBIENT_LLM_MODEL", SMALL)
    _free_queue(monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(narrator_mod, "httpx", _fake_httpx(cap, {"response": "Ward is quiet."}))

    narrator = narrator_mod.WardNarrator()
    text = asyncio.run(narrator._narrate(_SNAPSHOT))

    assert text == "Ward is quiet."
    # The whole point of Lever B: the interactive tier is never touched.
    assert cap.models == [SMALL]
    assert BIG not in cap.models
    assert narrator._narrated_with == SMALL
    # Must not force a reload of the tier it was just moved onto.
    assert cap.payloads[0]["options"]["num_ctx"] == AMBIENT_CTX


def test_ward_narrator_skips_rather_than_spilling_onto_the_interactive_tier(monkeypatch):
    """Ambient tier evicted ⇒ skip. It must NOT fall back to the 27B."""
    from services import sage_narrator_service as narrator_mod

    _route(monkeypatch, BIG)  # only the interactive tier is resident
    monkeypatch.setattr(narrator_mod, "AMBIENT_LLM_MODEL", SMALL)
    _free_queue(monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(narrator_mod, "httpx", _fake_httpx(cap, {"response": "should not run"}))

    narrator = narrator_mod.WardNarrator()
    assert asyncio.run(narrator._narrate(_SNAPSHOT)) is None
    assert cap.payloads == []


def test_ward_narrator_reports_the_model_it_actually_used(monkeypatch):
    """narrative_engine must name the tier that served the text, not a constant."""
    from services import sage_narrator_service as narrator_mod

    _route(monkeypatch, BIG, SMALL)
    monkeypatch.setattr(narrator_mod, "AMBIENT_LLM_MODEL", SMALL)
    _free_queue(monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(narrator_mod, "httpx", _fake_httpx(cap, {"response": "Ward is quiet."}))

    narrator = narrator_mod.WardNarrator()
    narrator._build_ward_snapshot = lambda: dict(_SNAPSHOT)
    asyncio.run(narrator.refresh(reason="test"))

    brief = narrator.get_brief()
    assert brief["narrative"] == "Ward is quiet."
    assert brief["narrative_engine"] == f"local/{SMALL}"


def test_ward_narrator_deterministic_floor_survives_a_skipped_narrative(monkeypatch):
    """No narrative is not the same as no brief — the snapshot always stands."""
    from services import sage_narrator_service as narrator_mod

    _route(monkeypatch, BIG)  # ambient evicted → narration skipped
    monkeypatch.setattr(narrator_mod, "AMBIENT_LLM_MODEL", SMALL)
    _free_queue(monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(narrator_mod, "httpx", _fake_httpx(cap, {"response": "should not run"}))

    narrator = narrator_mod.WardNarrator()
    narrator._build_ward_snapshot = lambda: dict(_SNAPSHOT)
    asyncio.run(narrator.refresh(reason="test"))

    brief = narrator.get_brief()
    assert brief["snapshot"]["census"] == 12
    assert brief["narrative"] is None
    assert cap.payloads == []


# ── Expected-course (timeline) projection ────────────────────────────────────

def test_timeline_projection_generates_on_the_ambient_tier(monkeypatch):
    from services import timeline_service

    _route(monkeypatch, BIG, SMALL)
    monkeypatch.setattr(timeline_service, "AMBIENT_LLM_MODEL", SMALL)
    _free_queue(monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(
        timeline_service, "httpx",
        _fake_httpx(cap, {"message": {"content": _PROJECTION_JSON}}),
    )

    projection = asyncio.run(timeline_service._call_llm(_INPUTS))

    assert cap.models == [SMALL]
    assert BIG not in cap.models
    assert projection["expected_los_days"] == 5
    assert len(projection["phases"]) == 3
    # Provenance reports the tier that actually ran.
    assert projection["engine"] == SMALL
    # Must not force a reload of the tier it was just moved onto.
    assert cap.payloads[0]["options"]["num_ctx"] == AMBIENT_CTX


def test_timeline_projection_skips_rather_than_spilling_onto_the_interactive_tier(monkeypatch):
    from services import timeline_service

    _route(monkeypatch, BIG)  # ambient evicted
    monkeypatch.setattr(timeline_service, "AMBIENT_LLM_MODEL", SMALL)
    _free_queue(monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(
        timeline_service, "httpx",
        _fake_httpx(cap, {"message": {"content": _PROJECTION_JSON}}),
    )

    with pytest.raises(RuntimeError):
        asyncio.run(timeline_service._call_llm(_INPUTS))
    assert cap.payloads == []


def test_timeline_heuristic_projection_carries_no_false_engine():
    """The deterministic fallback must not claim a model generated it."""
    from services import timeline_service

    projection = timeline_service._heuristic_projection(_INPUTS)
    assert "engine" not in projection
    assert len(projection["phases"]) >= 3


# ── Vigil: the garnish that was already ambient (regression guard) ───────────

def test_vigil_recommendation_garnish_uses_the_ambient_tier(monkeypatch):
    import config
    import httpx as real_httpx
    from services import vigil_service

    _route(monkeypatch, BIG, SMALL)
    monkeypatch.setattr(config, "AMBIENT_LLM_MODEL", SMALL)
    _free_queue(monkeypatch)
    monkeypatch.setitem(vigil_service._LLM_BREAKER, "open_until", 0.0)
    cap = _Capture()
    fake = _fake_httpx(cap, {"message": {"content": "Recheck vitals in 15 minutes."}})
    monkeypatch.setattr(real_httpx, "AsyncClient", fake.AsyncClient)

    text = asyncio.run(
        vigil_service.generate_ai_recommendation("HR 132, BP 82/49", "critical", ["tachycardia"])
    )

    assert text == "Recheck vitals in 15 minutes."
    assert cap.models == [SMALL]
    assert BIG not in cap.models


# ── Signed-note extraction: the deliberate PRIMARY-tier exception ────────────

def test_nervous_system_note_extraction_stays_on_the_primary_tier(monkeypatch):
    """This path charts vitals/medications autonomously — accuracy over latency.

    Extracted vitals are INSERTed with no human gate and feed Vigil's NEWS2
    scoring, so downgrading the extractor is a clinical-safety trade, not a
    prose-quality one. If someone routes this to the ambient tier, this fails.
    """
    from services import nervous_system_triggers as nst

    _route(monkeypatch, BIG, SMALL)
    _free_queue(monkeypatch)
    cap = _Capture()
    monkeypatch.setattr(nst, "httpx", _fake_httpx(cap, {"message": {"content": "{}"}}))

    updates = asyncio.run(nst._extract_updates(1, "P001", "OBJECTIVE: HR 88, BP 120/80"))

    assert cap.models == [BIG]
    assert SMALL not in cap.models
    assert updates == {}
