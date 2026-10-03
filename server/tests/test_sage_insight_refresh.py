"""Sage insight cards must not freeze at the numbers they were raised on (F031).

The dedup guard stopped a refire at the door, so a card raised at "SpO2 96% to
92%" still said so an hour later beside a chart reading 88, undated. A refire
for an insight that is still on the feed now rewrites its detail and stamps
updated_at, the time the detail describes. The title also stops doubling the
ward ("GM-1-GM-104").
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import db_session
from services.sage_proactive_service import _location, evaluate_patient


@pytest.fixture
def patient():
    pid = f"PAT-REFRESH-{uuid.uuid4().hex[:10]}"
    with db_session() as db:
        db.execute("DELETE FROM insight_feedback WHERE pattern = 'trajectory:warning'")
        db.execute(
            "INSERT INTO patients (id, name, mrn, diagnosis, status, ward, bed) "
            "VALUES (?, 'Refresh Patient', ?, 'Pneumonia', 'admitted', 'GM-1', 'GM-104')",
            (pid, f"MRN-{pid}"),
        )
    yield pid
    with db_session() as db:
        db.execute("DELETE FROM sage_insights WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _chart(pid, spo2, ago_min):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=ago_min)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, spo2, temperature, respiratory_rate) "
            "VALUES (?, ?, 80, ?, 37.0, 16)",
            (pid, ts, spo2),
        )


def _spo2_insights(pid):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM sage_insights WHERE patient_id = ? AND dedup_key = ?",
            (pid, f"{pid}:trajectory:spo2"),
        ).fetchall()]


def test_a_refire_rewrites_the_detail_of_the_card_on_the_feed(patient):
    _chart(patient, 96, ago_min=40)
    _chart(patient, 94, ago_min=30)
    _chart(patient, 92, ago_min=20)
    assert evaluate_patient(patient)
    [raised] = _spo2_insights(patient)
    assert "96% to 92%" in raised["detail"]

    _chart(patient, 88, ago_min=1)
    # SpO2 88 on air scores 3, so Rule 1 now raises its own "is deteriorating"
    # card (NEWS2 2 to 3 elevated, test_sage_insights_latest_vitals). The SpO2
    # card is the subject here: one row, the same id.
    new_ids = evaluate_patient(patient)
    with db_session() as db:
        new_keys = {r["dedup_key"] for r in db.execute(
            f"SELECT dedup_key FROM sage_insights WHERE id IN ({','.join('?' * len(new_ids))})",
            new_ids,
        ).fetchall()} if new_ids else set()
    assert new_keys <= {f"{patient}:deterioration:elevated"}, new_keys
    [now] = _spo2_insights(patient)  # one row: the same insight is not raised twice
    assert now["id"] == raised["id"]
    assert now["created_at"] == raised["created_at"], "Raised time is when it was raised"
    assert "94% to 88%" in now["detail"], now["detail"]
    assert now["updated_at"] >= raised["updated_at"]


def test_a_dismissed_card_is_not_rewritten(patient):
    _chart(patient, 96, ago_min=40)
    _chart(patient, 94, ago_min=30)
    _chart(patient, 92, ago_min=20)
    evaluate_patient(patient)
    with db_session() as db:
        db.execute("UPDATE sage_insights SET state = 'dismissed' WHERE patient_id = ?", (patient,))
    _chart(patient, 88, ago_min=1)
    evaluate_patient(patient)
    [row] = _spo2_insights(patient)
    assert "96% to 92%" in row["detail"], "a clinician's dismissed record stays as it was"


def test_the_title_names_the_bed_once(patient):
    _chart(patient, 96, ago_min=40)
    _chart(patient, 94, ago_min=30)
    _chart(patient, 92, ago_min=20)
    evaluate_patient(patient)
    [row] = _spo2_insights(patient)
    assert "(GM-104)" in row["title"]
    assert "GM-1-GM-104" not in row["title"]


@pytest.mark.parametrize("ward, bed, expected", [
    ("GM-1", "GM-104", "GM-104"),
    ("ICU", "3", "ICU, bed 3"),
    (None, "B2", "B2"),
    ("Ward 7", None, "Ward 7"),
    (None, None, "location not recorded"),
])
def test_location_text(ward, bed, expected):
    assert _location(ward, bed) == expected
