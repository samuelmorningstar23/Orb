"""Executive Command Center — house-wide aggregation snapshot.

Covers: census totals + capacity, acuity counts + heatmap, active deteriorations
(critical patient surfaced), the presence/shape of every board section, and the
empty-DB degrade-to-zeros path.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import db_session
from services import command_center_service as cc
from services.clinical_time import format_utc
# TOTAL_CAPACITY is deliberately NOT imported here: it is an int, so importing
# it binds a pre-discovery COPY, and every assertion written against that copy
# agrees with itself while the screen disagrees. Assert the invariant (total ==
# sum of parts) instead. WARD_CAPACITY is a dict, shared by reference, and stays
# live — which is why it is safe to import and TOTAL_CAPACITY is not.
from services.forecast_service import WARD_CAPACITY


_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: this module seeds status='admitted' explicitly (the
    census is what it asserts on), so every patient `_mk_patient` created used
    to stay admitted in the shared session DB — 3 by the end of the module.
    That is not a cosmetic leak. The FHIR self-export in
    services/downtime_service.py iterates `patients WHERE status='admitted'`
    and sleeps _PATIENT_SLEEP_S (250 ms) BETWEEN PATIENTS, so each leftover
    adds ~0.275 s to every export call in tests/test_downtime_fhir_export.py
    and to everything downstream of it.

    The seeding happens in a plain helper rather than a fixture, so the helper
    registers each id here and this autouse teardown discharges the batch. Do
    not DELETE — the risk_scores rows written during the test legitimately
    reference these ids. Teardown runs AFTER the test, so the within-test
    census assertions still see their own patients."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _SEEDED],
        )
    _SEEDED.clear()


# A full observation set for each band (HR, SBP, SpO2, temp, RR). Since
# 2026-10-06 the Command Center bands from the latest VITALS row, the Vigil
# board's rule, so the seed writes vitals, not risk_scores.
_VITALS_FOR = {
    "critical": (135, 85, 88, 39.5, 30),    # NEWS2 14
    "elevated": (115, 105, 96, 37.0, 22),   # NEWS2 5
    "stable": (72, 124, 98, 36.8, 16),      # NEWS2 0
}


def _mk_patient(ward: str, level: str | None, bed: str, risk_level: str | None = None) -> str:
    """Seed one admitted patient in `ward` whose latest vitals score `level`
    (None: no vitals at all). `risk_level` also writes a risk_scores row, which
    the Command Center must now ignore."""
    pid = f"CCP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, status) "
            "VALUES (?, ?, ?, ?, ?, 'admitted')",
            (pid, f"CC {(level or 'none').title()}", f"MRN-{uuid.uuid4().hex[:8]}", ward, bed),
        )
        if level is not None:
            hr, sbp, spo2, temp, rr = _VITALS_FOR[level]
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                       spo2, temperature, respiratory_rate, consciousness, on_oxygen)
                   VALUES (?, ?, ?, ?, 70, ?, ?, ?, 'alert', 0)""",
                (pid, format_utc(datetime.now(timezone.utc) - timedelta(minutes=5)),
                 hr, sbp, spo2, temp, rr),
            )
        if risk_level is not None:
            db.execute(
                "INSERT INTO risk_scores (patient_id, score, level) VALUES (?, ?, ?)",
                (pid, 0.9 if risk_level == "critical" else 0.2, risk_level),
            )
    _SEEDED.append(pid)
    return pid


def test_snapshot_census_and_acuity_aggregate():
    _mk_patient("ICU", "critical", f"ICU-{uuid.uuid4().hex[:3]}")
    _mk_patient("W2", "stable", f"W2-{uuid.uuid4().hex[:3]}")

    snap = cc.house_snapshot()

    # Census total must equal the true admitted count in the DB.
    with db_session() as db:
        admitted = db.execute(
            "SELECT COUNT(*) AS c FROM patients WHERE status = 'admitted'"
        ).fetchone()["c"]
    assert admitted >= 2
    assert snap["census"]["total_admitted"] == admitted
    # THE INVARIANT IS "the whole equals the sum of its parts", NOT "the whole
    # equals a number imported at module load". `TOTAL_CAPACITY` is an int, so
    # `from forecast_service import TOTAL_CAPACITY` binds a COPY taken before
    # startup ward discovery runs, while the WARD_CAPACITY dict beside it is
    # shared by reference and keeps growing as wards appear. That is the exact
    # defect this file's own history records — the service was fixed to sum the
    # live dict, but this assertion kept comparing against the stale copy, so it
    # passed alone and failed the moment a sibling suite admitted a patient into
    # a new ward (72 vs 48). Suite ORDER decided the verdict, which makes it a
    # test of nothing.
    assert snap["census"]["total_capacity"] == sum(
        w["capacity"] for w in snap["census"]["by_ward"]
    )

    # by_ward reuses the SAME capacity source as the Forecast pillar.
    by_ward = {w["ward"]: w for w in snap["census"]["by_ward"]}
    assert set(by_ward) == set(WARD_CAPACITY)
    for w, cap in WARD_CAPACITY.items():
        assert by_ward[w]["capacity"] == cap

    # Acuity buckets partition the census exactly.
    ac = snap["acuity"]
    assert ac["critical"] + ac["elevated"] + ac["stable"] + ac["not_scored"] == admitted
    assert ac["critical"] >= 1

    # Heatmap present: one row per canonical ward; ICU shows the critical patient.
    assert {h["ward"] for h in ac["heatmap"]} == set(WARD_CAPACITY)
    icu = next(h for h in ac["heatmap"] if h["ward"] == "ICU")
    assert icu["critical"] >= 1


def test_deteriorations_lists_critical_patient():
    pid = _mk_patient("ICU", "critical", f"ICU-{uuid.uuid4().hex[:3]}")

    snap = cc.house_snapshot()
    ids = [d["patient_id"] for d in snap["deteriorations"]]
    assert pid in ids

    row = next(d for d in snap["deteriorations"] if d["patient_id"] == pid)
    assert row["level"] == "critical"
    assert "news2" in row  # key always present (value may be None with no vitals)

    # Critical patients sort ahead of elevated ones.
    levels = [d["level"] for d in snap["deteriorations"]]
    if "critical" in levels and "elevated" in levels:
        assert levels.index("critical") < levels.index("elevated")


def test_all_board_sections_present():
    snap = cc.house_snapshot()
    for key in (
        "generated_at", "census", "acuity", "deteriorations",
        "bundles", "forecast", "downtime_readiness", "or_today",
    ):
        assert key in snap, f"missing section: {key}"

    assert {"active", "compliant", "compliance_pct"} <= set(snap["bundles"])
    assert {"patients_with_recent_snapshot", "total", "pct"} <= set(snap["downtime_readiness"])
    assert {"projected_admissions_24h", "projected_occupancy"} <= set(snap["forecast"])
    assert "count" in snap["or_today"] and "cases" in snap["or_today"]


def test_empty_db_path_returns_zeros_without_crashing():
    # The aggregation must degrade to sensible zeros when there are no patients.
    census, acuity = cc._census_and_acuity([])
    assert census["total_admitted"] == 0
    assert census["occupancy_pct"] == 0.0
    assert all(w["occupied"] == 0 for w in census["by_ward"])
    assert len(census["by_ward"]) == len(WARD_CAPACITY)

    assert acuity["critical"] == acuity["elevated"] == acuity["stable"] == 0
    assert len(acuity["heatmap"]) == len(WARD_CAPACITY)
    assert all(
        h["critical"] == 0 and h["elevated"] == 0 and h["stable"] == 0
        for h in acuity["heatmap"]
    )

    assert cc._deteriorations([]) == []

    dr = cc._downtime_readiness([])
    assert dr["total"] == 0
    assert dr["pct"] == 0.0
    assert dr["oldest_snapshot_age_min"] is None

    # Section fallbacks used when a subsystem raises.
    assert cc._empty_census()["total_admitted"] == 0
    assert cc._empty_acuity()["critical"] == 0


def test_capacity_is_internally_consistent_after_ward_discovery():
    """The header total must equal the sum of the per-ward capacities beside it.

    Regression for a divergence the existing assertions could not express: they
    compared the API against the imported `TOTAL_CAPACITY` constant, and BOTH
    were the stale pre-discovery value, so they agreed while the screen did not.
    The Command Center header read `30/48` (the four-ward fallback) at the same
    moment the capacity-forecast tile below it read `34/72` (the six real
    wards), because `TOTAL_CAPACITY` is an int and had been imported by value
    before `refresh_ward_capacity()` ran.

    This asserts the invariant instead of the constant: whatever the wards turn
    out to be, the total and the parts must agree.
    """
    from services.forecast_service import refresh_ward_capacity

    _mk_patient("ICU", "critical", f"ICU-{uuid.uuid4().hex[:3]}")
    refresh_ward_capacity()  # simulate startup discovery AFTER import time

    snap = cc.house_snapshot()
    census = snap["census"]

    assert census["total_capacity"] == sum(w["capacity"] for w in census["by_ward"]), (
        "census header total disagrees with the per-ward capacities it is built from"
    )
    # And the forecast pillar, which reads the same source, must agree with it.
    cap = snap.get("capacity_forecast") or {}
    if cap.get("total_capacity") is not None:
        assert cap["total_capacity"] == census["total_capacity"], (
            "capacity forecast total disagrees with the census total"
        )


def test_bands_and_order_come_from_the_latest_vitals_like_the_vigil_board():
    """The highest-scores list and the acuity map band each patient from the
    NEWS2 of their latest vitals and list them in vigil_service.board_order, so
    the Command Center and the Vigil board cannot disagree (2026-10-06)."""
    from services.vigil_service import board_order

    # Risk rows that contradict the vitals: the old source, now ignored.
    crit = _mk_patient("ICU", "critical", f"ICU-{uuid.uuid4().hex[:3]}", risk_level="stable")
    calm = _mk_patient("W2", "stable", f"W2-{uuid.uuid4().hex[:3]}", risk_level="critical")
    elev = _mk_patient("W2", "elevated", f"W2-{uuid.uuid4().hex[:3]}")
    blank = _mk_patient("W2", None, f"W2-{uuid.uuid4().hex[:3]}", risk_level="critical")

    patients = cc._admitted_with_acuity()
    by_id = {p["id"]: p for p in patients}
    assert by_id[crit]["level"] == "critical" and by_id[crit]["news2"] == 14
    assert by_id[calm]["level"] == "stable" and by_id[calm]["news2"] == 0
    assert by_id[elev]["level"] == "elevated"
    # Nothing recorded is not scored, never stable, whatever risk_scores says.
    assert by_id[blank]["level"] is None and by_id[blank]["news2"] is None

    # The whole list is in the board's order.
    keys = [board_order(p["id"], p["news2_result"]) for p in patients]
    assert keys == sorted(keys)

    snap = cc.house_snapshot()
    det = [d["patient_id"] for d in snap["deteriorations"]]
    assert crit in det and elev in det
    assert calm not in det and blank not in det
    assert det.index(crit) < det.index(elev)
    row = next(d for d in snap["deteriorations"] if d["patient_id"] == crit)
    assert row["news2"] == 14
    assert snap["acuity"]["not_scored"] >= 1


def test_a_set_entered_in_error_does_not_band_a_patient():
    """The old highest-scores lane skipped a vitals set marked entered in error
    (voided_at IS NULL); the 2026-10-06 rewrite read the newest row whatever
    its state, so a voided peri-arrest set kept a patient critical. The board
    and every other vitals reader skip it."""
    pid = _mk_patient("W2", "stable", f"W2-{uuid.uuid4().hex[:3]}")
    later = format_utc(datetime.now(timezone.utc) - timedelta(minutes=1))
    hr, sbp, spo2, temp, rr = _VITALS_FOR["critical"]
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                   spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                   voided_at, voided_by, void_reason)
               VALUES (?, ?, ?, ?, 70, ?, ?, ?, 'alert', 0, ?, 'CC-TEST', 'Wrong patient')""",
            (pid, later, hr, sbp, spo2, temp, rr, later),
        )
    by_id = {p["id"]: p for p in cc._admitted_with_acuity()}
    assert by_id[pid]["level"] == "stable" and by_id[pid]["news2"] == 0
    assert pid not in [d["patient_id"] for d in cc.house_snapshot()["deteriorations"]]


def test_one_patient_that_cannot_be_scored_does_not_blank_the_house(monkeypatch):
    """Scoring moved into the base read, outside the old per-patient guard, so
    one row that raised emptied the census, the acuity map and the list. That
    patient is now counted not scored and the rest still show."""
    import services.vigil_service as vs

    bad = _mk_patient("W2", "critical", f"W2-{uuid.uuid4().hex[:3]}")
    good = _mk_patient("ICU", "critical", f"ICU-{uuid.uuid4().hex[:3]}")
    real = vs.score_vitals_row

    def flaky(vitals, *, patient_id=None, **kw):
        if patient_id == bad:
            raise ValueError("unreadable row")
        return real(vitals, patient_id=patient_id, **kw)

    monkeypatch.setattr(vs, "score_vitals_row", flaky)
    snap = cc.house_snapshot()
    assert "patients_error" not in snap
    det = [d["patient_id"] for d in snap["deteriorations"]]
    assert good in det and bad not in det
    assert snap["acuity"]["not_scored"] >= 1
