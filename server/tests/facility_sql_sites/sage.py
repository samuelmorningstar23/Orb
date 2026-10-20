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
    ('routes/sage_routes.py', '_audit_prefetch_patient'): patient_keyed(
        ['patients'], 1,
        ("Keyed on the route's own patient, which the one route that reaches it "
         '(POST /api/sage/prefetch) checks first with enforce_patient_scope.')),
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
    ('services/sage_relay_context.py', '_ROOM_MESSAGE_SQL'): facility(
        ['patients'], 1,
        ("The case-room message read. Its one user, fetch_room_messages, attaches the "
         "caller's department and facility predicate through sage_service._scope_sql "
         '(facilities=, multisite-10) after the same gate in Python.')),
    # services/sage_service.py
    ('services/sage_service.py', '_DEPARTMENT_SCOPE_SQL'): facility(
        ['admissions'], 1,
        ("Sage's pinned copy of PATIENT_DEPARTMENT_SQL, the department fragment its scope "
         'compares; with department names unique across the group (MS-R4 (a)) it keeps ward '
         'staff to their facility, and _scope_sql adds FACILITY_PATIENT_SQL beside it.')),
    ('services/sage_service.py', '_answer_label_for'): blind(
        'multisite-10',
        ('Keyed on one patient. Both callers vet that patient with _patient_in_scope '
         '(facilities=) first, but the gate is in the callers, which no kind records.')),
    ('services/sage_service.py', '_assemble_patient_context'): blind(
        'multisite-10',
        ('Facility-aware for a caller that passes facilities= (every person\'s Sage turn, '
         'multisite-10). The system lanes that pass dept None (the warm cache, '
         'sage_assessment_service._open_assessment, surgery_service) read any patient and '
         'hold no row of their own.')),
    ('services/sage_service.py', '_authoritative_news2_line'): blind(
        'multisite-10',
        'Keyed on one patient; sage_assessment_service calls it with no scope at all.'),
    ('services/sage_service.py', '_fresh_risk_and_observations'): blind(
        'multisite-10',
        'Keyed on one patient; sage_assessment_service and the voice fallback call it.'),
    ('services/sage_service.py', '_get_relevant_context'): facility(
        ['patients'], 5,
        ("A Sage turn's census, discharged list and broad lane: every statement goes through "
         "_scope_sql with the caller's department and facility scope (facilities=, "
         'multisite-10), both derived from the authenticated user inside the function.')),
    ('services/sage_service.py', '_out_of_scope_locators'): blind(
        'multisite-10',
        ("Built (multisite-10): the complement roster behind 'outside your department' is "
         "kept to the caller's own facilities (the scope _ScopedIds carries), a name found "
         "only at another facility gets the same 'no matching patient at <facility>' answer "
         'as a name found nowhere (MS-R13 (a)), and ids with no scope get no roster while '
         'more than one facility is on record. Still registered blind because '
         'tests/test_facility_sql_registry.py pins it so; reclassify as facility '
         "(['patients'], 1) when that pin is amended.")),
    ('services/sage_service.py', '_patient_in_scope'): facility(
        ['patients'], 1,
        ("Sage's scope check itself: one patient against the caller's department and, with "
         'facilities=, facility (multisite-10, in lockstep with permissions.patient_in_scope). '
         'Answers yes or no, never a row.')),
    ('services/sage_service.py', '_refresh_census_name_tokens'): blind(
        'multisite-10',
        ("The bed locator's vocabulary (R026, held): name tokens and bed and ward labels of "
         'every admitted patient on the appliance, module-wide. Another facility\'s labels '
         'still change how a question is read until the vocabulary is kept per facility.')),
    ('services/sage_service.py', 'build_aggregate_context'): facility(
        ['patients'], 4,
        ("PHI-free counts; its one caller (_get_relevant_context) passes the caller's "
         'facility scope, so the counts are of the caller\'s facilities only (multisite-10).')),
    # services/voice_agent_service.py
    ('services/voice_agent_service.py', 'build_patient_context'): facility(
        ['medications', 'patients'], 2,
        ('The voice fallback chart: keyed on one patient, read only after the gate at its '
         "top, _patient_in_scope with the department and facility scope it derives from the "
         'authenticated user (multisite-10).')),
    # ws/realtime.py
    ('ws/realtime.py', '_patient_department'): facility(
        ['admissions'], 1,
        ('Routing of the live vitals update: the department room. With more than one '
         "facility on record the update goes only to that room, the patient's facility room "
         "and the group room, never the whole-census room (multisite-10), and department "
         'names are unique across the group (MS-R4 (a)).')),
    ('ws/realtime.py', '_patient_facility'): facility(
        ['patients'], 1,
        ("Routing of the live vitals update: the facility of the patient's latest encounter "
         '(FACILITY_PATIENT_SQL), which picks the facility room (multisite-10).')),
    ('ws/realtime.py', '_patient_name'): facility(
        ['patients'], 1,
        ("The name rides only on an update emit_patient_vitals delivers to the patient's own "
         'department and facility rooms and the group room (multisite-10), whose members can '
         'read this patient over REST.')),
    ('ws/realtime.py', '_self_patient_id'): facility(
        ['patients'], 1,
        ("Binds a patient login's socket to its own record (the require_patient_self rule): "
         'the one room it may join. A patient login is never facility-bound (UNBOUND_ROLES).')),
}

SURFACES = {
    '/api/sage': blind_surface(
        'multisite-10',
        ("The query, voice, provenance and prefetch lanes are facility-aware (multisite-10); "
         'the handoff, ambient, assessment, governance, proactive and ward brief lanes and the '
         "bed locator's vocabulary still are not.")),
    '/socket.io': blind_surface(
        'multisite-10',
        ('Facility rooms are built (multisite-10); the handlers still reach the clinical '
         "lanes' blind rows (stay_times, the Relay channel list, Scribe hints; multisite-8).")),
}
