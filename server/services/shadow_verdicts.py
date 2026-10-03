"""Orb — the adjudication verdict vocabulary. ONE definition, imported by both sides.

WHY THIS FILE EXISTS
────────────────────
It exists because the two halves of the false-alert metric were written
independently and never executed against each other. `routes/shadow_routes.py`
accepted ``true_positive`` / ``false_positive`` / ``artefact`` /
``indeterminate``; `services/shadow_scorecard_service.py` classified
``true_alert`` / ``false_alert`` / ``…`` and treated everything else as
UNRECOGNISED. Every verdict a clinician entered through the API was written
durably to `shadow_events`, counted as unrecognised on the way out, and the
metric reported ``state = 'unadjudicated'`` for ever. Six real, blinded,
API-recorded adjudications produced ``adjudicated = 0``.

Nothing raised. Nothing logged. The scorecard simply said, truthfully-looking
and completely falsely, that nobody had adjudicated anything.

So: the vocabulary is defined HERE, once, and both the write side and the read
side import it. There is no second copy to drift.

  * `CANONICAL_VERDICTS` is what the API accepts, what is STORED, and what the
    UI offers. Four values, closed set.
  * `normalise()` maps any historical spelling onto one of those four. It is the
    only place a synonym is resolved.
  * `classify()` maps a verdict onto the arithmetic class the metric uses:
    ``true`` / ``false`` / ``excluded`` / ``unrecognised``.

── The synonym table is a compatibility shim, not a second vocabulary ────────
Rows written before this file existed carry the old spellings and must keep
counting — a verdict a clinician actually gave is evidence, and silently
dropping it is precisely the failure this module was written to end. New writes
are normalised to canonical at the route, so the synonym table only ever has to
serve history. It is append-only in spirit: never remove an entry, or rows that
count today stop counting tomorrow.

── `unrecognised` is a first-class outcome, never a default bucket ───────────
`classify()` returns ``unrecognised`` rather than guessing. An unrecognised
verdict is counted and SURFACED (`detail.unrecognised_verdicts`); it is never
folded into "true" (which would flatter Orb) nor into "false" (which would
punish it). A number nobody chose is worse than a gap somebody can see.

── `artefact` and `indeterminate` are EXCLUDED, and they are not the same ────
Both are excluded from the numerator AND the denominator, but they stay
distinct verdicts: `artefact` says the signal was wrong (a disconnected lead),
`indeterminate` says the record does not settle it. Folding an artefact into
"false positive" blames the model for a monitoring failure; dropping either
silently flatters the rate.
"""
from __future__ import annotations

from typing import Optional

#: The closed set. This is what the API accepts, what is written to
#: `shadow_events.payload_json.verdict`, and what the adjudication UI offers.
#:
#: Chosen over the `true_alert` / `false_alert` spelling for two reasons: it is
#: already the wire vocabulary the shipped client posts, and "false alert"
#: collides with the NAME of the metric — which is exactly the ambiguity that
#: let the headline count episodes while calling them alerts.
CANONICAL_VERDICTS: tuple[str, ...] = (
    "true_positive", "false_positive", "artefact", "indeterminate",
)

#: The arithmetic class each canonical verdict belongs to. `excluded` verdicts
#: leave BOTH terms of the rate.
VERDICT_CLASS: dict[str, str] = {
    "true_positive": "true",
    "false_positive": "false",
    "artefact": "excluded",
    "indeterminate": "excluded",
}

#: What a reader is shown. The scorecard prints these, not the raw token.
VERDICT_LABEL: dict[str, str] = {
    "true_positive": "True positive: a real deterioration",
    "false_positive": "False positive: no deterioration",
    "artefact": "Artefact: the signal was wrong, not the judgement",
    "indeterminate": "Indeterminate: the record does not settle it",
}

#: Historical spellings → canonical. COMPATIBILITY ONLY (see the module
#: docstring): every entry here was a live spelling in this repo before the two
#: vocabularies were unified, and removing one would silently stop counting
#: adjudications that count today.
VERDICT_SYNONYMS: dict[str, str] = {
    # ── were VERDICT_TRUE in shadow_scorecard_service ──
    "true_alert": "true_positive",
    "true": "true_positive",
    "real": "true_positive",
    "real_deterioration": "true_positive",
    "clinically_significant": "true_positive",
    "significant": "true_positive",
    # ── were VERDICT_FALSE ──
    "false_alert": "false_positive",
    "false": "false_positive",
    "not_significant": "false_positive",
    "no_deterioration": "false_positive",
    "nuisance": "false_positive",
    # ── were VERDICT_EXCLUDED; split back onto the two distinct verdicts ──
    "artifact": "artefact",          # US spelling
    "unknown": "indeterminate",
    "cannot_assess": "indeterminate",
    "uncertain": "indeterminate",
}

#: Everything `normalise()` will accept, canonical first. Used verbatim in the
#: route's 422 so an operator is told what to type, not merely what was wrong.
ACCEPTED_VERDICTS: tuple[str, ...] = CANONICAL_VERDICTS + tuple(
    sorted(VERDICT_SYNONYMS)
)


def _key(raw: object) -> str:
    """Free-text tolerance on INPUT only: case, surrounding space, and the
    hyphen/space spellings of the same word. Nothing semantic happens here."""
    return (
        str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    )


def normalise(raw: object) -> Optional[str]:
    """Any accepted spelling → its canonical verdict. `None` if unrecognised.

    `None` is deliberate and must not be softened into a default: a verdict
    nobody in this table recognises is a data-quality fact the scorecard has to
    report, not a value to guess at.
    """
    key = _key(raw)
    if key in VERDICT_CLASS:
        return key
    return VERDICT_SYNONYMS.get(key)


def classify(raw: object) -> str:
    """Verdict → `true` | `false` | `excluded` | `unrecognised`.

    The ONLY function the false-alert arithmetic is allowed to ask.
    """
    canonical = normalise(raw)
    if canonical is None:
        return "unrecognised"
    return VERDICT_CLASS[canonical]


def label(raw: object) -> Optional[str]:
    """Human-readable form of a verdict, or `None` if it is not one of ours."""
    canonical = normalise(raw)
    return VERDICT_LABEL.get(canonical) if canonical else None


def is_canonical(raw: object) -> bool:
    return _key(raw) in VERDICT_CLASS
