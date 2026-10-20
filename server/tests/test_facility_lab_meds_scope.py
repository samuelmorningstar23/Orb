"""Facility scope in the laboratory and medication-order lanes, and the
facility a sample is performed at (multisite-8h2, W13-D, 20 October 2026).

The module owns its database (isolated_module_database), so the second
facility never reaches the session database. It plants facility 1 and a
second facility (id 9881), each with its own department, inpatient, case room,
laboratory staff and ward staff, runs each patient's laboratory work through
the real routes (orders taken in, labelled, collected, received, resulted,
validated, authorised, a critical raised, a sample left on the bench, one in
transit, one waiting for collection and one unmapped code) and checks that:

  * every new sample is stamped with the facility that performs it, a
    recollection keeps its sample's facility, and a planned tube never takes
    another facility's tests;
  * a laboratory account bound to one facility sees only its own facility's
    laboratory work on every list (collection, receiving, worklist, section
    counts, validation, criticals, reports, turnaround, exceptions), and a
    ward account only its own patients;
  * every step on another facility's sample, result, order or critical is the
    same 404 as an unknown one, writes nothing, and leaves an authorization
    patient_scope_denied row with scope_cause 'facility';
  * a printed report names the hospital whose laboratory performed it;
  * a released report is part of the patient's chart (MS-R2 (a)): the
    laboratory opens and prints one when either the performing facility or
    the patient is in its scope, so a report listed for a moved patient
    opens, and only a report outside both is refused;
  * an unmatched result is never attached to a patient at another facility;
  * the medication-order routes check the facility first, the to-sign queue
    keeps to the caller's facilities, and the appliance-wide parity report and
    adoption need group-wide access;
  * with one facility on record every lane reads exactly as before.

The route groups stay behind the facility fence until the wave-level fence
regeneration, so the HTTP checks open the fence (FACILITY_BLIND_SURFACES = ())
to show what each lane does once it opens. Test patients carry invented names
(never a word a clinician types). Run with the test env, never the shipping
.env.
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from collections.abc import Iterator
from typing import Any

import config
import facility_fence as F
import main
import pytest
from auth import create_token
from database import db_session
from fastapi.testclient import TestClient
from services.clinical_time import utc_iso_now
from services.hospital_time import hospital_today

from services import case_room_alerts, lab_facility, orders_service, terminology_service
from services import facility_service as FS
from services import lab_master_service as lm
from services import lab_order_service as los
from services import lab_tat_service as tat
from services import result_quarantine_service as rq

pytestmark = pytest.mark.usefixtures("isolated_module_database")

client = TestClient(main.app, raise_server_exceptions=False)

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9881  # an id no other module writes
F2_CODE = f"LM{TAG[:4]}"
F2_NAME = "Vantrel Satellite"
F1_DEPT = f"Quorlith Medicine {TAG}"
F2_DEPT = f"Vantrel Medicine {TAG}"
CODE = f"LMS{TAG}-K"

P1_NAME = "Orsolya Quandtveil"
P2_NAME = "Brennika Thalvorne"
#: Admitted at facility 2, then discharged and seen in a facility 1 clinic.
P3_NAME = "Ilvanne Corbrecht"
#: Reported at facility 2, then discharged and seen in a facility 1 clinic.
P4_NAME = "Thessaly Varnquist"

#: The accounts of the world: key -> (role, department, home facility).
STAFF = {
    "tech_f1": ("lab_technician", None, None),
    "tech_f2": ("lab_technician", None, F2),
    "path_f1": ("pathologist", None, None),
    "path_f2": ("pathologist", None, F2),
    "phleb_f2": ("phlebotomist", None, F2),
    "nurse_f1": ("nurse", F1_DEPT, None),
    "nurse_f2": ("nurse", F2_DEPT, None),
    "doctor_f1": ("doctor", F1_DEPT, None),
    "doctor_f2": ("doctor", F2_DEPT, None),
    "admin_f2": ("admin", None, F2),
    "admin_group": ("admin", None, None),  # holds an enterprise grant below
    "nurse_lost": ("nurse", f"Nowhere Medicine {TAG}", None),
}


# ── the world ────────────────────────────────────────────────────────────────


def _hdr(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


def _expected(result: dict) -> dict:
    return {
        "value_text": result["value_text"],
        "comment": result.get("comment"),
        "abnormal_flag": result.get("abnormal_flag"),
    }


def _ok(resp: Any) -> dict:
    assert resp.status_code == 200, resp.text
    return resp.json()


def _patient(db: Any, pid: str, name: str, mrn: str, department: str, facility: int) -> None:
    db.execute(
        "INSERT INTO patients (id, name, mrn, age, sex, status, ward, bed, date_of_birth, "
        "registered_facility_id, created_at) VALUES (?, ?, ?, 49, 'female', 'admitted', ?, "
        "'B-4', '1977-05-14', ?, ?)",
        (pid, name, mrn, f"W{TAG}{facility}", facility, utc_iso_now()),
    )
    db.execute(
        "INSERT INTO admissions (id, patient_id, admitted_by, department, status, facility_id, "
        "created_at) VALUES (?, ?, 'system', ?, 'active', ?, ?)",
        (f"LMAD{pid}", pid, department, facility, utc_iso_now()),
    )
    db.execute(
        "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
        (f"lm-case-{pid.lower()}", f"{name} case room", pid),
    )


def _items(order_id: str) -> list[dict]:
    with db_session() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT * FROM lab_order_items WHERE order_id = ? ORDER BY id", (order_id,)
            ).fetchall()
        ]


def _specimen(specimen_id: str) -> dict:
    with db_session() as db:
        return dict(
            db.execute("SELECT * FROM lab_specimens WHERE id = ?", (specimen_id,)).fetchone()
        )


def _order(patient: dict, doctor: dict, *, code: str = CODE, system: str = "LOCAL") -> dict:
    order = orders_service.create_order(
        patient_id=patient["id"],
        order_type="lab",
        description=f"{system} {code}",
        code=code,
        code_system=system,
        priority="stat",
        ordered_by=doctor["id"],
        status="active",
    )
    return {"order": order, "items": _items(order["id"])}


def _label_and_collect(patient: dict, specimen_id: str, collector: dict) -> None:
    identity = {"uhid": patient["mrn"], "name": patient["name"]}
    _ok(
        client.post(
            "/api/lab/collection/labels",
            json={"specimen_ids": [specimen_id], "identity": identity},
            headers=_hdr(collector),
        )
    )
    _ok(
        client.post(
            f"/api/lab/specimens/{specimen_id}/collect",
            json={"identity": identity},
            headers=_hdr(collector),
        )
    )


def _work(patient: dict, staff: dict) -> dict:
    """One patient's laboratory work, run through the routes by that
    facility's own staff. Returns the ids each test needs."""
    doctor, collector, tech, path = (
        staff["doctor"],
        staff["collector"],
        staff["tech"],
        staff["path"],
    )
    out: dict = {}
    # Reported, with a critical raised at technical validation.
    done = _order(patient, doctor)
    sid = done["items"][0]["specimen_id"]
    _label_and_collect(patient, sid, collector)
    _ok(client.post(f"/api/lab/specimens/{sid}/receive", json={}, headers=_hdr(tech)))
    entered = _ok(
        client.post(
            "/api/lab/results",
            json={"order_item_id": done["items"][0]["id"], "value": "7.0"},
            headers=_hdr(tech),
        )
    )["result"]
    _ok(
        client.post(
            "/api/lab/results/tech-validate",
            json={"results": [{"result_id": entered["id"], "expected": _expected(entered)}]},
            headers=_hdr(tech),
        )
    )
    with db_session() as db:
        validated = dict(
            db.execute("SELECT * FROM lab_results WHERE id = ?", (entered["id"],)).fetchone()
        )
    _ok(
        client.post(
            "/api/lab/results/authorise",
            json={
                "specimen_id": sid,
                "results": [{"result_id": entered["id"], "expected": _expected(validated)}],
            },
            headers=_hdr(path),
        )
    )
    out.update(done_order=done["order"]["id"], done_specimen=sid, done_result=entered["id"])
    with db_session() as db:
        note = db.execute(
            "SELECT id FROM lab_critical_notifications WHERE patient_id = ?", (patient["id"],)
        ).fetchone()
    out["critical"] = note[0]
    # On the bench: received, a result entered and not yet validated.
    bench = _order(patient, doctor)
    bsid = bench["items"][0]["specimen_id"]
    _label_and_collect(patient, bsid, collector)
    _ok(client.post(f"/api/lab/specimens/{bsid}/receive", json={}, headers=_hdr(tech)))
    bench_result = _ok(
        client.post(
            "/api/lab/results",
            json={"order_item_id": bench["items"][0]["id"], "value": "4.0"},
            headers=_hdr(tech),
        )
    )["result"]
    out.update(
        bench_specimen=bsid, bench_item=bench["items"][0]["id"], bench_result=bench_result["id"]
    )
    # In transit: collected on the ward, not yet received.
    transit = _order(patient, doctor)
    tsid = transit["items"][0]["specimen_id"]
    _label_and_collect(patient, tsid, collector)
    out["transit_specimen"] = tsid
    # Waiting for collection.
    planned = _order(patient, doctor)
    out.update(
        planned_order=planned["order"]["id"], planned_specimen=planned["items"][0]["specimen_id"]
    )
    # A code the laboratory catalogue lacks.
    unmapped = _order(patient, doctor, code=staff["unmapped"][1], system=staff["unmapped"][0])
    out["unmapped_order"] = unmapped["order"]["id"]
    return out


def _unmapped_code() -> tuple[str, str]:
    with db_session() as db:
        row = db.execute(
            "SELECT t.code_system, t.code FROM terminology t WHERE t.is_active = 1 "
            "AND t.code_system = 'SNOMED' AND NOT EXISTS (SELECT 1 FROM lab_tests l "
            "WHERE l.code_system = t.code_system AND l.code = t.code) LIMIT 1"
        ).fetchone()
    assert row is not None
    return row[0], row[1]


@contextlib.contextmanager
def _lanes(mp: pytest.MonkeyPatch) -> Iterator[None]:
    """The laboratory and the medication-order store on, and the fence open
    as it will be once the wave-level regeneration lands."""
    mp.setattr(config, "LAB_ENABLED", True)
    mp.setattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", True)
    mp.setattr(config, "MED_ORDER_STORE_ENABLED", True)
    mp.setattr(config, "MED_ORDER_LIFECYCLE_ENABLED", True)
    mp.setattr(F, "FACILITY_BLIND_SURFACES", ())
    mp.setattr(case_room_alerts, "push_message_new", lambda *a, **k: True)
    from ws import realtime

    mp.setattr(realtime, "schedule_patient_vitals", lambda *a, **k: True)
    yield


@pytest.fixture(scope="module")
def world(isolated_module_database: Any) -> Iterator[dict]:
    terminology_service.seed_terminology()
    lm.load_starter_catalogue()
    users = {
        key: {"id": f"LMU{n:02d}{TAG}", "role": role, "department": dept}
        for n, (key, (role, dept, _home)) in enumerate(STAFF.items())
    }
    pats = {
        "p1": {"id": f"LMP1{TAG}", "name": P1_NAME, "mrn": f"LMQ1{TAG}"},
        "p2": {"id": f"LMP2{TAG}", "name": P2_NAME, "mrn": f"LMQ2{TAG}"},
        "p3": {"id": f"LMP3{TAG}", "name": P3_NAME, "mrn": f"LMQ3{TAG}"},
    }
    with db_session() as db:
        db.execute(
            "INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
            (F2, str(uuid.uuid4()), F2_CODE, F2_NAME),
        )
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
            (F1_DEPT,),
        )
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
            (F2_DEPT, F2),
        )
        for key, (role, dept, home_at) in STAFF.items():
            db.execute(
                "INSERT INTO users (id, name, role, department, password_hash, is_active) "
                "VALUES (?, ?, ?, ?, 'x', 1)",
                (users[key]["id"], f"Lmscope {role} {key}", role, dept),
            )
            if home_at is not None:
                db.execute(
                    "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                    "granted_at, reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, "
                    "'facility scope test')",
                    (users[key]["id"], home_at),
                )
        db.execute(
            "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
            "VALUES (?, 'system', CURRENT_TIMESTAMP, 'facility scope test')",
            (users["admin_group"]["id"],),
        )
        _patient(db, pats["p1"]["id"], P1_NAME, pats["p1"]["mrn"], F1_DEPT, 1)
        _patient(db, pats["p2"]["id"], P2_NAME, pats["p2"]["mrn"], F2_DEPT, F2)
        _patient(db, pats["p3"]["id"], P3_NAME, pats["p3"]["mrn"], F2_DEPT, F2)
        db.execute(
            "INSERT INTO lab_module_events (event, occurred_at, detail) VALUES ('enabled', ?, ?)",
            (utc_iso_now(), "facility scope test cutover"),
        )
    master = {"id": users["path_f1"]["id"], "role": "pathologist"}
    lm.create_test(
        {
            "code_system": "LOCAL",
            "code": CODE,
            "kind": "analyte",
            "name": "Lmscope potassium",
            "short_name": "Lm K",
            "decimal_places": 1,
            "unit": "mmol/L",
            "result_type": "numeric",
            "section_id": "biochemistry",
            "specimen_type_code": "serum",
        },
        actor=master,
    )
    lm.approve_critical_limit(
        lm.test_id_for("LOCAL", CODE),
        {
            "critical_low": "2.5",
            "critical_high": "6.5",
            "agreed_with": "Dr Quillon, physician lead",
            "method_note": "Facility scope test limit",
        },
        actor=master,
    )
    FS.invalidate_cache()
    F._audit_last.clear()
    assert FS.multi_facility_data() is True
    unmapped = _unmapped_code()
    with pytest.MonkeyPatch.context() as mp, _lanes(mp):
        work = {
            "p1": _work(
                pats["p1"],
                {
                    "doctor": users["doctor_f1"],
                    "collector": users["nurse_f1"],
                    "tech": users["tech_f1"],
                    "path": users["path_f1"],
                    "unmapped": unmapped,
                },
            ),
            "p2": _work(
                pats["p2"],
                {
                    "doctor": users["doctor_f2"],
                    "collector": users["phleb_f2"],
                    "tech": users["tech_f2"],
                    "path": users["path_f2"],
                    "unmapped": unmapped,
                },
            ),
        }
        # P3: a sample collected while admitted at facility 2 (performed
        # there), then the stay ends and the patient is seen in a facility 1
        # clinic, so the patient's latest encounter is at facility 1.
        moved = _order(pats["p3"], users["doctor_f2"])
        moved_sid = moved["items"][0]["specimen_id"]
        _label_and_collect(pats["p3"], moved_sid, users["phleb_f2"])
        from services.medorders import chart_lane

        moved_draft = chart_lane.create_order(
            patient_id=pats["p3"]["id"],
            drug_name="Paracetamol",
            status="draft",
            actor_id=users["doctor_f2"]["id"],
            actor_role="doctor",
            dose="500 mg",
            route="Oral",
            frequency="TDS",
        )["order"]
        work["p3"] = {"transit_specimen": moved_sid, "draft": moved_draft["id"]}
    with db_session() as db:
        db.execute(
            "UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (pats["p3"]["id"],)
        )
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (pats["p3"]["id"],))
        db.execute(
            "INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
            "status, registered_by, registered_at, facility_id) VALUES (?, ?, ?, ?, 7, "
            "'waiting', 'system', datetime('now', '+1 minute'), 1)",
            (f"LMV3{TAG}", pats["p3"]["id"], F1_DEPT, hospital_today().isoformat()),
        )
    try:
        yield {"users": users, "patients": pats, "work": work}
    finally:
        FS.invalidate_cache()
        F._audit_last.clear()


@pytest.fixture()
def lanes_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with _lanes(monkeypatch):
        yield


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


@contextlib.contextmanager
def _one_facility() -> Iterator[None]:
    """Facility 2 taken off the record for the block (no row refers to it by
    key), so the lanes see one facility, as every install does today."""
    with db_session() as db:
        row = dict(db.execute("SELECT * FROM facilities WHERE id = ?", (F2,)).fetchone())
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        yield
    finally:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with db_session() as db:
            db.execute(f"INSERT INTO facilities ({cols}) VALUES ({marks})", tuple(row.values()))
        FS.invalidate_cache()


def _denials(actor_id: str, helper: str) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'patient_scope_denied' "
            "AND actor_user_id = ? ORDER BY id",
            (actor_id,),
        ).fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        if meta.get("scope_helper") == helper:
            out.append({**dict(r), "meta": meta})
    return out


def _assert_facility_denial(actor: dict, helper: str, patient_id: str, before: int) -> None:
    rows = _denials(actor["id"], helper)
    assert len(rows) == before + 1, rows
    row = rows[-1]
    assert row["patient_id"] == patient_id
    assert row["event_category"] == "authorization" and row["outcome"] == "denied"
    assert row["meta"]["scope_cause"] == "facility"
    assert row["meta"]["facility_scope"] == sorted(FS.facility_scope_for(actor) or [])


def _ids_in(body: Any) -> str:
    return json.dumps(body, default=str)


# ═════════════════════════════════════════════════════════════════════════════
#  1. The performing facility
# ═════════════════════════════════════════════════════════════════════════════


def test_the_world_is_what_it_says(world: dict) -> None:
    users = world["users"]
    assert lab_facility.scope_of(users["tech_f1"]) == frozenset({1})
    assert lab_facility.scope_of(users["tech_f2"]) == frozenset({F2})
    assert lab_facility.scope_of(users["admin_group"]) is None
    assert lab_facility.scope_of(users["nurse_lost"]) == frozenset()


def test_every_sample_is_stamped_with_the_facility_that_performs_it(world: dict) -> None:
    with db_session() as db:
        by_patient = {
            pid: {
                r[0]
                for r in db.execute(
                    "SELECT facility_id FROM lab_specimens WHERE patient_id = ?", (pid,)
                ).fetchall()
            }
            for pid in (world["patients"]["p1"]["id"], world["patients"]["p2"]["id"])
        }
    assert by_patient[world["patients"]["p1"]["id"]] == {1}
    assert by_patient[world["patients"]["p2"]["id"]] == {F2}


def test_a_planned_tube_takes_tests_only_from_its_own_facility(world: dict) -> None:
    tube = _specimen(world["work"]["p2"]["planned_specimen"])
    other_test = [lm.test_id_for("LOINC", "2823-3")]
    with db_session() as db:
        args = (tube["patient_id"], tube["specimen_type_code"], tube["container"], "stat")
        assert los._mergeable_specimen(db, *args, other_test, facility_id=F2) == tube["id"]
        assert los._mergeable_specimen(db, *args, other_test, facility_id=1) is None


def test_a_recollection_keeps_the_rejected_samples_facility(world: dict, lanes_on: None) -> None:
    users, p2 = world["users"], world["patients"]["p2"]
    order = _order(p2, users["doctor_f2"])
    sid = order["items"][0]["specimen_id"]
    _label_and_collect(p2, sid, users["phleb_f2"])
    _ok(
        client.post(
            f"/api/lab/specimens/{sid}/reject",
            json={"reason_code": "haemolysed"},
            headers=_hdr(users["tech_f2"]),
        )
    )
    answer = _ok(
        client.post(f"/api/lab/specimens/{sid}/recollect", json={}, headers=_hdr(users["tech_f2"]))
    )
    new_id = answer["specimen"]["id"]
    assert new_id != sid
    assert _specimen(new_id)["facility_id"] == F2
    assert _specimen(new_id)["recollect_of"] == sid


def test_with_one_facility_a_sample_is_stamped_at_facility_1(world: dict) -> None:
    with _one_facility():
        with db_session() as db:
            assert lab_facility.performing_facility_on(db, world["patients"]["p2"]["id"]) == 1
            assert lab_facility.copied_facility_on(db, world["work"]["p2"]["done_specimen"]) == 1
            assert lab_facility.merge_clause(db, F2) == ("", ())


# ═════════════════════════════════════════════════════════════════════════════
#  2. The laboratory's lists keep to the caller's facilities
# ═════════════════════════════════════════════════════════════════════════════


def _patients_on(rows: list[dict]) -> set[str]:
    return {r["patient"]["id"] for r in rows}


def test_the_collection_list_keeps_to_the_callers_facilities(world: dict, lanes_on: None) -> None:
    users, p1, p2 = world["users"], world["patients"]["p1"]["id"], world["patients"]["p2"]["id"]
    phleb_f2 = _ok(client.get("/api/lab/collection", headers=_hdr(users["phleb_f2"])))
    tech_f1 = _ok(client.get("/api/lab/collection", headers=_hdr(users["tech_f1"])))
    nurse_f1 = _ok(client.get("/api/lab/collection", headers=_hdr(users["nurse_f1"])))
    assert _patients_on(phleb_f2["specimens"]) == {p2}
    assert _patients_on(tech_f1["specimens"]) == {p1}
    assert _patients_on(nurse_f1["specimens"]) == {p1}
    lost = _ok(client.get("/api/lab/collection", headers=_hdr(users["nurse_lost"])))
    assert lost["specimens"] == []


def test_the_receiving_lists_keep_to_the_callers_facilities(world: dict, lanes_on: None) -> None:
    users = world["users"]
    lists = _ok(client.get("/api/lab/receiving", headers=_hdr(users["tech_f2"])))
    in_transit = {s["id"] for s in lists["in_transit"]}
    assert world["work"]["p2"]["transit_specimen"] in in_transit
    assert world["work"]["p1"]["transit_specimen"] not in in_transit
    assert P1_NAME not in _ids_in(lists)


def test_a_sample_stays_with_the_laboratory_that_performs_it_when_its_patient_moves(
    world: dict, lanes_on: None
) -> None:
    users, p3 = world["users"], world["patients"]["p3"]
    sid = world["work"]["p3"]["transit_specimen"]
    with db_session() as db:
        assert lab_facility.specimen_facility_on(db, sid) == F2
        assert lab_facility.performing_facility_on(db, p3["id"]) == 1

    def in_transit(user: dict) -> set[str]:
        lists = _ok(client.get("/api/lab/receiving", headers=_hdr(user)))
        return {s["id"] for s in lists["in_transit"]}

    # The facility 2 laboratory holds the tube and finishes it.
    assert sid in in_transit(users["tech_f2"])
    assert sid not in in_transit(users["tech_f1"])
    scanned = _ok(client.get(f"/api/lab/specimens/scan/{sid}", headers=_hdr(users["tech_f2"])))
    assert scanned["specimen"]["id"] == sid
    # A ward keeps to its own patients: the last ward's facility no longer
    # holds the patient, so the sample is not on that ward's list.
    with db_session() as db:
        ward = los.list_receiving(db, dept=F2_DEPT, facilities=frozenset({F2}))
        before = los.list_receiving(db, dept=F2_DEPT)
    assert sid not in {s["id"] for s in ward["in_transit"]}
    assert sid in {s["id"] for s in before["in_transit"]}


def test_the_bench_and_its_counts_keep_to_the_callers_facilities(
    world: dict, lanes_on: None
) -> None:
    users, work = world["users"], world["work"]
    bench = _ok(client.get("/api/lab/worklists/biochemistry", headers=_hdr(users["tech_f2"])))
    on_bench = {i["specimen_id"] for i in bench["items"]}
    assert work["p2"]["bench_specimen"] in on_bench
    assert work["p1"]["bench_specimen"] not in on_bench

    def entered(user: dict) -> int:
        sections = _ok(client.get("/api/lab/worklists", headers=_hdr(user)))["sections"]
        return next(s["entered"] for s in sections if s["id"] == "biochemistry")

    f1, f2 = entered(users["tech_f1"]), entered(users["tech_f2"])
    assert f1 == 1 and f2 == 1
    with _one_facility():
        assert entered(users["tech_f2"]) == f1 + f2


def test_the_validation_queues_keep_to_the_callers_facilities(world: dict, lanes_on: None) -> None:
    users = world["users"]
    queues = _ok(client.get("/api/lab/validation", headers=_hdr(users["tech_f2"])))
    patients = {g["patient"]["id"] for g in queues["technical"] + queues["authorisation"]}
    assert patients == {world["patients"]["p2"]["id"]}


def test_the_critical_lists_keep_to_the_callers_facilities(world: dict, lanes_on: None) -> None:
    users, work = world["users"], world["work"]
    screen = _ok(client.get("/api/lab/criticals", headers=_hdr(users["tech_f2"])))
    shown = {n["id"] for n in screen["open"] + screen["recent"]}
    assert work["p2"]["critical"] in shown and work["p1"]["critical"] not in shown
    card_f1 = _ok(client.get("/api/lab/criticals/ward", headers=_hdr(users["nurse_f1"])))
    card_f2 = _ok(client.get("/api/lab/criticals/ward", headers=_hdr(users["nurse_f2"])))
    assert {n["id"] for n in card_f1["open"]} == {work["p1"]["critical"]}
    assert {n["id"] for n in card_f2["open"]} == {work["p2"]["critical"]}


def test_the_report_list_keeps_to_the_callers_facilities(world: dict, lanes_on: None) -> None:
    users, pats = world["users"], world["patients"]
    day = hospital_today().isoformat()
    listed = _ok(client.get(f"/api/lab/reports?day={day}", headers=_hdr(users["tech_f2"])))
    assert {r["patient"]["id"] for r in listed["reports"]} == {pats["p2"]["id"]}
    by_uhid = _ok(
        client.get(f"/api/lab/reports?uhid={pats['p1']['mrn']}", headers=_hdr(users["tech_f2"]))
    )
    assert by_uhid["reports"] == []
    own = _ok(
        client.get(f"/api/lab/reports?uhid={pats['p1']['mrn']}", headers=_hdr(users["tech_f1"]))
    )
    assert {r["patient"]["id"] for r in own["reports"]} == {pats["p1"]["id"]}


def test_the_turnaround_figures_keep_to_the_callers_facilities(world: dict, lanes_on: None) -> None:
    users, pats = world["users"], world["patients"]
    first = last = hospital_today()
    with db_session() as db:
        start, end = tat.period_bounds(first, last)
        both = {r["patient_id"] for r in tat.reported_tests(db, start, end)}
        f2 = {r["patient_id"] for r in tat.reported_tests(db, start, end, facilities={F2})}
        crit = {r["patient_id"] for r in tat.critical_times(db, start, end, facilities={F2})}
        none = tat.reported_tests(db, start, end, facilities=frozenset())
    assert both == {pats["p1"]["id"], pats["p2"]["id"]}
    assert f2 == {pats["p2"]["id"]} and crit == {pats["p2"]["id"]}
    assert none == []
    board = _ok(client.get("/api/lab/tat", headers=_hdr(users["tech_f2"])))
    assert board["overall"]["count"] == 1
    assert board["exceptions"]["open"] == 1
    assert P1_NAME not in _ids_in(board)


def test_the_exceptions_list_keeps_to_the_callers_facilities(world: dict, lanes_on: None) -> None:
    users, work = world["users"], world["work"]
    rows = _ok(client.get("/api/lab/exceptions", headers=_hdr(users["tech_f2"])))["exceptions"]
    orders = {r["order_id"] for r in rows}
    assert work["p2"]["unmapped_order"] in orders and work["p1"]["unmapped_order"] not in orders


def test_with_one_facility_every_list_reads_as_before(world: dict, lanes_on: None) -> None:
    users, pats = world["users"], world["patients"]
    both = {pats["p1"]["id"], pats["p2"]["id"]}
    with _one_facility():
        assert lab_facility.scope_of(users["tech_f2"]) is None
        collection = _ok(client.get("/api/lab/collection", headers=_hdr(users["phleb_f2"])))
        assert _patients_on(collection["specimens"]) == both
        screen = _ok(client.get("/api/lab/criticals", headers=_hdr(users["tech_f2"])))
        assert {n["patient"]["id"] for n in screen["open"] + screen["recent"]} == both
        rows = _ok(client.get("/api/lab/exceptions", headers=_hdr(users["tech_f2"])))
        assert {r["patient_id"] for r in rows["exceptions"]} == both
        with db_session() as db:
            assert lab_facility.list_clause(
                dept=None, facilities=None, patient_col="x", facility_sql="y"
            ) == ("", ())
            assert los.list_open_exceptions(db, dept=None) == los.list_open_exceptions(
                db, dept=None, facilities=None
            )


# ═════════════════════════════════════════════════════════════════════════════
#  3. A step on another facility's work is an audited 404
# ═════════════════════════════════════════════════════════════════════════════


def test_labels_for_another_facilitys_sample_are_an_audited_404(
    world: dict, lanes_on: None
) -> None:
    users, p1, work = world["users"], world["patients"]["p1"], world["work"]
    phleb = users["phleb_f2"]
    before = len(_denials(phleb["id"], "lab_specimen"))
    resp = client.post(
        "/api/lab/collection/labels",
        json={
            "specimen_ids": [work["p1"]["planned_specimen"]],
            "identity": {"uhid": p1["mrn"], "name": p1["name"]},
        },
        headers=_hdr(phleb),
    )
    assert resp.status_code == 404
    assert P1_NAME not in resp.text
    _assert_facility_denial(phleb, "lab_specimen", p1["id"], before)
    assert _specimen(work["p1"]["planned_specimen"])["label_print_count"] == 0


def test_receiving_another_facilitys_sample_is_an_audited_404(world: dict, lanes_on: None) -> None:
    users, p1, work = world["users"], world["patients"]["p1"], world["work"]
    tech = users["tech_f2"]
    sid = work["p1"]["transit_specimen"]
    before = len(_denials(tech["id"], "lab_specimen"))
    scan = client.get(f"/api/lab/specimens/scan/{sid}", headers=_hdr(tech))
    assert scan.status_code == 404 and P1_NAME not in scan.text
    _assert_facility_denial(tech, "lab_specimen", p1["id"], before)
    received = client.post(f"/api/lab/specimens/{sid}/receive", json={}, headers=_hdr(tech))
    assert received.status_code == 404
    _assert_facility_denial(tech, "lab_specimen", p1["id"], before + 1)
    assert _specimen(sid)["status"] == "collected"
    rejected = client.post(
        f"/api/lab/specimens/{sid}/reject",
        json={"reason_code": "haemolysed"},
        headers=_hdr(tech),
    )
    assert rejected.status_code == 404
    assert _specimen(sid)["status"] == "collected"


def test_a_result_on_another_facilitys_sample_is_an_audited_404(
    world: dict, lanes_on: None
) -> None:
    users, p1, work = world["users"], world["patients"]["p1"], world["work"]
    tech = users["tech_f2"]
    before = len(_denials(tech["id"], "lab_result"))
    with db_session() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM lab_results WHERE patient_id = ?", (p1["id"],)
        ).fetchone()[0]
    entered = client.post(
        "/api/lab/results",
        json={"order_item_id": work["p1"]["bench_item"], "value": "5.0"},
        headers=_hdr(tech),
    )
    assert entered.status_code == 404
    _assert_facility_denial(tech, "lab_result", p1["id"], before)
    with db_session() as db:
        result = dict(
            db.execute(
                "SELECT * FROM lab_results WHERE id = ?", (work["p1"]["bench_result"],)
            ).fetchone()
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM lab_results WHERE patient_id = ?", (p1["id"],)
            ).fetchone()[0]
            == count
        )
    validated = client.post(
        "/api/lab/results/tech-validate",
        json={"results": [{"result_id": result["id"], "expected": _expected(result)}]},
        headers=_hdr(tech),
    )
    assert validated.status_code == 404
    _assert_facility_denial(tech, "lab_result", p1["id"], before + 1)
    with db_session() as db:
        status = db.execute(
            "SELECT status FROM lab_results WHERE id = ?", (result["id"],)
        ).fetchone()[0]
    assert status == "entered"


def test_authorising_or_withdrawing_another_facilitys_result_is_an_audited_404(
    world: dict, lanes_on: None
) -> None:
    users, p1, work = world["users"], world["patients"]["p1"], world["work"]
    path = users["path_f2"]
    with db_session() as db:
        bench = dict(
            db.execute(
                "SELECT * FROM lab_results WHERE id = ?", (work["p1"]["bench_result"],)
            ).fetchone()
        )
        done = dict(
            db.execute(
                "SELECT * FROM lab_results WHERE id = ?", (work["p1"]["done_result"],)
            ).fetchone()
        )
    before = len(_denials(path["id"], "lab_result_release"))
    authorised = client.post(
        "/api/lab/results/authorise",
        json={
            "specimen_id": work["p1"]["bench_specimen"],
            "results": [{"result_id": bench["id"], "expected": _expected(bench)}],
        },
        headers=_hdr(path),
    )
    assert authorised.status_code == 404
    _assert_facility_denial(path, "lab_result_release", p1["id"], before)
    before = len(_denials(path["id"], "lab_result"))
    withdrawn = client.post(
        f"/api/lab/results/{done['id']}/withdraw",
        json={"reason": "Entered against the wrong sample", "expected": _expected(done)},
        headers=_hdr(path),
    )
    assert withdrawn.status_code == 404
    _assert_facility_denial(path, "lab_result", p1["id"], before)
    reported = client.get(
        f"/api/lab/results/reported/{work['p1']['done_specimen']}", headers=_hdr(path)
    )
    assert reported.status_code == 404 and P1_NAME not in reported.text
    with db_session() as db:
        assert (
            db.execute("SELECT status FROM lab_results WHERE id = ?", (done["id"],)).fetchone()[0]
            == "released"
        )


def test_a_callback_on_another_facilitys_critical_is_an_audited_404(
    world: dict, lanes_on: None
) -> None:
    users, p1, work = world["users"], world["patients"]["p1"], world["work"]
    tech, nurse = users["tech_f2"], users["nurse_f2"]
    nid = work["p1"]["critical"]
    before = len(_denials(tech["id"], "lab_critical"))
    resp = client.post(
        f"/api/lab/criticals/{nid}/callback",
        json={
            "recipient_name": "Sister Quenby Arvale",
            "recipient_role": "nurse",
            "method": "phone",
            "read_back": True,
            "value_told": "7.0 mmol/L",
        },
        headers=_hdr(tech),
    )
    assert resp.status_code == 404
    _assert_facility_denial(tech, "lab_critical", p1["id"], before)
    before = len(_denials(nurse["id"], "lab_critical"))
    ack = client.post(
        f"/api/lab/criticals/{nid}/acknowledge",
        json={"recipient_name": "Sister Quenby Arvale", "read_back": True, "value_told": "7.0"},
        headers=_hdr(nurse),
    )
    assert ack.status_code == 404
    _assert_facility_denial(nurse, "lab_critical", p1["id"], before)
    with db_session() as db:
        status = db.execute(
            "SELECT status FROM lab_critical_notifications WHERE id = ?", (nid,)
        ).fetchone()[0]
    assert status == "open"


def test_a_report_of_another_facilitys_sample_is_an_audited_404(
    world: dict, lanes_on: None
) -> None:
    users, p1, work = world["users"], world["patients"]["p1"], world["work"]
    tech = users["tech_f2"]
    accession = work["p1"]["done_specimen"]
    before = len(_denials(tech["id"], "lab_report"))
    resp = client.get(f"/api/lab/reports/{accession}", headers=_hdr(tech))
    assert resp.status_code == 404 and P1_NAME not in resp.text
    _assert_facility_denial(tech, "lab_report", p1["id"], before)
    with db_session() as db:
        # The facility refusal is not recorded a second time as a ward refusal.
        department_rows = db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'patient_scope_denied' "
            "AND actor_user_id = ? AND target_type = 'lab_report'",
            (tech["id"],),
        ).fetchone()[0]
    assert department_rows == 0


def test_cancelling_another_facilitys_order_is_an_audited_404(world: dict, lanes_on: None) -> None:
    users, p1, work = world["users"], world["patients"]["p1"], world["work"]
    tech = users["tech_f2"]
    before = len(_denials(tech["id"], "lab_order_cancel"))
    resp = client.post(
        f"/api/lab/orders/{work['p1']['planned_order']}/cancel",
        json={"reason": "Ordered on the wrong patient"},
        headers=_hdr(tech),
    )
    assert resp.status_code == 404
    _assert_facility_denial(tech, "lab_order_cancel", p1["id"], before)
    assert {i["status"] for i in _items(work["p1"]["planned_order"])} == {"awaiting_collection"}


def test_a_printed_report_names_the_hospital_that_performed_it(world: dict, lanes_on: None) -> None:
    users, work = world["users"], world["work"]
    own = _ok(
        client.get(
            f"/api/lab/reports/{work['p2']['done_specimen']}/print", headers=_hdr(users["tech_f2"])
        )
    )
    assert own["report"]["hospital"]["name"] == F2_NAME
    main_site = _ok(
        client.get(
            f"/api/lab/reports/{work['p1']['done_specimen']}/print", headers=_hdr(users["tech_f1"])
        )
    )
    assert main_site["report"]["hospital"]["name"] != F2_NAME


def test_with_one_facility_the_print_carries_the_site_name_as_before(
    world: dict, lanes_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from routes.lab import reports as report_routes

    monkeypatch.setattr(report_routes, "_site_name", lambda: "Quorlith General")
    with _one_facility():
        printed = _ok(
            client.get(
                f"/api/lab/reports/{world['work']['p2']['done_specimen']}/print",
                headers=_hdr(world["users"]["tech_f2"]),
            )
        )
    assert printed["report"]["hospital"]["name"] == "Quorlith General"


def _all_denials(actor_id: str) -> int:
    with db_session() as db:
        return int(
            db.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'patient_scope_denied' "
                "AND actor_user_id = ?",
                (actor_id,),
            ).fetchone()[0]
        )


def _reported_at_f2_then_moved(world: dict) -> dict:
    """A patient reported at facility 2 (performed there by its own staff),
    then discharged and seen in a facility 1 clinic, so the patient's latest
    encounter is at facility 1 while the sample stays facility 2's."""
    users = world["users"]
    p4 = {"id": f"LMP4{TAG}", "name": P4_NAME, "mrn": f"LMQ4{TAG}"}
    with db_session() as db:
        _patient(db, p4["id"], P4_NAME, p4["mrn"], F2_DEPT, F2)
    tech, path = users["tech_f2"], users["path_f2"]
    done = _order(p4, users["doctor_f2"])
    sid = done["items"][0]["specimen_id"]
    _label_and_collect(p4, sid, users["phleb_f2"])
    _ok(client.post(f"/api/lab/specimens/{sid}/receive", json={}, headers=_hdr(tech)))
    entered = _ok(
        client.post(
            "/api/lab/results",
            json={"order_item_id": done["items"][0]["id"], "value": "4.0"},
            headers=_hdr(tech),
        )
    )["result"]
    _ok(
        client.post(
            "/api/lab/results/tech-validate",
            json={"results": [{"result_id": entered["id"], "expected": _expected(entered)}]},
            headers=_hdr(tech),
        )
    )
    with db_session() as db:
        validated = dict(
            db.execute("SELECT * FROM lab_results WHERE id = ?", (entered["id"],)).fetchone()
        )
    _ok(
        client.post(
            "/api/lab/results/authorise",
            json={
                "specimen_id": sid,
                "results": [{"result_id": entered["id"], "expected": _expected(validated)}],
            },
            headers=_hdr(path),
        )
    )
    with db_session() as db:
        db.execute("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?", (p4["id"],))
        db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?", (p4["id"],))
        db.execute(
            "INSERT INTO opd_visits (id, patient_id, department, visit_date, token_number, "
            "status, registered_by, registered_at, facility_id) VALUES (?, ?, ?, ?, 8, "
            "'waiting', 'system', datetime('now', '+1 minute'), 1)",
            (f"LMV4{TAG}", p4["id"], F1_DEPT, hospital_today().isoformat()),
        )
        assert lab_facility.specimen_facility_on(db, sid) == F2
        assert lab_facility.performing_facility_on(db, p4["id"]) == 1
    FS.invalidate_cache()
    return {**p4, "accession": sid}


@pytest.fixture(scope="module")
def moved_report(world: dict) -> Iterator[dict]:
    with pytest.MonkeyPatch.context() as mp, _lanes(mp):
        made = _reported_at_f2_then_moved(world)
    yield made


def test_a_moved_patients_report_opens_and_prints_wherever_it_is_listed(
    world: dict, moved_report: dict, lanes_on: None
) -> None:
    """Ruling MS-R2 (a): once a patient is in scope the whole chart is. The
    facility 1 laboratory lists the moved patient's facility 2 report by UHID
    and on the patient's own list, so it opens and prints it too, and no
    refusal is put on the record for a patient who is in scope."""
    users = world["users"]
    p4 = moved_report
    sid = p4["accession"]
    tech = users["tech_f1"]
    before = _all_denials(tech["id"])
    by_uhid = _ok(client.get(f"/api/lab/reports?uhid={p4['mrn']}", headers=_hdr(tech)))
    assert [r["accession"] for r in by_uhid["reports"]] == [sid]
    chart = _ok(client.get(f"/api/lab/reports/patient/{p4['id']}", headers=_hdr(tech)))
    assert [r["accession"] for r in chart["reports"]] == [sid]
    opened = _ok(client.get(f"/api/lab/reports/{sid}", headers=_hdr(tech)))
    assert opened["report"]["accession"] == sid
    assert opened["report"]["patient"]["id"] == p4["id"]
    printed = _ok(client.get(f"/api/lab/reports/{sid}/print", headers=_hdr(tech)))
    # The paper names the hospital whose laboratory performed the sample.
    assert printed["report"]["hospital"]["name"] == F2_NAME
    assert _all_denials(tech["id"]) == before
    with db_session() as db:
        reads = [
            r[0]
            for r in db.execute(
                "SELECT action_type FROM audit_logs WHERE actor_user_id = ? AND target_id = ? "
                "AND action_type IN ('lab_report_viewed', 'lab_report_printed') ORDER BY id",
                (tech["id"], sid),
            ).fetchall()
        ]
    assert reads == ["lab_report_viewed", "lab_report_printed"]
    # The facility 2 laboratory still reads the work it performed.
    own = _ok(client.get(f"/api/lab/reports/{sid}", headers=_hdr(users["tech_f2"])))
    assert own["report"]["accession"] == sid


def test_a_moved_patients_report_still_keeps_to_scope_outside_the_report_read(
    world: dict, moved_report: dict, lanes_on: None
) -> None:
    """The chart rule widens nothing else: a ward keeps to its own patients
    (the facility 2 ward no longer holds this one), a laboratory account
    bound to neither facility is refused, and the facility 1 laboratory
    cannot open the facility 2 laboratory's amend view of the sample."""
    users = world["users"]
    p4 = moved_report
    sid = p4["accession"]
    nurse = users["nurse_f2"]
    before = len(_denials(nurse["id"], "lab_report"))
    ward = client.get(f"/api/lab/reports/{sid}", headers=_hdr(nurse))
    assert ward.status_code == 404 and P4_NAME not in ward.text
    _assert_facility_denial(nurse, "lab_report", p4["id"], before)
    tech = users["tech_f1"]
    before_tech = len(_denials(tech["id"], "lab_reported_results"))
    amend_view = client.get(f"/api/lab/results/reported/{sid}", headers=_hdr(tech))
    assert amend_view.status_code == 404 and P4_NAME not in amend_view.text
    _assert_facility_denial(tech, "lab_reported_results", p4["id"], before_tech)
    # Neither the performing facility nor the patient: refused and audited.
    with db_session() as db:
        refused = lab_facility.FacilityRefusal
        with pytest.raises(refused) as caught:
            lab_facility.require_in_scope(
                db,
                patient_id=p4["id"],
                specimen_id=sid,
                dept=None,
                facilities=frozenset({F2 + 1}),
                helper="lab_report",
                not_found=LookupError("Sample not found"),
                chart_read=True,
            )
        assert caught.value.facility_id == F2
        with pytest.raises(refused):
            lab_facility.require_in_scope(
                db,
                patient_id=p4["id"],
                specimen_id=sid,
                dept=None,
                facilities=frozenset(),
                helper="lab_report",
                not_found=LookupError("Sample not found"),
                chart_read=True,
            )
        # Either half is enough for the report read.
        for scope in (frozenset({1}), frozenset({F2})):
            lab_facility.require_in_scope(
                db,
                patient_id=p4["id"],
                specimen_id=sid,
                dept=None,
                facilities=scope,
                helper="lab_report",
                not_found=LookupError("Sample not found"),
                chart_read=True,
            )
        # Without chart_read the laboratory step keeps the performing facility.
        with pytest.raises(refused):
            lab_facility.require_in_scope(
                db,
                patient_id=p4["id"],
                specimen_id=sid,
                dept=None,
                facilities=frozenset({1}),
                helper="lab_specimen",
                not_found=LookupError("Sample not found"),
            )


# ═════════════════════════════════════════════════════════════════════════════
#  4. The unmatched results queue never attaches across facilities
# ═════════════════════════════════════════════════════════════════════════════


def _queued(patient: dict) -> int:
    payload = {
        "patient_identifiers": [patient["mrn"]],
        "patient_name": patient["name"],
        "patient_dob": "1977-05-14",
    }
    with db_session() as db:
        return int(
            db.execute(
                "INSERT INTO result_quarantine (lane, reason, payload_json, raw_patient_identifier, "
                "raw_patient_name, raw_patient_dob, observation_name, created_at) "
                "VALUES ('ingestion', 'unmatched_patient', ?, ?, ?, '1977-05-14', 'Potassium', ?)",
                (json.dumps(payload), patient["mrn"], patient["name"], utc_iso_now()),
            ).lastrowid
        )


def test_an_unmatched_result_is_never_attached_to_another_facilitys_patient(
    world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "RESULT_QUARANTINE_ENABLED", True)
    users, p1 = world["users"], world["patients"]["p1"]
    qid = _queued(p1)
    typed = {"hospital_number": p1["mrn"], "name": p1["name"], "date_of_birth": "1977-05-14"}
    refused = rq.attach(
        qid, reason="Checked against the request form", user=users["tech_f2"], **typed
    )
    assert refused["attached"] is False
    assert refused["checks"]["hospital_number"] == "no_match"
    with db_session() as db:
        assert (
            db.execute("SELECT status FROM result_quarantine WHERE id = ?", (qid,)).fetchone()[0]
            == "open"
        )

    class _ReachedTheWrite(Exception):
        pass

    def _stop(*args: Any, **kwargs: Any) -> None:
        raise _ReachedTheWrite

    from services import ingestion_service

    monkeypatch.setattr(ingestion_service, "later_correction_for_event", _stop)
    with pytest.raises(_ReachedTheWrite):
        # The reconciler at the patient's own facility gets past the identity
        # check to the write (stopped here so no observation is filed).
        rq.attach(qid, reason="Checked against the request form", user=users["tech_f1"], **typed)


# ═════════════════════════════════════════════════════════════════════════════
#  5. Medication orders
# ═════════════════════════════════════════════════════════════════════════════


def _draft(user: dict, patient_id: str) -> Any:
    return client.post(
        "/api/medorders/orders",
        json={
            "patient_id": patient_id,
            "drug_name": "Paracetamol",
            "status": "draft",
            "dose": "500 mg",
            "route": "Oral",
            "frequency": "TDS",
        },
        headers=_hdr(user),
    )


def test_a_medication_order_of_another_facilitys_patient_is_an_audited_404(
    world: dict, lanes_on: None
) -> None:
    users, p1 = world["users"], world["patients"]["p1"]
    order = _ok(_draft(users["doctor_f1"], p1["id"]))["order"]
    doctor = users["doctor_f2"]
    before = len(_denials(doctor["id"], "med_order_scope"))
    read = client.get(f"/api/medorders/orders/{order['id']}", headers=_hdr(doctor))
    assert read.status_code == 404 and read.json()["detail"] == "Medication order not found"
    _assert_facility_denial(doctor, "med_order_scope", p1["id"], before)
    signed = client.post(
        f"/api/medorders/orders/{order['id']}/sign",
        json={"expected_version": order["version"]},
        headers=_hdr(doctor),
    )
    assert signed.status_code == 404
    _assert_facility_denial(doctor, "med_order_scope", p1["id"], before + 1)
    created = _draft(doctor, p1["id"])
    assert created.status_code == 404 and created.json()["detail"] == "Patient not found"
    _assert_facility_denial(doctor, "med_order_scope", p1["id"], before + 2)
    assert _ok(client.get(f"/api/medorders/orders/{order['id']}", headers=_hdr(users["doctor_f1"])))
    # An account whose department no facility lists sees no order at all.
    lost = users["nurse_lost"]
    before_lost = len(_denials(lost["id"], "med_order_scope"))
    assert client.get(f"/api/medorders/orders/{order['id']}", headers=_hdr(lost)).status_code == 404
    rows = _denials(lost["id"], "med_order_scope")
    assert len(rows) == before_lost + 1 and rows[-1]["meta"]["facility_scope"] == []


def test_the_to_sign_queue_keeps_to_the_callers_facilities(world: dict, lanes_on: None) -> None:
    users, pats = world["users"], world["patients"]
    _ok(_draft(users["doctor_f1"], pats["p1"]["id"]))
    _ok(_draft(users["doctor_f2"], pats["p2"]["id"]))
    queue = _ok(client.get("/api/medorders/queue/to-sign", headers=_hdr(users["doctor_f2"])))
    # P3's last stay was on this doctor's ward, but the patient's latest
    # encounter is now at facility 1, so the draft has left this queue.
    assert {d["patient_id"] for d in queue["drafts"]} == {pats["p2"]["id"]}
    with _one_facility():
        as_before = _ok(
            client.get("/api/medorders/queue/to-sign", headers=_hdr(users["doctor_f2"]))
        )
    assert {d["patient_id"] for d in as_before["drafts"]} == {pats["p2"]["id"], pats["p3"]["id"]}


def test_parity_and_adoption_need_group_wide_access(world: dict, lanes_on: None) -> None:
    users = world["users"]
    admin = users["admin_f2"]
    for method, path in (
        ("get", "/api/medorders/admin/parity"),
        ("post", "/api/medorders/admin/adopt"),
    ):
        resp = getattr(client, method)(path, headers=_hdr(admin))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "group_access_required"
    with db_session() as db:
        rows = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE actor_user_id = ? "
            "AND action_type = 'facility_group_access_required' ORDER BY id",
            (admin["id"],),
        ).fetchall()
    assert [json.loads(r[0])["action"] for r in rows[-2:]] == ["parity report", "adoption"]
    assert _ok(client.get("/api/medorders/admin/parity", headers=_hdr(users["admin_group"])))
    with _one_facility():
        assert _ok(client.get("/api/medorders/admin/parity", headers=_hdr(admin)))


# ═════════════════════════════════════════════════════════════════════════════
#  6. The sweep: no facility 2 account reads facility 1's patient
# ═════════════════════════════════════════════════════════════════════════════

_PATH_PARAM = re.compile(r"\{([^}]+)\}")


def _filled(path: str, world: dict) -> str:
    work = world["work"]["p1"]
    values = {
        "specimen_id": work["done_specimen"],
        "code": work["transit_specimen"],
        "accession": work["done_specimen"],
        "section_id": "biochemistry",
        "patient_id": world["patients"]["p1"]["id"],
        "order_id": work["planned_order"],
        "result_id": str(work["done_result"]),
        "notification_id": work["critical"],
        "instrument_id": "NONE",
        "test_id": "none",
    }
    return _PATH_PARAM.sub(lambda m: values.get(m.group(1).split(":")[0], "1"), path)


def _get_routes() -> list[str]:
    out = []
    for r in main.app.routes:
        path = getattr(r, "path", "")
        if "GET" in (getattr(r, "methods", None) or set()) and (
            path.startswith("/api/lab") or path.startswith("/api/medorders")
        ):
            out.append(path)
    return sorted(set(out))


def test_no_facility_two_account_reads_the_facility_one_patient(
    world: dict, lanes_on: None
) -> None:
    users, p1 = world["users"], world["patients"]["p1"]
    planted = (P1_NAME, "Quandtveil", p1["mrn"], p1["id"])
    leaks = []
    routes = _get_routes()
    assert len(routes) > 20
    for path in routes:
        url = _filled(path, world)
        for key in ("tech_f2", "path_f2", "phleb_f2", "nurse_f2", "doctor_f2", "admin_f2"):
            resp = client.get(url, headers=_hdr(users[key]))
            body = resp.content.decode("utf-8", "ignore")
            hits = [p for p in planted if p in body and p not in url]
            if hits:
                leaks.append(f"{key} GET {url} ({resp.status_code}): {hits}")
    assert leaks == []
