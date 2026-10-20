"""Orb: the patient index matcher (opd-4 and opd-5, W4-A, 4 October 2026).

Deterministic, explainable duplicate and returning-patient detection for the
registration desk. No AI model anywhere in this lane: every point a candidate
scores has a named reason a person can check against the patient in front of
them.

THE KEYS LIVE IN A SIDE TABLE THAT HEALS ITSELF
patient_match_keys (migration 50) holds one row per patient: the normalised
name, phone, date of birth and sex, AND the raw values they were computed from.
Patients are written by many doors (the registration desk, admission, Scribe
quick-admit, the roster and history importers, seed scripts) and only the first
of them knows about this table. So nothing relies on the writers: sync_keys(db)
runs before every match and every search, finds every patient whose key row is
missing, was computed by an older normaliser, or whose source values changed
since (whoever changed them), and recomputes exactly those rows. A patient
admitted through the Admit door a minute ago, or whose phone an importer just
rewrote, is therefore found by the next match.

THE SCORE (design opd.json, opd-5)
  * hard matches, refused at registration whatever the mode: the same UHID
    (exact or once spaces, hyphens, slashes and dots are ignored, any case) or
    the same ACTIVE ABHA number or address;
  * phone +40 (the same 10-digit number once country codes, a trunk zero and
    separators are dropped; obvious placeholders such as 9999999999 never match);
  * name +40 when the normalised names are equal, or +25 when they share a
    distinctive word;
  * date of birth +30 when equal, or +10 when the two ages are within two years;
  * sex -20 when both are recorded and differ.
  Bands: probable 70 or more, possible 50 to 69. Below 50 is not shown.

A DATE OF BIRTH THE CALLER MAY NOT READ NEVER CHANGES WHAT THE CALLER SEES
The stored date of birth is the discharge-link second factor (ruling OPD-S1),
and a department-scoped reader may not see it for a patient outside their
department. A matcher that scored a typed date of birth against it would be an
oracle: the same request with a right and a wrong guess would come back with
a different reason, score, band or candidate list. So find_candidates_on takes
`dob_visible(patient_id)`, and for every candidate it says False for (when the
probe carries a date of birth):
  * the date of birth is never compared. When the record holds one and the
    two ages are the same whole number of years, the pair scores the full +30
    as reason 'dob_hidden' (it MAY be the same date; the strict side, so a true
    duplicate is never scored lower than before); otherwise the age rule above;
  * a candidate the date of birth alone fetched (no shared phone, no equal
    name, no hard match) is left out, because whether it was fetched IS the
    comparison. The administrator's sweep, which is not scoped, still pairs
    those records.
Both inputs to the rule, the age and whether the age comes from a date of
birth, are on the index card anyway, so every guess with the same age gets
the same answer. None (the default) compares every date of birth, as before.

A CUT SCAN SAYS WHAT IT MAY HAVE MISSED
At most MAX_CANDIDATE_ROWS rows sharing the phone, the name or the date of
birth are scored. They are fetched in descending order of the most each could
score against the probe (_bound_sql: the phone, the name, a distinctive shared
word and the date of birth worked out in SQL, plus the most the age could
add), so a true duplicate sharing two details is never cut in favour of a
helpline number or a 1 January date of birth shared by hundreds. When rows
were left out, the result carries the most any of them could score
(unseen_bound) and its band (unseen_band): None means a full scan would have
listed exactly the same candidates; 'possible' or 'probable' means it might
not have, and the registration desk then fails closed (registration_service).
For a caller who may not read some dates of birth, no date orders the rows
and every row only the date fetched carries one constant bound, so a right
and a wrong guess at a hidden date report the same bound. Whether a cut
happened at all still depends on how many records share the typed date: a
count over the whole index, never one patient's date, and already visible
as `truncated` before the rows were ranked.

NAME NORMALISATION
  * leading honorifics are dropped (Mr, Mrs, Shri, Smt, Kumari, Dr, ...);
  * a relation clause and what follows it is dropped (Ramesh S/O Suresh is
    Ramesh); "Baby of" and "B/O" become one marker word, so the baby of
    Lakshmi matches the baby of Laxmi but never Lakshmi herself;
  * the Mohammed family (Mohd, Md, Muhammad, Mohamed, ...) is one word;
  * transliteration folds: ksh/ks to x (Lakshmi, Laxmi), sh to s, th to t,
    dh to d, bh to b, kh to k, gh to g, jh to j, ph to f, w to v, q to k,
    oo to u (Pooja, Puja), ee to i, au and ou to o, a doubled letter to one,
    a final y to i and a final h after a vowel dropped;
  * single-letter initials are left out of the key, and the words are sorted,
    so "Rao Lakshmi" and "Lakshmi Rao" are the same name.

Every function that touches the database takes the caller's connection and
never commits: the registration service holds the transaction.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from typing import Any

#: Bump whenever a rule above changes: sync_keys then recomputes every row.
NORMALIZER_VERSION = 1

# Score points and bands.
POINTS_PHONE = 40
POINTS_NAME_EXACT = 40
POINTS_NAME_OVERLAP = 25
POINTS_DOB_EXACT = 30
POINTS_AGE_CLOSE = 10
POINTS_SEX_MISMATCH = -20
AGE_CLOSE_YEARS = 2
PROBABLE_THRESHOLD = 70
POSSIBLE_THRESHOLD = 50
HARD_SCORE = 100

#: The most candidate rows one match will score. A shared phone (a hospital
#: helpline typed for every walk-in) could otherwise make one match score the
#: whole index; past this the result says it was truncated. The rows are
#: fetched best first by the most each could score (_bound_sql), so a cut
#: never drops a stronger row than it keeps, and the result says how high a
#: row it left out could have scored (unseen_bound, unseen_band).
MAX_CANDIDATE_ROWS = 500

#: Plain words for each reason code, for the screen.
REASON_LABELS = {
    "uhid": "Same UHID",
    "abha": "Same ABHA",
    "phone": "Same phone number",
    "name_exact": "Same name",
    "name_overlap": "Part of the name matches",
    "dob_exact": "Same date of birth",
    "dob_hidden": "Same age, date of birth not shown outside your department",
    "age_close": "Age within 2 years",
    "sex_mismatch": "Different sex recorded",
}

# ── Name rules ───────────────────────────────────────────────────────────────

# Dropped only at the START of a name, so "Priya Kumari" keeps her surname
# while "Kumari Priya" loses the honorific.
_HONORIFICS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "miss",
        "mstr",
        "master",
        "dr",
        "prof",
        "shri",
        "sri",
        "shree",
        "sree",
        "sh",
        "smt",
        "shrimati",
        "srimati",
        "sushri",
        "kumari",
        "kum",
        "km",
        "late",
        "janab",
        "mohtarma",
        "sir",
        "madam",
    }
)

# Relation clauses: everything from here on names someone else. A slash form
# ("s/o", "D / O", "w/o") becomes one sentinel word before the name is split,
# so a real two-letter name such as "Ho" or "Do" is never read as one.
_RELATION_SENTINEL = "zzrelationzz"
_BABY_SENTINEL = "zzbabyofzz"
_RELATION_PHRASES = (
    ("son", "of"),
    ("daughter", "of"),
    ("wife", "of"),
    ("husband", "of"),
    ("care", "of"),
)

#: The marker word for a newborn identified through a parent.
BABY_MARKER = "babyof"
_BABY_PHRASES = (("baby", "of"), ("boy", "of"), ("girl", "of"), ("infant", "of"))

_MOHAMMED = frozenset(
    {
        "mohd",
        "md",
        "mohammad",
        "muhammad",
        "mohamed",
        "mohammed",
        "muhammed",
        "mohamad",
        "mohmmad",
        "mhd",
        "mohmed",
        "muhamed",
        "mohummad",
        "mahomed",
    }
)

# A slash relation such as "s/o", "d / o", "b/o" becomes one sentinel word
# before the name is split on non-letters.
_SLASH_RELATION = re.compile(r"\b([bsdwch])\s*/\s*o\b", re.IGNORECASE)

_DIGRAPHS = (
    ("ksh", "x"),
    ("ks", "x"),
    ("chh", "ch"),
    ("sh", "s"),
    ("ph", "f"),
    ("bh", "b"),
    ("dh", "d"),
    ("th", "t"),
    ("kh", "k"),
    ("gh", "g"),
    ("jh", "j"),
    ("w", "v"),
    ("q", "k"),
)
_VOWELS = (("oo", "u"), ("ee", "i"), ("au", "o"), ("ou", "o"))
_DOUBLE = re.compile(r"(.)\1+")


def _ascii_fold(text: str) -> str:
    """Accents off Latin letters (é to e). A combining mark on any other
    script is part of the letter (a Devanagari vowel sign is not an accent),
    so it is kept."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(ch) and out and out[-1].isascii():
            continue
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def _in_word(ch: str) -> bool:
    return ch.isalpha() or unicodedata.category(ch).startswith("M")


def fold_token(token: str) -> str:
    """One lower-case word through the transliteration folds."""
    t = token.lower()
    if t in _MOHAMMED:
        t = "mohammed"
    if not t.isascii():
        return t
    for a, b in _DIGRAPHS:
        t = t.replace(a, b)
    for a, b in _VOWELS:
        t = t.replace(a, b)
    t = _DOUBLE.sub(r"\1", t)
    if len(t) > 2 and t.endswith("ey"):
        t = t[:-2] + "i"
    elif len(t) > 2 and t.endswith("y"):
        t = t[:-1] + "i"
    if len(t) > 2 and t.endswith("h") and t[-2] in "aeiou":
        t = t[:-1]
    return t


def _raw_words(name: str) -> list[str]:
    text = _ascii_fold(str(name or ""))
    text = _SLASH_RELATION.sub(
        lambda m: f" {_BABY_SENTINEL if m.group(1).lower() == 'b' else _RELATION_SENTINEL} ", text
    )
    words, current = [], []
    for ch in text:
        if _in_word(ch):
            current.append(ch.lower())
        else:
            if current:
                words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def name_tokens(name: str | None) -> list[str]:
    """The folded words of a name, in order, honorifics and relation clauses
    removed, with BABY_MARKER first for a "baby of" name. Initials included."""
    words = _raw_words(name or "")
    baby = False
    # Leading honorifics and "baby of" markers, in any order.
    changed = True
    while words and changed:
        changed = False
        if words[0] in _HONORIFICS and len(words) > 1:
            words = words[1:]
            changed = True
            continue
        if words[0] == _BABY_SENTINEL and len(words) > 1:
            words, baby, changed = words[1:], True, True
            continue
        for phrase in _BABY_PHRASES:
            if tuple(words[:2]) == phrase and len(words) > 2:
                words, baby, changed = words[2:], True, True
                break
    # A relation clause and everything after it names someone else. A "b/o"
    # in the middle of a name (Ram B/O ...) is read the same way.
    cut = len(words)
    for i, w in enumerate(words):
        if i == 0:
            continue
        if (
            w in (_RELATION_SENTINEL, _BABY_SENTINEL)
            or tuple(words[i : i + 2]) in _RELATION_PHRASES
        ):
            cut = i
            break
    words = [w for w in words[:cut] if w not in (_RELATION_SENTINEL, _BABY_SENTINEL)]
    folded = [fold_token(w) for w in words if w]
    folded = [w for w in folded if w]
    return ([BABY_MARKER] if baby else []) + folded


def name_key(name: str | None) -> str | None:
    """The comparable form of a name: folded words, initials dropped (unless
    the name is nothing but initials), de-duplicated and sorted. None when the
    name has no letters."""
    tokens = name_tokens(name)
    if not tokens:
        return None
    full = [t for t in tokens if len(t) > 1]
    chosen = full if [t for t in full if t != BABY_MARKER] else tokens
    return " ".join(sorted(set(chosen)))


# Words so common in Indian names that sharing one says little about identity.
# Sharing one still counts toward an EXACT name; it just never counts as a
# partial overlap on its own.
_COMMON_WORDS_RAW = (
    "devi",
    "kumar",
    "kumari",
    "singh",
    "bai",
    "ben",
    "bhai",
    "begum",
    "bibi",
    "rani",
    "lal",
    "prasad",
    "das",
    "khan",
    "mohammed",
)
COMMON_NAME_WORDS = frozenset(fold_token(w) for w in _COMMON_WORDS_RAW)


def _distinctive(tokens: Iterable[str]) -> set[str]:
    return {t for t in tokens if len(t) >= 3 and t != BABY_MARKER and t not in COMMON_NAME_WORDS}


def name_points(key_a: str | None, key_b: str | None) -> tuple[int, str | None]:
    """(points, reason) for two name keys. A "baby of" name never scores
    against a name without the marker: they are two people by construction."""
    if not key_a or not key_b:
        return 0, None
    a, b = key_a.split(" "), key_b.split(" ")
    if (BABY_MARKER in a) != (BABY_MARKER in b):
        return 0, None
    if key_a == key_b:
        return POINTS_NAME_EXACT, "name_exact"
    if _distinctive(a) & _distinctive(b):
        return POINTS_NAME_OVERLAP, "name_overlap"
    return 0, None


def stated_name_matches(stated: str | None, stored: str | None) -> bool:
    """Does a name the patient STATES fit the stored one? Equal keys, or every
    word the patient said is a word of the stored name (they may give fewer
    names than the record holds, never a different one)."""
    said, held = name_key(stated), name_key(stored)
    if not said or not held:
        return False
    if said == held:
        return True
    said_words, held_words = set(said.split(" ")), set(held.split(" "))
    if (BABY_MARKER in said_words) != (BABY_MARKER in held_words):
        return False
    meaningful = {w for w in said_words if w != BABY_MARKER}
    return bool(meaningful) and said_words <= held_words


# ── Phone, date of birth, sex, UHID ─────────────────────────────────────────

_PLACEHOLDER_PHONES = frozenset({"1234567890", "0123456789", "9876543210", "0987654321"})


def phone_digits(raw: str | None) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def phone_key(raw: str | None) -> str | None:
    """The 10-digit national number, or None when the value is too short to
    identify anyone or is an obvious placeholder."""
    digits = phone_digits(raw)
    if len(digits) == 14 and digits.startswith("0091"):
        digits = digits[4:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    elif len(digits) > 10:
        digits = digits[-10:]
    if len(digits) != 10:
        return None
    if len(set(digits)) == 1 or digits in _PLACEHOLDER_PHONES:
        return None
    return digits


def phone_last4(raw: str | None) -> str | None:
    digits = phone_digits(raw)
    return digits[-4:] if len(digits) >= 4 else None


def dob_key(raw: Any) -> str | None:
    """YYYY-MM-DD for a usable date of birth, else None (the discharge
    portal's own definition, so the two can never disagree)."""
    from services.discharge_access_service import usable_dob

    return usable_dob(raw)


_SEX_WORDS = {
    "m": "M",
    "male": "M",
    "man": "M",
    "boy": "M",
    "f": "F",
    "female": "F",
    "woman": "F",
    "girl": "F",
    "o": "O",
    "other": "O",
    "t": "O",
    "transgender": "O",
    "tg": "O",
}


def sex_key(raw: str | None) -> str | None:
    return _SEX_WORDS.get(str(raw or "").strip().lower()) if raw is not None else None


_UHID_SEPARATORS = re.compile(r"[\s\-/.]")


def uhid_key(raw: str | None) -> str | None:
    """A UHID as compared for uniqueness: upper case, no spaces, hyphens,
    slashes or dots. None when nothing is left."""
    text = _UHID_SEPARATORS.sub("", str(raw or "")).upper()
    return text or None


#: The same normalisation in SQL, for a scan that must not leave SQLite.
UHID_KEY_SQL = (
    "UPPER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({col}, ' ', ''), "
    "'-', ''), '/', ''), '.', ''), char(9), ''), char(10), ''))"
)


# ── Ages ─────────────────────────────────────────────────────────────────────


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def full_years(born: date, on: date) -> int:
    years = on.year - born.year
    if (on.month, on.day) < (born.month, born.day):
        years -= 1
    return years


def current_age(
    *,
    date_of_birth: Any,
    stated_age: Any,
    age_recorded_on: Any,
    created_at: Any,
    today: date | None = None,
) -> tuple[int | None, bool]:
    """(age in whole years today, stated?) for a record.

    From the date of birth when it is usable. Otherwise from the stated age,
    plus the whole years since it was recorded (age_recorded_on, else the day
    the record was created). (None, False) when neither is known.
    """
    from services.hospital_time import hospital_today

    today = today or hospital_today()
    dob = _as_date(dob_key(date_of_birth))
    if dob is not None:
        return max(0, full_years(dob, today)), False
    try:
        age = int(stated_age) if stated_age is not None and str(stated_age).strip() != "" else None
    except (TypeError, ValueError):
        age = None
    if age is None or age < 0:
        return None, False
    anchor = _as_date(age_recorded_on) or _as_date(created_at)
    if anchor is not None and anchor <= today:
        age += full_years(anchor, today)
    return age, True


# ── Keys and the self-healing sync ───────────────────────────────────────────


def keys_for(row: Any) -> dict:
    """The patient_match_keys values for one patients row (any mapping with
    name, phone, date_of_birth and sex)."""

    def get(k):
        try:
            return row[k]
        except (KeyError, IndexError):
            return None

    name, phone, dob, sex = get("name"), get("phone"), get("date_of_birth"), get("sex")
    return {
        "name_key": name_key(name),
        "phone_key": phone_key(phone),
        "dob": dob_key(dob),
        "sex": sex_key(sex),
        "src_name": name,
        "src_phone": phone,
        "src_dob": dob,
        "src_sex": sex,
    }


_UPSERT_KEYS_SQL = (
    "INSERT INTO patient_match_keys (patient_id, name_key, phone_key, dob, sex, "
    "src_name, src_phone, src_dob, src_sex, normalizer_version, computed_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(patient_id) DO UPDATE SET name_key = excluded.name_key, "
    "phone_key = excluded.phone_key, dob = excluded.dob, sex = excluded.sex, "
    "src_name = excluded.src_name, src_phone = excluded.src_phone, "
    "src_dob = excluded.src_dob, src_sex = excluded.src_sex, "
    "normalizer_version = excluded.normalizer_version, computed_at = excluded.computed_at"
)


def _now() -> str:
    from services.clinical_time import utc_iso_now

    return utc_iso_now()


def upsert_keys_on(db, patient_id: str, row: Any) -> dict:
    """Write one patient's key row from its source values. Never commits."""
    keys = keys_for(row)
    db.execute(
        _UPSERT_KEYS_SQL,
        (
            patient_id,
            keys["name_key"],
            keys["phone_key"],
            keys["dob"],
            keys["sex"],
            keys["src_name"],
            keys["src_phone"],
            keys["src_dob"],
            keys["src_sex"],
            NORMALIZER_VERSION,
            _now(),
        ),
    )
    return keys


#: The stale-row finder: no key row, an older normaliser, or any source value
#: changed since the keys were computed. `IS NOT` compares NULLs as values, so
#: a phone cleared by an importer is seen as a change too.
_STALE_SQL = (
    "SELECT p.id, p.name, p.phone, p.date_of_birth, p.sex FROM patients p "
    "LEFT JOIN patient_match_keys k ON k.patient_id = p.id "
    "WHERE k.patient_id IS NULL OR k.normalizer_version != ? "
    "OR k.src_name IS NOT p.name OR k.src_phone IS NOT p.phone "
    "OR k.src_dob IS NOT p.date_of_birth OR k.src_sex IS NOT p.sex"
)


def sync_keys(db, *, remove_orphans: bool = False) -> dict:
    """Recompute every key row that is missing or stale. Never commits.

    Returns {"recomputed": n, "removed": n}. With remove_orphans, key rows
    whose patient no longer exists are deleted too (the sweep does this;
    matches never surface them because candidates are joined to patients).
    """
    rows = db.execute(_STALE_SQL, (NORMALIZER_VERSION,)).fetchall()
    now = _now()
    batch = []
    for r in rows:
        k = keys_for(r)
        batch.append(
            (
                r["id"],
                k["name_key"],
                k["phone_key"],
                k["dob"],
                k["sex"],
                k["src_name"],
                k["src_phone"],
                k["src_dob"],
                k["src_sex"],
                NORMALIZER_VERSION,
                now,
            )
        )
    if batch:
        db.executemany(_UPSERT_KEYS_SQL, batch)
    removed = 0
    if remove_orphans:
        cur = db.execute(
            "DELETE FROM patient_match_keys WHERE NOT EXISTS "
            "(SELECT 1 FROM patients p WHERE p.id = patient_match_keys.patient_id)"
        )
        removed = max(0, cur.rowcount or 0)
    return {"recomputed": len(batch), "removed": removed}


# ── Scoring ──────────────────────────────────────────────────────────────────

#: Reasons that make a pair a hard match whatever it scores.
HARD_REASONS = frozenset({"uhid", "abha"})


def band_for(score: int, reasons: Iterable[str] = ()) -> str | None:
    """'hard' when a hard reason is present (the same UHID or active ABHA),
    else 'probable' for 70 or more, 'possible' for 50 to 69, else None. A
    scored pair can pass 100 (phone, name and date of birth make 110) and is
    still only probable: hard is a reason, not a number."""
    if HARD_REASONS.intersection(reasons or ()):
        return "hard"
    if score >= PROBABLE_THRESHOLD:
        return "probable"
    if score >= POSSIBLE_THRESHOLD:
        return "possible"
    return None


def probe_from(
    *,
    name=None,
    phone=None,
    date_of_birth=None,
    age_years=None,
    sex=None,
    uhid=None,
    abha_number=None,
    abha_address=None,
    today: date | None = None,
) -> dict:
    """The comparable form of what the desk typed for a new patient."""
    age, _stated = current_age(
        date_of_birth=date_of_birth,
        stated_age=age_years,
        age_recorded_on=None,
        created_at=None,
        today=today,
    )
    return {
        "name_key": name_key(name),
        "phone_key": phone_key(phone),
        "dob": dob_key(date_of_birth),
        "age": age,
        "sex": sex_key(sex),
        "uhid": str(uhid).strip() if uhid and str(uhid).strip() else None,
        "uhid_key": uhid_key(uhid),
        "abha_number": abha_number,
        "abha_address": abha_address,
    }


def score_pair(probe: dict, other: dict, *, dob_hidden: bool = False) -> tuple[int, list[str]]:
    """Points and reasons for a probe against one candidate's keys.

    `other` carries name_key, phone_key, dob, sex and age (its current age, or
    None). Hard matches are decided by find_candidates_on, not here.

    `dob_hidden`: the caller may not read this candidate's date of birth, so
    the probe's date of birth is never compared with it (module docstring). A
    record holding one, at the same age as the probe, scores +30 as
    'dob_hidden'; the answer depends only on the two ages and on whether the
    record holds a date of birth, never on the date itself.
    """
    score, reasons = 0, []
    if probe.get("phone_key") and probe["phone_key"] == other.get("phone_key"):
        score += POINTS_PHONE
        reasons.append("phone")
    pts, why = name_points(probe.get("name_key"), other.get("name_key"))
    if pts:
        score += pts
        assert why is not None
        reasons.append(why)
    close = (
        probe.get("age") is not None
        and other.get("age") is not None
        and abs(int(probe["age"]) - int(other["age"])) <= AGE_CLOSE_YEARS
    )
    if dob_hidden and probe.get("dob"):
        if close and other.get("dob") and int(probe["age"]) == int(other["age"]):
            score += POINTS_DOB_EXACT
            reasons.append("dob_hidden")
        elif close:
            score += POINTS_AGE_CLOSE
            reasons.append("age_close")
    elif probe.get("dob") and probe["dob"] == other.get("dob"):
        score += POINTS_DOB_EXACT
        reasons.append("dob_exact")
    elif close:
        score += POINTS_AGE_CLOSE
        reasons.append("age_close")
    if probe.get("sex") and other.get("sex") and probe["sex"] != other["sex"]:
        score += POINTS_SEX_MISMATCH
        reasons.append("sex_mismatch")
    return score, reasons


def _hard_uhid_ids(db, probe: dict) -> list[str]:
    if not probe.get("uhid_key"):
        return []
    ids = [
        r[0]
        for r in db.execute("SELECT id FROM patients WHERE mrn = ?", (probe["uhid"],)).fetchall()
    ]
    ids += [
        r[0]
        for r in db.execute(
            f"SELECT id FROM patients WHERE {UHID_KEY_SQL.format(col='mrn')} = ?",
            (probe["uhid_key"],),
        ).fetchall()
    ]
    return list(dict.fromkeys(ids))


def _hard_abha_ids(db, probe: dict) -> list[str]:
    ids = []
    for column in ("abha_number", "abha_address"):
        value = probe.get(column)
        if not value:
            continue
        ids += [
            r[0]
            for r in db.execute(
                f"SELECT patient_id FROM abha_identities WHERE {column} = ? AND status = 'active'",
                (value,),
            ).fetchall()
        ]
    return list(dict.fromkeys(ids))


def _bound_sql(probe: dict, *, dob_may_be_hidden: bool) -> tuple[str, list]:
    """SQL for the most a patient_match_keys row (alias k) could score against
    the probe, never less than score_pair gives it, and its parameters.

      * the phone and the exact name exactly as score_pair counts them;
      * +25 when the row holds any distinctive word of the probe's name (the
        same word list score_pair uses, without the baby-of check, so it can
        only count more);
      * the date of birth: +30 when equal, else +10 for a close age. When a
        scoped caller may not read some rows' dates (dob_may_be_hidden) every
        row counts +30, the most 'dob_hidden' can add; with no date typed,
        +10 when an age was;
      * the sex is left out: it can only take points away.

    For a scoped caller, a row sharing neither the phone nor the exact name
    (one only the date of birth fetched) gets ONE constant bound, the most a
    readable such row can score (+30, and +25 when the probe has a
    distinctive word). Such a row is dropped when its date is hidden, and
    whether the date fetched it is the comparison, so a hidden date must
    never change the order of the rows or the bound a cut reports.
    """
    parts: list[str] = []
    params: list = []
    words = sorted(_distinctive(probe["name_key"].split(" "))) if probe.get("name_key") else []
    if probe.get("phone_key"):
        parts.append(f"CASE WHEN k.phone_key = ? THEN {POINTS_PHONE} ELSE 0 END")
        params.append(probe["phone_key"])
    if probe.get("name_key"):
        expr = f"CASE WHEN k.name_key = ? THEN {POINTS_NAME_EXACT}"
        params.append(probe["name_key"])
        if words:
            shared = " OR ".join("instr(' ' || k.name_key || ' ', ?) > 0" for _ in words)
            expr += f" WHEN k.name_key IS NOT NULL AND ({shared}) THEN {POINTS_NAME_OVERLAP}"
            params.extend(f" {w} " for w in words)
        parts.append(expr + " ELSE 0 END")
    if probe.get("dob") and not dob_may_be_hidden:
        parts.append(f"CASE WHEN k.dob = ? THEN {POINTS_DOB_EXACT} ELSE {POINTS_AGE_CLOSE} END")
        params.append(probe["dob"])
    elif probe.get("dob"):
        parts.append(str(POINTS_DOB_EXACT))
    elif probe.get("age") is not None:
        parts.append(str(POINTS_AGE_CLOSE))
    expr = " + ".join(parts) or "0"
    if not (dob_may_be_hidden and probe.get("dob")):
        return expr, params
    date_only = POINTS_DOB_EXACT + (POINTS_NAME_OVERLAP if words else 0)
    keyed, keyed_params = [], []
    if probe.get("phone_key"):
        keyed.append("k.phone_key = ?")
        keyed_params.append(probe["phone_key"])
    if probe.get("name_key"):
        keyed.append("k.name_key = ?")
        keyed_params.append(probe["name_key"])
    if not keyed:
        return str(date_only), []
    return (
        f"CASE WHEN ({' OR '.join(keyed)}) THEN ({expr}) ELSE {date_only} END",
        [*keyed_params, *params],
    )


def find_candidates_on(
    db,
    probe: dict,
    *,
    exclude_patient_id: str | None = None,
    sync: bool = True,
    today: date | None = None,
    dob_visible: Callable[[str], bool] | None = None,
) -> dict:
    """Every existing patient the probe may be, best first.

    Returns {"candidates": [{patient_id, score, band, reasons}], "truncated":
    bool, "unseen_bound": int or None, "unseen_band": None, 'possible' or
    'probable', "keys_recomputed": n}. Hard matches come first with score 100
    and band 'hard' (reasons 'uhid' and or 'abha', plus whatever else matched).
    Only candidates scoring POSSIBLE_THRESHOLD or more are returned.
    `probe` is probe_from(...); abha values must already be normalised.

    `truncated`: more than MAX_CANDIDATE_ROWS rows shared the phone, name or
    date of birth, so some were not scored. They are fetched in descending
    order of _bound_sql, so every row left out could score at most
    `unseen_bound` (the bound of the first row left out); `unseen_band` is
    band_for(unseen_bound). None for both when nothing was left out. A caller
    deciding whether a patient may be registered must treat an unseen_band
    as candidates it was not shown (module docstring).

    `dob_visible(patient_id)` says whether the caller may read that patient's
    date of birth; None means every one. While the probe carries a date of
    birth, a candidate it answers False for never has that date compared, and
    is left out when only the date fetched it (module docstring). It is asked
    at most once per candidate, and only when the probe has a date.
    """
    recomputed = sync_keys(db)["recomputed"] if sync else 0
    hard: dict[str, list[str]] = {}
    for pid in _hard_uhid_ids(db, probe):
        hard.setdefault(pid, []).append("uhid")
    for pid in _hard_abha_ids(db, probe):
        hard.setdefault(pid, []).append("abha")

    clauses, params = [], []
    if probe.get("phone_key"):
        clauses.append("k.phone_key = ?")
        params.append(probe["phone_key"])
    if probe.get("name_key"):
        clauses.append("k.name_key = ?")
        params.append(probe["name_key"])
    if probe.get("dob"):
        clauses.append("k.dob = ?")
        params.append(probe["dob"])
    rows: list = []
    truncated = False
    unseen_bound: int | None = None
    if clauses:
        # Ranked BEFORE the cut: the strongest rows first, so a row sharing
        # two details is never dropped for one sharing only a crowded one.
        maybe_hidden = dob_visible is not None and bool(probe.get("dob"))
        bound, bound_params = _bound_sql(probe, dob_may_be_hidden=maybe_hidden)
        tiebreak, tie_params = "", []
        if probe.get("dob") and not maybe_hidden:
            # Never for a scoped caller: a hidden date must not order the rows.
            tiebreak = "CASE WHEN k.dob = ? THEN 1 ELSE 0 END DESC, "
            tie_params = [probe["dob"]]
        rows = db.execute(
            "SELECT k.patient_id, k.name_key, k.phone_key, k.dob, k.sex, "
            "p.age AS stated_age, p.date_of_birth, p.created_at, d.age_recorded_on, "
            f"({bound}) AS match_bound "
            "FROM patient_match_keys k JOIN patients p ON p.id = k.patient_id "
            "LEFT JOIN patient_demographics d ON d.patient_id = k.patient_id "
            f"WHERE ({' OR '.join(clauses)}) "
            f"ORDER BY match_bound DESC, {tiebreak}k.patient_id LIMIT ?",
            (*bound_params, *params, *tie_params, MAX_CANDIDATE_ROWS + 1),
        ).fetchall()
        if len(rows) > MAX_CANDIDATE_ROWS:
            truncated = True
            unseen_bound = int(rows[MAX_CANDIDATE_ROWS]["match_bound"])
            rows = rows[:MAX_CANDIDATE_ROWS]
    hidden_dob: dict[str, bool] = {}

    def dob_is_hidden(pid: str) -> bool:
        if dob_visible is None or not probe.get("dob"):
            return False
        if pid not in hidden_dob:
            hidden_dob[pid] = not dob_visible(pid)
        return hidden_dob[pid]

    scored: dict[str, dict] = {}
    for r in rows:
        pid = r["patient_id"]
        if pid == exclude_patient_id:
            continue
        age, stated = current_age(
            date_of_birth=r["date_of_birth"],
            stated_age=r["stated_age"],
            age_recorded_on=r["age_recorded_on"],
            created_at=r["created_at"],
            today=today,
        )
        if dob_is_hidden(pid):
            # Fetched by the phone or the name, never by the date of birth
            # alone: whether the date fetched it would be the comparison.
            if not (
                (probe.get("phone_key") and r["phone_key"] == probe["phone_key"])
                or (probe.get("name_key") and r["name_key"] == probe["name_key"])
            ):
                continue
            # Only what the index card shows: the age, and whether it comes
            # from a date of birth.
            other = {
                "name_key": r["name_key"],
                "phone_key": r["phone_key"],
                "dob": (age is not None and not stated) or None,
                "sex": r["sex"],
                "age": age,
            }
            score, reasons = score_pair(probe, other, dob_hidden=True)
        else:
            other = {
                "name_key": r["name_key"],
                "phone_key": r["phone_key"],
                "dob": r["dob"],
                "sex": r["sex"],
                "age": age,
            }
            score, reasons = score_pair(probe, other)
        scored[pid] = {"patient_id": pid, "score": score, "reasons": reasons}
    out: list[dict] = []
    for pid, why in hard.items():
        if pid == exclude_patient_id:
            continue
        extra = scored.get(pid, {}).get("reasons", [])
        out.append(
            {
                "patient_id": pid,
                "score": HARD_SCORE,
                "band": "hard",
                "reasons": why + [r for r in extra if r not in why],
            }
        )
    for pid, entry in scored.items():
        if pid in hard:
            continue
        band = band_for(entry["score"])
        if band is None:
            continue
        out.append({**entry, "band": band})
    out.sort(key=lambda c: (-c["score"], c["patient_id"]))
    return {
        "candidates": out,
        "truncated": truncated,
        "unseen_bound": unseen_bound,
        "unseen_band": band_for(unseen_bound) if unseen_bound is not None else None,
        "keys_recomputed": recomputed,
    }


def reason_labels(reasons: Iterable[str]) -> list[str]:
    return [REASON_LABELS.get(r, r) for r in reasons]


def utc_now() -> datetime:
    return datetime.now(UTC)
