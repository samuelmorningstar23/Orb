"""Orb — THE WEDGE, END TO END, IN ONE PASS.

Orb does not sell agents. It sells a loop: Vigil detects deterioration, Sage
reasons over it, Relay puts a DRAFTED ACTION in front of the right clinician.
The 2026-08-14 verification found every seam individually correct and the loop
broken, because nothing tested them together. That is the gap this file exists
to close, and it is the only reason it is a separate file from
``test_wedge_loop.py`` (Vigil→Relay) and ``test_sage_assessment.py``
(the generator's own properties): both of those assert PIECES.

WHAT IS DRIVEN HERE, in one uninterrupted pass per lane:

    abnormal vitals arrive
      → Vigil scores and fires
      → the alert is persisted and is READABLE by the right clinician
      → Sage drafts the assessment
      → its proposals appear as `sage_actions` cards
      → a clinician approves one and declines another
      → every stage lands in the shadow log ON ONE EPISODE

…for BOTH lanes that can raise a critical alert on the ward:

  * MANUAL — ``POST /api/nurse/patients/{id}/observations``. The lane the shadow
    pilot goes live on, and the lane the instrument was blind to until
    2026-08-14. Driven through the HTTP route, in a context-managed TestClient,
    because half of the original defect was a MISSING CALL and a test that calls
    the service directly cannot see a missing call.
  * SIMULATOR — ``bot_service.evaluate_and_dispatch_alerts``. The demo lane, and
    the one a champion will actually watch.

READ THROUGH THE ROUTES, NOT THROUGH THE TABLES. Every "the clinician can see
it" assertion goes through the real Relay endpoints under a real token, because
that is the only way to prove the alert and its draft are visible to the person
who is supposed to see them AND invisible to the person who is not.

THE PROPERTIES THAT MATTER (each is a safety or commercial invariant, not a
feature):

  1. THE ALERT NEVER WAITS. Vitals-in to clinician-visible alert is 51 ms and
     that number IS the product. The alert must be readable through the route
     while generation is still blocked.
  2. A SHED OR FAILED DRAFT LEAVES THE ALERT STANDING, and says so honestly —
     never a spinner implying something is coming when nothing is.
  3. ORB'S ARITHMETIC WINS. The NEWS2 on the assessment equals the deterministic
     engine's value for the observation that fired the alert. A 2026-07-31
     defect had the model answering 7 where Orb computed 11.
  4. AN APPROVED ASSESSMENT EXECUTES NOTHING BY ITSELF.
  5. A DECLINED DRAFT IS RECORDED WITH ITS REASON — the disagreements are the
     informative rows.
  6. THE SHADOW EPISODE CARRIES THE WHOLE CHAIN, and the scorecard's `agreement`
     metric can pair each proposal with its resolution.

WHAT IS ASSERTED is always a row in a table or a value on an HTTP response —
never a log line and never a return value. ``shadow_service`` swallows its own
exceptions by design, so a test that trusts a 200 can happily measure an empty
instrument.

WHY THE TEST CLIENT IS ALWAYS CONTEXT-MANAGED. Starlette tears the blocking
portal down when a non-context TestClient finishes a request, so the assessment
task the request scheduled on the loop would be cancelled before it ever ran.
`with TestClient(...)` keeps one portal alive across the block, which is also
the production shape: a long-lived loop the request hands work back to.
"""
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrations  # noqa: E402
from auth import create_token  # noqa: E402
from database import db_session  # noqa: E402
from services import sage_assessment_service as sas  # noqa: E402
from services import shadow_service  # noqa: E402
from services.vigil_service import compute_news2  # noqa: E402

DEPARTMENT = "Wedge E2E Medicine"

# NEWS2 19 — the dry run's peri-arrest picture: RR 32 (3), SpO2 86 (3), on O2
# (2), Temp 39.4 (2), SBP 78 (3), HR 138 (3), ACVPU voice (3).
PERI_ARREST = {
    "heart_rate": 138, "systolic_bp": 78, "diastolic_bp": 44,
    "respiratory_rate": 32, "temperature": 39.4, "spo2": 86,
    "consciousness": "voice", "on_oxygen": True,
}

def _brief(steps, cite=None) -> str:
    """The FAST LANE's wire format: one step per line, four fields.

    The alert lane stopped asking for four prose sections on 2026-08-14: it was
    measured at a median 46 s end to end and judged useless at a deteriorating
    bedside. What the alert now schedules is Tier 1 — the NEWS2 drivers and
    trend, deterministic, no model — plus at most three capped next steps in
    this format, streamed a step at a time. JSON braces and key names are output
    tokens, and output tokens are seconds at 10-15 tok/s.
    """
    lines = [" | ".join([step, why or "-", action or "-", target or "-"])
             for step, why, action, target in steps]
    if cite:
        lines.append("CITE: " + ", ".join(cite))
    return "\n".join(lines)


#: The model's answer. TWO cardable steps on purpose — the brief is "a clinician
#: approves one and DECLINES another", which needs two cards in one draft, and
#: two cards is also `_max_actions()`'s default ceiling so the wall stays bounded.
#:
#: EVERY LINE NAMES A FACT `_mk_patient` CHARTED, because Tier 2's post-filter
#: deletes a line that names none (unsourced), one that names a drug the
#: pre-screen never supplied (fabricated), and one that would be true of any
#: patient with these numbers (generic). "Escalate to the medical registrar now"
#: was the third step here and is now deleted by design — it is verbatim the
#: padding that made the old tier decoration.
#: KEPT SHORT ON PURPOSE. The fast lane caps its own output at
#: `_max_advice_tokens()` (~96 tok / 384 chars) and closes the stream there, so a
#: verbose fixture silently loses its LAST step to the cap and the card-wall
#: assertions below start measuring the truncation instead of the loop.
TWO_CARD_STEPS = [
    ("Sulfonamide allergy charted — first-line cover is out",
     "documented allergy, infective picture", "start_medication", "Ceftriaxone"),
    ("Warfarin charted — reverse before theatre", "anticoagulated",
     "transfer_to_surgery", "Emergency laparotomy"),
    ("Warfarin with SBP 78 — consider occult bleeding",
     "anticoagulated and hypotensive", None, None),
]
TWO_CARD_OUTPUT = _brief(TWO_CARD_STEPS)

SOURCES = [
    {"document_id": "LOC-SEPSIS-001", "title": "Sepsis Six — local policy",
     "source": "Hospital policy", "trust_tier": 1,
     "content": "Administer ceftriaxone within one hour of recognising sepsis. "
                "Measure serum lactate and take blood cultures before antibiotics."},
]


# ═════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═════════════════════════════════════════════════════════════════════════════
_CREATED: list[str] = []


@pytest.fixture(autouse=True)
def _fresh_instrument(monkeypatch):
    """A clean shadow table per test, an empty episode registry, no in-flight drafts.

    The episode registry is PROCESS-LOCAL and survives a table drop, so without
    the reset a test could resolve an episode id minted by an earlier one and
    every "one episode" assertion would be measuring the wrong test.
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


def _cleanup_patients() -> None:
    """Discharge and delete this file's fixture patients.

    NOT tidiness. These patients are `status='admitted'` with a deliberately
    worsening vitals series, and the suite shares ONE database across every
    file. `world_model_service.compute_ward_state` scans every admitted patient
    and returns `worsening[:10]`, so leaving synthetic deteriorating patients
    behind fails tests in modules this file never touches — which is exactly how
    test_sage_assessment's own fixtures broke test_world_model.

    Children before parents. `audit_logs` is deliberately NOT touched: it is an
    append-only hash chain and deleting from it would break verify_audit_chain()
    for the whole session.
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
        db.execute(f"DELETE FROM sage_handoff_events WHERE handoff_id IN "
                   f"(SELECT id FROM sage_handoffs WHERE patient_id IN ({marks}))", ids)
        db.execute(f"DELETE FROM sage_handoffs WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM sage_action_events WHERE action_id IN "
                   f"(SELECT id FROM sage_actions WHERE patient_id IN ({marks}))", ids)
        db.execute(f"DELETE FROM sage_actions WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM messages WHERE channel_id IN "
                   f"(SELECT id FROM channels WHERE patient_id IN ({marks}))", ids)
        db.execute(f"DELETE FROM channels WHERE patient_id IN ({marks})", ids)
        for table, col in _CHILD_TABLES:
            try:
                db.execute(f"DELETE FROM {table} WHERE {col} IN ({marks})", ids)
            except Exception:
                pass
    # Separate transaction, so a table this list has not learned about yet costs
    # the DELETE and nothing else. THE DISCHARGE IS THE LOAD-BEARING PART: what
    # broke test_world_model was synthetic patients still `status='admitted'`,
    # and that is guaranteed here whether or not the row itself can be removed.
    with db_session() as db:
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", ids)
    try:
        with db_session() as db:
            db.execute(f"DELETE FROM patients WHERE id IN ({marks})", ids)
    except Exception:
        pass


#: Everything with a foreign key to `patients` that this file's routes can
#: write. Children before parents.
_CHILD_TABLES: tuple[tuple[str, str], ...] = (
    ("observations", "patient_id"), ("vitals", "patient_id"),
    ("risk_scores", "patient_id"), ("medication_administrations", "patient_id"),
    ("medications", "patient_id"), ("orders", "patient_id"),
    ("care_bundles", "patient_id"), ("clinical_escalations", "patient_id"),
    ("sage_decision_cache", "patient_id"), ("patient_snapshots", "patient_id"),
    ("forecast_predictions", "patient_id"), ("lens_analyses", "patient_id"),
    ("problem_list", "patient_id"), ("patient_allergies", "patient_id"),
    ("patient_profiles", "patient_id"), ("message_room_fit_flags", "patient_id"),
    ("order_set_applications", "patient_id"), ("surgeries", "patient_id"),
    ("surgery_prep_requests", "patient_id"), ("appointments", "patient_id"),
    ("admissions", "patient_id"), ("world_model_state", "entity_id"),
)


def _rebuild_shadow_table() -> None:
    with db_session() as conn:
        conn.execute("DROP VIEW IF EXISTS shadow_events_current")
        conn.execute("DROP TABLE IF EXISTS shadow_events")
        migrations._m4_shadow_events(conn)
        migrations._m18_shadow_none_confirmed_once(conn)


@pytest.fixture(autouse=True)
def _formulary():
    """A one-drug formulary.

    The reconciler is REAL here on purpose: a `start_medication` card is only
    offered for something Orb can actually order, and that check runs against
    `helix_medication_master`. `init_db` does not create the Helix tables
    (main.py's lifespan calls `init_helix_db`), so the suite builds them rather
    than patching the reconciler out and losing the property.
    """
    from services.helix_db import HELIX_SCHEMA
    with db_session() as db:
        db.executescript(HELIX_SCHEMA)
        db.execute(
            "INSERT OR IGNORE INTO helix_medication_master (id, name, generic_name, "
            "form, strength) VALUES ('MED-CEF', 'Ceftriaxone', 'Ceftriaxone sodium', "
            "'Injection', '1 g')")
    yield


# ═════════════════════════════════════════════════════════════════════════════
#  The world
# ═════════════════════════════════════════════════════════════════════════════
def _mk_patient(*, observations: int = 4) -> str:
    """An admitted, deteriorating patient in DEPARTMENT with a Relay case room.

    The department lives on the ADMISSION — `patients.ward` is a bed label and is
    not what Relay's access rule reads.
    """
    pid = f"E2EP{uuid.uuid4().hex[:10].upper()}"
    _CREATED.append(pid)
    now = datetime.now(timezone.utc)
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) "
                   "VALUES ('E2EADM', 'E2E Admitter', 'doctor', 'x')")
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date)
               VALUES (?, ?, ?, 'E-1', 'E2E-1', 71, 'F', 'Community acquired pneumonia',
                       'admitted', ?)""",
            (pid, f"E2E {pid}", f"MRN-{uuid.uuid4().hex[:8].upper()}", now.isoformat()))
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, 'E2E-1', 'E-1', 'active', 'E2EADM')", (pid, DEPARTMENT))
        # Oldest first, worsening — so "the trend across recent observations" is a
        # real trend and not four copies of one snapshot.
        for i in range(observations):
            drift = observations - 1 - i
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                       diastolic_bp, spo2, temperature, respiratory_rate,
                                       consciousness, on_oxygen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pid, (now - timedelta(minutes=5 * (drift + 1))).isoformat(),
                 PERI_ARREST["heart_rate"] - 4 * drift,
                 PERI_ARREST["systolic_bp"] + 6 * drift,
                 PERI_ARREST["diastolic_bp"],
                 PERI_ARREST["spo2"] + 2 * drift,
                 PERI_ARREST["temperature"],
                 PERI_ARREST["respiratory_rate"] - 2 * drift,
                 PERI_ARREST["consciousness"], 1))
        # ── THE CHART FACTS THAT MAKE TIER 2 A REAL GENERATION ───────────────
        # Since 2026-08-15 the advice tier says only what THIS patient's chart
        # implies, and when the deterministic pre-screen finds nothing it
        # answers "no patient-specific factors identified" WITHOUT CALLING A
        # MODEL. A chartless patient therefore never reaches the model at all,
        # and every loop-closing assertion below — the cards, the approve /
        # decline pair, the shed and timeout paths, the NEWS2 pin — would be
        # measuring a draft that was never generated.
        #
        #   WARFARIN + SBP 78     -> `anticoagulated_in_this_picture`
        #   SULFA ALLERGY + T 39.4 -> `antimicrobial_allergy_in_an_infective_picture`
        #
        # Sulfonamides rather than penicillin so the REAL allergy interlock does
        # not block the Ceftriaxone these tests card — the drug-safety gate is
        # exercised here, and a blocked drug would silently remove the card the
        # whole end-to-end loop is built on.
        db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, "
            "frequency, status, prescribed_by) VALUES (?, 'Warfarin', '5 mg', "
            "'oral', 'OD', 'active', 'E2EADM')", (pid,))
        db.execute(
            "INSERT INTO patient_allergies (patient_id, substance, reaction, "
            "severity, status, recorded_by) VALUES (?, 'Sulfonamides', 'Rash', "
            "'severe', 'active', 'E2EADM')", (pid,))
        db.execute(
            "INSERT OR REPLACE INTO channels (id, name, type, patient_id) "
            "VALUES (?, ?, 'case_room', ?)", (_room(pid), f"E2E Case Room {pid}", pid))
    return pid


def _room(pid: str) -> str:
    return f"e2e-room-{pid}"


def _mk_nurse() -> str:
    uid = f"E2EN{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'E2E Nurse', 'nurse', 'x', ?)", (uid, DEPARTMENT))
    return uid


def _mk_doctor(department: str = DEPARTMENT) -> dict:
    uid = f"E2ED{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department) "
                   "VALUES (?, 'E2E Registrar', 'doctor', 'x', ?)", (uid, department))
    return {"id": uid, "name": "E2E Registrar", "role": "doctor"}


def _news2(vitals: dict) -> dict:
    return compute_news2(vitals["heart_rate"], vitals["systolic_bp"],
                         vitals["respiratory_rate"], vitals["temperature"],
                         vitals["spo2"], vitals["consciousness"], vitals["on_oxygen"])


ENGINE_NEWS2 = _news2(PERI_ARREST)["raw_news2"]


# ═════════════════════════════════════════════════════════════════════════════
#  The app under test — the REAL route surface for both halves of the loop
# ═════════════════════════════════════════════════════════════════════════════
def _app():
    """Nurse router + Relay router, and nothing else.

    Not `main.app`: its lifespan starts the simulator, the shadow sweeper, the
    escalation sweep and the model warmups, and every assertion here depends on
    the request being the only thing that ran. Both routers in ONE app because
    the whole point of this file is that the nurse's write and the registrar's
    read are the same pass.
    """
    from fastapi import FastAPI
    from routes.nurse_routes import router as nurse_router
    from routes.relay_routes import router as relay_router

    app = FastAPI()
    app.include_router(nurse_router)
    app.include_router(relay_router)
    return app


def _auth(uid: str, role: str = "doctor") -> dict:
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


# ═════════════════════════════════════════════════════════════════════════════
#  The fake model lane
# ═════════════════════════════════════════════════════════════════════════════
class _FakeProvider:
    """A stand-in for `llm_providers.OllamaChatProvider`.

    `gate` is a THREADING event, not an asyncio one, and awaited through the
    default executor. The manual lane's draft runs on the TestClient's portal
    loop in another thread while the test body runs on the main thread; an
    `asyncio.Event` set across that boundary is not thread-safe, and the whole
    ordering property this file exists to prove would then rest on a race.
    """

    def __init__(self, output, *, gate: threading.Event | None = None,
                 raises: BaseException | None = None):
        self._output = output
        self._gate = gate
        self._raises = raises
        self.messages: list = []
        self.calls = 0

    async def astream_chat(self, messages, options=None):
        self.calls += 1
        self.messages.append(messages)
        if self._gate is not None:
            # Bounded, so a defect in the test cannot hang the suite: a gate that
            # is never opened becomes a failed assertion downstream, not a stall.
            await asyncio.get_running_loop().run_in_executor(
                None, self._gate.wait, 30.0)
        if self._raises is not None:
            raise self._raises
        text = self._output if isinstance(self._output, str) else json.dumps(self._output)
        for i in range(0, len(text), 64):
            yield text[i:i + 64]

    @property
    def prompt(self) -> str:
        return "\n".join(m["content"] for m in self.messages[0])


def _arrange(monkeypatch, provider, *, sources=None, silence_bus=True):
    """Wire the fake model, the fake retrieval and the tier selection.

    The queue, the sanitizer, the grounding/confidence classifiers, the drug
    safety gate, the shadow instrument, the routes and every DB path are REAL —
    those are the parts under test.

    The EVENT BUS is silenced by default. It is the nervous-system accelerator
    (prewarm, care bundles), not the wedge; `test_wedge_loop` already holds that
    it fires on both lanes, and letting it run here would drag model warmups and
    the bundle policy into assertions about Sage's draft. Note the ordering this
    depends on: `bot_service._schedule_on_loop` schedules the ASSESSMENT before
    it publishes, so silencing the bus cannot silence the draft.
    """
    from services import llm_providers, model_router, sage_service
    from services.model_router import ModelChoice
    from config import LLM_MODEL

    sources = SOURCES if sources is None else sources
    text = "\n".join(f"(DOC: {s['document_id']}) {s['title']}\n{s['content']}"
                     for s in sources)

    def _fake_retrieval(query, warm_candidates=None, limit=None):
        return (text if sources else "", list(sources), "high")

    async def _fake_select(preferred=None, fallback=None):
        # NEVER downgraded: the careful tier is a precondition of the assessment
        # existing at all, and `test_sage_assessment` holds the eviction path.
        return ModelChoice(LLM_MODEL, False, LLM_MODEL)

    monkeypatch.setattr(sage_service, "_get_guideline_context", _fake_retrieval)
    monkeypatch.setattr(model_router, "select_chat_model", _fake_select)
    monkeypatch.setattr(llm_providers, "provider_for_model", lambda model: provider)

    if silence_bus:
        from services import event_bus

        async def _no_publish(evt):
            return None

        monkeypatch.setattr(event_bus.event_bus, "publish", _no_publish)
    return provider


def _allow_medications(monkeypatch):
    """The drug-safety gate says yes.

    Patched only where the assertion is about the LOOP rather than about the
    gate: the fixture patient has no allergies and no active meds, so the real
    gate's verdict depends on interaction-database contents this file does not
    own. `test_sage_assessment` holds the gate's own properties, including that
    a blocked drug never becomes a card.
    """
    from services import medication_safety_gate
    monkeypatch.setattr(medication_safety_gate, "enforce_medication_safety",
                        lambda *a, **kw: {"blocking": False, "max_severity": "none"})


async def _drain(timeout: float = 15.0):
    """Wait for every scheduled draft on THIS loop to finish."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pending = [t for t in list(sas._inflight) if not t.done()]
        if not pending:
            return
        if loop.time() > deadline:
            raise AssertionError("assessment tasks did not finish")
        await asyncio.wait(pending, timeout=max(0.05, deadline - loop.time()))


def _await_assessment(patient_id: str, timeout: float = 20.0) -> dict:
    """Poll the DB until the draft has RESOLVED, from a thread that is not the
    loop's.

    A poll is the honest shape for the manual lane: the alert path is
    deliberately fire-and-forget, so a synchronous wait would only pass on an
    implementation that made the nurse wait for the 27B.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _assessments(patient_id)
        if rows and rows[0]["state"] != sas.STATE_GENERATING:
            return rows[0]
        time.sleep(0.02)
    rows = _assessments(patient_id)
    raise AssertionError(
        f"no assessment resolved for {patient_id} within {timeout}s: {rows}")


# ═════════════════════════════════════════════════════════════════════════════
#  Readers
# ═════════════════════════════════════════════════════════════════════════════
def _assessments(patient_id) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM sage_assessments WHERE patient_id = ? ORDER BY created_at",
            (patient_id,)).fetchall()]


def _cards(patient_id) -> list[dict]:
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM sage_actions WHERE patient_id = ? ORDER BY created_at, id",
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
    with db_session() as db:
        row = db.execute(
            "SELECT m.* FROM messages m JOIN channels c ON c.id = m.channel_id "
            "WHERE c.patient_id = ? AND m.message_type = 'alert' "
            "ORDER BY m.id DESC LIMIT 1", (patient_id,)).fetchone()
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


# ═════════════════════════════════════════════════════════════════════════════
#  The chain assertion, shared by both lanes
# ═════════════════════════════════════════════════════════════════════════════
def _assert_one_coherent_episode(patient: str, lane: str) -> str:
    """Every stage of the loop, on ONE episode, in causal order.

    THIS IS THE ASSERTION THE 2026-08-14 VERIFICATION DID NOT HAVE. Each seam
    was individually correct; nothing checked that detection, reasoning,
    delivery and the clinician's verdict were the same chain. Without a shared
    episode_id there is no table in which both halves appear and no join that
    could reconstruct it afterwards — `messages` has no patient_id, no level and
    no FK to the vital that fired, and correlation_id overlap between audit_logs
    and event_log is literally zero rows.
    """
    rows = _shadow(patient)
    assert rows, "the loop ran and the instrument recorded nothing"
    episodes = {r["episode_id"] for r in rows}
    assert len(episodes) == 1, (
        f"the loop fragmented across {len(episodes)} episodes; nothing downstream "
        f"can pair detection with the clinician's verdict: "
        f"{[(r['kind'], r['episode_id']) for r in rows]}")

    kinds = [r["kind"] for r in rows]

    # ── EVERY STAGE IS PRESENT ───────────────────────────────────────────────
    for required in ("episode.opened", "alert.fired", "alert.persisted",
                     "action.proposed", "action.approved", "action.declined"):
        assert required in kinds, (
            f"the {lane} lane's chain is missing {required}: {kinds}")

    # THE ASSESSMENT'S OWN sage.drafted, picked by query_class.
    #
    # There may be MORE than one `sage.drafted` on this episode: the simulator
    # dispatcher still appends its one-sentence 4B garnish to the alert text and
    # instruments it as a draft of its own (`VIGIL_ALERT_RECOMMENDATION`, held by
    # test_wedge_loop). Selecting by class rather than by count is what keeps
    # this assertion about the ASSESSMENT and not about whichever row came first.
    drafted = [r for r in rows if r["kind"] == "sage.drafted"
               and _payload(r).get("query_class") == sas.QUERY_CLASS_BRIEF]
    assert len(drafted) == 1, (
        f"expected exactly one assessment draft on the episode, got {len(drafted)}")
    proposed = [r for r in rows if r["kind"] == "action.proposed"]
    assert len(proposed) == 2, f"expected two proposals, got {len(proposed)}"

    # ── CAUSAL ORDER, by insertion id (the instrument writes in order and
    #    occurred_at is only millisecond-resolution) ──────────────────────────
    fired = next(r for r in rows if r["kind"] == "alert.fired")
    persisted = next(r for r in rows if r["kind"] == "alert.persisted")
    resolutions = [r for r in rows if r["kind"] in ("action.approved", "action.declined")]
    assert fired["id"] < persisted["id"] < drafted[0]["id"], (
        f"the alert was not detected and delivered BEFORE Sage drafted: {kinds}")
    assert all(drafted[0]["id"] < p["id"] for p in proposed), (
        "a proposal is recorded before the draft that made it")
    assert all(max(p["id"] for p in proposed) < r["id"] for r in resolutions), (
        "a clinician resolved a card before it was proposed")
    assert fired["occurred_at"] <= drafted[0]["occurred_at"], (
        "Sage's draft is stamped BEFORE the alert that caused it")

    # ── THE ALERT'S OWN LANE IS THE LANE IT WAS RAISED ON ────────────────────
    # A manual alert filed under another lane leaves the scorecard exactly as
    # blind to bedside entry as no row at all — 2026-08-14's Defect A.
    for row in rows:
        if row["kind"] in ("episode.opened", "alert.fired", "alert.persisted",
                           "alert.delivered", "sage.drafted", "action.proposed"):
            assert row["lane"] == lane, (
                f"{row['kind']} was recorded under lane {row['lane']!r}, not {lane!r}")

    return episodes.pop()


def _assert_proposals_pair_with_resolutions(patient: str) -> None:
    """The scorecard's `agreement` metric matches a proposal to a resolution on
    (subject_type, subject_id). Without that pair a resolution is an orphan and
    the one metric that would show whether clinicians trust Sage stays pinned at
    insufficient_data however many alerts fire."""
    proposed = _shadow(patient, "action.proposed")
    approved = _shadow(patient, "action.approved")
    declined = _shadow(patient, "action.declined")
    assert len(proposed) == 2 and len(approved) == 1 and len(declined) == 1

    card_ids = {c["id"] for c in _cards(patient)}
    assert {p["subject_id"] for p in proposed} == card_ids
    for row in approved + declined:
        assert row["subject_type"] == "sage_actions"
        assert row["subject_id"] in card_ids, (
            "the clinician's verdict does not carry the card id — `agreement` "
            "cannot pair it with the proposal")
    assert approved[0]["subject_id"] != declined[0]["subject_id"], (
        "the approval and the decline resolved the SAME card")
    assert _payload(declined[0])["decline_reason"], (
        "THE DECLINE IS THE VALUABLE ROW and its reason was not kept")


# ═════════════════════════════════════════════════════════════════════════════
#  1. THE MANUAL LANE — the lane the pilot goes live on
# ═════════════════════════════════════════════════════════════════════════════
def test_the_manual_lane_closes_the_whole_loop_in_one_pass(monkeypatch):
    """Nurse charts a NEWS2 19 → registrar reads the alert, then the draft, then
    approves one proposal and declines another. One pass, one episode.

    Driven entirely through HTTP. Every earlier wedge defect was invisible from
    inside a module: a missing call, a lane string, an episode that was never
    threaded. Only the route surface can see those.
    """
    from fastapi.testclient import TestClient

    _allow_medications(monkeypatch)
    provider = _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))
    nurse, patient, doctor = _mk_nurse(), _mk_patient(), _mk_doctor()
    room = _room(patient)

    with TestClient(_app()) as client:
        # ── VIGIL: abnormal vitals arrive at the bedside ─────────────────────
        charted = client.post(f"/api/nurse/patients/{patient}/observations",
                              json=dict(PERI_ARREST), headers=_auth(nurse, "nurse"))
        assert charted.status_code == 200, charted.text
        assert charted.json()["news2"]["raw_news2"] == ENGINE_NEWS2 == 19

        # ── RELAY: the alert is on the registrar's screen, read through the
        #    real endpoint under the registrar's own token ──────────────────
        messages = client.get(f"/api/relay/channels/{room}/messages",
                              headers=_auth(doctor["id"]))
        assert messages.status_code == 200, messages.text
        alerts = [m for m in messages.json() if m.get("message_type") == "alert"]
        assert len(alerts) == 1, f"the registrar cannot see the alert: {messages.json()}"
        assert "VIGIL ALERT" in alerts[0]["content"]
        alert_id = alerts[0]["id"]

        # ── SAGE: the draft arrives AFTER, on its own ────────────────────────
        row = _await_assessment(patient)
        assert row["state"] == sas.STATE_READY, row["unavailable_reason"]
        assert row["message_id"] == alert_id, (
            "the draft is not tied to the alert the registrar is reading")

        listed = client.get(f"/api/relay/channels/{room}/sage_assessments",
                            params={"message_id": alert_id}, headers=_auth(doctor["id"]))
        assert listed.status_code == 200, listed.text
        drafts = listed.json()
        assert len(drafts) == 1
        draft = drafts[0]

        # BOTH TIERS, and the regulatory posture on every payload. Tier 1 is the
        # deterministic driver/trend block — it was on the row before the model
        # was asked for anything — and Tier 2 is the capped list of next steps.
        assert draft["has_tier1"] is True and draft["tier1"]["available"] is True
        assert draft["tier1"]["parameters"] and draft["tier1"]["headline"]
        assert draft["tier1"]["news2"] == ENGINE_NEWS2 == 19
        assert draft["considerations"]
        assert draft["citation_basis"]
        assert draft["watch_items"]
        assert "approval" in draft["disclaimer"].lower()
        # No prose interpretation on the alert lane: Tier 1 carries it, and
        # asking the model to restate it in sentences is what made the first
        # version take 46 seconds.
        assert draft["whats_happening"] is None

        # ORB'S ARITHMETIC WINS — the draft's NEWS2 is the engine's, for the
        # very observation that fired the alert.
        assert draft["news2"] == ENGINE_NEWS2 == 19
        assert "AUTHORITATIVE" in provider.prompt
        assert f"NEWS2 {ENGINE_NEWS2} (critical)" in provider.prompt
        assert "NEVER recalculate NEWS2" in provider.prompt

        # ── RELAY: the proposals are real cards, still unresolved ────────────
        carded = client.get(f"/api/relay/channels/{room}/sage_actions",
                            headers=_auth(doctor["id"]))
        assert carded.status_code == 200, carded.text
        cards = sorted(carded.json(), key=lambda c: c["action_type"])
        assert [c["action_type"] for c in cards] == ["start_medication", "transfer_to_surgery"]
        assert all(c["state"] == "suggested" for c in cards)
        assert not _executed_anything(patient), (
            "something executed before any clinician clicked")

        # ── THE CLINICIAN: approve one, decline the other ────────────────────
        approve = client.post(
            f"/api/relay/channels/{room}/sage_actions/{cards[0]['id']}/transition",
            json={"state": "confirmed", "version": cards[0]["version"], "reason": None},
            headers=_auth(doctor["id"]))
        assert approve.status_code == 200, approve.text

        decline = client.post(
            f"/api/relay/channels/{room}/sage_actions/{cards[1]['id']}/transition",
            json={"state": "dismissed", "version": cards[1]["version"],
                  "reason": "No surgical source — this is pneumonia, not perforation"},
            headers=_auth(doctor["id"]))
        assert decline.status_code == 200, decline.text

        # ── …and record a verdict on the REASONING itself ────────────────────
        verdict = client.post(
            f"/api/relay/channels/{room}/sage_assessments/{draft['id']}/transition",
            json={"state": "approved", "version": draft["version"], "reason": None},
            headers=_auth(doctor["id"]))
        assert verdict.status_code == 200, verdict.text
        assert verdict.json()["assessment"]["state"] == "approved"

    # ── THE CHAIN ────────────────────────────────────────────────────────────
    _assert_one_coherent_episode(patient, "manual")
    _assert_proposals_pair_with_resolutions(patient)


def _executed_anything(patient_id: str) -> bool:
    """Has anything downstream of a card actually happened for this patient?

    Deliberately reads the CLINICAL tables rather than the card state: "nothing
    executes" is a claim about the chart and the pharmacy, not about a status
    column Orb controls.

    `_mk_patient` charts Warfarin so the pre-screen has a fact to hand the model,
    so the question is no longer "is the medications table empty" but "did
    anything NEW arrive on it". Excluded by name, and only the fixture's own
    baseline is excluded — a card that actually executed would write Ceftriaxone
    and would still be caught.
    """
    with db_session() as db:
        meds = db.execute(
            "SELECT COUNT(*) AS n FROM medications WHERE patient_id = ? "
            "AND drug_name != 'Warfarin'", (patient_id,)).fetchone()["n"]
        handoffs = db.execute("SELECT COUNT(*) AS n FROM sage_handoffs WHERE patient_id = ?",
                              (patient_id,)).fetchone()["n"]
    return bool(meds or handoffs)


# ═════════════════════════════════════════════════════════════════════════════
#  2. THE SIMULATOR LANE — the one a champion actually watches
# ═════════════════════════════════════════════════════════════════════════════
async def test_the_simulator_lane_closes_the_whole_loop_in_one_pass(monkeypatch):
    """The same chain, driven from `evaluate_and_dispatch_alerts`.

    Both lanes are asserted because the ORIGINAL defect was lane-shaped: Sage's
    only contribution existed on the simulator lane and for nobody the pilot
    would run on. A file that proves the loop on one lane proves exactly the
    thing that was already true.
    """
    from fastapi.testclient import TestClient
    from services import bot_service

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))

    async def _rec(vitals_summary, risk_level, drivers):
        return "Escalate to the registrar."

    monkeypatch.setattr(bot_service, "generate_ai_recommendation", _rec)

    patient, doctor = _mk_patient(), _mk_doctor()
    room = _room(patient)

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])
    await _drain()

    rows = _assessments(patient)
    assert len(rows) == 1 and rows[0]["state"] == sas.STATE_READY, rows

    with TestClient(_app()) as client:
        messages = client.get(f"/api/relay/channels/{room}/messages",
                              headers=_auth(doctor["id"]))
        alerts = [m for m in messages.json() if m.get("message_type") == "alert"]
        assert len(alerts) == 1, "the simulator alert never reached the case room"

        draft = client.get(f"/api/relay/channels/{room}/sage_assessments",
                           params={"message_id": alerts[0]["id"]},
                           headers=_auth(doctor["id"])).json()[0]
        assert draft["state"] == "ready"
        assert draft["news2"] == ENGINE_NEWS2 == 19

        cards = sorted(client.get(f"/api/relay/channels/{room}/sage_actions",
                                  headers=_auth(doctor["id"])).json(),
                       key=lambda c: c["action_type"])
        assert len(cards) == 2

        assert client.post(
            f"/api/relay/channels/{room}/sage_actions/{cards[0]['id']}/transition",
            json={"state": "confirmed", "version": cards[0]["version"]},
            headers=_auth(doctor["id"])).status_code == 200
        assert client.post(
            f"/api/relay/channels/{room}/sage_actions/{cards[1]['id']}/transition",
            json={"state": "dismissed", "version": cards[1]["version"],
                  "reason": "Not a surgical abdomen"},
            headers=_auth(doctor["id"])).status_code == 200

    _assert_one_coherent_episode(patient, "simulator")
    _assert_proposals_pair_with_resolutions(patient)


# ═════════════════════════════════════════════════════════════════════════════
#  3. THE ALERT NEVER WAITS  —  the 51 ms number is the product
# ═════════════════════════════════════════════════════════════════════════════
def test_the_alert_is_readable_by_the_clinician_before_the_assessment_exists(monkeypatch):
    """PROVE-CAN-FAIL TARGET.

    Generation is held open. The nurse's POST must return, and the registrar
    must be able to READ the alert through the Relay endpoint, while the model
    has not produced a token. If someone ever awaits generation on the alert path
    — the natural, tidy-looking refactor — this test does not merely fail, the
    POST blocks and the client timeout turns that into a readable failure.

    Asserted through the route rather than on the `messages` table because
    "persisted" and "visible to the right clinician" are different claims, and
    the product claim is the second one.
    """
    from fastapi.testclient import TestClient

    gate = threading.Event()
    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT, gate=gate))
    nurse, patient, doctor = _mk_nurse(), _mk_patient(), _mk_doctor()
    room = _room(patient)

    try:
        with TestClient(_app()) as client:
            t0 = time.monotonic()
            charted = client.post(f"/api/nurse/patients/{patient}/observations",
                                  json=dict(PERI_ARREST), headers=_auth(nurse, "nurse"))
            charted_ms = (time.monotonic() - t0) * 1000
            assert charted.status_code == 200, charted.text

            alerts = [m for m in client.get(f"/api/relay/channels/{room}/messages",
                                            headers=_auth(doctor["id"])).json()
                      if m.get("message_type") == "alert"]
            assert len(alerts) == 1, (
                "the alert was not readable while the model was still blocked — "
                "the clinician is waiting on a 27B generation")

            # The draft is either not started yet or honestly `generating`.
            # NEVER `ready`: that would mean the alert path waited for it.
            drafts = client.get(f"/api/relay/channels/{room}/sage_assessments",
                                headers=_auth(doctor["id"])).json()
            assert all(d["state"] == sas.STATE_GENERATING for d in drafts), drafts
            assert all(d["whats_happening"] is None for d in drafts), (
                "a draft is showing content before generation finished")

            # A soft ceiling, not the 51 ms product number: this is a TestClient
            # round-trip through an encrypted-at-rest SQLite in CI. It exists to
            # catch the refactor that makes the write wait for the model, which
            # would blow past it by two orders of magnitude.
            assert charted_ms < 5000, (
                f"charting an observation took {charted_ms:.0f} ms with generation "
                f"blocked — the alert path is waiting for Sage")

            gate.set()
            row = _await_assessment(patient)
            assert row["state"] == sas.STATE_READY, row["unavailable_reason"]
    finally:
        gate.set()


# ═════════════════════════════════════════════════════════════════════════════
#  4. A SHED OR FAILED DRAFT LEAVES THE ALERT STANDING
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("failure,reason", [
    ("shed", sas.REASON_SHED),
    ("queue_timeout", sas.REASON_QUEUE_TIMEOUT),
    ("model_error", sas.REASON_MODEL_ERROR),
])
def test_a_shed_or_failed_draft_leaves_the_alert_intact_and_honestly_labelled(
        monkeypatch, failure, reason):
    """The alert stands alone and the surface says so — never a spinner.

    An alert is a clinical interruption that has already happened. A Sage failure
    may cost the clinician the reasoning; it may never cost them the alert, and
    it may never be presented as "still thinking" when nothing is coming.
    """
    from fastapi.testclient import TestClient
    from services.llm_queue_service import BackgroundAdmissionRejected, llm_queue

    _allow_medications(monkeypatch)
    if failure == "model_error":
        _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT,
                                            raises=RuntimeError("ollama is down")))
    else:
        _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))
        exc = (BackgroundAdmissionRejected("shed") if failure == "shed"
               else asyncio.TimeoutError())

        async def _acquire(*a, **kw):
            raise exc

        monkeypatch.setattr(llm_queue, "acquire", _acquire)

    nurse, patient, doctor = _mk_nurse(), _mk_patient(), _mk_doctor()
    room = _room(patient)

    with TestClient(_app()) as client:
        assert client.post(f"/api/nurse/patients/{patient}/observations",
                           json=dict(PERI_ARREST),
                           headers=_auth(nurse, "nurse")).status_code == 200
        _await_assessment(patient)

        alerts = [m for m in client.get(f"/api/relay/channels/{room}/messages",
                                        headers=_auth(doctor["id"])).json()
                  if m.get("message_type") == "alert"]
        assert len(alerts) == 1, "a Sage failure cost the ward its alert"

        draft = client.get(f"/api/relay/channels/{room}/sage_assessments",
                           headers=_auth(doctor["id"])).json()[0]

    assert draft["state"] == sas.STATE_UNAVAILABLE
    assert draft["unavailable_reason"] == reason
    assert draft["unavailable_message"], (
        "an unavailable draft with no ward-facing sentence renders as a blank "
        "panel, which reads as a spinner")
    assert draft["whats_happening"] is None and draft["considerations"] == [], (
        "content was fabricated for a draft that was never written")
    assert _cards(patient) == [], "a failed draft still proposed actions"

    # The instrument says FAILED, not DRAFTED. Counting a failure as a draft
    # makes the scorecard report Sage participation on a night Sage was down.
    assert _shadow(patient, "sage.drafted") == []
    failed = _shadow(patient, "sage.failed")
    assert len(failed) == 1 and _payload(failed[0])["failure"] == reason
    assert failed[0]["episode_id"] == _shadow(patient, "alert.fired")[0]["episode_id"], (
        "the failure is an orphan — it is not on the alert's episode")


def test_a_draft_killed_mid_generation_never_renders_as_pending(monkeypatch):
    """The process died while the 27B was running.

    No sweeper of ours ran either, so the row is still `generating` — and a
    surface that believes it shows a spinner forever, implying an assessment is
    on its way when the process that was writing it no longer exists. Asserted
    through the READ route, because the derivation only protects the clinician
    if it survives the endpoint.
    """
    from fastapi.testclient import TestClient

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))
    patient, doctor = _mk_patient(), _mk_doctor()
    room = _room(patient)

    stale = (datetime.now(timezone.utc) - timedelta(seconds=10_000)).isoformat()
    with db_session() as db:
        db.execute(
            """INSERT INTO sage_assessments (id, patient_id, channel_id, lane, state,
                                             news2, news2_level, created_at, updated_at)
               VALUES (?, ?, ?, 'manual', 'generating', 19, 'critical', ?, ?)""",
            (str(uuid.uuid4()), patient, room, stale, stale))

    with TestClient(_app()) as client:
        draft = client.get(f"/api/relay/channels/{room}/sage_assessments",
                           headers=_auth(doctor["id"])).json()[0]

    assert draft["state"] == sas.STATE_UNAVAILABLE
    assert draft["unavailable_reason"] == sas.REASON_ABANDONED
    assert draft["unavailable_message"]


# ═════════════════════════════════════════════════════════════════════════════
#  5. ORB'S ARITHMETIC WINS
# ═════════════════════════════════════════════════════════════════════════════
def test_the_models_news2_never_survives_a_disagreement_with_the_engine(monkeypatch):
    """The 2026-07-31 defect: the model answered 7 where Orb computed 11.

    A clinician looking at the escalation board and at this draft in the same
    second must not be shown two different scores for the same patient. Driven
    end to end so the property is proved on what the UI RECEIVES, not on a
    helper's return value.
    """
    from fastapi.testclient import TestClient

    # BOTH LINES NAME A CHARTED FACT, or Tier 2's post-filter deletes them and
    # this would prove the pin works by never reaching it. The wrong score is
    # what is under test; the drug and the allergy are what buy the lines their
    # ticket past the "every flag names its source fact" rule.
    lying = _brief([
        ("Warfarin charted — occult bleeding is possible",
         "NEWS2 of 7 with fever", None, None),
        ("Sulfonamide allergy charted — cover is limited",
         "The NEWS 2 score is 7", "start_medication", "Ceftriaxone"),
    ])

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(lying))
    nurse, patient, doctor = _mk_nurse(), _mk_patient(), _mk_doctor()
    room = _room(patient)

    with TestClient(_app()) as client:
        assert client.post(f"/api/nurse/patients/{patient}/observations",
                           json=dict(PERI_ARREST),
                           headers=_auth(nurse, "nurse")).status_code == 200
        _await_assessment(patient)
        draft = client.get(f"/api/relay/channels/{room}/sage_assessments",
                           headers=_auth(doctor["id"])).json()[0]

    assert draft["news2"] == ENGINE_NEWS2 == 19, (
        "the stored NEWS2 is not the deterministic engine's value")
    body = " ".join([c["step"] for c in draft["considerations"]]
                    + [c["why"] or "" for c in draft["considerations"]])
    assert "NEWS2 of 7" not in body and "score is 7" not in body and "NEWS2 score 7" not in body, (
        f"the model's own NEWS2 reached the clinician: {body!r}")
    assert "19" in body, "the engine's value did not replace the model's"


# ═════════════════════════════════════════════════════════════════════════════
#  6. NOTHING EXECUTES WITHOUT AN EXPLICIT CLICK
# ═════════════════════════════════════════════════════════════════════════════
def test_approving_the_assessment_executes_nothing_by_itself(monkeypatch):
    """"I agree with this reasoning" is not "give the patient this drug".

    The regulatory posture in one test: Orb is decision support, the assessment
    is a DRAFT, and the only thing that reaches a downstream module is a separate
    per-card click with its own safety gate and its own confirm lock.
    """
    from fastapi.testclient import TestClient

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))
    nurse, patient, doctor = _mk_nurse(), _mk_patient(), _mk_doctor()
    room = _room(patient)

    with TestClient(_app()) as client:
        assert client.post(f"/api/nurse/patients/{patient}/observations",
                           json=dict(PERI_ARREST),
                           headers=_auth(nurse, "nurse")).status_code == 200
        row = _await_assessment(patient)
        assert row["state"] == sas.STATE_READY

        assert client.post(
            f"/api/relay/channels/{room}/sage_assessments/{row['id']}/transition",
            json={"state": "approved", "version": row["version"], "reason": None},
            headers=_auth(doctor["id"])).status_code == 200

        assert not _executed_anything(patient), (
            "approving the ASSESSMENT reached a downstream module — an "
            "acknowledgement authorised a clinical action")
        assert all(c["state"] == "suggested" for c in _cards(patient)), (
            "approving the assessment resolved its action cards")

        # …and the per-card click is what actually reaches the pharmacy.
        card = [c for c in _cards(patient) if c["action_type"] == "start_medication"][0]
        assert client.post(
            f"/api/relay/channels/{room}/sage_actions/{card['id']}/transition",
            json={"state": "confirmed", "version": card["version"]},
            headers=_auth(doctor["id"])).status_code == 200

    assert _executed_anything(patient), (
        "the explicit per-card confirmation reached nothing downstream — the "
        "approve affordance is decorative")


def test_a_declined_draft_is_recorded_with_its_reason(monkeypatch):
    """Everything else in Orb records what a clinician ACCEPTED.

    The disagreements are the informative rows, and they are the only evidence
    that the shadow pilot is measuring clinician judgement rather than clinician
    compliance. Recorded twice on purpose: `sage_assessment_events` is the
    operational history, `audit_logs` is the tamper-evident governance record an
    NABH reviewer reads.
    """
    from fastapi.testclient import TestClient

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))
    nurse, patient, doctor = _mk_nurse(), _mk_patient(), _mk_doctor()
    room = _room(patient)
    why = "Disagree — this is fluid overload from the transfusion, not sepsis"

    with TestClient(_app()) as client:
        assert client.post(f"/api/nurse/patients/{patient}/observations",
                           json=dict(PERI_ARREST),
                           headers=_auth(nurse, "nurse")).status_code == 200
        row = _await_assessment(patient)
        out = client.post(
            f"/api/relay/channels/{room}/sage_assessments/{row['id']}/transition",
            json={"state": "declined", "version": row["version"], "reason": why},
            headers=_auth(doctor["id"]))
        assert out.status_code == 200, out.text
        assert out.json()["assessment"]["state"] == "declined"

    with db_session() as db:
        events = [dict(r) for r in db.execute(
            "SELECT * FROM sage_assessment_events WHERE assessment_id = ? ORDER BY id",
            (row["id"],)).fetchall()]
        audit = [dict(r) for r in db.execute(
            "SELECT * FROM audit_logs WHERE target_id = ? ORDER BY id",
            (row["id"],)).fetchall()]

    assert events and events[-1]["new_state"] == "declined"
    assert events[-1]["actor_id"] == doctor["id"]
    assert "fluid overload" in (events[-1]["reason"] or ""), (
        "the decline reason was not kept — the row is a bare 'no'")
    assert len(audit) == 1 and audit[0]["action_type"] == "sage_assessment_declined"
    assert audit[0]["row_hash"] and audit[0]["prev_hash"] is not None, (
        "the verdict is not on the tamper-evident chain")
    assert json.loads(audit[0]["metadata_json"])["executes_nothing"] is True


# ═════════════════════════════════════════════════════════════════════════════
#  7. THE INSTRUMENT CAN ACTUALLY SCORE THE LOOP
# ═════════════════════════════════════════════════════════════════════════════
async def test_the_scorecard_pairs_every_proposal_with_its_resolution(monkeypatch):
    """The chain is not merely present — the READ side can use it.

    Run through the real `shadow_scorecard_service`, because "the rows are
    there" and "the metric can pair them" are different claims, and it is the
    second one Samuel will show a champion. `agreement` stays below its n floor
    (10) so it correctly reports no percentage; what is asserted is the PAIRING,
    which is the part that was structurally impossible before this build.
    """
    from services import bot_service, shadow_scorecard_service

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))

    async def _rec(vitals_summary, risk_level, drivers):
        return "Escalate to the registrar."

    monkeypatch.setattr(bot_service, "generate_ai_recommendation", _rec)

    patient, doctor = _mk_patient(), _mk_doctor()
    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])
    await _drain()

    from routes.relay_routes import ActionTransitionRequest, _transition_action
    cards = sorted(_cards(patient), key=lambda c: c["action_type"])
    assert len(cards) == 2
    _transition_action(cards[0]["id"], _room(patient),
                       ActionTransitionRequest(state="confirmed",
                                               version=cards[0]["version"]), doctor)
    _transition_action(cards[1]["id"], _room(patient),
                       ActionTransitionRequest(state="dismissed",
                                               version=cards[1]["version"],
                                               reason="Not a surgical abdomen"), doctor)

    card = shadow_scorecard_service.scorecard(days=1, include_simulator=True)
    detail = card["metrics"]["agreement"]["detail"]
    mine = [r for r in detail["rows"]
            if r["episode_id"] == _shadow(patient, "action.proposed")[0]["episode_id"]]

    assert len(mine) == 2, (
        f"the scorecard paired {len(mine)} of 2 proposals; a resolution it cannot "
        f"pair is an orphan and `agreement` stays blind: {detail}")
    assert all(r["paired_by"] == "subject" for r in mine), (
        "the pairs were INFERRED from the clock rather than read off the card id "
        "— the explicit link the producers write is not reaching the metric")
    assert {r["resolution_kind"] for r in mine} == {"action.approved", "action.declined"}
    assert [r["matched"] for r in sorted(mine, key=lambda r: r["resolution_kind"])] == [
        True, False], "an approval did not count as agreement, or a decline did"


# ═════════════════════════════════════════════════════════════════════════════
#  8. THE DISCLOSURE BOUNDARY, on the whole chain
# ═════════════════════════════════════════════════════════════════════════════
def test_a_clinician_from_another_ward_sees_none_of_the_chain(monkeypatch):
    """The draft is a whole chart's reasoning. Every hop is checked separately.

    Included in the END-TO-END file rather than left to the unit tests because
    the failure mode is a NEW endpoint that forgets the check while its
    neighbours keep it — which only a sweep across the whole surface finds.
    """
    from fastapi.testclient import TestClient

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))
    nurse, patient = _mk_nurse(), _mk_patient()
    outsider = _mk_doctor(department="Cardiology")
    room = _room(patient)

    with TestClient(_app()) as client:
        assert client.post(f"/api/nurse/patients/{patient}/observations",
                           json=dict(PERI_ARREST),
                           headers=_auth(nurse, "nurse")).status_code == 200
        row = _await_assessment(patient)
        head = _auth(outsider["id"])

        assert client.get(f"/api/relay/channels/{room}/messages", headers=head
                          ).status_code == 403
        assert client.get(f"/api/relay/channels/{room}/sage_assessments", headers=head
                          ).status_code == 403
        assert client.get(f"/api/relay/sage_assessments/{row['id']}", headers=head
                          ).status_code == 403
        assert client.get(f"/api/relay/channels/{room}/sage_actions", headers=head
                          ).status_code == 403
        assert client.post(
            f"/api/relay/channels/{room}/sage_assessments/{row['id']}/transition",
            json={"state": "approved", "version": row["version"]},
            headers=head).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
#  9. SHADOW PROVENANCE AND PROXY REGRESSIONS
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("lane", ["manual", "simulator", None])
@pytest.mark.parametrize("state,kind", [
    ("confirmed", "action.approved"), ("dismissed", "action.declined"),
])
def test_action_resolution_keeps_assessment_origin_and_ambient_chat_fallback(monkeypatch, lane, state, kind):
    from routes.relay_routes import ActionTransitionRequest, _transition_action

    patient, doctor = _mk_patient(), _mk_doctor()
    action_id = uuid.uuid4().hex
    original_episode = "origin-" + uuid.uuid4().hex
    with db_session() as db:
        db.execute(
            "INSERT INTO sage_actions (id, room_id, patient_id, action_type, target) "
            "VALUES (?, ?, ?, 'transfer_to_surgery', 'test only')",
            (action_id, _room(patient), patient),
        )
        if lane:
            assessment_id = uuid.uuid4().hex
            db.execute(
                """INSERT INTO sage_assessments
                   (id, patient_id, channel_id, episode_id, lane, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'ready', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (assessment_id, patient, _room(patient), original_episode, lane),
            )
            db.execute(
                "INSERT INTO sage_assessment_actions (assessment_id, action_id, created_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (assessment_id, action_id),
            )

    if lane:
        # A delayed decision must not create/choose a newer episode, even when
        # its original shadow proposal was unavailable or has been archived.
        def unexpected_episode(*args, **kwargs):
            raise AssertionError("the assessment already identifies the original episode")

        monkeypatch.setattr(shadow_service, "open_episode", unexpected_episode)

    updated = _transition_action(
        action_id, _room(patient),
        ActionTransitionRequest(state=state, version=1, reason="test decision"), doctor,
    )
    assert updated["state"] == state
    rows = _shadow(patient, kind)
    assert len(rows) == 1
    assert rows[0]["lane"] == (lane or "chat")
    if lane:
        assert rows[0]["episode_id"] == original_episode


async def test_the_clinicians_verdict_is_filed_under_the_lane_the_alert_was_raised_on(monkeypatch):
    """The lane string IS the finding — the same shape as 2026-08-14's Defect A.

    Asserted on what it COSTS rather than on the string alone, because the cost
    is what makes it worth fixing:

      * `shadow_scorecard_service._load_events` excludes `lane = 'simulator'`
        from every metric by default (rule 9: simulator rows are not evidence).
        A proposal raised on the simulator lane is therefore dropped while its
        resolution — filed under 'chat' — is kept, so `agreement` reports an
        `actions_without_a_proposal`: a clinician decision that, as far as the
        scorecard can tell, Sage never prompted. Every demo of the wedge adds
        one.
      * On the manual lane both rows survive and the pair still matches, but the
        instrument's per-lane histogram then says the pilot's own lane never
        resolved anything — the same blindness Defect A was about, one hop later
        in the chain.
    """
    from services import bot_service, shadow_scorecard_service

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))

    async def _rec(vitals_summary, risk_level, drivers):
        return "Escalate to the registrar."

    monkeypatch.setattr(bot_service, "generate_ai_recommendation", _rec)

    patient, doctor = _mk_patient(), _mk_doctor()
    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])
    await _drain()

    from routes.relay_routes import ActionTransitionRequest, _transition_action
    card = sorted(_cards(patient), key=lambda c: c["action_type"])[0]
    _transition_action(card["id"], _room(patient),
                       ActionTransitionRequest(state="confirmed",
                                               version=card["version"]), doctor)

    lanes = {r["lane"] for r in _shadow(patient)}
    assert lanes == {"simulator"}, (
        f"one deterioration was recorded under lanes {sorted(lanes)}; the "
        f"clinician's verdict is not on the lane the alert was raised on")

    # The default scorecard — the one an operator actually opens.
    detail = shadow_scorecard_service.scorecard(days=1)["metrics"]["agreement"]["detail"]
    assert detail["actions_without_a_proposal"] == 0, (
        f"the scorecard sees {detail['actions_without_a_proposal']} clinician "
        f"action(s) with no proposal behind them — the proposal was filtered out "
        f"as simulator-lane while its resolution survived as 'chat'")


def test_the_alert_assessment_does_not_inflate_the_time_saved_proxy(monkeypatch):
    """THE HONEST-NUMBER GUARD, and it is the number most likely to be quoted.

    `_time_saved`'s own METRIC_SPEC calls it "the figure most likely to be quoted
    back in a fundraising context and the one with the weakest foundation". Its
    formula is `timed_answers x max(0, 240s - measured_p50)`. Nobody was waiting
    on the alert assessment — Vigil asked for it, not a clinician — so counting
    it as a lookup a clinician would otherwise have done by hand invents saved
    hours out of a background job.

    `test_wedge_loop.test_the_alert_draft_carries_no_latency_into_the_time_saved_
    proxy` kept the OLD one-sentence garnish out of this denominator by leaving
    it untimed. The replacement is genuinely timed — correctly, the timings are
    real — so the exclusion happens on the read side by query_class.
    """
    from fastapi.testclient import TestClient
    from services import shadow_scorecard_service

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))
    nurse, patient = _mk_nurse(), _mk_patient()

    with TestClient(_app()) as client:
        assert client.post(f"/api/nurse/patients/{patient}/observations",
                           json=dict(PERI_ARREST),
                           headers=_auth(nurse, "nurse")).status_code == 200
        row = _await_assessment(patient)
    assert row["state"] == sas.STATE_READY

    # One REAL clinician question, timed, on the same window: a 2-second answer
    # to something a doctor actually typed. This is the population `time_saved`
    # is defined over.
    episode = _shadow(patient, "alert.fired")[0]["episode_id"]
    with db_session() as db:
        shadow_service.emit_many([shadow_service.build(
            "sage.drafted", episode, "manual", patient_id=patient,
            payload={"conversation_id": "conv-interactive", "citation_basis": "retrieved",
                     "confidence": "high", "retrieved_doc_ids": [], "cited_doc_ids": [],
                     "trust_tiers": [], "ttft_ms": 400, "retrieval_ms": 100,
                     "total_ms": 2000, "query_class": "PATIENT", "cancelled": False},
        )], db=db)

    detail = shadow_scorecard_service.scorecard(
        days=1, include_simulator=True)["metrics"]["time_saved"]["detail"]

    assert detail["total_ms_p50"] == 2000.0, (
        f"the alert assessment moved the answer-latency p50 to "
        f"{detail['total_ms_p50']} — a machine-initiated draft is being priced "
        f"against a clinician's manual lookup")
    assert detail["answers"] == detail["timed_answers"] == 1


async def test_the_assessments_news2_and_its_level_come_from_the_same_computation(monkeypatch):
    """A NEWS2 19 wearing a 'stable' badge is the 2026-08-13 defect class again.

    FIXED 2026-08-14 (DEF-2), and this test flipped from a strict xfail to a
    pass. `_fresh_risk_block` took `news2` and `drivers` from a FRESH
    compute_news2 but `score` and `level` from the newest risk_scores row, which
    on the simulator lane is whatever admission_service wrote hours earlier. The
    band now comes from the same call as the score; the stored row may only
    ESCALATE it (`sage_service._reconciled_band`). The rule itself is pinned
    across the NEWS2 range and both SpO2 scales in
    tests/test_risk_block_consistency.py — this test stays because it is the
    only one that drives the whole alert→draft path end to end.

    `render()` hands the UI `news2` and `news2_level` side by side, and the two
    came from DIFFERENT computations against DIFFERENT observations. A surface
    that colours the panel by level shows a stable-coloured chip on a peri-arrest
    assessment, and the two figures on the same card contradict each other.

    Driven on the SIMULATOR lane deliberately: the nurse route writes a
    risk_scores row inside the same call that charts the observation, so the two
    agree there by luck of ordering rather than by construction. The simulator
    writes vitals and NOTHING else (`bot_service._simulate_tick`), which is the
    demo a champion watches.
    """
    from services import bot_service

    _allow_medications(monkeypatch)
    _arrange(monkeypatch, _FakeProvider(TWO_CARD_OUTPUT))

    async def _rec(vitals_summary, risk_level, drivers):
        return "Escalate to the registrar."

    monkeypatch.setattr(bot_service, "generate_ai_recommendation", _rec)
    patient = _mk_patient()

    # The risk_scores row `admission_service` writes when the patient is
    # admitted, and which nothing on the simulator lane ever refreshes.
    with db_session() as db:
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level, drivers, timestamp) "
            "VALUES (?, 0.1, 'stable', '[]', ?)",
            (patient, (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()))

    await bot_service.evaluate_and_dispatch_alerts(_NullSio(), [_sim_payload(patient)])
    await _drain()

    draft = sas.render(_assessments(patient)[0])
    assert draft["news2"] == 19
    assert draft["news2_level"] == "critical", (
        f"the draft says NEWS2 {draft['news2']} at level {draft['news2_level']!r} "
        f"— the aggregate and the band on the same card disagree")
