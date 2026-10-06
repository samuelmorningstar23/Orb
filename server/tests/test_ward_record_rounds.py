"""Orb Ward Record: the ward's observation interval and the monthly rounds file
(services/ward_record_service.py, routes/ward_record_routes.py).

THE SCENARIO, worked by hand. A ward at a 4 hour interval and a 30 minute grace
window. One patient arrives at 08:00 and leaves at 02:00 the next day, hospital
local time. Rounds fall due at 12:00, 16:00, 20:00 and 00:00 (04:00 is after
they left).

  12:00  a set at 12:10, inside [11:45, 12:30]           done
  16:00  first set after 16:30 is 16:45, before 20:00     late
  20:00  nothing counted before 00:00                     missed
         (a simulated set at 20:10, a monitor set at 20:20 and a set voided as
         entered in error at 20:15 are all there, and none of them counts)
  00:00  a set at 00:05 with no oxygen saturation and an
         illegible temperature                            done, 1 blank, 1 illegible

With the product's ward shifts (07:00 to 15:00, 15:00 to 23:00, 23:00 to
07:00): 12:00 sits in the day shift, 16:00 and 20:00 in the evening shift, and
00:00 in the night shift that began on the first day.

ISOLATION. The suite shares one database, so every test makes its own ward and
works in June 2025, which no other module writes into.
"""
import hashlib
import itertools
import os
import sys
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth import hash_password  # noqa: E402
from database import db_session  # noqa: E402
from main import app  # noqa: E402
from services import ward_record_service as svc  # noqa: E402
from services.hospital_time import hospital_timezone  # noqa: E402

client = TestClient(app)

_SEQ = itertools.count(1)
FORBIDDEN_WORDS = ("news2", "score", "band", "critical", "elevated", "stable")


def _ward() -> str:
    return f"WR-TEST-W{next(_SEQ):03d}-{os.getpid()}"


def _local(day: int, hh: int, mm: int = 0) -> str:
    """A June 2025 hospital-local wall time as a stored UTC instant."""
    tz = hospital_timezone()
    return datetime(2025, 6, day, hh, mm, tzinfo=tz).astimezone(timezone.utc).isoformat()


def _admin() -> str:
    user_id = "WR-ADM"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Quality Coordinator', 'admin', NULL, ?)",
            (user_id, hash_password("password")),
        )
    return user_id


def _nurse() -> str:
    user_id = "WR-NUR"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Staff Nurse', 'nurse', NULL, ?)",
            (user_id, hash_password("password")),
        )
    return user_id


def _login(user_id: str) -> dict:
    resp = client.post("/api/auth/login", json={"employee_id": user_id, "password": "password"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _vitals_columns() -> set:
    with db_session() as db:
        return {r[1] for r in db.execute("PRAGMA table_info(vitals)").fetchall()}


def _set(pid: str, at: str, *, source: str = "clerk_transcribed", spo2=97,
         temperature=37.0, entered_at: str | None = None, illegible: str | None = None,
         voided: bool = False) -> None:
    cols = _vitals_columns()
    row = {
        "patient_id": pid, "timestamp": at, "heart_rate": 80, "systolic_bp": 120,
        "diastolic_bp": 80, "spo2": spo2, "temperature": temperature,
        "respiratory_rate": 16, "consciousness": "alert", "on_oxygen": 0,
        "source": source,
    }
    if "entered_at" in cols:
        row["entered_at"] = entered_at
    if "illegible" in cols and illegible:
        row["illegible"] = illegible
    if "voided_at" in cols and voided:
        row["voided_at"] = at
        row["void_reason"] = "entered in error"
    keys = [k for k in row if k in cols]
    with db_session() as db:
        db.execute(f"INSERT INTO vitals ({', '.join(keys)}) VALUES "
                   f"({', '.join('?' for _ in keys)})", [row[k] for k in keys])


@pytest.fixture()
def scenario():
    """The worked scenario in the module docstring, on a ward of its own."""
    admin = _admin()
    ward = _ward()
    pid = f"WRPT-{next(_SEQ):04d}-{os.getpid()}"
    mrn = f"UHID-{pid}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, admission_date, status) "
            "VALUES (?, 'Ward Record Fixture', ?, ?, '7', ?, 'discharged')",
            (pid, mrn, ward, _local(10, 8)))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, ward, bed, status, "
            "created_at, admitted_at, discharged_at) "
            "VALUES (?, ?, ?, ?, '7', 'discharged', ?, ?, ?)",
            (f"ADM-{pid}", pid, admin, ward, _local(10, 8), _local(10, 8), _local(11, 2)))
    _set(pid, _local(10, 12, 10), entered_at=_local(10, 13, 0))
    _set(pid, _local(10, 16, 45), entered_at=_local(10, 17, 5))
    _set(pid, _local(10, 20, 10), source="simulated")
    _set(pid, _local(10, 20, 15), voided=True, entered_at=_local(10, 20, 30))
    _set(pid, _local(10, 20, 20), source="monitor_hl7")
    _set(pid, _local(11, 0, 5), spo2=None, temperature=None,
         entered_at=_local(11, 8, 30), illegible="temperature")
    headers = _login(admin)
    resp = client.put(f"/api/ward-record/settings/{ward}", headers=headers, json={
        "interval_minutes": 240, "grace_minutes": 30,
        "set_by": "Sister A. Thomas, Nursing Superintendent", "note": "Memo NS/4",
    })
    assert resp.status_code == 200, resp.text
    return {"ward": ward, "pid": pid, "mrn": mrn, "headers": headers, "admin": admin}


@pytest.fixture(autouse=True)
def _product_shifts(monkeypatch):
    monkeypatch.delenv("ORB_SCORECARD_SHIFTS", raising=False)


def _query(ward: str, **extra) -> str:
    params = {"ward": ward, "start": "2025-06-10", "end": "2025-06-11", **extra}
    return "&".join(f"{k}={v}" for k, v in params.items())


# ── The counts ──────────────────────────────────────────────────────────────

def test_every_count_in_the_worked_scenario(scenario):
    resp = client.get(f"/api/ward-record/rounds?{_query(scenario['ward'])}",
                      headers=scenario["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["totals"]["due"] == 4
    assert body["totals"]["done"] == 2
    assert body["totals"]["late"] == 1
    assert body["totals"]["missed"] == 1
    assert body["totals"]["open"] == 0
    # The simulated, monitor and voided sets never count.
    has_void = "voided_at" in _vitals_columns()
    assert body["sets_counted"] == (3 if has_void else 4)

    by_shift = {r["shift"]: r for r in body["shift_totals"]}
    assert by_shift["07:00 to 15:00"]["due"] == 1 and by_shift["07:00 to 15:00"]["done"] == 1
    evening = by_shift["15:00 to 23:00"]
    assert (evening["due"], evening["done"], evening["late"], evening["missed"]) == (2, 0, 1, 1)
    night = by_shift["23:00 to 07:00"]
    assert night["shift_date"] == "2025-06-10", "a night round sits under the evening it began"
    assert (night["due"], night["done"]) == (1, 1)

    readings = {r["field"]: r for r in body["readings"]}
    assert readings["spo2"]["blank"] == 1
    if "illegible" in _vitals_columns():
        assert readings["temperature"]["illegible"] == 1
        assert readings["temperature"]["blank"] == 0, "illegible is never also blank"
    else:
        assert readings["temperature"]["blank"] == 1

    # One row per patient per shift, the patient shown without the patient number.
    assert {r["patient"] for r in body["rows"]} == {"Patient 1"}
    assert {r["bed"] for r in body["rows"]} == {"7"}
    assert len(body["rows"]) == 3

    if "entered_at" in _vitals_columns():
        lag = {r["shift"]: r for r in body["entry_lag"]["by_shift"]}
        assert lag["07:00 to 15:00"]["median_minutes"] == 50
        assert lag["15:00 to 23:00"]["max_minutes"] == 20
        assert lag["23:00 to 07:00"]["max_minutes"] == 505
        # A long lag is never a late round: the 00:00 round keyed 8 hours
        # later is still done.
        assert night["late"] == 0


def test_the_patient_number_appears_only_when_asked_for(scenario):
    plain = client.get(f"/api/ward-record/rounds.csv?{_query(scenario['ward'])}",
                       headers=scenario["headers"])
    assert plain.status_code == 200, plain.text
    assert scenario["mrn"] not in plain.text
    assert "Ward Record Fixture" not in plain.text

    named = client.get(
        f"/api/ward-record/rounds.csv?{_query(scenario['ward'], include_patient_identifiers='true')}",
        headers=scenario["headers"])
    assert named.status_code == 200, named.text
    assert scenario["mrn"] in named.text
    assert "Ward Record Fixture" not in named.text, "a name never appears"


# ── The documents ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("ext", ["csv", "md"])
def test_the_file_has_no_score_band_or_response_words(scenario, ext):
    resp = client.get(f"/api/ward-record/rounds.{ext}?{_query(scenario['ward'])}",
                      headers=scenario["headers"])
    assert resp.status_code == 200, resp.text
    text = resp.text
    # The CSV quotes the sentence because it holds commas; a spreadsheet shows
    # it as one cell.
    closing = f'"{svc.CLOSING_LINE}"' if ext == "csv" else svc.CLOSING_LINE
    assert text.rstrip().endswith(closing)
    body = text.replace(closing, "").lower()
    for word in FORBIDDEN_WORDS:
        assert word not in body, f"{word!r} appears in the {ext} file"
    # The header says what the file is measured against.
    assert scenario["ward"] in text
    assert "every 240 minutes" in text
    assert "30 minutes" in text
    assert "Sister A. Thomas, Nursing Superintendent" in text
    assert "2025-06-10 to 2025-06-11" in text


@pytest.mark.parametrize("ext", ["csv", "md"])
def test_every_file_is_logged_with_its_digest_and_audited(scenario, ext):
    resp = client.get(f"/api/ward-record/rounds.{ext}?{_query(scenario['ward'])}",
                      headers=scenario["headers"])
    assert resp.status_code == 200, resp.text
    reference = resp.headers["X-Ward-Record-Reference"]
    digest = hashlib.sha256(resp.content).hexdigest()
    assert resp.headers["X-Content-SHA256"] == digest
    assert "attachment" in resp.headers["Content-Disposition"]
    with db_session() as db:
        row = db.execute("SELECT * FROM ward_record_exports WHERE reference = ?",
                         (reference,)).fetchone()
        audit = db.execute(
            "SELECT * FROM audit_logs WHERE action_type = 'ward_record_rounds_file_generated' "
            "AND target_id = ?", (reference,)).fetchone()
    assert row is not None, "the file was not logged"
    assert row["sha256"] == digest
    assert row["format"] == ext
    assert row["ward"] == scenario["ward"]
    assert (row["period_start"], row["period_end"]) == ("2025-06-10", "2025-06-11")
    assert row["generated_by"] == scenario["admin"]
    assert audit is not None, "the file was not audited"
    assert audit["actor_user_id"] == scenario["admin"]
    assert digest in (audit["metadata_json"] or "")


# ── Settings ────────────────────────────────────────────────────────────────

def test_a_ward_with_no_setting_gets_409_and_no_guess():
    admin = _admin()
    ward = _ward()
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, ward, bed, admission_date, status) "
                   "VALUES (?, 'No Setting', ?, ?, '1', ?, 'discharged')",
                   (f"WRNS-{ward}", f"MRN-{ward}", ward, _local(10, 8)))
    headers = _login(admin)
    for path in ("rounds", "rounds.csv", "rounds.md"):
        resp = client.get(f"/api/ward-record/{path}?{_query(ward)}", headers=headers)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["message"] == "Set the ward's observation interval first"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM ward_record_exports WHERE ward = ?",
                          (ward,)).fetchone()[0] == 0


def test_settings_list_every_ward_and_a_change_is_audited_with_old_and_new(scenario):
    headers = scenario["headers"]
    listing = client.get("/api/ward-record/settings", headers=headers)
    assert listing.status_code == 200, listing.text
    wards = {w["ward"]: w["setting"] for w in listing.json()["wards"]}
    assert wards[scenario["ward"]]["interval_minutes"] == 240

    resp = client.put(f"/api/ward-record/settings/{scenario['ward']}", headers=headers,
                      json={"interval_minutes": 360, "grace_minutes": 45,
                            "set_by": "Sister A. Thomas, Nursing Superintendent"})
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        audit = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE action_type = "
            "'ward_observation_interval_set' AND target_id = ? ORDER BY id DESC LIMIT 1",
            (scenario["ward"],)).fetchone()
    assert audit is not None
    assert '"interval_minutes": 240' in audit["metadata_json"] or \
        '"interval_minutes":240' in audit["metadata_json"]
    assert "360" in audit["metadata_json"]


@pytest.mark.parametrize("body", [
    {"interval_minutes": 20, "set_by": "NS"},
    {"interval_minutes": 1500, "set_by": "NS"},
    {"interval_minutes": 240, "grace_minutes": 300, "set_by": "NS"},
    {"interval_minutes": 240, "set_by": "  "},
])
def test_out_of_range_settings_are_refused(scenario, body):
    resp = client.put(f"/api/ward-record/settings/{scenario['ward']}",
                      headers=scenario["headers"], json=body)
    assert resp.status_code == 422, resp.text


def test_an_unknown_ward_cannot_be_given_a_setting():
    headers = _login(_admin())
    resp = client.put("/api/ward-record/settings/NO-SUCH-WARD-ZZ", headers=headers,
                      json={"interval_minutes": 240, "set_by": "NS"})
    assert resp.status_code == 404, resp.text


def test_a_nurse_cannot_read_or_change_the_ward_record():
    headers = _login(_nurse())
    assert client.get("/api/ward-record/settings", headers=headers).status_code == 403
    assert client.get(f"/api/ward-record/rounds.csv?{_query('ANY')}",
                      headers=headers).status_code == 403
