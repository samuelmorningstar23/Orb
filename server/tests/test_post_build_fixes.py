"""Post-build fixes, wave 15 of the whole-HIS build (21 October 2026).

Defects the wave 13 and 14 verifiers found, each pinned by a test that fails
on the code before its fix:

  * fix-1: the Command Center's OR today is the hospital's calendar day
    (services.hospital_time), not the UTC date. Between 00:00 and 05:30 IST
    the UTC date is still yesterday, so the board listed yesterday's theatre
    list for the first five and a half hours of every day.
  * fix-2: department_master.department_references passes over virtual
    tables. The knowledge corpus's vec0 table cannot even be described on a
    connection that has not loaded sqlite-vec (every ordinary db_session), so
    the scan raised "no such module: vec0" and move_department failed on any
    appliance holding the corpus. Every ordinary table is still read.
  * fix-3: the facility report (the operations page's "departments not in
    the list") leaves out patient portal logins, whose department column
    holds the ward admission wrote there, not a department.

fix-4 (the facility_created audit row carries the request) is pinned in
tests/test_facility_routes.py beside the other create tests.

This module owns its database (isolated_module_database), so the second
facility fix-2 needs never reaches another module. Test names are invented
words no clinician types. Run with the test env, never the shipping .env.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import config
from database import db_session
from services import command_center_service as cc
from services import department_master as DM
from services import facility_service as FS
from services import hospital_time
from services.vitals_source import hl7v2

pytestmark = pytest.mark.usefixtures("isolated_module_database")

TAG = uuid.uuid4().hex[:6].upper()
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _fresh_facility_cache():
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


# ═════════════════════════════════════════════════════════════════════════════
#  fix-1: OR today is the hospital's day
# ═════════════════════════════════════════════════════════════════════════════

OR_PATIENT = f"PBFOR{TAG}"
OR_SURGEON = f"PBFSURG{TAG}"
# Three hospital days in a row, far from the wall clock, one case on each.
OR_DAYS = (date(2031, 3, 14), date(2031, 3, 15), date(2031, 3, 16))


def _case_id(day: date) -> str:
    return f"PBFS{TAG}{day:%d}"


@pytest.fixture(scope="module")
def theatre_list(isolated_module_database):
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash) "
            "VALUES (?, 'Corvane Hallistrom', 'doctor', NULL, 'x')",
            (OR_SURGEON,),
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status) "
            "VALUES (?, 'Orsolya Quandt', ?, 58, 'F', 'Tarnwick Ward', 'T-4', "
            "'Brellish hernia', 'admitted')",
            (OR_PATIENT, f"MRN{OR_PATIENT}"),
        )
        for day in OR_DAYS:
            db.execute(
                "INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis, "
                "scheduled_date, scheduled_time, status) "
                "VALUES (?, ?, ?, 'Quillan repair', 'Brellish hernia', ?, '07:30', 'scheduled')",
                (_case_id(day), OR_PATIENT, OR_SURGEON, day.isoformat()),
            )
    yield


@pytest.fixture()
def ist_ward(monkeypatch):
    """The appliance (and so the hospital) is in Asia/Kolkata, whatever the
    machine running the suite says."""
    monkeypatch.setattr(hl7v2, "_default_tz", IST)
    monkeypatch.setattr(hl7v2, "_default_tz_resolved", True)


def _pin(monkeypatch, instant: datetime) -> None:
    """Both clocks at one instant: the hospital day resolver's, and the
    Command Center's own, which the UTC date used to be taken from."""
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: instant)
    monkeypatch.setattr(cc, "_now", lambda: instant)


@pytest.mark.parametrize(
    "instant,hospital_day",
    [
        # 00:30 IST on 15 March is 19:00 UTC on the 14th: the UTC date is
        # still yesterday, the hospital's day is the 15th.
        (datetime(2031, 3, 14, 19, 0, tzinfo=UTC), date(2031, 3, 15)),
        # 05:29 IST, the last minute the two dates differ.
        (datetime(2031, 3, 14, 23, 59, tzinfo=UTC), date(2031, 3, 15)),
        # 23:50 IST on the 15th: both clocks say the 15th.
        (datetime(2031, 3, 15, 18, 20, tzinfo=UTC), date(2031, 3, 15)),
        # 00:01 IST on the 16th is 18:31 UTC on the 15th.
        (datetime(2031, 3, 15, 18, 31, tzinfo=UTC), date(2031, 3, 16)),
    ],
)
def test_or_today_lists_the_hospitals_day(
    theatre_list, ist_ward, monkeypatch, instant, hospital_day
):
    _pin(monkeypatch, instant)
    assert hospital_time.hospital_today() == hospital_day
    cases = cc._or_today()["cases"]
    assert [c["id"] for c in cases] == [_case_id(hospital_day)]


def test_just_after_midnight_the_board_shows_tonights_list(theatre_list, ist_ward, monkeypatch):
    """00:30 IST through the public snapshot: the night team sees today's
    cases, never yesterday's."""
    _pin(monkeypatch, datetime(2031, 3, 14, 19, 0, tzinfo=UTC))
    snap = cc.house_snapshot()
    assert snap["or_today"]["count"] == 1
    assert [c["id"] for c in snap["or_today"]["cases"]] == [_case_id(date(2031, 3, 15))]


# ═════════════════════════════════════════════════════════════════════════════
#  fix-2: department_references passes over virtual tables
# ═════════════════════════════════════════════════════════════════════════════

F2 = 9417
F2_CODE = f"PB{TAG[:4]}"
DEPT_USED = f"Halvorine Medicine {TAG}"
DEPT_FREE = f"Halvorine Annex {TAG}"
VEC_TABLE = f"pbf_probe_vec_{TAG.lower()}"
FTS_TABLE = f"pbf_probe_fts_{TAG.lower()}"
ADMIN = f"PBFADM{TAG}"
NURSE = f"PBFNUR{TAG}"
DEPT_PATIENT = f"PBFDP{TAG}"

#: Every ordinary table the departments world below names DEPT_USED in.
ORDINARY_REFERENCES = [
    {"where": "admissions", "column": "department"},
    {"where": "channels", "column": "name"},
    {"where": "clinical_escalations", "column": "raised_in_department"},
    {"where": "users", "column": "department"},
    {"where": "wards", "column": "department"},
]


def _on_a_fresh_connection(fn: Callable[[], Any]) -> Any:
    """Run `fn` on a new thread, so on a pooled connection of its own, then
    retire that connection. A loadable extension is per connection, and
    db_backend pools one connection per thread and database: whatever loads
    sqlite-vec here leaves the connections of the thread running the tests
    exactly as they were (see test_dpdp_compliance._on_a_fresh_connection)."""
    box: dict = {}

    def _run() -> None:
        from database import get_db
        from db_backend import discard_pooled, is_pooled

        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc
        finally:
            try:
                conn = get_db()
                if is_pooled(conn):
                    discard_pooled(conn)
            except Exception:
                pass

    worker = threading.Thread(target=_run, name="post-build-fresh-conn")
    worker.start()
    worker.join(60)
    assert not worker.is_alive(), "fresh-connection worker did not finish"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _with_sqlite_vec(statement: str) -> None:
    import sqlite_vec

    def _run() -> None:
        with db_session() as db:
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.execute(statement)

    _on_a_fresh_connection(_run)


@pytest.fixture(scope="module")
def departments_world(isolated_module_database):
    """A second facility, a department every kind of ordinary record names,
    an unused one, and a group-wide administrator who may move either."""
    with db_session() as db:
        db.execute(
            "INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, 'Halvorine Satellite')",
            (F2, str(uuid.uuid4()), F2_CODE),
        )
        for name in (DEPT_USED, DEPT_FREE):
            db.execute(
                "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
                (name,),
            )
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, 'Ismeray Coldwater', 'admin', NULL, 'x', 1)",
            (ADMIN,),
        )
        db.execute(
            "INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, reason) "
            "VALUES (?, 'system', CURRENT_TIMESTAMP, 'post-build fixes')",
            (ADMIN,),
        )
        db.execute(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, 'Tamsk Ollivenne', 'nurse', ?, 'x', 1)",
            (NURSE, DEPT_USED),
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) "
            "VALUES (?, 'Wendrel Pashcourt', ?, 'discharged')",
            (DEPT_PATIENT, f"MRN{DEPT_PATIENT}"),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, status) "
            "VALUES (?, ?, 'system', ?, 'discharged')",
            (f"A{DEPT_PATIENT}", DEPT_PATIENT, DEPT_USED),
        )
        db.execute(
            "INSERT INTO wards (id, code, name, department, created_at) "
            "VALUES (?, ?, 'Halvorine Ward', ?, CURRENT_TIMESTAMP)",
            (f"W{TAG}", f"HLW-{TAG}", DEPT_USED),
        )
        db.execute(
            "INSERT INTO clinical_escalations (id, patient_id, raised_in_department, status, "
            "raised_at) VALUES (?, ?, ?, 'resolved', CURRENT_TIMESTAMP)",
            (f"E{DEPT_PATIENT}", DEPT_PATIENT, DEPT_USED),
        )
        db.execute(
            "INSERT INTO channels (id, name, type) VALUES (?, ?, 'department')",
            (f"chan_{TAG.lower()}", DEPT_USED),
        )
    FS.invalidate_cache()
    yield {"admin": {"id": ADMIN, "role": "admin", "department": None}}
    FS.invalidate_cache()


@pytest.fixture()
def corpus_vector_table(departments_world):
    """The knowledge corpus's arrangement: a vec0 table made on a connection
    that loaded sqlite-vec, in a database every other connection reads
    without it."""
    _with_sqlite_vec(f"CREATE VIRTUAL TABLE {VEC_TABLE} USING vec0(embedding float[4])")
    try:
        yield VEC_TABLE
    finally:
        _with_sqlite_vec(f"DROP TABLE IF EXISTS {VEC_TABLE}")


@pytest.fixture()
def multi_site_on(monkeypatch):
    monkeypatch.setattr(config, "MULTI_SITE_ENABLED", True)


def _unreadable_here(db: Any, table: str) -> str | None:
    """The error describing `table` raises on this connection, or None."""
    try:
        db.execute(f'PRAGMA table_info("{table}")').fetchall()
    except Exception as exc:
        return str(exc)
    return None


def test_the_scan_reads_every_ordinary_table_beside_a_vec0_table(corpus_vector_table):
    with db_session() as db:
        # The hazard is real on this connection, or the scan below proves nothing.
        err = _unreadable_here(db, corpus_vector_table)
        assert err and "vec0" in err, (
            f"precondition failed: this connection describes {corpus_vector_table} ({err!r})"
        )
        used = DM.department_references(db, DEPT_USED)
        unused = DM.department_references(db, DEPT_FREE)
    assert sorted(used, key=lambda r: (r["where"], r["column"])) == ORDINARY_REFERENCES
    assert unused == []


def _department(name: str) -> tuple[int, int]:
    """(id, facility_id) of a departments row."""
    with db_session() as db:
        row = db.execute("SELECT id, facility_id FROM departments WHERE name = ?", (name,))
        found = row.fetchone()
    return int(found["id"]), int(found["facility_id"])


def test_a_department_in_use_still_cannot_move_beside_a_vec0_table(
    corpus_vector_table, multi_site_on
):
    dept_id, _ = _department(DEPT_USED)
    with pytest.raises(DM.DepartmentError) as err:
        DM.move_department(
            dept_id, F2, actor={"id": ADMIN, "role": "admin"}, reason="Satellite opened"
        )
    assert err.value.code == "department_in_use"
    assert err.value.extra["where"] == sorted({r["where"] for r in ORDINARY_REFERENCES})
    assert _department(DEPT_USED) == (dept_id, 1)


def test_an_unused_department_moves_beside_a_vec0_table(corpus_vector_table, multi_site_on):
    dept_id, _ = _department(DEPT_FREE)
    moved = DM.move_department(
        dept_id, F2, actor={"id": ADMIN, "role": "admin"}, reason="Belongs to the satellite"
    )
    assert moved["facility_id"] == F2
    with db_session() as db:
        row = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE action_type = "
            "'department_facility_changed' AND target_id = ? ORDER BY id DESC LIMIT 1",
            (str(dept_id),),
        ).fetchone()
        assert row is not None
        db.execute("UPDATE departments SET facility_id = 1 WHERE id = ?", (dept_id,))
    FS.invalidate_cache()


class _NoVirtualTableReads:
    """A connection on which describing or reading one virtual table raises,
    as a vec0 table does on a connection without sqlite-vec. Its shadow
    tables (ordinary tables named after it) and everything else go to the
    real connection. The scan quotes every table name it reads."""

    def __init__(self, db: Any, virtual: str) -> None:
        self._db, self._quoted = db, f'"{virtual}"'

    def execute(self, sql: str, *args: Any) -> Any:
        if self._quoted in sql:
            raise RuntimeError(f"no such module while reading {self._quoted}")
        return self._db.execute(sql, *args)


def test_a_virtual_table_is_passed_over_before_it_is_described(departments_world):
    """The same skip with a built-in module (fts5), here holding a column
    named department with the department in it: the scan never touches the
    virtual table, and still finds every ordinary reference."""
    with db_session() as db:
        db.execute(f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5(department)")
        db.execute(f"INSERT INTO {FTS_TABLE} (department) VALUES (?)", (DEPT_FREE,))
    try:
        with db_session() as db:
            guarded = _NoVirtualTableReads(db, FTS_TABLE)
            used = DM.department_references(guarded, DEPT_USED)
            unused = DM.department_references(guarded, DEPT_FREE)
    finally:
        with db_session() as db:
            db.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
    assert sorted(used, key=lambda r: (r["where"], r["column"])) == ORDINARY_REFERENCES
    assert unused == []


# ═════════════════════════════════════════════════════════════════════════════
#  fix-3: the facility report leaves out portal logins
# ═════════════════════════════════════════════════════════════════════════════

PORTAL_WARD = f"Pellucine Ward {TAG}"
STAFF_UNLISTED = f"Brannoch Clinic {TAG}"
LISTED = f"Fellowmere Medicine {TAG}"


@pytest.fixture(scope="module")
def report_world(isolated_module_database):
    with db_session() as db:
        db.execute(
            "INSERT INTO departments (name, facility_id, seeded_from) VALUES (?, 1, 'test')",
            (LISTED,),
        )
        rows = [
            # A patient portal login: admission writes the ward here.
            (f"PBFPAT{TAG}", "Ysaline Corrowmont", "patient", PORTAL_WARD),
            (f"PBFPAT2{TAG}", "Daverin Holloquist", "patient", LISTED),
            # Staff: one in a department nobody listed, one in a listed one.
            (f"PBFST1{TAG}", "Merrow Thistleby", "nurse", STAFF_UNLISTED),
            (f"PBFST2{TAG}", "Ondine Varcastle", "doctor", LISTED),
        ]
        db.executemany(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, 'x', 1)",
            rows,
        )
    yield


def _missing(report: dict) -> dict:
    return {(m["where"], m["name"]): m["rows"] for m in report["departments_not_in_master"]}


def test_a_portal_logins_ward_is_not_a_missing_department(report_world):
    with db_session() as db:
        report = FS.facility_report(db)
    missing = _missing(report)
    assert PORTAL_WARD not in {name for _, name in missing}
    # A staff account in a department nobody listed is still reported.
    assert missing[("staff accounts", STAFF_UNLISTED)] == 1
    assert LISTED not in {name for _, name in missing}


def test_a_portal_login_adds_no_row_to_a_staff_departments_count(report_world):
    """A ward that is also a staff account's unlisted department counts the
    staff account only."""
    shared = f"Quorrel Ward {TAG}"
    with db_session() as db:
        db.executemany(
            "INSERT INTO users (id, name, role, department, password_hash, is_active) "
            "VALUES (?, ?, ?, ?, 'x', 1)",
            [
                (f"PBFQP{TAG}", "Ilsabet Marrowfield", "patient", shared),
                (f"PBFQN{TAG}", "Kestran Doolittan", "nurse", shared),
            ],
        )
        report = FS.facility_report(db)
    assert _missing(report)[("staff accounts", shared)] == 1
