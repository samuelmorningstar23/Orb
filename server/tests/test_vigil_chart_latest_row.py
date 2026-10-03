"""THE CHART'S COMMAND BAR AND ITS NEWS2 SCORE MUST BE ONE OBSERVATION.

WHAT WAS WRONG (ruled 2026-10-02). GET /api/vigil/patients/{id} builds the
chart from two separate reads of the vitals table:

  1. `_load_patient_bundle` fetched the vitals list with
     ``ORDER BY timestamp DESC`` and no tiebreak. Two sets CAN share an
     observation time (a corrected set re-charted at the same bedside time),
     and on a tie SQLite walks idx_vitals_patient in rowid ASC, so the OLDER,
     superseded set came back as vitals[0]. vitals[0] is what the command bar
     prints.
  2. The route then scored risk with `get_patient_risk(patient_id)`, which
     opens its own session and re-reads the latest row (it already had the
     ``id DESC`` tiebreak). Two sessions, two autocommit SELECTs, no shared
     snapshot: a bot tick or a monitor message landing between them meant the
     badge, gauge and breakdown described a set the bar was not showing.

So on a same-time correction the bar read HR 72 over a breakdown that said
"Severe tachycardia (HR 140)", and on a race the two halves of the screen
were simply different observations.

THE RULE: the bundle orders by ``timestamp DESC, id DESC`` (vitals.id is
AUTOINCREMENT, which is insertion order), and the route scores vitals[0], the
row it displays, through `vigil_service.risk_for_row`. `get_patient_risk`
keeps its own read for its other callers and returns the same thing.

The assertions are ABSOLUTE (heart rate 140, the correction's NEWS2), not
"bar agrees with score": a fix that made both halves agree on the superseded
set would still be wrong, and must still fail here.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from routes import vigil_routes
from services import vigil_service
from services.vigil_service import compute_news2

client = TestClient(app)

# Distinctive department name so this module cannot collide with any other
# test module's fixtures in the shared session DB.
DEPT = "Chart Latest Row Medicine"

# The superseded set: scores 0 on every parameter.
BENIGN = dict(heart_rate=72, systolic_bp=120, respiratory_rate=16, spo2=97,
              temperature=37.0)
# The correction charted at the SAME bedside time: four 3-point parameters.
PERI_ARREST = dict(heart_rate=140, systolic_bp=82, respiratory_rate=30, spo2=88,
                   temperature=37.0)
# The row a bot tick writes between the bundle read and the old second read.
# Every value differs from PERI_ARREST, so any field scored from it shows.
LATE_TICK = dict(heart_rate=104, systolic_bp=104, respiratory_rate=22, spo2=95,
                 temperature=38.4)

# Breakdown parameter label -> vitals column, for the five numeric vitals the
# command bar prints.
_BAR_PARAMS = {
    "Heart rate": "heart_rate",
    "Systolic BP": "systolic_bp",
    "Respiratory rate": "respiratory_rate",
    "SpO2": "spo2",
    "Temperature": "temperature",
}

_NO_VITALS_STUB = {"score": 0, "level": "stable",
                   "drivers": ["No vitals recorded"], "scoring_model": "NEWS2"}

# A fixed CfC result standing in for the model (no checkpoint ships with the
# tests). 0.91 is past the 0.9 line where the overlay lifts a NEWS2-stable
# patient to critical, so an overlay that ran cannot be mistaken for one that
# did not.
_HIGH_CFC = {
    "outcomes": {"deterioration": {"risk_by_horizon": {"1": 0.91},
                                   "peak_risk": 0.91}},
    "primary_risk": 0.91, "seq_len": 12, "model": "CfC-LNN",
}


def _news2(v: dict) -> dict:
    """The RCP score for a set charted alert and on air, Scale 1."""
    return compute_news2(v["heart_rate"], v["systolic_bp"], v["respiratory_rate"],
                         v["temperature"], v["spo2"], consciousness="alert",
                         on_oxygen=False, on_scale_2=False)


# ── fixtures ─────────────────────────────────────────────────────────────────

_USERS: list[str] = []
_PATIENTS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def _remove_what_this_module_made():
    """Delete every vitals, risk, admission, patient and user row this module
    wrote. Left admitted, a seeded patient picks up simulator vitals from any
    later module that boots the bot, and turns up in ward aggregates. A row
    another table now points at cannot be deleted under foreign_keys=ON; that
    patient is discharged instead, which takes it off every live surface.
    """
    yield
    for pid in _PATIENTS:
        try:
            with db_session() as db:
                db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
                db.execute("DELETE FROM risk_scores WHERE patient_id = ?", (pid,))
                db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
                db.execute("DELETE FROM patients WHERE id = ?", (pid,))
        except Exception:
            with db_session() as db:
                db.execute("UPDATE patients SET status = 'discharged' WHERE id = ?",
                           (pid,))
    for uid in _USERS:
        try:
            with db_session() as db:
                db.execute("DELETE FROM users WHERE id = ?", (uid,))
        except Exception:
            pass
    _PATIENTS.clear()
    _USERS.clear()


@pytest.fixture(scope="module")
def doctor():
    uid = f"CLRD{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Chart Latest Row Doctor', 'doctor', 'x', ?)",
            (uid, DEPT),
        )
    _USERS.append(uid)
    return uid


def _auth(user_id):
    return {"Authorization": f"Bearer {create_token(user_id, 'doctor')}"}


def _mk_patient(doctor_id, *, on_scale_2=0):
    """An admitted patient on the doctor's ward, with no vitals yet."""
    pid = f"CLRP{uuid.uuid4().hex[:8].upper()}"
    _PATIENTS.append(pid)
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date, on_scale_2)
               VALUES (?, 'Chartrow Meena Iyer', ?, 'CLR-1', ?, 64, 'F',
                       'Chartrow sepsis query', 'admitted', ?, ?)""",
            (pid, f"MRN-{uuid.uuid4().hex[:8].upper()}", f"{DEPT} bay", now,
             on_scale_2),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, ward, bed, status, "
            "admitted_by) VALUES (?, ?, ?, ?, 'CLR-1', 'active', ?)",
            (f"ADM-CLR-{uuid.uuid4().hex[:8].upper()}", pid, DEPT, DEPT, doctor_id),
        )
    return pid


def _chart(pid, v: dict, ts: str, *, on_oxygen=0):
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   diastolic_bp, spo2, temperature,
                                   respiratory_rate, consciousness, on_oxygen)
               VALUES (?, ?, ?, ?, 70, ?, ?, ?, 'alert', ?)""",
            (pid, ts, v["heart_rate"], v["systolic_bp"], v["spo2"],
             v["temperature"], v["respiratory_rate"], on_oxygen),
        )
        return cur.lastrowid


@pytest.fixture
def corrected_patient(doctor):
    """A benign set, then a peri-arrest correction at the SAME timestamp.

    Inserted in that order so the correction has the higher id, which is what
    a re-charted set looks like in the table.
    """
    pid = _mk_patient(doctor)
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    benign_id = _chart(pid, BENIGN, ts)
    correction_id = _chart(pid, PERI_ARREST, ts)
    assert correction_id > benign_id
    return {"pid": pid, "ts": ts, "benign_id": benign_id,
            "correction_id": correction_id}


# ═════════════════════════════════════════════════════════════════════════════
# (a) The bundle returns the correction first
# ═════════════════════════════════════════════════════════════════════════════

def test_bundle_returns_the_same_time_correction_first(corrected_patient):
    vitals = vigil_routes._load_patient_bundle(corrected_patient["pid"])[1]

    # Control: both sets are there, so an ordering failure is not a missing row.
    assert {v["id"] for v in vitals} == {corrected_patient["benign_id"],
                                         corrected_patient["correction_id"]}
    assert vitals[0]["id"] == corrected_patient["correction_id"], (
        "on a timestamp tie the bundle put the SUPERSEDED set first, so the "
        f"command bar would print HR {vitals[0]['heart_rate']:.0f} over a charted "
        f"HR {PERI_ARREST['heart_rate']}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# (b) The chart route: bar and score are the correction, in absolute terms
# ═════════════════════════════════════════════════════════════════════════════

def test_chart_route_shows_and_scores_the_correction(corrected_patient, doctor):
    expected = _news2(PERI_ARREST)
    superseded = _news2(BENIGN)
    assert expected["raw_news2"] > superseded["raw_news2"]

    resp = client.get(f"/api/vigil/patients/{corrected_patient['pid']}",
                      headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    risk = body["risk"]

    # Control: the score is the correction's (get_patient_risk already broke
    # the tie by id), so what follows is about the bar row, not the scorer.
    assert risk["raw_news2"] == expected["raw_news2"]

    assert body["vitals"][0]["heart_rate"] == PERI_ARREST["heart_rate"], (
        f"the command bar prints HR {body['vitals'][0]['heart_rate']:.0f} from the "
        f"superseded set while the score is NEWS2 {risk['raw_news2']}"
    )
    hr = next(b for b in risk["breakdown"] if b["parameter"] == "Heart rate")
    assert hr["value"] == body["vitals"][0]["heart_rate"]


# ═════════════════════════════════════════════════════════════════════════════
# Control: timestamp stays the primary sort key (a back-charted set)
# ═════════════════════════════════════════════════════════════════════════════

def test_back_charted_set_does_not_become_the_bar(doctor):
    """id DESC only breaks a TIE. A set entered late for an earlier bedside
    time (back-charting) has the higher id but the older timestamp, and must
    stay history: is_latest_vitals_row treats it the same way. Every other
    test here inserts in time order, where id order and time order agree, so
    a bundle that sorted by id alone would pass them all."""
    pid = _mk_patient(doctor)
    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    newest_id = _chart(pid, PERI_ARREST, t0.isoformat())
    late_entry_id = _chart(pid, BENIGN, (t0 - timedelta(hours=2)).isoformat())
    # Control: the late entry really did get the higher id.
    assert late_entry_id > newest_id

    vitals = vigil_routes._load_patient_bundle(pid)[1]
    assert vitals[0]["id"] == newest_id, (
        "the bundle put the back-charted set (2 h older, entered later) first, "
        "so id order overrode the observation time"
    )

    resp = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vitals"][0]["heart_rate"] == PERI_ARREST["heart_rate"]
    assert body["risk"]["raw_news2"] == _news2(PERI_ARREST)["raw_news2"]


# ═════════════════════════════════════════════════════════════════════════════
# (c) The race: a row lands between the bundle read and the score
# ═════════════════════════════════════════════════════════════════════════════

def test_score_comes_from_the_row_the_bar_shows_not_a_second_read(doctor,
                                                                  monkeypatch):
    pid = _mk_patient(doctor)
    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    _chart(pid, PERI_ARREST, t0.isoformat())

    real_bundle = vigil_routes._load_patient_bundle
    injected: list[int] = []

    def _bundle_then_tick(patient_id):
        out = real_bundle(patient_id)
        # A bot tick charting a NEWER set after the bundle's SELECT returned.
        injected.append(_chart(patient_id, LATE_TICK,
                               (t0 + timedelta(minutes=1)).isoformat()))
        return out

    monkeypatch.setattr(vigil_routes, "_load_patient_bundle", _bundle_then_tick)

    resp = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert injected, "the wrapper never ran, so this test proved nothing"

    # Control: the bar is the row the bundle read before the tick.
    bar = body["vitals"][0]
    assert bar["heart_rate"] == PERI_ARREST["heart_rate"]

    scored = {b["parameter"]: b["value"] for b in body["risk"]["breakdown"]}
    for label, column in _BAR_PARAMS.items():
        assert scored[label] == bar[column], (
            f"{label}: the breakdown scored {scored[label]} but the command bar "
            f"shows {bar[column]}; the score came from a second read that the "
            "tick landed between"
        )
    assert body["risk"]["raw_news2"] == _news2(PERI_ARREST)["raw_news2"]


# ═════════════════════════════════════════════════════════════════════════════
# (d) Control: no vitals is the stub and an empty list
# ═════════════════════════════════════════════════════════════════════════════

def test_patient_with_no_vitals_gets_the_stub(doctor):
    pid = _mk_patient(doctor)
    resp = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["vitals"] == []
    assert body["risk"] == _NO_VITALS_STUB
    assert body["risk"]["level"] == "stable"
    assert body["risk"]["drivers"] == ["No vitals recorded"]


# ═════════════════════════════════════════════════════════════════════════════
# (e) Control: get_patient_risk is risk_for_row on the latest row
# ═════════════════════════════════════════════════════════════════════════════

def test_get_patient_risk_is_risk_for_row_on_the_latest_row(corrected_patient,
                                                           monkeypatch):
    """Parity for every other get_patient_risk caller (I-PASS, the seed
    script). A fixed fake overlay, so the comparison also proves risk_for_row
    itself applies the CfC overlay. This is a service-level check only: that
    the chart ROUTE still passes the patient through to the overlay is pinned
    by test_chart_route_applies_the_cfc_overlay_for_this_patient below."""
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", lambda pid: _HIGH_CFC)
    pid = corrected_patient["pid"]
    with db_session() as db:
        row = db.execute(
            """SELECT * FROM vitals WHERE patient_id = ?
               ORDER BY timestamp DESC, id DESC LIMIT 1""",
            (pid,),
        ).fetchone()

    via_wrapper = vigil_service.get_patient_risk(pid)
    via_row = vigil_service.risk_for_row(row, patient_id=pid, on_scale_2=False)

    assert via_wrapper == via_row
    assert via_row["raw_news2"] == _news2(PERI_ARREST)["raw_news2"]
    assert via_row.get("cfc_primary_risk") == 0.91, (
        "risk_for_row scored the row without the CfC overlay"
    )
    assert vigil_service.risk_for_row(None, patient_id=pid,
                                      on_scale_2=False) == _NO_VITALS_STUB


def test_get_patient_risk_keeps_a_scale_2_patient_on_scale_2(doctor):
    """get_patient_risk still reads patients.on_scale_2 itself and hands it to
    risk_for_row; I-PASS and the seed script score through it and have no
    patient row of their own. Same numbers as the route test below: SpO2 90 on
    air is 0 points on Scale 2 and 3 on Scale 1."""
    pid = _mk_patient(doctor, on_scale_2=1)
    _chart(pid, dict(BENIGN, spo2=90),
           datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    with db_session() as db:
        row = db.execute(
            """SELECT * FROM vitals WHERE patient_id = ?
               ORDER BY timestamp DESC, id DESC LIMIT 1""",
            (pid,),
        ).fetchone()

    risk = vigil_service.get_patient_risk(pid)

    assert risk["spo2_scale"] == 2, (
        "get_patient_risk scored a Scale 2 patient on Scale 1: SpO2 90 on air "
        f"became {risk['raw_news2']} points"
    )
    assert risk["raw_news2"] == 0
    assert risk == vigil_service.risk_for_row(row, patient_id=pid, on_scale_2=True)


# ═════════════════════════════════════════════════════════════════════════════
# The chart route keeps the CfC overlay, for THIS patient
# ═════════════════════════════════════════════════════════════════════════════

def test_chart_route_applies_the_cfc_overlay_for_this_patient(doctor,
                                                             monkeypatch):
    """The overlay may lift a NEWS2-stable patient to critical. Before the fix
    the route got it through get_patient_risk; now it must hand the patient id
    to risk_for_row itself. A route that scored vitals[0] with the overlay off,
    or without the patient id (score_vitals_row then skips the overlay), would
    show 'stable' on the chart while the board says critical.

    The fake answers only for this patient, so an overlay asked about any
    other id does not pass either."""
    pid = _mk_patient(doctor)
    _chart(pid, BENIGN,
           datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    monkeypatch.setattr(vigil_service, "compute_cfc_risk",
                        lambda asked: _HIGH_CFC if asked == pid else None)

    resp = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor))
    assert resp.status_code == 200, resp.text
    risk = resp.json()["risk"]

    # Control: the NEWS2 half is the benign set's 0, so 'critical' below can
    # only have come from the overlay.
    assert risk["raw_news2"] == _news2(BENIGN)["raw_news2"] == 0
    assert risk.get("cfc_primary_risk") == 0.91, (
        "the chart route scored vitals[0] without the CfC overlay for this "
        f"patient: level {risk['level']!r}, scoring_model {risk['scoring_model']!r}"
    )
    assert risk["level"] == "critical"
    assert risk["scoring_model"] == "NEWS2+CfC-LNN"


# ═════════════════════════════════════════════════════════════════════════════
# Control: the route still scores the patient's recorded SpO2 scale
# ═════════════════════════════════════════════════════════════════════════════

def test_chart_route_scores_a_scale_2_patient_on_scale_2(doctor):
    """The route now reads on_scale_2 from the patient row it already has
    instead of get_patient_risk's own lookup. SpO2 90 on air is on target for
    Scale 2 (0 points) and 3 points on Scale 1, so a dropped flag shows."""
    pid = _mk_patient(doctor, on_scale_2=1)
    on_target = dict(BENIGN, spo2=90)
    _chart(pid, on_target,
           datetime.now(timezone.utc).replace(microsecond=0).isoformat())

    body = client.get(f"/api/vigil/patients/{pid}", headers=_auth(doctor)).json()
    risk = body["risk"]
    assert risk["spo2_scale"] == 2
    spo2 = next(b for b in risk["breakdown"] if b["parameter"] == "SpO2 (Scale 2)")
    assert spo2["points"] == 0
    assert risk["raw_news2"] == 0
