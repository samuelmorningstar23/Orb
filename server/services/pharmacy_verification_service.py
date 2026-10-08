"""Orb Hospital OS — Pharmacist Verification Service.

Closes the loop on AI / agentic medications. When Sage (or any agentic pathway)
charts a medication it does so with provenance ``source='llm_extracted'`` (or
``'agentic'``) and a placeholder dose of ``'REVIEW REQUIRED'`` — the drug is on
the chart but explicitly UNVERIFIED (``verified_by IS NULL``). A pharmacist must
review each one: correct the dose/route/frequency and then either

  * VERIFY  → the med becomes an active, human-verified order, or
  * REJECT  → the med is discontinued with the rejection reason recorded.

Every verify/reject re-runs the order-time drug-safety screen
(``drug_safety_service.check_medication_safety``) so the pharmacist sees any
allergy / interaction alerts at the moment of the decision.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from database import db_session
from services.audit_service import log_audit_event
from services.drug_safety_service import check_medication_safety

logger = logging.getLogger("orb.pharmacy_verification")

# Provenance values that require pharmacist verification before the med is trusted.
_UNVERIFIED_SOURCES = ("llm_extracted", "agentic")

INCOMPLETE_ORDER_MESSAGE = "Enter a dose, route and frequency before verifying"
STOPPED_ON_CHART_MESSAGE = "This medication was stopped on the chart"
# Refusals the Verify Queue prints as they are, so they are written for the
# pharmacist: no row ids, no source codes, no login ids.
NOT_ON_CHART_MESSAGE = "This medication is no longer on the chart. Refresh the queue."
NOT_SAGE_CHARTED_MESSAGE = "This medication was charted by a clinician, so it does not need a pharmacist check here."
ALREADY_DECIDED_MESSAGE = "Another pharmacist has already checked this medication. Refresh the queue."


class IncompleteOrderError(ValueError):
    """Verify was attempted while a dose, route or frequency is blank or still
    the AI placeholder. The route answers 422."""


def _is_placeholder(value) -> bool:
    """Blank, or the charting placeholder ('REVIEW REQUIRED', route 'Review').

    Verifying used to keep the stored value when the pharmacist left a field
    empty, so 'REVIEW REQUIRED' became the verified dose of an active order.
    """
    text = (value or "").strip()
    return not text or "review" in text.lower()


def _corrected(value, stored):
    """The value the order will carry: the pharmacist's entry, else the chart's."""
    text = (value or "").strip()
    return text if text else stored


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safety_alerts(patient_id: str, drug_name: str, med_id: int | None = None) -> dict:
    """Run the order-time safety screen, failing CLOSED on errors.

    Returns the full safety dict plus a flattened ``alerts`` list so callers can
    surface allergy + interaction warnings uniformly. Never raises — the queue
    must stay visible — but a failed screen is reported as ``screen_failed`` with
    max_severity 'unknown', NOT as "no alerts": an unscreened med must never look
    identical to a clean one.

    ``med_id`` is the row under review: unverified AI meds are charted active,
    so without excluding it the drug is screened against itself and every
    anticoagulant (say) raises a false severe duplicate-therapy alert.
    """
    try:
        safety = check_medication_safety(patient_id, drug_name, exclude_med_id=med_id)
    except Exception as e:
        logger.warning("drug-safety check failed for '%s' / %s: %s", drug_name, patient_id, e)
        return {"drug": drug_name, "allergy_alerts": [], "interaction_alerts": [],
                "max_severity": "unknown", "blocking": True, "safe": False,
                "screen_failed": True, "interaction_screened": False,
                "coverage_note": "Drug-safety screening unavailable: verify manually.",
                "alerts": []}
    safety["alerts"] = list(safety.get("allergy_alerts", [])) + list(safety.get("interaction_alerts", []))
    safety["screen_failed"] = False
    return safety


def _actor_role(user_id: str | None) -> str | None:
    """The acting account's role, read from its user row (the route passes only
    the id). None for an unknown account, which can clear nothing."""
    if not user_id:
        return None
    try:
        with db_session() as db:
            r = db.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        return r["role"] if r else None
    except Exception:                           # pragma: no cover (defensive)
        return None


def _override_by(user_id: str | None) -> dict:
    """{user_id, display_name, role} of whoever cleared a block, read from
    users now; the name and role are None when the user row is gone."""
    name = role = None
    if user_id:
        try:
            with db_session() as db:
                r = db.execute("SELECT name, role FROM users WHERE id = ?", (user_id,)).fetchone()
            if r:
                name, role = r["name"], r["role"]
        except Exception:                       # pragma: no cover (audit detail only)
            pass
    return {"user_id": user_id, "display_name": name, "role": role}


def _with_dose_check(safety: dict, drug_name: str, dose, route) -> dict:
    """The safety screen plus the dose guard's verdict on the dose that will be
    verified. Its findings join ``alerts``, the list the verify queue already
    renders, so a refused dose is shown in the queue's own words."""
    from permissions import MEDICATION_PRESCRIBE
    from services import drug_safety_service
    from services.medication_safety_gate import screen_order_dose
    if _is_placeholder(dose):
        return safety
    check = screen_order_dose(drug_name, dose, route)
    if not check:
        return safety
    summary = drug_safety_service.summarize_dose_check(check)
    blocked = check["decision"] == drug_safety_service.DOSE_BLOCK
    summary["override_requires_permission"] = MEDICATION_PRESCRIBE if blocked else None
    extra = [
        {"type": "dose check",
         "severity": "severe" if f.get("decision") == drug_safety_service.DOSE_BLOCK else "moderate",
         "message": f.get("message")}
        for f in check.get("findings") or [] if f.get("message")
    ]
    if check.get("unavailable"):
        extra.append({"type": "dose check", "severity": "moderate",
                      "message": "The dose check could not run. This dose has NOT been checked."})
    out = {**safety, "dose_check": summary, "alerts": list(safety.get("alerts") or []) + extra}
    if blocked:
        out["blocking"] = True
        out["safe"] = False
    return out


def _serialize_med(row) -> dict:
    """Map a medications row to the pharmacist-facing shape."""
    d = dict(row)
    return {
        "id": d["id"],
        "patient_id": d.get("patient_id"),
        "patient": d.get("patient_name"),
        "drug_name": d.get("drug_name"),
        "dose": d.get("dosage"),
        "route": d.get("route"),
        "frequency": d.get("frequency"),
        "source": d.get("source"),
        "status": d.get("status"),
        "verified_by": d.get("verified_by"),
        "verified_at": d.get("verified_at"),
        "notes": d.get("notes"),
        # Where the patient is, who wrote the order (the prescriber, or the
        # author of the note Sage read it from) and when it was charted: the
        # pharmacist cannot check an order, or ring anyone about it, without them.
        "ward": d.get("ward"),
        "bed": d.get("bed"),
        "prescriber": d.get("prescriber_name"),
        "started_at": d.get("started_at"),
        # The people, by name, read from users now (the row keeps only ids).
        # None when the user row is gone, never a guess.
        "ordered_by_name": d.get("prescriber_name"),
        "verified_by_name": d.get("verified_by_name"),
        # Provenance (migration 79): the signed note the medicine was read
        # from, who signed it and when, and the sentence that names the drug.
        # All None for a medicine that did not come from a note.
        "source_note_id": d.get("source_note_id"),
        "source_sentence": d.get("source_sentence"),
        "source_note_signed_by": d.get("source_note_signer"),
        "source_note_signed_at": d.get("source_note_signed_at"),
    }


def list_pending_verification() -> list[dict]:
    """All AI/agentic-sourced meds awaiting pharmacist verification.

    A med is pending when its provenance is llm_extracted/agentic AND no
    pharmacist has verified it yet (``verified_by IS NULL``) AND it is still
    active on the chart of an admitted patient: a drug a doctor has stopped, or
    one for a patient who has gone home, is not an order awaiting review. Each
    row is joined to the patient name and annotated with any drug-safety alerts
    so the queue can render them in-line.
    """
    with db_session() as db:
        rows = db.execute(
            """SELECT m.id, m.patient_id, m.drug_name, m.dosage, m.route, m.frequency,
                      m.status, m.source, m.verified_by, m.verified_at, m.notes,
                      m.started_at, p.name AS patient_name, p.ward, p.bed,
                      u.name AS prescriber_name, v.name AS verified_by_name,
                      m.source_note_id, m.source_sentence,
                      sn_u.name AS source_note_signer,
                      COALESCE(sn.signed_at, sn.created_at) AS source_note_signed_at
               FROM medications m
               JOIN patients p ON p.id = m.patient_id AND p.status = 'admitted'
               LEFT JOIN users u ON u.id = m.prescribed_by
               LEFT JOIN users v ON v.id = m.verified_by
               LEFT JOIN scribe_notes sn ON sn.id = m.source_note_id
               LEFT JOIN users sn_u ON sn_u.id = COALESCE(sn.signed_by, sn.author_id)
               WHERE m.source IN ({placeholders})
                 AND m.verified_by IS NULL
                 AND m.status = 'active'
               ORDER BY m.started_at DESC, m.id DESC""".format(
                placeholders=",".join("?" * len(_UNVERIFIED_SOURCES))
            ),
            _UNVERIFIED_SOURCES,
        ).fetchall()

    pending = []
    for r in rows:
        item = _serialize_med(r)
        safety = _safety_alerts(item["patient_id"], item["drug_name"], item["id"])
        item["safety_alerts"] = safety["alerts"]
        item["max_severity"] = safety["max_severity"]
        # Coverage honesty: the queue must distinguish "screened and clean" from
        # "not screened" (drug outside the interaction KB) and "screen failed".
        item["interaction_screened"] = safety.get("interaction_screened", False)
        item["coverage_note"] = safety.get("coverage_note")
        item["screen_failed"] = bool(safety.get("screen_failed"))
        pending.append(item)
    return pending


def source_note(med_id: int) -> dict | None:
    """The signed note a queue medicine was read from, for the pharmacist.

    None when the medicine is unknown, was not charted from a note, or the note
    is gone. Only the note's words (``services.note_text``), who signed it and
    when, and the sentence that names the drug: not the raw transcript.
    """
    from services.note_text import note_plain_text
    with db_session() as db:
        row = db.execute(
            """SELECT m.id, m.patient_id, m.source_note_id, m.source_sentence,
                      sn.structured_note, sn.raw_transcript,
                      COALESCE(sn.signed_at, sn.created_at) AS signed_at,
                      su.name AS signed_by
               FROM medications m
               JOIN scribe_notes sn ON sn.id = m.source_note_id
               LEFT JOIN users su ON su.id = COALESCE(sn.signed_by, sn.author_id)
               WHERE m.id = ?""",
            (med_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "medication_id": row["id"],
        "patient_id": row["patient_id"],
        "note_id": row["source_note_id"],
        "signed_by": row["signed_by"],
        "signed_at": row["signed_at"],
        "sentence": row["source_sentence"],
        "text": note_plain_text(row["structured_note"]) or "",
    }


def _fetch_med(db, med_id: int):
    return db.execute(
        """SELECT m.*, p.name AS patient_name,
                  u.name AS prescriber_name, v.name AS verified_by_name,
                  sn_u.name AS source_note_signer,
                  COALESCE(sn.signed_at, sn.created_at) AS source_note_signed_at
           FROM medications m
           LEFT JOIN patients p ON p.id = m.patient_id
           LEFT JOIN users u ON u.id = m.prescribed_by
           LEFT JOIN users v ON v.id = m.verified_by
           LEFT JOIN scribe_notes sn ON sn.id = m.source_note_id
           LEFT JOIN users sn_u ON sn_u.id = COALESCE(sn.signed_by, sn.author_id)
           WHERE m.id = ?""",
        (med_id,),
    ).fetchone()


def _mirror_into_order_store(db, med_id: int, decision: str, pharmacist_id: str, *,
                             reason: str | None = None, decided_at: str | None = None) -> None:
    """Invariant I2 of the medication-order store (medorders-3, W5-E): when the
    ward row just verified or rejected is LINKED to a medication order, the
    order moves in this same transaction, whatever the store switches say
    (services/medorders/chart_lane.mirror_legacy_decision_in_txn). An unlinked
    row is untouched. A refusal from the store undoes this decision and reaches
    the pharmacist as a sentence (the route's 400), because the bedside check
    follows the link and the two must never disagree."""
    from services.medorders import chart_lane, core as medorder_core
    try:
        chart_lane.mirror_legacy_decision_in_txn(
            db, medication_id=med_id, decision=decision, actor_id=pharmacist_id,
            reason=reason, decided_at=decided_at)
    except medorder_core.MedOrderError as exc:
        raise ValueError(exc.message) from exc


def verify_medication(med_id: int, pharmacist_id: str, dosage: str | None = None,
                      route: str | None = None, frequency: str | None = None,
                      ack_reason: str | None = None) -> dict:
    """Pharmacist verifies an AI/agentic med, optionally correcting the order.

    Optionally overwrites dosage/route/frequency (the pharmacist's correction of
    the ``REVIEW REQUIRED`` placeholder), then stamps ``verified_by`` /
    ``verified_at``. Guards: only an existing, still-unverified med of AI/agentic
    provenance may be verified (a second attempt raises); it must still be active
    on the chart (verifying never revives a drug a doctor stopped, so the status
    is not written here); and the resulting dose, route and frequency must all be
    real values, not blank or a placeholder (:class:`IncompleteOrderError`).

    Safety gate (fail closed): the drug-safety screen is re-run BEFORE the med is
    verified. A severe alert (or a failed screen) blocks verification unless
    the pharmacist supplies ``ack_reason``; the block is raised as a ValueError
    with the machine-readable prefix ``severe_ack_required:`` (the ``.safety``
    attribute carries the screen). An acknowledged override is written to the
    tamper-evident audit chain.

    Returns the updated med plus the drug-safety screen.
    """
    with db_session() as db:
        row = _fetch_med(db, med_id)
        if row is None:
            raise ValueError(NOT_ON_CHART_MESSAGE)
        if row["source"] not in _UNVERIFIED_SOURCES:
            raise ValueError(NOT_SAGE_CHARTED_MESSAGE)
        if row["verified_by"] is not None:
            raise ValueError(ALREADY_DECIDED_MESSAGE)
        if row["status"] != "active":
            raise ValueError(STOPPED_ON_CHART_MESSAGE)
        if any(_is_placeholder(_corrected(value, row[column]))
               for value, column in ((dosage, "dosage"), (route, "route"),
                                     (frequency, "frequency"))):
            raise IncompleteOrderError(INCOMPLETE_ORDER_MESSAGE)

    # Screen outside the session (check_medication_safety opens its own), and
    # BEFORE verification so a severe/failed screen can stop it.
    safety = _safety_alerts(row["patient_id"], row["drug_name"], med_id)
    ack_reason = (ack_reason or "").strip()
    safety = _with_dose_check(safety, row["drug_name"],
                              _corrected(dosage, row["dosage"]), _corrected(route, row["route"]))
    dose_check = safety.get("dose_check")
    dose_blocked = bool(dose_check) and dose_check.get("decision") == "block"
    if dose_blocked:
        # THE PHARMACIST'S CORRECTED DOSE GOES THROUGH THE DOSE GUARD (2026-10-03).
        # It used to be stored unchecked, so enoxaparin 400 mg verified with no
        # warning. Decided BEFORE the allergy branch below, so a refused dose
        # never leaves an allergy-override audit row for a verification that
        # did not happen. A pharmacist holds no medication.prescribe, so the
        # way on is a corrected dose or a rejection, not a typed reason.
        from services.medication_safety_gate import _dose_ack_refusal
        refusal = _dose_ack_refusal(_actor_role(pharmacist_id), ack_reason)
        if refusal:
            log_audit_event(
                correlation_id=str(uuid.uuid4()),
                event_category="clinical_safety",
                action_type="pharmacy_verify_dose_block",
                outcome="denied",
                summary_message=(
                    f"Dose check stopped verification of medication {med_id} "
                    f"({row['drug_name']} {dose_check.get('dose_given')})"
                ),
                actor_user_id=pharmacist_id,
                target_type="medication",
                target_id=str(med_id),
                patient_id=row["patient_id"],
                metadata={"dose_check": dose_check, "ack_refused": refusal if ack_reason else None},
            )
            err = ValueError(
                f"severe_ack_required: The dose check stopped {row['drug_name']} "
                f"{dose_check.get('dose_given')}. {dose_check.get('message') or ''} {refusal}"
            )
            err.safety = {**safety, "ack_refused": refusal if ack_reason else None}
            raise err
    severe_allergy = any((a or {}).get("severity") == "severe"
                         for a in safety.get("allergy_alerts") or [])
    if severe_allergy:
        # Named by the server so the queue offers a reason box only to a
        # prescriber, as at the bedside and at order time.
        safety = {**safety, "override_requires_permission": "medication.prescribe"}
    if severe_allergy and ack_reason:
        # A SEVERE ALLERGY CLEARS ONLY FOR A PRESCRIBER (2026-10-06), here as in
        # every other lane. A pharmacist holds no medication.prescribe, so the
        # way on is a rejection and a call to the prescriber, who can order the
        # drug with their own override. Decided before the override branch
        # below, so a refused attempt leaves no override row.
        from services.medication_safety_gate import _allergy_ack_refusal
        refusal = _allergy_ack_refusal(_actor_role(pharmacist_id))
        if refusal:
            log_audit_event(
                correlation_id=str(uuid.uuid4()),
                event_category="clinical_safety",
                action_type="pharmacy_verify_allergy_block",
                outcome="denied",
                summary_message=(
                    f"Allergy block stopped verification of medication {med_id} "
                    f"({row['drug_name']}): the acknowledgement was not a prescriber's"
                ),
                actor_user_id=pharmacist_id,
                target_type="medication",
                target_id=str(med_id),
                patient_id=row["patient_id"],
                metadata={"alerts": safety.get("allergy_alerts", []),
                          "ack_refused": refusal,
                          "attempted_reason": ack_reason[:500]},
            )
            err = ValueError(
                f"severe_ack_required: {row['drug_name']} is stopped by a severe allergy. "
                f"{refusal}"
            )
            err.safety = {**safety, "ack_refused": refusal}
            raise err
    needs_ack = safety["max_severity"] == "severe" or safety.get("screen_failed")
    if needs_ack:
        cause = ("the drug-safety screen is unavailable" if safety.get("screen_failed")
                 else "severe drug-safety alerts were found")
        if not ack_reason:
            err = ValueError(
                f"severe_ack_required: {cause[0].upper()}{cause[1:]} for {row['drug_name']}. "
                f"Give a reason to verify it anyway."
            )
            err.safety = safety  # the route surfaces this as the 428 payload
            raise err
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="clinical_safety",
            action_type="pharmacy_verify_override",
            outcome="partial_success",
            summary_message=(
                f"Pharmacist {pharmacist_id} verified medication {med_id} "
                f"({row['drug_name']}) although {cause}: {ack_reason}"
            ),
            actor_user_id=pharmacist_id,
            target_type="medication",
            target_id=str(med_id),
            patient_id=row["patient_id"],
            metadata={
                "max_severity": safety.get("max_severity"),
                "screen_failed": bool(safety.get("screen_failed")),
                "alerts": safety.get("alerts", []),
                "reason": ack_reason,
                # Who cleared it, by id and by name, as the order lanes record
                # it. For a severe allergy this is always a prescriber.
                "override_by": _override_by(pharmacist_id),
            },
        )
        safety = {**safety, "override": {"by": pharmacist_id, "reason": ack_reason}}

    if dose_blocked:
        # Reached only by an actor holding medication.prescribe with a reason
        # the dose guard accepts (a site that grants its clinical pharmacists
        # prescribing authority). Audited like the bedside dose override.
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="clinical_safety",
            action_type="pharmacy_verify_dose_override",
            outcome="partial_success",
            summary_message=(
                f"{pharmacist_id} verified medication {med_id} ({row['drug_name']} "
                f"{dose_check.get('dose_given')}) across a dose-check block: {ack_reason}"
            ),
            actor_user_id=pharmacist_id,
            target_type="medication",
            target_id=str(med_id),
            patient_id=row["patient_id"],
            metadata={"dose_check": dose_check, "reason": ack_reason},
        )
        safety = {**safety, "dose_check": {**dose_check,
                                           "override": {"by": pharmacist_id, "reason": ack_reason}}}

    with db_session() as db:
        # Re-guard inside the write session: another pharmacist may have verified
        # (or an admin purge deleted the med) while the screen — or the
        # acknowledgement round-trip — was in flight.
        row = _fetch_med(db, med_id)
        if row is None:
            raise ValueError(NOT_ON_CHART_MESSAGE)
        if row["verified_by"] is not None:
            raise ValueError(ALREADY_DECIDED_MESSAGE)
        if row["status"] != "active":
            raise ValueError(STOPPED_ON_CHART_MESSAGE)

        new_dosage = _corrected(dosage, row["dosage"])
        new_route = _corrected(route, row["route"])
        new_frequency = _corrected(frequency, row["frequency"])
        if any(_is_placeholder(v) for v in (new_dosage, new_route, new_frequency)):
            raise IncompleteOrderError(INCOMPLETE_ORDER_MESSAGE)

        stamp = _now()
        cur = db.execute(
            """UPDATE medications
               SET dosage = ?, route = ?, frequency = ?,
                   verified_by = ?, verified_at = ?
               WHERE id = ? AND verified_by IS NULL AND status = 'active'""",
            (new_dosage, new_route, new_frequency, pharmacist_id, stamp, med_id),
        )
        if cur.rowcount == 1:
            _mirror_into_order_store(db, med_id, "verified", pharmacist_id, decided_at=stamp)
        updated = _fetch_med(db, med_id)

    med = _serialize_med(updated)
    logger.info("Pharmacist %s VERIFIED med %s (%s)", pharmacist_id, med_id, med["drug_name"])
    return {
        "status": "verified",
        "medication": med,
        "safety": safety,
        "safety_alerts": safety["alerts"],
    }


def reject_medication(med_id: int, pharmacist_id: str, reason: str) -> dict:
    """Pharmacist rejects an AI/agentic med — discontinue it and record why.

    Sets status='discontinued', stamps verified_by/verified_at (the decision was
    made by this pharmacist) and appends the rejection reason to notes. Guard:
    only a still-unverified AI/agentic med may be rejected.

    Returns the updated med plus the re-run drug-safety screen (the alerts that
    likely motivated the rejection).
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Give a reason for rejecting this medication.")

    with db_session() as db:
        row = _fetch_med(db, med_id)
        if row is None:
            raise ValueError(NOT_ON_CHART_MESSAGE)
        if row["source"] not in _UNVERIFIED_SOURCES:
            raise ValueError(NOT_SAGE_CHARTED_MESSAGE)
        if row["verified_by"] is not None:
            raise ValueError(ALREADY_DECIDED_MESSAGE)

        stamp = _now()
        prior_notes = row["notes"] or ""
        # The pharmacist's NAME, read on this session: a printed list showed a
        # login id. The time stays UTC ISO in the stored note (one spelling on
        # disk); every screen that prints it renders it in local clinical form.
        who = db.execute("SELECT name FROM users WHERE id = ?", (pharmacist_id,)).fetchone()
        who_name = (who["name"] if who and who["name"] else None) or pharmacist_id
        rejection_note = f"[REJECTED by {who_name} @ {stamp}] {reason}"
        new_notes = f"{prior_notes}\n{rejection_note}".strip() if prior_notes else rejection_note

        cur = db.execute(
            """UPDATE medications
               SET status = 'discontinued', verified_by = ?, verified_at = ?, notes = ?
               WHERE id = ? AND verified_by IS NULL""",
            (pharmacist_id, stamp, new_notes, med_id),
        )
        if cur.rowcount == 1:
            _mirror_into_order_store(db, med_id, "rejected", pharmacist_id, reason=reason,
                                     decided_at=stamp)
        updated = _fetch_med(db, med_id)

    med = _serialize_med(updated)
    safety = _safety_alerts(med["patient_id"], med["drug_name"], med_id)
    logger.info("Pharmacist %s REJECTED med %s (%s): %s", pharmacist_id, med_id, med["drug_name"], reason)
    return {
        "status": "rejected",
        "medication": med,
        "reason": reason,
        "safety": safety,
        "safety_alerts": safety["alerts"],
    }
