"""Each LLM lane leaves one ai_inference row with the prompt's hash (F038).

The lanes that post through services/ollama_gateway get their row there
(tests/test_ollama_gateway.py proves the row, tests/test_ollama_gateway_guard.py
proves no module goes round the gateway). This file covers the lanes that write
their own row because they run through a provider seam, plus the Vigil
recommendation, the per-critical-patient garnish that is also a P3 yielder
(F036).
"""
import asyncio
import json

import pytest

from database import db_session
from services.ai_governance import _PENDING_AUDITS, hash_prompt

# Sage Tier 2's own fixtures and stand-ins: the real queue, sanitizer, screen
# and DB paths with only the model faked.
from tests.test_sage_assessment import (  # noqa: F401 (autouse fixtures)
    BRIEF_OUTPUT,
    _FakeProvider,
    _formulary,
    _fresh_instrument,
    _generate,
    _mk_patient,
)


def _rows(purpose: str) -> list[dict]:
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE event_category = 'ai_inference' ORDER BY id"
        ).fetchall()]
    out = []
    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        if meta.get("purpose") == purpose:
            r["meta"] = meta
            out.append(r)
    return out


async def _settle_audits():
    """Wait for scheduled (not awaited) audit writes to land."""
    pending = [t for t in list(_PENDING_AUDITS) if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=5)


def _hex64(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= set("0123456789abcdef")


# ── Sage Tier 2 (llm_providers seam) ─────────────────────────────────────────

async def test_a_tier2_pass_writes_one_row_with_the_hash_of_what_the_model_read(monkeypatch):
    patient = _mk_patient()
    provider = _FakeProvider(BRIEF_OUTPUT)
    before = len(_rows("sage_alert_assessment"))

    await _generate(patient, monkeypatch, provider)
    await _settle_audits()

    rows = _rows("sage_alert_assessment")[before:]
    assert len(rows) == 1
    row = rows[0]
    assert row["patient_id"] == patient
    assert row["outcome"] == "success"
    assert row["meta"]["prompt_sha256"] == hash_prompt({"messages": provider.messages[0]})


async def test_a_failed_tier2_pass_is_audited_with_its_reason(monkeypatch):
    patient = _mk_patient()
    provider = _FakeProvider(BRIEF_OUTPUT, raises=RuntimeError("model exploded"))
    before = len(_rows("sage_alert_assessment"))

    assert await _generate(patient, monkeypatch, provider) is None
    await _settle_audits()

    rows = _rows("sage_alert_assessment")[before:]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["meta"]["detail"] == "model_error"
    assert _hex64(rows[0]["meta"]["prompt_sha256"])


# ── Scribe (scribe_providers seam) ───────────────────────────────────────────

class _ScribeProvider:
    def __init__(self, answer="SUBJECTIVE:\nSOB overnight."):
        self.answer = answer
        self.sent = []

    def generate(self, prompt, system="", **kw):
        self.sent.append((system, prompt))
        return self.answer

    async def agenerate(self, prompt, system="", **kw):
        self.sent.append((system, prompt))
        return self.answer

    async def agenerate_stream(self, prompt, system="", **kw):
        self.sent.append((system, prompt))
        for word in self.answer.split(" "):
            yield word + " "


def _free_queue(monkeypatch):
    from services.llm_queue_service import llm_queue

    async def _acquire(priority=0, timeout=120.0):
        return True

    monkeypatch.setattr(llm_queue, "acquire", _acquire)
    monkeypatch.setattr(llm_queue, "release", lambda: None)


def test_the_scribe_draft_is_audited_against_the_patient_and_author(monkeypatch):
    from services import scribe_service
    provider = _ScribeProvider()
    monkeypatch.setattr(scribe_service, "get_provider", lambda name: provider)
    _free_queue(monkeypatch)
    before = len(_rows("scribe_draft"))

    asyncio.run(scribe_service.structure_transcript(
        "Known diabetic, sugars high overnight.", "soap",
        patient_id="P001", user_id="DOC001"))

    rows = _rows("scribe_draft")[before:]
    assert len(rows) == 1
    system, prompt = provider.sent[0]
    assert rows[0]["patient_id"] == "P001"
    assert rows[0]["actor_user_id"] == "DOC001"
    assert rows[0]["meta"]["prompt_sha256"] == hash_prompt({"system": system, "prompt": prompt})


def test_the_streamed_scribe_draft_is_audited_once(monkeypatch):
    from services import scribe_service
    provider = _ScribeProvider()
    monkeypatch.setattr(scribe_service, "get_provider", lambda name: provider)
    _free_queue(monkeypatch)
    before = len(_rows("scribe_draft"))

    async def run():
        out = [t async for t in scribe_service.structure_transcript_stream(
            "Known diabetic, sugars high overnight.", "soap", patient_id="P002")]
        await _settle_audits()
        return out

    assert asyncio.run(run())
    rows = _rows("scribe_draft")[before:]
    assert len(rows) == 1
    assert rows[0]["patient_id"] == "P002"
    assert rows[0]["outcome"] == "success"


def test_scribe_correction_and_verification_are_audited(monkeypatch):
    from config import SCRIBE
    from services import scribe_service
    provider = _ScribeProvider('{"verified": true, "confidence": 0.9}')
    monkeypatch.setattr(scribe_service, "get_provider", lambda name: provider)
    monkeypatch.setitem(SCRIBE["verify"], "enabled", True)
    _free_queue(monkeypatch)
    before_c = len(_rows("scribe_correction"))
    before_v = len(_rows("scribe_verify"))

    scribe_service.correct_transcript("Patient complains of breathlessness overnight. " * 5)
    asyncio.run(scribe_service.verify_note("dictation text", "SUBJECTIVE: note",
                                           patient_id="P003", user_id="DOC001"))

    assert len(_rows("scribe_correction")[before_c:]) == 1
    verify = _rows("scribe_verify")[before_v:]
    assert len(verify) == 1 and verify[0]["patient_id"] == "P003"
    assert _hex64(verify[0]["meta"]["prompt_sha256"])


def test_an_empty_scribe_answer_is_audited_as_an_error(monkeypatch):
    """The providers swallow a backend failure and return "", so an empty
    answer must not be recorded as served."""
    from services import scribe_service
    monkeypatch.setattr(scribe_service, "get_provider", lambda name: _ScribeProvider(""))
    _free_queue(monkeypatch)
    before = len(_rows("scribe_draft"))

    asyncio.run(scribe_service.structure_transcript("dictation", "soap"))

    rows = _rows("scribe_draft")[before:]
    assert rows[0]["outcome"] == "failure"
    assert rows[0]["meta"]["detail"] == "empty response"


# ── Vigil recommendation (gateway, P3 yielder) ──────────────────────────────

class _VigilClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        class _R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"message": {"content": "Recheck vitals in 15 minutes."}}
        return _R()


def _vigil_ready(monkeypatch):
    import httpx
    from services import model_router, vigil_service

    async def _resident(model):
        return False

    monkeypatch.setattr(model_router, "ambient_tier_evicted", _resident)
    monkeypatch.setitem(vigil_service._LLM_BREAKER, "open_until", 0.0)
    monkeypatch.setattr(httpx, "AsyncClient", _VigilClient)
    return vigil_service


def test_the_vigil_recommendation_is_audited(monkeypatch):
    vigil_service = _vigil_ready(monkeypatch)
    _free_queue(monkeypatch)
    before = len(_rows("vigil_recommendation"))

    text = asyncio.run(vigil_service.generate_ai_recommendation(
        "HR 132, BP 82/49", "critical", ["tachycardia"]))

    assert text == "Recheck vitals in 15 minutes."
    rows = _rows("vigil_recommendation")[before:]
    assert len(rows) == 1 and _hex64(rows[0]["meta"]["prompt_sha256"])


def test_the_vigil_recommendation_gives_the_gpu_to_a_clinician(monkeypatch):
    """A clinician's turn in flight: the garnish yields and the alert gets the
    existing static sentence instead of sharing the GPU with the turn."""
    from services.llm_queue_service import PriorityLLMQueue, llm_queue
    vigil_service = _vigil_ready(monkeypatch)
    # The real queue (the yield reads this task's lane ticket), ungoverned so
    # the P3 admission is not shed before it can yield.
    monkeypatch.setattr(llm_queue.governor, "enabled", False)
    monkeypatch.setattr(PriorityLLMQueue, "preempt_requested", property(lambda self: True))
    before = len(_rows("vigil_recommendation"))

    text = asyncio.run(vigil_service.generate_ai_recommendation(
        "HR 132, BP 82/49", "critical", ["tachycardia"]))

    assert text == vigil_service._FALLBACK_RECOMMENDATION
    assert llm_queue.background_active == 0, "the yielded garnish kept the lane"
    rows = _rows("vigil_recommendation")[before:]
    assert len(rows) == 1 and "clinician" in rows[0]["meta"]["detail"]
