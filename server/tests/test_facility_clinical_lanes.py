"""Facility scope on the clinical lanes (multisite-8, W12-A, wave 12, 20 October 2026).

Two facilities in a database this module owns (isolated_module_database):
facility 1 with the department Ovrelith Medicine and a second facility with
Pranvoke Medicine. Each has one admitted patient with critical vitals, an
escalation chain, a care bundle, an order, a medication, a booking, a
surgical case and a case room; facility 1 also has a discharged patient, a
filed and an unfiled Lens read and an unfiled Scribe draft. Accounts: a ward
doctor and nurse of each department and a department-less administrator at
each facility (home rows), plus a group-wide administrator by enterprise grant.

Pinned, with the fence opened where the route group is still blind (its
other lanes belong to other sweeps), and with the real fence where the group
is cleared:

  * every clinical board and list keeps a facility 2 account to facility 2's
    patients (the Vigil board, the discharged list, search, escalations, care
    bundles, the nurse board and clerk list, the discharge board, the forecast
    appointments, surgery, Relay rooms, Lens, the unseen counts, shadow
    episodes) and still shows facility 1's own staff their patients;
  * reads by id (an order, an escalation, a dose, a FHIR export, a surgical
    case) answer 404 across the boundary, and the order, escalation and dose
    lanes write a patient_scope_denied row with scope_cause 'facility';
  * the strict defaults: an unfiled Lens read, Scribe draft or media file is
    the author's alone under a facility limit; a Relay room with no patient or
    a department room the master does not list is withheld; a named case team
    member at another facility does not see the case; the ward brief with no
    facility scope given withholds itself for a department-less reader; a
    named attending at another facility is not paged and the tier falls to
    the patient's own department; shadow episodes with no patient are
    withheld;
  * with one facility on record (facility_service's cached count 1) nothing
    changes: a department-less administrator reads the whole house again.

Test patients carry invented names (never a word a clinician types). Run with
the test env, never the shipping .env.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import facility_fence as F
import main
from auth import create_token
from config import UPLOAD_DIR
from database import db_session
from services import facility_service as FS
from services.clinical_time import utc_iso_now
from services.facility_filter import NEVER, column_clause, in_scope, patient_clause

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9531
F2_CODE = f"CL{TAG[:4]}"
F1_DEPT = f"Ovrelith Medicine {TAG}"
F2_DEPT = f"Pranvoke Medicine {TAG}"
F2_THEATRE = f"Pranvoke Theatre {TAG}"

P1 = f"CLP1{TAG}"
P2 = f"CLP2{TAG}"
PD = f"CLPD{TAG}"
NAME1 = "Wexmora Quillfeather"
NAME2 = "Dravenna Ostrowicz"
NAMED = "Yolvaine Brecketton"
MRN1 = f"CLMRN1{TAG}"
MRN2 = f"CLMRN2{TAG}"
MRND = f"CLMRND{TAG}"

client = TestClient(main.app, raise_server_exceptions=False)


def _hdr(uid: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


@pytest.fixture(scope="module")
def world(isolated_module_database):
    from services.helix_db import init_helix_db

    init_helix_db()
    now = utc_iso_now()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    users = {
        "f1_doctor": ("doctor", F1_DEPT),
        "f1_nurse": ("nurse", F1_DEPT),
        "f2_doctor": ("doctor", F2_DEPT),
        "f2_nurse": ("nurse", F2_DEPT),
        "f2_theatre": ("doctor", F2_THEATRE),
        "f1_admin": ("admin", None),
        "f2_admin": ("admin", None),
        "group_admin": ("admin", None),
    }
    ids = {}
    with db_session() as db:
        db.execute(
            "INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
            (F2, str(uuid.uuid4()), F2_CODE, "Pranvoke Satellite"),
        )
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
            (F1_DEPT,),
        )
        for dept in (F2_DEPT, F2_THEATRE):
            db.execute(
                "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, ?, 'test')",
                (dept, F2),
            )
        for key, (role, dept) in users.items():
            uid = f"CL{key.upper().replace('_', '')}{TAG}"[:30]
            ids[key] = uid
            db.execute(
                "INSERT INTO users (id, name, role, department, password_hash) "
                "VALUES (?, ?, ?, ?, 'x')",
                (uid, f"Clinlane {key}", role, dept),
            )
        db.execute(
            "INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, granted_at, "
            "reason) VALUES (?, ?, 'home', 'system', ?, 'clinical lanes')",
            (ids["f2_admin"], F2, now),
        )
        db.execute(
            "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
            "VALUES (?, 'system', ?, 'clinical lanes')",
            (ids["group_admin"], now),
        )
        for pid, name, mrn, dept, fid, status in (
            (P1, NAME1, MRN1, F1_DEPT, 1, "admitted"),
            (P2, NAME2, MRN2, F2_DEPT, F2, "admitted"),
            (PD, NAMED, MRND, F1_DEPT, 1, "discharged"),
        ):
            db.execute(
                "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status, "
                "admission_date, registered_facility_id, attending_doctor_id) "
                "VALUES (?, ?, ?, 58, 'F', ?, 'K-4', 'Prandel fever', ?, ?, ?, ?)",
                (pid, name, mrn, f"Ward {dept}", status, date.today().isoformat(), fid,
                 ids["f2_doctor"] if pid == P1 else None),
            )
            db.execute(
                "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
                "status, facility_id, created_at) VALUES (?, ?, 'system', ?, ?, 'K-4', ?, ?, ?)",
                (f"A{pid}", pid, dept, f"Ward {dept}",
                 "active" if status == "admitted" else "discharged", fid, now),
            )
            if status != "admitted":
                continue
            db.execute(
                "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
                "diastolic_bp, spo2, temperature, respiratory_rate, consciousness, "
                "carried_from) VALUES (?, ?, 132, 88, 54, 87, 39.2, 29, 'alert', NULL)",
                (pid, now),
            )
            db.execute(
                "INSERT INTO clinical_escalations (id, patient_id, raised_in_department, "
                "status, news2, peak_news2, level, raised_at) "
                "VALUES (?, ?, ?, 'open', 11, 11, 'critical', ?)",
                (f"ESC{pid}", pid, dept, now),
            )
            db.execute(
                "INSERT INTO care_bundles (patient_id, status, source, triggered_at) "
                "VALUES (?, 'active', 'clinician', ?)",
                (pid, now),
            )
            db.execute(
                "INSERT INTO orders (id, patient_id, order_type, description, status, "
                "ordered_by) VALUES (?, ?, 'lab', 'Quarvel panel', 'active', 'system')",
                (f"ORD{pid}", pid),
            )
            cur = db.execute(
                "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, "
                "status, prescribed_by, source) VALUES (?, 'Zentravil', '5 mg', 'oral', "
                "'OD', 'active', 'system', 'clinician')",
                (pid,),
            )
            ids[f"med_{pid}"] = cur.lastrowid
            db.execute(
                "INSERT INTO appointments (id, patient_id, scheduled_date, scheduled_time, "
                "status, department, facility_id) VALUES (?, ?, ?, '10:00', 'booked', ?, ?)",
                (f"APT{pid}", pid, tomorrow, dept, fid),
            )
            db.execute(
                "INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis, "
                "scheduled_date, scheduled_time, status, facility_id) "
                "VALUES (?, ?, ?, 'Velstrian graft', 'Prandel fever', ?, '09:00', "
                "'scheduled', ?)",
                (f"SURG{pid}", pid, ids["f1_doctor"] if fid == 1 else ids["f2_theatre"],
                 date.today().isoformat(), fid),
            )
            db.execute(
                "INSERT INTO channels (id, name, type, patient_id) VALUES (?, ?, 'case_room', ?)",
                (f"case-{pid.lower()}", f"{name} case room", pid),
            )
        # The facility 2 doctor is on facility 1's case team (clause D).
        db.execute(
            "INSERT INTO surgery_team_assignments (surgery_id, user_id, role_name, status) "
            "VALUES (?, ?, 'assistant', 'alerted')",
            (f"SURG{P1}", ids["f2_doctor"]),
        )
        for dept in (F1_DEPT, F2_DEPT, f"Plenvory Lounge {TAG}"):
            db.execute(
                "INSERT INTO channels (id, name, type) VALUES (?, ?, 'department')",
                (f"dept-{uuid.uuid4().hex[:10]}", dept),
            )
        # Lens: a read filed for the facility 1 patient and one unfiled read by
        # the facility 1 doctor, whose image is on disk for the media route.
        ids["unfiled_image"] = f"lens_{uuid.uuid4().hex}.png"
        with open(os.path.join(str(UPLOAD_DIR), ids["unfiled_image"]), "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nclinical-lanes")
        db.execute(
            "INSERT INTO lens_analyses (patient_id, author_id, image_path, analysis) "
            "VALUES (?, ?, ?, 'Filed Quarvel read')",
            (P1, ids["f1_doctor"], str(UPLOAD_DIR / f"lens_{uuid.uuid4().hex}.png")),
        )
        db.execute(
            "INSERT INTO lens_analyses (patient_id, author_id, image_path, analysis) "
            "VALUES (NULL, ?, ?, 'Unfiled Quarvel read')",
            (ids["f1_doctor"], str(UPLOAD_DIR / ids["unfiled_image"])),
        )
        cur = db.execute(
            "INSERT INTO scribe_notes (patient_id, author_id, raw_transcript) "
            "VALUES (NULL, ?, 'Unfiled Quarvel draft')",
            (ids["f1_doctor"],),
        )
        ids["unfiled_note"] = cur.lastrowid
        # The facility 2 administrator reviewed both charts once.
        for pid in (P1, P2):
            db.execute(
                "INSERT INTO patient_review_marks (user_id, patient_id, last_reviewed_at) "
                "VALUES (?, ?, '2020-01-01T00:00:00+00:00')",
                (ids["f2_admin"], pid),
            )
    FS.invalidate_cache()
    F._audit_last.clear()
    assert FS.multi_facility_data() is True
    try:
        yield {"ids": ids, "h": {k: _hdr(ids[k], users[k][0]) for k in users}}
    finally:
        try:
            os.remove(os.path.join(str(UPLOAD_DIR), ids["unfiled_image"]))
        except OSError:
            pass
        FS.invalidate_cache()
        F._audit_last.clear()


@pytest.fixture(autouse=True)
def _fresh_cache():
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


@pytest.fixture()
def fence_open(monkeypatch):
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


@pytest.fixture()
def one_facility():
    """facility_service's cached answer for one facility on record, as on every
    install today: the scope resolver answers None before it reads anything."""
    FS._store(1)
    assert FS.cached_multi_facility_data() is False
    yield
    FS.invalidate_cache()


def _ids(rows, key="id") -> set:
    return {r[key] for r in rows}


def _denials(patient_id: str) -> list[dict]:
    with db_session() as db:
        rows = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE action_type = 'patient_scope_denied' "
            "AND patient_id = ? ORDER BY id",
            (patient_id,),
        ).fetchall()
    return [json.loads(r["metadata_json"] or "{}") for r in rows]


# ── the predicate builders ───────────────────────────────────────────────────


def test_no_facility_limit_adds_nothing():
    assert column_clause("a.facility_id", None) == ("", ())
    assert patient_clause(None) == ("", ())
    assert column_clause("a.facility_id", frozenset()) == (NEVER, ())
    assert patient_clause(frozenset()) == (NEVER, ())
    sql, args = column_clause("a.facility_id", frozenset({7, 2}))
    assert sql == " AND a.facility_id IN (?, ?)" and args == (2, 7)
    assert in_scope(None, None) is True
    assert in_scope(None, frozenset({1})) is False
    assert in_scope(2, frozenset({2})) is True


# ── boards and lists ─────────────────────────────────────────────────────────


def test_the_vigil_board_keeps_each_account_to_its_facility(world, fence_open):
    h = world["h"]
    seen = {}
    for who in ("f1_admin", "f2_admin", "f1_doctor", "f2_doctor", "group_admin"):
        resp = client.get("/api/vigil/patients", headers=h[who])
        assert resp.status_code == 200, (who, resp.text)
        seen[who] = _ids(resp.json())
    assert seen["f2_admin"] == {P2}
    assert seen["f1_admin"] == {P1}
    assert seen["f2_doctor"] == {P2}
    assert seen["f1_doctor"] == {P1}
    assert {P1, P2} <= seen["group_admin"]
    # The admission id the picker carries is the facility's own stay.
    row = client.get("/api/vigil/patients", headers=h["f2_admin"]).json()[0]
    assert row["admission_id"] == f"A{P2}"


def test_with_one_facility_the_board_is_the_whole_house_again(world, one_facility):
    resp = client.get("/api/vigil/patients", headers=world["h"]["f2_admin"])
    assert resp.status_code == 200
    assert {P1, P2} <= _ids(resp.json())


def test_the_discharged_list_keeps_to_the_facility(world, fence_open):
    h = world["h"]
    f2 = client.get("/api/vigil/discharged", headers=h["f2_admin"])
    f1 = client.get("/api/vigil/discharged", headers=h["f1_admin"])
    assert f2.status_code == 200 and f1.status_code == 200
    assert PD not in _ids(f2.json())
    assert PD in _ids(f1.json())
    assert f2.headers["X-Total-Count"] == str(len(f2.json()))


def test_search_is_cleared_and_keeps_to_the_facility(world):
    # The real fence: /api/search is a cleared group.
    assert not F.is_fenced("/api/search/patients")
    h = world["h"]
    f2 = client.get(f"/api/search/patients?q={MRN1}", headers=h["f2_admin"])
    f1 = client.get(f"/api/search/patients?q={MRN1}", headers=h["f1_admin"])
    assert f2.status_code == 200 and f2.json() == []
    assert f1.status_code == 200 and _ids(f1.json()) == {P1}
    assert NAME1 not in f2.text


def test_the_escalation_board_and_its_reads_keep_to_the_facility(world, fence_open):
    h = world["h"]
    f2 = client.get("/api/vigil/escalations", headers=h["f2_admin"])
    f1 = client.get("/api/vigil/escalations", headers=h["f1_admin"])
    assert f2.status_code == 200 and f1.status_code == 200
    assert _ids(f2.json()["escalations"], "patient_id") == {P2}
    assert _ids(f1.json()["escalations"], "patient_id") == {P1}
    one = client.get(f"/api/vigil/escalations/ESC{P1}", headers=h["f2_admin"])
    assert one.status_code == 404
    assert NAME1 not in one.text
    assert any(m.get("scope_cause") == "facility" for m in _denials(P1))
    assert client.get(f"/api/vigil/escalations/ESC{P1}", headers=h["f1_admin"]).status_code == 200


def test_the_escalation_scan_keeps_to_the_facility(world):
    from services import escalation_service

    raised = escalation_service.scan_new_observations(facilities=frozenset({F2}))
    assert P1 not in {r.get("patient_id") for r in raised}


def test_care_bundles_keep_to_the_facility(world, fence_open):
    h = world["h"]
    f2 = client.get("/api/bundles/active", headers=h["f2_admin"])
    assert f2.status_code == 200
    assert _ids(f2.json()["bundles"], "patient_id") == {P2}
    f1 = client.get("/api/bundles/active", headers=h["f1_admin"])
    assert _ids(f1.json()["bundles"], "patient_id") == {P1}


def test_the_nurse_board_and_clerk_list_keep_to_the_facility(world, fence_open):
    from services import nurse_shift_service

    h = world["h"]
    f2 = client.get("/api/nurse/board", headers=h["f2_admin"])
    assert f2.status_code == 200, f2.text
    assert NAME1 not in f2.text and NAME2 in f2.text
    f1 = client.get("/api/nurse/board", headers=h["f1_nurse"])
    assert NAME1 in f1.text and NAME2 not in f1.text
    listed = nurse_shift_service.transcribe_patients(None, facilities=frozenset({F2}))
    assert {p["patient_id"] for p in listed} == {P2}
    assert nurse_shift_service.transcribe_patients(None, facilities=frozenset()) == []


def test_the_discharge_board_and_bookings_keep_to_the_facility(world, fence_open):
    h = world["h"]
    board = client.get("/api/forecast/discharge-board", headers=h["f2_admin"])
    assert board.status_code == 200, board.text
    assert _ids(board.json(), "patient_id") == {P2}
    booked = client.get("/api/forecast/appointments", headers=h["f2_admin"])
    assert booked.status_code == 200
    assert _ids(booked.json(), "patient_id") == {P2}
    f1 = client.get("/api/forecast/appointments", headers=h["f1_admin"])
    assert _ids(f1.json(), "patient_id") == {P1}


def test_a_booking_is_seen_only_at_its_own_facility(world):
    from routes.appointments_routes import _appointment_visible

    with db_session() as db:
        row = db.execute("SELECT * FROM appointments WHERE id = ?", (f"APT{P1}",)).fetchone()
        assert _appointment_visible(db, row, None, None) is True
        assert _appointment_visible(db, row, None, frozenset({F2})) is False
        assert _appointment_visible(db, row, None, frozenset({1})) is True


def test_surgery_keeps_every_clause_to_the_facility(world, fence_open):
    h = world["h"]
    # (B) a facility 2 theatre account reads its own facility's list only.
    theatre = client.get("/api/surgery/cases", headers=h["f2_theatre"])
    assert theatre.status_code == 200, theatre.text
    assert _ids(theatre.json()) == {f"SURG{P2}"}
    # (A) a department-less administrator: its own facility only.
    f2 = client.get("/api/surgery/cases", headers=h["f2_admin"])
    assert _ids(f2.json()) == {f"SURG{P2}"}
    f1 = client.get("/api/surgery/cases", headers=h["f1_admin"])
    assert _ids(f1.json()) == {f"SURG{P1}"}
    # (D) a named team member at another facility does not see the case.
    assert client.get(f"/api/surgery/SURG{P1}", headers=h["f2_doctor"]).status_code == 404
    assert client.get(f"/api/surgery/SURG{P1}", headers=h["f1_admin"]).status_code == 200


def test_surgery_with_one_facility_is_unchanged(world, one_facility):
    from services.surgery_scope import reads_whole_theatre, surgical_list_filter

    theatre = {"id": world["ids"]["f2_theatre"], "role": "doctor", "department": F2_THEATRE}
    assert reads_whole_theatre(theatre) is True
    assert surgical_list_filter(theatre) == ("", ())


def test_relay_rooms_keep_to_the_facility(world, fence_open):
    h = world["h"]
    f2 = client.get("/api/relay/channels", headers=h["f2_admin"])
    assert f2.status_code == 200
    names = {c["name"] for c in f2.json()}
    assert f"{NAME2} case room" in names and f2.json()
    assert f"{NAME1} case room" not in names
    assert F2_DEPT in names and F1_DEPT not in names
    # A department room the master does not list has no facility: withheld.
    assert f"Plenvory Lounge {TAG}" not in names
    f1 = {c["name"] for c in client.get("/api/relay/channels", headers=h["f1_admin"]).json()}
    assert f"{NAME1} case room" in f1 and f"{NAME2} case room" not in f1


def test_relay_rooms_with_one_facility_are_unchanged(world, one_facility):
    names = {c["name"] for c in client.get(
        "/api/relay/channels", headers=world["h"]["f2_admin"]).json()}
    assert {f"{NAME1} case room", f"{NAME2} case room", f"Plenvory Lounge {TAG}"} <= names


def test_lens_is_cleared_and_unfiled_reads_stay_with_their_author(world):
    assert not F.is_fenced("/api/lens/analyses")
    h = world["h"]
    f2 = client.get("/api/lens/analyses", headers=h["f2_admin"])
    assert f2.status_code == 200 and f2.json() == []
    author = client.get("/api/lens/analyses", headers=h["f1_doctor"])
    texts = {r["analysis"] for r in author.json()}
    assert {"Filed Quarvel read", "Unfiled Quarvel read"} <= texts
    f1 = client.get("/api/lens/analyses", headers=h["f1_admin"])
    assert {r["analysis"] for r in f1.json()} == {"Filed Quarvel read"}
    named = client.get(f"/api/lens/analyses?patient_id={P1}", headers=h["f2_admin"])
    assert named.status_code == 404


def test_lens_with_one_facility_is_unchanged(world, one_facility):
    texts = {r["analysis"] for r in client.get(
        "/api/lens/analyses", headers=world["h"]["f2_admin"]).json()}
    assert {"Filed Quarvel read", "Unfiled Quarvel read"} <= texts


def test_the_media_route_is_cleared_and_unfiled_media_stays_with_its_author(world):
    assert not F.is_fenced(f"/uploads/{world['ids']['unfiled_image']}")
    h = world["h"]
    url = f"/uploads/{world['ids']['unfiled_image']}"
    assert client.get(url, headers=h["f2_admin"]).status_code == 404
    assert client.get(url, headers=h["f1_admin"]).status_code == 404
    mine = client.get(url, headers=h["f1_doctor"])
    assert mine.status_code == 200 and mine.content.endswith(b"clinical-lanes")


def test_the_media_route_with_one_facility_is_unchanged(world, one_facility):
    url = f"/uploads/{world['ids']['unfiled_image']}"
    assert client.get(url, headers=world["h"]["f2_admin"]).status_code == 200


def test_an_unfiled_scribe_draft_stays_with_its_author(world, fence_open):
    h = world["h"]
    note = world["ids"]["unfiled_note"]
    assert client.get(f"/api/scribe/notes/{note}", headers=h["f2_admin"]).status_code == 404
    assert client.get(f"/api/scribe/notes/{note}", headers=h["f1_doctor"]).status_code == 200


def test_an_unfiled_scribe_draft_with_one_facility_is_unchanged(world, fence_open, one_facility):
    note = world["ids"]["unfiled_note"]
    resp = client.get(f"/api/scribe/notes/{note}", headers=world["h"]["f2_admin"])
    assert resp.status_code == 200


def test_the_unseen_counts_keep_to_the_facility(world, fence_open):
    resp = client.get("/api/chart/unseen", headers=world["h"]["f2_admin"])
    assert resp.status_code == 200
    assert P1 not in resp.json()["counts"]


# ── reads by id ──────────────────────────────────────────────────────────────


def test_an_order_by_id_answers_404_across_the_boundary(world, fence_open):
    h = world["h"]
    resp = client.get(f"/api/orders/ORD{P1}", headers=h["f2_admin"])
    assert resp.status_code == 404 and resp.json()["detail"] == "Order not found"
    assert any(m.get("scope_cause") == "facility" for m in _denials(P1))
    assert client.get(f"/api/orders/ORD{P1}", headers=h["f1_admin"]).status_code == 200


def test_a_dose_across_the_boundary_is_refused_and_audited(world, fence_open):
    med = world["ids"][f"med_{P1}"]
    resp = client.post(
        f"/api/emar/medications/{med}/administer",
        headers=world["h"]["f2_nurse"],
        json={"status": "not_given", "not_given_reason": "Clinlane check"},
    )
    assert resp.status_code == 404 and resp.json()["detail"] == "Medication not found"
    facility_rows = [m for m in _denials(P1) if m.get("scope_helper") == "_enforce_medication_scope"]
    assert facility_rows and facility_rows[-1]["scope_cause"] == "facility"
    assert facility_rows[-1]["patient_facility_id"] == 1


def test_a_fhir_export_across_the_boundary_is_refused(world, fence_open):
    resp = client.get(f"/api/fhir/patient/{P1}/$everything", headers=world["h"]["f2_admin"])
    assert resp.status_code == 404
    assert NAME1 not in resp.text


# ── strict defaults ──────────────────────────────────────────────────────────


def test_the_ward_brief_keeps_each_reader_to_its_facility(world):
    from services import sage_narrator_service as sns

    narrator = sns.WardNarrator()
    # No facility scope given and no department: the shared snapshot spans
    # every facility, so the brief withholds itself.
    unknown = narrator.get_brief(None)
    assert unknown["snapshot"] is None and unknown["narrative"] is None
    # A ward reader is kept to the department's facility.
    ward = narrator.get_brief(F2_DEPT)
    assert {w["patient_id"] for w in ward["snapshot"]["watchlist"]} == {P2}
    # An explicit facility scope keeps the watchlist and the mix to it.
    f2 = narrator.get_brief(None, frozenset({F2}))
    assert {w["patient_id"] for w in f2["snapshot"]["watchlist"]} == {P2}
    assert f2["snapshot"]["census"] is None and f2["snapshot"]["recent_events"] == []
    assert f2["snapshot"]["scope"] == "Pranvoke Satellite"
    assert sum(f2["snapshot"]["risk_distribution"].values()) == 1
    # Group-wide (an enterprise account, or one facility): the whole brief.
    whole = narrator.get_brief(None, None)
    assert {P1, P2} <= {w["patient_id"] for w in whole["snapshot"]["watchlist"]}
    assert not any(k.startswith("_") for k in whole["snapshot"])


def test_the_ward_brief_with_one_facility_is_unchanged(world, one_facility):
    from services import sage_narrator_service as sns

    brief = sns.WardNarrator().get_brief(None)
    assert {P1, P2} <= {w["patient_id"] for w in brief["snapshot"]["watchlist"]}


def test_a_named_attending_at_another_facility_is_not_paged(world):
    from services import escalation_service

    with db_session() as db:
        ctx = escalation_service._patient_context(db, P1)
        assert ctx["attending"] == world["ids"]["f2_doctor"]
        target, words = escalation_service._derive_target(
            db, ctx, "consultant", "Consultant", None
        )
    # The tier falls through to the patient's own department's roster.
    assert target == world["ids"]["f1_doctor"]
    assert F1_DEPT in words


def test_a_named_attending_with_one_facility_is_paged_as_before(world, one_facility):
    from services import escalation_service

    with db_session() as db:
        ctx = escalation_service._patient_context(db, P1)
        target, _ = escalation_service._derive_target(db, ctx, "consultant", "Consultant", None)
    assert target == world["ids"]["f2_doctor"]


def test_shadow_episodes_keep_to_the_facility(world, monkeypatch):
    from routes import shadow_routes

    episodes = [
        {"episode_id": "EP1", "patient_id": P1, "ward": "x"},
        {"episode_id": "EP2", "patient_id": P2, "ward": "y"},
        {"episode_id": "EP3", "patient_id": None, "ward": "z"},
    ]

    def fake_episodes(**_kwargs):
        return {"episodes": [dict(e) for e in episodes], "total": 3}

    def fake_detail(episode_id):
        return next(
            ({"episode_id": e["episode_id"], "events": [e]} for e in episodes
             if e["episode_id"] == episode_id),
            None,
        )

    def fake_require(names, what):
        return fake_detail if "episode_detail" in names else fake_episodes

    monkeypatch.setattr(shadow_routes, "_require", fake_require)
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    h = world["h"]
    f2 = client.get("/api/admin/shadow/episodes", headers=h["f2_admin"])
    assert f2.status_code == 200, f2.text
    assert [e["episode_id"] for e in f2.json()["episodes"]] == ["EP2"]
    assert f2.json()["total"] is None
    assert client.get("/api/admin/shadow/episode/EP1", headers=h["f2_admin"]).status_code == 404
    assert client.get("/api/admin/shadow/episode/EP2", headers=h["f2_admin"]).status_code == 200
    group = client.get("/api/admin/shadow/episodes", headers=h["group_admin"])
    assert [e["episode_id"] for e in group.json()["episodes"]] == ["EP1", "EP2", "EP3"]
    assert group.json()["total"] == 3
