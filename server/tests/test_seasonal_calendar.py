"""Pulse: season and conditions, from the calendar built into Orb.

Pulse used to pull live internet feeds (weather, air quality, FDA recalls, CDC
flu) and Forecast multiplied admissions by weather guesses. Orb is strictly
offline, and on 6 October 2026 all of that was removed: Pulse now answers from
server/site_config/seasonal_calendar.json for the hospital's Indian state.

What is held here:

  * the calendar file is validated hard, every rule, and refused WHOLE with
    every problem listed;
  * a missing or refused file never stops Orb: the route answers 503 instead;
  * dates are the HOSPITAL's calendar day, ranges are inclusive at both ends,
    wrap the new year, and 29 February reads as 28 February in other years;
  * "upcoming" means the next start is 1 to 60 days away, soonest first;
  * the response has exactly the agreed shape, with no state configured too;
  * the old feed routes are gone (404), and the route needs forecast.read.

Every calendar here is a FIXTURE. The real one is written separately; the last
test validates it when it is present.
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from services import hospital_time, seasonal_calendar, site_config_service
from services.india_states import STATES, resolve_state, state_name
from services.seasonal_calendar import (
    CALENDAR_PATH_ENV,
    SETUP_MESSAGE,
    CalendarError,
    load_calendar,
    season_report,
)
from services.site_config_service import SiteConfig

IST = ZoneInfo("Asia/Kolkata")

WEST = ["MH", "GA", "GJ"]
SOUTH = ["KL", "TN"]
REST = [c for c in STATES if c not in WEST + SOUTH]
SOURCE = {"name": "Fixture source", "url": "https://example.org/source",
          "accessed": "2026-10-01"}


def _entry(eid, kind, title, zones, start, end, peak=None, rises=("Fever",)):
    entry = {
        "id": eid, "kind": kind, "title": title, "zones": list(zones),
        "start": start, "end": end, "summary": f"{title}, fixture summary.",
        "typically_rises": list(rises), "sources": [dict(SOURCE)],
    }
    if peak:
        entry["peak"] = {"start": peak[0], "end": peak[1]}
    return entry


def _calendar() -> dict:
    """A small, valid calendar. Tests that need a broken one mutate a copy."""
    return {
        "version": 1,
        "status": "draft",
        "reviewed_by": None,
        "reviewed_on": None,
        "compiled_on": "2026-10-06",
        "zones": [
            {"id": "west_coast", "name": "West coast", "states": list(WEST)},
            {"id": "south", "name": "South", "states": list(SOUTH)},
            {"id": "rest_of_india", "name": "Rest of India", "states": list(REST)},
        ],
        "entries": [
            _entry("southwest-monsoon", "season", "Southwest monsoon", ["west_coast"],
                   "06-01", "09-30", peak=("07-01", "08-15"),
                   rises=("Leptospirosis", "Dengue")),
            # Wraps the new year, ends on 29 February, peak wraps too.
            _entry("cool-dry-season", "season", "Cool dry season", ["west_coast"],
                   "11-01", "02-29", peak=("12-15", "01-15")),
            _entry("post-monsoon-dengue", "disease", "Dengue season", ["west_coast"],
                   "08-01", "11-15", peak=("09-15", "10-31"), rises=("Dengue",)),
            _entry("seasonal-flu", "disease", "Seasonal influenza", ["west_coast"],
                   "10-15", "12-15", rises=("Influenza-like illness",)),
            _entry("winter-smog", "environment", "Winter smog",
                   ["west_coast", "rest_of_india"], "11-01", "01-31",
                   rises=("Asthma attacks", "COPD exacerbations")),
            _entry("hot-season", "environment", "Hot season", ["west_coast"],
                   "03-15", "05-31", peak=("04-15", "05-20"), rises=("Heat illness",)),
            _entry("northeast-monsoon", "season", "Northeast monsoon", ["south"],
                   "10-01", "12-31"),
            # Starts on 29 February: 28 February in a common year.
            _entry("leap-start", "disease", "Leap start", ["rest_of_india"],
                   "02-29", "03-10"),
        ],
    }


def _write(tmp_path, doc, name="seasonal_calendar.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No test sees another's calendar, state or clock."""
    seasonal_calendar.reload_calendar()
    yield
    seasonal_calendar.reload_calendar()


@pytest.fixture()
def use_calendar(tmp_path, monkeypatch):
    def _use(doc=None) -> Path:
        path = _write(tmp_path, _calendar() if doc is None else doc)
        monkeypatch.setenv(CALENDAR_PATH_ENV, str(path))
        seasonal_calendar.reload_calendar()
        return path
    return _use


@pytest.fixture()
def set_state(monkeypatch):
    def _set(code):
        monkeypatch.setattr(site_config_service, "_loaded",
                            SiteConfig(present=code is not None, site_state=code))
    return _set


def _report(day: date) -> dict:
    return season_report(day)


def _ids(cards) -> list[str]:
    return [c["id"] for c in cards]


# ═════════════════════════════════════════════════════════════════════════════
#  The state list
# ═════════════════════════════════════════════════════════════════════════════
def test_there_are_exactly_the_36_agreed_codes():
    assert sorted(STATES) == sorted(
        "AN AP AR AS BR CH CT DH DL GA GJ HR HP JK JH KA KL LA LD MP MH MN ML MZ NL "
        "OR PY PB RJ SK TN TG TR UP UT WB".split()
    )
    assert state_name("DH") == "Dadra and Nagar Haveli and Daman and Diu"


@pytest.mark.parametrize("value,code", [
    ("MH", "MH"), ("mh", "MH"), ("Maharashtra", "MH"), ("MAHARASHTRA", "MH"),
    (" west bengal ", "WB"), ("Jammu & Kashmir", "JK"), ("Orissa", "OR"),
    ("Pondicherry", "PY"), ("Daman and Diu", "DH"), ("ut", "UT"),
])
def test_resolver_accepts_a_code_or_a_name_in_any_case(value, code):
    assert resolve_state(value) == code


@pytest.mark.parametrize("value", [None, "", "  ", "Bombay", "India", "OD", "TS", 27])
def test_resolver_never_guesses(value):
    assert resolve_state(value) is None


# ═════════════════════════════════════════════════════════════════════════════
#  Validation: each rule refuses the file, and says why
# ═════════════════════════════════════════════════════════════════════════════
def _entry_by_id(doc, eid):
    return next(e for e in doc["entries"] if e["id"] == eid)


def _drop_state(doc):
    doc["zones"][2]["states"].remove("WB")


def _state_twice(doc):
    doc["zones"][1]["states"].append("MH")


def _unknown_code(doc):
    doc["zones"][2]["states"].append("XX")


def _lowercase_code(doc):
    doc["zones"][0]["states"][0] = "mh"


def _duplicate_entry_id(doc):
    doc["entries"][1]["id"] = "southwest-monsoon"


def _duplicate_zone_id(doc):
    doc["zones"][1]["id"] = "west_coast"


def _bad_entry_id(doc):
    doc["entries"][0]["id"] = "Southwest_Monsoon"


def _bad_zone_id(doc):
    doc["zones"][0]["id"] = "West-Coast"
    for e in doc["entries"]:
        e["zones"] = ["West-Coast" if z == "west_coast" else z for z in e["zones"]]


def _impossible_date(doc):
    doc["entries"][0]["start"] = "02-30"


def _bad_month(doc):
    doc["entries"][0]["end"] = "13-01"


def _unpadded_date(doc):
    doc["entries"][0]["start"] = "6-1"


def _peak_outside(doc):
    _entry_by_id(doc, "southwest-monsoon")["peak"] = {"start": "09-15", "end": "10-15"}


def _peak_outside_a_wrapped_range(doc):
    _entry_by_id(doc, "cool-dry-season")["peak"] = {"start": "10-15", "end": "11-15"}


def _peak_wraps_but_range_does_not(doc):
    _entry_by_id(doc, "southwest-monsoon")["peak"] = {"start": "09-01", "end": "06-15"}


def _peak_bad_date(doc):
    _entry_by_id(doc, "southwest-monsoon")["peak"] = {"start": "07-01", "end": "07-32"}


def _no_sources(doc):
    doc["entries"][0]["sources"] = []


def _source_without_url(doc):
    del doc["entries"][0]["sources"][0]["url"]


def _source_bad_accessed(doc):
    doc["entries"][0]["sources"][0]["accessed"] = "October 2026"


def _undefined_zone(doc):
    doc["entries"][0]["zones"] = ["north_east"]


def _no_zones_on_entry(doc):
    doc["entries"][0]["zones"] = []


def _bad_kind(doc):
    doc["entries"][0]["kind"] = "weather"


def _long_title(doc):
    doc["entries"][0]["title"] = "x" * 41


def _empty_summary(doc):
    doc["entries"][0]["summary"] = "  "


def _rises_not_a_list(doc):
    doc["entries"][0]["typically_rises"] = "Dengue"


def _unknown_entry_key(doc):
    doc["entries"][0]["notes"] = "typo for summary?"


def _unknown_root_key(doc):
    doc["reviewer"] = "Dr A"


def _version_two(doc):
    doc["version"] = 2


def _version_true(doc):
    doc["version"] = True


def _bad_status(doc):
    doc["status"] = "final"


def _reviewed_without_reviewer(doc):
    doc["status"] = "reviewed"


def _draft_with_reviewer(doc):
    doc["reviewed_by"] = "Dr A"
    doc["reviewed_on"] = "2026-10-05"


def _bad_compiled_on(doc):
    doc["compiled_on"] = "2026-13-01"


def _no_entries(doc):
    doc["entries"] = []


@pytest.mark.parametrize("mutate,expected", [
    (_drop_state, "WB (West Bengal)"),
    (_state_twice, "already in zone 'west_coast'"),
    (_unknown_code, "'XX' is not one of the 36 state codes"),
    (_lowercase_code, "Did you mean 'MH'?"),
    (_duplicate_entry_id, "id is used by an earlier entry"),
    (_duplicate_zone_id, "id is used by an earlier zone"),
    (_bad_entry_id, "kebab-case"),
    (_bad_zone_id, "snake_case"),
    (_impossible_date, "start must be a real date written MM-DD; got '02-30'"),
    (_bad_month, "end must be a real date written MM-DD; got '13-01'"),
    (_unpadded_date, "got '6-1'"),
    (_peak_outside, "peak 09-15 to 10-15 is not inside the range 06-01 to 09-30"),
    (_peak_outside_a_wrapped_range, "peak 10-15 to 11-15 is not inside the range 11-01 to 02-29"),
    (_peak_wraps_but_range_does_not, "peak 09-01 to 06-15 is not inside"),
    (_peak_bad_date, "peak end must be a real date"),
    (_no_sources, "sources must list at least one source"),
    (_source_without_url, "url must be an http(s) address"),
    (_source_bad_accessed, "accessed must be a date written YYYY-MM-DD"),
    (_undefined_zone, "zone 'north_east' is not defined in zones"),
    (_no_zones_on_entry, "zones must list at least one zone id"),
    (_bad_kind, "kind must be one of ['season', 'environment', 'disease']"),
    (_long_title, "title is 41 characters"),
    (_empty_summary, "summary must be some text"),
    (_rises_not_a_list, "typically_rises must be a list"),
    (_unknown_entry_key, "unknown key(s) ['notes']"),
    (_unknown_root_key, "unknown key(s) ['reviewer']"),
    (_version_two, "must be 1 (the version this build reads); got 2"),
    (_version_true, "must be 1 (the version this build reads); got True"),
    (_bad_status, "must be one of ['draft', 'reviewed']; got 'final'"),
    (_reviewed_without_reviewer, "reviewed_by and reviewed_on must both say who"),
    (_draft_with_reviewer, "is 'draft', so reviewed_by and reviewed_on must both be null"),
    (_bad_compiled_on, "must be a date written YYYY-MM-DD; got '2026-13-01'"),
    (_no_entries, "must be a non-empty list"),
])
def test_each_rule_refuses_the_file(tmp_path, mutate, expected):
    doc = _calendar()
    mutate(doc)
    path = _write(tmp_path, doc)
    with pytest.raises(CalendarError) as excinfo:
        load_calendar(path)
    message = str(excinfo.value)
    assert expected in message, message
    assert str(path) in message
    assert excinfo.value.missing is False


def test_the_fixture_itself_is_valid(tmp_path):
    """Otherwise every refusal above could be refusing the fixture, not the rule."""
    cal = load_calendar(_write(tmp_path, _calendar()))
    assert len(cal.entries) == 8
    assert cal.zone_for_state("MH")["id"] == "west_coast"


def test_comment_keys_are_allowed_and_peak_may_be_null(tmp_path):
    doc = _calendar()
    doc["_comment"] = "Reviewed against IMD and NCVBDC pages."
    doc["zones"][0]["_note"] = "Konkan and Goa"
    doc["entries"][0]["_note"] = "peak from IMD normals"
    doc["entries"][0]["sources"][0]["_page"] = "p. 4"
    doc["entries"][1]["peak"] = None
    cal = load_calendar(_write(tmp_path, doc))
    assert next(e for e in cal.entries if e.id == "cool-dry-season").peak is None


def test_a_reviewed_calendar_with_reviewer_and_date_loads(tmp_path):
    doc = _calendar()
    doc.update(status="reviewed", reviewed_by="Dr A. Rao, MD", reviewed_on="2026-10-05")
    assert load_calendar(_write(tmp_path, doc)).header() == {
        "version": 1, "status": "reviewed", "reviewed_by": "Dr A. Rao, MD",
        "reviewed_on": "2026-10-05", "compiled_on": "2026-10-06",
    }


def test_every_problem_is_listed_at_once(tmp_path):
    """One refusal, every problem: fixing them one restart at a time is how an
    operator gives up."""
    doc = _calendar()
    _bad_kind(doc)
    _long_title(doc)
    _drop_state(doc)
    with pytest.raises(CalendarError) as excinfo:
        load_calendar(_write(tmp_path, doc))
    assert len(excinfo.value.problems) == 3, excinfo.value.problems


def test_an_entry_problem_names_the_entry_line_not_a_zone_with_the_same_id(tmp_path):
    doc = _calendar()
    doc["zones"].append({"id": "goa", "name": "Goa", "states": ["GA"]})
    doc["zones"][0]["states"].remove("GA")
    doc["entries"][0]["id"] = "goa"
    doc["entries"][0]["kind"] = "weather"
    path = _write(tmp_path, doc)
    lines = path.read_text().splitlines()
    zone_line = next(n for n, t in enumerate(lines, 1) if '"id": "goa"' in t)
    entry_line = next(n for n, t in enumerate(lines, 1)
                      if '"id": "goa"' in t and n > zone_line)
    with pytest.raises(CalendarError) as excinfo:
        load_calendar(path)
    assert f"(line {entry_line})" in str(excinfo.value), str(excinfo.value)


def test_a_duplicate_id_points_at_the_second_entry(tmp_path):
    doc = _calendar()
    doc["entries"][3]["id"] = "southwest-monsoon"
    path = _write(tmp_path, doc)
    lines = [n for n, t in enumerate(path.read_text().splitlines(), 1)
             if '"id": "southwest-monsoon"' in t]
    assert len(lines) == 2
    with pytest.raises(CalendarError) as excinfo:
        load_calendar(path)
    assert f"entries[3] 'southwest-monsoon' (line {lines[1]}): id is used by an earlier " \
           f"entry" in str(excinfo.value), str(excinfo.value)


def test_invalid_json_reports_line_and_column(tmp_path):
    path = tmp_path / "seasonal_calendar.json"
    path.write_text('{\n  "version": 1,\n  "status": "draft"\n  "zones": []\n}\n')
    with pytest.raises(CalendarError) as excinfo:
        load_calendar(path)
    assert "line 4, column 3: not valid JSON" in str(excinfo.value)


def test_a_missing_file_is_reported_as_missing(tmp_path):
    with pytest.raises(CalendarError) as excinfo:
        load_calendar(tmp_path / "nope.json")
    assert excinfo.value.missing is True
    assert "not installed" in excinfo.value.public_message


# ═════════════════════════════════════════════════════════════════════════════
#  Which entries are active, and when
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("day,active", [
    (date(2026, 5, 31), False),
    (date(2026, 6, 1), True),     # first day, inclusive
    (date(2026, 9, 30), True),    # last day, inclusive
    (date(2026, 10, 1), False),
])
def test_a_range_is_inclusive_at_both_ends(use_calendar, set_state, day, active):
    use_calendar()
    set_state("MH")
    report = _report(day)
    assert ("southwest-monsoon" in _ids(report["active"])) is active
    if active:
        card = next(c for c in report["active"] if c["id"] == "southwest-monsoon")
        assert card["ends_on"] == "2026-09-30"


@pytest.mark.parametrize("day,active,ends_on", [
    (date(2026, 10, 31), False, None),
    (date(2026, 11, 1), True, "2027-02-28"),   # wraps: ends NEXT year
    (date(2026, 12, 31), True, "2027-02-28"),
    (date(2027, 1, 15), True, "2027-02-28"),   # after the new year: ends THIS year
    (date(2027, 2, 28), True, "2027-02-28"),   # 02-29 reads as 02-28 in 2027
    (date(2027, 3, 1), False, None),
    (date(2027, 12, 1), True, "2028-02-29"),   # 2028 has a 29 February
    (date(2028, 2, 29), True, "2028-02-29"),
    (date(2028, 3, 1), False, None),
])
def test_a_range_that_wraps_the_year(use_calendar, set_state, day, active, ends_on):
    use_calendar()
    set_state("MH")
    report = _report(day)
    cards = {c["id"]: c for c in report["active"]}
    assert ("cool-dry-season" in cards) is active
    if active:
        assert cards["cool-dry-season"]["ends_on"] == ends_on


@pytest.mark.parametrize("day,active,days_away", [
    (date(2027, 2, 27), False, 1),   # starts 28 Feb in a common year
    (date(2027, 2, 28), True, None),
    (date(2028, 2, 28), False, 1),   # and on the real 29 Feb in a leap year
    (date(2028, 2, 29), True, None),
])
def test_29_february_start_in_common_and_leap_years(use_calendar, set_state, day, active,
                                                    days_away):
    use_calendar()
    set_state("DL")
    report = _report(day)
    assert ("leap-start" in _ids(report["active"])) is active
    if not active:
        card = next(c for c in report["upcoming"] if c["id"] == "leap-start")
        assert card["days_away"] == days_away
        assert card["starts_on"] == ("2027-02-28" if day.year == 2027 else "2028-02-29")


@pytest.mark.parametrize("day,in_peak", [
    (date(2026, 6, 30), False),
    (date(2026, 7, 1), True),
    (date(2026, 8, 15), True),
    (date(2026, 8, 16), False),
])
def test_in_peak_inside_the_peak_only(use_calendar, set_state, day, in_peak):
    use_calendar()
    set_state("MH")
    card = next(c for c in _report(day)["active"] if c["id"] == "southwest-monsoon")
    assert card["in_peak"] is in_peak


@pytest.mark.parametrize("day,in_peak", [
    (date(2026, 12, 14), False),
    (date(2026, 12, 15), True),
    (date(2026, 12, 31), True),
    (date(2027, 1, 15), True),
    (date(2027, 1, 16), False),
])
def test_a_peak_that_wraps_the_year(use_calendar, set_state, day, in_peak):
    use_calendar()
    set_state("MH")
    card = next(c for c in _report(day)["active"] if c["id"] == "cool-dry-season")
    assert card["in_peak"] is in_peak


def test_an_entry_without_a_peak_is_never_in_peak(use_calendar, set_state):
    use_calendar()
    set_state("MH")
    card = next(c for c in _report(date(2026, 11, 20))["active"] if c["id"] == "winter-smog")
    assert card["in_peak"] is False


def test_active_entries_are_ordered_season_environment_disease_then_title(
        use_calendar, set_state):
    use_calendar()
    set_state("MH")
    report = _report(date(2026, 11, 10))
    assert [(c["kind"], c["title"]) for c in report["active"]] == [
        ("season", "Cool dry season"),
        ("environment", "Winter smog"),
        ("disease", "Dengue season"),
        ("disease", "Seasonal influenza"),
    ]


def test_only_the_hospitals_zone_is_shown(use_calendar, set_state):
    use_calendar()
    set_state("KL")
    report = _report(date(2026, 11, 10))
    assert report["zone"] == {"id": "south", "name": "South"}
    assert _ids(report["active"]) == ["northeast-monsoon"]


# ═════════════════════════════════════════════════════════════════════════════
#  Upcoming: next start 1 to 60 days away, soonest first
# ═════════════════════════════════════════════════════════════════════════════
def test_upcoming_includes_exactly_60_days_and_not_61(use_calendar, set_state):
    use_calendar()
    set_state("MH")
    at_60 = _report(date(2026, 9, 2))      # 2 Sep -> 1 Nov is 60 days
    at_61 = _report(date(2026, 9, 1))
    assert {"cool-dry-season", "winter-smog"} <= set(_ids(at_60["upcoming"]))
    assert all(c["days_away"] == 60 for c in at_60["upcoming"]
               if c["id"] in ("cool-dry-season", "winter-smog"))
    assert not {"cool-dry-season", "winter-smog"} & set(_ids(at_61["upcoming"]))


def test_upcoming_is_soonest_first_then_kind_then_title(use_calendar, set_state):
    use_calendar()
    set_state("MH")
    report = _report(date(2026, 9, 20))
    assert [(c["id"], c["days_away"]) for c in report["upcoming"]] == [
        ("seasonal-flu", 25),
        ("cool-dry-season", 42),   # same day: season before environment
        ("winter-smog", 42),
    ]
    for card in report["upcoming"]:
        assert card["in_peak"] is False
        assert "ends_on" not in card


def test_upcoming_across_the_new_year(use_calendar, set_state):
    use_calendar()
    set_state("DL")
    report = _report(date(2027, 1, 20))
    card = next(c for c in report["upcoming"] if c["id"] == "leap-start")
    assert card["starts_on"] == "2027-02-28"
    assert card["days_away"] == 39


def test_an_active_entry_is_never_also_upcoming(use_calendar, set_state):
    use_calendar()
    set_state("MH")
    report = _report(date(2026, 11, 10))
    assert not set(_ids(report["active"])) & set(_ids(report["upcoming"]))


# ═════════════════════════════════════════════════════════════════════════════
#  The hospital's day, not the UTC day
# ═════════════════════════════════════════════════════════════════════════════
def _pin(monkeypatch, utc: datetime):
    monkeypatch.setattr(hospital_time, "_utc_now", lambda: utc)
    monkeypatch.setattr(hospital_time, "hospital_timezone", lambda: IST)


def test_just_after_midnight_ist_is_already_the_next_day(use_calendar, set_state,
                                                         monkeypatch):
    """00:10 IST on 1 November is still 31 October in UTC. The November entries
    are active on the ward, so they are active here."""
    use_calendar()
    set_state("MH")
    _pin(monkeypatch, datetime(2026, 10, 31, 18, 40, tzinfo=timezone.utc))
    report = season_report()
    assert report["date"] == "2026-11-01"
    assert {"cool-dry-season", "winter-smog"} <= set(_ids(report["active"]))


def test_just_before_midnight_ist_is_still_the_day_before(use_calendar, set_state,
                                                          monkeypatch):
    use_calendar()
    set_state("MH")
    _pin(monkeypatch, datetime(2026, 10, 31, 18, 20, tzinfo=timezone.utc))  # 23:50 IST
    report = season_report()
    assert report["date"] == "2026-10-31"
    assert "cool-dry-season" not in _ids(report["active"])
    card = next(c for c in report["upcoming"] if c["id"] == "cool-dry-season")
    assert (card["starts_on"], card["days_away"]) == ("2026-11-01", 1)


# ═════════════════════════════════════════════════════════════════════════════
#  No state configured
# ═════════════════════════════════════════════════════════════════════════════
def test_no_state_means_setup_needed_and_nothing_else(use_calendar, set_state):
    use_calendar()
    set_state(None)
    report = _report(date(2026, 11, 10))
    assert report == {
        "date": "2026-11-10",
        "state": None,
        "zone": None,
        "setup_needed": True,
        "setup_message": "This hospital's state has not been set up, so Pulse cannot "
                         "show its seasonal calendar. Ask the person who installed Orb to set it.",
        "calendar": {"version": 1, "status": "draft", "reviewed_by": None,
                     "reviewed_on": None, "compiled_on": "2026-10-06"},
        "active": [],
        "upcoming": [],
    }
    assert report["setup_message"] == SETUP_MESSAGE


def test_state_comes_from_the_site_config_file(use_calendar, tmp_path, monkeypatch):
    """End to end through site_config_service: the file's name becomes the code."""
    use_calendar()
    site = tmp_path / "site.toml"
    site.write_text('[site]\nname = "Test Hospital"\nstate = "goa"\n', encoding="utf-8")
    monkeypatch.setattr(site_config_service, "_loaded", SiteConfig())
    site_config_service.load_site_config(site, apply_env=False)
    report = _report(date(2026, 7, 10))
    assert report["state"] == {"code": "GA", "name": "Goa"}
    assert report["zone"] == {"id": "west_coast", "name": "West coast"}
    assert report["setup_needed"] is False and report["setup_message"] is None


# ═════════════════════════════════════════════════════════════════════════════
#  The route
# ═════════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def client():
    from main import app

    return TestClient(app)


def _auth(role: str) -> dict:
    uid = f"SEASON-{role.upper()[:6]}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,'x')",
            (uid, f"{role} season test", role),
        )
    return {"Authorization": f"Bearer {create_token(uid, role)}"}


EXPECTED_6_OCT_2026 = {
    "date": "2026-10-06",
    "state": {"code": "MH", "name": "Maharashtra"},
    "zone": {"id": "west_coast", "name": "West coast"},
    "setup_needed": False,
    "setup_message": None,
    "calendar": {"version": 1, "status": "draft", "reviewed_by": None,
                 "reviewed_on": None, "compiled_on": "2026-10-06"},
    "active": [
        {
            "id": "post-monsoon-dengue", "kind": "disease", "title": "Dengue season",
            "start": "08-01", "end": "11-15", "in_peak": True,
            "summary": "Dengue season, fixture summary.",
            "typically_rises": ["Dengue"],
            "sources": [SOURCE],
            "ends_on": "2026-11-15",
        },
    ],
    "upcoming": [
        {
            "id": "seasonal-flu", "kind": "disease", "title": "Seasonal influenza",
            "start": "10-15", "end": "12-15", "in_peak": False,
            "summary": "Seasonal influenza, fixture summary.",
            "typically_rises": ["Influenza-like illness"],
            "sources": [SOURCE],
            "starts_on": "2026-10-15", "days_away": 9,
        },
        {
            "id": "cool-dry-season", "kind": "season", "title": "Cool dry season",
            "start": "11-01", "end": "02-29", "in_peak": False,
            "summary": "Cool dry season, fixture summary.",
            "typically_rises": ["Fever"],
            "sources": [SOURCE],
            "starts_on": "2026-11-01", "days_away": 26,
        },
        {
            "id": "winter-smog", "kind": "environment", "title": "Winter smog",
            "start": "11-01", "end": "01-31", "in_peak": False,
            "summary": "Winter smog, fixture summary.",
            "typically_rises": ["Asthma attacks", "COPD exacerbations"],
            "sources": [SOURCE],
            "starts_on": "2026-11-01", "days_away": 26,
        },
    ],
}


def test_the_response_has_exactly_the_agreed_shape(client, use_calendar, set_state,
                                                   monkeypatch):
    use_calendar()
    set_state("MH")
    _pin(monkeypatch, datetime(2026, 10, 6, 6, 30, tzinfo=timezone.utc))  # 12:00 IST
    resp = client.get("/api/pulse/season", headers=_auth("doctor"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == EXPECTED_6_OCT_2026
    # Key ORDER too, so a client that renders keys as they come is stable.
    assert list(body) == ["date", "state", "zone", "setup_needed", "setup_message",
                          "calendar", "active", "upcoming"]
    assert list(body["calendar"]) == ["version", "status", "reviewed_by",
                                      "reviewed_on", "compiled_on"]
    common = ["id", "kind", "title", "start", "end", "in_peak", "summary",
              "typically_rises", "sources"]
    assert list(body["active"][0]) == common + ["ends_on"]
    assert all(list(c) == common + ["starts_on", "days_away"] for c in body["upcoming"])


def test_the_route_with_no_state_still_carries_the_calendar_block(client, use_calendar,
                                                                  set_state):
    use_calendar()
    set_state(None)
    body = client.get("/api/pulse/season", headers=_auth("nurse")).json()
    assert body["setup_needed"] is True
    assert body["setup_message"] == SETUP_MESSAGE
    assert body["state"] is None and body["zone"] is None
    assert body["active"] == [] and body["upcoming"] == []
    assert body["calendar"]["status"] == "draft"


def test_the_route_needs_forecast_read(client, use_calendar, set_state):
    use_calendar()
    set_state("MH")
    assert client.get("/api/pulse/season").status_code == 401
    assert client.get("/api/pulse/season", headers=_auth("pharmacist")).status_code == 403
    assert client.get("/api/pulse/season", headers=_auth("doctor")).status_code == 200


def test_a_missing_calendar_is_a_503_not_a_crash(client, tmp_path, monkeypatch, set_state):
    set_state("MH")
    monkeypatch.setenv(CALENDAR_PATH_ENV, str(tmp_path / "absent.json"))
    resp = client.get("/api/pulse/season", headers=_auth("doctor"))
    assert resp.status_code == 503
    assert resp.json()["detail"].startswith("Pulse's seasonal calendar is not installed")
    assert str(tmp_path) not in resp.text  # no server paths on a ward screen


def test_a_broken_calendar_is_a_503_and_is_logged_once(client, use_calendar, set_state,
                                                      caplog):
    doc = _calendar()
    _bad_kind(doc)
    use_calendar(doc)
    set_state("MH")
    with caplog.at_level("ERROR", logger="orb.pulse.season"):
        first = client.get("/api/pulse/season", headers=_auth("doctor"))
        second = client.get("/api/pulse/season", headers=_auth("doctor"))
    assert first.status_code == second.status_code == 503
    assert first.json()["detail"].startswith("Pulse's seasonal calendar has errors")
    errors = [r for r in caplog.records if r.name == "orb.pulse.season"]
    assert len(errors) == 1, "a refused calendar is logged once, not per request"
    assert "kind must be one of" in errors[0].getMessage()


@pytest.mark.parametrize("path", [
    "/api/pulse/dashboard", "/api/pulse/weather", "/api/pulse/aqi",
    "/api/pulse/flu", "/api/pulse/drug-recalls", "/api/pulse/audit-log",
])
def test_the_old_internet_feed_routes_are_gone(client, path):
    assert client.get(path, headers=_auth("doctor")).status_code == 404


def test_the_internet_feed_code_is_gone():
    import importlib.util

    assert importlib.util.find_spec("services.pulse_service") is None


# ═════════════════════════════════════════════════════════════════════════════
#  The real calendar, when it is present
# ═════════════════════════════════════════════════════════════════════════════
SHIPPED = Path(__file__).resolve().parent.parent / "site_config" / "seasonal_calendar.json"


@pytest.mark.skipif(
    not SHIPPED.exists(),
    reason="server/site_config/seasonal_calendar.json is not in this checkout yet "
           "(its content is written separately); the fixture tests above cover the rules",
)
def test_the_shipped_calendar_is_valid_and_answers_for_every_state(set_state, monkeypatch):
    cal = load_calendar(SHIPPED)
    assert cal.version == 1
    for code in STATES:
        assert cal.zone_for_state(code) is not None, code
    for zone in cal.zones:
        assert any(zone["id"] in e.zones for e in cal.entries), (
            f"zone {zone['id']!r} has no entries, so every hospital in "
            f"{', '.join(zone['states'])} would see an empty Pulse all year"
        )
    # Every state, on the first of every month of a common and a leap year.
    monkeypatch.setenv(CALENDAR_PATH_ENV, str(SHIPPED))
    seasonal_calendar.reload_calendar()
    for code in STATES:
        set_state(code)
        for year in (2027, 2028):
            for month in range(1, 13):
                report = _report(date(year, month, 1))
                assert report["state"]["code"] == code
                assert report["zone"] is not None
