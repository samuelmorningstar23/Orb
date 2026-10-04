"""One reader for live lab results (lis-5 part a: services/lab_observations.py).

THE DEFECT. Four lanes read structured results in `observations`, each with
its own SQL. The chart's lab card and the downtime ward pack left voided rows
out and ordered by julianday(); the Tier 1 creatinine lane ordered by the raw
string, read voided rows and had no tie-break; the chart timeline showed
voided rows. observed_at is stored as received ('T' or space, offset or none),
so a string sort can call an older result the newest, and a result the lab
withdrew could still raise a renal flag the chart no longer showed.

PINNED HERE, against every lane (chart route, ward pack, Tier 1, timeline):
  * mixed spellings order by the instant, newest first, in every reader and
    in previous_result;
  * equal instants break by id DESC (the later write wins);
  * voided and superseded rows disappear everywhere;
  * simulated rows: the chart, ward pack and timeline keep them (the card
    labels them), and the Tier 1 lane reads them unless the opt-in lab
    module (ORB_LAB) is on, the ruling for wave 1;
  * display_value: the lab's text first, then the number;
  * the guard: no new read of observations in services/ or routes/ outside
    the helper, matching FROM and JOIN and declarative from_sql strings.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import services.downtime_service as ds
from auth import create_token
from database import db_session
from main import app
from services import lab_observations as lo
from services import patient_specific_screen as pss
from services.patient_timeline_service import (
    available_sources, build_chart_timeline, count_new_since,
)

client = TestClient(app)

DEPT = "Lab Reader Parity Medicine"
_ADMITTED: list[str] = []

# The mixed-spelling fixture from the spec. As instants (UTC):
#   '2026-10-03 10:00:00'          10:00, naive, read as UTC
#   '2026-10-03T08:00:00Z'         08:00, canonical
#   '2026-10-03T09:30:00+05:30'    04:00, an Indian-offset stamp
# As strings, DESC puts 'T09:30...' first and ' 10:00' last: exactly wrong.
NAIVE_10 = "2026-10-03 10:00:00"
CANON_08 = "2026-10-03T08:00:00Z"
OFFSET_04 = "2026-10-03T09:30:00+05:30"


@pytest.fixture(autouse=True)
def _discharge_admitted():
    """Leave nobody admitted in the shared session DB."""
    yield
    if not _ADMITTED:
        return
    with db_session() as db:
        db.executemany("UPDATE patients SET status = 'discharged' WHERE id = ?",
                       [(p,) for p in _ADMITTED])
        db.executemany("UPDATE admissions SET status = 'discharged' WHERE patient_id = ?",
                       [(p,) for p in _ADMITTED])
    _ADMITTED.clear()


def _patient(status: str = "discharged") -> str:
    pid = f"LOR{uuid.uuid4().hex[:9].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, diagnosis, status, age, sex) "
            "VALUES (?, 'Reader Parity Patient', ?, 'LR-1', 'LR-B1', 'Sepsis', ?, 66, 'F')",
            (pid, f"MRN-{pid}", status),
        )
    return pid


def _obs(pid, name, value, observed_at, *, unit="mmol/L", status="final",
         source="ingested_hl7", flag=None, value_text=None, code=None,
         reference_range=None, recorded_at=None) -> int:
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO observations (patient_id, source, observation_code, observation_name,
                                         value_numeric, value_text, unit, reference_range,
                                         abnormal_flag, status, observed_at, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))""",
            (pid, source, code, name, value, value_text, unit, reference_range, flag,
             status, observed_at, recorded_at),
        )
        return cur.lastrowid


def _mixed_potassium(pid) -> None:
    # Inserted oldest-written first in an order that differs from both the
    # instant order and the string order.
    _obs(pid, "Potassium", 4.0, OFFSET_04, code="2823-3")
    _obs(pid, "Potassium", 6.2, NAIVE_10, code="2823-3")
    _obs(pid, "Potassium", 5.1, CANON_08, code="2823-3")


# ═════════════════════════════════════════════════════════════════════════════
#  Mixed spellings order by the instant in every reader
# ═════════════════════════════════════════════════════════════════════════════

def test_recent_observations_orders_mixed_spellings_by_the_instant():
    pid = _patient()
    _mixed_potassium(pid)
    with db_session() as db:
        rows = lo.recent_observations(db, pid, limit=10)
    assert [r["value_numeric"] for r in rows] == [6.2, 5.1, 4.0]


def test_the_chart_lab_rows_are_oldest_first_by_the_instant():
    pid = _patient()
    _mixed_potassium(pid)
    with db_session() as db:
        rows = lo.chart_lab_rows(db, pid, per_analyte=50, max_rows=1000)
    assert [r["value_numeric"] for r in rows] == [4.0, 5.1, 6.2]
    with db_session() as db:
        newest_only = lo.chart_lab_rows(db, pid, per_analyte=1, max_rows=1000)
    assert [r["value_numeric"] for r in newest_only] == [6.2]


def test_the_ward_pack_keeps_the_newest_by_the_instant():
    pid = _patient()
    _mixed_potassium(pid)
    with db_session() as db:
        rows = lo.latest_per_analyte(db, pid, limit=20)
    assert [(r["observation_name"], r["value_numeric"]) for r in rows] == [("Potassium", 6.2)]
    snap = ds.capture_snapshot(pid, created_by="auto")["snapshot"]
    assert [r["value_numeric"] for r in snap["lab_results"]] == [6.2]


def test_previous_result_compares_by_the_instant_whatever_the_spelling():
    pid = _patient()
    _mixed_potassium(pid)
    with db_session() as db:
        # Before 10:00 UTC, spelled canonically: the 08:00 result.
        assert lo.previous_result(db, pid, "2823-3", "2026-10-03T10:00:00Z")["value_numeric"] == 5.1
        # Before 07:30 UTC, spelled with the Indian offset: the 04:00 result.
        assert lo.previous_result(db, pid, "2823-3", "2026-10-03T13:00:00+05:30")["value_numeric"] == 4.0
        # Strictly before: nothing precedes the 04:00 result, in any spelling.
        assert lo.previous_result(db, pid, "2823-3", "2026-10-03 04:00:00") is None
        # After everything: the newest.
        assert lo.previous_result(db, pid, "2823-3", "2026-10-04 00:00:00")["value_numeric"] == 6.2
        # Another analyte code has no previous result.
        assert lo.previous_result(db, pid, "2160-0", "2026-10-04 00:00:00") is None


def test_previous_result_refuses_an_unparseable_time_rather_than_answering_none():
    pid = _patient()
    _mixed_potassium(pid)
    with db_session() as db:
        with pytest.raises(ValueError):
            lo.previous_result(db, pid, "2823-3", "yesterday")
        with pytest.raises(ValueError):
            lo.previous_result(db, pid, "2823-3", None)


def test_the_timeline_orders_mixed_spellings_by_the_instant():
    pid = _patient()
    _mixed_potassium(pid)
    events = [e for e in build_chart_timeline(pid)["events"] if e["kind"] == "observation"]
    assert [e["detail"].split()[0] for e in events] == ["6.2", "5.1", "4.0"]


def test_the_chart_route_orders_mixed_spellings_by_the_instant():
    doctor, pid = _admitted_patient_with_doctor()
    _mixed_potassium(pid)
    labs = _chart_labs(doctor, pid)
    assert [o["value_numeric"] for o in labs] == [4.0, 5.1, 6.2]


# ═════════════════════════════════════════════════════════════════════════════
#  Equal instants: the later write wins
# ═════════════════════════════════════════════════════════════════════════════

def test_equal_instants_break_by_id_desc_in_every_reader():
    pid = _patient()
    first = _obs(pid, "Sodium", 128.0, "2026-10-03 06:00:00", code="2951-2")
    second = _obs(pid, "Sodium", 131.0, "2026-10-03T06:00:00Z", code="2951-2")
    assert second > first
    with db_session() as db:
        assert [r["id"] for r in lo.recent_observations(db, pid, limit=5)] == [second, first]
        assert [r["value_numeric"] for r in lo.latest_per_analyte(db, pid, limit=5)] == [131.0]
        assert [r["value_numeric"] for r in
                lo.chart_lab_rows(db, pid, per_analyte=1, max_rows=10)] == [131.0]
        assert lo.previous_result(db, pid, "2951-2", "2026-10-03 07:00:00")["id"] == second
    events = [e for e in build_chart_timeline(pid)["events"] if e["kind"] == "observation"]
    assert [e["id"] for e in events] == [f"observation:{second}", f"observation:{first}"]


# ═════════════════════════════════════════════════════════════════════════════
#  Voided and superseded rows disappear everywhere
# ═════════════════════════════════════════════════════════════════════════════

VOIDS = ("superseded", "entered-in-error", "W", " x ", "Cancelled")


def _voids_world(pid) -> None:
    _obs(pid, "Haemoglobin", 9.1, "2026-10-03 06:00:00", unit="g/dL", code="718-7")
    for i, status in enumerate(VOIDS):
        _obs(pid, "Haemoglobin", 5.0 + i / 10, f"2026-10-03 0{7 + i}:00:00", unit="g/dL",
             status=status, code="718-7")


def test_voided_and_superseded_rows_disappear_from_every_reader():
    pid = _patient()
    _voids_world(pid)
    with db_session() as db:
        assert [r["value_numeric"] for r in lo.recent_observations(db, pid, limit=20)] == [9.1]
        assert [r["value_numeric"] for r in lo.latest_per_analyte(db, pid, limit=20)] == [9.1]
        assert [r["value_numeric"] for r in
                lo.chart_lab_rows(db, pid, per_analyte=50, max_rows=1000)] == [9.1]
        assert lo.previous_result(db, pid, "718-7", "2026-10-04 00:00:00")["value_numeric"] == 9.1
        # The "new since" count on the chart counts live results only.
        assert count_new_since(db, pid, "2000-01-01T00:00:00Z", available_sources(db)) == 1
    events = [e for e in build_chart_timeline(pid)["events"] if e["kind"] == "observation"]
    assert [e["detail"].split()[0] for e in events] == ["9.1"]
    snap = ds.capture_snapshot(pid, created_by="auto")["snapshot"]
    assert [r["value_numeric"] for r in snap["lab_results"]] == [9.1]


def test_a_superseded_result_is_gone_from_the_chart_route():
    doctor, pid = _admitted_patient_with_doctor()
    _obs(pid, "Potassium", 4.4, "2026-10-03 06:00:00")
    _obs(pid, "Potassium", 7.4, "2026-10-03 07:00:00", status="superseded")
    assert [o["value_numeric"] for o in _chart_labs(doctor, pid)] == [4.4]


# ═════════════════════════════════════════════════════════════════════════════
#  Simulated rows: kept and labelled on screen, as the chart has always done
# ═════════════════════════════════════════════════════════════════════════════

def test_simulated_rows_stay_on_the_chart_pack_and_timeline_with_their_source():
    pid = _patient()
    _obs(pid, "Lactate", 3.9, "2026-10-03 06:00:00", source="simulated")
    with db_session() as db:
        chart = lo.chart_lab_rows(db, pid, per_analyte=50, max_rows=1000)
        assert [(r["value_numeric"], r["source"]) for r in chart] == [(3.9, "simulated")]
        assert [r["value_numeric"] for r in lo.latest_per_analyte(db, pid, limit=5)] == [3.9]
        assert [r["value_numeric"] for r in lo.recent_observations(db, pid, limit=5)] == [3.9]
        # Only a caller that asks leaves them out.
        assert lo.recent_observations(db, pid, limit=5, exclude_simulated=True) == []
    assert [e["kind"] for e in build_chart_timeline(pid)["events"]] == ["observation"]


# ═════════════════════════════════════════════════════════════════════════════
#  The Tier 1 creatinine lane
# ═════════════════════════════════════════════════════════════════════════════

def _utc(hours_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


def _naive(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ist(dt: datetime) -> str:
    return (dt + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%dT%H:%M:%S+05:30")


def _creat(pid, value, stamp, **kw):
    return _obs(pid, "Creatinine", value, stamp, unit="mg/dL", **kw)


def _tier1(pid):
    with db_session() as db:
        return pss._creatinine_from_observations(db, pid)


def test_tier1_reads_the_newest_creatinine_by_the_instant_not_the_string():
    """The raised result is the newer one, written naive; the normal one is
    older, written with +05:30, so its string sorts first. A string sort read
    the normal one as the latest and the renal rules said nothing."""
    pid = _patient()
    _creat(pid, 1.0, _ist(_utc(3)))
    _creat(pid, 3.2, _naive(_utc(1)), flag="H")
    found = _tier1(pid)
    assert found is not None and found["value"] == 3.2


def test_tier1_does_not_flag_an_old_raised_value_over_a_newer_normal_one():
    pid = _patient()
    _creat(pid, 3.2, _ist(_utc(3)), flag="H")
    _creat(pid, 1.0, _naive(_utc(1)))
    assert _tier1(pid) is None


def test_tier1_breaks_equal_instants_by_the_later_write():
    pid = _patient()
    stamp = _naive(_utc(2))
    _creat(pid, 1.0, stamp)
    _creat(pid, 3.1, stamp, flag="H")
    found = _tier1(pid)
    assert found is not None and found["value"] == 3.1


@pytest.mark.parametrize("void", ["entered-in-error", "superseded", "W", "deleted"])
def test_tier1_never_reads_a_voided_or_superseded_creatinine(void):
    pid = _patient()
    _creat(pid, 1.0, _naive(_utc(5)))
    _creat(pid, 4.0, _naive(_utc(1)), flag="H", status=void)
    assert _tier1(pid) is None, f"a {void} creatinine raised a renal flag"


def test_tier1_reads_the_live_result_behind_a_voided_one():
    pid = _patient()
    _creat(pid, 2.9, _naive(_utc(5)), flag="H")
    _creat(pid, 1.0, _naive(_utc(1)), status="entered-in-error")
    found = _tier1(pid)
    assert found is not None and found["value"] == 2.9


def test_tier1_still_reads_a_simulated_creatinine_while_the_lab_module_is_off(monkeypatch):
    """Ruling for wave 1: a demo ward's labs are simulated, so with ORB_LAB off
    (every site today) the lane reads them as it always has."""
    monkeypatch.setattr(config, "LAB_ENABLED", False, raising=False)
    assert lo.tier1_excludes_simulated() is False
    pid = _patient()
    _creat(pid, 3.4, _naive(_utc(1)), flag="H", source="simulated")
    found = _tier1(pid)
    assert found is not None and found["value"] == 3.4


def test_the_lab_module_reads_as_off_when_config_has_no_switch(monkeypatch):
    monkeypatch.delattr(config, "LAB_ENABLED", raising=False)
    assert lo.lab_module_on() is False
    assert lo.tier1_excludes_simulated() is False


def test_tier1_ignores_a_simulated_creatinine_with_the_lab_module_on(monkeypatch):
    monkeypatch.setattr(config, "LAB_ENABLED", True, raising=False)
    assert lo.tier1_excludes_simulated() is True
    pid = _patient()
    _creat(pid, 1.0, _naive(_utc(6)))
    _creat(pid, 3.4, _naive(_utc(1)), flag="H", source="simulated")
    assert _tier1(pid) is None, "a simulated creatinine raised a renal flag with ORB_LAB on"

    other = _patient()
    _creat(other, 2.7, _naive(_utc(6)), flag="H")
    _creat(other, 1.0, _naive(_utc(1)), source="simulated")
    found = _tier1(other)
    assert found is not None and found["value"] == 2.7, "a simulated normal hid a genuine raised one"


# ═════════════════════════════════════════════════════════════════════════════
#  The display rule
# ═════════════════════════════════════════════════════════════════════════════

def test_display_value_prefers_the_labs_text_then_the_number():
    assert lo.display_value({"value_text": "1.20", "value_numeric": 1.2}) == "1.20"
    assert lo.display_value({"value_text": "<0.01", "value_numeric": None}) == "<0.01"
    assert lo.display_value({"value_text": "   ", "value_numeric": 7.0}) == 7.0
    assert lo.display_value({"value_text": None, "value_numeric": 5.9}) == 5.9
    assert lo.display_value({"value_text": None, "value_numeric": None}) is None
    assert lo.display_value({}) is None


def test_a_blank_valued_interfaced_result_never_prints_none_on_the_timeline():
    """An HL7 OBX with an empty OBX-5 (a pending result, a comment-only line)
    is stored with value_text '' and no number. display_value() answers None
    for it; the timeline and the Patient Story must print no value, as they
    always did, never the word None (on a culture it reads like a result)."""
    from services.patient_story_service import get_patient_story

    pid = _patient()
    _obs(pid, "Potassium", None, "2026-10-03 06:00:00", value_text="",
         reference_range="3.5-5.1", code="2823-3")
    _obs(pid, "Blood culture", None, "2026-10-03 07:00:00", value_text=None, unit=None,
         code="600-7")
    _obs(pid, "Sodium", None, "2026-10-03 08:00:00", value_text="   ", flag="H",
         code="2951-2")
    events = [e for e in build_chart_timeline(pid)["events"] if e["kind"] == "observation"]
    details = {e["title"]: e["detail"] for e in events}
    assert details == {
        "Sodium": "mmol/L · H",
        "Blood culture": "",
        "Potassium": "mmol/L (ref 3.5-5.1)",
    }
    story = [e for e in get_patient_story(pid) if e["type"] == "observation"]
    assert [e["detail"] for e in story] == ["mmol/L · H", "", "mmol/L (ref 3.5-5.1)"]
    for e in events + story:
        assert "None" not in e["detail"]


def test_the_ward_pack_prints_the_labs_own_precision():
    html = ds._render_labs({"lab_results": [
        {"observation_name": "Creatinine", "value_numeric": 1.2, "value_text": "1.20",
         "unit": "mg/dL", "reference_range": "0.7-1.3", "abnormal_flag": "",
         "observed_at": "2026-10-03 06:00:00"},
        {"observation_name": "Troponin", "value_numeric": None, "value_text": "<0.01",
         "unit": "ng/mL", "reference_range": None, "abnormal_flag": None,
         "observed_at": "2026-10-03 06:00:00"},
        {"observation_name": "Potassium", "value_numeric": 5.9, "value_text": None,
         "unit": "mmol/L", "reference_range": "3.5-5.1", "abnormal_flag": "H",
         "observed_at": "2026-10-03 06:00:00"},
    ]})
    assert "<td>1.20 mg/dL</td>" in html
    assert "&lt;0.01 ng/mL" in html
    assert "<td>5.9 mmol/L</td>" in html


def test_live_filter_is_a_plain_literal_fragment_with_no_placeholders():
    """The timeline splices it into a UNION whose placeholders it counts."""
    for sql in (lo.live_observations_sql(), lo.live_observations_sql("ob"),
                lo.live_observations_sql("ob", exclude_simulated=True)):
        assert "?" not in sql
    assert "'superseded'" in lo.live_observations_sql()
    with pytest.raises(ValueError):
        lo.live_observations_sql("ob; DROP TABLE observations")


# ═════════════════════════════════════════════════════════════════════════════
#  The guard: one reader
# ═════════════════════════════════════════════════════════════════════════════

SERVER = Path(__file__).resolve().parent.parent

# Reads of observations that are not result reads, plus the helper itself.
# Every entry must still match the guard (test_the_allow_list_does_not_rot), so
# an exemption cannot outlive the read it was written for. The two writers,
# services/ingestion_service.py and services/history_import_service.py, were
# listed in wave 1 but neither reads the table (INSERT and UPDATE do not
# match), so they are not exempt: a read added there later is caught.
ALLOWED = {
    "services/lab_observations.py",       # the one reader
    "routes/admin_routes.py",             # row counts by source on the admin overview
}

_TABLE_READ = re.compile(r"\b(?:from|join)\s+[\"'`\[]?observations\b(?![\"'`\]]?\s*\()", re.IGNORECASE)
_FROM_SQL = re.compile(r"\bfrom_sql\s*=\s*\(?\s*[rRbBfFuU]{0,2}[\"']\s*observations\b")


def _violations(text: str) -> list[int]:
    lines = []
    for pattern in (_TABLE_READ, _FROM_SQL):
        for m in pattern.finditer(text):
            lines.append(text.count("\n", 0, m.start()) + 1)
    return sorted(lines)


def test_the_guard_catches_every_spelling_it_is_meant_to():
    caught = [
        'db.execute("SELECT * FROM observations WHERE patient_id = ?")',
        '"""SELECT id\n   FROM\n      observations\n WHERE x"""',
        "SELECT * FROM vitals v JOIN observations ob ON ob.patient_id = v.patient_id",
        "select value_numeric from observations",
        'SELECT 1 FROM "observations"',
        'from_sql="observations ob",',
        "from_sql = ('observations ob LEFT JOIN users u ON u.id = ob.x'),",
        "from_sql=f'observations {alias}'",
    ]
    for text in caught:
        assert _violations(text), text
    missed = [
        "SELECT * FROM sage_observations",
        "the nurse's observations from the round",
        "escalation_service.scan_new_observations(department=dept)",
        "INSERT INTO observations (patient_id) VALUES (?)",
        "from_sql=lab_observations.from_sql('ob')",
        '"observations_counted": 0',
    ]
    for text in missed:
        assert not _violations(text), text


def test_no_lane_reads_observations_outside_the_one_reader():
    offenders = []
    for base in ("services", "routes"):
        for path in sorted((SERVER / base).rglob("*.py")):
            rel = path.relative_to(SERVER).as_posix()
            if rel in ALLOWED:
                continue
            for line in _violations(path.read_text(encoding="utf-8")):
                offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "These read the observations table directly. Read lab results through "
        "services/lab_observations.py (void and superseded filters, julianday "
        f"order, id tie-break), or add a writer to ALLOWED with a reason: {offenders}"
    )


def test_the_allow_list_does_not_rot():
    for rel in ALLOWED:
        assert (SERVER / rel).is_file(), f"{rel} no longer exists; drop it from ALLOWED"
        assert _violations((SERVER / rel).read_text(encoding="utf-8")), (
            f"{rel} no longer reads observations; drop it from ALLOWED so a future "
            "reader there is caught")


def test_the_writers_are_guarded_like_any_other_file():
    """The ingestion and history-import writers are not exempt: a SELECT of
    observations added to either must go through the helper or come back to
    ALLOWED with a reason."""
    for rel in ("services/ingestion_service.py", "services/history_import_service.py"):
        assert rel not in ALLOWED, rel
        assert (SERVER / rel).is_file(), rel


# ═════════════════════════════════════════════════════════════════════════════
#  Route helpers
# ═════════════════════════════════════════════════════════════════════════════

def _admitted_patient_with_doctor():
    doctor = f"LORD{uuid.uuid4().hex[:8].upper()}"
    pid = _patient(status="admitted")
    _ADMITTED.append(pid)
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Dr Parity', 'doctor', 'x', ?)",
            (doctor, DEPT),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'LR-1', 'LR-B1', 'active', ?)",
            (f"ADM-{pid}", pid, DEPT, doctor),
        )
    return doctor, pid


def _chart_labs(doctor, pid):
    resp = client.get(f"/api/vigil/patients/{pid}",
                      headers={"Authorization": f"Bearer {create_token(doctor, 'doctor')}"})
    assert resp.status_code == 200, resp.text
    return resp.json()["lab_observations"]
