"""Slice 3 — Socket.IO handshake revocation checks + presence roster.

The handshake `_authenticate` must mirror the REST revocation checks so a
logged-out / password-reset / deactivated session cannot keep streaming PHI
over the socket until the JWT expires. `_roster` must dedupe viewers by user so
one clinician with two tabs shows as a single presence.
"""
from database import db_session
from auth import hash_password, create_token
from ws import realtime


def _mk_user(uid, role="doctor", pwd="secretpass", token_version=0, is_active=1):
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            (uid, f"User {uid}", role, hash_password(pwd)),
        )
        db.execute(
            "UPDATE users SET token_version = ?, is_active = ? WHERE id = ?",
            (token_version, is_active, uid),
        )
    return uid


def test_authenticate_accepts_valid_token():
    _mk_user("WS-OK", token_version=3)
    token = create_token("WS-OK", "doctor", token_version=3)
    user = realtime._authenticate({"token": token})
    assert user is not None
    assert user["id"] == "WS-OK"


def test_authenticate_rejects_stale_token_version():
    # Token minted at tv=1, then the account revoked-all (tv bumped to 2).
    _mk_user("WS-REVOKED", token_version=1)
    token = create_token("WS-REVOKED", "doctor", token_version=1)
    with db_session() as db:
        db.execute("UPDATE users SET token_version = 2 WHERE id = ?", ("WS-REVOKED",))
    assert realtime._authenticate({"token": token}) is None


def test_authenticate_rejects_deactivated_user():
    _mk_user("WS-DEACT", token_version=0, is_active=0)
    token = create_token("WS-DEACT", "doctor", token_version=0)
    assert realtime._authenticate({"token": token}) is None


def test_authenticate_rejects_missing_and_bad_token():
    assert realtime._authenticate(None) is None
    assert realtime._authenticate({}) is None
    assert realtime._authenticate({"token": "not-a-jwt"}) is None


def test_authenticate_accepts_session_cookie_from_environ():
    """Cookie-based clients (no JS token in `auth`) authenticate the socket via the
    HttpOnly orb_session cookie sent with the handshake (Deferred A)."""
    from auth import SESSION_COOKIE
    _mk_user("WS-COOKIE", token_version=0)
    token = create_token("WS-COOKIE", "doctor", token_version=0)
    environ = {"HTTP_COOKIE": f"{SESSION_COOKIE}={token}; other=1"}
    user = realtime._authenticate(None, environ)
    assert user is not None and user["id"] == "WS-COOKIE"
    # A revoked (tv-bumped) cookie is rejected the same as a header token.
    with db_session() as db:
        db.execute("UPDATE users SET token_version = 5 WHERE id = ?", ("WS-COOKIE",))
    assert realtime._authenticate(None, environ) is None


def test_authenticate_no_cookie_no_auth_rejected():
    assert realtime._authenticate(None, {"HTTP_COOKIE": "unrelated=x"}) is None
    assert realtime._authenticate(None, {}) is None


def test_authenticate_rejects_discharge_access_grant():
    token = create_token("discharge_access", "patient", token_version=0)
    assert realtime._authenticate({"token": token}) is None


def test_roster_dedupes_by_user():
    realtime._presence.clear()
    # Same clinician, two tabs (two sids) + a second clinician.
    realtime._presence["P1"] = {
        "sidA": {"user_id": "DOC1", "name": "Dr A", "role": "doctor"},
        "sidB": {"user_id": "DOC1", "name": "Dr A", "role": "doctor"},
        "sidC": {"user_id": "NUR2", "name": "Nurse B", "role": "nurse"},
    }
    roster = realtime._roster("P1")
    ids = sorted(v["user_id"] for v in roster)
    assert ids == ["DOC1", "NUR2"]
    realtime._presence.clear()


def test_roster_empty_for_unknown_patient():
    realtime._presence.clear()
    assert realtime._roster("nobody") == []


# ── Slice C — vitals broadcast scoping ────────────────────────────────────────
import asyncio
import uuid as _uuid


def _seed_admitted_patient(dept="Cardiology"):
    pid = f"SCOPE-{_uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?,?,?, 'admitted')",
                   (pid, "Scope Test", f"MRN-{_uuid.uuid4().hex[:6]}"))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
            "VALUES (?,?, 'system', ?, 'active')",
            (f"ADM-{_uuid.uuid4().hex[:8]}", pid, dept),
        )
    return pid


def test_patient_department_lookup():
    realtime._patient_dept_cache.clear()
    pid = _seed_admitted_patient("Neurology")
    assert realtime._patient_department(pid) == "Neurology"
    # Unknown patient -> None (and cached).
    assert realtime._patient_department("nobody") is None


def test_emit_patient_vitals_targets_department_rooms(monkeypatch):
    realtime._patient_dept_cache.clear()
    pid = _seed_admitted_patient("Cardiology")
    sent = []

    async def fake_emit(event, payload, room=None):
        sent.append((event, room))

    monkeypatch.setattr(realtime.sio, "emit", fake_emit)
    asyncio.run(realtime.emit_patient_vitals("vitals:update", {"patient_id": pid}))
    rooms = {room for _, room in sent}
    # Goes to the patient's dept room AND the __all__ room (no-dept viewers) —
    # and NOT a global broadcast (every tuple carries a room).
    assert "dept:Cardiology" in rooms
    assert "dept:__all__" in rooms
    assert all(room is not None for _, room in sent)
