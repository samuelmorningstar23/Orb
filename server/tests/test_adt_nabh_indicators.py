"""NABH utilisation and outcome indicators from the bed movement record
(adt-13b, HIS wave 8, cluster W8-D).

nabh_service computes QI-UTL-01 (bed occupancy) and QI-OUT-04 (return to
intensive care within 48 hours), and reads the inpatient-days denominator
from the bed movement record, ONLY for wards and periods on or after each
ward's wards.ledger_complete_from. Everything else keeps its cannot-compute
reason. What this file defends:

  * with Beds and admissions off nothing changes: nine indicators, the gap
    list still names QI-UTL-01 and QI-OUT-04, the inpatient days are the
    admissions figure with no ledger keys;
  * QI-UTL-01 is listed only for complete wards, and a period that straddles
    a ward's ledger start is refused with words, never counted in part;
  * the occupancy figure is the Beds census's own (midnight census over
    available beds), hand-computed here;
  * the inpatient days come from the record only when it covers the whole
    period, counted on the ward where each patient lay; otherwise the
    admissions figure is used and the reason is printed;
  * the bias rule: a return to intensive care in a NEW admission counts, and
    a patient who died or left within the 48 hours is excluded rather than
    counted as not returned, so the rate can only be higher than the Beds
    census's same-stay count;
  * voided dispositions are excluded;
  * an unreadable record never sinks the pack.

The arithmetic runs on a scratch database holding the suite's real DDL for
every table, so no other test's wards or patients reach it. The route is
checked once on the suite's own database.
"""
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import config
import main
from auth import create_token, hash_password
from database import db_session
from db_backend import DBError
from services import adt_census_service as census
from services import nabh_service as svc

TZ = ZoneInfo("Asia/Kolkata")
D1, D2, D3 = date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12)
P_START, P_END = D1.isoformat(), D3.isoformat()
NOW = datetime(2026, 9, 20, 6, 30, tzinfo=timezone.utc)
COMPLETE = datetime(2026, 9, 1, tzinfo=TZ)
LEDGER_CODES = {"QI-UTL-01", "QI-OUT-04"}


# ═════════════════════════════════════════════════════════════════════════════
#  The scratch database: every table of the suite's schema, no rows
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def ddl() -> list[str]:
    with db_session() as db:
        rows = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'").fetchall()
    return [r["sql"] for r in rows if not r["sql"].upper().startswith("CREATE VIRTUAL")]


@pytest.fixture()
def db(ddl):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for sql in ddl:
        try:
            conn.execute(sql)
        except DBError:
            pass  # a shadow table of a virtual table this build cannot load
    yield conn
    conn.close()


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr("services.hospital_time.hospital_timezone", lambda: TZ)
    monkeypatch.setattr(svc, "_now", lambda: NOW)
    yield


@pytest.fixture()
def adt_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    monkeypatch.setattr("services.hospital_time.hospital_timezone", lambda: TZ)
    monkeypatch.setattr(svc, "_now", lambda: NOW)
    yield


def at(day: date, hh: int, mm: int = 0) -> datetime:
    """A hospital-local wall-clock time, as an aware UTC instant."""
    return datetime.combine(day, time(hh, mm), tzinfo=TZ).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def add_ward(conn, code, *, ward_type="general", complete_from=COMPLETE, active=1):
    wid = f"WRD-{code}"
    conn.execute(
        "INSERT INTO wards (id, code, name, department, ward_type, ledger_complete_from, "
        "is_active, created_at) VALUES (?, ?, ?, 'General Medicine', ?, ?, ?, ?)",
        (wid, code, code, ward_type, iso(complete_from) if complete_from else None, active,
         iso(datetime(2026, 1, 1, tzinfo=timezone.utc))))
    return wid


def add_beds(conn, ward_id, n):
    for i in range(1, n + 1):
        bid = f"{ward_id}/{i}"
        conn.execute(
            "INSERT INTO housekeeping_units (id, unit_code, ward, bed_label, state, ward_id, "
            "is_temporary, created_at) VALUES (?, ?, ?, ?, 'vacant_ready', ?, 0, ?)",
            (bid, bid, ward_id, str(i), ward_id, iso(datetime(2026, 1, 1, tzinfo=timezone.utc))))


def add_stay(conn, aid, *, start, end=None, end_type=None, pid=None):
    status = "active" if end is None else "discharged"
    conn.execute(
        "INSERT INTO admissions (id, patient_id, admitted_by, department, status, created_at, "
        "admitted_at, discharged_at, discharge_type) "
        "VALUES (?, ?, 'D001', 'General Medicine', ?, ?, ?, ?, ?)",
        (aid, pid or f"P-{aid}", status, iso(start), iso(start),
         iso(end) if end else None, end_type))


def add_move(conn, aid, kind, when, *, from_ward=None, to_ward=None, pid=None):
    conn.execute(
        "INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, reason, "
        "moved_at, from_ward_id, to_ward_id, recorded_at) VALUES (?, ?, ?, ?, 'test', ?, ?, ?, ?)",
        (f"MOV-{uuid.uuid4().hex[:10]}", pid or f"P-{aid}", aid, kind, iso(when), from_ward,
         to_ward, iso(when)))


def admit_on(conn, aid, ward_id, start, *, end=None, end_type=None, pid=None):
    add_stay(conn, aid, start=start, end=end, end_type=end_type, pid=pid)
    add_move(conn, aid, "admission", start, to_ward=ward_id, pid=pid)
    if end is not None:
        add_move(conn, aid, "discharge", end, from_ward=ward_id, pid=pid)


def dash(conn, *, ward=None, start=P_START, end=P_END):
    return svc.indicators(period_start=start, period_end=end, ward=ward, db=conn)


def ind(result, indicator_id) -> dict:
    return next(i for i in result["indicators"] if i["id"] == indicator_id)


# ═════════════════════════════════════════════════════════════════════════════
#  Flag off: nothing changes
# ═════════════════════════════════════════════════════════════════════════════

def test_with_the_module_off_the_pack_is_the_nine_and_the_gap_list_is_unchanged(adt_off, db):
    a = add_ward(db, "A")
    add_beds(db, a, 10)
    admit_on(db, "S1", a, at(D1, 1) - timedelta(days=3))
    out = dash(db)
    assert [i["id"] for i in out["indicators"]] == list(svc.INDICATOR_IDS)
    assert len(out["indicators"]) == 9
    codes = {g["code"] for g in out["not_collected_catalogue"]}
    assert LEDGER_CODES <= codes
    assert out["not_collected_catalogue"] == [dict(g) for g in svc.NOT_COLLECTED_CATALOGUE]
    days = out["denominators"]["inpatient_days"]
    assert "ledger" not in days and "source" not in days
    start, end = svc.resolve_period(P_START, P_END)
    assert days == svc._inpatient_days(db, start, end, None)
    assert [i["id"] for i in svc.meta()["indicators"]] == list(svc.INDICATOR_IDS)


def test_the_route_answers_the_nine_with_the_module_off_and_eleven_with_it_on(monkeypatch):
    uid = f"NABHADT{uuid.uuid4().hex[:6].upper()}"
    with db_session() as conn:
        conn.execute("INSERT INTO users (id, name, role, department, password_hash, is_active) "
                     "VALUES (?, 'Quality Desk', 'admin', NULL, ?, 1)", (uid, hash_password("pw")))
    client = TestClient(main.app)
    hdr = {"Authorization": f"Bearer {create_token(uid, 'admin')}"}
    params = {"period_start": "2025-03-01", "period_end": "2025-03-05"}
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    off = client.get("/api/nabh/indicators", params=params, headers=hdr)
    assert off.status_code == 200, off.text
    assert len(off.json()["indicators"]) == 9
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    on = client.get("/api/nabh/indicators", params=params, headers=hdr)
    assert on.status_code == 200, on.text
    body = on.json()
    assert {i["code"] for i in body["indicators"]} >= LEDGER_CODES
    assert len(body["indicators"]) == 11
    assert not LEDGER_CODES & {g["code"] for g in body["not_collected_catalogue"]}
    # Whatever the shared database holds, no ledger indicator prints a number
    # it cannot stand behind.
    for i in body["indicators"]:
        if i["state"] != "reported":
            assert i["value"] is None and i["display"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  QI-UTL-01: bed occupancy
# ═════════════════════════════════════════════════════════════════════════════

def test_occupancy_is_the_census_figure_hand_computed(adt_on, db):
    a = add_ward(db, "A")
    add_beds(db, a, 10)
    # Three patients on A through every midnight of the period, one admitted on
    # D2 at 10:00 and still there: midnight census 3, 4, 4 = 11 over 30 bed-days.
    for n in range(3):
        admit_on(db, f"S{n}", a, at(D1, 1) - timedelta(days=3))
    admit_on(db, "S9", a, at(D2, 10))
    occ = ind(dash(db), "bed_occupancy")
    assert occ["code"] == "QI-UTL-01"
    assert occ["state"] == "reported", occ
    assert occ["numerator"] == 11
    assert occ["denominator"] == 30.0
    assert occ["value"] == round(11 / 30 * 100, 1) == 36.7
    assert occ["display"] == "36.7%"
    assert occ["detail"]["days_counted"] == 3
    assert [w["code"] for w in occ["detail"]["by_ward"]] == ["A"]
    # The same number the Beds census prints for those days.
    every = [dict(r) for r in db.execute("SELECT * FROM wards").fetchall()]
    own = census.census_on(db, first_day=D1, last_day=D3, wards=every, all_wards=every,
                           hospital=True, tz=TZ, now=NOW)
    assert own["hospital"]["totals"]["occupancy"]["nabh"] == occ["value"]


def test_occupancy_is_listed_only_for_complete_wards(adt_on, db):
    a = add_ward(db, "A")
    b = add_ward(db, "B", complete_from=None)
    add_beds(db, a, 10)
    add_beds(db, b, 10)
    admit_on(db, "S1", a, at(D1, 1) - timedelta(days=3))
    admit_on(db, "S2", b, at(D1, 1) - timedelta(days=3))

    hospital = ind(dash(db), "bed_occupancy")
    assert hospital["state"] == "not_computable"
    assert hospital["value"] is None and hospital["display"] is None
    assert "B" in hospital["blockers"][0] and "every ward" in hospital["blockers"][0]
    assert [w["code"] for w in hospital["detail"]["by_ward"]] == ["A"]
    assert [w["code"] for w in hospital["detail"]["wards_not_counted"]] == ["B"]

    only_a = ind(dash(db, ward="a"), "bed_occupancy")   # the filter ignores capitals
    assert only_a["state"] == "reported"
    assert only_a["numerator"] == 3 and only_a["denominator"] == 30.0

    only_b = ind(dash(db, ward="B"), "bed_occupancy")
    assert only_b["state"] == "not_computable"
    assert only_b["blockers"] == ["The bed register of B has not been marked complete in Bed "
                                  "setup, so its bed occupancy is not counted yet."]


def test_a_period_straddling_the_ledger_start_is_refused_with_words(adt_on, db):
    a = add_ward(db, "A", complete_from=at(D2, 9))   # marked complete mid-period
    add_beds(db, a, 10)
    for n in range(3):
        admit_on(db, f"S{n}", a, at(D1, 1) - timedelta(days=3))

    for ward in (None, "A"):
        refused = ind(dash(db, ward=ward), "bed_occupancy")
        assert refused["state"] == "not_computable"
        assert "not complete for the whole period" in refused["blockers"][0] \
            or "Not complete for all of it" in refused["blockers"][0]
        assert refused["value"] is None
        assert refused["detail"]["first_complete_day"] == D3.isoformat()
        # The arithmetic of a part-count is not even kept: there is none.
        assert refused["detail"]["withheld_value"] is None

    # From the first full day after the register was complete, it counts.
    later = ind(dash(db, ward="A", start=D3.isoformat(), end=D3.isoformat()), "bed_occupancy")
    assert later["state"] == "insufficient_data"   # 10 bed-days is under the floor of 30
    assert later["detail"]["withheld_value"] == 30.0


def test_a_day_that_has_not_ended_is_excluded_not_counted(adt_on, db, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: at(D3, 15))
    a = add_ward(db, "A")
    add_beds(db, a, 20)
    for n in range(2):
        admit_on(db, f"S{n}", a, at(D1, 1) - timedelta(days=3))
    occ = ind(dash(db), "bed_occupancy")
    assert occ["excluded"]["days_not_ended"] == 1
    assert occ["detail"]["last_day"] == D2.isoformat()
    assert occ["detail"]["days_counted"] == 2
    assert occ["numerator"] == 4 and occ["denominator"] == 40.0

    monkeypatch.setattr(svc, "_now", lambda: at(D1, 15))
    nothing = ind(dash(db), "bed_occupancy")
    assert nothing["state"] == "not_computable"
    assert nothing["blockers"] == ["No whole day of this period has ended yet. Bed occupancy "
                                   "counts whole days only."]


def test_no_ward_in_bed_setup_reads_not_collected(adt_on, db):
    out = dash(db)
    occ, icu = ind(out, "bed_occupancy"), ind(out, "icu_return_48h")
    assert occ["state"] == icu["state"] == "not_collected"
    assert "No ward is set up in Bed setup yet" in occ["blockers"][0]
    assert out["denominators"]["inpatient_days"]["ledger"]["used"] is False


def test_a_ward_not_in_bed_setup_is_said_so(adt_on, db):
    add_ward(db, "A")
    occ = ind(dash(db, ward="GM-9"), "bed_occupancy")
    assert occ["state"] == "not_computable"
    assert occ["blockers"][0].startswith("GM-9 is not a ward in Bed setup")


# ═════════════════════════════════════════════════════════════════════════════
#  The inpatient-days denominator
# ═════════════════════════════════════════════════════════════════════════════

def test_inpatient_days_come_from_the_record_when_it_covers_the_period(adt_on, db):
    a, b = add_ward(db, "A"), add_ward(db, "B")
    add_beds(db, a, 2)
    add_beds(db, b, 2)
    # On A for the whole 72-hour period.
    admit_on(db, "S1", a, datetime(2026, 9, 5, tzinfo=timezone.utc))
    # On A from the period start, moved to B at exactly 12:00 UTC on the
    # second day: 36 hours on A, 36 on B.
    admit_on(db, "S2", a, datetime(2026, 9, 5, tzinfo=timezone.utc), pid="P2")
    add_move(db, "S2", "transfer", datetime(2026, 9, 11, 12, tzinfo=timezone.utc),
             from_ward=a, to_ward=b, pid="P2")

    hospital = dash(db)["denominators"]["inpatient_days"]
    assert hospital["source"] == "bed_movement_record"
    assert hospital["inpatient_days"] == 6.0
    assert hospital["stays"] == 2 and hospital["open_stays"] == 2

    on_a = dash(db, ward="A")["denominators"]["inpatient_days"]
    assert on_a["inpatient_days"] == 4.5          # 72 h + 36 h on A
    on_b = dash(db, ward="B")["denominators"]["inpatient_days"]
    assert on_b["inpatient_days"] == 1.5          # 36 h on B, never the whole stay
    assert on_b["source"] == "bed_movement_record" and "ledger" not in on_b
    assert on_b["method"] == svc._LEDGER_DAYS_METHOD


def test_inpatient_days_fall_back_with_the_reason_when_a_ward_is_incomplete(adt_on, db):
    a = add_ward(db, "A")
    b = add_ward(db, "B", complete_from=at(D2, 9))
    add_beds(db, a, 2)
    add_beds(db, b, 2)
    admit_on(db, "S1", a, datetime(2026, 9, 5, tzinfo=timezone.utc))
    admit_on(db, "S2", b, datetime(2026, 9, 5, tzinfo=timezone.utc), pid="P2")

    out = dash(db)
    days = out["denominators"]["inpatient_days"]
    assert "source" not in days
    assert days["ledger"]["used"] is False
    assert "B" in days["ledger"]["reason"] and "counted from the admissions" in days["ledger"]["reason"]
    start, end = svc.resolve_period(P_START, P_END)
    plain = svc._inpatient_days(db, start, end, None)
    assert {k: v for k, v in days.items() if k != "ledger"} == plain
    # A complete ward on its own still reads the record.
    assert dash(db, ward="A")["denominators"]["inpatient_days"]["source"] == "bed_movement_record"
    # The pack says which was used and why.
    pack = svc.build_evidence_pack(period_start=P_START, period_end=P_END, db=db)
    assert any("counted from the admissions" in line for line in pack["limitations"])


def test_the_record_closes_a_stay_with_no_end_time_and_says_so(adt_on, db):
    a = add_ward(db, "A")
    add_beds(db, a, 2)
    admit_on(db, "S1", a, datetime(2026, 9, 5, tzinfo=timezone.utc))
    # Ended, no end time anywhere: closed at its last movement (the admission).
    db.execute("UPDATE admissions SET status = 'discharged' WHERE id = 'S1'")
    days = dash(db)["denominators"]["inpatient_days"]
    assert days["source"] == "bed_movement_record"
    assert days["inpatient_days"] == 0.0           # fewer days, never more
    assert days["ended_without_time"] == 1
    pack = svc.build_evidence_pack(period_start=P_START, period_end=P_END, db=db)
    assert any("recorded as ended with no end time" in line for line in pack["limitations"])
    assert any("bed movement record, on the ward where" in line for line in pack["limitations"])


def test_rates_use_the_record_days_as_the_denominator(adt_on, db):
    a = add_ward(db, "A")
    add_beds(db, a, 2)
    admit_on(db, "S1", a, datetime(2026, 9, 5, tzinfo=timezone.utc))
    db.execute(
        "INSERT INTO nabh_incidents (reference, incident_type, severity, title, description, "
        "occurred_at, reported_at, reported_at_precision, ward, status, created_at, updated_at) "
        "VALUES ('INC-T1', 'medication_error', 'minor', 't', 'd', ?, ?, 'exact', 'A', "
        "'reported', ?, ?)", (iso(at(D2, 10)),) * 4)
    rate = ind(dash(db, ward="A"), "medication_error_rate")
    assert rate["denominator"] == 3.0
    assert rate["detail"]["inpatient_days"]["source"] == "bed_movement_record"


# ═════════════════════════════════════════════════════════════════════════════
#  QI-OUT-04: return to intensive care within 48 hours
# ═════════════════════════════════════════════════════════════════════════════

def _icu_fixture(db):
    icu, a = add_ward(db, "ICU", ward_type="icu"), add_ward(db, "A")
    add_beds(db, icu, 4)
    add_beds(db, a, 10)
    before = at(D1, 1) - timedelta(days=2)
    # S1: ICU -> A, back to ICU 22 h later in the same stay: a return.
    admit_on(db, "S1", icu, before, pid="P1")
    add_move(db, "S1", "transfer", at(D1, 10), from_ward=icu, to_ward=a, pid="P1")
    add_move(db, "S1", "transfer", at(D2, 8), from_ward=a, to_ward=icu, pid="P1")
    # S2: ICU -> A, home the same evening, readmitted straight to ICU 21 h
    # after leaving it: a return in a NEW admission.
    add_stay(db, "S2", start=before, end=at(D1, 20), end_type="routine", pid="P2")
    add_move(db, "S2", "admission", before, to_ward=icu, pid="P2")
    add_move(db, "S2", "transfer", at(D1, 12), from_ward=icu, to_ward=a, pid="P2")
    add_move(db, "S2", "discharge", at(D1, 20), from_ward=a, pid="P2")
    admit_on(db, "S3", icu, at(D2, 9), pid="P2")
    # S4: ICU -> A and stays on A: not returned.
    admit_on(db, "S4", icu, before, pid="P4")
    add_move(db, "S4", "transfer", at(D1, 6), from_ward=icu, to_ward=a, pid="P4")
    # S5: ICU -> A, died on A 14 h later: excluded, never 'not returned'.
    add_stay(db, "S5", start=before, end=at(D2, 20), end_type="death", pid="P5")
    add_move(db, "S5", "admission", before, to_ward=icu, pid="P5")
    add_move(db, "S5", "transfer", at(D2, 6), from_ward=icu, to_ward=a, pid="P5")
    # S6: ICU -> A, left against advice 5 h later: excluded.
    add_stay(db, "S6", start=before, end=at(D2, 12), end_type="lama", pid="P6")
    add_move(db, "S6", "admission", before, to_ward=icu, pid="P6")
    add_move(db, "S6", "transfer", at(D2, 7), from_ward=icu, to_ward=a, pid="P6")
    # S7: ICU -> A ten hours before now: still inside its 48 hours.
    admit_on(db, "S7", icu, before, pid="P7")
    add_move(db, "S7", "transfer", at(D3, 10), from_ward=icu, to_ward=a, pid="P7")
    return icu, a


def test_return_to_icu_counts_a_new_admission_and_excludes_the_undecidable(adt_on, db, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: at(D3, 20))
    monkeypatch.setenv("ORB_NABH_MIN_N_ICU_RETURN_48H", "0")
    _icu_fixture(db)
    icu = ind(dash(db), "icu_return_48h")
    assert icu["code"] == "QI-OUT-04"
    assert icu["state"] == "reported", icu
    assert icu["detail"]["moves_out_of_intensive_care"] == 6
    assert icu["numerator"] == 2                    # S1, and S2 by its readmission
    assert icu["detail"]["returned_in_a_new_admission"] == 1
    assert icu["denominator"] == 3                  # S1, S2, S4
    assert icu["excluded"]["died_within_48_hours"] == 1
    assert icu["excluded"]["left_the_hospital_within_48_hours"] == 1
    assert icu["excluded"]["still_inside_48_hours"] == 1
    assert icu["value"] == round(200 / 3, 2) and icu["display"] == "66.7%"

    # The bias rule, proven against the Beds census's same-stay count: there
    # the readmission is missed and the death and LAMA count as not returned,
    # so its rate is lower. The pack may only ever read worse.
    every = [dict(r) for r in db.execute("SELECT * FROM wards").fetchall()]
    own = census.census_on(db, first_day=D1, last_day=D3, wards=every, all_wards=every,
                           hospital=True, tz=TZ, now=at(D3, 20))["hospital"]["totals"]["icu"]
    assert own["returns_48h"] == 1
    assert own["return_rate"] < icu["value"]


def test_return_to_icu_holds_back_a_small_count(adt_on, db, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: at(D3, 20))
    _icu_fixture(db)
    icu = ind(dash(db), "icu_return_48h")
    assert icu["state"] == "insufficient_data"      # 3 decided, floor 10
    assert icu["value"] is None and icu["detail"]["withheld_value"] == round(200 / 3, 2)


def test_return_to_icu_on_one_ward_and_its_refusals(adt_on, db, monkeypatch):
    monkeypatch.setattr(svc, "_now", lambda: at(D3, 20))
    monkeypatch.setenv("ORB_NABH_MIN_N_ICU_RETURN_48H", "0")
    _icu_fixture(db)
    on_icu = ind(dash(db, ward="ICU"), "icu_return_48h")
    assert on_icu["state"] == "reported" and on_icu["numerator"] == 2
    on_a = ind(dash(db, ward="A"), "icu_return_48h")
    assert on_a["state"] == "not_computable"
    assert on_a["blockers"] == ["A is not an intensive care ward, so no return to intensive "
                                "care is counted for it."]
    # Any incomplete ward refuses it, even with the ICU chosen: a patient can
    # leave intensive care for any ward.
    db.execute("UPDATE wards SET ledger_complete_from = ? WHERE code = 'A'", (iso(at(D2, 1)),))
    refused = ind(dash(db, ward="ICU"), "icu_return_48h")
    assert refused["state"] == "not_computable"
    assert "every ward's bed register complete" in refused["blockers"][0]
    assert "Not complete for all of it: A." in refused["blockers"][0]


def test_no_intensive_care_ward_reads_not_collected(adt_on, db):
    a = add_ward(db, "A")
    add_beds(db, a, 2)
    icu = ind(dash(db), "icu_return_48h")
    assert icu["state"] == "not_collected"
    assert "No intensive care ward" in icu["blockers"][0]


# ═════════════════════════════════════════════════════════════════════════════
#  Dispositions, the gap list, the pack and failure
# ═════════════════════════════════════════════════════════════════════════════

def test_voided_dispositions_are_excluded_with_the_module_on(adt_on, db):
    for n, (kind, voided) in enumerate([("death", True), ("routine", False),
                                         ("routine", False)]):
        db.execute(
            "INSERT INTO nabh_discharge_dispositions (patient_id, disposition, occurred_at, "
            "source, voided_at, voided_by, void_reason, created_at, updated_at) "
            "VALUES (?, ?, ?, 'adt', ?, ?, ?, ?, ?)",
            (f"PV{n}", kind, iso(at(D2, 10 + n)), iso(NOW) if voided else None,
             "X" if voided else None, "stay reopened" if voided else None, iso(NOW), iso(NOW)))
    mortality = ind(dash(db), "inpatient_mortality")
    assert mortality["detail"]["by_disposition"] == {"routine": 2}
    assert mortality["detail"]["dispositions_recorded"] == 2
    assert mortality["numerator"] == 0


def test_the_gap_list_and_meta_follow_the_module(adt_on, db):
    out = dash(db)
    assert [i["id"] for i in out["indicators"]][:9] == list(svc.INDICATOR_IDS)
    assert [i["code"] for i in out["indicators"]][9:] == ["QI-UTL-01", "QI-OUT-04"]
    assert out["summary"]["total"] == 11
    assert not LEDGER_CODES & {g["code"] for g in out["not_collected_catalogue"]}
    meta_ids = [i["id"] for i in svc.meta()["indicators"]]
    assert meta_ids == list(svc.INDICATOR_IDS) + ["bed_occupancy", "icu_return_48h"]
    pack = svc.build_evidence_pack(period_start=P_START, period_end=P_END, db=db)
    assert not LEDGER_CODES & {g["code"] for g in pack["not_collected"]}
    # The module constants themselves are untouched.
    assert len(svc.INDICATOR_IDS) == 9
    assert LEDGER_CODES <= {g["code"] for g in svc.NOT_COLLECTED_CATALOGUE}


def test_the_markdown_pack_prints_words_for_a_refused_ledger_indicator(adt_on, db):
    a = add_ward(db, "A", complete_from=at(D2, 9))
    add_beds(db, a, 10)
    pack = svc.build_evidence_pack(period_start=P_START, period_end=P_END, db=db)
    text = svc.render_pack_markdown(pack)
    row = next(line for line in text.splitlines() if line.startswith("| QI-UTL-01 |"))
    assert "**NOT COMPUTABLE**" in row
    assert "%" not in row.split("|")[3]
    assert "\u2014" not in "".join(ind(pack, i)["blockers"][0] for i in ("bed_occupancy",))


def test_an_unreadable_record_never_sinks_the_pack(adt_on, db, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(census, "_load_stays", boom)
    add_ward(db, "A")
    out = dash(db)
    assert len(out["indicators"]) == 11
    for indicator_id in ("bed_occupancy", "icu_return_48h"):
        refused = ind(out, indicator_id)
        assert refused["state"] == "not_computable"
        assert "could not be read" in refused["blockers"][0]
    assert out["denominators"]["inpatient_days"]["ledger"]["used"] is False


def test_no_new_words_carry_an_em_dash():
    for spec in svc.ADT_INDICATOR_SPEC.values():
        for key in ("label", "definition", "caveat", "numerator_label", "denominator_label"):
            assert "\u2014" not in spec[key]
    assert "\u2014" not in svc._LEDGER_DAYS_METHOD
