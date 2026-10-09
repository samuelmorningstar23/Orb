"""The runtime facility fence (multisite-1, W7-J, 9 October 2026).

Isolation is keyed on data (ruling M2): the moment a second facilities row
exists, a screen not yet taught to keep each facility's patients apart must
answer 503, never every facility's patients. With one facility (every install
today) the fence must do nothing at all.

Pinned here:
  * the route groups: /api, /uploads and /socket.io are fenced; signing in,
    health, facility identity and the operations page are never fenced; the
    built screens are never fenced; a crafted spelling of an open prefix is;
  * FACILITY_BLIND_SURFACES ships as the sentinel '*';
  * one facility: patient routes answer as before, with no facilities query
    per request while the answer is cached;
  * a second row inserted by SQL: patient routes answer 503 within 30 s (the
    cache), while auth and health stay 200, and the switch makes no difference
    either way (it gates growth, not isolation);
  * the refusal is audited (authorization, facility_fence_blocked), at most
    one row per address and route group a minute, and a failed audit write
    never turns the 503 into a 500;
  * /socket.io/ is fenced in the Socket.IO app, polling and the upgrade both.

Run with the test env, never the shipping .env.
"""
import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
import facility_fence as F
import main
from auth import create_token, hash_password
from database import db_session
from services import facility_service as FS

client = TestClient(main.app)


def _hdr(role: str, department=None) -> dict:
    uid = f"FF{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, department, hash_password("password")))
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(autouse=True)
def _clean_state():
    FS.invalidate_cache()
    F._audit_last.clear()
    yield
    FS.invalidate_cache()
    F._audit_last.clear()


@pytest.fixture()
def clock(monkeypatch):
    """One clock for the cache and the audit throttle, moved by hand."""
    now = [5000.0]
    monkeypatch.setattr(FS, "_clock", lambda: now[0])
    monkeypatch.setattr(F, "_clock", lambda: now[0])
    return now


@pytest.fixture()
def second_facility():
    """A second facilities row inserted by SQL, as a script or a restore would,
    removed after so the session database goes back to one facility."""
    with db_session() as db:
        cur = db.execute("INSERT INTO facilities (uuid, code, name) VALUES (?, 'FENCE2', 'Fence Two')",
                         (str(uuid.uuid4()),))
        fid = cur.lastrowid
    try:
        yield fid
    finally:
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (fid,))
        FS.invalidate_cache()


# ── 1. Which paths the fence covers ──────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/vigil/patients", "/api/sage/query", "/api/bridge/me", "/api/admin/users",
    "/api/discharge-access/verify", "/api/ingest/vitals", "/uploads/scan.png",
    "/socket.io/", "/api/healthcare", "/api/opsroom", "/api",
    # Crafted spellings of an open prefix are judged as fenced.
    "/api/auth/../vigil/patients", "/api/health/../vigil/patients", "/api//auth/me",
    "/api/./auth/me",
])
def test_patient_routes_are_fenced(path):
    assert F.is_fenced(path) is True


@pytest.mark.parametrize("path", [
    "/api/auth/login", "/api/auth/me", "/api/auth", "/api/health", "/api/health/loop",
    "/api/health/models", "/api/facilities", "/api/facilities/meta", "/api/ops/status",
    "/api/ops/status.txt", "/", "/index.html", "/assets/index-abc.js", "/dashboard/vigil",
    "/display/opd",
])
def test_signin_health_identity_ops_and_the_screens_are_never_fenced(path):
    assert F.is_fenced(path) is False


def test_the_blind_list_ships_as_the_sentinel():
    assert F.FACILITY_BLIND_SURFACES == F.SENTINEL == "*"
    assert F.describe() == {"every_patient_screen": True, "blind_surfaces": ["all"],
                            "always_open": ["/api/auth", "/api/health", "/api/facilities",
                                            "/api/ops"]}
    # A swept list (multisite-5b) fences only what it names.
    assert F.is_fenced("/api/vigil/patients", blind=("/api/sage",)) is False
    assert F.is_fenced("/api/sage/query", blind=("/api/sage",)) is True


def test_the_roots_split_between_the_two_copies():
    assert F.is_fenced("/socket.io/", F.HTTP_ROOTS) is False
    assert F.is_fenced("/api/vigil/patients", F.SOCKET_ROOTS) is False
    assert F.is_fenced("/socket.io/", F.SOCKET_ROOTS) is True


def test_main_installs_both_copies():
    import socketio
    from services.host_gate import HostGate
    assert any(m.cls is F.FacilityFence and m.kwargs.get("roots") == F.HTTP_ROOTS
               for m in main.app.user_middleware)
    assert isinstance(main.socket_app, HostGate)
    assert isinstance(main.socket_app.app, socketio.ASGIApp)
    assert type(main.socket_app.app).__name__ == "FencedSocketIOApp"


# ── 2. One facility: nothing changes ─────────────────────────────────────────

def test_one_facility_answers_as_before_with_no_query_per_request(monkeypatch):
    hdr = _hdr("nurse", "General Medicine")
    FS.prime_cache()
    calls = []
    real = FS.facility_count
    monkeypatch.setattr(FS, "facility_count", lambda db: calls.append(1) or real(db))
    for _ in range(5):
        resp = client.get("/api/vigil/patients", headers=hdr)
        assert resp.status_code == 200, resp.text
    assert calls == []


def test_the_switch_alone_never_fences(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)
    resp = client.get("/api/vigil/patients", headers=_hdr("nurse", "General Medicine"))
    assert resp.status_code == 200


# ── 3. A second facility inserted by SQL ─────────────────────────────────────

def _fence_rows() -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'facility_fence_blocked' ORDER BY id")]


def test_patient_routes_answer_503_within_thirty_seconds(clock, second_facility):
    nurse = _hdr("nurse", "General Medicine")
    admin = _hdr("admin")
    FS._store(1)  # the cached answer from before the row was inserted
    assert client.get("/api/vigil/patients", headers=nurse).status_code == 200  # still cached
    clock[0] += 31
    resp = client.get("/api/vigil/patients", headers=nurse)
    assert resp.status_code == 503
    assert resp.json() == {"detail": F.FENCE_MESSAGE, "error": "facility_fence",
                           "surface": "/api/vigil"}
    assert resp.headers["cache-control"] == "no-store"
    # Sign-in, health, facility identity and the operations page stay open.
    assert client.get("/api/auth/me", headers=nurse).status_code == 200
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["platform"]["multi_facility_data"] is True
    meta = client.get("/api/facilities/meta", headers=admin)
    assert meta.status_code == 200 and meta.json()["multi_facility_data"] is True
    ops = client.get("/api/ops/status", headers=admin)
    assert ops.status_code == 200
    check = next(c for c in ops.json()["checks"] if c["key"] == "facilities")
    assert check["status"] in ("attention", "urgent")
    assert check["detail"]["facility_fence"]["holding"] is True
    # Every other patient surface too, unauthenticated or not.
    assert client.get("/uploads/nothing.png", headers=admin).status_code == 503
    assert client.get("/api/vigil/patients").status_code == 503
    assert client.post("/api/sage/query", json={}, headers=nurse).status_code == 503


def test_the_switch_off_keeps_two_facilities_apart(second_facility, monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", False)
    FS.invalidate_cache()
    assert client.get("/api/vigil/patients", headers=_hdr("nurse", "General Medicine")).status_code == 503


def test_an_inactive_second_facility_still_fences(second_facility):
    with db_session() as db:
        db.execute("UPDATE facilities SET is_active = 0 WHERE id = ?", (second_facility,))
    FS.invalidate_cache()
    assert client.get("/api/vigil/patients", headers=_hdr("admin")).status_code == 503


def test_the_fence_lifts_with_the_row(second_facility):
    hdr = _hdr("nurse", "General Medicine")
    FS.invalidate_cache()
    assert client.get("/api/vigil/patients", headers=hdr).status_code == 503
    with db_session() as db:
        db.execute("DELETE FROM facilities WHERE id = ?", (second_facility,))
    FS.invalidate_cache()
    assert client.get("/api/vigil/patients", headers=hdr).status_code == 200


def test_a_refusal_is_audited_once_a_minute_with_the_count(clock, second_facility):
    hdr = _hdr("nurse", "General Medicine")
    before = len(_fence_rows())
    FS.invalidate_cache()
    for _ in range(3):
        assert client.get("/api/vigil/patients", headers=hdr).status_code == 503
    rows = _fence_rows()[before:]
    assert len(rows) == 1
    row = rows[0]
    assert row["event_category"] == "authorization" and row["outcome"] == "denied"
    assert row["target_type"] == "route_group" and row["target_id"] == "/api/vigil"
    assert row["request_path"] == "/api/vigil/patients" and row["http_method"] == "GET"
    assert row["actor_role"] == "nurse" and row["actor_user_id"]
    assert row["patient_id"] is None
    assert json.loads(row["metadata_json"])["refusals_since_last_row"] == 0
    # Another route group gets its own row at once.
    assert client.get("/api/sage/insights", headers=hdr).status_code == 503
    assert len(_fence_rows()[before:]) == 2
    # A minute later the next refusal writes a row carrying the two it counted.
    clock[0] += 61
    assert client.get("/api/vigil/patients", headers=hdr).status_code == 503
    rows = _fence_rows()[before:]
    assert len(rows) == 3
    assert json.loads(rows[-1]["metadata_json"])["refusals_since_last_row"] == 2


def test_a_failed_audit_write_still_answers_503(second_facility, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(F, "_write_audit", boom)
    FS.invalidate_cache()
    assert client.get("/api/vigil/patients", headers=_hdr("admin")).status_code == 503


def test_two_facilities_never_refuse_a_boot(second_facility):
    assert config.feature_enabled("multi_site") is False
    with TestClient(main.app) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/vigil/patients", headers=_hdr("admin")).status_code == 503


# ── 4. The socket ────────────────────────────────────────────────────────────

POLL = "/socket.io/?EIO=4&transport=polling"


def test_socket_polling_is_fenced_only_with_two_facilities(second_facility):
    c = TestClient(main.socket_app, base_url="http://localhost")
    FS.invalidate_cache()
    resp = c.get(POLL)
    assert resp.status_code == 503
    assert resp.json()["error"] == "facility_fence"
    # Health through the same outermost app stays open.
    assert c.get("/api/health").status_code == 200


def test_socket_polling_answers_with_one_facility():
    c = TestClient(main.socket_app, base_url="http://localhost")
    resp = c.get(POLL)
    assert resp.status_code == 200
    assert resp.text.startswith("0")


def test_the_socket_upgrade_is_refused_with_two_facilities(second_facility):
    FS.invalidate_cache()
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/socket.io/", "query_string": b"EIO=4&transport=websocket",
             "headers": [(b"host", b"localhost")], "client": ("127.0.0.1", 5000)}
    asyncio.run(main.socket_app.app(scope, receive, send))
    assert sent and sent[0]["type"] == "websocket.close"


def test_a_lifespan_scope_passes_straight_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(F.FacilityFence(app)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]
