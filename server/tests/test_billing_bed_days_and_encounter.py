"""Bed days end at discharge and follow the ward (F100); an inpatient charge is
billed as inpatient (F096).

F100. post_bed_day_charges ended every run at today's date, because an
Orb-native discharge leaves admissions.discharged_at NULL (its time is on the
discharge summary), so a run a week after discharge billed a week more; and it
priced every day at the admission's CURRENT ward, so a CCU stay that ended on a
general ward was billed entirely at general rates.

F096. The billing desk passed no admission, so every charge was classified as
outpatient. The picker now carries admission_id (vigil /patients, /discharged)
and the service classifies an admission-linked charge as 'ip'.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from services import billing_masters, billing_service
from services.billing_service import BillingError
from services.money import rupees


def _patient() -> str:
    pid = f"PBED{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, status) VALUES (?, ?, ?, 'GEN', '3', 'admitted')",
            (pid, "Bed Day Test", f"MRN-{pid}"),
        )
    return pid


def _admission(pid: str, *, ward: str, created_at: str, status: str = "active") -> str:
    aid = f"ADMBED{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('BEDDOC', 'Dr Bed', 'doctor', 'x')")
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status, created_at) "
            "VALUES (?, ?, 'BEDDOC', 'General Medicine', ?, '3', ?, ?)",
            (aid, pid, ward, status, created_at),
        )
    return aid


@pytest.fixture()
def bed_code():
    """A bed item priced per ward class on a default plan of this test's own:
    CCU ₹8,000, general ward ₹2,500."""
    code = f"TST-BED{uuid.uuid4().hex[:6].upper()}"
    billing_masters.upsert_service_item(code=code, name="Bed day", category="bed")
    plan = int(billing_service.upsert_tariff_plan(code=f"TP{uuid.uuid4().hex[:6].upper()}",
                                                  name="Bed plan")["id"])
    billing_masters.set_tariff_rate(tariff_plan_id=plan, service_code=code,
                                    rate_paise=rupees("8000"), ward_class="CCU",
                                    effective_from="2026-01-01")
    billing_masters.set_tariff_rate(tariff_plan_id=plan, service_code=code,
                                    rate_paise=rupees("2500"), ward_class="GEN",
                                    effective_from="2026-01-01")
    with db_session() as db:
        db.execute("UPDATE tariff_plans SET is_default = 0")
        db.execute("UPDATE tariff_plans SET is_default = 1 WHERE id = ?", (plan,))
    return code


def _discharge_summary(pid: str, discharged_at: str):
    with db_session() as db:
        db.execute(
            "INSERT INTO discharge_summaries (patient_id, author_id, summary, discharged_at) "
            "VALUES (?, 'BEDDOC', 'Summary', ?)",
            (pid, discharged_at),
        )


def test_a_run_after_discharge_stops_at_the_discharge_day(bed_code):
    pid = _patient()
    # Admitted 10 Aug 09:00 IST, discharged 12 Aug 11:00 IST (stored UTC).
    aid = _admission(pid, ward="GEN", created_at="2026-08-10 03:30:00", status="discharged")
    _discharge_summary(pid, "2026-08-12 05:30:00")

    result = billing_service.post_bed_day_charges(admission_id=aid, service_code=bed_code)
    assert result["from"] == "2026-08-10"
    assert result["through"] == "2026-08-12"
    assert result["posted_count"] == 3          # not every day up to today


def test_an_earlier_stays_summary_is_not_taken_for_this_one(bed_code):
    pid = _patient()
    _discharge_summary(pid, "2026-07-01 05:30:00")   # a previous admission's
    aid = _admission(pid, ward="GEN", created_at="2026-08-10 03:30:00", status="discharged")
    _discharge_summary(pid, "2026-08-11 05:30:00")
    result = billing_service.post_bed_day_charges(admission_id=aid, service_code=bed_code)
    assert result["through"] == "2026-08-11"


def test_a_discharged_stay_with_no_recorded_end_asks_for_the_last_day(bed_code):
    pid = _patient()
    aid = _admission(pid, ward="GEN", created_at="2026-08-10 03:30:00", status="discharged")
    with pytest.raises(BillingError) as err:
        billing_service.post_bed_day_charges(admission_id=aid, service_code=bed_code)
    assert err.value.code == "invalid_input"
    # An explicit last day still works.
    ok = billing_service.post_bed_day_charges(admission_id=aid, service_code=bed_code,
                                              through="2026-08-11")
    assert ok["posted_count"] == 2


def test_each_day_is_priced_at_the_ward_the_patient_was_in(bed_code):
    from services.frontdesk_service import ensure_frontdesk_schema

    ensure_frontdesk_schema()
    pid = _patient()
    aid = _admission(pid, ward="GEN", created_at="2026-08-10 03:30:00")  # current ward: GEN
    with db_session() as db:
        # CCU from admission, moved to the general ward on 12 Aug at 14:00 IST.
        db.execute(
            "INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, "
            "from_ward, to_ward, reason, moved_at) VALUES (?, ?, ?, 'transfer', 'CCU', 'GEN', "
            "'Stepped down', '2026-08-12T08:30:00+00:00')",
            (f"MOV-{uuid.uuid4().hex[:8]}", pid, aid),
        )

    result = billing_service.post_bed_day_charges(admission_id=aid, service_code=bed_code,
                                                  through="2026-08-13")
    by_day = {c["service_date"]: c["total_paise"] for c in result["posted"]}
    assert by_day == {
        "2026-08-10": rupees("8000"),   # CCU
        "2026-08-11": rupees("8000"),   # CCU
        "2026-08-12": rupees("2500"),   # transfer day: the ward at the end of the day
        "2026-08-13": rupees("2500"),
    }


def test_no_transfers_prices_every_day_at_the_admission_ward(bed_code):
    pid = _patient()
    aid = _admission(pid, ward="CCU", created_at="2026-08-10 03:30:00")
    result = billing_service.post_bed_day_charges(admission_id=aid, service_code=bed_code,
                                                  through="2026-08-11")
    assert [c["total_paise"] for c in result["posted"]] == [rupees("8000"), rupees("8000")]


def test_an_admission_linked_charge_is_inpatient_and_an_unlinked_one_is_not():
    code = f"TST-CONS{uuid.uuid4().hex[:6].upper()}"
    billing_masters.upsert_service_item(code=code, name="Consultation", category="consultation",
                                        default_rate_paise=rupees("500"))
    pid = _patient()
    aid = _admission(pid, ward="GEN", created_at="2026-08-10 03:30:00")
    ip = billing_service.add_charge(patient_id=pid, admission_id=aid, service_code=code)
    op = billing_service.add_charge(patient_id=pid, service_code=code)
    assert ip["encounter_type"] == "ip"
    assert op["encounter_type"] == "op"


def test_the_ward_board_and_the_discharged_list_carry_the_admission_id():
    from main import app

    pid = _patient()
    aid = _admission(pid, ward="GEN", created_at="2026-08-10 03:30:00")
    uid = f"BEDADM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Admin Bed', 'admin', 'x')", (uid,))
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(uid, 'admin')}"}

    board = client.get("/api/vigil/patients?limit=500", headers=headers)
    assert board.status_code == 200, board.text
    row = next(r for r in board.json() if r["id"] == pid)
    assert row["admission_id"] == aid

    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute("UPDATE admissions SET status = 'discharged' WHERE id = ?", (aid,))
    gone = client.get("/api/vigil/discharged?limit=500", headers=headers)
    assert gone.status_code == 200, gone.text
    drow = next(r for r in gone.json() if r["id"] == pid)
    assert drow["admission_id"] == aid
    assert drow["final_bill_raised"] is False


def _consult_code() -> str:
    code = f"TST-CONS{uuid.uuid4().hex[:6].upper()}"
    billing_masters.upsert_service_item(code=code, name="Consultation", category="consultation",
                                        default_rate_paise=rupees("500"))
    return code


def test_a_stays_summary_and_advisory_still_count_a_charge_with_no_admission():
    """The OPD consultation that led to the stay is written with no admission.
    Asking about the stay dropped it: unbilled 1 (₹500) instead of 2 (₹1,000),
    so the discharge advisory said less was owed than was."""
    code = _consult_code()
    pid = _patient()
    aid = _admission(pid, ward="GEN", created_at="2026-08-10 03:30:00")
    billing_service.add_charge(patient_id=pid, service_code=code)                  # OPD, no stay
    billing_service.add_charge(patient_id=pid, admission_id=aid, service_code=code)

    summary = billing_service.patient_financial_summary(pid, admission_id=aid)
    assert summary["unbilled_charge_count"] == 2
    assert summary["unbilled_total_paise"] == rupees("1000")

    advisory = billing_service.discharge_financial_advisory(
        patient_id=pid, admission_id=aid, record=False)
    assert advisory["unbilled_charge_count"] == 2
    assert "2 captured charges worth ₹1,000.00 are not on an invoice yet" in advisory["reasons"]


def test_the_charge_the_stays_final_bill_leaves_is_billable_on_its_own_as_outpatient():
    code = _consult_code()
    pid = _patient()
    aid = _admission(pid, ward="GEN", created_at="2026-08-10 03:30:00")
    opd = billing_service.add_charge(patient_id=pid, service_code=code)
    billing_service.add_charge(patient_id=pid, admission_id=aid, service_code=code)

    stay = billing_service.create_invoice(patient_id=pid, admission_id=aid)
    assert stay["encounter_type"] == "ip"
    assert len(stay["lines"]) == 1
    # Still owed after the stay's bill, and the advisory says so.
    after = billing_service.patient_financial_summary(pid, admission_id=aid)
    assert after["unbilled_charge_count"] == 1

    # What the desk's "Bill these separately" sends: the id, no admission.
    separate = billing_service.create_invoice(patient_id=pid, charge_ids=[opd["id"]])
    assert separate["encounter_type"] == "op"
    assert [line["encounter_charge_id"] for line in separate["lines"]] == [opd["id"]]
    assert billing_service.patient_financial_summary(
        pid, admission_id=aid)["unbilled_charge_count"] == 0


def test_a_discharged_patient_with_a_charge_left_after_the_final_bill_says_so():
    from main import app

    code = _consult_code()
    pid = _patient()
    aid = _admission(pid, ward="GEN", created_at="2026-08-10 03:30:00", status="discharged")
    billing_service.add_charge(patient_id=pid, service_code=code)
    billing_service.add_charge(patient_id=pid, admission_id=aid, service_code=code)
    billing_service.create_invoice(patient_id=pid, admission_id=aid, invoice_type="final")
    uid = f"BEDADM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pid,))
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Admin Bed', 'admin', 'x')", (uid,))
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {create_token(uid, 'admin')}"}

    gone = client.get("/api/vigil/discharged?limit=500", headers=headers)
    assert gone.status_code == 200, gone.text
    row = next(r for r in gone.json() if r["id"] == pid)
    assert row["final_bill_raised"] is True
    assert row["has_pending_charges"] is True
