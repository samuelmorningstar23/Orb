"""The patient index matcher (opd-5, W4-A, 4 October 2026).

services/patient_matching.py is deterministic and explainable: every point a
candidate scores has a named reason. What this file pins:

  1. Name normalisation: honorifics, the Mohammed family, Laxmi and Lakshmi,
     Pooja and Puja, word order, initials, relation clauses and "Baby of".
  2. Phone, date of birth, sex and UHID keys.
  3. The score and the bands (probable 70+, possible 50 to 69), the age
     points and the sex penalty.
  4. The self-healing key table: a patient written by ANY door (the admission
     service, an importer rewriting a phone) is found by the next match, and a
     sync with nothing to do recomputes nothing.
  5. Hard matches on the UHID (any case and separators) and an active ABHA.
  6. Scale: sync_keys plus a match on 100,000 synthetic patients in an
     ENCRYPTED (SQLCipher) database, under a stated budget.
"""
from __future__ import annotations

import os
import random
import tempfile
import time
import uuid
from datetime import date

import pytest

from database import db_session
from services import patient_matching as pm

TODAY = date(2026, 10, 4)


# ── 1. Names ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a, b", [
    ("Mohd Rafiq", "Mohammed Rafiq"),
    ("Md. Rafiq", "Muhammad Rafiq"),
    ("Mohamed Rafiq", "Mohammad Rafiq"),
    ("Laxmi Rao", "Lakshmi Rao"),
    ("Pooja Sharma", "Puja Sarma"),
    ("Geetha Nair", "Gita Nair"),
    ("Siddharth Menon", "Sidharth Menon"),
    ("Gaurav Joshi", "Gourav Joshi"),
    ("Fatimah Khan", "Fatima Khan"),
    ("Rao Lakshmi", "Lakshmi Rao"),
    ("S. Lakshmi", "Lakshmi"),
    ("Mrs. Sunita Devi", "Sunita Devi"),
    ("Smt Sunita Devi", "Sunita Devi"),
    ("Shri Ramesh Kumar", "Ramesh Kumar"),
    ("Dr Anil Reddy", "Anil Reddy"),
    ("Kumari Priya Verma", "Priya Verma"),
    ("Baby of Lakshmi", "B/O Laxmi"),
    ("Ramesh S/O Suresh", "Ramesh"),
    ("Ramesh son of Suresh Kumar", "Ramesh"),
])
def test_name_variants_share_one_key(a, b):
    assert pm.name_key(a) == pm.name_key(b), (a, b, pm.name_key(a), pm.name_key(b))


@pytest.mark.parametrize("a, b", [
    ("Lakshmi Rao", "Lakshmi Iyer"),
    ("Baby of Lakshmi", "Lakshmi"),
    ("Priya Kumari", "Priya"),          # a surname is not an honorific
    ("Minh Ho", "Minh"),                # a real two-letter name is kept
])
def test_different_names_keep_different_keys(a, b):
    assert pm.name_key(a) != pm.name_key(b)


def test_a_baby_of_name_never_scores_against_the_parent():
    assert pm.name_points(pm.name_key("Baby of Lakshmi"), pm.name_key("Lakshmi")) == (0, None)
    assert pm.name_points(pm.name_key("B/O Lakshmi Rao"), pm.name_key("Lakshmi Rao")) == (0, None)


def test_a_distinctive_shared_word_is_a_partial_name_match_a_common_one_is_not():
    assert pm.name_points(pm.name_key("Lakshmi Devi"), pm.name_key("Lakshmi Rao")) == (
        pm.POINTS_NAME_OVERLAP, "name_overlap")
    assert pm.name_points(pm.name_key("Sunita Devi"), pm.name_key("Anita Devi")) == (0, None)
    assert pm.name_points(pm.name_key("Ravi Kumar"), pm.name_key("Ramesh Kumar")) == (0, None)


def test_a_stated_name_may_be_shorter_never_different():
    assert pm.stated_name_matches("Lakshmi", "Lakshmi Devi Rao")
    assert pm.stated_name_matches("laxmi rao", "Mrs Lakshmi Rao")
    assert not pm.stated_name_matches("Lakshmi Iyer", "Lakshmi Devi Rao")
    assert not pm.stated_name_matches("Lakshmi", "Baby of Lakshmi")
    assert not pm.stated_name_matches("", "Lakshmi")


def test_names_in_other_scripts_keep_their_vowel_signs():
    assert pm.name_key("राम कुमार") == " ".join(sorted(["राम", "कुमार"]))
    assert pm.name_key("कुमार राम") == pm.name_key("राम कुमार")
    assert pm.name_key("राम कुमार") != pm.name_key("रमा कुमार")
    assert pm.name_key("José Dsouza") == pm.name_key("Jose Dsouza")


def test_a_name_with_no_letters_has_no_key():
    assert pm.name_key("1234") is None
    assert pm.name_key(None) is None


# ── 2. Other keys ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["+91 98450 12345", "098450-12345", "9845012345",
                                 "0091 9845012345", "(98450) 12345"])
def test_phone_formats_share_one_key(raw):
    assert pm.phone_key(raw) == "9845012345"


@pytest.mark.parametrize("raw", ["9999999999", "1234567890", "12345", "", None, "0000000000"])
def test_placeholders_and_fragments_are_not_phone_keys(raw):
    assert pm.phone_key(raw) is None


def test_phone_last4():
    assert pm.phone_last4("+91 98450 12345") == "2345"
    assert pm.phone_last4("12") is None


def test_dob_key_uses_the_discharge_portals_rule():
    assert pm.dob_key("1980-05-05") == "1980-05-05"
    assert pm.dob_key("1980-05-05 00:00:00") == "1980-05-05"
    assert pm.dob_key("05/05/1980") is None
    assert pm.dob_key("2999-01-01") is None


@pytest.mark.parametrize("raw, key", [("M", "M"), ("male", "M"), ("F", "F"), ("Female", "F"),
                                      ("O", "O"), ("x", None), (None, None)])
def test_sex_key(raw, key):
    assert pm.sex_key(raw) == key


def test_uhid_key_ignores_case_and_separators():
    assert pm.uhid_key("mrn-00 12/3.") == pm.uhid_key("MRN00123") == "MRN00123"
    assert pm.uhid_key("  ") is None


def test_current_age_from_dob_or_a_stated_age_that_ages():
    assert pm.current_age(date_of_birth="1980-10-05", stated_age=None, age_recorded_on=None,
                          created_at=None, today=TODAY) == (45, False)
    assert pm.current_age(date_of_birth=None, stated_age=40, age_recorded_on="2024-10-01",
                          created_at=None, today=TODAY) == (42, True)
    assert pm.current_age(date_of_birth=None, stated_age=40, age_recorded_on=None,
                          created_at="2026-01-01T00:00:00+00:00", today=TODAY) == (40, True)
    assert pm.current_age(date_of_birth=None, stated_age=None, age_recorded_on=None,
                          created_at=None, today=TODAY) == (None, False)


# ── 3. Scores and bands ──────────────────────────────────────────────────────

def _probe(**kw):
    return pm.probe_from(today=TODAY, **kw)


def _other(name=None, phone=None, dob=None, sex=None, age=None):
    return {"name_key": pm.name_key(name), "phone_key": pm.phone_key(phone),
            "dob": pm.dob_key(dob), "sex": pm.sex_key(sex), "age": age}


def test_phone_and_exact_name_is_probable():
    score, reasons = pm.score_pair(_probe(name="Mohd Rafiq", phone="98450 12345"),
                                   _other("Mohammed Rafiq", "+919845012345"))
    assert score == 80 and pm.band_for(score) == "probable"
    assert reasons == ["phone", "name_exact"]


def test_name_and_dob_is_probable_name_and_close_age_is_possible():
    score, _ = pm.score_pair(_probe(name="Pooja Sharma", date_of_birth="1990-01-02"),
                             _other("Puja Sarma", dob="1990-01-02"))
    assert score == 70 and pm.band_for(score) == "probable"
    score, reasons = pm.score_pair(_probe(name="Pooja Sharma", age_years=35),
                                   _other("Puja Sarma", age=36))
    assert score == 50 and pm.band_for(score) == "possible"
    assert reasons == ["name_exact", "age_close"]


def test_an_age_three_years_off_scores_nothing_for_age():
    score, reasons = pm.score_pair(_probe(name="Pooja Sharma", age_years=35),
                                   _other("Puja Sarma", age=38))
    assert score == 40 and "age_close" not in reasons
    assert pm.band_for(score) is None


def test_a_sex_mismatch_costs_twenty_points():
    score, reasons = pm.score_pair(_probe(name="Lakshmi Rao", phone="9845012345", sex="F"),
                                   _other("Laxmi Rao", "9845012345", sex="M"))
    assert score == 60 and pm.band_for(score) == "possible"
    assert "sex_mismatch" in reasons


def test_phone_and_partial_name_is_possible():
    score, _ = pm.score_pair(_probe(name="Lakshmi Devi", phone="9845012345"),
                             _other("Lakshmi Rao", "9845012345"))
    assert score == 65 and pm.band_for(score) == "possible"


def test_bands():
    assert pm.band_for(100, ["uhid"]) == "hard"
    assert pm.band_for(0, ["abha"]) == "hard"
    assert pm.band_for(110) == "probable"   # phone + name + DOB is still not hard
    assert pm.band_for(70) == "probable"
    assert pm.band_for(69) == "possible"
    assert pm.band_for(50) == "possible"
    assert pm.band_for(49) is None


def test_a_hidden_date_of_birth_is_never_compared_only_the_age_counts():
    # The caller may not read this record's date of birth. A right guess and a
    # wrong guess of the same age score alike: the strict +30, as dob_hidden.
    stored = _other("Pooja Sharma", dob="1974-03-15", age=52)
    right = pm.score_pair(_probe(name="Pooja Sharma", date_of_birth="1974-03-15"), stored,
                          dob_hidden=True)
    wrong = pm.score_pair(_probe(name="Pooja Sharma", date_of_birth="1974-03-16"), stored,
                          dob_hidden=True)
    assert right == wrong == (70, ["name_exact", "dob_hidden"])
    # Never lower than the comparison it replaces.
    assert pm.score_pair(_probe(name="Pooja Sharma", date_of_birth="1974-03-15"),
                         stored)[0] == right[0]
    # A different age is what the index card shows anyway: the age rule applies.
    near = pm.score_pair(_probe(name="Pooja Sharma", date_of_birth="1972-03-15"), stored,
                         dob_hidden=True)
    assert near == (50, ["name_exact", "age_close"])
    # A record with a stated age only can never share a date of birth.
    stated = _other("Pooja Sharma", age=52)
    assert pm.score_pair(_probe(name="Pooja Sharma", date_of_birth="1974-03-15"), stated,
                         dob_hidden=True) == (50, ["name_exact", "age_close"])
    # With no date of birth typed, hiding changes nothing.
    typed_age = _probe(name="Pooja Sharma", age_years=52)
    assert pm.score_pair(typed_age, stored, dob_hidden=True) == pm.score_pair(typed_age, stored)
    assert pm.reason_labels(["dob_hidden"]) == [
        "Same age, date of birth not shown outside your department"]


# ── 4. The self-healing key table, on the suite database ─────────────────────

def _insert_patient(**kw):
    pid = f"PMT-{uuid.uuid4().hex[:10].upper()}"
    row = {"name": "Matcher Fixture", "mrn": f"PMT{uuid.uuid4().hex[:10].upper()}",
           "sex": "F", "status": "registered", **kw}
    with db_session() as db:
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, date_of_birth, sex, phone, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (pid, row["name"], row["mrn"], row.get("age"), row.get("date_of_birth"), row["sex"],
             row.get("phone"), row["status"]))
    return pid, row


def _unique_phone():
    return "9" + str(random.randint(100000000, 999999999))


def _match(**probe):
    with db_session() as db:
        return pm.find_candidates_on(db, _probe(**probe), today=TODAY)


def test_sync_finds_a_row_no_registration_code_wrote():
    phone = _unique_phone()
    pid, _ = _insert_patient(name="Zubeida Qureshi", phone=phone, date_of_birth="1971-03-04")
    found = _match(name="Zubeida Qureshi", phone=phone)
    assert [c["patient_id"] for c in found["candidates"]] == [pid]
    assert found["candidates"][0]["band"] == "probable"
    assert found["keys_recomputed"] >= 1


def test_sync_follows_a_phone_an_importer_rewrote():
    old, new = _unique_phone(), _unique_phone()
    pid, _ = _insert_patient(name="Harpreet Gill", phone=old)
    assert _match(name="Harpreet Gill", phone=old)["candidates"][0]["patient_id"] == pid
    with db_session() as db:
        db.execute("UPDATE patients SET phone = ? WHERE id = ?", (new, pid))
    found = _match(name="Harpreet Gill", phone=new)
    assert found["candidates"][0]["patient_id"] == pid
    assert "phone" in found["candidates"][0]["reasons"]
    assert not any("phone" in c["reasons"] for c in _match(name="Nobody Atall", phone=old)["candidates"]
                   if c["patient_id"] == pid)


def test_a_patient_admitted_through_the_admission_service_is_found():
    from services.admission_service import admit_patient
    phone = _unique_phone()
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
                   "VALUES ('PMTDOC', 'Matcher Doctor', 'doctor', 'x', 'Cardiology')")
    out = admit_patient({
        "name": "Thangamma Krishnan", "age": 61, "sex": "F", "phone": phone,
        "date_of_birth": "1965-02-03", "department": "Cardiology", "ward": "CCU", "bed": "PMT1",
        "admission_source": "er", "admission_type": "emergency",
        "chief_complaint": "chest pain", "initial_diagnosis": "chest pain",
    }, {"id": "PMTDOC", "name": "Matcher Doctor", "role": "doctor"})
    pid = out.get("patient_id") or out.get("patient", {}).get("id")
    assert pid
    found = _match(name="Thangama Krishnan", phone=phone, date_of_birth="1965-02-03")
    assert found["candidates"][0]["patient_id"] == pid
    assert found["candidates"][0]["band"] == "probable"


def test_a_sync_with_nothing_to_do_recomputes_nothing():
    _insert_patient(name="Settled Row", phone=_unique_phone(), date_of_birth="1960-06-06")
    with db_session() as db:
        pm.sync_keys(db)
    with db_session() as db:
        assert pm.sync_keys(db)["recomputed"] == 0


def test_an_older_normaliser_version_is_recomputed():
    pid, _ = _insert_patient(name="Versioned Row", phone=_unique_phone())
    with db_session() as db:
        pm.sync_keys(db)
        db.execute("UPDATE patient_match_keys SET normalizer_version = 0, name_key = 'stale' "
                   "WHERE patient_id = ?", (pid,))
    with db_session() as db:
        assert pm.sync_keys(db)["recomputed"] >= 1
        key = db.execute("SELECT name_key, normalizer_version FROM patient_match_keys "
                         "WHERE patient_id = ?", (pid,)).fetchone()
    assert key[0] == pm.name_key("Versioned Row") and key[1] == pm.NORMALIZER_VERSION


def test_orphan_keys_are_removed_only_by_the_sweep_form():
    ghost = f"PMT-GHOST-{uuid.uuid4().hex[:6]}"
    with db_session() as db:
        db.execute("INSERT INTO patient_match_keys (patient_id, name_key, normalizer_version, "
                   "computed_at) VALUES (?, 'ghost', ?, '2026-10-04')", (ghost, pm.NORMALIZER_VERSION))
    with db_session() as db:
        pm.sync_keys(db)
        assert db.execute("SELECT 1 FROM patient_match_keys WHERE patient_id = ?", (ghost,)).fetchone()
        assert pm.sync_keys(db, remove_orphans=True)["removed"] >= 1
        assert db.execute("SELECT 1 FROM patient_match_keys WHERE patient_id = ?", (ghost,)).fetchone() is None


# ── 5. Hard matches ──────────────────────────────────────────────────────────

def test_the_same_uhid_in_any_case_or_spacing_is_a_hard_match():
    mrn = f"HMT-{uuid.uuid4().hex[:6].upper()}"
    pid, _ = _insert_patient(name="Hardmatch Uhid", mrn=mrn)
    for typed in (mrn, mrn.lower(), mrn.replace("-", " "), mrn.replace("-", "")):
        found = _match(name="Someone Else", uhid=typed)
        hard = [c for c in found["candidates"] if c["band"] == "hard"]
        assert [c["patient_id"] for c in hard] == [pid], typed
        assert "uhid" in hard[0]["reasons"] and hard[0]["score"] == pm.HARD_SCORE


def test_an_active_abha_is_a_hard_match_a_withdrawn_one_is_not():
    number = str(random.randint(10**13, 10**14 - 1))
    pid, _ = _insert_patient(name="Hardmatch Abha")
    with db_session() as db:
        db.execute("INSERT INTO abha_identities (patient_id, abha_number, status, source) "
                   "VALUES (?, ?, 'active', 'counter_keyed')", (pid, number))
    hard = [c for c in _match(abha_number=number)["candidates"] if c["band"] == "hard"]
    assert [c["patient_id"] for c in hard] == [pid] and hard[0]["reasons"][0] == "abha"
    with db_session() as db:
        db.execute("UPDATE abha_identities SET status = 'withdrawn' WHERE patient_id = ?", (pid,))
    assert not [c for c in _match(abha_number=number)["candidates"] if c["band"] == "hard"]


def test_the_excluded_patient_is_never_its_own_candidate():
    phone = _unique_phone()
    pid, _ = _insert_patient(name="Self Exclusion", phone=phone)
    with db_session() as db:
        found = pm.find_candidates_on(db, _probe(name="Self Exclusion", phone=phone),
                                      exclude_patient_id=pid, today=TODAY)
    assert pid not in [c["patient_id"] for c in found["candidates"]]


# ── 5b. A date of birth the caller may not read ──────────────────────────────

def _letters(n=6):
    return "Q" + "".join(random.choice("bdfgklmnprstv") + random.choice("aeiou")
                         for _ in range(n // 2))


def _match_scoped(visible, **probe):
    with db_session() as db:
        return pm.find_candidates_on(db, _probe(**probe), today=TODAY, dob_visible=visible)


def _entry(found, pid):
    return [c for c in found["candidates"] if c["patient_id"] == pid]


def test_a_hidden_date_of_birth_answers_every_guess_of_the_same_age_alike():
    mrn = f"HDN-{uuid.uuid4().hex[:6].upper()}"
    phone = _unique_phone()
    name = f"{_letters()} {_letters()}"
    pid, _ = _insert_patient(name=name, mrn=mrn, phone=phone, date_of_birth="1974-03-15")
    def hidden(patient_id):
        return patient_id != pid  # every record readable but this one

    guesses = ("1974-03-14", "1974-03-15", "1974-03-16")
    for probe in ({"uhid": mrn}, {"name": name}, {"phone": phone}, {"uhid": mrn, "name": name}):
        answers = [_entry(_match_scoped(hidden, **probe, date_of_birth=g), pid) for g in guesses]
        assert answers[0] == answers[1] == answers[2], probe
        assert answers[0], probe
        assert not any("dob_exact" in c["reasons"] for c in answers[0]), probe
    hard = _entry(_match_scoped(hidden, uhid=mrn, date_of_birth="1974-03-15"), pid)[0]
    assert hard["band"] == "hard" and hard["reasons"] == ["uhid"]
    by_name = _entry(_match_scoped(hidden, name=name, date_of_birth="1974-03-16"), pid)[0]
    assert by_name["score"] == 70 and by_name["band"] == "probable"
    assert by_name["reasons"] == ["name_exact", "dob_hidden"]
    # A caller who may read it (or no predicate at all) still compares it.
    for visible in (None, lambda patient_id: True):
        right = _entry(_match_scoped(visible, uhid=mrn, date_of_birth="1974-03-15"), pid)[0]
        wrong = _entry(_match_scoped(visible, uhid=mrn, date_of_birth="1974-03-16"), pid)
        assert "dob_exact" in right["reasons"]
        assert not wrong or "dob_exact" not in wrong[0]["reasons"]


def test_a_record_only_the_hidden_date_of_birth_finds_is_left_out():
    shared = _letters()
    pid, _ = _insert_patient(name=f"{shared} {_letters()}", date_of_birth="1969-07-21")
    typed = {"name": f"{shared} {_letters()}", "date_of_birth": "1969-07-21"}
    seen = _entry(_match_scoped(None, **typed), pid)
    assert seen and seen[0]["band"] == "possible"
    assert seen[0]["reasons"] == ["name_overlap", "dob_exact"]
    assert _entry(_match_scoped(lambda patient_id: False, **typed), pid) == []
    assert _entry(_match_scoped(lambda patient_id: False, **{**typed, "date_of_birth": "1969-07-22"}),
                  pid) == []


def test_the_scope_question_is_asked_only_when_a_date_of_birth_was_typed():
    phone = _unique_phone()
    name = f"{_letters()} {_letters()}"
    pid, _ = _insert_patient(name=name, phone=phone, date_of_birth="1980-01-20")
    asked: list = []

    def visible(patient_id):
        asked.append(patient_id)
        return False

    plain = _match_scoped(visible, name=name, phone=phone, age_years=46)
    assert asked == []
    assert _entry(plain, pid) == _entry(_match_scoped(None, name=name, phone=phone, age_years=46), pid)
    _match_scoped(visible, name=name, phone=phone, date_of_birth="1980-01-20")
    assert asked.count(pid) == 1


# ── 5c. A cut scan ranks before it cuts and says what it may have missed ─────

def _fillers(n, *, phone=None, date_of_birth=None, sex="F", age=30, surname=None):
    """n older records sharing one crowded detail. Their name words start with
    Q (folded to k), so they never share a word with a test probe's name
    unless `surname` is given."""
    ids = []
    with db_session() as db:
        for _ in range(n):
            pid = f"PMC-{uuid.uuid4().hex[:10].upper()}"
            name = f"{_letters()} {surname or _letters()}"
            db.execute(
                "INSERT INTO patients (id, name, mrn, age, date_of_birth, sex, phone, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'registered', CURRENT_TIMESTAMP)",
                (pid, name, f"PMC{uuid.uuid4().hex[:10].upper()}",
                 None if date_of_birth else age, date_of_birth, sex, phone))
            ids.append(pid)
        pm.sync_keys(db)
    return ids


def _rare_dob():
    return f"{random.randint(1905, 1914)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def test_a_cut_scan_keeps_the_true_duplicate_behind_a_crowded_phone(monkeypatch):
    monkeypatch.setattr(pm, "MAX_CANDIDATE_ROWS", 10)
    helpline = _unique_phone()
    _fillers(30, phone=helpline)
    # Registered AFTER the crowd, so an unranked scan in table order cuts it.
    dup, _ = _insert_patient(name="Vandana Velankar", phone=helpline,
                             date_of_birth="1974-05-06", sex="F")
    found = _match(name="Vandana Velankar", phone=helpline, date_of_birth="1974-05-06", sex="F")
    entry = _entry(found, dup)
    assert entry and entry[0]["band"] == "probable"
    assert entry[0]["reasons"] == ["phone", "name_exact", "dob_exact"]
    # The crowd left out shares only the phone: 40, plus at most 10 for an age.
    assert found["truncated"] is True
    assert found["unseen_bound"] == pm.POINTS_PHONE + pm.POINTS_AGE_CLOSE
    assert found["unseen_band"] == "possible"
    # A scoped caller may not read some dates of birth, and a hidden one at
    # the same age scores +30 (dob_hidden), so the bound is higher for them.
    scoped = _match_scoped(lambda patient_id: True, name="Vandana Velankar", phone=helpline,
                           date_of_birth="1974-05-06")
    assert _entry(scoped, dup)
    assert scoped["unseen_bound"] == pm.POINTS_PHONE + pm.POINTS_DOB_EXACT
    assert scoped["unseen_band"] == "probable"


def test_a_cut_scan_keeps_the_true_duplicate_behind_a_crowded_date_of_birth(monkeypatch):
    monkeypatch.setattr(pm, "MAX_CANDIDATE_ROWS", 10)
    crowded = _rare_dob()
    _fillers(30, date_of_birth=crowded, sex="M")
    phone = _unique_phone()
    dup, _ = _insert_patient(name="Vikram Vadhera", phone=phone, date_of_birth=crowded, sex="M")
    found = _match(name="Vikram Vadhera", phone=phone, date_of_birth=crowded, sex="M")
    entry = _entry(found, dup)
    assert entry and entry[0]["score"] == 110 and entry[0]["band"] == "probable"
    # Every record left out shares only the date of birth: 30 at most, below
    # every band, so a full scan would have listed exactly the same.
    assert found["truncated"] is True
    assert found["unseen_bound"] == pm.POINTS_DOB_EXACT
    assert found["unseen_band"] is None


def test_a_record_left_out_sharing_a_name_word_is_bounded_as_probable(monkeypatch):
    monkeypatch.setattr(pm, "MAX_CANDIDATE_ROWS", 10)
    helpline = _unique_phone()
    surname = _letters()
    _fillers(30, phone=helpline, surname=surname, sex="M")
    found = _match(name=f"Vimla {surname}", phone=helpline, age_years=52, sex="F")
    # Phone 40 + a shared word 25 + an age 10: probable, though the sex
    # differs on every one of them (the bound leaves the sex out).
    assert found["truncated"] is True
    assert found["unseen_bound"] == 75 and found["unseen_band"] == "probable"


def test_a_cut_answers_every_guess_at_a_hidden_date_of_birth_alike(monkeypatch):
    monkeypatch.setattr(pm, "MAX_CANDIDATE_ROWS", 10)
    helpline = _unique_phone()
    _fillers(10, phone=helpline)                     # exactly the cap
    word = _letters()
    right, wrong = _rare_dob(), _rare_dob()
    while wrong == right:
        wrong = _rare_dob()
    _insert_patient(name=f"{_letters()} {word}", date_of_birth=right)    # shares a word
    _insert_patient(name=f"{_letters()} {_letters()}", date_of_birth=wrong)

    def nobody(patient_id):
        return False

    answers = [_match_scoped(nobody, name=f"Vimal {word}", phone=helpline, date_of_birth=guess)
               for guess in (right, wrong)]
    # Each guess fetches one more row than the cap, so both are cut, and the
    # row left out is the hidden one the date fetched: a right guess must not
    # report a different bound because that record shares a name word.
    for found in answers:
        assert found["truncated"] is True
    assert [(f["unseen_bound"], f["unseen_band"]) for f in answers] == [
        (pm.POINTS_DOB_EXACT + pm.POINTS_NAME_OVERLAP, "possible")] * 2
    assert answers[0]["candidates"] == answers[1]["candidates"]


def test_a_scan_that_is_not_cut_reports_nothing_unseen():
    phone = _unique_phone()
    _insert_patient(name="Varsha Vohra", phone=phone)
    found = _match(name="Varsha Vohra", phone=phone)
    assert found["truncated"] is False
    assert found["unseen_bound"] is None and found["unseen_band"] is None


def test_the_sql_bound_never_scores_a_row_below_score_pair():
    """The cut is only safe if _bound_sql never undercounts: checked against
    score_pair for every row and random probes, with and without a scope
    predicate, on a scratch database; and a cut scan never misses a
    candidate a full scan finds above the bound it reports."""
    import sqlite3
    from migrations_his import m0050_registration_opd_foundation as m50

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE patients (id TEXT PRIMARY KEY, name TEXT, mrn TEXT, age INTEGER, "
                 "date_of_birth TEXT, sex TEXT, phone TEXT, created_at TEXT)")
    conn.execute(m50.PATIENT_DEMOGRAPHICS_DDL)
    conn.execute(m50.PATIENT_MATCH_KEYS_DDL)
    conn.execute("CREATE TABLE abha_identities (id INTEGER PRIMARY KEY, patient_id TEXT, "
                 "abha_number TEXT, abha_address TEXT, status TEXT)")
    words = ["Lakshmi", "Laxmi", "Rao", "Devi", "Kumar", "Baby of", "Pooja", "Puja", "Sharma",
             "Ravi", "B/O", "Mohd", "Rafiq", "Iyer", "S."]
    phones = ["9845012345", "9845012346", "+91 98450 12345", None]
    dobs = ["1980-01-01", "1980-01-02", "1981-06-01", "1979-12-31", None]
    rng = random.Random(7)
    rows = []
    for i in range(400):
        dob = rng.choice(dobs)
        rows.append((f"P{i:05d}", " ".join(rng.choice(words) for _ in range(rng.randint(1, 3))),
                     f"M{i}", None if dob else rng.randint(40, 50), dob, rng.choice("MF"),
                     rng.choice(phones), "2026-01-01T00:00:00+00:00"))
    conn.executemany("INSERT INTO patients VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    pm.sync_keys(conn)

    def half_visible(patient_id):
        return int(patient_id[1:]) % 2 == 0

    for _ in range(40):
        dob = rng.choice(dobs)
        probe = pm.probe_from(name=" ".join(rng.choice(words) for _ in range(rng.randint(1, 3))),
                              phone=rng.choice(phones), date_of_birth=dob,
                              age_years=None if dob else rng.choice([None, 44, 47]),
                              sex=rng.choice("MF"), today=TODAY)
        for visible in (None, half_visible):
            maybe_hidden = visible is not None and bool(probe.get("dob"))
            bound, params = pm._bound_sql(probe, dob_may_be_hidden=maybe_hidden)
            for r in conn.execute(
                    f"SELECT k.*, p.age AS stated_age, p.date_of_birth, p.created_at, "
                    f"({bound}) AS b FROM patient_match_keys k JOIN patients p "
                    "ON p.id = k.patient_id", params):
                age, stated = pm.current_age(date_of_birth=r["date_of_birth"],
                                             stated_age=r["stated_age"], age_recorded_on=None,
                                             created_at=r["created_at"], today=TODAY)
                if maybe_hidden and not visible(r["patient_id"]):
                    other = {"name_key": r["name_key"], "phone_key": r["phone_key"],
                             "dob": (age is not None and not stated) or None,
                             "sex": r["sex"], "age": age}
                    score, _ = pm.score_pair(probe, other, dob_hidden=True)
                else:
                    other = {"name_key": r["name_key"], "phone_key": r["phone_key"],
                             "dob": r["dob"], "sex": r["sex"], "age": age}
                    score, _ = pm.score_pair(probe, other)
                assert score <= r["b"], (dict(probe), dict(r), score)
            full = pm.find_candidates_on(conn, probe, today=TODAY, dob_visible=visible)
            assert full["truncated"] is False
            original = pm.MAX_CANDIDATE_ROWS
            try:
                pm.MAX_CANDIDATE_ROWS = rng.choice([5, 20, 60])
                cut = pm.find_candidates_on(conn, probe, today=TODAY, dob_visible=visible)
            finally:
                pm.MAX_CANDIDATE_ROWS = original
            kept = {c["patient_id"] for c in cut["candidates"]}
            missed = [c["score"] for c in full["candidates"] if c["patient_id"] not in kept]
            if missed:
                assert cut["truncated"] and max(missed) <= cut["unseen_bound"]
                assert cut["unseen_band"] is not None
    conn.close()


# ── 6. Scale, in SQLCipher ───────────────────────────────────────────────────

#: Stated budgets at 100,000 patients on an encrypted database, warm cache.
#: Measured on the development Mac (M-series, 4 October 2026): the first full
#: sync about 1.8 s (once, the first match after a site switches registration
#: on), a sync with nothing to do about 40 ms, sync plus a match about 105 ms
#: (190 ms on a cold connection), and the minimal-field name search scan of
#: every row (a name nobody has) about 45 ms. The budgets leave roughly five
#: times headroom for a busier machine.
SCALE_PATIENTS = int(os.environ.get("ORB_MATCH_SCALE_PATIENTS", "100000"))
BUDGET_FIRST_SYNC_S = 15.0
BUDGET_NOOP_SYNC_MS = 250
BUDGET_SYNC_AND_MATCH_MS = 500
BUDGET_NAME_SCAN_MS = 500


@pytest.fixture(scope="module")
def encrypted_index():
    sqlcipher3 = pytest.importorskip("sqlcipher3")
    from migrations_his import m0050_registration_opd_foundation as m50

    folder = tempfile.mkdtemp(prefix="orb_match_scale_")
    path = os.path.join(folder, "scale.db")
    key = "x'" + ("5a" * 32) + "'"

    def connect():
        conn = sqlcipher3.connect(path)
        conn.execute(f'PRAGMA key = "{key}"')
        conn.row_factory = sqlcipher3.Row
        conn.execute("PRAGMA cache_size = -256000")
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    conn = connect()
    # The same order as database._build_connection: WAL first, so the file is
    # created at SQLCipher's own page size (a page_size set before the first
    # write would make the file unreadable to the next connection).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE patients (id TEXT PRIMARY KEY, user_id TEXT, name TEXT NOT NULL, "
        "mrn TEXT UNIQUE NOT NULL, bed TEXT, ward TEXT, admission_date TIMESTAMP, "
        "diagnosis TEXT, status TEXT, age INTEGER, date_of_birth DATE, sex TEXT, phone TEXT, "
        "emergency_contact_name TEXT, emergency_contact_phone TEXT, address TEXT, "
        "attending_doctor_id TEXT, created_at TIMESTAMP)")
    conn.execute(m50.PATIENT_DEMOGRAPHICS_DDL)
    conn.execute(m50.PATIENT_MATCH_KEYS_DDL)
    for statement in m50.NEW_TABLE_INDEXES:
        if "patient_match_keys" in statement:
            conn.execute(statement)
    conn.execute("CREATE TABLE abha_identities (id INTEGER PRIMARY KEY, patient_id TEXT, "
                 "abha_number TEXT, abha_address TEXT, status TEXT)")
    first = ["Ramesh", "Suresh", "Lakshmi", "Pooja", "Mohammed", "Anita", "Sunita", "Rajesh",
             "Priya", "Kavita", "Arjun", "Deepak", "Geetha", "Meena", "Ravi", "Sanjay"]
    last = ["Kumar", "Sharma", "Rao", "Iyer", "Khan", "Patel", "Singh", "Reddy", "Nair", "Das"]
    rng = random.Random(11)
    rows = []
    for i in range(SCALE_PATIENTS):
        dob = (f"{rng.randint(1930, 2024)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
               if i % 3 else None)
        rows.append((f"PAT-{i:08d}", f"{rng.choice(first)} {rng.choice(last)} {rng.choice(first)[:3]}{i % 97}",
                     f"SC{i:07d}", rng.randint(1, 90), dob, rng.choice("MF"),
                     f"9{rng.randint(100000000, 999999999)}", "2026-01-01T00:00:00+00:00"))
    conn.executemany("INSERT INTO patients (id, name, mrn, age, date_of_birth, sex, phone, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.execute("UPDATE patients SET name = 'Lakshmi Rao', phone = '9845012345', "
                 "date_of_birth = '1980-05-05', sex = 'F' WHERE id = 'PAT-00000500'")
    conn.commit()
    conn.close()
    yield connect
    import shutil
    shutil.rmtree(folder, ignore_errors=True)


def _ms(start):
    return (time.perf_counter() - start) * 1000


def test_scale_the_database_really_is_encrypted(encrypted_index):
    import sqlite3
    from db_backend import DBDatabaseError
    path = encrypted_index().execute("PRAGMA database_list").fetchone()[2]
    plain = sqlite3.connect(path)
    # The driver-neutral alias (tests/test_db_exception_aliases.py), pinned to
    # the stdlib's own refusal of an encrypted file.
    with pytest.raises(DBDatabaseError, match="file is not a database"):
        plain.execute("SELECT COUNT(*) FROM patients").fetchone()
    plain.close()


def test_scale_sync_and_match_on_100k_encrypted_patients_stay_in_budget(encrypted_index):
    conn = encrypted_index()
    start = time.perf_counter()
    first = pm.sync_keys(conn)
    conn.commit()
    first_s = (time.perf_counter() - start)
    assert first["recomputed"] == SCALE_PATIENTS
    assert first_s < BUDGET_FIRST_SYNC_S, f"first full sync took {first_s:.2f}s"

    noop_ms = []
    for _ in range(3):
        start = time.perf_counter()
        assert pm.sync_keys(conn)["recomputed"] == 0
        conn.commit()
        noop_ms.append(_ms(start))
    assert min(noop_ms) < BUDGET_NOOP_SYNC_MS, f"no-op sync took {noop_ms} ms"

    # An importer rewrites one phone: the next match finds the new number.
    conn.execute("UPDATE patients SET phone = '9845099999' WHERE id = 'PAT-00000700'")
    conn.commit()
    probe = pm.probe_from(name="Laxmi Rao", phone="+91 98450 12345", date_of_birth="1980-05-05",
                          sex="F", uhid="sc-0000123", today=TODAY)
    match_ms = []
    for _ in range(3):
        start = time.perf_counter()
        found = pm.find_candidates_on(conn, probe, today=TODAY)
        conn.commit()
        match_ms.append(_ms(start))
    ids = {c["patient_id"]: c for c in found["candidates"]}
    assert ids["PAT-00000500"]["band"] == "probable"
    assert ids["PAT-00000500"]["reasons"] == ["phone", "name_exact", "dob_exact"]
    assert ids["PAT-00000123"]["band"] == "hard"
    assert min(match_ms) < BUDGET_SYNC_AND_MATCH_MS, f"sync plus match took {match_ms} ms"
    # The rewritten phone was healed by the first match above, so this match
    # recomputes nothing and still finds the row by its new number.
    with_new = pm.find_candidates_on(
        conn, pm.probe_from(phone="9845099999", age_years=None, today=TODAY), today=TODAY)
    assert with_new["keys_recomputed"] == 0
    with_new_keys = conn.execute("SELECT phone_key FROM patient_match_keys WHERE patient_id = "
                                 "'PAT-00000700'").fetchone()[0]
    assert with_new_keys == "9845099999"

    name_scan = ("SELECT k.patient_id FROM patient_match_keys k JOIN patients p ON p.id = k.patient_id "
                 "WHERE ((' ' || k.name_key || ' ') LIKE ? ESCAPE '\\') OR p.name LIKE ? ESCAPE '\\' "
                 "LIMIT 21")
    assert conn.execute(name_scan, ("% laxmi%", "%laxmi%")).fetchall()
    # Timed on a name nobody has: the worst case, a scan of every row.
    start = time.perf_counter()
    rows = conn.execute(name_scan, ("% zqxvw%", "%zqxvw%")).fetchall()
    scan_ms = _ms(start)
    assert rows == []
    assert scan_ms < BUDGET_NAME_SCAN_MS, f"name search scan took {scan_ms:.0f} ms"
    print(f"\n[scale {SCALE_PATIENTS}] first sync {first_s:.2f}s; no-op sync {min(noop_ms):.0f} ms; "
          f"sync+match {min(match_ms):.0f} ms; name scan {scan_ms:.0f} ms")
    conn.close()
