"""The fixed list of appointment types a clinician can book.

The booking route took any text as the type and defaulted to "Follow-up", so
the same visit was written "Follow-up", "follow up", "FU" and "Clinical Review"
by different hands, and nothing could count, say, the post-discharge checks due
this week. A booking now names one of four types.

The stored value is the plain label ("Follow-up"), the same spelling the
column has defaulted to since the table was created, so every row written
before this list existed still reads as itself. On read, a value that is not
one of the four (an old free-text type, or the portal's "Patient request:
confirm time") is counted as `other`; its text is still shown as written.
"""
from __future__ import annotations

#: key -> the label a nurse reads, which is also what is stored.
APPOINTMENT_TYPES: dict[str, str] = {
    "follow_up": "Follow-up",
    "medication_review": "Medication review",
    "post_discharge_check": "Post-discharge check",
    "other": "Other",
}

DEFAULT_TYPE = "follow_up"
OTHER = "other"


def _fold(value) -> str:
    return " ".join(str(value or "").replace("_", " ").replace("-", " ").lower().split())


_BY_FOLDED = {}
for _key, _label in APPOINTMENT_TYPES.items():
    _BY_FOLDED[_fold(_key)] = _key
    _BY_FOLDED[_fold(_label)] = _key


def parse_type(value) -> str | None:
    """The key a booking names, or None when it names none of the four.

    Accepts the key ("post_discharge_check") or the label in any case
    ("Post-discharge check"), so a client that still sends "Follow-up" keeps
    working. Blank means the default, follow_up.
    """
    if value is None or not str(value).strip():
        return DEFAULT_TYPE
    return _BY_FOLDED.get(_fold(value))


def type_key(stored) -> str:
    """The key for a stored value. Anything not on the list reads as other."""
    if stored is None or not str(stored).strip():
        return DEFAULT_TYPE
    return _BY_FOLDED.get(_fold(stored), OTHER)


def type_label(stored) -> str:
    """What a screen prints: the list's label, or an old row's own text."""
    key = type_key(stored)
    if key != OTHER:
        return APPOINTMENT_TYPES[key]
    text = str(stored or "").strip()
    return text or APPOINTMENT_TYPES[OTHER]
