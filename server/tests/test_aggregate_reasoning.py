"""PHI-free aggregate reasoning tests.

The aggregate scope lets Sage answer hospital-level count/distribution
questions from a context of counts and distributions only — zero patient rows,
so the turn is PHI-free and audited as such. These tests prove:
  1. an aggregate query classifies PHI-FREE,
  2. any per-patient signal downgrades it back to PHI,
  3. the aggregate context contains NO patient identifiers.
"""
from services.ai_governance import classify_query_sensitivity
from services.sage_service import _is_aggregate_only, build_aggregate_context, get_relevant_context


AGGREGATE_QUERIES = [
    "how many patients are currently admitted",
    "what is our ICU bed occupancy",
    "current risk distribution across the hospital",
    "what is the average length of stay",
    "how many critical right now",
]

PER_PATIENT_QUERIES = [
    "how many days has this patient been admitted",     # pronoun
    "list the admitted patients",                        # enumeration
    "which patients are critical",                       # which patient
    "show me admitted patients and their beds",          # show me / their
]


def test_aggregate_only_helper():
    for q in AGGREGATE_QUERIES:
        assert _is_aggregate_only(q.lower()) is True, q
    for q in PER_PATIENT_QUERIES:
        assert _is_aggregate_only(q.lower()) is False, q


def test_aggregate_query_classifies_phi_free():
    for q in AGGREGATE_QUERIES:
        c = classify_query_sensitivity(q)
        assert c.contains_phi is False, q
        assert c.query_class == "aggregate_stats", q


def test_per_patient_signal_stays_phi():
    for q in PER_PATIENT_QUERIES:
        c = classify_query_sensitivity(q)
        assert c.contains_phi is True, q


def test_attached_patient_overrides_aggregate():
    # Even a count-shaped query is PHI if a specific patient is attached.
    c = classify_query_sensitivity("how many critical", patient_id="P001")
    assert c.contains_phi is True


def test_aggregate_context_contains_no_patient_identifiers():
    ctx = build_aggregate_context("how many admitted")
    # Must be counts/distributions only — never a name/bed/MRN/dx string.
    assert "MRN" not in ctx
    assert "Dx:" not in ctx
    # No "Name | Bed" census-row shape.
    assert "|" not in ctx or "occupancy" in ctx.lower()
    # Sanity: it does report a count.
    assert "admitted" in ctx.lower()


def test_get_relevant_context_returns_aggregate_scope():
    # A user with no department on record reads hospital-wide — the
    # permissions.department_scope() convention. The aggregate lane is
    # PHI-free by construction either way (counts and distributions only).
    context, scope, sources, conf, esc, pctx, qscope = get_relevant_context(
        "what is the current bed occupancy", {"id": "TESTUSER", "role": "admin"}, None, None
    )
    assert scope == "aggregate"
    assert qscope == "AGGREGATE_STATS"
    assert pctx is None  # no per-patient context dict
