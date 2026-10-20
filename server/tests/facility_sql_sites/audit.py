"""SQL-site registry: audit attribution and every audit reader.

Owned by W12-C (multisite-11). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

SITES = {
    # routes/admin_routes.py
    ('routes/admin_routes.py', 'admin_overview'): blind('multisite-11'),
    # routes/admin_users_routes.py
    ('routes/admin_users_routes.py', 'list_users'): blind('multisite-11'),
    # routes/audit_routes.py
    ('routes/audit_routes.py', 'get_audit_facets'): blind('multisite-11'),
    ('routes/audit_routes.py', 'get_audit_logs'): blind(
        'multisite-11',
        'Every audit row, whatever facility it names.'),
    # services/audit_service.py
    ('services/audit_service.py', 'log_audit_event._do_insert'): appliance(
        ("The audit writer: appends one row after reading the previous row's hash. It returns "
         'nothing to the caller.')),
    # services/compliance_service.py
    ('services/compliance_service.py', '_audit_coverage'): blind('multisite-11'),
    # services/flight_recorder_service.py
    ('services/flight_recorder_service.py', '_chain_state'): blind('multisite-11'),
    ('services/flight_recorder_service.py', '_prefix_unchanged'): blind('multisite-11'),
    ('services/flight_recorder_service.py', 'get_event_detail'): blind('multisite-11'),
    ('services/flight_recorder_service.py', 'get_timeline'): blind(
        'multisite-11',
        'Every order event and audit row.'),
    ('services/flight_recorder_service.py', 'verify_chain'): blind('multisite-11'),
    # services/medication_safety_gate.py
    ('services/medication_safety_gate.py', '_prescriber_override_on_file'): blind('multisite-11'),
    # services/medorders/outpatient.py
    ('services/medorders/outpatient.py', '_printed_before'): blind('multisite-11'),
    # services/nurse_shift_service.py
    ('services/nurse_shift_service.py', 'clerk_own_copies'): patient_keyed(
        ['audit_logs'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/nurse_shift_service.py', 'observation_keyed_by'): blind('multisite-11'),
    # services/opd_queue_service.py
    ('services/opd_queue_service.py', 'reissued_tokens'): blind('multisite-11'),
    # services/registration_service.py
    ('services/registration_service.py', '_recent_failures_on'): blind('multisite-11'),
}

SURFACES = {
    '/api/audit': blind_surface(
        'multisite-11',
        'The audit log lists every row.'),
    '/api/flight-recorder': blind_surface(
        'multisite-11',
        'The flight recorder lists every event.'),
}
