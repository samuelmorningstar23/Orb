"""One stored spelling for `vitals.timestamp` and `risk_scores.timestamp`.

Both columns are ordered as TEXT, and text order is only time order while every
row is spelled the same way. Verified 2026-10-02/03 through the real writers and
readers, ruled by Samuel 2026-10-03 (docs/TIMESTAMP_FORMAT_PLAN_2026-10-03.md):

  * A benign nurse set charted at 13:26 UTC (ISO, 'T') outranked a critical
    ingestion score written at 19:26 (SQLite CURRENT_TIMESTAMP, ' '), because
    'T' sorts above ' '. The ward's "latest risk" said stable all evening.
  * Device times were stored verbatim. A '+05:30' reading from two hours ago
    outranked a 'Z' reading from five minutes ago, and an offset-less reading
    (IST wall clock) was read as UTC everywhere, 5.5 h in the future.
    julianday() at the readers cannot fix that one; only the writer can.
  * `timestamp > datetime('now', ?)` let a 7-hour-old ISO row into a 1-hour
    window.

The fix is on the WRITE side (services/clinical_time.py, migration
`canonical_vitals_risk_timestamps`); the readers keep plain text ORDER BY. So
these tests go through the real writers, or through the backfill. A test that
hand-inserted mixed rows and asked a reader for "latest" would stay red for
ever under this design, by design.

All 13 tests failed on the code as it stood before the fix (run 2026-10-03,
19:49 UTC), each on the defect it names; the backfill tests because the
migration did not exist. Two of them depend on the UTC clock, see
`_earlier_same_utc_day`.
"""
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import command_center_service, ingestion_service, nurse_shift_service, vigil_service
from services.clinical_time import to_utc_iso
from services.vitals_source import hl7v2

client = TestClient(app)

IST = timezone(timedelta(hours=5, minutes=30))
CANONICAL = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")

# Every vital in its 0-point NEWS2 band.
BENIGN_OBS = dict(heart_rate=72, systolic_bp=124, diastolic_bp=78, spo2=98,
                  temperature=36.8, respiratory_rate=14, consciousness="alert",
                  on_oxygen=False)

_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _appliance_in_india():
    """State the appliance zone out loud: an offset-less device time is IST."""
    hl7v2.set_default_timezone("Asia/Kolkata")
    yield
    hl7v2.set_default_timezone(None)


@pytest.fixture(autouse=True)
def _no_checkpoint(monkeypatch):
    """Today's shipping state, made explicit: no CfC checkpoint loaded."""
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", lambda _pid: None)


@pytest.fixture(autouse=True)
def _discharge_created():
    """Leave no admitted patient behind: the FHIR self-export sleeps per
    admitted patient, so a leftover slows every later export test."""
    yield
    if not _CREATED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _CREATED])
        db.executemany("UPDATE admissions SET status = 'discharged' "
                       "WHERE patient_id = ? AND status = 'active'",
                       [(p,) for p in _CREATED])
    _CREATED.clear()


def _ward():
    """A doctor alone in a fresh department, so only this test's patient is on
    the board."""
    dept = f"Ts Canon {uuid.uuid4().hex[:8]}"
    uid = f"TSU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Ts Canon Doctor', 'doctor', 'x', ?)",
            (uid, dept),
        )
    return dept, uid, {"Authorization": f"Bearer {create_token(uid, 'doctor')}"}


def _patient(dept=None, admitted_by=None):
    pid = f"TSP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, status)
               VALUES (?, ?, ?, ?, ?, 60, 'F', 'Timestamp test', 'admitted')""",
            (pid, f"Ts {pid}", f"MRN-{pid}", f"B-{pid[-4:]}", dept or "General"),
        )
        if dept:
            db.execute(
                "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
                "VALUES (?, ?, ?, ?, 'active', ?)",
                (pid, dept, dept, f"B-{pid[-4:]}", admitted_by),
            )
    _CREATED.append(pid)
    return pid


def _stored(table, pid):
    with db_session() as db:
        return [r["timestamp"] for r in db.execute(
            f"SELECT timestamp FROM {table} WHERE patient_id = ? ORDER BY id", (pid,))]


def _earlier_same_utc_day(hours: float) -> datetime:
    """A moment `hours` ago, pulled forward so it shares today's UTC date.

    The old bug only bit inside one UTC date: 'T' beats ' ' at character 10,
    but a later DATE wins at character 9 whatever the separator. In the first
    few minutes of a UTC day there is no such earlier moment, and the test can
    only check the fixed behaviour (it passes on old and new code alike).
    """
    now = datetime.now(timezone.utc)
    room = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() - 120
    want = hours * 3600
    return now - timedelta(seconds=min(want, room) if room > 600 else want)


def _json_payload(pid, observed_at, hr):
    def obs(code, name, value, unit):
        return {"code": code, "name": name, "value": value, "unit": unit}
    return json.dumps({
        "patient_id": pid,
        "message_id": f"ts-{uuid.uuid4().hex}",
        "observed_at": observed_at,
        "observations": [
            obs("8867-4", "Heart Rate", hr, "bpm"),
            obs("8480-6", "Systolic Blood Pressure", 120, "mmHg"),
            obs("8462-4", "Diastolic Blood Pressure", 80, "mmHg"),
            obs("2708-6", "Oxygen Saturation", 97, "%"),
            obs("8310-5", "Body Temperature", 37.0, "Cel"),
            obs("9279-1", "Respiratory Rate", 16, "/min"),
        ],
    })


async def _ingest(pid, observed_at, hr):
    res = await ingestion_service.ingest_payload(_json_payload(pid, observed_at, hr),
                                                 source_name="ts-canonical-test")
    assert not res.errors, res.errors
    assert res.inserted_vitals == 1, res.to_dict()


# ════════════════════════════════════════════
#  1. Every writer stores one spelling
# ════════════════════════════════════════════

def test_a_nurse_set_sent_with_an_indian_offset_is_stored_in_utc():
    """The UI sends 'Z', but the route accepts any ISO string, and the old
    write kept a caller's '+05:30' as typed, beside '+00:00' rows."""
    pid = _patient()
    taken = (datetime.now(timezone.utc) - timedelta(minutes=30)).astimezone(IST)
    res = nurse_shift_service.record_observation(pid, "N001", dict(BENIGN_OBS),
                                                 taken_at=taken.isoformat())
    want = to_utc_iso(taken)
    assert _stored("vitals", pid) == [want]
    assert _stored("risk_scores", pid) == [want]
    assert res["recorded_at"] == want


def test_a_nurse_set_on_a_whole_second_keeps_the_same_spelling():
    """`isoformat()` drops the fraction when it is zero, so a set stamped at
    .000 was spelled differently from its neighbours."""
    pid = _patient()
    taken = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(microsecond=0)
    nurse_shift_service.record_observation(
        pid, "N001", dict(BENIGN_OBS),
        taken_at=taken.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    assert _stored("vitals", pid) == [to_utc_iso(taken)]
    assert all(CANONICAL.match(t) for t in _stored("vitals", pid) + _stored("risk_scores", pid))


def test_the_ingestion_risk_row_carries_an_explicit_utc_stamp():
    """It used to take the column's CURRENT_TIMESTAMP default: '... 19:26:36'."""
    pid = _patient()
    ingestion_service._persist_risk_score(pid, {"score": 0.1, "level": "stable", "drivers": []})
    [ts] = _stored("risk_scores", pid)
    assert CANONICAL.match(ts), ts


async def test_device_times_are_stored_as_the_instants_they_name():
    """'+05:30' and 'Z' are converted to UTC; an offset-less time is the
    appliance's wall clock (IST), the rule the sink and the HL7 parser apply."""
    pid = _patient()
    now = datetime.now(timezone.utc).replace(microsecond=250000)
    offset_ist = now - timedelta(hours=2)
    zulu = now - timedelta(minutes=5)
    naive_ist = now - timedelta(hours=3)

    await _ingest(pid, offset_ist.astimezone(IST).isoformat(), 60)
    await _ingest(pid, zulu.isoformat().replace("+00:00", "Z"), 140)
    await _ingest(pid, naive_ist.astimezone(IST).replace(tzinfo=None).isoformat(), 80)

    assert _stored("vitals", pid) == [to_utc_iso(offset_ist), to_utc_iso(zulu), to_utc_iso(naive_ist)]
    risk = _stored("risk_scores", pid)
    assert len(risk) == 3 and all(CANONICAL.match(t) for t in risk), risk


def test_the_admission_baseline_is_stored_in_the_same_spelling():
    from services.admission_service import admit_patient
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('TS-DOC', 'Dr Ts', 'doctor', 'x')")
    res = admit_patient({"name": "Ts Admission", "ward": "General",
                         "vitals": {"hr": 88, "bp_sys": 120, "spo2": 97}},
                        {"id": "TS-DOC", "name": "Dr Ts", "role": "doctor"})
    pid = res["patient_id"]
    _CREATED.append(pid)
    [vitals_ts] = _stored("vitals", pid)
    [risk_ts] = _stored("risk_scores", pid)
    assert CANONICAL.match(vitals_ts), vitals_ts
    assert risk_ts == vitals_ts, "the baseline score and the set it scored are one moment"


# ════════════════════════════════════════════
#  2. "Latest" is the newest
# ════════════════════════════════════════════

def test_a_critical_ingestion_score_is_not_hidden_by_an_earlier_nurse_set():
    pid = _patient()
    nurse_shift_service.record_observation(
        pid, "N001", dict(BENIGN_OBS),
        taken_at=_earlier_same_utc_day(6).isoformat().replace("+00:00", "Z"))
    ingestion_service._persist_risk_score(
        pid, {"score": 0.95, "level": "critical", "drivers": ["HR 140 (+3)"]})

    from routes import vigil_routes
    assert vigil_routes._load_latest_risk(pid)["level"] == "critical"

    # The MAX(timestamp) join search, the surgery board, the Sage census and
    # the ward narrator use.
    with db_session() as db:
        joined = db.execute(
            """SELECT rs.level FROM risk_scores rs
                WHERE rs.patient_id = ? AND rs.timestamp =
                      (SELECT MAX(rs2.timestamp) FROM risk_scores rs2
                        WHERE rs2.patient_id = rs.patient_id)""",
            (pid,)).fetchall()
    assert [r["level"] for r in joined] == ["critical"]

    # The Command Center acuity used to be checked here too. Since 2026-10-06 it
    # bands from the latest VITALS row (the Vigil board's rule), not
    # risk_scores, so it is covered by tests/test_command_center.py instead.


async def test_the_newest_device_reading_is_the_one_scored():
    """Inserted newest-first, so no id tiebreak can rescue a wrong text order."""
    dept, uid, headers = _ward()
    pid = _patient(dept, uid)
    now = datetime.now(timezone.utc)
    await _ingest(pid, (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"), 140)
    await _ingest(pid, (now - timedelta(hours=2)).astimezone(IST).isoformat(), 60)
    await _ingest(pid, (now - timedelta(hours=3)).astimezone(IST).replace(tzinfo=None).isoformat(), 80)

    # HR 140 scores 3 on its own; HR 60 and HR 80 score 0.
    risk = vigil_service.get_patient_risk(pid)
    assert risk["raw_news2"] == 3, risk

    r = client.get("/api/vigil/patients", headers=headers)
    assert r.status_code == 200, r.text
    [row] = [p for p in r.json() if p["id"] == pid]
    assert row["news2"]["raw_news2"] == 3, row["news2"]


def test_a_one_hour_window_holds_one_hour():
    pid = _patient()
    nurse_shift_service.record_observation(
        pid, "N001", dict(BENIGN_OBS),
        taken_at=_earlier_same_utc_day(7).isoformat().replace("+00:00", "Z"))
    from routes import vigil_routes
    assert vigil_routes._load_recent_vitals(pid, 1) == []


def test_a_fhir_since_given_with_an_offset_means_that_instant():
    from services.fhir_export_service import observations_to_fhir
    pid = _patient()
    now = datetime.now(timezone.utc)
    older, newer = to_utc_iso(now - timedelta(hours=3)), to_utc_iso(now - timedelta(hours=1))
    with db_session() as db:
        for ts in (older, newer):
            db.execute("INSERT INTO vitals (patient_id, timestamp, heart_rate, source) "
                       "VALUES (?, ?, 80, 'nurse_manual')", (pid, ts))
    since = (now - timedelta(hours=2)).astimezone(IST).isoformat()
    got = observations_to_fhir(pid, since=since)
    assert {o["effectiveDateTime"] for o in got} == {newer}

    with pytest.raises(ValueError):
        observations_to_fhir(pid, since="last tuesday")


# ════════════════════════════════════════════
#  3. The backfill
# ════════════════════════════════════════════

def _backfill():
    import migrations
    [mig] = [m for m in migrations.MIGRATIONS if m.name == "canonical_vitals_risk_timestamps"]
    return mig


@pytest.fixture
def old_db(tmp_path):
    """A DB with the real schema and rows in every spelling the old writers
    left behind. Separate from the session DB: the backfill rewrites a whole
    table, and other modules' fixture rows are not this test's to touch."""
    import database
    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(database.SCHEMA)
    conn.execute("ALTER TABLE vitals ADD COLUMN source TEXT DEFAULT 'simulated'")
    rows = {
        "risk_scores": [
            ("2026-10-02T13:26:35.789391+00:00", "stable", None),    # nurse lane
            ("2026-10-02 19:26:36", "critical", None),               # CURRENT_TIMESTAMP
            ("2026-10-02T19:30:00Z", "stable", None),
            ("2026-10-03T01:05:00+05:30", "elevated", None),         # = 19:35 UTC
            ("2026-10-02T19:00:00+00:00", "stable", None),           # whole second
        ],
        "vitals": [
            ("2026-10-02T22:56:35.789391+05:30", None, "ingested_json"),  # = 17:26:35 UTC
            ("2026-10-02T19:21:35.789391Z", None, "ingested_json"),
            ("2026-10-03T00:51:35.789391", None, "ingested_json"),        # IST wall clock
            ("2026-10-02T10:00:00", None, "nurse_manual"),                # Orb's own: UTC
            ("2026-10-02 18:00:00", None, "ingested_hl7"),                # device, space
            ("not a time", None, "ingested_json"),
        ],
    }
    for ts, level, _ in rows["risk_scores"]:
        conn.execute("INSERT INTO risk_scores (patient_id, timestamp, score, level) "
                     "VALUES ('P-OLD', ?, 0.5, ?)", (ts, level))
    for ts, _, source in rows["vitals"]:
        conn.execute("INSERT INTO vitals (patient_id, timestamp, heart_rate, source) "
                     "VALUES ('P-OLD', ?, 80, ?)", (ts, source))
    conn.commit()
    yield conn
    conn.close()


def _column(conn, table):
    return [r["timestamp"] for r in conn.execute(f"SELECT timestamp FROM {table} ORDER BY id")]


def test_the_backfill_rewrites_every_old_spelling_to_the_instant_it_meant(old_db):
    _backfill().apply(old_db)
    assert _column(old_db, "risk_scores") == [
        "2026-10-02T13:26:35.789391+00:00",
        "2026-10-02T19:26:36.000000+00:00",
        "2026-10-02T19:30:00.000000+00:00",
        "2026-10-02T19:35:00.000000+00:00",
        "2026-10-02T19:00:00.000000+00:00",
    ]
    assert _column(old_db, "vitals") == [
        "2026-10-02T17:26:35.789391+00:00",
        "2026-10-02T19:21:35.789391+00:00",
        "2026-10-02T19:21:35.789391+00:00",   # 00:51 IST
        "2026-10-02T10:00:00.000000+00:00",
        "2026-10-02T12:30:00.000000+00:00",   # 18:00 IST
        "not a time",                         # never guessed at
    ]


def test_after_the_backfill_text_order_is_time_order(old_db):
    _backfill().apply(old_db)
    latest = old_db.execute(
        "SELECT level FROM risk_scores ORDER BY timestamp DESC, id DESC LIMIT 1").fetchone()
    assert latest["level"] == "elevated"      # 19:35 UTC, the true latest


def test_the_backfill_is_audited_and_idempotent(old_db):
    from services.clinical_time import backfill_canonical_timestamps
    first = backfill_canonical_timestamps(old_db)
    assert first["risk_scores"]["rewritten"] == 4
    assert first["vitals"]["rewritten"] == 5
    assert first["vitals"]["unparseable"] == 1
    assert first["vitals"]["naive_read_in_device_zone"] == 2
    audit = old_db.execute(
        "SELECT event_category, metadata_json FROM audit_logs "
        "WHERE action_type = 'timestamp_spelling_normalised'").fetchall()
    assert len(audit) == 1 and audit[0]["event_category"] == "data_management"
    assert json.loads(audit[0]["metadata_json"])["device_zone"] == "Asia/Kolkata"

    before = _column(old_db, "vitals") + _column(old_db, "risk_scores")
    second = backfill_canonical_timestamps(old_db)
    assert second["risk_scores"]["rewritten"] == 0 and second["vitals"]["rewritten"] == 0
    assert _column(old_db, "vitals") + _column(old_db, "risk_scores") == before


# ════════════════════════════════════════════
#  4. Guard: no writer falls back to the column default
# ════════════════════════════════════════════

_INSERT = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(vitals|risk_scores)\b\s*(\(([^)]*)\))?", re.I)


def test_every_writer_names_the_timestamp_column():
    """`risk_scores.timestamp DEFAULT CURRENT_TIMESTAMP` is still in the schema
    (SQLite cannot drop a default without a table rebuild). Omitting the column
    silently writes the old spelling, so no non-test writer may omit it."""
    server = Path(__file__).resolve().parent.parent
    offenders = []
    for path in server.rglob("*.py"):
        rel = path.relative_to(server)
        if rel.parts[0] in ("tests", ".venv") or "site-packages" in rel.parts:
            continue
        for m in _INSERT.finditer(path.read_text(encoding="utf-8", errors="replace")):
            cols = m.group(3)
            if cols is None or "timestamp" not in cols.lower():
                offenders.append(f"{rel}: {m.group(0)[:80]!r}")
    assert not offenders, "writers relying on the column default:\n" + "\n".join(offenders)
