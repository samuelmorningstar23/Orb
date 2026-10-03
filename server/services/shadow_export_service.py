"""Orb — de-identified aggregate export of the shadow-mode scorecard.

THE HIGHEST-RISK SURFACE IN THE SHADOW PACKAGE. Everything else in shadow mode
is read by us; this file produces the artefact that leaves the building and is
read by a hospital CIO, by counsel, and by an investor. Two failures are
unrecoverable once the file is sent:

  1. **PHI in the export.** A single patient id in a JSON blob is a reportable
     disclosure, and no amount of later correction un-sends it.
  2. **An export that cannot state its own provenance.** A table of numbers with
     no date range, no denominators, no coverage and no statement of what was
     removed is not evidence — it is a claim. `provenance` is mandatory here and
     `build_export` will not produce a payload without it.

── De-identification is by CONSTRUCTION, not by filtering ────────────────────
Three independent layers, in this order, because any one of them can be wrong:

  a. **Whitelist.** Nothing reaches the payload except fields this module names.
     The metrics handed in by `shadow_scorecard_service` are projected through
     `_METRIC_FIELDS`; unknown keys are dropped, not inspected. A future metric
     that starts carrying `worst_patient_id` therefore fails CLOSED (the field
     vanishes) rather than open.
  b. **Key denylist + token redaction** (`_scrub`). A recursive pass that removes
     identifier-shaped KEYS at any depth and redacts identifier-shaped TOKENS
     inside free text — because `caveat` and `blockers` are prose written by
     another module, and prose is where an id leaks.
  c. **Small-cell suppression** at k = 5, WITH complementary suppression. This is
     the one that catches what the other two cannot: no individual field is PHI,
     but "Ward B: 1 death" re-identifies a person perfectly well.

── On reusing sage_service's suppression ─────────────────────────────────────
The design said to CALL the existing complementary-suppression logic rather than
reimplement it. WHAT IS ACTUALLY ON DISK (checked, not assumed):

  * `sage_service.AGGREGATE_SMALL_CELL = 5`  — a reusable constant. REUSED here.
  * `sage_service._mask_small_cell(n)`       — a reusable one-cell masker. REUSED.
  * complementary suppression                — NOT reusable. It exists only as
    ~12 inlined lines inside `build_aggregate_context()`, a function that opens
    its own connection, queries `patients` directly, and returns English prose
    for an LLM prompt. There is no function to call.

So the *policy* (k, and the vocabulary "<5") is imported rather than restated —
if the hospital raises k, both surfaces move together — and only the ALGORITHM
is written here, generalised from rows-of-prose to any count mapping. Extracting
it from sage_service would mean editing a clinical hot path from this package,
which is not a trade worth making for twelve lines.

── Fail-closed, unlike the rest of shadow mode ───────────────────────────────
`shadow_service` writes fail OPEN: a lost observational row must never fail a
clinical action. An EXPORT is the opposite — it is a disclosure, and a
disclosure that cannot be audited must not happen. The route audits under the
`phi_access` category (which `audit_service` treats as fail-closed) BEFORE the
bytes are returned. Know which side of that line you are on before adding to
either file.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from database import db_session

logger = logging.getLogger("orb.shadow.export")

#: 1.1.0 — the export no longer asserts its own append-only claim (it reads the
#: instrument's verified state and refuses the word "evidentiary" when it cannot
#: be established), and it states which of its counts the k rule actually covers
#: instead of claiming it covers all of them. Counts masked by the k rule
#: serialise as the string "<k"; unmasked counts stay numeric.
EXPORT_SCHEMA_VERSION = "1.1.0"
EXPORT_TYPE = "orb_shadow_scorecard_aggregate"

#: The instrument's own schema version (migration 4 writes schema_v = 1).
INSTRUMENT_SCHEMA_V = 1

#: Assumed shifts per calendar day when converting an observation window into a
#: ward-shift denominator. STATED in the export, never silently applied.
SHIFTS_PER_DAY = 3

FORMATS = ("json", "csv")


# ══════════════════════════════════════════════════════════════════════════════
#  Small-cell suppression (policy imported from sage_service, algorithm here)
# ══════════════════════════════════════════════════════════════════════════════
def _sage() -> Any:
    """Import sage_service lazily.

    It is already resident in the running app (sage_routes imports it), so this
    costs nothing in production; the laziness exists so that a broken or heavy
    sage import can never take the export offline. If it cannot be imported the
    policy falls back to the same k with a logged warning — never to *no*
    suppression.
    """
    try:
        from services import sage_service  # noqa: WPS433 (deliberate lazy import)
        return sage_service
    except Exception:  # pragma: no cover - defensive
        logger.warning("shadow export: sage_service unavailable; using local k", exc_info=True)
        return None


def small_cell_k() -> int:
    """The k-anonymity threshold. Single source of truth = sage_service."""
    svc = _sage()
    k = getattr(svc, "AGGREGATE_SMALL_CELL", None) if svc else None
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 5
    return max(2, k)


def mask_cell(n: int) -> str:
    """Mask ONE count. Delegates to sage_service._mask_small_cell so the masked
    vocabulary ("<5") is identical on both disclosure surfaces."""
    svc = _sage()
    fn = getattr(svc, "_mask_small_cell", None) if svc else None
    if callable(fn):
        try:
            return str(fn(int(n)))
        except Exception:  # pragma: no cover - defensive
            pass
    k = small_cell_k()
    return f"<{k}" if 0 < int(n) < k else str(int(n))


def mask_count(n: Any) -> Any:
    """Mask ONE standalone count, preserving its JSON type when it is not masked.

    `mask_cell` always returns a string because it feeds a breakdown where every
    cell must render the same way. This one feeds `provenance.row_counts` and the
    instrument's volume counters, where a reader (and a test) reasonably expects
    a number — so an unmasked count stays an int and only a masked one becomes
    the "<k" token. Non-integral and negative inputs are returned untouched:
    durations and latencies are not counts of people and masking them would
    corrupt a measurement rather than protect anyone.
    """
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return n
    if isinstance(n, float) and not n.is_integer():
        return n
    value = int(n)
    k = small_cell_k()
    return f"<{k}" if 0 < value < k else value


def _is_small_exact_count(value: Any) -> bool:
    """True when `value` is an exact integer count that the k rule would mask."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return 0 < int(value) < small_cell_k()


def suppress_counts(counts: dict[str, int]) -> tuple[dict[str, str], int]:
    """Apply small-cell + COMPLEMENTARY suppression to one breakdown.

    Returns ``(display_map, n_suppressed)``.

    Small-cell alone is not enough whenever the exact TOTAL of the breakdown is
    also published (it is — `provenance.row_counts` states it, and a reader can
    always sum the visible cells anyway). With exactly one cell masked, the
    masked value is recoverable by subtraction and the suppression has achieved
    literally nothing. So when the rule masks exactly one cell we mask a SECOND
    — the smallest non-zero survivor — leaving two unknowns and one equation.

    Zeroes are never masked: a zero discloses no individual, and masking it
    would hide the single most important fact on a pilot scorecard (that a thing
    has not happened yet).
    """
    k = small_cell_k()
    items = list(counts.items())
    masked = {key for key, n in items if 0 < int(n) < k}
    if len(masked) == 1 and len(items) > 1:
        survivors = [(key, int(n)) for key, n in items if key not in masked and int(n) > 0]
        if survivors:
            masked.add(min(survivors, key=lambda kv: kv[1])[0])
    display = {
        key: (f"<{k}" if key in masked else str(int(n)))
        for key, n in items
    }
    return display, len(masked)


def is_degenerate(counts: dict[str, int]) -> bool:
    """True when a breakdown has fewer than two non-zero cells.

    In that case masking is COSMETIC: a single populated cell necessarily equals
    the breakdown's total, and complementary suppression has no second cell to
    hide behind. The export must say so rather than let its own manifest claim a
    protection it is not providing — an evidence document that overstates its
    disclosure control is worse than one that states a weaker control honestly.
    """
    return sum(1 for n in counts.values() if int(n) > 0) < 2


# ══════════════════════════════════════════════════════════════════════════════
#  Layer (b): recursive key denylist + free-text token redaction
# ══════════════════════════════════════════════════════════════════════════════
#: Keys removed at ANY depth. Includes `episode_id`: an episode id is a stable
#: per-patient-episode handle, i.e. a pseudonymous identifier, and a pseudonym
#: that is also the join key back into our own database is not de-identified.
_DENY_KEYS = frozenset({
    "patient_id", "patient", "patients", "mrn", "uhid", "nhs_number",
    "encounter_id", "episode_id", "episode_ids", "subject_id", "subject",
    "actor_user_id", "user_id", "employee_id", "actor", "adjudicator",
    "phi", "phi_json", "name", "patient_name", "full_name", "dob",
    "date_of_birth", "age", "sex", "gender", "bed", "bed_number", "room",
    "address", "phone", "email", "identifier", "identifiers",
    "source_correlation_id", "correlation_id",
})

#: Identifier-shaped tokens redacted inside any string that survives the
#: whitelist. These are the id FORMATS this codebase actually mints:
#: `P001`/`patient123` for patients, `ep_<16 hex>` for episodes.
_TOKEN_PATTERNS = (
    re.compile(r"\bep_[0-9a-fA-F]{8,}\b"),
    re.compile(r"\bP\d{3,}\b"),
    re.compile(r"\bpatient[_-]?\d+\b", re.IGNORECASE),
    re.compile(r"\b[A-Z]{2,4}\d{3,}\b"),          # DOC001 / N001-style staff ids
    re.compile(r"\b\d{10,}\b"),                    # long numeric ids / phone
)

REDACTED = "[redacted]"


def _redact_text(s: str) -> str:
    for pat in _TOKEN_PATTERNS:
        s = pat.sub(REDACTED, s)
    return s


def _scrub(obj: Any) -> Any:
    """Recursively drop identifier-shaped keys and redact identifier-shaped
    tokens. Applied to the WHOLE payload as the last step before hashing, so it
    also covers anything a future contributor adds to the whitelist by mistake.
    """
    if isinstance(obj, dict):
        return {
            k: _scrub(v)
            for k, v in obj.items()
            if str(k).lower() not in _DENY_KEYS
        }
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str):
        return _redact_text(obj)
    return obj


# ══════════════════════════════════════════════════════════════════════════════
#  The de-identification manifest — "the export states what has been removed"
# ══════════════════════════════════════════════════════════════════════════════
def deidentification_manifest() -> list[dict[str, Any]]:
    """Itemised, reader-facing statement of every transformation applied.

    Shape matches `DeidentificationItem` in src/api/scorecard.ts
    ({field, treatment, note}) so the admin view can render it verbatim — the
    frontend refuses to offer an export that does not declare one.
    """
    k = small_cell_k()
    return [
        {"field": "patient_id", "treatment": "removed",
         "note": "No patient identifier, direct or pseudonymous, appears in any row."},
        {"field": "episode_id", "treatment": "removed",
         "note": "Episode ids are stable per-patient handles and join back to the source database; they are dropped, not hashed."},
        {"field": "phi_json", "treatment": "dropped",
         "note": "The free-text PHI column of shadow_events is never read by the export query."},
        {"field": "actor_user_id", "treatment": "removed",
         "note": "Individual staff are not identified. Roles are retained only where a role is itself the measurement (adjudicator role)."},
        {"field": "ward", "treatment": "coarsened",
         "note": "Ward names are replaced with sequential pseudonyms (Ward 1…n) assigned by descending event volume. The mapping is not exported."},
        {"field": "occurred_at", "treatment": "coarsened",
         "note": "No event timestamps are exported. Only the aggregate period bounds and elapsed-time statistics are."},
        {"field": "aggregate counts (counts.*, provenance.row_counts, instrument volume)",
         "treatment": "coarsened",
         "note": f"Small-cell suppression at k={k}: any non-zero count below {k} is shown as \"<{k}\". Where that would mask exactly one cell in a breakdown, a second (smallest) cell is masked too, so no masked value in a breakdown of two or more populated cells can be recovered by differencing against the published total. A breakdown with a single populated cell is inherently equal to that total; those are named in provenance.suppression.masking_is_cosmetic_in."},
        # `treatment` is a single lowercase token by convention (the admin view
        # turns it into a CSS modifier); the honest detail belongs in the note.
        {"field": "metric denominators, n_observed and method notes",
         "treatment": "published",
         "note": f"NOT SUPPRESSED. A rate whose denominator is hidden cannot be checked, so scorecard.metrics[*] and provenance.denominators publish their numerators, denominators and observation counts exactly, including counts below {k}. The metrics' caveat, assumptions, alternative_denominators and provisional_reason text restates those same counts in words. Whenever any of them is below {k} this export therefore does NOT achieve k-anonymity, and provenance.suppression.k_anonymity_achieved says so and names every field involved."},
        {"field": "ward-shift coverage and instrument health counters",
         "treatment": "published",
         "note": "NOT SUPPRESSED. Covered/uncovered ward-shifts are counts of time periods, and dropped/error event counters are counts of instrument failures. Neither counts individuals, and suppressing them would hide the two facts most likely to weaken a claim made from this document."},
        {"field": "free text", "treatment": "removed",
         "note": "No clinical narrative, alert text, Sage draft or adjudicator comment is exported. Method notes and caveats are the only prose, and they are token-redacted."},
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  Aggregate reads — COUNT/GROUP BY only, so the query itself produces no rows
# ══════════════════════════════════════════════════════════════════════════════
def _period(days: int) -> tuple[str, str, int]:
    days = max(1, min(int(days or 30), 365))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return (start.isoformat(timespec="milliseconds"),
            end.isoformat(timespec="milliseconds"),
            days)


def _instrument_present(db) -> bool:
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'view' AND name = 'shadow_events_current'"
    ).fetchone()
    return row is not None


#: The triggers that make `shadow_events` append-only. Named here, not just in
#: prose, because the export CHECKS for them rather than asserting them.
APPEND_ONLY_TRIGGERS = ("trg_shadow_no_update", "trg_shadow_no_delete")


def _append_only(db) -> Optional[bool]:
    """Is the shadow log append-only RIGHT NOW? None when it cannot be checked.

    The export verifies this itself instead of trusting the value handed in.
    Both readings are used (see `build_export`): the aggregation layer's and this
    one, combined so that a `False` from either wins. An artefact whose only
    evidence of integrity is its own assertion of integrity is not evidence, and
    this is the one claim in the document a tampered appliance has a motive to
    forge.
    """
    try:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name IN (?, ?)",
            list(APPEND_ONLY_TRIGGERS),
        ).fetchall()
    except Exception:  # pragma: no cover - defensive
        logger.warning("shadow export: append-only trigger check failed", exc_info=True)
        return None
    return set(APPEND_ONLY_TRIGGERS) <= {r[0] for r in rows}


def _payloads(db, kind: str, start: str, end: str, ward: Optional[str]) -> list[dict]:
    """Fetch payload_json for one kind and parse in Python.

    Deliberately NOT json_extract(): the payload keys we need vary by kind, the
    row counts here are small (outcomes and adjudications are human-entered),
    and a Python parse cannot be broken by a build of SQLite compiled without
    the JSON1 extension.
    """
    sql = ("SELECT payload_json, ward, episode_id FROM shadow_events_current "
           "WHERE kind = ? AND occurred_at >= ? AND occurred_at < ?")
    args: list[Any] = [kind, start, end]
    if ward:
        sql += " AND ward = ?"
        args.append(ward)
    out = []
    for r in db.execute(sql, args).fetchall():
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            payload["_ward"] = r["ward"]
            payload["_episode"] = r["episode_id"]
            out.append(payload)
    return out


def _counts_by(db, column: str, start: str, end: str, ward: Optional[str]) -> dict[str, int]:
    if column not in ("kind", "lane", "ward"):  # never interpolate free text
        raise ValueError(f"illegal group column {column!r}")
    sql = (f"SELECT {column} AS g, COUNT(*) AS n FROM shadow_events_current "
           "WHERE occurred_at >= ? AND occurred_at < ?")
    args: list[Any] = [start, end]
    if ward:
        sql += " AND ward = ?"
        args.append(ward)
    sql += f" GROUP BY {column} ORDER BY n DESC"
    return {(r["g"] or "unattributed"): int(r["n"]) for r in db.execute(sql, args).fetchall()}


def collect_aggregates(days: int = 30, ward: Optional[str] = None) -> dict[str, Any]:
    """Every DB read the export makes, in one place. COUNT/GROUP BY only.

    SYNC and heavy — worker thread only, never the event loop.
    """
    start, end, days = _period(days)
    blank = {
        "instrument_present": False, "period": {"start": start, "end": end, "days": days},
        "events_total": 0, "episodes_total": 0, "corrections": 0,
        "append_only": None,
        "by_kind": {}, "by_lane": {}, "by_ward": {}, "ward_pseudonyms": {},
        "outcomes": {}, "outcome_sources": {}, "adjudications": {},
        "coverage": _empty_coverage(days),
    }
    try:
        with db_session() as db:
            append_only = _append_only(db)
            if not _instrument_present(db):
                return {**blank, "append_only": append_only}

            by_kind = _counts_by(db, "kind", start, end, ward)
            by_lane = _counts_by(db, "lane", start, end, ward)
            by_ward_raw = _counts_by(db, "ward", start, end, ward)

            args: list[Any] = [start, end]
            ward_clause = ""
            if ward:
                ward_clause = " AND ward = ?"
                args.append(ward)
            episodes = db.execute(
                "SELECT COUNT(DISTINCT episode_id) AS n FROM shadow_events_current "
                "WHERE occurred_at >= ? AND occurred_at < ?" + ward_clause, args
            ).fetchone()
            corrections = db.execute(
                "SELECT COUNT(*) AS n FROM shadow_events "
                "WHERE supersedes_event_id IS NOT NULL AND occurred_at >= ? AND occurred_at < ?",
                [start, end],
            ).fetchone()

            outcome_rows = _payloads(db, "outcome.recorded", start, end, ward)
            none_rows = _payloads(db, "outcome.none_confirmed", start, end, ward)
            adj_rows = _payloads(db, "adjudication.recorded", start, end, ward)
    except Exception:
        logger.warning("shadow export: aggregate read failed", exc_info=True)
        return blank

    # Ward pseudonyms: descending volume, mapping NOT exported.
    #
    # `unattributed` keeps its own label rather than being handed a "Ward n".
    # Rows with no ward are events we could not place, and presenting them as a
    # ward would invent a ward that does not exist and hide a data-quality gap
    # inside a plausible-looking row — the reader must be able to see how much
    # of the volume is unplaced.
    pseudo: dict[str, str] = {}
    n = 0
    for name in by_ward_raw:
        if name == "unattributed":
            pseudo[name] = "unattributed"
            continue
        n += 1
        pseudo[name] = f"Ward {n}"

    outcomes: dict[str, int] = {}
    outcome_sources: dict[str, int] = {}
    for p in outcome_rows:
        outcomes[str(p.get("event_type") or "unspecified")] = \
            outcomes.get(str(p.get("event_type") or "unspecified"), 0) + 1
        src = str(p.get("source") or "unspecified")
        outcome_sources[src] = outcome_sources.get(src, 0) + 1

    adjudications: dict[str, int] = {}
    for p in adj_rows:
        v = str(p.get("verdict") or "unspecified")
        adjudications[v] = adjudications.get(v, 0) + 1

    return {
        "instrument_present": True,
        "append_only": append_only,
        "period": {"start": start, "end": end, "days": days},
        "events_total": sum(by_kind.values()),
        "episodes_total": int(episodes["n"] if episodes else 0),
        "corrections": int(corrections["n"] if corrections else 0),
        "by_kind": by_kind,
        "by_lane": by_lane,
        "by_ward": {pseudo[k]: v for k, v in by_ward_raw.items()},
        "ward_pseudonyms": pseudo,
        "outcomes": outcomes,
        "outcome_sources": outcome_sources,
        "adjudications": adjudications,
        "coverage": _coverage_from(none_rows, outcome_rows, by_ward_raw, days,
                                   start, end),
    }


def _empty_coverage(days: int) -> dict[str, Any]:
    return {
        "covered": 0,
        "expected": 0,
        "display": "0/0",
        "uncovered": 0,
        "complete": False,
        "shifts_per_day_assumed": SHIFTS_PER_DAY,
        "note": "A ward-shift with neither a recorded outcome nor a 'no deterioration' confirmation is excluded from every denominator and counted here as uncovered time.",
    }


def _coverage_from(none_rows: list[dict], outcome_rows: list[dict],
                   by_ward_raw: dict[str, int], days: int,
                   start: str, end: str) -> dict[str, Any]:
    """Ward-shift coverage: the thing everybody forgets.

    `outcome.none_confirmed` is what distinguishes "nothing happened on that
    shift" from "nobody logged that shift". A ward-shift with NEITHER a recorded
    outcome nor a none-confirmation is uncovered time, and uncovered time is
    excluded from denominators rather than silently counted as clean.

    A confirmation is fetched by WHEN IT WAS SUBMITTED but counted by WHICH
    SHIFT it describes. The two differ whenever a shift is back-filled the next
    morning, which is the normal case; without the second filter a confirmation
    entered today for a shift outside the window would inflate coverage inside
    it.
    """
    win_start, win_end = start[:10], end[:10]
    covered: set[tuple[str, str, str]] = set()
    for p in none_rows:
        day = str(p.get("period_start") or "")[:10]
        if day and not (win_start <= day <= win_end):
            continue
        covered.add((
            str(p.get("ward") or p.get("_ward") or "unattributed"),
            day,
            str(p.get("shift") or "unspecified"),
        ))
    # A recorded outcome also proves someone was looking at that ward that day,
    # but it cannot name the shift, so it covers the DAY, not a specific shift.
    outcome_days: set[tuple[str, str]] = set()
    for p in outcome_rows:
        outcome_days.add((str(p.get("_ward") or "unattributed"), ""))

    wards_n = max(1, len(by_ward_raw)) if by_ward_raw else 0
    expected = wards_n * days * SHIFTS_PER_DAY
    n_covered = len(covered)
    return {
        "covered": n_covered,
        "expected": expected,
        "display": f"{n_covered}/{expected}",
        "uncovered": max(0, expected - n_covered),
        "complete": expected > 0 and n_covered >= expected,
        "wards_observed": len(by_ward_raw),
        "outcome_confirmed_ward_days": len(outcome_days),
        "shifts_per_day_assumed": SHIFTS_PER_DAY,
        "note": "A ward-shift with neither a recorded outcome nor a 'no deterioration' confirmation is excluded from every denominator and counted here as uncovered time.",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Layer (a): the metric whitelist
# ══════════════════════════════════════════════════════════════════════════════
#: Exactly the fields of `ScorecardMetric` (src/api/scorecard.ts) that are
#: aggregate by nature. Anything else a metric carries is dropped unread.
_METRIC_FIELDS = (
    "id", "label", "definition", "state", "value", "unit", "display",
    "numerator", "denominator", "denominator_label", "alternative_denominators",
    "n_observed", "n_required", "spread", "caveat", "blockers", "is_proxy",
    "assumptions", "adjudication", "disagreements", "segments", "phase",
    "misses", "sensitivity", "witnessed_only", "provisional", "provisional_reason",
)
# NOT whitelisted, deliberately: `detail`. The sibling package uses it as a
# free-form bag (withheld values, working sets), and a free-form bag is exactly
# what must not be forwarded into an artefact that leaves the building.


def _project_metric(metric: Any) -> dict[str, Any]:
    if not isinstance(metric, dict):
        return {}
    return {k: metric.get(k) for k in _METRIC_FIELDS if k in metric}


def _metric_list(metrics: Any) -> list[dict[str, Any]]:
    """Accept either shape of `metrics`.

    `shadow_scorecard_service.scorecard()` returns a DICT keyed by metric id;
    the client contract (src/api/scorecard.ts::metricsFrom) accepts a dict or a
    list. The export takes both rather than pinning the sibling package to one,
    and always emits a list so the CSV has a stable row order.
    """
    if isinstance(metrics, dict):
        items = [v if isinstance(v, dict) else {} for v in metrics.values()]
    elif isinstance(metrics, list):
        items = metrics
    else:
        return []
    out = [_project_metric(m) for m in items]
    return [m for m in out if m]


def _orb_version() -> str:
    v = os.getenv("ORB_VERSION")
    if v:
        return v
    try:
        import main  # already resident; lazy to avoid an import cycle
        return str(getattr(main.app, "version", "") or "unknown")
    except Exception:
        return "unknown"


# ══════════════════════════════════════════════════════════════════════════════
#  Integrity: the one claim the document must not make about itself
# ══════════════════════════════════════════════════════════════════════════════
_NOT_EVIDENTIARY = (
    "NOT EVIDENTIARY. The append-only triggers on the shadow-event log "
    "(trg_shadow_no_update, trg_shadow_no_delete) were NOT in place when this export was "
    "produced, so rows in that log could have been edited or deleted. Nothing in this "
    "document can be relied on as a record of what happened. Do not submit it as evidence."
)
_UNVERIFIED = (
    "UNVERIFIED. This export could not establish whether the shadow-event log's append-only "
    "protection was in place when it was produced, so it does not claim to be evidentiary. "
    "Treat the figures below as indicative until the instrument's integrity is confirmed on "
    "the appliance itself."
)
_NO_INSTRUMENT = (
    "NO INSTRUMENT. The shadow-event log is not present on this appliance: nothing has been "
    "recorded here, so there is no record whose integrity could be attested. The counts below "
    "are absences, not observations."
)
_EVIDENTIARY = (
    "The shadow-event log was append-only when this export was produced: both database "
    "triggers that block UPDATE and DELETE (trg_shadow_no_update, trg_shadow_no_delete) were "
    "verified present at export time. Corrections are made by appending a superseding row, "
    "and the number of them is stated in provenance.row_counts."
)


def _evidentiary_status(verified: Optional[bool], claimed: Optional[bool],
                        instrument_present: bool,
                        instrument_warnings: Any) -> dict[str, Any]:
    """Combine the export's own trigger check with the aggregation layer's.

    A `False` from EITHER source wins: two independent readings of the same
    property should agree, and where they do not, the reading that says the log
    is not protected is the one a reader must be shown.
    """
    if verified is False or claimed is False:
        append_only: Optional[bool] = False
    elif verified is True or claimed is True:
        append_only = True
    else:
        append_only = None

    if append_only is False:
        status, statement = "not_evidentiary", _NOT_EVIDENTIARY
    elif not instrument_present:
        status, statement = "no_instrument", _NO_INSTRUMENT
    elif append_only is True:
        status, statement = "evidentiary", _EVIDENTIARY
    else:
        status, statement = "unverified", _UNVERIFIED

    if verified is not None:
        verified_by = ("append-only triggers read from sqlite_master by the export itself, "
                       "at export time")
    elif claimed is not None:
        verified_by = ("reported by the aggregation layer; the export could not re-read the "
                       "triggers itself")
    else:
        verified_by = "not checked, neither the export nor the aggregation layer could read the triggers"

    warnings = [w for w in (instrument_warnings or []) if isinstance(w, str)][:20]
    return {
        "status": status,
        "evidentiary": status == "evidentiary",
        "append_only": append_only,
        "append_only_verified_by": verified_by,
        "append_only_self_checked": verified,
        "append_only_reported_by_aggregation_layer": claimed,
        "statement": statement,
        "instrument_warnings": warnings,
        "note": ("This block is the export's statement about ITSELF and is deliberately not "
                 "a constant: an artefact that always certifies its own integrity certifies "
                 "nothing."),
    }


#: Metric fields that are counts of observed events or people, scanned to decide
#: whether the document's own k rule actually holds. Everything else a metric
#: carries is a measurement (a duration, a percentile, a rate) or a threshold.
_METRIC_COUNT_FIELDS = ("numerator", "denominator", "n_observed", "disagreements", "misses")
_ADJUDICATION_COUNT_FIELDS = ("adjudicated", "total", "adjudicators")


def _exact_small_counts(metrics: list[dict], denominators: dict) -> list[str]:
    """Every published field holding an exact observation count below k.

    Named, not hidden. The metrics cannot be masked without destroying the only
    thing that makes a rate checkable — and their own caveat/assumptions prose
    restates the same counts in words, so masking the numeric field would buy a
    claim of protection this document could not honour.
    """
    found: list[str] = []
    for metric in metrics:
        mid = str(metric.get("id") or "metric")
        for field in _METRIC_COUNT_FIELDS:
            if _is_small_exact_count(metric.get(field)):
                found.append(f"scorecard.metrics[{mid}].{field}")
        adjudication = metric.get("adjudication")
        if isinstance(adjudication, dict):
            for field in _ADJUDICATION_COUNT_FIELDS:
                if _is_small_exact_count(adjudication.get(field)):
                    found.append(f"scorecard.metrics[{mid}].adjudication.{field}")
    for mid, block in (denominators or {}).items():
        if not isinstance(block, dict):
            continue
        for field in ("denominator", "n_observed"):
            if _is_small_exact_count(block.get(field)):
                found.append(f"provenance.denominators.{mid}.{field}")
    return sorted(set(found))


# ══════════════════════════════════════════════════════════════════════════════
#  The export
# ══════════════════════════════════════════════════════════════════════════════
def build_export(
    days: int = 30,
    ward: Optional[str] = None,
    *,
    scorecard: Optional[dict] = None,
    instrument: Optional[dict] = None,
    actor_role: Optional[str] = None,
    aggregates: Optional[dict] = None,
) -> dict[str, Any]:
    """Assemble the de-identified aggregate export.

    `scorecard` is injected rather than imported so this module has no build-time
    dependency on `shadow_scorecard_service`: the export can still state the
    instrument's shape, volume, coverage and provenance on an appliance where
    the aggregation layer is absent, and it says so instead of failing.

    SYNC and heavy — worker thread only.
    """
    agg = aggregates if aggregates is not None else collect_aggregates(days, ward)
    period = agg["period"]
    k = small_cell_k()

    breakdowns = {
        "by_kind": agg["by_kind"],
        "by_lane": agg["by_lane"],
        "by_ward": agg["by_ward"],
        "outcomes_by_type": agg["outcomes"],
        "outcomes_by_source": agg["outcome_sources"],
        "adjudications_by_verdict": agg["adjudications"],
    }
    displays: dict[str, dict[str, str]] = {}
    suppressed_cells = 0
    degenerate: list[str] = []
    for name, counts in breakdowns.items():
        displays[name], n_masked = suppress_counts(counts)
        suppressed_cells += n_masked
        if n_masked and is_degenerate(counts):
            degenerate.append(name)

    metrics = _metric_list((scorecard or {}).get("metrics"))

    denominators = {}
    for m in metrics:
        if m.get("denominator") is not None:
            denominators[str(m.get("id"))] = {
                "denominator": m.get("denominator"),
                "label": m.get("denominator_label"),
                "n_observed": m.get("n_observed"),
                "n_required": m.get("n_required"),
                "state": m.get("state"),
            }

    inst = instrument if isinstance(instrument, dict) else {}
    evidentiary = _evidentiary_status(
        verified=agg.get("append_only"),
        claimed=inst.get("append_only") if "append_only" in inst else None,
        instrument_present=bool(agg["instrument_present"]),
        instrument_warnings=inst.get("warnings"),
    )
    exact_small = _exact_small_counts(metrics, denominators)

    body: dict[str, Any] = {
        "export_type": EXPORT_TYPE,
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        # Deliberately the THIRD key in the document, before any number: whether
        # these figures are a record or a claim is the first thing a reader has
        # to know, and it must not be buried in provenance.
        "evidentiary_status": evidentiary,
        "scorecard": {
            "phase": (scorecard or {}).get("phase"),
            "phase_label": (scorecard or {}).get("phase_label"),
            "metrics": metrics,
            "metrics_available": bool(metrics),
            "note": None if metrics else
                    "The aggregation layer did not supply metrics for this export. "
                    "Instrument volume, coverage and provenance below are still exact.",
        },
        "instrument": {
            "source": "shadow_events_current",
            "schema_v": inst.get("schema_v", INSTRUMENT_SCHEMA_V if agg["instrument_present"] else None),
            "present": agg["instrument_present"],
            # The real, checked value — never a constant. See evidentiary_status.
            "append_only": evidentiary["append_only"],
            "append_only_enforced_by": (
                "database triggers (trg_shadow_no_update, trg_shadow_no_delete), verified "
                "present at export time"
                if evidentiary["append_only"] is True else
                "NOTHING: the append-only triggers (trg_shadow_no_update, "
                "trg_shadow_no_delete) were absent when this export was produced"
                if evidentiary["append_only"] is False else
                "UNKNOWN: the append-only triggers could not be checked when this export "
                "was produced"
            ),
            # Volume totals go through the same k as the breakdowns that sum to
            # them; leaving them exact would un-mask counts.* by inspection.
            "events_total": mask_count(agg["events_total"]),
            "episodes_total": mask_count(agg["episodes_total"]),
            "corrections": mask_count(agg["corrections"]),
            "dropped_events": inst.get("dropped_events"),
            "error_events": inst.get("error_events"),
            "lag_ms_p50": inst.get("lag_ms_p50"),
            "lag_ms_p95": inst.get("lag_ms_p95"),
        },
        "counts": displays,
        "coverage": agg["coverage"],
        "provenance": {
            "orb_version": _orb_version(),
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "exported_by_role": actor_role or "unknown",
            "period": period,
            "ward_filter": "single ward (pseudonymised)" if ward else "all observed wards",
            "wards_observed": agg["coverage"].get("wards_observed", 0),
            # Masked at the SAME k as counts.*, because these are the totals of
            # those very breakdowns: publishing `outcomes_recorded: 1` beside
            # `outcomes_by_type: {"icu_transfer": "<5"}` defeats the mask inside
            # one document, which is worse than not masking at all.
            "row_counts": {
                "shadow_events_current": mask_count(agg["events_total"]),
                "episodes": mask_count(agg["episodes_total"]),
                "corrections_superseding_rows": mask_count(agg["corrections"]),
                "outcomes_recorded": mask_count(sum(agg["outcomes"].values())),
                "adjudications_recorded": mask_count(sum(agg["adjudications"].values())),
            },
            "row_counts_note": (
                f"Counts below k={k} are shown as \"<{k}\"; every other count here is exact."
            ),
            "denominators": denominators or {
                "note": "No metric supplied a denominator; nothing in this export is a rate.",
            },
            "coverage": {
                "ward_shifts": agg["coverage"].get("display"),
                "complete": agg["coverage"].get("complete"),
                "uncovered_ward_shifts": agg["coverage"].get("uncovered"),
                "shifts_per_day_assumed": SHIFTS_PER_DAY,
            },
            "suppression": {
                "rule": "small-cell k-anonymity with complementary suppression",
                "k": k,
                "cells_suppressed": suppressed_cells,
                "masked_as": f"<{k}",
                # WHERE the rule runs, and — the part an export must not leave
                # implied — where it does not.
                "applies_to": [
                    "counts.* (every breakdown)",
                    "provenance.row_counts.*",
                    "instrument.events_total, instrument.episodes_total, instrument.corrections",
                ],
                "not_applied_to": [
                    {"path": "scorecard.metrics[*].numerator / denominator / n_observed / disagreements",
                     "why": "A rate whose denominator is masked cannot be checked by the reader, and masking it would make this document unfalsifiable rather than private."},
                    {"path": "scorecard.metrics[*].caveat / assumptions / alternative_denominators / provisional_reason",
                     "why": "Method notes restate the same counts in prose. They are not masked, so masking the numeric fields beside them would buy a claim of protection this export could not honour."},
                    {"path": "provenance.denominators.*",
                     "why": "Same reason: this block exists so a reader can reconstruct every denominator."},
                    {"path": "coverage.* and provenance.coverage.*",
                     "why": "Ward-shifts are periods of time, not individuals."},
                    {"path": "instrument.dropped_events / error_events / lag_ms_*",
                     "why": "Instrument-failure counters and latencies. Hiding them would hide the facts most likely to weaken a claim made from this document."},
                ],
                # Computed, not asserted: does the k rule actually hold across
                # the WHOLE document, or is it defeated by a field this export
                # publishes exactly? Named fields, so the reader can check.
                "k_anonymity_achieved": not exact_small,
                "exact_counts_below_k_in": exact_small,
                "k_anonymity_note": (
                    f"This export publishes {len(exact_small)} field(s) holding an exact "
                    f"observation count below k={k} (named above), so a cell masked as "
                    f"\"<{k}\" elsewhere in this document may be recoverable from them. The "
                    "export states this rather than claiming a control it is not providing."
                ) if exact_small else (
                    f"No field in this export publishes an exact observation count below "
                    f"k={k}, so the masked cells are not recoverable from elsewhere in this "
                    "document."
                ),
                # Stated, not hidden: where a breakdown has a single populated
                # cell, that cell equals the breakdown's total and the mask is
                # cosmetic. Claiming otherwise would make the manifest wrong.
                "masking_is_cosmetic_in": degenerate,
                "masking_note": (
                    "The listed breakdowns have a single populated cell, so the masked "
                    "value equals the breakdown total and can be inferred. They are "
                    "masked for consistency, not for protection."
                ) if degenerate else None,
            },
            "removed": deidentification_manifest(),
            "limits": [
                "Aggregate only. This export contains no patient-level, episode-level or event-level rows.",
                "Counts are suppressed below k and are therefore not additive; do not reconstruct totals from breakdowns.",
                "Shadow-mode observations are not a randomised comparison and do not establish causation.",
                "Uncovered ward-shifts are excluded from denominators, not counted as event-free.",
                ("Small-cell suppression covers the breakdowns and row counts only. Metric "
                 "denominators, observation counts and method notes are published exactly; "
                 "see provenance.suppression.k_anonymity_achieved for whether that defeats "
                 "the mask in this particular export."),
            ] + ([] if evidentiary["evidentiary"] else [evidentiary["statement"]]),
        },
    }

    clean = _scrub(body)
    clean["provenance"]["integrity"] = {
        "algorithm": "sha256",
        "digest": _digest(clean),
        "covers": "every field of this document except provenance.integrity itself",
    }
    return clean


def _digest(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
#  CSV — long format, because provenance must survive the serialisation
# ══════════════════════════════════════════════════════════════════════════════
def to_csv(export: dict[str, Any]) -> str:
    """Flatten to `section,key,subkey,value`.

    Long format, not a metric-per-column table, for one reason: a wide CSV has
    nowhere to put provenance, and an export that drops its provenance on the
    way to a spreadsheet has stopped being evidence. Every row of the JSON
    document appears here, including the manifest and the integrity digest.
    """
    import csv

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["section", "key", "subkey", "value"])

    def emit(section: str, key: str, subkey: str, value: Any) -> None:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, default=str)
        w.writerow([section, key, subkey, "" if value is None else str(value)])

    def walk(section: str, prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (dict, list)) and not _is_leaf(v):
                    walk(section, f"{prefix}.{k}" if prefix else str(k), v)
                else:
                    emit(section, prefix or section, str(k), v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, dict):
                    for k2, v2 in v.items():
                        emit(section, f"{prefix}[{i}]", str(k2), v2)
                else:
                    emit(section, prefix, str(i), v)
        else:
            emit(section, prefix or section, "", node)

    for section in ("export_type", "export_schema_version"):
        emit("document", section, "", export.get(section))
    # Before any number: a spreadsheet reader must meet the integrity statement
    # first, not scroll past four hundred rows of counts to find it.
    walk("evidentiary_status", "", export.get("evidentiary_status", {}))
    for section in ("provenance", "coverage", "instrument", "counts", "scorecard"):
        walk(section, "", export.get(section, {}))
    return buf.getvalue()


def _is_leaf(v: Any) -> bool:
    """A short flat list (e.g. `blockers`) is written as one cell rather than
    exploded into rows — it reads better and stays lossless via JSON."""
    return isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v)


def filename(days: int, fmt: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"orb-shadow-scorecard-{days}d-{stamp}.{fmt}"
