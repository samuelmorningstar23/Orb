"""The facility scope core (multisite-5a, W8-K, 10 October 2026).

Samuel ruled on 3 October 2026 that isolation between the facilities of one
hospital group is keyed on data (multi-site M2): once more than one facilities
row exists, each account sees only its facilities' patients, whatever
ORB_MULTI_SITE says. This file builds a two-facility world by SQL and pins:

  * the resolver (facility_service.facility_scope_for): a ward-scoped account
    gets its department's facility, a department-less account its home row's
    facility or facility 1, an admin with a live enterprise grant, a service
    account and a patient login get no facility limit, a grant on any other
    role is ignored (MS-R6 (a)), and a department the master does not list
    sees nothing;
  * the predicates (scope_patient_ids_sql and patient_in_scope with
    facilities=), which agree with each other on every account and patient,
    and follow the LATEST encounter for a department-less account (MS-R5 (a));
  * both helpers: a cross-facility chart read is a 404 that writes a facility
    patient_scope_denied row from require_patient_access AND from
    enforce_patient_scope, while a denial the department alone causes still
    writes no row from enforce_patient_scope;
  * log_phi_read carries metadata.facility_scope only with two facilities;
  * UserOut and /api/auth/me carry the facility;
  * with one facility: the SQL strings, the statements and the rows are
    exactly what they were before this increment.

The runtime fence (W7-J) answers 503 on every patient screen while the blind
list is the sentinel '*'. The HTTP tests below open it (FACILITY_BLIND_SURFACES
= ()) to exercise the helpers the way multisite-5b's swept screens will.

TEST RULE (contracts, W7-J): the second facility is removed in teardown and
facility_service.invalidate_cache() called, or every later test's patient
routes answer 503. Every facility id this module writes is put back to 1.

Run with the test env, never the shipping .env.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import database
import facility_fence as F
import permissions as P
from auth import create_token
from database import db_session
from main import app
from services import facility_service as FS

client = TestClient(app)

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9201                      # an id no other module writes
F2_CODE = f"FS{TAG}"
DEPT_GM = f"FS General Medicine {TAG}"
DEPT_SURG = f"FS Surgery {TAG}"
DEPT_CARD = f"FS Cardiology {TAG}"
DEPT_UNKNOWN = f"FS Nowhere {TAG}"

SCOPE_SQL_MARK = "SELECT a.department FROM admissions a WHERE a.patient_id = p.id"
FACILITY_WORDS = ("facilit", "user_facilities", "enterprise_access_grants", "from departments")


# ── the world ────────────────────────────────────────────────────────────────

def _mk_user(db, role, department, label):
    uid = f"FSC{uuid.uuid4().hex[:8].upper()}"
    db.execute(
        "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, 'x')",
        (uid, f"Facscope {label}", role, department))
    return uid


def _mk_patient(db, label, admissions=(), registered=1):
    """admissions: (department, facility_id, status, created_at) tuples."""
    pid = f"FSCP{uuid.uuid4().hex[:8].upper()}"
    db.execute(
        "INSERT INTO patients (id, name, mrn, status, registered_facility_id) "
        "VALUES (?, ?, ?, 'admitted', ?)",
        (pid, f"Facscope {label}", f"MRN-{pid}", registered))
    for i, (dept, fid, status, created) in enumerate(admissions):
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, status, "
            "facility_id, created_at) VALUES (?, ?, 'system', ?, ?, ?, ?, ?)",
            (f"{pid}-A{i}", pid, dept, dept, status, fid, created))
    return pid


def _put_back_facility_two():
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Facility Scope Two"))
    FS.invalidate_cache()


@pytest.fixture(scope="module")
def world():
    users, patients = {}, {}
    with db_session() as db:
        for name, fid in ((DEPT_GM, 1), (DEPT_SURG, 1), (DEPT_CARD, F2)):
            db.execute("INSERT INTO departments (name, facility_id, seeded_from) "
                       "VALUES (?, ?, 'test')", (name, fid))
        users["nurse_f1"] = _mk_user(db, "nurse", DEPT_GM, "F1 nurse")
        users["nurse_f1_surg"] = _mk_user(db, "nurse", DEPT_SURG, "F1 surgery nurse")
        users["nurse_f2"] = _mk_user(db, "nurse", DEPT_CARD, "F2 nurse")
        users["nurse_unknown"] = _mk_user(db, "nurse", DEPT_UNKNOWN, "unlisted nurse")
        users["pharm_f2"] = _mk_user(db, "pharmacist", None, "F2 pharmacist")
        users["cashier_f1"] = _mk_user(db, "cashier", None, "F1 cashier")
        users["admin_f2"] = _mk_user(db, "admin", None, "F2 admin")
        users["admin_f1"] = _mk_user(db, "admin", None, "F1 admin")
        users["admin_ent"] = _mk_user(db, "admin", None, "enterprise admin")
        users["pharm_grant"] = _mk_user(db, "pharmacist", None, "pharmacist with a grant")
        for key in ("pharm_f2", "admin_f2"):
            db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                       "granted_at, reason) VALUES (?, ?, 'home', 'system', "
                       "CURRENT_TIMESTAMP, 'test')", (users[key], F2))
        for key in ("admin_ent", "pharm_grant"):
            db.execute("INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, "
                       "reason) VALUES (?, 'system', CURRENT_TIMESTAMP, 'test')", (users[key],))
        patients["p_f1"] = _mk_patient(
            db, "One", [(DEPT_GM, 1, "active", "2026-10-01 08:00:00")])
        patients["p_f1_surg"] = _mk_patient(
            db, "Surgery", [(DEPT_SURG, 1, "active", "2026-10-01 08:00:00")])
        patients["p_f2"] = _mk_patient(
            db, "Two", [(DEPT_CARD, F2, "active", "2026-10-01 08:00:00")], registered=F2)
        patients["p_f2_out"] = _mk_patient(db, "Outpatient Two", registered=F2)
        patients["p_f1_out"] = _mk_patient(db, "Outpatient One", registered=1)
        # An F1 stay long ago, an F2 stay now: the latest encounter decides.
        patients["p_moved"] = _mk_patient(db, "Moved", [
            (DEPT_GM, 1, "discharged", "2026-01-01 08:00:00"),
            (DEPT_CARD, F2, "active", "2026-10-01 08:00:00")])
        patients["p_unknown"] = _mk_patient(
            db, "Unlisted", [(DEPT_UNKNOWN, 1, "active", "2026-10-01 08:00:00")])
    _put_back_facility_two()
    try:
        yield {**users, **patients}
    finally:
        uids, pids = list(users.values()), list(patients.values())
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
            db.executemany("DELETE FROM departments WHERE name = ?",
                           [(d,) for d in (DEPT_GM, DEPT_SURG, DEPT_CARD)])
            db.executemany("DELETE FROM user_facilities WHERE user_id = ?", [(u,) for u in uids])
            db.executemany("DELETE FROM enterprise_access_grants WHERE user_id = ?",
                           [(u,) for u in uids])
            db.executemany("UPDATE admissions SET facility_id = 1, status = 'discharged' "
                           "WHERE patient_id = ?", [(p,) for p in pids])
            db.executemany("UPDATE patients SET registered_facility_id = 1, "
                           "status = 'discharged' WHERE id = ?", [(p,) for p in pids])
            db.executemany("UPDATE users SET is_active = 0 WHERE id = ?", [(u,) for u in uids])
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh_cache():
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


@pytest.fixture()
def fence_open(monkeypatch):
    """The blind list as multisite-5b leaves it once every screen is swept."""
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


@pytest.fixture()
def one_facility(world):
    """Facility two taken off record for one test, then put back."""
    with db_session() as db:
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        yield
    finally:
        _put_back_facility_two()


@pytest.fixture()
def statements(monkeypatch):
    """Every SQL statement run on a connection handed out during the test.

    Connections are pooled per thread (ORB_DB_POOL), so the trace is set each
    time database.get_db hands one out, and cleared again afterwards."""
    seen: list[str] = []
    touched: list = []
    active = [True]
    real = database.get_db

    def record(statement):
        if active[0]:
            seen.append(statement)

    def traced():
        conn = real()
        conn.set_trace_callback(record)
        touched.append(conn)
        return conn

    monkeypatch.setattr(database, "get_db", traced)
    yield seen
    active[0] = False
    for conn in touched:
        try:
            conn.set_trace_callback(None)
        except Exception:
            pass   # a pooled handle owned by a worker thread keeps a silent callback


def _user(world, key) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM users WHERE id = ?", (world[key],)).fetchone())


def _auth(world, key) -> dict:
    user = _user(world, key)
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


def _last_audit_id() -> int:
    with db_session() as db:
        return int(db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0])


def _rows(after: int, action_type: str, actor: str | None = None,
          patient_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_logs WHERE id > ? AND action_type = ?"
    params: list = [after, action_type]
    if actor:
        sql += " AND actor_user_id = ?"
        params.append(actor)
    if patient_id:
        sql += " AND patient_id = ?"
        params.append(patient_id)
    with db_session() as db:
        rows = [dict(r) for r in db.execute(sql + " ORDER BY id", tuple(params)).fetchall()]
    for row in rows:
        row["meta"] = json.loads(row["metadata_json"] or "{}")
    return rows


# ═════════════════════════════════════════════════════════════════════════════
#  1. The resolver
# ═════════════════════════════════════════════════════════════════════════════

def test_each_account_resolves_to_its_facilities(world):
    expected = {
        "nurse_f1": frozenset({1}),
        "nurse_f1_surg": frozenset({1}),
        "nurse_f2": frozenset({F2}),
        "pharm_f2": frozenset({F2}),          # department-less, home row at F2
        "cashier_f1": frozenset({1}),         # department-less, no row: facility 1
        "admin_f2": frozenset({F2}),
        "admin_f1": frozenset({1}),
        "admin_ent": None,                    # live enterprise grant: the group
        "pharm_grant": frozenset({1}),        # a grant on a pharmacist is ignored (MS-R6 (a))
        "nurse_unknown": frozenset(),         # a department no facility owns: nothing
    }
    got = {key: FS.facility_scope_for(_user(world, key)) for key in expected}
    assert got == expected
    assert P.facility_scope(_user(world, "nurse_f2")) == frozenset({F2})


def test_service_accounts_and_patient_logins_carry_no_facility_limit(world):
    assert FS.facility_scope_for({"id": "system", "role": "admin", "department": None}) is None
    assert FS.facility_scope_for({"id": "bot_vigil", "role": "doctor", "department": None}) is None
    assert FS.facility_scope_for({"id": "PT-ANY", "role": "patient", "department": None}) is None


def test_the_department_is_matched_ignoring_case_and_outer_spaces(world):
    user = {"id": "FSC-CASE", "role": "nurse", "department": f"  {DEPT_CARD.upper()} "}
    assert FS.facility_scope_for(user) == frozenset({F2})


def test_a_revoked_home_row_and_a_revoked_grant_stop_counting(world):
    with db_session() as db:
        db.execute("UPDATE user_facilities SET revoked_at = CURRENT_TIMESTAMP, revoked_by = 'system' "
                   "WHERE user_id = ?", (world["admin_f2"],))
        db.execute("UPDATE enterprise_access_grants SET revoked_at = CURRENT_TIMESTAMP, "
                   "revoked_by = 'system' WHERE user_id = ?", (world["admin_ent"],))
    try:
        FS.invalidate_cache()
        assert FS.facility_scope_for(_user(world, "admin_f2")) == frozenset({1})
        assert FS.facility_scope_for(_user(world, "admin_ent")) == frozenset({1})
    finally:
        with db_session() as db:
            db.execute("UPDATE user_facilities SET revoked_at = NULL, revoked_by = NULL "
                       "WHERE user_id = ?", (world["admin_f2"],))
            db.execute("UPDATE enterprise_access_grants SET revoked_at = NULL, revoked_by = NULL "
                       "WHERE user_id = ?", (world["admin_ent"],))
        FS.invalidate_cache()


def test_the_maps_are_cached_and_invalidate_cache_forgets_them(world, monkeypatch):
    now = [7000.0]
    monkeypatch.setattr(FS, "_clock", lambda: now[0])
    loads = []
    real = FS._load_scope_maps
    monkeypatch.setattr(FS, "_load_scope_maps", lambda db: loads.append(1) or real(db))
    FS.invalidate_cache()
    for _ in range(3):
        FS.facility_scope_for(_user(world, "nurse_f2"))
    assert len(loads) == 1
    now[0] += FS.CACHE_SECONDS + 1
    FS.facility_scope_for(_user(world, "nurse_f2"))
    assert len(loads) == 2
    FS.invalidate_cache()
    FS.facility_scope_for(_user(world, "nurse_f2"))
    assert len(loads) == 3


def test_unreadable_maps_fail_rather_than_guess(world, monkeypatch):
    def boom(db):
        raise RuntimeError("locked")
    FS.invalidate_cache()
    FS.multi_facility_data()
    monkeypatch.setattr(FS, "_load_scope_maps", boom)
    with pytest.raises(FS.FacilityScopeError):
        FS.facility_scope_for(_user(world, "admin_f2"))


def test_the_flag_does_not_switch_isolation(world, monkeypatch):
    import config
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", False)
    assert FS.facility_scope_for(_user(world, "admin_f2")) == frozenset({F2})


# ═════════════════════════════════════════════════════════════════════════════
#  2. The predicates
# ═════════════════════════════════════════════════════════════════════════════

ACCOUNTS = ("nurse_f1", "nurse_f1_surg", "nurse_f2", "nurse_unknown", "pharm_f2",
            "cashier_f1", "admin_f2", "admin_f1", "admin_ent", "pharm_grant")
PATIENTS = ("p_f1", "p_f1_surg", "p_f2", "p_f2_out", "p_f1_out", "p_moved", "p_unknown")


def _visible(world, key) -> set[str]:
    user = _user(world, key)
    sql, params = P.scope_patient_ids_sql(P.department_scope(user),
                                          facilities=P.facility_scope(user))
    mine = tuple(world[p] for p in PATIENTS)
    marks = ", ".join("?" for _ in mine)
    with db_session() as db:
        ids = {r[0] for r in db.execute(
            f"SELECT id FROM patients WHERE id IN ({sql}) AND id IN ({marks})",
            (*params, *mine)).fetchall()}
    return {p for p in PATIENTS if world[p] in ids}


def test_who_sees_whom_with_two_facilities(world):
    seen = {key: _visible(world, key) for key in ACCOUNTS}
    assert seen["nurse_f1"] == {"p_f1"}
    assert seen["nurse_f1_surg"] == {"p_f1_surg"}
    assert seen["nurse_f2"] == {"p_f2", "p_moved"}
    assert seen["nurse_unknown"] == set()
    # Department-less accounts follow the latest encounter (MS-R5 (a)): the
    # patient now at F2 is gone from F1's view although F1 once admitted them.
    assert seen["cashier_f1"] == {"p_f1", "p_f1_surg", "p_f1_out", "p_unknown"}
    assert seen["admin_f1"] == seen["cashier_f1"] == seen["pharm_grant"]
    assert seen["pharm_f2"] == {"p_f2", "p_f2_out", "p_moved"}
    assert seen["admin_f2"] == seen["pharm_f2"]
    assert seen["admin_ent"] == set(PATIENTS)


def test_patient_in_scope_agrees_with_the_subquery(world):
    with db_session() as db:
        for key in ACCOUNTS:
            user = _user(world, key)
            dept, facilities = P.department_scope(user), P.facility_scope(user)
            visible = _visible(world, key)
            for p in PATIENTS:
                assert P.patient_in_scope(db, world[p], dept, facilities=facilities) == (
                    p in visible), (key, p)
            assert P.patient_in_scope(db, "NO-SUCH-PATIENT", dept,
                                      facilities=facilities) is (facilities is None
                                                                 and dept is None)


def test_the_blocking_form_and_an_empty_set(world):
    assert P.patient_in_scope_blocking(world["p_f2"], None, facilities=frozenset({F2})) is True
    assert P.patient_in_scope_blocking(world["p_f2"], None, facilities=frozenset({1})) is False
    assert P.patient_in_scope_blocking(world["p_f2"], None, facilities=frozenset()) is False
    assert P.scope_patient_ids_sql(DEPT_CARD, facilities=frozenset()) == (
        "SELECT p.id FROM patients p WHERE 0", ())


def test_facility_denial_names_the_cause_and_never_a_missing_patient(world):
    with db_session() as db:
        assert P.facility_denial(db, world["p_f1"], frozenset({F2})) == (True, 1)
        assert P.facility_denial(db, world["p_f2"], frozenset({F2})) == (False, F2)
        assert P.facility_denial(db, world["p_moved"], frozenset({1})) == (True, F2)
        assert P.facility_denial(db, "NO-SUCH-PATIENT", frozenset({F2})) == (False, None)
        assert P.facility_denial(db, world["p_f1"], frozenset()) == (True, None)
        assert P.facility_denial(db, world["p_f1"], None) == (False, None)


# ═════════════════════════════════════════════════════════════════════════════
#  3. Both helpers over HTTP
# ═════════════════════════════════════════════════════════════════════════════

def _chart(world, key, patient):
    """require_patient_access lane: GET /api/vigil/patients/{id}/notes."""
    return client.get(f"/api/vigil/patients/{world[patient]}/notes", headers=_auth(world, key))


def _scribe(world, key, patient):
    """enforce_patient_scope lane: GET /api/scribe/notes?patient_id=."""
    return client.get(f"/api/scribe/notes?patient_id={world[patient]}", headers=_auth(world, key))


def test_a_cross_facility_read_is_a_404_with_a_facility_row_from_both_helpers(world, fence_open):
    for lane, helper in ((_chart, "require_patient_access"), (_scribe, "enforce_patient_scope")):
        mark = _last_audit_id()
        resp = lane(world, "admin_f2", "p_f1")
        assert resp.status_code == 404, (helper, resp.text)
        assert resp.json() == {"detail": "Patient not found"}
        rows = _rows(mark, "patient_scope_denied", world["admin_f2"], world["p_f1"])
        assert len(rows) == 1, helper
        row = rows[0]
        assert row["event_category"] == "authorization" and row["outcome"] == "denied"
        assert row["meta"]["scope_cause"] == "facility"
        assert row["meta"]["facility_scope"] == [F2]
        assert row["meta"]["patient_facility_id"] == 1
        assert row["meta"]["scope_helper"] == helper
        assert "facility" in row["summary_message"]
        # Nothing was read, so nothing was recorded as read.
        assert not _rows(mark, "patient_notes_viewed", world["admin_f2"], world["p_f1"])
        assert not _rows(mark, "scribe_notes_listed", world["admin_f2"], world["p_f1"])


def test_the_same_account_reads_its_own_facilitys_patient(world, fence_open):
    mark = _last_audit_id()
    assert _chart(world, "admin_f2", "p_f2").status_code == 200
    assert _scribe(world, "admin_f2", "p_f2").status_code == 200
    reads = (_rows(mark, "patient_notes_viewed", world["admin_f2"], world["p_f2"])
             + _rows(mark, "scribe_notes_listed", world["admin_f2"], world["p_f2"]))
    assert len(reads) == 2
    for row in reads:
        assert row["event_category"] == "phi_access"
        assert row["meta"]["facility_scope"] == [F2]
    assert not _rows(mark, "patient_scope_denied", world["admin_f2"])


def test_a_ward_nurse_probing_another_facility_is_recorded_as_a_facility_denial(world, fence_open):
    for lane, helper in ((_chart, "require_patient_access"), (_scribe, "enforce_patient_scope")):
        mark = _last_audit_id()
        assert lane(world, "nurse_f1", "p_f2").status_code == 404
        rows = _rows(mark, "patient_scope_denied", world["nurse_f1"], world["p_f2"])
        assert [r["meta"]["scope_cause"] for r in rows] == ["facility"], helper
        assert rows[0]["meta"]["department"] == DEPT_GM


def test_a_department_caused_denial_in_enforce_patient_scope_still_writes_no_row(world, fence_open):
    mark = _last_audit_id()
    assert _scribe(world, "nurse_f1", "p_f1_surg").status_code == 404
    assert _rows(mark, "patient_scope_denied", world["nurse_f1"]) == []
    # require_patient_access keeps its department row, unchanged in shape.
    assert _chart(world, "nurse_f1", "p_f1_surg").status_code == 404
    rows = _rows(mark, "patient_scope_denied", world["nurse_f1"], world["p_f1_surg"])
    assert len(rows) == 1
    assert rows[0]["meta"] == {"department": DEPT_GM, "scope_source": "authenticated_user"}


def test_the_ward_nurse_still_reads_their_own_ward(world, fence_open):
    assert _chart(world, "nurse_f1", "p_f1").status_code == 200
    assert _scribe(world, "nurse_f1", "p_f1").status_code == 200
    assert _chart(world, "nurse_f2", "p_f2").status_code == 200


def test_the_latest_encounter_decides_for_a_department_less_account(world, fence_open):
    assert _chart(world, "admin_f1", "p_moved").status_code == 404
    assert _chart(world, "admin_f2", "p_moved").status_code == 200
    assert _chart(world, "admin_f2", "p_f2_out").status_code == 200
    assert _chart(world, "admin_f1", "p_f2_out").status_code == 404


def test_an_unlisted_department_sees_nothing_and_says_why(world, fence_open):
    mark = _last_audit_id()
    assert _chart(world, "nurse_unknown", "p_unknown").status_code == 404
    assert _scribe(world, "nurse_unknown", "p_unknown").status_code == 404
    rows = _rows(mark, "patient_scope_denied", world["nurse_unknown"], world["p_unknown"])
    assert len(rows) == 2
    for row in rows:
        assert row["meta"]["scope_cause"] == "facility"
        assert row["meta"]["facility_scope"] == []
        assert "belongs to no facility" in row["summary_message"]


def test_an_enterprise_admin_reads_across_facilities(world, fence_open):
    mark = _last_audit_id()
    for p in ("p_f1", "p_f2"):
        assert _chart(world, "admin_ent", p).status_code == 200
    rows = _rows(mark, "patient_notes_viewed", world["admin_ent"])
    assert len(rows) == 2 and all(r["meta"]["facility_scope"] is None for r in rows)


def test_a_missing_patient_is_not_a_facility_denial(world, fence_open):
    mark = _last_audit_id()
    resp = client.get("/api/vigil/patients/NO-SUCH-FSC-PATIENT/notes",
                      headers=_auth(world, "admin_f2"))
    assert resp.status_code in (200, 404)
    assert _rows(mark, "patient_scope_denied", world["admin_f2"]) == []
    failures = _rows(mark, "patient_notes_viewed", world["admin_f2"], "NO-SUCH-FSC-PATIENT")
    assert [r["outcome"] for r in failures] == ["failure"]
    assert failures[0]["meta"]["reason"] == "not_found"


def test_enforce_patient_scope_directly(world):
    from fastapi import HTTPException
    admin_f2 = _user(world, "admin_f2")
    P.enforce_patient_scope(admin_f2, world["p_f2"])
    with pytest.raises(HTTPException) as caught:
        P.enforce_patient_scope(admin_f2, world["p_f1"])
    assert caught.value.status_code == 404
    P.enforce_patient_scope(admin_f2, None)   # no patient named: nothing to scope
    P.enforce_patient_scope(_user(world, "admin_ent"), world["p_f1"])


# ═════════════════════════════════════════════════════════════════════════════
#  4. UserOut and /api/auth/me
# ═════════════════════════════════════════════════════════════════════════════

def test_me_carries_the_facility(world):
    me = client.get("/api/auth/me", headers=_auth(world, "nurse_f2"))
    assert me.status_code == 200
    assert me.json()["facility"] == {"id": F2, "code": F2_CODE, "name": "Facility Scope Two"}
    assert me.json()["department"] == DEPT_CARD
    assert client.get("/api/auth/me", headers=_auth(world, "pharm_f2")).json()["facility"]["id"] == F2
    assert client.get("/api/auth/me", headers=_auth(world, "cashier_f1")).json()["facility"]["id"] == 1
    # The grant widens what the admin sees, not where they work.
    assert client.get("/api/auth/me", headers=_auth(world, "admin_ent")).json()["facility"]["id"] == 1
    assert client.get("/api/auth/me", headers=_auth(world, "nurse_unknown")).json()["facility"] is None


def test_me_names_facility_one_with_one_facility(world, one_facility):
    body = client.get("/api/auth/me", headers=_auth(world, "nurse_f2")).json()
    assert body["facility"] == {"id": 1, "code": "MAIN", "name": FS.display_name(1)}


# ═════════════════════════════════════════════════════════════════════════════
#  5. One facility: exactly as before
# ═════════════════════════════════════════════════════════════════════════════

def test_one_facility_resolves_to_no_limit_without_reading_the_maps(world, one_facility,
                                                                   monkeypatch):
    def boom(db):
        raise AssertionError("the scope maps were read with one facility on record")
    monkeypatch.setattr(FS, "_load_scope_maps", boom)
    for key in ACCOUNTS:
        assert FS.facility_scope_for(_user(world, key)) is None


def test_one_facility_keeps_the_sql_strings(one_facility):
    dept_sql = f"SELECT p.id FROM patients p WHERE {P.PATIENT_DEPARTMENT_SQL} = ?"
    assert P.scope_patient_ids_sql(None) == ("SELECT p.id FROM patients p", ())
    assert P.scope_patient_ids_sql("Ward X") == (dept_sql, ("Ward X",))
    assert P.scope_patient_ids_sql("Ward X", facilities=None) == (dept_sql, ("Ward X",))


def _queries(statements: list[str]) -> list[str]:
    """The statements a helper ran, without the pool's liveness probe
    (db_backend runs SELECT 1 each time it hands out a pooled connection)."""
    return [s for s in statements
            if s.lstrip().upper().startswith("SELECT") and s.strip() != "SELECT 1"]


def test_one_facility_runs_the_same_statements(world, one_facility, statements):
    FS.prime_cache()
    nurse, admin = _user(world, "nurse_f1"), _user(world, "admin_f1")
    statements.clear()
    with db_session() as db:
        assert P.patient_in_scope(db, world["p_f1"], DEPT_GM) is True
    P.enforce_patient_scope(nurse, world["p_f1"])
    P.enforce_patient_scope(admin, world["p_f1"])
    P.patient_in_scope_blocking(world["p_f1"], None)
    scope_runs = [s for s in statements if SCOPE_SQL_MARK in s]
    assert len(scope_runs) == 2      # one per department check, none for the admin
    # The trace shows each statement with its values filled in.
    assert all(s.startswith(f"SELECT 1 FROM patients p WHERE p.id = '{world['p_f1']}' AND ")
               for s in scope_runs)
    assert not [s for s in statements if any(w in s.lower() for w in FACILITY_WORDS)]
    assert len(_queries(statements)) == 2


def test_one_facility_requests_run_the_same_statements(world, one_facility, statements,
                                                       fence_open):
    FS.prime_cache()
    for lane, key in ((_chart, "nurse_f1"), (_scribe, "nurse_f1"), (_chart, "admin_f1")):
        statements.clear()
        assert lane(world, key, "p_f1").status_code == 200
        assert not [s for s in statements if any(w in s.lower() for w in FACILITY_WORDS)], key
        expected = 0 if key == "admin_f1" else 1
        assert len([s for s in statements if SCOPE_SQL_MARK in s]) == expected


def test_one_facility_rows_carry_no_facility_key(world, one_facility, fence_open):
    mark = _last_audit_id()
    assert _chart(world, "nurse_f1", "p_f1").status_code == 200
    assert _scribe(world, "nurse_f1", "p_f1").status_code == 200
    assert _scribe(world, "nurse_f1", "p_f1_surg").status_code == 404
    reads = (_rows(mark, "patient_notes_viewed", world["nurse_f1"])
             + _rows(mark, "scribe_notes_listed", world["nurse_f1"]))
    assert len(reads) == 2
    assert all("facility_scope" not in r["meta"] for r in reads)
    assert _rows(mark, "patient_scope_denied", world["nurse_f1"]) == []


def test_two_facilities_cost_one_statement_per_check(world, statements):
    FS.prime_cache()
    nurse, admin = _user(world, "nurse_f1"), _user(world, "admin_f2")
    FS.facility_scope_for(nurse)          # warm the maps
    statements.clear()
    P.enforce_patient_scope(nurse, world["p_f1"])
    P.enforce_patient_scope(admin, world["p_f2"])
    queries = _queries(statements)
    assert len(queries) == 3              # facility + department, then facility alone
    assert [("facility_id" in q, SCOPE_SQL_MARK in q) for q in queries] == [
        (True, False), (False, True), (True, False)]
