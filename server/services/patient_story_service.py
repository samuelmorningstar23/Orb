"""Orb — Unified Patient Story.

One scrollable clinical narrative per patient, merging every recorded event into
a single chronological feed with a stable `type` on each entry so the UI can
filter. This is the read model behind the patient timeline; it composes existing
tables and invents nothing.

── 2026-08-08: THIS FILE IS NOW AN ADAPTER, AND WHY ─────────────────────────
It used to assemble the feed itself and finish with

    events.sort(key=lambda e: e["timestamp"] or "", reverse=True)

— a LEXICAL sort over timestamps that this database stores in two different
dialects. ``orders.created_at`` and ``medications.started_at`` take SQLite's
``CURRENT_TIMESTAMP`` default (``"2026-08-08 01:15:00"`` — space separator)
while ``risk_scores.timestamp`` and anything the application writes carry an ISO
string (``"2026-08-08T01:15:00.123456+00:00"``). Sorting those as text is wrong
twice:

  * ``' '`` (0x20) sorts before ``'T'`` (0x54), so EVERY bare-dialect row sorts
    below EVERY ISO row regardless of when it happened. On a real chart that
    silently files every order and every drug start underneath every alert and
    note, whatever the clock said.
  * ``'Z'`` (0x5A) sorts after ``'.'`` (0x2E), so a whole-second stamp compares
    as later than a sub-second one inside the same second.

A timeline that reorders a chart is worse than no timeline — it is the one
surface a clinician reads specifically to establish sequence ("was the
antibiotic given before or after the lactate?"). So the assembly now lives in
``services.patient_timeline_service``, which normalises both dialects to an
aware UTC datetime before it sorts anything, and this module maps that engine's
output onto the story contract the UI and the ward round already consume.

Nothing was removed. The contract is unchanged — ``{timestamp, type, title,
detail, actor}``, newest first, optional ``types`` filter — and every type this
module used to emit it still emits. Three things got better on the way:

  * five streams that were simply missing are now in the narrative: lab/vitals
    OBSERVATIONS (the HL7 ingestion stream), care BUNDLES, the PROBLEM list,
    ALLERGIES, and ORDER STATUS TRANSITIONS;
  * ``actor`` is the clinician's NAME rather than their raw user id;
  * events whose timestamp is unreadable are dropped instead of being sorted to
    an arbitrary end of the chart.
"""

from __future__ import annotations

import logging

from services.patient_timeline_service import KINDS, build_chart_timeline

logger = logging.getLogger("orb.patient_story")

# The event types the feed can emit — also the filter vocabulary for the UI.
#
# 'vitals' has never been emitted by this feed (continuous monitor traces belong
# on the vitals chart, not in a narrative; the clinically meaningful ones surface
# as 'alert'). It is kept in the vocabulary because it has always been in it and
# a client may filter on it; asking for it simply returns nothing.
STORY_TYPES = ["vitals", *KINDS]


def get_patient_story(patient_id: str, types: list[str] | None = None) -> list[dict]:
    """Return the merged clinical narrative for a patient, newest first.

    `types`, when given, restricts the feed to those event types (any unknown
    type is ignored). Each event is EXACTLY ``{timestamp, type, title, detail,
    actor}`` — the shape this function has always returned, and the shape
    tests/test_patient_story.py pins. The engine carries more per event
    (severity, badge, a stable id); it is dropped here on purpose rather than
    widened into a contract three surfaces already depend on. Anything that
    wants the richer row reads /api/chart/patients/{id}/timeline.

    NOTE the deliberate asymmetry with build_chart_timeline: no ``user_id`` is
    passed, so no review watermark is consulted. This function feeds the ward
    round's overnight window, which must not change shape depending on whose
    session it is; the "since you last looked" layer is a sibling key on the
    route envelope instead.
    """
    want = set(types) if types else None
    # No limit: the story is the whole admission, and the ward round filters it
    # to a window of its own. `limit` is capped at 500 by the engine.
    result = build_chart_timeline(patient_id, user_id=None, limit=500)

    events = []
    for e in result["events"]:
        if want is not None and e["kind"] not in want:
            continue
        events.append(
            {
                "timestamp": e["timestamp"],
                "type": e["kind"],
                "title": e["title"],
                "detail": e["detail"],
                "actor": e["actor"],
            }
        )
    return events
