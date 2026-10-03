"""The operations status page — what hospital IT reads when Orb misbehaves.

The founder chose NO remote support. That decision is what these tests defend:
everything wrong with this appliance has to be visible ON SITE, in words a
hospital IT person understands, without ringing the vendor and without reading
Python.

So the assertions here are unusual for a test file. They check that the answer is
CORRECT, and then they check that the answer is READABLE and SAFE:

  * every failing check tells the operator what to DO about it, not only that
    something is wrong;
  * a backup is reported as verified only when something actually verified it —
    "a file exists" is the exact lie that cost this project fourteen nightly
    backups;
  * no patient identity appears anywhere in the payload, because the status page
    is the surface most likely to be screenshotted into a WhatsApp group.
"""
import json
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from auth import hash_password  # noqa: E402
from database import get_db  # noqa: E402
from services import ops_health_service  # noqa: E402

client = TestClient(app)

# A name no seed data uses, so finding it in a payload proves a leak rather than
# a coincidence.
PHI_NAME = "Zzyzx Testpatient Confidential"
PHI_MRN = "MRN-OPS-PHI-0001"


def _ensure_user(user_id: str, role: str, department: str = "Administration"):
    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, f"{role.title()} OpsHealth", role, department, hash_password("password")),
        )
        db.commit()
    finally:
        db.close()


def _login(user_id: str) -> dict:
    resp = client.post("/api/auth/login", json={"employee_id": user_id, "password": "password"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def admitted_patient():
    """One admitted patient with an unmistakable name, removed completely after.

    TEST-ISOLATION LAW: this seeds an ADMITTED patient, so it must leave none
    behind — a later module counting admissions would otherwise measure ours.
    The rows are deleted rather than discharged because they are entirely this
    module's invention and nothing else should ever see them.
    """
    patient_id = f"OPSPHI-{uuid.uuid4().hex[:8]}"
    admission_id = f"OPSADM-{uuid.uuid4().hex[:8]}"
    _ensure_user("OPS-DOC", "doctor", "General Medicine")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO patients (id, name, mrn, bed, ward, status, age, sex) "
            "VALUES (?, ?, ?, ?, ?, 'admitted', 44, 'Female')",
            (patient_id, PHI_NAME, PHI_MRN, "GM-101", "GM-1"),
        )
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active')",
            (admission_id, patient_id, "OPS-DOC", "General Medicine", "GM-1", "GM-101"),
        )
        db.commit()
    finally:
        db.close()
    yield patient_id
    db = get_db()
    try:
        db.execute("DELETE FROM admissions WHERE id = ?", (admission_id,))
        db.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
        db.commit()
    finally:
        db.close()


# ── Shape and readability ────────────────────────────────────────────────────

def test_status_is_admin_only():
    _ensure_user("OPS-NURSE", "nurse", "General Medicine")
    assert client.get("/api/ops/status").status_code in (401, 403)
    assert client.get("/api/ops/status", headers=_login("OPS-NURSE")).status_code == 403


def test_status_shape():
    _ensure_user("OPS-ADMIN", "admin")
    body = client.get("/api/ops/status", headers=_login("OPS-ADMIN")).json()

    assert body["overall"] in ("ok", "attention", "urgent")
    assert isinstance(body["headline"], str) and body["headline"]
    keys = {c["key"] for c in body["checks"]}
    # The six questions the brief names, plus the two this appliance cannot be
    # operated without knowing.
    for expected in ("server", "database", "backups", "models",
                     "integrations", "disk", "site_config", "access"):
        assert expected in keys, f"status page has no '{expected}' check"


def test_every_check_is_written_for_a_human():
    _ensure_user("OPS-ADMIN", "admin")
    body = client.get("/api/ops/status", headers=_login("OPS-ADMIN")).json()
    for check in body["checks"]:
        assert check["status"] in ("ok", "attention", "urgent")
        assert check["title"] and not check["title"].isupper()
        # A sentence, not a field dump.
        assert check["summary"].endswith((".", "?")), \
            f"{check['key']} summary is not a sentence: {check['summary']!r}"
        if check["status"] != "ok":
            assert check.get("what_to_do"), \
                f"{check['key']} is {check['status']} but tells the operator nothing to do"


def test_no_jargon_in_summaries():
    """Words that mean nothing to a hospital IT person on a Sunday."""
    _ensure_user("OPS-ADMIN", "admin")
    body = client.get("/api/ops/status", headers=_login("OPS-ADMIN")).json()
    banned = ("asyncio", "sqlcipher", "pragma", "traceback", "stderr",
              "uvicorn", "wal", "kv cache")
    for check in body["checks"]:
        text = f"{check['summary']} {check.get('what_to_do') or ''}".lower()
        for word in banned:
            assert word not in text, f"{check['key']} uses jargon {word!r}: {text}"


# ── The safety property: no PHI, ever ────────────────────────────────────────

def test_status_carries_no_patient_identity(admitted_patient):
    _ensure_user("OPS-ADMIN", "admin")
    resp = client.get("/api/ops/status?deep=true", headers=_login("OPS-ADMIN"))
    raw = resp.text
    assert PHI_NAME not in raw
    assert PHI_MRN not in raw
    assert admitted_patient not in raw


def test_text_view_carries_no_patient_identity(admitted_patient):
    _ensure_user("OPS-ADMIN", "admin")
    resp = client.get("/api/ops/status.txt", headers=_login("OPS-ADMIN"))
    assert resp.status_code == 200
    assert PHI_NAME not in resp.text
    assert PHI_MRN not in resp.text
    # It is meant to be read in a terminal, so it must actually be text.
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Orb" in resp.text


def test_offline_snapshot_carries_no_patient_identity(admitted_patient, tmp_path):
    """The same collector runs from a shell script when the API is unreachable —
    that path must be no leakier than the HTTP one."""
    payload = ops_health_service.build_status(deep=True)
    raw = json.dumps(payload)
    assert PHI_NAME not in raw and PHI_MRN not in raw and admitted_patient not in raw


# ── Backups: "did it VERIFY?" ────────────────────────────────────────────────

def _backup_check(tmp_path, monkeypatch, *, files: dict) -> dict:
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path))
    for name, body in files.items():
        path = tmp_path / name
        path.write_bytes(body if isinstance(body, bytes) else body.encode())
    payload = ops_health_service.build_status()
    return next(c for c in payload["checks"] if c["key"] == "backups")


def test_no_backups_at_all_is_urgent(tmp_path, monkeypatch):
    check = _backup_check(tmp_path, monkeypatch, files={})
    assert check["status"] == "urgent"
    assert "no backup" in check["summary"].lower()
    assert check["what_to_do"]


def test_backup_without_a_receipt_is_not_reported_as_verified(tmp_path, monkeypatch):
    """A file on disk is not a backup. It is a file on disk.

    Fourteen consecutive nightly runs once wrote zero-byte 'backups' and pruned
    the real ones, reporting success every time. Nothing may call a snapshot
    good unless something checked it and left a record.
    """
    check = _backup_check(tmp_path, monkeypatch, files={
        "orb-backup-20260807-020000.db": b"x" * 4096,
    })
    assert check["detail"]["newest_verified"] is False
    assert check["status"] != "ok"
    assert "not been checked" in check["summary"].lower() or \
           "no record" in check["summary"].lower()


def test_backup_with_a_passing_receipt_is_verified(tmp_path, monkeypatch):
    receipt = {
        "schema": ops_health_service.VERIFICATION_SCHEMA,
        "backup_file": "orb-backup-20260807-020000.db",
        "ok": True,
        # The field scripts/lib/verify_restore.py --receipt writes, and the one
        # the page reads. The fixture used to write verified_unixtime, so the
        # page's "checked at" was never exercised and always came back empty.
        "verified_utc": "2026-08-07T02:00:41Z",
        "integrity_check": "ok",
        "size_bytes": 4096,
        "row_counts": {"patients": 12, "vitals": 900, "audit_logs": 400, "users": 9},
        "encrypted": True,
    }
    check = _backup_check(tmp_path, monkeypatch, files={
        "orb-backup-20260807-020000.db": b"x" * 4096,
        "orb-backup-20260807-020000.verified.json": json.dumps(receipt),
    })
    assert check["detail"]["newest_verified"] is True
    assert check["status"] == "ok", check
    assert "checked" in check["summary"].lower() or "verified" in check["summary"].lower()
    assert check["detail"]["verification"]["checked_at"] == "2026-08-07T02:00:41Z"


def test_backup_with_a_failing_receipt_is_urgent(tmp_path, monkeypatch):
    receipt = {
        "schema": ops_health_service.VERIFICATION_SCHEMA,
        "backup_file": "orb-backup-20260807-020000.db",
        "ok": False,
        "verified_utc": "2026-08-07T02:00:41Z",
        "integrity_check": "malformed database page 7",
        "size_bytes": 4096,
    }
    check = _backup_check(tmp_path, monkeypatch, files={
        "orb-backup-20260807-020000.db": b"x" * 4096,
        "orb-backup-20260807-020000.verified.json": json.dumps(receipt),
    })
    assert check["status"] == "urgent"
    assert check["detail"]["newest_verified"] is False


def test_stale_backup_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_HEARTBEAT_BACKUP_MAX_AGE_H", "1")
    old = tmp_path / "orb-backup-20260101-020000.db"
    old.write_bytes(b"x" * 4096)
    ancient = time.time() - 72 * 3600
    os.utime(old, (ancient, ancient))
    monkeypatch.setenv("ORB_BACKUP_DIR", str(tmp_path))
    check = next(c for c in ops_health_service.build_status()["checks"]
                 if c["key"] == "backups")
    assert check["status"] in ("attention", "urgent")
    assert "hour" in check["summary"].lower() or "day" in check["summary"].lower()


# ── Disk and model checks ────────────────────────────────────────────────────

def _volumes(monkeypatch, database: dict, backups: dict) -> dict:
    by_path = {str(ops_health_service._db_path()): database}

    def fake(path):
        return by_path.get(str(path), backups)
    monkeypatch.setattr(ops_health_service, "_volume", fake)
    return ops_health_service._check_disk()


def _vol(pct, free_gb):
    free = int(free_gb * 1024 ** 3)
    return {"free_bytes": free, "total_bytes": free * 100, "free": f"{free_gb} GB",
            "free_percent": pct}


def test_a_full_disk_is_not_read_as_an_empty_one(monkeypatch):
    """free_percent 0.0 is falsy. `or 100.0` turned a full disk into an empty
    one; on a volume over ~4 TB the 2 GB floor did not catch it."""
    check = _volumes(monkeypatch, database=_vol(0.0, 3.0), backups=_vol(55.0, 400.0))
    assert check["status"] == "urgent", check
    assert "database" in check["summary"]


def test_the_fuller_of_two_low_disks_is_the_one_reported(monkeypatch):
    check = _volumes(monkeypatch, database=_vol(8.0, 80.0), backups=_vol(0.0, 3.0))
    assert check["status"] == "urgent"
    assert "backups" in check["summary"]


def test_model_probe_names_the_models_config_resolved(monkeypatch):
    """The app runs config.LLM_MODEL as RESOLVED (an installed variant may stand
    in for the configured one) plus the thinking-off fast tier. The page must
    probe those, not the raw environment."""
    import config
    monkeypatch.setenv("ORB_LLM_MODEL", "medgemma:27b-not-installed")
    monkeypatch.delenv("ORB_AMBIENT_LLM_MODEL", raising=False)
    monkeypatch.setattr(config, "LLM_MODEL", "medgemma:27b-resolved")
    monkeypatch.setattr(config, "AMBIENT_LLM_MODEL", "medgemma:27b-resolved")
    monkeypatch.setattr(config, "THINKING_FAST_MODEL", "medgemma:4b-fast")
    monkeypatch.setattr(ops_health_service, "_model_cache", None)
    monkeypatch.setenv("ORB_OLLAMA_BASE_URL", "http://127.0.0.1:9")
    info = ops_health_service._probe_models(timeout=0.2)
    assert info["configured"] == ["medgemma:27b-resolved", "medgemma:4b-fast"]
    assert "medgemma:27b-not-installed" not in info["configured"]


def test_remedies_name_admin_tabs_that_exist():
    """Two remedies named screens that do not exist ('Admin → Integrations',
    'Admin → Models' with a Load button). The admin tab labels live in
    src/pages/panels/AdminPanel.tsx."""
    import inspect
    import re
    src = inspect.getsource(ops_health_service)
    panel = (ops_health_service._server_dir().parent / "src" / "pages" / "panels"
             / "AdminPanel.tsx").read_text(encoding="utf-8")
    labels = set(re.findall(r"label: '([^']+)'", panel))
    named = re.findall(r"Admin → ((?:[A-Z][\w&]*)(?: [A-Z][\w&]*)*)", src.replace('"\n                "', ""))
    assert named, "expected the remedies to name admin screens"
    for name in named:
        assert name in labels, f"remedy names 'Admin → {name}', which is not an admin tab"


# ── Site config check ────────────────────────────────────────────────────────

def test_site_config_check_reports_absence_without_calling_it_broken():
    """No site.toml is a supported way to run. It must read as informational."""
    check = next(c for c in ops_health_service.build_status()["checks"]
                 if c["key"] == "site_config")
    assert check["status"] in ("ok", "attention")


def test_overall_is_the_worst_check():
    payload = ops_health_service.build_status()
    ranks = {"ok": 0, "attention": 1, "urgent": 2}
    worst = max(ranks[c["status"]] for c in payload["checks"])
    assert ranks[payload["overall"]] == worst


# ── The deep read is audited ─────────────────────────────────────────────────

def test_deep_status_writes_an_audit_row():
    _ensure_user("OPS-ADMIN", "admin")
    before = _audit_count()
    client.get("/api/ops/status?deep=true", headers=_login("OPS-ADMIN"))
    assert _audit_count() > before


def _audit_count() -> int:
    db = get_db()
    try:
        return db.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action_type = 'ops_status_read'"
        ).fetchone()[0]
    finally:
        db.close()
