"""The Sage ward brief bands each patient from the SAME moment as the Vigil board.

Verified in the browser 2026-10-03: the brief above the board said "Priya Nair,
bed GM-103, ELEVATED" while her board row showed NEWS2 8 and the Critical tile
counted her. The board scores each patient's latest vitals row through
`vigil_service.score_vitals_row` (ruled 2026-10-03, see test_vigil_board_news2).
The brief built its watchlist and risk mix from `risk_scores`, and the vitals
simulator and the Scribe lane write vitals with no risk row, so the brief kept
the admission-era band while the board moved on. They only agreed before the
board fix because both were stale.

These tests pin the brief to the board: same latest-vitals row (timestamp DESC,
id DESC), same scorer, same overlay, same scale, same order, same population.
"""
import uuid
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import vigil_service
from services.sage_narrator_service import WardNarrator, ward_narrator

client = TestClient(app)

TS = "2026-10-03T08:00:00+00:00"
EARLIER = "2026-10-03T06:00:00+00:00"

# NEWS2 on Scale 1, worked by hand from the RCP chart (same sets as the board pins).
# HR 135 (3) + RR 26 (3) + SpO2 91 (3) = 9, critical.
CRITICAL_9 = dict(heart_rate=135.0, systolic_bp=118.0, respiratory_rate=26.0,
                  temperature=37.0, spo2=91.0)
# RR 25 (3) alone = 3, a single parameter scoring 3, so 'elevated'.
SINGLE3_3 = dict(heart_rate=80.0, systolic_bp=120.0, respiratory_rate=25.0,
                 temperature=37.0, spo2=97.0)
# HR 105 (1) + Temp 38.5 (1) + SpO2 95 (1) = 3, no single 3, so 'stable'.
SPREAD_3 = dict(heart_rate=105.0, systolic_bp=120.0, respiratory_rate=16.0,
                temperature=38.5, spo2=95.0)
# RR 22 (2) + SpO2 93 (2) + HR 96 (1) + T 38.5 (1) = 6: 'elevated' on the table.
ELEVATED_6 = dict(heart_rate=96.0, systolic_bp=118.0, respiratory_rate=22.0,
                  temperature=38.5, spo2=93.0)
# Every vital in its 0-point band.
BENIGN_0 = dict(heart_rate=80.0, systolic_bp=120.0, respiratory_rate=16.0,
                temperature=37.0, spo2=97.0)


@pytest.fixture(autouse=True)
def _no_checkpoint(monkeypatch):
    """Today's shipping state, made explicit: no CfC checkpoint loaded."""
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", lambda _pid: None)


def _ward():
    """A doctor alone in a fresh department, so no other test's patients appear."""
    dept = f"Brief Vitals {uuid.uuid4().hex[:8]}"
    uid = f"BVU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Brief Vitals Doctor', 'doctor', 'x', ?)",
            (uid, dept),
        )
    return dept, uid, {"Authorization": f"Bearer {create_token(uid, 'doctor')}"}


def _patient(dept, admitted_by, *, on_scale_2=False):
    pid = f"BVP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date, on_scale_2)
               VALUES (?, ?, ?, ?, ?, 60, 'F', 'Brief test', 'admitted', ?, ?)""",
            (pid, f"Brief {pid}", f"MRN-{pid}", f"BV-{pid[-4:]}", dept, TS,
             1 if on_scale_2 else 0),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, ?, 'active', ?)",
            (pid, dept, dept, f"BV-{pid[-4:]}", admitted_by),
        )
    return pid


def _vitals(pid, v, *, ts=TS, consciousness="alert", on_oxygen=0):
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals
                   (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                    spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                    source)
               VALUES (?, ?, ?, ?, 75, ?, ?, ?, ?, ?, 'simulator')""",
            (pid, ts, v["heart_rate"], v["systolic_bp"], v["spo2"],
             v["temperature"], v["respiratory_rate"], consciousness, on_oxygen),
        )


def _risk_row(pid, *, score, level, ts=TS):
    with db_session() as db:
        db.execute(
            "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
            "VALUES (?, ?, ?, ?, '[\"stored driver\"]')",
            (pid, ts, score, level),
        )


def _brief(dept):
    """The brief a caller scoped to `dept` would get, built fresh."""
    narrator = WardNarrator()
    narrator._state["snapshot"] = narrator._build_ward_snapshot()
    return narrator.get_brief(dept)["snapshot"]


def _entries(snap, pid):
    return [w for w in snap["watchlist"] if w["patient_id"] == pid]


# ── 1. The band follows the latest vitals, not a stale risk row ──────────────

def test_a_stale_elevated_risk_row_does_not_hide_a_critical_patient():
    """The Priya Nair case: the newest risk row says 'elevated' (the admission
    baseline), the newest vitals score NEWS2 9. The board says critical."""
    dept, uid, _ = _ward()
    pid = _patient(dept, uid)
    _risk_row(pid, score=0.3, level="elevated", ts=EARLIER)
    _vitals(pid, ELEVATED_6, ts=EARLIER)
    _vitals(pid, CRITICAL_9, ts=TS)

    snap = _brief(dept)
    (entry,) = _entries(snap, pid)
    assert entry["level"] == "critical"
    assert snap["risk_distribution"]["critical"] == 1
    assert snap["risk_distribution"]["elevated"] == 0


def test_a_patient_who_recovered_leaves_the_watchlist():
    """The other direction: a critical risk row is the newest risk row, but the
    newest observation is benign. The brief must not keep calling them critical."""
    dept, uid, _ = _ward()
    pid = _patient(dept, uid)
    _risk_row(pid, score=0.9, level="critical", ts=TS)
    _vitals(pid, CRITICAL_9, ts=EARLIER)
    _vitals(pid, BENIGN_0, ts=TS)

    snap = _brief(dept)
    assert _entries(snap, pid) == []
    assert snap["risk_distribution"]["critical"] == 0
    assert snap["risk_distribution"]["stable"] == 1


def test_a_risk_row_with_no_vitals_behind_it_is_not_a_band():
    """The board shows such a patient as 'not recorded' and counts them in no
    tile. The brief must not name them on the watchlist or count them."""
    dept, uid, _ = _ward()
    pid = _patient(dept, uid)
    _risk_row(pid, score=0.9, level="critical")

    snap = _brief(dept)
    assert _entries(snap, pid) == []
    assert sum(snap["risk_distribution"].values()) == 0


def test_the_brief_scores_on_the_patients_own_spo2_scale():
    """SpO2 87 on air: 1 point on Scale 2 (target 88-92), 3 on Scale 1. With
    RR 22 (2) and HR 96 (1) that is 4 'stable' on Scale 2, 6 'elevated' on 1."""
    dept, uid, _ = _ward()
    pid = _patient(dept, uid, on_scale_2=True)
    _risk_row(pid, score=0.3, level="elevated")
    _vitals(pid, dict(BENIGN_0, respiratory_rate=22.0, heart_rate=96.0, spo2=87.0))

    snap = _brief(dept)
    assert _entries(snap, pid) == []
    assert snap["risk_distribution"]["stable"] == 1


# ── 2. A timestamp tie lists the patient once, in the correction's band ──────

def test_a_timestamp_tie_lists_the_patient_once_in_the_corrections_band():
    """Two vitals rows at the same instant: the higher id is the later write (a
    same-time correction), so it is the one scored. Two risk rows tie too."""
    dept, uid, _ = _ward()
    pid = _patient(dept, uid)
    _risk_row(pid, score=0.3, level="elevated")
    _risk_row(pid, score=0.3, level="elevated")
    _vitals(pid, BENIGN_0)
    _vitals(pid, CRITICAL_9)

    snap = _brief(dept)
    entries = _entries(snap, pid)
    assert len(entries) == 1, entries
    assert entries[0]["level"] == "critical"
    assert sum(snap["risk_distribution"].values()) == 1


def test_a_correction_to_benign_at_the_same_instant_wins():
    dept, uid, _ = _ward()
    pid = _patient(dept, uid)
    _risk_row(pid, score=0.45, level="critical")
    _vitals(pid, CRITICAL_9)
    _vitals(pid, BENIGN_0)

    snap = _brief(dept)
    assert _entries(snap, pid) == []
    assert snap["risk_distribution"] == {"critical": 0, "elevated": 0,
                                         "moderate": 0, "stable": 1}


# ── 3. Parity: the brief says what the board says ────────────────────────────

def _ward_of_five():
    """One patient per board case, each with a risk row that DISAGREES with the
    latest vitals, so a brief reading risk_scores cannot pass by accident."""
    dept, uid, headers = _ward()
    crit = _patient(dept, uid)
    _risk_row(crit, score=0.3, level="elevated")
    _vitals(crit, CRITICAL_9)
    single3 = _patient(dept, uid)
    _risk_row(single3, score=0.1, level="stable")
    _vitals(single3, SINGLE3_3)
    elev6 = _patient(dept, uid)
    _risk_row(elev6, score=0.9, level="critical")
    _vitals(elev6, ELEVATED_6)
    spread = _patient(dept, uid)
    _risk_row(spread, score=0.45, level="critical")
    _vitals(spread, SPREAD_3)
    none = _patient(dept, uid)
    _risk_row(none, score=0.9, level="critical")
    return dept, headers, [crit, elev6, single3, spread, none]


def _board(headers):
    r = client.get("/api/vigil/patients", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _assert_parity(board, snap):
    on_watch = [r for r in board if r["risk_level"] in ("critical", "elevated")]
    # Same patients, same band, same order as the board.
    assert [(w["patient_id"], w["level"]) for w in snap["watchlist"]] == \
        [(r["id"], r["risk_level"]) for r in on_watch]
    # The risk mix is the board's tiles: one count per scored row, nothing for
    # a patient with no observation.
    tiles = Counter(r["risk_level"] for r in board if r["risk_level"])
    for level in ("critical", "elevated", "stable"):
        assert snap["risk_distribution"][level] == tiles.get(level, 0), (level, snap, tiles)


def test_the_brief_band_equals_the_board_row_for_every_patient():
    dept, headers, (crit, elev6, single3, spread, none) = _ward_of_five()

    board = _board(headers)
    assert [r["id"] for r in board] == [crit, elev6, single3, spread, none], \
        "precondition: the board order these tests were worked against"
    _assert_parity(board, _brief(dept))


def test_parity_holds_when_the_overlay_escalates(monkeypatch):
    """The CfC overlay can make an NEWS2-6 patient critical. The board applies
    it, so the brief must too, from the same entry point."""
    def _high(_pid):
        return {"outcomes": {"deterioration": {"risk_by_horizon": {"6": 0.92},
                                               "peak_risk": 0.92}},
                "primary_risk": 0.92, "seq_len": 12, "model": "CfC-LNN"}
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", _high)

    dept, headers, _ = _ward_of_five()
    board = _board(headers)
    assert sum(r["risk_level"] == "critical" for r in board) == 4, \
        "precondition: the overlay escalated every scored row"
    _assert_parity(board, _brief(dept))


def test_the_ward_brief_route_matches_the_board_route(monkeypatch):
    """End to end, as the two cards on one screen fetch them, for the same
    scoped doctor. The singleton is made to build fresh, as on first load."""
    dept, headers, _ = _ward_of_five()
    monkeypatch.setitem(ward_narrator._state, "snapshot", None)

    board = _board(headers)
    r = client.get("/api/sage/ward-brief", headers=headers)
    assert r.status_code == 200, r.text
    snap = r.json()["snapshot"]
    assert snap["scope"] == dept
    _assert_parity(board, snap)


# ── 4. Cadence: the snapshot keeps up with the board, the model does not run more ──
# Ruled 2026-10-03. No event fires when a vitals write moves a band (vigil.alert.*
# is published for critical alerts only), so a rise to elevated or any recovery
# waited for the 300 s idle tick. The snapshot is now rebuilt every 30 s, the
# board's poll; the narrative keeps its old cadence.

class _Stop(Exception):
    pass


def test_idle_loop_rebuilds_every_30_s_and_narrates_every_300_s(monkeypatch):
    import asyncio
    import types

    from services import sage_narrator_service as narrator_mod

    clock = {"t": 1000.0}
    sleeps, narrate_flags = [], []

    async def _sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds
        if clock["t"] > 1000.0 + 600:
            raise _Stop

    async def _refresh(self, reason="event", narrate=True):
        narrate_flags.append(narrate)

    # The module's own references only: asyncio's loop keeps the real clock.
    monkeypatch.setattr(narrator_mod, "time",
                        types.SimpleNamespace(monotonic=lambda: clock["t"]))
    monkeypatch.setattr(narrator_mod, "asyncio", types.SimpleNamespace(sleep=_sleep))
    monkeypatch.setattr(narrator_mod.WardNarrator, "refresh", _refresh)

    with pytest.raises(_Stop):
        asyncio.run(narrator_mod.WardNarrator().idle_loop())

    assert set(sleeps) == {narrator_mod.SNAPSHOT_REFRESH_SECONDS} == {30}
    assert len(narrate_flags) == 20, "one snapshot rebuild per 30 s for 600 s"
    # The model is asked at 300 s and 600 s only, as before the 30 s tick.
    assert [i for i, n in enumerate(narrate_flags) if n] == [9, 19]


def test_a_snapshot_tick_never_runs_the_model(monkeypatch):
    import asyncio

    import ws.realtime as realtime

    calls = []

    async def _narrate(self, snapshot):
        calls.append(snapshot["as_of"])
        return "prose"

    async def _emit(*_a, **_k):
        return None

    monkeypatch.setattr(WardNarrator, "_narrate", _narrate)
    monkeypatch.setattr(realtime.sio, "emit", _emit)

    narrator = WardNarrator()
    narrator._build_ward_snapshot = lambda: {
        "census": 1, "risk_distribution": {}, "active_alerts": 0,
        "watchlist": [], "recent_events": [], "as_of": "2026-10-03T08:00:00+00:00",
    }
    asyncio.run(narrator.refresh("idle_refresh", narrate=False))
    assert calls == [] and narrator._state["narrative"] is None
    assert narrator._state["snapshot"]["census"] == 1, "the snapshot itself was rebuilt"

    asyncio.run(narrator.refresh("vigil.alert.critical"))
    assert calls == ["2026-10-03T08:00:00+00:00"]
    assert narrator._state["narrative"] == "prose"
