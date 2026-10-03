"""Orb — shadow-mode scorecard: fixture-driven arithmetic.

Every test here corresponds to a way this instrument could LIE to a hospital
CIO. Known episodes go in; hand-computed numbers come out. Nothing is asserted
against a value the code produced — the expectations below were computed by hand
from the fixture and are written as literals.

The fixtures live in the throwaway test DB (`conftest` points ORB_DB_PATH at a
temp file before `config` is first imported), never the live appliance DB. Each
test starts from a freshly recreated `shadow_events` — recreated by DROPping and
re-running migration 4, because `trg_shadow_no_delete` makes non-simulator rows
undeletable, which is the property that makes the export evidentiary.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

import migrations
from database import db_session, init_db
from services import shadow_scorecard_service as sc
from services import shadow_service

# ── The fixture clock ────────────────────────────────────────────────────────
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
DAYS = 3
START = NOW - timedelta(days=DAYS)          # 2026-07-22 12:00Z
WARD = "Ward SC"

#: One 24-hour "shift" per day makes coverage hand-countable: the 3-day window
#: contains exactly four ward-shift periods (12 h + 24 h + 24 h + 12 h = 72 h).
ONE_DAY_SHIFT = "00:00-24:00"

D22 = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
D23 = datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc)
D24 = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
D25 = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db()


def _wipe_shadow_events():
    """Actually give back a virgin `shadow_events`.

    Rows CANNOT simply be DELETEd: trg_shadow_no_delete makes every
    non-simulator row immutable, which is the property the instrument depends
    on. So the table, its view and its triggers are dropped and migration 4 is
    re-run to rebuild them exactly as production has them.

    This exists because the fixture below CLAIMED a virgin instrument without
    delivering one: it wiped nothing, so every test here ran against whatever
    test_shadow_routes.py and test_shadow_clinical_lanes.py had already written.
    Every test in this file scopes its assertions to WARD, so the lie was
    invisible — but a fixture whose docstring is not true of the database is one
    ward-less query away from a failure nobody can localise, and
    `test_empty_dataset_every_metric_is_explicitly_insufficient` is that query.

    Note what this does NOT fix, because the distinction cost a full suite run
    to find: a ward-less scorecard also reads the CENSUS (`patients`), which no
    wipe of `shadow_events` touches. See the `empty_appliance` fixture below.
    """
    with db_session() as db:
        db.execute("DROP TRIGGER IF EXISTS trg_shadow_no_update")
        db.execute("DROP TRIGGER IF EXISTS trg_shadow_no_delete")
        db.execute("DROP VIEW IF EXISTS shadow_events_current")
        db.execute("DROP TABLE IF EXISTS shadow_events")
        migrations._m4_shadow_events(db)
        migrations._m18_shadow_none_confirmed_once(db)


@pytest.fixture
def empty_appliance(tmp_path):
    """A REAL Orb database with NOTHING in it — the state the pilot starts in.

    A virgin `shadow_events` is not enough for the one test in this file that
    asks for the APPLIANCE-WIDE scorecard (`ward=None`). With no ward filter the
    scorecard takes its ward list — and therefore its coverage denominator —
    from the CENSUS (`shadow_scorecard_service._census_wards` reads `patients`),
    not from the instrument. The session DB is shared by the whole suite, dozens
    of whose tests insert an admitted patient with no `admission_date`, and an
    undated admitted patient is assumed to span ANY window. So a full-suite run
    saw `coverage.total == 12` (3 wards × 4 windows) on a dataset with zero
    events, while the shadow files alone left a census this window happened to
    miss: green on the file, red on the suite, and nothing to do with the
    instrument the fixture had just wiped.

    Rather than assert emptiness against a database 1,600 other tests have
    written to, the empty appliance is built for real — `init_db()` against a
    throwaway path: same schema, same migrations, same append-only triggers,
    zero rows. `database.DB_PATH` is restored before the test body runs (the
    idiom tests/test_db_encryption.py uses), so only the yielded connection
    points at the throwaway file and nothing else in the session is redirected.
    """
    import database
    from db_backend import discard_pooled

    original = database.DB_PATH
    database.DB_PATH = tmp_path / "orb_test_empty_appliance.db"
    try:
        database.init_db()
        # init_db goes through db_session(), which under ORB_DB_POOL caches a
        # handle per (thread, path); retire it so the pool is not left holding a
        # connection to a temp file for the rest of the session.
        discard_pooled(database.get_db())
        conn = database._build_connection()
    finally:
        database.DB_PATH = original
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """A virgin instrument, a deterministic clock config, and zeroed counters."""
    _wipe_shadow_events()
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "true")
    monkeypatch.setenv("ORB_SCORECARD_SHIFTS", ONE_DAY_SHIFT)
    # Shifts are laid on the hospital's clock (F025). The hand-counted windows
    # in this file are UTC days, so the hospital is pinned to UTC here; the
    # zone itself is tested in test_hospital_time_surfaces.py.
    from services.vitals_source import hl7v2
    monkeypatch.setattr(hl7v2, "_default_tz", timezone.utc)
    monkeypatch.setattr(hl7v2, "_default_tz_resolved", True)
    monkeypatch.delenv("ORB_SHADOW_PHASE_SCHEDULE", raising=False)
    monkeypatch.delenv("ORB_SHADOW_PHASE", raising=False)
    monkeypatch.delenv("ORB_SHADOW_SCORECARD_INCLUDE_SIMULATOR", raising=False)
    monkeypatch.delenv("ORB_SCORECARD_MANUAL_LOOKUP_SECONDS", raising=False)
    for key in list(shadow_service.STATS):
        monkeypatch.setitem(shadow_service.STATS, key, 0)
    shadow_service.reset_registry()
    with db_session() as conn:
        conn.execute("DROP VIEW IF EXISTS shadow_events_current")
        conn.execute("DROP TABLE IF EXISTS shadow_events")
        migrations._m4_shadow_events(conn)
        migrations._m18_shadow_none_confirmed_once(conn)
        conn.execute("DELETE FROM patients WHERE id LIKE 'sc\\_%' ESCAPE '\\'")
    yield
    with db_session() as conn:
        conn.execute("DROP VIEW IF EXISTS shadow_events_current")
        conn.execute("DROP TABLE IF EXISTS shadow_events")
        migrations._m4_shadow_events(conn)
        migrations._m18_shadow_none_confirmed_once(conn)
        conn.execute("DELETE FROM patients WHERE id LIKE 'sc\\_%' ESCAPE '\\'")


# ── Fixture builders ─────────────────────────────────────────────────────────
def _emit(conn, kind, episode_id, when, *, lane="ingestion", patient_id=None,
          payload=None, ward=WARD, **kwargs):
    """Write one shadow row through the real write path and INSIST it landed.

    shadow_service swallows its own exceptions by design (a clinical action must
    never fail because the log did), so a test that does not assert on the
    returned id can silently measure an empty table.
    """
    row_id = shadow_service.emit(
        kind, episode_id, lane, occurred_at=sc._iso(when), patient_id=patient_id,
        ward=ward, payload=payload, db=conn, **kwargs)
    assert row_id is not None, f"fixture row refused: {kind} / {episode_id}"
    return row_id


def _patients(conn, n=1, ward=WARD, admitted=None):
    """Census for the bed-day denominator."""
    admitted = admitted or (START - timedelta(days=30))
    for i in range(1, n + 1):
        conn.execute(
            "INSERT OR REPLACE INTO patients (id, name, mrn, ward, admission_date, status) "
            "VALUES (?, ?, ?, ?, ?, 'admitted')",
            (f"sc_p{i}", f"SC Patient {i}", f"SC-MRN-{i}", ward, sc._iso(admitted)),
        )


def _cover(conn, start=START, end=NOW, ward=WARD, at=None):
    """A charge nurse confirming 'no deterioration this shift' over a span —
    the row that turns silence into evidence."""
    _emit(conn, "outcome.none_confirmed", "ep_coverage", at or (start + timedelta(minutes=1)),
          lane="manual", ward=ward,
          payload={"ward": ward, "period_start": sc._iso(start),
                   "period_end": sc._iso(end), "shift": "all"})


def _card(**kwargs):
    kwargs.setdefault("days", DAYS)
    kwargs.setdefault("ward", WARD)
    kwargs.setdefault("now", NOW)
    return sc.scorecard(**kwargs)


# ════════════════════════════════════════════════════════════════════════════
#  1. The empty appliance — the state the pilot STARTS in
# ════════════════════════════════════════════════════════════════════════════
def test_empty_dataset_every_metric_is_explicitly_insufficient(empty_appliance):
    """No ZeroDivisionError, no bare 0 %, no invented number. Morning one."""
    card = sc.scorecard(days=DAYS, ward=None, now=NOW, db=empty_appliance)

    assert card["metrics"].keys() == set(sc.METRIC_IDS)
    for metric_id, metric in card["metrics"].items():
        assert metric["state"] != "reported", metric_id
        assert metric["value"] is None, metric_id
        assert metric["display"] is None, metric_id
        assert metric["n_observed"] == 0, metric_id
        # A denominator is still stated, even when it is zero.
        assert "denominator" in metric and "denominator_label" in metric

    lead = card["metrics"]["detection_lead_time"]
    assert lead["state"] == "not_computable"
    assert lead["blockers"], "lead time must name its missing prerequisite"

    assert card["coverage"]["total"] == 0
    assert card["coverage"]["display"] == "0/0"
    assert card["bed_days"]["bed_days"] == 0.0
    assert card["instrument"]["events_total"] == 0
    assert card["instrument"]["append_only"] is True
    assert all(item["met"] is False for item in card["readiness"]
               if item["id"] in ("clinical_data", "outcome_capture", "adjudication"))


def test_empty_dataset_drilldowns_do_not_raise():
    assert sc.episode_detail("ep_does_not_exist") is None
    index = sc.list_episodes(days=DAYS, ward=WARD, now=NOW)
    assert index["episodes"] == [] and index["total"] == 0
    health = sc.instrument_health()
    assert health["events_total"] == 0 and health["present"] is True


# ════════════════════════════════════════════════════════════════════════════
#  2. Detection lead time — median/IQR, a negative lead, and a miss
# ════════════════════════════════════════════════════════════════════════════
def _lead_fixture(conn):
    """Five confirmed outcomes at 2026-07-24 00:00Z.

        P1  alert  -180 min  → lead +180   (witnessed)
        P2  alert   -60 min  → lead  +60   (chart_review)
        P3  alert   +45 min  → lead  -45   (witnessed)   ← clinician was ahead
        P4  alert  -300 min  → lead +300   (chart_review)
        P5  'stable' alert only → MISS                   (handover)

    leads sorted = [-45, 60, 180, 300]
      p50 = 60 + 0.5*(180-60)      = 120.0
      p25 = -45 + 0.75*(60 - -45)  =  33.75
      p75 = 180 + 0.25*(300-180)   = 210.0
    witnessed-only = [-45, 180] → p50 = -45 + 0.5*225 = 67.5
    sensitivity = 1 - 1/5 = 0.8
    """
    outcome_at = D24
    plan = [
        ("P1", -180, "critical", "witnessed"),
        ("P2", -60, "elevated", "chart_review"),
        ("P3", +45, "critical", "witnessed"),
        ("P4", -300, "elevated", "chart_review"),
        ("P5", -30, "stable", "handover"),      # 'stable' is not a flag → miss
    ]
    for pid, offset, level, source in plan:
        episode = f"ep_{pid}"
        _emit(conn, "episode.opened", episode, outcome_at - timedelta(hours=6),
              patient_id=pid, payload={"trigger": "news2_threshold"})
        _emit(conn, "alert.fired", episode, outcome_at + timedelta(minutes=offset),
              patient_id=pid, payload={"level": level, "raw_news2": 7})
        _emit(conn, "outcome.recorded", episode, outcome_at, lane="manual", patient_id=pid,
              payload={"event_type": "icu_transfer", "source": source,
                       "recognised_by": "charge_nurse", "confirmed_by_role": "doctor"})


def test_lead_time_median_iqr_and_negative_lead_unclipped(monkeypatch):
    monkeypatch.setitem(sc.MIN_N, "detection_lead_time", 4)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _lead_fixture(conn)

    metric = _card()["metrics"]["detection_lead_time"]
    detail = metric["detail"]

    assert metric["state"] == "reported"
    assert metric["value"] == 120.0
    assert metric["display"] == "2 h 00 m"
    assert metric["spread"] == {"p25": 33.75, "p50": 120.0, "p75": 210.0}

    # The negative lead survives, with its sign, and is called out.
    assert detail["negative_leads"] == 1
    assert detail["negative_lead_values_minutes"] == [-45.0]
    assert sorted(r["lead_minutes"] for r in detail["rows"] if not r["miss"]) == \
        [-45.0, 60.0, 180.0, 300.0]

    # The miss is in the denominator, not dropped.
    assert detail["outcomes"] == 5
    assert detail["leads"] == 4
    assert detail["misses"] == 1
    assert detail["sensitivity"] == 0.8
    assert metric["denominator"] == 5
    assert metric["numerator"] == 4
    assert metric["n_observed"] == 4

    # chart_review biases late, so witnessed-only is reported separately and is
    # NOT the same number.
    assert detail["witnessed_only"]["n"] == 2
    assert detail["witnessed_only"]["median_minutes"] == 67.5
    assert detail["witnessed_only"]["median_minutes"] != metric["value"]
    assert detail["by_source"]["chart_review"]["median_minutes"] == 180.0
    assert detail["by_source"]["handover"]["misses"] == 1


def test_negative_lead_is_reported_not_clipped(monkeypatch):
    """A single case where the clinician beat the machine. It must print as a
    negative number, not as zero and not as a miss."""
    monkeypatch.setitem(sc.MIN_N, "detection_lead_time", 1)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _emit(conn, "alert.fired", "ep_neg", D24 + timedelta(minutes=45), patient_id="PN",
              payload={"level": "critical"})
        _emit(conn, "outcome.recorded", "ep_neg", D24, lane="manual", patient_id="PN",
              payload={"event_type": "code_blue", "source": "witnessed"})

    metric = _card()["metrics"]["detection_lead_time"]
    assert metric["state"] == "reported"
    assert metric["value"] == -45.0
    assert metric["display"] == "-45 m"
    assert metric["detail"]["negative_leads"] == 1
    assert metric["detail"]["misses"] == 0
    assert metric["detail"]["rows"][0]["miss"] is False


def test_miss_is_counted_never_dropped(monkeypatch):
    """A confirmed deterioration Orb never flagged. The metric must get SMALLER,
    not stay silent: sensitivity 0/1 and no reportable median."""
    monkeypatch.setitem(sc.MIN_N, "detection_lead_time", 1)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _emit(conn, "outcome.recorded", "ep_miss", D24, lane="manual", patient_id="PM",
              payload={"event_type": "death", "source": "witnessed"})

    metric = _card()["metrics"]["detection_lead_time"]
    assert metric["detail"]["outcomes"] == 1
    assert metric["detail"]["misses"] == 1
    assert metric["detail"]["leads"] == 0
    assert metric["detail"]["sensitivity"] == 0.0
    assert metric["denominator"] == 1          # the miss IS the denominator
    assert metric["state"] == "insufficient_data"
    assert metric["value"] is None and metric["display"] is None


def test_alert_outside_the_lookback_window_is_a_miss(monkeypatch):
    monkeypatch.setenv("ORB_SCORECARD_LOOKBACK_HOURS", "6")
    monkeypatch.setenv("ORB_SCORECARD_LATE_WINDOW_HOURS", "6")
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _emit(conn, "alert.fired", "ep_far", D24 - timedelta(hours=20), patient_id="PF",
              payload={"level": "critical"})
        _emit(conn, "outcome.recorded", "ep_far", D24, lane="manual", patient_id="PF",
              payload={"event_type": "icu_transfer", "source": "witnessed"})

    detail = _card()["metrics"]["detection_lead_time"]["detail"]
    assert detail["misses"] == 1 and detail["leads"] == 0


# ════════════════════════════════════════════════════════════════════════════
#  3. False-alert rate — denominators, artefacts, and the 0 % trap
# ════════════════════════════════════════════════════════════════════════════
def _alert_episode(conn, episode, when, patient_id, verdict=None, verdict_at=None,
                   level="elevated"):
    _emit(conn, "episode.opened", episode, when, patient_id=patient_id,
          payload={"trigger": "news2_threshold", "level": level})
    _emit(conn, "alert.fired", episode, when, patient_id=patient_id,
          payload={"level": level, "raw_news2": 6})
    if verdict:
        _emit(conn, "adjudication.recorded", episode, verdict_at or (when + timedelta(hours=2)),
              lane="manual", patient_id=patient_id, actor_user_id="D001",
              actor_role="doctor",
              payload={"verdict": verdict, "adjudicator_role": "doctor", "blinded": True})


def test_false_alert_rate_denominator_is_bed_days_and_artefacts_are_excluded(monkeypatch):
    """1 patient x 72 covered hours = 3.0 occupied bed-days.
    5 alert episodes: 2 false, 1 true, 1 artefact (excluded), 1 unadjudicated.
    rate = 2 / 3.0 = 0.6667 per occupied bed-day."""
    monkeypatch.setitem(sc.MIN_N, "false_alert_rate", 3)
    with db_session() as conn:
        _patients(conn, n=1)
        _cover(conn)
        _alert_episode(conn, "ep_f1", D23 + timedelta(hours=1), "P1", "false_alert")
        _alert_episode(conn, "ep_f2", D23 + timedelta(hours=2), "P2", "false_alert")
        _alert_episode(conn, "ep_t1", D23 + timedelta(hours=3), "P3", "true_alert")
        _alert_episode(conn, "ep_x1", D23 + timedelta(hours=4), "P4", "artefact")
        _alert_episode(conn, "ep_u1", D23 + timedelta(hours=5), "P5")   # unadjudicated

    card = _card()
    metric = card["metrics"]["false_alert_rate"]
    detail = metric["detail"]

    assert card["bed_days"]["bed_days"] == 3.0
    assert card["coverage"]["display"] == "4/4" and card["coverage"]["complete"] is True

    assert detail["episodes"] == 5
    assert detail["adjudicated"] == 3            # artefact + unadjudicated excluded
    assert detail["false"] == 2 and detail["true"] == 1
    assert detail["excluded_artefact_or_indeterminate"] == 1
    assert detail["unadjudicated"] == 1

    assert metric["state"] == "reported"
    # One alert per episode in this fixture, so alerts and episodes coincide —
    # which is exactly why the fixture could not catch the unit mismatch. The
    # test that does is test_headline_counts_alerts_not_episodes, below.
    assert metric["numerator"] == 2
    assert metric["denominator"] == 3.0
    assert round(metric["value"], 4) == 0.6667
    assert metric["display"] == "0.67 false alerts per occupied bed-day"
    assert metric["unit"] == "false alerts per occupied bed-day"
    assert metric["denominator_label"] == "occupied bed-days (covered ward-shifts only)"

    alts = {a["label"]: a["display"] for a in metric["alternative_denominators"]}
    assert alts["episodes adjudicated false, of episodes adjudicated"] == "67% (2/3)"
    assert alts["false alerts per 100 occupied bed-days"] == "66.7"
    assert alts["false alert EPISODES per occupied bed-day (clinical concerns)"] == "0.67"
    primary = [a for a in metric["alternative_denominators"] if a["is_primary"]]
    assert len(primary) == 1 and primary[0]["display"] == "0.67"
    assert "ALERTS" in primary[0]["label"]

    assert metric["adjudication"] == {
        "adjudicated": 3, "total": 5, "adjudicators": 1, "blinded": True,
        "sampling": metric["adjudication"]["sampling"],
    }
    # Two of five episodes are still unjudged, so the figure is provisional.
    assert metric["provisional"] is True
    assert "not yet" in (metric["provisional_reason"] or "")


def test_headline_counts_alerts_not_episodes(monkeypatch):
    """THE UNIT MISMATCH. `unit` says "false alerts per occupied bed-day" and the
    definition says the denominator determines "how often a nurse is
    interrupted" — so the numerator must be ALERTS. It used to be EPISODES.

    Live pilot-shaped data re-fires ~49 alerts per episode, so the figure a nurse
    would read as interruptions-per-bed-day was understated by that ratio, always
    in Orb's favour. Hand-computed here: 2 false episodes carrying 5 and 3 alerts
    = 8 false alerts; 1 true episode with 4 alerts contributes nothing. 3.0
    occupied bed-days ⇒ 8 / 3.0 = 2.6667 per bed-day, NOT 2 / 3.0 = 0.6667.
    """
    monkeypatch.setitem(sc.MIN_N, "false_alert_rate", 3)
    with db_session() as conn:
        _patients(conn, n=1)
        _cover(conn)
        # (episode, n alerts, verdict)
        for episode, n_alerts, verdict in (("ep_m1", 5, "false_alert"),
                                           ("ep_m2", 3, "false_positive"),
                                           ("ep_m3", 4, "true_positive")):
            when = D23 + timedelta(hours=1)
            _emit(conn, "episode.opened", episode, when, patient_id="P1",
                  payload={"trigger": "news2_threshold", "level": "critical"})
            for j in range(n_alerts):
                _emit(conn, "alert.fired", episode, when + timedelta(minutes=j),
                      patient_id="P1", payload={"level": "critical", "raw_news2": 9})
            _emit(conn, "adjudication.recorded", episode, when + timedelta(hours=2),
                  lane="manual", patient_id="P1", actor_user_id="D001",
                  payload={"verdict": verdict, "blinded": True})

    metric = _card()["metrics"]["false_alert_rate"]
    detail = metric["detail"]

    assert detail["alerts"] == 12                 # 5 + 3 + 4, all covered
    assert detail["false_episode_count"] == 2
    assert detail["false_alert_count"] == 8       # 5 + 3 — the true episode's 4 are not
    assert detail["alerts_per_false_episode"] == 4.0

    # The headline is the ALERT figure, and it is the LARGER of the two.
    assert metric["numerator"] == 8
    assert round(metric["value"], 4) == 2.6667
    assert metric["display"] == "2.67 false alerts per occupied bed-day"
    assert round(detail["false_episodes_per_bed_day"], 4) == 0.6667
    assert metric["value"] > detail["false_episodes_per_bed_day"]

    # The per-episode figure is reported, clearly labelled, and NOT primary.
    alts = {a["label"]: a for a in metric["alternative_denominators"]}
    episodes_alt = alts["false alert EPISODES per occupied bed-day (clinical concerns)"]
    assert episodes_alt["display"] == "0.67" and episodes_alt["is_primary"] is False
    assert alts["false ALERTS per occupied bed-day (interruptions, primary)"]["is_primary"] is True
    assert alts["alerts per false episode (why the two figures differ)"]["display"] == "4.00"

    # n_observed is the ADJUDICATION sample (episodes), not the alert count —
    # the floor gates how much human judgement is behind the figure.
    assert metric["n_observed"] == 3


def test_both_vocabularies_count_and_an_unknown_one_is_surfaced_not_guessed(monkeypatch):
    """Rule 10. The route's spelling and the historical spelling are the SAME
    verdict; a spelling in neither is reported, never bucketed.

    Before the vocabularies were unified this test's first two episodes counted
    as `unrecognised` and the metric could not report at all.
    """
    monkeypatch.setitem(sc.MIN_N, "false_alert_rate", 1)
    with db_session() as conn:
        _patients(conn, n=1)
        _cover(conn)
        _alert_episode(conn, "ep_v1", D23 + timedelta(hours=1), "P1", "false_positive")
        _alert_episode(conn, "ep_v2", D23 + timedelta(hours=2), "P2", "false_alert")
        _alert_episode(conn, "ep_v3", D23 + timedelta(hours=3), "P3", "TRUE_POSITIVE")
        _alert_episode(conn, "ep_v4", D23 + timedelta(hours=4), "P4", "probably fine")

    metric = _card()["metrics"]["false_alert_rate"]
    detail = metric["detail"]
    assert detail["false"] == 2, "the two spellings of 'false positive' must agree"
    assert detail["true"] == 1, "case and canonical spelling must both classify"
    assert detail["unrecognised_verdicts"] == 1
    # Neither term absorbed it — 2 false, not 3; adjudicated 3, not 4.
    assert metric["numerator"] == 2 and detail["adjudicated"] == 3
    # And the reader is told, on the artefact itself.
    assert metric["provisional"] is True
    assert "outside the controlled vocabulary" in metric["provisional_reason"]
    assert "does not recognise" in metric["caveat"]


def test_adjudication_after_the_window_still_counts():
    """Adjudication happens weekly, after the fact. An episode inside the window
    adjudicated after it closes must not read as unadjudicated."""
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _alert_episode(conn, "ep_late", D23, "P1", "false_alert",
                       verdict_at=NOW + timedelta(days=2))

    detail = _card()["metrics"]["false_alert_rate"]["detail"]
    assert detail["adjudicated"] == 1 and detail["false"] == 1


def test_four_alerts_and_no_judgement_never_prints_zero_percent():
    """THE worst failure mode: a scorecard announcing '0% false alerts' on its
    first morning, having seen four alerts and adjudicated none."""
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        for i in range(4):
            _alert_episode(conn, f"ep_n{i}", D23 + timedelta(hours=i), f"P{i}")

    metric = _card()["metrics"]["false_alert_rate"]
    assert metric["state"] == "unadjudicated"
    assert metric["value"] is None and metric["display"] is None
    assert metric["detail"]["episodes"] == 4
    assert metric["adjudication"]["adjudicated"] == 0


def test_adjudicated_below_the_floor_is_insufficient_not_reported(monkeypatch):
    monkeypatch.setitem(sc.MIN_N, "false_alert_rate", 10)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        for i in range(4):
            _alert_episode(conn, f"ep_s{i}", D23 + timedelta(hours=i), f"P{i}", "true_alert")

    metric = _card()["metrics"]["false_alert_rate"]
    assert metric["n_observed"] == 4 and metric["n_required"] == 10
    assert metric["state"] == "insufficient_data"
    assert metric["value"] is None and metric["display"] is None
    # The withheld figure survives for the audit trail, out of the view's reach.
    assert metric["detail"]["withheld_value"] == 0.0


# ════════════════════════════════════════════════════════════════════════════
#  4. Corrections — the view, not the base table
# ════════════════════════════════════════════════════════════════════════════
def test_superseded_adjudication_is_excluded_by_the_view(monkeypatch):
    """A revised verdict must move the metric. The original must survive in the
    base table (nothing is ever destroyed) and vanish from every number.

    The alert is corrected too, because that is the assertion that bites if a
    query ever reads `shadow_events` instead of `shadow_events_current`: a
    superseded verdict collapses harmlessly (latest wins), but a superseded
    alert DOUBLE-COUNTS, silently inflating every denominator it touches.
    """
    monkeypatch.setitem(sc.MIN_N, "false_alert_rate", 1)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        alert = _emit(conn, "alert.fired", "ep_corr", D23, patient_id="P1",
                      payload={"level": "critical", "raw_news2": 9})
        original = _emit(conn, "adjudication.recorded", "ep_corr", D23 + timedelta(hours=1),
                         lane="manual", patient_id="P1", actor_user_id="D001",
                         payload={"verdict": "true_alert", "blinded": True})

    with db_session() as conn:
        verdict_fix = shadow_service.record_correction(
            original, "champion revised the verdict on review",
            payload={"verdict": "false_alert"}, db=conn)
        alert_fix = shadow_service.record_correction(
            alert, "NEWS2 recomputed after a mis-keyed respiratory rate",
            payload={"level": "elevated", "raw_news2": 6}, db=conn)
    assert verdict_fix is not None and alert_fix is not None

    with db_session() as conn:
        base = conn.execute(
            "SELECT COUNT(*) FROM shadow_events WHERE kind IN "
            "('adjudication.recorded','alert.fired')").fetchone()[0]
        current = conn.execute(
            "SELECT kind, json_extract(payload_json,'$.verdict') AS verdict, "
            "json_extract(payload_json,'$.level') AS level FROM shadow_events_current "
            "WHERE kind IN ('adjudication.recorded','alert.fired') ORDER BY kind").fetchall()
    assert base == 4, "superseded rows must still exist in the base table"
    assert [(r["kind"], r["verdict"], r["level"]) for r in current] == [
        ("adjudication.recorded", "false_alert", None),
        ("alert.fired", None, "elevated"),
    ], "the view must show only the corrections"

    metric = _card()["metrics"]["false_alert_rate"]
    assert metric["detail"]["false"] == 1
    assert metric["detail"]["true"] == 0
    assert metric["detail"]["adjudicated"] == 1
    assert metric["detail"]["episodes"] == 1
    assert metric["detail"]["alerts"] == 1, "the superseded alert must not be counted twice"
    assert metric["numerator"] == 1

    detail = sc.episode_detail("ep_corr")
    assert detail["events_n"] == 2, "the drill-down shows current rows only"
    assert detail["corrections"] == 2
    assert [e["kind"] for e in detail["events"]] == ["alert.fired", "adjudication.recorded"]
    assert detail["events"][0]["payload"]["level"] == "elevated"
    assert detail["events"][0]["correction_reason"].startswith("NEWS2 recomputed")
    assert sc.list_episodes(days=DAYS, ward=WARD, now=NOW)["episodes"][0]["alerts"] == 1


# ════════════════════════════════════════════════════════════════════════════
#  5. Agreement — segmented by phase, NEVER pooled
# ════════════════════════════════════════════════════════════════════════════
def test_agreement_is_segmented_by_phase_and_never_pooled(monkeypatch):
    """Phase A: 1/4 = 25 %. Phase C: 4/4 = 100 %. Pooled would be 5/8 = 62.5 %,
    which is a category error — Phase C measures Orb-plus-staff."""
    monkeypatch.setenv(
        "ORB_SHADOW_PHASE_SCHEDULE",
        '[{"phase":"A","start":"2026-07-01T00:00:00+00:00"},'
        ' {"phase":"C","start":"2026-07-24T00:00:00+00:00"}]')
    monkeypatch.setitem(sc.MIN_N, "agreement", 4)

    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        # Phase A — one match out of four.
        phase_a = [
            ("ep_a1", "escalate_review", "action.approved", "escalate_review"),   # match
            ("ep_a2", "escalate_review", "action.approved", "start_antibiotics"),  # class differs
            ("ep_a3", "escalate_review", "action.approved", "no_action"),          # class differs
            ("ep_a4", "escalate_review", "action.declined", "escalate_review"),    # declined ≠ agreement
        ]
        for i, (episode, proposed, kind, taken) in enumerate(phase_a):
            at = D23 + timedelta(hours=i)
            _emit(conn, "action.proposed", episode, at, lane="chat", patient_id=f"PA{i}",
                  payload={"action_class": proposed})
            _emit(conn, kind, episode, at + timedelta(minutes=10), lane="chat",
                  patient_id=f"PA{i}", payload={"action_class": taken})
        # Phase C — four out of four.
        for i in range(4):
            episode = f"ep_c{i}"
            at = D24 + timedelta(hours=12 + i)
            _emit(conn, "action.proposed", episode, at, lane="chat", patient_id=f"PC{i}",
                  payload={"action_class": "escalate_review"})
            _emit(conn, "action.approved", episode, at + timedelta(minutes=10), lane="chat",
                  patient_id=f"PC{i}", payload={"action_class": "escalate_review"})

    metric = _card()["metrics"]["agreement"]
    detail = metric["detail"]
    segments = {s["phase"]: s for s in detail["segments"]}

    assert set(segments) == {"A", "C"}
    assert (segments["A"]["pairs"], segments["A"]["matched"]) == (4, 1)
    assert (segments["C"]["pairs"], segments["C"]["matched"]) == (4, 4)
    assert segments["A"]["rate"] == 0.25 and segments["C"]["rate"] == 1.0
    assert segments["A"]["display"] == "25% (1/4)"
    assert segments["C"]["display"] == "100% (4/4)"

    # The pooling guard: no pooled figure is computed, and the headline is ONE
    # phase's denominator — not the sum of both.
    assert detail["pooled"] is None
    assert detail["pairs_total_across_phases"] == 8
    assert detail["headline_phase"] == "A"
    assert metric["denominator"] == 4, "a pooled denominator (8) would be the lie"
    assert metric["numerator"] == 1
    assert metric["value"] == 0.25
    assert metric["value"] != pytest.approx(5 / 8), "phases must never be pooled"
    assert metric["disagreements"] == 3
    assert "Phase A" in metric["display"]
    assert metric["denominator_label"] == "proposal/action pairs in Phase A"


def test_one_clinician_action_can_resolve_only_one_proposal(monkeypatch):
    """THE ROUND-3 REGRESSION. Pairing used to be a lookup with no consumption:
    `next(a for a in actions if a._t >= proposal._t)`. One approval click
    therefore resolved EVERY open proposal in the episode, and both the numerator
    AND n were inflated by the same click.

    Three proposals, one approval: the instrument used to publish 100 % (3/3).
    The honest reading is one pair — the other two proposals drew no clinician
    action at all and are counted in NEITHER term."""
    monkeypatch.setitem(sc.MIN_N, "agreement", 1)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _emit(conn, "episode.opened", "ep_multi", D23, patient_id="P1",
              payload={"trigger": "news2_threshold", "level": "elevated"})
        for i in range(3):
            _emit(conn, "action.proposed", "ep_multi", D23 + timedelta(minutes=i),
                  lane="chat", patient_id="P1", payload={"action_class": "order_abg"})
        _emit(conn, "action.approved", "ep_multi", D23 + timedelta(minutes=30),
              lane="chat", patient_id="P1", payload={"action_class": "order_abg"})

    metric = _card()["metrics"]["agreement"]
    detail = metric["detail"]

    assert metric["denominator"] == 1, "one action cannot resolve three proposals"
    assert metric["numerator"] == 1
    assert len(detail["rows"]) == 1
    assert detail["pairs_total_across_phases"] == 1
    # The two ignored proposals are neither matched nor disagreements: they are
    # named, and excluded from both terms.
    assert detail["unresolved_proposals"] == 2
    assert detail["unresolved_by_phase"] == {"A": 2}
    assert metric["disagreements"] == 0
    segment = detail["segments"][0]
    assert (segment["pairs"], segment["matched"], segment["unresolved"]) == (1, 1, 2)
    assert segment["proposals"] == 3, "every in-window proposal is still accounted for"
    # A rate computed over one third of the proposals says so on its face.
    assert metric["provisional"] is True
    assert "1 of 3" in (metric["provisional_reason"] or "")


def test_the_n_floor_cannot_be_cleared_by_re_pairing_the_same_actions(monkeypatch):
    """The floor is the last defence, and the old bug went straight through it:
    12 proposals resolved by 4 clicks reported n = 12 and published
    "100 % (12/12)". n must count PAIRS, so four clicks are n = 4 and the metric
    stays below its stated floor of 10."""
    monkeypatch.setitem(sc.MIN_N, "agreement", 10)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        for e in range(4):
            episode = f"ep_floor{e}"
            for i in range(3):
                _emit(conn, "action.proposed", episode,
                      D23 + timedelta(hours=e, minutes=i), lane="chat",
                      patient_id="P1", payload={"action_class": "order_abg"})
            _emit(conn, "action.approved", episode, D23 + timedelta(hours=e, minutes=30),
                  lane="chat", patient_id="P1", payload={"action_class": "order_abg"})

    metric = _card()["metrics"]["agreement"]
    assert metric["n_observed"] == 4, "4 clicks are 4 pairs, not 12"
    assert metric["denominator"] == 4
    assert metric["state"] == "insufficient_data"
    assert metric["value"] is None and metric["display"] is None
    assert metric["detail"]["unresolved_proposals"] == 8


def test_an_action_resolves_the_nearest_preceding_proposal_and_is_consumed(monkeypatch):
    """The matching rule, pinned. Two proposals of DIFFERENT classes and two
    actions in one episode: each action takes the nearest preceding still-open
    proposal and is then spent.

    order_abg @ :00, escalate_review @ :01, then clinical.action escalate_review
    @ :02 (takes the NEARER proposal — a match) and approved order_abg @ :03
    (takes the one left over — also a match). Two pairs, both matched: the
    arithmetic only works if each action consumed exactly one proposal."""
    monkeypatch.setitem(sc.MIN_N, "agreement", 1)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _emit(conn, "action.proposed", "ep_near", D23, lane="chat", patient_id="P1",
              payload={"action_class": "order_abg"})
        _emit(conn, "action.proposed", "ep_near", D23 + timedelta(minutes=1), lane="chat",
              patient_id="P1", payload={"action_class": "escalate_review"})
        _emit(conn, "clinical.action", "ep_near", D23 + timedelta(minutes=2), lane="chat",
              patient_id="P1", payload={"action_class": "escalate_review"})
        _emit(conn, "action.approved", "ep_near", D23 + timedelta(minutes=3), lane="chat",
              patient_id="P1", payload={"action_class": "order_abg"})

    detail = _card()["metrics"]["agreement"]["detail"]
    pairs = sorted(detail["rows"], key=lambda r: r["proposed_at"])
    assert len(pairs) == 2 and detail["unresolved_proposals"] == 0
    assert [p["proposed_class"] for p in pairs] == ["order_abg", "escalate_review"]
    # The LATER proposal was resolved by the EARLIER action: nearest preceding.
    assert pairs[1]["resolution_kind"] == "clinical.action"
    assert pairs[0]["resolution_kind"] == "action.approved"
    assert all(p["matched"] for p in pairs)
    # And the two actions are not reused: two proposals, two actions, two pairs.
    assert len({p["resolved_at"] for p in pairs}) == 2


def test_the_explicit_subject_link_beats_the_clock(monkeypatch):
    """Where the producers identify the decision, the metric must not guess.

    The Sage card lane writes the same (subject_type, subject_id) on both sides:
    `action.proposed` from sage_ambient_service and `action.approved` /
    `action.declined` from relay_routes both carry ('sage_actions', <action_id>).
    Two cards resolved in the OPPOSITE order to the one they were proposed in is
    the case where a clock-based rule silently swaps the two action classes and
    reports the wrong agreement — here, 2/2 instead of the truth, 0/2."""
    monkeypatch.setitem(sc.MIN_N, "agreement", 1)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        for i, action_class in enumerate(("order_abg", "escalate_review")):
            _emit(conn, "action.proposed", "ep_link", D23 + timedelta(minutes=i),
                  lane="chat", patient_id="P1", subject_type="sage_actions",
                  subject_id=f"card{i}", payload={"action_class": action_class})
        # Resolved in reverse order, and each clinician did something ELSE.
        _emit(conn, "action.approved", "ep_link", D23 + timedelta(minutes=10),
              lane="chat", patient_id="P1", subject_type="sage_actions",
              subject_id="card1", payload={"action_class": "order_abg"})
        _emit(conn, "action.approved", "ep_link", D23 + timedelta(minutes=11),
              lane="chat", patient_id="P1", subject_type="sage_actions",
              subject_id="card0", payload={"action_class": "escalate_review"})

    metric = _card()["metrics"]["agreement"]
    detail = metric["detail"]
    rows = {r["proposed_class"]: r for r in detail["rows"]}

    assert detail["pairs_by_subject_link"] == 2, "the explicit link must be used"
    assert detail["pairs_by_nearest_preceding"] == 0
    assert all(r["paired_by"] == "subject" for r in detail["rows"])
    # card0 proposed order_abg and the clinician escalated; card1 the reverse.
    assert rows["order_abg"]["taken_class"] == "escalate_review"
    assert rows["escalate_review"]["taken_class"] == "order_abg"
    assert metric["numerator"] == 0 and metric["denominator"] == 2
    assert metric["disagreements"] == 2, "a clock-based pairing would report 2/2"


def test_a_clinician_action_with_no_preceding_proposal_is_not_a_pair(monkeypatch):
    """Sage said nothing; the clinician acted anyway. That is not agreement and
    it is not disagreement — it is outside what this metric measures, and it is
    counted rather than absorbed."""
    monkeypatch.setitem(sc.MIN_N, "agreement", 1)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _emit(conn, "clinical.action", "ep_solo", D23, lane="chat", patient_id="P1",
              payload={"action_class": "escalate_review"})
        _emit(conn, "action.proposed", "ep_solo", D23 + timedelta(minutes=5), lane="chat",
              patient_id="P1", payload={"action_class": "escalate_review"})
        _emit(conn, "action.approved", "ep_solo", D23 + timedelta(minutes=6), lane="chat",
              patient_id="P1", payload={"action_class": "escalate_review"})

    metric = _card()["metrics"]["agreement"]
    assert metric["denominator"] == 1
    assert metric["detail"]["actions_without_a_proposal"] == 1
    assert metric["detail"]["unresolved_proposals"] == 0


def test_agreement_defaults_to_phase_a_when_unconfigured(monkeypatch):
    monkeypatch.setitem(sc.MIN_N, "agreement", 1)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _emit(conn, "action.proposed", "ep_p", D23, lane="chat", patient_id="P1",
              payload={"action_class": "escalate_review"})
        _emit(conn, "action.approved", "ep_p", D23 + timedelta(minutes=5), lane="chat",
              patient_id="P1", payload={"action_class": "escalate_review"})

    metric = _card()["metrics"]["agreement"]
    assert metric["detail"]["headline_phase"] == "A"
    assert metric["value"] == 1.0 and metric["denominator"] == 1


# ════════════════════════════════════════════════════════════════════════════
#  6. Coverage — silence is not evidence of absence
# ════════════════════════════════════════════════════════════════════════════
def test_uncovered_ward_shift_is_excluded_from_denominators_and_surfaced(monkeypatch):
    """Only 2026-07-23 is confirmed. The alert on 07-24 falls in uncovered time:
    excluded from the episode denominator, excluded from bed-days, and reported
    as uncovered rather than assumed quiet.

    covered = 1 window of 24 h → 1 patient x 24 h = 1.0 occupied bed-day."""
    monkeypatch.setitem(sc.MIN_N, "false_alert_rate", 1)
    with db_session() as conn:
        _patients(conn, n=1)
        _cover(conn, start=D23, end=D24, at=D23 + timedelta(hours=23))
        _alert_episode(conn, "ep_in", D23 + timedelta(hours=6), "P1", "false_alert")
        _alert_episode(conn, "ep_out", D24 + timedelta(hours=6), "P2", "false_alert")

    card = _card()
    coverage = card["coverage"]
    metric = card["metrics"]["false_alert_rate"]

    assert coverage["display"] == "1/4"
    assert coverage["complete"] is False
    assert coverage["covered_hours"] == 24.0
    assert coverage["uncovered_hours"] == 48.0
    assert coverage["by_ward"][WARD] == "1/4"
    assert coverage["uncovered_sample"], "uncovered time must be shown, not hidden"

    assert card["bed_days"]["bed_days"] == 1.0
    assert metric["detail"]["episodes"] == 1, "the uncovered episode must not be counted"
    assert metric["detail"]["alerts_excluded_uncovered"] == 1
    assert metric["numerator"] == 1
    assert metric["denominator"] == 1.0
    assert metric["value"] == 1.0
    assert metric["provisional"] is True
    assert "coverage" in (metric["provisional_reason"] or "").lower()

    readiness = {r["id"]: r for r in card["readiness"]}
    assert readiness["coverage"]["met"] is False
    assert "1/4" in readiness["coverage"]["detail"]


def test_outcome_recorded_also_marks_a_ward_shift_covered():
    """A confirmed outcome is itself proof someone was watching that shift."""
    with db_session() as conn:
        _patients(conn)
        _emit(conn, "outcome.recorded", "ep_o", D23 + timedelta(hours=5), lane="manual",
              patient_id="P1", payload={"event_type": "icu_transfer", "source": "witnessed"})

    coverage = _card()["coverage"]
    assert coverage["covered"] == 1 and coverage["total"] == 4
    assert coverage["windows"][1]["evidence"] == "outcome.recorded"


def test_a_ward_with_patients_and_no_events_is_uncovered_not_invisible():
    """THE SILENT WARD. The worst thing that can happen to this instrument is
    that it stops running on a ward — and the ward then produces no events, so a
    ward list derived from the EVENT STREAM drops it from the denominator
    entirely and the pilot reports the coverage of whichever wards still work.

    `_wards_in_scope` therefore unions the patient CENSUS into the ward list. A
    ward with admitted patients and zero events must read as wholly uncovered
    time, be named in `silent_wards`, and appear in `card["wards"]`.

    Reverting `_wards_in_scope` to `return sorted(seen), diagnostics` — i.e.
    dropping the census union — must fail this test. That mutation passed the
    whole suite before this test existed.

    Ward-scoped assertions only: this DB is shared with the other shadow test
    modules, so other wards may also be in census scope. The two wards below are
    private to this test."""
    loud, silent = "Ward SC Loud", "Ward SC Silent"
    with db_session() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO patients (id, name, mrn, ward, admission_date, status) "
            "VALUES ('sc_loud', 'Loud', 'SC-MRN-L', ?, ?, 'admitted')",
            (loud, sc._iso(START - timedelta(days=30))))
        conn.execute(
            "INSERT OR REPLACE INTO patients (id, name, mrn, ward, admission_date, status) "
            "VALUES ('sc_silent', 'Silent', 'SC-MRN-S', ?, ?, 'admitted')",
            (silent, sc._iso(START - timedelta(days=30))))
        # The loud ward is fully confirmed for the whole window. The silent ward
        # gets nothing at all — not even an episode.opened. That is the point.
        _cover(conn, ward=loud)

    # ward=None: an explicit ward filter deliberately bypasses the census.
    card = sc.scorecard(days=DAYS, ward=None, now=NOW)
    coverage = card["coverage"]

    assert silent in card["wards"], "a ward with patients must be in scope"
    assert silent in coverage["by_ward"], "…and must appear in the coverage denominator"
    assert coverage["by_ward"][silent] == "0/4", "zero of its four ward-shifts are covered"
    assert coverage["by_ward"][loud] == "4/4"
    assert silent in coverage["silent_wards"]
    assert loud not in coverage["silent_wards"]
    assert coverage["complete"] is False, "a silent ward cannot leave coverage complete"
    assert coverage["silent_ward_note"] and silent in coverage["silent_ward_note"]
    assert any(w["ward"] == silent for w in coverage["uncovered_sample"])
    # 3 days x 24 h of unobserved ward time, which the events alone cannot see.
    assert coverage["uncovered_hours"] >= 72.0
    assert coverage["census"].get("available") is True
    assert coverage["census"]["wards_with_census_but_no_events"] >= 1
    # And it reaches the reader, not just the payload.
    assert any(silent in note for note in card["notes"])


# ════════════════════════════════════════════════════════════════════════════
#  7. Time saved — a proxy, labelled as one
# ════════════════════════════════════════════════════════════════════════════
def test_time_saved_is_a_labelled_proxy_with_visible_assumptions(monkeypatch):
    """3 answers, total_ms [5000, 7000, 9000] → p50 = 7 s.
    saved = 3 x (240 - 7) s = 699 s = 0.19417 h. ttft p50 = 2000 ms."""
    monkeypatch.setitem(sc.MIN_N, "time_saved", 3)
    monkeypatch.setenv("ORB_SCORECARD_MANUAL_LOOKUP_SECONDS", "240")
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        for i, (ttft, total) in enumerate([(1000, 5000), (2000, 7000), (3000, 9000)]):
            _emit(conn, "sage.drafted", f"ep_t{i}", D23 + timedelta(hours=i), lane="chat",
                  patient_id="P1", payload={"ttft_ms": ttft, "total_ms": total,
                                            "confidence": 0.8})

    metric = _card()["metrics"]["time_saved"]
    detail = metric["detail"]

    assert metric["is_proxy"] is True
    assert detail["timed_answers"] == 3
    assert detail["total_ms_p50"] == 7000.0
    assert detail["ttft_ms_p50"] == 2000.0
    assert detail["ttft_ms_p95"] == 2900.0
    assert round(detail["saved_hours"], 5) == 0.19417
    assert metric["state"] == "reported"
    assert metric["display"] == "0.2 h (proxy)"
    assert "proxy" in metric["display"].lower()

    sources = {a["label"]: a["source"] for a in metric["assumptions"]}
    assert "STATED ASSUMPTION" in sources["Manual lookup baseline"]
    assert sources["Sage time-to-first-token (p50 / p95)"].startswith("measured")
    assert sources["Clinical time saved"].startswith("would require a controlled comparison")


def test_cancelled_sage_drafts_are_not_timed_answers(monkeypatch):
    monkeypatch.setitem(sc.MIN_N, "time_saved", 1)
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _emit(conn, "sage.drafted", "ep_c", D23, lane="chat", patient_id="P1",
              payload={"ttft_ms": 900, "total_ms": 4000, "cancelled": True})

    metric = _card()["metrics"]["time_saved"]
    assert metric["detail"]["timed_answers"] == 0
    assert metric["state"] == "insufficient_data"
    assert metric["value"] is None


# ════════════════════════════════════════════════════════════════════════════
#  8. The instrument reporting on itself
# ════════════════════════════════════════════════════════════════════════════
def test_simulator_lane_is_not_evidence():
    """Simulated alerts must never be counted as pilot evidence — and their
    exclusion must be stated, not silent."""
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _emit(conn, "alert.fired", "ep_sim", D23, lane="simulator", patient_id="P1",
              payload={"level": "critical"})
        _emit(conn, "adjudication.recorded", "ep_sim", D23 + timedelta(hours=1),
              lane="simulator", patient_id="P1", payload={"verdict": "false_alert"})

    card = _card()
    assert card["metrics"]["false_alert_rate"]["detail"]["episodes"] == 0
    assert card["instrument"]["simulator_events"] == 2
    assert any("simulator" in w for w in card["instrument"]["warnings"])
    assert any("simulator" in n for n in card["notes"])

    included = _card(include_simulator=True)
    assert included["metrics"]["false_alert_rate"]["detail"]["episodes"] == 1


def test_instrument_health_reports_its_own_losses(monkeypatch):
    """A silent write failure must not read as 'no events happened'."""
    with db_session() as conn:
        _emit(conn, "alert.fired", "ep_h", D23, patient_id="P1", payload={"level": "elevated"})
    monkeypatch.setitem(shadow_service.STATS, "dropped_error", 3)
    monkeypatch.setitem(shadow_service.STATS, "dropped_backpressure", 1)

    health = sc.instrument_health()
    assert health["append_only"] is True
    assert health["dropped_events"] == 4
    assert health["error_events"] == 3
    assert health["healthy"] is False
    assert any("dropped" in w for w in health["warnings"])
    assert health["events_total"] == 1
    assert health["kinds"]["alert.fired"] == 1
    assert health["lanes"]["ingestion"] == 1
    assert health["lag_ms_p50"] is not None

    readiness = {r["id"]: r for r in _card()["readiness"]}
    assert readiness["no_dropped_events"]["met"] is False


def test_instrument_health_flags_a_missing_append_only_trigger():
    with db_session() as conn:
        conn.execute("DROP TRIGGER IF EXISTS trg_shadow_no_update")
    health = sc.instrument_health()
    assert health["append_only"] is False
    assert any("append-only" in w.lower() for w in health["warnings"])


# ════════════════════════════════════════════════════════════════════════════
#  9. Drill-down
# ════════════════════════════════════════════════════════════════════════════
def test_episode_detail_is_ordered_and_summarised():
    with db_session() as conn:
        _patients(conn)
        _emit(conn, "episode.opened", "ep_d", D23, patient_id="P1",
              payload={"trigger": "news2_threshold"})
        _emit(conn, "alert.fired", "ep_d", D23 + timedelta(minutes=5), patient_id="P1",
              payload={"level": "critical", "raw_news2": 9})
        _emit(conn, "sage.drafted", "ep_d", D23 + timedelta(minutes=10), lane="chat",
              patient_id="P1", payload={"ttft_ms": 1200, "total_ms": 4000})
        _emit(conn, "action.proposed", "ep_d", D23 + timedelta(minutes=12), lane="chat",
              patient_id="P1", payload={"action_class": "escalate_review"})
        _emit(conn, "action.approved", "ep_d", D23 + timedelta(minutes=20), lane="chat",
              patient_id="P1", payload={"action_class": "escalate_review"})
        _emit(conn, "outcome.recorded", "ep_d", D23 + timedelta(minutes=125), lane="manual",
              patient_id="P1", payload={"event_type": "icu_transfer", "source": "witnessed"})
        _emit(conn, "adjudication.recorded", "ep_d", D23 + timedelta(hours=6), lane="manual",
              patient_id="P1", payload={"verdict": "true_alert", "blinded": True})

    detail = sc.episode_detail("ep_d")
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds == ["episode.opened", "alert.fired", "sage.drafted", "action.proposed",
                     "action.approved", "outcome.recorded", "adjudication.recorded"]
    assert [e["occurred_at"] for e in detail["events"]] == \
        sorted(e["occurred_at"] for e in detail["events"])

    summary = detail["summary"]
    assert summary["alerts_fired"] == 1 and summary["max_level"] == "critical"
    assert summary["outcome"] == "icu_transfer" and summary["outcome_source"] == "witnessed"
    assert summary["lead_minutes"] == 120.0 and summary["lead_display"] == "2 h 00 m"
    assert summary["verdict_class"] == "true"
    assert detail["patient_id"] == "P1" and detail["ward"] == WARD
    assert detail["phi_included"] is False
    assert all(e["phi"] is None for e in detail["events"])
    assert all(e["lag_ms"] is not None for e in detail["events"])


def test_list_episodes_indexes_the_window():
    with db_session() as conn:
        _patients(conn)
        _alert_episode(conn, "ep_i1", D23, "P1", "false_alert", level="critical")
        _alert_episode(conn, "ep_i2", D24, "P2")
    index = sc.list_episodes(days=DAYS, ward=WARD, now=NOW)
    assert index["total"] == 2
    ids = [e["episode_id"] for e in index["episodes"]]
    assert ids == ["ep_i2", "ep_i1"]                     # newest first
    first = {e["episode_id"]: e for e in index["episodes"]}
    assert first["ep_i1"]["verdict_class"] == "false"
    assert first["ep_i1"]["max_level"] == "critical"
    assert first["ep_i2"]["verdict"] is None
    # Fields the blinded adjudication queue projects (routes/_BLIND_KEEP).
    for key in ("episode_id", "patient_id", "ward", "phase", "lane", "opened_at",
                "last_at", "closed_at", "duration_min", "events_n", "alerts",
                "sage_drafts", "has_outcome", "closed"):
        assert key in first["ep_i1"], key
    assert first["ep_i1"]["has_outcome"] is False
    assert first["ep_i1"]["lane"] == "ingestion"

    only_unadjudicated = sc.list_episodes(days=DAYS, ward=WARD, now=NOW,
                                          only_unadjudicated=True)
    assert [e["episode_id"] for e in only_unadjudicated["episodes"]] == ["ep_i2"]


# ════════════════════════════════════════════════════════════════════════════
#  10. The contract the frontend was written against
# ════════════════════════════════════════════════════════════════════════════
def test_payload_carries_every_field_the_client_normalizer_reads():
    """src/api/scorecard.ts downgrades anything it cannot verify. If a key goes
    missing here the view degrades silently, so the contract is pinned."""
    with db_session() as conn:
        _patients(conn)
        _cover(conn)
        _alert_episode(conn, "ep_k", D23, "P1", "false_alert")

    card = _card()
    for key in ("generated_at", "period", "wards", "phase", "phase_label", "instrument",
                "readiness", "metrics", "export"):
        assert key in card, key
    assert set(card["period"]) >= {"start", "end", "days"}
    for key in ("source", "schema_v", "append_only", "events_total", "episodes_total",
                "episodes_closed", "first_event_at", "last_event_at", "lag_ms_p50",
                "lag_ms_p95", "lanes", "kinds", "corrections"):
        assert key in card["instrument"], key
    for item in card["readiness"]:
        assert set(item) == {"id", "label", "met", "detail"}
    for metric_id, metric in card["metrics"].items():
        for key in ("id", "label", "definition", "state", "value", "unit", "display",
                    "numerator", "denominator", "denominator_label",
                    "alternative_denominators", "n_observed", "n_required", "spread",
                    "caveat", "blockers", "is_proxy", "assumptions", "adjudication",
                    "disagreements"):
            assert key in metric, f"{metric_id}.{key}"
        assert metric["id"] == metric_id
        assert metric["state"] in ("reported", "insufficient_data", "unadjudicated",
                                   "not_computable")
    assert card["export"]["formats"] == ["json", "csv"]
    assert card["export"]["deidentification"], "the export must state what it removes"
    for item in card["export"]["deidentification"]:
        assert set(item) == {"field", "treatment", "note"}
        assert item["treatment"] in ("removed", "hashed", "coarsened", "dropped", "retained")


def test_phase_schedule_assigns_events_to_the_phase_in_force(monkeypatch):
    monkeypatch.setenv("ORB_SHADOW_PHASE_SCHEDULE", "A@2026-07-01T00:00:00Z,C@2026-07-24T00:00:00Z")
    schedule = sc.PhaseSchedule.from_env()
    assert schedule.phase_at(D23) == "A"
    assert schedule.phase_at(D24) == "C"
    assert schedule.phase_at(D25) == "C"
    assert schedule.current(NOW) == "C"
    assert "Phase A" in sc.PhaseSchedule.label("A")


def test_unparsable_phase_schedule_falls_back_to_silent(monkeypatch):
    monkeypatch.setenv("ORB_SHADOW_PHASE_SCHEDULE", "[not json")
    assert sc.PhaseSchedule.from_env().phase_at(NOW) == "A"


# ════════════════════════════════════════════════════════════════════════════
#  11. THE CLASS, not the instance — no metric may flatter its own rows
# ════════════════════════════════════════════════════════════════════════════
# Two adversarial rounds produced eight blocking defects and EVERY ONE failed in
# Orb's favour, while this suite passed. That is a systematic bias, not bad luck:
# a metric that is silently too flattering looks correct to a test written by the
# same reasoning that produced the bug, because both are asking "does it say what
# I expect?" of the same expectation.
#
# So this section asserts no metric's value. It asserts a RELATION: every
# published figure must be contained by counts recomputed HERE, in the test, from
# the raw `shadow_events_current` rows and from the metric's own printed LABELS —
# never from the metric's `detail`, which is what keeps the recomputation
# independent. Where a metric can only be wrong in one direction, it must be
# wrong in the direction that costs Orb.
#
# WHAT THIS CATCHES
#   * n larger than the rows can support — the round-3 agreement double-count:
#     three pairs manufactured from one clinician action (C).
#   * a numerator in a different unit from the one its label names — the round-1
#     false-alert rate counting EPISODES while printing "alerts", which
#     understated the rate (D).
#   * a figure published below its own stated n floor, or a non-reported metric
#     still printing something (A).
#   * a denominator that silently shrinks — a ward with admitted patients falling
#     out of the coverage denominator, per-ward windows not summing to the
#     published total, a silent ward with covered shifts (E).
#   * an internally inconsistent proportion: value disagreeing with its own
#     numerator/denominator, or matched + disagreements not exhausting the
#     denominator (B).
#   * a miss quietly reclassified as a detection — leads exceeding the outcomes
#     whose patient was ever flagged at all (F).
#   * a proxy claiming more than its own stated assumption permits — hours
#     "saved" exceeding the entire assumed manual baseline (G).
#
# WHAT THIS DOES NOT CATCH — and writing this list down is part of the test,
# because a test claiming more coverage than it has is the same failure one level
# up:
#   * whether a metric measures the right clinical thing. Agreement could be a
#     flawless measurement of the wrong quantity and every assertion below holds.
#   * whether the ROWS are complete or honest. If the instrument never recorded
#     an event, the metric and this recomputation are blind in exactly the same
#     way. That blind spot is why the silent-ward coverage test exists separately
#     and is not merely an invariant.
#   * whether an exclusion rule is well chosen. Unresolved proposals, uncovered
#     ward-shifts and artefact verdicts are excluded by design; this checks that
#     the exclusions are counted and stated, not that excluding them is right.
#   * the CHOICE of pairing rule, lookback, shift definition or phase boundary.
#   * a bias implemented identically in the metric and in a naive recomputation.
#     Only the parts recomputed from raw rows (C, D, F, G's row bound, E's census)
#     are genuinely independent; the rest are internal-consistency checks.
#   * anything outside the four scorecard metrics and coverage — the export, the
#     routes and the instrument-health block are not examined here.
import json as _json


def _raw_bounds(conn, start, end, ward=None):
    """Bounds recomputed from the RAW rows. Nothing here reads a metric.

    `ward=None` means every ward, to match an unscoped scorecard."""
    rows = []
    for r in conn.execute(
            "SELECT id, episode_id, kind, occurred_at, lane, ward, patient_id, payload_json "
            "FROM shadow_events_current WHERE lane <> 'simulator'").fetchall():
        row = dict(r)
        try:
            row["payload"] = _json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            row["payload"] = {}
        if not isinstance(row["payload"], dict):
            row["payload"] = {}
        row["t"] = sc._dt(row["occurred_at"])
        row["w"] = row["ward"] or row["payload"].get("ward")
        rows.append(row)
    rows = [r for r in rows if r["t"] is not None and (ward is None or r["w"] == ward)]
    in_window = [r for r in rows if start <= r["t"] < end]

    def of(source, *names):
        return [r for r in source if r["kind"] in names]

    verdict_of = {}
    for row in sorted(of(rows, "adjudication.recorded"), key=lambda r: (r["t"], r["id"])):
        verdict_of[row["episode_id"]] = sc._classify_verdict(row["payload"].get("verdict"))
    false_episodes = {e for e, v in verdict_of.items() if v == "false"}
    alerts = of(in_window, "alert.fired")

    # An upper bound on "outcomes Orb could possibly have flagged": the same
    # patient has SOME alert.fired at a flagging level, anywhere in the record.
    # Deliberately looser than the metric's own rule (which also requires the
    # alert to fall inside the lookback), so it can only ever over-count — which
    # makes it a safe ceiling for `leads`.
    flagged_patients = {r["patient_id"] for r in of(rows, "alert.fired")
                        if r["payload"].get("level") in sc.ALERT_LEVELS}
    qualifying = [r for r in of(in_window, "outcome.recorded")
                  if r["payload"].get("event_type") in sc.OUTCOME_EVENT_TYPES]

    return {
        "outcomes_with_a_qualifying_alert": len(
            [r for r in qualifying if r.get("patient_id") in flagged_patients]),
        "proposals_in_window": len(of(in_window, "action.proposed")),
        # The scan reaches outside the reporting window, so the pairing's other
        # side is bounded by EVERY clinician action, not just the in-window ones.
        "clinician_actions": len(of(rows, *sorted(sc.CLINICIAN_ACTION_KINDS))),
        "alerts_in_window": len(alerts),
        "false_alerts_in_window": len([a for a in alerts if a["episode_id"] in false_episodes]),
        "false_episodes": len({a["episode_id"] for a in alerts
                               if a["episode_id"] in false_episodes}),
        "adjudicated_episodes": len({e for e, v in verdict_of.items() if v in ("true", "false")}),
        "outcomes_in_window": len([r for r in of(in_window, "outcome.recorded")
                                   if r["payload"].get("event_type") in sc.OUTCOME_EVENT_TYPES]),
        "drafts_in_window": len(of(in_window, "sage.drafted")),
    }


def _census_wards_now(conn, start, end):
    """Wards holding an admitted patient over the window, straight from `patients`."""
    wards = set()
    for r in conn.execute("SELECT ward, admission_date, status FROM patients").fetchall():
        row = dict(r)
        if (row.get("status") or "admitted").strip() != "admitted":
            continue
        if (sc._dt(row.get("admission_date")) or start) < end:
            wards.add(row.get("ward") or sc._UNSPECIFIED_WARD)
    return wards


def assert_no_metric_flatters_its_rows(card, raw, *, census_wards=None, exact_alerts=False):
    """Raise AssertionError if any published figure beats its own rows.

    A plain function, not a test, so the sensitivity test below can hand it a
    doctored card and prove it bites. `exact_alerts` requires fully-covered
    ward-shifts: uncovered alerts are excluded by design, so the false-alert
    equality only holds where nothing was excluded."""
    metrics = card["metrics"]

    # ── (A) The publication gate, every metric ────────────────────────────────
    for mid, m in metrics.items():
        assert m["state"] in ("reported", "insufficient_data", "unadjudicated",
                              "not_computable"), mid
        if m["state"] == "reported":
            assert m["n_required"] > 0, f"{mid}: reported against a zero floor"
            assert m["n_observed"] >= m["n_required"], (
                f"{mid}: published at n={m['n_observed']}, below its stated floor "
                f"of {m['n_required']}")
            assert m["denominator"] is not None and m["denominator"] > 0, mid
            assert m["display"] is not None, mid
            assert not m["blockers"], f"{mid}: reported despite blockers {m['blockers']}"
        else:
            assert m["value"] is None and m["display"] is None, (
                f"{mid}: state={m['state']} but still printing {m['display']!r}")
        if m["numerator"] is not None:
            assert m["numerator"] >= 0, mid

    # ── (B) A proportion is contained by its own denominator ──────────────────
    for mid, m in metrics.items():
        if m["unit"] != "proportion" or m["state"] != "reported":
            continue
        assert 0 <= m["numerator"] <= m["denominator"], (
            f"{mid}: numerator {m['numerator']} exceeds denominator {m['denominator']}")
        assert m["value"] == pytest.approx(m["numerator"] / m["denominator"]), (
            f"{mid}: the printed value disagrees with its own numerator/denominator")
        assert m["disagreements"] == m["denominator"] - m["numerator"], (
            f"{mid}: matched + disagreements must exhaust the denominator")

    # ── (C) A PAIRING metric cannot out-count the scarcer side ────────────────
    # The shape of the round-3 defect. n counts PAIRS; a pair consumes one row
    # from each side, so n can never exceed either side's row count.
    agreement = metrics["agreement"]
    detail = agreement["detail"]
    pairs_total = detail["pairs_total_across_phases"]
    assert pairs_total <= raw["proposals_in_window"], (
        f"agreement: {pairs_total} pair(s) from {raw['proposals_in_window']} proposal(s)")
    assert pairs_total <= raw["clinician_actions"], (
        f"agreement: {pairs_total} pair(s) from {raw['clinician_actions']} clinician "
        "action(s) — an action has been counted more than once")
    assert pairs_total + detail["unresolved_proposals"] == raw["proposals_in_window"], (
        "agreement: every in-window proposal must be either paired or named unresolved, "
        f"got {pairs_total} + {detail['unresolved_proposals']} vs "
        f"{raw['proposals_in_window']}")
    assert sum(s["pairs"] for s in detail["segments"]) == pairs_total
    assert sum(s["unresolved"] for s in detail["segments"]) == detail["unresolved_proposals"]
    if agreement["denominator"]:
        assert agreement["denominator"] <= pairs_total, (
            "agreement: the headline denominator exceeds the pairs that exist")
    assert detail["pooled"] is None, "agreement: phases must never be pooled"
    seen = [(r["episode_id"], r["resolved_at"]) for r in detail["rows"]]
    assert len(seen) == len(set(seen)), (
        "agreement: one clinician action resolves two proposals in the same episode")

    # ── (D) A numerator counts the unit its own label names ───────────────────
    far = metrics["false_alert_rate"]
    fd = far["detail"]
    assert "alerts per occupied bed-day" in far["unit"]
    assert fd["false_alert_count"] >= fd["false_episode_count"], (
        "false_alert_rate: fewer alerts than the episodes that contain them")
    if far["numerator"] is not None:
        # The headline must be the ALERT count its own label names, not the
        # episode count — which is always the smaller, flattering number.
        assert far["numerator"] == fd["false_alert_count"], (
            f"false_alert_rate: the headline ({far['numerator']}) is not the field its "
            f"label names (false_alert_count={fd['false_alert_count']})")
        assert far["numerator"] >= fd["false_episode_count"], (
            "false_alert_rate: counting EPISODES while printing 'alerts' understates "
            "the rate, which is the flattering direction")
        assert far["numerator"] <= raw["alerts_in_window"], (
            "false_alert_rate: more false alerts than alerts exist")
        if exact_alerts:
            assert far["numerator"] == raw["false_alerts_in_window"], (
                f"false_alert_rate: numerator {far['numerator']} is not the count of "
                f"alert.fired rows in false episodes ({raw['false_alerts_in_window']})")
            assert far["numerator"] >= raw["false_episodes"]
    if far["adjudication"]:
        adj = far["adjudication"]
        assert adj["adjudicated"] <= adj["total"], "more episodes adjudicated than exist"
        assert adj["adjudicated"] <= raw["adjudicated_episodes"]
    if far["state"] == "reported":
        assert far["value"] == pytest.approx(far["numerator"] / far["denominator"])

    # ── (E) Denominators do not silently shrink ───────────────────────────────
    coverage = card["coverage"]
    assert coverage["covered"] <= coverage["total"]
    assert coverage["complete"] is (bool(coverage["total"])
                                    and coverage["covered"] == coverage["total"])
    per_ward = {w: int(v.split("/")[1]) for w, v in coverage["by_ward"].items()}
    assert sum(per_ward.values()) == coverage["total"], (
        "coverage: the per-ward windows do not add up to the published total")
    assert set(coverage["by_ward"]) == set(coverage["wards"])
    if census_wards is not None:
        missing = census_wards - set(coverage["by_ward"])
        assert not missing, (
            "coverage: ward(s) with admitted patients dropped out of the denominator: "
            f"{sorted(missing)}")
        for silent in coverage["silent_wards"]:
            assert coverage["by_ward"][silent].startswith("0/"), (
                "coverage: a ward that recorded nothing cannot have covered shifts")

    # ── (F) Leads are a subset of outcomes; the misses are the rest ───────────
    lead = metrics["detection_lead_time"]
    ld = lead["detail"]
    if lead["denominator"]:
        assert ld["leads"] + ld["misses"] == ld["outcomes"] == lead["denominator"]
        assert lead["numerator"] == ld["leads"] <= lead["denominator"]
        assert lead["denominator"] <= raw["outcomes_in_window"], (
            "detection_lead_time: more outcomes in the denominator than were recorded")
        # A miss cannot be reclassified as a lead: an outcome only counts as
        # detected if that patient was actually flagged at all.
        assert ld["leads"] <= raw["outcomes_with_a_qualifying_alert"], (
            f"detection_lead_time: {ld['leads']} detected outcome(s), but only "
            f"{raw['outcomes_with_a_qualifying_alert']} had any flagging alert for that "
            "patient — misses are being absorbed into the numerator")
        assert ld["sensitivity"] == pytest.approx(ld["leads"] / ld["outcomes"])

    # ── (G) A proxy may not claim more than its own assumption allows ─────────
    ts = metrics["time_saved"]
    td = ts["detail"]
    assert td["timed_answers"] <= td["answers"] <= raw["drafts_in_window"]
    if td["saved_hours"] is not None:
        ceiling = td["timed_answers"] * td["manual_baseline_seconds"] / 3600.0
        assert 0.0 <= td["saved_hours"] <= ceiling + 1e-9, (
            f"time_saved: {td['saved_hours']} h saved exceeds the entire assumed "
            f"baseline of {ceiling} h — the proxy claims more than it assumes")
    if ts["state"] == "reported":
        assert ts["value"] == pytest.approx(round(td["saved_hours"], 3))


def _adversarial_fixture(conn):
    """One appliance-state carrying every failure shape from rounds 1–3."""
    _patients(conn, n=1)
    _cover(conn)
    # A ward with admitted patients and NO events at all — the dead instrument.
    conn.execute(
        "INSERT OR REPLACE INTO patients (id, name, mrn, ward, admission_date, status) "
        "VALUES ('sc_invsil', 'Silent', 'SC-MRN-INV', 'Ward SC Inv Silent', ?, 'admitted')",
        (sc._iso(START - timedelta(days=30)),))
    # Agreement: three proposals and ONE click, plus one genuine (declined) pair.
    for i in range(3):
        _emit(conn, "action.proposed", "ep_inv_multi", D23 + timedelta(minutes=i),
              lane="chat", patient_id="P1", payload={"action_class": "order_abg"})
    _emit(conn, "action.approved", "ep_inv_multi", D23 + timedelta(minutes=30),
          lane="chat", patient_id="P1", payload={"action_class": "order_abg"})
    _emit(conn, "action.proposed", "ep_inv_solo", D23 + timedelta(hours=2),
          lane="chat", patient_id="P1", payload={"action_class": "escalate_review"})
    _emit(conn, "action.declined", "ep_inv_solo", D23 + timedelta(hours=2, minutes=5),
          lane="chat", patient_id="P1", payload={"action_class": "escalate_review"})
    # False-alert rate: one FALSE episode carrying FOUR alerts, so episodes and
    # alerts are different numbers and a unit slip is visible.
    for episode, verdict, n_alerts in (("ep_inv_f", "false_alert", 4),
                                       ("ep_inv_t", "true_alert", 1),
                                       ("ep_inv_u", None, 2)):
        _emit(conn, "episode.opened", episode, D23, patient_id="P1",
              payload={"trigger": "news2_threshold", "level": "elevated"})
        for j in range(n_alerts):
            _emit(conn, "alert.fired", episode, D23 + timedelta(minutes=j),
                  patient_id="P1", payload={"level": "elevated", "raw_news2": 6})
        if verdict:
            _emit(conn, "adjudication.recorded", episode, D23 + timedelta(hours=3),
                  lane="manual", patient_id="P1", actor_user_id="D001", actor_role="doctor",
                  payload={"verdict": verdict, "adjudicator_role": "doctor", "blinded": True})
    _lead_fixture(conn)      # 5 outcomes, 4 leads, 1 miss
    for i in range(3):
        _emit(conn, "sage.drafted", f"ep_inv_s{i}", D23 + timedelta(hours=4, minutes=i),
              lane="chat", patient_id="P1",
              payload={"total_ms": 4000 + i * 100, "ttft_ms": 700 + i * 10,
                       "conversation_id": f"c{i}"})


def _floors(monkeypatch, n=2):
    for metric_id in sc.METRIC_IDS:
        monkeypatch.setitem(sc.MIN_N, metric_id, n)


def test_no_published_metric_is_more_favourable_than_its_own_rows(monkeypatch):
    """The class-level invariant over a fixture built from every known failure
    shape. Reverting the round-3 pairing fix fails (C); reverting the census
    union in `_wards_in_scope` fails (E)."""
    _floors(monkeypatch, 2)
    with db_session() as conn:
        _adversarial_fixture(conn)
        raw_ward = _raw_bounds(conn, START, NOW, WARD)
        raw_all = _raw_bounds(conn, START, NOW, None)
        census = _census_wards_now(conn, START, NOW)

    # Ward-scoped: fully covered, so the false-alert equality is exact.
    assert_no_metric_flatters_its_rows(_card(), raw_ward, exact_alerts=True)
    # Unscoped: an explicit ward filter bypasses the census by design, so the
    # ward-scope invariant is checked on the card that actually consults it.
    assert_no_metric_flatters_its_rows(
        sc.scorecard(days=DAYS, ward=None, now=NOW), raw_all, census_wards=census)

    # The fixture must genuinely exercise the invariants, or this is a no-op.
    assert (raw_ward["proposals_in_window"], raw_ward["clinician_actions"]) == (4, 2)
    assert (raw_ward["false_alerts_in_window"], raw_ward["false_episodes"]) == (4, 1)
    assert (raw_ward["outcomes_in_window"], raw_ward["drafts_in_window"]) == (5, 3)
    assert "Ward SC Inv Silent" in census
    for metric_id in sc.METRIC_IDS:
        assert _card()["metrics"][metric_id]["state"] == "reported", metric_id


#: Each entry doctors an honest card in a way that FLATTERS Orb. If the audit
#: above cannot tell these from the truth it is decoration, so the audit is run
#: against every one of them and must reject all of them.
def _lie_agreement_repairs_one_action(card):
    """The round-3 defect, re-inserted: one click resolving three proposals."""
    m = card["metrics"]["agreement"]
    d = m["detail"]
    row = dict(d["rows"][0], matched=True)
    d["rows"] = [row, dict(row), dict(row)]
    d["pairs_total_across_phases"] = 3
    d["unresolved_proposals"] = 0
    d["segments"] = [dict(d["segments"][0], pairs=3, matched=3, disagreements=0,
                          unresolved=0, rate=1.0, display="100% (3/3)")]
    m.update(numerator=3, denominator=3, n_observed=3, disagreements=0, value=1.0,
             display="100% (3/3) — Phase A", state="reported")


def _lie_false_alert_counts_episodes(card):
    """The round-1 defect: numerator switched to EPISODES under an 'alerts' label."""
    m = card["metrics"]["false_alert_rate"]
    m["numerator"] = m["detail"]["false_episode_count"]
    m["value"] = m["numerator"] / m["denominator"]


def _lie_publishes_below_its_floor(card):
    m = card["metrics"]["time_saved"]
    m["n_required"] = m["n_observed"] + 5
    m["state"] = "reported"


def _lie_prints_while_not_reported(card):
    m = card["metrics"]["agreement"]
    m["state"] = "insufficient_data"
    m["display"] = "100% (2/2) — Phase A"


def _lie_drops_the_silent_ward(card):
    """The defect-7 shape: the silent ward leaves the denominator."""
    coverage = card["coverage"]
    for ward in list(coverage["by_ward"]):
        if ward in coverage["silent_wards"]:
            coverage["total"] -= int(coverage["by_ward"].pop(ward).split("/")[1])
            coverage["wards"].remove(ward)
    coverage["silent_wards"] = []
    coverage["complete"] = coverage["covered"] == coverage["total"]


def _lie_marks_a_silent_ward_covered(card):
    coverage = card["coverage"]
    ward = card["coverage"]["silent_wards"][0]
    total = int(coverage["by_ward"][ward].split("/")[1])
    coverage["by_ward"][ward] = f"{total}/{total}"
    coverage["covered"] += total
    coverage["complete"] = coverage["covered"] == coverage["total"]


def _lie_more_leads_than_outcomes(card):
    m = card["metrics"]["detection_lead_time"]
    m["detail"]["leads"] += m["detail"]["misses"]
    m["detail"]["misses"] = 0
    m["detail"]["sensitivity"] = 1.0
    m["numerator"] = m["detail"]["leads"]


def _lie_proxy_beats_its_own_baseline(card):
    d = card["metrics"]["time_saved"]["detail"]
    d["saved_hours"] = d["timed_answers"] * d["manual_baseline_seconds"] / 3600.0 * 2
    card["metrics"]["time_saved"]["value"] = round(d["saved_hours"], 3)


FLATTERING_LIES = [
    _lie_agreement_repairs_one_action,
    _lie_false_alert_counts_episodes,
    _lie_publishes_below_its_floor,
    _lie_prints_while_not_reported,
    _lie_drops_the_silent_ward,
    _lie_marks_a_silent_ward_covered,
    _lie_more_leads_than_outcomes,
    _lie_proxy_beats_its_own_baseline,
]


@pytest.mark.parametrize("lie", FLATTERING_LIES, ids=lambda f: f.__name__[5:])
def test_the_favourability_audit_rejects_every_flattering_lie(monkeypatch, lie):
    """The audit's own sensitivity, proven rather than asserted. An invariant
    test that cannot fail is worse than no test: it converts an unexamined
    property into a documented one."""
    _floors(monkeypatch, 2)
    with db_session() as conn:
        _adversarial_fixture(conn)
        raw_all = _raw_bounds(conn, START, NOW, None)
        census = _census_wards_now(conn, START, NOW)

    honest = sc.scorecard(days=DAYS, ward=None, now=NOW)
    assert_no_metric_flatters_its_rows(honest, raw_all, census_wards=census)

    doctored = copy.deepcopy(honest)
    lie(doctored)
    assert doctored != honest, f"{lie.__name__} changed nothing"
    with pytest.raises(AssertionError):
        assert_no_metric_flatters_its_rows(doctored, raw_all, census_wards=census)
