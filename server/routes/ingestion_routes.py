"""Orb Hospital OS — Ingestion API Routes

POST /api/ingest/json   — Accept structured JSON webhook payloads
POST /api/ingest/hl7    — Accept raw HL7 ORU message strings
GET  /api/ingest/status — Query ingestion event status (admin/governance)
POST /api/ingest/replay — Replay a fixture file (dev only)
GET  /api/ingest/quarantine                  The unmatched results queue (lis-19)
POST /api/ingest/quarantine/{id}/attach      Attach one to a patient (ingest.reconcile)
POST /api/ingest/quarantine/{id}/discard     Discard one, with a reason (ingest.reconcile)
"""
from __future__ import annotations

import hmac
import uuid
from fastapi import APIRouter, Request, HTTPException, Header, Depends
from pydantic import BaseModel, Field
from typing import Optional

from auth import get_current_user
from config import INGESTION_API_KEY, INGESTION_MODE
from services.ingestion_adapters import HL7Adapter, JSONAdapter, CSVAdapter, FHIRAdapter
from services.ingestion_service import ingest_payload, IngestionResult
from services.audit_service import log_audit_event
from services import result_quarantine_service
from permissions import (
    INGEST_MONITOR, INGEST_RECONCILE, ROLE_PERMISSIONS, log_phi_read, require_permission,
)
from database import db_session, run_db

router = APIRouter(prefix="/api/ingest", tags=["ingestion"])


# ════════════════════════════════════════════
#  API Key Verification
# ════════════════════════════════════════════

def _verify_api_key(x_api_key: Optional[str]):
    """Verify the ingestion API key using a constant-time comparison.

    A plain `!=` leaks the key length and per-character match position via timing,
    letting an attacker recover the key that guards real-patient data ingestion.
    hmac.compare_digest runs in time independent of where the mismatch is.
    """
    if not x_api_key or not INGESTION_API_KEY or not hmac.compare_digest(
        str(x_api_key), str(INGESTION_API_KEY)
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


def _check_ingestion_enabled():
    """Ensure ingestion endpoints are active in the current mode."""
    if INGESTION_MODE == "simulation":
        raise HTTPException(
            status_code=503,
            detail="Ingestion endpoints are disabled in simulation mode. "
                   "Set ORB_INGESTION_MODE=ingestion or ORB_INGESTION_MODE=hybrid."
        )


# ════════════════════════════════════════════
#  JSON Webhook Endpoint
# ════════════════════════════════════════════

@router.post("/json")
async def ingest_json(
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_correlation_id: Optional[str] = Header(None),
    x_message_id: Optional[str] = Header(None),
):
    """Accept a structured JSON webhook payload and ingest into Orb.

    Headers:
        X-API-Key:          Required. Ingestion API key.
        X-Correlation-Id:   Optional. Caller-provided trace ID.
        X-Message-Id:       Optional. Sender-provided dedup key.

    Body:
        See JSONAdapter docstring for expected payload format.
    """
    _check_ingestion_enabled()
    _verify_api_key(x_api_key)

    body = await request.body()
    raw_payload = body.decode("utf-8", errors="replace")

    if not raw_payload.strip():
        raise HTTPException(status_code=400, detail="Empty request body")

    correlation_id = x_correlation_id or str(uuid.uuid4())

    # Get Socket.IO for real-time emission
    try:
        from ws.realtime import sio
    except ImportError:
        sio = None

    result = await ingest_payload(
        raw_payload=raw_payload,
        adapter=JSONAdapter(),
        source_name=request.headers.get("X-Source-Name"),
        content_type="application/json",
        correlation_id=correlation_id,
        external_message_id=x_message_id,
        sio=sio,
    )

    status_code = _http_status_from_result(result)
    return _build_response(result, status_code)


# ════════════════════════════════════════════
#  HL7 ORU Endpoint
# ════════════════════════════════════════════

@router.post("/hl7")
async def ingest_hl7(
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_correlation_id: Optional[str] = Header(None),
    x_message_id: Optional[str] = Header(None),
):
    """Accept a raw HL7 v2 ORU message and ingest into Orb.

    The body should be a raw HL7 message string (segments separated by \\r, \\n, or \\r\\n).
    Content-Type should be text/plain or x-application/hl7-v2+er7.

    Alternatively, send a JSON wrapper:
        { "message": "MSH|^~\\\\&|..." }
    """
    _check_ingestion_enabled()
    _verify_api_key(x_api_key)

    body = await request.body()
    raw_payload = body.decode("utf-8", errors="replace")

    if not raw_payload.strip():
        raise HTTPException(status_code=400, detail="Empty request body")

    # Support JSON wrapper for HL7 message
    if raw_payload.strip().startswith("{"):
        import json
        try:
            wrapper = json.loads(raw_payload)
            raw_payload = wrapper.get("message", raw_payload)
        except json.JSONDecodeError:
            pass  # treat as raw HL7

    correlation_id = x_correlation_id or str(uuid.uuid4())

    try:
        from ws.realtime import sio
    except ImportError:
        sio = None

    result = await ingest_payload(
        raw_payload=raw_payload,
        adapter=HL7Adapter(),
        source_name=request.headers.get("X-Source-Name", "hl7_sender"),
        content_type="text/plain",
        correlation_id=correlation_id,
        external_message_id=x_message_id,
        sio=sio,
    )

    status_code = _http_status_from_result(result)
    return _build_response(result, status_code)


# ════════════════════════════════════════════
#  FHIR R4 Endpoint
# ════════════════════════════════════════════

@router.post("/fhir")
async def ingest_fhir(
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_correlation_id: Optional[str] = Header(None),
    x_message_id: Optional[str] = Header(None),
):
    """Accept a FHIR R4 payload and ingest into Orb.

    The body should be a FHIR R4 JSON document — either a Bundle
    (type transaction/collection/batch with entry[].resource) or a single
    resource (Observation, Patient, Encounter, MedicationRequest,
    AllergyIntolerance). Content-Type should be application/fhir+json.

    Headers:
        X-API-Key:          Required. Ingestion API key.
        X-Correlation-Id:   Optional. Caller-provided trace ID.
        X-Message-Id:       Optional. Sender-provided dedup key.
    """
    _check_ingestion_enabled()
    _verify_api_key(x_api_key)

    body = await request.body()
    raw_payload = body.decode("utf-8", errors="replace")

    if not raw_payload.strip():
        raise HTTPException(status_code=400, detail="Empty request body")

    correlation_id = x_correlation_id or str(uuid.uuid4())

    # Get Socket.IO for real-time emission
    try:
        from ws.realtime import sio
    except ImportError:
        sio = None

    result = await ingest_payload(
        raw_payload=raw_payload,
        adapter=FHIRAdapter(),
        source_name=request.headers.get("X-Source-Name"),
        content_type="application/fhir+json",
        correlation_id=correlation_id,
        external_message_id=x_message_id,
        sio=sio,
    )

    status_code = _http_status_from_result(result)
    return _build_response(result, status_code)


# ════════════════════════════════════════════
#  Status / Debug Endpoint
# ════════════════════════════════════════════

@router.get("/status")
def ingestion_status(
    limit: int = 20,
    status: Optional[str] = None,
    user: dict = Depends(require_permission("ingest.monitor")),
):
    """Query recent ingestion events. Requires ingest.monitor permission.

    Query params:
        limit:  Number of events to return (default 20, max 100).
        status: Filter by parse_status (received, parsed, failed, duplicate).
    """
    limit = min(limit, 100)

    with db_session() as db:
        if status:
            rows = db.execute(
                """SELECT id, received_at, source_type, source_name, parse_status, parse_error,
                          correlation_id, vitals_inserted, observations_inserted, processed_at
                   FROM ingestion_events
                   WHERE parse_status = ?
                   ORDER BY received_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT id, received_at, source_type, source_name, parse_status, parse_error,
                          correlation_id, vitals_inserted, observations_inserted, processed_at
                   FROM ingestion_events
                   ORDER BY received_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    return {
        "events": [dict(r) for r in rows],
        "count": len(rows),
        "ingestion_mode": INGESTION_MODE,
    }


# ════════════════════════════════════════════
#  Dev Replay Endpoint
# ════════════════════════════════════════════

@router.post("/replay")
async def replay_fixture(
    request: Request,
    x_api_key: Optional[str] = Header(None),
):
    """Replay a raw payload with auto-detected adapter. Useful for dev testing.

    Send any raw payload (JSON, HL7, CSV) and it will be auto-detected and processed.
    """
    _check_ingestion_enabled()
    _verify_api_key(x_api_key)

    body = await request.body()
    raw_payload = body.decode("utf-8", errors="replace")

    if not raw_payload.strip():
        raise HTTPException(status_code=400, detail="Empty request body")

    correlation_id = f"replay-{uuid.uuid4()}"

    try:
        from ws.realtime import sio
    except ImportError:
        sio = None

    result = await ingest_payload(
        raw_payload=raw_payload,
        adapter=None,  # auto-detect
        source_name="dev_replay",
        correlation_id=correlation_id,
        sio=sio,
    )

    # Audit M2M replay for traceability.
    # OFF THE EVENT LOOP: log_audit_event is a hash-chained SQLite write that takes
    # BEGIN IMMEDIATE, and since encryption-at-rest every page costs HMAC-SHA512 +
    # AES. Called directly from this `async def` it blocked the loop — the failure
    # class behind all three of Orb's production freezes (docs/WORKLOG.md §3).
    # Awaited, not fire-and-forget: the row must still be written before we answer.
    await run_db(
        log_audit_event,
        correlation_id=correlation_id,
        event_category="ingestion",
        action_type="ingestion_replay_triggered",
        outcome="success" if result.parse_status in ("parsed", "partially_parsed") else "failure",
        summary_message=f"Dev replay: {result.parse_status}",
        source_type="m2m_replay",
        metadata={"parse_status": result.parse_status, "api_key_used": True},
    )

    status_code = _http_status_from_result(result)
    return _build_response(result, status_code)


# ════════════════════════════════════════════
#  Response Helpers
# ════════════════════════════════════════════

def _http_status_from_result(result: IngestionResult) -> int:
    if result.parse_status == "parsed":
        return 200
    elif result.parse_status == "partially_parsed":
        return 207  # Multi-Status
    elif result.parse_status == "duplicate":
        return 200  # Idempotent success
    else:
        return 422  # Unprocessable Entity


def _build_response(result: IngestionResult, status_code: int) -> dict:
    counts = {
        "received": result.received,
        "inserted_vitals": result.inserted_vitals,
        "inserted_observations": result.inserted_observations,
        "duplicates": result.duplicates,
    }
    # Results held in the unmatched results queue (lis-19). Present only when
    # there were some, so the answer is unchanged with the queue switched off.
    if getattr(result, "quarantined", 0):
        counts["quarantined"] = result.quarantined
    return {
        "status": "ok" if status_code < 300 else "error",
        "event_id": result.event_id,
        "correlation_id": result.correlation_id,
        "parse_status": result.parse_status,
        "counts": counts,
        "errors": result.errors if result.errors else None,
    }


# ════════════════════════════════════════════
#  Unmatched Results Queue (lis-19)
# ════════════════════════════════════════════
# Results whose patient identifier matched nobody, held with
# ORB_RESULT_QUARANTINE on (services/result_quarantine_service.py). The list
# answers ingest.monitor (the administrator: counts and a list without
# identifiers or values) or ingest.reconcile (laboratory staff: the full
# rows). Attach and discard answer ingest.reconcile ONLY, which roles.py
# grants to laboratory roles alone (ruling LIS-R17): an administrator gets
# 403. Plain def handlers, so every database touch runs off the event loop.

def _require_any_permission(*permissions: str):
    """Like require_permission, but ANY one of the permissions is enough. A
    denial is audited exactly as require_permission audits one (the same
    shape as routes/lab/master.py)."""
    async def _checker(request: Request, user: dict = Depends(get_current_user)) -> dict:
        role = user.get("role", "")
        if any(p in ROLE_PERMISSIONS.get(role, set()) for p in permissions):
            return user
        await run_db(
            log_audit_event,
            correlation_id=str(uuid.uuid4()),
            event_category="authorization",
            action_type="permission_denied",
            outcome="denied",
            summary_message=f"Missing any of: {', '.join(permissions)}",
            actor_user_id=user.get("id"),
            actor_role=role,
            request_path=request.url.path,
            http_method=request.method,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            metadata={"required_any": list(permissions), "user_role": role},
        )
        raise HTTPException(status_code=403, detail={
            "error": "permission_denied", "message": "Insufficient permissions",
            "required_any": list(permissions)})
    _checker.__name__ = "require_any_permission"
    return _checker


def _request_meta(request: Request) -> dict:
    return {
        "path": request.url.path,
        "method": request.method,
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _quarantine_http_error(exc: result_quarantine_service.QuarantineError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.as_detail())


class QuarantineAttachRequest(BaseModel):
    hospital_number: str = Field(..., max_length=64)
    name: Optional[str] = Field(None, max_length=200)
    date_of_birth: Optional[str] = Field(None, max_length=32)
    reason: str = Field(..., max_length=1000)


class QuarantineDiscardRequest(BaseModel):
    reason: str = Field(..., max_length=1000)


@router.get("/quarantine")
def list_quarantine(
    request: Request,
    status: str = "open",
    limit: int = 100,
    user: dict = Depends(_require_any_permission(INGEST_RECONCILE, INGEST_MONITOR)),
):
    """The unmatched results queue with its counts. A reconciler sees the
    identifiers the sender put in the message and the values; the
    administrator sees when each arrived, from where, the test and its state."""
    full = INGEST_RECONCILE in ROLE_PERMISSIONS.get(user.get("role", ""), set())
    try:
        body = result_quarantine_service.list_queue(status=status, limit=limit, full=full)
    except result_quarantine_service.QuarantineError as exc:
        raise _quarantine_http_error(exc)
    # Fail-closed phi_access: no row, no list.
    log_phi_read(
        request=request, user=user, action_type="lab_quarantine_viewed",
        summary_message=f"Unmatched results list viewed ({body['view']}, {status})",
        target_type="result_quarantine",
        metadata={"view": body["view"], "status": status, "rows": len(body["items"])},
    )
    return body


@router.post("/quarantine/{qid}/attach")
def attach_quarantined_result(
    qid: int,
    payload: QuarantineAttachRequest,
    request: Request,
    user: dict = Depends(require_permission(INGEST_RECONCILE)),
):
    """Attach one unmatched result to a patient after the two-identifier check.
    409 identity_not_confirmed, with the outcome of each check, when the
    identifiers do not confirm the patient; 409 message_names_another_patient
    when a hospital number in the message now names a different patient;
    409 name_and_birth_date_shared when another patient record has the same
    name and date of birth and no hospital number confirms this one; 409
    later_correction_on_chart when a correction of this result, sent after
    it, is on the patient's chart already."""
    try:
        outcome = result_quarantine_service.attach(
            qid, hospital_number=payload.hospital_number, name=payload.name,
            date_of_birth=payload.date_of_birth, reason=payload.reason,
            user=user, request_meta=_request_meta(request))
    except result_quarantine_service.QuarantineError as exc:
        raise _quarantine_http_error(exc)
    refusal_words = {
        result_quarantine_service.REFUSED_OTHER_PATIENT: (
            "A hospital number in this message now belongs to a different patient's "
            "record, so it cannot be attached to this one. Check that record, or ask "
            "the sending laboratory which patient the result is for."),
        result_quarantine_service.REFUSED_SHARED_IDENTITY: (
            "Another patient's record has the same name and date of birth, so these two "
            "cannot confirm which patient this result is for. It can be attached only by a "
            "hospital number in the message. Otherwise discard it and ask the sending "
            "laboratory to resend it with the patient's hospital number."),
        result_quarantine_service.REFUSED_LATER_CORRECTION: (
            "A corrected result for this test and order, sent after this one, is already on "
            "this patient's chart, so this older result cannot go on top of it. Discard it, "
            "or ask the sending laboratory which result stands."),
    }
    if not outcome.get("attached") and outcome.get("refusal") in refusal_words:
        raise HTTPException(status_code=409, detail={
            "error": outcome["refusal"],
            "message": refusal_words[outcome["refusal"]],
            "checks": outcome.get("checks", {}),
        })
    if not outcome.get("attached"):
        raise HTTPException(status_code=409, detail={
            "error": "identity_not_confirmed",
            "message": ("These identifiers do not confirm the patient. Attaching needs two "
                        "identifiers that match both this result and the patient's record."),
            "checks": outcome.get("checks", {}),
        })
    return outcome


@router.post("/quarantine/{qid}/discard")
def discard_quarantined_result(
    qid: int,
    payload: QuarantineDiscardRequest,
    request: Request,
    user: dict = Depends(require_permission(INGEST_RECONCILE)),
):
    """Discard one unmatched result, with a reason."""
    try:
        return result_quarantine_service.discard(
            qid, reason=payload.reason, user=user, request_meta=_request_meta(request))
    except result_quarantine_service.QuarantineError as exc:
        raise _quarantine_http_error(exc)
