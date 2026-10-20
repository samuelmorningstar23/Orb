"""SQL-site registry: LIS, medication-order and ingestion lanes.

Owned by W13-D (multisite-8h2). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).

The multisite-8h2 sweep (W13-D, wave 13, 20 October 2026) made every
laboratory list and step and the medication-order routes facility-aware
(services/lab_facility.py; tests/test_facility_lab_meds_scope.py). The three
route groups stay blind: each still reaches rows other sweeps own, named in
its reason below, and opening a group is the wave-level fence regeneration.
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

#: Why a laboratory row read after the step's own scope check is safe.
_AFTER_LAB_CHECK = (
    'Keyed on the sample, result, order or critical its laboratory step already holds: every '
    'route that reaches it is a /api/lab step whose service checked that row against the '
    "caller's department and facilities first (lab_facility.require_in_scope, an audited 404 "
    'outside them), and it answers nothing about any other patient.')

#: Why a laboratory post to the case room discloses nothing across facilities.
_CASE_ROOM_POST = (
    "Writes the laboratory's post into the case room of the notification's own patient and "
    'reads only that patient; who may read the room is relay_service.get_channels_for_user, '
    "which keeps case rooms to the reader's facilities (multisite-8), so no request is "
    "answered with another facility's rows.")

SITES = {
    # routes/lab/reports.py
    ('routes/lab/reports.py', 'list_patient_reports'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # routes/medorders/core.py
    ('routes/medorders/core.py', '_patient_in_scope_or_404'): facility(
        ['patients'], 1,
        ('The scope check itself: an existence probe for the patient a body names, then '
         'permissions.facility_denial and the department check, each refusal audited before '
         'the same 404 (multisite-8h2).')),
    ('routes/medorders/core.py', 'list_patient_orders'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # routes/medorders/schedule.py
    ('routes/medorders/schedule.py', 'patient_schedules'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # services/ingestion_service.py
    ('services/ingestion_service.py', '_facility_match'): facility(
        ['patients'], 1,
        ("Files a message only to a patient at the sending source's facility, refusing an "
         'identifier that names a record elsewhere (multisite-16, MS-R14 (a)).')),
    ('services/ingestion_service.py', '_persist_relay_alert'): blind('multisite-8h2'),
    ('services/ingestion_service.py', '_persist_vitals_row'): blind('multisite-8h2'),
    ('services/ingestion_service.py', '_resolve_patient_id'): blind(
        'multisite-8h2',
        ('Matches across the whole appliance unless a facility is passed (only _facility_match'
         ' passes one).')),
    # services/lab_critical_service.py
    ('services/lab_critical_service.py', '_LIST_SELECT'): document_keyed(
        ['patients'], 1,
        ('The select behind every critical view: rows are loaded only by id, from the lists '
         "kept to the caller's department and facilities (list_for_lab, list_for_ward) or after "
         'the facility-aware check of one notification (_open_notification).')),
    ('services/lab_critical_service.py', '_interfaced_message_text'): appliance(_CASE_ROOM_POST),
    ('services/lab_critical_service.py', '_message_text'): appliance(_CASE_ROOM_POST),
    ('services/lab_critical_service.py', 'deliver_in_txn'): appliance(_CASE_ROOM_POST),
    ('services/lab_critical_service.py', 'raise_for_result_in_txn'): document_keyed(
        ['patients'], 1, _AFTER_LAB_CHECK),
    ('services/lab_critical_service.py', 'raise_interfaced'): appliance(
        "Keyed on the interfaced result's own patient, whom the ingestion lane resolved within "
        "the sending source's facility (multisite-16): it writes that patient's notification "
        'and answers no request with any row.'),
    # services/lab_facility.py
    ('services/lab_facility.py', 'critical_facility_sql'): facility(
        ['patients'], 1,
        ('Scope machinery (multisite-8h2): the facility a critical belongs to, its sample\'s '
         "performing facility, else its patient's (FACILITY_PATIENT_SQL).")),
    ('services/lab_facility.py', 'specimen_facility_sql'): facility(
        ['patients'], 1,
        ("Scope machinery (multisite-8h2): the facility a laboratory row belongs to, its "
         "sample's performing facility, else its patient's (FACILITY_PATIENT_SQL).")),
    # services/lab_master_service.py
    ('services/lab_master_service.py', 'patient_demographics'): document_keyed(
        ['patients'], 1,
        ('The date of birth and sex that choose a reference range for one test; reached only '
         "from the laboratory's worklist (kept to the caller's facilities) and its entry and "
         'amendment steps, which check the test against the caller first. ' + _AFTER_LAB_CHECK)),
    # services/lab_order_service.py
    ('services/lab_order_service.py', '_LAB_SPECIMEN_SELECT'): facility(
        ['patients'], 1,
        ("The specimen select: completed either with the receiving lists' department and "
         'facility clauses (lab_facility.list_clause on the performing facility), or by '
         "_lab_specimen_row, which checks the one sample against the caller's department and "
         'facilities before returning it.')),
    ('services/lab_order_service.py', '_assess'): appliance(
        'The intake decision for one order its calling lane activated (the hook after the '
        "order's commit, or the background sweep); nothing it reads leaves the module or is "
        'answered to a request.'),
    ('services/lab_order_service.py', '_collection_rows'): facility(
        ['patients'], 1,
        ("The collection select: completed with list_collection's department and facility "
         'clauses, or for named samples each checked by _scoped_specimens against the '
         "caller's department and facilities before it is used.")),
    ('services/lab_order_service.py', '_sweep_candidates'): appliance(
        'The background intake sweep: order ids it takes into the laboratory, answered to '
        'nobody.'),
    ('services/lab_order_service.py', 'cancel_order'): document_keyed(
        ['orders'], 3,
        ("Keyed on one laboratory order, checked against the caller's department and "
         "facilities (its sample's performing facility, else its patient's) before anything "
         'is read or changed; an order outside them is an audited 404.')),
    ('services/lab_order_service.py', 'list_open_exceptions'): facility(
        ['orders', 'patients'], 1,
        ("Kept to the caller's facilities in SQL before the LIMIT (lab_facility.list_clause: "
         "the sample's performing facility, else the order's patient's; a ward by its "
         'patients), beside the department clause.')),
    ('services/lab_order_service.py', 'post_rejection_notice'): document_keyed(
        ['patients'], 1, _AFTER_LAB_CHECK),
    ('services/lab_order_service.py', 'reconcile_order_status'): document_keyed(
        ['orders'], 1, _AFTER_LAB_CHECK),
    ('services/lab_order_service.py', 'record_order_note_in_conn'): document_keyed(
        ['orders'], 1, _AFTER_LAB_CHECK),
    ('services/lab_order_service.py', 'settle_after_withdrawal'): document_keyed(
        ['orders'], 1, _AFTER_LAB_CHECK),
    # services/lab_report_service.py
    ('services/lab_report_service.py', '_load'): document_keyed(
        ['orders', 'patients'], 2,
        ('Builds the reports of samples its callers already chose: one report after the '
         'facility-aware check (report_for), the day list kept to the caller\'s facilities '
         '(reports_for_day), or one patient\'s reports after require_patient_access, '
         'require_patient_self or the facility-aware UHID match (patient_by_uhid).')),
    ('services/lab_report_service.py', 'patient_by_uhid'): facility(
        ['patients'], 1,
        ("The one patient a UHID names, answered only through patient_in_scope with the "
         "caller's facilities: a patient elsewhere reads exactly like no such UHID.")),
    # services/lab_result_service.py
    ('services/lab_result_service.py', '_QUEUE_SELECT'): facility(
        ['patients'], 1,
        ('The validation queue select; its one user (validation_queues through _queue_rows) '
         "appends the caller's department and facility clauses (the performing facility).")),
    ('services/lab_result_service.py', '_WORKLIST_SELECT'): facility(
        ['patients'], 1,
        ("The bench select; its one user (worklist) appends the caller's department and "
         'facility clauses (the performing facility).')),
    ('services/lab_result_service.py', '_apply_amendment'): document_keyed(
        ['patients'], 1, _AFTER_LAB_CHECK),
    ('services/lab_result_service.py', 'post_result_notice'): document_keyed(
        ['patients'], 1, _AFTER_LAB_CHECK),
    ('services/lab_result_service.py', 'reported_results'): document_keyed(
        ['patients'], 1,
        ("Keyed on one sample, checked against the caller's department and facilities before "
         'its results are read; a sample outside them is an audited 404.')),
    ('services/lab_result_service.py', 'withdraw_result'): document_keyed(
        ['patients'], 1, _AFTER_LAB_CHECK),
    ('services/lab_result_service.py', 'worklist'): facility(
        ['patients'], 1,
        ("The bench and its cancelled tests, kept to the caller's facilities in SQL before the "
         "LIMIT (the sample's performing facility; a ward by its patients).")),
    # services/lab_tat_service.py
    ('services/lab_tat_service.py', '_ward_clause'): blind(
        'multisite-12',
        ('A ward-name filter only the NABH indicators pass (nabh_service calls reported_tests '
         'and critical_times with a ward and no facilities= yet); the laboratory routes never '
         'pass a ward and keep every figure to the caller\'s facilities.')),
    ('services/lab_tat_service.py', 'open_exception_counts'): facility(
        ['orders'], 1,
        ("Counts kept to the caller's facilities in SQL by the same rule as the exceptions "
         'list (lab_facility.list_clause).')),
    ('services/lab_tat_service.py', 'overdue_tests'): facility(
        ['patients'], 1,
        ("The live overdue list, kept to the caller's facilities in SQL (the sample's "
         'performing facility; a ward by its patients).')),
    ('services/lab_tat_service.py', 'reported_tests'): blind(
        'multisite-12',
        ("Kept to the caller's facilities when facilities= is passed (the laboratory's "
         'turnaround routes pass it); the NABH indicators (nabh_service) call it without one, '
         'so they still count every facility.')),
    # services/medorders/adoption.py
    ('services/medorders/adoption.py', 'adopt_legacy'): appliance(
        'Adoption of unlinked chart rows into the store, answering counts only: run at boot, '
        'or by POST /api/medorders/admin/adopt, which refuses an account with a facility limit '
        'while more than one facility is on record (routes/medorders/core._require_group_wide, '
        'multisite-8h2).'),
    ('services/medorders/adoption.py', 'parity_report'): appliance(
        "The appliance-wide parity report (ids only, no drug names): its only route, GET "
        '/api/medorders/admin/parity, refuses an account with a facility limit while more '
        'than one facility is on record (routes/medorders/core._require_group_wide, '
        'multisite-8h2), and the parity CLI is a console script.'),
    # services/medorders/ai_lanes.py
    ('services/medorders/ai_lanes.py', 'write_sage_order_in_txn'): blind('multisite-8h2'),
    # services/medorders/chart_lane.py
    ('services/medorders/chart_lane.py', '_replay_exclusion'): blind('multisite-8h2'),
    ('services/medorders/chart_lane.py', 'insert_ward_row_in_txn'): blind('multisite-8h2'),
    ('services/medorders/chart_lane.py', 'mirror_legacy_decision_in_txn'): blind('multisite-8h2'),
    ('services/medorders/chart_lane.py', 'write_chart_order_in_txn'): blind('multisite-8h2'),
    # services/medorders/core.py
    ('services/medorders/core.py', 'link_projection_in_txn'): blind('multisite-8h2'),
    ('services/medorders/core.py', 'project_in_txn'): blind('multisite-8h2'),
    ('services/medorders/core.py', 'screen_fingerprint_in_txn'): blind('multisite-8h2'),
    # services/medorders/emar_check.py
    ('services/medorders/emar_check.py', 'check_dose_in_txn'): blind('multisite-8h2'),
    # services/medorders/outpatient.py
    ('services/medorders/outpatient.py', '_active_admission'): blind('multisite-8h2'),
    ('services/medorders/outpatient.py', '_visit_row'): blind('multisite-8h2'),
    # services/medorders/queries.py
    ('services/medorders/queries.py', 'active_admission_id'): blind(
        'multisite-8h2',
        ('A shared helper keyed on the patient its caller holds; the Relay Sage-action confirm '
         'and the order routes reach it as well as /api/medorders, so it waits for a registry '
         'kind for helpers whose callers check the scope (as the multisite-8 shared helpers '
         'do).')),
    ('services/medorders/queries.py', 'list_for_patient'): patient_keyed(
        ['medications'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/medorders/queries.py', 'same_ingredient_live'): blind(
        'multisite-8h2',
        ('A shared helper keyed on the patient its caller holds; the OPD prescription route '
         'reaches it as well as /api/medorders, so it waits for a registry kind for helpers '
         'whose callers check the scope.')),
    ('services/medorders/queries.py', 'to_sign_queue'): facility(
        ['patients'], 1,
        ('Filtered by the scope subquery its only route passes: '
         'scope_patient_ids_sql(department_scope(user), facilities=facility_scope(user)) '
         '(multisite-8h2).')),
    # services/medorders/screening.py
    ('services/medorders/screening.py', '_admitted'): blind('multisite-8h2'),
    ('services/medorders/screening.py', '_excluded'): blind('multisite-8h2'),
    ('services/medorders/screening.py', '_open_visits_from_today'): blind('multisite-8h2'),
    ('services/medorders/screening.py', 'comparison_rows'): blind('multisite-8h2'),
    # services/result_quarantine_service.py
    ('services/result_quarantine_service.py', '_find_patient'): facility(
        ['patients'], 3,
        ('The patient a typed hospital number names, used by the attach step only through '
         "patient_in_scope with the reconciler's department and facilities: a patient at "
         'another facility reads as no such patient (MS-R14 (a), multisite-8h2).')),
    ('services/result_quarantine_service.py', '_message_names_another_patient'): appliance(
        'A wrong-patient guard across the whole appliance on purpose (a patient at another '
        'facility is still somebody else): it only refuses an attach, the refusal names no '
        'patient to the caller, and the other id goes to the audit row only.'),
    ('services/result_quarantine_service.py', '_same_name_and_birth_date'): appliance(
        'A wrong-patient guard across the whole appliance on purpose (a patient at another '
        'facility is still somebody else): it only refuses an attach, the refusal names no '
        'patient to the caller, and the other ids go to the audit row only.'),
}

SURFACES = {
    '/api/lab': blind_surface(
        'multisite-8h2',
        ('Laboratory worklists, results, criticals and reports. Every list and step keeps to '
         "the caller's facilities since multisite-8h2 (W13-D), but the order steps still reach "
         'orders_service.transition_in_conn and set_lab_result_text_in_conn (multisite-8) and '
         'the turnaround screen reaches reported_tests and _ward_clause, which the NABH '
         'indicators call with no facility (multisite-12).')),
    '/api/medorders': blind_surface(
        'multisite-8h2',
        ('Medication orders and the to-sign queue. The routes check the facility first since '
         'multisite-8h2 (W13-D), but create, sign and cancel reach the shared store helpers '
         '(services/medorders core, chart_lane, screening and queries) and '
         'drug_safety_service._screen_interactions, orders_service.'
         'mirror_medication_status_in_conn and shadow_service._lookup_ward (multisite-8).')),
    '/api/ingest': blind_surface(
        'multisite-8h2',
        ('Ingestion: the message lanes file by facility (multisite-16) and an unmatched result '
         "is attached only to a patient at the reconciler's facilities (multisite-8h2), but the "
         'unmatched results queue list and discard are not facility-aware (a queued row '
         'records no facility, and its route passes no reconciler to list_queue), status and '
         'replay are house-wide, and the message lanes reach Relay alerts, escalation, Sage '
         'and the screening helpers not yet swept.')),
}
