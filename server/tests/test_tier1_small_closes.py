"""A11 small-closes: WHO checklist, discharge med reconciliation, Lens gate,
downtime FHIR self-export, relay message provenance."""
import json
import os
import uuid

import pytest

from database import db_session


# ── WHO surgical checklist ────────────────────────────────────────────────────
def _mk_surgery():
    sid = f"surg-{uuid.uuid4().hex[:8]}"
    pid = f"pat-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
                   ("SURG-DOC", "Dr Surg", "doctor", "h"))
        db.execute("INSERT INTO patients (id, name, age, sex, mrn, diagnosis, ward, bed, status) "
                   "VALUES (?,?,?,?,?,?,?,?,'admitted')",
                   (pid, "Surg Pat", 50, "M", f"MRN{uuid.uuid4().hex[:6]}", "Appendicitis", "Surg", "S1"))
        db.execute(
            "INSERT INTO surgeries (id, patient_id, surgeon_id, procedure_name, diagnosis, "
            "scheduled_date, scheduled_time, status) VALUES (?,?,?,?,?,?,?, 'scheduled')",
            (sid, pid, "SURG-DOC", "Appendectomy", "Appendicitis", "2026-02-01", "09:00"),
        )
    return sid


def test_who_checklist_template_has_three_phases():
    from services.who_checklist import get_template
    tpl = get_template()
    phase_ids = [p["id"] for p in tpl["phases"]]
    assert phase_ids == ["sign_in", "time_out", "sign_out"]
    assert all(p["items"] for p in tpl["phases"])


def test_who_checklist_check_and_progress():
    from services.who_checklist import set_item, get_checklist_state
    sid = _mk_surgery()
    state = get_checklist_state(sid)
    assert state["progress"]["checked"] == 0
    assert not state["completed"]

    after = set_item(sid, "si_identity", True, "SURG-DOC")
    assert after["progress"]["checked"] == 1
    assert after["state"]["si_identity"]["by"] == "SURG-DOC"

    # Unchecking removes it
    after2 = set_item(sid, "si_identity", False, "SURG-DOC")
    assert after2["progress"]["checked"] == 0


def test_who_checklist_rejects_unknown_item():
    from services.who_checklist import set_item
    sid = _mk_surgery()
    with pytest.raises(ValueError):
        set_item(sid, "not_a_real_item", True, "SURG-DOC")


# ── Discharge medication reconciliation ───────────────────────────────────────
def test_medication_reconciliation_splits_and_flags_unverified():
    from services.discharge_service import build_medication_reconciliation
    pid = f"pat-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, age, sex, mrn, diagnosis, ward, bed, status) "
                   "VALUES (?,?,?,?,?,?,?,?,'admitted')",
                   (pid, "Rec Pat", 60, "F", f"MRN{uuid.uuid4().hex[:6]}", "CHF", "Med", "M2"))
        # a continued clinician med, a stopped med, and an unverified AI med
        db.execute("INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source) "
                   "VALUES (?,?,?,?,?,?,?)", (pid, "Furosemide", "40mg", "Oral", "OD", "active", "clinician"))
        db.execute("INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source) "
                   "VALUES (?,?,?,?,?,?,?)", (pid, "Aspirin", "75mg", "Oral", "OD", "discontinued", "clinician"))
        db.execute("INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, source, verified_by) "
                   "VALUES (?,?,?,?,?,?,?, NULL)", (pid, "Bisoprolol", "2.5mg", "Oral", "OD", "active", "llm_extracted"))

    rec = build_medication_reconciliation(pid)
    cont_names = {m["name"] for m in rec["continued"]}
    stop_names = {m["name"] for m in rec["stopped"]}
    assert "Furosemide" in cont_names and "Bisoprolol" in cont_names
    assert "Aspirin" in stop_names
    assert rec["counts"]["unverified"] == 1
    assert rec["unverified"][0]["name"] == "Bisoprolol"


# ── Lens demo-only gate ───────────────────────────────────────────────────────
def test_lens_gate_config_default_dev(monkeypatch):
    # In the dev/test profile LENS should be enabled; the flag exists and is bool.
    import importlib
    import config
    importlib.reload(config)
    assert isinstance(config.LENS_ENABLED, bool)


# ── Downtime FHIR self-export ─────────────────────────────────────────────────
def test_fhir_self_export_writes_bundles(tmp_path, monkeypatch):
    monkeypatch.setenv("ORB_FHIR_EXPORT_DIR", str(tmp_path))
    from services.downtime_service import run_nightly_fhir_self_export
    pid = f"pat-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, age, sex, mrn, diagnosis, ward, bed, status) "
                   "VALUES (?,?,?,?,?,?,?,?,'admitted')",
                   (pid, "FHIR Pat", 45, "M", f"MRN{uuid.uuid4().hex[:6]}", "Sepsis", "ICU", "I1"))
    result = run_nightly_fhir_self_export("test")
    assert result["count"] >= 1
    files = list(tmp_path.glob("*.fhir.json"))
    assert files, "expected at least one exported bundle file"
    bundle = json.loads(files[0].read_text())
    assert bundle["resourceType"] == "Bundle"


# ── Relay message provenance ──────────────────────────────────────────────────
def test_send_message_tags_bot_provenance():
    from services.relay_service import send_message
    cid = f"chan-{uuid.uuid4().hex[:8]}"
    with db_session() as db:
        db.execute("INSERT OR IGNORE INTO users (id, name, role, password_hash) VALUES (?,?,?,?)",
                   ("bot_test", "Bot", "doctor", "h"))
        db.execute("INSERT INTO channels (id, name, type) VALUES (?,?,'department')", (cid, "Test Chan"))
    msg = send_message(cid, "bot_test", "Bot", "intelligence", "hello", is_bot=True, source="bot")
    assert msg["is_bot"] is True
    assert msg["source"] == "bot"
