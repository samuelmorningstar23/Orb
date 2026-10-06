"""Orb — Clinical escalation chain with acknowledgement.

WHAT THIS FIXES
---------------
A nurse-charted deterioration reached NOBODY. Orb scored it correctly — NEWS2 is
computed identically on every vitals path — and then the only durable
consequences were a ``vitals`` row and, on the manual path, one line posted into
the patient's Relay case room. Nothing recorded that a human had seen it,
nothing happened when no human did, and a line that scrolls off a busy channel
is indistinguishable from a line that was answered. Manual entry is the ONLY
vitals path that exists when a monitor VLAN is down (main.py's lifespan calls it
"the ward's redundancy layer"), so the path with no escalation was the path the
ward falls back to.

WHAT AN ESCALATION IS HERE
  * raised when NEWS2 crosses the site's threshold,
  * addressed to a TIER (ward nurse → registrar → consultant), each with a
    timeout,
  * OPEN until a named human acknowledges it, recording who and when,
  * climbing to the next tier when the timeout passes unacknowledged,
  * closed only by an explicit resolution WITH A REASON,
  * and — the whole point — VISIBLE FOREVER while it is neither acknowledged nor
    resolved. Nothing in this module retires a chain on the clock. A silently
    dropped deterioration alert is the failure mode this exists to end, so time
    can only ever make an escalation louder, never quieter.

THE SCORE IS NOT OURS
``services.vigil_service.compute_news2`` is the single implementation of the RCP
table and this module never re-derives it. Callers hand in the dict compute_news2
returned; the scan path reads the observation row and calls compute_news2 itself,
exactly as ``vigil_service.get_patient_risk`` does. A second scorer is how a
previewed score and a recorded score diverge, which is how a ward stops trusting
the number.

TIME IS INJECTED, NEVER SLEPT
Every public function takes ``now: datetime | None``. Production passes nothing
and gets ``datetime.now(timezone.utc)``; tests pass an instant and walk the
ladder in microseconds. There is no module-level mutable clock, so two tests can
never contaminate each other through it.

═══════════════════════════════════════════════════════════════════════════════
WHAT ORB DOES NOT HAVE, AND WHAT THAT COSTS — read this before trusting a target
═══════════════════════════════════════════════════════════════════════════════
Orb has a ROSTER (``users``: id, name, role, department) and an ATTENDING
(``patients.attending_doctor_id`` / ``admissions.attending_doctor_id``). It has
NO DUTY MODEL. Specifically, none of the following exists anywhere in the schema
and none of it is invented here:

  * no on-call table, no rota, no shift calendar — nothing says who is covering
    this ward at 03:00 on a Sunday;
  * no grade or seniority column — ``users.role`` is one of five values, so
    "registrar" is not a thing Orb can look up. There is no way to distinguish a
    first-year from a consultant except by whether they are named as attending;
  * no team or firm membership — a patient belongs to a department and to one
    attending, not to a team with a hierarchy;
  * no contact channel — no bleep, pager, phone or push token. Delivery here is
    an in-app Relay post plus the escalation board. If nobody has Orb open, the
    escalation is visible but not intrusive;
  * no acceptance/handover of cover — a doctor cannot tell Orb "I am holding the
    bleep for General Medicine tonight".

So a tier resolves to the BEST AVAILABLE approximation, and says so in
``target_description`` rather than pretending:

  nurse       → the nurse who charted the observation if we were told who that
                was, else a nurse in the patient's department, else the role
                itself ("Any nurse on General Medicine").
  registrar   → a doctor in the patient's department who is NOT the named
                attending (the closest Orb can get to "the junior covering the
                ward"), else the attending, else the role itself.
  consultant  → the admission's attending doctor, else the patient's attending,
                else a doctor in the department, else the role itself.

When no individual is derivable ``target_user_id`` is NULL and the escalation is
addressed to a ROLE ON A WARD. That is not a failure state: it stays on the board
of everyone holding that role in that department. An escalation must never
silently target nobody, which is what a NULL with no description would mean.

WHAT WOULD MAKE THIS CORRECT rather than approximate: one ``duty_assignments``
table (department, role/tier, user, from, to) written by the existing roster
importer, and a contact channel per user. Both are additive; ``_derive_target``
is the single function that would change.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from database import db_session
from db_backend import DBIntegrityError
from permissions import PATIENT_DEPARTMENT_SQL
from services.audit_service import log_audit_event
from services.vigil_service import compute_news2
from services.news2_text import news2_description

logger = logging.getLogger("orb.escalation")


# ═════════════════════════════════════════════════════════════════════════════
#  Per-site policy
# ═════════════════════════════════════════════════════════════════════════════
# The founder's chain: nurse → registrar → consultant, timed, each step recorded.
# Timings and tiers are per-site because ward geography decides them: a 40-bed
# open ward with one registrar on site is not a 12-bed HDU.
#
#   role              — which resolver derives the human (see _derive_target).
#                       An unrecognised role is legal: it addresses the role by
#                       name with no individual, which is honest rather than
#                       wrong.
#   timeout_minutes   — unacknowledged for this long → next tier. null on the
#                       LAST tier, which has nowhere left to go and therefore
#                       waits indefinitely, still open, still visible.
#   entry_min_news2   — a chain ENTERS at the highest tier whose entry score it
#                       meets. NEWS2 >= 7 is the RCP emergency-response band, so
#                       by default it does not spend ten minutes waiting on the
#                       ward-nurse tier first. null = never an entry tier.
_DEFAULT_TIER_POLICY: list[dict] = [
    {"tier": 1, "role": "nurse", "label": "Ward nurse",
     "timeout_minutes": 10, "entry_min_news2": 5},
    {"tier": 2, "role": "registrar", "label": "Duty registrar",
     "timeout_minutes": 15, "entry_min_news2": 7},
    {"tier": 3, "role": "consultant", "label": "Consultant / attending",
     "timeout_minutes": None, "entry_min_news2": None},
]

_TRUE = ("1", "true", "yes", "on")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


def tier_policy() -> list[dict]:
    """The tier ladder for this site, from ``ORB_ESCALATION_TIERS`` (JSON list).

    READ AT CALL TIME, not at import: an appliance operator changing the ladder
    should not need a restart to stop the next deterioration climbing on the old
    timings, and a test must be able to set it per case.

    A MALFORMED POLICY FALLS BACK TO THE DEFAULT AND LOGS LOUDLY. Refusing to
    escalate because someone fat-fingered a .env would convert a config typo into
    the exact clinical failure this module exists to prevent. The fallback is
    reported (``policy_error``) rather than hidden.
    """
    raw = os.getenv("ORB_ESCALATION_TIERS")
    if not raw or not raw.strip():
        return [dict(t) for t in _DEFAULT_TIER_POLICY]
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("ORB_ESCALATION_TIERS must be a non-empty JSON list")
        out: list[dict] = []
        for i, entry in enumerate(parsed, start=1):
            if not isinstance(entry, dict):
                raise ValueError(f"tier {i} is not an object")
            role = str(entry.get("role") or "").strip()
            if not role:
                raise ValueError(f"tier {i} has no role")
            timeout = entry.get("timeout_minutes")
            if timeout is not None:
                timeout = float(timeout)
                if timeout <= 0:
                    raise ValueError(f"tier {i} timeout_minutes must be > 0 or null")
            entry_min = entry.get("entry_min_news2")
            if entry_min is not None:
                entry_min = int(entry_min)
            out.append({
                "tier": i,
                "role": role,
                "label": str(entry.get("label") or role.title()),
                "timeout_minutes": timeout,
                "entry_min_news2": entry_min,
            })
        # The last tier must never auto-advance — there is nowhere to advance to,
        # and a due date with no successor would make the chain look answerable
        # by waiting.
        out[-1]["timeout_minutes"] = None
        return out
    except Exception as e:
        logger.error(
            "ORB_ESCALATION_TIERS is invalid (%s) — falling back to the default "
            "nurse→registrar→consultant ladder so deteriorations keep escalating.",
            e,
        )
        return [dict(t) for t in _DEFAULT_TIER_POLICY]


def news2_threshold() -> int:
    """Aggregate NEWS2 at or above which a chain is raised.

    Default 5 = the RCP urgent-response threshold ("registered nurse to urgently
    inform the medical team"). A site that finds 5 too noisy raises it in
    ``ORB_ESCALATION_NEWS2_THRESHOLD``; a site cannot silently lower the
    single-parameter rule, which is a separate flag below.
    """
    try:
        return int(os.getenv("ORB_ESCALATION_NEWS2_THRESHOLD", "5"))
    except ValueError:
        logger.error("ORB_ESCALATION_NEWS2_THRESHOLD is not an integer — using 5")
        return 5


def _honour_single_param_3() -> bool:
    """RCP's single-parameter rule: any ONE vital scoring 3 warrants urgent
    review even when the aggregate is below the threshold. On by default because
    it is part of NEWS2, not an Orb embellishment."""
    return _env_flag("ORB_ESCALATION_ON_SINGLE_PARAM_3", True)


def _lookback_minutes() -> int:
    """How far back the safety-net scan looks for an unescalated observation.

    Bounds the one-time catch-up on first deploy: an appliance switched on today
    raises chains for patients deteriorating in the last two hours, not for every
    deterioration in the database's history.
    """
    try:
        return int(os.getenv("ORB_ESCALATION_SCAN_LOOKBACK_MINUTES", "120"))
    except ValueError:
        return 120


def _relay_notify_enabled() -> bool:
    return _env_flag("ORB_ESCALATION_RELAY_NOTIFY", True)


# ═════════════════════════════════════════════════════════════════════════════
#  Time
# ═════════════════════════════════════════════════════════════════════════════
def _now(now: datetime | None = None) -> datetime:
    """The injected clock. Aware UTC always — this DB stores both offset-aware
    ISO strings and naive SQLite CURRENT_TIMESTAMP values, and comparing the two
    shapes is how a timed ladder silently stops advancing."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
#  Qualification
# ═════════════════════════════════════════════════════════════════════════════
def qualifies(news2: dict | None) -> bool:
    """Does this NEWS2 result cross the site's escalation threshold?

    Reads compute_news2's OWN fields (``raw_news2``, ``single_param_3``). It does
    not look at the vitals, because the moment this function inspects a heart
    rate there are two scorers in the system.
    """
    if not news2:
        return False
    raw = news2.get("raw_news2", news2.get("raw_mews"))
    if raw is None:
        return False
    if int(raw) >= news2_threshold():
        return True
    return bool(news2.get("single_param_3")) and _honour_single_param_3()


def _entry_tier(policy: list[dict], raw_news2: int) -> int:
    """The tier a chain STARTS at, given how sick the patient is."""
    entry = 1
    for t in policy:
        floor = t.get("entry_min_news2")
        if floor is not None and raw_news2 >= floor:
            entry = max(entry, int(t["tier"]))
    return min(entry, len(policy))


def _tier_spec(policy: list[dict], tier: int) -> dict:
    for t in policy:
        if int(t["tier"]) == int(tier):
            return t
    return policy[-1]


# ═════════════════════════════════════════════════════════════════════════════
#  Targeting — see the module docstring for everything Orb does not have
# ═════════════════════════════════════════════════════════════════════════════
_DOCTOR_ROLES = ("doctor", "clinician")


def _patient_context(db, patient_id: str) -> dict:
    """Department + attending, both taken from the ACTIVE ADMISSION first.

    Department comes from ``admissions.department`` and never from
    ``patients.ward`` — ward is a free-text bed prefix ("GM-1"), department is
    the clinical service ("General Medicine"), and confusing the two is how a
    scope stops scoping.
    """
    row = db.execute(
        f"""SELECT p.id, p.name, p.bed, p.ward, p.attending_doctor_id,
                   p.on_scale_2, p.status,
                   {PATIENT_DEPARTMENT_SQL} AS department,
                   (SELECT a.attending_doctor_id FROM admissions a
                     WHERE a.patient_id = p.id
                     ORDER BY (a.status = 'active') DESC, a.created_at DESC, a.rowid DESC
                     LIMIT 1) AS admission_attending
              FROM patients p WHERE p.id = ?""",
        (patient_id,),
    ).fetchone()
    if row is None:
        return {}
    return {
        "id": row["id"],
        "name": row["name"],
        "bed": row["bed"],
        "ward": row["ward"],
        "department": row["department"],
        "attending": row["admission_attending"] or row["attending_doctor_id"],
        "on_scale_2": bool(row["on_scale_2"]) if row["on_scale_2"] is not None else False,
        "status": row["status"],
    }


def _user_with_role(db, user_id: str | None, roles: tuple[str, ...]) -> str | None:
    if not user_id:
        return None
    marks = ",".join("?" * len(roles))
    row = db.execute(
        f"SELECT id FROM users WHERE id = ? AND role IN ({marks})",
        (user_id, *roles),
    ).fetchone()
    return row["id"] if row else None


def _first_in_department(db, roles: tuple[str, ...], department: str | None,
                         exclude: str | None = None) -> str | None:
    """A deterministic pick, not a fair one.

    ORDER BY id makes the same ward always page the same person, which is wrong
    as a duty model and right as a placeholder: it is stable, testable, and
    obviously not a rota, so nobody mistakes it for one. Returns None rather than
    reaching outside the department — paging a doctor who has never seen this
    ward is worse than addressing the role.
    """
    if not department:
        return None
    marks = ",".join("?" * len(roles))
    params: list = [*roles, department]
    sql = f"SELECT id FROM users WHERE role IN ({marks}) AND department = ?"
    if exclude:
        sql += " AND id != ?"
        params.append(exclude)
    sql += " ORDER BY id LIMIT 1"
    row = db.execute(sql, tuple(params)).fetchone()
    return row["id"] if row else None


def _derive_target(db, ctx: dict, role: str, label: str,
                   charting_actor_id: str | None) -> tuple[str | None, str]:
    """(target_user_id, human description) for one tier.

    THE SINGLE FUNCTION A REAL DUTY MODEL WOULD REPLACE. Everything it can do is
    an approximation over ``users`` + the attending; see the module docstring for
    the exhaustive list of what Orb does not know.
    """
    dept = ctx.get("department")
    attending = ctx.get("attending")
    role_key = (role or "").strip().lower()
    where = f" on {dept}" if dept else " (patient has no department on record)"

    if role_key == "nurse":
        # Prefer the person who took the observation: they are at the bedside and
        # they are the one who can act first. Only the manual path knows this;
        # a monitor feed does not record a human, and `vitals` has no author
        # column at all (the audit trail has it, keyed by vitals_id — reaching
        # into the hash chain to page someone would be worse than this).
        target = _user_with_role(db, charting_actor_id, ("nurse",))
        if target:
            return target, f"{label} (recorded this observation){where}"
        target = _first_in_department(db, ("nurse",), dept)
        if target:
            return target, f"{label}{where} (no duty roster, first nurse on the ward roster)"
        return None, f"Any nurse{where}, no named nurse on the roster for this ward"

    if role_key == "consultant":
        target = _user_with_role(db, attending, _DOCTOR_ROLES)
        if target:
            return target, f"{label}, named attending{where}"
        target = _first_in_department(db, _DOCTOR_ROLES, dept)
        if target:
            return target, f"{label}{where} (no attending on record, ward doctor)"
        return None, f"Any consultant{where}, no attending and no ward doctor on the roster"

    if role_key == "registrar":
        # Orb has no grade column, so "registrar" cannot be looked up. The closest
        # available meaning is "a doctor on this ward who is not the consultant".
        target = _first_in_department(db, _DOCTOR_ROLES, dept, exclude=attending)
        if target:
            return target, f"{label}{where} (no grade in the roster, ward doctor who is not the attending)"
        target = _user_with_role(db, attending, _DOCTOR_ROLES)
        if target:
            return target, f"{label}{where}, only the attending is on this ward"
        return None, f"Any doctor{where}, no doctor on the roster for this ward"

    # A site-defined tier Orb has no resolver for. Address the role by name; do
    # not guess a person.
    return None, f"Any {role}{where}: Orb has no roster mapping for this tier"


# ═════════════════════════════════════════════════════════════════════════════
#  Row → dict
# ═════════════════════════════════════════════════════════════════════════════
_ESCALATION_FIELDS = (
    "id", "patient_id", "raised_in_department", "status", "trigger", "news2",
    "peak_news2", "level", "single_param_3", "drivers_json", "vitals_id",
    "last_vitals_id", "source", "current_tier", "max_tier", "tier_role",
    "tier_label", "tier_entered_at", "tier_due_at", "target_user_id",
    "target_description", "raised_at", "acknowledged_at", "acknowledged_by",
    "acknowledged_tier", "resolved_at", "resolved_by", "resolution_reason",
    "resolution_category", "policy_json", "updated_at",
)


def _to_dict(row, now: datetime | None = None) -> dict:
    out = {f: row[f] for f in _ESCALATION_FIELDS}
    out["drivers"] = json.loads(out["drivers_json"]) if out["drivers_json"] else []
    raised = _parse_ts(out["raised_at"])
    ref = _now(now)
    out["age_minutes"] = int((ref - raised).total_seconds() // 60) if raised else None
    ack = _parse_ts(out["acknowledged_at"])
    out["minutes_to_acknowledge"] = (
        int((ack - raised).total_seconds() // 60) if ack and raised else None
    )
    # The single field a board should sort on: an unanswered chain is the only
    # kind that can kill somebody.
    out["awaiting_acknowledgement"] = out["status"] == "open"
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Event recording — append-only, one row per step
# ═════════════════════════════════════════════════════════════════════════════
def _record_event(db, escalation_id: str, event_type: str, occurred_at: datetime,
                  *, tier=None, tier_role=None, target_user_id=None,
                  target_description=None, actor_user_id=None, actor_role=None,
                  news2=None, detail=None, metadata=None) -> int:
    seq_row = db.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM escalation_events "
        "WHERE escalation_id = ?", (escalation_id,),
    ).fetchone()
    seq = seq_row["next"]
    db.execute(
        """INSERT INTO escalation_events
             (escalation_id, seq, event_type, tier, tier_role, target_user_id,
              target_description, actor_user_id, actor_role, news2, detail,
              metadata_json, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (escalation_id, seq, event_type, tier, tier_role, target_user_id,
         target_description, actor_user_id, actor_role, news2, detail,
         json.dumps(metadata) if metadata else None, occurred_at.isoformat()),
    )
    return seq


def _audit(db, action_type: str, summary: str, escalation_id: str,
           patient_id: str, *, actor_id=None, actor_role=None, metadata=None) -> None:
    """Fail-closed clinical audit, written INSIDE the caller's transaction.

    ``clinical_access`` is one of the fail-closed categories, so an escalation
    state change that cannot be audited does not commit. That is the same rule
    the eMAR and the observation write already hold themselves to: a clinical act
    with no trail is not a clinical act anyone can defend afterwards.
    """
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_access",
        action_type=action_type,
        outcome="success",
        db=db,
        summary_message=summary,
        actor_user_id=actor_id,
        actor_role=actor_role,
        target_type="escalation",
        target_id=escalation_id,
        patient_id=patient_id,
        metadata=metadata or {},
    )


# ═════════════════════════════════════════════════════════════════════════════
#  Raising / extending a chain
# ═════════════════════════════════════════════════════════════════════════════
def _scoring_enabled() -> bool:
    """ORB_SCORING_ENABLED, read at call time through vigil_service, so the
    Ward Record profile (ORB_WARD_RECORD) switches it off here too."""
    from services.vigil_service import scoring_enabled
    return scoring_enabled()


def evaluate_observation(
    patient_id: str,
    news2: dict,
    *,
    vitals_id: int | None = None,
    source: str | None = None,
    actor_id: str | None = None,
    observed_at: datetime | str | None = None,
    now: datetime | None = None,
    notify: bool | None = None,
) -> dict | None:
    """THE ENTRY POINT for any vitals path. Returns the chain, or None.

    Call this POST-COMMIT from whatever wrote the observation, handing it the
    dict ``compute_news2`` returned for that observation. It never raises for a
    delivery problem and it never re-scores.

    IDEMPOTENT BY CONSTRUCTION. A patient has at most one chain that is not
    resolved (enforced by a partial UNIQUE index, not by this function). A
    qualifying observation on a patient who already has one EXTENDS it: the
    observation is recorded on the chain, the peak score is updated, and the
    chain is bumped to a higher tier only if the new score deserves one. Five
    deteriorating sets in a row are one escalation with five recorded steps, not
    five acknowledgements a registrar has to clear.

    An observation BELOW the threshold does nothing at all — it neither raises
    nor closes anything. Improving vitals are not evidence that a human answered
    the alert, and resolution is a human act with a reason attached.

    Raises nothing while scoring is switched off on this computer
    (ORB_SCORING_ENABLED): no score is computed, so no chain may open on one.
    """
    if not _scoring_enabled():
        return None
    if not qualifies(news2):
        return None

    ts = _now(now)
    observed = _parse_ts(observed_at) or ts
    policy = tier_policy()
    raw = int(news2.get("raw_news2", news2.get("raw_mews") or 0))
    level = news2.get("level", "elevated")
    drivers = json.dumps(news2.get("drivers") or [])
    single3 = 1 if news2.get("single_param_3") else 0
    # The one NEWS2 label (news2_text), built from the same raw/level the
    # chain row stores, for every detail, audit and Relay line below.
    label = news2_description({**news2, "raw_news2": raw, "level": level})
    want_tier = _entry_tier(policy, raw)

    notified_payload = None
    with db_session() as db:
        ctx = _patient_context(db, patient_id)
        if not ctx:
            logger.error("escalation: no such patient %s — nothing raised", patient_id)
            return None

        existing = db.execute(
            "SELECT * FROM clinical_escalations WHERE patient_id = ? AND status != 'resolved'",
            (patient_id,),
        ).fetchone()

        if existing is not None:
            eid = existing["id"]
            new_peak = max(int(existing["peak_news2"] or 0), raw)
            # Does this observation deserve a higher tier than the chain is on?
            bump = want_tier > int(existing["current_tier"])
            if bump:
                spec = _tier_spec(policy, want_tier)
                target_id, target_desc = _derive_target(
                    db, ctx, spec["role"], spec["label"], actor_id)
                due = (ts + timedelta(minutes=spec["timeout_minutes"])
                       if spec["timeout_minutes"] is not None else None)
                # THE ACKNOWLEDGEMENT IS CLEARED, and that matters more than it
                # looks. `acknowledged_by` is what a board renders next to the
                # chain; leaving the old one on a chain that has just re-opened
                # unanswered would print "acknowledged by Dr X" beside a
                # deterioration nobody has yet taken — misleading in precisely
                # the direction that gets a patient missed. "Acknowledged" must
                # always mean the CURRENT state has been acknowledged. The
                # earlier acknowledgement is not lost: it is a row in
                # escalation_events, which is where history belongs.
                db.execute(
                    """UPDATE clinical_escalations
                          SET status = 'open', current_tier = ?, tier_role = ?,
                              tier_label = ?, tier_entered_at = ?, tier_due_at = ?,
                              target_user_id = ?, target_description = ?,
                              news2 = ?, peak_news2 = ?, level = ?,
                              single_param_3 = ?, drivers_json = ?,
                              last_vitals_id = COALESCE(?, last_vitals_id),
                              acknowledged_at = NULL, acknowledged_by = NULL,
                              acknowledged_tier = NULL,
                              updated_at = ?
                        WHERE id = ?""",
                    (want_tier, spec["role"], spec["label"], ts.isoformat(),
                     due.isoformat() if due else None, target_id, target_desc,
                     raw, new_peak, level, single3, drivers, vitals_id,
                     ts.isoformat(), eid),
                )
                # 'reopened' rather than 'tier_advanced': the cause is the
                # patient getting worse, not the clock running out, and an
                # acknowledged chain that deteriorates needs acknowledging again.
                # Acknowledgement must not become a mute button.
                _record_event(
                    db, eid, "reopened", ts, tier=want_tier, tier_role=spec["role"],
                    target_user_id=target_id, target_description=target_desc,
                    news2=raw, detail=(
                        f"{label} while the chain was at tier "
                        f"{existing['current_tier']} / {existing['status']}, "
                        f"re-opened at tier {want_tier} ({spec['label']})"
                    ),
                    metadata={"vitals_id": vitals_id, "source": source,
                              "previous_status": existing["status"]},
                )
                _audit(db, "escalation_reopened",
                       f"Escalation {eid} re-opened at tier {want_tier} for "
                       f"{patient_id} ({label})",
                       eid, patient_id, actor_id=actor_id,
                       metadata={"tier": want_tier, "news2": raw, "level": level,
                                 "vitals_id": vitals_id, "source": source})
                notified_payload = (eid, patient_id, raw, level, want_tier,
                                    spec["label"], target_desc)
            else:
                db.execute(
                    """UPDATE clinical_escalations
                          SET news2 = ?, peak_news2 = ?, level = ?,
                              single_param_3 = ?, drivers_json = ?,
                              last_vitals_id = COALESCE(?, last_vitals_id),
                              updated_at = ?
                        WHERE id = ?""",
                    (raw, new_peak, level, single3, drivers, vitals_id,
                     ts.isoformat(), eid),
                )
                _record_event(
                    db, eid, "observation", observed, tier=existing["current_tier"],
                    tier_role=existing["tier_role"], news2=raw,
                    actor_user_id=actor_id,
                    detail=f"Further qualifying observation: {label}",
                    metadata={"vitals_id": vitals_id, "source": source},
                )
            row = db.execute(
                "SELECT * FROM clinical_escalations WHERE id = ?", (eid,)).fetchone()
            result = _to_dict(row, ts)
        else:
            eid = f"ESC{uuid.uuid4().hex[:12].upper()}"
            spec = _tier_spec(policy, want_tier)
            target_id, target_desc = _derive_target(
                db, ctx, spec["role"], spec["label"], actor_id)
            due = (ts + timedelta(minutes=spec["timeout_minutes"])
                   if spec["timeout_minutes"] is not None else None)
            try:
                db.execute(
                    """INSERT INTO clinical_escalations
                         (id, patient_id, raised_in_department, status, trigger,
                          news2, peak_news2, level, single_param_3, drivers_json,
                          vitals_id, last_vitals_id, source, current_tier, max_tier,
                          tier_role, tier_label, tier_entered_at, tier_due_at,
                          target_user_id, target_description, raised_at,
                          policy_json, updated_at)
                       VALUES (?, ?, ?, 'open', 'news2', ?, ?, ?, ?, ?, ?, ?, ?,
                               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (eid, patient_id, ctx.get("department"), raw, raw, level,
                     single3, drivers, vitals_id, vitals_id, source, want_tier,
                     len(policy), spec["role"], spec["label"], ts.isoformat(),
                     due.isoformat() if due else None, target_id, target_desc,
                     ts.isoformat(), json.dumps(policy), ts.isoformat()),
                )
            except DBIntegrityError:
                # Another writer opened the chain between our SELECT and this
                # INSERT (a monitor push and a manual set landing in the same
                # second is routine). The index decided; take their row rather
                # than raising into a caller that has already committed a
                # reading.
                logger.info(
                    "escalation: lost the race to open a chain for %s — extending "
                    "the winner instead", patient_id)
                row = db.execute(
                    "SELECT * FROM clinical_escalations WHERE patient_id = ? "
                    "AND status != 'resolved'", (patient_id,)).fetchone()
                if row is None:
                    raise
                _record_event(
                    db, row["id"], "observation", observed,
                    tier=row["current_tier"], tier_role=row["tier_role"],
                    news2=raw, actor_user_id=actor_id,
                    detail=f"Concurrent qualifying observation: {label}",
                    metadata={"vitals_id": vitals_id, "source": source})
                return _to_dict(row, ts)

            _record_event(
                db, eid, "raised", ts, tier=want_tier, tier_role=spec["role"],
                target_user_id=target_id, target_description=target_desc,
                actor_user_id=actor_id, news2=raw,
                detail=(f"{label} crossed the escalation threshold, "
                        f"opened at tier {want_tier} ({spec['label']})"),
                metadata={"vitals_id": vitals_id, "source": source,
                          "drivers": news2.get("drivers"),
                          "single_param_3": bool(single3),
                          "complete_observation_set": news2.get("complete"),
                          "missing_parameters": news2.get("missing_parameters")},
            )
            _audit(db, "escalation_raised",
                   f"Escalation {eid} raised for {patient_id}: {label}, "
                   f"tier {want_tier} {spec['label']}",
                   eid, patient_id, actor_id=actor_id,
                   metadata={"tier": want_tier, "news2": raw, "level": level,
                             "single_param_3": bool(single3), "vitals_id": vitals_id,
                             "source": source, "target_user_id": target_id,
                             "target_description": target_desc})
            row = db.execute(
                "SELECT * FROM clinical_escalations WHERE id = ?", (eid,)).fetchone()
            result = _to_dict(row, ts)
            notified_payload = (eid, patient_id, raw, level, want_tier,
                                spec["label"], target_desc)

    if notified_payload and (notify if notify is not None else _relay_notify_enabled()):
        _notify(*notified_payload, now=ts, label=label,
                climbs=bool(result and result.get("tier_due_at")))
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Delivery
# ═════════════════════════════════════════════════════════════════════════════
def _notify(escalation_id, patient_id, raw, level, tier, tier_label,
            target_description, *, now: datetime | None = None,
            label: str | None = None, climbs: bool = True) -> bool:
    """Post the escalation into the patient's Relay case room.

    POST-COMMIT AND NON-RAISING, for the two reasons nurse_shift_service already
    documents: ``_persist_relay_alert`` opens its own connection and would block
    on a WAL lock taken inside our write transaction, and a delivery failure must
    never roll back an escalation that is already recorded.

    THIS IS NOT A PAGE. Orb has no bleep, pager, phone or push channel (see the
    module docstring). A Relay post plus the escalation board is the whole of
    delivery today: loud if someone has Orb open, silent if nobody does. The
    board is what makes the escalation impossible to lose; this only makes it
    faster to notice.

    ``label`` is the observation's full NEWS2 label (news2_text), passed when
    the caller holds the scored result. The tier-timeout sweep holds only the
    chain row, so it falls back to "NEWS2 n (level)" built from raw and level.

    ``climbs`` is False on a tier with no timeout (the last one), so the post
    does not promise a next tier that does not exist.
    """
    ts = _now(now)
    # The post names only what a reader can do today. No screen can acknowledge
    # a chain yet (the board is API-only), and "acknowledge it in Vigil" sent
    # staff looking for a control that is not there.
    next_step = (
        "This alert stays open and moves to the next tier automatically when "
        "this tier's time runs out."
        if climbs else
        "This alert stays open. It is at the last tier, so it does not move on."
    )
    content = (
        f"🔺 ESCALATION [TIER {tier}: {tier_label}]: "
        f"{label or news2_description({'raw_news2': raw, 'level': level})}\n"
        f"Addressed to: {target_description}\n"
        f"Orb does not page or phone anyone. If this is addressed to you, review "
        f"the patient now; if not, phone them.\n"
        f"{next_step}\n"
        f"Reference {escalation_id}"
    )
    logged = False
    msg_data = None
    try:
        from services.ingestion_service import _persist_relay_alert
        logged, msg_id, channel_id, msg_data, _n = _persist_relay_alert(patient_id, content)
    except Exception as e:
        logger.error("escalation %s: Relay delivery FAILED for %s: %s",
                     escalation_id, patient_id, e)
        msg_id = channel_id = None

    # The row alone reached nobody until they refreshed: a tier post written by
    # the background sweep at 03:00 sat in the case room unseen by the person
    # who had that room open. Push it the same way the ingestion alert does.
    if logged and msg_data:
        _emit_message_new(msg_data, channel_id)

    try:
        with db_session() as db:
            _record_event(
                db, escalation_id,
                "notified" if logged else "notify_failed", ts, tier=tier,
                detail=(f"Posted to the patient's case room ({channel_id})" if logged
                        else "No case room for this patient. The escalation is on "
                             "the Vigil board only"),
                metadata={"channel_id": channel_id, "message_id": msg_id},
            )
    except Exception as e:  # pragma: no cover — bookkeeping must not raise
        logger.warning("escalation %s: could not record delivery outcome: %s",
                       escalation_id, e)

    if not logged:
        # Loud on purpose. A deterioration whose notification reached nobody must
        # not be invisible to whoever debugs the ward later.
        logger.error(
            "escalation %s: patient %s has NO case room — NEWS2 %s (%s) was not "
            "delivered to Relay; it is visible on the escalation board only",
            escalation_id, patient_id, raw, level)
    return logged


#: The app's event loop, captured by ``sweep_loop`` when it starts. ``_notify``
#: runs in a DB worker thread on the sweep path, where there is no running loop
#: and no anyio portal, so this is the only way that post can reach a socket.
_EMIT_LOOP: asyncio.AbstractEventLoop | None = None
#: Strong references to in-flight emits (the loop holds tasks only weakly).
_EMIT_PENDING: set = set()


def _emit_message_new(msg_data: dict, channel_id: str | None) -> bool:
    """Schedule ``message:new`` for a stored case-room post. Never raises.

    Three ways back to the loop, because ``_notify`` is reached from three
    places: on the loop itself, from a FastAPI ``def`` route (anyio worker), and
    from ``sweep_loop`` via run_db (a plain executor thread). With none of them
    (a script, a unit test) the message is already stored and shows on the next
    load, so skipping the push is safe.
    """
    if not channel_id:
        return False
    try:
        from ws.realtime import sio
    except Exception as e:  # pragma: no cover (import-time failure only)
        logger.warning("escalation: socket server unavailable, post not pushed: %s", e)
        return False

    def _start() -> bool:
        task = asyncio.get_running_loop().create_task(
            sio.emit("message:new", msg_data, room=channel_id))
        _EMIT_PENDING.add(task)
        task.add_done_callback(_EMIT_PENDING.discard)
        return True

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        try:
            return _start()
        except Exception as e:
            logger.warning("escalation: message:new push failed: %s", e)
            return False

    loop = _EMIT_LOOP
    if loop is not None and loop.is_running() and not loop.is_closed():
        try:
            fut = asyncio.run_coroutine_threadsafe(
                sio.emit("message:new", msg_data, room=channel_id), loop)
            _EMIT_PENDING.add(fut)
            fut.add_done_callback(_EMIT_PENDING.discard)
            return True
        except Exception as e:
            logger.warning("escalation: message:new push failed: %s", e)
            return False

    try:
        from anyio.from_thread import run_sync as _portal_run_sync
        return bool(_portal_run_sync(_start))
    except Exception as e:
        logger.info("escalation: no event loop reachable, post stored but not "
                    "pushed live (%s)", e)
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  The clock: unacknowledged → next tier
# ═════════════════════════════════════════════════════════════════════════════
def advance_due(*, now: datetime | None = None, limit: int = 500) -> list[dict]:
    """Advance every OPEN chain whose current tier has timed out. Returns the
    chains that moved.

    Deliberately NOT department-scoped. Advancing a tier creates no new chain and
    discloses nothing; scoping it would mean an escalation on a ward nobody
    happens to be looking at never climbs — which is the failure this module
    exists to prevent, reintroduced through a permissions detail.

    Acknowledged chains are skipped: acknowledgement is exactly the signal that a
    human has it, so the ladder stops. It does not close the chain (see
    ``resolve``) and it does not stop a WORSE observation re-opening it (see
    ``evaluate_observation``).
    """
    ts = _now(now)
    moved: list[dict] = []
    with db_session() as db:
        due = db.execute(
            """SELECT * FROM clinical_escalations
                WHERE status = 'open' AND tier_due_at IS NOT NULL
                  AND tier_due_at <= ?
                ORDER BY tier_due_at ASC LIMIT ?""",
            (ts.isoformat(), limit),
        ).fetchall()

        for row in due:
            policy = _load_policy(row)
            eid = row["id"]
            from_tier = int(row["current_tier"])
            tier = from_tier
            ctx = _patient_context(db, row["patient_id"])

            # ── PHASE 1: work out the walk. No writes yet. ──
            # One tier at a time, so EVERY step is recorded even when a chain has
            # been unattended for hours and crosses several timeouts at once.
            # Collapsing that into a single jump would hide exactly the thing an
            # incident review needs to see.
            steps: list[tuple] = []
            due_at = _parse_ts(row["tier_due_at"])
            while due_at is not None and due_at <= ts and tier < len(policy):
                tier += 1
                spec = _tier_spec(policy, tier)
                target_id, target_desc = _derive_target(
                    db, ctx, spec["role"], spec["label"], None)
                entered = due_at
                due_at = (entered + timedelta(minutes=spec["timeout_minutes"])
                          if spec["timeout_minutes"] is not None else None)
                steps.append((tier, spec, target_id, target_desc, entered, due_at))
            if not steps:
                continue

            # ── PHASE 2: CLAIM the move before recording it. ──
            # The SELECT above ran in autocommit (see database.db_session: a
            # transaction opens only before DML), so a clinician could have
            # acknowledged this chain in the gap. Writing the tier_advanced rows
            # first would leave an append-only history saying a chain escalated
            # past a human who had already taken it. The conditional UPDATE is a
            # single atomic statement guarded on BOTH the status and the tier we
            # read; rowcount 0 means somebody else got there and we record
            # nothing at all.
            _t, spec, target_id, target_desc, entered, due_at = steps[-1]
            claimed = db.execute(
                """UPDATE clinical_escalations
                      SET current_tier = ?, tier_role = ?, tier_label = ?,
                          tier_entered_at = ?, tier_due_at = ?,
                          target_user_id = ?, target_description = ?, updated_at = ?
                    WHERE id = ? AND status = 'open' AND current_tier = ?""",
                (tier, spec["role"], spec["label"], entered.isoformat(),
                 due_at.isoformat() if due_at else None, target_id, target_desc,
                 ts.isoformat(), eid, from_tier),
            )
            if getattr(claimed, "rowcount", 1) == 0:
                logger.info(
                    "escalation %s was acknowledged or advanced by someone else "
                    "while this sweep was deciding — leaving it alone", eid)
                continue

            # ── PHASE 3: record each step, now that the move is real. ──
            raised_at = _parse_ts(row["raised_at"])
            for s_tier, s_spec, s_target, s_desc, s_entered, _s_due in steps:
                _record_event(
                    db, eid, "tier_advanced", s_entered, tier=s_tier,
                    tier_role=s_spec["role"], target_user_id=s_target,
                    target_description=s_desc, news2=row["peak_news2"],
                    detail=(f"Not acknowledged at tier {s_tier - 1}, escalated to "
                            f"tier {s_tier} ({s_spec['label']})"),
                    metadata={"unacknowledged_minutes": int(
                        (s_entered - (raised_at or s_entered)).total_seconds() // 60)},
                )
                _audit(db, "escalation_tier_advanced",
                       f"Escalation {eid} for {row['patient_id']} was not "
                       f"acknowledged and escalated to tier {s_tier} "
                       f"({s_spec['label']})",
                       eid, row["patient_id"],
                       metadata={"tier": s_tier, "role": s_spec["role"],
                                 "target_user_id": s_target,
                                 "news2": row["peak_news2"]})

            fresh = db.execute(
                "SELECT * FROM clinical_escalations WHERE id = ?", (eid,)).fetchone()
            moved.append(_to_dict(fresh, ts))

    for m in moved:
        if _relay_notify_enabled():
            _notify(m["id"], m["patient_id"], m["peak_news2"], m["level"],
                    m["current_tier"], m["tier_label"], m["target_description"],
                    now=ts, climbs=m.get("tier_due_at") is not None)
    return moved


def _load_policy(row) -> list[dict]:
    """The ladder this chain climbs.

    Prefers the policy snapshotted when the chain was raised: a site that edits
    its timings mid-shift must not retroactively change why an old escalation
    advanced when it did. Falls back to the live policy if the snapshot is
    missing or unreadable.
    """
    raw = row["policy_json"] if "policy_json" in row.keys() else None
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return parsed
        except (ValueError, TypeError):
            logger.warning("escalation %s has an unreadable policy snapshot — "
                           "using the live policy", row["id"])
    return tier_policy()


# ═════════════════════════════════════════════════════════════════════════════
#  Acknowledgement and resolution
# ═════════════════════════════════════════════════════════════════════════════
class EscalationNotFound(LookupError):
    """No such chain. Routes turn this into 404, never 403."""


def acknowledge(escalation_id: str, user: dict, *, note: str | None = None,
                now: datetime | None = None) -> dict:
    """"I have seen this and I am dealing with it" — WHO and WHEN, on the record.

    Stops the tier ladder. Does NOT close the escalation: acknowledged is not
    finished, and a chain stays on the board until somebody says what was done
    (see ``resolve``). Acknowledging twice is a no-op that records the second
    attempt rather than overwriting the first — the first responder is the
    clinically interesting one.
    """
    ts = _now(now)
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM clinical_escalations WHERE id = ?", (escalation_id,)
        ).fetchone()
        if row is None:
            raise EscalationNotFound(escalation_id)
        if row["status"] == "resolved":
            raise ValueError("this escalation is already resolved")

        if row["status"] == "acknowledged":
            _record_event(
                db, escalation_id, "acknowledgement_repeated", ts,
                tier=row["current_tier"], tier_role=row["tier_role"],
                actor_user_id=(user or {}).get("id"),
                actor_role=(user or {}).get("role"),
                detail=f"Already acknowledged by {row['acknowledged_by']}",
            )
            return _to_dict(row, ts)

        db.execute(
            """UPDATE clinical_escalations
                  SET status = 'acknowledged', acknowledged_at = ?,
                      acknowledged_by = ?, acknowledged_tier = ?,
                      tier_due_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'open'""",
            (ts.isoformat(), (user or {}).get("id"), row["current_tier"],
             ts.isoformat(), escalation_id),
        )
        _record_event(
            db, escalation_id, "acknowledged", ts, tier=row["current_tier"],
            tier_role=row["tier_role"], actor_user_id=(user or {}).get("id"),
            actor_role=(user or {}).get("role"), news2=row["peak_news2"],
            detail=note or "Acknowledged",
        )
        _audit(db, "escalation_acknowledged",
               f"Escalation {escalation_id} for {row['patient_id']} acknowledged "
               f"at tier {row['current_tier']} by {(user or {}).get('id')}",
               escalation_id, row["patient_id"],
               actor_id=(user or {}).get("id"), actor_role=(user or {}).get("role"),
               metadata={"tier": row["current_tier"], "note": note,
                         "minutes_open": int(
                             (ts - (_parse_ts(row["raised_at"]) or ts)
                              ).total_seconds() // 60)})
        fresh = db.execute(
            "SELECT * FROM clinical_escalations WHERE id = ?", (escalation_id,)
        ).fetchone()
        return _to_dict(fresh, ts)


# Free text is kept, but a category makes the pilot able to count outcomes
# without reading prose. Unknown values are accepted and stored — refusing a
# clinician's wording is how a resolution box gets filled with "other".
RESOLUTION_CATEGORIES = (
    "reviewed_treated",      # seen, treatment started/changed
    "reviewed_no_change",    # seen, current plan is correct
    "escalated_to_icu",      # care moved somewhere else
    "artefact",              # the observation was wrong (cuff, probe, transcription)
    "patient_discharged",    # closed from the discharge screen, with its reason
    "patient_died",
    "other",
)

_MIN_REASON_CHARS = 3


def resolve(escalation_id: str, user: dict, *, reason: str | None,
            category: str | None = None, now: datetime | None = None) -> dict:
    """Close the chain, WITH A REASON. The reason is mandatory, by design.

    A resolve button with no reason is a dismiss button, and a dismiss button is
    how a deterioration alert becomes noise that a ward learns to clear without
    reading. The reason is also the only artefact that tells the next reviewer
    whether the escalation worked.
    """
    ts = _now(now)
    text = (reason or "").strip()
    if len(text) < _MIN_REASON_CHARS:
        raise ValueError(
            "a resolution reason is required. Say what was done, in a few words")
    cat = (category or "other").strip().lower() or "other"

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM clinical_escalations WHERE id = ?", (escalation_id,)
        ).fetchone()
        if row is None:
            raise EscalationNotFound(escalation_id)
        if row["status"] == "resolved":
            raise ValueError("this escalation is already resolved")

        db.execute(
            """UPDATE clinical_escalations
                  SET status = 'resolved', resolved_at = ?, resolved_by = ?,
                      resolution_reason = ?, resolution_category = ?,
                      tier_due_at = NULL, updated_at = ?
                WHERE id = ? AND status != 'resolved'""",
            (ts.isoformat(), (user or {}).get("id"), text, cat, ts.isoformat(),
             escalation_id),
        )
        _record_event(
            db, escalation_id, "resolved", ts, tier=row["current_tier"],
            tier_role=row["tier_role"], actor_user_id=(user or {}).get("id"),
            actor_role=(user or {}).get("role"), news2=row["peak_news2"],
            detail=text, metadata={"category": cat,
                                   "was_acknowledged": bool(row["acknowledged_at"])},
        )
        _audit(db, "escalation_resolved",
               f"Escalation {escalation_id} for {row['patient_id']} resolved by "
               f"{(user or {}).get('id')}: {text[:160]}",
               escalation_id, row["patient_id"],
               actor_id=(user or {}).get("id"), actor_role=(user or {}).get("role"),
               metadata={"category": cat, "tier": row["current_tier"],
                         "acknowledged": bool(row["acknowledged_at"]),
                         "minutes_open": int(
                             (ts - (_parse_ts(row["raised_at"]) or ts)
                              ).total_seconds() // 60)})
        fresh = db.execute(
            "SELECT * FROM clinical_escalations WHERE id = ?", (escalation_id,)
        ).fetchone()
        return _to_dict(fresh, ts)


# ═════════════════════════════════════════════════════════════════════════════
#  Reads
# ═════════════════════════════════════════════════════════════════════════════
def list_escalations(*, department: str | None = None, patient_id: str | None = None,
                     include_resolved: bool = False, limit: int = 200,
                     now: datetime | None = None) -> list[dict]:
    """Escalations inside one department's ward boundary.

    ``department`` is the value ``permissions.department_scope(user)`` returned
    and NOTHING ELSE — None means a genuinely unscoped governance account, not
    "scope unknown". The boundary is derived on every read through
    ``PATIENT_DEPARTMENT_SQL`` (admissions.department), never from the
    ``raised_in_department`` snapshot on the row, so a patient who transfers ward
    mid-admission moves with their escalation.

    Default view is UNRESOLVED ONLY, worst first — the board answers "what has
    not been answered yet".
    """
    ts = _now(now)
    where = ["1=1"]
    params: list = []
    if not include_resolved:
        where.append("e.status != 'resolved'")
    if patient_id:
        where.append("e.patient_id = ?")
        params.append(patient_id)
    if department is not None:
        where.append(
            f"e.patient_id IN (SELECT p.id FROM patients p "
            f"WHERE {PATIENT_DEPARTMENT_SQL} = ?)")
        params.append(department)

    with db_session() as db:
        rows = db.execute(
            f"""SELECT e.*, p.name AS patient_name, p.bed AS patient_bed,
                       p.ward AS patient_ward, p.status AS patient_status,
                       u.name AS target_name, ack.name AS acknowledged_by_name
                  FROM clinical_escalations e
                  JOIN patients p ON p.id = e.patient_id
                  LEFT JOIN users u ON u.id = e.target_user_id
                  LEFT JOIN users ack ON ack.id = e.acknowledged_by
                 WHERE {' AND '.join(where)}
                 ORDER BY (e.status = 'open') DESC, e.peak_news2 DESC,
                          e.raised_at ASC
                 LIMIT ?""",
            (*params, limit),
        ).fetchall()

    out = []
    for r in rows:
        d = _to_dict(r, ts)
        d.update({
            "patient_name": r["patient_name"],
            "patient_bed": r["patient_bed"],
            "patient_ward": r["patient_ward"],
            "patient_status": r["patient_status"],
            "target_name": r["target_name"],
            "acknowledged_by_name": r["acknowledged_by_name"],
        })
        out.append(d)
    return out


def get_escalation(escalation_id: str, *, include_events: bool = True,
                   now: datetime | None = None) -> dict | None:
    """One chain plus its whole recorded history, oldest step first."""
    ts = _now(now)
    with db_session() as db:
        row = db.execute(
            """SELECT e.*, p.name AS patient_name, p.bed AS patient_bed,
                      p.ward AS patient_ward,
                      u.name AS target_name, ack.name AS acknowledged_by_name
                 FROM clinical_escalations e
                 JOIN patients p ON p.id = e.patient_id
                 LEFT JOIN users u ON u.id = e.target_user_id
                 LEFT JOIN users ack ON ack.id = e.acknowledged_by
                WHERE e.id = ?""",
            (escalation_id,),
        ).fetchone()
        if row is None:
            return None
        out = _to_dict(row, ts)
        out.update({
            "patient_name": row["patient_name"],
            "patient_bed": row["patient_bed"],
            "patient_ward": row["patient_ward"],
            "target_name": row["target_name"],
            "acknowledged_by_name": row["acknowledged_by_name"],
        })
        if include_events:
            evs = db.execute(
                """SELECT ev.*, u.name AS actor_name
                     FROM escalation_events ev
                     LEFT JOIN users u ON u.id = ev.actor_user_id
                    WHERE ev.escalation_id = ? ORDER BY ev.seq ASC""",
                (escalation_id,),
            ).fetchall()
            out["events"] = [{
                "seq": e["seq"], "event_type": e["event_type"], "tier": e["tier"],
                "tier_role": e["tier_role"], "target_user_id": e["target_user_id"],
                "target_description": e["target_description"],
                "actor_user_id": e["actor_user_id"], "actor_name": e["actor_name"],
                "actor_role": e["actor_role"], "news2": e["news2"],
                "detail": e["detail"], "occurred_at": e["occurred_at"],
                "metadata": json.loads(e["metadata_json"]) if e["metadata_json"] else None,
            } for e in evs]
    return out


def patient_escalations(patient_id: str, *, include_resolved: bool = True,
                        limit: int = 50, now: datetime | None = None) -> list[dict]:
    """Every escalation for one patient — for the chart, not the board.

    The CALLER is responsible for the ward check (routes use
    ``require_patient_access``); this function deliberately does not take a
    department, so there is no second, divergent scope derivation in the system.
    """
    return list_escalations(patient_id=patient_id, include_resolved=include_resolved,
                            limit=limit, now=now)


# ═════════════════════════════════════════════════════════════════════════════
#  The safety net
# ═════════════════════════════════════════════════════════════════════════════
def _scan_max_rows() -> int:
    try:
        return max(1, int(os.getenv("ORB_ESCALATION_SCAN_MAX_ROWS", "1000")))
    except ValueError:
        return 1000


def scan_new_observations(*, department: str | None = None,
                          now: datetime | None = None) -> list[dict]:
    """Raise chains for qualifying observations that NOTHING dispatched.

    WHY THIS EXISTS RATHER THAN JUST TRUSTING THE WRITE PATHS. Orb has three
    vitals lanes — the simulator, the HL7/FHIR/JSON ingestion feed, and manual
    bedside entry — and the bug this whole module answers was that ONE of them
    had no escalation wired to it. "Somebody will remember to call the service
    from the fourth lane" is precisely the assumption that failed. So the chain
    is also derivable from the ``vitals`` table alone: if a qualifying
    observation exists and no escalation covers it, this raises one, whatever
    wrote the row and whether or not that writer knows this module exists.

    HOW IT AVOIDS RE-RAISING. Per patient it looks only at observations NEWER
    than the newest one any escalation for that patient has already accounted for
    (``last_vitals_id``, across resolved chains too), inside
    ``ORB_ESCALATION_SCAN_LOOKBACK_MINUTES``, and takes the most recent
    QUALIFYING row. Most-recent-QUALIFYING rather than most-recent: a spike
    followed a minute later by a benign reading must still escalate, and "latest
    row only" would silently lose it — the dangerous direction to be wrong in.

    IT DOES NOT NARRATE A CHAIN THAT IS ALREADY OPEN. When a patient already has
    an active escalation that already covers the new score, the scan silently
    advances that chain's watermark and records nothing. This is not a detail: a
    monitor feed writes a row every few seconds, and a board polled every 30 s
    would otherwise staple two "further observation" steps a minute onto an open
    chain until its history is unreadable. Recording each charted observation on
    the chain is the WRITE path's job — it happens once per charted set, which is
    the right cadence. The safety net's job is only to make sure a deterioration
    is VISIBLE.

    "ALREADY COVERS" IS TWO TESTS, NOT ONE, and the second one is a fix
    (2026-08-14). It used to be entry-tier alone — ``_entry_tier(raw) <=
    active_tier`` — and tier 2 is the HIGHEST tier the policy can be entered at
    (tier 3 has no ``entry_min_news2``). So on a patient with an open tier-2
    chain, EVERY score from 7 upwards mapped to tier 2, compared "<= 2", and was
    swallowed whole: no chain event, no ``peak_news2`` update, and the watermark
    moved past the row so it could never be reconsidered. MEASURED on this
    appliance 2026-08-13 — a nurse charted NEWS2 19 and the escalation board
    showed 10 for the same patient in the same minute, with the drivers of an
    unrelated, milder monitor reading. Two numbers, one patient; the board is the
    one a registrar triages from, and it was showing the wrong one.

    The chain must therefore also already be AT LEAST THIS LOUD — ``raw <=
    peak_news2``. A strictly worse score than the chain has ever recorded goes
    through the full path, which raises the peak, records the observation and
    bumps the tier if the score earns one. The anti-chatter property survives
    intact: a monitor re-sending the same score has ``raw == peak`` and stays
    silent, and because each recorded step must be a NEW MAXIMUM the number of
    steps a chain can accumulate is bounded by the NEWS2 scale itself.

    TWO QUERIES, BOUNDED. One over ``clinical_escalations`` (small) for the
    per-patient watermark and active tier, one over ``vitals`` newest-first with
    a hard ``ORB_ESCALATION_SCAN_MAX_ROWS`` cap. The earlier shape was a query
    per admitted patient, which on a 40-bed ward polled by a dozen tablets is
    thousands of SQLCipher round trips a minute for nothing.

    Bounded to the caller's department when given one. Routes only run this for a
    ward-scoped caller: a hospital-wide governance account reading a board should
    not manufacture clinical state across every ward it can see.

    THE HONEST GAP: this is a SAFETY NET, not the mechanism. Latency is the gap
    between sweeps (a board read, or a call to the sweep endpoint). A vitals path
    that calls ``evaluate_observation`` post-commit escalates immediately and this
    finds nothing left to do. Wiring the remaining writers is the fix; this makes
    the missing wiring survivable in the meantime.

    SCORING SWITCHED OFF (ORB_SCORING_ENABLED, the Orb Ward Record): nothing
    is scored, so nothing is raised. Without this the background sweep would
    score every set the ward clerk keyed and open a chain, with its Relay post,
    a minute after the write path had correctly raised nothing.
    """
    if not _scoring_enabled():
        return []
    ts = _now(now)
    cutoff = (ts - timedelta(minutes=_lookback_minutes())).isoformat()
    policy = tier_policy()
    raised: list[dict] = []

    with db_session() as db:
        state = {
            r["patient_id"]: (r["watermark"] or 0, r["active_tier"], r["active_id"],
                              r["active_peak"])
            for r in db.execute(
                """SELECT patient_id,
                          COALESCE(MAX(last_vitals_id), 0) AS watermark,
                          MAX(CASE WHEN status != 'resolved' THEN current_tier END) AS active_tier,
                          MAX(CASE WHEN status != 'resolved' THEN id END) AS active_id,
                          MAX(CASE WHEN status != 'resolved' THEN peak_news2 END) AS active_peak
                     FROM clinical_escalations GROUP BY patient_id"""
            ).fetchall()
        }

        params: list = [cutoff]
        dept_sql = ""
        if department is not None:
            dept_sql = f" AND {PATIENT_DEPARTMENT_SQL} = ?"
            params.append(department)
        params.append(_scan_max_rows())
        rows = db.execute(
            f"""SELECT v.*, p.on_scale_2 AS patient_on_scale_2
                  FROM vitals v
                  JOIN patients p ON p.id = v.patient_id
                 WHERE p.status = 'admitted' AND v.timestamp >= ? AND v.voided_at IS NULL {dept_sql}
                 ORDER BY v.id DESC LIMIT ?""",
            tuple(params),
        ).fetchall()

        settled: set[str] = set()
        work: list[tuple[str, dict, dict]] = []
        bump_watermark: list[tuple[int, str]] = []
        for v in rows:
            pid = v["patient_id"]
            if pid in settled:
                continue
            watermark, active_tier, active_id, active_peak = state.get(
                pid, (0, None, None, None))
            if v["id"] <= (watermark or 0):
                # Everything older is already accounted for: this patient is done.
                settled.add(pid)
                continue
            keys = v.keys()
            news2 = compute_news2(
                v["heart_rate"], v["systolic_bp"], v["respiratory_rate"],
                v["temperature"], v["spo2"],
                # Unassessed values pass THROUGH as None: same points, and the
                # result's `complete` stays honest for the label it feeds.
                v["consciousness"] if "consciousness" in keys else None,
                (bool(v["on_oxygen"]) if "on_oxygen" in keys and v["on_oxygen"] is not None
                 else None),
                on_scale_2=bool(v["patient_on_scale_2"]),
            )
            if not qualifies(news2):
                continue
            settled.add(pid)
            raw = int(news2.get("raw_news2") or 0)
            if (active_id is not None
                    and _entry_tier(policy, raw) <= int(active_tier or 1)
                    # …AND already at least this loud. Without this clause every
                    # score >= the tier-2 floor compares equal on a tier-2 chain,
                    # so a NEWS2 19 was swallowed by a chain whose peak was 10 and
                    # the watermark moved past it. See the docstring.
                    and raw <= int(active_peak or 0)):
                # Already open, already at least this loud. Move the watermark on
                # so the next sweep does not re-examine the same rows, and say
                # nothing on the chain.
                bump_watermark.append((v["id"], active_id))
                continue
            work.append((pid, dict(v), news2))

        for vid, eid in bump_watermark:
            db.execute(
                "UPDATE clinical_escalations SET last_vitals_id = ? "
                "WHERE id = ? AND COALESCE(last_vitals_id, 0) < ?",
                (vid, eid, vid),
            )

    # OUTSIDE the read session on purpose: evaluate_observation opens its own
    # transaction and, on the notify path, so does _persist_relay_alert.
    for pid, v, news2 in work:
        out = evaluate_observation(
            pid, news2, vitals_id=v["id"], source=v.get("source"),
            observed_at=v["timestamp"], now=ts,
        )
        if out is not None:
            raised.append(out)
    return raised


def sweep(*, department: str | None = None, now: datetime | None = None) -> dict:
    """One tick of the whole mechanism: advance overdue tiers, then catch any
    observation nothing dispatched. Returns COUNTS ONLY — no PHI — so it is safe
    to expose to an ops caller and to log.
    """
    ts = _now(now)
    advanced = advance_due(now=ts)
    found = scan_new_observations(department=department, now=ts)
    if advanced or found:
        logger.warning("escalation sweep: %d tier advance(s), %d new chain(s)",
                       len(advanced), len(found))
    return {
        "advanced": len(advanced),
        "raised": len(found),
        "swept_at": ts.isoformat(),
        "department": department,
    }


def sweep_interval_seconds() -> int:
    """Seconds between background sweeps, from ``ORB_ESCALATION_SWEEP_INTERVAL_S``.

    Clamped to 10..120. The shortest tier timeout is minutes, so a sweep faster
    than 10 s buys nothing, and one slower than two minutes adds visible lag to
    every climb. There is deliberately no "off" value: an appliance with this
    loop disabled is the idle ward where nothing ever escalates.
    """
    try:
        raw = int(os.getenv("ORB_ESCALATION_SWEEP_INTERVAL_S", "30"))
    except ValueError:
        raw = 30
    return max(10, min(raw, 120))


async def sweep_loop(interval_seconds: float = 30) -> None:
    """The escalation clock. Registered from main.py's lifespan.

    Until this existed the ladder only moved when somebody opened the
    escalation board, so on a quiet night shift a tier-1 chain nobody looked at
    stayed at tier 1 and the registrar was never told. Runs ``sweep`` unscoped:
    the POST /escalations/sweep route already documents a scheduled sweep as the
    hospital-wide case, and an escalation on a ward nobody has open is exactly
    the one that must still climb.

    Never raises out (an escaped exception kills the task and the clock stops
    for good, silently); cancellation is re-raised so shutdown is clean.
    """
    global _EMIT_LOOP
    from database import run_db
    _EMIT_LOOP = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await run_db(sweep)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("escalation sweep cycle failed; retrying next interval",
                         exc_info=True)
