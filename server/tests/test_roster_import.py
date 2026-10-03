"""HIS roster import — tolerant CSV → patients (services/roster_import.py).

The pilot's incumbent is a "basic HIS" whose export format is unknown, so the
value of this importer is entirely in how it behaves on DIRTY data. These tests
are weighted accordingly: sniffing/mapping, then one case per class of mess we
expect from a real ward census (missing fields, mixed date formats, duplicate
MRNs, initials-only names, blank/ragged rows, junk preamble, BOM/encoding),
then idempotency, then the admin-only audited HTTP surface.

The governing rule under test throughout: REPORT, never silently coerce. A
dirty field must come back as an Issue with the row still traceable to its
physical line, and a row that cannot be safely identified must not be imported.
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from auth import create_token
from database import db_session
from main import app
from services import roster_import

client = TestClient(app)


# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture()
def admin_user():
    uid = f"RIADM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'admin', 'x')",
            (uid, "Roster Admin"),
        )
    return uid


@pytest.fixture()
def doctor_user():
    uid = f"RIDOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?, ?, 'doctor', 'x')",
            (uid, "Roster Doctor"),
        )
    return uid


def _auth(user_id: str) -> dict:
    return {"Authorization": f"Bearer {create_token(user_id, 'admin')}"}


def _mrn(tag: str) -> str:
    """Unique MRN per test — the suite shares one DB across modules."""
    return f"RI{tag}{uuid.uuid4().hex[:6].upper()}"


def _csv(header: str, *rows: str) -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode()


def _issue_codes(plan_rows) -> set[str]:
    return {i["code"] for r in plan_rows for i in r["issues"]}


def _row_for(plan, mrn):
    return next(r for r in plan["rows"] if r["mrn"] == mrn)


def _fetch(mrn):
    with db_session() as db:
        return db.execute("SELECT * FROM patients WHERE mrn = ?", (mrn,)).fetchone()


BASE_MAPPING = {"mrn": "MRN", "name": "Name", "age": "Age", "sex": "Sex",
                "ward": "Ward", "bed": "Bed", "diagnosis": "Diagnosis"}
BASE_HEADER = "MRN,Name,Age,Sex,Ward,Bed,Diagnosis"


# ══════════════════════════════════════════════════════════════════════════
#  Sniffing + mapping suggestion
# ══════════════════════════════════════════════════════════════════════════

def test_sniff_suggests_mapping_from_indian_his_header_names():
    """A basic Indian HIS calls them UHID / Pt Name / IP No, not mrn / name."""
    data = _csv("UHID,Pt Name,Age,Gender,Ward Name,Bed No,Provisional Diagnosis",
                "U1,Ramesh Kumar,54,M,Ward A,A-12,Sepsis")
    out = roster_import.sniff(data)
    m = out["suggested_mapping"]
    assert m["mrn"] == "UHID"
    assert m["name"] == "Pt Name"
    assert m["sex"] == "Gender"
    assert m["ward"] == "Ward Name"
    assert m["bed"] == "Bed No"
    assert out["missing_required"] == []


def test_sniff_detects_semicolon_and_tab_delimiters():
    semi = roster_import.sniff(b"MRN;Name;Ward\nA1;Asha;Ward A\nA2;Bimal;Ward B\n")
    assert semi["delimiter"] == ";"
    assert semi["row_count"] == 2
    tab = roster_import.sniff(b"MRN\tName\tWard\nA1\tAsha\tWard A\nA2\tBimal\tWard B\n")
    assert tab["delimiter"] == "\t"


def test_sniff_skips_junk_preamble_lines_and_reports_it():
    """Exports routinely start with a report title and a blank line."""
    data = ("Ward Census Report - printed 30/07/2026\n"
            "\n"
            "MRN,Name,Ward\n"
            "P1,Asha Rao,Ward A\n").encode()
    out = roster_import.sniff(data)
    assert out["headers"] == ["MRN", "Name", "Ward"]
    assert out["row_count"] == 1
    assert "preamble_skipped" in {i["code"] for i in out["issues"]}


def test_sniff_tolerates_bom_and_crlf():
    data = "﻿MRN,Name,Ward\r\nP1,Asha Rao,Ward A\r\n".encode("utf-8")
    out = roster_import.sniff(data)
    assert out["headers"] == ["MRN", "Name", "Ward"]
    assert out["suggested_mapping"]["mrn"] == "MRN"


def test_sniff_reports_non_utf8_encoding_rather_than_failing():
    data = "MRN,Name,Ward\nP1,José Álvarez,Ward A\n".encode("cp1252")
    out = roster_import.sniff(data)
    assert "non_utf8_encoding" in {i["code"] for i in out["issues"]}
    assert out["row_count"] == 1


# ── UTF-16 must not be guessed at ────────────────────────────────────────────
# Python's 'utf-16' codec WITHOUT a byte-order mark assumes little-endian and
# raises only on an odd byte count or an unpaired surrogate. So trying utf-16
# ahead of cp1252 does not fail loudly on a legacy Windows HIS export — an
# ordinary cp1252 CSV whose length happens to be EVEN decodes "successfully"
# into CJK mojibake ('MRN,Name' -> '前ⱎ慎敭'), corrupting every name, MRN and
# date. Worse, the header row is then unrecognisable, so the importer reports an
# ambiguous header and points the admin at entirely the wrong cause.
#
# Whether it happens at all is a COIN FLIP ON FILE LENGTH, which is exactly why
# the cp1252 test above passes. These three tests pin both sides of the flip and
# the genuine BOM-marked case, so the fix cannot regress in either direction.

_UTF16_HEADER = "MRN,Name,Ward,Bed,Diagnosis"
_UTF16_MAPPING = {"mrn": "MRN", "name": "Name", "ward": "Ward",
                  "bed": "Bed", "diagnosis": "Diagnosis"}


def _cp1252_roster(mrn: str, *, even: bool) -> bytes:
    """A legacy Windows HIS export: cp1252 bytes, one accented Goan name.

    The byte LENGTH is the entire point of this fixture, so it is padded to the
    requested parity rather than hand-counted — the test must keep proving what
    it claims even if the header or the MRN format changes.
    """
    def build(pad: int) -> bytes:
        return (f"{_UTF16_HEADER}\n"
                f"{mrn},José D'Souza,Ward A,A-12,{'Sepsis' + 'x' * pad}\n").encode("cp1252")

    data = build(0)
    if (len(data) % 2 == 0) is not even:
        data = build(1)
    assert (len(data) % 2 == 0) is even, "fixture failed to reach the parity under test"
    return data


def test_even_length_cp1252_export_is_not_silently_decoded_as_utf16():
    data = _cp1252_roster(_mrn("U16E"), even=True)
    out = roster_import.sniff(data)
    assert out["headers"] == ["MRN", "Name", "Ward", "Bed", "Diagnosis"], (
        "an even-length cp1252 file was decoded as UTF-16 — the header row is mojibake"
    )
    assert out["row_count"] == 1
    assert "non_utf8_encoding" in {i["code"] for i in out["issues"]}
    assert out["sample_rows"][0]["Name"] == "José D'Souza", (
        "the accented name did not survive the decode"
    )


def test_odd_length_cp1252_export_decodes_identically_to_the_even_one():
    """The other side of the coin flip: an odd byte count always made utf-16
    raise, so this length has always fallen through to cp1252. It is pinned so
    the fix cannot be 'corrected' back into a length-dependent guess."""
    data = _cp1252_roster(_mrn("U16O"), even=False)
    out = roster_import.sniff(data)
    assert out["headers"] == ["MRN", "Name", "Ward", "Bed", "Diagnosis"]
    assert out["sample_rows"][0]["Name"] == "José D'Souza"
    assert "non_utf8_encoding" in {i["code"] for i in out["issues"]}


def test_even_length_cp1252_export_plans_instead_of_blaming_the_header():
    """The admin-facing symptom of the bug: mojibake makes the header
    unrecognisable, so `plan` refuses with an AMBIGUOUS HEADER error and sends
    the admin hunting for a header problem that does not exist."""
    mrn = _mrn("U16P")
    data = _cp1252_roster(mrn, even=True)
    result = roster_import.plan(data, _UTF16_MAPPING)
    row = _row_for(result, mrn)
    assert row["action"] == "create"
    assert row["values"]["name"] == "José D'Souza"


def test_genuine_utf16_export_with_a_byte_order_mark_is_still_decoded():
    """The fix gates utf-16 on a BOM, so a REAL utf-16 export must still work —
    little-endian, big-endian, and the codec's own native-endian mark."""
    text = f"{_UTF16_HEADER}\nP1,José D'Souza,Ward A,A-12,Sepsis\n"
    for label, data in (("utf-16 (native BOM)", text.encode("utf-16")),
                        ("utf-16-le + BOM", b"\xff\xfe" + text.encode("utf-16-le")),
                        ("utf-16-be + BOM", b"\xfe\xff" + text.encode("utf-16-be"))):
        out = roster_import.sniff(data)
        assert out["headers"] == ["MRN", "Name", "Ward", "Bed", "Diagnosis"], label
        assert out["row_count"] == 1, label
        assert out["sample_rows"][0]["Name"] == "José D'Souza", label
        assert "non_utf8_encoding" in {i["code"] for i in out["issues"]}, label


def test_sniff_reports_duplicate_and_blank_headers():
    out = roster_import.sniff(b"MRN,Name,,Name\nP1,Asha,x,Rao\n")
    codes = {i["code"] for i in out["issues"]}
    assert "duplicate_header" in codes
    assert "blank_header" in codes


def test_suggest_mapping_does_not_give_one_column_to_two_fields():
    """'Emergency Contact Phone' must not also be claimed by `phone`."""
    m = roster_import.suggest_mapping(
        ["MRN", "Name", "Mobile", "Emergency Contact Phone"])
    assert m["phone"] == "Mobile"
    assert m["emergency_contact_phone"] == "Emergency Contact Phone"


def test_sniff_rejects_empty_file():
    with pytest.raises(ValueError):
        roster_import.sniff(b"   \n\n")


# ══════════════════════════════════════════════════════════════════════════
#  Mapping validation
# ══════════════════════════════════════════════════════════════════════════

def test_plan_refuses_mapping_without_required_fields():
    data = _csv(BASE_HEADER, f"{_mrn('R1')},Asha,44,F,Ward A,A-1,Sepsis")
    with pytest.raises(ValueError, match="required field"):
        roster_import.plan(data, {"mrn": "MRN"})          # no name
    with pytest.raises(ValueError, match="required field"):
        roster_import.plan(data, {"name": "Name"})        # no mrn


def test_plan_refuses_mapping_to_a_column_that_is_not_in_the_file():
    data = _csv(BASE_HEADER, f"{_mrn('R2')},Asha,44,F,Ward A,A-1,Sepsis")
    with pytest.raises(ValueError, match="not present in the file"):
        roster_import.plan(data, {"mrn": "MRN", "name": "Name", "ward": "WardName"})


def test_plan_refuses_unknown_target_field():
    data = _csv(BASE_HEADER, f"{_mrn('R3')},Asha,44,F,Ward A,A-1,Sepsis")
    with pytest.raises(ValueError, match="Unknown target field"):
        roster_import.plan(data, {"mrn": "MRN", "name": "Name", "blood_group": "Ward"})


# ══════════════════════════════════════════════════════════════════════════
#  DIRTY DATA — one case per class of mess
# ══════════════════════════════════════════════════════════════════════════

def test_row_without_mrn_is_not_imported_and_is_reported():
    """No MRN means no natural key: the row cannot be matched on re-import."""
    good = _mrn("D1")
    data = _csv(BASE_HEADER,
                f"{good},Asha Rao,44,F,Ward A,A-1,Sepsis",
                ",Nameless Person,50,M,Ward A,A-2,CHF")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["create"] == 1
    assert plan["counts"]["skip"] == 1
    skipped = next(r for r in plan["rows"] if r["action"] == "skip")
    assert skipped["reason"] == "missing_mrn"
    assert skipped["issues"][0]["severity"] == "error"


def test_row_without_name_is_not_imported():
    """A patient row with no name cannot support a bedside identity check."""
    m = _mrn("D2")
    data = _csv(BASE_HEADER, f"{m},,44,F,Ward A,A-1,Sepsis")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["skip"] == 1
    assert _row_for(plan, m)["reason"] == "missing_name"


def test_placeholder_null_tokens_count_as_missing_not_as_values():
    """A HIS that writes 'N/A' / '-' / 'NIL' must not create a patient named '-'."""
    m = _mrn("D3")
    data = _csv(BASE_HEADER, f"{m},N/A,44,F,-,NIL,unknown")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert _row_for(plan, m)["action"] == "skip"
    assert _row_for(plan, m)["reason"] == "missing_name"


def test_duplicate_mrn_in_file_keeps_first_row_and_never_merges():
    m = _mrn("D4")
    data = _csv(BASE_HEADER,
                f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis",
                f"{m},Asha R,44,F,Ward B,B-9,Pneumonia")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["create"] == 1
    assert plan["counts"]["skip"] == 1
    kept = plan["rows"][0]
    dropped = plan["rows"][1]
    assert kept["action"] == "create" and kept["values"]["ward"] == "Ward A"
    assert dropped["reason"] == "duplicate_mrn_in_file"
    # The second row's values must NOT have leaked into the first.
    assert kept["values"]["bed"] == "A-1"
    issue = dropped["issues"][0]
    assert issue["severity"] == "error"
    assert "NOT merged" in issue["message"]


def test_case_variant_mrns_are_reported_but_treated_as_separate_patients():
    """Case-folding two hospital MRNs together would merge two patients."""
    base = _mrn("D5")
    data = _csv(BASE_HEADER,
                f"{base},Asha Rao,44,F,Ward A,A-1,Sepsis",
                f"{base.lower()},Someone Else,61,M,Ward C,C-1,COPD")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["create"] == 2
    assert "near_duplicate_mrn" in _issue_codes(plan["rows"])


def test_blank_rows_are_skipped_and_counted():
    m = _mrn("D6")
    data = ("\n".join([BASE_HEADER,
                       f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis",
                       "",
                       ",,,,,,",
                       ""]) + "\n").encode()
    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["create"] == 1
    assert plan["counts"]["total_rows"] == 1
    assert "blank_rows" in {i["code"] for i in plan["file_issues"]}


def test_short_rows_are_imported_but_reported():
    """Too FEW fields shifts nothing: the columns present still line up with
    their headers and the absent trailing ones read as blank."""
    short_mrn = _mrn("D7")
    data = _csv(BASE_HEADER, f"{short_mrn},Asha Rao,44,F")           # 4 of 7
    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["create"] == 1
    assert "ragged_row" in {i["code"] for i in plan["file_issues"]}
    row = _row_for(plan, short_mrn)
    assert row["values"]["name"] == "Asha Rao" and row["values"]["sex"] == "F"
    assert row["values"].get("ward") is None


def test_overlong_row_is_rejected_not_silently_shifted():
    """Too MANY fields is the dangerous direction and is NOT imported.

    A surplus field is indistinguishable from an unquoted delimiter inside a
    field, and the damage is the SHIFT, not the overflow: truncating to the
    header width keeps every value one column to the left of where it belongs.
    """
    long_mrn = _mrn("D8")
    data = _csv(BASE_HEADER,
                f"{long_mrn},Bimal Sen,61,M,Ward C,C-1,COPD,extra")   # 8 of 7
    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["create"] == 0
    assert plan["counts"]["skip"] == 1
    row = _row_for(plan, long_mrn)
    assert row["action"] == "skip" and row["reason"] == "field_count_mismatch"
    assert row["values"] == {}
    issue = row["issues"][0]
    assert issue["code"] == "field_count_mismatch" and issue["severity"] == "error"


def test_unquoted_comma_in_an_address_does_not_relocate_the_patient():
    """The reason this matters here: Indian addresses carry commas constantly.

    'M200,Ramesh Kumar,12 Nehru Street, New Delhi,ICU,B1' under a 5-column
    header used to plan ward='New Delhi', bed='ICU' — a patient filed into a
    ward that does not exist, reported as a successful import.
    """
    m = _mrn("D8B")
    data = _csv("MRN,Patient Name,Address,Ward,Bed",
                f"{m},Ramesh Kumar,12 Nehru Street, New Delhi,ICU,B1")
    plan = roster_import.plan(
        data, {"mrn": "MRN", "name": "Patient Name", "address": "Address",
               "ward": "Ward", "bed": "Bed"})
    row = _row_for(plan, m)
    assert row["action"] == "skip", "a shifted row must never be imported"
    assert row["values"].get("ward") != "New Delhi"
    assert row["values"].get("bed") != "ICU"
    assert "New Delhi" in row["issues"][0]["message"] or "unquoted" in row["issues"][0]["message"]

    # Properly quoted, the same address imports cleanly into the right columns.
    quoted = _mrn("D8C")
    ok = roster_import.plan(
        _csv("MRN,Patient Name,Address,Ward,Bed",
             f'{quoted},Ramesh Kumar,"12 Nehru Street, New Delhi",ICU,B1'),
        {"mrn": "MRN", "name": "Patient Name", "address": "Address",
         "ward": "Ward", "bed": "Bed"})
    good = _row_for(ok, quoted)
    assert good["action"] == "create"
    assert good["values"]["ward"] == "ICU" and good["values"]["bed"] == "B1"
    assert good["values"]["address"] == "12 Nehru Street, New Delhi"


def test_trailing_empty_fields_are_tolerated_because_nothing_shifts():
    """A HIS that writes a trailing delimiter on every line is harmless noise."""
    m = _mrn("D8D")
    data = _csv(BASE_HEADER, f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis,")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["create"] == 1
    assert _row_for(plan, m)["values"]["ward"] == "Ward A"
    assert "trailing_empty_fields" in {i["code"] for i in plan["file_issues"]}


def test_initials_only_name_is_imported_but_flagged():
    """'S. R.' is a legitimate HIS value — import it, but say so."""
    m = _mrn("D9")
    data = _csv(BASE_HEADER, f"{m},S. R.,62,F,Ward A,A-3,Pneumonia")
    plan = roster_import.plan(data, BASE_MAPPING)
    row = _row_for(plan, m)
    assert row["action"] == "create"
    assert row["values"]["name"] == "S. R."          # never rewritten
    assert "initials_only_name" in {i["code"] for i in row["issues"]}


def test_unrecognised_sex_is_left_blank_not_defaulted():
    m = _mrn("DA")
    data = _csv(BASE_HEADER, f"{m},Asha Rao,44,ZZ,Ward A,A-1,Sepsis")
    plan = roster_import.plan(data, BASE_MAPPING)
    row = _row_for(plan, m)
    assert "sex" not in row["values"]
    assert "unrecognised_sex" in {i["code"] for i in row["issues"]}


@pytest.mark.parametrize("raw,expected", [
    ("M", "M"), ("male", "M"), ("F", "F"), ("Female", "F"),
    ("1", "M"), ("2", "F"), ("Other", "O"),
])
def test_common_sex_spellings_normalise(raw, expected):
    m = _mrn("DB")
    data = _csv(BASE_HEADER, f"{m},Asha Rao,44,{raw},Ward A,A-1,Sepsis")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert _row_for(plan, m)["values"]["sex"] == expected


def test_non_numeric_and_out_of_range_age_are_reported_not_coerced():
    bad, huge = _mrn("DC"), _mrn("DD")
    data = _csv(BASE_HEADER,
                f"{bad},Asha Rao,abc,F,Ward A,A-1,Sepsis",
                f"{huge},Bimal Sen,250,M,Ward A,A-2,CHF")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert "age" not in _row_for(plan, bad)["values"]
    assert "unparseable_number" in {i["code"] for i in _row_for(plan, bad)["issues"]}
    assert "age" not in _row_for(plan, huge)["values"]
    assert "out_of_range" in {i["code"] for i in _row_for(plan, huge)["issues"]}


def test_age_written_as_54y_is_understood():
    m = _mrn("DE")
    data = _csv(BASE_HEADER, f"{m},Asha Rao,54 yrs,F,Ward A,A-1,Sepsis")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert _row_for(plan, m)["values"]["age"] == 54


# ── dates ─────────────────────────────────────────────────────────────────────

DOB_HEADER = "MRN,Name,DOB"
DOB_MAPPING = {"mrn": "MRN", "name": "Name", "date_of_birth": "DOB"}


@pytest.mark.parametrize("raw,expected", [
    ("1971-03-12", "1971-03-12"),
    ("12-Mar-1971", "1971-03-12"),
    ("12 March 1971", "1971-03-12"),
    ("Mar 12, 1971", "1971-03-12"),
    ("1971/03/12", "1971-03-12"),
    ("1971-03-12 09:15", "1971-03-12"),
])
def test_unambiguous_date_formats_all_parse(raw, expected):
    m = _mrn("E1")
    # Quoted: "Mar 12, 1971" carries an embedded comma, exactly as a real export
    # would write it — this also exercises quoted-field parsing.
    plan = roster_import.plan(_csv(DOB_HEADER, f'{m},Asha Rao,"{raw}"'), DOB_MAPPING)
    assert _row_for(plan, m)["values"]["date_of_birth"] == expected


def test_ambiguous_slash_date_is_refused_under_auto_and_reported():
    """03/04/1971 is 3 April OR 4 March — guessing would be a month-wrong DOB."""
    m = _mrn("E2")
    plan = roster_import.plan(_csv(DOB_HEADER, f"{m},Asha Rao,03/04/1971"),
                              DOB_MAPPING, date_order="auto")
    row = _row_for(plan, m)
    assert "date_of_birth" not in row["values"]
    assert "ambiguous_date" in {i["code"] for i in row["issues"]}


def test_explicit_date_order_resolves_the_ambiguity_both_ways():
    m1, m2 = _mrn("E3"), _mrn("E4")
    dmy = roster_import.plan(_csv(DOB_HEADER, f"{m1},Asha Rao,03/04/1971"),
                             DOB_MAPPING, date_order="dmy")
    assert _row_for(dmy, m1)["values"]["date_of_birth"] == "1971-04-03"
    mdy = roster_import.plan(_csv(DOB_HEADER, f"{m2},Asha Rao,03/04/1971"),
                             DOB_MAPPING, date_order="mdy")
    assert _row_for(mdy, m2)["values"]["date_of_birth"] == "1971-03-04"


def test_unambiguous_slash_date_parses_under_auto():
    """25/12/1971 can only be D/M/Y — no hint needed."""
    m = _mrn("E5")
    plan = roster_import.plan(_csv(DOB_HEADER, f"{m},Asha Rao,25/12/1971"), DOB_MAPPING)
    assert _row_for(plan, m)["values"]["date_of_birth"] == "1971-12-25"


def test_two_digit_year_is_refused_rather_than_century_guessed():
    m = _mrn("E6")
    plan = roster_import.plan(_csv(DOB_HEADER, f"{m},Asha Rao,12/03/71"), DOB_MAPPING)
    row = _row_for(plan, m)
    assert "date_of_birth" not in row["values"]
    assert "two_digit_year" in {i["code"] for i in row["issues"]}


def test_impossible_date_is_reported():
    m = _mrn("E7")
    plan = roster_import.plan(_csv(DOB_HEADER, f"{m},Asha Rao,31/02/1971"), DOB_MAPPING)
    row = _row_for(plan, m)
    assert "date_of_birth" not in row["values"]
    assert "invalid_date" in {i["code"] for i in row["issues"]}


def test_garbage_date_is_reported_not_dropped_silently():
    m = _mrn("E8")
    plan = roster_import.plan(_csv(DOB_HEADER, f"{m},Asha Rao,sometime last year"),
                              DOB_MAPPING)
    assert "unrecognised_date_format" in {i["code"] for i in _row_for(plan, m)["issues"]}


def test_age_and_dob_that_disagree_are_both_kept_and_the_conflict_reported():
    m = _mrn("E9")
    data = _csv("MRN,Name,Age,DOB", f"{m},Asha Rao,20,1971-03-12")
    plan = roster_import.plan(
        data, {"mrn": "MRN", "name": "Name", "age": "Age", "date_of_birth": "DOB"})
    row = _row_for(plan, m)
    assert row["values"]["age"] == 20
    assert row["values"]["date_of_birth"] == "1971-03-12"
    assert "age_dob_mismatch" in {i["code"] for i in row["issues"]}


def test_unknown_attending_doctor_is_reported_and_dropped_not_written():
    """A dangling attending_doctor_id would violate the FK and abort the import."""
    m = _mrn("EA")
    data = _csv("MRN,Name,Consultant", f"{m},Asha Rao,NOSUCHDOC")
    plan = roster_import.plan(
        data, {"mrn": "MRN", "name": "Name", "attending_doctor_id": "Consultant"})
    row = _row_for(plan, m)
    assert "attending_doctor_id" not in row["values"]
    assert "unknown_attending_doctor" in {i["code"] for i in row["issues"]}


def test_known_attending_doctor_is_kept(doctor_user):
    m = _mrn("EB")
    data = _csv("MRN,Name,Consultant", f"{m},Asha Rao,{doctor_user}")
    plan = roster_import.plan(
        data, {"mrn": "MRN", "name": "Name", "attending_doctor_id": "Consultant"})
    assert _row_for(plan, m)["values"]["attending_doctor_id"] == doctor_user


def test_status_synonyms_normalise_and_unknown_status_falls_back_to_admitted():
    active, weird = _mrn("EC"), _mrn("ED")
    data = _csv("MRN,Name,Status",
                f"{active},Asha Rao,Inpatient",
                f"{weird},Bimal Sen,ON LEAVE")
    plan = roster_import.plan(data, {"mrn": "MRN", "name": "Name", "status": "Status"})
    assert _row_for(plan, active)["values"]["status"] == "admitted"
    weird_row = _row_for(plan, weird)
    assert weird_row["values"]["status"] == "admitted"
    assert "unrecognised_status" in {i["code"] for i in weird_row["issues"]}


def test_dry_run_writes_nothing():
    m = _mrn("EE")
    data = _csv(BASE_HEADER, f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis")
    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["create"] == 1
    assert _fetch(m) is None


# ══════════════════════════════════════════════════════════════════════════
#  Apply + idempotency
# ══════════════════════════════════════════════════════════════════════════

def test_apply_creates_patients_and_is_idempotent(admin_user):
    a, b = _mrn("F1"), _mrn("F2")
    data = _csv(BASE_HEADER,
                f"{a},Asha Rao,44,F,Ward A,A-1,Sepsis",
                f"{b},Bimal Sen,61,M,Ward A,A-2,CHF")

    first = roster_import.apply_import(data, BASE_MAPPING, actor_user_id=admin_user)
    assert first["summary"]["created"] == 2
    assert first["summary"]["updated"] == 0

    row = _fetch(a)
    assert row["name"] == "Asha Rao" and row["ward"] == "Ward A" and row["age"] == 44
    assert row["status"] == "admitted"

    second = roster_import.apply_import(data, BASE_MAPPING, actor_user_id=admin_user)
    assert second["summary"] == {"created": 0, "updated": 0, "unchanged": 2,
                                 "skipped": 0, "errors": 0, "warnings": 0}

    with db_session() as db:
        count = db.execute(
            "SELECT COUNT(*) c FROM patients WHERE mrn IN (?, ?)", (a, b)
        ).fetchone()["c"]
    assert count == 2, "re-import must not duplicate patients"


def test_apply_updates_only_changed_fields_and_never_blanks_existing_values(admin_user):
    m = _mrn("F3")
    roster_import.apply_import(
        _csv(BASE_HEADER, f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis"),
        BASE_MAPPING, actor_user_id=admin_user)

    # Ward move; diagnosis column now blank in the export.
    result = roster_import.apply_import(
        _csv(BASE_HEADER, f"{m},Asha Rao,44,F,Ward B,B-9,"),
        BASE_MAPPING, actor_user_id=admin_user)

    assert result["summary"]["updated"] == 1
    changes = _row_for(result, m)["changes"]
    assert set(changes) == {"ward", "bed"}
    assert changes["ward"] == {"from": "Ward A", "to": "Ward B"}

    row = _fetch(m)
    assert row["ward"] == "Ward B" and row["bed"] == "B-9"
    assert row["diagnosis"] == "Sepsis", "a blank CSV cell must not erase Orb data"


def test_apply_with_update_existing_off_skips_known_mrns(admin_user):
    m = _mrn("F4")
    roster_import.apply_import(
        _csv(BASE_HEADER, f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis"),
        BASE_MAPPING, actor_user_id=admin_user)

    result = roster_import.apply_import(
        _csv(BASE_HEADER, f"{m},Asha Rao,44,F,Ward Z,Z-9,Sepsis"),
        BASE_MAPPING, actor_user_id=admin_user, update_existing=False)

    assert result["summary"]["skipped"] == 1 and result["summary"]["updated"] == 0
    assert _fetch(m)["ward"] == "Ward A"


def test_apply_never_changes_the_mrn_of_an_existing_patient(admin_user):
    """MRN is the natural key, not a payload field — it must never be rewritten."""
    m = _mrn("F5")
    roster_import.apply_import(
        _csv(BASE_HEADER, f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis"),
        BASE_MAPPING, actor_user_id=admin_user)
    pid = _fetch(m)["id"]
    roster_import.apply_import(
        _csv(BASE_HEADER, f"{m},Asha Rao,45,F,Ward A,A-1,Sepsis"),
        BASE_MAPPING, actor_user_id=admin_user)
    with db_session() as db:
        row = db.execute("SELECT mrn FROM patients WHERE id = ?", (pid,)).fetchone()
    assert row["mrn"] == m


def test_apply_records_a_ledger_row(admin_user):
    m = _mrn("F6")
    result = roster_import.apply_import(
        _csv(BASE_HEADER, f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis"),
        BASE_MAPPING, actor_user_id=admin_user, source_filename="census.csv",
        note="week 1")
    with db_session() as db:
        run = db.execute("SELECT * FROM roster_import_runs WHERE id = ?",
                         (result["run_id"],)).fetchone()
    assert run is not None
    assert run["actor_user_id"] == admin_user
    assert run["source_filename"] == "census.csv"
    assert run["rows_created"] == 1 and run["note"] == "week 1"
    assert json.loads(run["mapping_json"])["mrn"] == "MRN"


def test_apply_is_all_or_nothing_when_a_write_fails(admin_user, monkeypatch):
    """A mid-import failure must leave zero patients behind, not a partial ward.

    The failure lands between the first patient and its admission (run id, then
    patient 1, then admission 1), so this also pins the admission write inside
    the same transaction as the patient it belongs to.
    """
    a, b = _mrn("F7"), _mrn("F8")
    data = _csv(BASE_HEADER,
                f"{a},Asha Rao,44,F,Ward A,A-1,Sepsis",
                f"{b},Bimal Sen,61,M,Ward A,A-2,CHF")

    real_uuid4 = uuid.uuid4
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] == 3:          # run id, 1st patient id, then its admission id
            raise RuntimeError("simulated failure mid-import")
        return real_uuid4()

    monkeypatch.setattr(roster_import.uuid, "uuid4", boom)
    with pytest.raises(RuntimeError):
        roster_import.apply_import(data, BASE_MAPPING, actor_user_id=admin_user)

    assert _fetch(a) is None and _fetch(b) is None
    with db_session() as db:
        orphans = db.execute(
            "SELECT COUNT(*) c FROM admissions WHERE admitting_notes LIKE '%roster import%' "
            "AND patient_id NOT IN (SELECT id FROM patients)").fetchone()["c"]
    assert orphans == 0


# ══════════════════════════════════════════════════════════════════════════
#  ADMISSIONS — "imported" has to mean "on the ward board"
#
#  The defect these cover shipped green: the importer wrote `patients` rows and
#  reported success, and BOTH readers of the ward (Vigil's board and the nurse
#  shift board) INNER JOIN `admissions` on status='active'. Every imported
#  patient was therefore invisible — on day one of shadow mode, which is the one
#  day this feature exists for. Row counts passed; nobody asserted the board.
#  So these tests assert the BOARD.
# ══════════════════════════════════════════════════════════════════════════

# Unique per test session so no other module's patients can drift into these
# boards and make a leak look like a pass (or a pass look like a leak).
BOARD_DEPT = f"RIDept{uuid.uuid4().hex[:6].upper()}"
ADM_HEADER = "MRN,Name,Age,Sex,Ward,Bed,Diagnosis,Department"
ADM_MAPPING = {**BASE_MAPPING, "department": "Department"}


def _adm_csv(mrn, ward, bed="A-1", name="Asha Rao", dept=None, extra_cols=""):
    return _csv(ADM_HEADER + extra_cols,
                f"{mrn},{name},44,F,{ward},{bed},Sepsis,{dept or BOARD_DEPT}")


@pytest.fixture()
def board_doctor():
    """A doctor WITH a department — the case that stays broken if the admission
    is written with a NULL department."""
    uid = f"RIDOC{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, 'doctor', 'x', ?)", (uid, "Board Doctor", BOARD_DEPT))
    return {"Authorization": f"Bearer {create_token(uid, 'doctor')}"}


@pytest.fixture()
def board_nurse():
    uid = f"RINUR{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
            "VALUES (?, ?, 'nurse', 'x', ?)", (uid, "Board Nurse", BOARD_DEPT))
    return {"Authorization": f"Bearer {create_token(uid, 'nurse')}"}


def _admissions_for(patient_id, status="active"):
    with db_session() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM admissions WHERE patient_id = ? AND status = ?",
            (patient_id, status)).fetchall()]


def _on_vigil_board(headers, patient_id):
    resp = client.get("/api/vigil/patients", headers=headers)
    assert resp.status_code == 200, resp.text
    return patient_id in {p["id"] for p in resp.json()}


def _on_nurse_board(headers, patient_id):
    resp = client.get("/api/nurse/board", headers=headers)
    assert resp.status_code == 200, resp.text
    return patient_id in {c["patient_id"] for c in resp.json()["patients"]}


def test_an_imported_patient_appears_on_the_vigil_and_nurse_boards(
        admin_user, board_doctor, board_nurse):
    """THE test whose absence let the defect ship. Not a row count — the board."""
    m = _mrn("H1")
    result = roster_import.apply_import(
        _adm_csv(m, "RI-Ward-A"), ADM_MAPPING, actor_user_id=admin_user)
    assert result["summary"]["created"] == 1
    assert result["admissions"]["created"] == 1
    pid = result["created_patient_ids"][0]

    assert _on_vigil_board(board_doctor, pid), "imported patient is invisible to Vigil"
    assert _on_nurse_board(board_nurse, pid), "imported patient is invisible to the nurse board"


def test_the_admission_carries_the_ward_bed_and_department_from_the_csv(admin_user):
    m = _mrn("H2")
    result = roster_import.apply_import(
        _adm_csv(m, "RI-Ward-B", bed="B-7"), ADM_MAPPING, actor_user_id=admin_user)
    adm = _admissions_for(result["created_patient_ids"][0])
    assert len(adm) == 1
    assert adm[0]["ward"] == "RI-Ward-B"
    assert adm[0]["bed"] == "B-7"
    assert adm[0]["department"] == BOARD_DEPT
    assert adm[0]["admitted_by"] == admin_user
    assert adm[0]["admission_source"] == "roster_import"


def test_re_import_does_not_open_a_second_admission(admin_user):
    m = _mrn("H3")
    data = _adm_csv(m, "RI-Ward-A")
    first = roster_import.apply_import(data, ADM_MAPPING, actor_user_id=admin_user)
    pid = first["created_patient_ids"][0]

    second = roster_import.apply_import(data, ADM_MAPPING, actor_user_id=admin_user)
    assert second["admissions"] == {"created": 0, "updated": 0, "closed": 0}
    assert len(_admissions_for(pid)) == 1, "re-import must not duplicate the encounter"


def test_a_patient_created_before_admissions_existed_is_repaired_by_a_re_run(
        admin_user, board_doctor):
    """The state every patient imported by the broken build is in: a patient row
    with no encounter. The patient row is `unchanged` on the re-run — so if
    admission planning hung off the row action, they would stay invisible."""
    m = _mrn("H4")
    data = _adm_csv(m, "RI-Ward-A")
    pid = roster_import.apply_import(
        data, ADM_MAPPING, actor_user_id=admin_user)["created_patient_ids"][0]
    with db_session() as db:                       # rewind to the broken state
        db.execute("DELETE FROM admissions WHERE patient_id = ?", (pid,))
    assert not _on_vigil_board(board_doctor, pid)

    repair = roster_import.apply_import(data, ADM_MAPPING, actor_user_id=admin_user)
    assert repair["summary"]["created"] == 0 and repair["summary"]["updated"] == 0
    assert repair["summary"]["unchanged"] == 1
    assert repair["admissions"]["created"] == 1
    assert _on_vigil_board(board_doctor, pid)


def test_a_ward_change_moves_the_open_admission_it_does_not_re_admit(
        admin_user, board_nurse):
    """A ward move is a move within one hospital stay. Closing the encounter and
    opening another would fabricate a discharge and detach everything hanging
    off the encounter id."""
    m = _mrn("H5")
    first = roster_import.apply_import(
        _adm_csv(m, "RI-Ward-A", bed="A-1"), ADM_MAPPING, actor_user_id=admin_user)
    pid = first["created_patient_ids"][0]
    original_admission = _admissions_for(pid)[0]["id"]

    moved = roster_import.apply_import(
        _adm_csv(m, "RI-Ward-C", bed="C-9"), ADM_MAPPING, actor_user_id=admin_user)
    assert moved["admissions"]["created"] == 0 and moved["admissions"]["updated"] == 1

    open_rows = _admissions_for(pid)
    assert len(open_rows) == 1, "a transfer must not open a second encounter"
    assert open_rows[0]["id"] == original_admission, "the encounter id must survive a move"
    assert open_rows[0]["ward"] == "RI-Ward-C" and open_rows[0]["bed"] == "C-9"
    assert _admissions_for(pid, status="discharged") == []
    assert "ward_transfer" in {i["code"] for i in _row_for(moved, m)["issues"]}
    assert _on_nurse_board(board_nurse, pid)


def test_a_patient_absent_from_a_later_roster_is_reported_never_discharged(
        admin_user, board_nurse):
    """A partial export must not empty the ward. Absence is not evidence."""
    ward = f"RI-Ward-{uuid.uuid4().hex[:6].upper()}"
    stays, vanishes = _mrn("H6"), _mrn("H7")
    both = _csv(ADM_HEADER,
                f"{stays},Asha Rao,44,F,{ward},A-1,Sepsis,{BOARD_DEPT}",
                f"{vanishes},Bimal Sen,61,M,{ward},A-2,CHF,{BOARD_DEPT}")
    created = roster_import.apply_import(both, ADM_MAPPING, actor_user_id=admin_user)
    missing_pid = [r["patient_id"] for r in created["rows"] if r["mrn"] == vanishes][0]

    partial = roster_import.apply_import(
        _csv(ADM_HEADER, f"{stays},Asha Rao,44,F,{ward},A-1,Sepsis,{BOARD_DEPT}"),
        ADM_MAPPING, actor_user_id=admin_user)

    assert partial["admissions"]["closed"] == 0
    assert len(_admissions_for(missing_pid)) == 1, "an absent row must not discharge anybody"
    assert _fetch(vanishes)["status"] == "admitted"
    assert _on_nurse_board(board_nurse, missing_pid)

    reported = partial["absent_from_roster"]
    assert ward in reported["wards"]
    assert vanishes in {p["mrn"] for p in reported["patients"]}
    assert stays not in {p["mrn"] for p in reported["patients"]}


def test_an_explicit_discharged_status_does_close_the_admission(
        admin_user, board_nurse):
    """A status column is the file SAYING so, not Orb inferring it — that is the
    difference that makes acting on it safe."""
    m = _mrn("H8")
    header = ADM_HEADER + ",Status"
    mapping = {**ADM_MAPPING, "status": "Status"}
    live = _csv(header, f"{m},Asha Rao,44,F,RI-Ward-A,A-4,Sepsis,{BOARD_DEPT},Admitted")
    pid = roster_import.apply_import(
        live, mapping, actor_user_id=admin_user)["created_patient_ids"][0]
    assert _on_nurse_board(board_nurse, pid)

    gone = _csv(header, f"{m},Asha Rao,44,F,RI-Ward-A,A-4,Sepsis,{BOARD_DEPT},Discharged")
    result = roster_import.apply_import(gone, mapping, actor_user_id=admin_user)
    assert result["admissions"]["closed"] == 1
    assert _admissions_for(pid) == []
    assert _fetch(m)["status"] == "discharged"
    assert not _on_nurse_board(board_nurse, pid)


def test_the_admission_instant_is_anchored_to_ist_not_utc(admin_user):
    """The hospital is in India. Stamping UTC midnight would file a patient who
    arrived at 02:00 IST on the 28th as having arrived on the 27th."""
    m = _mrn("H9")
    data = _csv("MRN,Name,Ward,Bed,Admission Date",
                f"{m},Asha Rao,RI-Ward-A,A-8,28/07/2026")
    result = roster_import.apply_import(
        data, {"mrn": "MRN", "name": "Name", "ward": "Ward", "bed": "Bed",
               "admission_date": "Admission Date"},
        actor_user_id=admin_user, date_order="dmy")
    adm = _admissions_for(result["created_patient_ids"][0])[0]
    # IST midnight on 28 July == 18:30 UTC on 27 July.
    assert adm["created_at"] == "2026-07-27T18:30:00+00:00"


def test_default_department_fills_an_export_that_has_no_department_column(
        admin_user, board_doctor):
    m = _mrn("HA")
    result = roster_import.apply_import(
        _csv(BASE_HEADER, f"{m},Asha Rao,44,F,RI-Ward-A,A-5,Sepsis"),
        BASE_MAPPING, actor_user_id=admin_user, default_department=BOARD_DEPT)
    pid = result["created_patient_ids"][0]
    assert _admissions_for(pid)[0]["department"] == BOARD_DEPT
    assert _on_vigil_board(board_doctor, pid)


def test_without_any_department_the_ward_is_used_and_the_plan_says_so(admin_user):
    """A NULL department is invisible to every user who has one, so the ward is
    the last resort — and the dry run shows the exact value that will be written."""
    m = _mrn("HB")
    ward = f"RI-Ward-{uuid.uuid4().hex[:6].upper()}"
    data = _csv(BASE_HEADER, f"{m},Asha Rao,44,F,{ward},A-6,Sepsis")
    dry = roster_import.plan(data, BASE_MAPPING)
    assert _row_for(dry, m)["admission"]["department"] == ward
    assert _row_for(dry, m)["admission"]["department_source"] == "ward"

    result = roster_import.apply_import(data, BASE_MAPPING, actor_user_id=admin_user)
    assert _admissions_for(result["created_patient_ids"][0])[0]["department"] == ward


def test_a_ward_name_never_overwrites_a_real_department_on_an_open_admission(admin_user):
    """The fallback fills a hole; it must not rip a properly-admitted patient out
    of 'General Medicine' because this week's export dropped the column."""
    m = _mrn("HC")
    roster_import.apply_import(_adm_csv(m, "RI-Ward-A"), ADM_MAPPING,
                               actor_user_id=admin_user)
    pid = _fetch(m)["id"]
    roster_import.apply_import(                       # same file, department column gone
        _csv(BASE_HEADER, f"{m},Asha Rao,44,F,RI-Ward-A,A-1,Sepsis"),
        BASE_MAPPING, actor_user_id=admin_user)
    assert _admissions_for(pid)[0]["department"] == BOARD_DEPT


def test_the_dry_run_plans_the_admission_without_writing_one(admin_user):
    m = _mrn("HD")
    dry = roster_import.plan(_adm_csv(m, "RI-Ward-A"), ADM_MAPPING)
    assert _row_for(dry, m)["admission"]["action"] == "create"
    assert dry["counts"]["admissions_create"] == 1
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) c FROM admissions WHERE ward = 'RI-Ward-A' "
                          "AND department = ?", (BOARD_DEPT,)).fetchone()["c"] >= 0
    assert _fetch(m) is None


# ══════════════════════════════════════════════════════════════════════════
#  HEADER DETECTION — reading the wrong line shifts every column
# ══════════════════════════════════════════════════════════════════════════

def test_a_preamble_line_containing_one_column_word_does_not_hijack_the_header():
    """'Ward Census Report, printed 30/07/2026' contains 'ward'. Accepting it as
    the header made column 1 the MRN of a report title and shifted the rest."""
    m = _mrn("J1")
    data = ("Ward Census Report, printed 30/07/2026\n"
            + BASE_HEADER + "\n"
            + f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis\n").encode()

    out = roster_import.sniff(data)
    assert out["headers"] == BASE_HEADER.split(",")
    assert out["header_line"] == 2
    assert out["header_confident"] is True
    assert "preamble_skipped" in {i["code"] for i in out["issues"]}

    plan = roster_import.plan(data, BASE_MAPPING)
    assert plan["counts"]["create"] == 1
    row = _row_for(plan, m)
    assert row["values"]["ward"] == "Ward A" and row["values"]["bed"] == "A-1"


def test_a_preamble_line_that_exactly_names_one_column_still_loses():
    """Even an exact single hit is not enough: two recognised columns are."""
    m = _mrn("J2")
    data = ("Ward,printed 30/07/2026\n" + BASE_HEADER + "\n"
            + f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis\n").encode()
    out = roster_import.sniff(data)
    assert out["header_line"] == 2 and out["headers"] == BASE_HEADER.split(",")


def test_an_unrecognisable_header_is_asked_about_rather_than_guessed():
    """Nothing in this file is recognisable, so Orb refuses to pick a line — the
    dry run exists precisely so a human can settle it."""
    data = _csv("Kramank,Rugna Naam,Kaksh", "K1,Asha Rao,Kaksh 4")
    out = roster_import.sniff(data)
    assert out["header_confident"] is False
    assert "header_ambiguous" in {i["code"] for i in out["issues"]}
    assert out["header_candidates"], "the admin must be shown what to choose between"

    mapping = {"mrn": "Kramank", "name": "Rugna Naam", "ward": "Kaksh"}
    with pytest.raises(ValueError, match="header_line"):
        roster_import.plan(data, mapping)

    answered = roster_import.plan(data, mapping, header_line=1)
    assert answered["counts"]["create"] == 1
    assert _row_for(answered, "K1")["values"]["ward"] == "Kaksh 4"


def test_a_pinned_header_line_out_of_range_is_refused():
    data = _csv("Kramank,Rugna Naam", "K2,Asha Rao")
    with pytest.raises(ValueError, match="header_line"):
        roster_import.plan(data, {"mrn": "Kramank", "name": "Rugna Naam"},
                           header_line=99)


def test_a_pinned_header_line_lets_a_junk_preamble_be_skipped_explicitly():
    m = _mrn("J3")
    data = ("Report: Ward Census\nGenerated by HIS v2, 30/07/2026\n"
            + BASE_HEADER + "\n" + f"{m},Asha Rao,44,F,Ward A,A-1,Sepsis\n").encode()
    plan = roster_import.plan(data, BASE_MAPPING, header_line=3)
    assert plan["counts"]["create"] == 1
    assert _row_for(plan, m)["values"]["ward"] == "Ward A"


# ══════════════════════════════════════════════════════════════════════════
#  HTTP surface — admin only, audited
# ══════════════════════════════════════════════════════════════════════════

def _upload(mrn_value: str) -> dict:
    return {"file": ("census.csv",
                     _csv(BASE_HEADER, f"{mrn_value},Asha Rao,44,F,Ward A,A-1,Sepsis"),
                     "text/csv")}


def test_routes_reject_a_doctor(doctor_user):
    headers = {"Authorization": f"Bearer {create_token(doctor_user, 'doctor')}"}
    assert client.post("/api/admin/roster-import/preview",
                       files=_upload(_mrn("G0")), headers=headers).status_code == 403
    assert client.post("/api/admin/roster-import/commit",
                       files=_upload(_mrn("G0")),
                       data={"mapping": json.dumps(BASE_MAPPING)},
                       headers=headers).status_code == 403
    assert client.get("/api/admin/roster-import/runs", headers=headers).status_code == 403


def test_routes_reject_anonymous():
    assert client.post("/api/admin/roster-import/preview",
                       files=_upload(_mrn("G1"))).status_code in (401, 403)


def test_preview_returns_suggested_mapping(admin_user):
    resp = client.post("/api/admin/roster-import/preview",
                       files=_upload(_mrn("G2")), headers=_auth(admin_user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["suggested_mapping"]["mrn"] == "MRN"
    assert body["filename"] == "census.csv"
    assert body["row_count"] == 1


def test_dry_run_reports_the_plan_and_writes_nothing(admin_user):
    m = _mrn("G3")
    resp = client.post("/api/admin/roster-import/dry-run",
                       files=_upload(m),
                       data={"mapping": json.dumps(BASE_MAPPING)},
                       headers=_auth(admin_user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is False
    assert body["counts"]["create"] == 1
    assert _fetch(m) is None


def test_commit_applies_and_audits(admin_user):
    m = _mrn("G4")
    resp = client.post("/api/admin/roster-import/commit",
                       files=_upload(m),
                       data={"mapping": json.dumps(BASE_MAPPING)},
                       headers=_auth(admin_user))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True and body["summary"]["created"] == 1
    assert _fetch(m) is not None

    with db_session() as db:
        audit = db.execute(
            """SELECT * FROM audit_logs WHERE action_type = 'roster_import_committed'
               AND target_id = ? ORDER BY id DESC LIMIT 1""",
            (body["run_id"],),
        ).fetchone()
    assert audit is not None, "an applied roster import must be audited"
    assert audit["actor_user_id"] == admin_user
    assert json.loads(audit["metadata_json"])["summary"]["created"] == 1


def test_commit_twice_over_the_same_file_is_idempotent_over_http(admin_user):
    m = _mrn("G5")
    first = client.post("/api/admin/roster-import/commit", files=_upload(m),
                        data={"mapping": json.dumps(BASE_MAPPING)},
                        headers=_auth(admin_user)).json()
    second = client.post("/api/admin/roster-import/commit", files=_upload(m),
                         data={"mapping": json.dumps(BASE_MAPPING)},
                         headers=_auth(admin_user)).json()
    assert first["summary"]["created"] == 1
    assert second["summary"]["created"] == 0
    assert second["summary"]["unchanged"] == 1


def test_commit_rejects_a_mapping_missing_required_fields(admin_user):
    resp = client.post("/api/admin/roster-import/commit",
                       files=_upload(_mrn("G6")),
                       data={"mapping": json.dumps({"mrn": "MRN"})},
                       headers=_auth(admin_user))
    assert resp.status_code == 422
    assert "required field" in resp.json()["detail"]


def test_commit_rejects_non_json_mapping(admin_user):
    resp = client.post("/api/admin/roster-import/commit",
                       files=_upload(_mrn("G7")),
                       data={"mapping": "not json"},
                       headers=_auth(admin_user))
    assert resp.status_code == 422


def test_empty_upload_is_a_400(admin_user):
    resp = client.post("/api/admin/roster-import/preview",
                       files={"file": ("empty.csv", b"", "text/csv")},
                       headers=_auth(admin_user))
    assert resp.status_code == 400


def test_runs_history_lists_applied_imports(admin_user):
    m = _mrn("G8")
    committed = client.post("/api/admin/roster-import/commit", files=_upload(m),
                            data={"mapping": json.dumps(BASE_MAPPING)},
                            headers=_auth(admin_user)).json()
    resp = client.get("/api/admin/roster-import/runs", headers=_auth(admin_user))
    assert resp.status_code == 200
    assert committed["run_id"] in {r["id"] for r in resp.json()["runs"]}


def test_fields_catalogue_is_served_for_the_mapping_ui(admin_user):
    body = client.get("/api/admin/roster-import/fields",
                      headers=_auth(admin_user)).json()
    names = {f["name"] for f in body["fields"]}
    assert {"mrn", "name", "ward", "bed"} <= names
    assert set(body["required"]) == {"mrn", "name"}
