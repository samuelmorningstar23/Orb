"""Allergy add and deactivate from the chart after admission (founding ward
list item 5, 21 October 2026).

The routes and the interlock existed; the chart's controls use them. An
allergy added with a reaction and a severity on day three reaches
patient_allergies, the list every safety check reads, and the order gate
refuses the matching drug at once. A deactivation needs a reason, which goes
to the audit chain with the substance; the row stays as inactive. The
permissions are the routes' own: patient.write, the doctor's.
"""

import json
import uuid

from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session, verify_audit_chain
from main import app
from services.admission_service import admit_patient
from services.clinical_records_service import list_allergies

client = TestClient(app)
_SEEDED: list[str] = []


def _user(role="doctor", department="General Medicine"):
    uid = f"ALC{uuid.uuid4().hex[:6].upper()}"
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


def _admit(department="General Medicine"):
    doc = _user(department=department)
    res = admit_patient(
        {
            "name": "Chart Allergy",
            "ward": "GM-1",
            "bed": "4",
            "department": department,
            "attending_doctor_id": doc["id"],
            "chief_complaint": "Fever",
            "allergies": [],
        },
        doc,
    )
    _SEEDED.append(res["patient_id"])
    return res["patient_id"], doc


def _order(hdr, pid, drug):
    return client.post(
        f"/api/vigil/patients/{pid}/medications",
        json={"drug_name": drug, "dosage": "500mg", "frequency": "TDS"},
        headers=hdr,
    )


def _audit(action, target_id):
    with db_session() as db:
        row = db.execute(
            "SELECT actor_user_id, actor_role, summary_message, metadata_json FROM audit_logs "
            "WHERE action_type = ? AND target_type = 'patient_allergy' AND target_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (action, str(target_id)),
        ).fetchone()
    assert row is not None, f"no {action} audit row for allergy {target_id}"
    return dict(row)


def teardown_module(module):
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?", [(p,) for p in _SEEDED]
        )
        db.executemany(
            "UPDATE admissions SET status = 'discharged' WHERE patient_id = ? AND status = 'active'",
            [(p,) for p in _SEEDED],
        )
    _SEEDED.clear()


def test_an_allergy_added_on_day_three_reaches_the_list_and_the_order_gate():
    pid, doc = _admit()
    hdr = _hdr(doc["id"])
    assert list_allergies(pid) == []
    assert _order(hdr, pid, "Amoxicillin 500mg").status_code in (200, 201), "no allergy yet"

    r = client.post(
        f"/api/patients/{pid}/allergies",
        json={"substance": "Penicillin", "reaction": "anaphylaxis", "severity": "severe"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    added = r.json()
    assert (added["substance"], added["reaction"], added["severity"], added["status"]) == (
        "penicillin", "anaphylaxis", "severe", "active",
    )
    assert added["source"] == "clinician" and added["recorded_by"] == doc["id"]
    rows = list_allergies(pid)
    assert [(a["substance"], a["reaction"], a["severity"]) for a in rows] == [
        ("penicillin", "anaphylaxis", "severe")
    ]
    audit = _audit("allergy_recorded", added["id"])
    meta = json.loads(audit["metadata_json"])
    assert meta == {
        "substance": "penicillin", "reaction": "anaphylaxis", "severity": "severe",
        "source": "clinician",
    }
    assert audit["actor_user_id"] == doc["id"] and audit["actor_role"] == "doctor"

    # The gate every order passes reads the new row at once.
    resp = _order(hdr, pid, "Amoxicillin 500mg")
    assert resp.status_code == 428, resp.text
    assert resp.json()["detail"]["safety"]["allergy_alerts"]


def test_a_severity_outside_the_four_grades_is_refused():
    pid, doc = _admit()
    r = client.post(
        f"/api/patients/{pid}/allergies",
        json={"substance": "iodine", "severity": "catastrophic"},
        headers=_hdr(doc["id"]),
    )
    assert r.status_code == 422, r.text
    assert list_allergies(pid) == []


def test_a_blank_substance_is_refused():
    pid, doc = _admit()
    r = client.post(
        f"/api/patients/{pid}/allergies", json={"substance": "   "}, headers=_hdr(doc["id"])
    )
    assert r.status_code == 422


def test_deactivation_needs_a_reason_and_puts_it_on_the_chain():
    pid, doc = _admit()
    hdr = _hdr(doc["id"])
    r = client.post(
        f"/api/patients/{pid}/allergies",
        json={"substance": "penicillin", "reaction": "rash as a child", "severity": "mild"},
        headers=hdr,
    )
    allergy_id = r.json()["id"]

    # No reason: refused, and the allergy stays active.
    for body in ({"reason": ""}, {"reason": "   "}, {}):
        bad = client.post(
            f"/api/patients/{pid}/allergies/{allergy_id}/deactivate", json=body, headers=hdr
        )
        assert bad.status_code == 422, (body, bad.text)
    assert [a["status"] for a in list_allergies(pid, include_inactive=True)] == ["active"]

    ok = client.post(
        f"/api/patients/{pid}/allergies/{allergy_id}/deactivate",
        json={"reason": "  Tolerated a full amoxicillin course in 2025; de-labelled on review  "},
        headers=hdr,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "inactive"
    rows = list_allergies(pid, include_inactive=True)
    assert [(a["substance"], a["status"]) for a in rows] == [("penicillin", "inactive")]
    assert list_allergies(pid) == [], "the row is kept, not deleted, and leaves the active list"

    audit = _audit("allergy_deactivated", allergy_id)
    meta = json.loads(audit["metadata_json"])
    assert meta["reason"] == "Tolerated a full amoxicillin course in 2025; de-labelled on review"
    assert meta["substance"] == "penicillin" and meta["severity"] == "mild"
    assert "penicillin" in audit["summary_message"] and "de-labelled" in audit["summary_message"]
    assert audit["actor_user_id"] == doc["id"]
    assert verify_audit_chain()["ok"] is True

    # Deactivated, the gate lets the drug through again.
    assert _order(hdr, pid, "Amoxicillin 500mg").status_code in (200, 201)


def test_deactivation_is_bound_to_the_patient_and_the_ward():
    victim, _ = _admit(department="Oncology")
    own, _ = _admit(department="Oncology")
    onc = _hdr(_user(department="Oncology")["id"])
    allergy_id = client.post(
        f"/api/patients/{victim}/allergies",
        json={"substance": "penicillin", "severity": "severe"},
        headers=onc,
    ).json()["id"]
    wrong_patient = client.post(
        f"/api/patients/{own}/allergies/{allergy_id}/deactivate",
        json={"reason": "wrong path"},
        headers=onc,
    )
    assert wrong_patient.status_code == 404
    other_ward = client.post(
        f"/api/patients/{victim}/allergies/{allergy_id}/deactivate",
        json={"reason": "other ward"},
        headers=_hdr(_user(department="Cardiology")["id"]),
    )
    assert other_ward.status_code == 404
    assert [a["status"] for a in list_allergies(victim)] == ["active"]


def test_a_nurse_and_a_clerk_cannot_change_the_list_and_a_nurse_can_read_it():
    pid, doc = _admit()
    allergy_id = client.post(
        f"/api/patients/{pid}/allergies",
        json={"substance": "penicillin", "severity": "severe"},
        headers=_hdr(doc["id"]),
    ).json()["id"]
    nurse = _hdr(_user(role="nurse")["id"])
    clerk = _hdr(_user(role="ward_clerk")["id"])
    assert client.post(
        f"/api/patients/{pid}/allergies", json={"substance": "iodine"}, headers=nurse
    ).status_code == 403
    assert client.post(
        f"/api/patients/{pid}/allergies/{allergy_id}/deactivate",
        json={"reason": "nurse"},
        headers=nurse,
    ).status_code == 403
    assert client.post(
        f"/api/patients/{pid}/allergies", json={"substance": "iodine"}, headers=clerk
    ).status_code == 403
    listed = client.get(f"/api/patients/{pid}/allergies", headers=nurse)
    assert listed.status_code == 200
    assert [a["substance"] for a in listed.json()["allergies"]] == ["penicillin"]
    assert [a["status"] for a in list_allergies(pid)] == ["active"]
