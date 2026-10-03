"""Chart timeline + the "since you last looked" watermark.

Three groups, and they exist for three different reasons.

1. **The timestamp trap.** The columns this feature reads hold two dialects in
   the same column — SQLite's bare ``"2026-08-08 01:15:00"`` and the application's
   ``"2026-08-08T01:15:00.123456+00:00"``. Sorting those as strings is wrong in
   two independent ways, and BOTH are asserted below with fixtures that a naive
   ``ORDER BY ts`` passes and a correct implementation must not. A timeline that
   silently reorders a chart is worse than no timeline.

2. **Scope and audit.** These routes return PHI, so they get the same treatment
   as every other PHI-returning GET after the 2026-08-05 leak: a cross-ward read
   is a 404 (never a 403 — that confirms the patient exists), and every read
   that returns leaves a ``phi_access`` row on the hash chain.

3. **The counting contract.** "6 new" above a rail capped at N must be true. The
   count is computed by the database over every row, so there is a test that
   caps the rail below the event count and asserts the number does not follow it
   down.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import patient_timeline_service as pts

client = TestClient(app)

DEPT_A = "Timeline Cardiology"
DEPT_B = "Timeline Oncology"
SECRET_NOTE = "TIMELINE SECRET NOTE for the other ward"

# Every patient this module creates carries this prefix, so the teardown below
# finds them all without a hand-kept list.
_PID_PREFIX = "TLP"


@pytest.fixture(autouse=True, scope="module")
def _discharge_this_modules_patients():
    """TEST-ISOLATION LAW — see the twin fixture in tests/test_patient_story.py
    for the incident this prevents.

    Short version: `patients.status` DEFAULTS to 'admitted', so without this
    every patient created here stays on the shared session DB's ward for the
    rest of the run, where it taxes the FHIR self-export and — because Sage's
    name matcher is fuzzy at 0.70 by design — can answer to a name it was never
    meant to. Fixture names in this module are also checked against that matcher
    ("Timeline Patient" scored 0.741 against the phrase "the patient" and was
    renamed for exactly that reason).

    Discharge, do not DELETE: audit rows written during these tests legitimately
    reference these ids. Close the ADMISSIONS row too, or the residue is clean on
    the census and still live on the ward board.
    """
    yield
    with db_session() as db:
        db.execute(
            "UPDATE patients SET status = 'discharged' WHERE id LIKE ?",
            (f"{_PID_PREFIX}%",),
        )
        db.execute(
            "UPDATE admissions SET status = 'discharged' "
            "WHERE patient_id LIKE ? AND status = 'active'",
            (f"{_PID_PREFIX}%",),
        )


# ── fixtures ─────────────────────────────────────────────────────────────────

def _mk_user(role, department):
    uid = f"TLU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Timeline {role}", role, department),
        )
    return uid


def _auth(user_id, role):
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


def _mk_patient(department, *, name="Orbfixture Wardcase", status="admitted",
                admitted_at="2026-07-01T00:00:00Z"):
    """Patient whose department lives on the ADMISSION, with patients.ward set to
    a DIFFERENT string — an implementation that scoped on the free-text ward
    label would pass a naive test and must fail this one.

    `admitted_at` is a FIXED past instant, not `now()`. The admission is itself a
    timeline event, so a "now" admission silently lands after every watermark in
    this file and every new-count assertion becomes off-by-one on a date change.
    """
    pid = f"TLP{uuid.uuid4().hex[:8].upper()}"
    now = admitted_at
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('TLADM', 'Timeline Admitter', 'doctor', 'x')"
        )
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, diagnosis, status,
                                     admission_date)
               VALUES (?, ?, ?, 'TL-1', ?, 'Timeline pneumonia', ?, ?)""",
            (pid, name, f"MRN-{uuid.uuid4().hex[:8].upper()}",
             f"{department} bay", status, now),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'TL-1', ?, 'TLADM')",
            (pid, department, department,
             "active" if status == "admitted" else "discharged"),
        )
    return pid


def _note(pid, content, created_at):
    with db_session() as db:
        db.execute(
            "INSERT INTO clinical_documents (patient_id, doc_type, title, content, "
            "author_id, created_at) VALUES (?, 'Progress Note', 'Timeline', ?, 'TLADM', ?)",
            (pid, content, created_at),
        )


def _order(pid, description, created_at, priority="routine"):
    oid = f"TLO{uuid.uuid4().hex[:10].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO orders (id, patient_id, order_type, description, priority,
                                   status, ordered_by, created_at)
               VALUES (?, ?, 'lab', ?, ?, 'active', 'TLADM', ?)""",
            (oid, pid, description, priority, created_at),
        )
    return oid


@pytest.fixture(scope="module")
def world():
    doctor_a = _mk_user("doctor", DEPT_A)
    doctor_b = _mk_user("doctor", DEPT_B)
    nurse_a = _mk_user("nurse", DEPT_A)
    admin = _mk_user("admin", None)  # governance account: no department on record
    p_a = _mk_patient(DEPT_A, name="Orbfixture Minecase")
    p_b = _mk_patient(DEPT_B, name="Orbfixture Theirscase")
    _note(p_b, SECRET_NOTE, datetime.now(timezone.utc).isoformat())
    return {"doctor_a": doctor_a, "doctor_b": doctor_b, "nurse_a": nurse_a,
            "admin": admin, "p_a": p_a, "p_b": p_b}


def _phi_rows(action_type, patient_id):
    with db_session() as db:
        return db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE event_category = 'phi_access' "
            "AND action_type = ? AND patient_id = ?",
            (action_type, patient_id),
        ).fetchone()["n"]


# ═════════════════════════════════════════════════════════════════════════════
#  1. The timestamp trap
# ═════════════════════════════════════════════════════════════════════════════

def test_normalize_ts_reads_both_dialects_as_the_same_instant():
    """The bare SQLite spelling and the application ISO spelling of one instant
    must normalise to one datetime. A bare string read as LOCAL time shifts every
    charted event by the appliance's UTC offset."""
    bare = pts._normalize_ts("2026-08-08 01:15:00")
    iso_z = pts._normalize_ts("2026-08-08T01:15:00Z")
    iso_off = pts._normalize_ts("2026-08-08T06:45:00+05:30")
    assert bare == iso_z == iso_off
    assert bare.tzinfo is not None and bare.utcoffset() == timedelta(0)
    # Pinned to an ABSOLUTE instant, not just to each other: reading the bare
    # dialect as local time (the defect src/lib/datetime.ts exists to stop on the
    # browser side) still makes the three agree with one another on a UTC+0 host,
    # and shifts all three off the real instant everywhere else.
    assert bare == datetime(2026, 8, 8, 1, 15, tzinfo=timezone.utc)


def test_normalize_ts_returns_none_for_unparseable_rather_than_a_sentinel():
    """None means "drop the row". Substituting the epoch would park an untimed
    event at the far end of the chart pretending to be real."""
    assert pts._normalize_ts("not a timestamp") is None
    assert pts._normalize_ts("") is None
    assert pts._normalize_ts(None) is None


def test_string_sorting_of_mixed_dialects_is_wrong_and_the_service_is_not():
    """The regression this whole normalisation layer exists for.

    Two independent lexical traps, both present in real Orb data:
      * ``' '`` (0x20) sorts BEFORE ``'T'`` (0x54) → every bare SQLite timestamp
        sorts before every ISO one regardless of instant.
      * ``'Z'`` (0x5A) sorts AFTER ``'.'`` (0x2E) → a whole-second stamp compares
        as LATER than a sub-second one inside the same second.
    """
    later_bare = "2026-08-08 09:00:00"          # the actually-later event
    earlier_iso = "2026-08-08T08:00:00.500000Z"
    assert later_bare < earlier_iso, "premise: naive string order is inverted here"
    assert pts._normalize_ts(later_bare) > pts._normalize_ts(earlier_iso)

    whole = "2026-08-08T09:00:00Z"
    sub = "2026-08-08T09:00:00.500000Z"
    assert whole > sub, "premise: 'Z' sorts after '.' lexically"
    assert pts._normalize_ts(whole) < pts._normalize_ts(sub)


def test_timeline_is_newest_first_across_mixed_dialects(world):
    """End to end: notes written in both spellings come back in true order."""
    pid = _mk_patient(DEPT_A)
    _note(pid, "TL-OLDEST", "2026-08-01 08:00:00")            # bare
    _note(pid, "TL-MIDDLE", "2026-08-02T08:00:00.250000Z")    # ISO sub-second
    _note(pid, "TL-NEWEST", "2026-08-03T08:00:00+00:00")      # ISO offset

    result = pts.build_chart_timeline(pid, user_id=world["doctor_a"])
    notes = [e for e in result["events"] if e["kind"] == "note"]
    assert [n["detail"] for n in notes] == ["TL-NEWEST", "TL-MIDDLE", "TL-OLDEST"]


def test_events_with_unreadable_timestamps_are_dropped_not_misplaced(world):
    pid = _mk_patient(DEPT_A)
    _note(pid, "TL-GOOD", "2026-08-02T08:00:00Z")
    _note(pid, "TL-BROKEN", "not-a-date")

    result = pts.build_chart_timeline(pid, user_id=world["doctor_a"])
    details = [e["detail"] for e in result["events"]]
    assert "TL-GOOD" in details
    assert "TL-BROKEN" not in details


# ═════════════════════════════════════════════════════════════════════════════
#  2. The counting contract
# ═════════════════════════════════════════════════════════════════════════════

def test_no_review_mark_means_no_new_badge_at_all(world):
    """Absent watermark is "never reviewed", NOT "everything is new" — otherwise
    a clinician's first login lights up every bed and the marker is dead on
    arrival."""
    pid = _mk_patient(DEPT_A)
    _note(pid, "TL-A", "2026-08-02T08:00:00Z")
    result = pts.build_chart_timeline(pid, user_id=world["doctor_a"])
    assert result["last_reviewed_at"] is None
    assert result["new_count"] == 0
    assert all(e["is_new"] is False for e in result["events"])


def test_new_count_counts_only_events_after_the_watermark(world):
    pid = _mk_patient(DEPT_A)
    uid = world["doctor_a"]
    _note(pid, "TL-BEFORE-1", "2026-08-02T08:00:00Z")
    _note(pid, "TL-BEFORE-2", "2026-08-02T09:00:00Z")
    pts.mark_reviewed(uid, pid, as_of="2026-08-02T12:00:00Z")
    _note(pid, "TL-AFTER-1", "2026-08-03T08:00:00Z")
    _order(pid, "TL Serum lactate", "2026-08-03 09:00:00")  # bare dialect on purpose

    result = pts.build_chart_timeline(pid, user_id=uid)
    assert result["new_count"] == 2
    new_titles = {e["detail"] or e["title"] for e in result["events"] if e["is_new"]}
    assert "TL-AFTER-1" in new_titles
    assert any("TL Serum lactate" in (e["title"] or "") for e in result["events"] if e["is_new"])


def test_new_count_is_exact_even_when_the_rail_is_truncated(world):
    """"6 new" above a capped list has to still be true — the count comes from
    the database over all rows, not from counting what was returned."""
    pid = _mk_patient(DEPT_A)
    uid = world["doctor_a"]
    pts.mark_reviewed(uid, pid, as_of="2026-08-01T00:00:00Z")
    for i in range(12):
        _note(pid, f"TL-BULK-{i}", f"2026-08-02T{i:02d}:00:00Z")

    result = pts.build_chart_timeline(pid, user_id=uid, limit=3)
    assert len(result["events"]) == 3
    assert result["truncated"] is True
    # 12, not 3: the count comes from the DB across every row, and the admission
    # (2026-07-01) predates the watermark so it is correctly not among them.
    assert result["new_count"] == 12


def test_mark_reviewed_never_moves_backwards(world):
    """A replayed offline request must not un-review a chart the clinician has
    since read — that resurrects a badge over events already acted on."""
    pid = _mk_patient(DEPT_A)
    uid = world["doctor_a"]
    pts.mark_reviewed(uid, pid, as_of="2026-08-05T10:00:00Z")
    stale = pts.mark_reviewed(uid, pid, as_of="2026-08-01T10:00:00Z")

    assert stale["advanced"] is False
    assert pts._normalize_ts(stale["last_reviewed_at"]) == pts._normalize_ts("2026-08-05T10:00:00Z")


def test_mark_reviewed_reports_the_previous_watermark(world):
    """The route writes `previous` into the audit row; "reviewed up to X" is only
    reconstructable afterwards if the value survives the write."""
    pid = _mk_patient(DEPT_A)
    uid = world["doctor_a"]
    pts.mark_reviewed(uid, pid, as_of="2026-08-05T10:00:00Z")
    second = pts.mark_reviewed(uid, pid, as_of="2026-08-06T10:00:00Z")
    assert pts._normalize_ts(second["previous"]) == pts._normalize_ts("2026-08-05T10:00:00Z")
    assert second["advanced"] is True


def test_mark_reviewed_rejects_an_unparseable_as_of(world):
    with pytest.raises(ValueError):
        pts.mark_reviewed(world["doctor_a"], world["p_a"], as_of="whenever")


def test_watermarks_are_per_clinician(world):
    """The consultant's marker must not be advanced by the night nurse opening
    the same chart."""
    pid = _mk_patient(DEPT_A)
    _note(pid, "TL-SHARED", "2026-08-03T08:00:00Z")
    pts.mark_reviewed(world["doctor_a"], pid, as_of="2026-08-01T00:00:00Z")

    doc = pts.build_chart_timeline(pid, user_id=world["doctor_a"])
    nurse = pts.build_chart_timeline(pid, user_id=world["nurse_a"])
    assert doc["new_count"] > 0
    assert nurse["last_reviewed_at"] is None and nurse["new_count"] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  3. Ward scoping and audit
# ═════════════════════════════════════════════════════════════════════════════

def test_route_returns_the_chart_for_a_patient_in_scope(world):
    r = client.get(f"/api/chart/patients/{world['p_a']}/timeline",
                   headers=_auth(world["doctor_a"], "doctor"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient_id"] == world["p_a"]
    assert any(e["kind"] == "admission" for e in body["events"])


def test_route_404s_a_cross_ward_chart_and_leaks_nothing(world):
    r = client.get(f"/api/chart/patients/{world['p_b']}/timeline",
                   headers=_auth(world["doctor_a"], "doctor"))
    # 404, not 403: a 403 confirms the patient exists, which is itself PHI.
    assert r.status_code == 404
    assert SECRET_NOTE not in r.text
    assert world["p_b"] not in r.text


def test_route_reviewed_404s_cross_ward(world):
    r = client.post(f"/api/chart/patients/{world['p_b']}/reviewed", json={},
                    headers=_auth(world["doctor_a"], "doctor"))
    assert r.status_code == 404
    with db_session() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM patient_review_marks WHERE user_id = ? AND patient_id = ?",
            (world["doctor_a"], world["p_b"]),
        ).fetchone()
    assert row["n"] == 0, "a refused chart must not leave a watermark behind"


def test_unscoped_governance_account_still_reads_across_wards(world):
    """The fix must not be over-applied: department None is a genuinely unscoped
    account, not "scope unknown"."""
    r = client.get(f"/api/chart/patients/{world['p_b']}/timeline",
                   headers=_auth(world["admin"], "admin"))
    assert r.status_code == 200, r.text


def test_reading_a_chart_timeline_writes_a_phi_access_row(world):
    pid = _mk_patient(DEPT_A)
    before = _phi_rows("patient_timeline_viewed", pid)
    r = client.get(f"/api/chart/patients/{pid}/timeline",
                   headers=_auth(world["doctor_a"], "doctor"))
    assert r.status_code == 200
    assert _phi_rows("patient_timeline_viewed", pid) == before + 1


def test_marking_reviewed_writes_a_phi_access_row(world):
    pid = _mk_patient(DEPT_A)
    before = _phi_rows("patient_timeline_reviewed", pid)
    r = client.post(f"/api/chart/patients/{pid}/reviewed", json={},
                    headers=_auth(world["doctor_a"], "doctor"))
    assert r.status_code == 200, r.text
    assert _phi_rows("patient_timeline_reviewed", pid) == before + 1


def test_route_reviewed_clears_the_badge_end_to_end(world):
    pid = _mk_patient(DEPT_A)
    hdr = _auth(world["doctor_a"], "doctor")
    _note(pid, "TL-E2E-1", "2026-08-02T08:00:00Z")

    client.post(f"/api/chart/patients/{pid}/reviewed",
                json={"as_of": "2026-08-01T00:00:00Z"}, headers=hdr)
    assert client.get(f"/api/chart/patients/{pid}/timeline",
                      headers=hdr).json()["new_count"] >= 1

    assert client.post(f"/api/chart/patients/{pid}/reviewed", json={},
                       headers=hdr).json()["new_count"] == 0
    assert client.get(f"/api/chart/patients/{pid}/timeline",
                      headers=hdr).json()["new_count"] == 0


def test_route_reviewed_rejects_an_unparseable_as_of(world):
    r = client.post(f"/api/chart/patients/{world['p_a']}/reviewed",
                    json={"as_of": "whenever"},
                    headers=_auth(world["doctor_a"], "doctor"))
    assert r.status_code == 422


def test_patient_role_is_refused_outright(world):
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('TLPATIENT', 'Timeline Pat', 'patient', 'x')")
    hdr = _auth("TLPATIENT", "patient")
    assert client.get(f"/api/chart/patients/{world['p_a']}/timeline",
                      headers=hdr).status_code == 403
    assert client.get("/api/chart/unseen", headers=hdr).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
#  4. The ward-board badge
# ═════════════════════════════════════════════════════════════════════════════

def test_unseen_counts_never_name_a_patient_outside_the_department(world):
    """The badge endpoint is a LIST surface, so it derives scope from
    department_scope inside the query — the shape that four read surfaces were
    missing on 2026-08-05."""
    other_ward = _mk_patient(DEPT_B)
    _note(other_ward, "TL-OTHER-WARD", "2026-08-03T08:00:00Z")
    # doctor_a has reviewed a chart on the other ward at some point in the past
    # (e.g. before a transfer) — the mark exists, but the read must not.
    pts.mark_reviewed(world["doctor_a"], other_ward, as_of="2026-08-01T00:00:00Z")

    r = client.get("/api/chart/unseen", headers=_auth(world["doctor_a"], "doctor"))
    assert r.status_code == 200, r.text
    assert other_ward not in r.json()["counts"]
    assert other_ward not in r.text


def test_unseen_counts_omit_patients_never_reviewed(world):
    fresh = _mk_patient(DEPT_A)
    _note(fresh, "TL-FRESH", "2026-08-03T08:00:00Z")
    r = client.get("/api/chart/unseen", headers=_auth(world["nurse_a"], "nurse"))
    assert r.status_code == 200
    assert fresh not in r.json()["counts"]


def test_unseen_counts_report_the_same_number_as_the_chart(world):
    """The board badge and the rail must not disagree — they are generated from
    one source declaration precisely so they cannot."""
    pid = _mk_patient(DEPT_A)
    uid = world["doctor_a"]
    pts.mark_reviewed(uid, pid, as_of="2026-08-01T00:00:00Z")
    _note(pid, "TL-BOARD-1", "2026-08-02T08:00:00Z")
    _note(pid, "TL-BOARD-2", "2026-08-02T09:00:00Z")

    board = client.get("/api/chart/unseen", headers=_auth(uid, "doctor")).json()["counts"]
    rail = client.get(f"/api/chart/patients/{pid}/timeline",
                      headers=_auth(uid, "doctor")).json()
    assert board.get(pid) == rail["new_count"]


def test_unseen_counts_ignore_discharged_patients(world):
    """The board only draws admitted beds; a discharged chart must not keep a
    badge alive on a bed somebody else is now in."""
    pid = _mk_patient(DEPT_A, status="discharged")
    uid = world["doctor_a"]
    pts.mark_reviewed(uid, pid, as_of="2026-08-01T00:00:00Z")
    _note(pid, "TL-DISCHARGED", "2026-08-02T08:00:00Z")
    counts = client.get("/api/chart/unseen", headers=_auth(uid, "doctor")).json()["counts"]
    assert pid not in counts


def test_unseen_counts_read_is_audited_when_it_discloses(world):
    pid = _mk_patient(DEPT_A)
    uid = world["doctor_a"]
    pts.mark_reviewed(uid, pid, as_of="2026-08-01T00:00:00Z")
    _note(pid, "TL-AUDIT", "2026-08-02T08:00:00Z")

    with db_session() as db:
        before = db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE action_type = 'chart_unseen_counts'"
        ).fetchone()["n"]
    r = client.get("/api/chart/unseen", headers=_auth(uid, "doctor"))
    assert r.status_code == 200 and r.json()["counts"]
    with db_session() as db:
        after = db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE action_type = 'chart_unseen_counts'"
        ).fetchone()["n"]
    assert after == before + 1


# ═════════════════════════════════════════════════════════════════════════════
#  5. Coverage of the streams themselves
# ═════════════════════════════════════════════════════════════════════════════

def test_every_declared_source_is_counted_and_rendered_from_one_declaration():
    """The count query and the detail query are GENERATED from `SOURCES`.

    If they were written twice they would drift, and drift here is the visible
    kind: a stream listed but not counted shows "0 new" above six new rows; a
    stream counted but not listed shows "3 new" above nothing.
    """
    union = pts._count_union_sql("{pcol} = ?")
    for src in pts.SOURCES:
        if not src.countable:
            continue
        assert src.from_sql in union, f"{src.kind} is rendered but never counted"
    # Each countable stream contributes exactly one bound parameter here.
    assert union.count("?") == sum(1 for s in pts.SOURCES if s.countable)


def test_unverified_ai_medication_is_labelled_on_the_rail(world):
    """An AI-extracted, pharmacist-unverified medication must never read on the
    timeline exactly like a charted one — the same rule the discharge
    reconciliation enforces."""
    pid = _mk_patient(DEPT_A)
    with db_session() as db:
        db.execute(
            """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                                        status, prescribed_by, source, started_at)
               VALUES (?, 'Amoxicillin', '500mg', 'Oral', 'TDS', 'active', 'TLADM',
                       'llm_extracted', '2026-08-02T08:00:00Z')""",
            (pid,),
        )
    events = pts.build_chart_timeline(pid, user_id=world["doctor_a"])["events"]
    med = next(e for e in events if e["kind"] == "medication")
    assert "UNVERIFIED" in med["detail"]


def test_critical_alert_does_not_render_the_index_as_a_probability(world):
    """Wave A found the 0-1 risk index shown as a percentage. The band is
    clinical; the index is not a probability of deterioration and is not shown."""
    pid = _mk_patient(DEPT_A)
    with db_session() as db:
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level, drivers, timestamp) "
            "VALUES (?, 0.81, 'critical', '[\"tachycardia\"]', '2026-08-02T08:00:00Z')",
            (pid,),
        )
    events = pts.build_chart_timeline(pid, user_id=world["doctor_a"])["events"]
    alert = next(e for e in events if e["kind"] == "alert")
    assert alert["severity"] == "critical"
    assert "tachycardia" in alert["detail"]
    assert "%" not in alert["title"] and "0.81" not in alert["title"]


def test_stat_order_is_not_rendered_as_routine(world):
    pid = _mk_patient(DEPT_A)
    _order(pid, "TL Blood cultures", "2026-08-02T08:00:00Z", priority="stat")
    events = pts.build_chart_timeline(pid, user_id=world["doctor_a"])["events"]
    order = next(e for e in events if e["kind"] == "order")
    assert order["severity"] == "warn"
    assert "STAT" in order["detail"]


def test_a_new_order_is_one_timeline_entry_not_two(world):
    """create_order writes the orders row AND a creation order_event at the same
    instant. Both used to be listed, so every order showed twice on the chart
    and counted twice in the round's "since" column. One row per placement,
    titled with the status it was placed in; later status changes still list."""
    from services import orders_service

    pid = _mk_patient(DEPT_A)
    uid = world["doctor_a"]
    pts.mark_reviewed(uid, pid, as_of="2026-08-01T00:00:00Z")
    order = orders_service.create_order(pid, "nursing", "TL turn every two hours",
                                        ordered_by="TLADM")

    result = pts.build_chart_timeline(pid, user_id=uid)
    placed = [e for e in result["events"] if e["kind"] in ("order", "order_event")]
    assert len(placed) == 1
    assert placed[0]["kind"] == "order"
    assert placed[0]["title"] == "Order placed (draft): TL turn every two hours"
    assert result["new_count"] == 1

    orders_service.transition_order(order["id"], "active", actor_id="TLADM")
    result = pts.build_chart_timeline(pid, user_id=uid)
    moves = [e for e in result["events"] if e["kind"] == "order_event"]
    assert [e["title"] for e in moves] == ["Order draft → active"]
    assert result["new_count"] == 2


def test_timeline_survives_a_stream_whose_table_is_missing(world, monkeypatch):
    """A minimal or un-migrated deployment must lose ONE stream, not the chart —
    and must lose it from the COUNT as well, or the badge advertises rows the
    rail can never show."""
    broken = pts._Source(
        kind="ghost", from_sql="table_that_does_not_exist g",
        patient_col="g.patient_id", ts_col="g.created_at", id_col="g.id",
    )
    monkeypatch.setattr(pts, "SOURCES", pts.SOURCES + (broken,))

    pid = _mk_patient(DEPT_A)
    uid = world["doctor_a"]
    pts.mark_reviewed(uid, pid, as_of="2026-08-01T00:00:00Z")
    _note(pid, "TL-GHOSTLESS", "2026-08-02T08:00:00Z")

    result = pts.build_chart_timeline(pid, user_id=uid)
    assert result["count"] >= 1
    assert all(e["kind"] != "ghost" for e in result["events"])
    assert result["new_count"] == 1

    counts = pts.unseen_counts_for_scope(uid, DEPT_A)
    assert counts.get(pid) == 1
