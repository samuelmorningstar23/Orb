"""Pharmacy masters: endpoints under /api/pharmacy-store.

Empty until grn-5, grn-6 (W3-C, W5-C). The package router in routes/pharmacy_store/__init__.py
adds the /api/pharmacy-store prefix and the ORB_PHARMACY_STORE gate; this
router has neither, so every endpoint added here inherits both.

Planned endpoints (docs/his_design/grn.json, section E):
  drugs, drugs/{drug_id}/classification-requests   read / write / approve tiers
  stores, par-levels                        approve tier for stores, write for par levels
  vendors, vendors/{code}/drug-licence      write tier, through the shared vendor core

A classification downgrade needs a second approve-tier person and is refused while vault stock exists (GRN-R20). Identity edits on a drug with orders or stock are refused (ruling GRN-RS1, held).
"""
from fastapi import APIRouter

router = APIRouter()
