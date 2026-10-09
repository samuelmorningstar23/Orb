"""The waiting-room token board (opd-9, W7-B, 9 October 2026).

What this file defends:

  1. OFF BY DEFAULT. With ORB_OPD off the board and the board settings answer
     404, whatever key is sent.
  2. A DEVICE KEY, NOT A SESSION. The administrator (schedule.manage) makes a
     board for one clinic and sees its key once; only the key's hash is
     stored, and neither appears in any record, list or later answer. The
     board answers only to the X-Orb-Board-Key header: no key, a wrong key, a
     switched-off key, a key in the address and a staff session are all 401.
  3. NO PATIENT DATA (ruling OPD-N7): the answer carries exactly the
     whitelisted keys, per doctor the name, room, the token being called and
     the next three in seeing order, and never a patient's name, UHID or
     complaint. So a board read writes no phi_access row.
  4. ONE CLINIC. A board's key never shows another clinic's tokens, and an
     administrator with a department makes and switches off boards for it only.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import opd_queue_service as oq

client = TestClient(app)

CLINIC = "Board Test Medicine"
OTHER = "Board Test Orthopaedics"


def _mk_user(role, department, name=None):
    uid = f"BDU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"Board {role} {uid[-4:]}", role, department))
    return uid


def _auth(uid, role="doctor"):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def staff():
    return {
        "doc": _mk_user("doctor", CLINIC, "Dr Anaya Board"),
        "doc2": _mk_user("doctor", CLINIC, "Dr Bharat Board"),
        "other_doc": _mk_user("doctor", OTHER, "Dr Chitra Board"),
        "admin": _mk_user("admin", None),
        "clinic_admin": _mk_user("admin", CLINIC),
        "recep": _mk_user("receptionist", CLINIC),
    }


@pytest.fixture
def opd_on(monkeypatch):
    from ws import realtime
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    monkeypatch.setattr(realtime, "schedule_on_loop", lambda make_coro, what: True)
    assert config.feature_enabled("opd")
    yield


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    yield


def _outpatient(tag):
    pid = f"BDP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status, created_at) "
            "VALUES (?, ?, ?, 44, 'M', 'registered', ?)",
            (pid, f"Boardname {tag} {pid[-4:]}", f"BUHID-{uuid.uuid4().hex[:8].upper()}",
             datetime.now(timezone.utc).isoformat()))
    return pid


def _visit(doc, tag, complaint="Secret complaint words"):
    pid = _outpatient(tag)
    with db_session() as db:
        r = db.execute("SELECT name, mrn FROM patients WHERE id = ?", (pid,)).fetchone()
    resp = client.post("/api/frontdesk/opd/visits",
                       json={"patient_id": pid, "doctor_id": doc, "chief_complaint": complaint,
                             "identity_check": {"name": r["name"], "age_years": 44,
                                                "uhid": r["mrn"]}},
                       headers=_auth(doc))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _create(admin, department=CLINIC, label=None, role="admin"):
    body = {"department": department}
    if label is not None:
        body["label"] = label
    return client.post("/api/frontdesk/opd/boards", json=body, headers=_auth(admin, role))


def _board(key=None, headers=None, params=None):
    hdrs = dict(headers or {})
    if key is not None:
        hdrs[oq.BOARD_KEY_HEADER] = key
    return client.get("/api/frontdesk/opd/board", headers=hdrs, params=params)


def _phi_rows_since(marker):
    with db_session() as db:
        return db.execute("SELECT COUNT(*) FROM audit_logs WHERE id > ? AND "
                          "event_category = 'phi_access'", (marker,)).fetchone()[0]


def _last_audit_id():
    with db_session() as db:
        return db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Off by default
# ═════════════════════════════════════════════════════════════════════════════

def test_everything_is_404_with_the_feature_off(staff, opd_off):
    for resp in (_board("anything"), _board(),
                 client.get("/api/frontdesk/opd/boards", headers=_auth(staff["admin"], "admin")),
                 _create(staff["admin"]),
                 client.post("/api/frontdesk/opd/boards/BRD-X/revoke", json={},
                             headers=_auth(staff["admin"], "admin"))):
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "feature_disabled"


# ═════════════════════════════════════════════════════════════════════════════
# 2. The key
# ═════════════════════════════════════════════════════════════════════════════

def test_the_key_is_shown_once_and_only_its_hash_is_stored(staff, opd_on):
    resp = _create(staff["admin"], label="Medicine waiting hall")
    assert resp.status_code == 201, resp.text
    assert resp.headers.get("cache-control") == "no-store"
    out = resp.json()
    key, board = out["key"], out["board"]
    assert len(key) >= 40 and board["department"] == CLINIC and board["active"] is True
    assert "key_hash" not in board and key not in json.dumps(board)
    with db_session() as db:
        row = db.execute("SELECT * FROM opd_display_boards WHERE id = ?", (board["id"],)).fetchone()
        audit = db.execute("SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ?",
                           (oq.BOARD_CREATED_ACTION, board["id"])).fetchall()
    assert row["key_hash"] == "sha256:" + hashlib.sha256(key.encode()).hexdigest()
    assert key not in json.dumps(dict(row))
    assert len(audit) == 1 and audit[0]["event_category"] == "security"
    text = json.dumps(dict(audit[0]))
    assert key not in text and row["key_hash"] not in text
    listed = client.get("/api/frontdesk/opd/boards", headers=_auth(staff["admin"], "admin"))
    assert listed.status_code == 200
    assert key not in listed.text and row["key_hash"] not in listed.text
    assert any(b["id"] == board["id"] for b in listed.json()["boards"])


def test_only_the_board_key_header_opens_the_board(staff, opd_on):
    key = _create(staff["admin"]).json()["key"]
    assert _board(key).status_code == 200
    staff_token = create_token(staff["admin"], "admin")
    refused = [
        _board(),
        _board("not-a-board-key"),
        _board(key + "x"),
        _board(staff_token),
        _board(headers={"Authorization": f"Bearer {staff_token}"}),
        _board(params={"key": key}),
    ]
    for resp in refused:
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"]["error"] == "board_key_refused"


def test_a_switched_off_key_is_refused_and_cannot_be_switched_off_twice(staff, opd_on):
    out = _create(staff["admin"]).json()
    assert _board(out["key"]).status_code == 200
    resp = client.post(f"/api/frontdesk/opd/boards/{out['board']['id']}/revoke",
                       json={"reason": "Screen moved"}, headers=_auth(staff["admin"], "admin"))
    assert resp.status_code == 200 and resp.json()["active"] is False
    assert _board(out["key"]).status_code == 401
    again = client.post(f"/api/frontdesk/opd/boards/{out['board']['id']}/revoke", json={},
                        headers=_auth(staff["admin"], "admin"))
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_revoked"
    missing = client.post("/api/frontdesk/opd/boards/BRD-NOPE/revoke", json={},
                          headers=_auth(staff["admin"], "admin"))
    assert missing.status_code == 404


def test_only_schedule_manage_makes_boards_and_a_clinic_admin_only_its_own(staff, opd_on):
    for uid, role in ((staff["doc"], "doctor"), (staff["recep"], "receptionist")):
        assert _create(uid, role=role).status_code == 403
        assert client.get("/api/frontdesk/opd/boards", headers=_auth(uid, role)).status_code == 403
    assert _create(staff["clinic_admin"], department=OTHER).status_code == 422
    other = _create(staff["admin"], department=OTHER).json()["board"]
    assert _create(staff["clinic_admin"], department=CLINIC).status_code == 201
    listed = client.get("/api/frontdesk/opd/boards",
                        headers=_auth(staff["clinic_admin"], "admin")).json()
    assert {b["department"] for b in listed["boards"]} == {CLINIC}
    assert listed["departments"] == [CLINIC]
    resp = client.post(f"/api/frontdesk/opd/boards/{other['id']}/revoke", json={},
                       headers=_auth(staff["clinic_admin"], "admin"))
    assert resp.status_code == 404
    assert _create(staff["admin"], department="No Such Clinic").status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
# 3. What the board shows
# ═════════════════════════════════════════════════════════════════════════════

def test_the_board_shows_tokens_doctors_and_rooms_and_nothing_about_a_patient(staff, opd_on):
    doc, doc2 = staff["doc"], staff["doc2"]
    visits = [_visit(doc, f"A{n}") for n in range(5)]
    other_doc_visit = _visit(doc2, "B0")
    called = visits[0]
    assert client.post(f"/api/frontdesk/opd/visits/{called['id']}/call",
                       headers=_auth(doc)).status_code == 200
    assert client.patch(f"/api/frontdesk/opd/visits/{called['id']}", json={"room": "4"},
                        headers=_auth(doc)).status_code == 200
    skipped = visits[1]
    assert client.post(f"/api/frontdesk/opd/visits/{skipped['id']}/skip",
                       headers=_auth(doc)).status_code == 200
    key = _create(staff["admin"]).json()["key"]

    marker = _last_audit_id()
    resp = _board(key)
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("cache-control") == "no-store"
    out = resp.json()
    assert set(out) == set(oq.BOARD_RESPONSE_KEYS)
    assert out["department"] == CLINIC and out["poll_seconds"] == 10
    by_name = {d["doctor_name"]: d for d in out["doctors"]}
    for d in out["doctors"]:
        assert set(d) == set(oq.BOARD_DOCTOR_KEYS)
    mine = by_name["Dr Anaya Board"]
    assert mine["calling"] == called["token_number"] and mine["room"] == "4"
    waiting = [v["token_number"] for v in visits[2:]]
    assert mine["next"] == sorted(waiting)[:3]
    assert skipped["token_number"] not in mine["next"]
    assert by_name["Dr Bharat Board"]["calling"] is None
    assert by_name["Dr Bharat Board"]["next"] == [other_doc_visit["token_number"]]

    text = resp.text
    for v in [*visits, other_doc_visit]:
        assert v["patient_id"] not in text and v["id"] not in text
        with db_session() as db:
            p = db.execute("SELECT name, mrn FROM patients WHERE id = ?",
                           (v["patient_id"],)).fetchone()
        assert p["name"] not in text and p["mrn"] not in text
    assert "Secret complaint" not in text and "Boardname" not in text
    assert _phi_rows_since(marker) == 0


def test_a_board_never_shows_another_clinics_tokens(staff, opd_on):
    _visit(staff["other_doc"], "C0")
    key = _create(staff["admin"]).json()["key"]
    out = _board(key).json()
    assert out["department"] == CLINIC
    assert "Dr Chitra Board" not in {d["doctor_name"] for d in out["doctors"]}
    other_key = _create(staff["admin"], department=OTHER).json()["key"]
    other = _board(other_key).json()
    assert other["department"] == OTHER
    assert {d["doctor_name"] for d in other["doctors"]} == {"Dr Chitra Board"}


def test_the_snapshot_whitelist_holds_whatever_the_rows_carry(staff, opd_on):
    _visit(staff["doc"], "D0")
    out = oq.board_snapshot(CLINIC, label="Hall")
    assert tuple(out) == oq.BOARD_RESPONSE_KEYS
    assert all(tuple(d) == oq.BOARD_DOCTOR_KEYS for d in out["doctors"])
