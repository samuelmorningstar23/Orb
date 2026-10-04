"""The unmatched-results queue (lis-19, W109), migration 60.

An interfaced result whose patient identifier matches nobody used to be stored
with no patient, where no chart and no review ever sees it. With
ORB_RESULT_QUARANTINE on (default OFF) it waits in result_quarantine until
laboratory staff attach it to a patient or discard it.

Pinned here:
  * migration 60: collected, idempotent on a fresh and a legacy database, no
    foreign key, a decided row never changes, the restore manifest names it;
  * switch off: an unmatched result is still stored with no patient, exactly
    as before, and the sender's answer is unchanged;
  * switch on: it goes to the queue instead, the sender still gets a success,
    a matched result and the vitals lane are untouched;
  * results only: a bedside monitor lane's metrics (mean pressure, ST level)
    and anything coded in a monitor's own nomenclature are stored with no
    patient as before, never queued and never swept;
  * ruling LIS-R17 (strictest option): only ingest.reconcile (laboratory
    roles) attaches or discards; the administrator lists and counts, without
    identifiers or values, and gets 403 on attach and discard;
  * the two-identifier check: two kinds must match both the message and the
    patient's record, a typed identifier that contradicts either refuses, a
    refusal is audited and leaves the row open;
  * strictest option (held for Samuel): a hospital number in the message that
    now names a different patient (registered after the result arrived)
    refuses the attach to anyone else outright, whatever the name and date of
    birth say, with its own outcome and a fail-closed refusal audit;
  * strictest option (held for Samuel): a name and date of birth that another
    patient record shares (or shares the name with no usable date of birth)
    confirm neither record, unless a hospital number in the message confirms
    the patient; refused and audited, department scope not applied;
  * a result older than a correction already on the chart answers 409 in
    words (the rule itself: tests/test_oru_corrections.py, section 8);
  * attach moves the result to the patient in one transaction with a
    fail-closed audit row, and rolls back when that row cannot be written;
    discard needs a reason;
  * the one-time sweep of results stored with no patient is idempotent, and
    attaching a swept row gives the existing row its patient;
  * the compliance report fails while the queue is on with nobody able to
    work it, and the boot check says so in the log.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import main
import migrations
import migrations_his
from auth import create_token, hash_password
from database import db_session
from db_backend import DBIntegrityError
from migrations_his import m0060_result_quarantine as m60
from permissions import DEPARTMENT_REQUIRED_ROLES, INGEST_RECONCILE, ROLE_PERMISSIONS
from services import compliance_service, ingestion_service, result_quarantine_service as rq
from services.audit_service import AuditWriteError
from services.ingestion_adapters import HL7Adapter, JSONAdapter, hl7_patient_identity

client = TestClient(main.app)
SERVER = Path(__file__).resolve().parents[1]


def _user(role: str, department: str | None = None, *, active: int = 1) -> tuple[str, dict]:
    uid = f"RQ{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, department, hash_password("password"), active))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


def _patient(name="Ravi Sharma", dob="1970-01-01", mrn=None) -> dict:
    pid = f"PRQ{uuid.uuid4().hex[:8].upper()}"
    mrn = mrn or f"MRN-RQ-{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, date_of_birth, status) VALUES (?, ?, ?, ?, 'discharged')",
            (pid, name, mrn, dob))
    return {"id": pid, "mrn": mrn, "name": name, "dob": dob}


def _oru(identifier: str, *, name="SHARMA^RAVI^^^MR", dob="19700101", value="5.1",
         code="2823-3", text="Potassium", extra_ids: str = "") -> str:
    pid3 = f"{identifier}^^^EXTLAB^MR" + (f"~{extra_ids}" if extra_ids else "")
    return "\r".join([
        f"MSH|^~\\&|LABSYS|CITYLAB|ORB|HOSP|20261004081000||ORU^R01|{uuid.uuid4().hex[:12]}|P|2.5",
        f"PID|1||{pid3}||{name}||{dob}|M",
        "OBR|1|PL1|FL1|24323-8^Basic panel^LN|||20261004080000",
        f"OBX|1|NM|{code}^{text}^LN||{value}|mmol/L|3.5-5.1|H|||F|||20261004080000",
    ])


def _person() -> dict:
    """A name no other patient in the session database has. The default
    'Ravi Sharma', born 1 January 1970, is registered by many tests, which is
    exactly the case where name and date of birth confirm nobody; a test that
    must attach by name and date of birth uses one of these."""
    tag = f"Q{uuid.uuid4().hex[:6].upper()}"
    return {"registered": f"Ravi {tag} Sharma", "hl7": f"SHARMA^RAVI {tag}^^^MR",
            "message": f"SHARMA, RAVI {tag}"}


def _ingest(raw: str):
    return asyncio.run(ingestion_service.ingest_payload(
        raw, adapter=HL7Adapter(), source_name="lis19-test"))


def _queue_row(event_id: int) -> dict | None:
    with db_session() as db:
        row = db.execute("SELECT * FROM result_quarantine WHERE ingestion_event_id = ?",
                         (event_id,)).fetchone()
        return dict(row) if row else None


def _audits(action: str, target_id: str) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND target_id = ? ORDER BY id",
            (action, target_id)).fetchall()]


@pytest.fixture
def queue_on(monkeypatch):
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", True)
    assert config.feature_enabled("result_quarantine")


@pytest.fixture
def lab_tech():
    return _user("lab_technician")


@pytest.fixture
def admin():
    return _user("admin")


@pytest.fixture
def held(queue_on):
    """One unmatched potassium in the queue, its message naming Ravi Sharma,
    born 1 January 1970, under an outside laboratory's number."""
    res = _ingest(_oru(f"EXT{uuid.uuid4().hex[:6].upper()}"))
    assert res.quarantined == 1 and res.inserted_observations == 0
    return _queue_row(res.event_id)


# ── 1. Migration 60 ──────────────────────────────────────────────────────────

def test_migration_60_is_collected_and_applied():
    assert {m.version: m.name for m in migrations_his.collect()}[60] == "result_quarantine"
    assert {m.version: m.name for m in migrations.MIGRATIONS}[60] == "result_quarantine"
    with db_session() as db:
        assert db.execute("SELECT 1 FROM schema_migrations WHERE version = 60").fetchone()
        assert db.execute("PRAGMA foreign_key_list(result_quarantine)").fetchall() == []
        cols = {r[1] for r in db.execute("PRAGMA table_info(observations)")}
        assert {"obr_placer", "obr_filler", "obx_sub_id", "hl7_sender"} <= cols
        ddl = db.execute("SELECT sql FROM sqlite_master WHERE name = 'result_quarantine'").fetchone()[0]
        assert "CURRENT_TIMESTAMP" not in ddl.upper()


def test_migration_60_is_idempotent_on_a_legacy_database_and_keeps_its_rows(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT, "
                 "source TEXT NOT NULL DEFAULT 'unknown', observation_name TEXT NOT NULL, "
                 "value_numeric REAL, status TEXT DEFAULT 'final')")
    conn.execute("INSERT INTO observations (patient_id, observation_name, value_numeric) "
                 "VALUES (NULL, 'Potassium', 4.1)")
    before = dict(conn.execute("SELECT * FROM observations").fetchone())
    for _ in range(2):
        m60.apply(conn)
    after = dict(conn.execute("SELECT * FROM observations").fetchone())
    assert {k: after[k] for k in before} == before
    assert all(after[c] is None for c in ("obr_placer", "obr_filler", "obx_sub_id", "hl7_sender"))
    assert conn.execute("SELECT COUNT(*) FROM result_quarantine").fetchone()[0] == 0
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_a_decided_row_never_changes_but_can_be_deleted(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "decided.db"))
    m60.apply(conn)
    now = "2026-10-04T06:00:00.000000+00:00"
    qid = conn.execute("INSERT INTO result_quarantine (lane, reason, payload_json, created_at) "
                       "VALUES ('ingestion', 'unmatched_patient', '{}', ?)", (now,)).lastrowid
    with pytest.raises(DBIntegrityError):
        conn.execute("UPDATE result_quarantine SET status = 'discarded', resolved_by = 'U', "
                     "resolved_at = ? WHERE id = ?", (now, qid))  # no reason
    with pytest.raises(DBIntegrityError):
        conn.execute("UPDATE result_quarantine SET status = 'attached', resolved_by = 'U', "
                     "resolved_at = ?, resolution_reason = 'r' WHERE id = ?", (now, qid))  # no patient
    conn.execute("UPDATE result_quarantine SET status = 'discarded', resolved_by = 'U', "
                 "resolved_at = ?, resolution_reason = 'not ours' WHERE id = ?", (now, qid))
    with pytest.raises(DBIntegrityError, match="cannot change"):
        conn.execute("UPDATE result_quarantine SET status = 'open' WHERE id = ?", (qid,))
    conn.execute("DELETE FROM result_quarantine WHERE id = ?", (qid,))


def test_the_restore_manifest_and_retention_map_name_the_queue():
    path = SERVER.parent / "scripts" / "lib" / "verify_restore.py"
    spec = importlib.util.spec_from_file_location("_vr_for_quarantine_test", path)
    vr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vr)
    assert vr.MODULE_TABLES["result quarantine"] == ("result_quarantine",)
    from services.consent_service import _RETENTION_SOURCES
    assert ("result_quarantine", None, "created_at") in _RETENTION_SOURCES["clinical_record"]


# ── 2. The ingestion lane, switch off and on ─────────────────────────────────

def test_switch_off_an_unmatched_result_is_stored_with_no_patient_as_before():
    assert not config.feature_enabled("result_quarantine")
    res = _ingest(_oru(f"EXT{uuid.uuid4().hex[:6].upper()}"))
    assert res.inserted_observations == 1 and res.quarantined == 0
    assert "quarantined" not in res.to_dict()
    assert _queue_row(res.event_id) is None
    with db_session() as db:
        row = db.execute("SELECT patient_id, value_numeric FROM observations "
                         "WHERE source_event_id = ?", (res.event_id,)).fetchone()
    assert row["patient_id"] is None and row["value_numeric"] == 5.1


def test_switch_on_an_unmatched_result_goes_to_the_queue(queue_on):
    ext = f"EXT{uuid.uuid4().hex[:6].upper()}"
    res = _ingest(_oru(ext, extra_ids="ALT-9^^^OTHER^PI"))
    assert res.parse_status == "parsed", "held is stored: the sender must not retry"
    assert (res.inserted_observations, res.quarantined) == (0, 1)
    assert res.to_dict()["quarantined"] == 1
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM observations WHERE source_event_id = ?",
                          (res.event_id,)).fetchone()[0] == 0
    row = _queue_row(res.event_id)
    assert row["status"] == "open" and row["lane"] == "ingestion"
    assert row["reason"] == "unmatched_patient" and row["source"] == "ingested_hl7"
    assert row["raw_patient_identifier"] == ext
    assert row["raw_patient_name"] == "SHARMA, RAVI", "prefix 'MR' is not part of the name"
    assert row["raw_patient_dob"] == "1970-01-01"
    payload = json.loads(row["payload_json"])
    assert payload["patient_identifiers"] == [ext, "ALT-9"]
    assert payload["value_numeric"] == 5.1 and payload["observation_code"] == "2823-3"
    assert _audits("observation_quarantined", str(row["id"]))


def test_switch_on_a_matched_result_is_stored_as_before(queue_on):
    p = _patient()
    res = _ingest(_oru(p["mrn"]))
    assert (res.inserted_observations, res.quarantined) == (1, 0)
    assert _queue_row(res.event_id) is None


def test_the_vitals_lane_is_untouched(queue_on):
    raw = "\r".join([
        f"MSH|^~\\&|MON|ICU|ORB|HOSP|20261004081000||ORU^R01|{uuid.uuid4().hex[:12]}|P|2.5",
        f"PID|1||NOBODY-{uuid.uuid4().hex[:6]}||X^Y",
        "OBR|1|||VITALS|||20261004080000",
        "OBX|1|NM|8867-4^Heart rate^LN||88|bpm|||||F|||20261004080000",
    ])
    res = _ingest(raw)
    assert res.quarantined == 0 and res.inserted_vitals == 0
    assert any("Patient not found" in e for e in res.errors)
    assert _queue_row(res.event_id) is None


def _monitor_oru(bed_id: str) -> str:
    """A bedside monitor's message from a bed nobody is registered under: a
    heart rate (a vital sign) and two parameters the monitor lane keeps as
    LAB-class rows, mean blood pressure and the lead II ST level."""
    ts = "20261004080000"
    return "\r".join([
        f"MSH|^~\\&|BEDSIDE|ICU|ORB|HOSP|{ts}||ORU^R01|{uuid.uuid4().hex[:12]}|P|2.5",
        f"PID|1||{bed_id}^^^LOCAL^PI||MONITOR^BED",
        f"OBR|1|||VITALS|||{ts}",
        f"OBX|1|NM|8867-4^Heart rate^LN||88|/min|||||F|||{ts}",
        f"OBX|2|NM|8478-0^Mean blood pressure^LN||72|mm[Hg]|||||F|||{ts}",
        f"OBX|3|NM|ST-II^ST segment lead II^L||0.1|mm|||||F|||{ts}",
    ])


def _monitor_ingest(raw: str):
    from services.vitals_source import LABEL_MONITOR_HL7, vendor_profiles
    from services.vitals_source.profile_adapter import VendorProfileAdapter
    return asyncio.run(ingestion_service.ingest_payload(
        raw, adapter=VendorProfileAdapter(vendor_profiles.GENERIC),
        source_name="BED12-MONITOR", source_label=LABEL_MONITOR_HL7))


def test_a_bedside_monitor_lane_never_fills_the_queue(queue_on, monkeypatch):
    """The reviewer's case: a monitor whose PID-3 is a local bed id sends
    mean blood pressure and ST level at monitor cadence. They are device
    metrics, not results: stored with no patient exactly as before, never
    queued, never swept, so the laboratory's results stay reachable."""
    bed = f"BED12-LOCAL-{uuid.uuid4().hex[:4]}"
    events = []
    for _ in range(5):
        res = _monitor_ingest(_monitor_oru(bed))
        assert res.quarantined == 0, res.to_dict()
        assert res.inserted_observations == 2
        assert _queue_row(res.event_id) is None
        events.append(res.event_id)
    marks = ", ".join("?" for _ in events)
    with db_session() as db:
        rows = db.execute(
            f"SELECT id, patient_id, source, observation_name FROM observations "
            f"WHERE source_event_id IN ({marks})", events).fetchall()
    assert len(rows) == 10
    assert all(r["patient_id"] is None and r["source"] == "monitor_hl7" for r in rows)
    assert {r["observation_name"] for r in rows} == {"Mean blood pressure", "ST segment lead II"}
    rq.sweep_unlinked()
    with db_session() as db:
        swept = db.execute(
            f"SELECT COUNT(*) FROM result_quarantine WHERE source_observation_id IN "
            f"({', '.join('?' for _ in rows)})", [r["id"] for r in rows]).fetchone()[0]
    assert swept == 0, "the sweep leaves a device lane's rows where they are"


def test_monitor_nomenclature_over_a_generic_connection_is_not_queued(queue_on):
    """A monitor gateway posting to the generic HL7 connection: its mean
    pressure is coded in the monitor's own nomenclature (OBX-3.3 MDIL) and is
    not queued; a laboratory result in the same message still is."""
    ts = "20261004080000"
    raw = "\r".join([
        f"MSH|^~\\&|LABSYS|CITYLAB|ORB|HOSP|{ts}||ORU^R01|{uuid.uuid4().hex[:12]}|P|2.5",
        f"PID|1||EXT{uuid.uuid4().hex[:6].upper()}^^^EXTLAB^MR||SHARMA^RAVI||19700101|M",
        f"OBR|1|PL1|FL1|24323-8^Basic panel^LN|||{ts}",
        f"OBX|1|NM|150019^MDC_PRESS_BLD_NONINV_MEAN^MDIL||72|mmHg|||||F|||{ts}",
        f"OBX|2|NM|2823-3^Potassium^LN||5.1|mmol/L|3.5-5.1|H|||F|||{ts}",
    ])
    res = _ingest(raw)
    assert (res.inserted_observations, res.quarantined) == (1, 1)
    assert _queue_row(res.event_id)["observation_name"] == "Potassium"
    with db_session() as db:
        stored = db.execute("SELECT patient_id, observation_code_system FROM observations "
                            "WHERE source_event_id = ?", (res.event_id,)).fetchall()
    assert [(r["patient_id"], r["observation_code_system"]) for r in stored] == [(None, "MDIL")]


def test_takes_result_keeps_every_generic_connection_and_drops_device_lanes():
    for label in ("ingested_hl7", "ingested_json", "ingested_fhir", "csv_replay", "a_new_feed", None):
        assert rq.takes_result(label, "LN"), label
    for label in ("monitor_hl7", "MONITOR_HL7", "manual_entry", "nurse_manual", "simulated"):
        assert not rq.takes_result(label, "LN"), label
    for system in ("MDC", "mdil", "99MDIL", "GEMS"):
        assert not rq.takes_result("ingested_hl7", system), system
    from services.vitals_source import base
    assert all(base.classify_source_label(label) is not base.SourceKind.UNKNOWN
               for label in rq.device_source_labels())


# ── 3. Who may see and decide (ruling LIS-R17) ───────────────────────────────

def test_no_department_scoped_role_and_no_admin_holds_ingest_reconcile():
    assert INGEST_RECONCILE not in ROLE_PERMISSIONS["admin"]
    for role in DEPARTMENT_REQUIRED_ROLES:
        assert INGEST_RECONCILE not in ROLE_PERMISSIONS[role], role
    assert {r for r, perms in ROLE_PERMISSIONS.items() if INGEST_RECONCILE in perms} == {
        "lab_technician", "pathologist"}


def test_the_admin_list_has_counts_but_no_identifiers_or_values(held, admin):
    uid, hdr = admin
    resp = client.get("/api/ingest/quarantine", headers=hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["view"] == "monitor" and body["counts"]["open"] >= 1
    item = next(i for i in body["items"] if i["id"] == held["id"])
    for hidden in ("message_identifiers", "message_name", "message_date_of_birth", "value",
                   "resolution_reason", "attached_patient_id", "reason"):
        assert hidden not in item, hidden
    assert item["observation_name"] == "Potassium"
    with db_session() as db:
        audit = db.execute(
            "SELECT event_category FROM audit_logs WHERE action_type = 'lab_quarantine_viewed' "
            "AND actor_user_id = ?", (uid,)).fetchone()
    assert audit["event_category"] == "phi_access"


def test_the_reconciler_list_shows_what_the_message_said(held, lab_tech):
    _, hdr = lab_tech
    body = client.get("/api/ingest/quarantine", headers=hdr).json()
    item = next(i for i in body["items"] if i["id"] == held["id"])
    assert body["view"] == "reconcile"
    assert item["message_identifiers"] == [held["raw_patient_identifier"]]
    assert item["message_name"] == "SHARMA, RAVI"
    assert item["message_date_of_birth"] == "1970-01-01"
    assert item["value"] == "5.1" and item["unit"] == "mmol/L"
    assert item["sender"] == "LABSYS, CITYLAB", "the sender the message names (MSH-3, MSH-4)"
    assert item["identifiable"] is True, "number, name and date of birth: three kinds"


def test_a_result_naming_the_patient_one_way_only_can_never_be_attached(queue_on, lab_tech, admin):
    number = f"JS-{uuid.uuid4().hex[:6].upper()}"
    res = asyncio.run(ingestion_service.ingest_payload(json.dumps({
        "patient_id": number, "message_id": uuid.uuid4().hex,
        "observations": [{"code": "2160-0", "name": "Creatinine", "value": 2.4, "unit": "mg/dL",
                          "status": "final"}]}), adapter=JSONAdapter(), source_name="lis19-json"))
    assert res.quarantined == 1
    held = _queue_row(res.event_id)
    item = next(i for i in client.get("/api/ingest/quarantine", headers=lab_tech[1]).json()["items"]
                if i["id"] == held["id"])
    assert item["identifiable"] is False
    admin_item = next(i for i in client.get("/api/ingest/quarantine", headers=admin[1]).json()["items"]
                      if i["id"] == held["id"])
    assert "identifiable" not in admin_item
    # Even once the patient is registered under that very number, the number is
    # one identifier: the message has no second one to confirm.
    _patient(mrn=number)
    resp = _attach(held["id"], lab_tech[1], hospital_number=number, name="Ravi Sharma",
                   date_of_birth="1970-01-01")
    assert resp.status_code == 409
    assert resp.json()["detail"]["checks"] == {
        "hospital_number": "match", "name": "not_in_message", "date_of_birth": "not_in_message"}
    assert _queue_row(res.event_id)["status"] == "open"


def test_message_identity_kinds_counts_only_usable_identifiers():
    assert rq.message_identity_kinds({"identifiers": ["X"], "name": "A B", "dob": "1970-01-01"}) == 3
    assert rq.message_identity_kinds({"identifiers": [" "], "name": " , ", "dob": "1970-13-40"}) == 0
    assert rq.message_identity_kinds({"identifiers": ["X"], "name": None, "dob": None}) == 1


def test_the_sender_falls_back_to_the_connection_name_never_a_default_label():
    assert rq._sender_words("LABSYS^CITYLAB", "x") == "LABSYS, CITYLAB"
    assert rq._sender_words(None, "City lab feed") == "City lab feed"
    assert rq._sender_words(None, "hl7_sender") is None


def test_admin_cannot_attach_or_discard(held, admin):
    _, hdr = admin
    p = _patient()
    attach = client.post(f"/api/ingest/quarantine/{held['id']}/attach", headers=hdr, json={
        "hospital_number": p["mrn"], "name": "Ravi Sharma", "date_of_birth": "1970-01-01",
        "reason": "admin try"})
    assert attach.status_code == 403
    discard = client.post(f"/api/ingest/quarantine/{held['id']}/discard", headers=hdr,
                          json={"reason": "admin try"})
    assert discard.status_code == 403
    assert _queue_row(held["ingestion_event_id"])["status"] == "open"


@pytest.mark.parametrize("role", ["nurse", "doctor", "phlebotomist", "receptionist"])
def test_other_roles_cannot_even_list(held, role):
    _, hdr = _user(role, "Cardiology" if role in DEPARTMENT_REQUIRED_ROLES else None)
    assert client.get("/api/ingest/quarantine", headers=hdr).status_code == 403


def test_switched_off_and_empty_the_queue_answers_404(monkeypatch, admin):
    monkeypatch.setattr(rq, "queue_reachable", lambda db: False)
    resp = client.get("/api/ingest/quarantine", headers=admin[1])
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "feature_disabled"


def test_switched_off_with_open_rows_the_queue_stays_reachable(held, monkeypatch, lab_tech):
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", False)
    resp = client.get("/api/ingest/quarantine", headers=lab_tech[1])
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    assert any(i["id"] == held["id"] for i in resp.json()["items"])


# ── 4. The two-identifier check ──────────────────────────────────────────────

def _attach(qid, hdr, **body):
    body.setdefault("reason", "Confirmed against the request form")
    return client.post(f"/api/ingest/quarantine/{qid}/attach", headers=hdr, json=body)


def test_attach_without_matching_identifiers_is_refused_and_audited(held, lab_tech):
    p = _patient(name="Asha Rao", dob="1981-03-03")
    resp = _attach(held["id"], lab_tech[1], hospital_number=p["mrn"], name="Asha Rao",
                   date_of_birth="1981-03-03")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "identity_not_confirmed"
    assert detail["checks"]["name"] == "no_match" and detail["checks"]["date_of_birth"] == "no_match"
    # The answer is per kind only: it never repeats the record's name or date.
    assert "Asha" not in json.dumps(detail) and "1981" not in json.dumps(detail)
    assert _queue_row(held["ingestion_event_id"])["status"] == "open"
    refusals = _audits("lab_quarantine_attach_refused", str(held["id"]))
    assert refusals and refusals[-1]["event_category"] == "clinical_access"
    assert refusals[-1]["outcome"] == "failure"


def test_one_matching_identifier_is_not_enough(held, lab_tech):
    p = _patient()
    resp = _attach(held["id"], lab_tech[1], hospital_number=p["mrn"], name="Ravi Sharma")
    assert resp.status_code == 409
    checks = resp.json()["detail"]["checks"]
    assert checks["name"] == "match"
    assert checks["hospital_number"] == "differs_in_message"
    assert checks["date_of_birth"] == "not_given"


def test_a_typed_identifier_that_contradicts_refuses_even_with_two_matches(queue_on, lab_tech):
    p = _patient()
    # The message carries the patient's own number this time.
    res = _ingest(_oru("EXT-ZZ", extra_ids=f"{p['mrn']}^^^HOSP^MR"))
    held = _queue_row(res.event_id)
    resp = _attach(held["id"], lab_tech[1], hospital_number=p["mrn"], name="Ravi Sharma",
                   date_of_birth="1971-01-01")
    assert resp.status_code == 409
    checks = resp.json()["detail"]["checks"]
    assert checks["hospital_number"] == "match" and checks["name"] == "match"
    assert checks["date_of_birth"] == "no_match"


def test_an_unknown_hospital_number_is_refused(held, lab_tech):
    resp = _attach(held["id"], lab_tech[1], hospital_number="NO-SUCH-NUMBER",
                   name="Ravi Sharma", date_of_birth="1970-01-01")
    assert resp.status_code == 409
    assert resp.json()["detail"]["checks"]["hospital_number"] == "no_match"


def test_a_number_that_names_two_patients_when_case_is_ignored_names_nobody(held, lab_tech):
    stem = f"case-{uuid.uuid4().hex[:6]}"
    _patient(mrn=stem.upper())
    _patient(mrn=stem.capitalize())
    # Typed in a third spelling, it matches neither exactly and both ignoring case.
    resp = _attach(held["id"], lab_tech[1], hospital_number=stem, name="Ravi Sharma",
                   date_of_birth="1970-01-01")
    assert resp.status_code == 409
    assert resp.json()["detail"]["checks"]["hospital_number"] == "no_match"
    assert _queue_row(held["ingestion_event_id"])["status"] == "open"
    # An exact spelling still names its own patient.
    with db_session() as db:
        assert rq._find_patient(db, stem.upper())["mrn"] == stem.upper()


def test_attach_moves_the_result_to_the_patient_in_one_audited_transaction(queue_on, lab_tech):
    uid, hdr = lab_tech
    who = _person()
    held = _queue_row(_ingest(_oru(f"EXT{uuid.uuid4().hex[:6].upper()}", name=who["hl7"])).event_id)
    p = _patient(name=who["registered"])
    typed = "  " + who["registered"].upper().replace(" ", "  ")
    resp = _attach(held["id"], hdr, hospital_number=p["mrn"], name=typed,
                   date_of_birth="1970-01-01", reason="Outside lab number; form checked")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["attached"] is True and body["patient_id"] == p["id"]
    with db_session() as db:
        ob = db.execute("SELECT * FROM observations WHERE id = ?", (body["observation_id"],)).fetchone()
    assert ob["patient_id"] == p["id"] and ob["value_numeric"] == 5.1
    assert ob["source"] == "ingested_hl7" and ob["source_event_id"] == held["ingestion_event_id"]
    row = _queue_row(held["ingestion_event_id"])
    assert row["status"] == "attached" and row["attached_patient_id"] == p["id"]
    assert row["attached_observation_id"] == body["observation_id"]
    assert row["resolved_by"] == uid and row["resolution_reason"] == "Outside lab number; form checked"
    assert json.loads(row["identity_confirmation_json"])["confirmed"] == ["date_of_birth", "name"]
    audits = _audits("lab_quarantine_attached", str(held["id"]))
    assert len(audits) == 1 and audits[0]["event_category"] == "clinical_access"
    assert audits[0]["patient_id"] == p["id"] and audits[0]["actor_user_id"] == uid
    again = _attach(held["id"], hdr, hospital_number=p["mrn"], name=who["registered"],
                    date_of_birth="1970-01-01")
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_resolved"


def test_a_patient_registered_after_the_result_arrived_matches_by_number(queue_on, lab_tech):
    number = f"LATE-{uuid.uuid4().hex[:6].upper()}"
    res = _ingest(_oru(number))
    held = _queue_row(res.event_id)
    p = _patient(mrn=number)
    resp = _attach(held["id"], lab_tech[1], hospital_number=number, name="Ravi Sharma")
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"]["hospital_number"] == "match"


def test_a_message_number_that_now_names_another_patient_refuses_the_attach(queue_on, lab_tech):
    """The reviewer's case: a result for H-777 matched nobody and was queued;
    patient Y is then registered as H-777. Patient X, a different person with
    the same name and the same approximated date of birth, must not receive
    it, although name and date of birth both match."""
    uid, hdr = lab_tech
    number = f"H-{uuid.uuid4().hex[:6].upper()}"
    res = _ingest(_oru(number))
    held = _queue_row(res.event_id)
    assert held["status"] == "open"
    y = _patient(mrn=number)
    x = _patient()
    resp = _attach(held["id"], hdr, hospital_number=x["mrn"], name="Ravi Sharma",
                   date_of_birth="1970-01-01")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "message_names_another_patient"
    assert detail["checks"] == {"hospital_number": "names_another_patient", "name": "match",
                                "date_of_birth": "match"}
    # Neither patient is named in the answer.
    assert y["id"] not in json.dumps(detail) and x["id"] not in json.dumps(detail)
    assert _queue_row(res.event_id)["status"] == "open"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM observations WHERE patient_id IN (?, ?)",
                          (x["id"], y["id"])).fetchone()[0] == 0
    refusal = _audits("lab_quarantine_attach_refused", str(held["id"]))[-1]
    assert refusal["event_category"] == "clinical_access" and refusal["outcome"] == "failure"
    assert refusal["patient_id"] == x["id"] and refusal["actor_user_id"] == uid
    meta = json.loads(refusal["metadata_json"])
    assert meta["refusal"] == "message_names_another_patient"
    assert meta["message_patient_id"] == y["id"]
    # The patient the number names can still receive it.
    ok = _attach(held["id"], hdr, hospital_number=number, name="Ravi Sharma")
    assert ok.status_code == 200, ok.text
    assert ok.json()["patient_id"] == y["id"]


def test_any_number_in_the_message_naming_someone_else_refuses(queue_on, lab_tech):
    """Every PID-3 repetition counts, matched ignoring case and spaces."""
    other = _patient(name="Asha Rao", dob="1981-03-03",
                     mrn=f"ALT-{uuid.uuid4().hex[:6].upper()}")
    spelled = other["mrn"].lower().replace("-", " - ")
    res = _ingest(_oru(f"EXT{uuid.uuid4().hex[:6].upper()}", extra_ids=f"{spelled}^^^OLD^MR"))
    held = _queue_row(res.event_id)
    x = _patient()
    out = rq.attach(held["id"], hospital_number=x["mrn"], name="Ravi Sharma",
                    date_of_birth="1970-01-01", reason="Form checked",
                    user={"id": lab_tech[0], "role": "lab_technician"})
    assert out["attached"] is False and out["refusal"] == rq.REFUSED_OTHER_PATIENT
    assert out["checks"]["hospital_number"] == rq.NAMES_ANOTHER
    with db_session() as db:
        message = rq._message_identity(held, json.loads(held["payload_json"]))
        assert rq._message_names_another_patient(db, message, {"id": other["id"]}) is None
        assert rq._message_names_another_patient(db, message, {"id": x["id"]}) == other["id"]


def test_the_refusal_for_another_patient_is_fail_closed(queue_on, lab_tech, monkeypatch):
    number = f"H-{uuid.uuid4().hex[:6].upper()}"
    res = _ingest(_oru(number))
    held = _queue_row(res.event_id)
    _patient(mrn=number)
    x = _patient()
    real = rq.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "lab_quarantine_attach_refused":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(rq, "log_audit_event", failing)
    with pytest.raises(AuditWriteError):
        rq.attach(held["id"], hospital_number=x["mrn"], name="Ravi Sharma",
                  date_of_birth="1970-01-01", reason="r",
                  user={"id": lab_tech[0], "role": "lab_technician"})
    assert _queue_row(res.event_id)["status"] == "open"


# ── 4b. A name and date of birth that name two records (strictest option) ────

def test_a_name_and_birth_date_two_records_share_confirm_neither(queue_on, lab_tech):
    """The reviewer's case: two registered patients with the same name, both
    born 1 January 1970, and an unmatched potassium of 6.9 under an outside
    laboratory's number. Name and date of birth cannot tell A from B, so
    neither may receive it."""
    uid, hdr = lab_tech
    who = _person()
    a = _patient(name=who["registered"])
    b = _patient(name=who["registered"])
    res = _ingest(_oru(f"EXT{uuid.uuid4().hex[:6].upper()}", name=who["hl7"], value="6.9"))
    held = _queue_row(res.event_id)
    for chosen, other in ((b, a), (a, b)):
        resp = _attach(held["id"], hdr, hospital_number=chosen["mrn"], name=who["registered"],
                       date_of_birth="1970-01-01")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "name_and_birth_date_shared"
        assert detail["checks"] == {"hospital_number": "differs_in_message",
                                    "name": "shared_with_another_patient",
                                    "date_of_birth": "shared_with_another_patient"}
        assert other["id"] not in json.dumps(detail) and chosen["id"] not in json.dumps(detail)
        refusal = _audits("lab_quarantine_attach_refused", str(held["id"]))[-1]
        assert refusal["event_category"] == "clinical_access" and refusal["outcome"] == "failure"
        assert refusal["patient_id"] == chosen["id"] and refusal["actor_user_id"] == uid
        meta = json.loads(refusal["metadata_json"])
        assert meta["refusal"] == "name_and_birth_date_shared"
        assert meta["shared_with_patient_ids"] == [other["id"]]
    assert _queue_row(res.event_id)["status"] == "open"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM observations WHERE patient_id IN (?, ?)",
                          (a["id"], b["id"])).fetchone()[0] == 0


def test_a_same_name_record_with_no_birth_date_also_counts(queue_on, lab_tech):
    who = _person()
    p = _patient(name=who["registered"])
    _patient(name=who["registered"].upper(), dob=None)
    held = _queue_row(_ingest(_oru(f"EXT{uuid.uuid4().hex[:6].upper()}", name=who["hl7"])).event_id)
    resp = _attach(held["id"], lab_tech[1], hospital_number=p["mrn"], name=who["registered"],
                   date_of_birth="1970-01-01")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "name_and_birth_date_shared"


def test_another_birth_date_or_another_name_is_not_shared(queue_on, lab_tech):
    who = _person()
    p = _patient(name=who["registered"])
    _patient(name=who["registered"], dob="1971-01-01")      # same name, another date
    _patient(name=f"{who['registered']} Junior")           # a different name
    held = _queue_row(_ingest(_oru(f"EXT{uuid.uuid4().hex[:6].upper()}", name=who["hl7"])).event_id)
    resp = _attach(held["id"], lab_tech[1], hospital_number=p["mrn"], name=who["registered"],
                   date_of_birth="1970-01-01")
    assert resp.status_code == 200, resp.text
    assert resp.json()["patient_id"] == p["id"]


def test_a_result_older_than_a_correction_on_the_chart_answers_409_in_words(queue_on, lab_tech):
    """The route's side of a late attach refusal (the rule itself is pinned
    in tests/test_oru_corrections.py): a final waits here while the
    laboratory's correction of it reaches the chart at receipt."""
    who = _person()
    p = _patient(name=who["registered"])
    held = _queue_row(_ingest(_oru(f"EXT{uuid.uuid4().hex[:6].upper()}", name=who["hl7"],
                                   value="6.9")).event_id)
    corrected = _ingest(_oru(p["mrn"], value="4.2").replace("|||F|||", "|||C|||"))
    assert corrected.inserted_observations == 1
    resp = _attach(held["id"], lab_tech[1], hospital_number=p["mrn"], name=who["registered"],
                   date_of_birth="1970-01-01")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "later_correction_on_chart"
    assert "sent after this one" in detail["message"]
    assert detail["checks"]["name"] == "match" and detail["checks"]["date_of_birth"] == "match"
    assert _queue_row(held["ingestion_event_id"])["status"] == "open"


def test_a_hospital_number_in_the_message_still_confirms_a_shared_name(queue_on, lab_tech):
    """When the message carries the chosen patient's own hospital number, the
    number tells the two records apart, so the number and one of name or date
    of birth confirm as before. The other record cannot receive it at all."""
    who = _person()
    a = _patient(name=who["registered"])
    b = _patient(name=who["registered"])
    res = _ingest(_oru("EXT-ZZ", name=who["hl7"], extra_ids=f"{b['mrn']}^^^HOSP^MR"))
    held = _queue_row(res.event_id)
    wrong = _attach(held["id"], lab_tech[1], hospital_number=a["mrn"], name=who["registered"],
                    date_of_birth="1970-01-01")
    assert wrong.status_code == 409
    assert wrong.json()["detail"]["error"] == "message_names_another_patient"
    ok = _attach(held["id"], lab_tech[1], hospital_number=b["mrn"], name=who["registered"],
                 date_of_birth="1970-01-01")
    assert ok.status_code == 200, ok.text
    assert ok.json()["patient_id"] == b["id"]
    assert ok.json()["checks"] == {"hospital_number": "match", "name": "match",
                                   "date_of_birth": "match"}


def test_attach_needs_a_reason_and_a_real_date(held, lab_tech):
    p = _patient()
    blank = _attach(held["id"], lab_tech[1], hospital_number=p["mrn"], name="Ravi Sharma",
                    date_of_birth="1970-01-01", reason="   ")
    assert blank.status_code == 422 and blank.json()["detail"]["error"] == "reason_required"
    bad = _attach(held["id"], lab_tech[1], hospital_number=p["mrn"], name="Ravi Sharma",
                  date_of_birth="1970-02-31")
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "bad_date_of_birth"
    assert _queue_row(held["ingestion_event_id"])["status"] == "open"


def test_attach_rolls_back_when_its_audit_row_cannot_be_written(queue_on, lab_tech, monkeypatch):
    who = _person()
    held = _queue_row(_ingest(_oru(f"EXT{uuid.uuid4().hex[:6].upper()}", name=who["hl7"])).event_id)
    p = _patient(name=who["registered"])
    real = rq.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "lab_quarantine_attached":
            raise AuditWriteError("audit store unavailable")
        return real(*args, **kwargs)
    monkeypatch.setattr(rq, "log_audit_event", failing)
    with pytest.raises(AuditWriteError):
        rq.attach(held["id"], hospital_number=p["mrn"], name=who["registered"],
                  date_of_birth="1970-01-01", reason="r", user={"id": lab_tech[0],
                                                                  "role": "lab_technician"})
    assert _queue_row(held["ingestion_event_id"])["status"] == "open"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM observations WHERE patient_id = ?",
                          (p["id"],)).fetchone()[0] == 0


def test_discard_needs_a_reason_and_is_audited(held, lab_tech):
    uid, hdr = lab_tech
    url = f"/api/ingest/quarantine/{held['id']}/discard"
    assert client.post(url, headers=hdr, json={"reason": " "}).status_code == 422
    assert client.post(url, headers=hdr, json={}).status_code == 422
    resp = client.post(url, headers=hdr, json={"reason": "Not a patient of this hospital"})
    assert resp.status_code == 200, resp.text
    row = _queue_row(held["ingestion_event_id"])
    assert row["status"] == "discarded" and row["resolved_by"] == uid
    assert row["attached_patient_id"] is None
    audits = _audits("lab_quarantine_discarded", str(held["id"]))
    assert len(audits) == 1 and audits[0]["event_category"] == "clinical_access"


# ── 5. The identity rules, unit by unit ──────────────────────────────────────

def test_identity_checks_rules():
    record = {"id": "P1", "mrn": "MRN-1", "name": "Ravi Kumar Sharma", "date_of_birth": "1970-01-01"}
    message = {"identifiers": ["EXT-1"], "name": "SHARMA, RAVI KUMAR", "dob": "1970-01-01"}
    checks = rq.identity_checks(message, record, {
        "hospital_number": "mrn-1", "name": "Sharma Ravi Kumar", "date_of_birth": "1970-01-01"})
    assert checks == {"hospital_number": "differs_in_message", "name": "match",
                      "date_of_birth": "match"}
    assert rq.identity_confirmed(checks)
    # A missing middle name is a different name: strict.
    assert rq.identity_checks(message, record, {"hospital_number": "MRN-1", "name": "Ravi Sharma"}
                              )["name"] == "no_match"
    # The message has no date of birth: it cannot count.
    no_dob = rq.identity_checks({**message, "dob": None}, record, {
        "hospital_number": "MRN-1", "name": "Ravi Kumar Sharma", "date_of_birth": "1970-01-01"})
    assert no_dob["date_of_birth"] == "not_in_message" and not rq.identity_confirmed(no_dob)
    # No patient at all.
    assert set(rq.identity_checks(message, None, {"hospital_number": "X", "name": "A",
                                                  "date_of_birth": "1970-01-01"}).values()) == {"no_match"}
    # A number in the message names somebody else: refused, even with every
    # other kind matching and the typed number in the message too.
    other = rq.identity_checks({**message, "identifiers": ["MRN-1", "EXT-1"]}, record, {
        "hospital_number": "MRN-1", "name": "Ravi Kumar Sharma", "date_of_birth": "1970-01-01"},
        message_names_another=True)
    assert other == {"hospital_number": "names_another_patient", "name": "match",
                     "date_of_birth": "match"}
    assert not rq.identity_confirmed(other)
    # Name and date of birth shared with another record: both read shared and
    # confirm nobody, unless a number in the message confirms the patient.
    typed = {"hospital_number": "MRN-1", "name": "Ravi Kumar Sharma", "date_of_birth": "1970-01-01"}
    shared = rq.identity_checks(message, record, typed, name_and_dob_shared=True)
    assert shared == {"hospital_number": "differs_in_message",
                      "name": "shared_with_another_patient",
                      "date_of_birth": "shared_with_another_patient"}
    assert not rq.identity_confirmed(shared)
    by_number = rq.identity_checks({**message, "identifiers": ["MRN-1"]}, record, typed,
                                   name_and_dob_shared=True)
    assert by_number == {"hospital_number": "match", "name": "match", "date_of_birth": "match"}
    assert rq.identity_confirmed(by_number)


def test_the_hl7_identity_reads_every_identifier_and_drops_name_prefixes():
    from services.vitals_source import hl7v2
    msg = hl7v2.parse(_oru("EXT-1", name="SHARMA^RAVI^K^JR^MR^MD", dob="19700101123000",
                           extra_ids="MRN-9^^^HOSP^MR"))
    identity = hl7_patient_identity(msg.first("PID"))
    assert identity == {"identifiers": ["EXT-1", "MRN-9"], "name": "SHARMA, RAVI K",
                        "dob": "1970-01-01", "sex": "M"}
    bad = hl7v2.parse(_oru("EXT-1", dob="19701345"))
    assert hl7_patient_identity(bad.first("PID"))["dob"] is None


# ── 6. The sweep of results stored with no patient ───────────────────────────

def test_the_sweep_copies_each_unlinked_result_once_and_attach_reuses_the_row(monkeypatch, lab_tech):
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", False)
    assert rq.sweep_unlinked() == 0, "the sweep does nothing while the switch is off"
    who = _person()
    res = _ingest(_oru(f"OLD{uuid.uuid4().hex[:6].upper()}", value="6.2", name=who["hl7"]))
    with db_session() as db:
        obs_id = db.execute("SELECT id FROM observations WHERE source_event_id = ?",
                            (res.event_id,)).fetchone()["id"]
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", True)
    assert rq.sweep_unlinked() >= 1
    assert rq.sweep_unlinked() == 0, "idempotent"
    with db_session() as db:
        row = dict(db.execute("SELECT * FROM result_quarantine WHERE source_observation_id = ?",
                              (obs_id,)).fetchone())
        assert db.execute("SELECT COUNT(*) FROM result_quarantine WHERE source_observation_id = ?",
                          (obs_id,)).fetchone()[0] == 1
    assert row["raw_patient_name"] == who["message"] and row["raw_patient_dob"] == "1970-01-01"
    p = _patient(name=who["registered"])
    out = rq.attach(row["id"], hospital_number=p["mrn"], name=who["registered"],
                    date_of_birth="1970-01-01", reason="Swept result, form checked",
                    user={"id": lab_tech[0], "role": "lab_technician"})
    assert out["attached"] and out["observation_id"] == obs_id
    with db_session() as db:
        assert db.execute("SELECT patient_id FROM observations WHERE id = ?",
                          (obs_id,)).fetchone()["patient_id"] == p["id"]
        assert db.execute("SELECT COUNT(*) FROM observations WHERE source_event_id = ?",
                          (res.event_id,)).fetchone()[0] == 1, "no second copy"
    assert rq.sweep_unlinked() == 0, "an attached row is never swept again"


# ── 7. Nobody to work the queue ──────────────────────────────────────────────

def test_compliance_fails_while_on_with_no_one_able_to_reconcile(queue_on, monkeypatch):
    # The laboratory module is off, so laboratory accounts cannot work it.
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    control = rq.compliance_control()
    assert control["id"] == "result_quarantine" and control["status"] == "fail"
    report = compliance_service.compliance_report()
    assert any(c["id"] == "result_quarantine" and c["status"] == "fail"
               for c in report["controls"])


def test_compliance_counts_only_active_reconcilers_with_their_module_on(queue_on, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    with db_session() as db:
        before = rq.active_reconciler_count(db)
    _user("pathologist", active=0)
    with db_session() as db:
        assert rq.active_reconciler_count(db) == before, "an inactive account cannot decide"
    _user("lab_technician")
    with db_session() as db:
        assert rq.active_reconciler_count(db) == before + 1
    assert rq.compliance_control()["status"] in ("pass", "warn")


def test_compliance_is_silent_when_off_and_empty(monkeypatch):
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", False)
    monkeypatch.setattr(rq, "_counts", lambda db: {"open": 0, "attached": 0, "discarded": 0,
                                                   "oldest_open_at": None})
    assert rq.compliance_control() is None


def test_the_boot_check_says_so_when_nobody_can_reconcile(queue_on, monkeypatch, caplog):
    monkeypatch.setattr(config, "LAB_ENABLED", False)
    with caplog.at_level(logging.WARNING, logger="orb.result_quarantine"):
        summary = rq.boot_check()
    assert summary["on"] is True and summary["reconcilers"] == 0
    assert any("no active account can attach or discard" in r.getMessage() for r in caplog.records)


def test_the_boot_check_does_nothing_with_the_switch_off(monkeypatch):
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", False)
    assert rq.boot_check() == {"on": False, "swept": 0, "reconcilers": None}


def test_the_boot_check_is_registered_for_every_module_state():
    from services import lab_module
    entry = [e for e in lab_module._LOOPS if e[0] == "result-quarantine-boot"]
    assert entry and set(entry[0][2]) == set(config.MODULE_STATES)
