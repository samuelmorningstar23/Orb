"""The score and the band on a Sage risk block come from ONE computation.

THE DEFECT THESE PIN (found on screen, 2026-08-14). A clinician had a drafted
assessment reading "NEWS2 10 ELEVATED" sitting two inches below a header reading
"NEWS2 9 · CRITICAL". Measured across the live census, 20 of 80 rows (25%)
displayed a band that disagreed with the score printed beside it.

`sage_service._fresh_risk_block` returned `news2`, `drivers` and the
per-parameter breakdown from a FRESH `compute_news2` call, and `score` and
`level` from the newest — possibly much older — `risk_scores` row. Two moments
in time, presented as one fact, in one dict, on one card. Its own docstring
claimed the block "cannot contradict itself".

`_assemble_patient_context` built the same shape with the same split, so the
prompt line `format_patient_channel_a` renders —

    RISK SCORE: 0.1 (stable) — [...]
    NEWS2 (computed by Orb's deterministic engine — AUTHORITATIVE...): 19

— stated a band and an aggregate that cannot both be true, to the model, on
every non-cached patient turn and in the chart-open prewarm.

THE RULE NOW IMPLEMENTED (`sage_service._reconciled_band`): the band is the one
the fresh computation itself produced, unless the stored row is STRICTLY MORE
SEVERE — the CfC overlay may escalate the NEWS2 baseline and that escalation
lives on the stored row. The stored row can raise the band; it can never lower
it. These tests are the whole of that rule:

  * table-driven across the NEWS2 range and BOTH SpO2 scales, against every
    possible stored band: score, aggregate, breakdown and band always come from
    the one call, and the band is never below what the fresh score implies;
  * the overlay can still escalate;
  * a stale stored row cannot contradict a fresh score — including the exact
    two numbers the clinician saw.
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import db_session                                    # noqa: E402
from services import sage_service                                  # noqa: E402
from services.sage_service import (_assemble_patient_context,      # noqa: E402
                                   _fresh_risk_block,
                                   _reconciled_band,
                                   format_patient_channel_a)
from services.vigil_service import compute_news2                   # noqa: E402

BANDS = ("stable", "elevated", "critical")


# ─────────────────────────────────────────────────────────────────────────────
#  The RCP band rule, restated INDEPENDENTLY of compute_news2.
#  If this drifts from the engine, one of the two is wrong and the suite says so
#  — that is the point of writing it out a second time here.
# ─────────────────────────────────────────────────────────────────────────────
def _band_from_aggregate(news2: int, single_param_3: bool) -> str:
    if news2 >= 7:
        return "critical"
    if news2 >= 5 or single_param_3:
        return "elevated"
    return "stable"


# ─────────────────────────────────────────────────────────────────────────────
#  Cases. `expect_news2` / `expect_single3` are hand-computed from the RCP
#  table (see the band table in test_news2.py), NOT read back off the engine.
# ─────────────────────────────────────────────────────────────────────────────
#   label, vitals, scale2, expected aggregate, expected single-param-3
CASES = [
    # ── SpO2 Scale 1 ────────────────────────────────────────────────────────
    ("quiet",
     dict(heart_rate=70, systolic_bp=120, respiratory_rate=16, temperature=37.0,
          spo2=98, consciousness="alert", on_oxygen=0),
     False, 0, False),
    ("single-parameter 3 only (RR 6) — 3 points but NOT stable",
     dict(heart_rate=70, systolic_bp=120, respiratory_rate=6, temperature=37.0,
          spo2=98, consciousness="alert", on_oxygen=0),
     False, 3, True),
    ("four ones and a two = 5, the elevated threshold",
     dict(heart_rate=95, systolic_bp=120, respiratory_rate=22, temperature=38.5,
          spo2=94, consciousness="alert", on_oxygen=0),
     False, 5, False),
    ("6 — inside the band, not on either edge",
     dict(heart_rate=95, systolic_bp=120, respiratory_rate=22, temperature=38.5,
          spo2=93, consciousness="alert", on_oxygen=0),
     False, 6, False),
    ("7 — the critical threshold exactly",
     dict(heart_rate=115, systolic_bp=120, respiratory_rate=22, temperature=38.5,
          spo2=93, consciousness="alert", on_oxygen=0),
     False, 7, False),
    ("10 — the number on the clinician's card",
     dict(heart_rate=122, systolic_bp=88, respiratory_rate=22, temperature=38.5,
          spo2=93, consciousness="alert", on_oxygen=0),
     False, 10, True),
    ("20 — peri-arrest, every parameter maxed",
     dict(heart_rate=140, systolic_bp=80, respiratory_rate=32, temperature=34.5,
          spo2=80, consciousness="voice", on_oxygen=1),
     False, 20, True),
    # ── SpO2 Scale 2 (COPD / chronic type-2 respiratory failure) ────────────
    # SpO2 90 on air is IN TARGET on Scale 2 (0 points) and severe hypoxia on
    # Scale 1 (3 points). The band has to follow the scale the patient is on,
    # from the same call that scored them.
    ("scale 2, SpO2 90 on air — in target, quiet",
     dict(heart_rate=70, systolic_bp=120, respiratory_rate=16, temperature=37.0,
          spo2=90, consciousness="alert", on_oxygen=0),
     True, 0, False),
    ("scale 2, SpO2 82 — severe hypoxia even on the COPD scale",
     dict(heart_rate=70, systolic_bp=120, respiratory_rate=16, temperature=37.0,
          spo2=82, consciousness="alert", on_oxygen=0),
     True, 3, True),
    ("scale 2, over-oxygenated on O2 — 3 for SpO2 plus 2 for the oxygen",
     dict(heart_rate=95, systolic_bp=120, respiratory_rate=22, temperature=37.0,
          spo2=98, consciousness="alert", on_oxygen=1),
     True, 8, True),
]


_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: this module seeds status='admitted' patients, and the
    session DB is shared. Left admitted they are not a cosmetic leak — 66 of them
    pushed test_world_model's own patient out of compute_ward_state's
    worsening[:10] and failed a test in a different file (2026-08-14). The same
    class already bit test_command_center.py; see its teardown, which this
    mirrors. Discharge rather than DELETE: the vitals rows written during the
    test legitimately reference these ids. Teardown runs AFTER the test, so the
    within-test assertions still see their own patients."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _SEEDED],
        )
    _SEEDED.clear()


def _mk_patient(scale2: bool = False) -> str:
    pid = f"RBC{uuid.uuid4().hex[:10].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, age, sex, diagnosis, "
            "status, admission_date, on_scale_2) "
            "VALUES (?, ?, ?, ?, 'Risk Block Ward', 71, 'F', 'Community-acquired pneumonia', "
            "'admitted', ?, ?)",
            (pid, f"Riskblock {pid[-4:]}", f"MRN-{uuid.uuid4().hex[:10]}",
             f"RB-{pid[-3:]}", now, int(bool(scale2))),
        )
    _SEEDED.append(pid)
    return pid


def _chart(patient_id: str, vitals: dict, *, when=None):
    """Chart ONE observation set — the fresh truth."""
    ts = (when or datetime.now(timezone.utc)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, heart_rate, systolic_bp, diastolic_bp, "
            "spo2, temperature, respiratory_rate, consciousness, on_oxygen, timestamp) "
            "VALUES (?, ?, ?, 70, ?, ?, ?, ?, ?, ?)",
            (patient_id, vitals["heart_rate"], vitals["systolic_bp"], vitals["spo2"],
             vitals["temperature"], vitals["respiratory_rate"],
             vitals["consciousness"], vitals["on_oxygen"], ts),
        )


def _stale_row(patient_id: str, level: str, score=0.1, hours_ago=6):
    """The risk_scores row a background pipeline wrote hours ago and nothing has
    rewritten since — the simulator lane's normal state."""
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with db_session() as db:
        db.execute(
            "INSERT INTO risk_scores (patient_id, score, level, drivers, timestamp) "
            "VALUES (?, ?, ?, '[]', ?)",
            (patient_id, score, level, ts),
        )


def _fresh(patient_id: str) -> dict:
    with db_session() as db:
        return _fresh_risk_block(db, patient_id)


def _context_risk(patient_id: str) -> dict:
    with db_session() as db:
        return (_assemble_patient_context(db, patient_id, None) or {})["risk"]


def _expected(vitals: dict, scale2: bool) -> dict:
    return compute_news2(vitals["heart_rate"], vitals["systolic_bp"],
                         vitals["respiratory_rate"], vitals["temperature"],
                         vitals["spo2"], vitals["consciousness"],
                         bool(vitals["on_oxygen"]), on_scale_2=scale2)


# ═════════════════════════════════════════════════════════════════════════════
#  (a) THE RULE ITSELF, as a pure function
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("fresh", BANDS)
@pytest.mark.parametrize("stored", BANDS)
def test_the_band_is_the_worse_of_fresh_and_stored_never_the_stored_one_alone(fresh, stored):
    """Escalate-only, stated as a table over every (fresh, stored) pair."""
    got = _reconciled_band(fresh, stored)
    expected = BANDS[max(BANDS.index(fresh), BANDS.index(stored))]
    assert got == expected, (
        f"fresh={fresh!r} stored={stored!r} → {got!r}; the stored row may only "
        f"ever raise the band, so this must be {expected!r}")


@pytest.mark.parametrize("stored", [None, "", "  ", "STABLE", "unknown", "sTaBlE", 3])
def test_an_absent_or_unrecognised_stored_band_cannot_lower_a_fresh_critical(stored):
    """A row with no usable band is not evidence of anything, least of all
    improvement. Case and whitespace must not turn a real band into an
    unrecognised one either."""
    assert _reconciled_band("critical", stored) == "critical"


def test_a_recognisable_stored_band_still_escalates_whatever_its_casing():
    assert _reconciled_band("stable", " CRITICAL ") == "critical"
    assert _reconciled_band("stable", "Elevated") == "elevated"


def test_with_no_fresh_band_the_stored_one_is_better_than_nothing():
    """A block with no band at all is worse than a stale band — the fallback is
    deliberate, and it is the ONLY direction the stored row wins by default."""
    assert _reconciled_band(None, "elevated") == "elevated"
    assert _reconciled_band(None, None) is None


# ═════════════════════════════════════════════════════════════════════════════
#  (b) THE BLOCK — across the NEWS2 range, both scales, every stored band
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("stored_band", BANDS)
@pytest.mark.parametrize("label,vitals,scale2,expect_news2,expect_single3", CASES,
                         ids=[c[0] for c in CASES])
def test_score_and_band_come_from_one_computation(label, vitals, scale2,
                                                  expect_news2, expect_single3,
                                                  stored_band):
    pid = _mk_patient(scale2=scale2)
    _stale_row(pid, stored_band)
    _chart(pid, vitals)

    block = _fresh(pid)
    engine = _expected(vitals, scale2)
    fresh_band = _band_from_aggregate(expect_news2, expect_single3)

    # The hand-computed expectation and the engine must agree before anything
    # else is asserted — otherwise the rest of this test is checking the block
    # against a number that is itself wrong.
    assert engine["raw_news2"] == expect_news2, (
        f"{label}: the RCP table says {expect_news2}, compute_news2 says "
        f"{engine['raw_news2']}")
    assert engine["level"] == fresh_band, f"{label}: engine band disagrees with the RCP rule"

    assert block["news2"] == expect_news2, f"{label}: the aggregate is not the fresh one"
    assert block["score"] == engine["score"], (
        f"{label}: the normalized index is {block['score']} while the observation "
        f"scores {engine['score']} — it is being read off the stored row")
    assert block["news2_scale"] == (2 if scale2 else 1), f"{label}: wrong SpO2 scale"
    assert block["news2_breakdown"] == engine["breakdown"], f"{label}: breakdown is not the fresh one"
    assert block["drivers"] == engine["drivers"], f"{label}: drivers are not the fresh one"

    # THE INVARIANT. One computation → the band beside the score is the band
    # that score implies, unless the stored row escalated it.
    expected_band = BANDS[max(BANDS.index(fresh_band), BANDS.index(stored_band))]
    assert block["level"] == expected_band, (
        f"{label}: NEWS2 {block['news2']} displayed at band {block['level']!r} "
        f"with a stored row saying {stored_band!r} — expected {expected_band!r}")
    assert BANDS.index(block["level"]) >= BANDS.index(fresh_band), (
        f"{label}: the displayed band {block['level']!r} is BELOW what NEWS2 "
        f"{block['news2']} implies ({fresh_band!r}) — the score has been "
        f"de-escalated by a stored row")


@pytest.mark.parametrize("stored_band", BANDS)
@pytest.mark.parametrize("label,vitals,scale2,expect_news2,expect_single3", CASES,
                         ids=[c[0] for c in CASES])
def test_the_assembled_chart_block_obeys_the_same_rule(label, vitals, scale2,
                                                       expect_news2, expect_single3,
                                                       stored_band):
    """`_assemble_patient_context` builds the same shape on the NON-cached turn
    and in the chart-open prewarm. It had the same split and needs the same
    guarantee — a contradiction here reaches the model, not just the card."""
    pid = _mk_patient(scale2=scale2)
    _stale_row(pid, stored_band)
    _chart(pid, vitals)

    risk = _context_risk(pid)
    engine = _expected(vitals, scale2)
    fresh_band = _band_from_aggregate(expect_news2, expect_single3)
    expected_band = BANDS[max(BANDS.index(fresh_band), BANDS.index(stored_band))]

    assert risk["news2"] == expect_news2, label
    assert risk["score"] == engine["score"], label
    assert risk["level"] == expected_band, (
        f"{label}: chart context shows NEWS2 {risk['news2']} at band "
        f"{risk['level']!r}, expected {expected_band!r}")


# ═════════════════════════════════════════════════════════════════════════════
#  (c) THE OVERLAY MAY STILL ESCALATE
# ═════════════════════════════════════════════════════════════════════════════

def test_a_stored_critical_still_escalates_a_quiet_fresh_score():
    """The CfC overlay's whole purpose: a patient the RCP table calls stable
    whom the learned ensemble puts at high near-term risk. That escalation is
    persisted on the risk_scores row, and deriving the band from the fresh
    computation must not throw it away."""
    pid = _mk_patient()
    _stale_row(pid, "critical", score=0.9)
    _chart(pid, CASES[0][1])            # NEWS2 0

    block = _fresh(pid)
    assert block["news2"] == 0
    assert block["level"] == "critical", (
        "the CfC escalation carried on the stored row was discarded — a patient "
        "the model flagged is being shown as stable")
    # The numbers stay the fresh ones. The overlay escalates the BAND; it does
    # not rewrite the RCP aggregate or its decomposition (vigil_service does
    # exactly this and no more).
    assert block["news2_breakdown"] == _expected(CASES[0][1], False)["breakdown"]


def test_a_stored_elevated_escalates_a_stable_score_but_not_past_itself():
    pid = _mk_patient()
    _stale_row(pid, "elevated")
    _chart(pid, CASES[0][1])            # NEWS2 0
    assert _fresh(pid)["level"] == "elevated"


# ═════════════════════════════════════════════════════════════════════════════
#  (d) A STALE ROW CANNOT CONTRADICT A FRESH SCORE — the numbers on the ward
# ═════════════════════════════════════════════════════════════════════════════

def test_the_card_the_clinician_saw_news2_10_at_elevated():
    """The exact defect, in the exact numbers: a draft reading "NEWS2 10
    ELEVATED" under a header reading "NEWS2 9 · CRITICAL"."""
    pid = _mk_patient()
    _stale_row(pid, "elevated")                        # the header's older truth
    _chart(pid, CASES[5][1])                           # deteriorated to NEWS2 10

    block = _fresh(pid)
    assert block["news2"] == 10
    assert block["level"] == "critical", (
        f"NEWS2 {block['news2']} is displayed at band {block['level']!r} — the "
        f"aggregate and the band on the same card disagree")


def test_the_simulator_lane_news2_20_at_stable():
    """admission_service writes one risk_scores row at admission; the simulator
    (`bot_service._simulate_tick`) writes vitals and nothing else. A patient
    admitted quiet and then deteriorated is stored 'stable' for ever — and this
    is the demo a champion watches."""
    pid = _mk_patient()
    _stale_row(pid, "stable", score=0.1, hours_ago=6)
    _chart(pid, CASES[6][1])                           # NEWS2 20, peri-arrest

    block = _fresh(pid)
    assert block["news2"] == 20
    assert block["level"] == "critical", (
        f"a peri-arrest observation is wearing a {block['level']!r} badge")


def test_a_newer_observation_re_bands_the_patient_without_a_new_risk_row():
    """Deterioration between two observations, with the stored row untouched —
    the ordinary ward case behind the 25%."""
    pid = _mk_patient()
    _stale_row(pid, "stable")
    _chart(pid, CASES[0][1], when=datetime.now(timezone.utc) - timedelta(hours=3))
    assert _fresh(pid)["level"] == "stable"

    _chart(pid, CASES[4][1])                           # now NEWS2 7
    block = _fresh(pid)
    assert block["news2"] == 7
    assert block["level"] == "critical"


def test_the_rendered_prompt_never_states_a_band_below_its_own_news2():
    """The renderer is where the contradiction became visible: the RISK line
    (it was `RISK SCORE: x (band)` until 2026-10-03, see
    test_news2_prompt_label.py) and the AUTHORITATIVE NEWS2 are consecutive
    lines of the same prompt. Whatever the stored row says, those two lines
    must be able to hold at the same time."""
    pid = _mk_patient()
    _stale_row(pid, "stable")
    _chart(pid, CASES[6][1])                           # NEWS2 20

    with db_session() as db:
        facts = _assemble_patient_context(db, pid, None)
    lines = format_patient_channel_a(facts, pid)
    risk_line = next(ln for ln in lines if ln.startswith("RISK:"))
    news2_line = next(ln for ln in lines if ln.startswith("NEWS2 (computed"))

    assert "NEWS2 20 (critical)" in risk_line, (
        f"prompt says {risk_line!r} directly above {news2_line!r}")
    # The stale row's 0-1 index (0.1) is not in the prompt at all.
    assert "0.1" not in risk_line, risk_line
    assert news2_line.rstrip().endswith("20")


def test_the_docstring_no_longer_claims_something_untrue():
    """It claimed the block "cannot contradict itself" while it did, on a
    quarter of the ward. A false comment is worse than none: it is the reason
    nobody looked here for a year."""
    doc = sage_service._fresh_risk_block.__doc__ or ""
    assert "ONE `compute_news2` call" in doc
    assert "_reconciled_band" in doc
