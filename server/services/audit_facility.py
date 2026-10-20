"""Orb: which facility an audit row belongs to, and which rows each reader sees
(multisite-11, W12-C, 20 October 2026).

Samuel ruled on 3 October 2026 (multi-site M6, audit (a)) that the audit trail
stays one hash chain for the whole appliance and that a row's facility goes
into its hashed metadata_json, never into a new column. audit_service's
_HASH_FIELDS do not change, so the chain is not bumped and the int-versus-TEXT
target id trap of 4 October cannot come back.

WRITING (audit_service.log_audit_event calls facility_metadata)
  With one facility on record nothing is read and the metadata is stored
  exactly as the caller gave it, byte for byte. With more than one, the row's
  metadata carries facility_id, settled in this order:
    1. the facility_id the caller passed to log_audit_event;
    2. a facility_id the caller already put in the metadata (a whole number);
    3. the patient's facility (permissions.FACILITY_PATIENT_SQL, the facility
       of their latest encounter, the rule the scope helpers use), cached for
       CACHE_SECONDS;
    4. the acting account's facility (facility_service.user_facility_id), or
       for a sign-in that has no actor yet the account the sign-in named
       (metadata attempted_id), cached for CACHE_SECONDS;
    5. none: the row carries facility_unresolved: true instead.
  A lookup that fails marks the row facility_unresolved and the row is still
  written: a fail-closed category never loses its row to attribution. A
  facility_id the caller put in the metadata that is not a whole number is
  kept as it is and the row is marked facility_unresolved as well.

READING (the epoch rule)
  * A row whose metadata names a facility belongs to that facility.
  * A row that names none, with an id at or below the audit epoch, belongs to
    facility 1. The epoch is the highest audit id at the moment the first
    facility other than 1 was added (facilities.audit_epoch_id, the smallest
    one recorded), so facility 1's administrators keep the whole history from
    before the expansion.
  * A later row that names none (facility_unresolved) is shown only to an
    account with no facility limit: a group-wide administrator.
  A facility other than 1 with no recorded epoch (added outside the product,
  or while the audit table was empty) counts as epoch 0: no unattributed row
  is then given to facility 1 (the strict reading, for Samuel to confirm).

  visible_sql() is that rule as one SQL condition over FACILITY_SQL, the exact
  expression idx_audit_facility indexes (scripts/build_audit_facility_index.py
  builds it in a maintenance window; the facility create gate requires it), so
  SQLite can use the index. row_visible() and row_facility() are the same rule
  for one row already read.

Every reader of audit_logs is classed in tests/facility_sql_sites/audit.py:
audit_routes and the flight recorder filter rows; the admin overview, the
staff list's sign-in columns, the compliance report's coverage and the impact
model's audit count filter their aggregates; the chain verify, the metrics
endpoint and the outbound egress record stay appliance-level by design; the
patient's own access history (consent_service) stays group-wide, because it
is the data principal's right to see every access to their record.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger("orb.audit")

#: The metadata key that names a row's facility, and the one that says it
#: could not be told.
FACILITY_KEY = "facility_id"
UNRESOLVED_KEY = "facility_unresolved"

#: The metadata keys an oversized metadata stub keeps (audit_service
#: bound_audit_fields), so a truncated row still says where it belongs.
STUB_KEYS = (FACILITY_KEY, UNRESOLVED_KEY)

#: The expression idx_audit_facility indexes. Readers use exactly this text.
FACILITY_SQL = "json_extract(metadata_json, '$.facility_id')"

#: The index the facility create gate asks for (facility_identity names it
#: too), and the statement the maintenance command runs.
INDEX_NAME = "idx_audit_facility"
INDEX_SQL = f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON audit_logs({FACILITY_SQL}, timestamp)"

#: Facility 1, whose history from before the expansion is its own.
MAIN_FACILITY_ID = 1

#: How long a patient's or an account's facility is reused before it is read
#: again, and how many of each are kept.
CACHE_SECONDS = 30.0
CACHE_LIMIT = 4096

#: Patched by tests to move time forward without sleeping.
_clock = time.monotonic

_lock = threading.Lock()
_patients: OrderedDict[str, tuple[int, float]] = OrderedDict()
_actors: OrderedDict[str, tuple[int | None, float]] = OrderedDict()


class FacilityFilterError(LookupError):
    """A ?facility= the reader may not narrow to (not on record, or outside
    their facilities). Routes answer it as 404, never 403."""


def invalidate_cache() -> None:
    """Forget every cached patient and account facility."""
    with _lock:
        _patients.clear()
        _actors.clear()


def _cache_get(cache: OrderedDict, key: str) -> tuple[bool, Any]:
    with _lock:
        hit = cache.get(key)
        if hit is None:
            return False, None
        value, at = hit
        if _clock() - at >= CACHE_SECONDS:
            del cache[key]
            return False, None
        cache.move_to_end(key)
        return True, value


def _cache_put(cache: OrderedDict, key: str, value: Any) -> None:
    with _lock:
        cache[key] = (value, _clock())
        cache.move_to_end(key)
        while len(cache) > CACHE_LIMIT:
            cache.popitem(last=False)


def _whole_number(value: Any) -> int | None:
    """A facility id given as an int (or digits), else None. A bool is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


# ═════════════════════════════════════════════════════════════════════════════
#  Writing: the facility a new row belongs to
# ═════════════════════════════════════════════════════════════════════════════


def patient_facility(db: Any, patient_id: str | None) -> int | None:
    """The facility of the patient's latest encounter, or None for an id that
    matches no patient. Cached for CACHE_SECONDS; a miss is not cached, so a
    record created a moment later is found."""
    if not patient_id:
        return None
    hit, value = _cache_get(_patients, patient_id)
    if hit:
        return value
    from permissions import FACILITY_PATIENT_SQL

    row = db.execute(
        f"SELECT {FACILITY_PATIENT_SQL} AS fid FROM patients p WHERE p.id = ?",
        (patient_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    fid = int(row[0])
    _cache_put(_patients, patient_id, fid)
    return fid


def actor_facility(db: Any, user_id: str | None) -> int | None:
    """The facility an account works at (facility_service.user_facility_id),
    or None for no account, a system account, or an account whose department
    no facility lists. Cached for CACHE_SECONDS, misses included."""
    if not user_id:
        return None
    from database import SERVICE_ACCOUNT_IDS

    if user_id in SERVICE_ACCOUNT_IDS:
        return None
    hit, value = _cache_get(_actors, user_id)
    if hit:
        return value
    from services import facility_service

    row = db.execute("SELECT id, role, department FROM users WHERE id = ?", (user_id,)).fetchone()
    fid: int | None = None
    if row is not None:
        account = {"id": row[0], "role": row[1], "department": row[2]}
        fid = facility_service.user_facility_id(account, db=db)
    _cache_put(_actors, user_id, fid)
    return fid


def _resolve(
    db: Any, meta: Mapping[str, Any], patient_id: str | None, actor_user_id: str | None
) -> int | None:
    fid = patient_facility(db, patient_id)
    if fid is not None:
        return fid
    actor = actor_user_id
    if not actor:
        attempted = meta.get("attempted_id")
        actor = attempted[:200] if isinstance(attempted, str) else None
    return actor_facility(db, actor)


def facility_metadata(
    db: Any,
    metadata: dict[str, Any] | None,
    *,
    facility_id: Any = None,
    patient_id: Any = None,
    actor_user_id: Any = None,
) -> dict[str, Any] | None:
    """The metadata an audit row stores. With one facility on record it is
    `metadata` itself, untouched and with nothing read beyond the cached
    facility count. Otherwise a copy carrying facility_id, or
    facility_unresolved: true when no facility could be told (see the module
    docstring for the order). Never raises."""
    from services import facility_service

    try:
        multi = facility_service.multi_facility_data(db=db)
    except Exception as e:  # multi_facility_data keeps its last answer itself
        logger.warning("Audit facility: the facilities count could not be read: %s", e)
        multi = False
    if not multi:
        return metadata
    out: dict[str, Any] = dict(metadata) if metadata else {}
    explicit = _whole_number(facility_id)
    if explicit is not None:
        out[FACILITY_KEY] = explicit
        return out
    if FACILITY_KEY in out:
        named = out[FACILITY_KEY]
        if not isinstance(named, int) or isinstance(named, bool):
            out[UNRESOLVED_KEY] = True
        return out
    try:
        fid = _resolve(
            db,
            out,
            str(patient_id) if patient_id else None,
            str(actor_user_id) if actor_user_id else None,
        )
    except Exception as e:
        logger.warning("Audit facility could not be resolved; the row is marked unresolved: %s", e)
        fid = None
    if fid is None:
        out[UNRESOLVED_KEY] = True
    else:
        out[FACILITY_KEY] = fid
    return out


def stub_facility_keys(metadata_json: str) -> dict[str, Any]:
    """The facility keys of an oversized metadata blob, for the truncation
    stub: facility_id when it is a whole number or null, facility_unresolved
    when it is true or false. Anything else, or a blob that does not parse,
    gives nothing."""
    try:
        parsed = json.loads(metadata_json)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    keep: dict[str, Any] = {}
    if FACILITY_KEY in parsed:
        value = parsed[FACILITY_KEY]
        if value is None or (
            isinstance(value, int) and not isinstance(value, bool) and abs(value) < 2**63
        ):
            keep[FACILITY_KEY] = value
    if isinstance(parsed.get(UNRESOLVED_KEY), bool):
        keep[UNRESOLVED_KEY] = parsed[UNRESOLVED_KEY]
    return keep


# ═════════════════════════════════════════════════════════════════════════════
#  Reading: which rows a reader sees
# ═════════════════════════════════════════════════════════════════════════════


def reader_scope(user: Any, *, db: Any = None) -> frozenset[int] | None:
    """The facilities whose audit rows `user` may read, or None for every row
    (one facility on record, or a group-wide administrator). The same answer
    as the patient scope (facility_service.facility_scope_for). BLOCKING on a
    cache miss: plain-def code and run_db only."""
    from services import facility_service

    return facility_service.facility_scope_for(user, db=db)


def audit_epoch(db: Any) -> int:
    """The highest audit id from before the first facility other than 1 was
    added: the smallest facilities.audit_epoch_id, a missing one counting as
    0. 0 when no other facility is on record."""
    row = db.execute(
        "SELECT MIN(COALESCE(audit_epoch_id, 0)) FROM facilities WHERE id != ?",
        (MAIN_FACILITY_ID,),
    ).fetchone()
    if row is None or row[0] is None:
        return 0
    return max(0, int(row[0]))


def visible_sql(scope: Iterable[int] | None, epoch: int) -> tuple[str, tuple[int, ...]]:
    """The epoch rule as one condition on audit_logs (unaliased), with its
    parameters. ('', ()) for scope None: no condition at all. An empty scope
    sees no row."""
    if scope is None:
        return "", ()
    ids = tuple(sorted({int(f) for f in scope}))
    if not ids:
        return "0", ()
    marks = ", ".join("?" for _ in ids)
    clause = f"{FACILITY_SQL} IN ({marks})"
    if MAIN_FACILITY_ID in ids and epoch > 0:
        return f"({clause} OR ({FACILITY_SQL} IS NULL AND id <= ?))", (*ids, epoch)
    return clause, ids


def _stored_value(metadata_json: Any) -> tuple[bool, Any]:
    """(parsed, the facility_id as SQLite's json_extract compares it). Not
    parsed: the row is treated as naming something no reader's scope holds."""
    if metadata_json is None or metadata_json == "":
        return True, None
    try:
        parsed = json.loads(metadata_json)
    except Exception:
        return False, None
    if not isinstance(parsed, dict):
        return True, None
    value = parsed.get(FACILITY_KEY)
    # json_extract gives 1 and 0 for true and false, and compares a whole
    # float equal to the int; mirror it so a row reads the same either way.
    if isinstance(value, bool):
        return True, int(value)
    if isinstance(value, float) and value.is_integer():
        return True, int(value)
    return True, value


def row_facility(row: Mapping[str, Any], epoch: int) -> int | None:
    """The facility one stored row belongs to under the epoch rule, or None
    (attributed to no facility a reader holds)."""
    parsed, value = _stored_value(row.get("metadata_json"))
    if not parsed:
        return None
    if value is None:
        rid = row.get("id")
        if epoch > 0 and rid is not None and int(rid) <= epoch:
            return MAIN_FACILITY_ID
        return None
    return value if isinstance(value, int) else None


def row_visible(row: Mapping[str, Any], scope: Iterable[int] | None, epoch: int) -> bool:
    """Whether a reader with `scope` may see one stored row (visible_sql for
    a row already read)."""
    if scope is None:
        return True
    fid = row_facility(row, epoch)
    return fid is not None and fid in {int(f) for f in scope}


def narrowed_scope(
    user: Any, facility: int | None, *, db: Any
) -> tuple[frozenset[int] | None, bool]:
    """(the scope to read with, whether more than one facility is on record)
    for a reader who may name a ?facility= to narrow to. Naming one only ever
    narrows: a facility not on record, or outside the reader's facilities,
    raises FacilityFilterError (answered as 404). With one facility on record,
    naming facility 1 changes nothing."""
    from services import facility_service

    multi = bool(facility_service.multi_facility_data(db=db))
    scope = reader_scope(user, db=db)
    if facility is None:
        return scope, multi
    fid = int(facility)
    if not multi:
        if fid == MAIN_FACILITY_ID:
            return None, False
        raise FacilityFilterError(fid)
    known = {int(f["id"]) for f in facility_service.list_facilities(db=db)}
    if fid not in known or (scope is not None and fid not in scope):
        raise FacilityFilterError(fid)
    return frozenset({fid}), True


def facility_options(scope: Iterable[int] | None, *, db: Any) -> list[dict[str, Any]]:
    """The facilities a reader can filter by, {id, code, name}, by id: every
    facility on record for a reader with no facility limit, else theirs."""
    from services import facility_service

    ids = sorted(
        {int(f["id"]) for f in facility_service.list_facilities(db=db)}
        if scope is None
        else {int(f) for f in scope}
    )
    out = []
    for fid in ids:
        label = facility_service.facility_identity(fid, db=db)
        if label is not None:
            out.append(label)
    return out


def attach_facilities(
    db: Any, rows: list[dict[str, Any]], epoch: int, key: str = "facility"
) -> list[dict[str, Any]]:
    """Give each row dict `key`: {id, code, name} of the facility it belongs to
    under the epoch rule, or None for a row attributed to none. In place."""
    from services import facility_service

    for row in rows:
        fid = row_facility(row, epoch)
        row[key] = None if fid is None else facility_service.facility_identity(fid, db=db)
    return rows
