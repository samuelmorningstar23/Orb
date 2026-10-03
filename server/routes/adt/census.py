"""Census (ADT): endpoints under /api/adt.

Empty until adt-13a, adt-13b (W7-C). The package router in routes/adt/__init__.py adds the
/api/adt prefix and the ORB_ADT gate; this router has neither, so every
endpoint added here inherits both.

Planned endpoints (docs/his_design/adt.json, section 7):
  GET  /census?from=&to=&ward_id=           adt.read
  GET  /census.csv                          adt.read

Hospital-local days (services.hospital_time); any day before a ward's ledger_complete_from reads 'insufficient data', never zero.
"""
from fastapi import APIRouter

router = APIRouter()
