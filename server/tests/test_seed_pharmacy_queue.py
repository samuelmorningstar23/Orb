"""scripts/reset_db_and_seed.py: the pharmacy work a fresh seed leaves behind.

A fresh seed left the Helix order board and the pharmacist's Verify Queue empty,
so neither screen could show what it exists for. seed_pharmacy_queue now seeds
two medications the way an agentic Sage order arrives (charted as unverified AI
entries with no invented dose, with a pending Helix order each) and three ward
orders at later pharmacy steps.

Pinned here: the AI medications reach the Verify Queue with the dose left for a
clinician, the Helix board holds orders at several statuses, every order passes
the drug-safety gate (an order the gate stops is skipped, never forced), and a
second run adds nothing. Runs in a subprocess against its own database because
the seeder writes to every named patient it finds.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCRIPT = """
import json
from datetime import datetime, timedelta, timezone
import database
database.init_db()
from database import db_session
from scripts.seed_helix import seed_helix
seed_helix()

admit = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
with db_session() as db:
    for uid, role in (("DOC001", "doctor"), ("DOC002", "doctor"), ("PHA001", "pharmacist")):
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, ?, NULL, 'x')", (uid, "User " + uid, role))
    for i, name in enumerate(("Vikram Singh", "Rajesh Iyer", "Kavitha Menon",
                              "Ananya Kapoor", "Lakshmi Devi")):
        pid = "PAT-PQ-%d" % i
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, attending_doctor_id, "
            "diagnosis, admission_date, status, created_at) "
            "VALUES (?, ?, ?, 60, 'Male', 'GM-1', ?, 'DOC001', 'x', ?, 'admitted', ?)",
            (pid, name, "MRN-" + pid, "GM-10%d" % i, admit, admit))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
            "attending_doctor_id, status, created_at) "
            "VALUES (?, ?, 'DOC001', 'General Medicine', 'GM-1', ?, 'DOC001', 'active', ?)",
            ("ADM-" + pid, pid, "GM-10%d" % i, admit))
    # A documented severe reaction to the drug one AI order would chart.
    db.execute(
        "INSERT INTO patient_allergies (patient_id, substance, substance_class, reaction, "
        "severity, status, source, recorded_by) "
        "VALUES ('PAT-PQ-1', 'Paracetamol', NULL, 'Anaphylaxis', 'severe', 'active', 'seed', 'DOC001')")

from scripts.reset_db_and_seed import seed_pharmacy_queue
seed_pharmacy_queue()
seed_pharmacy_queue()

from services.pharmacy_verification_service import list_pending_verification
with db_session() as db:
    orders = [dict(r) for r in db.execute(
        "SELECT o.patient_id, o.status, m.name FROM helix_patient_med_orders o "
        "JOIN helix_medication_master m ON m.id = o.medication_id ORDER BY o.patient_id").fetchall()]
print("RESULT" + json.dumps({
    "orders": orders,
    "verify_queue": [
        {"patient_id": m["patient_id"], "drug": m.get("drug_name"), "dosage": m.get("dose"),
         "source": m.get("source")}
        for m in list_pending_verification()
    ],
}))
"""


def _run(db_path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ORB_DEPLOYMENT_PROFILE"}
    env["ORB_DB_PATH"] = str(db_path)
    env["ORB_DEPLOYMENT_PROFILE"] = "dev"
    env["PYTHONPATH"] = SERVER_DIR
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT], capture_output=True, text=True,
        env=env, cwd=SERVER_DIR, timeout=300,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT"):
            return json.loads(line[len("RESULT"):])
    raise AssertionError(f"no result\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")


def test_pharmacy_queue_seed(tmp_path):
    result = _run(tmp_path / "orb.db")
    orders, queue = result["orders"], result["verify_queue"]

    # The AI medication reaches the Verify Queue, unverified, with no dose made up.
    assert [q["patient_id"] for q in queue] == ["PAT-PQ-0"], queue
    assert queue[0]["source"] in ("llm_extracted", "agentic")
    assert queue[0]["dosage"] == "REVIEW REQUIRED"

    # The gate stopped paracetamol for the patient with a severe paracetamol
    # reaction, and the seed skipped it rather than charting it anyway.
    assert not [o for o in orders if o["patient_id"] == "PAT-PQ-1"], orders

    # The board holds orders at several steps, and the second run added nothing.
    statuses = sorted(o["status"] for o in orders)
    assert statuses == ["dispensed", "pending", "reviewing", "verified"], orders
