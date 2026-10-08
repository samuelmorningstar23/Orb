"""Orb — Nurse Shift Service.

The ward-nurse half of the clinical loop. Doctors work patient-by-patient;
a nurse on a shift works TASK-by-task across a whole bay, and the three things
that fill that shift are:

  * observations — is this set of obs due, and what does it score,
  * medications  — which charted doses are due now, and were they given,
  * handover     — hand the whole list over safely at shift change.

This module assembles exactly those three views out of tables that already
exist (``vitals``, ``risk_scores``, ``medications``, ``medication_administrations``)
and adds ONE genuinely new write: a nurse-entered observation set. Everything
else composes services that already own their domain —
``vigil_service.score_vitals_row`` scores, ``emar_service`` administers,
``ipass_service`` hands over. Nothing here re-implements a clinical rule that
lives somewhere else.

DERIVED, NOT PRESCRIBED — read this before trusting a due time
--------------------------------------------------------------
Orb has no prescriber-authored administration schedule: ``medications`` carries
a free-text ``frequency`` and ``medication_administrations.scheduled_for`` is
never populated. The due times below are therefore DERIVED — interval parsed
out of the frequency string, anchored on the last recorded 'given' dose (or the
order start if none). Every med task carries ``due_basis`` saying which anchor
was used, and the UI must present these as a nurse's working list, never as an
authoritative prescription schedule. Anything the parser cannot read is
returned as ``unscheduled`` and is NEVER shown as overdue — a med we cannot
time is a med we must not imply is late.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from database import db_session
from services import allergy_interlock
from services.audit_service import log_audit_event
from services.clinical_time import format_utc
from services.drug_safety_service import _norm as _norm_drug
from services.vigil_service import is_latest_vitals_row, news2_reason_line, score_vitals_row
from services.vital_charting import as_charted, vitals_line
from services.news2_text import news2_description
from services.vitals_source.base import (
    LABEL_ADMISSION_VITALS,
    LABEL_CLERK_TRANSCRIBED,
    LABEL_MANUAL_ENTRY,
    LABEL_NURSE_OBSERVATION,
)

logger = logging.getLogger("orb.nurse")

# Nurse-entered observations are tagged with this in vitals.source so they are
# distinguishable from device/HL7 ingestion and from the simulator in every
# downstream read. The literal is OWNED by services/vitals_source/base.py so
# the writer here and `classify_source_label` can never disagree again: until
# 2026-08-10 this string was absent from _LABEL_KIND and every nurse-charted
# set classified UNKNOWN — the shadow scorecard could not attribute the ward's
# own observations to a human.
OBS_SOURCE = LABEL_NURSE_OBSERVATION

# The Orb Ward Record's clerk lane: a set copied from the paper observation
# sheet by an account holding vitals.transcribe and not vitals.write. Same
# route, same transaction, its own label, so every reader can tell a bedside
# set from a transcribed one.
CLERK_SOURCE = LABEL_CLERK_TRANSCRIBED
MANUAL_OBS_SOURCES = (OBS_SOURCE, CLERK_SOURCE)

# The readings a paper sheet can show but nobody can read. An illegible field
# is stored NULL (never guessed) and named in vitals.illegible, comma separated,
# in this order.
ILLEGIBLE_FIELDS = ("heart_rate", "systolic_bp", "diastolic_bp", "spo2",
                    "temperature", "respiratory_rate", "consciousness", "on_oxygen")

# THE READINGS THAT RESET THE OBS-DUE CLOCK: a set a person took (SourceKind
# MANUAL). The clock used to run off the newest row of any kind, so under
# simulation a synthetic reading every 5 s re-anchored it and "obs overdue"
# never fired. A simulated row is not care delivered, and a monitor row carries
# no ACVPU or supplemental O2, so neither stands in for a NEWS2 set.
OBS_CLOCK_SOURCES = (LABEL_NURSE_OBSERVATION, LABEL_MANUAL_ENTRY, LABEL_ADMISSION_VITALS,
                     LABEL_CLERK_TRANSCRIBED)

# The shadow instrument's lane for everything raised from bedside entry. One of
# the six in shadow_service.LANES (and in migration 4's CHECK constraint), and
# the one the pilot's Week-13 shadow go-live actually runs on: manual entry is
# the redundancy layer, so a scorecard that cannot see this lane is a scorecard
# about the simulator.
SHADOW_LANE = "manual"

# Physiologically survivable entry bounds. These are DATA-ENTRY guards against
# a fat-fingered keypad (a transposed "160" for RR), not clinical judgement —
# they are deliberately wide enough to accept a genuinely peri-arrest patient.
OBS_RANGES: dict[str, tuple[float, float, str]] = {
    "heart_rate":        (10, 300, "bpm"),
    "systolic_bp":       (40, 300, "mmHg"),
    "diastolic_bp":      (10, 200, "mmHg"),
    "respiratory_rate":  (2, 80, "/min"),
    "temperature":       (25.0, 45.0, "°C"),
    "spo2":              (30, 100, "%"),
}

# ACVPU. 'alert' scores 0; every other level is a new-onset 3-pointer in NEWS2.
ACVPU_LEVELS = ("alert", "confusion", "voice", "pain", "unresponsive")


# ─────────────────────────────────────────────────────────────────────────────
# Time helpers (timestamps in this DB arrive in two shapes: ISO-8601 with an
# offset from the ingestion path, and SQLite's naive 'YYYY-MM-DD HH:MM:SS' from
# CURRENT_TIMESTAMP defaults, which is UTC. Normalize both to aware UTC.)
# ─────────────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ─────────────────────────────────────────────────────────────────────────────
# Observation frequency — RCP NEWS2 monitoring escalation
# ─────────────────────────────────────────────────────────────────────────────
# Royal College of Physicians NEWS2 (2017), "clinical response to the NEWS
# trigger" table. Encoded here as the MINIMUM monitoring frequency for a score
# band, so the board can say "obs due" rather than making each nurse hold the
# table in their head at 3 a.m. Reference only — it never blocks a nurse from
# taking obs earlier, and the escalation ADVICE is what the table mandates.
_OBS_ESCALATION: tuple[tuple[int, int, str, str], ...] = (
    # (min aggregate, interval minutes, band label, RCP response)
    (7, 30, "NEWS2 7 or more",
     "Continuous monitoring. Emergency assessment by a critical-care-competent team."),
    (5, 60, "NEWS2 5-6",
     "Urgent review by a clinician competent in acute illness. Escalate now."),
    (0, 240, "NEWS2 1-4",
     "Ward-based response: inform the registered nurse in charge."),
)
# NEWS2 0 is the only band that relaxes past 4-hourly.
_OBS_INTERVAL_ZERO = 720
# The single-parameter-3 rule sits outside the aggregate bands: any one vital
# scoring 3 mandates at least hourly obs and an urgent review, even at a low total.
_OBS_INTERVAL_SINGLE_PARAM_3 = 60


#: The shift board's words for a ward with no observation interval set while
#: scoring is off (the Orb Ward Record). An administrator sets it in Admin, Data.
WARD_INTERVAL_MISSING = "Set the ward's observation interval"


def obs_schedule(news2: dict | None) -> dict:
    """Minimum obs interval + the RCP response for a NEWS2 result.

    Returns interval_minutes, a band label, and the escalation text. A patient
    with no NEWS2 at all falls back to 4-hourly — never to the 12-hourly
    "score 0" relaxation, because an absent score is not a reassuring one.
    """
    if not news2 or news2.get("raw_news2") is None:
        return {
            "interval_minutes": 240,
            "band": "No score",
            "response": "No NEWS2 on record: take a full set of observations.",
        }
    raw = int(news2.get("raw_news2") or 0)
    single_3 = bool(news2.get("single_param_3"))

    for threshold, minutes, band, response in _OBS_ESCALATION:
        if raw >= threshold and (threshold > 0 or raw > 0):
            if single_3 and minutes > _OBS_INTERVAL_SINGLE_PARAM_3:
                return {
                    "interval_minutes": _OBS_INTERVAL_SINGLE_PARAM_3,
                    "band": f"{band} (single parameter 3)",
                    "response": "A single parameter scoring 3 requires urgent review. Escalate.",
                }
            return {"interval_minutes": minutes, "band": band, "response": response}

    if single_3:
        return {
            "interval_minutes": _OBS_INTERVAL_SINGLE_PARAM_3,
            "band": "Single parameter 3",
            "response": "A single parameter scoring 3 requires urgent review. Escalate.",
        }
    return {
        "interval_minutes": _OBS_INTERVAL_ZERO,
        "band": "NEWS2 0",
        "response": "Routine monitoring, minimum 12-hourly.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Medication frequency parsing
# ─────────────────────────────────────────────────────────────────────────────
# Free-text `frequency` → a dosing interval, when one can be read out of it at
# all. Deliberately conservative: anything ambiguous returns kind='unscheduled'
# so the UI lists the med without ever claiming it is late.
_FREQ_INTERVALS: dict[str, int] = {
    # Latin / ward shorthand
    "od": 24 * 60, "qd": 24 * 60, "daily": 24 * 60, "nocte": 24 * 60, "nightly": 24 * 60,
    "bd": 12 * 60, "bid": 12 * 60, "twice daily": 12 * 60,
    "tds": 8 * 60, "tid": 8 * 60, "thrice daily": 8 * 60,
    "qds": 6 * 60, "qid": 6 * 60, "four times daily": 6 * 60,
    "alternate days": 48 * 60, "eod": 48 * 60,
    "weekly": 7 * 24 * 60,
}
# Frequencies that describe a running therapy rather than discrete due doses.
_CONTINUOUS_TOKENS = (
    "continuous", "infusion", "in fluids", "in situ", "all feeds", "transfusion",
    "titrate", "tapering", "sliding scale",
)
_STAT_TOKENS = ("stat", "once only", "immediately")


def parse_frequency(frequency: str | None) -> dict:
    """Read a dosing interval out of a free-text frequency string.

    Returns ``{kind, interval_minutes, label}`` where kind is one of:

      scheduled   — a repeating dose we can time (interval_minutes set)
      prn         — as-required; available, never due, never overdue
      continuous  — a running infusion/feed; checked, not "given" on a clock
      stat        — a one-off dose, due until it has been given once
      unscheduled — unparseable; listed but never timed (see module docstring)

    PRN is checked FIRST and wins outright: "Q10min PRN" and "Q8H PRN" are
    as-required orders with a minimum interval, not q10min/q8h schedules, and
    showing them as overdue every ten minutes would train nurses to ignore the
    overdue colour entirely.
    """
    raw = (frequency or "").strip()
    text = raw.lower()
    if not text:
        return {"kind": "unscheduled", "interval_minutes": None, "label": raw or "—"}

    if "prn" in text or "as required" in text or "as needed" in text or text.startswith("if "):
        return {"kind": "prn", "interval_minutes": None, "label": raw}

    if any(tok in text for tok in _CONTINUOUS_TOKENS):
        return {"kind": "continuous", "interval_minutes": None, "label": raw}

    if any(tok in text for tok in _STAT_TOKENS):
        return {"kind": "stat", "interval_minutes": None, "label": raw}

    # Qn H / Qn min forms: Q6H, q12h, Q 8 hourly, Q30min.
    m = re.search(r"\bq\s*(\d+)\s*(h|hr|hrs|hour|hourly|min|mins|minute|minutes)\b", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        minutes = n if unit.startswith("min") else n * 60
        if minutes > 0:
            return {"kind": "scheduled", "interval_minutes": minutes, "label": raw}

    # "6 hourly", "4-hourly"
    m = re.search(r"\b(\d+)\s*-?\s*hourly\b", text)
    if m and int(m.group(1)) > 0:
        return {"kind": "scheduled", "interval_minutes": int(m.group(1)) * 60, "label": raw}

    # Shorthand table. Longest key first so "twice daily" is not shadowed by "daily".
    for key in sorted(_FREQ_INTERVALS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", text):
            return {"kind": "scheduled", "interval_minutes": _FREQ_INTERVALS[key], "label": raw}

    return {"kind": "unscheduled", "interval_minutes": None, "label": raw}


# ─────────────────────────────────────────────────────────────────────────────
# Shift board
# ─────────────────────────────────────────────────────────────────────────────
def _load_board_rows(department: str | None):
    """Every table read the board needs, in four set-based queries.

    Deliberately NOT per-patient: a 60-bed ward would otherwise cost 240 round
    trips through the DB pool on every board refresh.
    """
    with db_session() as db:
        params: tuple = ()
        dept_clause = ""
        if department:
            dept_clause = " AND a.department = ?"
            params = (department,)

        patients = db.execute(
            f"""SELECT p.id, p.name, p.mrn, p.bed, p.ward, p.age, p.sex, p.diagnosis,
                       p.on_scale_2, a.department AS admission_department,
                       u.name AS attending_doctor_name
                FROM patients p
                JOIN admissions a ON a.patient_id = p.id AND a.status = 'active'
                LEFT JOIN users u ON u.id = p.attending_doctor_id
                WHERE p.status = 'admitted'{dept_clause}
                ORDER BY p.ward, p.bed""",
            params,
        ).fetchall()

        ids = [p["id"] for p in patients]
        if not ids:
            return [], {}, {}, {}
        marks = ",".join("?" for _ in ids)

        # Latest vitals row per patient.
        vitals = db.execute(
            f"""SELECT v.* FROM vitals v
                JOIN (SELECT patient_id, MAX(timestamp) AS ts
                        FROM vitals WHERE patient_id IN ({marks}) AND voided_at IS NULL
                       GROUP BY patient_id) latest
                  ON latest.patient_id = v.patient_id AND latest.ts = v.timestamp
                WHERE v.patient_id IN ({marks}) AND v.voided_at IS NULL""",
            (*ids, *ids),
        ).fetchall()
        # A tie on timestamp (two rows at the same instant) yields two rows for
        # one patient; the higher id is the later write, so keep that one.
        latest_vitals: dict[str, dict] = {}
        for v in vitals:
            prev = latest_vitals.get(v["patient_id"])
            if prev is None or (v["id"] or 0) > (prev["id"] or 0):
                latest_vitals[v["patient_id"]] = dict(v)

        # The newest set a PERSON took, per patient (OBS_CLOCK_SOURCES), picked
        # by timestamp DESC, id DESC. Anchors the obs-due clock.
        src_marks = ",".join("?" for _ in OBS_CLOCK_SOURCES)
        last_obs_rows = db.execute(
            f"""SELECT id, patient_id, timestamp, source FROM (
                    SELECT v.id, v.patient_id, v.timestamp, v.source,
                           ROW_NUMBER() OVER (PARTITION BY v.patient_id
                                              ORDER BY v.timestamp DESC, v.id DESC) AS rn
                      FROM vitals v
                     WHERE v.patient_id IN ({marks}) AND v.source IN ({src_marks})
                       AND v.voided_at IS NULL
                ) WHERE rn = 1""",
            (*ids, *OBS_CLOCK_SOURCES),
        ).fetchall()
        last_obs_by_patient = {r["patient_id"]: dict(r) for r in last_obs_rows}

        # Active + PRN medications.
        meds = db.execute(
            f"""SELECT id, patient_id, drug_name, dosage, route, frequency, status,
                       started_at, source, verified_by, notes
                FROM medications
                WHERE patient_id IN ({marks}) AND status IN ('active', 'prn')
                ORDER BY drug_name""",
            ids,
        ).fetchall()
        meds_by_patient: dict[str, list[dict]] = {}
        for m in meds:
            meds_by_patient.setdefault(m["patient_id"], []).append(dict(m))

        # Last GIVEN administration per medication. 'not_given' rows are
        # excluded on purpose: a withheld dose does not reset the dosing clock,
        # so the next dose stays due off the last dose that actually went in.
        last_given = db.execute(
            f"""SELECT medication_id, MAX(administered_at) AS last_given_at, COUNT(*) AS given_count
                FROM medication_administrations
                WHERE patient_id IN ({marks}) AND status = 'given'
                GROUP BY medication_id""",
            ids,
        ).fetchall()
        last_given_by_med = {r["medication_id"]: dict(r) for r in last_given}

        # Structured allergies — carried onto the card because the eMAR round
        # starts here and an allergy the nurse cannot see is an allergy that
        # cannot stop a dose.
        # `id` and `substance_class` are not display fields — they are what the
        # allergy interlock matches and fingerprints on. Selecting the same
        # columns the bedside screen selects is what makes the board's preview
        # and the server's verdict the SAME verdict; drop one and the board
        # silently under-matches or fails to see an override it should.
        allergies = db.execute(
            f"""SELECT id, patient_id, substance, substance_class, reaction, severity
                FROM patient_allergies
                WHERE patient_id IN ({marks}) AND status = 'active'""",
            ids,
        ).fetchall()
        allergies_by_patient: dict[str, list[dict]] = {}
        for a in allergies:
            allergies_by_patient.setdefault(a["patient_id"], []).append(dict(a))

        # ── Allergy-interlock context ────────────────────────────────────────
        # The board runs the SAME screen the bedside write will run, so a nurse
        # learns a dose is blocked here rather than at the bedside after walking
        # to it. This is a COURTESY, not the control: emar_service screens again
        # at the write, on the allergy list as it is at that instant. Nothing
        # here can let a dose through.
        #
        # Both lookups are set-based for the whole ward. Screening is otherwise
        # patients × medications, and a query per medication is how a board
        # stops being refreshed.
        drug_names = {m["drug_name"] for row in meds_by_patient.values() for m in row}
        ingredient_map = allergy_interlock.resolve_ingredients_bulk(db, drug_names)

        override_rows = db.execute(
            f"""SELECT o.medication_id, o.alert_fingerprint, o.reason,
                       o.overridden_by, o.overridden_at, o.id
                  FROM medication_allergy_overrides o
                 WHERE o.patient_id IN ({marks})
                 ORDER BY o.id ASC""",
            ids,
        ).fetchall()
        overrides_by_key: dict[tuple, dict] = {}
        for o in override_rows:
            # Last write wins: the most recent acceptance of this exact hazard.
            overrides_by_key[(o["medication_id"], o["alert_fingerprint"])] = {
                "source_override_id": o["id"],
                "by": o["overridden_by"],
                "reason": o["reason"],
                "at": o["overridden_at"],
                "carried": True,
            }

    return (
        [dict(p) for p in patients],
        latest_vitals,
        meds_by_patient,
        {
            "last_given": last_given_by_med,
            "allergies": allergies_by_patient,
            "ingredients": ingredient_map,
            "overrides": overrides_by_key,
            "last_obs": last_obs_by_patient,
        },
    )


def _score_from_vitals(vitals: dict | None, on_scale_2: bool) -> dict | None:
    """NEWS2 for a stored vitals row, through the one scoring ENTRY POINT.

    Not ``compute_news2`` — ``vigil_service.score_vitals_row``. compute_news2 is
    only the RCP table; the displayed score is that table PLUS the CfC overlay,
    and this board used to stop at the table while the doctor's chart
    (``get_patient_risk``) applied the overlay. Two scorers, one patient. It was
    latent only because no checkpoint is loaded — the day one lands, the ward
    board and the chart would disagree about who is critical, silently, with no
    test going red. See ``tests/test_vigil_scoring_parity.py``.

    ``patient_id`` is taken off the row itself (``_load_board_rows`` selects
    ``v.*``) so the overlay can read the patient's trajectory. This runs OUTSIDE
    the board's db_session — the overlay opens its own connection and must never
    be called from inside an open write transaction.
    """
    if not vitals:
        return None
    return score_vitals_row(
        vitals,
        patient_id=vitals.get("patient_id"),
        on_scale_2=on_scale_2,
    )


def _preview_allergy(
    med: dict,
    allergies: list[dict],
    ingredients: list[str],
    overrides: dict[tuple, dict],
) -> dict:
    """Run the bedside allergy interlock ahead of the bedside.

    Pure (``allergy_interlock.evaluate`` does no I/O) — everything it needs was
    loaded set-based in ``_load_board_rows``. If it raises for one medication,
    THAT medication reads as unscreened rather than the ward board dying or,
    worse, quietly rendering a clean screen it never ran.
    """
    try:
        screen = allergy_interlock.evaluate(allergies, med["drug_name"], ingredients)
        if screen["decision"] == allergy_interlock.BLOCK:
            carried = overrides.get((med["id"], screen.get("fingerprint")))
            if carried:
                screen["override"] = carried
        return allergy_interlock.summarize(screen)
    except Exception as e:  # pragma: no cover — defensive; see docstring
        logger.error("allergy preview failed for med %s (%s): %s",
                     med.get("id"), med.get("drug_name"), e)
        return allergy_interlock.summarize(
            allergy_interlock.failed_screen(med.get("drug_name") or "", str(e))
        )


def _build_med_task(
    med: dict,
    last_given: dict | None,
    now: datetime,
    allergies: list[dict] | None = None,
    ingredients: list[str] | None = None,
    overrides: dict[tuple, dict] | None = None,
) -> dict:
    """One eMAR row for the round, with a DERIVED due time (see module docstring)."""
    schedule = parse_frequency(med.get("frequency"))
    last_given_at = _parse_ts((last_given or {}).get("last_given_at"))
    started_at = _parse_ts(med.get("started_at"))

    due_at: datetime | None = None
    due_basis = "unscheduled"
    # Scheduled slots between the anchor and the current one with no recorded
    # 'given' row. On a ward that charts every dose this is 0; a non-zero value
    # means doses are being given but not charted (or not given at all), which
    # is exactly the gap a shadow-mode pilot needs to see.
    missed_slots = 0

    if schedule["kind"] == "scheduled" and schedule["interval_minutes"]:
        anchor = last_given_at or started_at
        if anchor:
            interval = timedelta(minutes=schedule["interval_minutes"])
            due_at = anchor + interval
            due_basis = "last_given" if last_given_at else "order_start"
            # Roll forward to the CURRENT dosing slot. Anchoring on an order
            # started three days ago would otherwise report the next dose as
            # three days late, painting the whole board red and teaching nurses
            # to ignore it. The honest statement is "this slot is N minutes
            # late, and k earlier slots were never charted".
            if due_at < now:
                skipped = int((now - due_at) / interval)
                if skipped > 0:
                    due_at += interval * skipped
                    missed_slots = skipped
    elif schedule["kind"] == "stat":
        # A stat dose is due from the moment it was ordered until it is given once.
        if not last_given_at:
            due_at = started_at
            due_basis = "stat_pending"
        else:
            due_basis = "stat_given"

    minutes_until = None if due_at is None else round((due_at - now).total_seconds() / 60)
    # A dose is only "overdue" once it is a clear 15 minutes late — a ward round
    # takes time, and colouring every dose red the second it ticks over is how a
    # board stops meaning anything.
    overdue = minutes_until is not None and minutes_until < -15
    due_soon = minutes_until is not None and -15 <= minutes_until <= 30

    unverified = (
        (med.get("source") in ("llm_extracted", "agentic")) and not med.get("verified_by")
    )

    return {
        "medication_id": med["id"],
        "drug_name": med["drug_name"],
        "dosage": med.get("dosage"),
        "route": med.get("route"),
        "frequency": med.get("frequency"),
        "schedule_kind": schedule["kind"],
        "interval_minutes": schedule["interval_minutes"],
        "status": med.get("status"),
        "last_given_at": _iso(last_given_at),
        "given_count": (last_given or {}).get("given_count", 0),
        "due_at": _iso(due_at),
        "due_basis": due_basis,
        "missed_slots": missed_slots,
        "minutes_until_due": minutes_until,
        "overdue": overdue,
        "due_soon": due_soon,
        # Mirrors vigil_routes: an AI-extracted med no pharmacist has verified is
        # NOT administrable, and emar_service rejects it at the write. Flagged
        # here so the round can grey it out rather than let a nurse walk to the
        # bedside and only then hit a 422.
        "unverified": unverified,
        # Same idea, for the allergy interlock. `decision` is allow | warn |
        # block; `block` means this dose will need a typed clinical reason at
        # the bedside (unless `override` is already populated from an earlier
        # dose of this order). Advisory ONLY — the server screens again at the
        # write; see _preview_allergy.
        "allergy": _preview_allergy(med, allergies or [], ingredients or [], overrides or {}),
    }


def build_board(department: str | None) -> dict:
    """The nurse's shift board: one task-shaped card per patient.

    ``department`` is REQUIRED — no default. Passing None means "every admitted
    patient in the hospital", and a defaulted parameter would make that the
    outcome of simply forgetting to scope the call. Callers must state the scope
    they mean, and the HTTP caller must derive it from the authenticated user
    (``permissions.department_scope``), never from the request.

    Ordering is clinical, not alphabetical — the sickest and the latest-running
    tasks float up, because the first thing a nurse needs off this screen is
    "who do I go to first".
    """
    now = _now()
    patients, latest_vitals, meds_by_patient, extra = _load_board_rows(department)
    last_given_by_med = extra.get("last_given", {})
    allergies_by_patient = extra.get("allergies", {})
    ingredient_map = extra.get("ingredients", {})
    overrides_by_key = extra.get("overrides", {})
    last_obs_by_patient = extra.get("last_obs", {})

    # Scoring switched off (the Orb Ward Record): no NEWS2 and no schedule
    # worked out from one. The due time is the ward's fixed interval from
    # ward_observation_settings, chained the way the monthly rounds file chains
    # it (ward_record_service.due_now); a ward with no setting shows no due time.
    scoring = scoring_enabled()
    ward_due: dict[str, dict | None] = {}
    if not scoring:
        from services import ward_record_service
        for ward in {str(p.get("ward") or "").strip() for p in patients}:
            try:
                ward_due[ward.upper()] = ward_record_service.due_now(ward, now=now) if ward else None
            except Exception as e:  # noqa: BLE001 - one ward's read must not blank the board
                logger.warning("ward record due times failed for ward %r: %s", ward, e)
                ward_due[ward.upper()] = None

    cards = []
    for p in patients:
        pid = p["id"]
        vitals = latest_vitals.get(pid)
        last_obs = last_obs_by_patient.get(pid)
        last_obs_at = _parse_ts(last_obs.get("timestamp")) if last_obs else None
        obs_due_note = None
        if scoring:
            news2 = _score_from_vitals(vitals, bool(p.get("on_scale_2")))
            schedule = obs_schedule(news2)
            # Anchored on the last set a person took, not on the newest row: see
            # OBS_CLOCK_SOURCES. The NEWS2 above still scores the newest row.
            obs_due_at = (
                last_obs_at + timedelta(minutes=schedule["interval_minutes"]) if last_obs_at else None
            )
            obs_minutes_until = (
                None if obs_due_at is None else round((obs_due_at - now).total_seconds() / 60)
            )
            # No obs at all on an admitted patient is itself the overdue state.
            obs_overdue = obs_minutes_until is None or obs_minutes_until < 0
            obs_due_soon = obs_minutes_until is not None and 0 <= obs_minutes_until <= 30
        else:
            news2 = None
            setting = ward_due.get(str(p.get("ward") or "").strip().upper())
            due = (setting or {}).get("patients", {}).get(pid) if setting else None
            schedule = {
                "interval_minutes": setting["interval_minutes"] if setting else None,
                "band": None,
                "response": None,
            }
            obs_due_at = due["due_at"] if due else None
            obs_minutes_until = (
                None if obs_due_at is None else round((obs_due_at - now).total_seconds() / 60)
            )
            # Late is the rounds file's late: past the due time plus the ward's
            # grace window. No setting, no due time and nothing is late.
            obs_overdue = bool(due and due["late"])
            obs_due_soon = (obs_minutes_until is not None and not obs_overdue
                            and obs_minutes_until <= 30)
            if setting is None:
                obs_due_note = WARD_INTERVAL_MISSING

        patient_allergies = allergies_by_patient.get(pid, [])
        med_tasks = [
            _build_med_task(
                m, last_given_by_med.get(m["id"]), now,
                allergies=patient_allergies,
                ingredients=ingredient_map.get(_norm_drug(m["drug_name"]), []),
                overrides=overrides_by_key,
            )
            for m in meds_by_patient.get(pid, [])
        ]
        meds_overdue = sum(1 for t in med_tasks if t["overdue"])
        meds_due_soon = sum(1 for t in med_tasks if t["due_soon"])
        # A dose that will need a typed clinical reason at the bedside, with no
        # acceptance already on record for this order.
        meds_allergy_blocked = sum(
            1 for t in med_tasks
            if t["allergy"]["decision"] == allergy_interlock.BLOCK
            and not t["allergy"].get("override")
        )

        cards.append({
            "patient_id": pid,
            "name": p["name"],
            "mrn": p.get("mrn"),
            "bed": p.get("bed"),
            "ward": p.get("ward"),
            "age": p.get("age"),
            "sex": p.get("sex"),
            "diagnosis": p.get("diagnosis"),
            "department": p.get("admission_department"),
            "attending_doctor_name": p.get("attending_doctor_name"),
            "on_scale_2": bool(p.get("on_scale_2")),
            "allergies": allergies_by_patient.get(pid, []),
            "news2": news2,
            # Where the row NEWS2 was scored from came from ('simulated' gets
            # an honesty label on the board), and where the last taken set did.
            "vitals_source": vitals.get("source") if vitals else None,
            "last_obs_at": _iso(last_obs_at),
            "last_obs_source": last_obs.get("source") if last_obs else None,
            # The vitals row behind last_obs_at, so the shift view can offer
            # "Entered in error" on that set (POST /api/nurse/observations/{id}/void).
            "last_obs_vitals_id": last_obs.get("id") if last_obs else None,
            "obs_interval_minutes": schedule["interval_minutes"],
            "obs_band": schedule["band"],
            "obs_response": schedule["response"],
            "obs_due_at": _iso(obs_due_at),
            # Where the due time came from: the NEWS2 schedule, or the ward's
            # fixed interval while scoring is off. obs_due_note says what to
            # do when the ward has no interval set.
            "obs_due_source": "news2" if scoring else ("ward_interval" if obs_due_at else None),
            "obs_due_note": obs_due_note,
            "obs_minutes_until_due": obs_minutes_until,
            "obs_overdue": obs_overdue,
            "obs_due_soon": obs_due_soon,
            "medications": med_tasks,
            "meds_overdue": meds_overdue,
            "meds_due_soon": meds_due_soon,
            "meds_allergy_blocked": meds_allergy_blocked,
        })

    _LEVEL_RANK = {"critical": 0, "elevated": 1, "stable": 2}

    def _priority(card: dict):
        """Sickest first, then latest-running, then soonest due, then bed order.
        With scoring off nobody is "sickest": late rounds first, then due."""
        level = (card["news2"] or {}).get("level", "stable") if scoring else "stable"
        # A patient with no obs at all has no due time; sort them with the most
        # overdue rather than at the "nothing due" end.
        minutes = card["obs_minutes_until_due"]
        return (
            _LEVEL_RANK.get(level, 3),
            0 if card["obs_overdue"] else 1,
            -card["meds_overdue"],
            minutes if minutes is not None else -10_000,
            str(card.get("bed") or ""),
        )

    cards.sort(key=_priority)

    return {
        "generated_at": _iso(now),
        "department": department,
        "scoring_enabled": scoring,
        "patients": cards,
        "totals": {
            "patients": len(cards),
            # Band counts are a score: None, not 0, while scoring is off.
            "critical": (sum(1 for c in cards if (c["news2"] or {}).get("level") == "critical")
                         if scoring else None),
            "elevated": (sum(1 for c in cards if (c["news2"] or {}).get("level") == "elevated")
                         if scoring else None),
            "obs_overdue": sum(1 for c in cards if c["obs_overdue"]),
            "meds_overdue": sum(c["meds_overdue"] for c in cards),
            "meds_allergy_blocked": sum(c["meds_allergy_blocked"] for c in cards),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Observations — score preview and record
# ─────────────────────────────────────────────────────────────────────────────
def normalise_illegible(illegible) -> tuple[str, ...]:
    """The illegible field names, validated and in ILLEGIBLE_FIELDS order.

    Raises ValueError naming an unknown field, so a typo can never quietly
    turn into "nothing was illegible".
    """
    if not illegible:
        return ()
    names = {str(f).strip() for f in illegible if str(f).strip()}
    unknown = sorted(names - set(ILLEGIBLE_FIELDS))
    if unknown:
        raise ValueError(f"illegible must name readings from {list(ILLEGIBLE_FIELDS)}, "
                         f"not {unknown}")
    return tuple(f for f in ILLEGIBLE_FIELDS if f in names)


def _clean_obs(obs: dict, illegible: tuple[str, ...] = ()) -> dict:
    """Validate and coerce a submitted observation set.

    Every field is optional (a nurse may legitimately record a partial set —
    e.g. no BP on an agitated patient), but anything present must be a number
    inside OBS_RANGES. Raises ValueError with the offending field named.

    A field named in `illegible` (normalise_illegible) is stored None whatever
    was sent for it: the sheet showed a value nobody could read, and a guess is
    worse than a gap. ACVPU and oxygen marked illegible are None too, which the
    scorer already reads as "not assessed", never as alert or on air.
    """
    cleaned: dict = {}
    for field, (lo, hi, unit) in OBS_RANGES.items():
        raw = obs.get(field)
        if field in illegible:
            cleaned[field] = None
            continue
        if raw is None or raw == "":
            cleaned[field] = None
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be a number")
        if not (lo <= value <= hi):
            raise ValueError(f"{field} must be between {lo:g} and {hi:g} {unit}")
        cleaned[field] = value

    # An EMPTY consciousness or oxygen box stays empty (None), never alert and
    # never on air (Orb Ward Record, 9 October 2026). The scorer reads None as
    # "not assessed": it adds no points and labels the set incomplete, a
    # minimum. The rounds file counts it as blank. Whether a bedside nurse may
    # leave one empty is decided by the observations route
    # (require_bedside_choices), not here, because the live preview scores a
    # half typed set.
    raw_cons = obs.get("consciousness")
    consciousness = (None if raw_cons is None or str(raw_cons).strip() == ""
                     else str(raw_cons).strip().lower())
    if consciousness is not None and consciousness not in ACVPU_LEVELS:
        raise ValueError(f"consciousness must be one of {list(ACVPU_LEVELS)}")
    raw_o2 = obs.get("on_oxygen")
    cleaned["consciousness"] = None if "consciousness" in illegible else consciousness
    cleaned["on_oxygen"] = (None if "on_oxygen" in illegible or raw_o2 is None or raw_o2 == ""
                            else bool(raw_o2))

    # A set whose only entries are illegible marks is still a round on the
    # sheet, so it is recorded; a set with nothing at all is refused as before.
    if not illegible and all(cleaned[f] is None for f in OBS_RANGES) \
            and cleaned["consciousness"] is None and cleaned["on_oxygen"] is None:
        raise ValueError("an observation set must contain at least one reading")

    return cleaned


def require_bedside_choices(obs: dict, illegible: tuple[str, ...] = ()) -> None:
    """Refuse a bedside set with no ACVPU or no air or oxygen choice.

    A nurse at the bedside looks at the patient, so she always has both
    answers, and the sheet preselects neither. A box she left empty would be
    stored as empty and score as an incomplete set, so it is refused instead,
    in words she can act on. The ward clerk is not held to this: the paper
    sheet she copies can leave either box blank. Asked by the observations
    route of a caller who charts at the bedside (vitals.write), on the
    submitted body. The readings are checked first (_clean_obs), so a number
    out of range is named before a missing choice. Raises ValueError.
    """
    cleaned = _clean_obs(obs, illegible)
    no_cons = cleaned["consciousness"] is None and "consciousness" not in illegible
    no_o2 = cleaned["on_oxygen"] is None and "on_oxygen" not in illegible
    if no_cons and no_o2:
        raise ValueError("Choose the patient's level of consciousness (ACVPU) and whether "
                         "they are on air or on oxygen, then save again.")
    if no_cons:
        raise ValueError("Choose the patient's level of consciousness (ACVPU), then save again.")
    if no_o2:
        raise ValueError("Choose whether the patient is on air or on oxygen, then save again.")


def _on_scale_2(db, patient_id: str) -> bool:
    row = db.execute("SELECT on_scale_2 FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if row is None:
        raise ValueError(f"patient {patient_id} not found")
    return bool(row["on_scale_2"])


def score_observation(patient_id: str, obs: dict) -> dict:
    """NEWS2 for a candidate observation set WITHOUT persisting anything.

    This exists so the entry screen can show the score the nurse is about to
    create as they type — which is the whole clinical point of taking obs — and
    do it from the SAME scorer the record path uses. Re-implementing the
    RCP table in TypeScript for a live preview would let the previewed score and
    the recorded score diverge, which is exactly the class of bug that makes a
    ward stop trusting the number.

    ``overlay=False``: this set is NOT in the chart yet. The CfC overlay is a
    trajectory over the patient's STORED history, so running it here would
    attach a model verdict to a reading that does not exist, and the preview
    would then differ from the recorded score for the same numbers. The preview
    is the RCP table, exactly as recorded.
    """
    cleaned = _clean_obs(obs, normalise_illegible(obs.get("illegible")))
    with db_session() as db:
        scale_2 = _on_scale_2(db, patient_id)

    news2 = score_vitals_row(cleaned, on_scale_2=scale_2, overlay=False)
    return {"news2": news2, "schedule": obs_schedule(news2), "observation": cleaned}


def _same_reading(row, cleaned: dict) -> bool:
    """Is this stored vitals row the SAME measurement as the submitted set?

    The test a replay has to pass. Compared on every charted field, numerically
    (the DB round-trips REALs and an int/float difference is not a different
    measurement), so an offline queue resending the same body is recognised while
    a corrected value is not mistaken for one.
    """
    def _num_eq(a, b) -> bool:
        if a is None or b is None:
            return a is None and b is None
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return str(a) == str(b)

    for field in OBS_RANGES:
        if not _num_eq(row[field], cleaned[field]):
            return False
    if cleaned["consciousness"] is None:
        # Left empty or marked illegible: only a row that also stored no ACVPU
        # matches.
        if row["consciousness"] is not None:
            return False
    elif row["consciousness"] is None:
        # A stored empty box is not the same reading as a chosen level.
        return False
    elif str(row["consciousness"]).strip().lower() != cleaned["consciousness"]:
        return False
    if cleaned["on_oxygen"] is None:
        return row["on_oxygen"] is None
    if row["on_oxygen"] is None:
        return False
    return bool(row["on_oxygen"]) == bool(cleaned["on_oxygen"])


def _shadow_alert_dropped(episode_id, patient_id: str, level: str, reason: str) -> None:
    """Record a NEWS2 that fired and reached nobody. Never raises.

    The negatives are the half of the instrument that decides whether a pilot
    scorecard is evidence or marketing: without this row, "the nurse charted a
    deterioration and no clinician was reachable" is indistinguishable from
    "the nurse charted nothing".
    """
    if not episode_id:
        return
    try:
        from services import shadow_service
        shadow_service.emit(
            "alert.dropped", episode_id, SHADOW_LANE,
            patient_id=patient_id, payload={"reason": reason, "level": level},
        )
    except Exception:
        logger.warning("nurse obs alert: shadow alert.dropped failed for %s",
                       patient_id, exc_info=True)


def _push_vitals_update(patient_id: str, observed_at: datetime, cleaned: dict,
                        news2: dict, source: str = OBS_SOURCE) -> bool:
    """Schedule `vitals:update` for a committed nurse set. Never raises.

    The route is a sync `def`, so this runs in an anyio worker thread and the
    emit is handed to the event loop (ws.realtime.schedule_patient_vitals). The
    reading is already charted, so a failure here costs the live push, never the
    set.
    """
    try:
        from ws.realtime import schedule_patient_vitals, vitals_update_payload
        # The stored spelling, so the push matches the charted row byte for byte.
        row = {"patient_id": patient_id, "timestamp": format_utc(observed_at), **cleaned}
        return schedule_patient_vitals(
            "vitals:update", vitals_update_payload(row, news2, source=source))
    except Exception:
        logger.warning("nurse obs: vitals:update push failed for %s (the set is charted)",
                       patient_id, exc_info=True)
        return False


def dispatch_observation_alert(patient_id, cleaned: dict, news2: dict,
                               actor_id: str | None = None,
                               vitals_id: int | None = None) -> bool:
    """Raise the Vigil alert for a NURSE-TAKEN observation set.

    WHY THIS EXISTS (found 2026-08-06 by attack, not by test). Identical
    peri-arrest vitals were pushed down two paths on a live server:

        POST /api/ingest/json          → "⚠️ VIGIL ALERT [INGESTED_JSON] …"
                                          posted to the patient's Relay case room
        POST /api/nurse/.../observations → NEWS2 14, level "critical", and
                                          NOTHING. No message, no event, nobody.

    ``evaluate_and_dispatch_alerts`` belongs to the SIMULATOR loop and
    ``_check_ingested_alert`` to the HL7/FHIR/JSON feed. Manual entry — which
    main.py's lifespan calls "the ward's redundancy layer", and which is the only
    vitals path that exists at all when a monitor's VLAN is down or the ward has
    no monitors — had no escalation at all. A nurse could chart a NEWS2 19 and
    the only thing that changed was a row in a table.

    Called POST-COMMIT and non-raising, for the same two reasons the sepsis
    bundle autostart above it is: a fresh connection opened inside
    record_observation's write transaction would block on the WAL lock this
    process already holds, and an alert-delivery failure must never lose a
    reading that is already charted.

    Returns True when an alert was written. Stable NEWS2 dispatches nothing —
    an interlock that fires on everything is the alert-fatigue hazard.

    THE SHADOW EPISODE IS THREADED THROUGH HERE (2026-08-14). Until it was, this
    function called `_persist_relay_alert(patient_id, content)` with no
    episode_id, so the manual lane wrote NEITHER `alert.fired` NOR
    `alert.persisted` — zero rows for the two real alerts raised during the
    2026-08-13 dry run. The simulator lane threads an episode (bot_service) and
    the ingestion lane opens one (`_check_ingested_alert`), so the instrument
    read as healthy while being blind to the one lane the pilot goes live on.
    Same mechanism as those two, not a second dialect: `open_episode` resolves
    before it mints, so a re-alert inside the window JOINS the open episode
    instead of minting a fresh one-row episode per alert.
    """
    level = (news2 or {}).get("level", "stable")
    if level == "stable":
        return False

    from services import shadow_service

    # ── SHADOW: detection time is stamped HERE ──
    # The instant NEWS2 is known to be above threshold, BEFORE the Relay write
    # and before the event-bus publish, exactly as bot_service and
    # ingestion_service stamp it. Stamping after the write would build SQLCipher
    # write latency into the pilot's headline lead-time number.
    fired_at = shadow_service.utc_now()

    # AS CHARTED (services/vital_charting.py): the numbers NEWS2 scored and
    # its drivers print, so this line cannot read "RR 20" beside a driver
    # reading "Tachypnea (RR 21)". A vital the nurse did not record stays None
    # and prints as not recorded. It used to be defaulted to a normal reading
    # (SpO2 100, Temp 37, RR 16, HR 0) and printed as "Observed at the
    # bedside", beside a NEWS2 that had counted the same vital as missing.
    hr, sbp, dbp, spo2, rr = (as_charted(cleaned.get(k)) for k in (
        "heart_rate", "systolic_bp", "diastolic_bp", "spo2", "respiratory_rate"))
    temp = as_charted(cleaned.get("temperature"), 1)

    # Human-readable wording only, NEWS2 is the single source of truth for
    # dispatch, exactly as documented in bot_service and ingestion_service.
    # The reason line IS the NEWS2 breakdown (ruled 2026-10-02, see
    # `news2_reason_line`). This lane used to word it from its own cutoffs:
    # "Severe tachycardia" from HR 121, "SpO2 critically low" from 89 %, "SpO2
    # dropping" from one reading, Scale 2 ignored, and a 3-point SBP 85-90 or
    # RR 25 dropped whenever any other phrase fired.
    # Wording only, so it can never cost the alert (ruled 2026-10-03, G2): a
    # raise here used to drop the whole alert, leaving a
    # charted critical reading that reached nobody. No real trigger is
    # known; the fallback makes "wording never gates dispatch" true anyway.
    try:
        reason_text = news2_reason_line(news2)
    except Exception:
        logger.exception("reason line failed for %s; sending the band alone", patient_id)
        reason_text = f"NEWS2 {level}"
    raw = news2.get("raw_news2", news2.get("raw_mews"))
    content = (
        f"⚠️ VIGIL ALERT [NURSE OBS]: {reason_text}\n"
        f"Observed at the bedside: "
        f"{vitals_line(hr=hr, sbp=sbp, dbp=dbp, spo2=spo2, temp=temp, rr=rr)} · "
        f"ACVPU {cleaned.get('consciousness') or 'not assessed'}"
        f"{' · on O2' if cleaned.get('on_oxygen') else ''}\n"
        f"{news2_description(news2)}. Immediate review recommended."
    )

    # ── SHADOW: open (or JOIN) this patient's episode and record the detection.
    # FAIL-OPEN, and the try/except is not decoration: shadow_service swallows
    # its own exceptions, but a monkeypatched/rewritten instrument must still be
    # unable to lose an observation that is already charted. `episode_id` stays
    # None on any failure and every later shadow write is skipped — the alert
    # itself is untouched either way.
    episode_id = None
    try:
        episode_id, opened_now = shadow_service.open_episode(
            patient_id, SHADOW_LANE,
            occurred_at=fired_at, trigger="news2_threshold",
            payload={"level": level, "raw_news2": raw},
        )
        if episode_id:
            shadow_service.emit(
                "alert.fired", episode_id, SHADOW_LANE,
                occurred_at=fired_at, patient_id=patient_id,
                actor_user_id=actor_id,
                subject_type="vitals", subject_id=vitals_id,
                payload={
                    "level": level,
                    "raw_news2": raw,
                    "score": news2.get("score"),
                    "drivers": (news2.get("drivers") or [])[:6],
                    "single_param_3": bool(news2.get("single_param_3")),
                    "spo2_scale": news2.get("spo2_scale"),
                    "vital_id": vitals_id,
                    # This alert joined an episode that was already open — the
                    # same meaning `repeat` carries on the simulator lane.
                    "repeat": not opened_now,
                },
            )
    except Exception:
        episode_id = None
        logger.warning("nurse obs alert: shadow instrument failed (fail-open) for %s",
                       patient_id, exc_info=True)

    try:
        from services.ingestion_service import _persist_relay_alert
        logged, msg_id, channel_id, msg_data, _name = _persist_relay_alert(patient_id, content)
    except Exception as e:
        logger.error("nurse obs alert: Relay write FAILED for %s: %s", patient_id, e)
        _shadow_alert_dropped(episode_id, patient_id, level, "relay_write_failed")
        return False

    if not logged:
        # No case room for this patient. Still loud in the log — a deterioration
        # that reached nobody must not be invisible to whoever debugs it later.
        logger.error(
            "nurse obs alert: patient %s has NO case room — NEWS2 %s (%s) reached nobody",
            patient_id, raw, level,
        )
        _shadow_alert_dropped(episode_id, patient_id, level, "no_case_room")
        return False

    # PUSH IT TO THE ROOM (2026-10-03). The row was stored and the message data
    # thrown away, so anyone with the case room open did not see a bedside
    # NEWS2 19 until they reopened it (the channel list polls; an open
    # conversation only grows on `message:new`). The simulator and ingestion
    # lanes push theirs; this is the same event to the same room. Never raises,
    # and the alert is already on the disk either way.
    if msg_data:
        try:
            from ws import realtime
            realtime.schedule_on_loop(
                lambda: realtime.sio.emit("message:new", msg_data, room=channel_id),
                f"message:new for nurse obs alert {msg_id}")
        except Exception:
            logger.warning("nurse obs alert: message:new push failed for %s",
                           patient_id, exc_info=True)

    # SHADOW: the alert is on the disk and addressed to a room. Emitted in its
    # own short transaction rather than inside `_persist_relay_alert`'s: that
    # function belongs to the ingestion lane and hard-codes lane='ingestion' on
    # the rows it writes, and a manual-lane alert filed under 'ingestion' leaves
    # the scorecard exactly as blind to bedside entry as no row at all. This
    # runs POST-COMMIT with no transaction held, so the self-deadlock rule at
    # the top of shadow_service does not apply.
    if episode_id:
        try:
            shadow_service.emit(
                "alert.persisted", episode_id, SHADOW_LANE,
                patient_id=patient_id, subject_type="messages", subject_id=msg_id,
                payload={"message_id": msg_id, "channel_id": channel_id},
            )
        except Exception:
            logger.warning("nurse obs alert: shadow alert.persisted failed for %s",
                           patient_id, exc_info=True)

    # ── THE NERVOUS SYSTEM, from the manual lane (2026-08-14) ────────────────
    # `vigil.alert.critical` had exactly one publisher — bot_service's simulator
    # loop — so a nurse charting peri-arrest vitals raised a Relay message and
    # left the prewarm asleep: no care bundle opened, no RAG context assembled,
    # and Sage cold when the registrar opens the chart. Through bot_service's
    # shared publisher so both lanes emit the SAME event; it never raises and
    # the alert is already written, so nothing clinical depends on it.
    try:
        from services.bot_service import publish_vigil_critical
        publish_vigil_critical(
            patient_id,
            level=level,
            score=news2.get("score"),
            drivers=news2.get("drivers") or [],
            message_id=msg_id,
            raw_news2=raw,
            lane=SHADOW_LANE,
        )
    except Exception as e:
        logger.warning("nurse obs alert: event bus publish failed for %s: %s", patient_id, e)

    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="clinical_access",
            action_type="observation_alert_generated",
            outcome="success",
            summary_message=(
                f"Nurse observation raised a Vigil alert for {patient_id}: "
                f"{news2_description(news2)}"
            ),
            actor_user_id=actor_id,
            target_type="message",
            target_id=str(msg_id),
            patient_id=patient_id,
            metadata={"reason": reason_text, "risk_level": level, "raw_news2": raw,
                      "channel_id": channel_id, "vitals_id": vitals_id,
                      "source": OBS_SOURCE},
        )
    except Exception as e:
        logger.warning("nurse obs alert audit failed for %s: %s", patient_id, e)

    logger.warning("nurse obs alert: patient %s NEWS2 %s (%s) posted to %s",
                   patient_id, raw, level, channel_id)
    return True


# Stand-in ManualEntrySource for a process that never ran configure_from_env
# (unit tests, scripts). Charting must not fail because observability plumbing
# is absent, so an unregistered instance carries the counters instead.
_standalone_manual_source = None


def _manual_entry_source():
    """The registered manual-entry source, or a module-local stand-in.

    `configure_from_env` always registers manual entry, so in production this
    is the registry's instance and every nurse write lands in the stats the
    admin surface actually shows.
    """
    from services.vitals_source import ManualEntrySource, manual_entry_source

    global _standalone_manual_source
    source = manual_entry_source()
    if source is not None:
        return source
    if _standalone_manual_source is None:
        # sink=None is safe: record_observation() never touches the sink.
        _standalone_manual_source = ManualEntrySource(None)
    return _standalone_manual_source


def record_observation(
    patient_id: str,
    actor_id: str,
    obs: dict,
    *,
    idempotency_key: str | None = None,
    taken_at: str | None = None,
    source: str | None = None,
    illegible: list | None = None,
) -> dict:
    """Record a nurse-entered observation set and its NEWS2.

    `source` is None for a bedside set (OBS_SOURCE) or CLERK_SOURCE for a set
    the ward clerk copied from the paper sheet; `illegible` names readings the
    sheet showed but nobody could read (see _clean_obs).

    Routed through `ManualEntrySource.record_observation` — the provenance seam
    every manual vitals write shares — so the write is counted on the same
    source-health surface as the monitor feed and the label it stamps is the
    one `classify_source_label` knows. The wrapper re-raises everything: this
    path stays FAIL-LOUD (an observation feeds an escalation decision; it is
    never queued or optimistic). The actual write is `_record_observation_txn`
    below, unchanged in transaction shape, scoring and permissions.
    """
    return _manual_entry_source().record_observation(
        patient_id, actor_id, obs,
        idempotency_key=idempotency_key, taken_at=taken_at,
        source=source, illegible=illegible)


def scoring_enabled() -> bool:
    """ORB_SCORING_ENABLED, read at call time so a test or an operator flip is
    seen without re-importing this module. Through vigil_service, so the Ward
    Record profile (ORB_WARD_RECORD) switches it off here too."""
    from services.vigil_service import scoring_enabled as _vigil_scoring_enabled
    return _vigil_scoring_enabled()


def _record_observation_txn(
    patient_id: str,
    actor_id: str,
    obs: dict,
    *,
    idempotency_key: str | None = None,
    taken_at: str | None = None,
    source: str | None = None,
    illegible: list | None = None,
) -> dict:
    """The transactional write behind `record_observation`.

    Writes, in one transaction: the ``vitals`` row (source='nurse_manual'), the
    ``risk_scores`` row the Vigil board and Command Center read, and a
    fail-closed audit entry — same pattern as emar_service.record_administration,
    so an observation can never commit unaudited.

    Idempotent on (patient, source, observation time): the client stamps
    ``taken_at`` when the nurse hits Save and the offline queue replays that body
    verbatim, so a replayed set lands on the identical timestamp and is
    recognised rather than charted twice. Deduping on the OBSERVATION TIME
    rather than on a request id is the stronger guarantee clinically — it also
    catches a double-tap or a second device charting the same reading, which a
    per-request key would happily let through as two genuine measurements.
    ``idempotency_key`` is accepted for symmetry with the rest of the offline
    path and recorded in the audit metadata, but it is not the dedupe key.

    A REPLAY IS AN IDENTICAL SET, NOT MERELY AN IDENTICAL TIME (fixed 2026-08-06,
    found by attack). The dedupe used to key on (patient, source, time) alone and
    then compute NEWS2 from the SUBMITTED values before returning early. So a
    nurse who mis-keyed a set and re-submitted the same observation time with the
    real, peri-arrest values got HTTP 200, ``replayed: true``, NEWS2 19,
    ``level: critical`` and the emergency-response schedule — while the chart kept
    the earlier benign row and the board went on reading "All vitals within
    normal range". The response described a reading that was never stored, which
    is worse than either accepting or refusing it. The dedupe now compares the
    VALUES too: same time + same values is a replay, same time + different values
    is a new reading and is charted.

    THE CLERK LANE (Orb Ward Record, 6 October 2026). `source=CLERK_SOURCE`
    stores the set under 'clerk_transcribed'; `taken_at` is then the time
    written on the paper sheet, which may be days back. Every set, either lane,
    also stores `entered_at`, the UTC time it was keyed, so the clerk's entry lag
    is reportable on its own and never moves the observation time. The replay
    check compares against rows of the SAME source: a clerk keying the nurse's
    own set is a second record, not a replay of hers.
    """
    if source is None:
        source = OBS_SOURCE
    if source not in MANUAL_OBS_SOURCES:
        raise ValueError(f"unknown observation source {source!r}")
    illegible_fields = normalise_illegible(illegible)
    illegible_text = ",".join(illegible_fields) or None
    cleaned = _clean_obs(obs, illegible_fields)
    entered_at = format_utc(_now())
    # ORB_SCORING_ENABLED off (the Orb Ward Record): the vitals row and its
    # audit entry are the whole write. No NEWS2 is computed, stored, returned or
    # logged, and nothing after the commit runs: no push, no sepsis bundle, no
    # Relay alert, no escalation chain.
    scoring = scoring_enabled()
    # In UTC, and stored in the one spelling every vitals/risk_scores writer
    # uses (services/clinical_time.py). `_parse_ts` keeps a caller's offset, so
    # an API caller's '+05:30' used to be stored as typed beside '+00:00' rows,
    # and text ORDER BY across the two is not time order. `stored_at` is also
    # the replay-dedupe key, so both sides of that equality use one spelling.
    observed_at = (_parse_ts(taken_at) or _now()).astimezone(timezone.utc)
    stored_at = format_utc(observed_at)
    if observed_at > _now() + timedelta(minutes=5):
        raise ValueError("observation time cannot be in the future")

    with db_session() as db:
        scale_2 = _on_scale_2(db, patient_id)

        superseded_id = None
        if taken_at:
            existing = db.execute(
                """SELECT id, timestamp, heart_rate, systolic_bp, diastolic_bp, spo2,
                          temperature, respiratory_rate, consciousness, on_oxygen
                     FROM vitals
                    WHERE patient_id = ? AND source = ? AND timestamp = ?
                    ORDER BY id DESC LIMIT 1""",
                (patient_id, source, stored_at),
            ).fetchone()
            if existing is not None and _same_reading(existing, cleaned):
                news2 = (score_vitals_row(cleaned, on_scale_2=scale_2, overlay=False)
                         if scoring else None)
                return {
                    "vitals_id": existing["id"],
                    "recorded_at": existing["timestamp"],
                    "news2": news2,
                    "schedule": obs_schedule(news2) if scoring else None,
                    "replayed": True,
                    "source": source,
                    "illegible": list(illegible_fields),
                }
            if existing is not None:
                # Same observation time, DIFFERENT values: a correction or a
                # second measurement, never a replay. Chart it, and say which row
                # it supersedes so the correction is visible rather than implied.
                superseded_id = existing["id"]
                logger.warning(
                    "nurse obs: patient %s already has a %s reading at %s with different "
                    "values (vitals %s) — charting the new set as a correction",
                    patient_id, source, stored_at, superseded_id,
                )

        # ``overlay=False`` — MANDATORY here, not a preference: we are inside an
        # open write transaction on `db`, and the CfC overlay reads the patient's
        # vitals history on a FRESH connection. That connection would block on
        # the WAL write lock this transaction holds (the self-deadlock documented
        # on ingestion_service._persist_relay_alert). The recorded, audited score
        # is the deterministic RCP aggregate; the overlay is a display-time
        # escalation applied by score_vitals_row wherever the score is shown.
        news2 = (score_vitals_row(cleaned, on_scale_2=scale_2, overlay=False)
                 if scoring else None)

        # Provenance contract shared with the ingestion sink's vitals write:
        # `source` is a label _LABEL_KIND classifies (here OBS_SOURCE → MANUAL);
        # `source_event_id` links a row to its raw wire event and stays NULL
        # here because a typed set has no wire payload — the fail-closed audit
        # entry below is this path's evidence row.
        cur = db.execute(
            """INSERT INTO vitals
                   (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                    spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                    source, entered_at, illegible)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (patient_id, stored_at, cleaned["heart_rate"],
             cleaned["systolic_bp"], cleaned["diastolic_bp"], cleaned["spo2"],
             cleaned["temperature"], cleaned["respiratory_rate"],
             cleaned["consciousness"],
             None if cleaned["on_oxygen"] is None else int(cleaned["on_oxygen"]),
             source, entered_at, illegible_text),
        )
        vitals_id = cur.lastrowid

        if scoring:
            db.execute(
                "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) VALUES (?, ?, ?, ?, ?)",
                (patient_id, stored_at, news2["score"], news2["level"],
                 json.dumps(news2["drivers"])),
            )

        # Is this set now the chart's newest? Decided here, where the INSERT
        # above is visible, with the ordering a chart reload uses. A set
        # back-charted behind a newer one is history and is not pushed.
        is_latest = is_latest_vitals_row(db, patient_id, vitals_id)

        # Fail-closed clinical audit inside the SAME transaction: the reading and
        # its audit row commit or roll back together.
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="clinical_access",
            action_type="observation_recorded",
            outcome="success",
            db=db,
            summary_message=(
                (f"Nurse observation set recorded for {patient_id} "
                 if source == OBS_SOURCE else
                 f"Observation set copied from the paper sheet for {patient_id} ")
                + (f"({news2_description(news2)}) by {actor_id}" if scoring
                   else f"(scoring switched off) by {actor_id}")
            ),
            actor_user_id=actor_id,
            target_type="vitals",
            target_id=str(vitals_id),
            patient_id=patient_id,
            metadata={
                "vitals_id": vitals_id,
                "observed_at": stored_at,
                "entered_at": entered_at,
                "illegible": list(illegible_fields),
                # None, never a stand-in, while scoring is off: "no score
                # computed, shown or logged" covers the audit row too.
                "news2": news2["raw_news2"] if scoring else None,
                "level": news2["level"] if scoring else None,
                "single_param_3": news2["single_param_3"] if scoring else None,
                "spo2_scale": news2["spo2_scale"] if scoring else None,
                "scoring_enabled": scoring,
                "source": source,
                "idempotency_key": idempotency_key,
                "supersedes_vitals_id": superseded_id,
                **{k: cleaned[k] for k in OBS_RANGES},
                "consciousness": cleaned["consciousness"],
                "on_oxygen": cleaned["on_oxygen"],
            },
        )

    # Post-commit: tell every open chart (2026-10-03). The Vigil chart updates
    # its command bar, badge and NEWS2 breakdown from `vitals:update`, and this
    # lane sent none, so a doctor with the chart open kept the older set and its
    # score until a reload, which on a ward with no monitors can be hours. First
    # after commit, before the alert, the same order as the simulator and
    # ingestion lanes. It is a push only and pages nobody: the alert below is
    # this set's one page. `news2` IS score_vitals_row on the values just
    # inserted, with the patient's scale.
    if not scoring:
        logger.info("nurse obs: patient %s set %s recorded by %s (scoring switched off)",
                    patient_id, vitals_id, actor_id)
        # Scoring switched off (the Orb Ward Record): the open chart still gets
        # the new set, with risk None, as the simulator, monitor and Scribe
        # lanes send it; the chart shows it with no score. Nothing pages.
        if is_latest:
            _push_vitals_update(patient_id, observed_at, cleaned, None, source=source)
        result = {
            "vitals_id": vitals_id,
            "recorded_at": stored_at,
            "news2": None,
            "schedule": None,
            "replayed": False,
            "source": source,
            "entered_at": entered_at,
            "illegible": list(illegible_fields),
            "scoring_enabled": False,
        }
        if superseded_id is not None:
            result["supersedes_vitals_id"] = superseded_id
        return result

    if is_latest:
        _push_vitals_update(patient_id, observed_at, cleaned, news2, source=source)

    # Post-commit, non-raising: opening a sepsis bundle at NEWS2>=7 is documented
    # in sepsis_bundle_service as safe to call from any risk path, and a
    # nurse-taken set is exactly such a path.
    try:
        from services.sepsis_bundle_service import maybe_autostart
        maybe_autostart(patient_id, news2)
    except Exception as e:  # never let a bundle failure lose a recorded reading
        logger.warning("sepsis bundle autostart after nurse obs failed for %s: %s", patient_id, e)

    # Post-commit escalation. See dispatch_observation_alert for why the ward's
    # OWN observations have to raise the same alert a monitor feed does.
    # Never raising: the reading is already committed and a failure to DELIVER an
    # alert must not throw away the chart entry that proves the deterioration.
    try:
        dispatch_observation_alert(patient_id, cleaned, news2, actor_id, vitals_id)
    except Exception as e:
        logger.error("nurse obs alert dispatch failed for %s (reading %s is charted): %s",
                     patient_id, vitals_id, e)

    # ── The ESCALATION CHAIN, raised from the WRITE PATH (2026-08-14) ──────────
    #
    # The Relay alert above tells the case room. The chain is the other half: the
    # tiered, timed, acknowledgeable record that says WHO owns this deterioration
    # and escalates when nobody answers. Until now NO vitals writer called
    # `evaluate_observation` — grep it: the only caller was
    # `scan_new_observations`, the safety net — so the chain existed only when
    # somebody happened to open the escalation board.
    #
    # That was not merely slow, it LOST SCORES. The safety net's
    # "already open, already at least this loud" branch advances the chain's
    # `last_vitals_id` watermark past any observation whose ENTRY TIER the open
    # chain already covers, and tier 2 is the highest entry tier the policy has
    # (tier 3 has no `entry_min_news2`). So on a patient with an open tier-2
    # chain, a bedside NEWS2 19 was swallowed whole: no chain event, no
    # `peak_news2` update, and the watermark moved past it so it could never be
    # reconsidered. MEASURED on this appliance 2026-08-13 — the case room showed
    # "NEWS2 19 (critical)" while the escalation board showed NEWS2 10 with the
    # drivers of an unrelated, milder monitor reading, for the same patient in the
    # same minute. Two numbers, one patient; the board is the one a registrar
    # triages from.
    #
    # `evaluate_observation` is the documented entry point for exactly this and
    # handles the open-chain case correctly (records the observation on the chain,
    # raises `peak_news2`, re-opens at a higher tier only when the score earns
    # one). It is idempotent, it does nothing at all below threshold, and it opens
    # its own transaction — hence POST-COMMIT, like the two calls above, and
    # never-raising for the same reason: the reading is already charted and a
    # delivery failure must not throw it away.
    #
    # notify=False, DELIBERATELY, and this is the one judgement call here.
    # `_notify` posts the chain into the patient's Relay case room — but
    # `dispatch_observation_alert`, three lines up, has ALREADY posted this exact
    # observation there, carrying the same score and the same drivers. Letting
    # both fire puts two messages in the room for one set of obs, every time a
    # nurse charts, which is precisely the alert-fatigue failure the cooldown in
    # bot_service and the `level == "stable"` guard in dispatch_observation_alert
    # exist to prevent. The chain is not silent for it: it is on the escalation
    # board, it is addressed to a named tier, it stays open until acknowledged,
    # and `advance_due` still posts loudly when the clock runs out — which is the
    # notification that carries information the ward does not already have.
    try:
        from services import escalation_service
        escalation_service.evaluate_observation(
            patient_id, news2, vitals_id=vitals_id, source=source,
            actor_id=actor_id, observed_at=observed_at, notify=False,
        )
    except Exception as e:
        logger.error("nurse obs escalation failed for %s (reading %s is charted): %s",
                     patient_id, vitals_id, e)

    logger.info(
        "nurse obs: patient %s NEWS2 %s (%s) by %s",
        patient_id, news2["raw_news2"], news2["level"], actor_id,
    )
    result = {
        "vitals_id": vitals_id,
        "recorded_at": stored_at,
        "news2": news2,
        "schedule": obs_schedule(news2),
        "replayed": False,
        "source": source,
        "entered_at": entered_at,
        "illegible": list(illegible_fields),
    }
    if superseded_id is not None:
        result["supersedes_vitals_id"] = superseded_id
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Entered in error (Orb Ward Record, 6 October 2026)
# ─────────────────────────────────────────────────────────────────────────────
class ObservationNotFound(LookupError):
    """No vitals row with that id."""


class ObservationAlreadyVoided(ValueError):
    """The set was already marked entered in error."""


def observation_patient(vitals_id: int) -> str | None:
    """The patient a stored observation set belongs to, or None."""
    with db_session() as db:
        row = db.execute("SELECT patient_id FROM vitals WHERE id = ?", (vitals_id,)).fetchone()
    return row["patient_id"] if row else None


def void_observation(vitals_id: int, actor_id: str, reason: str) -> dict:
    """Mark a stored observation set as entered in error.

    The row is NEVER deleted or edited: voided_at, voided_by and void_reason
    are set and every latest, window and trend reader skips it from then on
    (each one filters `voided_at IS NULL`). The original values stay in the
    table and in the audit chain, which is what the paper rule asks of a
    correction: struck through and signed, still readable.

    Writes, in one transaction, the three void columns and a fail-closed audit
    row, the same pattern as the observation insert, so a set can never be
    voided unaudited. A second void of the same set is refused rather than
    overwriting the first reason.

    The NEWS2 the set produced in risk_scores is left as it was: that table has
    no link to the vitals row, and the board, the ward brief and Sage's insights
    score the latest vitals, not risk_scores.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Say why this set is wrong.")
    if len(reason) > 500:
        raise ValueError("Keep the reason to 500 characters or fewer.")
    voided_at = format_utc(_now())
    with db_session() as db:
        row = db.execute(
            "SELECT id, patient_id, timestamp, source, voided_at FROM vitals WHERE id = ?",
            (vitals_id,),
        ).fetchone()
        if row is None:
            raise ObservationNotFound(f"observation {vitals_id} not found")
        if row["voided_at"]:
            raise ObservationAlreadyVoided(
                "This set is already marked entered in error. Reload to see the record.")
        db.execute(
            "UPDATE vitals SET voided_at = ?, voided_by = ?, void_reason = ? "
            "WHERE id = ? AND voided_at IS NULL",
            (voided_at, actor_id, reason, vitals_id),
        )
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="clinical_access",
            action_type="observation_voided",
            outcome="success",
            db=db,
            summary_message=(
                f"Observation set {vitals_id} for {row['patient_id']} marked entered "
                f"in error by {actor_id}"
            ),
            actor_user_id=actor_id,
            target_type="vitals",
            target_id=str(vitals_id),
            patient_id=row["patient_id"],
            metadata={
                "vitals_id": vitals_id,
                "observed_at": row["timestamp"],
                "source": row["source"],
                "voided_at": voided_at,
                "reason": reason,
            },
        )
    logger.info("observation %s for %s marked entered in error by %s",
                vitals_id, row["patient_id"], actor_id)
    return {
        "vitals_id": vitals_id,
        "patient_id": row["patient_id"],
        "voided_at": voided_at,
        "voided_by": actor_id,
        "void_reason": reason,
    }
