"""Orb Hospital OS — Ingestion Adapters

Adapter interface + concrete adapters for HL7 ORU, JSON webhook, and CSV replay.
Each adapter parses raw payloads into a list of CanonicalEvent objects.

Supported HL7 profile:
  - Single-message ORU^R01 input
  - One patient per message (PID segment)
  - Multiple OBR groups per message
  - Multiple OBX segments per OBR
  - Handles optional missing fields safely
  - Tolerates malformed segments without crashing
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum

# ════════════════════════════════════════════
#  Canonical Event Model
# ════════════════════════════════════════════


class ObservationType(StrEnum):
    # Retain legacy diagnostic/display strings while using the standard string enum.
    __str__ = Enum.__str__
    __format__ = Enum.__format__

    VITAL = "vital"
    LAB = "lab"
    SCORE = "score"
    DEVICE_METRIC = "device_metric"
    NOTE_STUB = "note_stub"
    UNKNOWN = "unknown"


class ValueType(StrEnum):
    # Retain legacy diagnostic/display strings while using the standard string enum.
    __str__ = Enum.__str__
    __format__ = Enum.__format__

    NUMERIC = "numeric"
    TEXT = "text"
    COMPOSITE = "composite"


@dataclass
class CanonicalEvent:
    """Single normalized observation from any source."""

    patient_identifier: str
    observation_name: str
    value: str  # always stored as string; value_numeric parsed separately
    observation_type: ObservationType = ObservationType.UNKNOWN
    encounter_identifier: str | None = None
    source_type: str = ""  # hl7, json, csv_replay
    source_name: str | None = None
    event_type: str = "observation"
    observation_code: str | None = None
    observation_code_system: str | None = None
    value_type: ValueType = ValueType.NUMERIC
    value_numeric: float | None = None
    unit: str | None = None
    reference_range: str | None = None
    abnormal_flag: str | None = None
    observed_at: str | None = None  # ISO 8601
    received_at: str | None = None  # ISO 8601
    status: str | None = None  # final, preliminary, corrected
    raw_segment_ref: str | None = None
    raw_event_id: int | None = None  # FK to ingestion_events.id
    correlation_id: str | None = None
    panel_name: str | None = None  # OBR-level grouping
    obr_group_id: str | None = None
    # Internal mapping result
    mapped_vital_key: str | None = None  # HR, SBP, DBP, RR, Temp, SpO2
    # ── Identity the sender put in the message (lis-19). Only the unmatched
    #    results queue uses these: laboratory staff see them, and attaching a
    #    result to a patient checks two of them against the patient's record.
    patient_identifiers: list | None = None  # every PID-3 repetition's ID
    patient_name: str | None = None  # PID-5 family, given, middle
    patient_dob: str | None = None  # PID-7 as YYYY-MM-DD
    patient_sex: str | None = None  # PID-8 as sent
    # ── Which order a result belongs to (lis-20). obr_group_id above keeps
    #    what it always held: OBR-2, or a random id when OBR-2 is empty.
    #    obr_placer is OBR-2 ONLY when the sender sent it, so the random
    #    fallback can never be matched by a correction.
    obr_placer: str | None = None  # OBR-2, sender-supplied only
    obr_filler: str | None = None  # OBR-3
    group_id_generated: bool = False  # obr_group_id was invented here
    obx_sub_id: str | None = None  # OBX-4
    hl7_sender: str | None = None  # MSH-3^MSH-4, the number's namespace

    def to_dict(self) -> dict:
        return asdict(self)


def canonical_event_from_dict(data: dict) -> CanonicalEvent:
    """Rebuild a CanonicalEvent from its to_dict() form, as the unmatched
    results queue stores it (payload_json). Keys this version does not know
    are ignored, and the two enum fields are read back from their values."""
    from dataclasses import fields as _fields

    known = {f.name for f in _fields(CanonicalEvent)}
    kwargs = {k: v for k, v in (data or {}).items() if k in known}
    for key, enum in (("observation_type", ObservationType), ("value_type", ValueType)):
        if key in kwargs and kwargs[key] is not None and not isinstance(kwargs[key], enum):
            try:
                kwargs[key] = enum(kwargs[key])
            except ValueError:
                kwargs.pop(key)
    kwargs.setdefault("patient_identifier", "")
    kwargs.setdefault("observation_name", "Unknown")
    kwargs.setdefault("value", "")
    return CanonicalEvent(**kwargs)


# ════════════════════════════════════════════
#  Code → Vital Key Mapping
# ════════════════════════════════════════════

# Maps LOINC codes and common names to Orb internal vital keys.
# Keys: HR, SBP, DBP, RR, Temp, SpO2
_VITAL_CODE_MAP: dict[str, str] = {
    # LOINC
    "8867-4": "HR",  # Heart rate
    "8480-6": "SBP",  # Systolic BP
    "8462-4": "DBP",  # Diastolic BP
    "9279-1": "RR",  # Respiratory rate
    "8310-5": "Temp",  # Body temperature
    "2708-6": "SpO2",  # Oxygen saturation
    "59408-5": "SpO2",  # SpO2 by pulse oximetry
    # Common names (case-insensitive matching done at lookup time)
    "heart rate": "HR",
    "hr": "HR",
    "pulse": "HR",
    "pulse rate": "HR",
    "systolic": "SBP",
    "systolic bp": "SBP",
    "systolic blood pressure": "SBP",
    "sbp": "SBP",
    "diastolic": "DBP",
    "diastolic bp": "DBP",
    "diastolic blood pressure": "DBP",
    "dbp": "DBP",
    "respiratory rate": "RR",
    "resp rate": "RR",
    "rr": "RR",
    "temperature": "Temp",
    "temp": "Temp",
    "body temperature": "Temp",
    "spo2": "SpO2",
    "oxygen saturation": "SpO2",
    "o2 sat": "SpO2",
}


def map_to_vital_key(code: str | None, name: str | None) -> str | None:
    """Attempt to map an observation code/name to an Orb vital key."""
    if code and code in _VITAL_CODE_MAP:
        return _VITAL_CODE_MAP[code]
    if name:
        lower = name.strip().lower()
        if lower in _VITAL_CODE_MAP:
            return _VITAL_CODE_MAP[lower]
    return None


# ════════════════════════════════════════════
#  Unit Normalization
# ════════════════════════════════════════════


def normalize_temp_to_celsius(value: float, unit: str | None) -> float:
    """Convert Fahrenheit to Celsius if needed."""
    if unit and unit.upper() in ("F", "°F", "DEG F", "[DEGF]"):
        return round((value - 32) * 5 / 9, 1)
    return value


# ════════════════════════════════════════════
#  Vital Plausibility Bounds
# ════════════════════════════════════════════

# Physiologically possible ranges per mapped vital key. A feed value outside
# these bounds (e.g. SpO2=900, HR=-5) is implausible and must NOT be silently
# fed into NEWS2/alerts — it would fabricate or suppress a critical alert. We
# reject the numeric value (return None) so the row falls back to carry-forward
# rather than poisoning the clinical picture with a sensor/parse glitch.
_VITAL_PLAUSIBLE_RANGE: dict[str, tuple[float, float]] = {
    "HR": (10.0, 300.0),  # bpm
    "SBP": (40.0, 300.0),  # mmHg
    "DBP": (10.0, 200.0),  # mmHg
    "RR": (2.0, 80.0),  # breaths/min
    "Temp": (20.0, 45.0),  # °C (post-normalization)
    "SpO2": (10.0, 100.0),  # %
}


def validate_vital_value(vital_key: str | None, value: float | None) -> float | None:
    """Return value if it is a plausible reading for the vital, else None.

    Used by every adapter before a numeric value is emitted, so an out-of-range
    feed value is dropped (and carried-forward downstream) instead of driving a
    spurious or suppressed deterioration alert.

    NON-FINITE VALUES ARE REJECTED FIRST, for ANY observation, keyed or not.
    `float("NaN")` and `float("inf")` both SUCCEED in Python, and a bedside
    monitor really does send `NaN` for a lead that has fallen off. NaN then
    passes every range check silently (`nan < 10` and `nan > 300` are both
    False), so it would reach NEWS2, where every comparison against it is also
    False — scoring a disconnected lead as a normal vital sign.
    """
    if value is None:
        return None
    try:
        if not math.isfinite(value):
            return None
    except TypeError:
        return None
    if vital_key is None:
        return value
    lo, hi = _VITAL_PLAUSIBLE_RANGE.get(vital_key, (None, None))
    if lo is None or hi is None:
        return value
    if value < lo or value > hi:
        return None
    return value


# ════════════════════════════════════════════
#  Fingerprinting
# ════════════════════════════════════════════


def compute_fingerprint(raw_payload: str) -> str:
    """SHA-256 fingerprint for deduplication."""
    return hashlib.sha256(raw_payload.encode("utf-8", errors="replace")).hexdigest()


# ════════════════════════════════════════════
#  HL7 Timestamp Parsing
# ════════════════════════════════════════════


def parse_hl7_timestamp(ts: str, *, default_timezone=None) -> str | None:
    """Parse an HL7 DTM (`yyyyMMddHHmmss[.S][+/-ZZZZ]`) into ISO 8601 UTC.

    Delegates to the real HL7 parser so the HTTP feed and the MLLP monitor feed
    can never disagree about what a device meant. The previous implementation
    truncated at the first `-`/`+`, which DISCARDED the timezone offset: a
    monitor in IST sending `...+0530` was read as 5.5 hours earlier than it
    happened, which reorders it against every other feed.

    `default_timezone` is the zone applied when a DTM carries NO offset — which
    is legal HL7 and what most bedside monitors emit, because they print local
    wall-clock time. It defaults to the appliance's configured zone
    (`ORB_HL7_DEFAULT_TZ`, else the OS zone), NOT to UTC: this hospital is at
    UTC+05:30, and reading its monitors' local time as UTC stamped every reading
    5.5 hours in the future. Pass it explicitly for a device known to be
    configured in a different zone from the appliance.
    """
    from services.vitals_source.hl7v2 import parse_timestamp

    return parse_timestamp(ts, default_tz=default_timezone)


def hl7_patient_identity(pid) -> dict:
    """The patient identity a PID segment states, for the unmatched results
    queue (lis-19): every PID-3 repetition's ID, the PID-5 name (family, given
    and middle only, never a prefix, suffix or degree), the PID-7 date of
    birth as YYYY-MM-DD when it is a real date, and PID-8 as sent. Missing
    parts are None (identifiers an empty list); nothing here raises."""
    out: dict = {"identifiers": [], "name": None, "dob": None, "sex": None}
    if pid is None:
        return out
    try:
        for rep in range(len(pid.repeats(3))):
            value = pid.field(3, repeat=rep, component=1)
            if value and value not in out["identifiers"]:
                out["identifiers"].append(value)
        family = pid.field(5, component=1)
        given = " ".join(p for p in (pid.field(5, component=2), pid.field(5, component=3)) if p)
        out["name"] = ", ".join(p for p in (family, given) if p) or None
        match = re.match(r"^(\d{4})(\d{2})(\d{2})", pid.field(7) or "")
        if match:
            try:
                out["dob"] = datetime(
                    int(match.group(1)), int(match.group(2)), int(match.group(3))
                ).strftime("%Y-%m-%d")
            except ValueError:
                out["dob"] = None
        out["sex"] = pid.field(8) or None
    except Exception:  # pragma: no cover - a malformed PID keeps what it had
        pass
    return out


# ════════════════════════════════════════════
#  Adapter Interface
# ════════════════════════════════════════════


class IngestionAdapter(ABC):
    """Abstract base for all ingestion adapters."""

    @property
    @abstractmethod
    def adapter_name(self) -> str: ...

    @property
    @abstractmethod
    def source_type(self) -> str: ...

    @abstractmethod
    def can_handle(self, raw_payload: str, content_type: str | None = None) -> bool:
        """Return True if this adapter can parse the given payload."""
        ...

    @abstractmethod
    def parse(
        self, raw_payload: str, source_name: str | None = None, correlation_id: str | None = None
    ) -> list[CanonicalEvent]:
        """Parse the payload into canonical events. Must not raise on malformed input."""
        ...


class ParseError(Exception):
    """Raised internally when a parse step fails; caught by the adapter."""

    pass


# ════════════════════════════════════════════
#  HL7 ORU Adapter
# ════════════════════════════════════════════


class HL7Adapter(IngestionAdapter):
    """Parses HL7 v2 ORU^R01 messages.

    Supported segments: MSH, PID, PV1 (optional), OBR (repeated), OBX (repeated per OBR), NTE (ignored).
    Segment separator: \\r, \\n, or \\r\\n.

    Field/component/repetition/sub-component separators and escape sequences are
    read from the message's own MSH-1/MSH-2 by services.vitals_source.hl7v2 —
    this adapter does NOT string-split, because bedside monitors and their
    gateways legitimately send repeated identifiers (`~`), escaped separators
    (`\\S\\`) and, occasionally, non-default encoding characters. This is the
    same parser the MLLP monitor feed uses, deliberately: one HL7 message must
    mean the same thing whether it arrived over HTTP or over a socket.

    `default_timezone` is the zone an offset-less DTM is read in. Left unset it
    is the appliance's zone, which is right for a device on the same ward. The
    MLLP monitor source passes its own per-device setting so that the timestamp
    stored in `vitals` is the same instant the ordering guard judged — if those
    two used different assumptions, the reading we alerted on and the reading in
    the record would disagree about when it happened.
    """

    def __init__(self, *, default_timezone=None):
        self.default_timezone = default_timezone

    @property
    def adapter_name(self) -> str:
        return "hl7_oru"

    @property
    def source_type(self) -> str:
        return "hl7"

    def can_handle(self, raw_payload: str, content_type: str | None = None) -> bool:
        stripped = raw_payload.strip()
        return stripped.startswith("MSH|")

    def parse(
        self, raw_payload: str, source_name: str | None = None, correlation_id: str | None = None
    ) -> list[CanonicalEvent]:
        from services.vitals_source import hl7v2

        events: list[CanonicalEvent] = []
        errors: list[str] = []

        try:
            message = hl7v2.parse(raw_payload)
        except hl7v2.Hl7ParseError as e:
            raise ParseError(f"Not a valid HL7 message: {e}") from e

        sending_app = message.sending_application
        msg_timestamp = parse_hl7_timestamp(
            message.message_datetime, default_timezone=self.default_timezone
        )

        # PID-3 is the patient identifier LIST: repetitions of `id^^^authority^type`.
        # A monitor commonly sends the hospital MRN alongside its own local id;
        # we take the first repetition's ID component, which is what the patient
        # resolver in ingestion_service matches against.
        pid = message.first("PID")
        patient_id = pid.field(3, component=1) if pid else ""
        if not patient_id:
            raise ParseError("No patient identifier found in PID segment")
        identity = hl7_patient_identity(pid)
        sender = (
            "^".join(p for p in (message.sending_application, message.sending_facility) if p)
            or None
        )

        pv1 = message.first("PV1")
        encounter_id = (pv1.field(19, component=1) or None) if pv1 else None

        # Group OBX segments under the OBR that precedes them.
        obr_groups: list[dict] = []
        current_obr: dict | None = None

        for seg in message.segments:
            if seg.name == "OBR":
                placer = seg.field(2) or None
                current_obr = {
                    "panel_name": seg.field(4, component=2) or seg.field(4, component=1),
                    "observed_at": parse_hl7_timestamp(
                        seg.field(7), default_timezone=self.default_timezone
                    ),
                    "group_id": placer or str(uuid.uuid4())[:8],
                    "placer": placer,
                    "filler": seg.field(3) or None,
                    "generated": placer is None,
                    "obx_segments": [],
                }
                obr_groups.append(current_obr)
            elif seg.name == "OBX":
                if current_obr is None:
                    # OBX without a preceding OBR — create an implicit group.
                    # Common from simple monitors that skip the order segment.
                    current_obr = {
                        "panel_name": "Unspecified",
                        "observed_at": msg_timestamp,
                        "group_id": str(uuid.uuid4())[:8],
                        "placer": None,
                        "filler": None,
                        "generated": True,
                        "obx_segments": [],
                    }
                    obr_groups.append(current_obr)
                current_obr["obx_segments"].append(seg)

        for obr in obr_groups:
            for obx in obr["obx_segments"]:
                try:
                    event = self._parse_obx(
                        obx,
                        patient_id=patient_id,
                        encounter_id=encounter_id,
                        panel_name=obr["panel_name"],
                        obr_group_id=obr["group_id"],
                        observed_at=obr["observed_at"],
                        source_name=source_name or sending_app,
                        correlation_id=correlation_id,
                        msg_timestamp=msg_timestamp,
                    )
                    # Identity and order numbers ride on every result of the
                    # message (lis-19, lis-20); nothing above reads them.
                    event.patient_identifiers = list(identity["identifiers"])
                    event.patient_name = identity["name"]
                    event.patient_dob = identity["dob"]
                    event.patient_sex = identity["sex"]
                    event.obr_placer = obr["placer"]
                    event.obr_filler = obr["filler"]
                    event.group_id_generated = obr["generated"]
                    event.obx_sub_id = obx.field(4) or None
                    event.hl7_sender = sender
                    events.append(event)
                except Exception as e:
                    # One bad OBX must never cost the rest of the panel — a
                    # device that garbles its temperature field still has a
                    # valid SpO2 in the same message.
                    errors.append(f"OBX parse error: {e} | segment: {obx.raw[:80]}")

        if errors and not events:
            raise ParseError("; ".join(errors))

        return events

    def _parse_obx(
        self,
        obx,
        *,
        patient_id: str,
        encounter_id: str | None,
        panel_name: str,
        obr_group_id: str,
        observed_at: str | None,
        source_name: str | None,
        correlation_id: str | None,
        msg_timestamp: str | None,
    ) -> CanonicalEvent:
        """Parse a single OBX segment into a CanonicalEvent."""
        obx_raw = obx.raw

        value_type_raw = obx.field(2)  # OBX-2: Value Type (NM, ST, CE, …)
        value_raw = obx.field(5)  # OBX-5: Observation Value
        units = obx.field(6, component=1) or obx.field(6)  # OBX-6: Units (CE: code^text^system)
        ref_range = obx.field(7)  # OBX-7: Reference Range
        abnormal_flag = obx.field(8)  # OBX-8: Abnormal Flags
        obs_status = obx.field(11)  # OBX-11: Observation Result Status
        obs_time = parse_hl7_timestamp(  # OBX-14: Date/Time of Observation
            obx.field(14), default_timezone=self.default_timezone
        )

        # OBX-3 is a CE: identifier^text^coding-system.
        obs_code = obx.field(3, component=1) or None
        obs_name = obx.field(3, component=2) or obs_code or "Unknown"
        obs_code_system = obx.field(3, component=3) or None

        # Determine value type and parse numeric
        value_numeric: float | None = None
        vt = ValueType.TEXT
        if value_type_raw in ("NM", "SN") and value_raw:
            try:
                # SN structured numeric: might be e.g. ">10" or "1:100"
                cleaned = re.sub(r"[<>= ]", "", value_raw)
                value_numeric = float(cleaned)
                vt = ValueType.NUMERIC
            except (ValueError, TypeError):
                pass

        # Map to vital key
        vital_key = map_to_vital_key(obs_code, obs_name)
        obs_type = ObservationType.VITAL if vital_key else ObservationType.LAB

        # Unit normalization
        if vital_key == "Temp" and value_numeric is not None:
            value_numeric = normalize_temp_to_celsius(value_numeric, units)
            units = "°C"

        # Reject implausible vital readings before they reach NEWS2/alerts.
        value_numeric = validate_vital_value(vital_key, value_numeric)

        effective_time = obs_time or observed_at or msg_timestamp

        return CanonicalEvent(
            patient_identifier=patient_id,
            encounter_identifier=encounter_id,
            source_type="hl7",
            source_name=source_name,
            event_type="observation",
            observation_type=obs_type,
            observation_code=obs_code,
            observation_code_system=obs_code_system,
            observation_name=obs_name,
            value=value_raw or "",
            value_type=vt,
            value_numeric=value_numeric,
            unit=units,
            reference_range=ref_range,
            abnormal_flag=abnormal_flag,
            observed_at=effective_time,
            received_at=datetime.now(UTC).isoformat(),
            status=obs_status or "final",
            raw_segment_ref=obx_raw,
            correlation_id=correlation_id,
            panel_name=panel_name,
            obr_group_id=obr_group_id,
            mapped_vital_key=vital_key,
        )


# ════════════════════════════════════════════
#  JSON Webhook Adapter
# ════════════════════════════════════════════


class JSONAdapter(IngestionAdapter):
    """Parses structured JSON webhook payloads.

    Expected format:
    {
        "patient_id": "PAT001",
        "encounter_id": "ENC001",              // optional
        "source_name": "BedsideMonitor-ICU-3",  // optional
        "message_id": "abc-123",                // optional, for dedup
        "observed_at": "2024-01-01T12:00:00Z",  // optional
        "observations": [
            {
                "code": "8867-4",               // optional LOINC
                "name": "Heart Rate",
                "value": 88,
                "unit": "bpm",
                "reference_range": "60-100",    // optional
                "abnormal_flag": "",            // optional
                "status": "final"               // optional
            },
            ...
        ]
    }
    """

    @property
    def adapter_name(self) -> str:
        return "json_webhook"

    @property
    def source_type(self) -> str:
        return "json"

    def can_handle(self, raw_payload: str, content_type: str | None = None) -> bool:
        if content_type and "json" in content_type.lower():
            return True
        stripped = raw_payload.strip()
        return stripped.startswith("{") and stripped.endswith("}")

    def parse(
        self, raw_payload: str, source_name: str | None = None, correlation_id: str | None = None
    ) -> list[CanonicalEvent]:
        import json

        try:
            data = json.loads(raw_payload)
        except json.JSONDecodeError as e:
            raise ParseError(f"Invalid JSON: {e}") from e

        patient_id = data.get("patient_id")
        if not patient_id:
            raise ParseError("Missing required field: patient_id")

        encounter_id = data.get("encounter_id")
        src_name = data.get("source_name") or source_name
        observed_at = data.get("observed_at")
        now_iso = datetime.now(UTC).isoformat()
        # Optional identity for the unmatched results queue (lis-19). A date of
        # birth is kept only when it is a real YYYY-MM-DD date.
        stated_name = data.get("patient_name")
        stated_name = (str(stated_name).strip() or None) if stated_name is not None else None
        stated_dob = None
        raw_dob = data.get("date_of_birth")
        if isinstance(raw_dob, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", raw_dob.strip()):
            try:
                stated_dob = datetime.strptime(raw_dob.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                stated_dob = None

        observations = data.get("observations")
        if not observations or not isinstance(observations, list):
            raise ParseError("Missing or invalid 'observations' array")

        events: list[CanonicalEvent] = []
        for obs in observations:
            if not isinstance(obs, dict):
                continue

            name = obs.get("name", "")
            code = obs.get("code")
            raw_value = obs.get("value")
            if raw_value is None and not name:
                continue  # skip empty observations

            value_str = str(raw_value) if raw_value is not None else ""
            value_numeric: float | None = None
            vt = ValueType.TEXT
            if isinstance(raw_value, (int, float)):
                value_numeric = float(raw_value)
                vt = ValueType.NUMERIC
            elif isinstance(raw_value, str):
                try:
                    value_numeric = float(raw_value)
                    vt = ValueType.NUMERIC
                except ValueError:
                    pass

            vital_key = map_to_vital_key(code, name)
            obs_type = ObservationType.VITAL if vital_key else ObservationType.LAB

            unit = obs.get("unit")
            if vital_key == "Temp" and value_numeric is not None:
                value_numeric = normalize_temp_to_celsius(value_numeric, unit)
                unit = "°C"

            # Reject implausible vital readings before they reach NEWS2/alerts.
            value_numeric = validate_vital_value(vital_key, value_numeric)

            events.append(
                CanonicalEvent(
                    patient_identifier=str(patient_id),
                    encounter_identifier=encounter_id,
                    source_type="json",
                    source_name=src_name,
                    event_type="observation",
                    observation_type=obs_type,
                    observation_code=code,
                    observation_code_system=obs.get("code_system"),
                    observation_name=name or code or "Unknown",
                    value=value_str,
                    value_type=vt,
                    value_numeric=value_numeric,
                    unit=unit,
                    reference_range=obs.get("reference_range"),
                    abnormal_flag=obs.get("abnormal_flag"),
                    observed_at=obs.get("observed_at") or observed_at or now_iso,
                    received_at=now_iso,
                    status=obs.get("status", "final"),
                    raw_segment_ref=None,
                    correlation_id=correlation_id,
                    mapped_vital_key=vital_key,
                    patient_identifiers=[str(patient_id)],
                    patient_name=stated_name,
                    patient_dob=stated_dob,
                )
            )

        if not events:
            raise ParseError("No valid observations found in payload")

        return events


# ════════════════════════════════════════════
#  CSV Replay Adapter
# ════════════════════════════════════════════


class CSVAdapter(IngestionAdapter):
    """Parses CSV replay files for testing and demo backfill.

    Expected columns (header row required):
        patient_id, name, code, value, unit, observed_at (optional)

    Example:
        patient_id,name,code,value,unit,observed_at
        PAT001,Heart Rate,8867-4,88,bpm,2024-01-01T12:00:00Z
    """

    @property
    def adapter_name(self) -> str:
        return "csv_replay"

    @property
    def source_type(self) -> str:
        return "csv_replay"

    def can_handle(self, raw_payload: str, content_type: str | None = None) -> bool:
        if content_type and "csv" in content_type.lower():
            return True
        # Check if it looks like CSV with a header row
        lines = raw_payload.strip().split("\n")
        if len(lines) >= 2 and "patient_id" in lines[0].lower():
            return True
        return False

    def parse(
        self, raw_payload: str, source_name: str | None = None, correlation_id: str | None = None
    ) -> list[CanonicalEvent]:
        reader = csv.DictReader(io.StringIO(raw_payload.strip()))
        now_iso = datetime.now(UTC).isoformat()
        events: list[CanonicalEvent] = []

        for _row_num, row in enumerate(reader, start=2):
            patient_id = row.get("patient_id", "").strip()
            if not patient_id:
                continue

            name = row.get("name", "").strip()
            code = row.get("code", "").strip() or None
            raw_value = row.get("value", "").strip()
            unit = row.get("unit", "").strip() or None
            observed_at = row.get("observed_at", "").strip() or now_iso

            value_numeric: float | None = None
            vt = ValueType.TEXT
            try:
                value_numeric = float(raw_value)
                vt = ValueType.NUMERIC
            except (ValueError, TypeError):
                pass

            vital_key = map_to_vital_key(code, name)
            obs_type = ObservationType.VITAL if vital_key else ObservationType.LAB

            if vital_key == "Temp" and value_numeric is not None:
                value_numeric = normalize_temp_to_celsius(value_numeric, unit)
                unit = "°C"

            # Reject implausible vital readings before they reach NEWS2/alerts.
            value_numeric = validate_vital_value(vital_key, value_numeric)

            events.append(
                CanonicalEvent(
                    patient_identifier=patient_id,
                    source_type="csv_replay",
                    source_name=source_name or "csv_replay",
                    event_type="observation",
                    observation_type=obs_type,
                    observation_code=code,
                    observation_name=name or code or "Unknown",
                    value=raw_value,
                    value_type=vt,
                    value_numeric=value_numeric,
                    unit=unit,
                    observed_at=observed_at,
                    received_at=now_iso,
                    status="final",
                    correlation_id=correlation_id,
                    mapped_vital_key=vital_key,
                )
            )

        if not events:
            raise ParseError("No valid rows found in CSV")

        return events


# ════════════════════════════════════════════
#  FHIR R4 Adapter
# ════════════════════════════════════════════


class FHIRAdapter(IngestionAdapter):
    """Parses FHIR R4 payloads — either a Bundle (transaction/collection/batch)
    with ``entry[].resource`` or a single resource document.

    Supported resource types:
      - Observation        → one CanonicalEvent per observation (vitals + labs)
      - Patient            → provenance event (event_type="patient")
      - Encounter          → provenance event (event_type="encounter")
      - MedicationRequest  → event_type="medication_request"
      - AllergyIntolerance → event_type="allergy"

    Unknown resource types are skipped. Vitals are mapped via LOINC codes and run
    through the same plausibility validation + Temp normalization as every other
    adapter, so implausible feed values never reach NEWS2/alerts.
    """

    _SUPPORTED_TYPES = frozenset(
        {
            "Bundle",
            "Observation",
            "Patient",
            "Encounter",
            "MedicationRequest",
            "AllergyIntolerance",
        }
    )
    _LOINC_URI = "http://loinc.org"

    @property
    def adapter_name(self) -> str:
        return "fhir_r4"

    @property
    def source_type(self) -> str:
        return "fhir"

    def can_handle(self, raw_payload: str, content_type: str | None = None) -> bool:
        if content_type and "fhir" in content_type.lower():
            return True
        stripped = raw_payload.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return False
        import json

        try:
            data = json.loads(raw_payload)
        except (json.JSONDecodeError, ValueError):
            return False
        return isinstance(data, dict) and data.get("resourceType") in self._SUPPORTED_TYPES

    def parse(
        self, raw_payload: str, source_name: str | None = None, correlation_id: str | None = None
    ) -> list[CanonicalEvent]:
        import json

        try:
            data = json.loads(raw_payload)
        except json.JSONDecodeError as e:
            raise ParseError(f"Invalid FHIR JSON: {e}") from e

        if not isinstance(data, dict):
            raise ParseError("FHIR payload must be a JSON object")

        resource_type = data.get("resourceType")
        if not resource_type:
            raise ParseError("Missing resourceType in FHIR payload")

        # Collect the resources to process: either every entry of a Bundle or the
        # single top-level resource.
        resources: list[dict] = []
        if resource_type == "Bundle":
            entries = data.get("entry")
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        res = entry.get("resource")
                        if isinstance(res, dict) and res.get("resourceType"):
                            resources.append(res)
        else:
            resources.append(data)

        now_iso = datetime.now(UTC).isoformat()
        events: list[CanonicalEvent] = []

        handlers: dict[str, Callable[..., CanonicalEvent | list[CanonicalEvent] | None]] = {
            "Observation": self._parse_observation,
            "Patient": self._parse_patient,
            "Encounter": self._parse_encounter,
            "MedicationRequest": self._parse_medication_request,
            "AllergyIntolerance": self._parse_allergy,
        }

        for res in resources:
            handler = handlers.get(res.get("resourceType") or "")
            if handler is None:
                continue  # skip unknown resource types gracefully
            try:
                evt = handler(res, source_name, correlation_id, now_iso)
            except Exception:
                # Tolerate a single malformed resource without failing the batch.
                continue
            # A handler may return a single event, a list (e.g. component-encoded
            # observations), or None.
            if isinstance(evt, list):
                events.extend(e for e in evt if e is not None)
            elif evt is not None:
                events.append(evt)

        if not events:
            raise ParseError("No parseable FHIR resources found in payload")

        return events

    # ── Resource handlers ──

    def _parse_observation(
        self, res: dict, source_name: str | None, correlation_id: str | None, now_iso: str
    ) -> list[CanonicalEvent]:
        """Parse an Observation into one or more CanonicalEvents.

        Handles FHIR R4 vital-signs panels (e.g. Blood Pressure 85354-9) where the
        actual readings live in ``component[]`` rather than a top-level value —
        one event is emitted per component. An Observation with no resolvable
        subject is skipped (an orphaned vital must not enter the clinical picture).
        """
        patient_id = self._ref_to_id(res.get("subject")) or ""
        if not patient_id:
            return []  # skip: cannot attribute this reading to a patient

        encounter_id = self._ref_to_id(res.get("encounter"))
        observed_at = self._extract_effective(res) or now_iso
        status = res.get("status") or "final"

        def _make(code_obj: dict, value_res: dict) -> CanonicalEvent | None:
            code, display, code_system = self._extract_code(code_obj)
            value_str, value_numeric, vt, unit = self._extract_value(value_res)
            # Skip a component that carried no value at all.
            if value_str == "" and value_numeric is None and not display:
                return None
            vital_key = map_to_vital_key(code, display)
            obs_type = ObservationType.VITAL if vital_key else ObservationType.LAB
            if vital_key == "Temp" and value_numeric is not None:
                value_numeric = normalize_temp_to_celsius(value_numeric, unit)
                unit = "°C"
            value_numeric = validate_vital_value(vital_key, value_numeric)
            return CanonicalEvent(
                patient_identifier=patient_id,
                encounter_identifier=encounter_id,
                source_type="fhir",
                source_name=source_name,
                event_type="observation",
                observation_type=obs_type,
                observation_code=code,
                observation_code_system=code_system or "UNKNOWN",
                observation_name=display or code or "Unknown",
                value=value_str,
                value_type=vt,
                value_numeric=value_numeric,
                unit=unit,
                reference_range=self._extract_reference_range(value_res),
                abnormal_flag=self._extract_interpretation(value_res),
                observed_at=observed_at,
                received_at=now_iso,
                status=status,
                correlation_id=correlation_id,
                mapped_vital_key=vital_key,
            )

        code_obj = res.get("code")
        if not isinstance(code_obj, dict):
            code_obj = {}
        components = res.get("component")
        events: list[CanonicalEvent] = []

        # Top-level value first (a simple vital or a lab result).
        top = _make(code_obj, res)
        if top is not None:
            events.append(top)

        # Component-encoded readings (e.g. systolic/diastolic under a BP panel).
        if isinstance(components, list):
            for comp in components:
                if not isinstance(comp, dict):
                    continue
                comp_code = comp.get("code")
                if not isinstance(comp_code, dict):
                    comp_code = {}
                evt = _make(comp_code, comp)
                if evt is not None:
                    events.append(evt)

        return events

    def _parse_patient(
        self, res: dict, source_name: str | None, correlation_id: str | None, now_iso: str
    ) -> CanonicalEvent:
        patient_id = res.get("id")
        if not patient_id:
            idents = res.get("identifier")
            if isinstance(idents, list) and idents and isinstance(idents[0], dict):
                patient_id = idents[0].get("value")
        name = self._human_name(res.get("name")) or ""
        return CanonicalEvent(
            patient_identifier=str(patient_id) if patient_id else "",
            source_type="fhir",
            source_name=source_name,
            event_type="patient",
            observation_type=ObservationType.UNKNOWN,
            observation_name="Patient demographics",
            value=name,
            value_type=ValueType.TEXT,
            status=res.get("active") is not False and "final" or "inactive",
            observed_at=now_iso,
            received_at=now_iso,
            correlation_id=correlation_id,
        )

    def _parse_encounter(
        self, res: dict, source_name: str | None, correlation_id: str | None, now_iso: str
    ) -> CanonicalEvent:
        patient_id = (
            self._ref_to_id(res.get("subject")) or self._ref_to_id(res.get("patient")) or ""
        )
        status = res.get("status") or "unknown"
        return CanonicalEvent(
            patient_identifier=patient_id,
            encounter_identifier=res.get("id"),
            source_type="fhir",
            source_name=source_name,
            event_type="encounter",
            observation_type=ObservationType.UNKNOWN,
            observation_name="Encounter",
            value=status,
            value_type=ValueType.TEXT,
            status=status,
            observed_at=now_iso,
            received_at=now_iso,
            correlation_id=correlation_id,
        )

    def _parse_medication_request(
        self, res: dict, source_name: str | None, correlation_id: str | None, now_iso: str
    ) -> CanonicalEvent:
        med = res.get("medicationCodeableConcept")
        med_name = None
        if isinstance(med, dict):
            _, med_name, _ = self._extract_code(med)
        if not med_name:
            medref = res.get("medicationReference")
            if isinstance(medref, dict):
                med_name = medref.get("display")
        med_name = med_name or "Unknown medication"
        return CanonicalEvent(
            patient_identifier=self._ref_to_id(res.get("subject")) or "",
            encounter_identifier=self._ref_to_id(res.get("encounter")),
            source_type="fhir",
            source_name=source_name,
            event_type="medication_request",
            observation_type=ObservationType.UNKNOWN,
            observation_name="MedicationRequest",
            value=med_name,
            value_type=ValueType.TEXT,
            status=res.get("status") or "active",
            observed_at=res.get("authoredOn") or now_iso,
            received_at=now_iso,
            correlation_id=correlation_id,
        )

    def _parse_allergy(
        self, res: dict, source_name: str | None, correlation_id: str | None, now_iso: str
    ) -> CanonicalEvent:
        code_obj = res.get("code")
        if not isinstance(code_obj, dict):
            code_obj = {}
        _, substance, _ = self._extract_code(code_obj)
        substance = substance or "Unknown substance"
        # clinicalStatus is a CodeableConcept; a conformant resource encodes the
        # status in coding[].code (e.g. "active") with no .text, so read the code
        # too rather than only .text (which would yield None).
        cs = res.get("clinicalStatus")
        if isinstance(cs, dict):
            cs_code, cs_display, _ = self._extract_code(cs)
            clinical_status = cs.get("text") or cs_code or cs_display or "active"
        else:
            clinical_status = res.get("status") or "active"
        return CanonicalEvent(
            patient_identifier=self._ref_to_id(res.get("patient")) or "",
            encounter_identifier=self._ref_to_id(res.get("encounter")),
            source_type="fhir",
            source_name=source_name,
            event_type="allergy",
            observation_type=ObservationType.UNKNOWN,
            observation_name="AllergyIntolerance",
            value=substance,
            value_type=ValueType.TEXT,
            abnormal_flag=res.get("criticality"),
            status=clinical_status,
            observed_at=res.get("recordedDate") or now_iso,
            received_at=now_iso,
            correlation_id=correlation_id,
        )

    # ── Extraction helpers ──

    def _extract_code(self, code_obj: dict) -> tuple[str | None, str | None, str | None]:
        """Return (code, display, code_system) from a FHIR CodeableConcept.

        Prefers a LOINC coding for the code; display prefers ``code.text`` then a
        coding display. code_system reflects where the code actually came from
        ("LOINC" for a LOINC coding, else the coding's own system URI, else None)
        so the observation is not mislabeled as LOINC when it isn't.
        """
        if not isinstance(code_obj, dict):
            return None, None, None
        codings = code_obj.get("coding")
        codings = codings if isinstance(codings, list) else []

        code = None
        code_system = None
        coding_display = None
        for c in codings:
            if not isinstance(c, dict):
                continue
            system = (c.get("system") or "").lower()
            if "loinc.org" in system:
                code = c.get("code")
                code_system = "LOINC"
                coding_display = coding_display or c.get("display")
                break
        # Fall back to the first coding when no LOINC coding is present.
        if code is None:
            for c in codings:
                if isinstance(c, dict) and c.get("code"):
                    code = c.get("code")
                    code_system = c.get("system") or "UNKNOWN"
                    coding_display = coding_display or c.get("display")
                    break
        if coding_display is None:
            for c in codings:
                if isinstance(c, dict) and c.get("display"):
                    coding_display = c.get("display")
                    break
        display = code_obj.get("text") or coding_display
        return code, display, code_system

    def _extract_value(self, res: dict) -> tuple[str, float | None, ValueType, str | None]:
        """Extract (value_str, value_numeric, value_type, unit) from an Observation."""
        vq = res.get("valueQuantity")
        if isinstance(vq, dict) and vq.get("value") is not None:
            raw = vq["value"]
            unit = vq.get("unit") or vq.get("code")
            try:
                return str(raw), float(raw), ValueType.NUMERIC, unit
            except (ValueError, TypeError):
                return str(raw), None, ValueType.TEXT, unit

        if res.get("valueString") is not None:
            return str(res.get("valueString")), None, ValueType.TEXT, None

        if res.get("valueInteger") is not None:
            raw = res["valueInteger"]
            try:
                return str(raw), float(raw), ValueType.NUMERIC, None
            except (ValueError, TypeError):
                return str(raw), None, ValueType.TEXT, None

        vcc = res.get("valueCodeableConcept")
        if isinstance(vcc, dict):
            _, display, _ = self._extract_code(vcc)
            if display:
                return display, None, ValueType.TEXT, None

        if res.get("valueBoolean") is not None:
            return str(res.get("valueBoolean")), None, ValueType.TEXT, None

        return "", None, ValueType.TEXT, None

    def _extract_effective(self, res: dict) -> str | None:
        if res.get("effectiveDateTime"):
            return res.get("effectiveDateTime")
        if res.get("effectiveInstant"):
            return res.get("effectiveInstant")
        period = res.get("effectivePeriod")
        if isinstance(period, dict) and period.get("start"):
            return period.get("start")
        if res.get("issued"):
            return res.get("issued")
        return None

    def _extract_reference_range(self, res: dict) -> str | None:
        ranges = res.get("referenceRange")
        if not (isinstance(ranges, list) and ranges):
            return None
        r0 = ranges[0]
        if not isinstance(r0, dict):
            return None
        low = r0.get("low", {}).get("value") if isinstance(r0.get("low"), dict) else None
        high = r0.get("high", {}).get("value") if isinstance(r0.get("high"), dict) else None
        if low is not None and high is not None:
            return f"{low}-{high}"
        if low is not None:
            return f">={low}"
        if high is not None:
            return f"<={high}"
        return r0.get("text")

    def _extract_interpretation(self, res: dict) -> str | None:
        interp = res.get("interpretation")
        if not (isinstance(interp, list) and interp):
            return None
        i0 = interp[0]
        if not isinstance(i0, dict):
            return None
        codings = i0.get("coding")
        if isinstance(codings, list) and codings and isinstance(codings[0], dict):
            return codings[0].get("code") or codings[0].get("display") or i0.get("text")
        return i0.get("text")

    @staticmethod
    def _ref_to_id(ref_obj) -> str | None:
        """Resolve a FHIR Reference to a bare identifier.

        Handles ``{"reference": "Patient/123"}`` (strips the resource-type prefix)
        and falls back to ``{"identifier": {"value": "..."}}``.
        """
        if not isinstance(ref_obj, dict):
            return None
        ref = ref_obj.get("reference")
        if isinstance(ref, str) and ref:
            # Strip any "ResourceType/" prefix (and leading URL path).
            return ref.rstrip("/").split("/")[-1]
        ident = ref_obj.get("identifier")
        if isinstance(ident, dict) and ident.get("value"):
            return str(ident.get("value"))
        return None

    @staticmethod
    def _human_name(name_list) -> str | None:
        if isinstance(name_list, list) and name_list:
            n0 = name_list[0]
            if isinstance(n0, dict):
                if n0.get("text"):
                    return n0["text"]
                given = n0.get("given")
                given_str = (
                    " ".join(g for g in given if isinstance(g, str))
                    if isinstance(given, list)
                    else ""
                )
                family = n0.get("family") or ""
                full = f"{given_str} {family}".strip()
                return full or None
        return None


# ════════════════════════════════════════════
#  Adapter Registry
# ════════════════════════════════════════════

ADAPTERS: list[IngestionAdapter] = [
    HL7Adapter(),
    FHIRAdapter(),
    JSONAdapter(),
    CSVAdapter(),
]


def select_adapter(raw_payload: str, content_type: str | None = None) -> IngestionAdapter | None:
    """Select the first adapter that can handle the given payload."""
    for adapter in ADAPTERS:
        if adapter.can_handle(raw_payload, content_type):
            return adapter
    return None
