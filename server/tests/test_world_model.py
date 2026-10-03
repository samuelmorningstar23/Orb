"""Persistent world-model tests: temporal trajectory computation, versioning,
and at-rest field encryption of the PHI-derived state.
"""
import importlib
import json

import pytest

from database import db_session


@pytest.fixture()
def worsening_patient():
    pid = "PAT-WM-1"
    with db_session() as db:
        db.execute("DELETE FROM world_model_state WHERE entity_id = ?", (pid,))
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM risk_scores WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM patients WHERE id = ?", (pid,))
        db.execute(
            """INSERT INTO patients (id, mrn, name, age, sex, bed, ward, diagnosis, status)
               VALUES (?, 'MRN-WM-1', 'Ravi Kumar', 60, 'M', 'ICU-3', 'ICU', 'Pneumonia', 'admitted')""",
            (pid,),
        )
        # SpO2 falling, RR rising over 5 readings (oldest→newest inserted with
        # increasing timestamps so ORDER BY timestamp DESC gives newest first).
        spo2 = [98, 96, 94, 92, 90]
        rr = [16, 18, 20, 24, 28]
        hr = [80, 84, 88, 95, 102]
        for i in range(5):
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, spo2, respiratory_rate, temperature)
                   VALUES (?, ?, ?, ?, ?, 37.5)""",
                (pid, f"2026-07-07T10:0{i}:00+00:00", hr[i], spo2[i], rr[i]),
            )
        db.execute(
            "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) VALUES (?, '2026-07-07T10:00:00+00:00', 3, 'stable', '[]')",
            (pid,),
        )
        db.execute(
            "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) VALUES (?, '2026-07-07T10:05:00+00:00', 7, 'critical', '[]')",
            (pid,),
        )
    yield pid
    with db_session() as db:
        db.execute("DELETE FROM world_model_state WHERE entity_id = ?", (pid,))
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM risk_scores WHERE patient_id = ?", (pid,))
        db.execute("DELETE FROM patients WHERE id = ?", (pid,))


def test_computes_worsening_trajectory(worsening_patient):
    from services.world_model_service import compute_patient_state
    st = compute_patient_state(worsening_patient)
    assert st is not None
    assert st["trajectory"] == "worsening"
    assert st["current_risk_level"] == "critical"
    # SpO2 falling, RR + HR rising.
    assert st["vital_trends"]["spo2"]["direction"] == "falling"
    assert st["vital_trends"]["respiratory_rate"]["direction"] == "rising"
    assert st["vital_trends"]["heart_rate"]["direction"] == "rising"


def test_trajectory_line_is_scoped_summary(worsening_patient):
    from services.world_model_service import trajectory_line
    line = trajectory_line(worsening_patient)
    assert "worsening" in line.lower()
    assert "spo2 falling" in line.lower()


def test_versioning_increments(worsening_patient):
    from services.world_model_service import update_patient_state, get_patient_state
    v1 = update_patient_state(worsening_patient, reason="test1")
    v2 = update_patient_state(worsening_patient, reason="test2")
    assert v2["_version"] == v1["_version"] + 1
    latest = get_patient_state(worsening_patient, recompute_if_missing=False)
    assert latest["_version"] == v2["_version"]


@pytest.fixture()
def unscored_crowd():
    """More deteriorating-but-UNSCORED admitted patients than the ward list holds.

    TEST-ISOLATION LAW: these are `status='admitted'` with a worsening vitals
    series and the session DB is shared, so they are deleted on teardown. This
    fixture exists to reproduce, in one file, the crowding that used to arrive
    by accident from other modules' leftovers.
    """
    from services.world_model_service import _WARD_WORSENING_LIMIT
    ids = [f"PAT-WM-CROWD-{i:02d}" for i in range(_WARD_WORSENING_LIMIT + 2)]
    spo2 = [98, 96, 94, 92, 90]
    rr = [16, 18, 20, 24, 28]
    hr = [80, 84, 88, 95, 102]
    with db_session() as db:
        for n, pid in enumerate(ids):
            db.execute(
                """INSERT INTO patients (id, mrn, name, age, sex, bed, ward, diagnosis, status)
                   VALUES (?, ?, ?, 55, 'F', ?, 'ICU', 'Pneumonia', 'admitted')""",
                (pid, f"MRN-WM-CROWD-{n:02d}", f"Crowd {n}", f"ICU-C{n}"),
            )
            for i in range(5):
                db.execute(
                    """INSERT INTO vitals (patient_id, timestamp, heart_rate, spo2,
                       respiratory_rate, temperature) VALUES (?, ?, ?, ?, ?, 37.5)""",
                    (pid, f"2026-07-07T10:0{i}:00+00:00", hr[i], spo2[i], rr[i]),
                )
    yield ids
    marks = ",".join("?" * len(ids))
    with db_session() as db:
        db.execute(f"DELETE FROM vitals WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM risk_scores WHERE patient_id IN ({marks})", ids)
        db.execute(f"DELETE FROM world_model_state WHERE entity_id IN ({marks})", ids)
        db.execute(f"DELETE FROM patients WHERE id IN ({marks})", ids)


def test_ward_state_counts_worsening(worsening_patient):
    from services.world_model_service import compute_ward_state
    st = compute_ward_state()
    assert st["census"] >= 1
    assert st["worsening_count"] >= 1
    assert any(w["patient_id"] == worsening_patient for w in st["worsening"])


def _assert_scanned_last(patient_id):
    """The bug only manifests when the raw scan reaches `patient_id` after the
    list is already full, so assert that precondition rather than trusting it —
    a repro that silently stops reproducing is worse than no repro. Fixture
    argument order is what puts the crowd's rows ahead of this one.
    """
    from services.world_model_service import _WARD_WORSENING_LIMIT
    with db_session() as db:
        scan = [r["id"] for r in db.execute(
            "SELECT id FROM patients WHERE status = 'admitted'"
        ).fetchall()]
    assert scan.index(patient_id) >= _WARD_WORSENING_LIMIT, (
        f"precondition lost: {patient_id} is at scan position "
        f"{scan.index(patient_id)}, so an unranked cut would still have kept it"
    )


def test_ward_worsening_list_is_ranked_by_severity(unscored_crowd, worsening_patient):
    """The truncated list must be ordered most-urgent-first.

    Without this the cut keeps an arbitrary slice of the deteriorating — see
    compute_ward_state's docstring.
    """
    from services.world_model_service import _LEVEL_RANK, compute_ward_state
    _assert_scanned_last(worsening_patient)
    # Score the BACK half of the crowd critical. In raw scan order the kept
    # window is then "unscored first, critical after" — monotonic only if the
    # list is actually ranked.
    with db_session() as db:
        for pid in unscored_crowd[len(unscored_crowd) // 2:]:
            db.execute(
                "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
                "VALUES (?, '2026-07-07T10:00:00+00:00', 3, 'stable', '[]')", (pid,))
            db.execute(
                "INSERT INTO risk_scores (patient_id, timestamp, score, level, drivers) "
                "VALUES (?, '2026-07-07T10:05:00+00:00', 8, 'critical', '[]')", (pid,))
    listed = compute_ward_state()["worsening"]
    ranks = [_LEVEL_RANK.get(w["level"], 0) for w in listed]
    assert ranks == sorted(ranks, reverse=True), (
        f"worsening list is not severity-ordered: {[w['level'] for w in listed]}"
    )


def test_critical_patient_is_not_crowded_out_by_unscored_ones(
    unscored_crowd, worsening_patient
):
    """A `critical` patient must survive the truncation even when more
    deteriorating-but-unscored patients exist than the list can hold.

    This is the regression that kept arriving from a distance: the ward list is
    capped, the scan order is SQLite's rowid order, and an uncapped
    `worsening_count` made the omission invisible. Five test modules grew
    bespoke cleanup fixtures for the symptom before the ordering was fixed.
    """
    from services.world_model_service import compute_ward_state
    _assert_scanned_last(worsening_patient)
    st = compute_ward_state()
    assert st["worsening_count"] > len(st["worsening"])  # the cut is in play
    listed = [w["patient_id"] for w in st["worsening"]]
    assert worsening_patient in listed, (
        f"critical patient dropped from a {len(listed)}-entry list while "
        f"worsening_count={st['worsening_count']}; listed={listed}"
    )


def test_state_encrypted_at_rest_when_key_set(worsening_patient, monkeypatch):
    # Turn field encryption on and persist — the stored blob must be ciphertext.
    monkeypatch.setenv("ORB_FIELD_ENCRYPTION_KEY", "wm-test-key-xyz")
    import services.field_crypto as fc
    importlib.reload(fc)
    if not fc._HAVE_AESGCM:
        pytest.skip("cryptography not installed")
    import services.world_model_service as wm
    importlib.reload(wm)

    st = wm.update_patient_state(worsening_patient, reason="enc-test")
    with db_session() as db:
        row = db.execute(
            "SELECT state_json, encrypted FROM world_model_state WHERE entity_id=? ORDER BY version DESC LIMIT 1",
            (worsening_patient,),
        ).fetchone()
    assert row["encrypted"] == 1
    assert row["state_json"].startswith("ORBF1:")
    assert "Ravi Kumar" not in row["state_json"]  # PHI not in cleartext
    # Round-trips back through decryption on read.
    loaded = wm.get_patient_state(worsening_patient, recompute_if_missing=False)
    assert loaded["name"] == "Ravi Kumar"

    # Restore modules to ambient env for other tests.
    monkeypatch.delenv("ORB_FIELD_ENCRYPTION_KEY", raising=False)
    importlib.reload(fc)
    importlib.reload(wm)
