"""SQL-site registry: OPD and ADT lanes.

Owned by W12-F (multisite-8h1). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

SITES = {
    # routes/adt/reservations.py
    ('routes/adt/reservations.py', '_scoped_patient'): blind('multisite-8h1'),
    # routes/appointments_routes.py
    ('routes/appointments_routes.py', '_appointment_or_404'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', '_book_as_staff_opd'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', '_book_slot_on'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', '_has_admission'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', '_reschedule_replay'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', '_resolve_own_patient_id'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', '_staff_view'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'appointments_day'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'cancel_appointment'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'check_in_appointment'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'check_in_appointment._on_registered'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'check_in_appointment._on_replay'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'complete_appointment'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'list_appointments'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'mark_no_show'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'my_appointments'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'persist_appointment'): blind('multisite-8h1'),
    ('routes/appointments_routes.py', 'reschedule_appointment'): blind('multisite-8h1'),
    # routes/opd_routes.py
    ('routes/opd_routes.py', 'cancel_investigation._belongs'): blind('multisite-8h1'),
    ('routes/opd_routes.py', 'complete_with_disposition._precheck'): blind('multisite-8h1'),
    # services/admission_service.py
    ('services/admission_service.py', '_admit'): blind('multisite-8h1'),
    ('services/admission_service.py', '_adt_replay_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_close_previous_stay_medicines_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_conversion_patient_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_conversion_refusal_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_conversion_replay_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_conversion_visit_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_deceased_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_previous_admission_id_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_previous_stay_end_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_readmission_patient_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_readmission_refusal_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_readmission_replay_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_readmission_replay_parts_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_readmit_on'): blind('multisite-8h1'),
    ('services/admission_service.py', '_stamp_admission_facility_on'): facility(
        ['admissions', 'patients'], 2,
        ('Stamps the facility the admission was checked against onto the admission and its new'
         ' patient record (multisite-3).')),
    ('services/admission_service.py', 'admission_choices'): blind('multisite-8h1'),
    ('services/admission_service.py', 'admit_existing_patient._convert'): blind('multisite-8h1'),
    ('services/admission_service.py', 'bed_in_use_warning'): blind('multisite-8h1'),
    # services/adt/clerk.py
    ('services/adt/clerk.py', 'ordered_transfer'): blind('multisite-8h1'),
    ('services/adt/clerk.py', 'record_admission_assessment'): blind('multisite-8h1'),
    ('services/adt/clerk.py', 'record_left_ward'): blind('multisite-8h1'),
    # services/adt/core.py
    ('services/adt/core.py', '_admission_without_place_on'): blind('multisite-8h1'),
    ('services/adt/core.py', '_holder_on'): blind('multisite-8h1'),
    ('services/adt/core.py', '_recorded_in_on'): blind('multisite-8h1'),
    ('services/adt/core.py', '_recorded_place_on'): blind('multisite-8h1'),
    ('services/adt/core.py', '_typed_holder_on'): blind('multisite-8h1'),
    ('services/adt/core.py', '_vacated_with_isolation_on'): blind('multisite-8h1'),
    ('services/adt/core.py', 'assign_bed'): blind('multisite-8h1'),
    ('services/adt/core.py', 'confirm_terminal_authoriser'): blind('multisite-8h1'),
    ('services/adt/core.py', 'current_admission_on'): blind('multisite-8h1'),
    ('services/adt/core.py', 'current_location._work'): blind('multisite-8h1'),
    ('services/adt/core.py', 'locate_admission_on'): blind('multisite-8h1'),
    ('services/adt/core.py', 'prepare_place_on'): blind('multisite-8h1'),
    # services/adt/end_stay.py
    ('services/adt/end_stay.py', '_bed_for_reopen_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', '_deactivate_portal_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', '_summary_for_stay_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', '_typed_holder_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', 'close_admission_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', 'end_stay_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', 'patient_for_admission'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', 'record_end_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', 'reopen_stay_on'): blind('multisite-8h1'),
    # services/adt/live.py
    ('services/adt/live.py', 'wards_and_departments'): blind('multisite-8h1'),
    # services/adt/presence.py
    ('services/adt/presence.py', 'presence_by_patient._work'): blind('multisite-8h1'),
    ('services/adt/presence.py', 'record_presence_on'): blind('multisite-8h1'),
    # services/adt/reservations.py
    ('services/adt/reservations.py', 'list_reservations'): blind('multisite-8h1'),
    ('services/adt/reservations.py', 'reserve_bed'): blind('multisite-8h1'),
    # services/adt/transfer.py
    ('services/adt/transfer.py', '_active_admission_on'): blind('multisite-8h1'),
    ('services/adt/transfer.py', '_result_on'): blind('multisite-8h1'),
    ('services/adt/transfer.py', 'admission_location'): blind('multisite-8h1'),
    ('services/adt/transfer.py', 'last_move_on'): blind('multisite-8h1'),
    ('services/adt/transfer.py', 'swap_beds_on'): blind('multisite-8h1'),
    # services/adt_census_service.py
    ('services/adt_census_service.py', '_load_stays'): blind('multisite-8h1'),
    # services/bed_master_service.py
    ('services/bed_master_service.py', '_bed_references'): blind('multisite-8h1'),
    ('services/bed_master_service.py', '_board_on'): blind('multisite-8h1'),
    ('services/bed_master_service.py', '_occupants_on'): blind('multisite-8h1'),
    ('services/bed_master_service.py', '_reconcile_on'): blind('multisite-8h1'),
    ('services/bed_master_service.py', '_scope_filter'): blind('multisite-8h1'),
    ('services/bed_master_service.py', '_typed_occupants'): blind('multisite-8h1'),
    ('services/bed_master_service.py', '_visible_wards'): blind('multisite-8h1'),
    ('services/bed_master_service.py', '_ward_references'): blind('multisite-8h1'),
    ('services/bed_master_service.py', 'awaiting_bed'): blind('multisite-8h1'),
    ('services/bed_master_service.py', 'retire_bed'): blind('multisite-8h1'),
    ('services/bed_master_service.py', 'save_ward'): blind('multisite-8h1'),
    # services/opd_queue_service.py
    ('services/opd_queue_service.py', '_load'): blind('multisite-8h1'),
    ('services/opd_queue_service.py', '_reload'): blind('multisite-8h1'),
    ('services/opd_queue_service.py', 'assign_visit'): blind('multisite-8h1'),
    ('services/opd_queue_service.py', 'board_snapshot'): blind('multisite-8h1'),
    ('services/opd_queue_service.py', 'list_my_queue'): blind('multisite-8h1'),
    ('services/opd_queue_service.py', 'recall_visit'): blind('multisite-8h1'),
    ('services/opd_queue_service.py', 'skip_visit'): blind('multisite-8h1'),
    ('services/opd_queue_service.py', 'taken_unassigned_top'): blind('multisite-8h1'),
    ('services/opd_queue_service.py', 'update_queue_fields'): blind('multisite-8h1'),
    # services/opd_service.py
    ('services/opd_service.py', '_active_admission'): blind('multisite-8h1'),
    ('services/opd_service.py', '_identity'): patient_keyed(
        ['admissions'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/opd_service.py', '_opd_results'): blind('multisite-8h1'),
    ('services/opd_service.py', '_signed_summaries'): patient_keyed(
        ['opd_visits'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/opd_service.py', '_visit_row'): blind('multisite-8h1'),
    ('services/opd_service.py', '_visits'): patient_keyed(
        ['opd_visits'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/opd_service.py', 'get_opd_record'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # services/registration_service.py
    ('services/registration_service.py', '_uhid_in_use_on'): blind('multisite-8h1'),
    ('services/registration_service.py', 'change_history'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/registration_service.py', 'check_identity'): blind('multisite-8h1'),
    ('services/registration_service.py', 'correct_demographics'): patient_keyed(
        ['admissions', 'patients'], 4,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/registration_service.py', 'evaluate_identity_on'): blind('multisite-8h1'),
    ('services/registration_service.py', 'get_registration_on'): blind('multisite-8h1'),
    ('services/registration_service.py', 'index_cards_on'): blind('multisite-8h1'),
    ('services/registration_service.py', 'register_patient'): blind('multisite-8h1'),
    ('services/registration_service.py', 'search_index'): blind(
        'multisite-8h1',
        'Group-wide patient index search at the desk.'),
    ('services/registration_service.py', 'sweep_duplicates'): blind('multisite-8h1'),
    ('services/registration_service.py', 'uhid_card'): blind('multisite-8h1'),
}

SURFACES = {
    '/api/opd': blind_surface(
        'multisite-8h1',
        'Outpatient queues and records.'),
    '/api/registration': blind_surface(
        'multisite-8h1',
        'Registration, the patient index and duplicates.'),
    '/api/schedule': blind_surface(
        'multisite-8h1',
        'Clinic sessions and display boards.'),
    '/api/appointments': blind_surface(
        'multisite-8h1',
        'Appointments by day.'),
    '/api/adt': blind_surface(
        'multisite-8h1',
        'The bed board, moves, census and reservations.'),
    '/api/admissions': blind_surface(
        'multisite-8h1',
        'The admit lanes are facility-aware (multisite-3); the choices and lists are not.'),
}
