"""Downtime ward pack: read on paper by ward staff, so it speaks their language
(F161).

The pack printed code status as the stored token ('full_code'), the snapshot's
author as 'by auto', and note authors as staff ids. Times were already moved to
the hospital clock (hospital_stamp); they are pinned here too so the whole page
is checked in one place.
"""
import uuid

import pytest

from database import db_session
from services.downtime_service import _code_status_text, _render_ward_pack

GENERATED = "2026-10-02T07:31:00+00:00"   # 13:01 IST


def _item(*, code_status="full_code", created_by="auto", notes=None) -> dict:
    return {
        "bed": "B4", "name": "Asha Rao", "mrn": "MRN-WP1", "ward": "Ward 7",
        "code_status": code_status, "escalation": None,
        "envelope": {
            "created_at": "2026-10-02T07:25:00+00:00",   # 12:55 IST
            "created_by": created_by,
            "snapshot": {
                "demographics": {"name": "Asha Rao", "mrn": "MRN-WP1"},
                "allergies": [], "problems": [], "medications": [],
                "recent_notes": notes or [],
                "latest_vitals": None, "news2": None,
            },
        },
    }


@pytest.fixture()
def author():
    uid = f"WPDOC-{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) "
                   "VALUES (?, 'Dr Meera Pillai', 'doctor', 'x')", (uid,))
    return uid


@pytest.mark.parametrize("stored, printed", [
    ("full_code", "Full code"),
    ("dnr", "DNR (do not resuscitate)"),
    ("dni", "DNI (do not intubate)"),
    ("comfort", "Comfort care only"),
    (None, "Not recorded"),
    ("", "Not recorded"),
    # Not on the form: printed as stored, never guessed at.
    ("limited_escalation", "limited escalation"),
])
def test_code_status_is_printed_in_words(stored, printed):
    assert _code_status_text(stored) == printed


def test_the_page_uses_the_hospital_clock_and_plain_words():
    doc = _render_ward_pack("Ward 7", [_item()], GENERATED)

    assert "<b>Code status:</b> Full code" in doc
    assert "full_code" not in doc
    assert "generated 2 Oct 2026, 13:01 IST" in doc
    assert "Snapshot captured 2 Oct 2026, 12:55 IST automatically." in doc
    assert "by auto" not in doc
    assert "T07:" not in doc, "a raw UTC ISO stamp reached the printed page"


def test_people_are_named_not_numbered(author):
    note = {"note_format": "soap", "structured_note": "S: comfortable overnight",
            "author_id": author, "signed_by": author,
            "signed_at": "2026-10-02T06:00:00+00:00"}   # 11:30 IST
    doc = _render_ward_pack("Ward 7", [_item(created_by=author, notes=[note])], GENERATED)

    assert "Snapshot captured 2 Oct 2026, 12:55 IST by Dr Meera Pillai." in doc
    assert "SOAP · Dr Meera Pillai · 2 Oct 2026, 11:30 IST" in doc
    assert author not in doc


def test_an_unknown_author_is_printed_as_stored():
    note = {"note_format": "soap", "structured_note": "S: ok", "author_id": "GONE-1",
            "signed_at": "2026-10-02T06:00:00+00:00"}
    doc = _render_ward_pack("Ward 7", [_item(created_by="GONE-2", notes=[note])], GENERATED)
    assert "by GONE-2." in doc
    assert "SOAP · GONE-1 ·" in doc
