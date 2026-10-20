"""SQL-site registry: clinical lanes.

Owned by W12-A (multisite-8). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).

Swept by W12-A on 20 October 2026: the boards and lists of this area take the
caller's facilities (permissions.facility_scope, services/facility_filter.py),
and /api/coding, /api/consent, /api/lens, /api/search and /uploads are cleared.
The other groups stay blind while they still reach a row another sweep owns;
each group's reason says which. Rows of shared helpers that a board reaches
without a one-patient scope check (the stay boundary helpers, the timeline
source table) stay blind: the patient_keyed graph rule cannot hold for them.
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

SITES = {
    # routes/bridge_routes.py
    ('routes/bridge_routes.py', '_cancel_own_appointment'): patient_keyed(
        ['appointments'], 1,
        ("The signed-in patient's own record: every route that reaches it is bound to the "
         "login's record by require_patient_self.")),
    ('routes/bridge_routes.py', '_patient_appointments'): patient_keyed(
        ['appointments'], 1,
        ("The signed-in patient's own record: every route that reaches it is bound to the "
         "login's record by require_patient_self.")),
    ('routes/bridge_routes.py', '_resolve_patient_id'): patient_keyed(
        ['patients'], 1,
        ("The signed-in patient's own record: every route that reaches it is bound to the "
         "login's record by require_patient_self.")),
    ('routes/bridge_routes.py', 'patient_care_team'): patient_keyed(
        ['admissions', 'patients'], 1,
        ("The signed-in patient's own record: every route that reaches it is bound to the "
         "login's record by require_patient_self.")),
    ('routes/bridge_routes.py', 'patient_medications'): patient_keyed(
        ['medications'], 1,
        ("The signed-in patient's own record: every route that reaches it is bound to the "
         "login's record by require_patient_self.")),
    ('routes/bridge_routes.py', 'patient_reports'): patient_keyed(
        ['medications'], 1,
        ("The signed-in patient's own record: every route that reaches it is bound to the "
         "login's record by require_patient_self.")),
    ('routes/bridge_routes.py', 'ward_portal_lab_reports'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # routes/emar_routes.py
    ('routes/emar_routes.py', '_enforce_medication_scope'): facility(
        ['medications'], 1,
        ("The eMAR's scope check itself: reads the medication's patient and refuses it "
         'unless permissions.facility_denial (facility first) and patient_in_scope allow '
         'it (multisite-8).')),
    ('routes/emar_routes.py', 'get_patient_administrations'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # routes/lens_routes.py
    ('routes/lens_routes.py', '_assign_analysis'): patient_keyed(
        ['patients'], 1,
        ('Keyed on the form-named patient, which assign_to_patient checks first with '
         'enforce_patient_scope (facility first) before the filing reads it.')),
    ('routes/lens_routes.py', '_load_patient_row'): patient_keyed(
        ['patients'], 1,
        ('Keyed on the form-named patient, which analyze_image checks first with '
         'enforce_patient_scope (facility first) before the context is read.')),
    # routes/patient_timeline_routes.py
    ('routes/patient_timeline_routes.py', '_at_facilities'): facility(
        ['patients'], 1,
        ("Keeps the unseen counts to patients whose latest encounter is at the caller's "
         'facilities (permissions.FACILITY_PATIENT_SQL, multisite-8).')),
    # routes/search_routes.py
    ('routes/search_routes.py', 'search_patients'): facility(
        ['admissions', 'patients'], 1,
        ("The match is kept, in the query, to the caller's department and to active "
         "admissions at the caller's facilities (permissions.facility_scope, multisite-8).")),
    # routes/shadow_routes.py
    ('routes/shadow_routes.py', '_patients_at_facilities'): facility(
        ['patients'], 1,
        ("Keeps the shadow episodes to patients whose latest encounter is at the caller's "
         'facilities (permissions.FACILITY_PATIENT_SQL, multisite-8).')),
    # routes/surgery_routes.py
    ('routes/surgery_routes.py', '_persist_alerts'): document_keyed(
        ['surgeries'], 2,
        ('Keyed on one surgical case; alert_surgical_team checks the case with '
         'surgery_in_scope_blocking (facility-aware since multisite-8) before this runs.')),
    ('routes/surgery_routes.py', 'acknowledge_emergency_surgery'): document_keyed(
        ['surgeries'], 1,
        ('Keyed on one surgical case, checked first with enforce_surgery_scope '
         '(facility-aware since multisite-8).')),
    ('routes/surgery_routes.py', 'complete_surgery'): document_keyed(
        ['surgeries'], 3,
        ('Keyed on one surgical case, checked first with enforce_surgery_scope '
         '(facility-aware since multisite-8).')),
    ('routes/surgery_routes.py', 'get_emergency_queue'): facility(
        ['patients', 'surgeries'], 1,
        ('Filtered by surgery_scope.surgical_list_filter, which keeps the case to the '
         "caller's facilities (surgeries.facility_id) since multisite-8.")),
    ('routes/surgery_routes.py', 'get_surgery_cases'): facility(
        ['patients', 'surgeries'], 1,
        ('Filtered by surgery_scope.surgical_list_filter, which keeps the case to the '
         "caller's facilities (surgeries.facility_id) since multisite-8.")),
    ('routes/surgery_routes.py', 'get_surgery_detail'): facility(
        ['patients', 'surgeries'], 1,
        ('Filtered by surgery_scope.surgical_list_filter, which keeps the case to the '
         "caller's facilities (surgeries.facility_id) since multisite-8.")),
    ('routes/surgery_routes.py', 'get_surgery_schedule'): facility(
        ['patients', 'surgeries'], 1,
        ('Filtered by surgery_scope.surgical_list_filter, which keeps the case to the '
         "caller's facilities (surgeries.facility_id) since multisite-8.")),
    ('routes/surgery_routes.py', 'start_surgery'): document_keyed(
        ['surgeries'], 3,
        ('Keyed on one surgical case, checked first with enforce_surgery_scope '
         '(facility-aware since multisite-8).')),
    ('routes/surgery_routes.py', 'surgical_team_response'): document_keyed(
        ['surgeries'], 1,
        ('Keyed on one surgical case, checked first with enforce_surgery_scope '
         '(facility-aware since multisite-8).')),
    # routes/vigil_routes.py
    ('routes/vigil_routes.py', '_active_admission_id'): patient_keyed(
        ['admissions'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('routes/vigil_routes.py', '_active_admission_ids'): facility(
        ['admissions'], 1,
        ("The board's admission ids, read for the board's own rows and kept to the "
         "caller's facilities (admissions.facility_id, multisite-8).")),
    ('routes/vigil_routes.py', '_insert_medication'): patient_keyed(
        ['medications'], 3,
        ("Writes for the route's own patient, which add_patient_medication checks first "
         'with require_patient_access; the idempotency lookup answers only a row id.')),
    ('routes/vigil_routes.py', '_load_patient_bundle'): patient_keyed(
        ['admissions', 'medications', 'patients'], 3,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('routes/vigil_routes.py', '_load_patient_row'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('routes/vigil_routes.py', '_patient_exists'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('routes/vigil_routes.py', 'list_discharged'): facility(
        ['admissions', 'invoices', 'patients'], 2,
        ("Both statements are kept to the caller's department and to patients whose latest "
         "encounter is at the caller's facilities (permissions.FACILITY_PATIENT_SQL, "
         'multisite-8); the subqueries are keyed on each listed patient.')),
    # services/bot_service.py
    ('services/bot_service.py', '_create_report'): appliance(
        ('The simulator and report bot, a lifespan task no route or socket handler reaches: '
         'it writes rows and answers no request. Its socket emits are the socket rooms\' '
         'sweep (multisite-10).')),
    ('services/bot_service.py', '_generate_lab_result'): appliance(
        ('The simulator and report bot, a lifespan task no route or socket handler reaches: '
         'it writes rows and answers no request. Its socket emits are the socket rooms\' '
         'sweep (multisite-10).')),
    ('services/bot_service.py', '_generate_vitals'): appliance(
        ('The simulator and report bot, a lifespan task no route or socket handler reaches: '
         'it writes rows and answers no request. Its socket emits are the socket rooms\' '
         'sweep (multisite-10).')),
    ('services/bot_service.py', '_get_patient_severity'): appliance(
        ('The simulator and report bot, a lifespan task no route or socket handler reaches: '
         'it writes rows and answers no request. Its socket emits are the socket rooms\' '
         'sweep (multisite-10).')),
    ('services/bot_service.py', '_persist_alerts'): appliance(
        ('The simulator and report bot, a lifespan task no route or socket handler reaches: '
         'it posts into case rooms, which Relay keeps to each facility '
         '(relay_service.get_channels_for_user, multisite-8), and answers no request.')),
    # services/bridge_service.py
    ('services/bridge_service.py', '_case_room_closed'): patient_keyed(
        ['patients'], 1,
        ("The signed-in patient's own record: every route that reaches it is bound to the "
         "login's record by require_patient_self.")),
    ('services/bridge_service.py', 'get_patient_status_simple'): patient_keyed(
        ['patients'], 1,
        ("The signed-in patient's own record: every route that reaches it is bound to the "
         "login's record by require_patient_self.")),
    ('services/bridge_service.py', 'patient_ask_context'): patient_keyed(
        ['patients'], 1,
        ("The signed-in patient's own record: every route that reaches it is bound to the "
         "login's record by require_patient_self.")),
    # services/coding_service.py
    ('services/coding_service.py', '_load_patient_context'): patient_keyed(
        ['medications', 'patients'], 2,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # services/consent_service.py
    ('services/consent_service.py', '_MINOR_PATIENT_SQL'): blind(
        'multisite-9b',
        'The retention review under /api/admin/compliance lists every facility.'),
    ('services/consent_service.py', '_RELEASABLE_PATIENT_SQL'): blind(
        'multisite-9b',
        'The retention review under /api/admin/compliance lists every facility.'),
    ('services/consent_service.py', '_erase_portal_account'): blind(
        'multisite-9b',
        'Erasure under /api/admin/compliance.'),
    ('services/consent_service.py', 'data_principal_export'): patient_keyed(
        ['audit_logs', 'patients'], 2,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/consent_service.py', 'record_consent_on'): appliance(
        ('A writer: checks the named patient exists and stores a consent row, returning only '
         'that row. POST /api/consent/{patient_id} checks the patient with '
         'require_patient_access (facility first) and registration passes the patient it has '
         'just created.')),
    ('services/consent_service.py', 'record_erasure_request'): blind(
        'multisite-9b',
        'Erasure requests under /api/admin/compliance.'),
    # services/discharge_access_service.py
    ('services/discharge_access_service.py', 'get_verified_discharge_summary'): blind('multisite-8'),
    ('services/discharge_access_service.py', 'second_factor_status._lookup'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/discharge_access_service.py', 'verify_discharge_access'): blind('multisite-8'),
    # services/discharge_service.py
    ('services/discharge_service.py', '_draft_inputs'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/discharge_service.py', '_recorded_context'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/discharge_service.py', '_store_orders'): blind('multisite-8'),
    ('services/discharge_service.py', 'build_medication_reconciliation'): blind('multisite-8'),
    ('services/discharge_service.py', 'build_patient_timeline'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/discharge_service.py', 'discharge_patient'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/discharge_service.py', 'offline_discharge_summary'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # services/drug_safety_service.py
    ('services/drug_safety_service.py', '_screen_interactions'): blind('multisite-8'),
    # services/emar_service.py
    ('services/emar_service.py', '_fetch_administration'): blind('multisite-8'),
    ('services/emar_service.py', '_read_recent_administrations'): blind('multisite-8'),
    ('services/emar_service.py', 'list_administrations'): patient_keyed(
        ['medications'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/emar_service.py', 'record_administration'): blind('multisite-8'),
    # services/escalation_service.py
    ('services/escalation_service.py', '_patient_context'): blind('multisite-8'),
    ('services/escalation_service.py', 'get_escalation'): document_keyed(
        ['patients'], 1,
        ('Keyed on one escalation chain; its only route caller, '
         "vigil_routes._escalation_or_404, checks the chain's patient with "
         'enforce_patient_scope (facility first) before anything is shown.')),
    ('services/escalation_service.py', 'list_escalations'): facility(
        ['patients'], 2,
        ("The board is kept to permissions.scope_patient_ids_sql(department, facilities=) "
         "for the caller's facilities (multisite-8); the chart's call is keyed on a patient "
         'require_patient_access checked.')),
    ('services/escalation_service.py', 'scan_new_observations'): facility(
        ['patients'], 1,
        ("A board read and the sweep keep the scan to the caller's facilities "
         '(permissions.FACILITY_PATIENT_SQL, multisite-8); the background safety net is '
         'house-wide and returns counts only.')),
    # services/forecast_service.py
    ('services/forecast_service.py', '_heuristic_prediction'): blind('multisite-8'),
    ('services/forecast_service.py', 'extract_patient_features'): blind('multisite-8'),
    ('services/forecast_service.py', 'get_appointments'): facility(
        ['admissions', 'appointments', 'patients'], 3,
        ("Bookings are kept, in the query, to the caller's department rule and to bookings "
         "at the caller's facilities (appointments.facility_id, multisite-8).")),
    ('services/forecast_service.py', 'get_bed_capacity_forecast'): blind(
        'multisite-12',
        'Ward bed counts for every facility; per-facility capacity is multisite-12.'),
    ('services/forecast_service.py', 'get_discharge_board'): facility(
        ['patients'], 1,
        ("The board is kept, in the query, to permissions.scope_patient_ids_sql(department, "
         "facilities=) for the caller's department and facilities (multisite-8).")),
    ('services/forecast_service.py', 'predict_hospital_flow'): blind(
        'multisite-12',
        'The house-wide flow forecast; per-facility capacity is multisite-12.'),
    ('services/forecast_service.py', 'refresh_all_predictions'): appliance(
        ('The bot\'s periodic prediction refresh, a lifespan task no route or socket handler '
         'reaches: it writes the prediction cache and answers no request.')),
    ('services/forecast_service.py', 'refresh_ward_capacity'): blind(
        'multisite-12',
        'Ward capacity for every facility; per-facility capacity is multisite-12.'),
    # services/ipass_service.py
    ('services/ipass_service.py', '_get_active_meds'): patient_keyed(
        ['medications'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/ipass_service.py', '_get_patient'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # services/nervous_system_triggers.py
    ('services/nervous_system_triggers.py', '_live_vitals_payload'): blind(
        'multisite-10',
        'The nervous system triggers are swept with the Sage lane and the socket rooms.'),
    ('services/nervous_system_triggers.py', '_persist_extraction'): blind(
        'multisite-10',
        'The nervous system triggers are swept with the Sage lane and the socket rooms.'),
    # services/nurse_shift_service.py
    ('services/nurse_shift_service.py', '_load_board_rows'): facility(
        ['admissions', 'medications', 'patients'], 2,
        ("The board's patients are kept, in the query, to the caller's department and to "
         "active admissions at the caller's facilities (multisite-8); every later read is "
         'keyed on those patients.')),
    ('services/nurse_shift_service.py', '_on_scale_2'): blind('multisite-8'),
    ('services/nurse_shift_service.py', '_stay_start'): blind('multisite-8'),
    ('services/nurse_shift_service.py', 'transcribe_patients'): facility(
        ['admissions', 'patients'], 1,
        ("Kept, in the query, to the clerk's department and to active admissions at the "
         "clerk's facilities (admissions.facility_id, multisite-8).")),
    # services/order_sets_service.py
    ('services/order_sets_service.py', 'apply_set'): blind('multisite-8'),
    ('services/order_sets_service.py', 'patient_banner'): blind('multisite-8'),
    ('services/order_sets_service.py', 'require_orderable_patient'): blind('multisite-8'),
    # services/orders_service.py
    ('services/orders_service.py', 'MEDICATION_MANAGED_MESSAGE'): appliance(
        "A sentence on screen ('cancel an unsigned one from Orders'), not SQL."),
    ('services/orders_service.py', '_check_opd_visit'): blind('multisite-8'),
    ('services/orders_service.py', 'add_result'): blind('multisite-8'),
    ('services/orders_service.py', 'create_order'): blind('multisite-8'),
    ('services/orders_service.py', 'get_order'): blind('multisite-8'),
    ('services/orders_service.py', 'list_orders'): patient_keyed(
        ['orders'], 2,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/orders_service.py', 'list_visit_orders'): blind('multisite-8'),
    ('services/orders_service.py', 'mirror_medication_status_in_conn'): blind('multisite-8'),
    ('services/orders_service.py', 'set_lab_result_text_in_conn'): blind('multisite-8'),
    ('services/orders_service.py', 'transition_in_conn'): blind('multisite-8'),
    ('services/orders_service.py', 'transition_order'): blind('multisite-8'),
    # services/patient_portal_access.py
    ('services/patient_portal_access.py', 'portal_access'): appliance(
        ("The portal session gate: reads the signed-in patient's own record status and answers"
         ' open or closed; it never reads another patient.')),
    # services/patient_specific_screen.py
    ('services/patient_specific_screen.py', '_active_medications'): blind('multisite-8'),
    ('services/patient_specific_screen.py', '_problems'): blind('multisite-8'),
    # services/patient_timeline_service.py
    ('services/patient_timeline_service.py', 'SOURCES'): blind('multisite-8'),
    ('services/patient_timeline_service.py', 'unseen_counts_for_scope'): blind('multisite-8'),
    # services/relay_service.py
    ('services/relay_service.py', '_rooms_at_facilities'): facility(
        ['patients'], 1,
        ("Keeps case rooms to patients whose latest encounter is at the caller's facilities "
         '(permissions.FACILITY_PATIENT_SQL, multisite-8).')),
    ('services/relay_service.py', 'get_channels_for_user'): facility(
        ['admissions', 'patients'], 1,
        ("Relay's access boundary: the rooms read are kept to the caller's department rules "
         "and, with more than one facility on record, to the caller's facilities "
         '(_rooms_at_facilities, multisite-8) before any is returned.')),
    # services/room_fit_service.py
    ('services/room_fit_service.py', '_load_snapshot'): blind('multisite-8'),
    # services/scribe_service.py
    ('services/scribe_service.py', 'build_whisper_hints'): blind('multisite-8'),
    # services/sepsis_bundle_service.py
    ('services/sepsis_bundle_service.py', 'get_active_bundles'): facility(
        ['patients'], 1,
        ('Kept to permissions.scope_patient_ids_sql(department, facilities=), the facilities '
         "the bundle board's caller passes (multisite-8).")),
    # services/shadow_scorecard_service.py
    ('services/shadow_scorecard_service.py', '_census_wards'): blind('multisite-8'),
    ('services/shadow_scorecard_service.py', '_occupied_bed_days'): blind('multisite-8'),
    ('services/shadow_scorecard_service.py', '_repeat_stays'): blind('multisite-8'),
    # services/shadow_service.py
    ('services/shadow_service.py', '_lookup_ward'): blind('multisite-8'),
    # services/stay_times.py
    ('services/stay_times.py', 'admission_for_summary'): blind('multisite-8'),
    ('services/stay_times.py', 'latest_admission'): blind('multisite-8'),
    ('services/stay_times.py', 'stay_end_sql'): blind('multisite-8'),
    ('services/stay_times.py', 'stay_end_stored'): blind('multisite-8'),
    ('services/stay_times.py', 'summary_less_ends_sql'): blind('multisite-8'),
    # services/surgery_escalation_service.py
    ('services/surgery_escalation_service.py', '_finalize_surgery'): blind('multisite-8'),
    ('services/surgery_escalation_service.py', '_mark_generating'): blind('multisite-8'),
    ('services/surgery_escalation_service.py', '_resolve_emergency_surgeon'): blind('multisite-8'),
    ('services/surgery_escalation_service.py', 'acknowledge_emergency'): blind('multisite-8'),
    ('services/surgery_escalation_service.py', 'confirm_surgery_prep'): blind('multisite-8'),
    ('services/surgery_escalation_service.py', 'get_emergency_queue'): blind('multisite-8'),
    # services/surgery_scope.py
    ('services/surgery_scope.py', 'surgery_in_scope'): facility(
        ['surgeries'], 1,
        ('The surgical scope check itself: surgical_list_filter keeps the case to the '
         "caller's facilities (surgeries.facility_id) since multisite-8.")),
    ('services/surgery_scope.py', 'surgery_patient_id'): document_keyed(
        ['surgeries'], 1,
        ("Keyed on one surgical case; its route checks the case with enforce_surgery_scope "
         'first and reads this only for the audit row.')),
    # services/surgery_service.py
    ('services/surgery_service.py', 'get_surgery_detail'): document_keyed(
        ['patients', 'surgeries'], 1,
        ('Keyed on one surgical case; the risk and prep routes check the case with '
         'surgery_in_scope_blocking or enforce_surgery_scope (facility-aware) first.')),
    ('services/surgery_service.py', 'get_weekly_schedule'): appliance(
        'No route, socket handler or service calls it (its docstring says so).'),
    # services/timeline_service.py
    ('services/timeline_service.py', '_load_clinical_inputs'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # services/vigil_service.py
    ('services/vigil_service.py', '_assemble_feature_sequence'): blind('multisite-8'),
    ('services/vigil_service.py', '_earlier_stay_ends_on'): blind('multisite-8'),
    ('services/vigil_service.py', 'get_patient_risk'): appliance(
        ('Called only by scripts/seed_clinical_data.py; no route or socket handler reaches '
         'it.')),
    ('services/vigil_service.py', 'readmission_stay_starts_on'): blind('multisite-8'),
    ('services/vigil_service.py', 'scored_admitted_patients'): facility(
        ['admissions', 'patients'], 2,
        ("Both statements are kept to the department and to active admissions at the "
         "facilities its caller passes (facilities=, multisite-8): the Vigil board passes the "
         "caller's; the ward narrator's shared snapshot keeps each reader to theirs in "
         'get_brief.')),
    # services/ward_record_service.py
    ('services/ward_record_service.py', '_stay_segments'): blind('multisite-8'),
    # services/who_checklist.py
    ('services/who_checklist.py', 'get_checklist_state'): document_keyed(
        ['surgeries'], 1,
        ('Keyed on one surgical case; both checklist routes check the case with '
         'enforce_surgery_scope (facility-aware since multisite-8) first.')),
    ('services/who_checklist.py', 'set_item'): document_keyed(
        ['surgeries'], 3,
        ('Keyed on one surgical case; the checklist route checks the case with '
         'enforce_surgery_scope (facility-aware since multisite-8) first.')),
    # services/world_model_service.py
    ('services/world_model_service.py', 'compute_patient_state'): blind('multisite-8'),
    ('services/world_model_service.py', 'compute_ward_state'): blind('multisite-8'),
}

SURFACES = {
    '/api/vigil': blind_surface(
        'multisite-8',
        ('The board, the discharged list and the escalation board keep to the caller\'s '
         'facilities since multisite-8; the chart lanes still reach the presence, medication '
         'order store and audit rows of 8h1, 8h2 and 11 and the shared stay helpers.')),
    '/api/emar': blind_surface(
        'multisite-8',
        ('The dose route checks the facility first since multisite-8; it still reaches the '
         'medication order store (8h2) and stock (8h3) rows.')),
    '/api/bundles': blind_surface(
        'multisite-8',
        ('The active list keeps to the caller\'s facilities since multisite-8; the patient '
         'lanes still reach the bed presence read (multisite-8h1).')),
    '/api/lens': cleared_surface(
        ('Swept in multisite-8: the analyses list keeps filed reads to patients in the '
         "caller's department and facility scope and unfiled reads to their author; the "
         'filing and the analysis check the named patient with enforce_patient_scope.')),
    '/api/search': cleared_surface(
        ('Swept in multisite-8: the match is kept in the query to active admissions at the '
         "caller's facilities.")),
    '/api/chart': blind_surface(
        'multisite-8',
        ('The unseen counts keep to the caller\'s facilities since multisite-8; the shared '
         'timeline source table is still reached by a route that names no patient.')),
    '/api/forecast': blind_surface(
        'multisite-8',
        ('The discharge board and appointments keep to the caller\'s facilities since '
         'multisite-8; the flow and capacity forecasts are house-wide counts (multisite-12).')),
    '/api/surgery': blind_surface(
        'multisite-8',
        ('Every case read keeps to the caller\'s facilities since multisite-8 '
         '(surgery_scope); the risk and prep lanes still reach the Sage rows (multisite-10).')),
    '/api/consent': cleared_surface(
        ('Swept in multisite-8: every consent route names its patient on the path and checks '
         'it with require_patient_access (facility first); the vocabulary names no patient.')),
    '/api/nurse': blind_surface(
        'multisite-8',
        ('The shift board and the clerk list keep to the caller\'s facilities since '
         'multisite-8; the observation lanes still reach the Sage and socket rows.')),
    '/api/ipass': blind_surface(
        'multisite-8',
        'I-PASS handoffs.'),
    '/api/patients': blind_surface(
        'multisite-8',
        'Allergies, problems and the medication safety view.'),
    '/api/orders': blind_surface(
        'multisite-8',
        ('Order reads by id check the facility first since multisite-8; the order lanes '
         'still reach the medication order store and lab rows (multisite-8h2).')),
    '/api/bridge': blind_surface(
        'multisite-8',
        'The patient portal and its ward lanes.'),
    '/api/discharge-access': blind_surface(
        'multisite-8',
        'The discharge link page.'),
    '/api/ward-record': blind_surface(
        'multisite-8',
        'The Ward Record rounds and their exports.'),
    '/api/coding': cleared_surface(
        ('Swept in multisite-8: the one route names its patient on the path and checks it '
         'with require_patient_access (facility first).')),
    '/api/scribe': blind_surface(
        'multisite-8',
        'Scribe notes and the quick admit.'),
    '/api/relay': blind_surface(
        'multisite-8',
        ('Case and department rooms keep to the caller\'s facilities since multisite-8 '
         '(get_channels_for_user); the action lanes still reach the Sage, ADT and medication '
         'order store rows.')),
    '/uploads': cleared_surface(
        ('Swept in multisite-8: the media route serves only registered Lens and Scribe media, '
         "checks a filed record's patient with enforce_patient_scope (facility first) and, "
         'with more than one facility on record, an unfiled record only to its author.')),
}
