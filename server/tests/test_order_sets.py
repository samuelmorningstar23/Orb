"""Order sets / protocols — and, above everything else, the proof that a set is
a CONVENIENCE and NEVER A BYPASS.

The feature's whole risk is one sentence long: an order set is the quiet path
around the interlocks. A clinician who is blocked when they type ceftriaxone by
hand, and not blocked when the sepsis bundle types it for them, has been handed
a way to switch the allergy interlock off — and within a month everybody uses
the bundle. So the majority of this module is attacks on that property:

  * a severe allergy on ONE drug refuses the WHOLE set and writes NOTHING;
  * a 10x dose edited in at apply time is stopped by the dose guard;
  * concentrated potassium re-routed to IV push is stopped;
  * intrathecal vincristine cannot be smuggled in through a protocol file;
  * a dose-guard BLOCK cannot be acknowledged away at set level;
  * a discharged patient cannot receive a protocol;
  * and every refusal leaves the orders table exactly as it was.

Plus the config contract: a malformed protocol file must fail LOUDLY at startup,
naming the file and the line, and must never present as "this site has no
protocols".
"""
import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import order_sets_service, orders_service, terminology_service

client = TestClient(app)

SERVER_ROOT = Path(__file__).resolve().parent.parent

# Distinctive so this module's fixtures cannot be confused with another's in the
# shared session DB.
DEPT = "Order Set General Medicine"
OTHER_DEPT = "Order Set Oncology"


# ═════════════════════════════════════════════════════════════════════════════
#  FIXTURES
# ═════════════════════════════════════════════════════════════════════════════

def _mk_user(role: str, department: str | None) -> str:
    uid = f"OSET{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Order Set {role}", role, department),
        )
    return uid


def _auth(user_id: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _mk_patient(department: str = DEPT) -> str:
    pid = f"POSET{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, ?, ?, 'admitted')",
            (pid, "Order Set Test", f"MRN-OSET-{uuid.uuid4().hex[:8]}"),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
            "VALUES (?, ?, 'D001', ?, 'active')",
            (f"AOSET{uuid.uuid4().hex[:8].upper()}", pid, department),
        )
    return pid


@pytest.fixture(scope="module", autouse=True)
def _module_world():
    """TEST-ISOLATION LAW: every patient this module admits is discharged in
    teardown, or a later module measures our leftovers as its own census."""
    terminology_service.seed_terminology()
    created: list[str] = []
    yield created
    if created:
        marks = ",".join("?" for _ in created)
        with db_session() as db:
            db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})",
                       tuple(created))
            db.execute(f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
                       tuple(created))


@pytest.fixture()
def patient(_module_world):
    pid = _mk_patient()
    _module_world.append(pid)
    return pid


@pytest.fixture(scope="module")
def doctor():
    return _mk_user("doctor", DEPT)


@pytest.fixture(scope="module")
def other_ward_doctor():
    return _mk_user("doctor", OTHER_DEPT)


def _orders_of(patient_id: str) -> list[dict]:
    return orders_service.list_orders(patient_id)


def _chart_allergy(patient_id: str, substance: str, severity: str = "severe",
                   reaction: str = "anaphylaxis") -> None:
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO patient_allergies "
            "(patient_id, substance, reaction, severity, status, source) "
            "VALUES (?, ?, ?, ?, 'active', 'clinician')",
            (patient_id, substance, reaction, severity),
        )


# ── config swapping ──────────────────────────────────────────────────────────

_MIN_ITEM = {"order_type": "nursing", "description": "Observations hourly", "priority": "stat"}


def _cfg(items: list[dict], set_id: str = "unit_test_set") -> dict:
    return {
        "config_version": 1,
        "sets": [{
            "id": set_id,
            "name": "Unit test protocol",
            "guideline": {"name": "Unit test", "citation": "Not a real guideline."},
            "items": items,
        }],
    }


def _write(tmp_path: Path, payload, name: str = "order_sets.json") -> Path:
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, indent=2),
                    encoding="utf-8")
    return path


@contextmanager
def use_config(path: Path):
    """Point the live service at a different protocol file, then put the shipped
    one back — a leaked config would silently change every later test."""
    previous = os.environ.get(order_sets_service.CONFIG_PATH_ENV)
    os.environ[order_sets_service.CONFIG_PATH_ENV] = str(path)
    try:
        order_sets_service.reload_config()
        yield
    finally:
        if previous is None:
            os.environ.pop(order_sets_service.CONFIG_PATH_ENV, None)
        else:
            os.environ[order_sets_service.CONFIG_PATH_ENV] = previous
        order_sets_service.reload_config()


# ═════════════════════════════════════════════════════════════════════════════
#  1. THE SHIPPED PROTOCOL FILE
# ═════════════════════════════════════════════════════════════════════════════

def test_the_shipped_file_loads_and_ships_the_five_named_protocols():
    ids = {s["id"] for s in order_sets_service.list_sets()}
    assert {"sepsis_six", "acs_initial", "dka_initial",
            "post_operative", "acute_asthma"} <= ids


def test_every_shipped_set_cites_a_guideline():
    """A protocol with no provenance cannot be defended, reviewed or audited."""
    for s in order_sets_service.list_sets():
        g = s["guideline"]
        assert g["name"].strip(), f"{s['id']} has no guideline name"
        assert len(g["citation"].strip()) > 30, f"{s['id']} has no real citation"


def test_every_shipped_set_is_an_ordered_list_of_real_order_types():
    for summary in order_sets_service.list_sets():
        s = order_sets_service.get_set(summary["id"])
        assert s["items"], f"{s['id']} has no items"
        assert [i["index"] for i in s["items"]] == list(range(len(s["items"])))
        for item in s["items"]:
            assert item["order_type"] in {"lab", "imaging", "medication",
                                          "procedure", "nursing", "other"}


def test_every_code_the_shipped_protocols_use_resolves_in_this_deployment():
    """Migration 13 seeds the local codes the starter protocols order. If the
    file and the migration ever drift, a ward applying a bundle gets a 400 on an
    order they cannot fix — so they are tied together here."""
    for s in order_sets_service.list_sets():
        assert order_sets_service.unknown_codes(s["id"]) == [], (
            f"{s['id']} names codes this deployment cannot resolve")


def test_no_shipped_protocol_item_is_stopped_by_the_dose_guard():
    """The load-time dose screen already enforces this; asserted separately so a
    future edit to the file cannot pass by also weakening the loader."""
    from services import drug_safety_service as dsg
    for summary in order_sets_service.list_sets():
        for item in order_sets_service.get_set(summary["id"])["items"]:
            if item["order_type"] != "medication":
                continue
            verdict = dsg.evaluate_dose(item["drug_name"], item["dose"], item["route"])
            assert verdict["decision"] != dsg.DOSE_BLOCK, (
                f"{summary['id']} item {item['index']} ships a dose the guard stops")


# ═════════════════════════════════════════════════════════════════════════════
#  2. A MALFORMED FILE FAILS LOUDLY, AT STARTUP, WITH A LINE NUMBER
# ═════════════════════════════════════════════════════════════════════════════

def test_broken_json_names_the_file_and_the_line(tmp_path):
    path = _write(tmp_path, '{\n  "config_version": 1,\n  "sets": [\n    {,\n  ]\n}\n')
    with pytest.raises(order_sets_service.OrderSetConfigError) as e:
        order_sets_service.load_config(path)
    msg = str(e.value)
    assert "order_sets.json:4" in msg, msg
    assert "not valid JSON" in msg


def test_a_missing_file_is_a_startup_failure_not_an_empty_protocol_list(tmp_path):
    with pytest.raises(order_sets_service.OrderSetConfigError) as e:
        order_sets_service.load_config(tmp_path / "nope.json")
    assert "not found" in str(e.value)


def test_a_set_with_no_citation_refuses_to_load(tmp_path):
    payload = _cfg([_MIN_ITEM])
    payload["sets"][0]["guideline"] = {"name": "Something"}
    path = _write(tmp_path, payload)
    with pytest.raises(order_sets_service.OrderSetConfigError) as e:
        order_sets_service.load_config(path)
    assert "citation" in str(e.value)


def test_a_typo_in_a_field_name_is_refused_rather_than_ignored(tmp_path):
    """`dosage` instead of `dose` leaves the dose guard nothing to check and the
    order text unchanged — the quietest possible failure, so it is fatal."""
    payload = _cfg([{
        "order_type": "medication", "description": "Digoxin loading dose",
        "code": "ORB-MED-KCL", "code_system": "LOCAL", "priority": "stat",
        "drug_name": "Digoxin", "dosage": "2.5 mg", "route": "IV",
        "frequency": "once",
    }])
    path = _write(tmp_path, payload)
    with pytest.raises(order_sets_service.OrderSetConfigError) as e:
        order_sets_service.load_config(path)
    msg = str(e.value)
    assert "dosage" in msg
    assert "order_sets.json:" in msg


def test_the_loader_reports_every_problem_at_once(tmp_path):
    """One restart per typo is how an engineer ends up deleting the validation."""
    payload = _cfg([
        {"order_type": "telepathy", "description": "Not a thing"},
        {"order_type": "lab", "description": ""},
        {"order_type": "lab", "description": "No code at all"},
    ])
    path = _write(tmp_path, payload)
    with pytest.raises(order_sets_service.OrderSetConfigError) as e:
        order_sets_service.load_config(path)
    msg = str(e.value)
    assert "'telepathy' is not a valid order type" in msg
    assert "description: must be a non-empty string" in msg
    assert "needs a code + code_system" in msg
    assert msg.count("order_sets.json") >= 3


def test_duplicate_set_ids_refuse_to_load(tmp_path):
    payload = _cfg([_MIN_ITEM])
    payload["sets"].append(json.loads(json.dumps(payload["sets"][0])))
    path = _write(tmp_path, payload)
    with pytest.raises(order_sets_service.OrderSetConfigError) as e:
        order_sets_service.load_config(path)
    assert "duplicate set id" in str(e.value)


def test_a_non_medication_item_may_not_carry_a_dose(tmp_path):
    payload = _cfg([{**_MIN_ITEM, "dose": "500 mg"}])
    path = _write(tmp_path, payload)
    with pytest.raises(order_sets_service.OrderSetConfigError) as e:
        order_sets_service.load_config(path)
    assert "only applies to a medication item" in str(e.value)


# ── the dose guard, applied to the PROTOCOL FILE itself ──────────────────────

@pytest.mark.parametrize("drug,dose,route,fragment", [
    ("Digoxin", "2.5 mg", "IV", "decimal"),
    ("Potassium chloride", "40 mmol", "IV push", "IV push"),
    ("Vincristine", "1 mg", "intrathecal", "intrathecally"),
    ("Insulin soluble human", "100 units", "subcutaneous", "decimal"),
])
def test_a_protocol_that_encodes_a_lethal_dose_or_route_never_loads(
        tmp_path, drug, dose, route, fragment):
    """A dose-guard BLOCK in a FILE is not one clinician's judgement about one
    patient — it is a lethal instruction that would be applied to every future
    patient who gets this protocol. It is caught once, before anyone is on the
    ward, not per-patient."""
    payload = _cfg([{
        "order_type": "medication", "description": f"{drug} {dose} {route}",
        "code": "ORB-MED-KCL", "code_system": "LOCAL", "priority": "stat",
        "drug_name": drug, "dose": dose, "route": route, "frequency": "once",
    }])
    path = _write(tmp_path, payload)
    with pytest.raises(order_sets_service.OrderSetConfigError) as e:
        order_sets_service.load_config(path)
    msg = str(e.value)
    assert "THE DOSE GUARD STOPS THIS ITEM" in msg
    assert fragment in msg


# ── proof the failure happens at IMPORT, i.e. at server start ───────────────

def test_the_config_is_loaded_at_import_time():
    assert order_sets_service._CONFIG is not None


def test_importing_the_service_with_a_broken_file_kills_the_process(tmp_path):
    """The mechanism behind 'fail loudly at startup': the module loads its config
    at import, and routes/orders_routes imports the module, and main imports that
    router — so uvicorn never binds a port over a broken protocol file."""
    bad = _write(tmp_path, '{ "config_version": 1, "sets": [ }')
    env = {
        **os.environ,
        order_sets_service.CONFIG_PATH_ENV: str(bad),
        "ORB_DEPLOYMENT_PROFILE": "dev",
        "ORB_DB_PATH": str(tmp_path / "orb_test_startup.db"),
        "PYTHONPATH": str(SERVER_ROOT),
    }
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [sys.executable, "-c", "import services.order_sets_service"],
        cwd=str(SERVER_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode != 0, "a broken protocol file must stop the process"
    assert "OrderSetConfigError" in proc.stderr, proc.stderr[-2000:]


def test_the_import_chain_from_main_to_the_loader_is_intact():
    """The subprocess above proves the module dies. This proves the module is
    actually on main's import path, so that death is a startup death."""
    routes_src = (SERVER_ROOT / "routes" / "orders_routes.py").read_text()
    main_src = (SERVER_ROOT / "main.py").read_text()
    assert "order_sets_service" in routes_src
    assert "orders_routes" in main_src


# ═════════════════════════════════════════════════════════════════════════════
#  3. APPLYING A SET MAKES REAL, INDIVIDUAL, DRAFT ORDERS
# ═════════════════════════════════════════════════════════════════════════════

def test_applying_a_set_creates_one_real_draft_order_per_item(patient, doctor):
    definition = order_sets_service.get_set("sepsis_six")
    result = order_sets_service.apply_set(
        patient, "sepsis_six", actor_id=doctor, actor_role="doctor")

    orders = _orders_of(patient)
    assert len(orders) == len(definition["items"]) == len(result["orders"])
    assert {o["status"] for o in orders} == {"draft"}, (
        "a set must never chart anything as active — the clinician still commits")
    assert {o["source"] for o in orders} == {"order_set"}


def test_each_order_from_a_set_carries_its_own_event_trail_and_provenance(patient, doctor):
    result = order_sets_service.apply_set(
        patient, "acs_initial", actor_id=doctor, actor_role="doctor")
    for order in result["orders"]:
        events = orders_service.get_order_events(order["id"])
        assert len(events) == 1 and events[0]["new_status"] == "draft"
        detail = json.loads(order["detail_json"])
        assert detail["order_set"]["set_id"] == "acs_initial"
        assert detail["order_set"]["application_id"] == result["application_id"]
        assert detail["order_set"]["guideline"]["citation"]


def test_orders_from_a_set_are_individually_cancellable_and_committable(patient, doctor):
    result = order_sets_service.apply_set(
        patient, "post_operative", actor_id=doctor, actor_role="doctor")
    first, second = result["orders"][0], result["orders"][1]

    orders_service.transition_order(first["id"], "cancelled", doctor, "not needed")
    orders_service.transition_order(second["id"], "active", doctor, "confirmed")

    assert orders_service.get_order(first["id"])["status"] == "cancelled"
    assert orders_service.get_order(second["id"])["status"] == "active"
    # Cancelling one leaves the others untouched — a set is not one lump.
    others = [o for o in result["orders"][2:]]
    assert {orders_service.get_order(o["id"])["status"] for o in others} == {"draft"}


def test_one_audited_event_names_the_set_and_lists_its_orders(patient, doctor):
    result = order_sets_service.apply_set(
        patient, "dka_initial", actor_id=doctor, actor_role="doctor")
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM audit_logs WHERE patient_id = ? AND action_type = 'order_set_applied' "
            "ORDER BY id DESC LIMIT 1", (patient,)
        ).fetchone()
    assert row is not None, "applying a protocol must leave one audited event"
    meta = json.loads(row["metadata_json"] if "metadata_json" in row.keys() else row["metadata"])
    assert meta["set_id"] == "dka_initial"
    assert meta["guideline"]["citation"]
    assert sorted(meta["order_ids"]) == sorted(o["id"] for o in result["orders"])
    assert len(meta["items"]) == len(result["orders"])


def test_the_application_ledger_records_the_guideline_and_the_file_version(patient, doctor):
    definition = order_sets_service.get_set("sepsis_six")
    result = order_sets_service.apply_set(
        patient, "sepsis_six", actor_id=doctor, actor_role="doctor")
    applications = order_sets_service.list_applications(patient)
    assert len(applications) == 1
    app_row = applications[0]
    assert app_row["set_id"] == "sepsis_six"
    assert app_row["order_count"] == len(result["orders"])
    assert app_row["config_fingerprint"] == definition["fingerprint"]
    assert definition["guideline"]["citation"] in (app_row["guideline"] or "")
    with db_session() as db:
        linked = db.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE order_set_application_id = ?",
            (app_row["id"],)).fetchone()["n"]
    assert linked == len(result["orders"])


# ═════════════════════════════════════════════════════════════════════════════
#  4. THE SAFETY PROPERTY — A SET IS NEVER A BYPASS
#     (this section is the reason the feature exists in this shape)
# ═════════════════════════════════════════════════════════════════════════════

def test_a_severe_allergy_on_one_drug_refuses_the_whole_set(patient, doctor):
    _chart_allergy(patient, "ceftriaxone")
    with pytest.raises(order_sets_service.OrderSetBlocked) as e:
        order_sets_service.apply_set(patient, "sepsis_six",
                                     actor_id=doctor, actor_role="doctor")
    blocked = e.value.blocked
    assert len(blocked) == 1
    assert blocked[0]["drug_name"] == "Ceftriaxone"
    assert blocked[0]["block_kind"] == "drug_safety_acknowledgement_required"


def test_a_refused_set_writes_nothing_at_all(patient, doctor):
    """NOT 'eight of nine'. A half-applied protocol is worse than a refused one:
    the chart then shows a bundle nobody completed and nobody knows to finish."""
    _chart_allergy(patient, "ceftriaxone")
    with pytest.raises(order_sets_service.OrderSetBlocked):
        order_sets_service.apply_set(patient, "sepsis_six",
                                     actor_id=doctor, actor_role="doctor")
    assert _orders_of(patient) == []
    assert order_sets_service.list_applications(patient) == []


def test_the_same_drug_is_blocked_through_the_set_and_through_plain_cpoe(patient, doctor):
    """The bypass test in its purest form: whatever the hand-typed path does with
    this drug on this patient, the set must do too."""
    _chart_allergy(patient, "ceftriaxone")
    hdr = _auth(doctor, "doctor")

    by_hand = client.post("/api/orders", headers=hdr, json={
        "patient_id": patient, "order_type": "medication",
        "description": "Ceftriaxone", "code": "ORB-MED-CEFTRIAXONE",
        "code_system": "LOCAL",
    })
    by_set = client.post("/api/orders/sets/sepsis_six/apply", headers=hdr,
                         json={"patient_id": patient})

    assert by_hand.status_code == 428
    assert by_set.status_code == 428
    assert _orders_of(patient) == []


def test_an_acknowledged_severe_allergy_proceeds_and_is_audited(patient, doctor):
    _chart_allergy(patient, "ceftriaxone")
    definition = order_sets_service.get_set("sepsis_six")
    ceftriaxone = next(i for i in definition["items"] if i["drug_name"] == "Ceftriaxone")

    result = order_sets_service.apply_set(
        patient, "sepsis_six", actor_id=doctor, actor_role="doctor",
        selections=[{"index": ceftriaxone["index"], "safety_ack": {
            "acknowledged": True,
            "reason": "No alternative cover available; allergy is a childhood rash, "
                      "discussed with microbiology.",
        }}],
    )
    assert len(result["orders"]) == len(definition["items"])
    verdict = next(v for v in result["items"] if v["drug_name"] == "Ceftriaxone")
    assert verdict["medication_safety"]["override"]["by"] == doctor

    with db_session() as db:
        overrides = db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE patient_id = ? "
            "AND action_type = 'drug_safety_override'", (patient,)).fetchone()["n"]
    assert overrides >= 1, "an override must land on the tamper-evident chain"


def test_an_acknowledgement_with_no_reason_does_not_unlock_the_set(patient, doctor):
    _chart_allergy(patient, "ceftriaxone")
    definition = order_sets_service.get_set("sepsis_six")
    ceftriaxone = next(i for i in definition["items"] if i["drug_name"] == "Ceftriaxone")
    with pytest.raises(order_sets_service.OrderSetBlocked):
        order_sets_service.apply_set(
            patient, "sepsis_six", actor_id=doctor, actor_role="doctor",
            selections=[{"index": ceftriaxone["index"],
                         "safety_ack": {"acknowledged": True, "reason": "   "}}],
        )
    assert _orders_of(patient) == []


def test_a_class_allergy_still_fires_through_a_set(patient, doctor):
    """Charted as a CLASS ('cephalosporins'), not as the drug name — the set must
    not need the exact string to stop."""
    _chart_allergy(patient, "cephalosporins")
    with pytest.raises(order_sets_service.OrderSetBlocked) as e:
        order_sets_service.apply_set(patient, "sepsis_six",
                                     actor_id=doctor, actor_role="doctor")
    assert any(b["drug_name"] == "Ceftriaxone" for b in e.value.blocked)
    assert _orders_of(patient) == []


# ── the dose guard, at apply time ────────────────────────────────────────────

def test_the_dose_guard_stops_a_10x_dose_edited_in_at_apply_time(patient, doctor):
    """The protocol says paracetamol 1 g. A clinician edits it to 10 g on the way
    through. The guard reads the EDITED value, not the file's."""
    definition = order_sets_service.get_set("post_operative")
    paracetamol = next(i for i in definition["items"] if i["drug_name"] == "Paracetamol")
    with pytest.raises(order_sets_service.OrderSetBlocked) as e:
        order_sets_service.apply_set(
            patient, "post_operative", actor_id=doctor, actor_role="doctor",
            selections=[{"index": paracetamol["index"], "dose": "10 g"}],
        )
    blocked = e.value.blocked
    assert [b["block_kind"] for b in blocked] == ["dose_guard_block"]
    assert "decimal" in blocked[0]["block_reason"]
    assert _orders_of(patient) == []


def test_the_dose_guard_stops_concentrated_potassium_re_routed_to_iv_push(patient, doctor):
    """The DKA protocol says 'IV infusion' and is correct. Re-routing it at apply
    time to an IV push is the ISMP never-event, and the set must refuse."""
    definition = order_sets_service.get_set("dka_initial")
    kcl = next(i for i in definition["items"] if i["drug_name"] == "Potassium chloride")
    with pytest.raises(order_sets_service.OrderSetBlocked) as e:
        order_sets_service.apply_set(
            patient, "dka_initial", actor_id=doctor, actor_role="doctor",
            selections=[{"index": kcl["index"], "route": "IV push"}],
        )
    assert "NEVER be given by IV push" in e.value.blocked[0]["block_reason"]
    assert _orders_of(patient) == []


def test_a_dose_guard_block_cannot_be_acknowledged_away_at_set_level(patient, doctor):
    """The severe-allergy override exists because a clinician may genuinely know
    better about THIS patient. A 10x dose arriving through a protocol is not that
    — it is a wrong number, and clicking past it once per patient is how the 10x
    block gets worked around. 422, not 428."""
    definition = order_sets_service.get_set("post_operative")
    paracetamol = next(i for i in definition["items"] if i["drug_name"] == "Paracetamol")
    hdr = _auth(doctor, "doctor")
    response = client.post(
        "/api/orders/sets/post_operative/apply", headers=hdr,
        json={"patient_id": patient, "items": [{
            "index": paracetamol["index"], "dose": "10 g",
            "safety_ack": {"acknowledged": True,
                           "reason": "Deliberate, discussed with the consultant."},
        }]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "order_set_blocked"
    assert _orders_of(patient) == []


def test_intrathecal_vincristine_cannot_be_smuggled_in_through_a_protocol(tmp_path, patient, doctor):
    """Belt AND braces: even if a file somehow carried it past the loader, the
    per-patient screen stops it. Proven by loading the item with the loader's
    file-level check bypassed, exactly as a corrupted deploy would."""
    definition = _cfg([{
        "order_type": "medication", "description": "Vincristine 1 mg intrathecal",
        "code": "ORB-MED-KCL", "code_system": "LOCAL", "priority": "stat",
        "drug_name": "Vincristine", "dose": "1 mg", "route": "IV",
        "frequency": "once",
    }], set_id="smuggle_test")
    path = _write(tmp_path, definition)
    with use_config(path):
        with pytest.raises(order_sets_service.OrderSetBlocked) as e:
            order_sets_service.apply_set(
                patient, "smuggle_test", actor_id=doctor, actor_role="doctor",
                selections=[{"index": 0, "route": "intrathecal"}],
            )
        assert "universally fatal" in e.value.blocked[0]["block_reason"]
    assert _orders_of(patient) == []


def test_the_allergy_screen_runs_through_the_one_enforcement_point(patient, doctor):
    """Not a re-implementation living in this service: the same function CPOE
    calls, which is why its own clinical_safety audit rows appear, named for the
    order-set entry point."""
    order_sets_service.apply_set(patient, "acute_asthma",
                                 actor_id=doctor, actor_role="doctor")
    medications = [i for i in order_sets_service.get_set("acute_asthma")["items"]
                   if i["order_type"] == "medication"]
    with db_session() as db:
        rows = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE patient_id = ? "
            "AND event_category = 'clinical_safety'", (patient,)).fetchall()
    entry_points = [json.loads(r["metadata_json"]).get("entry_point") for r in rows]
    assert entry_points.count("order_set.apply") == len(medications), (
        "every medication in the set must have been screened by the shared gate")


def test_a_discharged_patient_cannot_receive_a_protocol(patient, doctor):
    with db_session() as db:
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (patient,))
    with pytest.raises(order_sets_service.OrderSetError) as e:
        order_sets_service.apply_set(patient, "sepsis_six",
                                     actor_id=doctor, actor_role="doctor")
    assert "not admitted" in str(e.value)
    assert _orders_of(patient) == []


def test_an_unknown_patient_cannot_receive_a_protocol(doctor):
    with pytest.raises(order_sets_service.OrderSetError) as e:
        order_sets_service.apply_set("PDOESNOTEXIST", "sepsis_six",
                                     actor_id=doctor, actor_role="doctor")
    assert "not found" in str(e.value)


def test_an_unresolvable_code_refuses_the_set_before_anything_is_written(
        tmp_path, patient, doctor):
    payload = _cfg([
        {"order_type": "lab", "description": "A real lab",
         "code": "2524-7", "code_system": "LOINC", "priority": "stat"},
        {"order_type": "lab", "description": "A lab this site has never loaded",
         "code": "ORB-LAB-NOT-LOADED", "code_system": "LOCAL", "priority": "stat"},
    ], set_id="unknown_code_set")
    path = _write(tmp_path, payload)
    with use_config(path):
        with pytest.raises(order_sets_service.OrderSetError) as e:
            order_sets_service.apply_set(patient, "unknown_code_set",
                                         actor_id=doctor, actor_role="doctor")
        assert "ORB-LAB-NOT-LOADED" in str(e.value)
    assert _orders_of(patient) == [], "not even the valid item may be written"


# ═════════════════════════════════════════════════════════════════════════════
#  5. PREVIEW — the same verdicts, no writes, no silent overrides
# ═════════════════════════════════════════════════════════════════════════════

def test_preview_writes_no_orders_and_reports_what_would_happen(patient, doctor):
    result = order_sets_service.preview_set(patient, "sepsis_six", actor_id=doctor)
    assert result["would_apply"] is True
    assert len(result["items"]) == len(order_sets_service.get_set("sepsis_six")["items"])
    assert _orders_of(patient) == []


def test_preview_and_apply_agree_about_what_is_blocked(patient, doctor):
    _chart_allergy(patient, "ceftriaxone")
    preview = order_sets_service.preview_set(patient, "sepsis_six", actor_id=doctor)
    with pytest.raises(order_sets_service.OrderSetBlocked) as e:
        order_sets_service.apply_set(patient, "sepsis_six",
                                     actor_id=doctor, actor_role="doctor")
    assert [b["index"] for b in preview["blocked"]] == [b["index"] for b in e.value.blocked]


def test_a_fresh_patient_has_no_recent_application(patient, doctor):
    result = order_sets_service.preview_set(patient, "sepsis_six", actor_id=doctor)
    assert result["recent_application"] is None


def test_preview_flags_the_same_set_applied_minutes_ago(patient, doctor):
    """apply_set has no duplicate guard, so the preview must say a second
    Apply would place a second set of STAT drafts, and who placed the first."""
    first = order_sets_service.apply_set(patient, "sepsis_six",
                                         actor_id=doctor, actor_role="doctor")
    preview = order_sets_service.preview_set(patient, "sepsis_six", actor_id=doctor)
    recent = preview["recent_application"]
    assert recent is not None
    assert recent["application_id"] == first["application_id"]
    assert recent["applied_by"] == doctor
    assert recent["applied_by_name"] == "Order Set doctor"
    assert recent["order_count"] == len(first["orders"])
    assert recent["minutes_ago"] == 0

    # A different protocol on the same patient is not a repeat.
    other = order_sets_service.preview_set(patient, "acute_asthma", actor_id=doctor)
    assert other["recent_application"] is None


def test_an_old_application_is_not_flagged_as_recent(patient, doctor):
    order_sets_service.apply_set(patient, "sepsis_six", actor_id=doctor, actor_role="doctor")
    with db_session() as db:
        db.execute(
            "UPDATE order_set_applications SET applied_at = datetime('now', '-3 hours') "
            "WHERE patient_id = ?", (patient,))
    assert order_sets_service.recent_application(patient, "sepsis_six") is None


def test_the_recent_flag_reaches_the_preview_response(patient, doctor):
    client = TestClient(app)
    order_sets_service.apply_set(patient, "sepsis_six", actor_id=doctor, actor_role="doctor")
    r = client.post("/api/orders/sets/sepsis_six/preview", json={"patient_id": patient},
                    headers=_auth(doctor, "doctor"))
    assert r.status_code == 200, r.text
    assert r.json()["recent_application"]["applied_by"] == doctor


def test_preview_names_the_patient_the_gates_ran_for(patient, doctor):
    """The screen names the patient on Apply from THIS block, so it must be the
    patient the verdicts were computed for, with the allergies the interlock read."""
    with db_session() as db:
        db.execute("UPDATE patients SET bed = 'OS-7', ward = 'GM-1' WHERE id = ?", (patient,))
    _chart_allergy(patient, "penicillin")
    preview = order_sets_service.preview_set(patient, "sepsis_six", actor_id=doctor)
    banner = preview["patient"]
    assert banner["id"] == patient
    assert banner["name"] == "Order Set Test"
    assert banner["bed"] == "OS-7"
    assert banner["mrn"].startswith("MRN-OSET-")
    assert banner["allergies"] == [
        {"substance": "penicillin", "reaction": "anaphylaxis", "severity": "severe"}]


def test_an_unreadable_allergy_list_is_none_not_empty(patient, doctor, monkeypatch):
    """Empty would read as 'no allergies'; None tells the screen it could not look."""
    from services import allergy_interlock

    def broken(db, pid):
        raise RuntimeError("file is not a database")

    monkeypatch.setattr(allergy_interlock, "read_active_allergies", broken)
    assert order_sets_service.patient_banner(patient)["allergies"] is None


def test_preview_never_consumes_an_override(patient, doctor):
    """A preview that quietly banked the override would mean the clinician's one
    deliberate decision was spent looking at a screen."""
    _chart_allergy(patient, "ceftriaxone")
    definition = order_sets_service.get_set("sepsis_six")
    ceftriaxone = next(i for i in definition["items"] if i["drug_name"] == "Ceftriaxone")
    result = order_sets_service.preview_set(
        patient, "sepsis_six", actor_id=doctor,
        selections=[{"index": ceftriaxone["index"], "safety_ack": {
            "acknowledged": True, "reason": "Trying to spend this in a preview."}}],
    )
    assert result["would_apply"] is False
    assert result["blocked"], "the preview must still show the severe alert"
    with db_session() as db:
        overrides = db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE patient_id = ? "
            "AND action_type = 'drug_safety_override'", (patient,)).fetchone()["n"]
    assert overrides == 0


# ═════════════════════════════════════════════════════════════════════════════
#  6. EDITABLE BEFORE COMMIT
# ═════════════════════════════════════════════════════════════════════════════

def test_excluding_an_item_leaves_it_unordered(patient, doctor):
    definition = order_sets_service.get_set("acute_asthma")
    result = order_sets_service.apply_set(
        patient, "acute_asthma", actor_id=doctor, actor_role="doctor",
        selections=[{"index": 0, "include": False}],
    )
    assert len(result["orders"]) == len(definition["items"]) - 1
    assert definition["items"][0]["description"] not in {
        o["description"] for o in result["orders"]}


def test_an_edited_dose_is_what_gets_ordered_and_what_got_screened(patient, doctor):
    definition = order_sets_service.get_set("post_operative")
    paracetamol = next(i for i in definition["items"] if i["drug_name"] == "Paracetamol")
    result = order_sets_service.apply_set(
        patient, "post_operative", actor_id=doctor, actor_role="doctor",
        selections=[{"index": paracetamol["index"], "dose": "500 mg",
                     "priority": "urgent"}],
    )
    verdict = next(v for v in result["items"] if v["drug_name"] == "Paracetamol")
    assert verdict["dose"] == "500 mg"
    assert verdict["dose_check"]["dose"] == "500 mg"
    assert sorted(verdict["edited_fields"]) == ["dose", "priority"]

    order = next(o for o in result["orders"] if o["id"] == verdict["order_id"])
    assert json.loads(order["detail_json"])["medication"]["dose"] == "500 mg"
    assert order["priority"] == "urgent"


def test_excluding_everything_is_refused(patient, doctor):
    definition = order_sets_service.get_set("post_operative")
    with pytest.raises(order_sets_service.OrderSetError) as e:
        order_sets_service.apply_set(
            patient, "post_operative", actor_id=doctor, actor_role="doctor",
            selections=[{"index": i["index"], "include": False} for i in definition["items"]],
        )
    assert "nothing to order" in str(e.value)
    assert order_sets_service.list_applications(patient) == []


def test_a_selection_naming_an_item_that_does_not_exist_is_refused(patient, doctor):
    with pytest.raises(order_sets_service.OrderSetError):
        order_sets_service.apply_set(patient, "post_operative", actor_id=doctor,
                                     selections=[{"index": 999}])


# ═════════════════════════════════════════════════════════════════════════════
#  7. THE HTTP SURFACE — permissions, ward scope, audit
# ═════════════════════════════════════════════════════════════════════════════

def test_the_protocol_list_needs_order_read_and_carries_no_phi(doctor):
    response = client.get("/api/orders/sets", headers=_auth(doctor, "doctor"))
    assert response.status_code == 200
    body = response.json()
    assert {s["id"] for s in body["sets"]} >= {"sepsis_six", "dka_initial"}
    assert body["config"]["path"].endswith("order_sets.json")


def test_a_pharmacist_cannot_apply_a_protocol(patient):
    pharmacist = _mk_user("pharmacist", DEPT)
    response = client.post("/api/orders/sets/sepsis_six/apply",
                           headers=_auth(pharmacist, "pharmacist"),
                           json={"patient_id": patient})
    assert response.status_code == 403
    assert _orders_of(patient) == []


def test_a_patient_account_cannot_apply_a_protocol(patient):
    account = _mk_user("patient", None)
    response = client.post("/api/orders/sets/sepsis_six/apply",
                           headers=_auth(account, "patient"),
                           json={"patient_id": patient})
    assert response.status_code == 403
    assert _orders_of(patient) == []


def test_applying_to_another_wards_patient_404s_and_writes_nothing(
        patient, other_ward_doctor):
    """SCOPING LAW: the patient is named in the BODY, which is the spelling the
    path-shaped guard cannot see. 404, never 403 — a 403 would confirm the
    patient exists."""
    response = client.post("/api/orders/sets/sepsis_six/apply",
                           headers=_auth(other_ward_doctor, "doctor"),
                           json={"patient_id": patient})
    assert response.status_code == 404
    assert _orders_of(patient) == []


def test_previewing_another_wards_patient_404s(patient, other_ward_doctor):
    response = client.post("/api/orders/sets/sepsis_six/preview",
                           headers=_auth(other_ward_doctor, "doctor"),
                           json={"patient_id": patient})
    assert response.status_code == 404


def test_a_preview_leaves_a_phi_access_row(patient, doctor):
    client.post("/api/orders/sets/sepsis_six/preview", headers=_auth(doctor, "doctor"),
                json={"patient_id": patient})
    with db_session() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE patient_id = ? "
            "AND event_category = 'phi_access' AND action_type = 'order_set_previewed'",
            (patient,)).fetchone()
    assert row["n"] >= 1


def test_applying_a_set_over_http_returns_the_orders_and_the_application(patient, doctor):
    response = client.post("/api/orders/sets/sepsis_six/apply",
                           headers=_auth(doctor, "doctor"),
                           json={"patient_id": patient})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["application_id"]
    assert len(body["orders"]) == len(order_sets_service.get_set("sepsis_six")["items"])
    assert body["set"]["guideline"]["citation"]


def test_an_unknown_set_id_is_a_404(doctor, patient):
    hdr = _auth(doctor, "doctor")
    assert client.get("/api/orders/sets/not_a_protocol",
                      headers=hdr).status_code == 404
    assert client.post("/api/orders/sets/not_a_protocol/apply", headers=hdr,
                       json={"patient_id": patient}).status_code == 404
    assert client.post("/api/orders/sets/not_a_protocol/preview", headers=hdr,
                       json={"patient_id": patient}).status_code == 404
    assert _orders_of(patient) == []


def test_the_sets_path_is_not_shadowed_by_the_order_id_route(doctor):
    """`/api/orders/sets` must not be read as `/api/orders/{order_id}`."""
    response = client.get("/api/orders/sets", headers=_auth(doctor, "doctor"))
    assert response.status_code == 200
    assert "sets" in response.json()


def test_applications_are_listed_per_patient_and_ward_scoped(patient, doctor, other_ward_doctor):
    order_sets_service.apply_set(patient, "sepsis_six", actor_id=doctor, actor_role="doctor")
    mine = client.get(f"/api/orders/sets/applications/{patient}",
                      headers=_auth(doctor, "doctor"))
    theirs = client.get(f"/api/orders/sets/applications/{patient}",
                        headers=_auth(other_ward_doctor, "doctor"))
    assert mine.status_code == 200
    assert len(mine.json()["applications"]) == 1
    assert theirs.status_code == 404
