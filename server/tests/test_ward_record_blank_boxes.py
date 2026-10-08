"""Blank consciousness and oxygen boxes (Orb Ward Record, 9 October 2026).

A paper observation sheet can leave the consciousness (ACVPU) box or the air
or oxygen box empty, and the monthly rounds file counts a blank reading
(docs/fundraising/SALES_PLAN_2026-10.md section 3, step 2). A standing rule
says an unassessed ACVPU or oxygen is never defaulted to alert or to room air.
The request body, the service's cleaning and the entry sheet used to fill both
in, so a blank could never reach the database and the file carried a
disclaimer saying so.

Now:

* the clerk's set with an empty box stores NULL, and the rounds file counts it
  as blank;
* a nurse at the bedside must choose both, and a set without one is refused in
  words she can act on;
* a NULL ACVPU or oxygen scores as an incomplete set, a minimum, on the record
  path, the stored row and the nurse's board, never as a complete set on air.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from routes.nurse_routes import ObservationRequest, router as nurse_router
from services import nurse_shift_service, ward_record_service
from services.hospital_time import hospital_timezone
from services.news2_text import INCOMPLETE, news2_description
from services.vigil_service import score_vitals_row


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(nurse_router)
    return TestClient(app)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


def _name(prefix):
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _mk_user(role, department):
    uid = _name("WRB")
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Blank box {role}", role, department),
        )
    return uid


def _mk_patient(department, ward, admitted_at):
    pid = _name("WRBP")
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('WRBADM', 'Blank box admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, 'Blank box fixture', ?, '4', ?, 61, 'F', 'Fixture',
                       'admitted', ?)""",
            (pid, f"MRN-{pid}", ward, admitted_at),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
            "admitted_by, created_at, admitted_at) "
            "VALUES (?, ?, ?, ?, '4', 'active', 'WRBADM', ?, ?)",
            (f"ADM-{pid}", pid, department, ward, admitted_at, admitted_at),
        )
    return pid


def _row(vitals_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM vitals WHERE id = ?", (vitals_id,)).fetchone())


_NUMBERS = {"heart_rate": 84, "systolic_bp": 126, "diastolic_bp": 78,
            "respiratory_rate": 17, "temperature": 37.0, "spo2": 96}


# ── The request and the cleaning no longer fill an empty box in ───────────────
def test_the_request_body_leaves_both_boxes_empty():
    body = ObservationRequest(heart_rate=80).model_dump()
    assert body["consciousness"] is None
    assert body["on_oxygen"] is None
    cleaned = nurse_shift_service._clean_obs(body)
    assert cleaned["consciousness"] is None
    assert cleaned["on_oxygen"] is None


def test_a_chosen_alert_and_air_are_kept_as_chosen():
    cleaned = nurse_shift_service._clean_obs(
        {"heart_rate": 80, "consciousness": "Alert", "on_oxygen": False})
    assert cleaned["consciousness"] == "alert"
    assert cleaned["on_oxygen"] is False


def test_a_set_with_nothing_at_all_is_still_refused():
    with pytest.raises(ValueError, match="at least one reading"):
        nurse_shift_service._clean_obs({})


# ── The clerk: an empty box is stored empty and counted blank ─────────────────
def test_a_clerk_set_with_no_acvpu_stores_null_and_the_rounds_file_counts_it_blank(client):
    tz = hospital_timezone()
    dept = _name("Blank Box Medicine ")
    ward = _name("WRB-WARD-")
    day = (datetime.now(tz) - timedelta(days=2)).date()
    admitted = datetime(day.year, day.month, day.day, 8, 0, tzinfo=tz)
    pid = _mk_patient(dept, ward, admitted.astimezone(timezone.utc).isoformat())
    clerk = _mk_user("ward_clerk", dept)
    ward_record_service.set_setting(ward, interval_minutes=240, grace_minutes=30,
                                    set_by="NS")

    sheet_time = admitted + timedelta(hours=4)
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_NUMBERS, "taken_at": sheet_time.isoformat()},
                    headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200, r.text
    row = _row(r.json()["vitals_id"])
    assert row["source"] == "clerk_transcribed"
    assert row["consciousness"] is None
    assert row["on_oxygen"] is None

    data = ward_record_service.compute_rounds(ward, day.isoformat(), day.isoformat(),
                                              now=datetime.now(timezone.utc))
    blank = {r["field"]: r["blank"] for r in data["readings"]}
    assert blank["consciousness"] == 1
    assert blank["on_oxygen"] == 1
    assert blank["heart_rate"] == 0
    assert data["totals"]["blank_readings"] == 2
    labels = [label for label, _ in ward_record_service.header_lines(
        data, reference="WR-T", generated_at="now", generated_by="T")]
    assert "Consciousness and air or oxygen" not in labels


def test_the_clerk_may_send_an_empty_box_explicitly(client):
    dept = _name("Blank Box Surgery ")
    pid = _mk_patient(dept, _name("WRB-W-"), datetime.now(timezone.utc).isoformat())
    clerk = _mk_user("ward_clerk", dept)
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_NUMBERS, "consciousness": None, "on_oxygen": None},
                    headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200, r.text
    row = _row(r.json()["vitals_id"])
    assert row["consciousness"] is None and row["on_oxygen"] is None


# ── The nurse at the bedside must choose both ─────────────────────────────────
@pytest.mark.parametrize("missing,words", [
    ({"on_oxygen": False}, "Choose the patient's level of consciousness (ACVPU), then save again."),
    ({"consciousness": "alert"}, "Choose whether the patient is on air or on oxygen, then save again."),
    ({}, "Choose the patient's level of consciousness (ACVPU) and whether they are on air "
         "or on oxygen, then save again."),
])
def test_a_nurse_set_without_a_choice_is_refused_in_plain_words(client, missing, words):
    dept = _name("Blank Box Ward ")
    pid = _mk_patient(dept, _name("WRB-W-"), datetime.now(timezone.utc).isoformat())
    nurse = _mk_user("nurse", dept)
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_NUMBERS, **missing}, headers=_auth(nurse, "nurse"))
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == words
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM vitals WHERE patient_id = ?", (pid,)).fetchone()[0]
    assert n == 0


def test_a_nurse_out_of_range_reading_is_named_before_a_missing_choice(client):
    dept = _name("Blank Box Ward ")
    pid = _mk_patient(dept, _name("WRB-W-"), datetime.now(timezone.utc).isoformat())
    nurse = _mk_user("nurse", dept)
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={"heart_rate": 900}, headers=_auth(nurse, "nurse"))
    assert r.status_code == 422
    assert "heart_rate" in r.json()["detail"]


def test_a_nurse_set_with_both_choices_is_charted(client):
    dept = _name("Blank Box Ward ")
    pid = _mk_patient(dept, _name("WRB-W-"), datetime.now(timezone.utc).isoformat())
    nurse = _mk_user("nurse", dept)
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_NUMBERS, "consciousness": "alert", "on_oxygen": False},
                    headers=_auth(nurse, "nurse"))
    assert r.status_code == 200, r.text
    row = _row(r.json()["vitals_id"])
    assert row["consciousness"] == "alert" and row["on_oxygen"] == 0
    assert r.json()["news2"]["complete"] is True


# ── A NULL box scores as an incomplete set, a minimum, never as air ───────────
def test_a_null_oxygen_scores_as_incomplete_never_as_air(client):
    dept = _name("Blank Box Ward ")
    pid = _mk_patient(dept, _name("WRB-W-"), datetime.now(timezone.utc).isoformat())
    clerk = _mk_user("ward_clerk", dept)
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_NUMBERS, "consciousness": "alert"},
                    headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200, r.text
    news2 = r.json()["news2"]
    # The record path.
    assert news2["complete"] is False
    assert "Supplemental O2" in news2["missing_parameters"]
    assert "Consciousness (ACVPU)" not in news2["missing_parameters"]
    assert news2_description(news2).endswith(INCOMPLETE)

    # The stored row, through the shared scoring entry point every reader uses.
    stored = _row(r.json()["vitals_id"])
    assert stored["on_oxygen"] is None
    again = score_vitals_row(stored, on_scale_2=False, overlay=False)
    assert again["complete"] is False
    assert "Supplemental O2" in again["missing_parameters"]

    # The nurse's shift board for that ward.
    board = nurse_shift_service.build_board(dept)
    (card,) = [c for c in board["patients"] if c["patient_id"] == pid]
    assert card["news2"]["complete"] is False
    assert "Supplemental O2" in card["news2"]["missing_parameters"]


def test_a_null_acvpu_scores_as_incomplete_never_as_alert(client):
    dept = _name("Blank Box Ward ")
    pid = _mk_patient(dept, _name("WRB-W-"), datetime.now(timezone.utc).isoformat())
    clerk = _mk_user("ward_clerk", dept)
    r = client.post(f"/api/nurse/patients/{pid}/observations",
                    json={**_NUMBERS, "on_oxygen": False},
                    headers=_auth(clerk, "ward_clerk"))
    assert r.status_code == 200, r.text
    news2 = r.json()["news2"]
    assert news2["complete"] is False
    assert "Consciousness (ACVPU)" in news2["missing_parameters"]
    assert news2_description(news2).endswith(INCOMPLETE)


def test_the_preview_of_a_half_chosen_set_says_minimum(client):
    dept = _name("Blank Box Ward ")
    pid = _mk_patient(dept, _name("WRB-W-"), datetime.now(timezone.utc).isoformat())
    nurse = _mk_user("nurse", dept)
    r = client.post(f"/api/nurse/patients/{pid}/news2-preview",
                    json={"respiratory_rate": 24}, headers=_auth(nurse, "nurse"))
    assert r.status_code == 200, r.text
    news2 = r.json()["news2"]
    assert news2["complete"] is False
    assert {"Consciousness (ACVPU)", "Supplemental O2"} <= set(news2["missing_parameters"])
    assert r.json()["observation"]["consciousness"] is None
    assert r.json()["observation"]["on_oxygen"] is None


def test_a_replay_of_an_empty_box_is_not_matched_to_a_chosen_one():
    """The replay guard compared an empty stored box with a chosen 'alert' as
    the same reading, because both used to read as alert."""
    row = {"heart_rate": 80.0, "systolic_bp": None, "diastolic_bp": None,
           "spo2": None, "temperature": None, "respiratory_rate": None,
           "consciousness": None, "on_oxygen": None}
    chosen = nurse_shift_service._clean_obs(
        {"heart_rate": 80, "consciousness": "alert", "on_oxygen": False})
    assert nurse_shift_service._same_reading(row, chosen) is False
    empty = nurse_shift_service._clean_obs({"heart_rate": 80})
    assert nurse_shift_service._same_reading(row, empty) is True
