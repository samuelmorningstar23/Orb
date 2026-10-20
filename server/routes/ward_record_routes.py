"""Orb Ward Record: the ward's observation interval and the monthly rounds file.

Every route needs `admin.overview`, the permission the NABH quality desk uses
(routes/nabh_routes.py): the interval is a ward policy, and the rounds file is
a quality document about named beds.

Every change to a ward's interval or grace is audited with the old and the new
values. Every rounds file produced is logged in ward_record_exports with the
SHA-256 of the exact document, and audited.

Handlers are plain `def`, so FastAPI runs them in a threadpool: everything here
opens SQLite, and blocking the event loop is how this repo has frozen before.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from permissions import ADMIN_OVERVIEW, require_permission
from pydantic import BaseModel
from services.audit_service import log_audit_event

from services import ward_record_service as svc

logger = logging.getLogger("orb.ward_record_routes")

router = APIRouter(prefix="/api/ward-record", tags=["Ward Record"])


def _fail(exc: svc.WardRecordError) -> HTTPException:
    """A service refusal as HTTP, keeping the machine-readable code."""
    status = {
        "no_interval": 409,
        "unknown_ward": 404,
        "bad_value": 422,
        "missing_field": 422,
        "bad_period": 422,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail={"error": exc.code, "message": str(exc)})


def _audit(
    request: Request,
    user: dict,
    action: str,
    *,
    summary: str,
    target_type: str,
    target_id: str | int | None = None,
    metadata: dict | None = None,
) -> None:
    """Category `security`, fail-closed, like the NABH desk's writes: a ward
    policy change and a quality file leaving the system are administrative
    acts, not clinical reads."""
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome="success",
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        request_path=str(request.url.path),
        http_method=request.method,
        metadata=metadata,
    )


class SettingIn(BaseModel):
    interval_minutes: int
    grace_minutes: int | None = None
    #: Who set this in writing: the nursing superintendent's name as it
    #: appears on the written order. The account keying it is audited apart.
    set_by: str
    note: str | None = None


_GET_SETTINGS_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/settings")
def get_settings(user: dict = _GET_SETTINGS_USER_DEFAULT) -> dict:
    """Every ward, with its interval and grace, or none."""
    return svc.list_settings()


_PUT_SETTING_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.put("/settings/{ward}")
def put_setting(
    request: Request,
    ward: str,
    body: SettingIn,
    user: dict = _PUT_SETTING_USER_DEFAULT,
) -> dict:
    """Record a ward's interval and grace as set in writing."""
    try:
        result = svc.set_setting(
            ward,
            interval_minutes=body.interval_minutes,
            grace_minutes=body.grace_minutes,
            set_by=body.set_by,
            note=body.note,
        )
    except svc.WardRecordError as exc:
        raise _fail(exc) from exc
    new = result["new"]
    _audit(
        request,
        user,
        "ward_observation_interval_set",
        summary=(
            f"Observation interval for {new['ward']} set to "
            f"{new['interval_minutes']} minutes, grace "
            f"{new['grace_minutes']} minutes"
        ),
        target_type="ward_observation_setting",
        target_id=new["ward"],
        metadata={"old": result["old"], "new": new},
    )
    return new


def _rounds_file(
    request: Request,
    user: dict,
    fmt: str,
    ward: str,
    start: str,
    end: str,
    include_patient_identifiers: bool,
) -> dict:
    try:
        result = svc.produce_file(
            ward,
            start,
            end,
            fmt=fmt,
            generated_by=str(user.get("id") or ""),
            include_patient_identifiers=include_patient_identifiers,
        )
    except svc.WardRecordError as exc:
        raise _fail(exc) from exc
    data = result["data"]
    _audit(
        request,
        user,
        "ward_record_rounds_file_generated",
        summary=(
            f"Rounds file {result['reference']} ({fmt}) produced for "
            f"{data['ward']}, {data['period']['start_date']} to "
            f"{data['period']['end_date']}"
        ),
        target_type="ward_record_export",
        target_id=result["reference"],
        metadata={
            "ward": data["ward"],
            "format": fmt,
            "sha256": result["sha256"],
            "period_start": data["period"]["start_date"],
            "period_end": data["period"]["end_date"],
            "include_patient_identifiers": bool(include_patient_identifiers),
        },
    )
    return result


def _file_response(result: dict, media_type: str, extension: str) -> Response:
    data = result["data"]
    safe_ward = "".join(ch if ch.isalnum() else "_" for ch in data["ward"])[:40] or "ward"
    name = (
        f"rounds_{safe_ward}_{data['period']['start_date']}_"
        f"{data['period']['end_date']}.{extension}"
    )
    return Response(
        content=result["content"].encode("utf-8"),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Ward-Record-Reference": result["reference"],
            "X-Content-SHA256": result["sha256"],
            "Cache-Control": "no-store",
        },
    )


_GET_ROUNDS_WARD_DEFAULT = Query(..., min_length=1)
_GET_ROUNDS_START_DEFAULT = Query(...)
_GET_ROUNDS_END_DEFAULT = Query(...)
_GET_ROUNDS_INCLUDE_PATIENT_IDENTIFIERS_DEFAULT = Query(False)
_GET_ROUNDS_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/rounds")
def get_rounds(
    request: Request,
    ward: str = _GET_ROUNDS_WARD_DEFAULT,
    start: str = _GET_ROUNDS_START_DEFAULT,
    end: str = _GET_ROUNDS_END_DEFAULT,
    include_patient_identifiers: bool = _GET_ROUNDS_INCLUDE_PATIENT_IDENTIFIERS_DEFAULT,
    user: dict = _GET_ROUNDS_USER_DEFAULT,
) -> dict:
    """The rounds file as JSON. Logged and audited like the two downloads."""
    result = _rounds_file(request, user, "json", ward, start, end, include_patient_identifiers)
    return dict(result["content"], sha256=result["sha256"])


_GET_ROUNDS_CSV_WARD_DEFAULT = Query(..., min_length=1)
_GET_ROUNDS_CSV_START_DEFAULT = Query(...)
_GET_ROUNDS_CSV_END_DEFAULT = Query(...)
_GET_ROUNDS_CSV_INCLUDE_PATIENT_IDENTIFIERS_DEFAULT = Query(False)
_GET_ROUNDS_CSV_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/rounds.csv")
def get_rounds_csv(
    request: Request,
    ward: str = _GET_ROUNDS_CSV_WARD_DEFAULT,
    start: str = _GET_ROUNDS_CSV_START_DEFAULT,
    end: str = _GET_ROUNDS_CSV_END_DEFAULT,
    include_patient_identifiers: bool = _GET_ROUNDS_CSV_INCLUDE_PATIENT_IDENTIFIERS_DEFAULT,
    user: dict = _GET_ROUNDS_CSV_USER_DEFAULT,
) -> Response:
    result = _rounds_file(request, user, "csv", ward, start, end, include_patient_identifiers)
    return _file_response(result, "text/csv; charset=utf-8", "csv")


_GET_ROUNDS_MD_WARD_DEFAULT = Query(..., min_length=1)
_GET_ROUNDS_MD_START_DEFAULT = Query(...)
_GET_ROUNDS_MD_END_DEFAULT = Query(...)
_GET_ROUNDS_MD_INCLUDE_PATIENT_IDENTIFIERS_DEFAULT = Query(False)
_GET_ROUNDS_MD_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/rounds.md")
def get_rounds_md(
    request: Request,
    ward: str = _GET_ROUNDS_MD_WARD_DEFAULT,
    start: str = _GET_ROUNDS_MD_START_DEFAULT,
    end: str = _GET_ROUNDS_MD_END_DEFAULT,
    include_patient_identifiers: bool = _GET_ROUNDS_MD_INCLUDE_PATIENT_IDENTIFIERS_DEFAULT,
    user: dict = _GET_ROUNDS_MD_USER_DEFAULT,
) -> Response:
    result = _rounds_file(request, user, "md", ward, start, end, include_patient_identifiers)
    return _file_response(result, "text/markdown; charset=utf-8", "md")
