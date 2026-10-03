"""Front desk — OPD visits, consultation charge capture, and ward TRANSFERS.

WHAT THIS FILE IS DEFENDING.

Orb had `admit` and `discharge` and nothing between them, so a patient moved from
one ward to another left no record of the move: somebody edited a ward label and
the previous location was gone. `frontdesk_service.transfer_patient` makes the
move a first-class event — and in doing so it touches the single most dangerous
column in the system.

A patient's DEPARTMENT is the ACCESS BOUNDARY. `permissions.PATIENT_DEPARTMENT_SQL`
derives it from the active admission, and `permissions.department_scope` compares
that to the authenticated user's own department. Vigil's board, the nurse shift
board, the Sage lane, Relay's case rooms and every `/{patient_id}` route hang off
that one comparison. So a transfer can fail in three distinct, silent ways:

  1. IT CAN DELETE THE PATIENT FROM THE HOSPITAL. Both `patients.status` and
     `admissions.status` accept the value 'transferred', and every board filters
     `p.status = 'admitted'` / `a.status = 'active'`. A transfer that sets either
     one removes the patient from Vigil, the nurse board and the eMAR while they
     are lying in a bed. Guarded by test_transfer_preserves_admission.

  2. IT CAN MOVE THE SCOPE AND NOT THE SCREEN (or the reverse). Scope reads
     `admissions.department`; Vigil, Sage and the discharge summary read
     `patients.ward`. Updating one and not the other yields a patient who is
     scoped to the ICU and displayed in Cardiology. Guarded by
     test_transfer_moves_both_admission_and_patient_mirror.

  3. IT CAN ORPHAN THE PATIENT. This is the worst one and the reason for the
     prove-can-fail test at the bottom. Because the boundary is a STRING
     comparison, a transfer to "Ward 7" — a department no account covers —
     produces a patient who is still admitted, still being scored for
     deterioration, and visible on NOBODY's board. No error, no empty result,
     nothing to notice. `transfer_patient` therefore resolves the destination
     against `list_covering_departments()` and refuses anything else.

Every visibility assertion below goes through the REAL endpoint
(`GET /api/vigil/patients`) rather than a direct query, because the endpoint is
what a nurse actually looks at, and it is the composition of all three columns —
`p.status`, `a.status`, `a.department` — that decides whether she sees her patient.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from permissions import (
    ADMIN_OVERVIEW,
    FRONTDESK_WRITE,
    PATIENT_READ_ALL,
    PATIENT_WRITE,
    ROLE_PERMISSIONS,
    has_permission,
)
from services import frontdesk_service as fd
from services.money import GST_EXEMPT

client = TestClient(app)


# Distinctive names so this module cannot collide with another test module's
# fixtures in the shared session DB — and, crucially, so an assertion about
# "which departments have cover" cannot be satisfied by somebody else's user row.
DEPT_SEND = "FrontDesk Sending Cardiology"
DEPT_RECV = "FrontDesk Receiving Intensive Care"
DEPT_UNSTAFFED = "FrontDesk Ward Nobody Covers"

CONSULT_CODE = f"FD-CONSULT-{uuid.uuid4().hex[:6].upper()}"
UNPRICED_CODE = f"FD-UNPRICED-{uuid.uuid4().hex[:6].upper()}"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department, *, name=None):
    uid = f"FD{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department, is_active) "
            "VALUES (?, ?, ?, 'x', ?, 1)",
            (uid, name or f"FrontDesk {role}", role, department),
        )
    return uid


def _auth(user_id, role):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _mk_patient(department, *, ward=None, bed="1", status="admitted"):
    """An admitted patient whose department lives on the ADMISSION.

    `patients.ward` is deliberately a DIFFERENT string from the department: it is
    a free-text bed label, and a transfer that updated only `ward` (or only
    `department`) must not be able to pass by accident.
    """
    pid = f"FDP{uuid.uuid4().hex[:8].upper()}"
    aid = f"FDA{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('FDADMITTER', 'FrontDesk Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, ?, ?, 54, 'F', 'FrontDesk fixture', ?, ?)""",
            (pid, f"FrontDesk Patient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}",
             bed, ward or f"{department} bay", status, now),
        )
        db.execute(
            """INSERT INTO admissions (id, patient_id, admitted_by, department, ward,
                                       bed, status, created_at)
               VALUES (?, ?, 'FDADMITTER', ?, ?, ?, ?, ?)""",
            (aid, pid, department, ward or f"{department} bay", bed,
             "active" if status == "admitted" else "discharged", now),
        )
    return pid, aid


def _mk_outpatient():
    """A patient with NO admission row at all — the OPD walk-in case.

    `permissions.patient_in_scope` fail-closes this patient out of EVERY
    department (no admission, no derivable department), which is correct for an
    inpatient route and wrong for an OPD desk. See
    frontdesk_service.opd_registration_allowed.
    """
    pid = f"FDO{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, age, sex, status)
               VALUES (?, ?, ?, 33, 'M', 'discharged')""",
            (pid, f"FrontDesk Outpatient {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:8].upper()}"),
        )
    return pid


def _price(code, paise, *, name, gst_treatment=GST_EXEMPT, gst_rate_bp=0):
    """Create a priced service item. A price in this system is always typed by a
    human at the hospital; the fixture is that human."""
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO service_items
                   (code, name, category, uom, gst_treatment, gst_rate_bp,
                    default_rate_paise, rate_is_configured, is_active)
               VALUES (?, ?, 'consultation', 'each', ?, ?, ?, 1, 1)""",
            (code, name, gst_treatment, gst_rate_bp, paise),
        )


def _unpriced(code, *, name):
    """A service item that EXISTS but that nobody has priced. `rate_is_configured`
    is 0, which is not the same fact as a ₹0 item."""
    with db_session() as db:
        db.execute(
            """INSERT OR REPLACE INTO service_items
                   (code, name, category, uom, gst_treatment, gst_rate_bp,
                    default_rate_paise, rate_is_configured, is_active)
               VALUES (?, ?, 'consultation', 'each', 'exempt', 0, 0, 0, 1)""",
            (code, name),
        )


@pytest.fixture(scope="module", autouse=True)
def _seed():
    fd.ensure_frontdesk_schema()
    # Staff give the two real departments their clinical COVER. Without a user
    # row, a department does not exist as far as the transfer guard is concerned.
    doctor_send = _mk_user("doctor", DEPT_SEND, name="FrontDesk Sending Doctor")
    doctor_recv = _mk_user("doctor", DEPT_RECV, name="FrontDesk Receiving Doctor")
    nurse_send = _mk_user("nurse", DEPT_SEND)
    nurse_recv = _mk_user("nurse", DEPT_RECV)
    _price(CONSULT_CODE, 45000, name="FrontDesk OPD consultation")
    _unpriced(UNPRICED_CODE, name="FrontDesk unpriced consultation")
    yield {
        "doctor_send": doctor_send,
        "doctor_recv": doctor_recv,
        "nurse_send": nurse_send,
        "nurse_recv": nurse_recv,
    }


@pytest.fixture()
def actors(_seed):
    return _seed


def _vigil_patient_ids(user_id, role="nurse"):
    """The patient ids this account can actually see on the clinical board."""
    resp = client.get("/api/vigil/patients?limit=500", headers=_auth(user_id, role))
    assert resp.status_code == 200, resp.text
    return {p["id"] for p in resp.json()}


# ═════════════════════════════════════════════════════════════════════════════
#  OPD — lifecycle and token ordering
# ═════════════════════════════════════════════════════════════════════════════

def test_opd_visit_lifecycle_and_token_ordering(actors):
    """Register three, get tokens 1..N in order, call one in, complete it."""
    doctor = actors["doctor_send"]
    headers = _auth(doctor, "doctor")
    patients = [_mk_outpatient() for _ in range(3)]

    visits = []
    for pid in patients:
        resp = client.post(
            "/api/frontdesk/opd/visits",
            json={"patient_id": pid, "department": DEPT_SEND, "doctor_id": doctor,
                  "chief_complaint": "chest pain"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        visits.append(resp.json())

    tokens = [v["token_number"] for v in visits]
    # Consecutive and ascending — the queue display is the only instruction most
    # OPD patients get, so a repeated or skipped token is two people called by
    # one number.
    assert tokens == sorted(tokens)
    assert len(set(tokens)) == 3
    assert tokens[1] == tokens[0] + 1 and tokens[2] == tokens[1] + 1
    assert all(v["status"] == "waiting" for v in visits)

    queue = client.get("/api/frontdesk/opd/queue", headers=headers)
    assert queue.status_code == 200, queue.text
    body = queue.json()
    mine = [q for q in body["queues"] if q["doctor_id"] == doctor]
    assert len(mine) == 1
    assert [v["token_number"] for v in mine[0]["visits"]] == tokens
    assert mine[0]["waiting_count"] == 3
    # "nothing waiting" and "no wait" are different facts; the queue must not
    # report 0 minutes when it has no waiting patient to measure.
    assert mine[0]["longest_wait_minutes"] is not None

    first = visits[0]["id"]
    started = client.post(f"/api/frontdesk/opd/visits/{first}/start", headers=headers)
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "in_consultation"
    assert started.json()["called_at"]

    # Idempotent: calling the same patient in twice is a double-tap, not an error.
    again = client.post(f"/api/frontdesk/opd/visits/{first}/start", headers=headers)
    assert again.status_code == 200
    assert again.json()["called_at"] == started.json()["called_at"]

    done = client.post(
        f"/api/frontdesk/opd/visits/{first}/complete",
        json={"service_code": CONSULT_CODE},
        headers=headers,
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "completed"

    # A completed visit leaves the waiting queue.
    after = client.get("/api/frontdesk/opd/queue", headers=headers).json()
    still = [q for q in after["queues"] if q["doctor_id"] == doctor]
    assert first not in {v["id"] for v in (still[0]["visits"] if still else [])}


def test_completed_visit_cannot_be_cancelled(actors):
    """A consultation that happened is a clinical fact; cancelling it would erase
    the event and orphan its charge."""
    doctor = actors["doctor_send"]
    headers = _auth(doctor, "doctor")
    pid = _mk_outpatient()
    visit = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": pid, "department": DEPT_SEND, "doctor_id": doctor},
        headers=headers,
    ).json()
    client.post(f"/api/frontdesk/opd/visits/{visit['id']}/complete",
                json={"service_code": CONSULT_CODE}, headers=headers)
    resp = client.post(f"/api/frontdesk/opd/visits/{visit['id']}/cancel",
                       json={"reason": "changed my mind"}, headers=headers)
    assert resp.status_code == 422


def test_opd_queue_is_scoped_to_the_callers_department(actors):
    """A clerk in one department cannot see another department's clinic, and
    cannot ask for it either."""
    send_headers = _auth(actors["nurse_send"], "nurse")
    recv_headers = _auth(actors["doctor_recv"], "doctor")
    pid = _mk_outpatient()

    visit = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": pid, "department": DEPT_RECV,
              "doctor_id": actors["doctor_recv"]},
        headers=recv_headers,
    )
    assert visit.status_code == 200, visit.text
    visit_id = visit.json()["id"]

    seen_by_receiver = client.get("/api/frontdesk/opd/queue", headers=recv_headers).json()
    assert visit_id in {
        v["id"] for q in seen_by_receiver["queues"] for v in q["visits"]
    }

    seen_by_sender = client.get("/api/frontdesk/opd/queue", headers=send_headers).json()
    assert visit_id not in {
        v["id"] for q in seen_by_sender["queues"] for v in q["visits"]
    }
    # Every department in the sender's view is the sender's own — the query
    # string has no lever to widen it.
    assert {q["department"] for q in seen_by_sender["queues"]} <= {DEPT_SEND}

    # 404, not 403: a 403 would confirm the visit exists.
    assert client.get(f"/api/frontdesk/opd/visits/{visit_id}",
                      headers=send_headers).status_code == 404


def test_opd_doctor_roster_is_staff_not_todays_queue(actors):
    """The bookable-doctor list comes from WHO WORKS HERE, not from who already
    has patients waiting.

    THE DEFECT. The panel derived its "consulting doctor" dropdown from
    `/opd/queue` — from the visits already registered today. On an empty morning
    no doctor had a queue, so the dropdown held only "Unassigned", so the first
    visit of the day could not be assigned to anybody, so no doctor ever got a
    queue. `/opd/doctors` answers from the staff table instead, which has an
    answer before the first patient arrives.
    """
    recv_headers = _auth(actors["doctor_recv"], "doctor")
    resp = client.get("/api/frontdesk/opd/doctors", headers=recv_headers)
    assert resp.status_code == 200, resp.text
    ids = {d["id"] for d in resp.json()["doctors"]}

    # Present WITHOUT any visit having been registered against them.
    assert actors["doctor_recv"] in ids
    # Scoped exactly like the queue: another department's doctor is not offered,
    # and the caller has no lever to widen it.
    assert actors["doctor_send"] not in ids
    assert {d["department"] for d in resp.json()["doctors"]} == {DEPT_RECV}
    # A nurse covers a ward but does not hold an outpatient clinic.
    assert actors["nurse_recv"] not in ids


def test_opd_desk_cannot_launder_ward_scope(actors):
    """The OPD desk must not become a way to read another ward's INPATIENT.

    An outpatient has no admission and no ward, so registration deliberately
    allows them. An admitted patient DOES have a ward, and registering them into
    a different department's clinic would put their name, MRN and complaint on
    the registering clerk's own queue.
    """
    other_ward_patient, _ = _mk_patient(DEPT_RECV)
    resp = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": other_ward_patient, "department": DEPT_SEND,
              "doctor_id": actors["doctor_send"]},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 404, resp.text

    # And a caller cannot simply name somebody else's department either.
    resp = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_RECV},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 403, resp.text


def test_opd_visit_into_unstaffed_department_is_refused(actors):
    """A queue in a department nobody staffs is a patient sitting in a corridor
    on a list nobody opens."""
    with pytest.raises(fd.FrontDeskError):
        fd.register_opd_visit(
            patient_id=_mk_outpatient(),
            department=DEPT_UNSTAFFED,
            registered_by="FDADMITTER",
        )


# ═════════════════════════════════════════════════════════════════════════════
#  Consultation charge capture
# ═════════════════════════════════════════════════════════════════════════════

def test_consultation_charge_lands_as_a_proper_charge_line(actors):
    """Completing a visit writes ONE `encounter_charges` row, in integer paise,
    typed as an outpatient encounter, with the healthcare GST exemption applied.

    This module does not invent billing: the rate comes from
    billing_masters.resolve_rate and the arithmetic from money.compute_line.
    """
    doctor = actors["doctor_send"]
    headers = _auth(doctor, "doctor")
    pid = _mk_outpatient()
    visit = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": pid, "department": DEPT_SEND, "doctor_id": doctor},
        headers=headers,
    ).json()

    done = client.post(
        f"/api/frontdesk/opd/visits/{visit['id']}/complete",
        json={"service_code": CONSULT_CODE},
        headers=headers,
    )
    assert done.status_code == 200, done.text
    charge = done.json()["consultation_charge"]
    assert charge["rate_is_configured"] is True
    assert charge["rate_paise"] == 45000
    assert charge["total_paise"] == 45000          # exempt: no tax added
    assert charge["rate_display"] == "₹450.00"

    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM encounter_charges WHERE source_ref = ?", (visit["id"],)
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["patient_id"] == pid
    assert row["admission_id"] is None              # an OPD visit is not an admission
    assert row["encounter_type"] == "op"
    assert row["service_code"] == CONSULT_CODE
    assert row["status"] == "pending"
    assert row["rate_is_configured"] == 1
    # INTEGER PAISE, never float. A REAL here is the defect the whole spine's
    # naming convention exists to make visible.
    assert isinstance(row["unit_rate_paise"], int)
    assert isinstance(row["total_paise"], int)
    assert row["unit_rate_paise"] == 45000
    assert row["quantity_milli"] == 1000
    # Healthcare services by a clinical establishment are GST-exempt in India.
    assert row["gst_treatment"] == "exempt"
    assert row["cgst_paise"] == 0 and row["sgst_paise"] == 0 and row["igst_paise"] == 0
    assert row["taxable_value_paise"] + row["cgst_paise"] + row["sgst_paise"] \
        + row["igst_paise"] == row["total_paise"]

    # The visit points back at its charge, so the counter can find the line for
    # a consultation and the consultation for a line.
    stored = fd.get_opd_visit(visit["id"])
    assert stored["consultation_charge_id"] == row["id"]


def test_completing_twice_does_not_double_charge(actors):
    """A replayed completion (lost response, offline queue) must not bill the
    same consultation twice."""
    doctor = actors["doctor_send"]
    headers = _auth(doctor, "doctor")
    pid = _mk_outpatient()
    visit = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": pid, "department": DEPT_SEND, "doctor_id": doctor},
        headers=headers,
    ).json()
    for _ in range(3):
        resp = client.post(
            f"/api/frontdesk/opd/visits/{visit['id']}/complete",
            json={"service_code": CONSULT_CODE}, headers=headers,
        )
        assert resp.status_code == 200, resp.text

    with db_session() as db:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM encounter_charges WHERE source_ref = ?",
            (visit["id"],),
        ).fetchone()["n"]
    assert count == 1


def test_unpriced_consultation_is_recorded_as_unconfigured_not_zero(actors):
    """NOT CONFIGURED MUST NOT READ AS ZERO.

    A site that has not priced its consultation item must see "Not configured" at
    the counter, not "₹0.00". The charge row is still written — the consultation
    happened — flagged for the billing counter to price.
    """
    doctor = actors["doctor_send"]
    headers = _auth(doctor, "doctor")
    pid = _mk_outpatient()
    visit = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": pid, "department": DEPT_SEND, "doctor_id": doctor},
        headers=headers,
    ).json()
    done = client.post(
        f"/api/frontdesk/opd/visits/{visit['id']}/complete",
        json={"service_code": UNPRICED_CODE}, headers=headers,
    ).json()

    charge = done["consultation_charge"]
    assert charge["rate_is_configured"] is False
    assert charge["rate_paise"] is None
    assert charge["rate_display"] == "Not configured"
    assert charge["total_display"] == "Not configured"

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM encounter_charges WHERE source_ref = ?", (visit["id"],)
        ).fetchone()
    assert row is not None
    assert row["rate_is_configured"] == 0
    assert row["status"] == "pending"
    assert row["notes"] and "not configured" in row["notes"].lower()


def test_default_consultation_code_is_on_the_price_list_and_priceable(actors):
    """THE CODE THE FRONT DESK CHARGES MUST BE A CODE THE COUNTER CAN PRICE.

    Every test above names a service_code explicitly. The real front desk NEVER
    does — FrontDeskPanel completes with an empty body, so every production
    charge lands on OPD_CONSULTATION_SERVICE_CODE. That code used to exist only
    as a constant in this module: it was on no price list, so a billing admin
    could not discover it, priced a hand-made lookalike instead, and every
    consultation sat at "Not configured" forever. This test walks the admin's
    actual path: see the item on the price list, price it through the billing
    endpoint, and watch the next empty-body completion come out priced.
    """
    doctor = actors["doctor_send"]
    headers = _auth(doctor, "doctor")
    admin = _mk_user("admin", None, name="FrontDesk Billing Admin")
    admin_headers = _auth(admin, "admin")

    # An empty body — byte-for-byte what the panel sends — charges the default
    # code, unpriced so far.
    visit = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND,
              "doctor_id": doctor},
        headers=headers,
    ).json()
    done = client.post(
        f"/api/frontdesk/opd/visits/{visit['id']}/complete", json={}, headers=headers,
    )
    assert done.status_code == 200, done.text
    charge = done.json()["consultation_charge"]
    assert charge["service_code"] == fd.OPD_CONSULTATION_SERVICE_CODE
    assert charge["rate_is_configured"] is False

    # The code is VISIBLE where prices are set. This is the defended fact: an
    # admin browsing the price list finds the row the front desk charges,
    # rather than having to reverse-engineer a constant out of the server.
    listed = client.get(
        "/api/billing/service-items", headers=admin_headers
    ).json()["items"]
    item = next(
        (i for i in listed if i["code"] == fd.OPD_CONSULTATION_SERVICE_CODE), None
    )
    assert item is not None, "OPD-CONSULT is not on the billing price list"
    assert item["rate_is_configured"] == 0

    # The admin prices it — same endpoint, same keystrokes as the real counter.
    priced = client.post(
        "/api/billing/service-items",
        json={"code": fd.OPD_CONSULTATION_SERVICE_CODE, "name": item["name"],
              "category": "consultation", "default_rate_paise": 80000},
        headers=admin_headers,
    )
    assert priced.status_code == 200, priced.text

    # The next walk-in comes out priced, with no service_code named anywhere.
    visit2 = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND,
              "doctor_id": doctor},
        headers=headers,
    ).json()
    done2 = client.post(
        f"/api/frontdesk/opd/visits/{visit2['id']}/complete", json={}, headers=headers,
    )
    charge2 = done2.json()["consultation_charge"]
    assert charge2["service_code"] == fd.OPD_CONSULTATION_SERVICE_CODE
    assert charge2["rate_is_configured"] is True
    assert charge2["rate_paise"] == 80000
    assert charge2["rate_display"] == "₹800.00"


# ═════════════════════════════════════════════════════════════════════════════
#  THE MIDNIGHT BOUNDARY  (prove-can-fail)
# ═════════════════════════════════════════════════════════════════════════════

def test_opd_visit_at_0200_ist_is_dated_and_priced_for_the_hospitals_day(actors, monkeypatch):
    """A 2 A.M. CONSULTATION BELONGS TO TODAY, ON THE BILL AND IN THE TARIFF.

    THE DEFECT. The front desk stamped its charge with a UTC instant and then
    sliced ten characters off it for the tariff lookup. In a UTC+05:30 hospital
    every minute between 00:00 and 05:30 IST is still YESTERDAY in UTC — so a
    consultation registered at 02:15 was printed on the patient's bill with
    yesterday's date and priced against yesterday's tariff. Night OPD, casualty
    review clinics and the 2 a.m. walk-in are exactly the slots this hits, and it
    fails silently: a rate that starts TODAY is simply "not in force" at 02:15,
    so the charge falls back to the item default (or to "Not configured") and
    nothing anywhere says a date was wrong.

    The clock is frozen at 20:45 UTC on 14 March, which is 02:15 IST on the
    15th — the two dates differ, which is the whole point. The zone is stated
    explicitly rather than inherited from the machine, so this test means the
    same thing on a laptop in London and on the appliance in the ward.

    PROVEN ABLE TO FAIL: restore the old arithmetic in
    `frontdesk_service._raise_consultation_charge` — `on_date=service_date[:10]`
    with the UTC instant passed for `service_date` — and this goes red twice: the
    stored `service_date` reads 2027-03-14 and the rate resolves to the item
    default (₹300) instead of the day's tariff (₹950). Verified by doing exactly
    that and restoring it.
    """
    from services import hospital_time
    from services.vitals_source import hl7v2

    frozen_utc = datetime(2027, 3, 14, 20, 45, tzinfo=timezone.utc)  # 02:15 IST, 15th
    local_day, utc_day = "2027-03-15", "2027-03-14"

    code = f"FD-NIGHT-{uuid.uuid4().hex[:6].upper()}"
    # The item's own default is what a UTC-dated lookup would fall back to; the
    # tariff row that starts TODAY is what the ward's day must find.
    _price(code, 30000, name="FrontDesk night consultation")
    with db_session() as db:
        plan = db.execute(
            "SELECT id FROM tariff_plans WHERE is_active = 1 "
            "ORDER BY is_default DESC, id ASC LIMIT 1"
        ).fetchone()
        assert plan is not None, "no active tariff plan seeded"
        item = db.execute("SELECT id FROM service_items WHERE code = ?", (code,)).fetchone()
        db.execute(
            """INSERT INTO tariff_rates (tariff_plan_id, service_item_id, service_code,
                                         ward_class, rate_paise, effective_from, is_active)
               VALUES (?, ?, ?, NULL, 95000, ?, 1)""",
            (plan["id"], item["id"], code, local_day),
        )

    hl7v2.set_default_timezone("Asia/Kolkata")
    # Both clocks, one instant: `hospital_time._utc_now` is what the day resolver
    # reads, `fd._now` is what stamps the row. Freezing only one would test a
    # hospital whose two clocks disagree, which is not the failure being guarded.
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: frozen_utc)
    monkeypatch.setattr(fd, "_now", lambda: frozen_utc.isoformat())
    try:
        doctor = actors["doctor_send"]
        headers = _auth(doctor, "doctor")
        visit = client.post(
            "/api/frontdesk/opd/visits",
            json={"patient_id": _mk_outpatient(), "department": DEPT_SEND,
                  "doctor_id": doctor},
            headers=headers,
        ).json()

        # 1. The visit is dated the ward's day, not UTC's.
        assert visit["visit_date"] == local_day, (
            f"a visit registered at 02:15 IST is dated {visit['visit_date']}; "
            f"in UTC it is still {utc_day}, but the patient and the clerk are in "
            "the hospital"
        )
        # And the queue asks for the same day, or the visit just registered would
        # not be on it.
        queue = client.get("/api/frontdesk/opd/queue", headers=headers).json()
        assert queue["visit_date"] == local_day
        assert visit["id"] in {v["id"] for q in queue["queues"] for v in q["visits"]}

        done = client.post(
            f"/api/frontdesk/opd/visits/{visit['id']}/complete",
            json={"service_code": code}, headers=headers,
        )
        assert done.status_code == 200, done.text
        charge = done.json()["consultation_charge"]

        # 2. Priced against the ward's day. The tariff row starts today; a
        #    yesterday-dated lookup would not see it and would quietly bill ₹300.
        assert charge["rate_paise"] == 95000, (
            f"priced at {charge['rate_paise']} paise — the tariff effective from "
            f"{local_day} was not in force for a lookup dated {utc_day}"
        )
        assert charge["rate_source"] == "tariff_rate"

        # 3. The date the bill prints. A bare hospital-local day, exactly as
        #    billing_service.add_charge writes for every other line, so an OPD
        #    charge cannot sit in a different day-bucket from a lab charge raised
        #    in the same minute.
        with db_session() as db:
            row = db.execute(
                "SELECT service_date, charged_at FROM encounter_charges WHERE source_ref = ?",
                (visit["id"],),
            ).fetchone()
        assert row["service_date"] == local_day, (
            f"the bill is dated {row['service_date']}, which is yesterday on the ward"
        )
        # The INSTANT stays UTC — a timestamp and a calendar day are different
        # facts and only one of them is local.
        assert str(row["charged_at"]).startswith(utc_day)
    finally:
        hl7v2.set_default_timezone(None)


def test_hospital_day_never_shifts_a_bare_date_and_survives_junk():
    """The resolver's edges, stated because both have bitten this project.

    A value that is already a DAY is not an instant and must not be converted —
    doing so shifts a caller-supplied date by one for free. A value that will not
    parse degrades to its first ten characters: the front desk must never refuse
    to register a patient over a date format.
    """
    from services import hospital_time
    from services.vitals_source import hl7v2

    hl7v2.set_default_timezone("Asia/Kolkata")
    try:
        assert fd._hospital_day("2027-03-15") == "2027-03-15"
        assert fd._hospital_day("2027-03-14T20:45:00+00:00") == "2027-03-15"
        assert fd._hospital_day("2027-03-14 20:45:00") == "2027-03-15"
        # Naive timestamps are stored UTC everywhere in Orb.
        assert fd._hospital_day("2027-03-14T20:45:00") == "2027-03-15"
        assert fd._hospital_day("not a timestamp at all") == "not a time"
        frozen = datetime(2027, 3, 14, 20, 45, tzinfo=timezone.utc)
        original = hospital_time._utc_now
        hospital_time._utc_now = lambda: frozen
        try:
            assert fd._hospital_day() == "2027-03-15"
            assert fd._hospital_day(None) == "2027-03-15"
            assert fd._hospital_day("") == "2027-03-15"
        finally:
            hospital_time._utc_now = original
    finally:
        hl7v2.set_default_timezone(None)


def test_schema_ensure_never_overwrites_a_priced_consultation_item():
    """Registration is INSERT-if-absent, not upsert. Once a site has priced (or
    renamed, or retired) the consultation item, a server restart must not put
    the factory row back."""
    with db_session() as db:
        db.execute(
            """UPDATE service_items
                  SET default_rate_paise = 123400, rate_is_configured = 1,
                      name = 'Consultation priced by the site'
                WHERE code = ?""",
            (fd.OPD_CONSULTATION_SERVICE_CODE,),
        )
    fd.ensure_frontdesk_schema(force=True)
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM service_items WHERE code = ?",
            (fd.OPD_CONSULTATION_SERVICE_CODE,),
        ).fetchone()
    assert row["default_rate_paise"] == 123400
    assert row["rate_is_configured"] == 1
    assert row["name"] == "Consultation priced by the site"


# ═════════════════════════════════════════════════════════════════════════════
#  Transfers
# ═════════════════════════════════════════════════════════════════════════════

def test_transfer_records_the_movement(actors):
    pid, aid = _mk_patient(DEPT_SEND, bed="C-4")
    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV, "to_ward": "ICU Bay 2",
              "to_bed": "I-1", "reason": "Requires ventilatory support"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    movement = body["movement"]

    assert movement["movement_type"] == "transfer"
    assert movement["from_department"] == DEPT_SEND
    assert movement["from_bed"] == "C-4"
    assert movement["to_department"] == DEPT_RECV
    assert movement["to_ward"] == "ICU Bay 2"
    assert movement["to_bed"] == "I-1"
    assert movement["reason"] == "Requires ventilatory support"
    assert movement["moved_at"]
    assert movement["moved_by"] == actors["doctor_send"]
    assert movement["admission_id"] == aid

    history = client.get(
        f"/api/frontdesk/patients/{pid}/movements",
        headers=_auth(actors["doctor_recv"], "doctor"),
    )
    assert history.status_code == 200, history.text
    assert [m["id"] for m in history.json()["movements"]] == [movement["id"]]


def test_transfer_preserves_the_admission(actors):
    """SAME PATIENT, SAME ADMISSION, NEW LOCATION.

    Both `patients.status` and `admissions.status` have a 'transferred' value in
    their CHECK constraint, and every clinical board filters on 'admitted' /
    'active'. Setting either one during a ward move deletes the patient from
    Vigil, the nurse board and the eMAR while they are lying in a bed.
    """
    pid, aid = _mk_patient(DEPT_SEND)
    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV, "to_ward": "ICU Bay 1",
              "reason": "Post-op monitoring"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["admission_id"] == aid
    assert body["admission_status"] == "active"
    assert body["patient_status"] == "admitted"

    with db_session() as db:
        admissions = db.execute(
            "SELECT id, status, department FROM admissions WHERE patient_id = ?", (pid,)
        ).fetchall()
        patient = db.execute(
            "SELECT status FROM patients WHERE id = ?", (pid,)
        ).fetchone()
    # Exactly ONE admission — a transfer does not open a second one, because
    # orders, the eMAR, vitals and the case room all hang off the admission id.
    assert len(admissions) == 1
    assert admissions[0]["id"] == aid
    assert admissions[0]["status"] == "active"
    assert admissions[0]["department"] == DEPT_RECV
    assert patient["status"] == "admitted"


def test_transfer_moves_both_admission_and_patient_mirror(actors):
    """Scope reads `admissions.department`; Vigil/Sage/discharge read
    `patients.ward`. Moving one without the other yields a patient who is scoped
    to the ICU and displayed in Cardiology."""
    pid, aid = _mk_patient(DEPT_SEND, ward="Cardio Bay 3", bed="C-9")
    client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV, "to_ward": "ICU Bay 4",
              "to_bed": "I-7", "reason": "Deterioration"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    with db_session() as db:
        adm = db.execute("SELECT * FROM admissions WHERE id = ?", (aid,)).fetchone()
        pat = db.execute("SELECT ward, bed FROM patients WHERE id = ?", (pid,)).fetchone()
    assert (adm["department"], adm["ward"], adm["bed"]) == (DEPT_RECV, "ICU Bay 4", "I-7")
    assert (pat["ward"], pat["bed"]) == ("ICU Bay 4", "I-7")


def test_transfer_hands_the_patient_over_between_wards(actors):
    """STRICT HANDOVER, asserted through the screen a nurse actually reads.

    Before: the sending ward sees the patient and the receiving ward does not.
    After: exactly the reverse. This is not a separate policy — it is what
    deriving scope live from `admissions.department` means, and it is the reason
    the transfer must never be allowed to leave the department as a string
    nobody covers.
    """
    pid, _ = _mk_patient(DEPT_SEND)
    send_nurse, recv_nurse = actors["nurse_send"], actors["nurse_recv"]

    assert pid in _vigil_patient_ids(send_nurse)
    assert pid not in _vigil_patient_ids(recv_nurse)

    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV, "to_ward": "ICU Bay 9",
              "reason": "Escalation of care"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 200, resp.text

    assert pid in _vigil_patient_ids(recv_nurse), \
        "the receiving ward must be able to see the patient it has just accepted"
    assert pid not in _vigil_patient_ids(send_nurse), \
        "STRICT HANDOVER: the sending ward loses the chart the moment the move lands"


def test_transfer_to_a_nonexistent_ward_is_refused(actors):
    """A destination department with no clinical cover is refused, and the
    patient does not move."""
    pid, aid = _mk_patient(DEPT_SEND)
    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": "Ward 7 (does not exist)",
              "reason": "Bed shuffle"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 422, resp.text

    with db_session() as db:
        adm = db.execute("SELECT department FROM admissions WHERE id = ?", (aid,)).fetchone()
        movements = db.execute(
            "SELECT COUNT(*) AS n FROM patient_movements WHERE patient_id = ?", (pid,)
        ).fetchone()["n"]
    assert adm["department"] == DEPT_SEND
    assert movements == 0, "a refused transfer must not leave a movement row behind"


def test_transfer_requires_a_reason(actors):
    pid, _ = _mk_patient(DEPT_SEND)
    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV, "reason": "   "},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 422


def test_transfer_requires_an_active_admission(actors):
    """An outpatient has no ward to move between."""
    with pytest.raises(fd.FrontDeskError):
        fd.transfer_patient(
            patient_id=_mk_outpatient(),
            to_department=DEPT_RECV,
            reason="Nowhere to move from",
        )


def test_transfer_scope_is_checked_on_the_sending_side(actors):
    """A caller must be able to see the patient WHERE THEY ARE NOW.

    404, not 403 — a 403 confirms the patient exists on a ward the caller cannot
    read, which is itself the disclosure.
    """
    pid, _ = _mk_patient(DEPT_SEND)
    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV, "reason": "Poaching"},
        headers=_auth(actors["doctor_recv"], "doctor"),
    )
    assert resp.status_code == 404, resp.text


def test_transfer_department_typo_resolves_to_the_covered_spelling(actors):
    """'frontdesk receiving intensive care' typed at 3am must land on the exact
    string the receiving ward's accounts carry — a one-byte difference is an
    orphaned patient, not a cosmetic issue."""
    pid, aid = _mk_patient(DEPT_SEND)
    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": f"  {DEPT_RECV.lower()}  ",
              "reason": "Typed at the desk"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        adm = db.execute("SELECT department FROM admissions WHERE id = ?", (aid,)).fetchone()
    assert adm["department"] == DEPT_RECV
    assert pid in _vigil_patient_ids(actors["nurse_recv"])


def test_transfer_is_audited_on_the_fail_closed_chain(actors):
    """A transfer changes who can read the chart, so it is recorded in the
    fail-closed `clinical_access` category with both endpoints of the move."""
    pid, _ = _mk_patient(DEPT_SEND)
    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV, "reason": "Audit check"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        row = db.execute(
            """SELECT event_category, metadata_json FROM audit_logs
                WHERE action_type = 'patient_transferred' AND patient_id = ?
                ORDER BY id DESC LIMIT 1""",
            (pid,),
        ).fetchone()
    assert row is not None
    assert row["event_category"] == "clinical_access"
    assert DEPT_SEND in row["metadata_json"] and DEPT_RECV in row["metadata_json"]


def test_transfer_destinations_only_offers_covered_departments(actors):
    resp = client.get("/api/frontdesk/transfers/destinations",
                      headers=_auth(actors["doctor_send"], "doctor"))
    assert resp.status_code == 200, resp.text
    departments = {d["department"] for d in resp.json()["destinations"]}
    assert DEPT_SEND in departments and DEPT_RECV in departments
    assert DEPT_UNSTAFFED not in departments
    for dest in resp.json()["destinations"]:
        assert dest["staff_count"] >= 1


def test_a_ward_staffed_only_by_non_clinical_accounts_is_not_covered(actors):
    """COVER MEANS CLINICAL COVER.

    An administrator or a pharmacist carrying a department string does not mean
    anyone is looking at that ward's patients — neither role can open
    /api/vigil/patients. Counting them as cover would let a transfer land
    somewhere with a plausible-looking department and no clinician, which is the
    orphaning failure with an extra step.
    """
    dept = "FrontDesk Admin-Only Department"
    _mk_user("admin", dept)
    _mk_user("pharmacist", dept)

    assert dept not in fd.list_covering_departments()

    pid, aid = _mk_patient(DEPT_SEND)
    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": dept, "reason": "Looks staffed"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 422, resp.text
    with db_session() as db:
        assert db.execute(
            "SELECT department FROM admissions WHERE id = ?", (aid,)
        ).fetchone()["department"] == DEPT_SEND


# ═════════════════════════════════════════════════════════════════════════════
#  THE ORPHANING TEST  (prove-can-fail)
# ═════════════════════════════════════════════════════════════════════════════

def test_transfer_can_never_orphan_a_patient_into_an_uncovered_ward(actors):
    """THE ONE THAT MATTERS.

    A department is just a string compared against `users.department`. A transfer
    into a string no account carries produces a patient who is:

        * still `patients.status = 'admitted'`,
        * still on an `admissions.status = 'active'` row,
        * still scored for deterioration by Vigil,
        * and on NOBODY's board.

    Nothing errors. No list comes back empty. The patient simply stops being
    anyone's, which is how a deteriorating patient goes unwatched.

    PROVEN ABLE TO FAIL: delete the `_resolve_covering_department` guard in
    `frontdesk_service.transfer_patient` (accept `to_department` as typed) and
    this test goes red on the final assertion — the movement succeeds and the
    patient appears in NO account's `/api/vigil/patients`. Verified by doing
    exactly that and restoring it.

    The assertion is deliberately made over EVERY clinical account in the
    database rather than over the two fixtures: "some ward can see this patient"
    is the invariant, and checking it against the whole staff list is the only
    way to catch a transfer that lands somewhere unexpected but plausible.
    """
    pid, aid = _mk_patient(DEPT_SEND)

    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_UNSTAFFED,
              "reason": "Bed manager typed a ward that does not exist"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 422, (
        "a transfer into a department with no clinical cover must be refused, "
        f"got {resp.status_code}: {resp.text}"
    )

    # The refusal must be total: nothing moved, nothing recorded.
    with db_session() as db:
        adm = db.execute(
            "SELECT department, status FROM admissions WHERE id = ?", (aid,)
        ).fetchone()
        moved = db.execute(
            "SELECT COUNT(*) AS n FROM patient_movements WHERE patient_id = ?", (pid,)
        ).fetchone()["n"]
    assert adm["department"] == DEPT_SEND and adm["status"] == "active"
    assert moved == 0

    # THE INVARIANT. Whatever happened above, this admitted patient is visible to
    # at least one clinical account. An orphan is a patient for whom this set is
    # empty while `patients.status` still says 'admitted'.
    with db_session() as db:
        staff = db.execute(
            """SELECT id, role FROM users
                WHERE role IN ('doctor','nurse','clinician')
                  AND department IS NOT NULL AND TRIM(department) != ''
                  AND COALESCE(is_active, 1) = 1""",
        ).fetchall()
        patient_status = db.execute(
            "SELECT status FROM patients WHERE id = ?", (pid,)
        ).fetchone()["status"]
    assert patient_status == "admitted"

    covering = [s["id"] for s in staff if pid in _vigil_patient_ids(s["id"], s["role"])]
    assert covering, (
        f"{pid} is admitted but appears on no ward's patient board — the patient "
        "has been orphaned into a department nobody covers"
    )


def test_a_successful_transfer_never_lands_outside_the_covered_set(actors):
    """The complement of the test above: after a transfer that SUCCEEDS, the
    department the patient now sits in is one somebody covers."""
    pid, aid = _mk_patient(DEPT_SEND)
    resp = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV,
              "reason": "Normal escalation"},
        headers=_auth(actors["doctor_send"], "doctor"),
    )
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        landed = db.execute(
            "SELECT department FROM admissions WHERE id = ?", (aid,)
        ).fetchone()["department"]
    assert landed in fd.list_covering_departments()
    assert pid in _vigil_patient_ids(actors["nurse_recv"])


# ═════════════════════════════════════════════════════════════════════════════
#  Module switch
# ═════════════════════════════════════════════════════════════════════════════

def test_module_switch_hides_the_whole_module(actors, monkeypatch):
    """A site running its own registration system sets ORB_FRONTDESK=0 and the
    routes are simply not there — 404, matching the tab the shell will not draw."""
    import config
    monkeypatch.setattr(config, "FRONTDESK_ENABLED", False)
    headers = _auth(actors["doctor_send"], "doctor")
    assert client.get("/api/frontdesk/opd/queue", headers=headers).status_code == 404
    assert client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": "x", "to_department": DEPT_RECV, "reason": "y"},
        headers=headers,
    ).status_code == 404


# ═════════════════════════════════════════════════════════════════════════════
#  Who may work the desk
# ═════════════════════════════════════════════════════════════════════════════

def test_a_nurse_can_read_the_queue_but_cannot_move_a_patient(actors):
    """A transfer changes who can read the chart. That is not a bedside decision,
    so it needs `patient.write` (doctor) or `frontdesk.write` (the registration
    clerk's grant, held today by the desk's admin account) — a ward nurse holds
    neither.

    The 403 body is pinned too: the gate must name exactly its two real arms.
    Until the 2026-08-12 ruling the clerk's arm was `admin.overview` — filing an
    admission required holding the audit log — and a regression to that shape
    would surface here as the wrong `required` list.
    """
    pid, _ = _mk_patient(DEPT_SEND)
    nurse = _auth(actors["nurse_send"], "nurse")

    assert client.get("/api/frontdesk/opd/queue", headers=nurse).status_code == 200
    denied = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV, "reason": "Ward move"},
        headers=nurse,
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["required"] == [PATIENT_WRITE, FRONTDESK_WRITE]
    assert client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND},
        headers=nurse,
    ).status_code == 403


def test_an_administrator_can_work_the_desk(actors):
    """BOUNDARY: every mutation on the router, driven by the desk's real account.

    The registration counter runs on an administrator account, which holds
    `frontdesk.write` and NOT `patient.write` — so this passing end-to-end is
    proof the `frontdesk.write` arm gates each mutation, not a side effect of
    some clinical grant the admin never had.
    """
    assert has_permission("admin", FRONTDESK_WRITE)
    assert not has_permission("admin", PATIENT_WRITE)

    admin = _mk_user("admin", DEPT_SEND)
    headers = _auth(admin, "admin")

    # Register → start → complete: the whole OPD lifecycle.
    v1 = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND},
        headers=headers,
    )
    assert v1.status_code == 200, v1.text
    vid = v1.json()["id"]
    assert client.post(
        f"/api/frontdesk/opd/visits/{vid}/start", headers=headers
    ).status_code == 200
    assert client.post(
        f"/api/frontdesk/opd/visits/{vid}/complete",
        json={"service_code": CONSULT_CODE}, headers=headers,
    ).status_code == 200

    # Cancel — on a second visit, because a completed consultation cannot be
    # cancelled (its own test above).
    v2 = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND},
        headers=headers,
    ).json()
    assert client.post(
        f"/api/frontdesk/opd/visits/{v2['id']}/cancel",
        json={"reason": "Did not wait"}, headers=headers,
    ).status_code == 200

    # And a ward transfer, scope-checked on the sending side as for everyone.
    pid, _ = _mk_patient(DEPT_SEND)
    moved = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV,
              "reason": "Counter-initiated ward move"},
        headers=headers,
    )
    assert moved.status_code == 200, moved.text


def test_a_doctor_with_patient_write_still_passes_the_gate(actors):
    """BOUNDARY: the OR's other arm, untouched by the 2026-08-12 swap.

    A doctor holds `patient.write` and does NOT hold `frontdesk.write`, so every
    doctor-driven mutation in this file passes through the `patient.write` arm —
    this pins that replacing `admin.overview` with `frontdesk.write` left
    clinicians exactly where they were.
    """
    assert has_permission("doctor", PATIENT_WRITE)
    assert not has_permission("doctor", FRONTDESK_WRITE)

    doctor = actors["doctor_send"]
    headers = _auth(doctor, "doctor")
    visit = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND,
              "doctor_id": doctor},
        headers=headers,
    )
    assert visit.status_code == 200, visit.text

    pid, _ = _mk_patient(DEPT_SEND)
    moved = client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV,
              "reason": "Clinical escalation"},
        headers=headers,
    )
    assert moved.status_code == 200, moved.text


def test_a_pharmacist_cannot_reach_the_front_desk_at_all(actors):
    pharmacist = _mk_user("pharmacist", DEPT_SEND)
    headers = _auth(pharmacist, "pharmacist")
    assert client.get("/api/frontdesk/opd/queue", headers=headers).status_code == 403
    assert client.get(
        "/api/frontdesk/transfers/destinations", headers=headers
    ).status_code == 403


def test_a_pharmacist_cannot_mutate_the_desk_either(actors):
    """BOUNDARY: neither arm. A pharmacist holds neither `patient.write` nor
    `frontdesk.write` (nor `patient.read_all`, so the denial lands at the read
    gate the write gate stacks on) — every mutation is 403."""
    assert not has_permission("pharmacist", PATIENT_WRITE)
    assert not has_permission("pharmacist", FRONTDESK_WRITE)

    pharmacist = _mk_user("pharmacist", DEPT_SEND)
    headers = _auth(pharmacist, "pharmacist")
    pid, _ = _mk_patient(DEPT_SEND)

    assert client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND},
        headers=headers,
    ).status_code == 403
    assert client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV,
              "reason": "Not this desk's job"},
        headers=headers,
    ).status_code == 403


def test_frontdesk_write_grants_match_the_ruling():
    """`frontdesk.write` exists for exactly one account shape: the registration
    clerk (today, the desk's admin account). Clinicians keep reaching the desk
    through `patient.write` and were deliberately NOT granted the new permission."""
    assert has_permission("admin", FRONTDESK_WRITE)
    for role in ("doctor", "nurse", "clinician", "pharmacist", "patient", "cashier"):
        assert not has_permission(role, FRONTDESK_WRITE), (
            f"{role} must not hold frontdesk.write"
        )


def test_frontdesk_write_alone_opens_the_desk_and_admin_overview_no_longer_does(
    actors, monkeypatch
):
    """BOUNDARY: the clerk's arm, isolated from every other grant admin holds.

    No seeded role carries `frontdesk.write` without admin's other permissions,
    and `users.role` is CHECK-constrained to the five seeded roles, so a real
    arbitrary-role user cannot exist end-to-end. Instead the pharmacist role —
    which natively holds NOTHING this router consults — is re-granted at runtime
    (ROLE_PERMISSIONS patched in-process, restored on exit; permissions.py is
    not touched) and driven through the REAL endpoints.

    Two grants are stacked deliberately: `require_frontdesk_write` sits on top
    of `require_permission(patient.read_all)`, because working the desk means
    reading the patient register. `frontdesk.write` is the DECIDING arm; the
    read permission is the floor under both arms.
    """
    clerk = _mk_user("pharmacist", DEPT_SEND, name="FrontDesk Synthetic Clerk")
    headers = _auth(clerk, "pharmacist")

    # 1. The minimal clerk grant — read the register + frontdesk.write, with
    #    neither patient.write nor admin.overview — passes every desk mutation.
    monkeypatch.setitem(
        ROLE_PERMISSIONS, "pharmacist", {PATIENT_READ_ALL, FRONTDESK_WRITE}
    )
    visit = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND},
        headers=headers,
    )
    assert visit.status_code == 200, visit.text
    vid = visit.json()["id"]
    assert client.post(
        f"/api/frontdesk/opd/visits/{vid}/start", headers=headers
    ).status_code == 200
    assert client.post(
        f"/api/frontdesk/opd/visits/{vid}/complete",
        json={"service_code": CONSULT_CODE}, headers=headers,
    ).status_code == 200
    v2 = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND},
        headers=headers,
    ).json()
    assert client.post(
        f"/api/frontdesk/opd/visits/{v2['id']}/cancel",
        json={"reason": "Left before being seen"}, headers=headers,
    ).status_code == 200
    pid, _ = _mk_patient(DEPT_SEND)
    assert client.post(
        "/api/frontdesk/transfers",
        json={"patient_id": pid, "to_department": DEPT_RECV,
              "reason": "Clerk-initiated move"},
        headers=headers,
    ).status_code == 200

    # 2. THE OLD GATE IS DEAD. The pre-ruling grant — governance next to the
    #    read permission — no longer opens the desk: holding the audit log is
    #    no longer a way to file an admission.
    monkeypatch.setitem(
        ROLE_PERMISSIONS, "pharmacist", {PATIENT_READ_ALL, ADMIN_OVERVIEW}
    )
    relic = client.post(
        "/api/frontdesk/opd/visits",
        json={"patient_id": _mk_outpatient(), "department": DEPT_SEND},
        headers=headers,
    )
    assert relic.status_code == 403, (
        "admin.overview opened the front desk — the governance permission has "
        "leaked back into the write gate"
    )
    assert relic.json()["detail"]["required"] == [PATIENT_WRITE, FRONTDESK_WRITE]


def test_waiting_time_is_measured_not_assumed(actors):
    """A wait that cannot be computed reads as unknown, never as zero minutes."""
    registered = (datetime.now(timezone.utc) - timedelta(minutes=37)).isoformat()
    visit = {"registered_at": registered, "status": "waiting", "called_at": None}
    assert fd._waiting_minutes(visit) >= 36
    assert fd._waiting_minutes({"registered_at": None, "status": "waiting"}) is None
    assert fd._waiting_minutes({"registered_at": "not a timestamp",
                                "status": "waiting"}) is None
