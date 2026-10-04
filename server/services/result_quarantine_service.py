"""The unmatched-results queue (lis-19, W109).

WHAT IT IS. An interfaced result (an HL7, JSON or FHIR feed) whose patient
identifier matches no patient used to be stored with no patient at all, where
no chart, ward pack or Tier 1 lane ever shows it and nobody is asked to look.
With ORB_RESULT_QUARANTINE on (default OFF), services/ingestion_service.py
writes such a result into result_quarantine (migration 60) instead, and
laboratory staff either attach it to the right patient or discard it.

RESULTS ONLY. A bedside monitor's lane (and Orb's other device lanes in
services/vitals_source) sends what is not a vital sign, such as mean arterial
pressure, ST level or a PVC count, at monitor cadence. Those device metrics
never enter the queue, nor does anything coded in a monitor's own
nomenclature over any connection (takes_result): they are stored with no
patient exactly as before, so they cannot bury the laboratory's results.

WHO MAY DECIDE (ruling LIS-R17, strictest option, held for Samuel to confirm
before go-live). Attaching a result to a patient is a wrong-patient decision:
the result then feeds Tier 1, the chart and the ward pack. So only a holder of
ingest.reconcile may attach or discard, and roles.py grants that to the
laboratory roles alone (never to a department-scoped role, never to admin).
The administrator (ingest.monitor) sees the counts and a list without the
patient identifiers or values. A site without laboratory staff keeps the
switch off; the compliance report fails while it is on with nobody able to
work the queue.

THE TWO-IDENTIFIER CHECK. To attach, the reconciler names the patient by
hospital number and types the identifiers they confirmed. Three kinds exist:
hospital number, name and date of birth. Each one typed must match the
patient's record AND what the sending system put in the message (PID-3, PID-5,
PID-7), and at least two kinds must match both. A kind typed that contradicts
either side refuses the attach outright. The hospital number finds the
patient, so it counts only when the message carries that same number; a
message number that matches nobody (the reason the result is here) neither
counts nor refuses. The answer is per kind, match or no match, and never shows
the patient's stored name or date of birth. Every refusal is audited.

A NUMBER IN THE MESSAGE THAT NOW NAMES SOMEONE ELSE (strictest option, held
for Samuel to confirm before go-live). The result is here because its number
matched nobody when it arrived, but a patient registered since may carry it.
If any hospital number in the message now names a patient other than the one
chosen, the attach is refused outright, whatever the name and date of birth
say (two people can share a name and an approximated date of birth such as
1 January): the hospital number check reads names_another_patient, the
refusal says so, and the refusal is audited on the same transaction.

A NAME AND DATE OF BIRTH THAT NAME TWO RECORDS (strictest option, held for
Samuel to confirm before go-live). For the queue's usual result, an outside
laboratory's number, name and date of birth are the two identifiers that
confirm. When another patient record has the same name and the same date of
birth (or no usable date of birth at all), the pair cannot tell the two
apart: both kinds read shared_with_another_patient and the attach is refused
(name_and_birth_date_shared), department scope not applied. A hospital number
in the message that names the chosen patient still confirms with one of them.

A RESULT ATTACHED AFTER ITS OWN CORRECTION (strictest option, held for Samuel
to confirm before go-live). A final can wait here while a correction of it,
sent later, is attached first or reaches the chart at receipt. Written as it
stands, the stale final would be the newest row and the ward pack would show
only it. So before writing, services/ingestion_service
.later_correction_for_attach looks for a live correction of the same test and
order from a later message: when the picture is exactly what receipt in order
would have made, the result is stored already superseded by that correction
(fail-closed audit); otherwise the attach is refused (later_correction_on_chart)
and the result is discarded or taken up with the laboratory.

ONE TRANSACTION. Attach writes the observations row (through the same writer
the ingestion lane uses, so a correction supersedes the result it corrects
exactly as it would have at receipt, lis-20), marks the queue row attached and
writes a fail-closed clinical_access audit row, all on one IMMEDIATE
transaction. If the audit row cannot be written, nothing is attached. Discard
does the same with a required reason. A decided row never changes again
(a trigger refuses it).

SWEEP. Results stored with no patient before the switch went on are copied
into the queue once, at boot with the switch on (idempotent: each row at most
once, by source_observation_id), a device lane's metrics excepted. Attaching one of those sets the patient on
the row that is already there; it is never copied a second time.

This module never reads the observations table itself: the writes go through
services/ingestion_service.py and the reads through services/lab_observations.py,
as tests/test_lab_observation_readers.py requires of every lane.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

import config
from database import db_session
from services.audit_service import log_audit_event
from services.clinical_time import utc_iso_now

logger = logging.getLogger("orb.result_quarantine")

FEATURE_ID = "result_quarantine"
LANES = ("ingestion", "analyser")
STATUSES = ("open", "attached", "discarded")
IDENTIFIER_KINDS = ("hospital_number", "name", "date_of_birth")
#: How many identifier kinds must match both the message and the record.
REQUIRED_MATCHES = 2
MAX_REASON_CHARS = 500
MAX_LIST = 200
#: An open result older than this turns the compliance check amber.
STALE_OPEN_HOURS = 24

# Outcomes of one identifier check, as the attach answer reports them.
MATCH = "match"
NO_MATCH = "no_match"
NOT_IN_MESSAGE = "not_in_message"
DIFFERS = "differs_in_message"
NOT_GIVEN = "not_given"
#: A hospital number in the message now names a patient other than the one
#: chosen. Refuses the attach outright.
NAMES_ANOTHER = "names_another_patient"
#: Name and date of birth both match, but another patient record could be
#: named by them too, and no hospital number in the message confirms the
#: chosen one. Set on both kinds; refuses the attach.
SHARED = "shared_with_another_patient"

# Why an attach was refused, as the attach answer reports it.
REFUSED_IDENTITY = "identity_not_confirmed"
REFUSED_OTHER_PATIENT = "message_names_another_patient"
REFUSED_SHARED_IDENTITY = "name_and_birth_date_shared"
REFUSED_LATER_CORRECTION = "later_correction_on_chart"


class QuarantineError(Exception):
    """A refusal the route turns into an HTTP answer."""

    status_code = 400

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_detail(self) -> dict:
        return {"error": self.code, "message": self.message, **self.detail}


class QuarantineNotFound(QuarantineError):
    status_code = 404


class QuarantineConflict(QuarantineError):
    status_code = 409


class QuarantineInvalid(QuarantineError):
    status_code = 422


# ════════════════════════════════════════════════════════════════════════════
#  Switch and reachability
# ════════════════════════════════════════════════════════════════════════════

def queue_on() -> bool:
    """ORB_RESULT_QUARANTINE, read at call time."""
    return bool(config.feature_enabled(FEATURE_ID))


def queue_reachable(db) -> bool:
    """Whether the queue's screens and routes answer at all.

    On while the switch is on, while the laboratory module is not off (the
    analyser lane always uses the queue, lis-24), and while any row remains,
    so switching it off never hides a result nobody has decided on (the same
    drain rule as ruling LIS-R21). Off, with nothing in it, it answers 404.
    """
    if queue_on():
        return True
    try:
        if config.module_state("lab") != "off":
            return True
    except Exception:  # pragma: no cover - an unknown module reads as off
        pass
    try:
        return db.execute("SELECT 1 FROM result_quarantine LIMIT 1").fetchone() is not None
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
#  Writing a result into the queue (called by the ingestion lane)
# ════════════════════════════════════════════════════════════════════════════

def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@lru_cache(maxsize=1)
def device_source_labels() -> frozenset[str]:
    """Lane labels of Orb's own device and charting lanes
    (services/vitals_source: a bedside monitor, a typed reading, the
    simulator, a value read out of a note), lower case.

    Read from the registry of every label Orb writes (vitals_source.base,
    pinned by tests/test_vitals_source.py): each label the registry classifies
    as anything but UNKNOWN. A generic connection's label (ingested_hl7,
    ingested_json, ingested_fhir, csv_replay) is UNKNOWN there, so it stays a
    result feed."""
    from services.vitals_source import base
    return frozenset(
        label.strip().lower() for label in base._LABEL_KIND
        if base.classify_source_label(label) is not base.SourceKind.UNKNOWN)


@lru_cache(maxsize=1)
def device_code_systems() -> frozenset[str]:
    """OBX-3.3 coding systems only a bedside monitor writes (IEEE 11073 MDC
    and the vendor systems in vitals_source/vendor_profiles.py), upper case."""
    from services.vitals_source import vendor_profiles
    return frozenset(
        system.strip().upper() for profile in vendor_profiles.all_profiles()
        for system in profile.coding_systems if system and system.strip())


def takes_result(source_label: Any, code_system: Any) -> bool:
    """Whether the queue takes an unmatched observation (lis-19).

    The queue is for results from a result feed. A bedside monitor sends what
    is not a vital sign (mean arterial pressure, ST level, a PVC count) at
    monitor cadence; queued, those device metrics would bury the laboratory's
    results under rows nobody can reach. So an observation from a device lane
    (device_source_labels), or coded in a monitor's own nomenclature over any
    connection (device_code_systems), keeps today's behaviour: stored with no
    patient. Anything else, an unknown lane included, is a result."""
    if str(source_label or "").strip().lower() in device_source_labels():
        return False
    if str(code_system or "").strip().upper() in device_code_systems():
        return False
    return True


def enqueue_in_txn(db, evt, *, event_id: Optional[int], source_label: str,
                   lane: str = "ingestion", reason: str = "unmatched_patient") -> int:
    """Insert one unmatched result on the caller's open transaction and return
    its queue id. `evt` is a services.ingestion_adapters.CanonicalEvent."""
    if lane not in LANES:
        raise ValueError(f"unknown quarantine lane {lane!r}")
    payload = evt.to_dict()
    payload["source_label"] = source_label
    identifiers = [i for i in (payload.get("patient_identifiers") or []) if _clean(i)]
    raw_id = _clean(evt.patient_identifier) or (identifiers[0] if identifiers else None)
    cursor = db.execute(
        """INSERT INTO result_quarantine
           (lane, reason, payload_json, source, ingestion_event_id,
            raw_patient_identifier, raw_patient_name, raw_patient_dob,
            observation_code, observation_name, observed_at, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (lane, reason, json.dumps(payload, default=str), source_label, event_id,
         raw_id, _clean(evt.patient_name), _clean(evt.patient_dob),
         _clean(evt.observation_code), _clean(evt.observation_name) or "Unknown",
         _clean(evt.observed_at), utc_iso_now()),
    )
    return int(cursor.lastrowid)


# ════════════════════════════════════════════════════════════════════════════
#  The one-time sweep of results stored with no patient
# ════════════════════════════════════════════════════════════════════════════

def _identity_from_raw_event(db, event_id: Optional[int]) -> dict:
    """What the original message said about the patient, for a swept row."""
    out = {"identifiers": [], "name": None, "dob": None, "sex": None}
    if not event_id:
        return out
    row = db.execute(
        "SELECT patient_identifier_raw, raw_payload FROM ingestion_events WHERE id = ?",
        (event_id,)).fetchone()
    if row is None:
        return out
    raw = (row["raw_payload"] or "").strip()
    try:
        if raw.startswith("MSH|"):
            from services.ingestion_adapters import hl7_patient_identity
            from services.vitals_source import hl7v2
            out = hl7_patient_identity(hl7v2.parse(raw).first("PID"))
        elif raw.startswith("{"):
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("patient_id"):
                out["identifiers"] = [str(data["patient_id"])]
                out["name"] = _clean(data.get("patient_name"))
                dob = _clean(data.get("date_of_birth"))
                out["dob"] = _date_only(dob)
    except Exception:
        logger.info("Swept result: the raw message %s could not be read for identity", event_id)
    if not out["identifiers"] and _clean(row["patient_identifier_raw"]):
        out["identifiers"] = [_clean(row["patient_identifier_raw"])]
    return out


def sweep_unlinked(*, batch: int = 500) -> int:
    """Copy every result stored with no patient into the queue, once.

    Idempotent: a row already copied (any status) is skipped, and the unique
    index on source_observation_id makes a concurrent second sweep a no-op.
    A device lane's metrics are never copied (takes_result): they stay where
    they are. Runs only with the switch on. Returns how many rows it copied.
    """
    if not queue_on():
        return 0
    from services import ingestion_service
    copied = 0
    identities: dict = {}  # one parse per raw message, however many results it held
    while True:
        copied_now = 0
        with db_session(immediate=True) as db:
            rows = ingestion_service.unlinked_observations_not_queued(
                db, limit=batch, exclude_sources=device_source_labels(),
                exclude_code_systems=device_code_systems())
            for ob in rows:
                event_id = ob["source_event_id"]
                if event_id not in identities:
                    identities[event_id] = _identity_from_raw_event(db, event_id)
                identity = identities[event_id]
                payload = {
                    "patient_identifier": identity["identifiers"][0] if identity["identifiers"] else "",
                    "patient_identifiers": identity["identifiers"],
                    "patient_name": identity["name"],
                    "patient_dob": identity["dob"],
                    "patient_sex": identity["sex"],
                    "observation_name": ob["observation_name"],
                    "observation_code": ob["observation_code"],
                    "observation_code_system": ob["observation_code_system"],
                    "value": ob["value_text"] if ob["value_text"] is not None else (
                        "" if ob["value_numeric"] is None else str(ob["value_numeric"])),
                    "value_numeric": ob["value_numeric"],
                    "unit": ob["unit"],
                    "reference_range": ob["reference_range"],
                    "abnormal_flag": ob["abnormal_flag"],
                    "status": ob["status"],
                    "observed_at": ob["observed_at"],
                    "panel_name": ob["panel_name"],
                    "obr_group_id": ob["obr_group_id"],
                    "source_label": ob["source"],
                    "swept_from_observation": ob["id"],
                }
                cur = db.execute(
                    """INSERT OR IGNORE INTO result_quarantine
                       (lane, reason, payload_json, source, ingestion_event_id,
                        source_observation_id, raw_patient_identifier, raw_patient_name,
                        raw_patient_dob, observation_code, observation_name, observed_at,
                        status, created_at)
                       VALUES ('ingestion', 'unmatched_patient', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               'open', ?)""",
                    (json.dumps(payload, default=str), ob["source"], ob["source_event_id"],
                     ob["id"], payload["patient_identifier"] or None, identity["name"],
                     identity["dob"], ob["observation_code"], ob["observation_name"],
                     ob["observed_at"], utc_iso_now()),
                )
                copied_now += cur.rowcount or 0
        copied += copied_now
        # Stop on a short batch, and on a batch that copied nothing (a row
        # that could not be copied must not loop the sweep forever).
        if len(rows) < batch or not copied_now:
            break
    if copied:
        logger.warning("Unmatched results queue: %d result(s) stored with no patient before the "
                       "queue was switched on are now in the queue for laboratory staff", copied)
    return copied


# ════════════════════════════════════════════════════════════════════════════
#  Who can work the queue
# ════════════════════════════════════════════════════════════════════════════

def reconciler_roles() -> list[str]:
    """Roles holding ingest.reconcile whose part of Orb is switched on."""
    import roles as roles_mod
    from permissions import INGEST_RECONCILE, ROLE_PERMISSIONS
    out = []
    for role, perms in ROLE_PERMISSIONS.items():
        if role == "clinician" or INGEST_RECONCILE not in perms:
            continue
        feature = roles_mod.ROLE_FEATURE.get(role)
        if feature and not config.switch_enabled(feature):
            continue
        out.append(role)
    return sorted(out)


def active_reconciler_count(db) -> int:
    """Active accounts that can attach or discard right now."""
    roles = reconciler_roles()
    if not roles:
        return 0
    marks = ", ".join("?" for _ in roles)
    return int(db.execute(
        f"SELECT COUNT(*) FROM users WHERE role IN ({marks}) AND COALESCE(is_active, 1) = 1",
        tuple(roles)).fetchone()[0])


def _counts(db) -> dict:
    out = {s: 0 for s in STATUSES}
    for row in db.execute("SELECT status, COUNT(*) AS n FROM result_quarantine GROUP BY status"):
        out[row["status"]] = int(row["n"])
    oldest = db.execute(
        "SELECT MIN(created_at) FROM result_quarantine WHERE status = 'open'").fetchone()[0]
    out["oldest_open_at"] = oldest
    return out


def boot_check() -> dict:
    """At boot with the switch on: sweep, then say loudly when nobody can work
    the queue. Blocking; run through run_db."""
    summary = {"on": queue_on(), "swept": 0, "reconcilers": None}
    if not summary["on"]:
        return summary
    try:
        summary["swept"] = sweep_unlinked()
    except Exception:
        logger.error("Unmatched results queue: the sweep of results with no patient failed",
                     exc_info=True)
    try:
        with db_session() as db:
            summary["reconcilers"] = active_reconciler_count(db)
    except Exception:
        logger.error("Unmatched results queue: could not count who can work it", exc_info=True)
        return summary
    if not summary["reconcilers"]:
        logger.warning(
            "ORB_RESULT_QUARANTINE is on but no active account can attach or discard an "
            "unmatched result (ingest.reconcile is held by laboratory staff only, ruling "
            "LIS-R17, and needs ORB_LAB on). Results that match no patient will wait unseen "
            "by any chart until a laboratory account exists. Switch the queue off or create one.")
    return summary


async def _boot_task() -> None:
    """The one-shot boot hook, run by services/lab_module.startup in every
    module state (it is not a laboratory-only check)."""
    from database import run_db
    try:
        await run_db(boot_check)
    except Exception:
        logger.error("Unmatched results queue boot check failed", exc_info=True)


def _register_boot_task() -> None:
    try:
        from services import lab_module
        lab_module.register_loop("result-quarantine-boot", _boot_task,
                                 states=tuple(config.MODULE_STATES))
    except Exception:  # pragma: no cover - boot must not fail on this
        logger.warning("Unmatched results queue: boot check not registered", exc_info=True)


_register_boot_task()


def compliance_control() -> Optional[dict]:
    """The compliance report's line for the queue, or None when the switch is
    off and nothing is left in it (a site that never used it sees no line)."""
    on = queue_on()
    try:
        with db_session() as db:
            counts = _counts(db)
            reconcilers = active_reconciler_count(db)
    except Exception:
        if not on:
            return None
        return {
            "id": "result_quarantine", "name": "Unmatched laboratory results",
            "status": "unknown",
            "evidence": "The unmatched results queue is switched on, but Orb could not read it.",
            "remediation": "Contact Orb support: the unmatched results queue could not be read.",
            "settings": ["ORB_RESULT_QUARANTINE"],
        }
    open_n = counts["open"]
    if not on and not open_n:
        return None
    control = {"id": "result_quarantine", "name": "Unmatched laboratory results",
               "settings": ["ORB_RESULT_QUARANTINE"]}
    if on and not reconcilers:
        control.update(
            status="fail",
            evidence=(
                f"Interfaced results that match no patient are held for laboratory staff, "
                f"but no active account can attach or discard them. {open_n} result(s) are "
                f"waiting, and none of them reaches any patient's chart until someone does."),
            remediation=(
                "Create a laboratory technician or pathologist account (the laboratory module "
                "must be on), or switch the queue off so unmatched results are stored as before."),
        )
        return control
    if not on:
        control.update(
            status="warn",
            evidence=(f"The unmatched results queue is switched off, and {open_n} result(s) in it "
                      f"have not been attached or discarded."),
            remediation="Have laboratory staff attach or discard each one on the Laboratory screen.",
        )
        return control
    oldest = _parse_instant(counts.get("oldest_open_at"))
    stale = bool(oldest and datetime.now(timezone.utc) - oldest > timedelta(hours=STALE_OPEN_HOURS))
    if stale:
        control.update(
            status="warn",
            evidence=(f"{open_n} unmatched result(s) are waiting, the oldest for more than "
                      f"{STALE_OPEN_HOURS} hours. None of them is on any patient's chart."),
            remediation="Ask the laboratory to work the unmatched results list today.",
        )
    else:
        control.update(
            status="pass",
            evidence=(f"Interfaced results that match no patient are held for laboratory staff "
                      f"({reconcilers} account(s) can decide). {open_n} waiting now."),
            remediation="Check the list each day; an unmatched result is a result no chart shows.",
        )
    return control


def _parse_instant(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ════════════════════════════════════════════════════════════════════════════
#  The list
# ════════════════════════════════════════════════════════════════════════════

def _payload(row) -> dict:
    try:
        data = json.loads(row["payload_json"] or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


#: Labels the ingestion routes give a message when the sender names nothing;
#: they are machine words, not a sender, and are never shown.
_DEFAULT_SOURCE_NAMES = frozenset({"hl7_sender", "dev_replay"})


def _sender_words(hl7_sender: Any, source_name: Any) -> Optional[str]:
    """Who sent the result, for the lists: the sending application and
    facility the message names (MSH-3, MSH-4), else the name the connection
    gave, else None."""
    named = _clean(hl7_sender)
    if named:
        return ", ".join(p for p in named.split("^") if p.strip())
    label = _clean(source_name)
    return None if not label or label in _DEFAULT_SOURCE_NAMES else label


def _display_value(payload: dict) -> Optional[str]:
    text = payload.get("value")
    if text is not None and str(text).strip():
        return str(text)
    num = payload.get("value_numeric")
    return None if num is None else str(num)


def list_queue(*, status: str = "open", limit: int = 100, full: bool) -> dict:
    """The queue, newest first, with its counts.

    `full` is for a holder of ingest.reconcile: the identifiers the sender put
    in the message and the value. Without it (the administrator's list) each
    row carries when it arrived, the sender, the test name and its state only.
    """
    if status not in STATUSES + ("all",):
        raise QuarantineInvalid("bad_status", "Status must be open, attached, discarded or all.")
    limit = max(1, min(int(limit or 100), MAX_LIST))
    where, params = ("", ()) if status == "all" else ("WHERE q.status = ?", (status,))
    with db_session() as db:
        if not queue_reachable(db):
            raise QuarantineNotFound(
                "feature_disabled", "The unmatched results queue is switched off on this site.",
                feature=FEATURE_ID)
        rows = db.execute(
            f"""SELECT q.*, e.source_name AS sender, u.name AS resolved_by_name
                FROM result_quarantine q
                LEFT JOIN ingestion_events e ON e.id = q.ingestion_event_id
                LEFT JOIN users u ON u.id = q.resolved_by
                {where}
                ORDER BY q.created_at DESC, q.id DESC
                LIMIT ?""",
            params + (limit,)).fetchall()
        counts = _counts(db)
    items = []
    for row in rows:
        payload = _payload(row)
        item = {
            "id": row["id"],
            "created_at": row["created_at"],
            "lane": row["lane"],
            "status": row["status"],
            "source": row["source"],
            "sender": _sender_words(payload.get("hl7_sender"), row["sender"]),
            "observation_name": row["observation_name"],
            "observed_at": row["observed_at"],
            "swept": row["source_observation_id"] is not None,
            "resolved_at": row["resolved_at"],
        }
        if full:
            identifiers = [i for i in (payload.get("patient_identifiers") or []) if _clean(i)]
            if not identifiers and _clean(row["raw_patient_identifier"]):
                identifiers = [_clean(row["raw_patient_identifier"])]
            item.update({
                # Whether the message names the patient in at least two ways.
                # Fewer can never pass the two-identifier check, so the screen
                # offers only Discard and says why.
                "identifiable": message_identity_kinds(_message_identity(row, payload))
                >= REQUIRED_MATCHES,
                "reason": row["reason"],
                "observation_code": row["observation_code"],
                "value": _display_value(payload),
                "unit": payload.get("unit"),
                "abnormal_flag": payload.get("abnormal_flag"),
                "reference_range": payload.get("reference_range"),
                "result_status": payload.get("status"),
                "message_identifiers": identifiers,
                "message_name": row["raw_patient_name"],
                "message_date_of_birth": row["raw_patient_dob"],
                "message_sex": payload.get("patient_sex"),
                "resolved_by_name": row["resolved_by_name"],
                "resolution_reason": row["resolution_reason"],
                "attached_patient_id": row["attached_patient_id"],
            })
        items.append(item)
    return {"enabled": queue_on(), "view": "reconcile" if full else "monitor",
            "status": status, "counts": counts, "items": items}


# ════════════════════════════════════════════════════════════════════════════
#  The two-identifier check
# ════════════════════════════════════════════════════════════════════════════

def _number(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _name_tokens(value: Any) -> list[str]:
    return sorted(re.findall(r"[0-9a-z]+", str(value or "").lower()))


def _date_only(value: Any) -> Optional[str]:
    match = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)),
                        int(match.group(3))).strftime("%Y-%m-%d")
    except ValueError:
        return None


def identity_checks(message: dict, record: Optional[dict], typed: dict, *,
                    message_names_another: bool = False,
                    name_and_dob_shared: bool = False) -> dict[str, str]:
    """One outcome per identifier kind. Pure: no database.

    message: {"identifiers": [...], "name": str|None, "dob": "YYYY-MM-DD"|None}
    record:  the patient row ({"id", "mrn", "name", "date_of_birth"}) or None
    typed:   {"hospital_number", "name", "date_of_birth"} as the reconciler typed them
    message_names_another: a hospital number in the message now names a
             patient other than `record` (the caller looked it up); the
             hospital number check then reads names_another_patient
    name_and_dob_shared: another patient record could be named by the
             message's name and date of birth (the caller looked it up,
             _same_name_and_birth_date). When name and date of birth both
             match and the hospital number does not, both then read
             shared_with_another_patient: the pair cannot tell the chosen
             patient from the other record, so it confirms nobody
    """
    out: dict[str, str] = {}

    number = _number(typed.get("hospital_number"))
    if not number:
        out["hospital_number"] = NOT_GIVEN
    elif record is None or number not in {_number(record.get("id")), _number(record.get("mrn"))}:
        out["hospital_number"] = NO_MATCH
    else:
        in_message = {_number(i) for i in (message.get("identifiers") or []) if _number(i)}
        if not in_message:
            out["hospital_number"] = NOT_IN_MESSAGE
        elif number in in_message:
            out["hospital_number"] = MATCH
        else:
            # The message's own number matched nobody when it arrived, which
            # is why the result is here. Still naming nobody, it is not
            # evidence either way (the caller checks that it still names
            # nobody else: message_names_another).
            out["hospital_number"] = DIFFERS
    if message_names_another and record is not None:
        out["hospital_number"] = NAMES_ANOTHER

    name = _name_tokens(typed.get("name"))
    if not name:
        out["name"] = NOT_GIVEN
    elif record is None or name != _name_tokens(record.get("name")):
        out["name"] = NO_MATCH
    elif not _name_tokens(message.get("name")):
        out["name"] = NOT_IN_MESSAGE
    else:
        out["name"] = MATCH if _name_tokens(message.get("name")) == name else NO_MATCH

    raw_dob = typed.get("date_of_birth")
    dob = _date_only(raw_dob)
    if not _clean(raw_dob):
        out["date_of_birth"] = NOT_GIVEN
    elif dob is None or record is None or _date_only(record.get("date_of_birth")) != dob:
        out["date_of_birth"] = NO_MATCH
    elif not _date_only(message.get("dob")):
        out["date_of_birth"] = NOT_IN_MESSAGE
    else:
        out["date_of_birth"] = MATCH if _date_only(message.get("dob")) == dob else NO_MATCH

    if (name_and_dob_shared and out["hospital_number"] != MATCH
            and out["name"] == MATCH and out["date_of_birth"] == MATCH):
        out["name"] = SHARED
        out["date_of_birth"] = SHARED
    return out


def message_identity_kinds(message: dict) -> int:
    """How many identifier kinds the message carries at all (a number, a name,
    a real date of birth). Pure. Below REQUIRED_MATCHES no attach can pass."""
    return sum((
        any(_number(i) for i in (message.get("identifiers") or [])),
        bool(_name_tokens(message.get("name"))),
        _date_only(message.get("dob")) is not None,
    ))


def identity_confirmed(checks: dict[str, str]) -> bool:
    """At least two kinds match both sides, nothing typed contradicts, no
    number in the message names somebody else, and the name and date of birth
    are not shared with another patient record."""
    if any(v in (NO_MATCH, NAMES_ANOTHER, SHARED) for v in checks.values()):
        return False
    return sum(1 for v in checks.values() if v == MATCH) >= REQUIRED_MATCHES


def _message_identity(row, payload: dict) -> dict:
    identifiers = [i for i in (payload.get("patient_identifiers") or []) if _clean(i)]
    if _clean(row["raw_patient_identifier"]) and _clean(row["raw_patient_identifier"]) not in identifiers:
        identifiers.append(_clean(row["raw_patient_identifier"]))
    return {"identifiers": identifiers,
            "name": row["raw_patient_name"] or payload.get("patient_name"),
            "dob": row["raw_patient_dob"] or payload.get("patient_dob")}


def _find_patient(db, hospital_number: str) -> Optional[dict]:
    """The patient a typed hospital number names: Orb id, then hospital number,
    then hospital number ignoring case (the order the ingestion lane resolves
    one in). None when it names nobody.

    patients.mrn is unique only as typed, so 'MRN-1' and 'mrn-1' can both
    exist. Where the ingestion lane takes the first such row, an attach is a
    person's wrong-patient decision, so a number that names two patients when
    case is ignored names nobody here and the attach is refused."""
    number = (hospital_number or "").strip()
    if not number:
        return None
    for sql in ("SELECT id, mrn, name, date_of_birth FROM patients WHERE id = ?",
                "SELECT id, mrn, name, date_of_birth FROM patients WHERE mrn = ?"):
        row = db.execute(sql, (number,)).fetchone()
        if row:
            return dict(row)
    rows = db.execute(
        "SELECT id, mrn, name, date_of_birth FROM patients WHERE LOWER(mrn) = LOWER(?) LIMIT 2",
        (number,)).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def _message_names_another_patient(db, message: dict, record: dict) -> Optional[str]:
    """The id of a patient other than `record` whom a hospital number in the
    message names now, or None.

    Wider than the lookups that resolve a number (the ingestion lane and
    _find_patient): every number the message carries (each PID-3 repetition),
    matched on the Orb id or the hospital number as sent, ignoring case, and
    ignoring spaces the way the check compares numbers. A case-insensitive
    number that names two patients names another one here, so the attach is
    refused rather than guessed. Department scope is not applied: a patient
    outside the reconciler's departments is still somebody else."""
    for identifier in message.get("identifiers") or []:
        raw = _clean(identifier)
        if not raw:
            continue
        row = db.execute(
            """SELECT id FROM patients
               WHERE id != ?
                 AND (id = ? OR mrn = ? OR LOWER(mrn) = LOWER(?)
                      OR UPPER(REPLACE(id, ' ', '')) = ?
                      OR UPPER(REPLACE(COALESCE(mrn, ''), ' ', '')) = ?)
               LIMIT 1""",
            (record["id"], raw, raw, raw, _number(raw), _number(raw))).fetchone()
        if row is not None:
            return row["id"]
    return None


def _same_name_and_birth_date(db, message: dict, record: dict) -> list[str]:
    """Ids of patient records other than `record` that the message's name and
    date of birth could equally name, oldest id first.

    The same name by the check's own rule (the same words in any order,
    ignoring case and punctuation), and the same date of birth or no usable
    date of birth at all: a record whose date of birth was never taken cannot
    be told apart by one (strictest option, held for Samuel). Department scope
    is not applied: a patient outside the reconciler's departments is still
    somebody else. The typed pair needs no second look: name and date of birth
    both read match only when the typed pair equals the message's.

    Two people share a name and an approximated date of birth such as
    1 January often enough, and an outside laboratory's number names nobody
    here, so for the queue's usual result name and date of birth are all
    there is. When they name two records, they confirm neither."""
    tokens = _name_tokens(message.get("name"))
    dob = _date_only(message.get("dob"))
    if not tokens or dob is None:
        return []
    # A prefilter only: each word appears somewhere in the stored name. The
    # words are [0-9a-z]+, so they carry no LIKE wildcard.
    words = sorted(set(tokens))
    rows = db.execute(
        "SELECT id, name, date_of_birth FROM patients WHERE id != ? AND "
        + " AND ".join("LOWER(name) LIKE ?" for _ in words) + " ORDER BY id",
        (record["id"], *[f"%{w}%" for w in words])).fetchall()
    out = []
    for row in rows:
        if _name_tokens(row["name"]) != tokens:
            continue
        other_dob = _date_only(row["date_of_birth"])
        if other_dob is None or other_dob == dob:
            out.append(row["id"])
    return out


def _reason(text: Any) -> str:
    reason = (str(text) if text is not None else "").strip()
    if not reason:
        raise QuarantineInvalid("reason_required", "Give a reason.")
    if len(reason) > MAX_REASON_CHARS:
        raise QuarantineInvalid("reason_too_long",
                                f"Keep the reason under {MAX_REASON_CHARS} characters.")
    return reason


def _open_row(db, qid: int):
    row = db.execute("SELECT * FROM result_quarantine WHERE id = ?", (int(qid),)).fetchone()
    if row is None:
        raise QuarantineNotFound("not_found", "No such unmatched result.")
    if row["status"] != "open":
        raise QuarantineConflict("already_resolved",
                                 f"This result was already {row['status']}.", status=row["status"])
    return row


def _actor_fields(user: dict, request_meta: Optional[dict]) -> dict:
    meta = request_meta or {}
    return {
        "actor_user_id": (user or {}).get("id"),
        "actor_role": (user or {}).get("role"),
        "request_path": meta.get("path"),
        "http_method": meta.get("method"),
        "ip_address": meta.get("ip"),
        "user_agent": meta.get("user_agent"),
    }


# ════════════════════════════════════════════════════════════════════════════
#  Attach and discard
# ════════════════════════════════════════════════════════════════════════════

def attach(qid: int, *, hospital_number: Any, name: Any = None, date_of_birth: Any = None,
           reason: Any, user: dict, request_meta: Optional[dict] = None) -> dict:
    """Attach one open result to a patient after the two-identifier check.

    Returns {"attached": True, ...} on success, or {"attached": False,
    "refusal": ..., "checks": {...}} when the attach is refused:
      identity_not_confirmed      the identifiers do not confirm the patient;
      message_names_another_patient  a hospital number in the message now
                                  names a different patient;
      name_and_birth_date_shared  name and date of birth would confirm, but
                                  another patient record could be named by
                                  them too, and no number confirms this one;
      later_correction_on_chart   a correction of this result, sent after it,
                                  is on the chart already, and Orb cannot
                                  place this result behind it
                                  (services/ingestion_service
                                  .later_correction_for_attach).
    Every refusal is audited and committed. Raises QuarantineError otherwise.
    """
    from permissions import department_scope, patient_in_scope
    from services import ingestion_service
    from services.ingestion_adapters import canonical_event_from_dict

    reason_text = _reason(reason)
    if not _clean(hospital_number):
        raise QuarantineInvalid("hospital_number_required",
                                "Give the patient's hospital number.")
    typed = {"hospital_number": hospital_number, "name": name, "date_of_birth": date_of_birth}
    if _clean(date_of_birth) and _date_only(date_of_birth) is None:
        raise QuarantineInvalid("bad_date_of_birth", "Write the date of birth as a calendar date.")
    correlation_id = str(uuid.uuid4())
    actor = _actor_fields(user, request_meta)

    with db_session(immediate=True) as db:
        if not queue_reachable(db):
            raise QuarantineNotFound(
                "feature_disabled", "The unmatched results queue is switched off on this site.",
                feature=FEATURE_ID)
        row = _open_row(db, qid)
        payload = _payload(row)
        record = _find_patient(db, str(hospital_number))
        if record is not None and not patient_in_scope(db, record["id"], department_scope(user)):
            record = None  # out of scope reads exactly as no such patient
        message = _message_identity(row, payload)
        other_patient = (_message_names_another_patient(db, message, record)
                         if record is not None else None)
        shared_with = (_same_name_and_birth_date(db, message, record)
                       if record is not None and other_patient is None else [])
        checks = identity_checks(message, record, typed,
                                 message_names_another=other_patient is not None,
                                 name_and_dob_shared=bool(shared_with))
        if not identity_confirmed(checks):
            metadata: dict = {"checks": checks}
            if other_patient:
                refusal = REFUSED_OTHER_PATIENT
                metadata["message_patient_id"] = other_patient
                summary = ("Unmatched result not attached: a hospital number in the message now "
                           "names a different patient")
            elif SHARED in checks.values():
                refusal = REFUSED_SHARED_IDENTITY
                metadata["shared_with_patient_ids"] = shared_with
                summary = ("Unmatched result not attached: another patient record has the same "
                           "name and date of birth, and no hospital number confirms the patient")
            else:
                refusal = REFUSED_IDENTITY
                summary = "Unmatched result not attached: the identifiers did not confirm the patient"
            metadata["refusal"] = refusal
            log_audit_event(
                correlation_id=correlation_id, event_category="clinical_access",
                action_type="lab_quarantine_attach_refused", outcome="failure", db=db,
                patient_id=record["id"] if record else None,
                target_type="result_quarantine", target_id=str(row["id"]),
                summary_message=summary, metadata=metadata, **actor)
            return {"attached": False, "id": row["id"], "refusal": refusal, "checks": checks}

        patient_id = record["id"]
        actor_ref = {"user_id": actor["actor_user_id"], "role": actor["actor_role"]}
        swept = row["source_observation_id"] is not None
        evt = source_label = None
        if swept:
            late = ingestion_service.later_correction_for_unlinked(
                db, int(row["source_observation_id"]), patient_id)
        else:
            evt = canonical_event_from_dict(payload)
            source_label = row["source"] or payload.get("source_label") or "ingested_unknown"
            late = ingestion_service.later_correction_for_event(
                db, evt, patient_id, event_id=row["ingestion_event_id"],
                source_label=source_label,
                sibling_in_message=_message_has_queued_sibling(db, row, evt))
        if late["outcome"] == "refuse":
            log_audit_event(
                correlation_id=correlation_id, event_category="clinical_access",
                action_type="lab_quarantine_attach_refused", outcome="failure", db=db,
                patient_id=patient_id, target_type="result_quarantine", target_id=str(row["id"]),
                summary_message=(
                    "Unmatched result not attached: a correction of it, sent after it, is on "
                    "the patient's chart already"),
                metadata={"checks": checks, "refusal": REFUSED_LATER_CORRECTION,
                          "why": late.get("reason"), "corrections": late.get("candidates", [])},
                **actor)
            return {"attached": False, "id": row["id"], "refusal": REFUSED_LATER_CORRECTION,
                    "checks": checks}

        if swept:
            # Its message's other results are observations rows too, which the
            # correction rule reads itself.
            obs_id, superseded = ingestion_service.attach_unlinked_observation_in_txn(
                db, int(row["source_observation_id"]), patient_id,
                correlation_id=correlation_id, actor=actor_ref, later_correction=late)
        else:
            obs_id, superseded = ingestion_service.write_observation_in_txn(
                db, evt, patient_id, row["ingestion_event_id"], source_label,
                correlation_id=correlation_id, actor=actor_ref,
                sibling_in_message=_queued_sibling_result(db, row, evt),
                later_correction=late)
        superseded_by = late.get("correction_id") if late["outcome"] == "supersede" else None
        confirmation = {"confirmed": sorted(k for k, v in checks.items() if v == MATCH),
                        "checks": checks}
        cur = db.execute(
            """UPDATE result_quarantine
               SET status = 'attached', attached_patient_id = ?, attached_observation_id = ?,
                   resolved_by = ?, resolved_at = ?, resolution_reason = ?,
                   identity_confirmation_json = ?
               WHERE id = ? AND status = 'open'""",
            (patient_id, obs_id, actor["actor_user_id"], utc_iso_now(), reason_text,
             json.dumps(confirmation), row["id"]))
        if cur.rowcount != 1:  # pragma: no cover - the IMMEDIATE lock prevents it
            raise QuarantineConflict("already_resolved", "This result was decided meanwhile.")
        log_audit_event(
            correlation_id=correlation_id, event_category="clinical_access",
            action_type="lab_quarantine_attached", outcome="success", db=db,
            patient_id=patient_id, target_type="result_quarantine", target_id=str(row["id"]),
            summary_message=(f"Unmatched result {row['id']} ({row['observation_name']}) attached "
                             f"to the patient's chart as observation {obs_id}"
                             + (f", already superseded by corrected result {superseded_by}"
                                if superseded_by else "")),
            metadata={"observation_id": obs_id, "confirmed": confirmation["confirmed"],
                      "checks": checks, "reason": reason_text, "superseded": superseded,
                      "superseded_by": superseded_by, "swept": swept},
            **actor)
    return {"attached": True, "id": int(qid), "patient_id": patient_id,
            "observation_id": obs_id, "superseded": superseded,
            "superseded_by": superseded_by, "checks": checks}


def _message_has_queued_sibling(db, row, evt) -> bool:
    """Whether the queue holds another result from the same raw message (in
    any state) with the same test, OBX-4 sub-id, sender and order as this
    one, whatever either result's status. Its siblings already attached are
    observations rows, which the readers find themselves."""
    from services import ingestion_service
    from services.ingestion_adapters import canonical_event_from_dict
    if row["ingestion_event_id"] is None:
        return False
    others = db.execute(
        "SELECT payload_json FROM result_quarantine WHERE ingestion_event_id = ? AND id != ?",
        (row["ingestion_event_id"], row["id"])).fetchall()
    events = [canonical_event_from_dict(_payload(o)) for o in others]
    return ingestion_service.message_has_sibling_result(evt, events)


def _queued_sibling_result(db, row, evt) -> bool:
    """Whether the queue holds another result from the same raw message (in
    any state) with the same test, OBX-4 sub-id, sender and order as this
    correction. Its siblings that were attached already are observations rows
    and the correction rule finds those itself; the ones still here, or
    discarded, only the queue knows. Then the correction supersedes nothing
    (lis-20, services/ingestion_service.supersede_corrected_in_txn)."""
    from services import ingestion_service
    if not ingestion_service.is_external_correction(evt):
        return False
    return _message_has_queued_sibling(db, row, evt)


def discard(qid: int, *, reason: Any, user: dict, request_meta: Optional[dict] = None) -> dict:
    """Discard one open result, with a reason, audited on the same transaction."""
    reason_text = _reason(reason)
    actor = _actor_fields(user, request_meta)
    with db_session(immediate=True) as db:
        if not queue_reachable(db):
            raise QuarantineNotFound(
                "feature_disabled", "The unmatched results queue is switched off on this site.",
                feature=FEATURE_ID)
        row = _open_row(db, qid)
        cur = db.execute(
            """UPDATE result_quarantine
               SET status = 'discarded', resolved_by = ?, resolved_at = ?, resolution_reason = ?
               WHERE id = ? AND status = 'open'""",
            (actor["actor_user_id"], utc_iso_now(), reason_text, row["id"]))
        if cur.rowcount != 1:  # pragma: no cover - the IMMEDIATE lock prevents it
            raise QuarantineConflict("already_resolved", "This result was decided meanwhile.")
        log_audit_event(
            correlation_id=str(uuid.uuid4()), event_category="clinical_access",
            action_type="lab_quarantine_discarded", outcome="success", db=db,
            target_type="result_quarantine", target_id=str(row["id"]),
            summary_message=f"Unmatched result {row['id']} ({row['observation_name']}) discarded",
            metadata={"reason": reason_text, "swept": row["source_observation_id"] is not None},
            **actor)
    return {"discarded": True, "id": int(qid)}
