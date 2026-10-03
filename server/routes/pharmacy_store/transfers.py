"""Pharmacy-internal transfers: endpoints under /api/pharmacy-store.

Empty until grn-11 (W5-G). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  transfers, plus dispatch, receive, cancel   the receiver must differ from the dispatcher

In-transit units are dispensable nowhere.
"""
from fastapi import APIRouter

router = APIRouter()
