"""SQL-site registry: audit attribution and every audit reader.

Owned by W12-C (multisite-11). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

#: multisite-11 (wave 12) swept every reader here. The four rows that
#: tests/test_facility_sql_registry.py pins as named house-wide lanes
#: (get_audit_logs, get_audit_facets, get_timeline, get_event_detail) filter by
#: facility now but stay blind, with their route groups, until the fence's
#: lists are regenerated for the whole wave: that step moves them to
#: facility(['audit_logs'], 1 and 3) and facility(['audit_logs', 'orders'], 2)
#: and the two groups to cleared together, and amends that pin.
_PINNED = ("Filters by facility since multisite-11 (the epoch rule, services/audit_facility). "
           "Kept blind, with its route group, until the fence's lists are regenerated for the "
           "wave.")
_KEYED_FACT = ("Reads the audit rows about the one patient, visit or order its lane already "
               "holds, to decide one fact about it; it lists no row and names no other patient. "
               "Whether the caller may hold that patient is the lane's own scope check, swept "
               "with the lane.")

SITES = {
    # routes/admin_routes.py
    ('routes/admin_routes.py', 'admin_overview'): facility(
        ['audit_logs'], 4,
        ("The four audit counts carry the reader's facility condition "
         '(audit_facility.visible_sql, the epoch rule).')),
    # routes/admin_users_routes.py
    ('routes/admin_users_routes.py', 'list_users'): facility(
        ['audit_logs'], 2,
        ("The last sign-in and failed sign-in columns carry the reader's facility condition; "
         'the accounts themselves are listed by _staff_facility_view.')),
    # routes/audit_routes.py
    ('routes/audit_routes.py', 'get_audit_facets'): blind('multisite-11', _PINNED),
    ('routes/audit_routes.py', 'get_audit_logs'): blind('multisite-11', _PINNED),
    # services/audit_facility.py
    ('services/audit_facility.py', 'patient_facility'): facility(
        ['patients'], 1,
        ('The audit attribution machinery: the facility of the patient an audit row names, '
         'written into that row. It answers no reader.')),
    # services/audit_service.py
    ('services/audit_service.py', 'log_audit_event._do_insert'): appliance(
        ("The audit writer: appends one row after reading the previous row's hash. It returns "
         'nothing to the caller.')),
    # services/compliance_service.py
    ('services/compliance_service.py', '_audit_coverage'): facility(
        ['audit_logs'], 1,
        ("The coverage count carries the reader's facility condition when the report is read "
         'by a user; the console caller counts the appliance.')),
    # services/flight_recorder_service.py
    ('services/flight_recorder_service.py', '_chain_state'): appliance(
        ('The chain walk: reads every row to recompute its hash and keeps only the tip, the '
         'counts and the ids that fail. Whether the chain holds is appliance-level by design.')),
    ('services/flight_recorder_service.py', '_prefix_unchanged'): appliance(
        "The chain walk's cache check: a row count and one hash, never a row."),
    ('services/flight_recorder_service.py', 'get_event_detail'): blind('multisite-11', _PINNED),
    ('services/flight_recorder_service.py', 'get_timeline'): blind('multisite-11', _PINNED),
    ('services/flight_recorder_service.py', 'verify_chain'): facility(
        ['audit_logs'], 1,
        ("The per-patient count carries the reader's facility condition; ok and broken_at "
         'stay appliance-level by design.')),
    # services/medication_safety_gate.py
    ('services/medication_safety_gate.py', '_prescriber_override_on_file'): appliance(_KEYED_FACT),
    # services/medorders/outpatient.py
    ('services/medorders/outpatient.py', '_printed_before'): appliance(_KEYED_FACT),
    # services/nurse_shift_service.py
    ('services/nurse_shift_service.py', 'clerk_own_copies'): patient_keyed(
        ['audit_logs'], 1,
        ("Keyed on the route's own patient, which every route that reaches it checks first "
         'with require_patient_access.')),
    ('services/nurse_shift_service.py', 'observation_keyed_by'): appliance(_KEYED_FACT),
    # services/opd_queue_service.py
    ('services/opd_queue_service.py', 'reissued_tokens'): appliance(_KEYED_FACT),
    # services/registration_service.py
    ('services/registration_service.py', '_recent_failures_on'): appliance(_KEYED_FACT),
}

SURFACES = {
    '/api/audit': blind_surface(
        'multisite-11',
        ('The audit log filters by facility since multisite-11; cleared with the fence '
         'regeneration for the wave.')),
    '/api/flight-recorder': blind_surface(
        'multisite-11',
        ('The flight recorder filters by facility since multisite-11; cleared with the fence '
         'regeneration for the wave.')),
}
