"""The Trust Center grades on evidence, not on the presence of a file or a key.

Each control here used to read green (or describe a protection) on the strength
of something that did not prove it: backups passed because scripts/backup.sh
exists, residency printed "explicit CORS allow-list" while the dev branch was
serving `*`, the database read encrypted because a key sat in .env, TLS was
amber whatever certificate was served, and twelve shipped default passwords
were found at boot and never mentioned. These tests pin the evidence each
control now reads.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from services import compliance_service
from services.compliance_service import compliance_report


def _control(report, cid):
    return next(c for c in report["controls"] if c["id"] == cid)


# ── The checklist enumerates every control ──────────────────────────────────

def test_every_control_is_enumerated():
    ids = {c["id"] for c in compliance_report()["controls"]}
    assert {
        "tls_in_transit", "enc_at_rest_db", "enc_at_rest_uploads",
        "default_credentials", "mfa", "audit_hash_chain", "rbac", "backups",
        "session_revocation", "data_residency", "deployment_profile",
        "abdm_certification", "nabh_dhs", "dpdp_consent",
        "dpdp_data_principal_rights", "dpdp_retention", "dpdp_breach_register",
    } <= ids


def test_screen_text_names_no_setting_and_no_api_path():
    """Evidence and remediation are read by a CIO. The installer's machine names
    travel in `settings`, which the screen does not print."""
    for c in compliance_report()["controls"]:
        for field in ("evidence", "remediation"):
            text = c[field]
            assert "/api/" not in text, (c["id"], text)
            assert not re.search(r"\bORB_[A-Z_]+", text), (c["id"], text)
            assert "\u2014" not in text, (c["id"], "em dash", text)


def test_national_certifications_are_stated_as_not_held():
    report = compliance_report()
    assert _control(report, "abdm_certification")["status"] == "warn"
    assert "not certified" in _control(report, "abdm_certification")["evidence"]
    assert _control(report, "nabh_dhs")["status"] == "warn"


# ── Backups: evidence, not a script ─────────────────────────────────────────

def _backup(dirpath, stamp, *, receipt_ok=None, age_hours=1.0):
    path = dirpath / f"orb-backup-{stamp}.db"
    path.write_bytes(b"x" * 4096)
    when = datetime.now().timestamp() - age_hours * 3600
    import os
    os.utime(path, (when, when))
    if receipt_ok is not None:
        (dirpath / f"orb-backup-{stamp}.verified.json").write_text(json.dumps({
            "schema": "orb.backup.verification/1", "ok": receipt_ok,
            "integrity_check": "ok" if receipt_ok else "corrupt",
            "row_counts": {"patients": 3, "audit_logs": 10},
            "verified_utc": datetime.now(timezone.utc).isoformat(),
        }))
    return path


def test_no_backups_is_a_warning_even_with_the_script_present(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path))
    control = _control(compliance_report(), "backups")
    assert control["status"] == "warn", control
    assert "no backup" in control["evidence"].lower()


def test_a_backup_that_failed_its_check_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path))
    _backup(tmp_path, "20261003-020000", receipt_ok=False)
    assert _control(compliance_report(), "backups")["status"] == "fail"


def test_an_unchecked_backup_is_a_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path))
    _backup(tmp_path, "20261003-020000", receipt_ok=None)
    control = _control(compliance_report(), "backups")
    assert control["status"] == "warn"
    assert "no record that it was checked" in control["evidence"]


def test_a_stale_backup_is_judged_against_the_site_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("ORB_HEARTBEAT_BACKUP_MAX_AGE_H", "24")
    _backup(tmp_path, "20261001-020000", receipt_ok=True, age_hours=72)
    control = _control(compliance_report(), "backups")
    assert control["status"] == "warn"
    assert "older than the 24 hours" in control["evidence"]


def test_a_fresh_checked_backup_still_needs_an_offsite_copy_and_a_drill(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path))
    _backup(tmp_path, "20261003-020000", receipt_ok=True)
    monkeypatch.delenv("ORB_BACKUP_OFFSITE_DIR", raising=False)
    control = _control(compliance_report(), "backups")
    assert control["status"] == "warn"
    assert "every copy is on this appliance" in control["evidence"]

    monkeypatch.setenv("ORB_BACKUP_OFFSITE_DIR", str(tmp_path / "offsite"))
    control = _control(compliance_report(), "backups")
    assert control["status"] == "warn"
    assert "no restore drill is on record" in control["evidence"]


# ── Residency: the CORS policy installed, not the one configured ────────────

def test_wildcard_cors_fails_residency(monkeypatch):
    with mock.patch.multiple("config", DEPLOYMENT_PROFILE="dev", HOST="127.0.0.1"):
        report = compliance_report()
    assert report["residency"]["cors_wildcard"] is True
    control = _control(report, "data_residency")
    assert control["status"] == "fail", control
    assert "allow-list" not in control["evidence"]


def test_named_origins_on_loopback_pass_residency(monkeypatch):
    with mock.patch.multiple("config", DEPLOYMENT_PROFILE="prod", HOST="127.0.0.1",
                             CORS_ORIGINS=["https://orb.hospital.local"]):
        report = compliance_report()
    assert report["residency"]["cors_origins"] == ["https://orb.hospital.local"]
    assert _control(report, "data_residency")["status"] == "pass"


def test_main_installs_the_origins_the_report_describes():
    import config
    from main import app
    from starlette.middleware.cors import CORSMiddleware

    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    assert list(cors.kwargs["allow_origins"]) == config.cors_allow_origins()
    # Credentials are never combined with a wildcard origin.
    if "*" in cors.kwargs["allow_origins"]:
        assert cors.kwargs["allow_credentials"] is False


# ── Encryption: the connection, not the key ─────────────────────────────────

def test_a_key_without_a_cipher_on_the_connection_fails(monkeypatch):
    import database
    monkeypatch.setattr("config.DB_ENCRYPTION_KEY", "a-configured-key")
    monkeypatch.setattr(database, "encryption_at_rest_status",
                        lambda: {"key_set": True, "cipher_version": None, "active": False})
    report = compliance_report()
    assert report["encryption"]["db_at_rest"] is False
    assert _control(report, "enc_at_rest_db")["status"] == "fail"


def test_an_encrypted_connection_passes(monkeypatch):
    import database
    monkeypatch.setattr(database, "encryption_at_rest_status",
                        lambda: {"key_set": True, "cipher_version": "4.6.1 community", "active": True})
    assert _control(compliance_report(), "enc_at_rest_db")["status"] == "pass"


def test_an_upload_enforcement_opt_out_is_shown(monkeypatch):
    from services import crypto_service
    monkeypatch.setattr(crypto_service, "is_encryption_enabled", lambda: True)
    monkeypatch.setenv("ORB_REQUIRE_UPLOAD_ENCRYPTION", "0")
    control = _control(compliance_report(), "enc_at_rest_uploads")
    assert control["status"] == "warn"
    assert "switched off" in control["evidence"]


# ── Default credentials ─────────────────────────────────────────────────────

def test_live_default_passwords_fail(monkeypatch):
    from services import bootstrap_service
    monkeypatch.setattr(bootstrap_service, "live_default_credential_accounts",
                        lambda: ["ADM001", "DOC001"])
    control = _control(compliance_report(), "default_credentials")
    assert control["status"] == "fail"
    assert "ADM001" in control["evidence"] and "Admin, Users" in control["remediation"]


def test_unchecked_default_passwords_are_unknown_not_pass(monkeypatch):
    from services import bootstrap_service
    monkeypatch.setattr(bootstrap_service, "live_default_credential_accounts", lambda: None)
    assert _control(compliance_report(), "default_credentials")["status"] == "unknown"


def test_a_password_reset_after_boot_clears_the_finding(monkeypatch):
    """The scan runs once at boot; the live check re-reads the flagged rows, so
    a reset clears the finding without a restart and without bcrypt."""
    import uuid
    from database import db_session
    from services import bootstrap_service

    uid = f"DEFCRED{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, 'Fixture', 'nurse', 'hash-A')",
                   (uid,))
    monkeypatch.setattr(bootstrap_service, "_snapshot", {
        "state": "ok", "scan_skipped": False, "default_credential_accounts": [uid],
    })
    monkeypatch.setattr(bootstrap_service, "_flagged_hash_digests",
                        {uid: bootstrap_service._hash_digest("hash-A")})
    assert bootstrap_service.live_default_credential_accounts() == [uid]

    with db_session() as db:
        db.execute("UPDATE users SET password_hash = 'hash-B' WHERE id = ?", (uid,))
    assert bootstrap_service.live_default_credential_accounts() == []


# ── MFA counts staff only ───────────────────────────────────────────────────

def test_mfa_adoption_leaves_out_patients_and_service_accounts():
    from database import SERVICE_ACCOUNT_IDS, db_session

    mfa = compliance_report()["mfa"]
    assert "patient" not in mfa["by_role"]
    with db_session() as db:
        staff = db.execute(
            "SELECT COUNT(*) FROM users WHERE role != 'patient' AND id NOT IN ({})".format(
                ",".join("?" for _ in SERVICE_ACCOUNT_IDS)),
            tuple(SERVICE_ACCOUNT_IDS),
        ).fetchone()[0]
    assert mfa["total_users"] == staff


# ── TLS: the certificate Orb serves ─────────────────────────────────────────

def _write_cert(tmp_path, *, self_signed=True, days=365, names=("orb.hospital.local",),
                expired=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "orb.hospital.local")])
    issuer = subject if self_signed else x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Hospital Root CA")])
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=400) if expired else now - timedelta(days=1)
    end = now - timedelta(days=1) if expired else now + timedelta(days=days)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(start).not_valid_after(end))
    if names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    cert_path, key_path = tmp_path / "orb.crt", tmp_path / "orb.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def _tls_control(**config_values):
    with mock.patch.multiple("config", **config_values):
        return _control(compliance_report(), "tls_in_transit")


def test_the_installer_self_signed_certificate_is_amber(tmp_path):
    cert, key = _write_cert(tmp_path, self_signed=True)
    control = _tls_control(TLS_CERT=cert, TLS_KEY=key, TLS_TERMINATED_UPSTREAM=False)
    assert control["status"] == "warn"
    assert "self-signed" in control["evidence"]


def test_an_expired_certificate_fails(tmp_path):
    cert, key = _write_cert(tmp_path, self_signed=False, expired=True)
    control = _tls_control(TLS_CERT=cert, TLS_KEY=key, TLS_TERMINATED_UPSTREAM=False)
    assert control["status"] == "fail"


def test_a_ca_issued_certificate_with_a_host_name_passes(tmp_path):
    cert, key = _write_cert(tmp_path, self_signed=False, days=200)
    control = _tls_control(TLS_CERT=cert, TLS_KEY=key, TLS_TERMINATED_UPSTREAM=False)
    assert control["status"] == "pass", control
    assert "orb.hospital.local" in control["evidence"]


def test_a_certificate_about_to_expire_is_amber(tmp_path):
    cert, key = _write_cert(tmp_path, self_signed=False, days=10)
    assert _tls_control(TLS_CERT=cert, TLS_KEY=key,
                        TLS_TERMINATED_UPSTREAM=False)["status"] == "warn"


def test_no_https_at_all_fails_in_the_prod_profile():
    control = _tls_control(TLS_CERT=None, TLS_KEY=None, TLS_TERMINATED_UPSTREAM=False,
                           DEPLOYMENT_PROFILE="prod")
    assert control["status"] == "fail"


def test_a_declared_proxy_is_amber_because_it_cannot_be_seen():
    control = _tls_control(TLS_CERT=None, TLS_KEY=None, TLS_TERMINATED_UPSTREAM=True)
    assert control["status"] == "warn"
    assert "proxy" in control["evidence"]


@pytest.mark.parametrize("probe", ["_backup_posture", "_tls_posture", "_default_credentials"])
def test_a_failed_probe_reports_unknown_never_a_verdict(monkeypatch, probe):
    monkeypatch.setattr(compliance_service, probe,
                        lambda: (_ for _ in ()).throw(RuntimeError("probe exploded")))
    report = compliance_report()
    cid = {"_backup_posture": "backups", "_tls_posture": "tls_in_transit",
           "_default_credentials": "default_credentials"}[probe]
    assert _control(report, cid)["status"] == "unknown"
