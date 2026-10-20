"""Billing — the three-tier permission split (2026-08-18).

Until today every route in routes/billing_routes.py borrowed ``admin.overview``
— the governance permission that also opens the audit log — because the
dedicated pair did not exist yet. It does now, and it is a TRIO, because the
desk holds two different pens:

  billing.read                    every read, the tariff list included
  billing.read + billing.write    what happened to ONE patient: charges,
                                  invoices, credit notes
  billing.read + billing.tariff   the TARIFF MASTER: service items, plans,
                                  rates, packages — whoever edits the price
                                  list changes every FUTURE invoice

These tests are what makes the split true rather than merely written down:
the role table says who holds what, clinical roles bounce off every route,
read-alone cannot touch anything, write-alone cannot move a price, and a
structural sweep over the router proves no endpoint is on the wrong tier —
including any endpoint added after tonight.

Idioms are borrowed deliberately: the minimal-app client from
tests/test_billing.py, the MonkeyPatch-the-role-table boundary tests and the
dependency-closure drift guard from tests/test_payer.py.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import config
import permissions as perms
from auth import create_token
from database import db_session
from services import billing_masters, billing_service
from services.money import rupees


# ── fixtures (tests/test_billing.py idioms) ───────────────────────────────────

app = FastAPI()
from routes.billing_routes import router as billing_router  # noqa: E402

app.include_router(billing_router)
client = TestClient(app)


def _auth(role="admin", uid=None):
    """A Bearer header for a user row of the given role.

    Seeded WITHOUT a department on purpose: permissions.department_scope treats
    that as the hospital-wide back-office convention, so these tests exercise
    the PERMISSION gate, not ward scoping (which has its own suite).
    """
    uid = uid or f"BPERM-{role.upper()[:3]}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,'x')",
            (uid, f"{role} billing-perm", role),
        )
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _make_patient():
    pid = f"PATBPR{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed) VALUES (?, ?, ?, 'W1', '3')",
            (pid, "Billing Perm Test", f"MRN-{uuid.uuid4().hex[:6]}"),
        )
    return pid


@pytest.fixture()
def patient():
    return _make_patient()


@pytest.fixture()
def plan():
    code = f"TP{uuid.uuid4().hex[:6].upper()}"
    row = billing_service.upsert_tariff_plan(code=code, name="Perm test plan")
    return int(row["id"])


def _priced_code(plan_id, rate="500"):
    """A TST- service item priced on `plan_id`, created at the SERVICE layer —
    these tests are about the HTTP gate, so the fixtures must not depend on it.
    (The TST- prefix is load-bearing: tests/test_money.py exempts it from the
    no-seeded-prices rule.)"""
    code = f"TST-PERM{uuid.uuid4().hex[:6].upper()}"
    billing_masters.upsert_service_item(
        code=code, name="Perm consult", category="consultation")
    billing_masters.set_tariff_rate(
        tariff_plan_id=plan_id, service_code=code, rate_paise=rupees(rate))
    return code


# ── the role table says what the split assumes ────────────────────────────────

def test_role_grants_match_the_split():
    """`admin` holds all three; no other role holds any.

    Cashier is pinned HERE and only here: the users table's CHECK constraint
    has no 'cashier' spelling yet, so no cashier account can authenticate and a
    behavioural 403 for it cannot be staged. The role table is the strongest
    statement available — the day the constraint is widened, the behavioural
    tests below want a cashier row added.
    """
    trio = {perms.BILLING_READ, perms.BILLING_WRITE, perms.BILLING_TARIFF}
    assert trio <= perms.ROLE_PERMISSIONS["admin"]
    for role in ("doctor", "nurse", "pharmacist", "cashier", "patient"):
        held = trio & perms.ROLE_PERMISSIONS[role]
        assert not held, f"{role} unexpectedly holds {held}"


# ── behavioural boundaries ────────────────────────────────────────────────────

def test_routes_require_authentication():
    assert client.get("/api/billing/invoices").status_code in (401, 403)
    assert client.post(
        "/api/billing/service-items", json={"code": "TST-ANON", "name": "x"}
    ).status_code in (401, 403)


def test_clinical_roles_do_not_reach_the_billing_desk(patient):
    """A doctor writes orders, not invoices. Reads and writes both refuse, and
    the refusal names the desk's OWN permission — if `required` ever reads
    `admin.overview` again, the split has been reverted somewhere."""
    for role, uid in (("doctor", "BPERM-DOC"), ("nurse", "BPERM-NUR"),
                      ("pharmacist", "BPERM-PHR")):
        headers = _auth(role=role, uid=uid)

        for path in ("/api/billing/config", "/api/billing/invoices",
                     "/api/billing/service-items", "/api/billing/tariff-rates",
                     f"/api/billing/patients/{patient}/charges",
                     f"/api/billing/patients/{patient}/summary"):
            resp = client.get(path, headers=headers)
            assert resp.status_code == 403, f"{role} read {path}"

        body = client.get("/api/billing/invoices", headers=headers).json()
        assert body["detail"]["required"] == ["billing.read"], role

        writes = [
            (f"/api/billing/patients/{patient}/charges", {"service_code": "TST-X"}),
            (f"/api/billing/patients/{patient}/invoices", {}),
            ("/api/billing/charges/1/cancel", {"reason": "not mine to cancel"}),
            ("/api/billing/invoices/1/issue", {}),
            ("/api/billing/service-items", {"code": "TST-X", "name": "x"}),
            ("/api/billing/tariff-rates",
             {"tariff_plan_id": 1, "service_code": "TST-X", "rate_paise": 1}),
        ]
        for path, payload in writes:
            resp = client.post(path, json=payload, headers=headers)
            assert resp.status_code == 403, f"{role} wrote {path}"

    # The one deliberate exception: the discharge advisory is gated on
    # discharge.read — the person doing the discharge needs the warning, and a
    # pharmacist (no discharge.read) is refused while doctor and nurse are not.
    for role, uid, expected in (("doctor", "BPERM-DOC", 200),
                                ("nurse", "BPERM-NUR", 200),
                                ("pharmacist", "BPERM-PHR", 403)):
        resp = client.get(
            f"/api/billing/patients/{patient}/discharge-advisory",
            headers=_auth(role=role, uid=uid),
        )
        assert resp.status_code == expected, role


def test_a_holder_of_billing_read_alone_can_look_but_not_touch(patient, plan):
    """Reading the desk is a finance manager's Monday; every mutation is
    refused, each naming exactly the tier it is missing."""
    headers = _auth()
    code = _priced_code(plan)

    read_only = set(perms.ROLE_PERMISSIONS["admin"]) - {
        perms.BILLING_WRITE, perms.BILLING_TARIFF}
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(perms.ROLE_PERMISSIONS, "admin", read_only)

        for path in ("/api/billing/config", "/api/billing/service-items",
                     "/api/billing/tariff-plans", "/api/billing/tariff-rates",
                     "/api/billing/packages", "/api/billing/invoices",
                     f"/api/billing/rate/{code}",
                     f"/api/billing/patients/{patient}/charges",
                     f"/api/billing/patients/{patient}/summary"):
            assert client.get(path, headers=headers).status_code == 200, path

        charge_writes = [
            (f"/api/billing/patients/{patient}/charges",
             {"service_code": code, "tariff_plan_id": plan}),
            (f"/api/billing/patients/{patient}/charges/capture", {}),
            (f"/api/billing/patients/{patient}/bed-days",
             {"admission_id": "ADM-RO", "service_code": code}),
            (f"/api/billing/patients/{patient}/invoices", {}),
            (f"/api/billing/patients/{patient}/opd-invoice",
             {"lines": [{"service_code": code}]}),
            ("/api/billing/charges/999999/cancel", {"reason": "read-only desk"}),
            ("/api/billing/invoices/999999/issue", {}),
            ("/api/billing/invoices/999999/cancel", {"reason": "read-only desk"}),
            ("/api/billing/invoices/999999/credit-note", {"reason": "read-only desk"}),
        ]
        for path, payload in charge_writes:
            resp = client.post(path, json=payload, headers=headers)
            assert resp.status_code == 403, f"{path} was writable on billing.read alone"
            assert resp.json()["detail"]["missing"] == ["billing.write"], path

        tariff_writes = [
            ("/api/billing/service-items", {"code": "TST-RO", "name": "x"}),
            ("/api/billing/tariff-plans", {"code": "TPRO", "name": "x"}),
            ("/api/billing/tariff-rates",
             {"tariff_plan_id": plan, "service_code": code, "rate_paise": 1}),
            ("/api/billing/packages",
             {"tariff_plan_id": plan, "package_code": "PKG-RO", "name": "x",
              "package_rate_paise": 1}),
        ]
        for path, payload in tariff_writes:
            resp = client.post(path, json=payload, headers=headers)
            assert resp.status_code == 403, f"{path} was writable on billing.read alone"
            assert resp.json()["detail"]["missing"] == ["billing.tariff"], path

    # Nothing leaked past the refusals.
    assert billing_service.list_charges(patient_id=patient) == []
    assert billing_service.list_invoices(patient_id=patient) == []


def test_the_tariff_pen_is_not_implied_by_the_charge_pen(patient, plan):
    """THE POINT OF THE THIRD TIER. An account holding billing.read+write runs
    the counter end to end — captures the charge, raises and issues the bill —
    and still cannot move the price list that every future invoice reads."""
    headers = _auth()
    code = _priced_code(plan)
    refused_code = f"TST-DENIED{uuid.uuid4().hex[:6].upper()}"

    no_tariff = set(perms.ROLE_PERMISSIONS["admin"]) - {perms.BILLING_TARIFF}
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(perms.ROLE_PERMISSIONS, "admin", no_tariff)

        # The counter works: one patient's day, recorded and billed.
        resp = client.post(
            f"/api/billing/patients/{patient}/charges",
            json={"service_code": code, "tariff_plan_id": plan}, headers=headers)
        assert resp.status_code == 200, resp.text
        resp = client.post(
            f"/api/billing/patients/{patient}/invoices",
            json={"issue": True}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["invoice"]["status"] == "issued"

        # The tariff stays READABLE — pricing a line requires seeing prices.
        assert client.get(
            "/api/billing/tariff-rates", headers=headers).status_code == 200

        # Every master mutation is refused, naming the missing pen.
        tariff_writes = [
            ("/api/billing/service-items",
             {"code": refused_code, "name": "Denied item"}),
            ("/api/billing/tariff-plans",
             {"code": f"TPD{uuid.uuid4().hex[:5].upper()}", "name": "Denied plan"}),
            ("/api/billing/tariff-rates",
             {"tariff_plan_id": plan, "service_code": code, "rate_paise": 1}),
            ("/api/billing/packages",
             {"tariff_plan_id": plan, "package_code": "PKG-DENIED",
              "name": "Denied package", "package_rate_paise": 1}),
        ]
        for path, payload in tariff_writes:
            resp = client.post(path, json=payload, headers=headers)
            assert resp.status_code == 403, (
                f"{path} mutated the tariff master on read+write alone")
            assert resp.json()["detail"]["missing"] == ["billing.tariff"], path

    # Nothing reached the master: the item was never created and the ₹500 rate
    # the refused POST tried to stamp to 1 paisa never moved.
    assert all(i["code"] != refused_code
               for i in billing_masters.list_service_items(limit=2000))
    rates = billing_service.list_tariff_rates(tariff_plan_id=plan, service_code=code)
    assert rates and all(int(r["rate_paise"]) == 50000 for r in rates)


def test_the_desk_runs_on_the_billing_family_alone(patient, plan):
    """The other half of the split: an account holding NOTHING but the three
    billing permissions can run the whole desk — price the list, capture the
    charge, raise the bill — so no route still leans on the governance grant
    it used to borrow."""
    headers = _auth()
    code = f"TST-SOLO{uuid.uuid4().hex[:6].upper()}"

    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(perms.ROLE_PERMISSIONS, "admin",
                   {perms.BILLING_READ, perms.BILLING_WRITE, perms.BILLING_TARIFF})

        assert client.post(
            "/api/billing/service-items",
            json={"code": code, "name": "Solo consult", "category": "consultation"},
            headers=headers).status_code == 200
        assert client.post(
            "/api/billing/tariff-rates",
            json={"tariff_plan_id": plan, "service_code": code, "rate_paise": 25000},
            headers=headers).status_code == 200
        assert client.post(
            f"/api/billing/patients/{patient}/charges",
            json={"service_code": code, "tariff_plan_id": plan},
            headers=headers).status_code == 200
        resp = client.post(
            f"/api/billing/patients/{patient}/invoices",
            json={"issue": True}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert client.get("/api/billing/invoices", headers=headers).status_code == 200


def test_module_flag_serves_404_on_every_tier(monkeypatch):
    """ORB_BILLING=0 removes the module on all three tiers alike: absent, not
    forbidden — including for an account that holds every permission."""
    monkeypatch.setattr(config, "BILLING_ENABLED", False, raising=False)
    headers = _auth()
    assert client.get("/api/billing/invoices", headers=headers).status_code == 404
    assert client.post(
        "/api/billing/invoices/1/issue", json={}, headers=headers
    ).status_code == 404
    assert client.post(
        "/api/billing/service-items", json={"code": "TST-OFF", "name": "x"},
        headers=headers,
    ).status_code == 404


# ── the drift guard: every route on its own tier, structurally ────────────────

def test_every_route_is_gated_on_its_own_tier():
    """Asserted on the ROUTER, not on sampled requests, so an endpoint added
    later that copies its Depends(...) from the wrong neighbour goes red here.

    The required permissions are read out of each route's dependency tree —
    the `permissions` closure cell of require_permission's checker — so this
    cannot pass by reading a comment, and it sees through require_patient_access
    (whose permission check is the same checker one level down).
    """
    from routes import billing_routes as br

    TARIFF_MASTER = {
        ("POST", "/api/billing/service-items"),
        ("POST", "/api/billing/tariff-plans"),
        ("POST", "/api/billing/tariff-rates"),
        ("POST", "/api/billing/packages"),
        # Price lists per facility (multisite-13): where a list applies and a
        # facility's default change every future bill there, the tariff pen.
        ("PUT", "/api/billing/facilities/{facility_id}/default-price-list"),
        ("PUT", "/api/billing/tariff-plans/{tariff_plan_id}/applies-to"),
    }
    ADVISORY = "/api/billing/patients/{patient_id}/discharge-advisory"

    def _route_permissions(route) -> set:
        found: set = set()

        def walk(dep):
            call = getattr(dep, "call", None)
            code = getattr(call, "__code__", None)
            closure = getattr(call, "__closure__", None) or ()
            if code is not None and closure:
                cells = dict(zip(code.co_freevars,
                                 (c.cell_contents for c in closure)))
                if "permissions" in cells:
                    found.update(cells["permissions"])
            for sub in dep.dependencies:
                walk(sub)

        walk(route.dependant)
        return found

    routes = [r for r in br.router.routes if isinstance(r, APIRoute)]
    assert len(routes) >= 20, "the router shrank — is this test still reading it?"

    seen_tariff = set()
    for route in routes:
        needed = _route_permissions(route)
        assert perms.ADMIN_OVERVIEW not in needed, (
            f"{route.path} still borrows admin.overview")
        for method in route.methods - {"HEAD", "OPTIONS"}:
            where = f"{method} {route.path}"
            if route.path == ADVISORY:
                # The clinician's warning, not the desk's — see the route.
                assert needed == {perms.DISCHARGE_READ}, where
            elif (method, route.path) in TARIFF_MASTER:
                assert needed == {perms.BILLING_READ, perms.BILLING_TARIFF}, (
                    f"{where} is not on the tariff tier: {needed}")
                seen_tariff.add((method, route.path))
            elif method == "GET":
                assert needed == {perms.BILLING_READ}, (
                    f"{where} over- or under-requires: {needed}")
            else:
                assert needed == {perms.BILLING_READ, perms.BILLING_WRITE}, (
                    f"{where} is not on the write tier — a read-only desk "
                    f"account could make this write: {needed}")
    assert seen_tariff == TARIFF_MASTER, (
        "the tariff-master set changed — reclassify deliberately, here")

    # And the module-level dependencies ask for exactly what they claim to,
    # read from their own closures (tests/test_payer.py idiom).
    def _required(dep) -> set:
        cells = dict(zip(dep.__code__.co_freevars,
                         (c.cell_contents for c in dep.__closure__)))
        return set(cells["permissions"])

    assert _required(br._DESK_READ) == {perms.BILLING_READ}
    assert _required(br._DESK_WRITE) == {perms.BILLING_READ, perms.BILLING_WRITE}
    assert _required(br._DESK_TARIFF) == {perms.BILLING_READ, perms.BILLING_TARIFF}
