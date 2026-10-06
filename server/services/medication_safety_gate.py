"""Orb — Medication Safety Gate.

The single enforcement point for order-time drug safety. Every code path that
writes a medication (Vigil med-add, CPOE med orders, Relay ambient actions,
Sage agentic handoff, signed-note auto-extraction, Helix approve) calls
``enforce_medication_safety`` before the write, so a drug can no longer reach
the chart without the same allergy + interaction screen a pharmacist would
expect — or without an explicit, audited override.

Contract (mirrors the Scribe sign gate's 428 pattern):

  * severity < severe            → proceed; alerts returned for display and
                                   audited alongside the write.
  * severity == severe, no ack   → HTTP 428 Precondition Required, body carries
                                   the full safety payload; interactive callers
                                   re-submit with ``safety_ack``.
  * severity == severe, ack      → proceed; the override (who/why/what alerts)
                                   is written to the tamper-evident audit chain.
  * non-interactive callers      → pass ``interactive=False``; severe alerts
                                   can never be acknowledged away — the caller
                                   must route the med to human review instead
                                   (raises MedicationSafetyBlocked).

The screen itself NEVER fails open silently: if the safety check errors, the
gate raises for interactive paths (the clinician sees "screening unavailable",
not "no alerts") and blocks non-interactive paths.

THE DOSE GUARD RIDES THE SAME GATE (2026-10-03). A caller that knows the dose
passes ``dose`` (and ``route``), and ``drug_safety_service.evaluate_dose`` runs
here, so the 8 g paracetamol the order-set and bedside lanes stop is stopped on
every single-order lane too. A dose BLOCK answers the same 428 with the
verdict on ``safety.dose_check``, and clears only for an actor holding
``medication.prescribe`` with a reason ``validate_dose_override_reason``
accepts: "ok" must not clear a 10x dose at order time any more than at the bed.

AN ALLERGY BLOCK IS PRESCRIBER ONLY TOO (2026-10-06). A severe allergy alert
clears only for an actor holding ``medication.prescribe``, exactly as the
bedside interlock (routes/emar_routes.py) and the dose block above. Anyone else
gets the same 428 with the refusal on ``safety.ack_refused``, and nothing is
written. An override that does clear names the prescriber by id AND name in its
audit entry, so the log reads as a person and not a number.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from fastapi import HTTPException

from permissions import MEDICATION_PRESCRIBE, has_permission
from services import drug_safety_service
from services.audit_service import log_audit_event
from services.drug_safety_service import check_medication_safety
from database import db_session

logger = logging.getLogger("orb.medication_safety_gate")


class MedicationSafetyBlocked(Exception):
    """A non-interactive path hit a severe (blocking) alert or a failed screen."""

    def __init__(self, safety: dict, reason: str):
        self.safety = safety
        self.reason = reason
        super().__init__(reason)


def _audit(
    *,
    outcome: str,
    action_type: str,
    patient_id: str,
    drug_name: str,
    actor_id: Optional[str],
    entry_point: str,
    safety: dict,
    ack_reason: Optional[str] = None,
    prescriber: Optional[dict] = None,
) -> None:
    """Audit the screen/override on the hash chain. Clinical-safety events are
    fail-open for auditing (the audit middleware still records the mutation) —
    a logging failure must not block care — but we log loudly if it happens."""
    dose_check = safety.get("dose_check")
    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="clinical_safety",
            action_type=action_type,
            outcome=outcome,
            summary_message=(
                f"Drug-safety screen for {drug_name}: max_severity={safety.get('max_severity')}, "
                f"alerts={len(safety.get('allergy_alerts', [])) + len(safety.get('interaction_alerts', []))}, "
                f"interaction_screened={safety.get('interaction_screened')}, entry={entry_point}"
                + (f", dose_check={dose_check.get('decision')}" if dose_check else "")
                + (f", override_reason={ack_reason}" if ack_reason else "")
            ),
            actor_user_id=actor_id,
            target_type="medication",
            target_id=drug_name,
            patient_id=patient_id,
            metadata={
                "entry_point": entry_point,
                "max_severity": safety.get("max_severity"),
                "blocking": safety.get("blocking"),
                "interaction_screened": safety.get("interaction_screened"),
                "allergy_alerts": safety.get("allergy_alerts", []),
                "interaction_alerts": safety.get("interaction_alerts", []),
                "dose_check": dose_check,
                "ack_refused": safety.get("ack_refused"),
                "override_reason": ack_reason,
                # Only on an override: who cleared it, by id and by name. For an
                # allergy or dose block this is always a prescriber.
                **({"override_by": prescriber} if prescriber else {}),
            },
        )
    except Exception as e:  # pragma: no cover — audit must not block care
        logger.error("[SafetyGate] audit write failed for %s/%s at %s: %s",
                     patient_id, drug_name, entry_point, e)


def screen_order_dose(drug_name: str, dose: Optional[str],
                      route: Optional[str] = None) -> Optional[dict]:
    """The dose guard for ONE ordered dose, or None when no dose was given.

    Same shape and the same honesty rule as order_sets_service._dose_screen: the
    hospital formulary resolves a brand to its ingredient first, and a guard
    that could not run says so as an ``unavailable`` WARN rather than passing
    silently. It does not block on its own fault, because an internal error in
    this code is not evidence about the patient.
    """
    if not (dose or "").strip():
        return None
    check = {
        "decision": drug_safety_service.DOSE_ALLOW,
        "rule": None,
        "dose_given": dose,
        "route_given": route,
        "parsed_dose": None,
        "findings": [],
        "unavailable": False,
        "unavailable_reason": None,
    }
    ingredients: list[str] = []
    try:
        from database import db_session
        from services import allergy_interlock
        with db_session() as db:
            ingredients = allergy_interlock.resolve_ingredients(db, drug_name)
    except Exception as e:                    # pragma: no cover (defensive)
        logger.warning("[SafetyGate] formulary resolution failed for %s: %s", drug_name, e)
    try:
        verdict = drug_safety_service.evaluate_dose(drug_name, dose, route, ingredients)
    except Exception as e:
        logger.error("[SafetyGate] dose evaluation FAILED for %s (%s): %s", drug_name, dose, e)
        check["decision"] = drug_safety_service.DOSE_WARN
        check["unavailable"] = True
        check["unavailable_reason"] = "the dose check could not run for this order. It has NOT been checked"
        return check
    check.update({
        "decision": verdict["decision"],
        "rule": verdict["rule"],
        "parsed_dose": verdict["parsed_dose"],
        "findings": verdict["findings"],
    })
    if verdict["decision"] == drug_safety_service.DOSE_BLOCK:
        check["message"] = " ".join(
            f["message"] for f in verdict["findings"]
            if f.get("decision") == drug_safety_service.DOSE_BLOCK
        )
    return check


def _dose_ack_refusal(actor_role: Optional[str], ack_reason: str) -> Optional[str]:
    """Why a written acknowledgement may NOT clear a dose block, or None if it may.

    The permission, never the role name (permissions.MEDICATION_PRESCRIBE's note).
    """
    role = (actor_role or "").strip().lower()
    if not role or not has_permission(role, MEDICATION_PRESCRIBE):
        return ("Only a prescriber can authorise a dose the dose check stopped. "
                "Change the dose, or ask the prescriber.")
    try:
        drug_safety_service.validate_dose_override_reason(ack_reason)
    except ValueError as e:
        return str(e)
    return None


ALLERGY_ACK_REFUSAL = ("Only a prescriber can override an allergy block. "
                       "Choose another medicine, or ask the prescriber.")


def _severe_allergy(safety: dict) -> bool:
    """True when a SEVERE allergy alert is one of the reasons this order blocks."""
    return any((a or {}).get("severity") == "severe"
               for a in (safety.get("allergy_alerts") or []))


def _allergy_ack_refusal(actor_role: Optional[str]) -> Optional[str]:
    """Why an acknowledgement may NOT clear a severe allergy block, or None.

    The permission, never the role name, as for the dose block. A caller that
    names no role can never clear one.
    """
    role = (actor_role or "").strip().lower()
    if not role or not has_permission(role, MEDICATION_PRESCRIBE):
        return ALLERGY_ACK_REFUSAL
    return None


def _prescriber_override_on_file(patient_id: str, prescriber_id: Optional[str],
                                 allergy_ids: set) -> Optional[dict]:
    """The prescriber's own audited override that already covers these allergies.

    For the pharmacy steps on an order that already exists (verify, dispense):
    the pharmacist is not overriding anything when the prescriber who wrote the
    order already overrode this same allergy for this same patient, on the
    chain. Returns that audit row's id and time, or None. ``prescriber_id``
    must still hold ``medication.prescribe`` today, and every allergy blocking
    now must appear, as severe, in that one override.
    """
    if not prescriber_id or not allergy_ids:
        return None
    try:
        with db_session() as db:
            user = db.execute("SELECT role FROM users WHERE id = ?",
                              (prescriber_id,)).fetchone()
            if not user or not has_permission((user["role"] or "").lower(),
                                              MEDICATION_PRESCRIBE):
                return None
            rows = db.execute(
                "SELECT id, timestamp, metadata_json FROM audit_logs "
                "WHERE action_type = 'drug_safety_override' AND patient_id = ? "
                "AND actor_user_id = ? ORDER BY id DESC LIMIT 50",
                (patient_id, prescriber_id),
            ).fetchall()
    except Exception as e:  # a failed read carries nothing forward
        logger.warning("[SafetyGate] override lookup failed for %s: %s", patient_id, e)
        return None
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            continue
        covered = {a.get("allergy_id") for a in (meta.get("allergy_alerts") or [])
                   if isinstance(a, dict) and a.get("severity") == "severe"}
        if allergy_ids <= covered:
            return {"audit_id": row["id"], "at": row["timestamp"],
                    "prescriber_id": prescriber_id}
    return None


def _prescriber(actor_id: Optional[str], actor_role: Optional[str]) -> dict:
    """Who cleared a block, by id, display name and role.

    The name is read from users at override time; a missing user or a failed
    read leaves the name None rather than blocking care.
    """
    name = None
    if actor_id:
        try:
            with db_session() as db:
                row = db.execute("SELECT name FROM users WHERE id = ?",
                                 (actor_id,)).fetchone()
            if row:
                name = row["name"]
        except Exception as e:  # pragma: no cover, audit detail only
            logger.warning("[SafetyGate] could not read the name of %s: %s", actor_id, e)
    return {"user_id": actor_id, "display_name": name, "role": actor_role}


def enforce_medication_safety(
    patient_id: str,
    drug_name: str,
    *,
    actor_id: Optional[str],
    entry_point: str,
    safety_ack: Optional[dict[str, Any]] = None,
    interactive: bool = True,
    dose: Optional[str] = None,
    route: Optional[str] = None,
    actor_role: Optional[str] = None,
    order_prescriber_id: Optional[str] = None,
) -> dict:
    """Screen ``drug_name`` for ``patient_id`` and enforce the gate.

    Returns the safety payload (include it in the API response so the UI can
    render alerts + the not-screened notice). Raises HTTPException(428) when an
    interactive caller must acknowledge severe alerts, HTTPException(503) when
    the screen itself failed for an interactive caller, and
    MedicationSafetyBlocked for non-interactive callers in either case.

    ``dose`` / ``route`` switch on the dose guard (see the module note);
    ``actor_role`` is what decides whether a written reason may clear a dose
    block. A caller that omits the role can never clear one.

    ``order_prescriber_id`` is passed only by the pharmacy steps on an order
    that already exists. A severe allergy block there clears for a
    non-prescriber only when that prescriber's own override of the same
    allergies is already on the audit chain (``_prescriber_override_on_file``):
    the pharmacist then confirms a prescriber's decision rather than making one.
    """
    try:
        safety = check_medication_safety(patient_id, drug_name)
    except Exception as e:
        logger.error("[SafetyGate] screen failed for %s/%s at %s: %s",
                     patient_id, drug_name, entry_point, e)
        failed = {
            "drug": drug_name,
            "allergy_alerts": [],
            "interaction_alerts": [],
            "max_severity": "unknown",
            "blocking": True,
            "safe": False,
            "interaction_screened": False,
            "screen_failed": True,
            "coverage_note": "Drug-safety screening is unavailable: do not assume this order was checked.",
        }
        _audit(outcome="failure", action_type="drug_safety_screen", patient_id=patient_id,
               drug_name=drug_name, actor_id=actor_id, entry_point=entry_point, safety=failed)
        if not interactive:
            raise MedicationSafetyBlocked(failed, "drug-safety screen unavailable")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "drug_safety_screen_unavailable",
                "message": "The drug-safety screen could not run. Resolve the fault or use the pharmacist verification path.",
                "safety": failed,
            },
        )

    # The dose guard, beside the allergy screen and never instead of it.
    dose_check = screen_order_dose(drug_name, dose, route) if dose is not None else None
    dose_blocked = bool(dose_check) and dose_check["decision"] == drug_safety_service.DOSE_BLOCK
    if dose_check:
        summary = drug_safety_service.summarize_dose_check(dose_check)
        # Named by the SERVER, as the bedside refusal names it, so no screen
        # hard-codes who may clear it.
        summary["override_requires_permission"] = MEDICATION_PRESCRIBE if dose_blocked else None
        safety = {**safety, "dose_check": summary}
    if dose_blocked:
        safety = {**safety, "blocking": True, "safe": False}
    # Which audit verb: a block the dose guard alone raised is a dose-guard
    # event; anything the allergy/interaction screen raised keeps its own name.
    dose_only = dose_blocked and safety.get("max_severity") != "severe"
    block_verb = "dose_guard_block" if dose_only else "drug_safety_block"
    override_verb = "dose_guard_override" if dose_only else "drug_safety_override"

    if safety["blocking"]:
        ack_reason = str((safety_ack or {}).get("reason") or "").strip()
        acknowledged = bool(safety_ack) and bool(ack_reason) and bool((safety_ack or {}).get("acknowledged"))
        ack_refused = None
        allergy_blocked = _severe_allergy(safety)
        if allergy_blocked:
            # Named by the SERVER, as the dose block names it, so no screen
            # hard-codes who may clear an allergy block.
            safety = {**safety, "override_requires_permission": MEDICATION_PRESCRIBE}
        confirms = None
        if acknowledged and allergy_blocked:
            # A severe allergy clears only for a prescriber, in every lane.
            ack_refused = _allergy_ack_refusal(actor_role)
            if ack_refused and order_prescriber_id:
                confirms = _prescriber_override_on_file(
                    patient_id, order_prescriber_id,
                    {a.get("allergy_id") for a in safety.get("allergy_alerts") or []
                     if a.get("severity") == "severe"})
                if confirms:
                    ack_refused = None
            if ack_refused:
                acknowledged = False
                safety = {**safety, "ack_refused": ack_refused}
        if acknowledged and dose_blocked:
            # A dose block needs more than a non-empty reason: a prescriber, and
            # a reason that says why THIS dose is right.
            ack_refused = _dose_ack_refusal(actor_role, ack_reason)
            if ack_refused:
                acknowledged = False
                safety = {**safety, "ack_refused": ack_refused}
        if not interactive:
            _audit(outcome="denied", action_type=block_verb, patient_id=patient_id,
                   drug_name=drug_name, actor_id=actor_id, entry_point=entry_point, safety=safety)
            raise MedicationSafetyBlocked(
                safety, "dose guard block on a non-interactive path" if dose_only
                else "severe drug-safety alert on a non-interactive path")
        if not acknowledged:
            _audit(outcome="denied", action_type=block_verb, patient_id=patient_id,
                   drug_name=drug_name, actor_id=actor_id, entry_point=entry_point, safety=safety)
            raise HTTPException(
                status_code=428,
                detail={
                    "error": "drug_safety_acknowledgement_required",
                    "message": (
                        f"The dose check stopped this order. {dose_check.get('message') or ''}".strip()
                        if dose_blocked else
                        "Severe drug-safety alerts must be acknowledged with a reason before this order proceeds."
                    ),
                    "safety": safety,
                },
            )
        prescriber = _prescriber(actor_id, actor_role)
        if confirms:
            prescriber["confirms_prescriber_override"] = confirms
        _audit(outcome="partial_success", action_type=override_verb, patient_id=patient_id,
               drug_name=drug_name, actor_id=actor_id, entry_point=entry_point, safety=safety,
               ack_reason=ack_reason, prescriber=prescriber)
        override = {"by": actor_id, "by_name": prescriber["display_name"], "reason": ack_reason}
        safety = {**safety, "override": override}
        if dose_blocked:
            safety["dose_check"] = {**safety["dose_check"], "override": override}
        return safety

    _audit(outcome="success", action_type="drug_safety_screen", patient_id=patient_id,
           drug_name=drug_name, actor_id=actor_id, entry_point=entry_point, safety=safety)
    return safety
