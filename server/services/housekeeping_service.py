"""Orb — HOUSEKEEPING & FACILITIES: what stands between a discharge and the next
admission.

A discharged bed is not an available bed. It is a dirty bed. Somewhere between
the patient leaving and the next patient arriving there is a gap that is
routinely measured in hours, that nothing in Orb could previously see, and that
gates capacity far more often than the number of beds does. This module makes
that gap a first-class, timed, attributable object:

    occupied → vacant_dirty → cleaning_in_progress → vacant_ready → occupied

plus `out_of_service` for a unit that is physically unusable, and a linen ledger
because linen is the consumable a ward runs out of at 2am.

════════════════════════════════════════════════════════════════════════════════
 CAPACITY. READ THIS BEFORE CHANGING ANY NUMBER THIS FILE RETURNS.
════════════════════════════════════════════════════════════════════════════════
Orb already has a bed-availability number, and a bed manager already acts on it.
`forecast_service.get_bed_capacity_forecast` computes, per ward:

    available = WARD_CAPACITY[ward] − (count of patients with status 'admitted')

WARD_CAPACITY is a per-ward INTEGER SITE SETTING (`ORB_WARD_BEDS`). It is not a
list of beds. There is no bed registry anywhere in Orb — `patients.bed` and
`admissions.bed` are free-text labels that nothing validates — so that number is
computed from a count and a constant, and it knows nothing about any individual
bed.

THIS MODULE DOES NOT TOUCH THAT NUMBER, AND THE CHOICE WAS DELIBERATE.

The tempting move is to subtract dirty beds from `available` so the capacity
screen tells the truth. It would make the capacity screen tell a NEW kind of
lie. `housekeeping_units` is a register a site fills in by hand, one bed at a
time. On the day this module ships it contains ZERO rows. A ward with twelve
beds and three registered would report "3 clean of 12" — and subtracting
`registered − ready` from a capacity computed on an entirely different basis
produces a number that is neither the old number nor a correct one, silently,
on a screen somebody uses to decide whether to accept a transfer.

So the two live side by side, and this module's job is to make the RELATIONSHIP
visible rather than to hide it:

  * `ward_readiness()` reports housekeeping's own figures under their own names
    (`ready`, `dirty`, `in_progress`, `out_of_service`), NEVER under the name
    `available`.
  * Every readiness response carries `registered_units` and `ward_capacity` and
    a `coverage_pct`, so "0 of 12 beds registered" can never be read as "all
    twelve are clean".
  * `capacity_view_available` repeats what the capacity screen would say, beside
    `not_ready_registered`, and names the gap explicitly. A bed manager sees the
    two numbers and the reason they differ, on one line, instead of discovering
    the difference by walking to the ward.

WHEN THIS SHOULD BE MERGED INSTEAD: when a site's unit register is complete for
a ward (coverage 100%) and has been for long enough to be trusted, capacity can
be derived from the register — which would also fix the deeper problem that
`available` today cannot tell a bed that is free from a bed that is broken. That
is a change to forecast_service, it changes the meaning of a number people
already rely on, and it needs the champion's sign-off. It is a followup, not
something to smuggle in from here.

════════════════════════════════════════════════════════════════════════════════
 PILOT SAFETY: THIS MODULE CANNOT BLOCK A CLINICAL ACTION.
════════════════════════════════════════════════════════════════════════════════
Nothing here is read by medication ordering, administration, vitals, alerts,
shadow capture, the audit chain, auth or the offline queue.

In particular: `transition_unit` REFUSES vacant_dirty → occupied. That refusal
refuses a HOUSEKEEPING RECORD. It does not and cannot refuse an admission —
`POST /api/admissions/admit` never calls this module, and must never be made to.
If a hospital in extremis puts a patient into an uncleaned bed, that happens, and
Orb's job is then to hold an honest record of it (`force_occupied`, which logs
the skipped clean rather than pretending it happened), not to stand in the
doorway.

The one clinical fact that crosses into this module is a BOOLEAN: whether the
departing admission carried isolation precautions, which becomes
`clean_type = 'terminal'`. The precaution text itself is never copied, and no
task row carries a patient id, a name or a diagnosis. A housekeeping board is
read on a corridor trolley by staff who hold no clinical grant.

CONVENTIONS
  * Every function is PURE SYNC and opens its own `db_session()`. Async callers
    go through `database.run_db` (tests/test_event_loop_discipline.py).
  * Never catch stdlib `sqlite3` exception classes — sqlcipher3 raises its own
    hierarchy, so `except sqlite3.IntegrityError` is dead code on the shipping
    encrypted DB. Import `DBIntegrityError` from `db_backend`.
  * No money. There is no `_paise` column in this module and there must not be
    one; services/money.py is the single money implementation.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import config
from database import db_session
from db_backend import DBIntegrityError
from migrations import HOUSEKEEPING_INDEXES, HOUSEKEEPING_TABLES
from migrations_his.m0053_adt_bed_master import add_housekeeping_unit_columns

logger = logging.getLogger("orb.housekeeping")

__all__ = [
    "HousekeepingError",
    "UNIT_STATES",
    "LEGAL_TRANSITIONS",
    "REQUEST_TYPES",
    "PRIORITIES",
    "PRIORITY_RANK",
    "CLEAN_TYPES",
    "LINEN_DIRECTIONS",
    "ensure_housekeeping_schema",
    "expected_minutes_for",
    "register_unit",
    "register_unit_on",
    "update_master_bed_on",
    "adopt_unit_on",
    "master_unit_code",
    "find_unit_by_code",
    "UnitManagedByBedSetup",
    "list_units",
    "get_unit",
    "transition_unit",
    "force_occupied",
    "raise_cleaning_request",
    "start_cleaning",
    "complete_cleaning",
    "cancel_cleaning",
    "list_tasks",
    "get_task",
    "sync_discharge_turnarounds",
    "ward_turnaround_report",
    "ward_readiness",
    "list_linen_items",
    "record_linen_movement",
    "linen_summary",
    "overview",
]


class HousekeepingError(ValueError):
    """A housekeeping request that cannot be honoured (illegal transition, bad
    state, unknown unit). Always caller error → 422 at the route."""


class UnitAlreadyRegistered(HousekeepingError):
    """The register form named a unit code that is already on the register.
    409 at the route: the request is fine, it collides with a record."""


class UnitManagedByBedSetup(HousekeepingError):
    """The unit is a bed the bed master registered (it carries a ward_id), so
    only Bed setup may change how it is described. 409 at the route: a second
    writer of the physical bed row is how its ward and label drift apart from
    the master's (adt-1, critique C5)."""


# ═════════════════════════════════════════════════════════════════════════════
#  THE STATE MACHINE
# ═════════════════════════════════════════════════════════════════════════════
#
# Five states. The four-state cycle the ward talks about, plus `out_of_service`
# for a unit that is physically unusable (bed broken, room being painted,
# plumbing out) — without it the only way to record a broken bed is to leave it
# looking dirty forever, which quietly poisons the turnaround report with an
# open task nobody can close.

UNIT_STATES: tuple[str, ...] = (
    "occupied",
    "vacant_dirty",
    "cleaning_in_progress",
    "vacant_ready",
    "out_of_service",
)

#: from-state → the states it may legally move to.
#:
#: THE TWO REFUSALS THAT CARRY THE MODULE, and why each one is not pedantry:
#:
#:   vacant_dirty → vacant_ready is ILLEGAL.
#:       This is the whole point of the module. If a bed can jump from dirty to
#:       ready, "ready" stops meaning "somebody cleaned this" and starts meaning
#:       "somebody pressed a button", the turnaround report loses its start
#:       timestamp, and the module is decoration. A clean must be STARTED, by a
#:       named person, before it can be finished.
#:
#:   vacant_dirty → occupied is ILLEGAL.
#:       An uncleaned bed cannot quietly acquire a patient in the record. It is
#:       still possible to record that it happened — `force_occupied` writes an
#:       explicit, reasoned, attributed event saying the clean was skipped — but
#:       it cannot happen by taking the ordinary path, because then nobody would
#:       ever know it had. See PILOT SAFETY above: this refuses a record, never
#:       a patient.
#:
#: And one that is easy to get wrong in the other direction:
#:
#:   out_of_service → vacant_ready is ILLEGAL; out_of_service → vacant_dirty is
#:       legal. A bed that comes back from maintenance has had an engineer and a
#:       toolbox on it. It needs cleaning before it is anybody's bed.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "occupied": frozenset({"vacant_dirty", "out_of_service"}),
    "vacant_dirty": frozenset({"cleaning_in_progress", "out_of_service"}),
    # cleaning_in_progress → vacant_dirty: a clean abandoned or interrupted, or
    # the bed re-soiled mid-clean. The task reopens; the turnaround clock, which
    # started when the unit became dirty, keeps running — which is correct.
    "cleaning_in_progress": frozenset({"vacant_ready", "vacant_dirty", "out_of_service"}),
    # vacant_ready → vacant_dirty: a spillage on a made bed. Common, and the
    # reason `raise_cleaning_request` can dirty a ready unit.
    "vacant_ready": frozenset({"occupied", "vacant_dirty", "out_of_service"}),
    "out_of_service": frozenset({"vacant_dirty"}),
}

REQUEST_TYPES: tuple[str, ...] = (
    "discharge_turnaround",
    "transfer_turnaround",
    "spillage",
    "routine",
    "deep_clean",
    "post_maintenance",
)

#: Request types that mean "the occupant has gone" — raising one of these on an
#: occupied unit vacates it in the same breath, because the two facts are the
#: same event and asking a ward clerk to record them separately guarantees they
#: will diverge.
_VACATING_REQUEST_TYPES: frozenset[str] = frozenset(
    {"discharge_turnaround", "transfer_turnaround"}
)

PRIORITIES: tuple[str, ...] = ("routine", "urgent", "immediate")

#: Queue order. A blood spillage in a corridor bay and a routine discharge
#: turnaround are not the same job, and a board that sorts purely by age puts the
#: spillage behind them. Lower rank sorts first.
PRIORITY_RANK: dict[str, int] = {"immediate": 0, "urgent": 1, "routine": 2}

CLEAN_TYPES: tuple[str, ...] = ("standard", "terminal")

LINEN_DIRECTIONS: tuple[str, ...] = ("issue", "return_soiled", "return_clean", "condemned")

#: Text caps. These columns are unbounded TEXT.
_MAX_TEXT = 2000
_MAX_LABEL = 120


# ═════════════════════════════════════════════════════════════════════════════
#  Schema
# ═════════════════════════════════════════════════════════════════════════════

_schema_ready = False
_schema_lock = threading.Lock()


def ensure_housekeeping_schema(force: bool = False) -> None:
    """Create the housekeeping tables if absent. Idempotent, cached, thread-safe.

    The DDL lives in `migrations.HOUSEKEEPING_TABLES` and is shared with
    migration 22 rather than copied, so the lazy path and the migration path can
    never drift into two different schemas — the failure mode where a test passes
    against a table shape production does not have.

    Lazy rather than at import: `DB_PATH` is bound at import time from config and
    the test harness rewrites it before `database` is first imported, so opening a
    connection at module import would race that.

    The bed master columns (ward_id, room_id, bed_class, is_temporary,
    retired_at; migration 53) are added by the same PRAGMA-guarded function the
    migration uses, so a table this path creates, or one it created before
    migration 53 existed, can never be left in the old shape.
    """
    global _schema_ready
    if _schema_ready and not force:
        return
    with _schema_lock:
        if _schema_ready and not force:
            return
        with db_session() as db:
            for statement in HOUSEKEEPING_TABLES:
                db.execute(statement)
            for statement in HOUSEKEEPING_INDEXES:
                db.execute(statement)
            add_housekeeping_unit_columns(db)
        _schema_ready = True


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any) -> Optional[datetime]:
    """Tolerant timestamp parse, always tz-aware UTC.

    Three shapes arrive here and all three are real: this module's own
    `datetime.now(timezone.utc).isoformat()`, SQLite's `CURRENT_TIMESTAMP`
    (`'YYYY-MM-DD HH:MM:SS'`, UTC, NO offset — `discharge_summaries.discharged_at`
    is one), and `...Z`-suffixed strings from imports. A naive value is read as
    UTC because that is what CURRENT_TIMESTAMP produces; guessing local time
    would shift every turnaround by the IST offset and make the report look
    plausible while being 5½ hours wrong.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _clean(value: object, limit: int = _MAX_TEXT) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


#: A bed state in the words the board uses, for refusals a person reads.
_STATE_WORDS = {
    "occupied": "occupied",
    "vacant_dirty": "waiting to be cleaned",
    "cleaning_in_progress": "being cleaned",
    "vacant_ready": "ready",
    "out_of_service": "out of service",
}


def _state_words(state: str) -> str:
    return _STATE_WORDS.get(state, state.replace("_", " "))


def _require(value: object, field: str, limit: int = _MAX_LABEL) -> str:
    text = _clean(value, limit)
    if not text:
        raise HousekeepingError(f"{field} is required")
    return text


def _one_of(value: object, allowed: Iterable[str], field: str) -> str:
    text = (str(value or "")).strip().lower()
    allowed = tuple(allowed)
    if text not in allowed:
        raise HousekeepingError(
            f"{field} must be one of {', '.join(allowed)} (got {value!r})"
        )
    return text


def _norm_key(value: object) -> str:
    """Ward/bed labels are typed by hand in six places. Compare them case- and
    space-insensitively or the discharge sweep will miss 'GM-1' against 'gm-1 '."""
    return " ".join(str(value or "").strip().lower().split())


def _canonical_ward(db, ward: str) -> str:
    """The hospital's own spelling of a ward typed at registration.

    'gm-1' registered beside a ward the hospital calls 'GM-1' became a second,
    phantom ward: its own readiness row with no capacity, and beds the discharge
    sweep and the board filed apart from the real ward's. Matched case- and
    space-insensitively (the same _norm_key the sweep uses) against the capacity
    screen's wards, then the wards patients are on, then wards already holding
    registered beds. A ward none of those know is kept as typed, whitespace
    tidied: a hospital may register beds before its first admission.
    """
    key = _norm_key(ward)
    known: list[str] = list(_ward_capacity_map())
    known += [r["ward"] for r in db.execute(
        "SELECT DISTINCT ward FROM patients WHERE ward IS NOT NULL AND ward <> ''"
    ).fetchall()]
    known += [r["ward"] for r in db.execute(
        "SELECT DISTINCT ward FROM housekeeping_units WHERE is_active = 1"
    ).fetchall()]
    for name in known:
        if name and _norm_key(name) == key:
            return str(name).strip()
    return " ".join(ward.split())


def expected_minutes_for(clean_type: str) -> int:
    """The turnaround target for this kind of clean, from site settings.

    Read through `config` rather than imported by value so an ops restart — or a
    test — sees the current setting. A terminal clean is expected to take longer,
    and that is the point of the flag: contact time for the disinfectant, curtain
    and linen change, PPE on and off. A board that expects 45 minutes for both
    reports every isolation room as late and teaches its users to ignore it.
    """
    if clean_type == "terminal":
        return int(config.HOUSEKEEPING_TERMINAL_CLEAN_MINUTES)
    return int(config.HOUSEKEEPING_STANDARD_CLEAN_MINUTES)


def _row(row) -> dict:
    return dict(row) if row is not None else {}


def _minutes_between(start: Any, end: Any) -> Optional[float]:
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds() / 60.0
    # A negative interval means the two timestamps disagree about the order of
    # events (a clock skew, a hand-edited row). Returning it would drag a median
    # down silently; dropping it is visible, because the sample count falls.
    return round(delta, 1) if delta >= 0 else None


def _percentile(values: list[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile. No numpy: this runs on a request thread and the
    samples are tens, not millions."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = max(1, min(len(ordered), int(-(-pct * len(ordered) // 100))))
    return round(ordered[rank - 1], 1)


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 1)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 1)


# ═════════════════════════════════════════════════════════════════════════════
#  The unit register
# ═════════════════════════════════════════════════════════════════════════════

def register_unit(
    *,
    unit_code: str,
    ward: str,
    room: str | None = None,
    bed_label: str | None = None,
    unit_type: str = "bed",
    is_isolation_room: bool = False,
    initial_state: str = "vacant_dirty",
    notes: str | None = None,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    create_only: bool = False,
) -> dict:
    """Put one physical bed / room / bay / theatre into the register.

    `initial_state` DEFAULTS TO 'vacant_dirty' and that is not a pessimism
    reflex. Registering a bed is inventory; 'vacant_ready' is a claim that
    somebody cleaned it. If registration could assert readiness, a site that
    imports its bed list on a Tuesday afternoon would publish "48 beds ready"
    without a single person having walked a ward — and the first thing anyone
    would do with that number is offer a bed. A site that genuinely knows the
    state of a bed at registration may pass it; the DEFAULT never claims a clean
    nobody did.

    Re-registering an existing `unit_code` UPDATES its descriptive fields and
    leaves its STATE and its history alone. A ward re-import must not silently
    reset every bed to dirty and orphan the open tasks. A blank room, bed label
    or note keeps what is on file: the register form sends blanks, and a wiped
    bed label is how the discharge sweep stopped matching the bed.

    `create_only` is the register form: a code already on the register is
    refused (UnitAlreadyRegistered, naming the bed) and nothing is changed.

    A thin wrapper over `register_unit_on` in a session of its own. A unit the
    bed master registered (it carries a ward_id) is refused here
    (UnitManagedByBedSetup): Bed setup is its only writer.
    """
    ensure_housekeeping_schema()
    with db_session() as db:
        return register_unit_on(
            db, unit_code=unit_code, ward=ward, room=room, bed_label=bed_label,
            unit_type=unit_type, is_isolation_room=is_isolation_room,
            initial_state=initial_state, notes=notes, actor_user_id=actor_user_id,
            actor_role=actor_role, create_only=create_only,
        )


def master_unit_code(ward_code: str, bed_label: str) -> str:
    """The unit code of a bed the bed master registers: '<ward code>/<bed label>'.

    idx_hk_units_code is global, so the ward code is part of every bed's unit
    code, and bed_master_service refuses '/' in ward codes and bed labels, so
    two wards can never produce the same code."""
    return f"{ward_code}/{bed_label}"


def _has_ward_id(row) -> Optional[str]:
    """The unit's ward_id, or None. Tolerates a row read before the bed master
    columns existed."""
    if row is None:
        return None
    return row["ward_id"] if "ward_id" in row.keys() else None


def register_unit_on(
    db,
    *,
    unit_code: str | None = None,
    ward: str | None = None,
    room: str | None = None,
    bed_label: str | None = None,
    unit_type: str = "bed",
    is_isolation_room: bool = False,
    initial_state: str = "vacant_dirty",
    notes: str | None = None,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    create_only: bool = False,
    ward_id: str | None = None,
    room_id: str | None = None,
    bed_class: str | None = None,
    is_temporary: bool = False,
) -> dict:
    """`register_unit` on the CALLER's connection, so many units, their events
    and the caller's audit commit or roll back together (bed_master_service.
    register_beds registers a whole ward in one immediate session).

    The caller owns the session and must have called
    `ensure_housekeeping_schema()` BEFORE opening it: this function never opens
    a session of its own.

    Without `ward_id` it is exactly `register_unit`: the typed ward is matched to
    the hospital's spelling through `_canonical_ward`, and re-registering an
    existing code updates its description.

    With `ward_id` it registers a BED the bed master owns:
      * the ward text is wards.code and the room text ward_rooms.code, read from
        the master, so a typed spelling can never override them, and
        `_canonical_ward` is skipped;
      * the unit code is always '<ward code>/<bed label>' (master_unit_code);
      * an existing unit code is always refused (UnitAlreadyRegistered): a
        master bed is changed through update_master_bed_on, never by
        re-registering it.
    """
    if ward_id is None:
        if room_id is not None or bed_class is not None or is_temporary:
            raise HousekeepingError(
                "A room link, bed class or temporary bed is set in Bed setup, "
                "which needs the ward the bed is on"
            )
        code = _require(unit_code, "unit_code")
        ward_name = _require(ward, "ward")
    kind = _one_of(unit_type, ("bed", "room", "bay", "theatre"), "unit_type")
    state = _one_of(initial_state, UNIT_STATES, "initial_state")
    now = _now()

    if ward_id is None:
        ward_name = _canonical_ward(db, ward_name)
        room_text = _clean(room, _MAX_LABEL)
        label = _clean(bed_label, _MAX_LABEL)
    else:
        if kind != "bed":
            raise HousekeepingError("Bed setup registers beds only")
        label = _require(bed_label, "bed_label")
        ward_row = db.execute("SELECT id, code FROM wards WHERE id = ?", (ward_id,)).fetchone()
        if ward_row is None:
            raise HousekeepingError("No ward with that id is set up in Bed setup")
        ward_name = ward_row["code"]
        room_text = None
        if room_id is not None:
            room_row = db.execute(
                "SELECT id, ward_id, code FROM ward_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room_row is None or room_row["ward_id"] != ward_id:
                raise HousekeepingError(f"That room is not on {ward_name}")
            room_text = room_row["code"]
        code = master_unit_code(ward_name, label)
        typed = _clean(unit_code)
        if typed is not None and typed != code:
            raise HousekeepingError(
                f"Bed {label} on {ward_name} is coded {code}. The unit code of a bed "
                "in Bed setup is not chosen separately."
            )

    existing = db.execute(
        "SELECT * FROM housekeeping_units WHERE unit_code = ?", (code,)
    ).fetchone()
    if existing and (create_only or ward_id is not None):
        where = ", ".join(x for x in (
            existing["ward"],
            f"bed {existing['bed_label']}" if existing["bed_label"] else None,
        ) if x)
        raise UnitAlreadyRegistered(
            f"{code} is already registered ({where}). Nothing was changed. "
            "Use a different unit code, or change that unit from its card."
        )
    if existing and _has_ward_id(existing):
        raise UnitManagedByBedSetup(
            f"{code} is a bed set up in Bed setup, so it is changed there. "
            "Nothing was changed."
        )
    if existing:
        db.execute(
            """UPDATE housekeeping_units
                  SET ward = ?, room = COALESCE(?, room),
                      bed_label = COALESCE(?, bed_label), unit_type = ?,
                      is_isolation_room = ?, notes = COALESCE(?, notes),
                      is_active = 1, updated_at = ?
                WHERE id = ?""",
            (
                ward_name, room_text, label,
                kind, 1 if is_isolation_room else 0, _clean(notes), now,
                existing["id"],
            ),
        )
        return _row(
            db.execute(
                "SELECT * FROM housekeeping_units WHERE id = ?", (existing["id"],)
            ).fetchone()
        )

    unit_id = f"HKU{uuid.uuid4().hex[:12].upper()}"
    if ward_id is None:
        db.execute(
            """INSERT INTO housekeeping_units
                   (id, unit_code, ward, room, bed_label, unit_type, is_isolation_room,
                    state, state_since, is_active, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                unit_id, code, ward_name, room_text,
                label, kind, 1 if is_isolation_room else 0,
                state, now, _clean(notes), now, now,
            ),
        )
    else:
        db.execute(
            """INSERT INTO housekeeping_units
                   (id, unit_code, ward, room, bed_label, unit_type, is_isolation_room,
                    state, state_since, is_active, notes, created_at, updated_at,
                    ward_id, room_id, bed_class, is_temporary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                unit_id, code, ward_name, room_text,
                label, kind, 1 if is_isolation_room else 0,
                state, now, _clean(notes), now, now,
                ward_id, room_id, _clean(bed_class, _MAX_LABEL), 1 if is_temporary else 0,
            ),
        )
    # The register's first event. Without it a unit created directly as
    # 'vacant_ready' would have a state nobody signed for.
    db.execute(
        """INSERT INTO housekeeping_unit_events
               (unit_id, from_state, to_state, reason, actor_user_id, actor_role, occurred_at)
           VALUES (?, NULL, ?, ?, ?, ?, ?)""",
        (unit_id, state, "unit registered", actor_user_id, actor_role, now),
    )
    return _row(
        db.execute("SELECT * FROM housekeeping_units WHERE id = ?", (unit_id,)).fetchone()
    )


#: "Leave this field as it is" for update_master_bed_on, distinct from None
#: ("clear it").
_KEEP = object()


def update_master_bed_on(
    db,
    unit_id: str,
    *,
    room_id: Any = _KEEP,
    bed_class: Any = _KEEP,
    is_isolation_room: Any = _KEEP,
    notes: Any = _KEEP,
    bed_label: Any = _KEEP,
) -> dict:
    """Change how a bed the bed master registered is described, on the
    CALLER's connection. Only bed_master_service calls this; it owns the
    session, the reference guards (a label is immutable once anything refers
    to the bed), the bed_master_events row and the audit.

    Never touches the cleaning state or its ledger: those stay housekeeping's.
    A field left as _KEEP is unchanged; room_id or bed_class None clears it.
    A new label also changes the unit code ('<ward code>/<label>').
    """
    unit = db.execute("SELECT * FROM housekeeping_units WHERE id = ?", (unit_id,)).fetchone()
    if unit is None or not _has_ward_id(unit):
        raise HousekeepingError("No bed with that id is set up in Bed setup")
    sets: list[str] = []
    params: list[Any] = []
    if room_id is not _KEEP:
        if room_id is None:
            sets += ["room_id = NULL", "room = NULL"]
        else:
            room_row = db.execute(
                "SELECT id, ward_id, code FROM ward_rooms WHERE id = ?", (room_id,)
            ).fetchone()
            if room_row is None or room_row["ward_id"] != unit["ward_id"]:
                raise HousekeepingError(f"That room is not on {unit['ward']}")
            sets += ["room_id = ?", "room = ?"]
            params += [room_row["id"], room_row["code"]]
    if bed_class is not _KEEP:
        sets.append("bed_class = ?")
        params.append(_clean(bed_class, _MAX_LABEL))
    if is_isolation_room is not _KEEP:
        sets.append("is_isolation_room = ?")
        params.append(1 if is_isolation_room else 0)
    if notes is not _KEEP:
        sets.append("notes = ?")
        params.append(_clean(notes))
    if bed_label is not _KEEP:
        label = _require(bed_label, "bed_label")
        ward_row = db.execute("SELECT code FROM wards WHERE id = ?", (unit["ward_id"],)).fetchone()
        if ward_row is None:
            raise HousekeepingError("The ward this bed is on is not set up in Bed setup")
        sets += ["bed_label = ?", "unit_code = ?"]
        params += [label, master_unit_code(ward_row["code"], label)]
    if not sets:
        return _row(unit)
    sets.append("updated_at = ?")
    params.append(_now())
    db.execute(
        f"UPDATE housekeeping_units SET {', '.join(sets)} WHERE id = ?",
        (*params, unit_id),
    )
    return _row(db.execute("SELECT * FROM housekeeping_units WHERE id = ?", (unit_id,)).fetchone())


def adopt_unit_on(
    db,
    unit_id: str,
    *,
    ward_id: str,
    bed_label: str,
    room_id: str | None = None,
    bed_class: str | None = None,
    is_isolation_room: bool | None = None,
) -> dict:
    """Link a bed that was typed into the housekeeping register before Bed
    setup existed to the master, on the CALLER's connection, keeping its
    cleaning state, its ledger and its open tasks. Only bed_master_service
    calls this, and only when the bed manager asked to adopt existing beds.

    The ward and room text become the master's spelling, the label the
    master's label, and the unit code '<ward code>/<label>'.
    """
    unit = db.execute("SELECT * FROM housekeeping_units WHERE id = ?", (unit_id,)).fetchone()
    if unit is None:
        raise HousekeepingError("No unit with that id is on the housekeeping register")
    if _has_ward_id(unit):
        raise UnitManagedByBedSetup(f"{unit['unit_code']} is already a bed in Bed setup")
    if unit["unit_type"] != "bed":
        raise HousekeepingError(f"{unit['unit_code']} is registered as a {unit['unit_type']}, not a bed")
    label = _require(bed_label, "bed_label")
    ward_row = db.execute("SELECT id, code FROM wards WHERE id = ?", (ward_id,)).fetchone()
    if ward_row is None:
        raise HousekeepingError("No ward with that id is set up in Bed setup")
    room_text = None
    if room_id is not None:
        room_row = db.execute(
            "SELECT id, ward_id, code FROM ward_rooms WHERE id = ?", (room_id,)
        ).fetchone()
        if room_row is None or room_row["ward_id"] != ward_id:
            raise HousekeepingError(f"That room is not on {ward_row['code']}")
        room_text = room_row["code"]
    isolation = unit["is_isolation_room"] if is_isolation_room is None else (1 if is_isolation_room else 0)
    db.execute(
        """UPDATE housekeeping_units
              SET unit_code = ?, ward = ?, room = COALESCE(?, room), bed_label = ?,
                  ward_id = ?, room_id = ?, bed_class = ?, is_isolation_room = ?,
                  is_active = 1, updated_at = ?
            WHERE id = ?""",
        (
            master_unit_code(ward_row["code"], label), ward_row["code"], room_text, label,
            ward_id, room_id, _clean(bed_class, _MAX_LABEL), isolation, _now(), unit_id,
        ),
    )
    return _row(db.execute("SELECT * FROM housekeeping_units WHERE id = ?", (unit_id,)).fetchone())


def find_unit_by_code(unit_code: str) -> dict | None:
    """The unit with exactly this code, or None. Read only."""
    ensure_housekeeping_schema()
    code = _clean(unit_code)
    if not code:
        return None
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM housekeeping_units WHERE unit_code = ?", (code,)
        ).fetchone()
    return _row(row) if row is not None else None


def list_units(ward: str | None = None, *, include_inactive: bool = False) -> list[dict]:
    ensure_housekeeping_schema()
    sql = "SELECT * FROM housekeeping_units WHERE 1 = 1"
    params: list[Any] = []
    if not include_inactive:
        sql += " AND is_active = 1"
    if ward:
        sql += " AND ward = ?"
        params.append(ward)
    sql += " ORDER BY ward, unit_code"
    with db_session() as db:
        rows = db.execute(sql, tuple(params)).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        unit = _row(r)
        since = _parse_ts(unit.get("state_since"))
        unit["minutes_in_state"] = (
            round((now - since).total_seconds() / 60.0, 1) if since else None
        )
        unit["is_isolation_room"] = bool(unit.get("is_isolation_room"))
        unit["is_active"] = bool(unit.get("is_active"))
        out.append(unit)
    return out


def get_unit(unit_id: str) -> dict:
    ensure_housekeeping_schema()
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ? OR unit_code = ?",
            (unit_id, unit_id),
        ).fetchone()
    if row is None:
        raise HousekeepingError(f"No such housekeeping unit: {unit_id}")
    unit = _row(row)
    unit["is_isolation_room"] = bool(unit.get("is_isolation_room"))
    unit["is_active"] = bool(unit.get("is_active"))
    return unit


# ═════════════════════════════════════════════════════════════════════════════
#  The one state writer
# ═════════════════════════════════════════════════════════════════════════════

def _apply_transition(
    db,
    unit: dict,
    to_state: str,
    *,
    actor_user_id: str | None,
    actor_role: str | None,
    reason: str | None,
    task_id: str | None,
    occurred_at: str,
    allow_illegal: bool = False,
) -> dict:
    """Move a unit and ledger it, INSIDE the caller's transaction.

    `allow_illegal` exists for exactly one caller (`force_occupied`) and the
    event row it writes says so. Nothing else may set it: an "override" flag that
    spreads is a state machine that has stopped existing.
    """
    from_state = unit["state"]
    if from_state == to_state:
        # Not an error, and not a no-op either: the ledger records the assertion
        # so "who said this bed was still dirty at 4pm" has an answer.
        db.execute(
            """INSERT INTO housekeeping_unit_events
                   (unit_id, from_state, to_state, task_id, reason, actor_user_id,
                    actor_role, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (unit["id"], from_state, to_state, task_id,
             _clean(reason) or "restated", actor_user_id, actor_role, occurred_at),
        )
        return unit

    if not allow_illegal and to_state not in LEGAL_TRANSITIONS.get(from_state, frozenset()):
        legal = ", ".join(_state_words(s) for s in sorted(LEGAL_TRANSITIONS.get(from_state, frozenset())))
        raise HousekeepingError(
            f"{unit['unit_code']} is {_state_words(from_state)}, so it cannot be marked "
            f"{_state_words(to_state)}. From here it can be marked: {legal or 'nothing'}."
        )

    db.execute(
        "UPDATE housekeeping_units SET state = ?, state_since = ?, updated_at = ? WHERE id = ?",
        (to_state, occurred_at, occurred_at, unit["id"]),
    )
    db.execute(
        """INSERT INTO housekeeping_unit_events
               (unit_id, from_state, to_state, task_id, reason, actor_user_id,
                actor_role, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (unit["id"], from_state, to_state, task_id, _clean(reason),
         actor_user_id, actor_role, occurred_at),
    )
    updated = dict(unit)
    updated["state"] = to_state
    updated["state_since"] = occurred_at
    return updated


def transition_unit(
    unit_id: str,
    to_state: str,
    *,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    reason: str | None = None,
    task_id: str | None = None,
) -> dict:
    """The public state writer. Every legal move goes through here."""
    ensure_housekeeping_schema()
    target = _one_of(to_state, UNIT_STATES, "state")
    now = _now()
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ? OR unit_code = ?",
            (unit_id, unit_id),
        ).fetchone()
        if row is None:
            raise HousekeepingError(f"No such housekeeping unit: {unit_id}")
        unit = _row(row)
        updated = _apply_transition(
            db, unit, target,
            actor_user_id=actor_user_id, actor_role=actor_role,
            reason=reason, task_id=task_id, occurred_at=now,
        )
        # Moving a unit back to dirty while a clean was running reopens its task:
        # the work is not done and the queue must still show it.
        if unit["state"] == "cleaning_in_progress" and target == "vacant_dirty":
            db.execute(
                "UPDATE housekeeping_tasks SET status = 'open', started_at = NULL, "
                "started_by_user_id = NULL WHERE unit_id = ? AND status = 'in_progress'",
                (unit["id"],),
            )
    updated["is_isolation_room"] = bool(updated.get("is_isolation_room"))
    return updated


def force_occupied(
    unit_id: str,
    *,
    reason: str,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
) -> dict:
    """Record that a patient went into a unit that was NOT vacant_ready.

    THIS IS NOT A BACK DOOR, IT IS THE HONEST RECORD OF ONE. A hospital at
    capacity at 3am will sometimes put a patient into a bed that has not been
    terminally cleaned. Refusing to record it does not stop it happening; it
    just means the only trace is a bed that stays 'vacant_dirty' with a patient
    in it and a turnaround report that quietly never closes.

    So the move is allowed, is REQUIRED to carry a reason, is written to the
    ledger with `to_state = 'occupied'` and a `from_state` that says what was
    skipped, and any open cleaning task on the unit is cancelled with that same
    reason rather than left to rot in the queue.

    It does not gate anything. Admission never calls it — a ward clerk or bed
    manager records it after the fact.
    """
    ensure_housekeeping_schema()
    why = _require(reason, "reason", _MAX_TEXT)
    now = _now()
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ? OR unit_code = ?",
            (unit_id, unit_id),
        ).fetchone()
        if row is None:
            raise HousekeepingError(f"No such housekeeping unit: {unit_id}")
        unit = _row(row)
        if unit["state"] == "occupied":
            unit["is_isolation_room"] = bool(unit.get("is_isolation_room"))
            unit["clean_was_skipped"] = False
            return unit
        skipped = unit["state"] != "vacant_ready"
        updated = _apply_transition(
            db, unit, "occupied",
            actor_user_id=actor_user_id, actor_role=actor_role,
            reason=(f"OCCUPIED WITHOUT COMPLETED CLEAN (was {unit['state']}): {why}"
                    if skipped else why),
            task_id=None, occurred_at=now, allow_illegal=True,
        )
        if skipped:
            db.execute(
                """UPDATE housekeeping_tasks
                      SET status = 'cancelled', cancelled_at = ?, cancelled_by_user_id = ?,
                          cancel_reason = ?
                    WHERE unit_id = ? AND status IN ('open', 'in_progress')""",
                (now, actor_user_id,
                 f"Unit occupied before the clean finished: {why}", unit["id"]),
            )
    updated["is_isolation_room"] = bool(updated.get("is_isolation_room"))
    updated["clean_was_skipped"] = skipped
    return updated


# ═════════════════════════════════════════════════════════════════════════════
#  Cleaning requests
# ═════════════════════════════════════════════════════════════════════════════

def _insert_task(
    db,
    *,
    unit: dict,
    request_type: str,
    priority: str,
    clean_type: str,
    description: str | None,
    precaution_note: str | None,
    source_type: str,
    source_ref: str | None,
    requested_by_user_id: str | None,
    requested_at: str,
    vacated_at: str | None,
) -> str:
    task_id = f"HKT{uuid.uuid4().hex[:12].upper()}"
    db.execute(
        """INSERT INTO housekeeping_tasks
               (id, unit_id, ward, request_type, priority, clean_type, expected_minutes,
                status, source_type, source_ref, description, precaution_note,
                vacated_at, requested_at, requested_by_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id, unit["id"], unit["ward"], request_type, priority, clean_type,
            expected_minutes_for(clean_type), source_type, source_ref,
            _clean(description), _clean(precaution_note), vacated_at,
            requested_at, requested_by_user_id, requested_at,
        ),
    )
    return task_id


def raise_cleaning_request(
    *,
    unit_id: str,
    request_type: str = "routine",
    priority: str = "routine",
    clean_type: str = "standard",
    description: str | None = None,
    precaution_note: str | None = None,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
) -> dict:
    """Raise a cleaning request against a unit.

    PRIORITY IS THE CALLER'S, NOT DERIVED. A discharge turnaround on a quiet
    Tuesday is routine; the same turnaround when four ambulances are inbound is
    urgent; a blood spillage in a corridor bay is immediate. Only the person
    looking at the ward knows which, so the module takes the answer rather than
    inventing one from `request_type` — the queue then sorts by priority first
    and age second (`PRIORITY_RANK`).

    A turnaround request on an OCCUPIED unit vacates it in the same transaction:
    "the patient has left and this bed needs cleaning" is one event, and making a
    clerk record it as two guarantees the two will disagree.

    A request on a vacant_ready unit dirties it — that is a spillage on a made
    bed, which is the single most common ad-hoc housekeeping call there is.
    """
    ensure_housekeeping_schema()
    kind = _one_of(request_type, REQUEST_TYPES, "request_type")
    prio = _one_of(priority, PRIORITIES, "priority")
    clean = _one_of(clean_type, CLEAN_TYPES, "clean_type")
    now = _now()

    with db_session() as db:
        row = db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ? OR unit_code = ?",
            (unit_id, unit_id),
        ).fetchone()
        if row is None:
            raise HousekeepingError(f"No such housekeeping unit: {unit_id}")
        unit = _row(row)

        if unit["state"] == "out_of_service" and kind != "post_maintenance":
            raise HousekeepingError(
                f"{unit['unit_code']} is out of service. Bring it back with a "
                f"'Back from maintenance' request, which puts it on the cleaning list."
            )

        vacated_at: str | None = None
        if unit["state"] == "occupied":
            if kind not in _VACATING_REQUEST_TYPES:
                raise HousekeepingError(
                    f"{unit['unit_code']} is occupied. Only a discharge or transfer "
                    f"turnaround can be raised on an occupied bed."
                )
            unit = _apply_transition(
                db, unit, "vacant_dirty",
                actor_user_id=actor_user_id, actor_role=actor_role,
                reason=f"{kind} raised", task_id=None, occurred_at=now,
            )
            vacated_at = now
        elif unit["state"] == "vacant_ready":
            unit = _apply_transition(
                db, unit, "vacant_dirty",
                actor_user_id=actor_user_id, actor_role=actor_role,
                reason=f"{kind} raised on a ready unit", task_id=None, occurred_at=now,
            )
            vacated_at = now
        elif unit["state"] == "out_of_service":
            unit = _apply_transition(
                db, unit, "vacant_dirty",
                actor_user_id=actor_user_id, actor_role=actor_role,
                reason="returned from maintenance", task_id=None, occurred_at=now,
            )
            vacated_at = now
        else:
            # Already dirty or already being cleaned: the clock started when it
            # became dirty, and THAT is the timestamp the turnaround is measured
            # from — not the moment somebody got round to raising the request.
            vacated_at = unit.get("state_since") or now

        task_id = _insert_task(
            db, unit=unit, request_type=kind, priority=prio, clean_type=clean,
            description=description, precaution_note=precaution_note,
            source_type="manual", source_ref=None,
            requested_by_user_id=actor_user_id, requested_at=now, vacated_at=vacated_at,
        )
        task = _row(db.execute("SELECT * FROM housekeeping_tasks WHERE id = ?", (task_id,)).fetchone())
    return _decorate_task(task)


def start_cleaning(
    task_id: str,
    *,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
) -> dict:
    ensure_housekeeping_schema()
    now = _now()
    with db_session() as db:
        task = _row(db.execute("SELECT * FROM housekeeping_tasks WHERE id = ?", (task_id,)).fetchone())
        if not task:
            raise HousekeepingError(f"No such cleaning task: {task_id}")
        if task["status"] != "open":
            raise HousekeepingError(
                f"Cleaning task {task_id} is {task['status']}; only an open task can be started."
            )
        unit = _row(db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ?", (task["unit_id"],)
        ).fetchone())
        if not unit:
            raise HousekeepingError(f"Task {task_id} points at a unit that no longer exists")
        _apply_transition(
            db, unit, "cleaning_in_progress",
            actor_user_id=actor_user_id, actor_role=actor_role,
            reason="cleaning started", task_id=task["id"], occurred_at=now,
        )
        db.execute(
            "UPDATE housekeeping_tasks SET status = 'in_progress', started_at = ?, "
            "started_by_user_id = ? WHERE id = ?",
            (now, actor_user_id, task["id"]),
        )
        return _decorate_task(_row(
            db.execute("SELECT * FROM housekeeping_tasks WHERE id = ?", (task["id"],)).fetchone()
        ))


def complete_cleaning(
    task_id: str,
    *,
    actor_user_id: str | None = None,
    actor_role: str | None = None,
    note: str | None = None,
) -> dict:
    """Finish a clean: the unit becomes vacant_ready and `ready_at` is stamped.

    `ready_at` is the turnaround clock's STOP, and it is stamped in the same
    transaction as the unit transition so the task's idea of when the bed became
    usable and the ledger's can never disagree.
    """
    ensure_housekeeping_schema()
    now = _now()
    with db_session() as db:
        task = _row(db.execute("SELECT * FROM housekeeping_tasks WHERE id = ?", (task_id,)).fetchone())
        if not task:
            raise HousekeepingError(f"No such cleaning task: {task_id}")
        if task["status"] != "in_progress":
            raise HousekeepingError(
                f"Cleaning task {task_id} is {task['status']}; a clean must be STARTED "
                f"before it can be completed. That is what makes 'ready' mean somebody "
                f"cleaned this bed."
            )
        unit = _row(db.execute(
            "SELECT * FROM housekeeping_units WHERE id = ?", (task["unit_id"],)
        ).fetchone())
        if not unit:
            raise HousekeepingError(f"Task {task_id} points at a unit that no longer exists")
        _apply_transition(
            db, unit, "vacant_ready",
            actor_user_id=actor_user_id, actor_role=actor_role,
            reason=_clean(note) or "cleaning complete", task_id=task["id"], occurred_at=now,
        )
        db.execute(
            "UPDATE housekeeping_tasks SET status = 'done', completed_at = ?, "
            "completed_by_user_id = ?, ready_at = ?, description = COALESCE(?, description) "
            "WHERE id = ?",
            (now, actor_user_id, now, _clean(note), task["id"]),
        )
        return _decorate_task(_row(
            db.execute("SELECT * FROM housekeeping_tasks WHERE id = ?", (task["id"],)).fetchone()
        ))


def cancel_cleaning(
    task_id: str,
    *,
    reason: str,
    actor_user_id: str | None = None,
) -> dict:
    """Cancel a request. The UNIT'S STATE IS NOT TOUCHED — a cancelled request
    does not make a dirty bed clean, and the commonest reason to cancel is a
    duplicate, where quietly moving the unit would undo the surviving task."""
    ensure_housekeeping_schema()
    why = _require(reason, "reason", _MAX_TEXT)
    now = _now()
    with db_session() as db:
        task = _row(db.execute("SELECT * FROM housekeeping_tasks WHERE id = ?", (task_id,)).fetchone())
        if not task:
            raise HousekeepingError(f"No such cleaning task: {task_id}")
        if task["status"] in ("done", "cancelled"):
            raise HousekeepingError(f"Cleaning task {task_id} is already {task['status']}")
        db.execute(
            "UPDATE housekeeping_tasks SET status = 'cancelled', cancelled_at = ?, "
            "cancelled_by_user_id = ?, cancel_reason = ? WHERE id = ?",
            (now, actor_user_id, why, task["id"]),
        )
        return _decorate_task(_row(
            db.execute("SELECT * FROM housekeeping_tasks WHERE id = ?", (task["id"],)).fetchone()
        ))


def _decorate_task(task: dict, *, now: datetime | None = None) -> dict:
    """Add the derived numbers a board needs, WITHOUT storing them.

    `turnaround_minutes` is ready_at − vacated_at and is None until the unit is
    ready. `cleaning_minutes` is completed_at − started_at. They are different
    questions: the first is how long the bed was out of action, the second is how
    long somebody was working on it, and the difference between them is the queue
    delay this whole module exists to expose.
    """
    if not task:
        return {}
    now = now or datetime.now(timezone.utc)
    out = dict(task)
    out["turnaround_minutes"] = _minutes_between(task.get("vacated_at"), task.get("ready_at"))
    out["cleaning_minutes"] = _minutes_between(task.get("started_at"), task.get("completed_at"))
    out["is_terminal_clean"] = task.get("clean_type") == "terminal"
    out["priority_rank"] = PRIORITY_RANK.get(task.get("priority", "routine"), 9)

    started = _parse_ts(task.get("vacated_at")) or _parse_ts(task.get("requested_at"))
    if task.get("status") in ("open", "in_progress") and started:
        elapsed = round((now - started).total_seconds() / 60.0, 1)
        out["elapsed_minutes"] = max(0.0, elapsed)
        out["minutes_over_expected"] = round(
            max(0.0, out["elapsed_minutes"] - float(task.get("expected_minutes") or 0)), 1
        )
        out["is_overdue"] = out["elapsed_minutes"] > float(task.get("expected_minutes") or 0)
    else:
        out["elapsed_minutes"] = None
        turn = out["turnaround_minutes"]
        out["minutes_over_expected"] = (
            round(max(0.0, turn - float(task.get("expected_minutes") or 0)), 1)
            if turn is not None else None
        )
        out["is_overdue"] = (
            turn is not None and turn > float(task.get("expected_minutes") or 0)
        )
    return out


def list_tasks(
    *,
    ward: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """The cleaning queue, ordered the way it should be worked.

    ORDER BY priority THEN age. A board sorted purely by age buries an immediate
    spillage behind six routine turnarounds raised before it.
    """
    ensure_housekeeping_schema()
    sql = "SELECT * FROM housekeeping_tasks WHERE 1 = 1"
    params: list[Any] = []
    if ward:
        sql += " AND ward = ?"
        params.append(ward)
    if status:
        sql += " AND status = ?"
        params.append(_one_of(status, ("open", "in_progress", "done", "cancelled"), "status"))
    if priority:
        sql += " AND priority = ?"
        params.append(_one_of(priority, PRIORITIES, "priority"))
    sql += (
        " ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,"
        " CASE priority WHEN 'immediate' THEN 0 WHEN 'urgent' THEN 1 ELSE 2 END,"
        " COALESCE(vacated_at, requested_at) ASC LIMIT ?"
    )
    params.append(max(1, min(1000, int(limit))))
    with db_session() as db:
        rows = db.execute(sql, tuple(params)).fetchall()
        codes = {
            r["id"]: r["unit_code"]
            for r in db.execute("SELECT id, unit_code FROM housekeeping_units").fetchall()
        }
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        task = _decorate_task(_row(r), now=now)
        task["unit_code"] = codes.get(task.get("unit_id"), "—")
        # `source_ref` is deliberately NOT published. It is a discharge record
        # id; a housekeeping board has no business carrying a handle to one.
        task.pop("source_ref", None)
        out.append(task)
    return out


def get_task(task_id: str) -> dict:
    ensure_housekeeping_schema()
    with db_session() as db:
        row = db.execute("SELECT * FROM housekeeping_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise HousekeepingError(f"No such cleaning task: {task_id}")
        task = _decorate_task(_row(row))
        unit = db.execute(
            "SELECT unit_code FROM housekeeping_units WHERE id = ?", (task["unit_id"],)
        ).fetchone()
        events = db.execute(
            "SELECT from_state, to_state, reason, actor_user_id, actor_role, occurred_at "
            "FROM housekeeping_unit_events WHERE unit_id = ? ORDER BY id DESC LIMIT 50",
            (task["unit_id"],),
        ).fetchall()
    task["unit_code"] = unit["unit_code"] if unit else "—"
    task["unit_events"] = [_row(e) for e in events]
    task.pop("source_ref", None)
    return task


# ═════════════════════════════════════════════════════════════════════════════
#  THE DISCHARGE SWEEP — exactly one turnaround task per discharge
# ═════════════════════════════════════════════════════════════════════════════

def sync_discharge_turnarounds(
    *,
    lookback_hours: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Create a turnaround task for every recent discharge that has not got one.

    ── WHY THIS IS A PULL AND NOT A PUSH ────────────────────────────────────────
    The obvious implementation is a line in `discharge_service.discharge_patient`
    that raises a cleaning task. It is forbidden, and rightly: the discharge write
    path is a clinical write path, and NOTHING this module does may be able to
    make a discharge slower, or fail. A housekeeping table that is locked, a
    schema that is mid-migration, an exception in a facilities module — none of
    those may reach a patient going home.

    So the sweep reads instead. It runs whenever the housekeeping board is
    opened, looks back a bounded window (config.HOUSEKEEPING_DISCHARGE_LOOKBACK_HOURS)
    and inserts what is missing. The consequence is that the SAME discharge is
    offered to the inserter over and over — every board load, every tab, every
    user — which makes idempotency not a nicety but the entire correctness
    condition.

    ── HOW EXACTLY-ONCE IS ACTUALLY ACHIEVED ───────────────────────────────────
    By the DATABASE, via `idx_hk_tasks_source` UNIQUE (source_type, source_ref).
    Not by a SELECT-then-INSERT check, which two board loads in two tabs will
    interleave, and not by an in-process set, which dies with the worker. The
    insert is attempted and `DBIntegrityError` is swallowed as "already done".
    `source_ref` is the `discharge_summaries.id` — one row per discharge event,
    so a patient readmitted and discharged again correctly gets a second task.
    An end of stay the beds and admissions module recorded with no summary (a
    death, a LAMA, an absconded closure; adt-8c) is keyed on its own discharge
    movement id instead ('MOV-...', which never collides with a summary id).

    ── WHICH BED (adt-8c) ──────────────────────────────────────────────────────
    The bed of the stay that ENDED (services/stay_times.py), by its master bed
    id when it has one, else its typed ward and bed. Never the patient's
    current labels, which after a readmission name the NEW bed. A bed a later
    admission holds by master bed id is not flipped to vacant_dirty.

    (`DBIntegrityError` from db_backend, never `sqlite3.IntegrityError`: sqlcipher3
    raises its own hierarchy and the stdlib class is dead code on the shipping
    encrypted DB.)

    ── WHAT IT DELIBERATELY DOES NOT DO ────────────────────────────────────────
    It does not invent a unit. A discharge from a bed that is not in the register
    produces NO task and is counted in `unmatched` and listed in `unmatched_wards`
    — because a housekeeping module that silently ignores two thirds of a
    hospital's discharges looks exactly like one that is working.

    Returns a summary: {created, already_present, unmatched, unmatched_wards,
    scanned, lookback_hours}.
    """
    ensure_housekeeping_schema()
    hours = int(lookback_hours if lookback_hours is not None
                else config.HOUSEKEEPING_DISCHARGE_LOOKBACK_HOURS)
    hours = max(1, min(24 * 30, hours))
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    created = 0
    already = 0
    unmatched = 0
    unmatched_wards: dict[str, int] = {}
    scanned = 0

    from services import stay_times

    with db_session() as db:
        units = db.execute(
            "SELECT id, unit_code, ward, bed_label, state, state_since, is_isolation_room "
            "FROM housekeeping_units WHERE is_active = 1"
        ).fetchall()
        # (ward, bed) → unit. Both halves normalised, because these labels are
        # typed by hand in admission, transfer and the unit register, and a
        # trailing space is not a different bed.
        by_ward_bed: dict[tuple[str, str], dict] = {}
        by_code: dict[str, dict] = {}
        by_id: dict[str, dict] = {}
        admission_bed_ids = "bed_id" in {
            r[1] for r in db.execute("PRAGMA table_info(admissions)").fetchall()}
        for u in units:
            unit = _row(u)
            by_id[unit["id"]] = unit
            by_code[_norm_key(unit["unit_code"])] = unit
            label = unit.get("bed_label") or unit.get("unit_code")
            by_ward_bed[(_norm_key(unit["ward"]), _norm_key(label))] = unit

        # One row per discharge EVENT. `discharge_summaries` is written by the
        # single discharge write path (discharge_service.discharge_patient) in the
        # same transaction that flips patients.status, so it is the discharge's
        # own record — not a status column we would have to guess the timing of.
        #
        # THE WINDOW IS FILTERED TWICE, AND THE SQL HALF IS DELIBERATELY SLOPPY.
        # `discharged_at` holds two formats in the same column: SQLite's
        # CURRENT_TIMESTAMP writes 'YYYY-MM-DD HH:MM:SS' and imports write
        # 'YYYY-MM-DDTHH:MM:SS+00:00'. A `>= ?` string comparison across those two
        # is WRONG, not merely imprecise — at position 10 'T' (0x54) sorts above
        # ' ' (0x20), so an ISO row from 10am on the cutoff date compares GREATER
        # than a space-separated cutoff of 8pm the same date and is wrongly
        # included. So SQL narrows on the DATE prefix only (one day wide, always
        # inclusive) and Python does the real comparison with `_parse_ts`.
        discharges = db.execute(
            """SELECT ds.id AS discharge_id, ds.patient_id, ds.discharged_at,
                      p.ward AS patient_ward, p.bed AS patient_bed
                 FROM discharge_summaries ds
                 JOIN patients p ON p.id = ds.patient_id
                WHERE ds.discharged_at IS NULL
                   OR substr(ds.discharged_at, 1, 10) >= ?
                ORDER BY ds.id""",
            ((cutoff.date() - timedelta(days=1)).isoformat(),),
        ).fetchall()

        # KEYED ON THE ENDED STAY, NOT ON THE PATIENT (adt-8c). Each discharge
        # is matched to the bed of the admission it ended
        # (stay_times.admission_for_summary). It used to take the patient's
        # CURRENT ward and bed, so a patient readmitted inside the lookback
        # had the old discharge mapped onto their new bed: a turnaround task
        # raised on the bed they were lying in, and that bed flipped from
        # occupied to waiting to be cleaned. The patient's own labels are used
        # only when no admission row exists for them at all (records older
        # than admissions), exactly as before.
        events: list[dict] = []
        for d in discharges:
            row = _row(d)
            ended = stay_times.admission_for_summary(db, {
                "id": row["discharge_id"], "patient_id": row["patient_id"],
                "discharged_at": row["discharged_at"]})
            events.append({"source_ref": str(row["discharge_id"]),
                           "ended_at": row.get("discharged_at"), "admission": ended,
                           "patient_ward": row.get("patient_ward"),
                           "patient_bed": row.get("patient_bed")})
        # Ends the beds and admissions module recorded with no discharge
        # summary (a death, a LAMA, an absconded closure) vacate a bed too.
        # Keyed on the end's own movement id, so each end is one task and a
        # summary-backed end (keyed on its summary id above) is never two.
        # None exist with ORB_ADT off.
        for end in stay_times.summary_less_ends(
                db, since=(cutoff - timedelta(days=1)).isoformat()):
            events.append({"source_ref": str(end["movement_id"]), "ended_at": end["ended_at"],
                           "admission": end, "patient_ward": None, "patient_bed": None})

        for event in events:
            discharged_at = _parse_ts(event.get("ended_at"))
            # An UNPARSEABLE timestamp is included, not skipped. A spurious
            # cleaning task is visible and cancellable; a discharge this module
            # silently ignored is invisible, and invisible is the failure mode
            # this whole file is written against.
            if discharged_at is not None and discharged_at < cutoff:
                continue
            scanned += 1
            source_ref = event["source_ref"]
            ended = event["admission"]
            bed_id = None
            if ended is not None and (ended.get("bed_id") or str(ended.get("bed") or "").strip()):
                ward = ended.get("ward") or ""
                bed = ended.get("bed") or ""
                bed_id = ended.get("bed_id")
            elif ended is None or (
                    event.get("patient_bed")
                    and (stay_times.latest_admission(db, ended["patient_id"]) or {}).get("id")
                    == ended.get("id")):
                # No admission row, or an ended stay that recorded no bed and
                # is still the patient's latest: the patient's own labels, as
                # this sweep always read them (they cannot name a later bed).
                ward = event.get("patient_ward") or ""
                bed = event.get("patient_bed") or ""
            else:
                # The ended stay recorded no bed and a later stay exists:
                # nothing of the ended one to clean.
                ward, bed = ended.get("ward") or "", ""

            unit = (
                (by_id.get(bed_id) if bed_id else None)
                or (by_ward_bed.get((_norm_key(ward), _norm_key(bed))) if bed else None)
                or (by_code.get(_norm_key(bed)) if bed else None)
                or (by_code.get(_norm_key(f"{ward}/{bed}")) if bed else None)
            )
            if unit is None:
                unmatched += 1
                key = str(ward or "(no ward recorded)")
                unmatched_wards[key] = unmatched_wards.get(key, 0) + 1
                continue

            # ONE clinical fact crosses the boundary and it is a boolean: did the
            # departing admission carry isolation precautions? A housekeeper sent
            # into an isolation room without knowing is an infection-control harm
            # and the PPE choice depends on it. The precaution TEXT is not copied.
            # Read from the stay that ended; for a patient with no admission
            # row, the query this sweep always used.
            if ended is not None:
                iso_text = ended.get("isolation_precautions") or ""
            else:
                iso = db.execute(
                    """SELECT isolation_precautions FROM admissions
                        WHERE patient_id = (SELECT patient_id FROM discharge_summaries WHERE id = ?)
                        ORDER BY CASE status WHEN 'discharged' THEN 0 ELSE 1 END, id DESC
                        LIMIT 1""",
                    (source_ref,),
                ).fetchone()
                iso_text = (iso["isolation_precautions"] if iso else None) or ""
            terminal = bool(str(iso_text).strip()) or bool(unit.get("is_isolation_room"))
            clean_type = "terminal" if terminal else "standard"

            vacated_iso = (discharged_at or now).isoformat()

            try:
                task_id = _insert_task(
                    db, unit=unit, request_type="discharge_turnaround",
                    priority="routine", clean_type=clean_type,
                    description="Discharge turnaround (raised automatically from the discharge record)",
                    precaution_note=(
                        "Terminal clean: this bed's last occupant was nursed with isolation "
                        "precautions, or this is a designated isolation room. Follow the "
                        "hospital's terminal-clean protocol and PPE."
                        if terminal else None
                    ),
                    source_type="discharge", source_ref=source_ref,
                    requested_by_user_id=None, requested_at=_now(),
                    # The clock starts when the PATIENT LEFT, not when the sweep
                    # noticed. A sweep that stamped its own run time would report
                    # a five-hour turnaround as five minutes.
                    vacated_at=vacated_iso,
                )
            except DBIntegrityError:
                # The unique index did its job: this discharge already has a task.
                already += 1
                continue

            created += 1
            # Vacating the unit is best-effort and MUST NOT undo the task: a bed
            # that is already dirty, being cleaned, or out of service is not moved.
            # Nor is a bed a later admission already holds by its master bed id
            # (beds and admissions module): its occupant is real, and the record
            # of the earlier departure is the task alone.
            held_now = admission_bed_ids and db.execute(
                "SELECT 1 FROM admissions WHERE bed_id = ? AND status = 'active' "
                "AND id <> ? LIMIT 1",
                (unit["id"], (ended or {}).get("admission_id") or (ended or {}).get("id") or ""),
            ).fetchone() is not None
            if unit["state"] == "occupied" and not held_now:
                try:
                    _apply_transition(
                        db, unit, "vacant_dirty",
                        actor_user_id=None, actor_role=None,
                        reason="patient discharged", task_id=task_id,
                        occurred_at=vacated_iso,
                    )
                    unit["state"] = "vacant_dirty"
                except HousekeepingError:  # pragma: no cover - occupied→dirty is legal
                    logger.debug("discharge sweep could not vacate %s", unit["unit_code"])

    return {
        "created": created,
        "already_present": already,
        "unmatched": unmatched,
        "unmatched_wards": unmatched_wards,
        "scanned": scanned,
        "lookback_hours": hours,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Turnaround reporting
# ═════════════════════════════════════════════════════════════════════════════

def ward_turnaround_report(*, ward: str | None = None, days: int = 30) -> dict:
    """Turnaround time per ward, measured vacated_at → ready_at.

    THE TWO TIMESTAMPS ARE THE WHOLE REPORT. Measuring started_at → completed_at
    instead would answer "how fast do the cleaners work", which is not the
    question: it would show 30 minutes on a bed that sat dirty for four hours
    before anyone was told, and the four hours is the thing a hospital can
    actually fix. `cleaning_minutes` is reported SEPARATELY, and the gap between
    the two medians is the queue delay.

    Terminal cleans are broken out rather than blended, because they are expected
    to take longer (`config.HOUSEKEEPING_TERMINAL_CLEAN_MINUTES`) and a ward with
    a heavy isolation load would otherwise look slow when it is not.
    """
    ensure_housekeeping_schema()
    days = max(1, min(365, int(days)))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    sql = (
        "SELECT ward, clean_type, expected_minutes, vacated_at, ready_at, "
        "started_at, completed_at FROM housekeeping_tasks "
        "WHERE status = 'done' AND ready_at IS NOT NULL AND ready_at >= ?"
    )
    params: list[Any] = [cutoff]
    if ward:
        sql += " AND ward = ?"
        params.append(ward)

    with db_session() as db:
        rows = db.execute(sql, tuple(params)).fetchall()

    buckets: dict[str, dict[str, list[float]]] = {}
    breaches: dict[str, int] = {}
    counted: dict[str, int] = {}
    unmeasurable: dict[str, int] = {}
    cleaning: dict[str, list[float]] = {}

    for r in rows:
        row = _row(r)
        w = row["ward"]
        buckets.setdefault(w, {"standard": [], "terminal": []})
        counted[w] = counted.get(w, 0) + 1
        turn = _minutes_between(row.get("vacated_at"), row.get("ready_at"))
        if turn is None:
            # A completed task whose start timestamp is missing or out of order.
            # Counted and REPORTED, never silently dropped: a report that quietly
            # excludes its awkward rows is how a median gets to look good.
            unmeasurable[w] = unmeasurable.get(w, 0) + 1
            continue
        buckets[w][row.get("clean_type") or "standard"].append(turn)
        if turn > float(row.get("expected_minutes") or 0):
            breaches[w] = breaches.get(w, 0) + 1
        clean_mins = _minutes_between(row.get("started_at"), row.get("completed_at"))
        if clean_mins is not None:
            cleaning.setdefault(w, []).append(clean_mins)

    wards = []
    for w in sorted(buckets):
        std = buckets[w]["standard"]
        term = buckets[w]["terminal"]
        allv = std + term
        wards.append({
            "ward": w,
            "completed": counted.get(w, 0),
            "measured": len(allv),
            "unmeasurable": unmeasurable.get(w, 0),
            "median_turnaround_minutes": _median(allv),
            "p90_turnaround_minutes": _percentile(allv, 90),
            "longest_turnaround_minutes": round(max(allv), 1) if allv else None,
            "median_cleaning_minutes": _median(cleaning.get(w, [])),
            "standard": {
                "count": len(std),
                "median_minutes": _median(std),
                "expected_minutes": expected_minutes_for("standard"),
            },
            "terminal": {
                "count": len(term),
                "median_minutes": _median(term),
                "expected_minutes": expected_minutes_for("terminal"),
            },
            "over_expected": breaches.get(w, 0),
        })

    return {
        "window_days": days,
        "measured_between": "vacated_at → ready_at",
        "expected_minutes": {
            "standard": expected_minutes_for("standard"),
            "terminal": expected_minutes_for("terminal"),
            "source": "site setting (ORB_HK_STANDARD_CLEAN_MINUTES / ORB_HK_TERMINAL_CLEAN_MINUTES)",
        },
        "wards": wards,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  READINESS — housekeeping's own numbers, next to the capacity screen's
# ═════════════════════════════════════════════════════════════════════════════

def _ward_capacity_map() -> dict[str, int]:
    """The capacity screen's per-ward bed count, READ ONLY.

    Imported inside the function and wrapped, because `forecast_service` pulls in
    the LoS model machinery and this module must never be the reason a facilities
    screen cannot load. An empty map means "capacity unknown", which the caller
    renders as unknown — never as zero.
    """
    try:
        from services.forecast_service import WARD_CAPACITY
        return dict(WARD_CAPACITY)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ward capacity unavailable for the readiness view: %s", exc)
        return {}


def ward_readiness(*, ward: str | None = None) -> dict:
    """Per-ward cleaning readiness, reported so that it CANNOT be mistaken for
    the capacity screen's `available`.

    Every ward row carries four things together, deliberately:

      registered_units      how many beds housekeeping actually knows about
      ward_capacity         what the capacity screen thinks the ward holds
      coverage_pct          the first as a percentage of the second
      capacity_view_available   what the capacity screen would say right now

    A hospital reading `ready = 3` without `registered = 3 of 12` would conclude
    the ward has three usable beds. It has three CLEAN REGISTERED beds and nine
    beds nobody has told this module about. The numbers are only safe together,
    so they are only ever returned together.

    `reconciliation_note` states the difference in words, because the one thing
    that is certain about a dashboard is that somebody will read the number and
    not the documentation.
    """
    ensure_housekeeping_schema()
    capacity = _ward_capacity_map()

    sql = ("SELECT ward, state, COUNT(*) AS n FROM housekeeping_units "
           "WHERE is_active = 1")
    params: list[Any] = []
    if ward:
        sql += " AND ward = ?"
        params.append(ward)
    sql += " GROUP BY ward, state"

    with db_session() as db:
        rows = db.execute(sql, tuple(params)).fetchall()
        occupied_counts = {
            r["ward"]: r["n"] for r in db.execute(
                "SELECT ward, COUNT(*) AS n FROM patients "
                "WHERE status = 'admitted' AND ward IS NOT NULL AND ward <> '' "
                "GROUP BY ward"
            ).fetchall()
        }

    per_ward: dict[str, dict[str, int]] = {}
    for r in rows:
        per_ward.setdefault(r["ward"], {s: 0 for s in UNIT_STATES})[r["state"]] = r["n"]

    # Wards the capacity screen knows about but housekeeping does not are LISTED,
    # with registered_units = 0. Omitting them would make a hospital that has
    # registered one ward look like a hospital with one ward.
    for w in capacity:
        if ward and w != ward:
            continue
        per_ward.setdefault(w, {s: 0 for s in UNIT_STATES})

    wards = []
    for w in sorted(per_ward):
        counts = per_ward[w]
        registered = sum(counts.values())
        cap = capacity.get(w)
        admitted = occupied_counts.get(w, 0)
        not_ready = counts["vacant_dirty"] + counts["cleaning_in_progress"] + counts["out_of_service"]
        # More beds registered than the capacity setting holds is a stale
        # setting (or a duplicate registration), not 125% coverage: capped at
        # 100 and said in words instead.
        over_capacity = bool(cap) and registered > cap
        wards.append({
            "ward": w,
            "registered_units": registered,
            "ward_capacity": cap,
            "coverage_pct": min(100.0, round(registered / cap * 100, 1)) if cap else None,
            "over_capacity": over_capacity,
            "coverage_note": (
                f"{registered} beds are registered here but the ward's bed count is "
                f"{cap}. Check the bed count setting, or look for a bed registered twice."
            ) if over_capacity else None,
            "occupied": counts["occupied"],
            "vacant_dirty": counts["vacant_dirty"],
            "cleaning_in_progress": counts["cleaning_in_progress"],
            "vacant_ready": counts["vacant_ready"],
            "out_of_service": counts["out_of_service"],
            "not_ready_registered": not_ready,
            # What the OTHER screen says. Repeated verbatim, computed the same
            # way forecast_service computes it, so the two can be compared
            # without anyone having to open two tabs and do arithmetic.
            "capacity_view_available": max(0, cap - admitted) if cap is not None else None,
            "capacity_view_admitted": admitted,
        })

    return {
        "wards": wards,
        "capacity_basis": (
            "The capacity and forecast screens compute available beds as the ward's "
            "bed count (a site setting) minus admitted patients. They do NOT subtract "
            "beds awaiting cleaning, and this module does not change them."
        ),
        "reconciliation_note": (
            "A bed waiting to be cleaned, being cleaned or out of service is NOT "
            "usable, but the capacity screen still counts it as available. Compare "
            "what the capacity screen says is free with the beds here that are not "
            "yet usable, and read both against how many of the ward's beds are "
            "registered here, because housekeeping can only speak for beds that "
            "have been registered here."
        ),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Linen
# ═════════════════════════════════════════════════════════════════════════════

def list_linen_items(*, include_inactive: bool = False) -> list[dict]:
    ensure_housekeeping_schema()
    sql = "SELECT code, name, is_active FROM housekeeping_linen_items"
    if not include_inactive:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY name"
    with db_session() as db:
        return [
            {**_row(r), "is_active": bool(r["is_active"])}
            for r in db.execute(sql).fetchall()
        ]


def record_linen_movement(
    *,
    ward: str,
    item_code: str,
    direction: str,
    quantity: int,
    task_id: str | None = None,
    note: str | None = None,
    actor_user_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Record linen moving between the linen room and a ward.

    FOUR DIRECTIONS, NOT TWO. "Issue" and "return" would collapse three different
    facts into one and lose the number a linen room actually argues about: clean
    linen issued to a ward comes back either SOILED (to the laundry) or
    UNUSED-CLEAN (to the store), and some is CONDEMNED. Outstanding is
    issued − soiled − clean − condemned, and it is the linen the ward is holding
    or has lost.

    Counts only. No price column, ever — services/money.py is the one money
    implementation and a linen valuation belongs to procurement's item master.
    """
    ensure_housekeeping_schema()
    ward_name = _require(ward, "ward")
    code = _require(item_code, "item_code").upper()
    dirn = _one_of(direction, LINEN_DIRECTIONS, "direction")
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        raise HousekeepingError("The quantity must be a whole number of pieces.")
    if qty <= 0:
        raise HousekeepingError("The quantity must be more than zero.")
    now = _now()

    with db_session() as db:
        known = db.execute(
            "SELECT code FROM housekeeping_linen_items WHERE code = ? AND is_active = 1", (code,)
        ).fetchone()
        if known is None:
            raise HousekeepingError(
                f"Unknown linen item {code}. Add it to the linen item list first. "
                f"Free-text item names are how one ward's 'draw sheet' stops "
                f"reconciling with another's."
            )
        try:
            db.execute(
                """INSERT INTO housekeeping_linen_movements
                       (ward, item_code, direction, quantity, task_id, note,
                        actor_user_id, moved_at, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ward_name, code, dirn, qty, task_id, _clean(note),
                 actor_user_id, now, _clean(idempotency_key, 120)),
            )
        except DBIntegrityError:
            existing = db.execute(
                "SELECT * FROM housekeeping_linen_movements WHERE idempotency_key = ?",
                (_clean(idempotency_key, 120),),
            ).fetchone()
            if existing is not None:
                return {**_row(existing), "duplicate_ignored": True}
            raise
        # `last_insert_rowid()` on THIS connection, not `ORDER BY id DESC LIMIT 1`
        # on the table: the table-wide read returns whatever row committed most
        # recently, which under two concurrent linen desks is somebody else's.
        new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        row = db.execute(
            "SELECT * FROM housekeeping_linen_movements WHERE id = ?", (new_id,)
        ).fetchone()
    return _row(row)


def linen_summary(*, ward: str | None = None, days: int = 30) -> dict:
    ensure_housekeeping_schema()
    days = max(1, min(365, int(days)))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    sql = ("SELECT ward, item_code, direction, SUM(quantity) AS qty "
           "FROM housekeeping_linen_movements WHERE moved_at >= ?")
    params: list[Any] = [cutoff]
    if ward:
        sql += " AND ward = ?"
        params.append(ward)
    sql += " GROUP BY ward, item_code, direction"

    with db_session() as db:
        rows = db.execute(sql, tuple(params)).fetchall()
        names = {
            r["code"]: r["name"]
            for r in db.execute("SELECT code, name FROM housekeeping_linen_items").fetchall()
        }

    agg: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        key = (r["ward"], r["item_code"])
        agg.setdefault(key, {d: 0 for d in LINEN_DIRECTIONS})[r["direction"]] = int(r["qty"] or 0)

    lines = []
    for (w, code) in sorted(agg):
        d = agg[(w, code)]
        outstanding = d["issue"] - d["return_soiled"] - d["return_clean"] - d["condemned"]
        lines.append({
            "ward": w,
            "item_code": code,
            "item_name": names.get(code, code),
            "issued": d["issue"],
            "returned_soiled": d["return_soiled"],
            "returned_clean": d["return_clean"],
            "condemned": d["condemned"],
            # Can legitimately go NEGATIVE — a ward returning stock it was issued
            # before this window opened. Reported as-is rather than clamped to 0,
            # because a clamp turns "your window is too short" into "your linen
            # reconciles", which is a lie a store manager would act on.
            "outstanding": outstanding,
        })

    return {
        "window_days": days,
        "definition": "outstanding = issued − returned_soiled − returned_clean − condemned",
        "lines": lines,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Overview
# ═════════════════════════════════════════════════════════════════════════════

def overview(*, sync: bool = True) -> dict:
    """Everything the housekeeping board opens on.

    Runs the discharge sweep first (best-effort). A sweep failure NEVER blanks
    the board: the board's job is to show the queue, and a facilities screen that
    refuses to render because a sweep hit a lock is worse than one that renders a
    slightly stale queue and says so.
    """
    ensure_housekeeping_schema()
    sweep: dict[str, Any] = {"skipped": True}
    if sync:
        try:
            sweep = sync_discharge_turnarounds()
        except Exception as exc:  # noqa: BLE001
            logger.warning("discharge turnaround sweep failed: %s", exc, exc_info=True)
            sweep = {"error": "sweep_failed", "detail": str(exc)}

    open_tasks = list_tasks(status="open", limit=200)
    active = list_tasks(status="in_progress", limit=200)
    readiness = ward_readiness()

    overdue = [t for t in open_tasks + active if t.get("is_overdue")]
    terminal_open = [t for t in open_tasks + active if t.get("is_terminal_clean")]
    # A bed can be dirty with nobody asked to clean it (marked dirty by hand, or
    # a request cancelled), and then it was on no queue at all. Counted, not
    # auto-raised: whether to raise a request for it is the site's call.
    with db_session() as db:
        dirty_without_request = int(db.execute(
            "SELECT COUNT(*) FROM housekeeping_units u "
            "WHERE u.is_active = 1 AND u.state = 'vacant_dirty' AND NOT EXISTS ("
            "  SELECT 1 FROM housekeeping_tasks t "
            "  WHERE t.unit_id = u.id AND t.status IN ('open', 'in_progress'))"
        ).fetchone()[0])

    return {
        "counts": {
            "open": len(open_tasks),
            "in_progress": len(active),
            "overdue": len(overdue),
            "terminal_open": len(terminal_open),
            "immediate": len([t for t in open_tasks + active if t.get("priority") == "immediate"]),
            "dirty_without_request": dirty_without_request,
        },
        "queue": (active + open_tasks)[:50],
        "readiness": readiness,
        "discharge_sweep": sweep,
        "expected_minutes": {
            "standard": expected_minutes_for("standard"),
            "terminal": expected_minutes_for("terminal"),
        },
    }
