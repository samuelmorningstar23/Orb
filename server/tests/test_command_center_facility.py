"""The Command Center, capacity and NABH figures per facility (multisite-12,
W13-A, wave 13, 20 October 2026).

Two facilities in a database this module owns (isolated_module_database):
facility 1 with the department Ostravel Medicine and its Bed setup ward, and
a second facility with Quimbrel Medicine and its own ward. Facility 1 has one
admitted patient at NEWS2 critical; facility 2 has one critical and one not
yet observed. Each facility has a surgical case today, an active care bundle,
an open escalation and an incident. One ward on the capacity map belongs to no
facility. Accounts: a department-less administrator at each facility (home
rows), a facility 2 ward nurse and a group-wide administrator by enterprise
grant.

Pinned, with the fence opened (/api/command-center and /api/nabh are still
blind route groups: their other lanes belong to other sweeps):

  * a facility 2 account's Command Center counts facility 2 only: census,
    capacity from its Bed setup ward, acuity, the named highest-scores list,
    bundles, the forecast and today's operating list; and says which facility
    it is about;
  * the group-wide administrator gets the group, with one tile per facility
    that adds up to the group's census and, with the unplaced ward, its beds;
    ?facility= narrows to one facility; a facility outside the caller's own,
    or one not on record, is 404;
  * NABH indicators, the register, dispositions and packs keep to one
    facility: a facility 2 pack counts facility 2's encounters and incidents,
    is numbered from facility 2's own series (refused without one), and is
    stamped facility 2; a group-wide reader must name the facility; another
    facility's incident is 404 like a missing one;
  * with one facility on record (facility_service's cached count 1) nothing
    changes and no facility key is added.

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
from services import audit_facility
from services import command_center_service as cc
from services import facility_service as FS
from services import forecast_service as fs
from services import hospital_time
from services import nabh_service as nabh
from services.clinical_time import utc_iso_now

pytestmark = pytest.mark.usefixtures("isolated_module_database")

# The hospital clock for the whole module (wave 15 fix-1): the seed's surgical
# day and the Command Center's OR today are both the hospital's day at this
# instant, so the module passes at any hour, 00:00 to 05:30 IST included,
# when the UTC date is still yesterday.
CLOCK = datetime.now(UTC)

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9663
F2_CODE = f"CC{TAG[:4]}"
F1_DEPT = f"Ostravel Medicine {TAG}"
F2_DEPT = f"Quimbrel Medicine {TAG}"
W1 = f"OSW-{TAG}"
W2 = f"QMW-{TAG}"
W_UNPLACED = f"ZNW-{TAG}"

P1 = f"CCFP1{TAG}"
P2 = f"CCFP2{TAG}"
P3 = f"CCFP3{TAG}"
NAME1 = "Vellamy Ostrander"
NAME2 = "Quessa Brindlemoor"
NAME3 = "Tarvish Okonkwe"

client = TestClient(main.app, raise_server_exceptions=False)


def _hdr(uid: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def world(isolated_module_database):
    now = utc_iso_now()
    # Ten minutes ago, for rows the NABH windows read: they compare whole seconds.
    earlier = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    # A surgical case's scheduled_date is a ward calendar date: the hospital's
    # day at CLOCK. An admission date is an instant, as admission writes it.
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr(hospital_time, "_utc_now", lambda: CLOCK)
        today = hospital_time.hospital_today().isoformat()
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
            (F2, str(uuid.uuid4()), F2_CODE, "Quimbrel Satellite"),
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
            uid = f"CCF{key.upper().replace('_', '')}{TAG}"[:30]
            ids[key] = uid
            db.execute(
                "INSERT INTO users (id, name, role, department, password_hash) "
                "VALUES (?, ?, ?, ?, 'x')",
                (uid, f"Pellidor {key}", role, user_dept),
            )
        db.execute(
            "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, "
            "reason) VALUES (?, ?, 'home', 'system', ?, 'command center')",
            (ids["f2_admin"], F2, now),
        )
        db.execute(
            "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
            "VALUES (?, 'system', ?, 'command center')",
            (ids["group_admin"], now),
        )
        for pid, name, dept, ward, fid, critical in (
            (P1, NAME1, F1_DEPT, W1, 1, True),
            (P2, NAME2, F2_DEPT, W2, F2, True),
            (P3, NAME3, F2_DEPT, W2, F2, False),
        ):
            db.execute(
                "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status, "
                "admission_date, registered_facility_id) "
                "VALUES (?, ?, ?, 61, 'M', ?, 'B-2', 'Prandel fever', 'admitted', ?, ?)",
                (pid, name, f"MRN{pid}", ward, earlier, fid),
            )
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                "status, facility_id, created_at) VALUES (?, ?, 'system', ?, ?, 'B-2', "
                "'active', ?, ?)",
                (f"A{pid}", pid, dept, ward, fid, now),
            )
            if critical:
                db.execute(
                    "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
                    "diastolic_bp, spo2, temperature, respiratory_rate, consciousness, "
                    "carried_from) VALUES (?, ?, 132, 88, 54, 87, 39.2, 29, 'alert', NULL)",
                    (pid, now),
                )
                db.execute(
                    "INSERT INTO care_bundles (patient_id, status, source, triggered_at) "
                    "VALUES (?, 'active', 'clinician', ?)",
                    (pid, now),
                )
                db.execute(
                    "INSERT INTO clinical_escalations (id, patient_id, raised_in_department, "
                    "status, news2, peak_news2, level, raised_at) "
                    "VALUES (?, ?, ?, 'open', 11, 11, 'critical', ?)",
                    (f"ESC{pid}", pid, dept, earlier),
                )
                db.execute(
                    "INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, "
                    "diagnosis, scheduled_date, scheduled_time, status, facility_id) "
                    "VALUES (?, ?, ?, 'Velstrian graft', 'Prandel fever', ?, '09:00', "
                    "'scheduled', ?)",
                    (f"SURG{pid}", pid, ids["group_admin"], today, fid),
                )
    FS.invalidate_cache()
    audit_facility.invalidate_cache()
    F._audit_last.clear()
    assert FS.multi_facility_data() is True
    ids["incident_f1"] = nabh.report_incident(
        incident_type="patient_fall", severity="minor", title="Slip by the sink",
        description="Slipped near the sink.", department=F1_DEPT, ward=W1,
        occurred_at=earlier, reported_by=ids["f1_admin"], reporter_role="admin",
    )["id"]
    ids["incident_f2"] = nabh.report_incident(
        incident_type="medication_error", severity="minor", title="Late dose",
        description="A dose was given late.", department=F2_DEPT, ward=W2,
        occurred_at=earlier, reported_by=ids["f2_admin"], reporter_role="admin",
    )["id"]
    try:
        yield {"ids": ids, "h": {k: _hdr(ids[k], users[k][0]) for k in users}}
    finally:
        FS.invalidate_cache()
        audit_facility.invalidate_cache()
        F._audit_last.clear()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """The ward map is this module's three wards, the flow forecast is not
    cached between tests, the facility caches start empty, and the hospital
    clock reads CLOCK, the instant the seed's surgical day was taken at."""
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: CLOCK)
    saved = dict(fs.WARD_CAPACITY)
    fs.WARD_CAPACITY.clear()
    fs.WARD_CAPACITY.update({W1: 6, W2: 4, W_UNPLACED: 3})
    monkeypatch.setattr(fs, "TOTAL_CAPACITY", 13)
    fs._hospital_flow_cache.clear()
    FS.invalidate_cache()
    audit_facility.invalidate_cache()
    try:
        yield
    finally:
        fs.WARD_CAPACITY.clear()
        fs.WARD_CAPACITY.update(saved)
        fs._hospital_flow_cache.clear()
        FS.invalidate_cache()
        audit_facility.invalidate_cache()


@pytest.fixture()
def fence_open(monkeypatch):
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


@pytest.fixture()
def one_facility():
    """facility_service's cached answer for one facility on record, as on every
    install today."""
    FS._store(1)
    assert FS.cached_multi_facility_data() is False
    yield
    FS.invalidate_cache()


def _snapshot(world, who: str, facility: int | None = None):
    url = "/api/command-center/" + (f"?facility={facility}" if facility else "")
    return client.get(url, headers=world["h"][who])


def _patient_ids(snap: dict) -> set:
    return {d["patient_id"] for d in snap["deteriorations"]}


# ── capacity per facility ────────────────────────────────────────────────────


def test_each_ward_is_placed_at_its_bed_setup_facility(world):
    placed = fs.ward_facilities()
    assert placed[W1] == 1 and placed[W2] == F2
    # Neither Bed setup nor an open stay places it.
    assert placed[W_UNPLACED] is None
    assert fs.capacity_for(frozenset({F2})) == {W2: 4}
    assert fs.capacity_for(frozenset({1})) == {W1: 6}
    assert fs.capacity_for(None) == {W1: 6, W2: 4, W_UNPLACED: 3}


def test_with_one_facility_every_ward_is_facility_one(world, one_facility):
    assert set(fs.ward_facilities().values()) == {1}


def test_the_capacity_forecast_keeps_to_the_facilitys_wards(world):
    cap = fs.get_bed_capacity_forecast(facilities=frozenset({F2}))
    assert set(cap["current"]) == {W2}
    assert cap["current"][W2]["occupied"] == 2
    assert cap["total"]["capacity"] == 4
    whole = fs.get_bed_capacity_forecast()
    assert set(whole["current"]) == {W1, W2, W_UNPLACED}
    assert whole["total"]["capacity"] == 13


def test_the_flow_forecast_counts_the_facilitys_patients_and_beds(world):
    f2 = fs.predict_hospital_flow(days_ahead=1, facilities=frozenset({F2}))
    assert f2["current_occupancy"] == 2
    assert f2["total_capacity"] == 4
    assert set(f2["daily_forecasts"][0]["ward_breakdown"]) == {W2}
    whole = fs.predict_hospital_flow(days_ahead=1)
    assert whole["current_occupancy"] == 3
    assert whole["total_capacity"] == fs.TOTAL_CAPACITY


# ── the Command Center ───────────────────────────────────────────────────────


def test_a_facility_two_account_sees_facility_two_only(world, fence_open):
    resp = _snapshot(world, "f2_admin")
    assert resp.status_code == 200, resp.text
    snap = resp.json()
    assert snap["census"]["total_admitted"] == 2
    assert snap["census"]["total_capacity"] == 4
    assert [w["ward"] for w in snap["census"]["by_ward"]] == [W2]
    assert _patient_ids(snap) == {P2}
    assert snap["acuity"]["critical"] == 1
    assert [c["id"] for c in snap["or_today"]["cases"]] == [f"SURG{P2}"]
    assert snap["bundles"]["active"] == 1
    assert snap["forecast"]["total_capacity"] == 4
    assert snap["downtime_readiness"]["total"] == 2
    assert snap["facility"]["id"] == F2
    assert snap["facility_view"] == "facility"
    assert [f["id"] for f in snap["facilities"]] == [F2]
    assert "facility_tiles" not in snap
    body = resp.text
    for word in (NAME1, P1, f"SURG{P1}", W1):
        assert word not in body


def test_a_facility_one_account_sees_facility_one_only(world, fence_open):
    snap = _snapshot(world, "f1_admin").json()
    assert snap["census"]["total_admitted"] == 1
    assert snap["census"]["total_capacity"] == 6
    assert _patient_ids(snap) == {P1}
    assert [c["id"] for c in snap["or_today"]["cases"]] == [f"SURG{P1}"]
    assert snap["facility"]["id"] == 1
    for word in (NAME2, NAME3, P2, P3):
        assert word not in str(snap)


def test_the_group_view_has_a_tile_per_facility_that_adds_up(world, fence_open):
    snap = _snapshot(world, "group_admin").json()
    assert snap["facility_view"] == "group"
    assert snap["facility"] is None
    assert {f["id"] for f in snap["facilities"]} == {1, F2}
    assert _patient_ids(snap) == {P1, P2}
    tiles = {t["facility"]["id"]: t for t in snap["facility_tiles"]}
    assert set(tiles) == {1, F2}
    assert tiles[1]["total_admitted"] == 1 and tiles[1]["total_capacity"] == 6
    assert tiles[F2]["total_admitted"] == 2 and tiles[F2]["total_capacity"] == 4
    assert tiles[F2]["critical"] == 1 and tiles[F2]["not_scored"] == 1
    census = snap["census"]
    assert sum(t["total_admitted"] for t in tiles.values()) == census["total_admitted"] == 3
    # The unplaced ward's beds are counted apart, never given to a facility.
    assert snap["unplaced_capacity"] == 3
    assert (
        sum(t["total_capacity"] for t in tiles.values()) + snap["unplaced_capacity"]
        == census["total_capacity"]
        == 13
    )
    assert sum(t["critical"] for t in tiles.values()) == snap["acuity"]["critical"]


def test_a_group_reader_narrows_with_the_facility_query(world, fence_open):
    snap = _snapshot(world, "group_admin", facility=F2).json()
    assert snap["facility"]["id"] == F2
    assert snap["census"]["total_admitted"] == 2
    assert _patient_ids(snap) == {P2}
    assert "facility_tiles" not in snap


def test_a_facility_outside_the_callers_own_is_404(world, fence_open):
    for who, facility in (("f2_admin", 1), ("f1_admin", F2), ("group_admin", 987654)):
        resp = _snapshot(world, who, facility=facility)
        assert resp.status_code == 404, (who, facility, resp.text)
        assert resp.json()["detail"]["error"] == "not_found"
    assert _snapshot(world, "f2_admin", facility=F2).status_code == 200


def test_a_ward_nurse_gets_the_department_list_inside_the_facility(world, fence_open):
    snap = _snapshot(world, "f2_nurse").json()
    assert snap["deteriorations_scope"] == F2_DEPT
    assert _patient_ids(snap) == {P2}
    # The count beside the list is the facility's, not the group's.
    assert snap["deteriorations_house_count"] == 1
    assert snap["facility"]["id"] == F2


def test_the_named_list_read_is_audited_with_the_facility_scope(world, fence_open):
    import json

    _snapshot(world, "f2_admin")
    with db_session() as db:
        row = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE action_type = 'command_center_viewed' "
            "AND actor_user_id = ? ORDER BY id DESC LIMIT 1",
            (world["ids"]["f2_admin"],),
        ).fetchone()
    meta = json.loads(row["metadata_json"])
    assert meta["patient_ids"] == [P2]
    assert meta["facility_scope"] == [F2]


def test_the_real_fence_still_holds_the_command_center_shut(world):
    resp = _snapshot(world, "f2_admin")
    assert resp.status_code == 503


def test_with_one_facility_the_command_center_is_as_before(world, one_facility):
    resp = _snapshot(world, "f2_admin")
    assert resp.status_code == 200, resp.text
    snap = resp.json()
    assert {P1, P2} <= _patient_ids(snap)
    assert snap["census"]["total_admitted"] == 3
    for key in ("facility", "facilities", "facility_view", "facility_tiles"):
        assert key not in snap
    assert _snapshot(world, "f2_admin", facility=1).status_code == 200
    assert _snapshot(world, "f2_admin", facility=F2).status_code == 404


def test_the_service_default_is_the_whole_appliance(world):
    snap = cc.house_snapshot()
    assert snap["census"]["total_admitted"] == 3
    assert "facility_tiles" not in snap
    scoped = cc.house_snapshot(frozenset({F2}))
    assert scoped["census"]["total_admitted"] == 2
    empty = cc.house_snapshot(frozenset())
    assert empty["census"]["total_admitted"] == 0
    assert empty["census"]["total_capacity"] == 0
    assert empty["deteriorations"] == [] and empty["or_today"]["count"] == 0


# ── NABH per facility ────────────────────────────────────────────────────────


def _series(fid: int, code: str, prefix: str) -> None:
    with db_session() as db:
        db.execute(
            "INSERT INTO document_number_series (facility_id, series_code, document_type, "
            "prefix, next_number) VALUES (?, ?, 'nabh_pack', ?, 1)",
            (fid, code, prefix),
        )


def _indicator(dash: dict, indicator_id: str) -> dict:
    return next(i for i in dash["indicators"] if i["id"] == indicator_id)


def test_a_facilitys_indicators_count_its_encounters_only(world):
    f2 = nabh.indicators(facility_id=F2)
    whole = nabh.indicators()
    assert f2["facility_id"] == F2 and "facility_id" not in whole
    assert f2["register_usage"]["total"] == 1
    assert f2["register_usage"]["by_type"] == {"medication_error": 1}
    assert whole["register_usage"]["total"] == 2
    esc_f2 = _indicator(f2, "escalation_ack_compliance")["detail"]["escalations_raised"]
    esc_all = _indicator(whole, "escalation_ack_compliance")["detail"]["escalations_raised"]
    assert (esc_f2, esc_all) == (1, 2)
    assert f2["denominators"]["inpatient_days"]["patients"] == 2
    assert whole["denominators"]["inpatient_days"]["patients"] == 3
    f1 = nabh.indicators(facility_id=1)
    assert f1["register_usage"]["by_type"] == {"patient_fall": 1}


def test_the_indicator_route_is_the_callers_facility(world, fence_open):
    h = world["h"]
    resp = client.get("/api/nabh/indicators", headers=h["f2_admin"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["facility"]["id"] == F2
    assert body["register_usage"]["total"] == 1
    group = client.get("/api/nabh/indicators", headers=h["group_admin"]).json()
    assert group["facility"] is None and group["register_usage"]["total"] == 2
    narrowed = client.get(f"/api/nabh/indicators?facility={F2}", headers=h["group_admin"]).json()
    assert narrowed["facility"]["id"] == F2 and narrowed["register_usage"]["total"] == 1
    assert client.get("/api/nabh/indicators?facility=1", headers=h["f2_admin"]).status_code == 404


def test_the_register_keeps_to_the_facility_and_another_is_404(world, fence_open):
    h, ids = world["h"], world["ids"]
    rows = client.get("/api/nabh/incidents", headers=h["f2_admin"]).json()["incidents"]
    assert [r["id"] for r in rows] == [ids["incident_f2"]]
    assert client.get(f"/api/nabh/incidents/{ids['incident_f1']}",
                      headers=h["f2_admin"]).status_code == 404
    resp = client.post(f"/api/nabh/incidents/{ids['incident_f1']}/investigate",
                       json={"note": "Taking it on"}, headers=h["f2_admin"])
    assert resp.status_code == 404
    assert nabh.get_incident(ids["incident_f1"])["status"] == "reported"
    group = client.get("/api/nabh/incidents", headers=h["group_admin"]).json()["incidents"]
    assert {r["id"] for r in group} >= {ids["incident_f1"], ids["incident_f2"]}


def test_the_ward_list_is_the_facilitys_wards(world, fence_open):
    wards = client.get("/api/nabh/meta", headers=world["h"]["f2_admin"]).json()["wards"]
    assert W2 in wards and W1 not in wards


def test_a_facility_pack_is_numbered_and_stamped_at_that_facility(world, fence_open):
    h = world["h"]
    refused = client.post("/api/nabh/export", json={}, headers=h["f2_admin"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "series_not_set_up"
    _series(F2, "NABHPACK", f"{F2_CODE}/PACK-")
    resp = client.post("/api/nabh/export", json={}, headers=h["f2_admin"])
    assert resp.status_code == 200, resp.text
    pack = resp.json()
    assert pack["facility"]["id"] == F2
    assert pack["reference"].startswith(f"{F2_CODE}/PACK-")
    assert pack["register"]["count"] == 1
    assert pack["indicator_summary"]["total"] >= 9
    assert nabh.FACILITY_PACK_LIMITATION in pack["limitations"]
    with db_session() as db:
        row = db.execute(
            "SELECT facility_id FROM nabh_evidence_exports WHERE reference = ?",
            (pack["reference"],),
        ).fetchone()
    assert row["facility_id"] == F2
    exports = client.get("/api/nabh/exports", headers=h["f1_admin"]).json()["exports"]
    assert pack["reference"] not in {e["reference"] for e in exports}
    own = client.get("/api/nabh/exports", headers=h["f2_admin"]).json()["exports"]
    assert pack["reference"] in {e["reference"] for e in own}


def test_a_group_reader_must_name_the_facility_of_a_pack(world, fence_open):
    h = world["h"]
    refused = client.post("/api/nabh/export", json={}, headers=h["group_admin"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "pack_spans_facilities"
    resp = client.post("/api/nabh/export", json={"facility": 1}, headers=h["group_admin"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["facility"]["id"] == 1
    md = client.post("/api/nabh/export.md", json={"facility": 1}, headers=h["group_admin"])
    assert md.status_code == 200
    assert "**Facility:**" in md.text
    other = client.post("/api/nabh/export", json={"facility": F2}, headers=h["f1_admin"])
    assert other.status_code == 404


def test_with_one_facility_nabh_is_as_before(world, one_facility):
    h = world["h"]
    body = client.get("/api/nabh/indicators", headers=h["f2_admin"]).json()
    assert "facility" not in body and "facility_id" not in body
    assert body["register_usage"]["total"] == 2
    rows = client.get("/api/nabh/incidents", headers=h["f2_admin"]).json()
    assert "facilities" not in rows and len(rows["incidents"]) == 2
