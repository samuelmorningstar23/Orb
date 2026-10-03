"""Orb — the NABH accreditation evidence pack (services/nabh_service.py +
routes/nabh_routes.py).

WHAT THESE TESTS GUARD, in the order the hospital is harmed:

  1. THE HONESTY RULE. An indicator with too little behind it must render as
     INSUFFICIENT DATA or NOT COLLECTED — never as 0, never as 100 %. A pack that
     flatters the hospital is worse than no pack: a quality committee that reads
     "0 medication errors" stops looking for medication errors, and an assessor
     who catches one invented number stops believing the other eight. Every
     shape of this failure has its own test, and the guard is proven able to
     fail: `test_PROVE_CAN_FAIL_*` reproduce the exact defect (report the
     arithmetic anyway) and assert the wrong, flattering number those defects
     produce, so the tests are checking behaviour and not merely tautologies.

  2. THE ARITHMETIC. Every computable indicator is exercised against seeded
     fixtures with a HAND-COMPUTED expected value written in the test beside the
     assertion. A dashboard whose numbers nobody has checked by hand is a
     dashboard that will be wrong in a direction nobody notices.

  3. THE ANONYMITY PROMISE. An anonymous report must store no reporter id and no
     reporter role — in the incident row, in the transition log, or in this
     module's audit event — and the database must refuse the write even when the
     service is bypassed. Tested at all four levels, because a register a ward
     believes is attributable is a register that records nothing.

  4. THE LIFECYCLE. A closure with no investigation, a closure with an open
     corrective action, and a closure with neither an action nor a stated reason
     are the three findings an assessor makes on a paper register. All three are
     refusals here.

  5. THE WINDOW. Timestamps are compared through `datetime()`, not as text —
     `'2025-03-01 10:00:00' >= '2025-03-01T00:00:00+00:00'` is FALSE as text, so
     a raw comparison silently drops the first day of every period.

ISOLATION NOTE. The suite shares one DB across modules, and this module's
denominators (inpatient-days, administrations, discharges) would otherwise be
polluted by every patient any other test file ever created. Every fixture below
therefore seeds its OWN unique ward and every indicator is computed with that
ward as the filter, over a window in 2025 that no other module writes into.
"""
import hashlib
import itertools
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402
from auth import hash_password  # noqa: E402
from database import db_session, get_db  # noqa: E402
from db_backend import DBIntegrityError  # noqa: E402
from main import app  # noqa: E402
from services import nabh_service as svc  # noqa: E402

client = TestClient(app)


# ═════════════════════════════════════════════════════════════════════════════
# Fixtures — every one seeds its own ward, so the denominators are exact
# ═════════════════════════════════════════════════════════════════════════════

#: A window entirely in the past, 40 days long. 2025-03-01 .. 2025-04-09
#: inclusive resolves to [2025-03-01T00:00, 2025-04-10T00:00) — see
#: `resolve_period`, which makes the end EXCLUSIVE at the following midnight so
#: the last day of a quarter is inside the pack.
P_START = "2025-03-01"
P_END = "2025-04-09"
WINDOW_DAYS = 40

_WARDS = itertools.count(1)
_IDS = itertools.count(1)


def _ward() -> str:
    return f"NABH-TEST-W{next(_WARDS):03d}"


def _uid(prefix: str) -> str:
    return f"{prefix}-{next(_IDS):05d}"


def _admin_user(user_id: str = "NABH-ADM", department: str | None = None) -> str:
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, 'admin', ?, ?)",
            (user_id, "Quality Manager", department, hash_password("password")),
        )
    return user_id


def _nurse_user(user_id: str = "NABH-NUR") -> str:
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, 'nurse', NULL, ?)",
            (user_id, "Staff Nurse", hash_password("password")),
        )
    return user_id


def _login(user_id: str) -> dict:
    resp = client.post("/api/auth/login",
                       json={"employee_id": user_id, "password": "password"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


#: The exclusive end of the window — see `resolve_period`. A fixture stay that
#: ends exactly here covers every hour of the period and is counted in NO
#: in-window discharge query, which is what lets bed-days be exact while the
#: discharge-event denominators stay under each test's own control.
WINDOW_END_EXCLUSIVE = "2025-04-10T00:00:00+00:00"


def _seed_patients(ward: str, n: int, *, admitted_on: str = "2025-02-01") -> list[str]:
    """`n` patients on `ward`, admitted BEFORE the window and discharged AT its
    exclusive end. Each contributes exactly WINDOW_DAYS inpatient-days, so
    `n * 40` is a denominator that can be checked by hand.

    THEY ARE DISCHARGED, NOT ADMITTED, AND THAT MATTERS TO OTHER TEST MODULES.
    The suite shares one DB. Leaving a hundred `status='admitted'` fixture
    patients behind changes the hospital-wide census that
    tests/test_command_center.py asserts on — verified: it went red before this
    fixture was changed. A test file that has to be run alone is a test file
    that will be run alone once and then forgotten.
    """
    ids = []
    with db_session() as db:
        for _ in range(n):
            pid = _uid("NABHPT")
            db.execute(
                "INSERT INTO patients (id, name, mrn, ward, bed, admission_date, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'discharged')",
                (pid, "NABH Fixture Patient", f"MRN-{pid}", ward, "1",
                 f"{admitted_on}T00:00:00+00:00"),
            )
            db.execute(
                "INSERT INTO discharge_summaries (patient_id, author_id, summary, "
                "discharged_at) VALUES (?, 'system', 'fixture', ?)",
                (pid, WINDOW_END_EXCLUSIVE),
            )
            ids.append(pid)
    return ids


def _seed_incident(*, ward: str, incident_type: str = "medication_error",
                   severity: str = "no_harm", occurred_at: str = "2025-03-05T09:00:00+00:00",
                   reported_at: str | None = None, status: str = "reported",
                   closed_at: str | None = None, patient_id: str | None = None,
                   is_anonymous: int = 0, reported_by: str | None = None) -> int:
    """Insert a register row directly.

    Direct SQL rather than `report_incident` because the indicators key on
    `reported_at` and `occurred_at`, and the service (correctly) stamps
    `reported_at` with the clock. A closure-target indicator cannot be tested
    through an API that can only ever say "reported just now".
    """
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO nabh_incidents (reference, incident_type, severity, title, "
            "description, occurred_at, reported_at, ward, patient_id, is_anonymous, "
            "reported_by, status, closed_at, investigation_summary, created_at, updated_at) "
            "VALUES (?, ?, ?, 'fixture', 'fixture', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_uid("INCFX"), incident_type, severity, occurred_at,
             reported_at or occurred_at, ward, patient_id, is_anonymous, reported_by,
             status, closed_at, "fixture investigation" if status == "closed" else None,
             occurred_at, occurred_at),
        )
        return int(cur.lastrowid)


@pytest.fixture()
def quality_admin():
    return _admin_user()


# ═════════════════════════════════════════════════════════════════════════════
# 1. The empty database
# ═════════════════════════════════════════════════════════════════════════════

def test_an_empty_period_yields_a_complete_explicitly_empty_dashboard():
    """A hospital that switched Orb on this morning gets nine indicators, every
    one saying what it is missing — never an exception, and never a page of
    zeroes."""
    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=_ward())

    assert len(dash["indicators"]) == len(svc.INDICATOR_IDS) == 9
    assert dash["summary"]["reported"] == 0
    assert dash["summary"]["not_collected"] == 9
    for ind in dash["indicators"]:
        assert ind["state"] == "not_collected", ind["id"]
        assert ind["value"] is None, ind["id"]
        assert ind["display"] is None, ind["id"]
        # The reason is on the indicator, not in a log nobody reads.
        assert ind["blockers"], ind["id"]


def test_the_period_end_day_is_inside_the_window():
    """An inclusive-looking end date that resolves to an exclusive midnight is
    how the last day of every quarter goes missing."""
    start, end = svc.resolve_period("2025-03-01", "2025-03-31")
    assert start.isoformat() == "2025-03-01T00:00:00+00:00"
    assert end.isoformat() == "2025-04-01T00:00:00+00:00"


def test_a_timestamp_written_in_the_legacy_format_on_day_one_is_not_dropped():
    """The text-comparison bug, pinned. `'2025-03-01 10:00:00'` (the format
    CURRENT_TIMESTAMP writes) sorts BEFORE `'2025-03-01T00:00:00+00:00'` as
    text, so a raw >= comparison loses the first day of every period."""
    ward = _ward()
    patient = _seed_patients(ward, 1)[0]
    with db_session() as db:
        # Written exactly the way CURRENT_TIMESTAMP writes it.
        db.execute(
            "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, "
            "structured_note, note_format, status, created_at) "
            "VALUES (?, 'system', 'x', 'y', 'handoff', 'signed', '2025-03-01 10:00:00')",
            (patient,),
        )
    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    handoff = next(i for i in dash["indicators"] if i["id"] == "handoff_completion")
    assert handoff["denominator"] == 1, (
        "a handoff stamped on the first day of the period was dropped — the "
        "window comparison is being done on raw text again"
    )


# ═════════════════════════════════════════════════════════════════════════════
# 2. Indicator arithmetic, hand-checked
# ═════════════════════════════════════════════════════════════════════════════

def test_medication_error_and_fall_rates_are_per_1000_inpatient_days():
    """HAND-COMPUTED. 5 patients admitted before the window and never
    discharged, over a 40-day period → 5 x 40 = 200 inpatient-days.
    4 medication errors → 1000 x 4 / 200 = 20.0 per 1,000.
    2 falls           → 1000 x 2 / 200 = 10.0 per 1,000.
    """
    ward = _ward()
    _seed_patients(ward, 5)
    for _ in range(4):
        _seed_incident(ward=ward, incident_type="medication_error")
    for _ in range(2):
        _seed_incident(ward=ward, incident_type="patient_fall")

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    assert dash["denominators"]["inpatient_days"]["inpatient_days"] == 200.0

    med = next(i for i in dash["indicators"] if i["id"] == "medication_error_rate")
    assert med["state"] == "reported"
    assert med["numerator"] == 4
    assert med["denominator"] == 200.0
    assert med["value"] == 20.0
    assert med["display"] == "20.00 per 1,000 inpatient-days"

    fall = next(i for i in dash["indicators"] if i["id"] == "patient_fall_rate")
    assert fall["state"] == "reported"
    assert fall["value"] == 10.0


def test_a_voided_report_leaves_the_register_but_leaves_the_numerator():
    """A duplicate report must not inflate the rate, and must not vanish."""
    ward = _ward()
    _seed_patients(ward, 5)
    for _ in range(4):
        _seed_incident(ward=ward, incident_type="medication_error")
    voided = _seed_incident(ward=ward, incident_type="medication_error", status="void")

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    med = next(i for i in dash["indicators"] if i["id"] == "medication_error_rate")
    assert med["numerator"] == 4, "a voided report was counted"

    listing = svc.list_incidents(period_start=P_START, period_end=P_END, ward=ward,
                                 include_void=True)
    assert voided in [i["id"] for i in listing["incidents"]], "a voided report vanished"


def test_medication_override_rate_is_per_1000_administrations():
    """HAND-COMPUTED. 250 administrations marked 'given', 2 dose-guard overrides
    and 1 allergy-interlock override → 1000 x 3 / 250 = 12.0 per 1,000."""
    ward = _ward()
    patient = _seed_patients(ward, 1)[0]
    nurse = _nurse_user()
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
            "status, started_at) VALUES (?, 'Fixture', '1 mg', 'Oral', 'OD', 'active', ?)",
            (patient, "2025-02-01T00:00:00+00:00"),
        )
        med_id = int(cur.lastrowid)
        for i in range(250):
            db.execute(
                "INSERT INTO medication_administrations (medication_id, patient_id, "
                "administered_by, status, administered_at) VALUES (?, ?, ?, 'given', ?)",
                (med_id, patient, nurse, f"2025-03-{(i % 28) + 1:02d} 08:00:00"),
            )
        # A 'not_given' row must NOT be in the denominator: the indicator counts
        # doses that reached a patient.
        db.execute(
            "INSERT INTO medication_administrations (medication_id, patient_id, "
            "administered_by, status, not_given_reason, administered_at) "
            "VALUES (?, ?, ?, 'not_given', 'refused', '2025-03-05 08:00:00')",
            (med_id, patient, nurse),
        )
        for _ in range(2):
            db.execute(
                "INSERT INTO medication_dose_overrides (medication_id, patient_id, "
                "reason, overridden_by, overridden_at) VALUES (?, ?, 'clinical', ?, ?)",
                (med_id, patient, nurse, "2025-03-05 09:00:00"),
            )
        db.execute(
            "INSERT INTO medication_allergy_overrides (medication_id, patient_id, "
            "reason, overridden_by, overridden_at) VALUES (?, ?, 'tolerated', ?, ?)",
            (med_id, patient, nurse, "2025-03-06 09:00:00"),
        )

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    ind = next(i for i in dash["indicators"] if i["id"] == "medication_override_rate")
    assert ind["state"] == "reported"
    assert ind["denominator"] == 250, "a 'not_given' row leaked into the denominator"
    assert ind["numerator"] == 3
    assert ind["value"] == 12.0
    assert ind["detail"]["dose_guard_overrides"] == 2
    assert ind["detail"]["allergy_interlock_overrides"] == 1
    # The one indicator where a real zero is a finding, so it is labelled a proxy
    # and says why in its own caveat rather than in a design document.
    assert ind["is_proxy"] is True


def test_handoff_completion_counts_a_superseded_note_as_completed():
    """HAND-COMPUTED. 25 handoffs drafted: 18 signed, 2 superseded (signed, then
    corrected by a newer signed version), 5 still draft → 20 / 25 = 80.0 %.

    A superseded note counts as complete because penalising a ward for
    CORRECTING a handoff is the opposite of what the indicator should reward.
    """
    ward = _ward()
    patient = _seed_patients(ward, 1)[0]
    with db_session() as db:
        for status, count in (("signed", 18), ("superseded", 2), ("draft", 5)):
            for _ in range(count):
                db.execute(
                    "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, "
                    "structured_note, note_format, status, created_at) "
                    "VALUES (?, 'system', 'x', 'y', 'handoff', ?, '2025-03-10 07:00:00')",
                    (patient, status),
                )

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    ind = next(i for i in dash["indicators"] if i["id"] == "handoff_completion")
    assert ind["state"] == "reported"
    assert ind["numerator"] == 20
    assert ind["denominator"] == 25
    assert ind["value"] == 80.0
    assert ind["display"] == "80.0%"


def test_escalation_acknowledgement_excludes_chains_still_inside_the_window():
    """HAND-COMPUTED. 12 chains raised inside the period:
         8 acknowledged 5 minutes later   → in time
         2 acknowledged 40 minutes later  → late
         2 never acknowledged, window long closed → decidable failures
       → 8 / 12 = 66.67 %.
    Plus 1 chain raised MOMENTS AGO, which is right-censored: it has not yet
    missed its window and counting it as a miss would make the indicator a
    function of when the pack was generated.
    """
    ward = _ward()
    # One patient per chain: `clinical_escalations` carries a partial UNIQUE
    # index on patient_id for any chain that is not resolved (migration 11), so
    # a ward cannot have two live chains on one patient.
    patients = _seed_patients(ward, 13)
    nurse = _nurse_user()
    raised = datetime(2025, 3, 12, 8, 0, tzinfo=timezone.utc)

    with db_session() as db:
        def chain(offset_minutes: int | None, seq: int) -> None:
            acked = (raised + timedelta(minutes=offset_minutes)
                     if offset_minutes is not None else None)
            db.execute(
                "INSERT INTO clinical_escalations (id, patient_id, status, trigger, "
                "raised_at, acknowledged_at, acknowledged_by) VALUES (?, ?, ?, 'news2', ?, ?, ?)",
                (f"{ward}-ESC-{seq}", patients[seq - 1],
                 "acknowledged" if acked else "open",
                 raised.isoformat(),
                 acked.isoformat() if acked else None,
                 nurse if acked else None),
            )

        seq = 0
        for _ in range(8):
            seq += 1
            chain(5, seq)
        for _ in range(2):
            seq += 1
            chain(40, seq)
        for _ in range(2):
            seq += 1
            chain(None, seq)

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    ind = next(i for i in dash["indicators"] if i["id"] == "escalation_ack_compliance")
    assert ind["state"] == "reported"
    assert ind["numerator"] == 8
    assert ind["denominator"] == 12
    assert ind["value"] == 66.67
    assert ind["excluded"]["still_inside_response_window"] == 0

    # Now a chain raised seconds ago, in a window that runs to today.
    now = datetime.now(timezone.utc)
    with db_session() as db:
        db.execute(
            "INSERT INTO clinical_escalations (id, patient_id, status, trigger, raised_at) "
            "VALUES (?, ?, 'open', 'news2', ?)",
            (f"{ward}-ESC-CENSORED", patients[12], now.isoformat()),
        )
    live = svc.indicators(period_start="2025-03-01",
                          period_end=now.date().isoformat(), ward=ward)
    live_ind = next(i for i in live["indicators"] if i["id"] == "escalation_ack_compliance")
    assert live_ind["excluded"]["still_inside_response_window"] == 1
    assert live_ind["denominator"] == 12, (
        "a chain that has not yet run out of time was counted as a miss"
    )


def _seed_dispositions(ward: str, spec: dict[str, int]) -> None:
    """One disposition row per discharge event, each on its own patient."""
    total = sum(spec.values())
    patients = _seed_patients(ward, total)
    idx = 0
    for disposition, count in spec.items():
        for _ in range(count):
            svc.record_disposition(
                patient_id=patients[idx], disposition=disposition,
                occurred_at=f"2025-03-{(idx % 28) + 1:02d}T12:00:00+00:00",
                recorded_by="NABH-ADM")
            idx += 1


def test_mortality_and_lama_are_computed_from_recorded_dispositions():
    """HAND-COMPUTED. 25 recorded dispositions: 2 death + 1 death-within-48h,
    3 LAMA + 1 DAMA, 1 absconded, 17 routine.
        mortality = 3 / 25 = 12.0 %
        LAMA+DAMA = 4 / 25 = 16.0 %
    Absconding is REPORTED BESIDE the AMA rate, never folded into it: it is a
    different failure and folding it in moves the number in a direction nobody
    chose.
    """
    ward = _ward()
    _seed_dispositions(ward, {"death": 2, "death_within_48h": 1, "lama": 3,
                              "dama": 1, "absconded": 1, "routine": 17})

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    mort = next(i for i in dash["indicators"] if i["id"] == "inpatient_mortality")
    assert mort["state"] == "reported"
    assert (mort["numerator"], mort["denominator"]) == (3, 25)
    assert mort["value"] == 12.0

    ama = next(i for i in dash["indicators"] if i["id"] == "against_medical_advice_rate")
    assert ama["state"] == "reported"
    assert (ama["numerator"], ama["denominator"]) == (4, 25)
    assert ama["value"] == 16.0
    assert ama["detail"]["absconded_reported_separately"] == 1


def test_readmission_excludes_discharges_whose_follow_up_has_not_elapsed():
    """HAND-COMPUTED. 25 discharges inside the period, all more than 7 days ago:
       5 followed by a new admission 3 days later  → re-admitted
       3 followed by one 19 days later             → outside the window
      17 with no return
    → 5 / 25 = 20.0 %.
    """
    ward = _ward()
    patients = _seed_patients(ward, 25)
    _admin_user()
    with db_session() as db:
        for pid in patients:
            db.execute(
                "INSERT INTO discharge_summaries (patient_id, author_id, summary, "
                "discharged_at) VALUES (?, 'NABH-ADM', 'fixture', '2025-03-05 10:00:00')",
                (pid,),
            )
        for pid in patients[:5]:
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, ward, status, created_at) "
                "VALUES (?, ?, 'NABH-ADM', ?, 'active', '2025-03-08 09:00:00')",
                (_uid("NABHADM"), pid, ward),
            )
        for pid in patients[5:8]:
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, ward, status, created_at) "
                "VALUES (?, ?, 'NABH-ADM', ?, 'active', '2025-03-24 09:00:00')",
                (_uid("NABHADM"), pid, ward),
            )

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    ind = next(i for i in dash["indicators"] if i["id"] == "readmission_7_day")
    assert ind["state"] == "reported"
    assert (ind["numerator"], ind["denominator"]) == (5, 25)
    assert ind["value"] == 20.0
    assert ind["excluded"]["follow_up_not_elapsed"] == 0


def test_a_discharge_inside_its_follow_up_window_is_censored_not_counted_as_clean():
    """The end-of-period optimism bug. A patient discharged yesterday cannot yet
    be known not to have come back; counting them as 'not re-admitted' makes
    every month look better on the day it closes."""
    ward = _ward()
    patients = _seed_patients(ward, 25)
    _admin_user()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1))
    with db_session() as db:
        for pid in patients:
            db.execute(
                "INSERT INTO discharge_summaries (patient_id, author_id, summary, "
                "discharged_at) VALUES (?, 'NABH-ADM', 'fixture', ?)",
                (pid, yesterday.isoformat()),
            )

    # A window that opens AFTER the fixture's own stay-closing discharge, so the
    # only discharges inside it are yesterday's — every one of them censored.
    recent_start = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    dash = svc.indicators(period_start=recent_start,
                          period_end=datetime.now(timezone.utc).date().isoformat(),
                          ward=ward)
    ind = next(i for i in dash["indicators"] if i["id"] == "readmission_7_day")
    assert ind["excluded"]["follow_up_not_elapsed"] == 25
    assert ind["denominator"] == 0
    assert ind["state"] == "insufficient_data"
    assert ind["value"] is None
    assert ind["display"] is None


def test_incident_closure_uses_only_incidents_whose_target_has_elapsed():
    """HAND-COMPUTED. 20 incidents reported 2025-03-01, all long past the 30-day
    target: 12 closed within 30 days, 4 closed after, 4 still open →
    12 / 20 = 60.0 %."""
    ward = _ward()
    for _ in range(12):
        _seed_incident(ward=ward, status="closed",
                       reported_at="2025-03-01T09:00:00+00:00",
                       closed_at="2025-03-10T09:00:00+00:00")
    for _ in range(4):
        _seed_incident(ward=ward, status="closed",
                       reported_at="2025-03-01T09:00:00+00:00",
                       closed_at="2025-05-01T09:00:00+00:00")
    for _ in range(4):
        _seed_incident(ward=ward, status="reported",
                       reported_at="2025-03-01T09:00:00+00:00")

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    ind = next(i for i in dash["indicators"] if i["id"] == "incident_closure_30d")
    assert ind["state"] == "reported"
    assert (ind["numerator"], ind["denominator"]) == (12, 20)
    assert ind["value"] == 60.0
    assert ind["detail"]["open_past_target"] == 4


# ═════════════════════════════════════════════════════════════════════════════
# 3. THE HONESTY RULE — and proof each guard can fail
# ═════════════════════════════════════════════════════════════════════════════

def test_an_unused_register_reads_NOT_COLLECTED_not_zero_errors():
    """The worst number this module could print. A ward with 200 inpatient-days
    and an empty register has NOT got a zero medication-error rate; it has got
    an unused register, and the two require opposite responses."""
    ward = _ward()
    _seed_patients(ward, 5)

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    ind = next(i for i in dash["indicators"] if i["id"] == "medication_error_rate")
    assert ind["state"] == "not_collected"
    assert ind["value"] is None
    assert ind["display"] is None
    assert "statement about the register" in ind["blockers"][0]


def test_PROVE_CAN_FAIL_an_unused_register_would_otherwise_print_zero():
    """The defect, reproduced. Remove the `_register_in_use` gate — i.e. let the
    indicator compute its arithmetic regardless — and the SAME fixture yields
    `0.00 per 1,000 inpatient-days`, a clean bill of health for a ward that has
    reported nothing at all. This test asserts the WRONG number the defect
    produces, so the guard above is checking behaviour and not itself."""
    ward = _ward()
    _seed_patients(ward, 5)

    with db_session() as db:
        usage = svc._register_in_use(db, *svc.resolve_period(P_START, P_END), ward)
        bed_days = svc._inpatient_days(db, *svc.resolve_period(P_START, P_END), ward)
        assert usage["in_use"] is False
        assert bed_days["inpatient_days"] == 200.0
        # The defect: claim the register is in use.
        defective = svc._register_rate_indicator(
            "medication_error_rate", "medication_error", db,
            *svc.resolve_period(P_START, P_END), ward,
            {**usage, "in_use": True}, bed_days)

    assert defective["state"] == "reported"
    assert defective["value"] == 0.0
    assert defective["display"] == "0.00 per 1,000 inpatient-days", (
        "this is the flattering number the gate exists to prevent"
    )


def test_a_partly_kept_disposition_register_never_prints_a_mortality_rate():
    """COVERAGE GATE. 4 dispositions recorded — one of them a death — against 20
    discharge events Orb actually observed. The arithmetic is 25 % mortality.
    Printing that would be a catastrophe on a page an assessor reads, so it does
    not print: the state is INSUFFICIENT DATA and the coverage is stated."""
    ward = _ward()
    patients = _seed_patients(ward, 20)
    _admin_user()
    with db_session() as db:
        for pid in patients:
            db.execute(
                "INSERT INTO discharge_summaries (patient_id, author_id, summary, "
                "discharged_at) VALUES (?, 'NABH-ADM', 'fixture', '2025-03-05 10:00:00')",
                (pid,),
            )
    for i, pid in enumerate(patients[:4]):
        svc.record_disposition(
            patient_id=pid, disposition="death" if i == 0 else "routine",
            occurred_at=f"2025-03-0{i + 1}T12:00:00+00:00", recorded_by="NABH-ADM")

    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    mort = next(i for i in dash["indicators"] if i["id"] == "inpatient_mortality")

    assert mort["state"] == "insufficient_data"
    assert mort["value"] is None
    assert mort["display"] is None
    assert mort["detail"]["coverage"] == 0.2
    assert "biased sample" in mort["detail"]["coverage_shortfall"]
    # The arithmetic survives for the audit trail — withheld, not lost.
    assert mort["detail"]["withheld_value"] == 25.0


def test_PROVE_CAN_FAIL_the_finaliser_is_what_withholds_the_number():
    """`_finalise` is the single gate. Hand it an indicator that has computed
    100 % off a denominator of 1 and it downgrades and strips the value; hand it
    the same indicator with the floor met and it reports. Without the downgrade,
    the identical input renders '100.0%' — a perfect score from one data point.
    """
    good = svc._new_indicator("inpatient_mortality")
    good.update({"state": "reported", "value": 100.0, "display": "100.0%",
                 "numerator": 1, "denominator": 1, "n_observed": 1})
    assert good["n_required"] >= 20
    withheld = svc._finalise(dict(good))
    assert withheld["state"] == "insufficient_data"
    assert withheld["value"] is None and withheld["display"] is None
    assert withheld["detail"]["withheld_display"] == "100.0%"

    # The same code path with the floor met MUST still report — otherwise the
    # guard is not discriminating, it is just always saying no.
    met = dict(good)
    met.update({"denominator": 40, "n_observed": 40})
    assert svc._finalise(met)["display"] == "100.0%"


def test_no_indicator_ever_carries_a_value_unless_it_is_reported():
    """The invariant across every indicator and every state, checked in one
    place so a tenth indicator added tomorrow cannot opt out of it."""
    ward = _ward()
    _seed_patients(ward, 5)
    _seed_incident(ward=ward, incident_type="medication_error")
    dash = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    for ind in dash["indicators"]:
        if ind["state"] != "reported":
            assert ind["value"] is None, ind["id"]
            assert ind["display"] is None, ind["id"]
        else:
            assert ind["display"] is not None, ind["id"]
            assert ind["denominator"], ind["id"]


# ═════════════════════════════════════════════════════════════════════════════
# 4. Anonymous reporting
# ═════════════════════════════════════════════════════════════════════════════

def test_an_anonymous_report_stores_no_reporter_id_even_when_one_is_passed():
    """The service OVERWRITES the identity arguments rather than merely
    declining to use them, so a future caller cannot store one by accident."""
    incident = svc.report_incident(
        incident_type="medication_error", severity="near_miss",
        title="Wrong strength nearly given", description="Caught at the trolley.",
        occurred_at="2025-03-05T09:00:00+00:00", ward=_ward(),
        is_anonymous=True, reported_by="DOC001", reporter_role="doctor")

    assert incident["is_anonymous"] is True
    assert incident["reported_by"] is None
    assert incident["reporter_role"] is None

    with db_session() as db:
        row = db.execute(
            "SELECT reported_by, reporter_role, reported_at, reported_at_precision "
            "FROM nabh_incidents WHERE id = ?", (incident["id"],)).fetchone()
        assert row["reported_by"] is None
        assert row["reporter_role"] is None
        # Truncated to the day: a second-resolution report time is itself an
        # identifier once something else records who was calling the API.
        assert row["reported_at_precision"] == "day"
        assert row["reported_at"].endswith("T00:00:00+00:00")

        events = db.execute(
            "SELECT actor_user_id FROM nabh_incident_events WHERE incident_id = ?",
            (incident["id"],)).fetchall()
        assert [e["actor_user_id"] for e in events] == [None], (
            "the transition log kept a second copy of the reporter"
        )


def test_the_database_itself_refuses_an_attributed_anonymous_row():
    """The backstop. Bypass the service entirely and SQLite still refuses — the
    guarantee survives raw SQL, a future column and anybody who did not read the
    service."""
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute(
                "INSERT INTO nabh_incidents (reference, incident_type, severity, title, "
                "description, occurred_at, reported_at, is_anonymous, reported_by) "
                "VALUES (?, 'other', 'no_harm', 't', 'd', '2025-03-01T00:00:00+00:00', "
                "'2025-03-01T00:00:00+00:00', 1, 'DOC001')",
                (_uid("INCBAD"),),
            )


def test_a_named_report_does_record_its_reporter():
    """The other half: anonymity must be a CHOICE, not the only behaviour. A
    guard that refused every reporter id would pass the tests above and destroy
    the register's usefulness."""
    doctor = _admin_user("NABH-DOC-NAMED")
    incident = svc.report_incident(
        incident_type="patient_fall", severity="minor", title="Fall by the bed",
        description="Assisted fall, no injury.", ward=_ward(),
        is_anonymous=False, reported_by=doctor, reporter_role="admin")
    assert incident["reported_by"] == doctor
    assert incident["reporter_role"] == "admin"
    assert incident["reported_at_precision"] == "exact"


def test_the_anonymity_statement_admits_what_it_cannot_guarantee():
    """A promise nobody can keep is worse than none. The platform audit
    middleware records who called which endpoint, and the sentence shown above
    the checkbox has to say so — deleting that admission turns this red."""
    statement = svc.ANONYMITY_STATEMENT
    assert "no reporter name, id or role" in statement
    assert "NOT untraceable" in statement
    assert "audit" in statement


# ═════════════════════════════════════════════════════════════════════════════
# 5. Incident lifecycle
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def open_incident(quality_admin):
    return svc.report_incident(
        incident_type="delay_in_care", severity="moderate",
        title="Delayed antibiotic", description="Two hours from order to dose.",
        ward=_ward(), reported_by=quality_admin, reporter_role="admin")


def test_a_freshly_reported_incident_cannot_be_closed(open_incident, quality_admin):
    with pytest.raises(svc.NabhError) as exc:
        svc.close_incident(open_incident["id"], actor_user_id=quality_admin)
    assert exc.value.code == "illegal_transition"


def test_closing_without_a_recorded_investigation_is_refused(open_incident, quality_admin):
    """A closure with no investigation is a deletion with extra steps."""
    svc.start_investigation(open_incident["id"], actor_user_id=quality_admin)
    with pytest.raises(svc.NabhError) as exc:
        svc.close_incident(open_incident["id"], actor_user_id=quality_admin)
    # It cannot even reach the investigation check: `under_investigation` does
    # not transition straight to `closed`, and the API says so.
    assert exc.value.code == "illegal_transition"
    assert "closed" not in svc.get_incident(open_incident["id"])["allowed_transitions"]


def test_the_full_lifecycle_closes_only_once_every_action_is_done(open_incident,
                                                                 quality_admin):
    iid = open_incident["id"]
    svc.start_investigation(iid, actor_user_id=quality_admin)
    svc.record_investigation(iid, actor_user_id=quality_admin,
                             summary="Order placed at 09:00, dispensed 11:05.",
                             root_cause="Pharmacy queue not prioritised for antibiotics",
                             contributing_factors=["staffing", "no priority flag"])
    assert svc.get_incident(iid)["status"] == "actions_pending"

    svc.add_action(iid, actor_user_id=quality_admin,
                   description="Flag antibiotics as priority in the pharmacy queue",
                   action_type="system_change", due_date="2025-04-01")

    with pytest.raises(svc.NabhError) as exc:
        svc.close_incident(iid, actor_user_id=quality_admin, closure_note="done")
    assert exc.value.code == "open_actions"

    action_id = svc.get_incident(iid)["actions"][0]["id"]
    svc.complete_action(iid, action_id, actor_user_id=quality_admin, note="Shipped")
    closed = svc.close_incident(iid, actor_user_id=quality_admin,
                                closure_note="Fix verified over two weeks")
    assert closed["status"] == "closed"
    assert closed["open_action_count"] == 0

    kinds = [e["event_type"] for e in closed["events"]]
    assert kinds == ["reported", "investigation_started", "investigation_recorded",
                     "action_added", "action_completed", "closed"]


def test_closing_with_no_action_at_all_needs_a_stated_reason(open_incident,
                                                            quality_admin):
    """Deciding nothing needs to change is a legitimate outcome — and a
    DECISION, so it gets a sentence somebody can be asked about."""
    iid = open_incident["id"]
    svc.start_investigation(iid, actor_user_id=quality_admin)
    svc.record_investigation(iid, actor_user_id=quality_admin,
                             summary="Reviewed; the delay was a data-entry error.")
    with pytest.raises(svc.NabhError) as exc:
        svc.close_incident(iid, actor_user_id=quality_admin)
    assert exc.value.code == "no_action_reason_required"

    closed = svc.close_incident(iid, actor_user_id=quality_admin,
                                no_action_reason="Timestamp was mis-keyed; no delay occurred.")
    assert closed["status"] == "closed"
    assert closed["no_action_reason"]


def test_a_closed_incident_can_be_reopened_and_the_closure_is_not_erased(
        open_incident, quality_admin):
    iid = open_incident["id"]
    svc.start_investigation(iid, actor_user_id=quality_admin)
    svc.record_investigation(iid, actor_user_id=quality_admin, summary="s")
    svc.close_incident(iid, actor_user_id=quality_admin, no_action_reason="none needed")

    reopened = svc.start_investigation(iid, actor_user_id=quality_admin,
                                       note="Happened again on 12 March")
    assert reopened["status"] == "under_investigation"
    assert reopened["reopened_count"] == 1
    # The closure fields survive: a reopen is a new fact, not an erasure.
    assert reopened["closed_at"] is not None
    assert "reopened" in [e["event_type"] for e in svc.get_incident(iid)["events"]]


def test_a_voided_incident_is_terminal_and_is_never_deleted(open_incident,
                                                            quality_admin):
    iid = open_incident["id"]
    voided = svc.void_incident(iid, actor_user_id=quality_admin,
                               reason="Duplicate of INC-000012")
    assert voided["status"] == "void"
    assert voided["allowed_transitions"] == []
    with pytest.raises(svc.NabhError) as exc:
        svc.start_investigation(iid, actor_user_id=quality_admin)
    assert exc.value.code == "illegal_transition"
    assert svc.get_incident(iid)["void_reason"] == "Duplicate of INC-000012"


def test_an_unknown_incident_type_is_refused_with_the_list(quality_admin):
    with pytest.raises(svc.NabhError) as exc:
        svc.report_incident(incident_type="oopsie", severity="no_harm", title="t",
                            description="d", reported_by=quality_admin)
    assert exc.value.code == "bad_value"
    assert "medication_error" in str(exc.value)


def test_a_disposition_recorded_twice_for_one_discharge_does_not_double_count():
    """A correction must not inflate the mortality denominator."""
    ward = _ward()
    patient = _seed_patients(ward, 1)[0]
    svc.record_disposition(patient_id=patient, disposition="routine",
                           occurred_at="2025-03-05T12:00:00+00:00",
                           recorded_by="NABH-ADM")
    svc.record_disposition(patient_id=patient, disposition="lama",
                           occurred_at="2025-03-05T12:00:00+00:00",
                           recorded_by="NABH-ADM", notes="corrected")
    listing = svc.list_dispositions(period_start=P_START, period_end=P_END)
    mine = [d for d in listing["dispositions"] if d["patient_id"] == patient]
    assert len(mine) == 1
    assert mine[0]["disposition"] == "lama"


# ═════════════════════════════════════════════════════════════════════════════
# 6. The evidence pack
# ═════════════════════════════════════════════════════════════════════════════

def test_the_pack_is_de_identified_by_default_and_says_so():
    ward = _ward()
    patient = _seed_patients(ward, 1)[0]
    svc.report_incident(incident_type="patient_fall", severity="minor",
                        title="Fall", description="No injury.", ward=ward,
                        occurred_at="2025-03-05T09:00:00+00:00",
                        patient_id=patient, reported_by=_admin_user())

    pack = svc.build_evidence_pack(period_start=P_START, period_end=P_END, ward=ward)
    row = pack["register"]["incidents"][0]
    assert row["patient_id"] is None
    assert row["patient_identifier_withheld"] is True
    assert any("Free-text incident descriptions are NOT redacted" in note
               for note in pack["limitations"])

    named = svc.build_evidence_pack(period_start=P_START, period_end=P_END, ward=ward,
                                    include_patient_identifiers=True)
    assert named["register"]["incidents"][0]["patient_id"] == patient
    assert any("CONTAINS PATIENT IDENTIFIERS" in note for note in named["limitations"])


def test_the_pack_digest_is_reproducible_from_the_returned_document():
    pack = svc.record_evidence_export(period_start=P_START, period_end=P_END,
                                      generated_by=_admin_user())
    body = {k: v for k, v in pack.items()
            if k not in ("reference", "content_sha256", "digest_note")}
    recomputed = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()
    assert recomputed == pack["content_sha256"]

    logged = svc.list_evidence_exports(limit=1)["exports"][0]
    assert logged["reference"] == pack["reference"]
    assert logged["content_sha256"] == pack["content_sha256"]
    assert logged["include_patient_identifiers"] == 0


def test_the_pack_carries_the_gap_list_and_the_attestation():
    """The honest half of an evidence pack is the half that says what is
    missing."""
    pack = svc.build_evidence_pack(period_start=P_START, period_end=P_END)
    codes = {g["code"] for g in pack["not_collected"]}
    assert {"QI-INF-01", "QI-INF-02", "QI-UTL-01"} <= codes
    for gap in pack["not_collected"]:
        assert gap["reason"] and gap["needed"]
    assert "NABH accredits the hospital" in pack["attestation"]
    assert "must not be reported as zero" in pack["attestation"]


def test_the_markdown_pack_prints_words_not_numbers_for_a_withheld_indicator():
    """The rendering is where a careful service gets undone. A non-reported
    indicator must print INSUFFICIENT DATA / NOT COLLECTED in the result column
    — never a number, and never a bare dash that reads as zero."""
    ward = _ward()
    _seed_patients(ward, 5)
    pack = svc.build_evidence_pack(period_start=P_START, period_end=P_END, ward=ward)
    text = svc.render_pack_markdown(pack)

    assert "NOT COLLECTED" in text
    for ind in pack["indicators"]:
        if ind["state"] == "reported":
            continue
        row = next(line for line in text.splitlines()
                   if line.startswith(f"| {ind['code']} |"))
        result = row.split("|")[3].strip()
        assert result in ("**INSUFFICIENT DATA**", "**NOT COLLECTED**",
                          "**NOT COMPUTABLE**"), (ind["id"], result)
        assert "0" not in result and "%" not in result


# ═════════════════════════════════════════════════════════════════════════════
# 7. The API surface
# ═════════════════════════════════════════════════════════════════════════════

def test_a_nurse_can_report_but_cannot_read_the_register():
    """A register only a manager can write to is a register nobody writes to;
    a register any clinician can BROWSE is a different disclosure."""
    nurse = _nurse_user("NABH-NUR-REPORT")
    headers = _login(nurse)
    resp = client.post("/api/nabh/incidents", headers=headers, json={
        "incident_type": "medication_error", "severity": "near_miss",
        "title": "Near miss at the trolley",
        "description": "Two strengths of the same drug side by side.",
        "ward": _ward(), "is_anonymous": True,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["incident"]["reported_by"] is None
    assert body["anonymity_statement"] == svc.ANONYMITY_STATEMENT

    assert client.get("/api/nabh/incidents", headers=headers).status_code == 403


def test_the_anonymous_route_writes_no_actor_on_this_modules_audit_event():
    """This module's own audit row must not name the reporter. (The platform
    mutation middleware still records the caller — that residual is what
    ANONYMITY_STATEMENT admits to, and closing it needs a change to
    services/audit_middleware.py, which is a follow-up.)"""
    nurse = _nurse_user("NABH-NUR-AUDIT")
    headers = _login(nurse)
    resp = client.post("/api/nabh/incidents", headers=headers, json={
        "incident_type": "documentation_error", "severity": "no_harm",
        "title": "Anonymous audit check", "description": "d",
        "ward": _ward(), "is_anonymous": True,
    })
    assert resp.status_code == 201, resp.text
    reference = resp.json()["incident"]["reference"]

    with db_session() as db:
        row = db.execute(
            "SELECT actor_user_id FROM audit_logs WHERE action_type = "
            "'nabh_incident_reported' AND target_id = ?", (reference,)).fetchone()
    assert row is not None, "the report was not audited at all"
    assert row["actor_user_id"] is None


def test_the_indicator_and_register_reads_need_admin_overview(quality_admin):
    headers = _login(quality_admin)
    assert client.get(f"/api/nabh/indicators?period_start={P_START}&period_end={P_END}",
                      headers=headers).status_code == 200
    assert client.get("/api/nabh/incidents", headers=headers).status_code == 200
    assert client.get("/api/nabh/meta", headers=headers).status_code == 200


def test_a_disabled_module_answers_404_with_a_reason(monkeypatch, quality_admin):
    """A site that keeps its quality register elsewhere sets ORB_NABH=0. Every
    route must then say WHY, so a stale client gets a sentence and not a blank."""
    monkeypatch.setattr(config, "NABH_ENABLED", False)
    headers = _login(quality_admin)
    resp = client.get("/api/nabh/indicators", headers=headers)
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "module_disabled"
    assert detail["module"] == "nabh"
    assert "ORB_NABH=0" in detail["message"]


def test_the_module_is_registered_on_the_health_switchboard():
    assert "nabh" in config.enabled_modules()
    assert config.HOSPITAL_MODULE_ENV["nabh"] == "ORB_NABH"


def test_the_register_read_route_scopes_a_query_string_patient_id():
    """A caller-chosen patient_id on the QUERY STRING is a per-patient PHI read
    in a spelling the path-param drift guard cannot see
    (tests/test_phi_read_scope_gaps.py). A department-scoped account must get a
    404 — never a 403, which would confirm the patient exists."""
    scoped = _admin_user("NABH-ADM-SCOPED", department="NABH Quality Dept A")
    other_patient = _seed_patients(_ward(), 1)[0]
    with db_session() as db:
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
            "VALUES (?, ?, ?, 'NABH Quality Dept B', 'active')",
            (_uid("NABHADM"), other_patient, scoped),
        )
    headers = _login(scoped)
    resp = client.get(f"/api/nabh/incidents?patient_id={other_patient}", headers=headers)
    assert resp.status_code == 404, resp.text


def test_the_export_route_records_the_pack_it_produced(quality_admin):
    headers = _login(quality_admin)
    resp = client.post("/api/nabh/export", headers=headers,
                       json={"period_start": P_START, "period_end": P_END})
    assert resp.status_code == 200, resp.text
    pack = resp.json()
    assert pack["content_sha256"]
    listed = client.get("/api/nabh/exports", headers=headers).json()["exports"]
    assert pack["reference"] in [e["reference"] for e in listed]


def test_the_markdown_export_is_a_downloadable_document(quality_admin):
    headers = _login(quality_admin)
    resp = client.post("/api/nabh/export.md", headers=headers,
                       json={"period_start": P_START, "period_end": P_END})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment; filename=" in resp.headers["content-disposition"]
    assert "# NABH accreditation evidence pack" in resp.text


def test_no_handler_in_this_module_is_async():
    """Every route here opens SQLite. `async def` would put an encrypted page
    scan on the event loop, which is the shape of all three production freezes
    this repo has shipped. tests/test_event_loop_discipline.py is the general
    guard; this is the local one, so the failure names this module."""
    import inspect

    from routes import nabh_routes
    offenders = [
        route.name for route in app.routes
        if getattr(route, "endpoint", None) is not None
        and getattr(route.endpoint, "__module__", "") == nabh_routes.__name__
        and inspect.iscoroutinefunction(route.endpoint)
    ]
    assert offenders == []
