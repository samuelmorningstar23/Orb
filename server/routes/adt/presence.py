"""Leave and absconded (ADT): endpoints under /api/adt.

Empty until adt-10, adt-11 (W7-D). The package router in routes/adt/__init__.py adds the
/api/adt prefix and the ORB_ADT gate; this router has neither, so every
endpoint added here inherits both.

Planned endpoints (docs/his_design/adt.json, section 7):
  POST /admissions/{admission_id}/leave, /leave-end, /absconded, /returned   adt.presence or patient.write

Ruling ADT-S7 is held: admins and doctors only until Samuel confirms; nurses and clerks get nothing here until then.
"""
from fastapi import APIRouter

router = APIRouter()
