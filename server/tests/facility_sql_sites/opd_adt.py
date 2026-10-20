"""SQL-site registry: OPD and ADT lanes.

Owned by W12-F (multisite-8h1). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

# The reasons the multisite-8h1 sweep (W12-F, 20 October 2026) gives. A row it
# left blind is reached by a lane another sweep owns (admissions, Scribe, the
# front desk, Relay, Vigil, NABH, housekeeping) whose own scope is not
# facility-aware yet, so its safety is not this sweep's to claim.
_VISIT_CHECKED = (
    "Keyed on one outpatient visit, or that visit's own patient, which every route reaching "
    "it checks first against the caller's clinic and facility (opd_routes._visit_in_clinic; "
    "a refusal is 404 and audited).")
_ADMISSION_CHECKED = (
    "Keyed on the route's own admission, which every route reaching it checks first against "
    "the caller's department and facility (routes/adt/end_stay._scoped_admission or "
    "_scoped_ended_admission; a refusal is 404 and audited).")
_QUEUE_VISIT = (
    "Keyed on the one visit _load has just checked against the acting account's facility "
    "scope, in the same transaction.")
_ROUTE_PATIENT = (
    "Keyed on the route's own patient, which every route that reaches it checks first "
    "with require_patient_access.")
_COUNTS_ONLY = (
    "Counts admissions and patients by ward and bed text so a code in use stays immutable; it "
    "returns numbers only, to a bed manager working on their own facility's ward or bed.")

SITES = {
    # routes/adt/reservations.py
    ('routes/adt/reservations.py', '_scoped_patient'): facility(
        ['patients'], 1,
        ("Checks the named patient against the caller's department and facility scope "
         "(permissions.facility_denial) before a bed is held; a refusal is 404 and audited.")),
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
    ('routes/opd_routes.py', 'cancel_investigation._belongs'): document_keyed(
        ['admissions'], 1, _VISIT_CHECKED),
    ('routes/opd_routes.py', 'complete_with_disposition._precheck'): document_keyed(
        ['admissions'], 1, _VISIT_CHECKED),
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
    ('services/adt/core.py', 'assign_bed'): document_keyed(
        ['admissions'], 1, _ADMISSION_CHECKED),
    ('services/adt/core.py', 'confirm_terminal_authoriser'): blind('multisite-8h1'),
    ('services/adt/core.py', 'cross_facility_refusal_on'): facility(
        ['admissions'], 1,
        ("The cross-facility move refusal itself (ruling MS-R3 (c)): reads one admission's own "
         'facility to refuse a ward or treating department at another.')),
    ('services/adt/core.py', 'current_admission_on'): blind('multisite-8h1'),
    ('services/adt/core.py', 'current_location._work'): blind('multisite-8h1'),
    ('services/adt/core.py', 'locate_admission_on'): facility(
        ['admissions', 'patients'], 5,
        ('The single location writer: keyed on one admission, and it refuses a ward or a '
         "treating department at another facility than the admission's own "
         '(cross_facility_refusal_on) before anything else reads that ward.')),
    ('services/adt/core.py', 'prepare_place_on'): facility(
        ['admissions'], 1,
        ("Keyed on one admission (or none yet, at the admission door); it refuses a ward or "
         'department at another facility before the cleaning and occupancy rules read it.')),
    # services/adt/end_stay.py
    ('services/adt/end_stay.py', '_bed_for_reopen_on'): document_keyed(
        ['admissions'], 2, _ADMISSION_CHECKED),
    ('services/adt/end_stay.py', '_deactivate_portal_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', '_summary_for_stay_on'): document_keyed(
        ['admissions'], 1, _ADMISSION_CHECKED),
    ('services/adt/end_stay.py', '_typed_holder_on'): document_keyed(
        ['admissions'], 1, _ADMISSION_CHECKED),
    ('services/adt/end_stay.py', 'close_admission_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', 'end_stay_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', 'patient_for_admission'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', 'record_end_on'): blind('multisite-8h1'),
    ('services/adt/end_stay.py', 'reopen_stay_on'): document_keyed(
        ['admissions', 'patients'], 7, _ADMISSION_CHECKED),
    # services/adt/live.py
    ('services/adt/live.py', 'wards_and_departments'): blind('multisite-8h1'),
    # services/adt/presence.py
    ('services/adt/presence.py', 'presence_by_patient._work'): blind('multisite-8h1'),
    ('services/adt/presence.py', 'record_presence_on'): document_keyed(
        ['admissions'], 3, _ADMISSION_CHECKED),
    # services/adt/reservations.py
    ('services/adt/reservations.py', 'list_reservations'): facility(
        ['patients'], 1,
        ("Names only the patients _scope_filter keeps inside the caller's department and "
         "facility, on the caller's facility's wards (_visible_wards).")),
    ('services/adt/reservations.py', 'reserve_bed'): facility(
        ['admissions', 'patients'], 3,
        ("Refuses a bed on another facility's ward and, inside its transaction, a patient "
         "outside the caller's department or facility scope (ruling MS-R3 (c)).")),
    # services/adt/transfer.py
    ('services/adt/transfer.py', '_active_admission_on'): blind('multisite-8h1'),
    ('services/adt/transfer.py', '_result_on'): blind('multisite-8h1'),
    ('services/adt/transfer.py', 'admission_location'): document_keyed(
        ['admissions'], 1, _ADMISSION_CHECKED),
    ('services/adt/transfer.py', 'last_move_on'): document_keyed(
        ['admissions'], 1, _ADMISSION_CHECKED),
    ('services/adt/transfer.py', 'swap_beds_on'): facility(
        ['admissions', 'patients'], 3,
        ('Keyed on two admissions the route checks first (_scoped_admission), and it refuses '
         'either patient a ward at another facility than their own (cross_facility_refusal_on).')),
    # services/adt_census_service.py
    ('services/adt_census_service.py', '_load_stays'): facility(
        ['admissions'], 1,
        ("Counts only the stays of admissions at the caller's facilities when given them: GET "
         '/api/adt/census passes the caller\'s scope; the NABH indicators (multisite-12) call '
         'it without one and stay behind the fence through their own rows. Counts, no patient.')),
    # services/bed_master_service.py
    ('services/bed_master_service.py', '_bed_references'): appliance(_COUNTS_ONLY),
    ('services/bed_master_service.py', '_board_on'): facility(
        ['patients'], 1,
        ("Names an occupant only among the ids _scope_filter keeps inside the caller's "
         'department and facility; with identity off it reads no patient at all.')),
    ('services/bed_master_service.py', '_occupants_on'): appliance(
        ('Which beds an active admission holds, for a tile to say Occupied; no patient field '
         'leaves _board_on outside the caller\'s department and facility (_scope_filter).')),
    ('services/bed_master_service.py', '_reconcile_on'): facility(
        ['admissions'], 1,
        ("Sorts only the active admissions inside the caller's department and facility "
         "(scope_patient_ids_sql with facilities); another facility's ward is 404.")),
    ('services/bed_master_service.py', '_scope_filter'): facility(
        ['patients'], 2,
        ("Keeps only the ids inside the caller's department and, with a facility scope, its "
         'facilities (scope_patient_ids_sql).')),
    ('services/bed_master_service.py', '_typed_occupants'): appliance(
        ('Admissions typed into one bed, counted to refuse retiring it; the refusal says how '
         'many, never who.')),
    ('services/bed_master_service.py', '_visible_wards'): facility(
        ['admissions'], 1,
        ("The caller's facility's wards only, and boarders counted among the caller's "
         'patients there.')),
    ('services/bed_master_service.py', '_ward_references'): appliance(_COUNTS_ONLY),
    ('services/bed_master_service.py', 'awaiting_bed'): facility(
        ['admissions', 'patients'], 2,
        ("Names the admissions _reconcile_on sorted inside the caller's department and "
         'facility.')),
    ('services/bed_master_service.py', 'retire_bed'): facility(
        ['admissions'], 1,
        ("Refuses a bed of another facility's ward (404) and counts the admissions lying in "
         'the bed it retires; names nobody.')),
    ('services/bed_master_service.py', 'save_ward'): facility(
        ['admissions'], 1,
        ("Stamps a ward with its department's facility and refuses another facility's "
         'department or ward; its admissions statement counts patients on the ward.')),
    # services/facility_lanes.py
    ('services/facility_lanes.py', 'admission_facility_on'): facility(
        ['admissions'], 1,
        "Reads one admission's facility for the OPD and ADT lanes' facility checks."),
    ('services/facility_lanes.py', 'visit_facility_on'): facility(
        ['opd_visits'], 1,
        "Reads one outpatient visit's facility for the OPD lanes' facility checks."),
    # services/opd_queue_service.py
    ('services/opd_queue_service.py', '_load'): facility(
        ['opd_visits'], 1,
        ("Loads one visit and refuses it, like an unknown one, when it is at a facility "
         "outside the acting account's scope.")),
    ('services/opd_queue_service.py', '_reload'): document_keyed(['opd_visits'], 1, _QUEUE_VISIT),
    ('services/opd_queue_service.py', 'assign_visit'): document_keyed(
        ['opd_visits'], 2, _QUEUE_VISIT),
    ('services/opd_queue_service.py', 'board_snapshot'): facility(
        ['opd_visits'], 1,
        ('One clinic\'s name-free token board (token, doctor, room); a clinic belongs to one '
         'facility (ruling MS-R4 (a)).')),
    ('services/opd_queue_service.py', 'list_my_queue'): facility(
        ['opd_visits', 'patients'], 1,
        ("The doctor's own queue in their clinic, limited to the caller's facilities "
         '(v.facility_id) when the route passes its scope.')),
    ('services/opd_queue_service.py', 'recall_visit'): document_keyed(
        ['opd_visits'], 1, _QUEUE_VISIT),
    ('services/opd_queue_service.py', 'skip_visit'): document_keyed(['opd_visits'], 1, _QUEUE_VISIT),
    ('services/opd_queue_service.py', 'taken_unassigned_top'): facility(
        ['audit_logs', 'opd_visits'], 1,
        ("The highest token taken out of one clinic's unassigned line today; a clinic belongs "
         'to one facility (ruling MS-R4 (a)). A number, no patient.')),
    ('services/opd_queue_service.py', 'update_queue_fields'): document_keyed(
        ['opd_visits'], 1, _QUEUE_VISIT),
    # services/opd_service.py
    ('services/opd_service.py', '_active_admission'): document_keyed(
        ['admissions'], 1, _VISIT_CHECKED),
    ('services/opd_service.py', '_identity'): patient_keyed(
        ['admissions'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/opd_service.py', '_opd_results'): patient_keyed(
        ['opd_visits', 'orders'], 1, _ROUTE_PATIENT),
    ('services/opd_service.py', '_signed_summaries'): patient_keyed(
        ['opd_visits'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/opd_service.py', '_visit_row'): document_keyed(['opd_visits'], 1, _VISIT_CHECKED),
    ('services/opd_service.py', '_visits'): patient_keyed(
        ['opd_visits'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/opd_service.py', 'get_opd_record'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # services/registration_service.py
    ('services/registration_service.py', '_in_facilities_on'): facility(
        ['patients'], 1,
        ("Keeps only the ids whose latest encounter is at the caller's facilities "
         '(permissions.FACILITY_PATIENT_SQL).')),
    ('services/registration_service.py', '_uhid_in_use_on'): appliance(
        ('The group-wide uniqueness of the hospital number (one group MRN, ruling MS-R11 (a)): '
         'its callers refuse a number in use without naming the record, and the index search '
         'filters the ids it returns by facility.')),
    ('services/registration_service.py', 'change_history'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/registration_service.py', 'check_identity'): facility(
        ['admissions', 'patients'], 2,
        ("Answers only match or no match, and an inpatient outside the caller's department "
         'or facility scope (from the authenticated caller) is 404 and audited.')),
    ('services/registration_service.py', 'correct_demographics'): patient_keyed(
        ['admissions', 'patients'], 4,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/registration_service.py', 'evaluate_identity_on'): document_keyed(
        ['patients'], 1,
        ('Keyed on the one patient check_identity has checked against the caller\'s scope; '
         'it answers only match or no match.')),
    ('services/registration_service.py', 'get_registration_on'): document_keyed(
        ['admissions', 'patients'], 1,
        ("Keyed on the route's own patient (require_patient_access), or on the record the "
         'caller has just registered at their own facility.')),
    # Left blind: the admission door's duplicate check (admission_service, the
    # admissions sweep) decorates candidates no facility limit has filtered.
    ('services/registration_service.py', 'index_cards_on'): blind('multisite-8h1'),
    ('services/registration_service.py', 'register_patient'): facility(
        ['patients'], 3,
        ("Registers the patient at the registrar's facility and never shows a duplicate "
         'candidate whose latest encounter is at another facility (it is counted, unshown).')),
    ('services/registration_service.py', 'search_index'): facility(
        ['patients'], 2,
        ("The desk's index search, inside the caller's facilities (scope_patient_ids_sql with "
         'facilities, ruling MS-R5 (a)); the group identity match is multisite-17.')),
    ('services/registration_service.py', 'sweep_duplicates'): facility(
        ['patients'], 1,
        ("Pairs only records whose latest encounter is at the caller's facilities; it "
         'answers counts and names nobody.')),
    ('services/registration_service.py', 'uhid_card'): document_keyed(
        ['patients'], 1,
        ("Keyed on one patient: the route's own (require_patient_access) or a visit's, after "
         'opd_routes._visit_in_clinic has checked the visit.')),
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
