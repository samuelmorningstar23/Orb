"""The address a patient's discharge link and QR code point at.

Finding (3 October 2026): the link is ORB_PUBLIC_APP_BASE_URL + "/discharge/<token>".
That setting defaults to the Vite dev server (http://localhost:5173),
.env.example writes the same value, and the install runbook never set it, so on
an appliance installed by the book every QR a patient scanned pointed at their
own phone.

The invariants:
  1. In the prod profile, an address a phone cannot open (unset, not https, or
     this machine) issues NO link, token or QR. The discharge still completes,
     and the desk is told why.
  2. A configured https address is exactly what lands in the link and the QR,
     and that link opens.
  3. The dev profile is untouched: localhost:5173 is where the app really is.
  4. The operator hears it at boot, and the Trust Center shows it in prod.
"""
import inspect
import logging
import re
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from database import db_session
from main import app
from services import compliance_service, discharge_service
from services.discharge_access_service import (
    NO_PUBLIC_ADDRESS_REASON,
    log_public_address_problem,
    public_address_problem,
    public_link_base_status,
)

client = TestClient(app)

_DEFAULT = "http://localhost:5173"
_APPLIANCE = "https://orb.hospital.local:8000"


def _prod(base_url):
    return mock.patch.multiple("config", DEPLOYMENT_PROFILE="prod", PUBLIC_APP_BASE_URL=base_url)


def _dev(base_url):
    return mock.patch.multiple("config", DEPLOYMENT_PROFILE="dev", PUBLIC_APP_BASE_URL=base_url)


def _seed_patient(patient_id: str, *, mrn: str, dob="1964-03-11") -> None:
    with db_session() as db:
        db.execute("DELETE FROM discharge_access_tokens WHERE patient_id = ?", (patient_id,))
        db.execute("DELETE FROM discharge_summaries WHERE patient_id = ?", (patient_id,))
        db.execute(
            """INSERT OR REPLACE INTO patients
                   (id, name, mrn, age, sex, ward, bed, diagnosis, date_of_birth, status)
               VALUES (?, ?, ?, 58, 'M', 'General Medicine', 'D-4', 'Test diagnosis', ?, 'admitted')""",
            (patient_id, f"Link Address {patient_id}", mrn, dob),
        )


def _discharge(patient_id: str, qr_urls: list | None = None) -> dict:
    """The real discharge write path with a canned summary. When qr_urls is
    given, every URL handed to the QR generator is recorded in it."""
    if qr_urls is None:
        return discharge_service.discharge_patient(patient_id, "DOC001", "Test hospital course.", [])
    real = discharge_service.generate_qr_code

    def _spy(pid, url):
        qr_urls.append(url)
        return real(pid, url)

    with mock.patch.object(discharge_service, "generate_qr_code", _spy):
        return discharge_service.discharge_patient(patient_id, "DOC001", "Test hospital course.", [])


def _db_state(patient_id: str) -> dict:
    with db_session() as db:
        tokens = db.execute(
            "SELECT COUNT(*) c FROM discharge_access_tokens WHERE patient_id = ?", (patient_id,)
        ).fetchone()["c"]
        summary = db.execute(
            "SELECT qr_code_path FROM discharge_summaries WHERE patient_id = ?", (patient_id,)
        ).fetchone()
        status = db.execute("SELECT status FROM patients WHERE id = ?", (patient_id,)).fetchone()["status"]
        audit = db.execute(
            "SELECT summary_message FROM audit_logs WHERE patient_id = ? "
            "AND action_type = 'discharge_link_not_issued' ORDER BY rowid DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
    return {"tokens": tokens, "summary": summary, "status": status,
            "audit": audit["summary_message"] if audit else None}


# ── The shape check ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("base_url", [
    _DEFAULT,                          # the shipped default and .env.example value
    "",
    "   ",
    "http://orb.hospital.local:8000",  # reachable host, but the date of birth would travel in clear
    "https://localhost:8000",
    "https://LOCALHOST",
    "https://orb.localhost",
    "https://127.0.0.1:8000",
    "https://127.0.1.1",
    "https://0.0.0.0:8000",
    "https://[::1]:8000",
    "https:///discharge",
    "orb.hospital.local",
])
def test_an_address_a_phone_cannot_open_is_named_as_a_problem(base_url):
    assert public_address_problem(base_url), base_url


@pytest.mark.parametrize("base_url,phrase", [
    (_DEFAULT, "this machine"),        # the host is the fault; https://localhost would be no better
    ("", "not set"),
    ("http://orb.hospital.local:8000", "https"),
    ("orb.hospital.local", "not a web address"),
])
def test_the_problem_names_the_real_fault(base_url, phrase):
    assert phrase in public_address_problem(base_url)


@pytest.mark.parametrize("base_url", [
    _APPLIANCE,
    "https://orb.hospital.local",
    "https://10.20.0.15:8000",
    "https://patients.example-hospital.in/orb",
])
def test_an_https_network_address_is_accepted(base_url):
    assert public_address_problem(base_url) is None


def test_only_the_prod_profile_refuses():
    with _prod(_DEFAULT):
        prod = public_link_base_status()
    with _dev(_DEFAULT):
        dev = public_link_base_status()
    assert prod["available"] is False and prod["reason"] == NO_PUBLIC_ADDRESS_REASON
    assert dev["available"] is True and dev["reason"] is None
    assert dev["problem"], "dev still knows the address is local, it just does not refuse"


# ── The discharge path ───────────────────────────────────────────────────────

def test_prod_with_the_default_address_issues_no_link_and_says_why():
    _seed_patient("PAT-PUB-DEFAULT", mrn="MRN-PUB-1")
    qr_urls: list = []
    with _prod(_DEFAULT):
        result = _discharge("PAT-PUB-DEFAULT", qr_urls)

    assert result["status"] == "success", "The discharge itself must not be blocked"
    assert result["secure_link_issued"] is False
    assert not result["summary_url"], "A link to the patient's own phone was printed"
    assert not result["qr_code_url"], "A QR code to the patient's own phone was printed"
    assert qr_urls == [], "A QR image was generated for a link that cannot open"
    assert "ORB_PUBLIC_APP_BASE_URL" in result["secure_link_reason"]
    assert "date of birth" not in result["secure_link_reason"].lower(), (
        "The patient has a date of birth; the desk is sent to fix the wrong thing"
    )

    state = _db_state("PAT-PUB-DEFAULT")
    assert state["tokens"] == 0, "A token row exists for a link nobody can open"
    assert state["summary"] is not None and not state["summary"]["qr_code_path"]
    assert state["status"] == "discharged"
    assert "no patient-reachable address" in (state["audit"] or ""), state["audit"]


def test_prod_with_neither_address_nor_date_of_birth_gives_both_reasons():
    _seed_patient("PAT-PUB-BOTH", mrn="MRN-PUB-2", dob=None)
    with _prod(_DEFAULT):
        result = _discharge("PAT-PUB-BOTH")
    reason = result["secure_link_reason"]
    assert "ORB_PUBLIC_APP_BASE_URL" in reason and "date of birth" in reason.lower(), reason
    audit = _db_state("PAT-PUB-BOTH")["audit"] or ""
    assert "no patient-reachable address" in audit and "second factor" in audit, audit


def test_prod_with_a_configured_address_puts_exactly_that_in_the_link_and_qr():
    _seed_patient("PAT-PUB-SET", mrn="MRN-PUB-3")
    qr_urls: list = []
    with _prod(_APPLIANCE):
        result = _discharge("PAT-PUB-SET", qr_urls)

    assert result["secure_link_issued"] is True, result["secure_link_reason"]
    assert re.fullmatch(re.escape(_APPLIANCE) + r"/discharge/[A-Za-z0-9_-]{43}", result["summary_url"]), (
        result["summary_url"]
    )
    assert qr_urls == [result["summary_url"]], "The QR encodes something other than the link"
    assert result["qr_code_url"]
    assert _db_state("PAT-PUB-SET")["tokens"] == 1

    raw_token = result["summary_url"].rsplit("/", 1)[-1]
    verify = client.post("/api/discharge-access/verify", json={"token": raw_token, "dob": "1964-03-11"})
    assert verify.status_code == 200, f"The printed link does not open: {verify.text}"


def test_a_trailing_slash_does_not_double_up_in_the_link():
    _seed_patient("PAT-PUB-SLASH", mrn="MRN-PUB-4")
    with _prod(_APPLIANCE + "/"):
        result = _discharge("PAT-PUB-SLASH")
    assert result["summary_url"].startswith(_APPLIANCE + "/discharge/"), result["summary_url"]
    assert "//discharge" not in result["summary_url"]


def test_dev_keeps_issuing_links_on_the_vite_address():
    _seed_patient("PAT-PUB-DEV", mrn="MRN-PUB-5")
    with _dev(_DEFAULT):
        result = _discharge("PAT-PUB-DEV")
    assert result["secure_link_issued"] is True
    assert result["summary_url"].startswith(_DEFAULT + "/discharge/")


# ── The operator hears it at boot ────────────────────────────────────────────

def test_boot_logs_an_error_in_prod_while_the_address_cannot_open(caplog):
    log = logging.getLogger("orb.test.public_address")
    with caplog.at_level(logging.ERROR, logger=log.name), _prod(_DEFAULT):
        log_public_address_problem(log)
    errors = [r for r in caplog.records if r.name == log.name and r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "ORB_PUBLIC_APP_BASE_URL" in message and _DEFAULT in message
    assert "section 8.4" in message


@pytest.mark.parametrize("profile,base_url", [("prod", _APPLIANCE), ("dev", _DEFAULT)])
def test_boot_is_quiet_when_links_can_be_issued(caplog, profile, base_url):
    log = logging.getLogger("orb.test.public_address")
    with caplog.at_level(logging.DEBUG, logger=log.name), \
            mock.patch.multiple("config", DEPLOYMENT_PROFILE=profile, PUBLIC_APP_BASE_URL=base_url):
        log_public_address_problem(log)
    assert not [r for r in caplog.records if r.name == log.name]


def test_the_app_lifespan_runs_the_boot_check():
    import main
    assert "log_public_address_problem(logger)" in inspect.getsource(main.lifespan)


# ── The Trust Center ─────────────────────────────────────────────────────────

def _link_control():
    report = compliance_service.compliance_report()
    return next((c for c in report["controls"] if c["id"] == "patient_discharge_link"), None)


def test_trust_center_is_amber_in_prod_on_the_default_address():
    with _prod(_DEFAULT):
        control = _link_control()
    assert control is not None, "No Trust Center row in prod"
    assert control["status"] == "warn"
    assert _DEFAULT in control["evidence"] and "this machine" in control["evidence"]
    assert control["settings"] == ["ORB_PUBLIC_APP_BASE_URL"]
    for field in ("evidence", "remediation"):
        assert not re.search(r"\bORB_[A-Z_]+", control[field]), control[field]
        assert "\u2014" not in control[field], "em dash"


def test_trust_center_passes_in_prod_on_a_configured_address():
    with _prod(_APPLIANCE):
        control = _link_control()
    assert control["status"] == "pass"
    assert _APPLIANCE in control["evidence"]


def test_trust_center_says_unknown_when_the_probe_fails(monkeypatch):
    monkeypatch.setattr(compliance_service, "_patient_link_posture",
                        lambda: (_ for _ in ()).throw(RuntimeError("probe exploded")))
    with _prod(_APPLIANCE):
        control = _link_control()
    assert control["status"] == "unknown"


def test_trust_center_has_no_row_in_dev():
    with _dev(_DEFAULT):
        assert _link_control() is None
