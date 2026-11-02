"""Typed, local staff documentation. No model, device, medication or ADT writes.

Every operation checks stored identity, permission and chart/facility scope on
its connection. Clinical mutations, version history and the fail-closed audit
entry commit together. Record scope is applied before worklist pagination.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Annotated, Literal
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt, StringConstraints, model_validator

import config
from database import db_session
from permissions import department_scope, has_permission, patient_in_scope, scope_patient_ids_sql
from services import facility_service
from services.audit_service import log_audit_event
from services.surgery_scope import surgery_in_scope, surgical_list_filter

Short = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strict=True, max_length=8000)]
Stamp = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=40)]


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def timestamps(self):
        for name, value in self.__dict__.items():
            if (name.endswith("_at") or name == "at") and value:
                try:
                    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    raise ValueError(f"{name} must be an ISO date and time")
                if dt.tzinfo is None or dt.utcoffset() is None:
                    raise ValueError(f"{name} must include a timezone")
        return self


class Observation(Document):
    at: Stamp
    blood_pressure: Annotated[str, StringConstraints(strict=True, max_length=30)] | None = ""
    pulse: FiniteFloat | None = Field(None, ge=0, le=1000)
    respiratory_rate: FiniteFloat | None = Field(None, ge=0, le=1000)
    spo2: FiniteFloat | None = Field(None, ge=0, le=100)
    temperature_c: FiniteFloat | None = Field(None, ge=-100, le=100)
    note: Text = ""


class RisData(Document):
    modality: Literal["X-ray", "CT", "MRI", "Ultrasound", "Other"]
    body_site: Short
    indication: Text = ""
    priority: Literal["routine", "urgent", "stat"] = "routine"
    scheduled_at: Stamp | None = None
    arrived_at: Stamp | None = None
    performed_at: Stamp | None = None
    performer: Short | None = None
    findings: Text = ""
    impression: Text = ""
    order_id: Short | None = None
    accession: Short | None = None


class CareTask(Document):
    id: Short
    description: Short
    owner: Short
    due_at: Stamp | None = None
    state: Literal["planned", "done", "not_done"] = "planned"
    completed_at: Stamp | None = None
    evaluation: Text = ""

    @model_validator(mode="after")
    def completion(self):
        if self.state != "planned" and (not self.completed_at or not self.evaluation.strip()):
            raise ValueError("A completed or not-done intervention needs its time and evaluation")
        if self.state == "planned" and self.completed_at:
            raise ValueError("A planned intervention cannot have a completion time")
        return self


class CareData(Document):
    problem: Short
    goal: Text
    review_at: Stamp | None = None
    tasks: list[CareTask] = Field(default_factory=list, max_length=40)
    evaluation: Text = ""
    closure_reason: Text = ""

    @model_validator(mode="after")
    def unique_tasks(self):
        if len({t.id for t in self.tasks}) != len(self.tasks):
            raise ValueError("Intervention ids must be unique")
        if not self.goal.strip():
            raise ValueError("A care goal is required")
        return self


class EmergencyData(Document):
    arrival_at: Stamp
    complaint: Text
    triage_category: Short | None = None
    triaged_at: Stamp | None = None
    triage_note: Text = ""
    seen_at: Stamp | None = None
    clinician: Short | None = None
    assessment: Text = ""
    observations: list[Observation] = Field(default_factory=list, max_length=100)
    disposition: Literal["admission_requested", "discharged_from_ed", "referred", "left", "other"] | None = None
    disposition_at: Stamp | None = None
    disposition_note: Text = ""

    @model_validator(mode="after")
    def complaint_required(self):
        if not self.complaint.strip():
            raise ValueError("A presenting complaint is required")
        return self


class PerioperativeData(Document):
    record_type: Literal["anaesthesia", "pacu", "implant"]
    started_at: Stamp | None = None
    ended_at: Stamp | None = None
    technique: Short | None = None
    assessment: Text = ""
    airway: Text = ""
    observations: list[Observation] = Field(default_factory=list, max_length=100)
    handover: Text = ""
    device_name: Short | None = None
    manufacturer: Short | None = None
    model: Short | None = None
    lot: Short | None = None
    serial: Short | None = None
    expiry_date: Annotated[str, StringConstraints(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None
    site: Short | None = None
    implanted_at: Stamp | None = None
    notes: Text = ""

    @model_validator(mode="after")
    def implant(self):
        if self.expiry_date:
            from datetime import date
            date.fromisoformat(self.expiry_date)
        if self.record_type == "implant" and not (self.device_name and self.site and (self.lot or self.serial)):
            raise ValueError("An implant record needs the device, site and lot or serial number")
        return self


class DialysisPrescription(Document):
    modality: Short
    duration_minutes: StrictInt = Field(ge=1, le=2880)
    target_fluid_ml: FiniteFloat | None = Field(None, ge=0, le=100000)
    blood_flow_ml_min: FiniteFloat | None = Field(None, ge=0, le=10000)
    dialysate: Short | None = None
    anticoagulation: Text = ""
    instructions: Text = ""


class DialysisData(Document):
    scheduled_at: Stamp | None = None
    started_at: Stamp | None = None
    ended_at: Stamp | None = None
    indication: Text = ""
    access: Short
    prescription: DialysisPrescription | None = None
    actual_duration_minutes: StrictInt | None = Field(None, ge=0, le=2880)
    actual_fluid_ml: FiniteFloat | None = Field(None, ge=0, le=100000)
    observations: list[Observation] = Field(default_factory=list, max_length=100)
    complications: Text = ""
    handover: Text = ""


class CreateRecord(Document):
    patient_id: Short
    admission_id: Short | None = None
    surgery_id: Short | None = None
    title: Short
    data: dict


class UpdateRecord(Document):
    version: StrictInt = Field(ge=1)
    title: Short
    state: Short
    data: dict
    reason: Annotated[str, StringConstraints(strict=True, max_length=1000)] = ""


class VersionIn(Document):
    version: StrictInt = Field(ge=1)


class AddendumIn(VersionIn):
    text: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=8000)]
    reason: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1000)]


@dataclass(frozen=True)
class Domain:
    table: str
    flag: str
    read: str
    write: str
    sign: str | None
    model: type[Document]
    states: tuple[str, ...]
    initial: str
    terminal: frozenset[str]


DOMAINS = {
    "radiology": Domain("ris_studies", "RADIOLOGY_ENABLED", "ris.read", "ris.write", "ris.sign", RisData,
                        ("requested", "scheduled", "arrived", "performed", "reported", "signed", "cancelled"), "requested", frozenset({"signed", "cancelled"})),
    "care-plans": Domain("care_plans", "CARE_PLANS_ENABLED", "care_plan.read", "care_plan.write", None, CareData,
                         ("draft", "active", "completed", "cancelled"), "draft", frozenset({"completed", "cancelled"})),
    "emergency": Domain("emergency_visits", "EMERGENCY_ENABLED", "emergency_record.read", "emergency_record.write", None, EmergencyData,
                        ("arrived", "triaged", "seen", "record_closed", "cancelled"), "arrived", frozenset({"record_closed", "cancelled"})),
    "perioperative": Domain("perioperative_records", "PERIOPERATIVE_ENABLED", "perioperative_record.read", "perioperative_record.write", "perioperative_record.sign", PerioperativeData,
                            ("draft", "in_progress", "completed", "signed", "cancelled"), "draft", frozenset({"signed", "cancelled"})),
    "dialysis": Domain("dialysis_sessions", "DIALYSIS_ENABLED", "dialysis_record.read", "dialysis_record.write", "dialysis_record.prescribe", DialysisData,
                       ("planned", "in_progress", "completed", "signed", "cancelled"), "planned", frozenset({"signed", "cancelled"})),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _user(db, actor, permission):
    row = db.execute("SELECT id, name, role, department FROM users WHERE id = ? AND COALESCE(is_active,1)=1", (actor.get("id"),)).fetchone()
    if row is None or not has_permission(row["role"], permission):
        raise HTTPException(403, "Not authorized for this documentation")
    if facility_service.multi_facility_data(db=db):
        raise HTTPException(503, "Local documentation remains unavailable while the multi-facility fence is active")
    return dict(row)


def require_module(domain):
    if not getattr(config, DOMAINS[domain].flag, False):
        raise HTTPException(404, detail={"error": "module_disabled", "message": "This documentation module is switched off"})


def _facilities(db, user):
    return facility_service.facility_scope_for(user, db=db)


def _registered_ed(db, patient, user, *, intake=True):
    if not department_scope(user) or (intake and not has_permission(user["role"], "registration.read")):
        return False
    facilities = _facilities(db, user)
    home = facility_service.user_facility_id(user, db=db)
    return (patient["status"] == "registered" and patient["registered_facility_id"] is not None
            and home is not None and patient["registered_facility_id"] == home
            and (facilities is None or patient["registered_facility_id"] in facilities)
            and db.execute("SELECT 1 FROM admissions WHERE patient_id=? AND status='active'", (patient["id"],)).fetchone() is None)


def _allowed(db, domain, patient, user, record=None, surgery_id=None):
    facilities = _facilities(db, user)
    if domain == "perioperative":
        case_id = record["surgery_id"] if record else surgery_id
        return bool(case_id and surgery_in_scope(db, user, case_id) and patient_in_scope(db, patient["id"], None, facilities=facilities))
    if patient_in_scope(db, patient["id"], department_scope(user), facilities=facilities):
        return True
    # A narrow documentation arm, never passed to inpatient chart helpers.
    if domain == "emergency" and _registered_ed(db, patient, user, intake=record is None):
        return record is None or record["department"] == department_scope(user)
    return False


def _patient(db, patient_id):
    row = db.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Record not found")
    return row


def _record(db, domain, record_id, user):
    row = db.execute(f"SELECT * FROM {DOMAINS[domain].table} WHERE id=?", (record_id,)).fetchone()
    if row is None or not _allowed(db, domain, _patient(db, row["patient_id"]), user, record=row):
        raise HTTPException(404, "Record not found")
    return row


def _scope(db, domain, user):
    facilities = _facilities(db, user)
    ids, params = scope_patient_ids_sql(None if domain == "perioperative" else department_scope(user), facilities=facilities)
    sql = f"r.patient_id IN ({ids})"
    if domain == "perioperative":
        surgical_sql, surgical_params = surgical_list_filter(user)
        return sql + surgical_sql, (*params, *surgical_params)
    if domain == "emergency" and department_scope(user):
        if facilities is None:
            facility_sql, facility_params = "p.registered_facility_id IS NOT NULL", ()
        elif facilities:
            facility_sql = "p.registered_facility_id IN (" + ",".join("?" for _ in facilities) + ")"
            facility_params = tuple(sorted(facilities))
        else:
            facility_sql, facility_params = "0", ()
        sql = f"({sql} OR (r.department=? AND p.status='registered' AND p.registered_facility_id=? AND {facility_sql} AND NOT EXISTS(SELECT 1 FROM admissions a WHERE a.patient_id=p.id AND a.status='active')))"
        params = (*params, department_scope(user), facility_service.user_facility_id(user, db=db), *facility_params)
    return sql, params


def _validate(domain, data):
    from pydantic import ValidationError
    try:
        parsed = DOMAINS[domain].model.model_validate(data)
    except ValidationError as exc:
        # Do not echo the submitted clinical payload through validation errors.
        raise HTTPException(422, detail=[{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors(include_input=False, include_context=False)])
    payload = parsed.model_dump(mode="json")
    if len(_json(payload).encode()) > 64000:
        raise HTTPException(422, "Documentation is too large")
    for start, end in (("started_at", "ended_at"), ("arrival_at", "triaged_at"), ("triaged_at", "seen_at"), ("seen_at", "disposition_at"), ("arrived_at", "performed_at")):
        if payload.get(start) and payload.get(end):
            if datetime.fromisoformat(payload[end].replace("Z", "+00:00")) < datetime.fromisoformat(payload[start].replace("Z", "+00:00")):
                raise HTTPException(422, f"{end} cannot precede {start}")
    return payload


def _state_data(domain, state, data):
    needed = []
    if domain == "radiology":
        if state in {"scheduled", "arrived", "performed", "reported", "signed"}: needed.append("scheduled_at")
        if state in {"arrived", "performed", "reported", "signed"}: needed.append("arrived_at")
        if state in {"performed", "reported", "signed"}: needed.append("performed_at")
        if state in {"reported", "signed"}: needed.extend(("findings", "impression"))
    elif domain == "care-plans" and state == "completed":
        needed.extend(("evaluation", "closure_reason"))
        if any(t["state"] == "planned" for t in data["tasks"]):
            raise HTTPException(422, "Record an outcome for every intervention before completing this plan")
    elif domain == "emergency":
        if state in {"triaged", "seen", "record_closed"}: needed.extend(("triage_category", "triaged_at"))
        if state in {"seen", "record_closed"}: needed.extend(("seen_at", "clinician"))
        if state == "record_closed": needed.extend(("disposition", "disposition_at", "disposition_note"))
    elif domain == "perioperative":
        if data["record_type"] == "implant" and state in {"completed", "signed"}: needed.append("implanted_at")
        elif state in {"in_progress", "completed", "signed"}: needed.append("started_at")
        if data["record_type"] != "implant" and state in {"completed", "signed"}: needed.extend(("ended_at", "handover"))
    elif domain == "dialysis":
        if state in {"in_progress", "completed", "signed"}: needed.append("started_at")
        if state in {"completed", "signed"}: needed.extend(("ended_at", "handover", "actual_duration_minutes"))
    for field in needed:
        if data.get(field) is None or (isinstance(data.get(field), str) and not data[field].strip()):
            raise HTTPException(422, f"{field} is required for {state}")


def _links(db, domain, body, user, data):
    patient = _patient(db, body.patient_id)
    if domain != "perioperative" and body.surgery_id:
        raise HTTPException(422, "Surgery linkage belongs to perioperative records")
    if domain == "perioperative":
        case = db.execute("SELECT patient_id FROM surgeries WHERE id=?", (body.surgery_id,)).fetchone()
        if case is None or case["patient_id"] != body.patient_id:
            raise HTTPException(404, "Record not found")
    if not _allowed(db, domain, patient, user, surgery_id=body.surgery_id):
        raise HTTPException(404, "Record not found")
    admission = None
    if body.admission_id:
        admission = db.execute("SELECT * FROM admissions WHERE id=? AND patient_id=?", (body.admission_id, body.patient_id)).fetchone()
        if admission is None:
            raise HTTPException(422, "Admission does not belong to this patient")
    latest = db.execute("SELECT department,facility_id FROM admissions WHERE patient_id=? ORDER BY (status='active') DESC,created_at DESC,rowid DESC LIMIT 1", (body.patient_id,)).fetchone()
    facility = (latest["facility_id"] if latest else patient["registered_facility_id"])
    if facility is None:
        raise HTTPException(422, "Patient has no facility")
    if admission and admission["facility_id"] != facility:
        raise HTTPException(422, "Admission belongs to a different facility")
    _order_link(db, domain, body.patient_id, data)
    return int(facility), department_scope(user) if domain == "emergency" else (latest["department"] if latest else department_scope(user))


def _order_link(db, domain, patient_id, data):
    if domain == "radiology" and data.get("order_id"):
        order = db.execute("SELECT patient_id,order_type FROM orders WHERE id=?", (data["order_id"],)).fetchone()
        if order is None or order["patient_id"] != patient_id or order["order_type"] != "imaging":
            raise HTTPException(422, "Imaging order does not belong to this patient")


def _prescription(domain, user, data, old=None):
    if domain == "dialysis" and data.get("prescription") != (old or {}).get("prescription"):
        if not has_permission(user["role"], "dialysis_record.prescribe"):
            raise HTTPException(403, "A prescriber must record or change the dialysis prescription")


def _audit(db, domain, user, action, row=None, count=None, request=None):
    log_audit_event(correlation_id=str(uuid.uuid4()), event_category="phi_access" if action in {"viewed", "listed"} else "clinical_access",
                    action_type=f"{domain.replace('-', '_')}_{action}", outcome="success", db=db,
                    summary_message=f"{domain} documentation {action}", actor_user_id=user["id"], actor_role=user["role"],
                    patient_id=row["patient_id"] if row else None, target_type=domain,
                    target_id=row["id"] if row else None, encounter_id=row["admission_id"] if row else None,
                    request_path=request.url.path if request else None,
                    http_method=request.method if request else None,
                    metadata={"version": row["version"]} if row else {"count": count or 0})


def _event(db, domain, row, kind, user, prior=None, reason=None, snapshot=None):
    db.execute(f"INSERT INTO {DOMAINS[domain].table}_events (record_id,patient_id,version,kind,prior_state,new_state,actor_id,recorded_at,reason,snapshot_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (row["id"],row["patient_id"],row["version"],kind,prior,row["state"],user["id"],_now(),reason,_json(snapshot if snapshot is not None else {"title":row["title"],"data":json.loads(row["payload_json"])})))


def _answer(row):
    out = dict(row)
    out["data"] = json.loads(out.pop("payload_json"))
    out.pop("request_fingerprint", None)
    out.pop("idempotency_key", None)
    return out


def meta(domain, actor):
    require_module(domain)
    spec = DOMAINS[domain]
    with db_session() as db:
        user = _user(db, actor, spec.read)
        return {"module":domain,"states":list(spec.states),"initial_state":spec.initial,"terminal_states":sorted(spec.terminal),
                "can_write":has_permission(user["role"],spec.write),"can_sign":bool(spec.sign and has_permission(user["role"],spec.sign)),
                "can_prescribe":has_permission(user["role"],"dialysis_record.prescribe") if domain == "dialysis" else False,
                "documentation_only":True}


def list_records(domain, actor, patient_id=None, limit=50, offset=0, request=None):
    require_module(domain)
    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(422, "Invalid pagination")
    spec = DOMAINS[domain]
    with db_session() as db:
        user = _user(db, actor, spec.read)
        where, params = _scope(db, domain, user)
        if patient_id:
            where += " AND r.patient_id=?"
            params = (*params, patient_id)
        rows = db.execute(f"SELECT r.*,p.name AS patient_name,p.mrn FROM {spec.table} r JOIN patients p ON p.id=r.patient_id LEFT JOIN surgeries s ON s.id=r.surgery_id WHERE {where} ORDER BY r.updated_at DESC,r.id LIMIT ? OFFSET ?", (*params,limit,offset)).fetchall()
        _audit(db, domain, user, "listed", count=len(rows), request=request)
        return {"records":[_answer(r) for r in rows],"limit":limit,"offset":offset}


def list_perioperative_cases(actor, request=None, q="", offset=0):
    """Existing surgical scope lets theatre staff choose cases across wards."""
    require_module("perioperative")
    with db_session() as db:
        user = _user(db, actor, DOMAINS["perioperative"].read)
        ids, params = scope_patient_ids_sql(None, facilities=_facilities(db, user))
        surgery_sql, surgery_params = surgical_list_filter(user)
        if not isinstance(q, str) or len(q) > 200 or type(offset) is not int or offset < 0:
            raise HTTPException(422, "Invalid case search")
        search = ""
        search_params = ()
        if q.strip():
            search = " AND (s.id LIKE ? OR p.name LIKE ? OR p.mrn LIKE ? OR s.procedure_name LIKE ? OR s.scheduled_date LIKE ?)"
            search_params = (f"%{q.strip()}%",) * 5
        rows = db.execute(
            f"SELECT s.id,s.patient_id,s.procedure_name,s.scheduled_date,p.name AS patient_name "
            f"FROM surgeries s JOIN patients p ON p.id=s.patient_id "
            f"WHERE s.patient_id IN ({ids}){surgery_sql}{search} "
            "ORDER BY s.scheduled_date DESC,s.id LIMIT 100 OFFSET ?", (*params, *surgery_params, *search_params, offset)).fetchall()
        _audit(db, "perioperative", user, "listed", count=len(rows), request=request)
        return {"cases": [dict(row) for row in rows], "offset": offset}


def _history(db, domain, record_id, offset=0):
    events = db.execute(f"SELECT version,kind,prior_state,new_state,actor_id,recorded_at,reason,snapshot_json FROM {DOMAINS[domain].table}_events WHERE record_id=? ORDER BY version DESC LIMIT 101 OFFSET ?", (record_id, offset)).fetchall()
    return {"history": [{**{k:e[k] for k in e.keys() if k != "snapshot_json"}, "snapshot":json.loads(e["snapshot_json"])} for e in events[:100]],
            "history_next_offset": offset + 100 if len(events) > 100 else None}


def history(domain, record_id, actor, offset=0, request=None):
    require_module(domain)
    if type(offset) is not int or offset < 0:
        raise HTTPException(422, "Invalid history pagination")
    with db_session() as db:
        user = _user(db, actor, DOMAINS[domain].read)
        row = _record(db, domain, record_id, user)
        _audit(db, domain, user, "viewed", row, request=request)
        return _history(db, domain, record_id, offset)


def detail(domain, record_id, actor, request=None):
    require_module(domain)
    with db_session() as db:
        user = _user(db, actor, DOMAINS[domain].read)
        row = _record(db, domain, record_id, user)
        _audit(db, domain, user, "viewed", row, request=request)
        out = _answer(row)
        out.update(_history(db, domain, record_id))
        return out


def create(domain, body, actor, idempotency_key=None, request=None):
    require_module(domain)
    spec = DOMAINS[domain]
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key)>200):
        raise HTTPException(422, "Idempotency key must contain 1 to 200 characters")
    data = _validate(domain, body.data)
    fingerprint = hashlib.sha256(_json(body.model_dump()).encode()).hexdigest()
    with db_session(immediate=True) as db:
        user = _user(db, actor, spec.write)
        facility, dept = _links(db, domain, body, user, data)
        _prescription(domain, user, data)
        if idempotency_key:
            old = db.execute(f"SELECT id,request_fingerprint FROM {spec.table} WHERE created_by=? AND idempotency_key=?", (user["id"],idempotency_key)).fetchone()
            if old:
                row = _record(db, domain, old["id"], user)
                if old["request_fingerprint"] != fingerprint:
                    raise HTTPException(409, "Idempotency key was used for different documentation")
                _audit(db, domain, user, "viewed", row, request=request)
                return _answer(row)
        record_id, now = str(uuid.uuid4()), _now()
        db.execute(f"INSERT INTO {spec.table} (id,patient_id,admission_id,surgery_id,facility_id,department,title,state,payload_json,created_by,updated_by,created_at,updated_at,idempotency_key,request_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (record_id,body.patient_id,body.admission_id,body.surgery_id,facility,dept,body.title,spec.initial,_json(data),user["id"],user["id"],now,now,idempotency_key,fingerprint))
        row = db.execute(f"SELECT * FROM {spec.table} WHERE id=?", (record_id,)).fetchone()
        _event(db,domain,row,"created",user)
        _audit(db,domain,user,"created",row,request=request)
        return _answer(row)


def update(domain, record_id, body, actor, request=None):
    require_module(domain)
    spec = DOMAINS[domain]
    data = _validate(domain,body.data)
    with db_session(immediate=True) as db:
        user = _user(db,actor,spec.write)
        row = _record(db,domain,record_id,user)
        if row["version"] != body.version:
            raise HTTPException(409,"Record changed; reload before saving")
        if row["state"] in spec.terminal:
            raise HTTPException(409,"Final records accept addenda only")
        if body.state not in spec.states or body.state == "signed":
            raise HTTPException(422,"Use the signing action to sign a record")
        if body.state != "cancelled" and spec.states.index(body.state) < spec.states.index(row["state"]):
            raise HTTPException(409,"A workflow cannot move backwards")
        if body.state == "cancelled" and not body.reason.strip():
            raise HTTPException(422,"Cancellation needs a reason")
        old_data = json.loads(row["payload_json"])
        if domain == "perioperative" and data["record_type"] != old_data["record_type"]:
            raise HTTPException(422,"Record type cannot change")
        _prescription(domain,user,data,old_data)
        _order_link(db,domain,row["patient_id"],data)
        _state_data(domain,body.state,data)
        db.execute(f"UPDATE {spec.table} SET title=?,state=?,payload_json=?,version=version+1,updated_by=?,updated_at=? WHERE id=? AND version=?", (body.title,body.state,_json(data),user["id"],_now(),record_id,body.version))
        updated = db.execute(f"SELECT * FROM {spec.table} WHERE id=?",(record_id,)).fetchone()
        _event(db,domain,updated,"updated",user,prior=row["state"],reason=body.reason or None)
        _audit(db,domain,user,"updated",updated,request=request)
        return _answer(updated)


def sign(domain, record_id, version, actor, request=None):
    require_module(domain)
    spec=DOMAINS[domain]
    if not spec.sign:
        raise HTTPException(405,"This workflow closes through its documented outcome")
    with db_session(immediate=True) as db:
        user=_user(db,actor,spec.sign)
        row=_record(db,domain,record_id,user)
        if row["version"] != version:
            raise HTTPException(409,"Record changed; reload before signing")
        required="reported" if domain=="radiology" else "completed"
        if row["state"] != required:
            raise HTTPException(409,f"Record must be {required} before signing")
        _state_data(domain,"signed",json.loads(row["payload_json"]))
        now=_now()
        db.execute(f"UPDATE {spec.table} SET state='signed',version=version+1,signed_by=?,signed_at=?,updated_by=?,updated_at=? WHERE id=? AND version=?", (user["id"],now,user["id"],now,record_id,version))
        updated=db.execute(f"SELECT * FROM {spec.table} WHERE id=?",(record_id,)).fetchone()
        _event(db,domain,updated,"signed",user,prior=row["state"])
        _audit(db,domain,user,"signed",updated,request=request)
        return _answer(updated)


def addendum(domain, record_id, body, actor, request=None):
    require_module(domain)
    spec=DOMAINS[domain]
    with db_session(immediate=True) as db:
        user=_user(db,actor,spec.write)
        row=_record(db,domain,record_id,user)
        if row["version"] != body.version:
            raise HTTPException(409,"Record changed; reload before adding a note")
        if row["state"] not in spec.terminal:
            raise HTTPException(409,"Save the working record before adding a final-record addendum")
        db.execute(f"UPDATE {spec.table} SET version=version+1,updated_by=?,updated_at=? WHERE id=? AND version=?",(user["id"],_now(),record_id,body.version))
        updated=db.execute(f"SELECT * FROM {spec.table} WHERE id=?",(record_id,)).fetchone()
        _event(db,domain,updated,"addendum",user,prior=row["state"],reason=body.reason,snapshot={"text":body.text})
        _audit(db,domain,user,"addendum_added",updated,request=request)
        return _answer(updated)
