"""The pain score on the observation set (migration 87, founding ward item 1).

A 0 to 10 whole-number rating stored with each vitals set. Written by the nurse
at the bedside and by the ward clerk copying the paper sheet, printed with the
set, and never scored: it feeds no NEWS2, changes no band and raises no alert.
A blank box stays blank (NULL) and the rounds file counts it as a blank like
any other missing reading; a box the clerk cannot read is named illegible.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from auth import create_token
from database import db_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routes.nurse_routes import router as nurse_router
from services.hospital_time import hospital_timezone
from services.vigil_service import score_vitals_row
from ws import realtime

from services import nurse_shift_service, ward_record_service


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
    uid = _name("PAIN")
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Pain score {role}", role, department),
        )
    return uid


def _mk_patient(department, ward="PS-1", admitted_at=None):
    pid = _name("PAINP")
    admitted_at = admitted_at or (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('PAINADM', 'Pain score admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, 'Pain score fixture', ?, '3', ?, 64, 'M', 'Fixture',
                       'admitted', ?)""",
            (pid, f"MRN-{pid}", ward, admitted_at),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
            "admitted_by, created_at, admitted_at) "
            "VALUES (?, ?, ?, ?, '3', 'active', 'PAINADM', ?, ?)",
            (f"ADM-{pid}", pid, department, ward, admitted_at, admitted_at),
        )
    return pid


def _row(vitals_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM vitals WHERE id = ?", (vitals_id,)).fetchone())


def _audit_metadata(vitals_id):
    import json

    with db_session() as db:
        row = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE action_type = 'observation_recorded' "
            "AND target_type = 'vitals' AND target_id = ? ORDER BY id DESC LIMIT 1",
            (str(vitals_id),),
        ).fetchone()
    assert row is not None
    return json.loads(row["metadata_json"])


_SET = {
    "heart_rate": 88,
    "systolic_bp": 128,
    "diastolic_bp": 80,
    "respiratory_rate": 16,
    "temperature": 37.1,
    "spo2": 97,
    "consciousness": "alert",
    "on_oxygen": False,
}


# ── The column ────────────────────────────────────────────────────────────────
def test_migration_87_adds_the_column_once(tmp_path):
    import sqlite3

    from migrations_his.m0087_vitals_pain_score import MIGRATION

    assert MIGRATION.version == 87
    conn = sqlite3.connect(str(tmp_path / "m87.db"))
    conn.execute("CREATE TABLE vitals (id INTEGER PRIMARY KEY, patient_id TEXT, timestamp TEXT)")
    MIGRATION.apply(conn)
    MIGRATION.apply(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(vitals)").fetchall()]
    assert cols.count("pain_score") == 1
    conn.close()


def test_the_test_database_has_the_column():
    with db_session() as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(vitals)").fetchall()}
    assert "pain_score" in cols


# ── Stored with the set, by nurse and by clerk ────────────────────────────────
def test_a_nurse_set_stores_the_pain_score_and_audits_it(client):
    dept = _name("Pain Medicine ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept)
    r = client.post(
        f"/api/nurse/patients/{pid}/observations",
        json={**_SET, "pain_score": 7},
        headers=_auth(nurse, "nurse"),
    )
    assert r.status_code == 200, r.text
    row = _row(r.json()["vitals_id"])
    assert row["pain_score"] == 7
    assert _audit_metadata(row["id"])["pain_score"] == 7


def test_a_clerk_copies_the_pain_score_from_the_sheet(client):
    dept = _name("Pain Clerk ")
    pid = _mk_patient(dept)
    clerk = _mk_user("ward_clerk", dept)
    sheet_time = (datetime.now(UTC) - timedelta(hours=5)).replace(microsecond=0)
    r = client.post(
        f"/api/nurse/patients/{pid}/observations",
        json={
            **_SET,
            "consciousness": None,
            "on_oxygen": None,
            "pain_score": 3,
            "taken_at": sheet_time.isoformat(),
        },
        headers=_auth(clerk, "ward_clerk"),
    )
    assert r.status_code == 200, r.text
    row = _row(r.json()["vitals_id"])
    assert row["source"] == "clerk_transcribed"
    assert row["pain_score"] == 3


def test_a_blank_pain_box_is_stored_empty(client):
    dept = _name("Pain Blank ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept)
    r = client.post(
        f"/api/nurse/patients/{pid}/observations", json=_SET, headers=_auth(nurse, "nurse")
    )
    assert r.status_code == 200, r.text
    row = _row(r.json()["vitals_id"])
    assert row["pain_score"] is None
    assert _audit_metadata(row["id"])["pain_score"] is None


def test_an_illegible_pain_box_is_stored_empty_and_named(client):
    dept = _name("Pain Illegible ")
    pid = _mk_patient(dept)
    clerk = _mk_user("ward_clerk", dept)
    sheet_time = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    r = client.post(
        f"/api/nurse/patients/{pid}/observations",
        json={
            **_SET,
            "pain_score": 9,
            "illegible": ["pain_score"],
            "taken_at": sheet_time.isoformat(),
        },
        headers=_auth(clerk, "ward_clerk"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["illegible"] == ["pain_score"]
    row = _row(r.json()["vitals_id"])
    assert row["pain_score"] is None, "a number never travels with the illegible mark"
    assert row["illegible"] == "pain_score"


@pytest.mark.parametrize("bad", [11, -1, 7.5, "seven"])
def test_a_pain_score_off_the_scale_is_refused_by_name(client, bad):
    dept = _name("Pain Range ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept)
    r = client.post(
        f"/api/nurse/patients/{pid}/observations",
        json={**_SET, "pain_score": bad},
        headers=_auth(nurse, "nurse"),
    )
    assert r.status_code == 422, r.text
    assert "pain_score" in r.text
    with db_session() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM vitals WHERE patient_id = ?", (pid,)).fetchone()[0]
            == 0
        )


def test_the_pain_score_counts_as_a_reading_on_its_own():
    cleaned = nurse_shift_service._clean_obs({"pain_score": 4})
    assert cleaned["pain_score"] == 4
    with pytest.raises(ValueError, match="at least one reading"):
        nurse_shift_service._clean_obs({"pain_score": ""})


# ── Never scored ──────────────────────────────────────────────────────────────
def test_the_pain_score_never_changes_the_news2(client):
    dept = _name("Pain NEWS2 ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept)
    deranged = {**_SET, "respiratory_rate": 26, "spo2": 90}
    without = client.post(
        f"/api/nurse/patients/{pid}/news2-preview", json=deranged, headers=_auth(nurse, "nurse")
    )
    with_pain = client.post(
        f"/api/nurse/patients/{pid}/news2-preview",
        json={**deranged, "pain_score": 10},
        headers=_auth(nurse, "nurse"),
    )
    assert without.status_code == 200 and with_pain.status_code == 200
    assert with_pain.json()["news2"] == without.json()["news2"]
    assert with_pain.json()["schedule"] == without.json()["schedule"]
    assert with_pain.json()["observation"]["pain_score"] == 10

    # The shared scorer reads the NEWS2 parameters by name and nothing else.
    row = {**_SET, "pain_score": 10}
    assert score_vitals_row(row, on_scale_2=False, overlay=False) == score_vitals_row(
        {**_SET, "pain_score": None}, on_scale_2=False, overlay=False
    )
    assert "pain" not in str(score_vitals_row(row, on_scale_2=False, overlay=False)).lower()


def test_a_resubmission_with_a_changed_pain_score_is_a_correction_not_a_replay(client):
    dept = _name("Pain Replay ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept)
    taken_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    first = client.post(
        f"/api/nurse/patients/{pid}/observations",
        json={**_SET, "pain_score": 2, "taken_at": taken_at},
        headers=_auth(nurse, "nurse"),
    )
    again = client.post(
        f"/api/nurse/patients/{pid}/observations",
        json={**_SET, "pain_score": 2, "taken_at": taken_at},
        headers=_auth(nurse, "nurse"),
    )
    changed = client.post(
        f"/api/nurse/patients/{pid}/observations",
        json={**_SET, "pain_score": 8, "taken_at": taken_at},
        headers=_auth(nurse, "nurse"),
    )
    assert first.status_code == again.status_code == changed.status_code == 200
    assert again.json()["replayed"] is True
    assert again.json()["vitals_id"] == first.json()["vitals_id"]
    assert changed.json()["replayed"] is False
    assert changed.json()["supersedes_vitals_id"] == first.json()["vitals_id"]


# ── Read back wherever the set is read ────────────────────────────────────────
def test_the_wire_payload_carries_the_pain_score():
    row = {"patient_id": "P1", "timestamp": "2026-10-21T09:00:00+00:00", **_SET, "pain_score": 6}
    assert realtime.vitals_update_payload(row, None, source="nurse_manual")["pain_score"] == 6
    bare = {k: v for k, v in row.items() if k != "pain_score"}
    assert realtime.vitals_update_payload(bare, None, source="monitor_hl7")["pain_score"] is None


def test_the_clerks_own_copies_list_the_pain_score(client):
    dept = _name("Pain Copies ")
    pid = _mk_patient(dept)
    clerk = _mk_user("ward_clerk", dept)
    sheet_time = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)
    r = client.post(
        f"/api/nurse/patients/{pid}/observations",
        json={**_SET, "pain_score": 5, "taken_at": sheet_time.isoformat()},
        headers=_auth(clerk, "ward_clerk"),
    )
    assert r.status_code == 200, r.text
    copies = client.get(
        f"/api/nurse/transcribe/patients/{pid}/my-copies", headers=_auth(clerk, "ward_clerk")
    )
    assert copies.status_code == 200, copies.text
    assert [c["readings"]["pain_score"] for c in copies.json()["copies"]] == [5]


def test_the_downtime_pack_prints_the_pain_score_beside_the_set():
    from services.downtime_service import _render_vitals_news2, capture_snapshot

    dept = _name("Pain Pack ")
    pid = _mk_patient(dept)
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen, pain_score)
               VALUES (?, '2026-01-02T09:00:00+00:00', 80, 120, 70, 97, 37.0, 16,
                       'alert', 0, 4)""",
            (pid,),
        )
    snap = capture_snapshot(pid, created_by="auto")["snapshot"]
    assert snap["latest_vitals"]["pain_score"] == 4
    html = _render_vitals_news2(snap)
    assert "<th>Pain</th>" in html
    assert "<td>4/10</td>" in html
    # A snapshot captured before the column existed prints the dash, not an error.
    old = dict(snap)
    old["latest_vitals"] = {k: v for k, v in snap["latest_vitals"].items() if k != "pain_score"}
    assert "<th>Pain</th>" in _render_vitals_news2(old)
    assert "4/10" not in _render_vitals_news2(old)


def test_the_rounds_file_counts_a_blank_pain_box_and_an_illegible_one(client):
    tz = hospital_timezone()
    dept = _name("Pain Rounds ")
    ward = _name("PAIN-WARD-")
    day = (datetime.now(tz) - timedelta(days=2)).date()
    admitted = datetime(day.year, day.month, day.day, 8, 0, tzinfo=tz)
    pid = _mk_patient(dept, ward, admitted.astimezone(UTC).isoformat())
    clerk = _mk_user("ward_clerk", dept)
    ward_record_service.set_setting(ward, interval_minutes=240, grace_minutes=30, set_by="NS")

    full = {**_SET, "consciousness": "alert", "on_oxygen": False}
    first = admitted + timedelta(hours=4)
    second = admitted + timedelta(hours=8)
    third = admitted + timedelta(hours=12)
    for when, extra in (
        (first, {"pain_score": 2}),
        (second, {}),
        (third, {"illegible": ["pain_score"]}),
    ):
        r = client.post(
            f"/api/nurse/patients/{pid}/observations",
            json={**full, **extra, "taken_at": when.isoformat()},
            headers=_auth(clerk, "ward_clerk"),
        )
        assert r.status_code == 200, r.text

    data = ward_record_service.compute_rounds(
        ward, day.isoformat(), day.isoformat(), now=datetime.now(UTC)
    )
    readings = {r["field"]: r for r in data["readings"]}
    assert readings["pain_score"]["reading"] == "Pain (0 to 10)"
    assert readings["pain_score"]["blank"] == 1
    assert readings["pain_score"]["illegible"] == 1
    assert data["vitals_columns"]["pain_score"] is True
    labels = [
        label
        for label, _ in ward_record_service.header_lines(
            data, reference="PAIN-T", generated_at="now", generated_by="T"
        )
    ]
    assert "Pain (0 to 10)" not in labels, "the column exists, so no caveat line"
    # The file carries no score word: the reading is named "Pain (0 to 10)".
    text = ward_record_service.render_markdown(
        data, reference="PAIN-T", generated_at="now", generated_by="T"
    ).lower()
    assert "score" not in text.replace(ward_record_service.CLOSING_LINE.lower(), "")


def test_the_rounds_file_counts_nothing_for_pain_on_a_database_without_the_column():
    """A database a migration has not reached yet has no pain column: the file
    counts no blank for it and its header says so, rather than counting every
    set as missing a reading it could never hold."""
    import sqlite3

    tz = hospital_timezone()
    day = datetime(2025, 5, 10, tzinfo=tz)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE vitals (id INTEGER PRIMARY KEY, patient_id TEXT, timestamp TEXT, "
        "heart_rate REAL, systolic_bp REAL, diastolic_bp REAL, spo2 REAL, temperature REAL, "
        "respiratory_rate REAL, consciousness TEXT, on_oxygen INTEGER, source TEXT)"
    )
    conn.execute(
        "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp, "
        "spo2, temperature, respiratory_rate, consciousness, on_oxygen, source) VALUES "
        "('P', ?, 80, 120, 70, 97, 37.0, 16, 'alert', 0, 'clerk_transcribed')",
        ((day + timedelta(hours=12)).astimezone(UTC).isoformat(),),
    )
    cols = ward_record_service._table_columns(conn, "vitals")
    assert "pain_score" not in cols
    sets = ward_record_service._load_sets(
        conn,
        "P",
        day.astimezone(UTC),
        (day + timedelta(days=1)).astimezone(UTC),
        cols,
    )
    assert len(sets) == 1 and "pain_score" not in sets[0]
    data = {
        "setting": {
            "interval_minutes": 240,
            "grace_minutes": 30,
            "set_by": "NS",
            "set_at": "2025-05-01T00:00:00+00:00",
            "note": None,
        },
        "shifts": {"source": "fallback", "labels": ["08:00 to 20:00"]},
        "ward": "W",
        "period": {"start_date": "2025-05-10", "end_date": "2025-05-10"},
        "totals": {"due": 0, "done": 0, "late": 0, "missed": 0, "open": 0},
        "patients": 0,
        "sets_counted": 0,
        "include_patient_identifiers": False,
        "vitals_columns": {c: c in cols for c in ward_record_service._OPTIONAL_VITALS_COLUMNS},
    }
    labels = dict(
        ward_record_service.header_lines(
            data, reference="PAIN-T", generated_at="now", generated_by="T"
        )
    )
    assert labels["Pain (0 to 10)"] == "this system does not record a pain rating yet"


def test_the_chart_and_the_sparkline_routes_carry_the_pain_score(client):
    """Found in the browser on 21 October 2026: both chart serializers list
    the vitals columns by name, so the stored score never reached the bar."""
    from routes.vigil_routes import router as vigil_router

    dept = _name("Pain Chart ")
    pid = _mk_patient(dept)
    nurse = _mk_user("nurse", dept)
    doctor = _mk_user("doctor", dept)
    r = client.post(
        f"/api/nurse/patients/{pid}/observations",
        json={**_SET, "pain_score": 6},
        headers=_auth(nurse, "nurse"),
    )
    assert r.status_code == 200, r.text

    app = FastAPI()
    app.include_router(vigil_router)
    chart_client = TestClient(app)
    chart = chart_client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor, "doctor"))
    assert chart.status_code == 200, chart.text
    assert chart.json()["vitals"][0]["pain_score"] == 6
    recent = chart_client.get(f"/api/vigil/patients/{pid}/vitals", headers=_auth(doctor, "doctor"))
    assert recent.status_code == 200, recent.text
    assert [row["pain_score"] for row in recent.json()] == [6]
