"""Laboratory (LIS): the /api/lab route package.

Created empty in wave 1 of the hospital-system build (3 October 2026) and
registered once in main.py, so each later LIS increment adds endpoints to its
own sub-module here instead of editing main.py.

BEHIND ORB_LAB (default off), with a state the other modules do not have:
'draining', when the switch is off but open laboratory work remains (ruling
LIS-R21; services/lab_module registers the drain check with
config.register_module_drain_check). So the gate sits on each SUB-router, not
on this package router:
  * every sub-router carries require_lab_module(allow_draining=True): 404 when
    the module is off, reachable while on or draining;
  * a route that STARTS new work (order intake, collection, analyser intake)
    adds require_lab_module() as well, so it 404s while draining;
  * criticals.py carries require_lab_critical_surface instead, which also
    stays open when interfaced criticals are on without ORB_LAB (LIS-R22).
tests/test_his_route_skeletons.py checks every /api/lab route carries one of
these gates.

Rules for every endpoint added here (design: docs/his_design/lis.json):
  * a plain `def` handler, or async with run_db for every DB touch;
  * lab permissions from permissions.py (lab.read, lab.collect, lab.receive,
    lab.result, lab.authorise, lab.master, lab.critical_log, lab.critical_ack,
    lab.interface, ingest.reconcile);
  * ward-facing endpoints scope with department_scope and scope_patient_ids_sql
    (404, never 403); laboratory-facing ones are hospital-wide and call
    log_phi_read;
  * clinical writes audit as clinical_access inside the transaction;
    configuration as clinical_config (fail-closed).

Sub-modules, one per concern, each exporting `router` (no prefix):
  master     catalogue, tests, panels, ranges, critical limits, specimen types
  orders     laboratory-mediated cancel, exceptions
  specimens  collection list and labels, collect, scan, receive, reject, recollect
  results    worklists, entry, technical validation, authorisation, amend, withdraw
  criticals  critical results: list, callback log, ward acknowledgement
  reports    laboratory reports
  tat        turnaround times
  analysers  analyser registry, test mapping, instrument intake
"""
from fastapi import APIRouter, Depends

import config
from permissions import LAB_READ, require_permission
from routes.lab import analysers, criticals, master, orders, reports, results, specimens, tat
from routes.opt_in_gates import require_lab_module

router = APIRouter(prefix="/api/lab", tags=["Laboratory"])


@router.get("/meta", dependencies=[Depends(require_lab_module(allow_draining=True))])
def lab_meta(user: dict = Depends(require_permission(LAB_READ))):
    """What this site's laboratory module is set to. No patient data."""
    return {
        "module": "lab",
        "state": config.module_state("lab"),
        "features": {f: config.feature_enabled(f) for f in (
            "lab_analyser_listener", "result_quarantine", "result_corrections",
            "oru_order_link", "lab_critical_interfaced", "lab_portal_reports")},
        "settings": {
            "require_distinct_authoriser": config.LAB_REQUIRE_DISTINCT_AUTHORISER,
            "collection_backdate_minutes": config.LAB_COLLECTION_BACKDATE_MINUTES,
            "critical_sweep_seconds": config.LAB_CRITICAL_SWEEP_S,
        },
    }


for _sub in (master, orders, specimens, results, criticals, reports, tat, analysers):
    router.include_router(_sub.router)
