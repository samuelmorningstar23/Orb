"""Scope sweep: the pharmacy queue, Helix, payments, billing, the payer desk
and the front desk keep each facility's work apart (multisite-9a, W12-E,
wave 12).

Ruling MS-R5 (a): a facility-bound account works on its own facility's
documents (invoices, receipts, refunds, counters, claims, outpatient visits:
the row's own facility_id, migrations 74 and 75) and on the charts of patients
whose latest encounter is there (medicines and supply orders, which have no
facility of their own). Ruling MS-R9 (a): the verify queue is per facility.

Pinned here, on a database this module owns (isolated_module_database), with
a second facility and the fence opened (FACILITY_BLIND_SURFACES = ()) to show
what each lane itself answers:

  * an F2 pharmacist never sees an F1 medicine in /api/pharmacy/pending, and
    verify or reject of an F1 medicine is a 404 with an authorization
    'facility_document_denied' row, before anything is screened or changed;
  * the Helix order lists keep the caller's facilities, and an F1 order's
    transitions are a 404;
  * an F2 cashier cannot take payment against an F1 invoice (404 and the
    audit row), but the F2 desk can bill the F2 outpatient visit of a patient
    last admitted at F1, and the F2 cashier can take that payment;
  * every money list (receipts, refunds, discounts, counters, the day-end
    sheet, the counter search, invoices, claims, policies, the work queue,
    the denial log) keeps the caller's facilities, and every route that names
    another facility's document is a 404 with the audit row;
  * a body naming another facility's admission (a charge, a capture, bed
    days, a bill, a receipt, a refund, a deposit use, a discount request, a
    claim or a policy) is a 404 with the audit row whoever's patient it
    names, and a bill that names no charge sweeps only the caller's
    facility's pending lines;
  * the F2 front desk lists only F2 visits, a visit of F1 is a 404, a clinic
    of F1 is 403 facility_scope and an F1 patient is a 404 (without a group
    match ticket, tests/test_group_identity_match.py);
  * with one facility on record every helper is a no-op (no clause, no
    denial, no audit row) and every list answers exactly as before;
  * the money route groups reach no blind SQL owner, so they are ready to
    leave the fence (the registry, tests/facility_sql_sites/money.py).

Test patients carry invented names (never a word a clinician types). Run with
the test env, never the shipping .env.
"""
from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

import facility_fence as F
import main
from auth import create_token
from database import db_session
from services import billing_masters as BM
from services import billing_service
from services import facility_documents as FD
from services import facility_service as FS
from services import frontdesk_service as FDS
from services import helix_service
from services import payer_service
from services import payments_service as ps
from services import pharmacy_verification_service as pharmacy
from services.clinical_time import utc_iso_now

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9861                       # an id no other module writes
F2_CODE = f"PQ{TAG[:4]}"
F1_DEPT = f"Brennath Medicine {TAG}"
F2_DEPT = f"Ovrelle Medicine {TAG}"

client = TestClient(main.app, raise_server_exceptions=False)


def _hex(n: int = 6) -> str:
    return uuid.uuid4().hex[:n].upper()


def _user(db, role: str, *, department=None, home=None) -> str:
    uid = f"PQ{role[:3].upper()}{_hex(6)}"
    db.execute("INSERT INTO users (id, name, role, department, password_hash) "
               "VALUES (?, ?, ?, ?, 'x')", (uid, f"Quessa Marvell {uid[-4:]}", role, department))
    if home is not None:
        db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                   "granted_at, reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, "
                   "'pharmacy queue sweep')", (uid, home))
    return uid


def _patient(db, name: str, *, facility: int, department=None, active: bool = True) -> tuple:
    pid = f"PQP{_hex(8)}"
    db.execute("INSERT INTO patients (id, name, mrn, age, sex, ward, bed, status, "
               "registered_facility_id) VALUES (?, ?, ?, 52, 'F', ?, '3', ?, ?)",
               (pid, name, f"MRN-{pid}", f"Ward {TAG}",
                "admitted" if active else "discharged", facility))
    aid = None
    if department:
        aid = f"PQA{_hex(8)}"
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, facility_id, created_at) VALUES (?, ?, 'system', ?, ?, '3', ?, ?, ?)",
                   (aid, pid, department, f"Ward {TAG}", "active" if active else "discharged",
                    facility, "2026-09-01T08:00:00+00:00"))
    return pid, aid


def _ai_med(db, pid: str, drug: str) -> int:
    cur = db.execute(
        "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
        "prescribed_by, source) VALUES (?, ?, 'REVIEW REQUIRED', 'oral', 'OD', 'active', "
        "'system', 'llm_extracted')", (pid, drug))
    return int(cur.lastrowid)


def _visit(db, pid: str, department: str, facility: int) -> str:
    vid = f"OPD-{_hex(8)}"
    db.execute("INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
               "visit_type, status, registered_by, registered_at, facility_id) "
               "VALUES (?, ?, ?, ?, ?, 'new', 'waiting', 'system', ?, ?)",
               (vid, pid, department, FDS._hospital_day(None), int(_hex(3), 16) % 900 + 1,
                utc_iso_now(), facility))
    return vid


def _headers(uid: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _denials(target_type: str, target_id) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT event_category, outcome, actor_user_id, metadata_json FROM audit_logs "
            "WHERE action_type = ? AND target_type = ? AND target_id = ? ORDER BY id",
            (FD.DENIAL_ACTION, target_type, str(target_id))).fetchall()
    return [{**dict(r), "metadata": json.loads(r["metadata_json"] or "{}")} for r in rows]


# ── the world ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def world(isolated_module_database):
    from services.helix_db import init_helix_db

    init_helix_db()
    FDS.ensure_frontdesk_schema(force=True)
    w: dict = {}
    with db_session() as db:
        db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, "Ovrelle Satellite"))
        for name, fid in ((F1_DEPT, 1), (F2_DEPT, F2)):
            db.execute("INSERT INTO departments (name, facility_id, seeded_from) "
                       "VALUES (?, ?, 'test')", (name, fid))
        # Clinical cover in each department (the covering list needs it).
        w["doc1"] = _user(db, "doctor", department=F1_DEPT)
        w["doc2"] = _user(db, "doctor", department=F2_DEPT)
        w["pharm1"] = _user(db, "pharmacist")
        w["pharm2"] = _user(db, "pharmacist", home=F2)
        w["cashier1"] = _user(db, "cashier")
        w["cashier2"] = _user(db, "cashier", home=F2)
        w["admin1"] = _user(db, "admin")
        w["admin2"] = _user(db, "admin", home=F2)
        # P1: an inpatient at F1. P2: an inpatient at F2.
        w["p1"], w["a1"] = _patient(db, "Tamsin Orlavy", facility=1, department=F1_DEPT)
        w["p2"], w["a2"] = _patient(db, "Ilvane Rocheby", facility=F2, department=F2_DEPT)
        # P3: discharged from F1, now in an F2 clinic (latest encounter F2).
        w["p3"], w["a3"] = _patient(db, "Corvessa Whitlan", facility=1, department=F1_DEPT,
                                    active=False)
        # P4: a pure outpatient of F1.
        w["p4"], _ = _patient(db, "Marrowen Tull", facility=1, active=False)
        w["med1"] = _ai_med(db, w["p1"], "Vandrozine")
        w["med2"] = _ai_med(db, w["p2"], "Kelparin")
        w["v_f1"] = _visit(db, w["p4"], F1_DEPT, 1)
        w["v_f2"] = _visit(db, w["p3"], F2_DEPT, F2)
        # Receipt, invoice and refund series at F2 (the satellite's own numbering).
        for code in ("RCPT", "INV", "REF"):
            row = db.execute("SELECT document_type FROM document_number_series "
                             "WHERE series_code = ? AND facility_id = 1", (code,)).fetchone()
            db.execute("INSERT INTO document_number_series (facility_id, series_code, "
                       "document_type, prefix, next_number) VALUES (?, ?, ?, ?, 1)",
                       (F2, code, row["document_type"], f"{F2_CODE}{code[:1]}-"))
        # Helix: a supply order for each inpatient.
        db.execute("INSERT INTO helix_medication_master (id, name) VALUES (?, ?)",
                   (f"HXM{TAG}", "Telvorinate 10 mg"))
        for key, pid in (("h1", w["p1"]), ("h2", w["p2"])):
            w[key] = f"HXO-{_hex(8)}"
            db.execute("INSERT INTO helix_patient_med_orders (id, patient_id, medication_id, "
                       "status, prescribed_by, dosage, frequency, route) "
                       "VALUES (?, ?, ?, 'pending', ?, '10 mg', 'OD', 'oral')",
                       (w[key], pid, f"HXM{TAG}", w["doc1"]))
        # Payer desk: one claim at each facility.
        payer = db.execute("INSERT INTO payers (code, name, payer_type) VALUES (?, ?, 'tpa')",
                           (f"PQT{TAG}", "Quillmore Health TPA")).lastrowid
        for key, pid, fid in (("c1", w["p1"], 1), ("c2", w["p2"], F2)):
            w[key] = int(db.execute(
                "INSERT INTO claims (patient_id, payer_id, claim_type, claimed_paise, status, "
                "facility_id, created_at) VALUES (?, ?, 'cashless', 50000, 'submitted', ?, ?)",
                (pid, payer, fid, utc_iso_now())).lastrowid)
            db.execute("INSERT INTO claim_events (claim_id, event_type, denial_code, "
                       "recorded_at) VALUES (?, 'denied', 'OTHER', ?)", (w[key], utc_iso_now()))
    FS.invalidate_cache()
    assert FS.multi_facility_data() is True

    # A priced service item for the money lanes.
    w["code"] = f"TST-PQ{TAG}"
    BM.upsert_service_item(code=w["code"], name="Consultation", category="consultation",
                           gst_treatment="exempt", gst_rate_bp=0)
    # An F1 bill for P3, raised against their F1 admission.
    charge = billing_service.add_charge(patient_id=w["p3"], admission_id=w["a3"],
                                        service_code=w["code"], unit_rate_paise=40000)
    w["inv_f1"] = int(billing_service.create_invoice(
        patient_id=w["p3"], admission_id=w["a3"], invoice_type="final",
        charge_ids=[int(charge["id"])])["id"])
    w["h"] = {k: _headers(w[k], role) for k, role in (
        ("pharm1", "pharmacist"), ("pharm2", "pharmacist"), ("cashier1", "cashier"),
        ("cashier2", "cashier"), ("admin1", "admin"), ("admin2", "admin"))}
    try:
        yield w
    finally:
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def fence_open(monkeypatch):
    """The blind list as it will stand once every screen is swept."""
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


# ═════════════════════════════════════════════════════════════════════════════
#  1. The pharmacist's verify queue (MS-R9 (a))
# ═════════════════════════════════════════════════════════════════════════════

def test_the_world_is_what_it_says(world):
    import permissions as P
    with db_session() as db:
        for key, fid in (("p1", 1), ("p2", F2), ("p3", F2), ("p4", 1)):
            got = db.execute(f"SELECT {P.FACILITY_PATIENT_SQL} FROM patients p WHERE p.id = ?",
                             (world[key],)).fetchone()[0]
            assert got == fid, key
        for key, fid in (("pharm1", 1), ("pharm2", F2), ("cashier2", F2), ("admin2", F2)):
            user = dict(db.execute("SELECT id, role, department FROM users WHERE id = ?",
                                   (world[key],)).fetchone())
            assert FS.facility_scope_for(user, db=db) == frozenset({fid}), key


def test_an_f2_pharmacist_never_sees_an_f1_medicine_in_the_queue(world):
    resp = client.get("/api/pharmacy/pending", headers=world["h"]["pharm2"])
    assert resp.status_code == 200, resp.text
    ids = {m["id"] for m in resp.json()["pending"]}
    assert world["med2"] in ids
    assert world["med1"] not in ids
    assert "Tamsin Orlavy" not in resp.text and "Vandrozine" not in resp.text
    resp = client.get("/api/pharmacy/pending", headers=world["h"]["pharm1"])
    ids = {m["id"] for m in resp.json()["pending"]}
    assert world["med1"] in ids and world["med2"] not in ids


@pytest.mark.parametrize("action, body", [
    ("verify", {"dosage": "5 mg", "route": "oral", "frequency": "OD"}),
    ("reject", {"reason": "Not this patient's medicine"}),
])
def test_verify_or_reject_of_an_f1_medicine_is_a_404_with_an_audit_row(world, action, body):
    before = len(_denials("medication", world["med1"]))
    resp = client.post(f"/api/pharmacy/{world['med1']}/{action}", json=body,
                       headers=world["h"]["pharm2"])
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == pharmacy.NOT_ON_CHART_MESSAGE
    rows = _denials("medication", world["med1"])
    assert len(rows) == before + 1
    row = rows[-1]
    assert row["event_category"] == "authorization" and row["outcome"] == "denied"
    assert row["actor_user_id"] == world["pharm2"]
    assert row["metadata"]["scope_cause"] == "facility"
    assert row["metadata"]["facility_scope"] == [F2]
    assert row["metadata"]["document_facility_id"] == 1
    assert row["metadata"]["lane"] == "pharmacy_verify_queue"
    # Nothing changed on the chart.
    with db_session() as db:
        med = db.execute("SELECT dosage, status, verified_by FROM medications WHERE id = ?",
                         (world["med1"],)).fetchone()
    assert (med["dosage"], med["status"], med["verified_by"]) == ("REVIEW REQUIRED", "active", None)


def test_an_f2_pharmacist_reaches_their_own_facilitys_medicine(world):
    resp = client.post(f"/api/pharmacy/{world['med2']}/verify", json={},
                       headers=world["h"]["pharm2"])
    # Past the facility gate: the service's own refusal (no dose typed yet).
    assert resp.status_code == 422, resp.text
    assert _denials("medication", world["med2"]) == []


def test_an_unknown_medicine_is_refused_as_before_with_no_facility_row(world):
    resp = client.post("/api/pharmacy/987654321/reject", json={"reason": "No such medicine"},
                       headers=world["h"]["pharm2"])
    assert resp.status_code == 400
    assert _denials("medication", 987654321) == []


# ═════════════════════════════════════════════════════════════════════════════
#  2. Helix supply orders
# ═════════════════════════════════════════════════════════════════════════════

def test_the_helix_order_lists_keep_the_callers_facility(world):
    for path in ("/api/helix/orders/pending", "/api/helix/orders"):
        resp = client.get(path, headers=world["h"]["pharm2"])
        assert resp.status_code == 200, (path, resp.text)
        ids = {o["id"] for o in resp.json()["orders"]}
        assert world["h2"] in ids and world["h1"] not in ids, path
    ids = {o["id"] for o in helix_service.get_all_orders(facilities=frozenset({1}))}
    assert world["h1"] in ids and world["h2"] not in ids
    assert helix_service.get_pending_orders(facilities=frozenset()) == []


def test_an_f1_orders_transitions_are_a_404_with_an_audit_row(world):
    resp = client.get(f"/api/helix/orders/{world['h1']}/transitions",
                      headers=world["h"]["pharm2"])
    assert resp.status_code == 404
    assert _denials("helix_order", world["h1"])[-1]["metadata"]["document_facility_id"] == 1
    resp = client.get(f"/api/helix/orders/{world['h2']}/transitions",
                      headers=world["h"]["pharm2"])
    assert resp.status_code == 200


def test_approve_by_name_matches_only_the_callers_facility(world):
    resp = client.post("/api/helix/orders/approve", headers=world["h"]["pharm2"],
                       json={"patient_name": "Tamsin Orlavy", "medication": "Telvorinate",
                             "dose": "10 mg", "route": "oral", "frequency": "OD"})
    # The F1 patient is not a candidate at F2: no patient matched, nobody named.
    assert resp.status_code == 422
    assert resp.json()["detail"]["candidates"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  3. Money: payments and billing (document-keyed)
# ═════════════════════════════════════════════════════════════════════════════

def test_an_f2_cashier_cannot_take_payment_against_an_f1_invoice(world):
    resp = client.post("/api/payments/receipts", headers=world["h"]["cashier2"],
                       json={"patient_id": world["p3"], "amount_paise": 10000, "method": "card",
                             "reference_no": "PQ-REF-1", "invoice_id": world["inv_f1"]})
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Invoice not found"
    row = _denials("invoice", world["inv_f1"])[-1]
    assert row["metadata"]["lane"] == "payments" and row["metadata"]["document_facility_id"] == 1
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM payments WHERE invoice_id = ?",
                          (world["inv_f1"],)).fetchone()[0] == 0
    # The F1 counter can (the patient is not theirs any more, so the patient
    # check refuses it there: the strict reading, kept from before).
    resp = client.post("/api/payments/receipts", headers=world["h"]["cashier1"],
                       json={"patient_id": world["p3"], "amount_paise": 10000, "method": "card",
                             "reference_no": "PQ-REF-2", "invoice_id": world["inv_f1"]})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Patient not found"


def test_the_f2_desk_bills_the_f2_visit_of_a_patient_last_admitted_at_f1(world):
    resp = client.post(f"/api/billing/patients/{world['p3']}/opd-invoice",
                       headers=world["h"]["admin2"],
                       json={"lines": [{"service_code": world["code"],
                                        "unit_rate_paise": 30000}],
                             "issue": False})
    assert resp.status_code == 200, resp.text
    invoice = resp.json()["invoice"]
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM invoices WHERE id = ?",
                          (invoice["id"],)).fetchone()[0] == F2
    world["inv_f2"] = int(invoice["id"])
    # ...and the F2 cashier takes the payment, numbered from F2's own series.
    resp = client.post("/api/payments/receipts", headers=world["h"]["cashier2"],
                       json={"patient_id": world["p3"], "amount_paise": 30000, "method": "card",
                             "reference_no": "PQ-REF-3", "invoice_id": world["inv_f2"]})
    assert resp.status_code == 200, resp.text
    receipt = resp.json()
    assert receipt["receipt_number"].startswith(f"{F2_CODE}R-")
    world["rcpt_f2"] = int(receipt["id"])
    # The F2 invoice is the F1 desk's 404.
    resp = client.get(f"/api/billing/invoices/{world['inv_f2']}", headers=world["h"]["admin1"])
    assert resp.status_code == 404


def test_the_money_lists_keep_the_callers_facility(world):
    resp = client.get("/api/billing/invoices", headers=world["h"]["admin2"])
    ids = {i["id"] for i in resp.json()["invoices"]}
    assert world["inv_f1"] not in ids and world["inv_f2"] in ids
    resp = client.get("/api/billing/invoices", headers=world["h"]["admin1"])
    ids = {i["id"] for i in resp.json()["invoices"]}
    assert world["inv_f1"] in ids and world["inv_f2"] not in ids
    resp = client.get("/api/payments/receipts", headers=world["h"]["cashier1"])
    assert world["rcpt_f2"] not in {r["id"] for r in resp.json()["receipts"]}
    resp = client.get("/api/payments/receipts", headers=world["h"]["cashier2"])
    assert world["rcpt_f2"] in {r["id"] for r in resp.json()["receipts"]}
    # The counter's patient search: the F1 inpatient is not found at F2.
    resp = client.get("/api/payments/patients", params={"q": "Tamsin"},
                      headers=world["h"]["cashier2"])
    assert resp.status_code == 200 and resp.json()["patients"] == []
    resp = client.get("/api/payments/patients", params={"q": "Corvessa"},
                      headers=world["h"]["cashier2"])
    assert [p["id"] for p in resp.json()["patients"]] == [world["p3"]]
    # Open bills offered for the patient at F2: F2's only.
    resp = client.get(f"/api/payments/patients/{world['p3']}/invoices",
                      headers=world["h"]["cashier2"])
    assert world["inv_f1"] not in {i["invoice_id"] for i in resp.json()["invoices"]}


@pytest.mark.parametrize("method, path, body, target_type", [
    ("GET", "/api/payments/receipts/{rcpt_f2}", None, "payment"),
    ("GET", "/api/payments/receipts/{rcpt_f2}/print", None, "payment"),
    ("POST", "/api/payments/receipts/{rcpt_f2}/reverse", {"reason": "Wrong drawer"}, "payment"),
    ("GET", "/api/billing/invoices/{inv_f2}/print", None, "invoice"),
    ("POST", "/api/billing/invoices/{inv_f2}/issue", {}, "invoice"),
    ("POST", "/api/billing/invoices/{inv_f2}/cancel", {"reason": "Raised in error"}, "invoice"),
])
def test_another_facilitys_document_is_a_404_with_an_audit_row(world, method, path, body,
                                                                 target_type):
    url = path.format(**world)
    doc_id = url.split("/")[4]
    who = world["h"]["admin1"]
    resp = client.get(url, headers=who) if method == "GET" else client.post(url, headers=who,
                                                                            json=body)
    assert resp.status_code == 404, (url, resp.text)
    row = _denials(target_type, doc_id)[-1]
    assert row["actor_user_id"] == world["admin1"]
    assert row["metadata"]["document_facility_id"] == F2
    assert row["metadata"]["facility_scope"] == [1]


def test_counters_and_the_day_end_sheet_are_per_facility(world):
    session = ps.open_counter_session(counter_code=f"PQ{_hex(4)}", opened_by=world["cashier2"])
    sid = int(session["id"])
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM cash_counter_sessions WHERE id = ?",
                          (sid,)).fetchone()[0] == F2
    resp = client.get("/api/payments/counters", headers=world["h"]["cashier1"])
    assert sid not in {s["id"] for s in resp.json()["sessions"]}
    resp = client.get(f"/api/payments/counters/{sid}/expected", headers=world["h"]["cashier1"])
    assert resp.status_code == 404
    assert _denials("counter_session", sid)
    resp = client.get(f"/api/payments/counters/{sid}/expected", headers=world["h"]["cashier2"])
    assert resp.status_code == 200
    # The day-end sheet: F1's shows none of F2's drawer or receipt.
    sheet1 = ps.day_end_summary(facilities=frozenset({1}))
    sheet2 = ps.day_end_summary(facilities=frozenset({F2}))
    assert sid not in {s["id"] for s in sheet1["sessions"]}
    assert sid in {s["id"] for s in sheet2["sessions"]}
    assert sheet2["totals"]["in_paise"] >= 30000
    whole = ps.day_end_summary()
    assert whole["totals"]["in_paise"] == sheet1["totals"]["in_paise"] + sheet2["totals"]["in_paise"]
    ps.close_counter_session(sid, declared_cash_paise=0, closed_by=world["cashier2"])


def test_refunds_and_discounts_are_per_facility(world):
    refund = ps.request_refund(patient_id=world["p3"], amount_paise=1000,
                               reason="Charged twice at the counter", requested_by=world["cashier2"],
                               payment_id=world["rcpt_f2"])
    rid = int(refund["id"])
    assert rid not in {r["id"] for r in ps.list_refunds(facilities=frozenset({1}))}
    assert rid in {r["id"] for r in ps.list_refunds(facilities=frozenset({F2}))}
    resp = client.post(f"/api/payments/refunds/{rid}/decision", json={"approve": True},
                       headers=world["h"]["admin1"])
    assert resp.status_code == 404
    assert _denials("refund", rid)
    discount = ps.request_discount(patient_id=world["p3"], reason="Senior citizen concession",
                                   requested_by=world["cashier2"], invoice_id=world["inv_f2"],
                                   requested_paise=500)
    did = int(discount["id"])
    assert did not in {d["id"] for d in ps.list_discounts(facilities=frozenset({1}))}
    assert did in {d["id"] for d in ps.list_discounts(facilities=frozenset({F2}))}
    resp = client.post(f"/api/payments/discounts/{did}/decision", json={"approve": False},
                       headers=world["h"]["admin1"])
    assert resp.status_code == 404


def _count(sql: str, *args) -> int:
    with db_session() as db:
        return int(db.execute(sql, args).fetchone()[0])


def _admission_denial(world, actor: str, lane: str) -> dict:
    row = _denials("admission", world["a3"])[-1]
    assert row["actor_user_id"] == world[actor]
    assert row["event_category"] == "authorization" and row["outcome"] == "denied"
    assert row["metadata"]["lane"] == lane
    assert row["metadata"]["document_facility_id"] == 1
    assert row["metadata"]["facility_scope"] == [F2]
    return row


@pytest.mark.parametrize("path, body", [
    ("/api/billing/patients/{pid}/charges", {"unit_rate_paise": 20000}),
    ("/api/billing/patients/{pid}/charges/capture", {"dry_run": False}),
    ("/api/billing/patients/{pid}/charges/capture", {"dry_run": True}),
    ("/api/billing/patients/{pid}/bed-days", {}),
    ("/api/billing/patients/{pid}/invoices", {"invoice_type": "interim"}),
])
@pytest.mark.parametrize("patient", ["p3", "p2"])
def test_a_charge_route_naming_another_facilitys_admission_is_a_404_with_an_audit_row(
        world, path, body, patient):
    """An F2 account names P3's F1 stay, through P3 (in F2's scope now) or
    through P2 (another patient of F2's): 404 and the audit row before any
    line is posted, captured or billed, whoever the admission belongs to."""
    before = len(_denials("admission", world["a3"]))
    charges = _count("SELECT COUNT(*) FROM encounter_charges WHERE admission_id = ?", world["a3"])
    payload = {"admission_id": world["a3"], "service_code": world["code"], **body}
    resp = client.post(path.format(pid=world[patient]), json=payload,
                       headers=world["h"]["admin2"])
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Admission not found"
    assert len(_denials("admission", world["a3"])) == before + 1
    _admission_denial(world, "admin2", "billing")
    assert _count("SELECT COUNT(*) FROM encounter_charges WHERE admission_id = ?",
                  world["a3"]) == charges


def test_a_charge_on_the_callers_own_admission_passes_the_facility_gate(world):
    resp = client.post(f"/api/billing/patients/{world['p2']}/charges",
                       headers=world["h"]["admin2"],
                       json={"admission_id": world["a2"], "service_code": world["code"],
                             "unit_rate_paise": 20000})
    assert resp.status_code == 200, resp.text
    charge = resp.json()["charge"]
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM encounter_charges WHERE id = ?",
                          (charge["id"],)).fetchone()[0] == F2
    assert _denials("admission", world["a2"]) == []


def test_a_bill_naming_no_charge_sweeps_only_the_callers_facilitys_lines(world):
    """The pending outpatient lines of P3 at F1 stay pending for F1's counter
    when the F2 desk bills P3 without naming charges; with no facility limit
    (one facility on record) the sweep is what it always was."""
    f1_line = billing_service.add_charge(patient_id=world["p3"], service_code=world["code"],
                                         unit_rate_paise=25000, encounter_type="op")
    with db_session() as db:
        db.execute("UPDATE encounter_charges SET facility_id = 1 WHERE id = ?",
                   (int(f1_line["id"]),))
    f2_line = billing_service.add_charge(patient_id=world["p3"], service_code=world["code"],
                                         unit_rate_paise=15000, encounter_type="op")
    with db_session() as db:
        assert FD.charge_facility(db, int(f2_line["id"])) == F2
    # Named, the F1 line is the route's 404 (the check that was already there).
    resp = client.post(f"/api/billing/patients/{world['p3']}/invoices",
                       headers=world["h"]["admin2"],
                       json={"invoice_type": "final", "charge_ids": [int(f1_line["id"])]})
    assert resp.status_code == 404 and resp.json()["detail"] == "Charge not found"
    # Swept, only F2's line goes on F2's bill.
    resp = client.post(f"/api/billing/patients/{world['p3']}/invoices",
                       headers=world["h"]["admin2"], json={"invoice_type": "final"})
    assert resp.status_code == 200, resp.text
    invoice = resp.json()["invoice"]
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM invoices WHERE id = ?",
                          (invoice["id"],)).fetchone()[0] == F2
        swept = {int(r[0]) for r in db.execute(
            "SELECT encounter_charge_id FROM invoice_lines WHERE invoice_id = ?",
            (invoice["id"],)).fetchall()}
        f1 = db.execute("SELECT status, invoice_id, facility_id FROM encounter_charges "
                        "WHERE id = ?", (int(f1_line["id"]),)).fetchone()
    assert int(f2_line["id"]) in swept and int(f1_line["id"]) not in swept
    assert (f1["status"], f1["invoice_id"], f1["facility_id"]) == ("pending", None, 1)
    # Nothing left at F2: the sweep refuses rather than take F1's line.
    with pytest.raises(billing_service.BillingError) as exc:
        billing_service.create_invoice(patient_id=world["p3"], facilities=frozenset({F2}))
    assert exc.value.code == "no_charges"
    # The admission branch keeps the facility too: P2's F2 stay has no F1 line.
    with pytest.raises(billing_service.BillingError) as exc:
        billing_service.create_invoice(patient_id=world["p2"], admission_id=world["a2"],
                                       facilities=frozenset({1}))
    assert exc.value.code == "no_charges"
    # No facility limit: the sweep is unchanged and takes the F1 line.
    old = billing_service.create_invoice(patient_id=world["p3"], invoice_type="final")
    assert int(f1_line["id"]) in {int(line["encounter_charge_id"]) for line in old["lines"]}


def test_a_money_write_naming_another_facilitys_admission_is_a_404_with_an_audit_row(world):
    """A receipt, refund, deposit use or discount request that names P3's F1
    stay is refused at the F2 counter before a number is drawn, whoever's
    patient id the body carries (the service would stamp it with the stay's
    facility, F1's series)."""
    h = world["h"]["cashier2"]
    receipts = _count("SELECT COUNT(*) FROM payments WHERE admission_id = ?", world["a3"])
    refunds = _count("SELECT COUNT(*) FROM refunds WHERE admission_id = ?", world["a3"])
    discounts = _count("SELECT COUNT(*) FROM discount_approvals WHERE admission_id = ?",
                       world["a3"])
    for patient in ("p3", "p2"):
        resp = client.post("/api/payments/receipts", headers=h,
                           json={"patient_id": world[patient], "amount_paise": 5000,
                                 "method": "card", "reference_no": f"PQ-ADV-{patient}",
                                 "purpose": "advance", "admission_id": world["a3"]})
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Admission not found"
        _admission_denial(world, "cashier2", "payments")
    calls = [
        ("/api/payments/refunds", {"patient_id": world["p3"], "amount_paise": 100,
                                   "reason": "Advance returned at the counter"}),
        ("/api/payments/deposits/apply", {"patient_id": world["p3"], "amount_paise": 100,
                                          "invoice_id": world["inv_f2"]}),
        ("/api/payments/discounts", {"patient_id": world["p3"], "requested_paise": 100,
                                     "reason": "Staff family concession",
                                     "invoice_id": world["inv_f2"]}),
    ]
    for path, body in calls:
        before = len(_denials("admission", world["a3"]))
        resp = client.post(path, headers=h, json={**body, "admission_id": world["a3"]})
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"] == "Admission not found", path
        assert len(_denials("admission", world["a3"])) == before + 1, path
    assert _count("SELECT COUNT(*) FROM payments WHERE admission_id = ?", world["a3"]) == receipts
    assert _count("SELECT COUNT(*) FROM refunds WHERE admission_id = ?", world["a3"]) == refunds
    assert _count("SELECT COUNT(*) FROM discount_approvals WHERE admission_id = ?",
                  world["a3"]) == discounts
    # A discount on another facility's charge line is refused the same way.
    f1_charge = _count("SELECT MIN(id) FROM encounter_charges WHERE admission_id = ?",
                       world["a3"])
    resp = client.post("/api/payments/discounts", headers=h,
                       json={"patient_id": world["p3"], "requested_paise": 100,
                             "reason": "Staff family concession", "scope": "line",
                             "encounter_charge_id": f1_charge})
    assert resp.status_code == 404 and resp.json()["detail"] == "Charge not found"
    assert _denials("encounter_charge", f1_charge)[-1]["metadata"]["lane"] == "payments"


def test_the_f2_counter_takes_an_advance_against_its_own_stay(world):
    resp = client.post("/api/payments/receipts", headers=world["h"]["cashier2"],
                       json={"patient_id": world["p2"], "amount_paise": 5000, "method": "card",
                             "reference_no": "PQ-ADV-OWN", "purpose": "advance",
                             "admission_id": world["a2"]})
    assert resp.status_code == 200, resp.text
    receipt = resp.json()
    assert receipt["receipt_number"].startswith(f"{F2_CODE}R-")
    with db_session() as db:
        assert db.execute("SELECT facility_id FROM payments WHERE id = ?",
                          (receipt["id"],)).fetchone()[0] == F2
    assert _denials("admission", world["a2"]) == []


def test_the_admission_lookup_reads_the_stays_own_row(world):
    """A blank admission id is no admission, an unknown one is no refusal
    (the service answers it as before), and a stay is read by its own row
    whoever's patient id the request carries."""
    assert FD.admission_ref(None) is None
    assert FD.admission_ref("   ") is None
    assert FD.admission_ref(" PQA1 ") == "PQA1"
    with db_session() as db:
        assert FD.admission_row_facility(db, "") is None
        assert FD.admission_row_facility(db, "PQA-no-such-stay") is None
        assert FD.admission_row_facility(db, world["a3"]) == 1
        assert FD.admission_row_facility(db, f" {world['a2']} ") == F2


def test_with_one_facility_a_named_admission_is_not_checked(world, monkeypatch):
    """With one facility on record facility_scope is None: the gate reads
    nothing and writes no row, and the route answers as it always did."""
    monkeypatch.setattr(FS, "multi_facility_data", lambda *a, **k: False)
    FS.invalidate_cache()
    before = len(_denials("admission", world["a3"]))
    resp = client.post(f"/api/billing/patients/{world['p3']}/charges/capture",
                       headers=world["h"]["admin2"],
                       json={"admission_id": world["a3"], "dry_run": True})
    assert resp.status_code == 200, resp.text
    assert len(_denials("admission", world["a3"])) == before


# ═════════════════════════════════════════════════════════════════════════════
#  4. The payer desk
# ═════════════════════════════════════════════════════════════════════════════

def test_the_payer_desk_keeps_the_callers_facility(world):
    resp = client.get("/api/payer/claims", headers=world["h"]["admin2"])
    assert resp.status_code == 200, resp.text
    ids = {c["id"] for c in resp.json()["claims"]}
    assert world["c2"] in ids and world["c1"] not in ids
    assert "Tamsin Orlavy" not in resp.text
    for path, key in (("/api/payer/work-queue", "claims"), ("/api/payer/denials", "denials")):
        resp = client.get(path, headers=world["h"]["admin2"])
        assert resp.status_code == 200, (path, resp.text)
        assert "Tamsin Orlavy" not in resp.text, path
    summary = payer_service.desk_summary(facilities=frozenset({F2}))
    assert summary["open_claims"] == 1
    resp = client.get(f"/api/payer/claims/{world['c1']}", headers=world["h"]["admin2"])
    assert resp.status_code == 404
    assert _denials("claim", world["c1"])[-1]["metadata"]["lane"] == "payer"
    resp = client.post(f"/api/payer/claims/{world['c1']}/note", json={"note": "Chased by phone"},
                       headers=world["h"]["admin2"])
    assert resp.status_code == 404
    resp = client.get(f"/api/payer/claims/{world['c2']}", headers=world["h"]["admin2"])
    assert resp.status_code == 200


@pytest.mark.parametrize("path, body", [
    ("/api/payer/claims", {"claim_type": "cashless"}),
    ("/api/payer/policies", {"policy_number": "QZ-PQ-0042"}),
])
def test_a_claim_or_policy_naming_another_facilitys_stay_is_a_404(world, path, body):
    """P2 is F2's; the stay named is P3's at F1. A claim or policy is stamped
    with the named stay's facility, so the desk refuses it by the stay's own
    row before anything is numbered or stored."""
    claims = _count("SELECT COUNT(*) FROM claims WHERE admission_id = ?", world["a3"])
    resp = client.post(path, headers=world["h"]["admin2"],
                       json={"patient_id": world["p2"], "payer_code": f"PQT{TAG}",
                             "admission_id": world["a3"], **body})
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Admission not found"
    _admission_denial(world, "admin2", "payer")
    assert _count("SELECT COUNT(*) FROM claims WHERE admission_id = ?", world["a3"]) == claims
    assert _count("SELECT COUNT(*) FROM patient_payer_policies WHERE admission_id = ?",
                  world["a3"]) == 0


# ═════════════════════════════════════════════════════════════════════════════
#  5. The front desk
# ═════════════════════════════════════════════════════════════════════════════

def test_the_f2_front_desk_lists_only_f2_visits(world):
    resp = client.get("/api/frontdesk/opd/queue", headers=world["h"]["admin2"])
    assert resp.status_code == 200, resp.text
    ids = {v["id"] for q in resp.json()["queues"] for v in q["visits"]}
    assert world["v_f2"] in ids and world["v_f1"] not in ids
    assert "Marrowen Tull" not in resp.text
    resp = client.get("/api/frontdesk/opd/queue", headers=world["h"]["admin1"])
    ids = {v["id"] for q in resp.json()["queues"] for v in q["visits"]}
    assert world["v_f1"] in ids and world["v_f2"] not in ids


def test_an_f1_visit_is_a_404_at_the_f2_desk_with_an_audit_row(world):
    resp = client.get(f"/api/frontdesk/opd/visits/{world['v_f1']}", headers=world["h"]["admin2"])
    assert resp.status_code == 404
    row = _denials("opd_visit", world["v_f1"])[-1]
    assert row["metadata"]["lane"] == "frontdesk_opd"
    resp = client.post(f"/api/frontdesk/opd/visits/{world['v_f1']}/cancel",
                       json={"reason": "Left before being seen"}, headers=world["h"]["admin2"])
    assert resp.status_code == 404
    with db_session() as db:
        assert db.execute("SELECT status FROM opd_visits WHERE id = ?",
                          (world["v_f1"],)).fetchone()[0] == "waiting"


def test_the_desk_lists_only_its_own_facilitys_clinics_and_doctors(world):
    resp = client.get("/api/frontdesk/opd/doctors", headers=world["h"]["admin2"])
    depts = {d["department"] for d in resp.json()["doctors"]}
    assert F2_DEPT in depts and F1_DEPT not in depts
    resp = client.get("/api/frontdesk/transfers/destinations", headers=world["h"]["admin2"])
    depts = {d["department"] for d in resp.json()["destinations"]}
    assert F2_DEPT in depts and F1_DEPT not in depts


def test_registering_into_another_facilitys_clinic_is_403_and_audited(world):
    resp = client.post("/api/frontdesk/opd/visits", headers=world["h"]["admin2"],
                       json={"patient_id": world["p3"], "department": F1_DEPT})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error"] == "facility_scope"
    with db_session() as db:
        row = db.execute("SELECT metadata_json FROM audit_logs WHERE action_type = "
                         "'cross_facility_move_refused' AND actor_user_id = ? ORDER BY id DESC "
                         "LIMIT 1", (world["admin2"],)).fetchone()
    meta = json.loads(row["metadata_json"])
    assert meta["lane"] == "opd_registration" and meta["cause"] == "actor_outside_facility"


def test_registering_an_f1_patient_at_the_f2_desk_is_a_404_and_audited(world):
    resp = client.post("/api/frontdesk/opd/visits", headers=world["h"]["admin2"],
                       json={"patient_id": world["p4"], "department": F2_DEPT})
    assert resp.status_code == 404, resp.text
    with db_session() as db:
        row = db.execute("SELECT metadata_json FROM audit_logs WHERE action_type = "
                         "'patient_scope_denied' AND patient_id = ? ORDER BY id DESC LIMIT 1",
                         (world["p4"],)).fetchone()
    meta = json.loads(row["metadata_json"])
    assert meta["scope_cause"] == "facility" and meta["scope_helper"] == "frontdesk_registration"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM opd_visits WHERE patient_id = ? AND "
                          "department = ?", (world["p4"], F2_DEPT)).fetchone()[0] == 0


def test_the_f2_desk_registers_its_own_patient(world):
    resp = client.post("/api/frontdesk/opd/visits", headers=world["h"]["admin2"],
                       json={"patient_id": world["p3"], "department": F2_DEPT})
    assert resp.status_code == 200, resp.text
    assert FDS.opd_visit_facility_id(resp.json()["id"]) == F2


def test_the_day_summary_counts_only_the_callers_facility(world):
    from services import front_office_mis

    f1 = front_office_mis.daily_summary(None, facilities=frozenset({1}))
    f2 = front_office_mis.daily_summary(None, facilities=frozenset({F2}))
    assert F1_DEPT in {c["department"] for c in f1["clinics"]}
    assert F1_DEPT not in {c["department"] for c in f2["clinics"]}
    assert F2_DEPT in {c["department"] for c in f2["clinics"]}


# ═════════════════════════════════════════════════════════════════════════════
#  6. With one facility on record nothing changes
# ═════════════════════════════════════════════════════════════════════════════

def test_the_helpers_are_no_ops_without_a_facility_limit():
    assert FD.facility_clause("x.facility_id", None) == ("", ())
    assert FD.facility_condition("x.facility_id", None) == (None, ())
    assert FD.patient_facility_clause("x.patient_id", None) == ("", ())
    assert FD.discount_facility_condition(None) == (None, ())
    assert FD.document_denied(2, None) is False
    assert FD.document_denied(None, frozenset({1})) is False
    assert FD.document_denied(1, frozenset()) is True
    assert FD.facility_clause("f", frozenset()) == (" AND 0", ())
    assert FD.facility_clause("f", frozenset({3, 1})) == (" AND f IN (?, ?)", (1, 3))


def test_with_one_facility_every_list_answers_exactly_as_before(world, monkeypatch):
    """facility_scope answers None with one facility on record; a None scope
    passed to every list gives the same rows as no argument at all."""
    pairs = [
        (pharmacy.list_pending_verification, {}),
        (helix_service.get_pending_orders, {}),
        (helix_service.get_all_orders, {}),
        (ps.list_payments, {}),
        (ps.list_refunds, {}),
        (ps.list_discounts, {}),
        (ps.list_counter_sessions, {}),
        (billing_service.list_invoices, {}),
        (payer_service.list_claims, {}),
        (payer_service.list_policies, {}),
    ]
    for fn, kwargs in pairs:
        assert fn(**kwargs) == fn(**kwargs, facilities=None), fn.__name__
    q1 = FDS.list_opd_queue()
    q2 = FDS.list_opd_queue(facilities=None)
    assert q1 == q2
    monkeypatch.setattr(FS, "multi_facility_data", lambda *a, **k: False)
    import permissions as P
    assert P.facility_scope({"id": world["pharm2"], "role": "pharmacist"}) is None


# ═════════════════════════════════════════════════════════════════════════════
#  7. The registry: the money groups are ready to leave the fence
# ═════════════════════════════════════════════════════════════════════════════

def test_the_money_route_groups_reach_no_blind_sql_owner():
    """Every route under /api/billing, /api/payments and /api/payer reaches
    only facility-aware or appliance SQL owners on the call graph, so the
    wave that regenerates the fence can move these three groups to 'cleared'.
    /api/pharmacy, /api/helix and /api/frontdesk are swept too, but still
    reach rows of other areas (medication orders, drug safety, ADT, audit)."""
    from tests import facility_sql_sites as R

    g = R.graph()
    found = R.scan()
    rows = R.registry()
    problems = []
    checked = 0
    for r in R.app_routes(main.app, g):
        if r.node is None or R.surface_for(r.path, ("/api/billing", "/api/payments",
                                                    "/api/payer")) is None:
            continue
        checked += 1
        for key in sorted(g.reach(r.node) & set(found)):
            _, row = R.row_for(rows, *key)
            if row is None or row.kind == R.BLIND:
                problems.append(f"{r.path} reaches {key}")
    assert checked > 60
    assert problems == []
    # Every money-area row these groups reach is a safe kind now.
    money = {k for k, area in R.row_areas().items() if area == "money"}
    for key in ("services/payments_service.py", "services/billing_service.py",
                "services/payer_service.py"):
        assert all(rows[k].kind != R.BLIND for k in money if k[0] == key), key
