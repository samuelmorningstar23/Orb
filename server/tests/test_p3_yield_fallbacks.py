"""Long P3 background jobs give the GPU back to a clinician's turn (F036).

The ward narrator, the history summarizer, the expected-course projection and
Vigil's recommendation garnish held the background lane through a clinician's
turn, sharing the one GPU with it; speed is Sage's sharpest trust gap. Each now
posts with ollama_gateway.post(yield_to_clinician=True) and, when a turn is in
flight, takes its existing deterministic fallback at once. The Vigil garnish is
covered in tests/test_inference_audit_lanes.py; the mechanism itself in
tests/test_ollama_gateway.py.
"""
import asyncio
import json
import uuid

import pytest

from database import db_session
from services.llm_queue_service import PriorityLLMQueue, llm_queue


class _NeverAnswers:
    """An httpx.AsyncClient whose model would take 30 s."""
    posted = 0

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _NeverAnswers.posted += 1
        await asyncio.sleep(30)


@pytest.fixture()
def clinician_turn_in_flight(monkeypatch):
    import httpx
    from services import model_router
    from services.model_router import ModelChoice

    async def _select(preferred=None, fallback=None):
        return ModelChoice(preferred or "m", False, preferred or "m")

    async def _resident(model):
        return False

    monkeypatch.setattr(model_router, "select_chat_model", _select)
    monkeypatch.setattr(model_router, "ambient_tier_evicted", _resident)
    monkeypatch.setattr(httpx, "AsyncClient", _NeverAnswers)
    # The real queue (the yield reads the caller's own lane ticket), with the
    # admission governor off so the P3 job reaches the model call at all.
    monkeypatch.setattr(llm_queue.governor, "enabled", False)
    monkeypatch.setattr(PriorityLLMQueue, "preempt_requested", property(lambda self: True))


def _yielded(purpose: str) -> bool:
    """The newest audit row for `purpose` records a yield to a clinician."""
    with db_session() as db:
        rows = db.execute("SELECT metadata_json FROM audit_logs WHERE "
                          "event_category = 'ai_inference' ORDER BY id DESC LIMIT 20").fetchall()
    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        if meta.get("purpose") == purpose:
            return "clinician" in (meta.get("detail") or "")
    return False


def _within(seconds, coro):
    async def run():
        return await asyncio.wait_for(coro, timeout=seconds)
    return asyncio.run(run())


def test_the_ward_narrator_skips_its_refresh(clinician_turn_in_flight):
    from services.sage_narrator_service import WardNarrator
    snapshot = {"census": 1, "risk_distribution": {}, "watchlist": [],
                "recent_events": [], "active_alerts": 0}

    assert _within(2, WardNarrator()._narrate(snapshot)) is None
    assert llm_queue.background_active == 0
    assert _yielded("ward_narrator")


def test_the_history_summary_keeps_its_head_trim(clinician_turn_in_flight):
    from services import sage_history_service

    assert _within(2, sage_history_service._generate("a long earlier turn " * 40)) is None
    assert llm_queue.background_active == 0
    assert _yielded("sage_history_summary")


def test_a_yielded_projection_is_shown_but_never_cached(clinician_turn_in_flight, monkeypatch):
    """The cache is reused for as long as the inputs hash matches, so caching
    the heuristic a yield produced would pin the patient to it."""
    from services import timeline_service

    pid = f"YLD-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, mrn, name, age, sex, diagnosis, status) "
                   "VALUES (?, ?, 'Yield Patient', 60, 'M', 'Pneumonia', 'admitted')",
                   (pid, f"MRN-{pid}"))

    out = _within(5, timeline_service.get_timeline_projection(pid, "stable"))
    assert out["source"] == "heuristic"
    assert _yielded("expected_course_projection")
    with db_session() as db:
        row = db.execute("SELECT 1 FROM timeline_projections WHERE patient_id = ?",
                         (pid,)).fetchone()
        db.execute("DELETE FROM patients WHERE id = ?", (pid,))
    assert row is None
