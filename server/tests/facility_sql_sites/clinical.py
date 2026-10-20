"""SQL-site registry: clinical lanes.

Owned by W12-A (multisite-8). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).
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
    ('routes/emar_routes.py', '_enforce_medication_scope'): blind('multisite-8'),
    ('routes/emar_routes.py', 'get_patient_administrations'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # routes/lens_routes.py
    ('routes/lens_routes.py', '_assign_analysis'): blind('multisite-8'),
    ('routes/lens_routes.py', '_load_patient_row'): blind('multisite-8'),
    # routes/search_routes.py
    ('routes/search_routes.py', 'search_patients'): blind(
        'multisite-8',
        'House-wide search for a department-less caller.'),
    # routes/surgery_routes.py
    ('routes/surgery_routes.py', '_persist_alerts'): blind('multisite-8'),
    ('routes/surgery_routes.py', 'acknowledge_emergency_surgery'): blind('multisite-8'),
    ('routes/surgery_routes.py', 'complete_surgery'): blind('multisite-8'),
    ('routes/surgery_routes.py', 'get_emergency_queue'): blind('multisite-8'),
    ('routes/surgery_routes.py', 'get_surgery_cases'): blind('multisite-8'),
    ('routes/surgery_routes.py', 'get_surgery_detail'): blind('multisite-8'),
    ('routes/surgery_routes.py', 'get_surgery_schedule'): blind('multisite-8'),
    ('routes/surgery_routes.py', 'start_surgery'): blind('multisite-8'),
    ('routes/surgery_routes.py', 'surgical_team_response'): blind('multisite-8'),
    # routes/vigil_routes.py
    ('routes/vigil_routes.py', '_active_admission_id'): patient_keyed(
        ['admissions'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('routes/vigil_routes.py', '_active_admission_ids'): blind('multisite-8'),
    ('routes/vigil_routes.py', '_insert_medication'): blind('multisite-8'),
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
    ('routes/vigil_routes.py', 'list_discharged'): blind('multisite-8'),
    # services/bot_service.py
    ('services/bot_service.py', '_create_report'): blind('multisite-8'),
    ('services/bot_service.py', '_generate_lab_result'): blind('multisite-8'),
    ('services/bot_service.py', '_generate_vitals'): blind('multisite-8'),
    ('services/bot_service.py', '_get_patient_severity'): blind('multisite-8'),
    ('services/bot_service.py', '_persist_alerts'): blind('multisite-8'),
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
    ('services/consent_service.py', '_MINOR_PATIENT_SQL'): blind('multisite-8'),
    ('services/consent_service.py', '_RELEASABLE_PATIENT_SQL'): blind('multisite-8'),
    ('services/consent_service.py', '_erase_portal_account'): blind('multisite-8'),
    ('services/consent_service.py', 'data_principal_export'): patient_keyed(
        ['audit_logs', 'patients'], 2,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/consent_service.py', 'record_consent_on'): blind('multisite-8'),
    ('services/consent_service.py', 'record_erasure_request'): blind('multisite-8'),
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
    ('services/escalation_service.py', 'get_escalation'): blind('multisite-8'),
    ('services/escalation_service.py', 'list_escalations'): blind('multisite-8'),
    ('services/escalation_service.py', 'scan_new_observations'): blind('multisite-8'),
    # services/forecast_service.py
    ('services/forecast_service.py', '_heuristic_prediction'): blind('multisite-8'),
    ('services/forecast_service.py', 'extract_patient_features'): blind('multisite-8'),
    ('services/forecast_service.py', 'get_appointments'): blind('multisite-8'),
    ('services/forecast_service.py', 'get_bed_capacity_forecast'): blind('multisite-8'),
    ('services/forecast_service.py', 'get_discharge_board'): blind(
        'multisite-8',
        'Every admitted patient.'),
    ('services/forecast_service.py', 'predict_hospital_flow'): blind('multisite-8'),
    ('services/forecast_service.py', 'refresh_all_predictions'): blind('multisite-8'),
    ('services/forecast_service.py', 'refresh_ward_capacity'): blind('multisite-8'),
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
    ('services/nervous_system_triggers.py', '_live_vitals_payload'): blind('multisite-8'),
    ('services/nervous_system_triggers.py', '_persist_extraction'): blind('multisite-8'),
    # services/nurse_shift_service.py
    ('services/nurse_shift_service.py', '_load_board_rows'): blind('multisite-8'),
    ('services/nurse_shift_service.py', '_on_scale_2'): blind('multisite-8'),
    ('services/nurse_shift_service.py', '_stay_start'): blind('multisite-8'),
    ('services/nurse_shift_service.py', 'transcribe_patients'): blind('multisite-8'),
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
    ('services/relay_service.py', 'get_channels_for_user'): blind('multisite-8'),
    # services/room_fit_service.py
    ('services/room_fit_service.py', '_load_snapshot'): blind('multisite-8'),
    # services/scribe_service.py
    ('services/scribe_service.py', 'build_whisper_hints'): blind('multisite-8'),
    # services/sepsis_bundle_service.py
    ('services/sepsis_bundle_service.py', 'get_active_bundles'): blind('multisite-8'),
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
    ('services/surgery_scope.py', 'surgery_in_scope'): blind('multisite-8'),
    ('services/surgery_scope.py', 'surgery_patient_id'): blind('multisite-8'),
    # services/surgery_service.py
    ('services/surgery_service.py', 'get_surgery_detail'): blind('multisite-8'),
    ('services/surgery_service.py', 'get_weekly_schedule'): blind('multisite-8'),
    # services/timeline_service.py
    ('services/timeline_service.py', '_load_clinical_inputs'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # services/vigil_service.py
    ('services/vigil_service.py', '_assemble_feature_sequence'): blind('multisite-8'),
    ('services/vigil_service.py', '_earlier_stay_ends_on'): blind('multisite-8'),
    ('services/vigil_service.py', 'get_patient_risk'): blind('multisite-8'),
    ('services/vigil_service.py', 'readmission_stay_starts_on'): blind('multisite-8'),
    ('services/vigil_service.py', 'scored_admitted_patients'): blind(
        'multisite-8',
        ("Takes the department as an argument: a department-less caller gets every facility's "
         'board.')),
    # services/ward_record_service.py
    ('services/ward_record_service.py', '_stay_segments'): blind('multisite-8'),
    # services/who_checklist.py
    ('services/who_checklist.py', 'get_checklist_state'): blind('multisite-8'),
    ('services/who_checklist.py', 'set_item'): blind('multisite-8'),
    # services/world_model_service.py
    ('services/world_model_service.py', 'compute_patient_state'): blind('multisite-8'),
    ('services/world_model_service.py', 'compute_ward_state'): blind('multisite-8'),
}

SURFACES = {
    '/api/vigil': blind_surface(
        'multisite-8',
        'The ward board, the discharged list and the chart lanes.'),
    '/api/emar': blind_surface(
        'multisite-8',
        'The eMAR round.'),
    '/api/bundles': blind_surface(
        'multisite-8',
        'Sepsis bundles: the active list is house-wide.'),
    '/api/lens': blind_surface(
        'multisite-8',
        "Lens analyses list every patient's images."),
    '/api/search': blind_surface(
        'multisite-8',
        'Patient search is house-wide for a department-less caller.'),
    '/api/chart': blind_surface(
        'multisite-8',
        'The chart timeline and the unseen counts.'),
    '/api/forecast': blind_surface(
        'multisite-8',
        'Forecasts, the discharge board and appointments.'),
    '/api/surgery': blind_surface(
        'multisite-8',
        'Surgery lists and the emergency queue.'),
    '/api/consent': blind_surface(
        'multisite-8',
        'Consent records.'),
    '/api/nurse': blind_surface(
        'multisite-8',
        'The nurse shift board.'),
    '/api/ipass': blind_surface(
        'multisite-8',
        'I-PASS handoffs.'),
    '/api/patients': blind_surface(
        'multisite-8',
        'Allergies, problems and the medication safety view.'),
    '/api/orders': blind_surface(
        'multisite-8',
        'CPOE orders.'),
    '/api/bridge': blind_surface(
        'multisite-8',
        'The patient portal and its ward lanes.'),
    '/api/discharge-access': blind_surface(
        'multisite-8',
        'The discharge link page.'),
    '/api/ward-record': blind_surface(
        'multisite-8',
        'The Ward Record rounds and their exports.'),
    '/api/coding': blind_surface(
        'multisite-8',
        'Clinical coding for a patient.'),
    '/api/scribe': blind_surface(
        'multisite-8',
        'Scribe notes and the quick admit.'),
    '/api/relay': blind_surface(
        'multisite-8',
        'Relay case rooms and channels.'),
    '/uploads': blind_surface(
        'multisite-8',
        'The media route serves any stored file by name.'),
}
