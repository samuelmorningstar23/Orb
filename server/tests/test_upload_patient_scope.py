"""/uploads per-patient authorization.

The route used to require only "any clinical role": a Cardiology doctor could
fetch an Oncology patient's radiology image or consultation audio by filename.
serve_upload now resolves the media to its owning patient and enforces the
caller's ward scope against that patient with the API's established
out-of-scope semantics (404, never 403 — a 403 confirms the patient exists; the
detail is this route's uniform "Not found" so the caller cannot even learn the
filename maps to a registered record). The fail-closed media_view audit write
stays: no durable row, no media.
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from config import UPLOAD_DIR
from database import db_session
from main import app

client = TestClient(app)

DEPT_A = "UploadScope Cardiology"
DEPT_B = "UploadScope Oncology"

_SEEDED_PATIENTS: list[str] = []
_SEEDED_FILES: list[str] = []


@pytest.fixture(autouse=True)
def _cleanup():
    """Discharge seeded patients (they default to 'admitted' and would slow the
    nightly-FHIR-export tests — same isolation law as test_compliance.py) and
    remove the media files dropped into the real UPLOAD_DIR."""
    yield
    if _SEEDED_PATIENTS:
        with db_session() as db:
            db.executemany(
                "UPDATE patients SET status = 'discharged' WHERE id = ?",
                [(p,) for p in _SEEDED_PATIENTS],
            )
        _SEEDED_PATIENTS.clear()
    for path in _SEEDED_FILES:
        if os.path.exists(path):
            os.remove(path)
    _SEEDED_FILES.clear()


def _mk_user(role, department):
    uid = f"UPSC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Upload Scope {role}", role, department),
        )
    return uid


def _auth(uid, role="doctor"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _mk_patient_with_media(department):
    """A patient whose department lives on the ADMISSION (the scoped column),
    plus a registered lens_analyses row pointing at a real file in UPLOAD_DIR."""
    author = _mk_user("doctor", department)
    pid = f"UPSCP{uuid.uuid4().hex[:8].upper()}"
    fname = f"upsc_{uuid.uuid4().hex[:10]}.png"
    fpath = os.path.join(str(UPLOAD_DIR), fname)
    with open(fpath, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n upload-scope test bytes")
    _SEEDED_FILES.append(fpath)
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
            (pid, "Upload Scope Patient", f"MRN-{uuid.uuid4().hex[:6].upper()}"),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'B1', 'active', ?)",
            (pid, department, department, author),
        )
        db.execute(
            "INSERT INTO lens_analyses (patient_id, author_id, image_path, analysis) "
            "VALUES (?, ?, ?, ?)",
            (pid, author, f"uploads/{fname}", "upload scope test analysis"),
        )
    _SEEDED_PATIENTS.append(pid)
    return pid, fname


def _media_view_rows(actor_id):
    with db_session() as db:
        return db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'media_view' AND actor_user_id = ?",
            (actor_id,),
        ).fetchall()


def test_same_department_doctor_is_served_and_audited():
    pid, fname = _mk_patient_with_media(DEPT_A)
    doc = _mk_user("doctor", DEPT_A)
    r = client.get(f"/uploads/{fname}", headers=_auth(doc))
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"\x89PNG")
    rows = _media_view_rows(doc)
    assert rows and rows[-1]["patient_id"] == pid


def test_out_of_scope_doctor_is_refused_with_uniform_404():
    """A doctor from another ward gets the same 404 as a nonexistent file — not
    a 403, not a different detail string — and no media_view row is written."""
    pid, fname = _mk_patient_with_media(DEPT_A)
    outsider = _mk_user("doctor", DEPT_B)
    r = client.get(f"/uploads/{fname}", headers=_auth(outsider))
    assert r.status_code == 404, r.text
    r_ghost = client.get("/uploads/upsc_no_such_file.png", headers=_auth(outsider))
    assert r_ghost.status_code == 404
    assert r.json() == r_ghost.json()
    assert b"PNG" not in r.content
    assert _media_view_rows(outsider) == []


def test_unscoped_account_still_reads_hospital_wide():
    """department None is the established governance-account convention — the
    scope check must not be over-applied to it."""
    pid, fname = _mk_patient_with_media(DEPT_A)
    governance = _mk_user("doctor", None)
    r = client.get(f"/uploads/{fname}", headers=_auth(governance))
    assert r.status_code == 200, r.text


def test_media_not_served_when_audit_write_fails(monkeypatch):
    """phi_access is FAIL-CLOSED: if the media_view row cannot be durably
    persisted, the PHI is not served (503), even for an in-scope caller."""
    import services.audit_service as audit_service

    pid, fname = _mk_patient_with_media(DEPT_A)
    doc = _mk_user("doctor", DEPT_A)

    def _refuse(*args, **kwargs):
        raise audit_service.AuditWriteError("simulated audit outage")

    monkeypatch.setattr(audit_service, "log_audit_event", _refuse)
    r = client.get(f"/uploads/{fname}", headers=_auth(doc))
    assert r.status_code == 503
    assert b"PNG" not in r.content
