"""Adoption of existing chart drugs into the medication-order store, and the
parity report (medorders-2, W4-E).

Pinned here:
  * equivalence: for every chart status, source and verifier, the bedside
    verdict before adoption equals the verdict after it (the ward row's own
    checks AND order_allows_dose), and the ward row is left exactly as it was;
  * a pharmacist-rejected AI drug adopts as rejected, an add-form
    'discontinued' drug as stopped;
  * a second run changes nothing; batches of the given size, one
    clinical_access audit row per batch;
  * a Sage pair adopts into one order and leaves its pharmacy row unlinked,
    which the parity report lists; an unreadable status and a broken link are
    never guessed at, only reported;
  * adoption is refused while the store switch is off; the boot hook does
    nothing then, never raises, and is what the lifespan calls;
  * a GET of the patient list with the store on writes nothing to the store
    and leaves no write lock behind;
  * scripts/medorder_parity.py on a scratch database: report, refusal, adopt.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from database import db_session
from services.medorders import adoption, core

SERVER_DIR = Path(__file__).resolve().parent.parent
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_store_rows():
    """Take back everything this module put in the store, so no later module
    meets links it did not make."""
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL WHERE patient_id IN ({marks})",
                   _PATIENTS)
        db.execute(f"UPDATE helix_patient_med_orders SET med_order_id = NULL "
                   f"WHERE patient_id IN ({marks})", _PATIENTS)


def _patient():
    pid = f"PMOAD{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?, ?, ?)",
                   (pid, "Adoption Test", f"MRN-{uuid.uuid4().hex[:8]}"))
    _PATIENTS.append(pid)
    return pid


@pytest.fixture()
def patient():
    return _patient()


@pytest.fixture()
def store_on(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)


def _chart(patient_id, *, status="active", source="clinician", verified_by=None,
           drug="Paracetamol", dosage="500 mg"):
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source, verified_by, verified_at) VALUES (?, ?, ?, 'Oral', 'TDS', ?, "
            "'D001', ?, ?, ?)",
            (patient_id, drug, dosage, status, source, verified_by,
             "2026-10-04 08:00:00" if verified_by else None))
        return cur.lastrowid


def _ward_row(med_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone())


def _order_for(med_id):
    with db_session() as db:
        row = db.execute("SELECT o.* FROM med_orders o JOIN medications m "
                         "ON m.med_order_id = o.id WHERE m.id = ?", (med_id,)).fetchone()
        return dict(row) if row else None


def _adopt(*patients, **kw):
    return adoption.adopt_legacy(trigger="admin", patient_ids=list(patients), **kw)


def _ward_check_allows(row) -> bool:
    """The ward row's own eMAR checks for a 'given' dose, exactly as
    emar_service.record_administration reads them today (active status; an AI
    source needs a pharmacist). test_the_mirror_agrees_with_the_real_emar keeps
    this honest against the real service."""
    if row["status"] != "active":
        return False
    if row["source"] in core.UNVERIFIED_SOURCES and row["verified_by"] is None:
        return False
    return True


def _real_emar_allows(med_id) -> bool:
    from services import emar_service
    try:
        emar_service.record_administration(med_id, "N001", status="given",
                                           idempotency_key=f"adopt-{uuid.uuid4().hex}")
        return True
    except ValueError:
        return False


COMBOS = list(itertools.product(("active", "prn", "discontinued"),
                                ("clinician", "llm_extracted", "agentic"),
                                (None, "PH1")))


# ── 1. Equivalence ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,source,verified_by", COMBOS)
def test_the_bedside_verdict_is_the_same_before_and_after_adoption(store_on, status, source,
                                                                   verified_by):
    pid = _patient()
    med_id = _chart(pid, status=status, source=source, verified_by=verified_by)
    row_before = _ward_row(med_id)
    before = _ward_check_allows(row_before)
    assert _real_emar_allows(med_id) is before

    result = _adopt(pid)
    assert result["adopted"] == 1

    row_after = _ward_row(med_id)
    order = _order_for(med_id)
    assert order is not None and row_after["med_order_id"] == order["id"]
    assert {k: v for k, v in row_after.items() if k != "med_order_id"} == \
        {k: v for k, v in row_before.items() if k != "med_order_id"}, \
        "adoption changed the ward row"
    after = _ward_check_allows(row_after) and core.order_allows_dose(order, "given")["allowed"]
    assert after is before, (status, source, verified_by, order["status"])
    # The mapping, as the design states it.
    mapping = core.legacy_status_for(row_before)
    assert order["status"] == mapping["status"]
    assert order["needs_verification"] == (1 if source in core.UNVERIFIED_SOURCES else 0)
    assert order["source"] == "legacy_adopted" and order["legacy_medication_id"] == med_id
    assert order["projection_status"] == (status if status != "discontinued" else None)
    # Not given is always recordable on an order adoption made live.
    if order["status"] in core.LIVE_STATUSES:
        assert core.order_allows_dose(order, "not_given")["allowed"] is True


def test_the_mirror_agrees_with_the_real_emar(patient):
    """The bedside service itself, for the rows whose answer it gives today."""
    for status, source, verified_by in COMBOS:
        med_id = _chart(patient, status=status, source=source, verified_by=verified_by,
                        drug="Ondansetron", dosage="4 mg")
        assert _real_emar_allows(med_id) is _ward_check_allows(_ward_row(med_id)), (
            status, source, verified_by)


def test_a_pharmacist_rejected_ai_drug_adopts_as_rejected(store_on, patient):
    from services import pharmacy_verification_service as pharmacy
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('PHADOPT', 'Pharm Adopt', 'pharmacist', 'x')")
    med_id = _chart(patient, source="llm_extracted", dosage="REVIEW REQUIRED")
    pharmacy.reject_medication(med_id, "PHADOPT", "Wrong drug for this indication")
    _adopt(patient)
    order = _order_for(med_id)
    assert order["status"] == "rejected"
    assert order["status_reason"] == core.LEGACY_REJECTED_REASON
    assert order["verified_by"] == "PHADOPT" and order["projection_status"] is None
    assert _ward_row(med_id)["status"] == "discontinued"


def test_an_add_form_discontinued_drug_adopts_as_stopped(store_on, patient):
    med_id = _chart(patient, status="discontinued")
    _adopt(patient)
    order = _order_for(med_id)
    assert order["status"] == "stopped"
    assert order["status_reason"] == core.LEGACY_STOPPED_REASON
    events = _events_for(order["id"])
    assert [e["kind"] for e in events] == ["adopted"]
    payload = json.loads(events[0]["payload_json"])
    assert payload["legacy_status"] == "discontinued" and payload["legacy_source"] == "clinician"


def _events_for(order_id):
    with db_session() as db:
        return core.events_in_txn(db, order_id)


def _store_counts(pid):
    with db_session() as db:
        return (
            db.execute("SELECT COUNT(*) FROM med_orders WHERE patient_id = ?", (pid,)).fetchone()[0],
            db.execute("SELECT COUNT(*) FROM med_order_events WHERE patient_id = ?",
                       (pid,)).fetchone()[0],
            db.execute("SELECT COUNT(*) FROM medications WHERE patient_id = ? "
                       "AND med_order_id IS NOT NULL", (pid,)).fetchone()[0],
        )


def _adoption_audits():
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'med_orders_adopted' ORDER BY id")]


# ── 2. Idempotence, batches and the audit ─────────────────────────────────────

def test_a_second_run_changes_nothing(store_on, patient):
    for status in ("active", "prn", "discontinued"):
        _chart(patient, status=status)
    first = _adopt(patient)
    assert first["adopted"] == 3
    counts, audits = _store_counts(patient), len(_adoption_audits())
    again = _adopt(patient)
    assert again["adopted"] == 0
    assert _store_counts(patient) == counts == (3, 3, 3)
    assert len(_adoption_audits()) == audits


def test_one_audit_row_per_batch(store_on, patient):
    ids = [_chart(patient) for _ in range(5)]
    start = len(_adoption_audits())
    result = _adopt(patient, batch_size=2)
    assert (result["adopted"], result["batches"]) == (5, 3)
    rows = _adoption_audits()[start:]
    assert len(rows) == 3
    for row in rows:
        assert row["event_category"] == "clinical_access" and row["outcome"] == "success"
        assert isinstance(row["target_id"], str) and "-" in row["target_id"]
        assert json.loads(row["metadata_json"])["trigger"] == "admin"
    assert sum(json.loads(r["metadata_json"])["adopted"] for r in rows) == 5
    assert json.loads(rows[0]["metadata_json"])["first_medication_id"] == str(ids[0])
    # Adoption writes no per-order audit; the batch row is the record.
    with db_session() as db:
        per_order = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'med_order_adopted' "
            "AND patient_id = ?", (patient,)).fetchone()[0]
    assert per_order == 0


# ── 3. What adoption leaves to the parity report ──────────────────────────────

def _report():
    return adoption.parity_report(sample_limit=10 ** 6)


def _ids(section, key):
    return {item[key] for item in section["sample"]}


def test_a_sage_pair_adopts_one_order_and_leaves_its_pharmacy_row_for_the_report(store_on,
                                                                                  patient):
    med_id = _chart(patient, source="llm_extracted", dosage="REVIEW REQUIRED")
    helix_id = f"HX-{uuid.uuid4().hex[:10]}"
    with db_session() as db:
        db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, status, prescribed_by, "
                   "dosage) VALUES (?, ?, 'pending', 'D001', 'REVIEW REQUIRED')",
                   (helix_id, patient))
    _adopt(patient)
    assert _store_counts(patient)[0] == 1
    assert _order_for(med_id)["needs_verification"] == 1
    with db_session() as db:
        assert db.execute("SELECT med_order_id FROM helix_patient_med_orders WHERE id = ?",
                          (helix_id,)).fetchone()[0] is None
    section = _report()["informational"]["helix_rows_without_order"]
    assert helix_id in _ids(section, "helix_order_id")


def test_an_unreadable_status_is_never_guessed(store_on, patient):
    with db_session() as db:
        cur = db.execute("INSERT INTO medications (patient_id, drug_name, dosage, frequency, "
                         "status) VALUES (?, 'Aspirin', '75 mg', 'OD', NULL)", (patient,))
        med_id = cur.lastrowid
    result = _adopt(patient)
    assert (result["adopted"], result["unadoptable"]) == (0, 1)
    assert med_id in result["unadoptable_medication_ids"]
    assert _order_for(med_id) is None
    assert med_id in _ids(_report()["pending"]["unadoptable_rows"], "medication_id")
    assert _adopt(patient)["adopted"] == 0


def test_a_row_the_store_cannot_hold_never_blocks_the_rest(store_on, patient):
    blank = _chart(patient, drug="   ")
    good = _chart(patient)
    result = _adopt(patient)
    assert (result["adopted"], result["unadoptable"]) == (1, 1)
    assert blank in result["unadoptable_medication_ids"]
    assert _order_for(blank) is None and _order_for(good) is not None
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM med_order_events WHERE patient_id = ?",
                          (patient,)).fetchone()[0] == 1
    assert blank in _ids(_report()["pending"]["unadoptable_rows"], "medication_id")


def test_a_broken_link_is_reported_not_repaired(store_on, patient):
    med_id = _chart(patient)
    with db_session(immediate=True) as db:
        stray = core.create_in_txn(db, patient_id=patient, drug_name="Paracetamol",
                                   source="chart", status="prescribed", entered_by="D001",
                                   actor_id="D001", entry_point="test.stray",
                                   legacy_medication_id=med_id).order
    result = _adopt(patient)
    assert (result["adopted"], result["conflict"]) == (0, 1)
    assert _ward_row(med_id)["med_order_id"] is None
    broken = _report()["problems"]["broken_links"]
    assert stray["id"] in {b.get("med_order_id") for b in broken["sample"]}


def test_the_report_finds_a_ward_row_that_disagrees_with_its_order(store_on, patient):
    med_id = _chart(patient)
    _adopt(patient)
    order = _order_for(med_id)
    report = _report()
    assert order["id"] not in _ids(report["problems"]["projection_mismatches"], "med_order_id")
    with db_session() as db:
        db.execute("UPDATE medications SET status = 'discontinued' WHERE id = ?", (med_id,))
    report = _report()
    hit = [m for m in report["problems"]["projection_mismatches"]["sample"]
           if m["med_order_id"] == order["id"]]
    assert hit and hit[0]["fields"] == ["status"] and report["clean"] is False
    with db_session() as db:
        db.execute("UPDATE medications SET status = 'active' WHERE id = ?", (med_id,))


def test_the_report_finds_open_supply_on_an_ended_order(patient):
    with db_session(immediate=True) as db:
        ended = core.create_in_txn(db, patient_id=patient, drug_name="Ceftriaxone",
                                   source="chart", status="stopped", entered_by="D001",
                                   actor_id="D001", entry_point="test.ended",
                                   status_reason="course changed").order
    helix_id = f"HX-{uuid.uuid4().hex[:10]}"
    with db_session() as db:
        db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, status, prescribed_by, "
                   "med_order_id) VALUES (?, ?, 'scheduled', 'D001', ?)",
                   (helix_id, patient, ended["id"]))
    section = _report()["problems"]["terminal_orders_with_open_supply"]
    assert helix_id in _ids(section, "helix_order_id")


def test_the_report_lists_ordered_medicines_with_no_order(patient):
    cpoe_id = f"ORD-{uuid.uuid4().hex[:10]}"
    with db_session() as db:
        db.execute("INSERT INTO orders (id, patient_id, order_type, description, ordered_by) "
                   "VALUES (?, ?, 'medication', 'Metformin 500 mg BD', 'D001')",
                   (cpoe_id, patient))
    section = _report()["informational"]["cpoe_medication_rows_without_order"]
    assert cpoe_id in _ids(section, "cpoe_order_id")


def test_the_report_names_no_drug(store_on, patient):
    _chart(patient, drug="Uniquedrugnamezz")
    assert "Uniquedrugnamezz" not in json.dumps(_report())


# ── 4. The switch, the boot hook and the lifespan ─────────────────────────────

def test_adoption_is_refused_while_the_store_is_off(patient):
    med_id = _chart(patient)
    with pytest.raises(adoption.AdoptionRefused):
        _adopt(patient)
    assert _order_for(med_id) is None and _ward_row(med_id)["med_order_id"] is None


def test_the_boot_hook_does_nothing_while_the_store_is_off(monkeypatch):
    called = []
    monkeypatch.setattr(adoption, "adopt_legacy", lambda **k: called.append(k))
    assert adoption.boot_adoption() is None
    assert called == []


def test_the_boot_hook_adopts_with_the_store_on_and_never_raises(store_on, monkeypatch):
    called = []
    monkeypatch.setattr(adoption, "adopt_legacy",
                        lambda **k: called.append(k) or {"adopted": 0, "batches": 0,
                                                          "unadoptable": 0, "conflict": 0})
    assert adoption.boot_adoption()["adopted"] == 0
    assert called == [{"trigger": "boot"}]

    def boom(**k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(adoption, "adopt_legacy", boom)
    assert adoption.boot_adoption() == {"error": "adoption_failed"}


def test_the_lifespan_runs_the_boot_hook(store_on, monkeypatch):
    import main
    called = []
    monkeypatch.setattr(adoption, "adopt_legacy",
                        lambda **k: called.append(k) or {"adopted": 0, "batches": 0,
                                                          "unadoptable": 0, "conflict": 0})
    with TestClient(main.app) as client:
        assert client.get("/api/health").status_code == 200
    assert called == [{"trigger": "boot"}]


# ── 5. A GET never adopts ─────────────────────────────────────────────────────

def test_a_get_of_the_patient_list_writes_nothing_to_the_store(store_on, patient):
    import main
    from auth import create_token
    _chart(patient)
    with db_session() as db:
        def counts():
            return (
                db.execute("SELECT COUNT(*) FROM med_orders").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM med_order_events").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM medications WHERE med_order_id IS NOT NULL")
                .fetchone()[0],
            )
        before = counts()
    client = TestClient(main.app)  # no lifespan: only the request path runs
    headers = {"Authorization": f"Bearer {create_token('D001', 'doctor')}"}
    for path in ("/api/vigil/patients", f"/api/vigil/patients/{patient}"):
        resp = client.get(path, headers=headers)
        assert resp.status_code in (200, 404), (path, resp.text)
    with db_session() as db:
        after = (
            db.execute("SELECT COUNT(*) FROM med_orders").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM med_order_events").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM medications WHERE med_order_id IS NOT NULL")
            .fetchone()[0],
        )
    assert after == before
    # And no write lock was left behind: an immediate session starts at once.
    started = time.monotonic()
    with db_session(immediate=True) as db:
        db.execute("SELECT 1").fetchone()
    assert time.monotonic() - started < 2.0


# ── 6. The command line ───────────────────────────────────────────────────────

def _run(args, db_path, **env):
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "ORB_DEPLOYMENT_PROFILE": "dev", "ORB_DB_PATH": str(db_path),
            "ORB_JWT_SECRET": "suite-only-secret-0123456789abcdef0123456789"}
    return subprocess.run([sys.executable, *args], cwd=str(SERVER_DIR), env={**base, **env},
                          capture_output=True, text=True, timeout=300)


def test_the_parity_script_reports_refuses_and_adopts(tmp_path):
    db_path = tmp_path / "orb_parity_cli.db"
    seed = _run(["-c", """
from database import init_db, db_session
init_db()
with db_session() as db:
    db.execute("INSERT INTO patients (id, name, mrn) VALUES ('PCLI', 'Cli', 'MRN-CLI')")
    db.execute("INSERT INTO medications (patient_id, drug_name, dosage, frequency, status) "
               "VALUES ('PCLI', 'Aspirin', '75 mg', 'OD', 'active')")
    db.execute("INSERT INTO medications (patient_id, drug_name, dosage, frequency, status) "
               "VALUES ('PCLI', 'Paracetamol', '1 g', 'QDS', 'discontinued')")
print("seeded")
"""], db_path)
    assert seed.returncode == 0, seed.stderr

    report = _run(["scripts/medorder_parity.py", "--json"], db_path)
    assert report.returncode == 0, report.stderr
    body = json.loads(report.stdout)
    assert body["adopted"] is None
    assert body["report"]["pending"]["unlinked_live_rows"]["count"] == 1
    assert body["report"]["clean"] is True

    refused = _run(["scripts/medorder_parity.py", "--adopt"], db_path)
    assert refused.returncode == 2 and "switched off" in refused.stderr

    adopted = _run(["scripts/medorder_parity.py", "--adopt", "--json"], db_path,
                   ORB_MED_ORDER_STORE="1")
    assert adopted.returncode == 0, adopted.stderr
    body = json.loads(adopted.stdout)
    assert body["adopted"]["adopted"] == 2
    assert body["adopted"]["by_status"] == {"prescribed": 1, "stopped": 1}
    assert body["report"]["pending"]["unlinked_live_rows"]["count"] == 0
    assert body["report"]["orders"] == 2

    words = _run(["scripts/medorder_parity.py"], db_path)
    assert words.returncode == 0 and "Clean." in words.stdout
    assert "\u2014" not in words.stdout
    check = _run(["-c", """
import json
from database import db_session
with db_session() as db:
    print(json.dumps([r[0] for r in db.execute(
        "SELECT action_type FROM audit_logs WHERE action_type LIKE 'med_order%' ORDER BY id")]))
"""], db_path)
    actions = json.loads(check.stdout.strip().splitlines()[-1])
    assert actions.count("med_orders_adopted") == 1
    assert actions.count("med_order_parity_report") == 3
