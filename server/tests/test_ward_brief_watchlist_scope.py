"""Ward brief: scope first, then cut to eight; and only tell clients to refetch
when something they show has changed.

The narrator cut the hospital-wide watchlist to eight BEFORE get_brief filtered
it by department, so a critical patient ranked ninth hospital-wide vanished
from their own ward's brief. And every refresh emitted sage:ward_brief, every
emit made every open card refetch, and every refetch wrote a PHI read row.
"""
import asyncio
import uuid
from datetime import datetime, timezone

from database import db_session
from services import sage_narrator_service as narrator_mod

DEPT_MINE = "Brief Ninth Medicine"
DEPT_OTHER = "Brief Ninth Elsewhere"


# The brief bands from the latest VITALS row since 2026-10-03, scored exactly as
# the board scores it, so these patients are seeded with vitals, worked by hand
# from the RCP chart. HR 135 (3) + RR 26 (3) + SpO2 91 (3) + SBP 95 (2) +
# T 39.5 (2) = NEWS2 13; without the SBP and temperature it is 9; all normal is 0.
NEWS2_13 = (135, 95, 91, 39.5, 26)
NEWS2_9 = (135, 118, 91, 37.0, 26)
NEWS2_0 = (80, 120, 97, 37.0, 16)


def _mk_scored_patient(department, vitals):
    hr, sbp, spo2, temp, rr = vitals
    pid = f"BRF{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('BRFADM', 'Brief Admitter', 'doctor', 'x')"
        )
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, status) "
            "VALUES (?, ?, ?, 'B-9', ?, 'admitted')",
            (pid, f"Brief Case {pid[-4:]}", f"MRN-{pid}", f"{department} bay"),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, 'B-9', 'active', 'BRFADM')",
            (pid, department, department),
        )
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen)
               VALUES (?, ?, ?, ?, 75, ?, ?, ?, 'alert', 0)""",
            (pid, now, hr, sbp, spo2, temp, rr),
        )
    return pid


def test_scoped_brief_keeps_a_critical_patient_ranked_ninth_hospital_wide():
    for _ in range(9):
        _mk_scored_patient(DEPT_OTHER, NEWS2_13)
    mine = _mk_scored_patient(DEPT_MINE, NEWS2_9)
    _mk_scored_patient(DEPT_MINE, NEWS2_0)

    narrator = narrator_mod.WardNarrator()
    snapshot = narrator._build_ward_snapshot()
    narrator._state["snapshot"] = snapshot

    hospital = narrator.get_brief(None)["snapshot"]
    assert mine not in {w["patient_id"] for w in hospital["watchlist"]}, \
        "precondition: the patient is outside the hospital-wide top eight"
    assert len(hospital["watchlist"]) <= narrator_mod.WATCHLIST_LIMIT

    scoped = narrator.get_brief(DEPT_MINE)["snapshot"]
    assert mine in {w["patient_id"] for w in scoped["watchlist"]}
    assert all(w["department"] == DEPT_MINE for w in scoped["watchlist"])
    # The department's own risk mix, stable patients included.
    assert scoped["risk_distribution"]["critical"] >= 1
    assert scoped["risk_distribution"]["stable"] >= 1
    assert scoped["census"] is None and scoped["scope"] == DEPT_MINE


def test_private_snapshot_keys_never_leave_the_server():
    narrator = narrator_mod.WardNarrator()
    narrator._state["snapshot"] = narrator._build_ward_snapshot()
    for dept in (None, DEPT_MINE):
        snap = narrator.get_brief(dept)["snapshot"]
        for key in narrator_mod._PRIVATE_KEYS:
            assert key not in snap, f"{key} leaked to a {'scoped' if dept else 'hospital'} caller"


def test_scoped_caller_gets_no_engine_line_without_a_narrative():
    narrator = narrator_mod.WardNarrator()
    narrator._state.update({
        "snapshot": narrator._build_ward_snapshot(),
        "narrative": "Hospital-wide prose.",
        "narrative_engine": "local/medgemma",
    })
    scoped = narrator.get_brief(DEPT_MINE)
    assert scoped["narrative"] is None
    assert scoped["narrative_engine"] is None


def test_refresh_emits_only_when_the_brief_changed(monkeypatch):
    import ws.realtime as realtime

    emitted = []

    async def _emit(event, payload):
        emitted.append((event, payload))

    async def _no_narrative(self, snapshot):
        return None

    monkeypatch.setattr(realtime.sio, "emit", _emit)
    monkeypatch.setattr(narrator_mod.WardNarrator, "_narrate", _no_narrative)

    narrator = narrator_mod.WardNarrator()
    base = {
        "census": 3, "risk_distribution": {"critical": 1}, "active_alerts": 0,
        "watchlist": [{"patient_id": "P1", "level": "critical", "score": 80, "bed": "1"}],
        "recent_events": [],
    }
    clock = iter(["2026-10-01T08:00:00+00:00", "2026-10-01T08:01:00+00:00",
                  "2026-10-01T08:02:00+00:00", "2026-10-01T08:03:00+00:00"])
    current = {"snap": base}
    narrator._build_ward_snapshot = lambda: {**current["snap"], "as_of": next(clock)}

    asyncio.run(narrator.refresh("first"))
    asyncio.run(narrator.refresh("same again"))
    assert len(emitted) == 1, "an identical brief must not make every card refetch"

    # The number moved, the band did not. The card prints only name, bed and
    # band, and the snapshot is rebuilt every 30 s from the latest vitals since
    # 2026-10-03, so emitting on the number would make every open card refetch
    # (and write a PHI read row) on most simulator ticks.
    current["snap"] = {**base, "watchlist": [
        {"patient_id": "P1", "level": "critical", "score": 91, "bed": "1"}]}
    asyncio.run(narrator.refresh("score moved"))
    assert len(emitted) == 1, "a number the card does not print must not refetch"

    current["snap"] = {**base, "risk_distribution": {"elevated": 1}, "watchlist": [
        {"patient_id": "P1", "level": "elevated", "score": 91, "bed": "1"}]}
    asyncio.run(narrator.refresh("band moved"))
    assert len(emitted) == 2
    assert emitted[-1] == ("sage:ward_brief", {"updated_at": "2026-10-01T08:03:00+00:00"})


def test_a_department_with_no_scored_patient_reads_all_zeros():
    narrator = narrator_mod.WardNarrator()
    narrator._state["snapshot"] = narrator._build_ward_snapshot()
    scoped = narrator.get_brief("Brief Department With Nobody")["snapshot"]
    assert scoped["watchlist"] == []
    assert scoped["risk_distribution"] == {
        "critical": 0, "elevated": 0, "moderate": 0, "stable": 0,
    }
