"""Phase 3: Sepsis Six / deterioration care-bundle timers."""
import uuid

import pytest

from database import db_session
from services.sepsis_bundle_service import (
    start_bundle, complete_element, cancel_bundle,
    get_bundles_for_patient, maybe_autostart, _ELEMENTS,
)


@pytest.fixture()
def patient():
    pid = f"PATBDL{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?,?,?)",
                   (pid, "Bundle Test", f"MRN-{uuid.uuid4().hex[:6]}"))
    return pid


def test_start_is_idempotent(patient):
    b1 = start_bundle(patient, trigger_reason="test")
    b2 = start_bundle(patient, trigger_reason="test again")
    assert b1["id"] == b2["id"]  # no stacked active bundles
    assert b1["status"] == "active"
    assert b1["compliance_pct"] == 0
    assert b1["total_count"] == len(_ELEMENTS)


def test_complete_elements_and_autocomplete(patient):
    b = start_bundle(patient)
    for i, key in enumerate(_ELEMENTS):
        b = complete_element(b["id"], key)
    assert b["status"] == "completed"
    assert b["compliance_pct"] == 100
    assert b["completed_count"] == len(_ELEMENTS)


def test_unknown_element_raises(patient):
    b = start_bundle(patient)
    with pytest.raises(ValueError):
        complete_element(b["id"], "nonsense_element")


def test_compliance_decoration_marks_pending(patient):
    b = start_bundle(patient)
    b = complete_element(b["id"], "antibiotics")
    states = {e["key"]: e["state"] for e in b["elements"]}
    assert states["antibiotics"] in ("done_on_time", "done_late")
    # freshly-started so others are still pending (within target window)
    assert states["cultures"] == "pending"
    assert b["on_track"] is True


def test_cancel(patient):
    b = start_bundle(patient)
    assert cancel_bundle(b["id"]) is True
    assert get_bundles_for_patient(patient, active_only=True) == []


def test_autostart_triggers_on_high_news2(patient):
    assert maybe_autostart(patient, {"raw_news2": 3}) is None  # below threshold
    b = maybe_autostart(patient, {"raw_news2": 8})
    assert b is not None
    assert b["source"] == "vigil_auto"
    # idempotent: second call returns the same active bundle
    b2 = maybe_autostart(patient, {"raw_news2": 9})
    assert b2["id"] == b["id"]


# ── Who opened the bundle (2026-10-06) ───────────────────────────────────────
# The timer printed "Trigger: Manual start" with no name. The payload now says
# who opened it, resolved from users at read time, and "Orb" with the NEWS2 for
# an automatic start.

def test_manual_start_names_the_clinician(patient):
    uid = f"UBDL{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
                   (uid, "Dr Meera Sharma", "doctor", "x"))
    b = start_bundle(patient, triggered_by=uid, trigger_reason="Manual start")
    assert b["started_by_name"] == "Dr Meera Sharma"
    assert b["trigger_news2"] is None
    # The idempotent second start and both list reads say the same.
    assert start_bundle(patient, triggered_by=uid)["started_by_name"] == "Dr Meera Sharma"
    listed = get_bundles_for_patient(patient)
    assert [x["started_by_name"] for x in listed] == ["Dr Meera Sharma"]


def test_unknown_starter_prints_no_id(patient):
    b = start_bundle(patient, triggered_by="NO-SUCH-USER")
    assert b["started_by_name"] is None


def test_auto_start_is_orb_with_the_score(patient):
    b = maybe_autostart(patient, {"raw_news2": 12})
    assert b["started_by_name"] == "Orb"
    assert b["trigger_news2"] == 12
    # The stored criterion still reads as before.
    assert b["trigger_reason"] == "NEWS2 12, at or above 7"
