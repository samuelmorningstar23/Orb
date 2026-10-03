"""Phase 1 compliance MVP: PHI mutation audit middleware + patient-scoped,
audited /uploads media access.

Plus (from 2026-08-14) the Trust Center's DEPLOYMENT POSTURE contract — see the
section at the bottom of this file."""
from unittest import mock

import pytest

import routes.ingestion_routes  # noqa: F401

with mock.patch("config.INGESTION_MODE", "hybrid"), \
     mock.patch("routes.ingestion_routes.INGESTION_MODE", "hybrid"), \
     mock.patch("main.INGESTION_MODE", "hybrid"):
    from fastapi.testclient import TestClient
    from main import app
    from database import db_session

client = TestClient(app)


def _doctor_token():
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            ("CMP-DOC", "Dr Cmp", "doctor", hash_password("cmp12345")),
        )
    r = client.post("/api/auth/login", json={"employee_id": "CMP-DOC", "password": "cmp12345"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


_SEEDED: list[str] = []


@pytest.fixture(autouse=True)
def _discharge_seeded_patients():
    """TEST-ISOLATION LAW: patients.status DEFAULTS to 'admitted', so every
    patient `_make_patient` seeds used to stay admitted in the shared session DB
    — 2 by the end of the module. That is not a cosmetic leak. The FHIR
    self-export in services/downtime_service.py iterates
    `patients WHERE status='admitted'` and sleeps _PATIENT_SLEEP_S (250 ms)
    BETWEEN PATIENTS, so each leftover adds ~0.275 s to every export call in
    tests/test_downtime_fhir_export.py and to everything downstream of it.

    The seeding happens in a plain helper rather than a fixture, so the helper
    registers each id here and this autouse teardown discharges the batch. Do
    not DELETE — the audit_logs rows this module exists to assert on
    legitimately reference these ids."""
    yield
    if not _SEEDED:
        return
    with db_session() as db:
        db.executemany(
            "UPDATE patients SET status = 'discharged' WHERE id = ?",
            [(p,) for p in _SEEDED],
        )
    _SEEDED.clear()


def _make_patient():
    import uuid
    pid = f"PATCMP{uuid.uuid4().hex[:5].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn) VALUES (?,?,?)",
                   (pid, "Cmp Patient", f"MRN-{uuid.uuid4().hex[:6]}"))
    _SEEDED.append(pid)
    return pid


def test_mutation_is_audited_by_middleware():
    token = _doctor_token()
    pid = _make_patient()
    # A state-changing request through a route that has NO route-level audit of
    # its own body still gets caught by the middleware.
    r = client.post(
        f"/api/patients/{pid}/problems",
        headers={"Authorization": f"Bearer {token}"},
        json={"problem": "Test problem"},
    )
    assert r.status_code == 200, r.text
    with db_session() as db:
        row = db.execute(
            "SELECT * FROM audit_logs WHERE event_category = 'api_mutation' "
            "AND request_path = ? AND http_method = 'POST' ORDER BY id DESC LIMIT 1",
            (f"/api/patients/{pid}/problems",),
        ).fetchone()
    assert row is not None, "middleware should have logged the mutation"
    assert row["actor_user_id"] == "CMP-DOC"


def test_uploads_unregistered_file_is_404():
    token = _doctor_token()
    r = client.get("/uploads/not-a-real-media-file.png",
                   headers={"Authorization": f"Bearer {token}"})
    # Not a registered lens/scribe media record → not served.
    assert r.status_code == 404


def test_uploads_requires_clinical_role():
    # A patient-role user must not reach media at all.
    from auth import hash_password
    with db_session() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
            ("CMP-PAT", "Pat Cmp", "patient", hash_password("cmp12345")),
        )
    r = client.post("/api/auth/login", json={"employee_id": "CMP-PAT", "password": "cmp12345"})
    token = r.json()["token"]
    resp = client.get("/uploads/anything.png", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_registered_lens_media_is_served_and_audited(tmp_path):
    import os
    from config import UPLOAD_DIR
    token = _doctor_token()
    pid = _make_patient()
    # Create a real file in UPLOAD_DIR and register it as a lens analysis.
    fname = "cmp_test_image.png"
    fpath = os.path.join(str(UPLOAD_DIR), fname)
    with open(fpath, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n fake")
    try:
        with db_session() as db:
            db.execute(
                "INSERT INTO lens_analyses (patient_id, author_id, image_path, analysis) "
                "VALUES (?,?,?,?)",
                (pid, "CMP-DOC", f"uploads/{fname}", "test analysis"),
            )
        resp = client.get(f"/uploads/{fname}", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        with db_session() as db:
            row = db.execute(
                "SELECT * FROM audit_logs WHERE action_type = 'media_view' "
                "AND patient_id = ? ORDER BY id DESC LIMIT 1", (pid,),
            ).fetchone()
        assert row is not None
    finally:
        if os.path.exists(fpath):
            os.remove(fpath)


# ═════════════════════════════════════════════════════════════════════════════
#  DEPLOYMENT POSTURE — the Trust Center must not over-claim
# ═════════════════════════════════════════════════════════════════════════════
#
# THE DEFECT (2026-08-14 pilot dry run). GET /api/admin/compliance/ returned
# deployment_profile "prod" on a box where .env said dev. The report read
# `config.DEPLOYMENT_PROFILE`, and config.py line 11 defaults that to "prod" when
# ORB_DEPLOYMENT_PROFILE is unset. That default is right where it lives — the
# JWT-secret and ingestion-key guards must fail CLOSED — but the same string was
# being handed to a compliance reviewer as an attestation. It made the AUDITOR-
# FACING screen fail toward the STRONGER claim: it told a reviewer the appliance
# was hardened when nobody had ever said so.
#
# THE CONTRACT THESE TESTS PIN:
#   * the reported profile is the EFFECTIVE, OBSERVED posture, not a label;
#   * "prod" is claimed only when prod was declared explicitly AND no
#     development posture is observable;
#   * every uncertainty resolves to the WEAKER claim (dev, or unknown) — never
#     to prod.

_PROD_ENVIRONMENT = {
    "config.DEPLOYMENT_PROFILE": "prod",
    "config.JWT_SECRET": "a-real-site-supplied-signing-secret",
    "config.INGESTION_API_KEY": "a-real-site-supplied-ingestion-key",
    "config.INGESTION_MODE": "ingestion",
    "config.DEMO_MODE": False,
}


def _posture(monkeypatch, *, declared="prod", declare_env=True, **overrides):
    """Run the posture probe against a synthetic config.

    NOTE ON WHY THE SECRETS MUST BE PATCHED: under pytest, config.py's fail-open
    branch hands JWT_SECRET and INGESTION_API_KEY their DEV fallbacks (the
    `is_pytest` guards), so a test that only set the profile would be exercising
    a box that is genuinely running dev credentials — and would prove nothing
    about the prod branch.
    """
    from services.compliance_service import effective_deployment_posture

    env = dict(_PROD_ENVIRONMENT)
    env["config.DEPLOYMENT_PROFILE"] = declared
    env.update({f"config.{k}": v for k, v in overrides.items()})

    if declare_env:
        monkeypatch.setenv("ORB_DEPLOYMENT_PROFILE", declared)
    else:
        monkeypatch.delenv("ORB_DEPLOYMENT_PROFILE", raising=False)

    with mock.patch.multiple("config", **{k.split(".", 1)[1]: v for k, v in env.items()}):
        return effective_deployment_posture()


@pytest.mark.parametrize("declared", ["dev", "prod"])
def test_profile_matches_the_effective_runtime_posture(monkeypatch, declared):
    """Declared dev reads dev; declared prod on an otherwise clean box reads prod."""
    posture = _posture(monkeypatch, declared=declared)
    assert posture["declared"] == declared
    assert posture["effective"] == declared, posture
    assert posture["hardened"] is (declared == "prod")


def test_undeclared_profile_is_dev_not_prod(monkeypatch):
    """config.py's fail-closed "prod" default must never become an attestation.

    This is the exact live shape of the bug: the variable is unset, config
    resolves to "prod" so the secret guards bite, and the Trust Center used to
    print "prod profile" to an auditor on that basis alone.
    """
    posture = _posture(monkeypatch, declared="prod", declare_env=False)
    assert posture["declared"] == "prod"          # config's fail-closed default
    assert posture["declared_explicitly"] is False
    assert posture["effective"] == "dev", posture  # …but nobody ever said prod
    assert posture["hardened"] is False
    assert "profile_not_declared" in {s["code"] for s in posture["dev_signals"]}


def test_dev_signing_secret_downgrades_a_prod_label(monkeypatch):
    """A box anyone can mint a session token for is not a hardened box."""
    posture = _posture(monkeypatch, declared="prod",
                       JWT_SECRET="orb-local-dev-secret-key-change-in-prod")
    assert posture["effective"] == "dev"
    assert "dev_jwt_secret" in {s["code"] for s in posture["dev_signals"]}


def test_dev_ingestion_key_downgrades_a_prod_label(monkeypatch):
    posture = _posture(monkeypatch, declared="prod",
                       INGESTION_API_KEY="orb-ingest-dev-key")
    assert posture["effective"] == "dev"
    assert "dev_ingestion_key" in {s["code"] for s in posture["dev_signals"]}


@pytest.mark.parametrize("mode", ["simulation", "hybrid"])
def test_running_vitals_simulator_downgrades_a_prod_label(monkeypatch, mode):
    """The live dry-run box: ORB_DEPLOYMENT_PROFILE=prod with the simulator on.

    Whatever the label says, an appliance writing fabricated vitals into patient
    charts is not in a hospital posture, and an auditor must be told so.
    """
    posture = _posture(monkeypatch, declared="prod", INGESTION_MODE=mode)
    assert posture["effective"] == "dev", posture
    assert "synthetic_vitals_active" in {s["code"] for s in posture["dev_signals"]}


def test_demo_mode_downgrades_a_prod_label(monkeypatch):
    posture = _posture(monkeypatch, declared="prod", DEMO_MODE=True)
    assert posture["effective"] == "dev"
    assert "demo_mode" in {s["code"] for s in posture["dev_signals"]}


def test_unreadable_profile_never_resolves_to_prod(monkeypatch):
    """A config that yields no readable profile must land on a WEAKER claim.

    Either answer ("dev" because nothing was declared, or "unknown" because
    nothing could be read) is acceptable; "prod" is not. This is the direction-
    of-failure rule stated as a test.
    """
    posture = _posture(monkeypatch, declared="", declare_env=False)
    assert posture["declared"] == "unknown"
    assert posture["effective"] in ("dev", "unknown")
    assert posture["effective"] != "prod"
    assert posture["hardened"] is False


@pytest.mark.parametrize("declared", ["dev", "prod"])
def test_report_prints_the_effective_profile(monkeypatch, declared):
    """`residency.deployment_profile` is the string the Trust Center header
    prints ("… · {profile} profile · on-prem"), so it is the one that must not
    over-claim. The declared value is carried alongside it, not instead of it."""
    from services.compliance_service import compliance_report

    monkeypatch.setenv("ORB_DEPLOYMENT_PROFILE", declared)
    with mock.patch.multiple(
        "config",
        DEPLOYMENT_PROFILE=declared,
        JWT_SECRET="a-real-site-supplied-signing-secret",
        INGESTION_API_KEY="a-real-site-supplied-ingestion-key",
        INGESTION_MODE="ingestion",
        DEMO_MODE=False,
    ):
        report = compliance_report()

    residency = report["residency"]
    assert residency["deployment_profile"] == declared
    assert residency["declared_deployment_profile"] == declared
    assert residency["deployment_posture"]["effective"] == declared


def test_report_never_prints_prod_when_the_box_is_not(monkeypatch):
    """End to end through the public report, in the live dry-run configuration."""
    from services.compliance_service import compliance_report

    monkeypatch.setenv("ORB_DEPLOYMENT_PROFILE", "prod")
    with mock.patch.multiple(
        "config",
        DEPLOYMENT_PROFILE="prod",
        JWT_SECRET="a-real-site-supplied-signing-secret",
        INGESTION_API_KEY="a-real-site-supplied-ingestion-key",
        INGESTION_MODE="simulation",   # the simulator is running
        DEMO_MODE=True,
    ):
        report = compliance_report()

    assert report["residency"]["deployment_profile"] == "dev"
    assert report["residency"]["declared_deployment_profile"] == "prod"
    control = next(c for c in report["controls"] if c["id"] == "deployment_profile")
    assert control["status"] == "warn", control
    # The control must name the reason, not just assert a label.
    assert "simulator" in control["evidence"].lower()


def test_deployment_control_passes_only_on_a_real_prod_box(monkeypatch):
    from services.compliance_service import compliance_report

    monkeypatch.setenv("ORB_DEPLOYMENT_PROFILE", "prod")
    with mock.patch.multiple(
        "config",
        DEPLOYMENT_PROFILE="prod",
        JWT_SECRET="a-real-site-supplied-signing-secret",
        INGESTION_API_KEY="a-real-site-supplied-ingestion-key",
        INGESTION_MODE="ingestion",
        DEMO_MODE=False,
    ):
        report = compliance_report()
    control = next(c for c in report["controls"] if c["id"] == "deployment_profile")
    assert control["status"] == "pass", control


def test_residency_probe_failure_reports_unknown_not_prod(monkeypatch):
    """A probe that cannot run must degrade to 'unknown'. The default it falls
    back to is part of the contract: it is the last line between a broken probe
    and a false attestation."""
    from services import compliance_service

    monkeypatch.setattr(
        compliance_service, "_residency",
        lambda: (_ for _ in ()).throw(RuntimeError("probe exploded")),
    )
    report = compliance_service.compliance_report()
    assert report["residency"]["deployment_profile"] == "unknown"
    control = next(c for c in report["controls"] if c["id"] == "deployment_profile")
    assert control["status"] == "unknown", control


def test_ops_health_and_trust_center_agree_on_the_profile(monkeypatch):
    """The operator page and the auditor page read the SAME definition. Two
    screens disagreeing about which mode the appliance is in is how the original
    bug survived: the boot log said one thing and the Trust Center another."""
    from services.compliance_service import effective_deployment_posture
    from services import ops_health_service

    monkeypatch.setenv("ORB_DEPLOYMENT_PROFILE", "prod")
    with mock.patch.multiple(
        "config",
        DEPLOYMENT_PROFILE="prod",
        JWT_SECRET="a-real-site-supplied-signing-secret",
        INGESTION_API_KEY="a-real-site-supplied-ingestion-key",
        INGESTION_MODE="simulation",
        DEMO_MODE=False,
    ):
        posture = effective_deployment_posture()
        check = ops_health_service._check_server()

    assert posture["effective"] == "dev"
    assert check["detail"]["profile"] == "dev"
    assert check["status"] == "attention", check
    assert "synthetic_vitals_active" in check["detail"]["dev_signals"]
