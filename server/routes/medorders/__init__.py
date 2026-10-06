"""Medication orders: the /api/medorders route package.

Created empty in wave 4 of the hospital-system build (medorders-1, W4-E) and
registered once in main.py, so each later medication-order increment adds
endpoints to its own sub-module here instead of editing main.py.

THE WHOLE PACKAGE IS BEHIND ORB_MED_ORDER_STORE (default off). The gate is a
router-level dependency on this package router, so every endpoint any
sub-module adds inherits it and answers 404 {error: 'feature_disabled',
feature: 'med_order_store'} while the store is off (after a 401 for a caller
with no session). tests/test_medorder_registries.py checks every /api/medorders
route carries it first, and that every handler is a plain def.

WRITES NEED THE LIFECYCLE TOO (design section 8). verify, lifecycle and supply
carry require_med_order_lifecycle on their own routers, so each of their
endpoints also answers 404 {feature: 'med_order_lifecycle'} while
ORB_MED_ORDER_LIFECYCLE is off. core holds reads that work in record mode; a
write route added to core adds Depends(require_med_order_lifecycle) itself
(the admin adopt action is the one record-mode write: adoption is what record
mode does at boot).

Rules for every endpoint added here (design: docs/his_design/medorders.json):
  * a plain `def` handler (FastAPI threadpools it; no sync SQLite on the loop);
  * permissions from permissions.py: medication.prescribe for sign, hold,
    resume, stop and live cancel (ruling MO-R2 (a)); medication.verify for
    verify and reject (MO-R4 (a), added in W5-E); order.write or
    medication.write for a draft; admin.overview for parity and adopt;
  * scope through permissions.require_patient_access for /patient/{patient_id}
    routes and an audited 404 (patient_scope_denied, then 404, the
    emar_routes._enforce_medication_scope model) for routes keyed by an order
    id; queues filtered with permissions.scope_patient_ids_sql;
  * every write goes through services/medorders: the safety gate runs OUTSIDE
    the write transaction (enforce_medication_safety with exclude_med_id),
    then one db_session(immediate=True) re-checks the version and the screen
    fingerprint and writes the order, its event, the ward row and the
    clinical_access audit together;
  * after commit, realtime.schedule_patient_vitals('medorders:update', ...)
    with ids and status only, never a drug name.

Sub-modules, one per concern, each exporting `router` (no prefix):
  core       list, get, events, create, sign, draft cancel, the to-sign queue,
             admin parity and adopt (W5-E)
  verify     the verification queue, verify and reject (W6-D)
  lifecycle  hold, resume, stop (W6-D)
  supply     request from pharmacy (W7-E)
"""
from fastapi import APIRouter, Depends

from routes.opt_in_gates import feature_gate

#: The store switch, at request time (404 feature_disabled while off).
require_med_order_store = feature_gate("med_order_store")
#: The lifecycle switch, for every write route (404 while off; the lifecycle
#: is itself off whenever the store is).
require_med_order_lifecycle = feature_gate("med_order_lifecycle")

# Imported after the gates exist: the sub-modules import them from here.
from routes.medorders import core, lifecycle, supply, verify  # noqa: E402

router = APIRouter(
    prefix="/api/medorders",
    tags=["Medication orders"],
    dependencies=[Depends(require_med_order_store)],
)

for _sub in (core, verify, lifecycle, supply):
    router.include_router(_sub.router)
