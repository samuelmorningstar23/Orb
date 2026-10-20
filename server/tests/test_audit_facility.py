"""Audit attribution and every audit reader across facilities (multisite-11, W12-C).

The module owns its database (isolated_module_database), so the second
facility never reaches the session database. The first tests run with one
facility on record and pin that nothing changes there; the `two` fixture then
writes a row from before the expansion, adds facility 2 with its audit epoch
(as facility_identity.create_facility does), and the rest pin the writer's
resolver, the truncation stub, the chain, and every reader under the epoch
rule. HTTP tests open the fence (FACILITY_BLIND_SURFACES = ()), as the leak
sweep does: /api/audit and /api/flight-recorder stay fenced until the wave's
fence lists are regenerated. Test people carry invented names. Run with the
test env, never the shipping .env.
"""

from __future__ import annotations

import io
import json
import sqlite3
import uuid
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import facility_fence as F
import main
from auth import create_token
from database import db_session, verify_audit_chain
from scripts import build_audit_facility_index as CLI
from services import audit_facility as AF
from services import facility_service as FS
from services import flight_recorder_service as FR
from services.audit_service import _MAX_METADATA_BYTES, log_audit_event
from services.clinical_time import utc_iso_now

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9611  # an id no other module writes
F2_CODE = f"AF{TAG[:4]}"
F1_DEPT = f"Vostrelline Medicine {TAG}"
F2_DEPT = f"Quandrevel Surgery {TAG}"

P1 = f"AFPA{TAG}"  # admitted at facility 1
P2 = f"AFPB{TAG}"  # admitted at facility 2
ADM1 = f"AFAA{TAG}"  # administrator at facility 1 (no home row: facility 1)
ADM2 = f"AFAB{TAG}"  # administrator whose home is facility 2
ENT = f"AFEN{TAG}"  # administrator with group-wide access
DOC1 = f"AFDA{TAG}"
DOC2 = f"AFDB{TAG}"

USERS = {
    ADM1: ("Orsolya Quandt", "admin", None),
    ADM2: ("Thessaly Vrocomb", "admin", None),
    ENT: ("Ilvaro Pentecrue", "admin", None),
    DOC1: ("Marisol Ekwuendo", "doctor", F1_DEPT),
    DOC2: ("Teodric Halvanne", "doctor", F2_DEPT),
}

client = TestClient(main.app)


def _hdr(uid: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, USERS[uid][1])}"}


def _user(uid: str) -> dict:
    name, role, dept = USERS[uid]
    return {"id": uid, "name": name, "role": role, "department": dept}


def _log(corr: str | None = None, **kw) -> dict:
    """Write one row through the real writer and return it."""
    corr = corr or f"afac-{uuid.uuid4().hex[:12]}"
    args = {
        "event_category": "api_mutation",
        "action_type": f"afac_probe_{TAG}",
        "outcome": "success",
        "summary_message": "audit facility probe",
    }
    args.update(kw)
    log_audit_event(corr, **args)
    return _row(corr)


def _row(corr: str) -> dict:
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM audit_logs WHERE correlation_id = ? ORDER BY id DESC LIMIT 1", (corr,)
        ).fetchone()
    assert row is not None, corr
    return dict(row)


def _meta(row: dict) -> dict:
    return json.loads(row["metadata_json"]) if row["metadata_json"] else {}


@pytest.fixture(autouse=True)
def _fresh_caches():
    FS.invalidate_cache()
    AF.invalidate_cache()
    yield
    FS.invalidate_cache()
    AF.invalidate_cache()


@pytest.fixture()
def fence_open(monkeypatch):
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


# ── 1. One facility on record: nothing changes ──────────────────────────────


def test_one_facility_stores_the_metadata_byte_for_byte(monkeypatch):
    assert FS.multi_facility_data() is False

    def boom(*a, **k):
        raise AssertionError("no facility lookup with one facility on record")

    monkeypatch.setattr(AF, "patient_facility", boom)
    monkeypatch.setattr(AF, "actor_facility", boom)
    meta = {"department": "Ward 7", "nested": {"a": [1, 2]}, "unicode": "Ravikirán"}
    row = _log(metadata=meta, patient_id="NOBODY", actor_user_id="NOBODY", facility_id=7)
    assert row["metadata_json"] == json.dumps(meta)
    assert _log(metadata=None)["metadata_json"] is None
    assert _log(metadata={})["metadata_json"] is None


def test_one_facility_truncation_stub_is_unchanged():
    blob = "M" * (_MAX_METADATA_BYTES * 2)
    row = _log(metadata={"blob": blob})
    original = len(json.dumps({"blob": blob}).encode("utf-8"))
    assert row["metadata_json"] == json.dumps(
        {"_truncated": True, "original_bytes": original}, separators=(",", ":")
    )


def test_one_facility_audit_reads_answer_as_before(fence_open):
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'admin', 'x')",
            (ADM1, USERS[ADM1][0]),
        )
    corr = _log(actor_user_id=ADM1, actor_role="admin")["correlation_id"]
    body = client.get(f"/api/audit/logs?correlation_id={corr}", headers=_hdr(ADM1)).json()
    assert set(body) == {"logs", "total", "limit", "offset"}
    assert body["total"] == 1 and "facility" not in body["logs"][0]
    narrowed = client.get(f"/api/audit/logs?correlation_id={corr}&facility=1", headers=_hdr(ADM1))
    assert narrowed.status_code == 200 and narrowed.json()["total"] == 1
    assert client.get("/api/audit/logs?facility=2", headers=_hdr(ADM1)).status_code == 404
    assert FR.verify_chain()["ok"] is True


# ── 2. Two facilities ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def two(isolated_module_database):
    """A row from before the expansion, then facility 2 with its audit epoch."""
    pre = f"afac-pre-{TAG}"
    log_audit_event(
        pre,
        "phi_access",
        f"afac_pre_{TAG}",
        "success",
        patient_id=P2,
        summary_message="written while one facility was on record",
    )
    now = utc_iso_now()
    with db_session() as db:
        epoch = db.execute("SELECT MAX(id) FROM audit_logs").fetchone()[0]
        db.execute(
            "INSERT INTO facilities (id, uuid, code, name, audit_epoch_id) VALUES (?, ?, ?, ?, ?)",
            (F2, str(uuid.uuid4()), F2_CODE, "Quandrevel Satellite", epoch),
        )
        for name, fid in ((F1_DEPT, 1), (F2_DEPT, F2)):
            db.execute(
                "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
                (name, fid),
            )
        for uid, (name, role, dept) in USERS.items():
            db.execute(
                "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
                "VALUES (?, ?, ?, ?, 'x')",
                (uid, name, role, dept),
            )
        db.execute(
            "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, "
            "reason) VALUES (?, ?, 'home', 'system', ?, 'audit facility test')",
            (ADM2, F2, now),
        )
        db.execute(
            "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
            "VALUES (?, 'system', ?, 'audit facility test')",
            (ENT, now),
        )
        for pid, name, fid, dept in (
            (P1, "Wenceslava Orrimund", 1, F1_DEPT),
            (P2, "Caspian Threlkeld", F2, F2_DEPT),
        ):
            db.execute(
                "INSERT INTO patients (id, name, mrn, status, registered_facility_id) "
                "VALUES (?, ?, ?, 'admitted', ?)",
                (pid, name, f"MRN-{pid}", fid),
            )
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                "status, facility_id, created_at) VALUES (?, ?, 'system', ?, 'Ward Q', '1', "
                "'active', ?, ?)",
                (f"ADM-{pid}", pid, dept, fid, now),
            )
    FS.invalidate_cache()
    AF.invalidate_cache()
    assert FS.multi_facility_data() is True
    try:
        yield {"pre": pre, "epoch": int(epoch)}
    finally:
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
        FS.invalidate_cache()
        AF.invalidate_cache()


def test_the_epoch_is_the_audit_id_before_the_expansion(two):
    with db_session() as db:
        assert AF.audit_epoch(db) == two["epoch"]
    assert _row(two["pre"])["id"] <= two["epoch"]
    assert "facility_id" not in _meta(_row(two["pre"]))


def test_an_explicit_facility_wins(two):
    assert _meta(_log(facility_id=F2, patient_id=P1))["facility_id"] == F2


def test_the_patient_then_the_actor(two):
    assert _meta(_log(patient_id=P2, actor_user_id=ADM1))["facility_id"] == F2
    assert _meta(_log(patient_id=P1, actor_user_id=DOC2))["facility_id"] == 1
    assert _meta(_log(actor_user_id=DOC2))["facility_id"] == F2
    assert _meta(_log(actor_user_id=DOC1))["facility_id"] == 1
    assert _meta(_log(actor_user_id=ADM2))["facility_id"] == F2
    assert _meta(_log(actor_user_id=ADM1))["facility_id"] == 1
    # A patient id that matches no patient falls through to the actor.
    assert _meta(_log(patient_id="NO-SUCH-PATIENT", actor_user_id=DOC2))["facility_id"] == F2


def test_a_sign_in_with_no_actor_belongs_to_the_account_it_named(two):
    row = _log(
        event_category="authentication",
        action_type="login_failure",
        outcome="failure",
        metadata={"attempted_id": DOC2},
    )
    assert _meta(row) == {"attempted_id": DOC2, "facility_id": F2}


def test_nobody_to_attribute_marks_the_row_unresolved(two):
    for kw in ({}, {"actor_user_id": "system"}, {"actor_user_id": "NO-SUCH-ACCOUNT"}):
        meta = _meta(_log(**kw))
        assert meta == {"facility_unresolved": True}, kw


def test_a_facility_the_caller_named_is_kept(two):
    assert _meta(_log(patient_id=P2, metadata={"facility_id": 1})) == {"facility_id": 1}
    assert _meta(_log(patient_id=P2, metadata={"facility_id": None})) == {
        "facility_id": None,
        "facility_unresolved": True,
    }


def test_a_resolver_failure_still_writes_the_fail_closed_row(two, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(AF, "patient_facility", boom)
    row = _log(event_category="phi_access", patient_id=P2, metadata={"what": "chart"})
    assert _meta(row) == {"what": "chart", "facility_unresolved": True}
    monkeypatch.setattr(FS, "user_facility_id", boom)
    row = _log(event_category="security", actor_user_id=DOC2)
    assert _meta(row) == {"facility_unresolved": True}
    assert verify_audit_chain()["ok"] is True


def test_an_oversized_stub_keeps_the_facility_and_the_chain_verifies(two):
    blob = "M" * (_MAX_METADATA_BYTES * 3)
    row = _log(patient_id=P2, metadata={"blob": blob})
    stored = row["metadata_json"]
    assert len(stored.encode("utf-8")) <= _MAX_METADATA_BYTES
    stub = json.loads(stored)
    assert stub["_truncated"] is True and stub["facility_id"] == F2
    assert stub["original_bytes"] > _MAX_METADATA_BYTES
    unresolved = json.loads(_log(metadata={"blob": blob})["metadata_json"])
    assert unresolved["_truncated"] is True and unresolved["facility_unresolved"] is True
    assert "facility_id" not in unresolved
    assert verify_audit_chain()["ok"] is True
    assert FR.verify_chain()["ok"] is True


def test_the_chain_verifies_across_the_epoch(two):
    _log(patient_id=P1)
    result = verify_audit_chain()
    assert result["ok"] is True and result["broken_row_ids"] == []
    with db_session() as db:
        lo, hi = db.execute("SELECT MIN(id), MAX(id) FROM audit_logs").fetchone()
    assert lo <= two["epoch"] < hi


def test_the_reading_rule_row_by_row(two):
    epoch = two["epoch"]
    pre = {"id": epoch, "metadata_json": None}
    later = {"id": epoch + 1, "metadata_json": json.dumps({"facility_unresolved": True})}
    at_two = {"id": epoch + 2, "metadata_json": json.dumps({"facility_id": F2})}
    assert AF.row_facility(pre, epoch) == 1 and AF.row_facility(later, epoch) is None
    assert AF.row_visible(pre, {1}, epoch) and not AF.row_visible(pre, {F2}, epoch)
    assert not AF.row_visible(later, {1}, epoch) and AF.row_visible(later, None, epoch)
    assert AF.row_visible(at_two, {F2}, epoch) and not AF.row_visible(at_two, {1}, epoch)
    assert not AF.row_visible(at_two, frozenset(), epoch)
    assert AF.visible_sql(None, epoch) == ("", ())
    assert AF.visible_sql(frozenset(), epoch) == ("0", ())


# ── 3. The readers ───────────────────────────────────────────────────────────


def _logs(uid: str, query: str) -> dict:
    resp = client.get(f"/api/audit/logs?{query}", headers=_hdr(uid))
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_rows_from_before_the_expansion_are_facility_ones(two, fence_open):
    q = f"correlation_id={two['pre']}"
    mine = _logs(ADM1, q)
    assert mine["total"] == 1 and mine["logs"][0]["facility"]["id"] == 1
    assert [f["id"] for f in mine["facilities"]] == [1]
    assert _logs(ADM2, q)["total"] == 0
    group = _logs(ENT, q)
    assert group["total"] == 1
    assert [f["id"] for f in group["facilities"]] == [1, F2]


def test_a_later_row_naming_no_facility_is_for_group_wide_access_only(two, fence_open):
    corr = _log()["correlation_id"]
    assert _logs(ADM1, f"correlation_id={corr}")["total"] == 0
    assert _logs(ADM2, f"correlation_id={corr}")["total"] == 0
    group = _logs(ENT, f"correlation_id={corr}")
    assert group["total"] == 1 and group["logs"][0]["facility"] is None


def test_each_administrator_reads_their_own_facility(two, fence_open):
    corr = _log(patient_id=P2)["correlation_id"]
    theirs = _logs(ADM2, f"correlation_id={corr}")
    assert theirs["total"] == 1
    assert theirs["logs"][0]["facility"] == {"id": F2, "code": F2_CODE, "name": "Quandrevel Satellite"}
    assert _logs(ADM1, f"correlation_id={corr}")["total"] == 0
    assert _logs(ENT, f"patient_id={P2}&facility={F2}")["total"] >= 1


def test_the_facility_filter_only_narrows(two, fence_open):
    q = f"correlation_id={two['pre']}"
    assert _logs(ENT, f"{q}&facility={F2}")["total"] == 0
    assert _logs(ENT, f"{q}&facility=1")["total"] == 1
    for uid, fid in ((ADM1, F2), (ADM2, 1), (ENT, 999999), (ADM1, 999999)):
        resp = client.get(f"/api/audit/logs?{q}&facility={fid}", headers=_hdr(uid))
        assert resp.status_code == 404, (uid, fid)
        assert resp.json()["detail"]["error"] == "not_found"


def test_the_filter_lists_come_from_the_rows_the_reader_may_see(two, fence_open):
    action = f"afac_only_one_{TAG}"
    _log(action_type=action, facility_id=1)

    def actions(uid: str) -> list:
        resp = client.get("/api/audit/facets?event_category=api_mutation", headers=_hdr(uid))
        assert resp.status_code == 200, resp.text
        return resp.json()["actions"]

    assert action in actions(ADM1) and action in actions(ENT)
    assert action not in actions(ADM2)


def test_the_flight_recorder_replays_only_the_readers_facility(two, fence_open):
    audit_row = _log(patient_id=P1, action_type=f"afac_fr_{TAG}")
    order_id = f"ORD-AF{TAG}"
    ev_patient, ev_system = str(uuid.uuid4()), str(uuid.uuid4())
    with db_session() as db:
        for event_id, pid in ((ev_patient, P1), (ev_system, None)):
            db.execute(
                "INSERT INTO event_log (event_id, topic, source_node, event_type, patient_id, "
                "payload, timestamp, correlation_id) VALUES (?, 'vigil.test', 'vigil', "
                "'afac_event', ?, '{}', ?, ?)",
                (event_id, pid, utc_iso_now(), f"afac-ev-{TAG}"),
            )
        db.execute(
            "INSERT INTO orders (id, patient_id, order_type, description, status, ordered_by) "
            "VALUES (?, ?, 'lab', 'Vostrel panel', 'active', 'system')",
            (order_id, P1),
        )
        oe = db.execute(
            "INSERT INTO order_events (order_id, prior_status, new_status, actor_id, created_at) "
            "VALUES (?, 'draft', 'active', 'system', ?)",
            (order_id, utc_iso_now()),
        ).lastrowid

    def timeline(uid: str) -> list:
        resp = client.get(f"/api/flight-recorder/timeline?patient_id={P1}", headers=_hdr(uid))
        assert resp.status_code == 200, resp.text
        return resp.json()["events"]

    sources = {e["source"] for e in timeline(ADM1)}
    assert {"audit", "event", "order"} <= sources
    # Facility 2's administrator sees none of facility 1's patient's events, and
    # of that patient's audit rows only those a writer attributed to facility 2
    # (test_an_explicit_facility_wins wrote one).
    theirs = timeline(ADM2)
    assert {e["source"] for e in theirs} <= {"audit"}
    assert audit_row["id"] not in {e["id"] for e in theirs}
    with db_session() as db:
        for e in theirs:
            stored = db.execute(
                "SELECT metadata_json FROM audit_logs WHERE id = ?", (e["id"],)
            ).fetchone()
            assert json.loads(stored[0])["facility_id"] == F2

    def detail(uid: str, source: str, event_id) -> int:
        url = f"/api/flight-recorder/event/{source}/{event_id}"
        return client.get(url, headers=_hdr(uid)).status_code

    assert detail(ADM1, "audit", audit_row["id"]) == 200
    assert detail(ADM2, "audit", audit_row["id"]) == 404
    assert detail(ADM1, "event", ev_patient) == 200
    assert detail(ADM2, "event", ev_patient) == 404
    assert detail(ADM1, "event", ev_system) == 404
    assert detail(ENT, "event", ev_system) == 200
    assert detail(ADM1, "order", oe) == 200
    assert detail(ADM2, "order", oe) == 404
    pre_id = _row(two["pre"])["id"]
    assert detail(ADM2, "audit", pre_id) == 404
    assert detail(ADM1, "audit", pre_id) == 200

    def verify(uid: str) -> dict:
        resp = client.get(f"/api/flight-recorder/verify?patient_id={P1}", headers=_hdr(uid))
        assert resp.status_code == 200, resp.text
        return resp.json()

    f2_rows = len(theirs)
    mine, theirs_count = verify(ADM1), verify(ADM2)
    assert mine["ok"] is True and theirs_count["ok"] is True
    assert mine["count"] >= 1 and theirs_count["count"] == f2_rows
    assert verify(ENT)["count"] == mine["count"] + f2_rows
    whole = client.get("/api/flight-recorder/verify", headers=_hdr(ADM2)).json()
    assert whole["count"] == FR.verify_chain()["count"]


def test_the_overview_counts_per_facility(two, fence_open):
    category = f"afac_cat_{TAG}".lower()
    for fid, n in ((F2, 2), (1, 3)):
        for _ in range(n):
            _log(event_category=category, facility_id=fid)

    def counted(uid: str) -> int:
        resp = client.get("/api/admin/overview", headers=_hdr(uid))
        assert resp.status_code == 200, resp.text
        return resp.json()["audit_categories"].get(category, 0)

    assert (counted(ADM2), counted(ADM1), counted(ENT)) == (2, 3, 5)


def test_the_staff_list_sign_in_columns_read_the_readers_rows(two, fence_open):
    at_two = _log(
        event_category="authentication", action_type="login_success", actor_user_id=DOC2
    )
    at_one = _log(
        event_category="authentication",
        action_type="login_success",
        actor_user_id=DOC2,
        facility_id=1,
    )
    _log(
        event_category="authentication",
        action_type="login_failure",
        outcome="failure",
        metadata={"attempted_id": DOC2},
    )

    def doctor(uid: str) -> dict:
        resp = client.get("/api/admin/users", headers=_hdr(uid))
        assert resp.status_code == 200, resp.text
        return next(u for u in resp.json()["users"] if u["id"] == DOC2)

    theirs, group = doctor(ADM2), doctor(ENT)
    assert theirs["last_login"] == at_two["timestamp"]
    assert group["last_login"] == at_one["timestamp"]
    assert theirs["failed_logins_24h"] >= 1


def test_the_compliance_coverage_counts_the_readers_rows(two):
    from services.compliance_service import compliance_report

    category = f"afac_cov_{TAG}".lower()
    _log(event_category=category, facility_id=F2)
    _log(event_category=category, facility_id=1)
    _log(event_category=category, facility_id=1)

    def counted(user: dict | None) -> int:
        return compliance_report(user=user)["audit_coverage"]["by_category"].get(category, 0)

    assert counted(_user(ADM2)) == 1
    assert counted(_user(ADM1)) == 2
    assert counted(_user(ENT)) == 3
    assert counted(None) == 3


def test_the_impact_audit_figure_counts_the_readers_rows(two):
    from services.impact_service import compute_impact

    def evidence(uid: str) -> int:
        report = compute_impact(user=_user(uid))
        return next(m for m in report["metrics"] if m["key"] == "medication_safety")[
            "evidence_count"
        ]

    before = {uid: evidence(uid) for uid in (ADM1, ADM2, ENT)}
    for fid in (1, 1, F2):
        _log(event_category="clinical_access", action_type="medication_verify", facility_id=fid)
    after = {uid: evidence(uid) for uid in (ADM1, ADM2, ENT)}
    assert after[ADM1] - before[ADM1] == 2
    assert after[ADM2] - before[ADM2] == 1
    assert after[ENT] - before[ENT] == 3


def test_the_patients_own_access_history_lists_every_facility(two):
    from services import consent_service

    first, second = f"afac_hist_one_{TAG}", f"afac_hist_two_{TAG}"
    _log(action_type=first, patient_id=P2, facility_id=1)
    _log(action_type=second, patient_id=P2)
    export = consent_service.data_principal_export(P2)
    actions = {r["action_type"] for r in export["sections"]["access_history"]["rows"]}
    assert {first, second, f"afac_pre_{TAG}"} <= actions


# ── 4. The index maintenance command ─────────────────────────────────────────


def _copy_of_the_module_database(dest: Path) -> Path:
    import database

    src = sqlite3.connect(str(database.DB_PATH))
    out = sqlite3.connect(str(dest))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dest


def _cli(argv: list) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = CLI.main(argv)
    return code, buf.getvalue()


def test_the_index_command_checks_then_builds_on_a_copy(two, tmp_path):
    copy = _copy_of_the_module_database(tmp_path / "copy.db")
    code, out = _cli(["--db", str(copy), "--check", "--json"])
    facts = json.loads(out)
    assert code == 0 and facts["index_exists"] is False and facts["built"] is False
    assert facts["unreadable_rows"] == 0 and facts["audit_rows"] > 0
    code, _ = _cli(["--db", str(copy), "--build"])
    assert code == 1  # --yes is required
    code, out = _cli(["--db", str(copy), "--build", "--yes", "--json"])
    built = json.loads(out)
    assert code == 0 and built["built"] is True and built["index_exists"] is True
    assert isinstance(built["seconds"], float) and built["seconds"] >= 0
    assert built["uses_index"] is True
    code, out = _cli(["--db", str(copy)])
    assert code == 0 and "already built" in out
    conn = sqlite3.connect(str(copy))
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (AF.INDEX_NAME,)
        ).fetchone()
        rows = conn.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'audit_facility_index_built'"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["event_category"] == "site_admin"
        assert rows[0]["actor_role"] == "console"
        assert verify_audit_chain(conn)["ok"] is True
    finally:
        conn.close()
    # The copy only: the module's own database has no index and no such row.
    with db_session() as db:
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (AF.INDEX_NAME,)
        ).fetchone()
        assert not db.execute(
            "SELECT 1 FROM audit_logs WHERE action_type = 'audit_facility_index_built'"
        ).fetchone()


def test_the_index_command_refuses_metadata_sqlite_cannot_read(two, tmp_path):
    copy = _copy_of_the_module_database(tmp_path / "bad.db")
    conn = sqlite3.connect(str(copy))
    try:
        cur = conn.execute(
            "INSERT INTO audit_logs (timestamp, correlation_id, event_category, outcome, "
            "action_type, metadata_json) VALUES (?, 'afac-bad', 'api_mutation', 'success', "
            "'afac_bad', 'not json {')",
            (utc_iso_now(),),
        )
        bad_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    code, out = _cli(["--db", str(copy), "--json"])
    facts = json.loads(out)
    assert code == 0 and facts["unreadable_rows"] == 1 and facts["unreadable_ids"] == [bad_id]
    code, _ = _cli(["--db", str(copy), "--build", "--yes"])
    assert code == 1
    conn = sqlite3.connect(str(copy))
    try:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (AF.INDEX_NAME,)
        ).fetchone()
    finally:
        conn.close()


def test_the_index_command_never_creates_a_database(tmp_path):
    missing = tmp_path / "nowhere.db"
    code, _ = _cli(["--db", str(missing), "--check"])
    assert code == 2 and not missing.exists()


def test_the_index_statement_is_the_one_the_create_gate_names():
    from services import facility_identity

    assert AF.INDEX_NAME == facility_identity.AUDIT_FACILITY_INDEX
    assert "json_extract(metadata_json, '$.facility_id'), timestamp" in AF.INDEX_SQL
