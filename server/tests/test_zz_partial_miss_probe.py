"""Partial-miss: the classifier charts ONE risk and misses the lethal one.

This is the realistic middle case between "the model got it" and "the model
returned nothing", and it is the case a message-level fallback would get wrong.
`_deterministic_extract`'s action fast-path fires only `if not new_actions` —
i.e. it stands down the moment the model produced ANY action. Copying that shape
for risk would mean a classifier that charted sepsis and missed a potassium of
6.8 suppresses the potassium card, because the message "already had a risk".

`_risk_already_charted` is per-VALUE instead of per-message, so the sepsis card
stands and the potassium card is added alongside it.
"""
import json

import pytest

from database import db_session
from services.sage_ambient_service import _update_memory

ROOM = "chan-crit-partial"
PATIENT = "PAT-CRIT-PARTIAL"


def _clear():
    with db_session() as db:
        db.execute("DELETE FROM sage_room_memory WHERE room_id = ?", (ROOM,))


def _flags():
    with db_session() as db:
        r = db.execute("SELECT risk_flags FROM sage_room_memory WHERE room_id = ?",
                       (ROOM,)).fetchone()
    return json.loads((dict(r) if r else {}).get("risk_flags") or "[]")


@pytest.fixture(autouse=True)
def clean():
    _clear()
    yield
    _clear()


def test_a_partial_classifier_miss_still_gets_the_lethal_value_charted():
    sepsis_only = {
        "type": "risk_flag", "confidence": 0.9,
        "signal": "Febrile with rising lactate",
        "risk_detail": "Possible evolving sepsis — febrile with a rising lactate",
        "risk_rationale": "Fever and lactate trend in pneumonia",
    }
    _update_memory(
        ROOM, PATIENT,
        {"id": 1, "sender_name": "Nurse Priya Reddy", "sender_role": "nurse",
         "content": "Febrile 38.9. POTASSIUM 6.8 mmol/L, ECG shows peaked T waves."},
        sepsis_only, None)

    flags = _flags()
    live = [f for f in flags if (f.get("confidence") or 0) >= 0.80]

    assert any("sepsis" in f["detail"].lower() for f in live), \
        f"the classifier's own card was lost: {live}"
    assert any("potassium" in f["detail"].lower() for f in live), (
        f"the classifier charted sepsis and missed the potassium, and the "
        f"fallback stood down because the message 'already had a risk': {live}"
    )
    assert any(f.get("source") == "deterministic_critical_value" for f in live)
