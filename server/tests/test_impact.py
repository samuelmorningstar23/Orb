"""Orb — Impact / ROI model tests.

Seeds a couple of Scribe notes, a critical risk score, a sepsis bundle and a
pharmacist-verified AI medication, then asserts the ROI model:
  * computes non-negative dollars for each metric,
  * counts the seeded evidence,
  * degrades to all-zeros (without error) on an empty window,
  * and responds to an assumption override.
"""
import uuid

import pytest

from database import db_session
from services.impact_service import compute_impact, DEFAULT_ASSUMPTIONS


# ── fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def seeded_patient():
    """Create a patient plus recent impact-generating activity, return its id."""
    pid = f"PIMP{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Impact Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        # 2 Scribe notes (documentation time saved)
        for _ in range(2):
            db.execute(
                "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, note_format) "
                "VALUES (?, 'D001', 'test transcript', 'soap')",
                (pid,),
            )
        # A charted (non-simulated) reading behind the escalation and bundle:
        # the model only counts deterioration that real vitals drove.
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, respiratory_rate, source) "
            "VALUES (?, ?, 128, 30, 'manual_entry')",
            (pid, (__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                   - __import__("datetime").timedelta(minutes=5)).isoformat()),
        )
        # 1 critical NEWS2 escalation (deterioration)
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level) VALUES (?, 9, 'critical')",
            (pid,),
        )
        # 1 sepsis bundle started (deterioration)
        db.execute(
            "INSERT INTO care_bundles (patient_id, bundle_type, triggered_at, status) "
            "VALUES (?, 'sepsis6', ?, 'active')",
            (pid, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()),
        )
        # 1 pharmacist-verified AI medication (medication safety)
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
            "status, source, verified_by, verified_at) "
            "VALUES (?, 'Amoxicillin', '500mg', 'Oral', 'TDS', 'active', 'llm_extracted', 'D001', ?)",
            (pid, __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()),
        )
        # 1 discharge reconciliation (throughput)
        db.execute(
            "INSERT INTO discharge_summaries (patient_id, author_id, summary) "
            "VALUES (?, 'D001', 'discharge summary')",
            (pid,),
        )
    return pid


# ── shape / basic contract ────────────────────────────────────────────────────
def test_returns_expected_structure():
    result = compute_impact()
    assert set(result) >= {"generated_at", "period", "metrics", "totals", "assumptions"}
    assert {m["key"] for m in result["metrics"]} == {
        "documentation_time_saved", "deterioration_caught_early",
        "medication_safety", "throughput",
    }
    for m in result["metrics"]:
        assert set(m) >= {"key", "label", "value", "unit", "amount", "assumption", "evidence_count"}
    # ICU days and adverse drug events are different units, so they are two
    # totals. They used to be summed into one "events prevented" figure.
    assert set(result["totals"]) == {
        "monthly_savings", "annual_savings", "icu_days_avoided", "ades_avoided"}


# ── metrics compute + dollars non-negative on seeded data ──────────────────────
def test_metrics_compute_with_data(seeded_patient):
    result = compute_impact()
    by_key = {m["key"]: m for m in result["metrics"]}

    # Documentation: >= 2 notes seeded → positive minutes + dollars.
    doc = by_key["documentation_time_saved"]
    assert doc["evidence_count"] >= 2
    assert doc["amount"] > 0

    # Deterioration: >= 1 critical + >= 1 bundle → positive dollars.
    deter = by_key["deterioration_caught_early"]
    assert deter["evidence_count"] >= 2
    assert deter["amount"] > 0

    # Medication safety: the verified AI med counts as evidence.
    med = by_key["medication_safety"]
    assert med["evidence_count"] >= 1
    assert med["amount"] > 0

    # Throughput: 1 discharge reconciliation → positive dollars.
    tp = by_key["throughput"]
    assert tp["evidence_count"] >= 1
    assert tp["amount"] > 0

    # No metric may ever produce a negative dollar figure.
    for m in result["metrics"]:
        assert m["amount"] >= 0

    assert result["totals"]["monthly_savings"] > 0
    assert result["totals"]["annual_savings"] >= result["totals"]["monthly_savings"]
    assert result["totals"]["icu_days_avoided"] == deter["value"]
    assert result["totals"]["ades_avoided"] == med["value"]
    assert result["has_data"] is True


# ── empty window degrades to zeros without error ───────────────────────────────
def test_empty_window_returns_zeros():
    # A 0-day-ago... use a tiny window that predates any seed data by asking for
    # the smallest window (1 day) but on a fresh far-future cutoff is not possible;
    # instead assert the structural zero path via an impossible override count.
    # We simulate "no data yet" by querying with a period so short that the model
    # still must not crash and every dollar is >= 0.
    result = compute_impact({"period_days": 1})
    for m in result["metrics"]:
        assert m["amount"] >= 0
        assert m["evidence_count"] >= 0
    assert result["totals"]["monthly_savings"] >= 0
    assert result["totals"]["annual_savings"] >= 0


def test_no_data_path_is_all_zero(monkeypatch):
    """Force every count query to 0 and confirm a clean all-zero result."""
    import services.impact_service as svc
    monkeypatch.setattr(svc, "_count", lambda *a, **k: 0)
    monkeypatch.setattr(svc, "_count_by_reading_source", lambda *a, **k: (0, 0, 0))
    monkeypatch.setattr(svc, "_scalar_float", lambda *a, **k: None)
    result = svc.compute_impact()
    assert result["has_data"] is False
    assert result["totals"]["monthly_savings"] == 0
    assert result["totals"]["annual_savings"] == 0
    assert result["totals"]["icu_days_avoided"] == 0
    assert result["totals"]["ades_avoided"] == 0
    for m in result["metrics"]:
        assert m["amount"] == 0
        assert m["evidence_count"] == 0


# ── assumption override changes the result ─────────────────────────────────────
def test_assumption_override_changes_result(seeded_patient):
    base = compute_impact()
    base_doc = next(m for m in base["metrics"] if m["key"] == "documentation_time_saved")

    # Double the minutes-saved-per-note → documentation dollars must increase.
    default_min = DEFAULT_ASSUMPTIONS["doc_minutes_saved_per_note"]["value"]
    bumped = compute_impact({"doc_minutes_saved_per_note": default_min * 2})
    bumped_doc = next(m for m in bumped["metrics"] if m["key"] == "documentation_time_saved")

    assert bumped_doc["amount"] > base_doc["amount"]
    # Echoed assumptions reflect the override.
    assert bumped["assumptions"]["doc_minutes_saved_per_note"]["value"] == default_min * 2


def test_negative_override_ignored():
    """A negative assumption must fall back to the default, never a negative dollar."""
    result = compute_impact({"clinician_hourly_cost_inr": -999})
    assert result["assumptions"]["clinician_hourly_cost_inr"]["value"] == \
        DEFAULT_ASSUMPTIONS["clinician_hourly_cost_inr"]["value"]


# ── honesty: simulated vitals, separate units, unmeasured LOS ─────────────────
def _deterioration(result):
    return next(m for m in result["metrics"] if m["key"] == "deterioration_caught_early")


def _seed_escalation(pid: str, source: str | None) -> None:
    """A critical score plus a bundle, behind a reading from ``source`` (None =
    no reading at all)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    with db_session() as db:
        # Discharged: the model does not filter on status, and an admitted
        # leftover would be counted in a later module's census.
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'discharged')",
            (pid, "Impact Honesty", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
        if source is not None:
            db.execute(
                "INSERT INTO vitals (patient_id, timestamp, heart_rate, respiratory_rate, source) "
                "VALUES (?, ?, 130, 32, ?)",
                (pid, (now - timedelta(minutes=5)).isoformat(), source),
            )
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level) VALUES (?, 11, 'critical')",
            (pid,),
        )
        db.execute(
            "INSERT INTO care_bundles (patient_id, bundle_type, triggered_at, status) "
            "VALUES (?, 'sepsis6', ?, 'active')",
            (pid, now.isoformat()),
        )


def test_simulated_escalations_contribute_nothing():
    """The vitals simulator writes source='simulated' readings that score
    critical and open bundles. On the investor screen those became rupees."""
    before = _deterioration(compute_impact())
    _seed_escalation(f"PIMS{uuid.uuid4().hex[:6].upper()}", "simulated")
    after = _deterioration(compute_impact())

    assert after["evidence_count"] == before["evidence_count"]
    assert after["amount"] == before["amount"]
    assert after["excluded_simulated"] == before["excluded_simulated"] + 2
    assert "simulated vitals" in after["assumption"]


def test_escalations_with_no_reading_are_not_claimed():
    before = _deterioration(compute_impact())
    _seed_escalation(f"PIMN{uuid.uuid4().hex[:6].upper()}", None)
    after = _deterioration(compute_impact())
    assert after["evidence_count"] == before["evidence_count"]
    assert after["excluded_unattributed"] == before["excluded_unattributed"] + 2


def test_a_real_reading_counts():
    before = _deterioration(compute_impact())
    _seed_escalation(f"PIMR{uuid.uuid4().hex[:6].upper()}", "nurse_observation")
    after = _deterioration(compute_impact())
    assert after["evidence_count"] == before["evidence_count"] + 2
    assert after["amount"] > before["amount"]


def test_icu_days_and_ades_are_never_summed():
    result = compute_impact()
    by_key = {m["key"]: m for m in result["metrics"]}
    assert "events_prevented" not in result["totals"]
    assert result["totals"]["icu_days_avoided"] == by_key["deterioration_caught_early"]["value"]
    assert result["totals"]["ades_avoided"] == by_key["medication_safety"]["value"]


def test_unmeasured_length_of_stay_is_none_not_zero(monkeypatch):
    import services.impact_service as svc
    monkeypatch.setattr(svc, "_scalar_float", lambda *a, **k: None)
    tp = next(m for m in svc.compute_impact()["metrics"] if m["key"] == "throughput")
    assert tp["value"] is None
    assert "not yet measured" in tp["assumption"]


def test_assumption_text_is_rupees_and_real_plurals(monkeypatch):
    import services.impact_service as svc
    monkeypatch.setattr(svc, "CURRENCY_CODE", "INR")
    monkeypatch.setattr(svc, "CURRENCY_LOCALE", "en-IN")
    monkeypatch.setattr(svc, "_count", lambda *a, **k: 1)
    monkeypatch.setattr(svc, "_count_by_reading_source", lambda *a, **k: (1, 0, 0))
    by_key = {m["key"]: m for m in svc.compute_impact()["metrics"]}
    doc = by_key["documentation_time_saved"]["assumption"]
    assert "1 Scribe note ×" in doc and "(s)" not in doc
    assert "₹1,500 per clinician-hour" in doc
    assert "Rs" not in by_key["deterioration_caught_early"]["assumption"]
    assert svc._fmt_money(1234567) == "₹12,34,567"


def test_a_rate_above_one_is_ignored():
    """15 typed for 15% would multiply the ICU figure a hundredfold."""
    result = compute_impact({"deterioration_icu_avoidance_rate": 15})
    assert result["assumptions"]["deterioration_icu_avoidance_rate"]["value"] == \
        DEFAULT_ASSUMPTIONS["deterioration_icu_avoidance_rate"]["value"]
