"""Laboratory results: section worklists and manual entry with flags
(lis-10), technical validation, authorisation and release into the chart
(lis-11), and delta checks (lis-12).

Behind ORB_LAB (default off): every route that reaches this module answers
404 while the laboratory is off, and nothing here writes while it is. Entry
and pre-validation edits finish open work, so they are served while the
module drains (ruling LIS-R21).

THE WORKLIST of a section (biochemistry, haematology...) holds every test on
a RECEIVED sample that is waiting for a result or has one entered and not yet
technically validated: STAT first, then the oldest receipt. A cancelled test
leaves it at once, and the worklist says who cancelled it and when, so the
bench stops.

WHAT A VALUE IS
  * numeric: typed as text at the test's reporting precision. More decimal
    places than the test reports is refused (never rounded); fewer is the
    same number ("5.1" for a two-place test is 5.10). Stored as value_milli
    (thousandths) with value_text at the reporting precision, because
    trailing zeros are part of the report.
  * a comparator result ('<0.01', '>600'): the comparator and the bound are
    kept in comparator and value_text, and value_milli stays NULL, as
    migration 55 defines the column, so no reader can ever treat a bound as
    an exact value (the chart cannot plot it, a delta check skips it). The
    bound is still held to the test's precision.
  * coded: one of the test's coded values, in the catalogue's spelling;
    anything else is refused.
  * text: free text.

FLAGS (rulings LIS-R2 and LIS-R7, strictest options, held for Samuel to
confirm before go-live): H and L come ONLY from an approved reference range
(lab_master_service.applicable_range), compared at the test's reporting
precision: L below low, H above high, so a value equal to a limit is within
the range. No range ships, so until a pathologist approves one nothing is
flagged and the result says "Reference range not configured". A patient with
no date of birth gets only a range that covers every age; one with no
recorded sex only a range for any sex; otherwise no range, no flag, and the
reason in words. The age is the age when the sample was collected (from
patients.date_of_birth, never the static patients.age); the range version is
the one approved when the value is entered. A comparator result, a coded
result and a text result carry no H or L.

CRITICAL FLAGS (lis-11; ruling LIS-R1, strictest option): HH and LL come
ONLY from a critical limit a pathologist approved (none ships), decided at
entry and at each edit by the same rule as the range (the limit approved now,
the patient's age at collection), at the reporting precision. "Critical
above 6.5" means above: a value equal to a critical limit is not critical. A
bound wholly beyond a limit ('>600' against "critical above 450") is
critical, because the true value is certainly past it; a bound that could be
either side is not. A critical flag outranks H or L. is_critical and
critical_limit_id say which limit was crossed. The critical result is RAISED
at technical validation (lis-15, ruling LIS-R4 (a): marked preliminary, not
yet authorised), at release for one validated before that, and at an
amendment whose new value crosses a limit, through
services/lab_critical_service.py, inside the step's own transaction; it is
posted to the patient's case room after that transaction commits, and a
failed post never undoes the step. A result sent back to the bench after its
critical was told, changed and validated again tells the ward the change the
same way: a changed critical value, or a correction when it is no longer
critical (a critical already confirmed is reopened then, so the laboratory
phones the correction).

DELTA CHECKS (lis-12; ruling LIS-R9 (a), strictest option): a numeric test
with a delta kind and threshold in the catalogue (none ships one) compares a
value with the patient's newest earlier live result for the same code,
observed before this sample was collected, read through
lab_observations.previous_result (julianday order, id DESC, simulated rows
left out), from any lane. Absolute: the change in thousandths of the unit;
percent: the change over the earlier value, the threshold in thousandths of
a percent. A change AT or beyond the threshold is flagged. An earlier result
outside the test's window (hours), none at all, a bound on either side, a
text earlier result or an earlier result in another unit is not checked, and
the result says why. A flag needs a technologist's reason before technical
validation; the reason is kept with who and when and shown to the signatory.
The check is decided at entry and at each edit and decided again at the
reason and at technical validation, so a reason given against one earlier
result never covers a different one.

TECHNICAL VALIDATION, AUTHORISATION AND RELEASE (lis-11; rulings LIS-R5,
LIS-R8, LIS-R16). No autoverification: a person technically validates every
result (lab.result) and a signatory authorises it (lab.authorise, one sample
at a time). While config.LAB_REQUIRE_DISTINCT_AUTHORISER is on (the shipped
default until LIS-R5 is ruled) the signatory must be someone who did not
enter, change, explain the delta of or technically validate that result;
with it off one signatory may do every step, and the release records that
one person did. Authorising releases the result in ONE immediate
transaction: the result to 'released', one observations row per analyte (the
chart, ward pack, timeline and Tier 1 read it there), the specimen's
progress, the order reconciled by the completion rule (LIS-R16 (a): partial
panels release analyte by analyte) and a fail-closed clinical_access audit
row per result. The release never depends on the order's state: an order
already moved outside the laboratory still receives the value, and
reconcile_order_status records an 'order_state_conflict' exception. A
signatory who disagrees sends the result back to the bench with a reason.

AMENDED AND WITHDRAWN RESULTS (lis-13; ruling LIS-R10 (a), the recommended
option, held for Samuel). Once released, a result is never changed in place.
A signatory (lab.authorise) AMENDS it with a reason: the released row becomes
'superseded', a new row (version n+1, status 'released', amend_reason) takes
its place, the chart's observation is marked superseded with superseded_by
pointing at a new observation with status 'corrected', the order keeps its
status and its fixed result text, and an order_events row says the result
was amended. While two different people are required (LIS-R5, the shipped
default) an amendment is two steps: one signatory proposes it (recorded as a
result event; the chart keeps the reported value) and a DIFFERENT signatory
confirms it, which applies it; either may cancel a proposal. With the setting
off one signatory amends in one step and the amendment records that one
person did. A signatory WITHDRAWS a result entered in error (the wrong
patient, for example) with a reason, in one step: the row becomes
'withdrawn', its observation 'entered-in-error' (so no chart, ward pack,
timeline or Tier 1 reader shows it), its test is cancelled, and if it was the
order's only reported test the order is not reopened: the laboratory gets an
exception saying so. After either commits, a plain notice goes to the
patient's case room ("Laboratory"); a failed notice never undoes the change.
An amendment whose new value crosses a critical limit raises a critical
result. A critical raised for an earlier version is never closed by the
change; it records it, the same way as a result sent back to the bench
(lab_critical_service.raise_for_result_in_txn follows the version chain): an
open one takes the new value (or, for a withdrawal, 'withdrawn') and posts
it at once, and a closed one whose confirmed value no longer holds is
reopened at tier 1, so the laboratory phones the ward and logs the call with
a read-back on Critical results. The ward notice promises that call only
when a critical is open to track it.

EVERY WRITE is one BEGIN IMMEDIATE transaction with its clinical_access audit
row inside it (fail-closed) and a lab_result_events row: 'entered' with the
value after, 'edited' with the value before and after. An edit is allowed
only before technical validation; after that a change is an amendment
(lis-13). An edit says what the editor's screen showed (value, comment and
flag); if the stored result no longer matches, someone else changed it, and
the edit is refused 409 'result_changed' with the current row, so a stale
screen never overwrites another person's correction in silence. One live
result per test is guaranteed by the partial unique index
uq_lab_results_live_item: a second entry is refused, never doubled.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

import config
from database import db_session
from db_backend import DBIntegrityError
from permissions import LAB_AUTHORISE, LAB_RESULT, has_permission
from services import lab_critical_service as lcs
from services import lab_master_service as lm
from services import lab_module
from services import lab_observations as lobs
from services import lab_order_service as los
from services.audit_service import log_audit_event
from services.clinical_time import to_utc_iso, utc_iso_now

logger = logging.getLogger("orb.lab.results")

#: The most rows one worklist returns.
WORKLIST_CAP = 500
#: How far back the worklist lists tests cancelled after their sample was
#: received ("Cancelled by ... at ...").
CANCELLED_LOOKBACK_HOURS = 24
#: The most recently cancelled tests listed.
CANCELLED_LIST_CAP = 100
#: The longest text or coded result kept.
TEXT_RESULT_MAX = 2000
#: Thousandths. Far beyond any laboratory value, small enough to stay exact.
_MILLI_LIMIT = 10 ** 15

#: Item statuses on a section's worklist: waiting for a result, or entered
#: and not yet technically validated.
WORKLIST_ITEM_STATUSES = ("received", "resulted")
#: Specimen statuses whose tests are on the bench.
_BENCH_SPECIMEN_STATUSES = ("received", "in_process")
#: Result statuses an edit accepts.
_EDITABLE_RESULT_STATUSES = ("entered",)

#: The most results one technical validation or authorisation names.
VALIDATION_BATCH_MAX = 100
#: The most results each validation queue returns.
VALIDATION_QUEUE_CAP = 500
#: The status a released result carries in observations.
RELEASED_OBSERVATION_STATUS = "final"
#: The status an amended result's new observation carries (lis-13).
AMENDED_OBSERVATION_STATUS = "corrected"
#: The status the observation of a replaced version carries, with superseded_by.
SUPERSEDED_OBSERVATION_STATUS = "superseded"
#: The status the observation of a withdrawn result carries. Both are in
#: lab_observations.VOID_OBSERVATION_STATUSES, so no reader shows the row.
WITHDRAWN_OBSERVATION_STATUS = "entered-in-error"
#: Result events that record an amendment waiting for a second signatory.
_PROPOSED_EVENT = "amendment_proposed"
_CANCELLED_EVENT = "amendment_cancelled"
#: The incident a wrong-patient withdrawal prompts while ORB_NABH is on
#: (ruling LIS-R10's recommendation).
WRONG_PATIENT_INCIDENT_TEXT = (
    "A result was reported against the wrong patient. Report a patient identification "
    "incident in the NABH register.")
#: The longest reason kept for a delta flag or a result sent back.
REASON_MAX = 500
#: Result events whose actor took part in a result: the signatory must not be
#: one of them while two different people are required (ruling LIS-R5).
_PART_EVENTS = ("entered", "edited", "delta_acknowledged", "tech_validated")

#: The observations source labels this laboratory writes on a release.
_LIS_SOURCES = ("lis_manual", "lis_analyser")

#: Delta check outcomes. Only 'flagged' needs a reason.
DELTA_FLAGGED = "flagged"
DELTA_WITHIN = "within"
DELTA_NO_PREVIOUS = "no_previous"
DELTA_OUTSIDE_WINDOW = "outside_window"
DELTA_BOUND = "bound"
DELTA_PREVIOUS_NOT_NUMERIC = "previous_not_numeric"
DELTA_UNIT_MISMATCH = "unit_mismatch"
DELTA_NOT_CHECKED = "not_checked"

_COMPARATOR = re.compile(r"^(<=|>=|<|>|≤|≥)\s*(.*)$")
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_COMPARATOR_SPELLING = {"≤": "<=", "≥": ">="}

_PRIORITY_RANK_SQL = "CASE s.priority WHEN 'stat' THEN 0 WHEN 'urgent' THEN 1 ELSE 2 END"

_RESULT_TABLES = ("lab_tests", "lab_specimens", "lab_order_items", "lab_results",
                  "lab_result_events", "lab_sections", "lab_reference_ranges")


class LabResultError(Exception):
    """A refusal with a machine code, a sentence for the screen, the HTTP
    status the route answers with, and any extra fields for the body."""

    def __init__(self, code: str, message: str, *, status: int = 409, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


# ════════════════════════════════════════════════════════════════════════════
#  Pure helpers: parsing, precision and flags
# ════════════════════════════════════════════════════════════════════════════

def _places_words(decimal_places: int) -> str:
    if decimal_places == 0:
        return "whole numbers"
    return f"{decimal_places} decimal place" + ("" if decimal_places == 1 else "s")


def parse_numeric(text: Any, decimal_places: int, *, test_name: str = "This test") -> dict:
    """A typed numeric result, checked at the test's reporting precision.

    Returns {comparator, value_milli, value_text}. A comparator result keeps
    its bound in value_text ('<0.01') with value_milli None. Raises
    LabResultError (422): 'value_required', 'not_a_number' or 'precision'
    (more decimal places than the test reports; never rounded)."""
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise LabResultError("value_required", "Type the result.", status=422)
    comparator = None
    match = _COMPARATOR.match(raw)
    if match:
        comparator = _COMPARATOR_SPELLING.get(match.group(1), match.group(1))
        raw = match.group(2).strip()
    if not _NUMBER.match(raw):
        raise LabResultError(
            "not_a_number",
            "Type a number, such as 4.2, or a bound such as <0.01 or >600.", status=422)
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise LabResultError("not_a_number", "Type a number.", status=422)
    places = int(decimal_places)
    scaled = number * (Decimal(10) ** places)
    if scaled != scaled.to_integral_value():
        raise LabResultError(
            "precision",
            f"{test_name} is reported in {_places_words(places)}. Type the value as the "
            "report prints it; Orb does not round a result.",
            status=422, decimal_places=places)
    milli = number * 1000
    if abs(milli) >= _MILLI_LIMIT:
        raise LabResultError("out_of_range", "This value is out of range.", status=422)
    shown = f"{number:.{places}f}"
    if shown.startswith("-") and Decimal(shown) == 0:
        shown = shown[1:]
    if comparator:
        return {"comparator": comparator, "value_milli": None, "value_text": f"{comparator}{shown}"}
    return {"comparator": None, "value_milli": int(milli), "value_text": shown}


def parse_coded(text: Any, allowed: list[str]) -> str:
    """One of the test's coded values, matched without regard to case or
    surrounding spaces and stored in the catalogue's spelling. Anything else
    is refused (422 'not_in_list', with the allowed values)."""
    raw = re.sub(r"\s+", " ", str(text if text is not None else "")).strip()
    if not raw:
        raise LabResultError("value_required", "Choose the result.", status=422)
    for value in allowed:
        if value.casefold() == raw.casefold():
            return value
    raise LabResultError("not_in_list", "Choose one of this test's results.", status=422,
                         allowed=allowed)


def parse_text(text: Any) -> str:
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise LabResultError("value_required", "Type the result.", status=422)
    if len(raw) > TEXT_RESULT_MAX:
        raise LabResultError("too_long",
                             f"A text result is at most {TEXT_RESULT_MAX} characters.",
                             status=422)
    return raw


def at_precision(milli: Optional[int], decimal_places: int) -> Optional[int]:
    """Thousandths rounded half-up to the reporting precision, the rule the
    NEWS2 charted-value ruling uses: a flag is decided on the number the
    report prints, never on a finer one."""
    if milli is None:
        return None
    step = Decimal(10) ** (3 - int(decimal_places))
    value = Decimal(int(milli)) / step
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)


def range_flag(value_milli: Optional[int], *, low_milli: Optional[int],
               high_milli: Optional[int], decimal_places: int) -> str:
    """'L', 'H' or '' for a value against an approved range, at the
    reporting precision. Within means low <= value <= high, so a value equal
    to a limit is NOT flagged. A missing value (a comparator result) is never
    flagged. Pure."""
    value = at_precision(value_milli, decimal_places)
    if value is None:
        return ""
    low = at_precision(low_milli, decimal_places)
    high = at_precision(high_milli, decimal_places)
    if low is not None and value < low:
        return "L"
    if high is not None and value > high:
        return "H"
    return ""


def flag_text(result: dict) -> str:
    """The flag in words for the bench. Pure."""
    flag = result.get("abnormal_flag") or ""
    if flag == "H":
        return "High"
    if flag == "L":
        return "Low"
    if flag == "HH":
        return "Critically high"
    if flag == "LL":
        return "Critically low"
    if flag == "A":
        return "Abnormal"
    if result.get("result_type") not in (None, "numeric"):
        return "No flag"
    if result.get("comparator"):
        return "No flag: the value is a bound"
    if not result.get("reference_range_id"):
        return "No flag: reference range not configured"
    return "Within range"


def critical_flag(value_milli: Optional[int], *, low_milli: Optional[int],
                  high_milli: Optional[int], decimal_places: int,
                  comparator: Optional[str] = None,
                  bound_milli: Optional[int] = None) -> str:
    """'LL', 'HH' or '' against an approved critical limit, at the reporting
    precision. "Critical above 6.5" means above, so a value equal to a limit
    is not critical.

    A bound (comparator with `bound_milli`) is critical only when every value
    it allows is past the limit: '>' at or above the high limit, '>=' above
    it, '<' at or below the low limit, '<=' below it. Pure."""
    low = at_precision(low_milli, decimal_places)
    high = at_precision(high_milli, decimal_places)
    if comparator:
        bound = at_precision(bound_milli, decimal_places)
        if bound is None:
            return ""
        if high is not None and ((comparator == ">" and bound >= high)
                                 or (comparator == ">=" and bound > high)):
            return "HH"
        if low is not None and ((comparator == "<" and bound <= low)
                                or (comparator == "<=" and bound < low)):
            return "LL"
        return ""
    value = at_precision(value_milli, decimal_places)
    if value is None:
        return ""
    if low is not None and value < low:
        return "LL"
    if high is not None and value > high:
        return "HH"
    return ""


def _bound_milli(value_text: Optional[str], comparator: Optional[str]) -> Optional[int]:
    """The number behind a stored bound ('<0.01' gives 10), or None."""
    if not comparator or not value_text:
        return None
    text = str(value_text).strip()
    if text.startswith(comparator):
        text = text[len(comparator):]
    try:
        return int(Decimal(text.strip()) * 1000)
    except (InvalidOperation, ValueError):
        return None


def _plain_number(milli: int) -> str:
    """Thousandths as the shortest exact decimal: 1500 gives '1.5'."""
    text = format(Decimal(int(milli)) / Decimal(1000), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _with_unit(text: str, unit: Optional[str]) -> str:
    unit = (unit or "").strip()
    return f"{text} {unit}" if unit else text


def _unit_key(unit: Any) -> str:
    return re.sub(r"\s+", "", str(unit or "")).casefold()


def _previous_milli(previous: dict, decimal_places: int) -> Optional[int]:
    """The earlier result's number at this test's reporting precision, or None
    when it holds no number (a bound or a text result)."""
    number = previous.get("value_numeric")
    if number is None or isinstance(number, bool):
        return None
    try:
        exact = Decimal(str(number))
    except (InvalidOperation, ValueError):
        return None
    if not exact.is_finite():
        return None
    return at_precision(int((exact * 1000).to_integral_value(rounding=ROUND_HALF_UP)),
                        decimal_places)


def delta_threshold_text(kind: str, threshold_milli: int, unit: Optional[str]) -> str:
    if kind == "percent":
        return f"{_plain_number(threshold_milli)}%"
    return _with_unit(_plain_number(threshold_milli), unit)


def delta_check(*, value_milli: Optional[int], comparator: Optional[str], value_text: Any,
                unit: Optional[str], decimal_places: int, kind: str, threshold_milli: int,
                window_hours: Optional[int], previous: Optional[dict],
                hours_before: Optional[float]) -> dict:
    """One delta check (ruling LIS-R9). Pure.

    `previous` is the newest earlier live result (id, value_numeric,
    value_text, unit, observed_at, source, lab_result_id) or None;
    `hours_before` is how long before this sample's collection it was
    observed. Returns {status, note, kind, threshold_milli, threshold_text,
    window_hours, previous, change_milli, change_text, percent_text, basis}.
    Only status 'flagged' needs a reason; `basis` is what a reason is given
    against (the earlier result and this value)."""
    dp = int(decimal_places)
    threshold_text = delta_threshold_text(kind, threshold_milli, unit)
    prev_view = None
    if previous is not None:
        shown = lobs.display_value(previous)
        prev_view = {
            "observation_id": previous.get("id"),
            "value_text": None if shown is None else str(shown),
            "unit": previous.get("unit"),
            "observed_at": previous.get("observed_at") or previous.get("recorded_at"),
            "source": previous.get("source"),
            # previous_result does not return lab_result_id; the source label
            # says the same thing for a row this laboratory released.
            "from_laboratory": (previous.get("lab_result_id") is not None
                                or previous.get("source") in _LIS_SOURCES),
        }
    out = {"status": DELTA_NOT_CHECKED, "note": "", "kind": kind,
           "threshold_milli": int(threshold_milli), "threshold_text": threshold_text,
           "window_hours": window_hours, "previous": prev_view, "change_milli": None,
           "change_text": None, "percent_text": None}

    def done(status: str, note: str) -> dict:
        out["status"] = status
        out["note"] = note
        out["basis"] = {"status": status,
                        "previous_id": prev_view["observation_id"] if prev_view else None,
                        "value_text": _seen_text(value_text)}
        return out

    if comparator or value_milli is None:
        return done(DELTA_BOUND, "This value is a bound, so it is not delta-checked.")
    if previous is None:
        return done(DELTA_NO_PREVIOUS,
                    "No earlier result for this test, so there is nothing to compare.")
    if window_hours and hours_before is not None and hours_before > float(window_hours):
        return done(DELTA_OUTSIDE_WINDOW,
                    f"The last earlier result was {int(hours_before)} hours before this "
                    f"sample, outside the {int(window_hours)}-hour delta window, so it is "
                    "not compared.")
    before = _previous_milli(previous, dp)
    if before is None:
        return done(DELTA_PREVIOUS_NOT_NUMERIC,
                    f"The earlier result ({prev_view['value_text'] or 'blank'}) is not a "
                    "number, so it is not compared.")
    if _unit_key(previous.get("unit")) != _unit_key(unit):
        then = (previous.get("unit") or "").strip() or "no unit"
        now_unit = (unit or "").strip() or "no unit"
        return done(DELTA_UNIT_MISMATCH,
                    f"The earlier result is in {then} and this test reports in {now_unit}, "
                    "so they are not compared.")
    current = at_precision(value_milli, dp)
    change = current - before
    sign = "+" if change > 0 else ("-" if change < 0 else "")
    out["change_milli"] = change
    out["change_text"] = _with_unit(f"{sign}{lm.format_milli(abs(change), dp)}", unit)
    previous_text = _with_unit(lm.format_milli(before, dp), unit)
    if kind == "percent":
        if before == 0:
            flagged = change != 0
            out["percent_text"] = None
        else:
            # Exact: |change| / |before| * 100 >= threshold, all in integers.
            flagged = abs(change) * 100 * 1000 >= int(threshold_milli) * abs(before)
            percent = (Decimal(abs(change)) * 100 / Decimal(abs(before))).quantize(
                Decimal(1), rounding=ROUND_HALF_UP)
            out["percent_text"] = f"{sign}{percent}%"
    else:
        flagged = abs(change) >= int(threshold_milli)
    change_words = out["change_text"] + (f" ({out['percent_text']})"
                                         if out["percent_text"] else "")
    if flagged:
        return done(DELTA_FLAGGED,
                    f"Changed by {change_words} from the earlier {previous_text}, at or "
                    f"beyond the delta limit of {threshold_text}. Give a reason before "
                    "technical validation.")
    return done(DELTA_WITHIN,
                f"Changed by {change_words} from the earlier {previous_text}, within the "
                f"delta limit of {threshold_text}.")


# ════════════════════════════════════════════════════════════════════════════
#  Reads
# ════════════════════════════════════════════════════════════════════════════

def _tables_present(conn) -> bool:
    return lm.tables_present(conn, _RESULT_TABLES)


def _placeholders(values) -> str:
    return ", ".join("?" for _ in values)


def _test_block(row: dict) -> dict:
    return {"id": row["test_id"], "name": row.get("test_name") or row["test_id"],
            "short_name": row.get("test_short_name") or row.get("test_name") or row["test_id"],
            "result_type": row.get("result_type") or "numeric",
            "unit": row.get("unit") or "",
            "decimal_places": int(row.get("decimal_places") or 0),
            "coded_values": lm.coded_values({"coded_values_json": row.get("coded_values_json")})}


def _tat_minutes(row: dict) -> Optional[int]:
    key = {"stat": "tat_stat_min", "urgent": "tat_urgent_min"}.get(row.get("priority"),
                                                                    "tat_routine_min")
    value = row.get(key)
    return int(value) if value else None


def _result_block(row: dict, result_type: str) -> Optional[dict]:
    if not row.get("result_id"):
        return None
    out = {
        "id": row["result_id"],
        "status": row["result_status"],
        "version": row["result_version"],
        "value_text": row["value_text"],
        "comparator": row["comparator"],
        "unit": row["result_unit"],
        "abnormal_flag": row["abnormal_flag"] or "",
        "reference_range_id": row["reference_range_id"],
        "reference_range_text": row["reference_range_text"],
        "comment": row["result_comment"],
        "entered_by_name": row["entered_by_name"],
        "entered_at": row["entered_at"],
        "is_critical": int(row.get("is_critical") or 0),
        "critical_limit_text": row.get("critical_limit_text"),
    }
    out["flag_text"] = flag_text({**out, "result_type": result_type})
    out.update(_delta_view(row))
    out["returned"] = (_returned_view(row.get("returned_json"))
                       if row["result_status"] == "entered" else None)
    return out


def _returned_view(raw: Any) -> Optional[dict]:
    """Who sent a result back to the bench, when and why, from its latest
    'returned' event, or None."""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {"by_name": data.get("by_name"), "at": data.get("at"), "reason": data.get("reason")}


_WORKLIST_SELECT = """
    SELECT i.id AS item_id, i.order_id, i.specimen_id, i.section_id, i.status AS item_status,
           i.test_id, s.priority, s.received_at, s.collected_at, s.patient_id,
           t.name AS test_name, t.short_name AS test_short_name, t.result_type, t.unit,
           t.decimal_places, t.coded_values_json, t.tat_routine_min, t.tat_urgent_min,
           t.tat_stat_min,
           p.name AS patient_name, p.mrn, p.ward, p.bed,
           r.id AS result_id, r.status AS result_status, r.version AS result_version,
           r.value_text, r.comparator, r.unit AS result_unit, r.abnormal_flag,
           r.reference_range_id, r.reference_range_text, r.comment AS result_comment,
           r.entered_at, eu.name AS entered_by_name,
           r.is_critical, r.critical_limit_id, cl.display_text AS critical_limit_text,
           r.delta_flag, r.delta_json, r.delta_ack_by, r.delta_ack_at, r.delta_ack_reason,
           au.name AS delta_ack_by_name,
           (SELECT e.after_json FROM lab_result_events e
             WHERE e.result_id = r.id AND e.event_type = 'returned'
             ORDER BY e.id DESC LIMIT 1) AS returned_json
    FROM lab_order_items i
    JOIN lab_specimens s ON s.id = i.specimen_id
    JOIN patients p ON p.id = s.patient_id
    LEFT JOIN lab_tests t ON t.id = i.test_id
    LEFT JOIN lab_results r ON r.order_item_id = i.id
         AND r.status NOT IN ('superseded', 'withdrawn')
    LEFT JOIN users eu ON eu.id = r.entered_by
    LEFT JOIN users au ON au.id = r.delta_ack_by
    LEFT JOIN lab_critical_limits cl ON cl.id = r.critical_limit_id"""


def list_sections(conn) -> list[dict]:
    """Every active section with its counts: tests waiting for a result,
    results entered and not yet validated, and STAT tests waiting. Counts
    only, no patient data."""
    if not _tables_present(conn):
        return []
    rows = conn.execute(
        f"""SELECT sec.id, sec.name, sec.sort_order,
                   SUM(CASE WHEN w.status = 'received' THEN 1 ELSE 0 END) AS waiting,
                   SUM(CASE WHEN w.status = 'resulted' THEN 1 ELSE 0 END) AS entered,
                   SUM(CASE WHEN w.status = 'received' AND w.priority = 'stat'
                            THEN 1 ELSE 0 END) AS stat_waiting
            FROM lab_sections sec
            LEFT JOIN (SELECT i.section_id, i.status, s.priority
                       FROM lab_order_items i
                       JOIN lab_specimens s ON s.id = i.specimen_id
                       WHERE i.status IN ({_placeholders(WORKLIST_ITEM_STATUSES)})
                         AND s.status IN ({_placeholders(_BENCH_SPECIMEN_STATUSES)})) w
                 ON w.section_id = sec.id
            WHERE sec.active = 1
            GROUP BY sec.id
            ORDER BY sec.sort_order, sec.name COLLATE NOCASE""",
        (*WORKLIST_ITEM_STATUSES, *_BENCH_SPECIMEN_STATUSES)).fetchall()
    return [{"id": r["id"], "name": r["name"], "waiting": int(r["waiting"] or 0),
             "entered": int(r["entered"] or 0), "stat_waiting": int(r["stat_waiting"] or 0)}
            for r in rows]


def _range_preview(conn, cache: dict, test_id: str, patient_id: str,
                   collected_at: Optional[str], result_type: str) -> dict:
    """The range a value entered now would be flagged against, for the
    bench to see before typing. The server decides again at entry."""
    if result_type != "numeric":
        return {"id": None, "text": None, "configured": False}
    key = (test_id, patient_id, collected_at)
    if key not in cache:
        choice = lm.applicable_range_for_patient(conn, test_id, patient_id,
                                                 at=collected_at or None,
                                                 version_at=utc_iso_now())
        cache[key] = {"id": choice.row["id"] if choice.row else None,
                      "text": choice.display_text, "configured": choice.row is not None}
    return cache[key]


def worklist(conn, section_id: str, *, dept: Optional[str], limit: int = WORKLIST_CAP) -> dict:
    """One section's bench: every test on a received sample waiting for a
    result or with one entered and not yet validated, STAT first, then the
    oldest receipt, then the sample and the test in order. Each row carries
    the patient, the test (unit, precision, coded values), the range a value
    would be flagged against now, the turnaround target and any entered
    result. Also the tests cancelled after their sample was received, within
    CANCELLED_LOOKBACK_HOURS, with who cancelled them and when.

    `dept` is the caller's department_scope and is required: None (the
    laboratory roles) is hospital-wide; a department keeps only its own
    patients, filtered in SQL before the LIMIT. 404 for an unknown section."""
    if not _tables_present(conn):
        raise LabResultError("not_found", "Section not found", status=404)
    section = conn.execute("SELECT id, name FROM lab_sections WHERE id = ?",
                           (section_id,)).fetchone()
    if section is None:
        raise LabResultError("not_found", "Section not found", status=404)
    from permissions import scope_patient_ids_sql

    limit = max(1, min(int(limit or WORKLIST_CAP), WORKLIST_CAP))
    scope_where, scope_params = "", ()
    if dept is not None:
        scope_sql, params = scope_patient_ids_sql(dept)
        scope_where = f" AND s.patient_id IN ({scope_sql})"
        scope_params = tuple(params)
    rows = [dict(r) for r in conn.execute(
        f"""{_WORKLIST_SELECT}
            WHERE i.section_id = ?
              AND i.status IN ({_placeholders(WORKLIST_ITEM_STATUSES)})
              AND s.status IN ({_placeholders(_BENCH_SPECIMEN_STATUSES)})
              {scope_where}
            ORDER BY {_PRIORITY_RANK_SQL}, julianday(s.received_at) ASC, s.id ASC, i.id ASC
            LIMIT ?""",
        (section_id, *WORKLIST_ITEM_STATUSES, *_BENCH_SPECIMEN_STATUSES, *scope_params,
         limit)).fetchall()]
    cache: dict = {}
    items = []
    for row in rows:
        test = _test_block(row)
        items.append({
            "item_id": row["item_id"],
            "order_id": row["order_id"],
            "specimen_id": row["specimen_id"],
            "priority": row["priority"],
            "status": row["item_status"],
            "patient": {"id": row["patient_id"], "name": row["patient_name"],
                        "mrn": row["mrn"], "ward": row["ward"], "bed": row["bed"]},
            "test": test,
            "received_at": row["received_at"],
            "collected_at": row["collected_at"],
            "tat_minutes": _tat_minutes(row),
            "range": _range_preview(conn, cache, row["test_id"], row["patient_id"],
                                    row["collected_at"], test["result_type"]),
            "result": _result_block(row, test["result_type"]),
        })
    since = (datetime.now(timezone.utc) - timedelta(hours=CANCELLED_LOOKBACK_HOURS)).isoformat()
    cancelled = [dict(r) for r in conn.execute(
        f"""SELECT i.id AS item_id, i.order_id, i.specimen_id, i.test_id,
                   i.cancelled_reason AS reason, i.updated_at AS cancelled_at,
                   t.name AS test_name, u.name AS cancelled_by_name,
                   p.id AS patient_id, p.name AS patient_name, p.mrn
            FROM lab_order_items i
            JOIN lab_specimens s ON s.id = i.specimen_id
            JOIN patients p ON p.id = s.patient_id
            LEFT JOIN lab_tests t ON t.id = i.test_id
            LEFT JOIN users u ON u.id = i.cancelled_by
            WHERE i.section_id = ? AND i.status = 'cancelled'
              AND s.received_at IS NOT NULL
              AND julianday(i.updated_at) >= julianday(?)
              {scope_where}
            ORDER BY julianday(i.updated_at) DESC, i.id DESC
            LIMIT ?""",
        (section_id, since, *scope_params, CANCELLED_LIST_CAP)).fetchall()]
    return {
        "section": {"id": section["id"], "name": section["name"]},
        "items": items,
        "cancelled": [{"item_id": c["item_id"], "order_id": c["order_id"],
                       "specimen_id": c["specimen_id"],
                       "test_name": c["test_name"] or c["test_id"],
                       "patient": {"id": c["patient_id"], "name": c["patient_name"],
                                   "mrn": c["mrn"]},
                       "cancelled_by_name": c["cancelled_by_name"],
                       "cancelled_at": c["cancelled_at"], "reason": c["reason"]}
                      for c in cancelled],
    }


# ════════════════════════════════════════════════════════════════════════════
#  Writes
# ════════════════════════════════════════════════════════════════════════════

def _require_result_permission(actor: dict) -> None:
    if not actor or not actor.get("id") or not has_permission(actor.get("role", ""), LAB_RESULT):
        raise LabResultError("permission_denied",
                             "Only laboratory staff enter results.", status=403)


def _audit(conn, *, action: str, summary: str, actor: dict, result_id: Any,
           patient_id: str, metadata: dict, request_meta: Optional[dict]) -> None:
    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_access",
        action_type=action,
        outcome="success",
        db=conn,
        summary_message=summary,
        actor_user_id=(actor or {}).get("id"),
        actor_role=(actor or {}).get("role"),
        target_type="lab_result",
        target_id=str(result_id),
        patient_id=patient_id,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata=metadata,
    )


def _item_for_entry(conn, order_item_id: Any) -> dict:
    try:
        item_id = int(order_item_id)
    except (TypeError, ValueError):
        raise LabResultError("not_found", "Test not found", status=404)
    row = conn.execute(
        """SELECT i.id AS item_id, i.order_id, i.patient_id, i.test_id, i.specimen_id,
                  i.panel_test_id,
                  i.status AS item_status, i.cancelled_reason, i.updated_at AS item_updated_at,
                  cu.name AS cancelled_by_name,
                  s.status AS specimen_status, s.collected_at, s.received_at,
                  t.name AS test_name, t.result_type, t.unit, t.decimal_places,
                  t.coded_values_json, t.code AS test_code, t.code_system AS test_code_system,
                  t.delta_kind, t.delta_threshold_milli, t.delta_window_hours
           FROM lab_order_items i
           LEFT JOIN lab_specimens s ON s.id = i.specimen_id
           LEFT JOIN lab_tests t ON t.id = i.test_id
           LEFT JOIN users cu ON cu.id = i.cancelled_by
           WHERE i.id = ?""", (item_id,)).fetchone()
    if row is None:
        raise LabResultError("not_found", "Test not found", status=404)
    return dict(row)


def _refuse_item_state(item: dict) -> None:
    status = item["item_status"]
    if status == "received" and item["specimen_status"] in _BENCH_SPECIMEN_STATUSES:
        return
    if status == "cancelled":
        raise LabResultError(
            "item_cancelled",
            "This test was cancelled" + (f" by {item['cancelled_by_name']}"
                                         if item.get("cancelled_by_name") else "")
            + ". Do not enter a result.",
            cancelled_by_name=item.get("cancelled_by_name"),
            cancelled_at=item.get("item_updated_at"),
            reason=item.get("cancelled_reason"))
    if status == "rejected":
        raise LabResultError("specimen_rejected",
                             "The sample for this test was rejected. Do not enter a result.")
    if status == "resulted":
        raise LabResultError("result_exists",
                             "A result is already entered for this test. Edit it instead.")
    if status in ("tech_validated", "released"):
        raise LabResultError("already_validated",
                             "This result has been validated. A change now is an amendment.")
    raise LabResultError("not_received",
                         "This test's sample has not been received in the laboratory yet.",
                         item_status=status)


def _value_fields(conn, item: dict, value: Any) -> dict:
    """Parse the typed value for this test and decide its flag and range.
    Returns the lab_results columns that depend on the value."""
    result_type = item.get("result_type") or "numeric"
    dp = int(item.get("decimal_places") or 0)
    fields = {"value_milli": None, "comparator": None, "value_text": None,
              "unit": item.get("unit") or None, "decimal_places": dp,
              "reference_range_id": None, "reference_range_text": None, "abnormal_flag": "",
              "is_critical": 0, "critical_limit_id": None}
    if result_type == "coded":
        fields["value_text"] = parse_coded(value, lm.coded_values(item))
        return fields
    if result_type == "text":
        fields["value_text"] = parse_text(value)
        return fields
    parsed = parse_numeric(value, dp, test_name=item.get("test_name") or "This test")
    fields.update(parsed)
    choice = lm.applicable_range_for_patient(
        conn, item["test_id"], item["patient_id"],
        at=item.get("collected_at") or None, version_at=utc_iso_now())
    fields["reference_range_text"] = choice.display_text
    if choice.row is not None:
        fields["reference_range_id"] = int(choice.row["id"])
        fields["abnormal_flag"] = range_flag(
            parsed["value_milli"], low_milli=choice.row.get("low_milli"),
            high_milli=choice.row.get("high_milli"), decimal_places=dp)
    limit = _critical_choice(conn, item)
    if limit.row is not None:
        crossed = critical_flag(
            parsed["value_milli"], low_milli=limit.row.get("critical_low_milli"),
            high_milli=limit.row.get("critical_high_milli"), decimal_places=dp,
            comparator=parsed["comparator"],
            bound_milli=_bound_milli(parsed["value_text"], parsed["comparator"]))
        if crossed:
            fields.update(abnormal_flag=crossed, is_critical=1,
                          critical_limit_id=int(limit.row["id"]))
    return fields


def _critical_choice(conn, item: dict) -> lm.RangeChoice:
    """The approved critical limit for this patient, by the range's rule: the
    version approved now, the age at collection (ruling LIS-R7 for the age
    and sex). None ships (ruling LIS-R1)."""
    demo = lm.patient_demographics(conn, item["patient_id"])
    rows = lm.list_critical_limits(conn, test_id=item["test_id"])
    return lm.select_applicable(
        rows, sex=lm.normalise_sex(demo["sex"]),
        age_days=lm.age_days_at(demo["date_of_birth"], item.get("collected_at") or utc_iso_now()),
        limits=True)


def _delta_for(conn, item: dict, value: dict) -> tuple[int, Optional[dict]]:
    """(delta_flag, delta) for a value of this test (ruling LIS-R9). `value`
    holds value_milli, comparator and value_text. (0, None) when the test
    has no delta check configured or is not numeric."""
    if (item.get("result_type") or "numeric") != "numeric":
        return 0, None
    kind = item.get("delta_kind")
    threshold = item.get("delta_threshold_milli")
    if kind not in lm.DELTA_KINDS or not threshold:
        return 0, None
    window = int(item["delta_window_hours"]) if item.get("delta_window_hours") else None
    common = dict(value_milli=value.get("value_milli"), comparator=value.get("comparator"),
                  value_text=value.get("value_text"), unit=item.get("unit"),
                  decimal_places=int(item.get("decimal_places") or 0), kind=kind,
                  threshold_milli=int(threshold), window_hours=window)
    if value.get("comparator") or value.get("value_milli") is None:
        # A bound is never delta-checked; delta_check says so.
        return 0, delta_check(previous=None, hours_before=None, **common)
    code = item.get("test_code")
    collected = item.get("collected_at")
    row = None
    try:
        if code and collected:
            row = lobs.previous_result(conn, item["patient_id"], code, collected,
                                       exclude_simulated=True)
    except ValueError:
        collected = None
    if not code or not collected:
        delta = delta_check(previous=None, hours_before=None, **common)
        delta.update(status=DELTA_NOT_CHECKED,
                     note="This sample has no readable collection time, so it is not "
                          "delta-checked.")
        delta["basis"]["status"] = DELTA_NOT_CHECKED
        return 0, delta
    previous, hours = None, None
    if row is not None:
        previous = dict(row)
        instant = previous.get("observed_at") or previous.get("recorded_at")
        hours = conn.execute("SELECT (julianday(?) - julianday(?)) * 24.0",
                             (collected, instant)).fetchone()[0]
    delta = delta_check(previous=previous, hours_before=hours, **common)
    return (1 if delta["status"] == DELTA_FLAGGED else 0), delta


def _stored_delta(row: dict) -> Optional[dict]:
    raw = row.get("delta_json")
    if not raw:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _delta_reason_holds(row: dict, delta: Optional[dict]) -> bool:
    """Whether the reason on file covers this delta check: one was given, and
    against the same earlier result and the same value."""
    if not delta or delta.get("status") != DELTA_FLAGGED:
        return True
    stored = _stored_delta(row) or {}
    return bool(row.get("delta_ack_at")) and stored.get("basis") == delta.get("basis")


def _delta_view(row: dict) -> dict:
    """The delta fields every result answer carries."""
    delta = _stored_delta(row)
    acked = bool(row.get("delta_ack_at"))
    return {
        "delta": delta,
        "needs_delta_reason": bool(row.get("delta_flag")) and not acked,
        "delta_ack": ({"by_name": row.get("delta_ack_by_name"), "at": row.get("delta_ack_at"),
                       "reason": row.get("delta_ack_reason")} if acked else None),
    }


def _clean_comment(comment: Any) -> Optional[str]:
    text = str(comment if comment is not None else "").strip()
    if len(text) > 1000:
        raise LabResultError("too_long", "A comment is at most 1000 characters.", status=422)
    return text or None


def _result_row(conn, result_id: int) -> dict:
    row = conn.execute(
        """SELECT r.*, u.name AS entered_by_name, t.result_type,
                  au.name AS delta_ack_by_name, cl.display_text AS critical_limit_text
           FROM lab_results r
           LEFT JOIN users u ON u.id = r.entered_by
           LEFT JOIN lab_tests t ON t.id = r.test_id
           LEFT JOIN users au ON au.id = r.delta_ack_by
           LEFT JOIN lab_critical_limits cl ON cl.id = r.critical_limit_id
           WHERE r.id = ?""", (result_id,)).fetchone()
    out = dict(row)
    out["flag_text"] = flag_text(out)
    out.update(_delta_view(out))
    return out


_SNAPSHOT_KEYS = ("value_text", "comparator", "value_milli", "unit", "decimal_places",
                  "abnormal_flag", "reference_range_id", "reference_range_text", "comment",
                  "is_critical", "critical_limit_id")


def _snapshot(row: dict) -> dict:
    return {k: row.get(k) for k in _SNAPSHOT_KEYS}


#: What an edit must say it last saw. The comparator is part of value_text
#: ('<0.01'), so these three are everything the bench shows of a result.
EXPECTED_KEYS = ("value_text", "comment", "abnormal_flag")


def _seen_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _refuse_if_changed(conn, current: dict, expected: Any, *,
                       with_current: bool = True) -> None:
    """Refuse an edit made from a screen that is out of date (409
    'result_changed'), so a stale bench never overwrites another person's
    change in silence.

    `expected` is the value_text, comment and abnormal_flag the editor's
    screen showed. Each is compared with the stored row (blank and missing
    are the same; nothing else is forgiven). A missing or malformed
    `expected` is refused the same way: an edit that cannot say what it saw
    is treated as stale. The refusal carries the current row and who last
    saved it, so the bench can show what is now true; with `with_current`
    False (validation and authorisation, whose screens reload their audited
    queue instead) it carries only which fields differ."""
    seen = expected if isinstance(expected, dict) else {}
    differs = [k for k in EXPECTED_KEYS
               if k not in seen or _seen_text(seen.get(k)) != _seen_text(current.get(k))]
    if not differs:
        return
    last = conn.execute(
        """SELECT e.event_type, e.occurred_at, u.name AS actor_name
           FROM lab_result_events e
           LEFT JOIN users u ON u.id = e.actor
           WHERE e.result_id = ?
           ORDER BY e.id DESC LIMIT 1""", (current["id"],)).fetchone()
    by = last["actor_name"] if last is not None else None
    if not with_current:
        raise LabResultError(
            "result_changed",
            "This result has changed since your screen loaded. Nothing was saved. "
            "Check the list again before you go on.",
            result_id=int(current["id"]), changed_fields=differs)
    now_reads = _seen_text(current.get("value_text")) or "blank"
    raise LabResultError(
        "result_changed",
        "This result has changed since your screen loaded: it now reads "
        f"{now_reads}" + (f" (last saved by {by})" if by else "")
        + ". Nothing was saved. Check it and make your change again.",
        changed_fields=differs,
        changed_by_name=by,
        changed_at=last["occurred_at"] if last is not None else None,
        current=_result_row(conn, int(current["id"])))


def enter_result(order_item_id: Any, *, value: Any, comment: Any = None, actor: dict,
                 dept: Optional[str] = None,
                 request_meta: Optional[dict] = None) -> dict:
    """Enter one result by hand, in ONE immediate transaction: the
    lab_results row (status 'entered', version 1, source 'lis_manual', its
    range and critical flags and its delta check), the test to 'resulted',
    the sample to 'in_process', the 'entered' result event and the
    clinical_access audit row. Refused: a test not on a received sample,
    cancelled, rejected or already resulted; a value the test cannot take
    (precision, coded list). The partial unique index refuses a second live
    result even under a race.
    """
    from permissions import patient_in_scope

    _require_result_permission(actor)
    comment = _clean_comment(comment)
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Test not found", status=404)
        item = _item_for_entry(conn, order_item_id)
        if not patient_in_scope(conn, item["patient_id"], dept):
            raise LabResultError("not_found", "Test not found", status=404)
        _refuse_item_state(item)
        fields = _value_fields(conn, item, value)
        delta_flag, delta = _delta_for(conn, item, fields)
        now = utc_iso_now()
        try:
            cur = conn.execute(
                """INSERT INTO lab_results
                   (order_item_id, order_id, specimen_id, patient_id, test_id, version,
                    value_milli, comparator, value_text, unit, decimal_places, comment,
                    reference_range_id, reference_range_text, abnormal_flag,
                    critical_limit_id, is_critical, delta_flag, delta_json,
                    entry_method, entered_by, entered_at, status, source)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?,
                           'entered', 'lis_manual')""",
                (item["item_id"], item["order_id"], item["specimen_id"], item["patient_id"],
                 item["test_id"], fields["value_milli"], fields["comparator"],
                 fields["value_text"], fields["unit"], fields["decimal_places"], comment,
                 fields["reference_range_id"], fields["reference_range_text"],
                 fields["abnormal_flag"], fields["critical_limit_id"], fields["is_critical"],
                 delta_flag, json.dumps(delta, default=str) if delta else None, actor_id, now))
        except DBIntegrityError:
            raise LabResultError("result_exists",
                                 "A result is already entered for this test. Edit it instead.")
        result_id = int(cur.lastrowid)
        moved = conn.execute(
            "UPDATE lab_order_items SET status = 'resulted', updated_at = ? "
            "WHERE id = ? AND status = 'received'", (now, item["item_id"]))
        if moved.rowcount != 1:
            raise LabResultError("result_exists",
                                 "A result is already entered for this test. Edit it instead.")
        after = {**fields, "comment": comment,
                 "delta_status": delta["status"] if delta else None}
        conn.execute(
            "INSERT INTO lab_result_events (result_id, patient_id, event_type, actor, "
            "occurred_at, before_json, after_json) VALUES (?, ?, 'entered', ?, ?, NULL, ?)",
            (result_id, item["patient_id"], actor_id, now, json.dumps(after, default=str)))
        los.sync_specimen_progress(conn, item["specimen_id"], actor_id=actor_id,
                                   actor_role=actor.get("role"), now=now)
        los.reconcile_order_status(conn, item["order_id"], actor_id=actor_id,
                                   reason="Laboratory result entered")
        _audit(conn, action="lab_result_entered",
               summary=f"Laboratory result entered: {item.get('test_name') or item['test_id']}",
               actor=actor, result_id=result_id, patient_id=item["patient_id"],
               metadata={"order_item_id": item["item_id"], "order_id": item["order_id"],
                         "specimen_id": item["specimen_id"], "test_id": item["test_id"],
                         "after": after},
               request_meta=request_meta)
        return {"result": _result_row(conn, result_id)}


def edit_result(result_id: Any, *, value: Any, expected: Any, comment: Any = None,
                reason: Any = None, actor: dict, dept: Optional[str] = None,
                request_meta: Optional[dict] = None) -> dict:
    """Change an entered result before technical validation, in ONE immediate
    transaction: the row is updated in place (same version, the flag decided
    again against the range approved now), and the 'edited' result event and
    the clinical_access audit row keep the value before and after, with the
    reason when one is given. A validated, reported, withdrawn or superseded
    result is refused: after validation a change is an amendment (lis-13).

    `expected` is required: the value_text, comment and abnormal_flag the
    editor's screen showed. If the stored result differs (someone else
    changed it since), the edit is refused 409 'result_changed' with the
    current row, and nothing is written; this check runs before the new value
    is read, so a stale screen is never told its value was saved, even when
    it matches. An edit that changes nothing writes nothing."""
    from permissions import patient_in_scope

    _require_result_permission(actor)
    comment = _clean_comment(comment)
    reason = str(reason if reason is not None else "").strip()[:500] or None
    actor_id = actor["id"]
    try:
        rid = int(result_id)
    except (TypeError, ValueError):
        raise LabResultError("not_found", "Result not found", status=404)
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Result not found", status=404)
        current = conn.execute("SELECT * FROM lab_results WHERE id = ?", (rid,)).fetchone()
        if current is None or not patient_in_scope(conn, current["patient_id"], dept):
            raise LabResultError("not_found", "Result not found", status=404)
        current = dict(current)
        if current["status"] not in _EDITABLE_RESULT_STATUSES:
            if current["status"] in ("tech_validated", "released"):
                raise LabResultError(
                    "already_validated",
                    "This result has been validated. A change now is an amendment.")
            raise LabResultError("not_live", "This result is no longer current.",
                                 result_status=current["status"])
        item = _item_for_entry(conn, current["order_item_id"])
        if item["item_status"] != "resulted":
            if item["item_status"] in ("cancelled", "rejected"):
                _refuse_item_state(item)
            raise LabResultError("not_live", "This result is no longer current.",
                                 item_status=item["item_status"])
        _refuse_if_changed(conn, current, expected)
        fields = _value_fields(conn, item, value)
        before = _snapshot(current)
        after = {**fields, "comment": comment}
        if _snapshot(after) == before:
            return {"result": _result_row(conn, rid), "unchanged": True}
        # The delta check is decided again for the new value. A reason given
        # earlier stands only while it covers the same earlier result and
        # the same value (ruling LIS-R9).
        delta_flag, delta = _delta_for(conn, item, fields)
        keep_reason = (bool(current.get("delta_ack_at")) and delta is not None
                       and (_stored_delta(current) or {}).get("basis") == delta.get("basis"))
        now = utc_iso_now()
        cur = conn.execute(
            """UPDATE lab_results
               SET value_milli = ?, comparator = ?, value_text = ?, unit = ?,
                   decimal_places = ?, comment = ?, reference_range_id = ?,
                   reference_range_text = ?, abnormal_flag = ?, critical_limit_id = ?,
                   is_critical = ?, delta_flag = ?, delta_json = ?,
                   delta_ack_by = CASE WHEN ? THEN delta_ack_by END,
                   delta_ack_at = CASE WHEN ? THEN delta_ack_at END,
                   delta_ack_reason = CASE WHEN ? THEN delta_ack_reason END
               WHERE id = ? AND status = 'entered'""",
            (fields["value_milli"], fields["comparator"], fields["value_text"], fields["unit"],
             fields["decimal_places"], comment, fields["reference_range_id"],
             fields["reference_range_text"], fields["abnormal_flag"],
             fields["critical_limit_id"], fields["is_critical"], delta_flag,
             json.dumps(delta, default=str) if delta else None,
             1 if keep_reason else 0, 1 if keep_reason else 0, 1 if keep_reason else 0, rid))
        if cur.rowcount != 1:
            raise LabResultError("not_live", "This result is no longer current.")
        conn.execute(
            "INSERT INTO lab_result_events (result_id, patient_id, event_type, actor, "
            "occurred_at, before_json, after_json) VALUES (?, ?, 'edited', ?, ?, ?, ?)",
            (rid, current["patient_id"], actor_id, now, json.dumps(before, default=str),
             json.dumps({**after, "reason": reason,
                         "delta_status": delta["status"] if delta else None,
                         "delta_reason_kept": keep_reason}, default=str)))
        _audit(conn, action="lab_result_edited",
               summary=(f"Laboratory result changed before validation: "
                        f"{item.get('test_name') or item['test_id']}"),
               actor=actor, result_id=rid, patient_id=current["patient_id"],
               metadata={"order_item_id": current["order_item_id"],
                         "test_id": current["test_id"], "before": before,
                         "after": _snapshot(after), "reason": reason},
               request_meta=request_meta)
        return {"result": _result_row(conn, rid), "unchanged": False}


# ════════════════════════════════════════════════════════════════════════════
#  Delta reasons, technical validation, authorisation and release
#  (lis-11, lis-12)
# ════════════════════════════════════════════════════════════════════════════

def _require_authorise_permission(actor: dict) -> None:
    if not actor or not actor.get("id") or not has_permission(actor.get("role", ""),
                                                              LAB_AUTHORISE):
        raise LabResultError("permission_denied",
                             "Only an authorised signatory authorises and releases results.",
                             status=403)


def distinct_authoriser_required() -> bool:
    """Ruling LIS-R5, held: two different people unless the site has turned
    config.LAB_REQUIRE_DISTINCT_AUTHORISER off. Anything but an explicit
    False reads as on, the strictest option."""
    return getattr(config, "LAB_REQUIRE_DISTINCT_AUTHORISER", True) is not False


def _clean_reason(reason: Any, *, words: str) -> str:
    text = re.sub(r"\s+", " ", str(reason if reason is not None else "")).strip()
    if not text:
        raise LabResultError("reason_required", words, status=422)
    if len(text) > REASON_MAX:
        raise LabResultError("too_long", f"A reason is at most {REASON_MAX} characters.",
                             status=422)
    return text


def _result_id(value: Any) -> int:
    try:
        rid = int(value)
    except (TypeError, ValueError):
        raise LabResultError("not_found", "Result not found", status=404)
    if rid <= 0:
        raise LabResultError("not_found", "Result not found", status=404)
    return rid


def _batch(results: Any) -> list[tuple[int, Any]]:
    """[(result_id, expected)] from a request's list, each result once.
    Refused when empty, too long or naming a result twice."""
    if not isinstance(results, list) or not results:
        raise LabResultError("nothing_chosen", "Choose at least one result.", status=422)
    if len(results) > VALIDATION_BATCH_MAX:
        raise LabResultError("too_many",
                             f"At most {VALIDATION_BATCH_MAX} results at a time.", status=422)
    out: list[tuple[int, Any]] = []
    seen: set[int] = set()
    for entry in results:
        entry = entry if isinstance(entry, dict) else {}
        rid = _result_id(entry.get("result_id"))
        if rid in seen:
            raise LabResultError("duplicate_result", "A result is named twice.", status=422)
        seen.add(rid)
        out.append((rid, entry.get("expected")))
    return out


def _live_result(conn, rid: int, dept: Optional[str]) -> dict:
    from permissions import patient_in_scope

    row = conn.execute("SELECT * FROM lab_results WHERE id = ?", (rid,)).fetchone()
    if row is None or not patient_in_scope(conn, row["patient_id"], dept):
        raise LabResultError("not_found", "Result not found", status=404)
    return dict(row)


def _refuse_result_status(current: dict, wanted: str) -> None:
    status = current["status"]
    if status == wanted:
        return
    if status == "entered":
        raise LabResultError("not_validated",
                             "This result has not been technically validated yet.",
                             result_id=current["id"])
    if status == "tech_validated":
        raise LabResultError("already_validated",
                             "This result has already been technically validated.",
                             result_id=current["id"])
    if status == "released":
        raise LabResultError("already_released", "This result has already been reported.",
                             result_id=current["id"])
    raise LabResultError("not_live", "This result is no longer current.",
                         result_id=current["id"], result_status=status)


def _item_in_step(conn, current: dict, item_status: str) -> dict:
    item = _item_for_entry(conn, current["order_item_id"])
    if item["item_status"] != item_status:
        if item["item_status"] in ("cancelled", "rejected"):
            _refuse_item_state(item)
        raise LabResultError("not_live", "This result is no longer current.",
                             result_id=current["id"], item_status=item["item_status"])
    return item


def _event(conn, rid: int, patient_id: str, event_type: str, actor_id: str, now: str,
           *, before: Optional[dict] = None, after: Optional[dict] = None) -> None:
    conn.execute(
        "INSERT INTO lab_result_events (result_id, patient_id, event_type, actor, "
        "occurred_at, before_json, after_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (rid, patient_id, event_type, actor_id, now,
         json.dumps(before, default=str) if before is not None else None,
         json.dumps(after, default=str) if after is not None else None))


def _test_name(item: dict) -> str:
    return item.get("test_name") or item["test_id"]


def acknowledge_delta(result_id: Any, *, reason: Any, expected: Any, actor: dict,
                      dept: Optional[str] = None,
                      request_meta: Optional[dict] = None) -> dict:
    """Give the technologist's reason for a delta flag (ruling LIS-R9 (a)),
    in ONE immediate transaction with its clinical_access audit row
    ('lab_delta_acknowledged') and a 'delta_acknowledged' result event.

    The check is decided again first, against the earlier result as it is
    now. If it is still flagged, the reason is kept against that check
    (who, when, why, and what it was compared with). If it no longer flags
    (the earlier result changed since the value was entered), the new check
    is stored, nothing is acknowledged, and the answer says so
    ('acknowledged' False), audited as 'lab_delta_rechecked'.

    Only while the result is entered and not yet technically validated, and
    only from a screen that showed the result as it is (`expected`)."""
    _require_result_permission(actor)
    text = _clean_reason(reason, words="Say why this change from the earlier result is "
                                       "expected, or what was checked.")
    rid = _result_id(result_id)
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Result not found", status=404)
        current = _live_result(conn, rid, dept)
        if current["status"] != "entered":
            _refuse_result_status(current, "entered")
        item = _item_in_step(conn, current, "resulted")
        _refuse_if_changed(conn, current, expected, with_current=False)
        flag, delta = _delta_for(conn, item, current)
        if flag == 1 and current.get("delta_ack_at") and _delta_reason_holds(current, delta):
            raise LabResultError("already_acknowledged",
                                 "A reason has already been given for this delta flag.",
                                 result_id=rid)
        now = utc_iso_now()
        if delta is None or flag != 1:
            conn.execute(
                "UPDATE lab_results SET delta_flag = ?, delta_json = ? "
                "WHERE id = ? AND status = 'entered'",
                (flag, json.dumps(delta, default=str) if delta else None, rid))
            _event(conn, rid, current["patient_id"], "delta_rechecked", actor_id, now,
                   before={"delta_flag": current.get("delta_flag")},
                   after={"delta_flag": flag, "delta_status": delta["status"] if delta else None})
            _audit(conn, action="lab_delta_rechecked",
                   summary=f"Delta check decided again, no longer flagged: {_test_name(item)}",
                   actor=actor, result_id=rid, patient_id=current["patient_id"],
                   metadata={"test_id": current["test_id"],
                             "delta_status": delta["status"] if delta else None},
                   request_meta=request_meta)
            return {"result": _result_row(conn, rid), "acknowledged": False,
                    "message": "This value no longer needs a delta reason: "
                               + (delta["note"] if delta else "no delta check applies.")}
        conn.execute(
            """UPDATE lab_results
               SET delta_flag = 1, delta_json = ?, delta_ack_by = ?, delta_ack_at = ?,
                   delta_ack_reason = ?
               WHERE id = ? AND status = 'entered'""",
            (json.dumps(delta, default=str), actor_id, now, text, rid))
        _event(conn, rid, current["patient_id"], "delta_acknowledged", actor_id, now,
               after={"reason": text, "basis": delta.get("basis"),
                      "previous": delta.get("previous"), "change_text": delta.get("change_text"),
                      "threshold_text": delta.get("threshold_text")})
        _audit(conn, action="lab_delta_acknowledged",
               summary=f"Delta flag explained before validation: {_test_name(item)}",
               actor=actor, result_id=rid, patient_id=current["patient_id"],
               metadata={"test_id": current["test_id"], "reason": text,
                         "previous_observation_id": (delta.get("previous") or {}).get(
                             "observation_id"),
                         "change_text": delta.get("change_text"),
                         "threshold_text": delta.get("threshold_text")},
               request_meta=request_meta)
        return {"result": _result_row(conn, rid), "acknowledged": True}


def technically_validate(results: Any, *, actor: dict, dept: Optional[str] = None,
                         request_meta: Optional[dict] = None) -> dict:
    """Technically validate entered results (lab.result), in ONE immediate
    transaction: all of them or none. Each result goes to 'tech_validated'
    with who and when, its test to 'tech_validated', a 'tech_validated'
    result event and a clinical_access audit row ('lab_result_tech_validated').

    `results` is [{result_id, expected}], where `expected` is what the
    screen showed of each (value_text, comment, abnormal_flag); a result
    changed since is refused 409 'result_changed' and nothing is validated.
    The delta check is decided again here: a flag with no reason on file for
    the same earlier result and value is refused 409 'delta_reason_required'
    (ruling LIS-R9 (a)). No autoverification (LIS-R8).

    A result flagged critical against an approved limit raises its critical
    in the same transaction (preliminary, ruling LIS-R4 (a)), and after the
    commit it is posted to the patient's case room; the answer's 'criticals'
    says, for each, whether the ward was told there, why not, and what it
    told them ('raised', or 'changed' / 'corrected' for a result sent back
    to the bench after its critical was told and validated again with a
    different value)."""
    _require_result_permission(actor)
    entries = _batch(results)
    actor_id = actor["id"]
    raised: list[str] = []
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Result not found", status=404)
        now = utc_iso_now()
        validated: list[int] = []
        for rid, expected in entries:
            current = _live_result(conn, rid, dept)
            _refuse_result_status(current, "entered")
            item = _item_in_step(conn, current, "resulted")
            _refuse_if_changed(conn, current, expected, with_current=False)
            flag, delta = _delta_for(conn, item, current)
            if flag and not _delta_reason_holds(current, delta):
                raise LabResultError(
                    "delta_reason_required",
                    f"{_test_name(item)} for this patient changed beyond its delta limit. "
                    "Give a reason for the delta flag before technical validation. Nothing "
                    "was validated.", result_id=rid)
            moved = conn.execute(
                """UPDATE lab_results
                   SET status = 'tech_validated', tech_validated_by = ?, tech_validated_at = ?,
                       delta_flag = ?, delta_json = ?
                   WHERE id = ? AND status = 'entered'""",
                (actor_id, now, flag, json.dumps(delta, default=str) if delta else None, rid))
            item_moved = conn.execute(
                "UPDATE lab_order_items SET status = 'tech_validated', updated_at = ? "
                "WHERE id = ? AND status = 'resulted'", (now, item["item_id"]))
            if moved.rowcount != 1 or item_moved.rowcount != 1:
                raise LabResultError("not_live", "This result is no longer current.",
                                     result_id=rid)
            _event(conn, rid, current["patient_id"], "tech_validated", actor_id, now,
                   after={"value_text": current["value_text"],
                          "abnormal_flag": current["abnormal_flag"],
                          "delta_status": delta["status"] if delta else None,
                          "delta_reason": current.get("delta_ack_reason") if flag else None})
            _audit(conn, action="lab_result_tech_validated",
                   summary=f"Laboratory result technically validated: {_test_name(item)}",
                   actor=actor, result_id=rid, patient_id=current["patient_id"],
                   metadata={"order_item_id": item["item_id"], "order_id": item["order_id"],
                             "specimen_id": item["specimen_id"], "test_id": item["test_id"],
                             "value_text": current["value_text"],
                             "abnormal_flag": current["abnormal_flag"],
                             "is_critical": int(current.get("is_critical") or 0),
                             "delta_flag": flag,
                             "delta_reason": current.get("delta_ack_reason") if flag else None},
                   request_meta=request_meta)
            validated.append(rid)
            # lis-15, ruling LIS-R4 (a): a critical value is communicated at
            # technical validation, marked preliminary, inside this
            # transaction (so an audit failure raises nothing either). Asked
            # for every result, critical or not: a result sent back to the
            # bench after its critical was told, and changed, must tell the
            # ward the value it was given has changed.
            nid = lcs.raise_for_result_in_txn(
                conn, current, actor=actor, preliminary=True, now=now,
                trigger="tech_validated", request_meta=request_meta)
            if nid:
                raised.append(nid)
        out = {"validated": len(validated),
               "results": [_result_row(conn, rid) for rid in validated]}
    # After the commit: post each critical to its case room. Never raises; a
    # failed post leaves the critical open and due at once.
    out["criticals"] = lcs.deliver_all(raised)
    return out


def _people_on(conn, current: dict) -> set[str]:
    """Everyone who entered, changed, explained the delta of or technically
    validated this result, now or before it was sent back to the bench."""
    people = {current.get("entered_by"), current.get("tech_validated_by"),
              current.get("delta_ack_by")}
    for row in conn.execute(
            f"SELECT DISTINCT actor FROM lab_result_events WHERE result_id = ? "
            f"AND event_type IN ({_placeholders(_PART_EVENTS)})",
            (current["id"], *_PART_EVENTS)).fetchall():
        people.add(row[0])
    return {p for p in people if p}


def _observation_number(current: dict) -> Optional[float]:
    """The number a released result plots and compares as, or None for a
    bound, a coded or a text result: a bound is never a value."""
    if current.get("comparator") or current.get("value_milli") is None:
        return None
    return float(Decimal(int(current["value_milli"])) / Decimal(1000))


def _write_observation(conn, current: dict, item: dict, specimen: dict, *, status: str,
                       now: str) -> int:
    """The chart's copy of one laboratory result: one observations row on the
    caller's transaction, observed at the sample's collection instant in
    canonical UTC, recorded now, linked to the order, the accession and the
    lab_results row. `status` is 'final' for a release and 'corrected' for
    an amendment. The one observations writer for the laboratory's own
    results. Returns the observation id."""
    rid = int(current["id"])
    test = conn.execute("SELECT code, code_system, name FROM lab_tests WHERE id = ?",
                        (current["test_id"],)).fetchone()
    code_system, _, code = str(current["test_id"]).partition(":")
    test = dict(test) if test is not None else {"code": code or current["test_id"],
                                                 "code_system": code_system or "LOCAL",
                                                 "name": current["test_id"]}
    panel_name = None
    if item.get("panel_test_id"):
        panel = conn.execute("SELECT name FROM lab_tests WHERE id = ?",
                             (item["panel_test_id"],)).fetchone()
        panel_name = panel[0] if panel is not None else None
    observed_at = to_utc_iso(specimen["collected_at"])
    cur = conn.execute(
        """INSERT INTO observations
           (patient_id, encounter_id, source, source_event_id, observation_code,
            observation_code_system, observation_name, value_text, value_numeric, unit,
            reference_range, abnormal_flag, status, observed_at, recorded_at, panel_name,
            obr_group_id, created_at, order_id, specimen_id, lab_result_id)
           VALUES (?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (current["patient_id"], current.get("source") or "lis_manual", test["code"],
         test["code_system"], test["name"], current.get("value_text"),
         _observation_number(current), current.get("unit"),
         current.get("reference_range_text"), current.get("abnormal_flag") or None,
         status, observed_at, now, panel_name, specimen["id"], now,
         current["order_id"], specimen["id"], rid))
    return int(cur.lastrowid)


def _release_in_txn(conn, current: dict, item: dict, specimen: dict, *, actor: dict,
                    now: str, one_person: bool, request_meta: Optional[dict]) -> int:
    """Release one technically validated result on the caller's transaction:
    the observations row, the result and its test to released, the result
    event and the audit row. Returns the observation id."""
    rid = int(current["id"])
    actor_id = actor["id"]
    observation_id = _write_observation(conn, current, item, specimen,
                                        status=RELEASED_OBSERVATION_STATUS, now=now)
    moved = conn.execute(
        """UPDATE lab_results SET status = 'released', released_by = ?, released_at = ?,
                                  observation_id = ?
           WHERE id = ? AND status = 'tech_validated'""",
        (actor_id, now, observation_id, rid))
    item_moved = conn.execute(
        "UPDATE lab_order_items SET status = 'released', updated_at = ? "
        "WHERE id = ? AND status = 'tech_validated'", (now, item["item_id"]))
    if moved.rowcount != 1 or item_moved.rowcount != 1:
        raise LabResultError("not_live", "This result is no longer current.", result_id=rid)
    _event(conn, rid, current["patient_id"], "released", actor_id, now,
           after={"observation_id": observation_id, "value_text": current.get("value_text"),
                  "abnormal_flag": current.get("abnormal_flag"), "one_person": one_person})
    _audit(conn, action="lab_result_released",
           summary=f"Laboratory result authorised and released: {_test_name(item)}",
           actor=actor, result_id=rid, patient_id=current["patient_id"],
           metadata={"order_item_id": item["item_id"], "order_id": item["order_id"],
                     "specimen_id": specimen["id"], "test_id": current["test_id"],
                     "observation_id": observation_id,
                     "value_text": current.get("value_text"),
                     "abnormal_flag": current.get("abnormal_flag"),
                     "is_critical": int(current.get("is_critical") or 0),
                     "entered_by": current.get("entered_by"),
                     "tech_validated_by": current.get("tech_validated_by"),
                     "one_person": one_person,
                     "distinct_authoriser_required": not one_person
                     and distinct_authoriser_required()},
           request_meta=request_meta)
    return observation_id


def authorise_and_release(specimen_id: Any, results: Any, *, actor: dict,
                          dept: Optional[str] = None,
                          request_meta: Optional[dict] = None) -> dict:
    """Authorise technically validated results on ONE sample and release
    them into the chart (lab.authorise), in ONE immediate transaction: all
    of the named results or none.

    For each result: the observations row (one per analyte; value_numeric
    NULL for a bound, a coded or a text result, value_text at the reporting
    precision or the bound as written, observed_at the sample's collection
    instant in canonical UTC, recorded_at now, the order, the accession and
    the lab_results id, source 'lis_manual'), the result and its test to
    'released', a 'released' result event and a clinical_access audit row
    ('lab_result_released'). Then the sample's progress and each order by
    the completion rule (LIS-R16 (a)); an order moved outside the
    laboratory still receives the value and gets an 'order_state_conflict'
    exception (reconcile_order_status). An audit row that cannot be written
    commits nothing.

    While two different people are required (LIS-R5, the default) a
    signatory who entered, changed, explained the delta of or technically
    validated a result is refused 403 'second_person_required' for it; with
    the setting off the release records that one person did every step."""
    from permissions import patient_in_scope

    _require_authorise_permission(actor)
    try:
        accession = los.checked_accession(specimen_id)
    except los.LabOrderError as err:
        raise LabResultError(err.code, err.message, status=err.status)
    entries = _batch(results)
    distinct = distinct_authoriser_required()
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Specimen not found", status=404)
        specimen = conn.execute(
            "SELECT id, patient_id, status, collected_at FROM lab_specimens WHERE id = ?",
            (accession,)).fetchone()
        if specimen is None or not patient_in_scope(conn, specimen["patient_id"], dept):
            raise LabResultError("not_found", "Specimen not found", status=404)
        specimen = dict(specimen)
        if specimen["status"] not in ("received", "in_process"):
            raise LabResultError("specimen_not_on_bench",
                                 "This sample is not on the bench, so nothing on it can be "
                                 "released.", specimen_status=specimen["status"])
        if not specimen.get("collected_at"):
            raise LabResultError("no_collection_time",
                                 "This sample has no collection time, so its results cannot "
                                 "be placed on the chart. Correct the collection first.")
        try:
            to_utc_iso(specimen["collected_at"])
        except ValueError:
            raise LabResultError("no_collection_time",
                                 "This sample's collection time cannot be read, so its "
                                 "results cannot be placed on the chart.")
        now = utc_iso_now()
        released: list[int] = []
        observations: list[int] = []
        orders: list[str] = []
        raised: list[str] = []
        one_person_count = 0
        for rid, expected in entries:
            current = _live_result(conn, rid, dept)
            if current.get("specimen_id") != accession:
                raise LabResultError("not_on_this_sample",
                                     "A result named is not on this sample. Nothing was "
                                     "released.", result_id=rid)
            _refuse_result_status(current, "tech_validated")
            item = _item_in_step(conn, current, "tech_validated")
            _refuse_if_changed(conn, current, expected, with_current=False)
            took_part = actor_id in _people_on(conn, current)
            if took_part and distinct:
                raise LabResultError(
                    "second_person_required",
                    f"You entered, changed or validated {_test_name(item)} on this sample. "
                    "This site requires a second person to authorise it. Nothing was "
                    "released.", status=403, result_id=rid)
            one_person = took_part
            one_person_count += 1 if one_person else 0
            observations.append(_release_in_txn(
                conn, current, item, specimen, actor=actor, now=now, one_person=one_person,
                request_meta=request_meta))
            released.append(rid)
            if item["order_id"] not in orders:
                orders.append(item["order_id"])
            # lis-15: a critical normally went out at technical validation. One
            # that did not (validated before the laboratory raised criticals)
            # goes out now, authorised, and so does a change no validation
            # told. Never a second one for the value already told.
            nid = lcs.raise_for_result_in_txn(
                conn, current, actor=actor, preliminary=False, now=now,
                trigger="released", request_meta=request_meta)
            if nid:
                raised.append(nid)
        progress = los.sync_specimen_progress(conn, accession, actor_id=actor_id,
                                              actor_role=actor.get("role"), now=now,
                                              detail="Results reported.")
        order_outcomes = []
        for order_id in orders:
            outcome = los.reconcile_order_status(conn, order_id, actor_id=actor_id,
                                                 reason="Laboratory result reported")
            order_outcomes.append({"id": order_id, "status": outcome["status"],
                                   "conflict": outcome["conflict"]})
        status_now = conn.execute("SELECT status FROM lab_specimens WHERE id = ?",
                                  (accession,)).fetchone()[0]
        out = {"specimen": {"id": accession, "status": status_now, "moved_to": progress},
               "released": len(released),
               "results": [_result_row(conn, rid) for rid in released],
               "observation_ids": observations,
               "orders": order_outcomes,
               "one_person": one_person_count}
    out["criticals"] = lcs.deliver_all(raised)
    # Released results close open work; a draining module may now be finished.
    lab_module.refresh_drain_state()
    return out


def return_to_bench(result_id: Any, *, reason: Any, expected: Any, actor: dict,
                    dept: Optional[str] = None,
                    request_meta: Optional[dict] = None) -> dict:
    """A signatory sends a technically validated result back to the bench
    with a reason (lab.authorise), in ONE immediate transaction: the result
    to 'entered' (its validation cleared, so it must be validated again), its
    test to 'resulted' (back on the section's worklist), a 'returned' result
    event naming who and why, and a clinical_access audit row
    ('lab_result_returned'). Nothing is released."""
    _require_authorise_permission(actor)
    text = _clean_reason(reason, words="Say why the result goes back to the bench.")
    rid = _result_id(result_id)
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Result not found", status=404)
        current = _live_result(conn, rid, dept)
        _refuse_result_status(current, "tech_validated")
        item = _item_in_step(conn, current, "tech_validated")
        _refuse_if_changed(conn, current, expected, with_current=False)
        now = utc_iso_now()
        moved = conn.execute(
            """UPDATE lab_results SET status = 'entered', tech_validated_by = NULL,
                                      tech_validated_at = NULL
               WHERE id = ? AND status = 'tech_validated'""", (rid,))
        item_moved = conn.execute(
            "UPDATE lab_order_items SET status = 'resulted', updated_at = ? "
            "WHERE id = ? AND status = 'tech_validated'", (now, item["item_id"]))
        if moved.rowcount != 1 or item_moved.rowcount != 1:
            raise LabResultError("not_live", "This result is no longer current.", result_id=rid)
        by_name = conn.execute("SELECT name FROM users WHERE id = ?", (actor_id,)).fetchone()
        _event(conn, rid, current["patient_id"], "returned", actor_id, now,
               before={"status": "tech_validated",
                       "tech_validated_by": current.get("tech_validated_by"),
                       "tech_validated_at": current.get("tech_validated_at")},
               after={"status": "entered", "reason": text, "at": now,
                      "by_name": by_name[0] if by_name else None})
        _audit(conn, action="lab_result_returned",
               summary=f"Laboratory result sent back to the bench: {_test_name(item)}",
               actor=actor, result_id=rid, patient_id=current["patient_id"],
               metadata={"test_id": current["test_id"], "reason": text,
                         "tech_validated_by": current.get("tech_validated_by")},
               request_meta=request_meta)
        return {"result": _result_row(conn, rid)}


# ── The Validate screen's two queues ────────────────────────────────────────

_QUEUE_SELECT = """
    SELECT r.id AS result_id, r.order_item_id AS item_id, r.order_id, r.specimen_id,
           r.patient_id, r.test_id, r.status, r.value_text, r.value_milli, r.comparator, r.unit,
           r.abnormal_flag, r.reference_range_id, r.reference_range_text, r.comment,
           r.is_critical, r.critical_limit_id, r.delta_flag, r.delta_json, r.delta_ack_by,
           r.delta_ack_at, r.delta_ack_reason, r.entered_by, r.entered_at,
           r.tech_validated_by, r.tech_validated_at,
           s.priority, s.received_at, s.collected_at,
           t.name AS test_name, t.short_name AS test_short_name, t.result_type,
           t.code AS test_code, t.unit AS test_unit, t.decimal_places AS test_decimal_places,
           t.delta_kind, t.delta_threshold_milli, t.delta_window_hours,
           p.name AS patient_name, p.mrn, p.ward, p.bed,
           eu.name AS entered_by_name, vu.name AS tech_validated_by_name,
           au.name AS delta_ack_by_name, cl.display_text AS critical_limit_text,
           (SELECT e.after_json FROM lab_result_events e
             WHERE e.result_id = r.id AND e.event_type = 'returned'
             ORDER BY e.id DESC LIMIT 1) AS returned_json
    FROM lab_results r
    JOIN lab_order_items i ON i.id = r.order_item_id
    JOIN lab_specimens s ON s.id = r.specimen_id
    JOIN patients p ON p.id = r.patient_id
    LEFT JOIN lab_tests t ON t.id = r.test_id
    LEFT JOIN users eu ON eu.id = r.entered_by
    LEFT JOIN users vu ON vu.id = r.tech_validated_by
    LEFT JOIN users au ON au.id = r.delta_ack_by
    LEFT JOIN lab_critical_limits cl ON cl.id = r.critical_limit_id"""


def _queue_rows(conn, *, result_status: str, item_status: str, scope_where: str,
                scope_params: tuple, limit: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"""{_QUEUE_SELECT}
            WHERE r.status = ? AND i.status = ?
              AND s.status IN ({_placeholders(_BENCH_SPECIMEN_STATUSES)})
              {scope_where}
            ORDER BY {_PRIORITY_RANK_SQL}, julianday(s.received_at) ASC, s.id ASC, i.id ASC
            LIMIT ?""",
        (result_status, item_status, *_BENCH_SPECIMEN_STATUSES, *scope_params,
         limit)).fetchall()]


def _queue_entry(row: dict) -> dict:
    result_type = row.get("result_type") or "numeric"
    out = {
        "result_id": row["result_id"],
        "item_id": row["item_id"],
        "order_id": row["order_id"],
        "status": row["status"],
        "test": {"id": row["test_id"], "name": row.get("test_name") or row["test_id"],
                 "short_name": row.get("test_short_name") or row.get("test_name")
                 or row["test_id"], "result_type": result_type},
        "value_text": row["value_text"],
        "comparator": row["comparator"],
        "unit": row["unit"],
        "abnormal_flag": row["abnormal_flag"] or "",
        "reference_range_text": row["reference_range_text"],
        "comment": row["comment"],
        "is_critical": int(row.get("is_critical") or 0),
        "critical_limit_text": row.get("critical_limit_text"),
        "entered_by_name": row["entered_by_name"],
        "entered_at": row["entered_at"],
        "tech_validated_by_name": row.get("tech_validated_by_name"),
        "tech_validated_at": row.get("tech_validated_at"),
        "returned": (_returned_view(row.get("returned_json"))
                     if row["status"] == "entered" else None),
    }
    out["flag_text"] = flag_text({**out, "result_type": result_type,
                                  "reference_range_id": row.get("reference_range_id")})
    out.update(_delta_view(row))
    return out


def _technical_entry(conn, row: dict) -> dict:
    """A row waiting for technical validation, with its delta check decided
    as technical validation will decide it: against the earlier result as it
    is now, not as it was at entry. A value stored by a lane that did not
    check it, or whose earlier result changed since, shows the check that
    validation will apply, so the screen never offers a step the server
    refuses without saying why."""
    entry = _queue_entry(row)
    item = {"result_type": row.get("result_type"), "delta_kind": row.get("delta_kind"),
            "delta_threshold_milli": row.get("delta_threshold_milli"),
            "delta_window_hours": row.get("delta_window_hours"),
            "unit": row.get("test_unit"), "decimal_places": row.get("test_decimal_places"),
            "test_code": row.get("test_code"), "collected_at": row.get("collected_at"),
            "patient_id": row["patient_id"]}
    flag, delta = _delta_for(conn, item, row)
    entry["delta"] = delta
    entry["needs_delta_reason"] = bool(flag) and not _delta_reason_holds(row, delta)
    return entry


def _grouped(rows: list[dict], entries: list[dict]) -> list[dict]:
    groups: list[dict] = []
    for row, entry in zip(rows, entries):
        last = groups[-1] if groups else None
        if last is None or last["specimen_id"] != row["specimen_id"]:
            last = {"specimen_id": row["specimen_id"], "priority": row["priority"],
                    "patient": {"id": row["patient_id"], "name": row["patient_name"],
                                "mrn": row["mrn"], "ward": row["ward"], "bed": row["bed"]},
                    "collected_at": row["collected_at"], "received_at": row["received_at"],
                    "results": []}
            groups.append(last)
        last["results"].append(entry)
    return groups


def _part_takers(conn, result_ids: list[int]) -> dict[int, set[str]]:
    """For each result, everyone who took part in it (as _people_on), in one
    read per chunk."""
    out: dict[int, set[str]] = {rid: set() for rid in result_ids}
    for start in range(0, len(result_ids), 400):
        chunk = result_ids[start:start + 400]
        for row in conn.execute(
                f"SELECT result_id, actor FROM lab_result_events "
                f"WHERE result_id IN ({_placeholders(chunk)}) "
                f"AND event_type IN ({_placeholders(_PART_EVENTS)})",
                (*chunk, *_PART_EVENTS)).fetchall():
            if row[1]:
                out[int(row[0])].add(row[1])
    return out


def validation_queues(conn, *, actor: dict, dept: Optional[str],
                      limit: int = VALIDATION_QUEUE_CAP) -> dict:
    """The Validate screen: results entered and waiting for technical
    validation, and results technically validated and waiting for a
    signatory, each grouped by sample, STAT first then the oldest receipt.

    Each authorisation row says whether this user may authorise it
    ('you_took_part', and 'may_authorise' while two different people are
    required, LIS-R5). `dept` is the caller's department_scope: None is
    hospital-wide (the laboratory roles); a department keeps its own
    patients, filtered in SQL before the LIMIT. A read: the route audits the
    patients returned."""
    from permissions import scope_patient_ids_sql

    holds_authorise = has_permission((actor or {}).get("role", ""), LAB_AUTHORISE)
    distinct = distinct_authoriser_required()
    empty = {"technical": [], "authorisation": [], "can_authorise": holds_authorise,
             "require_distinct_authoriser": distinct}
    if not _tables_present(conn):
        return empty
    limit = max(1, min(int(limit or VALIDATION_QUEUE_CAP), VALIDATION_QUEUE_CAP))
    scope_where, scope_params = "", ()
    if dept is not None:
        scope_sql, params = scope_patient_ids_sql(dept)
        scope_where = f" AND r.patient_id IN ({scope_sql})"
        scope_params = tuple(params)
    technical = _queue_rows(conn, result_status="entered", item_status="resulted",
                            scope_where=scope_where, scope_params=scope_params, limit=limit)
    waiting = _queue_rows(conn, result_status="tech_validated", item_status="tech_validated",
                          scope_where=scope_where, scope_params=scope_params, limit=limit)
    takers = _part_takers(conn, [r["result_id"] for r in waiting])
    actor_id = (actor or {}).get("id")
    sign_entries = []
    for row in waiting:
        entry = _queue_entry(row)
        people = takers.get(row["result_id"], set()) | {
            p for p in (row.get("entered_by"), row.get("tech_validated_by"),
                        row.get("delta_ack_by")) if p}
        took_part = bool(actor_id) and actor_id in people
        entry["you_took_part"] = took_part
        entry["may_authorise"] = holds_authorise and not (took_part and distinct)
        sign_entries.append(entry)
    return {
        "technical": _grouped(technical, [_technical_entry(conn, r) for r in technical]),
        "authorisation": _grouped(waiting, sign_entries),
        "can_authorise": holds_authorise,
        "require_distinct_authoriser": distinct,
    }


# ════════════════════════════════════════════════════════════════════════════
#  Amended and withdrawn results (lis-13)
# ════════════════════════════════════════════════════════════════════════════

def _reported(conn, rid: int, dept: Optional[str]) -> tuple[dict, dict, dict]:
    """The released result, its test and its sample, for an amendment or a
    withdrawal. Refused: out of scope (404), a result already amended or
    withdrawn, one not yet reported (change it on the bench instead), a test
    no longer reported, a sample with no readable collection time."""
    current = _live_result(conn, rid, dept)
    status = current["status"]
    if status == "superseded":
        raise LabResultError("already_amended",
                             "This result has already been amended. Reload to see the current "
                             "version.", result_id=rid,
                             current_result_id=current.get("superseded_by_result_id"))
    if status == "withdrawn":
        raise LabResultError("withdrawn", "This result has been withdrawn.", result_id=rid)
    if status != "released":
        raise LabResultError("not_released",
                             "This result has not been reported yet. Change it on the bench, "
                             "or send it back, instead.", result_id=rid, result_status=status)
    item = _item_for_entry(conn, current["order_item_id"])
    if item["item_status"] != "released":
        raise LabResultError("not_live", "This result is no longer current.", result_id=rid,
                             item_status=item["item_status"])
    specimen = conn.execute(
        "SELECT id, patient_id, status, collected_at FROM lab_specimens WHERE id = ?",
        (current["specimen_id"],)).fetchone()
    if specimen is None or not specimen["collected_at"]:
        raise LabResultError("no_collection_time",
                             "This sample has no collection time, so the result cannot be "
                             "placed on the chart.")
    try:
        to_utc_iso(specimen["collected_at"])
    except ValueError:
        raise LabResultError("no_collection_time",
                             "This sample's collection time cannot be read, so the result "
                             "cannot be placed on the chart.")
    return current, item, dict(specimen)


def _clean_optional_reason(value: Any) -> Optional[str]:
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if len(text) > REASON_MAX:
        raise LabResultError("too_long", f"A reason is at most {REASON_MAX} characters.",
                             status=422)
    return text or None


def _json_dict(raw: Any) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _pending_proposal(conn, rid: int) -> Optional[dict]:
    """The amendment waiting for a second signatory on this result, or None.
    Only the latest proposal can be waiting, and a later cancellation (or the
    result no longer being the reported one) means none is."""
    row = conn.execute(
        """SELECT e.id, e.actor, e.occurred_at, e.after_json, u.name AS actor_name
           FROM lab_result_events e LEFT JOIN users u ON u.id = e.actor
           WHERE e.result_id = ? AND e.event_type = ?
           ORDER BY e.id DESC LIMIT 1""", (rid, _PROPOSED_EVENT)).fetchone()
    if row is None:
        return None
    if conn.execute("SELECT 1 FROM lab_result_events WHERE result_id = ? AND event_type = ? "
                    "AND id > ?", (rid, _CANCELLED_EVENT, row["id"])).fetchone() is not None:
        return None
    status = conn.execute("SELECT status FROM lab_results WHERE id = ?", (rid,)).fetchone()
    if status is None or status[0] != "released":
        return None
    after = _json_dict(row["after_json"])
    return {"proposal_id": int(row["id"]), "proposed_by": row["actor"],
            "proposed_by_name": row["actor_name"], "proposed_at": row["occurred_at"],
            "value": after.get("value"), "value_text": after.get("value_text"),
            "unit": after.get("unit"), "flag_text": after.get("flag_text"),
            "comment": after.get("comment"), "reason": after.get("reason"),
            "delta_reason": after.get("delta_reason")}


def _proposal_view(pending: Optional[dict], *, actor: dict) -> Optional[dict]:
    if pending is None:
        return None
    actor_id = (actor or {}).get("id")
    holds = has_permission((actor or {}).get("role", ""), LAB_AUTHORISE)
    mine = bool(actor_id) and actor_id == pending["proposed_by"]
    view = {k: pending[k] for k in ("proposal_id", "proposed_by_name", "proposed_at",
                                    "value_text", "unit", "flag_text", "comment", "reason",
                                    "delta_reason")}
    view["you_proposed"] = mine
    view["may_confirm"] = holds and not (mine and distinct_authoriser_required())
    return view


def _flag_words(fields: dict, result_type: Optional[str]) -> str:
    return flag_text({**fields, "result_type": result_type or "numeric"})


def _apply_amendment(conn, current: dict, item: dict, specimen: dict, *, value: Any,
                     comment: Optional[str], reason: str, delta_reason: Optional[str],
                     entered_by: str, actor: dict, now: str, one_person: bool,
                     proposal_id: Optional[int], request_meta: Optional[dict]) -> dict:
    """Replace a reported result with its amended version, on the caller's
    write transaction (see the module notes). Returns {result_id,
    observation_id, superseded_observation_id, raised, notice}."""
    rid = int(current["id"])
    fields = _value_fields(conn, item, value)
    delta_flag, delta = _delta_for(conn, item, fields)
    if delta_flag and not delta_reason:
        raise LabResultError(
            "delta_reason_required",
            f"{_test_name(item)}: {delta['note'] if delta else 'the amended value is delta-flagged.'} "
            "Give a reason for the delta flag with the amendment. Nothing was changed.",
            delta=delta)
    version = int(current.get("version") or 1) + 1
    actor_id = actor["id"]
    moved = conn.execute(
        "UPDATE lab_results SET status = 'superseded' WHERE id = ? AND status = 'released'",
        (rid,))
    if moved.rowcount != 1:
        raise LabResultError("not_live", "This result is no longer current.", result_id=rid)
    cur = conn.execute(
        """INSERT INTO lab_results
           (order_item_id, order_id, specimen_id, patient_id, test_id, version,
            value_milli, comparator, value_text, unit, decimal_places, comment,
            reference_range_id, reference_range_text, abnormal_flag,
            critical_limit_id, is_critical, delta_flag, delta_json,
            delta_ack_by, delta_ack_at, delta_ack_reason,
            entry_method, entered_by, entered_at, tech_validated_by, tech_validated_at,
            released_by, released_at, status, amend_reason, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   'manual', ?, ?, ?, ?, ?, ?, 'released', ?, 'lis_manual')""",
        (current["order_item_id"], current["order_id"], current["specimen_id"],
         current["patient_id"], current["test_id"], version,
         fields["value_milli"], fields["comparator"], fields["value_text"], fields["unit"],
         fields["decimal_places"], comment, fields["reference_range_id"],
         fields["reference_range_text"], fields["abnormal_flag"], fields["critical_limit_id"],
         fields["is_critical"], delta_flag, json.dumps(delta, default=str) if delta else None,
         entered_by if delta_flag else None, now if delta_flag else None,
         delta_reason if delta_flag else None,
         entered_by, now, entered_by, now, actor_id, now, reason))
    new_id = int(cur.lastrowid)
    conn.execute("UPDATE lab_results SET superseded_by_result_id = ? WHERE id = ?",
                 (new_id, rid))
    new_row = dict(conn.execute("SELECT * FROM lab_results WHERE id = ?", (new_id,)).fetchone())
    observation_id = _write_observation(conn, new_row, item, specimen,
                                        status=AMENDED_OBSERVATION_STATUS, now=now)
    conn.execute("UPDATE lab_results SET observation_id = ? WHERE id = ?",
                 (observation_id, new_id))
    old_observation = current.get("observation_id")
    if old_observation:
        replaced = conn.execute(
            "UPDATE observations SET status = ?, superseded_by = ? "
            "WHERE id = ? AND superseded_by IS NULL",
            (SUPERSEDED_OBSERVATION_STATUS, observation_id, int(old_observation)))
        if replaced.rowcount != 1:
            raise LabResultError("not_live",
                                 "The reported value on the chart has already been replaced. "
                                 "Nothing was amended.", result_id=rid)
    result_type = item.get("result_type")
    before = {**_snapshot(current), "version": current.get("version"),
              "flag_text": _flag_words(current, result_type)}
    after = {**_snapshot({**fields, "comment": comment}), "version": version,
             "flag_text": _flag_words(fields, result_type)}
    _event(conn, rid, current["patient_id"], "superseded", actor_id, now,
           before={"status": "released"},
           after={"status": "superseded", "superseded_by_result_id": new_id, "reason": reason,
                  "observation_id": old_observation})
    _event(conn, new_id, current["patient_id"], "amended", actor_id, now, before=before,
           after={**after, "reason": reason, "delta_reason": delta_reason,
                  "previous_result_id": rid, "entered_by": entered_by,
                  "one_person": one_person, "proposal_id": proposal_id,
                  "observation_id": observation_id})
    los.record_order_note_in_conn(
        conn, current["order_id"], actor_id=actor_id,
        detail=f"Laboratory result amended: {_test_name(item)} (version {version}). "
               f"Reason: {reason}")
    _audit(conn, action="lab_result_amended",
           summary=f"Reported laboratory result amended: {_test_name(item)}",
           actor=actor, result_id=new_id, patient_id=current["patient_id"],
           metadata={"previous_result_id": rid, "version": version, "before": before,
                     "after": after, "reason": reason, "delta_reason": delta_reason,
                     "order_id": current["order_id"], "specimen_id": current["specimen_id"],
                     "observation_id": observation_id,
                     "superseded_observation_id": old_observation,
                     "entered_by": entered_by, "one_person": one_person,
                     "distinct_authoriser_required": distinct_authoriser_required(),
                     "proposal_id": proposal_id},
           request_meta=request_meta)
    raised: list[str] = []
    # The new version's value goes through the critical raised for any
    # version it replaces (the chain), like a value changed on the bench:
    # an open one takes the change and posts it, a closed one whose value
    # no longer holds is reopened, and a new critical value with none open
    # raises one naming the value reported earlier.
    nid = lcs.raise_for_result_in_txn(conn, new_row, actor=actor, preliminary=False, now=now,
                                      trigger="amended", request_meta=request_meta)
    if nid:
        raised.append(nid)
    followup = lcs.open_count_for_result(conn, new_id) > 0
    patient = conn.execute("SELECT name, mrn FROM patients WHERE id = ?",
                           (current["patient_id"],)).fetchone()
    by = conn.execute("SELECT name FROM users WHERE id = ?", (actor_id,)).fetchone()
    notice = {"kind": "amended", "result_id": new_id, "patient_id": current["patient_id"],
              "actor_id": actor_id,
              "patient_name": patient["name"] if patient else None,
              "mrn": patient["mrn"] if patient else None, "test": _test_name(item),
              "before": _with_unit(_seen_text(current.get("value_text")), current.get("unit")),
              "after": _with_unit(_seen_text(fields.get("value_text")), fields.get("unit")),
              "flag_before": before["flag_text"], "flag_after": after["flag_text"],
              "was_critical": bool(int(current.get("is_critical") or 0)),
              "now_critical": bool(int(fields.get("is_critical") or 0)),
              "critical_followup": followup,
              "reason": reason, "by_name": by[0] if by else None, "at": now}
    return {"result_id": new_id, "observation_id": observation_id,
            "superseded_observation_id": old_observation, "raised": raised, "notice": notice}


def _amendment_answer(applied: dict, *, rid: int) -> dict:
    """What an applied amendment answers, after its commit: the new and the
    replaced result, the ward notice and any critical's delivery."""
    with db_session() as conn:
        result = _result_row(conn, applied["result_id"])
        previous = _result_row(conn, rid)
    return {"amended": True, "proposed": False, "result": result, "previous": previous,
            "observation_id": applied["observation_id"],
            "superseded_observation_id": applied["superseded_observation_id"],
            "ward_notice": post_result_notice(applied["notice"]),
            "criticals": lcs.deliver_all(applied["raised"])}


def amend_result(result_id: Any, *, value: Any, reason: Any, expected: Any,
                 comment: Any = None, delta_reason: Any = None, actor: dict,
                 dept: Optional[str] = None, request_meta: Optional[dict] = None) -> dict:
    """Amend a reported result (lab.authorise), with a reason, in ONE
    immediate transaction.

    While two different people are required (LIS-R5, the default) this
    PROPOSES the amendment: an 'amendment_proposed' result event and a
    clinical_access audit row ('lab_result_amendment_proposed'); the chart
    keeps the reported value until a different signatory confirms it
    (confirm_amendment). With the setting off it is applied at once
    (_apply_amendment), recorded as one person doing every step.

    `expected` is what the screen showed of the reported result; a result
    changed since is refused 409 'result_changed'. The new value is checked
    at the test's precision and flagged against the range and the critical
    limit approved now. A value that is delta-flagged needs `delta_reason`
    (409 'delta_reason_required', with the check). The same value and
    comment as reported is refused 422 'nothing_to_amend'. One proposal at a
    time (409 'amendment_pending')."""
    _require_authorise_permission(actor)
    text = _clean_reason(reason, words="Say why the reported result is being amended.")
    comment = _clean_comment(comment)
    delta_text = _clean_optional_reason(delta_reason)
    rid = _result_id(result_id)
    actor_id = actor["id"]
    distinct = distinct_authoriser_required()
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Result not found", status=404)
        current, item, specimen = _reported(conn, rid, dept)
        _refuse_if_changed(conn, current, expected, with_current=False)
        if _pending_proposal(conn, rid) is not None:
            raise LabResultError("amendment_pending",
                                 "An amendment to this result is already waiting for a second "
                                 "signatory. Confirm or cancel it first.", result_id=rid)
        fields = _value_fields(conn, item, value)
        if (_seen_text(fields["value_text"]) == _seen_text(current.get("value_text"))
                and _seen_text(comment) == _seen_text(current.get("comment"))):
            raise LabResultError("nothing_to_amend",
                                 "The amended result is the same as the reported one. Nothing "
                                 "was changed.", status=422)
        now = utc_iso_now()
        if not distinct:
            applied = _apply_amendment(
                conn, current, item, specimen, value=value, comment=comment, reason=text,
                delta_reason=delta_text, entered_by=actor_id, actor=actor, now=now,
                one_person=True, proposal_id=None, request_meta=request_meta)
        else:
            delta_flag, delta = _delta_for(conn, item, fields)
            if delta_flag and not delta_text:
                raise LabResultError(
                    "delta_reason_required",
                    f"{_test_name(item)}: {delta['note'] if delta else ''} Give a reason for "
                    "the delta flag with the amendment. Nothing was changed.", delta=delta)
            proposal = {"value": str(value), "value_text": fields["value_text"],
                        "unit": fields.get("unit"),
                        "flag_text": _flag_words(fields, item.get("result_type")),
                        "abnormal_flag": fields["abnormal_flag"],
                        "is_critical": fields["is_critical"], "comment": comment,
                        "reason": text, "delta_reason": delta_text if delta_flag else None}
            _event(conn, rid, current["patient_id"], _PROPOSED_EVENT, actor_id, now,
                   before=_snapshot(current), after=proposal)
            _audit(conn, action="lab_result_amendment_proposed",
                   summary=(f"Amendment to a reported laboratory result proposed, waiting for a "
                            f"second signatory: {_test_name(item)}"),
                   actor=actor, result_id=rid, patient_id=current["patient_id"],
                   metadata={"before": _snapshot(current), "proposal": proposal},
                   request_meta=request_meta)
            return {"amended": False, "proposed": True, "result": _result_row(conn, rid),
                    "proposal": _proposal_view(_pending_proposal(conn, rid), actor=actor)}
    return _amendment_answer(applied, rid=rid)


def confirm_amendment(result_id: Any, *, proposal_id: Any, expected: Any, proposed: Any,
                      actor: dict, dept: Optional[str] = None,
                      request_meta: Optional[dict] = None) -> dict:
    """A second signatory confirms a proposed amendment, which applies it in
    ONE immediate transaction (_apply_amendment): entered by the proposer,
    released by the confirmer. While two different people are required the
    proposer cannot confirm (403 'second_person_required'). `expected` is
    what the screen showed of the reported result and `proposed` the
    proposal's value_text and comment; either changed since is refused 409.
    The value is decided again now (precision, range, critical limit, delta);
    a newly delta-flagged value with no reason on the proposal is refused,
    and the proposal must be cancelled and made again."""
    _require_authorise_permission(actor)
    rid = _result_id(result_id)
    try:
        pid = int(proposal_id)
    except (TypeError, ValueError):
        raise LabResultError("no_pending_amendment", "This amendment is no longer waiting.",
                             status=409)
    seen = proposed if isinstance(proposed, dict) else {}
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Result not found", status=404)
        current, item, specimen = _reported(conn, rid, dept)
        _refuse_if_changed(conn, current, expected, with_current=False)
        pending = _pending_proposal(conn, rid)
        if pending is None or pending["proposal_id"] != pid:
            raise LabResultError("no_pending_amendment",
                                 "This amendment is no longer waiting. Reload the reported "
                                 "results.", result_id=rid)
        if (_seen_text(seen.get("value_text")) != _seen_text(pending.get("value_text"))
                or _seen_text(seen.get("comment")) != _seen_text(pending.get("comment"))):
            raise LabResultError("amendment_changed",
                                 "This amendment has changed since your screen loaded. "
                                 "Nothing was confirmed.", result_id=rid)
        mine = actor_id == pending["proposed_by"]
        if mine and distinct_authoriser_required():
            raise LabResultError("second_person_required",
                                 "You proposed this amendment. This site requires a second "
                                 "signatory to confirm it. Nothing was changed.",
                                 status=403, result_id=rid)
        now = utc_iso_now()
        applied = _apply_amendment(
            conn, current, item, specimen, value=pending["value"], comment=pending["comment"],
            reason=pending["reason"] or "", delta_reason=pending["delta_reason"],
            entered_by=pending["proposed_by"], actor=actor, now=now, one_person=mine,
            proposal_id=pid, request_meta=request_meta)
    return _amendment_answer(applied, rid=rid)


def cancel_amendment(result_id: Any, *, proposal_id: Any, reason: Any, actor: dict,
                     dept: Optional[str] = None, request_meta: Optional[dict] = None) -> dict:
    """Cancel a proposed amendment (lab.authorise, either signatory), with a
    reason, in ONE immediate transaction: an 'amendment_cancelled' result
    event and a clinical_access audit row. The reported result is unchanged."""
    _require_authorise_permission(actor)
    text = _clean_reason(reason, words="Say why the amendment is cancelled.")
    rid = _result_id(result_id)
    try:
        pid = int(proposal_id)
    except (TypeError, ValueError):
        raise LabResultError("no_pending_amendment", "This amendment is no longer waiting.")
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Result not found", status=404)
        current = _live_result(conn, rid, dept)
        pending = _pending_proposal(conn, rid)
        if pending is None or pending["proposal_id"] != pid:
            raise LabResultError("no_pending_amendment",
                                 "This amendment is no longer waiting. Reload the reported "
                                 "results.", result_id=rid)
        now = utc_iso_now()
        _event(conn, rid, current["patient_id"], _CANCELLED_EVENT, actor_id, now,
               after={"proposal_id": pid, "reason": text})
        _audit(conn, action="lab_result_amendment_cancelled",
               summary="Proposed amendment to a reported laboratory result cancelled",
               actor=actor, result_id=rid, patient_id=current["patient_id"],
               metadata={"proposal_id": pid, "reason": text,
                         "proposed_by": pending["proposed_by"]},
               request_meta=request_meta)
        return {"result": _result_row(conn, rid), "cancelled": True}


def withdraw_result(result_id: Any, *, reason: Any, expected: Any, wrong_patient: bool = False,
                    actor: dict, dept: Optional[str] = None,
                    request_meta: Optional[dict] = None) -> dict:
    """Withdraw a reported result entered in error (lab.authorise), with a
    reason, in ONE immediate transaction: the result to 'withdrawn', its
    observation to 'entered-in-error' (gone from every chart reader), its
    test cancelled with the reason, a 'withdrawn' result event, an
    order_events note and a clinical_access audit row
    ('lab_result_withdrawn'). The order is settled by
    lab_order_service.settle_after_withdrawal: a resulted order whose only
    reported test this was stays resulted, with an exception for the
    laboratory. A proposed amendment waiting on it is cancelled with it. A
    critical raised for it, or for a version it replaced, is never closed by
    it and records it (lab_critical_service.record_withdrawal_in_txn, in the
    same transaction): an open one records the withdrawal and stays open, or
    with none open the latest closed one, whose value the ward confirmed and
    may have acted on, is reopened at tier 1. Each is posted to the case room
    after commit (`criticals` in the answer), and the laboratory phones the
    ward and logs the call with a read-back. `open_criticals` counts the
    criticals now open for any version of the result.

    One signatory withdraws in one step whatever the two-person setting:
    removing a value entered in error must not wait for a second signatory.
    It is recorded as one person. With `wrong_patient` and ORB_NABH on, the
    answer carries a prompt to report a patient identification incident."""
    _require_authorise_permission(actor)
    text = _clean_reason(reason, words="Say why the reported result is withdrawn.")
    rid = _result_id(result_id)
    actor_id = actor["id"]
    with db_session(immediate=True) as conn:
        if not _tables_present(conn):
            raise LabResultError("not_found", "Result not found", status=404)
        current, item, specimen = _reported(conn, rid, dept)
        _refuse_if_changed(conn, current, expected, with_current=False)
        now = utc_iso_now()
        pending = _pending_proposal(conn, rid)
        if pending is not None:
            _event(conn, rid, current["patient_id"], _CANCELLED_EVENT, actor_id, now,
                   after={"proposal_id": pending["proposal_id"],
                          "reason": "The reported result was withdrawn"})
        moved = conn.execute(
            "UPDATE lab_results SET status = 'withdrawn', amend_reason = ? "
            "WHERE id = ? AND status = 'released'", (text, rid))
        if moved.rowcount != 1:
            raise LabResultError("not_live", "This result is no longer current.", result_id=rid)
        observation = current.get("observation_id")
        if observation:
            gone = conn.execute(
                "UPDATE observations SET status = ? WHERE id = ? AND superseded_by IS NULL",
                (WITHDRAWN_OBSERVATION_STATUS, int(observation)))
            if gone.rowcount != 1:
                raise LabResultError("not_live",
                                     "The reported value on the chart has already been "
                                     "replaced. Nothing was withdrawn.", result_id=rid)
        cancelled = conn.execute(
            "UPDATE lab_order_items SET status = 'cancelled', cancelled_reason = ?, "
            "cancelled_by = ?, updated_at = ? WHERE id = ? AND status = 'released'",
            (f"Result withdrawn: {text}"[:500], actor_id, now, current["order_item_id"]))
        if cancelled.rowcount != 1:
            raise LabResultError("not_live", "This result is no longer current.", result_id=rid)
        _event(conn, rid, current["patient_id"], "withdrawn", actor_id, now,
               before={**_snapshot(current), "status": "released"},
               after={"status": "withdrawn", "reason": text, "wrong_patient": bool(wrong_patient),
                      "observation_id": observation, "one_person": True})
        order = los.settle_after_withdrawal(conn, current["order_id"], actor_id=actor_id,
                                            test_name=_test_name(item))
        los.record_order_note_in_conn(
            conn, current["order_id"], actor_id=actor_id,
            detail=f"Laboratory result withdrawn: {_test_name(item)}. Reason: {text}")
        withdrawn_row = dict(conn.execute("SELECT * FROM lab_results WHERE id = ?",
                                          (rid,)).fetchone())
        recorded = lcs.record_withdrawal_in_txn(conn, withdrawn_row, actor=actor, now=now,
                                                request_meta=request_meta)
        open_criticals = lcs.open_count_for_result(conn, rid)
        _audit(conn, action="lab_result_withdrawn",
               summary=f"Reported laboratory result withdrawn: {_test_name(item)}",
               actor=actor, result_id=rid, patient_id=current["patient_id"],
               metadata={"reason": text, "wrong_patient": bool(wrong_patient),
                         "before": _snapshot(current), "order_id": current["order_id"],
                         "order_status": order.get("status"),
                         "order_conflict": bool(order.get("conflict")),
                         "observation_id": observation, "one_person": True,
                         "open_criticals": open_criticals,
                         "critical_notifications": recorded},
               request_meta=request_meta)
        incident = None
        if wrong_patient and bool(getattr(config, "NABH_ENABLED", False)):
            incident = {"incident_type": "patient_identification",
                        "message": WRONG_PATIENT_INCIDENT_TEXT}
        patient = conn.execute("SELECT name, mrn FROM patients WHERE id = ?",
                               (current["patient_id"],)).fetchone()
        by = conn.execute("SELECT name FROM users WHERE id = ?", (actor_id,)).fetchone()
        notice = {"kind": "withdrawn", "result_id": rid, "patient_id": current["patient_id"],
                  "actor_id": actor_id,
                  "patient_name": patient["name"] if patient else None,
                  "mrn": patient["mrn"] if patient else None, "test": _test_name(item),
                  "before": _with_unit(_seen_text(current.get("value_text")), current.get("unit")),
                  "collected_at": specimen.get("collected_at"),
                  "wrong_patient": bool(wrong_patient),
                  "was_critical": bool(int(current.get("is_critical") or 0)) or bool(recorded),
                  "critical_followup": open_criticals > 0,
                  "reason": text, "by_name": by[0] if by else None, "at": now}
        out = {"result": _result_row(conn, rid),
               "order": {"id": current["order_id"], "status": order.get("status"),
                         "conflict": bool(order.get("conflict"))},
               "open_criticals": open_criticals, "incident_prompt": incident}
    out["ward_notice"] = post_result_notice(notice)
    out["criticals"] = lcs.deliver_all(recorded)
    lab_module.refresh_drain_state()
    return out


# ── The ward's notice ───────────────────────────────────────────────────────

def result_notice_text(notice: dict) -> str:
    """The plain words posted to the patient's case room for an amended or
    withdrawn result. The laboratory's phone call about a critical value is
    promised only while a critical is open to track it on Critical results
    (`critical_followup`, true unless the step said otherwise). Pure."""
    from services.hospital_time import hospital_stamp

    who = notice.get("patient_name") or "this patient"
    if notice.get("mrn"):
        who += f", UHID {notice['mrn']}"
    when = hospital_stamp(notice.get("at")) if notice.get("at") else ""
    by = notice.get("by_name")
    signed = (f" {'Amended' if notice.get('kind') == 'amended' else 'Withdrawn'} by {by}"
              + (f" at {when}" if when else "") + ".") if by else ""
    if notice.get("kind") == "amended":
        text = (f"The laboratory amended a reported result for {who}: {notice.get('test')} was "
                f"{notice.get('before') or 'blank'}, now {notice.get('after') or 'blank'}.")
        if notice.get("flag_before") != notice.get("flag_after"):
            text += f" Flag: {notice.get('flag_before')} before, {notice.get('flag_after')} now."
        text += f" Reason: {notice.get('reason')}.{signed} The chart shows the new value."
        if notice.get("was_critical") and not notice.get("now_critical"):
            text += (" The earlier value was reported as critical; the laboratory will phone "
                     "to confirm the change." if notice.get("critical_followup", True)
                     else " The earlier value was reported as critical.")
        return text
    collected = hospital_stamp(notice.get("collected_at")) if notice.get("collected_at") else ""
    text = (f"The laboratory withdrew a reported result for {who}: {notice.get('test')} "
            f"{notice.get('before') or ''}".rstrip()
            + (f", collected {collected}" if collected else "") + ".")
    text += (" It belonged to another patient." if notice.get("wrong_patient")
             else " It was entered in error.")
    text += f" It is no longer on the chart. Reason: {notice.get('reason')}.{signed}"
    if notice.get("was_critical"):
        text += (" It had been reported as critical; the laboratory will phone to confirm "
                 "the withdrawal." if notice.get("critical_followup", True)
                 else " It had been reported as critical.")
    return text


def _record_result_notice_failure(result_id: int, patient_id: str, reason: str) -> None:
    try:
        with db_session() as conn:
            _event(conn, result_id, patient_id, "ward_notice_failed", None, utc_iso_now(),
                   after={"reason": reason})
    except Exception:
        logger.exception("Could not record the failed result notice for result %s", result_id)


def post_result_notice(notice: dict) -> dict:
    """Tell the ward, after an amendment or a withdrawal has committed, with a
    plain message in the patient's case room ('Laboratory', type 'text',
    is_bot 0, source 'lab'), pushed live as 'message:new'. NEVER raises: the
    change already stands, so a failure is logged, recorded as a
    'ward_notice_failed' result event and answered {posted: False, reason}.
    No case room is reason 'no_case_room'; a discharged patient is
    'case_room_closed' and nothing is posted, because no clinician would see
    it."""
    from services import case_room_alerts

    result_id = int(notice["result_id"])
    patient_id = notice.get("patient_id")
    # The signatory's own account, shown as 'Laboratory' (messages.sender_id
    # must name a users row).
    sender = notice.get("actor_id")
    try:
        with db_session() as conn:
            row = conn.execute("SELECT status FROM patients WHERE id = ?",
                               (patient_id,)).fetchone()
            if row is not None and row["status"] == "discharged":
                _event(conn, result_id, patient_id, "ward_notice_failed", None, utc_iso_now(),
                       after={"reason": "case_room_closed"})
                return {"posted": False, "reason": "case_room_closed"}
            logged, message_id, channel_id, msg_data = case_room_alerts.persist_case_room_alert(
                patient_id, result_notice_text(notice), sender_id=sender,
                sender_name=lcs.SENDER_NAME, sender_role=lcs.SENDER_ROLE, is_bot=False,
                source=lcs.MESSAGE_SOURCE, db=conn, message_type="text")
            if not logged:
                _event(conn, result_id, patient_id, "ward_notice_failed", None, utc_iso_now(),
                       after={"reason": "no_case_room"})
                return {"posted": False, "reason": "no_case_room"}
            _event(conn, result_id, patient_id, "ward_notified", sender, utc_iso_now(),
                   after={"message_id": message_id, "channel_id": channel_id})
    except Exception:
        logger.exception("Laboratory notice for result %s could not be posted; the change "
                         "stands", result_id)
        _record_result_notice_failure(result_id, patient_id, "failed")
        return {"posted": False, "reason": "failed"}
    case_room_alerts.push_message_new(msg_data, channel_id,
                                      what=f"laboratory result notice {result_id}")
    return {"posted": True, "reason": None, "message_id": message_id, "channel_id": channel_id}


# ── The reported results of one sample ──────────────────────────────────────

_REPORTED_STATUSES = ("released", "superseded", "withdrawn")


def _reported_entry(row: dict) -> dict:
    entry = {
        "result_id": row["id"], "version": int(row.get("version") or 1),
        "status": row["status"], "value_text": row["value_text"],
        "comparator": row["comparator"], "unit": row["unit"],
        "abnormal_flag": row["abnormal_flag"] or "", "reference_range_text":
        row["reference_range_text"], "comment": row["comment"],
        "is_critical": int(row.get("is_critical") or 0),
        "critical_limit_text": row.get("critical_limit_text"),
        "entered_by_name": row.get("entered_by_name"), "entered_at": row.get("entered_at"),
        "released_by_name": row.get("released_by_name"), "released_at": row.get("released_at"),
        "amend_reason": row.get("amend_reason"),
    }
    entry["flag_text"] = flag_text({**entry, "result_type": row.get("result_type"),
                                    "reference_range_id": row.get("reference_range_id")})
    return entry


def reported_results(conn, specimen_id: Any, *, actor: dict, dept: Optional[str]) -> dict:
    """One sample's reported results, for amending or withdrawing them: per
    test, the reported version, the versions it replaced or the withdrawal,
    and any amendment waiting for a second signatory. 404 for an unknown or
    out-of-scope sample; 422 'bad_accession' for a bad check digit. A read:
    the route audits the patient."""
    from permissions import patient_in_scope

    try:
        accession = los.checked_accession(specimen_id)
    except los.LabOrderError as err:
        raise LabResultError(err.code, err.message, status=err.status)
    if not _tables_present(conn):
        raise LabResultError("not_found", "Specimen not found", status=404)
    sample = conn.execute(
        """SELECT s.id, s.patient_id, s.status, s.collected_at, s.received_at,
                  p.name, p.mrn, p.ward, p.bed, p.status AS patient_status
           FROM lab_specimens s JOIN patients p ON p.id = s.patient_id
           WHERE s.id = ?""", (accession,)).fetchone()
    if sample is None or not patient_in_scope(conn, sample["patient_id"], dept):
        raise LabResultError("not_found", "Specimen not found", status=404)
    rows = [dict(r) for r in conn.execute(
        f"""SELECT r.*, t.name AS test_name, t.short_name AS test_short_name, t.result_type,
                   cl.display_text AS critical_limit_text, eu.name AS entered_by_name,
                   ru.name AS released_by_name
            FROM lab_results r
            LEFT JOIN lab_tests t ON t.id = r.test_id
            LEFT JOIN lab_critical_limits cl ON cl.id = r.critical_limit_id
            LEFT JOIN users eu ON eu.id = r.entered_by
            LEFT JOIN users ru ON ru.id = r.released_by
            WHERE r.specimen_id = ? AND r.status IN ({_placeholders(_REPORTED_STATUSES)})
            ORDER BY r.order_item_id, r.version, r.id""",
        (accession, *_REPORTED_STATUSES)).fetchall()]
    tests: list[dict] = []
    by_item: dict[int, dict] = {}
    for row in rows:
        group = by_item.get(row["order_item_id"])
        if group is None:
            group = {"item_id": row["order_item_id"], "order_id": row["order_id"],
                     "test": {"id": row["test_id"], "name": row.get("test_name") or row["test_id"],
                              "short_name": row.get("test_short_name") or row.get("test_name")
                              or row["test_id"], "result_type": row.get("result_type") or "numeric"},
                     "current": None, "history": [], "withdrawn": None, "pending_amendment": None}
            by_item[row["order_item_id"]] = group
            tests.append(group)
        entry = _reported_entry(row)
        if row["status"] == "released":
            group["current"] = entry
            group["pending_amendment"] = _proposal_view(_pending_proposal(conn, int(row["id"])),
                                                        actor=actor)
        elif row["status"] == "withdrawn":
            group["withdrawn"] = entry
        else:
            group["history"].append(entry)
    discharged = sample["patient_status"] == "discharged"
    return {
        "specimen": {"id": accession, "status": sample["status"],
                     "collected_at": sample["collected_at"], "received_at": sample["received_at"],
                     "patient": {"id": sample["patient_id"], "name": sample["name"],
                                 "mrn": sample["mrn"],
                                 "ward": None if discharged else sample["ward"],
                                 "bed": None if discharged else sample["bed"],
                                 "discharged": discharged}},
        "tests": tests,
        "can_amend": has_permission((actor or {}).get("role", ""), LAB_AUTHORISE),
        "require_distinct_authoriser": distinct_authoriser_required(),
    }
