"""Orb eMAR — Medication Administration Service.

The bedside half of the medication loop. CPOE / pharmacist verification put a
medication on the chart; eMAR records what actually happened at the bedside —
one row per administration attempt in ``medication_administrations``, either

  * 'given'      → the five rights as confirmed at the bedside (dose/route
                   actually given, defaulted from the order), or
  * 'not_given'  → the dose was withheld, with a mandatory reason
                   (refused / held / NPO / unavailable...).

The record is append-only by design: corrections are new rows, never UPDATEs,
so the administration history stays faithful. Guards keep the loop honest — a
medication must be an active order, an AI/agentic-sourced med that no pharmacist
has verified is NOT administrable, and — since the allergy interlock landed —
the dose is screened against the patient's ACTIVE allergy list at the moment it
is given, not merely at the moment it was ordered. See
services/allergy_interlock.py for why the bedside check exists in addition to
the order-time one, and what it deliberately does and does not stop.

THE DOSE GUARD sits immediately after the allergy interlock and answers the
question nothing answered before it: is this AMOUNT, by this ROUTE, at this
TIME, plausible? A decimal slip on digoxin, insulin or an opioid used to chart
clean, and so did a repeated dose on a rounds handoff. The clinical rules and
the reasoning behind their grading are in services/drug_safety_service.py (the
"DOSE SAFETY" section); this module owns the database half — reading the recent
administration history, the override ledger, and the audit rows.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from database import db_session
from db_backend import DBIntegrityError

from services import allergy_interlock, drug_safety_service
from services.audit_service import log_audit_event
from services.clinical_time import parse_to_utc
from services.drug_safety_service import DoseGuardBlocked
from services.medorders import emar_check
from services.medorders import supply as medorder_supply

logger = logging.getLogger("orb.emar")

# Provenance values that require pharmacist verification before the med may be
# administered (mirrors pharmacy_verification_service._UNVERIFIED_SOURCES).
_UNVERIFIED_SOURCES = ("llm_extracted", "agentic")
_CONFIRMATION_METHODS = ("typed", "barcode")


def _givable_statuses() -> tuple:
    """Ward row statuses a dose may be recorded against: 'active', and 'prn'
    too while ORB_MED_ORDER_LIFECYCLE is on (MO-R3 (a), medorders-9)."""
    from services.medorders import screening

    return ("active", "prn") if screening.prn_givable() else ("active",)


def _fetch_administration(db, admin_id: int):
    return db.execute(
        """SELECT ma.*, m.drug_name, u.name AS administered_by_name
           FROM medication_administrations ma
           JOIN medications m ON m.id = ma.medication_id
           LEFT JOIN users u ON u.id = ma.administered_by
           WHERE ma.id = ?""",
        (admin_id,),
    ).fetchone()


def _hydrate(row) -> dict:
    """Row → API dict, with the stored screens parsed back into objects.

    ``allergy_check`` / ``dose_check`` are None for a dose recorded before the
    respective guard existed (and for 'not_given' rows, which are never
    screened). That NULL is meaningful and is left as None rather than defaulted
    to an empty screen: a dose nobody checked must not read like a dose that came
    back clean.
    """
    d = dict(row)
    for column, key in (("allergy_check_json", "allergy_check"), ("dose_check_json", "dose_check")):
        raw = d.pop(column, None)
        if raw:
            try:
                d[key] = json.loads(raw)
            except (TypeError, ValueError):
                d[key] = {"decision": "unknown", "parse_error": True}
        else:
            d[key] = None
    return d


# ═════════════════════════════════════════════════════════════════════════════
# DOSE GUARD — the database half. The clinical rules are in
# services/drug_safety_service.py; this is history, ledger and audit.
# ═════════════════════════════════════════════════════════════════════════════
def _parse_ts(value) -> datetime | None:
    """A stored administered_at → naive UTC datetime, or None.

    SQLite's CURRENT_TIMESTAMP writes 'YYYY-MM-DD HH:MM:SS' in UTC, which is
    what every production row carries. The other shapes are tolerated because
    rows also arrive from imports and fixtures. None (rather than a guess) when
    it cannot be read — a timestamp we cannot trust must not become an elapsed
    time we then act on.
    """
    try:
        # Explicit offsets describe an instant, not a wall clock to relabel.
        # Bare SQLite/legacy Orb values retain their existing UTC meaning.
        return parse_to_utc(value).replace(tzinfo=None)
    except (ValueError, TypeError, OverflowError):
        return None


def _read_recent_administrations(
    db,
    patient_id: str,
    *,
    lookback_hours: float,
    exclude_idempotency_key: str | None = None,
) -> list[dict]:
    """Doses actually GIVEN to this patient recently, newest first.

    'not_given' rows are excluded at the source: a withheld dose is not a dose,
    and counting one as a duplicate would punish a nurse for charting honestly.

    ``exclude_idempotency_key`` is belt-and-braces against double-counting an
    idempotent retry. record_administration already returns early on a known
    key — long before this runs — so a replay can never reach the duplicate
    check. This keeps that invariant true LOCALLY too, so a future reordering of
    the guards cannot quietly turn a re-sent lost response into a "second dose".

    Scoped by patient, not by medication_id: the dangerous duplicate is almost
    always TWO ORDERS for the same drug (a branded one and a generic one), not
    two presses on one order.

    THE WINDOW IS BOUNDED BY TIME, NOT BY ROW COUNT, and that distinction is the
    whole correctness of the long-interval cases. This first read `LIMIT 400`
    with no time predicate, which silently lost the flagship methotrexate case:
    the matching dose is a WEEK back, and on a busy patient 400 doses of OTHER
    drugs sit between it and now, so it fell off the end of the page and the
    repeat charted clean. Reproduced with 450 intervening doses before the fix.
    A row cap alone can always be exhausted by unrelated drugs; a time predicate
    cannot. The cap remains only as a runaway guard, and because the ordering is
    newest-first it can now only discard rows OLDER than a match we already have.
    """
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=float(lookback_hours) + 1.0)
    # Imported explicit offsets can cross a UTC day boundary, so raw TEXT
    # comparison can discard a recent dose before Python ever normalizes it.
    # Select and order by the actual instant before the row cap; the patient
    # predicate still bounds the scan, and _parse_ts applies exact precision.
    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    rows = db.execute(
        """SELECT ma.id, ma.medication_id, ma.administered_at, ma.administered_by,
                  ma.dose_given, ma.idempotency_key, m.drug_name
             FROM medication_administrations ma
             JOIN medications m ON m.id = ma.medication_id
            WHERE ma.patient_id = ? AND ma.status = 'given'
              AND julianday(ma.administered_at) >= julianday(?)
            ORDER BY julianday(ma.administered_at) DESC, ma.id DESC
            LIMIT 2000""",
        (patient_id, cutoff_text),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if exclude_idempotency_key and d.get("idempotency_key") == exclude_idempotency_key:
            continue
        when = _parse_ts(d.get("administered_at"))
        if when is None or when < cutoff:
            continue
        d["_when"] = when
        out.append(d)
    return out


def _find_carried_dose_override(db, med_id, fingerprint: str | None) -> dict | None:
    """The most recent override for THIS order at THIS dose/route/rule.

    None when there is no fingerprint (an unreadable check is never carried) or
    nothing matches — in which case the guard stays armed. A lookup that FAILS
    also returns None: we cannot prove an override exists, so we behave as if
    none does, which fails in the direction that stops a dose.
    """
    if not fingerprint or med_id is None:
        return None
    try:
        row = db.execute(
            """SELECT id, reason, overridden_by, overridden_at
                 FROM medication_dose_overrides
                WHERE medication_id = ? AND alert_fingerprint = ?
                ORDER BY id DESC LIMIT 1""",
            (med_id, fingerprint),
        ).fetchone()
    except Exception as e:
        logger.error("[DoseGuard] override lookup failed for med %s: %s", med_id, e)
        return None
    if row is None:
        return None
    d = dict(row)
    return {
        "source_override_id": d["id"],
        "by": d["overridden_by"],
        "reason": d["reason"],
        "at": d["overridden_at"],
        "carried": True,
    }


def _screen_dose(
    db,
    *,
    med,
    med_id: int,
    dose_given: str | None,
    route_given: str | None,
    idempotency_key: str | None,
    override_reason: str | None,
) -> dict:
    """Full bedside dose screen for one dose, including the override decision.

    Never writes. Returns the check with ``override`` populated when the dose may
    proceed despite a BLOCK; raises :class:`DoseGuardBlocked` when it may not.

    FAILURE POLICY — this is the one thing this module gets asked to do
    differently from the allergy interlock, and the difference is deliberate.
    The allergy interlock fails CLOSED: an unreadable allergy list becomes a
    block, because "we do not know this patient's allergies" is itself a reason
    to stop and think. A dose check cannot borrow that. An internal fault in
    THIS code is not evidence about the patient at all, and turning every such
    fault into a ward-wide stop would make an outage in a convenience feature
    into a clinical incident.

    But the failure discovered tonight in sage_handoff_service — an unreadable
    allergy screen quietly reported as a clean one — is the outcome that must
    never be repeated. So the answer here is neither silent pass nor silent
    block: the check reports ``unavailable`` with an explicit decision of WARN,
    and that flag rides on the administration row, on the API response and in
    the fail-closed audit metadata. The clinician is told "this dose was NOT
    checked". Nothing proceeds *silently*.
    """
    drug_name = med["drug_name"]
    check: dict = {
        "drug": drug_name,
        "medication_id": med_id,
        "dose_given": dose_given,
        "route_given": route_given,
        "ordered_dose": med["dosage"],
        "ordered_route": med["route"],
        "decision": drug_safety_service.DOSE_ALLOW,
        "rule": None,
        "parsed_dose": None,
        "route_tags": [],
        "findings": [],
        "duplicate": None,
        "unavailable": False,
        "unavailable_reason": None,
        "fingerprint": None,
        "override": None,
        "requires_override_write": False,
    }
    unavailable: list[str] = []

    # The hospital's OWN formulary resolves a brand to its ingredient, so a
    # "Dolo 650" order is screened as paracetamol and a repeat of it against a
    # "Paracetamol" order is seen as the duplicate it is. Same vocabulary the
    # allergy interlock uses — no second drug ontology.
    ingredients: list[str] = []
    try:
        ingredients = allergy_interlock.resolve_ingredients(db, drug_name)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("[DoseGuard] formulary resolution failed for %s: %s", drug_name, e)

    # ── (1) magnitude + never-correct route ──────────────────────────────────
    rule = None
    try:
        magnitude = drug_safety_service.evaluate_dose(
            drug_name,
            dose_given,
            route_given,
            ingredients,
            ordered_dose=med["dosage"],
        )
        check.update(
            {
                "decision": magnitude["decision"],
                "rule": magnitude["rule"],
                "parsed_dose": magnitude["parsed_dose"],
                "route_tags": magnitude["route_tags"],
                "findings": list(magnitude["findings"]),
            }
        )
        rule = drug_safety_service.find_dose_rule(
            [drug_name, *ingredients], frozenset(magnitude["route_tags"])
        )
    except Exception as e:
        logger.error("[DoseGuard] dose evaluation FAILED for %s (%s): %s", drug_name, dose_given, e)
        unavailable.append("dose magnitude check")

    # ── (2) duplicate ────────────────────────────────────────────────────────
    # The window comes from the ORDER's frequency, never a flat constant, with a
    # drug-level floor where the order's own frequency is the thing most likely
    # to be wrong (oral methotrexate — see DOSE_RULES).
    floor = (rule or {}).get("min_interval_hours")
    try:
        window, _basis = drug_safety_service.duplicate_window_hours(med["frequency"], floor)
        priors = _read_recent_administrations(
            db,
            med["patient_id"],
            lookback_hours=window,
            exclude_idempotency_key=idempotency_key,
        )
        # Existing administration timestamps and _parse_ts use naive UTC.
        # Keep that arithmetic contract while avoiding deprecated utcnow().
        now = datetime.now(UTC).replace(tzinfo=None)
        prior_names = _resolve_prior_names(db, priors)
        prior_doses = []
        for p in priors:
            gap = (now - p["_when"]).total_seconds() / 3600.0
            prior_doses.append(
                {
                    "administration_id": p["id"],
                    "medication_id": p["medication_id"],
                    "drug_name": p["drug_name"],
                    "administered_at": p["administered_at"],
                    "administered_by": p["administered_by"],
                    "same_order": p["medication_id"] == med_id,
                    "names": prior_names.get(p["id"], [p["drug_name"]]),
                    "hours_ago": gap,
                }
            )
        duplicate = drug_safety_service.evaluate_duplicate(
            drug_name, ingredients, med["frequency"], prior_doses, floor
        )
        if duplicate:
            check["duplicate"] = duplicate
            check["findings"] = [*check["findings"], duplicate]
            check["decision"] = drug_safety_service.worse_dose_decision(
                check["decision"], duplicate["decision"]
            )
    except Exception as e:
        logger.error(
            "[DoseGuard] duplicate check FAILED for %s / %s: %s", med["patient_id"], drug_name, e
        )
        unavailable.append("duplicate-dose check")

    if unavailable:
        check["unavailable"] = True
        check["unavailable_reason"] = (
            "could not run: " + ", ".join(unavailable) + ". This dose has NOT been fully checked"
        )
        # Surfaced, not swallowed: at least a WARN so the clinician sees it.
        if check["decision"] == drug_safety_service.DOSE_ALLOW:
            check["decision"] = drug_safety_service.DOSE_WARN

    # ── (3) fingerprint + override ───────────────────────────────────────────
    # A dose that MATCHES THE ORDER is a standing clinical decision: if a
    # clinician has justified "this order at this dose" once, re-prompting on
    # every dose of it is how a guard gets typed through without being read
    # (the allergy interlock's carry-forward argument, unchanged).
    #
    # A dose that DEVIATES from the order is not that. It is a per-dose,
    # per-keystroke event, and it is the exact shape of the error this guard
    # exists to catch — so it is never carried, and must be justified afresh
    # every single time. That distinction is baked into the fingerprint, so the
    # two can never be confused for one another.
    matches_order = drug_safety_service._norm(dose_given or "") == drug_safety_service._norm(
        med["dosage"] or ""
    ) and drug_safety_service._norm(route_given or "") == drug_safety_service._norm(
        med["route"] or ""
    )
    check["matches_order"] = matches_order
    if not check["unavailable"]:
        # An unreadable check is not a decision anyone made about a known risk,
        # so it gets no fingerprint and can never be inherited by a later dose.
        check["fingerprint"] = drug_safety_service.dose_fingerprint(
            drug_name,
            _override_scope(check),
            check["parsed_dose"],
            check["route_tags"],
            matches_order,
        )

    if check["decision"] != drug_safety_service.DOSE_BLOCK:
        return check

    check["message"] = " ".join(
        f["message"]
        for f in check["findings"]
        if f.get("decision") == drug_safety_service.DOSE_BLOCK
    )

    # A fresh typed reason always wins — it is a new, explicit decision.
    if (override_reason or "").strip():
        try:
            cleaned = drug_safety_service.validate_dose_override_reason(override_reason)
        except ValueError as e:
            raise DoseGuardBlocked(check, str(e)) from None
        check["override"] = {"reason": cleaned, "carried": False}
        check["requires_override_write"] = True
        return check

    if matches_order:
        carried = _find_carried_dose_override(db, med_id, check.get("fingerprint"))
        if carried:
            check["override"] = carried
            return check

    raise DoseGuardBlocked(check, check["message"] or "dose guard: this dose was stopped")


def _override_scope(check: dict) -> str | None:
    """Everything a typed reason would have to cover, as ONE identity.

    Not simply ``check["rule"]``. A dose can trip a magnitude rule AND a
    never-correct route rule at once (concentrated potassium, at 200 mmol, by IV
    push). Naming only the dose rule would (a) mislabel the ledger row, so
    "show me every KCl push override" misses it, and (b) let a reason typed for
    the AMOUNT be carried onto a later dose that is also being pushed. Both
    blocks are in the identity, so an override covers exactly the hazards that
    were actually shown to the clinician and no others.
    """
    parts = {
        f"{f.get('kind')}:{f.get('rule')}"
        for f in (check.get("findings") or [])
        if f.get("decision") == drug_safety_service.DOSE_BLOCK
    }
    if parts:
        return "|".join(sorted(parts))
    return check.get("rule")


def _resolve_prior_names(db, priors: list[dict]) -> dict:
    """{administration_id: [charted name, *resolved ingredient names]}.

    ONE formulary query for the whole history rather than one per prior dose —
    this runs on every administration, so a per-row lookup would be an N+1 on
    the bedside write path.
    """
    names = {p["drug_name"] for p in priors if p.get("drug_name")}
    try:
        resolved = allergy_interlock.resolve_ingredients_bulk(db, names)
    except Exception:  # pragma: no cover — defensive
        resolved = {}
    out: dict = {}
    for p in priors:
        charted = p.get("drug_name") or ""
        extra = resolved.get(drug_safety_service._norm(charted), [])
        out[p["id"]] = [charted, *extra]
    return out


def _record_dose_override(
    db,
    *,
    check: dict,
    med_id: int | None,
    patient_id: str,
    actor_id: str,
    administration_id: int | None,
) -> int | None:
    """Persist a NEW dose override on the caller's connection/transaction.

    Deliberately inside the SAME transaction as the administration row: the dose
    and the record of why it was allowed commit together or not at all. Same
    contract as allergy_interlock.record_override.
    """
    ov = check.get("override") or {}
    if not ov.get("reason"):
        return None
    cur = db.execute(
        """INSERT INTO medication_dose_overrides
               (medication_id, patient_id, administration_id, alert_fingerprint,
                decision, rule, dose_given, route_given, reason, overridden_by,
                findings_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            med_id,
            patient_id,
            administration_id,
            check.get("fingerprint"),
            check.get("decision"),
            _override_scope(check),
            check.get("dose_given"),
            check.get("route_given"),
            ov["reason"],
            actor_id,
            json.dumps(check.get("findings") or []),
        ),
    )
    return cur.lastrowid


def record_administration(
    med_id: int,
    actor_id: str,
    *,
    status: str,
    dose_given: str | None = None,
    route_given: str | None = None,
    not_given_reason: str | None = None,
    confirmation_method: str = "typed",
    idempotency_key: str | None = None,
    notes: str | None = None,
    allergy_override_reason: str | None = None,
    dose_override_reason: str | None = None,
) -> dict:
    """Record one bedside administration attempt of a charted medication.

    Guards (each raises ValueError):
      * the medication must exist and be an active order (or a PRN one while
        ORB_MED_ORDER_LIFECYCLE is on, ruling MO-R3 (a)),
      * an AI/agentic med (source llm_extracted/agentic) with no pharmacist
        verification is not administrable,
      * with ORB_MED_ORDER_VERIFY_BEFORE_DOSE on, a ward row linked to a
        medication order needs the pharmacist's check before a 'given' dose,
        whatever its source (ruling MO-R1, services/medorders/emar_check.py);
      * a 'not_given' record requires a non-empty reason.

    ALLERGY INTERLOCK (raises AllergyInterlockBlocked, not ValueError — the
    caller must be able to tell "you may not do this" from "you may do this, but
    say why"): a 'given' dose is screened against the patient's active allergies
    at administration time. A same-drug / same-class match with a severe (or
    unknown-and-not-obviously-trivial) history stops the dose; so does an
    unreadable allergy list. Either can proceed only with an explicit typed
    ``allergy_override_reason``, which is written to a dedicated override ledger
    and to the tamper-evident audit chain in the SAME transaction as the dose.
    Lower-grade matches are recorded as warnings and do not stop anything.
    'not_given' is never interlocked — withholding a dose must always be easy.

    DOSE GUARD (raises DoseGuardBlocked, same 428-shaped contract): a 'given'
    dose is checked for an order-of-magnitude error, a never-correct route, and
    a repeat inside a window derived from the ORDER's own frequency. Only the
    first two can stop a dose, and only at thresholds no legitimate indication
    reaches; a high-but-plausible dose and a suspected duplicate are warnings.
    An unknown drug, an unparseable dose and a per-kg dose all pass in silence.
    A block clears with a typed ``dose_override_reason``, written to
    ``medication_dose_overrides`` and the audit chain in the SAME transaction as
    the dose. Runs AFTER the allergy interlock so the existing 428 contract
    keeps precedence, and — like the allergy interlock — never touches
    'not_given'. See services/drug_safety_service.py for the rules and why each
    is graded the way it is.

    A 'given' record defaults dose_given/route_given from the medication row
    (the ordered dose IS the administered dose unless the nurse says otherwise).
    Idempotent: offline replays resend the same idempotency_key, so a key that
    already exists returns the existing record (flagged ``replayed``) instead
    of double-charting a dose. Every fresh record is audited on the
    tamper-evident chain (fail-closed: an unauditable administration raises).
    """
    if status not in ("given", "not_given"):
        raise ValueError(f"status must be 'given' or 'not_given' (got {status!r})")
    if confirmation_method not in _CONFIRMATION_METHODS:
        raise ValueError(
            f"confirmation_method must be one of {list(_CONFIRMATION_METHODS)} (got {confirmation_method!r})"
        )
    not_given_reason = (not_given_reason or "").strip() or None
    if status == "not_given" and not not_given_reason:
        raise ValueError("a 'not_given' administration requires a non-empty reason")

    with db_session() as db:
        med = db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
        if med is None:
            raise ValueError(f"medication {med_id} not found")
        # A drug charted PRN ('as needed') is givable with the lifecycle on
        # (ruling MO-R3 (a), held for Samuel; services/medorders/screening.py),
        # through every check below: the order state, the pharmacist checks,
        # the inpatient guard, the allergy interlock and the dose guard with
        # its repeat window. With it off, as before: every PRN dose is refused,
        # which leaves a PRN dose given off the record and past the interlock.
        if med["status"] not in _givable_statuses():
            raise ValueError(f"medication {med_id} is not an active order (status={med['status']})")
        if med["source"] in _UNVERIFIED_SOURCES and med["verified_by"] is None:
            raise ValueError("unverified medication: pharmacist verification required")

        record_drug_name = med["drug_name"]

        if status == "given":
            dose_given = dose_given or med["dosage"]
            route_given = route_given or med["route"]

        # Idempotent replay is settled BEFORE the allergy interlock AND before
        # the discharged-patient guard below, and that ordering is deliberate. A
        # replay means this dose was already given, already screened and already
        # charted; re-evaluating it can only produce a NEW verdict (the ward
        # charted an allergy, or discharged the patient, in the meantime), and
        # blocking a replay would not un-give the dose — it would only wedge the
        # offline client into retrying a delivery it can never complete. The
        # honest response to "I already gave this" is the existing record.
        if idempotency_key:
            existing = db.execute(
                "SELECT id FROM medication_administrations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                (replay,) = _mark_before_check(
                    [_hydrate(_fetch_administration(db, existing["id"]))],
                    _before_check_administration_ids(db, med["patient_id"]),
                )
                return {**replay, "replayed": True}

        # ── The patient must still be an inpatient ────────────────────────────
        # Found 2026-08-06 by attack: a patient was discharged through
        # /api/vigil/patients/{id}/discharge and enoxaparin 40mg SC was then
        # administered to them, 200 OK. discharge_service flips patients.status
        # and admissions.status but leaves medications.status = 'active', and the
        # only order-state guard here read the MEDICATION. Every board had
        # already dropped the patient — nurse board, Vigil list, roster — so the
        # dose was charted against someone no ward believed it still had.
        #
        # 'given' only. Withholding must always be easy, including tidying up a
        # round after a discharge, so 'not_given' is deliberately unaffected —
        # same principle as the allergy interlock.
        #
        # BELOW the replay check, not above it (verifier, 2026-08-06): sited
        # above, it turned the offline tablet's replay of a dose given BEFORE the
        # discharge into a 422. api/client.ts treats a 4xx replay as a permanent
        # failure, drops the queued item and tells the nurse the sync failed —
        # for a dose that is in fact charted. A replay writes no new row, so
        # nothing this guard exists to prevent can happen on that path.
        if status == "given":
            patient_status = db.execute(
                "SELECT status FROM patients WHERE id = ?", (med["patient_id"],)
            ).fetchone()
            if patient_status is not None and patient_status["status"] not in (
                None,
                "",
                "admitted",
            ):
                raise ValueError(
                    f"patient {med['patient_id']} is not admitted "
                    f"(status={patient_status['status']}): a dose cannot be administered "
                    f"to a discharged patient; record it as 'not_given' with a reason, or "
                    f"re-admit the patient first"
                )

        # ── The medication ORDER must allow the dose (medorders-3) ──────────
        # Only for a ward row LINKED to a medication order, and keyed on that
        # link rather than on the store switches (ruling MO-R9 (a)): a held,
        # unsigned or ended order refuses 'given' (OrderStateRefused, a
        # ValueError, so the route's 422 with these words). 'not_given' is never
        # refused. BELOW the replay check and the discharged-patient guard for
        # the reasons given above them; ABOVE the allergy interlock so a held
        # drug is refused before anyone is asked about an allergy. Read again
        # after the INSERT below, under the write lock. See
        # services/medorders/emar_check.py. An unlinked row skips this entirely.
        if status == "given":
            emar_check.check_dose_in_txn(db, med_id, status)

        # ── Allergy interlock (the last line of defence) ──────────────────────
        # Runs on THIS connection, inside this transaction, immediately before
        # the write — so it sees the allergy list as of the instant of the dose,
        # and so an override row cannot commit without the dose it authorised
        # (or vice versa). Fail-closed: an unreadable allergy list raises rather
        # than passing as "no allergies".
        allergy_screen = None
        if status == "given":
            allergy_screen = allergy_interlock.screen_for_administration(
                db,
                med["patient_id"],
                record_drug_name,
                med_id,
                override_reason=allergy_override_reason,
                # WHO is giving it decides whether a typed reason counts.
                # Clearing a charted allergy is a prescribing decision
                # (AllergyOverrideNotPermitted otherwise); the actor's authority
                # is read from their user row on THIS connection, never taken
                # from the request.
                actor_id=actor_id,
            )
        allergy_json = (
            json.dumps(allergy_interlock.summarize(allergy_screen)) if allergy_screen else None
        )

        # ── Dose guard ────────────────────────────────────────────────────────
        # Same connection, same transaction, immediately before the write, for
        # the same reasons as the allergy interlock: it sees the administration
        # history as of the instant of this dose, and an override row cannot
        # commit without the dose it authorised.
        #
        # AFTER the allergy interlock on purpose. Both raise a 428-shaped
        # exception and only one can be reported at a time; an allergy is the
        # more serious finding and already owns that contract, so it keeps
        # precedence. In the rare case both fire, the nurse clears the allergy
        # first and then sees the dose alert — two prompts, in severity order.
        dose_check = None
        if status == "given":
            dose_check = _screen_dose(
                db,
                med=med,
                med_id=med_id,
                dose_given=dose_given,
                route_given=route_given,
                idempotency_key=idempotency_key,
                override_reason=dose_override_reason,
            )
        dose_json = (
            json.dumps(drug_safety_service.summarize_dose_check(dose_check)) if dose_check else None
        )

        try:
            cur = db.execute(
                """INSERT INTO medication_administrations
                       (medication_id, patient_id, administered_by, status, not_given_reason,
                        dose_given, route_given, confirmation_method, idempotency_key, notes,
                        allergy_check_json, dose_check_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    med_id,
                    med["patient_id"],
                    actor_id,
                    status,
                    not_given_reason,
                    dose_given,
                    route_given,
                    confirmation_method,
                    idempotency_key,
                    notes,
                    allergy_json,
                    dose_json,
                ),
            )
            inserted_id = cur.lastrowid
        except DBIntegrityError:
            # Concurrent replay raced past the pre-INSERT check — return the
            # existing row for this key rather than a duplicate.
            existing = (
                db.execute(
                    "SELECT id FROM medication_administrations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if idempotency_key
                else None
            )
            if not existing:
                raise
            (replay,) = _mark_before_check(
                [_hydrate(_fetch_administration(db, existing["id"]))],
                _before_check_administration_ids(db, med["patient_id"]),
            )
            return {**replay, "replayed": True}

        # The order-state check again, now that this transaction holds the
        # write lock: no hold can commit after this read, and one that
        # committed since the first read refuses here and rolls the dose back.
        # Then the order's bedside event, in this same transaction. Both are
        # no-ops for an unlinked ward row. The row just written is left out of
        # the pharmacist check's dose count (medorders-11), so a first stat
        # dose does not count itself, while a second nurse's 'first' dose,
        # committed before this lock was taken, does.
        linked_order = emar_check.check_dose_in_txn(
            db, med_id, status, exclude_administration_id=inserted_id
        )
        before_check = bool(linked_order and linked_order.get("before_pharmacist_check"))
        # A given dose names the supply row it came from only when exactly one
        # received, unused unit of the order is on the ward; otherwise its
        # event says 'supply not linked' (medorders-5b). Read here, under the
        # write lock, so two doses cannot both take the last unit. None for an
        # unlinked ward row.
        custody = (
            medorder_supply.custody_for_dose_in_txn(db, linked_order) if status == "given" else None
        )
        order_event_id = emar_check.record_bedside_event_in_txn(
            db,
            linked_order,
            medication_id=med_id,
            administration_id=inserted_id,
            administration_status=status,
            actor_id=actor_id,
            not_given_reason=not_given_reason,
            confirmation_method=confirmation_method,
            custody=custody,
        )

        # A NEW override (as opposed to one carried forward from an earlier dose
        # of this order) gets its own ledger row, in this transaction, linked to
        # the administration it authorised. Who overrode what, and why.
        override_id = None
        if allergy_screen and allergy_screen.get("requires_override_write"):
            override_id = allergy_interlock.record_override(
                db,
                screen_result=allergy_screen,
                med_id=med_id,
                patient_id=med["patient_id"],
                actor_id=actor_id,
                administration_id=inserted_id,
            )

        dose_override_id = None
        if dose_check and dose_check.get("requires_override_write"):
            dose_override_id = _record_dose_override(
                db,
                check=dose_check,
                med_id=med_id,
                patient_id=med["patient_id"],
                actor_id=actor_id,
                administration_id=inserted_id,
            )

        record = _hydrate(_fetch_administration(db, inserted_id))

        # Audit in the SAME transaction (log_audit_event supports caller-owned
        # transactions via db= — after our INSERT this connection holds the
        # write lock, so the prev-hash read + audit insert are serialized).
        # clinical_access is fail-closed: the administration row and its audit
        # row commit or roll back TOGETHER, so an audit-write failure can never
        # leave a committed-but-unaudited dose (which an idempotent replay
        # would otherwise return forever without auditing).
        action_type = "medication_administered" if status == "given" else "medication_not_given"
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="clinical_access",
            action_type=action_type,
            outcome="success",
            db=db,
            summary_message=(
                f"Medication {med_id} ({record['drug_name']}) "
                + (
                    f"administered ({dose_given}, {route_given})"
                    if status == "given"
                    else f"NOT given: {not_given_reason}"
                )
                + (" before the pharmacist check" if before_check else "")
                + f" by {actor_id}"
            ),
            actor_user_id=actor_id,
            target_type="medication",
            target_id=str(med_id),
            patient_id=record["patient_id"],
            metadata={
                "administration_id": record["id"],
                "medication_id": med_id,
                "drug_name": record["drug_name"],
                "status": status,
                "dose_given": dose_given,
                "route_given": route_given,
                "not_given_reason": not_given_reason,
                "confirmation_method": confirmation_method,
                # What the allergy interlock saw and decided for THIS dose.
                # Rides on the SAME fail-closed clinical_access event as the
                # administration, so the screen can never be missing from a dose
                # that committed.
                "allergy_check": record.get("allergy_check"),
                # Likewise the dose guard — including, crucially, `unavailable`.
                # A dose the guard could not check is a fact about the record
                # and has to be on the chain, not just in a log line nobody
                # reads.
                "dose_check": record.get("dose_check"),
                # The medication order this ward row is linked to, and the
                # bedside event written for it (medorders-3). Absent for an
                # unlinked row, so its audit row is exactly as before.
                **(
                    {
                        "med_order_id": linked_order["med_order_id"],
                        "med_order_event_id": order_event_id,
                    }
                    if linked_order
                    else {}
                ),
                # The supply row this dose came from, or None with the event
                # saying 'supply not linked' (medorders-5b). Linked rows only.
                **(
                    {"supply_helix_order_id": custody.get("helix_order_id")}
                    if linked_order and custody is not None
                    else {}
                ),
                # A first stat or urgent dose given before the pharmacist's
                # check (ruling MO-R1 (a), medorders-11). Absent otherwise.
                **(
                    {
                        "given_before_pharmacist_check": True,
                        "first_dose_rule": linked_order.get("first_dose_rule")
                        if linked_order is not None
                        else None,
                        "order_priority": linked_order.get("priority")
                        if linked_order is not None
                        else None,
                    }
                    if before_check
                    else {}
                ),
            },
        )

        # An override is a second, separately-searchable clinical_safety event —
        # but written with the fail-closed clinical_access category and inside
        # this transaction, so "the dose committed but the reason for
        # overriding the allergy did not" is not a reachable state.
        if allergy_screen and (
            allergy_screen.get("override")
            or allergy_screen.get("decision") == allergy_interlock.BLOCK
        ):
            ov = allergy_screen.get("override") or {}
            authority = allergy_screen.get("override_authority") or {}
            # The allergen(s) NAMED in the summary line, not only buried in the
            # alert list. An incident review reads summaries first, and "gave X
            # despite the interlock" without saying what the patient is allergic
            # to is a sentence that has to be re-derived to be understood.
            allergens = allergy_interlock.blocking_allergens(allergy_screen)
            log_audit_event(
                correlation_id=str(uuid.uuid4()),
                event_category="clinical_access",
                action_type="medication_allergy_override",
                outcome="partial_success",
                db=db,
                summary_message=(
                    f"{actor_id} administered {record['drug_name']} (medication {med_id}) "
                    f"DESPITE the allergy interlock"
                    + (f", documented allergy: {', '.join(allergens)}" if allergens else "")
                    + (
                        " (allergy screening unavailable)"
                        if allergy_screen.get("screen_failed")
                        else ""
                    )
                    + (
                        " [reason carried forward from an earlier dose of this order]"
                        if ov.get("carried")
                        else ""
                    )
                    + f": {ov.get('reason')}"
                ),
                actor_user_id=actor_id,
                # The authority the override was made WITH, stamped now. Roles
                # are mutable; the role recorded on the event must be the one
                # held at the moment of the decision.
                actor_role=authority.get("actor_role"),
                target_type="medication",
                target_id=str(med_id),
                patient_id=record["patient_id"],
                metadata={
                    "administration_id": record["id"],
                    "medication_id": med_id,
                    "drug_name": record["drug_name"],
                    "allergens": allergens,
                    "override_id": override_id,
                    "carried_forward": bool(ov.get("carried")),
                    "source_override_id": ov.get("source_override_id"),
                    "reason": ov.get("reason"),
                    "authorised_by_role": authority.get("actor_role"),
                    "required_permission": authority.get("required_permission"),
                    "screen_failed": bool(allergy_screen.get("screen_failed")),
                    "fingerprint": allergy_screen.get("fingerprint"),
                    "alerts": allergy_screen.get("alerts") or [],
                },
            )

        # A dose override is its own separately-searchable event, on the same
        # fail-closed category and in the same transaction — "the dose committed
        # but the reason it was allowed did not" is not a reachable state.
        # Written whenever the guard BLOCKED, whether the reason was typed now
        # or carried forward, so the ledger answers "how often is this threshold
        # overridden, and by whom" — the only honest way to tell a threshold
        # that is protecting patients from one that is just in the way.
        if dose_check and dose_check.get("decision") == drug_safety_service.DOSE_BLOCK:
            ov = dose_check.get("override") or {}
            log_audit_event(
                correlation_id=str(uuid.uuid4()),
                event_category="clinical_access",
                action_type="medication_dose_override",
                outcome="partial_success",
                db=db,
                summary_message=(
                    f"{actor_id} administered {record['drug_name']} {dose_given} "
                    f"({route_given}, medication {med_id}) DESPITE the dose guard"
                    + (
                        " [reason carried forward from an earlier dose of this order]"
                        if ov.get("carried")
                        else ""
                    )
                    + f": {ov.get('reason')}"
                ),
                actor_user_id=actor_id,
                target_type="medication",
                target_id=str(med_id),
                patient_id=record["patient_id"],
                metadata={
                    "administration_id": record["id"],
                    "medication_id": med_id,
                    "drug_name": record["drug_name"],
                    "dose_given": dose_given,
                    "route_given": route_given,
                    "ordered_dose": med["dosage"],
                    "override_id": dose_override_id,
                    "carried_forward": bool(ov.get("carried")),
                    "source_override_id": ov.get("source_override_id"),
                    "reason": ov.get("reason"),
                    "rule": dose_check.get("rule"),
                    "fingerprint": dose_check.get("fingerprint"),
                    "matches_order": bool(dose_check.get("matches_order")),
                    "findings": dose_check.get("findings") or [],
                },
            )

    # ── Ward stock used for this dose (grn-17) ───────────────────────────────
    # AFTER the commit above, on purpose: the dose is charted and audited
    # whatever happens here. A given dose of an order given from ward stock
    # takes one unit off the ward store's books in a transaction of its own;
    # any failure there is logged and the dose stands. A replayed dose never
    # gets here (it returned above), and an unlinked ward row is never booked.
    if status == "given" and linked_order:
        _book_ward_stock_use(inserted_id, linked_order, actor_id)

    if dose_check and dose_check.get("decision") != drug_safety_service.DOSE_ALLOW:
        logger.warning(
            "eMAR: DOSE %s on med %s (%s %s %s) for patient %s by %s%s%s",
            dose_check["decision"].upper(),
            med_id,
            record_drug_name,
            dose_given,
            route_given,
            record["patient_id"],
            actor_id,
            " [OVERRIDDEN]" if dose_check.get("override") else "",
            " [CHECK UNAVAILABLE]" if dose_check.get("unavailable") else "",
        )
    if allergy_screen and allergy_screen.get("decision") != allergy_interlock.ALLOW:
        logger.warning(
            "eMAR: ALLERGY %s on med %s (%s) for patient %s by %s%s",
            allergy_screen["decision"].upper(),
            med_id,
            record_drug_name,
            record["patient_id"],
            actor_id,
            " [OVERRIDDEN]" if allergy_screen.get("override") else "",
        )
    logger.info(
        "eMAR: med %s %s by %s (patient %s)", med_id, status, actor_id, record["patient_id"]
    )
    if before_check:
        record["given_before_pharmacist_check"] = True
    return {**record, "replayed": False}


def _book_ward_stock_use(administration_id: int, linked_order: dict, actor_id: str) -> None:
    """Book a given dose's ward stock use (grn-17), after the dose committed.

    Never raises and never changes the dose or its answer: with
    ORB_PHARMACY_WARD_STOCK off it returns before opening anything; otherwise
    services/pharmacy_stock_service decides and writes, in one immediate
    transaction of its own, the consumption movement and its audit row
    together. Anything that goes wrong there (a busy or broken stock ledger, an
    audit outage) rolls that transaction back and is logged here."""
    try:
        from services import pharmacy_stock_service as stock

        if not stock.ward_consumption_on():
            return
        with db_session(immediate=True) as conn:
            outcome = stock.record_ward_consumption_in_txn(
                conn,
                administration_id,
                med_order_id=linked_order.get("med_order_id"),
                actor_id=actor_id,
            )
    except Exception as exc:  # the dose is charted; the stock books must never touch it
        logger.warning(
            "eMAR: administration %s is charted, but its ward stock use was not booked (%s)",
            administration_id,
            getattr(exc, "code", None) or type(exc).__name__,
        )
        return
    if outcome.get("status") == "consumed" and outcome.get("below_zero"):
        logger.warning(
            "eMAR: administration %s was booked against ward stock that the books did not "
            "hold (lot %s now %s): the ward should count this drug",
            administration_id,
            outcome.get("lot_id"),
            outcome.get("balance_after_units"),
        )
    elif outcome.get("status") == "skipped":
        logger.info(
            "eMAR: administration %s took nothing off ward stock: %s",
            administration_id,
            outcome.get("reason_words") or outcome.get("reason"),
        )


def _before_check_administration_ids(db, patient_id: str) -> set[int]:
    """The patient's doses given before the pharmacist's check (ruling MO-R1
    (a), medorders-11), read from the medication orders' bedside events. Empty
    on a database that never ran the order store."""
    found: set[int] = set()
    for row in db.execute(
        "SELECT ref_id, payload_json FROM med_order_events WHERE patient_id = ? "
        "AND kind = 'administered' AND ref_type = 'medication_administration'",
        (patient_id,),
    ).fetchall():
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("before_pharmacist_check") is True:
            try:
                found.add(int(row["ref_id"]))
            except (TypeError, ValueError):
                continue
    return found


def _mark_before_check(records: list[dict], flagged: set[int]) -> list[dict]:
    """Add given_before_pharmacist_check: True to the flagged records only, so
    every other record keeps exactly its old shape."""
    for rec in records:
        if rec.get("id") in flagged:
            rec["given_before_pharmacist_check"] = True
    return records


def list_administrations(patient_id: str) -> list[dict]:
    """All administration records for a patient, newest first, joined to the
    medication (drug_name) and the administering user (administered_by_name).
    A dose given before the pharmacist's check carries
    given_before_pharmacist_check: True (medorders-11); no other record
    changes shape."""
    with db_session() as db:
        rows = db.execute(
            """SELECT ma.*, m.drug_name, u.name AS administered_by_name
               FROM medication_administrations ma
               JOIN medications m ON m.id = ma.medication_id
               LEFT JOIN users u ON u.id = ma.administered_by
               WHERE ma.patient_id = ?
               ORDER BY ma.administered_at DESC, ma.id DESC""",
            (patient_id,),
        ).fetchall()
        flagged = _before_check_administration_ids(db, patient_id) if rows else set()
    return _mark_before_check([_hydrate(r) for r in rows], flagged)


def administration_summary(med_id: int) -> dict:
    """Per-medication chip summary: how many doses were actually given and when
    the last one was. 'not_given' rows are excluded from both figures."""
    with db_session() as db:
        row = db.execute(
            """SELECT COUNT(*) AS count, MAX(administered_at) AS last_given_at
               FROM medication_administrations
               WHERE medication_id = ? AND status = 'given'""",
            (med_id,),
        ).fetchone()
    return {"last_given_at": row["last_given_at"], "count": row["count"]}
