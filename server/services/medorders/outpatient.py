"""Orb: outpatient prescriptions on the medication-order store (opd-12, W8-C).

An OPD prescription line is a medication order like any other (decision A1:
one authoritative order, med_orders, with its ledger), written through core's
in-transaction writers with:

    encounter_type  'op'
    source          'opd_prescription'
    status          'prescribed' at once: the consulting doctor writes and
                    signs the line in one step, as its prescriber
    admission_id    NULL (an outpatient line is refused for an admitted patient)
    start_at        when it was written; end_at the end of the course when a
                    duration was given (start plus duration_days), else NULL
    duration_text   'N days', notes = the instructions to the patient
and the visit, the department, duration_days, quantity and instructions on the
'created' event's payload (migration 61 has no column for them and this
increment adds no migration). A visit's lines are found through that payload.

WHAT IT NEVER WRITES. No medications row (the ward projection the chart and the
eMAR read: a take-home line must never be offered to a nurse as an inpatient
dose), no Helix supply row (outpatient dispensing is the pharmacy epic's lane),
no CPOE row, no vitals or risk_scores. tests/test_opd_prescription.py holds this
module to it by reading its source. The ward projection is never touched: an
'op' order has no ward row, and its cancel passes project=False.

THE SCREEN (every line, before anything is written):
  1. Ruling OPD-S7 (a), applied strictly: no line is written until the patient
     has an allergy review that lets this visit's note be signed
     (opd_service.allergy_review_status, can_sign). The interlock cannot tell
     "no known allergies" from "never asked", and an outpatient prescription
     leaves the building with nobody else checking it. 409
     allergy_review_required, before the gate runs, and again inside the
     write transaction.
  2. medication_safety_gate.enforce_medication_safety(entry_point=
     'opd_prescription') with the dose, the route and the prescriber's role:
     the same allergy match (floored at effective_severity), interaction
     screen against the patient's active chart rows and dose guard every lane
     uses. A severe allergy or a dose block clears only for a prescriber with a
     written reason (428 until then); the gate is unchanged, so held ruling
     R011 is untouched.
  3. The outpatient interaction screen (builder's addition, strict): the gate's
     interaction screen reads only active medications rows, and an outpatient
     line has none, so two lines written on the same visit were never checked
     against each other. Here the new drug is compared, through the same
     knowledge base (drug_safety_service.interaction_terms and
     interaction_db.lookup_interactions), with the patient's live outpatient
     lines whose course has not ended. A severe pair needs the prescriber's
     acknowledgement with a reason, exactly as a severe gate alert does (428
     drug_safety_acknowledgement_required with the merged screen); a drug the
     knowledge base does not know is named in the coverage note. It runs
     before the gate, so a 428 from the gate shows these alerts too and one
     acknowledgement never covers an alert the prescriber was not shown.
Then one immediate transaction re-reads the visit (cancelled, finished, outside
the OPD window or the patient now admitted: 409), the allergy review and the
screen fingerprint (an allergy or a drug charted in between: 409
screen_stale), and writes the order, its 'created' event and its fail-closed
clinical_access audit row together.

Same-ingredient duplicates are a notice (ruling MO-R6 is a later increment).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from fastapi import HTTPException

from database import db_session
from services import medication_safety_gate
from services.clinical_time import format_utc, parse_to_utc
from services.medorders import core, queries

logger = logging.getLogger("orb.medorders.outpatient")

__all__ = [
    "ENTRY_POINT", "ENTRY_CANCEL", "LANE_OPD", "SOURCE", "ENCOUNTER_TYPE",
    "PRINT_AUDIT_ACTION", "LIST_AUDIT_ACTION", "MAX_LINES_PER_VISIT", "MAX_DURATION_DAYS",
    "MAX_QUANTITY", "MIN_CANCEL_REASON", "STALE_SCREEN_MESSAGE", "PrescriptionRefused",
    "normalise_line", "outpatient_interactions", "visit_lines", "list_visit_prescriptions",
    "write_prescription", "cancel_prescription", "print_lines",
]

#: The safety gate's entry point for this lane (its audit rows name it).
ENTRY_POINT = "opd_prescription"
ENTRY_CANCEL = "opd_prescription.cancel"
LANE_OPD = "opd"
SOURCE = "opd_prescription"
ENCOUNTER_TYPE = "op"
#: phi_access rows the routes write for a print and a list read.
PRINT_AUDIT_ACTION = "opd_prescription_printed"
LIST_AUDIT_ACTION = "opd_prescriptions_viewed"

#: Live lines one visit may carry: a bound on one runaway form, not a
#: clinical limit.
MAX_LINES_PER_VISIT = 25
MAX_DURATION_DAYS = 365
MAX_QUANTITY = 9999
MIN_CANCEL_REASON = 3

#: Field caps (the route's body model holds the same).
_TEXT_LIMITS = {
    "drug_name": 200, "dose": 60, "route": 40, "frequency": 60,
    "prn_indication": 200, "indication": 200, "instructions": 500,
}

STALE_SCREEN_MESSAGE = ("An allergy or a medicine was charted for this patient while this "
                        "line was being checked. Check the line again.")

_SEVERITY_RANK = {"none": 0, "mild": 1, "moderate": 2, "severe": 3}

#: Plain words for each order state on the clinic's screen and the print.
STATUS_WORDS = {
    "prescribed": "Prescribed", "verified": "Prescribed", "held": "On hold",
    "stopped": "Stopped", "completed": "Course complete", "cancelled": "Cancelled",
    "rejected": "Rejected", "draft": "Not signed",
}


class PrescriptionRefused(ValueError):
    """A prescription request that cannot be honoured. `status` is the HTTP
    status, `code` detail.error, `extra` more detail fields."""

    def __init__(self, code: str, message: str, status: int = 409, **extra: Any):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


# ── Input ─────────────────────────────────────────────────────────────────────

def _text(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) > _TEXT_LIMITS[name]:
        raise PrescriptionRefused(f"bad_{name}", f"Keep this under {_TEXT_LIMITS[name]} characters",
                                  status=422)
    return text or None


def _whole_number(value: Any, name: str, low: int, high: int, words: str) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise PrescriptionRefused(f"bad_{name}", words, status=422)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise PrescriptionRefused(f"bad_{name}", words, status=422) from None
    if number != value and not (isinstance(value, str) and value.strip() == str(number)):
        raise PrescriptionRefused(f"bad_{name}", words, status=422)
    if not (low <= number <= high):
        raise PrescriptionRefused(f"bad_{name}", words, status=422)
    return number


def normalise_line(raw: Mapping[str, Any]) -> dict:
    """One prescription line as it is stored, or a 422 refusal.

    Required: the medicine, its dose, route and frequency (the dose guard
    reads the dose and the route, and a printed line without them is not a
    prescription; the route is never defaulted). 'Only when needed' needs
    what for. Duration in whole days (1 to 365) and quantity in whole units
    (1 to 9999) are optional; nothing is invented when they are left out."""
    drug = _text(raw.get("drug_name"), "drug_name")
    if not drug:
        raise PrescriptionRefused("drug_name_required", "Enter the medicine's name", status=422)
    dose = _text(raw.get("dose"), "dose")
    route = _text(raw.get("route"), "route")
    frequency = _text(raw.get("frequency"), "frequency")
    if not (dose and route and frequency):
        raise PrescriptionRefused(
            "dose_route_and_frequency_required",
            "Enter the dose, the route and how often it is taken", status=422)
    is_prn = bool(raw.get("is_prn"))
    prn_indication = _text(raw.get("prn_indication"), "prn_indication")
    if is_prn and not prn_indication:
        raise PrescriptionRefused(
            "prn_indication_required",
            "Say when the patient should take it, for example for fever above 38.5", status=422)
    if not is_prn:
        prn_indication = None
    return {
        "drug_name": drug,
        "dose": dose,
        "route": route,
        "frequency": frequency,
        "is_prn": is_prn,
        "prn_indication": prn_indication,
        "duration_days": _whole_number(
            raw.get("duration_days"), "duration_days", 1, MAX_DURATION_DAYS,
            f"Duration is a whole number of days from 1 to {MAX_DURATION_DAYS}"),
        "quantity": _whole_number(
            raw.get("quantity"), "quantity", 1, MAX_QUANTITY,
            f"Quantity is a whole number from 1 to {MAX_QUANTITY}"),
        "instructions": _text(raw.get("instructions"), "instructions"),
        "indication": _text(raw.get("indication"), "indication"),
    }


# ── The visit ─────────────────────────────────────────────────────────────────

def _visit_row(conn, visit_id: str):
    return conn.execute("SELECT * FROM opd_visits WHERE id = ?", (visit_id,)).fetchone()


def _active_admission(conn, patient_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
        (patient_id,),
    ).fetchone() is not None


def _require_writable_visit(conn, visit) -> None:
    """The visit a prescription write may touch: not cancelled, not finished,
    inside the OPD scope window (permissions.opd_scope_window, the OPD arm's
    own window), and the patient not admitted anywhere (the inpatient chart
    owns the medicines then)."""
    from permissions import opd_scope_window

    if visit is None:
        raise PrescriptionRefused("visit_not_found", "OPD visit not found", status=404)
    if visit["status"] == "cancelled":
        raise PrescriptionRefused("visit_cancelled",
                                  "This visit was cancelled; nothing can be prescribed on it.")
    if visit["status"] == "completed":
        raise PrescriptionRefused(
            "visit_completed",
            "This visit is finished. Prescribe from the patient's current visit.")
    first_day, last_day = opd_scope_window()
    day = str(visit["visit_date"] or "").strip()[:10]
    if day and day > last_day:
        raise PrescriptionRefused(
            "visit_not_current",
            "This visit is booked for a later day. Prescribe on the day of the visit.")
    if not day or day < first_day:
        raise PrescriptionRefused(
            "visit_not_current",
            "This visit is too old to prescribe on. Prescribe from the patient's current visit.")
    if _active_admission(conn, visit["patient_id"]):
        raise PrescriptionRefused(
            "patient_admitted",
            "This patient is now admitted. Prescribe on the inpatient chart.")


def _require_allergy_review(conn, patient_id: str, visit_id: str) -> dict:
    """Ruling OPD-S7 (a): refused until an allergy review lets this visit's
    note be signed. The answer carries the review as a visit-addressed answer
    may (status, label and time; never an allergen)."""
    from services.opd_service import allergy_review_status

    review = allergy_review_status(conn, patient_id, visit_id)
    if not review["can_sign"]:
        raise PrescriptionRefused(
            "allergy_review_required",
            "Record the allergy review before prescribing: the patient's allergies, no known "
            "allergies, or why they could not be asked.",
            allergy_review=review)
    return review


# ── Reading a visit's lines ───────────────────────────────────────────────────

def _loads(text: Optional[str]) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _created_payloads(conn, order_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for start in range(0, len(order_ids), 400):
        chunk = order_ids[start:start + 400]
        marks = ", ".join("?" for _ in chunk)
        for r in conn.execute(
            f"SELECT med_order_id, payload_json FROM med_order_events "
            f"WHERE kind = 'created' AND med_order_id IN ({marks})", chunk,
        ).fetchall():
            out[r["med_order_id"]] = _loads(r["payload_json"])
    return out


def _user_names(conn, ids) -> dict:
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    marks = ", ".join("?" for _ in wanted)
    return {r["id"]: r["name"] for r in conn.execute(
        f"SELECT id, name FROM users WHERE id IN ({marks})", wanted).fetchall()}


def _line_view(order: Mapping, payload: Mapping, names: Mapping) -> dict:
    """A line as the clinic sees it: what was prescribed and its state. The
    stored screen is not returned (it names allergens, and a visit id of any
    date reaches this answer); only whether a block was overridden."""
    status = order["status"]
    ended = status in core.TERMINAL_STATUSES
    safety = _loads(order.get("last_safety_json"))
    return {
        "id": order["id"],
        "opd_visit_id": payload.get("opd_visit_id"),
        "drug_name": order["drug_name"],
        "dose": order.get("dose"),
        "route": order.get("route"),
        "frequency": order.get("frequency"),
        "is_prn": bool(order.get("is_prn")),
        "prn_indication": order.get("prn_indication"),
        "duration_days": payload.get("duration_days"),
        "quantity": payload.get("quantity"),
        "instructions": payload.get("instructions"),
        "indication": order.get("indication"),
        "status": status,
        "status_label": STATUS_WORDS.get(status, status),
        "live": status in core.LIVE_STATUSES,
        "version": order.get("version"),
        "prescribed_at": order.get("prescribed_at") or order.get("created_at"),
        "prescriber_name": names.get(order.get("prescriber_id") or order.get("entered_by")),
        "course_ends_at": order.get("end_at"),
        "cancel_reason": order.get("status_reason") if ended else None,
        "ended_at": order.get("status_changed_at") if ended else None,
        "ended_by_name": names.get(order.get("status_changed_by")) if ended else None,
        "safety_overridden": bool(safety.get("override")),
    }


def visit_lines(conn, visit) -> list[dict]:
    """Every outpatient line written on this visit, oldest first, any state."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM med_orders WHERE patient_id = ? AND encounter_type = ? AND source = ? "
        "ORDER BY created_at, id",
        (visit["patient_id"], ENCOUNTER_TYPE, SOURCE),
    ).fetchall()]
    if not rows:
        return []
    payloads = _created_payloads(conn, [r["id"] for r in rows])
    mine = [r for r in rows if payloads.get(r["id"], {}).get("opd_visit_id") == visit["id"]]
    names = _user_names(conn, [x for r in mine for x in (r.get("prescriber_id"),
                                                         r.get("entered_by"),
                                                         r.get("status_changed_by"))])
    return [_line_view(r, payloads[r["id"]], names) for r in mine]


def list_visit_prescriptions(visit_id: str) -> dict:
    """The visit's lines (the caller has proved the visit is in its clinic)."""
    with db_session() as conn:
        visit = _visit_row(conn, visit_id)
        if visit is None:
            raise PrescriptionRefused("visit_not_found", "OPD visit not found", status=404)
        lines = visit_lines(conn, visit)
    return {"visit_id": visit_id, "patient_id": visit["patient_id"], "prescriptions": lines}


def print_lines(visit_id: str) -> dict:
    """What the printed prescription carries, read in one session: the live
    lines (409 no_prescriptions when there are none), the allergy review as a
    visit-addressed answer may carry it (status, label and time), the signed
    note's diagnoses (a draft is never printed) and the prescribers' ids."""
    from services.opd_service import allergy_review_status, consultation_signed

    with db_session() as conn:
        visit = _visit_row(conn, visit_id)
        if visit is None:
            raise PrescriptionRefused("visit_not_found", "OPD visit not found", status=404)
        lines = [line for line in visit_lines(conn, visit) if line["live"]]
        if not lines:
            raise PrescriptionRefused("no_prescriptions",
                                      "No medicine is prescribed on this visit.")
        review = allergy_review_status(conn, visit["patient_id"], visit["id"])
        diagnoses: list[str] = []
        if consultation_signed(conn, visit["id"]):
            row = conn.execute("SELECT diagnoses_json FROM opd_consultations WHERE visit_id = ?",
                               (visit["id"],)).fetchone()
            try:
                stored = json.loads(row["diagnoses_json"] or "[]") if row else []
            except (TypeError, ValueError):
                stored = []
            diagnoses = [d.get("text") for d in stored
                         if isinstance(d, dict) and str(d.get("text") or "").strip()]
        prescribers = [r["prescriber_id"] for r in conn.execute(
            f"SELECT prescriber_id FROM med_orders WHERE id IN ({', '.join('?' for _ in lines)}) "
            "ORDER BY created_at, id", [line["id"] for line in lines]).fetchall()]
    return {
        "visit": dict(visit),
        "lines": lines,
        "allergy_review": review.get("assessment"),
        "diagnoses": diagnoses,
        "prescriber_ids": list(dict.fromkeys(p for p in prescribers if p)),
    }


# ── The outpatient interaction screen ─────────────────────────────────────────

def _course_ended(end_at: Optional[str], now: datetime) -> bool:
    """A course with an end that has passed. An unreadable end is treated as
    live (the screen stays stricter, never quieter)."""
    if not end_at:
        return False
    try:
        return parse_to_utc(end_at) <= now
    except ValueError:
        return False


def _live_outpatient_drugs(conn, patient_id: str, now: datetime) -> list[str]:
    marks = ", ".join("?" for _ in core.LIVE_STATUSES)
    rows = conn.execute(
        f"SELECT drug_name, end_at FROM med_orders WHERE patient_id = ? AND encounter_type = ? "
        f"AND status IN ({marks}) ORDER BY created_at, id",
        (patient_id, ENCOUNTER_TYPE, *core.LIVE_STATUSES),
    ).fetchall()
    return [r["drug_name"] for r in rows if not _course_ended(r["end_at"], now)]


def outpatient_interactions(patient_id: str, drug_name: str, *,
                            now: Optional[datetime] = None) -> tuple[list[dict], list[str]]:
    """Interaction alerts for `drug_name` against the patient's live
    outpatient lines (courses not yet ended), and the lines the knowledge base
    does not know. One alert per co-prescribed line, the worst severity
    leading, every distinct mechanism kept: the shape of the gate's own
    interaction alerts, plus setting 'outpatient'."""
    from services import interaction_db
    from services.drug_safety_service import interaction_recognised, interaction_terms

    when = now or datetime.now(timezone.utc)
    with db_session() as conn:
        existing_drugs = _live_outpatient_drugs(conn, patient_id, when)
    if not existing_drugs:
        return [], []
    interaction_db.ensure_seeded()
    new_terms = interaction_terms(drug_name)
    alerts: list[dict] = []
    unscreened: list[str] = []
    for existing in existing_drugs:
        existing_terms = interaction_terms(existing)
        if not interaction_recognised(existing, existing_terms):
            if existing not in unscreened:
                unscreened.append(existing)
            continue
        hits = interaction_db.lookup_interactions(new_terms, existing_terms) if new_terms else []
        if not hits:
            continue
        hits.sort(key=lambda h: (-_SEVERITY_RANK.get(h["severity"], 0),
                                 h.get("match_type") != "ingredient"))
        mechanisms: list[str] = []
        for h in hits:
            m = (h["mechanism"] or "").strip()
            if m and m not in mechanisms:
                mechanisms.append(m)
        alerts.append({
            "type": "interaction",
            "severity": hits[0]["severity"],
            "with": existing,
            "setting": "outpatient",
            "message": (f"{drug_name} + {existing} (outpatient prescription): "
                        f"{' '.join(mechanisms)}").strip(),
            "classes": sorted({t for h in hits for t in (h["term_a"], h["term_b"])}),
        })
    return alerts, unscreened


def _merge_screen(safety: Mapping, alerts: list[dict], unscreened: list[str]) -> dict:
    """The gate's screen with the outpatient alerts added: the worst severity,
    blocking and the coverage note recomputed so nothing reads cleaner than
    either screen said."""
    merged = dict(safety)
    merged["interaction_alerts"] = list(safety.get("interaction_alerts") or []) + alerts
    worst = safety.get("max_severity") or "none"
    for a in alerts:
        if _SEVERITY_RANK.get(a["severity"], 0) > _SEVERITY_RANK.get(worst, 0):
            worst = a["severity"]
    merged["max_severity"] = worst
    merged["blocking"] = bool(safety.get("blocking")) or worst == "severe"
    merged["safe"] = bool(safety.get("safe", True)) and not alerts
    before = list(safety.get("unscreened_with") or [])
    union = before + [d for d in unscreened if d not in before]
    merged["unscreened_with"] = union
    if safety.get("interaction_screened") is not False and len(union) > len(before):
        merged["coverage_note"] = (
            f"Not screened against {', '.join(union)}: outside the interaction knowledge base. "
            "Check those pairs manually.")
    merged["outpatient_screen"] = {"alerts": len(alerts), "unscreened": unscreened}
    return merged


def _prescriber_may_clear(actor_role: Optional[str]) -> bool:
    from permissions import MEDICATION_PRESCRIBE, has_permission
    role = (actor_role or "").strip().lower()
    return bool(role) and has_permission(role, MEDICATION_PRESCRIBE)


def _audit_screen(*, outcome: str, action_type: str, patient_id: str, drug_name: str,
                  actor_id: Optional[str], alerts: list[dict], ack_reason: Optional[str] = None,
                  prescriber: Optional[dict] = None, db=None) -> None:
    """The outpatient screen's block or override on the chain, in the gate's
    shape (category clinical_safety, entry_point opd_prescription)."""
    from services.audit_service import log_audit_event

    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="clinical_safety",
            action_type=action_type,
            outcome=outcome,
            db=db,
            summary_message=(
                f"Outpatient interaction screen for {drug_name}: {len(alerts)} alert(s) against "
                f"the patient's outpatient prescriptions, entry={ENTRY_POINT}"
                + (f", override_reason={ack_reason}" if ack_reason else "")),
            actor_user_id=actor_id,
            target_type="medication",
            target_id=drug_name,
            patient_id=patient_id,
            metadata={
                "entry_point": ENTRY_POINT,
                "screen": "outpatient_interactions",
                "max_severity": max((a["severity"] for a in alerts),
                                    key=lambda s: _SEVERITY_RANK.get(s, 0), default="none"),
                "interaction_alerts": alerts,
                "override_reason": ack_reason,
                **({"override_by": prescriber} if prescriber else {}),
            },
        )
    except Exception as e:  # pragma: no cover: the gate's own rule, audit must not block care
        logger.error("[OPD prescription] screen audit failed for %s/%s: %s",
                     patient_id, drug_name, e)


def _screen(patient_id: str, line: Mapping, *, actor_id: str, actor_role: Optional[str],
            safety_ack: Optional[Mapping],
            visit_id: Optional[str] = None) -> tuple[str, dict, Optional[dict]]:
    """The fingerprint, the outpatient interaction screen and the gate, all
    outside any write transaction. Returns (fingerprint, merged screen,
    outpatient override or None); raises the gate's 428 or 503, or this
    screen's own 428.

    The outpatient screen runs FIRST so that a 428 from the gate carries its
    alerts too: one acknowledgement then answers every severe alert the
    prescriber was shown, never one they were not."""
    with db_session() as conn:
        before = core.screen_fingerprint_in_txn(conn, patient_id)
    try:
        alerts, unscreened = outpatient_interactions(patient_id, line["drug_name"])
    except Exception as e:
        # Never fail open: a screen that could not run is the gate's 503.
        logger.error("[OPD prescription] outpatient interaction screen failed for %s/%s: %s",
                     patient_id, line["drug_name"], e)
        raise HTTPException(status_code=503, detail={
            "error": "drug_safety_screen_unavailable",
            "message": ("The drug-safety screen could not run against this patient's other "
                        "outpatient prescriptions. Nothing was prescribed. Try again."),
        })
    try:
        safety = medication_safety_gate.enforce_medication_safety(
            patient_id,
            line["drug_name"],
            actor_id=actor_id,
            entry_point=ENTRY_POINT,
            safety_ack=dict(safety_ack) if safety_ack else None,
            dose=line["dose"],
            route=line["route"],
            actor_role=actor_role,
            # The visit, so the same-medicine rule tells this visit's lines
            # from an earlier visit's (services/medorders/screening.py).
            opd_visit_id=visit_id,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else None
        if exc.status_code == 428 and detail and isinstance(detail.get("safety"), dict):
            raise HTTPException(status_code=428, detail={
                **detail, "safety": _merge_screen(detail["safety"], alerts, unscreened)})
        raise
    merged = _merge_screen(safety, alerts, unscreened)
    severe = [a for a in alerts if a["severity"] == "severe"]
    if not severe:
        return before, merged, None
    reason = str((safety_ack or {}).get("reason") or "").strip()
    acknowledged = bool(safety_ack) and bool(reason) and bool((safety_ack or {}).get("acknowledged"))
    if acknowledged and not _prescriber_may_clear(actor_role):
        acknowledged = False
        merged["ack_refused"] = medication_safety_gate.ALLERGY_ACK_REFUSAL.replace(
            "an allergy block", "a severe interaction")
    if not acknowledged:
        _audit_screen(outcome="denied", action_type="drug_safety_block", patient_id=patient_id,
                      drug_name=line["drug_name"], actor_id=actor_id, alerts=severe)
        raise HTTPException(status_code=428, detail={
            "error": "drug_safety_acknowledgement_required",
            "message": merged.get("ack_refused") or (
                "Severe drug-safety alerts must be acknowledged with a reason before this "
                "line is prescribed."),
            "safety": merged,
        })
    with db_session() as conn:
        row = conn.execute("SELECT name FROM users WHERE id = ?", (actor_id,)).fetchone()
    override = {"by": actor_id, "by_name": row["name"] if row else None, "reason": reason}
    merged.setdefault("override", override)
    merged["outpatient_override"] = override
    return before, merged, {"alerts": severe, "reason": reason,
                            "prescriber": {"user_id": actor_id,
                                           "display_name": override["by_name"],
                                           "role": actor_role}}


# ── The write ─────────────────────────────────────────────────────────────────

def _replay(conn, idempotency_key: Optional[str], *, visit_id: str, patient_id: str,
            drug_name: str) -> Optional[dict]:
    """The line an earlier request with this key wrote, when it is this same
    line on this visit; 409 idempotency_key_reused for any other use."""
    if not idempotency_key:
        return None
    prior = conn.execute("SELECT * FROM med_orders WHERE idempotency_key = ?",
                         (idempotency_key,)).fetchone()
    if prior is None:
        return None
    prior = dict(prior)
    payload = _created_payloads(conn, [prior["id"]]).get(prior["id"], {})
    if (prior["patient_id"] != patient_id or prior["drug_name"] != drug_name
            or prior["source"] != SOURCE or payload.get("opd_visit_id") != visit_id):
        raise PrescriptionRefused("idempotency_key_reused",
                                  "This request key was already used for another line.")
    names = _user_names(conn, [prior.get("prescriber_id"), prior.get("status_changed_by")])
    return _line_view(prior, payload, names)


def _course_end(now_iso: str, days: Optional[int]) -> Optional[str]:
    if not days:
        return None
    return format_utc(parse_to_utc(now_iso) + timedelta(days=days))


def write_prescription(visit_id: str, raw: Mapping[str, Any], *, actor_id: str,
                       actor_role: Optional[str], safety_ack: Optional[Mapping] = None,
                       idempotency_key: Optional[str] = None,
                       request_meta: Optional[Mapping] = None) -> dict:
    """Prescribe one line on an outpatient visit. BLOCKING (the gate opens its
    own sessions): the route is a plain def.

    The caller has proved the visit is in its clinic and that the caller holds
    opd.consult and medication.prescribe. Returns {prescription, safety,
    duplicates, replayed}. Raises PrescriptionRefused, core.MedOrderError
    (screen_stale), the gate's HTTPException (428, 503) or AuditWriteError."""
    if not (actor_id or "").strip():
        raise PrescriptionRefused("actor_required", "Sign in again to prescribe", status=401)
    line = normalise_line(raw)

    with db_session() as conn:
        visit = _visit_row(conn, visit_id)
        if visit is None:
            raise PrescriptionRefused("visit_not_found", "OPD visit not found", status=404)
        patient_id = visit["patient_id"]
        replayed = _replay(conn, idempotency_key, visit_id=visit_id, patient_id=patient_id,
                           drug_name=line["drug_name"])
        if replayed is not None:
            return {"prescription": replayed, "safety": None, "duplicates": [],
                    "replayed": True}
        _require_writable_visit(conn, visit)
        _require_allergy_review(conn, patient_id, visit_id)

    before, safety, op_override = _screen(patient_id, line, actor_id=actor_id,
                                          actor_role=actor_role, safety_ack=safety_ack,
                                          visit_id=visit_id)

    with db_session(immediate=True) as conn:
        visit = _visit_row(conn, visit_id)
        replayed = _replay(conn, idempotency_key, visit_id=visit_id, patient_id=patient_id,
                           drug_name=line["drug_name"])
        if replayed is not None:
            return {"prescription": replayed, "safety": None, "duplicates": [],
                    "replayed": True}
        _require_writable_visit(conn, visit)
        review = _require_allergy_review(conn, patient_id, visit_id)
        fingerprint = core.screen_fingerprint_in_txn(conn, patient_id)
        if fingerprint != before:
            raise core.StaleScreen(STALE_SCREEN_MESSAGE)
        live_on_visit = [x for x in visit_lines(conn, visit) if x["live"]]
        if len(live_on_visit) >= MAX_LINES_PER_VISIT:
            raise PrescriptionRefused(
                "too_many_lines",
                f"A visit carries at most {MAX_LINES_PER_VISIT} medicines. Cancel one first.")
        duplicates = queries.same_ingredient_live(conn, patient_id, line["drug_name"])
        now = format_utc(datetime.now(timezone.utc))
        payload = {
            "setting": "outpatient",
            "opd_visit_id": visit_id,
            "department": visit["department"],
            "duration_days": line["duration_days"],
            "quantity": line["quantity"],
            "instructions": line["instructions"],
        }
        write = core.create_in_txn(
            conn,
            patient_id=patient_id,
            drug_name=line["drug_name"],
            source=SOURCE,
            status="prescribed",
            entered_by=actor_id,
            entered_by_role=actor_role,
            actor_id=actor_id,
            actor_role=actor_role,
            entry_point=ENTRY_POINT,
            admission_id=None,
            encounter_type=ENCOUNTER_TYPE,
            dose=line["dose"],
            route=line["route"],
            frequency=line["frequency"],
            is_prn=line["is_prn"],
            prn_indication=line["prn_indication"],
            start_at=now,
            end_at=_course_end(now, line["duration_days"]),
            duration_text=(f"{line['duration_days']} days" if line["duration_days"] else None),
            indication=line["indication"],
            notes=line["instructions"],
            prescriber_id=actor_id,
            idempotency_key=idempotency_key,
            safety=safety,
            fingerprint=fingerprint,
            event_payload=payload,
            lane=LANE_OPD,
            audit=False,
            now=now,
        )
        if op_override is not None:
            _audit_screen(outcome="partial_success", action_type="drug_safety_override",
                          patient_id=patient_id, drug_name=line["drug_name"], actor_id=actor_id,
                          alerts=op_override["alerts"], ack_reason=op_override["reason"],
                          prescriber=op_override["prescriber"], db=conn)
        core.audit_in_txn(
            conn, order=write.order, action="med_order_created", actor_id=actor_id,
            actor_role=actor_role, entry_point=ENTRY_POINT, from_status=None,
            to_status="prescribed", lane=LANE_OPD, event_id=write.event_id, safety=safety,
            extra={
                "setting": "outpatient",
                "opd_visit_id": visit_id,
                "department": visit["department"],
                "duration_days": line["duration_days"],
                "quantity": line["quantity"],
                "allergy_review": review["status"],
                "allergy_review_basis": review["basis"],
                "same_ingredient_live": len(duplicates),
                "outpatient_interaction_alerts": (safety.get("outpatient_screen") or {}).get(
                    "alerts", 0),
                "outpatient_override": bool(op_override),
            },
            request_meta=request_meta,
        )
        names = _user_names(conn, [actor_id])
        view = _line_view(write.order, payload, names)
    return {"prescription": view, "safety": safety, "duplicates": duplicates, "replayed": False}


# ── Cancel ────────────────────────────────────────────────────────────────────

def _printed_before(conn, patient_id: str, visit_id: str, order_id: str) -> bool:
    """Was a printed prescription that listed this line handed out already?
    Read from the print's own phi_access rows (they name the lines printed)."""
    for r in conn.execute(
        "SELECT metadata_json FROM audit_logs WHERE patient_id = ? AND action_type = ? "
        "AND target_id = ?",
        (patient_id, PRINT_AUDIT_ACTION, visit_id),
    ).fetchall():
        if order_id in (_loads(r["metadata_json"]).get("order_ids") or []):
            return True
    return False


def cancel_prescription(visit_id: str, order_id: str, *, reason: Any,
                        expected_version: Optional[int], actor_id: str,
                        actor_role: Optional[str],
                        request_meta: Optional[Mapping] = None) -> dict:
    """Cancel one line on an open visit, with a reason, by a prescriber.

    Only while the visit is open (not finished or cancelled), inside the OPD
    window and the patient not admitted: a finished visit's prescription has
    left the clinic. The order moves to 'cancelled' through core (guarded
    UPDATE, 'cancelled' event, fail-closed audit row); there is no ward row to
    change. `printed_before` says a print listing the line was already made,
    so the clinic takes that copy back."""
    why = " ".join(str(reason or "").split())
    if len(why) < MIN_CANCEL_REASON:
        raise PrescriptionRefused("reason_required", "Say why this medicine is cancelled.",
                                  status=422)
    with db_session(immediate=True) as conn:
        visit = _visit_row(conn, visit_id)
        _require_writable_visit(conn, visit)
        order = core.get_order_in_txn(conn, order_id)
        payload = _created_payloads(conn, [order_id]).get(order_id, {}) if order else {}
        if (order is None or order["encounter_type"] != ENCOUNTER_TYPE
                or order["source"] != SOURCE or order["patient_id"] != visit["patient_id"]
                or payload.get("opd_visit_id") != visit_id):
            raise PrescriptionRefused("prescription_not_found",
                                      "This medicine is not on this visit's prescription.",
                                      status=404)
        printed = _printed_before(conn, visit["patient_id"], visit_id, order_id)
        write = core.transition_in_txn(
            conn, order_id, "cancelled", expected_version=expected_version,
            actor_id=actor_id, actor_role=actor_role, entry_point=ENTRY_CANCEL, reason=why,
            lane=LANE_OPD, project=False,
            audit_extra={"setting": "outpatient", "opd_visit_id": visit_id,
                         "printed_before_cancel": printed},
            request_meta=request_meta,
        )
        names = _user_names(conn, [write.order.get("prescriber_id"), actor_id])
        view = _line_view(write.order, payload, names)
    return {"prescription": view, "printed_before": printed}
