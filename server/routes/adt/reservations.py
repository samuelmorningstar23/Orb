"""Bed reservations (ADT): endpoints under /api/adt.

Empty until adt-12 (W7-D). The package router in routes/adt/__init__.py adds the
/api/adt prefix and the ORB_ADT gate; this router has neither, so every
endpoint added here inherits both.

Planned endpoints (docs/his_design/adt.json, section 7):
  POST /reservations                        adt.bed or patient.write
  POST /reservations/{reservation_id}/release   adt.bed or patient.write

Expiry from config.ADT_RESERVATION_MINUTES and ADT_RESERVATION_ELECTIVE_MINUTES (ADT-P9).
"""
from fastapi import APIRouter

router = APIRouter()
