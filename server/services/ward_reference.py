"""Is a ward named in a question? ONE rule for Sage retrieval and the PHI classifier.

THE DEFECT (found 4 October 2026). Both lanes squashed the question and the
ward label to letters and digits and tested for a SUBSTRING, which has no word
boundaries left to respect:

  * retrieval (sage_service._match_patients): in a CCU doctor's session, "Does
    atrial fibrillation occur often after cardiac surgery?" and "What is the
    accuracy of troponin?" matched ward CCU inside "occur" and "accuracy", and
    Sage answered a general question with "which patient did you mean?",
    listing every CCU patient by name in the prompt;
  * the PHI classifier (ai_governance._match_patient_identifiers): ward "OR"
    sat inside "for", so nearly every question was labelled ward-scoped PHI,
    which also trips the thinking-mode floor onto the slow model.

The classifier's over-labelling mirrored the retrieval over-match, which is
why the two are fixed together and read this one function: if retrieval can
put a ward's patients in the prompt, the classifier must have said PHI.

THE RULE (ruled by Samuel 2026-10-04):
  * A ward label matches only as WHOLE tokens, in order. Tokens are runs of
    letters or of digits, so letter/digit boundaries split ("GM-1", "gm 1" and
    "gm1" are all ["gm", "1"]), and leading zeros are ignored ("ICU-02" is
    "icu 2"). "GM-1" is not named by "gm 101", "CCU" not by "occur", "OR" not
    by "for".
  * A label that is one common short English word ("OR", "ER", "IN") counts
    only where it is typed in CAPITALS or right after the word "ward": "back
    from OR" and "ward or" name the ward, "A or B" does not. Everything else
    is case-blind.

Beds are NOT read here: bed matching is ruling R026's work
(docs/SAGE_BED_LOCATOR_PLAN_2026-10-03.md) and still uses the substring test.
"""
from __future__ import annotations

import functools
import re

#: Runs of letters or of digits, read off the text as typed (case kept).
_TOKEN_RE = re.compile(r"[A-Za-z]+|[0-9]+")

#: Single-token ward labels that are also ordinary short English words (or a
#: spoken hesitation: "er" in a voice transcript). Such a label is a ward only
#: where it is written in capitals or follows the word "ward".
_WORD_LIKE = frozenset({
    "a", "am", "an", "as", "at", "be", "by", "do", "er", "go", "he", "hi", "if",
    "in", "is", "it", "me", "my", "no", "of", "oh", "ok", "on", "or", "so", "to",
    "um", "up", "us", "we",
    "all", "and", "any", "are", "but", "can", "day", "did", "few", "for", "had",
    "has", "her", "him", "his", "how", "its", "new", "not", "now", "off", "old",
    "one", "our", "out", "own", "per", "see", "she", "the", "too", "two", "use",
    "was", "way", "who", "why", "yes", "yet", "you",
})


def _norm(token: str) -> str:
    """Lower-case; a run of digits loses its leading zeros ("02" is "2")."""
    if token.isdigit():
        return token.lstrip("0") or "0"
    return token.lower()


@functools.lru_cache(maxsize=512)
def _label_tokens(label: str) -> tuple:
    return tuple(_norm(t) for t in _TOKEN_RE.findall(label or ""))


class _TextIndex:
    """One question, tokenised once and shared by every ward test against it."""

    def __init__(self, text: str):
        raw = _TOKEN_RE.findall(text or "")
        self.tokens = [_norm(t) for t in raw]
        self.capitals = frozenset(_norm(t) for t in raw if t.isupper())
        self.after_ward = frozenset(
            self.tokens[i + 1] for i, t in enumerate(self.tokens[:-1]) if t == "ward")
        self.starts: dict = {}
        for i, t in enumerate(self.tokens):
            self.starts.setdefault(t, []).append(i)
        self.verdicts: dict = {}


#: A turn reads at most four texts (the question and three earlier user turns).
@functools.lru_cache(maxsize=4)
def _index(text: str) -> _TextIndex:
    return _TextIndex(text)


def ward_named_in(label, text: str) -> bool:
    """Does `text`, as typed (case kept), name the ward `label`?

    Pass the question as the clinician typed it, not lower-cased: the capitals
    half of the word-like rule reads the case (lower-cased text still finds
    "ward or", and every other label). An empty label names nothing.
    """
    want = _label_tokens(str(label) if label is not None else "")
    if not want:
        return False
    idx = _index(text or "")
    hit = idx.verdicts.get(want)
    if hit is None:
        if len(want) == 1 and want[0] in _WORD_LIKE:
            hit = want[0] in idx.capitals or want[0] in idx.after_ward
        else:
            n = len(want)
            hit = any(tuple(idx.tokens[i:i + n]) == want
                      for i in idx.starts.get(want[0], ()))
        idx.verdicts[want] = hit
    return hit
