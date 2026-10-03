"""Shadow-mode scorecard API + de-identified export.

These tests guard the four properties that, if they rot, turn the pilot artefact
from evidence into a liability:

  1. No handler is `async def`. These are full scans of the busiest table in the
     pilot and every page read pays HMAC-SHA512 + AES since encryption-at-rest;
     a coroutine handler runs them ON the event loop and wedges the API. Asserted
     programmatically over the router, because a later edit will not notice.
  2. No PHI in the export. Asserted against the REAL serialised bytes (JSON and
     CSV), with PHI deliberately seeded into the source rows first — not against
     the intent of the code.
  3. Small-cell suppression actually fires, including complementary suppression,
     so a masked cell cannot be recovered by differencing against the total.
  4. Provenance is present and complete. An export that cannot state its own
     date range, denominators, coverage and removals is not evidence.

Plus: RBAC denies non-admins, and the surface behaves correctly on an EMPTY
dataset — the pilot starts empty and a bare "0 %" on morning one is the single
worst failure mode of this package.
"""
import inspect
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrations  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from auth import hash_password  # noqa: E402
from database import get_db, db_session  # noqa: E402
from routes import shadow_routes  # noqa: E402
from services import shadow_service  # noqa: E402
from services import shadow_export_service as exporter  # noqa: E402

client = TestClient(app)

BASE = "/api/admin/shadow"

# ── PHI deliberately planted in the source rows, then hunted in the output ───
PHI_PATIENT = "P9412"
PHI_NAME = "Aisha Verma"
PHI_MRN = "88123456"
PHI_WARD = "Ward B Cardiology Annexe"
PHI_ACTOR = "SHADOW-DOC-77"

# The department every actor in this file is seeded with (see `_ensure_user`).
# Named rather than repeated because, since 2026-08-14, POST /outcome derives
# ward scope from it — see `_admit_to_actor_ward`.
ACTOR_DEPARTMENT = "Administration"


# ── isolation ───────────────────────────────────────────────────────────────
def _wipe_shadow_events():
    """Give this file back a genuinely virgin `shadow_events`.

    Rows CANNOT be DELETEd: `trg_shadow_no_delete` makes every non-simulator row
    immutable, and that immutability is the property that makes the export
    evidentiary — it is to be preserved, not worked around. So the two triggers,
    the view and the table are dropped and migration 4 is re-run, rebuilding the
    instrument exactly as production has it. Same idiom as
    tests/test_shadow_scorecard.py::_wipe_shadow_events and the `_fresh`
    fixtures in tests/test_shadow_producers.py / test_shadow_clinical_lanes.py.
    """
    with db_session() as db:
        db.execute("DROP TRIGGER IF EXISTS trg_shadow_no_update")
        db.execute("DROP TRIGGER IF EXISTS trg_shadow_no_delete")
        db.execute("DROP VIEW IF EXISTS shadow_events_current")
        db.execute("DROP TABLE IF EXISTS shadow_events")
        migrations._m4_shadow_events(db)
        # Migration 18 extends idx_shadow_once to outcome.none_confirmed. The
        # rebuilt instrument must match what init_db produces (m4 THEN m18), or
        # every test below runs against the pre-fix index and the duplicate-
        # confirmation regression tests pass against a schema production
        # doesn't have.
        migrations._m18_shadow_none_confirmed_once(db)


@pytest.fixture(autouse=True)
def _fresh_instrument(monkeypatch):
    """A virgin instrument, a clean module table and no census leftovers —
    before AND after every test in this file.

    THE DEFECT THIS CLOSES. This file had no isolation at all. Roughly a third
    of its tests read `shadow_events` APPLIANCE-WIDE — `build_export(30, None,
    ...)`, `GET /scorecard` with no ward — and several of them only mean
    anything on an empty or near-empty instrument (`test_export_on_an_empty_
    window_states_absence_not_zero`, `test_scorecard_never_reports_a_bare_zero_
    on_an_empty_pilot`, the coverage before/after pair). They passed because
    pytest collects files alphabetically, so `test_shadow_producers.py` runs
    immediately before this one and its LAST test's teardown drops and rebuilds
    the table. Nothing in this file asked for that; nothing outside it promised
    it. Rename a file, add `test_shadow_r*.py`, run this file first, or shuffle
    the collection order, and every one of those assertions is suddenly reading
    whatever the rest of the suite left behind — in a table whose rows cannot be
    deleted afterwards.

    Three kinds of state are reset, because a wipe of only the first is the
    mistake test_shadow_scorecard.py already paid a full suite run to find:

      1. `shadow_events` itself (see `_wipe_shadow_events`).
      2. The CENSUS. `_seam_setup` admits `SEAM-*` patients to make the bed-day
         denominator real; left behind, they inflate `_census_wards` for every
         later reader of an appliance-wide scorecard — which is exactly the
         cross-file failure `test_shadow_scorecard.py::empty_appliance` exists
         to route around.
      3. PROCESS state: `_install_fake_service` replaces a real entry in
         `sys.modules`. Every call site guards it with try/finally, but a
         try/finally only runs if the assertion inside it is reached — an error
         raised while the stand-in is installed used to hand the fake scorecard
         module to the entire remainder of the suite. Teardown makes that
         impossible, and setup makes an inherited fake impossible.

    `shadow_service.STATS` is zeroed through monkeypatch.setitem (never a raw
    assignment) so instrument-health readings cannot accumulate across tests.
    """
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "true")
    _uninstall_fake_service()
    for key in list(shadow_service.STATS):
        monkeypatch.setitem(shadow_service.STATS, key, 0)
    shadow_service.reset_registry()
    _wipe_shadow_events()
    _wipe_seam_census()
    yield
    _uninstall_fake_service()
    shadow_service.reset_registry()
    _wipe_shadow_events()
    _wipe_seam_census()


def _wipe_seam_census():
    """Remove the admitted patients this file creates. `patients` has no
    append-only trigger, so unlike `shadow_events` these really can be deleted."""
    with db_session() as db:
        db.execute("DELETE FROM patients WHERE id LIKE 'SEAM-%'")
        db.execute("DELETE FROM patients WHERE id = ?", (PHI_PATIENT,))
        # The ground-truth fixtures below (`_admit_to_actor_ward`) put a real
        # admission on the actor's ward. Both rows go, and the admission goes
        # FIRST — it holds an FK to patients(id).
        db.execute("DELETE FROM admissions WHERE id LIKE 'ADM-SHADOWTEST-%'")
        db.execute("DELETE FROM patients WHERE id LIKE 'SHADOWTEST-%'")


# ── helpers ─────────────────────────────────────────────────────────────────
def _admit_to_actor_ward(patient_id: str) -> str:
    """A real patient, admitted to the department every actor here is seeded with.

    NEW ON 2026-08-14, and it is a fixture change that records a REAL rule
    rather than working around one. `POST /outcome` now derives ward scope from
    the AUTHENTICATED user (routes/shadow_routes.py), because the unscoped
    version was the worst entry in tests/test_caller_named_patient_scope.py's
    inventory: PROBED on 2026-08-07, a Cardiology nurse logged an `icu_transfer`
    on an Oncology patient — 201, row written, pilot denominator corrupted from
    an account that is deliberately denied the scorecard.

    So a ground-truth entry now needs the patient to actually be on the
    recorder's ward, which is what a real shift looks like: the charge nurse
    records the patients in front of her. A patient's department lives on their
    ADMISSION, never on patients.ward (permissions.PATIENT_DEPARTMENT_SQL), so
    the admission is what has to exist. Both rows are cleaned in
    `_wipe_seam_census`.
    """
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, status) "
            "VALUES (?, 'Shadow Ground Truth', ?, 'admitted')",
            (patient_id, f"MRN-{patient_id}"),
        )
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) "
            "VALUES ('SHADOWTEST-ADMITTER', 'Shadow Fixture', 'admin', 'x')"
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, department, status, admitted_by) "
            "VALUES (?, ?, ?, 'active', 'SHADOWTEST-ADMITTER')",
            (f"ADM-SHADOWTEST-{patient_id}", patient_id, ACTOR_DEPARTMENT),
        )
    return patient_id


def _ensure_user(user_id: str, role: str):
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, f"{role.title()} ShadowTest", role, ACTOR_DEPARTMENT, hash_password("password")),
        )
        db.commit()
    finally:
        db.close()


def _login(user_id: str) -> str:
    r = client.post("/api/auth/login", json={"employee_id": user_id, "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _admin() -> dict:
    _ensure_user("SHADOW-ADM", "admin")
    return _auth(_login("SHADOW-ADM"))


def _nurse() -> dict:
    _ensure_user("SHADOW-NUR", "nurse")
    return _auth(_login("SHADOW-NUR"))


def _seed_phi_events() -> str:
    """Write real rows carrying PHI in every column that could leak it."""
    episode = shadow_service.new_episode_id()
    shadow_service.emit(
        "episode.opened", episode, "ingestion",
        patient_id=PHI_PATIENT, ward=PHI_WARD, actor_user_id=PHI_ACTOR,
        payload={"trigger": "vitals", "level": "critical"},
        phi={"note": f"{PHI_NAME}, bed 7, MRN {PHI_MRN}"},
    )
    shadow_service.emit(
        "alert.fired", episode, "ingestion",
        patient_id=PHI_PATIENT, ward=PHI_WARD, actor_user_id=PHI_ACTOR,
        payload={"level": "critical", "raw_news2": 9},
        phi={"vitals_note": f"{PHI_NAME} desaturating"},
    )
    return episode


def _seed_rare_kind(n: int, kind: str = "sage.refused", ward: str = None) -> None:
    """n rows of one kind — n < k, so the cell must be masked in the export.

    `ward` is the isolation boundary (same reason as `_seam_setup` below): a
    count taken with no ward filter is a count over every row every other test
    in the suite has written, and these rows cannot be deleted afterwards.

    Stamped a second ago, not "now". The export window is [now-days, now) at
    MILLISECOND resolution, so a row whose `occurred_at` lands in the same
    millisecond as the `build_export` call that follows falls outside its own
    window and the breakdown comes back empty — which is what these rows are
    for. A backdated stamp is also the honest shape: the events an export
    reports on happened before it was taken.
    """
    from datetime import datetime, timedelta, timezone
    when = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="milliseconds")
    for _ in range(n):
        shadow_service.emit(
            kind, shadow_service.new_episode_id(), "chat",
            occurred_at=when, ward=ward,
            payload={"refusal_class": "out_of_scope", "stage": "pre"},
        )


class _FakeScorecardService:
    """Stand-in for the sibling package so blinding and metric projection are
    testable deterministically, whether or not shadow_scorecard_service has
    landed on disk yet."""

    @staticmethod
    def build_scorecard(days=30, ward=None):
        return {
            "generated_at": "2026-08-01T00:00:00.000+00:00",
            "period": {"start": "2026-07-02T00:00:00.000+00:00",
                       "end": "2026-08-01T00:00:00.000+00:00", "days": days},
            "phase": "A", "phase_label": "Phase A: silent",
            "instrument": {"schema_v": 1, "events_total": 2},
            "metrics": [
                {"id": "detection_lead_time", "label": "Detection lead time",
                 "state": "insufficient_data", "value": None, "display": None,
                 "numerator": 0, "denominator": 0,
                 "denominator_label": "confirmed deterioration events",
                 "n_observed": 0, "n_required": 10, "blockers": [],
                 "caveat": "Chart-review outcomes are biased late.",
                 "is_proxy": False,
                 # Fields a future metric might carry that MUST NOT be exported:
                 "worst_case_patient_id": PHI_PATIENT,
                 "example_episode_id": "ep_deadbeefdeadbeef",
                 "debug_note": f"driven by {PHI_PATIENT} ({PHI_NAME})"},
                {"id": "false_alert_rate", "label": "False-alert rate",
                 "state": "unadjudicated", "value": None, "display": None,
                 "numerator": None, "denominator": 3,
                 "denominator_label": "occupied bed-days",
                 "n_observed": 0, "n_required": 20, "blockers": ["No adjudications recorded"],
                 "caveat": "Artefact and indeterminate verdicts are excluded.",
                 "is_proxy": False},
            ],
        }

    @staticmethod
    def list_episodes(days=30, ward=None, status="pending", limit=50, offset=0):
        return {
            "episodes": [{
                "episode_id": "ep_1111222233334444",
                "opened_at": "2026-07-31T04:00:00.000+00:00",
                "ward": "Ward B", "events_n": 6, "alerts_n": 2,
                "has_outcome": False, "adjudicated": False,
                # Orb's verdict — must be withheld from a blinded queue:
                "level": "critical", "score": 9, "raw_news2": 9,
                "drivers": ["RR 30", "SpO2 88"], "draft": "Consider ICU review",
                "orb_verdict": "true_positive", "confidence": 0.91,
            }],
            "total": 1,
        }

    @staticmethod
    def episode_detail(episode_id):
        return {"episode_id": episode_id,
                "events": [{"kind": "alert.fired", "occurred_at": "2026-07-31T04:00:00.000+00:00"}]}

    @staticmethod
    def instrument_health():
        return {"dropped_events": 0, "error_events": 0, "lag_ms_p50": 4.0, "lag_ms_p95": 11.0}


def _install_fake_service():
    import services
    fake = types.ModuleType("services.shadow_scorecard_service")
    for name in ("build_scorecard", "list_episodes", "episode_detail", "instrument_health"):
        setattr(fake, name, getattr(_FakeScorecardService, name))
    sys.modules["services.shadow_scorecard_service"] = fake
    setattr(services, "shadow_scorecard_service", fake)
    return fake


def _uninstall_fake_service():
    import services
    sys.modules.pop("services.shadow_scorecard_service", None)
    if hasattr(services, "shadow_scorecard_service"):
        delattr(services, "shadow_scorecard_service")


# ════════════════════════════════════════════════════════════════════════════
#  1. Every handler is `def`, not `async def`
# ════════════════════════════════════════════════════════════════════════════
def test_no_handler_is_a_coroutine():
    """The rule most likely to rot. Asserted over the router, not by reading."""
    offenders = [
        r.path for r in shadow_routes.router.routes
        if inspect.iscoroutinefunction(getattr(r, "endpoint", None))
    ]
    assert offenders == [], (
        f"These shadow handlers are `async def` and would run heavy encrypted "
        f"scans on the event loop: {offenders}"
    )


def test_router_exposes_the_full_surface():
    paths = {r.path for r in shadow_routes.router.routes}
    assert paths == {
        f"{BASE}/scorecard",
        f"{BASE}/episodes",
        f"{BASE}/episode/{{episode_id}}",
        f"{BASE}/adjudication",
        f"{BASE}/outcome",
        f"{BASE}/outcome/none",
        # Added 2026-08-02 with charge-nurse outcome capture: lets the nurse view
        # ask "has this shift already been confirmed?" so the once-per-shift
        # confirmation is not silently recorded twice or, worse, skipped because
        # nobody could tell. Recorder-scoped (`_RECORDER`), like the two POSTs.
        f"{BASE}/outcome/shift-status",
        f"{BASE}/export/scorecard",
    }, paths


def test_registered_on_the_app_at_the_canonical_prefix():
    """The frontend calls exactly these paths; a mismatch renders as a clean,
    plausible, completely false zero-state instead of an error."""
    app_paths = {getattr(r, "path", None) for r in app.routes}
    assert f"{BASE}/scorecard" in app_paths
    assert f"{BASE}/export/scorecard" in app_paths
    # No alias may exist: two mounts is how the two sides drift silently.
    assert "/api/shadow/scorecard" not in app_paths
    assert "/api/admin/scorecard" not in app_paths


# ════════════════════════════════════════════════════════════════════════════
#  2. RBAC
# ════════════════════════════════════════════════════════════════════════════
def test_a_nurse_may_record_an_outcome_and_nothing_else():
    """The shadow surface is NOT uniformly admin-only, and the shape of the
    exception is the point.

    This test used to assert all seven endpoints returned 403 to a nurse. That
    stopped being right on 2026-08-02, when outcome capture was opened to the
    CHARGE NURSE — the only person on a ward who reliably knows a patient went to
    ICU at 06:20. Detection lead time, the pilot's headline metric, is undefined
    without that record and cannot be reconstructed afterwards
    (docs/SHADOW_MODE.md §5.1).

    So the boundary is deliberately asymmetric, and it is asserted here rather
    than merely relaxed — a test rewritten to `!= 403` would have hidden a real
    hole just as effectively as it unblocked the intended change:

      * WRITE the two outcome rows       — allowed. This is the nurse's job.
      * READ the scorecard, the episode  — denied. A ward nurse has no reason to
        queue, an episode, or the export     read the whole appliance's clinical
                                             state, and `export` is the
                                             de-identified artefact for the CIO.
      * ADJUDICATE an episode            — denied, and this is the load-bearing
                                             one. Adjudication decides whether an
                                             alert was a false alarm. If the ward
                                             being measured could grade its own
                                             alerts, the false-alert rate would
                                             measure nothing. It stays with the
                                             champion.
    """
    h = _nurse()

    reads = [
        client.get(f"{BASE}/scorecard", headers=h),
        client.get(f"{BASE}/episodes", headers=h),
        client.get(f"{BASE}/episode/ep_abc", headers=h),
        client.get(f"{BASE}/export/scorecard", headers=h),
    ]
    assert [r.status_code for r in reads] == [403] * 4, [r.status_code for r in reads]

    # The one thing a nurse MUST be able to do, or the pilot has no headline
    # metric — for a patient ON HER OWN WARD, which is the only kind she can
    # record since 2026-08-14 (see `_admit_to_actor_ward`).
    recorded = client.post(f"{BASE}/outcome", headers=h,
                           json={"patient_id": _admit_to_actor_ward("SHADOWTEST-NUR1"),
                                 "event_type": "icu_transfer"})
    confirmed = client.post(f"{BASE}/outcome/none", headers=h,
                            json={"ward": "A", "period_start": "2026-08-01T00:00:00Z",
                                  "period_end": "2026-08-01T08:00:00Z", "shift": "morning"})
    assert recorded.status_code == 201, recorded.text
    assert confirmed.status_code == 201, confirmed.text

    # Judging one's own ward is not part of the job.
    adjudicated = client.post(f"{BASE}/adjudication", headers=h,
                              json={"episode_id": "ep_abc", "verdict": "false_positive"})
    assert adjudicated.status_code == 403, (
        "a nurse adjudicated an episode — the false-alert rate is only meaningful "
        "if the ward being measured cannot grade its own alerts"
    )


def test_rbac_denies_unauthenticated():
    assert client.get(f"{BASE}/scorecard").status_code in (401, 403)
    assert client.get(f"{BASE}/export/scorecard").status_code in (401, 403)


# ════════════════════════════════════════════════════════════════════════════
#  3. Small-cell suppression, including complementary suppression
# ════════════════════════════════════════════════════════════════════════════
def test_suppression_fires_below_k():
    k = exporter.small_cell_k()
    assert k == 5, "k must track sage_service.AGGREGATE_SMALL_CELL"
    display, n = exporter.suppress_counts({"rare": 3, "big": 90, "bigger": 400})
    assert display["rare"] == "<5"
    # Complementary: masking one cell while publishing the total discloses it by
    # subtraction, so the smallest survivor is masked too.
    assert display["big"] == "<5"
    assert display["bigger"] == "400"
    assert n == 2


def test_suppression_leaves_safe_cells_and_zeroes_alone():
    display, n = exporter.suppress_counts({"a": 7, "b": 12})
    assert display == {"a": "7", "b": "12"} and n == 0
    # A zero discloses no individual, and hiding it would hide the most
    # important fact on a pilot scorecard: that a thing has not happened.
    display, n = exporter.suppress_counts({"deaths": 0, "transfers": 40})
    assert display["deaths"] == "0" and n == 0


def test_suppression_masks_everything_when_only_one_survivor():
    display, _ = exporter.suppress_counts({"a": 2, "b": 9})
    assert display == {"a": "<5", "b": "<5"}


def test_export_admits_when_masking_is_only_cosmetic():
    """A breakdown with one populated cell equals its own total, so the mask
    protects nothing. The export must say so rather than let its manifest claim
    a control it is not providing."""
    agg = {
        "instrument_present": True,
        "period": {"start": "2026-07-02T00:00:00.000+00:00",
                   "end": "2026-08-01T00:00:00.000+00:00", "days": 30},
        "events_total": 2, "episodes_total": 1, "corrections": 0,
        "by_kind": {"alert.fired": 2}, "by_lane": {"ingestion": 2},
        "by_ward": {"Ward 1": 2}, "ward_pseudonyms": {},
        "outcomes": {}, "outcome_sources": {}, "adjudications": {},
        "coverage": exporter._empty_coverage(30),
    }
    payload = exporter.build_export(30, None, aggregates=agg, actor_role="admin")
    supp = payload["provenance"]["suppression"]
    assert set(supp["masking_is_cosmetic_in"]) == {"by_kind", "by_lane", "by_ward"}
    assert supp["masking_note"] and "not for protection" in supp["masking_note"]
    # And with two populated cells the protection is real, so nothing is flagged.
    agg["by_kind"] = {"alert.fired": 2, "episode.opened": 40}
    payload = exporter.build_export(30, None, aggregates=agg, actor_role="admin")
    assert "by_kind" not in payload["provenance"]["suppression"]["masking_is_cosmetic_in"]


def test_export_refuses_to_certify_itself_on_a_tampered_appliance():
    """THE SELF-CERTIFICATION REGRESSION. `evidentiary_status.append_only` used
    to be a constant: the export asserted its own integrity unconditionally, so
    an appliance whose append-only triggers had been DROPPED and whose rows had
    been UPDATEd still produced a document calling itself evidentiary. An
    artefact whose only evidence of integrity is its own assertion of integrity
    is not evidence.

    Here the triggers are dropped, a row is genuinely rewritten, and the
    aggregation layer is made to LIE (`instrument={"append_only": True}`) so that
    the export cannot pass by trusting what it was handed. It must re-read
    sqlite_master itself and refuse.

    Replacing the three-branch computation in `_evidentiary_status` with
    `append_only = True` must fail this test. That mutation passed the whole
    suite before this test existed."""
    import migrations

    episode = _seed_phi_events()
    with db_session() as db:
        assert exporter._append_only(db) is True, "triggers must be present to start"

    try:
        with db_session() as db:
            db.execute("DROP TRIGGER IF EXISTS trg_shadow_no_update")
            db.execute("DROP TRIGGER IF EXISTS trg_shadow_no_delete")
        # Prove the appliance really is mutable now — otherwise this test would
        # pass on an appliance that was never actually tampered with.
        with db_session() as db:
            db.execute("UPDATE shadow_events SET correction_reason = 'tampered' "
                       "WHERE episode_id = ?", (episode,))
        with db_session() as db:
            assert exporter._append_only(db) is False
            rewritten = db.execute(
                "SELECT COUNT(*) AS n FROM shadow_events WHERE correction_reason = 'tampered'"
            ).fetchone()["n"]
        assert rewritten >= 1, "the UPDATE did not land; the tamper was not real"

        payload = exporter.build_export(
            30, None, scorecard=None, actor_role="admin",
            instrument={"append_only": True, "warnings": []})  # a lying caller

        status = payload["evidentiary_status"]
        assert status["append_only"] is False, "the export certified a tampered appliance"
        assert status["evidentiary"] is False
        assert status["status"] == "not_evidentiary"
        assert status["append_only_self_checked"] is False, "the export must re-read the triggers"
        assert status["append_only_reported_by_aggregation_layer"] is True
        assert "trg_shadow_no_update" in status["statement"]
        assert payload["instrument"]["append_only"] is False
        assert "NOTHING" in payload["instrument"]["append_only_enforced_by"]
    finally:
        with db_session() as db:
            migrations._m4_shadow_events(db)
            migrations._m18_shadow_none_confirmed_once(db)

    with db_session() as db:
        assert exporter._append_only(db) is True, "triggers were not restored"
    # …and with the instrument intact again the export goes back to certifying.
    restored = exporter.build_export(30, None, scorecard=None, actor_role="admin")
    assert restored["evidentiary_status"]["status"] == "evidentiary"
    assert restored["evidentiary_status"]["append_only"] is True


def test_export_will_not_certify_on_the_word_of_the_aggregation_layer_alone():
    """`_evidentiary_status` combines two independent readings and a False from
    EITHER wins. Pinned directly, because the combination is the whole control:
    a single-source check is one compromised caller away from being a constant."""
    f = exporter._evidentiary_status
    assert f(verified=False, claimed=True, instrument_present=True,
             instrument_warnings=[])["status"] == "not_evidentiary"
    assert f(verified=True, claimed=False, instrument_present=True,
             instrument_warnings=[])["status"] == "not_evidentiary"
    assert f(verified=None, claimed=None, instrument_present=True,
             instrument_warnings=[])["status"] == "unverified"
    assert f(verified=None, claimed=None, instrument_present=True,
             instrument_warnings=[])["append_only"] is None
    assert f(verified=True, claimed=True, instrument_present=False,
             instrument_warnings=[])["status"] == "no_instrument"
    assert f(verified=True, claimed=True, instrument_present=True,
             instrument_warnings=[])["status"] == "evidentiary"
    # The four statuses must not share prose, or a reader cannot tell them apart.
    statements = {f(v, c, p, [])["statement"]
                  for v, c, p in ((False, False, True), (None, None, True),
                                  (True, True, False), (True, True, True))}
    assert len(statements) == 4


def test_suppression_fires_in_a_real_export():
    # TWO rare kinds, because the assertion below is `cells_suppressed >= 2`.
    # Seeding only one produced a test that PASSED only when other shadow test
    # files had already left small cells behind, and FAILED when this file ran
    # alone — a test that depends on cross-file pollution is asserting the
    # suite's ordering, not the code's behaviour.
    #
    # Both on a ward PRIVATE TO THIS TEST, and the export scoped to it, for the
    # same reason and in the same direction: `sage.refused` and `sage.failed`
    # are written by the REAL Sage routes, so an appliance-wide export in a
    # full-suite run counted every refusal and failure the sage/adversarial
    # files had already produced, pushed both cells to or past k, and the mask
    # correctly stopped firing. The rows cannot be deleted (trg_shadow_no_delete
    # is what makes the export evidentiary), so the ward is the only boundary
    # available — cf. `_seam_setup`. The export below is still a real export
    # through the real aggregation; only its scope is pinned.
    ward = "Ward Suppression K"
    _seed_rare_kind(3, ward=ward)
    _seed_rare_kind(2, kind="sage.failed", ward=ward)
    payload = exporter.build_export(30, ward, scorecard=None, actor_role="admin")
    by_kind = payload["counts"]["by_kind"]
    assert by_kind.get("sage.refused") == "<5", by_kind
    assert payload["provenance"]["suppression"]["k"] == 5
    assert payload["provenance"]["suppression"]["cells_suppressed"] >= 2


# ════════════════════════════════════════════════════════════════════════════
#  4. Provenance
# ════════════════════════════════════════════════════════════════════════════
def test_provenance_block_is_complete():
    payload = exporter.build_export(30, None, scorecard=None, actor_role="admin")
    p = payload["provenance"]
    for key in ("orb_version", "exported_at", "exported_by_role", "period",
                "row_counts", "denominators", "coverage", "suppression",
                "removed", "limits", "integrity"):
        assert key in p, f"provenance is missing {key}"
    assert p["period"]["days"] == 30 and p["period"]["start"] < p["period"]["end"]
    assert p["integrity"]["algorithm"] == "sha256" and len(p["integrity"]["digest"]) == 64
    assert p["coverage"]["ward_shifts"] and "/" in p["coverage"]["ward_shifts"]
    # "what was removed" must be itemised, not a sentence.
    assert len(p["removed"]) >= 5
    assert {"field", "treatment", "note"} <= set(p["removed"][0])


def test_export_endpoint_returns_provenance_and_is_audited():
    h = _admin()
    before = _audit_count("shadow_scorecard_export")
    r = client.get(f"{BASE}/export/scorecard?days=30", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["export_type"] == "orb_shadow_scorecard_aggregate"
    assert "integrity" in body["provenance"]
    assert _audit_count("shadow_scorecard_export") == before + 1


def _audit_count(action_type: str) -> int:
    with db_session() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM audit_logs WHERE action_type = ?", (action_type,)
        ).fetchone()
    return int(row["n"] if row else 0)


def test_csv_export_carries_provenance_too():
    h = _admin()
    r = client.get(f"{BASE}/export/scorecard?days=30&format=csv", headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    text = r.text
    assert text.splitlines()[0] == "section,key,subkey,value"
    # An export that drops its provenance on the way to a spreadsheet has
    # stopped being evidence.
    assert "provenance" in text and "sha256" in text and "small-cell" in text


# ════════════════════════════════════════════════════════════════════════════
#  5. NO PHI IN THE EXPORT — asserted on the real bytes
# ════════════════════════════════════════════════════════════════════════════
def test_export_contains_no_phi_json():
    _seed_phi_events()
    h = _admin()
    r = client.get(f"{BASE}/export/scorecard?days=30", headers=h)
    assert r.status_code == 200, r.text
    raw = r.text  # the actual bytes that would leave the building
    for needle in (PHI_PATIENT, PHI_NAME, PHI_MRN, PHI_WARD, PHI_ACTOR):
        assert needle not in raw, f"PHI leaked into the JSON export: {needle!r}"
    # And the ward really was coarsened rather than simply absent. Events with
    # no ward keep their own label: inventing a "Ward n" for them would hide a
    # data-quality gap inside a plausible-looking row.
    counts = r.json()["counts"]["by_ward"]
    assert counts, "the ward breakdown must not silently vanish"
    assert all(str(w).startswith("Ward ") or w == "unattributed" for w in counts), counts
    assert any(str(w).startswith("Ward ") for w in counts), counts


def test_export_contains_no_phi_csv():
    _seed_phi_events()
    h = _admin()
    r = client.get(f"{BASE}/export/scorecard?days=30&format=csv", headers=h)
    assert r.status_code == 200, r.text
    for needle in (PHI_PATIENT, PHI_NAME, PHI_MRN, PHI_WARD, PHI_ACTOR):
        assert needle not in r.text, f"PHI leaked into the CSV export: {needle!r}"


def test_export_drops_identifier_fields_a_metric_smuggles_in():
    """The whitelist must fail CLOSED: a metric carrying `worst_case_patient_id`
    loses the field rather than exporting it."""
    _install_fake_service()
    try:
        h = _admin()
        r = client.get(f"{BASE}/export/scorecard?days=30", headers=h)
        assert r.status_code == 200, r.text
        raw = r.text
        assert PHI_PATIENT not in raw
        assert PHI_NAME not in raw
        assert "worst_case_patient_id" not in raw
        assert "ep_deadbeefdeadbeef" not in raw
        metrics = r.json()["scorecard"]["metrics"]
        assert metrics and all("patient_id" not in json.dumps(m) for m in metrics)
        # The legitimate aggregate fields survived.
        assert metrics[0]["denominator_label"] == "confirmed deterioration events"
    finally:
        _uninstall_fake_service()


def test_scrub_redacts_identifier_tokens_in_free_text():
    out = exporter._scrub({"caveat": "driven by P9412 in episode ep_deadbeefdeadbeef"})
    assert "P9412" not in out["caveat"] and "ep_deadbeef" not in out["caveat"]
    assert exporter.REDACTED in out["caveat"]


# ════════════════════════════════════════════════════════════════════════════
#  6. Empty dataset — never a bare 0 %
# ════════════════════════════════════════════════════════════════════════════
def test_export_on_an_empty_window_states_absence_not_zero():
    """days=1 on a fresh appliance: no metrics, but exact provenance."""
    payload = exporter.build_export(1, None, scorecard=None, actor_role="admin")
    sc = payload["scorecard"]
    assert sc["metrics"] == [] and sc["metrics_available"] is False
    assert sc["note"] and "did not supply metrics" in sc["note"]
    # Coverage is reported as uncovered time, not as clean time.
    assert payload["coverage"]["complete"] is False
    assert "uncovered" in payload["coverage"]
    assert payload["provenance"]["row_counts"]["shadow_events_current"] >= 0


def _metrics_of(body: dict) -> list:
    """`metrics` is a dict keyed by id in shadow_scorecard_service and a list in
    the client contract; both are legal, so the tests read both."""
    m = body.get("metrics")
    if isinstance(m, dict):
        return list(m.values())
    return m if isinstance(m, list) else []


def test_scorecard_never_reports_a_bare_zero_on_an_empty_pilot():
    """The single worst failure mode: "0 % false alerts" on morning one, having
    seen four alerts. A metric may only be `reported` with a real denominator
    and n at or above its floor."""
    _uninstall_fake_service()
    h = _admin()
    r = client.get(f"{BASE}/scorecard?days=30", headers=h)
    if r.status_code == 503:
        assert "shadow_scorecard_service" in r.json()["detail"]
        assert "nothing has been lost" in r.json()["detail"]
        return
    assert r.status_code == 200, r.text
    metrics = _metrics_of(r.json())
    assert metrics, "the scorecard must always describe its metrics, even empty"
    for m in metrics:
        assert m["state"] in ("reported", "insufficient_data",
                              "unadjudicated", "not_computable"), m
        if m["state"] == "reported":
            assert m.get("denominator"), m
            assert (m.get("n_observed") or 0) >= (m.get("n_required") or 0), m
        else:
            # Not reported ⇒ nothing printable is handed to the view.
            assert m.get("display") is None and m.get("value") is None, m
        assert m.get("denominator_label"), m


def test_scorecard_without_aggregation_layer_would_be_503_not_zeroes():
    """If the aggregation module is absent the endpoint must say so, not
    fabricate an empty scorecard."""
    real_service = shadow_routes._service
    shadow_routes._service = lambda: None   # the module is not on this appliance
    try:
        h = _admin()
        for path in (f"{BASE}/scorecard?days=30", f"{BASE}/episodes",
                     f"{BASE}/episode/ep_x"):
            r = client.get(path, headers=h)
            assert r.status_code == 503, (path, r.text)
            assert "shadow_scorecard_service" in r.json()["detail"]
            assert "nothing has been lost" in r.json()["detail"]
    finally:
        shadow_routes._service = real_service


def test_missing_aggregation_module_cannot_take_the_server_down():
    """The import is lazy on purpose: a module-level import of an ABSENT
    analytics module would propagate through main.py and take every clinical
    route down with it."""
    src = open(shadow_routes.__file__).read()
    assert "from services import shadow_scorecard_service" in src
    top_level = [ln for ln in src.splitlines()
                 if ln.startswith("from services import shadow_scorecard_service")]
    assert top_level == [], "the aggregation import must stay inside a function"


def test_export_still_works_without_the_aggregation_layer():
    """An appliance with no metrics module still exports its instrument volume,
    coverage and provenance — and says the metrics are absent."""
    real_service = shadow_routes._service
    shadow_routes._service = lambda: None
    try:
        h = _admin()
        r = client.get(f"{BASE}/export/scorecard?days=30", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["scorecard"]["metrics_available"] is False
        assert "integrity" in body["provenance"]
    finally:
        shadow_routes._service = real_service


def test_export_carries_real_metrics_from_the_aggregation_layer():
    """End-to-end with whatever `shadow_scorecard_service` actually returns —
    including its dict-shaped `metrics` — and still no PHI."""
    _uninstall_fake_service()
    _seed_phi_events()
    h = _admin()
    r = client.get(f"{BASE}/export/scorecard?days=30", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    metrics = body["scorecard"]["metrics"]
    if body["scorecard"]["metrics_available"]:
        assert isinstance(metrics, list) and metrics
        ids = {m.get("id") for m in metrics}
        assert ids <= {"detection_lead_time", "false_alert_rate", "agreement", "time_saved"}
        # The free-form `detail` bag must never be forwarded into the artefact.
        assert all("detail" not in m for m in metrics), metrics
    for needle in (PHI_PATIENT, PHI_NAME, PHI_MRN, PHI_WARD, PHI_ACTOR):
        assert needle not in r.text, f"PHI leaked into the JSON export: {needle!r}"


def test_scorecard_reports_instrument_health():
    _install_fake_service()
    try:
        h = _admin()
        r = client.get(f"{BASE}/scorecard?days=30", headers=h)
        assert r.status_code == 200, r.text
        inst = r.json()["instrument"]
        # A silent write failure must not read as "no events happened".
        assert "process_stats" in inst and "dropped_events" in inst
        assert "healthy" in inst
        exp = r.json()["export"]
        assert exp["available"] is True and set(exp["formats"]) == {"json", "csv"}
        assert exp["deidentification"], "the client refuses an export with no manifest"
    finally:
        _uninstall_fake_service()


# ════════════════════════════════════════════════════════════════════════════
#  7. Blinding
# ════════════════════════════════════════════════════════════════════════════
def test_adjudication_queue_is_blinded():
    _install_fake_service()
    try:
        h = _admin()
        r = client.get(f"{BASE}/episodes?status=pending", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["blinded"] is True
        # Assert on the ROWS, not the whole body: the blinding note legitimately
        # names the fields it withholds.
        rows = json.dumps(body["episodes"])
        for leak in ("critical", "score", "drivers", "orb_verdict", "raw_news2",
                     "confidence", "Consider ICU review"):
            assert leak not in rows, f"Orb's verdict leaked into the blinded queue: {leak!r}"
        row = body["episodes"][0]
        assert row["episode_id"] == "ep_1111222233334444"
        assert row["events_n"] == 6 and row["blinded"] is True

        # Already adjudicated → nothing left to contaminate, so unblinded.
        r2 = client.get(f"{BASE}/episodes?status=adjudicated", headers=h)
        assert r2.status_code == 200 and r2.json()["blinded"] is False
    finally:
        _uninstall_fake_service()


def test_episode_detail_is_ordered_and_unblinded():
    _install_fake_service()
    try:
        h = _admin()
        r = client.get(f"{BASE}/episode/ep_1111222233334444", headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["events"][0]["kind"] == "alert.fired"
    finally:
        _uninstall_fake_service()


# ════════════════════════════════════════════════════════════════════════════
#  8. Human entry (ground truth + adjudication)
# ════════════════════════════════════════════════════════════════════════════
def test_post_outcome_writes_a_shadow_row():
    h = _admin()
    r = client.post(f"{BASE}/outcome", headers=h, json={
        "patient_id": _admit_to_actor_ward("SHADOWTEST-P9413"),
        "event_type": "icu_transfer", "source": "witnessed",
    })
    assert r.status_code == 201, r.text
    row_id = r.json()["shadow_event_id"]
    with db_session() as db:
        row = db.execute(
            "SELECT kind, payload_json FROM shadow_events_current WHERE id = ?", (row_id,)
        ).fetchone()
    assert row and row["kind"] == "outcome.recorded"
    assert json.loads(row["payload_json"])["source"] == "witnessed"


def test_post_outcome_rejects_free_text_event_types():
    h = _admin()
    r = client.post(f"{BASE}/outcome", headers=h,
                    json={"patient_id": "P9414", "event_type": "got worse"})
    assert r.status_code == 422 and "event_type must be one of" in r.text


def test_post_outcome_rejects_unknown_source():
    h = _admin()
    r = client.post(f"{BASE}/outcome", headers=h, json={
        "patient_id": "P9415", "event_type": "death", "source": "guessed"})
    assert r.status_code == 422 and "source must be one of" in r.text


def test_shift_confirmation_is_idempotent_per_ward_shift():
    """Resubmitting a shift must thread onto the SAME coverage episode — or the
    coverage numerator counts submissions instead of shifts — AND must not
    write a second row.

    THE REGRESSION (migration 18 + the route no-op). The deterministic
    `cov_<sha256>` episode id got the first half right and hid the second: both
    submissions threaded onto one episode, so this test passed, while each
    resubmission APPENDED an identical `outcome.none_confirmed` row that
    trg_shadow_no_delete then made permanent. The resubmit now answers 200
    with the ORIGINAL confirmation ("already confirmed"), never 201 twice and
    never an error — the shift-status fail-soft note tells a blind nurse to
    "confirm anyway; a duplicate confirmation is deduplicated", and that
    sentence has to be true."""
    h = _admin()
    body = {"ward": "Ward A", "period_start": "2026-08-01T00:00:00Z",
            "period_end": "2026-08-01T08:00:00Z", "shift": "morning"}
    r1 = client.post(f"{BASE}/outcome/none", headers=h, json=body)
    r2 = client.post(f"{BASE}/outcome/none", headers=h, json=body)
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["episode_id"] == r2.json()["episode_id"]
    assert r1.json()["episode_id"].startswith("cov_")
    dup = r2.json()
    assert dup["already_confirmed"] is True and dup["recorded"] is False
    assert dup["shadow_event_id"] == r1.json()["shadow_event_id"]
    assert dup["confirmed_at"], dup
    with db_session() as db:
        n = db.execute(
            "SELECT COUNT(*) AS n FROM shadow_events "
            "WHERE kind = 'outcome.none_confirmed' AND episode_id = ?",
            (r1.json()["episode_id"],),
        ).fetchone()["n"]
    assert n == 1, f"resubmission appended a duplicate confirmation row (rows={n})"


def test_duplicate_confirmation_is_refused_by_the_database_not_only_the_route():
    """idx_shadow_once covers outcome.none_confirmed (migration 18), so even a
    writer that never passes through the route's already-confirmed check — the
    losing side of two concurrent submissions, or a future direct caller of
    shadow_service.emit — cannot land a second row for the same ward-shift."""
    ward = "Ward Once"
    episode = shadow_routes._coverage_episode_id(ward, "2026-08-01T00:00:00Z", "night")
    payload = {"ward": ward, "period_start": "2026-08-01T00:00:00Z",
               "period_end": "2026-08-02T07:00:00Z", "shift": "night"}
    first = shadow_service.emit("outcome.none_confirmed", episode, "manual",
                                ward=ward, payload=payload)
    assert first is not None, "fixture confirmation refused"
    second = shadow_service.emit("outcome.none_confirmed", episode, "manual",
                                 ward=ward, payload=payload)
    assert second is None, "idx_shadow_once no longer covers outcome.none_confirmed"
    with db_session() as db:
        n = db.execute(
            "SELECT COUNT(*) AS n FROM shadow_events WHERE episode_id = ?", (episode,)
        ).fetchone()["n"]
    assert n == 1, f"the unique index let a duplicate confirmation through (rows={n})"


def test_a_lost_race_on_the_index_reports_already_confirmed_not_503(monkeypatch):
    """Two confirmations in flight for one ward-shift: the loser's pre-check
    sees nothing, its emit is then refused by idx_shadow_once. That refusal
    must read back as "already confirmed" — the 503 is reserved for a down
    instrument, and a nurse told the confirmation FAILED when it is durably
    recorded would either submit again for ever or stop trusting the screen."""
    h = _admin()
    ward = "Ward Race"
    body = {"ward": ward, "period_start": "2026-08-01T00:00:00Z",
            "period_end": "2026-08-01T08:00:00Z", "shift": "morning"}
    # The "other" request's row is already durable by the time this one emits.
    episode = shadow_routes._coverage_episode_id(ward, body["period_start"], "morning")
    assert shadow_service.emit(
        "outcome.none_confirmed", episode, "manual", ward=ward,
        payload={"ward": ward, "period_start": body["period_start"],
                 "period_end": body["period_end"], "shift": "morning"},
    ) is not None

    real = shadow_routes._existing_confirmation
    calls = {"n": 0}

    def raced(episode_id):
        calls["n"] += 1
        # The loser's pre-check ran before the winner's row landed.
        return None if calls["n"] == 1 else real(episode_id)

    monkeypatch.setattr(shadow_routes, "_existing_confirmation", raced)
    r = client.post(f"{BASE}/outcome/none", headers=h, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["already_confirmed"] is True
    assert calls["n"] == 2, "the route never re-read after the refused emit"


def test_confirmation_still_503s_when_the_instrument_is_down(monkeypatch):
    """The no-op path must not soften the fail-loud rule: with the instrument
    disabled and NO existing confirmation, the nurse is still told the
    confirmation was NOT recorded — this ward-shift really will count as
    uncovered time."""
    monkeypatch.setenv("ORB_SHADOW_ENABLED", "false")
    r = client.post(f"{BASE}/outcome/none", headers=_admin(), json={
        "ward": "Ward Down", "period_start": "2026-08-01T00:00:00Z",
        "period_end": "2026-08-01T08:00:00Z", "shift": "morning"})
    assert r.status_code == 503, r.text
    assert "NOT recorded" in r.json()["detail"]


def test_shift_status_reports_the_confirmation_with_who_and_when():
    """The read half of the capture screen: an outstanding current shift, then
    — after the POST — "already confirmed by <role> at <when>", matched on the
    exact period strings the endpoint itself handed out. Also pins the closed
    vocabularies the entry form offers, so the UI cannot invent an event type
    the metric would discard."""
    h = _admin()
    ward = "Ward Status"
    r = client.get(f"{BASE}/outcome/shift-status", params={"ward": ward}, headers=h)
    assert r.status_code == 200, r.text
    status = r.json()
    assert status["status_available"] is True
    assert status["event_types"] == list(shadow_routes.OUTCOME_EVENT_TYPES)
    assert status["sources"] == list(shadow_routes.OUTCOME_SOURCES)
    assert status["recognition"] == list(shadow_routes.OUTCOME_RECOGNITION)
    current = next(w for w in status["shifts"] if w["is_current"])
    before = current["wards"][0]
    assert before["ward"] == ward
    assert before["confirmed"] is False and before["status"] == "outstanding"

    posted = client.post(f"{BASE}/outcome/none", headers=h, json={
        "ward": ward, "period_start": current["period_start"],
        "period_end": current["period_end"], "shift": current["shift"]})
    assert posted.status_code == 201, posted.text

    r2 = client.get(f"{BASE}/outcome/shift-status", params={"ward": ward}, headers=h)
    current2 = next(w for w in r2.json()["shifts"] if w["is_current"])
    after = current2["wards"][0]
    assert after["confirmed"] is True and after["status"] == "confirmed"
    assert after["confirmed_at"], after
    assert after["confirmed_by_role"] == "admin"


def test_shift_confirmation_moves_coverage():
    """REGRESSION: a shift confirmed a few minutes before the shift formally
    ends must still count. Stamping `occurred_at` at `period_end` put the row in
    the future, so `occurred_at < now` excluded it from every window and the
    ward-shift silently read as uncovered — coverage would be understated for
    exactly the shift most recently confirmed."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    h = _admin()
    r = client.post(f"{BASE}/outcome/none", headers=h, json={
        "ward": "Ward Cover",
        "period_start": now.strftime("%Y-%m-%dT00:00:00Z"),
        "period_end": (now + timedelta(hours=2)).isoformat(),  # deliberately future
        "shift": "night"})
    assert r.status_code == 201, r.text
    payload = exporter.build_export(30, None, scorecard=None, actor_role="admin")
    assert payload["coverage"]["covered"] >= 1
    assert payload["coverage"]["display"].split("/")[0] != "0"


def test_backfilled_confirmation_does_not_inflate_a_window_it_is_outside():
    """Fetched by when it was submitted, counted by which shift it describes."""
    h = _admin()
    before = exporter.build_export(30, None, scorecard=None, actor_role="admin")
    r = client.post(f"{BASE}/outcome/none", headers=h, json={
        "ward": "Ward Ancient", "period_start": "2020-01-01T00:00:00Z",
        "period_end": "2020-01-01T08:00:00Z", "shift": "morning"})
    assert r.status_code == 201, r.text
    after = exporter.build_export(30, None, scorecard=None, actor_role="admin")
    assert after["coverage"]["covered"] == before["coverage"]["covered"]


def test_post_adjudication_writes_and_keeps_artefact_verdicts():
    h = _admin()
    episode = _seed_phi_events()
    r = client.post(f"{BASE}/adjudication", headers=h,
                    json={"episode_id": episode, "verdict": "artefact",
                          "blinded": True, "review_seconds": 45})
    assert r.status_code == 201, r.text
    assert r.json()["verdict"] == "artefact"
    with db_session() as db:
        row = db.execute(
            "SELECT payload_json FROM shadow_events_current "
            "WHERE episode_id = ? AND kind = 'adjudication.recorded'", (episode,)
        ).fetchone()
    assert row and json.loads(row["payload_json"])["verdict"] == "artefact"


def test_post_adjudication_rejects_unknown_verdict():
    h = _admin()
    r = client.post(f"{BASE}/adjudication", headers=h,
                    json={"episode_id": "ep_x", "verdict": "probably fine"})
    assert r.status_code == 422 and "verdict must be one of" in r.text


# ════════════════════════════════════════════════════════════════════════════
#  9. THE SEAM: the route's vocabulary and the scorecard's are the SAME one
# ════════════════════════════════════════════════════════════════════════════
# THE TEST THAT WAS MISSING. Every test above this line either stubs the
# aggregation layer (`_FakeScorecardService`) or reads the scorecard without
# writing an adjudication through the route. Both halves were honestly built and
# individually tested; neither was ever executed against the other, and the seam
# between them silently destroyed 100 % of clinician adjudications:
#
#   route wrote  payload_json.verdict = 'false_positive'
#   scorecard    recognised only 'false_alert', ... -> 'unrecognised'
#   result       adjudicated = 0, state = 'unadjudicated', value = None, FOR EVER
#
# No exception. No log line. Nothing on the face of the scorecard said the
# verdicts had been thrown away. So: NO FAKE SERVICE BELOW THIS LINE. These tests
# POST through the real route and read the real `shadow_scorecard_service`.
from services import shadow_verdicts  # noqa: E402
from services import shadow_scorecard_service as real_sc  # noqa: E402

def _seam_setup(monkeypatch, ward: str, episodes: list[tuple[str, int]]) -> None:
    """Real alerts on a ward PRIVATE TO ONE TEST, plus the census and coverage
    the denominators need. `episodes` is [(episode_id, n_alerts), ...].

    The ward is the isolation boundary: `shadow_events` rows are undeletable by
    design (`trg_shadow_no_delete` is what makes the export evidentiary), so a
    shared ward would let one test's alerts become another's denominator.
    """
    from datetime import datetime, timedelta, timezone
    _uninstall_fake_service()
    monkeypatch.setenv("ORB_SCORECARD_SHIFTS", "00:00-24:00")
    monkeypatch.setenv("ORB_SCORECARD_MIN_N_FALSE_ALERT_RATE", "1")
    monkeypatch.delenv("ORB_SHADOW_SCORECARD_INCLUDE_SIMULATOR", raising=False)

    now = datetime.now(timezone.utc)
    patient = f"SEAM-{abs(hash(ward)) % 100000}"
    with db_session() as db:
        db.execute(
            "INSERT OR REPLACE INTO patients (id, name, mrn, ward, admission_date, status) "
            "VALUES (?, ?, ?, ?, ?, 'admitted')",
            (patient, "Seam Patient", f"MRN-{patient}", ward,
             (now - timedelta(days=5)).isoformat()),
        )
    for episode, n_alerts in episodes:
        for j in range(n_alerts):
            assert shadow_service.emit(
                "alert.fired", episode, "ingestion",
                occurred_at=(now - timedelta(hours=3, minutes=j)).isoformat(),
                patient_id=patient, ward=ward,
                payload={"level": "critical", "raw_news2": 9},
            ) is not None, "fixture alert refused"
    # Charge-nurse coverage, or every alert above is excluded as uncovered time.
    r = client.post(f"{BASE}/outcome/none", headers=_admin(), json={
        "ward": ward,
        "period_start": (now - timedelta(hours=30)).isoformat(),
        "period_end": now.isoformat(), "shift": "day"})
    assert r.status_code == 201, r.text


def _seam_metric(ward: str) -> dict:
    r = client.get(f"{BASE}/scorecard?days=1&ward={ward}", headers=_admin())
    assert r.status_code == 200, r.text
    metrics = _metrics_of(r.json())
    return next(m for m in metrics if m["id"] == "false_alert_rate")


def test_adjudication_posted_through_the_route_reaches_the_real_scorecard(monkeypatch):
    """THE REGRESSION. Six verdicts in through `/adjudication`, six counted by
    the scorecard the same API serves. Previously: adjudicated=0,
    unrecognised_verdicts=6, state='unadjudicated', value=None."""
    ward = "Ward Seam Vocab"
    episodes = [(f"ep_seam_a{i}", 1) for i in range(6)]
    _seam_setup(monkeypatch, ward, episodes)
    h = _admin()
    for i, (episode, _) in enumerate(episodes):
        r = client.post(f"{BASE}/adjudication", headers=h, json={
            "episode_id": episode,
            "verdict": "false_positive" if i < 4 else "true_positive",
            "blinded": True})
        assert r.status_code == 201, r.text

    metric = _seam_metric(ward)
    detail = metric["detail"]
    assert detail["unrecognised_verdicts"] == 0, (
        "the route's vocabulary is not the scorecard's — Defect 1 has returned")
    assert detail["adjudicated"] == 6 and detail["false"] == 4 and detail["true"] == 2
    assert metric["adjudication"]["adjudicated"] == 6
    assert metric["state"] == "reported"
    assert metric["value"] is not None and metric["display"]


def test_every_verdict_the_route_accepts_is_classified_by_the_scorecard():
    """The two-sided property, asserted directly rather than sampled: nothing
    the API will store may be unrecognised on read, and nothing the reader
    recognises may be un-postable."""
    for verdict in shadow_routes.ADJUDICATION_VERDICTS:
        assert real_sc._classify_verdict(verdict) in ("true", "false", "excluded"), verdict
    for verdict in shadow_verdicts.ACCEPTED_VERDICTS:
        assert shadow_verdicts.normalise(verdict) in shadow_routes.ADJUDICATION_VERDICTS
        assert real_sc._classify_verdict(verdict) != "unrecognised", verdict
    # And there is exactly ONE definition: the route holds no list of its own.
    assert shadow_routes.ADJUDICATION_VERDICTS is shadow_verdicts.CANONICAL_VERDICTS


def test_historical_verdict_spellings_are_accepted_and_stored_canonically(monkeypatch):
    """Rows and integrations predating the unification used `false_alert`. They
    must still count — and must be written in ONE spelling, so the next reader
    does not need the synonym table to get the right answer."""
    ward = "Ward Seam History"
    _seam_setup(monkeypatch, ward, [("ep_seam_hist", 1)])
    r = client.post(f"{BASE}/adjudication", headers=_admin(), json={
        "episode_id": "ep_seam_hist", "verdict": "FALSE_ALERT", "blinded": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["verdict"] == "false_positive"
    assert body["verdict_as_submitted"] == "FALSE_ALERT"
    assert body["counts_as"] == "false"
    with db_session() as db:
        row = db.execute(
            "SELECT payload_json FROM shadow_events_current "
            "WHERE episode_id = 'ep_seam_hist' AND kind = 'adjudication.recorded'"
        ).fetchone()
    assert json.loads(row["payload_json"])["verdict"] == "false_positive"
    assert _seam_metric(ward)["detail"]["false"] == 1


def test_scorecard_headline_counts_alerts_not_episodes_end_to_end(monkeypatch):
    """DEFECT 2 THROUGH THE API. One episode, ten alerts, adjudicated false.
    `unit` says "false alerts per occupied bed-day", so the numerator must be
    10 — the interruptions — not 1, the episode."""
    ward = "Ward Seam Units"
    _seam_setup(monkeypatch, ward, [("ep_seam_many", 10)])
    r = client.post(f"{BASE}/adjudication", headers=_admin(), json={
        "episode_id": "ep_seam_many", "verdict": "false_positive", "blinded": True})
    assert r.status_code == 201, r.text

    metric = _seam_metric(ward)
    assert metric["unit"] == "false alerts per occupied bed-day"
    assert metric["detail"]["false_episode_count"] == 1
    assert metric["detail"]["false_alert_count"] == 10
    assert metric["numerator"] == 10, "the headline is counting episodes again"
    assert metric["display"].endswith("false alerts per occupied bed-day")
    # Both denominators are on the artefact, and only one is primary.
    labels = [a["label"] for a in metric["alternative_denominators"]]
    primary = [a for a in metric["alternative_denominators"] if a.get("is_primary")]
    assert len(primary) == 1 and "ALERTS" in primary[0]["label"]
    assert any("EPISODES" in lab for lab in labels)


def test_adjudication_queue_publishes_the_vocabulary_the_scorecard_counts():
    """The UI offers what `/episodes` lists. If that list ever contains a verdict
    the metric discards, clinicians adjudicate into a void again."""
    _uninstall_fake_service()
    r = client.get(f"{BASE}/episodes?status=pending", headers=_admin())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdicts"] == list(shadow_verdicts.CANONICAL_VERDICTS)
    for item in body["verdict_meanings"]:
        assert item["counts_as"] == real_sc._classify_verdict(item["verdict"])
        assert item["counts_as"] in ("true", "false", "excluded")
