"""End of stay and reopen (ADT): endpoints under /api/adt.

Empty until adt-8a, adt-8b (W3-B, W4-B). The package router in routes/adt/__init__.py adds the
/api/adt prefix and the ORB_ADT gate; this router has neither, so every
endpoint added here inherits both.

Planned endpoints (docs/his_design/adt.json, section 7):
  POST /admissions/{admission_id}/end       discharge.generate plus scope, typed MRN read-back (confirm_mrn)
  POST /admissions/{admission_id}/reopen    discharge.generate plus scope, reason and confirm_mrn

No ADT permission ends a stay. Death, LAMA and DAMA stay doctor-only (ruling ADT-S3, held). Open escalation chains are listed, never closed (R009, held).
"""
from fastapi import APIRouter

router = APIRouter()
