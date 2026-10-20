"""Migration 76: the WHO Surgical Safety Checklist gets its own column.

surgeries.surgical_checklist had two writers that disagreed about its shape:

  * the AI prep package (sage_handoff_service._persist_package and
    surgery_escalation_service._finalize_surgery) writes a LIST of prep steps;
  * the WHO checklist (services/who_checklist.set_item) wrote an OBJECT,
    {template, state}, and carried a prep list it found there under 'legacy'.

GET /api/surgery/{id} returned whichever was stored, so the Surgical Suite's
prep-step list crashed (`.map` on an object) as soon as anyone ticked a WHO
item. The other way round lost data: a prep package written after a tick (an
emergency case can be signed in while its package is still generating) replaced
the OR's safety record with a bare list.

1. ADD COLUMN surgeries.who_checklist TEXT. NULL means nothing has been ticked.
2. Every row whose surgical_checklist holds a WHO payload is split:
   who_checklist gets {template, state}; surgical_checklist gets back the prep
   list from 'legacy', or NULL when there was none. A who_checklist that is
   already set is never overwritten.

Idempotent: once split, surgical_checklist no longer holds a WHO payload, so a
second run moves nothing. Plain ADD COLUMN, no rebuild, no foreign key.

The template id is written out here rather than imported from
services/who_checklist: a migration must keep doing exactly what it did when it
shipped, whatever the service later calls its template.
"""

from __future__ import annotations

import json

from migrations import Migration

_WHO_TEMPLATE = "who-ssc-2009"


def _columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(surgeries)").fetchall()}


def apply(conn) -> None:
    existing = _columns(conn)
    if not existing:
        # surgeries is a baseline table that init_db creates before any
        # migration runs; a database without it has nothing to split.
        return
    if "who_checklist" not in existing:
        conn.execute("ALTER TABLE surgeries ADD COLUMN who_checklist TEXT")

    rows = conn.execute(
        "SELECT id, surgical_checklist FROM surgeries "
        "WHERE surgical_checklist LIKE '%' || ? || '%'",
        (_WHO_TEMPLATE,),
    ).fetchall()
    for row in rows:
        surgery_id, raw = row[0], row[1]
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not (isinstance(payload, dict) and payload.get("template") == _WHO_TEMPLATE):
            continue
        state = payload.get("state")
        who = {"template": _WHO_TEMPLATE, "state": state if isinstance(state, dict) else {}}
        legacy = payload.get("legacy")
        prep = json.dumps(legacy) if legacy is not None else None
        conn.execute(
            "UPDATE surgeries SET who_checklist = COALESCE(who_checklist, ?), "
            "surgical_checklist = ? WHERE id = ?",
            (json.dumps(who), prep, surgery_id),
        )


MIGRATION = Migration(76, "surgery_who_checklist_column", apply)
