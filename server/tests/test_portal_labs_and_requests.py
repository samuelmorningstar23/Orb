"""Patient portal: Lab History and follow-up requests (gap register F079).

LAB HISTORY. GET /api/bridge/reports returned lab results only inside
clinical_documents, and the portal built Lab History from imaging_reports, so a
patient whose results were charted read "No lab or imaging reports yet" while
Report History listed the same results under the internal type "lab_result".
Lab results now come back under `labs`, Report History no longer repeats them,
the lens copy of a lab panel does not appear twice, and every document carries
its type in plain words (`type_label`).

FOLLOW-UP REQUESTS. The portal posted to the staff route POST /api/appointments,
which saved the patient's own booking as origin='staff', so the staff list and
the patient's view both showed an ask as a booking the care team had made. A
patient booking through that route is now stored as a request, the same as
POST /api/bridge/appointments; a clinician's booking is unchanged.
"""
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from routes.appointments_routes import PATIENT_REQUEST_TYPE

client = TestClient(app)

PATIENT_USER = "PLR-PAT"
PATIENT_ID = "PAT-PLR-1"
DOCTOR = "PLR-DOC"
DAY = (date.today() + timedelta(days=3)).isoformat()

LAB_TEXT = "Complete Blood Count\nHb 11.2 g/dL\nWBC 12.4"


def _setup():
    """One patient with a portal login and one clinician with no department,
    so ward scoping cannot mask a result. The patient is discharged so no ward
    board in another test counts them, and discharged NOW: Bridge stays open
    only until the end of the discharge day (ruling R029), and a discharge with
    no time on record closes it. Rows from an earlier run are removed."""
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'patient', 'x')",
            (PATIENT_USER, "Portal Labs Test"),
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'doctor', 'x')",
            (DOCTOR, "Portal Labs Doctor"),
        )
        db.execute(
            """INSERT OR IGNORE INTO patients (id, user_id, name, mrn, age, sex, ward, bed, diagnosis, status)
               VALUES (?, ?, 'Portal Labs Test', 'MRN-PLR-1', 50, 'F', 'GM', 'B-9', 'Test', 'discharged')""",
            (PATIENT_ID, PATIENT_USER),
        )
        for table in ("clinical_documents", "lens_analyses", "appointments", "discharge_summaries"):
            db.execute(f"DELETE FROM {table} WHERE patient_id = ?", (PATIENT_ID,))
        db.execute(
            "INSERT INTO discharge_summaries (patient_id, author_id, summary) VALUES (?, ?, 'Test.')",
            (PATIENT_ID, DOCTOR),
        )


def _auth(user_id: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _patient() -> dict:
    return _auth(PATIENT_USER, "patient")


def _chart_labs_and_notes():
    with db_session() as db:
        db.executemany(
            """INSERT INTO clinical_documents (patient_id, doc_type, title, content, author_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (PATIENT_ID, "lab_result", "Complete Blood Count", LAB_TEXT, DOCTOR, "2026-10-01T06:00:00+00:00"),
                (PATIENT_ID, "note", "Progress Note: Day 2", "Stable overnight.", DOCTOR, "2026-10-01T08:00:00+00:00"),
                (PATIENT_ID, "Ward Round", "Manual Entry", "Seen on the round.", DOCTOR, "2026-10-01T09:00:00+00:00"),
            ],
        )
        # The copy the lab system also writes for the staff timeline, plus a
        # real X-ray a clinician reviewed and filed (migration 45).
        db.executemany(
            """INSERT INTO lens_analyses (patient_id, author_id, image_path, image_type, clinical_context, analysis,
                                          created_at, reviewed_by, reviewed_at, reviewed_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (PATIENT_ID, DOCTOR, "", "lab", "Lab: Complete Blood Count", LAB_TEXT, "2026-10-01T06:00:00+00:00",
                 None, None, None),
                (PATIENT_ID, DOCTOR, "/uploads/cxr.png", "xray", "Chest", "Raw model read.", "2026-10-01T07:00:00+00:00",
                 DOCTOR, "2026-10-01T07:10:00+00:00", "No consolidation."),
            ],
        )


# ── Lab History ───────────────────────────────────────────────────────────────

def test_lab_results_come_back_under_labs_in_plain_words():
    _setup()
    _chart_labs_and_notes()
    r = client.get("/api/bridge/reports", headers=_patient())
    assert r.status_code == 200, r.text
    body = r.json()

    assert [(lab["title"], lab["content"], lab["type_label"]) for lab in body["labs"]] == [
        ("Complete Blood Count", LAB_TEXT, "Lab result"),
    ]
    # Report History no longer repeats the result, and names each type in words.
    docs = body["clinical_documents"]
    assert all(d["doc_type"] != "lab_result" for d in docs)
    assert {d["title"]: d["type_label"] for d in docs} == {
        "Progress Note: Day 2": "Clinical note",
        "Manual Entry": "Ward round note",
    }


def test_the_lens_copy_of_a_lab_panel_is_not_listed_twice_and_imaging_stays():
    _setup()
    _chart_labs_and_notes()
    body = client.get("/api/bridge/reports", headers=_patient()).json()
    assert [(i["image_type"], i["clinical_context"]) for i in body["imaging_reports"]] == [("xray", "Chest")]


def test_the_reviewed_text_reaches_the_patient_never_the_raw_read():
    _setup()
    _chart_labs_and_notes()
    body = client.get("/api/bridge/reports", headers=_patient()).json()
    texts = [i["analysis"] for i in body["imaging_reports"]]
    assert texts == ["No consolidation."]
    assert "Raw model read." not in json.dumps(body)


def test_an_unreviewed_lens_read_is_not_shown_to_the_patient():
    """F041: the model's draft reached the portal unread. An imaging row with
    no clinician review, and a 'lab' row (the lab bot's copy of a lab report,
    never an image and never reviewed), are both withheld."""
    _setup()
    with db_session() as db:
        db.executemany(
            """INSERT INTO lens_analyses (patient_id, author_id, image_path, image_type, clinical_context, analysis)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (PATIENT_ID, DOCTOR, "/uploads/ct.png", "ct", "Abdomen", "Unreviewed draft"),
                (PATIENT_ID, DOCTOR, "/uploads/lft.png", "lab", "Liver panel", "ALT 40"),
            ],
        )
    body = client.get("/api/bridge/reports", headers=_patient()).json()
    assert body["imaging_reports"] == []
    assert "Unreviewed draft" not in json.dumps(body)


def test_document_labels_cover_unknown_and_blank_types():
    from routes.bridge_routes import _document_label

    assert _document_label("lab_result") == "Lab result"
    assert _document_label("Progress Note") == "Progress note"
    assert _document_label("imported_document") == "Imported record"
    assert _document_label("operative_NOTE") == "Operative note"
    assert _document_label("") == "Clinical note"
    assert _document_label(None) == "Clinical note"


# ── Follow-up requests ────────────────────────────────────────────────────────

def test_a_patient_booking_on_the_staff_route_is_stored_as_a_request():
    _setup()
    r = client.post(
        "/api/appointments",
        json={"scheduled_date": DAY, "scheduled_time": "10:00",
              "appointment_type": "Cardio", "reason": "Discuss my tablets"},
        headers=_patient(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["origin"] == "patient"
    assert r.json()["appointment_type"] == PATIENT_REQUEST_TYPE
    assert r.json()["reason"] == "Patient-requested follow-up. Asked for: Cardio. Discuss my tablets"

    mine = client.get("/api/bridge/appointments", headers=_patient()).json()["appointments"]
    assert len(mine) == 1 and mine[0]["requested_by_patient"] is True
    assert "nobody has agreed" in mine[0]["status_note"].lower()

    with db_session() as db:
        audit = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE target_id = ? AND action_type = 'appointment_booked'",
            (r.json()["id"],),
        ).fetchone()
    assert audit is not None and json.loads(audit["metadata_json"])["origin"] == "patient"


def test_a_clinician_booking_stays_a_care_team_booking():
    _setup()
    r = client.post(
        "/api/appointments",
        json={"patient_id": PATIENT_ID, "scheduled_date": DAY, "scheduled_time": "11:00",
              "appointment_type": "Follow-up"},
        headers=_auth(DOCTOR, "doctor"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["origin"] == "staff"
    assert r.json()["appointment_type"] == "Follow-up"


def test_the_portal_request_route_keeps_the_department():
    _setup()
    r = client.post(
        "/api/bridge/appointments",
        json={"requested_date": DAY, "requested_time": "14:00", "department": "Cardiology"},
        headers=_patient(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["requested_by_patient"] is True
    assert r.json()["department"] == "Cardiology"
