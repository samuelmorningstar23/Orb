"""Price lists per facility (multisite-13, W13-B, 20 October 2026).

  1. One resolver. billing_masters.default_tariff_plan(facility_id) is the one
     default lookup: the price of an unnamed plan, a package, the payer
     resolution and /config all come through default_tariff_plan_on. With
     every price list group-wide (every install today) it answers exactly as
     the old inline query did.
  2. A patient at a second facility is priced on that facility's own default
     list when one exists, and on the group's default otherwise. A list kept
     for one facility never prices another facility's patient, whether it
     would come in as the default or through a payer.
  3. is_default is unique per facility: setting the second facility's
     default leaves facility 1's (and the group's) in place.
  4. The place of supply decides IGST against the billing facility's GST
     state code (state 29 with a place of supply in 27 is inter-state) while
     the multi-site switch is on or more than one facility is on record; with
     one facility and the switch off the caller's flag decides, as before.
  5. The price list editor (Admin, Facilities): its three routes answer 404
     feature_disabled with the switch off; with it on they show and change a
     facility's lists; every tariff master write is checked against the
     caller's facility scope (a list of another facility is not found, a
     group list needs group-wide reach), each refusal recorded.

The second facility (id 9137) is removed in teardown with the cache
invalidated (the TEST RULE in docs/his_design/build/contracts.md); every
price list a test makes is deleted and the group's defaults are put back.
Facility 1's row is restored after a test that edits it.
"""
import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from auth import create_token, hash_password
from database import db_session
from migrations_his import m0075_facility_columns_admin as M75
from routes.billing_routes import router as billing_router
from services import billing_masters as BM
from services import billing_service
from services import facility_service as FS
from services.money import rupees

F2 = 9137                       # an id no other module writes
TAG = uuid.uuid4().hex[:5].upper()

app = FastAPI()
app.include_router(billing_router)
client = TestClient(app)


# ── helpers ──────────────────────────────────────────────────────────────────

def _hex(n: int = 6) -> str:
    return uuid.uuid4().hex[:n].upper()


def _plan(name: str = "Annexe rates", **kwargs) -> dict:
    return billing_service.upsert_tariff_plan(code=f"TPFT{_hex()}", name=name, **kwargs)


def _item(*, taxable: bool = False) -> str:
    """A service item of this test's own. The TST- prefix keeps it out of
    tests/test_money.py's 'no seeded prices' count."""
    code = f"TST-FT{_hex()}"
    if taxable:
        BM.upsert_service_item(code=code, name="Crepe bandage", category="consumable",
                               gst_treatment="taxable", gst_rate_bp=1200)
    else:
        BM.upsert_service_item(code=code, name="Review visit", category="consultation",
                               gst_treatment="exempt", gst_rate_bp=0)
    return code


def _patient(facility: int = 1, *, admitted: bool = False) -> tuple:
    """A patient registered at `facility` (admitted there too when asked)."""
    pid = f"PFT{_hex(8)}"
    aid = None
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, ward, bed, status, "
                   "registered_facility_id) VALUES (?, ?, ?, 'FT-W1', '1', ?, ?)",
                   (pid, "Orsolya Quandt", f"MRN-{pid}",
                    "admitted" if admitted else "discharged", facility))
        if admitted:
            aid = f"AFT{_hex(8)}"
            db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, "
                       "bed, status, facility_id) VALUES (?, ?, 'D001', 'General Medicine', "
                       "'FT-W1', '1', 'active', ?)", (aid, pid, facility))
    return pid, aid


def _user(role: str = "admin", *, home: int | None = None) -> str:
    uid = f"FTU{_hex(8)}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, ?, ?)",
                   (uid, f"Thessaly Vonmarck {uid[-4:]}", role, hash_password("pw")))
        if home is not None:
            db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                       "granted_at) VALUES (?, ?, 'home', 'test', CURRENT_TIMESTAMP)",
                       (uid, home))
    FS.invalidate_cache()
    return uid


def _hdr(uid: str, role: str = "admin") -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _plan_row(plan_id: int) -> dict:
    with db_session() as db:
        return dict(db.execute("SELECT * FROM tariff_plans WHERE id = ?", (plan_id,)).fetchone())


def _max_audit_id() -> int:
    with db_session() as db:
        return int(db.execute("SELECT COALESCE(MAX(id), 0) FROM audit_logs").fetchone()[0])


def _audit_rows(action: str, since: int) -> list:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE action_type = ? AND id > ? ORDER BY id",
            (action, since)).fetchall()]


_LEGACY_DEFAULT_SQL = ("SELECT * FROM tariff_plans WHERE is_active = 1 "
                       "ORDER BY is_default DESC, id ASC LIMIT 1")


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def lists():
    """Every price list, payer and policy a test makes is removed afterwards,
    the group's defaults are put back, and no list is left kept for a
    facility."""
    with db_session() as db:
        group_defaults = [int(r[0]) for r in db.execute(
            "SELECT id FROM tariff_plans WHERE is_default = 1 AND facility_id IS NULL")]
        max_plan = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM tariff_plans").fetchone()[0])
        max_payer = int(db.execute("SELECT COALESCE(MAX(id), 0) FROM payers").fetchone()[0])
        scoped_before = int(db.execute(
            "SELECT COUNT(*) FROM tariff_plans WHERE facility_id IS NOT NULL").fetchone()[0])
    assert scoped_before == 0, "a price list kept for a facility leaked from another test"
    yield {"group_defaults": group_defaults}
    with db_session() as db:
        db.execute("DELETE FROM patient_payer_policies WHERE payer_id > ?", (max_payer,))
        db.execute("DELETE FROM payers WHERE id > ?", (max_payer,))
        db.execute("DELETE FROM tariff_rates WHERE tariff_plan_id > ? OR service_code LIKE 'TST-FT%'",
                   (max_plan,))
        db.execute("DELETE FROM package_rates WHERE tariff_plan_id > ?", (max_plan,))
        db.execute("DELETE FROM tariff_plans WHERE id > ?", (max_plan,))
        db.execute("UPDATE tariff_plans SET facility_id = NULL WHERE facility_id IS NOT NULL")
        marks = ",".join("?" for _ in group_defaults) or "NULL"
        db.execute(f"UPDATE tariff_plans SET is_default = CASE WHEN id IN ({marks}) THEN 1 "
                   "ELSE 0 END WHERE facility_id IS NULL", group_defaults)


@pytest.fixture()
def second(lists):
    """Facility 9137, GST state 29 (Karnataka), with one department. Removed
    in teardown; every row a test placed there goes back to facility 1."""
    code = f"FT{_hex(4)}"
    dept = f"Quellmoor Annexe {TAG}"
    with db_session() as db:
        db.execute("INSERT INTO facilities (id, uuid, code, name, state_code) "
                   "VALUES (?, ?, ?, ?, '29')", (F2, str(uuid.uuid4()), code, "Quellmoor Annexe"))
        db.execute("INSERT INTO departments (facility_id, name, created_by) VALUES (?, ?, 'test')",
                   (F2, dept))
    FS.invalidate_cache()
    users: list[str] = []
    yield {"id": F2, "code": code, "dept": dept, "users": users}
    with db_session() as db:
        db.execute("UPDATE admissions SET facility_id = 1 WHERE facility_id = ?", (F2,))
        db.execute("UPDATE patients SET registered_facility_id = 1 "
                   "WHERE registered_facility_id = ?", (F2,))
        for table, column, _declaration in M75.FACILITY_COLUMNS:
            db.execute(f"UPDATE {table} SET {column} = 1 WHERE {column} = ?", (F2,))
        db.execute("UPDATE tariff_plans SET facility_id = NULL, is_default = 0 "
                   "WHERE facility_id = ?", (F2,))
        db.execute("DELETE FROM departments WHERE name = ?", (dept,))
        db.execute("DELETE FROM user_facilities WHERE facility_id = ?", (F2,))
        for uid in users:
            db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (uid,))
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()


@pytest.fixture()
def facility_one_state():
    """Facility 1's GST state code set to 29 for one test, then put back."""
    with db_session() as db:
        before = dict(db.execute("SELECT state_code, gstin FROM facilities WHERE id = 1").fetchone())
        db.execute("UPDATE facilities SET state_code = '29' WHERE id = 1")
    FS.invalidate_cache()
    yield
    with db_session() as db:
        db.execute("UPDATE facilities SET state_code = ?, gstin = ? WHERE id = 1",
                   (before["state_code"], before["gstin"]))
    FS.invalidate_cache()


@pytest.fixture()
def multi_site_on(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True, raising=False)
    assert config.feature_enabled("multi_site") is True


def _keep_at(plan_id: int, facility_id: int | None) -> None:
    BM.set_tariff_plan_scope(plan_id, facility_id, facilities=None)


# ── 1. one resolver, unchanged with one facility ─────────────────────────────

def test_with_every_list_group_wide_the_default_is_the_old_answer(lists):
    """Single site: facility 1, the group and the old inline query agree,
    including when a later list is made the default and the old one is not."""
    with db_session() as db:
        legacy = dict(db.execute(_LEGACY_DEFAULT_SQL).fetchone())
    assert BM.default_tariff_plan()["id"] == legacy["id"]
    assert BM.default_tariff_plan(1)["id"] == legacy["id"]
    assert BM.default_tariff_plan(None)["id"] == legacy["id"]
    assert BM.default_plan_source(BM.default_tariff_plan()) == "group"

    newer = _plan(is_default=True)
    with db_session() as db:
        legacy = dict(db.execute(_LEGACY_DEFAULT_SQL).fetchone())
        defaults = db.execute("SELECT COUNT(*) FROM tariff_plans WHERE is_default = 1").fetchone()[0]
    assert legacy["id"] == newer["id"] == BM.default_tariff_plan()["id"]
    assert defaults == 1, "one group: setting a default still clears every other"


def test_the_four_default_lookups_go_through_one_resolver(lists, monkeypatch):
    """resolve_rate, resolve_package_rate, the payer resolution and
    default_tariff_plan all ask default_tariff_plan_on, with the facility."""
    seen: list = []
    real = BM.default_tariff_plan_on

    def spy(db, facility_id=1):
        seen.append(facility_id)
        return real(db, facility_id)

    monkeypatch.setattr(BM, "default_tariff_plan_on", spy)
    code = _item()
    pid, _ = _patient()
    BM.resolve_rate(code)
    BM.resolve_rate(code, facility_id=F2)
    BM.resolve_package_rate("PKG-FT-NONE")
    BM.resolve_patient_payer(pid)
    BM.default_tariff_plan()
    assert seen == [None, F2, None, 1, 1]


def test_a_named_plan_is_used_as_named(lists):
    code = _item()
    plan = _plan()
    BM.set_tariff_rate(tariff_plan_id=plan["id"], service_code=code, rate_paise=rupees("410"))
    res = BM.resolve_rate(code, tariff_plan_id=plan["id"], facility_id=F2)
    assert res.rate_paise == rupees("410") and res.tariff_plan_id == plan["id"]


def test_tariff_plans_list_says_where_each_applies(lists):
    plans = BM.list_tariff_plans()
    assert plans and all(p["applies_to"] == BM.GROUP_WIDE_LABEL for p in plans)


# ── 2. an F2 patient is priced on the F2 list, else the group's ──────────────

def test_an_f2_patient_is_priced_on_the_f2_plan_when_one_exists(second):
    code = _item()
    group = BM.default_tariff_plan(None)
    BM.set_tariff_rate(tariff_plan_id=group["id"], service_code=code, rate_paise=rupees("500"))
    f2_patient, _ = _patient(F2)
    f1_patient, _ = _patient(1)

    # No list of its own yet: the group's default prices the F2 patient.
    first = billing_service.add_charge(patient_id=f2_patient, service_code=code)
    assert first["unit_rate_paise"] == rupees("500")
    assert first["tariff_plan_id"] == group["id"]
    assert first["facility_id"] == F2

    annexe = _plan("Quellmoor rates")
    _keep_at(annexe["id"], F2)
    view = BM.set_facility_default_tariff_plan(F2, annexe["id"], facilities=None)
    assert view["changed"] is True and view["default_source"] == "facility"
    BM.set_tariff_rate(tariff_plan_id=annexe["id"], service_code=code, rate_paise=rupees("700"))

    again = billing_service.add_charge(patient_id=f2_patient, service_code=code)
    assert again["unit_rate_paise"] == rupees("700")
    assert again["tariff_plan_id"] == annexe["id"]
    # Facility 1's patient is still on the group's list, never the annexe's.
    home = billing_service.add_charge(patient_id=f1_patient, service_code=code)
    assert home["unit_rate_paise"] == rupees("500")
    assert home["tariff_plan_id"] == group["id"]

    assert BM.resolve_patient_payer(f2_patient).tariff_plan_id == annexe["id"]
    assert BM.resolve_patient_payer(f1_patient).tariff_plan_id == group["id"]
    # An admission at F2 decides it as well.
    admitted, aid = _patient(F2, admitted=True)
    assert BM.resolve_patient_payer(admitted, admission_id=aid).tariff_plan_id == annexe["id"]


def test_a_list_kept_for_one_facility_never_prices_another(second):
    """Not as a default, not through a payer, not through the fallback."""
    group = BM.default_tariff_plan(None)
    annexe = _plan("Quellmoor only")
    _keep_at(annexe["id"], F2)
    BM.set_facility_default_tariff_plan(F2, annexe["id"], facilities=None)
    assert BM.default_tariff_plan(F2)["id"] == annexe["id"]
    assert BM.default_tariff_plan(1)["id"] == group["id"]
    assert BM.default_tariff_plan(None)["id"] == group["id"]
    assert BM.resolve_rate(_item()).tariff_plan_id == group["id"]
    assert BM.resolve_rate(_item(), facility_id=F2).tariff_plan_id == annexe["id"]

    # A payer pointed at the annexe's list (written past upsert_payer, which
    # refuses it): its facility 1 patient is priced on facility 1's default,
    # its F2 patient on the annexe's list.
    with db_session() as db:
        cur = db.execute("INSERT INTO payers (code, name, payer_type, tariff_plan_id, is_active) "
                         "VALUES (?, 'Vardane Mutual', 'tpa', ?, 1)",
                         (f"TST-FTP{_hex()}", annexe["id"]))
        payer_id = int(cur.lastrowid)
    f1_patient, _ = _patient(1)
    f2_patient, _ = _patient(F2)
    with db_session() as db:
        for pid in (f1_patient, f2_patient):
            db.execute("INSERT INTO patient_payer_policies (patient_id, payer_id, status) "
                       "VALUES (?, ?, 'active')", (pid, payer_id))
    at_home = BM.resolve_patient_payer(f1_patient)
    assert at_home.payer_id == payer_id and at_home.source == "policy"
    assert at_home.tariff_plan_id == group["id"]
    assert BM.resolve_patient_payer(f2_patient).tariff_plan_id == annexe["id"]

    # With no default marked anywhere, facility 1 falls back to the first
    # active group list, never to the annexe's.
    with db_session() as db:
        db.execute("UPDATE tariff_plans SET is_default = 0 WHERE facility_id IS NULL")
    fallback = BM.default_tariff_plan(1)
    assert fallback is not None and fallback["facility_id"] is None
    assert BM.default_plan_source(fallback) == "first_active"
    assert BM.default_tariff_plan(F2)["id"] == annexe["id"]


def test_upsert_payer_refuses_a_list_kept_for_one_facility(second):
    annexe = _plan()
    _keep_at(annexe["id"], F2)
    with pytest.raises(BM.MasterDataError, match="whole group"):
        BM.upsert_payer(code=f"TST-FTP{_hex()}", name="Ostrava Health", payer_type="tpa",
                        tariff_plan_id=annexe["id"])
    group = _plan()
    payer = BM.upsert_payer(code=f"TST-FTP{_hex()}", name="Ostrava Health", payer_type="tpa",
                            tariff_plan_id=group["id"])
    assert payer["tariff_plan_id"] == group["id"]


# ── 3. is_default is unique per facility ─────────────────────────────────────

def test_setting_the_f2_default_leaves_the_f1_default_in_place(second, lists):
    group_before = BM.default_tariff_plan(None)["id"]
    main_list = _plan("Main campus rates")
    _keep_at(main_list["id"], 1)
    BM.set_facility_default_tariff_plan(1, main_list["id"], facilities=None)
    annexe = _plan("Quellmoor rates")
    _keep_at(annexe["id"], F2)
    BM.set_facility_default_tariff_plan(F2, annexe["id"], facilities=None)

    assert BM.default_tariff_plan(1)["id"] == main_list["id"]
    assert BM.default_tariff_plan(F2)["id"] == annexe["id"]
    assert BM.default_tariff_plan(None)["id"] == group_before
    assert _plan_row(main_list["id"])["is_default"] == 1

    # The writer of POST /tariff-plans clears defaults in the plan's own scope.
    billing_service.upsert_tariff_plan(code=annexe["code"], name="Quellmoor rates",
                                       is_default=True)
    assert _plan_row(main_list["id"])["is_default"] == 1
    assert BM.default_tariff_plan(None)["id"] == group_before
    second_annexe = _plan("Quellmoor night rates")
    _keep_at(second_annexe["id"], F2)
    billing_service.upsert_tariff_plan(code=second_annexe["code"], name="Quellmoor night rates",
                                       is_default=True)
    assert _plan_row(annexe["id"])["is_default"] == 0
    assert BM.default_tariff_plan(F2)["id"] == second_annexe["id"]
    assert BM.default_tariff_plan(1)["id"] == main_list["id"]

    # A new group default leaves both facilities' own defaults as they were.
    _plan("Group rates 2027", is_default=True)
    assert BM.default_tariff_plan(1)["id"] == main_list["id"]
    assert BM.default_tariff_plan(F2)["id"] == second_annexe["id"]


def test_the_default_editor_rules(second):
    group_default = BM.default_tariff_plan(None)
    spare = _plan("Spare group list")
    other = _plan("Main campus only")
    _keep_at(other["id"], 1)

    # The group's own default: the facility simply uses it.
    view = BM.set_facility_default_tariff_plan(F2, group_default["id"], facilities=None)
    assert view["default_source"] == "group" and view["moved"] is False
    assert view["changed"] is False

    # Another group list is kept for this facility from now on.
    since = _max_audit_id()
    view = BM.set_facility_default_tariff_plan(F2, spare["id"], facilities=None)
    assert view["moved"] is True and view["changed"] is True
    assert view["default_plan"]["id"] == spare["id"]
    assert view["default_plan"]["applies_to"] == "Quellmoor Annexe"
    assert _plan_row(spare["id"])["facility_id"] == F2
    moved = _audit_rows("tariff_plan_scope_changed", since)
    assert len(moved) == 1 and moved[0]["event_category"] == "site_admin"
    assert json.loads(moved[0]["metadata_json"])["to_facility_id"] == F2
    assert len(_audit_rows("facility_default_tariff_plan_set", since)) == 1

    # Choosing it again changes nothing and records nothing.
    since = _max_audit_id()
    assert BM.set_facility_default_tariff_plan(F2, spare["id"], facilities=None)["changed"] is False
    assert _audit_rows("facility_default_tariff_plan_set", since) == []

    # None: back on the group's default; the list stays kept for F2.
    view = BM.set_facility_default_tariff_plan(F2, None, facilities=None)
    assert view["default_plan"]["id"] == group_default["id"]
    assert _plan_row(spare["id"])["facility_id"] == F2

    with pytest.raises(BM.TariffScopeError) as other_facility:
        BM.set_facility_default_tariff_plan(F2, other["id"], facilities=None)
    assert other_facility.value.code == "plan_of_another_facility"

    retired = _plan("Retired list", is_active=False)
    _keep_at(retired["id"], F2)
    with pytest.raises(BM.TariffScopeError) as inactive:
        BM.set_facility_default_tariff_plan(F2, retired["id"], facilities=None)
    assert inactive.value.code == "plan_inactive"

    with db_session() as db:
        db.execute("UPDATE facilities SET is_active = 0 WHERE id = ?", (F2,))
    try:
        with pytest.raises(BM.TariffScopeError) as closed:
            BM.set_facility_default_tariff_plan(F2, None, facilities=None)
        assert closed.value.code == "facility_inactive"
    finally:
        with db_session() as db:
            db.execute("UPDATE facilities SET is_active = 1 WHERE id = ?", (F2,))


def test_scope_changes_that_would_strand_a_facility_or_a_payer_are_refused(second):
    group_default = BM.default_tariff_plan(None)
    with pytest.raises(BM.TariffScopeError) as stranded:
        BM.set_tariff_plan_scope(group_default["id"], F2, facilities=None)
    assert stranded.value.code == "group_default_plan"

    tpa_list = _plan("Vardane schedule")
    BM.upsert_payer(code=f"TST-FTP{_hex()}", name="Vardane Mutual", payer_type="tpa",
                    tariff_plan_id=tpa_list["id"])
    with pytest.raises(BM.TariffScopeError) as payer:
        BM.set_tariff_plan_scope(tpa_list["id"], F2, facilities=None)
    assert payer.value.code == "plan_used_by_payer"
    assert "Vardane Mutual" in str(payer.value)

    # Moving a facility's default back to the group clears the flag, so the
    # group keeps one default and the facility falls back to it.
    annexe = _plan()
    _keep_at(annexe["id"], F2)
    BM.set_facility_default_tariff_plan(F2, annexe["id"], facilities=None)
    out = BM.set_tariff_plan_scope(annexe["id"], None, facilities=None)
    assert out["changed"] is True and out["plan"]["applies_to"] == BM.GROUP_WIDE_LABEL
    assert out["plan"]["is_default"] is False
    assert BM.default_tariff_plan(F2)["id"] == group_default["id"]


def test_reach_a_facility_bound_caller_edits_only_its_own_lists(second):
    f2_list = _plan()
    _keep_at(f2_list["id"], F2)
    f1_list = _plan()
    _keep_at(f1_list["id"], 1)
    group_list = _plan()
    at_f2 = frozenset({F2})

    BM.assert_plan_editable(facilities=at_f2, tariff_plan_id=f2_list["id"])
    BM.assert_plan_editable(facilities=None, tariff_plan_id=group_list["id"])
    with pytest.raises(BM.TariffScopeError) as hidden:
        BM.assert_plan_editable(facilities=at_f2, tariff_plan_id=f1_list["id"])
    assert hidden.value.status == 404 and hidden.value.denial["document_facility_id"] == 1
    with pytest.raises(BM.TariffScopeError) as group:
        BM.assert_plan_editable(facilities=at_f2, tariff_plan_id=group_list["id"])
    assert group.value.status == 403 and group.value.code == "group_wide_access_required"
    with pytest.raises(BM.TariffScopeError) as new:
        BM.assert_plan_editable(facilities=at_f2, code="TPFT-NEW-ONE")
    assert new.value.code == "group_wide_access_required"

    # Moving a group list into a facility needs group-wide reach too.
    with pytest.raises(BM.TariffScopeError) as move:
        BM.set_facility_default_tariff_plan(F2, group_list["id"], facilities=at_f2)
    assert move.value.code == "group_wide_access_required"
    assert BM.set_facility_default_tariff_plan(F2, f2_list["id"], facilities=at_f2)["changed"]
    with pytest.raises(BM.TariffScopeError) as other:
        BM.facility_price_lists(1, facilities=at_f2)
    assert other.value.status == 404

    names = {p["id"] for p in BM.list_tariff_plans(facilities=at_f2)}
    assert f2_list["id"] in names and group_list["id"] in names
    assert f1_list["id"] not in names
    assert {p["id"] for p in BM.list_tariff_plans(facilities=frozenset())} >= {group_list["id"]}
    assert f2_list["id"] not in {p["id"] for p in BM.list_tariff_plans(facilities=frozenset())}


# ── 4. the place of supply decides IGST ──────────────────────────────────────

@pytest.mark.parametrize("typed, code", [
    ("27", "27"), (" 27 ", "27"), ("27-Maharashtra", "27"), ("27 Maharashtra", "27"),
    ("Maharashtra", "27"), ("karnataka", "29"), ("Andhra Pradesh", "37"),
    ("270", None), ("99", None), ("7", None), ("Atlantis", None), ("", None), (None, None),
])
def test_place_of_supply_state_codes(typed, code):
    assert BM.place_of_supply_state_code(typed) == code


def _taxable_bill(patient_id: str, **kwargs) -> dict:
    code = _item(taxable=True)
    plan = _plan()
    BM.set_tariff_rate(tariff_plan_id=plan["id"], service_code=code, rate_paise=rupees("100"))
    billing_service.add_charge(patient_id=patient_id, service_code=code,
                               tariff_plan_id=plan["id"])
    return billing_service.create_invoice(patient_id=patient_id, **kwargs)


def test_facility_state_29_with_a_place_of_supply_in_27_gives_igst(second):
    pid, _ = _patient(F2)
    since = _max_audit_id()
    bill = _taxable_bill(pid, place_of_supply="27", interstate=False)
    assert bill["is_interstate"] == 1
    assert bill["igst_paise"] == 1200 and bill["cgst_paise"] == 0 and bill["sgst_paise"] == 0
    assert bill["total_paise"] == 11200
    created = _audit_rows("billing_invoice_created", since)
    assert json.loads(created[-1]["metadata_json"])["interstate_source"] == "place_of_supply"

    # The same state: CGST and SGST, whatever the caller ticked.
    same = _taxable_bill(_patient(F2)[0], place_of_supply="29-Karnataka", interstate=True)
    assert same["is_interstate"] == 0
    assert same["cgst_paise"] == 600 and same["sgst_paise"] == 600 and same["igst_paise"] == 0

    # A place of supply that names no state: the caller's flag, as before.
    unknown = _taxable_bill(_patient(F2)[0], place_of_supply="Across the river", interstate=True)
    assert unknown["is_interstate"] == 1 and unknown["igst_paise"] == 1200


def test_a_facility_with_no_state_code_keeps_the_callers_flag(second):
    with db_session() as db:
        db.execute("UPDATE facilities SET state_code = NULL WHERE id = ?", (F2,))
    bill = _taxable_bill(_patient(F2)[0], place_of_supply="27", interstate=False)
    assert bill["is_interstate"] == 0 and bill["cgst_paise"] == 600


def test_one_facility_with_the_switch_off_keeps_the_callers_flag(lists, facility_one_state):
    """Single site, ORB_MULTI_SITE off: exactly as before, even with a state
    code on facility 1 and a place of supply in another state."""
    assert config.feature_enabled("multi_site") is False
    assert FS.multi_facility_data() is False
    bill = _taxable_bill(_patient(1)[0], place_of_supply="27", interstate=False)
    assert bill["is_interstate"] == 0
    assert bill["cgst_paise"] == 600 and bill["sgst_paise"] == 600 and bill["igst_paise"] == 0


def test_one_facility_with_the_switch_on_derives_it(lists, facility_one_state, multi_site_on):
    bill = _taxable_bill(_patient(1)[0], place_of_supply="27", interstate=False)
    assert bill["is_interstate"] == 1 and bill["igst_paise"] == 1200


# ── 5. the routes ────────────────────────────────────────────────────────────

def test_the_price_list_routes_are_off_with_the_switch_off(lists):
    uid = _user()
    assert config.feature_enabled("multi_site") is False
    for method, path, body in (
        ("GET", "/api/billing/facilities/1/price-lists", None),
        ("PUT", "/api/billing/facilities/1/default-price-list", {"tariff_plan_id": None}),
        ("PUT", f"/api/billing/tariff-plans/{BM.default_tariff_plan()['id']}/applies-to",
         {"facility_id": 1}),
    ):
        resp = client.request(method, path, json=body, headers=_hdr(uid))
        assert resp.status_code == 404, (path, resp.text)
        assert resp.json()["detail"]["error"] == "feature_disabled"
    # The existing desk is unchanged: /config, the list and a rate write.
    assert client.get("/api/billing/config", headers=_hdr(uid)).status_code == 200
    plans = client.get("/api/billing/tariff-plans", headers=_hdr(uid)).json()["plans"]
    assert all(p["applies_to"] == "Whole group" for p in plans)
    plan = _plan()
    resp = client.post("/api/billing/tariff-rates", headers=_hdr(uid), json={
        "tariff_plan_id": plan["id"], "service_code": _item(), "rate_paise": 12300})
    assert resp.status_code == 200, resp.text


def test_the_editor_on_one_facility(lists, multi_site_on):
    uid = _user()
    group_default = BM.default_tariff_plan(None)
    resp = client.get("/api/billing/facilities/1/price-lists", headers=_hdr(uid))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_plan"]["id"] == group_default["id"]
    assert body["default_source"] == "group" and body["can_edit"] is True
    assert body["group_wide"] is True
    assert any(p["applies_to"] == "Whole group" for p in body["plans"])

    spare = _plan("Main campus rates")
    resp = client.put("/api/billing/facilities/1/default-price-list",
                      json={"tariff_plan_id": spare["id"]}, headers=_hdr(uid))
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"] is True and resp.json()["default_source"] == "facility"
    assert BM.default_tariff_plan(1)["id"] == spare["id"]
    config_plan = client.get("/api/billing/config", headers=_hdr(uid)).json()
    assert config_plan["default_tariff_plan"]["id"] == spare["id"]

    resp = client.put(f"/api/billing/tariff-plans/{spare['id']}/applies-to",
                      json={"facility_id": None}, headers=_hdr(uid))
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"]["applies_to"] == "Whole group"
    assert BM.default_tariff_plan(1)["id"] == group_default["id"]

    refused = client.put(f"/api/billing/tariff-plans/{group_default['id']}/applies-to",
                         json={"facility_id": 1}, headers=_hdr(uid))
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "group_default_plan"
    missing = client.put("/api/billing/facilities/424242/default-price-list",
                         json={"tariff_plan_id": None}, headers=_hdr(uid))
    assert missing.status_code == 404
    blank = client.put("/api/billing/facilities/1/default-price-list", json={},
                       headers=_hdr(uid))
    assert blank.status_code == 422, "the list must be named, or null sent on purpose"


def test_the_price_list_reads_need_billing_read(lists, multi_site_on):
    nurse = _user("nurse")
    resp = client.get("/api/billing/facilities/1/price-lists", headers=_hdr(nurse, "nurse"))
    assert resp.status_code == 403


def test_a_facility_bound_desk_is_held_to_its_facility(second, multi_site_on):
    uid = _user(home=F2)
    second["users"].append(uid)
    hdr = _hdr(uid)
    f2_list = _plan("Quellmoor rates")
    _keep_at(f2_list["id"], F2)
    f1_list = _plan("Main campus only")
    _keep_at(f1_list["id"], 1)
    group_default = BM.default_tariff_plan(None)

    since = _max_audit_id()
    resp = client.get("/api/billing/facilities/1/price-lists", headers=hdr)
    assert resp.status_code == 404
    denied = _audit_rows("facility_document_denied", since)
    assert denied and denied[-1]["target_type"] == "facility"
    meta = json.loads(denied[-1]["metadata_json"])
    assert meta["facility_scope"] == [F2] and meta["lane"] == "billing"

    mine = client.get(f"/api/billing/facilities/{F2}/price-lists", headers=hdr)
    assert mine.status_code == 200, mine.text
    assert mine.json()["group_wide"] is False
    assert {p["id"] for p in mine.json()["plans"]} >= {f2_list["id"], group_default["id"]}

    ok = client.put(f"/api/billing/facilities/{F2}/default-price-list",
                    json={"tariff_plan_id": f2_list["id"]}, headers=hdr)
    assert ok.status_code == 200, ok.text
    assert BM.default_tariff_plan(F2)["id"] == f2_list["id"]

    since = _max_audit_id()
    code = _item()
    group_rate = client.post("/api/billing/tariff-rates", headers=hdr, json={
        "tariff_plan_id": group_default["id"], "service_code": code, "rate_paise": 100})
    assert group_rate.status_code == 403
    assert group_rate.json()["detail"]["error"] == "group_wide_access_required"
    assert _audit_rows("facility_document_denied", since)
    other_rate = client.post("/api/billing/tariff-rates", headers=hdr, json={
        "tariff_plan_id": f1_list["id"], "service_code": code, "rate_paise": 100})
    assert other_rate.status_code == 404
    own_rate = client.post("/api/billing/tariff-rates", headers=hdr, json={
        "tariff_plan_id": f2_list["id"], "service_code": code, "rate_paise": 100})
    assert own_rate.status_code == 200, own_rate.text
    new_group_list = client.post("/api/billing/tariff-plans", headers=hdr,
                                 json={"code": f"TPFT{_hex()}", "name": "Unasked"})
    assert new_group_list.status_code == 403
    package = client.post("/api/billing/packages", headers=hdr, json={
        "tariff_plan_id": f1_list["id"], "package_code": "PKG-FT", "name": "Hernia",
        "package_rate_paise": 100})
    assert package.status_code == 404

    listed = {p["id"] for p in client.get("/api/billing/tariff-plans", headers=hdr).json()["plans"]}
    assert f2_list["id"] in listed and f1_list["id"] not in listed
    cfg = client.get("/api/billing/config", headers=hdr).json()
    assert cfg["default_tariff_plan"]["id"] == f2_list["id"]
    assert cfg["default_tariff_plan"]["applies_to"] == "Quellmoor Annexe"
