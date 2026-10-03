"""Orb Hospital OS — HL7 v2 message parser (encoding-aware).

A real HL7 v2 parser, not `split("|")`. Bedside monitors and their gateways are
the sloppiest senders in a hospital: they emit repetition separators, escape
sequences, sub-components, custom encoding characters, trailing empty fields and
segments nobody documented. Every one of those is NORMAL device output, and a
parser that string-splits either loses the reading or raises into the ingestion
loop.

The hierarchy this implements (HL7 v2.x, "ER7" pipe-and-hat encoding):

    message   → segments        separated by \\r (or \\n / \\r\\n from a file)
    segment   → fields          separated by MSH-1                (default `|`)
    field     → repetitions     separated by MSH-2 repetition     (default `~`)
    repetition→ components      separated by MSH-2 component      (default `^`)
    component → sub-components  separated by MSH-2 sub-component  (default `&`)

Field numbering is HL7's, 1-based, and MSH is special-cased the way the standard
defines it: MSH-1 *is* the field separator character and MSH-2 *is* the encoding
character string, so every later MSH field sits one physical position earlier
than in any other segment. The previous parser papered over this with a raw
index that happened to land right; here it is explicit, because getting it wrong
by one silently reads the wrong field on the one message type we care about.

Nothing in this module touches the database, the event loop or the network — it
is pure string work, so it is safe to call from anywhere (including inside a
`run_db` worker).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional, Union

try:  # stdlib since 3.9; the appliance ships 3.12
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - defensive, keeps the bench sim portable
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass


class Hl7ParseError(ValueError):
    """Raised when a payload is not recoverable as an HL7 v2 message.

    Callers in this package treat it as ordinary device output, never as an
    exception that may reach the ingestion loop.
    """


# ════════════════════════════════════════════
#  Encoding characters (MSH-1 / MSH-2)
# ════════════════════════════════════════════

@dataclass(frozen=True)
class EncodingCharacters:
    field: str = "|"
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"

    @classmethod
    def from_msh(cls, msh_segment: str) -> "EncodingCharacters":
        """Read the separators out of an MSH segment header.

        `MSH` is followed immediately by the field separator, then the encoding
        characters field. A monitor that uses non-default separators (rare, but
        legal, and some Indian OEM gateways do it) parses correctly here and
        would silently mis-parse under a hard-coded `|`/`^`.
        """
        if len(msh_segment) < 4:
            raise Hl7ParseError("MSH segment too short to carry encoding characters")
        field_sep = msh_segment[3]
        if field_sep in ("\r", "\n"):
            raise Hl7ParseError("MSH segment has no field separator")
        enc = msh_segment[4:msh_segment.find(field_sep, 4)] if field_sep in msh_segment[4:] else msh_segment[4:]
        # Pad with defaults: a sender may legally send fewer than four encoding
        # characters (only `^~\&` in full is universal, but `^~\` appears).
        comp = enc[0] if len(enc) > 0 else "^"
        rep = enc[1] if len(enc) > 1 else "~"
        esc = enc[2] if len(enc) > 2 else "\\"
        sub = enc[3] if len(enc) > 3 else "&"
        return cls(field=field_sep, component=comp, repetition=rep,
                   escape=esc, subcomponent=sub)


# ════════════════════════════════════════════
#  Escape sequence decoding
# ════════════════════════════════════════════

_HEX_ESCAPE = re.compile(r"^X([0-9A-Fa-f]+)$")


def unescape(text: str, enc: EncodingCharacters) -> str:
    """Decode HL7 escape sequences (`\\F\\`, `\\S\\`, `\\X0A\\`, …).

    An undecoded `\\S\\` inside an observation name is the difference between
    "Sp02^Pulse Ox" reading as one name and as two. Unknown escapes are left
    verbatim rather than dropped — losing characters from a clinical field is
    worse than showing an escape sequence a human can recognise.
    """
    if not text or enc.escape not in text:
        return text

    out: list[str] = []
    i = 0
    n = len(text)
    esc = enc.escape
    while i < n:
        ch = text[i]
        if ch != esc:
            out.append(ch)
            i += 1
            continue
        end = text.find(esc, i + 1)
        if end == -1:
            # Unterminated escape — emit the rest literally.
            out.append(text[i:])
            break
        code = text[i + 1:end]
        i = end + 1
        if code == "F":
            out.append(enc.field)
        elif code == "S":
            out.append(enc.component)
        elif code == "T":
            out.append(enc.subcomponent)
        elif code == "R":
            out.append(enc.repetition)
        elif code == "E":
            out.append(enc.escape)
        elif code in (".br", ".BR"):
            out.append("\n")
        else:
            m = _HEX_ESCAPE.match(code)
            if m:
                try:
                    out.append(bytes.fromhex(m.group(1)).decode("utf-8", errors="replace"))
                except ValueError:
                    out.append(f"{esc}{code}{esc}")
            else:
                # Formatting/locally-defined escape we do not interpret.
                out.append(f"{esc}{code}{esc}")
    return "".join(out)


# ════════════════════════════════════════════
#  Segment
# ════════════════════════════════════════════

class Segment:
    """One HL7 segment with 1-based, HL7-numbered field access."""

    __slots__ = ("name", "raw", "_parts", "_enc", "_is_msh")

    def __init__(self, raw: str, enc: EncodingCharacters):
        self.raw = raw
        self._enc = enc
        self._parts = raw.split(enc.field)
        self.name = self._parts[0][:3] if self._parts and self._parts[0] else ""
        self._is_msh = self.name == "MSH"

    # ── raw field access ──

    def _raw_field(self, index: int) -> str:
        """Physical field string for HL7 field number `index` (1-based).

        MSH is offset by one: MSH-1 is the field separator itself, which is not
        a list element, so MSH-2 lands at `_parts[1]`, MSH-3 at `_parts[2]`, …
        Every other segment has its name at `_parts[0]` and field N at
        `_parts[N]`.
        """
        if index < 1:
            return ""
        if self._is_msh:
            if index == 1:
                return self._enc.field
            pos = index - 1
        else:
            pos = index
        if pos >= len(self._parts):
            return ""
        return self._parts[pos]

    def repeats(self, index: int) -> list[str]:
        """All repetitions of a field (`~`-separated). Always at least one item."""
        raw = self._raw_field(index)
        if self._is_msh and index == 2:
            # MSH-2 IS the encoding characters; splitting it on its own
            # repetition separator would shred it.
            return [raw]
        return raw.split(self._enc.repetition) if raw else [""]

    def components(self, index: int, repeat: int = 0) -> list[str]:
        """Components of one repetition, already unescaped."""
        reps = self.repeats(index)
        if repeat >= len(reps):
            return [""]
        return [unescape(c, self._enc) for c in reps[repeat].split(self._enc.component)]

    def field(self, index: int, repeat: int = 0, component: Optional[int] = None,
              subcomponent: Optional[int] = None) -> str:
        """Read a field / component / sub-component, unescaped and stripped.

        `component` and `subcomponent` are 1-based HL7 positions. Out-of-range
        access returns "" rather than raising — a short segment from a device is
        the common case, not an error.
        """
        if component is None:
            reps = self.repeats(index)
            if repeat >= len(reps):
                return ""
            if self._is_msh and index == 2:
                return reps[repeat]
            return unescape(reps[repeat], self._enc).strip()
        comps = self.components(index, repeat)
        if component < 1 or component > len(comps):
            return ""
        value = comps[component - 1]
        if subcomponent is None:
            return value.strip()
        subs = value.split(self._enc.subcomponent)
        if subcomponent < 1 or subcomponent > len(subs):
            return ""
        return subs[subcomponent - 1].strip()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Segment {self.name} fields={len(self._parts)}>"


# ════════════════════════════════════════════
#  Message
# ════════════════════════════════════════════

_SEGMENT_SPLIT = re.compile(r"[\r\n]+")
# HL7 segment IDs are three upper-case alphanumerics (Z-segments included).
_SEGMENT_ID = re.compile(r"^[A-Z][A-Z0-9]{2}")


class Hl7Message:
    """A parsed HL7 v2 message.

    Construction is total for anything that starts with an MSH segment; garbage
    between segments is skipped and counted rather than raised, because a serial
    gateway that injects a stray line must not take out a whole vitals message.
    """

    def __init__(self, raw: str):
        if raw is None:
            raise Hl7ParseError("Empty HL7 payload")
        # Strip MLLP framing bytes if a caller handed us an unframed buffer, and
        # any BOM / leading whitespace a file round-trip added.
        text = raw.replace("\x0b", "").replace("\x1c", "").lstrip("﻿ \t\r\n")
        if not text.strip():
            raise Hl7ParseError("Empty HL7 payload")
        if not text.startswith("MSH"):
            raise Hl7ParseError("Not an HL7 v2 message: no leading MSH segment")

        first_line = _SEGMENT_SPLIT.split(text, maxsplit=1)[0]
        self.encoding = EncodingCharacters.from_msh(first_line)

        self.segments: list[Segment] = []
        self.skipped_lines: list[str] = []
        for line in _SEGMENT_SPLIT.split(text):
            line = line.strip("\r\n")
            if not line.strip():
                continue
            if not _SEGMENT_ID.match(line):
                self.skipped_lines.append(line[:80])
                continue
            self.segments.append(Segment(line, self.encoding))

        if not self.segments or self.segments[0].name != "MSH":
            raise Hl7ParseError("Not an HL7 v2 message: no MSH segment")

    # ── segment lookup ──

    def first(self, name: str) -> Optional[Segment]:
        for seg in self.segments:
            if seg.name == name:
                return seg
        return None

    def all(self, name: str) -> list[Segment]:
        return [seg for seg in self.segments if seg.name == name]

    # ── MSH conveniences ──

    @property
    def msh(self) -> Segment:
        return self.segments[0]

    @property
    def sending_application(self) -> str:
        return self.msh.field(3, component=1)

    @property
    def sending_facility(self) -> str:
        return self.msh.field(4, component=1)

    @property
    def message_datetime(self) -> str:
        return self.msh.field(7)

    @property
    def message_type(self) -> str:
        """MSH-9.1 — e.g. `ORU`."""
        return self.msh.field(9, component=1)

    @property
    def trigger_event(self) -> str:
        """MSH-9.2 — e.g. `R01`."""
        return self.msh.field(9, component=2)

    @property
    def control_id(self) -> str:
        return self.msh.field(10)

    @property
    def processing_id(self) -> str:
        return self.msh.field(11, component=1)

    @property
    def version_id(self) -> str:
        return self.msh.field(12, component=1)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<Hl7Message {self.message_type}^{self.trigger_event} "
                f"id={self.control_id!r} segments={len(self.segments)}>")


def parse(raw: str) -> Hl7Message:
    """Parse an ER7 HL7 v2 payload. Raises Hl7ParseError only."""
    try:
        return Hl7Message(raw)
    except Hl7ParseError:
        raise
    except Exception as e:  # defensive: no unexpected type may escape a parser
        raise Hl7ParseError(f"Unparseable HL7 payload: {e}") from e


# ════════════════════════════════════════════
#  HL7 timestamps
# ════════════════════════════════════════════

_TS_OFFSET = re.compile(r"([+-]\d{4})$")

# Environment variable naming the zone an OFFSET-LESS DTM is understood to be in.
# An IANA name ("Asia/Kolkata"), because a fixed offset cannot express DST and we
# must not bake "India never shifts" into a parser other sites will reuse.
TZ_ENV_VAR = "ORB_HL7_DEFAULT_TZ"

# Resolved lazily and cached; `set_default_timezone(None)` re-reads the env.
_default_tz: Optional[tzinfo] = None
_default_tz_resolved = False


def resolve_timezone(value: Union[str, tzinfo, None]) -> Optional[tzinfo]:
    """Turn an IANA name / tzinfo / `+05:30`-style offset into a tzinfo.

    Returns None for an unusable value rather than raising: a typo in a config
    file must degrade to "use the appliance's own zone", not stop the ward's
    monitor feed from parsing timestamps.
    """
    if value is None:
        return None
    if isinstance(value, tzinfo):
        return value
    name = str(value).strip()
    if not name:
        return None
    if name.upper() in ("UTC", "Z", "GMT"):
        return timezone.utc
    if name.upper() == "LOCAL":
        return datetime.now().astimezone().tzinfo
    m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})", name)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return None
    return None


def appliance_timezone() -> tzinfo:
    """The zone an offset-less DTM is assumed to be in, when nobody says otherwise.

    THIS IS A CLINICAL SETTING, NOT A FORMATTING ONE. An HL7 DTM with no offset
    is legal, and it is what most bedside monitors emit: they print their own
    local wall-clock time. Reading it as UTC in a hospital that is UTC+05:30
    stamps every reading 5.5 hours in the FUTURE — which corrupts ordering
    against every other feed and, combined with any wall-clock liveness rule,
    can mute a patient's alerting outright.

    Resolution order, most explicit first:
      1. `ORB_HL7_DEFAULT_TZ` (an IANA name — set this on the appliance),
      2. the appliance's own OS timezone,
      3. UTC, only if the OS will not tell us (containers with no /etc/localtime).

    "The appliance's local zone" is the right default because the appliance sits
    on the same ward as the monitors: if it is set to Asia/Kolkata, so are they.
    A per-device override exists for the case where they are NOT (see
    `HL7MonitorSource(device_timezone=...)`) — assuming every device on a ward
    agrees is how this bug bites a second time.
    """
    global _default_tz, _default_tz_resolved
    if not _default_tz_resolved:
        _default_tz = resolve_timezone(os.getenv(TZ_ENV_VAR))
        if _default_tz is None:
            _default_tz = datetime.now().astimezone().tzinfo or timezone.utc
        _default_tz_resolved = True
    return _default_tz or timezone.utc


def set_default_timezone(value: Union[str, tzinfo, None]) -> tzinfo:
    """Override the assumed zone process-wide. `None` re-reads the environment.

    Exists so a deployment (and the test suite) can state the assumption out
    loud instead of inheriting whatever the OS happened to be set to.
    """
    global _default_tz, _default_tz_resolved
    resolved = resolve_timezone(value)
    if value is not None and resolved is None:
        raise ValueError(f"unusable timezone: {value!r}")
    _default_tz = resolved
    _default_tz_resolved = resolved is not None
    return appliance_timezone()


@dataclass(frozen=True)
class Hl7Timestamp:
    """A parsed DTM, plus what we had to ASSUME to parse it.

    `had_offset` is the honest bit: False means the device told us a wall-clock
    reading and we supplied the zone. Anything that reasons about clock skew has
    to know the difference between "the device and I disagree by 20 minutes" and
    "the device never said which planet it was on".
    """
    iso: str                     # ISO 8601, UTC
    dt: datetime                 # the same instant, aware, UTC
    had_offset: bool             # the DTM carried an explicit +/-ZZZZ
    assumed_timezone: str        # the zone we applied when it did not
    precision: str               # second | minute | hour | day


def parse_timestamp_detail(
    ts: str, *, default_tz: Union[str, tzinfo, None] = None,
) -> Optional[Hl7Timestamp]:
    """HL7 DTM (`yyyyMMddHHmmss[.S][+/-ZZZZ]`) → an `Hl7Timestamp`, or None.

    Two rules, and the second one is the fix for a patient-safety defect:

      1. An offset that IS present is APPLIED, not stripped. `20260801143000+0530`
         is 09:00 UTC; discarding the offset back-dates the reading by 5.5 h.
      2. An offset that is ABSENT means the device's own local time. We apply
         `default_tz` — the per-device setting if one is configured, else the
         appliance's zone. We do NOT assume UTC: that is only correct in a
         hospital at Greenwich, and this one is in India.
    """
    if not ts:
        return None
    text = str(ts).strip()
    if not text:
        return None

    offset: Optional[timezone] = None
    m = _TS_OFFSET.search(text)
    if m:
        sign = 1 if m.group(1)[0] == "+" else -1
        offset = timezone(sign * timedelta(hours=int(m.group(1)[1:3]),
                                           minutes=int(m.group(1)[3:5])))
        text = text[: m.start()]
    text = text.split(".")[0]  # drop fractional seconds
    digits = "".join(ch for ch in text if ch.isdigit())

    for fmt, width, precision in (
        ("%Y%m%d%H%M%S", 14, "second"),
        ("%Y%m%d%H%M", 12, "minute"),
        ("%Y%m%d%H", 10, "hour"),
        ("%Y%m%d", 8, "day"),
    ):
        if len(digits) < width:
            continue
        try:
            naive = datetime.strptime(digits[:width], fmt)
        except ValueError:
            continue
        if offset is not None:
            aware = naive.replace(tzinfo=offset)
            assumed = "explicit"
        else:
            zone = resolve_timezone(default_tz) or appliance_timezone()
            # `replace(tzinfo=ZoneInfo(...))` is correct for a wall-clock reading:
            # ZoneInfo resolves the UTC offset for THAT date, so a zone with DST
            # is handled without us hard-coding one. (IST has no DST, which is
            # why this hospital is the easy case — other sites will not be.)
            aware = naive.replace(tzinfo=zone)
            assumed = str(getattr(zone, "key", None) or aware.tzname() or zone)
        utc = aware.astimezone(timezone.utc)
        return Hl7Timestamp(iso=utc.isoformat(), dt=utc,
                            had_offset=offset is not None,
                            assumed_timezone=assumed, precision=precision)
    return None


def parse_timestamp(ts: str, *, default_tz: Union[str, tzinfo, None] = None) -> Optional[str]:
    """HL7 DTM → ISO 8601 UTC string. See `parse_timestamp_detail` for the rules."""
    detail = parse_timestamp_detail(ts, default_tz=default_tz)
    return detail.iso if detail is not None else None


def format_timestamp(dt: Optional[datetime] = None, *,
                     tz: Union[str, tzinfo, None] = None,
                     with_offset: bool = False) -> str:
    """Render an HL7 DTM.

    Defaults to UTC without an offset, which is what Orb's ACKs have always
    carried. `with_offset=True` appends `+ZZZZ` — worth doing on anything a
    device might read back, because an offset-less DTM is precisely the
    ambiguity this module exists to stop guessing about.
    """
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    zone = resolve_timezone(tz) or timezone.utc
    local = dt.astimezone(zone)
    return local.strftime("%Y%m%d%H%M%S%z" if with_offset else "%Y%m%d%H%M%S")


# ════════════════════════════════════════════
#  ACK construction
# ════════════════════════════════════════════

# HL7 acknowledgement codes we emit.
ACK_ACCEPT = "AA"       # accepted
ACK_ERROR = "AE"        # application error — sender SHOULD retry
ACK_REJECT = "AR"       # rejected — sender should NOT retry unchanged

_ACK_TEXT_SAFE = re.compile(r"[\r\n|^~\\&]")


def build_ack(message: Optional[Hl7Message], code: str = ACK_ACCEPT,
              text: str = "", *, receiving_app: str = "ORB",
              receiving_facility: str = "ORB_OS",
              control_id: Optional[str] = None) -> str:
    """Build an ACK^R01 response.

    `message` may be None when the inbound payload could not be parsed at all —
    the sender still needs a reply or it will block on its own ACK timeout and
    stop sending vitals entirely. In that case we echo an empty control id,
    which is what the standard leaves us and what every gateway tolerates.

    The ACK text is sanitised of HL7 delimiters and CR/LF: an error string built
    from device input could otherwise inject extra fields or segments into our
    own reply.
    """
    enc = message.encoding if message else EncodingCharacters()
    sending_app = message.sending_application if message else ""
    sending_fac = message.sending_facility if message else ""
    msg_control = control_id if control_id is not None else (message.control_id if message else "")
    processing = (message.processing_id if message else "") or "P"
    version = (message.version_id if message else "") or "2.5"
    safe_text = _ACK_TEXT_SAFE.sub(" ", text or "")[:180]

    f = enc.field
    c = enc.component
    msh = f.join([
        "MSH",
        f"{c}{enc.repetition}{enc.escape}{enc.subcomponent}",
        receiving_app, receiving_facility,
        sending_app, sending_fac,
        # MSH-7 carries its offset explicitly (`+0000`). We spend this module
        # arguing that an offset-less DTM is a guess the receiver should not have
        # to make; emitting one ourselves would be the same defect pointed the
        # other way. The control id below stays bare — it is an opaque token.
        format_timestamp(with_offset=True), "",
        f"ACK{c}R01", f"ORB{format_timestamp()}", processing, version,
    ])
    msa = f.join(["MSA", code, msg_control, safe_text]) if safe_text else f.join(["MSA", code, msg_control])
    return f"{msh}\r{msa}\r"
