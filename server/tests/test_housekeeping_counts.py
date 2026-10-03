"""Housekeeping numbers that used to disagree or overflow (gap review F155).

  * the board's first tile counted open REQUESTS under a caption about beds, and
    a bed marked dirty with no request was on no queue and in no count;
  * coverage was registered over capacity with no ceiling, so a stale bed-count
    setting printed "125%".
"""
import uuid

from services import housekeeping_service as hk


def _unit(ward, state):
    suffix = uuid.uuid4().hex[:6].upper()
    return hk.register_unit(
        unit_code=f"HKC-{suffix}", ward=ward, bed_label=f"B{suffix}",
        initial_state=state, actor_user_id="HKCOUNTS",
    )


def test_a_dirty_bed_with_no_request_is_counted_and_one_with_a_request_is_not():
    ward = f"HKC Ward {uuid.uuid4().hex[:6].upper()}"
    before = hk.overview(sync=False)["counts"]["dirty_without_request"]
    _unit(ward, "vacant_dirty")                       # nobody asked to clean it
    asked = _unit(ward, "vacant_dirty")
    hk.raise_cleaning_request(unit_id=asked["id"], request_type="routine", actor_user_id="HKCOUNTS")

    counts = hk.overview(sync=False)["counts"]
    assert counts["dirty_without_request"] == before + 1


def test_more_beds_registered_than_the_capacity_setting_is_explained_not_overflowed(monkeypatch):
    ward = f"HKC Over {uuid.uuid4().hex[:6].upper()}"
    monkeypatch.setattr(hk, "_ward_capacity_map", lambda: {ward: 2})
    for _ in range(3):
        _unit(ward, "vacant_ready")

    row = next(w for w in hk.ward_readiness(ward=ward)["wards"] if w["ward"] == ward)
    assert row["coverage_pct"] == 100.0
    assert row["over_capacity"] is True
    assert "3 beds are registered here" in row["coverage_note"]
    assert "bed count is 2" in row["coverage_note"]


def test_a_ward_within_capacity_carries_no_note(monkeypatch):
    ward = f"HKC Under {uuid.uuid4().hex[:6].upper()}"
    monkeypatch.setattr(hk, "_ward_capacity_map", lambda: {ward: 4})
    _unit(ward, "vacant_ready")

    row = next(w for w in hk.ward_readiness(ward=ward)["wards"] if w["ward"] == ward)
    assert row["coverage_pct"] == 25.0
    assert row["over_capacity"] is False
    assert row["coverage_note"] is None
