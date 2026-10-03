"""The roster dry run counts the dates it will write blank.

The admin UI holds Apply on this number until a date order is chosen or the
blanks are accepted. Before 2026-10-02 the dry run said "3 warnings" and never
showed which, then committed patients with DOB and admission date blanked.
"""
import uuid

from services import roster_import

HEADER = "MRN,Name,DOB"
MAPPING = {"mrn": "MRN", "name": "Name", "date_of_birth": "DOB"}


def _mrn(tag):
    return f"RDB{tag}{uuid.uuid4().hex[:6].upper()}"


def _csv(*rows):
    return ("\n".join([HEADER, *rows]) + "\n").encode()


def test_ambiguous_and_two_digit_dates_are_counted_as_left_blank():
    content = _csv(f"{_mrn('A')},Asha Rao,03/04/1971",   # ambiguous under auto
                   f"{_mrn('B')},Ravi Kumar,03/04/71",    # two-digit year
                   f"{_mrn('C')},Meena Iyer,25/12/1971")  # only D/M/Y: parses
    plan = roster_import.plan(content, MAPPING, date_order="auto")
    assert plan["counts"]["dates_left_blank"] == 2


def test_choosing_day_month_year_clears_the_ambiguous_one():
    content = _csv(f"{_mrn('D')},Asha Rao,03/04/1971")
    assert roster_import.plan(content, MAPPING, date_order="dmy")["counts"]["dates_left_blank"] == 0
    assert roster_import.plan(content, MAPPING, date_order="auto")["counts"]["dates_left_blank"] == 1


def test_skipped_rows_do_not_count():
    # No name: the row is skipped, so its date is never written at all.
    content = _csv(f"{_mrn('E')},,03/04/1971")
    assert roster_import.plan(content, MAPPING, date_order="auto")["counts"]["dates_left_blank"] == 0
