"""Regressions for two defects found by audit on 2026-08-05.

1. GET /api/sage/ward-brief returned the hospital-wide snapshot to any holder of
   `sage.query`. A ward-scoped nurse received names, beds and risk levels for
   every admitted patient plus an LLM narrative written over the whole hospital.
   Same defect class as the Sage chat leak, in a surface nobody had checked
   because it is a dashboard rather than a question.

2. The wrong-room detector flagged a nurse who was CORRECTING a misroute
   ("The 80mg enoxaparin is for Suresh Reddy, not this patient."). Warning
   someone at the moment they prevent the error is the worst false positive this
   feature can produce — it punishes the right behaviour and teaches people to
   dismiss the warning.
"""
import services.room_fit_service as rf
from services.sage_narrator_service import ward_narrator
from tests.room_fit_corpus import CENSUS, STAFF_NAMES, POSITIVES, NEGATIVES


def _roster():
    return [
        rf.RosterPatient(
            patient_id=p[0], name=p[1], mrn=p[2], ward=p[3], bed=p[4],
            channel_id=f"ch-{p[0]}", channel_name=f"{p[1]} room",
        )
        for p in CENSUS
    ]


# ── 1. Ward brief department scoping ────────────────────────────────────────

def test_ward_brief_never_returns_a_patient_outside_the_callers_department():
    unscoped = (ward_narrator.get_brief(None).get("snapshot") or {})
    watchlist = unscoped.get("watchlist") or []
    departments = {w.get("department") for w in watchlist if w.get("department")}
    if not departments:
        return  # nothing on the watchlist to scope; nothing to assert

    for dept in departments:
        snap = ward_narrator.get_brief(dept).get("snapshot") or {}
        for row in snap.get("watchlist") or []:
            assert row.get("department") == dept, (
                f"ward brief scoped to {dept!r} leaked a patient from "
                f"{row.get('department')!r} — {row.get('name')!r} in bed {row.get('bed')!r}"
            )


def test_ward_brief_withholds_the_hospital_wide_narrative_from_a_scoped_caller():
    """The narrative is prose over the WHOLE ward and cannot be redacted after
    the fact, so a scoped caller must not receive it at all."""
    unscoped = ward_narrator.get_brief(None).get("snapshot") or {}
    departments = {
        w.get("department") for w in (unscoped.get("watchlist") or [])
        if w.get("department")
    }
    for dept in departments:
        assert ward_narrator.get_brief(dept).get("narrative") is None


def test_ward_brief_withholds_hospital_wide_aggregates_from_a_scoped_caller():
    unscoped = ward_narrator.get_brief(None).get("snapshot") or {}
    departments = {
        w.get("department") for w in (unscoped.get("watchlist") or [])
        if w.get("department")
    }
    for dept in departments:
        snap = ward_narrator.get_brief(dept).get("snapshot") or {}
        # Counts computed over every ward describe patients this caller cannot
        # see; recent_events carries patient_ids from other departments.
        assert snap.get("census") is None
        assert snap.get("active_alerts") is None
        assert (snap.get("recent_events") or []) == []
        assert snap.get("scope") == dept


def test_an_unscoped_admin_still_reads_hospital_wide():
    """department_scope returns None for a governance account seeded without a
    department. None means genuinely unscoped — not 'scope unknown'."""
    snap = ward_narrator.get_brief(None).get("snapshot") or {}
    assert "scope" not in snap


# ── 2. Attribution disclaimer suppressor ────────────────────────────────────

def test_correcting_a_misroute_is_not_itself_flagged_as_one():
    roster = _roster()
    room = roster[0]
    other = roster[3].name
    for text in (
        f"The 80mg enoxaparin is for {other}, not this patient.",
        f"That insulin chart belongs to {other}, wrong folder.",
        f"That 6.8 potassium was {other}'s, not ours.",
    ):
        verdict = rf.check_message(text, room, roster, set(STAFF_NAMES))
        assert verdict.band == "clear", (
            f"flagged a nurse who was correcting a misroute: {text!r} "
            f"(band={verdict.band}, suppressors={verdict.suppressors})"
        )


def test_a_disclaimer_cannot_excuse_a_record_of_your_own_action():
    """Documenting care YOU GAVE to another patient is a misroute whatever
    disclaimer trails it — the dose still lands in the wrong chart. Note the
    dropped subject: nursing documentation says "Gave 500 ml", not "I gave"."""
    roster = _roster()
    room = roster[0]
    other = roster[1].name
    for text in (
        f"Gave {other} 80mg enoxaparin at 2200, not this patient.",
        f"{other} BP 96/58, gave 500ml stat. belongs to another room.",
        f"Administered {other} 1g paracetamol, not ours.",
    ):
        verdict = rf.check_message(text, room, roster, set(STAFF_NAMES))
        assert verdict.band != "clear", (
            f"attribution disclaimer suppressed a genuine misroute: {text!r}"
        )


def test_the_new_suppressor_did_not_blunt_the_held_out_corpus():
    roster = _roster()
    by_id = {p.patient_id: p for p in roster}
    staff = set(STAFF_NAMES)

    missed = [
        text for room_id, text, _ in POSITIVES
        if rf.check_message(text, by_id.get(room_id, roster[0]), roster, staff).band == "clear"
    ]
    assert not missed, f"stopped detecting {len(missed)} known misroutes: {missed}"

    false_positives = []
    for item in NEGATIVES:
        room_id, text = (item[0], item[1]) if isinstance(item, (tuple, list)) and len(item) >= 2 \
            else (roster[0].patient_id, item)
        if rf.check_message(text, by_id.get(room_id, roster[0]), roster, staff).band != "clear":
            false_positives.append(text)
    assert not false_positives, f"new false positives: {false_positives}"
