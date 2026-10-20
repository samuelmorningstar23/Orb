"""Ingestion sources bound to a facility (multisite-16, W10-B, 20 October 2026).

Ruling MS-R14 (a), built strictest: once more than one facility is on record,
every ingestion source files only to its own facility's patients. The REST
key that authenticated a request binds it (ORB_INGESTION_API_KEY stays facility
1's; ORB_INGESTION_API_KEY_<CODE> is that facility's), and the bedside monitor
feed is bound by its source name in site.toml [[facilities]] ingestion_sources.
An unbound source, or an identifier that names a patient at another facility,
files nothing: the reading takes today's unknown-patient path and an
authorization 'ingestion_source_unbound' row is written. The operations status
page lists such sources. This file pins:

  * an F2 source sending an identifier equal to an F1 hospital number files
    nothing and writes the audit row; the same identifier from an F1 source
    files to the F1 patient;
  * an unbound source takes the unknown-patient path; so do a key set for two
    facilities, a source bound to a closed facility and an identifier two
    facilities' records answer to;
  * the chart's own manual lane needs no binding;
  * the audit row is throttled per source, cause and patient, with a count;
  * with one facility, the existing key, a facility key and the monitor feed
    all file exactly as before, with no audit row;
  * new vitals rows still carry the canonical UTC timestamp and carried_from.

TEST RULE (contracts, W7-J): the second facility is removed in teardown and
facility_service.invalidate_cache() called. HTTP tests open the fence. Run
with the test env, never the shipping .env.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import config
import facility_fence as F
from database import db_session
from main import app
from services import facility_service as FS
from services import ingestion_service as IS
from services import ops_health_service
from services.site_config_service import load_site_config

client = TestClient(app)

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9321                      # an id no other module writes
F2_CODE = f"FI{TAG}"
F2_KEY = f"satellite-key-{uuid.uuid4().hex}"
F2_KEY_ENV = f"{config.INGESTION_FACILITY_KEY_PREFIX}{F2_CODE}"
MAIN_KEY = str(config.INGESTION_API_KEY)
DEPT_F1 = f"Zelmora Medicine {TAG}"
DEPT_F2 = f"Kestrine Cardiology {TAG}"
F2_FEED = f"kestrine-icu-{TAG.lower()}"


def _mk_patient(db, label, department, facility):
    pid = f"FIB{uuid.uuid4().hex[:8].upper()}"
    mrn = f"MRN-FIB{uuid.uuid4().hex[:8].upper()}"
    db.execute(
        "INSERT INTO patients (id, name, mrn, status, registered_facility_id) "
        "VALUES (?, ?, ?, 'admitted', ?)", (pid, f"Orvanne {label}", mrn, facility))
    db.execute(
        "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, status, "
        "facility_id, created_at) VALUES (?, ?, 'system', ?, ?, 'active', ?, "
        "'2026-10-01 08:00:00')", (f"{pid}-A", pid, department, department, facility))
    return pid, mrn


def _put_back_facility_two():
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Kestrine Satellite"))
    FS.invalidate_cache()


@pytest.fixture(scope="module")
def world():
    with db_session() as db:
        for name, fid in ((DEPT_F1, 1), (DEPT_F2, F2)):
            db.execute("INSERT INTO departments (name, facility_id, seeded_from) "
                       "VALUES (?, ?, 'test')", (name, fid))
        p1, mrn1 = _mk_patient(db, "Ferrick", DEPT_F1, 1)
        p2, mrn2 = _mk_patient(db, "Talvette", DEPT_F2, F2)
        # Two records the same identifier answers to, one at each facility.
        pa, mrn_a = _mk_patient(db, "Ambrosine", DEPT_F2, F2)
        pb = f"FIB{uuid.uuid4().hex[:8].upper()}"
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pb, "Orvanne Ambroselle", mrn_a.lower()))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status, "
                   "facility_id) VALUES (?, ?, 'system', ?, 'active', 1)",
                   (f"{pb}-A", pb, DEPT_F1))
    _put_back_facility_two()
    pids = [p1, p2, pa, pb]
    try:
        yield {"p1": p1, "mrn1": mrn1, "p2": p2, "mrn2": mrn2, "pa": pa, "mrn_a": mrn_a,
               "pb": pb}
    finally:
        with db_session() as db:
            db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
            db.execute("DELETE FROM departments WHERE name LIKE ?", (f"%{TAG}%",))
            db.executemany("UPDATE admissions SET facility_id = 1, status = 'discharged' "
                           "WHERE patient_id = ?", [(p,) for p in pids])
            db.executemany("UPDATE patients SET registered_facility_id = 1, "
                           "status = 'discharged' WHERE id = ?", [(p,) for p in pids])
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    FS.invalidate_cache()
    IS.reset_source_refusals()
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    monkeypatch.setenv(F2_KEY_ENV, F2_KEY)
    yield
    IS.reset_source_refusals()
    FS.invalidate_cache()
    load_site_config(tmp_path / "no-such-site.toml", apply_env=False)


@pytest.fixture()
def one_facility(world):
    with db_session() as db:
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        yield
    finally:
        _put_back_facility_two()


@pytest.fixture()
def site_binds_feed(tmp_path):
    path = tmp_path / "site.toml"
    path.write_text(f"""
[site]
name = "Zelmora Hospital"

[[facilities]]
code = "MAIN"
ingestion_sources = ["zelmora-ward-feed"]

[[facilities]]
code = "{F2_CODE}"
ingestion_sources = ["{F2_FEED}"]
""", encoding="utf-8")
    load_site_config(path, apply_env=False)


_SEQ = [0]


def _vitals_payload(identifier, *, hr=88, full=True):
    _SEQ[0] += 1
    at = (datetime.now(timezone.utc) - timedelta(minutes=30) + timedelta(seconds=_SEQ[0]))
    obs = [{"code": "8867-4", "name": "Heart Rate", "value": hr, "unit": "bpm"}]
    if full:
        obs += [{"code": "8480-6", "name": "Systolic Blood Pressure", "value": 124,
                 "unit": "mmHg"},
                {"code": "9279-1", "name": "Respiratory Rate", "value": 16, "unit": "/min"}]
    return json.dumps({"patient_id": identifier, "observed_at": at.isoformat(),
                       "message_id": f"fib-{uuid.uuid4().hex}", "observations": obs})


def _lab_payload(identifier):
    return json.dumps({"patient_id": identifier, "message_id": f"fib-{uuid.uuid4().hex}",
                       "observations": [{"code": "2345-7", "name": "Glucose", "value": 5.4,
                                         "unit": "mmol/L"}]})


def _post(path_key, payload, key):
    return client.post("/api/ingest/json", content=payload, headers={
        "X-API-Key": key, "Content-Type": "application/json", "X-Source-Name": path_key})


def _assert_unknown_patient_answer(resp):
    """Today's answer for vitals whose identifier names nobody: the request
    is accepted (200) and the body says it failed, with no row written."""
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "error" and body["parse_status"] == "failed", body
    assert body["counts"]["inserted_vitals"] == 0, body


def _vitals_count(pid) -> int:
    with db_session() as db:
        return int(db.execute("SELECT COUNT(*) FROM vitals WHERE patient_id = ?",
                              (pid,)).fetchone()[0])


def _last_audit_id() -> int:
    with db_session() as db:
        return int(db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0])


def _unbound_rows(after: int) -> list[dict]:
    with db_session() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE id > ? AND action_type = 'ingestion_source_unbound' "
            "ORDER BY id", (after,)).fetchall()]
    for row in rows:
        row["meta"] = json.loads(row["metadata_json"] or "{}")
    return rows


def _ingest(payload, **kwargs):
    from services.ingestion_adapters import JSONAdapter
    return asyncio.run(IS.ingest_payload(raw_payload=payload, adapter=JSONAdapter(),
                                         content_type="application/json", **kwargs))


# ═════════════════════════════════════════════════════════════════════════════
#  1. REST keys bind a facility
# ═════════════════════════════════════════════════════════════════════════════

def test_an_f2_key_sending_an_f1_number_files_nothing_and_is_audited(world):
    before = _vitals_count(world["p1"])
    after = _last_audit_id()
    resp = _post("satellite-gateway", _vitals_payload(world["mrn1"]), F2_KEY)
    _assert_unknown_patient_answer(resp)
    # Exactly the words an identifier that names nobody gets: nothing disclosed.
    assert resp.json()["errors"] == [
        f"Vitals insert error for {world['mrn1']}: Patient not found for identifier: "
        f"{world['mrn1']}"]
    assert _vitals_count(world["p1"]) == before
    rows = _unbound_rows(after)
    assert len(rows) == 1
    row = rows[0]
    assert row["event_category"] == "authorization" and row["outcome"] == "denied"
    assert row["patient_id"] == world["p1"]
    assert row["target_type"] == "ingestion_source" and row["target_id"] == "satellite-gateway"
    assert row["meta"]["cause"] == "identifier_at_another_facility"
    assert row["meta"]["source_facility_id"] == F2
    assert row["meta"]["bound_by"] == "ingestion_key"
    assert row["meta"]["other_facility_ids"] == [1]


def test_the_same_number_from_an_f1_source_files_to_the_f1_patient(world):
    before = _vitals_count(world["p1"])
    after = _last_audit_id()
    resp = _post("main-gateway", _vitals_payload(world["mrn1"]), MAIN_KEY)
    assert resp.status_code == 200, resp.text
    assert resp.json()["counts"]["inserted_vitals"] == 1
    assert _vitals_count(world["p1"]) == before + 1
    assert _unbound_rows(after) == []


def test_the_f2_key_files_to_its_own_patients(world):
    before = _vitals_count(world["p2"])
    resp = _post("satellite-gateway", _vitals_payload(world["mrn2"]), F2_KEY)
    assert resp.status_code == 200, resp.text
    assert _vitals_count(world["p2"]) == before + 1


def test_a_key_set_for_two_facilities_binds_none(world, monkeypatch):
    monkeypatch.setenv(F2_KEY_ENV, MAIN_KEY)
    after = _last_audit_id()
    before = _vitals_count(world["p1"])
    resp = _post("shared-gateway", _vitals_payload(world["mrn1"]), MAIN_KEY)
    _assert_unknown_patient_answer(resp)
    assert _vitals_count(world["p1"]) == before
    rows = _unbound_rows(after)
    assert [r["meta"]["cause"] for r in rows] == ["key_shared"]


def test_a_wrong_key_is_still_401(world):
    assert _post("x", _vitals_payload(world["mrn1"]), "not-a-key").status_code == 401
    assert client.post("/api/ingest/json", content=_vitals_payload(world["mrn1"]),
                       headers={"Content-Type": "application/json"}).status_code == 401


def test_a_laboratory_result_for_another_facility_is_kept_unlinked(world, monkeypatch):
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", False, raising=False)
    after = _last_audit_id()
    resp = _post("satellite-lab", _lab_payload(world["mrn1"]), F2_KEY)
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        linked = db.execute(
            "SELECT COUNT(*) FROM observations WHERE source_event_id = ? AND patient_id = ?",
            (resp.json()["event_id"], world["p1"])).fetchone()[0]
        unlinked = db.execute(
            "SELECT COUNT(*) FROM observations WHERE source_event_id = ? AND patient_id IS NULL",
            (resp.json()["event_id"],)).fetchone()[0]
    assert linked == 0 and unlinked == 1
    rows = _unbound_rows(after)
    assert len(rows) == 1 and rows[0]["meta"]["cause"] == "identifier_at_another_facility"


# ═════════════════════════════════════════════════════════════════════════════
#  2. The monitor feed is bound by site.toml
# ═════════════════════════════════════════════════════════════════════════════

def test_a_feed_listed_under_f2_files_to_f2_only(world, site_binds_feed):
    before2, before1 = _vitals_count(world["p2"]), _vitals_count(world["p1"])
    ok = _ingest(_vitals_payload(world["mrn2"]), source_name=F2_FEED, source_label="monitor_hl7")
    assert ok.inserted_vitals == 1 and _vitals_count(world["p2"]) == before2 + 1
    after = _last_audit_id()
    refused = _ingest(_vitals_payload(world["mrn1"]), source_name=F2_FEED,
                      source_label="monitor_hl7")
    assert refused.inserted_vitals == 0 and refused.parse_status == "failed"
    assert _vitals_count(world["p1"]) == before1
    rows = _unbound_rows(after)
    assert len(rows) == 1 and rows[0]["meta"]["bound_by"] == "site_config"
    assert rows[0]["meta"]["source_facility_code"] == F2_CODE


def test_an_unbound_feed_takes_the_unknown_patient_path(world, site_binds_feed):
    before = _vitals_count(world["p1"])
    after = _last_audit_id()
    result = _ingest(_vitals_payload(world["mrn1"]), source_name="unlisted-bay-monitor",
                     source_label="monitor_hl7")
    assert result.inserted_vitals == 0
    assert result.errors == [
        f"Vitals insert error for {world['mrn1']}: Patient not found for identifier: "
        f"{world['mrn1']}"]
    assert _vitals_count(world["p1"]) == before
    rows = _unbound_rows(after)
    assert len(rows) == 1
    assert rows[0]["meta"]["cause"] == "source_not_listed"
    assert rows[0]["meta"]["source_facility_id"] is None
    assert rows[0]["patient_id"] == world["p1"]


def test_a_feed_bound_to_a_closed_facility_files_nothing(world, site_binds_feed):
    with db_session() as db:
        db.execute("UPDATE facilities SET is_active = 0 WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        before = _vitals_count(world["p2"])
        after = _last_audit_id()
        result = _ingest(_vitals_payload(world["mrn2"]), source_name=F2_FEED,
                         source_label="monitor_hl7")
        assert result.inserted_vitals == 0 and _vitals_count(world["p2"]) == before
        assert [r["meta"]["cause"] for r in _unbound_rows(after)] == ["facility_inactive"]
    finally:
        with db_session() as db:
            db.execute("UPDATE facilities SET is_active = 1 WHERE id = ?", (F2,))
        FS.invalidate_cache()


def test_an_identifier_two_facilities_answer_to_is_refused(world):
    # The F2 record's number exactly, and an F1 record's in other capitals.
    before = _vitals_count(world["pa"])
    after = _last_audit_id()
    resp = _post("satellite-gateway", _vitals_payload(world["mrn_a"]), F2_KEY)
    _assert_unknown_patient_answer(resp)
    assert _vitals_count(world["pa"]) == before and _vitals_count(world["pb"]) == 0
    rows = _unbound_rows(after)
    assert len(rows) == 1 and rows[0]["meta"]["cause"] == "identifier_at_another_facility"
    assert rows[0]["meta"]["other_facility_ids"] == [1]


def test_the_charts_manual_lane_needs_no_binding(world):
    before = _vitals_count(world["p2"])
    after = _last_audit_id()
    result = _ingest(_vitals_payload(world["p2"]), source_name="manual",
                     source_label="manual_entry")
    assert result.inserted_vitals == 1 and _vitals_count(world["p2"]) == before + 1
    assert _unbound_rows(after) == []


def test_the_resolver_takes_the_facility(world):
    with db_session() as db:
        assert IS._resolve_patient_id(db, world["mrn1"]) == world["p1"]
        assert IS._resolve_patient_id(db, world["mrn1"], 1) == world["p1"]
        assert IS._resolve_patient_id(db, world["mrn1"], F2) is None
        assert IS._resolve_patient_id(db, world["mrn2"], F2) == world["p2"]
        assert IS._resolve_patient_id(db, world["p2"], F2) == world["p2"]
        assert IS._resolve_patient_id(db, world["mrn2"], None) is None
        assert IS._resolve_patient_id(db, world["mrn_a"], F2) is None


# ═════════════════════════════════════════════════════════════════════════════
#  3. The audit row is throttled, the status page lists the sources
# ═════════════════════════════════════════════════════════════════════════════

def test_the_audit_row_is_throttled_and_counts_what_it_skipped(world, monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(IS, "_refusal_clock", lambda: now[0])
    after = _last_audit_id()
    for _ in range(3):
        _post("satellite-gateway", _vitals_payload(world["mrn1"]), F2_KEY)
    assert len(_unbound_rows(after)) == 1
    now[0] += IS.SOURCE_REFUSAL_AUDIT_EVERY_SECONDS + 1
    _post("satellite-gateway", _vitals_payload(world["mrn1"]), F2_KEY)
    rows = _unbound_rows(after)
    assert len(rows) == 2 and rows[1]["meta"]["refusals_since_last_row"] == 2
    seen = IS.source_refusals()
    assert seen == [{"source_name": "satellite-gateway",
                     "cause": "identifier_at_another_facility",
                     "words": IS.SOURCE_REFUSAL_WORDS["identifier_at_another_facility"],
                     "refusals": 4, "last_at": seen[0]["last_at"]}]


def _facilities_check() -> dict:
    payload = ops_health_service.build_status()
    return next(c for c in payload["checks"] if c["key"] == "facilities")


def test_the_status_page_lists_unbound_sources(world, monkeypatch, site_binds_feed):
    monkeypatch.setenv("ORB_MONITOR_ENABLED", "1")
    monkeypatch.setenv("ORB_MONITOR_SOURCE_ID", "unlisted-bay-monitor")
    monkeypatch.setenv(f"{config.INGESTION_FACILITY_KEY_PREFIX}NOWHERE{TAG[:4]}", "orphan-key")
    _post("satellite-gateway", _vitals_payload(world["mrn1"]), F2_KEY)
    check = _facilities_check()
    sources = check["detail"]["ingestion_sources"]
    assert sources["monitor_feed"] == {
        "enabled": True, "source_name": "unlisted-bay-monitor", "facility_code": None,
        "bound": False, "cause": "source_not_listed"}
    assert any("unlisted-bay-monitor" in u for u in sources["unbound"])
    assert any(f"NOWHERE{TAG[:4]}" in u for u in sources["unbound"])
    codes = {k["code"]: k for k in sources["facility_keys"]}
    assert codes[F2_CODE]["on_record"] is True and codes[F2_CODE]["shared"] is False
    assert sources["refused"][0]["source_name"] == "satellite-gateway"
    text = " ".join([check["summary"], *check["notes"]])
    assert "unlisted-bay-monitor" in text
    assert world["p1"] not in json.dumps(check) and world["mrn1"] not in json.dumps(check)
    assert F2_KEY not in json.dumps(check) and "orphan-key" not in json.dumps(check)


# ═════════════════════════════════════════════════════════════════════════════
#  4. With one facility nothing changes
# ═════════════════════════════════════════════════════════════════════════════

def test_with_one_facility_every_key_files_as_before(world, one_facility):
    after = _last_audit_id()
    before = _vitals_count(world["p1"])
    assert _post("main-gateway", _vitals_payload(world["mrn1"]), MAIN_KEY).status_code == 200
    # A facility key files across the appliance, as every key did.
    assert _post("satellite-gateway", _vitals_payload(world["mrn1"]), F2_KEY).status_code == 200
    assert _vitals_count(world["p1"]) == before + 2
    unbound = _ingest(_vitals_payload(world["mrn1"]), source_name="unlisted-bay-monitor",
                      source_label="monitor_hl7")
    assert unbound.inserted_vitals == 1
    assert _unbound_rows(after) == []
    assert IS.source_refusals() == []


def test_with_one_facility_the_source_lookup_reads_nothing(world, one_facility, monkeypatch):
    FS.multi_facility_data()            # warm the cache
    hops = []
    real = IS.run_db
    monkeypatch.setattr(IS, "run_db", lambda fn, *a, **k: hops.append(fn) or real(fn, *a, **k))
    got = asyncio.run(IS._source_facility(None, "unlisted-bay-monitor", "monitor_hl7"))
    assert got is IS.NO_FACILITY_LIMIT and hops == []


# ═════════════════════════════════════════════════════════════════════════════
#  5. The vitals INSERT path is untouched
# ═════════════════════════════════════════════════════════════════════════════

def test_new_vitals_rows_keep_the_canonical_time_and_carried_from(world):
    from services.clinical_time import CANONICAL_GLOB
    assert _post("satellite-gateway", _vitals_payload(world["p2"]), F2_KEY).status_code == 200
    assert _post("satellite-gateway", _vitals_payload(world["p2"], hr=101, full=False),
                 F2_KEY).status_code == 200
    with db_session() as db:
        row = db.execute(
            "SELECT timestamp, heart_rate, systolic_bp, carried_from FROM vitals "
            "WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (world["p2"],)).fetchone()
        canonical = db.execute("SELECT ? GLOB ?", (row["timestamp"], CANONICAL_GLOB)).fetchone()[0]
    assert canonical == 1, row["timestamp"]
    assert row["heart_rate"] == 101 and row["systolic_bp"] == 124
    assert "systolic_bp" in json.loads(row["carried_from"])
