"""Orb: live bed board updates (adt-19, HIS wave 9, W9-A). ORB_ADT only.

After an ADT write COMMITS, the route that made it calls notify_adt_changed,
and every bed board that may show the ward hears one content-free
'adt:changed' event per ward touched: {"ward_id": <the ward's id>} and nothing
else, never a patient id, name, bed or department. The board answers by
re-reading GET /api/adt/board, which applies the caller's own scope and audits
the read as it always did; the board's 30 second poll stays as the fallback
when the socket is down (src/hooks/useAdtBoardLive.ts).

WHO HEARS IT (ws/realtime.py): every adt.read holder joins 'adt:<their
department>' (department_scope, never a value the client names), or
'adt:__all__' with no department, while the module is on. One write emits to
'adt:__all__' and to the room of every department involved: each ward's own
department and the patient's treating department before and after the move.
A ward with no id (an admission awaiting a bed, nowhere yet) is said as
{"ward_id": null}.

Called from the route layer only, after the service call returned (so after
its transaction committed), and never for a refused write or a replay. It
never raises and never fails or slows the write: the push is scheduled on the
event loop and the route answers at once (ws/realtime.schedule_on_loop).

Lanes that do not call it yet (the 30 second poll covers them): bed
reservations, bed setup, the doctor's discharge (vigil_routes), the front
desk's transfer, the Relay transfer lane, roster import and housekeeping's
cleaning states.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from database import db_session

logger = logging.getLogger("orb.adt.live")

__all__ = ["notify_adt_changed", "wards_and_departments"]

#: Keys of an ADT answer that name a ward, anywhere in it (the answer, its
#: movement, a swap's legs).
_WARD_KEYS = frozenset({"ward_id", "from_ward_id", "to_ward_id"})
#: Keys that name a treating department.
_DEPARTMENT_KEYS = frozenset({"from_department", "to_department", "current_department"})
_MAX_DEPTH = 6


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _walk(value: Any, wards: set, departments: set, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _WARD_KEYS:
                ward = _clean(item)
                if ward:
                    wards.add(ward)
            elif key in _DEPARTMENT_KEYS:
                dept = _clean(item)
                if dept:
                    departments.add(dept)
            elif isinstance(item, (dict, list, tuple)):
                _walk(item, wards, departments, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _walk(item, wards, departments, depth + 1)


def wards_and_departments(
    answer: Any = None,
    *,
    admission_ids: Iterable[Any] = (),
    ward_ids: Iterable[Any] = (),
    departments: Iterable[Any] = (),
) -> tuple[set, set]:
    """The ward ids a write touched and the departments whose boards may show
    them: from the answer's ward and department keys, the admissions named
    (where each lies now and who treats it) and each ward's own department.
    Reads only."""
    wards = {w for w in (_clean(x) for x in ward_ids) if w}
    depts = {d for d in (_clean(x) for x in departments) if d}
    _walk(answer, wards, depts)
    ids = sorted({a for a in (_clean(x) for x in admission_ids) if a})
    if not ids and not wards:
        return wards, depts
    with db_session() as db:
        if ids:
            marks = ",".join("?" for _ in ids)
            for row in db.execute(
                f"SELECT ward_id, department FROM admissions WHERE id IN ({marks})", ids
            ).fetchall():
                if _clean(row["ward_id"]):
                    wards.add(row["ward_id"].strip())
                if _clean(row["department"]):
                    depts.add(row["department"].strip())
        if wards:
            marks = ",".join("?" for _ in wards)
            for row in db.execute(
                f"SELECT department FROM wards WHERE id IN ({marks})", sorted(wards)
            ).fetchall():
                if _clean(row["department"]):
                    depts.add(row["department"].strip())
    return wards, depts


def notify_adt_changed(
    *,
    answer: Any = None,
    admission_ids: Iterable[Any] = (),
    ward_ids: Iterable[Any] = (),
    departments: Iterable[Any] = (),
) -> bool:
    """Tell the bed boards an ADT write committed. Call it AFTER the commit,
    never for a refusal or a replay. Does nothing while ORB_ADT is not 'on'.
    Never raises. True when a push was scheduled."""
    try:
        from services.adt.core import adt_on

        if not adt_on():
            return False
        wards, depts = wards_and_departments(
            answer, admission_ids=admission_ids, ward_ids=ward_ids, departments=departments
        )
        from ws import realtime

        return realtime.schedule_adt_changed(sorted(wards), sorted(depts))
    except Exception as exc:  # noqa: BLE001 - a nudge must never fail a write
        logger.warning("adt:changed not pushed: %s", exc)
        return False
