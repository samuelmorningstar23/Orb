"""Orb — Allergy Interlock at Medication Administration.

THE LAST LINE OF DEFENCE. Every other bedside guard already fires in
``emar_service.record_administration`` — the order must be active, an
AI-extracted med must carry a pharmacist's verification, a withheld dose needs a
reason, the drug name must be typed back. Nothing checked the patient's
ALLERGIES at the moment the drug went in. This module is that check.

Why here as well as at order time
---------------------------------
Order-time screening already exists (``medication_safety_gate`` on every write
path, and again at ``pharmacy_verification_service``) and is the RIGHT place to
catch most of this: it is early, it is cheap, and it stops the wrong drug
reaching the chart at all. It is also, on its own, insufficient — not because it
is badly built but because of what it is: a point-in-time screen of a mutable
fact.

  1. Allergy lists change AFTER the order. The admitting antibiotic is ordered
     in ED at 02:00; the patient's daughter arrives at 09:00 and reports the
     penicillin reaction; the allergy is charted then. The order was screened
     against an allergy list that did not yet contain the allergy. Only a check
     at administration sees the list as it is when the drug is given.
  2. Not every order goes through the gate. Downtime mode, an HIS/FHIR
     ingestion, a migration, a direct fix — the chart is reachable by paths the
     order-time gate does not own. eMAR is the single write path for a dose, so
     it is the one place a check cannot be routed around.
  3. The prescriber's override is not the nurse's decision. A doctor
     acknowledging "amoxicillin despite penicillin label" at 02:00 does not
     transfer to whoever draws it up at 06:00.

So: all three. This module adds the third and, deliberately, does not weaken the
other two.

Alert fatigue is a patient-safety hazard, not a UX complaint
------------------------------------------------------------
An interlock that fires on everything is worse than no interlock, because staff
learn to clear it reflexively and then clear the real one. Three things keep the
firing rate honest:

  * ONE vocabulary. Matching runs through ``drug_safety_service.match_allergies``
    — the same class map, the same cross-reactivity pairs the order-time gate
    uses. No second drug ontology. Brand names are resolved to ingredients
    through the hospital's OWN formulary (``helix_medication_master``), so
    "Monocef" is screened as ceftriaxone rather than as an unknown word.
  * Graded response. Only a same-drug/same-class match with a severe (or
    unknown-and-not-obviously-trivial) history is a HARD STOP. A mild rash
    history, or a documented intolerance recorded without a severity, is a
    warning that is recorded and does not stop the dose.
  * Order-scoped override. Once a clinician has typed a reason for THIS order
    against THIS allergy picture, later doses of the same order do not re-prompt
    — the decision is carried forward and re-recorded on every dose. If the
    allergy picture changes (new allergy, escalated severity, changed reaction)
    the fingerprint changes and the interlock RE-ARMS.

Who may clear it (2026-08-15)
-----------------------------
A block over a CHARTED allergy is cleared by a PRESCRIBER and by nobody else —
an actor holding ``permissions.MEDICATION_PRESCRIBE``, which in the shipped role
table is the doctor alone. Deciding to give a drug across a documented allergy
(a desensitisation step, an allergy de-labelled after formal testing, a
risk/benefit call in an emergency) is a prescribing decision, and the nurse at
the bedside is the last INDEPENDENT check on it. Letting her clear it would not
add a safeguard, it would delete one: two checks would become one person
agreeing with themselves. She sees the block, sees the allergen and severity,
and sees who can authorise it — she is never shown an override control.

Two deliberate seams in that rule, both of them narrower than they look:

  * A CARRIED-FORWARD override is not a new decision, so any administering user
    may act on one. The prescriber's reason was typed against this order and
    this allergy picture; a QDS course is 28 doses and demanding the prescriber
    at the bedside for all 28 would either stop the treatment or push the ward
    onto paper. Every carried dose is still audited as given despite the
    interlock, and any change to the allergy picture re-arms it. What carries is
    strictly a PRESCRIBER's decision: ``find_carried_override`` checks the role
    stamped on the ledger row, so a row this rule does not describe — authored by
    someone without prescribing authority, or too old to prove who authored it —
    never becomes a standing authorisation.
  * A FAILED screen (the allergy list could not be read) is not a charted
    allergy being overruled — there is nothing in front of anyone to overrule,
    only an outage. Restricting that to a prescriber would let an infrastructure
    fault stop a nurse giving adrenaline in a peri-arrest, which is the larger
    hazard. It stays clearable by any administering user and is audited with
    ``screen_failed: true`` so review can count it.

Fail closed
-----------
If the allergy data cannot be read, the screen does NOT return "no allergies".
It returns ``screen_failed`` with decision ``block``, which is overridable only
with the same explicit typed reason a real alert needs. A total unoverridable
block would be its own hazard (you must still be able to give adrenaline during
a partial outage) — but nothing proceeds *silently*.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Iterable, Optional

from permissions import MEDICATION_PRESCRIBE, has_permission
from services.drug_safety_service import _norm, effective_severity, match_allergies

logger = logging.getLogger("orb.allergy_interlock")

# ── Who may clear a charted allergy ──────────────────────────────────────────
# THE PERMISSION, NOT THE ROLE NAME. `role == "doctor"` would have been shorter
# and would have drifted the first time a site added a nurse-prescriber or a
# registrar role, silently and in the unsafe direction (a role the string test
# does not know is refused; a role added to the doctor family without the grant
# is admitted). permissions.MEDICATION_PRESCRIBE is the thing being asked about,
# so it is the thing that is asked.
#
# Deliberately NOT `medication.write`: nurse and doctor both hold that (a nurse
# must be able to record a dose), so gating on it is the same as not gating.
OVERRIDE_PERMISSION = MEDICATION_PRESCRIBE

# ── Decisions ────────────────────────────────────────────────────────────────
ALLOW = "allow"    # nothing matched — proceed, recorded
WARN = "warn"      # matched, but not at a grade that should stop the dose
BLOCK = "block"    # hard stop; proceeds only with an explicit typed reason

_DECISION_RANK = {ALLOW: 0, WARN: 1, BLOCK: 2}

# A typed reason means a sentence, not a keystroke. "ok" / "y" / "." must not
# clear an anaphylaxis interlock, and a length floor is the cheapest honest
# barrier against a reflex clear-through.
MIN_OVERRIDE_REASON_CHARS = 10
_JUNK_REASONS = {
    "ok", "okay", "yes", "y", "n/a", "na", "none", "fine", "done", "test",
    "asdf", "aaaa", "override", "no reason", "as discussed", "per doctor",
    "as per doctor", "doctor said", "verbal order", "-", ".", "..", "...",
}

# ── Reaction-text policy ─────────────────────────────────────────────────────
# Lives in drug_safety_service (effective_severity and its three expressions)
# since 2026-10-03, so the order-time gate and this interlock read ONE policy
# and the order screen can never grade an allergy below what blocks here.


class AllergyInterlockBlocked(Exception):
    """A dose was stopped by the allergy interlock (or by an unreadable screen).

    Carries ``.screen`` — the full payload — so the route can hand the caller
    everything it needs to display the alert and re-submit with a reason.
    """

    def __init__(self, screen: dict, message: str):
        self.screen = screen
        super().__init__(message)


class AllergyOverrideNotPermitted(AllergyInterlockBlocked):
    """This actor may not clear a charted allergy — only a prescriber may.

    A SUBCLASS ON PURPOSE. Every existing caller catches
    :class:`AllergyInterlockBlocked` and stops the dose; if this were a sibling
    exception, a caller that had not been taught about it would let the dose
    through. Inheriting means the failure mode of forgetting this class is "the
    dose is blocked and the message is less specific", never "the dose
    proceeds". Callers that want the sharper answer catch this FIRST — see
    routes/emar_routes.py, which turns it into a 403 rather than the 428 that
    means "you may do this, but say why".
    """

    def __init__(self, screen: dict, message: str, *, actor_id=None, actor_role=None):
        self.actor_id = actor_id
        self.actor_role = actor_role
        self.required_permission = OVERRIDE_PERMISSION
        super().__init__(screen, message)


def override_authority(db, actor_id: Optional[str]) -> dict:
    """May ``actor_id`` clear a charted allergy? Read from the USER ROW.

    The authority is derived from the actor's persisted role and the central
    permission map, never from anything the request carried — a caller that can
    name its own authority has none. Every write path into eMAR (the route,
    downtime replay, an import) therefore gets the same answer, because they all
    pass the same actor id.

    FAIL CLOSED in both directions that matter: a user row that cannot be read,
    an unknown user, an unknown role and a missing actor all resolve to "may
    not". The cost of that is a refused dose with a clear message; the cost of
    the opposite is an unauthorised clear-through of an anaphylaxis interlock.
    """
    role = None
    if actor_id:
        try:
            row = db.execute("SELECT role FROM users WHERE id = ?", (actor_id,)).fetchone()
            if row is not None:
                role = (row["role"] if not isinstance(row, tuple) else row[0]) or None
        except Exception as e:
            logger.error("[AllergyInterlock] could not read actor %s for override "
                         "authority: %s", actor_id, e)
            role = None
    permitted = bool(role) and has_permission(str(role).strip().lower(), OVERRIDE_PERMISSION)
    return {
        "actor_id": actor_id,
        "actor_role": role,
        "permitted": permitted,
        "required_permission": OVERRIDE_PERMISSION,
    }


# ── Formulary resolution (reuse the hospital's own vocabulary) ───────────────
def resolve_ingredients_bulk(db, drug_names: Iterable[str]) -> dict[str, list[str]]:
    """Map each charted drug name → the ingredient/generic names it denotes.

    Reads ``helix_medication_master`` — the hospital's OWN catalogue, already
    used by Helix ordering and the Sage formulary lexicon — rather than
    inventing a second drug dictionary. A charted "Monocef 1g" resolves to
    "Ceftriaxone", which the shared class map then places in `cephalosporins`;
    without this the brand name classifies as nothing at all and a
    penicillin-allergic patient's cross-reactivity alert never fires.

    ONE query for the whole set: this runs on the ward board across every
    patient × every medication, so a per-drug lookup would be an N+1.

    A missing/empty catalogue is not an error — it yields no extra names and the
    screen falls back to matching the charted name alone.
    """
    wanted = {_norm(n) for n in drug_names if _norm(n)}
    out: dict[str, list[str]] = {n: [] for n in wanted}
    if not wanted:
        return out
    try:
        rows = db.execute(
            "SELECT name, generic_name FROM helix_medication_master"
        ).fetchall()
    except Exception as e:
        # The formulary is an ENRICHMENT, not the allergy source. Losing it
        # degrades brand-name resolution; it does not blind the screen, and it
        # must not take the whole interlock down. The caller still sees this in
        # `formulary_resolved`.
        logger.warning("[AllergyInterlock] formulary lookup failed: %s", e)
        return out

    catalogue = []
    for r in rows:
        cat_name = _norm(r["name"] if not isinstance(r, tuple) else r[0])
        generic = _norm(r["generic_name"] if not isinstance(r, tuple) else r[1])
        if cat_name:
            catalogue.append((cat_name, generic))

    for want in wanted:
        head = want.split()[0] if want.split() else want
        found: list[str] = []
        for cat_name, generic in catalogue:
            hit = (
                cat_name == want
                or generic == want
                or cat_name.startswith(want)
                or want.startswith(cat_name)
                # First-token prefix catches "Monocef" ↔ "Monocef 1g Injection".
                # Guarded at 4 chars: a short head token would prefix-match a
                # large arbitrary slice of the catalogue.
                or (len(head) >= 4 and cat_name.startswith(head))
            )
            if hit and generic and generic not in found:
                found.append(generic)
        out[want] = found
    return out


def resolve_ingredients(db, drug_name: str) -> list[str]:
    """Single-drug convenience wrapper over :func:`resolve_ingredients_bulk`."""
    return resolve_ingredients_bulk(db, [drug_name]).get(_norm(drug_name), [])


# ── Severity policy ──────────────────────────────────────────────────────────
# effective_severity is imported from drug_safety_service (see the
# reaction-text policy note above); decide() below is the bedside half.


def decide(alert: dict) -> tuple[str, str, str]:
    """(decision, severity, basis) for one matched alert.

    * direct / same-class + severe  → BLOCK. This is "give the drug the patient
      reacts to", the case the interlock exists for.
    * cross-reactivity + severe     → BLOCK. ~1–3% of penicillin-allergic
      patients react to a cephalosporin; with an anaphylaxis history that is a
      stop-and-decide, not a footnote.
    * everything else               → WARN. Recorded on the administration and
      audited, but a mild rash history does not stop a dose at 03:00.
    """
    severity, basis = effective_severity(alert)
    if severity == "severe":
        return BLOCK, severity, basis
    return WARN, severity, basis


def _fingerprint(drug_key: str, alerts: list[dict]) -> str:
    """Stable identity of "this drug against this allergy picture".

    An override is carried forward only while the fingerprint holds. Adding an
    allergy, escalating a severity, or editing a reaction into anaphylaxis
    territory all change it — which RE-ARMS the interlock rather than letting a
    stale acknowledgement cover a new hazard.
    """
    parts = sorted(
        "|".join([
            str(a.get("allergy_id")),
            str(a.get("substance")),
            str(a.get("match")),
            str(a.get("effective_severity")),
            str(a.get("decision")),
        ])
        for a in alerts
    )
    raw = "\x1f".join([drug_key, *parts])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ── The screen ───────────────────────────────────────────────────────────────
def evaluate(
    allergies: list[dict],
    drug_name: str,
    ingredients: Iterable[str] = (),
) -> dict:
    """PURE screen — no DB, no I/O. Given the patient's active allergies and the
    drug (plus any resolved ingredient names), decide.

    Pure on purpose: the ward board screens every medication of every patient on
    a refresh, and doing that with a query per medication is how a board stops
    being refreshed.
    """
    ingredients = [i for i in ingredients if i]
    alerts = match_allergies(allergies, drug_name, tuple(ingredients))

    decision = ALLOW
    for a in alerts:
        d, sev, basis = decide(a)
        a["decision"] = d
        a["effective_severity"] = sev
        a["severity_basis"] = basis
        if _DECISION_RANK[d] > _DECISION_RANK[decision]:
            decision = d

    # Allergen records too short to be matched safely (see
    # drug_safety_service._MIN_SUBSTRING_MATCH). Neither matched nor silently
    # dropped: reported, so a nurse sees "there is an allergy record this system
    # could not interpret" instead of a clean screen it did not earn.
    matched_ids = {a.get("allergy_id") for a in alerts}
    uninterpreted = [
        {"allergy_id": a.get("id"), "substance": a.get("substance"),
         "reason": "allergen record too short to match safely"}
        for a in allergies
        if a.get("id") not in matched_ids and len(_norm(a.get("substance") or "")) < 4
    ]

    drug_key = "+".join([_norm(drug_name), *sorted(_norm(i) for i in ingredients)])
    return {
        "drug": drug_name,
        "ingredients": list(ingredients),
        "decision": decision,
        "alerts": alerts,
        "blocking_alerts": [a for a in alerts if a["decision"] == BLOCK],
        "uninterpreted_allergies": uninterpreted,
        # Coverage honesty, mirroring check_medication_safety's
        # `interaction_screened`: zero alerts against zero recorded allergies is
        # NOT the same statement as zero alerts against a documented list. A
        # patient with no allergy row may be NKDA or may never have been asked,
        # and Orb cannot currently tell those apart (see module notes).
        "allergies_on_file": len(allergies),
        "screen_failed": False,
        "fingerprint": _fingerprint(drug_key, alerts),
        "override": None,
    }


def failed_screen(drug_name: str, error: str) -> dict:
    """The payload for "the allergy data could not be read".

    Decision is BLOCK and ``screen_failed`` is True — an unreadable allergy list
    must never render as a clean one.
    """
    return {
        "drug": drug_name,
        "ingredients": [],
        "decision": BLOCK,
        "alerts": [],
        "blocking_alerts": [],
        "uninterpreted_allergies": [],
        "allergies_on_file": None,
        "screen_failed": True,
        "screen_error": error,
        "fingerprint": None,   # never carried forward: an unreadable screen is
                               # not a decision anyone made about a known risk
        "override": None,
        "message": (
            "Allergy data could not be read. This dose has NOT been screened. "
            "Do not assume the patient has no allergies."
        ),
    }


def read_active_allergies(db, patient_id: str) -> list[dict]:
    """The patient's active allergy list on the caller's connection.

    Its own function so the fail-closed path above it is testable for real (a
    fault injected here travels the same route a disk/decryption/schema failure
    would) rather than only through a stubbed screen.
    """
    rows = db.execute(
        "SELECT id, substance, substance_class, reaction, severity "
        "FROM patient_allergies WHERE patient_id = ? AND status = 'active'",
        (patient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def screen(db, patient_id: str, drug_name: str) -> dict:
    """Screen one drug for one patient on the caller's open connection.

    Fail-closed: any failure reading the allergy list yields a ``screen_failed``
    BLOCK, never a clean pass.
    """
    try:
        allergies = read_active_allergies(db, patient_id)
    except Exception as e:
        logger.error("[AllergyInterlock] allergy read FAILED for %s / %s: %s",
                     patient_id, drug_name, e)
        return failed_screen(drug_name, f"{type(e).__name__}: {e}")

    ingredients = resolve_ingredients(db, drug_name)
    result = evaluate(allergies, drug_name, ingredients)
    result["patient_id"] = patient_id
    return result


def find_carried_override(db, med_id: int, fingerprint: Optional[str]) -> Optional[dict]:
    """The most recent PRESCRIBER-AUTHORED override for THIS order against THIS
    allergy picture.

    Returns None when the fingerprint is absent (an unreadable screen is never
    carried), when nothing matches, or when what matches was not authorised by
    someone holding prescribing authority — in which case the interlock is armed.

    WHY THE AUTHORITY IS RE-CHECKED HERE (found in a browser check, 2026-08-15).
    The carried-forward seam is justified by "the PRESCRIBER's reason was typed
    against this order" — but the write gate and the carry lane read the same
    ledger, and only the write gate was checking who was typing. A row authored
    before that gate existed (or by any future path that reaches the table
    without going through ``screen_for_administration``) carried forever and
    cleared the interlock for everyone. Live proof: override id 1 on this dev
    appliance was authored by a NURSE on 2026-08-13, and on 2026-08-15 a nurse
    gave ceftriaxone to a patient with charted anaphylaxis at 200 OK, with no
    prescriber involved at any point. Guarding one lane and not the other is how
    this repo has lost guarantees before.

    The check reads the role STAMPED on the ledger row, not the author's role
    today. "Was this authorised by someone who could authorise it?" is a question
    about the moment of the decision: a prescriber who later moves to an admin
    post does not retroactively invalidate a course they authorised, and a nurse
    who later qualifies as a prescriber does not retroactively validate one she
    could not. `users` is mutable; the stamp is not.

    A row with NO stamped role predates migration 32 and cannot prove its author
    held anything. It does NOT carry. That fails closed in the direction that
    stops a dose: the interlock re-arms and a prescriber types a fresh reason,
    which is a prompt, not a lost treatment.
    """
    if not fingerprint or med_id is None:
        return None
    try:
        row = db.execute(
            """SELECT id, reason, overridden_by, overridden_at, overridden_by_role
                 FROM medication_allergy_overrides
                WHERE medication_id = ? AND alert_fingerprint = ?
                ORDER BY id DESC LIMIT 1""",
            (med_id, fingerprint),
        ).fetchone()
    except Exception as e:
        # Cannot prove an override exists → behave as if none does → the
        # interlock stays armed. Fail-closed in the direction that stops a dose.
        logger.error("[AllergyInterlock] override lookup failed for med %s: %s", med_id, e)
        return None
    if row is None:
        return None
    d = dict(row)
    stamped_role = (d.get("overridden_by_role") or "").strip().lower()
    if not stamped_role or not has_permission(stamped_role, OVERRIDE_PERMISSION):
        logger.warning(
            "[AllergyInterlock] override %s on med %s is not carried: authorised by "
            "%s (role=%r), which does not hold %s. The interlock stays armed.",
            d["id"], med_id, d["overridden_by"], d.get("overridden_by_role"),
            OVERRIDE_PERMISSION,
        )
        return None
    return {
        "source_override_id": d["id"],
        "by": d["overridden_by"],
        "by_role": d["overridden_by_role"],
        "reason": d["reason"],
        "at": d["overridden_at"],
        "carried": True,
    }


def validate_override_reason(reason: Optional[str]) -> str:
    """Return a cleaned typed reason, or raise ValueError.

    Guards against the reflex clear-through: a reason must be at least
    :data:`MIN_OVERRIDE_REASON_CHARS` characters, must not be one of the known
    non-reasons, and must not be a single repeated character.
    """
    cleaned = re.sub(r"\s+", " ", (reason or "").strip())
    if not cleaned:
        raise ValueError("an allergy override requires a typed reason")
    if cleaned.lower() in _JUNK_REASONS:
        raise ValueError(
            "an allergy override requires a clinical reason "
            "(e.g. 'allergy de-labelled by immunology 12/06, challenge tolerated')"
        )
    if len(set(cleaned.replace(" ", ""))) <= 1:
        raise ValueError("an allergy override requires a clinical reason")
    if len(cleaned) < MIN_OVERRIDE_REASON_CHARS:
        raise ValueError(
            f"an allergy override reason must be at least {MIN_OVERRIDE_REASON_CHARS} "
            f"characters describing WHY this dose is safe to give"
        )
    return cleaned


def screen_for_administration(
    db,
    patient_id: str,
    drug_name: str,
    med_id: Optional[int],
    *,
    override_reason: Optional[str] = None,
    actor_id: Optional[str] = None,
) -> dict:
    """Full bedside screen for one dose, including the override decision.

    Never writes. Returns the screen with ``override`` populated when the dose
    may proceed despite a BLOCK; raises :class:`AllergyInterlockBlocked` when it
    may not. ``requires_override_write`` tells the caller a NEW override row must
    be persisted alongside the administration (a carried-forward one is already
    on record).

    ``actor_id`` is WHO IS GIVING THE DOSE, and it decides whether a fresh typed
    reason is even considered. Clearing a charted allergy is a prescribing
    decision (see OVERRIDE_PERMISSION); omitting the actor therefore means "no
    authority", not "unchecked", and a fresh reason from an unauthorised actor
    raises :class:`AllergyOverrideNotPermitted` rather than being silently
    ignored — ignoring it would chart a dose while discarding the reason someone
    typed for it.
    """
    result = screen(db, patient_id, drug_name)
    result["medication_id"] = med_id
    result["requires_override_write"] = False

    if result["decision"] != BLOCK:
        return result

    # Resolved for EVERY blocked dose, not only for the one that raises. It
    # costs one indexed read on a path that is rare by construction, and it puts
    # the actor's role on the ledger row and on the audit event for the carried-
    # forward case too — "this dose was given under a prescriber's carried
    # override, by a nurse" is precisely the sentence a review needs, and it
    # cannot be reconstructed later from a `users` table that has moved on.
    authority = override_authority(db, actor_id)
    result["override_authority"] = authority

    # A fresh typed reason always wins — it is a new, explicit decision. Which
    # is exactly why it has to be an authorised person making it.
    if (override_reason or "").strip():
        # WHOSE DECISION IS BEING OVERRULED decides who may overrule it.
        #
        #   * A charted allergy (blocking_alerts) is a clinical fact recorded by
        #     a clinician, and giving the drug anyway — desensitisation, a
        #     de-labelled allergy, a risk/benefit call in an emergency — is a
        #     PRESCRIBING decision. Prescriber only. The nurse at the bedside is
        #     the last independent check on it; letting her clear it collapses
        #     two checks into one.
        #   * A screen that FAILED (the allergy list could not be read) is not
        #     anyone's clinical decision — there is no charted allergy in front
        #     of us to overrule, only an outage. Refusing that to a nurse would
        #     mean an infrastructure fault could stop her giving adrenaline in a
        #     peri-arrest, which is a bigger hazard than the one being guarded.
        #     So it stays clearable by any administering user, and every
        #     instance is audited with `screen_failed: true` for review.
        if result["blocking_alerts"] and not authority["permitted"]:
            subs = ", ".join(blocking_allergens(result))
            raise AllergyOverrideNotPermitted(
                result,
                f"{drug_name} conflicts with a documented allergy ({subs}) and only a "
                f"prescriber may authorise giving it. This dose has NOT been recorded. "
                f"The prescriber must enter the reason themselves.",
                actor_id=actor_id,
                actor_role=authority["actor_role"],
            )
        try:
            cleaned = validate_override_reason(override_reason)
        except ValueError as e:
            raise AllergyInterlockBlocked(result, str(e)) from None
        result["override"] = {"reason": cleaned, "carried": False}
        result["requires_override_write"] = True
        return result

    # Otherwise: has this exact hazard already been accepted for this order?
    carried = find_carried_override(db, med_id, result.get("fingerprint"))
    if carried:
        result["override"] = carried
        return result

    if result["screen_failed"]:
        raise AllergyInterlockBlocked(
            result,
            "allergy screening is unavailable: this dose has not been checked "
            "against the patient's allergies; a typed reason is required to proceed",
        )
    subs = ", ".join(blocking_allergens(result))
    raise AllergyInterlockBlocked(
        result,
        f"allergy interlock: {drug_name} conflicts with a documented allergy "
        f"({subs}): a prescriber must authorise this dose with a written clinical "
        f"reason before it can be administered",
    )


def record_override(
    db,
    *,
    screen_result: dict,
    med_id: Optional[int],
    patient_id: str,
    actor_id: str,
    administration_id: Optional[int],
) -> Optional[int]:
    """Persist a NEW override row on the caller's connection/transaction.

    Called only when ``requires_override_write`` is set, and deliberately inside
    the SAME transaction as the administration row: the dose and the record of
    why it was allowed commit together or not at all.

    The row is written to be READ COLD, months later, by someone reviewing an
    incident with no access to this conversation: the drug as it was named on
    the dose and the actor's role as it stood at the moment of the decision are
    STAMPED here rather than joined from `medications` / `users`, both of which
    are mutable (migration 32).
    """
    ov = screen_result.get("override") or {}
    if not ov.get("reason"):
        return None
    authority = screen_result.get("override_authority") or {}
    cur = db.execute(
        """INSERT INTO medication_allergy_overrides
               (medication_id, patient_id, administration_id, alert_fingerprint,
                decision, reason, overridden_by, alerts_json, drug_name,
                overridden_by_role)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            med_id,
            patient_id,
            administration_id,
            screen_result.get("fingerprint"),
            screen_result.get("decision"),
            ov["reason"],
            actor_id,
            json.dumps(screen_result.get("alerts") or []),
            screen_result.get("drug"),
            authority.get("actor_role"),
        ),
    )
    return cur.lastrowid


def blocking_allergens(screen_result: dict) -> list[str]:
    """The allergen names that stopped this dose, de-duplicated and sorted.

    One derivation, used by the ledger's audit row and by the refusal audit, so
    "which allergy was overridden" cannot be answered two different ways in two
    different places.
    """
    return sorted({
        str(a.get("substance"))
        for a in (screen_result.get("blocking_alerts") or screen_result.get("alerts") or [])
        if a.get("substance") and a.get("decision") == BLOCK
    })


def summarize(screen_result: dict) -> dict[str, Any]:
    """Compact form stored on the administration row / sent to the ward board.

    The full alert list goes to the audit chain; this is what a UI needs to
    render a banner without re-deriving anything.
    """
    return {
        "decision": screen_result.get("decision"),
        "screen_failed": bool(screen_result.get("screen_failed")),
        "allergies_on_file": screen_result.get("allergies_on_file"),
        "fingerprint": screen_result.get("fingerprint"),
        "ingredients": screen_result.get("ingredients") or [],
        "alerts": [
            {
                "substance": a.get("substance"),
                "reaction": a.get("reaction"),
                "match": a.get("match"),
                "decision": a.get("decision"),
                "effective_severity": a.get("effective_severity"),
                "severity_basis": a.get("severity_basis"),
                "message": a.get("message"),
            }
            for a in (screen_result.get("alerts") or [])
        ],
        "uninterpreted_allergies": screen_result.get("uninterpreted_allergies") or [],
        "override": screen_result.get("override"),
        # WHO CAN CLEAR THIS, stated by the server rather than guessed by each
        # client. A UI that hard-codes "ask a doctor" drifts the day a site
        # licenses nurse prescribers; a UI that renders this field does not.
        # None means "no permission is required to clear it" — which is only
        # ever true of a FAILED screen (an outage, not a charted allergy) and of
        # a dose that was never blocked at all. Derived, never stored as a
        # separate decision, so it cannot disagree with the gate.
        "override_requires_permission": (
            OVERRIDE_PERMISSION
            if (screen_result.get("decision") == BLOCK
                and not screen_result.get("screen_failed"))
            else None
        ),
        "message": screen_result.get("message"),
    }
