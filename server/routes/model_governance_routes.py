"""Orb — Model Governance Routes. Exposes the AI/ML model registry so the admin
console can show provenance, training-data lineage, checkpoint presence, and
validation status for every model surface (the honesty layer)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import get_current_user
from permissions import require_permission
from services.model_governance_service import list_models, seed_default_models

router = APIRouter(prefix="/api/admin", tags=["model-governance"])

# Compact provenance fields safe to expose to any authenticated clinician for the
# per-output provenance chips (no internal metrics/paths — just what the chip shows).
_CHIP_FIELDS = ("model_key", "version", "task", "training_data",
                "validated", "checkpoint_present", "is_active")


@router.get("/models")
def get_models(user: dict = Depends(require_permission("admin.overview"))):
    """Return the model registry with provenance + validation status."""
    return {"models": list_models()}


@router.get("/models/provenance")
def get_model_provenance(user: dict = Depends(get_current_user)):
    """Compact per-model provenance for output chips.

    Readable by any authenticated user (not just admins) so a clinician can see,
    inline on an AI output, which model+version produced it and whether that model
    is validated. Detail (metrics, lineage) stays in the admin Model Governance
    console, which this chip deep-links to.
    """
    return {"models": [{k: m.get(k) for k in _CHIP_FIELDS} for m in list_models()]}


@router.post("/models/reseed")
def reseed_models(user: dict = Depends(require_permission("admin.overview"))):
    """Re-sync the registry with the known model surfaces (recomputes checkpoint
    presence from disk)."""
    n = seed_default_models()
    return {"ok": True, "count": n}
