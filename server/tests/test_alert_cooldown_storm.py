"""A critical patient must page on DETERIORATION, not on every tick.

WHY THIS FILE EXISTS. `evaluate_and_dispatch_alerts` exempted `critical`
patients from the alert cooldown entirely:

    if risk_level != "critical" and not escalated and now - last_ts < COOLDOWN:

The intent in the comment was right — "a further deterioration (SpO2 88→80, new
hypotension) pages the team immediately rather than waiting out the 2-minute
window" — but the code did not test for deterioration. It re-alerted a critical
patient on EVERY cycle.

That was invisible for as long as the vitals simulator was crashing on every
tick (bot_service.py:145): nothing was generated, so nothing alerted. The moment
the simulator was repaired it became a storm — the simulator ticks every 5
seconds, so a steady-state critical patient produced ~12 identical auto-alerts a
minute, four of them landing in their Relay case room per minute, and the
scripted ward thread scrolled out of reach within two minutes. Alert fatigue is
precisely what the cooldown exists to prevent.

These tests pin BOTH halves, because a fix that only silences the storm would
break a safety property that was hard-won: deterioration must still page
immediately.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services.bot_service as bot_service
from services.bot_service import (
    ALERT_COOLDOWN_SECONDS,
    MIN_REALERT_SECONDS,
    evaluate_and_dispatch_alerts,
)

PID = "PAT-ALERTSTORM"


def _payload(news2: int, level: str = "critical") -> dict:
    return {
        "patient_id": PID,
        "timestamp": "2026-08-08T12:00:00+00:00",
        "heart_rate": 132.0,
        "systolic_bp": 78.0,
        "diastolic_bp": 42.0,
        "spo2": 83.0,
        "temperature": 39.4,
        "respiratory_rate": 32.0,
        "consciousness": "alert",
        "on_oxygen": True,
        "risk": {"level": level, "score": 0.9, "raw_news2": news2, "drivers": ["Severe hypoxia"]},
    }


@pytest.fixture
def captured(monkeypatch):
    """Run the dispatcher with every side effect stubbed; collect what it sent."""
    sent: list[dict] = []

    async def fake_record_batch(obs, source):
        return {}

    async def fake_ai(*a, **k):
        return "Stubbed recommendation."

    async def fake_run_db(fn, *a, **k):
        # _persist_alerts(alerts_to_send, episodes) — record and return nothing
        # persistable so the emit path below is a no-op.
        if a:
            sent.extend(a[0])
        return []

    # shadow_service is imported INSIDE the function, so patch the module the
    # import resolves to rather than an attribute on bot_service.
    from services import shadow_service
    monkeypatch.setattr(shadow_service, "arecord_batch", fake_record_batch)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation", fake_ai)
    monkeypatch.setattr(bot_service, "run_db", fake_run_db)
    bot_service._alert_cooldowns.pop(PID, None)
    yield sent
    bot_service._alert_cooldowns.pop(PID, None)


def _dispatch(payloads):
    asyncio.run(evaluate_and_dispatch_alerts(None, payloads))


# ── The storm ─────────────────────────────────────────────────────────────────

def test_a_steady_critical_patient_alerts_once_not_every_tick(captured):
    """Twelve simulator ticks at an unchanged NEWS2 = ONE page."""
    for _ in range(12):
        _dispatch([_payload(16)])

    assert len(captured) == 1, (
        f"{len(captured)} alerts for one unchanged critical patient — this is the "
        "storm that buried the Relay case rooms"
    )


# ── The safety property the storm-fix must not break ──────────────────────────

def _age_last_alert(seconds: float) -> None:
    """Pretend the last page happened `seconds` ago (no sleeping in tests)."""
    ts, level, news2 = bot_service._alert_cooldowns[PID]
    bot_service._alert_cooldowns[PID] = (ts - seconds, level, news2)


def test_a_deteriorating_critical_patient_pages_well_inside_the_cooldown(captured):
    """SpO2 88→80 style worsening must not wait out the 2-minute window."""
    _dispatch([_payload(14)])
    _age_last_alert(MIN_REALERT_SECONDS + 1)   # still far inside the cooldown
    _dispatch([_payload(17)])                  # worse

    assert len(captured) == 2, "a deteriorating critical patient was muted by the cooldown"
    assert MIN_REALERT_SECONDS < ALERT_COOLDOWN_SECONDS / 2, (
        "the deterioration floor must stay well under the cooldown, or "
        "'deterioration pages fast' stops being true"
    )


def test_a_one_point_flicker_does_not_page_every_tick(captured):
    """NEWS2 wobbling up by a point on consecutive 5-second samples is noise."""
    _dispatch([_payload(14)])
    for n in (15, 16, 15, 16, 17):
        _dispatch([_payload(n)])

    assert len(captured) == 1, "monitor noise rang the bell repeatedly"


def test_band_escalation_still_bypasses_the_cooldown(captured):
    _dispatch([_payload(6, level="elevated")])
    _age_last_alert(MIN_REALERT_SECONDS + 1)
    _dispatch([_payload(7, level="critical")])

    assert len(captured) == 2, "elevated→critical escalation was muted"


def test_improvement_does_not_re_page(captured):
    """Getting BETTER is not news that needs to interrupt anyone."""
    _dispatch([_payload(17)])
    _age_last_alert(MIN_REALERT_SECONDS + 1)
    _dispatch([_payload(12)])

    assert len(captured) == 1


def test_the_cooldown_expires_normally(captured):
    _dispatch([_payload(16)])
    _age_last_alert(ALERT_COOLDOWN_SECONDS + 1)
    _dispatch([_payload(16)])

    assert len(captured) == 2, "the periodic re-alert after the cooldown was lost"


def test_a_stable_patient_never_alerts(captured):
    _dispatch([_payload(1, level="stable")])
    assert captured == []
