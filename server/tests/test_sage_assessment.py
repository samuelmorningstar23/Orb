"""Orb — SAGE'S DRAFTED ASSESSMENT: the middle of the wedge.

Vigil detects, Sage reasons, Relay puts a drafted action in front of a
clinician. Before ``services/sage_assessment_service.py`` the middle was one
sentence of 4B prose glued onto the alert text on the simulator lane only, with
nothing to accept or reject. This file holds the properties that make the
replacement safe to put in front of real patients.

FOUR PROPERTIES, and every one of them is a safety property rather than a
feature:

  1. THE ALERT NEVER WAITS. Vitals-in to clinician-visible alert is 51 ms and
     that number is the product. The assessment is generated AFTER the alert is
     already on screen. ``test_the_alert_does_not_wait_for_the_assessment``
     proves it by blocking generation indefinitely and requiring the alert path
     to complete anyway.
  2. NOTHING EXECUTES. Every action the draft proposes is a `sage_actions` card
     in state 'suggested'. No order, no handoff, no medication row exists until
     a clinician explicitly calls the confirm route.
  3. FAILURE IS HONEST. Shed, timeout, eviction, unparseable output — each
     produces `unavailable` with a NAMED reason and a ward-facing sentence, and
     never a fabricated assessment or a row that looks pending forever.
  4. ORB'S ARITHMETIC WINS. NEWS2 in the prompt is the deterministic engine's,
     and a model that states a different one has its number replaced.

WHAT IS ASSERTED is always a row in a table or a value on the payload the UI
renders — never a log line. The shadow instrument swallows its own exceptions by
design, so a test that trusts a return value can happily measure nothing.
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrations  # noqa: E402
from database import db_session  # noqa: E402
from services import sage_assessment_service as sas  # noqa: E402
from services import shadow_service  # noqa: E402
from services.vigil_service import compute_news2  # noqa: E402

DEPARTMENT = "Assessment Medicine"

# NEWS2 19 — the dry run's peri-arrest picture: RR 32 (3), SpO2 86 (3), on O2
# (2), Temp 39.4 (2), SBP 78 (3), HR 138 (3), ACVPU voice (3).
PERI_ARREST = {
    "heart_rate": 138, "systolic_bp": 78, "diastolic_bp": 44,
    "respiratory_rate": 32, "temperature": 39.4, "spo2": 86,
    "consciousness": "voice", "on_oxygen": True,
}

#: A well-formed model response. One cardable medication step, one advice step,
#: no citations. Used wherever the CONTENT is not what is under test.
GOOD_OUTPUT = {
    "whats_happening": (
        "Respiratory rate 32 and SpO2 86% on oxygen are the dominant NEWS2 "
        "contributors, with hypotension at 78 systolic and a rising heart rate "
        "across the last four sets. The picture is consistent with worsening "
        "sepsis and the trajectory over the last hour is downward."),
    "considerations": [
        {"step": "Start IV ceftriaxone for suspected sepsis", "why": "Fever with hypotension",
         "orb_can_act": True, "action_type": "start_medication", "target": "Ceftriaxone"},
        {"step": "Escalate to the medical registrar now", "why": "NEWS2 in the critical band",
         "orb_can_act": True, "action_type": "escalate", "target": "Registrar"},
    ],
    "cited_sources": [],
    "watch_items": [
        "Whether the patient is protecting their airway",
        "Whether there is a source of infection visible at the bedside",
    ],
}

def _brief(steps, cite=None) -> str:
    """Render steps in the FAST LANE's wire format: one line, four fields.

    The alert lane does not ask for JSON. Braces, quotes and repeated key names
    are output tokens, output tokens are seconds at 10-15 tok/s, and a
    line-delimited format can also be surfaced one complete step at a time
    while the next is still decoding. `-` is how the model says "no value".
    """
    lines = [" | ".join([step, why or "-", action or "-", target or "-"])
             for step, why, action, target in steps]
    if cite:
        lines.append("CITE: " + ", ".join(cite))
    return "\n".join(lines)


#: The fast lane's equivalent of GOOD_OUTPUT: one cardable medication step, one
#: advice step, no citations.
#:
#: EVERY LINE HERE NAMES A FACT FROM `_mk_patient`'s CHART, and that is a
#: requirement rather than a style. Since 2026-08-15 Tier 2 says only what
#: depends on this patient's own chart, and the post-filter enforces it three
#: ways: a line matching a generic phrase is dropped, a line naming no charted
#: fact is dropped as unsourced, and a line naming a drug or a number the
#: pre-screen did not supply is dropped as fabricated.
#:
#: These steps used to read "Start IV ceftriaxone for suspected sepsis" and
#: "Escalate to the medical registrar now". BOTH ARE NOW DELETED BY DESIGN — the
#: second is verbatim the padding that made the old tier decoration, and the
#: first names a drug the screen never supplied. They are kept as the negative
#: fixtures in `test_a_line_true_of_any_patient_never_reaches_the_clinician` and
#: `test_a_fabricated_drug_is_deleted_and_counted` (tests/test_assessment_tiers)
#: rather than being deleted, so the strings a clinician actually saw stay
#: pinned somewhere.
#:
#: The medication step still carries `start_medication | Ceftriaxone`, because
#: the drug-safety gate applying to an assessment-proposed drug is an invariant
#: this file owns. The STEP TEXT names the allergy rather than the drug: only
#: `step` and `why` go through the fabrication guard, so a proposal whose target
#: the screen never supplied is still screened, carded and blocked exactly as
#: before.
BRIEF_STEPS = [
    ("Sulfonamide allergy is charted — the sulfa-based first-line option is ruled "
     "out for this patient", "documented sulfa allergy with an infective picture",
     "start_medication", "Ceftriaxone"),
    ("Warfarin on the chart with a systolic of 78 — occult bleeding belongs in the "
     "differential", "anticoagulated and hypotensive", None, None),
]
BRIEF_OUTPUT = _brief(BRIEF_STEPS)


#: Two retrieved guideline passages, in the shape `_get_guideline_context`
#: returns. `content` is what `assess_answer_grounding` scores against.
SOURCES = [
    {"document_id": "LOC-SEPSIS-001", "title": "Sepsis Six — local policy",
     "source": "Hospital policy", "trust_tier": 1,
     "content": "Administer ceftriaxone within one hour of recognising sepsis. "
                "Measure serum lactate and take blood cultures before antibiotics."},
    {"document_id": "EXT-RESP-004", "title": "Acute oxygen therapy",
     "source": "External guideline", "trust_tier": 3,
     "content": "Target saturations of 94-98% unless the patient is at risk of "
                "hypercapnic respiratory failure, in which case target 88-92%."},
]


# Keep the missing-machine-principal scenario independent of system-authored
# rows left by other modules. Real foreign keys remain enabled throughout.
pytestmark = pytest.mark.usefixtures("isolated_module_database")

# ═════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _fresh_instrument(monkeypatch):
    """A clean shadow table per test, an empty episode registry, and a clean
    in-flight set.

    The episode registry is PROCESS-LOCAL and survives a table drop, so without
    the reset a test could resolve an episode id minted by an earlier one and
    every "landed on the alert's episode" assertion would be measuring the wrong
    test.
    """
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "true")
    monkeypatch.setenv("ORB_SAGE_ASSESSMENT_ENABLED", "true")
    shadow_service.reset_registry()
    _rebuild_shadow_table()
    sas._inflight.clear()
    yield
    sas._inflight.clear()
    shadow_service.reset_registry()
    _rebuild_shadow_table()
    _cleanup_patients()


#: Every patient this file admits. Cleaned up after each test — see below.
_CREATED: list[str] = []


def _cleanup_patients() -> None:
    """Discharge and delete this file's fixture patients.

    NOT tidiness. These patients are `status='admitted'` with a deliberately
    worsening vitals series, and the suite shares ONE database across every
    file. `world_model_service.compute_ward_state` scans every admitted patient
    and returns `worsening[:10]` — so leaving ~45 synthetic deteriorating
    patients behind pushed test_world_model's own patient out of that top-ten
    list and failed a test in a module this change never touched. A fixture that
    leaks into a hospital-wide aggregate is a fixture that breaks other people's
    tests at a distance.

    Children before parents. `audit_logs` is deliberately NOT touched: it is an
    append-only hash chain and deleting from it would break
    verify_audit_chain() for the whole session.
    """
    if not _CREATED:
        return
    ids = list(_CREATED)
    _CREATED.clear()
    marks = ",".join("?" * len(ids))
    with db_session() as db:
        db.execute(f"DELETE FROM sage_assessment_actions WHERE assessment_id IN "
                   f"(SELECT id FROM sage_assessments WHERE patient_id IN ({marks}))", ids)
        db.execute(f"DELETE FROM sage_assessment_events WHERE assessment_id IN "
                   f"(SELECT id FROM sage_assessments WHERE patient_id IN ({marks}))", ids)
        db.execute(f"DELETE FROM sage_assessments WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM sage_action_events WHERE action_id IN "
                   f"(SELECT id FROM sage_actions WHERE patient_id IN ({marks}))", ids)
        db.execute(f"DELETE FROM sage_actions WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM messages WHERE channel_id IN "
                   f"(SELECT id FROM channels WHERE patient_id IN ({marks}))", ids)
        db.execute(f"DELETE FROM channels WHERE patient_id IN ({marks})", ids)
        # `medications` and `patient_allergies` are the pre-screen's inputs —
        # `_mk_patient` charts both so Tier 2 has something patient-specific to
        # say. They are FK children of `patients` like the rest, so leaving them
        # out here does not leak quietly: it fails the DELETE below outright.
        for table in ("vitals", "risk_scores", "admissions", "medications",
                      "patient_allergies", "world_model_state"):
            try:
                col = "entity_id" if table == "world_model_state" else "patient_id"
                db.execute(f"DELETE FROM {table} WHERE {col} IN ({marks})", ids)
            except Exception:
                pass
        db.execute(f"DELETE FROM patients WHERE id IN ({marks})", ids)


def _rebuild_shadow_table() -> None:
    with db_session() as conn:
        conn.execute("DROP VIEW IF EXISTS shadow_events_current")
        conn.execute("DROP TABLE IF EXISTS shadow_events")
        migrations._m4_shadow_events(conn)
        migrations._m18_shadow_none_confirmed_once(conn)


def _mk_patient(*, case_room=True, observations=4) -> str:
    """An admitted, deteriorating patient with a case room and a vitals series.

    The department lives on the ADMISSION — `patients.ward` is a bed label and
    is not what scoping reads.
    """
    pid = f"ASSESSP{uuid.uuid4().hex[:8].upper()}"
    _CREATED.append(pid)
    now = datetime.now(timezone.utc)
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('ASSESSADM', 'Assessment Admitter', 'doctor', 'x')")
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'A-1', 'AS-1', 71, 'F', 'Community acquired pneumonia',
                       'admitted', ?)""",
            (pid, f"Assessment {pid}", f"MRN-{uuid.uuid4().hex[:8].upper()}",
             now.isoformat()),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'AS-1', 'A-1', 'active', 'ASSESSADM')",
            (pid, DEPARTMENT),
        )
        # Oldest first, worsening — so "the trend across recent observations" is
        # a real trend and not four copies of one snapshot.
        for i in range(observations):
            drift = (observations - 1 - i)
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                       diastolic_bp, spo2, temperature, respiratory_rate,
                                       consciousness, on_oxygen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, (now - timedelta(minutes=5 * drift)).isoformat(),
                 PERI_ARREST["heart_rate"] - 4 * drift,
                 PERI_ARREST["systolic_bp"] + 6 * drift,
                 PERI_ARREST["diastolic_bp"],
                 PERI_ARREST["spo2"] + 2 * drift,
                 PERI_ARREST["temperature"],
                 PERI_ARREST["respiratory_rate"] - 2 * drift,
                 PERI_ARREST["consciousness"], 1),
            )
        # ── THE CHART FACTS THAT MAKE THIS A REAL TIER-2 GENERATION ──────────
        # Tier 2 stopped producing generic next steps on 2026-08-15: it now says
        # only what depends on THIS patient's own chart, and if the deterministic
        # pre-screen (`patient_specific_screen.screen`) finds nothing it answers
        # "no patient-specific factors identified" WITHOUT CALLING A MODEL AT
        # ALL. That is the design and it is tested in its own file.
        #
        # It is also a trap for THIS one. Every test below that is about
        # something else — the safety gate, the cards, the episode, the HTTP
        # contract, the lane — needs a generation to have happened at all, and a
        # chartless patient silently gives them a zero-step draft that passes
        # nothing and fails confusingly. So this fixture charts exactly two
        # facts, each tripping one pre-screen rule against the peri-arrest
        # picture above:
        #
        #   WARFARIN + SBP 78  -> `anticoagulated_in_this_picture`
        #   SULFA ALLERGY + T 39.4 -> `antimicrobial_allergy_in_an_infective_picture`
        #
        # SULFONAMIDES RATHER THAN PENICILLIN IS DELIBERATE. The allergy has to
        # be one `classify_allergen` puts in an antimicrobial class (or the rule
        # stays silent), but it must NOT be one that makes the real allergy
        # interlock block Ceftriaxone — the drug these tests propose to prove the
        # medication gate still runs. A penicillin allergy would do both, and the
        # card tests would then be measuring a block instead of a gate.
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, "
            "frequency, status, prescribed_by) VALUES (?, 'Warfarin', '5 mg', "
            "'oral', 'OD', 'active', 'ASSESSADM')", (pid,))
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, "
            "severity, status, recorded_by) VALUES (?, 'Sulfonamides', 'Rash', "
            "'severe', 'active', 'ASSESSADM')", (pid,))
        if case_room:
            db.execute(
                "INSERT OR REPLACE INTO channels (id, name, type, patient_id) "
                "VALUES (?, ?, 'case_room', ?)",
                (f"assess-room-{pid}", f"Assessment Case Room {pid}", pid),
            )
    return pid


def _room(pid: str) -> str:
    return f"assess-room-{pid}"


@pytest.fixture(autouse=True)
def _formulary():
    """A one-drug formulary.

    The reconciler is REAL in these tests, on purpose: a `start_medication` card
    is only offered for something Orb can actually order, and that check runs
    against `helix_medication_master`. `init_db` does not create the Helix
    tables (main.py's lifespan calls `init_helix_db`), so the suite builds them
    itself rather than patching the reconciler out and losing the property.
    """
    from services.helix_db import HELIX_SCHEMA
    with db_session() as db:
        db.executescript(HELIX_SCHEMA)
        db.execute(
            "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, "
            "form, strength) VALUES ('MED-CEF', 'Ceftriaxone', 'Ceftriaxone sodium', "
            "'Injection', '1 g')")
    yield


def _news2(vitals: dict) -> dict:
    return compute_news2(vitals["heart_rate"], vitals["systolic_bp"],
                         vitals["respiratory_rate"], vitals["temperature"],
                         vitals["spo2"], vitals["consciousness"], vitals["on_oxygen"])


# ── the fake model lane ──────────────────────────────────────────────────────
class _FakeProvider:
    """A stand-in for `llm_providers.OllamaChatProvider`.

    Records the messages it was sent (so the prompt itself can be asserted on)
    and can be gated, so a test can hold generation open while it checks what the
    rest of the system did without it.
    """

    def __init__(self, output, *, gate: asyncio.Event | None = None,
                 on_call=None, raises: BaseException | None = None):
        self._output = output
        self._gate = gate
        self._on_call = on_call
        self._raises = raises
        self.messages: list = []
        self.options: dict | None = None
        self.calls = 0

    async def astream_chat(self, messages, options=None):
        self.calls += 1
        self.messages.append(messages)
        self.options = options
        if self._on_call is not None:
            self._on_call()
        if self._gate is not None:
            await self._gate.wait()
        if self._raises is not None:
            raise self._raises
        text = self._output if isinstance(self._output, str) else json.dumps(self._output)
        for i in range(0, len(text), 48):
            yield text[i:i + 48]

    @property
    def prompt(self) -> str:
        return "\n".join(m["content"] for m in self.messages[0])


def _arrange(monkeypatch, provider, *, sources=None, downgraded=False,
             retrieval_confidence="high"):
    """Wire the fake model, the fake retrieval and the tier selection.

    The queue, the sanitizer, the grounding/confidence classifiers, the shadow
    instrument and every DB path are REAL — those are the parts under test.
    """
    from services import llm_providers, model_router, sage_service
    from services.model_router import ModelChoice
    from config import AMBIENT_LLM_MODEL, LLM_MODEL

    sources = SOURCES if sources is None else sources
    text = "\n".join(f"(DOC: {s['document_id']}) {s['title']}\n{s['content']}"
                     for s in sources)

    def _fake_retrieval(query, warm_candidates=None, limit=None):
        return (text if sources else "", list(sources), retrieval_confidence)

    async def _fake_select(preferred=None, fallback=None):
        return (ModelChoice(AMBIENT_LLM_MODEL, True, LLM_MODEL) if downgraded
                else ModelChoice(LLM_MODEL, False, LLM_MODEL))

    monkeypatch.setattr(sage_service, "_get_guideline_context", _fake_retrieval)
    monkeypatch.setattr(model_router, "select_chat_model", _fake_select)
    monkeypatch.setattr(llm_providers, "provider_for_model", lambda model: provider)
    return provider


async def _drain(timeout: float = 10.0):
    """Wait for every scheduled draft to finish. Returns when none are in flight."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        pending = [t for t in list(sas._inflight) if not t.done()]
        if not pending:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("assessment tasks did not finish")
        await asyncio.wait(pending, timeout=max(0.05, deadline - asyncio.get_running_loop().time()))


# ── readers ──────────────────────────────────────────────────────────────────
def _assessments(patient_id) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM sage_assessments WHERE patient_id = ? ORDER BY created_at",
            (patient_id,)).fetchall()]


def _one(patient_id) -> dict:
    rows = _assessments(patient_id)
    assert len(rows) == 1, f"expected exactly one assessment, got {len(rows)}"
    return rows[0]


def _charted_drugs(patient_id) -> set:
    """The drug names on this patient's chart.

    `_mk_patient` charts Warfarin so the pre-screen has a fact to hand the model,
    so "nothing was prescribed" is no longer "the medications table is empty".
    The invariant is that NOTHING WAS ADDED, and it is asserted BY NAME rather
    than by row count — a count passes just as happily when one drug is silently
    swapped for another.
    """
    with db_session() as db:
        return {r["drug_name"] for r in db.execute(
            "SELECT drug_name FROM medications WHERE patient_id = ?",
            (patient_id,)).fetchall()}


def _cards(patient_id) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM sage_actions WHERE patient_id = ? ORDER BY created_at",
            (patient_id,)).fetchall()]


def _shadow(patient_id=None, kind=None) -> list[dict]:
    sql = "SELECT * FROM shadow_events_current WHERE 1=1"
    args: list = []
    if patient_id:
        sql += " AND patient_id = ?"
        args.append(patient_id)
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    with db_session() as db:
        return [dict(r) for r in db.execute(sql + " ORDER BY id", tuple(args)).fetchall()]


def _payload(row) -> dict:
    try:
        return json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def _alert_message(patient_id):
    """The Relay alert row for this patient, or None."""
    with db_session() as db:
        row = db.execute(
            "SELECT m.* FROM messages m JOIN channels c ON c.id = m.channel_id "
            "WHERE c.patient_id = ? ORDER BY m.id DESC LIMIT 1", (patient_id,)).fetchone()
    return dict(row) if row else None


def _sim_payload(patient_id: str) -> dict:
    """One vitals tick as the simulator hands it to the dispatcher."""
    return {
        "patient_id": patient_id,
        "heart_rate": PERI_ARREST["heart_rate"],
        "systolic_bp": PERI_ARREST["systolic_bp"],
        "diastolic_bp": PERI_ARREST["diastolic_bp"],
        "spo2": PERI_ARREST["spo2"],
        "temperature": PERI_ARREST["temperature"],
        "respiratory_rate": PERI_ARREST["respiratory_rate"],
        "risk": _news2(PERI_ARREST),
    }


class _NullSio:
    async def emit(self, *a, **kw):
        return None


def _write_alert(patient_id: str) -> int:
    """A Relay alert already persisted and delivered — the state the world is in
    by the time `publish_vigil_critical` is called."""
    with db_session() as db:
        # role='doctor' matches how bot_service seeds this account; the users
        # table's CHECK has no 'intelligence' role, and `INSERT OR IGNORE` would
        # swallow the violation and leave the FK below to fail confusingly.
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('bot_vigil', 'Orb Vigil', 'doctor', 'system')")
        cur = db.execute(
            "INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, "
            "content, message_type) VALUES (?, 'bot_vigil', 'Orb Vigil', "
            "'intelligence', 'VIGIL ALERT — NEWS2 19 (critical)', 'alert')",
            (_room(patient_id),))
        return cur.lastrowid


# ═════════════════════════════════════════════════════════════════════════════
#  1. EVERY LANE
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("lane", ["simulator", "manual", "ingestion"])
async def test_a_critical_alert_from_every_lane_produces_a_drafted_assessment(monkeypatch, lane):
    """One seam, every lane.

    The assessment hangs off `bot_service.publish_vigil_critical` — the ONE
    publisher every lane already reaches — precisely so this cannot become the
    old defect again: the previous Sage output existed on the simulator lane
    only, so it fired for demo patients and for nobody the pilot will run on.
    Manual bedside entry is the lane the shadow pilot goes live on.
    """
    from services import bot_service, event_bus

    async def _no_publish(evt):
        return None

    monkeypatch.setattr(event_bus.event_bus, "publish", _no_publish)
    _arrange(monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    patient = _mk_patient()
    msg_id = _write_alert(patient)

    assert bot_service.publish_vigil_critical(
        patient, level="critical", score=0.95, drivers=["RR 32", "SpO2 86%"],
        message_id=msg_id, raw_news2=19, lane=lane) is True
    await _drain()

    row = _one(patient)
    assert row["state"] == "ready", row["unavailable_reason"]
    assert row["lane"] == lane
    assert row["message_id"] == msg_id
    # THE ALERT LANE IS THE FAST LANE. A 46-second four-section draft was
    # measured useless at a deteriorating bedside; what the alert now schedules
    # is Tier 1 (instant, deterministic) plus a capped list of next steps.
    assert row["depth"] == sas.DEPTH_BRIEF
    assert json.loads(row["tier1"])["available"] is True, (
        "the deterministic drivers are missing from the row the alert produced")
    assert json.loads(row["considerations"]), (
        "a ready assessment with no next steps is an empty card")
    assert row["summary"] is None, (
        "the fast lane spent decode time re-writing what Tier 1 already states")


async def test_an_elevated_alert_gets_no_assessment(monkeypatch):
    """The clinical case for a written assessment starts at critical, and a 27B
    generation per elevated patient per vitals cycle is a background storm."""
    from services import bot_service, event_bus

    async def _no_publish(evt):
        return None

    monkeypatch.setattr(event_bus.event_bus, "publish", _no_publish)
    _arrange(monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    patient = _mk_patient()
    bot_service.publish_vigil_critical(
        patient, level="elevated", score=0.4, drivers=["RR 22"],
        message_id=_write_alert(patient), raw_news2=5, lane="manual")
    await _drain()

    assert _assessments(patient) == []


async def test_the_simulator_dispatcher_reaches_the_assessment_seam(monkeypatch):
    """End to end through the real dispatch loop, not through a hand-built call.

    `evaluate_and_dispatch_alerts` is the simulator's whole alert path; if the
    seam moves, this fails even though the unit-level lane test above still
    passes.
    """
    from services import bot_service, event_bus

    async def _no_publish(evt):
        return None

    monkeypatch.setattr(event_bus.event_bus, "publish", _no_publish)

    seen: list[dict] = []
    monkeypatch.setattr(bot_service, "_schedule_assessment",
                        lambda pid, **kw: seen.append({"pid": pid, **kw}) or True)

    async def _rec(vitals_summary, risk_level, drivers):
        return "Escalate to the registrar."

    monkeypatch.setattr(bot_service, "generate_ai_recommendation", _rec)

    patient = _mk_patient()
    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])

    assert len(seen) == 1, f"the simulator lane no longer asks Sage for a draft: {seen}"
    assert seen[0]["level"] == "critical"
    assert seen[0]["raw_news2"] == 19
    assert seen[0]["message_id"] is not None, (
        "the draft cannot be tied back to the alert it belongs to")


# ═════════════════════════════════════════════════════════════════════════════
#  2. THE ALERT NEVER WAITS  —  the load-bearing latency property
# ═════════════════════════════════════════════════════════════════════════════
async def test_the_alert_does_not_wait_for_the_assessment(monkeypatch):
    """PROVE-CAN-FAIL TARGET #1.

    Generation is blocked forever. The alert path must still complete: the Relay
    message written, the alert delivered, the dispatcher returned. The draft is
    left honestly `generating`.

    This is the property that protects the 51 ms number. If someone ever awaits
    generation on the alert path — the natural, tidy-looking refactor — this test
    does not merely fail, it hangs, and the timeout below turns that into a
    failure with a readable message.
    """
    from services import bot_service, event_bus

    gate = asyncio.Event()
    _arrange(monkeypatch, _FakeProvider(BRIEF_OUTPUT, gate=gate))

    async def _no_publish(evt):
        return None

    monkeypatch.setattr(event_bus.event_bus, "publish", _no_publish)

    async def _rec(vitals_summary, risk_level, drivers):
        return "Escalate to the registrar."

    monkeypatch.setattr(bot_service, "generate_ai_recommendation", _rec)

    patient = _mk_patient()
    try:
        try:
            await asyncio.wait_for(
                bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)]),
                timeout=5.0)
        except asyncio.TimeoutError:
            raise AssertionError(
                "the alert path waited for the assessment — vitals-to-alert latency "
                "is now bounded by a 27B generation")

        # THE ALERT IS REAL AND ON THE RECORD while the model is still blocked.
        assert _alert_message(patient) is not None, "no Relay alert was written"
        assert _shadow(patient, "alert.fired"), "the alert did not reach the instrument"
        # Zero rows is the common (and stronger) outcome: the alert path returned
        # before the draft's first DB dispatch even completed. What must never be
        # true is a FINISHED draft, which would mean the path waited for it.
        assert all(r["state"] != "ready" for r in _assessments(patient)), (
            "the assessment completed before the alert path returned — the "
            "ordering under test cannot have held")
    finally:
        gate.set()

    await _drain()
    assert _one(patient)["state"] == "ready"


async def test_the_alert_row_exists_before_generation_begins(monkeypatch):
    """The same ordering, asserted from the other end.

    At the instant the model is first called, the Relay alert must already be a
    row a clinician could be reading. Checked inside the provider so it is a
    statement about causal order, not about wall-clock luck.
    """
    from services import bot_service, event_bus

    patient = _mk_patient()
    observed: dict = {}

    def _look():
        observed["alert"] = _alert_message(patient)

    _arrange(monkeypatch, _FakeProvider(BRIEF_OUTPUT, on_call=_look))

    async def _no_publish(evt):
        return None

    monkeypatch.setattr(event_bus.event_bus, "publish", _no_publish)

    async def _rec(vitals_summary, risk_level, drivers):
        return "Escalate to the registrar."

    monkeypatch.setattr(bot_service, "generate_ai_recommendation", _rec)

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])
    await _drain()

    assert observed.get("alert") is not None, (
        "Sage began drafting before the alert existed — the clinician would have "
        "been offered an assessment of a page they had not been shown")


# ═════════════════════════════════════════════════════════════════════════════
#  3. FAILURE IS HONEST
# ═════════════════════════════════════════════════════════════════════════════
async def _generate(patient, monkeypatch, provider, *, depth=sas.DEPTH_BRIEF, **kw):
    """Run one draft directly, bypassing the alert path.

    `depth` defaults to BRIEF because that is what the ALERT path now runs — the
    four-section draft was measured at a median 46 s and moved to the surfaces
    where a clinician is reading rather than reacting. Tests that are about the
    four-section shape pass depth=full explicitly.
    """
    _arrange(monkeypatch, provider, **kw)
    return await sas.generate_for_alert(
        patient, level="critical", score=0.95, drivers=["RR 32", "SpO2 86%"],
        message_id=_write_alert(patient), raw_news2=19, lane="manual", depth=depth)


@pytest.mark.parametrize("failure,reason", [
    ("shed", sas.REASON_SHED),
    ("queue_timeout", sas.REASON_QUEUE_TIMEOUT),
    ("generation_timeout", sas.REASON_GENERATION_TIMEOUT),
    ("model_error", sas.REASON_MODEL_ERROR),
    ("empty", sas.REASON_EMPTY_OUTPUT),
    ("garbage", sas.REASON_UNPARSEABLE),
])
async def test_a_failed_generation_is_unavailable_with_a_reason_never_fabricated(
        monkeypatch, failure, reason):
    """Every way this can fail ends in the same honest place.

    Not an empty card that looks pending, not a partial assessment, not the
    static sentence dressed up as a draft. `unavailable` + a named reason + a
    ward-facing sentence + the EXISTING deterministic fallback text, and no
    interpretation at all.
    """
    from services.llm_queue_service import BackgroundAdmissionRejected, llm_queue

    patient = _mk_patient()
    provider = _FakeProvider(BRIEF_OUTPUT)

    if failure == "shed":
        async def _acquire(*a, **kw):
            raise BackgroundAdmissionRejected("shed")
        monkeypatch.setattr(llm_queue, "acquire", _acquire)
    elif failure == "queue_timeout":
        async def _acquire(*a, **kw):
            raise asyncio.TimeoutError()
        monkeypatch.setattr(llm_queue, "acquire", _acquire)
    elif failure == "generation_timeout":
        # Both budgets: the fast lane has its own (30 s, because advice that
        # arrives later than that cannot be used at a bedside) and the full tier
        # keeps the 120 s one.
        monkeypatch.setenv("ORB_SAGE_ADVICE_TIMEOUT", "0.2")
        monkeypatch.setenv("ORB_SAGE_ASSESSMENT_TIMEOUT", "0.2")
        provider = _FakeProvider(BRIEF_OUTPUT, gate=asyncio.Event())
    elif failure == "model_error":
        provider = _FakeProvider(BRIEF_OUTPUT, raises=RuntimeError("ollama exploded"))
    elif failure == "empty":
        provider = _FakeProvider("")
    elif failure == "garbage":
        provider = _FakeProvider("I am afraid I cannot help with that request.")

    assert await _generate(patient, monkeypatch, provider) is None

    row = _one(patient)
    assert row["state"] == "unavailable"
    assert row["unavailable_reason"] == reason
    assert not row["summary"], "an unavailable assessment must carry NO interpretation"
    assert not row["considerations"]

    payload = sas.render(row)
    assert payload["unavailable_message"], (
        "the surface has no honest sentence to show, so it will show a spinner")
    assert payload["fallback_text"], "the existing deterministic sentence was dropped"
    assert payload["whats_happening"] is None

    # A failure is NOT a draft. Counting it would make the instrument report Sage
    # participation on a night Sage was down.
    assert _shadow(patient, "sage.drafted") == []
    failed = _shadow(patient, "sage.failed")
    assert len(failed) == 1 and _payload(failed[0])["failure"] == reason


async def test_an_evicted_careful_tier_refuses_rather_than_answering_on_the_small_model(monkeypatch):
    """The 4B does not author a clinical assessment, and it certainly does not do
    so in secret.

    `select_chat_model` silently downgrades to the ambient tier when the 27B has
    been evicted, which is right for a chat turn labelled as downgraded and wrong
    for this: the 4B was measured wrong on 8 of 20 single-fact recall questions.
    """
    patient = _mk_patient()
    provider = _FakeProvider(BRIEF_OUTPUT)

    assert await _generate(patient, monkeypatch, provider, downgraded=True) is None
    assert provider.calls == 0, "the small model was asked to write a clinical assessment"

    row = _one(patient)
    assert row["state"] == "unavailable"
    assert row["unavailable_reason"] == sas.REASON_MODEL_EVICTED


async def test_an_abandoned_draft_reads_as_unavailable_not_as_pending(monkeypatch):
    """A killed process must not leave a permanent spinner.

    If generation is interrupted — shutdown, SIGKILL, a cancelled task — the row
    stays `generating` because no code of ours ran to change it. Read-side
    derivation is what makes that honest: past the stale window the surface is
    told 'abandoned', not 'still coming'.
    """
    monkeypatch.setenv("ORB_SAGE_ASSESSMENT_STALE_AFTER", "60")
    patient = _mk_patient()
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO sage_assessments (id, patient_id, channel_id, lane, state, "
            "created_at, updated_at) VALUES ('stale-1', ?, ?, 'manual', 'generating', ?, ?)",
            (patient, _room(patient), old, old))

    payload = sas.render(_one(patient))
    assert payload["state"] == "unavailable"
    assert payload["unavailable_reason"] == sas.REASON_ABANDONED
    assert payload["unavailable_message"]


async def test_an_alert_with_no_assemblable_chart_still_gets_an_answer(monkeypatch):
    """Silent absence is the failure mode, not the safe default.

    The alert is on someone's screen. If Orb cannot assemble the chart there is
    no assessment to write — but there must still be a row saying so, or the
    surface has nothing to distinguish "not written" from "still coming".
    """
    provider = _FakeProvider(BRIEF_OUTPUT)
    _arrange(monkeypatch, provider)
    # Prefixed like the real fixtures so the cleanup sweep collects its row too.
    ghost = f"ASSESSP-GHOST-{uuid.uuid4().hex[:6].upper()}"
    _CREATED.append(ghost)

    assert await sas.generate_for_alert(
        ghost, level="critical", score=0.9, drivers=[], message_id=None,
        raw_news2=19, lane="manual") is None
    assert provider.calls == 0, "a model was asked to assess a patient with no chart"

    row = _one(ghost)
    assert row["state"] == "unavailable"
    assert row["unavailable_reason"] == sas.REASON_NO_CONTEXT
    assert sas.render(row)["unavailable_message"]


async def test_a_cancel_propagates_and_does_not_hold_the_appliance_open(monkeypatch):
    """Six swallowed cancels hung a shutdown on 2026-08-14.

    A draft nobody asked for must never be able to keep the process alive at
    "Waiting for application shutdown."
    """
    gate = asyncio.Event()
    patient = _mk_patient()
    _arrange(monkeypatch, _FakeProvider(BRIEF_OUTPUT, gate=gate))

    task = asyncio.create_task(sas.generate_for_alert(
        patient, level="critical", score=0.9, drivers=["RR 32"],
        message_id=_write_alert(patient), raw_news2=19, lane="manual"))
    for _ in range(200):
        if _assessments(patient):
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ═════════════════════════════════════════════════════════════════════════════
#  4. ORB'S ARITHMETIC WINS
# ═════════════════════════════════════════════════════════════════════════════
async def test_the_news2_in_the_prompt_is_the_deterministic_value(monkeypatch):
    """Pinned, marked AUTHORITATIVE, and never left to the model to derive.

    Measured twice before: asked for a NEWS2 the model re-derived it from the
    raw vitals and answered 7 against Orb's 11; pinned at the aggregate it then
    got the decomposition wrong ("SpO2 80.1% — 4 points", where 3 is the NEWS2
    per-parameter maximum).

    NOTE WHAT THIS FIXTURE DOES NOT HAVE: a `risk_scores` row. That row is
    written by a BACKGROUND pipeline, so on a freshly charted bedside
    observation — the lane the pilot runs on — it lags the vitals it scores. The
    chart renderer emits its NEWS2 lines only when that row exists, so a pin
    that relied on it would silently vanish exactly when a nurse charts a
    deterioration by hand. This test is deliberately run without one.
    """
    patient = _mk_patient()
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) c FROM risk_scores WHERE patient_id = ?",
                          (patient,)).fetchone()["c"] == 0, "fixture precondition"
    provider = _FakeProvider(BRIEF_OUTPUT)
    await _generate(patient, monkeypatch, provider)

    prompt = provider.prompt
    assert "AUTHORITATIVE" in prompt
    assert "NEWS2" in prompt and "19" in prompt, (
        "the deterministic score is not in the prompt — the model will do the "
        "arithmetic itself, which is the NEWS2 7-vs-11 defect")
    assert "never recalculate" in prompt.lower()
    # The per-parameter decomposition too: pinning only the aggregate is what
    # produced "SpO2 80.1% — 4 points". Asserted on the DECOMPOSITION ITSELF
    # rather than on a heading, because the fast lane states it compactly —
    # prefill is on the critical path to the first word a clinician reads.
    assert "per-parameter points" in prompt.lower()
    assert "3 is the maximum for any one parameter" in prompt
    assert "Respiratory rate 32/min = 3pt" in prompt, (
        f"the per-parameter points are not pinned: {prompt}")
    assert _one(patient)["news2"] == 19, "the stored score is not the engine's"


async def test_the_model_cannot_override_the_deterministic_news2(monkeypatch):
    """The model says 10. The engine says 19. The clinician is shown 19.

    A clinician looking at the escalation board and at this draft in the same
    second must not be handed two different scores for the same patient — the
    exact divergence found on 2026-08-13 (case room 19, board 10).

    Asserted on the FULL tier because that is the one that still writes prose.
    The fast lane's version of the same law — the pin applied to a step line as
    it streams — is in test_assessment_tiers.
    """
    patient = _mk_patient()
    lying = dict(GOOD_OUTPUT)
    lying["whats_happening"] = (
        "This patient has a NEWS2 of 10, driven by respiratory rate and "
        "saturations. A NEWS2 score 10 warrants urgent review. NEWS2 = 10 "
        "and NEWS 2 is 10 on recheck.")
    await _generate(patient, monkeypatch, _FakeProvider(lying), depth=sas.DEPTH_FULL)

    summary = _one(patient)["summary"]
    # Every phrasing is corrected, and the label is normalised to "NEWS2" —
    # a corrected sentence is already being rewritten, so it may as well come
    # out spelled the way the rest of the product spells it.
    for phrasing in ("NEWS2 of 19", "NEWS2 score 19", "NEWS2 = 19", "NEWS2 is 19"):
        assert phrasing in summary, f"{phrasing!r} missing from: {summary}"
    assert "10" not in summary, f"the model's wrong score survived: {summary}"
    assert "NEWS 2" not in summary, "the label was left in two spellings"


# ═════════════════════════════════════════════════════════════════════════════
#  5. NOTHING EXECUTES
# ═════════════════════════════════════════════════════════════════════════════
async def test_nothing_executes_without_an_explicit_approval_call(monkeypatch):
    """PROVE-CAN-FAIL TARGET #2, and the reason this feature is allowed near a ward.

    The draft proposes a drug. After generation there must be NO medication row,
    NO downstream handoff and NO confirmed card — only a card sitting in
    'suggested' waiting for a human.
    """
    from services import medication_safety_gate

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"drug": "Ceftriaxone", "blocking": False,
                                          "max_severity": "none", "safe": True})
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    cards = _cards(patient)
    assert len(cards) == 1, f"expected exactly one proposed card, got {len(cards)}"
    assert cards[0]["action_type"] == "start_medication"
    assert cards[0]["state"] == "suggested", (
        "Sage's proposal resolved itself — a machine approved a clinical action")

    with db_session() as db:
        handoffs = db.execute("SELECT * FROM sage_handoffs WHERE patient_id = ?",
                              (patient,)).fetchall()
        orders = db.execute("SELECT COUNT(*) c FROM orders WHERE patient_id = ?",
                            (patient,)).fetchone()
    # Ceftriaxone is what the draft PROPOSED; Warfarin is what the fixture
    # charted. The chart must be exactly as it was before the draft ran.
    assert _charted_drugs(patient) == {"Warfarin"}, (
        "a medication reached the chart with no clinician click")
    assert handoffs == [], "a downstream handoff was dispatched with no clinician click"
    assert orders["c"] == 0, "an order was created with no clinician click"


async def test_a_missing_system_account_does_not_destroy_the_draft(monkeypatch):
    """Found by the full suite, not by this file in isolation.

    `sage_action_events.actor_id` is `REFERENCES users(id) NOT NULL` and the
    'system' account is seeded once, at init. Anything that removes it — a
    purge, an erasure run, an operator tidying accounts — turned every card
    write into a FOREIGN KEY failure that rolled back the ENTIRE draft: no
    assessment, no cards, no instrument rows, for want of a bookkeeping row.
    """
    from services import medication_safety_gate

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"blocking": False, "max_severity": "none"})
    with db_session() as db:
        # Children before the parent — 'system' is referenced by every machine-
        # authored card event an earlier test in this file left behind.
        db.execute("DELETE FROM sage_action_events WHERE actor_id = 'system'")
        db.execute("DELETE FROM users WHERE id = 'system'")
        assert db.execute("SELECT COUNT(*) c FROM users WHERE id = 'system'"
                          ).fetchone()["c"] == 0, "precondition: the account is gone"

    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    assert _one(patient)["state"] == "ready", (
        "a clinical draft was lost because a machine-principal row was missing")
    assert len(_cards(patient)) == 1


async def test_the_card_wall_is_bounded(monkeypatch):
    """A card per idea is how alert fatigue starts.

    Everything past the budget is still SHOWN — as advice, with a stated reason —
    so nothing is silently dropped from the clinician's view.
    """
    from services import medication_safety_gate

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"blocking": False, "max_severity": "none"})
    monkeypatch.setenv("ORB_SAGE_ASSESSMENT_MAX_ACTIONS", "1")

    # Three cardable proposals, each naming the charted allergy so it survives
    # the patient-specific filter. Lettered rather than numbered: a digit the
    # pre-screen never supplied is itself dropped as a fabricated value, which
    # would make this a test about the guard instead of about the card wall.
    greedy = _brief([(f"Sulfonamide allergy charted — alternative cover, option "
                      f"{chr(ord('A') + i)}", "documented sulfa allergy",
                      "start_medication", "Ceftriaxone") for i in range(3)])
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(greedy))

    assert len(_cards(patient)) == 1
    considerations = json.loads(_one(patient)["considerations"])
    assert len(considerations) == 3, "considerations were dropped rather than demoted"
    demoted = [c for c in considerations if not c["orb_can_act"]]
    assert len(demoted) == 2
    assert all(c["advice_only_reason"] for c in demoted), (
        "a step was quietly demoted to advice with no reason a clinician can read")


async def test_a_step_orb_cannot_route_is_labelled_advice_not_action(monkeypatch):
    """"Escalate to the registrar" is advice. Orb cannot do it, so it must not
    look like something Orb offered to do — and `orb_can_act` is derived from
    whether a card exists, never from the model's claim about itself."""
    patient = _mk_patient()
    from services import medication_safety_gate
    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"blocking": False, "max_severity": "none"})
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    considerations = json.loads(_one(patient)["considerations"])
    # The advice-only step is the anticoagulation flag: a fact for the clinician
    # to weigh, with nothing for Orb to draft as an order.
    escalate = [c for c in considerations if "warfarin" in c["step"].lower()][0]
    assert escalate["orb_can_act"] is False
    assert escalate["action_id"] is None
    assert escalate["action_type"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  6. THE MEDICATION SAFETY GATE STILL APPLIES
# ═════════════════════════════════════════════════════════════════════════════
async def test_a_proposed_medication_is_screened_before_a_card_exists(monkeypatch):
    """An assessment-proposed drug is not a privileged drug.

    The gate is called NON-INTERACTIVELY, which is what makes it a gate rather
    than a formality: a non-interactive caller cannot be handed a 428 and asked
    to acknowledge, so a severe alert is a hard block.
    """
    from services import medication_safety_gate

    seen: list[dict] = []

    def _gate(patient_id, drug_name, **kw):
        seen.append({"patient_id": patient_id, "drug": drug_name, **kw})
        return {"blocking": False, "max_severity": "none"}

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety", _gate)
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    assert len(seen) == 1, "the drug-safety gate was bypassed"
    assert seen[0]["drug"] == "Ceftriaxone"
    assert seen[0]["patient_id"] == patient
    assert seen[0]["interactive"] is False
    assert seen[0]["entry_point"] == "sage_assessment"
    assert len(_cards(patient)) == 1


async def test_a_blocked_medication_never_becomes_a_card(monkeypatch):
    """A severe allergy/interaction alert demotes the proposal to advice — with
    the block stated — rather than offering a clinician a one-click order."""
    from services import medication_safety_gate
    from services.medication_safety_gate import MedicationSafetyBlocked

    def _blocked(patient_id, drug_name, **kw):
        raise MedicationSafetyBlocked(
            {"max_severity": "severe", "blocking": True},
            "severe drug-safety alert on a non-interactive path")

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety", _blocked)
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    assert _cards(patient) == [], "a drug the safety gate blocked was offered as a card"
    considerations = json.loads(_one(patient)["considerations"])
    # The demoted step is the one that PROPOSED the drug. It is found by the
    # allergy it names rather than by the drug name: the step text names the
    # charted fact, and the drug lives in the target the gate screened.
    blocked = [c for c in considerations if "sulfonamide" in c["step"].lower()][0]
    assert blocked["orb_can_act"] is False
    assert "drug-safety" in (blocked["advice_only_reason"] or "").lower()
    assert _shadow(patient, "action.proposed") == [], (
        "a blocked proposal was recorded as an offered action")


async def test_a_drug_class_is_not_a_card(monkeypatch):
    """Measured against the real medgemma:27b on 2026-08-14: one run proposed
    "antibiotics".

    A class is not a drug. The safety screen waves it through (no allergy match,
    no interaction class) and the confirm path's reconciler then cannot resolve
    it — a one-click card that dead-ends at confirmation. It is also not "specific
    enough to act on", which is the bar a proposed action has to clear.
    """
    from services import medication_safety_gate

    screened: list = []
    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: screened.append(a) or {"blocking": False})
    # Names the charted allergy so the patient-specific filter lets it through —
    # what is under test is the unresolvable TARGET, and a line the filter eats
    # first would prove nothing about the formulary check.
    vague = _brief([("Sulfonamide allergy charted — start broad-spectrum cover",
                     "documented sulfa allergy", "start_medication", "antibiotics")])
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(vague))

    assert _cards(patient) == [], "a drug CLASS was offered as a one-click order"
    assert screened == [], (
        "an unresolvable name reached the safety gate and was screened as if it "
        "were a real drug")
    item = json.loads(_one(patient)["considerations"])[0]
    assert item["orb_can_act"] is False
    assert "formulary" in (item["advice_only_reason"] or "")


async def test_a_blocked_slot_is_not_refilled_with_the_next_drug(monkeypatch):
    """The moment an automatic second choice looks most helpful and is least
    defensible.

    When the safety gate refuses a drug, the freed card slot stays empty.
    Promoting the next proposal into it would be Orb quietly choosing a
    substitute medication after a safety block.
    """
    from services import medication_safety_gate
    from services.medication_safety_gate import MedicationSafetyBlocked

    def _blocked(patient_id, drug_name, **kw):
        raise MedicationSafetyBlocked({"max_severity": "severe", "blocking": True},
                                      "severe drug-safety alert")

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety", _blocked)
    monkeypatch.setenv("ORB_SAGE_ASSESSMENT_MAX_ACTIONS", "1")

    # Both steps name a charted fact, or the patient-specific filter would drop
    # them before the card wall ever saw them and this would pass vacuously.
    pair = _brief([
        ("Sulfonamide allergy charted — alternative cover needed", "sulfa allergy",
         "start_medication", "Ceftriaxone"),
        ("Warfarin on the chart — reverse the anticoagulation before theatre",
         "anticoagulated", "transfer_to_surgery", "Laparotomy"),
    ])
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(pair))

    assert _cards(patient) == [], (
        "the slot freed by a drug-safety block was refilled with the next "
        "proposal — Orb chose a substitute after a safety refusal")
    reasons = [c["advice_only_reason"] for c in json.loads(_one(patient)["considerations"])]
    assert any("drug-safety" in (r or "") for r in reasons)
    assert any("at most 1 action cards" in (r or "") for r in reasons)


async def test_a_failed_safety_screen_is_not_a_clean_screen(monkeypatch):
    """The screen erroring is not the same as the screen passing. Fail closed."""
    from services import medication_safety_gate

    def _boom(*a, **kw):
        raise RuntimeError("interaction DB unavailable")

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety", _boom)
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    assert _cards(patient) == []
    assert _one(patient)["state"] == "ready", (
        "one unscreenable drug should demote that step, not destroy the assessment")


# ═════════════════════════════════════════════════════════════════════════════
#  7. THE INSTRUMENT
# ═════════════════════════════════════════════════════════════════════════════
async def test_sage_drafted_and_action_proposed_land_on_the_alerts_episode(monkeypatch):
    """One episode, or the loop is three unrelated rows.

    `agreement` pairs a proposal with the clinician's next action IN THE SAME
    EPISODE. A draft on its own episode is an orphan and the metric stays at
    insufficient_data however many alerts fire.
    """
    from services import bot_service, medication_safety_gate

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"blocking": False, "max_severity": "none"})

    async def _rec(vitals_summary, risk_level, drivers):
        return "Escalate to the registrar."

    monkeypatch.setattr(bot_service, "generate_ai_recommendation", _rec)
    from services import event_bus

    async def _no_publish(evt):
        return None

    monkeypatch.setattr(event_bus.event_bus, "publish", _no_publish)
    _arrange(monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    patient = _mk_patient()
    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])
    await _drain()

    fired = _shadow(patient, "alert.fired")
    # The FAST lane's own query_class. A capped three-step draft and a
    # four-section assessment must not pool into one p50 — `_time_saved` quotes
    # that figure and the two workloads have nothing to do with each other.
    drafted = [d for d in _shadow(patient, "sage.drafted")
               if _payload(d).get("query_class") == sas.QUERY_CLASS_BRIEF]
    proposed = _shadow(patient, "action.proposed")

    assert len(fired) == 1
    assert len(drafted) == 1, "the assessment was not recorded as a Sage draft"
    assert len(proposed) == 1, "the proposed card was not recorded as a proposal"
    assert drafted[0]["episode_id"] == fired[0]["episode_id"], "the draft is an orphan"
    assert proposed[0]["episode_id"] == fired[0]["episode_id"], "the proposal is an orphan"

    # The pairing key `agreement` matches on. Without it the clinician's later
    # approve/decline is a resolution with nothing to resolve.
    assert proposed[0]["subject_type"] == "sage_actions"
    assert proposed[0]["subject_id"] == _cards(patient)[0]["id"]
    assert _payload(proposed[0])["action_class"] == "start_medication"
    assert _payload(proposed[0])["gate"] == "clinician_confirmation"

    payload = _payload(drafted[0])
    # THE FAST LANE'S PROVENANCE IS THE CHART, NOT A DOCUMENT. Since 2026-08-15
    # Tier 2 answers only what this patient's own chart implies and makes no
    # retrieval call at all, so the instrument records which CHARTED FACT each
    # surviving flag rests on. Asserted positively rather than deleted: a
    # provenance field nobody checks is how the shadow log quietly stops being
    # evidence.
    assert payload["citation_basis"] == "chart_derived"
    assert payload["retrieved_doc_ids"] == [], (
        "the fast lane retrieved a guideline it is no longer supposed to fetch")
    assert payload["trust_tiers"] == []
    # One per flag, naming the pre-screen rule that produced the fact it cites.
    assert set(payload["cited_doc_ids"]) == {
        "chart:anticoagulated_in_this_picture",
        "chart:antimicrobial_allergy_in_an_infective_picture",
    }, payload["cited_doc_ids"]
    assert isinstance(payload["total_ms"], int)
    assert payload["cancelled"] is False


async def test_a_second_publish_for_the_same_alert_does_not_double_draft(monkeypatch):
    """A retry, a restart race or a double publish must not produce two drafts.

    A clinician reading two machine assessments of the same deterioration has no
    way to tell which is current, and the second would double-count in the
    instrument.
    """
    patient = _mk_patient()
    msg_id = _write_alert(patient)
    _arrange(monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    first = await sas.generate_for_alert(
        patient, level="critical", score=0.9, drivers=["RR 32"],
        message_id=msg_id, raw_news2=19, lane="manual")
    second = await sas.generate_for_alert(
        patient, level="critical", score=0.9, drivers=["RR 32"],
        message_id=msg_id, raw_news2=19, lane="manual")

    assert first is not None and second is None
    assert len(_assessments(patient)) == 1


# ═════════════════════════════════════════════════════════════════════════════
#  8. WHAT THE CLINICIAN READS
# ═════════════════════════════════════════════════════════════════════════════
async def test_the_draft_never_claims_to_have_changed_the_chart(monkeypatch):
    """The guardrail applies here exactly as it does to chat, stream and voice.

    A phrase any lane strips, every lane must strip — and this lane imports THE
    list rather than restating it, because the hand-copied clone in the voice
    route had already drifted.
    """
    # The line has to NAME A CHARTED FACT to survive the patient-specific filter
    # at all — otherwise it is dropped as unsourced and this would prove the
    # sanitizer works by never reaching it. The forbidden phrases are what is
    # under test; "Warfarin" is what buys the line its ticket.
    lying = _brief([
        ("Warfarin is on the chart. Order placed. Continue fluid resuscitation "
         "for septic shock.",
         "I have placed the order. Chart updated. Done.", None, None)])
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(lying))

    row = _one(patient)
    body = row["considerations"]
    for phrase in ("I have placed", "Chart updated", "Order placed", "Done."):
        assert phrase not in body, f"a fabricated confirmation survived: {phrase!r}"
    assert "septic" in body, "the strip ate the clinical content too"


async def test_the_guideline_query_says_what_the_patient_has(monkeypatch):
    """DEF-4's root cause, pinned at THIS lane's seam.

    `hint` was ' '.join(prep["news2_drivers"]) — pure physiology, so a DKA
    patient, a dengue patient and a UTI patient with similar vitals all sent
    identical text to retrieval and got identical documents back
    (services/retrieval_query.py carries the measurement).

    Asserted here rather than only in test_evidence_relevance because this is
    the join that has to hold: `_open_assessment` assembles the chart, and
    `_generate` has to actually USE it. Retrieval itself is stubbed — what is
    under test is the query, not what the corpus does with it.

    ON THE FULL TIER, because that is now the only tier that retrieves. Tier 2
    was re-shaped on 2026-08-15 to say only what this patient's own chart
    implies, and a guideline pack in that window would be an open invitation to
    write exactly the generic management the post-filter deletes — so the fast
    lane makes no retrieval call at all. The query-construction defect this test
    pins (DEF-4) lives in `build_guideline_query`, which the full tier still
    reaches on every draft.
    """
    from services import sage_service

    seen: list[str] = []
    patient = _mk_patient()  # diagnosis: 'Community acquired pneumonia'
    _arrange(monkeypatch, _FakeProvider(GOOD_OUTPUT))
    real = sage_service._get_guideline_context

    def _spy(query, warm_candidates=None, limit=None):
        seen.append(query)
        return real(query, warm_candidates=warm_candidates, limit=limit)

    monkeypatch.setattr(sage_service, "_get_guideline_context", _spy)
    await sas.generate_for_alert(
        patient, level="critical", score=0.95, drivers=["RR 32", "SpO2 86%"],
        message_id=_write_alert(patient), raw_news2=19, lane="manual",
        depth=sas.DEPTH_FULL)

    assert seen, "retrieval was never called"
    query = seen[0]
    assert "Community acquired pneumonia" in query, (
        f"the query does not name the patient's condition: {query!r}")
    assert query != sas.build_guideline_query(None, None), (
        "the query fell all the way through to the no-context fallback")


async def test_a_fabricated_citation_never_reaches_the_payload(monkeypatch):
    """Measured 2026-08-03: 3 of 4 citations the model produced were invented ids.

    A citation that points at nothing is worse than no citation — it looks
    checkable and is not, and a clinician being able to check is this product's
    whole trust argument.

    ON THE FULL TIER, because it is the only one that cites documents. The fast
    lane cites the patient's own charted facts (`citation_basis`
    `chart_derived`) and cannot invent a document id because it is never given
    one; its equivalent guard is the fabrication check on drug names and values,
    pinned in tests/test_assessment_tiers.
    """
    inventing = {**GOOD_OUTPUT, "cited_sources": ["SSC23", "NICE_NG51", "EXT"]}
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(inventing),
                    depth=sas.DEPTH_FULL)

    row = _one(patient)
    evidence = json.loads(row["evidence"])
    ids = {e["document_id"] for e in evidence}
    assert ids == {"LOC-SEPSIS-001", "EXT-RESP-004"}
    assert all(e["attribution"] in ("model_cited", "system_attributed", "context_only")
               for e in evidence)
    assert not any(e["attribution"] == "model_cited" for e in evidence), (
        "an invented id was rendered as a citation the clinician could 'check'")
    assert row["confidence"] == "low", (
        "the model named documents and not one was retrieved — that is active "
        "invention and must not read as confident")


async def test_no_retrieval_says_so_rather_than_implying_guideline_backing(monkeypatch):
    """"WHAT THIS IS BASED ON" has to be honest when the answer is nothing.

    The full tier, for the same reason as the two tests above: it is the tier
    that retrieves, so it is the only one where "nothing came back" is a state
    that can arise.
    """
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(GOOD_OUTPUT), sources=[],
                    depth=sas.DEPTH_FULL)

    payload = sas.render(_one(patient))
    assert payload["citation_basis"] == "none"
    assert payload["evidence"] == []
    assert payload["evidence_note"], (
        "an empty evidence section with no explanation reads as 'guideline-backed'")
    assert "no guideline" in payload["evidence_note"].lower()


async def test_what_would_change_the_picture_is_mandatory(monkeypatch):
    """The section that separates decision support from an oracle.

    When the model omits it, Orb supplies statements about its OWN data coverage
    — reporting, never authored clinical advice — so the section can be mandatory
    without ever fabricating content.
    """
    silent = dict(GOOD_OUTPUT)
    silent["watch_items"] = []
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(silent), depth=sas.DEPTH_FULL)

    watch = json.loads(_one(patient)["watch_items"])
    assert watch, "the mandatory section was allowed to be empty"
    assert any("Orb sees charted data only" in w for w in watch)


@pytest.mark.parametrize("depth,output", [(sas.DEPTH_BRIEF, BRIEF_OUTPUT),
                                          (sas.DEPTH_FULL, GOOD_OUTPUT)])
async def test_the_payload_carries_every_section_and_the_posture(monkeypatch, depth, output):
    """The contract the UI renders against, pinned in one place, for both depths.

    Tier 1 and the disclaimer are on BOTH. `whats_happening` is the one section
    that differs: on the fast lane the interpretation is Tier 1's deterministic
    block, and asking the model to restate it in prose is what made the first
    version take 46 seconds.
    """
    from services import medication_safety_gate
    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"blocking": False, "max_severity": "none"})
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(output), depth=depth)

    p = sas.render(_one(patient))
    assert p["state"] == "ready" and p["advice_state"] == "ready"
    assert p["depth"] == depth
    assert p["has_tier1"] is True and p["tier1"]["available"] is True
    assert p["has_advice"] is True
    assert p["considerations"]
    assert p["evidence"] and p["citation_basis"]
    assert p["watch_items"]
    assert p["news2"] == 19 and p["news2_level"] == "critical"
    assert p["disclaimer"] and "approval" in p["disclaimer"].lower()
    assert p["unavailable_message"] is None
    if depth == sas.DEPTH_FULL:
        assert p["whats_happening"]
    else:
        assert p["whats_happening"] is None
    first = p["considerations"][0]
    assert set(first) >= {"step", "why", "orb_can_act", "action_id", "action_type",
                          "advice_only_reason"}


# ═════════════════════════════════════════════════════════════════════════════
#  9. APPROVE / DECLINE
# ═════════════════════════════════════════════════════════════════════════════
def _mk_doctor() -> dict:
    uid = f"ASSESSD{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Assessment Doctor', 'doctor', 'x', ?)", (uid, DEPARTMENT))
    return {"id": uid, "name": "Assessment Doctor", "role": "doctor"}


async def test_a_clinician_can_approve_or_decline_the_draft(monkeypatch):
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))
    row = _one(patient)
    user = _mk_doctor()

    out = sas.transition(row["id"], _room(patient), "declined", row["version"],
                         "Disagree — this is fluid overload, not sepsis", user)
    assert out["state"] == "declined"

    with db_session() as db:
        events = [dict(r) for r in db.execute(
            "SELECT * FROM sage_assessment_events WHERE assessment_id = ? "
            "ORDER BY id", (row["id"],)).fetchall()]
    resolution = events[-1]
    assert resolution["new_state"] == "declined"
    assert resolution["actor_id"] == user["id"]
    assert "fluid overload" in resolution["reason"], (
        "THE DECLINE IS THE VALUABLE ROW and its reason was not kept")

    # …and on the TAMPER-EVIDENT chain, which is where an NABH reviewer or an
    # incident investigator looks. `sage_assessment_events` is operational
    # history; a governance record must be hash-chained.
    with db_session() as db:
        audit = [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE target_id = ? ORDER BY id", (row["id"],)).fetchall()]
    assert len(audit) == 1, "the clinician's verdict is not on the audit chain"
    assert audit[0]["action_type"] == "sage_assessment_declined"
    assert audit[0]["actor_user_id"] == user["id"]
    assert audit[0]["row_hash"] and audit[0]["prev_hash"] is not None, (
        "the audit row is not chained, so verify_audit_chain() cannot see it")
    assert json.loads(audit[0]["metadata_json"])["executes_nothing"] is True


async def test_a_draft_cannot_be_approved_twice_or_out_of_state(monkeypatch):
    """Optimistic locking, matching the sage_actions convention: run_db is a
    four-thread pool, so a read-then-write is resolved in SQL or not at all."""
    from fastapi import HTTPException

    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))
    row = _one(patient)
    user = _mk_doctor()

    sas.transition(row["id"], _room(patient), "approved", row["version"], None, user)
    with pytest.raises(HTTPException) as first:
        sas.transition(row["id"], _room(patient), "declined", row["version"], None, user)
    assert first.value.status_code in (400, 409)

    with pytest.raises(HTTPException) as bad:
        sas.transition(row["id"], _room(patient), "confirmed", 99, None, user)
    assert bad.value.status_code == 400


async def test_an_unavailable_draft_cannot_be_approved(monkeypatch):
    """There is nothing to agree with. Allowing it would put a clinical
    endorsement in the record for an assessment that was never written."""
    from fastapi import HTTPException
    from services.llm_queue_service import llm_queue

    async def _acquire(*a, **kw):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(llm_queue, "acquire", _acquire)
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))
    row = _one(patient)

    with pytest.raises(HTTPException) as exc:
        sas.transition(row["id"], _room(patient), "approved", row["version"], None,
                       _mk_doctor())
    assert exc.value.status_code == 400


async def test_approving_the_assessment_does_not_approve_its_actions(monkeypatch):
    """The one that keeps "I agree with this reasoning" from becoming a blanket
    authorisation for every drug it mentions."""
    from services import medication_safety_gate
    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"blocking": False, "max_severity": "none"})
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))
    row = _one(patient)

    sas.transition(row["id"], _room(patient), "approved", row["version"], None,
                   _mk_doctor())

    assert _cards(patient)[0]["state"] == "suggested", (
        "approving the assessment resolved its action cards — a drug was "
        "authorised by an acknowledgement")
    assert _charted_drugs(patient) == {"Warfarin"}, (
        "approving the reasoning put the proposed drug on the chart")


async def test_the_proposed_card_resolves_through_the_existing_card_route(monkeypatch):
    """The cards inherit the EXISTING mechanism — no parallel approve/decline.

    Driven through `relay_routes._transition_action`, the same body the HTTP
    endpoint calls, so the shadow pairing this proves is the production one.
    """
    from services import medication_safety_gate
    from routes.relay_routes import ActionTransitionRequest, _transition_action

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"blocking": False, "max_severity": "none"})
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))
    card = _cards(patient)[0]
    user = _mk_doctor()

    _transition_action(card["id"], _room(patient),
                       ActionTransitionRequest(state="dismissed", version=card["version"],
                                               reason="Already covered by the sepsis bundle"),
                       user)

    declined = _shadow(patient, "action.declined")
    proposed = _shadow(patient, "action.proposed")
    assert len(declined) == 1
    assert declined[0]["subject_id"] == proposed[0]["subject_id"] == card["id"], (
        "the resolution cannot be paired with the proposal — `agreement` stays blind")
    assert declined[0]["episode_id"] == proposed[0]["episode_id"]


# ═════════════════════════════════════════════════════════════════════════════
#  10. THE HTTP CONTRACT the UI is built against
# ═════════════════════════════════════════════════════════════════════════════
def _app():
    """ONLY the relay router — `main.app`'s lifespan would start the simulator,
    the shadow sweeper and the model warmups, and every assertion here depends on
    the request being the only thing that ran."""
    from fastapi import FastAPI
    from routes.relay_routes import router as relay_router

    app = FastAPI()
    app.include_router(relay_router)
    return app


def _auth(uid, role="doctor"):
    from auth import create_token
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


async def test_the_http_contract_fetches_and_resolves_a_draft(monkeypatch):
    """List, fetch, approve — the three calls the alert surface makes."""
    from fastapi.testclient import TestClient
    from services import medication_safety_gate

    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"blocking": False, "max_severity": "none"})
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))
    row = _one(patient)
    doctor = _mk_doctor()

    with TestClient(_app()) as client:
        listed = client.get(f"/api/relay/channels/{_room(patient)}/sage_assessments",
                            headers=_auth(doctor["id"]))
        assert listed.status_code == 200, listed.text
        assert [a["id"] for a in listed.json()] == [row["id"]]

        narrowed = client.get(
            f"/api/relay/channels/{_room(patient)}/sage_assessments",
            params={"message_id": row["message_id"]}, headers=_auth(doctor["id"]))
        assert [a["id"] for a in narrowed.json()] == [row["id"]]

        one = client.get(f"/api/relay/sage_assessments/{row['id']}",
                         headers=_auth(doctor["id"]))
        assert one.status_code == 200
        # Over the wire, the two tiers arrive as two distinct things and the
        # client is never asked to infer one from the other.
        assert one.json()["tier1"]["available"] is True
        assert one.json()["tier1"]["parameters"], "the drivers did not reach the UI"
        assert one.json()["considerations"]
        assert one.json()["advice_state"] == "ready"
        assert one.json()["disclaimer"]

        done = client.post(
            f"/api/relay/channels/{_room(patient)}/sage_assessments/{row['id']}/transition",
            json={"state": "approved", "version": row["version"], "reason": None},
            headers=_auth(doctor["id"]))
        assert done.status_code == 200, done.text
        assert done.json()["assessment"]["state"] == "approved"

    assert _cards(patient)[0]["state"] == "suggested", (
        "the HTTP approval resolved the action cards too")


async def test_a_clinician_outside_the_ward_cannot_read_a_draft(monkeypatch):
    """The assessment is a whole chart's reasoning. The id is not a bearer token."""
    from fastapi.testclient import TestClient

    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))
    row = _one(patient)

    outsider = f"ASSESSX{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'Other Ward Doctor', 'doctor', 'x', 'Cardiology')",
                   (outsider,))

    with TestClient(_app()) as client:
        assert client.get(f"/api/relay/sage_assessments/{row['id']}",
                          headers=_auth(outsider)).status_code == 403
        assert client.get(f"/api/relay/channels/{_room(patient)}/sage_assessments",
                          headers=_auth(outsider)).status_code == 403
        assert client.post(
            f"/api/relay/channels/{_room(patient)}/sage_assessments/{row['id']}/transition",
            json={"state": "approved", "version": row["version"]},
            headers=_auth(outsider)).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
#  11. THE LANE IT RUNS IN
# ═════════════════════════════════════════════════════════════════════════════
async def test_the_draft_never_takes_the_clinicians_interactive_lane(monkeypatch):
    """A machine-initiated assessment must not sit in front of a doctor's typed
    question. The interactive lane is ONE slot wide; taking it would reintroduce
    the 31-second queue wait the two-lane design was built to end."""
    from services.gpu_coordinator import PRIORITY_SAGE, PRIORITY_SCRIBE
    from services.llm_queue_service import llm_queue

    seen: list[int] = []
    real = llm_queue.acquire

    async def _spy(priority=2, timeout=120.0):
        seen.append(priority)
        return await real(priority=priority, timeout=timeout)

    monkeypatch.setattr(llm_queue, "acquire", _spy)
    patient = _mk_patient()
    await _generate(patient, monkeypatch, _FakeProvider(BRIEF_OUTPUT))

    assert seen == [PRIORITY_SCRIBE], seen
    assert PRIORITY_SAGE not in seen, (
        "the assessment took the reserved interactive lane")


async def test_the_assessment_can_be_switched_off(monkeypatch):
    """An operator kill switch, and it must leave no half-state behind."""
    monkeypatch.setenv("ORB_SAGE_ASSESSMENT_ENABLED", "false")
    patient = _mk_patient()
    assert await sas.generate_for_alert(
        patient, level="critical", score=0.9, drivers=[], message_id=_write_alert(patient),
        raw_news2=19, lane="manual") is None
    assert _assessments(patient) == []


# ═════════════════════════════════════════════════════════════════════════════
#  7. THE PIN IS ONE PHRASE, SHARED BY THREE LANES
# ═════════════════════════════════════════════════════════════════════════════
def test_the_never_recalculate_pin_is_worded_identically_on_both_prompts():
    """`_SYSTEM_BRIEF` was cut from 564 to 358 medgemma:27b tokens on 2026-08-15
    because it was 58% of every alert's prefill and nothing caches it. This is
    the assertion that says which words were NOT available to cut.

    THE SAME LITERAL STRING IS ASSERTED IN THREE PLACES ACROSS THREE FILES:
    `test_the_news2_in_the_prompt_is_the_deterministic_value` (above, fast lane),
    `test_the_fast_prompt_does_not_pay_for_what_tier1_already_says`
    (tests/test_assessment_tiers) and the wedge end-to-end run, which asserts
    "NEVER recalculate NEWS2" on the prompt the real alert path built. Reword it
    on one prompt and two other suites go red for a reason that reads like an
    unrelated failure — so the shared wording is pinned HERE, once, on both
    prompts at the same time.

    The rule itself is not negotiable in either tier: `_pin_news2` rewrites a
    disagreeing AGGREGATE in prose, but nothing after generation can police a
    wrong PER-PARAMETER decomposition, which is the "SpO2 80.1% — 4 points"
    defect this repo has already shipped once.
    """
    canonical = "NEVER recalculate NEWS2"
    assert canonical in sas._SYSTEM_BRIEF, "the fast lane lost the pin in a trim"
    assert canonical in sas._SYSTEM, "the careful lane lost the pin"
    for prompt in (sas._SYSTEM_BRIEF, sas._SYSTEM):
        assert "per-parameter points" in prompt.lower(), (
            "the aggregate is pinned but the decomposition is not — that is the "
            "exact shape of the defect the pin was written for")
        assert "AUTHORITATIVE" in prompt


def test_the_trimmed_fast_brief_did_not_borrow_its_savings_from_the_careful_one():
    """The trim was scoped to the ALERT lane. `_SYSTEM` still asks for four prose
    sections, cited sources and mandatory watch items, and it is reached from
    review surfaces where a clinician is reading rather than reacting — so its
    grounding, citation and watch-item rules are not prefill to be reclaimed."""
    assert "GROUNDING" in sas._SYSTEM
    assert 'RULES FOR "watch_items"' in sas._SYSTEM
    assert 'RULES FOR "cited_sources"' in sas._SYSTEM
    assert "<<<BEGIN_CHART>>>" in sas._SYSTEM
    # And the fast brief still does NOT carry any of that weight.
    assert "watch_items" not in sas._SYSTEM_BRIEF
    assert "cited_sources" not in sas._SYSTEM_BRIEF
