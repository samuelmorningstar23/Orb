"""Orb Hospital OS — EHR Downtime Mode routes.

Read/refresh a durable, local, read-only snapshot of the essential clinical
picture so the ward keeps a safe view when the EHR/network/LLM is down. The
capture + read paths are pure DB (no Ollama, no network), which is exactly what
makes this usable during an outage — see services/downtime_service.py.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from permissions import require_patient_access, require_permission
from services.downtime_service import (
    capture_snapshot,
    capture_all_admitted,
    get_latest_snapshot,
    list_snapshots,
    serve_ward_pack,
)
from services.audit_service import log_audit_event

router = APIRouter(prefix="/api/downtime", tags=["downtime"])


@router.post("/capture/{patient_id}")
def capture(patient_id: str,
            # require_patient_access, not require_permission: this returns the
            # FULL snapshot, so an unscoped check let any vitals.read holder
            # read a cross-ward patient's whole clinical picture through the
            # capture side door (DEMO_READINESS §15.1's neighbourhood).
            user: dict = Depends(require_patient_access("vitals.read"))):
    """Freeze the current essential clinical picture for one patient into a local
    snapshot and return it."""
    try:
        result = capture_snapshot(patient_id, created_by=user.get("id", "auto"))
    except ValueError:
        # Byte-identical to require_patient_access's scope refusal: an unknown
        # id and an out-of-ward id must be indistinguishable — the established
        # posture on the chart, timeline, orders and eMAR.
        raise HTTPException(status_code=404, detail="Patient not found")

    log_audit_event(
        correlation_id=f"downtime-capture-{result['id']}",
        event_category="clinical_access",
        action_type="downtime_snapshot_captured",
        outcome="success",
        summary_message=f"Downtime snapshot #{result['id']} captured for {patient_id}",
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        patient_id=patient_id,
        target_type="patient_snapshot",
        target_id=str(result["id"]),
    )
    return result


@router.post("/capture-all")
def capture_all(user: dict = Depends(require_permission("admin.overview"))):
    """Snapshot every admitted patient (pre-downtime drill / scheduled job)."""
    result = capture_all_admitted(created_by=user.get("id", "auto"))
    log_audit_event(
        correlation_id=f"downtime-capture-all-{uuid.uuid4()}",
        event_category="clinical_access",
        action_type="downtime_snapshot_captured_all",
        outcome="success",
        summary_message=(
            f"Downtime drill: captured {result['count']}/{result['total_admitted']} "
            "admitted patients"
        ),
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        metadata={
            "count": result["count"],
            "total_admitted": result["total_admitted"],
            "failed": result["failed"],
        },
    )
    return result


@router.get("/{patient_id}/latest")
def latest(patient_id: str,
           # Scope-checked, NOT read-audited. This is the DEGRADED-MODE fallback
           # view; wiring a fail-closed audit write into the surface whose whole
           # job is to work when things are broken is exactly backwards.
           user: dict = Depends(require_patient_access("vitals.read"))):
    """Return the most recent snapshot for a patient (the downtime fallback view)."""
    snap = get_latest_snapshot(patient_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="No snapshot captured for this patient")
    return snap


@router.get("/{patient_id}/history")
def history(patient_id: str, limit: int = 50,
            user: dict = Depends(require_patient_access("vitals.read"))):
    """Return the snapshot history (newest first) for a patient."""
    return {"snapshots": list_snapshots(patient_id, limit=limit)}


@router.get("/{patient_id}/ward-pack", response_class=HTMLResponse)
def ward_pack(patient_id: str,
              # Same posture as /latest: scope-checked, NOT read-audited — this
              # is the degraded-mode surface (see the comment there). Resolving
              # the ward from a scope-checked patient id means no caller ever
              # names a ward directly, so the pack inherits the one per-patient
              # scope derivation instead of growing its own.
              user: dict = Depends(require_patient_access("vitals.read"))):
    """Serve the ward-level downtime print pack covering this patient's ward,
    as a self-contained printable HTML document. A stale pack, or one missing
    a patient admitted since it was written, is rebuilt first (pure SQLite);
    anyone it still lacks is listed on its first page. The same artifact lives
    on local disk in downtime_exports/ so it stays retrievable with the app down."""
    try:
        result = serve_ward_pack(patient_id, created_by=user.get("id", "auto"), user=user)
    except ValueError:
        raise HTTPException(status_code=404, detail="Patient not found")
    if result["captured"]:
        # The rebuild WROTE snapshots, so it is recorded, unlike the read
        # itself. data_management is fail-soft on purpose: a failed audit
        # write must not take the pack away from the degraded-mode surface.
        log_audit_event(
            correlation_id=f"downtime-ward-pack-{uuid.uuid4()}",
            event_category="data_management",
            action_type="downtime_ward_pack_rebuilt",
            outcome="success",
            summary_message=(
                f"Ward pack for {result['ward']} rebuilt on request: "
                f"{result['captured']} snapshot(s) re-captured"
            ),
            actor_user_id=user.get("id"),
            actor_role=user.get("role"),
            patient_id=patient_id,
            target_type="ward_pack",
            target_id=result["ward"],
            metadata={"ward": result["ward"], "captured": result["captured"],
                      "missing": len(result["missing"])},
        )
    if result["html"] is None:
        raise HTTPException(status_code=404,
                            detail="No ward pack generated for this ward yet")
    return HTMLResponse(content=result["html"])
