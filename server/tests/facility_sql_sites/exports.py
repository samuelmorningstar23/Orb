"""SQL-site registry: downtime packs and exports.

Owned by W12-D (multisite-15). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

SITES = {
    # routes/fhir_export_routes.py
    ('routes/fhir_export_routes.py', '_authorize_patient_access'): blind('multisite-15'),
    # services/downtime_service.py
    ('services/downtime_service.py', '_admitted_for_packs'): blind('multisite-15'),
    ('services/downtime_service.py', 'capture_all_admitted'): blind(
        'multisite-15',
        "Every admitted patient's pack in one folder."),
    ('services/downtime_service.py', 'capture_snapshot'): blind('multisite-15'),
    ('services/downtime_service.py', 'read_latest_ward_pack'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/downtime_service.py', 'run_nightly_fhir_self_export'): blind(
        'multisite-15',
        'The nightly export of every admitted patient into one folder (no request).'),
    ('services/downtime_service.py', 'serve_ward_pack'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # services/fhir_export_service.py
    ('services/fhir_export_service.py', 'medications_to_fhir'): blind('multisite-15'),
    ('services/fhir_export_service.py', 'patient_to_fhir'): blind('multisite-15'),
}

SURFACES = {
    '/api/fhir': blind_surface(
        'multisite-15',
        'FHIR exports of a patient.'),
    '/api/downtime': blind_surface(
        'multisite-15',
        'Downtime ward packs, built for every admitted patient.'),
}
