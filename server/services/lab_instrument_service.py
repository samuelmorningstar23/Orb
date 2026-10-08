"""The laboratory's analyser registry: instruments, their keys and their test
codes (lis-23).

Behind ORB_LAB (default off). Every route that reaches this module answers 404
while the laboratory is off, and nothing here runs at startup, so a site that
never switches the laboratory on keeps both tables (migration 64) empty.

WHAT IT HOLDS
  * lab_instruments: each analyser, or the middleware that speaks for
    several, that may send results to Orb, with the hash of its key.
  * lab_instrument_test_map: which Orb laboratory test each analyser code is.

NOTHING HERE RECEIVES A RESULT. The HTTP intake (lis-24) and the MLLP
listener (lis-25) are later increments, each behind its own switch. They use
three things from this module and nothing else:
  * authenticate(conn, instrument_id, presented_key): the one check of an
    analyser's X-Instrument-Id and X-Instrument-Key. An unknown id, an
    instrument with no key and a wrong key all fail the same way, after the
    same constant-time comparison, so the answer never says which ids exist;
    a right key on a switched-off analyser is refused separately.
  * mapping_for(conn, instrument_id, instrument_code): the active mapping for
    a code exactly as the analyser sent it, or None (the intake quarantines).
  * record_message_received(conn, instrument_id): stamps last_message_at.

THE KEY IS SHOWN ONCE. issue_key returns a new random key exactly once, in
its own answer; Orb stores only 'sha256:' and its digest. No read, no list,
no audit row and no log line ever carries the key or the stored digest
(the public row shape below is an allow-list, never SELECT *). Issuing a new
key replaces the digest in the same transaction, so the old key stops working
at once. A key is 32 random bytes, so a plain SHA-256 digest is enough: there
is nothing to guess, and a slow password hash would only slow every result
an analyser sends.

WHO CHANGES WHAT
  * Registering an analyser, changing its details, switching it on or off and
    issuing its key need lab.interface (the administrator).
  * The code map needs lab.master (the pathologist): which test a code is
    decides which patient result a number becomes, so it is a clinical
    decision, not an interface setting.
  Each write checks the permission again here, beside the route's own check.

STRICT DEFAULTS (for Samuel to confirm before go-live)
  * A new analyser starts switched off, and the database refuses a switched-on
    analyser with no key.
  * Only HL7 version 2 over HTTP or MLLP is accepted. ASTM and the two TCP
    transports are kept in the schema for lis-26 and lis-27 and refused here
    until those exist, so nobody registers an analyser Orb cannot hear.
  * An IP address must be on a private network (Orb is strictly offline).
  * One active code per test per analyser, and no two codes on one analyser
    that differ only in capitals. A code maps only to an active laboratory
    analyte, never a panel and never a bedside test (LIS-R18).
  * The conversion factor is exact in thousandths; anything finer is refused
    rather than rounded, and a text or coded test takes no factor.
  * Every mapping change needs a note, and retiring one needs a reason; a
    mapping is retired, never deleted, so a result can always be traced.
  * Switching an analyser on or off, and replacing its key, need a reason.

EVERY WRITE is one BEGIN IMMEDIATE transaction with its audit row inside it,
category clinical_config (fail-closed): an audit row that cannot be written
rolls the change back, so the registry never changes unaudited.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import config
from database import db_session
from permissions import LAB_INTERFACE, LAB_MASTER, has_permission
from services.audit_service import log_audit_event
from services.clinical_time import utc_iso_now

PROTOCOLS = ("hl7v2", "astm")
SUPPORTED_PROTOCOLS = ("hl7v2",)
TRANSPORTS = ("http", "mllp_listen", "tcp_client", "tcp_listen")
SUPPORTED_TRANSPORTS = ("http", "mllp_listen")

PROTOCOL_LABELS = {
    "hl7v2": "HL7 version 2",
    "astm": "ASTM",
}
TRANSPORT_LABELS = {
    "http": "Sends results to Orb over the network (HTTP)",
    "mllp_listen": "Connects to Orb's analyser port (MLLP)",
    "tcp_client": "Orb connects to the analyser (TCP)",
    "tcp_listen": "The analyser connects to Orb (TCP)",
}

CREDENTIAL_SCHEME = "sha256"
KEY_BYTES = 32
# A digest no key can produce, compared against when there is nothing stored,
# so an unknown instrument costs the same comparison as a known one.
_NO_KEY_DIGEST = f"{CREDENTIAL_SCHEME}:{'0' * 64}"

INSTRUMENT_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
# HL7 delimiters and control characters never survive into an OBX-3 code.
_CODE_FORBIDDEN = re.compile(r"[|^~\\&\x00-\x1f\x7f]")

CODE_MAX = 64
UNIT_MAX = 32
TEXT_MAX = 200
NOTE_MAX = 500
FACTOR_MILLI_MAX = 1_000_000_000      # a factor of one million

_INSTRUMENT_EDITABLE = (
    "name", "make", "model", "serial", "section_id", "protocol", "transport",
    "host", "port", "assumed_timezone", "biomedical_asset_id",
)

# The public row: never credential_hash. An allow-list, so a later column is
# not published by accident.
_INSTRUMENT_SELECT = """
    SELECT i.id, i.name, i.make, i.model, i.serial, i.section_id, s.name AS section_name,
           i.protocol, i.transport, i.host, i.port, i.assumed_timezone,
           i.biomedical_asset_id, i.enabled, i.status_reason, i.last_message_at,
           (i.credential_hash IS NOT NULL) AS key_issued,
           i.credential_rotated_at, i.credential_rotated_by, ku.name AS credential_rotated_by_name,
           i.version, i.created_at, i.created_by, i.updated_at, i.updated_by,
           uu.name AS updated_by_name,
           (SELECT COUNT(*) FROM lab_instrument_test_map m
             WHERE m.instrument_id = i.id AND m.active = 1) AS mapped_codes
      FROM lab_instruments i
      LEFT JOIN lab_sections s ON s.id = i.section_id
      LEFT JOIN users ku ON ku.id = i.credential_rotated_by
      LEFT JOIN users uu ON uu.id = i.updated_by
"""

_MAP_SELECT = """
    SELECT m.instrument_id, m.instrument_code, m.test_id, t.name AS test_name,
           t.short_name AS test_short_name, t.unit AS test_unit, t.result_type AS test_result_type,
           t.active AS test_active, t.performed_at AS test_performed_at,
           m.factor_milli, m.instrument_unit, m.active, m.note,
           m.approved_by, au.name AS approved_by_name, m.approved_at,
           m.retired_by, ru.name AS retired_by_name, m.retired_at, m.retire_reason, m.version
      FROM lab_instrument_test_map m
      LEFT JOIN lab_tests t ON t.id = m.test_id
      LEFT JOIN users au ON au.id = m.approved_by
      LEFT JOIN users ru ON ru.id = m.retired_by
"""


class LabInstrumentError(ValueError):
    """A refusal a route turns into an HTTP answer: `code` is the machine
    reason, `message` the sentence for the screen, `status` the HTTP status."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class InstrumentAuthError(Exception):
    """An analyser's credentials were refused. `code` is 'unauthenticated'
    (unknown id, no key issued or a wrong key: deliberately one answer) or
    'instrument_disabled' (the right key on a switched-off analyser)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ════════════════════════════════════════════════════════════════════════════
#  Small pure helpers
# ════════════════════════════════════════════════════════════════════════════

def normalise_instrument_id(value: Any) -> Optional[str]:
    """The instrument id in its one stored spelling (capitals), or None when
    it cannot be one: 2 to 32 of A-Z, 0-9, '-' and '_', starting with a letter
    or digit. The intake normalises the X-Instrument-Id header the same way."""
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text if INSTRUMENT_ID_RE.fullmatch(text) else None


def new_key() -> str:
    """A fresh analyser key: 32 random bytes, URL-safe text (43 characters)."""
    return secrets.token_urlsafe(KEY_BYTES)


def hash_key(key: str) -> str:
    """What is stored for a key: 'sha256:' and the hex digest."""
    return f"{CREDENTIAL_SCHEME}:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def key_matches(stored: Optional[str], presented: Optional[str]) -> bool:
    """Whether `presented` is the key whose digest is `stored`, compared in
    constant time. With nothing stored (an unknown instrument, or no key
    issued yet) the same comparison runs against a digest no key produces."""
    presented_digest = hash_key(presented if isinstance(presented, str) else "")
    target = stored if isinstance(stored, str) and stored.startswith(f"{CREDENTIAL_SCHEME}:") \
        else _NO_KEY_DIGEST
    same = hmac.compare_digest(presented_digest.encode("ascii"), target.encode("ascii"))
    return bool(same and stored and presented)


def factor_text(factor_milli: Optional[int]) -> str:
    """'1', '0.1', '1000' from thousandths, with no trailing zeros."""
    if factor_milli is None:
        return ""
    value = (Decimal(int(factor_milli)) / 1000).normalize()
    text = format(value, "f")
    return text


def _factor_milli(value: Any, *, numeric: bool) -> int:
    """The conversion factor in thousandths, refused unless exact. A text or
    coded test takes no factor (only 1 is accepted)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return 1000
    if isinstance(value, bool):
        raise LabInstrumentError("invalid_value", "The factor must be a number.")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise LabInstrumentError("invalid_value", "The factor must be a number.")
    if not number.is_finite() or number <= 0:
        raise LabInstrumentError("invalid_value", "The factor must be a number above zero.")
    milli = number * 1000
    if milli != milli.to_integral_value():
        raise LabInstrumentError(
            "precision", "The factor can have at most three decimal places. Set the "
            "analyser to report in the test's unit instead of converting here.")
    milli_int = int(milli)
    if milli_int > FACTOR_MILLI_MAX:
        raise LabInstrumentError("invalid_value", "The factor is too large.")
    if not numeric and milli_int != 1000:
        raise LabInstrumentError(
            "factor_not_numeric", "Only a test with a number result takes a conversion factor.")
    return milli_int


def _clean_text(value: Any, label: str, *, required: bool = False,
                max_len: int = TEXT_MAX) -> Optional[str]:
    if value is not None and not isinstance(value, (str, int)):
        raise LabInstrumentError("invalid_value", f"{label} must be text.")
    text = str(value).strip() if value is not None else ""
    if not text:
        if required:
            raise LabInstrumentError("missing_field", f"{label} is required.")
        return None
    if len(text) > max_len:
        raise LabInstrumentError("too_long", f"{label} is longer than {max_len} characters.")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise LabInstrumentError("invalid_value", f"{label} has characters that cannot be stored.")
    return text


def _clean_host(value: Any) -> Optional[str]:
    """A network name or an IP address on the hospital's own network.

    Orb is strictly offline: an IP address that is not private, loopback or
    link-local is on the internet and is refused. A name cannot be checked
    without a lookup Orb does not make, so only its spelling is checked."""
    text = _clean_text(value, "Address", max_len=253)
    if text is None:
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        address = None
    if address is not None:
        if not (address.is_private or address.is_loopback or address.is_link_local):
            raise LabInstrumentError(
                "host_not_local",
                "This address is on the internet. Orb only talks to machines on the "
                "hospital's own network.")
        return str(address)
    name = text.lower()
    if not _HOSTNAME_RE.fullmatch(name):
        raise LabInstrumentError(
            "invalid_value",
            "Address must be a network name or an IP address, with no http:// and no path.")
    return name


def _clean_port(value: Any) -> Optional[int]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise LabInstrumentError("invalid_value", "Port must be a whole number.")
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise LabInstrumentError("invalid_value", "Port must be a whole number.")
    if str(port) != str(value).strip() or not 1 <= port <= 65535:
        raise LabInstrumentError("invalid_value", "Port must be a whole number from 1 to 65535.")
    return port


def _clean_timezone(value: Any) -> Optional[str]:
    """An IANA zone name ('Asia/Kolkata') or UTC; blank means the hospital's
    own zone. Offsets such as +05:30 are refused: a zone name says what the
    clock does on every date."""
    text = _clean_text(value, "Time zone", max_len=64)
    if text is None:
        return None
    if text.upper() == "UTC":
        return "UTC"
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:  # pragma: no cover - Python 3.9+ always has it
        raise LabInstrumentError("invalid_value", "Time zones cannot be checked on this computer.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+", text):
        raise LabInstrumentError(
            "invalid_value", "Time zone must be a zone name such as Asia/Kolkata.")
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise LabInstrumentError("invalid_value", f"{text} is not a known time zone.")
    return text


def _choice(value: Any, allowed: tuple, supported: tuple, label: str,
            words: dict) -> str:
    text = str(value or "").strip().lower()
    if text not in allowed:
        raise LabInstrumentError("invalid_value", f"Unknown {label}: {value}.")
    if text not in supported:
        raise LabInstrumentError(
            "not_supported",
            f"{words.get(text, text)} is not supported in this version of Orb yet.")
    return text


def _require(actor: dict, permission: str, message: str) -> None:
    if not actor or not actor.get("id") or not has_permission(actor.get("role", ""), permission):
        raise LabInstrumentError("permission_denied", message, 403)


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _require_tables(conn) -> None:
    for name in ("lab_instruments", "lab_instrument_test_map", "lab_tests", "lab_sections"):
        if not _table_exists(conn, name):
            raise LabInstrumentError("not_ready", "The laboratory tables are not installed yet.", 409)


def _audit(conn, actor: dict, action: str, *, target_type: str, target_id: Any,
           summary: str, metadata: Optional[dict] = None,
           request_meta: Optional[dict] = None) -> None:
    meta = request_meta or {}
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="clinical_config",
        action_type=action,
        outcome="success",
        db=conn,
        summary_message=summary,
        actor_user_id=actor.get("id"),
        actor_role=actor.get("role"),
        target_type=target_type,
        target_id=str(target_id),
        request_path=meta.get("request_path"),
        http_method=meta.get("http_method"),
        ip_address=meta.get("ip_address"),
        user_agent=meta.get("user_agent"),
        metadata=metadata or None,
    )


# ════════════════════════════════════════════════════════════════════════════
#  Reads
# ════════════════════════════════════════════════════════════════════════════

def options() -> dict:
    """What the set-up screen offers. No patient data."""
    from services.hospital_time import hospital_timezone
    zone = hospital_timezone()
    return {
        "protocols": [{"value": p, "label": PROTOCOL_LABELS[p], "supported": p in SUPPORTED_PROTOCOLS}
                      for p in PROTOCOLS],
        "transports": [{"value": t, "label": TRANSPORT_LABELS[t], "supported": t in SUPPORTED_TRANSPORTS}
                       for t in TRANSPORTS],
        "hospital_timezone": getattr(zone, "key", None) or str(zone),
        "listener": {
            "enabled": bool(config.feature_enabled("lab_analyser_listener")),
            "port": config.LAB_MLLP_PORT,
        },
    }


def _parse_day(value: Any) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _biomedical(conn, asset_id: Optional[int]) -> Optional[dict]:
    """The analyser's entry in the biomedical register and its calibration,
    or None when it is not linked."""
    if asset_id is None:
        return None
    if not _table_exists(conn, "biomedical_assets"):
        return {"id": asset_id, "found": False}
    asset = conn.execute(
        "SELECT id, asset_tag, name, status, is_active FROM biomedical_assets WHERE id = ?",
        (asset_id,)).fetchone()
    if asset is None:
        return {"id": asset_id, "found": False}
    calibration = None
    if _table_exists(conn, "biomedical_schedules"):
        row = conn.execute(
            "SELECT last_done_date, next_due_date, grace_days FROM biomedical_schedules "
            "WHERE asset_id = ? AND schedule_type = 'calibration' AND is_active = 1 "
            "ORDER BY next_due_date, id LIMIT 1", (asset_id,)).fetchone()
        if row is not None:
            from datetime import timedelta
            from services.hospital_time import hospital_today
            due = _parse_day(row["next_due_date"])
            grace = int(row["grace_days"] or 0)
            calibration = {
                "last_done_date": row["last_done_date"],
                "next_due_date": row["next_due_date"],
                "overdue": bool(due and hospital_today() > due + timedelta(days=grace)),
            }
    return {"id": asset["id"], "found": True, "asset_tag": asset["asset_tag"],
            "name": asset["name"], "status": asset["status"],
            "in_register": bool(asset["is_active"]), "calibration": calibration}


def _instrument_row(conn, r) -> dict:
    row = dict(r)
    return {
        "id": row["id"],
        "name": row["name"],
        "make": row["make"],
        "model": row["model"],
        "serial": row["serial"],
        "section_id": row["section_id"],
        "section_name": row["section_name"],
        "protocol": row["protocol"],
        "protocol_label": PROTOCOL_LABELS.get(row["protocol"], row["protocol"]),
        "transport": row["transport"],
        "transport_label": TRANSPORT_LABELS.get(row["transport"], row["transport"]),
        "supported": row["protocol"] in SUPPORTED_PROTOCOLS and row["transport"] in SUPPORTED_TRANSPORTS,
        "host": row["host"],
        "port": row["port"],
        "assumed_timezone": row["assumed_timezone"],
        "biomedical_asset_id": row["biomedical_asset_id"],
        "biomedical": _biomedical(conn, row["biomedical_asset_id"]),
        "enabled": bool(row["enabled"]),
        "status_reason": row["status_reason"],
        "last_message_at": row["last_message_at"],
        "key": {
            "issued": bool(row["key_issued"]),
            "issued_at": row["credential_rotated_at"],
            "issued_by": row["credential_rotated_by"],
            "issued_by_name": row["credential_rotated_by_name"],
        },
        "mapped_codes": int(row["mapped_codes"] or 0),
        "version": row["version"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "updated_by_name": row["updated_by_name"],
    }


def _mapping_row(r) -> dict:
    row = dict(r)
    row["active"] = bool(row["active"])
    row["test_active"] = bool(row["test_active"]) if row["test_active"] is not None else False
    row["test_found"] = row["test_name"] is not None
    row["factor"] = factor_text(row["factor_milli"])
    return row


def list_instruments(conn) -> list[dict]:
    rows = conn.execute(_INSTRUMENT_SELECT + " ORDER BY i.name COLLATE NOCASE, i.id").fetchall()
    return [_instrument_row(conn, r) for r in rows]


def list_mappings(conn, instrument_id: str, *, include_retired: bool = True) -> list[dict]:
    sql = _MAP_SELECT + " WHERE m.instrument_id = ?"
    if not include_retired:
        sql += " AND m.active = 1"
    sql += " ORDER BY m.active DESC, m.instrument_code COLLATE NOCASE, m.instrument_code"
    return [_mapping_row(r) for r in conn.execute(sql, (instrument_id,)).fetchall()]


def get_instrument(conn, instrument_id: Any, *, include_map: bool = True) -> Optional[dict]:
    ident = normalise_instrument_id(instrument_id)
    if ident is None:
        return None
    r = conn.execute(_INSTRUMENT_SELECT + " WHERE i.id = ?", (ident,)).fetchone()
    if r is None:
        return None
    out = _instrument_row(conn, r)
    if include_map:
        out["map"] = list_mappings(conn, ident)
    return out


def mapping_for(conn, instrument_id: Any, instrument_code: Any) -> Optional[dict]:
    """The ACTIVE mapping for a code exactly as the analyser sent it, with the
    test it names, or None. For the intake (lis-24): None means quarantine,
    and so does a mapping whose test is no longer an active laboratory
    analyte."""
    ident = normalise_instrument_id(instrument_id)
    if ident is None or not isinstance(instrument_code, str):
        return None
    r = conn.execute(_MAP_SELECT + " WHERE m.instrument_id = ? AND m.instrument_code = ? "
                     "AND m.active = 1", (ident, instrument_code.strip())).fetchone()
    return _mapping_row(r) if r is not None else None


def authenticate(conn, instrument_id: Any, presented_key: Any) -> dict:
    """The one check of an analyser's id and key (for lis-24 and lis-25).

    Returns the public instrument row. Raises InstrumentAuthError: code
    'unauthenticated' for an unknown id, an analyser with no key or a wrong
    key (one answer, after the same constant-time comparison), and
    'instrument_disabled' for the right key on a switched-off analyser."""
    ident = normalise_instrument_id(instrument_id)
    row = None
    if ident is not None and _table_exists(conn, "lab_instruments"):
        row = conn.execute("SELECT id, enabled, credential_hash FROM lab_instruments WHERE id = ?",
                           (ident,)).fetchone()
    stored = row["credential_hash"] if row is not None else None
    if not key_matches(stored, presented_key if isinstance(presented_key, str) else None):
        raise InstrumentAuthError("unauthenticated")
    if not row["enabled"]:
        raise InstrumentAuthError("instrument_disabled")
    return get_instrument(conn, ident, include_map=False)


def record_message_received(conn, instrument_id: str, *, at: Optional[str] = None) -> None:
    """Stamp last_message_at (for the intake, inside its own transaction). No
    audit here: the intake audits the message under 'ingestion'."""
    conn.execute("UPDATE lab_instruments SET last_message_at = ? WHERE id = ?",
                 (at or utc_iso_now(), instrument_id))


# ════════════════════════════════════════════════════════════════════════════
#  Instrument writes (lab.interface)
# ════════════════════════════════════════════════════════════════════════════

_INTERFACE_ONLY = "Only an administrator can register analysers and issue their keys."


def _check_section(conn, section_id: Optional[str]) -> None:
    if section_id is None:
        return
    if conn.execute("SELECT 1 FROM lab_sections WHERE id = ? AND active = 1",
                    (section_id,)).fetchone() is None:
        raise LabInstrumentError("unknown_reference", f"Unknown laboratory section: {section_id}.")


def _check_asset(conn, value: Any) -> Optional[int]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise LabInstrumentError("invalid_value", "The biomedical register entry must be a number.")
    try:
        asset_id = int(str(value).strip())
    except (TypeError, ValueError):
        raise LabInstrumentError("invalid_value", "The biomedical register entry must be a number.")
    if not _table_exists(conn, "biomedical_assets") or conn.execute(
            "SELECT 1 FROM biomedical_assets WHERE id = ? AND is_active = 1",
            (asset_id,)).fetchone() is None:
        raise LabInstrumentError("unknown_reference",
                                 f"No entry {asset_id} in the biomedical register.")
    return asset_id


def _validated_fields(conn, data: dict) -> dict:
    """Clean every editable field present in `data`."""
    out: dict = {}
    if "name" in data:
        out["name"] = _clean_text(data["name"], "Name", required=True)
    for key, label in (("make", "Make"), ("model", "Model"), ("serial", "Serial number")):
        if key in data:
            out[key] = _clean_text(data[key], label)
    if "section_id" in data:
        out["section_id"] = _clean_text(data["section_id"], "Section", max_len=64)
        _check_section(conn, out["section_id"])
    if "protocol" in data:
        out["protocol"] = _choice(data["protocol"], PROTOCOLS, SUPPORTED_PROTOCOLS,
                                  "protocol", PROTOCOL_LABELS)
    if "transport" in data:
        out["transport"] = _choice(data["transport"], TRANSPORTS, SUPPORTED_TRANSPORTS,
                                   "connection", TRANSPORT_LABELS)
    if "host" in data:
        out["host"] = _clean_host(data["host"])
    if "port" in data:
        out["port"] = _clean_port(data["port"])
    if "assumed_timezone" in data:
        out["assumed_timezone"] = _clean_timezone(data["assumed_timezone"])
    if "biomedical_asset_id" in data:
        out["biomedical_asset_id"] = _check_asset(conn, data["biomedical_asset_id"])
    return out


def _public_fields(row: dict) -> dict:
    """The audited picture of an instrument: never the key's digest."""
    return {k: row.get(k) for k in ("id",) + _INSTRUMENT_EDITABLE + ("enabled",)}


def _stored(conn, ident: str) -> Optional[dict]:
    r = conn.execute(
        "SELECT id, name, make, model, serial, section_id, protocol, transport, host, port, "
        "assumed_timezone, biomedical_asset_id, enabled, credential_hash, credential_rotated_at, "
        "version FROM lab_instruments WHERE id = ?", (ident,)).fetchone()
    return dict(r) if r is not None else None


def _id_or_refuse(value: Any) -> str:
    ident = normalise_instrument_id(value)
    if ident is None:
        raise LabInstrumentError(
            "invalid_id",
            "The analyser id is 2 to 32 letters, digits, '-' or '_', for example XN1000-1.")
    return ident


def create_instrument(data: dict, *, actor: dict, request_meta: Optional[dict] = None) -> dict:
    """Register an analyser. It starts switched off and with no key."""
    _require(actor, LAB_INTERFACE, _INTERFACE_ONLY)
    unknown = set(data) - set(_INSTRUMENT_EDITABLE) - {"id"}
    if unknown:
        raise LabInstrumentError("not_editable", f"Unknown fields: {', '.join(sorted(unknown))}.")
    ident = _id_or_refuse(data.get("id"))
    now = utc_iso_now()
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        if conn.execute("SELECT 1 FROM lab_instruments WHERE id = ?", (ident,)).fetchone():
            raise LabInstrumentError("duplicate", f"An analyser with the id {ident} is already registered.",
                                     409)
        fields = {"protocol": "hl7v2", "transport": "http"}
        fields.update(_validated_fields(conn, {"name": data.get("name"), **{
            k: v for k, v in data.items() if k in _INSTRUMENT_EDITABLE}}))
        row = {"id": ident, **fields, "enabled": 0, "version": 1, "created_at": now,
               "created_by": actor["id"], "updated_at": now, "updated_by": actor["id"]}
        cols = ", ".join(row)
        conn.execute(f"INSERT INTO lab_instruments ({cols}) VALUES ({', '.join('?' for _ in row)})",
                     tuple(row.values()))
        _audit(conn, actor, "lab_instrument_registered", target_type="lab_instrument",
               target_id=ident, summary=f"Analyser registered: {row['name']} ({ident})",
               metadata={"after": _public_fields(row)}, request_meta=request_meta)
        return get_instrument(conn, ident)


def update_instrument(instrument_id: str, changes: dict, *, actor: dict,
                      expected_version: Optional[int] = None,
                      request_meta: Optional[dict] = None) -> dict:
    """Change an analyser's details. Its id never changes; its key and its
    on or off state have their own steps."""
    _require(actor, LAB_INTERFACE, _INTERFACE_ONLY)
    unknown = set(changes) - set(_INSTRUMENT_EDITABLE)
    if unknown:
        raise LabInstrumentError("not_editable", f"These cannot be changed here: {', '.join(sorted(unknown))}.")
    if not changes:
        raise LabInstrumentError("missing_field", "Nothing to change.")
    ident = _id_or_refuse(instrument_id)
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        before = _stored(conn, ident)
        if before is None:
            raise LabInstrumentError("not_found", "No such analyser.", 404)
        if expected_version is not None and int(expected_version) != int(before["version"]):
            raise LabInstrumentError(
                "stale_version", "Someone changed this analyser since you opened it. Reload and try again.",
                409)
        fields = _validated_fields(conn, changes)
        changed = {k: v for k, v in fields.items() if before.get(k) != v}
        if not changed:
            return get_instrument(conn, ident)
        now = utc_iso_now()
        changed_sql = dict(changed, version=int(before["version"]) + 1, updated_at=now,
                           updated_by=actor["id"])
        assignments = ", ".join(f"{k} = ?" for k in changed_sql)
        conn.execute(f"UPDATE lab_instruments SET {assignments} WHERE id = ?",
                     (*changed_sql.values(), ident))
        _audit(conn, actor, "lab_instrument_changed", target_type="lab_instrument",
               target_id=ident,
               summary=f"Analyser changed: {changed.get('name', before['name'])} ({ident})",
               metadata={"before": {k: before.get(k) for k in changed},
                         "after": changed, "version": changed_sql["version"]},
               request_meta=request_meta)
        return get_instrument(conn, ident)


def set_instrument_enabled(instrument_id: str, enabled: bool, reason: Any, *, actor: dict,
                           request_meta: Optional[dict] = None) -> dict:
    """Switch an analyser on (it may then send results, once intake exists)
    or off (everything it sends is refused). Both need a reason; switching on
    needs a key."""
    _require(actor, LAB_INTERFACE, _INTERFACE_ONLY)
    if not isinstance(enabled, bool):
        raise LabInstrumentError("invalid_value", "Say whether the analyser is switched on or off.")
    why = _clean_text(reason, "Reason", required=True, max_len=NOTE_MAX)
    ident = _id_or_refuse(instrument_id)
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        before = _stored(conn, ident)
        if before is None:
            raise LabInstrumentError("not_found", "No such analyser.", 404)
        if bool(before["enabled"]) == enabled:
            raise LabInstrumentError(
                "no_change", f"This analyser is already switched {'on' if enabled else 'off'}.", 409)
        if enabled:
            if before["credential_hash"] is None:
                raise LabInstrumentError(
                    "key_required", "Issue this analyser a key before switching it on.", 409)
            if before["protocol"] not in SUPPORTED_PROTOCOLS or \
                    before["transport"] not in SUPPORTED_TRANSPORTS:
                raise LabInstrumentError(
                    "not_supported", "This analyser's connection is not supported in this version "
                    "of Orb yet.", 409)
        now = utc_iso_now()
        version = int(before["version"]) + 1
        conn.execute("UPDATE lab_instruments SET enabled = ?, status_reason = ?, version = ?, "
                     "updated_at = ?, updated_by = ? WHERE id = ?",
                     (1 if enabled else 0, why, version, now, actor["id"], ident))
        _audit(conn, actor, "lab_instrument_enabled" if enabled else "lab_instrument_disabled",
               target_type="lab_instrument", target_id=ident,
               summary=f"Analyser switched {'on' if enabled else 'off'}: {before['name']} ({ident})",
               metadata={"enabled": enabled, "reason": why, "version": version},
               request_meta=request_meta)
        return get_instrument(conn, ident)


def issue_key(instrument_id: str, *, actor: dict, reason: Any = None,
              request_meta: Optional[dict] = None) -> dict:
    """Issue the analyser a new key and return it, ONCE.

    The answer is {"instrument", "key", "issued_at", "replaced"}. Only the
    digest is stored. Replacing a key needs a reason, and the old key stops
    working in the same transaction. Neither the key nor its digest is ever
    put in the audit row."""
    _require(actor, LAB_INTERFACE, _INTERFACE_ONLY)
    why = _clean_text(reason, "Reason", max_len=NOTE_MAX)
    ident = _id_or_refuse(instrument_id)
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        before = _stored(conn, ident)
        if before is None:
            raise LabInstrumentError("not_found", "No such analyser.", 404)
        replacing = before["credential_hash"] is not None
        if replacing and why is None:
            raise LabInstrumentError(
                "reason_required", "Say why the key is being replaced, for example a service "
                "visit or a key that may have been seen.")
        key = new_key()
        now = utc_iso_now()
        version = int(before["version"]) + 1
        conn.execute("UPDATE lab_instruments SET credential_hash = ?, credential_rotated_at = ?, "
                     "credential_rotated_by = ?, version = ?, updated_at = ?, updated_by = ? "
                     "WHERE id = ?",
                     (hash_key(key), now, actor["id"], version, now, actor["id"], ident))
        _audit(conn, actor,
               "lab_instrument_credential_rotated" if replacing else "lab_instrument_credential_issued",
               target_type="lab_instrument", target_id=ident,
               summary=(f"Analyser key replaced: {before['name']} ({ident})" if replacing
                        else f"Analyser key issued: {before['name']} ({ident})"),
               metadata={"replaced": replacing, "reason": why,
                         "previous_issued_at": before["credential_rotated_at"],
                         "enabled": bool(before["enabled"]), "version": version},
               request_meta=request_meta)
        answer = {"instrument": get_instrument(conn, ident), "key": key, "issued_at": now,
                  "replaced": replacing}
    return answer


# ════════════════════════════════════════════════════════════════════════════
#  The code map (lab.master)
# ════════════════════════════════════════════════════════════════════════════

_MASTER_ONLY = "Only a pathologist can change which test an analyser code is."


def _clean_code(value: Any) -> str:
    code = _clean_text(value, "Analyser code", required=True, max_len=CODE_MAX)
    if _CODE_FORBIDDEN.search(code):
        raise LabInstrumentError("invalid_value",
                                 "An analyser code cannot contain | ^ ~ \\ or &.")
    return code


def _map_audit_picture(row: Optional[dict]) -> Optional[dict]:
    if row is None:
        return None
    return {k: row.get(k) for k in ("test_id", "factor_milli", "instrument_unit", "active",
                                    "note", "retire_reason", "version")}


def _stored_mapping(conn, ident: str, code: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM lab_instrument_test_map WHERE instrument_id = ? "
                     "AND instrument_code = ?", (ident, code)).fetchone()
    return dict(r) if r is not None else None


def _mapping_out(conn, ident: str, code: str) -> dict:
    r = conn.execute(_MAP_SELECT + " WHERE m.instrument_id = ? AND m.instrument_code = ?",
                     (ident, code)).fetchone()
    return _mapping_row(r)


def save_mapping(instrument_id: str, data: dict, *, actor: dict,
                 request_meta: Optional[dict] = None) -> dict:
    """Map one analyser code to an Orb test, or change or bring back that
    mapping. Body: instrument_code, test_id, factor (default 1),
    instrument_unit (number tests only), note (required)."""
    _require(actor, LAB_MASTER, _MASTER_ONLY)
    unknown = set(data) - {"instrument_code", "test_id", "factor", "instrument_unit", "note"}
    if unknown:
        raise LabInstrumentError("not_editable", f"Unknown fields: {', '.join(sorted(unknown))}.")
    ident = _id_or_refuse(instrument_id)
    code = _clean_code(data.get("instrument_code"))
    test_id = _clean_text(data.get("test_id"), "Test", required=True, max_len=128)
    note = _clean_text(data.get("note"), "Note", required=True, max_len=NOTE_MAX)
    unit = _clean_text(data.get("instrument_unit"), "Analyser unit", max_len=UNIT_MAX)
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        instrument = _stored(conn, ident)
        if instrument is None:
            raise LabInstrumentError("not_found", "No such analyser.", 404)
        test = conn.execute("SELECT id, name, kind, active, performed_at, result_type FROM lab_tests "
                            "WHERE id = ?", (test_id,)).fetchone()
        if test is None:
            raise LabInstrumentError("unknown_reference", f"Unknown test: {test_id}.")
        if test["kind"] != "analyte":
            raise LabInstrumentError("not_an_analyte",
                                     "A code maps to one test, never a panel. Map each test in the panel.")
        if not test["active"]:
            raise LabInstrumentError("test_inactive", f"{test['name']} is switched off in the catalogue.")
        if test["performed_at"] != "lab":
            raise LabInstrumentError(
                "test_is_bedside",
                f"{test['name']} is done at the bedside, so its results never come from the laboratory.")
        numeric = test["result_type"] == "numeric"
        factor_milli = _factor_milli(data.get("factor"), numeric=numeric)
        if unit is not None and not numeric:
            raise LabInstrumentError("invalid_value", "Only a test with a number result has a unit.")
        twin = conn.execute(
            "SELECT instrument_code FROM lab_instrument_test_map WHERE instrument_id = ? "
            "AND instrument_code = ? COLLATE NOCASE AND instrument_code != ?",
            (ident, code, code)).fetchone()
        if twin is not None:
            raise LabInstrumentError(
                "code_case_twin",
                f"This analyser already has the code {twin['instrument_code']}, which differs from "
                f"{code} only in capitals. Change that one instead.", 409)
        other = conn.execute(
            "SELECT instrument_code FROM lab_instrument_test_map WHERE instrument_id = ? "
            "AND test_id = ? AND active = 1 AND instrument_code != ?",
            (ident, test_id, code)).fetchone()
        if other is not None:
            raise LabInstrumentError(
                "test_already_mapped",
                f"{test['name']} already comes from the code {other['instrument_code']} on this "
                "analyser. Retire that mapping first.", 409)
        before = _stored_mapping(conn, ident, code)
        now = utc_iso_now()
        if before is None:
            conn.execute(
                "INSERT INTO lab_instrument_test_map (instrument_id, instrument_code, test_id, "
                "factor_milli, instrument_unit, active, note, approved_by, approved_at, version) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 1)",
                (ident, code, test_id, factor_milli, unit, note, actor["id"], now))
            change = "added"
        else:
            same = (before["active"] == 1 and before["test_id"] == test_id
                    and before["factor_milli"] == factor_milli
                    and (before["instrument_unit"] or None) == unit and before["note"] == note)
            if same:
                return _mapping_out(conn, ident, code)
            change = "changed" if before["active"] == 1 else "brought_back"
            conn.execute(
                "UPDATE lab_instrument_test_map SET test_id = ?, factor_milli = ?, "
                "instrument_unit = ?, active = 1, note = ?, approved_by = ?, approved_at = ?, "
                "retired_by = NULL, retired_at = NULL, retire_reason = NULL, version = version + 1 "
                "WHERE instrument_id = ? AND instrument_code = ?",
                (test_id, factor_milli, unit, note, actor["id"], now, ident, code))
        after = _stored_mapping(conn, ident, code)
        _audit(conn, actor, "lab_analyser_map_changed", target_type="lab_instrument",
               target_id=ident,
               summary=f"Analyser code {code} on {instrument['name']} ({ident}) {change.replace('_', ' ')}: "
                       f"{test['name']}",
               metadata={"instrument_id": ident, "instrument_code": code, "change": change,
                         "before": _map_audit_picture(before), "after": _map_audit_picture(after)},
               request_meta=request_meta)
        return _mapping_out(conn, ident, code)


def retire_mapping(instrument_id: str, instrument_code: Any, reason: Any, *, actor: dict,
                   request_meta: Optional[dict] = None) -> dict:
    """Retire one mapping with a reason. It is kept, so a result entered from
    it can always be traced; a result with this code is then quarantined."""
    _require(actor, LAB_MASTER, _MASTER_ONLY)
    ident = _id_or_refuse(instrument_id)
    code = _clean_code(instrument_code)
    why = _clean_text(reason, "Reason", required=True, max_len=NOTE_MAX)
    with db_session(immediate=True) as conn:
        _require_tables(conn)
        instrument = _stored(conn, ident)
        if instrument is None:
            raise LabInstrumentError("not_found", "No such analyser.", 404)
        before = _stored_mapping(conn, ident, code)
        if before is None:
            raise LabInstrumentError("not_found", f"No mapping for the code {code} on this analyser.", 404)
        if before["active"] != 1:
            raise LabInstrumentError("no_change", "This mapping is already retired.", 409)
        now = utc_iso_now()
        conn.execute(
            "UPDATE lab_instrument_test_map SET active = 0, retired_by = ?, retired_at = ?, "
            "retire_reason = ?, version = version + 1 WHERE instrument_id = ? AND instrument_code = ?",
            (actor["id"], now, why, ident, code))
        after = _stored_mapping(conn, ident, code)
        _audit(conn, actor, "lab_analyser_map_changed", target_type="lab_instrument",
               target_id=ident,
               summary=f"Analyser code {code} on {instrument['name']} ({ident}) retired",
               metadata={"instrument_id": ident, "instrument_code": code, "change": "retired",
                         "before": _map_audit_picture(before), "after": _map_audit_picture(after)},
               request_meta=request_meta)
        return _mapping_out(conn, ident, code)
