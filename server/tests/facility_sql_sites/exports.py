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
    ('routes/fhir_export_routes.py', '_authorize_patient_access'): blind(
        'multisite-15',
        ('The $everything export checks the department only (patient_in_scope without '
         "facilities=); the route file is W12-A's (multisite-8) this wave.")),
    # services/downtime_service.py (multisite-15, W12-D: one folder and one
    # mirror per facility once more than one facility is on record)
    ('services/downtime_service.py', '_admitted_for_packs'): facility(
        ['admissions', 'patients'], 1,
        ("Every row carries the facility of the patient's latest encounter "
         '(FACILITY_PATIENT_SQL) and every request lane keeps only the anchor patient\'s '
         'facility (serve_ward_pack, rebuild_ward_pack, then a scope check per row); the '
         'hourly sweep reads every facility and writes each one into its own folder.')),
    ('services/downtime_service.py', '_ward_and_facility'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient (its ward and facility), which every route that "
         'reaches it checks first with require_patient_access.')),
    ('services/downtime_service.py', 'capture_all_admitted'): facility(
        ['patients'], 1,
        ('Limited to the facilities its caller passes (POST /api/downtime/capture-all passes '
         "permissions.facility_scope); the hourly sweep, with no request, captures every "
         'facility.')),
    ('services/downtime_service.py', 'capture_snapshot'): blind(
        'multisite-15',
        ('Keyed on one patient, but POST /api/downtime/capture-all reaches it through a '
         'facility-limited list with no per-patient scope helper, which patient_keyed cannot '
         'hold; /api/downtime stays fenced until that lane checks each patient.')),
    ('services/downtime_service.py', 'run_nightly_fhir_self_export'): appliance(
        ('No request reaches it: the hourly sweep writes each admitted patient\'s bundle into '
         "the folder of the patient's facility (facility_export_dir) and answers no one.")),
    # services/fhir_export_service.py
    ('services/fhir_export_service.py', 'encounters_to_fhir'): blind(
        'multisite-15',
        ('Keyed on one patient (their stays and visits, each naming its facility); reached by '
         'the $everything route, whose scope check is the department only.')),
    ('services/fhir_export_service.py', 'medications_to_fhir'): blind(
        'multisite-15',
        ('Keyed on one patient; reached by the $everything route, whose scope check is the '
         'department only.')),
    ('services/fhir_export_service.py', 'patient_facility_id'): blind(
        'multisite-15',
        ("Keyed on one patient (the facility of their latest encounter); reached by the "
         '$everything route, whose scope check is the department only.')),
    ('services/fhir_export_service.py', 'patient_to_fhir'): blind(
        'multisite-15',
        ('Keyed on one patient; reached by the $everything route, whose scope check is the '
         'department only.')),
}

SURFACES = {
    '/api/fhir': blind_surface(
        'multisite-15',
        ('FHIR exports of a patient. The bundle names each facility (Organization, '
         'Encounter.serviceProvider); the $everything route still checks the department '
         'only.')),
    '/api/downtime': blind_surface(
        'multisite-15',
        ('Downtime snapshots and ward packs. Packs are per facility; the capture-all drill '
         'is facility-limited but checks no single patient.')),
}
