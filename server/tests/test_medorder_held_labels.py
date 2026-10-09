"""A medicine on hold, as every presentation reader shows it (medorders-6b, W7-E).

A held order keeps its ward row 'active' (the interaction screen and the regimen
still see the drug) and sets medications.held_at and hold_reason
(services/medorders/core.projection_for). Pinned here, for each reader that
shows or hands on a drug list:

  * I-PASS: the entry carries on_hold and 'on hold since <ward time>: <reason>',
    and the printed sheet says so;
  * discharge reconciliation: the drug stays in 'continued' (it must not vanish)
    and is called out under its own warning, like an unverified drug;
  * the patient portal: the status is 'On hold' and nothing more, with no time,
    no reason, no verdict and no colour class of its own;
  * FHIR: 'on-hold' with the reason as statusReason; an unverified AI row stays
    a draft;
  * Sage and the ambient Relay context: the drug line says it is on hold;
  * the screening sets keep the held drug (it still interacts).
And for a drug that is not held, every reader prints exactly what it did before.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth import hash_password
from database import db_session
from main import app
from services.clinical_time import utc_iso_now
from services.medorders import core, held

client = TestClient(app)

REASON = "Nil by mouth before theatre"
_PATIENTS: list[str] = []
SRC_DIR = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    if not _PATIENTS:
        return
    marks = ", ".join("?" for _ in _PATIENTS)
    with db_session() as db:
        db.execute(f"DELETE FROM med_order_events WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"DELETE FROM med_orders WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE medications SET med_order_id = NULL, held_at = NULL, "
                   f"hold_reason = NULL WHERE patient_id IN ({marks})", _PATIENTS)
        db.execute(f"UPDATE patients SET status = 'discharged' WHERE id IN ({marks})", _PATIENTS)


def _patient(name="Held Label Patient"):
    pid = f"PHLD{uuid.uuid4().hex[:8].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO patients (id, name, mrn, status, ward, bed) "
                   "VALUES (?, ?, ?, 'admitted', 'W7E', '3')",
                   (pid, name, f"MRN-{uuid.uuid4().hex[:8]}"))
    _PATIENTS.append(pid)
    return pid


def _chart(pid, drug, *, dose="40 mg", route="IV", freq="OD", status="active",
           source="clinician", verified_by=None):
    with db_session() as db:
        return db.execute(
            "INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, "
            "prescribed_by, source, verified_by) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (pid, drug, dose, route, freq, status, source, verified_by)).lastrowid


def _held_order(pid, drug="Furosemide", **chart):
    """A charted drug, adopted into the store as an order and put on hold
    through the store's own writer, so the ward row carries the real marker."""
    med_id = _chart(pid, drug, **chart)
    with db_session(immediate=True) as conn:
        row = conn.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone()
        mapping = core.legacy_status_for(row)
        write = core.create_in_txn(
            conn, patient_id=pid, drug_name=drug, source="chart", status=mapping["status"],
            entered_by="HLDDOC", actor_id="HLDDOC", entry_point="test.held_labels",
            dose=row["dosage"], route=row["route"], frequency=row["frequency"],
            prescriber_id="HLDDOC", verified_by=mapping["verified_by"],
            verified_at=mapping["verified_at"] or (utc_iso_now() if mapping["verified_by"]
                                                   else None),
            needs_verification=bool(mapping["needs_verification"]))
        core.link_projection_in_txn(conn, write.order["id"], med_id)
        core.transition_in_txn(conn, write.order["id"], "held", expected_version=None,
                               actor_id="HLDDOC", actor_role="doctor",
                               entry_point="test.hold", reason=REASON)
    return med_id


def _ward_row(med_id):
    with db_session() as db:
        return dict(db.execute("SELECT * FROM medications WHERE id = ?", (med_id,)).fetchone())


# ── The marker and the wording ────────────────────────────────────────────────

def test_a_hold_leaves_the_row_active_and_sets_the_marker():
    pid = _patient()
    row = _ward_row(_held_order(pid))
    assert row["status"] == "active"
    assert row["held_at"] and row["hold_reason"] == REASON


def test_the_hold_phrase_names_the_ward_time_and_the_reason():
    stamp = "2026-10-09T08:50:00+00:00"
    phrase = held.hold_phrase({"held_at": stamp, "hold_reason": f"  {REASON} "})
    assert phrase.startswith("on hold since ")
    assert phrase.endswith(f": {REASON}")
    from services.hospital_time import hospital_stamp
    assert hospital_stamp(stamp) in phrase
    assert held.hold_phrase({"held_at": stamp, "hold_reason": None}) == (
        f"on hold since {hospital_stamp(stamp)}")
    assert held.hold_phrase({"held_at": "not a time", "hold_reason": "x"}) == "on hold: x"
    assert held.hold_phrase({"held_at": None, "hold_reason": "x"}) == ""
    assert not held.is_held({"held_at": None})


# ── I-PASS ────────────────────────────────────────────────────────────────────

def test_ipass_hands_a_held_drug_over_as_on_hold():
    from services.ipass_service import build_ipass
    pid = _patient()
    _held_order(pid, "Furosemide")
    _chart(pid, "Paracetamol", dose="1 g", route="Oral", freq="QDS")
    draft = build_ipass(pid, include_assessment=False)
    meds = {m["drug_name"]: m for m in draft["sections"]["action_list"]["active_medications"]}
    assert meds["Furosemide"]["on_hold"] is True
    assert meds["Furosemide"]["hold_label"].startswith("on hold since ")
    assert meds["Furosemide"]["hold_label"].endswith(REASON)
    # The screen formats the time itself (src/lib/datetime.ts), so the raw
    # values travel too.
    assert meds["Furosemide"]["held_at"] and meds["Furosemide"]["hold_reason"] == REASON
    # A drug that is not held is exactly as before: no hold keys at all.
    assert set(meds["Paracetamol"]) == {"drug_name", "dosage", "route", "frequency",
                                        "unverified"}
    line = next(ln for ln in draft["plain_text"].splitlines() if "Furosemide" in ln)
    assert f"(on hold since " in line and line.endswith(f": {REASON})")
    plain = next(ln for ln in draft["plain_text"].splitlines() if "Paracetamol" in ln)
    assert "on hold" not in plain


# ── Discharge reconciliation ──────────────────────────────────────────────────

def test_discharge_reconciliation_keeps_a_held_drug_and_calls_it_out():
    from services.discharge_service import _render_med_rec_text, build_medication_reconciliation
    pid = _patient()
    _held_order(pid, "Furosemide")
    _chart(pid, "Paracetamol", dose="1 g", route="Oral", freq="QDS")
    rec = build_medication_reconciliation(pid)
    names = {e["name"] for e in rec["continued"]}
    assert {"Furosemide", "Paracetamol"} <= names, "a held drug must not vanish"
    assert [e["name"] for e in rec["on_hold"]] == ["Furosemide"]
    assert rec["counts"]["on_hold"] == 1
    text = _render_med_rec_text(rec)
    assert "WARNING: medications ON HOLD at discharge" in text
    fur = [ln for ln in text.splitlines() if "Furosemide" in ln]
    assert fur and all("[On hold since " in ln for ln in fur)
    assert all("On hold" not in ln for ln in text.splitlines() if "Paracetamol" in ln)


def test_discharge_reconciliation_with_nothing_held_is_exactly_as_before():
    from services.discharge_service import _render_med_rec_text, build_medication_reconciliation
    pid = _patient()
    _chart(pid, "Paracetamol", dose="1 g", route="Oral", freq="QDS")
    rec = build_medication_reconciliation(pid)
    assert set(rec) == {"continued", "stopped", "unverified", "counts"}
    assert set(rec["counts"]) == {"continued", "stopped", "unverified"}
    assert set(rec["continued"][0]) == {"name", "dosage", "route", "frequency", "source"}
    assert "ON HOLD" not in _render_med_rec_text(rec)


# ── The patient portal ────────────────────────────────────────────────────────

def _portal_login(pid):
    uid = f"PHLDU{uuid.uuid4().hex[:6].upper()}"
    with db_session() as db:
        db.execute("INSERT INTO users (id, name, role, password_hash) VALUES (?, ?, 'patient', ?)",
                   (uid, f"Portal {uid}", hash_password("secretpass")))
        db.execute("UPDATE patients SET user_id = ? WHERE id = ?", (uid, pid))
    resp = client.post("/api/auth/login", json={"employee_id": uid, "password": "secretpass"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_the_portal_says_on_hold_and_nothing_more():
    pid = _patient()
    _held_order(pid, "Furosemide")
    _chart(pid, "Paracetamol", dose="1 g", route="Oral", freq="QDS")
    hdr = _portal_login(pid)
    for path, key in (("/api/bridge/medications", "medications"),
                      ("/api/bridge/reports", "medications")):
        resp = client.get(path, headers=hdr)
        assert resp.status_code == 200, (path, resp.text)
        meds = {m["drug_name"]: m for m in resp.json()[key]}
        fur, par = meds["Furosemide"], meds["Paracetamol"]
        assert fur["status"] == "On hold"
        # No time, no reason, no verdict: the same keys as every other row.
        assert set(fur) == set(par) == {"drug_name", "dosage", "route", "frequency", "status",
                                        "started_at"}
        assert REASON not in resp.text
        assert par["status"] == "active"


def test_on_hold_has_no_colour_class_on_the_portal():
    """PatientShell draws the badge as bridge__report-item-badge--{status}; the
    stylesheet colours active, prn and discontinued only. 'On hold' must reach
    none of them, nor any class of its own."""
    css = (SRC_DIR / "pages" / "PatientShell.css").read_text()
    shell = (SRC_DIR / "pages" / "PatientShell.tsx").read_text()
    assert "bridge__report-item-badge--${m.status}" in shell
    classes = f"bridge__report-item-badge--{held.PORTAL_STATUS}".split()
    for cls in classes:
        assert not re.search(rf"\.{re.escape(cls)}(?![\w-])", css), cls
    coloured = set(re.findall(r"\.bridge__report-item-badge--([\w-]+)", css))
    assert coloured == {"active", "discontinued", "prn"}


# ── FHIR ──────────────────────────────────────────────────────────────────────

def test_fhir_exports_a_held_drug_on_hold_with_its_reason():
    from services import fhir_export_service as export
    pid = _patient()
    held_id = _held_order(pid, "Furosemide")
    plain_id = _chart(pid, "Paracetamol", dose="1 g", route="Oral", freq="QDS")
    by_id = {r["id"]: r for r in export.medications_to_fhir(pid)}
    held_res = by_id[f"orb-medreq-{held_id}"]
    assert held_res["status"] == "on-hold"
    assert held_res["statusReason"] == {"text": REASON}
    assert held_res["intent"] == "order"
    plain = by_id[f"orb-medreq-{plain_id}"]
    assert plain["status"] == "active" and "statusReason" not in plain


def test_fhir_keeps_an_unverified_ai_drug_a_draft_even_when_held():
    from services import fhir_export_service as export
    pid = _patient()
    med_id = _held_order(pid, "Ceftriaxone", dose="1 g", route="IV", freq="OD",
                         source="llm_extracted")
    res = next(r for r in export.medications_to_fhir(pid) if r["id"] == f"orb-medreq-{med_id}")
    assert res["status"] == "draft" and res["intent"] == "proposal"
    assert "statusReason" not in res


# ── Sage and the ambient Relay context ────────────────────────────────────────

def test_sage_context_says_a_held_drug_is_on_hold():
    from services.sage_service import _assemble_patient_context
    pid = _patient()
    _held_order(pid, "Furosemide")
    _chart(pid, "Paracetamol", dose="1 g", route="Oral", freq="QDS")
    with db_session() as db:
        ctx = _assemble_patient_context(db, pid, None)
    lines = {ln.split(" ")[0]: ln for ln in ctx["medications"]}
    assert lines["Furosemide"].startswith("Furosemide 40 mg IV OD (on hold since ")
    assert lines["Furosemide"].endswith(f": {REASON})")
    assert lines["Paracetamol"] == "Paracetamol 1 g Oral QDS"


def test_ambient_context_says_a_held_drug_is_on_hold():
    from services.sage_ambient_service import _fetch_patient_context
    pid = _patient()
    _held_order(pid, "Furosemide")
    _chart(pid, "Paracetamol", dose="1 g", route="Oral", freq="QDS")
    meds = {m.split(" ")[0]: m for m in _fetch_patient_context(pid)["meds"]}
    assert meds["Furosemide"].startswith("Furosemide 40 mg OD (on hold since ")
    assert meds["Paracetamol"] == "Paracetamol 1 g QDS"


# ── The screening sets keep a held drug ───────────────────────────────────────

def test_the_screening_sets_keep_a_held_drug():
    """A held drug is still in the patient: the order-time interaction screen,
    the patient-specific screen and the Scribe vocabulary keep it."""
    from services import drug_safety_service, scribe_service
    from services.patient_specific_screen import _active_medications
    pid = _patient()
    _held_order(pid, "Enoxaparin", dose="40 mg", route="SC", freq="OD")
    with db_session() as db:
        assert "Enoxaparin" in {m["name"] for m in _active_medications(db, pid)}
    alerts, _ = drug_safety_service._screen_interactions(pid, "Warfarin")
    assert alerts, "a held anticoagulant still interacts with a new one"
    assert "Enoxaparin" in scribe_service.build_whisper_hints(pid)
