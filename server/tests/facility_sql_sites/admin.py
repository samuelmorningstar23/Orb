"""SQL-site registry: administrative lanes, the Command Center and the platform.

Owned by W13-A (multisite-9b, multisite-12); the scope machinery, schema,
migrations and scripts are classified here too. The rows and route groups
this area's sweep owns; see __init__.py for the kinds and the rules. A sweep
that makes a lane facility-aware moves its rows to a safe kind (with tables,
count and reason) and its route group to 'cleared', then regenerates the
fence's lists (python -m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

SITES = {
    # database.py
    ('database.py', '_SCHEMA_TEMPLATE'): appliance(
        'Schema created at boot, before any request.'),
    ('database.py', 'init_db'): appliance(
        ('Schema creation and boot seeding, run at start-up and by the restore lane; it '
         "answers no person's request.")),
    ('database.py', 'verify_audit_chain'): appliance(
        "Walks the audit chain's hashes for the integrity check; no row leaves it."),
    # dev_tools/
    ('dev_tools/', '*'): appliance(
        "Development tools run by hand on a developer's copy; no request reaches them."),
    # migrations.py
    ('migrations.py', '*'): appliance(
        'Schema migrations, run once at boot before any request.'),
    # migrations_his/
    ('migrations_his/', '*'): appliance(
        'Schema migrations (one per file), run once at boot before any request.'),
    # permissions.py
    ('permissions.py', 'FACILITY_PATIENT_SQL'): facility(
        ['admissions', 'appointments', 'opd_visits'], 1,
        ("The facility a patient belongs to: an active admission's, else the newest "
         "encounter's (admission, outpatient visit or booking, multisite-4), else the record's "
         'registered facility (multisite-5a). Every facility check reads it.')),
    ('permissions.py', 'PATIENT_DEPARTMENT_SQL'): facility(
        ['admissions'], 1,
        ('The department a patient belongs to, the fragment every department scope compares. '
         'With department names unique across the group (MS-R4 (a)) it keeps ward staff to '
         'their facility.')),
    ('permissions.py', '_log_patient_read_blocking'): appliance(
        ('An existence check on the patient a read audit row names; it returns nothing to the '
         'caller and the audit row is a write.')),
    ('permissions.py', '_opd_scope_visit'): facility(
        ['opd_visits'], 1,
        "The OPD arm of the scope check: one patient's recent visit in the caller's clinic."),
    ('permissions.py', '_patient_has_active_admission'): facility(
        ['admissions'], 1,
        'The OPD arm of the scope check: whether one patient has an active admission.'),
    ('permissions.py', 'facility_denial'): facility(
        ['patients'], 1,
        "The facility check itself: one patient's facility against the caller's facilities."),
    ('permissions.py', 'patient_in_scope'): facility(
        ['patients'], 3,
        ("The scope check itself: one patient against the caller's department and, with "
         'facilities=, facility. Answers yes or no, never a row.')),
    ('permissions.py', 'scope_patient_ids_sql'): facility(
        ['patients'], 5,
        ('The scoped patient-id subquery the lists join; with facilities= it keeps to the '
         "caller's facilities. A caller that leaves facilities= out is classified by its own "
         'row.')),
    # routes/admin_users_routes.py
    ('routes/admin_users_routes.py', '_known_departments'): blind('multisite-9b'),
    # scripts/
    ('scripts/', '*'): appliance(
        'Console scripts an operator runs at the appliance; no request reaches them.'),
    # services/abdm_service.py
    ('services/abdm_service.py', '_clash_patient_line'): blind('multisite-9b'),
    ('services/abdm_service.py', '_require_patient'): blind('multisite-9b'),
    ('services/abdm_service.py', 'get_consent'): blind('multisite-9b'),
    ('services/abdm_service.py', 'list_abha'): blind('multisite-9b'),
    ('services/abdm_service.py', 'list_care_contexts'): blind('multisite-9b'),
    ('services/abdm_service.py', 'list_consents'): blind('multisite-9b'),
    ('services/abdm_service.py', 'unlinked_encounter_queue'): blind('multisite-9b'),
    # services/command_center_service.py
    ('services/command_center_service.py', '_admitted_with_acuity'): blind(
        'multisite-12',
        'Every admitted patient.'),
    ('services/command_center_service.py', '_or_today'): blind('multisite-12'),
    # services/dietary_service.py
    ('services/dietary_service.py', '_ADMITTED_JOIN'): blind('multisite-9b'),
    ('services/dietary_service.py', '_current_admission'): blind('multisite-9b'),
    ('services/dietary_service.py', '_load_order'): blind('multisite-9b'),
    ('services/dietary_service.py', '_patients_without_orders'): blind('multisite-9b'),
    ('services/dietary_service.py', 'list_wards'): blind('multisite-9b'),
    ('services/dietary_service.py', 'order_diet'): blind('multisite-9b'),
    ('services/dietary_service.py', 'patient_diet_history'): blind('multisite-9b'),
    # services/facility_identity.py
    ('services/facility_identity.py', 'create_facility'): appliance(
        "Reads the newest audit row id as the new facility's audit epoch; no row is shown."),
    ('services/facility_identity.py', 'document_facility_id'): facility(
        ['admissions', 'patients'], 2,
        ("Which facility a document's admission or patient belongs to, so the document is "
         'stamped and numbered there (multisite-2, multisite-7a).')),
    ('services/facility_identity.py', 'open_work_problems'): appliance(
        "Counts a facility's active admissions before it may be closed; no patient is named."),
    # services/facility_service.py
    ('services/facility_service.py', '_patient_record_facility'): facility(
        ['patients'], 1,
        "The facility of the signed-in patient's own record, for the account's label."),
    ('services/facility_service.py', 'facility_report'): appliance(
        ('Counts for the operations page (patients with admissions at more than one facility);'
         ' no patient is named.')),
    # services/history_import_service.py
    ('services/history_import_service.py', '_Resolver.prefetch'): blind('multisite-9b'),
    ('services/history_import_service.py', '_write_patient'): blind('multisite-9b'),
    # services/housekeeping_service.py
    ('services/housekeeping_service.py', '_canonical_ward'): blind('multisite-9b'),
    ('services/housekeeping_service.py', 'sync_adt_movements'): blind('multisite-9b'),
    ('services/housekeeping_service.py', 'sync_adt_movements._lies_in_now'): blind('multisite-9b'),
    ('services/housekeeping_service.py', 'sync_discharge_turnarounds'): blind('multisite-9b'),
    ('services/housekeeping_service.py', 'ward_readiness'): blind('multisite-9b'),
    # services/impact_service.py
    ('services/impact_service.py', 'compute_impact'): blind('multisite-9b'),
    # services/metrics_service.py
    ('services/metrics_service.py', 'render_metrics'): appliance(
        ('Appliance-wide counters for the metrics page (records, orders, audit rows, admitted '
         'patients); no patient, ward or department is named.')),
    # services/nabh_service.py
    ('services/nabh_service.py', '_adt_disposition_on'): blind('multisite-12'),
    ('services/nabh_service.py', '_disposition_base'): blind('multisite-12'),
    ('services/nabh_service.py', '_escalation_ack_compliance'): blind('multisite-12'),
    ('services/nabh_service.py', '_handoff_completion'): blind('multisite-12'),
    ('services/nabh_service.py', '_inpatient_days'): blind('multisite-12'),
    ('services/nabh_service.py', '_medication_override_rate'): blind('multisite-12'),
    ('services/nabh_service.py', '_per_stay_counting_on'): blind('multisite-12'),
    ('services/nabh_service.py', '_per_stay_totals'): blind('multisite-12'),
    ('services/nabh_service.py', '_readmission'): blind('multisite-12'),
    ('services/nabh_service.py', '_repeat_stay_patients'): blind('multisite-12'),
    ('services/nabh_service.py', 'hospital_wards._work'): blind('multisite-12'),
    # services/ops_health_service.py
    ('services/ops_health_service.py', '_check_site_config'): appliance(
        ('Counts active admissions per department name for the operations page; no patient is '
         'named, and department names are unique across the group (MS-R4 (a)).')),
    # services/roster_import.py
    ('services/roster_import.py', '_absent_from_roster'): blind('multisite-9b'),
    ('services/roster_import.py', '_load_existing'): blind('multisite-9b'),
    ('services/roster_import.py', '_load_open_admissions'): blind('multisite-9b'),
    ('services/roster_import.py', '_reject_unlisted_places'): blind('multisite-9b'),
    ('services/roster_import.py', 'apply_import'): blind('multisite-9b'),
}

SURFACES = {
    '/api/admin': blind_surface(
        'multisite-9b',
        'Administration, compliance, imports, backups and shadow mode.'),
    '/api/abdm': blind_surface(
        'multisite-9b',
        'ABDM identities, consents and care contexts.'),
    '/api/dietary': blind_surface(
        'multisite-9b',
        'Diet orders by ward.'),
    '/api/housekeeping': blind_surface(
        'multisite-9b',
        'Housekeeping by ward.'),
    '/api/biomedical': blind_surface(
        'multisite-9b',
        'Biomedical assets.'),
    '/api/procurement': blind_surface(
        'multisite-9b',
        'Procurement documents.'),
    '/api/workforce': blind_surface(
        'multisite-9b',
        'Rosters and payroll exports.'),
    '/api/nabh': blind_surface(
        'multisite-12',
        'NABH indicators and evidence packs.'),
    '/api/command-center': blind_surface(
        'multisite-12',
        'The house snapshot lists every admitted patient.'),
    '/api/capacity-sim': blind_surface(
        'multisite-12',
        'Capacity scenarios from the whole census.'),
    '/api/pulse': no_patient_data_surface(
        "The seasonal calendar for the hospital's state; it reads no patient."),
}

#: Routes outside the fence's roots, and why each carries no patient.
UNFENCED = {
    '/openapi.json':
        'The API schema, generated from the routes; no data.',
    '/docs':
        'The API documentation page; no data.',
    '/docs/oauth2-redirect':
        'The API documentation sign-in redirect; no data.',
    '/redoc':
        'The API documentation page; no data.',
    '/metrics':
        ('Appliance-wide counters (services/metrics_service.render_metrics); no patient is '
         'named.'),
}
