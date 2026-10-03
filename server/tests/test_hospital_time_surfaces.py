"""Server surfaces that print or file hospital time must use the ward's clock (F025).

Every one of these used UTC on an IST ward:
  * End of shift offered "Morning 12:30-20:30" at 18:40 IST, because shift
    windows were laid on UTC midnight (the pilot's evidence surface);
  * the I-PASS sheet stamped the AI supplement "... UTC" and printed a raw ISO
    "Auto-drafted" time;
  * the downtime ward pack printed raw UTC ISO strings;
  * an emergency surgical case raised 00:00-05:30 IST was filed under yesterday.

The zone is pinned to Asia/Kolkata here so the suite does not depend on the
machine it runs on.
"""
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from database import db_session
from services import hospital_time
from services.vitals_source import hl7v2

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _ist(monkeypatch):
    monkeypatch.setattr(hl7v2, "_default_tz", IST)
    monkeypatch.setattr(hl7v2, "_default_tz_resolved", True)
    monkeypatch.delenv("ORB_SCORECARD_SHIFTS", raising=False)


def _pin_now(monkeypatch, utc: datetime):
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: utc)


# ── the formatter ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    "2026-10-02 07:49:00",              # SQLite CURRENT_TIMESTAMP: bare, UTC
    "2026-10-02T07:49:00+00:00",
    "2026-10-02T07:49:00.123456Z",
    datetime(2026, 10, 2, 7, 49, tzinfo=timezone.utc),
])
def test_a_stored_utc_instant_prints_on_the_ward_clock(raw):
    assert hospital_time.hospital_stamp(raw) == "2 Oct 2026, 13:19 IST"


@pytest.mark.parametrize("raw", [None, "", "not a time"])
def test_an_unreadable_time_prints_nothing(raw):
    assert hospital_time.hospital_stamp(raw) == ""


# ── shadow End of shift ──────────────────────────────────────────────────────
def test_at_1840_ist_the_current_shift_is_the_evening_one():
    from routes import shadow_routes
    now = datetime(2026, 10, 2, 13, 10, tzinfo=timezone.utc)   # 18:40 IST
    windows = shadow_routes._windows_around(now)
    current = windows[-1]
    assert current["shift"] == "evening"
    assert current["start"] == datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc)  # 15:00 IST
    assert current["end"] == datetime(2026, 10, 2, 17, 30, tzinfo=timezone.utc)   # 23:00 IST
    body = shadow_routes._window_body(current, [], [])
    assert body["label"] == "15:00–23:00"
    previous = windows[0]
    assert previous["shift"] == "morning"
    assert shadow_routes._window_body(previous, [], [])["label"] == "07:00–15:00"


def test_just_after_midnight_ist_is_still_last_nights_shift():
    from routes import shadow_routes
    now = datetime(2026, 10, 2, 18, 40, tzinfo=timezone.utc)   # 00:10 IST, 3 Oct
    current = shadow_routes._windows_around(now)[-1]
    assert current["shift"] == "night"
    assert current["start"].astimezone(IST) == datetime(2026, 10, 2, 23, 0, tzinfo=IST)


def test_the_scorecard_counts_the_same_windows_the_nurse_confirms():
    """A confirmation is matched to coverage by its period; if the two sides lay
    shifts on different clocks no confirmation ever covers anything."""
    from routes import shadow_routes
    from services import shadow_scorecard_service as sc
    now = datetime(2026, 10, 2, 13, 10, tzinfo=timezone.utc)
    nurse_side = {(w["start"], w["end"]) for w in shadow_routes._windows_around(now)}
    day_start = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)  # 00:00 IST 2 Oct
    day_end = datetime(2026, 10, 2, 18, 30, tzinfo=timezone.utc)
    scorecard_side = {(w["start"], w["end"]) for w in sc._ward_shift_windows(day_start, day_end, ["A"])}
    assert nurse_side <= scorecard_side


# ── I-PASS and the downtime pack ─────────────────────────────────────────────
def test_the_ipass_stamp_is_hospital_time():
    from services.ipass_service import _stamp_phrase
    assert _stamp_phrase("2026-10-02T07:49:00+00:00") == "2 Oct 2026, 13:19 IST"


def test_the_ward_pack_prints_hospital_time():
    from services.downtime_service import _render_ward_pack
    item = {"bed": "A-1", "name": "Pack Patient", "mrn": "M-1", "code_status": "Full",
            "escalation": {"status": "open", "current_tier": 1, "max_tier": 3,
                           "raised_at": "2026-10-02 07:49:00"},
            "envelope": {"created_at": "2026-10-02T07:55:00+00:00", "created_by": "auto",
                         "snapshot": {"demographics": {"name": "Pack Patient"}}}}
    doc = _render_ward_pack("Ward A", [item], "2026-10-02T08:00:00+00:00")
    assert "generated 2 Oct 2026, 13:30 IST" in doc
    assert "raised 2 Oct 2026, 13:19 IST" in doc
    assert "Snapshot captured 2 Oct 2026, 13:25 IST" in doc
    assert "T08:00:00" not in doc and "T07:55:00" not in doc


# ── surgical filing ──────────────────────────────────────────────────────────
def test_a_case_raised_after_midnight_ist_is_filed_under_today(monkeypatch):
    import config
    from services import surgery_escalation_service as esc
    monkeypatch.setattr(config, "DEMO_MODE", False)
    _pin_now(monkeypatch, datetime(2026, 10, 2, 18, 40, tzinfo=timezone.utc))  # 00:10 IST, 3 Oct

    doc = f"HTDOC{uuid.uuid4().hex[:6].upper()}"
    pid = f"PATHT{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Dr HT', 'doctor', 'x')",
                   (doc,))
        db.execute("INSERT INTO patients (id, name, mrn, ward, bed, diagnosis) "
                   "VALUES (?, 'Night Case', ?, 'Ward A', 'A-2', 'Acute abdomen')",
                   (pid, f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO channels (id, name, type, patient_id) VALUES (?, 'Case', 'case_room', ?)",
                   (f"chan-{pid}", pid))
    try:
        req = esc.create_surgery_prep_request(pid, f"chan-{pid}", doc, "Dr HT", "Laparotomy",
                                              "Perforation", "emergency", "")
        out = esc.confirm_surgery_prep(req["id"], doc, "Dr HT", req["idempotency_key"])
        with db_session() as db:
            row = db.execute("SELECT scheduled_date, scheduled_time FROM surgeries WHERE id = ?",
                             (out["surgery_id"],)).fetchone()
        assert row["scheduled_date"] == "2026-10-03"
        assert row["scheduled_time"] == "00:10"
    finally:
        with db_session() as db:
            db.execute("DELETE FROM surgery_prep_requests WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM surgeries WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM channels WHERE patient_id = ?", (pid,))
