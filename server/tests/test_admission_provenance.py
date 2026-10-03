"""Fix-list regression tests: admission creates a real one-time patient login,
and real vitals carry non-'simulated' provenance (so the bot prune can't delete them).

Also holds the admission half of the VitalsSource provenance seam: the vitals a
clinician types on the admission form are a MANUAL reading like any other, and
they are written by the same source object the nurse charting path uses, not by
a hand-rolled INSERT in the orchestrator.
"""
import pathlib
import uuid

import pytest

from database import db_session
from services import admission_service
from services.admission_service import admit_patient


_SEEDED: list[str] = []


def _admit(data):
    """Admit through the real service, registering the new patient id so the
    autouse teardown below can close it."""
    res = admit_patient(data, _clinician())
    if res.get("patient_id"):
        _SEEDED.append(res["patient_id"])
    return res


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: admit_patient() creates a patient that defaults to
    status='admitted' AND an admissions row that defaults to status='active', so
    each test here used to leave one of each behind in the shared session DB.
    That is not a cosmetic leak. The FHIR self-export in
    services/downtime_service.py iterates `patients WHERE status='admitted'` and
    sleeps _PATIENT_SLEEP_S (250 ms) BETWEEN PATIENTS, so each leftover adds
    ~0.275 s to every export call in tests/test_downtime_fhir_export.py and to
    everything downstream of it.

    BOTH rows are closed: the census surfaces filter patients.status while the
    department-scoped surfaces join admissions, so closing only one leaves the
    residue half-visible — which is worse than an honest leak, because it looks
    clean. Discharge rather than DELETE: the patient user, vitals and audit rows
    this module asserts on legitimately reference these ids."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _SEEDED],
        )
        db.executemany(
            "UPDATE admissions SET status = 'discharged' "
            "WHERE patient_id = ? AND status = 'active'",
            [(p,) for p in _SEEDED],
        )
    _SEEDED.clear()


def _clinician():
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            ("PROV-DOC", "Dr Prov", "doctor", hash_password("x")),
        )
    return {"id": "PROV-DOC", "name": "Dr Prov", "role": "doctor"}


def test_admission_creates_unique_patient_login_and_links_it():
    data = {"name": "Prov Patient", "ward": "General",
            "vitals": {"hr": 88, "bp_sys": 120, "spo2": 97, "on_oxygen": True}}
    res = _admit(data)
    assert res["success"]
    temp = res["temporary_password"]
    uid = res["patient_login_id"]
    assert temp and temp != "patient123"

    # The patient user exists, is role 'patient', and is linked to the patient row.
    with db_session() as db:
        u = db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
        assert u and u["role"] == "patient"
        p = db.execute("SELECT user_id FROM patients WHERE id = ?", (res["patient_id"],)).fetchone()
        assert p["user_id"] == uid

    # The one-time password verifies; a shared default does not.
    from auth import verify_password
    with db_session() as db:
        h = db.execute("SELECT password_hash FROM users WHERE id = ?", (uid,)).fetchone()["password_hash"]
    assert verify_password(temp, h)
    assert not verify_password("patient123", h)


def test_admission_vitals_are_not_marked_simulated():
    data = {"name": "Prov Patient 2", "ward": "ICU",
            "vitals": {"hr": 110, "bp_sys": 95, "spo2": 92, "on_oxygen": True}}
    res = _admit(data)
    with db_session() as db:
        row = db.execute(
            "SELECT source, on_oxygen FROM vitals WHERE patient_id = ? ORDER BY id DESC LIMIT 1",
            (res["patient_id"],),
        ).fetchone()
    assert row is not None
    assert row["source"] == "clinician"        # NOT 'simulated' → safe from the bot prune
    assert row["on_oxygen"] == 1               # supplemental O2 persisted for NEWS2


# ── the admission write is a ManualEntrySource path ───────────────────────────
def _nurse_user():
    from auth import hash_password
    uid = f"PROVNUR{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, "Prov Nurse", "nurse", hash_password("x")),
        )
    return uid


def _provenance_contract(row):
    """What a reader can say about a row's origin, from the row alone."""
    from services.vitals_source import classify_source_label, is_machine_measured
    return (classify_source_label(row["source"]),
            is_machine_measured(row["source"]),
            row["source_event_id"] is None)


def test_admission_vitals_match_the_nurse_paths_provenance_contract():
    """The two manual paths must be indistinguishable in KIND and distinguishable
    in PATH. Until the admission write went through ManualEntrySource it was a
    hand-rolled INSERT in the orchestrator with its own literal, one edit away
    from producing a reading the shadow scorecard could not attribute — the same
    hole 'nurse_manual' fell into on 2026-08-10."""
    from services import nurse_shift_service as nurse
    from services.vitals_source import LABEL_ADMISSION_VITALS, SourceKind

    res = _admit({"name": "Prov Seam", "ward": "ICU",
                  "vitals": {"hr": 104, "bp_sys": 108, "spo2": 93, "on_oxygen": True}})
    pid = res["patient_id"]
    obs = nurse.record_observation(pid, _nurse_user(), {"heart_rate": 96, "spo2": 95})

    with db_session() as db:
        admission_row = db.execute(
            "SELECT source, source_event_id FROM vitals WHERE patient_id = ? "
            "ORDER BY id ASC LIMIT 1", (pid,),
        ).fetchone()
        nurse_row = db.execute(
            "SELECT source, source_event_id FROM vitals WHERE id = ?",
            (obs["vitals_id"],),
        ).fetchone()

    assert _provenance_contract(admission_row) == _provenance_contract(nurse_row)
    assert _provenance_contract(admission_row) == (SourceKind.MANUAL, False, True)
    assert admission_row["source"] == LABEL_ADMISSION_VITALS
    # Same kind, different path: 'which manual surface produced this?' stays answerable.
    assert admission_row["source"] != nurse_row["source"] == nurse.OBS_SOURCE


def test_the_seam_writes_exactly_the_clinical_row_admission_always_wrote():
    """Routing a write through a seam is where a column quietly goes missing.
    Every vital the form carried is on the row, an absent one is NULL (not 0 —
    an RR of 0 scores +3 on NEWS2). An ACVPU the form did not send is NULL, not
    assessed (ruled 2026-10-03); it used to take the column default 'alert'."""
    res = _admit({"name": "Prov Seam Columns", "ward": "ICU",
                  "vitals": {"hr": 104, "bp_sys": 108, "bp_dia": 62, "spo2": 93,
                             "temperature": 38.4, "on_oxygen": False}})
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM vitals WHERE patient_id = ? ORDER BY id ASC LIMIT 1",
            (res["patient_id"],),
        ).fetchone()
    assert (row["heart_rate"], row["systolic_bp"], row["diastolic_bp"]) == (104, 108, 62)
    assert (row["spo2"], row["temperature"]) == (93, 38.4)
    assert row["respiratory_rate"] is None
    assert row["on_oxygen"] == 0            # supplied as False: a recorded false
    assert row["consciousness"] is None     # not supplied: not assessed
    assert row["timestamp"]


def test_admission_vitals_are_counted_on_the_manual_source_health_surface():
    """'Is manual entry alive?' is one question with one answer: the admission
    form's sets land in the same counters the nurse charting path reports."""
    from services.vitals_source import ManualEntrySource
    from services.vitals_source.registry import registry

    displaced = registry.get("manual")
    if displaced is not None:
        registry.unregister("manual")
    source = registry.register(ManualEntrySource(None))
    try:
        _admit({"name": "Prov Seam Stats", "ward": "ICU",
                "vitals": {"hr": 88, "spo2": 97}})
        assert source.stats.messages_received == 1
        assert source.stats.messages_accepted == 1
        assert source.stats.readings_ingested == 1
        assert source.stats.last_reading_at is not None
    finally:
        registry.unregister("manual")
        if displaced is not None:
            registry.register(displaced)


def test_a_refused_vitals_write_fails_the_whole_admission_loudly(monkeypatch):
    """REGRESSION (prove-can-fail): proves the admission vitals write REALLY goes
    through the seam, and that admission stayed fail-loud and atomic while it
    moved. With the old direct INSERT in admission_service this patch would be
    bypassed entirely and the admission would succeed."""
    from services.vitals_source import ManualEntrySource

    def _refuse(*args, **kwargs):
        raise RuntimeError("seam refused the write")

    monkeypatch.setattr(ManualEntrySource, "record_at_admission", _refuse)
    name = f"Prov Seam Rollback {uuid.uuid4().hex[:6]}"
    with db_session() as db:
        last_vitals_id = db.execute("SELECT COALESCE(MAX(id), 0) m FROM vitals").fetchone()["m"]

    with pytest.raises(RuntimeError, match="seam refused the write"):
        _admit({"name": name, "ward": "ICU", "vitals": {"hr": 88, "spo2": 97}})

    # Atomic: no half-admitted patient, and no reading, left behind.
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) c FROM patients WHERE name = ?",
                          (name,)).fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM vitals WHERE id > ?",
                          (last_vitals_id,)).fetchone()["c"] == 0


def test_admission_service_owns_no_vitals_insert_of_its_own():
    """Structural half of the regression above: the orchestrator must not hold a
    second, divergent vitals writer. A row shape duplicated here is how the
    provenance columns drift apart between two paths that must agree."""
    src = pathlib.Path(admission_service.__file__).read_text().lower()
    assert "into vitals" not in src
    assert "'clinician'" not in src, "the provenance label belongs to the seam's constant"
