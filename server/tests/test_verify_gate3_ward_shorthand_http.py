"""Adversarial verification of the Gate 3 ward-shorthand fix, over HTTP.

tests/test_zz_prefix_postfix_proof.py asserts the unit fact
(`not _gate_content("K 6.8")`). That is necessary but not sufficient: it proves
a boolean flipped, not that a nurse typing five characters into a case room ends
up with a standing alarm on the sidebar.

This file closes that gap. It logs a real ward NURSE in through /api/auth/login,
posts through the real POST /api/relay/channels/{id}/messages route (which is
what wires `sage_ambient_observe` in as a BackgroundTask,
routes/relay_routes.py:437-438), and then asserts on `sage_room_memory` — the
row the sidebar renders from.

Reverting Gate 3 to its pre-fix body (`len(stripped) < 8 -> return True`) makes
`test_terse_critical_potassium_from_a_nurse_charts_a_standing_alarm` fail with
"no sage_room_memory row at all"; shipped code passes it. The three
ward-chatter/normal-value tests pass in BOTH directions on purpose — they are
the over-tightening guard, not the proof.

No LLM is involved: "K 6.8" does not match Gate 5's signal patterns, so the
card comes from the deterministic critical-value fast-path inside
`_update_memory`, with zero model calls.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from database import db_session
from auth import hash_password

client = TestClient(app)

NURSE_ID = "N-GATE3-VERIFY"
RENDER_THRESHOLD = 0.80  # SageAmbientSidebar.tsx:92 CONFIDENCE_THRESHOLD_RISK


@pytest.fixture()
def ward(monkeypatch):
    """An ordinary ward nurse, an admitted patient, and their case room."""
    suffix = uuid.uuid4().hex[:6].upper()
    patient_id = f"PAT-G3-{suffix}"
    channel_id = f"case-{patient_id}"

    with db_session() as db:
        db.execute(
            """INSERT OR IGNORE INTO users (id, name, role, department, password_hash)
               VALUES (?, 'Nurse Priya Reddy', 'nurse', 'General Medicine', ?)""",
            (NURSE_ID, hash_password("testpass123")),
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, status) VALUES (?, 'Gate3 Patient', ?, 'admitted')",
            (patient_id, f"MRN-G3-{suffix}"),
        )
        db.execute(
            "INSERT INTO channels (id, name, type, patient_id) "
            "VALUES (?, 'Gate3 Case Room', 'case_room', ?)",
            (channel_id, patient_id),
        )

    resp = client.post("/api/auth/login",
                       json={"employee_id": NURSE_ID, "password": "testpass123"})
    assert resp.status_code == 200, f"nurse login failed: {resp.text}"

    # Record instead of raise: `sage_ambient_observe` swallows every exception by
    # design (its outer catch), so a raising stub would be invisible rather than
    # loud. Returning None also reproduces the measured 4B failure — the run in
    # which the classifier volunteered nothing on a critical potassium.
    import services.sage_ambient_service as sam
    llm_calls = []

    async def _no_llm(prompt, system):
        llm_calls.append(prompt)
        return None

    monkeypatch.setattr(sam, "_classify_with_llm", _no_llm)

    yield {"patient_id": patient_id, "channel_id": channel_id,
           "llm_calls": llm_calls,
           "headers": {"Authorization": f"Bearer {resp.json()['token']}"}}

    with db_session() as db:
        db.execute("DELETE FROM sage_room_memory WHERE room_id = ?", (channel_id,))


def _post(ward, content):
    r = client.post(f"/api/relay/channels/{ward['channel_id']}/messages",
                    headers=ward["headers"],
                    json={"content": content, "message_type": "text"})
    assert r.status_code == 200, r.text
    return r


def _memory(channel_id):
    with db_session() as db:
        row = db.execute("SELECT * FROM sage_room_memory WHERE room_id = ?",
                         (channel_id,)).fetchone()
    return dict(row) if row else None


def _live_flags(channel_id):
    row = _memory(channel_id) or {}
    return [f for f in json.loads(row.get("risk_flags") or "[]")
            if (f.get("confidence") or 0) >= RENDER_THRESHOLD]


def test_terse_critical_potassium_from_a_nurse_charts_a_standing_alarm(ward):
    """The whole claim, end to end: five characters, one lethal potassium."""
    posted = _post(ward, "K 6.8")

    # The message itself is never at risk — Gate 3 only ever governed Sage.
    assert posted.json()["content"] == "K 6.8"

    assert _memory(ward["channel_id"]) is not None, (
        "Gate 3 dropped 'K 6.8' before memory — no room memory row exists, so "
        "the fast-path never ran and the room has no standing alarm"
    )
    live = _live_flags(ward["channel_id"])
    assert live, "no risk card above the sidebar's render threshold for K 6.8"
    assert any("6.8" in f["detail"] for f in live), live

    # And it is on the payload the sidebar actually renders from, not just in
    # the table (routes/relay_routes.py:176 get_sage_memory).
    api = client.get(f"/api/relay/channels/{ward['channel_id']}/sage_memory",
                     headers=ward["headers"])
    assert api.status_code == 200, api.text
    body = api.json()
    assert body, "sage_memory returned null — the sidebar renders nothing"
    rendered = [f for f in body["risk_flags"]
                if f.get("confidence", 0) >= RENDER_THRESHOLD]
    assert any("6.8" in f["detail"] for f in rendered), body["risk_flags"]

    # And it cost nothing: "K 6.8" carries no Gate 5 signal token, so the card
    # comes from the rule, not the model.
    assert ward["llm_calls"] == [], ward["llm_calls"]


@pytest.mark.parametrize("content,needle", [
    ("Na 118", "118"),
    ("GCS 6", "GCS 6"),
    ("sats 82", "82"),
])
def test_other_terse_critical_values_also_reach_the_sidebar(ward, content, needle):
    _post(ward, content)
    live = _live_flags(ward["channel_id"])
    assert live, f"no risk card for {content!r}"
    assert any(needle.lower() in f["detail"].lower() for f in live), live


# ── Over-tightening guards. These must hold with OR without the fix. ──────────

def test_ward_acknowledgements_still_never_reach_memory(ward):
    """The gate's actual job. Letting these through would put every 'ok' into
    room memory and (for anything matching Gate 5) into the model queue."""
    for chatter in ("ok", "thanks", "noted", "👍", "yes", "on it"):
        _post(ward, chatter)
    assert _memory(ward["channel_id"]) is None, (
        "ward chatter reached room memory — Gate 3 has been opened too far"
    )


def test_short_but_normal_values_chart_no_alarm(ward):
    """The false-alarm direction: alert fatigue is how the feature gets switched
    off at the ward, and then the real hyperkalaemia has no card either."""
    for normal in ("K 4.2", "Na 140", "pH 7.4", "GCS 15", "sats 98"):
        _post(ward, normal)
    assert not _live_flags(ward["channel_id"]), _live_flags(ward["channel_id"])


def test_a_normal_length_message_is_unaffected(ward):
    """Regression guard on the >= 8 char path the fix did not touch."""
    _post(ward, "Reviewed with the registrar, plan unchanged for tonight.")
    assert _memory(ward["channel_id"]) is not None
    assert not _live_flags(ward["channel_id"])
