"""Offline accounting exports and attributable doctor statements; no transmit."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, StrictInt

from permissions import (FINANCE_TOOLS_APPROVE, FINANCE_TOOLS_EXPORT, FINANCE_TOOLS_READ,
                         FINANCE_TOOLS_WRITE, require_permission)
from routes.opt_in_gates import module_gate
from services import finance_tools_service as svc
from services.reporting_finance_common import WorkbenchError

logger = logging.getLogger("orb.finance_tools")
router = APIRouter(prefix="/api/finance-tools", tags=["Local finance tools"], dependencies=[Depends(module_gate("finance-tools"))])
READ = require_permission(FINANCE_TOOLS_READ)
WRITE = require_permission(FINANCE_TOOLS_READ, FINANCE_TOOLS_WRITE)
APPROVE = require_permission(FINANCE_TOOLS_READ, FINANCE_TOOLS_APPROVE)
EXPORT = require_permission(FINANCE_TOOLS_READ, FINANCE_TOOLS_EXPORT)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PeriodIn(StrictBody):
    facility_id: StrictInt = 1
    from_day: str
    to_day: str


class JournalExportIn(PeriodIn):
    idempotency_key: str


class MappingIn(StrictBody):
    facility_id: StrictInt = 1
    account_role: str
    account_code: str
    account_name: str


class AttributionIn(StrictBody):
    facility_id: StrictInt = 1
    doctor_id: str
    reason: str
    version: StrictInt | None = None


class RuleIn(StrictBody):
    facility_id: StrictInt = 1
    doctor_id: str
    selector_type: Literal["service", "category", "all"]
    selector_value: str
    mode: Literal["percentage", "fixed"]
    basis: Literal["gross_ex_tax", "net_ex_tax"]
    recognition: Literal["billed", "fully_collected"]
    effective_from: str
    effective_to: str | None = None
    percent_bp: StrictInt | None = None
    fixed_paise: StrictInt | None = None


class PayoutPeriodIn(PeriodIn):
    doctor_id: str | None = None


class StatementIn(PayoutPeriodIn):
    idempotency_key: str
    deduction_paise: StrictInt = 0
    deduction_reason: str | None = None


class PaidIn(StrictBody):
    payment_reference: str


def _call(fn, **kwargs):
    try:
        return fn(**kwargs)
    except WorkbenchError as exc:
        raise HTTPException(status_code=exc.status, detail={"error": exc.code, "message": str(exc)}) from exc
    except Exception:
        logger.exception("Local finance operation failed")
        raise HTTPException(status_code=500, detail={"error": "finance_tools_failed", "message": "The finance operation could not be completed."}) from None


@router.get("/meta")
def metadata(facility_id: int = 1, user=Depends(READ)):
    return _call(svc.metadata, user=user, facility_id=facility_id)


@router.put("/account-mappings")
def mapping(body: MappingIn, user=Depends(WRITE)):
    return _call(svc.set_mapping, user=user, **body.model_dump())


@router.post("/journal/preview")
def journal_preview(body: PeriodIn, user=Depends(READ)):
    return _call(svc.journal_preview, user=user, **body.model_dump())


@router.get("/journal/exports")
def journal_exports(facility_id: int = 1, user=Depends(READ)):
    return _call(svc.list_journal_exports, user=user, facility_id=facility_id)


@router.post("/journal/exports")
def journal_export(body: JournalExportIn, user=Depends(EXPORT)):
    return _call(svc.create_journal_export, user=user, **body.model_dump())


@router.get("/journal/exports/{export_id}.csv")
def journal_csv(export_id: str, user=Depends(EXPORT)):
    content = _call(svc.journal_csv, user=user, export_id=export_id)
    return Response(content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="orb-journal.csv"', "Cache-Control": "no-store"})


@router.get("/charges")
def charges(from_day: str, to_day: str, facility_id: int = 1, user=Depends(READ)):
    return _call(svc.charges, user=user, facility_id=facility_id, from_day=from_day, to_day=to_day)


@router.put("/charges/{charge_id}/doctor")
def attribute(charge_id: int, body: AttributionIn, user=Depends(WRITE)):
    return _call(svc.attribute_charge, user=user, charge_id=charge_id, **body.model_dump())


@router.post("/payout-rules")
def rule(body: RuleIn, user=Depends(WRITE)):
    return _call(svc.create_rule, user=user, **body.model_dump())


@router.post("/payout-rules/{rule_id}/retire")
def retire_rule(rule_id: int, user=Depends(WRITE)):
    return _call(svc.retire_rule, user=user, rule_id=rule_id)


@router.post("/payouts/preview")
def payout_preview(body: PayoutPeriodIn, user=Depends(READ)):
    return _call(svc.payout_preview, user=user, **body.model_dump())


@router.get("/payout-statements")
def statements(facility_id: int = 1, user=Depends(READ)):
    return {"statements": _call(svc.list_statements, user=user, facility_id=facility_id)}


@router.post("/payout-statements")
def create_statement(body: StatementIn, user=Depends(WRITE)):
    return _call(svc.create_statement, user=user, **body.model_dump())


@router.get("/payout-statements/{statement_id}.csv")
def payout_csv(statement_id: str, user=Depends(EXPORT)):
    content = _call(svc.statement_csv, user=user, statement_id=statement_id)
    return Response(content, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="orb-doctor-statement.csv"', "Cache-Control": "no-store"})


@router.get("/payout-statements/{statement_id}")
def statement(statement_id: str, user=Depends(READ)):
    return _call(svc.get_statement, user=user, statement_id=statement_id)


@router.post("/payout-statements/{statement_id}/approve")
def approve(statement_id: str, user=Depends(APPROVE)):
    return _call(svc.approve_statement, user=user, statement_id=statement_id)


@router.post("/payout-statements/{statement_id}/paid")
def paid(statement_id: str, body: PaidIn, user=Depends(require_permission(FINANCE_TOOLS_READ, FINANCE_TOOLS_WRITE, FINANCE_TOOLS_APPROVE))):
    return _call(svc.mark_statement_paid, user=user, statement_id=statement_id, **body.model_dump())


@router.post("/payout-statements/{statement_id}/cancel")
def cancel(statement_id: str, user=Depends(WRITE)):
    return _call(svc.cancel_statement, user=user, statement_id=statement_id)


