"""Outbound appliance heartbeat (services/heartbeat_service.py).

THE HEADLINE TEST is test_wire_bytes_contain_no_phi: it stands up a real HTTP
server, sends a real heartbeat to it, and asserts against THE BYTES THAT ARRIVED
— not against build_payload()'s return value, and not against intent. PHI that
leaks does so on the wire, so that is where the assertion belongs. The DB it runs
against is seeded first with a patient carrying a distinctive name, MRN, ward,
bed, diagnosis and phone number, so a leak has something to leak.

The other properties under test are the ones that make the guarantee structural
rather than aspirational:
  * build_payload() issues ZERO database queries (proved by making every DB entry
    point raise);
  * a field not on the key allowlist is dropped before send, in production code;
  * failure is reported as a closed enum, never as an exception string (an
    exception string is where "no such patient: P001" gets in);
  * the loop's health is read from the watchdog's status.json ON DISK, so it
    still reports when the event loop is starved;
  * it is off unless configured, refuses cleartext, and never raises on a
    transport failure.
"""
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import heartbeat_service as hb  # noqa: E402

# PHI planted in the database before the payload is built. Every one of these
# must be absent from the bytes on the wire.
PHI_NAME = "Rajeshwari Balasubramanian"
PHI_MRN = "MRN-99-ZZTOP-4471"
PHI_WARD = "Bhandari Ward Three"
PHI_BED = "B-17-QQ"
PHI_DIAGNOSIS = "Community acquired pneumonia with sepsis"
PHI_PHONE = "+919812345678"
PHI_PATIENT_ID = "PTEST-HEARTBEAT-001"


@pytest.fixture(autouse=True)
def _no_env_leaks():
    """No test in this file may change the process environment permanently.

    This file necessarily touches ORB_DB_ENCRYPTION_KEY, ORB_BACKUP_DIR and
    friends. A test that sets one of those with a raw `os.environ[...] = ...`
    (or, worse, `pop`s a key that was already there) hands every LATER test —
    and any backup or restore drill that runs in the same process — the wrong
    key or the wrong directory. That has already cost this project real files.
    monkeypatch is the suite's idiom precisely because it undoes itself; this
    fixture is the enforcement. Autouse, so it wraps every test, and its
    teardown runs after monkeypatch's undo.
    """
    def _snapshot() -> dict:
        # PYTEST_CURRENT_TEST is pytest's own per-phase bookkeeping, not ours.
        return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}

    before = _snapshot()
    yield
    after = _snapshot()
    assert after == before, (
        "test mutated the process environment without restoring it — use "
        "monkeypatch. Changed: "
        f"{sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))}"
    )


@pytest.fixture
def sealed_env(monkeypatch, tmp_path):
    """Point every path the heartbeat reads at a throwaway tree.

    Without this the test would measure the developer's real backups directory
    and the real server/ops/watchdog, which is both flaky and (for the age
    assertions) meaningless.
    """
    for name in ("ORB_HEARTBEAT_URL", "ORB_HEARTBEAT_TOKEN", "ORB_HEARTBEAT_SECRET",
                 "ORB_BACKUP_OFFSITE_DIR", "ORB_HEARTBEAT_ALLOW_INSECURE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ORB_WATCHDOG_DIR", str(tmp_path / "watchdog"))
    monkeypatch.setenv("ORB_BOOTSTRAP_DIR", str(tmp_path / "bootstrap"))
    monkeypatch.setenv("ORB_STANDBY_RECEIPT", str(tmp_path / "standby" / "last-restore.json"))
    monkeypatch.setenv("ORB_HEARTBEAT_SITE", "pilot-ward-a")
    # Ollama may or may not be running on the developer's machine; either way the
    # payload shape is the same, so point it at a closed port for determinism.
    monkeypatch.setenv("ORB_OLLAMA_BASE_URL", "http://127.0.0.1:1")
    (tmp_path / "backups").mkdir()
    return tmp_path


def _seed_phi():
    """Put a real-looking patient in the suite's test database.

    A no-PHI assertion against an empty database proves nothing.
    """
    from database import get_db
    db = get_db()
    try:
        db.execute(
            """INSERT OR REPLACE INTO patients
               (id, name, mrn, bed, ward, diagnosis, status, age, sex, phone)
               VALUES (?, ?, ?, ?, ?, ?, 'admitted', 61, 'F', ?)""",
            (PHI_PATIENT_ID, PHI_NAME, PHI_MRN, PHI_BED, PHI_WARD, PHI_DIAGNOSIS, PHI_PHONE),
        )
        db.commit()
    finally:
        db.close()


class _Capture(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's contract
        length = int(self.headers.get("Content-Length", "0"))
        _Capture.received.append({
            "body": self.rfile.read(length),
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "path": self.path,
        })
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):  # silence the stdlib access log
        pass


@pytest.fixture
def receiver():
    """A real HTTP endpoint, so the payload is asserted as it arrives."""
    _Capture.received = []
    srv = HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, _Capture
    srv.shutdown()
    srv.server_close()


# ═══════════════════════════════════════════════════════════════════════════
#  The no-PHI guarantee
# ═══════════════════════════════════════════════════════════════════════════
def test_wire_bytes_contain_no_phi(sealed_env, receiver, monkeypatch, capsys):
    """Assert against the bytes that actually left the process."""
    _seed_phi()
    srv, cap = receiver
    url = f"http://127.0.0.1:{srv.server_address[1]}/beat"
    monkeypatch.setenv("ORB_HEARTBEAT_ALLOW_INSECURE", "1")

    result = hb.send_once(url)
    assert result["sent"] is True, result
    assert len(cap.received) == 1

    raw = cap.received[0]["body"]
    text = raw.decode("utf-8")

    for secret in (PHI_NAME, PHI_MRN, PHI_WARD, PHI_BED, PHI_DIAGNOSIS,
                   PHI_PHONE, PHI_PATIENT_ID,
                   # Substrings too: a truncated leak is still a leak.
                   "Rajeshwari", "Balasubramanian", "ZZTOP", "pneumonia", "9812345678"):
        assert secret not in text, f"PHI on the wire: {secret!r}"
        assert secret.lower() not in text.lower(), f"PHI on the wire (case-folded): {secret!r}"

    payload = json.loads(text)
    # Deliberately absent: clinical counts. /metrics exposes some of these on the
    # LAN; nothing that counts patients leaves the building.
    for forbidden in ("patients", "admissions", "vitals", "alerts_total",
                      "notes", "orders", "census", "beds"):
        assert forbidden not in text, f"clinical count in the payload: {forbidden}"

    # Print the exact document for the record — this is the proof artefact.
    print("\n=== HEARTBEAT PAYLOAD AS SENT (" + str(len(raw)) + " bytes) ===")
    print(json.dumps(payload, indent=2, sort_keys=True))


def test_every_key_is_on_the_allowlist(sealed_env):
    """A key not in _ALLOWED_KEYS cannot ship, so the allowlist is the contract."""
    payload = hb.build_payload()

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k in hb._ALLOWED_KEYS, f"{path}.{k} is not on the allowlist"
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(payload)


def test_every_leaf_is_shape_constrained(sealed_env):
    """Bool / number / None everywhere, except the handful of keys explicitly
    allowed to hold a string — and those are capped at 64 chars with no spaces
    and no punctuation a name, address or free-text note needs."""
    payload = hb.build_payload()

    def walk(node, path="", key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}", k)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]", key)
        else:
            assert hb._leaf_ok(node, key), f"{path} = {node!r} is not leaf-shaped"

    walk(payload)


def test_a_string_under_a_numeric_key_is_refused():
    """The per-key type contract, stated on its own.

    An MRN is letters, digits and hyphens — it sails through the character
    class. Only 'this key holds numbers' stops an identifier-shaped string
    appearing where a byte count belongs.
    """
    assert hb._leaf_ok(PHI_MRN, "size_bytes") is False    # numeric key
    assert hb._leaf_ok(PHI_MRN, "healthy") is False       # boolean key
    assert hb._leaf_ok(PHI_MRN, "count") is False         # numeric key
    assert hb._leaf_ok(PHI_MRN, "newest_age_s") is False
    assert hb._leaf_ok(PHI_MRN, "site") is True           # sanitised at source
    assert hb._leaf_ok(1234, "count") is True
    # Closed vocabularies are enforced, so an attacker-chosen "alert" cannot ride
    # out inside the alerts list.
    assert hb._leaf_ok("backup_stale", "alerts") is True
    assert hb._leaf_ok("patient-P001-deteriorating", "alerts") is False


def test_payload_build_makes_zero_database_queries(sealed_env, monkeypatch):
    """The structural guarantee: there is no code path from a PHI row into this
    payload, because there is no database read at all.

    Every database entry point is made to explode. If build_payload() still
    succeeds, nothing it emits came from the database.
    """
    import database

    def _boom(*args, **kwargs):
        raise AssertionError("build_payload() must not touch the database")

    monkeypatch.setattr(database, "get_db", _boom)
    monkeypatch.setattr(database, "db_session", _boom)
    monkeypatch.setattr(database, "run_db", _boom)

    payload = hb.build_payload()
    assert payload["schema"] == hb.SCHEMA


def test_guard_drops_an_unlisted_field_carrying_phi(sealed_env, monkeypatch, caplog):
    """A future edit that adds a leaky field must lose the field, not leak it.

    The guard runs in production code, not only here — this test asserts the
    production behaviour.
    """
    real = hb._collect_backup

    def leaky(errors):
        out = real(errors)
        out["patient_name"] = PHI_NAME          # key not on the allowlist
        out["count"] = PHI_MRN                  # allowed key, disallowed shape
        return out

    monkeypatch.setattr(hb, "_collect_backup", leaky)
    payload = hb.build_payload()
    text = json.dumps(payload)

    assert PHI_NAME not in text
    assert PHI_MRN not in text
    assert "patient_name" not in payload["backup"]
    assert payload["backup"]["count"] is None
    assert "payload_guard_tripped" in payload["errors"], \
        "a dropped field must be visible as an error code — silent loss is its own bug"


def test_errors_are_a_closed_enum_never_exception_text(sealed_env, monkeypatch):
    """An exception string embeds its inputs — 'no such patient: P001',
    'UNIQUE constraint failed: patients.mrn = ...'. Forwarding one would be a PHI
    leak dressed as diagnostics."""
    def explode(path, *a, **k):
        raise OSError(f"cannot stat /var/orb/{PHI_MRN}: permission denied")

    monkeypatch.setattr(hb.shutil, "disk_usage", explode)
    payload = hb.build_payload()
    assert PHI_MRN not in json.dumps(payload)
    assert "disk_unreadable" in payload["errors"]
    for code in payload["errors"]:
        assert code in hb._ERROR_CODES


def test_watchdog_last_error_is_reduced_to_a_boolean(sealed_env, tmp_path):
    """loop_watchdog's last_error is a repr() of an arbitrary exception. It is
    actionable as a flag and dangerous as text."""
    wd = tmp_path / "watchdog"
    wd.mkdir()
    (wd / "status.json").write_text(json.dumps({
        "enabled": True, "healthy": True, "stall_active": False,
        "last_error": f"RuntimeError('failed for patient {PHI_MRN}')",
    }))
    payload = hb.build_payload()
    assert PHI_MRN not in json.dumps(payload)
    assert payload["loop"]["watchdog_error"] is True


def test_site_slug_cannot_carry_text():
    """The one operator-supplied string. It comes from the environment, never
    from the database, and is reduced to a slug regardless."""
    # Spaces and punctuation collapse to hyphens, and the whole thing is capped
    # at 32 characters — so even a pasted patient identifier arrives mangled.
    assert hb.sanitise_site("Rajeshwari Balasubramanian, MRN 4471") == \
        "rajeshwari-balasubramanian-mrn-4"
    assert " " not in hb.sanitise_site("a b c")
    assert len(hb.sanitise_site("x" * 200)) <= 32
    assert hb.sanitise_site("") == ""


def test_appliance_id_is_pseudonymous(sealed_env):
    """Stable per box, and reveals neither the hostname nor the path."""
    import socket as _socket
    ident = hb.appliance_id()
    assert len(ident) == 16 and all(c in "0123456789abcdef" for c in ident)
    assert _socket.gethostname() not in ident
    assert ident == hb.appliance_id()


# ═══════════════════════════════════════════════════════════════════════════
#  What it measures
# ═══════════════════════════════════════════════════════════════════════════
def test_backup_age_is_measured_and_staleness_alerts(sealed_env, monkeypatch):
    """The field this heartbeat exists for. A silently stale backup has already
    bitten this project once."""
    backups = sealed_env / "backups"
    old = backups / "orb-backup-20260101-020000.db"
    old.write_bytes(b"x" * 2048)
    two_days = time.time() - 48 * 3600
    os.utime(old, (two_days, two_days))

    payload = hb.build_payload()
    assert payload["backup"]["count"] == 1
    assert payload["backup"]["newest_age_s"] >= 47 * 3600
    assert payload["backup"]["stale"] is True
    assert "backup_stale" in payload["alerts"]

    now = time.time()
    os.utime(old, (now, now))
    payload = hb.build_payload()
    assert payload["backup"]["stale"] is False
    assert "backup_stale" not in payload["alerts"]


def test_zero_byte_backup_is_visible(sealed_env):
    """The historical failure wrote a technically-present zero-byte file that
    passed integrity_check. Size of the newest is reported so it cannot hide
    behind a fresh mtime."""
    (sealed_env / "backups" / "orb-backup-20260801-020000.db").write_bytes(b"")
    payload = hb.build_payload()
    assert payload["backup"]["newest_size_bytes"] == 0


def test_no_backups_at_all_is_critical(sealed_env):
    payload = hb.build_payload()
    assert "backup_missing" in payload["alerts"]
    assert payload["severity"] == "critical"


def test_disk_headroom_is_reported_and_alerts(sealed_env, monkeypatch):
    payload = hb.build_payload()
    for vol in ("db_volume", "backup_volume"):
        assert payload["disk"][vol]["free_bytes"] is not None
        assert 0 <= payload["disk"][vol]["free_pct"] <= 100

    class _Tight:
        total, used, free = 1000, 985, 15  # 1.5% free

    monkeypatch.setattr(hb.shutil, "disk_usage", lambda p: _Tight)
    payload = hb.build_payload()
    assert "disk_low" in payload["alerts"]


def test_loop_health_comes_from_disk_not_http(sealed_env, tmp_path):
    """The design decision that makes this survive the failure it reports: the
    watchdog's status.json is written by a DAEMON THREAD, so it keeps moving
    while the event loop is dead. No HTTP call to /api/health is made — this test
    runs with no server at all."""
    wd = tmp_path / "watchdog"
    wd.mkdir()
    (wd / "status.json").write_text(json.dumps({
        "enabled": True, "healthy": False, "stall_active": True,
        "stalls_total": 3, "max_stall_seconds": 92.5, "beat_age_seconds": 61.2,
        "deadman_fired": True, "deadman_armed": 1,
    }))
    payload = hb.build_payload()
    assert payload["loop"]["stall_active"] is True
    assert payload["loop"]["stalls_total"] == 3
    assert payload["loop"]["deadman_fired"] is True
    assert "loop_stalled" in payload["alerts"]
    assert "deadman_fired" in payload["alerts"]
    assert payload["severity"] == "critical"


def test_stale_watchdog_status_is_itself_an_alarm(sealed_env, tmp_path):
    """When even the watchdog's daemon thread stops writing, the file's age is
    the signal — a process wedged below the level Python can see."""
    wd = tmp_path / "watchdog"
    wd.mkdir()
    status = wd / "status.json"
    status.write_text(json.dumps({"enabled": True, "healthy": True}))
    old = time.time() - 3600
    os.utime(status, (old, old))
    payload = hb.build_payload()
    assert payload["loop"]["status_age_s"] >= 3500
    assert "loop_status_stale" in payload["alerts"]


def test_access_posture_is_counts_only(sealed_env, tmp_path):
    """Which accounts hold a guessable password is an attack map. 'One account'
    is enough to start the phone call."""
    bd = tmp_path / "bootstrap"
    bd.mkdir()
    (bd / "status.json").write_text(json.dumps({
        "state": "ok", "admins_active": 0, "admins_with_mfa": 0,
        "default_credential_accounts": ["ADM001", "DOC001"],
        "scan_budget_exhausted": False,
    }))
    payload = hb.build_payload()
    assert payload["access"]["default_credential_accounts"] == 2
    assert "ADM001" not in json.dumps(payload)
    assert "no_admin" in payload["alerts"]
    assert "default_credentials" in payload["alerts"]
    assert payload["severity"] == "critical"


def test_admin_without_mfa_warns_but_is_not_critical(sealed_env, tmp_path, monkeypatch):
    bd = tmp_path / "bootstrap"
    bd.mkdir()
    (bd / "status.json").write_text(json.dumps({
        "state": "ok", "admins_active": 2, "admins_with_mfa": 0,
        "default_credential_accounts": [], "scan_budget_exhausted": False,
    }))
    # Give it a fresh backup and an encrypted DB so nothing else escalates it.
    (sealed_env / "backups" / "orb-backup-x.db").write_bytes(b"x" * 2048)
    # monkeypatch, never raw os.environ: this key is REAL on the appliance, and a
    # test that pops it leaves the rest of the session (and any backup or restore
    # drill that runs in-process) looking at an unencrypted database. monkeypatch
    # restores the prior value — including "it was set to the production key".
    monkeypatch.setenv("ORB_DB_ENCRYPTION_KEY", "test-key")
    payload = hb.build_payload()
    assert "admin_without_mfa" in payload["alerts"]
    assert payload["severity"] == "warn"


def test_standby_receipt_is_read_when_present(sealed_env, tmp_path):
    receipt = tmp_path / "standby" / "last-restore.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"ok": False, "finished_unixtime": time.time() - 7200}))
    payload = hb.build_payload()
    assert payload["standby"]["receipt_present"] is True
    assert payload["standby"]["last_restore_ok"] is False
    assert payload["standby"]["last_restore_age_s"] >= 7100
    assert "standby_stale" in payload["alerts"]


def test_ollama_unreachable_is_an_error_code_not_a_crash(sealed_env):
    payload = hb.build_payload()
    assert "ollama_unreachable" in payload["errors"]
    assert payload["models"]["ollama_reachable"] is False


# ═══════════════════════════════════════════════════════════════════════════
#  Transport
# ═══════════════════════════════════════════════════════════════════════════
def test_off_by_default(sealed_env):
    """No URL configured is a normal, successful outcome. No appliance ever calls
    home by accident."""
    result = hb.send_once()
    assert result == {"sent": False, "reason": "disabled", "severity": result["severity"]}
    assert hb.start_heartbeat() is None


def test_refuses_cleartext_unless_explicitly_allowed(sealed_env, monkeypatch):
    """The payload has no PHI, but it describes exactly how to attack this
    appliance. In cleartext on a hospital LAN that is a gift."""
    monkeypatch.delenv("ORB_HEARTBEAT_ALLOW_INSECURE", raising=False)
    result = hb.send_once("http://example.invalid/beat")
    assert result["sent"] is False and result["reason"] == "insecure_url"


def test_transport_failure_is_silent_and_never_raises(sealed_env, monkeypatch):
    """Never blocks and never breaks a clinical path — including when the
    hospital's internet is down, which it will be."""
    monkeypatch.setenv("ORB_HEARTBEAT_ALLOW_INSECURE", "1")
    # Port 1 on loopback: connection refused, fast and deterministic.
    result = hb.send_once("http://127.0.0.1:1/beat")
    assert result["sent"] is False and result["reason"] == "transport"


def test_http_error_status_is_reported_not_raised(sealed_env, monkeypatch, receiver):
    class _Reject(_Capture):
        def do_POST(self):  # noqa: N802
            self.send_response(503)
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), _Reject)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("ORB_HEARTBEAT_ALLOW_INSECURE", "1")
        result = hb.send_once(f"http://127.0.0.1:{srv.server_address[1]}/beat")
        assert result["sent"] is False and result["reason"] == "http_503"
    finally:
        srv.shutdown()
        srv.server_close()


def test_bearer_token_and_hmac_signature_are_sent(sealed_env, receiver, monkeypatch):
    """The signature lets the receiver reject a forged 'everything is fine' beat
    — for a dead-man's switch, the one message an attacker would want to send on
    the appliance's behalf."""
    import hashlib
    import hmac as _hmac

    srv, cap = receiver
    monkeypatch.setenv("ORB_HEARTBEAT_ALLOW_INSECURE", "1")
    monkeypatch.setenv("ORB_HEARTBEAT_TOKEN", "tok-abc")
    monkeypatch.setenv("ORB_HEARTBEAT_SECRET", "s3cr3t")

    assert hb.send_once(f"http://127.0.0.1:{srv.server_address[1]}/beat")["sent"] is True
    sent = cap.received[-1]
    assert sent["headers"]["authorization"] == "Bearer tok-abc"
    expected = _hmac.new(b"s3cr3t", sent["body"], hashlib.sha256).hexdigest()
    assert sent["headers"]["x-orb-signature"] == f"sha256={expected}"
    # The secret itself must never be in the body.
    assert b"s3cr3t" not in sent["body"] and b"tok-abc" not in sent["body"]


def test_thread_starts_and_stops_cleanly(sealed_env, monkeypatch, receiver):
    """It runs on a daemon thread, not the event loop — that is what lets it
    keep beating through a starved loop."""
    srv, _ = receiver
    monkeypatch.setenv("ORB_HEARTBEAT_ALLOW_INSECURE", "1")
    monkeypatch.setenv("ORB_HEARTBEAT_URL", f"http://127.0.0.1:{srv.server_address[1]}/beat")
    thread = hb.start_heartbeat()
    try:
        assert thread is not None and thread.daemon and thread.is_alive()
        assert hb.heartbeat_status()["enabled"] is True
    finally:
        hb.stop_heartbeat()
    assert hb.heartbeat_status() == {"enabled": False}


def test_standalone_print_mode_emits_the_same_document(sealed_env, capsys, monkeypatch):
    """`python -m services.heartbeat_service --print` is what an operator runs to
    see exactly what leaves the box. It must be the same document."""
    # _main() does os.environ.setdefault("ORB_HEARTBEAT_STANDALONE", "1"), which
    # is correct for a real CLI process and a leak when we call it in-process.
    # Pre-setting it through monkeypatch makes that setdefault a no-op (identical
    # behaviour, standalone mode still exercised) and gets the value restored.
    monkeypatch.setenv("ORB_HEARTBEAT_STANDALONE", "1")
    assert hb._main(["--print"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["schema"] == hb.SCHEMA
    assert set(printed) <= hb._ALLOWED_KEYS
