"""The Verify Queue's verify and reject mirrored through the medication-order
store, keyed on the link (medorders-3, W5-E; invariants I1 and I2).

Pinned here:
  * the blocker scenario: an unverified AI drug adopted into the store, then
    verified on the legacy Verify Queue, is givable at the bedside and the
    store shows it verified, by that pharmacist, with its event and audit row;
  * a dose, route or frequency the pharmacist corrected while verifying is
    copied onto the order as an 'amended' event before the verification; a
    verification with no correction writes none;
  * a legacy reject moves the order to rejected with the reason;
  * an unlinked row writes nothing to the store;
  * a held order: verify keeps the hold (held_from becomes 'verified', so a
    resume returns to verified), reject ends it and clears the ward row's hold;
  * a reject on an order that had already ended records an event only;
  * a broken link, or a refusal from the store, undoes the legacy write and
    reaches the pharmacist as words;
  * the mirror runs through the HTTP route as well;
  * store on, chart and verify, then both switches off: every eMAR and Verify
    Queue verdict equals the verdict of a site that never turned the store on.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import emar_service
from services import pharmacy_verification_service as pv
from services.medorders import adoption, chart_lane, core, emar_check

_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _cleanup_store_rows():
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL, held_at = NULL, "
                   f"hold_reason = NULL WHERE patient_id IN ({marks})", _PATIENTS)


def _user(role):
    uid = f"MOLM{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, 'x')",
                   (uid, f"Legacy mirror {role}", role))
    return uid


def _patient():
    pid = f"PMOLM{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, "Legacy Mirror Test", f"MRN-{uuid.uuid4().hex[:8]}"))
    _PATIENTS.append(pid)
    return pid


def _ai_chart(pid, *, drug="Paracetamol", status="active"):
    """A Sage-charted drug as the Sage lane writes it: llm_extracted, the
    placeholder dose, unverified."""
    with db_session() as db:
        return db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, ?, 'REVIEW REQUIRED', 'Review', 'TDS', ?, NULL, "
            "'llm_extracted')", (pid, drug, status)).lastrowid


def _adopt(pid):
    adoption.adopt_legacy(trigger="script", patient_ids=[pid])


def _ward(med_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone())


def _order_of(med_id):
    with db_session() as db:
        row = db.execute("SELECT o.* FROM med_orders o JOIN medications m "
                         "ON m.med_order_id = o.id WHERE m.id = ?", (med_id,)).fetchone()
        return dict(row) if row else None


def _events(order_id, kind=None):
    sql, args = "SELECT * FROM med_order_events WHERE med_order_id = ?", [order_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY id", args).fetchall()]


def _audits(order_id, action):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE target_type = 'med_order' AND target_id = ? "
            "AND action_type = ? ORDER BY rowid", (order_id, action)).fetchall()]


def _verify(med_id, pharmacist):
    return pv.verify_medication(med_id, pharmacist, dosage="500 mg", route="Oral",
                                frequency="TDS")


def _hold(order_id):
    with db_session(immediate=True) as conn:
        core.transition_in_txn(conn, order_id, "held", expected_version=None,
                               actor_id="MOLMDOC", actor_role="doctor", entry_point="test.hold",
                               reason="Awaiting the renal function result")


def _give_verdict(med_id, nurse):
    """The bedside verdict for a 'given' dose: ('given', None) or the
    refusal's class and words (the row's own id written as <id>, so two
    scenarios compare). Commits the dose when it is allowed."""
    try:
        emar_service.record_administration(med_id, nurse, status="given")
        return ("given", None)
    except ValueError as e:
        return (type(e).__name__, str(e).replace(f"medication {med_id} ", "medication <id> "))


@pytest.fixture()
def store_on(monkeypatch):
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)


@pytest.fixture()
def people():
    return {"pharmacist": _user("pharmacist"), "nurse": _user("nurse")}


# ── The blocker scenario ──────────────────────────────────────────────────────

def test_an_adopted_unverified_ai_drug_verified_on_the_legacy_queue_is_givable(store_on, people):
    pid = _patient()
    med_id = _ai_chart(pid)
    _adopt(pid)
    order = _order_of(med_id)
    assert order["status"] == "prescribed" and order["needs_verification"] == 1
    refused = _give_verdict(med_id, people["nurse"])
    assert refused[0] == "ValueError" and "pharmacist verification required" in refused[1]

    result = _verify(med_id, people["pharmacist"])
    assert result["status"] == "verified"
    order = _order_of(med_id)
    assert order["status"] == "verified" and order["verified_by"] == people["pharmacist"]
    ward = _ward(med_id)
    assert ward["verified_by"] == people["pharmacist"] and ward["dosage"] == "500 mg"
    (event,) = _events(order["id"], "verified")
    assert event["entry_point"] == chart_lane.ENTRY_LEGACY_VERIFY
    assert event["actor_role"] == "pharmacist"
    (audit,) = _audits(order["id"], "med_order_verified")
    meta = json.loads(audit["metadata_json"])
    assert meta["lane"] == chart_lane.LANE_LEGACY
    assert meta["medication_id"] == str(med_id)
    assert _give_verdict(med_id, people["nurse"]) == ("given", None)
    assert len(_events(order["id"], "administered")) == 1


def test_the_pharmacists_correction_is_copied_onto_the_order_before_it_is_verified(
        store_on, people):
    pid = _patient()
    med_id = _ai_chart(pid)
    _adopt(pid)
    order_id = _order_of(med_id)["id"]
    _verify(med_id, people["pharmacist"])
    order = _order_of(med_id)
    assert (order["dose"], order["route"], order["frequency"]) == ("500 mg", "Oral", "TDS")
    assert order["status"] == "verified" and order["version"] == 3
    kinds = [e["kind"] for e in _events(order_id)]
    assert kinds == ["adopted", "amended", "verified"]
    (amended,) = _events(order_id, "amended")
    assert amended["entry_point"] == chart_lane.ENTRY_LEGACY_VERIFY
    assert json.loads(amended["payload_json"])["changes"] == {
        "dose": {"before": "REVIEW REQUIRED", "after": "500 mg"},
        "route": {"before": "Review", "after": "Oral"},
    }
    (audit,) = _audits(order_id, "med_order_amended")
    assert json.loads(audit["metadata_json"])["amended_fields"] == ["dose", "route"]


def test_a_verification_without_a_correction_writes_no_amendment(store_on, people):
    pid = _patient()
    with db_session() as db:
        med_id = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, 'Ondansetron', '4 mg', 'IV', 'TDS', 'active', "
            "NULL, 'agentic')", (pid,)).lastrowid
    _adopt(pid)
    pv.verify_medication(med_id, people["pharmacist"])
    order = _order_of(med_id)
    assert order["status"] == "verified" and order["version"] == 2
    assert _events(order["id"], "amended") == []


def test_a_legacy_reject_moves_the_order_to_rejected(store_on, people):
    pid = _patient()
    med_id = _ai_chart(pid)
    _adopt(pid)
    pv.reject_medication(med_id, people["pharmacist"], "Duplicate of the oral course")
    order = _order_of(med_id)
    assert order["status"] == "rejected"
    assert order["status_reason"] == "Duplicate of the oral course"
    assert _ward(med_id)["status"] == "discontinued"
    (event,) = _events(order["id"], "rejected")
    assert event["entry_point"] == chart_lane.ENTRY_LEGACY_REJECT
    assert len(_audits(order["id"], "med_order_rejected")) == 1


def test_an_unlinked_row_writes_nothing_to_the_store(people):
    pid = _patient()
    verified, rejected = _ai_chart(pid), _ai_chart(pid, drug="Ibuprofen")
    _verify(verified, people["pharmacist"])
    pv.reject_medication(rejected, people["pharmacist"], "Not indicated")
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM med_orders WHERE patient_id = ?",
                          (pid,)).fetchone()[0] == 0
    assert _ward(verified)["verified_by"] == people["pharmacist"]
    assert _ward(rejected)["status"] == "discontinued"


# ── Held orders ───────────────────────────────────────────────────────────────

def test_verifying_a_held_order_keeps_the_hold_and_a_resume_returns_to_verified(
        store_on, people):
    pid = _patient()
    med_id = _ai_chart(pid)
    _adopt(pid)
    order_id = _order_of(med_id)["id"]
    _hold(order_id)
    _verify(med_id, people["pharmacist"])
    order = _order_of(med_id)
    assert order["status"] == "held" and order["held_from"] == "verified"
    assert order["verified_by"] == people["pharmacist"]
    assert order["status_reason"] == "Awaiting the renal function result"
    ward = _ward(med_id)
    assert ward["held_at"] is not None and ward["verified_by"] == people["pharmacist"]
    (event,) = _events(order_id, "verified")
    assert (event["from_status"], event["to_status"]) == ("held", "held")
    # Still held at the bedside.
    assert _give_verdict(med_id, people["nurse"])[0] == "OrderStateRefused"
    with db_session(immediate=True) as conn:
        core.transition_in_txn(conn, order_id, "verified", expected_version=None,
                               actor_id="MOLMDOC", actor_role="doctor",
                               entry_point="test.resume", reason="eGFR back, normal")
    assert _order_of(med_id)["status"] == "verified"
    assert _give_verdict(med_id, people["nurse"]) == ("given", None)


def test_rejecting_a_held_order_ends_it_and_clears_the_hold_marker(store_on, people):
    pid = _patient()
    med_id = _ai_chart(pid)
    _adopt(pid)
    order_id = _order_of(med_id)["id"]
    _hold(order_id)
    pv.reject_medication(med_id, people["pharmacist"], "Wrong patient")
    order = _order_of(med_id)
    assert order["status"] == "rejected" and order["held_from"] is None
    assert order["projection_status"] is None
    ward = _ward(med_id)
    assert ward["status"] == "discontinued"
    assert ward["held_at"] is None and ward["hold_reason"] is None
    assert len(_events(order_id, "rejected")) == 1


def test_a_reject_on_an_order_that_had_already_ended_records_an_event_only(store_on, people):
    pid = _patient()
    med_id = _ai_chart(pid, status="discontinued")
    _adopt(pid)
    order = _order_of(med_id)
    assert order["status"] == "stopped"
    pv.reject_medication(med_id, people["pharmacist"], "Was never indicated")
    after = _order_of(med_id)
    assert after["status"] == "stopped" and after["version"] == order["version"]
    (event,) = _events(order["id"], "rejected")
    assert json.loads(event["payload_json"]) == {"order_already_ended": True}


# ── Refusals undo the legacy write ────────────────────────────────────────────

def test_a_broken_link_refuses_the_decision_and_leaves_the_row_unverified(people):
    pid = _patient()
    med_id = _ai_chart(pid)
    with db_session() as db:
        db.execute("UPDATE medications SET med_order_id = ? WHERE id = ?",
                   (str(uuid.uuid4()), med_id))
    with pytest.raises(ValueError) as exc:
        _verify(med_id, people["pharmacist"])
    assert "order record is missing" in str(exc.value)
    ward = _ward(med_id)
    assert ward["verified_by"] is None and ward["dosage"] == "REVIEW REQUIRED"


def test_a_store_refusal_undoes_the_legacy_verify(store_on, people, monkeypatch):
    pid = _patient()
    med_id = _ai_chart(pid)
    _adopt(pid)

    def _stale(*a, **k):
        raise core.StaleOrderVersion("This order was changed by someone else. "
                                     "Refresh and try again.")
    monkeypatch.setattr(core, "transition_in_txn", _stale)
    with pytest.raises(ValueError) as exc:
        _verify(med_id, people["pharmacist"])
    assert "changed by someone else" in str(exc.value)
    assert _ward(med_id)["verified_by"] is None
    order = _order_of(med_id)
    # The correction written before the refused step is undone with it.
    assert order["status"] == "prescribed" and order["version"] == 1
    assert order["dose"] == "REVIEW REQUIRED" and _events(order["id"], "amended") == []


def test_the_route_mirrors_and_answers_a_refusal_as_a_sentence(store_on, people):
    client = TestClient(app)
    hdr = {"Authorization": f"Bearer {create_token(people['pharmacist'], 'pharmacist')}"}
    pid = _patient()
    med_id = _ai_chart(pid)
    _adopt(pid)
    resp = client.post(f"/api/pharmacy/{med_id}/verify", headers=hdr,
                       json={"dosage": "500 mg", "route": "Oral", "frequency": "TDS"})
    assert resp.status_code == 200, resp.text
    assert _order_of(med_id)["status"] == "verified"

    broken = _ai_chart(pid, drug="Ibuprofen")
    with db_session() as db:
        db.execute("UPDATE medications SET med_order_id = ? WHERE id = ?",
                   (str(uuid.uuid4()), broken))
    resp = client.post(f"/api/pharmacy/{broken}/reject", headers=hdr,
                       json={"reason": "Not indicated"})
    assert resp.status_code == 400
    assert "order record is missing" in resp.json()["detail"]
    assert _ward(broken)["status"] == "active"


# ── Store on, then off: the verdicts are the never-on verdicts ────────────────

def _scenario(people, *, store_first: bool, monkeypatch) -> dict:
    """Four AI drugs and one clinician drug for one patient: verify one,
    reject one, hold none, leave two pending. With `store_first` they are
    adopted and decided with the store on, then both switches go off. Returns
    the Verify Queue membership and each drug's bedside verdict."""
    if store_first:
        monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    pid = _patient()
    meds = {name: _ai_chart(pid, drug=name)
            for name in ("Paracetamol", "Ibuprofen", "Ondansetron", "Pantoprazole")}
    with db_session() as db:
        meds["Cetirizine"] = db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source) VALUES (?, 'Cetirizine', '10 mg', 'Oral', 'OD', 'active', "
            "NULL, 'clinician')", (pid,)).lastrowid
    if store_first:
        _adopt(pid)
    _verify(meds["Paracetamol"], people["pharmacist"])
    pv.reject_medication(meds["Ibuprofen"], people["pharmacist"], "Renal impairment")
    monkeypatch.setattr(config, "MED_ORDER_STORE_ENABLED", False)
    monkeypatch.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", False)
    queue = {m["drug_name"] for m in pv.list_pending_verification() if m["patient_id"] == pid}
    verdicts = {name: _give_verdict(med_id, people["nurse"]) for name, med_id in meds.items()}
    linked = sum(1 for med_id in meds.values() if _ward(med_id)["med_order_id"])
    return {"queue": queue, "verdicts": verdicts, "linked": linked}


def test_store_on_then_both_switches_off_gives_the_never_on_verdicts(people, monkeypatch):
    never_on = _scenario(people, store_first=False, monkeypatch=monkeypatch)
    was_on = _scenario(people, store_first=True, monkeypatch=monkeypatch)
    assert never_on["linked"] == 0 and was_on["linked"] == 5
    assert was_on["queue"] == never_on["queue"] == {"Ondansetron", "Pantoprazole"}
    assert was_on["verdicts"] == never_on["verdicts"]
    assert never_on["verdicts"]["Paracetamol"] == ("given", None)
    assert never_on["verdicts"]["Cetirizine"] == ("given", None)
    assert never_on["verdicts"]["Ibuprofen"][0] == "ValueError"
    assert "pharmacist verification required" in never_on["verdicts"]["Ondansetron"][1]
    assert not any(v[0] == emar_check.OrderStateRefused.__name__
                   for v in was_on["verdicts"].values())
