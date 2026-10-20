"""Facility predicates for the clinical lists (multisite-8, W12-A, wave 12, 20 October 2026).

Orb keeps the facilities of one hospital group apart by row-level facility
ids (ruling MS-R1 (a)). Samuel ruled on 3 October 2026 that the isolation is
keyed on DATA: once the facilities table holds more than one row, facility
scope applies whatever ORB_MULTI_SITE says. The clinical boards and lists
(the Vigil board and discharged list, search, escalations, bundles, the nurse
board, Lens, the discharge board, Relay case rooms and the rest of the
multisite-8 sweep) ask the caller's facilities from the one resolver,
permissions.facility_scope (services.facility_service.facility_scope_for),
and add the predicate built here to the statement itself, so a LIMIT can
never hide an in-scope row behind an out-of-scope one.

ONE FACILITY ON RECORD (every install today). facility_scope answers None
before reading anything, every clause below is the empty string with no
parameters, and each lane runs exactly the statement it always ran.

The scope values follow permissions.scope_patient_ids_sql:

  * None means no facility limit (one facility on record, a group-wide
    account by enterprise grant, a service account or a patient login);
  * the empty set means no patient at all (a department the master does not
    list), and the clause is a predicate that is never true;
  * otherwise the rows are kept to those facility ids.

Two shapes, because a list reads a facility in one of two places:

  * column_clause(column, facilities): a row that carries its own facility,
    such as the active admission a board joins (admissions.facility_id), a
    surgical case (surgeries.facility_id) or a booking
    (appointments.facility_id);
  * patient_clause(facilities): a patient row aliased ``p`` whose facility is
    the facility of the patient's latest encounter
    (permissions.FACILITY_PATIENT_SQL, the rule ruling MS-R5 (a) states and
    require_patient_access applies to one chart), for a list that is not
    joined to an active admission (the discharged list).

Allergies, active medications and problems are never filtered by facility
here: once a patient is in scope the whole chart is (ruling MS-R2 (a)).
"""

from __future__ import annotations

from collections.abc import Iterable

from permissions import FACILITY_PATIENT_SQL

__all__ = [
    "NEVER",
    "column_clause",
    "facility_ids",
    "in_scope",
    "patient_clause",
]

#: The predicate an empty scope adds: true for no row.
NEVER = " AND 0"


def facility_ids(facilities: Iterable[int]) -> tuple[int, ...]:
    """The scope's ids as a sorted tuple of ints, for SQL parameters."""
    return tuple(sorted(int(f) for f in facilities))


def _marks(ids: tuple[int, ...]) -> str:
    return ", ".join("?" for _ in ids)


def column_clause(column: str, facilities: Iterable[int] | None) -> tuple[str, tuple[int, ...]]:
    """(' AND <column> IN (?, ...)', ids) keeping rows to the facilities.

    ('', ()) for None (no facility limit), so the statement is unchanged;
    (NEVER, ()) for the empty set. `column` is code, never request text."""
    if facilities is None:
        return "", ()
    ids = facility_ids(facilities)
    if not ids:
        return NEVER, ()
    return f" AND {column} IN ({_marks(ids)})", ids


def patient_clause(facilities: Iterable[int] | None) -> tuple[str, tuple[int, ...]]:
    """(' AND <the latest encounter's facility> IN (?, ...)', ids) for a
    statement whose patients row is aliased ``p``.

    The facility is permissions.FACILITY_PATIENT_SQL, so a list keeps exactly
    the patients require_patient_access would let the caller open. ('', ())
    for None; (NEVER, ()) for the empty set."""
    if facilities is None:
        return "", ()
    ids = facility_ids(facilities)
    if not ids:
        return NEVER, ()
    return f" AND {FACILITY_PATIENT_SQL} IN ({_marks(ids)})", ids


def in_scope(facility_id: int | None, facilities: Iterable[int] | None) -> bool:
    """Is a row at `facility_id` inside the scope? Always True for None (no
    facility limit); never for a row whose facility is unknown (None) while
    a limit applies."""
    if facilities is None:
        return True
    if facility_id is None:
        return False
    return int(facility_id) in set(facility_ids(facilities))
