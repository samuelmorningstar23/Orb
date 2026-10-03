"""What-If Capacity & Surge Simulator — service tests.

Covers the contract shape, projection length, monotone response to the surge and
bed knobs, graceful empty-DB behaviour, and strict determinism.
"""
from services import capacity_sim_service


# A hermetic baseline used by most tests so results don't depend on DB contents.
def _base(**overrides):
    b = {
        "baseline_census": 20,
        "baseline_capacity": 32,
        "admission_rate_daily": 6.0,
    }
    b.update(overrides)
    return b


# ── contract / length ──────────────────────────────────────────────────────
def test_baseline_projection_length_and_contract():
    horizon = 24
    result = capacity_sim_service.simulate(_base(), horizon_hours=horizon)

    # Top-level contract keys
    for key in ("baseline", "scenario", "projection", "summary", "assumptions"):
        assert key in result, f"missing top-level key: {key}"

    # Projection includes the t0 baseline step → horizon + 1 entries.
    assert len(result["projection"]) == horizon + 1
    assert result["projection"][0]["hour_offset"] == 0
    assert result["projection"][-1]["hour_offset"] == horizon

    # Every step reports occupancy, capacity and overflow.
    for step in result["projection"]:
        assert "projected_occupancy" in step
        assert "capacity" in step
        assert "overflow" in step

    # Summary contract
    for key in (
        "peak_occupancy_pct", "hours_to_overflow", "beds_short_at_peak",
        "suggested_discharges", "staffing_gap_nurses",
    ):
        assert key in result["summary"]

    # Baseline echoes the census/capacity we asked for.
    assert result["baseline"]["census"] == 20
    assert result["baseline"]["capacity"] == 32


def test_horizon_reflected_in_length():
    for h in (12, 48, 72):
        r = capacity_sim_service.simulate(_base(), horizon_hours=h)
        assert len(r["projection"]) == h + 1


# ── surge raises peak occupancy ─────────────────────────────────────────────
def test_extra_admissions_raises_peak_occupancy():
    baseline = capacity_sim_service.simulate(_base(), horizon_hours=24)
    surged = capacity_sim_service.simulate(
        _base(extra_admissions=20), horizon_hours=24
    )
    assert surged["summary"]["peak_occupancy"] > baseline["summary"]["peak_occupancy"]
    assert surged["summary"]["peak_occupancy_pct"] > baseline["summary"]["peak_occupancy_pct"]


def test_admission_rate_multiplier_raises_peak():
    baseline = capacity_sim_service.simulate(_base(), horizon_hours=48)
    flu = capacity_sim_service.simulate(
        _base(admission_rate_multiplier=3.0), horizon_hours=48
    )
    assert flu["summary"]["peak_occupancy"] > baseline["summary"]["peak_occupancy"]


# ── closing beds reduces capacity and can cause overflow ────────────────────
def test_closing_beds_reduces_capacity_and_causes_overflow():
    # Start near-full, then close 8 beds → capacity drops below census → overflow.
    scenario = _base(baseline_census=30, baseline_capacity=32, beds_delta=-8)
    result = capacity_sim_service.simulate(scenario, horizon_hours=24)

    assert result["scenario"]["effective_capacity"] == 24  # 32 - 8
    assert result["projection"][0]["capacity"] == 24
    # Census 30 already exceeds the reduced 24-bed capacity → immediate overflow.
    assert result["summary"]["hours_to_overflow"] == 0
    assert result["summary"]["beds_short_at_peak"] >= 6
    assert result["summary"]["suggested_discharges"] >= 6


def test_opening_beds_raises_capacity():
    r = capacity_sim_service.simulate(_base(beds_delta=10), horizon_hours=12)
    assert r["scenario"]["effective_capacity"] == 42  # 32 + 10


# ── staffing gap responds to surge ──────────────────────────────────────────
def test_staffing_gap_grows_with_surge():
    calm = capacity_sim_service.simulate(_base(), horizon_hours=24)
    surge = capacity_sim_service.simulate(
        _base(extra_admissions=40), horizon_hours=24
    )
    assert surge["summary"]["staffing_gap_nurses"] >= calm["summary"]["staffing_gap_nurses"]
    assert surge["summary"]["staffing_gap_nurses"] > 0


# ── empty / no-scenario / real-DB path doesn't crash ────────────────────────
def test_empty_scenario_uses_live_baseline_without_crashing():
    # No overrides → pulls the baseline from forecast_service against the (test)
    # DB. Must not raise and must return a well-formed, non-negative projection.
    result = capacity_sim_service.simulate({}, horizon_hours=24)
    assert len(result["projection"]) == 25
    assert result["baseline"]["census"] >= 0
    assert result["baseline"]["capacity"] >= 0
    for step in result["projection"]:
        assert step["projected_occupancy"] >= 0


def test_none_scenario_does_not_crash():
    result = capacity_sim_service.simulate(None, horizon_hours=6)
    assert len(result["projection"]) == 7


def test_zero_capacity_baseline_does_not_divide_by_zero():
    r = capacity_sim_service.simulate(
        _base(baseline_census=0, baseline_capacity=0), horizon_hours=12
    )
    assert r["summary"]["peak_occupancy_pct"] is None or isinstance(
        r["summary"]["peak_occupancy_pct"], (int, float)
    )


# ── determinism ─────────────────────────────────────────────────────────────
def test_determinism_same_input_same_output():
    scenario = _base(
        extra_admissions=15,
        beds_delta=-4,
        admission_rate_multiplier=1.5,
        los_multiplier=1.2,
        discharge_boost=0.25,
        now="2026-07-04T20:00:00Z",
    )
    a = capacity_sim_service.simulate(dict(scenario), horizon_hours=48)
    b = capacity_sim_service.simulate(dict(scenario), horizon_hours=48)
    assert a == b


def test_determinism_on_live_baseline():
    # Even without overrides the output must be stable across calls.
    a = capacity_sim_service.simulate({"extra_admissions": 5}, horizon_hours=24)
    b = capacity_sim_service.simulate({"extra_admissions": 5}, horizon_hours=24)
    assert a == b


# ── discharge boost reduces occupancy ───────────────────────────────────────
def test_discharge_boost_lowers_peak():
    baseline = capacity_sim_service.simulate(
        _base(extra_admissions=30), horizon_hours=48
    )
    boosted = capacity_sim_service.simulate(
        _base(extra_admissions=30, discharge_boost=0.8), horizon_hours=48
    )
    assert boosted["summary"]["peak_occupancy"] <= baseline["summary"]["peak_occupancy"]
