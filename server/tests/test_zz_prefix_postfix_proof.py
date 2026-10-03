"""The critical-value defect, stated in terms that predate its fix.

WHY THIS IS A SEPARATE FILE FROM test_sage_ambient_critical_fastpath.py.
That file imports `_deterministic_risk_extract` and `_CRIT_CONFIDENCE`, so on a
service reverted to before the fast-path it does not fail — it fails to COLLECT,
which proves only that a symbol is missing. This file imports nothing that the
pre-fix service lacked (`_update_memory`, `_gate_content` both predate it), so it
runs against either version and states the defect as clinical behaviour:

    git stash the fast-path, run this file, and it reports
      - no risk card for a potassium of 6.8 with peaked T waves
      - Gate 3 dropping "K 6.8" before memory ever sees it
    measured pre-fix: 3 failed, 1 passed. Post-fix: 4 passed.

Keep it that way. If a future refactor makes these tests import fast-path
internals, the file stops being able to describe the world without them and the
regression becomes untestable against a reverted build.
"""
import json

import pytest

from database import db_session
from services.sage_ambient_service import _gate_content, _update_memory

ROOM = "chan-crit-proof"
PATIENT = "PAT-CRIT-PROOF"

# The exact message pair from the measured 1-in-8 failure. `classification=None`
# on BOTH is what the 4B actually produced in that run.
CRITICAL = "Lab rang - POTASSIUM 6.8 mmol/L. ECG shows peaked T waves."
TREATMENT = "Giving calcium gluconate and an insulin-dextrose infusion now."
RETRACTION = ("The lab called back about that potassium — the sample was "
              "haemolysed. The repeat potassium is 4.1. There is no "
              "hyperkalaemia.")


def _clear():
    with db_session() as db:
        db.execute("DELETE FROM sage_room_memory WHERE room_id = ?", (ROOM,))


def _row():
    with db_session() as db:
        r = db.execute("SELECT * FROM sage_room_memory WHERE room_id = ?",
                       (ROOM,)).fetchone()
        return dict(r) if r else None


def _live():
    return [f for f in json.loads((_row() or {}).get("risk_flags") or "[]")
            if (f.get("confidence") or 0) >= 0.80]


def _msg(mid, content):
    return {"id": mid, "sender_name": "Nurse Priya Reddy",
            "sender_role": "nurse", "content": content}


@pytest.fixture(autouse=True)
def clean():
    _clear()
    yield
    _clear()


def test_PROOF_critical_potassium_charts_a_standing_alarm_without_the_model():
    _update_memory(ROOM, PATIENT, _msg(1, CRITICAL), None, None)
    _update_memory(ROOM, PATIENT, _msg(2, TREATMENT), None, _row())
    live = _live()
    assert live, ("NO RISK CARD for potassium 6.8 with peaked T waves when the "
                  "classifier returned nothing — this is the measured defect")
    assert any("6.8" in f["detail"] for f in live)


def test_PROOF_that_alarm_is_still_retired_by_a_ward_retraction():
    _update_memory(ROOM, PATIENT, _msg(1, CRITICAL), None, None)
    assert _live(), "precondition: card must be up (fails pre-fix)"
    _update_memory(ROOM, PATIENT, _msg(2, RETRACTION), None, _row())
    assert not _live(), "the card survived the retraction"


def test_PROOF_ward_shorthand_reaches_memory():
    assert not _gate_content("K 6.8"), "Gate 3 dropped a lethal potassium"


def test_PROOF_a_normal_value_still_charts_nothing():
    """Guards the other direction: the fix must not invent alarms."""
    _update_memory(ROOM, PATIENT,
                   _msg(1, "Potassium 4.2 mmol/L, sats 96% on air, BP 124/78."),
                   None, None)
    assert not _live(), f"false critical alarm on normal observations: {_live()}"
