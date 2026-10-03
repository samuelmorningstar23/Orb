"""DPDP erasure of dictation audio, and what an erasure says it kept.

Two defects, one flow (record, adjudicate, execute through the HTTP routes):

  * ambient_audio was "honoured but not executable": scribe_notes.audio_path
    named a file on the appliance's disk and nothing ever removed it. The
    executor now detaches the pointer inside the transaction and unlinks the
    file only after the commit, so a rolled-back execution destroys nothing.
  * an executed erasure said nothing about the append-only stores that still
    name the patient. shadow_events.phi_json can hold a recorder's free text,
    so the result now carries a `retained` list with the reason for each.

The patient here has shadow, consent and withdrawal rows, the shape that used to
abort the one-click erase outright. Teardown lifts the append-only triggers to
clean up, then restores them, the same convention as test_dpdp_compliance.py.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app

client = TestClient(app)

_APPEND_ONLY_TRIGGERS = (
    "trg_consent_records_no_update", "trg_consent_records_no_delete",
    "trg_consent_withdrawals_no_update", "trg_consent_withdrawals_no_delete",
    "trg_erasure_decisions_no_update", "trg_erasure_decisions_no_delete",
    "trg_erasure_executions_no_update", "trg_erasure_executions_no_delete",
    "trg_breach_notifications_no_update", "trg_breach_notifications_no_delete",
)


def _auth(uid: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, 'admin')}"}


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    root.mkdir()
    # consent_service reads config.UPLOAD_DIR at call time.
    monkeypatch.setattr(config, "UPLOAD_DIR", root)
    return root


@pytest.fixture
def case(upload_dir, tmp_path):
    """A discharged patient with dictation audio, shadow, consent and withdrawal rows."""
    tag = uuid.uuid4().hex[:8].upper()
    pid, admin, doc = f"EAUD{tag}", f"EAADM{tag}", f"EADOC{tag}"
    inside = upload_dir / f"{uuid.uuid4().hex}.webm"
    inside.write_bytes(b"dictation")
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside = outside_dir / "not-ours.webm"
    outside.write_bytes(b"keep me")
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with db_session() as db:
        # department NULL: an unscoped governance account, as the erasure desk is.
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES (?, 'Erasure Admin', 'admin', 'x')", (admin,))
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES (?, 'Erasure Doctor', 'doctor', 'x')", (doc,))
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, diagnosis, status) "
            "VALUES (?, 'Erasure Audio Fixture', ?, 52, 'F', 'Fixture', 'discharged')",
            (pid, f"MRN-{pid}"))
        for path in (str(inside), str(outside)):
            db.execute(
                "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript, "
                "note_format, audio_path) VALUES (?, ?, 'signed note text', 'soap', ?)",
                (pid, doc, path))
        # Simulator lane, so teardown can remove it; the retained list counts it
        # the same as any other lane.
        db.execute(
            "INSERT INTO shadow_events (episode_id, kind, occurred_at, recorded_at, lane, "
            "patient_id, payload_json, phi_json) VALUES (?, 'clinical.action', "
            "'2001-01-01T00:00:00.000+00:00', ?, 'simulator', ?, '{}', ?)",
            (f"EP-{pid}", now, pid, json.dumps({"note": "free text"})))
        cid = f"CON-{pid}"
        db.execute(
            "INSERT INTO consent_records (id, patient_id, purpose, method, granted_at, "
            "recorded_by) VALUES (?, ?, 'treatment', 'written_signature', ?, ?)",
            (cid, pid, now, doc))
        db.execute(
            "INSERT INTO consent_withdrawals (id, consent_id, patient_id, withdrawn_at, "
            "method, recorded_by) VALUES (?, ?, ?, ?, 'verbal_witnessed', ?)",
            (f"WD-{pid}", cid, pid, now, doc))
    yield {"pid": pid, "admin": admin, "inside": inside, "outside": outside}

    import migrations
    with db_session() as db:
        for trg in _APPEND_ONLY_TRIGGERS:
            db.execute(f"DROP TRIGGER IF EXISTS {trg}")
        for table in ("erasure_executions", "erasure_decisions", "erasure_requests",
                      "consent_withdrawals", "consent_records", "scribe_notes"):
            db.execute(f"DELETE FROM {table} WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM shadow_events WHERE patient_id = ? AND lane = 'simulator'",
                   (pid,))
        db.execute("DELETE FROM patients WHERE id = ?", (pid,))
        migrations._m14_dpdp_act_machinery(db)


def _erase_audio(case) -> "object":
    h = _auth(case["admin"])
    req = client.post("/api/admin/compliance/erasure", headers=h,
                      json={"patient_id": case["pid"], "categories": ["ambient_audio"]})
    assert req.status_code == 200, req.text
    rid = req.json()["id"]
    decided = client.post(f"/api/admin/compliance/erasure/{rid}/adjudicate", headers=h)
    assert decided.status_code == 200, decided.text
    (decision,) = decided.json()["decisions"]
    assert decision["disposition"] == "honoured" and decision["executable"], decision
    return client.post(f"/api/admin/compliance/erasure/{rid}/execute", headers=h)


def _last_executed_audit(pid: str) -> dict:
    with db_session() as db:
        row = db.execute(
            "SELECT outcome, metadata_json FROM audit_logs WHERE action_type = "
            "'erasure_executed' AND patient_id = ? ORDER BY id DESC LIMIT 1",
            (pid,)).fetchone()
    assert row is not None, "the execution left no outcome row"
    return {"outcome": row["outcome"], "meta": json.loads(row["metadata_json"] or "{}")}


def test_erasure_removes_the_audio_file_and_keeps_the_signed_note(case):
    resp = _erase_audio(case)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert not case["inside"].exists(), "the dictation audio is still on disk"
    assert case["outside"].exists(), "a file outside the upload folder was deleted"
    (audio,) = body["categories"]
    assert audio["category"] == "ambient_audio"
    assert audio["detail"]["notes_detached"] == 1
    assert audio["detail"]["kept_outside_upload_folder"] == 1
    assert body["files_not_removed"] == []

    with db_session() as db:
        notes = db.execute(
            "SELECT raw_transcript, audio_path FROM scribe_notes WHERE patient_id = ?",
            (case["pid"],)).fetchall()
    assert len(notes) == 2, "the signed notes are the medical record and must stay"
    pointers = sorted(n["audio_path"] or "" for n in notes)
    assert pointers == ["", str(case["outside"])], pointers

    assert _last_executed_audit(case["pid"])["outcome"] == "success"


def test_an_executed_erasure_names_what_it_kept_and_why(case):
    resp = _erase_audio(case)
    assert resp.status_code == 200, resp.text
    retained = {t["table"]: t for t in resp.json()["retained"]}

    for table in ("shadow_events", "consent_records", "consent_withdrawals",
                  "erasure_requests", "erasure_decisions", "erasure_executions"):
        assert table in retained, f"{table} still holds this patient but was not reported"
        assert retained[table]["rows"] >= 1 and retained[table]["reason"]
    assert "phi_json" in retained["shadow_events"]["reason"], \
        "the free-text column the pilot log keeps must be named"
    assert "shadow_events" in _last_executed_audit(case["pid"])["meta"]["retained"]


def test_a_file_that_will_not_go_is_reported_not_hidden(case, monkeypatch):
    real_unlink = os.unlink

    def refuse(path, *a, **kw):
        if os.path.basename(str(path)) == case["inside"].name:
            raise PermissionError("read-only volume")
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", refuse)
    resp = _erase_audio(case)
    assert resp.status_code == 200, resp.text
    assert resp.json()["files_not_removed"] == [case["inside"].name]
    assert case["inside"].exists()
    audit = _last_executed_audit(case["pid"])
    assert audit["outcome"] == "partial_success"
    assert audit["meta"]["files_not_removed"] == [case["inside"].name]
