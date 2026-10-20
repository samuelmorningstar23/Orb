"""Bed days priced by the class of the bed (adt-15, W10-A, ORB_ADT).

With beds and admissions on and no service code, post_bed_day_charges prices
each hospital-local day by the bed the patient lay in at the midnight census:
the ledger's placing moves give the bed, the bed its class, the class
(bed_classes) the charge code, and the tariff row for that class (ward_class =
the class code) the price. Pinned here:

  * ward, then ICU, then ward again, priced by class day by day (a transfer
    day at the class the patient moved to);
  * a bed with no class, a class with no code and a day in no bed are not
    charged and are named with the reason;
  * re-running posts nothing twice, whichever way a day was first priced;
  * a back-timed admission bills from its arrival day (stay_times);
  * leave days follow ORB_ADT_LEAVE_RENT_CHARGED (ruling ADT-P6);
  * the last day of a stay may not be passed;
  * with ORB_ADT off a service code is still required, and the explicit-code
    path is the one tests/test_billing_bed_days_and_encounter.py pins;
  * the route needs billing.write and the admission must be the path
    patient's.

Every ward, bed, class and stay made here is removed afterwards, so the board,
census and capacity tests that read the shared database see none of it.
"""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import permissions as perms
from auth import create_token
from database import db_session
from services import billing_masters, billing_service
from services.billing_service import BillingError
from services.money import rupees

TAG = uuid.uuid4().hex[:6].upper()


def _ts(day: str, hhmm: str) -> str:
    """A hospital-local (IST) wall-clock time as the UTC ISO string Orb stores."""
    from datetime import datetime, timedelta, timezone
    local = datetime.fromisoformat(f"{day}T{hhmm}:00")
    return (local - timedelta(hours=5, minutes=30)).replace(tzinfo=timezone.utc).isoformat()


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "ADT_LEAVE_RENT_CHARGED", True)


@pytest.fixture()
def master():
    """Two wards, four beds and three classes of this test's own, priced on a
    default plan of its own: general ward Rs 2,500 a day, ICU Rs 9,000."""
    t = uuid.uuid4().hex[:6].upper()
    gen_code, icu_code = f"TST-BDG{t}", f"TST-BDI{t}"
    for code, name in ((gen_code, "General bed day"), (icu_code, "ICU bed day")):
        billing_masters.upsert_service_item(code=code, name=name, category="bed")
    plan = int(billing_service.upsert_tariff_plan(code=f"TP{t}", name="Bed class plan")["id"])
    classes = {"gen": f"GEN{t}", "icu": f"ICU{t}", "bare": f"BARE{t}"}
    billing_masters.set_tariff_rate(tariff_plan_id=plan, service_code=gen_code,
                                    rate_paise=rupees("2500"), ward_class=classes["gen"],
                                    effective_from="2026-01-01")
    billing_masters.set_tariff_rate(tariff_plan_id=plan, service_code=icu_code,
                                    rate_paise=rupees("9000"), ward_class=classes["icu"],
                                    effective_from="2026-01-01")
    wards = {"gen": f"WRD-G{t}", "icu": f"WRD-I{t}"}
    beds = {"gen": f"{wards['gen']}/1", "icu": f"{wards['icu']}/1",
            "noclass": f"{wards['gen']}/2", "nocode": f"{wards['gen']}/3"}
    with db_session() as db:
        db.execute("UPDATE tariff_plans SET is_default = 0")
        db.execute("UPDATE tariff_plans SET is_default = 1 WHERE id = ?", (plan,))
        for key, code, service in (("gen", classes["gen"], gen_code),
                                   ("icu", classes["icu"], icu_code),
                                   ("bare", classes["bare"], None)):
            db.execute("INSERT INTO bed_classes (code, name, bed_day_service_code, created_at) "
                       "VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00')",
                       (code, {"gen": "General ward", "icu": "Intensive care",
                               "bare": "Recliner"}[key], service))
        for key, kind in (("gen", "general"), ("icu", "icu")):
            db.execute("INSERT INTO wards (id, code, name, department, ward_type, created_at) "
                       "VALUES (?, ?, ?, 'General Medicine', ?, '2026-01-01T00:00:00+00:00')",
                       (wards[key], wards[key][4:], wards[key][4:], kind))
        for key, ward, klass in (("gen", "gen", classes["gen"]), ("icu", "icu", classes["icu"]),
                                 ("noclass", "gen", None), ("nocode", "gen", classes["bare"])):
            bid = beds[key]
            db.execute("INSERT INTO housekeeping_units (id, unit_code, ward, bed_label, state, "
                       "ward_id, bed_class, created_at) VALUES (?, ?, ?, ?, 'occupied', ?, ?, "
                       "'2026-01-01T00:00:00+00:00')",
                       (bid, bid, wards[ward][4:], bid.rsplit("/", 1)[1], wards[ward], klass))
    made = {"plan": plan, "gen_code": gen_code, "icu_code": icu_code, "classes": classes,
            "wards": wards, "beds": beds, "admissions": []}
    yield made
    with db_session() as db:
        for aid in made["admissions"]:
            db.execute("DELETE FROM patient_movements WHERE admission_id = ?", (aid,))
            db.execute("DELETE FROM admissions WHERE id = ?", (aid,))
        for bid in beds.values():
            db.execute("DELETE FROM housekeeping_units WHERE id = ?", (bid,))
        for wid in wards.values():
            db.execute("DELETE FROM wards WHERE id = ?", (wid,))
        for code in classes.values():
            db.execute("DELETE FROM bed_classes WHERE code = ?", (code,))


def _stay(master, *, admitted: str, bed: str, created: str = None, status: str = "active",
          discharged_at: str = None) -> tuple:
    pid = f"PBC{uuid.uuid4().hex[:8].upper()}"
    aid = f"ADMBC{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('BCDOC', 'Dr Ysolde Marrick', 'doctor', 'x')")
        db.execute("INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, ?)",
                   (pid, "Tamsin Okonwe", f"MRN-{pid}",
                    "admitted" if status == "active" else "discharged"))
        db.execute("INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                   "status, created_at, admitted_at, bed_id, ward_id, discharged_at) "
                   "VALUES (?, ?, 'BCDOC', 'General Medicine', ?, ?, ?, ?, ?, ?, ?, ?)",
                   (aid, pid, master["wards"]["gen"][4:], "1", status, created or admitted,
                    admitted, master["beds"][bed], master["wards"]["gen"], discharged_at))
    master["admissions"].append(aid)
    return pid, aid


def _move(aid: str, pid: str, kind: str, when: str, *, to_bed=None, from_bed=None):
    with db_session() as db:
        db.execute("INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, "
                   "reason, moved_at, from_bed_id, to_bed_id, recorded_at) "
                   "VALUES (?, ?, ?, ?, 'test', ?, ?, ?, ?)",
                   (f"MOV-{uuid.uuid4().hex[:10]}", pid, aid, kind, when, from_bed, to_bed, when))


def _by_day(result) -> dict:
    return {c["service_date"]: (c["service_code"], c["ward_class"], c["total_paise"])
            for c in result["posted"]}


def test_ward_then_icu_then_ward_is_priced_by_class_day_by_day(master, adt_on):
    beds, cls = master["beds"], master["classes"]
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="gen")
    _move(aid, pid, "admission", _ts("2026-09-01", "09:00"), to_bed=beds["gen"])
    _move(aid, pid, "transfer", _ts("2026-09-02", "14:00"), from_bed=beds["gen"],
          to_bed=beds["icu"])
    _move(aid, pid, "transfer", _ts("2026-09-04", "10:00"), from_bed=beds["icu"],
          to_bed=beds["gen"])
    out = billing_service.post_bed_day_charges(admission_id=aid, through="2026-09-05")
    assert out["pricing"] == "bed_class" and out["service_code"] is None
    gen = (master["gen_code"], cls["gen"], rupees("2500"))
    icu = (master["icu_code"], cls["icu"], rupees("9000"))
    assert _by_day(out) == {"2026-09-01": gen, "2026-09-02": icu, "2026-09-03": icu,
                            "2026-09-04": gen, "2026-09-05": gen}
    assert out["unpriced_days"] == [] and out["leave_days"] == []
    descriptions = {c["service_date"]: c["description"] for c in out["posted"]}
    assert descriptions["2026-09-02"] == "Bed charge (Intensive care): 2026-09-02"


def test_a_corrected_transfer_never_happened(master, adt_on):
    beds = master["beds"]
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="gen")
    _move(aid, pid, "admission", _ts("2026-09-01", "09:00"), to_bed=beds["gen"])
    with db_session() as db:
        db.execute("INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, "
                   "reason, moved_at, from_bed_id, to_bed_id) VALUES (?, ?, ?, 'transfer', "
                   "'test', ?, ?, ?)", (f"MOV-X{TAG}{aid}", pid, aid,
                                        _ts("2026-09-01", "20:00"), beds["gen"], beds["icu"]))
        db.execute("INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, "
                   "reason, moved_at, from_bed_id, to_bed_id, corrects_movement_id) VALUES "
                   "(?, ?, ?, 'correction', 'Recorded in error', ?, ?, ?, ?)",
                   (f"MOV-C{TAG}{aid}", pid, aid, _ts("2026-09-02", "08:00"), beds["icu"],
                    beds["gen"], f"MOV-X{TAG}{aid}"))
    out = billing_service.post_bed_day_charges(admission_id=aid, through="2026-09-02")
    assert {d: v[1] for d, v in _by_day(out).items()} == {
        "2026-09-01": master["classes"]["gen"], "2026-09-02": master["classes"]["gen"]}


def test_days_that_cannot_be_priced_by_class_are_named_never_charged(master, adt_on):
    beds = master["beds"]
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="gen")
    _move(aid, pid, "admission", _ts("2026-09-01", "09:00"), to_bed=None)        # awaiting a bed
    _move(aid, pid, "bed_assigned", _ts("2026-09-02", "11:00"), to_bed=beds["noclass"])
    _move(aid, pid, "bed_change", _ts("2026-09-03", "11:00"), from_bed=beds["noclass"],
          to_bed=beds["nocode"])
    _move(aid, pid, "bed_change", _ts("2026-09-04", "11:00"), from_bed=beds["nocode"],
          to_bed=beds["gen"])
    out = billing_service.post_bed_day_charges(admission_id=aid, through="2026-09-04")
    assert out["unpriced_days"] == ["2026-09-01", "2026-09-02", "2026-09-03"]
    reasons = {d["day"]: d["reason"] for d in out["unpriced_detail"]}
    assert reasons == {"2026-09-01": "no_bed", "2026-09-02": "no_bed_class",
                       "2026-09-03": "no_service_code"}
    assert all(d["message"] for d in out["unpriced_detail"])
    assert list(_by_day(out)) == ["2026-09-04"]
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM encounter_charges WHERE admission_id = ? "
                       "AND total_paise = 0", (aid,)).fetchone()[0]
    assert n == 0


def test_a_code_not_on_the_price_list_or_unpriced_that_day_is_named(master, adt_on):
    beds, cls = master["beds"], master["classes"]
    with db_session() as db:
        db.execute("UPDATE bed_classes SET bed_day_service_code = 'TST-NOSUCH-BED' "
                   "WHERE code = ?", (cls["bare"],))
    pid, aid = _stay(master, admitted=_ts("2025-12-31", "09:00"), bed="gen")
    _move(aid, pid, "admission", _ts("2025-12-31", "09:00"), to_bed=beds["gen"])
    _move(aid, pid, "bed_change", _ts("2026-01-01", "12:00"), from_bed=beds["gen"],
          to_bed=beds["nocode"])
    out = billing_service.post_bed_day_charges(admission_id=aid, through="2026-01-01")
    reasons = {d["day"]: d["reason"] for d in out["unpriced_detail"]}
    # 31 Dec: the general rate starts on 1 January. 1 Jan: the code is unknown.
    assert reasons == {"2025-12-31": "rate_not_configured", "2026-01-01": "no_service_item"}
    assert out["posted_count"] == 0


def test_rerunning_posts_nothing_twice_whichever_way_a_day_was_priced(master, adt_on):
    beds = master["beds"]
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="gen")
    _move(aid, pid, "admission", _ts("2026-09-01", "09:00"), to_bed=beds["gen"])
    gen = master["classes"]["gen"]
    first = billing_service.post_bed_day_charges(admission_id=aid, service_code=master["gen_code"],
                                                 through="2026-09-01", ward_class=gen)
    assert first["pricing"] == "service_code" and first["posted_count"] == 1
    again = billing_service.post_bed_day_charges(admission_id=aid, through="2026-09-03")
    assert again["posted_count"] == 2 and again["already_present"] == 1
    third = billing_service.post_bed_day_charges(admission_id=aid, through="2026-09-03")
    assert third["posted_count"] == 0 and third["already_present"] == 3
    explicit = billing_service.post_bed_day_charges(
        admission_id=aid, service_code=master["gen_code"], through="2026-09-03", ward_class=gen)
    assert explicit["posted_count"] == 0 and explicit["already_present"] == 3
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM encounter_charges WHERE admission_id = ?",
                       (aid,)).fetchone()[0]
    assert n == 3


def test_a_back_timed_admission_bills_from_its_arrival_day(master, adt_on):
    beds = master["beds"]
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "22:00"), bed="gen",
                     created=_ts("2026-09-03", "08:00"))
    _move(aid, pid, "admission", _ts("2026-09-01", "22:00"), to_bed=beds["gen"])
    out = billing_service.post_bed_day_charges(admission_id=aid, through="2026-09-03")
    assert out["from"] == "2026-09-01"
    assert out["stay_start"] == _ts("2026-09-01", "22:00")
    assert sorted(_by_day(out)) == ["2026-09-01", "2026-09-02", "2026-09-03"]


@pytest.mark.parametrize("charged", [True, False])
def test_leave_days_follow_the_site_setting(master, adt_on, monkeypatch, charged):
    monkeypatch.setattr(config, "ADT_LEAVE_RENT_CHARGED", charged)
    beds = master["beds"]
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="gen")
    _move(aid, pid, "admission", _ts("2026-09-01", "09:00"), to_bed=beds["gen"])
    _move(aid, pid, "leave_start", _ts("2026-09-02", "09:00"))
    _move(aid, pid, "leave_end", _ts("2026-09-03", "18:00"))
    _move(aid, pid, "absconded", _ts("2026-09-04", "07:00"))
    out = billing_service.post_bed_day_charges(admission_id=aid, through="2026-09-04")
    assert out["leave_days"] == ["2026-09-02"]
    assert out["leave_rent_charged"] is charged
    expected = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"] if charged else [
        "2026-09-01", "2026-09-03", "2026-09-04"]
    assert sorted(_by_day(out)) == expected      # an absconded day is charged either way
    assert out["unpriced_days"] == []


def test_the_last_day_of_the_stay_cannot_be_passed(master, adt_on):
    beds = master["beds"]
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="gen", status="discharged",
                     discharged_at=_ts("2026-09-03", "11:00"))
    _move(aid, pid, "admission", _ts("2026-09-01", "09:00"), to_bed=beds["gen"])
    with pytest.raises(BillingError) as err:
        billing_service.post_bed_day_charges(admission_id=aid, through="2026-09-04")
    assert err.value.code == "through_after_stay_end"
    assert err.value.detail == {"last_day": "2026-09-03"}
    out = billing_service.post_bed_day_charges(admission_id=aid)
    assert out["through"] == "2026-09-03" and out["posted_count"] == 3

    live_pid, live = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="icu")
    with pytest.raises(BillingError) as future:
        billing_service.post_bed_day_charges(admission_id=live, through="2099-01-01")
    assert future.value.code == "through_after_stay_end"


def test_with_adt_off_a_service_code_is_still_required(master, monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="gen")
    with pytest.raises(BillingError) as err:
        billing_service.post_bed_day_charges(admission_id=aid, through="2026-09-02")
    assert err.value.code == "invalid_input"
    assert str(err.value) == "a bed-day service code is required"
    out = billing_service.post_bed_day_charges(admission_id=aid, service_code=master["gen_code"],
                                               through="2026-09-02",
                                               ward_class=master["classes"]["gen"])
    assert out["pricing"] == "service_code" and out["posted_count"] == 2
    assert out["leave_days"] == [] and out["leave_rent_charged"] is None


def test_a_ward_class_without_a_service_code_is_refused(master, adt_on):
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="gen")
    with pytest.raises(BillingError) as err:
        billing_service.post_bed_day_charges(admission_id=aid, ward_class="ICU")
    assert err.value.code == "invalid_input"


# ── The route ────────────────────────────────────────────────────────────────

app = FastAPI()
from routes.billing_routes import router as billing_router  # noqa: E402

app.include_router(billing_router)
client = TestClient(app)


def _auth(role: str) -> dict:
    uid = f"BCB-{role.upper()[:3]}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,'x')",
                   (uid, f"Pelagia {role}", role))
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def test_the_route_needs_billing_write_and_the_path_patients_admission(master, adt_on):
    beds = master["beds"]
    pid, aid = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="gen")
    _move(aid, pid, "admission", _ts("2026-09-01", "09:00"), to_bed=beds["gen"])
    other_pid, _ = _stay(master, admitted=_ts("2026-09-01", "09:00"), bed="icu")
    admin = _auth("admin")
    body = {"admission_id": aid, "through": "2026-09-02"}

    read_only = set(perms.ROLE_PERMISSIONS["admin"]) - {perms.BILLING_WRITE, perms.BILLING_TARIFF}
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(perms.ROLE_PERMISSIONS, "admin", read_only)
        refused = client.post(f"/api/billing/patients/{pid}/bed-days", json=body, headers=admin)
    assert refused.status_code == 403
    assert refused.json()["detail"]["missing"] == ["billing.write"]
    assert client.post(f"/api/billing/patients/{pid}/bed-days", json=body,
                       headers=_auth("doctor")).status_code == 403

    crossed = client.post(f"/api/billing/patients/{other_pid}/bed-days", json=body, headers=admin)
    assert crossed.status_code == 422
    assert crossed.json()["detail"]["error"] == "admission_patient_mismatch"

    ok = client.post(f"/api/billing/patients/{pid}/bed-days", json=body, headers=admin)
    assert ok.status_code == 200, ok.text
    assert ok.json()["pricing"] == "bed_class" and ok.json()["posted_count"] == 2
