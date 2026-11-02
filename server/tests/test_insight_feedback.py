"""Insight feedback-loop tests: acceptance-rate learning + suppression, with the
critical-severity safety exemption.
"""
import uuid

import pytest

from database import db_session
from services import sage_feedback_service as fb
from services.sage_feedback_service import (
    is_pattern_suppressed, pattern_of, pattern_stats, record_feedback,
)


@pytest.fixture(autouse=True)
def _clean_feedback():
    with db_session() as db:
        db.execute("DELETE FROM insight_feedback")
    yield
    with db_session() as db:
        db.execute("DELETE FROM insight_feedback")


def _feed(pattern, acted, dismissed):
    for _ in range(acted):
        record_feedback(pattern, "acted")
    for _ in range(dismissed):
        record_feedback(pattern, "dismissed")


def test_pattern_stats_computes_acceptance_rate():
    _feed("stewardship:info", acted=2, dismissed=8)
    s = pattern_stats("stewardship:info")
    assert s["acted"] == 2 and s["dismissed"] == 8 and s["total"] == 10
    assert abs(s["acceptance_rate"] - 0.2) < 1e-9


def test_consistently_rejected_warning_pattern_is_suppressed():
    # 1 acted / 11 dismissed = ~8% acceptance, over MIN_SAMPLES → suppressed.
    _feed("trajectory:warning", acted=1, dismissed=11)
    assert is_pattern_suppressed("trajectory", "warning") is True


def test_below_min_samples_never_suppressed():
    _feed("trajectory:warning", acted=0, dismissed=3)  # only 3 < MIN_SAMPLES
    assert is_pattern_suppressed("trajectory", "warning") is False


def test_useful_pattern_not_suppressed():
    _feed("sepsis:warning", acted=9, dismissed=1)  # 90% acceptance
    assert is_pattern_suppressed("sepsis", "warning") is False


def test_critical_severity_is_never_suppressed():
    # Even if clinicians dismiss it every single time, a critical alert is a
    # clinician decision, not noise — you never mute "patient is critical".
    _feed("deterioration:critical", acted=0, dismissed=50)
    assert is_pattern_suppressed("deterioration", "critical") is False


def test_transition_records_feedback_end_to_end():
    # Insert a real insight, dismiss it via the service, assert feedback lands.
    from services.sage_proactive_service import transition_insight
    iid = str(uuid.uuid4())
    pid = f"PAT-FB-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, 'Feedback Test', ?, 'admitted')",
                   (pid, pid))
        db.execute("INSERT INTO admissions (patient_id, department, status, admitted_by) "
                   "VALUES (?, 'General Medicine', 'active', 'D001')", (pid,))
        db.execute(
            """INSERT INTO sage_insights (id, patient_id, insight_type, severity, title, detail,
                                          target_user_id, dedup_key, state, created_at, updated_at)
               VALUES (?, ?, 'stewardship', 'info', 't', 'd', NULL, 'k', 'active',
                       datetime('now'), datetime('now'))""",
            (iid, pid),
        )
    transition_insight(iid, "dismissed", "D001", "doctor")
    s = pattern_stats(pattern_of("stewardship", "info"))
    assert s["dismissed"] == 1


def test_insert_insight_skips_suppressed_pattern():
    from services.sage_proactive_service import _insert_insight
    _feed("stewardship:info", acted=0, dismissed=12)  # suppressed
    with db_session() as db:
        iid = _insert_insight(
            db, "PAT-FB-2", "stewardship", "info", "Antibiotic review", "detail",
            None, "PAT-FB-2:stewardship:test",
        )
    assert iid is None, "a suppressed pattern must not create a new insight"


def test_insert_insight_still_creates_critical_even_if_dismissed_often():
    from services.sage_proactive_service import _insert_insight
    _feed("deterioration:critical", acted=0, dismissed=30)
    with db_session() as db:
        iid = _insert_insight(
            db, "PAT-FB-3", "deterioration", "critical", "Critical", "detail",
            None, "PAT-FB-3:deterioration:critical",
        )
    assert iid is not None, "critical insights must never be suppressed"
    with db_session() as db:
        db.execute("DELETE FROM sage_insights WHERE id = ?", (iid,))
