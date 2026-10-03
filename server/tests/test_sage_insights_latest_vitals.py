"""Sage "needs your attention" Rule 1 bands from the SAME moment as the Vigil board.

Verified in the browser 2026-10-03: after a simulator-style benign vitals write
for Rajesh Iyer (GM-102), the board and the ward brief showed him stable while
the insights feed above them still said "Rajesh Iyer (GM-102) is critical" with
"Risk level CRITICAL. Mild tachycardia.", drivers that matched neither his
latest nor his prior vitals. Rule 1 read `risk_scores`, and the simulator and
the Scribe lane write vitals with no risk row. On a seeded ward the feed's
critical set was the exact inverse of the board's.

Ruled by Samuel 2026-10-03:
  * Rule 1 bands from each patient's latest vitals row (timestamp DESC, id
    DESC) through `score_vitals_row`, overlay on, on the patient's own SpO2
    scale: the board's scorer (test_vigil_board_news2).
  * The text is the NEWS2 label (news2_text.news2_description, the one server
    label, test_news2_prompt_label) and the Relay reason line
    (vigil_service.news2_reason_line).
  * "Rose from" compares against the newest row at least 15 minutes older, not
    the previous row: the simulator writes every 5 s and the sweep runs every
    15 s, so a previous-row compare saw about one rise in three.
  * On recovery the card is rewritten to "was critical" at once and resolves 10
    minutes later; going critical again flips the same card back.
  * An incomplete set below critical is NOT recovery: an unmeasured parameter
    scores 0, so it can only under-score.
Then, same day ("fix and build everything"):
  * A stood-down card is severity info (a blue dot), and goes back to its own
    severity if the patient goes back up.
  * An incomplete set IS recovery when even the unmeasured parameters at their
    worst could not reach the card's band (a monitor feed's NEWS2 1 with no ACVPU
    or O2 can reach 6, never 7). Before, a monitor-only patient's card never cleared.
  * A card put back up is pushed on the socket, not left for the feed's poll.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import vigil_service
from services.clinical_time import format_utc
from services.sage_proactive_service import NON_ACTIONABLE_ACTION_TYPES, evaluate_patient

client = TestClient(app)

BASE = datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc)


def _at(minutes=0, seconds=0):
    """A canonical timestamp `minutes`/`seconds` before BASE."""
    return format_utc(BASE - timedelta(minutes=minutes, seconds=seconds))


TS = _at()
EARLIER = _at(minutes=120)

# NEWS2 on Scale 1, worked by hand from the RCP chart (the board's pins).
# HR 135 (3) + RR 26 (3) + SpO2 91 (3) = 9, critical.
CRITICAL_9 = dict(heart_rate=135.0, systolic_bp=118.0, respiratory_rate=26.0,
                  temperature=37.0, spo2=91.0)
# RR 25 (3) alone = 3, a single parameter scoring 3, so 'elevated'.
SINGLE3_3 = dict(heart_rate=80.0, systolic_bp=120.0, respiratory_rate=25.0,
                 temperature=37.0, spo2=97.0)
# Every vital in its 0-point band.
BENIGN_0 = dict(heart_rate=80.0, systolic_bp=120.0, respiratory_rate=16.0,
                temperature=37.0, spo2=97.0)


@pytest.fixture(autouse=True)
def _no_checkpoint(monkeypatch):
    """Today's shipping state, made explicit: no CfC checkpoint loaded."""
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", lambda _pid: None)


@pytest.fixture(autouse=True)
def _no_feedback_suppression():
    """Another test's dismissals must not quieten a warning raised here."""
    with db_session() as db:
        db.execute("DELETE FROM insight_feedback WHERE pattern LIKE 'deterioration:%'")


def _ward():
    """A doctor alone in a fresh department, so no other test's patients appear."""
    dept = f"Insight Vitals {uuid.uuid4().hex[:8]}"
    uid = f"IVU{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash, department) "
            "VALUES (?, 'Insight Vitals Doctor', 'doctor', 'x', ?)",
            (uid, dept),
        )
    return dept, uid, {"Authorization": f"Bearer {create_token(uid, 'doctor')}"}


def _patient(dept, doctor, *, on_scale_2=False):
    pid = f"IVP{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute(
            """INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis,
                                     status, admission_date, on_scale_2, attending_doctor_id)
               VALUES (?, ?, ?, ?, ?, 60, 'M', 'Insight test', 'admitted', ?, ?, ?)""",
            (pid, f"Insight {pid}", f"MRN-{pid}", f"IV-{pid[-4:]}", dept, EARLIER,
             1 if on_scale_2 else 0, doctor),
        )
        db.execute(
            "INSERT INTO admissions (patient_id, department, ward, bed, status, admitted_by) "
            "VALUES (?, ?, ?, ?, 'active', ?)",
            (pid, dept, dept, f"IV-{pid[-4:]}", doctor),
        )
    return pid


def _one(on_scale_2=False):
    dept, uid, _ = _ward()
    return _patient(dept, uid, on_scale_2=on_scale_2)


def _vitals(pid, v, *, ts=TS, consciousness="alert", on_oxygen=0):
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals
                   (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                    spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                    source)
               VALUES (?, ?, ?, ?, 75, ?, ?, ?, ?, ?, 'simulated')""",
            (pid, ts, v["heart_rate"], v["systolic_bp"], v["spo2"],
             v["temperature"], v["respiratory_rate"], consciousness, on_oxygen),
        )


def _risk_row(pid, *, score, level, ts=TS):
    """A stored risk row whose drivers match none of the vitals in this file."""
    with db_session() as db:
        db.execute(
            "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
            "VALUES (?, ?, ?, ?, '[\"Mild tachycardia\"]')",
            (pid, ts, score, level),
        )


def _card(pid, kind="critical"):
    """The ACTIVE Rule 1 card for this patient, or None."""
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM sage_insights WHERE patient_id = ? AND state = 'active' "
            "AND dedup_key = ?",
            (pid, f"{pid}:deterioration:{kind}"),
        ).fetchall()
    assert len(rows) <= 1, rows
    return dict(rows[0]) if rows else None


def _age_card(pid, kind, minutes):
    """Pretend the card was last stamped `minutes` ago (the auto-resolve clock)."""
    old = format_utc(datetime.now(timezone.utc) - timedelta(minutes=minutes))
    with db_session() as db:
        db.execute(
            "UPDATE sage_insights SET updated_at = ? WHERE dedup_key = ? AND state = 'active'",
            (old, f"{pid}:deterioration:{kind}"),
        )


def _score(v, on_scale_2=False):
    return vigil_service.compute_news2(
        v["heart_rate"], v["systolic_bp"], v["respiratory_rate"], v["temperature"],
        v["spo2"], "alert", False, on_scale_2=on_scale_2)


def _no_action(card):
    assert card["suggested_action_type"] is None, card
    assert card["suggested_action_target"] is None, card
    assert card["suggested_action_type"] not in NON_ACTIONABLE_ACTION_TYPES


# ── 1. The band follows the latest vitals, not a stale risk row ──────────────

def test_a_stale_critical_risk_row_over_benign_vitals_raises_nothing():
    """The Rajesh Iyer case: the newest risk row says 'critical', the newest
    vitals score NEWS2 0. The board says stable; the feed must not say critical."""
    pid = _one()
    _vitals(pid, CRITICAL_9, ts=EARLIER)
    _risk_row(pid, score=0.9, level="critical")
    _vitals(pid, BENIGN_0)

    evaluate_patient(pid)
    assert _card(pid) is None
    assert _card(pid, "elevated") is None


def test_critical_vitals_raise_the_card_over_a_stale_elevated_risk_row():
    """The other direction: newest risk row 'elevated', newest vitals NEWS2 9."""
    pid = _one()
    _risk_row(pid, score=0.3, level="elevated", ts=EARLIER)
    _vitals(pid, CRITICAL_9)

    assert evaluate_patient(pid)
    card = _card(pid)
    assert card is not None
    assert card["severity"] == "critical"
    assert card["title"].endswith(" is critical")
    _no_action(card)


def test_a_risk_row_with_no_vitals_behind_it_raises_nothing():
    """The board shows such a patient as 'not recorded', in no tile."""
    pid = _one()
    _risk_row(pid, score=0.9, level="critical")
    evaluate_patient(pid)
    assert _card(pid) is None


def test_feed_critical_set_equals_the_board_critical_rows():
    """On one ward: every patient with an active critical deterioration card is
    a board row with risk_level 'critical', and the other way round."""
    dept, uid, headers = _ward()
    a = _patient(dept, uid)                 # critical vitals, stale elevated risk
    _risk_row(a, score=0.3, level="elevated", ts=EARLIER)
    _vitals(a, CRITICAL_9)
    b = _patient(dept, uid)                 # benign vitals, stale critical risk
    _vitals(b, CRITICAL_9, ts=EARLIER)
    _risk_row(b, score=0.9, level="critical")
    _vitals(b, BENIGN_0)
    c = _patient(dept, uid)                 # single parameter 3: elevated
    _vitals(c, SINGLE3_3)
    d = _patient(dept, uid)                 # risk row, no vitals: not recorded
    _risk_row(d, score=0.9, level="critical")
    e = _patient(dept, uid)                 # timestamp tie: the correction is critical
    _vitals(e, BENIGN_0)
    _vitals(e, CRITICAL_9)
    f = _patient(dept, uid, on_scale_2=True)  # SpO2 87 on air: 1 point on Scale 2
    _risk_row(f, score=0.9, level="critical")
    _vitals(f, dict(BENIGN_0, respiratory_rate=22.0, heart_rate=96.0, spo2=87.0))
    ward = (a, b, c, d, e, f)

    for pid in ward:
        evaluate_patient(pid)

    r = client.get("/api/vigil/patients", headers=headers, params={"limit": 200})
    assert r.status_code == 200, r.text
    board = {row["id"]: row for row in r.json()}
    assert set(board) == set(ward)
    board_critical = {pid for pid, row in board.items() if row["risk_level"] == "critical"}
    feed_critical = {pid for pid in ward if _card(pid)}
    assert board_critical == {a, e}
    assert feed_critical == board_critical


# ── 2. The text is the NEWS2 label and the Relay reason line ─────────────────

def test_the_critical_detail_is_the_news2_label_and_the_relay_reason_line():
    pid = _one()
    _vitals(pid, CRITICAL_9)
    evaluate_patient(pid)

    reason = vigil_service.news2_reason_line(_score(CRITICAL_9))
    assert _card(pid)["detail"] == (
        f"NEWS2 9 (critical) · a single parameter scores 3. {reason}. "
        "Recommend immediate senior review.")
    assert "Mild tachycardia" not in _card(pid)["detail"]


def test_an_incomplete_critical_set_says_it_is_a_minimum():
    """No BP, no ACVPU, no O2 documentation: a floor of 8 is still critical."""
    pid = _one()
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, spo2, temperature, "
            "respiratory_rate) VALUES (?, ?, 118, 93, 38.9, 26)",
            (pid, TS),
        )
    evaluate_patient(pid)
    card = _card(pid)
    assert card["detail"].startswith(
        "NEWS2 8 (critical) · a single parameter scores 3 · incomplete set, minimum. ")


def test_a_reason_line_failure_still_raises_the_card(monkeypatch):
    """As ruling G2 does for the Relay alert lanes: wording never drops an
    alert. The line is left out; the label already states the band."""
    def _boom(*_a, **_k):
        raise RuntimeError("wording failure")
    monkeypatch.setattr(vigil_service, "news2_reason_line", _boom)
    import services.sage_proactive_service as sps
    monkeypatch.setattr(sps, "news2_reason_line", _boom)

    pid = _one()
    _vitals(pid, CRITICAL_9)
    evaluate_patient(pid)
    card = _card(pid)
    assert card is not None
    assert card["detail"] == ("NEWS2 9 (critical) · a single parameter scores 3. "
                              "Recommend immediate senior review.")


# ── 3. "Rose from" compares against the newest row >= 15 minutes older ──────

def test_a_rise_raises_the_deteriorating_card_with_both_scores():
    pid = _one()
    _vitals(pid, BENIGN_0, ts=_at(minutes=30))
    _vitals(pid, SINGLE3_3)

    assert evaluate_patient(pid)
    card = _card(pid, "elevated")
    assert card["severity"] == "warning"
    assert card["title"].endswith(" is deteriorating")
    reason = vigil_service.news2_reason_line(_score(SINGLE3_3))
    assert card["detail"] == (
        "Rose from NEWS2 0 (stable) to NEWS2 3 (elevated) · a single parameter scores 3. "
        f"{reason}. Consider review.")
    _no_action(card)


def test_a_rise_at_simulator_cadence_is_not_missed():
    """Rows every 5 s. The rise happened one row ago, so the PREVIOUS row is
    already elevated and a previous-row compare raises nothing; 15 minutes back
    the patient was stable, which is the rise the clinician needs to see."""
    pid = _one()
    for s in range(20 * 60, 10, -5):        # stable from 20 min ago to 15 s ago
        _vitals(pid, BENIGN_0, ts=_at(seconds=s))
    _vitals(pid, SINGLE3_3, ts=_at(seconds=5))
    _vitals(pid, SINGLE3_3)

    evaluate_patient(pid)
    card = _card(pid, "elevated")
    assert card is not None
    assert card["detail"].startswith("Rose from NEWS2 0 (stable) to NEWS2 3 (elevated)")


def test_a_patient_elevated_for_the_whole_window_has_not_risen():
    """Stable 2 h ago, elevated for the last 20 minutes: the reference row (the
    newest at least 15 minutes older) is elevated, so no new rise is raised."""
    pid = _one()
    _vitals(pid, BENIGN_0, ts=_at(minutes=120))
    _vitals(pid, SINGLE3_3, ts=_at(minutes=20))
    _vitals(pid, SINGLE3_3, ts=_at(seconds=5))
    _vitals(pid, SINGLE3_3)

    evaluate_patient(pid)
    assert _card(pid, "elevated") is None


def test_no_row_old_enough_to_compare_raises_no_rise():
    pid = _one()
    _vitals(pid, BENIGN_0, ts=_at(minutes=10))
    _vitals(pid, SINGLE3_3)
    evaluate_patient(pid)
    assert _card(pid, "elevated") is None


# ── 4. Recovery: rewrite to "was critical" at once, resolve 10 minutes later ─

def test_a_complete_benign_set_stands_the_critical_card_down():
    pid = _one()
    _vitals(pid, CRITICAL_9, ts=_at(minutes=1))
    evaluate_patient(pid)
    raised = _card(pid)
    assert raised["title"].endswith(" is critical")

    _vitals(pid, BENIGN_0)
    assert evaluate_patient(pid) == []
    down = _card(pid)
    assert down["id"] == raised["id"]
    assert down["title"] == raised["title"].replace(" is critical", " was critical")
    assert down["detail"] == "No longer critical: latest observation NEWS2 0 (stable)."
    assert down["severity"] == "info", "a blue dot: no longer an active alert"
    _no_action(down)

    # A second sweep does not restamp it, so the 10-minute clock keeps running.
    evaluate_patient(pid)
    assert _card(pid)["updated_at"] == down["updated_at"]

    _age_card(pid, "critical", minutes=11)
    evaluate_patient(pid)
    assert _card(pid) is None
    with db_session() as db:
        state = db.execute("SELECT state FROM sage_insights WHERE id = ?",
                           (raised["id"],)).fetchone()["state"]
    assert state == "auto_resolved"


def test_dropping_to_elevated_stands_the_critical_card_down_too():
    pid = _one()
    _vitals(pid, CRITICAL_9, ts=_at(minutes=1))
    evaluate_patient(pid)
    _vitals(pid, SINGLE3_3)
    evaluate_patient(pid)
    card = _card(pid)
    assert card["title"].endswith(" was critical")
    assert card["severity"] == "info"
    assert card["detail"] == ("No longer critical: latest observation NEWS2 3 (elevated) "
                              "· a single parameter scores 3.")


def test_going_critical_again_flips_the_same_card_back():
    pid = _one()
    _vitals(pid, CRITICAL_9, ts=_at(minutes=2))
    evaluate_patient(pid)
    raised = _card(pid)
    _vitals(pid, BENIGN_0, ts=_at(minutes=1))
    evaluate_patient(pid)
    assert _card(pid)["title"].endswith(" was critical")

    _vitals(pid, CRITICAL_9)
    assert evaluate_patient(pid) == [], "the same card, not a second one"
    back = _card(pid)
    assert back["id"] == raised["id"]
    assert back["title"] == raised["title"]
    assert back["detail"].startswith("NEWS2 9 (critical)")
    assert back["severity"] == "critical"


def test_the_deteriorating_card_stands_down_when_stable_again():
    pid = _one()
    _vitals(pid, BENIGN_0, ts=_at(minutes=30))
    _vitals(pid, SINGLE3_3, ts=_at(minutes=1))
    evaluate_patient(pid)
    raised = _card(pid, "elevated")
    assert raised["title"].endswith(" is deteriorating")

    _vitals(pid, BENIGN_0)
    evaluate_patient(pid)
    down = _card(pid, "elevated")
    assert down["id"] == raised["id"]
    assert down["title"].endswith(" was deteriorating")
    assert down["severity"] == "info"
    assert down["detail"] == "No longer elevated: latest observation NEWS2 0 (stable)."


# ── 5. An incomplete set below critical is not recovery ──────────────────────

def _incomplete(pid, *, ts=TS, hr=80.0, sbp=120.0, spo2=97.0, temp=37.0, rr=16.0,
                consciousness="alert", on_oxygen=0):
    """A vitals row where a None is stored as NULL ("not measured")."""
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                   diastolic_bp, spo2, temperature, respiratory_rate, consciousness,
                   on_oxygen, source)
               VALUES (?, ?, ?, ?, 75, ?, ?, ?, ?, ?, 'simulated')""",
            (pid, ts, hr, sbp, spo2, temp, rr, consciousness, on_oxygen),
        )


def test_an_incomplete_set_below_critical_does_not_stand_the_card_down():
    """HR 112 (2) + SpO2 94 (1) + Temp 38.5 (1) = 4 with RR not measured: an RR
    of 25 or more would make it 7. NEWS2 is a minimum, so it cannot show the
    patient recovered. The card stays 'is critical', says why, and stays put."""
    pid = _one()
    _vitals(pid, CRITICAL_9, ts=_at(minutes=1))
    evaluate_patient(pid)
    raised = _card(pid)

    _incomplete(pid, hr=112.0, spo2=94.0, temp=38.5, rr=None)
    evaluate_patient(pid)
    card = _card(pid)
    assert card["id"] == raised["id"]
    assert card["title"] == raised["title"]
    assert card["severity"] == "critical"
    assert card["detail"] == ("Not confirmed recovered: NEWS2 4 (stable) · incomplete set, "
                              "minimum; not measured: Respiratory rate.")

    _age_card(pid, "critical", minutes=11)
    evaluate_patient(pid)
    assert _card(pid) is not None, "still not confirmed, so still on the feed"


def test_a_complete_set_after_an_incomplete_one_stands_it_down():
    pid = _one()
    _vitals(pid, CRITICAL_9, ts=_at(minutes=2))
    evaluate_patient(pid)
    # HR 112 (2) + SpO2 94 (1) + RR 21 (2) = 5, temperature not measured: a
    # temperature of 35.0 or below would make it 8. (Leaving ACVPU or O2 out of
    # the INSERT would not make a set incomplete: the column defaults store
    # 'alert' and 0, which score as assessed. Pass NULL.)
    _incomplete(pid, ts=_at(minutes=1), hr=112.0, spo2=94.0, rr=21.0, temp=None)
    evaluate_patient(pid)
    assert _card(pid)["detail"] == ("Not confirmed recovered: NEWS2 5 (elevated) · incomplete "
                                    "set, minimum; not measured: Temperature.")

    _vitals(pid, BENIGN_0)
    evaluate_patient(pid)
    assert _card(pid)["title"].endswith(" was critical")


def test_an_incomplete_set_that_cannot_reach_critical_stands_it_down():
    """RR not measured on an otherwise 0-point set: RR at its worst adds 3, so
    the most this set can be is NEWS2 3. It shows the patient is not critical."""
    pid = _one()
    _vitals(pid, CRITICAL_9, ts=_at(minutes=1))
    evaluate_patient(pid)
    _incomplete(pid, rr=None)
    evaluate_patient(pid)
    card = _card(pid)
    assert card["title"].endswith(" was critical")
    assert card["severity"] == "info"
    assert card["detail"] == (
        "No longer critical: latest observation NEWS2 0 (stable) · incomplete set, minimum; "
        "not measured: Respiratory rate, which could not bring it to critical.")


def test_a_monitor_only_patient_is_stood_down_when_benign():
    """A monitor never sends ACVPU or O2. With every vital in range the worst
    they could add is 3 + 2 = 5, never 7, so the card clears (it used to stay
    for good on a monitor-only patient)."""
    pid = _one()
    _vitals(pid, CRITICAL_9, ts=_at(minutes=1))
    evaluate_patient(pid)
    _incomplete(pid, consciousness=None, on_oxygen=None)
    evaluate_patient(pid)
    card = _card(pid)
    assert card["title"].endswith(" was critical")
    assert card["detail"] == (
        "No longer critical: latest observation NEWS2 0 (stable) · incomplete set, minimum; "
        "not measured: Consciousness (ACVPU), Supplemental O2, which could not bring it to "
        "critical.")
    _age_card(pid, "critical", minutes=11)
    evaluate_patient(pid)
    assert _card(pid) is None


@pytest.mark.parametrize("on_scale_2, stays", [(True, True), (False, False)])
def test_an_unrecorded_o2_is_tried_both_ways_on_scale_2(on_scale_2, stays):
    """HR 115 (2), SpO2 97, O2 not recorded. On air that is 2 on either scale.
    On oxygen, Scale 2 scores SpO2 97 as over-oxygenation (3) plus O2 (2): 7,
    critical. Scale 1 only adds the 2 for O2: 4. So only Scale 2 stays open."""
    pid = _one(on_scale_2=on_scale_2)
    # Critical on either scale: HR 135 (3) + RR 26 (3) + SBP 88 (3) = 9.
    _vitals(pid, dict(CRITICAL_9, systolic_bp=88.0, spo2=97.0), ts=_at(minutes=1))
    evaluate_patient(pid)
    assert _card(pid) is not None
    _incomplete(pid, hr=115.0, on_oxygen=None)
    evaluate_patient(pid)
    card = _card(pid)
    assert card["title"].endswith(" is critical" if stays else " was critical"), card


def test_the_deteriorating_card_stays_open_on_an_unassessed_acvpu():
    """Any one parameter at 3 makes a patient elevated, and an unassessed ACVPU
    could be new confusion: such a set cannot clear an elevated card."""
    pid = _one()
    _vitals(pid, BENIGN_0, ts=_at(minutes=30))
    _vitals(pid, SINGLE3_3, ts=_at(minutes=1))
    evaluate_patient(pid)
    raised = _card(pid, "elevated")
    _incomplete(pid, consciousness=None)
    evaluate_patient(pid)
    card = _card(pid, "elevated")
    assert card["id"] == raised["id"]
    assert card["title"].endswith(" is deteriorating")
    assert card["detail"] == ("Not confirmed recovered: NEWS2 0 (stable) · incomplete set, "
                              "minimum; not measured: Consciousness (ACVPU).")


# ── 6. A card put back up is pushed, not left for the poll ──────────────────

def _stood_down_then_critical(pid):
    _vitals(pid, CRITICAL_9, ts=_at(minutes=2))
    evaluate_patient(pid)
    _vitals(pid, BENIGN_0, ts=_at(minutes=1))
    evaluate_patient(pid)
    assert _card(pid)["severity"] == "info"
    _vitals(pid, CRITICAL_9)


def test_a_card_put_back_up_is_reported_for_the_push():
    pid = _one()
    _stood_down_then_critical(pid)
    raised_again = []
    assert evaluate_patient(pid, raised_again) == []
    assert raised_again == [_card(pid)["id"]]

    again = []
    evaluate_patient(pid, again)
    assert again == [], "only the sweep that puts it back up reports it"


def test_the_sweep_passes_raised_again_through():
    from services.sage_proactive_service import evaluate_all_admitted
    pid = _one()
    _stood_down_then_critical(pid)
    raised_again = []
    evaluate_all_admitted(raised_again)
    assert _card(pid)["id"] in raised_again


def test_the_evaluate_route_pushes_a_card_put_back_up(monkeypatch):
    import routes.sage_proactive_routes as routes_mod
    emitted = []

    async def _emit(event, data=None, **_kw):
        emitted.append((event, data))
    monkeypatch.setattr(routes_mod.sio, "emit", _emit)

    dept, uid, headers = _ward()
    pid = _patient(dept, uid)
    _stood_down_then_critical(pid)
    r = client.post("/api/sage/insights/evaluate", headers=headers, json={"patient_id": pid})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 0
    assert r.json()["raised_again"] == [_card(pid)["id"]]
    assert ("sage:insight", {"event": "new", "count": 0, "raised_again": 1}) in emitted


# ── 7. The CfC overlay is the board's overlay ────────────────────────────────

def test_an_overlay_escalation_raises_the_card_and_says_so(monkeypatch):
    """With a checkpoint loaded the overlay can make a NEWS2-stable patient
    critical. The board shows critical, so the feed does, and the detail must
    say the band came from the overlay, not the RCP chart."""
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", lambda _pid: {
        "outcomes": {}, "primary_risk": 0.95, "seq_len": 12})
    dept, uid, headers = _ward()
    pid = _patient(dept, uid)
    _vitals(pid, BENIGN_0)

    evaluate_patient(pid)
    card = _card(pid)
    assert card is not None
    assert card["detail"] == (
        "NEWS2 0 (stable), raised to critical by the CfC overlay. "
        "CfC: high near-term deterioration risk; All vitals within normal range. "
        "Recommend immediate senior review.")

    r = client.get("/api/vigil/patients", headers=headers)
    (row,) = [x for x in r.json() if x["id"] == pid]
    assert row["risk_level"] == "critical"
