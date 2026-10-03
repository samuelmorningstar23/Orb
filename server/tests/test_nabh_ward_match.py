"""NABH ward matching ignores case and spacing, and the panel gets a ward list.

The register's ward filter was free text matched with `ward = ?`, so typing
'gm-1' for a ward stored as 'GM-1' emptied the register (and every ward-filtered
indicator) without a word. The report form stored whatever was typed, so one
ward could become two rows in a by-ward count.
"""
import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import db_session  # noqa: E402
from services import nabh_service as svc  # noqa: E402

P_START = "2025-03-01"
P_END = "2025-04-09"
_SEQ = itertools.count(1)


def _ward() -> str:
    return f"NABH-WM-{next(_SEQ):03d}"


def _seed_incident(ward: str) -> int:
    with db_session() as db:
        cur = db.execute(
            "INSERT INTO nabh_incidents (reference, incident_type, severity, title, "
            "description, occurred_at, reported_at, ward, is_anonymous, status, "
            "created_at, updated_at) "
            "VALUES (?, 'medication_error', 'no_harm', 'fixture', 'fixture', ?, ?, ?, 0, "
            "'reported', ?, ?)",
            (f"INCWM-{next(_SEQ):05d}", "2025-03-05T09:00:00+00:00",
             "2025-03-05T09:00:00+00:00", ward,
             "2025-03-05T09:00:00+00:00", "2025-03-05T09:00:00+00:00"),
        )
        return int(cur.lastrowid)


def test_a_lower_case_filter_finds_the_upper_case_ward():
    ward = _ward()
    incident_id = _seed_incident(ward)
    for typed in (ward.lower(), f"  {ward} ", ward):
        listing = svc.list_incidents(period_start=P_START, period_end=P_END, ward=typed)
        ids = [row["id"] for row in listing["incidents"]]
        assert incident_id in ids, typed


def test_a_lower_case_filter_counts_the_ward_in_the_indicators():
    ward = _ward()
    _seed_incident(ward)
    upper = svc.indicators(period_start=P_START, period_end=P_END, ward=ward)
    lower = svc.indicators(period_start=P_START, period_end=P_END, ward=ward.lower())
    assert upper["register_usage"]["total"] == 1
    assert lower["register_usage"]["total"] == upper["register_usage"]["total"]


def test_a_report_stores_the_hospitals_spelling_of_the_ward():
    ward = _ward()
    _seed_incident(ward)  # the ward is now known to the hospital list
    stored = svc.report_incident(
        incident_type="medication_error", severity="near_miss",
        title="Look-alike vials", description="Caught at the trolley.",
        ward=f" {ward.lower()} ",
    )
    assert stored["ward"] == ward


def test_an_unknown_ward_is_kept_as_typed_not_refused():
    stored = svc.report_incident(
        incident_type="patient_fall", severity="no_harm",
        title="Slipped near the sink", description="No injury.",
        ward="  Annexe B  ",
    )
    assert stored["ward"] == "Annexe B"


def test_meta_lists_each_ward_once():
    ward = _ward()
    _seed_incident(ward)
    _seed_incident(ward.lower())
    wards = svc.meta()["wards"]
    assert ward in wards
    assert [w.upper() for w in wards].count(ward.upper()) == 1
