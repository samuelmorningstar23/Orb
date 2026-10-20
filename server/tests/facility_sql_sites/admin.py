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
    # services/abdm_service.py (multisite-9b, W13-A: facilities= from the route)
    ('services/abdm_service.py', '_clash_patient_line'): facility(
        ['patients'], 1,
        ("Names the patient a duplicate ABHA belongs to only when patient_in_scope holds with "
         "the caller's department and facilities; a caller that gives no facility scope "
         'learns no name while more than one facility is on record.')),
    ('services/abdm_service.py', '_require_patient'): appliance(
        ("An existence check on the patient an ABHA or consent write names; the row it reads "
         'is not returned, and every route naming the patient checks its scope first.')),
    ('services/abdm_service.py', 'get_consent'): document_keyed(
        ['patients'], 1,
        ("One consent artefact by id; every route checks the artefact's own patient with "
         'enforce_patient_scope before it is shown or changed.')),
    ('services/abdm_service.py', 'list_abha'): facility(
        ['patients'], 1,
        "ABHA records of the caller's ward and facilities (scope_patient_ids_sql, facilities=)."),
    ('services/abdm_service.py', 'list_care_contexts'): facility(
        ['patients'], 1,
        ("Care-context links of the caller's ward and facilities (scope_patient_ids_sql, "
         'facilities=).')),
    ('services/abdm_service.py', 'list_consents'): facility(
        ['patients'], 1,
        ("Consent artefacts of the caller's ward and facilities (scope_patient_ids_sql, "
         'facilities=).')),
    ('services/abdm_service.py', 'unlinked_encounter_queue'): facility(
        ['admissions', 'patients'], 2,
        ("The linking queue: encounters at the caller's facilities (admissions.facility_id) "
         "of patients inside the caller's ward and facility scope.")),
    # services/command_center_service.py (multisite-12, W13-A)
    ('services/command_center_service.py', '_admitted_with_acuity'): facility(
        ['patients'], 2,
        ("Admitted patients at the snapshot's facilities (facility_filter.patient_clause); "
         'the route passes the caller\'s facility, or none for a group-wide reader.')),
    ('services/command_center_service.py', '_or_today'): facility(
        ['patients', 'surgeries'], 1,
        "Today's operating list at the snapshot's facilities (surgeries.facility_id)."),
    # services/dietary_service.py (multisite-9b, W13-A: facilities= from the route)
    ('services/dietary_service.py', '_ADMITTED_JOIN'): facility(
        ['admissions', 'patients'], 1,
        ("The latest-admission join every diet list reads; each caller adds the facility "
         'clause on that admission (a.facility_id).')),
    ('services/dietary_service.py', '_current_admission'): patient_keyed(
        ['admissions'], 1,
        ("The admission of the one patient a diet order names; the order route checks that "
         'patient with enforce_patient_scope first.')),
    ('services/dietary_service.py', '_load_order'): document_keyed(
        ['admissions', 'patients'], 1,
        ("One diet order by id; the routes check the order's own patient with "
         'enforce_patient_scope before acting on it or showing it.')),
    ('services/dietary_service.py', '_patients_without_orders'): facility(
        ['admissions', 'patients'], 1,
        "Admitted patients with no diet order, at the caller's facilities (a.facility_id)."),
    ('services/dietary_service.py', '_ward_facility_on'): facility(
        ['admissions'], 1,
        ("Which facility a ward is at (Bed setup, else its admissions' one facility): the "
         'scope machinery for meal plans; it returns a facility id, never a patient.')),
    ('services/dietary_service.py', 'list_wards'): facility(
        ['admissions', 'patients'], 1,
        "Ward counts of admitted patients at the caller's facilities (a.facility_id)."),
    ('services/dietary_service.py', 'order_diet'): patient_keyed(
        ['patients'], 1,
        "Writes one named patient's diet order; the route checks the patient first."),
    ('services/dietary_service.py', 'patient_diet_history'): patient_keyed(
        ['admissions', 'patients'], 1,
        "One patient's diet orders; the route checks the patient with enforce_patient_scope."),
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
    # services/forecast_service.py (multisite-12 additions; the module's other rows are in
    # clinical.py)
    ('services/forecast_service.py', '_discharges_at'): facility(
        ['admissions', 'patients'], 2,
        ("Today's discharge events at the caller's facilities: summaries of their patients "
         'and summary-less ends of their stays. A count, no patient named.')),
    ('services/forecast_service.py', 'ward_facility_lookup'): facility(
        ['admissions'], 1,
        ('Which facility a ward is at: Bed setup, else the one facility of the stays open on '
         'it. The capacity-per-facility machinery; it returns facility ids only.')),
    # services/history_import_service.py
    ('services/history_import_service.py', '_Resolver.prefetch'): blind('multisite-9b'),
    ('services/history_import_service.py', '_write_patient'): blind('multisite-9b'),
    # services/housekeeping_service.py (multisite-9b, W13-A: routes/housekeeping_routes.py
    # keeps every answer to the caller's facilities' units and wards)
    ('services/housekeeping_service.py', '_canonical_ward'): appliance(
        ("Matches a typed ward to the spelling patients and units already use; it returns a "
         'ward name, never a patient.')),
    ('services/housekeeping_service.py', 'sync_adt_movements'): appliance(
        ('The appliance-wide sweep that raises cleaning requests from bed movements; it '
         'answers with counts only.')),
    ('services/housekeeping_service.py', 'sync_adt_movements._lies_in_now'): appliance(
        ("Whether a bed's patient still lies in it, inside the movement sweep; a yes or no "
         'that leaves the sweep as a count.')),
    ('services/housekeeping_service.py', 'sync_discharge_turnarounds'): appliance(
        ('The appliance-wide sweep that raises turnaround cleans from discharges; it answers '
         "with counts and unmatched ward names, which the route keeps to the caller's.")),
    ('services/housekeeping_service.py', 'ward_readiness'): appliance(
        ('Admitted-patient counts per ward beside the units registered there; no patient is '
         "named, and the route keeps the rows to the caller's facilities' wards.")),
    # services/impact_service.py
    ('services/impact_service.py', 'compute_impact'): blind('multisite-9b'),
    # services/metrics_service.py
    ('services/metrics_service.py', 'render_metrics'): appliance(
        ('Appliance-wide counters for the metrics page (records, orders, audit rows, admitted '
         'patients); no patient, ward or department is named.')),
    # services/nabh_service.py (multisite-12, W13-A: one facility's figures, see the
    # module's "One facility's figures" block)
    ('services/nabh_service.py', '_PATIENT_STAY_FACILITY_SQL'): facility(
        ['admissions'], 1,
        ("The facility a patient's NABH events are counted at: their latest admission's, "
         'else the registered facility. The scope machinery of the indicators.')),
    ('services/nabh_service.py', '_adt_disposition_on'): appliance(
        ("A writer's lookup of the disposition of the stay it is ending or keying; it reads "
         "the one patient its lane has already scoped, and nothing is listed.")),
    ('services/nabh_service.py', '_facility_rows'): facility(
        ['patients'], 1,
        "Keeps indicator rows to the patients counted at one facility (the scope machinery)."),
    ('services/nabh_service.py', '_inpatient_days'): facility(
        ['patients'], 1,
        ("The inpatient-day count; with a facility it counts that facility's patients only "
         '(_facility_rows). Counts, no patient named.')),
    ('services/nabh_service.py', '_patient_join'): facility(
        ['patients'], 1,
        ("The patients join of every per-event indicator: the chosen ward and the facility "
         "of the patient's stays (the scope machinery).")),
    ('services/nabh_service.py', '_per_stay_counting_on'): appliance(
        'Whether any admission carries what the beds module writes; a yes or no, no row.'),
    ('services/nabh_service.py', '_per_stay_totals'): facility(
        ['admissions', 'patients'], 2,
        ("Per-stay inpatient hours; with a facility only that facility's stays "
         '(admissions.facility_id) and its legacy patients. Counts, no patient named.')),
    ('services/nabh_service.py', '_readmission'): facility(
        ['admissions', 'patients'], 2,
        ("The re-admission rate over one facility's discharges (_patient_join); the "
         'admissions it looks for are read whole so the rate is never flattered. Counts only.')),
    ('services/nabh_service.py', '_repeat_stay_patients'): appliance(
        ('The patients with more than one stay, used inside the inpatient-day count; no id '
         'leaves the module.')),
    ('services/nabh_service.py', 'hospital_wards._work'): facility(
        ['patients'], 2,
        ("Ward names for the NABH selects; with facilities only those facilities' patients' "
         'and incidents\' wards. Ward names, never a patient.')),
    ('services/nabh_service.py', 'list_dispositions'): facility(
        ['patients'], 1,
        "Discharge dispositions of the patients at the caller's facilities."),
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

#: Wave 13 (W13-A) made the lanes of these groups facility-aware. A group that
#: reaches no blind or unregistered owner any more (/api/abdm, /api/dietary,
#: /api/biomedical, /api/procurement and /api/workforce, pinned by
#: tests/test_command_center_facility.py) is ready to move to cleared_surface;
#: that move and the regenerated fence lists are one wave-level step
#: (server/facility_fence.py), so every group below stays blind until it lands.
SURFACES = {
    '/api/admin': blind_surface(
        'multisite-9b',
        ('Administration, compliance, imports, backups and shadow mode. The overview, '
         'alerts and compliance lanes are facility-aware (W13-A); the roster and history '
         'imports, consent retention and erasure, the impact figures and the user list '
         'still reach rows other owners hold.')),
    '/api/abdm': blind_surface(
        'multisite-9b',
        ('ABDM identities, consents and care contexts: facility-aware (W13-A) and reaching '
         'no blind owner; it waits for the wave-level fence step.')),
    '/api/dietary': blind_surface(
        'multisite-9b',
        ('Diet orders, meal plans and kitchen indents: facility-aware (W13-A) and reaching '
         'no blind owner; it waits for the wave-level fence step.')),
    '/api/housekeeping': blind_surface(
        'multisite-9b',
        ('Housekeeping by unit and ward: facility-aware in the routes (W13-A); the board '
         'and the sweeps still reach the shared stay-time and ADT helpers (multisite-8, '
         'multisite-8h1).')),
    '/api/biomedical': blind_surface(
        'multisite-9b',
        ('Biomedical machines, schedules, calls and contracts: facility-aware (W13-A) and '
         'reaching no blind owner; it waits for the wave-level fence step.')),
    '/api/procurement': blind_surface(
        'multisite-9b',
        ('Stores and their documents: facility-aware (W13-A) and reaching no blind owner; '
         'it waits for the wave-level fence step.')),
    '/api/workforce': blind_surface(
        'multisite-9b',
        ("Staff, rosters, attendance and payroll exports: kept to the caller's facility's "
         'staff (W13-A) and reaching no blind owner; it waits for the wave-level fence step.')),
    '/api/nabh': blind_surface(
        'multisite-12',
        ('NABH indicators, the register and evidence packs per facility (W13-A); the '
         'indicators still reach the shared stay-time helpers (multisite-8) and the '
         'laboratory turnaround readers (multisite-8h2).')),
    '/api/command-center': blind_surface(
        'multisite-12',
        ('The snapshot per facility with group tiles (W13-A); it still reaches the shared '
         'per-patient forecast, stay-time and Vigil helpers (multisite-8).')),
    '/api/capacity-sim': blind_surface(
        'multisite-12',
        ('Capacity scenarios from the whole census: routes/capacity_sim_routes.py and its '
         'service are not facility-aware yet.')),
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
