"""Compliance & Trust Center — the compliance posture aggregated from real
system state into a self-serve report. These tests assert the report's contract
holds and that it never crashes on a fresh DB."""
from services.compliance_service import compliance_report

_VALID_STATUSES = {"pass", "warn", "fail", "unknown"}
# Controls the checklist must always enumerate (honesty: some default to warn).
_EXPECTED_CONTROL_IDS = {
    "tls_in_transit", "enc_at_rest_db", "enc_at_rest_uploads", "mfa",
    "audit_hash_chain", "rbac", "backups", "session_revocation", "data_residency",
}


def test_report_does_not_crash_and_has_top_level_shape():
    """Fresh DB (no PHI, possibly no audit rows) must still produce a full report."""
    report = compliance_report()
    for key in ("generated_at", "overall_score", "controls", "mfa",
                "rbac_matrix", "audit_chain", "encryption", "models"):
        assert key in report, f"missing top-level key: {key}"


def test_overall_score_in_range():
    report = compliance_report()
    score = report["overall_score"]
    assert isinstance(score, int)
    assert 0 <= score <= 100


def test_control_checklist_is_present_and_well_formed():
    report = compliance_report()
    controls = report["controls"]
    assert isinstance(controls, list) and controls, "control list must be non-empty"
    ids = set()
    for c in controls:
        assert {"id", "name", "status", "evidence", "remediation"} <= set(c)
        assert c["status"] in _VALID_STATUSES, f"bad status: {c['status']}"
        assert c["evidence"] and c["remediation"], "controls need evidence + remediation"
        ids.add(c["id"])
    # The full auditor checklist is always enumerated, even when some are warn.
    assert _EXPECTED_CONTROL_IDS <= ids


def test_rbac_matrix_includes_doctor_and_nurse():
    report = compliance_report()
    rbac = report["rbac_matrix"]
    assert "doctor" in rbac["roles"]
    assert "nurse" in rbac["roles"]
    # The matrix is a role × permission grid of booleans.
    assert "doctor" in rbac["matrix"]
    perms = rbac["permissions"]
    assert perms, "permission list must be non-empty"
    doctor_row = rbac["matrix"]["doctor"]
    assert set(doctor_row.keys()) == set(perms)
    assert all(isinstance(v, bool) for v in doctor_row.values())


def test_audit_chain_key_present_and_shaped():
    report = compliance_report()
    audit = report["audit_chain"]
    assert "verified" in audit
    assert "count" in audit
    assert isinstance(audit["count"], int)


def test_encryption_and_models_posture_present():
    report = compliance_report()
    enc = report["encryption"]
    assert "db_at_rest" in enc and "uploads_at_rest" in enc
    models = report["models"]
    assert "validated" in models and "total" in models
    assert models["validated"] <= models["total"]
