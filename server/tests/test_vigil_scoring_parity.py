"""ONE deterioration score per patient — the doctor's chart and the nurse's board.

THE DEFECT (fixed 2026-08-14). Orb had two scoring paths over the same vitals row:

  * ``vigil_service.get_patient_risk`` — the doctor-facing chart. NEWS2 baseline
    PLUS the CfC liquid-neural-network overlay, which may ESCALATE the level
    (never lower it) when the learned model flags high near-term risk.
  * ``nurse_shift_service._score_from_vitals`` — the nurse's shift board. Called
    ``compute_news2`` directly and stopped there. No overlay, ever.

Today no CfC checkpoint ships, so ``compute_cfc_risk`` returns None and the two
paths agree by accident. Nothing was red. The day a checkpoint lands in
``server/models/`` — which is a file drop, not a code change, and therefore not
something a code review would catch — the chart would read 'critical' and the
ward board would read 'stable' FOR THE SAME PATIENT AND THE SAME ROW. Silently.

That is the whole reason this file exists: a latent divergence that turns on
without anyone editing a line of Python, and with no existing test that would go
red when it does. So these tests STUB the overlay active. A parity test that only
exercises the state where the overlay is switched off is a test that certifies
the bug.

THE INVARIANT: for one patient and one stored vitals row, every displayed
scoring path returns the same dict — same aggregate, same level, same drivers,
same overlay fields — because they all go through the one entry point,
``vigil_service.score_vitals_row``.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import db_session
from services import nurse_shift_service, vigil_service

PID = "PAT-PARITY-1"
MRN = "MRN-PARITY-1"

# RR 22 (2) + SpO2 93 (2) + SBP 118 (0) + HR 96 (1) + T 38.5 (1) + alert (0)
# + on air (0) = 6. Deliberately BELOW the 7 that bands 'critical' on the RCP
# table, so an overlay escalation is unambiguously the overlay's doing.
VITALS = dict(heart_rate=96.0, systolic_bp=118.0, diastolic_bp=74.0, spo2=93.0,
              temperature=38.5, respiratory_rate=22.0, consciousness="alert",
              on_oxygen=0)


@pytest.fixture
def parity_patient():
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO patients (id, name, mrn, status) VALUES (?, ?, ?, ?)",
            (PID, "Parity Test", MRN, "admitted"),
        )
        db.execute("UPDATE patients SET on_scale_2 = 0 WHERE id = ?", (PID,))
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (PID,))
        db.execute(
            """INSERT INTO vitals
                   (patient_id, timestamp, heart_rate, systolic_bp, diastolic_bp,
                    spo2, temperature, respiratory_rate, consciousness, on_oxygen,
                    source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (PID, "2026-08-14T08:00:00+00:00", VITALS["heart_rate"],
             VITALS["systolic_bp"], VITALS["diastolic_bp"], VITALS["spo2"],
             VITALS["temperature"], VITALS["respiratory_rate"],
             VITALS["consciousness"], VITALS["on_oxygen"], "nurse_observation"),
        )
    yield PID
    with db_session() as db:
        db.execute("DELETE FROM vitals WHERE patient_id = ?", (PID,))


def _stored_row(patient_id: str) -> dict:
    """The row exactly as ``nurse_shift_service._load_board_rows`` hands it over."""
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM vitals WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
    return dict(row)


def _on_scale_2(patient_id: str) -> bool:
    with db_session() as db:
        row = db.execute(
            "SELECT on_scale_2 FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
    return bool(row["on_scale_2"])


@pytest.fixture
def cfc_overlay_high(monkeypatch):
    """Stub a LOADED CfC ensemble reporting high near-term deterioration risk.

    0.92 clears the >= 0.9 rung, so the overlay escalates the baseline all the
    way to 'critical'. Patched at ``compute_cfc_risk`` — the seam every scoring
    path shares — so no torch checkpoint is needed to exercise the state this
    whole file exists to protect.
    """
    calls: list[str] = []

    def _fake(patient_id: str):
        calls.append(patient_id)
        return {
            "outcomes": {
                "deterioration": {
                    "risk_by_horizon": {"1": 0.81, "3": 0.88, "6": 0.92,
                                        "12": 0.90, "24": 0.77},
                    "peak_risk": 0.92,
                },
            },
            "primary_risk": 0.92,
            "seq_len": 12,
            "model": "CfC-LNN",
        }

    monkeypatch.setattr(vigil_service, "compute_cfc_risk", _fake)
    return calls


# ═══════════════════════════════════════════════════════════════════════════
#  Parity with the overlay OFF (today's shipping state)
# ═══════════════════════════════════════════════════════════════════════════

def test_both_paths_agree_with_no_checkpoint_loaded(parity_patient, monkeypatch):
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", lambda pid: None)

    chart = vigil_service.get_patient_risk(parity_patient)
    board = nurse_shift_service._score_from_vitals(
        _stored_row(parity_patient), _on_scale_2(parity_patient)
    )

    assert chart == board
    assert chart["raw_news2"] == 6
    assert chart["level"] == "elevated"
    assert "cfc" not in chart


# ═══════════════════════════════════════════════════════════════════════════
#  Parity with the overlay ON — the state that was never covered
# ═══════════════════════════════════════════════════════════════════════════

def test_both_paths_agree_with_the_overlay_active(parity_patient, cfc_overlay_high):
    """THE test. With a checkpoint loaded, the chart and the board must still
    return the same dict — including the escalated level and every overlay
    field."""
    chart = vigil_service.get_patient_risk(parity_patient)
    board = nurse_shift_service._score_from_vitals(
        _stored_row(parity_patient), _on_scale_2(parity_patient)
    )

    assert chart == board, (
        "the doctor's chart and the nurse's board disagree about this patient "
        f"— chart {chart.get('level')!r} vs board {board.get('level')!r}"
    )
    assert len(cfc_overlay_high) == 2, "one of the two paths never ran the overlay"


def test_the_overlay_escalates_on_both_paths_not_just_the_chart(parity_patient,
                                                               cfc_overlay_high):
    """Named separately from the dict-equality test on purpose. Equality alone
    would still pass if BOTH paths silently lost the overlay; this pins the
    clinical outcome — a NEWS2-6 'elevated' patient the model flags at 0.92 is
    shown as 'critical' on the ward board too, not only on the chart."""
    chart = vigil_service.get_patient_risk(parity_patient)
    board = nurse_shift_service._score_from_vitals(
        _stored_row(parity_patient), _on_scale_2(parity_patient)
    )

    for name, result in (("chart", chart), ("board", board)):
        assert result["raw_news2"] == 6, f"{name} changed the RCP aggregate"
        assert result["level"] == "critical", (
            f"{name} did not apply the CfC escalation — a patient the model puts "
            f"at 0.92 near-term risk is being shown as {result['level']!r}"
        )
        assert result["scoring_model"] == "NEWS2+CfC-LNN", name
        assert result["cfc_primary_risk"] == 0.92, name
        assert result["drivers"][0].startswith("CfC:"), name


def test_the_overlay_never_lowers_the_baseline(parity_patient, monkeypatch):
    """A quiet model must not talk a sick patient down. NEWS2 is the floor."""
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", lambda pid: {
        "outcomes": {"deterioration": {"risk_by_horizon": {"1": 0.02}, "peak_risk": 0.02}},
        "primary_risk": 0.02, "seq_len": 12, "model": "CfC-LNN",
    })

    chart = vigil_service.get_patient_risk(parity_patient)
    board = nurse_shift_service._score_from_vitals(
        _stored_row(parity_patient), _on_scale_2(parity_patient)
    )
    assert chart == board
    assert chart["level"] == "elevated", "the overlay de-escalated the NEWS2 baseline"


# ═══════════════════════════════════════════════════════════════════════════
#  The structural guarantee: one entry point, not two implementations
# ═══════════════════════════════════════════════════════════════════════════

def test_the_board_scores_through_the_shared_entry_point(parity_patient, monkeypatch):
    """A grep-shaped guard on the seam itself.

    Dict equality proves the two paths AGREE today. This proves they agree
    BECAUSE they are the same code — so a future edit that reintroduces a direct
    ``compute_news2`` call on the board fails here immediately, rather than
    waiting for a checkpoint to land to become visible.
    """
    seen: list[dict] = []
    real = vigil_service.score_vitals_row

    def _spy(vitals, **kwargs):
        seen.append(kwargs)
        return real(vitals, **kwargs)

    monkeypatch.setattr(vigil_service, "score_vitals_row", _spy)
    monkeypatch.setattr(nurse_shift_service, "score_vitals_row", _spy)

    vigil_service.get_patient_risk(parity_patient)
    nurse_shift_service._score_from_vitals(
        _stored_row(parity_patient), _on_scale_2(parity_patient)
    )

    assert len(seen) == 2, "a scoring path bypassed vigil_service.score_vitals_row"
    for kwargs in seen:
        assert kwargs.get("patient_id") == parity_patient, (
            "a displayed score was computed without a patient_id, so the CfC "
            "overlay could never run for it"
        )
        assert kwargs.get("overlay", True) is True


def test_a_patient_with_no_vitals_yields_no_score_on_the_board():
    """None in, None out — the board renders 'no obs', it does not invent a
    stable patient out of an empty row."""
    assert nurse_shift_service._score_from_vitals(None, False) is None
    assert vigil_service.score_vitals_row(None, patient_id="anything") is None


# ═══════════════════════════════════════════════════════════════════════════
#  The entry point carries the coverage truth through, unaltered
# ═══════════════════════════════════════════════════════════════════════════

def test_a_stored_row_with_no_acvpu_scores_incomplete_on_both_paths(parity_patient,
                                                                    monkeypatch):
    """A monitor-written row (NULL consciousness, NULL on_oxygen) must not be
    reported as a complete seven-parameter NEWS2 by either surface."""
    monkeypatch.setattr(vigil_service, "compute_cfc_risk", lambda pid: None)
    with db_session() as db:
        db.execute(
            """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                                   spo2, temperature, respiratory_rate,
                                   consciousness, on_oxygen, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)""",
            (parity_patient, "2026-08-14T09:00:00+00:00", 96.0, 118.0, 93.0,
             38.5, 22.0, "ingested_hl7"),
        )

    chart = vigil_service.get_patient_risk(parity_patient)
    board = nurse_shift_service._score_from_vitals(
        _stored_row(parity_patient), _on_scale_2(parity_patient)
    )

    assert chart == board
    assert chart["raw_news2"] == 6, "coverage honesty must not move a single point"
    assert chart["complete"] is False
    assert set(chart["missing_parameters"]) == {"Consciousness (ACVPU)", "Supplemental O2"}
