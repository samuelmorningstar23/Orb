"""Bed board (ADT): endpoints under /api/adt.

Empty until adt-4a (W4-B). The package router in routes/adt/__init__.py adds the
/api/adt prefix and the ORB_ADT gate; this router has neither, so every
endpoint added here inherits both.

Planned endpoints (docs/his_design/adt.json, section 7):
  GET  /board?ward_id=                      adt.read (identity only for in-scope occupants, phi_access audit)
  GET  /awaiting-bed                        adt.read (two lists: awaiting a bed, bed text not linked)
  GET  /beds/available?ward_id=             adt.read

Bed status is derived in the precedence the design states (retired, occupied, reserved, out of service, needs cleaning, cleaning, available).
"""
from fastapi import APIRouter

router = APIRouter()
