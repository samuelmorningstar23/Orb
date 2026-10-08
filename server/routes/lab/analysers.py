"""Analysers: endpoints under /api/lab.

The package router in routes/lab/__init__.py adds the /api/lab prefix. This
router carries require_lab_module(allow_draining=True): 404 while the
laboratory is off, reachable while it is on or draining. Every write also
carries the strict gate (require_lab_module()), so the registry cannot change
while the laboratory drains (ruling LIS-R21).

Built in lis-23 (W5-F): the analyser registry, each analyser's key and its
test-code map (services/lab_instrument_service.py). No route here returns
patient data.
  GET   /analysers                              lab.read
  GET   /analysers/{instrument_id}              lab.read (with its code map)
  POST  /analysers                              lab.interface
  PATCH /analysers/{instrument_id}              lab.interface
  POST  /analysers/{instrument_id}/status       lab.interface {enabled, reason}
  POST  /analysers/{instrument_id}/key          lab.interface {reason}: the new
                                                key, shown once (no-store)
  PUT   /analysers/{instrument_id}/map          lab.master  one code's mapping
  POST  /analysers/{instrument_id}/map/retire   lab.master  {instrument_code, reason}
The service checks the permission again and writes its clinical_config audit
row inside the same transaction, so a write whose audit fails commits nothing
(503 audit_unavailable).

Still to come (lis-24, lis-25, W6-G):
  POST /analysers/{instrument_id}/messages   instrument key, no staff session

The instrument intake route authenticates by X-Instrument-Id and X-Instrument-Key, not a session: put it on a second router in this file gated by require_lab_module(require_session=False) (strict, not draining), include that router from routes/lab/__init__.py, and list the route in tests/test_unauthenticated_surface.py PUBLIC_ROUTES with its reason. It checks the pair through lab_instrument_service.authenticate, finds each code through mapping_for, and sets HTTP_INTAKE_BUILT below to True so the Analysers screen stops saying intake is not built.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from database import db_session
from permissions import LAB_INTERFACE, LAB_MASTER, LAB_READ, require_permission
from routes.lab.orders import audit_unavailable, request_meta
from routes.opt_in_gates import require_lab_module
from services import lab_instrument_service as instruments
from services.audit_service import AuditWriteError

router = APIRouter(dependencies=[Depends(require_lab_module(allow_draining=True))])

# A write changes configuration, so it 404s while the laboratory drains.
_WRITE_GATE = [Depends(require_lab_module())]

#: Whether POST /analysers/{instrument_id}/messages exists yet. lis-24 sets it.
HTTP_INTAKE_BUILT = False


def _refusal(err: instruments.LabInstrumentError) -> HTTPException:
    return HTTPException(status_code=err.status,
                         detail={"error": err.code, "message": err.message})


def _read(fn, *args, **kwargs):
    try:
        with db_session() as db:
            return fn(db, *args, **kwargs)
    except instruments.LabInstrumentError as err:
        raise _refusal(err)


def _write(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except instruments.LabInstrumentError as err:
        raise _refusal(err)
    except AuditWriteError:
        raise audit_unavailable()


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "not_found",
                                                  "message": "No such analyser."})


# ── Request bodies (unknown fields are refused, never ignored) ───────────────

class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstrumentCreate(_Body):
    id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=200)
    make: Optional[str] = Field(None, max_length=200)
    model: Optional[str] = Field(None, max_length=200)
    serial: Optional[str] = Field(None, max_length=200)
    section_id: Optional[str] = Field(None, max_length=64)
    protocol: str = Field("hl7v2", max_length=16)
    transport: str = Field("http", max_length=16)
    host: Optional[str] = Field(None, max_length=253)
    port: Optional[int] = None
    assumed_timezone: Optional[str] = Field(None, max_length=64)
    biomedical_asset_id: Optional[int] = None


class InstrumentUpdate(_Body):
    name: Optional[str] = Field(None, max_length=200)
    make: Optional[str] = Field(None, max_length=200)
    model: Optional[str] = Field(None, max_length=200)
    serial: Optional[str] = Field(None, max_length=200)
    section_id: Optional[str] = Field(None, max_length=64)
    protocol: Optional[str] = Field(None, max_length=16)
    transport: Optional[str] = Field(None, max_length=16)
    host: Optional[str] = Field(None, max_length=253)
    port: Optional[int] = None
    assumed_timezone: Optional[str] = Field(None, max_length=64)
    biomedical_asset_id: Optional[int] = None
    version: Optional[int] = None


class InstrumentStatus(_Body):
    enabled: bool
    reason: str = Field(..., max_length=500)


class KeyIssue(_Body):
    reason: Optional[str] = Field(None, max_length=500)


class MappingBody(_Body):
    instrument_code: str = Field(..., max_length=64)
    test_id: str = Field(..., max_length=128)
    factor: Optional[str | int | float] = None
    instrument_unit: Optional[str] = Field(None, max_length=32)
    note: str = Field(..., max_length=500)


class MappingRetire(_Body):
    instrument_code: str = Field(..., max_length=64)
    reason: str = Field(..., max_length=500)


# ── Reads ────────────────────────────────────────────────────────────────────

@router.get("/analysers")
def list_lab_analysers(user: dict = Depends(require_permission(LAB_READ))):
    """Every registered analyser, its key's issue date (never the key), its
    on or off state and how many codes are mapped. No patient data."""
    options = instruments.options()
    options["http_intake_built"] = HTTP_INTAKE_BUILT
    return {"analysers": _read(instruments.list_instruments), "options": options}


@router.get("/analysers/{instrument_id}")
def get_lab_analyser(instrument_id: str, user: dict = Depends(require_permission(LAB_READ))):
    found = _read(instruments.get_instrument, instrument_id)
    if found is None:
        raise _not_found()
    return found


# ── The registry and keys (lab.interface) ────────────────────────────────────

@router.post("/analysers", dependencies=_WRITE_GATE, status_code=201)
def register_lab_analyser(body: InstrumentCreate, request: Request,
                          user: dict = Depends(require_permission(LAB_INTERFACE))):
    return _write(instruments.create_instrument, body.model_dump(exclude_unset=True),
                  actor=user, request_meta=request_meta(request))


@router.patch("/analysers/{instrument_id}", dependencies=_WRITE_GATE)
def change_lab_analyser(instrument_id: str, body: InstrumentUpdate, request: Request,
                        user: dict = Depends(require_permission(LAB_INTERFACE))):
    changes = body.model_dump(exclude_unset=True)
    version = changes.pop("version", None)
    return _write(instruments.update_instrument, instrument_id, changes, actor=user,
                  expected_version=version, request_meta=request_meta(request))


@router.post("/analysers/{instrument_id}/status", dependencies=_WRITE_GATE)
def set_lab_analyser_status(instrument_id: str, body: InstrumentStatus, request: Request,
                            user: dict = Depends(require_permission(LAB_INTERFACE))):
    return _write(instruments.set_instrument_enabled, instrument_id, body.enabled, body.reason,
                  actor=user, request_meta=request_meta(request))


@router.post("/analysers/{instrument_id}/key", dependencies=_WRITE_GATE)
def issue_lab_analyser_key(instrument_id: str, body: KeyIssue, request: Request,
                           response: Response,
                           user: dict = Depends(require_permission(LAB_INTERFACE))):
    """The analyser's new key, in this answer only. Orb keeps its digest, so
    it can never be shown again; the previous key stops working now."""
    answer = _write(instruments.issue_key, instrument_id, actor=user, reason=body.reason,
                    request_meta=request_meta(request))
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return answer


# ── The code map (lab.master) ────────────────────────────────────────────────

@router.put("/analysers/{instrument_id}/map", dependencies=_WRITE_GATE)
def save_lab_analyser_mapping(instrument_id: str, body: MappingBody, request: Request,
                              user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(instruments.save_mapping, instrument_id, body.model_dump(exclude_unset=True),
                  actor=user, request_meta=request_meta(request))


@router.post("/analysers/{instrument_id}/map/retire", dependencies=_WRITE_GATE)
def retire_lab_analyser_mapping(instrument_id: str, body: MappingRetire, request: Request,
                                user: dict = Depends(require_permission(LAB_MASTER))):
    return _write(instruments.retire_mapping, instrument_id, body.instrument_code, body.reason,
                  actor=user, request_meta=request_meta(request))
