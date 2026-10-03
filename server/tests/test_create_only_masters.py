"""'Add' forms create; they never overwrite.

Every master below is keyed on a hospital code and written by an upsert, and
the screens' Add forms sent nulls for every field nobody typed. So adding a
code already in use silently replaced that record: an equipment tag took a new
name and lost its serial number, a re-registered bed lost the bed label the
discharge sweep matches on, and the price list's New item un-priced a code the
desk had been billing. A create now returns 409 naming what holds the code and
changes nothing; an update keeps what is on file wherever the form sent a blank.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app
from services import biomedical_service as bio
from services import billing_masters, billing_service
from services import housekeeping_service as hk
from services import procurement_service as proc
from services import workforce_service as wf

client = TestClient(app)


def _uid() -> str:
    return uuid.uuid4().hex[:6].upper()


@pytest.fixture(scope="module")
def admin():
    uid = f"COADM{_uid()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, is_active) "
                   "VALUES (?, 'Create-only admin', 'admin', 'x', 1)", (uid,))
    return {"Authorization": f"Bearer {create_token(uid, 'admin')}"}


@pytest.fixture(autouse=True)
def _modules_on(monkeypatch):
    for flag in ("BIOMEDICAL_ENABLED", "PROCUREMENT_ENABLED", "WORKFORCE_ENABLED",
                 "HOUSEKEEPING_ENABLED", "BILLING_ENABLED"):
        monkeypatch.setattr(config, flag, True)


def _refused(resp, *, naming: str):
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "already_exists"
    assert naming in detail["message"]


# ── Equipment ───────────────────────────────────────────────────────────────

def test_adding_an_existing_asset_tag_is_refused_and_changes_nothing(admin):
    tag = f"COBIO-{_uid()}"
    bio.upsert_asset(asset_tag=tag, name="Infusion pump 14", ward="ICU",
                     serial_number="SN-14")
    resp = client.post("/api/biomedical/assets", headers=admin, json={
        "asset_tag": tag.lower(), "name": "Something else", "create_only": True})
    _refused(resp, naming="Infusion pump 14")
    row = bio.get_asset(asset_tag=tag)
    assert row["name"] == "Infusion pump 14"
    assert row["serial_number"] == "SN-14" and row["ward"] == "ICU"


def test_an_asset_update_keeps_fields_the_form_left_blank():
    tag = f"COBIO-{_uid()}"
    bio.upsert_asset(asset_tag=tag, name="Ventilator", serial_number="SN-9",
                     manufacturer="Acme", purchase_cost_paise=150000)
    again = bio.upsert_asset(asset_tag=tag, name="Ventilator 2")
    assert again["name"] == "Ventilator 2"
    assert again["serial_number"] == "SN-9" and again["manufacturer"] == "Acme"
    assert again["purchase_cost_paise"] == 150000 and again["cost_is_configured"] == 1


# ── Procurement ─────────────────────────────────────────────────────────────

def test_adding_an_existing_vendor_code_is_refused(admin):
    code = f"COV{_uid()}"
    proc.upsert_vendor(code=code, name="Lifeline Surgicals", gstin="29ABCDE1234F1Z5")
    resp = client.post("/api/procurement/vendors", headers=admin,
                       json={"code": code, "name": "Other", "create_only": True})
    _refused(resp, naming="Lifeline Surgicals")
    with db_session() as db:
        row = db.execute("SELECT name, gstin FROM vendors WHERE code = ?", (code,)).fetchone()
    assert (row["name"], row["gstin"]) == ("Lifeline Surgicals", "29ABCDE1234F1Z5")


def test_a_vendor_update_keeps_fields_the_form_left_blank():
    code = f"COV{_uid()}"
    proc.upsert_vendor(code=code, name="Vendor A", gstin="29ABCDE1234F1Z5",
                       payment_terms_days=30)
    again = proc.upsert_vendor(code=code, name="Vendor A Ltd")
    assert again["gstin"] == "29ABCDE1234F1Z5" and again["payment_terms_days"] == 30


def test_adding_an_existing_item_code_is_refused(admin):
    code = f"COI{_uid()}"
    proc.upsert_inventory_item(code=code, name="Gauze swab 10x10 cm", reorder_level_qty=50)
    resp = client.post("/api/procurement/items", headers=admin,
                       json={"code": code, "name": "Gauze, other", "create_only": True})
    _refused(resp, naming="Gauze swab 10x10 cm")
    assert proc.get_inventory_item(code)["reorder_level_qty"] == 50


def test_adding_an_existing_store_code_is_refused():
    code = f"COS{_uid()}"
    proc.upsert_store(code=code, name="Ward store A", ward="GM-1")
    with pytest.raises(proc.ProcurementError) as err:
        proc.upsert_store(code=code, name="Other", create_only=True)
    assert err.value.code == "already_exists"
    assert proc.get_store(code)["ward"] == "GM-1"


# ── Workforce ───────────────────────────────────────────────────────────────

def test_adding_an_existing_employee_code_is_refused(admin):
    code = f"COE{_uid()}"
    wf.upsert_employee(employee_code=code, full_name="Anita Das", department="Nursing",
                       payroll_bureau_ref="PB-77")
    resp = client.post("/api/workforce/employees", headers=admin, json={
        "employee_code": code, "full_name": "Someone Else", "create_only": True})
    _refused(resp, naming="Anita Das")
    with db_session() as db:
        row = db.execute("SELECT full_name, payroll_bureau_ref FROM staff_employment "
                         "WHERE employee_code = ?", (code,)).fetchone()
    assert (row["full_name"], row["payroll_bureau_ref"]) == ("Anita Das", "PB-77")


def test_an_employee_update_keeps_blanks_but_writes_meaningful_nulls():
    code = f"COE{_uid()}"
    wf.upsert_employee(employee_code=code, full_name="Ravi K", department="Kitchen",
                       weekly_off_day=6, date_of_exit="2030-01-01",
                       date_of_joining="2026-01-01")
    again = wf.upsert_employee(employee_code=code, full_name="Ravi Kumar")
    assert again["department"] == "Kitchen"
    # A blank exit date means still employed; a blank day off means it rotates.
    assert again["date_of_exit"] is None
    assert again["weekly_off_day"] is None


# ── Housekeeping ────────────────────────────────────────────────────────────

def test_registering_an_existing_unit_is_refused_and_keeps_its_bed_label(admin):
    code = f"CO-HK-{_uid()}"
    ward = f"CO Ward {_uid()}"
    hk.register_unit(unit_code=code, ward=ward, bed_label="03")
    resp = client.post("/api/housekeeping/units", headers=admin,
                       json={"unit_code": code, "ward": ward, "create_only": True})
    _refused(resp, naming="bed 03")
    units = [u for u in hk.list_units(ward) if u["unit_code"] == code]
    assert units[0]["bed_label"] == "03"


def test_a_ward_reimport_without_bed_labels_does_not_wipe_them():
    """The discharge sweep matches a discharge to its bed by this label."""
    code = f"CO-HK-{_uid()}"
    ward = f"CO Ward {_uid()}"
    hk.register_unit(unit_code=code, ward=ward, bed_label="07", room="R2")
    again = hk.register_unit(unit_code=code, ward=ward)
    assert again["bed_label"] == "07" and again["room"] == "R2"


# ── Billing price list ──────────────────────────────────────────────────────

def test_new_item_on_an_existing_code_is_refused_and_keeps_its_price():
    code = f"CO-SVC-{_uid()}"
    billing_masters.upsert_service_item(code=code, name="OPD consultation",
                                        category="consultation", default_rate_paise=80000)
    with pytest.raises(billing_service.BillingError) as err:
        billing_service.create_service_item(code=code, name="Other", category="consultation",
                                            default_rate_paise=None)
    assert err.value.code == "already_exists"
    assert "OPD consultation" in str(err.value)
    item = billing_masters.get_service_item(code)
    assert item["rate_is_configured"] == 1 and item["default_rate_paise"] == 80000


def test_new_item_route_answers_409(admin):
    code = f"CO-SVC-{_uid()}"
    billing_masters.upsert_service_item(code=code, name="Dressing, small",
                                        category="procedure", default_rate_paise=25000)
    resp = client.post("/api/billing/service-items", headers=admin, json={
        "code": code, "name": "Dressing", "category": "procedure", "create_only": True})
    _refused(resp, naming="Dressing, small")
    fresh = f"CO-SVC-{_uid()}"
    ok = client.post("/api/billing/service-items", headers=admin, json={
        "code": fresh, "name": "New thing", "category": "procedure", "create_only": True})
    assert ok.status_code == 200, ok.text
