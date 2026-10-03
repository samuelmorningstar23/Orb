"""THE CHART MUST NOT BE ABLE TO START A GENERATION IT WILL NEVER FINISH.

WHAT WAS ON SCREEN (2026-08-14, real browser, running ward). Under the
deterministic drivers on EVERY patient chart:

    ✦ MedGemma AI Assessment — Generating…

It never resolved. Held at "Generating…" for a full 150-second dedicated test
and for 6+ minutes in the live pane. A spinner is a promise, and this one made
it to a clinician standing at a deteriorating bedside — the exact "something is
still coming" implication the honest degraded-mode copy elsewhere in Orb is
careful never to make.

WHAT IT WAS DOING WHILE IT SPUN. `GET /api/vigil/patients/{id}/analysis` opened
a 512-token 27B stream on every chart open, with its five-minute cache commented
out ("Disable cache temporarily for debugging"), and then queued for the SAME
background LLM slot as Sage's alert advice, at the same priority, waiting 120 s
for it. So it was not only the permanent shimmer: it was a major cause of the
22 % delivery rate measured on the advice tier it was competing with. One chart
open per clinician per patient, each one taking the slot the alert path needs.

WHY REMOVAL RATHER THAN A CACHE AND A DEADLINE. The panel had no content of its
own worth rescuing. It duplicated the full four-section assessment that was
deliberately relocated to the handover and the ward round
(tests/test_assessment_relocation.py), on the one surface where Tier 1 —
deterministic, no model, 33-240 ms, present on 100 % of alerts — already answers
"why is this score what it is, and which way is it moving".

THE INVARIANT, and it is about the ROUTE TABLE rather than about one response
code: nothing mounted on this appliance serves a per-patient analysis stream.
Asserted against the assembled app, not against `vigil_routes` alone, so
reintroducing the endpoint on any other router fails here too.

PROVEN ABLE TO FAIL: restoring the route makes both tests below fail (verified
by re-adding it and running this file).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from routes import vigil_routes  # noqa: E402

client = TestClient(app)


def _paths(router_or_app) -> list[str]:
    return [getattr(r, "path", "") for r in router_or_app.routes]


def test_no_per_patient_analysis_stream_is_mounted_anywhere():
    """No route on the whole app ends in a per-patient `/analysis` stream."""
    offenders = [p for p in _paths(app) if p.endswith("/analysis")]
    assert offenders == [], (
        "A per-patient analysis stream is mounted again: "
        f"{offenders}. That endpoint put a 27B generation on the chart-open "
        "path and took the alert lane's LLM slot; the chart's deterministic "
        "Tier 1 block is what answers this question now."
    )


def test_vigil_router_serves_no_streaming_debug_leftovers():
    """The two debug streams this router carried are both gone.

    `/test-stream` is included because it is the same defect wearing different
    clothes: a permission-gated route that streamed a hardcoded sentence about a
    named seeded patient ("Suresh Pillai is a 55-year-old male with acute chest
    pain") as though it were clinical content, with no caller anywhere in the
    frontend.
    """
    paths = _paths(vigil_routes.router)
    assert not [p for p in paths if p.endswith("/analysis")]
    assert not [p for p in paths if p.endswith("/test-stream")]


@pytest.mark.parametrize("path", [
    "/api/vigil/patients/P001/analysis",
    "/api/vigil/test-stream",
])
def test_the_removed_paths_are_not_served(path):
    """Unauthenticated is enough: a 404 proves nothing is mounted there.

    A route that still existed would answer 401/403 (every route on this router
    is permission-gated), so the distinction this asserts is real — it is not
    just "the request failed".
    """
    res = client.get(path)
    assert res.status_code == 404, (
        f"{path} answered {res.status_code}; a mounted-but-gated route answers "
        "401/403, so this path is being served again."
    )
