"""Orb Ward Record rounds file: the edges the worked scenario does not reach
(services/ward_record_service.py).

* A set in the overlap between one round's late window and the next round's
  on time window goes to the next round, unless another set is there for it.
* A set earlier than half the grace window before the due time is not on time.
* A patient moved onto the ward is due one interval after the move, not after
  the admission, and rounds on the ward they came from are not counted.

Each test makes its own wards and works in July 2025, which no other module
writes into. A 4 hour interval and a 30 minute grace window throughout, so the
on time window of a round due at 16:00 opens at 15:45.
"""
import itertools
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import db_session  # noqa: E402
from services import ward_record_service as svc  # noqa: E402
from services.hospital_time import hospital_timezone  # noqa: E402

_SEQ = itertools.count(1)
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _local(day: int, hh: int, mm: int = 0) -> str:
    tz = hospital_timezone()
    return datetime(2025, 7, day, hh, mm, tzinfo=tz).astimezone(timezone.utc).isoformat()


def _name(prefix: str) -> str:
    return f"{prefix}-{next(_SEQ):03d}-{os.getpid()}"


def _patient(ward: str, *, admitted: str, discharged: str) -> str:
    pid = _name("WRE-PT")
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES ('WRE-ADM', 'Edge Admin', 'admin', NULL, 'x')")
        db.execute(
            "INSERT INTO patients (id, name, mrn, ward, bed, admission_date, status) "
            "VALUES (?, 'Edge Fixture', ?, ?, '3', ?, 'discharged')",
            (pid, f"UHID-{pid}", ward, admitted))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, ward, bed, status, "
            "created_at, admitted_at, discharged_at) "
            "VALUES (?, ?, 'WRE-ADM', ?, '3', 'discharged', ?, ?, ?)",
            (f"ADM-{pid}", pid, ward, admitted, admitted, discharged))
    return pid


def _set(pid: str, at: str) -> None:
    with db_session() as db:
        db.execute(
            "INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp, "
            "diastolic_bp, spo2, temperature, respiratory_rate, consciousness, "
            "on_oxygen, source) VALUES (?, ?, 80, 120, 80, 97, 37.0, 16, 'alert', 0, "
            "'clerk_transcribed')", (pid, at))


def _setting(ward: str) -> None:
    svc.set_setting(ward, interval_minutes=240, grace_minutes=30, set_by="NS")


def _totals(ward: str) -> dict:
    data = svc.compute_rounds(ward, "2025-07-10", "2025-07-11", now=NOW)
    return data["totals"]


@pytest.fixture()
def ward():
    name = _name("WRE-WARD")
    return name


def test_a_set_in_the_overlap_is_the_next_round_on_time(ward):
    # Arrived 08:00, left 18:00: rounds due 12:00 and 16:00.
    pid = _patient(ward, admitted=_local(10, 8), discharged=_local(10, 18))
    _setting(ward)
    _set(pid, _local(10, 15, 50))  # no round at 12:00; the 16:00 one at 15:50
    t = _totals(ward)
    assert (t["due"], t["done"], t["late"], t["missed"]) == (2, 1, 0, 1)


def test_a_set_in_the_overlap_is_late_when_the_next_round_has_its_own(ward):
    pid = _patient(ward, admitted=_local(10, 8), discharged=_local(10, 18))
    _setting(ward)
    _set(pid, _local(10, 15, 50))  # the 12:00 round, late
    _set(pid, _local(10, 16, 5))   # the 16:00 round, on time
    t = _totals(ward)
    assert (t["due"], t["done"], t["late"], t["missed"]) == (2, 1, 1, 0)


def test_a_set_before_half_the_grace_window_is_not_on_time(ward):
    # Arrived 08:00, left 13:00: one round due at 12:00, on time from 11:45.
    pid = _patient(ward, admitted=_local(10, 8), discharged=_local(10, 13))
    _setting(ward)
    _set(pid, _local(10, 11, 40))
    t = _totals(ward)
    assert (t["due"], t["done"], t["late"], t["missed"]) == (1, 0, 0, 1)


def test_a_move_onto_the_ward_starts_the_interval_there(ward):
    other = _name("WRE-FROM")
    # Admitted to another ward at 08:00, moved onto this one at 10:00, left
    # at 19:00. On this ward rounds fall due at 14:00 and 18:00, never 12:00.
    pid = _patient(other, admitted=_local(10, 8), discharged=_local(10, 19))
    _setting(other)  # while a patient row still names that ward
    with db_session() as db:
        db.execute(
            "INSERT INTO patient_movements (id, patient_id, admission_id, movement_type, "
            "from_ward, from_bed, to_ward, to_bed, reason, moved_at) "
            "VALUES (?, ?, ?, 'transfer', ?, '3', ?, '9', 'step down', ?)",
            (_name("MOVE"), pid, f"ADM-{pid}", other, ward, _local(10, 10)))
        # admissions.ward follows the patient, as the transfer lane writes it.
        db.execute("UPDATE admissions SET ward = ?, bed = '9' WHERE id = ?",
                   (ward, f"ADM-{pid}"))
        db.execute("UPDATE patients SET ward = ?, bed = '9' WHERE id = ?", (ward, pid))
    _setting(ward)
    _set(pid, _local(10, 12, 0))   # the other ward's 12:00 round
    _set(pid, _local(10, 14, 10))  # this ward's 14:00 round, on time
    t = _totals(ward)
    assert (t["due"], t["done"], t["late"], t["missed"]) == (2, 1, 0, 1)
    data = svc.compute_rounds(ward, "2025-07-10", "2025-07-11", now=NOW)
    assert {r["bed"] for r in data["rows"]} == {"9"}
    # The other ward sees only its own spell: arrived 08:00, left 10:00, so no
    # round fell due there.
    assert _totals(other)["due"] == 0


def test_the_file_says_when_an_empty_consciousness_or_oxygen_box_cannot_be_blank(ward):
    pid = _patient(ward, admitted=_local(10, 8), discharged=_local(10, 13))
    _setting(ward)
    _set(pid, _local(10, 12, 0))
    data = svc.compute_rounds(ward, "2025-07-10", "2025-07-11", now=NOW)
    # Asked of the entry code: today an empty box is stored as alert and air.
    from services.nurse_shift_service import _clean_obs
    filled = _clean_obs({"heart_rate": 80, "consciousness": "alert",
                         "on_oxygen": False})["consciousness"] is not None
    assert data["empty_acvpu_oxygen_stored_as_alert_air"] is filled

    def labels(flag: bool) -> list:
        return [label for label, _ in svc.header_lines(
            dict(data, empty_acvpu_oxygen_stored_as_alert_air=flag),
            reference="WR-T", generated_at=NOW.isoformat(), generated_by="T")]

    assert "Consciousness and air or oxygen" in labels(True)
    assert "Consciousness and air or oxygen" not in labels(False)


def test_patients_are_listed_in_the_order_they_arrived(ward):
    # Eleven patients, one an hour from 01:00, each with one round due.
    for n in range(11):
        pid = _patient(ward, admitted=_local(10, 1 + n), discharged=_local(10, 6 + n))
        _set(pid, _local(10, 5 + n))
    _setting(ward)
    data = svc.compute_rounds(ward, "2025-07-10", "2025-07-11", now=NOW)
    night = [r["patient"] for r in data["rows"] if r["shift"] == "23:00 to 07:00"]
    assert night == ["Patient 1", "Patient 2"]
    day = [r["patient"] for r in data["rows"] if r["shift"] == "07:00 to 15:00"]
    assert day == [f"Patient {n}" for n in range(3, 11)]  # 10 before 3 as text
