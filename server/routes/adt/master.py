"""Bed master (ADT): endpoints under /api/adt.

Empty until adt-1, adt-4b (W2-B, W5-B). The package router in routes/adt/__init__.py adds the
/api/adt prefix and the ORB_ADT gate; this router has neither, so every
endpoint added here inherits both.

Planned endpoints (docs/his_design/adt.json, section 7):
  GET  /master                              adt.read
  POST/PATCH /master/wards, /master/rooms, /master/beds, /master/bed-classes   bed.master
  POST /master/beds/{bed_id}/retire         bed.master
  POST /master/wards/{ward_id}/register-complete   bed.master
  POST /master/import-site-config           bed.master
  GET  /master/reconciliation, POST /master/reconciliation/link   bed.master

Master edits are audited as category 'facilities' with a bed_master_events row; a ward code or bed label is immutable once referenced (retire and recreate).
"""
from fastapi import APIRouter

router = APIRouter()
