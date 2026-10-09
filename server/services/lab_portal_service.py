"""Laboratory reports in the patient portal (lis-29, ruling LIS-R11 (c)).

Behind ORB_LAB_PORTAL_REPORTS (feature 'lab_portal_reports', default off),
which needs the laboratory module on (config.FEATURE_PREREQUISITES; not while
it drains). With the switch off every route that reaches this module answers
404 and Bridge shows exactly what it showed before.

WHEN A RESULT REACHES THE PATIENT. A released result (the laboratory's
authorised value, lab_results.released_at) reaches the patient's own Bridge
portal only after one of two things (ruling LIS-R11 (c), built in its strictest
reading and held for Samuel before go-live):

  * A doctor released it. POST /api/bridge/patients/{patient_id}/lab-reports/
    {accession}/release, by a holder of discharge.publish (the permission that
    already releases a reviewed discharge summary to the portal) inside the
    patient's department. The doctor names the results on the report they read
    (seen_result_ids); if a result was released, amended or withdrawn since, the
    answer is 409 report_changed and nothing is written. The release is one
    'portal_released' event on the sample (lab_specimen_events, append-only)
    naming exactly the result versions it covers, so a version released later
    is never covered by an earlier release, whatever the clocks say.
  * The site's delay passed: ORB_LAB_PORTAL_DELAY_HOURS after that version was
    released, while it was still the live version. Unset, the default, means
    never: only a doctor's release. A critical value (HH or LL against an
    approved limit) never reaches the portal by the delay; only a doctor's
    release shows it. A version amended or withdrawn before its delay ran out
    never reaches the portal by the delay (it stopped being live at its
    successor's release or at its withdrawal, whichever the records show
    first; when neither can be read, never), so the delay never shows the
    patient a value the laboratory had already replaced or withdrawn.

Each result version decides for itself. An amended result shows its new value
only once the new version has reached the portal; until then the test says the
laboratory corrected it and shows no value, never the superseded value as if it
were current. Once the new value shows, the earlier values the patient had
already been shown are listed beneath it as corrected. A withdrawn result never
shows a value. Pending, rejected and held tests all read 'Not available here
yet', so the patient cannot tell a result held for a doctor from one the
laboratory has not finished.

A report is listed for the patient once any result on it has reached the
portal, and it stays listed afterwards.

WHAT THE PATIENT SEES of a report: the tests; each shown value as the
laboratory reported it, with its unit; the flag in words as printed (High, Low,
Critically high, Critically low, Abnormal), never in colour; the reference
range as snapshot at entry; the laboratory's comment; the specimen type, the
collection and report times; and the signatories with their registration
numbers. Never the patient block, ward or bed, the staff who collected or
received the sample, an amendment or withdrawal reason (it can name another
patient), a critical-call log, or anything the laboratory has not released.

SELF-SCOPE. The patient's own record only (patients.user_id, resolved by the
route); another patient's sample is the same 404 as an unknown one.

This module never reads observations (the one reader is
services/lab_observations.py). Every function takes the caller's open
connection; release_in_txn writes inside the caller's immediate transaction
and audits there, fail-closed.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import config
from services import lab_master_service as lm
from services import lab_order_service as los
from services import lab_report_service as lrep
from services.audit_service import log_audit_event
from services.clinical_time import format_utc, parse_to_utc

#: The lab_specimen_events type a doctor's release writes.
PORTAL_RELEASE_EVENT = "portal_released"

#: Flags that never reach the portal by the delay alone.
CRITICAL_FLAGS = ("HH", "LL")

#: The most result ids one release may name.
SEEN_IDS_MAX = 200

#: Values a SQL IN (...) list carries at once.
_CHUNK = 400

# Words on the patient's screen. Plain, and promising nothing the system does
# not do.
SIMULATED_TEXT = "Simulated result, not from your care"
NOT_AVAILABLE_TEXT = "Not available here yet"
CORRECTED_WAITING_TEXT = ("The laboratory corrected this result. The corrected result is "
                          "not available here yet.")
CORRECTED_TEXT = "Corrected by the laboratory"
WITHDRAWN_TEXT = "Withdrawn by the laboratory. There is no result to show."
CANCELLED_TEXT = "Cancelled"
SOME_HELD_TEXT = "Some results on this report are not available here yet."

PATIENT_NOTES = (
    "These are the laboratory's results as it reported them. Ask your doctor or nurse "
    "what they mean for you.",
    "A flag such as High or Low compares a result with the laboratory's reference range. "
    "It is not a diagnosis.",
    "Each reference range is the one in force when the result was entered.",
)

# Words on the staff answer.
STAFF_STATE_WORDS = {
    "shown": "Shown to the patient",
    "partly_shown": "Partly shown to the patient: some results are not shown yet",
    "waiting": "Not shown to the patient yet: shown after the site's delay, or sooner "
               "when a doctor releases it",
    "needs_release": "Not shown to the patient: waits for a doctor's release",
    "nothing_released": "Nothing on this sample is reported yet",
}


class PortalReportError(Exception):
    """A refusal with a machine code, a sentence and the HTTP status."""

    def __init__(self, code: str, message: str, *, status: int = 404, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


def _not_found(**extra: Any) -> PortalReportError:
    return PortalReportError("not_found", "Report not found", status=404, **extra)


# ════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ════════════════════════════════════════════════════════════════════════════

def _now() -> datetime:
    """Now, aware UTC. Its own name so a test can pin it."""
    return datetime.now(timezone.utc)


def delay_hours() -> Optional[int]:
    """The site's delay in hours, read at call time, or None (never by itself)."""
    value = getattr(config, "LAB_PORTAL_DELAY_HOURS", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value > getattr(config, "LAB_PORTAL_DELAY_MAX_HOURS", 8760):
        return None
    return value


def portal_note(delay: Optional[int] = None) -> str:
    """The one line the patient reads above their laboratory reports."""
    if delay is None:
        return ("Laboratory reports appear here after your doctor has released them "
                "to you.")
    unit = "hour" if delay == 1 else "hours"
    return (f"Laboratory reports appear here {delay} {unit} after the laboratory reports "
            "them, or sooner when your doctor releases them. Some results appear only "
            "after your doctor has released them.")


def _instant(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parse_to_utc(value)
    except (TypeError, ValueError):
        return None


def _latest(values: Iterable[Any]) -> Optional[str]:
    best, best_at = None, None
    for value in values:
        at = _instant(value)
        if at is not None and (best_at is None or at > best_at):
            best, best_at = value, at
    return best


def _earliest(values: Iterable[Any]) -> Optional[datetime]:
    """The earliest readable instant among these values, aware UTC, or None."""
    best = None
    for value in values:
        at = _instant(value)
        if at is not None and (best is None or at < best):
            best = at
    return best


def _chunks(values: Iterable[Any]) -> Iterable[list]:
    items = list(dict.fromkeys(v for v in values if v is not None))
    for start in range(0, len(items), _CHUNK):
        yield items[start:start + _CHUNK]


def _tables_present(conn) -> bool:
    return lrep.tables_present(conn) and lm.tables_present(conn, ("lab_specimen_events",))


def is_critical(version: dict) -> bool:
    """A critical value: flagged critical at entry, or HH or LL."""
    return (int(version.get("is_critical") or 0) == 1
            or (version.get("abnormal_flag") or "") in CRITICAL_FLAGS)


def _int_ids(values: Iterable[Any]) -> set[int]:
    out: set[int] = set()
    for value in values or ():
        if isinstance(value, bool):
            continue
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Loading
# ════════════════════════════════════════════════════════════════════════════

#: The lab_result_events types that end a version's time as the live one.
_END_EVENTS = ("superseded", "withdrawn")


def _versions(conn, specimen_ids: list[str]) -> dict[int, list[dict]]:
    """Every released version of every result on these samples, by order item,
    oldest version first. A version that is no longer the live one (amended
    or withdrawn) carries `live_until`: when it stopped being live, or None
    when that cannot be read (see _mark_ends)."""
    out: dict[int, list[dict]] = {}
    for chunk in _chunks(specimen_ids):
        marks = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT r.id, r.order_item_id, i.specimen_id, r.version, r.value_text, r.unit, "
            "r.status, r.released_at, r.is_critical, r.abnormal_flag, "
            "r.superseded_by_result_id "
            "FROM lab_results r JOIN lab_order_items i ON i.id = r.order_item_id "
            f"WHERE i.specimen_id IN ({marks}) AND r.released_at IS NOT NULL "
            "ORDER BY r.order_item_id, r.version, r.id", tuple(chunk)).fetchall()
        for row in rows:
            out.setdefault(int(row["order_item_id"]), []).append(dict(row))
    _mark_ends(conn, out)
    return out


def _mark_ends(conn, versions_by_item: dict[int, list[dict]]) -> None:
    """Set `live_until` on every version that is no longer live: the earliest
    of its successor's release time and its own 'superseded' or 'withdrawn'
    result event. None when none of these can be read, and then the delay
    never puts that version in the portal (see delay_due_at)."""
    every = [v for rows in versions_by_item.values() for v in rows]
    ended = [v for v in every if (v.get("status") or "released") != "released"]
    if not ended:
        return
    by_id = {int(v["id"]): v for v in every}
    stamps: dict[int, list[Any]] = {}
    for chunk in _chunks(int(v["id"]) for v in ended):
        marks = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT result_id, occurred_at FROM lab_result_events "
            f"WHERE event_type IN ({', '.join('?' for _ in _END_EVENTS)}) "
            f"AND result_id IN ({marks})", (*_END_EVENTS, *chunk)).fetchall()
        for row in rows:
            stamps.setdefault(int(row["result_id"]), []).append(row["occurred_at"])
    for version in ended:
        candidates = list(stamps.get(int(version["id"]), []))
        successor = by_id.get(int(version.get("superseded_by_result_id") or 0))
        if successor is not None:
            candidates.append(successor.get("released_at"))
        version["live_until"] = _earliest(candidates)


def _releases(conn, specimen_ids: list[str]) -> dict[str, list[dict]]:
    """The doctors' portal releases on these samples, oldest first:
    {specimen_id: [{result_ids, at, by}]}."""
    out: dict[str, list[dict]] = {}
    for chunk in _chunks(specimen_ids):
        marks = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT e.specimen_id, e.occurred_at, e.metadata_json, e.actor_user_id, "
            "u.name AS actor_name FROM lab_specimen_events e "
            "LEFT JOIN users u ON u.id = e.actor_user_id "
            f"WHERE e.event_type = ? AND e.specimen_id IN ({marks}) ORDER BY e.id",
            (PORTAL_RELEASE_EVENT, *chunk)).fetchall()
        for row in rows:
            try:
                meta = json.loads(row["metadata_json"] or "{}")
            except ValueError:
                meta = {}
            ids = _int_ids(meta.get("result_ids") if isinstance(meta, dict) else ())
            out.setdefault(row["specimen_id"], []).append({
                "result_ids": sorted(ids),
                "at": row["occurred_at"],
                "by": {"name": row["actor_name"] or "Name not on file"},
            })
    return out


def _released_ids(releases: list[dict]) -> set[int]:
    ids: set[int] = set()
    for release in releases or ():
        ids.update(release["result_ids"])
    return ids


# ════════════════════════════════════════════════════════════════════════════
#  When one version reaches the portal
# ════════════════════════════════════════════════════════════════════════════

def delay_due_at(version: dict, delay: Optional[int]) -> Optional[datetime]:
    """When the delay alone puts this version in the portal, or None when it
    never does: no delay set, a critical value, no readable release time, or
    a version that stopped being live (amended or withdrawn) before its delay
    ran out, or at a time that cannot be read. The delay shows only the value
    that is live when it runs out, so the patient is never shown a value the
    laboratory had already replaced or withdrawn."""
    if delay is None or is_critical(version):
        return None
    at = _instant(version.get("released_at"))
    if at is None:
        return None
    due = at + timedelta(hours=delay)
    if (version.get("status") or "released") != "released":
        until = _instant(version.get("live_until"))
        if until is None or due >= until:
            return None
    return due


def version_out(version: dict, released: set[int], now: datetime,
                delay: Optional[int]) -> bool:
    """Has this released version reached the patient's portal?"""
    if int(version["id"]) in released:
        return True
    due = delay_due_at(version, delay)
    return due is not None and due <= now


# ════════════════════════════════════════════════════════════════════════════
#  The patient's view
# ════════════════════════════════════════════════════════════════════════════

def _patient_test(entry: dict, versions: list[dict], released: set[int], now: datetime,
                  delay: Optional[int]) -> tuple[dict, bool, Optional[int]]:
    """One test as the patient sees it. Returns (test, any version out, the
    id of the shown version or None)."""
    out_ids = {int(v["id"]) for v in versions if version_out(v, released, now, delay)}
    live = entry.get("result")
    state = entry["state"]
    test = {
        "name": entry["test"]["name"],
        "panel_name": entry.get("panel_name"),
        "state": "waiting",
        "state_text": NOT_AVAILABLE_TEXT,
        "value_text": None,
        "unit": None,
        "flag_text": "",
        "reference_range_text": None,
        "comment": None,
        "reported_at": None,
        "corrected": False,
        "corrected_text": None,
        "earlier": [],
        "simulated": False,
    }
    shown_id = None
    if state == "reported" and live is not None and int(live["id"]) in out_ids:
        shown_id = int(live["id"])
        # A withdrawn version anywhere on the chain may be another patient's
        # sample (lis-13): then no earlier value is repeated.
        withheld = any(v["status"] == "withdrawn" for v in versions)
        earlier = []
        if not withheld:
            for index, version in enumerate(versions):
                if int(version["id"]) == shown_id or int(version["id"]) not in out_ids:
                    continue
                successor = versions[index + 1] if index + 1 < len(versions) else None
                earlier.append({
                    "value_text": version.get("value_text"),
                    "unit": version.get("unit") or None,
                    "replaced_at": successor.get("released_at") if successor else None,
                })
        corrected = int(live.get("version") or 1) > 1
        test.update({
            "state": "shown",
            "state_text": "",
            "value_text": live.get("value_text"),
            "unit": live.get("unit") or None,
            "flag_text": live.get("flag_text") or "",
            "reference_range_text": (live.get("reference_range_text")
                                     if live.get("range_applies") else None),
            "comment": live.get("comment") or None,
            "reported_at": live.get("reported_at"),
            "corrected": corrected,
            "corrected_text": CORRECTED_TEXT if corrected else None,
            "earlier": earlier,
            "simulated": bool(live.get("simulated")),
        })
    elif state == "reported" and out_ids:
        test.update({"state": "corrected_waiting", "state_text": CORRECTED_WAITING_TEXT})
    elif state == "withdrawn":
        test.update({"state": "withdrawn", "state_text": WITHDRAWN_TEXT})
    elif state == "cancelled":
        test.update({"state": "cancelled", "state_text": CANCELLED_TEXT})
    return test, bool(out_ids), shown_id


def _patient_report(report: dict, versions_by_item: dict[int, list[dict]],
                    released: set[int], now: datetime,
                    delay: Optional[int]) -> tuple[Optional[dict], list[int]]:
    """The report as the patient sees it and the ids of the values shown, or
    (None, []) when nothing on it has reached the portal."""
    tests, shown_ids, listed = [], [], False
    signatories: list[dict] = []
    for entry in report["items"]:
        versions = versions_by_item.get(int(entry["item_id"]), [])
        test, out, shown_id = _patient_test(entry, versions, released, now, delay)
        listed = listed or out
        tests.append(test)
        if shown_id is not None:
            shown_ids.append(shown_id)
            signer = (entry.get("result") or {}).get("authorised_by")
            if signer:
                person = {"name": signer.get("name"),
                          "registration_text": signer.get("registration_text")}
                if person not in signatories:
                    signatories.append(person)
    if not listed:
        return None, []
    held = any(t["state"] in ("waiting", "corrected_waiting") for t in tests)
    simulated = any(t["simulated"] for t in tests)
    view = {
        "accession": report["accession"],
        "specimen_type": report["specimen"]["type"],
        "collected_at": report["specimen"]["collected_at"],
        "reported_at": _latest(t["reported_at"] for t in tests if t["state"] == "shown"),
        "complete": not held,
        "status_text": SOME_HELD_TEXT if held else report["status_text"],
        "simulated": simulated,
        "simulated_text": SIMULATED_TEXT if simulated else None,
        "tests": tests,
        "signatories": signatories,
        "notes": list(PATIENT_NOTES),
    }
    return view, shown_ids


def _line(view: dict) -> dict:
    """A patient report as one line of the Lab History list: no values."""
    return {
        "accession": view["accession"],
        "specimen_type": view["specimen_type"],
        "collected_at": view["collected_at"],
        "reported_at": view["reported_at"],
        "complete": view["complete"],
        "status_text": view["status_text"],
        "simulated": view["simulated"],
        "tests": [t["name"] for t in view["tests"]],
        "shown_count": sum(1 for t in view["tests"] if t["state"] == "shown"),
        "test_count": len(view["tests"]),
    }


def _views(conn, patient_id: str, specimen_ids: list[str], now: datetime,
           delay: Optional[int]) -> list[tuple[dict, list[int]]]:
    """The patient views of these samples that are listed, in the order the
    report service returns them (newest collection first). A sample recorded
    against any other patient is dropped, whatever its results say."""
    if not specimen_ids:
        return []
    reports = [r for r in lrep._reports(conn, specimen_ids)
               if r["patient"]["id"] == patient_id]
    versions = _versions(conn, [r["accession"] for r in reports])
    releases = _releases(conn, [r["accession"] for r in reports])
    out = []
    for report in reports:
        released = _released_ids(releases.get(report["accession"], []))
        view, shown = _patient_report(report, versions, released, now, delay)
        if view is not None:
            out.append((view, shown))
    return out


def patient_reports(conn, patient_id: str, *, now: Optional[datetime] = None) -> list[dict]:
    """The patient's own reports that have reached the portal, newest
    collection first, as list lines with no values."""
    if not patient_id or not _tables_present(conn):
        return []
    ids = lrep._released_specimen_ids(conn, "r.patient_id = ?", (patient_id,),
                                      lrep.REPORT_LIST_CAP)
    return [_line(view) for view, _shown in
            _views(conn, patient_id, ids, now or _now(), delay_hours())]


def _own_specimen(conn, patient_id: str, accession: Any) -> str:
    """The accession, checked, when it is one of this patient's samples;
    otherwise the same 404 as an unknown sample. A sample of another patient
    raises with other_patient_id for the denial audit."""
    try:
        accession = los.checked_accession(accession)
    except los.LabOrderError:
        raise _not_found()
    if not _tables_present(conn):
        raise _not_found()
    row = conn.execute("SELECT patient_id FROM lab_specimens WHERE id = ?",
                       (accession,)).fetchone()
    if row is None:
        raise _not_found()
    if row["patient_id"] != patient_id:
        raise _not_found(other_patient_id=row["patient_id"], accession=accession)
    return accession


def patient_report(conn, patient_id: str, accession: Any, *,
                   now: Optional[datetime] = None) -> tuple[dict, list[int]]:
    """One of the patient's own reports as they see it, with the ids of the
    values shown. Raises PortalReportError 'not_found' (404) for an unknown
    sample, another patient's sample, or one with nothing in the portal yet."""
    accession = _own_specimen(conn, patient_id, accession)
    views = _views(conn, patient_id, [accession], now or _now(), delay_hours())
    if not views:
        raise _not_found(accession=accession)
    return views[0]


# ════════════════════════════════════════════════════════════════════════════
#  The staff view and the doctor's release
# ════════════════════════════════════════════════════════════════════════════

def _staff_state(report: dict, versions_by_item: dict[int, list[dict]],
                 releases: list[dict], now: datetime, delay: Optional[int]) -> dict:
    released = _released_ids(releases)
    live_ids, shown, held, needs_doctor, due_times = [], [], [], False, []
    for entry in report["items"]:
        live = entry.get("result")
        if entry["state"] != "reported" or live is None:
            continue
        live_id = int(live["id"])
        live_ids.append(live_id)
        version = next((v for v in versions_by_item.get(int(entry["item_id"]), [])
                        if int(v["id"]) == live_id), None)
        if version is None:
            continue
        if version_out(version, released, now, delay):
            shown.append(live_id)
            continue
        held.append(live_id)
        due = delay_due_at(version, delay)
        if due is None:
            needs_doctor = True
        else:
            due_times.append(due)
    if not live_ids:
        state = "nothing_released"
    elif not held:
        state = "shown"
    elif shown:
        state = "partly_shown"
    elif needs_doctor:
        state = "needs_release"
    else:
        state = "waiting"
    return {
        "state": state,
        "state_text": STAFF_STATE_WORDS[state],
        "live_result_ids": sorted(live_ids),
        "shown_result_ids": sorted(shown),
        "held_result_ids": sorted(held),
        "needs_doctor_release": needs_doctor,
        "shown_by_delay_from": (format_utc(max(due_times)) if due_times and not needs_doctor
                                else None),
        "releases": releases,
    }


def portal_states(conn, patient_id: str, *, now: Optional[datetime] = None) -> list[dict]:
    """Every laboratory report of one patient with what of it the patient's
    portal shows, newest collection first. For the ward: the caller has
    already checked the patient is in scope (require_patient_access)."""
    if not patient_id or not _tables_present(conn):
        return []
    now = now or _now()
    delay = delay_hours()
    ids = lrep._released_specimen_ids(conn, "r.patient_id = ?", (patient_id,),
                                      lrep.REPORT_LIST_CAP)
    if not ids:
        return []
    reports = [r for r in lrep._reports(conn, ids) if r["patient"]["id"] == patient_id]
    accessions = [r["accession"] for r in reports]
    versions = _versions(conn, accessions)
    releases = _releases(conn, accessions)
    out = []
    for report in reports:
        out.append({
            "accession": report["accession"],
            "specimen_type": report["specimen"]["type"],
            "collected_at": report["specimen"]["collected_at"],
            "last_reported_at": report["last_reported_at"],
            "status_text": report["status_text"],
            "tests": [i["test"]["short_name"] for i in report["items"]],
            "portal": _staff_state(report, versions, releases.get(report["accession"], []),
                                   now, delay),
        })
    return out


def parse_seen_ids(value: Any) -> list[int]:
    """The result ids the doctor's screen showed: a non-empty list of whole
    numbers, at most SEEN_IDS_MAX. Raises PortalReportError (422) otherwise."""
    if not isinstance(value, list) or not value or len(value) > SEEN_IDS_MAX:
        raise PortalReportError(
            "seen_result_ids_required",
            "Name the results on the report you read (seen_result_ids).", status=422)
    ids = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise PortalReportError(
                "seen_result_ids_required",
                "Name the results on the report you read (seen_result_ids).", status=422)
        ids.append(item)
    return sorted(set(ids))


def release_in_txn(conn, patient_id: str, accession: Any, *, seen_result_ids: Any,
                   actor: dict, now: Optional[datetime] = None,
                   request_meta: Optional[dict] = None) -> dict:
    """A doctor releases a report, as they read it, to the patient's portal.

    Runs inside the caller's IMMEDIATE transaction, so no result can be
    released, amended or withdrawn between the check and the write. The
    results the doctor read must be exactly the report's released results now,
    else 409 report_changed and nothing is written (the answer carries no
    value). A report already fully released answers 'already' and writes
    nothing. Otherwise one 'portal_released' event names the versions it
    covers, with a fail-closed clinical_access audit row in the same
    transaction."""
    seen = parse_seen_ids(seen_result_ids)
    try:
        accession = los.checked_accession(accession)
    except los.LabOrderError as err:
        raise PortalReportError(err.code, err.message, status=err.status)
    if not _tables_present(conn):
        raise _not_found()
    row = conn.execute("SELECT patient_id FROM lab_specimens WHERE id = ?",
                       (accession,)).fetchone()
    if row is None or row["patient_id"] != patient_id:
        raise _not_found()
    versions = _versions(conn, [accession])
    live = [v for rows in versions.values() for v in rows if v["status"] == "released"]
    if not live:
        raise PortalReportError(
            "not_reported", "No result on this sample has been reported yet, so there is "
            "nothing to release.", status=409)
    live_ids = sorted(int(v["id"]) for v in live)
    if seen != live_ids:
        raise PortalReportError(
            "report_changed", "The report changed since you opened it. Open it again "
            "and read it before releasing it to the patient.", status=409)
    releases = _releases(conn, [accession]).get(accession, [])
    new = sorted(set(live_ids) - _released_ids(releases))
    if not new:
        return {"accession": accession, "released_result_ids": [], "already": True}
    at = format_utc(now or _now())
    actor = actor or {}
    critical = sorted(int(v["id"]) for v in live if int(v["id"]) in new and is_critical(v))
    los._specimen_event(
        conn, accession, patient_id, PORTAL_RELEASE_EVENT,
        actor_id=actor.get("id"), actor_role=actor.get("role"), now=at,
        detail="Released to the patient portal by a doctor",
        metadata={"result_ids": new, "seen_result_ids": seen,
                  "critical_result_ids": critical})
    meta = dict(request_meta or {})
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_access",
        action_type="lab_report_portal_released",
        outcome="success",
        db=conn,
        summary_message=f"Laboratory report {accession} released to the patient portal",
        actor_user_id=actor.get("id"),
        actor_role=actor.get("role"),
        patient_id=patient_id,
        target_type="lab_report",
        target_id=accession,
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata={"result_ids": new, "critical_result_ids": critical,
                  "delay_hours": delay_hours()},
    )
    return {"accession": accession, "released_result_ids": new, "already": False,
            "released_at": at}
