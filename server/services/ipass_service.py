"""Orb — I-PASS Shift-Handoff Automation.

Auto-drafts a structured I-PASS handoff from the last ~12h of a patient's data.

I-PASS (Illinois/AAP structured verbal + written handoff standard) sections:
  I  — Illness severity        (Stable / Watcher / Unstable)
  P  — Patient summary         (demographics, diagnosis, active problems, allergies)
  A  — Action list             (active meds, open care bundles, outstanding items)
  S  — Situation awareness      (vitals trend, active deterioration, contingencies)
  S  — Synthesis by receiver    (read-back confirmation placeholder)

The draft is assembled DETERMINISTICALLY from real DB data — no LLM call is
required, so the handoff never fabricates content and is safe when Ollama is
down. An optional best-effort LLM synthesis line can be layered on top; if the
model is unavailable it silently degrades to the deterministic text.

Persistence reuses the existing `scribe_notes` schema with note_format='handoff'
(raw_transcript = source summary, structured_note = rendered handoff, status='draft').

THE AI-DRAFTED SUPPLEMENT (see `get_reasoned_assessment`)
--------------------------------------------------------
Sage's full four-section reasoned assessment is measured at 42-57 s end to end.
At a deteriorating bedside that is useless — 45 seconds is past the point where
a second opinion changes anything. On THIS surface it costs nothing: the
handover is composed ahead of a shift change and the ward round is walked
patient by patient, so the clinician is REVIEWING rather than reacting.

It is therefore carried here, in two halves that are deliberately separate:

  READING (`get_reasoned_assessment`, and `build_ipass`) NEVER GENERATES and
  never waits. It reuses a `full` draft Sage already wrote, so the sheet says
  what the ward was actually shown rather than a second, slower, subtly
  different opinion. Reuse is preferred over generation ALWAYS. See the reuse
  rule and its staleness bound on `get_reasoned_assessment`.

  ASKING (`prepare_reasoned_assessment`) starts one, and returns immediately.
  Since migration 30 the ALERT lane writes `depth='brief'` — Tier 1 + at most
  three next steps — so there is usually no `full` draft to reuse unless
  something asked for one. That ask is placed where nobody is waiting: the
  handover is composed ahead of the change-over, and the round can prepare the
  next patient while the clinician is still at this one.

If the answer is not back yet, the sheet says NOTHING. It never blocks and it
never shows a spinner promising an assessment that is still 40 seconds away.

The section is labelled as machine-authored in BOTH the structured payload and
`plain_text`, and it is stamped with the NEWS2 it was written against and how
long ago — the rest of the sheet is current and this section is a snapshot. A
supplement that reads as though a clinician wrote it, or as though it describes
the patient now, is worse than no supplement at all.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta, date

from database import db_session, run_db
from services.vigil_service import get_vitals_trend, risk_for_row, scoring_enabled
from services.hospital_time import hospital_stamp
from services.vital_charting import charted_text
from services.news2_text import news2_qualifiers

logger = logging.getLogger("orb.ipass")

# Default look-back window for auto-drafting a handoff.
HANDOFF_WINDOW_HOURS = 12

#: What an empty allergy list is called on the handover. The same words the
#: nurse round, the administer screen and the patient card use.
NO_ALLERGIES_RECORDED = "No allergies recorded. Confirm with the patient."

#: What the receiver records at read-back, as labelled prompts. The prose used
#: to carry underscore blanks ("Confirmed by: ____________"), which read as an
#: unfinished template on screen; the printed sheet gives each its own line.
READ_BACK_PROMPTS = ("Outstanding questions", "Confirmed by", "Time")

# NEWS2 clinical band → I-PASS illness-severity bucket.
_SEVERITY_MAP = {
    "critical": "Unstable",
    "elevated": "Watcher",
    "stable": "Stable",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _num(v, digits: int = 0):
    """Format a numeric vital for display, tolerating None.

    AS CHARTED (services/vital_charting.py), the number NEWS2 scored: the same
    sheet prints the drivers, and f"{20.5:.0f}" gave "RR 20" two lines below
    "Tachypnea (RR 21)".
    """
    return charted_text(v, digits)


# ─────────────────────────────────────────────────────────────────────────────
# Machine enums → clinician prose
# ─────────────────────────────────────────────────────────────────────────────
# THE PRINTED SHEET NEVER PASSES THROUGH THE BROWSER. `src/lib/humanise.ts`
# exists for the same reason and cannot help here: `plain_text` below is what a
# nurse prints, copies into a WhatsApp handover and staples into the notes, and
# it is assembled entirely on the server. It shipped reading
#
#     Trend over the shift: insufficient_data (insufficient recent data)
#
# — a raw Python identifier, standing next to its own English translation, in a
# medico-legal document. A handover that prints its own internals reads as a
# machine's scratchpad rather than a clinical statement, and "insufficient_data"
# is precisely the line a receiving clinician must not skim past.
#
# The WIRE FORMAT IS UNCHANGED: `sections[...]["trend"]`, `["news2_level"]` and
# `["bundle_type"]` still carry the raw enum for the UI and for anything
# downstream that matches on it. Only the rendered prose is humanised.

#: Tokens that must keep their conventional casing when humanised generically.
_ACRONYMS: dict[str, str] = {
    "news2": "NEWS2", "spo2": "SpO2", "bp": "BP", "hr": "HR", "rr": "RR",
    "acvpu": "ACVPU", "iv": "IV", "icu": "ICU", "prn": "PRN", "nkda": "NKDA",
    "ai": "AI", "mrn": "MRN", "ipass": "I-PASS", "od": "OD", "gcs": "GCS",
}


def humanise_enum(value, fallback: str = "not recorded", *, sentence: bool = False) -> str:
    """`insufficient_data` → `insufficient data`; `phi_access` → `PHI access`.

    The SERVER's copy of the rule in `src/lib/humanise.ts`. Default is lower
    case because everything this renders is mid-sentence; `sentence=True`
    capitalises the first word for a line that starts one.

    A value with no underscore passes through unchanged apart from case, so this
    is safe to wrap around any enum — including ones added later, which is the
    point: the invariant asserted in tests/test_ipass.py is "no snake_case token
    reaches the printed sheet", not "these four strings are translated".
    """
    if value is None:
        return fallback
    words = [w for w in str(value).strip().replace("-", "_").split("_") if w]
    if not words:
        return fallback
    out = [_ACRONYMS.get(w.lower(), w.lower()) for w in words]
    if sentence and out[0] not in _ACRONYMS.values():
        out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


#: Trend vocabulary from `vigil_service.get_vitals_trend`. Each phrase is a
#: clinical statement, not a label: "insufficient_data" carries a real and
#: specific meaning ("nothing to compare against"), and printing that meaning is
#: the difference between an honest handover and a blank one.
_TREND_PHRASES: dict[str, str] = {
    "improving": "improving",
    "deteriorating": "deteriorating",
    "stable": "stable",
    "insufficient_data": "not enough data to call a trend",
}

#: NEWS2 clinical bands from `vigil_service.get_patient_risk`.
_LEVEL_PHRASES: dict[str, str] = {
    "critical": "critical band",
    "elevated": "elevated band",
    "stable": "stable band",
}

#: Care-bundle identifiers from `sepsis_bundle_service`.
_BUNDLE_NAMES: dict[str, str] = {
    "sepsis6": "Sepsis Six",
    "deterioration": "Deterioration",
}

#: Assessment states from `sage_assessment_service`. Only the two READABLE ones
#: are described, because only those two are ever carried onto the sheet.
_ASSESSMENT_STATE_PHRASES: dict[str, str] = {
    "ready": "no clinician has reviewed this draft",
    "approved": "a clinician read this draft and agreed with the reasoning",
}

#: `citation_basis` from `sage_assessment_service._parse_assessment`. Each is a
#: DIFFERENT evidential claim and the sheet must not blur them — "Sage cited
#: this guideline" and "Orb matched Sage's wording to this guideline afterwards"
#: are not the same statement about where the reasoning came from.
_CITATION_PHRASES: dict[str, str] = {
    "model_cited": "Sage cited the guideline(s) listed below",
    "system_attributed": ("Sage cited nothing; Orb matched its wording to the "
                          "guideline(s) listed below"),
    "retrieved": ("guidelines were retrieved but the wording could not be tied "
                  "to any of them"),
    "none": ("no guideline in Orb's corpus matched this alert. This is model "
             "reasoning over the chart alone"),
}

#: `confidence` from `sage_service.classify_answer_confidence`.
_CONFIDENCE_PHRASES: dict[str, str] = {
    "high": "high confidence",
    "medium": "medium confidence",
    "low": "low confidence",
}


def _trend_phrase(trend_label) -> str:
    return _TREND_PHRASES.get(str(trend_label or "").lower(),
                              humanise_enum(trend_label, "trend not assessed"))


def _level_phrase(level) -> str:
    return _LEVEL_PHRASES.get(str(level or "").lower(),
                              humanise_enum(level, "band not assessed"))


def _bundle_label(bundle_type) -> str:
    key = str(bundle_type or "").strip().lower()
    if key in _BUNDLE_NAMES:
        return _BUNDLE_NAMES[key]
    return humanise_enum(bundle_type, "care bundle", sentence=True)


def _assessment_state_phrase(state) -> str:
    return _ASSESSMENT_STATE_PHRASES.get(
        str(state or "").lower(), humanise_enum(state, "review status not recorded"))


def _citation_phrase(basis) -> str:
    return _CITATION_PHRASES.get(
        str(basis or "").lower(), humanise_enum(basis, "evidence basis not recorded"))


def _confidence_phrase(confidence) -> str:
    return _CONFIDENCE_PHRASES.get(
        str(confidence or "").lower(),
        humanise_enum(confidence, "confidence not recorded"))


def _stamp_phrase(created_at) -> str:
    """'14 Aug 2026, 19:29 IST'. A PRINTED sheet outlives the moment it was
    printed, so "2 hours ago" alone is not a timestamp on a medico-legal
    document: it is only a timestamp while you are holding it.

    Hospital time, not UTC: a UTC clock on an IST ward sheet read 5.5 hours
    wrong to every nurse holding it."""
    return hospital_stamp(created_at)


def _ago_phrase(minutes: int) -> str:
    """'2 hours ago' / '40 minutes ago'. Never a bare number and never `None`."""
    if minutes is None:
        return "at an unrecorded time"
    minutes = max(0, int(minutes))
    if minutes < 1:
        return "moments ago"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    rest = minutes % 60
    head = f"{hours} hour{'s' if hours != 1 else ''}"
    if rest:
        head += f" {rest} minute{'s' if rest != 1 else ''}"
    return f"{head} ago"


# ─────────────────────────────────────────────────────────────────────────────
# Data assembly (each helper reads exactly one clinical-core table)
# ─────────────────────────────────────────────────────────────────────────────
def _get_patient(db, patient_id: str) -> dict | None:
    row = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    return dict(row) if row else None


def _compute_age(patient: dict) -> int | None:
    age = patient.get("age")
    if age is not None:
        return int(age)
    dob = patient.get("date_of_birth")
    if dob:
        try:
            born = datetime.fromisoformat(str(dob).replace("Z", "+00:00")).date()
            today = date.today()
            return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        except (ValueError, TypeError):
            return None
    return None


def _get_vitals_window(db, patient_id: str, cutoff_iso: str) -> list[dict]:
    """Vitals recorded within the window, chronological. Falls back to the most
    recent single reading if nothing was recorded in the window.

    id ASC breaks a same-timestamp tie, so a correction charted at the same
    bedside time as the set it supersedes is LAST, and vitals[-1] is the
    reading the handoff prints and scores."""
    rows = db.execute(
        """SELECT * FROM vitals WHERE patient_id = ? AND timestamp >= ?
             AND voided_at IS NULL
           ORDER BY timestamp ASC, id ASC""",
        (patient_id, cutoff_iso),
    ).fetchall()
    if rows:
        return [dict(r) for r in rows]
    latest = db.execute(
        "SELECT * FROM vitals WHERE patient_id = ? AND voided_at IS NULL "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (patient_id,),
    ).fetchone()
    return [dict(latest)] if latest else []


def _get_active_meds(db, patient_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT drug_name, dosage, route, frequency, notes, status, source, verified_by
           FROM medications WHERE patient_id = ? AND status IN ('active','prn')
           ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'prn' THEN 1 ELSE 2 END, started_at DESC""",
        (patient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_active_problems(db, patient_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT problem, icd10_code FROM problem_list
           WHERE patient_id = ? AND status = 'active'
           ORDER BY recorded_at DESC""",
        (patient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_active_allergies(db, patient_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT substance, reaction, severity FROM patient_allergies
           WHERE patient_id = ? AND status = 'active'
           ORDER BY recorded_at DESC""",
        (patient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _get_recent_notes(db, patient_id: str, cutoff_iso: str) -> list[dict]:
    rows = db.execute(
        # Compare via datetime() so the stored 'YYYY-MM-DD HH:MM:SS' created_at and
        # an ISO-8601 cutoff ('...T...+00:00') are normalized to the same form —
        # a raw string compare would always fail (space 0x20 < 'T' 0x54 at pos 10).
        """SELECT id, note_format, structured_note, raw_transcript, status, created_at
           FROM scribe_notes
           WHERE patient_id = ? AND datetime(created_at) >= datetime(?)
           ORDER BY created_at DESC LIMIT 5""",
        (patient_id, cutoff_iso),
    ).fetchall()
    return [dict(r) for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
#  THE AI-DRAFTED SUPPLEMENT — Sage's reasoned assessment, REUSED
# ═════════════════════════════════════════════════════════════════════════════
#
# THE REUSE RULE, stated once so the handover, the ward round and the tests
# cannot drift into three different rules:
#
#   1. NEVER GENERATE. This module issues no model call. It carries the draft
#      Sage already wrote when Vigil paged. Re-deriving it here would be both
#      slower (42-57 s measured) and INCONSISTENT with what the outgoing shift
#      was actually shown — and "the machine said something different to the
#      night nurse" is exactly the disagreement a handover exists to prevent.
#   2. READABLE STATES ONLY: `ready` (nobody has reviewed it yet) and `approved`
#      (a clinician read it and agreed). A `declined` draft is EXCLUDED — a
#      clinician has already rejected that reasoning, and reprinting it on a
#      medico-legal sheet would re-assert it over their judgement. `generating`
#      and `unavailable` have no text to carry at all.
#   2b. AND A DECLINE VETOES WHAT IS UNDER IT. If the most recent verdict on
#      this patient is a decline, the surface carries NOTHING — it does not
#      quietly fall back to an older draft the clinician has effectively
#      superseded. That is the same rule `sage_assessment_service._select_cards`
#      already applies to a blocked medication ("A BLOCKED SLOT IS NOT
#      REFILLED"): the moment an automatic second choice looks most helpful is
#      the moment it is least defensible. `unavailable` and `generating` do NOT
#      veto — they are Sage failing to write, not a human saying no.
#   3. IT MUST ACTUALLY SAY SOMETHING. A row whose `whats_happening` is empty is
#      not carried. Nothing is ever invented to fill a heading.
#   4. THE STALENESS BOUND is the handover's own look-back window (12h by
#      default, and the round uses the same default). Tied to the window rather
#      than to a second independent constant so the supplement can never
#      describe a deterioration from OUTSIDE the period the rest of the sheet
#      claims to cover — a sheet that says "last 12h" at the top and then quotes
#      a 30-hour-old assessment is lying in its own header.
#   5. NEWEST QUALIFYING DRAFT ONLY — never a merge of several. Two machine
#      assessments of one patient on one sheet leaves the reader with no way to
#      tell which is current; that is the same failure the unique index on
#      `sage_assessments.message_id` prevents at the alert.
#
# AND THE SNAPSHOT RULE. Everything else on this sheet is current. This section
# is not: it was written at a moment, against the NEWS2 the engine computed at
# that moment. So it is stamped with that score (read from the row's
# engine-written column — never recomputed, never taken from model prose) and
# with how long ago it was written. A supplement that reads as current is worse
# than no supplement.

#: The heading, and the sentence under it. CONSTANTS because the labelling is
#: the safety property: `plain_text` is the print/copy source of truth for a
#: medico-legal document, and a reader must never be able to mistake this
#: section for something a clinician wrote. Both forms carry the same words.
AI_SUPPLEMENT_HEADING = "SUPPLEMENT: AI-DRAFTED ASSESSMENT (NOT WRITTEN BY A CLINICIAN)"
AI_DRAFTED_LABEL = "AI-drafted by Orb Sage, not written by a clinician"
AI_SUPPLEMENT_DISCLAIMER = (
    "Nothing in this section has been ordered, charted or administered. "
    "It is machine-generated decision support and carries no clinical authority.")
AI_SUPPLEMENT_END = "-- end of AI-drafted supplement --"

def _get_reusable_assessment_rows(db, patient_id: str, states, depth: str,
                                  limit: int = 5) -> list[dict]:
    """Candidate assessments, newest first. Age is filtered in PYTHON, not SQL.

    `sage_assessments.created_at` is a Python `isoformat()` string carrying a
    UTC offset and microseconds. SQLite's `datetime()` returns NULL on anything
    it cannot parse, and a NULL comparison silently excludes the row — the
    supplement would just never appear, with no error to notice. Parsing in
    Python makes an unreadable timestamp a visible skip instead.

    `states` includes `declined` so the veto in rule 2b can SEE a decline. It is
    never carried; it stops the walk.

    THE DEPTH FILTER IS LOAD-BEARING (migration 30). The alert lane now writes
    `depth='brief'` — Tier 2, at most three next steps and deliberately NO prose
    interpretation, because Tier 1 carries that deterministically. Carrying a
    brief draft under a heading that says "assessment" would put three bullet
    points on a medico-legal sheet dressed as reasoning that was never written.
    Only `full` is the four-section artefact this supplement claims to be, and
    the decline veto is scoped the same way: declining the alert's next steps is
    not a verdict on a reasoned assessment nobody has read.
    """
    placeholders = ",".join("?" for _ in states)
    rows = db.execute(
        f"""SELECT * FROM sage_assessments
             WHERE patient_id = ? AND depth = ? AND state IN ({placeholders})
             ORDER BY created_at DESC LIMIT ?""",
        (patient_id, depth, *states, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _full_draft_in_flight(patient_id: str, depth: str, stale_after_seconds: float) -> bool:
    """Is a full assessment already being written for this patient?

    THE DEDUPE. Unlike the alert lane, a handover generation carries no
    `message_id`, so the unique index that stops two drafts per alert does not
    apply here — nothing but this check stops a ward round tapping twice from
    starting two 46-second generations on the same patient.

    A row past the stale window does NOT count: the process may have died
    mid-generation, and `_derive_state` already reports that row as abandoned.
    Treating it as in-flight would mean a killed appliance could never prepare
    that patient again.
    """
    with db_session() as db:
        row = db.execute(
            """SELECT created_at FROM sage_assessments
                WHERE patient_id = ? AND depth = ? AND state = 'generating'
                ORDER BY created_at DESC LIMIT 1""",
            (patient_id, depth),
        ).fetchone()
    if not row:
        return False
    age = _age_seconds(row["created_at"], _now())
    return age is not None and age <= stale_after_seconds


def _get_endorsement(db, assessment_id: str) -> dict | None:
    """WHO agreed with the machine, if anyone. The single most important line in
    this section: "an AI draft a named doctor endorsed" and "an AI draft nobody
    has read" are different documents."""
    row = db.execute(
        """SELECT actor_name, actor_role, created_at FROM sage_assessment_events
            WHERE assessment_id = ? AND new_state = 'approved'
            ORDER BY created_at DESC LIMIT 1""",
        (assessment_id,),
    ).fetchone()
    return dict(row) if row else None


def _age_seconds(created_at, now: datetime) -> float | None:
    if not created_at:
        return None
    try:
        stamp = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (now - stamp).total_seconds())


def _age_minutes(created_at, now: datetime) -> int | None:
    seconds = _age_seconds(created_at, now)
    return None if seconds is None else int(seconds // 60)


def get_reasoned_assessment(patient_id: str,
                            max_age_hours: int = HANDOFF_WINDOW_HOURS) -> dict | None:
    """Sage's full reasoned assessment for this patient, or None.

    REUSE ONLY — see the rule above this function. Returns None whenever there
    is nothing honest to show, and the callers render NOTHING in that case
    rather than an empty heading.

    FAILS OPEN. The supplement is an extra on a deterministic sheet: if the
    lookup raises for any reason (a table that a migration has not reached yet,
    a shape change upstream), the handover is produced without it. Losing the
    whole shift handover because a machine-authored extra could not be read is
    not a trade worth making.
    """
    try:
        from services import sage_assessment_service as sas
        carriable = (sas.STATE_READY, sas.STATE_APPROVED)
        # `declined` is fetched but never carried — see rule 2b.
        considered = carriable + (sas.STATE_DECLINED,)
        depth = sas.DEPTH_FULL
    except Exception:
        logger.warning("[I-PASS] Sage assessment service unavailable", exc_info=True)
        return None

    now = _now()
    bound_minutes = max(1, int(max_age_hours)) * 60
    try:
        with db_session() as db:
            rows = _get_reusable_assessment_rows(db, patient_id, considered, depth)
            chosen = None
            age = None
            for row in rows:
                candidate_age = _age_minutes(row.get("created_at"), now)
                if candidate_age is None or candidate_age > bound_minutes:
                    continue
                if row.get("state") == sas.STATE_DECLINED:
                    # THE VETO (rule 2b). The most recent verdict on this
                    # patient is a clinician saying no. Stop — do not reach past
                    # it for an older draft they have effectively superseded.
                    logger.info("[I-PASS] %s: newest Sage assessment was declined "
                                "— no supplement carried", patient_id)
                    return None
                if not (row.get("summary") or "").strip():
                    # Nothing to carry. Rule 3: never invent prose for a heading.
                    continue
                chosen, age = row, candidate_age
                break
            if not chosen:
                return None
            endorsement = _get_endorsement(db, chosen["id"]) if (
                chosen.get("state") == sas.STATE_APPROVED) else None
        payload = sas.render(chosen)
    except Exception:
        logger.warning("[I-PASS] could not read a Sage assessment for %s",
                       patient_id, exc_info=True)
        return None

    # `render` re-derives state on read (a row abandoned mid-generation reports
    # `unavailable`). Re-check it: the SQL state is what the row SAYS, this is
    # what a reader should BELIEVE, and only the second one may be printed.
    state = payload.get("state")
    if state not in carriable or not (payload.get("whats_happening") or "").strip():
        return None

    news2 = payload.get("news2")
    # The news2_text clauses (single parameter 3, Scale 2, incomplete set) come
    # off the row's Tier 1 block, and only when Tier 1 scored this same number:
    # the row's `news2_level` may carry a CfC escalation Tier 1's band does not.
    tier1 = payload.get("tier1") or {}
    quals = news2_qualifiers(tier1) if tier1.get("news2") == news2 else []
    news2_context = (
        f"NEWS2 {news2} ({_level_phrase(payload.get('news2_level'))})"
        + ("".join(f" · {q}" for q in quals) + "," if quals else "")
        + " as scored when this was written"
        if news2 is not None else
        "Orb had no NEWS2 on file when this was written")

    endorsed_by = None
    if endorsement:
        name = (endorsement.get("actor_name") or "").strip()
        role = humanise_enum(endorsement.get("actor_role"), "").strip()
        if name:
            endorsed_by = f"{name} ({role})" if role else name

    evidence = [e for e in (payload.get("evidence") or []) if e.get("title")]

    return {
        # ── The labelling. Present, unmissable, and machine-checkable. ────────
        "available": True,
        "ai_drafted": True,
        "authored_by": "Orb Sage (artificial intelligence)",
        "label": AI_DRAFTED_LABEL,
        "heading": AI_SUPPLEMENT_HEADING,
        "disclaimer": AI_SUPPLEMENT_DISCLAIMER,
        # ── Provenance and the snapshot stamp ────────────────────────────────
        "assessment_id": payload.get("id"),
        "reused": True,
        # WHY IT EXISTS, read off the row rather than assumed. A draft carrying
        # a `message_id` was written because Vigil paged; one without was
        # written because a surface like this one asked for it (see
        # `prepare_reasoned_assessment`, which passes `message_id=None`).
        # Printing "when Vigil paged" over an assessment nobody was paged about
        # would be a false statement about a clinical event, on a medico-legal
        # sheet, which is exactly the class of thing this section must not do.
        "reuse_basis": ("drafted_for_vigil_alert" if payload.get("message_id")
                        else "drafted_for_review"),
        "reuse_basis_display": ("when Vigil paged for this patient"
                                if payload.get("message_id")
                                else "when this patient was prepared for review"),
        "drafted_at": payload.get("created_at"),
        "drafted_at_display": _stamp_phrase(payload.get("created_at")),
        "age_minutes": age,
        "age_display": _ago_phrase(age),
        "stale_after_hours": int(max_age_hours),
        # Raw enums stay on the wire for the UI; the *_display twins are what any
        # renderer prints. Same contract as `trend` / `trend_display` above.
        "state": state,
        "state_display": _assessment_state_phrase(state),
        "endorsed_by": endorsed_by,
        "endorsed_at": (endorsement or {}).get("created_at"),
        "news2_at_drafting": news2,
        "news2_level_at_drafting": payload.get("news2_level"),
        "news2_context": news2_context,
        # ── The four sections themselves ─────────────────────────────────────
        "whats_happening": payload.get("whats_happening"),
        "considerations": payload.get("considerations") or [],
        "watch_items": payload.get("watch_items") or [],
        "evidence": evidence,
        "citation_basis": payload.get("citation_basis"),
        "citation_display": _citation_phrase(payload.get("citation_basis")),
        "confidence": payload.get("confidence"),
        "confidence_display": _confidence_phrase(payload.get("confidence")),
        "model": payload.get("model"),
    }


def _render_assessment_lines(block: dict, indent: str = "   ") -> list[str]:
    """The supplement as printed text. Called by the handover sheet AND returned
    on its own for the ward round, so the two can never word it differently.

    EVERY Orb-authored value here goes through a phrase helper. The model's own
    prose is carried verbatim (it was sanitized and NEWS2-pinned before it was
    stored, and rewriting a clinician-facing clinical sentence at print time
    would be worse than printing it), but no enum, id or `None` of Orb's ever
    reaches the page.
    """
    if not block:
        return []

    lines: list[str] = [AI_SUPPLEMENT_HEADING]

    author = f"{indent}Author: {block['authored_by']}."
    if block.get("model"):
        author = f"{indent}Author: {block['authored_by']}, model {block['model']}."
    lines.append(author)
    stamp = block.get("drafted_at_display")
    written = f"Written {block['age_display']}"
    if stamp:
        written += f" ({stamp})"
    # "for this handover" would be a lie on the ward round, which is served
    # these same lines standalone.
    lines.append(f"{indent}{written}, {block['reuse_basis_display']}. "
                 f"Reproduced here exactly as it was written; it has not been "
                 f"re-run.")
    # NOT "see the illness-severity line above": these same lines are served
    # standalone to the ward round, where there is no line above. A
    # cross-reference that is only true on one of the two surfaces is a false
    # statement on the other.
    lines.append(f"{indent}It describes that moment, not the patient's state now: "
                 f"{block['news2_context']}. Check Orb's current score before "
                 f"acting on anything here.")
    if block.get("endorsed_by"):
        lines.append(f"{indent}Reviewed and endorsed by {block['endorsed_by']}.")
    else:
        lines.append(f"{indent}Review status: {block['state_display']}.")
    lines.append(f"{indent}{block['disclaimer']}")

    lines.append("")
    lines.append(f"{indent}What Sage said was happening:")
    lines.append(f"{indent}  {block['whats_happening']}")

    considerations = block.get("considerations") or []
    if considerations:
        lines.append("")
        lines.append(f"{indent}Steps Sage suggested (suggestions only, none of them done):")
        for n, item in enumerate(considerations, start=1):
            step = (item.get("step") or "").strip()
            if not step:
                continue
            why = (item.get("why") or "").strip()
            lines.append(f"{indent}  {n}. {step}" + (f", {why}" if why else ""))
            if item.get("orb_can_act"):
                lines.append(f"{indent}     Orb has a one-click card ready for this; "
                             f"it does nothing until a clinician approves it.")

    watch_items = block.get("watch_items") or []
    if watch_items:
        lines.append("")
        lines.append(f"{indent}What Sage could not see. Establish these at the bedside:")
        for item in watch_items:
            text = str(item or "").strip()
            if text:
                lines.append(f"{indent}  - {text}")

    lines.append("")
    lines.append(f"{indent}Evidence: {block['citation_display']}. "
                 f"Sage's own confidence: {block['confidence_display']}.")
    for source in (block.get("evidence") or []):
        title = str(source.get("title") or "").strip()
        origin = str(source.get("source") or "").strip()
        lines.append(f"{indent}  - {title}" + (f" ({origin})" if origin else ""))

    lines.append(f"{indent}{AI_SUPPLEMENT_END}")
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# PREPARING ONE AHEAD OF TIME — the "nobody is waiting" half
# ─────────────────────────────────────────────────────────────────────────────
# Migration 30 split the alert lane down to Tier 1 + Tier 2, so the alert no
# longer produces a `full` assessment at all. Reuse alone would therefore give
# an empty supplement on almost every sheet. `sage_assessment_service` left the
# seam for the other half — ``generate_full_assessment`` — and said in as many
# words that deciding WHEN to ask belongs to this module. This is that decision.
#
# THE RULE: ASK EARLY, READ LATER. `build_ipass` never generates and never
# waits — it is a deterministic sheet and stays one. Generation is requested
# through `prepare_reasoned_assessment` at a moment that is structurally NOT the
# moment of reading:
#
#   * the shift handover is COMPOSED AHEAD of the change-over — the outgoing
#     nurse opens the ward's handover list minutes before they hand over;
#   * the ward round is WALKED PATIENT BY PATIENT — the round can ask for the
#     next patient while the clinician is still standing at this one.
#
# Both spend the 46 seconds against something a human is already doing. If the
# call comes late and the draft is not ready, the sheet simply says nothing —
# it NEVER blocks, and there is no spinner promising an assessment that is
# still 40 seconds out.

#: Scheduled preparations, held so the loop's WEAK task reference cannot let a
#: generation be collected mid-flight. Bounded for the same reason
#: `sage_assessment_service._inflight` is: a "prepare the whole ward" client
#: must not turn this into an unbounded coroutine factory. Each generation is
#: ~46 s on a serialized background lane, so this ceiling is already ~25
#: minutes of queued GPU — the number that matters is the queue's, not ours.
_PREPARE_INFLIGHT: set = set()
_MAX_PREPARE_INFLIGHT = 32

#: What this surface WANTS its shadow lane to be called, and what it falls back
#: to while that value is not one the instrument accepts. See `_shadow_safe_lane`.
PREPARE_LANE = "handover"
PREPARE_LANE_FALLBACK = "manual"


def _shadow_safe_lane(lane: str) -> str:
    """A lane the shadow instrument will actually accept.

    MEASURED, not assumed, on 2026-08-14: ``shadow_service.LANES`` is
    {simulator, ingestion, chat, voice, manual, sweeper} and ``shadow_events``
    carries the same set as a CHECK constraint. ``open_episode(pid,
    "handover")`` therefore returns a NON-NULL episode id with `opened=True`
    while writing ZERO rows — `build` rejects the lane, `emit_many` drops the
    event, and the whole path fails open by design.

    That has two consequences, and the second is the serious one:

      1. Every assessment prepared from the handover or the round would be
         INVISIBLE to the shadow log — the pilot's only evidence instrument —
         while looking instrumented from the inside.
      2. `open_episode` still MINTS the id and caches it in its process-local
         registry, so the next real Vigil alert for that patient resolves onto
         an episode whose `episode.opened` row was never written. Genuine alert
         events would hang off a phantom episode.

    So the lane is checked against the instrument's own vocabulary before it is
    used. `manual` is the fallback because, among the six permitted values, it
    is the only one that means "a human asked for this by hand" rather than a
    feed or a timer — and the `sage.drafted` payload's `query_class` already
    distinguishes an assessment from a manually charted observation.

    Written as a LOOKUP rather than a constant so this self-heals: the day
    'handover' is added to LANES and the CHECK, this starts using it with no
    change here. Until then it degrades loudly instead of silently.
    """
    try:
        from services.shadow_service import LANES
    except Exception:                                       # pragma: no cover
        return PREPARE_LANE_FALLBACK
    if lane in LANES:
        return lane
    logger.warning("[I-PASS] shadow lane %r is not in the instrument's vocabulary "
                   "— recording this assessment under %r instead",
                   lane, PREPARE_LANE_FALLBACK)
    return PREPARE_LANE_FALLBACK if PREPARE_LANE_FALLBACK in LANES else next(iter(LANES))


async def prepare_reasoned_assessment(patient_id: str,
                                      max_age_hours: int = HANDOFF_WINDOW_HOURS,
                                      *, lane: str = PREPARE_LANE) -> dict:
    """Reuse a full assessment, or start one — for a surface that is early.

    RETURNS IMMEDIATELY. It never awaits the generation; the caller gets
    `preparing` and reads the result later through the ordinary reuse path.

    * `ready`      — one already exists inside the staleness bound. REUSE IS
                     PREFERRED and is checked first: a fresh generation would be
                     slower AND could disagree with what the ward was shown.
    * `preparing`  — one is being written (already in flight, or just started).
    * `unavailable`— nothing exists and nothing could be started, with a reason.

    ON RE-ASKING AFTER A DECLINE. The rule-2b veto stops the SHEET from quietly
    substituting an older draft for one a clinician rejected. It does not stop a
    clinician who explicitly asks for a fresh assessment from getting one —
    that is a human requesting a second opinion, which is the opposite of the
    machine choosing its own answer. So a decline suppresses the supplement and
    still allows a prepare.
    """
    block = await run_db(get_reasoned_assessment, patient_id, max_age_hours)
    if block:
        return {"status": "ready", "assessment": block,
                "stale_after_hours": int(max_age_hours),
                "reason": None, "message": None}

    try:
        from services import sage_assessment_service as sas
    except Exception:
        logger.warning("[I-PASS] Sage assessment service unavailable", exc_info=True)
        return _preparing_failure("service_unavailable", max_age_hours)

    # `_enabled` and `_stale_generating_seconds` are that module's private config
    # readers, used rather than restated. They resolve env vars PER CALL, and a
    # second copy of "ORB_SAGE_ASSESSMENT_ENABLED" over here is a switch an
    # operator could flip and have only half the system obey.
    if not sas._enabled():
        return _preparing_failure("disabled", max_age_hours)

    in_flight = await run_db(_full_draft_in_flight, patient_id, sas.DEPTH_FULL,
                             sas._stale_generating_seconds())
    if in_flight:
        return _preparing(max_age_hours)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:                                   # pragma: no cover
        return _preparing_failure("no_event_loop", max_age_hours)
    if len(_PREPARE_INFLIGHT) >= _MAX_PREPARE_INFLIGHT:
        logger.warning("[I-PASS] backpressure — no assessment prepared for %s", patient_id)
        return _preparing_failure("busy", max_age_hours)

    # The lane is passed EXPLICITLY rather than left to the callee's default,
    # which is `handover` — a value the shadow instrument rejects. See
    # `_shadow_safe_lane` for the phantom episode that would otherwise leave
    # behind.
    safe_lane = _shadow_safe_lane(lane)
    try:
        task = loop.create_task(
            sas.generate_full_assessment(patient_id, lane=safe_lane))
    except Exception:
        logger.warning("[I-PASS] could not start an assessment for %s",
                       patient_id, exc_info=True)
        return _preparing_failure("could_not_start", max_age_hours)
    _PREPARE_INFLIGHT.add(task)
    task.add_done_callback(_PREPARE_INFLIGHT.discard)
    logger.info("[I-PASS] preparing a full Sage assessment for %s (lane=%s)",
                patient_id, safe_lane)
    return _preparing(max_age_hours)


#: Ward-facing wording for every `prepare` outcome, kept HERE rather than in the
#: client for the same reason `sage_assessment_service.UNAVAILABLE_MESSAGES` is:
#: two surfaces must not invent two different stories about one fact.
PREPARE_MESSAGES: dict[str, str] = {
    "preparing": ("Orb Sage is writing a reasoned assessment for this patient. "
                  "It takes about a minute; reopen this sheet to see it."),
    "disabled": "Sage assessments are switched off on this appliance.",
    "service_unavailable": "Orb could not reach Sage, so no assessment was started.",
    "busy": ("Sage is already writing as many assessments as this appliance will "
             "run at once. Nothing was started for this patient."),
    "could_not_start": "Orb could not start an assessment for this patient.",
    "no_event_loop": "Orb could not start an assessment for this patient.",
}


def _preparing(max_age_hours) -> dict:
    return {"status": "preparing", "assessment": None,
            "stale_after_hours": int(max_age_hours),
            "reason": None, "message": PREPARE_MESSAGES["preparing"]}


def _preparing_failure(reason: str, max_age_hours) -> dict:
    return {"status": "unavailable", "assessment": None,
            "stale_after_hours": int(max_age_hours),
            "reason": reason,
            "message": PREPARE_MESSAGES.get(
                reason, "Orb could not start an assessment for this patient.")}


def render_assessment_text(block: dict | None) -> str:
    """The supplement as a standalone printable block — the ward round's copy of
    exactly what the handover sheet prints. Empty string when there is nothing
    to carry, so a caller that prints it unconditionally still prints nothing."""
    return "\n".join(_render_assessment_lines(block))


# ─────────────────────────────────────────────────────────────────────────────
# Section builders
# ─────────────────────────────────────────────────────────────────────────────
def _build_illness_severity(risk: dict | None) -> dict:
    if risk is None:
        # Scoring switched off: no severity is drafted. Every key stays on the
        # wire, empty, so the screen tests one value and prints no line.
        return {"severity": None, "news2": None, "news2_level": None,
                "scoring_model": None, "spo2_scale": None, "drivers": [],
                "text": None, "scoring_enabled": False}
    level = str(risk.get("level", "stable")).lower()
    severity = _SEVERITY_MAP.get(level, "Watcher")
    raw_news2 = risk.get("raw_news2", risk.get("raw_mews"))
    drivers = risk.get("drivers", []) or []
    news2_text = f"NEWS2 {raw_news2}" if raw_news2 is not None else "NEWS2 unavailable"
    # Scale 2 is named on the line (2026-10-02): "NEWS2 0" beside a COPD
    # patient's SpO2 90 reads as a scoring error unless the sheet says 90 IS
    # this patient's target. Scale 1 (or no scale on the dict) is unchanged.
    spo2_scale = risk.get("spo2_scale")
    scale_text = ", SpO2 Scale 2, target 88-92%" if spo2_scale == 2 else ""
    # The rest of the news2_text clauses (2026-10-03): a single parameter
    # scoring 3, and an incomplete set whose aggregate is only a minimum. The
    # scale stays in this line's own words above, with its target.
    quals = news2_qualifiers(risk, scale=False) if raw_news2 is not None else []
    text = (
        f"{severity}: {news2_text} ({_level_phrase(level)}{scale_text})"
        f"{''.join(f' · {q}' for q in quals)}. "
        f"Key drivers: {', '.join(drivers) if drivers else 'none noted'}."
    )
    return {
        "severity": severity,
        "news2": raw_news2,
        # Raw enum kept on the wire for the UI; only `text` is humanised.
        "news2_level": level,
        "scoring_model": risk.get("scoring_model", "NEWS2"),
        "spo2_scale": spo2_scale,
        "drivers": list(drivers),
        "text": text,
    }


def _build_patient_summary(patient: dict, problems: list[dict], allergies: list[dict]) -> dict:
    age = _compute_age(patient)
    problem_labels = [p["problem"] for p in problems if p.get("problem")]
    allergy_labels = [
        (f"{a['substance']} ({a['reaction']})" if a.get("reaction") else a["substance"])
        for a in allergies if a.get("substance")
    ]
    demo = f"{age if age is not None else '?'}y {patient.get('sex') or ''}".strip()
    # Never "NKDA" from an empty list. NKDA is a clinician's assertion that the
    # patient was asked; an empty patient_allergies is also every patient nobody
    # asked, and Orb stores no NKDA assertion to tell the two apart.
    text = (
        f"{patient.get('name', 'Unknown')}: {demo}, "
        f"{patient.get('ward') or 'ward ?'} bed {patient.get('bed') or '?'}. "
        f"Primary diagnosis: {patient.get('diagnosis') or 'not documented'}. "
        f"Active problems: {', '.join(problem_labels) if problem_labels else 'none recorded'}. "
        + (f"Allergies: {', '.join(allergy_labels)}." if allergy_labels
           else f"Allergies: {NO_ALLERGIES_RECORDED}")
    )
    return {
        "name": patient.get("name"),
        "mrn": patient.get("mrn"),
        "age": age,
        "sex": patient.get("sex"),
        "ward": patient.get("ward"),
        "bed": patient.get("bed"),
        "diagnosis": patient.get("diagnosis"),
        "problems": problem_labels,
        # Empty means none recorded; the UI words that state itself.
        "allergies": allergy_labels,
        "text": text,
    }


#: How an AI-extracted order nobody has verified is named on the handover,
#: the same words as the printed medication list and downtime sheet.
UNVERIFIED_MED_LABEL = "AI-extracted, awaiting pharmacist verification"
_UNVERIFIED_SOURCES = ("llm_extracted", "agentic")


def _build_action_list(meds: list[dict], bundles: list[dict], risk: dict | None) -> dict:
    active_meds = [
        {
            "drug_name": m.get("drug_name"),
            "dosage": m.get("dosage"),
            "route": m.get("route"),
            "frequency": m.get("frequency"),
            # Handed over like a prescription it is not, unless it says so.
            "unverified": m.get("source") in _UNVERIFIED_SOURCES and not m.get("verified_by"),
        }
        for m in meds
    ]

    open_bundles = []
    outstanding_items: list[str] = []
    for b in bundles:
        overdue = [e["label"] for e in b.get("elements", []) if e.get("state") == "overdue"]
        pending = [e["label"] for e in b.get("elements", []) if e.get("state") == "pending"]
        # `bundle_type` is the machine key ('sepsis6'); `bundle_label` is what the
        # printed sheet says. Both travel, so nothing downstream has to parse prose.
        label_for_bundle = _bundle_label(b.get("bundle_type"))
        open_bundles.append({
            "bundle_type": b.get("bundle_type"),
            "bundle_label": label_for_bundle,
            "compliance_pct": b.get("compliance_pct"),
            "completion_pct": b.get("completion_pct"),
            "outstanding": overdue + pending,
        })
        for label in overdue:
            outstanding_items.append(f"OVERDUE ({label_for_bundle}): {label}")
        for label in pending:
            outstanding_items.append(f"Pending ({label_for_bundle}): {label}")

    # Severity-driven monitoring action so the list is never empty for a sick
    # patient. None while scoring is switched off: no line from a score.
    level = str(risk.get("level", "stable")).lower() if risk is not None else None
    if level == "critical":
        outstanding_items.append("Repeat a full set of observations now and keep continuous monitoring.")
    elif level == "elevated":
        outstanding_items.append("Recheck observations within the hour and review response to treatment.")
    if not active_meds and not open_bundles and not outstanding_items:
        outstanding_items.append("Continue routine observations; no outstanding actions handed over.")

    return {
        "active_medications": active_meds,
        "open_care_bundles": open_bundles,
        "outstanding_items": outstanding_items,
    }


# ── CONTINGENCY TRIGGERS ARE THE RCP CHART'S 3-POINT THRESHOLDS (ruled 2026-10-03) ──
# The lines used to be picked by keyword-matching the driver text and carried
# hand-written cutoffs. Run through the real scorer on 2026-10-03:
#   SBP 90 (3 pts, red on the Vigil bar) -> "If systolic BP < 90 mmHg ..."
#   SBP 225 (hypertension)               -> the hypotension fluid-bolus line
#   HR 38, RR 8 (3 pts, low side)        -> the HIGH-side line ("exceeds 130",
#                                           "rises above 24")
#   Temp 35.0 (3 pts) -> no line at all; Temp 35.8 -> "If febrile ..."
# The keyword lost the direction ("(hr" matched bradycardia, "sbp" matched
# hypertension), and the drivers list is cut to three (the CfC overlay can take
# a slot), so a vital that scored could lose its line.
#
# Now: one line per vital that scored, read off its breakdown row whatever the
# drivers kept, for the side of normal its charted value sits on. The number is
# the first charted value the RCP chart scores 3 on that side, written the way
# the chart prints it (<= / >=), so the sheet and the bar call the same reading
# red. The four directions that had no line of their own give the RCP
# red-score response and invent no clinical action. Temperature has no 3-point
# band above normal, so the fever line names no number. SpO2 Scale 2 keeps its
# own target-range lines (ruled 2026-10-02). test_ipass_news2_contingency sweeps
# compute_news2 and fails if a number here stops being where the 3 points start.
_URGENT_REVIEW = "inform the covering doctor for urgent review."

#: Lowest and highest charted value that scores 0, per breakdown parameter.
#: Only tells which side of normal a scoring value is on; the sweep test checks
#: it against the scorer.
_ZERO_BAND = {
    "SpO2": (96, 100),
    "Systolic BP": (111, 219),
    "Heart rate": (51, 90),
    "Temperature": (36.1, 38.0),
    "Respiratory rate": (12, 20),
}

#: (breakdown parameter, side of normal) -> the handover line.
_VITAL_LINES = {
    ("SpO2", "low"): ("If SpO2 is 91% or below (NEWS2 3 points) or oxygen requirement "
                      "rises, titrate O2 to target and call the covering doctor."),
    ("Systolic BP", "low"): ("If systolic BP is 90 mmHg or below (NEWS2 3 points), give a "
                             "fluid bolus per protocol and escalate to the registrar."),
    ("Systolic BP", "high"): f"If systolic BP is 220 mmHg or above (NEWS2 3 points), {_URGENT_REVIEW}",
    ("Heart rate", "low"): f"If heart rate is 40 or below (NEWS2 3 points), {_URGENT_REVIEW}",
    ("Heart rate", "high"): ("If heart rate is 131 or above (NEWS2 3 points) or a new "
                             "arrhythmia appears, obtain a 12-lead ECG and review."),
    ("Temperature", "low"): f"If temperature is 35.0°C or below (NEWS2 3 points), {_URGENT_REVIEW}",
    ("Temperature", "high"): ("If febrile with a rising NEWS2, start the Sepsis Six bundle "
                              "and send blood cultures + lactate."),
    ("Respiratory rate", "low"): f"If respiratory rate is 8 or below (NEWS2 3 points), {_URGENT_REVIEW}",
    ("Respiratory rate", "high"): ("If respiratory rate is 25 or above (NEWS2 3 points) or work "
                                   "of breathing worsens, reassess the airway and escalate."),
}
_ACVPU_LINE = ("If new confusion or a drop in ACVPU, check glucose, do a neuro assessment "
               "and escalate urgently.")


def _vital_line(row: dict | None) -> str | None:
    """The line for one scoring breakdown row, or None if it did not score."""
    if not row or row.get("value") is None:
        return None
    parameter = row["parameter"]
    side = "low" if row["value"] < _ZERO_BAND[parameter][0] else "high"
    return _VITAL_LINES[(parameter, side)]


def _contingencies(risk: dict) -> list[str]:
    """'If X then Y' contingency planning, one line per vital that scored.

    Read from the NEWS2 breakdown, not the drivers; see the note above. A risk
    dict with no breakdown (nothing charted) gets the level line only.
    """
    level = str(risk.get("level", "stable")).lower()
    scored = {b.get("parameter"): b for b in (risk.get("breakdown") or [])
              if (b.get("points") or 0) > 0}
    out: list[str] = []

    # Oxygenation first: the saturation, or the oxygen it takes to hold it.
    spo2_row = scored.get("SpO2") or scored.get("SpO2 (Scale 2)")
    if spo2_row or "Supplemental O2" in scored:
        # Scale 2 (2026-10-02): the Scale 1 "below 92%" line told the nurse to
        # escalate an on-target COPD reading and never mentioned over-oxygenation,
        # the harm Scale 2 exists to catch. A dict with no scale is Scale 1.
        if risk.get("spo2_scale") == 2:
            # Scale 2 scores above its 88-92% target only on oxygen, so a
            # scoring value above 92 is over-target, read off the row itself
            # (a 1-point over-target SpO2 is often cut from the drivers).
            if spo2_row and spo2_row.get("value") is not None and spo2_row["value"] > 92:
                out.append("If SpO2 stays above this patient's 88-92% target on oxygen (SpO2 Scale 2), "
                           "reduce O2 to target and call the covering doctor.")
            out.append("If SpO2 falls below 88% or oxygen requirement rises, titrate O2 to this "
                       "patient's 88-92% target (SpO2 Scale 2) and call the covering doctor.")
        else:
            out.append(_VITAL_LINES[("SpO2", "low")])
    for parameter in ("Systolic BP", "Heart rate", "Temperature"):
        line = _vital_line(scored.get(parameter))
        if line:
            out.append(line)
    if "Consciousness (ACVPU)" in scored:
        out.append(_ACVPU_LINE)
    line = _vital_line(scored.get("Respiratory rate"))
    if line:
        out.append(line)

    if not out:
        if level == "critical":
            out.append("If any vital sign deteriorates further, escalate immediately to the medical emergency team.")
        elif level == "elevated":
            out.append("If NEWS2 rises, increase monitoring frequency and inform the covering doctor.")
        else:
            out.append("If new abnormal vitals develop, repeat a full set of observations and reassess.")
    return out


def _build_situation_awareness(vitals: list[dict], trend: dict, risk: dict | None) -> dict:
    latest = vitals[-1] if vitals else {}
    trend_label = trend.get("trend", "insufficient_data")
    level = str(risk.get("level", "stable")).lower() if risk is not None else None
    # Scoring switched off (risk None): no "ACTIVE DETERIORATION" line either,
    # not even from the trend. It is a warning Orb works out, and with scoring
    # off Orb raises none.
    active_deterioration = risk is not None and (
        level in ("critical", "elevated") or trend_label == "deteriorating")

    latest_vitals = {
        "timestamp": latest.get("timestamp"),
        "heart_rate": latest.get("heart_rate"),
        "systolic_bp": latest.get("systolic_bp"),
        "diastolic_bp": latest.get("diastolic_bp"),
        "spo2": latest.get("spo2"),
        "temperature": latest.get("temperature"),
        "respiratory_rate": latest.get("respiratory_rate"),
        "consciousness": latest.get("consciousness"),
    }

    trend_bits = []
    for key, label in (("hr_delta", "HR"), ("sbp_delta", "SBP"), ("spo2_delta", "SpO2")):
        if trend.get(key) is not None:
            trend_bits.append(f"{label} {trend[key]:+.1f}")
    if trend_bits:
        trend_detail = ", ".join(trend_bits)
    else:
        # `get_vitals_trend` states WHY it could not compare ("no earlier vitals
        # to compare against"). That reason is the useful half of the sentence —
        # it tells the oncoming nurse this is a newly-arrived patient rather than
        # a broken feed — so it is printed instead of a second generic phrase.
        trend_detail = str(trend.get("reason") or "").strip() or "insufficient recent data"

    # No NEWS2 contingency lines while scoring is switched off: each one is
    # read off a score's breakdown.
    contingencies = _contingencies(risk) if risk is not None else []

    vit_text = (
        f"HR {_num(latest.get('heart_rate'))}, "
        f"BP {_num(latest.get('systolic_bp'))}/{_num(latest.get('diastolic_bp'))}, "
        f"SpO2 {_num(latest.get('spo2'))}%, "
        f"RR {_num(latest.get('respiratory_rate'))}, "
        f"Temp {_num(latest.get('temperature'), 1)}C"
    )
    trend_text = _trend_phrase(trend_label)
    # No "Readings analysed: N." The count is an input to the trend, not a fact
    # about the patient, and it stays on the wire as `readings_count`.
    text = (
        f"Latest vitals: {vit_text}. Trend over the shift: {trend_text} ({trend_detail})."
        f"{' ACTIVE DETERIORATION: watch closely.' if active_deterioration else ''}"
    )
    return {
        # Raw enum on the wire (the UI humanises it itself); prose in `text`.
        "trend": trend_label,
        "trend_display": trend_text,
        "trend_detail": trend_detail,
        "active_deterioration": active_deterioration,
        "latest_vitals": latest_vitals,
        "readings_count": len(vitals),
        "contingencies": contingencies,
        "text": text,
    }


def _build_synthesis(patient: dict, severity: str | None) -> dict:
    who = patient.get('name', 'the patient') + (f" ({severity})" if severity else "")
    text = (
        f"Read-back required: receiving clinician to confirm understanding of "
        f"{who}, the action list and the "
        f"contingency plans above, then record outstanding questions, their name "
        f"and the time."
    )
    return {
        "read_back_required": True,
        "confirmed": False,
        "prompts": list(READ_BACK_PROMPTS),
        "text": text,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Plain-text rendering
# ─────────────────────────────────────────────────────────────────────────────
def _render_plain_text(sections: dict, patient: dict, generated_at: str) -> str:
    isv = sections["illness_severity"]
    ps = sections["patient_summary"]
    al = sections["action_list"]
    sa = sections["situation_awareness"]
    sy = sections["synthesis"]

    lines: list[str] = []
    lines.append("I-PASS SHIFT HANDOFF")
    lines.append(f"Patient: {patient.get('name', 'Unknown')}  (MRN {patient.get('mrn', '--')})")
    # The wire keeps the ISO instant; the sheet prints the ward's clock.
    lines.append(f"Auto-drafted: {hospital_stamp(generated_at) or generated_at}  |  "
                 f"Window: last {HANDOFF_WINDOW_HOURS}h")
    lines.append("=" * 60)

    # No severity line at all while scoring is switched off (severity None).
    if isv.get("severity") is not None:
        lines.append("")
        lines.append(f"I. ILLNESS SEVERITY: {isv['severity']}")
        lines.append(f"   {isv['text']}")

    lines.append("")
    lines.append("P. PATIENT SUMMARY")
    lines.append(f"   {ps['text']}")

    lines.append("")
    lines.append("A. ACTION LIST")
    if al["active_medications"]:
        lines.append("   Active medications:")
        for m in al["active_medications"]:
            lines.append(
                (f"     - {m.get('drug_name')} {m.get('dosage') or ''} "
                 f"{m.get('route') or ''} {m.get('frequency') or ''}").rstrip()
                + (f" ({UNVERIFIED_MED_LABEL})" if m.get("unverified") else "")
            )
    else:
        lines.append("   Active medications: none charted.")
    if al["open_care_bundles"]:
        for b in al["open_care_bundles"]:
            out = ", ".join(b["outstanding"]) if b["outstanding"] else "all elements complete"
            name = b.get("bundle_label") or _bundle_label(b.get("bundle_type"))
            lines.append(f"   Open bundle [{name}] outstanding: {out}")
    if al["outstanding_items"]:
        lines.append("   Outstanding items:")
        for item in al["outstanding_items"]:
            lines.append(f"     - {item}")

    lines.append("")
    lines.append("S. SITUATION AWARENESS & CONTINGENCY")
    lines.append(f"   {sa['text']}")
    if sa["contingencies"]:
        lines.append("   Contingency planning:")
        for c in sa["contingencies"]:
            lines.append(f"     - {c}")

    # The AI-drafted supplement sits BETWEEN the last I-PASS section and the
    # read-back, deliberately: the receiver signs off having seen everything
    # they were handed, and the signature block stays the last thing on the
    # page. When there is no assessment on file this contributes NOTHING — not
    # a heading, not a blank line, not "none available". A heading with nothing
    # under it reads as a section someone forgot to complete.
    assessment_lines = _render_assessment_lines(sections.get("ai_assessment"))
    if assessment_lines:
        lines.append("")
        lines.extend(assessment_lines)

    lines.append("")
    lines.append("S. SYNTHESIS BY RECEIVER")
    lines.append(f"   {sy['text']}")
    # One labelled line per prompt, left open for the receiver's pen.
    for prompt in sy.get("prompts") or ():
        lines.append(f"   {prompt}:")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def build_ipass(patient_id: str, window_hours: int = HANDOFF_WINDOW_HOURS,
                *, include_assessment: bool = True) -> dict:
    """Assemble a structured I-PASS handoff draft from the last ~12h of data.

    Returns a dict with the 5 I-PASS sections, a `source_summary`, and a
    `plain_text` rendering. Deterministic — NO LLM CALL, including for the
    AI-drafted supplement, which is read from a draft Sage wrote earlier rather
    than generated here (see `get_reasoned_assessment`). Raises ValueError if
    the patient does not exist.

    `sections["ai_assessment"]` is always present and is None when there is no
    assessment to carry, so the client tests one key rather than inferring from
    absence. `plain_text` renders nothing at all in that case.
    """
    cutoff = _now() - timedelta(hours=window_hours)
    cutoff_iso = _iso(cutoff)

    with db_session() as db:
        patient = _get_patient(db, patient_id)
        if not patient:
            raise ValueError(f"Patient not found: {patient_id}")
        vitals = _get_vitals_window(db, patient_id, cutoff_iso)
        meds = _get_active_meds(db, patient_id)
        problems = _get_active_problems(db, patient_id)
        allergies = _get_active_allergies(db, patient_id)
        recent_notes = _get_recent_notes(db, patient_id, cutoff_iso)

    # Risk + trend (deterministic; degrade gracefully if models/CfC absent).
    # Risk scores the row this handoff PRINTS (vitals[-1]). A second "latest"
    # read was a separate query, and a reading landing between the two gave a
    # severity for a set the handoff did not show.
    # Scoring switched off (ORB_SCORING_ENABLED off): risk is None and the
    # draft carries no severity line and no NEWS2 contingency line.
    risk = (risk_for_row(vitals[-1] if vitals else None, patient_id=patient_id,
                         on_scale_2=bool(patient.get("on_scale_2") or 0))
            if scoring_enabled() else None)
    try:
        trend = get_vitals_trend(patient_id, hours=max(1, window_hours // 2))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[I-PASS] vitals trend failed for %s: %s", patient_id, e)
        trend = {"trend": "insufficient_data"}

    # Open care bundles (active only), with per-element compliance decoration.
    try:
        from services.sepsis_bundle_service import get_bundles_for_patient
        bundles = get_bundles_for_patient(patient_id, active_only=True)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[I-PASS] bundle lookup failed for %s: %s", patient_id, e)
        bundles = []

    # Sage's reasoned assessment, REUSED not generated — its own session, opened
    # after the reads above are closed, for the same reason the bundle and risk
    # lookups are: nothing here nests a second connection inside another's
    # transaction. Staleness bound = this handover's own window (rule 4).
    assessment = get_reasoned_assessment(patient_id, window_hours) if include_assessment else None

    sections = {
        "illness_severity": _build_illness_severity(risk),
        "patient_summary": _build_patient_summary(patient, problems, allergies),
        "action_list": _build_action_list(meds, bundles, risk),
        "situation_awareness": _build_situation_awareness(vitals, trend, risk),
    }
    sections["synthesis"] = _build_synthesis(patient, sections["illness_severity"]["severity"])
    sections["ai_assessment"] = assessment

    generated_at = _iso(_now())
    source_summary = (
        f"Auto-drafted I-PASS handoff for {patient.get('name')} (MRN {patient.get('mrn')}) "
        f"from the last {window_hours}h: {len(vitals)} vitals reading(s), {len(meds)} active "
        f"medication(s), {len(problems)} active problem(s), {len(allergies)} allergy record(s), "
        f"{len(bundles)} open care bundle(s), {len(recent_notes)} recent note(s)."
        + (f" Illness severity {sections['illness_severity']['severity']} "
           f"({sections['illness_severity']['text']})."
           if sections['illness_severity']['severity'] is not None else "")
    )
    if assessment:
        # The summary is stored as the note's `raw_transcript` and is read by a
        # human, so the machine authorship is stated here too — not only in the
        # section itself. Someone reading the summary alone must not come away
        # believing a clinician wrote the assessment.
        source_summary += (
            f" Carries an AI-drafted assessment written by Orb Sage "
            f"{assessment['age_display']} (not written by a clinician; "
            f"{assessment['state_display']})."
        )
    plain_text = _render_plain_text(sections, patient, generated_at)

    return {
        "patient_id": patient_id,
        "generated_at": generated_at,
        "window_hours": window_hours,
        "source_summary": source_summary,
        "sections": sections,
        "plain_text": plain_text,
    }


def save_ipass_handoff(patient_id: str, author_id: str, content) -> int:
    """Persist an I-PASS handoff as a scribe_notes row (note_format='handoff').

    `content` may be the dict returned by build_ipass or a plain string. The
    source summary is stored as raw_transcript and the rendered handoff as
    structured_note, with status='draft'. Returns the new note id.
    """
    from services.scribe_service import save_scribe_note

    if isinstance(content, str):
        source_summary = content
        rendered = content
    elif isinstance(content, dict):
        source_summary = content.get("source_summary") or content.get("plain_text") or ""
        rendered = content.get("plain_text") or content.get("rendered") or ""
        # If a caller passed only structured sections, render a JSON fallback so
        # the note is never empty.
        if not rendered and content.get("sections"):
            rendered = json.dumps(content["sections"], default=str, indent=2)
        if not source_summary:
            source_summary = "I-PASS handoff (auto-drafted)"
    else:
        raise ValueError("content must be an I-PASS dict or a string")

    note_id = save_scribe_note(
        patient_id=patient_id,
        author_id=author_id,
        raw_transcript=source_summary,
        structured_note=rendered,
        note_format="handoff",
        status="draft",
        ai_generated=True,
    )
    logger.info("[I-PASS] saved handoff note %s for patient %s by %s", note_id, patient_id, author_id)
    return note_id


def get_recent_handoffs(patient_id: str, limit: int = 10) -> list[dict]:
    """Return recent I-PASS handoff notes for a patient (note_format='handoff').

    Newest first, `id` breaking a same-second tie, with the author's NAME: the
    incoming nurse reads "handed over 06:40 PM by S. Nair", not a user id.
    """
    with db_session() as db:
        rows = db.execute(
            """SELECT s.id, s.patient_id, s.author_id, u.name AS author_name,
                      s.raw_transcript, s.structured_note, s.note_format, s.status,
                      s.created_at
               FROM scribe_notes s
               LEFT JOIN users u ON u.id = s.author_id
               WHERE s.patient_id = ? AND s.note_format = 'handoff'
               ORDER BY s.created_at DESC, s.id DESC LIMIT ?""",
            (patient_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
