"""Bed moves and transfers (ADT): endpoints under /api/adt.

Empty until adt-5, adt-6a, adt-6b, adt-6c (W5-B, W6-C, W7-C). The package router in routes/adt/__init__.py adds the
/api/adt prefix and the ORB_ADT gate; this router has neither, so every
endpoint added here inherits both.

Planned endpoints (docs/his_design/adt.json, section 7):
  POST /admissions/{admission_id}/bed, /bed-change   adt.bed or patient.write
  POST /transfers                           adt.transfer or patient.write
  POST /bed-swaps                           adt.bed or patient.write (both admissions in scope)
  POST /movements/{movement_id}/correct     adt.bed or patient.write

Every move carries an Idempotency-Key and expected_bed_id (409 'state_changed' on a mismatch) and writes its patient_movement audit in the same transaction. A ward clerk never changes the treating department (ruling ADT-S8, held).
"""
from fastapi import APIRouter

router = APIRouter()
