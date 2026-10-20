"""Medication orders, the times an order is due: endpoints under /api/medorders
(medorders-19, W8-I; ruling MO-R8 (W117), held for Samuel).

The package router adds the /api/medorders prefix and the ORB_MED_ORDER_STORE
gate. Like core, this router carries no gate of its own: the reads work in
record mode, and the one write adds the ORB_MED_ORDER_LIFECYCLE gate on its own
route (set times are what the lifecycle's board uses). Every handler is a plain
def.

  GET  /dose-times
      The hospital's signed dose times (server/site_config/dose_times.json):
      {available, version, fingerprint, signed, times}. No patient data.
      Anyone who reads charts, prescribes or verifies.

  GET  /patient/{patient_id}/schedules
      patient.read_all through require_patient_access (audit
      med_order_schedules_viewed): for each of the patient's live or draft
      orders, the times set for it, the hospital's times for its frequency,
      and how many doses a day the frequency means. {patient_id, site_times,
      orders: [...]}.

  PUT  /orders/{order_id}/schedule   {times, expected_version?}
      medication.prescribe; lifecycle. Sets the times the order is due, or
      clears them with an empty list (the board then uses the hospital's
      times for the frequency, or the times worked out from it). An order
      outside the caller's department is an audited 404. Refusals use
      core.http_detail: order_not_live, prn_has_no_set_times,
      frequency_not_daily, invalid_times, stale_version.
"""

from database import db_session
from fastapi import APIRouter, Depends, HTTPException, Request
from permissions import (
    MEDICATION_PRESCRIBE,
    MEDICATION_VERIFY,
    PATIENT_READ_ALL,
    require_any_permission,
    require_patient_access,
    require_permission,
)
from pydantic import BaseModel, ConfigDict
from services.medorders import core as store
from services.medorders import queries
from services.medorders import schedule as schedule_service

from routes.medorders import require_med_order_lifecycle
from routes.medorders.core import (
    ORDER_NOT_FOUND,
    PATIENT_NOT_FOUND,
    _emit,
    _med_order_patient_in_scope_or_404,
    _refusal,
    _request_meta,
)

router = APIRouter()

#: Orders whose times can still matter: drafts and live orders.
_OPEN_STATUSES = ("draft", "prescribed", "verified", "held")


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 'HH:MM' on the hospital's 24-hour clock, one per dose a day; [] clears.
    times: list[str]
    expected_version: int | None = None


_DOSE_TIMES_USER_DEFAULT = Depends(
    require_any_permission(PATIENT_READ_ALL, MEDICATION_PRESCRIBE, MEDICATION_VERIFY)
)


@router.get("/dose-times")
def dose_times(
    user: dict = _DOSE_TIMES_USER_DEFAULT,
):
    """The hospital's signed dose times, or {available: false}."""
    return schedule_service.site_times_view()


_PATIENT_SCHEDULES_USER_DEFAULT = Depends(
    require_patient_access(PATIENT_READ_ALL, audit_action="med_order_schedules_viewed")
)


@router.get("/patient/{patient_id}/schedules")
def patient_schedules(
    patient_id: str,
    user: dict = _PATIENT_SCHEDULES_USER_DEFAULT,
):
    """The set times of the patient's draft and live orders."""
    site = schedule_service.site_dose_times()
    with db_session() as db:
        if db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail=PATIENT_NOT_FOUND)
        marks = ",".join("?" for _ in _OPEN_STATUSES)
        orders = [
            dict(r)
            for r in db.execute(
                f"SELECT id, status, version, frequency, is_prn FROM med_orders "
                f"WHERE patient_id = ? AND status IN ({marks}) ORDER BY created_at, id",
                (patient_id, *_OPEN_STATUSES),
            ).fetchall()
        ]
        authored = schedule_service.current_schedules_in_txn(db, [o["id"] for o in orders])
    out = []
    for order in orders:
        site_times = schedule_service.site_times_for(order["frequency"], site)
        out.append(
            {
                "med_order_id": order["id"],
                "status": order["status"],
                "version": order["version"],
                "frequency": order["frequency"],
                "doses_per_day": (
                    None if order["is_prn"] else schedule_service.doses_per_day(order["frequency"])
                ),
                "schedule": authored.get(order["id"]),
                "site_times": list(site_times) if site_times else None,
            }
        )
    return {
        "patient_id": patient_id,
        "site_times": schedule_service.site_times_view(site),
        "orders": out,
    }


_SET_ORDER_SCHEDULE_USER_DEFAULT = Depends(require_permission(MEDICATION_PRESCRIBE))


@router.put("/orders/{order_id}/schedule", dependencies=[Depends(require_med_order_lifecycle)])
def set_order_schedule(
    order_id: str,
    body: ScheduleRequest,
    request: Request,
    user: dict = _SET_ORDER_SCHEDULE_USER_DEFAULT,
):
    """The prescriber sets the times this order is due. Answers {order,
    schedule, replayed}."""
    _med_order_patient_in_scope_or_404(order_id, user, request)
    try:
        result = schedule_service.set_schedule(
            order_id,
            body.times,
            actor_id=user["id"],
            actor_role=user.get("role"),
            expected_version=body.expected_version,
            request_meta=_request_meta(request),
        )
    except store.MedOrderNotFound:
        raise HTTPException(status_code=404, detail=ORDER_NOT_FOUND) from None
    except store.MedOrderError as e:
        raise _refusal(e, user, request) from e
    if not result["replayed"]:
        _emit(result["order"])
    return {
        "order": queries.serialize_order(result["order"]),
        "schedule": result["schedule"],
        "replayed": result["replayed"],
    }
