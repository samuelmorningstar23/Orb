"""The pharmacist's check before the first dose (medorders-11, W8-I; ruling
MO-R1, a safety ruling held for Samuel).

Pinned here:
  * the switch: ORB_MED_ORDER_VERIFY_BEFORE_DOSE is off by default, is off
    whenever the lifecycle is, is on /api/health, and config refuses to load
    with it on and the lifecycle off; ORB_MED_ORDER_FIRST_DOSE_BEFORE_CHECK
    defaults to 'none' (the strictest option, (c)) and an unknown value reads
    as 'none';
  * switch off: a linked order no pharmacist has checked is given exactly as
    before, and no record, audit row or board task gains a key;
  * switch on, rule 'none': a routine order is refused until a pharmacist has
    checked it (the ward row's verified_by, invariant I1), a stat order too;
    'not given' is always recorded; an unlinked row is unchanged; the route
    answers 422 with the bedside's words;
  * switch on, rule 'stat_urgent' (option (a)): the FIRST dose of a stat or
    urgent order goes before the check and is marked on the bedside event,
    the audit row, the record, the MAR list and a replay; the next dose is
    refused until the check; a routine order is refused; a dose already
    charted by someone else counts (the second check after the INSERT);
  * an AI-charted order keeps its older refusal whatever the rule;
  * the shift board shows the bedside's answer before the walk to the bed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import emar_service, nurse_shift_service
from services.medorders import adoption, emar_check

SERVER_DIR = Path(__file__).resolve().parent.parent
DEPT = "W8I Verify Ward"
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL WHERE patient_id IN ({marks})",
                   _PATIENTS)
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                   _PATIENTS)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(role, department=None):
    uid = f"W8IV{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, ?, ?, 'x', ?)", (uid, f"Verify check {role}", role, department))
    return uid


def _patient():
    pid = f"PW8IV{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, bed, ward, status) "
                   "VALUES (?, ?, ?, 'V-1', 'VW', 'admitted')",
                   (pid, "Verify Before Dose", f"MRN-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('W8IVADM', 'Verify Admitter', 'doctor', 'x')")
        db.execute("INSERT INTO admissions (patient_id, department, status, admitted_by) "
                   "VALUES (?, ?, 'active', 'W8IVADM')", (pid, DEPT))
    _PATIENTS.append(pid)
    return pid


def _chart(pid, *, drug="Paracetamol", dosage="500 mg", frequency="TDS", source="clinician"):
    with db_session() as db:
        return db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, ?, ?, 'Oral', ?, 'active', NULL, ?)",
            (pid, drug, dosage, frequency, source)).lastrowid


def _link(pid, med_id, *, priority=None):
    """Adopt the patient's chart rows into the store (prescribed, unchecked)
    and set the order's priority."""
    adoption.adopt_legacy(trigger="script", patient_ids=[pid])
    with db_session() as db:
        order_id = db.execute("SELECT med_order_id FROM medications WHERE id = ?",
                              (med_id,)).fetchone()[0]
        assert order_id, "the chart row was not adopted"
        if priority is not None:
            db.execute("UPDATE med_orders SET priority = ? WHERE id = ?", (priority, order_id))
    return order_id


def _pharmacist_checks(med_id, by="W8IVPHARM"):
    """The verification fact every verify path writes onto the ward row (I1)."""
    with db_session() as db:
        db.execute("UPDATE medications SET verified_by = ?, verified_at = '2026-10-10 08:00:00' "
                   "WHERE id = ?", (by, med_id))


def _give(med_id, nurse, **kw):
    return emar_service.record_administration(med_id, nurse, status="given", **kw)


def _mar_rows(med_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM medication_administrations WHERE medication_id = ? ORDER BY id",
            (med_id,)).fetchall()]


def _events(order_id, kind):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM med_order_events WHERE med_order_id = ? AND kind = ? ORDER BY id",
            (order_id, kind)).fetchall()]


def _dose_audit(admin_id):
    with db_session() as db:
        for r in db.execute("SELECT * FROM audit_logs WHERE action_type = "
                            "'medication_administered' ORDER BY rowid DESC LIMIT 80").fetchall():
            meta = json.loads(r["metadata_json"])
            if meta.get("administration_id") == admin_id:
                return dict(r), meta
    return None, None


@pytest.fixture()
def nurse():
    return _user("nurse")


@pytest.fixture()
def switches(monkeypatch):
    """Store and lifecycle on; the pharmacist check off until a test turns it
    on with `check_on`."""
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_VERIFY_BEFORE_DOSE_ENABLED", False)
    return monkeypatch


def check_on(monkeypatch, rule="none"):
    monkeypatch.setattr(config, "MED_ORDER_VERIFY_BEFORE_DOSE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_FIRST_DOSE_BEFORE_CHECK", rule)


# ── 1. The switch and the setting ─────────────────────────────────────────────

def test_the_switch_is_registered_and_off_by_default():
    assert config.FEATURE_FLAGS["med_order_verify_before_dose"] == "ORB_MED_ORDER_VERIFY_BEFORE_DOSE"
    assert config.MED_ORDER_VERIFY_BEFORE_DOSE_ENABLED is False
    assert config.feature_flags()["med_order_verify_before_dose"] is False
    assert config.MED_ORDER_FIRST_DOSE_BEFORE_CHECK == "none"
    assert emar_check.verify_before_dose_rule() is None


def test_the_switch_is_off_whenever_the_lifecycle_is(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_VERIFY_BEFORE_DOSE_ENABLED", True)
    assert config.feature_enabled("med_order_verify_before_dose") is False
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    assert config.feature_enabled("med_order_verify_before_dose") is False  # store still off
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    assert config.feature_enabled("med_order_verify_before_dose") is True
    assert emar_check.verify_before_dose_rule() == "none"


def test_an_unknown_first_dose_rule_reads_as_none(monkeypatch):
    check_on(monkeypatch, rule="anything")
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    assert emar_check.verify_before_dose_rule() == "none"


def _import_config(env: dict) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "ORB_DEPLOYMENT_PROFILE": "dev",
            "ORB_JWT_SECRET": "probe-only-secret-0123456789abcdef"}
    return subprocess.run(
        [sys.executable, "-c",
         "import json, config; f = config.feature_flags(); "
         "print('FLAGS:' + json.dumps([f['med_order_verify_before_dose'], "
         "config.MED_ORDER_FIRST_DOSE_BEFORE_CHECK]))"],
        cwd=str(SERVER_DIR), env={**base, **env}, capture_output=True, text=True, timeout=120)


def test_config_refuses_to_load_with_the_check_on_and_the_lifecycle_off():
    proc = _import_config({"ORB_MED_ORDER_STORE": "1", "ORB_MED_ORDER_VERIFY_BEFORE_DOSE": "1"})
    assert proc.returncode != 0
    assert "ORB_MED_ORDER_VERIFY_BEFORE_DOSE is on but ORB_MED_ORDER_LIFECYCLE is off" \
        in proc.stderr


@pytest.mark.parametrize("env,expected", [
    ({}, '[false, "none"]'),
    ({"ORB_MED_ORDER_STORE": "1", "ORB_MED_ORDER_LIFECYCLE": "1",
      "ORB_MED_ORDER_VERIFY_BEFORE_DOSE": "yes"}, '[true, "none"]'),
    ({"ORB_MED_ORDER_STORE": "1", "ORB_MED_ORDER_LIFECYCLE": "1",
      "ORB_MED_ORDER_VERIFY_BEFORE_DOSE": "on",
      "ORB_MED_ORDER_FIRST_DOSE_BEFORE_CHECK": "STAT_URGENT"}, '[true, "stat_urgent"]'),
    ({"ORB_MED_ORDER_FIRST_DOSE_BEFORE_CHECK": "every"}, '[false, "none"]'),
])
def test_config_reads_the_switch_and_the_rule(env, expected):
    proc = _import_config(env)
    assert proc.returncode == 0, proc.stderr
    assert "FLAGS:" + expected in proc.stdout


def test_health_publishes_the_switch_off():
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["features"]["med_order_verify_before_dose"] is False


# ── 2. The rule itself (pure) ─────────────────────────────────────────────────

@pytest.mark.parametrize("rule,priority,verified,given,allowed,before", [
    (None, None, False, 0, True, False),            # switch off: no question asked
    ("none", None, False, 0, False, False),
    ("none", "stat", False, 0, False, False),        # strictest: no exceptions
    ("none", "routine", True, 3, True, False),        # checked: always allowed
    ("stat_urgent", "stat", False, 0, True, True),
    ("stat_urgent", "urgent", False, 0, True, True),
    ("stat_urgent", "stat", False, 1, False, False),  # the next dose waits
    ("stat_urgent", "routine", False, 0, False, False),
    ("stat_urgent", None, False, 0, False, False),
    ("stat_urgent", "stat", True, 1, True, False),
])
def test_the_pharmacist_check_table(rule, priority, verified, given, allowed, before):
    out = emar_check.pharmacist_check(
        {"priority": priority}, {"verified_by": "PH1" if verified else None},
        given_doses=given, rule=rule)
    assert out["allowed"] is allowed
    assert out["before_check"] is before
    assert out["required"] is (rule is not None)
    if not allowed:
        assert out["code"] == emar_check.NOT_VERIFIED_CODE
        assert "not given" in out["message"]


# ── 3. Switch off: exactly as before ──────────────────────────────────────────

def test_switch_off_a_linked_unchecked_order_is_given_as_before(switches, nurse):
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id)
    rec = _give(med_id, nurse)
    assert "given_before_pharmacist_check" not in rec
    (event,) = _events(order_id, "administered")
    assert "before_pharmacist_check" not in json.loads(event["payload_json"])
    row, meta = _dose_audit(rec["id"])
    assert "given_before_pharmacist_check" not in meta
    assert "pharmacist" not in row["summary_message"]
    assert all("given_before_pharmacist_check" not in r
               for r in emar_service.list_administrations(pid))


def test_switch_off_the_board_task_gains_no_key(switches):
    pid = _patient()
    med_id = _chart(pid)
    _link(pid, med_id)
    task = _board_task(pid, med_id)
    assert "pharmacist_check" not in task


# ── 4. Switch on, the strictest rule ──────────────────────────────────────────

def test_an_unchecked_routine_order_is_refused_in_words(switches, nurse):
    check_on(switches)
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id, priority="routine")
    with pytest.raises(emar_check.OrderStateRefused) as exc:
        _give(med_id, nurse)
    assert isinstance(exc.value, ValueError)
    assert exc.value.verdict["code"] == "order_not_verified"
    assert "pharmacist has not checked" in str(exc.value)
    assert _mar_rows(med_id) == []
    assert _events(order_id, "administered") == []


def test_a_stat_order_is_refused_too_under_the_strictest_rule(switches, nurse):
    check_on(switches, rule="none")
    pid = _patient()
    med_id = _chart(pid, frequency="STAT")
    _link(pid, med_id, priority="stat")
    with pytest.raises(emar_check.OrderStateRefused):
        _give(med_id, nurse)
    assert _mar_rows(med_id) == []


def test_not_given_is_always_recorded(switches, nurse):
    check_on(switches)
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id)
    rec = emar_service.record_administration(med_id, nurse, status="not_given",
                                             not_given_reason="Waiting for the pharmacist")
    assert rec["status"] == "not_given"
    assert len(_events(order_id, "not_given")) == 1


def test_a_checked_order_is_given_and_not_marked(switches, nurse):
    check_on(switches, rule="stat_urgent")
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id, priority="stat")
    _pharmacist_checks(med_id)
    rec = _give(med_id, nurse)
    assert "given_before_pharmacist_check" not in rec
    (event,) = _events(order_id, "administered")
    assert "before_pharmacist_check" not in json.loads(event["payload_json"])


def test_an_unlinked_row_is_untouched(switches, nurse):
    check_on(switches)
    pid = _patient()
    med_id = _chart(pid)  # never adopted: no link
    rec = _give(med_id, nurse)
    assert rec["status"] == "given" and "given_before_pharmacist_check" not in rec


def test_an_ai_charted_order_keeps_its_older_refusal(switches, nurse):
    check_on(switches, rule="stat_urgent")
    pid = _patient()
    med_id = _chart(pid, source="llm_extracted")
    _link(pid, med_id, priority="stat")
    with pytest.raises(ValueError) as exc:
        _give(med_id, nurse)
    assert not isinstance(exc.value, emar_check.OrderStateRefused)
    assert "pharmacist verification required" in str(exc.value)


def test_the_pharmacists_verify_releases_the_dose(switches, nurse):
    """The legacy Verify Queue writes verified_by on the ward row and mirrors
    the order (I1): the same check then lets the dose go."""
    from services import pharmacy_verification_service as pv
    check_on(switches)
    pid = _patient()
    med_id = _chart(pid, source="llm_extracted")
    order_id = _link(pid, med_id)
    pharmacist = _user("pharmacist")
    pv.verify_medication(med_id, pharmacist)
    with db_session() as db:
        status = db.execute("SELECT status FROM med_orders WHERE id = ?",
                            (order_id,)).fetchone()[0]
    assert status == "verified"
    rec = _give(med_id, nurse)
    assert rec["status"] == "given" and "given_before_pharmacist_check" not in rec


def test_the_route_answers_422_with_the_words(switches):
    check_on(switches)
    pid = _patient()
    med_id = _chart(pid)
    _link(pid, med_id)
    nurse = _user("nurse")
    resp = TestClient(app).post(
        f"/api/emar/medications/{med_id}/administer",
        headers={"Authorization": f"Bearer {create_token(nurse, 'nurse')}",
                 "Host": "localhost"},
        json={"status": "given"})
    assert resp.status_code == 422, resp.text
    assert "pharmacist has not checked" in resp.json()["detail"]


# ── 5. Option (a): the first stat or urgent dose before the check ─────────────

@pytest.mark.parametrize("priority", ["stat", "urgent"])
def test_the_first_stat_or_urgent_dose_goes_and_is_marked(switches, nurse, priority):
    check_on(switches, rule="stat_urgent")
    pid = _patient()
    med_id = _chart(pid, drug="Ceftriaxone", dosage="1 g")
    order_id = _link(pid, med_id, priority=priority)
    rec = _give(med_id, nurse, idempotency_key=f"w8iv-{uuid.uuid4().hex}")
    assert rec["given_before_pharmacist_check"] is True and rec["replayed"] is False
    (event,) = _events(order_id, "administered")
    payload = json.loads(event["payload_json"])
    assert payload["before_pharmacist_check"] is True
    assert payload["first_dose_rule"] == "stat_urgent" and payload["priority"] == priority
    row, meta = _dose_audit(rec["id"])
    assert meta["given_before_pharmacist_check"] is True
    assert meta["first_dose_rule"] == "stat_urgent" and meta["order_priority"] == priority
    assert "before the pharmacist check" in row["summary_message"]
    listed = {r["id"]: r for r in emar_service.list_administrations(pid)}
    assert listed[rec["id"]]["given_before_pharmacist_check"] is True


def test_a_replay_of_the_marked_dose_still_says_so(switches, nurse):
    check_on(switches, rule="stat_urgent")
    pid = _patient()
    med_id = _chart(pid)
    _link(pid, med_id, priority="stat")
    key = f"w8iv-{uuid.uuid4().hex}"
    first = _give(med_id, nurse, idempotency_key=key)
    again = _give(med_id, nurse, idempotency_key=key)
    assert again["replayed"] is True and again["id"] == first["id"]
    assert again["given_before_pharmacist_check"] is True


def test_the_next_dose_waits_for_the_check(switches, nurse):
    check_on(switches, rule="stat_urgent")
    pid = _patient()
    med_id = _chart(pid)
    order_id = _link(pid, med_id, priority="urgent")
    _give(med_id, nurse)
    with pytest.raises(emar_check.OrderStateRefused) as exc:
        _give(med_id, nurse)
    assert "next dose waits" in str(exc.value)
    assert len(_mar_rows(med_id)) == 1
    # Once a pharmacist has checked the order, the next dose goes, unmarked.
    _pharmacist_checks(med_id)
    rec = _give(med_id, nurse)
    assert "given_before_pharmacist_check" not in rec
    assert len(_events(order_id, "administered")) == 2


def test_a_routine_order_still_waits_under_option_a(switches, nurse):
    check_on(switches, rule="stat_urgent")
    pid = _patient()
    med_id = _chart(pid)
    _link(pid, med_id, priority="routine")
    with pytest.raises(emar_check.OrderStateRefused):
        _give(med_id, nurse)


def test_a_dose_charted_by_someone_else_counts_after_the_insert(switches, nurse):
    """The second check, under the dose's write lock, leaves out only the row
    the dose itself wrote: a 'first' dose another nurse committed first is
    counted, and this one is refused and rolled back."""
    check_on(switches, rule="stat_urgent")
    pid = _patient()
    med_id = _chart(pid)
    _link(pid, med_id, priority="stat")
    with db_session() as db:
        other = db.execute(
            "INSERT INTO medication_administrations (medication_id, patient_id, administered_by, "
            "status, dose_given, route_given, confirmation_method) "
            "VALUES (?, ?, ?, 'given', '500 mg', 'Oral', 'typed')", (med_id, pid, nurse)).lastrowid
        mine = db.execute(
            "INSERT INTO medication_administrations (medication_id, patient_id, administered_by, "
            "status, dose_given, route_given, confirmation_method) "
            "VALUES (?, ?, ?, 'given', '500 mg', 'Oral', 'typed')", (med_id, pid, nurse)).lastrowid
        assert emar_check.given_doses_in_txn(db, med_id, exclude_administration_id=mine) == 1
        with pytest.raises(emar_check.OrderStateRefused):
            emar_check.check_dose_in_txn(db, med_id, "given", exclude_administration_id=mine)
        db.execute("DELETE FROM medication_administrations WHERE id IN (?, ?)", (other, mine))


# ── 6. The shift board ────────────────────────────────────────────────────────

def _board_task(pid, med_id):
    board = nurse_shift_service.build_board(DEPT)
    (card,) = [c for c in board["patients"] if c["patient_id"] == pid]
    (task,) = [t for t in card["medications"] if t["medication_id"] == med_id]
    return task


def test_the_board_says_waiting_for_the_pharmacist(switches):
    check_on(switches)
    pid = _patient()
    med_id = _chart(pid)
    _link(pid, med_id)
    check = _board_task(pid, med_id)["pharmacist_check"]
    assert check["state"] == "waiting"
    assert "pharmacist has not checked" in check["message"]


def test_the_board_offers_the_first_stat_dose_and_then_waits(switches, nurse):
    check_on(switches, rule="stat_urgent")
    pid = _patient()
    med_id = _chart(pid)
    _link(pid, med_id, priority="stat")
    assert _board_task(pid, med_id)["pharmacist_check"]["state"] == "first_dose_before_check"
    _give(med_id, nurse)
    assert _board_task(pid, med_id)["pharmacist_check"]["state"] == "waiting"


def test_the_board_says_nothing_once_checked(switches):
    check_on(switches)
    pid = _patient()
    med_id = _chart(pid)
    _link(pid, med_id)
    _pharmacist_checks(med_id)
    assert _board_task(pid, med_id)["pharmacist_check"] is None


def test_the_board_says_nothing_for_an_unlinked_row(switches):
    check_on(switches)
    pid = _patient()
    med_id = _chart(pid)
    assert _board_task(pid, med_id)["pharmacist_check"] is None
