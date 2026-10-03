"""Orb Hospital OS — Shadow-mode scorecard, adjudication and ground-truth API.

The read + human-entry surface of the pilot instrument. The WRITE path for
machine-generated events lives in `services/shadow_service.py`; the metric maths
lives in `services/shadow_scorecard_service.py`; this file is transport, access
control and blinding, and holds no clinical logic of its own.

    GET  /api/admin/shadow/scorecard            the four metrics + instrument health
    GET  /api/admin/shadow/episodes             adjudication queue (BLINDED)
    GET  /api/admin/shadow/episode/{id}         ordered episode detail (unblinded)
    POST /api/admin/shadow/adjudication         record a clinician verdict
    POST /api/admin/shadow/outcome              ground-truth clinical outcome
    POST /api/admin/shadow/outcome/none         per-shift "no deterioration"
    GET  /api/admin/shadow/outcome/shift-status what this shift still owes
    GET  /api/admin/shadow/export/scorecard     de-identified aggregate export

── TWO PERMISSIONS, NOT ONE ──────────────────────────────────────────────────
Everything here was `admin.overview` until the physician champion answered
Decision 2 (docs/OUTCOME_CAPTURE.md §4): **the charge nurse records outcomes,
once per shift.** So the three GROUND-TRUTH ENTRY endpoints — record an event,
confirm a shift had none, and read what this shift still owes — now require
`outcome.record`, held by nurse, doctor and admin.

Everything else stays `admin.overview`, and that separation is deliberate rather
than incidental: the people generating the pilot's ground truth must not be able
to watch the scorecard move as they enter it. A nurse who could see the
false-alert rate rise while recording an outcome has been handed an incentive to
record differently. Same reason the adjudication queue is blinded — and the same
reason the nurse endpoints are WRITE-ONLY apart from "has this shift been
confirmed yet", which reveals nothing about how Orb is scoring.

── CANONICAL PATHS: the mismatch, and how it was settled ─────────────────────
The shipped client (src/api/scorecard.ts) called `/api/admin/scorecard` with
`/api/shadow/scorecard` as a 404 fallback. The design specified
`/api/admin/shadow/*`. Both cannot be right and an alias is not a decision, it
is a decision deferred — so: **`/api/admin/shadow/*` is canonical**, and
`src/api/scorecard.ts` was changed to match.

Why this set and not the client's:
  * There are SEVEN endpoints here, not one. `episodes`, `outcome` and
    `adjudication` are not sub-resources of a scorecard — they are the inputs
    that produce it. `/api/admin/scorecard/episodes` would be a lie about the
    shape of the thing.
  * The resource is the shadow INSTRUMENT. The scorecard is one projection of
    it; the export is another. One prefix, one router, one permission.
  * `/api/shadow/*` (the client's fallback) sits outside `/api/admin`, which is
    where every other governance surface in this codebase lives and where an
    operator looks for admin-only routes.

NO ALIAS IS REGISTERED. A compatibility alias here would have let the two sides
drift while both appeared to work, and the failure mode is silent: the client
renders `emptyScorecard()` — "this appliance has recorded nothing" — on a 404
from every mount, so a path mismatch would present as a *clean, plausible,
completely false* zero-state to a CIO. That is precisely the outcome the whole
package exists to prevent.

── ENGINEERING RULES ENFORCED HERE ───────────────────────────────────────────
 * **Every handler is `def`, never `async def`.** These are full scans of the
   busiest table in the pilot, and since encryption-at-rest every page read pays
   HMAC-SHA512 + AES. A coroutine handler would run them ON the event loop and
   stall the whole API — this repo has already shipped that outage once. `def`
   handlers are dispatched to the threadpool by Starlette. `tests/
   test_shadow_routes.py` asserts this property over the router programmatically,
   because it is the rule most likely to rot under a later edit.
 * **Admin-only via the existing RBAC**: `require_permission(ADMIN_OVERVIEW)`,
   the same dependency `admin_routes` uses. No new mechanism.
 * **Empty behaves correctly.** The pilot starts empty and every endpoint here
   is reachable on day zero. Nothing returns a bare `0%`; a metric with n below
   its floor is `insufficient_data` *with its denominator*, and a missing
   aggregation layer is a 503 that says so rather than a zeroed body.
 * **Fail-open vs fail-closed.** Shadow writes fail OPEN (a lost observational
   row must never fail a clinical action). The EXPORT fails CLOSED: it is a
   disclosure, audited under `phi_access` before a byte is returned, and if the
   audit cannot be persisted the export does not happen.
 * **The verdict vocabulary is not defined here.** It lives in
   `services/shadow_verdicts.py` and is shared with the module that does the
   arithmetic. A local copy in this file, and a different local copy in
   `shadow_scorecard_service`, is how every adjudication a clinician ever
   entered came to be written durably, counted as UNRECOGNISED, and reported as
   `unadjudicated` — with no exception, no log line, and no way for the operator
   to tell. `tests/test_shadow_routes.py` now posts through this route and reads
   the REAL scorecard, with no fake service, because a stub on the far side of
   the seam is what let the mismatch ship.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import uuid
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from permissions import (require_permission, enforce_patient_scope,
                         ADMIN_OVERVIEW, OUTCOME_RECORD)
from services import shadow_service
from services import shadow_verdicts
from services import shadow_export_service as exporter
from services.audit_service import log_audit_event, AuditWriteError

logger = logging.getLogger("orb.shadow.routes")

router = APIRouter(prefix="/api/admin/shadow", tags=["shadow-scorecard"])

#: One dependency object per permission, so neither can drift between handlers.
_ADMIN = Depends(require_permission(ADMIN_OVERVIEW))
#: Ground-truth ENTRY: nurse, doctor, admin. See the module docstring for why
#: this is not the same permission as reading the scorecard.
_RECORDER = Depends(require_permission(OUTCOME_RECORD))


# ══════════════════════════════════════════════════════════════════════════════
#  Aggregation layer resolution
# ══════════════════════════════════════════════════════════════════════════════
# `shadow_scorecard_service` is built by a sibling package and may not be on disk
# yet. It is imported LAZILY, per request, for one hard reason: a module-level
# `from services import shadow_scorecard_service` that raises ImportError would
# propagate through `main.py` and take the ENTIRE server down — every clinical
# route with it — because one analytics module was missing. An observability
# feature must never be able to do that.
#
# The candidate name lists are deliberate: the sibling package owns those names
# and this module must not force a rename by importing one spelling. The first
# match wins; if none match, the endpoint returns 503 naming what it looked for,
# which is a diagnosable failure rather than an AttributeError in a traceback.
_SCORECARD_FNS = ("build_scorecard", "scorecard", "compute_scorecard", "get_scorecard")
_EPISODES_FNS = ("list_episodes", "adjudication_queue", "episodes", "episode_queue")
_DETAIL_FNS = ("episode_detail", "get_episode_detail", "episode")
_HEALTH_FNS = ("instrument_health", "health", "instrument_status")


def _service() -> Any:
    try:
        from services import shadow_scorecard_service  # noqa: WPS433 (deliberate)
        return shadow_scorecard_service
    except ImportError:
        return None
    except Exception:
        logger.warning("shadow: scorecard service failed to import", exc_info=True)
        return None


def _resolve(names: tuple[str, ...]):
    svc = _service()
    if svc is None:
        return None
    for n in names:
        fn = getattr(svc, n, None)
        if callable(fn):
            return fn
    return None


def _require(names: tuple[str, ...], what: str):
    fn = _resolve(names)
    if fn is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"The shadow aggregation layer is not installed on this appliance: "
                f"no {what} implementation found (looked for "
                f"services/shadow_scorecard_service.py::{{{', '.join(names)}}}). "
                "Recorded events are unaffected; nothing has been lost."
            ),
        )
    return fn


def _call(fn, /, **kwargs) -> Any:
    """Call an aggregation function with only the kwargs it actually declares.

    The sibling package's signatures are not frozen. Passing `ward=` to a
    function that does not take it would be a 500 on an admin dashboard; instead
    the argument is dropped and the narrower result is returned. `**kwargs`
    signatures are passed through untouched.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return fn(**kwargs)
    accepted = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(**accepted)


def _days(days: int) -> int:
    return max(1, min(int(days or 30), 365))


# ══════════════════════════════════════════════════════════════════════════════
#  Request models
# ══════════════════════════════════════════════════════════════════════════════
#: Mirrors the outcome vocabulary the lead-time metric joins on. A free-text
#: event type would make the metric uncountable, so it is closed here.
OUTCOME_EVENT_TYPES = (
    "icu_transfer", "rapid_response_call", "code_blue", "death",
    "unplanned_escalation_of_care",
)
#: `witnessed` is the conservative source. `chart_review` is systematically
#: biased LATE (the reviewer finds the event, not the moment it began), which
#: INFLATES lead time — so the source is mandatory and reported separately.
OUTCOME_SOURCES = ("witnessed", "chart_review", "handover")

#: HOW the deterioration was recognised. Closed, because this is the field that
#: detects contamination: during the silent phase nobody is shown Orb's output,
#: so `prompted_by_orb` should be impossible — and a pilot that cannot see it
#: happening cannot know its own lead-time numbers are still clean.
OUTCOME_RECOGNITION = ("clinician_independent", "prompted_by_orb", "unknown")

#: A recorded event may be back-dated this far. Long enough for "it happened at
#: 06:20 and I am writing it at the end of a long shift" and for a missed
#: handover; short enough that a mistyped date cannot silently land an event
#: outside the pilot window, where it would join no episode and count for
#: nothing. Matches the 72 h in docs/OUTCOME_CAPTURE.md §5.
MAX_BACKDATE_HOURS = 72
#: A small tolerance for a device clock that is a few minutes ahead of the
#: server's. Anything further into the future is a typo, and an event stamped in
#: the future falls outside every aggregation window and silently disappears.
MAX_FUTURE_MINUTES = 5

#: NOT a local list. `services/shadow_verdicts.py` is the ONE definition, shared
#: with `shadow_scorecard_service`, because a second copy here is what silently
#: destroyed every adjudication the pilot ever recorded: this file accepted
#: `false_positive`, the scorecard recognised only `false_alert`, wrote the row
#: durably, classified it unrecognised, and reported `unadjudicated` for ever.
#: Neither side raised. Neither side logged.
ADJUDICATION_VERDICTS = shadow_verdicts.CANONICAL_VERDICTS
SHIFTS = ("morning", "evening", "night", "day", "unspecified")


class OutcomeIn(BaseModel):
    patient_id: str = Field(min_length=1, max_length=64)
    event_type: str
    occurred_at: Optional[str] = None
    source: str = "witnessed"
    recognised_by: Optional[str] = Field(default=None, max_length=64)
    confirmed_by_role: Optional[str] = Field(default=None, max_length=32)
    #: The recorder's own words. Stored in `phi_json`, never in the payload the
    #: export reads — see `shadow_service.record_outcome`.
    note: Optional[str] = Field(default=None, max_length=1000)


class NoOutcomeIn(BaseModel):
    ward: str = Field(min_length=1, max_length=64)
    period_start: str
    period_end: str
    shift: str = "unspecified"


class AdjudicationIn(BaseModel):
    episode_id: str = Field(min_length=1, max_length=64)
    verdict: str
    blinded: bool = True
    review_seconds: Optional[int] = Field(default=None, ge=0, le=86400)
    adjudicator_role: Optional[str] = Field(default=None, max_length=32)


def _choice(value: str, allowed: tuple[str, ...], field: str) -> str:
    v = (value or "").strip().lower()
    if v not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be one of {', '.join(allowed)} (got {value!r})",
        )
    return v


def _verdict(value: str) -> str:
    """Any accepted spelling in → the CANONICAL verdict, which is what is stored.

    Two properties, both load-bearing:

      * **Historical spellings are accepted, not rejected.** Rows and integrations
        written before the vocabularies were unified used `false_alert` and
        friends; refusing them would turn a silent miscount into a loud outage
        for no gain. They normalise on the way in, so from here the log holds
        exactly one spelling.
      * **What is stored is canonical.** The alternative — store what was typed,
        normalise on read — leaves the log carrying N spellings of one fact and
        makes every future reader responsible for knowing the synonym table. One
        reader forgetting is the whole of Defect 1.
    """
    canonical = shadow_verdicts.normalise(value)
    if canonical is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"verdict must be one of {', '.join(shadow_verdicts.CANONICAL_VERDICTS)} "
                f"(got {value!r}). Historical spellings are also accepted and normalised: "
                f"{', '.join(sorted(shadow_verdicts.VERDICT_SYNONYMS))}."
            ),
        )
    return canonical


# ══════════════════════════════════════════════════════════════════════════════
#  READ
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/scorecard")
def get_scorecard(
    days: int = Query(30, ge=1, le=365),
    ward: Optional[str] = Query(None, max_length=64),
    user: dict = _ADMIN,
):
    """The four metrics, the instrument's own health, and the export descriptor.

    Heavy scan → `def`, threadpool. See the module docstring.
    """
    fn = _require(_SCORECARD_FNS, "scorecard")
    card = _call(fn, days=_days(days), ward=ward)
    if not isinstance(card, dict):
        raise HTTPException(status_code=500, detail="Scorecard service returned a non-object")

    # The export descriptor is owned HERE, not by the aggregation layer: this
    # module knows which formats it can actually serve and what the export
    # removes, and the client refuses to offer an export that does not declare
    # its de-identification manifest.
    card.setdefault("export", {})
    if isinstance(card.get("export"), dict):
        desc = card["export"]
        # `available` stays the aggregation layer's call (it says False when the
        # instrument is absent — there is nothing to export). Everything else is
        # replaced with what the export code ACTUALLY does: the manifest shown
        # to the reader must be the one `shadow_export_service` enforces, not a
        # second copy that can drift away from it silently.
        desc.update({
            "available": desc.get("available", True),
            "formats": list(exporter.FORMATS),
            "deidentification": exporter.deidentification_manifest(),
            "note": desc.get("note") or (
                f"Aggregate only, de-identified, small-cell suppressed at "
                f"k={exporter.small_cell_k()} with complementary suppression. "
                "Every export is audited."
            ),
        })

    health = _resolve(_HEALTH_FNS)
    if health is not None:
        try:
            card.setdefault("instrument", {})
            if isinstance(card.get("instrument"), dict):
                extra = _call(health)
                if isinstance(extra, dict):
                    # The instrument must report its own health, or a silent
                    # write failure reads as "no events happened".
                    for k, v in extra.items():
                        card["instrument"].setdefault(k, v)
        except Exception:
            logger.warning("shadow: instrument_health failed", exc_info=True)
    _attach_stats(card)
    return card


def _attach_stats(card: dict) -> None:
    """Fold `shadow_service.STATS` onto the instrument block.

    These are process-lifetime counters, so they are labelled as such. They are
    the only evidence that would distinguish "no alerts fired" from "every emit
    was dropped": without them a broken instrument and a quiet ward produce an
    identical scorecard.
    """
    inst = card.get("instrument")
    if not isinstance(inst, dict):
        inst = {}
        card["instrument"] = inst
    stats = dict(shadow_service.STATS)
    dropped = (stats.get("dropped_error", 0)
               + stats.get("dropped_backpressure", 0)
               + stats.get("dropped_invalid", 0))
    inst["process_stats"] = stats
    inst.setdefault("dropped_events", dropped)
    inst.setdefault("error_events", stats.get("dropped_error", 0))
    inst["stats_scope"] = "since this server process started, not since the pilot began"
    inst["healthy"] = dropped == 0
    if dropped:
        inst["health_warning"] = (
            f"{dropped} shadow event(s) were dropped by this process "
            "(instrument fail-open). Denominators below undercount by at least that much."
        )


@router.get("/episodes")
def list_episodes(
    days: int = Query(30, ge=1, le=365),
    ward: Optional[str] = Query(None, max_length=64),
    status: str = Query("pending", pattern="^(pending|adjudicated|all)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = _ADMIN,
):
    """The adjudication queue — **BLINDED**.

    The adjudicator decides whether a deterioration was real from the CLINICAL
    RECORD, then their verdict is compared with Orb's. If they can see Orb's
    level, score, drivers or draft first, the false-alert rate measures their
    agreement with a number they were shown, not clinical validity, and the
    metric is contaminated beyond repair.

    Blinding is a WHITELIST applied here, in transport (`_BLIND_KEEP`), not a
    denylist and not a promise made by the aggregation layer. A field the
    sibling package adds tomorrow is invisible by default; the failure mode of a
    denylist is that a new field leaks silently, which is exactly the mistake
    this metric cannot survive.

    `status=adjudicated` returns the unblinded rows — the verdict is already in,
    so there is nothing left to contaminate.
    """
    fn = _require(_EPISODES_FNS, "episode queue")
    # `only_unadjudicated` is the aggregation layer's own spelling of "pending";
    # `status` is passed too and harmlessly dropped if it does not take it. The
    # complementary filter (already adjudicated) has no server-side flag, so it
    # is applied here on the verdict the row carries.
    rows = _call(fn, days=_days(days), ward=ward, status=status,
                 only_unadjudicated=(status == "pending"),
                 limit=limit, offset=offset)

    items = rows.get("episodes") if isinstance(rows, dict) else rows
    if not isinstance(items, list):
        items = []
    items = [r for r in items if isinstance(r, dict)]
    if status == "adjudicated":
        items = [r for r in items if r.get("verdict") or r.get("adjudicated")]

    blinded = status == "pending"
    projected = [_blind(r) for r in items] if blinded else items

    body = {
        "episodes": projected,
        "blinded": blinded,
        "blinding_note": (
            "Orb's level, score, drivers and draft are withheld until a verdict is "
            "recorded. Adjudicate from the clinical record, not from this row."
            if blinded else
            "Already adjudicated: Orb's assessment is shown for comparison."
        ),
        "status": status,
        "count": len(projected),
        "limit": limit,
        "offset": offset,
        "total": rows.get("total") if isinstance(rows, dict) else None,
        # The vocabulary the adjudicator may choose from, and how each one is
        # counted — served from the same table the metric classifies with, so
        # the UI cannot offer a verdict the scorecard would discard.
        "verdicts": list(ADJUDICATION_VERDICTS),
        "verdict_meanings": [
            {"verdict": v, "label": shadow_verdicts.VERDICT_LABEL[v],
             "counts_as": shadow_verdicts.VERDICT_CLASS[v]}
            for v in ADJUDICATION_VERDICTS
        ],
    }
    if not projected:
        body["empty_reason"] = (
            "No episodes in this window. On a new appliance this means the "
            "instrument has recorded nothing yet. It does not mean no alerts fired."
        )
    return body


#: Everything an adjudicator may see BEFORE giving a verdict. Deliberately
#: excludes `max_level`, `level`, `score`, `raw_news2`, `drivers`, `draft`,
#: `confidence`, `verdict`, `verdict_class` and the outcome TYPE — those are
#: either the machine's answer to the question being asked, or the answer key.
#:
#: `patient_id` IS included. Blinding is about withholding ORB'S ASSESSMENT, not
#: about hiding the patient: the adjudicator's whole job is to open that
#: patient's chart and decide from the clinical record. Withholding the patient
#: would not be blinding, it would be preventing adjudication. This endpoint is
#: admin-only and is not the export.
_BLIND_KEEP = (
    "episode_id", "patient_id", "ward", "phase", "lane",
    "opened_at", "last_at", "closed_at", "closed", "duration_min",
    "events_n", "alerts", "alerts_n", "sage_drafts",
)


def _blind(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    out = {k: row[k] for k in _BLIND_KEEP if k in row}
    # Derived, not copied: THAT an outcome exists tells the reviewer where to
    # look; WHICH outcome it was is the answer key and stays behind.
    if "outcome" in row or "has_outcome" in row:
        out["has_outcome"] = bool(row.get("has_outcome") or row.get("outcome"))
    out["blinded"] = True
    return out


@router.get("/episode/{episode_id}")
def get_episode(episode_id: str, user: dict = _ADMIN):
    """Ordered detail for one episode: every row, in the order it occurred.

    Unblinded by design — this is the review view, opened once a verdict has
    been formed or when investigating the instrument itself.
    """
    if not episode_id or len(episode_id) > 64:
        raise HTTPException(status_code=422, detail="Invalid episode id")
    fn = _require(_DETAIL_FNS, "episode detail")
    detail = _call(fn, episode_id=episode_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"No episode {episode_id}")
    if isinstance(detail, list):
        detail = {"episode_id": episode_id, "events": detail}
    return detail


# ══════════════════════════════════════════════════════════════════════════════
#  HUMAN ENTRY (ground truth + adjudication)
# ══════════════════════════════════════════════════════════════════════════════
# Both POSTs are human-entered, off any clinical path, and write through
# shadow_service's public API. NOTE the deliberate asymmetry with the machine
# emit path: there, a failed write is swallowed so a clinical action cannot
# fail. HERE a failed write is reported as 503, because a human just typed a
# ground-truth fact into a form and must not be told it was recorded when it was
# not. Fail-open protects the patient; it must not silently deceive the operator.
@router.post("/outcome", status_code=201)
def post_outcome(body: OutcomeIn, request: Request, user: dict = _RECORDER):
    """Log a confirmed clinical outcome — the ground-truth term of lead time.

    Without these rows detection lead time is not weak, it is UNDEFINED:
    `patients.status` is CHECK-constrained to admitted/discharged/transferred
    and cannot represent death, ICU transfer or arrest.

    Recorded by the charge nurse from the nurse view (Decision 2), or by an
    administrator backfilling.
    """
    event_type = _choice(body.event_type, OUTCOME_EVENT_TYPES, "event_type")
    source = _choice(body.source, OUTCOME_SOURCES, "source")
    recognised_by = (_choice(body.recognised_by, OUTCOME_RECOGNITION, "recognised_by")
                     if body.recognised_by else None)
    occurred_at = _event_time(body.occurred_at)

    # SCOPING LAW — the patient is named in the BODY (2026-08-14).
    # PROBED on 2026-08-07: a Cardiology nurse logged an `icu_transfer` on an
    # Oncology patient — HTTP 201, shadow_event row created. This is worse than
    # an ordinary boundary crossing. These rows are the pilot's GROUND TRUTH:
    # a fabricated or misfiled one moves the denominator of the metric the
    # appliance is judged on, and it is entered by an account this file
    # deliberately denies the scorecard to (see the two-permissions note at the
    # top) — so the person who can corrupt the number is exactly the person who
    # cannot see it move. `shadow_events` is append-only, so a wrong row cannot
    # be taken back.
    #
    # Placed AFTER the enum validation on purpose: "event_type must be one of…"
    # is a statement about the REQUEST and owes nothing to who the patient is,
    # so a malformed body keeps its 422 rather than being masked by a 404.
    # A hospital-wide account (no department on its user row — this codebase's
    # convention) is unaffected and can still backfill across wards.
    enforce_patient_scope(user, body.patient_id)

    row_id = shadow_service.record_outcome(
        body.patient_id,
        event_type,
        occurred_at=occurred_at,
        recognised_by=recognised_by,
        source=source,
        confirmed_by_role=body.confirmed_by_role or user.get("role"),
        actor_user_id=user.get("id"),
        note=body.note,
        lane="manual",
    )
    if row_id is None:
        raise HTTPException(
            status_code=503,
            detail="The outcome was NOT recorded (shadow instrument unavailable or disabled). Do not treat it as logged.",
        )
    _audit_entry(request, user, "shadow_outcome_recorded",
                 f"Ground-truth outcome {event_type} recorded (source={source})",
                 patient_id=body.patient_id,
                 metadata={"event_type": event_type, "source": source,
                           "recognised_by": recognised_by, "backdated": occurred_at is not None,
                           "shadow_event_id": row_id})
    return {
        "recorded": True, "shadow_event_id": row_id,
        "event_type": event_type, "source": source, "recognised_by": recognised_by,
        "occurred_at": occurred_at,
        "note": ("Recorded as ground truth. `witnessed` is the conservative source; "
                 "`chart_review` is biased late and is reported separately because it inflates lead time."),
    }


@router.post("/outcome/none", status_code=201)
def post_no_outcome(body: NoOutcomeIn, request: Request, user: dict = _RECORDER):
    """The per-shift "no deterioration events this shift" confirmation.

    THE MOST OVERLOOKED ROW IN THE PILOT. Without it, an unrecorded shift and an
    event-free shift are indistinguishable, and every denominator quietly
    inherits the optimistic reading. A ward-shift with neither this nor a
    recorded outcome is EXCLUDED from all denominators and reported as uncovered
    time on the face of the scorecard.

    No patient is involved, so there is no episode to resolve. A synthetic
    coverage episode id is minted per (ward, date, shift) — deterministic, so
    re-submitting the same shift threads onto the same episode instead of
    inflating the episode count.
    """
    shift = _choice(body.shift, SHIFTS, "shift")
    ward = body.ward.strip()
    episode_id = _coverage_episode_id(ward, body.period_start, shift)

    # A resubmitted ward-shift is a NO-OP, mirroring how idx_shadow_once treats
    # a re-emitted once-per-episode kind in shadow_service (deduped, expected,
    # not a failure). The shift-status fail-soft path explicitly tells the
    # nurse to "confirm the shift anyway; a duplicate confirmation is
    # deduplicated" — before migration 18 that sentence was FALSE: each
    # resubmission appended an identical, undeletable row. Telling the nurse
    # a resubmit failed would be equally false, so it answers 200 with the
    # original confirmation, never 4xx/5xx.
    existing = _existing_confirmation(episode_id)
    if existing is None:
        # `occurred_at` is the moment of CONFIRMATION (default = now), NOT
        # `period_end`. A charge nurse confirms at the end of a shift but the
        # clock is rarely exact, and any confirmation stamped slightly in the
        # future falls outside `occurred_at < now` in every aggregation window
        # and vanishes — coverage would silently read low for the shift most
        # recently confirmed. The shift itself is fully described by the
        # payload, which is what coverage is keyed on.
        row_id = shadow_service.emit(
            "outcome.none_confirmed", episode_id, "manual",
            ward=ward,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            payload={"ward": ward, "period_start": body.period_start,
                     "period_end": body.period_end, "shift": shift},
        )
        if row_id is not None:
            _audit_entry(request, user, "shadow_shift_confirmed",
                         f"No-deterioration confirmation for {ward} {shift}",
                         metadata={"ward": ward, "shift": shift,
                                   "period_start": body.period_start, "shadow_event_id": row_id})
            return {"recorded": True, "shadow_event_id": row_id,
                    "episode_id": episode_id, "ward": ward, "shift": shift}
        # The emit was refused. Two very different states share that return
        # value: the instrument is down (the nurse must be told, loudly), or a
        # concurrent resubmission just won the race on idx_shadow_once (the
        # shift IS confirmed). Re-read to tell them apart — the unique index
        # makes a present row authoritative and an absent one honest.
        existing = _existing_confirmation(episode_id)
        if existing is None:
            raise HTTPException(
                status_code=503,
                detail="The shift confirmation was NOT recorded (shadow instrument unavailable or disabled). This ward-shift will count as uncovered time.",
            )
    return JSONResponse(status_code=200, content={
        "recorded": False, "already_confirmed": True,
        "episode_id": episode_id, "ward": ward, "shift": shift,
        "shadow_event_id": existing.get("id"),
        "confirmed_at": existing.get("occurred_at"),
        "confirmed_by_role": existing.get("actor_role"),
        "note": "This ward-shift was already confirmed; the original confirmation stands and nothing was written.",
    })


def _existing_confirmation(episode_id: str) -> Optional[dict]:
    """The current (unsuperseded) confirmation row for a coverage episode.

    Read failures return None rather than raising: recording must never be
    blocked by a broken read (the shift-status endpoint makes the same call),
    and the emit that follows — with idx_shadow_once behind it — still holds
    the once-per-shift line even when this lookup is blind.
    """
    try:
        from database import db_session
        with db_session() as db:
            row = db.execute(
                "SELECT id, occurred_at, actor_role FROM shadow_events_current "
                "WHERE episode_id = ? AND kind = 'outcome.none_confirmed' "
                "ORDER BY id LIMIT 1",
                (episode_id,),
            ).fetchone()
        return dict(row) if row is not None else None
    except Exception:
        logger.warning("shadow: confirmation lookup failed for %s", episode_id, exc_info=True)
        return None


def _event_time(raw: Optional[str]) -> Optional[str]:
    """Validate an edited event time. None means "now" (the service's default).

    Two failure modes this closes, both of which silently destroy a row rather
    than rejecting it:

      * **A future timestamp vanishes.** Every aggregation window is
        `occurred_at < now`, so an event stamped tomorrow is in no window at all.
        The nurse would be told it was recorded, and it would never appear.
      * **A far-past timestamp joins no episode.** Beyond the episode window it
        cannot be matched to the alert it should be timed against, so it counts
        toward nothing. A mistyped date is the normal cause.

    Both are rejected loudly at entry, where the person who typed it is still
    standing there and can fix it.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"occurred_at must be an ISO-8601 timestamp (got {raw!r})",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if parsed > now + timedelta(minutes=MAX_FUTURE_MINUTES):
        raise HTTPException(
            status_code=422,
            detail=("The event time is in the future. It would fall outside every "
                    "reporting window and the event would count for nothing."),
        )
    if parsed < now - timedelta(hours=MAX_BACKDATE_HOURS):
        raise HTTPException(
            status_code=422,
            detail=(f"The event time is more than {MAX_BACKDATE_HOURS} hours ago. Record it "
                    "with source=chart_review through an administrator if it is genuinely "
                    "that old. A date this far back is usually a typo, and it would not "
                    "match the episode it belongs to."),
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _coverage_episode_id(ward: str, period_start: str, shift: str) -> str:
    """Deterministic per-ward-shift id. Not `new_episode_id()`: a resubmitted or
    retried confirmation must land on the SAME episode, or the coverage
    numerator counts submissions instead of shifts."""
    import hashlib
    key = f"{ward}|{str(period_start)[:10]}|{shift}".lower()
    return "cov_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════════
#  What this shift still owes — the read side of the nurse's surface
# ══════════════════════════════════════════════════════════════════════════════
def _shift_definitions() -> list[tuple[dt_time, float]]:
    """Ward shifts as (start, hours). ONE definition, shared with the scorecard.

    Resolved from `shadow_scorecard_service` when it is installed, so the
    windows the nurse confirms are byte-for-byte the windows coverage is
    measured over. The fallback parses the SAME environment variable with the
    same default, so the two cannot disagree about the ward's shift pattern —
    if they did, a nurse could confirm every shift and coverage would still read
    zero, which is the most demoralising possible failure of this feature.
    """
    svc = _service()
    fn = getattr(svc, "_shifts", None) if svc is not None else None
    if callable(fn):
        try:
            shifts = fn()
            if shifts:
                return shifts
        except Exception:
            logger.warning("shadow: scorecard shift definition unusable", exc_info=True)

    raw = os.getenv("ORB_SCORECARD_SHIFTS", "07:00-15:00,15:00-23:00,23:00-07:00")
    out: list[tuple[dt_time, float]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        a, _, b = chunk.partition("-")
        try:
            sh, sm = (int(x) for x in a.strip().split(":"))
            eh, em = (int(x) for x in b.strip().split(":"))
        except ValueError:
            continue
        length = ((eh * 60 + em) - (sh * 60 + sm)) / 60.0
        if length <= 0:
            length += 24.0
        out.append((dt_time(hour=sh % 24, minute=sm % 60), length))
    return out or [(dt_time(7, 0), 8.0), (dt_time(15, 0), 8.0), (dt_time(23, 0), 8.0)]


def _shift_label(start: datetime) -> str:
    """A window's start time (hospital wall clock) → the closed `SHIFTS` vocabulary.

    Only the ID minted for the confirmation depends on this, not the coverage
    arithmetic (which matches on the period, not the name) — so a hospital
    running a non-standard shift pattern gets a slightly odd label, never a
    mis-counted shift.
    """
    hour = start.hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "evening"
    if hour >= 18 or hour < 5:
        return "night"
    return "unspecified"


def _windows_around(now: datetime) -> list[dict]:
    """The shift window containing `now`, and the one before it.

    Shift times are the ward's wall clock. They used to be laid on UTC
    midnight, so at 18:40 IST the nurse was asked to confirm "Morning
    12:30-20:30". The same rule lives in shadow_scorecard_service's
    `_ward_shift_windows`; the two must produce identical period strings.
    """
    from services.hospital_time import hospital_timezone
    tz = hospital_timezone()
    local_now = now.astimezone(tz)
    shifts = _shift_definitions()
    candidates: list[dict] = []
    for day_offset in (-1, 0):
        day = (local_now + timedelta(days=day_offset)).date()
        for start_time, length in shifts:
            local_start = datetime.combine(day, start_time, tzinfo=tz)
            start = local_start.astimezone(timezone.utc)
            end = start + timedelta(hours=length)
            candidates.append({"start": start, "end": end, "shift": _shift_label(local_start)})
    candidates.sort(key=lambda w: w["start"])
    current = [w for w in candidates if w["start"] <= now < w["end"]]
    if not current:
        # Gapped shift configuration: fall back to the most recent window that
        # has already started, rather than inventing one.
        started = [w for w in candidates if w["start"] <= now]
        current = started[-1:] or candidates[:1]
    window = current[-1]
    earlier = [w for w in candidates if w["end"] <= window["start"]]
    previous = earlier[-1] if earlier else None
    return [w for w in (previous, window) if w is not None]


@router.get("/outcome/shift-status")
def get_shift_status(
    ward: list[str] = Query(default=[]),
    user: dict = _RECORDER,
):
    """What this ward-shift still owes the pilot: the read half of the nurse's
    end-of-shift surface.

    Returns, for the CURRENT shift and the one before it, per ward: whether a
    "no deterioration events" confirmation exists, and how many events were
    recorded. It returns NOTHING about how Orb is scoring — see the module
    docstring on why the recorder must not see the scorecard.

    The `period_start` / `period_end` / `shift` strings returned here are the
    exact values the client posts back to `/outcome/none`. That is what makes
    "already confirmed?" exact instead of approximate: both sides derive the
    coverage episode id from the same three strings through the same function.

    Heavy scan → `def`, threadpool. Same rule as every other handler here.
    """
    wards: list[str] = []
    for raw in ward[:8]:
        name = (raw or "").strip()[:64]
        if name and name not in wards:
            wards.append(name)

    now = datetime.now(timezone.utc)
    windows = _windows_around(now)
    oldest = min(w["start"] for w in windows)

    rows: list[dict] = []
    try:
        from database import db_session
        with db_session() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT kind, ward, occurred_at, payload_json, actor_role "
                "FROM shadow_events_current "
                "WHERE kind IN ('outcome.none_confirmed', 'outcome.recorded') "
                "  AND occurred_at >= ? ORDER BY id",
                (_iso(oldest - timedelta(hours=12)),),
            ).fetchall()]
    except Exception:
        # Fail SOFT on the read: a nurse must still be able to record. The
        # response says the status is unknown rather than claiming "confirmed",
        # because claiming it would stop them recording something real.
        logger.warning("shadow: shift-status read failed", exc_info=True)
        return {
            "generated_at": _iso(now),
            "status_available": False,
            "shifts": [_window_body(w, wards, [], unknown=True) for w in windows],
            "note": ("The confirmation history could not be read. Recording still works: "
                     "confirm the shift anyway; a duplicate confirmation is deduplicated."),
            "event_types": list(OUTCOME_EVENT_TYPES),
            "sources": list(OUTCOME_SOURCES),
            "recognition": list(OUTCOME_RECOGNITION),
        }

    parsed: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        parsed.append({**row, "payload": payload if isinstance(payload, dict) else {}})

    return {
        "generated_at": _iso(now),
        "status_available": True,
        "shifts": [_window_body(w, wards, parsed) for w in windows],
        "shift_definition": ", ".join(
            f"{s.strftime('%H:%M')}+{h:g}h" for s, h in _shift_definitions()),
        "max_backdate_hours": MAX_BACKDATE_HOURS,
        "event_types": list(OUTCOME_EVENT_TYPES),
        "sources": list(OUTCOME_SOURCES),
        "recognition": list(OUTCOME_RECOGNITION),
        "why": ("A ward-shift with neither a recorded event nor a no-events confirmation is "
                "excluded from every denominator in the pilot and reported as uncovered "
                "time. Silence is not evidence that nothing happened."),
    }


def _window_body(window: dict, wards: list[str], rows: list[dict],
                 *, unknown: bool = False) -> dict:
    start, end = window["start"], window["end"]
    body = {
        "shift": window["shift"],
        "period_start": _iso(start),
        "period_end": _iso(end),
        # Hospital time, not the server process's zone (UTC on an appliance
        # that was never told otherwise).
        "label": f"{_hospital_clock(start)}–{_hospital_clock(end)}",
        "is_current": start <= datetime.now(timezone.utc) < end,
        "wards": [],
    }
    for ward in wards:
        confirmed_at = None
        confirmed_by = None
        events = 0
        if not unknown:
            for row in rows:
                if (row.get("ward") or row["payload"].get("ward")) != ward:
                    continue
                if row["kind"] == "outcome.none_confirmed":
                    # Exact match on the period this confirmation DESCRIBES —
                    # not on when it was submitted. A nurse confirming at 06:55
                    # for a shift ending at 07:00 must not have it counted
                    # against the next shift.
                    if row["payload"].get("period_start") == body["period_start"]:
                        confirmed_at = row.get("occurred_at")
                        confirmed_by = row.get("actor_role")
                elif row["kind"] == "outcome.recorded":
                    when = _parse_iso(row.get("occurred_at"))
                    if when is not None and start <= when < end:
                        events += 1
        body["wards"].append({
            "ward": ward,
            "confirmed": confirmed_at is not None,
            "confirmed_at": confirmed_at,
            # Role only, never a name: the capture screen says "already
            # confirmed by the charge nurse at 06:55", and a name here would
            # put staff identity on every poll of a recorder-scoped endpoint.
            "confirmed_by_role": confirmed_by,
            "events_recorded": events,
            "status": ("unknown" if unknown else
                       "confirmed" if confirmed_at else
                       "events_recorded" if events else "outstanding"),
        })
    return body


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _hospital_clock(when: datetime) -> str:
    from services.hospital_time import hospital_timezone
    return when.astimezone(hospital_timezone()).strftime("%H:%M")


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@router.post("/adjudication", status_code=201)
def post_adjudication(body: AdjudicationIn, request: Request, user: dict = _ADMIN):
    """Record a clinician's verdict on an episode (the false-alert term).

    `artefact` and `indeterminate` are first-class verdicts, not a bin. They are
    EXCLUDED from the false-alert numerator and denominator and reported
    separately — folding an artefact into "false positive" would blame the model
    for a disconnected lead, and dropping them silently would flatter the rate.

    The verdict is normalised to the canonical vocabulary BEFORE it is written,
    against the same table `shadow_scorecard_service` classifies with. There is
    no second list here to drift away from it.
    """
    verdict = _verdict(body.verdict)

    row_id = shadow_service.record_adjudication(
        body.episode_id,
        verdict,
        adjudicator_role=body.adjudicator_role or user.get("role"),
        blinded=bool(body.blinded),
        review_seconds=body.review_seconds,
        actor_user_id=user.get("id"),
        lane="manual",
    )
    if row_id is None:
        raise HTTPException(
            status_code=503,
            detail="The adjudication was NOT recorded (shadow instrument unavailable or disabled). Do not treat it as logged.",
        )
    submitted = (body.verdict or "").strip()
    _audit_entry(request, user, "shadow_adjudication_recorded",
                 f"Adjudication {verdict} recorded",
                 metadata={"verdict": verdict, "verdict_as_submitted": submitted,
                           "blinded": bool(body.blinded), "shadow_event_id": row_id})
    return {
        "recorded": True, "shadow_event_id": row_id, "verdict": verdict,
        # Stated back when it differs, so an integration posting a historical
        # spelling is told what was actually written rather than assuming.
        "verdict_as_submitted": (submitted if submitted.lower() != verdict else None),
        "verdict_label": shadow_verdicts.label(verdict),
        "counts_as": shadow_verdicts.classify(verdict),
        "blinded": bool(body.blinded),
        "note": ("artefact and indeterminate verdicts are excluded from the false-alert "
                 "rate and reported separately."),
    }


def _audit_entry(request: Request, user: dict, action: str, summary: str,
                 *, patient_id: Optional[str] = None,
                 metadata: Optional[dict] = None) -> None:
    """Audit a human entry. Fail-SOFT (`governance` category).

    The row it accompanies is already durably written in `shadow_events`, which
    is append-only at the DB layer; refusing the request after that write would
    tell the operator their entry failed when it did not, and they would enter
    it twice. The export, where nothing is written and bytes leave the building,
    is the opposite and fails closed.
    """
    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="governance",
            action_type=action,
            outcome="success",
            summary_message=summary,
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=patient_id,
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata=metadata,
        )
    except Exception:
        logger.warning("shadow: audit of %s failed", action, exc_info=True)


# ══════════════════════════════════════════════════════════════════════════════
#  EXPORT — audited FAIL-CLOSED
# ══════════════════════════════════════════════════════════════════════════════
@router.get("/export/scorecard")
def export_scorecard(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    ward: Optional[str] = Query(None, max_length=64),
    format: str = Query("json", pattern="^(json|csv)$"),
    user: dict = _ADMIN,
):
    """De-identified aggregate export, with a mandatory provenance block.

    Ordering is load-bearing: BUILD, then AUDIT, then RETURN. The audit is
    written under `phi_access`, which `audit_service` treats as fail-closed — if
    it cannot be persisted, `AuditWriteError` propagates and the caller gets 503
    with nothing in the body. An unaudited disclosure never leaves this process.

    The scorecard metrics are injected into the export rather than imported by
    it, so an appliance without the aggregation layer still exports its
    instrument volume, coverage and provenance — and says the metrics are
    absent — instead of failing outright.
    """
    days = _days(days)

    card: Optional[dict] = None
    fn = _resolve(_SCORECARD_FNS)
    if fn is not None:
        try:
            result = _call(fn, days=days, ward=ward)
            card = result if isinstance(result, dict) else None
        except Exception:
            logger.warning("shadow export: scorecard computation failed", exc_info=True)

    health: Optional[dict] = None
    hfn = _resolve(_HEALTH_FNS)
    if hfn is not None:
        try:
            h = _call(hfn)
            health = h if isinstance(h, dict) else None
        except Exception:
            logger.warning("shadow export: instrument_health failed", exc_info=True)

    payload = exporter.build_export(
        days, ward, scorecard=card, instrument=health, actor_role=user.get("role"),
    )
    prov = payload.get("provenance", {})
    if not prov or "integrity" not in prov or "removed" not in prov:
        # An export that cannot state its own provenance is not evidence.
        raise HTTPException(status_code=500, detail="Export refused: provenance block incomplete")

    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            event_category="phi_access",           # fail-closed category
            action_type="shadow_scorecard_export",
            outcome="success",
            summary_message=(
                f"De-identified shadow scorecard exported ({format}, {days}d, "
                f"{prov['row_counts']['shadow_events_current']} source rows)"
            ),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            target_type="shadow_scorecard_export",
            target_id=prov["integrity"]["digest"][:32],
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={
                "format": format, "days": days, "ward_filtered": bool(ward),
                "deidentified": True,
                "suppression_k": prov["suppression"]["k"],
                "cells_suppressed": prov["suppression"]["cells_suppressed"],
                "coverage": prov["coverage"]["ward_shifts"],
                "sha256": prov["integrity"]["digest"],
            },
        )
    except AuditWriteError as e:
        logger.error("shadow export REFUSED: audit could not be persisted (%s)", e)
        raise HTTPException(
            status_code=503,
            detail="Export refused: the disclosure could not be audited, and an unaudited export must not leave the appliance.",
        )

    if format == "csv":
        return PlainTextResponse(
            exporter.to_csv(payload),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{exporter.filename(days, "csv")}"'},
        )
    return payload
