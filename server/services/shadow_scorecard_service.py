"""Orb — Shadow-mode composite scorecard: the READ side of the pilot instrument.

`shadow_service` writes `shadow_events`; this module is the only thing that
turns those rows into the four numbers a hospital CIO is asked to trust. It
computes, it never writes, and it is the artefact the Week-13 decision rests on
— so the honesty rules below are load-bearing code, not commentary.

── Eleven rules, in priority order ───────────────────────────────────────────

 1. **Read `shadow_events_current`, never the base table.** The view resolves
    corrections (a superseded row vanishes from every metric while surviving in
    the base table for audit). One `SELECT ... FROM shadow_events` here would
    silently double-count every adjudication that was ever revised.

 2. **Sync only — never on the event loop.** Every entry point here is a bounded
    but heavy scan, and since encryption-at-rest each page I/O pays
    HMAC-SHA512 + AES. Route handlers must be `def`, not `async def`. This repo
    has already shipped one event-loop wedge; this must not be the second.

 3. **Never a bare 0 %.** Every metric carries a `state`
    (`reported` / `insufficient_data` / `unadjudicated` / `not_computable`), an
    `n_observed`, an `n_required` and its denominator. A scorecard reporting
    "0 % false alerts" on its first morning, having seen four alerts, is the
    single worst failure mode of this file. An EMPTY database must therefore
    produce a complete, explicitly-empty scorecard — never an exception, never a
    zero dressed as a finding.

 4. **Medians, computed in Python.** SQLite has no median. Detection lead time
    is right-skewed and one 23-hour lead dominates a mean, so every distribution
    is reported as median + IQR from a materialised list.

 5. **Negative leads are reported, not clipped.** A negative lead is a case
    where the clinician was ahead of the machine. Clipping them is the easiest
    way to make this artefact indefensible, so the alert search window extends
    PAST the outcome (`late_window_hours`): without that, every clinician-ahead
    case would be silently miscounted as a miss, conflating "Orb was late" with
    "Orb never saw it".

 6. **A miss is a row, not an absence.** A confirmed deterioration with no
    qualifying alert is counted in the denominator and reported both as a miss
    count and as `sensitivity = 1 − misses/outcomes`. Never imputed, never
    dropped.

 7. **Phases are never pooled.** Phase A (silent) and Phase C (clinicians can
    see Orb) measure different things — after Phase B the scorecard measures
    Orb-plus-staff. Agreement is reported per phase segment and the pooled
    figure is explicitly `None`.

 8. **Coverage gates the time-based denominators.** `outcome.none_confirmed` is
    what distinguishes "nothing happened" from "nobody logged it". A ward-shift
    with NEITHER an `outcome.recorded` nor an `outcome.none_confirmed` is
    excluded from the episode and bed-day denominators and surfaced as
    uncovered time on the face of the scorecard.

 9. **Simulator-lane rows are not evidence.** The appliance ships with a
    simulator, and counting simulated alerts as pilot evidence would be a lie to
    the CIO. `lane = 'simulator'` is excluded from every metric by default
    (`ORB_SHADOW_SCORECARD_INCLUDE_SIMULATOR=1` to override, e.g. for a demo)
    and the excluded volume is reported.

10. **One vocabulary, defined once — `services/shadow_verdicts.py`.** This file
    does not define what a verdict means; it imports the definition the API
    writes with. A local copy of that decision is what silently discarded EVERY
    clinician adjudication ever recorded through the route: the API wrote
    `false_positive`, this module recognised only `false_alert`, and the metric
    reported `unadjudicated` for ever without raising or logging once.

11. **The unit on the label is the unit in the numerator.** The false-alert
    headline is per ALERT, because `unit` says "false alerts per occupied
    bed-day" and the definition claims to measure how often a nurse is
    interrupted. Counting EPISODES under that label understated the burden by
    the re-fire ratio — 49x in live pilot-shaped data, always in Orb's favour.
    Both figures are computed; the per-episode one is an explicitly-labelled
    alternative denominator and never the headline.

── Public API (this is what routes / export call) ────────────────────────────
    scorecard(days, ward, ...)      → the full contract payload (src/api/scorecard.ts)
    instrument_health(...)          → the instrument's report on itself
    episode_detail(episode_id, ...) → ordered rows + derived summary for one episode
    list_episodes(days, ward, ...)  → episode index for the drill-down

All four are `def`. All four accept `db=` so a caller that already holds a
connection does not open a second one.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any

from database import db_session

from services import shadow_verdicts

logger = logging.getLogger("orb.shadow.scorecard")

# ── Controlled vocabulary ────────────────────────────────────────────────────
#: The clinical events that count as a confirmed deterioration. Anything else in
#: an `outcome.recorded` row is ignored by lead time rather than quietly folded
#: in — the definition of "escalation" is the champion's, and it is fixed here.
OUTCOME_EVENT_TYPES = frozenset(
    {
        "icu_transfer",
        "rapid_response_call",
        "code_blue",
        "death",
        "unplanned_escalation_of_care",
    }
)

#: Only these alert levels count as "Orb flagged this patient". A 'stable' row
#: is not a flag.
ALERT_LEVELS = frozenset({"elevated", "critical"})

#: Outcome provenance. `chart_review` is systematically biased LATE (the note is
#: written after the fact), which INFLATES lead time — so the witnessed-only
#: figure is reported as the conservative one.
OUTCOME_SOURCES = ("witnessed", "chart_review", "handover")

#: Adjudication verdicts. NOT DEFINED HERE — `services/shadow_verdicts.py` is
#: the single definition, imported by this module AND by `routes/shadow_routes`,
#: because two independent copies is exactly how every clinician adjudication
#: came to be silently discarded (the route wrote `false_positive`; this file
#: only recognised `false_alert`, called it unrecognised, and the metric could
#: never report). Artefact / indeterminate are EXCLUDED from the numerator and
#: denominator and reported separately — folding them into either side moves the
#: rate in a direction nobody chose.
#:
#: These names are derived views over that one table, kept because they document
#: the arithmetic in place. They cannot drift: they are computed from it.
CANONICAL_VERDICTS = shadow_verdicts.CANONICAL_VERDICTS
VERDICT_TRUE = frozenset(
    v for v in shadow_verdicts.ACCEPTED_VERDICTS if shadow_verdicts.classify(v) == "true"
)
VERDICT_FALSE = frozenset(
    v for v in shadow_verdicts.ACCEPTED_VERDICTS if shadow_verdicts.classify(v) == "false"
)
VERDICT_EXCLUDED = frozenset(
    v for v in shadow_verdicts.ACCEPTED_VERDICTS if shadow_verdicts.classify(v) == "excluded"
)

#: A clinician action that can agree with a Sage proposal. `action.declined` is a
#: pair too — it is simply never a match.
CLINICIAN_ACTION_KINDS = frozenset({"action.approved", "action.declined", "clinical.action"})

METRIC_IDS = ("detection_lead_time", "false_alert_rate", "agreement", "time_saved")

#: Floors below which a metric prints its n but NOT a number. Module-level so a
#: deployment (or a test) can move them explicitly; per-metric env override is
#: ORB_SCORECARD_MIN_N_<METRIC_ID>.
MIN_N: dict[str, int] = {
    "detection_lead_time": 5,  # leads (outcomes matched to an alert)
    "false_alert_rate": 10,  # adjudicated episodes
    "agreement": 10,  # proposal/action pairs IN THE HEADLINE PHASE
    "time_saved": 10,  # timed Sage answers
}

PHASE_LABELS = {
    "A": "Phase A: Orb output not shown to clinicians",
    "B": "Phase B: champion-only",
    "C": "Phase C: phased clinician access",
}

#: What the export removes. Declared HERE so the scorecard's own statement of
#: de-identification and `shadow_export_service`'s behaviour cannot drift apart:
#: the export service must implement exactly this list.
DEIDENTIFICATION: list[dict[str, str | None]] = [
    {
        "field": "patient_id",
        "treatment": "hashed",
        "note": "Salted per export; the salt is not exported, so the mapping cannot be reversed off-appliance.",
    },
    {
        "field": "phi_json",
        "treatment": "dropped",
        "note": "Free clinical text never leaves the appliance in any form.",
    },
    {
        "field": "actor_user_id",
        "treatment": "hashed",
        "note": "Staff identity is pseudonymised; the adjudicator's ROLE is retained.",
    },
    {
        "field": "ward",
        "treatment": "coarsened",
        "note": "Two wards in the pilot makes ward a quasi-identifier; exported as ward_1 / ward_2.",
    },
    {"field": "encounter_id", "treatment": "removed", "note": None},
    {
        "field": "source_correlation_id",
        "treatment": "removed",
        "note": "Joins back to identified audit rows on the appliance.",
    },
    {
        "field": "occurred_at",
        "treatment": "retained",
        "note": "Timing IS the finding; retained at millisecond resolution.",
    },
]

METRIC_SPEC: dict[str, dict[str, Any]] = {
    "detection_lead_time": {
        "label": "Detection lead time",
        "unit": "minutes",
        "definition": (
            "Median interval (with IQR) between Orb first flagging a patient at "
            "elevated or critical and a confirmed deterioration. ICU transfer, "
            "rapid-response call, code blue, unplanned escalation, or death."
        ),
        "denominator_label": "confirmed deterioration outcomes",
        "caveat": (
            "It cannot show that acting on the flag would have helped. A flag three "
            "hours before a transfer is evidence Orb saw something; it is not evidence "
            "that intervening then would have changed the outcome."
        ),
        "is_proxy": False,
    },
    "false_alert_rate": {
        "label": "False-alert rate",
        "unit": "false alerts per occupied bed-day",
        "definition": (
            "ALERTS: every alert Orb raised, including each repeat within one "
            "episode, belonging to episodes a clinician adjudicated FALSE, per "
            "occupied bed-day of covered ward-shift time. One alert is one "
            "interruption, which is the thing a nurse actually experiences; an "
            "episode is one clinical concern and may contain dozens of alerts, so "
            "the per-episode figure is reported alongside as an alternative "
            "denominator and is always the smaller number."
        ),
        "denominator_label": "occupied bed-days (covered ward-shifts only)",
        "caveat": (
            "It cannot show alert fatigue. A tolerable rate on paper can still be "
            "intolerable at 3 a.m. on a short-staffed night. Unadjudicated episodes are "
            "reported as unadjudicated, never as correct. Adjudication is per EPISODE "
            "and the rate is per ALERT: every alert in an episode judged false is "
            "counted false, which is the conservative reading."
        ),
        "is_proxy": False,
    },
    "agreement": {
        "label": "Agreement",
        "unit": "proportion",
        "definition": (
            "How often the action class Sage proposed matched the action class the "
            "clinician actually took. Reported per shadow-mode phase and never pooled."
        ),
        "denominator_label": "proposal/action pairs in the reported phase",
        "caveat": (
            "Agreement is not correctness. A high figure is equally consistent with "
            '"Sage is good" and with "Sage is a fluent restatement of the obvious". '
            "The disagreements are the informative rows. Phase A and Phase C measure "
            "different things and are never pooled."
        ),
        "is_proxy": False,
    },
    "time_saved": {
        "label": "Time saved (proxy)",
        "unit": "hours",
        "definition": (
            "A PROXY: measured Sage answer latency against a stated manual-lookup "
            "baseline, multiplied by the measured number of answers. The genuinely "
            "measured part is the latency; the baseline is an assumption and is printed "
            "as one."
        ),
        "denominator_label": "timed Sage answers",
        "caveat": (
            "It cannot measure clinical time saved; that needs a controlled comparison "
            "this pilot is not running. It is the figure most likely to be quoted back "
            "in a fundraising context and the one with the weakest foundation."
        ),
        "is_proxy": True,
    },
}


# ── Configuration (read per call so a test or an operator can move it) ───────
def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _lookback_hours() -> int:
    """How far BEFORE a confirmed outcome an alert may sit and still count."""
    return _env_int("ORB_SCORECARD_LOOKBACK_HOURS", 24, 1)


def _late_window_hours() -> int:
    """How far AFTER the outcome an alert is still attributed to it — i.e. how
    late Orb may be and still be measured as late rather than counted as blind.
    This is what makes a negative lead expressible at all."""
    return _env_int("ORB_SCORECARD_LATE_WINDOW_HOURS", 24, 0)


def _manual_lookup_seconds() -> float:
    """STATED ASSUMPTION: how long the same lookup takes a clinician manually."""
    return _env_float("ORB_SCORECARD_MANUAL_LOOKUP_SECONDS", 240.0, 0.0)


def _max_events() -> int:
    return _env_int("ORB_SCORECARD_MAX_EVENTS", 250_000, 1000)


def _coverage_threshold() -> float:
    return _env_float("ORB_SCORECARD_COVERAGE_THRESHOLD", 0.9, 0.0)


def _include_simulator_default() -> bool:
    return os.getenv("ORB_SHADOW_SCORECARD_INCLUDE_SIMULATOR", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _min_n(metric_id: str) -> int:
    return _env_int(f"ORB_SCORECARD_MIN_N_{metric_id.upper()}", MIN_N.get(metric_id, 1), 1)


def _shifts() -> list[tuple[dt_time, float]]:
    """Ward shifts as (start-of-shift, length in hours). Default 3 x 8h."""
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


# ── Phases ───────────────────────────────────────────────────────────────────
class PhaseSchedule:
    """When each shadow-mode phase started.

    Configured, not inferred: `ORB_SHADOW_PHASE_SCHEDULE` is either JSON
    (``[{"phase":"A","start":"2026-08-01T00:00:00+00:00"}, ...]``) or the compact
    form ``A@2026-08-01T00:00:00Z,C@2026-09-15T00:00:00Z``. With nothing
    configured every event is Phase A — the silent phase is the default because
    assuming the *later* phase would quietly relabel clean data as contaminated.
    """

    def __init__(self, entries: list[tuple[datetime | None, str]] | None = None):
        cleaned = [(s, p) for (s, p) in (entries or []) if p]
        cleaned.sort(key=lambda e: e[0] or datetime.min.replace(tzinfo=UTC))
        self.entries = cleaned

    @classmethod
    def from_env(cls) -> PhaseSchedule:
        raw = (os.getenv("ORB_SHADOW_PHASE_SCHEDULE") or "").strip()
        entries: list[tuple[datetime | None, str]] = []
        if raw.startswith("["):
            try:
                for item in json.loads(raw):
                    entries.append((_dt(item.get("start")), str(item.get("phase") or "").strip()))
            except Exception:
                logger.warning(
                    "scorecard: unparsable ORB_SHADOW_PHASE_SCHEDULE; defaulting", exc_info=True
                )
                entries = []
        elif raw:
            for chunk in raw.split(","):
                phase, _, start = chunk.strip().partition("@")
                if phase.strip():
                    entries.append((_dt(start.strip()) if start.strip() else None, phase.strip()))
        if not entries:
            entries = [(None, (os.getenv("ORB_SHADOW_PHASE") or "A").strip() or "A")]
        return cls(entries)

    def phase_at(self, ts: datetime | None) -> str:
        if not self.entries:
            return "A"
        current = self.entries[0][1]
        if ts is None:
            return current
        for start, phase in self.entries:
            if start is None or start <= ts:
                current = phase
            else:
                break
        return current

    def current(self, now: datetime | None = None) -> str:
        return self.phase_at(now or datetime.now(UTC))

    @staticmethod
    def label(phase: str) -> str:
        return PHASE_LABELS.get(phase, f"Phase {phase}")


# ── Time ─────────────────────────────────────────────────────────────────────
def _dt(value: Any) -> datetime | None:
    """Parse any timestamp Orb writes into an aware UTC datetime.

    `shadow_events` is single-format by construction, but `patients`,
    `discharge_summaries` and `vitals` predate that discipline and carry
    ``CURRENT_TIMESTAMP`` strings at second resolution with no zone.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if len(text) == 10:
        text += "T00:00:00"
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    """The one format `shadow_events` uses — so string comparison is valid."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _overlap_hours(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0.0, (hi - lo).total_seconds() / 3600.0)


# ── Percentiles, in Python, because SQLite has no median ─────────────────────
def _percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (the numpy default), so a
    hand-computed expectation in a test is reproducible: idx = (n-1) * q."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def _spread(values: list[float]) -> dict[str, float | None] | None:
    if not values:
        return None
    return {
        "p25": _percentile(values, 0.25),
        "p50": _percentile(values, 0.50),
        "p75": _percentile(values, 0.75),
    }


def _fmt_minutes(value: float | None) -> str | None:
    """ "-45 m", "3 h 07 m". The sign survives: a negative lead is a finding."""
    if value is None:
        return None
    sign = "-" if value < 0 else ""
    total = abs(value)
    if total < 60:
        return f"{sign}{total:.0f} m"
    hours, minutes = divmod(int(round(total)), 60)
    return f"{sign}{hours} h {minutes:02d} m"


def _fmt_pct(numerator: int, denominator: int) -> str | None:
    if denominator <= 0:
        return None
    return f"{100.0 * numerator / denominator:.0f}% ({numerator}/{denominator})"


# ── Row loading ──────────────────────────────────────────────────────────────
_SCAN_COLUMNS = (
    "id, episode_id, kind, occurred_at, recorded_at, lane, patient_id, ward, "
    "actor_user_id, actor_role, subject_type, subject_id, payload_json, "
    "supersedes_event_id, correction_reason"
)


def _row_to_event(row: Any) -> dict:
    event = dict(row)
    raw = event.pop("payload_json", None)
    try:
        payload = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        payload = {}
    event["payload"] = payload if isinstance(payload, dict) else {}
    event["_t"] = _dt(event.get("occurred_at"))
    return event


def _instrument_present(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = 'shadow_events_current'"
    ).fetchone()
    return row is not None


def _load_events(
    conn, start: datetime, end: datetime, ward: str | None, include_simulator: bool
) -> tuple[list[dict], bool]:
    """One bounded scan of the VIEW. Returns (events, truncated)."""
    cap = _max_events()
    sql = [
        f"SELECT {_SCAN_COLUMNS} FROM shadow_events_current WHERE occurred_at >= ? AND occurred_at < ?"
    ]
    params: list[Any] = [_iso(start), _iso(end)]
    if not include_simulator:
        sql.append("AND lane <> 'simulator'")
    if ward:
        sql.append("AND (ward = ? OR json_extract(payload_json, '$.ward') = ?)")
        params.extend([ward, ward])
    sql.append("ORDER BY occurred_at, id LIMIT ?")
    params.append(cap + 1)
    rows = conn.execute(" ".join(sql), params).fetchall()
    truncated = len(rows) > cap
    return [_row_to_event(r) for r in rows[:cap]], truncated


def _load_late_adjudications(
    conn, start: datetime, ward: str | None, include_simulator: bool
) -> list[dict]:
    """Adjudication happens WEEKLY, after the fact. An episode inside the window
    is routinely adjudicated after the window closes, so the bounded scan above
    would systematically undercount adjudications and make every rate look less
    adjudicated than it is. Fetched separately, with no upper bound."""
    sql = [
        f"SELECT {_SCAN_COLUMNS} FROM shadow_events_current "
        "WHERE kind = 'adjudication.recorded' AND occurred_at >= ?"
    ]
    params: list[Any] = [_iso(start)]
    if not include_simulator:
        sql.append("AND lane <> 'simulator'")
    if ward:
        sql.append("AND (ward = ? OR ward IS NULL)")
        params.append(ward)
    sql.append("ORDER BY occurred_at, id LIMIT ?")
    params.append(_max_events())
    return [_row_to_event(r) for r in conn.execute(" ".join(sql), params).fetchall()]


# ── Coverage: the thing everybody forgets ────────────────────────────────────
_UNSPECIFIED_WARD = "(unspecified)"


def _event_ward(event: dict) -> str:
    return event.get("ward") or event["payload"].get("ward") or _UNSPECIFIED_WARD


def _ward_shift_windows(start: datetime, end: datetime, wards: list[str]) -> list[dict]:
    """Every ward-shift period overlapping the reporting window, clipped to it."""
    windows: list[dict] = []
    if not wards:
        return windows
    shifts = _shifts()
    # Shifts start on the WARD's clock. Laid on UTC midnight, a 07:00 shift in
    # an IST hospital ran 12:30-20:30 local, so no window matched a real shift
    # and the nurse's confirmations covered the wrong hours. Must stay the same
    # rule as shadow_routes._windows_around, which mints the period strings.
    from services.hospital_time import hospital_timezone

    tz = hospital_timezone()
    day = (start - timedelta(days=1)).astimezone(tz).date()
    last = end.astimezone(tz).date()
    while day <= last:
        for shift_start, length in shifts:
            w_start = datetime.combine(day, shift_start, tzinfo=tz).astimezone(UTC)
            w_end = w_start + timedelta(hours=length)
            if w_end <= start or w_start >= end:
                continue
            for ward in wards:
                windows.append(
                    {
                        "ward": ward,
                        "shift": shift_start.strftime("%H:%M"),
                        "start": max(w_start, start),
                        "end": min(w_end, end),
                        "covered": False,
                        "evidence": None,
                    }
                )
        day += timedelta(days=1)
    windows.sort(key=lambda w: (w["start"], w["ward"]))
    return windows


def _coverage(
    events: list[dict],
    start: datetime,
    end: datetime,
    wards: list[str],
    *,
    census: dict | None = None,
) -> dict:
    """A ward-shift is covered when SOMEONE said something about it: either a
    confirmed outcome, or a charge nurse confirming there wasn't one. Silence is
    not evidence of absence, and uncovered time is excluded, not assumed quiet.

    `wards` comes from the CENSUS, not from the events (see `_wards_in_scope`),
    so a ward that recorded nothing at all appears here as 0/n uncovered rather
    than not appearing. Those wards are named in `silent_wards`: a ward with
    admitted patients and no events is the single most important thing this
    metric can detect, because it means the instrument is dead there.
    """
    windows = _ward_shift_windows(start, end, wards)
    confirmations = [e for e in events if e["kind"] == "outcome.none_confirmed"]
    outcomes = [e for e in events if e["kind"] == "outcome.recorded"]

    for window in windows:
        for event in confirmations:
            if _event_ward(event) != window["ward"]:
                continue
            p_start = _dt(event["payload"].get("period_start")) or event["_t"]
            p_end = _dt(event["payload"].get("period_end")) or p_start
            if p_start is None:
                continue
            if p_end is None or p_end <= p_start:
                p_end = p_start
            if _overlap_hours(p_start, p_end, window["start"], window["end"]) > 0 or (
                window["start"] <= p_start < window["end"]
            ):
                window["covered"] = True
                window["evidence"] = "outcome.none_confirmed"
                break
        if window["covered"]:
            continue
        for event in outcomes:
            if _event_ward(event) != window["ward"] or event["_t"] is None:
                continue
            if window["start"] <= event["_t"] < window["end"]:
                window["covered"] = True
                window["evidence"] = "outcome.recorded"
                break

    covered = [w for w in windows if w["covered"]]
    uncovered = [w for w in windows if not w["covered"]]
    total = len(windows)
    by_ward: dict[str, str] = {}
    for ward in wards:
        ward_windows = [w for w in windows if w["ward"] == ward]
        by_ward[ward] = f"{sum(1 for w in ward_windows if w['covered'])}/{len(ward_windows)}"

    # A ward in scope that produced NO event at all in the window. Not "quiet" —
    # unobserved. Named separately because 0/n uncovered on one ward and 0/n
    # uncovered across the pilot look identical in a ratio and mean very
    # different things.
    event_wards = {_event_ward(e) for e in events}
    silent = sorted(w for w in wards if w not in event_wards)

    covered_hours = sum((w["end"] - w["start"]).total_seconds() / 3600.0 for w in covered)
    uncovered_hours = sum((w["end"] - w["start"]).total_seconds() / 3600.0 for w in uncovered)
    return {
        "windows": windows,
        "covered_windows": covered,
        "covered": len(covered),
        "total": total,
        "display": f"{len(covered)}/{total}",
        "ratio": (len(covered) / total) if total else None,
        "complete": bool(total) and len(covered) == total,
        "by_ward": by_ward,
        "wards": list(wards),
        "silent_wards": silent,
        "silent_ward_note": (
            f"{len(silent)} ward(s) in scope recorded NO shadow event in this window "
            f"({', '.join(silent)}). They have admitted patients, so they are counted as "
            "wholly uncovered time; a ward with a census and no events means the instrument "
            "is not running there, not that nothing happened."
        )
        if silent
        else None,
        "census": census or {},
        "ward_scope_source": (
            "patients census (wards with an admitted or overlapping stay) unioned with wards "
            "that produced events"
        ),
        "covered_hours": round(covered_hours, 2),
        "uncovered_hours": round(uncovered_hours, 2),
        "uncovered_sample": [
            {
                "ward": w["ward"],
                "shift": w["shift"],
                "start": _iso(w["start"]),
                "end": _iso(w["end"]),
            }
            for w in uncovered[:50]
        ],
        "shift_definition": ", ".join(f"{s.strftime('%H:%M')}+{int(h)}h" for s, h in _shifts()),
    }


def _is_covered(event: dict, covered_windows: list[dict]) -> bool:
    when = event.get("_t")
    if when is None:
        return False
    ward = _event_ward(event)
    for window in covered_windows:
        if window["ward"] == ward and window["start"] <= when < window["end"]:
            return True
    return False


# ── Occupied bed-days ────────────────────────────────────────────────────────
#: The census readers below take every patients row as a stay. A 'registered'
#: row (migration 52) is a person on the patient index who has never been
#: admitted: no ward, no admission date, no stay. Counting one would add a
#: patient to `patients_scanned` and to `undated_admissions` in the coverage
#: diagnostics, so those rows are left out at the source. Until the
#: registration feature is switched on no such row exists, and the scorecard's
#: numbers are exactly what they were.
_NEVER_ADMITTED_EXCLUDED = " WHERE COALESCE(status, 'admitted') <> 'registered'"


def _last_vital_in_stay(
    conn, patient_id: str, since: datetime, before: datetime | None
) -> datetime | None:
    """The patient's last vital signs at or after `since` and before `before`
    (no upper bound when None), or None. This is where one stay of a patient
    admitted more than once is closed when it records no end and has no
    summary, so no reading from the next stay ever stretches it over the days
    the patient was at home (that would OVER-state bed-days and deflate the
    false-alert rate). A set marked entered in error is skipped; skipping it
    can only move the end earlier, the direction this instrument may be wrong in.
    julianday() compares any stored spelling as a time; the bounds are
    checked again here, since it keeps only milliseconds."""
    sql = (
        "SELECT timestamp FROM vitals WHERE patient_id = ? AND voided_at IS NULL "
        "AND julianday(timestamp) >= julianday(?)"
    )
    params: list[Any] = [patient_id, since.isoformat()]
    if before is not None:
        sql += " AND julianday(timestamp) < julianday(?)"
        params.append(before.isoformat())
    try:
        row = conn.execute(sql + " ORDER BY timestamp DESC, id DESC LIMIT 1", params).fetchone()
    except Exception:
        logger.debug("scorecard: vitals unavailable for a repeat stay", exc_info=True)
        return None
    when = _dt(row[0]) if row else None
    if when is None or when < since or (before is not None and when >= before):
        return None
    return when


def _repeat_stays(conn) -> dict[str, list[dict]]:
    """Each stay of every patient with more than one admission Orb opened
    (stay keying, opd-17b): {patient_id: [{ward, start, end, active,
    next_start}]}, oldest first.

    The census readers below take a patients row as one stay, from the
    current admission date to the latest discharge row. For a patient
    readmitted onto the same record that merged two stays into one span
    (the gap between them counted as occupied), so such a patient is read
    stay by stay from `admissions` instead. `end` is the admission's own end
    (admissions.discharged_at), else the discharge summary keyed to it
    (discharge_summaries.admission_id, migration 65), else the first summary
    at or after its start and before the next stay's; None when none is
    found (the caller applies its usual "still here" or last-vital rule).
    `last_vital` is, for a stay that has ended with no end found, the last
    vital signs taken inside it (_last_vital_in_stay), else None: never the
    patient's latest vital overall, which is taken in the next stay and would
    close this one at the next one's start, counting the days at home as
    occupied. No stay ends after the next one starts. Encounters a history import
    filed are left out, as the census never counted them. Empty when every
    patient has one stay, so every figure is exactly what it was."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(admissions)").fetchall()}
        native = "WHERE a.import_run_id IS NULL" if "import_run_id" in cols else ""
        repeat = [
            r[0]
            for r in conn.execute(
                f"SELECT a.patient_id FROM admissions a {native} "
                "GROUP BY a.patient_id HAVING COUNT(*) > 1"
            ).fetchall()
        ]
    except Exception:
        logger.debug("scorecard: admissions unavailable for repeat stays", exc_info=True)
        return {}
    if not repeat:
        return {}
    out: dict[str, list[dict]] = {}
    for chunk in (repeat[i : i + 400] for i in range(0, len(repeat), 400)):
        marks = ",".join("?" * len(chunk))
        extra = "AND a.import_run_id IS NULL" if native else ""
        rows = conn.execute(
            f"SELECT a.id, a.patient_id, a.ward, a.status, a.admitted_at, a.created_at, "
            f"a.discharged_at FROM admissions a WHERE a.patient_id IN ({marks}) {extra}",
            chunk,
        ).fetchall()
        for row in rows:
            r = dict(row)
            start = _dt(r.get("admitted_at")) or _dt(r.get("created_at"))
            out.setdefault(r["patient_id"], []).append(
                {
                    "id": r["id"],
                    "ward": r.get("ward"),
                    "start": start,
                    "end": _dt(r.get("discharged_at")),
                    "active": (r.get("status") or "active") == "active",
                }
            )
    summaries: dict[str, list[tuple[datetime, Any]]] = defaultdict(list)
    try:
        scols = {r[1] for r in conn.execute("PRAGMA table_info(discharge_summaries)").fetchall()}
        keyed = "admission_id" if "admission_id" in scols else "NULL AS admission_id"
        for chunk in (list(out)[i : i + 400] for i in range(0, len(out), 400)):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT patient_id, discharged_at, {keyed} FROM discharge_summaries "
                f"WHERE patient_id IN ({marks})",
                chunk,
            ).fetchall():
                when = _dt(row["discharged_at"])
                if when:
                    summaries[row["patient_id"]].append((when, row["admission_id"]))
    except Exception:
        logger.debug("scorecard: discharge_summaries unavailable for repeat stays", exc_info=True)
    far = datetime.max.replace(tzinfo=UTC)
    for pid, stays in out.items():
        stays.sort(key=lambda st: st["start"] or far)
        times = sorted(summaries.get(pid, []), key=lambda t: t[0])
        for here, after in zip(stays, stays[1:] + [None], strict=False):
            here["next_start"] = after["start"] if after is not None else None
            here["last_vital"] = None
            if here["end"] is None and not here["active"] and here["start"] is not None:
                own = [t for t, aid in times if aid == here["id"] and t >= here["start"]]
                later = own or [
                    t
                    for t, _aid in times
                    if t >= here["start"] and (here["next_start"] is None or t < here["next_start"])
                ]
                here["end"] = later[0] if later else None
                if here["end"] is None:
                    here["last_vital"] = _last_vital_in_stay(
                        conn, pid, here["start"], here["next_start"]
                    )
            if (
                here["end"] is not None
                and here["next_start"] is not None
                and here["start"] is not None
                and here["next_start"] > here["start"]
                and here["end"] > here["next_start"]
            ):
                here["end"] = here["next_start"]
    return out


def _occupied_bed_days(conn, covered_windows: list[dict], ward: str | None) -> dict:
    """Bed-days inside COVERED ward-shift time only.

    Census comes from `patients` + `discharge_summaries`. Where a discharged
    patient has no discharge row, the stay is closed at their last recorded
    vital rather than at "now" — that UNDER-states bed-days, which INFLATES the
    false-alert rate. The conservative direction is the only defensible one for
    a number a hospital is being asked to accept.
    """
    empty = {
        "bed_days": 0.0,
        "patients": 0,
        "method": "no covered ward-shift time",
        "open_stays": 0,
        "assumed_ends": 0,
    }
    if not covered_windows:
        return empty
    try:
        rows = conn.execute(
            "SELECT id, ward, admission_date, status FROM patients" + _NEVER_ADMITTED_EXCLUDED
        ).fetchall()
    except Exception:
        logger.debug("scorecard: patients table unavailable for bed-days", exc_info=True)
        return {**empty, "method": "patients table unavailable"}
    if not rows:
        return {**empty, "method": "no patients on the appliance"}

    discharges: dict[str, datetime] = {}
    try:
        for row in conn.execute(
            "SELECT patient_id, MAX(discharged_at) AS ended FROM discharge_summaries GROUP BY patient_id"
        ).fetchall():
            when = _dt(row["ended"] if hasattr(row, "keys") else row[1])
            if when:
                discharges[row["patient_id"] if hasattr(row, "keys") else row[0]] = when
    except Exception:
        logger.debug("scorecard: discharge_summaries unavailable", exc_info=True)

    last_vital: dict[str, datetime] = {}
    try:
        for row in conn.execute(
            "SELECT patient_id, MAX(timestamp) AS last_at FROM vitals GROUP BY patient_id"
        ).fetchall():
            when = _dt(row["last_at"] if hasattr(row, "keys") else row[1])
            if when:
                last_vital[row["patient_id"] if hasattr(row, "keys") else row[0]] = when
    except Exception:
        logger.debug("scorecard: vitals unavailable", exc_info=True)

    windows_by_ward: dict[str, list[dict]] = defaultdict(list)
    for window in covered_windows:
        windows_by_ward[window["ward"]].append(window)

    # Stay keying (opd-17b): a patient readmitted onto the same record is
    # read stay by stay below; everyone else exactly as before.
    patient_rows = {dict(r)["id"]: dict(r) for r in rows}
    repeat = {pid: st for pid, st in _repeat_stays(conn).items() if pid in patient_rows}

    bed_hours = 0.0
    counted: set[str] = set()
    open_stays = 0
    assumed_ends = 0
    for row in rows:
        patient = dict(row)
        if patient["id"] in repeat:
            continue
        patient_ward = patient.get("ward") or _UNSPECIFIED_WARD
        if ward and patient_ward != ward:
            continue
        relevant = windows_by_ward.get(patient_ward) or []
        if not relevant:
            continue
        stay_start = _dt(patient.get("admission_date"))
        if stay_start is None:
            continue
        status = (patient.get("status") or "admitted").strip()
        stay_end = discharges.get(patient["id"])
        if stay_end is None:
            if status == "admitted":
                stay_end = max(w["end"] for w in relevant)
                open_stays += 1
            else:
                stay_end = last_vital.get(patient["id"]) or stay_start
                assumed_ends += 1
        if stay_end <= stay_start:
            continue
        overlap = 0.0
        for window in relevant:
            overlap += _overlap_hours(stay_start, stay_end, window["start"], window["end"])
        if overlap > 0:
            bed_hours += overlap
            counted.add(patient["id"])

    stays_counted = len(counted)
    repeat_counted: set[str] = set()
    for pid, stays in repeat.items():
        for stay in stays:
            stay_ward = stay["ward"] or patient_rows[pid].get("ward") or _UNSPECIFIED_WARD
            if ward and stay_ward != ward:
                continue
            relevant = windows_by_ward.get(stay_ward) or []
            stay_start = stay["start"]
            if not relevant or stay_start is None:
                continue
            stay_end = stay["end"]
            if stay_end is None:
                if stay["active"]:
                    stay_end = max(w["end"] for w in relevant)
                    open_stays += 1
                else:
                    # Its own last vital signs, never one from the next stay.
                    stay_end = stay.get("last_vital") or stay_start
                    assumed_ends += 1
                following = stay["next_start"]
                if following is not None and stay_start < following < stay_end:
                    stay_end = following
            if stay_end <= stay_start:
                continue
            overlap = 0.0
            for window in relevant:
                overlap += _overlap_hours(stay_start, stay_end, window["start"], window["end"])
            if overlap > 0:
                bed_hours += overlap
                counted.add(pid)
                repeat_counted.add(pid)
                stays_counted += 1

    result: dict[str, Any] = {
        "bed_days": round(bed_hours / 24.0, 4),
        "patients": len(counted),
        "open_stays": open_stays,
        "assumed_ends": assumed_ends,
        "method": (
            "patients.admission_date to discharge_summaries.discharged_at, "
            "clipped to covered ward-shift windows; discharged patients with no "
            "discharge row are closed at their last recorded vital (under-states "
            "bed-days, inflates the rate)"
        ),
    }
    if repeat_counted:
        # Only when a repeat stay was counted here: otherwise the answer is
        # exactly the one-stay-per-patient answer it always was.
        result["stays"] = stays_counted
        result["repeat_stay_patients"] = len(repeat_counted)
        result["method"] += (
            "; a patient admitted more than once is counted stay by stay, "
            "each admission to the discharge summary of that stay, else to "
            "the last vital recorded in that stay, and never past the start "
            "of the next"
        )
    return result


# ── Metric scaffolding ───────────────────────────────────────────────────────
def _new_metric(metric_id: str) -> dict:
    spec = METRIC_SPEC[metric_id]
    return {
        "id": metric_id,
        "label": spec["label"],
        "definition": spec["definition"],
        "state": "not_computable",
        "value": None,
        "unit": spec["unit"],
        "display": None,
        "numerator": None,
        "denominator": None,
        "denominator_label": spec["denominator_label"],
        "alternative_denominators": [],
        "n_observed": 0,
        "n_required": _min_n(metric_id),
        "spread": None,
        "caveat": spec["caveat"],
        "blockers": [],
        "is_proxy": spec["is_proxy"],
        "assumptions": [],
        "adjudication": None,
        "disagreements": None,
        # Server-side extras. The TS normalizer ignores unknown keys; the export
        # and the drill-down use them.
        "provisional": False,
        "provisional_reason": None,
        "detail": {},
    }


def _finalise(metric: dict) -> dict:
    """The last gate before a number leaves this module.

    Mirrors (and therefore cannot be weaker than) the client-side downgrades in
    src/api/scorecard.ts: `reported` requires something to print, a positive
    denominator, n at or above the floor, and no blockers. Enforced in BOTH
    places on purpose — this is the rule that stops "0 % false alerts" on
    morning one, and it should take two independent bugs to break it.
    """
    if metric["blockers"]:
        metric["state"] = "not_computable"
    elif metric["state"] == "reported":
        if metric["display"] is None and metric["value"] is None:
            metric["state"] = "not_computable"
        elif metric["denominator"] is None or metric["denominator"] <= 0:
            metric["state"] = "insufficient_data"
        elif metric["n_required"] > 0 and metric["n_observed"] < metric["n_required"]:
            metric["state"] = "insufficient_data"
    if metric["state"] != "reported":
        # Keep the numbers for the export/audit trail, but never let a
        # non-reported metric hand the view something printable.
        metric["detail"]["withheld_value"] = metric["value"]
        metric["detail"]["withheld_display"] = metric["display"]
        metric["value"] = None
        metric["display"] = None
    return metric


# ── Metric 1: detection lead time ────────────────────────────────────────────
def _detection_lead_time(events: list[dict], start: datetime, end: datetime) -> dict:
    metric = _new_metric("detection_lead_time")
    lookback = timedelta(hours=_lookback_hours())
    late = timedelta(hours=_late_window_hours())

    outcomes = [
        e
        for e in events
        if e["kind"] == "outcome.recorded"
        and e["_t"] is not None
        and start <= e["_t"] < end
        and e["payload"].get("event_type") in OUTCOME_EVENT_TYPES
    ]

    alerts_by_patient: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        if (
            event["kind"] == "alert.fired"
            and event["payload"].get("level") in ALERT_LEVELS
            and event.get("patient_id")
            and event["_t"] is not None
        ):
            alerts_by_patient[event["patient_id"]].append(event)
    for series in alerts_by_patient.values():
        series.sort(key=lambda e: (e["_t"], e["id"]))

    rows: list[dict] = []
    for outcome in outcomes:
        when = outcome["_t"]
        source = outcome["payload"].get("source") or "unspecified"
        candidates = [
            a
            for a in alerts_by_patient.get(outcome.get("patient_id") or "", [])
            if when - lookback <= a["_t"] <= when + late
        ]
        row = {
            "episode_id": outcome["episode_id"],
            "patient_id": outcome.get("patient_id"),
            "ward": outcome.get("ward"),
            "event_type": outcome["payload"].get("event_type"),
            "source": source,
            "outcome_at": outcome["occurred_at"],
            "alert_at": None,
            "alert_level": None,
            "lead_minutes": None,
            "miss": True,
        }
        if candidates:
            first = candidates[0]
            row.update(
                {
                    "alert_at": first["occurred_at"],
                    "alert_level": first["payload"].get("level"),
                    # POSITIVE = Orb was ahead. NEGATIVE = the clinician was ahead of
                    # the machine. Both are reported; neither is clipped.
                    "lead_minutes": round((when - first["_t"]).total_seconds() / 60.0, 2),
                    "miss": False,
                }
            )
        rows.append(row)

    leads = [r for r in rows if not r["miss"]]
    misses = [r for r in rows if r["miss"]]
    values = [float(r["lead_minutes"]) for r in leads]
    negatives = [v for v in values if v < 0]
    witnessed = [float(r["lead_minutes"]) for r in leads if r["source"] == "witnessed"]

    by_source: dict[str, dict] = {}
    for source in sorted({r["source"] for r in rows}):
        subset = [float(r["lead_minutes"]) for r in rows if r["source"] == source and not r["miss"]]
        by_source[source] = {
            "outcomes": sum(1 for r in rows if r["source"] == source),
            "leads": len(subset),
            "misses": sum(1 for r in rows if r["source"] == source and r["miss"]),
            "median_minutes": _percentile(subset, 0.5),
            "median_display": _fmt_minutes(_percentile(subset, 0.5)),
        }

    metric["n_observed"] = len(leads)
    metric["numerator"] = len(leads)
    metric["denominator"] = len(rows)
    metric["spread"] = _spread(values)
    metric["detail"] = {
        "outcomes": len(rows),
        "leads": len(leads),
        "misses": len(misses),
        "negative_leads": len(negatives),
        "negative_lead_values_minutes": sorted(negatives),
        "sensitivity": (round(1.0 - len(misses) / len(rows), 4) if rows else None),
        "sensitivity_display": _fmt_pct(len(leads), len(rows)),
        "by_source": by_source,
        "witnessed_only": {
            "n": len(witnessed),
            "median_minutes": _percentile(witnessed, 0.5),
            "median_display": _fmt_minutes(_percentile(witnessed, 0.5)),
            "spread": _spread(witnessed),
        },
        "lookback_hours": _lookback_hours(),
        "late_window_hours": _late_window_hours(),
        "rows": rows,
    }
    metric["alternative_denominators"] = [
        {
            "label": "median lead, all sources",
            "display": _fmt_minutes(_percentile(values, 0.5)),
            "is_primary": True,
        },
        {
            "label": "median lead, witnessed only (conservative)",
            "display": _fmt_minutes(_percentile(witnessed, 0.5)),
            "is_primary": False,
        },
        {
            "label": "sensitivity (outcomes Orb flagged at all)",
            "display": _fmt_pct(len(leads), len(rows)),
            "is_primary": False,
        },
        {
            "label": "misses (confirmed deterioration, never flagged)",
            "display": f"{len(misses)}",
            "is_primary": False,
        },
    ]
    metric["caveat"] += (
        f" Negative leads are reported, not clipped: {len(negatives)} of {len(leads)} "
        f"lead(s) are negative (the clinician was ahead of Orb). {len(misses)} confirmed "
        f"outcome(s) had no qualifying alert and are counted as misses, not dropped. "
        "chart_review outcomes are recorded after the fact and bias lead time LATE, so "
        "the witnessed-only median is the conservative figure."
    )

    if not rows:
        metric["blockers"] = [
            "No clinical outcome has been recorded in this window. Detection lead time "
            "is not weak without one, it is UNDEFINED. Recording outcomes is a "
            "prerequisite, not a refinement."
        ]
    elif not leads:
        metric["state"] = "insufficient_data"
    else:
        metric["value"] = _percentile(values, 0.5)
        metric["display"] = _fmt_minutes(metric["value"])
        metric["state"] = "reported"
    return _finalise(metric)


# ── Metric 2: false-alert rate ───────────────────────────────────────────────
def _classify_verdict(verdict: str | None) -> str:
    """`true` / `false` / `excluded` / `unrecognised`, via the ONE normaliser.

    Delegated, not reimplemented. The previous local copy of this decision is
    what made every route-recorded verdict unrecognised.
    """
    return shadow_verdicts.classify(verdict)


def _false_alert_rate(
    events: list[dict],
    adjudications: list[dict],
    start: datetime,
    end: datetime,
    coverage: dict,
    bed_days: dict,
) -> dict:
    metric = _new_metric("false_alert_rate")
    covered_windows = coverage["covered_windows"]

    alerts = [
        e
        for e in events
        if e["kind"] == "alert.fired" and e["_t"] is not None and start <= e["_t"] < end
    ]
    covered_alerts = [a for a in alerts if _is_covered(a, covered_windows)]
    excluded_alerts = len(alerts) - len(covered_alerts)

    episodes: dict[str, dict] = {}
    for alert in covered_alerts:
        bucket = episodes.setdefault(alert["episode_id"], {"alerts": 0, "ward": _event_ward(alert)})
        bucket["alerts"] += 1

    # Latest current adjudication per episode. The VIEW has already dropped
    # superseded rows; if a second, independent adjudication exists the most
    # recent one stands.
    latest: dict[str, dict] = {}
    for row in adjudications:
        episode = row["episode_id"]
        if episode not in episodes:
            continue
        previous = latest.get(episode)
        if previous is None or (row["_t"] or start) >= (previous["_t"] or start):
            latest[episode] = row

    counts = {"true": 0, "false": 0, "excluded": 0, "unrecognised": 0}
    false_episodes: list[str] = []
    for episode, row in latest.items():
        verdict = _classify_verdict(row["payload"].get("verdict"))
        counts[verdict] += 1
        if verdict == "false":
            false_episodes.append(episode)

    adjudicated = counts["true"] + counts["false"]
    adjudicators = {r.get("actor_user_id") for r in latest.values() if r.get("actor_user_id")}
    blinded = bool(latest) and all(r["payload"].get("blinded") is True for r in latest.values())
    days = float(bed_days.get("bed_days") or 0.0)

    # ── EPISODES vs ALERTS: the unit the headline is actually in ─────────────
    # Adjudication is per EPISODE — a clinician judges one clinical concern.
    # Interruption is per ALERT — a nurse is paged once per `alert.fired`, and a
    # single episode re-fires many times (49x per episode in live pilot-shaped
    # data). Reporting the episode count under the label "false alerts per
    # occupied bed-day" understated the interruption burden by that ratio, in
    # Orb's favour.
    #
    # The PRIMARY figure is therefore per ALERT: it is what the unit says, what
    # the definition claims to measure ("how often a nurse is interrupted"), and
    # the larger of the two numbers. The per-episode figure is real and is
    # reported as an explicitly-labelled alternative denominator, never as the
    # headline.
    false_alert_count = sum(episodes[e]["alerts"] for e in false_episodes)
    false_episode_count = len(false_episodes)
    alerts_per_false_episode = (
        round(false_alert_count / false_episode_count, 2) if false_episode_count else None
    )

    metric["n_observed"] = adjudicated  # the SAMPLE is adjudicated episodes …
    metric["numerator"] = false_alert_count  # … the NUMERATOR is alerts. Not the same unit.
    metric["denominator"] = days
    metric["adjudication"] = {
        "adjudicated": adjudicated,
        "total": len(episodes),
        "adjudicators": len(adjudicators),
        "blinded": blinded,
        "sampling": (
            "Latest current adjudication per episode; artefact and indeterminate "
            "verdicts are excluded from both terms and reported separately."
        ),
    }
    metric["alternative_denominators"] = [
        {
            "label": "false ALERTS per occupied bed-day (interruptions, primary)",
            "display": (f"{false_alert_count / days:.2f}" if days > 0 else None),
            "is_primary": True,
        },
        {
            "label": "false alert EPISODES per occupied bed-day (clinical concerns)",
            "display": (f"{false_episode_count / days:.2f}" if days > 0 else None),
            "is_primary": False,
        },
        {
            "label": "false alerts per 100 occupied bed-days",
            "display": (f"{100.0 * false_alert_count / days:.1f}" if days > 0 else None),
            "is_primary": False,
        },
        {
            "label": "alerts per false episode (why the two figures differ)",
            "display": (
                f"{alerts_per_false_episode:.2f}" if alerts_per_false_episode is not None else None
            ),
            "is_primary": False,
        },
        {
            "label": "episodes adjudicated false, of episodes adjudicated",
            "display": _fmt_pct(false_episode_count, adjudicated),
            "is_primary": False,
        },
        {
            "label": "share of alerts fired that were false (the flattering framing)",
            "display": _fmt_pct(false_alert_count, len(covered_alerts)),
            "is_primary": False,
        },
    ]
    metric["detail"] = {
        "episodes": len(episodes),
        "alerts": len(covered_alerts),
        "alerts_excluded_uncovered": excluded_alerts,
        "adjudicated": adjudicated,
        "true": counts["true"],
        "false": counts["false"],
        "excluded_artefact_or_indeterminate": counts["excluded"],
        "unrecognised_verdicts": counts["unrecognised"],
        "unadjudicated": len(episodes) - adjudicated - counts["excluded"] - counts["unrecognised"],
        # The headline numerator, and the per-episode figure it is NOT.
        "false_alert_count": false_alert_count,
        "false_episode_count": false_episode_count,
        "alerts_per_false_episode": alerts_per_false_episode,
        "false_alerts_per_bed_day": (round(false_alert_count / days, 4) if days > 0 else None),
        "false_episodes_per_bed_day": (round(false_episode_count / days, 4) if days > 0 else None),
        "bed_days": days,
        "bed_day_method": bed_days.get("method"),
        "coverage": coverage["display"],
    }
    metric["assumptions"] = [
        {
            "label": "Occupied bed-days",
            "value": f"{days:.2f}",
            "source": bed_days.get("method", "measured"),
        },
        {
            "label": "Ward-shift coverage",
            "value": coverage["display"],
            "source": "ward-shifts with an outcome recorded, or confirmed as having none",
        },
        {
            "label": "One alert = one interruption",
            "value": (
                f"{false_alert_count} alert(s) across {false_episode_count} false episode(s)"
            ),
            "source": (
                "measured, every alert Orb raised, repeats included. If the ward "
                "silences repeats within an episode, this OVER-states interruptions "
                "and the per-episode alternative is the floor"
            ),
        },
        {
            "label": "Adjudication unit",
            "value": "per episode, not per alert",
            "source": (
                "measured, a clinician judges one clinical concern; every alert in "
                "an episode judged false is counted false"
            ),
        },
    ]

    if coverage["total"] and not coverage["complete"]:
        metric["provisional"] = True
        metric["provisional_reason"] = (
            f"Ward-shift coverage is incomplete ({coverage['display']}). "
            f"{coverage['uncovered_hours']:.0f} uncovered hours and {excluded_alerts} alert(s) "
            "in them are excluded from every denominator, not assumed quiet."
        )
    elif not coverage["total"]:
        metric["provisional"] = True
        metric["provisional_reason"] = "No ward-shift period could be established for this window."
    if len(episodes) and adjudicated < len(episodes):
        metric["provisional"] = True
        metric["provisional_reason"] = (
            (metric["provisional_reason"] or "") + " " if metric["provisional_reason"] else ""
        ) + (
            f"{len(episodes) - adjudicated} of {len(episodes)} alert episode(s) are not yet "
            "adjudicated and are excluded, never counted as correct."
        )

    metric["caveat"] += (
        f" Numerator: {false_alert_count} false ALERT(S) from {false_episode_count} "
        f"episode(s) adjudicated false"
        + (
            f" ({alerts_per_false_episode:.2f} alerts per episode)"
            if alerts_per_false_episode is not None
            else ""
        )
        + f". Denominator: {days:.2f} occupied bed-days over {coverage['display']} covered "
        f"ward-shifts. {counts['excluded']} adjudication(s) were artefact/indeterminate and "
        "are excluded from both terms."
        + (
            f" {counts['unrecognised']} adjudication(s) carried a verdict this build does not "
            "recognise and are excluded from both terms rather than guessed at. See "
            "services/shadow_verdicts.py."
            if counts["unrecognised"]
            else ""
        )
    )

    if counts["unrecognised"]:
        # Not a blocker (the recognised verdicts still count), but the reader is
        # owed the fact that human judgement was recorded and not used.
        metric["provisional"] = True
        metric["provisional_reason"] = (
            (metric["provisional_reason"] or "") + " " if metric["provisional_reason"] else ""
        ) + (
            f"{counts['unrecognised']} recorded adjudication(s) use a verdict outside the "
            "controlled vocabulary and are counted in NEITHER term."
        )

    if not episodes:
        metric["state"] = "insufficient_data"
    elif adjudicated == 0:
        metric["state"] = "unadjudicated"
    else:
        # PER ALERT — the unit the label, the definition and `unit` all state.
        metric["value"] = (false_alert_count / days) if days > 0 else None
        metric["display"] = (
            f"{metric['value']:.2f} false alerts per occupied bed-day"
            if metric["value"] is not None
            else None
        )
        metric["state"] = "reported"
    return _finalise(metric)


# ── Metric 3: agreement (segmented by phase, NEVER pooled) ───────────────────
def _agreement(events: list[dict], start: datetime, end: datetime, phases: PhaseSchedule) -> dict:
    metric = _new_metric("agreement")

    by_episode: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        if event["kind"] in CLINICIAN_ACTION_KINDS or event["kind"] == "action.proposed":
            by_episode[event["episode_id"]].append(event)
    for series in by_episode.values():
        series.sort(key=lambda e: ((e["_t"] or start), e["id"]))

    pairs: list[dict] = []
    unresolved_rows: list[dict] = []
    actions_without_proposal = 0
    for episode, series in by_episode.items():
        proposals = [e for e in series if e["kind"] == "action.proposed"]
        actions = [e for e in series if e["kind"] in CLINICIAN_ACTION_KINDS]

        # ── ONE ACTION RESOLVES AT MOST ONE PROPOSAL ─────────────────────────
        # The pairing is a MATCHING, not a lookup. The previous reading — "the
        # first action at or after this proposal" — consumed nothing, so a single
        # approval click resolved every open proposal in the episode and inflated
        # BOTH the numerator and n by the same click. Three proposals and one
        # approval reported "100 % (3/3)"; the honest answer is one pair.
        #
        # PASS 1 — the EXPLICIT LINK, where the producers give us one. The Sage
        # card lane writes the same (subject_type, subject_id) on both sides:
        # `action.proposed` from sage_ambient_service and
        # `action.approved`/`action.declined` from relay_routes both carry
        # ('sage_actions', <action_id>). That is the actual identity of the
        # decision, so it is used in preference to any inference from the clock,
        # and it is exact where an exact answer exists.
        #
        # PASS 2 — NEAREST PRECEDING, for the lanes with no link. `clinical.action`
        # comes from CPOE carrying ('orders', <order_id>) and can never be tied to
        # a specific card, so its proposal has to be inferred. It resolves the
        # nearest preceding still-open proposal (a stack — last in, first out):
        #   * that is what the clinician actually did. They act on what is in
        #     front of them; within one episode Sage's later proposal has
        #     superseded the earlier one on screen. Pairing the action with the
        #     OLDEST open proposal instead would grade Sage against advice Sage
        #     itself had already replaced.
        #   * it is CONTENT-BLIND. The matching reads ids, subjects and times —
        #     never `action_class` — so it cannot be tuned, in either direction,
        #     to move the match rate. That property, not the LIFO/FIFO choice, is
        #     what stops this from becoming a favourable-rounding knob.
        #
        # Both passes run over every proposal in the SCAN (which reaches back
        # before `start`), so a proposal just outside the window still consumes
        # the action it earned; only in-window proposals are then reported. The
        # boundary therefore cannot hand an in-window proposal a free pairing.
        resolution_of: dict[Any, dict] = {}
        link_of: dict[Any, str] = {}

        by_subject: dict[tuple, list[dict]] = defaultdict(list)
        for proposal in proposals:
            subject = (proposal.get("subject_type"), proposal.get("subject_id"))
            if subject[0] and subject[1]:
                by_subject[subject].append(proposal)
        unlinked_actions: list[dict] = []
        for action in actions:
            subject = (action.get("subject_type"), action.get("subject_id"))
            queue = by_subject.get(subject) if subject[0] and subject[1] else None
            if queue:
                proposal = queue.pop(0)
                resolution_of[proposal["id"]] = action
                link_of[proposal["id"]] = "subject"
            else:
                unlinked_actions.append(action)

        open_proposals: list[dict] = []
        unclaimed = [p for p in proposals if p["id"] not in resolution_of]
        cursor = 0
        for action in unlinked_actions:
            action_t = action["_t"] or start
            while cursor < len(unclaimed) and (unclaimed[cursor]["_t"] or start) <= action_t:
                open_proposals.append(unclaimed[cursor])
                cursor += 1
            if not open_proposals:
                # A clinician action with no proposal preceding it. Not a
                # disagreement — Sage said nothing to disagree with. Counted and
                # named, never folded into either term.
                actions_without_proposal += 1
                continue
            proposal = open_proposals.pop()
            resolution_of[proposal["id"]] = action
            link_of[proposal["id"]] = "nearest_preceding"

        for proposal in proposals:
            if proposal["_t"] is None or not (start <= proposal["_t"] < end):
                continue
            phase = phases.phase_at(proposal["_t"])
            resolution = resolution_of.get(proposal["id"])
            if resolution is None:
                # UNRESOLVED: Sage proposed, no clinician action ever landed on
                # it. Excluded from both terms, because there is no decision to
                # compare against: scoring it a match would invent agreement that
                # nobody expressed, and scoring it a disagreement would invent a
                # rejection nobody made. But exclusion is NOT free — a proposal a
                # clinician silently ignored is at least as likely to be one they
                # disagreed with, so the exclusion runs in Orb's favour and is
                # reported per phase rather than dropped.
                unresolved_rows.append(
                    {
                        "episode_id": episode,
                        "phase": phase,
                        "proposed_class": proposal["payload"].get("action_class"),
                        "proposed_at": proposal["occurred_at"],
                    }
                )
                continue
            proposed_class = proposal["payload"].get("action_class")
            taken_class = resolution["payload"].get("action_class")
            matched = bool(
                proposed_class
                and taken_class
                and proposed_class == taken_class
                and resolution["kind"] != "action.declined"
            )
            pairs.append(
                {
                    "episode_id": episode,
                    "phase": phase,
                    "proposed_class": proposed_class,
                    "taken_class": taken_class,
                    "resolution_kind": resolution["kind"],
                    "matched": matched,
                    "proposed_at": proposal["occurred_at"],
                    "resolved_at": resolution["occurred_at"],
                    # How this pair was established. A reader is owed the difference
                    # between a pair the producers identified and a pair this module
                    # inferred from the clock.
                    "paired_by": link_of.get(proposal["id"], "nearest_preceding"),
                }
            )

    # SEGMENT, NEVER POOL. Phase A is Orb; Phase C is Orb-plus-staff. One
    # percentage covering both is a category error that inflates the number, so
    # the pooled figure is not merely unreported — it is not computed.
    segments: list[dict] = []
    floor = metric["n_required"]
    unresolved_by_phase: dict[str, int] = defaultdict(int)
    for row in unresolved_rows:
        unresolved_by_phase[row["phase"]] += 1
    # A phase in which EVERY proposal went unanswered still gets a segment: 0
    # pairs and n unresolved is a finding, and dropping the phase entirely would
    # hide it.
    for phase in sorted({p["phase"] for p in pairs} | set(unresolved_by_phase)):
        subset = [p for p in pairs if p["phase"] == phase]
        matched_count = sum(1 for p in subset if p["matched"])
        unresolved = unresolved_by_phase.get(phase, 0)
        proposals_seen = len(subset) + unresolved
        state = "reported" if len(subset) >= floor else "insufficient_data"
        segments.append(
            {
                "phase": phase,
                "phase_label": PhaseSchedule.label(phase),
                "pairs": len(subset),
                "matched": matched_count,
                "disagreements": len(subset) - matched_count,
                "unresolved": unresolved,
                "proposals": proposals_seen,
                "resolution_rate": (len(subset) / proposals_seen) if proposals_seen else None,
                "rate": (matched_count / len(subset)) if subset else None,
                "display": (_fmt_pct(matched_count, len(subset)) if state == "reported" else None),
                "state": state,
                "n_required": floor,
            }
        )

    # The headline is ONE segment — Phase A when it exists, because the silent
    # phase is the scientifically clean one and it is what the pilot claims.
    headline = next((s for s in segments if s["phase"] == "A"), None)
    if headline is None:
        headline = max(segments, key=lambda s: s["pairs"], default=None)

    metric["detail"] = {
        "segments": segments,
        "pooled": None,
        "pooling_guard": (
            "Phase A (silent) and Phase C (clinicians can see Orb) measure "
            "different things. A pooled figure is not computed anywhere in "
            "this module."
        ),
        "headline_phase": headline["phase"] if headline else None,
        "pairs_total_across_phases": len(pairs),
        "unresolved_proposals": len(unresolved_rows),
        "unresolved_by_phase": dict(unresolved_by_phase),
        "unresolved_rows": unresolved_rows[:50],
        "actions_without_a_proposal": actions_without_proposal,
        "pairs_by_subject_link": sum(1 for p in pairs if p["paired_by"] == "subject"),
        "pairs_by_nearest_preceding": sum(
            1 for p in pairs if p["paired_by"] == "nearest_preceding"
        ),
        "pairing_rule": (
            "One-to-one. A clinician action resolves the proposal carrying the same "
            "(subject_type, subject_id) where the producers wrote one, the Sage card lane "
            "does, and otherwise the nearest PRECEDING still-open proposal in the same "
            "episode. Either way the action is consumed, so it can never resolve a second "
            "proposal. Proposals never acted on are counted as unresolved and excluded from "
            "both terms; clinician actions with no proposal to resolve are counted "
            "separately and excluded from both terms. The matching reads ids, subjects and "
            "timestamps, never the proposed or taken action class, so it cannot move the "
            "match rate."
        ),
        "rows": pairs,
    }
    metric["alternative_denominators"] = [
        {
            "label": f"Phase {s['phase']}",
            "display": _fmt_pct(s["matched"], s["pairs"]),
            "is_primary": bool(headline and s["phase"] == headline["phase"]),
        }
        for s in segments
    ]
    metric["caveat"] += (
        f" Reported for {('phase ' + headline['phase']) if headline else 'no phase'} only; "
        f"{len(segments)} phase segment(s) present and never pooled."
        f" One clinician action resolves at most one proposal;"
        f" {len(unresolved_rows)} in-window proposal(s) drew no clinician action at all and are"
        f" counted in NEITHER term, and {actions_without_proposal} clinician action(s) had no"
        f" preceding proposal to be compared against."
    )
    metric["assumptions"] = [
        {
            "label": "Pairing",
            "value": "one action resolves at most one proposal",
            "source": (
                "measured, the proposal carrying the same (subject_type, subject_id) "
                "where the producers wrote one, else the nearest preceding still-open "
                "proposal in the same episode; consumed either way. The rule never "
                "reads the action class"
            ),
        },
        {
            "label": "Unanswered proposals",
            "value": f"{len(unresolved_rows)} excluded from both terms",
            "source": (
                "assumption: a proposal nobody acted on is neither agreement nor "
                "disagreement. Excluding it is the direction that FLATTERS Orb if "
                "silence means dissent, so the count is reported per phase"
            ),
        },
    ]

    if headline is not None and headline["unresolved"] > headline["pairs"]:
        metric["provisional"] = True
        metric["provisional_reason"] = (
            f"Only {headline['pairs']} of {headline['proposals']} proposal(s) in Phase "
            f"{headline['phase']} were ever acted on. The rate describes the acted-on "
            f"minority; the other {headline['unresolved']} are excluded, not counted as "
            "agreement."
        )

    if headline is None or headline["pairs"] == 0:
        metric["state"] = "insufficient_data"
        metric["n_observed"] = 0
        metric["denominator"] = 0
    else:
        metric["n_observed"] = headline["pairs"]
        metric["numerator"] = headline["matched"]
        metric["denominator"] = headline["pairs"]
        metric["disagreements"] = headline["disagreements"]
        metric["denominator_label"] = f"proposal/action pairs in Phase {headline['phase']}"
        metric["value"] = headline["rate"]
        metric["display"] = (
            f"{_fmt_pct(headline['matched'], headline['pairs'])}: Phase {headline['phase']}"
        )
        metric["state"] = "reported"
    return _finalise(metric)


# ── Metric 4: time saved (a PROXY, labelled as one everywhere) ───────────────
def _time_saved(events: list[dict], start: datetime, end: datetime) -> dict:
    metric = _new_metric("time_saved")

    drafts = [
        e
        for e in events
        if e["kind"] == "sage.drafted"
        and e["_t"] is not None
        and start <= e["_t"] < end
        and e["payload"].get("cancelled") is not True
        # Alert drafts are machine-initiated, not a lookup a clinician asked
        # for. Keep their real timings in the log, outside this proxy's count
        # and latency distribution. Legacy interactive rows have no class.
        and not str(e["payload"].get("query_class") or "").startswith("VIGIL_ALERT_")
    ]
    ttfts = [
        float(d["payload"]["ttft_ms"])
        for d in drafts
        if isinstance(d["payload"].get("ttft_ms"), (int, float))
    ]
    totals = [
        float(d["payload"]["total_ms"])
        for d in drafts
        if isinstance(d["payload"].get("total_ms"), (int, float))
    ]

    ttft_p50 = _percentile(ttfts, 0.5)
    ttft_p95 = _percentile(ttfts, 0.95)
    total_p50 = _percentile(totals, 0.5)
    total_p95 = _percentile(totals, 0.95)

    baseline = _manual_lookup_seconds()
    measured_seconds = (total_p50 / 1000.0) if total_p50 is not None else None
    timed = len(totals)
    saved_hours = None
    if measured_seconds is not None and timed:
        saved_hours = max(0.0, (baseline - measured_seconds)) * timed / 3600.0

    metric["n_observed"] = timed
    metric["numerator"] = timed
    metric["denominator"] = timed
    metric["spread"] = (
        {"p25": _percentile(ttfts, 0.25), "p50": ttft_p50, "p75": _percentile(ttfts, 0.75)}
        if ttfts
        else None
    )
    metric["assumptions"] = [
        {
            "label": "Manual lookup baseline",
            "value": f"{baseline:.0f} s per answer",
            "source": "STATED ASSUMPTION, not measured in this pilot",
        },
        {
            "label": "Sage answer latency (p50)",
            "value": (
                f"{measured_seconds:.1f} s" if measured_seconds is not None else "not measured"
            ),
            "source": "measured, sage.drafted.total_ms",
        },
        {
            "label": "Sage time-to-first-token (p50 / p95)",
            "value": (
                f"{ttft_p50 / 1000.0:.1f} s / {ttft_p95 / 1000.0:.1f} s"
                if ttft_p50 is not None and ttft_p95 is not None
                else "not measured"
            ),
            "source": "measured, sage.drafted.ttft_ms",
        },
        {"label": "Answers timed", "value": str(timed), "source": "measured, event count"},
        {
            "label": "Clinical time saved",
            "value": "NOT MEASURED",
            "source": "would require a controlled comparison this pilot is not running",
        },
    ]
    metric["detail"] = {
        "answers": len(drafts),
        "timed_answers": timed,
        "ttft_ms_p50": ttft_p50,
        "ttft_ms_p95": ttft_p95,
        "total_ms_p50": total_p50,
        "total_ms_p95": total_p95,
        "manual_baseline_seconds": baseline,
        "saved_hours": saved_hours,
        "formula": "timed_answers x max(0, manual_baseline_seconds - measured_total_p50_seconds)",
    }
    metric["alternative_denominators"] = [
        {
            "label": "hours saved (proxy)",
            "display": (f"{saved_hours:.2f} h" if saved_hours is not None else None),
            "is_primary": True,
        },
        {
            "label": "measured Sage TTFT p50",
            "display": (f"{ttft_p50 / 1000.0:.1f} s" if ttft_p50 is not None else None),
            "is_primary": False,
        },
        {
            "label": "measured Sage TTFT p95",
            "display": (f"{ttft_p95 / 1000.0:.1f} s" if ttft_p95 is not None else None),
            "is_primary": False,
        },
    ]
    metric["caveat"] += (
        f" This is a PROXY: {timed} measured answer(s) multiplied by a stated "
        f"{baseline:.0f} s manual baseline. Only the latency is measured; the baseline is "
        "an assumption and is printed as one."
    )

    if timed == 0 or saved_hours is None:
        metric["state"] = "insufficient_data"
    else:
        metric["value"] = round(saved_hours, 3)
        metric["display"] = f"{saved_hours:.1f} h (proxy)"
        metric["state"] = "reported"
    return _finalise(metric)


# ── Instrument health ────────────────────────────────────────────────────────
def _instrument(conn, include_simulator: bool, truncated: bool) -> dict:
    """The instrument's report on ITSELF. Without this a silent write failure
    reads as "no events happened", which is the one error mode that turns an
    empty scorecard into a confident one."""
    from services import shadow_service

    status: dict[str, Any] = {
        "source": "shadow_events_current",
        "schema_v": None,
        "append_only": False,
        "events_total": 0,
        "episodes_total": 0,
        "episodes_closed": 0,
        "first_event_at": None,
        "last_event_at": None,
        "lag_ms_p50": None,
        "lag_ms_p95": None,
        "lanes": {},
        "kinds": {},
        "corrections": 0,
        "simulator_events": 0,
        "simulator_included": include_simulator,
        "scan_truncated": truncated,
    }
    present = _instrument_present(conn)
    status["present"] = present
    if not present:
        status["warnings"] = [
            "The pilot evidence log is missing from this appliance's "
            "database, so nothing has been recorded. Contact Orb support."
        ]
        status["healthy"] = False
        status["dropped_events"] = 0
        status["error_events"] = 0
        status["runtime"] = dict(shadow_service.STATS)
        return status

    triggers = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name IN "
            "('trg_shadow_no_update','trg_shadow_no_delete')"
        ).fetchall()
    }
    status["append_only"] = {"trg_shadow_no_update", "trg_shadow_no_delete"} <= triggers

    row = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT episode_id) AS episodes, "
        "MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at, "
        "MAX(schema_v) AS schema_v, "
        "SUM(CASE WHEN supersedes_event_id IS NOT NULL THEN 1 ELSE 0 END) AS corrections, "
        "SUM(CASE WHEN kind = 'episode.closed' THEN 1 ELSE 0 END) AS closed, "
        "SUM(CASE WHEN lane = 'simulator' THEN 1 ELSE 0 END) AS simulated "
        "FROM shadow_events_current"
    ).fetchone()
    if row is not None:
        row = dict(row)
        status.update(
            {
                "events_total": int(row.get("n") or 0),
                "episodes_total": int(row.get("episodes") or 0),
                "episodes_closed": int(row.get("closed") or 0),
                "first_event_at": row.get("first_at"),
                "last_event_at": row.get("last_at"),
                "schema_v": row.get("schema_v"),
                "corrections": int(row.get("corrections") or 0),
                "simulator_events": int(row.get("simulated") or 0),
            }
        )

    status["lanes"] = {
        r["lane"]: int(r["n"])
        for r in conn.execute(
            "SELECT lane, COUNT(*) AS n FROM shadow_events_current GROUP BY lane"
        ).fetchall()
    }
    status["kinds"] = {
        r["kind"]: int(r["n"])
        for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM shadow_events_current GROUP BY kind"
        ).fetchall()
    }

    # Instrument lag: recorded_at − occurred_at, over a bounded recent sample.
    lags: list[float] = []
    for r in conn.execute(
        "SELECT occurred_at, recorded_at FROM shadow_events_current ORDER BY id DESC LIMIT 5000"
    ).fetchall():
        occurred, recorded = _dt(r["occurred_at"]), _dt(r["recorded_at"])
        if occurred and recorded:
            lags.append((recorded - occurred).total_seconds() * 1000.0)
    status["lag_ms_p50"] = _percentile(lags, 0.5)
    status["lag_ms_p95"] = _percentile(lags, 0.95)
    status["lag_sample"] = len(lags)

    stats = dict(shadow_service.STATS)
    dropped = (
        stats.get("dropped_error", 0)
        + stats.get("dropped_backpressure", 0)
        + stats.get("dropped_invalid", 0)
    )
    status["runtime"] = stats
    status["dropped_events"] = dropped
    status["error_events"] = stats.get("dropped_error", 0)

    warnings: list[str] = []
    if not status["append_only"]:
        warnings.append(
            "Append-only triggers are MISSING: rows in this table could have "
            "been edited or deleted, so the export is not evidentiary."
        )
    if dropped:
        warnings.append(
            f"{dropped} shadow event(s) were dropped by the writer since start "
            f"({stats.get('dropped_error', 0)} error, "
            f"{stats.get('dropped_backpressure', 0)} backpressure, "
            f"{stats.get('dropped_invalid', 0)} invalid). Every denominator "
            "below is a LOWER bound."
        )
    if truncated:
        warnings.append(f"The scan hit its {_max_events()} row cap; this window is partial.")
    if status["simulator_events"] and not include_simulator:
        warnings.append(
            f"{status['simulator_events']} simulator-lane event(s) exist and are "
            "EXCLUDED from every metric: simulated alerts are not evidence."
        )
    status["warnings"] = warnings
    status["healthy"] = bool(status["append_only"]) and dropped == 0 and not truncated
    return status


def instrument_health(*, db=None) -> dict:
    """The instrument's self-report. SYNC — never call on the event loop."""
    if db is not None:
        return _instrument(db, _include_simulator_default(), False)
    with db_session() as conn:
        return _instrument(conn, _include_simulator_default(), False)


# ── Readiness ────────────────────────────────────────────────────────────────
def _readiness(instrument: dict, coverage: dict, metrics: dict, events: list[dict]) -> list[dict]:
    outcomes = sum(1 for e in events if e["kind"] == "outcome.recorded")
    adjudications = sum(1 for e in events if e["kind"] == "adjudication.recorded")
    negatives = sum(
        1 for e in events if e["kind"] in ("alert.expired", "action.abandoned", "episode.closed")
    )
    clinical = instrument["events_total"] - instrument.get("simulator_events", 0)
    ratio = coverage["ratio"]
    threshold = _coverage_threshold()
    return [
        {
            "id": "instrument",
            "label": "Shadow event log present and append-only",
            "met": bool(instrument.get("present")) and bool(instrument["append_only"]),
            "detail": (
                f"{instrument['events_total']} event(s); protection against editing "
                f"or deleting entries {'in place' if instrument['append_only'] else 'MISSING'}."
            ),
        },
        {
            "id": "clinical_data",
            "label": "Non-simulator (real) events recorded",
            "met": clinical > 0,
            "detail": (
                f"{clinical} clinical-lane event(s); "
                f"{instrument.get('simulator_events', 0)} simulator event(s) excluded."
            ),
        },
        {
            "id": "outcome_capture",
            "label": "Clinical outcome capture running",
            "met": outcomes > 0,
            "detail": (
                f"{outcomes} clinical outcome(s) recorded. Without them, detection "
                "lead time does not exist."
            ),
        },
        {
            "id": "adjudication",
            "label": "False-alert adjudication running",
            "met": adjudications > 0,
            "detail": f"{adjudications} alert adjudication(s) recorded in this window.",
        },
        {
            "id": "coverage",
            "label": "Ward-shift coverage",
            "met": ratio is not None and ratio >= threshold,
            "detail": (
                f"{coverage['display']} ward-shifts covered "
                f"({coverage['uncovered_hours']:.0f} uncovered hours). Uncovered time is "
                "excluded from every denominator."
            ),
        },
        {
            "id": "negatives",
            "label": "Negatives materialised (the sweeper is running)",
            "met": negatives > 0,
            "detail": (
                f"{negatives} closed/expired/abandoned row(s). Without these the "
                "scorecard would score only the successes."
            ),
        },
        {
            "id": "no_dropped_events",
            "label": "No shadow writes dropped",
            "met": instrument.get("dropped_events", 0) == 0,
            "detail": f"{instrument.get('dropped_events', 0)} dropped since process start.",
        },
    ]


# ── The scorecard ────────────────────────────────────────────────────────────
def _census_wards(
    conn, start: datetime, end: datetime, ward: str | None = None
) -> tuple[set[str], dict]:
    """Wards that had a patient on them during the window, from `patients`.

    Stay bounds are computed exactly as `_occupied_bed_days` computes them —
    discharge row, else "still here" for an admitted patient, else the last
    recorded vital — so the coverage denominator and the bed-day numerator are
    always talking about the same set of wards.

    A patient whose admission_date will not parse is assumed to have spanned the
    whole window if they are still admitted. That OVER-states the denominator,
    which UNDER-states coverage, which is the only direction a coverage figure
    may be wrong in.
    """
    diagnostics: dict[str, Any] = {
        "available": True,
        "patients_scanned": 0,
        "patients_in_window": 0,
        "undated_admissions": 0,
        "wards_from_census": 0,
    }
    try:
        rows = conn.execute(
            "SELECT id, ward, admission_date, status FROM patients" + _NEVER_ADMITTED_EXCLUDED
        ).fetchall()
    except Exception:
        logger.debug("scorecard: patients table unavailable for ward scope", exc_info=True)
        return set(), {
            **diagnostics,
            "available": False,
            "note": "patients table unavailable; ward scope fell back to events",
        }
    if not rows:
        return set(), {**diagnostics, "note": "no patients on the appliance"}

    patients = [dict(r) for r in rows]
    # Stay keying (opd-17b): a patient readmitted onto the same record is
    # placed in time stay by stay (see the loop's tail); everyone else exactly
    # as before.
    by_id = {p.get("id"): p for p in patients}
    repeat = {pid: st for pid, st in _repeat_stays(conn).items() if pid in by_id}
    if repeat:
        patients = [p for p in patients if p.get("id") not in repeat]
    needs_end = any((p.get("status") or "admitted").strip() != "admitted" for p in patients)
    discharges: dict[str, datetime] = {}
    last_vital: dict[str, datetime] = {}
    if needs_end:
        try:
            for row in conn.execute(
                "SELECT patient_id, MAX(discharged_at) AS ended FROM discharge_summaries "
                "GROUP BY patient_id"
            ).fetchall():
                when = _dt(row["ended"] if hasattr(row, "keys") else row[1])
                if when:
                    discharges[row["patient_id"] if hasattr(row, "keys") else row[0]] = when
        except Exception:
            logger.debug("scorecard: discharge_summaries unavailable for ward scope", exc_info=True)
        # `vitals` is the largest table on the appliance, so this asks only about
        # the patients that actually need it — the discharged ones with no
        # discharge row to close on — instead of grouping the whole table the way
        # `_occupied_bed_days` does. Usually that set is empty and the query
        # never runs at all.
        undischarged = [
            p["id"]
            for p in patients
            if (p.get("status") or "admitted").strip() != "admitted"
            and p.get("id")
            and p.get("id") not in discharges
        ]
        for chunk in (undischarged[i : i + 400] for i in range(0, len(undischarged), 400)):
            placeholders = ",".join("?" * len(chunk))
            try:
                for row in conn.execute(
                    f"SELECT patient_id, MAX(timestamp) AS last_at FROM vitals "
                    f"WHERE patient_id IN ({placeholders}) GROUP BY patient_id",
                    chunk,
                ).fetchall():
                    when = _dt(row["last_at"] if hasattr(row, "keys") else row[1])
                    if when:
                        last_vital[row["patient_id"] if hasattr(row, "keys") else row[0]] = when
            except Exception:
                logger.debug("scorecard: vitals unavailable for ward scope", exc_info=True)
                break

    wards: set[str] = set()
    for patient in patients:
        patient_ward = patient.get("ward") or _UNSPECIFIED_WARD
        if ward and patient_ward != ward:
            continue
        diagnostics["patients_scanned"] += 1
        status = (patient.get("status") or "admitted").strip()
        stay_start = _dt(patient.get("admission_date"))
        stay_end = discharges.get(patient.get("id") or "")
        if stay_start is None:
            diagnostics["undated_admissions"] += 1
            if status != "admitted" and stay_end is None:
                continue  # gone, and undateable: cannot honestly place them in time
            stay_start = start
        if stay_end is None:
            stay_end = (
                end
                if status == "admitted"
                else (last_vital.get(patient.get("id") or "") or stay_start)
            )
        if stay_start < end and stay_end > start:
            wards.add(patient_ward)
            diagnostics["patients_in_window"] += 1
    if repeat:
        _census_repeat_stays(conn, repeat, by_id, start, end, ward, wards, diagnostics)
    diagnostics["wards_from_census"] = len(wards)
    return wards, diagnostics


def _census_repeat_stays(
    conn,
    repeat: dict,
    by_id: dict,
    start: datetime,
    end: datetime,
    ward: str | None,
    wards: set,
    diagnostics: dict,
) -> None:
    """_census_wards for patients with more than one stay (opd-17b): each
    stay is placed in time on its own, with the same rules as one stay (no
    end and still here: the window's end; no end and gone: the last vital
    taken inside that stay, as _occupied_bed_days reads it, else its start;
    undateable and gone: left out). The diagnostics count
    such a patient once in patients_scanned and patients_in_window, and add
    repeat_stay_patients and repeat_stays_in_window, so two stays are visible as
    two. Only called when such a patient exists."""
    for pid, stays in repeat.items():
        scanned = in_window = False
        for stay in stays:
            stay_ward = stay["ward"] or by_id[pid].get("ward") or _UNSPECIFIED_WARD
            if ward and stay_ward != ward:
                continue
            scanned = True
            stay_start, stay_end = stay["start"], stay["end"]
            if stay_start is None:
                diagnostics["undated_admissions"] += 1
                if not stay["active"] and stay_end is None:
                    continue
                stay_start = start
            if stay_end is None:
                if stay["active"]:
                    stay_end = end
                else:
                    stay_end = stay.get("last_vital") or stay_start
                    following = stay["next_start"]
                    if following is not None and stay_start < following < stay_end:
                        stay_end = following
            if stay_start < end and stay_end > start:
                wards.add(stay_ward)
                in_window = True
                diagnostics["repeat_stays_in_window"] = (
                    diagnostics.get("repeat_stays_in_window", 0) + 1
                )
        if scanned:
            diagnostics["patients_scanned"] += 1
            diagnostics["repeat_stay_patients"] = diagnostics.get("repeat_stay_patients", 0) + 1
            diagnostics.setdefault("repeat_stays_in_window", 0)
        if in_window:
            diagnostics["patients_in_window"] += 1


def _wards_in_scope(
    conn, events: list[dict], ward: str | None, start: datetime, end: datetime
) -> tuple[list[str], dict]:
    """The wards coverage is measured OVER — from the census, not the events.

    Deriving the ward list from the event stream makes the worst possible
    outcome invisible: a ward whose instrument is dead produces no events, so it
    silently leaves the denominator and the pilot reports the coverage of
    whichever wards happened to be working. A ward with admitted patients and no
    events must read as uncovered, and it is the single most important thing
    this metric can detect.

    Event wards are still unioned in, so a ward that produced events without
    appearing in the census (a transfer, a stale ward string) is not dropped
    either. Uncovered shifts WITHIN a known ward were always handled correctly
    by `_coverage`; this only fixes which wards exist at all.
    """
    if ward:
        return [ward], {"available": True, "note": "explicit ward filter; census not consulted"}
    seen = {
        _event_ward(e)
        for e in events
        if e["kind"]
        in ("alert.fired", "outcome.recorded", "outcome.none_confirmed", "episode.opened")
    }
    census, diagnostics = _census_wards(conn, start, end, ward)
    diagnostics["wards_from_events"] = len(seen)
    diagnostics["wards_with_census_but_no_events"] = len(census - seen)
    return sorted(seen | census), diagnostics


def _empty_metrics(reason: str) -> dict:
    out = {}
    for metric_id in METRIC_IDS:
        metric = _new_metric(metric_id)
        metric["blockers"] = [reason]
        out[metric_id] = _finalise(metric)
    return out


def scorecard(
    days: int = 14,
    ward: str | None = None,
    *,
    now: datetime | None = None,
    db=None,
    include_simulator: bool | None = None,
) -> dict:
    """The composite scorecard for the last `days` days.

    SYNC — never call on the event loop; route handlers must be `def`.
    Correct and complete on an EMPTY database: every metric returns an explicit
    state with its denominator, and nothing raises.
    """
    if db is not None:
        return _scorecard(db, days, ward, now, include_simulator)
    with db_session() as conn:
        return _scorecard(conn, days, ward, now, include_simulator)


def _scorecard(
    conn, days: int, ward: str | None, now: datetime | None, include_simulator: bool | None
) -> dict:
    days = max(1, int(days or 1))
    end = (now or datetime.now(UTC)).astimezone(UTC)
    start = end - timedelta(days=days)
    if include_simulator is None:
        include_simulator = _include_simulator_default()
    phases = PhaseSchedule.from_env()

    period = {"start": _iso(start), "end": _iso(end), "days": days}
    export = {
        "available": True,
        "formats": ["json", "csv"],
        "deidentification": DEIDENTIFICATION,
        "note": (
            "The exported artefact is de-identified as itemised above and is the only "
            "form that may leave the appliance. The identified data never does."
        ),
    }

    if not _instrument_present(conn):
        instrument = _instrument(conn, include_simulator, False)
        reason = (
            "The shadow-event instrument is not present on this appliance: no episode, "
            "alert or outcome has been recorded here."
        )
        coverage = _coverage([], start, end, [])
        return {
            "generated_at": _iso(datetime.now(UTC)),
            "period": period,
            "wards": [],
            "phase": phases.current(end),
            "phase_label": PhaseSchedule.label(phases.current(end)),
            "instrument": instrument,
            "coverage": coverage,
            "bed_days": {"bed_days": 0.0, "patients": 0, "method": "instrument absent"},
            "readiness": _readiness(instrument, coverage, {}, []),
            "metrics": _empty_metrics(reason),
            "export": {**export, "available": False},
            "notes": [reason],
        }

    # Alerts just BEFORE the window can still precede an outcome inside it, so the
    # scan reaches back by the lookback and forward by the late window.
    scan_start = start - timedelta(hours=_lookback_hours())
    scan_end = max(end, datetime.now(UTC)) + timedelta(hours=_late_window_hours())
    events, truncated = _load_events(conn, scan_start, scan_end, ward, include_simulator)
    adjudications = _load_late_adjudications(conn, start, ward, include_simulator)
    seen_ids = {e["id"] for e in events}
    events.extend(a for a in adjudications if a["id"] not in seen_ids)

    in_window = [e for e in events if e["_t"] is not None and start <= e["_t"] < end]
    wards, census = _wards_in_scope(conn, in_window, ward, start, end)
    coverage = _coverage(in_window, start, end, wards, census=census)
    bed_days = _occupied_bed_days(conn, coverage["covered_windows"], ward)
    instrument = _instrument(conn, include_simulator, truncated)

    metrics = {
        "detection_lead_time": _detection_lead_time(events, start, end),
        "false_alert_rate": _false_alert_rate(
            events, adjudications, start, end, coverage, bed_days
        ),
        "agreement": _agreement(events, start, end, phases),
        "time_saved": _time_saved(events, start, end),
    }
    current_phase = phases.current(end)

    notes = [
        f"Ward-shift coverage: {coverage['display']}. A ward-shift with neither a recorded "
        "outcome nor a charge-nurse 'no deterioration' confirmation is EXCLUDED from every "
        "denominator and reported as uncovered time. Silence is not evidence of absence.",
        "Medians and percentiles are computed in Python; SQLite has no median. Lead time is "
        "reported as median + IQR because the distribution is right-skewed.",
    ]
    if coverage.get("silent_wards"):
        notes.append(coverage["silent_ward_note"])
    if not include_simulator and instrument.get("simulator_events"):
        notes.append(
            f"{instrument['simulator_events']} simulator-lane event(s) are excluded "
            "from every metric on this page."
        )
    for warning in instrument.get("warnings", []):
        notes.append(warning)

    return {
        "generated_at": _iso(datetime.now(UTC)),
        "period": period,
        "wards": [w for w in wards if w != _UNSPECIFIED_WARD] or wards,
        "phase": current_phase,
        "phase_label": PhaseSchedule.label(current_phase),
        "instrument": instrument,
        "coverage": coverage,
        "bed_days": bed_days,
        "readiness": _readiness(instrument, coverage, metrics, in_window),
        "metrics": metrics,
        "export": export,
        "notes": notes,
    }


# ── Drill-down ───────────────────────────────────────────────────────────────
def episode_detail(episode_id: str, *, db=None, include_phi: bool = False) -> dict | None:
    """Every current row for one episode, in order, plus the derived summary.

    SYNC — never call on the event loop. `include_phi` is off by default: the
    aggregate view has no business carrying free clinical text.
    """
    if not episode_id:
        return None
    if db is not None:
        return _episode_detail(db, episode_id, include_phi)
    with db_session() as conn:
        return _episode_detail(conn, episode_id, include_phi)


def _episode_detail(conn, episode_id: str, include_phi: bool) -> dict | None:
    if not _instrument_present(conn):
        return None
    columns = _SCAN_COLUMNS + (", phi_json" if include_phi else "")
    rows = conn.execute(
        f"SELECT {columns} FROM shadow_events_current WHERE episode_id = ? ORDER BY occurred_at, id",
        (episode_id,),
    ).fetchall()
    if not rows:
        return None

    phases = PhaseSchedule.from_env()
    events: list[dict] = []
    for raw in rows:
        event = _row_to_event(raw)
        occurred, recorded = event["_t"], _dt(event.get("recorded_at"))
        events.append(
            {
                "id": event["id"],
                "kind": event["kind"],
                "occurred_at": event["occurred_at"],
                "recorded_at": event["recorded_at"],
                "lag_ms": (
                    (recorded - occurred).total_seconds() * 1000.0
                    if occurred and recorded
                    else None
                ),
                "lane": event["lane"],
                "patient_id": event.get("patient_id"),
                "ward": event.get("ward"),
                "actor_user_id": event.get("actor_user_id"),
                "actor_role": event.get("actor_role"),
                "subject_type": event.get("subject_type"),
                "subject_id": event.get("subject_id"),
                "payload": event["payload"],
                "supersedes_event_id": event.get("supersedes_event_id"),
                "correction_reason": event.get("correction_reason"),
                "phi": (
                    json.loads(dict(raw).get("phi_json") or "null")
                    if include_phi and dict(raw).get("phi_json")
                    else None
                ),
                "_t": occurred,
            }
        )

    def _first(kind: str) -> dict | None:
        return next((e for e in events if e["kind"] == kind), None)

    alerts = [e for e in events if e["kind"] == "alert.fired"]
    qualifying = [e for e in alerts if e["payload"].get("level") in ALERT_LEVELS]
    outcome = next((e for e in events if e["kind"] == "outcome.recorded"), None)
    adjudication = next((e for e in reversed(events) if e["kind"] == "adjudication.recorded"), None)
    opened, closed = _first("episode.opened"), _first("episode.closed")

    lead_minutes = None
    if outcome and qualifying and outcome["_t"] and qualifying[0]["_t"]:
        lead_minutes = round((outcome["_t"] - qualifying[0]["_t"]).total_seconds() / 60.0, 2)

    first_event_time = events[0]["_t"]
    for event in events:
        event.pop("_t", None)

    return {
        "episode_id": episode_id,
        "patient_id": next((e["patient_id"] for e in events if e["patient_id"]), None),
        "ward": next((e["ward"] for e in events if e["ward"]), None),
        "phase": phases.phase_at(first_event_time),
        "opened_at": opened["occurred_at"] if opened else events[0]["occurred_at"],
        "closed_at": closed["occurred_at"] if closed else None,
        "events_n": len(events),
        "lanes": sorted({e["lane"] for e in events}),
        "corrections": sum(1 for e in events if e["supersedes_event_id"] is not None),
        "summary": {
            "alerts_fired": len(alerts),
            "qualifying_alerts": len(qualifying),
            "max_level": (
                "critical"
                if any(e["payload"].get("level") == "critical" for e in alerts)
                else ("elevated" if qualifying else None)
            ),
            "outcome": (outcome["payload"].get("event_type") if outcome else None),
            "outcome_source": (outcome["payload"].get("source") if outcome else None),
            "outcome_at": (outcome["occurred_at"] if outcome else None),
            "lead_minutes": lead_minutes,
            "lead_display": _fmt_minutes(lead_minutes),
            # `verdict` is what was RECORDED (the audit view must not rewrite the
            # row); `verdict_canonical` is what it normalises to and therefore
            # how the metric counted it. Both, so a reader can see a historical
            # spelling and see that it still counted.
            "verdict": (adjudication["payload"].get("verdict") if adjudication else None),
            "verdict_canonical": (
                shadow_verdicts.normalise(adjudication["payload"].get("verdict"))
                if adjudication
                else None
            ),
            "verdict_class": (
                _classify_verdict(adjudication["payload"].get("verdict")) if adjudication else None
            ),
            "sage_drafts": sum(1 for e in events if e["kind"] == "sage.drafted"),
            "proposals": sum(1 for e in events if e["kind"] == "action.proposed"),
            "clinician_actions": sum(1 for e in events if e["kind"] in CLINICIAN_ACTION_KINDS),
        },
        "events": events,
        "phi_included": include_phi,
    }


def list_episodes(
    days: int = 14,
    ward: str | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
    db=None,
    only_with_outcome: bool = False,
    only_unadjudicated: bool = False,
    include_simulator: bool | None = None,
) -> dict:
    """Episode index for the drill-down. SYNC — never call on the event loop."""
    if db is not None:
        return _list_episodes(
            db,
            days,
            ward,
            limit,
            offset,
            now,
            only_with_outcome,
            only_unadjudicated,
            include_simulator,
        )
    with db_session() as conn:
        return _list_episodes(
            conn,
            days,
            ward,
            limit,
            offset,
            now,
            only_with_outcome,
            only_unadjudicated,
            include_simulator,
        )


def _list_episodes(
    conn,
    days: int,
    ward: str | None,
    limit: int,
    offset: int,
    now: datetime | None,
    only_with_outcome: bool,
    only_unadjudicated: bool,
    include_simulator: bool | None,
) -> dict:
    days = max(1, int(days or 1))
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    end = (now or datetime.now(UTC)).astimezone(UTC)
    start = end - timedelta(days=days)
    if include_simulator is None:
        include_simulator = _include_simulator_default()
    if not _instrument_present(conn):
        return {
            "episodes": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
            "period": {"start": _iso(start), "end": _iso(end), "days": days},
        }

    phases = PhaseSchedule.from_env()
    events, truncated = _load_events(conn, start, end, ward, include_simulator)

    grouped: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        grouped[event["episode_id"]].append(event)

    summaries: list[dict] = []
    for episode_id, series in grouped.items():
        alerts = [e for e in series if e["kind"] == "alert.fired"]
        outcome = next((e for e in series if e["kind"] == "outcome.recorded"), None)
        adjudication = next(
            (e for e in reversed(series) if e["kind"] == "adjudication.recorded"), None
        )
        if only_with_outcome and outcome is None:
            continue
        if only_unadjudicated and adjudication is not None:
            continue
        closed_row = next((e for e in series if e["kind"] == "episode.closed"), None)
        first_at, last_at = series[0]["_t"], series[-1]["_t"]
        summaries.append(
            {
                "episode_id": episode_id,
                "patient_id": next((e["patient_id"] for e in series if e.get("patient_id")), None),
                "ward": _event_ward(series[0]),
                "phase": phases.phase_at(series[0]["_t"]),
                "lane": series[0]["lane"],
                "opened_at": series[0]["occurred_at"],
                "last_at": series[-1]["occurred_at"],
                "closed_at": (closed_row["occurred_at"] if closed_row else None),
                "duration_min": (
                    round((last_at - first_at).total_seconds() / 60.0, 1)
                    if first_at and last_at
                    else None
                ),
                "has_outcome": outcome is not None,
                "events_n": len(series),
                "alerts": len(alerts),
                "max_level": (
                    "critical"
                    if any(e["payload"].get("level") == "critical" for e in alerts)
                    else (
                        "elevated"
                        if any(e["payload"].get("level") == "elevated" for e in alerts)
                        else None
                    )
                ),
                "closed": any(e["kind"] == "episode.closed" for e in series),
                "outcome": (outcome["payload"].get("event_type") if outcome else None),
                "outcome_source": (outcome["payload"].get("source") if outcome else None),
                "verdict": (adjudication["payload"].get("verdict") if adjudication else None),
                "verdict_canonical": (
                    shadow_verdicts.normalise(adjudication["payload"].get("verdict"))
                    if adjudication
                    else None
                ),
                "verdict_class": (
                    _classify_verdict(adjudication["payload"].get("verdict"))
                    if adjudication
                    else None
                ),
                "sage_drafts": sum(1 for e in series if e["kind"] == "sage.drafted"),
            }
        )

    summaries.sort(key=lambda s: s["opened_at"], reverse=True)
    return {
        "episodes": summaries[offset : offset + limit],
        "total": len(summaries),
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "period": {"start": _iso(start), "end": _iso(end), "days": days},
    }
