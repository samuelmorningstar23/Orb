"""HTTP error mapping shared only by the three local operations routers."""
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict
from services.audit_service import AuditWriteError
from services.local_operations_common import OperationsError
from services.offline_workflows import WorkflowError


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def call(fn, *args, request=None, **kwargs):
    if request is not None:
        kwargs["request_meta"] = {"request_path": request.url.path, "http_method": request.method,
                                  "ip_address": request.client.host if request.client else None,
                                  "user_agent": request.headers.get("user-agent")}
    try:
        return fn(*args, **kwargs)
    except (OperationsError, WorkflowError) as exc:
        raise HTTPException(exc.status, detail={"error": exc.code, "message": str(exc)}) from exc
    except AuditWriteError as exc:
        raise HTTPException(503, detail={"error": "audit_unavailable", "message": "The audit record could not be saved. Nothing was changed; try again."}) from exc
