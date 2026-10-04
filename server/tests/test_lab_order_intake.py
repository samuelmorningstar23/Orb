"""Laboratory order intake, specimen planning and accession (lis-6).

Pinned here:
  * ORB_LAB off (or draining): an activated laboratory order is exactly what
    it was before the laboratory existed. No item, no specimen, no exception.
  * The cutover: an order activated before the latest 'enabled' event is
    never taken in, by the hook or the sweep; a draft signed after it is.
  * Bedside tests (ruling LIS-R18): the DKA order set's ketones and VBG get no
    specimen and keep the free-text result path; its glucose and U&E do.
  * Panels explode one item per component; a panel is one tube of the
    panel's specimen type; U&E and LFT share one serum tube; a printed label
    is never added to; the same test twice is two tubes; STAT is its own tube.
  * Unmapped codes land in lab_exceptions, once, and the laboratory's list
    shows them (lab.read, audited, 404 while off).
  * Idempotent under replay and under the hook racing the sweep.
  * The hook never raises and never changes what orders_service returns; a
    failure (an audit that cannot be written, used-up accession numbers) is
    recorded as 'intake_failed' and the sweep retries it.
  * The sweep rule: an order whose catalogue entry changed after activation
    is left where the hook left it, so a reclassification applies only to
    orders activated afterwards.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from services import lab_accession, lab_module, lab_order_service as los
from services import lab_master_service as lm
from services import order_sets_service, orders_service, terminology_service
from services.audit_service import AuditWriteError
from services.clinical_time import utc_iso_now

client = TestClient(main.app)

PATHOLOGIST = {"id": "LOIPATH01", "role": "pathologist", "name": "Pathologist Intake"}
DOCTOR_ID = "D001"


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _catalogue():
    """The starter catalogue, and at the end every laboratory row this module
    planned removed. Items left awaiting collection are open laboratory work,
    which would make a switched-off module 'draining' for every later test
    file in the session (test_opt_in_platform, the health probe)."""
    terminology_service.seed_terminology()
    lm.load_starter_catalogue()
    created: list[str] = []
    yield created
    if created:
        marks = ", ".join("?" for _ in created)
        ids = tuple(created)
        with db_session() as db:
            db.execute(f"DELETE FROM lab_exceptions WHERE order_id IN "
                       f"(SELECT id FROM orders WHERE patient_id IN ({marks}))", ids)
            db.execute(f"DELETE FROM lab_order_items WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimen_events WHERE patient_id IN ({marks})", ids)
            db.execute(f"DELETE FROM lab_specimens WHERE patient_id IN ({marks})", ids)
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
            db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                       ids)
    lab_module.refresh_drain_state()


@pytest.fixture
def lab_on(monkeypatch):
    """The module on, with a cutover written now (as startup would)."""
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    instant = utc_iso_now()
    with db_session() as db:
        event_id = db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, ?)",
            (instant, "test cutover")).lastrowid
    yield instant
    with db_session() as db:
        db.execute("DELETE FROM lab_module_events WHERE id = ?", (event_id,))


@pytest.fixture
def patient(_catalogue):
    pid = f"PLOI{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, "Intake Test", f"MRN-LOI-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', 'General Medicine', 'active')",
                   (f"ALOI{uuid.uuid4().hex[:8].upper()}", pid))
    _catalogue.append(pid)
    return pid


@pytest.fixture
def no_hook(monkeypatch):
    """Activate orders without the post-commit hook, as if the process had
    stopped between the commit and the hook."""
    monkeypatch.setattr(orders_service, "_after_lab_activation", lambda order, actor_id: None)


def _order(patient_id, code, system="LOINC", *, priority="routine", status="active",
           order_type="lab"):
    return orders_service.create_order(
        patient_id=patient_id, order_type=order_type, description=f"{system} {code}",
        code=code, code_system=system, priority=priority, ordered_by=DOCTOR_ID, status=status)


def _items(order_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_order_items WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()]


def _specimen(specimen_id):
    with db_session() as db:
        row = db.execute("SELECT * FROM lab_specimens WHERE id = ?", (specimen_id,)).fetchone()
    return dict(row) if row else None


def _specimen_events(specimen_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_specimen_events WHERE specimen_id = ? ORDER BY seq",
            (specimen_id,)).fetchall()]


def _exceptions(order_id, kind=None):
    sql = "SELECT * FROM lab_exceptions WHERE order_id = ?"
    args: list = [order_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY id", args).fetchall()]


def _intake_audits(order_id, outcome=None):
    sql = ("SELECT * FROM audit_logs WHERE action_type = 'lab_order_intake' AND target_id = ?")
    args: list = [order_id]
    if outcome:
        sql += " AND outcome = ?"
        args.append(outcome)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def _patient_specimens(patient_id):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM lab_specimens WHERE patient_id = ? ORDER BY created_at, id",
            (patient_id,)).fetchall()]


def _hdr(role):
    uid = f"LOI{uuid.uuid4().hex[:6].upper()}"
    dept = "General Medicine" if role in ("doctor", "nurse", "ward_clerk") else None
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, dept, hash_password("password")))
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _user_in(role, dept):
    """(user id, headers) for an account whose department is `dept`."""
    uid = f"LOI{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) VALUES (?, ?, ?, ?, ?)",
            (uid, f"{role} {uid}", role, dept, hash_password("password")))
    return uid, {"Authorization": f"Bearer {create_token(uid, role)}"}


def _patient_in(created, dept, name):
    pid = f"PLOI{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (pid, name, f"MRN-LOI-{uuid.uuid4().hex[:8]}"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
                   "VALUES (?, ?, 'D001', ?, 'active')",
                   (f"ALOI{uuid.uuid4().hex[:8].upper()}", pid, dept))
    created.append(pid)
    return pid


@pytest.fixture
def own_test():
    """Tests of this file's own, removed after, so a reclassification here
    never reaches a starter test another file reads."""
    made: list[str] = []

    def make(**fields):
        code = f"ORB-LOI-{uuid.uuid4().hex[:8].upper()}"
        body = {"code_system": "LOCAL", "code": code, "name": f"Intake analyte {code}",
                "short_name": "Intake test", "section_id": "biochemistry",
                "specimen_type_code": "serum", "unit": "mmol/L", "decimal_places": 1}
        body.update(fields)
        test = lm.create_test(body, actor=PATHOLOGIST)
        made.append(test["id"])
        return test
    yield make
    with db_session() as db:
        for tid in made:
            db.execute("DELETE FROM lab_tests WHERE id = ?", (tid,))


# ── 1. Switched off, draining, no cutover ────────────────────────────────────

def test_with_orb_lab_off_an_activated_lab_order_is_untouched(patient):
    assert config.LAB_ENABLED is not True
    order = _order(patient, "58410-2")
    assert order["status"] == "active"
    assert _items(order["id"]) == [] and _exceptions(order["id"]) == []
    assert _patient_specimens(patient) == []
    # And the sweep refuses to run at all.
    assert los.ensure_intake()["reason"] == "module_not_on"


def test_while_draining_nothing_is_taken_in(patient, monkeypatch):
    with db_session() as db:
        event_id = db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, 't')",
            (utc_iso_now(),)).lastrowid
    try:
        monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
        assert lab_module.module_state() == "draining"
        order = _order(patient, "58410-2")
        assert los.intake(order["id"])["reason"] == "module_not_on"
        assert _items(order["id"]) == []
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_module_events WHERE id = ?", (event_id,))


def test_the_hook_imports_nothing_while_the_module_is_off(patient, monkeypatch):
    called = []
    monkeypatch.setattr(los, "intake_after_activation", lambda *a, **k: called.append(a))
    _order(patient, "2823-3")
    assert called == []


# ── 2. The cutover ───────────────────────────────────────────────────────────

def test_an_order_activated_before_the_cutover_is_never_taken_in(patient, monkeypatch):
    before = _order(patient, "58410-2")         # ORB_LAB off: no hook
    draft = _order(patient, "24325-3", status="draft")
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    with db_session() as db:
        event_id = db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, 't')",
            (utc_iso_now(),)).lastrowid
    try:
        los.ensure_intake()
        assert los.intake(before["id"]) == {"status": "not_eligible", "reason": "before_cutover"}
        assert _items(before["id"]) == []
        # A draft signed after the cutover is activated after it: taken in.
        orders_service.transition_order(draft["id"], "active", DOCTOR_ID)
        assert len(_items(draft["id"])) == 7
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_module_events WHERE id = ?", (event_id,))


def test_no_enabled_event_takes_nothing(patient, monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True)
    monkeypatch.setattr(lab_module, "current_enable_instant", lambda conn: None)
    order = _order(patient, "58410-2")
    assert _items(order["id"]) == []
    assert los.intake(order["id"])["reason"] == "no_cutover"


# ── 3. Panels, specimens and accession numbers ───────────────────────────────

def test_cbc_gives_one_item_per_component_on_one_planned_edta_specimen(lab_on, patient):
    order = _order(patient, "58410-2")
    items = _items(order["id"])
    assert [i["test_id"] for i in items] == [
        "LOINC:718-7", "LOINC:4544-3", "LOINC:789-8", "LOINC:6690-2", "LOINC:777-3"]
    assert {i["panel_test_id"] for i in items} == {"LOINC:58410-2"}
    assert {i["status"] for i in items} == {"awaiting_collection"}
    assert {i["section_id"] for i in items} == {"haematology"}
    specimen_ids = {i["specimen_id"] for i in items}
    assert len(specimen_ids) == 1
    sid = specimen_ids.pop()
    spec = _specimen(sid)
    assert spec["status"] == "planned" and spec["label_print_count"] == 0
    assert spec["specimen_type_code"] == "edta_whole_blood" and spec["container"] == "EDTA tube"
    assert spec["priority"] == "routine" and spec["patient_id"] == patient
    parts = lab_accession.parse_accession(sid)
    assert parts.hospital_day == lab_accession.hospital_day_for(spec["created_at"])
    events = _specimen_events(sid)
    assert [(e["seq"], e["event_type"]) for e in events] == [(1, "planned")]
    audits = _intake_audits(order["id"], "success")
    assert len(audits) == 1 and audits[0]["event_category"] == "clinical_access"
    assert audits[0]["patient_id"] == patient and audits[0]["actor_user_id"] == DOCTOR_ID


def test_ue_and_lft_share_one_serum_specimen_and_the_panel_tube_wins(lab_on, patient):
    ue = _order(patient, "51990-0")
    lft = _order(patient, "24325-3")
    ue_items, lft_items = _items(ue["id"]), _items(lft["id"])
    assert len(ue_items) == 8 and len(lft_items) == 7
    specimens = {i["specimen_id"] for i in ue_items + lft_items}
    assert len(specimens) == 1, "U&E and LFT go in one serum tube"
    sid = specimens.pop()
    assert _specimen(sid)["specimen_type_code"] == "serum"
    # The panel's glucose comes from the panel's serum tube, not a fluoride tube.
    assert any(i["test_id"] == "LOINC:2345-7" for i in ue_items)
    assert [e["event_type"] for e in _specimen_events(sid)] == ["planned", "items_added"]


def test_a_new_order_after_the_label_printed_gets_a_new_specimen(lab_on, patient):
    first = _order(patient, "51990-0")
    sid = _items(first["id"])[0]["specimen_id"]
    with db_session() as db:
        db.execute("UPDATE lab_specimens SET label_print_count = 1, label_printed_at = ? "
                   "WHERE id = ?", (utc_iso_now(), sid))
    second = _order(patient, "24325-3")
    second_sid = {i["specimen_id"] for i in _items(second["id"])}
    assert len(second_sid) == 1 and sid not in second_sid
    assert [e["event_type"] for e in _specimen_events(sid)] == ["planned"]


def test_the_same_test_twice_is_two_tubes_never_one(lab_on, patient):
    first = _order(patient, "10839-9")
    repeat = _order(patient, "10839-9")
    a, b = _items(first["id"]), _items(repeat["id"])
    assert len(a) == 1 and len(b) == 1
    assert a[0]["specimen_id"] != b[0]["specimen_id"]


def test_a_stat_order_is_its_own_tube(lab_on, patient):
    routine = _order(patient, "2160-0")
    stat = _order(patient, "2823-3", priority="stat")
    r, s = _items(routine["id"])[0], _items(stat["id"])[0]
    assert r["specimen_id"] != s["specimen_id"]
    assert _specimen(s["specimen_id"])["priority"] == "stat" and s["priority"] == "stat"


def test_another_patients_planned_tube_is_never_joined(lab_on, patient, _catalogue):
    other = f"PLOI{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
                   (other, "Intake Other", f"MRN-LOI-{uuid.uuid4().hex[:8]}"))
    _catalogue.append(other)
    a = _order(patient, "51990-0")
    b = _order(other, "24325-3")
    assert _items(a["id"])[0]["specimen_id"] != _items(b["id"])[0]["specimen_id"]


def test_imaging_orders_are_not_the_laboratorys(lab_on, patient):
    terminology_service.seed_terminology()
    with db_session() as db:
        row = db.execute("SELECT code_system, code FROM terminology WHERE code_system = 'LOCAL' "
                         "AND code = 'ORB-IMG-CXR'").fetchone()
    order = _order(patient, row["code"], row["code_system"], order_type="imaging")
    assert _items(order["id"]) == [] and _exceptions(order["id"]) == []


# ── 4. Bedside tests stay on the ward (ruling LIS-R18) ───────────────────────

def test_the_dka_order_set_gives_no_specimen_for_ketones_or_vbg(lab_on, patient):
    s = order_sets_service.get_set("dka_initial")
    selections = [{"index": item["index"], "include": item["order_type"] == "lab"}
                  for item in s["items"]]
    applied = order_sets_service.apply_set(patient, "dka_initial", actor_id=DOCTOR_ID,
                                           actor_role="doctor", selections=selections)
    by_code = {}
    for draft in applied["orders"]:
        signed = orders_service.transition_order(draft["id"], "active", DOCTOR_ID)
        by_code[signed["code"]] = signed
    assert set(by_code) == {"ORB-LAB-BLOOD-KETONES", "ORB-LAB-VBG", "2345-7", "51990-0"}
    for bedside in ("ORB-LAB-BLOOD-KETONES", "ORB-LAB-VBG"):
        oid = by_code[bedside]["id"]
        assert _items(oid) == [] and _exceptions(oid) == [], bedside
        assert los.intake(oid) == {"status": "skipped", "reason": "ward_poct"}
    # The ward keeps the free-text result path for the bedside test.
    resulted = orders_service.add_result(by_code["ORB-LAB-BLOOD-KETONES"]["id"],
                                         "Ketones 3.4 mmol/L at the bedside", DOCTOR_ID)
    assert resulted["status"] == "resulted"
    glucose = _items(by_code["2345-7"]["id"])
    ue = _items(by_code["51990-0"]["id"])
    assert len(glucose) == 1 and len(ue) == 8
    assert _specimen(glucose[0]["specimen_id"])["specimen_type_code"] == "fluoride_plasma"
    assert _specimen(ue[0]["specimen_id"])["specimen_type_code"] == "serum"
    assert len(_patient_specimens(patient)) == 2


# ── 5. Unmapped codes are never silent ───────────────────────────────────────

def _unmapped_code():
    """A code terminology validates but the laboratory catalogue lacks."""
    with db_session() as db:
        row = db.execute(
            "SELECT t.code_system, t.code FROM terminology t WHERE t.is_active = 1 "
            "AND t.code_system = 'SNOMED' AND NOT EXISTS (SELECT 1 FROM lab_tests l "
            "WHERE l.code_system = t.code_system AND l.code = t.code) LIMIT 1").fetchone()
    assert row is not None
    return row["code_system"], row["code"]


def test_an_unmapped_code_lands_in_lab_exceptions_once(lab_on, patient):
    system, code = _unmapped_code()
    order = _order(patient, code, system)
    assert _items(order["id"]) == []
    found = _exceptions(order["id"], "unmapped_code")
    assert len(found) == 1 and "not in the laboratory catalogue" in found[0]["detail"]
    assert found[0]["resolved_at"] is None
    assert len(_intake_audits(order["id"], "failure")) == 1
    # Replay and the sweep add nothing.
    assert los.intake(order["id"])["reason"] == "open_exception"
    los.ensure_intake()
    assert len(_exceptions(order["id"])) == 1


def test_a_switched_off_test_and_a_componentless_panel_are_unmapped(lab_on, patient, own_test):
    off = own_test()
    lm.update_test(off["id"], {"active": False}, actor=PATHOLOGIST)
    order = _order(patient, off["code"], "LOCAL")
    assert "switched off" in _exceptions(order["id"], "unmapped_code")[0]["detail"]
    panel = own_test(kind="panel", short_name="Empty panel")
    order2 = _order(patient, panel["code"], "LOCAL")
    assert "no active laboratory component" in \
        _exceptions(order2["id"], "unmapped_code")[0]["detail"]
    assert _items(order["id"]) == [] and _items(order2["id"]) == []


def test_the_laboratory_sees_its_exceptions_list(lab_on, patient):
    system, code = _unmapped_code()
    order = _order(patient, code, system)
    tech = _hdr("lab_technician")
    resp = client.get("/api/lab/exceptions", headers=tech)
    assert resp.status_code == 200, resp.text
    rows = [r for r in resp.json()["exceptions"] if r["order_id"] == order["id"]]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "unmapped_code" and row["patient_id"] == patient
    assert row["kind_label"] == "Code not in the laboratory catalogue"
    assert row["patient_name"] == "Intake Test"
    with db_session() as db:
        audited = db.execute(
            "SELECT event_category FROM audit_logs WHERE action_type = 'lab_exceptions_viewed' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    assert audited["event_category"] == "phi_access"
    assert client.get("/api/lab/exceptions", headers=_hdr("nurse")).status_code == 403
    assert client.get("/api/lab/exceptions").status_code == 401


def _viewed_audit(uid):
    with db_session() as db:
        row = db.execute(
            "SELECT event_category, metadata_json FROM audit_logs "
            "WHERE action_type = 'lab_exceptions_viewed' AND actor_user_id = ? "
            "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    assert row is not None
    return row["event_category"], json.loads(row["metadata_json"])


def test_a_department_account_sees_only_its_own_departments_exceptions(lab_on, patient,
                                                                       _catalogue):
    """lab.read never widens who reads a patient: an account with a department
    sees only that department's patients, like every chart route. The lab
    roles are seeded with no department and stay hospital-wide (LIS-R12)."""
    ward = f"Cardiology LOI {uuid.uuid4().hex[:6]}"
    cardiac = _patient_in(_catalogue, ward, "Cardiac Intake Test")
    system, code = _unmapped_code()
    gm_order = _order(patient, code, system)            # General Medicine
    ward_order = _order(cardiac, code, system)

    def order_ids(headers, **params):
        resp = client.get("/api/lab/exceptions", headers=headers, params=params)
        assert resp.status_code == 200, resp.text
        return {r["order_id"] for r in resp.json()["exceptions"]}, resp.json()["exceptions"]

    # An admin on that ward (the reviewer's probe): its own ward only.
    admin_id, admin = _user_in("admin", ward)
    seen, rows = order_ids(admin)
    assert ward_order["id"] in seen and gm_order["id"] not in seen
    assert {r["patient_id"] for r in rows} == {cardiac}
    category, meta = _viewed_audit(admin_id)
    assert category == "phi_access"
    assert meta["department"] == ward and meta["hospital_wide"] is False
    assert meta["patient_ids"] == [cardiac] and meta["patient_count"] == 1

    # A laboratory account given General Medicine: the other way round.
    tech_id, tech = _user_in("lab_technician", "General Medicine")
    seen, _ = order_ids(tech)
    assert gm_order["id"] in seen and ward_order["id"] not in seen
    assert cardiac not in _viewed_audit(tech_id)[1]["patient_ids"]

    # A laboratory account with no department (how the roles are seeded): both.
    open_id, hospital_wide = _user_in("pathologist", None)
    seen, _ = order_ids(hospital_wide, limit=500)
    assert {gm_order["id"], ward_order["id"]} <= seen
    assert _viewed_audit(open_id)[1]["hospital_wide"] is True

    # The scope is applied before the page limit: older rows from other
    # departments never push this department's row off a one-row page.
    seen, _ = order_ids(admin, limit=1)
    assert seen == {ward_order["id"]}


def test_a_row_no_department_owns_is_hidden_from_a_department_account(lab_on, patient):
    """Fail-closed like patient_in_scope: an exception with no order (so no
    derivable department) is on the hospital-wide list only."""
    with db_session() as db:
        xid = db.execute(
            "INSERT INTO lab_exceptions (kind, order_id, specimen_id, detail, created_at) "
            "VALUES ('rejected_no_decision', NULL, ?, 'test row', ?)",
            (f"SLOI{uuid.uuid4().hex[:8].upper()}", utc_iso_now())).lastrowid
    try:
        _, ward = _user_in("lab_technician", "General Medicine")
        resp = client.get("/api/lab/exceptions", headers=ward, params={"limit": 500})
        assert resp.status_code == 200, resp.text
        assert xid not in {r["id"] for r in resp.json()["exceptions"]}
        _, everywhere = _user_in("lab_technician", None)
        resp = client.get("/api/lab/exceptions", headers=everywhere, params={"limit": 500})
        assert xid in {r["id"] for r in resp.json()["exceptions"]}
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_exceptions WHERE id = ?", (xid,))


def test_the_exceptions_reader_requires_the_callers_scope():
    """dept is a required keyword, so no future caller can forget the scope."""
    with db_session() as db:
        with pytest.raises(TypeError):
            los.list_open_exceptions(db, limit=10)


def test_the_exceptions_list_is_404_while_the_module_is_off():
    resp = client.get("/api/lab/exceptions", headers=_hdr("lab_technician"))
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "module_disabled"


def test_the_exceptions_list_stays_open_while_the_module_drains(monkeypatch):
    monkeypatch.setattr(config, "_MODULE_DRAIN_CHECKS", {"lab": lambda: True})
    resp = client.get("/api/lab/exceptions", headers=_hdr("lab_technician"))
    assert resp.status_code == 200, resp.text
    assert "exceptions" in resp.json()


# ── 6. Idempotent under replay and racing ────────────────────────────────────

def test_replay_and_the_sweep_change_nothing_once_taken_in(lab_on, patient):
    order = _order(patient, "58410-2")
    before = _items(order["id"])
    assert los.intake(order["id"]) == {"status": "already", "reason": "items_exist"}
    los.ensure_intake()
    orders_service.transition_order(order["id"], "active", DOCTOR_ID)  # idempotent no-op
    assert _items(order["id"]) == before
    assert len(_intake_audits(order["id"], "success")) == 1


def test_the_hook_racing_the_sweep_gives_one_set_of_items(lab_on, patient, no_hook):
    order = _order(patient, "58410-2")
    assert _items(order["id"]) == []
    errors: list[BaseException] = []
    start = threading.Barrier(6)

    def run(fn):
        try:
            start.wait(timeout=30)
            fn()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    jobs = [lambda: los.intake_after_activation(order["id"], actor_id=DOCTOR_ID)] * 3 \
        + [los.ensure_intake] * 3
    threads = [threading.Thread(target=run, args=(job,)) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors
    items = _items(order["id"])
    assert len(items) == 5 and len({i["specimen_id"] for i in items}) == 1
    assert len(_intake_audits(order["id"], "success")) == 1
    assert _exceptions(order["id"]) == []


# ── 7. The hook never raises; failures are recorded and retried ──────────────

def test_a_failing_intake_never_breaks_the_order_and_the_sweep_retries(lab_on, patient,
                                                                       monkeypatch):
    real = los._intake_in_conn

    def boom(*a, **k):
        raise RuntimeError("laboratory tables locked")
    monkeypatch.setattr(los, "_intake_in_conn", boom)
    order = _order(patient, "58410-2")
    assert order["status"] == "active"
    failed = _exceptions(order["id"], "intake_failed")
    assert len(failed) == 1 and failed[0]["resolved_at"] is None
    assert "retries it automatically" in failed[0]["detail"]
    monkeypatch.setattr(los, "_intake_in_conn", real)
    summary = los.ensure_intake()
    assert summary["taken"] >= 1
    assert len(_items(order["id"])) == 5
    healed = _exceptions(order["id"], "intake_failed")[0]
    assert healed["resolved_at"] and healed["resolved_by"] == "system"


def test_an_intake_whose_audit_fails_commits_nothing(lab_on, patient, monkeypatch):
    day = lab_accession.hospital_day_for(None).isoformat()
    with db_session() as db:
        row = db.execute("SELECT last_seq FROM lab_accession_counters WHERE hospital_day = ?",
                         (day,)).fetchone()
    seq_before = row[0] if row else 0

    def refuse(*a, **k):
        raise AuditWriteError("audit store unavailable")
    monkeypatch.setattr(los, "log_audit_event", refuse)
    order = _order(patient, "58410-2")
    assert order["status"] == "active"
    assert _items(order["id"]) == [] and _patient_specimens(patient) == []
    with db_session() as db:
        row = db.execute("SELECT last_seq FROM lab_accession_counters WHERE hospital_day = ?",
                         (day,)).fetchone()
    assert (row[0] if row else 0) == seq_before, "the accession number rolled back too"
    assert len(_exceptions(order["id"], "intake_failed")) == 1


def test_used_up_accession_numbers_refuse_loudly_and_plan_nothing(lab_on, patient,
                                                                   monkeypatch, caplog):
    monkeypatch.setattr(lab_accession, "hospital_day_for", lambda at=None: date(2033, 1, 1))
    with db_session() as db:
        db.execute("INSERT OR REPLACE INTO lab_accession_counters (hospital_day, last_seq) "
                   "VALUES ('2033-01-01', 99999)")
    try:
        order = _order(patient, "2823-3")
        assert order["status"] == "active"
        assert _items(order["id"]) == [] and _patient_specimens(patient) == []
        failed = _exceptions(order["id"], "intake_failed")
        assert len(failed) == 1 and "used up" in failed[0]["detail"]
        assert any("used up" in r.getMessage() for r in caplog.records
                   if r.name == "orb.lab.accession")
        with pytest.raises(lab_accession.AccessionOverflow):
            los.intake(order["id"], trigger="sweep")
        assert len(_exceptions(order["id"], "intake_failed")) == 1
    finally:
        with db_session() as db:
            db.execute("DELETE FROM lab_accession_counters WHERE hospital_day = '2033-01-01'")


# ── 8. The sweep ─────────────────────────────────────────────────────────────

def test_the_sweep_takes_an_order_the_hook_missed(lab_on, patient, no_hook):
    order = _order(patient, "24325-3")
    assert _items(order["id"]) == []
    summary = los.ensure_intake()
    assert summary["taken"] >= 1 and len(_items(order["id"])) == 7
    audit = _intake_audits(order["id"], "success")[0]
    assert audit["actor_user_id"] == "system"


def test_startup_runs_the_sweep_while_the_module_is_on(lab_on, patient, no_hook):
    order = _order(patient, "2160-0")
    summary = lab_module._startup_sync()
    assert summary["cutover"] is None, "the test's own 'enabled' event is the latest"
    assert summary["intake"]["taken"] >= 1
    assert len(_items(order["id"])) == 1


def test_the_sweep_loop_is_registered_for_the_on_state_only():
    assert "services.lab_order_service" in lab_module._LOOP_MODULES
    loops = {name: states for name, _f, states in lab_module._LOOPS}
    assert loops.get("intake-sweep") == ("on",)


def test_a_reclassification_applies_only_to_orders_activated_afterwards(lab_on, patient,
                                                                        own_test):
    bedside = own_test(performed_at="ward_poct", section_id=None,
                       specimen_type_code="capillary_blood", short_name="Bedside")
    early = _order(patient, bedside["code"], "LOCAL")
    assert _items(early["id"]) == []
    lm.update_test(bedside["id"], {"performed_at": "lab", "section_id": "biochemistry"},
                   actor=PATHOLOGIST)
    los.ensure_intake()
    assert _items(early["id"]) == [], "activated as a bedside test: stays on the ward"
    late = _order(patient, bedside["code"], "LOCAL")
    assert len(_items(late["id"])) == 1, "activated after the change: a laboratory test"


def test_the_sweep_leaves_an_order_whose_test_changed_unless_the_hook_failed(lab_on, patient,
                                                                             own_test,
                                                                             no_hook):
    test = own_test()
    order = _order(patient, test["code"], "LOCAL")
    lm.update_test(test["id"], {"name": f"{test['name']} renamed"}, actor=PATHOLOGIST)
    los.ensure_intake()
    assert _items(order["id"]) == []
    assert los.intake(order["id"], trigger="sweep")["reason"] == \
        "catalogue_changed_after_activation"
    # The hook recorded a failure: the retry takes the order in and heals it.
    los.record_exception("intake_failed", order["id"], "test failure")
    los.ensure_intake()
    assert len(_items(order["id"])) == 1
    assert _exceptions(order["id"], "intake_failed")[0]["resolved_at"]


# ── 9. The sweep is never starved by orders it leaves alone ──────────────────

def test_orders_the_sweep_leaves_alone_never_use_up_its_batch(lab_on, patient, own_test,
                                                              no_hook):
    """Bedside orders stay active with no items for as long as the ward keeps
    them open, and so does an order whose test changed after activation. A
    plain oldest-first batch filled with them and never reached the order the
    hook really missed; the batch here is one order, and the missed one is
    the newest."""
    bedside = [_order(patient, "ORB-LAB-BLOOD-KETONES", "LOCAL") for _ in range(3)]
    renamed = []
    for _ in range(2):
        test = own_test()
        renamed.append(_order(patient, test["code"], "LOCAL"))
        lm.update_test(test["id"], {"name": f"{test['name']} renamed"}, actor=PATHOLOGIST)
    missed = _order(patient, "2160-0")
    summary = los.ensure_intake(limit=1)
    assert summary["considered"] == 1 and summary["taken"] == 1, summary
    # The renamed tests were read, never locked. (An earlier test's order
    # activated within the listing's one second of slack below the cutover
    # is settled the same way, so this counts at least, not exactly.)
    assert summary["settled"] >= 2, summary
    assert len(_items(missed["id"])) == 1
    for order in bedside + renamed:
        assert _items(order["id"]) == [] and _exceptions(order["id"]) == []


def test_the_sweep_lists_no_bedside_order_unless_its_hook_failed(lab_on, patient, no_hook):
    order = _order(patient, "ORB-LAB-VBG", "LOCAL")
    with db_session() as db:
        cutover = lab_module.current_enable_instant(db)
        assert order["id"] not in los._sweep_candidates(db, cutover, 100)
    los.record_exception("intake_failed", order["id"], "test failure")
    with db_session() as db:
        assert order["id"] in los._sweep_candidates(db, cutover, 100)


def test_a_retry_that_finds_a_bedside_test_closes_the_stale_failure(lab_on, patient, no_hook):
    order = _order(patient, "ORB-LAB-BLOOD-KETONES", "LOCAL")
    los.record_exception("intake_failed", order["id"], "test failure")
    summary = los.ensure_intake()
    assert summary["skipped"] >= 1
    assert _items(order["id"]) == []
    failed = _exceptions(order["id"], "intake_failed")
    assert len(failed) == 1 and failed[0]["resolved_at"] and failed[0]["resolved_by"] == "system"
    assert "bedside" in failed[0]["resolution"]
    with db_session() as db:
        cutover = lab_module.current_enable_instant(db)
        assert order["id"] not in los._sweep_candidates(db, cutover, 100)


def test_a_retry_that_finds_an_unmapped_code_leaves_one_open_entry(lab_on, patient, no_hook):
    system, code = _unmapped_code()
    order = _order(patient, code, system)
    los.record_exception("intake_failed", order["id"], "test failure")
    los.ensure_intake()
    open_rows = [x for x in _exceptions(order["id"]) if x["resolved_at"] is None]
    assert [x["kind"] for x in open_rows] == ["unmapped_code"]
    assert _exceptions(order["id"], "intake_failed")[0]["resolved_at"]


def test_one_unreadable_order_never_stops_the_sweep(lab_on, patient, no_hook, monkeypatch):
    bad = _order(patient, "2823-3")
    good = _order(patient, "2160-0")
    real = los._assess

    def assess(conn, order_id, *, trigger):
        if order_id == bad["id"]:
            raise RuntimeError("unreadable row")
        return real(conn, order_id, trigger=trigger)
    monkeypatch.setattr(los, "_assess", assess)
    summary = los.ensure_intake()
    assert summary["taken"] >= 1 and len(_items(good["id"])) == 1
    assert _items(bad["id"]) == []
    assert len(_exceptions(bad["id"], "intake_failed")) == 1
