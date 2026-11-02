"""Import mapping contracts and legacy UTC storage survive API cleanup."""
from datetime import datetime, timezone
from io import BytesIO
import json
import uuid

from fastapi import BackgroundTasks, HTTPException, Request, Response
import pytest
from starlette.datastructures import UploadFile

from database import db_session
from routes import admin_routes, admission_routes, roster_import_routes
from services import emar_service, helix_service


@pytest.mark.parametrize("parse", [admin_routes._parse_mapping, roster_import_routes._parse_mapping], ids=["history", "roster"])
@pytest.mark.parametrize("raw,expected", [
    ("", {}),
    ('{"name":"Patient Name","ward":"कक्ष","bed":"","age":null}', {"name": "Patient Name", "ward": "कक्ष"}),
    ('{"name":"0","ward":"  exact header  "}', {"name": "0", "ward": "  exact header  "}),
])
def test_both_importers_preserve_mapping_form_values(parse, raw, expected):
    assert parse(raw) == expected


@pytest.mark.parametrize("parse", [admin_routes._parse_mapping, roster_import_routes._parse_mapping], ids=["history", "roster"])
@pytest.mark.parametrize("raw,message", [
    ('{"name":', "mapping must be valid JSON:"),
    ("[]", "mapping must be a JSON object"),
    ("null", "mapping must be a JSON object"),
    ('{"age":0}', "mapping keys and values must both be strings"),
    ('{"ward":false}', "mapping keys and values must both be strings"),
    ('{"name":["Patient"]}', "mapping keys and values must both be strings"),
])
def test_both_importers_reject_ambiguous_mapping_types_with_422(parse, raw, message):
    with pytest.raises(HTTPException) as exc:
        parse(raw)
    assert exc.value.status_code == 422 and exc.value.detail.startswith(message)


async def test_roster_reads_only_its_existing_csv_ceiling(monkeypatch):
    monkeypatch.setattr(roster_import_routes.roster_import, "MAX_CSV_BYTES", 16)
    source = BytesIO(b"x" * 100)
    upload = UploadFile(source, filename="synthetic.csv")
    try:
        with pytest.raises(HTTPException) as exc:
            await roster_import_routes._read_upload(upload)
        assert exc.value.status_code == 413
        assert source.tell() == 17
    finally:
        await upload.close()


def test_admission_model_dump_preserves_nested_values_and_optional_fields(monkeypatch):
    payload = admission_routes.AdmissionRequest(
        name="Synthetic admission", age=42, sex="F", admission_source="er", admission_type="inpatient",
        department="  medicine ", ward="  Synthetic ward  ", bed="  7  ", attending_doctor_id="doctor-synthetic",
        chief_complaint="Synthetic complaint", comorbidities=["Recorded history"],
        vitals={"heart_rate": 80, "source": "staff"}, duplicate_review={"decision": "admit_new", "candidate_ids": [], "reason": "Reviewed"},
    )
    expected = payload.model_dump()
    expected.update(department="General Medicine", ward="Synthetic ward", bed="7")
    captured = []

    def deprecated(*args, **kwargs):
        raise AssertionError("Pydantic's deprecated dict API must not be used")

    def admit(data, user, **kwargs):
        captured.append((data, user, kwargs))
        return {"patient_id": "synthetic-patient", "admission_id": "synthetic-admission"}

    monkeypatch.setattr(admission_routes.AdmissionRequest, "dict", deprecated)
    monkeypatch.setattr(admission_routes, "resolve_admission_department", lambda _: "General Medicine")
    monkeypatch.setattr(admission_routes, "require_listed_attending", lambda value: value)
    monkeypatch.setattr(admission_routes, "admit_patient", admit)
    request = Request({"type": "http", "method": "POST", "path": "/api/admissions/admit", "headers": []})
    result = admission_routes.process_admission(payload, BackgroundTasks(), request, Response(), "retry-test", {"id": "doctor-synthetic"})
    assert result["patient_id"] == "synthetic-patient"
    assert captured[0][0] == expected
    assert captured[0][2]["idempotency_key"] == "retry-test"


class FrozenUTC(datetime):
    @classmethod
    def now(cls, tz=None):
        assert tz is timezone.utc, "UTC must be explicit; never use the appliance wall clock"
        return cls(2026, 10, 11, 6, 7, 8, 123456, tzinfo=timezone.utc)

    @classmethod
    def utcnow(cls):
        raise AssertionError("Deprecated utcnow must not be used")


def test_helix_review_and_receipt_keep_legacy_offsetless_utc_storage(isolated_module_database, monkeypatch):
    monkeypatch.setattr(helix_service, "datetime", FrozenUTC)
    tag = uuid.uuid4().hex
    patient, actor, drug, order = (prefix + tag for prefix in ("UTC-P-", "UTC-U-", "UTC-M-", "UTC-O-"))
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,password_hash) VALUES (?,'Synthetic nurse','nurse','disabled')", (actor,))
        db.execute("INSERT INTO patients(id,name,mrn) VALUES (?,'Synthetic timestamp',?)", (patient, patient))
        db.execute("INSERT INTO helix_medication_master(id,name) VALUES (?,'Synthetic timestamp medication')", (drug,))
        db.execute("INSERT INTO helix_patient_med_orders(id,patient_id,medication_id,prescribed_by,status) VALUES (?,?,?,?,'pending')", (order, patient, drug, actor))
    reviewed = helix_service.transition_order_status(order, "reviewing", actor)
    expected = "2026-10-11T06:07:08.123456"
    assert reviewed["order"]["reviewed_at"] == expected
    with db_session() as db:
        db.execute("UPDATE helix_patient_med_orders SET status='dispensed' WHERE id=?", (order,))
    received = helix_service.receive_order(order, actor)
    assert received["order"]["received_at"] == expected
    assert json.loads(received["transition"]["payload"])["received_at"] == expected
    assert datetime.fromisoformat(received["order"]["received_at"]).tzinfo is None


def test_emar_duplicate_arithmetic_keeps_legacy_naive_utc(isolated_module_database, monkeypatch):
    monkeypatch.setattr(emar_service, "datetime", FrozenUTC)
    tag = uuid.uuid4().hex
    patient, actor = "UTC-P-" + tag, "UTC-N-" + tag
    with db_session() as db:
        db.execute("INSERT INTO users(id,name,role,password_hash) VALUES (?,'Synthetic nurse','nurse','disabled')", (actor,))
        db.execute("INSERT INTO patients(id,name,mrn) VALUES (?,'Synthetic timestamp',?)", (patient, patient))
        med = db.execute("""INSERT INTO medications(patient_id,drug_name,dosage,route,frequency,status,prescribed_by,source)
            VALUES (?,'Paracetamol','500mg','Oral','QDS','active',?,'clinician')""", (patient, actor)).lastrowid
        instants = ["2026-10-11 04:07:08.123456", "2026-10-11T04:07:08.123456Z", "2026-10-11T04:07:08.123456+00:00",
                    "2026-10-11T09:37:08.123456+05:30", "2026-10-10T21:07:08.123456-07:00"]
        for stamp in [*instants, "2026-10-11T06:07:08.123456+05:30"]:
            db.execute("""INSERT INTO medication_administrations(medication_id,patient_id,administered_by,status,dose_given,route_given,administered_at)
                VALUES (?,?,?,'given','500mg','Oral',?)""", (med, patient, actor, stamp))
        recent = emar_service._read_recent_administrations(db, patient, lookback_hours=4)
        stored = [r[0] for r in db.execute("SELECT administered_at FROM medication_administrations WHERE patient_id=?", (patient,))]
    assert set(instants) <= set(stored)  # acceptance uses persisted input spellings
    assert len(recent) == 5 and all(r["_when"] == datetime(2026, 10, 11, 4, 7, 8, 123456) for r in recent)
    evaluated = []
    duplicate = emar_service.drug_safety_service.evaluate_duplicate

    def capture(drug, ingredients, frequency, prior_doses, floor):
        evaluated.extend(prior_doses)
        return duplicate(drug, ingredients, frequency, prior_doses, floor)

    monkeypatch.setattr(emar_service.drug_safety_service, "evaluate_duplicate", capture)
    result = emar_service.record_administration(med, actor, status="given")
    assert result["dose_check"]["unavailable"] is False
    assert len(evaluated) == 5 and all(r["hours_ago"] == pytest.approx(2) for r in evaluated)
    assert result["dose_check"]["duplicate"] is not None


@pytest.mark.parametrize("value", [
    "2026-10-11 04:07:08.123456", "2026-10-11T04:07:08.123456Z", "2026-10-11T09:37:08.123456+05:30",
    "2026-10-10T21:07:08.123456-07:00", datetime(2026, 10, 11, 4, 7, 8, 123456),
    datetime.fromisoformat("2026-10-11T09:37:08.123456+05:30"), datetime.fromisoformat("2026-10-10T21:07:08.123456-07:00"),
])
def test_emar_explicit_offsets_and_legacy_timestamps_describe_the_same_instant(value):
    assert emar_service._parse_ts(value) == datetime(2026, 10, 11, 4, 7, 8, 123456)
