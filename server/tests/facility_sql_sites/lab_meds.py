"""SQL-site registry: LIS, medication-order and ingestion lanes.

Owned by W13-D (multisite-8h2). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

SITES = {
    # routes/lab/reports.py
    ('routes/lab/reports.py', 'list_patient_reports'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    # routes/medorders/core.py
    ('routes/medorders/core.py', '_patient_in_scope_or_404'): blind('multisite-8h2'),
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
    ('services/lab_critical_service.py', '_LIST_SELECT'): blind('multisite-8h2'),
    ('services/lab_critical_service.py', '_interfaced_message_text'): blind('multisite-8h2'),
    ('services/lab_critical_service.py', '_message_text'): blind('multisite-8h2'),
    ('services/lab_critical_service.py', 'deliver_in_txn'): blind('multisite-8h2'),
    ('services/lab_critical_service.py', 'raise_for_result_in_txn'): blind('multisite-8h2'),
    ('services/lab_critical_service.py', 'raise_interfaced'): blind('multisite-8h2'),
    # services/lab_master_service.py
    ('services/lab_master_service.py', 'patient_demographics'): blind('multisite-8h2'),
    # services/lab_order_service.py
    ('services/lab_order_service.py', '_LAB_SPECIMEN_SELECT'): blind('multisite-8h2'),
    ('services/lab_order_service.py', '_assess'): blind('multisite-8h2'),
    ('services/lab_order_service.py', '_collection_rows'): blind('multisite-8h2'),
    ('services/lab_order_service.py', '_sweep_candidates'): blind('multisite-8h2'),
    ('services/lab_order_service.py', 'cancel_order'): blind('multisite-8h2'),
    ('services/lab_order_service.py', 'list_open_exceptions'): blind('multisite-8h2'),
    ('services/lab_order_service.py', 'post_rejection_notice'): blind('multisite-8h2'),
    ('services/lab_order_service.py', 'reconcile_order_status'): blind('multisite-8h2'),
    ('services/lab_order_service.py', 'record_order_note_in_conn'): blind('multisite-8h2'),
    ('services/lab_order_service.py', 'settle_after_withdrawal'): blind('multisite-8h2'),
    # services/lab_report_service.py
    ('services/lab_report_service.py', '_load'): blind('multisite-8h2'),
    ('services/lab_report_service.py', 'patient_by_uhid'): blind('multisite-8h2'),
    # services/lab_result_service.py
    ('services/lab_result_service.py', '_QUEUE_SELECT'): blind('multisite-8h2'),
    ('services/lab_result_service.py', '_WORKLIST_SELECT'): blind('multisite-8h2'),
    ('services/lab_result_service.py', '_apply_amendment'): blind('multisite-8h2'),
    ('services/lab_result_service.py', 'post_result_notice'): blind('multisite-8h2'),
    ('services/lab_result_service.py', 'reported_results'): blind('multisite-8h2'),
    ('services/lab_result_service.py', 'withdraw_result'): blind('multisite-8h2'),
    ('services/lab_result_service.py', 'worklist'): blind('multisite-8h2'),
    # services/lab_tat_service.py
    ('services/lab_tat_service.py', '_ward_clause'): blind('multisite-8h2'),
    ('services/lab_tat_service.py', 'open_exception_counts'): blind('multisite-8h2'),
    ('services/lab_tat_service.py', 'overdue_tests'): blind('multisite-8h2'),
    ('services/lab_tat_service.py', 'reported_tests'): blind('multisite-8h2'),
    # services/medorders/adoption.py
    ('services/medorders/adoption.py', 'adopt_legacy'): blind('multisite-8h2'),
    ('services/medorders/adoption.py', 'parity_report'): blind('multisite-8h2'),
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
    ('services/medorders/queries.py', 'active_admission_id'): blind('multisite-8h2'),
    ('services/medorders/queries.py', 'list_for_patient'): patient_keyed(
        ['medications'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/medorders/queries.py', 'same_ingredient_live'): blind('multisite-8h2'),
    ('services/medorders/queries.py', 'to_sign_queue'): blind('multisite-8h2'),
    # services/medorders/screening.py
    ('services/medorders/screening.py', '_admitted'): blind('multisite-8h2'),
    ('services/medorders/screening.py', '_excluded'): blind('multisite-8h2'),
    ('services/medorders/screening.py', '_open_visits_from_today'): blind('multisite-8h2'),
    ('services/medorders/screening.py', 'comparison_rows'): blind('multisite-8h2'),
    # services/result_quarantine_service.py
    ('services/result_quarantine_service.py', '_find_patient'): blind('multisite-8h2'),
    ('services/result_quarantine_service.py', '_message_names_another_patient'): blind('multisite-8h2'),
    ('services/result_quarantine_service.py', '_same_name_and_birth_date'): blind('multisite-8h2'),
}

SURFACES = {
    '/api/lab': blind_surface(
        'multisite-8h2',
        'Laboratory worklists, results, criticals and reports.'),
    '/api/medorders': blind_surface(
        'multisite-8h2',
        'Medication orders and the to-sign queue.'),
    '/api/ingest': blind_surface(
        'multisite-8h2',
        ('Ingestion: the message lanes file by facility (multisite-16), but status, replay and'
         ' the unmatched results queue are not facility-aware, and the message lanes reach '
         'Relay alerts, escalation and lab criticals not yet swept.')),
}
