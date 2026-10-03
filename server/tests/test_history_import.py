"""Bulk historical record import (services/history_import_service.py).

Orb REPLACES the incumbent EHR, so on cutover day years of existing clinical
history arrive from a system nobody here designed. The value of this importer is
almost entirely in what it REFUSES to do, so the tests are weighted that way:

  * the three DANGER tests first — a history import must not put historical
    patients on the live ward board, must not put stopped drugs back on the live
    medication list, and must not assert old diagnoses as currently active. Each
    of those would be a silent clinical-safety failure that reports success;
  * then provenance — an imported row must be distinguishable from an
    Orb-native one, and the importer must never overwrite Orb-native data;
  * then idempotency on the external id;
  * then one case per class of dirty data we expect from a real legacy export;
  * then the completeness of the reject ledger (the "silently dropped 4%"
    insurance), and finally the admin-only audited HTTP surface.

Governing rule under test throughout, inherited from roster import: REPORT,
never silently coerce, and never guess at a clinical value.
"""
import csv
import io
import itertools
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import history_import_service as hi

client = TestClient(app)

SOURCE_SYSTEM = "MediSys HIS"


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture()
def admin_user():
    uid = f"HIADM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'admin', 'x')",
            (uid, "History Admin"),
        )
    return uid


@pytest.fixture()
def doctor_user():
    uid = f"HIDOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'doctor', 'x')",
            (uid, "History Doctor"),
        )
    return uid


@pytest.fixture()
def orb_native_patient():
    """A patient Orb already holds — as if roster import had put them there.

    TEST-ISOLATION LAW: created ADMITTED (that is the point — it is the live
    ward state the history import must not disturb), so it is discharged and the
    admission closed in teardown or later modules measure this leftover.
    """
    pid = f"PAT-HI{uuid.uuid4().hex[:6].upper()}"
    mrn = _mrn("NATIVE")
    adm = f"ADM-HI{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, mrn, name, ward, bed, status, sex) "
            "VALUES (?, ?, 'Existing Orb Patient', 'GM-1', 'B4', 'admitted', 'F')",
            (pid, mrn),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, status) "
            "VALUES (?, ?, 'system', 'General Medicine', 'GM-1', 'active')",
            (adm, pid),
        )
    yield {"id": pid, "mrn": mrn, "admission_id": adm}
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _auth(user_id: str, role: str = "admin") -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


# A process-wide counter, NOT just randomness. These helpers used 6 hex
# characters alone, which is 24 bits: the 500-row streaming test generates 500
# ids in one file and collided about once every eight runs, failing with
# 499 == 500. The product was right — it detected the duplicate external id and
# refused the row, exactly as designed — but a fixture that is randomly wrong
# teaches you to distrust real failures. The counter makes uniqueness within a
# run certain; the uuid keeps ids distinct across runs of the shared session DB.
_SEQ = itertools.count()


def _mrn(tag: str) -> str:
    """Unique MRN per test — the suite shares one DB across modules."""
    return f"HI{tag}{next(_SEQ):06d}{uuid.uuid4().hex[:6].upper()}"


def _ext(tag: str) -> str:
    return f"EXT-{tag}-{next(_SEQ):06d}{uuid.uuid4().hex[:6].upper()}"


def _csv(header: str, *rows: str) -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode()


def _system() -> str:
    """A unique source system per test.

    The idempotency key is (source_system, external_id) and it is UNIQUE in the
    schema, so tests that reuse a literal name would collide with each other
    through the shared session DB.
    """
    return f"{SOURCE_SYSTEM} {uuid.uuid4().hex[:6].upper()}"


def _codes(result) -> set[str]:
    """Every issue code the run reported, from wherever it reported it."""
    out = set(result.get("issue_summary", {}))
    for row in result.get("rows", []):
        out |= {i["code"] for i in row.get("issues", [])}
    for fail in result.get("failures", []):
        out |= {i["code"] for i in fail.get("issues", [])}
    return out


def _row_by_ext(result, external_id):
    return next(r for r in result["rows"] if r["external_id"] == external_id)


def _patient_by_mrn(mrn):
    with db_session() as db:
        return db.execute("SELECT * FROM patients WHERE mrn = ?", (mrn,)).fetchone()


PATIENT_HEADER = "PatientID,MRN,Name,DOB,Sex,Phone"
PATIENT_MAPPING = {"external_id": "PatientID", "mrn": "MRN", "name": "Name",
                   "date_of_birth": "DOB", "sex": "Sex", "phone": "Phone"}


def _import_patient(system, ext, mrn, name="Ramesh Kumar", actor="system", **kw):
    """Commit one historical patient and return the plan result."""
    data = _csv(PATIENT_HEADER, f"{ext},{mrn},{name},1971-04-02,M,9876500000")
    return hi.apply_import(data, "patients", PATIENT_MAPPING,
                           source_system=system, actor_user_id=actor, **kw)


# ══════════════════════════════════════════════════════════════════════════
#  DANGER 1 — a history import must NOT put historical patients on the ward
# ══════════════════════════════════════════════════════════════════════════

def test_imported_patients_are_not_placed_on_the_live_ward_board():
    """The inverse of roster import's rule, and the more dangerous direction.

    Roster import exists to make "imported" mean "on the board". A HISTORY
    import must mean the opposite: these are people treated years ago. Filing
    them as admitted would flood the live ward board and the nurse shift board
    with hundreds of patients who are not in the building.
    """
    system, ext, mrn = _system(), _ext("P"), _mrn("WARD")
    _import_patient(system, ext, mrn)

    row = _patient_by_mrn(mrn)
    assert row is not None, "patient should have been created"
    assert row["status"] == "discharged", (
        f"historical patient was filed as {row['status']!r} — a history import "
        f"must never place a past patient on the live ward board"
    )
    with db_session() as db:
        open_adm = db.execute(
            "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active'",
            (row["id"],),
        ).fetchone()
    assert open_adm is None, "history import opened a live admission"


def test_admission_still_open_in_the_source_is_imported_closed_and_reported():
    """A legacy row claiming the encounter is still open is out of contract.

    History import handles CLOSED encounters. An 'active' row means a current
    inpatient, which belongs to roster import — importing it as active here
    would put a patient on the live board through the wrong door and with no
    ward/bed discipline. So it is imported CLOSED and listed for the admin.
    """
    system, pext, mrn = _system(), _ext("P"), _mrn("OPEN")
    _import_patient(system, pext, mrn)

    aext = _ext("A")
    data = _csv("EncID,PatientID,Dept,Ward,AdmitDate,DischDate,Status",
                f"{aext},{pext},General Medicine,GM-2,2024-03-01,,active")
    result = hi.apply_import(
        data, "admissions",
        {"external_id": "EncID", "external_patient_id": "PatientID",
         "department": "Dept", "ward": "Ward", "admitted_at": "AdmitDate",
         "discharged_at": "DischDate", "status": "Status"},
        source_system=system, actor_user_id="system")

    assert "admission_open_in_source" in _codes(result)
    assert result["still_open_in_source"], "the open encounter should be listed for the admin"
    with db_session() as db:
        adm = db.execute(
            "SELECT * FROM admissions WHERE import_source_system = ? AND import_external_id = ?",
            (system, aext),
        ).fetchone()
    assert adm["status"] != "active", "history import created a LIVE admission"


# ══════════════════════════════════════════════════════════════════════════
#  DANGER 2 — historical medications must not rejoin the live medication list
# ══════════════════════════════════════════════════════════════════════════

def test_historical_medications_are_not_imported_as_active():
    """`medications.status='active'` is read by the live drug-safety path.

    services/drug_safety_service.py checks interactions against active meds, and
    the discharge summary and Sage both list them. A drug the patient stopped in
    2023 arriving as 'active' would be screened, displayed and handed over as a
    current medication — a direct patient-safety hazard, not a cosmetic one.
    """
    system, pext, mrn = _system(), _ext("P"), _mrn("MED")
    _import_patient(system, pext, mrn)

    mext = _ext("M")
    data = _csv("RxID,PatientID,Drug,Dose,Route,Freq",
                f"{mext},{pext},Amoxicillin,500 mg,Oral,TDS")
    result = hi.apply_import(
        data, "medications",
        {"external_id": "RxID", "external_patient_id": "PatientID",
         "drug_name": "Drug", "dosage": "Dose", "route": "Route", "frequency": "Freq"},
        source_system=system, actor_user_id="system")
    assert result["summary"]["created"] == 1

    with db_session() as db:
        med = db.execute(
            "SELECT * FROM medications WHERE import_source_system = ? AND import_external_id = ?",
            (system, mext),
        ).fetchone()
    assert med["status"] != "active", (
        "an imported historical medication is live on the drug-interaction path"
    )
    assert med["source"] == "history_import", "medication provenance was not recorded"
    assert "medication_status_defaulted" in _codes(result)


def test_medication_missing_dose_records_unknown_rather_than_guessing():
    """Never guess at a clinical value — but do not lose the drug either.

    `medications.dosage` is NOT NULL. Inventing '500 mg' would be a fabricated
    dose; dropping the row would erase the clinically important fact that the
    patient was exposed to the drug at all (which is what allergy and
    interaction history is FOR). So the dose is recorded as the literal UNKNOWN
    and the row is flagged.
    """
    system, pext, mrn = _system(), _ext("P"), _mrn("NODOSE")
    _import_patient(system, pext, mrn)

    mext = _ext("M")
    data = _csv("RxID,PatientID,Drug,Dose,Route,Freq",
                f"{mext},{pext},Metformin,,Oral,")
    result = hi.apply_import(
        data, "medications",
        {"external_id": "RxID", "external_patient_id": "PatientID",
         "drug_name": "Drug", "dosage": "Dose", "route": "Route", "frequency": "Freq"},
        source_system=system, actor_user_id="system")

    with db_session() as db:
        med = db.execute(
            "SELECT * FROM medications WHERE import_source_system = ? AND import_external_id = ?",
            (system, mext),
        ).fetchone()
    assert med is not None, "the drug exposure was dropped entirely"
    assert med["dosage"] == hi.UNKNOWN_VALUE
    assert med["frequency"] == hi.UNKNOWN_VALUE
    assert "missing_clinical_value" in _codes(result)


# ══════════════════════════════════════════════════════════════════════════
#  DANGER 3 — old diagnoses must not be asserted as currently active
# ══════════════════════════════════════════════════════════════════════════

def test_diagnosis_without_a_status_is_not_asserted_as_active():
    """`problem_list.status` defaults to 'active' in the schema.

    Taking that default would turn every historical diagnosis into a claim about
    the patient's CURRENT problem list — a claim the export never made. The
    importer files it inactive and says so per row.
    """
    system, pext, mrn = _system(), _ext("P"), _mrn("DX")
    _import_patient(system, pext, mrn)

    dext = _ext("D")
    data = _csv("DxID,PatientID,Diagnosis,ICD10,OnsetDate",
                f"{dext},{pext},Type 2 diabetes mellitus,E11.9,2019-06-01")
    result = hi.apply_import(
        data, "diagnoses",
        {"external_id": "DxID", "external_patient_id": "PatientID",
         "problem": "Diagnosis", "icd10_code": "ICD10", "onset_date": "OnsetDate"},
        source_system=system, actor_user_id="system")

    with db_session() as db:
        prob = db.execute(
            "SELECT * FROM problem_list WHERE import_source_system = ? AND import_external_id = ?",
            (system, dext),
        ).fetchone()
    assert prob["status"] != "active", (
        "a historical diagnosis was asserted as an ACTIVE problem the export never claimed"
    )
    assert "diagnosis_status_defaulted" in _codes(result)


def test_diagnosis_status_in_the_file_is_honoured():
    """When the export DOES state the status, that statement is imported."""
    system, pext, mrn = _system(), _ext("P"), _mrn("DXA")
    _import_patient(system, pext, mrn)

    dext = _ext("D")
    data = _csv("DxID,PatientID,Diagnosis,Status",
                f"{dext},{pext},Essential hypertension,active")
    hi.apply_import(data, "diagnoses",
                    {"external_id": "DxID", "external_patient_id": "PatientID",
                     "problem": "Diagnosis", "status": "Status"},
                    source_system=system, actor_user_id="system")
    with db_session() as db:
        prob = db.execute(
            "SELECT status FROM problem_list WHERE import_source_system = ? AND import_external_id = ?",
            (system, dext),
        ).fetchone()
    assert prob["status"] == "active"


# ══════════════════════════════════════════════════════════════════════════
#  Provenance — "this came from the old system" must be legible
# ══════════════════════════════════════════════════════════════════════════

def test_created_rows_carry_source_system_external_id_and_run_id():
    system, ext, mrn = _system(), _ext("P"), _mrn("PROV")
    result = _import_patient(system, ext, mrn)

    row = _patient_by_mrn(mrn)
    assert row["import_source_system"] == system
    assert row["import_external_id"] == ext
    assert row["import_run_id"] == result["run_id"]


def test_orb_native_patient_is_never_stamped_as_imported(orb_native_patient):
    """Matching an existing patient must not relabel their origin.

    The patient row was written by Orb. Stamping it with the legacy system's
    name because a history file happened to mention them would be a lie about
    provenance — and provenance is what a clinician uses to weigh the record.
    The MAPPING is still recorded, in the xref, because child records reference
    the old system's id and still have to resolve.
    """
    system, ext = _system(), _ext("P")
    _import_patient(system, ext, orb_native_patient["mrn"])

    row = _patient_by_mrn(orb_native_patient["mrn"])
    assert row["id"] == orb_native_patient["id"], "matched the wrong patient"
    assert row["import_source_system"] is None, "an Orb-native row was labelled as imported"
    assert row["import_external_id"] is None

    with db_session() as db:
        xref = db.execute(
            "SELECT * FROM history_import_patient_xref WHERE source_system = ? "
            "AND external_patient_id = ?", (system, ext)).fetchone()
    assert xref is not None, "the cross-system id mapping was not recorded"
    assert xref["patient_id"] == orb_native_patient["id"]
    assert xref["created_by_import"] == 0


def test_import_never_overwrites_a_value_orb_already_holds(orb_native_patient):
    """The live record is fresher than the legacy export. Fill holes only."""
    system, ext = _system(), _ext("P")
    data = _csv(PATIENT_HEADER,
                f"{ext},{orb_native_patient['mrn']},LEGACY NAME,1980-01-01,M,9999999999")
    result = hi.apply_import(data, "patients", PATIENT_MAPPING,
                             source_system=system, actor_user_id="system")

    row = _patient_by_mrn(orb_native_patient["mrn"])
    assert row["name"] == "Existing Orb Patient", "legacy data overwrote a live value"
    assert row["sex"] == "F", "legacy data overwrote a live value"
    # ...but a column Orb had no value for is filled.
    assert row["date_of_birth"] == "1980-01-01"
    assert "kept_orb_value" in _codes(result)


# ══════════════════════════════════════════════════════════════════════════
#  Dry run
# ══════════════════════════════════════════════════════════════════════════

def test_dry_run_writes_nothing_at_all():
    system, ext, mrn = _system(), _ext("P"), _mrn("DRY")
    data = _csv(PATIENT_HEADER, f"{ext},{mrn},Sunita Devi,1990-02-02,F,900000000")

    result = hi.plan(data, "patients", PATIENT_MAPPING, source_system=system)

    assert result["applied"] is False
    assert result["counts"]["create"] == 1
    assert _patient_by_mrn(mrn) is None, "the DRY RUN created a patient"
    with db_session() as db:
        runs = db.execute("SELECT COUNT(*) c FROM history_import_runs "
                          "WHERE source_system = ?", (system,)).fetchone()
    assert runs["c"] == 0, "the dry run wrote a run ledger row"


def test_dry_run_reports_every_failed_row_with_a_reason():
    """The whole point: a migration that silently drops rows is the catastrophe."""
    system = _system()
    good_ext, good_mrn = _ext("P"), _mrn("OK")
    data = _csv(
        PATIENT_HEADER,
        f"{good_ext},{good_mrn},Valid Patient,1970-01-01,M,900000001",
        f",{_mrn('NOEXT')},No External Id,1970-01-01,M,900000002",
        f"{_ext('P')},,No MRN,1970-01-01,F,900000003",
        f"{_ext('P')},{_mrn('NONAME')},,1970-01-01,F,900000004",
    )
    result = hi.plan(data, "patients", PATIENT_MAPPING, source_system=system)

    assert result["counts"]["total_rows"] == 4
    assert result["counts"]["create"] == 1
    assert result["counts"]["failed"] == 3
    codes = {f["reason"] for f in result["failures"]}
    assert codes == {"missing_external_id", "missing_mrn", "missing_name"}
    # every failure is tied to a physical line the admin can open the file to
    assert sorted(f["line"] for f in result["failures"]) == [3, 4, 5]
    assert all(f["issues"] for f in result["failures"]), "a failure carried no reason"


def test_a_file_with_no_data_rows_is_flagged_not_reported_as_success():
    """'0 created, 0 failed' reads as a clean run. An empty export, a filter left
    on in the old system, or a header line picked one row too low all produce
    exactly that — the silent-failure class, arrived at from the other end."""
    system = _system()
    result = hi.plan(_csv(PATIENT_HEADER), "patients", PATIENT_MAPPING,
                     source_system=system)

    assert result["counts"]["total_rows"] == 0
    codes = {i["code"] for i in result["file_issues"]}
    assert "no_data_rows" in codes, "an empty import reported as a clean success"


def test_issue_summary_counts_are_exact_even_when_detail_is_truncated():
    """Detail may be capped for a huge file; the COUNTS never may be.

    A truncated response that also under-reports the number of failures is
    exactly the silent-4%-drop failure in a new costume.
    """
    system = _system()
    rows = [f"{_ext('P')},,Row {i},1970-01-01,M,90000{i:04d}" for i in range(40)]
    result = hi.plan(_csv(PATIENT_HEADER, *rows), "patients", PATIENT_MAPPING,
                     source_system=system, detail_limit=5, reject_limit=5)

    assert result["counts"]["failed"] == 40
    assert result["issue_summary"]["missing_mrn"] == 40
    assert len(result["failures"]) == 5
    assert result["failures_truncated"] is True


# ══════════════════════════════════════════════════════════════════════════
#  Idempotency
# ══════════════════════════════════════════════════════════════════════════

def test_reimporting_the_same_file_creates_nothing_the_second_time():
    system, ext, mrn = _system(), _ext("P"), _mrn("IDEM")
    data = _csv(PATIENT_HEADER, f"{ext},{mrn},Ramesh Kumar,1971-04-02,M,9876500000")

    first = hi.apply_import(data, "patients", PATIENT_MAPPING,
                            source_system=system, actor_user_id="system")
    second = hi.apply_import(data, "patients", PATIENT_MAPPING,
                             source_system=system, actor_user_id="system")

    assert first["summary"]["created"] == 1
    assert second["summary"]["created"] == 0
    assert second["counts"]["unchanged"] == 1
    with db_session() as db:
        count = db.execute("SELECT COUNT(*) c FROM patients WHERE mrn = ?", (mrn,)).fetchone()
    assert count["c"] == 1


def test_child_records_are_idempotent_on_their_own_external_id():
    system, pext, mrn = _system(), _ext("P"), _mrn("IDEMC")
    _import_patient(system, pext, mrn)

    rext = _ext("R")
    data = _csv("ResultID,PatientID,Test,Value,Unit,ObservedAt",
                f"{rext},{pext},Haemoglobin,11.2,g/dL,2024-05-06")
    mapping = {"external_id": "ResultID", "external_patient_id": "PatientID",
               "observation_name": "Test", "value": "Value", "unit": "Unit",
               "observed_at": "ObservedAt"}

    hi.apply_import(data, "results", mapping, source_system=system, actor_user_id="system")
    second = hi.apply_import(data, "results", mapping, source_system=system,
                             actor_user_id="system")

    assert second["summary"]["created"] == 0
    with db_session() as db:
        count = db.execute(
            "SELECT COUNT(*) c FROM observations WHERE import_source_system = ? "
            "AND import_external_id = ?", (system, rext)).fetchone()
    assert count["c"] == 1


def test_admissions_with_notes_are_idempotent():
    """Regression: the importer used to prefix an 'Imported from X by run Y'
    banner onto admitting_notes. On the next run the file's plain note no longer
    matched the stored banner+note, so every re-import reported an update and
    rewrote a clinical free-text field — churn that would make the run ledger
    useless for spotting what actually changed."""
    system, pext, mrn = _system(), _ext("P"), _mrn("ADMN")
    _import_patient(system, pext, mrn)

    aext = _ext("A")
    data = _csv("EncID,PatientID,Dept,AdmitDate,Notes",
                f"{aext},{pext},General Medicine,2024-03-01,Admitted via casualty")
    mapping = {"external_id": "EncID", "external_patient_id": "PatientID",
               "department": "Dept", "admitted_at": "AdmitDate", "notes": "Notes"}

    first = hi.apply_import(data, "admissions", mapping, source_system=system,
                            actor_user_id="system")
    second = hi.apply_import(data, "admissions", mapping, source_system=system,
                             actor_user_id="system")

    assert first["summary"]["created"] == 1
    assert second["summary"]["created"] == 0
    assert second["summary"]["updated"] == 0, "a re-run rewrote an unchanged admission"
    assert second["counts"]["unchanged"] == 1
    with db_session() as db:
        adm = db.execute(
            "SELECT admitting_notes FROM admissions WHERE import_source_system = ? "
            "AND import_external_id = ?", (system, aext)).fetchone()
    assert adm["admitting_notes"] == "Admitted via casualty", (
        "generated text was written into a clinical free-text field"
    )


def test_duplicate_external_id_inside_one_file_is_reported_not_merged():
    system, mrn_a, mrn_b = _system(), _mrn("DUPA"), _mrn("DUPB")
    ext = _ext("P")
    data = _csv(PATIENT_HEADER,
                f"{ext},{mrn_a},First Row,1970-01-01,M,900000001",
                f"{ext},{mrn_b},Second Row,1980-01-01,F,900000002")
    result = hi.plan(data, "patients", PATIENT_MAPPING, source_system=system)

    assert result["counts"]["create"] == 1
    assert result["counts"]["failed"] == 1
    assert "duplicate_external_id_in_file" in _codes(result)


# ══════════════════════════════════════════════════════════════════════════
#  Patient resolution across two systems
# ══════════════════════════════════════════════════════════════════════════

def test_child_row_for_an_unknown_patient_is_rejected_never_orphaned():
    """An observation with no patient is invisible and untraceable — worse than
    absent, because the import reports success."""
    system = _system()
    data = _csv("ResultID,PatientID,Test,Value",
                f"{_ext('R')},{_ext('NOBODY')},Haemoglobin,11.2")
    result = hi.plan(data, "results",
                     {"external_id": "ResultID", "external_patient_id": "PatientID",
                      "observation_name": "Test", "value": "Value"},
                     source_system=system)

    assert result["counts"]["create"] == 0
    assert result["counts"]["failed"] == 1
    assert "unknown_patient" in _codes(result)


def test_child_row_can_resolve_by_mrn_when_the_export_has_no_patient_id():
    system, mrn = _system(), _mrn("BYMRN")
    _import_patient(system, _ext("P"), mrn)

    data = _csv("ResultID,MRN,Test,Value", f"{_ext('R')},{mrn},Creatinine,1.1")
    result = hi.apply_import(data, "results",
                             {"external_id": "ResultID", "mrn": "MRN",
                              "observation_name": "Test", "value": "Value"},
                             source_system=system, actor_user_id="system")
    assert result["summary"]["created"] == 1


def test_patient_id_and_mrn_disagreeing_is_reported_never_guessed():
    """Two identifiers pointing at two different humans is the duplicate-patient
    mess. Picking one would attach a result to the wrong chart."""
    system = _system()
    mrn_a, mrn_b = _mrn("DISA"), _mrn("DISB")
    pext_a = _ext("PA")
    _import_patient(system, pext_a, mrn_a)
    _import_patient(system, _ext("PB"), mrn_b)

    data = _csv("ResultID,PatientID,MRN,Test,Value",
                f"{_ext('R')},{pext_a},{mrn_b},Sodium,139")
    result = hi.plan(data, "results",
                     {"external_id": "ResultID", "external_patient_id": "PatientID",
                      "mrn": "MRN", "observation_name": "Test", "value": "Value"},
                     source_system=system)

    assert result["counts"]["failed"] == 1
    assert "patient_identifier_conflict" in _codes(result)


def test_same_human_under_two_mrns_is_reported_not_silently_merged():
    """One legacy patient id already mapped to a DIFFERENT Orb patient."""
    system, ext = _system(), _ext("P")
    _import_patient(system, ext, _mrn("TWIN1"))
    data = _csv(PATIENT_HEADER, f"{ext},{_mrn('TWIN2')},Same Human,1971-04-02,M,9876500000")
    result = hi.plan(data, "patients", PATIENT_MAPPING, source_system=system)

    assert "external_id_mrn_conflict" in _codes(result)
    assert result["counts"]["create"] == 0


# ══════════════════════════════════════════════════════════════════════════
#  Dirty data
# ══════════════════════════════════════════════════════════════════════════

def test_three_date_formats_in_one_column_all_parse():
    system = _system()
    rows = [f"{_ext('P')},{_mrn('D1')},One,2019-06-01,M,1",
            f"{_ext('P')},{_mrn('D2')},Two,12-Mar-1988,F,2",
            f"{_ext('P')},{_mrn('D3')},Three,05/09/1975,M,3"]
    result = hi.plan(_csv(PATIENT_HEADER, *rows), "patients", PATIENT_MAPPING,
                     source_system=system, date_order="dmy")
    assert result["counts"]["create"] == 3
    dobs = sorted(r["values"]["date_of_birth"] for r in result["rows"])
    assert dobs == ["1975-09-05", "1988-03-12", "2019-06-01"]


def test_ambiguous_date_is_left_blank_and_reported_not_guessed():
    system = _system()
    data = _csv(PATIENT_HEADER, f"{_ext('P')},{_mrn('AMB')},Amb,03/04/1990,M,1")
    result = hi.plan(data, "patients", PATIENT_MAPPING, source_system=system,
                     date_order="auto")
    assert "ambiguous_date" in _codes(result)
    assert "date_of_birth" not in result["rows"][0]["values"]


def test_honorifics_and_whitespace_are_reported_not_stripped():
    """A name is a bedside identity check. Rewriting it is not the importer's
    call — but 'Mr. Ramesh Kumar' should be flagged so the admin can decide."""
    system = _system()
    data = _csv(PATIENT_HEADER,
                f"{_ext('P')},{_mrn('HON')},  Mr.   Ramesh   Kumar ,1971-04-02,M,1")
    result = hi.plan(data, "patients", PATIENT_MAPPING, source_system=system)
    assert "honorific_in_name" in _codes(result)
    # whitespace is collapsed (safe), the honorific is NOT removed (not safe)
    assert result["rows"][0]["values"]["name"] == "Mr. Ramesh Kumar"


def test_non_numeric_lab_value_keeps_the_text_and_leaves_the_number_null():
    """'<0.01' is not 0.01, and 'NEGATIVE' is not 0. Coercing either would
    invent a result — and a numeric column is what trend graphs read."""
    system, pext, mrn = _system(), _ext("P"), _mrn("LAB")
    _import_patient(system, pext, mrn)

    ext_lt, ext_neg, ext_ok = _ext("R"), _ext("R"), _ext("R")
    data = _csv("ResultID,PatientID,Test,Value,Unit",
                f"{ext_lt},{pext},Troponin,<0.01,ng/mL",
                f"{ext_neg},{pext},Dengue NS1,NEGATIVE,",
                f"{ext_ok},{pext},Sodium,139,mmol/L")
    result = hi.apply_import(data, "results",
                             {"external_id": "ResultID", "external_patient_id": "PatientID",
                              "observation_name": "Test", "value": "Value", "unit": "Unit"},
                             source_system=system, actor_user_id="system")
    assert result["summary"]["created"] == 3
    assert "non_numeric_value" in _codes(result)

    with db_session() as db:
        got = {r["import_external_id"]: r for r in db.execute(
            "SELECT * FROM observations WHERE import_source_system = ?", (system,)).fetchall()}
    assert got[ext_lt]["value_numeric"] is None
    assert got[ext_lt]["value_text"] == "<0.01"
    assert got[ext_neg]["value_numeric"] is None
    assert got[ext_neg]["value_text"] == "NEGATIVE"
    assert got[ext_ok]["value_numeric"] == 139.0


def test_document_content_keeps_its_line_structure():
    """A discharge summary is written in paragraphs, and often carries a
    medication list one drug per line. The importer's ordinary whitespace
    tidying collapses all of that into a single run-on paragraph — right for a
    ward name, a quiet downgrade of the clinical record for a note. Regression:
    it did exactly that until an end-to-end rehearsal caught it.
    """
    system, pext, mrn = _system(), _ext("P"), _mrn("DOC")
    _import_patient(system, pext, mrn)

    body = ("Admitted with sepsis.\n"
            "\n"
            "Discharge medications:\n"
            "  Amoxicillin 500 mg TDS\n"
            "  Paracetamol 1 g QDS")
    dext = _ext("D")
    payload = io.StringIO()
    writer = csv.writer(payload)
    writer.writerow(["DocID", "PatientID", "Title", "Body"])
    writer.writerow([dext, pext, "Discharge Summary", body])

    hi.apply_import(payload.getvalue().encode(), "documents",
                    {"external_id": "DocID", "external_patient_id": "PatientID",
                     "title": "Title", "content": "Body"},
                    source_system=system, actor_user_id="system")

    with db_session() as db:
        doc = db.execute(
            "SELECT content FROM clinical_documents WHERE import_source_system = ? "
            "AND import_external_id = ?", (system, dext)).fetchone()
    assert "\n" in doc["content"], "the note was flattened into one paragraph"
    assert "Amoxicillin 500 mg TDS" in doc["content"]
    lines = doc["content"].split("\n")
    assert lines[0] == "Admitted with sepsis."
    assert "Discharge medications:" in lines
    assert lines.count("") == 1, "blank-line structure was not preserved"


def test_non_utf8_export_is_decoded_and_flagged():
    system = _system()
    # A Goan/Anglo-Indian name is the realistic case: a legacy Windows HIS emits
    # cp1252, where 'é' is one byte that is not valid UTF-8.
    body = f"{_ext('P')},{_mrn('ENC')},José D'Souza,1971-04-02,M,1"
    data = (PATIENT_HEADER + "\n" + body + "\n").encode("cp1252")
    result = hi.plan(data, "patients", PATIENT_MAPPING, source_system=system)
    assert "non_utf8_encoding" in {i["code"] for i in result["file_issues"]}
    assert result["counts"]["create"] == 1
    assert result["rows"][0]["values"]["name"] == "José D'Souza", (
        "the accented name did not survive the decode"
    )


def test_junk_preamble_before_the_header_is_skipped():
    system = _system()
    data = _csv("Discharge Summary Export, printed 30/07/2026",
                "",
                PATIENT_HEADER,
                f"{_ext('P')},{_mrn('PRE')},Preamble Patient,1971-04-02,M,1")
    result = hi.plan(data, "patients", PATIENT_MAPPING, source_system=system)
    assert result["header_line"] == 3
    assert result["counts"]["create"] == 1


def test_unquoted_comma_shifting_columns_rejects_the_row():
    """Inherited from roster import: a surplus field means every later column is
    one place to the left, so the row cannot be trusted at all."""
    system = _system()
    data = _csv(PATIENT_HEADER,
                f"{_ext('P')},{_mrn('SHIFT')},Ramesh Kumar,12 Nehru Street, New Delhi,M,1")
    result = hi.plan(data, "patients", PATIENT_MAPPING, source_system=system)
    assert result["counts"]["failed"] == 1
    assert "field_count_mismatch" in _codes(result)


# ══════════════════════════════════════════════════════════════════════════
#  The reject ledger — complete, durable, traceable
# ══════════════════════════════════════════════════════════════════════════

def test_commit_persists_every_reject_even_when_the_response_is_capped():
    system = _system()
    rows = [f"{_ext('P')},,Row {i},1970-01-01,M,1" for i in range(25)]
    rows.append(f"{_ext('P')},{_mrn('GOOD')},Good Row,1970-01-01,M,1")
    result = hi.apply_import(_csv(PATIENT_HEADER, *rows), "patients", PATIENT_MAPPING,
                             source_system=system, actor_user_id="system",
                             detail_limit=3, reject_limit=3)

    assert result["summary"]["created"] == 1
    assert result["summary"]["failed"] == 25
    with db_session() as db:
        stored = db.execute(
            "SELECT COUNT(*) c FROM history_import_rejects WHERE run_id = ?",
            (result["run_id"],)).fetchone()
    assert stored["c"] == 25, (
        "the response was capped and the durable reject ledger was capped with it — "
        "the dropped rows are now invisible"
    )


def test_run_ledger_records_the_file_hash_and_mapping():
    system, ext, mrn = _system(), _ext("P"), _mrn("LEDG")
    data = _csv(PATIENT_HEADER, f"{ext},{mrn},Ledger Patient,1971-04-02,M,1")
    result = hi.apply_import(data, "patients", PATIENT_MAPPING, source_system=system,
                             actor_user_id="system", source_filename="patients_2019.csv")

    with db_session() as db:
        run = db.execute("SELECT * FROM history_import_runs WHERE id = ?",
                         (result["run_id"],)).fetchone()
    assert run["record_type"] == "patients"
    assert run["source_system"] == system
    assert run["source_filename"] == "patients_2019.csv"
    assert run["source_sha256"] and len(run["source_sha256"]) == 64
    assert json.loads(run["mapping_json"])["mrn"] == "MRN"


def test_nothing_is_written_when_the_import_raises_midway(monkeypatch):
    """All-or-nothing. A half-applied migration is the worst outcome of all."""
    system, mrn = _system(), _mrn("ATOMIC")
    data = _csv(PATIENT_HEADER, f"{_ext('P')},{mrn},Atomic Patient,1971-04-02,M,1")

    boom = RuntimeError("disk fell over mid-import")

    def _explode(*a, **kw):
        raise boom

    monkeypatch.setattr(hi, "_record_run", _explode)
    with pytest.raises(RuntimeError):
        hi.apply_import(data, "patients", PATIENT_MAPPING, source_system=system,
                        actor_user_id="system")

    assert _patient_by_mrn(mrn) is None, "a failed import left a patient behind"
    with db_session() as db:
        runs = db.execute("SELECT COUNT(*) c FROM history_import_runs "
                          "WHERE source_system = ?", (system,)).fetchone()
        xref = db.execute("SELECT COUNT(*) c FROM history_import_patient_xref "
                          "WHERE source_system = ?", (system,)).fetchone()
    assert runs["c"] == 0, "a failed import left its ledger row behind"
    assert xref["c"] == 0, "a failed import left a patient id mapping behind"


def test_rejects_are_flushed_in_batches_not_held_in_memory(monkeypatch):
    """A mis-mapped column fails EVERY row — the likeliest failure there is.

    Buffering all of those to write at the end would blow up memory on exactly
    the file the operator most needs a report for, so rejects are flushed as
    they are found. Batch size is shrunk here so the batched path runs.
    """
    monkeypatch.setattr(hi, "BATCH_SIZE", 4)
    system = _system()
    rows = [f"{_ext('P')},,Row {i},1970-01-01,M,1" for i in range(21)]
    result = hi.apply_import(_csv(PATIENT_HEADER, *rows), "patients", PATIENT_MAPPING,
                             source_system=system, actor_user_id="system")

    assert result["summary"]["failed"] == 21
    with db_session() as db:
        stored = db.execute("SELECT COUNT(*) c FROM history_import_rejects WHERE run_id = ?",
                            (result["run_id"],)).fetchone()
    assert stored["c"] == 21, "batched reject flushing lost rows"


# ══════════════════════════════════════════════════════════════════════════
#  Streaming — a large file must not be held in memory
# ══════════════════════════════════════════════════════════════════════════

def test_generated_row_ids_do_not_collide_at_import_scale():
    """Regression, found on a 50 000-row rehearsal — not by any unit test.

    Row ids were minted as PREFIX + uuid4().hex[:8], the pattern the rest of Orb
    uses. That is 32 bits, which by the birthday bound collides with probability
    ~25% over 50 000 rows and ~99% over this module's 200 000-row cap. Because
    the import is one all-or-nothing transaction, the collision lands at the END:
    the whole migration rolls back after doing all the work, with an opaque
    UNIQUE-constraint error naming no row.

    Asserted on the id space rather than by importing 200 000 rows, which would
    make the suite unusable — but the number below is the one that matters.
    """
    assert hi._ID_HEX >= 16, (
        f"row ids carry only {hi._ID_HEX * 4} bits of randomness; a bulk import "
        f"of {hi.MAX_ROWS:,} rows needs at least 64"
    )
    minted = {hi._new_id("PAT-HI") for _ in range(200_000)}
    assert len(minted) == 200_000, "generated ids collided at the row cap"


def test_import_reads_from_a_file_path_without_loading_it_whole(tmp_path):
    system = _system()
    path = tmp_path / "history.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(PATIENT_HEADER.split(","))
        for i in range(500):
            writer.writerow([_ext("P"), _mrn(f"S{i}"), f"Streamed {i}",
                             "1971-04-02", "M", "9876500000"])

    result = hi.plan(str(path), "patients", PATIENT_MAPPING, source_system=system)
    assert result["counts"]["total_rows"] == 500
    assert result["counts"]["create"] == 500
    # detail is capped by default; the counts are not
    assert len(result["rows"]) <= hi.DEFAULT_DETAIL_LIMIT


# ══════════════════════════════════════════════════════════════════════════
#  Contract with roster_import's proven parsing primitives
# ══════════════════════════════════════════════════════════════════════════

def test_reused_roster_import_primitives_still_behave_as_this_module_expects():
    """This module reuses roster import's hard-won dirty-data helpers rather
    than forking them. Nothing on roster import's side tests that contract, so
    it is pinned here — otherwise an edit there breaks history import silently.
    """
    from services import roster_import as ri

    assert ri._clean("  Mr.   Ramesh  ") == "Mr. Ramesh"
    assert ri._clean("N/A") == ""
    assert ri._parse_date("12-Mar-1988", "dob", 1, "auto")[0] == "1988-03-12"
    assert ri._parse_date("03/04/1990", "dob", 1, "auto")[0] is None
    assert ri._decode("plain")[0] == "plain"
    assert ri._normalise_mrn(" M 100 ") == "M100"
    assert ri.Issue(1, "c", "error", "m").to_dict()["severity"] == "error"


# ══════════════════════════════════════════════════════════════════════════
#  HTTP surface — admin only, audited
# ══════════════════════════════════════════════════════════════════════════

def _upload(path_suffix, admin, files, data):
    return client.post(f"/api/admin/history-import/{path_suffix}",
                       headers=_auth(admin), files=files, data=data)


def test_endpoints_require_admin(doctor_user, admin_user):
    payload = _csv(PATIENT_HEADER, f"{_ext('P')},{_mrn('RBAC')},X,1971-04-02,M,1")
    files = {"file": ("h.csv", payload, "text/csv")}
    form = {"record_type": "patients", "source_system": _system(),
            "mapping": json.dumps(PATIENT_MAPPING)}

    denied = client.post("/api/admin/history-import/dry-run",
                         headers=_auth(doctor_user, "doctor"), files=files, data=form)
    assert denied.status_code == 403

    files = {"file": ("h.csv", payload, "text/csv")}
    allowed = client.post("/api/admin/history-import/dry-run",
                          headers=_auth(admin_user), files=files, data=form)
    assert allowed.status_code == 200


def test_commit_endpoint_writes_an_audit_row(admin_user):
    system, ext, mrn = _system(), _ext("P"), _mrn("HTTP")
    payload = _csv(PATIENT_HEADER, f"{ext},{mrn},Http Patient,1971-04-02,M,1")
    resp = _upload("commit", admin_user,
                   {"file": ("patients.csv", payload, "text/csv")},
                   {"record_type": "patients", "source_system": system,
                    "mapping": json.dumps(PATIENT_MAPPING)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"]["created"] == 1

    with db_session() as db:
        audit = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'history_import_committed' "
            "AND target_id = ?", (body["run_id"],)).fetchone()
    assert audit is not None, "a bulk PHI write committed without an audit row"
    assert audit["actor_user_id"] == admin_user


def test_preview_suggests_a_mapping_from_legacy_header_names(admin_user):
    payload = _csv("Patient ID,UHID,Patient Name,Date of Birth,Gender,Mobile",
                   f"{_ext('P')},{_mrn('SNIFF')},Ramesh Kumar,1971-04-02,M,9876500000")
    resp = _upload("preview", admin_user,
                   {"file": ("patients.csv", payload, "text/csv")},
                   {"record_type": "patients"})
    assert resp.status_code == 200, resp.text
    suggested = resp.json()["suggested_mapping"]
    assert suggested.get("mrn") == "UHID"
    assert suggested.get("name") == "Patient Name"
    assert suggested.get("date_of_birth") == "Date of Birth"


def test_rejects_endpoint_returns_the_durable_failure_list(admin_user):
    system = _system()
    rows = [f"{_ext('P')},,Bad {i},1970-01-01,M,1" for i in range(4)]
    payload = _csv(PATIENT_HEADER, *rows)
    commit = _upload("commit", admin_user,
                     {"file": ("patients.csv", payload, "text/csv")},
                     {"record_type": "patients", "source_system": system,
                      "mapping": json.dumps(PATIENT_MAPPING)})
    run_id = commit.json()["run_id"]

    resp = client.get(f"/api/admin/history-import/runs/{run_id}/rejects",
                      headers=_auth(admin_user))
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 4
    assert all(r["code"] == "missing_mrn" for r in body["rejects"])


def test_the_erasure_plan_also_removes_their_cross_system_id_mapping(admin_user):
    """The migration docstring claims the erasure walk discovers the new tables.

    routes/admin_routes._build_erase_plan derives its plan from the live schema
    by finding every table with a patient_id column, precisely so a table added
    by a later migration is purged the day it appears. This asserts that held
    for the two tables migration 15 added: an old-system patient id and a
    quoted source row are both personal data, and a destruction that silently
    leaves them behind is not one.

    The one-click route that used to run this plan is retired (410, see
    test_legacy_erase_retired.py), so the plan is exercised directly, inside a
    transaction that is rolled back.
    """
    from routes.admin_routes import _build_erase_plan

    system, ext, mrn = _system(), _ext("P"), _mrn("ERASE")
    result = _import_patient(system, ext, mrn)
    patient_id = _patient_by_mrn(mrn)["id"]

    # a reject that DID resolve to this patient, so it is discoverable
    with db_session() as db:
        db.execute(
            """INSERT INTO history_import_rejects
                   (run_id, line, severity, code, message, external_id, patient_id, raw_row)
               VALUES (?, 2, 'error', 'test_case', 'test', ?, ?, 'raw,phi,row')""",
            (result["run_id"], ext, patient_id))

    class _Rollback(Exception):
        pass

    try:
        with db_session() as db:
            plan = {table: (sql, n) for table, sql, n in _build_erase_plan(db)}
            assert "history_import_patient_xref" in plan, \
                "the erasure plan does not reach the legacy patient id mapping"
            assert "history_import_rejects" in plan, \
                "the erasure plan does not reach PHI-bearing reject rows"
            for table in ("history_import_patient_xref", "history_import_rejects"):
                sql, n = plan[table]
                db.execute(sql, (patient_id,) * n)
                left = db.execute(f"SELECT COUNT(*) c FROM {table} WHERE patient_id = ?",
                                  (patient_id,)).fetchone()["c"]
                assert left == 0, f"the plan's statement for {table} left rows behind"
            raise _Rollback
    except _Rollback:
        pass


def test_unknown_record_type_is_refused(admin_user):
    payload = _csv(PATIENT_HEADER, f"{_ext('P')},{_mrn('BAD')},X,1971-04-02,M,1")
    resp = _upload("dry-run", admin_user,
                   {"file": ("h.csv", payload, "text/csv")},
                   {"record_type": "wildlife", "source_system": _system(),
                    "mapping": json.dumps(PATIENT_MAPPING)})
    assert resp.status_code == 422


def test_source_system_is_required(admin_user):
    payload = _csv(PATIENT_HEADER, f"{_ext('P')},{_mrn('NOSYS')},X,1971-04-02,M,1")
    resp = _upload("dry-run", admin_user,
                   {"file": ("h.csv", payload, "text/csv")},
                   {"record_type": "patients", "source_system": "  ",
                    "mapping": json.dumps(PATIENT_MAPPING)})
    assert resp.status_code == 422
