"""Laboratory accession numbers (lis-6, ruling LIS-R13).

Pinned here:
  * the format: 12 digits, YYMMDD of the hospital-local day, a 5-digit daily
    sequence and a Damm check digit; an optional site prefix of capital
    letters is checked by equality and never covered by the check digit;
  * the Damm digit catches every single-digit error and every adjacent
    transposition, 09 and 90 included (where Luhn fails);
  * the day rolls over at local midnight (23:59 to 00:01 IST changes the day)
    and NOT at the UTC date change (05:29 to 05:31 IST does not);
  * allocation is safe under concurrency: 20 threads get 20 unique,
    consecutive numbers, and a rolled-back transaction gives its number back;
  * the sequence never wraps: at 99999 allocation refuses loudly.
"""
from __future__ import annotations

import logging
import random
import threading
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from database import db_session
from services import hospital_time, lab_accession as acc

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def ist(monkeypatch):
    monkeypatch.setattr(hospital_time, "hospital_timezone", lambda: IST)
    return IST


@pytest.fixture
def clean_days():
    """Counter rows for the far-future days these tests use, removed after."""
    days: list[str] = []
    yield days
    if days:
        marks = ", ".join("?" for _ in days)
        with db_session() as db:
            db.execute(f"DELETE FROM lab_accession_counters WHERE hospital_day IN ({marks})",
                       tuple(days))


def _counter(day: str):
    with db_session() as db:
        row = db.execute("SELECT last_seq FROM lab_accession_counters WHERE hospital_day = ?",
                         (day,)).fetchone()
    return None if row is None else row[0]


# ── 1. The check digit ───────────────────────────────────────────────────────

def test_the_damm_digit_matches_the_published_example():
    assert acc.damm_check_digit("572") == 4
    assert acc.damm_valid("5724")
    assert not acc.damm_valid("5723")


def _bodies(n=150):
    rnd = random.Random(20261004)
    yield "26100400001"
    yield "09090909090"
    yield "90909090909"
    for _ in range(n):
        yield "".join(rnd.choice("0123456789") for _ in range(11))


def test_every_single_digit_error_is_caught():
    for body in _bodies():
        good = body + str(acc.damm_check_digit(body))
        assert acc.damm_valid(good)
        for i, ch in enumerate(good):
            for d in "0123456789":
                if d == ch:
                    continue
                bad = good[:i] + d + good[i + 1:]
                assert not acc.damm_valid(bad), (good, bad)


def test_every_adjacent_transposition_is_caught_including_09_and_90():
    for body in _bodies():
        good = body + str(acc.damm_check_digit(body))
        for i in range(len(good) - 1):
            a, b = good[i], good[i + 1]
            if a == b:
                continue
            swapped = good[:i] + b + a + good[i + 2:]
            assert not acc.damm_valid(swapped), (good, swapped)
    # The case Luhn misses, stated on its own.
    body = "26100400090"
    good = body + str(acc.damm_check_digit(body))
    i = good.index("90")
    swapped = good[:i] + "09" + good[i + 2:]
    assert acc.damm_valid(good) and not acc.damm_valid(swapped)


def test_the_check_digit_refuses_anything_but_digits():
    assert not acc.damm_valid("")
    assert not acc.damm_valid("12a4")
    assert not acc.damm_valid("１２３")  # full-width digits are not ASCII digits
    with pytest.raises(ValueError):
        acc.damm_check_digit("12-3")


# ── 2. Format and parse ──────────────────────────────────────────────────────

def test_the_format_is_twelve_digits_day_sequence_check():
    text = acc.format_accession(date(2026, 10, 4), 42)
    assert len(text) == 12 and text.isdigit()
    assert text[:6] == "261004" and text[6:11] == "00042"
    assert acc.damm_valid(text)
    parts = acc.parse_accession(text)
    assert parts.hospital_day == date(2026, 10, 4) and parts.sequence == 42 and parts.prefix == ""


def test_the_sequence_bounds_are_one_to_99999():
    acc.format_accession(date(2026, 10, 4), 1)
    acc.format_accession(date(2026, 10, 4), 99999)
    for bad in (0, 100000, -1, True, "7"):
        with pytest.raises(ValueError):
            acc.format_accession(date(2026, 10, 4), bad)


def test_parse_refuses_what_orb_never_issued():
    good = acc.format_accession(date(2026, 10, 4), 7)
    assert acc.parse_accession(f"  {good} ").text == good
    cases = {
        "": "No accession number",
        good[:-1]: "has 12 digits",
        good + "1": "has 12 digits",
        good[:5] + "A" + good[6:]: "digits only",
        good[:-1] + str((int(good[-1]) + 1) % 10): "check digit",
    }
    for text, words in cases.items():
        with pytest.raises(acc.AccessionInvalid, match=words):
            acc.parse_accession(text)
    impossible = "261332" + "00001"
    with pytest.raises(acc.AccessionInvalid, match="not a real date"):
        acc.parse_accession(impossible + str(acc.damm_check_digit(impossible)))
    zero = "261004" + "00000"
    with pytest.raises(acc.AccessionInvalid, match="zero"):
        acc.parse_accession(zero + str(acc.damm_check_digit(zero)))
    assert acc.is_valid_accession(good) and not acc.is_valid_accession(good[:-1])


def test_a_site_prefix_is_checked_by_equality_and_not_by_the_check_digit():
    text = acc.format_accession(date(2026, 10, 4), 9, prefix="blr")
    assert text.startswith("BLR") and len(text) == 15 and acc.damm_valid(text[3:])
    assert acc.parse_accession(text, prefix="BLR").prefix == "BLR"
    with pytest.raises(acc.AccessionInvalid, match="does not start with HYD"):
        acc.parse_accession(text, prefix="HYD")
    with pytest.raises(acc.AccessionInvalid, match="digits only"):
        acc.parse_accession(text)  # a site with no prefix
    for bad in ("B1", "TOOLONG", "", "b-r"):
        with pytest.raises(ValueError):
            acc.format_accession(date(2026, 10, 4), 9, prefix=bad)


# ── 3. The hospital day rolls over at local midnight ─────────────────────────

def test_the_day_rolls_over_at_local_midnight_not_at_the_utc_date_change(ist):
    # 14 March 2031: 18:29 UTC is 23:59 IST, 18:31 UTC is 00:01 IST next day.
    assert acc.hospital_day_for("2031-03-14T18:29:00+00:00") == date(2031, 3, 14)
    assert acc.hospital_day_for("2031-03-14T18:31:00+00:00") == date(2031, 3, 15)
    # 23:59 UTC is 05:29 IST and 00:01 UTC the next UTC date is 05:31 IST:
    # the UTC date changes, the hospital day does not.
    assert acc.hospital_day_for("2031-03-14T23:59:00+00:00") == date(2031, 3, 15)
    assert acc.hospital_day_for("2031-03-15T00:01:00+00:00") == date(2031, 3, 15)


def test_allocation_starts_a_new_sequence_at_local_midnight(ist, clean_days):
    clean_days += ["2031-03-14", "2031-03-15"]
    with db_session(immediate=True) as db:
        before = acc.allocate(db, at="2031-03-14T18:29:00+00:00")   # 23:59 IST
        after = acc.allocate(db, at="2031-03-14T18:31:00+00:00")    # 00:01 IST
        at_0529 = acc.allocate(db, at="2031-03-14T23:59:00+00:00")  # 05:29 IST
        at_0531 = acc.allocate(db, at="2031-03-15T00:01:00+00:00")  # 05:31 IST
    assert before[:6] == "310314" and before[6:11] == "00001"
    assert after[:6] == "310315" and after[6:11] == "00001"
    assert at_0529[:6] == "310315" and at_0529[6:11] == "00002"
    assert at_0531[:6] == "310315" and at_0531[6:11] == "00003"
    for text in (before, after, at_0529, at_0531):
        assert acc.is_valid_accession(text)


# ── 4. Concurrency and transactions ──────────────────────────────────────────

def test_allocation_needs_the_callers_write_transaction(clean_days):
    clean_days.append("2031-04-01")
    with db_session() as db:   # deferred, nothing written yet: not in a transaction
        with pytest.raises(RuntimeError, match="write transaction"):
            acc.allocate(db, at="2031-04-01T06:00:00+00:00")
    assert _counter("2031-04-01") is None


def test_twenty_concurrent_allocations_are_unique_and_consecutive(clean_days):
    clean_days.append("2031-05-02")
    at = "2031-05-02T06:00:00+00:00"
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(20)

    def worker():
        try:
            start.wait(timeout=30)
            with db_session(immediate=True) as db:
                number = acc.allocate(db, at=at)
            with lock:
                results.append(number)
        except BaseException as exc:  # pragma: no cover - reported below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors
    assert len(results) == 20 and len(set(results)) == 20
    sequences = sorted(int(r[6:11]) for r in results)
    assert sequences == list(range(1, 21))
    assert _counter("2031-05-02") == 20


def test_a_rolled_back_transaction_gives_its_number_back(clean_days):
    clean_days.append("2031-06-03")
    at = "2031-06-03T06:00:00+00:00"
    with db_session(immediate=True) as db:
        first = acc.allocate(db, at=at)
    with pytest.raises(RuntimeError, match="specimen insert failed"):
        with db_session(immediate=True) as db:
            acc.allocate(db, at=at)
            raise RuntimeError("specimen insert failed")
    with db_session(immediate=True) as db:
        second = acc.allocate(db, at=at)
    assert int(first[6:11]) == 1 and int(second[6:11]) == 2


# ── 5. The sequence never wraps ──────────────────────────────────────────────

def test_sequence_99999_is_the_last_and_the_next_refuses_loudly(clean_days, caplog):
    clean_days.append("2031-07-04")
    at = "2031-07-04T06:00:00+00:00"
    with db_session() as db:
        db.execute("INSERT INTO lab_accession_counters (hospital_day, last_seq) VALUES (?, ?)",
                   ("2031-07-04", 99998))
    with db_session(immediate=True) as db:
        last = acc.allocate(db, at=at)
    assert last[6:11] == "99999" and acc.is_valid_accession(last)
    caplog.set_level(logging.ERROR, logger="orb.lab.accession")
    with pytest.raises(acc.AccessionOverflow, match="used up") as err:
        with db_session(immediate=True) as db:
            acc.allocate(db, at=at)
    assert err.value.hospital_day == "2031-07-04"
    assert any("used up" in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)
    assert _counter("2031-07-04") == 99999, "nothing wraps back to 00001"
