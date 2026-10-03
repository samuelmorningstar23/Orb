"""Orb — diet orders, meal plans and the kitchen indent
(services/dietary_service.py + routes/dietary_routes.py).

WHAT THESE TESTS GUARD, in the order somebody gets hurt:

  1. A DISCHARGED PATIENT LEAVES THE KITCHEN COUNT. This is the one with a
     PROVEN-CAN-FAIL demonstration below (test_discharge_rule_is_load_bearing):
     the discharge filter is removed from the service at runtime and the
     discharged patient reappears in the count, which is what would happen in a
     hospital if somebody "simplified" that WHERE clause. A diabetic tray cooked
     for a bed that now holds a different patient is the harm; the count
     silently including a discharged patient is the defect.

  2. THE COUNT ADDS UP. `total_meals == sum(line.meal_count)`, exactly, and
     `total_meals + npo_count == order_count`. A kitchen count that cannot be
     reconciled to the orders behind it is a count somebody over-cooks by 10%
     forever to be safe.

  3. NPO IS DISTINCT, BOUNDED AND ATTRIBUTED. It renders as its own list with
     its own label, it carries the ordering clinician by name, it cannot be
     written without an end time (a DATABASE CHECK, not just a validation), and
     when the window passes the diet does NOT resume by itself.

  4. ALLERGIES COME FROM `patient_allergies`, LIVE. There is no allergy column
     on `diet_orders` — asserted against the real schema — and an allergy
     recorded AFTER the diet order was written appears on the order immediately.
     An allergy read that fails renders as "could not be read", never as "no
     known allergies".

  5. NOTHING HERE BLOCKS A CLINICAL ACTION, and the ward boundary holds: a diet
     order names a patient, a bed and their allergies, so the query-string read
     is department-scoped like every other PHI surface.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth import create_token, hash_password  # noqa: E402
from database import db_session, get_db  # noqa: E402
from main import app  # noqa: E402
from services import dietary_service as ds  # noqa: E402
from services.hospital_time import hospital_now  # noqa: E402

client = TestClient(app)

DEPT = "Dietary Test Medicine"
OTHER_DEPT = "Dietary Test Surgery"


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role: str, department: str = DEPT) -> str:
    uid = f"DIET{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (uid, f"Diet {role.title()}", role, department, hash_password("password")),
        )
    return uid


def _auth(user_id: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _admit(
    *, ward: str = "DIET-W1", bed: str = "1", department: str = DEPT,
    name: str = "Diet Test Patient",
) -> str:
    """A patient with an ACTIVE admission — the only state a diet order accepts."""
    pid = f"DP{uuid.uuid4().hex[:8].upper()}"
    aid = f"DA{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, status) "
            "VALUES (?, ?, ?, ?, ?, 'admitted')",
            (pid, name, f"MRN-{pid}", ward, bed),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status) "
            "VALUES (?, ?, 'D001', ?, ?, ?, 'active')",
            (aid, pid, department, ward, bed),
        )
    return pid


def _discharge(patient_id: str) -> None:
    """Exactly what services/discharge_service.py does to these two tables — no
    dietary hook, no sweep, nothing this module is told about."""
    with db_session() as db:
        db.execute(
            "UPDATE patients SET status = 'discharged' WHERE id = ?", (patient_id,)
        )
        db.execute(
            "UPDATE admissions SET status = 'discharged' WHERE patient_id = ? "
            "AND status = 'active'",
            (patient_id,),
        )


def _record_allergy(patient_id: str, substance: str, *, severity: str = "severe",
                    reaction: str = "anaphylaxis") -> None:
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO patient_allergies "
            "(patient_id, substance, reaction, severity, status, source, recorded_by) "
            "VALUES (?, ?, ?, ?, 'active', 'clinician', 'D001')",
            (patient_id, substance, reaction, severity),
        )


def _in_hours(hours: float) -> str:
    return (hospital_now() + timedelta(hours=hours)).isoformat()


@pytest.fixture()
def ward_name() -> str:
    """A ward of this test's own, so a count assertion cannot be perturbed by
    rows another test in the shared session DB left behind."""
    return f"DIET-{uuid.uuid4().hex[:6].upper()}"


# ══════════════════════════════════════════════════════════════════════════════
#  1. THE DISCHARGE RULE — the count's most important property
# ══════════════════════════════════════════════════════════════════════════════

def test_discharged_patient_leaves_the_kitchen_count(ward_name):
    """Three patients on a ward, one goes home, the kitchen cooks two.

    Nothing tells this module about the discharge. `_active_diet_rows`
    re-derives the current admission on every read, so the count is right on the
    very next build with no hook on the discharge path to fail.
    """
    keep_a = _admit(ward=ward_name, bed="1")
    keep_b = _admit(ward=ward_name, bed="2")
    leaving = _admit(ward=ward_name, bed="3")
    for pid in (keep_a, keep_b, leaving):
        ds.order_diet(patient_id=pid, diet_type="diabetic", ordered_by="D001")

    before = ds.build_meal_plan(ward=ward_name, meal_service="lunch", generated_by="D001")
    assert before["total_meals"] == 3
    assert before["order_count"] == 3

    _discharge(leaving)

    after = ds.build_meal_plan(ward=ward_name, meal_service="lunch", generated_by="D001")
    assert after["total_meals"] == 2, "a discharged patient is still being cooked for"
    assert after["order_count"] == 2
    assert after["lines_total"] == 2

    # And the ward view agrees — one definition of "who is being fed", not two.
    view = ds.ward_view(ward=ward_name)
    assert {r["patient_id"] for r in view["rows"]} == {keep_a, keep_b}

    # The order is NOT deleted. It surfaces as lapsed so a dietitian can close
    # it, rather than vanishing where nobody can audit it.
    stale_ids = {r["patient_id"] for r in ds.stale_orders()}
    assert leaving in stale_ids


def test_discharge_rule_is_load_bearing(ward_name, monkeypatch):
    """PROVEN ABLE TO FAIL.

    This is the same scenario as the test above, run against a service whose
    discharge filter has been removed — the single WHERE clause a future
    "simplification" would delete. The discharged patient comes back into the
    count, which proves the assertion above is testing the filter and not merely
    describing an empty ward.
    """
    keep = _admit(ward=ward_name, bed="1")
    leaving = _admit(ward=ward_name, bed="2")
    for pid in (keep, leaving):
        ds.order_diet(patient_id=pid, diet_type="normal", ordered_by="D001")
    _discharge(leaving)

    # Sanity: with the rule in place, the discharged patient is gone.
    assert ds.build_meal_plan(
        ward=ward_name, meal_service="dinner", generated_by="D001"
    )["total_meals"] == 1

    # Now break exactly the rule under test: "still admitted" becomes "always".
    monkeypatch.setattr(ds, "_STILL_ADMITTED", "1 = 1")
    broken = ds.build_meal_plan(ward=ward_name, meal_service="dinner", generated_by="D001")
    assert broken["total_meals"] == 2, (
        "removing the discharge filter did NOT change the count — the passing "
        "test above is not testing what it claims to test"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  2. THE ARITHMETIC — total == sum(parts), exactly
# ══════════════════════════════════════════════════════════════════════════════

def test_kitchen_count_equals_the_sum_of_its_diet_orders(ward_name):
    """total_meals == sum(line.meal_count), and every order is either a tray or
    an NPO with nothing falling between them."""
    plan_input = [
        ("diabetic", "regular"), ("diabetic", "regular"), ("diabetic", "soft"),
        ("renal", "regular"), ("cardiac", "regular"), ("soft", "pureed"),
        ("liquid", "liquid"), ("normal", "regular"), ("normal", "regular"),
        ("normal", "regular"),
    ]
    for index, (diet, texture) in enumerate(plan_input):
        pid = _admit(ward=ward_name, bed=str(index + 1))
        ds.order_diet(patient_id=pid, diet_type=diet, texture=texture, ordered_by="D001")

    npo_patient = _admit(ward=ward_name, bed="99")
    ds.order_diet(
        patient_id=npo_patient, diet_type="npo", ordered_by="D001",
        npo_reason="for OT at 08:00", npo_until=_in_hours(6),
    )

    plan = ds.build_meal_plan(ward=ward_name, meal_service="breakfast", generated_by="D001")

    parts = [line["meal_count"] for line in plan["lines"]]
    assert plan["total_meals"] == sum(parts)
    assert plan["total_meals"] == len(plan_input) == 10
    assert plan["npo_count"] == 1
    assert plan["order_count"] == 11
    assert plan["total_meals"] + plan["npo_count"] == plan["order_count"]
    assert plan["totals_agree"] is True

    # The lines are keyed on (diet_type, texture): a diabetic soft tray is not
    # a diabetic regular tray, and the kitchen has to cook them differently.
    by_key = {(line["diet_type"], line["texture"]): line["meal_count"] for line in plan["lines"]}
    assert by_key[("diabetic", "regular")] == 2
    assert by_key[("diabetic", "soft")] == 1
    assert by_key[("normal", "regular")] == 3
    assert ("npo", "regular") not in by_key, "an NPO patient must not be cooked a tray"


def test_indent_carries_the_plans_count_exactly(ward_name):
    """The indent is a COPY of the locked plan, to the tray."""
    for index, diet in enumerate(("diabetic", "renal", "normal", "normal")):
        pid = _admit(ward=ward_name, bed=str(index + 1))
        ds.order_diet(patient_id=pid, diet_type=diet, ordered_by="D001")

    plan = ds.build_meal_plan(ward=ward_name, meal_service="lunch", generated_by="D001")
    ds.lock_meal_plan(plan["id"], locked_by="D001")
    indent = ds.raise_kitchen_indent(meal_plan_id=plan["id"], raised_by="D001")

    assert indent["total_meals"] == plan["total_meals"] == 4
    assert indent["lines_total"] == sum(line["meal_count"] for line in indent["lines"])
    assert indent["totals_agree"] is True
    assert [(line["diet_type"], line["texture"], line["meal_count"]) for line in indent["lines"]] \
        == [(line["diet_type"], line["texture"], line["meal_count"]) for line in plan["lines"]]

    issued = ds.issue_kitchen_indent(indent["id"], issued_by="D001")
    assert issued["status"] == "issued"
    assert issued["indent_number"], "an issued indent must carry a number"
    assert issued["total_meals"] == 4


def test_an_indent_cannot_be_raised_off_a_moving_count(ward_name):
    """A draft plan is still being rebuilt as the ward fills. Refusing the indent
    is the difference between a count the kitchen can reconcile and one it
    cannot."""
    pid = _admit(ward=ward_name)
    ds.order_diet(patient_id=pid, diet_type="normal", ordered_by="D001")
    plan = ds.build_meal_plan(ward=ward_name, meal_service="dinner", generated_by="D001")

    with pytest.raises(ds.KitchenIndentError) as exc:
        ds.raise_kitchen_indent(meal_plan_id=plan["id"], raised_by="D001")
    assert "lock it first" in str(exc.value)

    ds.lock_meal_plan(plan["id"], locked_by="D001")
    # And a locked plan cannot be silently rebuilt underneath the kitchen.
    with pytest.raises(ds.MealPlanError) as exc:
        ds.build_meal_plan(ward=ward_name, meal_service="dinner", generated_by="D001")
    assert "locked" in str(exc.value)


# ══════════════════════════════════════════════════════════════════════════════
#  3. NPO — distinct, bounded, attributed, never silently lifted
# ══════════════════════════════════════════════════════════════════════════════

def test_npo_is_rendered_distinctly_and_names_its_clinician(ward_name):
    """The ward view must be able to show NPO patients WITHOUT filtering a mixed
    list, and every NPO row has to answer "who said so, and until when"."""
    doctor = _mk_user("doctor")
    eater = _admit(ward=ward_name, bed="1")
    faster = _admit(ward=ward_name, bed="2", name="NPO Test Patient")
    ds.order_diet(patient_id=eater, diet_type="normal", ordered_by="D001")
    ds.order_diet(
        patient_id=faster, diet_type="npo", ordered_by=doctor,
        npo_reason="for OT at 08:00", npo_until=_in_hours(5),
    )

    view = ds.ward_view(ward=ward_name)

    assert view["npo_count"] == 1
    assert [r["patient_id"] for r in view["npo_rows"]] == [faster]
    assert view["meal_count"] == 1
    assert view["npo_label"] == ds.NPO_LABEL == "NIL BY MOUTH"

    npo_row = view["npo_rows"][0]
    assert npo_row["is_npo"] is True
    assert npo_row["npo_label"] == "NIL BY MOUTH"
    assert npo_row["npo_reason"] == "for OT at 08:00"
    assert npo_row["npo_until_iso"], "an NPO row must carry its end time"
    assert npo_row["ordered_by"] == doctor
    # By NAME, not just an id — the ward reads a name at 07:00.
    assert npo_row["ordered_by_display"] == "Diet Doctor"
    assert npo_row["npo_expired"] is False
    assert 4.0 < npo_row["npo_hours_remaining"] < 5.1

    # The eating patient is not marked NPO by accident.
    meal_row = next(r for r in view["rows"] if r["patient_id"] == eater)
    assert meal_row["is_npo"] is False
    assert meal_row["npo_label"] is None


def test_npo_cannot_be_written_without_an_end_time():
    """Bounded by the SERVICE, and independently by the DATABASE."""
    pid = _admit()

    with pytest.raises(ds.DietOrderError) as exc:
        ds.order_diet(patient_id=pid, diet_type="npo", ordered_by="D001",
                      npo_reason="for OT")
    assert "npo_until" in str(exc.value)

    with pytest.raises(ds.DietOrderError):
        ds.order_diet(patient_id=pid, diet_type="npo", ordered_by="D001",
                      npo_until=_in_hours(4))  # no reason

    with pytest.raises(ds.DietOrderError) as exc:
        ds.order_diet(patient_id=pid, diet_type="npo", ordered_by="D001",
                      npo_reason="for OT", npo_until=_in_hours(-2))
    assert "future" in str(exc.value)

    # The CHECK constraint stands on its own, so a future caller that bypasses
    # the service (a script, a repair tool) still cannot write an endless NPO.
    from db_backend import DBIntegrityError
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute(
                "INSERT INTO diet_orders (patient_id, diet_type, texture, ordered_by) "
                "VALUES (?, 'npo', 'regular', 'D001')",
                (pid,),
            )


def test_an_expired_npo_window_does_not_resume_the_diet(ward_name):
    """The window passing flags a REVIEW. It does not feed anybody.

    Auto-resuming would be this module deciding a patient may now eat, which is
    a clinical decision and one where being wrong means aspiration on the way to
    theatre.
    """
    pid = _admit(ward=ward_name)
    order = ds.order_diet(
        patient_id=pid, diet_type="npo", ordered_by="D001",
        npo_reason="for OT at 08:00", npo_until=_in_hours(1),
    )
    # Move the window into the past exactly as the clock would have.
    with db_session() as db:
        db.execute(
            "UPDATE diet_orders SET npo_until = ? WHERE id = ?",
            ((datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
             order["id"]),
        )

    view = ds.ward_view(ward=ward_name)
    assert view["npo_count"] == 1, "an expired NPO must not silently become a meal"
    row = view["npo_rows"][0]
    assert row["diet_type"] == "npo"
    assert row["npo_expired"] is True
    assert row["needs_review"] is True
    assert "NOT resumed automatically" in row["npo_notice"]
    assert view["npo_needs_review_count"] == 1

    # And the kitchen still does not cook them a tray.
    plan = ds.build_meal_plan(ward=ward_name, meal_service="lunch", generated_by="D001")
    assert plan["total_meals"] == 0
    assert plan["npo_count"] == 1


# ══════════════════════════════════════════════════════════════════════════════
#  4. ALLERGIES — read from the existing record, never copied
# ══════════════════════════════════════════════════════════════════════════════

def test_diet_orders_table_has_no_allergy_column():
    """The structural half of "do not duplicate allergy storage".

    Asserted against the LIVE schema, not against the migration source, so a
    column added by any route — a later migration, an ALTER in a repair script —
    turns this red.
    """
    with db_session() as db:
        columns = {row[1].lower() for row in db.execute("PRAGMA table_info(diet_orders)")}
    offending = [c for c in columns if "allerg" in c]
    assert not offending, (
        f"diet_orders has grown an allergy column {offending} — allergies must be "
        f"READ from {ds.ALLERGY_SOURCE_TABLE}, never copied, or a tray card will "
        f"be printed from a list that went stale when a nurse recorded a reaction"
    )


def test_allergies_are_read_live_not_snapshotted_at_order_time():
    """An allergy recorded AFTER the diet order appears on the order at once.

    This is the behavioural half. A snapshot taken at ordering time would pass
    every test that records the allergy first; recording it AFTER is what tells
    the two designs apart.
    """
    pid = _admit()
    order = ds.order_diet(patient_id=pid, diet_type="diabetic", ordered_by="D001")
    assert order["allergies"] == []
    assert order["allergies_unavailable"] is False

    _record_allergy(pid, "peanut", severity="severe", reaction="anaphylaxis")

    refreshed = ds.get_active_order(pid)
    assert [a["substance"] for a in refreshed["allergies"]] == ["peanut"]
    assert refreshed["allergy_source"] == "patient_allergies"

    # Retiring the allergy on the source record retires it here too.
    with db_session() as db:
        db.execute(
            "UPDATE patient_allergies SET status = 'inactive' WHERE patient_id = ?", (pid,)
        )
    assert ds.get_active_order(pid)["allergies"] == []


def test_allergies_agree_with_the_drug_interlocks_source(ward_name):
    """The tray card and the drug interlock read the same rows.

    services/allergy_interlock.read_active_allergies is what order-time drug
    screening uses. If this module's bulk read ever drifts from that predicate,
    a patient could be blocked from a penicillin and handed a tray containing
    the food they react to, or vice versa.
    """
    from services import allergy_interlock

    pid = _admit(ward=ward_name)
    _record_allergy(pid, "shellfish", severity="moderate", reaction="urticaria")
    _record_allergy(pid, "penicillin", severity="severe")
    ds.order_diet(patient_id=pid, diet_type="normal", ordered_by="D001")

    with db_session() as db:
        interlock_view = {a["substance"] for a in
                          allergy_interlock.read_active_allergies(db, pid)}
    dietary_view = {a["substance"] for a in ds.get_active_order(pid)["allergies"]}
    assert dietary_view == interlock_view == {"shellfish", "penicillin"}


def test_an_unreadable_allergy_list_says_so_instead_of_saying_none(ward_name, monkeypatch):
    """FAIL-CLOSED. An empty allergy list and an unreadable one render
    identically as "no known allergies" and mean opposite things."""
    pid = _admit(ward=ward_name)
    _record_allergy(pid, "egg")
    ds.order_diet(patient_id=pid, diet_type="normal", ordered_by="D001")

    def _boom(db, patient_ids):
        raise RuntimeError("allergy table unreadable")

    monkeypatch.setattr(ds, "_read_allergies", _boom)
    view = ds.ward_view(ward=ward_name)

    assert view["allergies_unavailable"] is True
    assert "Do not assume the patient has no allergies" in view["allergies_message"]
    row = view["rows"][0]
    assert row["allergies_unavailable"] is True
    assert row["allergies"] == []
    assert row["allergies_message"] == ds.ALLERGY_UNAVAILABLE_MESSAGE


# ══════════════════════════════════════════════════════════════════════════════
#  5. ORDERS — one active per patient, superseded not overwritten
# ══════════════════════════════════════════════════════════════════════════════

def test_changing_a_diet_supersedes_rather_than_overwrites():
    pid = _admit()
    first = ds.order_diet(patient_id=pid, diet_type="normal", ordered_by="D001")
    second = ds.order_diet(patient_id=pid, diet_type="renal", ordered_by="D002")

    assert second["supersedes_order_id"] == first["id"]
    history = ds.patient_diet_history(pid)
    assert [h["diet_type"] for h in history] == ["renal", "normal"]
    assert history[1]["status"] == "superseded"
    assert history[0]["status"] == "active"

    with db_session() as db:
        active = db.execute(
            "SELECT COUNT(*) AS n FROM diet_orders WHERE patient_id = ? AND status = 'active'",
            (pid,),
        ).fetchone()["n"]
    assert active == 1, "a patient must never hold two active diet orders"


def test_the_one_active_order_rule_is_enforced_by_the_database():
    """Not merely by the service. A direct insert of a second active order is
    refused by the partial unique index."""
    from db_backend import DBIntegrityError

    pid = _admit()
    ds.order_diet(patient_id=pid, diet_type="normal", ordered_by="D001")
    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute(
                "INSERT INTO diet_orders (patient_id, diet_type, texture, ordered_by, status) "
                "VALUES (?, 'renal', 'regular', 'D001', 'active')",
                (pid,),
            )


def test_a_diet_order_needs_an_admitted_patient():
    pid = _admit()
    _discharge(pid)
    with pytest.raises(ds.DietOrderError) as exc:
        ds.order_diet(patient_id=pid, diet_type="normal", ordered_by="D001")
    assert "not currently admitted" in str(exc.value)

    with pytest.raises(ds.DietOrderError) as exc:
        ds.order_diet(patient_id="NO-SUCH-PATIENT", diet_type="normal", ordered_by="D001")
    assert "unknown patient" in str(exc.value)


def test_an_order_must_name_its_clinician_and_a_known_diet():
    pid = _admit()
    with pytest.raises(ds.DietOrderError) as exc:
        ds.order_diet(patient_id=pid, diet_type="normal", ordered_by="")
    assert "ordered_by" in str(exc.value)

    with pytest.raises(ds.DietOrderError):
        ds.order_diet(patient_id=pid, diet_type="keto", ordered_by="D001")
    with pytest.raises(ds.DietOrderError):
        ds.order_diet(patient_id=pid, diet_type="normal", texture="crunchy", ordered_by="D001")


def test_the_ward_view_shows_admitted_patients_with_no_diet_order(ward_name):
    """A screen that lists only the patients somebody remembered to write an
    order for cannot show you the one they forgot."""
    fed = _admit(ward=ward_name, bed="1")
    forgotten = _admit(ward=ward_name, bed="2", name="Nobody Ordered For Me")
    ds.order_diet(patient_id=fed, diet_type="normal", ordered_by="D001")

    view = ds.ward_view(ward=ward_name)
    assert view["unordered_count"] == 1
    assert view["unordered"][0]["patient_id"] == forgotten


# ══════════════════════════════════════════════════════════════════════════════
#  6. ROUTES — permissions, ward scope, the module switch
# ══════════════════════════════════════════════════════════════════════════════

def test_ward_scope_holds_on_the_query_string_read():
    """A diet order names a patient, a bed and their live allergies. The
    query-string spelling is exactly as much a PHI read as the path spelling,
    and it 404s — never 403, which would confirm the patient exists."""
    mine = _admit(department=DEPT, ward="DIET-SCOPE-A")
    theirs = _admit(department=OTHER_DEPT, ward="DIET-SCOPE-B")
    ds.order_diet(patient_id=mine, diet_type="normal", ordered_by="D001")
    ds.order_diet(patient_id=theirs, diet_type="renal", ordered_by="D001")

    nurse = _mk_user("nurse", DEPT)
    headers = _auth(nurse, "nurse")

    ok = client.get(f"/api/dietary/orders?patient_id={mine}", headers=headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["order"]["diet_type"] == "normal"

    blocked = client.get(f"/api/dietary/orders?patient_id={theirs}", headers=headers)
    assert blocked.status_code == 404, blocked.text


def test_a_catering_account_cannot_change_what_a_patient_eats():
    """`admin` runs the kitchen desk and holds `admin.overview`; it does NOT hold
    `order.write`. Writing a diet is a clinical order."""
    pid = _admit()
    admin_headers = _auth(_mk_user("admin"), "admin")

    refused = client.post(
        "/api/dietary/orders",
        json={"patient_id": pid, "diet_type": "diabetic"},
        headers=admin_headers,
    )
    assert refused.status_code == 403, refused.text

    # But it CAN run the kitchen: build, lock, indent.
    ds.order_diet(patient_id=pid, diet_type="diabetic", ordered_by="D001")
    ward = client.get("/api/dietary/wards", headers=admin_headers)
    assert ward.status_code == 200

    built = client.post(
        "/api/dietary/meal-plans",
        json={"ward": "DIET-W1", "meal_service": "lunch"},
        headers=admin_headers,
    )
    assert built.status_code == 200, built.text


def test_a_nurse_can_write_a_diet_order_through_the_route():
    pid = _admit()
    nurse_headers = _auth(_mk_user("nurse", DEPT), "nurse")
    resp = client.post(
        "/api/dietary/orders",
        json={
            "patient_id": pid, "diet_type": "npo",
            "npo_reason": "for endoscopy", "npo_until": _in_hours(8),
        },
        headers=nurse_headers,
    )
    assert resp.status_code == 200, resp.text
    order = resp.json()["order"]
    assert order["is_npo"] is True
    assert order["npo_label"] == "NIL BY MOUTH"
    assert order["npo_until_iso"]


def test_the_kitchen_board_carries_counts_and_not_one_patient_name(ward_name):
    pid = _admit(ward=ward_name, name="Very Identifiable Person")
    _record_allergy(pid, "peanut")
    ds.order_diet(patient_id=pid, diet_type="diabetic", ordered_by="D001")

    headers = _auth(_mk_user("admin"), "admin")
    resp = client.get("/api/dietary/kitchen", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.text

    assert "Very Identifiable Person" not in body
    assert pid not in body
    assert "peanut" not in body
    payload = resp.json()
    assert payload["contains_phi"] is False
    ward_entry = next(w for w in payload["wards"] if w["ward"] == ward_name)
    assert ward_entry["total_meals"] == 1


def test_a_disabled_module_404s_the_whole_desk(monkeypatch):
    """ORB_DIETARY=0 means this site has no diet desk. 404, not 403: a 403 says
    "you are not allowed", which is a different and wrong statement."""
    import config as cfg
    headers = _auth(_mk_user("nurse", DEPT), "nurse")
    monkeypatch.setattr(cfg, "DIETARY_ENABLED", False)

    for path in ("/api/dietary/meta", "/api/dietary/ward", "/api/dietary/summary"):
        resp = client.get(path, headers=headers)
        assert resp.status_code == 404, f"{path}: {resp.text}"
        assert resp.json()["detail"]["error"] == "module_disabled"

    assert cfg.enabled_modules()["dietary"] is False


def test_the_module_flag_is_published_on_health():
    body = client.get("/api/health").json()
    assert "dietary" in body["modules"], (
        "the shell learns which tabs to build from /api/health — a module the "
        "server does not publish is a tab that cannot be switched off"
    )


def test_meal_plan_payloads_do_not_carry_order_ids():
    """`basis_order_ids` is a forensic aid held in the database. An order id is a
    handle onto a patient and the catering screen has no use for one."""
    pid = _admit(ward="DIET-BASIS")
    ds.order_diet(patient_id=pid, diet_type="normal", ordered_by="D001")
    headers = _auth(_mk_user("admin"), "admin")
    built = client.post(
        "/api/dietary/meal-plans",
        json={"ward": "DIET-BASIS", "meal_service": "dinner"},
        headers=headers,
    ).json()["plan"]
    assert "basis_order_ids" not in built
    assert "basis_order_ids_json" not in built
    # …but it IS recorded, so the count remains explicable after the fact.
    with db_session() as db:
        stored = db.execute(
            "SELECT basis_order_ids_json FROM meal_plans WHERE id = ?", (built["id"],)
        ).fetchone()["basis_order_ids_json"]
    assert stored and str(stored).startswith("[")


# ══════════════════════════════════════════════════════════════════════════════
#  7. THE PILOT-SAFETY PROMISE
# ══════════════════════════════════════════════════════════════════════════════

def test_this_module_writes_only_its_own_tables():
    """A dietary write must never be able to fail a clinical write. The service
    is the thing that would break that promise, so it is the thing asserted:
    every INSERT/UPDATE/DELETE target in dietary_service.py is a dietary table.
    """
    import ast
    import inspect
    import re

    allowed = {
        "diet_orders", "meal_plans", "meal_plan_lines",
        "kitchen_indents", "kitchen_indent_lines",
        # The ONE shared table this module appends to, and only through
        # migration 25's seed: the document-number series row for KIND. The
        # service itself never writes it — services/billing_masters owns that
        # allocator — so it must not appear below.
    }
    # Scan the strings actually handed to db.execute / db.executemany, found via
    # the AST. A regex over raw source (or over every string constant) matches
    # the English in this module's long docstrings — "nothing to update and
    # nothing to go wrong" — and a guard that fires on its own prose is a guard
    # somebody deletes. Only real SQL literals count.
    tree = ast.parse(inspect.getsource(ds))
    sql_literals: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany")):
            continue
        for arg in node.args[:1]:
            for piece in ast.walk(arg):
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    sql_literals.append(piece.value)

    targets = set()
    for sql in sql_literals:
        for match in re.finditer(
            r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(\w+)",
            sql, re.IGNORECASE,
        ):
            targets.add(match.group(1).lower())
    assert targets, "the write-target scan found nothing — the scan is broken, not the module"
    forbidden = targets - allowed
    assert not forbidden, (
        f"dietary_service writes to {sorted(forbidden)} — this module may write "
        f"ONLY its own tables. It reads patients, admissions, users and "
        f"patient_allergies; it writes none of them."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  8. WHAT THE PANEL IS TOLD ABOUT THE CALLER AND THE HOUR
# ══════════════════════════════════════════════════════════════════════════════

def test_meta_tells_the_panel_what_this_caller_may_do():
    """The panel hides what the server would refuse. A catering account runs the
    kitchen and never writes a diet; a nurse does both. Read from the same
    permission checks the write routes use, so the two cannot drift."""
    nurse = client.get(
        "/api/dietary/meta", headers=_auth(_mk_user("nurse", DEPT), "nurse")
    ).json()
    admin = client.get(
        "/api/dietary/meta", headers=_auth(_mk_user("admin"), "admin")
    ).json()
    assert nurse["capabilities"] == {"write_orders": True, "run_kitchen": True}
    assert admin["capabilities"] == {"write_orders": False, "run_kitchen": True}


def test_meta_carries_the_hospitals_day_and_current_meal_service(monkeypatch):
    """The desk opens on the service the ward is counting for at this hour of the
    HOSPITAL's day, not on Lunch at 02:00, and lists today's plans by the
    hospital's date rather than the browser's."""
    from services import hospital_time

    tz = hospital_time.hospital_timezone()
    headers = _auth(_mk_user("nurse", DEPT), "nurse")
    for hour, expected in ((2, "breakfast"), (9, "breakfast"), (12, "lunch"), (19, "dinner")):
        local = datetime(2026, 10, 3, hour, 30, tzinfo=tz)
        monkeypatch.setattr(hospital_time, "_utc_now", lambda local=local: local)
        body = client.get("/api/dietary/meta", headers=headers).json()
        assert body["current_meal_service"] == expected, (hour, body["current_meal_service"])
        assert body["service_date"] == "2026-10-03"


def test_the_summary_says_whose_wards_it_counts(ward_name):
    """The tiles are ward-scoped and the kitchen tab is hospital-wide. The scope
    travels with the counts so the panel can say which wards it counted, and can
    explain a department with nobody admitted instead of showing bare zeros."""
    _admit(ward=ward_name, department=DEPT)

    scoped = client.get(
        "/api/dietary/summary", headers=_auth(_mk_user("nurse", DEPT), "nurse")
    ).json()["scope"]
    assert scoped["department"] == DEPT
    assert scoped["hospital_wide"] is False
    assert ward_name in scoped["wards"]

    nobody = client.get(
        "/api/dietary/summary",
        headers=_auth(_mk_user("doctor", "Dietary Test Nobody Admitted"), "doctor"),
    ).json()["scope"]
    assert nobody["wards"] == []
    assert nobody["hospital_wide"] is False

    wide = client.get(
        "/api/dietary/summary", headers=_auth(_mk_user("admin", None), "admin")
    ).json()["scope"]
    assert wide["hospital_wide"] is True
    assert ward_name in wide["wards"]
