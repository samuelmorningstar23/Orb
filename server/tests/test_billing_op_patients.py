"""Outpatient billing at the counter (opd-14, W8-B).

What this file defends:

  1. FLAG OFF, NOTHING NEW. GET /api/billing/op-patients is 404
     feature_disabled while the 'opd' feature is off, for an account holding
     every billing permission, and 401 with no session.
  2. A WALK-IN IS SELECTABLE. An outpatient with a pending outpatient line on
     the day is listed with that day's visits and the ids of exactly those
     lines; a line from another day, an inpatient's stay line and a billed or
     cancelled line are not.
  3. THE BILL TAKES ONLY THAT PATIENT'S OUTPATIENT LINES. 'Bill this visit'
     (encounter_type 'op', no admission) bills the named outpatient lines;
     naming an inpatient line, or a line recorded as inpatient with no
     admission, is refused with nothing billed; the outpatient sweep never
     takes such a line; an outpatient bill with an admission is refused.
  4. THE CASHIER STILL CANNOT REACH THE BILLING SCREENS (403 on the list and
     on every billing read), whatever the switch says.
  5. THE READ IS AUDITED: one fail-closed phi_access row naming every listed
     patient when anyone is listed, none for an empty day.
  6. SCOPE. A department-scoped billing account sees only patients inside its
     department by the house rule, which is what the per-patient billing
     routes would let it open; an outpatient with no admission is not one.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import billing_masters, billing_service
from services.billing_service import BillingError

client = TestClient(app)

DEPT = "Op Bill Cardiology"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department=None):
    uid = f"OPB{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, f"Op bill {role} {uid[-4:]}", role, department),
        )
    return uid


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _patient(name=None):
    pid = f"OPBP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, status) "
            "VALUES (?, ?, ?, 40, 'F', 'registered')",
            (pid, name or f"Walk In {pid[-4:]}", f"UH-{uuid.uuid4().hex[:8].upper()}"),
        )
    return pid


def _admission(pid, department=DEPT, status="active"):
    aid = f"OPBA{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
            "status, created_at) VALUES (?, ?, 'D001', ?, 'W1', '3', ?, ?)",
            (aid, pid, department, status, "2026-08-10T09:00:00"),
        )
    return aid


def _visit(pid, day, *, department=DEPT, token=1, status="waiting"):
    vid = f"OPBV{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO opd_visits (id, patient_id, department, doctor_id, doctor_name, "
            "visit_date, token_number, status, registered_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
            (vid, pid, department, day, token, status, f"{day}T04:00:00+00:00"),
        )
    return vid


@pytest.fixture(scope="module")
def code():
    """A priced, GST-exempt consultation item (TST- prefix: test_money.py
    excludes it from its no-seeded-prices count)."""
    c = f"TST-OPB{uuid.uuid4().hex[:6].upper()}"
    billing_masters.upsert_service_item(code=c, name="Outpatient consultation",
                                        category="consultation", default_rate_paise=60000)
    return c


@pytest.fixture(scope="module")
def staff():
    return {
        "admin": _mk_user("admin"),
        "admin_scoped": _mk_user("admin", DEPT),
        "cashier": _mk_user("cashier"),
        "doctor": _mk_user("doctor", DEPT),
    }


@pytest.fixture
def opd_on(monkeypatch):
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", True)
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "OPD_ENABLED", True)
    assert config.feature_enabled("opd")


@pytest.fixture
def opd_off(monkeypatch):
    monkeypatch.setattr(config, "OPD_ENABLED", False)
    assert not config.feature_enabled("opd")


def _today() -> str:
    return billing_service.hospital_today().isoformat()


def _list(uid, role="admin", day=None):
    path = "/api/billing/op-patients" + (f"?date={day}" if day else "")
    return client.get(path, headers=_auth(uid, role))


def _entry(body, pid):
    return next((p for p in body["patients"] if p["id"] == pid), None)


def _audit_rows(action):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? ORDER BY id", (action,)).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Flag off
# ═════════════════════════════════════════════════════════════════════════════

def test_the_list_is_absent_while_the_feature_is_off(staff, opd_off):
    resp = _list(staff["admin"])
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "feature_disabled"
    assert resp.json()["detail"]["feature"] == "opd"


def test_no_session_is_401_on_or_off(opd_on):
    assert client.get("/api/billing/op-patients").status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# 2. Who is listed
# ═════════════════════════════════════════════════════════════════════════════

def test_a_walk_in_with_a_pending_line_is_listed_with_that_days_lines(staff, opd_on, code):
    walk_in = _patient("Asha Walkin")
    _visit(walk_in, _today(), token=7)
    line = billing_service.add_charge(patient_id=walk_in, service_code=code)
    assert line["encounter_type"] == "op" and line["admission_id"] is None

    # Someone else's stay line and a line already cancelled are never listed.
    inpatient = _patient("Bela Inpatient")
    aid = _admission(inpatient)
    billing_service.add_charge(patient_id=inpatient, service_code=code, admission_id=aid)
    gone = _patient("Chand Cancelled")
    cancelled = billing_service.add_charge(patient_id=gone, service_code=code)
    billing_service.cancel_charge(cancelled["id"], reason="Posted in error")

    resp = _list(staff["admin"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["date"] == _today()
    entry = _entry(body, walk_in)
    assert entry is not None
    assert entry["charge_ids"] == [line["id"]]
    assert entry["pending_charge_count"] == 1 and entry["unpriced_charge_count"] == 0
    assert entry["priced_total_paise"] == 60000
    assert entry["inpatient_now"] is False
    assert entry["visits"] == [{"department": DEPT, "doctor_name": None,
                                "token_number": 7, "status": "waiting"}]
    assert entry["name"] == "Asha Walkin" and entry["mrn"].startswith("UH-")
    assert _entry(body, inpatient) is None
    assert _entry(body, gone) is None


def test_a_line_from_another_day_is_listed_on_its_own_day_only(staff, opd_on, code):
    pid = _patient()
    old_day = (billing_service.hospital_today() - timedelta(days=40)).isoformat()
    line = billing_service.add_charge(patient_id=pid, service_code=code, service_date=old_day)
    assert _entry(_list(staff["admin"]).json(), pid) is None
    entry = _entry(_list(staff["admin"], day=old_day).json(), pid)
    assert entry is not None and entry["charge_ids"] == [line["id"]]


def test_the_day_is_the_hospitals_not_the_utc_date(staff, opd_on, code, monkeypatch):
    """01:30 IST on 10 March is 20:00 UTC on 9 March: the line is the 10th's."""
    monkeypatch.setattr(billing_service, "hospital_timezone", lambda: ZoneInfo("Asia/Kolkata"))
    pid = _patient()
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO encounter_charges (patient_id, admission_id, encounter_type, "
            "service_code, description, quantity_milli, unit_rate_paise, rate_is_configured, "
            "gross_paise, total_paise, status, source, service_date) "
            "VALUES (?, NULL, 'op', ?, 'Late consultation', 1000, 60000, 1, 60000, 60000, "
            "'pending', 'manual', '2026-03-09T20:00:00+00:00')", (pid, code))
        charge_id = cur.lastrowid
    assert _entry(_list(staff["admin"], day="2026-03-09").json(), pid) is None
    entry = _entry(_list(staff["admin"], day="2026-03-10").json(), pid)
    assert entry is not None and entry["charge_ids"] == [charge_id]


def test_an_unpriced_line_is_counted_and_never_priced_at_zero(staff, opd_on):
    unpriced = f"TST-OPBU{uuid.uuid4().hex[:6].upper()}"
    billing_masters.upsert_service_item(code=unpriced, name="Unpriced item")
    pid = _patient()
    with db_session() as db:
        db.execute(
            "INSERT INTO encounter_charges (patient_id, admission_id, encounter_type, "
            "service_code, description, rate_is_configured, status, source, service_date) "
            "VALUES (?, NULL, 'op', ?, 'Consultation', 0, 'pending', 'manual', ?)",
            (pid, unpriced, _today()))
    entry = _entry(_list(staff["admin"]).json(), pid)
    assert entry["unpriced_charge_count"] == 1
    assert entry["priced_total_paise"] == 0


def test_a_bad_date_is_422(staff, opd_on):
    resp = _list(staff["admin"], day="10-03-2026")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_input"


# ═════════════════════════════════════════════════════════════════════════════
# 3. The bill takes only that patient's outpatient lines
# ═════════════════════════════════════════════════════════════════════════════

def test_bill_this_visit_bills_exactly_the_outpatient_lines(staff, opd_on, code):
    pid = _patient()
    _visit(pid, _today())
    first = billing_service.add_charge(patient_id=pid, service_code=code)
    second = billing_service.add_charge(patient_id=pid, service_code=code, quantity_milli=2000)
    resp = client.post(
        f"/api/billing/patients/{pid}/invoices",
        json={"invoice_type": "final", "encounter_type": "op",
              "charge_ids": [first["id"], second["id"]]},
        headers=_auth(staff["admin"], "admin"))
    assert resp.status_code == 200, resp.text
    invoice = resp.json()["invoice"]
    assert invoice["encounter_type"] == "op" and invoice["admission_id"] is None
    assert sorted(l["encounter_charge_id"] for l in invoice["lines"]) == sorted(
        [first["id"], second["id"]])
    assert invoice["payable_paise"] == 180000
    # Billed lines leave the counter list.
    assert _entry(_list(staff["admin"]).json(), pid) is None


def test_an_inpatient_line_is_never_put_on_an_outpatient_bill(staff, opd_on, code):
    pid = _patient()
    aid = _admission(pid, status="discharged")
    op_line = billing_service.add_charge(patient_id=pid, service_code=code)
    stay_line = billing_service.add_charge(patient_id=pid, service_code=code, admission_id=aid)
    # Recorded as inpatient but carrying no admission: still not an outpatient line.
    stray = billing_service.add_charge(patient_id=pid, service_code=code, encounter_type="ip")
    assert stray["admission_id"] is None and stray["encounter_type"] == "ip"

    for named in ([op_line["id"], stay_line["id"]], [op_line["id"], stray["id"]]):
        resp = client.post(
            f"/api/billing/patients/{pid}/invoices",
            json={"invoice_type": "final", "encounter_type": "op", "charge_ids": named},
            headers=_auth(staff["admin"], "admin"))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "charge_encounter_mismatch"
    assert all(c["status"] == "pending"
               for c in billing_service.list_charges(patient_id=pid))

    # The outpatient sweep (no ids named) takes the outpatient line only.
    invoice = billing_service.create_invoice(patient_id=pid, encounter_type="op")
    assert [l["encounter_charge_id"] for l in invoice["lines"]] == [op_line["id"]]
    statuses = {c["id"]: c["status"] for c in billing_service.list_charges(patient_id=pid)}
    assert statuses == {op_line["id"]: "billed", stay_line["id"]: "pending",
                        stray["id"]: "pending"}


def test_an_outpatient_bill_never_carries_an_admission(opd_on, code):
    pid = _patient()
    aid = _admission(pid)
    billing_service.add_charge(patient_id=pid, service_code=code, admission_id=aid)
    with pytest.raises(BillingError) as err:
        billing_service.create_invoice(patient_id=pid, admission_id=aid, encounter_type="op")
    assert err.value.code == "op_bill_with_admission"


def test_another_patients_line_is_refused(staff, opd_on, code):
    mine, theirs = _patient(), _patient()
    line = billing_service.add_charge(patient_id=theirs, service_code=code)
    resp = client.post(
        f"/api/billing/patients/{mine}/invoices",
        json={"invoice_type": "final", "encounter_type": "op", "charge_ids": [line["id"]]},
        headers=_auth(staff["admin"], "admin"))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "charge_patient_mismatch"


def test_the_counter_form_route_still_bills_one_outpatient_visit(staff, opd_on, code):
    pid = _patient()
    resp = client.post(
        f"/api/billing/patients/{pid}/opd-invoice",
        json={"lines": [{"service_code": code}, {"service_code": code, "quantity_milli": 2000}]},
        headers=_auth(staff["admin"], "admin"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["invoice"]["encounter_type"] == "op"
    assert body["invoice"]["status"] == "issued"
    assert len(body["charge_ids"]) == 2


def test_unnamed_callers_are_unchanged(opd_off, code):
    """No encounter named: the old sweep, which takes every unlinked line."""
    pid = _patient()
    op_line = billing_service.add_charge(patient_id=pid, service_code=code)
    stray = billing_service.add_charge(patient_id=pid, service_code=code, encounter_type="ip")
    invoice = billing_service.create_invoice(patient_id=pid)
    assert sorted(l["encounter_charge_id"] for l in invoice["lines"]) == sorted(
        [op_line["id"], stray["id"]])


# ═════════════════════════════════════════════════════════════════════════════
# 4. The cashier
# ═════════════════════════════════════════════════════════════════════════════

def test_the_cashier_still_cannot_reach_the_billing_screens(staff, opd_on):
    headers = _auth(staff["cashier"], "cashier")
    for path in ("/api/billing/op-patients", "/api/billing/config",
                 "/api/billing/invoices", "/api/billing/service-items"):
        resp = client.get(path, headers=headers)
        assert resp.status_code == 403, path
        assert resp.json()["detail"]["missing"] == ["billing.read"], path


def test_a_clinician_cannot_read_the_counter_list(staff, opd_on):
    assert _list(staff["doctor"], role="doctor").status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# 5. The audit
# ═════════════════════════════════════════════════════════════════════════════

def test_a_listing_is_audited_with_every_listed_patient(staff, opd_on, code):
    pid = _patient()
    billing_service.add_charge(patient_id=pid, service_code=code)
    before = len(_audit_rows("billing_op_patients_listed"))
    body = _list(staff["admin"]).json()
    rows = _audit_rows("billing_op_patients_listed")
    assert len(rows) == before + 1
    row = rows[-1]
    assert row["event_category"] == "phi_access" and row["actor_user_id"] == staff["admin"]
    meta = json.loads(row["metadata_json"] or "{}")
    assert sorted(meta["patient_ids"]) == sorted(p["id"] for p in body["patients"])
    assert pid in meta["patient_ids"]
    assert meta["visit_date"] == _today()


def test_an_empty_day_writes_no_row(staff, opd_on):
    before = len(_audit_rows("billing_op_patients_listed"))
    resp = _list(staff["admin"], day="2001-01-01")
    assert resp.status_code == 200 and resp.json()["patients"] == []
    assert len(_audit_rows("billing_op_patients_listed")) == before


def test_an_unwritable_audit_returns_nothing(staff, opd_on, code, monkeypatch):
    from routes import billing_routes
    from services.audit_service import AuditWriteError

    pid = _patient()
    billing_service.add_charge(patient_id=pid, service_code=code)

    def refuse(**_kw):
        raise AuditWriteError("audit chain unavailable")

    monkeypatch.setattr(billing_routes, "log_phi_read", refuse)
    resp = _list(staff["admin"])
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "audit_unavailable"
    assert "patients" not in resp.json()


# ═════════════════════════════════════════════════════════════════════════════
# 6. Scope
# ═════════════════════════════════════════════════════════════════════════════

def test_a_scoped_billing_account_sees_only_its_departments_patients(staff, opd_on, code):
    walk_in = _patient()
    billing_service.add_charge(patient_id=walk_in, service_code=code)
    # A discharged Cardiology patient with an outpatient line after the stay.
    former = _patient()
    _admission(former, status="discharged")
    line = billing_service.add_charge(patient_id=former, service_code=code)
    elsewhere = _patient()
    _admission(elsewhere, department="Op Bill Oncology", status="discharged")
    billing_service.add_charge(patient_id=elsewhere, service_code=code)

    body = _list(staff["admin_scoped"]).json()
    ids = {p["id"] for p in body["patients"]}
    assert former in ids and _entry(body, former)["charge_ids"] == [line["id"]]
    assert walk_in not in ids
    assert elsewhere not in ids
    # The unscoped desk sees all three.
    everyone = {p["id"] for p in _list(staff["admin"]).json()["patients"]}
    assert {walk_in, former, elsewhere} <= everyone
