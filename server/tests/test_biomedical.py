"""Biomedical asset & equipment tracking.

WHAT THIS FILE IS DEFENDING, in order of how much it would cost to get wrong:

  1. THE OVERDUE BOUNDARY. `today >= next_due_date` — a device is out of
     calibration ON its due date, not the day after. An off-by-one here is
     invisible: the screen looks right, every count is plausible, and the only
     symptom is that every lapse is reported one day late. Asserted at day−1,
     the day itself and day+1, at BOTH the pure-function level and through the
     register that a ward actually reads, because a correct predicate wired into
     a `<` filter in SQL is the same defect wearing a disguise.

  2. NEXT-DUE ARITHMETIC. `last_done + interval_days`, plain calendar addition,
     including across a month end and across a year end where an off-by-one in
     the day count is easiest to write and hardest to see.

  3. UNKNOWN IS NOT COMPLIANT. A schedule with no `last_done_date` is NEVER
     DONE and appears in the overdue register; a schedule cannot be created with
     no basis for its due date at all; and an asset with no schedule is reported
     as a coverage gap rather than silently counted as compliant. These three
     are the ways a register can lie while looking perfect.

  4. DOWNTIME ACCUMULATES. Three two-hour outages are six hours down, not two.
     A hospital argues with a vendor using this number.

  5. THE AMC NOTICE WINDOW, and the fact that it uses the OPPOSITE boundary rule
     from a maintenance schedule (`end_date` is the last covered day, so expiry
     is `today > end_date`). Both boundaries are pinned so the two rules cannot
     drift into each other.

  6. A FAILED CALIBRATION DOES NOT CERTIFY ANYTHING. `result='fail'` must leave
     the asset in the overdue register; rolling it forward would convert a
     failure into a year of false compliance.

PROVEN ABLE TO FAIL (2026-08-12). Each defect was reintroduced, the suite was
watched go RED on the named tests, and the code was restored (66 passed after):

  1. `is_overdue` using `today > next_due` instead of `today >= next_due`
     → 7 failures, led by
       test_overdue_boundary_the_predicate[2026-04-01-True] and
       test_overdue_register_catches_the_day_it_becomes_overdue[2026-04-01-True].
       The day−1 and day+1 cases stayed GREEN (`.F.`), which is exactly what
       makes a three-point boundary worth writing rather than a single case.
       Recorded verbatim:
         E  AssertionError: assert False is True
         E   +  where False = is_overdue(datetime.date(2026, 4, 1),
         E                               datetime.date(2026, 4, 1))

  1b. THE DISGUISED VERSION, and the reason there are two boundary tests instead
     of one: `is_overdue` left CORRECT, but `_schedule_view` computing
     `day > next_due + grace` itself instead of calling it. All three cases of
     test_overdue_boundary_the_predicate stayed GREEN; the register and ward
     tests failed on the day-of case. A correct predicate that nothing calls is
     the same off-by-one wearing a disguise.

  2. `contract_in_notice_window` using `<` instead of `<=`
     → test_amc_warning_fires_on_the_first_day_of_the_notice_window[2027-01-30-True]
       ONLY — the 61-days-out, last-day and expired cases stayed green.
  3. `asset_downtime_minutes` taking MAX rather than SUM
     → test_downtime_accumulates_across_multiple_service_calls
  4. `record_schedule_completion` rolling a FAILED calibration forward
     → test_a_failed_calibration_does_not_certify_anything
  5. `due_register` filtering `never_done` schedules out
     → test_a_device_never_calibrated_is_overdue_not_invisible

TWO DEFECTS THIS FILE FOUND IN ITS OWN FIRST DRAFT, both in the TEST rather than
the code, recorded because they are the same class of error the module is about:
a 60-day notice window starting on the day with 60 days left (not 61), and an
outage from 08:00 to 01:00 the next day being 17 hours rather than 5. Date
arithmetic is easy to assert confidently and wrongly, which is precisely why the
production rules live in four named functions with three-point boundary tests.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

# Imported as a MODULE, not `from config import BIOMEDICAL_ENABLED`: the router
# reads `config.BIOMEDICAL_ENABLED` at call time, so the module-flag test has to
# patch the same module object rather than a copied constant.
import config
from auth import create_token
from database import db_session
from main import app
from services import biomedical_service as svc

client = TestClient(app)

# Distinctive so this module's fixtures cannot collide with another agent's in
# the shared session DB.
TAG = "BIO"


# ═════════════════════════════════════════════════════════════════════════════
#  Fixtures
# ═════════════════════════════════════════════════════════════════════════════

def _uid() -> str:
    return uuid.uuid4().hex[:8].upper()


def _mk_user(role: str = "admin") -> str:
    uid = f"{TAG}U{_uid()}"
    with db_session() as db:
        db.execute(
            "INSERT INTO users (id, name, role, password_hash) VALUES (?,?,?,'x')",
            (uid, f"Biomedical {role}", role),
        )
    return uid


def _auth(user_id: str, role: str) -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, role)}"}


@pytest.fixture(scope="module")
def admin_headers() -> dict:
    return _auth(_mk_user("admin"), "admin")


@pytest.fixture(scope="module")
def pharmacist_headers() -> dict:
    return _auth(_mk_user("pharmacist"), "pharmacist")


@pytest.fixture(scope="module")
def nurse_headers() -> dict:
    return _auth(_mk_user("nurse"), "nurse")


def _asset(**overrides) -> dict:
    """A fresh asset with a unique tag, so tests never share state."""
    payload = {
        "asset_tag": f"{TAG}-{_uid()}",
        "name": "Test device",
        "category": "patient_monitor",
        "criticality": "critical",
    }
    payload.update(overrides)
    return svc.upsert_asset(**payload)


def _dt(day: str, hhmm: str = "00:00") -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime.fromisoformat(day).replace(hour=hour, minute=minute,
                                               tzinfo=timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
#  1. Next-due arithmetic
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("last_done,interval,expected", [
    # The plain case.
    ("2026-01-01", 90, "2026-04-01"),
    # Across a month end, where a naive "+3 months" would land somewhere else.
    ("2026-01-31", 30, "2026-03-02"),
    # Across a year end.
    ("2026-12-15", 30, "2027-01-14"),
    # A full non-leap year.
    ("2026-03-01", 365, "2027-03-01"),
    # Through 29 February: 2028 IS a leap year, so a 365-day interval starting
    # 1 Mar 2027 lands on 29 Feb 2028, NOT 1 Mar. This is the case that proves
    # the module counts DAYS rather than pretending a year is a year.
    ("2027-03-01", 365, "2028-02-29"),
    # The shortest legal interval.
    ("2026-06-30", 1, "2026-07-01"),
])
def test_next_due_is_last_done_plus_interval_days(last_done, interval, expected):
    assert svc.compute_next_due(date.fromisoformat(last_done), interval) == \
        date.fromisoformat(expected)


def test_next_due_is_stored_on_the_schedule_not_recomputed_by_the_reader():
    """The stored column and the pure function must agree — a schedule whose
    `next_due_date` disagreed with `compute_next_due` would make the register
    and any report disagree about the same machine."""
    asset = _asset()
    sched = svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                                interval_days=90, last_done_date="2026-01-01")
    assert sched["next_due_date"] == "2026-04-01"
    assert sched["next_due_date"] == svc.compute_next_due(date(2026, 1, 1), 90).isoformat()


def test_an_interval_must_be_a_positive_whole_number_of_days():
    asset = _asset()
    for bad in (0, -30):
        with pytest.raises(svc.BiomedicalError) as exc:
            svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                                interval_days=bad, last_done_date="2026-01-01")
        assert exc.value.code == "bad_interval"


# ═════════════════════════════════════════════════════════════════════════════
#  2. THE OVERDUE BOUNDARY — the day it becomes overdue, not the day after
# ═════════════════════════════════════════════════════════════════════════════

# Calibration done 1 Jan 2026, 90-day interval → next due 1 April 2026.
# The certificate covers 1 Jan..31 Mar. On 1 APRIL it has run out.
_LAST_DONE = "2026-01-01"
_INTERVAL = 90
_DUE = "2026-04-01"


@pytest.mark.parametrize("today,expected_overdue", [
    ("2026-03-31", False),   # last covered day — still compliant
    ("2026-04-01", True),    # THE DAY IT BECOMES OVERDUE
    ("2026-04-02", True),    # and it stays overdue
])
def test_overdue_boundary_the_predicate(today, expected_overdue):
    """The rule itself, with no database anywhere near it."""
    assert svc.is_overdue(date.fromisoformat(_DUE),
                          date.fromisoformat(today)) is expected_overdue


@pytest.mark.parametrize("today,expected_overdue", [
    ("2026-03-31", False),
    ("2026-04-01", True),
    ("2026-04-02", True),
])
def test_overdue_register_catches_the_day_it_becomes_overdue(today, expected_overdue):
    """THE SAME BOUNDARY, THROUGH THE VIEW A WARD ACTUALLY READS.

    Asserted separately from the predicate on purpose: a correct `is_overdue`
    wired into a `WHERE next_due_date < ?` filter, or into a `_schedule_view`
    that compares dates itself, is the same off-by-one wearing a disguise, and
    the predicate test would stay green through it.
    """
    asset = _asset(ward=f"WARD-{_uid()}")
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=_INTERVAL, last_done_date=_LAST_DONE)

    register = svc.due_register(ward=asset["ward"], today=date.fromisoformat(today))
    tags = [r["asset_asset_tag"] for r in register["overdue"]]

    assert (asset["asset_tag"] in tags) is expected_overdue
    assert (register["overdue_count"] == 1) is expected_overdue
    # And the complement: on the last covered day it is DUE SOON, not silent.
    if not expected_overdue:
        assert register["due_soon_count"] == 1


def test_days_overdue_is_zero_on_the_first_overdue_day_and_one_the_next():
    """0 on the first day is arithmetic, not a bug — nothing has elapsed. The
    panel says 'Overdue today' for 0; what must not happen is 0 being read as
    'not overdue' by anything downstream."""
    due = date.fromisoformat(_DUE)
    assert svc.days_overdue(due, date.fromisoformat("2026-03-31")) == 0
    assert svc.days_overdue(due, due) == 0
    assert svc.is_overdue(due, due) is True          # …and it IS overdue
    assert svc.days_overdue(due, due + timedelta(days=1)) == 1
    assert svc.days_overdue(due, due + timedelta(days=45)) == 45


@pytest.mark.parametrize("today,expected_overdue", [
    ("2026-04-07", False),   # inside a 7-day grace window
    ("2026-04-08", True),    # grace exhausted — the same >= boundary, shifted
])
def test_grace_days_shift_the_boundary_and_do_not_soften_it(today, expected_overdue):
    """Grace moves the day; it does not change the rule. A site that documents a
    7-day tolerance gets exactly 7 days, and day 8 is overdue — not day 9."""
    asset = _asset(ward=f"WARD-{_uid()}")
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="preventive_maintenance",
                        interval_days=_INTERVAL, last_done_date=_LAST_DONE,
                        grace_days=7)
    register = svc.due_register(ward=asset["ward"], today=date.fromisoformat(today))
    assert (register["overdue_count"] == 1) is expected_overdue
    assert svc.is_overdue(date.fromisoformat(_DUE), date.fromisoformat(today),
                          grace_days=7) is expected_overdue


def test_recording_the_work_clears_it_from_the_overdue_register():
    """The other half of the boundary: the register must also let go."""
    asset = _asset(ward=f"WARD-{_uid()}")
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=_INTERVAL, last_done_date=_LAST_DONE)
    overdue_day = date.fromisoformat("2026-04-10")
    assert svc.due_register(ward=asset["ward"], today=overdue_day)["overdue_count"] == 1

    done = svc.record_schedule_completion(
        asset_id=asset["id"], schedule_type="calibration",
        performed_date="2026-04-10", certificate_reference="CAL/2026/1",
        today=overdue_day)

    # Next due runs from the day the work was DONE, not from the old due date.
    assert done["next_due_date"] == "2026-07-09"   # 2026-04-10 + 90
    assert done["recorded_was_overdue"] is True
    assert done["recorded_days_overdue"] == 9      # 1 Apr → 10 Apr
    assert svc.due_register(ward=asset["ward"], today=overdue_day)["overdue_count"] == 0


def test_a_failed_calibration_does_not_certify_anything():
    """A device that FAILED calibration is not calibrated. Rolling the schedule
    forward would convert one failure into a year of false compliance."""
    asset = _asset(ward=f"WARD-{_uid()}")
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=_INTERVAL, last_done_date=_LAST_DONE)
    day = date.fromisoformat("2026-04-10")

    result = svc.record_schedule_completion(
        asset_id=asset["id"], schedule_type="calibration",
        performed_date="2026-04-10", result="fail", today=day)

    assert result["next_due_date"] == _DUE          # unmoved
    assert result["is_overdue"] is True
    assert svc.due_register(ward=asset["ward"], today=day)["overdue_count"] == 1
    # …but the attempt is on the record, because "we tried and it failed" is the
    # fact an assessor and a vendor conversation both need.
    history = svc.get_asset(asset["id"], today=day)["history"]
    assert [h["result"] for h in history] == ["fail"]


# ═════════════════════════════════════════════════════════════════════════════
#  3. Unknown is not compliant
# ═════════════════════════════════════════════════════════════════════════════

def test_a_device_never_calibrated_is_overdue_not_invisible():
    """`last_done_date IS NULL` means NEVER DONE. It is the most dangerous row
    the register can hold, so it must be LOUD — counted in the overdue register
    and flagged, not filtered out as "no history to compare against"."""
    asset = _asset(ward=f"WARD-{_uid()}")
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=365, first_due_date="2026-01-01")

    register = svc.due_register(ward=asset["ward"], today=date(2026, 6, 1))
    assert register["overdue_count"] == 1
    assert register["never_done_count"] == 1
    assert register["overdue"][0]["never_done"] is True
    assert register["overdue"][0]["last_done_date"] is None


def test_a_schedule_with_no_basis_for_its_due_date_is_refused():
    """No `last_done_date` and no `first_due_date` → refusal, NOT a silent
    "today + interval". That default would mark every legacy machine compliant
    for a full cycle on the day it was entered, and the resulting screen would
    look perfect."""
    asset = _asset()
    with pytest.raises(svc.BiomedicalError) as exc:
        svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                            interval_days=365)
    assert exc.value.code == "missing_due_basis"


def test_an_asset_with_no_schedule_at_all_is_reported_as_a_gap():
    """The register's blind spot. An overdue view can only report on schedules
    that exist, so a machine nobody scheduled is perfectly compliant according
    to every other read in this module."""
    asset = _asset(criticality="life_support")
    gaps = svc.coverage_gaps()
    assert asset["asset_tag"] in [g["asset_tag"] for g in gaps]

    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=365, first_due_date="2026-01-01")
    assert asset["asset_tag"] not in [g["asset_tag"] for g in svc.coverage_gaps()]


def test_an_unpriced_asset_reads_as_words_not_as_zero():
    """₹0.00 is a price. "Not configured" is the absence of one."""
    unpriced = _asset()
    assert unpriced["purchase_cost_paise"] is None
    assert unpriced["cost_is_configured"] == 0
    assert unpriced["cost_display"] == "Not configured"

    free = _asset(purchase_cost_paise=0)
    assert free["cost_is_configured"] == 1
    assert free["cost_display"] != "Not configured"


# ═════════════════════════════════════════════════════════════════════════════
#  4. Downtime accumulates
# ═════════════════════════════════════════════════════════════════════════════

def test_downtime_accumulates_across_multiple_service_calls():
    """Three separate outages on one machine are the SUM of their durations.

    This is the number a hospital takes into a vendor review, and the natural
    wrong answers — the longest outage, or the most recent one — are both
    plausible enough to survive a glance at a screen.
    """
    asset = _asset()
    outages = [
        (_dt("2026-05-01", "08:00"), _dt("2026-05-01", "10:00"), 120),
        (_dt("2026-05-10", "08:00"), _dt("2026-05-10", "11:00"), 180),
        # Across midnight, which is where a naive same-day subtraction breaks.
        (_dt("2026-05-20", "20:00"), _dt("2026-05-21", "01:00"), 300),
    ]
    for down_from, restored_at, expected in outages:
        call = svc.open_service_call(
            asset_id=asset["id"], fault_description="Screen blank",
            reported_at=down_from)
        closed = svc.close_service_call(call_id=call["id"], restored_at=restored_at)
        assert closed["downtime_minutes"] == expected

    total = svc.asset_downtime_minutes(asset["id"])
    assert total == 120 + 180 + 300 == 600
    # Not the longest outage, and not the most recent — the two wrong answers
    # that are plausible enough to survive a glance at a screen.
    assert total != 300


def test_downtime_counts_from_when_the_machine_went_down_not_when_it_was_reported():
    """A pump fails at 02:10 and the call is logged at 07:00 when the day shift
    arrives. Charging downtime from 07:00 flatters the figure by five hours."""
    asset = _asset()
    call = svc.open_service_call(
        asset_id=asset["id"], fault_description="Occlusion alarm, will not clear",
        reported_at=_dt("2026-05-02", "07:00"), down_from=_dt("2026-05-02", "02:10"))
    assert call["down_from_assumed"] is False
    svc.close_service_call(call_id=call["id"], restored_at=_dt("2026-05-02", "09:10"))
    assert svc.asset_downtime_minutes(asset["id"]) == 420    # 02:10 → 09:10


def test_a_call_that_never_took_the_machine_out_of_service_adds_no_downtime():
    """A noisy fan is a service call, not an outage. Conflating them makes the
    availability figure meaningless."""
    asset = _asset()
    call = svc.open_service_call(
        asset_id=asset["id"], fault_description="Fan noisy", severity="degraded",
        reported_at=_dt("2026-05-03", "09:00"), took_out_of_service=False)
    svc.close_service_call(call_id=call["id"], restored_at=_dt("2026-05-04", "09:00"))
    assert svc.asset_downtime_minutes(asset["id"]) == 0
    assert svc.get_service_call(call["id"])["downtime_minutes"] == 0


def test_an_open_call_keeps_accruing_downtime():
    """A machine that has been down for a week and is still down is not zero
    downtime. The running figure is computed, never stored."""
    asset = _asset()
    call = svc.open_service_call(
        asset_id=asset["id"], fault_description="Dead",
        reported_at=_dt("2026-05-05", "08:00"))
    assert call["downtime_minutes"] is None          # nothing frozen yet
    assert call["downtime_is_running"] is True

    now = _dt("2026-05-05", "14:00")
    assert svc.asset_downtime_minutes(asset["id"], now=now) == 360


def test_a_breakdown_marks_the_asset_down_and_closing_it_brings_it_back():
    """The register and the ward view must agree without anybody remembering to
    update two places."""
    asset = _asset()
    call = svc.open_service_call(asset_id=asset["id"], fault_description="No power",
                                 reported_at=_dt("2026-05-06", "08:00"))
    assert svc.get_asset(asset["id"])["status"] == "down"
    svc.close_service_call(call_id=call["id"], restored_at=_dt("2026-05-06", "12:00"))
    assert svc.get_asset(asset["id"])["status"] == "in_service"


def test_a_second_open_fault_keeps_the_machine_down():
    """Closing one call must not declare the machine fit while another fault is
    still open against it."""
    asset = _asset()
    first = svc.open_service_call(asset_id=asset["id"], fault_description="No power",
                                  reported_at=_dt("2026-05-07", "08:00"))
    svc.open_service_call(asset_id=asset["id"], fault_description="Cracked sensor",
                          reported_at=_dt("2026-05-07", "09:00"))
    svc.close_service_call(call_id=first["id"], restored_at=_dt("2026-05-07", "10:00"))
    assert svc.get_asset(asset["id"])["status"] == "down"


def test_a_call_cannot_be_closed_before_the_machine_went_down():
    asset = _asset()
    call = svc.open_service_call(asset_id=asset["id"], fault_description="X",
                                 reported_at=_dt("2026-05-08", "10:00"))
    with pytest.raises(svc.BiomedicalError) as exc:
        svc.close_service_call(call_id=call["id"], restored_at=_dt("2026-05-08", "09:00"))
    assert exc.value.code == "bad_date_order"


def test_a_closed_call_cannot_be_closed_twice():
    """Double-closing would recompute and overwrite a downtime figure the
    hospital may already have reported."""
    asset = _asset()
    call = svc.open_service_call(asset_id=asset["id"], fault_description="X",
                                 reported_at=_dt("2026-05-09", "08:00"))
    svc.close_service_call(call_id=call["id"], restored_at=_dt("2026-05-09", "09:00"))
    with pytest.raises(svc.BiomedicalError) as exc:
        svc.close_service_call(call_id=call["id"], restored_at=_dt("2026-05-09", "18:00"))
    assert exc.value.code == "already_closed"
    assert svc.get_service_call(call["id"])["downtime_minutes"] == 60


# ═════════════════════════════════════════════════════════════════════════════
#  5. AMC / CMC expiry — the OPPOSITE boundary rule, pinned so it cannot drift
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("today,expired", [
    ("2027-03-30", False),
    ("2027-03-31", False),   # the LAST COVERED DAY — the vendor still comes out
    ("2027-04-01", True),    # …and now they do not
])
def test_a_contract_is_covered_through_its_end_date(today, expired):
    """`end_date` is the last day the contract covers, because that is what
    "valid to 31 March 2027" means on the paper a hospital signed. This is the
    OPPOSITE boundary from a maintenance schedule's `next_due_date`, which is the
    first UNCOVERED day. Both are pinned so neither can drift into the other."""
    assert svc.contract_is_expired(date(2027, 3, 31),
                                   date.fromisoformat(today)) is expired


def test_the_two_boundary_rules_are_deliberately_different():
    """Stated as an assertion so a future edit that "makes them consistent"
    breaks a test with a sentence attached rather than silently shifting one of
    them by a day."""
    day = date(2027, 3, 31)
    assert svc.is_overdue(day, day) is True             # due date  → already late
    assert svc.contract_is_expired(day, day) is False   # end date  → still covered


@pytest.mark.parametrize("today,warn", [
    ("2027-01-29", False),   # 61 days of cover left — outside a 60-day window
    ("2027-01-30", True),    # 60 days left — THE FIRST DAY OF THE WINDOW
    ("2027-03-31", True),    # last covered day, still a warning
    ("2027-04-01", False),   # expired: a louder state, not this one
])
def test_amc_warning_fires_on_the_first_day_of_the_notice_window(today, warn):
    end = date(2027, 3, 31)
    assert svc.contract_in_notice_window(end, 60, date.fromisoformat(today)) is warn


def test_the_expiring_report_separates_a_warning_from_an_uncovered_machine():
    """"Renew this" and "you are uncovered right now" must not share a bucket."""
    number = f"{TAG}-AMC-{_uid()}"
    asset = _asset()
    svc.upsert_contract(contract_number=number, contract_type="amc",
                        start_date="2026-04-01", end_date="2027-03-31",
                        vendor_name="Test Biomed Services", notice_days=60,
                        asset_ids=[asset["id"]])

    quiet = svc.expiring_contracts(today=date(2027, 1, 29))     # 61 days left
    assert number not in [c["contract_number"] for c in quiet["expiring"]]

    warning = svc.expiring_contracts(today=date(2027, 1, 30))   # 60 days left
    hit = [c for c in warning["expiring"] if c["contract_number"] == number]
    assert len(hit) == 1
    assert hit[0]["cover_state"] == "expiring"
    assert hit[0]["days_remaining"] == 60
    assert hit[0]["asset_count"] == 1

    gone = svc.expiring_contracts(today=date(2027, 4, 1))
    assert number not in [c["contract_number"] for c in gone["expiring"]]
    assert number in [c["contract_number"] for c in gone["expired"]]


def test_a_terminated_contract_is_not_reported_as_expiring():
    """Status wins over arithmetic: a human decided this one is over."""
    number = f"{TAG}-AMC-{_uid()}"
    svc.upsert_contract(contract_number=number, start_date="2026-04-01",
                        end_date="2027-03-31", notice_days=60, status="terminated")
    report = svc.expiring_contracts(today=date(2027, 1, 31))
    assert number not in [c["contract_number"] for c in report["expiring"]]
    assert number not in [c["contract_number"] for c in report["expired"]]


def test_an_unvalued_contract_reads_as_words_not_as_zero():
    number = f"{TAG}-AMC-{_uid()}"
    contract = svc.upsert_contract(contract_number=number, start_date="2026-04-01",
                                   end_date="2027-03-31")
    assert contract["value_paise"] is None
    assert contract["value_display"] == "Not configured"


def test_contract_coverage_is_replaced_only_when_it_is_named():
    """Updating a contract's value must not silently uncover a fleet."""
    number = f"{TAG}-AMC-{_uid()}"
    a1, a2 = _asset(), _asset()
    svc.upsert_contract(contract_number=number, start_date="2026-04-01",
                        end_date="2027-03-31", asset_ids=[a1["id"], a2["id"]])
    updated = svc.upsert_contract(contract_number=number, start_date="2026-04-01",
                                  end_date="2027-03-31", value_paise=250000000)
    assert updated["asset_count"] == 2                     # untouched

    cleared = svc.upsert_contract(contract_number=number, start_date="2026-04-01",
                                  end_date="2027-03-31", asset_ids=[])
    assert cleared["asset_count"] == 0                     # explicit, deliberate


def test_linking_an_unknown_asset_to_a_contract_is_refused():
    """A coverage list that silently dropped a mistyped tag only surfaces when
    the vendor declines a call."""
    number = f"{TAG}-AMC-{_uid()}"
    with pytest.raises(svc.UnknownAsset):
        svc.upsert_contract(contract_number=number, start_date="2026-04-01",
                            end_date="2027-03-31", asset_tags=["NO-SUCH-TAG-XYZ"])


# ═════════════════════════════════════════════════════════════════════════════
#  6. The ward view — the clinically-visible slice
# ═════════════════════════════════════════════════════════════════════════════

def test_a_ward_sees_its_own_overdue_calibration_and_nobody_elses():
    ours = f"WARD-{_uid()}"
    theirs = f"WARD-{_uid()}"
    mine = _asset(ward=ours, produces_clinical_readings=True)
    other = _asset(ward=theirs, produces_clinical_readings=True)
    for a in (mine, other):
        svc.upsert_schedule(asset_id=a["id"], schedule_type="calibration",
                            interval_days=_INTERVAL, last_done_date=_LAST_DONE)

    view = svc.ward_attention(ours, today=date(2026, 4, 1))
    assert view["overdue_count"] == 1
    assert view["overdue"][0]["asset_asset_tag"] == mine["asset_tag"]
    assert other["asset_tag"] not in [r["asset_asset_tag"] for r in view["overdue"]]
    assert "overdue for calibration" in view["headline"]


def test_the_ward_view_flags_a_measuring_device_specifically():
    """An overdue calibration on a device Orb reads numbers FROM is the specific
    thing this module was built to surface — it is not just a paperwork lapse."""
    ward = f"WARD-{_uid()}"
    measuring = _asset(ward=ward, produces_clinical_readings=True)
    trolley = _asset(ward=ward, produces_clinical_readings=False)
    for a in (measuring, trolley):
        svc.upsert_schedule(asset_id=a["id"], schedule_type="calibration",
                            interval_days=_INTERVAL, last_done_date=_LAST_DONE)

    view = svc.ward_attention(ward, today=date(2026, 4, 1))
    assert view["overdue_count"] == 2
    assert view["measurement_at_risk_count"] == 1
    at_risk = [r for r in view["overdue"] if r["measurement_at_risk"]]
    assert [r["asset_asset_tag"] for r in at_risk] == [measuring["asset_tag"]]


def test_a_quiet_ward_says_so_rather_than_showing_an_empty_list():
    ward = f"WARD-{_uid()}"
    _asset(ward=ward)
    view = svc.ward_attention(ward, today=date(2026, 4, 1))
    assert view["overdue_count"] == 0
    assert view["headline"] == "No equipment on this ward is overdue or out of service."


def test_the_ward_payload_carries_no_patient_data():
    """The reason this view can be readable by every clinical role. Asserted
    rather than assumed, because a future field added "for context" is exactly
    how a no-PHI payload stops being one."""
    ward = f"WARD-{_uid()}"
    asset = _asset(ward=ward)
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=_INTERVAL, last_done_date=_LAST_DONE)
    view = svc.ward_attention(ward, today=date(2026, 4, 1))

    banned = {"patient_id", "patient_name", "mrn", "admission_id", "encounter_id"}

    def walk(node):
        if isinstance(node, dict):
            assert not (banned & set(node)), f"PHI-shaped key in ward payload: {set(node) & banned}"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(view)


# ═════════════════════════════════════════════════════════════════════════════
#  7. The register itself
# ═════════════════════════════════════════════════════════════════════════════

def test_an_asset_tag_identifies_exactly_one_machine():
    """Two rows for one physical device means two people each maintaining what
    they believe is its whole history."""
    tag = f"{TAG}-{_uid()}"
    svc.upsert_asset(asset_tag=tag, name="First name", ward="A")
    again = svc.upsert_asset(asset_tag=tag.lower(), name="Corrected name", ward="B")
    assert again["name"] == "Corrected name"
    assert again["ward"] == "B"
    assert len(svc.list_assets(search=tag)) == 1


def test_condemning_an_asset_keeps_its_history():
    """Condemned is not deleted. An assessor asking "what happened to pump 14"
    must get an answer, and a deleted row is how a machine with a bad history
    quietly returns to a ward."""
    asset = _asset()
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=90, last_done_date="2026-01-01")
    svc.set_asset_status(asset_id=asset["id"], status="condemned",
                         reason="Beyond economic repair")

    assert asset["asset_tag"] not in [a["asset_tag"] for a in svc.list_assets()]
    still_there = svc.get_asset(asset["id"])
    assert still_there["status"] == "condemned"
    assert still_there["is_active"] == 0
    assert len(still_there["schedules"]) == 1


def test_condemning_needs_a_reason():
    asset = _asset()
    with pytest.raises(svc.BiomedicalError) as exc:
        svc.set_asset_status(asset_id=asset["id"], status="condemned")
    assert exc.value.code == "missing_field"


def test_a_schedule_for_an_unknown_asset_is_refused():
    with pytest.raises(svc.UnknownAsset):
        svc.upsert_schedule(asset_tag="NO-SUCH-ASSET-XYZ", schedule_type="calibration",
                            interval_days=90, last_done_date="2026-01-01")


def test_maintenance_cannot_be_recorded_in_the_future():
    """A calibration certificate dated next month is a typo or a lie, and either
    way it would grant cover nobody has earned."""
    asset = _asset()
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=90, last_done_date="2026-01-01")
    with pytest.raises(svc.BiomedicalError) as exc:
        svc.record_schedule_completion(
            asset_id=asset["id"], schedule_type="calibration",
            performed_date="2026-05-01", today=date(2026, 4, 1))
    assert exc.value.code == "future_date"


def test_an_asset_carries_one_schedule_per_type():
    """A ventilator has a PM schedule AND a calibration schedule on different
    intervals; re-saving one must not create a second of the same kind."""
    asset = _asset()
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=365, last_done_date="2026-01-01")
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="preventive_maintenance",
                        interval_days=90, last_done_date="2026-01-01")
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=180, last_done_date="2026-01-01")

    schedules = svc.list_schedules(asset_id=asset["id"])
    assert len(schedules) == 2
    calibration = [s for s in schedules if s["schedule_type"] == "calibration"][0]
    assert calibration["interval_days"] == 180


def test_the_overdue_register_puts_the_worst_first():
    """The list an assessor is handed, and the one a busy engineer reads only the
    top of."""
    ward = f"WARD-{_uid()}"
    late = _asset(ward=ward, criticality="routine")
    later = _asset(ward=ward, criticality="life_support")
    svc.upsert_schedule(asset_id=late["id"], schedule_type="calibration",
                        interval_days=90, last_done_date="2026-01-01")   # due 1 Apr
    svc.upsert_schedule(asset_id=later["id"], schedule_type="calibration",
                        interval_days=30, last_done_date="2026-01-01")   # due 31 Jan

    register = svc.due_register(ward=ward, today=date(2026, 4, 10))
    assert [r["asset_asset_tag"] for r in register["overdue"]] == \
        [later["asset_tag"], late["asset_tag"]]
    assert register["overdue_life_support_count"] == 1


# ═════════════════════════════════════════════════════════════════════════════
#  8. The API surface
# ═════════════════════════════════════════════════════════════════════════════

def test_the_overdue_register_is_reachable_over_http(admin_headers):
    ward = f"WARD-{_uid()}"
    asset = _asset(ward=ward, produces_clinical_readings=True)
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="calibration",
                        interval_days=_INTERVAL, last_done_date=_LAST_DONE)

    resp = client.get(f"/api/biomedical/due?ward={ward}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    # Today is real "now" here, which is well past April 2026 in this fixture's
    # frame — the point of the HTTP test is the wiring, not the boundary.
    assert body["overdue_count"] >= 0
    assert "as_of" in body


def test_a_ward_nurse_can_read_the_ward_view(nurse_headers):
    """The whole point of the module: the ward using the machine finds out.
    A nurse holds vitals.read and no administrative permission at all."""
    ward = f"WARD-{_uid()}"
    _asset(ward=ward)
    resp = client.get(f"/api/biomedical/wards/{ward}/attention", headers=nurse_headers)
    assert resp.status_code == 200
    assert resp.json()["ward"] == ward


def test_a_nurse_cannot_read_the_whole_equipment_register(nurse_headers):
    """The ward view is scoped and PHI-free; the register carries costs,
    contracts and every machine in the hospital."""
    assert client.get("/api/biomedical/assets", headers=nurse_headers).status_code == 403
    assert client.get("/api/biomedical/contracts", headers=nurse_headers).status_code == 403


def test_a_contract_needs_an_administrator(pharmacist_headers, admin_headers):
    """A maintenance contract is money the hospital has committed. `helix.write`
    (which a pharmacist holds) is not enough."""
    payload = {"contract_number": f"{TAG}-AMC-{_uid()}",
               "start_date": "2026-04-01", "end_date": "2027-03-31"}
    assert client.post("/api/biomedical/contracts", json=payload,
                       headers=pharmacist_headers).status_code == 403
    assert client.post("/api/biomedical/contracts", json=payload,
                       headers=admin_headers).status_code == 200


def test_condemning_needs_an_administrator(pharmacist_headers, admin_headers):
    asset = _asset()
    body = {"status": "condemned", "reason": "Beyond economic repair"}
    assert client.post(f"/api/biomedical/assets/{asset['id']}/status", json=body,
                       headers=pharmacist_headers).status_code == 403
    assert client.post(f"/api/biomedical/assets/{asset['id']}/status", json=body,
                       headers=admin_headers).status_code == 200


def test_a_switched_off_module_explains_itself(admin_headers, monkeypatch):
    """404 with a sentence, not a blank. A client built before the flag flipped
    deserves an explanation."""
    monkeypatch.setattr(config, "BIOMEDICAL_ENABLED", False)
    resp = client.get("/api/biomedical/overview", headers=admin_headers)
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error"] == "module_disabled"
    assert detail["module"] == "biomedical"
    assert "ORB_BIOMEDICAL" in detail["message"]


def test_the_module_flag_is_on_by_default_and_published_on_health():
    """The frontend hides the tab from this answer, so the two must agree."""
    assert config.BIOMEDICAL_ENABLED is True
    modules = client.get("/api/health").json()["modules"]
    assert modules["biomedical"] is True


def test_the_api_publishes_the_boundary_rules_so_a_client_cannot_invent_them(admin_headers):
    meta = client.get("/api/biomedical/meta", headers=admin_headers).json()
    assert "ON OR AFTER" in meta["due_rule"]
    assert "AFTER its end date" in meta["contract_rule"]
    assert "calibration" in meta["schedule_types"]


def test_a_bad_date_is_refused_with_a_code_a_client_can_branch_on(admin_headers):
    resp = client.post("/api/biomedical/assets", headers=admin_headers, json={
        "asset_tag": f"{TAG}-{_uid()}", "name": "X", "purchase_date": "31-02-2026"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "bad_date"


def test_recording_maintenance_over_http_rolls_the_schedule_forward(admin_headers):
    asset = _asset()
    svc.upsert_schedule(asset_id=asset["id"], schedule_type="preventive_maintenance",
                        interval_days=90, last_done_date="2026-01-01")
    today = svc.hospital_today()
    resp = client.post("/api/biomedical/schedules/complete", headers=admin_headers,
                       json={"asset_id": asset["id"],
                             "schedule_type": "preventive_maintenance",
                             "performed_date": today.isoformat(),
                             "result": "pass"})
    assert resp.status_code == 200
    assert resp.json()["next_due_date"] == (today + timedelta(days=90)).isoformat()
    assert resp.json()["compliance"] == "ok"


# ═════════════════════════════════════════════════════════════════════════════
#  9. This module is not a clinical path
# ═════════════════════════════════════════════════════════════════════════════

def test_no_biomedical_table_is_read_by_the_clinical_write_paths():
    """A grep-shaped guard, deliberately. The ruling is that nothing here may
    block a clinical action, and the cheapest durable way to hold that line is to
    assert that the clinical modules do not import or query these tables at all.
    If a future change genuinely needs the link, this test is the conversation.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    clinical = [
        "services/vigil_service.py", "services/emar_service.py",
        "services/orders_service.py", "services/shadow_service.py",
        "services/medication_safety_gate.py", "services/audit_service.py",
        "services/ingestion_service.py", "services/nurse_shift_service.py",
    ]
    for rel in clinical:
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        assert "biomedical" not in text.lower(), (
            f"{rel} references the biomedical module. This module must never be "
            f"able to block or alter a clinical action — see the ruling in "
            f"services/biomedical_service.py."
        )
