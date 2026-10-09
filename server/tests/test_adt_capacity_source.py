"""Capacity screens from the bed register (adt-14, ruling ADT-P5; HIS wave 8,
cluster W8-D).

With Beds and admissions on, forecast_service.refresh_ward_capacity takes a
ward's bed count from Bed setup once that ward's register is marked complete,
and keeps ORB_WARD_BEDS for every other ward; Bed setup re-runs it after each
write that can change a count. What this file defends:

  * a complete ward reads the register's census beds (not retired, not a
    temporary extra bed, of a class counted in the census); an incomplete
    ward reads ORB_WARD_BEDS; each figure says 'from bed register' or 'site
    setting';
  * adding or retiring a bed, or marking a register complete, changes the
    capacity at once, without a restart, and drops a forecast cached under
    the old capacity;
  * Command Center, the capacity forecast and housekeeping readiness agree
    on every ward's count and its source;
  * with the module off nothing changes: the map is built exactly as before,
    no answer gains a key, and a Bed setup write does not touch the map;
  * a register that cannot be read, or a refresh that fails after a write,
    never fails anything.

The capacity map is process-wide; every test here restores it.
"""
import uuid

import pytest

import config
from auth import hash_password
from database import db_session
from services import bed_master_service as bm
from services import command_center_service as cc
from services import forecast_service as fs
from services import housekeeping_service as hk

DEPT = "General Medicine"   # conftest seeds doctors and a nurse in it


def _tag() -> str:
    return uuid.uuid4().hex[:6].upper()


_ADMIN: dict | None = None


def _admin() -> dict:
    global _ADMIN
    if _ADMIN is None:
        uid = f"CAP{_tag()}"
        with db_session() as db:
            db.execute("INSERT INTO users (id, name, role, department, password_hash, is_active) "
                       "VALUES (?, 'Bed Manager', 'admin', NULL, ?, 1)", (uid, hash_password("pw")))
        _ADMIN = {"id": uid, "role": "admin", "department": None}
    return _ADMIN


@pytest.fixture(autouse=True)
def restore_capacity():
    """The map, its sources and the total are module state shared by the
    whole suite: put them back exactly as they were."""
    capacity = dict(fs.WARD_CAPACITY)
    sources = dict(fs.WARD_CAPACITY_SOURCE)
    total = fs.TOTAL_CAPACITY
    yield
    fs.WARD_CAPACITY.clear()
    fs.WARD_CAPACITY.update(capacity)
    fs.WARD_CAPACITY_SOURCE.clear()
    fs.WARD_CAPACITY_SOURCE.update(sources)
    fs.TOTAL_CAPACITY = total


class _Site:
    """The real site configuration with its declared wards replaced."""

    def __init__(self, real, wards: list[str]):
        self._real = real
        self.wards = wards

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def site(monkeypatch):
    """The site's declared wards, so discovery is this test's alone."""
    from services import site_config_service

    real = site_config_service.current_site_config()
    declared: list[str] = []
    monkeypatch.setattr(site_config_service, "current_site_config",
                        lambda: _Site(real, declared))
    return declared


@pytest.fixture()
def adt_on(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", True)
    monkeypatch.setattr(config, "HOUSEKEEPING_ENABLED", True)
    yield


@pytest.fixture()
def adt_off(monkeypatch):
    monkeypatch.setattr(config, "ADT_ENABLED", False)
    yield


def _ward(labels=("1", "2", "3"), **extra) -> dict:
    ward = bm.save_ward({"code": f"CAP-{_tag()}", "department": DEPT, **extra}, actor=_admin())
    if labels:
        bm.register_beds({"ward_id": ward["id"], "labels": list(labels)}, actor=_admin())
    return ward


def _mixed_words() -> str:
    """The summary words for the map as it stands. Other tests in the same
    session leave complete wards in the shared database, and a complete ward
    is part of the building whether or not the site names it, so the count is
    read from the map rather than assumed."""
    register = sum(1 for w in fs.WARD_CAPACITY
                   if fs.WARD_CAPACITY_SOURCE.get(w) == "bed_register")
    return (f"from bed register for {register} of {len(fs.WARD_CAPACITY)} wards, "
            "site setting for the rest")


def _bed_id(ward: dict, label: str) -> str:
    with db_session() as db:
        return db.execute("SELECT id FROM housekeeping_units WHERE ward_id = ? AND bed_label = ?",
                          (ward["id"], label)).fetchone()["id"]


# ═════════════════════════════════════════════════════════════════════════════
#  Flag off: nothing changes
# ═════════════════════════════════════════════════════════════════════════════

def test_with_the_module_off_the_map_is_built_exactly_as_before(adt_off, site):
    ward = _ward()
    bm.mark_register_complete(ward["id"], actor=_admin())
    site[:] = [ward["code"], "OTHER-1"]
    fs.refresh_ward_capacity()
    assert fs.WARD_CAPACITY == {ward["code"]: fs.DEFAULT_WARD_BEDS, "OTHER-1": fs.DEFAULT_WARD_BEDS}
    assert fs.TOTAL_CAPACITY == 2 * fs.DEFAULT_WARD_BEDS
    assert fs.capacity_source_summary() is None
    assert fs.capacity_source_for(ward["code"]) is None
    census, _acuity = cc._census_and_acuity([])
    assert "capacity_source" not in census
    assert all("capacity_source" not in row for row in census["by_ward"])
    readiness = hk.ward_readiness()
    assert "capacity_source" not in readiness
    assert all("capacity_source" not in row for row in readiness["wards"])
    assert "(a site setting)" in readiness["capacity_basis"]


def test_with_the_module_off_a_bed_setup_write_leaves_the_map_alone(adt_off, site):
    site[:] = ["UNTOUCHED-1"]
    fs.refresh_ward_capacity()
    fs.WARD_CAPACITY["SENTINEL"] = 99
    ward = _ward()
    bm.register_beds({"ward_id": ward["id"], "labels": ["9"]}, actor=_admin())
    bm.mark_register_complete(ward["id"], actor=_admin())
    assert fs.WARD_CAPACITY == {"UNTOUCHED-1": fs.DEFAULT_WARD_BEDS, "SENTINEL": 99}


# ═════════════════════════════════════════════════════════════════════════════
#  Flag on: the register for complete wards, the site setting for the rest
# ═════════════════════════════════════════════════════════════════════════════

def test_a_complete_ward_reads_the_register_and_an_incomplete_one_the_setting(adt_on, site):
    complete = _ward(labels=("1", "2", "3", "4", "5"))
    incomplete = _ward(labels=("1", "2"))
    site[:] = [complete["code"], incomplete["code"]]
    # Not census beds: a retired bed, a temporary extra bed, and a bed of a
    # class the census does not count.
    bm.retire_bed(_bed_id(complete, "5"), actor=_admin())
    with db_session() as db:
        db.execute("UPDATE housekeeping_units SET is_temporary = 1 WHERE id = ?",
                   (_bed_id(complete, "4"),))
    obs = bm.save_bed_class({"code": f"OBS{_tag()}", "name": "Observation chair",
                             "counts_in_census": False}, actor=_admin())
    bm.register_beds({"ward_id": complete["id"], "labels": ["C1"], "bed_class": obs["code"]},
                     actor=_admin())
    bm.mark_register_complete(complete["id"], actor=_admin())

    assert fs.WARD_CAPACITY[complete["code"]] == 3
    assert fs.WARD_CAPACITY[incomplete["code"]] == fs.DEFAULT_WARD_BEDS
    assert fs.TOTAL_CAPACITY == sum(fs.WARD_CAPACITY.values())
    assert fs.capacity_source_for(complete["code"]) == {"source": "bed_register",
                                                        "words": "from bed register"}
    assert fs.capacity_source_for(incomplete["code"]) == {"source": "site_setting",
                                                          "words": "site setting"}
    summary = fs.capacity_source_summary()
    assert summary["source"] == "mixed"
    assert summary["words"] == _mixed_words()
    assert summary["by_ward"][complete["code"]]["words"] == "from bed register"
    assert summary["by_ward"][incomplete["code"]]["words"] == "site setting"


def test_adding_a_bed_changes_capacity_without_a_restart(adt_on, site):
    ward = _ward(labels=("1", "2"))
    site[:] = [ward["code"]]
    bm.mark_register_complete(ward["id"], actor=_admin())
    assert fs.WARD_CAPACITY[ward["code"]] == 2
    same_map = fs.WARD_CAPACITY
    fs._hospital_flow_cache[("probe", False, "2026-01-01")] = (0.0, {"total_capacity": 2})

    before = fs.TOTAL_CAPACITY
    bm.register_beds({"ward_id": ward["id"], "labels": ["3", "4"]}, actor=_admin())
    assert fs.WARD_CAPACITY[ward["code"]] == 4
    assert fs.TOTAL_CAPACITY == before + 2
    assert fs.WARD_CAPACITY is same_map and cc.WARD_CAPACITY is same_map
    # A forecast cached under the old capacity is not served again.
    assert ("probe", False, "2026-01-01") not in fs._hospital_flow_cache

    bm.retire_bed(_bed_id(ward, "4"), actor=_admin())
    assert fs.WARD_CAPACITY[ward["code"]] == 3
    assert fs.TOTAL_CAPACITY == before + 1
    assert fs.capacity_source_for(ward["code"])["words"] == "from bed register"


def test_marking_the_register_complete_switches_the_source(adt_on, site):
    ward = _ward(labels=("1", "2", "3", "4"))
    site[:] = [ward["code"]]
    fs.refresh_ward_capacity()
    assert fs.WARD_CAPACITY[ward["code"]] == fs.DEFAULT_WARD_BEDS
    assert fs.capacity_source_for(ward["code"])["words"] == "site setting"
    bm.mark_register_complete(ward["id"], actor=_admin())
    assert fs.WARD_CAPACITY[ward["code"]] == 4
    assert fs.capacity_source_for(ward["code"])["words"] == "from bed register"


def test_a_complete_ward_the_discovery_missed_is_still_counted(adt_on, site):
    named = _ward(labels=("1",))
    missed = _ward(labels=("1", "2", "3"))
    site[:] = [named["code"]]
    bm.mark_register_complete(missed["id"], actor=_admin())
    assert fs.WARD_CAPACITY[missed["code"]] == 3
    assert fs.capacity_source_for(missed["code"])["source"] == "bed_register"
    assert fs.WARD_CAPACITY[named["code"]] == fs.DEFAULT_WARD_BEDS


def test_a_complete_ward_with_no_census_bed_keeps_the_site_setting(adt_on, site):
    ward = _ward(labels=("1",))
    site[:] = [ward["code"]]
    bm.retire_bed(_bed_id(ward, "1"), actor=_admin())
    bm.mark_register_complete(ward["id"], actor=_admin())
    # Zero beds would divide every occupancy by zero; the setting stands and
    # says so.
    assert fs.WARD_CAPACITY[ward["code"]] == fs.DEFAULT_WARD_BEDS
    assert fs.capacity_source_for(ward["code"])["source"] == "site_setting"


def test_the_register_is_matched_without_regard_to_capitals_or_spaces(adt_on, site):
    ward = _ward(labels=("1", "2"))
    typed = f"  {ward['code'].lower()} "
    site[:] = [typed]
    bm.mark_register_complete(ward["id"], actor=_admin())
    assert fs.WARD_CAPACITY[typed] == 2
    assert ward["code"] not in fs.WARD_CAPACITY      # one ward, one key
    assert fs.capacity_source_for(typed)["words"] == "from bed register"


# ═════════════════════════════════════════════════════════════════════════════
#  The screens agree, and each says where its count came from
# ═════════════════════════════════════════════════════════════════════════════

def test_command_center_forecast_and_readiness_agree(adt_on, site, monkeypatch):
    complete = _ward(labels=("1", "2", "3"))
    incomplete = _ward(labels=("1",))
    site[:] = [complete["code"], incomplete["code"]]
    bm.mark_register_complete(complete["id"], actor=_admin())
    monkeypatch.setattr(fs, "_get_cached_or_predict",
                        lambda _pid: {"predicted_los_days": 5.0})

    census, _acuity = cc._census_and_acuity([])
    cap = fs.get_bed_capacity_forecast()
    readiness = {row["ward"]: row for row in hk.ward_readiness()["wards"]}
    by_ward = {row["ward"]: row for row in census["by_ward"]}

    for ward, words in ((complete["code"], "from bed register"),
                        (incomplete["code"], "site setting")):
        assert by_ward[ward]["capacity"] == cap["current"][ward]["total"] \
            == readiness[ward]["ward_capacity"] == fs.WARD_CAPACITY[ward]
        assert by_ward[ward]["capacity_source"]["words"] == words
        assert cap["current"][ward]["capacity_source"]["words"] == words
        assert readiness[ward]["capacity_source"]["words"] == words
    assert census["total_capacity"] == cap["total"]["capacity"] == fs.TOTAL_CAPACITY
    mixed = _mixed_words()
    assert census["capacity_source"] == {"source": "mixed", "words": mixed}
    assert cap["capacity_source"] == {"source": "mixed", "words": mixed}
    assert hk.ward_readiness()["capacity_source"]["words"] == mixed
    assert "from the bed register" in hk.ward_readiness()["capacity_basis"]


def test_the_hospital_forecast_says_where_its_capacity_came_from(adt_on, site, monkeypatch):
    ward = _ward(labels=("1", "2"))
    site[:] = [ward["code"]]
    bm.mark_register_complete(ward["id"], actor=_admin())
    monkeypatch.setattr(fs, "_get_cached_or_predict",
                        lambda _pid: {"predicted_los_days": 5.0})
    fs._hospital_flow_cache.clear()
    flow = fs.predict_hospital_flow(days_ahead=2)
    assert flow["total_capacity"] == fs.TOTAL_CAPACITY == sum(fs.WARD_CAPACITY.values())
    summary = fs.capacity_source_summary()
    assert flow["capacity_source"] == {"source": summary["source"], "words": summary["words"]}
    assert summary["by_ward"][ward["code"]]["words"] == "from bed register"
    fs._hospital_flow_cache.clear()


# ═════════════════════════════════════════════════════════════════════════════
#  Failure never spreads
# ═════════════════════════════════════════════════════════════════════════════

def test_an_unreadable_register_leaves_the_site_setting(adt_on, site, monkeypatch):
    site[:] = ["FALLBACK-1"]

    def boom():
        raise RuntimeError("register unreadable")
    monkeypatch.setattr(fs, "_register_bed_counts", boom)
    out = fs.refresh_ward_capacity()
    assert out == {"FALLBACK-1": fs.DEFAULT_WARD_BEDS}
    assert fs.capacity_source_summary()["words"] == "site setting"


def test_a_failed_refresh_never_fails_a_bed_setup_write(adt_on, site, monkeypatch):
    def boom():
        raise RuntimeError("refresh failed")
    monkeypatch.setattr(fs, "refresh_ward_capacity", boom)
    ward = _ward(labels=())
    saved = bm.register_beds({"ward_id": ward["id"], "labels": ["1", "2"]}, actor=_admin())
    assert saved["count"] == 2
    done = bm.mark_register_complete(ward["id"], actor=_admin())
    assert done["register_complete"]
