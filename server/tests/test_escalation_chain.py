"""Escalation chain with acknowledgement — nurse → registrar → consultant.

WHY THIS FILE EXISTS
--------------------
A nurse-charted deterioration reached NOBODY. ``dispatch_observation_alert``
posts a line into the patient's Relay case room and stops there: nothing records
that a human saw it, nothing happens when no human does, and an alert that
scrolls off a busy channel is indistinguishable from an alert that was answered.
Manual entry is the ONLY vitals path that exists when a monitor VLAN is down —
main.py calls it "the ward's redundancy layer" — so the path with no escalation
is the path the ward falls back to.

WHAT IS ASSERTED HERE
  * a NEWS2 at or above the site's threshold opens ONE chain, and repeated
    qualifying observations extend it rather than spawning another,
  * the chain stays OPEN until someone acknowledges, and the acknowledgement
    records WHO and WHEN,
  * an unacknowledged chain climbs tiers ON A CLOCK — injected, never slept,
  * resolution needs a reason, and a chain that is neither acknowledged nor
    resolved is STILL VISIBLE days later (the whole failure mode),
  * the score is compute_news2's, not a second implementation,
  * the ward boundary holds: another department's escalation is invisible and
    un-acknowledgeable, and the refusal is 404 rather than 403.

TIMING IS INJECTED. Every service entry point takes ``now``; no test sleeps.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from services.vigil_service import compute_news2


# DEPARTMENTS ARE UNIQUE PER TEST, and that is not fastidiousness.
# `users` rows are never cleaned up by any fixture in this suite, so a fixed
# ward name accumulates every doctor and nurse every earlier test created in it.
# _derive_target picks deterministically (ORDER BY id), so a stale consultant
# from a previous test can win the "registrar" pick and the assertion fails for
# a reason that has nothing to do with the code. Verified: the registrar test
# resolved to a CONS… id minted by an earlier test's fixture.
def _ward(prefix="Escalation Test Ward"):
    return f"{prefix} {uuid.uuid4().hex[:8]}"


# A fixed instant so every expectation in this file is arithmetic, not a race.
T0 = datetime(2026, 8, 7, 9, 0, 0, tzinfo=timezone.utc)


# ── vitals sets, named by what they mean clinically ──────────────────────────
STABLE = dict(heart_rate=72, systolic_bp=120, respiratory_rate=16,
              temperature=36.8, spo2=98)
# NEWS2 6 (RR 22 = 2, SpO2 93 = 2, HR 95 = 1, Temp 38.5 = 1) — over the
# urgent-response threshold of 5, under the emergency band of 7.
DETERIORATING = dict(heart_rate=95, systolic_bp=120, respiratory_rate=22,
                     temperature=38.5, spo2=93)
# NEWS2 >= 7 — the emergency-response band. Charted as written (ACVPU alert, no
# supplemental O2) this aggregates to 15.
PERI_ARREST = dict(heart_rate=135, systolic_bp=84, respiratory_rate=30,
                   temperature=35.0, spo2=88)
# NEWS2 9 — comfortably inside the emergency band, and therefore ALREADY at the
# highest tier the policy can be ENTERED at (tier 2). It exists to be the open
# chain that a worse score has to get past. See
# test_a_worse_score_at_the_same_entry_tier_still_reaches_the_chain.
CRITICAL_9 = dict(heart_rate=120, systolic_bp=95, respiratory_rate=24,
                  temperature=38.5, spo2=92)
# Aggregate below the threshold but ONE parameter scoring 3 (RR 28).
SINGLE_PARAM_3 = dict(heart_rate=72, systolic_bp=120, respiratory_rate=28,
                      temperature=36.8, spo2=98)


def _news2(vitals: dict, **kw) -> dict:
    return compute_news2(vitals["heart_rate"], vitals["systolic_bp"],
                         vitals["respiratory_rate"], vitals["temperature"],
                         vitals["spo2"], kw.get("consciousness", "alert"),
                         kw.get("on_oxygen", False))


# ── fixtures ─────────────────────────────────────────────────────────────────
def _mk_user(role, department, prefix="ESC"):
    uid = f"{prefix}{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, ?, 'x', ?)",
            (uid, f"Escalation {role}", role, department),
        )
    return uid


def _mk_patient(department, attending=None, bed="E-1"):
    pid = f"PATESC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, "
            "status, attending_doctor_id) "
            "VALUES (?, ?, ?, ?, 'EW', 64, 'M', 'Pneumonia', 'admitted', ?)",
            (pid, "Escalation Test Patient", f"MRN-{uuid.uuid4().hex[:8]}", bed, attending),
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('ESCADMITTER', 'Escalation Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, status, admitted_by, "
            "attending_doctor_id) VALUES (?, ?, 'active', 'ESCADMITTER', ?)",
            (pid, department, attending),
        )
    return pid


def _cleanup(patient_ids):
    """TEST-ISOLATION LAW: discharge everything this module admitted, and delete
    the escalation rows it raised, so a later module measuring open escalations
    or admitted patients never measures our leftovers."""
    if not patient_ids:
        return
    marks = ",".join("?" * len(patient_ids))
    with db_session() as db:
        db.execute(
            f"DELETE FROM escalation_events WHERE escalation_id IN "
            f"(SELECT id FROM clinical_escalations WHERE patient_id IN ({marks}))",
            tuple(patient_ids),
        )
        db.execute(
            f"DELETE FROM clinical_escalations WHERE patient_id IN ({marks})",
            tuple(patient_ids),
        )
        db.execute(f"DELETE FROM vitals WHERE patient_id IN ({marks})", tuple(patient_ids))
        db.execute(f"DELETE FROM risk_scores WHERE patient_id IN ({marks})", tuple(patient_ids))
        db.execute(
            f"UPDATE admissions SET status = 'discharged' WHERE patient_id IN ({marks})",
            tuple(patient_ids),
        )
        db.execute(
            f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})",
            tuple(patient_ids),
        )


@pytest.fixture()
def world():
    """One ward with a full cast, and a second ward that must never leak in."""
    dept = _ward()
    other_dept = _ward("Escalation Test Other Ward")
    consultant = _mk_user("doctor", dept, "CONS")
    registrar = _mk_user("doctor", dept, "REG")
    nurse = _mk_user("nurse", dept, "NUR")
    other_nurse = _mk_user("nurse", other_dept, "ONUR")
    patient = _mk_patient(dept, attending=consultant)
    other_patient = _mk_patient(other_dept, attending=None, bed="O-9")
    w = {
        "dept": dept, "other_dept": other_dept,
        "consultant": consultant, "registrar": registrar, "nurse": nurse,
        "other_nurse": other_nurse, "patient": patient, "other_patient": other_patient,
    }
    yield w
    _cleanup([patient, other_patient])


@pytest.fixture()
def client():
    from routes.vigil_routes import router as vigil_router
    app = FastAPI()
    app.include_router(vigil_router)
    return TestClient(app)


def _auth(uid, role):
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


# ═════════════════════════════════════════════════════════════════════════════
# 1. Schema
# ═════════════════════════════════════════════════════════════════════════════
def test_migration_11_created_the_escalation_tables():
    from database import init_db
    import migrations
    init_db()
    with db_session() as db:
        applied = {r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        idx = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert 11 in applied, "migration 11 not recorded in the ledger"
    assert any(m.version == 11 for m in migrations.MIGRATIONS)
    assert "clinical_escalations" in tables
    assert "escalation_events" in tables
    assert "idx_escalation_one_active_per_patient" in idx, (
        "the one-active-chain-per-patient guarantee must be enforced by the "
        "DATABASE, not by a service-layer convention"
    )


def test_the_database_itself_refuses_a_second_active_chain(world):
    """Idempotency is a constraint, not an intention. Two OPEN chains for one
    patient cannot exist even if a caller bypasses the service entirely."""
    from db_backend import DBIntegrityError
    from services import escalation_service as esc

    esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)

    with pytest.raises(DBIntegrityError):
        with db_session() as db:
            db.execute(
                """INSERT INTO clinical_escalations
                   (id, patient_id, status, trigger, news2, peak_news2, level,
                    current_tier, max_tier, raised_at)
                   VALUES (?, ?, 'open', 'news2', 9, 9, 'critical', 1, 3, ?)""",
                (f"ESCDUP{uuid.uuid4().hex[:6]}", world["patient"], T0.isoformat()),
            )


# ═════════════════════════════════════════════════════════════════════════════
# 2. Raising — threshold, score provenance, idempotency
# ═════════════════════════════════════════════════════════════════════════════
def test_a_stable_observation_raises_nothing(world):
    from services import escalation_service as esc
    assert esc.evaluate_observation(world["patient"], _news2(STABLE), now=T0) is None
    assert esc.list_escalations(department=world["dept"]) == []


def test_a_qualifying_observation_opens_one_chain_at_the_first_tier(world):
    from services import escalation_service as esc

    raised = esc.evaluate_observation(
        world["patient"], _news2(DETERIORATING), vitals_id=1234,
        source="nurse_manual", actor_id=world["nurse"], now=T0,
    )
    assert raised is not None
    assert raised["status"] == "open"
    assert raised["current_tier"] == 1
    assert raised["tier_role"] == "nurse"
    assert raised["news2"] == _news2(DETERIORATING)["raw_news2"] == 6
    assert raised["acknowledged_at"] is None
    # The clock is injected end to end — raised_at is T0, not wall time.
    assert raised["raised_at"].startswith("2026-08-07T09:00")
    assert raised["tier_due_at"] == (T0 + timedelta(minutes=10)).isoformat()

    chain = esc.get_escalation(raised["id"])
    kinds = [e["event_type"] for e in chain["events"]]
    assert kinds[0] == "raised"
    assert chain["events"][0]["tier"] == 1


def test_the_score_is_compute_news2s_and_not_a_second_implementation(world):
    """The one rule this module must NOT own. Every stored aggregate has to be
    byte-for-byte what the RCP scorer returned for the same observation set."""
    from services import escalation_service as esc

    for vitals in (DETERIORATING, PERI_ARREST, SINGLE_PARAM_3):
        pid = _mk_patient(world["dept"])
        try:
            expected = _news2(vitals)
            got = esc.evaluate_observation(pid, expected, now=T0)
            assert got["news2"] == expected["raw_news2"]
            assert got["level"] == expected["level"]
            assert json.loads(got["drivers_json"]) == expected["drivers"]
        finally:
            _cleanup([pid])


def test_repeated_qualifying_observations_do_not_spawn_a_new_chain(world):
    """The alert-fatigue guarantee AND the audit guarantee at once: five
    deteriorating sets in a row are ONE escalation with five recorded steps."""
    from services import escalation_service as esc

    ids = set()
    for i in range(5):
        out = esc.evaluate_observation(
            world["patient"], _news2(DETERIORATING),
            vitals_id=100 + i, now=T0 + timedelta(minutes=i),
        )
        ids.add(out["id"])

    assert len(ids) == 1, "a repeated qualifying observation spawned a new chain"
    assert len(esc.list_escalations(department=world["dept"])) == 1

    chain = esc.get_escalation(ids.pop())
    kinds = [e["event_type"] for e in chain["events"]]
    assert kinds.count("raised") == 1
    assert kinds.count("observation") == 4, (
        "the four repeat observations must each be recorded on the chain — a "
        "deterioration that is silently swallowed is the failure mode"
    )
    assert [e["seq"] for e in chain["events"]] == sorted(e["seq"] for e in chain["events"])


def test_single_parameter_three_raises_below_the_aggregate_threshold(world):
    """RCP's single-parameter rule: one vital scoring 3 needs urgent review even
    when the aggregate is low. compute_news2 already says so; we honour it."""
    from services import escalation_service as esc
    n = _news2(SINGLE_PARAM_3)
    assert n["raw_news2"] < 5 and n["single_param_3"] is True
    out = esc.evaluate_observation(world["patient"], n, now=T0)
    assert out is not None and out["single_param_3"] == 1


def test_a_critical_score_enters_higher_up_the_chain(world):
    """NEWS2 >= 7 is the emergency-response band; the site policy starts it at
    the registrar rather than making it wait out the ward-nurse tier first."""
    from services import escalation_service as esc
    out = esc.evaluate_observation(world["patient"], _news2(PERI_ARREST), now=T0)
    assert _news2(PERI_ARREST)["raw_news2"] >= 7
    assert out["current_tier"] == 2
    assert out["tier_role"] == "registrar"


# ═════════════════════════════════════════════════════════════════════════════
# 3. The clock — tiers advance without anybody sleeping
# ═════════════════════════════════════════════════════════════════════════════
def test_an_unacknowledged_chain_climbs_the_tiers_on_the_clock(world):
    from services import escalation_service as esc

    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    eid = out["id"]

    # 9 minutes: not yet due.
    esc.advance_due(now=T0 + timedelta(minutes=9))
    assert esc.get_escalation(eid)["current_tier"] == 1

    # 11 minutes: nurse tier expired → registrar.
    esc.advance_due(now=T0 + timedelta(minutes=11))
    step2 = esc.get_escalation(eid)
    assert step2["current_tier"] == 2
    assert step2["tier_role"] == "registrar"
    assert step2["status"] == "open"

    # 15 more minutes on the registrar tier → consultant.
    esc.advance_due(now=T0 + timedelta(minutes=27))
    step3 = esc.get_escalation(eid)
    assert step3["current_tier"] == 3
    assert step3["tier_role"] == "consultant"
    assert step3["tier_due_at"] is None, "the last tier has nowhere left to escalate to"

    # And it stops there rather than wrapping or dying.
    esc.advance_due(now=T0 + timedelta(days=2))
    end = esc.get_escalation(eid)
    assert end["current_tier"] == 3
    assert end["status"] == "open"

    steps = [e for e in end["events"] if e["event_type"] == "tier_advanced"]
    assert [s["tier"] for s in steps] == [2, 3], "each step must be recorded"


def test_a_chain_neither_acknowledged_nor_resolved_is_still_visible_days_later(world):
    """THE point of the feature. A silently dropped deterioration alert is the
    entire failure mode, so time must never retire an unanswered chain."""
    from services import escalation_service as esc

    esc.evaluate_observation(world["patient"], _news2(PERI_ARREST), now=T0)
    esc.advance_due(now=T0 + timedelta(days=3))

    three_days_on = T0 + timedelta(days=3)
    still_open = esc.list_escalations(department=world["dept"], now=three_days_on)
    assert len(still_open) == 1
    assert still_open[0]["status"] == "open"
    assert still_open[0]["acknowledged_at"] is None
    assert still_open[0]["resolved_at"] is None
    assert still_open[0]["age_minutes"] >= 3 * 24 * 60


# ═════════════════════════════════════════════════════════════════════════════
# 4. Acknowledgement and resolution
# ═════════════════════════════════════════════════════════════════════════════
def test_acknowledgement_records_who_and_when_and_stops_the_ladder(world):
    from services import escalation_service as esc

    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    acked = esc.acknowledge(
        out["id"],
        {"id": world["registrar"], "role": "doctor", "name": "Dr Registrar"},
        now=T0 + timedelta(minutes=4),
    )
    assert acked["status"] == "acknowledged"
    assert acked["acknowledged_by"] == world["registrar"]
    assert acked["acknowledged_at"] == (T0 + timedelta(minutes=4)).isoformat()
    assert acked["acknowledged_tier"] == 1
    assert acked["tier_due_at"] is None, "acknowledgement must stop the timer"

    # An hour later the ladder has NOT moved.
    esc.advance_due(now=T0 + timedelta(hours=1))
    after = esc.get_escalation(out["id"])
    assert after["current_tier"] == 1
    assert after["status"] == "acknowledged"


def test_an_acknowledged_chain_stays_on_the_board_until_it_is_resolved(world):
    from services import escalation_service as esc
    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    esc.acknowledge(out["id"], {"id": world["nurse"], "role": "nurse"},
                    now=T0 + timedelta(minutes=2))
    listed = esc.list_escalations(department=world["dept"])
    assert [e["status"] for e in listed] == ["acknowledged"], (
        "acknowledged is not finished — the patient is still deteriorating until "
        "somebody says what was done"
    )


def test_resolution_demands_a_reason(world):
    from services import escalation_service as esc
    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    for bad in (None, "", "   "):
        with pytest.raises(ValueError):
            esc.resolve(out["id"], {"id": world["registrar"], "role": "doctor"},
                        reason=bad, now=T0 + timedelta(minutes=5))
    assert esc.get_escalation(out["id"])["status"] == "open"


def test_resolution_closes_the_chain_and_records_the_reason(world):
    from services import escalation_service as esc
    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    esc.acknowledge(out["id"], {"id": world["registrar"], "role": "doctor"},
                    now=T0 + timedelta(minutes=3))
    done = esc.resolve(
        out["id"], {"id": world["registrar"], "role": "doctor"},
        reason="Reviewed at bedside, IV fluids started, obs now Q30min",
        category="reviewed_treated", now=T0 + timedelta(minutes=9),
    )
    assert done["status"] == "resolved"
    assert done["resolved_by"] == world["registrar"]
    assert done["resolution_category"] == "reviewed_treated"
    assert "IV fluids" in done["resolution_reason"]
    assert esc.list_escalations(department=world["dept"]) == []
    assert len(esc.list_escalations(department=world["dept"], include_resolved=True)) == 1


def test_after_resolution_a_fresh_deterioration_opens_a_new_chain(world):
    from services import escalation_service as esc
    first = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    esc.resolve(first["id"], {"id": world["registrar"], "role": "doctor"},
                reason="Settled", now=T0 + timedelta(minutes=20))
    second = esc.evaluate_observation(
        world["patient"], _news2(DETERIORATING), now=T0 + timedelta(hours=2))
    assert second["id"] != first["id"]
    assert second["status"] == "open"


def test_deterioration_while_acknowledged_reopens_at_the_higher_tier(world):
    """Acknowledgement must not become a mute button. A worse score on an
    already-acknowledged chain re-opens it at the tier that score deserves."""
    from services import escalation_service as esc

    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    esc.acknowledge(out["id"], {"id": world["nurse"], "role": "nurse"},
                    now=T0 + timedelta(minutes=2))

    worse = esc.evaluate_observation(
        world["patient"], _news2(PERI_ARREST), now=T0 + timedelta(minutes=20))
    assert worse["id"] == out["id"], "still one chain"
    assert worse["status"] == "open", "re-opened for a fresh acknowledgement"
    assert worse["current_tier"] == 2
    assert worse["peak_news2"] == _news2(PERI_ARREST)["raw_news2"]
    # A board renders acknowledged_by next to the chain. Leaving the stale one on
    # a chain that has just re-opened unanswered would print "acknowledged by
    # nurse X" beside a deterioration nobody has taken.
    assert worse["acknowledged_at"] is None
    assert worse["acknowledged_by"] is None

    chain = esc.get_escalation(out["id"])
    kinds = [e["event_type"] for e in chain["events"]]
    assert "reopened" in kinds
    # The first acknowledgement is not erased — it moved to where history lives.
    ack_events = [e for e in chain["events"] if e["event_type"] == "acknowledged"]
    assert len(ack_events) == 1
    assert ack_events[0]["actor_user_id"] == world["nurse"]


# ═════════════════════════════════════════════════════════════════════════════
# 5. Who it goes to — derived from what Orb actually has
# ═════════════════════════════════════════════════════════════════════════════
def test_the_consultant_tier_targets_the_attending_doctor(world):
    from services import escalation_service as esc
    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    esc.advance_due(now=T0 + timedelta(minutes=11))
    esc.advance_due(now=T0 + timedelta(minutes=27))
    top = esc.get_escalation(out["id"])
    assert top["tier_role"] == "consultant"
    assert top["target_user_id"] == world["consultant"]


def test_the_registrar_tier_prefers_a_department_doctor_who_is_not_the_attending(world):
    from services import escalation_service as esc
    out = esc.evaluate_observation(world["patient"], _news2(PERI_ARREST), now=T0)
    assert out["tier_role"] == "registrar"
    assert out["target_user_id"] == world["registrar"], (
        "Orb has no grade column, so 'registrar' resolves to a department doctor "
        "who is not the named consultant"
    )


def test_a_tier_with_nobody_derivable_is_addressed_to_the_role_and_ward(world):
    """No duty roster exists. When no individual can be derived the escalation is
    addressed to a ROLE on a WARD and stays visible to everyone who holds it —
    it must never silently target nobody."""
    from services import escalation_service as esc
    lonely_dept = f"Escalation Empty Ward {uuid.uuid4().hex[:6]}"
    pid = _mk_patient(lonely_dept)
    try:
        out = esc.evaluate_observation(pid, _news2(DETERIORATING), now=T0)
        assert out["target_user_id"] is None
        assert out["tier_role"] == "nurse"
        assert lonely_dept in out["target_description"]
        assert "nurse" in out["target_description"].lower()
    finally:
        _cleanup([pid])


def test_the_nurse_tier_prefers_the_nurse_who_charted_the_observation(world):
    from services import escalation_service as esc
    out = esc.evaluate_observation(
        world["patient"], _news2(DETERIORATING), actor_id=world["nurse"], now=T0)
    assert out["target_user_id"] == world["nurse"]


# ═════════════════════════════════════════════════════════════════════════════
# 6. Per-site configuration
# ═════════════════════════════════════════════════════════════════════════════
def test_tiers_and_timings_are_configurable_per_site(world, monkeypatch):
    from services import escalation_service as esc
    monkeypatch.setenv("ORB_ESCALATION_TIERS", json.dumps([
        {"tier": 1, "role": "nurse", "label": "Bay nurse", "timeout_minutes": 3},
        {"tier": 2, "role": "consultant", "label": "Consultant", "timeout_minutes": None},
    ]))
    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    assert out["max_tier"] == 2
    assert out["tier_due_at"] == (T0 + timedelta(minutes=3)).isoformat()
    esc.advance_due(now=T0 + timedelta(minutes=4))
    assert esc.get_escalation(out["id"])["tier_role"] == "consultant"


def test_the_threshold_is_configurable_per_site(world, monkeypatch):
    from services import escalation_service as esc
    monkeypatch.setenv("ORB_ESCALATION_NEWS2_THRESHOLD", "7")
    monkeypatch.setenv("ORB_ESCALATION_ON_SINGLE_PARAM_3", "0")
    assert esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0) is None
    assert esc.evaluate_observation(world["patient"], _news2(PERI_ARREST), now=T0) is not None


def test_a_broken_policy_falls_back_to_the_default_rather_than_taking_the_ward_down(
        world, monkeypatch):
    """A typo in a site's .env must not mean deteriorations stop escalating."""
    from services import escalation_service as esc
    monkeypatch.setenv("ORB_ESCALATION_TIERS", "{not json at all")
    policy = esc.tier_policy()
    assert [t["role"] for t in policy] == ["nurse", "registrar", "consultant"]
    assert esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0) is not None


# ═════════════════════════════════════════════════════════════════════════════
# 7. The wedge: a manually charted deterioration that nothing wired up
# ═════════════════════════════════════════════════════════════════════════════
def test_the_sweep_finds_a_nurse_charted_deterioration_nobody_dispatched(world):
    """The safety net. A vitals row lands from the manual-entry path with no
    escalation call at all — the sweep must still raise the chain, because
    'nobody wired it up' is precisely how the original hole existed."""
    from services import escalation_service as esc

    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                   diastolic_bp, spo2, temperature, respiratory_rate, consciousness,
                   on_oxygen, source)
               VALUES (?, ?, ?, ?, 70, ?, ?, ?, 'alert', 0, 'nurse_manual')""",
            (world["patient"], (T0 - timedelta(minutes=2)).isoformat(),
             PERI_ARREST["heart_rate"], PERI_ARREST["systolic_bp"], PERI_ARREST["spo2"],
             PERI_ARREST["temperature"], PERI_ARREST["respiratory_rate"]),
        )

    assert esc.list_escalations(department=world["dept"]) == []
    found = esc.scan_new_observations(department=world["dept"], now=T0)
    assert len(found) == 1
    live = esc.list_escalations(department=world["dept"])
    assert len(live) == 1
    assert live[0]["source"] == "nurse_manual"
    assert live[0]["news2"] == _news2(PERI_ARREST)["raw_news2"]


def test_the_sweep_is_idempotent_and_does_not_re_raise_the_same_reading(world):
    from services import escalation_service as esc
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                   diastolic_bp, spo2, temperature, respiratory_rate, consciousness,
                   on_oxygen, source)
               VALUES (?, ?, ?, ?, 70, ?, ?, ?, 'alert', 0, 'nurse_manual')""",
            (world["patient"], (T0 - timedelta(minutes=2)).isoformat(),
             DETERIORATING["heart_rate"], DETERIORATING["systolic_bp"],
             DETERIORATING["spo2"], DETERIORATING["temperature"],
             DETERIORATING["respiratory_rate"]),
        )
    first = esc.scan_new_observations(department=world["dept"], now=T0)
    second = esc.scan_new_observations(department=world["dept"], now=T0 + timedelta(minutes=1))
    third = esc.scan_new_observations(department=world["dept"], now=T0 + timedelta(minutes=2))
    assert len(first) == 1 and second == [] and third == []
    assert len(esc.list_escalations(department=world["dept"])) == 1


def _chart_vitals(patient_id, vitals, when, source="nurse_manual"):
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                   diastolic_bp, spo2, temperature, respiratory_rate, consciousness,
                   on_oxygen, source)
               VALUES (?, ?, ?, ?, 70, ?, ?, ?, 'alert', 0, ?)""",
            (patient_id, when.isoformat(), vitals["heart_rate"], vitals["systolic_bp"],
             vitals["spo2"], vitals["temperature"], vitals["respiratory_rate"], source),
        )
        return cur.lastrowid


def test_the_sweep_does_not_staple_a_step_onto_an_already_open_chain(world):
    """A monitor feed writes a row every few seconds and the board is polled
    every 30s. If each sweep recorded "further observation" on the open chain,
    its history would be unreadable within the hour — and an unreadable history
    is the same as no history when somebody reviews the incident."""
    from services import escalation_service as esc

    _chart_vitals(world["patient"], DETERIORATING, T0 - timedelta(minutes=5))
    opened = esc.scan_new_observations(department=world["dept"], now=T0)[0]
    before = len(esc.get_escalation(opened["id"])["events"])

    for i in range(1, 6):
        _chart_vitals(world["patient"], DETERIORATING, T0 - timedelta(minutes=5) + timedelta(seconds=10 * i))
        esc.scan_new_observations(department=world["dept"], now=T0 + timedelta(minutes=i))

    after = esc.get_escalation(opened["id"])["events"]
    assert len(after) == before, (
        "the safety net narrated an already-open chain: "
        + str([e["event_type"] for e in after])
    )
    assert len(esc.list_escalations(department=world["dept"])) == 1


def test_but_a_worse_reading_still_climbs_the_open_chain(world):
    """The flip side of the rule above: 'already open' must not mean 'already
    handled'. A score that deserves a higher tier goes through the full path."""
    from services import escalation_service as esc

    _chart_vitals(world["patient"], DETERIORATING, T0 - timedelta(minutes=5))
    opened = esc.scan_new_observations(department=world["dept"], now=T0)[0]
    assert opened["current_tier"] == 1

    _chart_vitals(world["patient"], PERI_ARREST, T0 - timedelta(minutes=1))
    esc.scan_new_observations(department=world["dept"], now=T0 + timedelta(minutes=1))

    now_at = esc.get_escalation(opened["id"])
    assert now_at["current_tier"] == 2
    assert now_at["peak_news2"] == _news2(PERI_ARREST)["raw_news2"]


def test_a_worse_score_at_the_same_entry_tier_still_reaches_the_chain(world):
    """THE NEWS2 19 THAT VANISHED. Measured on the appliance 2026-08-13: a nurse
    charted NEWS2 19 and the escalation board showed 10 for the same patient in
    the same minute, with an unrelated monitor reading's drivers.

    The swallowing branch tested "does this score deserve a HIGHER TIER than the
    chain is on?" — and tier 2 is the highest tier the policy defines an
    ``entry_min_news2`` for. So on a patient with an open tier-2 chain, EVERY
    score from 7 to 20 mapped to tier 2, compared ``<= 2``, and was dropped:
    no chain event, no peak update, and the watermark advanced past the row so
    it could never be reconsidered. The chain went on displaying the milder
    score, which is the number a registrar triages from.

    Deliberately asserted on PEAK and on the recorded step rather than on the
    tier: at this entry tier there is no higher tier to climb to, which is
    exactly why the tier comparison alone could not see the deterioration.
    """
    from services import escalation_service as esc

    _chart_vitals(world["patient"], CRITICAL_9, T0 - timedelta(minutes=5))
    opened = esc.scan_new_observations(department=world["dept"], now=T0)[0]
    assert opened["current_tier"] == 2, "fixture drifted: CRITICAL_9 must enter at tier 2"
    assert opened["peak_news2"] == _news2(CRITICAL_9)["raw_news2"] == 9
    steps_before = len(esc.get_escalation(opened["id"])["events"])

    worse = _news2(PERI_ARREST)["raw_news2"]
    assert worse > 9, "fixture drifted: the second reading must be strictly worse"
    assert esc._entry_tier(esc.tier_policy(), worse) == opened["current_tier"], (
        "this test is only meaningful while the worse score enters at the SAME "
        "tier — that identity is the whole defect")

    _chart_vitals(world["patient"], PERI_ARREST, T0 - timedelta(minutes=1))
    esc.scan_new_observations(department=world["dept"], now=T0 + timedelta(minutes=1))

    chain = esc.get_escalation(opened["id"])
    assert chain["peak_news2"] == worse, (
        f"a NEWS2 {worse} was swallowed by an open tier-2 chain — the board still "
        f"reads {chain['peak_news2']}")
    assert chain["news2"] == worse, "the chain is displaying a stale, milder score"
    assert len(chain["events"]) > steps_before, (
        "the deterioration left no step on the chain: "
        + str([e["event_type"] for e in chain["events"]]))


def test_the_sweep_never_reaches_another_ward(world):
    from services import escalation_service as esc
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                   diastolic_bp, spo2, temperature, respiratory_rate, consciousness,
                   on_oxygen, source)
               VALUES (?, ?, ?, ?, 70, ?, ?, ?, 'alert', 0, 'nurse_manual')""",
            (world["other_patient"], (T0 - timedelta(minutes=2)).isoformat(),
             PERI_ARREST["heart_rate"], PERI_ARREST["systolic_bp"], PERI_ARREST["spo2"],
             PERI_ARREST["temperature"], PERI_ARREST["respiratory_rate"]),
        )
    assert esc.scan_new_observations(department=world["dept"], now=T0) == []
    assert esc.list_escalations(department=world["dept"]) == []


# ═════════════════════════════════════════════════════════════════════════════
# 8. Routes — ward boundary, RBAC, audit
# ═════════════════════════════════════════════════════════════════════════════
def test_the_board_lists_only_the_callers_ward(world, client):
    from services import escalation_service as esc
    esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    esc.evaluate_observation(world["other_patient"], _news2(PERI_ARREST), now=T0)

    r = client.get("/api/vigil/escalations", headers=_auth(world["nurse"], "nurse"))
    assert r.status_code == 200
    ids = {e["patient_id"] for e in r.json()["escalations"]}
    assert world["patient"] in ids
    assert world["other_patient"] not in ids, "cross-ward escalation leaked onto the board"


def test_a_foreign_escalation_is_404_and_never_403(world, client):
    from services import escalation_service as esc
    other = esc.evaluate_observation(world["other_patient"], _news2(PERI_ARREST), now=T0)
    hdr = _auth(world["nurse"], "nurse")

    assert client.get(f"/api/vigil/escalations/{other['id']}", headers=hdr).status_code == 404
    assert client.post(f"/api/vigil/escalations/{other['id']}/acknowledge",
                       json={}, headers=hdr).status_code == 404
    assert client.post(f"/api/vigil/escalations/{other['id']}/resolve",
                       json={"reason": "not mine"}, headers=hdr).status_code == 404
    # And it is genuinely still open — the refusal changed nothing.
    assert esc.get_escalation(other["id"])["status"] == "open"


def test_a_pharmacist_cannot_see_or_acknowledge_a_deterioration(world, client):
    from services import escalation_service as esc
    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    pharm = _mk_user("pharmacist", world["dept"], "PHA")
    hdr = _auth(pharm, "pharmacist")
    assert client.get("/api/vigil/escalations", headers=hdr).status_code == 403
    assert client.post(f"/api/vigil/escalations/{out['id']}/acknowledge",
                       json={}, headers=hdr).status_code == 403


def test_acknowledge_and_resolve_through_the_api(world, client):
    from services import escalation_service as esc
    from database import verify_audit_chain

    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    hdr = _auth(world["registrar"], "doctor")

    ack = client.post(f"/api/vigil/escalations/{out['id']}/acknowledge",
                      json={"note": "on my way"}, headers=hdr)
    assert ack.status_code == 200
    assert ack.json()["status"] == "acknowledged"
    assert ack.json()["acknowledged_by"] == world["registrar"]

    bad = client.post(f"/api/vigil/escalations/{out['id']}/resolve",
                      json={"reason": "  "}, headers=hdr)
    assert bad.status_code == 400

    done = client.post(f"/api/vigil/escalations/{out['id']}/resolve",
                       json={"reason": "Seen, ABG sent, plan documented",
                             "category": "reviewed_treated"}, headers=hdr)
    assert done.status_code == 200
    assert done.json()["status"] == "resolved"

    with db_session() as db:
        rows = db.execute(
            "SELECT action_type FROM audit_logs WHERE patient_id = ? "
            "AND action_type LIKE 'escalation_%'", (world["patient"],)
        ).fetchall()
    kinds = {r["action_type"] for r in rows}
    assert {"escalation_raised", "escalation_acknowledged", "escalation_resolved"} <= kinds
    assert verify_audit_chain()["ok"] is True


def test_reading_the_board_advances_a_tier_that_fell_due(world, client):
    """The ward board is the thing a nurse actually has open. A chain must not
    sit at tier 1 forever merely because no background ticker is configured."""
    from services import escalation_service as esc
    out = esc.evaluate_observation(
        world["patient"], _news2(DETERIORATING),
        now=datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    assert out["current_tier"] == 1
    r = client.get("/api/vigil/escalations", headers=_auth(world["nurse"], "nurse"))
    assert r.status_code == 200
    mine = [e for e in r.json()["escalations"] if e["id"] == out["id"]][0]
    assert mine["current_tier"] == 3, "the read did not advance an overdue chain"


def test_the_board_read_is_audited_as_a_phi_disclosure(world, client):
    from services import escalation_service as esc
    esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    client.get("/api/vigil/escalations", headers=_auth(world["nurse"], "nurse"))
    with db_session() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE action_type = "
            "'escalation_board_viewed' AND actor_user_id = ?", (world["nurse"],)
        ).fetchone()
    assert row["n"] >= 1


def test_the_detail_view_returns_the_whole_recorded_chain(world, client):
    from services import escalation_service as esc
    out = esc.evaluate_observation(world["patient"], _news2(DETERIORATING), now=T0)
    esc.advance_due(now=T0 + timedelta(minutes=11))
    r = client.get(f"/api/vigil/escalations/{out['id']}",
                   headers=_auth(world["registrar"], "doctor"))
    assert r.status_code == 200
    body = r.json()
    # Delivery bookkeeping ('notified' / 'notify_failed') is interleaved with the
    # clinical steps; the CHAIN steps must still read in order.
    chain_steps = [e["event_type"] for e in body["events"]
                   if e["event_type"] in ("raised", "tier_advanced", "acknowledged",
                                          "resolved", "reopened", "observation")]
    assert chain_steps == ["raised", "tier_advanced"]
    assert body["current_tier"] == 2
