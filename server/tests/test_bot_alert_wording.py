"""The simulator lane's Relay alert: same band, same words (F045), and the
service identities it writes as stay protected (F108).

The alert text used to be random.choice over three templates, so two identical
deteriorations read differently and "Consider bedside evaluation" could land on
a critical page.
"""
import asyncio
import re
from pathlib import Path

import pytest

import services.bot_service as bot_service
from database import SERVICE_ACCOUNT_IDS


def _payload(pid: str, level: str, news2: int) -> dict:
    return {
        "patient_id": pid, "timestamp": "2026-10-03T05:00:00+00:00",
        "heart_rate": 124.0, "systolic_bp": 92.0, "diastolic_bp": 58.0, "spo2": 91.0,
        "temperature": 38.6, "respiratory_rate": 25.0, "consciousness": "alert", "on_oxygen": False,
        "risk": {"level": level, "score": 0.5, "raw_news2": news2, "drivers": ["Tachycardia"],
                 "breakdown": []},
    }


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []

    async def fake_run_db(fn, *a, **k):
        if fn is bot_service._persist_alerts:
            out.extend(a[0])
        return []

    async def fake_record_batch(obs, lane):
        return {}

    async def fake_ai(*a, **k):
        return ""

    from services import shadow_service
    monkeypatch.setattr(shadow_service, "arecord_batch", fake_record_batch)
    monkeypatch.setattr(bot_service, "run_db", fake_run_db)
    monkeypatch.setattr(bot_service, "generate_ai_recommendation", fake_ai)
    monkeypatch.setattr(bot_service, "_cooldowns_reconciled", True)
    return out


def _dispatch(payloads):
    asyncio.run(bot_service.evaluate_and_dispatch_alerts(None, payloads))


@pytest.mark.parametrize("level,news2,ask", [
    ("critical", 9, "Immediate review recommended."),
    ("elevated", 5, "Urgent review recommended."),
])
def test_the_same_band_always_reads_the_same(sent, level, news2, ask):
    pids = [f"PAT-WORDING-{level}-{i}" for i in range(6)]
    for pid in pids:
        bot_service._alert_cooldowns.pop(pid, None)
    try:
        _dispatch([_payload(pid, level, news2) for pid in pids])
    finally:
        for pid in pids:
            bot_service._alert_cooldowns.pop(pid, None)
    assert len(sent) == 6
    texts = {a["content"] for a in sent}
    assert len(texts) == 1, "identical deteriorations were worded differently"
    text = texts.pop()
    assert text.startswith("⚠️ VIGIL ALERT: ")
    assert text.rstrip().endswith(ask)
    assert "Auto-Alert" not in text and "Consider bedside evaluation" not in text


def test_the_bot_identities_are_protected_service_accounts():
    assert {bot_service.VIGIL_BOT_ID, bot_service.LAB_BOT_ID, bot_service.LENS_BOT_ID} <= SERVICE_ACCOUNT_IDS


def test_no_lane_spells_a_bot_identity_by_hand():
    """One name per identity: a literal elsewhere is how a lane drifts off the
    protected set."""
    root = Path(__file__).resolve().parent.parent / "services"
    for name in ("bot_service.py", "ingestion_service.py"):
        src = (root / name).read_text()
        literals = re.findall(r"""['"](bot_vigil|bot_lab|bot_lens)['"]""", src)
        if name == "bot_service.py":
            assert sorted(literals) == ["bot_lab", "bot_lens", "bot_vigil"], literals
        else:
            assert literals == [], f"{name} spells {literals} by hand"
