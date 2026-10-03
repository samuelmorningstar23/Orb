"""Sage never names a drug from a surveillance rule, and a blocked order says so.

Ruled 2026-10-02 (items 1 and 3 of the sepsis-insight review):

1. The proactive rules attached "IV ceftriaxone 2g" to every critical and every
   SIRS-positive patient, chosen by nothing: not the allergy list, renal
   function, the source or local resistance. The frontend hid the button, but
   POST /api/sage/insights/{id}/act still drafted the order for any caller, and
   a doctor confirming it charted ceftriaxone ACTIVE for a patient with a
   documented penicillin anaphylaxis. The rules no longer suggest a drug, and
   the server refuses to draft one from an insight stored before the change.

2. When the drug-safety screen stopped an agentic order, the handoff was still
   stored as 'accepted' (downstream_ref_id 'blocked-allergy') and the Relay
   sidebar read "Sent to Helix", so a doctor could believe a septic patient's
   antibiotic had gone out when nothing had. A stopped order is now 'blocked',
   carries the reason, and the screen's alerts reach the doctor who confirmed,
   including the ones that do not block.
"""
import importlib
import uuid

import pytest
from fastapi.testclient import TestClient

import migrations
from database import db_session
from main import app
from services.clinical_records_service import add_allergy

client = TestClient(app)
DOCTOR = "D-HBLOCK"
DEPT = "Internal Medicine"


def _make_doctor():
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, 'Dr Blocked Test', 'doctor', ?, ?, 1)",
            (DOCTOR, hash_password("testpass123"), DEPT),
        )


def _headers():
    r = client.post("/api/auth/login", json={"employee_id": DOCTOR, "password": "testpass123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def ward():
    """An admitted patient with a case room the test doctor can reach."""
    from services.helix_db import init_helix_db
    init_helix_db()
    _make_doctor()
    pid = f"PAT-HB-{uuid.uuid4().hex[:8]}"
    room = f"case-{pid}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, diagnosis, status, ward, bed, attending_doctor_id) "
            "VALUES (?, 'Blocked Order', ?, 'Pneumonia', 'admitted', 'W1', '1', ?)",
            (pid, f"MRN-{pid}", DOCTOR),
        )
        db.execute("INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                   (room, f"{pid} room", pid))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, ?, ?, 'active')", (f"ADM-{pid}", pid, DOCTOR, DEPT))
    yield {"pid": pid, "room": room}
    with db_session() as db:
        db.execute("DELETE FROM sage_handoff_events WHERE handoff_id IN "
                   "(SELECT id FROM sage_handoffs WHERE patient_id = ?)", (pid,))
        db.execute("DELETE FROM sage_handoffs WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM sage_action_events WHERE action_id IN "
                   "(SELECT id FROM sage_actions WHERE patient_id = ?)", (pid,))
        db.execute("DELETE FROM sage_actions WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM sage_insights WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM channels WHERE id = ?", (room,))
        db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))


def _charted(pid, drug):
    with db_session() as db:
        return db.execute(
            "SELECT count(*) FROM medications WHERE patient_id = ? AND drug_name = ?", (pid, drug),
        ).fetchone()[0]


def _card(ward, target, action_type="start_medication"):
    aid = str(uuid.uuid4())
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale,
                                         confidence, state, version)
               VALUES (?, ?, ?, ?, ?, 'start iv antibiotics', 0.9, 'suggested', 1)""",
            (aid, ward["room"], ward["pid"], action_type, target),
        )
    return aid


def _confirm(ward, aid):
    r = client.post(f"/api/relay/channels/{ward['room']}/sage_actions/{aid}/transition",
                    json={"state": "confirmed", "version": 1}, headers=_headers())
    assert r.status_code == 200, r.text
    return r.json()["handoff"]


def _listed_handoff(ward, aid):
    r = client.get(f"/api/relay/channels/{ward['room']}/sage_actions", headers=_headers())
    assert r.status_code == 200, r.text
    return next(a for a in r.json() if a["id"] == aid)["handoff"]


def _insight(ward, action_type=None, action_target=None):
    iid = str(uuid.uuid4())
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_insights (id, patient_id, insight_type, severity, title, detail,
                   suggested_action_type, suggested_action_target, target_user_id, dedup_key,
                   state, created_at, updated_at)
               VALUES (?, ?, 'sepsis', 'warning', 'possible sepsis', 'd', ?, ?, ?, ?, 'active',
                       datetime('now'), datetime('now'))""",
            (iid, ward["pid"], action_type, action_target, DOCTOR, f"{ward['pid']}:{iid}"),
        )
    return iid


# ═════════════════════════════════════════════════════════════════════════════
#  1. Surveillance never drafts a drug
# ═════════════════════════════════════════════════════════════════════════════
def test_no_surveillance_rule_suggests_a_drug(ward):
    """Rule 1 (critical) and Rule 3 (SIRS) both used to attach IV ceftriaxone."""
    from services.sage_proactive_service import evaluate_patient
    pid = ward["pid"]
    with db_session() as db:
        db.execute("DELETE FROM insight_feedback WHERE pattern IN ('sepsis:warning', 'deterioration:critical')")
        db.execute("INSERT INTO risk_scores (patient_id, score, level, drivers) VALUES (?, 9, 'critical', '[]')",
                   (pid,))
        db.execute("INSERT INTO vitals (patient_id, timestamp, heart_rate, spo2, temperature, respiratory_rate) "
                   "VALUES (?, datetime('now'), 118, 93, 38.9, 26)", (pid,))
    evaluate_patient(pid)
    with db_session() as db:
        rows = db.execute(
            "SELECT insight_type, severity, suggested_action_type, suggested_action_target "
            "FROM sage_insights WHERE patient_id = ?", (pid,),
        ).fetchall()
        db.execute("DELETE FROM risk_scores WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
    kinds = {(r["insight_type"], r["severity"]) for r in rows}
    assert ("deterioration", "critical") in kinds and ("sepsis", "warning") in kinds, kinds
    for r in rows:
        assert r["suggested_action_type"] is None, dict(r)
        assert r["suggested_action_target"] is None, dict(r)


def test_act_refuses_a_stored_medication_suggestion(ward):
    """An insight stored before the change still carries start_medication. The
    server, not the button, is what must refuse it."""
    iid = _insight(ward, "start_medication", "IV ceftriaxone 2g")
    r = client.post(f"/api/sage/insights/{iid}/act", headers=_headers())
    assert r.status_code == 400, r.text
    with db_session() as db:
        assert db.execute("SELECT count(*) FROM sage_actions WHERE patient_id = ?",
                          (ward["pid"],)).fetchone()[0] == 0
        assert db.execute("SELECT state FROM sage_insights WHERE id = ?",
                          (iid,)).fetchone()["state"] == "active"


def test_feed_does_not_offer_a_stored_medication_suggestion(ward):
    from services.sage_proactive_service import get_active_insights
    iid = _insight(ward, "start_medication", "IV ceftriaxone 2g")
    row = next(i for i in get_active_insights(DOCTOR, "doctor") if i["id"] == iid)
    assert row["suggested_action_type"] is None
    assert row["suggested_action_target"] is None


def test_act_still_drafts_a_non_medication_suggestion(ward):
    """The refusal is scoped to drugs; the one-click lane itself stays."""
    iid = _insight(ward, "document_note", "Sepsis screen and cultures")
    r = client.post(f"/api/sage/insights/{iid}/act", headers=_headers())
    assert r.status_code == 200, r.text


# ═════════════════════════════════════════════════════════════════════════════
#  3. A stopped order reads as stopped, and the alerts reach the doctor
# ═════════════════════════════════════════════════════════════════════════════
def test_blocked_order_is_reported_blocked_not_sent(ward):
    add_allergy(ward["pid"], "ceftriaxone", reaction="anaphylaxis", severity="severe")
    aid = _card(ward, "Ceftriaxone 1g")
    handoff = _confirm(ward, aid)

    assert handoff["status"] == "blocked", handoff
    assert handoff["target_module"] == "helix"
    assert handoff["downstream_ref_id"] is None
    assert "ceftriaxone" in handoff["error_summary"].lower()
    assert "anaphylaxis" in handoff["error_summary"].lower()
    assert _charted(ward["pid"], "Ceftriaxone 1g") == 0

    listed = _listed_handoff(ward, aid)
    assert listed["status"] == "blocked"
    assert listed["error_summary"] == handoff["error_summary"]
    assert listed["safety"]["blocking"] is True
    assert any(a["severity"] == "severe" and a["type"] == "allergy" for a in listed["safety"]["alerts"])


def test_failed_screen_is_reported_blocked(ward, monkeypatch):
    def _boom(db, patient_id):
        raise RuntimeError("patient_allergies unreadable")

    import services.drug_safety_service as dss
    monkeypatch.setattr(dss, "_active_allergies", _boom)
    aid = _card(ward, "Amoxicillin 500mg")
    handoff = _confirm(ward, aid)

    assert handoff["status"] == "blocked", handoff
    assert "could not run" in handoff["error_summary"]
    assert _charted(ward["pid"], "Amoxicillin 500mg") == 0
    assert _listed_handoff(ward, aid)["safety"]["screen_failed"] is True


def test_non_blocking_alert_reaches_the_confirming_doctor(ward):
    """A moderate penicillin rash against a cephalosporin charts (a warning, not
    a stop), and the doctor now sees that alert instead of a bare "Sent to
    Helix"."""
    add_allergy(ward["pid"], "penicillin", reaction="rash", severity="moderate")
    aid = _card(ward, "Ceftriaxone 1g")
    handoff = _confirm(ward, aid)

    assert handoff["status"] == "accepted", handoff
    alerts = _listed_handoff(ward, aid)["safety"]["alerts"]
    cross = [a for a in alerts if a["match"] == "cross_reactivity"]
    assert cross and cross[0]["severity"] == "moderate", alerts
    assert "penicillin" in cross[0]["message"].lower()


def test_penicillin_anaphylaxis_against_a_cephalosporin_is_blocked(ward):
    """Ruled 2026-10-03: the order-time screen grades this as the bedside does,
    so the confirmed card is blocked rather than charted with a warning."""
    add_allergy(ward["pid"], "penicillin", reaction="anaphylaxis", severity="severe")
    aid = _card(ward, "Ceftriaxone 1g")
    handoff = _confirm(ward, aid)

    assert handoff["status"] == "blocked", handoff
    assert "anaphylaxis" in handoff["error_summary"].lower()
    assert _charted(ward["pid"], "Ceftriaxone 1g") == 0


def test_clean_order_carries_no_alerts(ward):
    aid = _card(ward, "Paracetamol 1g")
    handoff = _confirm(ward, aid)
    assert handoff["status"] == "accepted", handoff
    safety = _listed_handoff(ward, aid)["safety"]
    assert safety["alerts"] == [] and safety["blocking"] is False
    assert _charted(ward["pid"], "Paracetamol 1g") == 1


def test_charted_only_says_no_pharmacy_order_exists(ward):
    """A drug charted for review whose Helix order could not be placed (here,
    not in the catalog) used to read "Sent to Helix". It now carries why."""
    aid = _card(ward, "Zyloquin 500mg")
    handoff = _confirm(ward, aid)

    assert handoff["status"] == "accepted" and handoff["downstream_ref_id"] == "charted-only", handoff
    assert "not sent to pharmacy" in handoff["error_summary"]
    assert _charted(ward["pid"], "Zyloquin 500mg") == 1
    assert _listed_handoff(ward, aid)["error_summary"] == handoff["error_summary"]


def test_blocked_handoff_is_terminal(ward):
    """Dispatching the same action again returns the blocked record; it does not
    re-run the order behind the doctor's back."""
    from services.sage_handoff_service import create_and_dispatch_handoff
    add_allergy(ward["pid"], "ceftriaxone", reaction="anaphylaxis", severity="severe")
    aid = _card(ward, "Ceftriaxone 1g")
    first = _confirm(ward, aid)
    with db_session() as db:
        action = dict(db.execute("SELECT * FROM sage_actions WHERE id = ?", (aid,)).fetchone())
    again = create_and_dispatch_handoff(action, DOCTOR)
    assert again["id"] == first["id"] and again["status"] == "blocked"
    with db_session() as db:
        assert db.execute("SELECT count(*) FROM sage_handoffs WHERE source_action_id = ?",
                          (aid,)).fetchone()[0] == 1


# ═════════════════════════════════════════════════════════════════════════════
#  Schema: 'blocked' needs a sage_handoffs rebuild (SQLite cannot alter a CHECK)
# ═════════════════════════════════════════════════════════════════════════════
_REBUILD_DRIVERS = ["sqlite3"]
try:
    import sqlcipher3.dbapi2  # noqa: F401 (presence probe only)

    _REBUILD_DRIVERS.append("sqlcipher3.dbapi2")
except ImportError:
    pass


@pytest.mark.parametrize("driver", _REBUILD_DRIVERS)
def test_migration_admits_blocked_on_legacy_db(tmp_path, driver):
    """sage_handoffs is an FK parent (sage_handoff_events), so the rebuild has to
    own its autocommit window. Run inside an open IMMEDIATE transaction exactly
    as run_pending_migrations holds one."""
    drv = importlib.import_module(driver)
    raw = drv.connect(str(tmp_path / "legacy.db"))
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
    raw.execute("CREATE TABLE sage_actions (id TEXT PRIMARY KEY)")
    raw.execute("""CREATE TABLE sage_handoffs (
        id TEXT PRIMARY KEY,
        source_action_id TEXT NOT NULL REFERENCES sage_actions(id),
        room_id TEXT NOT NULL,
        patient_id TEXT NOT NULL,
        target_module TEXT NOT NULL,
        handoff_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'failed', 'unsupported')),
        downstream_ref_id TEXT,
        error_summary TEXT,
        created_by TEXT NOT NULL REFERENCES users(id),
        last_attempted_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    raw.execute("CREATE INDEX idx_sage_handoffs_action ON sage_handoffs(source_action_id, created_at DESC)")
    raw.execute("""CREATE TABLE sage_handoff_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        handoff_id TEXT NOT NULL REFERENCES sage_handoffs(id),
        new_status TEXT)""")
    raw.execute("INSERT INTO users (id) VALUES ('D1')")
    raw.execute("INSERT INTO sage_actions (id) VALUES ('A1')")
    raw.execute("INSERT INTO sage_handoffs (id, source_action_id, room_id, patient_id, target_module, "
                "handoff_type, payload_json, status, downstream_ref_id, created_by) "
                "VALUES ('H1', 'A1', 'R1', 'P1', 'helix', 'start_medication', '{}', 'accepted', "
                "'blocked-allergy', 'D1')")
    raw.execute("INSERT INTO sage_handoff_events (handoff_id, new_status) VALUES ('H1', 'accepted')")
    raw.commit()

    raw.isolation_level = None
    raw.execute("BEGIN IMMEDIATE")
    migrations._m39_handoff_blocked_status(raw)
    assert raw.in_transaction, "the runner's transaction was not re-opened"
    raw.execute("COMMIT")

    ddl = raw.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sage_handoffs'").fetchone()[0]
    assert "'blocked'" in ddl
    cols = [r[1] for r in raw.execute("PRAGMA table_info(sage_handoffs)").fetchall()]
    assert cols[-1] == "safety_json" and "status" in cols
    assert raw.execute("SELECT status, downstream_ref_id FROM sage_handoffs WHERE id='H1'").fetchone() \
        == ("accepted", "blocked-allergy"), "row not preserved"
    assert raw.execute("SELECT handoff_id FROM sage_handoff_events").fetchone()[0] == "H1"
    assert raw.execute("SELECT name FROM sqlite_master WHERE type='index' "
                       "AND name='idx_sage_handoffs_action'").fetchone() is not None
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    raw.execute("INSERT INTO sage_actions (id) VALUES ('A2')")
    raw.execute("INSERT INTO sage_handoffs (id, source_action_id, room_id, patient_id, target_module, "
                "handoff_type, payload_json, status, created_by, safety_json) "
                "VALUES ('H2', 'A2', 'R1', 'P1', 'helix', 'start_medication', '{}', 'blocked', 'D1', '{}')")
    with pytest.raises(drv.IntegrityError):
        raw.execute("INSERT INTO sage_handoffs (id, source_action_id, room_id, patient_id, target_module, "
                    "handoff_type, payload_json, status, created_by) "
                    "VALUES ('H3', 'A2', 'R1', 'P1', 'helix', 'x', '{}', 'wormhole', 'D1')")

    raw.execute("BEGIN IMMEDIATE")
    migrations._m39_handoff_blocked_status(raw)
    raw.execute("COMMIT")
    assert raw.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                       "AND name='sage_handoffs'").fetchone()[0] == ddl, "second run was not a no-op"
    raw.close()


def test_session_db_admits_blocked():
    """The live schema, fresh via SCHEMA or rebuilt by the migration, agrees."""
    with db_session() as db:
        ddl = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sage_handoffs'").fetchone()[0]
        cols = [r[1] for r in db.execute("PRAGMA table_info(sage_handoffs)").fetchall()]
    assert "'blocked'" in ddl
    assert cols[-1] == "safety_json"
