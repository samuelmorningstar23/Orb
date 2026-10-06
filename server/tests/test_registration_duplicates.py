"""Duplicate and returning-patient detection at registration (opd-5, W4-A,
4 October 2026).

Ruling OPD-S2 is built in both modes and enforced by the SERVER, not the
screen. config.DUPLICATE_MODE ships as 'block' (the strictest option named,
pending Samuel's confirmation); 'warn' is the architect's recommendation.

What this file pins:

  1. Hard matches (the same UHID, an active ABHA) are refused in BOTH modes,
     whatever review the desk sends.
  2. block: a probable duplicate is refused; a possible one needs a review.
  3. warn: a probable or possible duplicate needs a duplicate_review naming
     every candidate the server found, with a reason; without the reason it is
     refused; a stale candidate list is refused with the fresh one; a reviewed
     override is audited and opens one worklist row per candidate.
  4. Name variants are caught over HTTP (POST /match): Mohd and Mohammed,
     Laxmi and Lakshmi, Pooja and Puja, honorifics, Baby of; a patient the
     admission service wrote is found.
  5. The worklist: minimal fields on both sides, audited; resolution audited;
     the person who overrode may not resolve; a resolved item stays resolved.
  6. The administrator's bounded sweep.
  7. A crowded detail never hides a duplicate: with more than
     MAX_CANDIDATE_ROWS records sharing a helpline number or a date of birth,
     the true duplicate is still found, and a check that could not compare
     every record that might score in a band fails closed (a review in warn,
     a refusal in block when a record left out could be probable), audited.
"""
from __future__ import annotations

import json
import random
import string
import uuid

import pytest
from fastapi.testclient import TestClient

import config
from auth import create_token
from database import db_session
from main import app

client = TestClient(app)


def _mk_user(role, department=None):
    uid = f"RGD{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, ?, ?, 'x', ?, 1)", (uid, f"Regdup {role} {uid[-4:]}", role, department))
    return uid, role


def _hdr(user):
    return {"Authorization": f"Bearer {create_token(user[0], user[1])}"}


@pytest.fixture(scope="module")
def desk():
    return _mk_user("receptionist")


@pytest.fixture(scope="module")
def desk2():
    return _mk_user("receptionist")


@pytest.fixture(scope="module")
def admin():
    return _mk_user("admin")


@pytest.fixture
def warn_mode(monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_ENABLED", True)
    monkeypatch.setattr(config, "UHID_SOURCE", "hospital")
    monkeypatch.setattr(config, "UHID_PATTERN", None)
    monkeypatch.setattr(config, "DUPLICATE_MODE", "warn")
    monkeypatch.setattr(config, "ABDM_ENABLED", True)


@pytest.fixture
def block_mode(warn_mode, monkeypatch):
    monkeypatch.setattr(config, "DUPLICATE_MODE", "block")


def test_the_shipped_default_is_the_strictest_mode():
    # OPD-S2 is a safety ruling for Samuel to confirm: the strict option ships
    # until he does (docs/HIS_BUILD_PLAN_2026-10.md, "Samuel's answers").
    import os
    if (os.environ.get("ORB_DUPLICATE_MODE") or "").strip():
        pytest.skip("ORB_DUPLICATE_MODE is set in this environment")
    assert config.DUPLICATE_MODE == "block"


def _word(first="Y"):
    return first + "".join(random.choice("bdfgklmnprstv") + random.choice("aeiou") for _ in range(3))


def _phone():
    return "7" + "".join(random.choice(string.digits) for _ in range(9))


def _dob():
    return f"{random.randint(1935, 2005)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _body(**kw):
    body = {"name": f"{_word()} {_word()}", "sex": "F", "date_of_birth": _dob(), "phone": _phone(),
            "uhid": f"RGD{uuid.uuid4().hex[:8].upper()}"}
    body.update(kw)
    return body


def _post(user, body, path="/api/registration/patients"):
    return client.post(path, json=body, headers=_hdr(user))


def _existing(desk, **kw):
    body = _body(**kw)
    resp = _post(desk, body)
    assert resp.status_code == 201, resp.text
    return resp.json()["patient"], body


def _audit_rows(action, patient_id=None):
    with db_session() as db:
        if patient_id:
            rows = db.execute("SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? "
                              "ORDER BY id", (action, patient_id)).fetchall()
        else:
            rows = db.execute("SELECT * FROM audit_logs WHERE action_type = ? ORDER BY id",
                              (action,)).fetchall()
    return [dict(r) for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
# 1. Hard matches, both modes
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("mode", ["warn", "block"])
def test_the_same_uhid_is_a_hard_refusal_in_both_modes(desk, warn_mode, monkeypatch, mode):
    monkeypatch.setattr(config, "DUPLICATE_MODE", mode)
    first, _ = _existing(desk)
    body = _body(uhid=first["uhid"].lower(), duplicate_review={
        "decision": "register_new", "candidate_ids": [first["patient_id"]],
        "reason": "Different person, the desk is sure"})
    resp = _post(desk, body)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "existing_record"
    assert [c["patient_id"] for c in detail["candidates"]] == [first["patient_id"]]
    assert detail["candidates"][0]["band"] == "hard"
    rows = _audit_rows("registration_refused")
    assert json.loads(rows[-1]["metadata_json"])["matched_patient_ids"] == [first["patient_id"]]
    assert rows[-1]["event_category"] == "patient_identity"


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_an_active_abha_is_a_hard_refusal_in_both_modes(desk, warn_mode, monkeypatch, mode):
    monkeypatch.setattr(config, "DUPLICATE_MODE", mode)
    number = "".join(random.choice(string.digits) for _ in range(14))
    first, _ = _existing(desk, abha_number=number)
    resp = _post(desk, _body(abha_number=number, duplicate_review={
        "decision": "register_new", "candidate_ids": [first["patient_id"]],
        "reason": "Different person, the desk is sure"}))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "existing_record" and "ABHA" in detail["message"]
    assert detail["candidates"][0]["reasons"][0] == "abha"


# ═════════════════════════════════════════════════════════════════════════════
# 2. block
# ═════════════════════════════════════════════════════════════════════════════

def test_block_refuses_a_probable_duplicate_even_with_a_review(desk, block_mode):
    first, body = _existing(desk)
    again = _body(name=body["name"], phone=body["phone"], date_of_birth=_dob(),
                  duplicate_review={"decision": "register_new",
                                    "candidate_ids": [first["patient_id"]],
                                    "reason": "Different person, the desk is sure"})
    resp = _post(desk, again)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "probable_duplicate"
    assert resp.json()["detail"]["mode"] == "block"


def test_block_still_lets_a_reviewed_possible_duplicate_through(desk, block_mode):
    first, body = _existing(desk, date_of_birth="1950-01-01")
    # Same phone, a different first name sharing the surname: 40 + 25 = 65.
    # Fifty years apart, so no age points can lift it into the probable band.
    surname = body["name"].split()[1]
    probe = _body(name=f"{_word('W')} {surname}", phone=body["phone"], date_of_birth="2000-01-01")
    refused = _post(desk, probe)
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "duplicate_review_required"
    assert refused.json()["detail"]["candidates"][0]["band"] == "possible"
    probe["duplicate_review"] = {"decision": "register_new", "candidate_ids": [first["patient_id"]],
                                 "reason": "Sister, same family phone"}
    assert _post(desk, probe).status_code == 201


# ═════════════════════════════════════════════════════════════════════════════
# 3. warn
# ═════════════════════════════════════════════════════════════════════════════

def _twin(first, body, **kw):
    """A new patient that scores probable against `body` (name + phone)."""
    return _body(name=body["name"], phone=body["phone"], date_of_birth=_dob(), **kw)


def test_warn_needs_a_review_and_shows_minimal_fields(desk, warn_mode):
    first, body = _existing(desk)
    resp = _post(desk, _twin(first, body))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "duplicate_review_required" and detail["mode"] == "warn"
    cand = detail["candidates"][0]
    assert cand["patient_id"] == first["patient_id"] and cand["band"] == "probable"
    assert cand["reason_labels"][:2] == ["Same phone number", "Same name"]
    assert body["date_of_birth"] not in resp.text and body["phone"] not in resp.text


def test_an_override_without_a_reason_is_refused(desk, warn_mode):
    first, body = _existing(desk)
    for reason in (None, "", "ok"):
        resp = _post(desk, _twin(first, body, duplicate_review={
            "decision": "register_new", "candidate_ids": [first["patient_id"]], "reason": reason}))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "override_reason_required"


def test_a_review_that_misses_a_candidate_is_refused_with_the_fresh_list(desk, warn_mode):
    first, body = _existing(desk)
    resp = _post(desk, _twin(first, body, duplicate_review={
        "decision": "register_new", "candidate_ids": ["PAT-SOMEONE-ELSE"],
        "reason": "Different person, checked the card"}))
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "duplicate_review_stale"
    assert first["patient_id"] in [c["patient_id"] for c in resp.json()["detail"]["candidates"]]


def test_a_reviewed_override_is_audited_and_opens_a_worklist_row(desk, warn_mode):
    first, body = _existing(desk)
    resp = _post(desk, _twin(first, body, duplicate_review={
        "decision": "register_new", "candidate_ids": [first["patient_id"]],
        "reason": "Father and son, same name and phone"}))
    assert resp.status_code == 201, resp.text
    new_id = resp.json()["patient"]["patient_id"]
    assert resp.json()["duplicate_review"]["candidate_ids"] == [first["patient_id"]]
    pair = tuple(sorted((new_id, first["patient_id"])))
    with db_session() as db:
        row = db.execute("SELECT * FROM patient_duplicate_candidates WHERE patient_id = ? "
                         "AND candidate_patient_id = ?", pair).fetchone()
    assert row["status"] == "open" and row["source"] == "registration"
    assert row["overridden_by"] == desk[0]
    assert row["override_reason"] == "Father and son, same name and phone"
    rows = _audit_rows("registration_duplicate_override", new_id)
    assert rows and rows[-1]["event_category"] == "patient_identity"
    assert json.loads(rows[-1]["metadata_json"])["candidate_ids"] == [first["patient_id"]]


def test_a_review_for_nothing_found_is_not_needed(desk, warn_mode):
    resp = _post(desk, _body())
    assert resp.status_code == 201
    assert resp.json()["duplicate_review"] is None


# ═════════════════════════════════════════════════════════════════════════════
# 4. Name variants over /match, and other doors
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("stored, typed", [
    ("Mohd Rafiq Ansari", "Mohammed Rafiq Ansari"),
    ("Laxmi Venkatesh", "Lakshmi Venkatesh"),
    ("Pooja Hegde", "Puja Hegde"),
    ("Smt. Kamala Bhat", "Kamala Bhat"),
    ("Baby of Shanthi", "B/O Shanti"),
])
def test_name_variants_are_found_by_match(desk, warn_mode, stored, typed):
    phone = _phone()
    first, _ = _existing(desk, name=stored, phone=phone)
    resp = client.post("/api/registration/match", json={"name": typed, "phone": "+91 " + phone},
                       headers=_hdr(desk))
    assert resp.status_code == 200, resp.text
    hit = [c for c in resp.json()["candidates"] if c["patient_id"] == first["patient_id"]]
    assert hit and hit[0]["band"] == "probable" and "name_exact" in hit[0]["reasons"]
    rows = _audit_rows("registration_match_checked")
    assert first["patient_id"] in json.loads(rows[-1]["metadata_json"])["matched_patient_ids"]


def test_baby_of_a_mother_is_not_the_mother(desk, warn_mode):
    phone = _phone()
    mother, _ = _existing(desk, name="Shanthi Gowda", phone=phone, date_of_birth="1994-03-03")
    resp = client.post("/api/registration/match", json={
        "name": "Baby of Shanthi Gowda", "phone": phone, "date_of_birth": "2026-10-01"},
        headers=_hdr(desk))
    assert all(c["patient_id"] != mother["patient_id"] or c["band"] != "probable"
               for c in resp.json()["candidates"])


def test_a_sex_mismatch_lowers_a_probable_match(desk, warn_mode):
    phone = _phone()
    first, body = _existing(desk, name="Kiran Shetty", phone=phone, sex="F")
    resp = client.post("/api/registration/match", json={"name": "Kiran Shetty", "phone": phone,
                                                         "sex": "M"}, headers=_hdr(desk))
    hit = [c for c in resp.json()["candidates"] if c["patient_id"] == first["patient_id"]][0]
    assert hit["score"] == 60 and hit["band"] == "possible" and "sex_mismatch" in hit["reasons"]


def test_a_patient_admitted_at_the_admit_door_is_found(desk, warn_mode):
    from services.admission_service import admit_patient
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash, department) "
                   "VALUES ('RGDDOC', 'Regdup Doctor', 'doctor', 'x', 'Cardiology')")
    phone = _phone()
    out = admit_patient({"name": "Basavaraj Hiremath", "age": 70, "sex": "M", "phone": phone,
                         "department": "Cardiology", "ward": "CCU", "bed": "RGD1",
                         "admission_source": "er", "admission_type": "emergency",
                         "chief_complaint": "breathless", "initial_diagnosis": "breathless"},
                        {"id": "RGDDOC", "name": "Regdup Doctor", "role": "doctor"})
    resp = _post(desk, _body(name="Basavraj Hiremath", phone=phone, sex="M", date_of_birth=None,
                             age_years=70))
    assert resp.status_code == 409
    assert resp.json()["detail"]["candidates"][0]["patient_id"] == out["patient_id"]
    assert resp.json()["detail"]["candidates"][0]["inpatient_now"] is True


def test_match_needs_something_to_match(desk, warn_mode):
    resp = client.post("/api/registration/match", json={"sex": "F"}, headers=_hdr(desk))
    assert resp.status_code == 422


# ═════════════════════════════════════════════════════════════════════════════
# 5. The worklist and resolution
# ═════════════════════════════════════════════════════════════════════════════

def _overridden_pair(desk):
    first, body = _existing(desk)
    resp = _post(desk, _twin(first, body, duplicate_review={
        "decision": "register_new", "candidate_ids": [first["patient_id"]],
        "reason": "Different people sharing a phone"}))
    assert resp.status_code == 201, resp.text
    new_id = resp.json()["patient"]["patient_id"]
    pair = tuple(sorted((new_id, first["patient_id"])))
    with db_session() as db:
        cid = db.execute("SELECT id FROM patient_duplicate_candidates WHERE patient_id = ? AND "
                         "candidate_patient_id = ?", pair).fetchone()[0]
    return cid, pair, body


def test_the_worklist_shows_minimal_fields_and_is_audited(desk, warn_mode):
    cid, pair, body = _overridden_pair(desk)
    resp = client.get("/api/registration/duplicates", headers=_hdr(desk))
    assert resp.status_code == 200, resp.text
    item = [i for i in resp.json()["items"] if i["id"] == cid][0]
    assert {item["patient"]["patient_id"], item["candidate"]["patient_id"]} == set(pair)
    assert item["overridden_by"] == desk[0] and item["band"] == "probable"
    assert body["date_of_birth"] not in resp.text and body["phone"] not in resp.text
    rows = _audit_rows("duplicate_worklist_viewed")
    assert set(pair) <= set(json.loads(rows[-1]["metadata_json"])["matched_patient_ids"])


def test_the_overrider_cannot_resolve_and_the_refusal_is_audited(desk, warn_mode):
    cid, pair, _ = _overridden_pair(desk)
    resp = client.post(f"/api/registration/duplicates/{cid}/resolve", headers=_hdr(desk),
                       json={"resolution": "not_duplicate", "reason": "Checked, different people"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "resolver_is_overrider"
    assert _audit_rows("duplicate_resolution_refused", pair[0])


def test_another_desk_user_resolves_once_and_it_is_audited(desk, desk2, warn_mode):
    cid, pair, _ = _overridden_pair(desk)
    no_reason = client.post(f"/api/registration/duplicates/{cid}/resolve", headers=_hdr(desk2),
                            json={"resolution": "not_duplicate"})
    assert no_reason.status_code == 422
    ok = client.post(f"/api/registration/duplicates/{cid}/resolve", headers=_hdr(desk2),
                     json={"resolution": "not_duplicate", "reason": "Called both, different people"})
    assert ok.status_code == 200, ok.text
    rows = _audit_rows("duplicate_resolved", pair[0])
    assert rows and rows[-1]["event_category"] == "patient_identity"
    again = client.post(f"/api/registration/duplicates/{cid}/resolve", headers=_hdr(desk2),
                        json={"resolution": "confirmed_duplicate", "reason": "Changed my mind now"})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "already_resolved"


def test_merged_is_not_a_resolution_the_desk_can_choose(desk, desk2, warn_mode):
    cid, _, _ = _overridden_pair(desk)
    resp = client.post(f"/api/registration/duplicates/{cid}/resolve", headers=_hdr(desk2),
                       json={"resolution": "merged", "reason": "Merged the two charts"})
    assert resp.status_code == 422


def test_the_administrator_reads_the_worklist_but_cannot_resolve(desk, admin, warn_mode):
    cid, _, _ = _overridden_pair(desk)
    assert client.get("/api/registration/duplicates", headers=_hdr(admin)).status_code == 200
    resp = client.post(f"/api/registration/duplicates/{cid}/resolve", headers=_hdr(admin),
                       json={"resolution": "not_duplicate", "reason": "Different people here"})
    assert resp.status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# 6. The sweep
# ═════════════════════════════════════════════════════════════════════════════

def _raw_patient(name, phone, mrn=None, dob=None):
    pid = f"PAT-RGD{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, sex, phone, date_of_birth, status, created_at) "
                   "VALUES (?, ?, ?, 'F', ?, ?, 'discharged', CURRENT_TIMESTAMP)",
                   (pid, name, mrn or f"RGD{uuid.uuid4().hex[:10]}", phone, dob))
    return pid


def test_the_sweep_is_the_administrators_and_opens_bounded_pairs(desk, admin, warn_mode):
    phone = _phone()
    a = _raw_patient("Nagaveni Kulkarni", phone)
    b = _raw_patient("Nagaveni Kulkarni", phone)
    base = f"SW{uuid.uuid4().hex[:6].upper()}"
    c = _raw_patient(_word() + " " + _word(), _phone(), mrn=f"{base}-01")
    d = _raw_patient(_word() + " " + _word(), _phone(), mrn=f"{base.lower()}01")
    assert client.post("/api/registration/duplicates/sweep", json={},
                       headers=_hdr(desk)).status_code == 403
    resp = client.post("/api/registration/duplicates/sweep", json={"max_pairs": 500},
                       headers=_hdr(admin))
    assert resp.status_code == 200, resp.text
    with db_session() as db:
        rows = {(r["patient_id"], r["candidate_patient_id"]): dict(r) for r in db.execute(
            "SELECT * FROM patient_duplicate_candidates WHERE source = 'sweep'")}
    phone_pair = rows[tuple(sorted((a, b)))]
    uhid_pair = rows[tuple(sorted((c, d)))]
    assert json.loads(phone_pair["reasons_json"])[:2] == ["phone", "name_exact"]
    assert json.loads(uhid_pair["reasons_json"]) == ["uhid"]
    assert _audit_rows("duplicate_sweep_run")
    # A second sweep opens nothing new for the same pairs.
    again = client.post("/api/registration/duplicates/sweep", json={}, headers=_hdr(admin)).json()
    with db_session() as db:
        n = db.execute("SELECT COUNT(*) FROM patient_duplicate_candidates WHERE patient_id = ? "
                       "AND candidate_patient_id = ?", tuple(sorted((a, b)))).fetchone()[0]
    assert n == 1 and again["max_pairs"] == 200


def test_the_sweep_stops_at_its_limit(desk, admin, warn_mode):
    for _ in range(3):
        phone = _phone()
        name = f"{_word()} {_word()}"
        _raw_patient(name, phone)
        _raw_patient(name, phone)
    resp = client.post("/api/registration/duplicates/sweep", json={"max_pairs": 1},
                       headers=_hdr(admin))
    body = resp.json()
    assert body["pairs_opened"] == 1 and body["truncated"] is True


# ═════════════════════════════════════════════════════════════════════════════
# 7. A crowded phone or date of birth (the candidate scan is cut)
# ═════════════════════════════════════════════════════════════════════════════

def _crowd_size():
    from services import patient_matching as pm
    return pm.MAX_CANDIDATE_ROWS + 1


def _crowd(*, phone=None, date_of_birth=None, sex="F", age=30, surname=None):
    """More older records than one match scores, written by an importer and
    sharing one crowded detail. Their name words start with Q (folded to k),
    so they share no word with a probe's Y words unless `surname` is given."""
    from services import patient_matching as pm
    with db_session() as db:
        for _ in range(_crowd_size()):
            db.execute(
                "INSERT INTO patients (id, name, mrn, age, date_of_birth, sex, phone, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'registered', CURRENT_TIMESTAMP)",
                (f"PAT-RGC{uuid.uuid4().hex[:9].upper()}",
                 f"{_word('Q')} {surname or _word('Q')}", f"RGC{uuid.uuid4().hex[:10].upper()}",
                 None if date_of_birth else age, date_of_birth, sex, phone))
        pm.sync_keys(db)


def _helpline():
    return "80" + "".join(random.choice(string.digits) for _ in range(8))


def _crowded_dob():
    return f"{random.randint(1915, 1925)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _uhid():
    return f"RGD{uuid.uuid4().hex[:8].upper()}"


def _meta(row):
    return json.loads(row["metadata_json"])


def test_block_a_crowded_helpline_never_hides_a_second_chart(desk, block_mode):
    helpline = _helpline()
    _crowd(phone=helpline)
    body = {"name": f"{_word()} {_word()}", "sex": "M", "age_years": 52, "phone": helpline}
    # Every record sharing the number could be a possible duplicate (40 for
    # the phone, 10 for an age) and the scan could not score them all, so the
    # first registration needs a review even though none is listed.
    first = _post(desk, {**body, "uhid": _uhid()})
    assert first.status_code == 409, first.text
    detail = first.json()["detail"]
    assert detail["error"] == "duplicate_review_required"
    assert detail["check_incomplete"] is True and detail["candidates"] == []
    meta = _meta(_audit_rows("registration_refused")[-1])
    assert meta["truncated"] is True and meta["check_incomplete"] is True
    assert meta["unseen_band"] == "possible"
    ok = _post(desk, {**body, "uhid": _uhid(), "duplicate_review": {
        "decision": "register_new", "candidate_ids": [],
        "reason": "Hospital helpline typed, the patient has no phone"}})
    assert ok.status_code == 201, ok.text
    pid = ok.json()["patient"]["patient_id"]
    assert ok.json()["duplicate_review"]["check_incomplete"] is True
    override = _audit_rows("registration_duplicate_override", pid)
    assert override and _meta(override[-1])["check_incomplete"] is True
    assert _meta(override[-1])["candidate_ids"] == []
    # The same patient again: found first, though the crowd shares the number.
    match = client.post("/api/registration/match", json=body, headers=_hdr(desk))
    assert match.status_code == 200, match.text
    hit = [c for c in match.json()["candidates"] if c["patient_id"] == pid]
    assert hit and hit[0]["band"] == "probable"
    assert match.json()["truncated"] is True and match.json()["check_incomplete"] is True
    assert match.json()["blocked"] is True
    assert _meta(_audit_rows("registration_match_checked")[-1])["check_incomplete"] is True
    again = _post(desk, {**body, "uhid": _uhid()})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["error"] == "probable_duplicate"
    assert pid in [c["patient_id"] for c in again.json()["detail"]["candidates"]]


def test_warn_a_crowded_helpline_still_lists_the_true_duplicate(desk, warn_mode):
    helpline = _helpline()
    _crowd(phone=helpline)
    body = {"name": f"{_word()} {_word()}", "sex": "F", "age_years": 61, "phone": helpline}
    review = {"decision": "register_new", "candidate_ids": [],
              "reason": "Hospital helpline typed, the patient has no phone"}
    first = _post(desk, {**body, "uhid": _uhid(), "duplicate_review": review})
    assert first.status_code == 201, first.text
    pid = first.json()["patient"]["patient_id"]
    # A review that named nobody is stale now that the first chart exists.
    again = _post(desk, {**body, "uhid": _uhid(), "duplicate_review": review})
    assert again.status_code == 409, again.text
    detail = again.json()["detail"]
    assert detail["error"] == "duplicate_review_stale" and detail["check_incomplete"] is True
    hit = [c for c in detail["candidates"] if c["patient_id"] == pid]
    assert hit and hit[0]["band"] == "probable"


def test_block_a_crowded_date_of_birth_never_hides_a_second_chart(desk, block_mode):
    dob = _crowded_dob()
    _crowd(date_of_birth=dob, sex="M")
    body = {"name": f"{_word()} {_word()}", "sex": "M", "date_of_birth": dob, "phone": _phone()}
    # Records sharing only the date of birth score 30 at most, below every
    # band: the cut hides nothing, so no review, but the cut is audited.
    first = _post(desk, {**body, "uhid": _uhid()})
    assert first.status_code == 201, first.text
    pid = first.json()["patient"]["patient_id"]
    assert first.json()["duplicate_review"] is None
    again = _post(desk, {**body, "uhid": _uhid()})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["error"] == "probable_duplicate"
    hit = [c for c in again.json()["detail"]["candidates"] if c["patient_id"] == pid]
    assert hit and hit[0]["score"] == 110
    meta = _meta(_audit_rows("patient_registered", pid)[-1])
    assert meta["candidate_scan_truncated"] is True and meta["check_incomplete"] is False


@pytest.mark.parametrize("mode", ["block", "warn"])
def test_records_left_out_that_could_be_probable(desk, warn_mode, monkeypatch, mode):
    monkeypatch.setattr(config, "DUPLICATE_MODE", mode)
    helpline = _helpline()
    surname = _word()
    # Each shares the number and a surname: 40 + 25, plus 10 for an age, so a
    # record left out could be probable. None scores into a band itself
    # (another sex, an age 22 years off: 45), so nothing is listed.
    _crowd(phone=helpline, surname=surname, sex="F", age=30)
    body = {"name": f"{_word()} {surname}", "sex": "M", "age_years": 52, "phone": helpline}
    review = {"decision": "register_new", "candidate_ids": [],
              "reason": "Hospital helpline typed, the patient has no phone"}
    preview = client.post("/api/registration/match", json=body, headers=_hdr(desk)).json()
    assert preview["candidates"] == [] and preview["check_incomplete"] is True
    resp = _post(desk, {**body, "uhid": _uhid()})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["check_incomplete"] is True and detail["candidates"] == []
    meta = _meta(_audit_rows("registration_refused")[-1])
    assert meta["unseen_band"] == "probable" and meta["reason_code"] == detail["error"]
    with_review = _post(desk, {**body, "uhid": _uhid(), "duplicate_review": review})
    if mode == "block":
        # The strict side of OPD-S2: a record that could be a probable
        # duplicate is refused unseen, whatever review is sent.
        assert preview["blocked"] is True and preview["review_required"] is False
        assert detail["error"] == "duplicate_check_incomplete"
        assert with_review.status_code == 409, with_review.text
        assert with_review.json()["detail"]["error"] == "duplicate_check_incomplete"
    else:
        assert preview["blocked"] is False and preview["review_required"] is True
        assert detail["error"] == "duplicate_review_required"
        assert with_review.status_code == 201, with_review.text
        pid = with_review.json()["patient"]["patient_id"]
        override = _audit_rows("registration_duplicate_override", pid)
        assert override and _meta(override[-1])["unseen_band"] == "probable"
