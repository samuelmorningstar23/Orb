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
  * A ward code matches only as WHOLE tokens, in order. Tokens are runs of
    letters or of digits, so letter/digit boundaries split ("GM-1", "gm 1" and
    "gm1" are all ["gm", "1"]), and leading zeros are ignored ("ICU-02" is
    "icu 2"). "GM-1" is not named by "gm 101", "CCU" not by "occur", "OR" not
    by "for". Short abbreviations (ICU, CCU, HDU, NICU, ENT) are case-blind.
  * A code that is ONE ORDINARY WORD needs a sign that it is meant as a ward,
    because ward codes are free text in the bed master and a hospital may code
    its wards "Surgery", "Recovery", "OBS" or "Male": "pain after cardiac
    surgery" listed every patient on ward Surgery. A code is an ordinary word
    when it is a short English word (_FUNCTION_WORDS), a clinical word on the
    list below (_WARD_WORDS), any all-letter code of five letters or more, or
    digits only ("6"). The signs are:
      - written in CAPITALS ("SURGERY", "back from OR");
      - written with a capital anywhere but the start of a sentence ("anyone
        in Recovery?"; "Recovery after MI takes weeks" is not);
      - right next to the word "ward": after it for every such code ("ward
        surgery", "ward or", "ward 6"), before it too for a word code
        ("Surgery ward", "6 ward"), but NOT for a short English word, because
        "in ward 6" and "on ward 6" are how wards are named, not an IN ward.
    Digits have no capitals, so a digit-only code counts only beside "ward".
  * A mention that is GENERIC names no ward, whatever the code (ruled by
    Samuel 2026-10-06). With bed locator attempt 8 every ICU mention gave the
    ICU census: "What are the ICU admission criteria for DKA?", "ICU
    delirium", "call the ICU team" and "Is he for ICU if he deteriorates?"
    all put every ICU patient in the prompt. A mention is generic when a
    LISTED cue sits beside it, in the same sentence:
      - a noun after it that the code describes (_GENERIC_AFTER): "ICU
        criteria", "ICU delirium", "ICU-acquired", "ICU-level", "the ICU
        team", also across one linking noun ("ICU admission criteria");
      - a NEED or a DESTINATION before it (_GENERIC_BEFORE): "needs ICU",
        "call ICU", "should she go to ICU?", "is he for ICU?". The patient
        being discussed is meant, so a pronoun resolves to them as usual
        instead of to the ward's census;
      - but not after a word saying someone already got there (_ARRIVED):
        "admitted to ICU", "transfers to CCU", "came back to ICU" still ask
        about who is there now.
    The lists only REMOVE matches: a phrasing they do not cover names the
    ward as before ("the mortality of septic shock in ICU" still does).

Beds are NOT read here: bed matching is ruling R026's work
(docs/SAGE_BED_LOCATOR_PLAN_2026-10-03.md). A ward code made of several
ordinary words ("General Medicine") still matches that phrase case-blind.
"""
from __future__ import annotations

import functools
import re

#: Runs of letters or of digits, read off the text as typed (case kept).
_TOKEN_RE = re.compile(r"[A-Za-z]+|[0-9]+")

#: Short English words (and spoken hesitations: "er", "um" in a voice
#: transcript). As a ward code they need capitals, a mid-sentence capital or
#: "ward" BEFORE them; never "ward" after, see the module docstring.
_FUNCTION_WORDS = frozenset({
    "a", "am", "an", "as", "at", "be", "by", "do", "er", "go", "he", "hi", "if",
    "in", "is", "it", "me", "my", "no", "of", "oh", "ok", "on", "or", "so", "to",
    "um", "up", "us", "we",
    "all", "and", "any", "are", "but", "can", "day", "did", "few", "for", "had",
    "has", "her", "him", "his", "how", "its", "new", "not", "now", "off", "old",
    "one", "our", "out", "own", "per", "see", "she", "the", "too", "two", "use",
    "was", "way", "who", "why", "yes", "yet", "you",
})

#: Clinical and everyday words of four letters or fewer that hospitals use as
#: ward codes and clinicians write in ordinary sentences ("repeat obs", "male,
#: 45", "PRN med"). Longer all-letter codes are words by length alone.
_WARD_WORDS = frozenset({
    "obs", "male", "med", "meds", "surg", "gen", "peds", "paed", "gyn", "gyne",
    "onc", "onco", "uro", "endo", "cath", "burn", "eye", "skin", "bone", "lung",
    "post", "step", "main", "side", "east", "west", "red", "blue", "pink",
    "gold", "grey", "gray", "rose", "lily",
})

#: An all-letter code this long or longer is an ordinary word.
_WORD_MIN_LETTERS = 5

#: Characters that end a sentence, for the mid-sentence capital sign.
_SENTENCE_END = ".!?\n"

#: A noun right after a ward code that the code only describes: the question
#: is about criteria, a condition, a team or a policy, not the ward's patients.
_GENERIC_AFTER = frozenset({
    "criteria", "criterion", "delirium", "psychosis", "acquired", "level",
    "levels", "team", "teams", "consult", "consults", "consultation",
    "consultant", "consultants", "registrar", "registrars", "resident",
    "residents", "fellow", "fellows", "intensivist", "intensivists", "nurse",
    "nurses", "doctor", "doctors", "staff", "referral", "referrals", "outreach",
    "protocol", "protocols", "guideline", "guidelines", "policy", "policies",
    "bundle", "bundles", "mortality", "survival", "outcome", "outcomes", "trial",
    "trials", "training", "rotation", "rotations", "course", "courses",
    "availability",
})

#: Nouns that may sit between the code and a generic noun: "ICU admission
#: criteria", "ICU bed availability". On their own they describe nothing.
_LINKING = frozenset({"admission", "transfer", "discharge", "escalation", "triage", "bed"})

#: A word right before a ward code that makes it a need or a destination for
#: the patient being discussed ("needs ICU", "call ICU", "go to ICU", "for
#: ICU", "an ICU bed"), not a question about who is on the ward.
_GENERIC_BEFORE = frozenset({
    "need", "needs", "needed", "needing", "require", "requires", "required",
    "requiring", "call", "page", "bleep", "consult", "for", "to", "into", "an",
})

#: Before "to" or "into", a word saying someone already got there: "admitted
#: to ICU", "transfers to CCU", "came back to ICU" ask about who is there now.
_ARRIVED = frozenset({
    "admitted", "transferred", "moved", "shifted", "sent", "went", "brought",
    "returned", "taken", "came", "gone", "back", "admissions", "transfers",
    "arrivals",
})


def _norm(token: str) -> str:
    """Lower-case; a run of digits loses its leading zeros ("02" is "2")."""
    if token.isdigit():
        return token.lstrip("0") or "0"
    return token.lower()


@functools.lru_cache(maxsize=512)
def _label_tokens(label: str) -> tuple:
    return tuple(_norm(t) for t in _TOKEN_RE.findall(label or ""))


def _needs_a_sign(token: str) -> bool:
    """Is this one-token code an ordinary word (or bare digits)?"""
    return (token.isdigit() or token in _FUNCTION_WORDS or token in _WARD_WORDS
            or len(token) >= _WORD_MIN_LETTERS)


class _TextIndex:
    """One question, tokenised once and shared by every ward test against it."""

    def __init__(self, text: str):
        self.text = text
        matches = list(_TOKEN_RE.finditer(text))
        self.raw = [m.group(0) for m in matches]
        self.begin = [m.start() for m in matches]
        self.tokens = [_norm(t) for t in self.raw]
        self.starts: dict = {}
        for i, t in enumerate(self.tokens):
            self.starts.setdefault(t, []).append(i)
        self.verdicts: dict = {}

    def opens_a_sentence(self, i: int) -> bool:
        before = self.text[:self.begin[i]].rstrip(" \t\"'([{")
        return not before or before[-1] in _SENTENCE_END

    def signed(self, i: int, word: str) -> bool:
        """Is the token at i written as a ward, by the signs in the docstring?"""
        raw = self.raw[i]
        if raw.isalpha() and raw.isupper():
            return True
        if raw[0].isupper() and not self.opens_a_sentence(i):
            return True
        if i > 0 and self.tokens[i - 1] == "ward":
            return True
        if (word not in _FUNCTION_WORDS and i + 1 < len(self.tokens)
                and self.tokens[i + 1] == "ward"):
            return True
        return False

    def _together(self, a: int, b: int) -> bool:
        """Are neighbouring tokens a < b in one sentence?"""
        gap = self.text[self.begin[a] + len(self.raw[a]):self.begin[b]]
        return not any(c in _SENTENCE_END for c in gap)

    def generic(self, i: int, n: int) -> bool:
        """Is the code at tokens i..i+n-1 used generically, by a listed cue
        beside it in the same sentence (see the module docstring)?"""
        toks = self.tokens
        j = i + n
        if j < len(toks) and self._together(j - 1, j):
            if toks[j] in _GENERIC_AFTER:
                return True
            if (toks[j] in _LINKING and j + 1 < len(toks) and self._together(j, j + 1)
                    and toks[j + 1] in _GENERIC_AFTER):
                return True
        k = i - 1
        if k >= 0 and self._together(k, i) and toks[k] in _GENERIC_BEFORE:
            arrived = (toks[k] in ("to", "into") and k > 0 and self._together(k - 1, k)
                       and toks[k - 1] in _ARRIVED)
            return not arrived
        return False


#: A turn reads at most four texts (the question and three earlier user turns).
@functools.lru_cache(maxsize=4)
def _index(text: str) -> _TextIndex:
    return _TextIndex(text)


def ward_named_in(label, text: str) -> bool:
    """Does `text`, as typed (case kept), name the ward `label`?

    Pass the question as the clinician typed it, not lower-cased: the signs
    for a one-word code read the case and the sentence position. A
    lower-cased text still finds every abbreviation and multi-part code, and
    a word code beside "ward". An empty label names nothing. A mention used
    generically ("ICU admission criteria", "should she go to ICU?") names no
    ward; each occurrence is judged on its own, and any one that counts is
    enough.
    """
    want = _label_tokens(str(label) if label is not None else "")
    if not want:
        return False
    idx = _index(text or "")
    hit = idx.verdicts.get(want)
    if hit is None:
        n = len(want)
        needs_sign = n == 1 and _needs_a_sign(want[0])
        hit = any(tuple(idx.tokens[i:i + n]) == want
                  and (not needs_sign or idx.signed(i, want[0]))
                  and not idx.generic(i, n)
                  for i in idx.starts.get(want[0], ()))
        idx.verdicts[want] = hit
    return hit
