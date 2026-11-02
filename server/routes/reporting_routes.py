"""Opt-in local reporting; sync handlers keep DB and CSV work off the loop."""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, StrictInt

from permissions import REPORTING_EXPORT, REPORTING_READ, REPORTING_WRITE, require_permission
from routes.opt_in_gates import module_gate
from services import reporting_service as svc
from services.reporting_finance_common import WorkbenchError

logger = logging.getLogger("orb.reporting")
router = APIRouter(prefix="/api/reporting", tags=["Local reporting"], dependencies=[Depends(module_gate("reporting"))])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilterIn(StrictBody):
    field: str
    op: Literal["eq", "ne", "in", "contains", "gte", "lte", "is_null"]
    value: Any


class QueryIn(StrictBody):
    dataset: str
    mode: Literal["rows", "summary"] = "rows"
    columns: list[str] | None = None
    filters: list[FilterIn] | None = None
    group_by: list[str] | None = None
    measures: list[str] | None = None
    limit: StrictInt = 200
    from_day: str | None = None
    to_day: str | None = None


class DefinitionIn(StrictBody):
    name: str
    query: QueryIn
    version: StrictInt | None = None


def _call(fn, **kwargs):
    try:
        return fn(**kwargs)
    except WorkbenchError as exc:
        raise HTTPException(status_code=exc.status, detail={"error": exc.code, "message": str(exc)}) from exc
    except Exception:
        logger.exception("Local reporting operation failed")
        raise HTTPException(status_code=500, detail={"error": "reporting_failed", "message": "The report could not be completed."}) from None


@router.get("/catalog")
def catalog(user=Depends(require_permission(REPORTING_READ))):
    return _call(svc.catalog, user=user)


@router.post("/query")
def query(body: QueryIn, user=Depends(require_permission(REPORTING_READ))):
    return _call(svc.run, user=user, query=body.model_dump(exclude_none=True))


@router.post("/query.csv")
def query_csv(body: QueryIn, user=Depends(require_permission(REPORTING_READ, REPORTING_EXPORT))):
    content = _call(svc.export_csv, user=user, query=body.model_dump(exclude_none=True))
    return Response(content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="orb-report.csv"', "Cache-Control": "no-store"})


@router.get("/definitions")
def definitions(user=Depends(require_permission(REPORTING_READ))):
    return {"definitions": _call(svc.definitions, user=user)}


@router.post("/definitions")
def create_definition(body: DefinitionIn, user=Depends(require_permission(REPORTING_READ, REPORTING_WRITE))):
    return _call(svc.save_definition, user=user, name=body.name, query=body.query.model_dump(exclude_none=True))


@router.put("/definitions/{definition_id}")
def update_definition(definition_id: str, body: DefinitionIn, user=Depends(require_permission(REPORTING_READ, REPORTING_WRITE))):
    return _call(svc.save_definition, user=user, definition_id=definition_id, version=body.version, name=body.name, query=body.query.model_dump(exclude_none=True))


@router.delete("/definitions/{definition_id}")
def delete_definition(definition_id: str, user=Depends(require_permission(REPORTING_READ, REPORTING_WRITE))):
    return _call(svc.delete_definition, user=user, definition_id=definition_id)
