"""Sage's facility scope and the out-of-scope locator across facilities
(multisite-10, wave 12, 20 October 2026).

Once a second facilities row exists (isolation is keyed on data), every Sage
lane keeps one facility's patients from another's staff:

  * sage_service._scope_sql and _patient_in_scope take facilities= in lockstep
    with permissions.scope_patient_ids_sql and permissions.patient_in_scope,
    and the two agree on two-facility fixtures (the parity test, extended);
  * a Sage turn's census, discharged list, broad lane, aggregate counts, chart
    assembly and caller-named patient id all carry the caller's facility scope,
    so an F2 account naming an F1 patient attaches no chart;
  * the out-of-scope locator's complement roster is kept to the caller's own
    facilities, so a name found only at F1 builds no locator row and gets the
    same no-matching-patient answer as a name found nowhere (ruling MS-R13,
    built strictest: option (a)); only an account with no department, whose
    census is its facility, is told "no matching patient at <facility>", and a
    ward account keeps the scope-neutral notice, since another department of
    its own facility may hold the bed or name;
  * the department notice still fires inside one facility;
  * the Relay room context, the voice fallback chart and label, and the
    provenance listing and replay carry the facility scope too;
  * with one facility on record every statement and every answer is what it
    was.

The bed matcher (_match_patients, _match_out_of_scope, _name_match, the bed
and ward locator) is not touched: the scope reaches it through the rows it is
given (R026 held). The module owns its database (isolated_module_database),
so the second facility never reaches the session database; the process-wide
facility caches are invalidated around every test. Test patients carry
invented names that hold no word a clinician types.
Run with the test env, never the shipping .env.
"""
from __future__ import annotations

import ast
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import facility_fence as F
import main
import services.sage_service as sage
from auth import create_token
from database import db_session
from permissions import (
    department_scope,
    facility_scope,
    patient_in_scope,
    scope_patient_ids_sql,
)
from services import facility_service as FS
from services.clinical_time import utc_iso_now

pytestmark = pytest.mark.usefixtures("isolated_module_database")

SERVER = Path(__file__).resolve().parents[1]

TAG = uuid.uuid4().hex[:6].upper()
F2 = 9521                       # an id no other module writes
F2_CODE = f"SL{TAG[:4]}"
F2_NAME = "Vexhollow Satellite"
F1_DEPT_A = f"Quorvane Medicine {TAG}"
F1_DEPT_B = f"Quorvane Surgery {TAG}"
F2_DEPT = f"Lekswe Medicine {TAG}"

F1A = {"name": "Zelphine Arkwrightson", "diagnosis": "Vandrelith fever", "bed": "QA-7",
       "drug": "Zorquinate", "dept": F1_DEPT_A, "facility": 1}
F1B = {"name": "Ostravia Pimbleworth", "diagnosis": "Thrennic colitis", "bed": "QB-3",
       "drug": "Plimsorbate", "dept": F1_DEPT_B, "facility": 1}
F2P = {"name": "Brondalia Quillfeather", "diagnosis": "Mervash pneumonia", "bed": "LK-4",
       "drug": "Kelvaxin", "dept": F2_DEPT, "facility": F2}
# A bare bed number held by F1's other department: the out-of-department
# locator reads no bare bed number, so an F1 ward doctor asking about it
# reaches a no-match exit while someone is in that bed at F1.
F1BED = {"name": "Ysolde Brackenmoor", "diagnosis": "Ferrovine nephritis", "bed": "47",
         "drug": "Talquorin", "dept": F1_DEPT_B, "facility": 1}
NOBODY = "Quintavia Morrowgate"     # a name on record nowhere
EMPTY_BED = "83"                     # a bed number nobody holds

ROOM_NOTE = f"Quorvane room note {TAG}"

client = TestClient(main.app, raise_server_exceptions=False)


# ── the world ────────────────────────────────────────────────────────────────

def _add_patient(db, p: dict) -> str:
    pid = f"SFLP{uuid.uuid4().hex[:8].upper()}"
    now = utc_iso_now()
    db.execute(
        "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, diagnosis, status, "
        "admission_date, registered_facility_id) VALUES (?, ?, ?, 58, 'F', ?, ?, ?, "
        "'admitted', ?, ?)",
        (pid, p["name"], f"SFLMRN{uuid.uuid4().hex[:8]}", p["dept"], p["bed"], p["diagnosis"],
         now, p["facility"]))
    db.execute(
        "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status, "
        "facility_id, created_at) VALUES (?, ?, 'system', ?, ?, ?, 'active', ?, ?)",
        (f"SFLA{uuid.uuid4().hex[:8].upper()}", pid, p["dept"], p["dept"], p["bed"],
         p["facility"], now))
    db.execute(
        "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp, "
        "spo2, temperature, respiratory_rate, consciousness, carried_from) "
        "VALUES (?, ?, 121, 94, 57, 90, 38.7, 26, 'alert', NULL)", (pid, utc_iso_now()))
    db.execute(
        "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
        "prescribed_by, source) VALUES (?, ?, '5 mg', 'oral', 'OD', 'active', 'system', "
        "'clinician')", (pid, p["drug"]))
    return pid


def _add_user(db, role: str, dept: str | None, home: int | None = None) -> dict:
    uid = f"SFL{role[:3].upper()}{uuid.uuid4().hex[:6].upper()}"
    db.execute("INSERT INTO users (id, name, role, department, password_hash) "
               "VALUES (?, ?, ?, ?, 'x')", (uid, f"Locator {role.title()}", role, dept))
    if home is not None:
        db.execute("INSERT INTO user_facilities (user_id, facility_id, kind, granted_by, "
                   "granted_at, reason) VALUES (?, ?, 'home', 'system', CURRENT_TIMESTAMP, "
                   "'facility locator test')", (uid, home))
    return {"id": uid, "role": role, "department": dept, "name": f"Locator {role.title()}"}


@pytest.fixture(scope="module")
def world(isolated_module_database):
    with db_session() as db:
        db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                   (F2, str(uuid.uuid4()), F2_CODE, F2_NAME))
        for name, fid in ((F1_DEPT_A, 1), (F1_DEPT_B, 1), (F2_DEPT, F2)):
            db.execute("INSERT INTO departments (name, facility_id, seeded_from) "
                       "VALUES (?, ?, 'test')", (name, fid))
        ids = {"f1a": _add_patient(db, F1A), "f1b": _add_patient(db, F1B),
               "f2": _add_patient(db, F2P), "f1bed": _add_patient(db, F1BED)}
        users = {
            "doc_f1a": _add_user(db, "doctor", F1_DEPT_A),
            "doc_f2": _add_user(db, "doctor", F2_DEPT),
            "admin_f2": _add_user(db, "admin", None, home=F2),
            "admin_f1": _add_user(db, "admin", None, home=1),
            "admin_group": _add_user(db, "admin", None, home=1),
        }
        db.execute("INSERT INTO enterprise_access_grants (user_id, granted_by, granted_at, "
                   "reason) VALUES (?, 'system', CURRENT_TIMESTAMP, 'facility locator test')",
                   (users["admin_group"]["id"],))
        channel = f"case-sfl-{TAG.lower()}"
        db.execute("INSERT INTO channels (id, name, type, patient_id) "
                   "VALUES (?, ?, 'case_room', ?)", (channel, "Locator case room", ids["f1a"]))
        db.execute("INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, "
                   "content, message_type, timestamp, is_bot) "
                   "VALUES (?, 'system', 'Locator Nurse', 'nurse', ?, 'text', ?, 0)",
                   (channel, ROOM_NOTE, utc_iso_now()))
    FS.invalidate_cache()
    assert FS.multi_facility_data() is True
    try:
        yield {"ids": ids, "users": users}
    finally:
        FS.invalidate_cache()


@pytest.fixture(autouse=True)
def _fresh_cache():
    FS.invalidate_cache()
    yield
    FS.invalidate_cache()


@pytest.fixture()
def no_guidelines(monkeypatch):
    """Guideline retrieval answers nothing, so two turns differ only in what
    the scope let through."""
    monkeypatch.setattr(sage, "_get_guideline_context", lambda *a, **k: ("", [], "high"))


@contextmanager
def _one_facility_on_record():
    """The second facility row removed, then put back."""
    with db_session() as db:
        db.execute("DELETE FROM facilities WHERE id = ?", (F2,))
    FS.invalidate_cache()
    try:
        yield
    finally:
        with db_session() as db:
            db.execute("INSERT INTO facilities (id, uuid, code, name) VALUES (?, ?, ?, ?)",
                       (F2, str(uuid.uuid4()), F2_CODE, F2_NAME))
        FS.invalidate_cache()


@pytest.fixture()
def one_facility(world):
    """The second facility row removed for one test, then put back."""
    with _one_facility_on_record():
        yield


def _ctx(query: str, user: dict, patient_id: str | None = None):
    return sage.get_relevant_context(query, user, patient_id, [])


def _no_trace(blob: str, victim: dict) -> list:
    low = blob.lower()
    found = [victim[k] for k in ("name", "diagnosis", "bed", "drug") if victim[k].lower() in low]
    found += [t for t in victim["name"].split() if t.lower() in low]
    return found


def _blob(context, pctx) -> str:
    return (context or "") + json.dumps(pctx or {}, default=str)


def _facility_no_match(name: str) -> str:
    return f"there is no patient matching that at {name}"


# ═══════════════════════════════════════════════════════════════════════════
# 1. Parity with permissions on two-facility fixtures
# ═══════════════════════════════════════════════════════════════════════════

FACILITY_SETS = (None, frozenset(), frozenset({1}), frozenset({F2}), frozenset({1, F2}))
DEPTS = (F1_DEPT_A, F1_DEPT_B, F2_DEPT, None)


def test_the_route_scope_and_the_sage_scope_agree_on_two_facilities(world):
    """The pinned Sage copy and permissions answer alike for every patient,
    department and facility set, the unknown id included."""
    pids = list(world["ids"].values()) + ["NO-SUCH-PATIENT", ""]
    with db_session() as db:
        for pid in pids:
            for dept in DEPTS:
                for fs in FACILITY_SETS:
                    assert (patient_in_scope(db, pid, dept, facilities=fs)
                            == sage._patient_in_scope(db, pid, dept, facilities=fs)), (
                        pid, dept, fs)


def test_the_id_sets_agree_on_two_facilities(world):
    ours = set(world["ids"].values())
    with db_session() as db:
        for dept in DEPTS:
            for fs in FACILITY_SETS:
                sql, params = scope_patient_ids_sql(dept, facilities=fs)
                route = {r[0] for r in db.execute(sql, params).fetchall()} & ours
                sql, params = sage._scope_sql(
                    "SELECT p.id FROM patients p WHERE 1 = 1", dept, (), facilities=fs)
                lane = {r[0] for r in db.execute(sql, params).fetchall()} & ours
                assert route == lane, (dept, fs)


def test_every_account_resolves_to_the_same_answer_in_both_modules(world):
    with db_session() as db:
        for key, user in world["users"].items():
            dept, fs = department_scope(user), facility_scope(user)
            for pid in world["ids"].values():
                assert (patient_in_scope(db, pid, dept, facilities=fs)
                        == sage._patient_in_scope(db, pid, dept, facilities=fs)), (key, pid)


def test_the_accounts_resolve_as_the_design_says(world):
    u = world["users"]
    assert facility_scope(u["doc_f2"]) == frozenset({F2})
    assert facility_scope(u["admin_f2"]) == frozenset({F2})
    assert facility_scope(u["doc_f1a"]) == frozenset({1})
    assert facility_scope(u["admin_group"]) is None


# ═══════════════════════════════════════════════════════════════════════════
# 2. Named retrieval across facilities attaches no chart
# ═══════════════════════════════════════════════════════════════════════════

NAMED = [
    "What is {name}'s condition?",
    "How is {name} doing, what are her vitals?",
    "What medications is {name} on?",
    "Give me {name}'s latest vitals.",
    "Is {name} deteriorating?",
    "Tell me about {name}.",
]


@pytest.mark.parametrize("who", ["doc_f2", "admin_f2"])
@pytest.mark.parametrize("phrasing", NAMED)
def test_an_f2_account_naming_an_f1_patient_gets_no_chart(world, who, phrasing):
    query = phrasing.format(name=F1A["name"])
    context, _scope, _src, _conf, _esc, pctx, qscope = _ctx(query, world["users"][who])
    assert pctx is None, f"{query!r} assembled an F1 chart for {who}"
    leaked = _no_trace(_blob(context, pctx), F1A)
    assert leaked == [], f"{query!r} put F1 chart material in front of {who}: {leaked}"
    # Not the department notice: that would say a record exists.
    assert qscope != "OUT_OF_SCOPE"
    assert sage.OUT_OF_SCOPE_NOTICE not in (context or "")


def test_a_department_less_account_is_told_its_facility(world):
    """The F2 admin's census is all of F2, so 'no patient matching that at F2'
    is true."""
    context, *_rest = _ctx(f"How is {F1A['name']} doing, what are her vitals?",
                           world["users"]["admin_f2"])
    assert context.startswith(sage.NO_MATCHING_PATIENT_NOTICE)
    assert _facility_no_match(F2_NAME) in context
    assert "any other facility" in context


def test_a_ward_account_gets_the_scope_neutral_notice(world):
    """A ward doctor's census is its department's, not the facility's, so the
    answer says nothing about the facility (the R026 wording, unchanged)."""
    context, *_rest = _ctx(f"How is {F1A['name']} doing, what are her vitals?",
                           world["users"]["doc_f2"])
    assert context.startswith(sage.NO_MATCHING_PATIENT_NOTICE)
    assert F2_NAME not in context
    assert "census available to you is" not in context
    assert "facility" not in context.lower()


def test_the_notice_names_a_facility_only_for_a_department_less_account(world):
    plain = sage.NO_MATCHING_PATIENT_NOTICE
    assert sage._no_matching_patient_notice(None, None) == plain
    assert sage._no_matching_patient_notice(F1_DEPT_A, None) == plain
    assert sage._no_matching_patient_notice(F1_DEPT_A, frozenset({1})) == plain
    assert sage._no_matching_patient_notice(F2_DEPT, frozenset({F2})) == plain
    named = sage._no_matching_patient_notice(None, frozenset({F2}))
    assert named.startswith(plain) and _facility_no_match(F2_NAME) in named


BED_QUESTIONS = [
    "bed {bed} lactate?",
    "How is the patient in bed {bed} doing?",
]


@pytest.mark.parametrize("phrasing", BED_QUESTIONS)
def test_a_bed_held_by_another_department_of_the_same_facility_claims_nothing(
        world, phrasing, no_guidelines):
    """Someone is in bed 47 at F1, in the F1 ward doctor's other department.
    The turn must not tell the doctor there is no such patient at F1: it reads
    exactly as it does with one facility on record."""
    doc = world["users"]["doc_f1a"]
    query = phrasing.format(bed=F1BED["bed"])
    two = _ctx(query, doc)
    context, *_r, pctx, _q = two
    assert pctx is None
    assert context.startswith(sage.NO_MATCHING_PATIENT_NOTICE)
    assert "census available to you is" not in context
    assert "facility" not in context.lower()
    assert _no_trace(_blob(context, pctx), F1BED) == []
    with _one_facility_on_record():
        one = _ctx(query, doc)
    assert two == one, f"{query!r}: two facilities changed a ward doctor's answer"


@pytest.mark.parametrize("who", ["doc_f2", "admin_f2"])
@pytest.mark.parametrize("phrasing", BED_QUESTIONS)
def test_a_bed_at_another_facility_reads_exactly_like_a_bed_held_nowhere(
        world, who, phrasing, no_guidelines):
    """MS-R13 (a) for beds: bed 47 is held at F1, bed 83 by nobody."""
    user = world["users"][who]
    there = _ctx(phrasing.format(bed=F1BED["bed"]), user)
    nowhere = _ctx(phrasing.format(bed=EMPTY_BED), user)
    assert there[5] is None
    assert there[0].startswith(sage.NO_MATCHING_PATIENT_NOTICE)
    # The facility is named only to the account whose census is the facility.
    assert (_facility_no_match(F2_NAME) in there[0]) is (who == "admin_f2")
    assert _no_trace(_blob(there[0], there[5]), F1BED) == []
    assert there == nowhere, f"{phrasing!r}: a bed at F1 and an empty bed answer differently"


@pytest.mark.parametrize("who", ["doc_f2", "admin_f2"])
@pytest.mark.parametrize("phrasing", NAMED)
def test_a_name_at_another_facility_reads_exactly_like_a_name_found_nowhere(
        world, who, phrasing, no_guidelines):
    """MS-R13 (a): true, and discloses nothing. An F2 account cannot tell an F1
    patient from nobody at all."""
    user = world["users"][who]
    there = _ctx(phrasing.format(name=F1A["name"]), user)
    nowhere = _ctx(phrasing.format(name=NOBODY), user)
    assert there == nowhere, f"{phrasing!r}: an F1 name and no name answer differently"


def test_the_locator_builds_no_row_from_another_facility(world, monkeypatch):
    """The complement roster an F2 ward doctor's turn reads holds no F1 patient,
    and no denial is recorded against the F1 patient."""
    seen: list = []
    real = sage._out_of_scope_locators

    def spy(db, dept, in_scope_ids):
        rows = real(db, dept, in_scope_ids)
        seen.append((dept, getattr(in_scope_ids, "facilities", "plain"), [r["id"] for r in rows]))
        return rows

    monkeypatch.setattr(sage, "_out_of_scope_locators", spy)
    doc = world["users"]["doc_f2"]
    _ctx(f"How is {F1A['name']} doing, what are her vitals?", doc)
    assert seen, "the out-of-department check never ran"
    f1 = {world["ids"]["f1a"], world["ids"]["f1b"]}
    for dept, carried, ids in seen:
        assert dept == F2_DEPT
        assert carried == frozenset({F2})
        assert not set(ids) & f1, "the locator read an F1 patient for an F2 doctor"
    with db_session() as db:
        rows = db.execute(
            "SELECT 1 FROM audit_logs WHERE action_type = 'sage_out_of_scope_patient_denied' "
            "AND actor_user_id = ?", (doc["id"],)).fetchall()
    assert rows == []


def test_ids_that_carry_no_scope_get_no_roster_while_two_facilities_exist(world):
    with db_session() as db:
        assert sage._out_of_scope_locators(db, F2_DEPT, set()) == []
        scoped = sage._out_of_scope_locators(db, F1_DEPT_A, sage._ScopedIds((), frozenset({1})))
        assert {r["id"] for r in scoped} >= {world["ids"]["f1b"]}
        assert world["ids"]["f2"] not in {r["id"] for r in scoped}
        assert sage._out_of_scope_locators(db, F1_DEPT_A, sage._ScopedIds((), frozenset())) == []


def test_inside_one_facility_the_department_notice_still_fires(world):
    doc = world["users"]["doc_f1a"]
    context, _s, _src, _c, _e, pctx, qscope = _ctx(f"What is {F1B['name']}'s condition?", doc)
    assert qscope == "OUT_OF_SCOPE"
    assert context == sage.OUT_OF_SCOPE_NOTICE
    assert pctx is None
    with db_session() as db:
        row = db.execute(
            "SELECT metadata_json FROM audit_logs WHERE action_type = "
            "'sage_out_of_scope_patient_denied' AND actor_user_id = ? ORDER BY id DESC LIMIT 1",
            (doc["id"],)).fetchone()
    assert row is not None
    meta = json.loads(row[0])
    assert meta["facility_scope"] == [1]
    assert meta["caller_department"] == F1_DEPT_A


def test_the_own_facility_patient_still_answers(world):
    context, _s, _src, _c, _e, pctx, _q = _ctx(f"What is {F2P['name']}'s condition?",
                                               world["users"]["doc_f2"])
    assert pctx is not None and pctx["patient_id"] == world["ids"]["f2"]


def test_the_group_account_reads_both_facilities(world):
    admin = world["users"]["admin_group"]
    for p, key in ((F1A, "f1a"), (F2P, "f2")):
        _c, _s, _src, _conf, _e, pctx, _q = _ctx(f"What is {p['name']}'s condition?", admin)
        assert pctx is not None and pctx["patient_id"] == world["ids"][key]


# ═══════════════════════════════════════════════════════════════════════════
# 3. A caller-named patient id from another facility
# ═══════════════════════════════════════════════════════════════════════════

def test_a_ward_doctor_sending_an_f1_patient_id_gets_the_department_notice(world):
    doc = world["users"]["doc_f2"]
    context, _s, _src, _c, _e, pctx, qscope = _ctx("What are her vitals?", doc,
                                                   world["ids"]["f1a"])
    assert qscope == "OUT_OF_SCOPE" and pctx is None
    assert context == sage.OUT_OF_SCOPE_NOTICE
    unknown = _ctx("What are her vitals?", doc, "NO-SUCH-PATIENT")
    assert unknown[0] == context, "an unknown id and an F1 id must read alike"


def test_a_facility_bound_admin_sending_an_f1_patient_id_gets_the_facility_words(world):
    admin = world["users"]["admin_f2"]
    context, _s, _src, _c, _e, pctx, qscope = _ctx("What are her vitals?", admin,
                                                   world["ids"]["f1a"])
    assert qscope == "OUT_OF_SCOPE" and pctx is None
    assert context == sage.FACILITY_OUT_OF_SCOPE_NOTICE.format(facility=F2_NAME)
    assert _no_trace(context, F1A) == []
    unknown = _ctx("What are her vitals?", admin, "NO-SUCH-PATIENT")
    assert unknown[0] == context
    with db_session() as db:
        row = db.execute(
            "SELECT metadata_json, patient_id FROM audit_logs WHERE action_type = "
            "'sage_out_of_scope_patient_denied' AND actor_user_id = ? AND patient_id = ? "
            "ORDER BY id DESC LIMIT 1", (admin["id"], world["ids"]["f1a"])).fetchone()
    assert row is not None, "the reach was not recorded"
    assert json.loads(row[0])["facility_scope"] == [F2]


# ═══════════════════════════════════════════════════════════════════════════
# 4. Census, aggregate counts, Relay, voice
# ═══════════════════════════════════════════════════════════════════════════

def test_the_census_of_a_facility_bound_admin_is_its_facility(world):
    context, scope, *_rest = _ctx("Give me a census overview of all patients",
                                  world["users"]["admin_f2"])
    assert scope == "broad"
    assert F2P["name"] in context
    assert _no_trace(context, F1A) == [] and _no_trace(context, F1B) == []
    assert f"{F2_NAME.upper()} CENSUS" in context


def test_the_aggregate_counts_only_the_callers_facility(world):
    text = sage.build_aggregate_context("how many admitted", facilities=frozenset({F2}))
    assert "Total admitted: 1." in text
    assert f"AGGREGATE STATISTICS FOR {F2_NAME.upper()}" in text
    nothing = sage.build_aggregate_context("how many admitted", facilities=frozenset())
    assert "Total admitted: 0." in nothing
    whole = sage.build_aggregate_context("how many admitted")
    assert whole.startswith("HOSPITAL AGGREGATE STATISTICS")
    context, scope, *_r = _ctx("how many patients are currently admitted",
                               world["users"]["admin_f2"])
    assert scope == "aggregate" and "Total admitted: 1." in context


def test_the_relay_room_context_keeps_to_the_facility(world):
    from services.sage_relay_context import fetch_room_messages

    with db_session() as db:
        assert fetch_room_messages(db, world["ids"]["f1a"], None, world["users"]["admin_f2"]) == []
        assert fetch_room_messages(db, world["ids"]["f1a"], F2_DEPT,
                                   world["users"]["doc_f2"]) == []
        own = fetch_room_messages(db, world["ids"]["f1a"], F1_DEPT_A, world["users"]["doc_f1a"])
        assert [m["content"] for m in own] == [ROOM_NOTE]
        group = fetch_room_messages(db, world["ids"]["f1a"], None, world["users"]["admin_group"])
        assert [m["content"] for m in group] == [ROOM_NOTE]


def test_an_unreadable_facility_scope_reads_no_room(world, monkeypatch):
    import services.sage_relay_context as relay_ctx
    from services.facility_service import FacilityScopeError

    def boom(user):
        raise FacilityScopeError("maps unreadable")

    monkeypatch.setattr(relay_ctx, "facility_scope", boom)
    with db_session() as db:
        assert relay_ctx.fetch_room_messages(
            db, world["ids"]["f1a"], F1_DEPT_A, world["users"]["doc_f1a"]) == []


def test_the_voice_fallback_chart_and_label_keep_to_the_facility(world):
    from services.voice_agent_service import _facility_refusal_words, build_patient_context

    u, ids = world["users"], world["ids"]
    refused = build_patient_context(ids["f1a"], u["doc_f2"])
    assert refused.startswith("That patient is not in your department")
    assert _no_trace(refused, F1A) == []
    words = build_patient_context(ids["f1a"], u["admin_f2"])
    assert words.startswith(f"No patient matching that at {F2_NAME}")
    assert _no_trace(words, F1A) == []
    assert F2P["name"] in build_patient_context(ids["f2"], u["admin_f2"])
    assert sage.answer_label_for_patient(ids["f1a"], u["admin_f2"]) is None
    assert F2P["name"] in (sage.answer_label_for_patient(ids["f2"], u["admin_f2"]) or "")
    assert _facility_refusal_words(u["admin_f2"]) == F2_NAME
    assert _facility_refusal_words(u["doc_f2"]) is None
    assert _facility_refusal_words(u["admin_group"]) is None


def test_the_voice_out_of_scope_prompt_names_the_facility(world):
    from services.voice_agent_service import VoiceAgent

    agent = VoiceAgent.__new__(VoiceAgent)
    agent.hospital_name = "Locator Hospital"
    prompt = agent._build_system_prompt("What are her vitals?", world["users"]["admin_f2"],
                                        world["ids"]["f1a"])
    assert f"no patient matching that at {F2_NAME}" in prompt
    assert _no_trace(prompt, F1A) == []
    ward = agent._build_system_prompt("What are her vitals?", world["users"]["doc_f2"],
                                      world["ids"]["f1a"])
    assert "NOT in their department" in ward


# ═══════════════════════════════════════════════════════════════════════════
# 5. Provenance listing and replay
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def turns(world):
    from services import sage_provenance_service as prov

    u, ids = world["users"], world["ids"]
    made = {
        "f1": prov.record_turn(user=u["doc_f1a"], question="q", patient_id=ids["f1a"],
                               answer="a"),
        "f2": prov.record_turn(user=u["doc_f2"], question="q", patient_id=ids["f2"], answer="a"),
        "f2_patientless": prov.record_turn(user=u["admin_f2"], question="q", answer="a"),
        "group_patientless": prov.record_turn(user=u["admin_group"], question="q", answer="a"),
    }
    assert all(made.values()), made
    return made


def _auth(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_token(user['id'], user['role'])}"}


def test_the_provenance_listing_keeps_to_the_readers_facility(world, turns, monkeypatch):
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    resp = client.get("/api/sage/provenance?limit=500", headers=_auth(world["users"]["admin_f2"]))
    assert resp.status_code == 200, resp.text
    listed = {t["turn_id"] for t in resp.json()["turns"]}
    assert turns["f2"] in listed
    assert turns["f1"] not in listed
    assert turns["f2_patientless"] not in listed and turns["group_patientless"] not in listed
    assert resp.json()["scope"]["hospital_wide"] is False
    group = client.get("/api/sage/provenance?limit=500",
                       headers=_auth(world["users"]["admin_group"]))
    assert group.status_code == 200
    assert {turns["f1"], turns["f2"], turns["group_patientless"]} <= {
        t["turn_id"] for t in group.json()["turns"]}
    assert group.json()["scope"]["hospital_wide"] is True


def test_the_provenance_replay_keeps_to_the_readers_facility(world, turns, monkeypatch):
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())
    hdr = _auth(world["users"]["admin_f2"])
    assert client.get(f"/api/sage/provenance/{turns['f1']}", headers=hdr).status_code == 404
    assert client.get(f"/api/sage/provenance/{turns['f2_patientless']}",
                      headers=hdr).status_code == 404
    assert client.get(f"/api/sage/provenance/{turns['f2']}", headers=hdr).status_code == 200
    grp = _auth(world["users"]["admin_group"])
    assert client.get(f"/api/sage/provenance/{turns['f1']}", headers=grp).status_code == 200


def test_a_facility_bound_turn_is_not_recorded_hospital_wide(world, turns):
    with db_session() as db:
        flags = dict(db.execute(
            "SELECT turn_id, hospital_wide FROM sage_provenance_turns WHERE turn_id IN (?, ?)",
            (turns["f2_patientless"], turns["group_patientless"])).fetchall())
    assert flags[turns["f2_patientless"]] == 0
    assert flags[turns["group_patientless"]] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 6. The HTTP lane, with the fence opened and the model echoing its context
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def model_echoes_context(monkeypatch):
    import routes.sage_routes as sage_routes

    async def echo(prompt, context="", conversation_history=None, scope="simple", **kwargs):
        info = kwargs.get("route_info")
        if info is not None:
            info.update(lane="local", provider="ollama", model="locator-echo")
        yield "NOACT|Context I was given: " + " ".join(str(context or "").split())[:8000]

    monkeypatch.setattr(sage_routes, "query_ollama", echo)
    monkeypatch.setattr(sage_routes, "_save_turn", lambda *a, **k: None)
    monkeypatch.setattr(F, "FACILITY_BLIND_SURFACES", ())


@pytest.mark.parametrize("who", ["doc_f2", "admin_f2"])
@pytest.mark.parametrize("body_kind", ["by_name", "by_id"])
def test_the_query_route_gives_an_f2_account_nothing_of_the_f1_patient(
        world, who, body_kind, model_echoes_context):
    body = ({"query": f"What is the latest NEWS2 for {F1A['name']}?"} if body_kind == "by_name"
            else {"query": "Summarise this patient", "patient_id": world["ids"]["f1a"]})
    resp = client.post("/api/sage/query", json=body, headers=_auth(world["users"][who]))
    assert resp.status_code == 200, resp.text
    text = resp.content.decode("utf-8", "ignore")
    sent = json.dumps(body).lower()
    leaked = [x for x in _no_trace(text, F1A) if x.lower() not in sent]
    assert leaked == [], f"{who} {body_kind}: {leaked}"


# ═══════════════════════════════════════════════════════════════════════════
# 7. One facility on record: nothing changes
# ═══════════════════════════════════════════════════════════════════════════

def test_with_one_facility_every_statement_and_answer_is_unchanged(world, one_facility):
    assert FS.multi_facility_data() is False
    for user in world["users"].values():
        assert facility_scope(user) is None
    base = "SELECT 1 FROM patients p WHERE p.id = ?"
    assert sage._scope_sql(base, F1_DEPT_A, ("x",)) == (
        f"{base} AND {sage._DEPARTMENT_SCOPE_SQL}", ("x", F1_DEPT_A))
    assert sage._scope_sql(base, None, ("x",)) == (base, ("x",))
    assert sage._no_matching_patient_notice(None, None) == sage.NO_MATCHING_PATIENT_NOTICE
    assert sage._out_of_scope_notice(None, None) == sage.OUT_OF_SCOPE_NOTICE
    assert sage._facility_audit_metadata(world["users"]["admin_f2"]) == {}
    with db_session() as db:
        plain = {r["id"] for r in sage._out_of_scope_locators(db, F2_DEPT, set())}
    assert world["ids"]["f1a"] in plain, "a plain id set lost its roster with one facility"
    # The F2 ward doctor's department is now just another department: naming
    # an F1A patient is the ordinary cross-department reach, as before.
    context, *_r, qscope = _ctx(f"What is {F1A['name']}'s condition?", world["users"]["doc_f2"])
    assert qscope == "OUT_OF_SCOPE" and context == sage.OUT_OF_SCOPE_NOTICE
    # A department-less admin reads hospital-wide again.
    _c, _s, _src, _conf, _e, pctx, _q = _ctx(f"What is {F1A['name']}'s condition?",
                                             world["users"]["admin_f2"])
    assert pctx is not None and pctx["patient_id"] == world["ids"]["f1a"]


# ═══════════════════════════════════════════════════════════════════════════
# 8. The ratchet: every Sage scope call passes the facility scope
# ═══════════════════════════════════════════════════════════════════════════

#: (module, functions whose calls are checked). The system lanes that pass
#: dept None on purpose (nervous_system_triggers._compile_context,
#: sage_assessment_service, surgery_service) are outside this list.
CHECKED = {
    "services/sage_service.py": {"_get_relevant_context", "answer_label_for_patient",
                                 "_assemble_patient_context", "_patient_in_scope"},
    "services/sage_relay_context.py": {"fetch_room_messages"},
    "services/voice_agent_service.py": {"build_patient_context"},
    "routes/sage_routes.py": {"_prepare_context"},
}
SCOPE_CALLS = {"_scope_sql", "_patient_in_scope", "_assemble_patient_context",
               "build_aggregate_context"}


def _calls_in(path: str, names: set) -> list:
    tree = ast.parse((SERVER / path).read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    fn = call.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                    if name in SCOPE_CALLS:
                        out.append((node.name, call))
    return out


def test_every_scope_call_in_the_sage_lanes_passes_the_facility_scope():
    checked = 0
    for path, names in CHECKED.items():
        for owner, call in _calls_in(path, names):
            checked += 1
            kws = {k.arg for k in call.keywords}
            assert "facilities" in kws or None in kws, (
                f"{path} {owner} line {call.lineno}: a scope call without facilities=")
    assert checked >= 20


def test_the_out_of_department_check_hands_the_matcher_scoped_ids():
    src = (SERVER / "services/sage_service.py").read_text()
    assert re.search(r"_in_scope_ids = _ScopedIds\(", src)
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_get_relevant_context")
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
             and getattr(c.func, "id", None) == "_match_out_of_scope"]
    assert calls and all(isinstance(c.args[2], ast.Name) and c.args[2].id == "_in_scope_ids"
                         for c in calls)
