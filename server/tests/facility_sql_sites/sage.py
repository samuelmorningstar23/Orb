"""SQL-site registry: the Sage lane and socket rooms.

Owned by W12-B (multisite-10). The rows and route groups this area's sweep
owns; see __init__.py for the kinds and the rules. A sweep that makes a lane
facility-aware moves its rows to a safe kind (with tables, count and reason)
and its route group to 'cleared', then regenerates the fence's lists (python
-m tests.facility_sql_sites, from server/).
"""
from . import (appliance, blind, blind_surface, cleared_surface, document_keyed,  # noqa: F401
               facility, no_patient_data_surface, patient_keyed)

SITES = {
    # routes/sage_routes.py
    ('routes/sage_routes.py', '_audit_prefetch_patient'): blind('multisite-10'),
    # services/ai_governance.py
    ('services/ai_governance.py', '_match_patient_identifiers'): blind('multisite-10'),
    # services/sage_ambient_service.py
    ('services/sage_ambient_service.py', '_fetch_patient_context'): blind('multisite-10'),
    # services/sage_assessment_service.py
    ('services/sage_assessment_service.py', '_tier1'): blind('multisite-10'),
    # services/sage_handoff_service.py
    ('services/sage_handoff_service.py', '_alert_assigned_team'): blind('multisite-10'),
    ('services/sage_handoff_service.py', '_dispatch_to_transfer'): blind('multisite-10'),
    ('services/sage_handoff_service.py', '_mark_generating'): blind('multisite-10'),
    ('services/sage_handoff_service.py', '_persist_package'): blind('multisite-10'),
    ('services/sage_handoff_service.py', '_resolve_surgeon_id'): blind('multisite-10'),
    ('services/sage_handoff_service.py', '_transfer_through_adt_on'): blind('multisite-10'),
    ('services/sage_handoff_service.py', '_write_helix'): blind('multisite-10'),
    # services/sage_narrator_service.py
    ('services/sage_narrator_service.py', 'WardNarrator._build_ward_snapshot'): blind('multisite-10'),
    # services/sage_proactive_service.py
    ('services/sage_proactive_service.py', '_read_moment'): blind('multisite-10'),
    ('services/sage_proactive_service.py', 'evaluate_all_admitted'): blind('multisite-10'),
    ('services/sage_proactive_service.py', 'get_active_insights'): blind('multisite-10'),
    # services/sage_relay_context.py
    ('services/sage_relay_context.py', '_ROOM_MESSAGE_SQL'): blind('multisite-10'),
    # services/sage_service.py
    ('services/sage_service.py', '_DEPARTMENT_SCOPE_SQL'): blind('multisite-10'),
    ('services/sage_service.py', '_answer_label_for'): blind('multisite-10'),
    ('services/sage_service.py', '_assemble_patient_context'): blind('multisite-10'),
    ('services/sage_service.py', '_authoritative_news2_line'): blind('multisite-10'),
    ('services/sage_service.py', '_fresh_risk_and_observations'): blind('multisite-10'),
    ('services/sage_service.py', '_get_relevant_context'): blind('multisite-10'),
    ('services/sage_service.py', '_out_of_scope_locators'): blind(
        'multisite-10',
        ('The complement set: every admitted or discharged patient, an existence oracle across'
         ' facilities until MS-R13 (a) is built.')),
    ('services/sage_service.py', '_patient_in_scope'): blind('multisite-10'),
    ('services/sage_service.py', '_refresh_census_name_tokens'): blind('multisite-10'),
    ('services/sage_service.py', 'build_aggregate_context'): blind('multisite-10'),
    # services/voice_agent_service.py
    ('services/voice_agent_service.py', 'build_patient_context'): blind('multisite-10'),
    # ws/realtime.py
    ('ws/realtime.py', '_patient_department'): blind('multisite-10'),
    ('ws/realtime.py', '_patient_name'): blind('multisite-10'),
    ('ws/realtime.py', '_self_patient_id'): blind('multisite-10'),
}

SURFACES = {
    '/api/sage': blind_surface(
        'multisite-10',
        'Sage reads the census and the out-of-scope locator.'),
    '/socket.io': blind_surface(
        'multisite-10',
        'Socket rooms are per department, not yet per facility.'),
}
