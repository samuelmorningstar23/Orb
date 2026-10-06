"""The registration service (opd-4, W4-A, 4 October 2026).

services/registration_service.register_patient writes a patient, their
demographics, their match keys, an unverified ABHA and the patient_identity
audit rows in ONE immediate transaction. What this file pins:

  1. The UHID: Verhoeff check digits, the series allocator on the caller's
     connection (no yearly reset), orb mode refused until the series is
     configured, concurrent orb-mode registrations never share a number;
     hospital mode needs the number, honours the site pattern and refuses a
     number already in use (exactly or once case and separators are ignored).
  2. What a registration writes, and that an audit failure rolls ALL of it
     back; an idempotent replay lands on the same row.
  3. Date of birth or stated age, never both, never an estimated DOB; the
     identity document as type plus last 4 only; ABHA always unverified.
  4. The stated-identifier check: match or no match, never a stored value,
     audited, and the sixth failure in an hour is refused.
  5. Corrections: a reason, the row_version, an append-only change log, the
     UHID for the administrator only.
"""
from __future__ import annotations

import json
import random
import string
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import config
from database import db_session
from services import billing_masters
from services import registration_service as reg
from services.audit_service import AuditWriteError
from services.hospital_time import hospital_today


# ── fixtures and helpers ─────────────────────────────────────────────────────

def _mk_user(role, department=None):
    uid = f"RGS{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash, department, is_active) "
                   "VALUES (?, ?, ?, 'x', ?, 1)", (uid, f"Regsvc {role} {uid[-4:]}", role, department))
    return {"id": uid, "role": role, "department": department, "name": f"Regsvc {role}"}


@pytest.fixture(scope="module")
def desk():
    return _mk_user("receptionist")


@pytest.fixture(scope="module")
def admin():
    return _mk_user("admin")


@pytest.fixture
def hospital_mode(monkeypatch):
    monkeypatch.setattr(config, "UHID_SOURCE", "hospital")
    monkeypatch.setattr(config, "UHID_PATTERN", None)
    monkeypatch.setattr(config, "DUPLICATE_MODE", "warn")
    monkeypatch.setattr(config, "ABDM_ENABLED", True)


@pytest.fixture
def orb_mode(monkeypatch):
    monkeypatch.setattr(config, "UHID_SOURCE", "orb")
    monkeypatch.setattr(config, "DUPLICATE_MODE", "warn")
    with db_session() as db:
        before = dict(db.execute("SELECT * FROM document_number_series WHERE series_code = 'UHID'")
                      .fetchone())
    yield before
    with db_session() as db:
        db.execute("UPDATE document_number_series SET prefix = ?, is_configured = ?, "
                   "pad_width = ? WHERE series_code = 'UHID'",
                   (before["prefix"], before["is_configured"], before["pad_width"]))


def _configure_uhid_series(prefix="UH"):
    with db_session() as db:
        db.execute("UPDATE document_number_series SET prefix = ?, is_configured = 1, pad_width = 6 "
                   "WHERE series_code = 'UHID'", (prefix,))


def _name():
    """A random two-word name of letters only, so no other test's patient can
    be a duplicate candidate."""
    def word():
        return "Q" + "".join(random.choice("bcdfgjklmnprtvz") + random.choice("aeiou")
                             for _ in range(3))
    return f"{word()} {word()}"


def _phone():
    return "9" + "".join(random.choice(string.digits) for _ in range(9))


def _uhid():
    return f"HSP{uuid.uuid4().hex[:8].upper()}"


def _dob():
    return f"{random.randint(1935, 2005)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"


def _payload(**kw):
    body = {"name": _name(), "sex": "F", "date_of_birth": _dob(), "phone": _phone()}
    if config.UHID_SOURCE == "hospital":
        body["uhid"] = _uhid()
    body.update(kw)
    return body


def _audit_rows(action, patient_id=None):
    with db_session() as db:
        if patient_id is None:
            rows = db.execute("SELECT * FROM audit_logs WHERE action_type = ? ORDER BY id",
                              (action,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM audit_logs WHERE action_type = ? AND patient_id = ? "
                              "ORDER BY id", (action, patient_id)).fetchall()
    return [dict(r) for r in rows]


def _count(table, patient_id, column="patient_id"):
    with db_session() as db:
        return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?",
                          (patient_id,)).fetchone()[0]


# ═════════════════════════════════════════════════════════════════════════════
# 1. The UHID
# ═════════════════════════════════════════════════════════════════════════════

def test_verhoeff_known_vectors():
    assert billing_masters.verhoeff_check_digit("236") == "3"
    assert billing_masters.verhoeff_check_digit("12345") == "1"
    assert billing_masters.verhoeff_check_digit("142857") == "0"
    assert billing_masters.verhoeff_valid("2363")
    assert not billing_masters.verhoeff_valid("2364")


def test_verhoeff_catches_every_adjacent_transposition_and_single_digit_error():
    rng = random.Random(3)
    for _ in range(300):
        digits = "".join(rng.choice(string.digits) for _ in range(7))
        number = digits + billing_masters.verhoeff_check_digit(digits)
        assert billing_masters.verhoeff_valid(number)
        for i in range(len(number) - 1):
            if number[i] != number[i + 1]:
                swapped = number[:i] + number[i + 1] + number[i] + number[i + 2:]
                assert not billing_masters.verhoeff_valid(swapped), (number, swapped)
        i = rng.randrange(len(number))
        wrong = str((int(number[i]) + rng.randint(1, 9)) % 10)
        assert not billing_masters.verhoeff_valid(number[:i] + wrong + number[i + 1:])


def test_verhoeff_refuses_non_digits():
    assert not billing_masters.verhoeff_valid("12a4")
    with pytest.raises(ValueError):
        billing_masters.verhoeff_check_digit("12a")


def test_the_allocator_needs_the_callers_transaction():
    with db_session() as db:
        with pytest.raises(RuntimeError):
            billing_masters.allocate_series_number(db, "UHID")


def test_the_allocator_refuses_an_unconfigured_series_before_moving_it(orb_mode):
    with db_session() as db:
        db.execute("UPDATE document_number_series SET is_configured = 0 WHERE series_code = 'UHID'")
    with db_session(immediate=True) as db:
        before = db.execute("SELECT next_number FROM document_number_series "
                            "WHERE series_code = 'UHID'").fetchone()[0]
        with pytest.raises(billing_masters.SeriesNotConfigured):
            billing_masters.allocate_series_number(db, "UHID", require_configured=True)
        after = db.execute("SELECT next_number FROM document_number_series "
                           "WHERE series_code = 'UHID'").fetchone()[0]
    assert before == after


def test_a_uhid_never_resets_never_embeds_a_year_and_carries_a_check_digit(orb_mode):
    _configure_uhid_series("UH")
    with db_session(immediate=True) as db:
        a = billing_masters.allocate_series_number(db, "UHID", require_configured=True)
        b = billing_masters.allocate_series_number(db, "UHID", when="2031-06-01",
                                                   require_configured=True)
    assert a["financial_year"] is None and b["financial_year"] is None
    assert b["sequence"] == a["sequence"] + 1
    for alloc in (a, b):
        body = alloc["number"][2:]
        assert alloc["number"].startswith("UH") and len(body) == 7 and body.isdigit()
        assert billing_masters.verhoeff_valid(body)
        assert "20" + "26" not in alloc["number"][:4]


def test_a_yearly_series_still_numbers_like_next_document_number():
    with db_session(immediate=True) as db:
        out = billing_masters.allocate_series_number(db, "INV", when="2026-10-04")
    assert out["number"].startswith("INV-2026-27-")
    assert out["check_digit"] is None


def test_orb_mode_refuses_until_the_series_is_configured(orb_mode, desk):
    with db_session() as db:
        db.execute("UPDATE document_number_series SET is_configured = 0 WHERE series_code = 'UHID'")
    body = _payload()
    with pytest.raises(reg.RegistrationConflict) as err:
        reg.register_patient(body, user=desk)
    assert err.value.code == "uhid_series_not_configured" and err.value.status == 409
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patients WHERE name = ?",
                          (body["name"],)).fetchone()[0] == 0


def test_orb_mode_issues_a_checked_uhid_and_refuses_a_typed_one(orb_mode, desk):
    _configure_uhid_series("UH")
    out = reg.register_patient(_payload(), user=desk)
    uhid = out["patient"]["uhid"]
    assert uhid.startswith("UH") and reg.uhid_check(uhid)
    transposed = uhid[:3] + uhid[4] + uhid[3] + uhid[5:]
    if transposed != uhid:
        assert not reg.uhid_check(transposed)
    with pytest.raises(reg.RegistrationError) as err:
        reg.register_patient(_payload(uhid="UH1234567"), user=desk)
    assert err.value.code == "uhid_assigned_by_orb"


def test_concurrent_orb_mode_registrations_never_share_a_uhid(orb_mode, desk):
    _configure_uhid_series("UH")
    results, errors = [], []

    def one():
        try:
            results.append(reg.register_patient(_payload(), user=desk)["patient"]["uhid"])
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    threads = [threading.Thread(target=one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert len(results) == 8 and len(set(results)) == 8
    sequences = sorted(int(u[2:-1]) for u in results)
    assert sequences == list(range(sequences[0], sequences[0] + 8))


def test_hospital_mode_needs_the_number(hospital_mode, desk):
    with pytest.raises(reg.RegistrationError) as err:
        reg.register_patient(_payload(uhid=None), user=desk)
    assert err.value.code == "uhid_required"


def test_hospital_mode_honours_the_site_pattern(hospital_mode, desk, monkeypatch):
    monkeypatch.setattr(config, "UHID_PATTERN", r"HSP[0-9A-F]{8}")
    reg.register_patient(_payload(), user=desk)
    with pytest.raises(reg.RegistrationError) as err:
        reg.register_patient(_payload(uhid="12-34"), user=desk)
    assert err.value.code == "uhid_format"


def test_an_invalid_site_pattern_refuses_every_registration(hospital_mode, desk, monkeypatch):
    monkeypatch.setattr(config, "UHID_PATTERN", r"HSP[")
    with pytest.raises(reg.RegistrationConflict) as err:
        reg.register_patient(_payload(), user=desk)
    assert err.value.code == "uhid_pattern_invalid"
    assert reg.registration_config()["uhid_ready"] is False


def test_a_hospital_number_in_use_is_refused_exactly_and_normalised(hospital_mode, desk):
    first = reg.register_patient(_payload(), user=desk)["patient"]
    uhid = first["uhid"]
    for typed in (uhid, uhid.lower(), f"{uhid[:3]}-{uhid[3:]}"):
        with pytest.raises(reg.RegistrationConflict) as err:
            reg.register_patient(_payload(uhid=typed), user=desk)
        assert err.value.code == "existing_record"
        candidates = err.value.extra["candidates"]
        assert [c["patient_id"] for c in candidates] == [first["patient_id"]]
        assert candidates[0]["band"] == "hard"
        assert "date_of_birth" not in candidates[0] and "phone" not in candidates[0]


# ═════════════════════════════════════════════════════════════════════════════
# 2. What a registration writes, all or nothing
# ═════════════════════════════════════════════════════════════════════════════

def test_a_registration_writes_one_registered_patient_with_no_login(hospital_mode, desk):
    out = reg.register_patient(_payload(abha_number="91-1234-5678-9012"), user=desk)
    pid = out["patient"]["patient_id"]
    with db_session() as db:
        p = db.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
        d = db.execute("SELECT * FROM patient_demographics WHERE patient_id = ?", (pid,)).fetchone()
        k = db.execute("SELECT * FROM patient_match_keys WHERE patient_id = ?", (pid,)).fetchone()
        a = db.execute("SELECT * FROM abha_identities WHERE patient_id = ?", (pid,)).fetchall()
        allergies = db.execute("SELECT COUNT(*) FROM patient_allergies WHERE patient_id = ?",
                               (pid,)).fetchone()[0]
        admissions = db.execute("SELECT COUNT(*) FROM admissions WHERE patient_id = ?",
                                (pid,)).fetchone()[0]
    assert p["status"] == "registered" and p["admission_date"] is None
    assert p["user_id"] is None, "registration must not create a portal login"
    assert allergies == 0 and admissions == 0
    assert d["registration_source"] == "desk" and d["registered_by"] == desk["id"]
    assert d["row_version"] == 1 and d["uhid_source"] == "hospital"
    assert k["name_key"] and k["src_name"] == p["name"]
    assert len(a) == 1 and a[0]["verification_status"] == "unverified"
    assert out["patient"]["abha"][0]["is_verified"] is False
    rows = _audit_rows("patient_registered", pid)
    assert len(rows) == 1 and rows[0]["event_category"] == "patient_identity"
    assert json.loads(rows[0]["metadata_json"])["abha_recorded"] is True


def test_an_audit_failure_rolls_the_whole_registration_back(hospital_mode, desk, monkeypatch):
    real = reg.log_audit_event

    def failing(*args, **kwargs):
        if kwargs.get("action_type") == "patient_registered":
            raise AuditWriteError("simulated audit outage")
        return real(*args, **kwargs)

    monkeypatch.setattr(reg, "log_audit_event", failing)
    body = _payload(abha_number="91-2222-3333-4444")
    with pytest.raises(AuditWriteError):
        reg.register_patient(body, user=desk, idempotency_key="rollback-1")
    pid = reg._patient_id_for(desk["id"], "rollback-1")
    for table in ("patients", "patient_demographics", "patient_match_keys", "abha_identities"):
        column = "id" if table == "patients" else "patient_id"
        assert _count(table, pid, column) == 0, f"{table} survived an unwritten audit row"


def test_an_idempotent_replay_lands_on_the_same_patient(hospital_mode, desk):
    key = f"replay-{uuid.uuid4().hex}"
    body = _payload()
    first = reg.register_patient(body, user=desk, idempotency_key=key)
    again = reg.register_patient(body, user=desk, idempotency_key=key)
    assert first["replayed"] is False and again["replayed"] is True
    assert again["patient"]["patient_id"] == first["patient"]["patient_id"]
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patients WHERE name = ?",
                          (body["name"],)).fetchone()[0] == 1
    rows = _audit_rows("patient_registered", first["patient"]["patient_id"])
    assert [json.loads(r["metadata_json"])["replay"] for r in rows] == [False, True]


def test_a_replayed_key_with_a_different_patient_is_refused(hospital_mode, desk):
    key = f"replay-{uuid.uuid4().hex}"
    reg.register_patient(_payload(), user=desk, idempotency_key=key)
    with pytest.raises(reg.RegistrationConflict) as err:
        reg.register_patient(_payload(), user=desk, idempotency_key=key)
    assert err.value.code == "registration_key_conflict"
    assert "candidates" not in err.value.extra
    assert _audit_rows("registration_replay_refused")


def test_the_same_key_from_another_registrar_is_another_registration(hospital_mode, desk):
    other = _mk_user("receptionist")
    key = f"shared-{uuid.uuid4().hex}"
    a = reg.register_patient(_payload(), user=desk, idempotency_key=key)
    b = reg.register_patient(_payload(), user=other, idempotency_key=key)
    assert a["patient"]["patient_id"] != b["patient"]["patient_id"]


def test_a_replay_answers_the_id_and_uhid_only_never_the_current_record(hospital_mode, desk,
                                                                         admin):
    key = f"replay-{uuid.uuid4().hex}"
    body = _payload(address_line="12 First Street")
    first = reg.register_patient(body, user=desk, idempotency_key=key)["patient"]
    pid = first["patient_id"]
    _correct(pid, {"address_line": "77 Secret Lane", "email": "later@example.com",
                   "guardian_name": "Later Guardian"}, admin, row_version=1)
    again = reg.register_patient(body, user=desk, idempotency_key=key)
    assert again["replayed"] is True and again["duplicate_review"] is None
    assert again["patient"] == {"patient_id": pid, "uhid": first["uhid"],
                                "record_returned": False}
    text = json.dumps(again)
    for leaked in ("Secret Lane", "later@example.com", "Later Guardian", "inpatient_now",
                   "First Street", body["date_of_birth"]):
        assert leaked not in text
    meta = json.loads(_audit_rows("patient_registered", pid)[-1]["metadata_json"])
    assert meta["replay"] is True and meta["record_returned"] is False


def test_a_replay_after_the_window_is_refused_and_writes_nothing(hospital_mode, desk):
    key = f"replay-{uuid.uuid4().hex}"
    body = _payload()
    pid = reg.register_patient(body, user=desk, idempotency_key=key)["patient"]["patient_id"]
    stale = (datetime.now(timezone.utc) - reg.REPLAY_WINDOW - timedelta(minutes=5)).isoformat()
    with db_session() as db:
        db.execute("UPDATE patient_demographics SET registered_at = ? WHERE patient_id = ?",
                   (stale, pid))
    before = len(_audit_rows("patient_registered", pid))
    with pytest.raises(reg.RegistrationConflict) as err:
        reg.register_patient(body, user=desk, idempotency_key=key)
    assert err.value.code == "registration_key_expired" and err.value.status == 409
    assert "patient_id" not in err.value.extra and "candidates" not in err.value.extra
    assert len(_audit_rows("patient_registered", pid)) == before
    refused = [json.loads(r["metadata_json"]) for r in _audit_rows("registration_replay_refused")]
    assert refused and refused[-1]["reason_code"] == "registration_key_expired"
    with db_session() as db:
        assert db.execute("SELECT COUNT(*) FROM patients WHERE name = ?",
                          (body["name"],)).fetchone()[0] == 1


def test_the_replay_window_fails_closed_on_a_missing_or_unreadable_stamp():
    now = datetime.now(timezone.utc)
    assert reg._replay_window_open(now.isoformat(), now=now) is True
    assert reg._replay_window_open((now - reg.REPLAY_WINDOW).isoformat(), now=now) is True
    late = now - reg.REPLAY_WINDOW - timedelta(seconds=1)
    assert reg._replay_window_open(late.isoformat(), now=now) is False
    assert reg._replay_window_open(None, now=now) is False
    assert reg._replay_window_open("", now=now) is False
    assert reg._replay_window_open("not a time", now=now) is False


# ═════════════════════════════════════════════════════════════════════════════
# 3. Age, identity documents, ABHA
# ═════════════════════════════════════════════════════════════════════════════

def test_a_stated_age_is_stored_with_its_date_never_as_a_dob(hospital_mode, desk):
    out = reg.register_patient(_payload(date_of_birth=None, age_years=52), user=desk)
    p = out["patient"]
    assert p["date_of_birth"] is None and p["age"] == 52 and p["age_stated"] is True
    assert p["age_recorded_on"] == hospital_today().isoformat()
    assert p["discharge_link_possible"] is False and p["date_of_birth_hint"] == reg.NO_DOB_HINT


@pytest.mark.parametrize("extra, code", [
    ({"date_of_birth": "1984-07-09", "age_years": 41}, "age_with_date_of_birth"),
    ({"date_of_birth": None}, "age_or_date_of_birth_required"),
    ({"date_of_birth": "2999-01-01"}, "bad_date_of_birth"),
    ({"age_years": 131, "date_of_birth": None}, "bad_age"),
    ({"sex": "unknown"}, "sex_required"),
    ({"name": "X"}, "name_invalid"),
    ({"phone": "call me"}, "bad_phone"),
    ({"id_document_type": "aadhaar", "id_document_last4": "123456789012"}, "bad_id_document_last4"),
    ({"id_document_last4": "9012"}, "bad_id_document_type"),
    ({"pin_code": "56001"}, "bad_pin_code"),
])
def test_registration_refuses_what_it_must_not_store(hospital_mode, desk, extra, code):
    with pytest.raises(reg.RegistrationError) as err:
        reg.register_patient(_payload(**extra), user=desk)
    assert err.value.code == code


def test_an_identity_document_is_kept_as_type_and_last_four(hospital_mode, desk):
    out = reg.register_patient(_payload(id_document_type="Aadhaar", id_document_last4="9012"),
                               user=desk)
    demo = out["patient"]["demographics"]
    assert demo["id_document_type"] == "aadhaar" and demo["id_document_last4"] == "9012"


def test_abha_is_refused_while_the_abdm_module_is_off(hospital_mode, desk, monkeypatch):
    monkeypatch.setattr(config, "ABDM_ENABLED", False)
    with pytest.raises(reg.RegistrationError) as err:
        reg.register_patient(_payload(abha_number="91-5555-6666-7777"), user=desk)
    assert err.value.code == "abdm_off"


def test_record_abha_on_rolls_back_with_its_caller(hospital_mode, desk):
    from services.abdm_service import record_abha_on
    pid = reg.register_patient(_payload(), user=desk)["patient"]["patient_id"]
    with pytest.raises(RuntimeError):
        with db_session(immediate=True) as db:
            out = record_abha_on(db, patient_id=pid, abha_number="91-7777-8888-9999",
                                 recorded_by=desk["id"])
            assert out["verification_status"] == "unverified"
            raise RuntimeError("caller fails after the ABHA write")
    assert _count("abha_identities", pid) == 0


def test_record_abha_on_has_no_way_to_record_a_verified_abha():
    import inspect
    from services.abdm_service import record_abha_on
    params = set(inspect.signature(record_abha_on).parameters)
    assert not params & {"verification_status", "verified", "is_verified"}


# ═════════════════════════════════════════════════════════════════════════════
# 4. The stated-identifier check
# ═════════════════════════════════════════════════════════════════════════════

def _registered(desk, **kw):
    body = _payload(**kw)
    return reg.register_patient(body, user=desk)["patient"], body


def test_the_right_identifiers_match_and_nothing_stored_comes_back(hospital_mode, desk):
    patient, body = _registered(desk, date_of_birth="1961-11-23", phone="98450 61234")
    checker = _mk_user("receptionist")
    out = reg.check_identity(patient["patient_id"], {
        "name": body["name"].split()[0], "date_of_birth": "1961-11-23", "phone_last4": "1234",
    }, user=checker)
    assert out["result"] == "match"
    assert set(out) == {"result", "checked", "checked_at"}
    assert "1961" not in json.dumps(out)


def test_a_wrong_date_of_birth_is_no_match_and_audited(hospital_mode, desk):
    patient, body = _registered(desk, date_of_birth="1961-11-23")
    checker = _mk_user("receptionist")
    out = reg.check_identity(patient["patient_id"], {
        "name": body["name"], "date_of_birth": "1961-11-24", "uhid": patient["uhid"],
    }, user=checker)
    assert out == {"result": "no_match", "checked": ["name", "date_of_birth", "uhid"],
                   "checked_at": out["checked_at"]}
    rows = _audit_rows("identity_check_failed", patient["patient_id"])
    assert rows and rows[-1]["event_category"] == "patient_identity"
    assert json.loads(rows[-1]["metadata_json"])["mismatched"] == ["date_of_birth"]


def test_a_stated_age_checks_against_a_stated_age(hospital_mode, desk):
    patient, body = _registered(desk, date_of_birth=None, age_years=60)
    checker = _mk_user("receptionist")
    ok = reg.check_identity(patient["patient_id"], {"name": body["name"], "age_years": 61,
                                                   "uhid": patient["uhid"]}, user=checker)
    bad = reg.check_identity(patient["patient_id"], {"name": body["name"], "age_years": 63,
                                                    "uhid": patient["uhid"]}, user=checker)
    assert ok["result"] == "match" and bad["result"] == "no_match"


def test_two_identifiers_beside_the_name_are_required(hospital_mode, desk):
    patient, body = _registered(desk)
    with pytest.raises(reg.RegistrationError) as err:
        reg.check_identity(patient["patient_id"], {"name": body["name"],
                                                   "date_of_birth": body["date_of_birth"]},
                           user=desk)
    assert err.value.code == "identifiers_required"


def test_the_sixth_failure_in_an_hour_is_refused_whatever_it_says(hospital_mode, desk):
    patient, body = _registered(desk, date_of_birth="1970-01-15")
    checker = _mk_user("receptionist")
    wrong = {"name": body["name"], "date_of_birth": "1970-01-16", "uhid": patient["uhid"]}
    right = {"name": body["name"], "date_of_birth": "1970-01-15", "uhid": patient["uhid"]}
    for _ in range(reg.IDENTITY_FAILURE_LIMIT):
        assert reg.check_identity(patient["patient_id"], wrong, user=checker)["result"] == "no_match"
    with pytest.raises(reg.RegistrationRateLimited) as err:
        reg.check_identity(patient["patient_id"], right, user=checker)
    assert err.value.status == 429
    assert _audit_rows("identity_check_rate_limited", patient["patient_id"])
    # Another desk user is counted separately.
    other = _mk_user("receptionist")
    assert reg.check_identity(patient["patient_id"], right, user=other)["result"] == "match"


# ═════════════════════════════════════════════════════════════════════════════
# 5. Corrections
# ═════════════════════════════════════════════════════════════════════════════

def _correct(pid, changes, user, *, row_version=1, reason="Patient showed their card",
             fields=True, uhid=False):
    return reg.correct_demographics(pid, changes, row_version=row_version, reason=reason,
                                    user=user, may_edit_fields=fields, may_correct_uhid=uhid)


def test_a_correction_needs_a_reason(hospital_mode, desk):
    patient, _ = _registered(desk)
    with pytest.raises(reg.RegistrationError) as err:
        _correct(patient["patient_id"], {"city": "Mysuru"}, desk, reason="  ")
    assert err.value.code == "reason_required" and err.value.status == 422


def test_a_stale_row_version_is_refused(hospital_mode, desk):
    patient, _ = _registered(desk)
    _correct(patient["patient_id"], {"city": "Mysuru"}, desk, row_version=1)
    with pytest.raises(reg.RegistrationConflict) as err:
        _correct(patient["patient_id"], {"city": "Mandya"}, desk, row_version=1)
    assert err.value.code == "stale_row_version" and err.value.extra["current_row_version"] == 2


def test_a_correction_is_logged_append_only_and_re_keys_the_patient(hospital_mode, desk):
    patient, body = _registered(desk)
    pid = patient["patient_id"]
    new_phone = _phone()
    out = _correct(pid, {"phone": new_phone, "city": "Hassan", "pin_code": "573201"}, desk)
    assert out["patient"]["row_version"] == 2 and out["patient"]["phone"] == new_phone
    assert "Hassan" in out["patient"]["address"]
    changes = reg.change_history(pid)["changes"]
    assert {c["field"] for c in changes} == {"phone", "city", "pin_code"}
    assert all(c["reason"] == "Patient showed their card" for c in changes)
    with db_session() as db:
        key = db.execute("SELECT phone_key FROM patient_match_keys WHERE patient_id = ?",
                         (pid,)).fetchone()[0]
        with pytest.raises(Exception):
            db.execute("DELETE FROM patient_identity_changes WHERE patient_id = ?", (pid,))
    assert key == new_phone
    rows = _audit_rows("patient_demographics_corrected", pid)
    assert rows and json.loads(rows[-1]["metadata_json"])["fields"] == ["city", "phone", "pin_code"]


def test_the_uhid_is_corrected_by_the_administrator_only(hospital_mode, desk, admin):
    patient, _ = _registered(desk)
    pid = patient["patient_id"]
    with pytest.raises(reg.RegistrationForbidden) as err:
        _correct(pid, {"uhid": _uhid()}, desk, fields=True, uhid=False)
    assert err.value.code == "uhid_correction_admin_only"
    with pytest.raises(reg.RegistrationForbidden):
        _correct(pid, {"city": "Udupi"}, admin, fields=False, uhid=True)
    new = _uhid()
    out = _correct(pid, {"uhid": new}, admin, fields=False, uhid=True)
    assert out["patient"]["uhid"] == new
    assert _audit_rows("patient_uhid_corrected", pid)


def test_a_uhid_correction_cannot_take_another_patients_number(hospital_mode, desk, admin):
    first, _ = _registered(desk)
    second, _ = _registered(desk)
    with pytest.raises(reg.RegistrationConflict) as err:
        _correct(second["patient_id"], {"uhid": first["uhid"].lower()}, admin, fields=False, uhid=True)
    assert err.value.code == "uhid_in_use"


def test_a_stated_age_cannot_sit_beside_a_date_of_birth(hospital_mode, desk):
    patient, _ = _registered(desk)
    with pytest.raises(reg.RegistrationError) as err:
        _correct(patient["patient_id"], {"age_years": 40}, desk)
    assert err.value.code == "age_with_date_of_birth"
    out = _correct(patient["patient_id"], {"date_of_birth": None, "age_years": 40}, desk)
    assert out["patient"]["date_of_birth"] is None and out["patient"]["age_stated"] is True


def _dob_and_age(pid):
    with db_session() as db:
        row = db.execute("SELECT p.date_of_birth, p.age, d.age_recorded_on, d.row_version "
                         "FROM patients p LEFT JOIN patient_demographics d "
                         "ON d.patient_id = p.id WHERE p.id = ?", (pid,)).fetchone()
    return dict(row)


@pytest.mark.parametrize("changes", [
    {"date_of_birth": None},
    {"date_of_birth": None, "age_years": None},
    {"date_of_birth": "", "age_years": ""},
])
def test_removing_a_date_of_birth_without_a_stated_age_is_refused(hospital_mode, desk, changes):
    patient, _ = _registered(desk, date_of_birth="1981-03-04")
    pid = patient["patient_id"]
    before = _dob_and_age(pid)
    with pytest.raises(reg.RegistrationError) as err:
        _correct(pid, changes, desk)
    assert err.value.code == "age_or_date_of_birth_required" and err.value.status == 422
    assert _dob_and_age(pid) == before
    assert reg.change_history(pid)["changes"] == []


def test_removing_a_date_of_birth_records_the_stated_age_with_todays_date(hospital_mode, desk):
    patient, _ = _registered(desk, date_of_birth="1981-03-04")
    pid = patient["patient_id"]
    derived = patient["age"]
    out = _correct(pid, {"date_of_birth": None, "age_years": derived}, desk)["patient"]
    assert out["date_of_birth"] is None and out["age"] == derived
    assert out["age_stated"] is True
    assert out["age_recorded_on"] == hospital_today().isoformat()
    assert out["discharge_link_possible"] is False
    history = {c["field"]: c for c in reg.change_history(pid)["changes"]}
    assert set(history) == {"date_of_birth", "age_years"}
    assert history["date_of_birth"]["old_value"] == "1981-03-04"
    assert history["date_of_birth"]["new_value"] is None
    # The age worked out from the removed date of birth was never a stated age.
    assert history["age_years"]["old_value"] is None
    assert history["age_years"]["new_value"] == str(derived)


@pytest.mark.parametrize("changes", [
    {"age_years": None},
    {"age_years": ""},
    {"age_years": None, "date_of_birth": None},
])
def test_a_stated_age_cannot_be_cleared_without_a_date_of_birth(hospital_mode, desk, changes):
    patient, _ = _registered(desk, date_of_birth=None, age_years=40)
    pid = patient["patient_id"]
    before = _dob_and_age(pid)
    with pytest.raises(reg.RegistrationError) as err:
        _correct(pid, changes, desk)
    assert err.value.code == "age_or_date_of_birth_required"
    assert _dob_and_age(pid) == before
    assert before["age"] == 40 and before["date_of_birth"] is None


@pytest.mark.parametrize("changes", [
    {"date_of_birth": "1986-05-17"},
    {"date_of_birth": "1986-05-17", "age_years": None},
])
def test_a_date_of_birth_may_replace_a_stated_age(hospital_mode, desk, changes):
    patient, _ = _registered(desk, date_of_birth=None, age_years=40)
    out = _correct(patient["patient_id"], changes, desk)["patient"]
    assert out["date_of_birth"] == "1986-05-17" and out["age_stated"] is False
    assert out["age_recorded_on"] is None
    assert {c["field"] for c in reg.change_history(patient["patient_id"])["changes"]} \
        == {"date_of_birth"}


def test_a_new_stated_age_is_recorded_with_todays_date(hospital_mode, desk):
    patient, _ = _registered(desk, date_of_birth=None, age_years=40)
    with db_session() as db:
        db.execute("UPDATE patient_demographics SET age_recorded_on = '2020-01-01' "
                   "WHERE patient_id = ?", (patient["patient_id"],))
    out = _correct(patient["patient_id"], {"age_years": 41}, desk)["patient"]
    assert out["age"] == 41 and out["age_stated"] is True
    assert out["age_recorded_on"] == hospital_today().isoformat()


def test_a_record_without_either_can_still_have_other_details_corrected(hospital_mode, desk):
    pid = f"PAT-ADM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, age, sex, status, created_at) "
                   "VALUES (?, ?, ?, NULL, 'F', 'discharged', CURRENT_TIMESTAMP)",
                   (pid, _name(), _uhid()))
    out = _correct(pid, {"occupation": "Weaver"}, desk, row_version=0)["patient"]
    assert out["demographics"]["occupation"] == "Weaver"
    with pytest.raises(reg.RegistrationError) as err:
        _correct(pid, {"date_of_birth": None}, desk, row_version=1)
    assert err.value.code == "age_or_date_of_birth_required"
    fixed = _correct(pid, {"age_years": 63}, desk, row_version=1)["patient"]
    assert fixed["age"] == 63 and fixed["age_recorded_on"] == hospital_today().isoformat()


def test_a_patient_from_the_admission_door_gets_demographics_on_first_correction(hospital_mode, desk):
    pid = f"PAT-ADM{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, age, sex, status, created_at) "
                   "VALUES (?, ?, ?, 50, 'M', 'discharged', CURRENT_TIMESTAMP)",
                   (pid, _name(), _uhid()))
    record = reg.get_registration(pid)
    assert record["row_version"] == 0
    out = _correct(pid, {"occupation": "Farmer"}, desk, row_version=0)
    assert out["patient"]["row_version"] == 1
    assert out["patient"]["demographics"]["occupation"] == "Farmer"


def test_search_returns_minimal_fields_only(hospital_mode, desk):
    patient, body = _registered(desk, address_line="44 Secret Street", email="hidden@example.com")
    found = reg.search_index(body["name"].split()[0])
    hit = [c for c in found["results"] if c["patient_id"] == patient["patient_id"]]
    assert hit and set(hit[0]) == set(reg.INDEX_CARD_FIELDS)
    text = json.dumps(found)
    assert body["date_of_birth"] not in text and "Secret Street" not in text
    assert body["phone"] not in text and hit[0]["phone_last4"] == body["phone"][-4:]


def test_search_needs_three_characters_and_returns_at_most_twenty(hospital_mode, desk):
    with pytest.raises(reg.RegistrationError) as err:
        reg.search_index("ab")
    assert err.value.code == "query_too_short"
    surname = _name().split()[0]
    # Distinct dates of birth, so a shared surname plus a chance birthday
    # collision (a possible duplicate, 55 points) cannot stop the setup.
    for i in range(reg.SEARCH_MAX_RESULTS + 2):
        reg.register_patient(_payload(name=f"{_name().split()[0]} {surname}",
                                      date_of_birth=f"{1950 + i}-03-15"), user=desk)
    found = reg.search_index(surname)
    assert len(found["results"]) == reg.SEARCH_MAX_RESULTS and found["truncated"] is True


def test_search_by_phone_and_uhid(hospital_mode, desk):
    patient, body = _registered(desk)
    by_phone = reg.search_index("+91 " + body["phone"])
    assert patient["patient_id"] in [c["patient_id"] for c in by_phone["results"]]
    by_uhid = reg.search_index(patient["uhid"].lower())
    assert by_uhid["results"][0]["patient_id"] == patient["patient_id"]


def test_the_card_carries_no_date_of_birth(hospital_mode, desk):
    patient, body = _registered(desk)
    card = reg.uhid_card(patient["patient_id"])
    assert card["uhid"] == patient["uhid"] and card["registered_on"] == hospital_today().isoformat()
    assert body["date_of_birth"] not in json.dumps(card)


def test_a_stated_age_ages_on_the_card_over_time(hospital_mode, desk):
    patient, _ = _registered(desk, date_of_birth=None, age_years=30)
    later = hospital_today() + timedelta(days=366 * 2)
    with db_session() as db:
        card = reg.index_cards_on(db, [patient["patient_id"]], today=later)[patient["patient_id"]]
    assert card["age"] == 32 and card["age_stated"] is True
