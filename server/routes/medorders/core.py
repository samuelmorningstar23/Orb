"""Medication orders, record-mode reads and the core writes: endpoints under
/api/medorders.

Empty until medorders-4 (W5-E). The package router in
routes/medorders/__init__.py adds the /api/medorders prefix and the
ORB_MED_ORDER_STORE gate; this router has neither, so every endpoint added
here inherits both.

Planned endpoints (docs/his_design/medorders.json, section 8):
  GET  /patient/{patient_id}       require_patient_access(patient.read_all,
                                   audit_action='med_orders_viewed'); linked
                                   orders plus unlinked chart rows shown through
                                   services.medorders.core.legacy_status_for
                                   (read-only: a GET never adopts)
  GET  /{id}, /{id}/events         audited 404 out of scope, log_phi_read
  POST ''                          create a draft (order.write or
                                   medication.write) or create and sign
                                   (medication.prescribe); Idempotency-Key;
                                   needs require_med_order_lifecycle
  POST /{id}/sign                  medication.prescribe, safety_ack,
                                   expected_version; lifecycle
  POST /{id}/cancel                a draft only until ruling MO-R2; lifecycle
  GET  /queue/to-sign              with the 'Entered by a nurse' list
  GET  /admin/parity               admin.overview;
                                   services.medorders.adoption.parity_report
  POST /admin/adopt                admin.overview;
                                   services.medorders.adoption.adopt_legacy
                                   (trigger='admin'), the one record-mode write
"""
from fastapi import APIRouter

router = APIRouter()
