"""The administrative lanes per facility (multisite-9b, W13-A, wave 13, 20 October 2026).

Two facilities in a database this module owns (isolated_module_database):
facility 1 with the department Brelstock Medicine and its Bed setup ward, and a
second facility with Tamsquill Medicine and its own ward. Each facility has one
admitted patient with a diet order, an ABHA record and a linked care context,
an erasure request and alerts on the shadow log; both are named on one breach.
Each facility has its own store and indent, machine, housekeeping unit and
employee. Facility 1 also has a locked meal plan and a kitchen indent.
Accounts: a department-less administrator at each facility (home rows), a
facility 2 ward nurse and a group-wide administrator by enterprise grant.

Pinned, with the fence opened (every group here is still a blind route group
until the wave-level fence step):

  * as the facility 2 administrator, no list, count or ward view of dietary,
    ABDM, procurement, biomedical, housekeeping, workforce, compliance or the
    admin alerts names the facility 1 patient, store, machine, unit or
    employee, and every one of them asked for by id answers 404;
  * writes that would land at facility 1 are refused: a meal plan on its ward,
    an indent on its store, a store on its department, a cleaning request on
    its unit, an unlink of its care context;
  * the strict defaults: the retention review without a patient, the shift
    and holiday lists, and whole-department workforce views without a
    department are refused for a facility-bound account;
  * the group-wide administrator still sees both facilities;
  * with one facility on record (facility_service's cached count 1) nothing
    changes: the facility 2 administrator reads the whole house again;
  * /api/abdm, /api/dietary, /api/biomedical, /api/procurement and
    /api/workforce reach no blind or unregistered SQL owner, so they are ready
    for the wave-level fence step.

Test patients carry invented names (never a word a clinician types). Run with
the test env, never the shipping .env.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import facility_fence as F
import main
from auth import create_token
from database import db_session
from services import abdm_service as ab
from services import audit_facility
from services import biomedical_service as bio
from services import consent_service as cs
from services import dietary_service as ds
from services import facility_service as FS
from services import housekeeping_service as hk
from services import procurement_service as proc
from services import shadow_service
from services import workforce_service as wf
from services.clinical_time import utc_iso_now

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9667
F2_CODE = f"AL{TAG[:4]}"
F1_DEPT = f"Brelstock Medicine {TAG}"
F2_DEPT = f"Tamsquill Medicine {TAG}"
W1 = f"BRW-{TAG}"
W2 = f"TQW-{TAG}"

P1 = f"ALP1{TAG}"
P2 = f"ALP2{TAG}"
NAME1 = "Corvella Thistlewaite"
NAME2 = "Ondrej Pallisandro"
E1 = f"ALE1{TAG}"
E2 = f"ALE2{TAG}"

client = TestClient(main.app, raise_server_exceptions=False)


def _hdr(uid: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _abha() -> str:
    return "7" + str(uuid.uuid4().int)[:13]


@pytest.fixture(scope="module")
def world(isolated_module_database):
    from services.helix_db import init_helix_db

    init_helix_db()
    now = utc_iso_now()
    users = {
        "f1_admin": ("admin", None),
        "f2_admin": ("admin", None),
        "f2_nurse": ("nurse", F2_DEPT),
        "group_admin": ("admin", None),
    }
    ids: dict = {}
    with db_session() as db:
        db.execute(
            "INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
            (F2, str(uuid.uuid4()), F2_CODE, "Tamsquill Satellite"),
        )
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
            (F1_DEPT,),
        )
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
            (F2_DEPT, F2),
        )
        for code, dept, fid in ((W1, F1_DEPT, 1), (W2, F2_DEPT, F2)):
            db.execute(
                "INSERT INTO wards (id, code, name, department, facility_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"WID{code}", code, f"Ward {code}", dept, fid, now),
            )
        for key, (role, user_dept) in users.items():
            uid = f"ALF{key.upper().replace('_', '')}{TAG}"[:30]
            ids[key] = uid
            db.execute(
                "INSERT INTO users (id, name, role, department, password_hash) "
                "VALUES (?, ?, ?, ?, 'x')",
                (uid, f"Quillan {key}", role, user_dept),
            )
        db.execute(
            "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, "
            "reason) VALUES (?, ?, 'home', 'system', ?, 'admin lanes')",
            (ids["f2_admin"], F2, now),
        )
        db.execute(
            "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
            "VALUES (?, 'system', ?, 'admin lanes')",
            (ids["group_admin"], now),
        )
        for pid, name, dept, ward, fid in (
            (P1, NAME1, F1_DEPT, W1, 1),
            (P2, NAME2, F2_DEPT, W2, F2),
        ):
            db.execute(
                "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status, "
                "admission_date, registered_facility_id) "
                "VALUES (?, ?, ?, 47, 'F', ?, 'C-3', 'Prandel fever', 'admitted', ?, ?)",
                (pid, name, f"MRN{pid}", ward, now[:10], fid),
            )
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                "status, facility_id, created_at) VALUES (?, ?, 'system', ?, ?, 'C-3', "
                "'active', ?, ?)",
                (f"A{pid}", pid, dept, ward, fid, now),
            )
    FS.invalidate_cache()
    audit_facility.invalidate_cache()
    F._audit_last.clear()
    assert FS.multi_facility_data() is True

    # Dietary: an order for each patient; facility 1's ward has a locked plan
    # and a kitchen indent.
    for pid in (P1, P2):
        ds.order_diet(patient_id=pid, diet_type="diabetic", ordered_by=ids["group_admin"])
    plan = ds.build_meal_plan(ward=W1, meal_service="lunch", generated_by=ids["group_admin"])
    ids["plan_f1"] = ds.lock_meal_plan(plan["id"], locked_by=ids["group_admin"])["id"]
    ids["indent_f1"] = ds.raise_kitchen_indent(
        meal_plan_id=ids["plan_f1"], raised_by=ids["f1_admin"]
    )["id"]

    # ABDM: an identity and a linked care context for each patient.
    for pid in (P1, P2):
        identity = ab.record_abha(patient_id=pid, abha_number=_abha(), recorded_by="system")
        ab.record_verification(
            identity_id=identity["id"], method="aadhaar_otp", outcome="verified",
            performed_by="system", performed_where="abdm_portal", reference="TXN-AL",
        )
        cc = ab.link_care_context(
            identity_id=identity["id"], care_context_reference=f"A{pid}",
            encounter_ref=f"A{pid}", hi_type="DischargeSummary", linked_by="system",
        )
        ids[f"cc_{pid}"] = cc["id"]

    # Procurement: one store and one indent per facility.
    item = proc.upsert_inventory_item(
        code=f"ALI{TAG}", name="Sterile gauze roll", category="consumable", uom="box",
        gst_treatment="taxable", gst_rate_bp=1200,
    )
    for key, dept, actor in (("f1", F1_DEPT, ids["f1_admin"]), ("f2", F2_DEPT, ids["f2_admin"])):
        store = proc.upsert_store(code=f"ALS{key.upper()}{TAG}", name=f"Store {key}",
                                  department=dept)
        ids[f"store_{key}"] = store["code"]
        indent = proc.create_indent(
            store_code=store["code"], department=dept,
            lines=[{"item_code": item["code"], "quantity_milli": 2000}], requested_by=actor,
        )
        ids[f"pindent_{key}"] = indent["id"]

    # Biomedical machines and housekeeping units.
    ids["asset_f1"] = bio.upsert_asset(asset_tag=f"ALA1{TAG}", name="Syringe pump",
                                       department=F1_DEPT, actor=ids["f1_admin"])["id"]
    ids["asset_f2"] = bio.upsert_asset(asset_tag=f"ALA2{TAG}", name="Syringe pump",
                                       department=F2_DEPT, actor=ids["f2_admin"])["id"]
    ids["unit_f1"] = hk.register_unit(unit_code=f"ALU1{TAG}", ward=W1, bed_label="1",
                                      unit_type="room", actor_user_id=ids["f1_admin"])["id"]
    ids["unit_f2"] = hk.register_unit(unit_code=f"ALU2{TAG}", ward=W2, bed_label="1",
                                      unit_type="room", actor_user_id=ids["f2_admin"])["id"]

    # Workforce: one employee per facility.
    wf.upsert_employee(employee_code=E1, full_name="Marisol Fenwright", department=F1_DEPT)
    wf.upsert_employee(employee_code=E2, full_name="Teodric Valshane", department=F2_DEPT)

    # Compliance: an erasure request each, one breach naming both.
    for pid in (P1, P2):
        ids[f"erasure_{pid}"] = cs.record_erasure_request(
            patient_id=pid, requested_by="system"
        )["id"]
    ids["breach"] = cs.record_breach(
        nature="loss", description="A printed list went missing.", reported_by="system",
        discovered_at=now, affected_patient_ids=[P1, P2],
    )["id"]

    # Alerts on the shadow log for each patient.
    stamp = datetime.now(UTC)
    for pid in (P1, P2):
        shadow_service.record_batch([{
            "patient_id": pid,
            "kind": "alert.fired",
            "occurred_at": (stamp - timedelta(seconds=5)).isoformat(timespec="milliseconds"),
            "trigger": "news2_threshold",
            "episode_payload": {"level": "critical", "raw_news2": 9},
            "payload": {"level": "critical", "raw_news2": 9, "score": 0.9, "repeat": False},
        }], "simulator")
    try:
        yield {"ids": ids, "h": {k: _hdr(ids[k], users[k][0]) for k in users}}
    finally:
        FS.invalidate_cache()
        audit_facility.invalidate_cache()
        F._audit_last.clear()


@pytest.fixture(autouse=True)
def _fresh_cache():
    FS.invalidate_cache()
    audit_facility.invalidate_cache()
    yield
    FS.invalidate_cache()
    audit_facility.invalidate_cache()


@pytest.fixture()
def fence_open(monkeypatch):
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


@pytest.fixture()
def one_facility():
    FS._store(1)
    assert FS.cached_multi_facility_data() is False
    yield
    FS.invalidate_cache()


def _get(world, who: str, path: str):
    return client.get(path, headers=world["h"][who])


def _post(world, who: str, path: str, body: dict):
    return client.post(path, json=body, headers=world["h"][who])


# ── dietary ──────────────────────────────────────────────────────────────────


def test_the_diet_desk_keeps_to_the_facility(world, fence_open):
    ids = world["ids"]
    for path in ("/api/dietary/ward", "/api/dietary/summary", "/api/dietary/wards",
                 "/api/dietary/stale", "/api/dietary/kitchen", "/api/dietary/meal-plans",
                 "/api/dietary/indents"):
        resp = _get(world, "f2_admin", path)
        assert resp.status_code == 200, (path, resp.text)
        for word in (NAME1, P1, W1):
            assert word not in resp.text, (path, word)
    ward = _get(world, "f2_admin", "/api/dietary/ward").json()
    assert {r["patient_id"] for r in ward["rows"]} == {P2}
    assert _get(world, "f2_admin", f"/api/dietary/meal-plans/{ids['plan_f1']}").status_code == 404
    assert _get(world, "f2_admin", f"/api/dietary/indents/{ids['indent_f1']}").status_code == 404
    built = _post(world, "f2_admin", "/api/dietary/meal-plans",
                  {"ward": W1, "meal_service": "dinner"})
    assert built.status_code == 404
    lock = _post(world, "f2_admin", f"/api/dietary/meal-plans/{ids['plan_f1']}/lock", {})
    assert lock.status_code == 404
    issue = _post(world, "f2_admin", f"/api/dietary/indents/{ids['indent_f1']}/issue", {})
    assert issue.status_code == 404
    group = _get(world, "group_admin", "/api/dietary/ward").json()
    assert {P1, P2} <= {r["patient_id"] for r in group["rows"]}


# ── ABDM ─────────────────────────────────────────────────────────────────────


def test_the_abdm_desk_keeps_to_the_facility(world, fence_open):
    ids = world["ids"]
    for path in ("/api/abdm/identities", "/api/abdm/consents", "/api/abdm/care-contexts",
                 "/api/abdm/queue", "/api/abdm/summary"):
        resp = _get(world, "f2_admin", path)
        assert resp.status_code == 200, (path, resp.text)
        for word in (NAME1, P1, f"A{P1}"):
            assert word not in resp.text, (path, word)
    contexts = _get(world, "f2_admin", "/api/abdm/care-contexts").json()["care_contexts"]
    assert {c["patient_id"] for c in contexts} == {P2}
    summary = _get(world, "f2_admin", "/api/abdm/summary").json()
    assert summary["identities"]["total"] == 1
    unlink = _post(world, "f2_admin", f"/api/abdm/care-contexts/{ids['cc_' + P1]}/unlink",
                   {"reason": "Linked to the wrong stay"})
    assert unlink.status_code == 404
    group = _get(world, "group_admin", "/api/abdm/care-contexts").json()["care_contexts"]
    assert {P1, P2} <= {c["patient_id"] for c in group}


def test_a_duplicate_abha_refusal_names_no_patient_of_another_facility(world, fence_open):
    with db_session() as db:
        number = db.execute(
            "SELECT abha_number FROM abha_identities WHERE patient_id = ?", (P1,)
        ).fetchone()[0]
    resp = _post(world, "f2_admin", "/api/abdm/identities",
                 {"patient_id": P2, "abha_number": number})
    assert resp.status_code == 400
    assert NAME1 not in resp.text
    # The service called with no facility scope names nobody while two exist.
    with db_session() as db:
        assert ab._clash_patient_line(db, P1, None) is None
        assert ab._clash_patient_line(db, P1, None, None) is not None


# ── procurement ──────────────────────────────────────────────────────────────


def test_the_stores_desk_keeps_to_the_facility(world, fence_open):
    ids = world["ids"]
    stores = _get(world, "f2_admin", "/api/procurement/stores").json()["stores"]
    assert {s["code"] for s in stores} == {ids["store_f2"]}
    indents = _get(world, "f2_admin", "/api/procurement/indents").json()["indents"]
    assert {i["id"] for i in indents} == {ids["pindent_f2"]}
    assert _get(world, "f2_admin",
                f"/api/procurement/indents/{ids['pindent_f1']}").status_code == 404
    assert _get(world, "f2_admin",
                f"/api/procurement/stock?store_code={ids['store_f1']}").status_code == 404
    overview = _get(world, "f2_admin", "/api/procurement/overview")
    assert overview.status_code == 200 and ids["store_f1"] not in overview.text
    raised = _post(world, "f2_admin", "/api/procurement/indents",
                   {"store_code": ids["store_f1"],
                    "lines": [{"item_code": f"ALI{TAG}", "quantity_milli": 1000}]})
    assert raised.status_code == 404
    moved = _post(world, "f2_admin", "/api/procurement/stores",
                  {"code": ids["store_f1"], "name": "Renamed", "department": F1_DEPT})
    assert moved.status_code == 404
    elsewhere = _post(world, "f2_admin", "/api/procurement/stores",
                      {"code": f"ALSX{TAG}", "name": "New store", "department": F1_DEPT})
    assert elsewhere.status_code == 403
    assert elsewhere.json()["detail"]["error"] == "facility_scope"
    submit = _post(world, "f2_admin", f"/api/procurement/indents/{ids['pindent_f1']}/submit", {})
    assert submit.status_code == 404
    group = _get(world, "group_admin", "/api/procurement/stores").json()["stores"]
    assert {ids["store_f1"], ids["store_f2"]} <= {s["code"] for s in group}


# ── biomedical ───────────────────────────────────────────────────────────────


def test_the_equipment_register_keeps_to_the_facility(world, fence_open):
    ids = world["ids"]
    assets = _get(world, "f2_admin", "/api/biomedical/assets").json()["assets"]
    assert {a["id"] for a in assets} == {ids["asset_f2"]}
    assert _get(world, "f2_admin",
                f"/api/biomedical/assets/{ids['asset_f1']}").status_code == 404
    overview = _get(world, "f2_admin", "/api/biomedical/overview").json()
    assert overview["assets_total"] == 1
    gaps = _get(world, "f2_admin", "/api/biomedical/coverage-gaps").json()
    assert {a["id"] for a in gaps["assets"]} == {ids["asset_f2"]}
    status = _post(world, "f2_admin", f"/api/biomedical/assets/{ids['asset_f1']}/status",
                   {"status": "down", "reason": "Will not power on"})
    assert status.status_code == 404
    call = _post(world, "f2_admin", "/api/biomedical/service-calls",
                 {"asset_id": ids["asset_f1"], "fault_description": "Alarm sounding"})
    assert call.status_code == 404
    group = _get(world, "group_admin", "/api/biomedical/assets").json()["assets"]
    assert {ids["asset_f1"], ids["asset_f2"]} <= {a["id"] for a in group}


def _contract(number: str, asset_ids: list[int], **extra) -> dict:
    return bio.upsert_contract(
        contract_number=number, contract_type="amc", start_date="2026-01-01",
        end_date="2027-12-31", vendor_name="Halvorine Biomed", asset_ids=asset_ids, **extra,
    )


def _contract_body(number: str, **extra) -> dict:
    return {"contract_number": number, "contract_type": "amc", "start_date": "2026-01-01",
            "end_date": "2027-12-31", "vendor_name": "Halvorine Biomed", **extra}


def _covered(number: str) -> tuple[set[int], str]:
    with db_session() as db:
        row = db.execute(
            "SELECT id, status FROM biomedical_contracts WHERE contract_number = ?", (number,)
        ).fetchone()
        assets = db.execute(
            "SELECT asset_id FROM biomedical_contract_assets WHERE contract_id = ?", (row[0],)
        ).fetchall()
    return {int(a[0]) for a in assets}, row[1]


def test_a_contract_shared_with_another_facility_is_not_rewritten_by_one(world, fence_open):
    """A facility's admin cannot narrow, clear or end a contract that also covers
    another facility's machine: its coverage list and fields are shared."""
    ids = world["ids"]
    both = {ids["asset_f1"], ids["asset_f2"]}
    shared = f"ALC-SH-{TAG}"
    _contract(shared, sorted(both))
    for change in (
        {"asset_ids": [ids["asset_f2"]], "status": "terminated"},
        {"asset_ids": []},
        {"asset_tags": [f"ALA2{TAG}"]},
        {"status": "terminated"},
        {"value_paise": 100},
    ):
        resp = _post(world, "f2_admin", "/api/biomedical/contracts",
                     _contract_body(shared, **change))
        assert resp.status_code == 403, (change, resp.text)
        assert resp.json()["detail"]["error"] == "facility_scope"
        assert _covered(shared) == (both, "active"), change

    # The facility's reader still sees it, with only their own machine listed
    # and counted.
    listed = _get(world, "f2_admin", "/api/biomedical/contracts").json()["contracts"]
    mine = [c for c in listed if c["contract_number"] == shared]
    assert len(mine) == 1 and mine[0]["asset_count"] == 1
    one = _get(world, "f2_admin", f"/api/biomedical/contracts/{mine[0]['id']}").json()
    assert {a["id"] for a in one["assets"]} == {ids["asset_f2"]}
    assert one["asset_count"] == 1

    # A group account may change it, and leaving the lists out keeps coverage.
    group = _post(world, "group_admin", "/api/biomedical/contracts",
                  _contract_body(shared, value_paise=5000000))
    assert group.status_code == 200, group.text
    assert _covered(shared) == (both, "active")


def test_a_facility_amends_and_makes_only_its_own_contracts(world, fence_open):
    ids = world["ids"]
    own = f"ALC-F2-{TAG}"
    _contract(own, [ids["asset_f2"]])
    amended = _post(world, "f2_admin", "/api/biomedical/contracts",
                    _contract_body(own, status="renewed"))
    assert amended.status_code == 200, amended.text
    assert _covered(own) == ({ids["asset_f2"]}, "renewed")
    # Clearing its coverage would place it at no facility.
    cleared = _post(world, "f2_admin", "/api/biomedical/contracts",
                    _contract_body(own, asset_ids=[]))
    assert cleared.status_code == 422
    assert cleared.json()["detail"]["error"] == "assets_required"
    assert _covered(own) == ({ids["asset_f2"]}, "renewed")
    # Another facility's machine cannot be added to it.
    added = _post(world, "f2_admin", "/api/biomedical/contracts",
                  _contract_body(own, asset_ids=[ids["asset_f2"], ids["asset_f1"]]))
    assert added.status_code == 404
    assert _covered(own) == ({ids["asset_f2"]}, "renewed")

    # Facility 1's own contract is not there for facility 2 at all.
    theirs = f"ALC-F1-{TAG}"
    _contract(theirs, [ids["asset_f1"]])
    hidden = _post(world, "f2_admin", "/api/biomedical/contracts",
                   _contract_body(theirs, asset_ids=[ids["asset_f2"]]))
    assert hidden.status_code == 404
    assert _covered(theirs) == ({ids["asset_f1"]}, "active")

    # A new contract names at least one of the facility's own machines.
    fresh = f"ALC-NEW-{TAG}"
    bare = _post(world, "f2_admin", "/api/biomedical/contracts", _contract_body(fresh))
    assert bare.status_code == 422
    assert bare.json()["detail"]["error"] == "assets_required"
    made = _post(world, "f2_admin", "/api/biomedical/contracts",
                 _contract_body(fresh, asset_ids=[ids["asset_f2"]]))
    assert made.status_code == 200, made.text
    assert _covered(fresh) == ({ids["asset_f2"]}, "active")


# ── housekeeping ─────────────────────────────────────────────────────────────


def test_the_housekeeping_desk_keeps_to_the_facility(world, fence_open):
    ids = world["ids"]
    units = _get(world, "f2_admin", "/api/housekeeping/units").json()["units"]
    assert {u["id"] for u in units} == {ids["unit_f2"]}
    assert _get(world, "f2_admin", f"/api/housekeeping/units/{ids['unit_f1']}").status_code == 404
    task = _post(world, "f2_admin", "/api/housekeeping/tasks", {"unit_id": ids["unit_f1"]})
    assert task.status_code == 404
    readiness = _get(world, "f2_admin", "/api/housekeeping/readiness").json()
    assert W1 not in {w["ward"] for w in readiness["wards"]}
    overview = _get(world, "f2_admin", "/api/housekeeping/overview?sync=false")
    assert overview.status_code == 200 and W1 not in overview.text
    linen = _post(world, "f2_admin", "/api/housekeeping/linen",
                  {"ward": W1, "item_code": "BEDSHEET", "direction": "issue", "quantity": 2})
    assert linen.status_code == 403
    group = _get(world, "group_admin", "/api/housekeeping/units").json()["units"]
    assert {ids["unit_f1"], ids["unit_f2"]} <= {u["id"] for u in group}


# ── workforce ────────────────────────────────────────────────────────────────


def test_the_workforce_desk_keeps_to_the_facility(world, fence_open):
    employees = _get(world, "f2_admin", "/api/workforce/employees").json()["employees"]
    assert {e["employee_code"] for e in employees} == {E2}
    assert _get(world, "f2_admin", f"/api/workforce/employees/{E1}").status_code == 404
    today = datetime.now(UTC).date().isoformat()
    whole = _get(world, "f2_admin", f"/api/workforce/roster?date_from={today}&date_to={today}")
    assert whole.status_code == 422
    assert whole.json()["detail"]["error"] == "department_required"
    other = _get(world, "f2_admin",
                 f"/api/workforce/roster?date_from={today}&date_to={today}&department={F1_DEPT}")
    assert other.status_code == 404
    own = _get(world, "f2_admin",
               f"/api/workforce/roster?date_from={today}&date_to={today}&department={F2_DEPT}")
    assert own.status_code == 200
    holiday = _post(world, "f2_admin", "/api/workforce/holidays",
                    {"holiday_date": "2099-01-26", "name": "A shared holiday"})
    assert holiday.status_code == 403
    assert holiday.json()["detail"]["error"] == "group_setting"
    attendance = _post(world, "f2_admin", "/api/workforce/attendance",
                       {"employee_code": E1, "attendance_date": today, "status": "absent"})
    assert attendance.status_code == 404
    group = _get(world, "group_admin", "/api/workforce/employees").json()["employees"]
    assert {E1, E2} <= {e["employee_code"] for e in group}


# ── compliance and the admin alerts ──────────────────────────────────────────


def test_compliance_keeps_to_the_facilitys_patients(world, fence_open):
    ids = world["ids"]
    rows = _get(world, "f2_admin", "/api/admin/compliance/erasure").json()["requests"]
    assert {r["patient_id"] for r in rows} == {P2}
    review = _get(world, "f2_admin", "/api/admin/compliance/retention")
    assert review.status_code == 403
    assert review.json()["detail"]["error"] == "group_wide_only"
    assert _get(world, "f2_admin",
                f"/api/admin/compliance/retention?patient_id={P2}").status_code == 200
    breach = _get(world, "f2_admin", f"/api/admin/compliance/breaches/{ids['breach']}").json()
    assert breach["affected_patient_ids"] == [P2]
    assert breach["affected_patient_ids_withheld"] == 1
    assert P1 not in str(breach)
    group = _get(world, "group_admin", f"/api/admin/compliance/breaches/{ids['breach']}").json()
    assert set(group["affected_patient_ids"]) == {P1, P2}
    assert _get(world, "group_admin", "/api/admin/compliance/retention").status_code == 200


def test_the_admin_alerts_and_overview_keep_to_the_facility(world, fence_open):
    alerts = _get(world, "f2_admin", "/api/admin/alerts").json()
    assert {a["patient_id"] for a in alerts["alerts"]} == {P2}
    assert P1 not in str(alerts)
    group = _get(world, "group_admin", "/api/admin/alerts").json()
    assert {P1, P2} <= {a["patient_id"] for a in group["alerts"]}
    overview = _get(world, "f2_admin", "/api/admin/overview").json()
    assert overview["recent_failures"] == []
    assert overview["alerts"]["total"] == 1


def test_the_real_fence_still_holds_these_groups_shut(world):
    for path in ("/api/dietary/ward", "/api/abdm/queue", "/api/procurement/stores",
                 "/api/biomedical/assets", "/api/housekeeping/units",
                 "/api/workforce/employees", "/api/admin/alerts"):
        assert _get(world, "f2_admin", path).status_code == 503, path


# ── one facility on record ───────────────────────────────────────────────────


def test_with_one_facility_every_lane_is_as_before(world, one_facility):
    ids = world["ids"]
    stores = {s["code"] for s in _get(world, "f2_admin", "/api/procurement/stores").json()["stores"]}
    assert {ids["store_f1"], ids["store_f2"]} <= stores
    ward = _get(world, "f2_admin", "/api/dietary/ward").json()
    assert {P1, P2} <= {r["patient_id"] for r in ward["rows"]}
    employees = _get(world, "f2_admin", "/api/workforce/employees").json()["employees"]
    assert {E1, E2} <= {e["employee_code"] for e in employees}
    assert _get(world, "f2_admin", "/api/admin/compliance/retention").status_code == 200
    alerts = _get(world, "f2_admin", "/api/admin/alerts").json()
    assert {P1, P2} <= {a["patient_id"] for a in alerts["alerts"]}
    # Contracts: any admin amends any contract, and one may cover no machine.
    shared = f"ALC-ONE-{TAG}"
    _contract(shared, [ids["asset_f1"], ids["asset_f2"]])
    narrowed = _post(world, "f2_admin", "/api/biomedical/contracts",
                     _contract_body(shared, asset_ids=[ids["asset_f2"]], status="terminated"))
    assert narrowed.status_code == 200, narrowed.text
    assert _covered(shared) == ({ids["asset_f2"]}, "terminated")
    listed = _get(world, "f2_admin", "/api/biomedical/contracts").json()["contracts"]
    assert any(c["contract_number"] == shared and c["asset_count"] == 1 for c in listed)
    bare = _post(world, "f2_admin", "/api/biomedical/contracts",
                 _contract_body(f"ALC-ONE-BARE-{TAG}"))
    assert bare.status_code == 200, bare.text


# ── readiness for the wave-level fence step ──────────────────────────────────


def test_the_swept_groups_reach_no_blind_sql_owner():
    from tests import facility_sql_sites as R

    graph = R.graph()
    found = set(R.scan())
    rows = R.registry()
    ready = ("/api/abdm", "/api/dietary", "/api/biomedical", "/api/procurement",
             "/api/workforce")
    problems = []
    for route in R.app_routes(main.app, graph):
        if route.node is None or not any(R.under(route.path, p) for p in ready):
            continue
        for key in sorted(graph.reach(route.node) & found):
            _, row = R.row_for(rows, *key)
            if row is None or row.kind == R.BLIND:
                problems.append(f"{route.path} reaches {key}")
    assert problems == []
