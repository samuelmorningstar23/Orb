"""The per-shift nursing note (founding ward list item 4, 21 October 2026).

A nurse writes free text about a patient on her own ward; the note is filed
under the shift running now with her name and the UTC time, audited on the
hash chain in the same transaction, listed newest first, shown on the chart
timeline and the I-PASS handover, carried on the chart payload for the
inpatient record print, and switched off under the Orb Ward Record profile.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import permissions as P
import pytest
from auth import create_token
from database import db_session, verify_audit_chain
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routes.nursing_note_routes import router as nursing_note_router
from routes.vigil_routes import router as vigil_router
from services.clinical_time import parse_to_utc
from services.ipass_service import build_ipass
from services.patient_timeline_service import build_chart_timeline

import config
from services import nursing_note_service as svc
from services import ward_record_service


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nursing_note_router)
    app.include_router(vigil_router)
    return TestClient(app)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _name(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _mk_user(role, department, name=None):
    uid = _name("NN")
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, name or f"Nursing note {role}", role, department),
        )
    return uid


def _mk_patient(department, ward="NN-1"):
    pid = _name("NNP")
    admitted_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('NNADM', 'Nursing note admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, 'Nursing note fixture', ?, '2', ?, 70, 'F', 'Fixture',
                       'admitted', ?)""",
            (pid, f"MRN-{pid}", ward, admitted_at),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
            "admitted_by, created_at, admitted_at) "
            "VALUES (?, ?, ?, ?, '2', 'active', 'NNADM', ?, ?)",
            (f"ADM-{pid}", pid, department, ward, admitted_at, admitted_at),
        )
    return pid


def _write(client, pid, uid, role, content):
    return client.post(
        f"/api/patients/{pid}/nursing-notes",
        json={"content": content},
        headers=_auth(uid, role),
    )


# ── Permissions ───────────────────────────────────────────────────────────────
def test_the_nurse_writes_and_reads_and_the_doctor_only_reads():
    assert P.NURSING_NOTE_WRITE in P.ROLE_PERMISSIONS["nurse"]
    assert P.NURSING_NOTE_READ in P.ROLE_PERMISSIONS["nurse"]
    assert P.NURSING_NOTE_READ in P.ROLE_PERMISSIONS["doctor"]
    assert P.NURSING_NOTE_WRITE not in P.ROLE_PERMISSIONS["doctor"]
    for role in ("ward_clerk", "admin", "pharmacist", "receptionist", "cashier"):
        assert P.NURSING_NOTE_WRITE not in P.ROLE_PERMISSIONS[role], role
        assert P.NURSING_NOTE_READ not in P.ROLE_PERMISSIONS[role], role
    assert P.PATIENT_WRITE not in P.ROLE_PERMISSIONS["nurse"], "the note is not patient.write"


def test_a_doctor_cannot_write_and_a_clerk_cannot_read(client):
    dept = _name("NN Medicine ")
    pid = _mk_patient(dept)
    doctor = _mk_user("doctor", dept)
    clerk = _mk_user("ward_clerk", dept)
    assert _write(client, pid, doctor, "doctor", "Seen on the round.").status_code == 403
    assert (
        client.get(
            f"/api/patients/{pid}/nursing-notes", headers=_auth(clerk, "ward_clerk")
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/patients/{pid}/nursing-notes", headers=_auth(doctor, "doctor")
        ).status_code
        == 200
    )


def test_a_nurse_on_another_ward_gets_a_404_and_a_denial_row(client):
    stranger = _mk_patient(_name("NN Surgery "))
    nurse = _mk_user("nurse", _name("NN Medicine "))
    r = _write(client, stranger, nurse, "nurse", "Not my patient.")
    assert r.status_code == 404, r.text
    with db_session() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM nursing_notes WHERE patient_id = ?", (stranger,)
            ).fetchone()[0]
            == 0
        )
        denied = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'patient_scope_denied' "
            "AND actor_user_id = ? AND patient_id = ?",
            (nurse, stranger),
        ).fetchone()[0]
    assert denied == 1
    assert (
        client.get(
            f"/api/patients/{stranger}/nursing-notes", headers=_auth(nurse, "nurse")
        ).status_code
        == 404
    )


# ── The note itself ──────────────────────────────────────────────────────────
def test_a_note_is_stored_with_shift_author_and_utc_time_and_audited_on_the_chain(client):
    dept = _name("NN Medicine ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept, name="Sister Mary Thomas")
    before = datetime.now(UTC)
    r = _write(client, pid, nurse, "nurse", "  Settled after analgesia. Drinking well.  ")
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["content"] == "Settled after analgesia. Drinking well."
    assert note["author_id"] == nurse
    assert note["author_name"] == "Sister Mary Thomas"
    assert note["patient_id"] == pid
    written = parse_to_utc(note["written_at"])
    assert before - timedelta(seconds=5) <= written <= datetime.now(UTC) + timedelta(seconds=5)
    assert note["written_at"].endswith("+00:00"), "the one stored spelling"
    expected_date, expected_label = ward_record_service.shift_at(written)
    assert (note["shift_date"], note["shift_label"]) == (expected_date, expected_label)

    with db_session() as db:
        row = db.execute("SELECT * FROM nursing_notes WHERE id = ?", (note["id"],)).fetchone()
        assert dict(row)["content"] == note["content"]
        audit = db.execute(
            "SELECT event_category, actor_user_id, actor_role, patient_id, metadata_json "
            "FROM audit_logs WHERE action_type = 'nursing_note_written' AND target_type = "
            "'nursing_note' AND target_id = ?",
            (str(note["id"]),),
        ).fetchone()
    assert audit is not None, "the note and its audit row commit together"
    assert audit["event_category"] == "clinical_record"
    assert audit["actor_user_id"] == nurse and audit["actor_role"] == "nurse"
    assert audit["patient_id"] == pid
    meta = json.loads(audit["metadata_json"])
    assert meta["shift_label"] == expected_label and meta["characters"] == len(note["content"])
    chain = verify_audit_chain()
    assert chain["ok"] is True, chain


@pytest.mark.parametrize("bad", ["", "   ", None, "x" * (svc.MAX_CHARACTERS + 1)])
def test_an_empty_or_over_long_note_is_refused_and_nothing_is_written(client, bad):
    dept = _name("NN Medicine ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept)
    r = client.post(
        f"/api/patients/{pid}/nursing-notes",
        json={"content": bad},
        headers=_auth(nurse, "nurse"),
    )
    assert r.status_code == 422, r.text
    with db_session() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM nursing_notes WHERE patient_id = ?", (pid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'nursing_note_written' "
                "AND patient_id = ?",
                (pid,),
            ).fetchone()[0]
            == 0
        )


def test_notes_list_newest_first_with_the_author_name(client):
    dept = _name("NN Medicine ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept, name="Sister Anu")
    for text in ("First note.", "Second note.", "Third note."):
        assert _write(client, pid, nurse, "nurse", text).status_code == 201
    r = client.get(f"/api/patients/{pid}/nursing-notes", headers=_auth(nurse, "nurse"))
    assert r.status_code == 200, r.text
    notes = r.json()["notes"]
    assert [n["content"] for n in notes] == ["Third note.", "Second note.", "First note."]
    assert {n["author_name"] for n in notes} == {"Sister Anu"}
    # The read is audited as a PHI read, like every other chart read.
    with db_session() as db:
        viewed = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'nursing_notes_viewed' "
            "AND actor_user_id = ? AND patient_id = ?",
            (nurse, pid),
        ).fetchone()[0]
    assert viewed == 1


# ── Where the note is read back ──────────────────────────────────────────────
def test_the_note_is_on_the_chart_timeline_the_handover_and_the_chart_payload(client):
    dept = _name("NN Medicine ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept, name="Sister Mary Thomas")
    doctor = _mk_user("doctor", dept)
    assert _write(client, pid, nurse, "nurse", "Wound dressing changed, clean.").status_code == 201

    timeline = build_chart_timeline(pid, user_id=doctor)
    events = [e for e in timeline["events"] if e["kind"] == "nursing_note"]
    assert len(events) == 1
    assert events[0]["badge"] == "NURSING"
    assert events[0]["title"].startswith("Nursing note (")
    assert "Wound dressing changed" in events[0]["detail"]
    assert events[0]["actor"] == "Sister Mary Thomas"

    draft = build_ipass(pid, include_assessment=False)
    notes = draft["sections"]["nursing_notes"]
    assert [n["content"] for n in notes] == ["Wound dressing changed, clean."]
    assert notes[0]["author_name"] == "Sister Mary Thomas"
    assert "Nursing notes (last" in draft["plain_text"]
    assert "Sister Mary Thomas: Wound dressing changed, clean." in draft["plain_text"]
    assert "1 nursing note(s)" in draft["source_summary"]

    chart = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor, "doctor"))
    assert chart.status_code == 200, chart.text
    carried = chart.json()["nursing_notes"]
    assert [n["content"] for n in carried] == ["Wound dressing changed, clean."]
    assert carried[0]["author_name"] == "Sister Mary Thomas"
    assert carried[0]["shift_label"]


def test_a_patient_without_notes_has_an_empty_list_and_no_handover_heading(client):
    dept = _name("NN Medicine ")
    pid = _mk_patient(dept)
    doctor = _mk_user("doctor", dept)
    draft = build_ipass(pid, include_assessment=False)
    assert draft["sections"]["nursing_notes"] == []
    assert "Nursing notes" not in draft["plain_text"]
    assert "nursing note(s)" not in draft["source_summary"]
    chart = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor, "doctor"))
    assert chart.json()["nursing_notes"] == []


# ── The Orb Ward Record profile ──────────────────────────────────────────────
def test_under_the_ward_record_profile_notes_are_off_everywhere(client, monkeypatch):
    dept = _name("NN Medicine ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept)
    doctor = _mk_user("doctor", dept)
    assert (
        _write(client, pid, nurse, "nurse", "Written while the profile is off.").status_code == 201
    )

    monkeypatch.setattr(config, "WARD_RECORD", True, raising=False)
    assert svc.nursing_notes_enabled() is False
    r = _write(client, pid, nurse, "nurse", "Not on this computer.")
    assert r.status_code == 404 and r.json()["detail"] == svc.SWITCHED_OFF
    r = client.get(f"/api/patients/{pid}/nursing-notes", headers=_auth(nurse, "nurse"))
    assert r.status_code == 404 and r.json()["detail"] == svc.SWITCHED_OFF
    timeline = build_chart_timeline(pid, user_id=doctor)
    assert [e for e in timeline["events"] if e["kind"] == "nursing_note"] == []
    chart = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor, "doctor"))
    assert chart.status_code == 200 and chart.json()["nursing_notes"] == []
    with db_session() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM nursing_notes WHERE patient_id = ?", (pid,)
            ).fetchone()[0]
            == 1
        ), "the stored note is kept, only hidden"


def test_migration_88_creates_the_table_once(tmp_path):
    import sqlite3

    from migrations_his.m0088_nursing_notes import MIGRATION

    assert MIGRATION.version == 88
    conn = sqlite3.connect(str(tmp_path / "m88.db"))
    MIGRATION.apply(conn)
    MIGRATION.apply(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(nursing_notes)").fetchall()]
    assert cols == [
        "id",
        "patient_id",
        "author_id",
        "shift_date",
        "shift_label",
        "content",
        "written_at",
    ]
    conn.close()
