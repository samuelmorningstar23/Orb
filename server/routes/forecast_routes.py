"""Orb Forecast — API Routes

Endpoints:
  GET /api/forecast/hospital          — 7-day hospital flow forecast
  GET /api/forecast/patient/{id}      — Per-patient LoS prediction
  GET /api/forecast/capacity          — Bed capacity projections
  GET /api/forecast/discharge-board   — Discharge readiness ranking
  GET /api/forecast/admission-patterns — Historical admission analysis
  GET /api/forecast/appointments      Booked appointments, caller's department only
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from permissions import (
    department_scope,
    facility_scope,
    log_phi_read,
    require_patient_access,
    require_permission,
)
from services.forecast_service import (
    get_admission_patterns,
    get_appointments,
    get_bed_capacity_forecast,
    get_discharge_board,
    predict_hospital_flow,
    predict_patient_los,
)

router = APIRouter(prefix="/api/forecast", tags=["forecast"])


_HOSPITAL_FORECAST_USER_DEFAULT = Depends(require_permission("forecast.read"))


@router.get("/hospital")
def hospital_forecast(
    days: int = 7,
    user: dict = _HOSPITAL_FORECAST_USER_DEFAULT,
):
    """Get hospital-level admission/discharge/occupancy forecast.

    The series leads with a MEASURED day 0 (`is_current: true`) — today's census
    and today's actual admissions/discharges. Without it the panel's first chart
    point was tomorrow's prediction sitting next to a Current Census card that
    said something else.
    """
    try:
        return predict_hospital_flow(days_ahead=max(1, min(days, 14)), include_current_day=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Forecast error: {str(e)}") from e


_PATIENT_FORECAST_USER_DEFAULT = Depends(
    require_patient_access("forecast.read", audit_action="patient_forecast_viewed")
)


@router.get("/patient/{patient_id}")
def patient_forecast(
    patient_id: str,
    user: dict = _PATIENT_FORECAST_USER_DEFAULT,
):
    """Get per-patient LoS prediction and discharge probability."""
    try:
        result = predict_patient_los(patient_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prediction error: {str(e)}") from e


_CAPACITY_FORECAST_USER_DEFAULT = Depends(require_permission("forecast.read"))


@router.get("/capacity")
def capacity_forecast(
    user: dict = _CAPACITY_FORECAST_USER_DEFAULT,
):
    """Get current + projected bed capacity by ward."""
    try:
        return get_bed_capacity_forecast()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Capacity error: {str(e)}") from e


_DISCHARGE_BOARD_USER_DEFAULT = Depends(require_permission("forecast.read"))


@router.get("/discharge-board")
def discharge_board(
    request: Request,
    user: dict = _DISCHARGE_BOARD_USER_DEFAULT,
) -> list:
    """Patients sorted by discharge likelihood — the caller's department only.

    This route bound `user` and then never referenced it: get_discharge_board()
    took no argument, so the service could not scope what it was never told, and
    its query had no department predicate at all. Any forecast.read holder — the
    permission a ward NURSE has — opened the discharge-readiness board and got
    name, ward, bed, diagnosis, age and sex for every admitted patient in the
    hospital (audit 2026-08-05). Nobody thought of it as a PHI query because it
    is a dashboard rather than a search.

    The scope arrives from the authenticated user and is never re-derived
    inside the service or read off the request. Since multisite-8 (W12-A) the
    department AND the caller's facilities (permissions.facility_scope) are
    explicit arguments of forecast_service.get_discharge_board and the filter
    is in its query, so a department-less account at one facility no longer
    reads another facility's admitted patients.
    """
    dept = department_scope(user)
    try:
        board = get_discharge_board(dept, facilities=facility_scope(user))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Board error: {str(e)}") from e

    if board:
        log_phi_read(
            request=request,
            user=user,
            action_type="discharge_board_viewed",
            summary_message=(
                f"Discharge readiness board viewed ({len(board)} patients) for "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={
                "result_count": len(board),
                "patient_ids": [r.get("patient_id") for r in board],
            },
        )

    return board


_ADMISSION_PATTERNS_USER_DEFAULT = Depends(require_permission("forecast.read"))


@router.get("/admission-patterns")
def admission_patterns(
    user: dict = _ADMISSION_PATTERNS_USER_DEFAULT,
):
    """Get historical admission pattern analysis."""
    try:
        return get_admission_patterns()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Pattern error: {str(e)}") from e


_APPOINTMENTS_SCHEDULE_USER_DEFAULT = Depends(require_permission("forecast.read"))


@router.get("/appointments")
def appointments_schedule(
    request: Request,
    days: int = 7,
    user: dict = _APPOINTMENTS_SCHEDULE_USER_DEFAULT,
) -> list:
    """Booked appointments for the next N days, the caller's department only.

    Every row names a patient, so this is a PHI list read: it was unscoped and
    unaudited, and any forecast.read holder (a ward nurse has it) saw every
    booking in the hospital. The scope comes from the authenticated user and is
    passed into the query, never read off the request. With more than one
    facility on record only bookings at the caller's facilities are listed
    (multisite-8).
    """
    dept = department_scope(user)
    try:
        rows = get_appointments(
            days_ahead=max(1, min(days, 14)), department=dept, facilities=facility_scope(user)
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Appointments error: {str(e)}") from e

    if rows:
        log_phi_read(
            request=request,
            user=user,
            action_type="appointments_schedule_viewed",
            summary_message=(
                f"Appointment schedule viewed ({len(rows)} bookings) for "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={
                "result_count": len(rows),
                "patient_ids": sorted({r["patient_id"] for r in rows}),
            },
        )
    return rows
