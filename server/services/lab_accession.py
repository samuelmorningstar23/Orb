"""Laboratory accession numbers (lis-6, ruling LIS-R13).

Every specimen the laboratory plans gets one accession number, and the
specimen is stored under it (lab_specimens.id). The number is printed on the
tube's label, scanned at the bench and typed when a scanner fails, so it is
built to survive all three.

THE FORMAT: 12 digits, numeric only
    YYMMDD   the hospital-local calendar day the specimen was planned on.
             The day rolls over at local MIDNIGHT (services/hospital_time), not
             at the UTC date change, which in an IST hospital falls at 05:30.
    NNNNN    that day's sequence, 00001 to 99999, from lab_accession_counters
             (one row per hospital day, migration 55).
    C        a Damm check digit over the 11 digits before it.

    Numeric only, so older analysers read it and the label can use Code 128
    subset C (all digits, even length). An optional site prefix of one to four
    capital letters may stand in front (multi-site, later); the check digit
    never covers the prefix, which is checked by equality instead.

WHY DAMM. A plain sum of digits modulo 10 detects no transposition at all,
and Luhn misses the 09 and 90 swap. The Damm digit detects every single-digit
error and every swap of two adjacent digits, 09 and 90 included, with no
letter in the alphabet (unlike ISO 7064 mod 11-2, which needs an 'X').

THE SEQUENCE NEVER WRAPS. At 99999 numbers for a day are used up: allocate()
logs an error and raises AccessionOverflow, and the caller refuses the work
and records it where the laboratory will see it (lab_exceptions). Wrapping to
00001 would hand out a number a tube in the laboratory already carries.

CONCURRENCY. allocate() takes the caller's connection, which must already be
inside a write transaction (db_session(immediate=True), BEGIN IMMEDIATE). It
increments the day's counter with one UPDATE and reads it back in the same
transaction, so two allocations can never return the same number, and a
specimen that fails to insert rolls its number back with it (no gaps from
failed work, and no number escapes a rolled-back transaction).

This module never commits, never opens a session and never decides what a
number is for.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

logger = logging.getLogger("orb.lab.accession")

ACCESSION_DIGITS = 12
SEQUENCE_DIGITS = 5
SEQUENCE_MAX = 99999

_PREFIX = re.compile(r"^[A-Z]{1,4}$")

# The Damm operation table: a totally anti-symmetric quasigroup of order 10
# (H. M. Damm, 2004). Row is the interim digit, column the next input digit.
_DAMM = (
    (0, 3, 1, 7, 5, 9, 8, 6, 4, 2),
    (7, 0, 9, 2, 1, 5, 4, 8, 6, 3),
    (4, 2, 0, 6, 8, 7, 1, 3, 5, 9),
    (1, 7, 5, 0, 9, 8, 3, 4, 2, 6),
    (6, 1, 2, 3, 0, 4, 5, 9, 7, 8),
    (3, 6, 7, 4, 2, 0, 9, 5, 8, 1),
    (5, 8, 6, 9, 7, 2, 0, 1, 3, 4),
    (8, 9, 4, 5, 3, 6, 2, 0, 1, 7),
    (9, 4, 3, 8, 6, 1, 7, 2, 0, 5),
    (2, 5, 8, 1, 4, 3, 6, 7, 9, 0),
)


class AccessionOverflow(RuntimeError):
    """Every accession number for a hospital day is used (sequence 99999).
    The work that needed a number is refused; nothing wraps."""

    def __init__(self, hospital_day: str):
        self.hospital_day = hospital_day
        super().__init__(
            f"Accession numbers for {hospital_day} are used up ({SEQUENCE_MAX} specimens "
            "planned that day). No new specimen can be planned until the next day. "
            "Contact Orb support."
        )


class AccessionInvalid(ValueError):
    """A scanned or typed accession number that is not one Orb issued: wrong
    length, a letter where a digit belongs, an impossible date, or a check
    digit that does not match (a misread or mistyped digit)."""


@dataclass(frozen=True)
class AccessionParts:
    text: str
    prefix: str
    hospital_day: date
    sequence: int
    check_digit: int


# ════════════════════════════════════════════════════════════════════════════
#  The check digit
# ════════════════════════════════════════════════════════════════════════════


def _digits(text: str) -> list[int]:
    if not isinstance(text, str) or not text or not text.isascii() or not text.isdigit():
        raise ValueError("digits only")
    return [ord(c) - 48 for c in text]


def damm_check_digit(digits: str) -> int:
    """The Damm check digit for a string of decimal digits."""
    interim = 0
    for d in _digits(digits):
        interim = _DAMM[interim][d]
    return interim


def damm_valid(digits: str) -> bool:
    """True when the last digit is the Damm check digit of the ones before it."""
    try:
        values = _digits(digits)
    except ValueError:
        return False
    interim = 0
    for d in values:
        interim = _DAMM[interim][d]
    return interim == 0


# ════════════════════════════════════════════════════════════════════════════
#  Days, formatting and parsing
# ════════════════════════════════════════════════════════════════════════════


def hospital_day_for(at=None) -> date:
    """The hospital-local calendar day of an instant (now when not given).
    Midnight local is the rollover, never the UTC date change."""
    from services.hospital_time import hospital_today, to_hospital

    if at is None:
        return hospital_today()
    local = to_hospital(at)
    if local is None:
        raise ValueError(f"unreadable instant: {at!r}")
    return local.date()


def _clean_prefix(prefix: str | None) -> str:
    if prefix is None:
        return ""
    text = str(prefix).strip().upper()
    if not _PREFIX.match(text):
        raise ValueError("A site prefix is one to four capital letters.")
    return text


def format_accession(day: date, sequence: int, *, prefix: str | None = None) -> str:
    """'261004' + '00042' + check digit, with the site prefix in front if any."""
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= SEQUENCE_MAX
    ):
        raise ValueError(f"sequence must be 1 to {SEQUENCE_MAX}")
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("day must be a date")
    body = f"{day:%y%m%d}{sequence:0{SEQUENCE_DIGITS}d}"
    return f"{_clean_prefix(prefix)}{body}{damm_check_digit(body)}"


def parse_accession(text: str, *, prefix: str | None = None) -> AccessionParts:
    """Check a scanned or typed accession number and take it apart.

    `prefix` is the site's own prefix (None for a site without one): a number
    must carry exactly that prefix. Surrounding spaces are ignored and letters
    are read in capitals; anything else that is wrong raises AccessionInvalid
    with a sentence a person can act on."""
    expected = _clean_prefix(prefix)
    raw = str(text or "").strip().upper()
    if not raw:
        raise AccessionInvalid("No accession number was given.")
    if expected:
        if not raw.startswith(expected):
            raise AccessionInvalid(f"This accession number does not start with {expected}.")
        body = raw[len(expected) :]
    else:
        body = raw
    if not body.isascii() or not body.isdigit():
        raise AccessionInvalid(
            "An accession number is digits only" + (f" after {expected}." if expected else ".")
        )
    if len(body) != ACCESSION_DIGITS:
        raise AccessionInvalid(
            f"An accession number has {ACCESSION_DIGITS} digits; this one has {len(body)}."
        )
    if not damm_valid(body):
        raise AccessionInvalid(
            "The check digit does not match. Scan the label again, or check each digit you typed."
        )
    try:
        day = datetime.strptime(body[:6], "%y%m%d").date()
    except ValueError:
        raise AccessionInvalid("The date in this accession number is not a real date.") from None
    sequence = int(body[6:11])
    if sequence < 1:
        raise AccessionInvalid("The sequence in this accession number is zero.")
    return AccessionParts(
        text=raw, prefix=expected, hospital_day=day, sequence=sequence, check_digit=int(body[11])
    )


def is_valid_accession(text: str, *, prefix: str | None = None) -> bool:
    try:
        parse_accession(text, prefix=prefix)
    except AccessionInvalid:
        return False
    return True


# ════════════════════════════════════════════════════════════════════════════
#  Allocation
# ════════════════════════════════════════════════════════════════════════════


def allocate(conn, *, at=None, prefix: str | None = None) -> str:
    """The next accession number for the hospital day of `at` (now when not
    given), on the caller's write transaction.

    Raises AccessionOverflow (after logging an error) when the day's sequence
    is at 99999, and RuntimeError when the connection is not inside a
    transaction, because then two callers could read the same counter."""
    if not getattr(conn, "in_transaction", False):
        raise RuntimeError(
            "allocate() needs the caller's write transaction: open it with "
            "db_session(immediate=True) and pass that connection."
        )
    clean_prefix = _clean_prefix(prefix)
    day = hospital_day_for(at)
    key = day.isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO lab_accession_counters (hospital_day, last_seq) VALUES (?, 0)",
        (key,),
    )
    cur = conn.execute(
        "UPDATE lab_accession_counters SET last_seq = last_seq + 1 "
        "WHERE hospital_day = ? AND last_seq < ?",
        (key, SEQUENCE_MAX),
    )
    if cur.rowcount != 1:
        logger.error(
            "Laboratory accession numbers for %s are used up (sequence %d reached); "
            "refusing to plan another specimen that day. Nothing wraps.",
            key,
            SEQUENCE_MAX,
        )
        raise AccessionOverflow(key)
    sequence = int(
        conn.execute(
            "SELECT last_seq FROM lab_accession_counters WHERE hospital_day = ?", (key,)
        ).fetchone()[0]
    )
    return format_accession(day, sequence, prefix=clean_prefix or None)
