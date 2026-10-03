"""THE registration rule for a patient's name.

One predicate, imported by every door a patient record can come in through:
the Vigil create-patient route, the admission service (and so the admit
route), the HIS roster importer and the history importer. A name that fails
here is refused before anything is written, and the caller says why.

The rule: a name must contain at least two letters, in any script. What it
refuses is not a name at all: a bare initial ("S"), a placeholder ("X", "-"),
a number. Why two letters and not more: initials-only names ("S. R.") and
short mononyms ("Om", "Li") are real values on an Indian ward and stay
importable (the importers flag initials as a warning, which is the right
level for something a bedside identity check can still act on).

Why the rule exists (2026-09-05): Sage's name matchers refuse to match a name
with fewer than two letters (services/sage_service.py, _whole_name_in_query
and _name_match), because a one-letter name is what ward shorthand is made of
("s/p", "x 3 days", "K 3.2") and what a possessive tokenises to. A record
that no clinician can reach by name should not be registrable by name
either; the two floors are kept equal and parity-tested in
tests/test_patient_name_rule.py. Letters are counted with str.isalpha so a
Devanagari or Tamil name passes here even though the matchers, which are
ASCII-only, will not find it; that limitation is the matchers', not the
patient's.
"""

MIN_NAME_LETTERS = 2

NAME_TOO_SHORT_MESSAGE = "Patient name must contain at least two letters."
NAME_REQUIRED_MESSAGE = "name is required"


class PatientNameError(ValueError):
    """A value that cannot serve as a patient's name. HTTP routes map it to 400;
    the importers record it as a rejected row."""


def name_letter_count(name) -> int:
    """Letters in any script; spaces, punctuation and digits do not count."""
    return sum(1 for ch in (name or "") if ch.isalpha())


def name_is_too_short(name) -> bool:
    return name_letter_count(name) < MIN_NAME_LETTERS


def validate_patient_name(name) -> str:
    """Return the name stripped of surrounding whitespace, or raise
    PatientNameError with the message the caller should show."""
    if not isinstance(name, str) or not name.strip():
        raise PatientNameError(NAME_REQUIRED_MESSAGE)
    stripped = name.strip()
    if name_is_too_short(stripped):
        raise PatientNameError(NAME_TOO_SHORT_MESSAGE)
    return stripped
