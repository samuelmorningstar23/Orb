"""Rule 3 of sage_proactive_service (the sepsis pattern) against the SIRS edges.

SIRS is strict: temperature > 38.0 C or < 36.0 C, heart rate > 90, respiratory
rate > 20 (or PaCO2 < 32), white cell count > 12 or < 4 (or > 10% bands), and
it is MET by any 2 of those 4. The rule fired on >= at every edge, so "Temp
38.0C, HR 90, RR 20, meets SIRS criteria" could print although none of those
values meets SIRS; it ignored the hypothermia limb; and it demanded all three
vital-sign limbs, so tachycardia plus tachypnoea without a fever never fired.
Orb holds no structured white cell count or blood gas, so only the three
vital-sign limbs can be checked, and the insight says so.

The rule tests and prints the value AS CHARTED (services/vital_charting.py,
half-up to chart precision), the same number NEWS2 scores, and reads the newest
vitals by observation time, the same set NEWS2 reads.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import db_session
from services.sage_proactive_service import evaluate_patient


@pytest.fixture
def patient():
    pid = f"PAT-SIRS-{uuid.uuid4().hex[:10]}"
    with db_session() as db:
        # A pattern the feedback loop has learned to suppress would hide the
        # insight for a reason that has nothing to do with the thresholds.
        db.execute("DELETE FROM insight_feedback WHERE pattern = 'sepsis:warning'")
        db.execute(
            "INSERT INTO patients (id, name, mrn, diagnosis, status, ward, bed) "
            "VALUES (?, 'SIRS Edge', ?, 'Pneumonia', 'admitted', 'W1', '1')",
            (pid, f"MRN-{pid}"),
        )
    yield pid
    with db_session() as db:
        db.execute("DELETE FROM sage_insights WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _chart(pid, temp, hr, rr, ago_min=0):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=ago_min)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, spo2, temperature, respiratory_rate) "
            "VALUES (?, ?, ?, 97, ?, ?)",
            (pid, ts, hr, temp, rr),
        )


def _sepsis_detail(pid):
    """Run the sweep, return the sepsis insight's detail, or None when Rule 3
    did not fire."""
    evaluate_patient(pid)
    with db_session() as db:
        row = db.execute(
            "SELECT detail FROM sage_insights WHERE patient_id = ? AND insight_type = 'sepsis'",
            (pid,),
        ).fetchone()
    return row["detail"] if row else None


# (temp C, HR, RR, expected detail prefix or None for "must not fire").
# Edge rows keep exactly ONE other limb abnormal, so the edge decides.
EDGES = [
    pytest.param(38.0, 110, 16, None, id="temp-38.0-is-not-fever"),
    pytest.param(38.1, 110, 16, "Temp 38.1C, HR 110, RR 16: meets SIRS, 2 of 4", id="temp-38.1-fires"),
    pytest.param(38.6, 90, 16, None, id="hr-90-is-not-tachycardia"),
    pytest.param(38.6, 91, 16, "Temp 38.6C, HR 91, RR 16: meets SIRS, 2 of 4", id="hr-91-fires"),
    pytest.param(37.0, 110, 20, None, id="rr-20-is-not-tachypnoea"),
    pytest.param(37.0, 110, 21, "Temp 37.0C, HR 110, RR 21: meets SIRS, 2 of 4", id="rr-21-fires"),
    pytest.param(35.9, 110, 16, "Temp 35.9C, HR 110, RR 16: meets SIRS, 2 of 4", id="temp-35.9-hypothermia-fires"),
    pytest.param(36.0, 110, 16, None, id="temp-36.0-is-not-hypothermia"),
    pytest.param(38.0, 90, 20, None, id="the-old-printed-triple-never-fires"),
]

# Any 2 of 4, not all 3 vitals.
COUNTS = [
    pytest.param(37.0, 110, 24, "Temp 37.0C, HR 110, RR 24: meets SIRS, 2 of 4", id="no-fever-two-limbs-fires"),
    pytest.param(38.6, 110, 24, "Temp 38.6C, HR 110, RR 24: meets SIRS, 3 of 4", id="three-limbs"),
    pytest.param(37.0, 110, 16, None, id="one-limb-does-not-fire"),
    pytest.param(None, 110, 24, "Temp --, HR 110, RR 24: meets SIRS, 2 of 4", id="unmeasured-temp-two-limbs-fires"),
    pytest.param(None, 110, None, None, id="one-limb-measured-does-not-fire"),
]

# Raw device values between chart steps: the rule decides on, and prints, the
# charted number.
CHARTED = [
    pytest.param(38.05, 110, 16, "Temp 38.1C, HR 110, RR 16: meets SIRS, 2 of 4", id="temp-38.05-charts-38.1-and-fires"),
    pytest.param(38.04, 110, 16, None, id="temp-38.04-charts-38.0-no-fire"),
    pytest.param(38.6, 90.4, 16, None, id="hr-90.4-charts-90-no-fire"),
    pytest.param(38.6, 90.5, 16, "Temp 38.6C, HR 91, RR 16: meets SIRS, 2 of 4", id="hr-90.5-charts-91-and-fires"),
    pytest.param(37.0, 110, 20.5, "Temp 37.0C, HR 110, RR 21: meets SIRS, 2 of 4", id="rr-20.5-charts-21-and-fires"),
    pytest.param(35.95, 110, 16, None, id="temp-35.95-charts-36.0-no-fire"),
    pytest.param(35.94, 110, 16, "Temp 35.9C, HR 110, RR 16: meets SIRS, 2 of 4", id="temp-35.94-charts-35.9-fires"),
]


@pytest.mark.parametrize("temp,hr,rr,expected", EDGES + COUNTS + CHARTED)
def test_sirs_edges(patient, temp, hr, rr, expected):
    _chart(patient, temp, hr, rr)
    detail = _sepsis_detail(patient)
    if expected is None:
        assert detail is None, f"Rule 3 fired below SIRS: {detail!r}"
    else:
        assert detail is not None, "Rule 3 did not fire on SIRS-positive vitals"
        assert detail.startswith(expected), detail
        assert "white cell count not recorded" in detail, detail


def test_reads_the_newest_observation_not_the_newest_row(patient):
    """A back-charted older set entered last must not stand in for the latest
    vitals. Ordering by row id alone read it as current."""
    _chart(patient, 38.9, 118, 26, ago_min=0)    # now: SIRS-positive
    _chart(patient, 36.8, 76, 14, ago_min=120)   # 2 h ago, entered afterwards
    detail = _sepsis_detail(patient)
    assert detail is not None and detail.startswith("Temp 38.9C, HR 118, RR 26:"), detail
