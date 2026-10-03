"""Allergies typed at admission or in a roster reach the allergy interlock.

Before 2026-10-02 admit_patient kept the allergy chips only in the
patient_profiles narrative, so patient_allergies stayed empty and the
order-time interlock cleared amoxicillin for a patient admitted as
penicillin-allergic. Also pins the patient binding on the allergy DELETE and
problem resolve routes, and the audit row on resolve.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app
from services import roster_import
from services.admission_service import admit_patient
from services.clinical_records_service import (
    add_allergy, add_problem, allergy_entries, backfill_profile_allergies,
    list_allergies,
)

client = TestClient(app)
_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded():
    """Close what this module admits, so the shared session DB does not carry
    extra admitted patients into later modules (see test_admission_provenance)."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _SEEDED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'",
                       [(p,) for p in _SEEDED])
    _SEEDED.clear()


def _user(role="doctor", department=None):
    uid = f"ALG{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?,?,?,?,?)",
            (uid, f"Dr {uid}", role, department, hash_password("secretpass")),
        )
    return {"id": uid, "name": f"Dr {uid}", "role": role, "department": department}


def _hdr(uid):
    resp = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _admit(allergies, department="General Medicine"):
    doc = _user()
    res = admit_patient({
        "name": "Allergy Charted", "ward": "GM-1", "bed": "4",
        "department": department, "attending_doctor_id": doc["id"],
        "chief_complaint": "Fever", "allergies": allergies,
    }, doc)
    _SEEDED.append(res["patient_id"])
    return res["patient_id"], doc


def _order(hdr, pid, drug):
    return client.post(f"/api/vigil/patients/{pid}/medications",
                       json={"drug_name": drug, "dosage": "500mg", "frequency": "TDS"},
                       headers=hdr)


# ── allergy_entries ───────────────────────────────────────────────────────────
def test_allergy_entries_splits_lists_and_drops_nothing_to_record():
    assert allergy_entries(["Penicillin", "NKDA", "  sulfa ; Iodine ", "penicillin"]) == [
        "Penicillin", "sulfa", "Iodine"]
    # Brackets and combination products stay whole.
    assert allergy_entries("Amoxicillin (rash, mild), amoxicillin/clavulanate") == [
        "Amoxicillin (rash, mild)", "amoxicillin/clavulanate"]
    assert allergy_entries(["No known drug allergies", "nil", ""]) == []
    assert allergy_entries(None) == []


# ── admission ─────────────────────────────────────────────────────────────────
def test_admission_charts_allergy_chips_as_structured_allergies():
    pid, doc = _admit(["Penicillin", "NKDA"])
    rows = list_allergies(pid)
    assert [r["substance"] for r in rows] == ["penicillin"]
    row = rows[0]
    assert row["status"] == "active"
    assert row["source"] == "admission"
    assert row["severity"] == "unknown"
    assert row["reaction"] is None
    assert row["recorded_by"] == doc["id"]
    # The narrative is kept as typed.
    with db_session() as db:
        prof = db.execute("SELECT allergies FROM patient_profiles WHERE patient_id = ?",
                          (pid,)).fetchone()
    assert json.loads(prof["allergies"]) == ["Penicillin", "NKDA"]


def test_admitted_penicillin_allergy_blocks_amoxicillin_order():
    pid, doc = _admit(["penicillin"])
    resp = _order(_hdr(doc["id"]), pid, "Amoxicillin 500mg")
    assert resp.status_code == 428, resp.text
    assert resp.json()["detail"]["safety"]["allergy_alerts"]
    with db_session() as db:
        n = db.execute("SELECT count(*) FROM medications WHERE patient_id = ?",
                       (pid,)).fetchone()[0]
    assert n == 0


def test_admission_without_allergies_charts_none():
    pid, _ = _admit([])
    assert list_allergies(pid, include_inactive=True) == []


# ── roster import ─────────────────────────────────────────────────────────────
def _roster(mrn, allergies):
    return ("MRN,Patient Name,Ward,Allergies\n"
            f"{mrn},Roster Allergy,GM-1,\"{allergies}\"\n")


def test_roster_import_suggests_and_charts_allergies():
    mrn = f"RA{uuid.uuid4().hex[:6].upper()}"
    admin = _user(role="admin")
    content = _roster(mrn, "Penicillin; NKDA")
    sniffed = roster_import.sniff(content)
    assert sniffed["suggested_mapping"].get("allergies") == "Allergies"
    mapping = {"mrn": "MRN", "name": "Patient Name", "ward": "Ward", "allergies": "Allergies"}

    plan = roster_import.plan(content, mapping, default_department="General Medicine")
    assert plan["counts"]["allergies_add"] == 1
    assert plan["rows"][0]["allergies"]["add"] == ["Penicillin"]

    result = roster_import.apply_import(content, mapping, actor_user_id=admin["id"],
                                        default_department="General Medicine")
    pid = result["created_patient_ids"][0]
    _SEEDED.append(pid)
    assert result["allergies"] == {"charted": 1}
    rows = list_allergies(pid)
    assert [(r["substance"], r["source"]) for r in rows] == [("penicillin", "roster_import")]

    doc = _user()
    resp = _order(_hdr(doc["id"]), pid, "Amoxicillin 500mg")
    assert resp.status_code == 428, resp.text

    # Re-import is idempotent and never re-arms an allergy a clinician
    # deactivated: it is reported instead.
    with db_session() as db:
        db.execute("UPDATE patient_allergies SET status = 'inactive' WHERE patient_id = ?",
                   (pid,))
    again = roster_import.apply_import(content, mapping, actor_user_id=admin["id"],
                                       default_department="General Medicine")
    assert again["allergies"] == {"charted": 0}
    codes = [i["code"] for i in again["rows"][0]["issues"]]
    assert "allergy_inactive_in_orb" in codes
    assert list_allergies(pid) == []


def test_roster_suggests_hospital_no_as_mrn():
    mapping = roster_import.suggest_mapping(["Hospital No.", "Patient Name"])
    assert mapping.get("mrn") == "Hospital No."


# ── backfill of patients admitted before the fix ─────────────────────────────
def _bare_patient_with_profile(allergies_json):
    pid = f"PBF{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?,?,?, 'discharged')",
                   (pid, "Backfill Patient", f"MRN-{pid}"))
        db.execute("INSERT INTO patient_profiles (patient_id, allergies, updated_at) "
                   "VALUES (?, ?, '2026-09-01T08:00:00+00:00')", (pid, allergies_json))
    return pid


def test_backfill_charts_profile_allergies_once_and_leaves_free_text():
    listed = _bare_patient_with_profile(json.dumps(["Sulfa drugs", "NKDA"]))
    narrative = _bare_patient_with_profile("allergic to penicillin, causes rash")
    deactivated = _bare_patient_with_profile(json.dumps(["Iodine"]))
    with db_session() as db:
        db.execute("INSERT INTO patient_allergies (patient_id, substance, status) "
                   "VALUES (?, 'iodine', 'inactive')", (deactivated,))

    with db_session() as db:
        first = backfill_profile_allergies(db)
    assert first["skipped_free_text"] >= 1
    rows = list_allergies(listed)
    assert [(r["substance"], r["source"]) for r in rows] == [("sulfa drugs", "admission")]
    assert rows[0]["recorded_at"] == "2026-09-01T08:00:00+00:00"
    assert list_allergies(narrative, include_inactive=True) == []
    assert list_allergies(deactivated) == [], "a clinician's deactivation must stand"

    with db_session() as db:
        backfill_profile_allergies(db)
    assert len(list_allergies(listed, include_inactive=True)) == 1


# ── route scoping ─────────────────────────────────────────────────────────────
def test_cross_ward_allergy_delete_is_404_and_changes_nothing():
    pid, _ = _admit(["penicillin"], department="Oncology")
    allergy_id = list_allergies(pid)[0]["id"]
    cardiology = _user(department="Cardiology")
    resp = client.delete(f"/api/patients/{pid}/allergies/{allergy_id}",
                         headers=_hdr(cardiology["id"]))
    assert resp.status_code == 404
    assert [r["status"] for r in list_allergies(pid)] == ["active"]


def test_allergy_delete_through_another_patient_path_is_404():
    victim, _ = _admit(["penicillin"], department="Oncology")
    own, _ = _admit([], department="Oncology")
    allergy_id = list_allergies(victim)[0]["id"]
    oncology = _user(department="Oncology")
    resp = client.delete(f"/api/patients/{own}/allergies/{allergy_id}",
                         headers=_hdr(oncology["id"]))
    assert resp.status_code == 404
    assert [r["status"] for r in list_allergies(victim)] == ["active"]

    ok = client.delete(f"/api/patients/{victim}/allergies/{allergy_id}",
                       headers=_hdr(oncology["id"]))
    assert ok.status_code == 200, ok.text
    assert list_allergies(victim) == []


def test_problem_resolve_is_bound_to_patient_and_audited():
    pid, _ = _admit([], department="Oncology")
    other, _ = _admit([], department="Oncology")
    problem = add_problem(pid, "Community-acquired pneumonia")
    hdr = _hdr(_user(department="Oncology")["id"])

    wrong = client.patch(f"/api/patients/{other}/problems/{problem['id']}/resolve", headers=hdr)
    assert wrong.status_code == 404

    resp = client.patch(f"/api/patients/{pid}/problems/{problem['id']}/resolve", headers=hdr)
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        log = db.execute(
            "SELECT outcome, target_id FROM audit_logs WHERE action_type = 'problem_resolved' "
            "AND patient_id = ? ORDER BY timestamp DESC LIMIT 1", (pid,)).fetchone()
    assert log is not None, "resolving a problem must be audited"
    assert log["target_id"] == str(problem["id"])


def test_add_allergy_still_upserts_after_admission_row():
    """The allergy form can grade an admission-charted allergy."""
    pid, _ = _admit(["penicillin"])
    graded = add_allergy(pid, "Penicillin", reaction="anaphylaxis", severity="severe")
    assert graded["severity"] == "severe"
    assert len(list_allergies(pid)) == 1
