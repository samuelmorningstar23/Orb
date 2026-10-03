"""DPDP Act 2023 machinery — consent, data-principal rights, retention,
erasure adjudication, and breach records.

The load-bearing assertion in this file is
``test_export_contains_this_patient_and_nobody_else``: the data-principal export
is itself a PHI disclosure, and an export that leaks one row of a second patient
turns a compliance feature into the fourth PHI leak of the month.

TEST-ISOLATION LAW: every patient seeded here is admitted with
``status='discharged'`` from the outset and deleted in ``_cleanup``, so nothing
this module writes can show up in another module's census. The consent, erasure
and breach rows go too — which means teardown has to LIFT the append-only
triggers first and put them back, and that round trip is itself the proof the
triggers are armed. The staff accounts (``DPDP-*``) are left in place, following
the suite-wide convention for ``INSERT OR IGNORE`` seed users; they are named
distinctly enough not to collide, and audit rows reference them by id.
"""
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from auth import hash_password  # noqa: E402
from database import db_session  # noqa: E402
from db_backend import DBIntegrityError, DBOperationalError  # noqa: E402

from services import consent_service as cs  # noqa: E402

client = TestClient(app)

DEPT = "DPDP Medicine"
OTHER_DEPT = "DPDP Surgery"

# Distinctive strings so a cross-patient leak is unmistakable in a diff.
A_MARK = "AAA-PRINCIPAL-ALPHA"
B_MARK = "BBB-PRINCIPAL-BRAVO"


# ════════════════════════════════════════════════════════════════
#  Fixtures
# ════════════════════════════════════════════════════════════════

def _ensure_user(user_id: str, role: str, department: str | None):
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, f"DPDP {role}", role, department, hash_password("password")),
        )
        # A user seeded by an earlier module may carry the wrong department;
        # scope is derived from this column, so pin it every time.
        db.execute("UPDATE users SET department = ? WHERE id = ?", (department, user_id))


def _login(user_id: str) -> str:
    resp = client.post("/api/auth/login",
                       json={"employee_id": user_id, "password": "password"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_patient(mark: str, department: str) -> str:
    """One patient with a row in as many patient-keyed tables as we can reach,
    every free-text field carrying `mark`."""
    pid = f"DPDP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, phone, address, emergency_contact_name,
                                     emergency_contact_phone)
               VALUES (?, ?, ?, ?, ?, 54, 'F', ?, 'admitted', ?, ?, ?, ?)""",
            (pid, f"Patient {mark}", f"MRN-{mark}", f"{mark}-BED", f"{department} bay",
             f"Diagnosis {mark}", f"PHONE-{mark}", f"ADDRESS-{mark}",
             f"KIN-{mark}", f"KINPHONE-{mark}"),
        )
        # status='discharged' from the start: this module must never leave an
        # admitted patient behind for another module's census to count.
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, "
            "admitted_by, chief_complaint) VALUES (?, ?, ?, ?, 'discharged', 'DPDP-DOC', ?)",
            (pid, department, department, f"{mark}-BED", f"COMPLAINT-{mark}"),
        )
        db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id) "
            "VALUES (?, 'Progress Note', ?, ?, 'DPDP-DOC')",
            (pid, f"NOTE-TITLE-{mark}", f"NOTE-BODY-{mark}"),
        )
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
            "status, prescribed_by) VALUES (?, ?, '500 mg', 'PO', 'BD', 'active', 'DPDP-DOC')",
            (pid, f"DRUG-{mark}"),
        )
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, severity, "
            "recorded_by) VALUES (?, ?, ?, 'severe', 'DPDP-DOC')",
            (pid, f"ALLERGEN-{mark}", f"REACTION-{mark}"),
        )
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
            "diastolic_bp, spo2, temperature, respiratory_rate) "
            "VALUES (?, ?, 96, 118, 74, 97, 36.9, 18)",
            (pid, datetime.now(timezone.utc).isoformat()),
        )
        db.execute(
            "INSERT INTO sage_observations (room_id, patient_id, observation_type, "
            "content, confidence) VALUES (?, ?, 'concern', ?, 0.7)",
            (f"room-{pid}", pid, f"AIOBS-{mark}"),
        )
        db.execute(
            "INSERT INTO problem_list (patient_id, problem, status, recorded_by) "
            "VALUES (?, ?, 'active', 'DPDP-DOC')",
            (pid, f"PROBLEM-{mark}"),
        )
    return pid


@pytest.fixture(scope="module", autouse=True)
def _fixtures():
    _ensure_user("DPDP-DOC", "doctor", DEPT)
    _ensure_user("DPDP-NURSE", "nurse", DEPT)
    _ensure_user("DPDP-ADM", "admin", None)          # unscoped governance account
    _ensure_user("DPDP-ADM-SURG", "admin", OTHER_DEPT)  # scoped admin
    pid_a = _seed_patient(A_MARK, DEPT)
    pid_b = _seed_patient(B_MARK, DEPT)
    yield {"a": pid_a, "b": pid_b}
    _cleanup(pid_a, pid_b)
    # The breach register is not patient-keyed, so it is cleared once at module
    # teardown rather than inside _cleanup — which individual tests call
    # mid-module and which must not delete a register row another test is
    # still asserting on.
    with db_session() as db:
        for trg in _APPEND_ONLY_TRIGGERS:
            db.execute(f"DROP TRIGGER IF EXISTS {trg}")
        db.execute("DELETE FROM breach_notifications WHERE recorded_by = 'DPDP-ADM'")
        db.execute("DELETE FROM breach_records WHERE reported_by = 'DPDP-ADM'")
        import migrations
        migrations._m14_dpdp_act_machinery(db)


#: Every append-only trigger migration 14 installs. Teardown has to lift them —
#: which is itself the proof they are on. They are put straight back by
#: re-running the migration, which doubles as an idempotency check on it.
_APPEND_ONLY_TRIGGERS = (
    "trg_consent_records_no_update", "trg_consent_records_no_delete",
    "trg_consent_withdrawals_no_update", "trg_consent_withdrawals_no_delete",
    "trg_erasure_decisions_no_update", "trg_erasure_decisions_no_delete",
    "trg_erasure_executions_no_update", "trg_erasure_executions_no_delete",
    "trg_breach_notifications_no_update", "trg_breach_notifications_no_delete",
)


def _cleanup(*patient_ids: str):
    """TEST-ISOLATION LAW: leave the shared session DB exactly as we found it."""
    import migrations
    with db_session() as db:
        for trg in _APPEND_ONLY_TRIGGERS:
            db.execute(f"DROP TRIGGER IF EXISTS {trg}")
        # Children before parents — these tables carry real FKs.
        for pid in patient_ids:
            for table in ("erasure_executions", "erasure_decisions", "erasure_requests",
                          "consent_withdrawals", "consent_records",
                          "sage_observations", "sage_room_memory", "sage_insights",
                          "insight_feedback", "problem_list", "vitals",
                          "patient_allergies", "medications", "clinical_documents",
                          "admissions"):
                db.execute(f"DELETE FROM {table} WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))
        # Restore the triggers. _m14 is idempotent (CREATE ... IF NOT EXISTS),
        # so this is a no-op for everything except the triggers just dropped.
        migrations._m14_dpdp_act_machinery(db)
    with db_session() as db:
        present = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
    missing = sorted(set(_APPEND_ONLY_TRIGGERS) - present)
    assert not missing, f"teardown left append-only triggers off: {missing}"


@pytest.fixture
def pid_a(_fixtures):
    return _fixtures["a"]


@pytest.fixture
def pid_b(_fixtures):
    return _fixtures["b"]


# ════════════════════════════════════════════════════════════════
#  1. Consent records
# ════════════════════════════════════════════════════════════════

def test_consent_record_captures_what_when_how_and_by_whom(pid_a):
    rec = cs.record_consent(
        patient_id=pid_a,
        purpose="ai_assisted_documentation",
        method="written_signature",
        recorded_by="DPDP-DOC",
        recorded_by_role="doctor",
        scope_detail="Ambient note drafting during ward rounds",
        notice_version="v1.0",
        notice_language="hi",
    )
    assert rec["patient_id"] == pid_a
    assert rec["purpose"] == "ai_assisted_documentation"
    assert rec["method"] == "written_signature"
    assert rec["recorded_by"] == "DPDP-DOC"
    assert rec["granted_at"]
    assert rec["effective_status"] == "active"
    assert rec["notice_version"] == "v1.0"
    assert rec["notice_language"] == "hi"


def test_consent_rejects_unknown_purpose_and_method(pid_a):
    with pytest.raises(ValueError):
        cs.record_consent(patient_id=pid_a, purpose="sell_to_anyone",
                          method="written_signature", recorded_by="DPDP-DOC")
    with pytest.raises(ValueError):
        cs.record_consent(patient_id=pid_a, purpose="research_secondary_use",
                          method="telepathy", recorded_by="DPDP-DOC")


def test_consent_row_is_immutable_at_the_db_layer(pid_a):
    rec = cs.record_consent(patient_id=pid_a, purpose="research_secondary_use",
                            method="digital_portal", recorded_by="DPDP-DOC")
    with db_session() as db:
        with pytest.raises(DBIntegrityError):
            db.execute("UPDATE consent_records SET purpose = 'treatment' WHERE id = ?",
                       (rec["id"],))
    with db_session() as db:
        with pytest.raises(DBIntegrityError):
            db.execute("DELETE FROM consent_records WHERE id = ?", (rec["id"],))


def test_withdrawal_is_a_new_record_not_an_overwrite(pid_a):
    rec = cs.record_consent(patient_id=pid_a, purpose="family_sharing",
                            method="verbal_witnessed", recorded_by="DPDP-DOC",
                            scope_detail="Share updates with named next of kin")
    wd = cs.withdraw_consent(consent_id=rec["id"], recorded_by="DPDP-NURSE",
                             method="verbal_witnessed",
                             reason="Patient asked us to stop telling the family")
    assert wd["consent_id"] == rec["id"]
    assert wd["withdrawn_at"]

    # The ORIGINAL is untouched, byte for byte.
    with db_session() as db:
        row = db.execute("SELECT * FROM consent_records WHERE id = ?",
                         (rec["id"],)).fetchone()
    assert row["purpose"] == "family_sharing"
    assert row["method"] == "verbal_witnessed"
    assert row["granted_at"] == rec["granted_at"]
    assert row["recorded_by"] == "DPDP-DOC"

    # But the effective status now reads withdrawn, derived from the new row.
    fresh = cs.get_consent(rec["id"])
    assert fresh["effective_status"] == "withdrawn"
    assert fresh["withdrawal"]["reason"].startswith("Patient asked")
    assert fresh["withdrawal"]["recorded_by"] == "DPDP-NURSE"


def test_withdrawing_twice_is_refused(pid_a):
    rec = cs.record_consent(patient_id=pid_a, purpose="marketing_communications",
                            method="digital_portal", recorded_by="DPDP-DOC")
    cs.withdraw_consent(consent_id=rec["id"], recorded_by="DPDP-DOC",
                        method="digital_portal")
    with pytest.raises(ValueError):
        cs.withdraw_consent(consent_id=rec["id"], recorded_by="DPDP-DOC",
                            method="digital_portal")


def test_has_active_consent_tracks_grant_then_withdrawal(pid_b):
    assert cs.has_active_consent(pid_b, "research_secondary_use") is False
    rec = cs.record_consent(patient_id=pid_b, purpose="research_secondary_use",
                            method="written_signature", recorded_by="DPDP-DOC")
    assert cs.has_active_consent(pid_b, "research_secondary_use") is True
    cs.withdraw_consent(consent_id=rec["id"], recorded_by="DPDP-DOC",
                        method="written_signature")
    assert cs.has_active_consent(pid_b, "research_secondary_use") is False


def test_withdrawal_surfaces_the_erasure_review_it_triggers(pid_b):
    """DPDP §8(7): withdrawal creates an erasure obligation. Orb must SAY so,
    and must not act on it silently."""
    rec = cs.record_consent(patient_id=pid_b, purpose="ai_assisted_documentation",
                            method="digital_portal", recorded_by="DPDP-DOC")
    wd = cs.withdraw_consent(consent_id=rec["id"], recorded_by="DPDP-DOC",
                             method="digital_portal")
    assert wd["erasure_review_required"] is True
    assert wd["categories_for_review"], "must name the data categories to review"
    assert "auto" not in json.dumps(wd).lower() or wd.get("auto_deleted") in (None, False)


# ── Consent routes ──

def test_consent_routes_record_list_and_withdraw(pid_a):
    token = _login("DPDP-DOC")
    r = client.post(
        f"/api/consent/{pid_a}",
        headers=_auth(token),
        json={"purpose": "treatment", "method": "written_signature",
              "scope_detail": "Inpatient care", "notice_version": "v1.0",
              "notice_language": "ta"},
    )
    assert r.status_code == 200, r.text
    consent_id = r.json()["id"]

    r = client.get(f"/api/consent/{pid_a}", headers=_auth(token))
    assert r.status_code == 200, r.text
    ids = [c["id"] for c in r.json()["consents"]]
    assert consent_id in ids

    r = client.post(f"/api/consent/{pid_a}/{consent_id}/withdraw",
                    headers=_auth(token),
                    json={"method": "verbal_witnessed", "reason": "changed mind"})
    assert r.status_code == 200, r.text
    assert r.json()["withdrawal"]["consent_id"] == consent_id


def test_consent_route_is_ward_scoped(pid_a):
    """A clinician outside the patient's department gets 404, never 403."""
    _ensure_user("DPDP-DOC-SURG", "doctor", OTHER_DEPT)
    token = _login("DPDP-DOC-SURG")
    r = client.get(f"/api/consent/{pid_a}", headers=_auth(token))
    assert r.status_code == 404, r.text
    r = client.post(f"/api/consent/{pid_a}", headers=_auth(token),
                    json={"purpose": "treatment", "method": "written_signature"})
    assert r.status_code == 404, r.text


# ════════════════════════════════════════════════════════════════
#  2. Data-principal rights — THE EXPORT
# ════════════════════════════════════════════════════════════════

def test_export_contains_this_patient_and_nobody_else(pid_a, pid_b):
    """The one that matters. Every marker belonging to patient B must be absent
    from patient A's export, and A's own record must actually be in there."""
    export = cs.data_principal_export(pid_a)
    blob = json.dumps(export, default=str)

    # A's own data is present.
    assert pid_a in blob
    for marker in (f"NOTE-BODY-{A_MARK}", f"DRUG-{A_MARK}", f"ALLERGEN-{A_MARK}",
                   f"PROBLEM-{A_MARK}", f"ADDRESS-{A_MARK}", f"MRN-{A_MARK}"):
        assert marker in blob, f"export is missing the principal's own {marker}"

    # Nothing at all about patient B.
    assert pid_b not in blob, "export leaked another patient's identifier"
    assert B_MARK not in blob, "export leaked another patient's data"


def test_export_is_organised_by_section_with_row_counts(pid_a):
    export = cs.data_principal_export(pid_a)
    assert export["patient_id"] == pid_a
    assert export["generated_at"]
    assert isinstance(export["sections"], dict) and export["sections"]
    assert export["record_count"] > 0
    # Sections name their source table so the reader can ask a follow-up.
    for name, section in export["sections"].items():
        assert "rows" in section and isinstance(section["rows"], list)
        assert "row_count" in section
        assert section["row_count"] == len(section["rows"])
    assert "patients" in export["sections"]
    assert "clinical_documents" in export["sections"]


def test_export_does_not_list_consents_twice(pid_b):
    """The schema walk and the curated consent section would otherwise both
    emit consent_records — the same facts once readable and once raw."""
    cs.record_consent(patient_id=pid_b, purpose="treatment",
                      method="written_signature", recorded_by="DPDP-DOC")
    export = cs.data_principal_export(pid_b)
    assert "consents" in export["sections"]
    assert "consent_records" not in export["sections"]
    assert "consent_withdrawals" not in export["sections"]
    # …and the curated section is the readable one.
    row = export["sections"]["consents"]["rows"][0]
    assert "effective_status" in row and "purpose_label" in row


def test_export_names_what_it_deliberately_leaves_out(pid_a):
    """Honesty: an export that silently omits categories is a lie to the data
    principal. It must enumerate its own exclusions and why."""
    export = cs.data_principal_export(pid_a)
    assert export["excluded"], "the export must declare its exclusions"
    for item in export["excluded"]:
        assert item["name"] and item["reason"]


def test_export_discovers_tables_from_the_live_schema(pid_a):
    """A hand-maintained table list goes stale silently. Assert the export walks
    the schema and reaches a broad set of patient-keyed tables."""
    export = cs.data_principal_export(pid_a)
    assert export["tables_scanned"] > 30
    with db_session() as db:
        included, refused, scanned = cs._patient_tables(db)
    # Every included table is keyed on exactly one plain `patient_id`.
    assert "clinical_documents" in included and "medications" in included
    assert "vitals" in included and "patient_allergies" in included
    with db_session() as db:
        for table in included:
            cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
            patient_cols = [c for c in cols
                            if c == "patient_id" or c.endswith("_patient_id")]
            assert patient_cols == ["patient_id"], (
                f"{table} would be exported despite {patient_cols}")


def test_multi_patient_shaped_tables_are_refused_and_declared():
    """The structural guard. A table whose rows can name a SECOND patient must
    be refused — and the refusal declared, never a silent skip.

    message_room_fit_flags is the live example: a misdirected-message flag
    carries `room_patient_id` AND `suspected_patient_id`, so exporting it under
    one patient would hand them the identity of another.
    """
    with db_session() as db:
        included, refused, _ = cs._patient_tables(db)
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "message_room_fit_flags" in tables:
        assert "message_room_fit_flags" not in included
        assert "message_room_fit_flags" in refused
        assert refused["message_room_fit_flags"]
    # Nothing is dropped without a stated reason.
    for name, reason in refused.items():
        assert reason and len(reason) > 40, f"{name} refused with a thin reason"


def _on_a_fresh_connection(fn):
    """Run ``fn`` on a brand-new thread, then retire that thread's pooled handle.

    THE THREAD IS THE POINT HERE, not plumbing. Two facts combine: a loadable
    SQLite extension is PER-CONNECTION state, and db_backend pools ONE
    connection per thread (``ORB_DB_POOL`` has defaulted ON since 2026-08-01).
    So on the main thread, "a db_session that has not loaded sqlite-vec" is not
    something this test can ask for — whoever loads the extension loads it onto
    the same handle every later db_session on that thread hands back. That is
    true of this test's own setup, and also of test_reranker and
    test_retrieval_eval, which both call ``sqlite_vec.load()`` on the main
    thread's pooled connection earlier in a full-suite run.

    An earlier revision created the vec0 table through ``db_session()`` and then
    asserted that the NEXT ``db_session()`` could not read it. That could only
    ever hold with pooling off; with the pooled default it is the same
    connection, so the extension was still loaded, the probe did not raise, and
    the test died on its own precondition — meaning the export assertions below
    had never once executed. Pinning it to the main thread would also have made
    it order-dependent: green alone, red after the corpus tests.

    A fresh thread gets a fresh pooled connection that has provably loaded
    nothing. That is also exactly the production arrangement — the corpus loader
    holds the module, the request worker serving the export does not.
    """
    box: dict = {}

    def _run():
        from database import get_db
        from db_backend import discard_pooled, is_pooled
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            box["error"] = exc
        finally:
            # TEST-ISOLATION LAW, connection edition: this thread is about to
            # die, so close its pooled handle rather than leave it (and its id)
            # in the pool registry for the rest of the session.
            try:
                conn = get_db()
                if is_pooled(conn):
                    discard_pooled(conn)
            except Exception:
                pass

    t = threading.Thread(target=_run, name="dpdp-fresh-conn")
    t.start()
    t.join(60)
    assert not t.is_alive(), "fresh-connection worker did not finish"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def test_export_survives_a_virtual_table_it_cannot_introspect(pid_a):
    """EXECUTED, and it failed before the fix.

    Orb's knowledge corpus creates ``clinical_knowledge_vec`` as a ``vec0``
    virtual table from the sqlite-vec extension. That extension is PER
    CONNECTION state an ordinary db_session does not load, so even
    ``PRAGMA table_info`` on it raises "no such module: vec0" — and the export,
    which walks the whole schema, returned a 500 on every appliance with the
    corpus loaded.

    Running this module ALONE can never catch that: alone, no vec0 table exists.
    It surfaced only when this file ran in the same session as the corpus tests.
    So the test builds the arrangement itself: the vec0 table is created on one
    connection that HAS the extension, and both the hazard probe and the export
    run on another that does NOT — see ``_on_a_fresh_connection`` for why that
    takes a thread. Where sqlite-vec is absent it falls back to fts5, which
    needs no extension and still reproduces the schema SHAPE.
    """
    try:
        import sqlite_vec
    except ImportError:  # pragma: no cover — sqlite-vec is in requirements
        sqlite_vec = None

    if sqlite_vec is not None:
        probe = "dpdp_probe_vec"

        def _create():
            with db_session() as db:
                db.enable_load_extension(True)
                sqlite_vec.load(db)
                db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {probe} "
                           f"USING vec0(embedding float[4])")

        _on_a_fresh_connection(_create)

        # Prove the hazard is real on THIS machine before relying on it: a
        # connection that never loaded the extension cannot read even the
        # table's structure. Returned as a string rather than raised, because
        # pytest.raises does not reach across a thread boundary.
        def _probe():
            with db_session() as db:
                try:
                    db.execute(f"PRAGMA table_info({probe})").fetchall()
                except Exception as exc:
                    return str(exc)
            return None

        err = _on_a_fresh_connection(_probe)
        assert err and "vec0" in err, (
            f"precondition failed: a connection without sqlite-vec was able to "
            f"introspect {probe} (got {err!r}) — the export below would then be "
            f"passing vacuously"
        )
    else:  # pragma: no cover — sqlite-vec is in requirements
        probe = "dpdp_probe_fts"

        def _create_fts():
            with db_session() as db:
                db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {probe} USING fts5(body)")

        _on_a_fresh_connection(_create_fts)

    try:
        # The export must run on a connection WITHOUT the module too — that is
        # the request worker in production, and running it on a handle that
        # happened to load sqlite-vec would exercise nothing.
        export = _on_a_fresh_connection(lambda: cs.data_principal_export(pid_a))
        assert export["record_count"] > 0
        assert probe not in export["sections"]
        excluded = {e["name"]: e["reason"] for e in export["excluded"]}
        assert probe in excluded, (
            "a skipped table must be DECLARED — a silent skip is how an export "
            "becomes incomplete without anyone noticing"
        )
        assert excluded[probe]
    finally:
        def _drop():
            with db_session() as db:
                if sqlite_vec is not None:
                    db.enable_load_extension(True)
                    sqlite_vec.load(db)   # required to DROP a vec0 table
                db.execute(f"DROP TABLE IF EXISTS {probe}")

        _on_a_fresh_connection(_drop)


class _StubCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _UnreadableTableDB:
    """A db handle where one table's structure cannot be read.

    Stands in for the shape the ``CREATE VIRTUAL TABLE`` sniff CANNOT catch: a
    shadow table of an extension, or a table whose module was removed after the
    schema was written. Both leave a row in sqlite_master whose DDL looks
    ordinary and whose ``PRAGMA table_info`` still raises.
    """

    def __init__(self, master_rows, unreadable, error):
        self._master = master_rows
        self._unreadable = unreadable
        self._error = error

    def execute(self, sql, params=()):
        if "sqlite_master" in sql:
            return _StubCursor(self._master)
        if sql.startswith("PRAGMA table_info("):
            name = sql[len("PRAGMA table_info("):].rstrip(")")
            if name in self._unreadable:
                raise self._error
            # (cid, name, type, notnull, dflt, pk) — only [1] is read.
            return _StubCursor([(0, "id", "TEXT", 0, None, 1),
                                (1, "patient_id", "TEXT", 0, None, 0)])
        raise AssertionError(f"unexpected query in _patient_tables: {sql}")


def test_export_declares_an_unreadable_table_instead_of_dying():
    """The belt-and-braces branch, which nothing exercised until now.

    The virtual-table sniff is the FIRST defence and it is a string match on the
    DDL. This is the second one: a table that introspects with an error but does
    not announce itself as virtual. The requirement is not that the export
    guesses — it is that one unreadable table cannot take down a data
    principal's whole export, and that its omission is DECLARED with the error
    quoted, never a silent skip.

    Driven through a stub rather than a real broken table on purpose: the shared
    session DB is the one thing this suite must not corrupt, and forging a
    sqlite_master row to make a table unreadable would risk exactly that.
    """
    # DBOperationalError is an except-able TUPLE of every installed driver's
    # class (see db_backend._alias), so it cannot be instantiated directly —
    # [0] is the driver actually in use.
    err = DBOperationalError[0]("no such module: zzz_departed")
    db = _UnreadableTableDB(
        master_rows=[
            ("dpdp_readable", "CREATE TABLE dpdp_readable (id TEXT, patient_id TEXT)"),
            # DDL reads as an ordinary table, so the virtual-table sniff misses it.
            ("dpdp_orphaned", "CREATE TABLE dpdp_orphaned (id TEXT, patient_id TEXT)"),
        ],
        unreadable={"dpdp_orphaned"},
        error=err,
    )

    included, refused, scanned = cs._patient_tables(db)   # must NOT raise

    assert scanned == 2
    assert "dpdp_readable" in included, (
        "one unreadable table must not cost the principal the readable ones")
    assert "dpdp_orphaned" not in included
    assert "dpdp_orphaned" in refused, (
        "an unreadable table must be DECLARED, not silently dropped")
    assert "zzz_departed" in refused["dpdp_orphaned"], (
        "the refusal must quote the actual error — 'left out for some reason' "
        "is not something a records officer can act on")


def test_export_endpoint_is_admin_only_and_audited(pid_a):
    doc = _login("DPDP-DOC")
    r = client.get(f"/api/admin/compliance/data-principal/{pid_a}/export",
                   headers=_auth(doc))
    assert r.status_code == 403, "a doctor must not hold the rights-export key"

    adm = _login("DPDP-ADM")
    r = client.get(f"/api/admin/compliance/data-principal/{pid_a}/export",
                   headers=_auth(adm))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient_id"] == pid_a

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'data_principal_export' "
            "AND patient_id = ? ORDER BY id DESC LIMIT 1", (pid_a,)
        ).fetchone()
    assert row is not None, "the export is a PHI disclosure and MUST be audited"
    assert row["event_category"] == "phi_access"
    assert row["actor_user_id"] == "DPDP-ADM"
    meta = json.loads(row["metadata_json"] or "{}")
    assert meta.get("record_count", 0) > 0
    assert meta.get("sections")


def test_export_endpoint_respects_department_scope(pid_a):
    """An admin scoped to another department gets 404, not a whole-record dump."""
    token = _login("DPDP-ADM-SURG")
    r = client.get(f"/api/admin/compliance/data-principal/{pid_a}/export",
                   headers=_auth(token))
    assert r.status_code == 404, r.text


# ════════════════════════════════════════════════════════════════
#  3. Retention
# ════════════════════════════════════════════════════════════════

def test_retention_policies_are_seeded_with_basis_and_authority():
    policies = cs.list_retention_policies()
    assert policies
    by_cat = {p["category"]: p for p in policies}
    for required in ("clinical_record", "audit_trail", "ai_interaction_transcript"):
        assert required in by_cat, f"missing retention policy: {required}"
    for p in policies:
        assert p["retention_days"] is None or p["retention_days"] > 0
        assert p["legal_basis"], "every policy states its lawful basis"
        assert p["authority"], "every policy cites the rule it comes from"
        assert p["erasable"] in (0, 1, True, False)
    # The clinical record is retained under a legal obligation, not consent.
    assert by_cat["clinical_record"]["legal_basis"] == "legal_obligation"
    assert not by_cat["clinical_record"]["erasable"]
    assert by_cat["ai_interaction_transcript"]["erasable"]


def test_retention_review_surfaces_overdue_without_deleting(pid_a):
    """Requirement 3: SEE what is past retention. Orb must not delete it."""
    with db_session() as db:
        before = db.execute(
            "SELECT COUNT(*) c FROM clinical_documents WHERE patient_id = ?",
            (pid_a,)).fetchone()["c"]
        # Backdate this patient's note well past every plausible retention window.
        db.execute("UPDATE clinical_documents SET created_at = '2001-01-01 00:00:00' "
                   "WHERE patient_id = ?", (pid_a,))

    review = cs.retention_review()
    assert review["deleted_anything"] is False
    cats = {c["category"]: c for c in review["categories"]}
    clinical = cats["clinical_record"]
    assert clinical["overdue_count"] >= 1
    assert clinical["cutoff_date"]
    assert clinical["disposition"], "a human needs to be told what to DO"
    # Nothing was destroyed.
    with db_session() as db:
        after = db.execute(
            "SELECT COUNT(*) c FROM clinical_documents WHERE patient_id = ?",
            (pid_a,)).fetchone()["c"]
    assert after == before


def test_retention_handles_both_timestamp_formats_this_schema_writes():
    """This schema stores timestamps two ways — ISO-8601 with a 'T' from Python,
    and 'YYYY-MM-DD HH:MM:SS' from SQLite's CURRENT_TIMESTAMP — and 'T' sorts
    ABOVE ' '. Comparing against a full ISO cutoff would therefore count a
    space-format row stamped ON the cutoff date as overdue, and quietly put a
    live medical record on a destruction list. Four rows, two formats, both
    sides of the boundary.
    """
    pid = _seed_patient("TSFMT-PRINCIPAL", DEPT)
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1095)).date()
        before = (cutoff - timedelta(days=1)).isoformat()
        on_cutoff = cutoff.isoformat()
        with db_session() as db:
            db.execute("DELETE FROM clinical_documents WHERE patient_id = ?", (pid,))
            for stamp in (f"{before}T23:59:59+00:00",   # overdue, ISO
                          f"{before} 23:59:59",          # overdue, CURRENT_TIMESTAMP
                          f"{on_cutoff}T00:00:00+00:00", # NOT overdue, ISO
                          f"{on_cutoff} 00:00:00"):      # NOT overdue, CURRENT_TIMESTAMP
                db.execute(
                    "INSERT INTO clinical_documents (patient_id, doc_type, title, "
                    "content, author_id, created_at) "
                    "VALUES (?, 'Progress Note', 'ts', 'ts', 'DPDP-DOC', ?)",
                    (pid, stamp),
                )

        review = cs.retention_review(patient_id=pid)
        clinical = {c["category"]: c for c in review["categories"]}["clinical_record"]
        # The count first: it names the actual harm if this regresses.
        assert clinical["by_table"]["clinical_documents"] == 2, (
            "exactly the two rows dated before the cutoff are overdue — one in "
            "each timestamp format, and neither on-cutoff row. A count of 3 means "
            "a live record was put on a destruction list."
        )
        assert clinical["cutoff_date"] == on_cutoff, (
            "the cutoff must be a bare date; a full timestamp reintroduces the "
            "'T' vs ' ' ordering bug"
        )
    finally:
        _cleanup(pid)


def test_per_patient_retention_review_does_not_count_other_peoples_rows(pid_a):
    """The breach register is keyed on incidents, not patients. Counting it
    unfiltered inside a per-patient view would report every breach in the
    hospital as this one person's overdue data."""
    cs.record_breach(nature="loss", description="retention scoping probe",
                     reported_by="DPDP-ADM", discovered_at="2001-01-01T00:00:00+00:00")
    scoped = {c["category"]: c
              for c in cs.retention_review(patient_id=pid_a)["categories"]}
    breach = scoped["breach_record"]
    assert breach["overdue_count"] == 0
    assert "breach_records" in breach.get("not_patient_scoped", []), (
        "a table with no patient column must be skipped AND declared"
    )
    # Hospital-wide, the same row IS counted.
    unscoped = {c["category"]: c for c in cs.retention_review()["categories"]}
    assert unscoped["breach_record"]["overdue_count"] >= 1


def test_migration_14_is_idempotent():
    """Migrations must be safe to re-run — the ledger prevents it in normal
    operation, but an ops/repair script or a reset ledger will not. Re-running
    must not duplicate a seeded policy or fail on an existing trigger."""
    import migrations
    before = cs.list_retention_policies()
    with db_session() as db:
        migrations._m14_dpdp_act_machinery(db)
        migrations._m14_dpdp_act_machinery(db)
    after = cs.list_retention_policies()
    assert len(after) == len(before)
    assert [p["category"] for p in after] == [p["category"] for p in before]


def test_retention_endpoint(pid_a):
    token = _login("DPDP-ADM")
    r = client.get("/api/admin/compliance/retention", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policies"] and body["review"]["categories"]


# ════════════════════════════════════════════════════════════════
#  4. THE HARD PART — erasure vs the audit trail vs medical-record law
# ════════════════════════════════════════════════════════════════

def test_erasure_is_refused_for_the_clinical_record_with_a_stated_reason(pid_a):
    req = cs.record_erasure_request(patient_id=pid_a, requested_by="DPDP-ADM",
                                    categories=None)  # None = everything
    decision = cs.adjudicate_erasure_request(req["id"], decided_by="DPDP-ADM")
    by_cat = {d["category"]: d for d in decision["decisions"]}

    clinical = by_cat["clinical_record"]
    assert clinical["disposition"] == "refused"
    assert clinical["reason"], "a refusal without a reason is not a decision"
    assert clinical["authority"], "the refusal must cite what compels retention"
    assert "legal_obligation" in clinical["basis"]

    audit = by_cat["audit_trail"]
    assert audit["disposition"] == "refused"
    assert "chain" in audit["reason"].lower() or "tamper" in audit["reason"].lower()


def test_the_encoded_distinction_holds_across_the_whole_matrix(pid_b):
    """The position Orb takes, asserted as an invariant rather than trusted.

    THE LAWFUL BASIS DECIDES: anything held under `legal_obligation` is out of
    reach of erasure and must cite the rule that compels it; anything held under
    `consent` is in reach. If a future edit ever makes a clinical record erasable
    — or quietly refuses a marketing list — this fails.
    """
    for p in cs.list_retention_policies():
        basis, cat = p["legal_basis"], p["category"]
        assert basis in ("legal_obligation", "consent"), f"{cat}: {basis}"
        if basis == "legal_obligation":
            assert not p["erasable"], f"{cat} is compelled by law but marked erasable"
            assert p["authority"], f"{cat} refuses erasure without citing a rule"
        else:
            assert p["erasable"], f"{cat} is held on consent but refuses erasure"

    # And the adjudicator agrees with the matrix, category for category.
    req = cs.record_erasure_request(patient_id=pid_b, requested_by="DPDP-ADM")
    decision = cs.adjudicate_erasure_request(req["id"], decided_by="DPDP-ADM")
    policies = {p["category"]: p for p in cs.list_retention_policies()}
    for d in decision["decisions"]:
        expected = "honoured" if policies[d["category"]]["erasable"] else "refused"
        assert d["disposition"] == expected, d["category"]
        if expected == "refused":
            assert d["authority"], f"{d['category']} refused with no authority"
        assert d["reason"]


def test_erasure_is_honoured_for_non_clinical_personal_data(pid_a):
    req = cs.record_erasure_request(patient_id=pid_a, requested_by="DPDP-ADM",
                                    categories=["ai_interaction_transcript"])
    decision = cs.adjudicate_erasure_request(req["id"], decided_by="DPDP-ADM")
    by_cat = {d["category"]: d for d in decision["decisions"]}
    ai = by_cat["ai_interaction_transcript"]
    assert ai["disposition"] == "honoured"
    assert ai["executable"] is True

    with db_session() as db:
        before = db.execute(
            "SELECT COUNT(*) c FROM sage_observations WHERE patient_id = ?",
            (pid_a,)).fetchone()["c"]
    assert before >= 1, "fixture should have seeded an AI observation"

    result = cs.execute_erasure_request(req["id"], executed_by="DPDP-ADM")
    assert result["status"] == "executed"
    assert result["rows_erased"] >= 1

    with db_session() as db:
        after = db.execute(
            "SELECT COUNT(*) c FROM sage_observations WHERE patient_id = ?",
            (pid_a,)).fetchone()["c"]
        clinical_left = db.execute(
            "SELECT COUNT(*) c FROM clinical_documents WHERE patient_id = ?",
            (pid_a,)).fetchone()["c"]
    assert after == 0, "honoured erasure must actually erase"
    assert clinical_left >= 1, "the medical record must survive an erasure request"


def test_erasure_of_a_portal_account_kills_the_login_and_keeps_the_record():
    """The second executable category, exercised for real.

    Written after review found `_erase_portal_account` set `password_hash = NULL`
    against a NOT NULL column — a crash that only fires when this exact path
    runs, and that no other test in this file could have expressed.
    """
    portal_pid = _seed_patient("PORTAL-PRINCIPAL", DEPT)
    portal_uid = f"DPDPPAT{uuid.uuid4().hex[:6].upper()}"
    try:
        with db_session() as db:
            db.execute(
                "INSERT INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
                (portal_uid, "Portal Principal", "patient", hash_password("password")),
            )
            db.execute("UPDATE patients SET user_id = ? WHERE id = ?",
                       (portal_uid, portal_pid))
            db.execute(
                "INSERT INTO discharge_access_tokens (token_id, token_hash, patient_id, "
                "expires_at, created_by_user_id) VALUES (?,?,?,?,'DPDP-DOC')",
                (f"TOK-{uuid.uuid4().hex[:8]}", "x" * 64, portal_pid, "2099-01-01"),
            )

        # The login works before the erasure.
        assert client.post("/api/auth/login",
                           json={"employee_id": portal_uid,
                                 "password": "password"}).status_code == 200

        req = cs.record_erasure_request(patient_id=portal_pid, requested_by="DPDP-ADM",
                                        categories=["patient_portal_account"])
        decision = cs.adjudicate_erasure_request(req["id"], decided_by="DPDP-ADM")
        assert decision["decisions"][0]["disposition"] == "honoured"
        result = cs.execute_erasure_request(req["id"], executed_by="DPDP-ADM")
        assert result["rows_erased"] >= 2

        # The login is dead, the credential is unusable, the token is revoked.
        assert client.post("/api/auth/login",
                           json={"employee_id": portal_uid,
                                 "password": "password"}).status_code != 200
        with db_session() as db:
            u = db.execute("SELECT * FROM users WHERE id = ?", (portal_uid,)).fetchone()
            tok = db.execute(
                "SELECT revoked_at FROM discharge_access_tokens WHERE patient_id = ?",
                (portal_pid,)).fetchone()
            doc = db.execute(
                "SELECT COUNT(*) c FROM clinical_documents WHERE patient_id = ?",
                (portal_pid,)).fetchone()["c"]
        assert u["password_hash"] and not u["password_hash"].startswith("$2")
        assert u["is_active"] == 0
        assert tok["revoked_at"], "the discharge access token must be revoked"
        # And the medical record the login opened is untouched.
        assert doc >= 1
    finally:
        # Children before parents: the token FKs patients, patients FKs users.
        with db_session() as db:
            db.execute("DELETE FROM discharge_access_tokens WHERE patient_id = ?",
                       (portal_pid,))
        _cleanup(portal_pid)
        with db_session() as db:
            db.execute("DELETE FROM users WHERE id = ?", (portal_uid,))


def test_erasure_decisions_are_append_only(pid_b):
    req = cs.record_erasure_request(patient_id=pid_b, requested_by="DPDP-ADM",
                                    categories=["ai_interaction_transcript"])
    cs.adjudicate_erasure_request(req["id"], decided_by="DPDP-ADM")
    with db_session() as db:
        row = db.execute("SELECT id FROM erasure_decisions WHERE request_id = ? LIMIT 1",
                         (req["id"],)).fetchone()
        with pytest.raises(DBIntegrityError):
            db.execute("UPDATE erasure_decisions SET disposition = 'honoured' WHERE id = ?",
                       (row["id"],))


def test_execute_before_adjudicate_is_refused(pid_b):
    req = cs.record_erasure_request(patient_id=pid_b, requested_by="DPDP-ADM",
                                    categories=["ai_interaction_transcript"])
    with pytest.raises(ValueError):
        cs.execute_erasure_request(req["id"], executed_by="DPDP-ADM")


def test_erasure_endpoints(pid_b):
    token = _login("DPDP-ADM")
    r = client.post("/api/admin/compliance/erasure", headers=_auth(token),
                    json={"patient_id": pid_b, "categories": ["ai_interaction_transcript"],
                          "requester_identity": "patient", "channel": "in_person"})
    assert r.status_code == 200, r.text
    req_id = r.json()["id"]

    r = client.post(f"/api/admin/compliance/erasure/{req_id}/adjudicate",
                    headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["decisions"]

    r = client.get(f"/api/admin/compliance/erasure/{req_id}", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["request"]["patient_id"] == pid_b

    r = client.get("/api/admin/compliance/erasure", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert any(x["id"] == req_id for x in r.json()["requests"])


def test_erasure_execution_audits_intent_before_it_destroys_anything():
    """Erasure is irreversible, so the audit row that says it is about to happen
    is written BEFORE any row is deleted — on the fail-closed phi_access
    category, so an unavailable audit log aborts the request instead of
    destroying data nobody can prove was destroyed."""
    pid = _seed_patient("AUDITORDER-PRINCIPAL", DEPT)
    try:
        token = _login("DPDP-ADM")
        r = client.post("/api/admin/compliance/erasure", headers=_auth(token),
                        json={"patient_id": pid,
                              "categories": ["ai_interaction_transcript"]})
        req_id = r.json()["id"]
        client.post(f"/api/admin/compliance/erasure/{req_id}/adjudicate",
                    headers=_auth(token))
        r = client.post(f"/api/admin/compliance/erasure/{req_id}/execute",
                        headers=_auth(token))
        assert r.status_code == 200, r.text

        with db_session() as db:
            rows = db.execute(
                "SELECT id, action_type FROM audit_logs WHERE patient_id = ? "
                "AND action_type IN ('erasure_execution_started','erasure_executed') "
                "ORDER BY id", (pid,)).fetchall()
        seq = [row["action_type"] for row in rows]
        assert seq == ["erasure_execution_started", "erasure_executed"], seq
    finally:
        _cleanup(pid)


def test_erasure_request_is_ward_scoped(pid_a):
    token = _login("DPDP-ADM-SURG")
    r = client.post("/api/admin/compliance/erasure", headers=_auth(token),
                    json={"patient_id": pid_a})
    assert r.status_code == 404, r.text


def test_erasure_listing_is_ward_scoped(pid_a):
    """The by-id lookups were scoped from the start; the LISTING was not, and an
    unfiltered register hands a scoped administrator the id of every patient in
    the hospital who ever asked to be forgotten."""
    cs.record_erasure_request(patient_id=pid_a, requested_by="DPDP-ADM",
                              categories=["ai_interaction_transcript"])

    unscoped = _login("DPDP-ADM")
    rows = client.get("/api/admin/compliance/erasure",
                      headers=_auth(unscoped)).json()["requests"]
    assert any(r["patient_id"] == pid_a for r in rows), (
        "the hospital-wide governance account must still see everything"
    )

    scoped = _login("DPDP-ADM-SURG")          # a different department
    rows = client.get("/api/admin/compliance/erasure",
                      headers=_auth(scoped)).json()["requests"]
    assert not any(r["patient_id"] == pid_a for r in rows), (
        "erasure listing leaked a patient outside the caller's department"
    )


# ════════════════════════════════════════════════════════════════
#  5. Breach notification records
# ════════════════════════════════════════════════════════════════

def test_breach_record_captures_what_to_whom_and_when():
    rec = cs.record_breach(
        nature="unauthorised_access",
        description="A ward tablet left unlocked in a corridor was used by a visitor",
        discovered_at="2026-08-05T09:15:00+00:00",
        occurred_at="2026-08-05T08:40:00+00:00",
        reported_by="DPDP-ADM",
        exposed_to="One unidentified member of the public",
        data_categories=["clinical_record"],
        records_affected=3,
        principals_affected=3,
    )
    assert rec["id"]
    assert rec["nature"] == "unauthorised_access"
    assert rec["exposed_to"]
    assert rec["discovered_at"] == "2026-08-05T09:15:00+00:00"
    assert rec["status"] == "open"
    assert rec["board_notified_at"] is None

    notif = cs.record_breach_notification(
        breach_id=rec["id"], party_type="board", notified_party="Data Protection Board of India",
        method="portal", reference="DPB/2026/0001", recorded_by="DPDP-ADM",
    )
    assert notif["breach_id"] == rec["id"]
    assert notif["notified_at"]

    cs.record_breach_notification(
        breach_id=rec["id"], party_type="data_principal", notified_party="3 affected patients",
        method="letter", reference="WARD-A-LETTERS", recorded_by="DPDP-ADM",
    )
    full = cs.get_breach(rec["id"])
    assert len(full["notifications"]) == 2
    assert full["board_notified_at"], "notifying the Board updates the breach header"
    assert full["principals_notified_at"]


def test_breach_endpoints():
    token = _login("DPDP-ADM")
    r = client.post("/api/admin/compliance/breaches", headers=_auth(token),
                    json={"nature": "loss", "description": "USB stick with an export lost",
                          "exposed_to": "unknown", "records_affected": 1,
                          "principals_affected": 1})
    assert r.status_code == 200, r.text
    bid = r.json()["id"]

    r = client.post(f"/api/admin/compliance/breaches/{bid}/notifications",
                    headers=_auth(token),
                    json={"party_type": "board", "notified_party": "DPB India",
                          "method": "portal", "reference": "DPB/2026/0002"})
    assert r.status_code == 200, r.text

    r = client.get("/api/admin/compliance/breaches", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert any(b["id"] == bid for b in r.json()["breaches"])

    r = client.get(f"/api/admin/compliance/breaches/{bid}", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["notifications"]


def test_breach_detail_withholds_out_of_ward_patient_ids(pid_a):
    """A breach is a hospital-wide governance record and stays visible. Its list
    of AFFECTED PATIENTS is PHI, and an administrator scoped to another ward has
    no business reading those ids out of an incident report — but must still be
    told the incident is wider than what they can see."""
    rec = cs.record_breach(
        nature="unauthorised_disclosure",
        description="Ward list printed and left on a bus",
        reported_by="DPDP-ADM",
        affected_patient_ids=[pid_a],
        principals_affected=1,
    )
    token = _login("DPDP-ADM")               # unscoped governance account
    body = client.get(f"/api/admin/compliance/breaches/{rec['id']}",
                      headers=_auth(token)).json()
    assert body["affected_patient_ids"] == [pid_a]
    assert body["affected_patient_ids_withheld"] == 0

    token = _login("DPDP-ADM-SURG")          # scoped to a different department
    body = client.get(f"/api/admin/compliance/breaches/{rec['id']}",
                      headers=_auth(token)).json()
    assert body["affected_patient_ids"] == []
    assert body["affected_patient_ids_withheld"] == 1, (
        "the count must still be stated, or a scoped reader under-reports the "
        "breach to the Board"
    )
    # The incident itself is not hidden — only the identities.
    assert body["description"] and body["nature"] == "unauthorised_disclosure"


def test_breach_endpoints_are_admin_only():
    token = _login("DPDP-DOC")
    r = client.get("/api/admin/compliance/breaches", headers=_auth(token))
    assert r.status_code == 403


# ════════════════════════════════════════════════════════════════
#  6. Compliance report picks up the new controls
# ════════════════════════════════════════════════════════════════

def test_compliance_report_gains_dpdp_controls():
    from services.compliance_service import compliance_report
    ids = {c["id"] for c in compliance_report()["controls"]}
    assert {"dpdp_consent", "dpdp_data_principal_rights", "dpdp_retention",
            "dpdp_breach_register"} <= ids


# ════════════════════════════════════════════════════════════════
#  The minority protection (Limitation Act 1963 s.6)
# ════════════════════════════════════════════════════════════════
# A minor's medical record outlives the ordinary three-year clinical clock,
# because limitation does not start running until the child turns 18. The bug
# these tests exist to prevent is the quiet one: a retention review that hands a
# hospital a destruction list with a child's record on it. Nothing crashes, the
# count just looks normal, and the record most likely to be demanded in a suit
# is the one destroyed.

def _backdated_note(pid: str, mark: str = "MINOR-RETENTION") -> None:
    """One clinical document, stamped well past every retention window."""
    with db_session() as db:
        db.execute("DELETE FROM clinical_documents WHERE patient_id = ?", (pid,))
        db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, "
            "author_id, created_at) VALUES (?, 'Progress Note', ?, ?, 'DPDP-DOC', "
            "'2001-01-01 00:00:00')",
            (pid, mark, mark),
        )


def _set_age(pid: str, *, age, dob=None) -> None:
    with db_session() as db:
        db.execute("UPDATE patients SET age = ?, date_of_birth = ? WHERE id = ?",
                   (age, dob, pid))


def _clinical(pid: str) -> dict:
    review = cs.retention_review(patient_id=pid)
    return {c["category"]: c for c in review["categories"]}["clinical_record"]


def test_minor_record_is_held_back_not_listed_as_overdue():
    """THE BUG THIS FILE EXISTS FOR. A 9-year-old's note, 25 years overdue on the
    flat clinical clock, must not appear in overdue_count."""
    pid = _seed_patient("MINOR-NINE", DEPT)
    try:
        _set_age(pid, age=9)
        _backdated_note(pid)
        clinical = _clinical(pid)
        assert clinical["overdue_count"] == 0, (
            "a paediatric record was put on a destruction list; under Limitation "
            "Act 1963 s.6 this patient's claim window does not open until 18 and "
            "closes at 21"
        )
        mp = clinical["minority_protection"]
        assert mp["held_minor"] == 1
        assert mp["held_age_unknown"] == 0
        assert mp["authority"] == "Limitation Act 1963 s.6"
        assert "HELD BACK" in clinical["note"]
    finally:
        _cleanup(pid)


def test_adult_record_is_still_overdue():
    """The protection must not swallow the ordinary case: an adult's record past
    three years is exactly what the review is for."""
    pid = _seed_patient("MINOR-ADULT", DEPT)
    try:
        _set_age(pid, age=54)
        _backdated_note(pid)
        clinical = _clinical(pid)
        assert clinical["overdue_count"] == 1
        assert clinical["minority_protection"]["held_minor"] == 0
    finally:
        _cleanup(pid)


def test_patient_of_unknown_age_is_held_back_and_said():
    """Neither DOB nor age. The safe answer is to hold the record AND report it,
    not to authorise destroying a record on the strength of not knowing whose
    it is."""
    pid = _seed_patient("MINOR-UNKNOWN", DEPT)
    try:
        _set_age(pid, age=None)
        _backdated_note(pid)
        clinical = _clinical(pid)
        assert clinical["overdue_count"] == 0
        mp = clinical["minority_protection"]
        assert mp["held_age_unknown"] == 1
        assert mp["held_minor"] == 0
        assert "cannot establish" in clinical["note"]
    finally:
        _cleanup(pid)


def test_date_of_birth_wins_over_a_contradicting_age():
    """DOB is authoritative when present. A row whose age column says 40 and
    whose DOB says the patient is 10 is bad data, and the safe reading of bad
    data on a destruction list is the one that protects the record."""
    pid = _seed_patient("MINOR-CONFLICT", DEPT)
    try:
        child_dob = (datetime.now(timezone.utc) - timedelta(days=365 * 10)).date().isoformat()
        _set_age(pid, age=40, dob=child_dob)
        _backdated_note(pid)
        clinical = _clinical(pid)
        assert clinical["overdue_count"] == 0
        assert clinical["minority_protection"]["held_minor"] == 1
    finally:
        _cleanup(pid)


def test_patient_exactly_at_the_protected_age_is_releasable():
    """The boundary. At 21 the s.6 window has closed and the record joins the
    ordinary clinical clock. Off-by-one here holds records forever."""
    pid = _seed_patient("MINOR-BOUNDARY", DEPT)
    try:
        _set_age(pid, age=21)
        _backdated_note(pid)
        assert _clinical(pid)["overdue_count"] == 1
        _set_age(pid, age=20)
        assert _clinical(pid)["overdue_count"] == 0, "20 is still protected"
    finally:
        _cleanup(pid)


def test_minority_protection_does_not_reach_consent_basis_categories():
    """A child has the same erasure rights as anyone over data held on consent.
    The protection covers evidence in a limitation suit, not Sage's working
    memory."""
    pid = _seed_patient("MINOR-CONSENT", DEPT)
    try:
        _set_age(pid, age=9)
        review = cs.retention_review(patient_id=pid)
        cats = {c["category"]: c for c in review["categories"]}
        assert "minority_protection" not in cats["ai_interaction_transcript"]
        assert "minority_protection" in cats["clinical_record"]
        assert "minority_protection" in cats["prescription_and_dispensing"]
        assert "minority_protection" in cats["imaging"]
    finally:
        _cleanup(pid)


def test_retention_review_still_deletes_nothing_for_a_minor():
    """The whole module's standing invariant, re-asserted on the new path."""
    pid = _seed_patient("MINOR-NODELETE", DEPT)
    try:
        _set_age(pid, age=9)
        _backdated_note(pid)
        review = cs.retention_review(patient_id=pid)
        assert review["deleted_anything"] is False
        with db_session() as db:
            n = db.execute("SELECT COUNT(*) c FROM clinical_documents WHERE "
                           "patient_id = ?", (pid,)).fetchone()["c"]
        assert n == 1
    finally:
        _cleanup(pid)


def test_policy_authority_states_the_minority_rule():
    """A rule enforced in code that the retention register does not state is a
    rule the hospital's counsel cannot audit. Migration 34 puts it in writing."""
    by_cat = {p["category"]: p for p in cs.list_retention_policies()}
    for cat in ("clinical_record", "prescription_and_dispensing", "imaging"):
        assert "Limitation Act 1963 s.6" in by_cat[cat]["authority"], (
            f"{cat} does not state the minority protection it is subject to"
        )
