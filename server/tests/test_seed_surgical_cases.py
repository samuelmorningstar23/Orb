"""scripts/reset_db_and_seed.py: seeded operations belong to surgical patients.

The clinical-history backfill used to hand five canned procedures to whichever
five admitted patients the query returned first, and pick each surgeon
round-robin from every account with role 'doctor'. On the 2026-09-07 seed the
Surgical Suite showed a laparoscopic cholecystectomy for diabetic ketoacidosis
operated by the Vigil bot, an open appendectomy for COPD operated by the Lab
bot, and an ORIF femur for dengue under a load-test login.

Pinned here: a case is seeded only for a patient whose own diagnosis calls for
it, its diagnosis is that patient's diagnosis, and its surgeon is that
patient's attending surgeon. The backfill runs in a subprocess against its own
database, as tests/test_seed_clinical_history.py does, because it writes to
every admitted patient it can see.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from scripts.reset_db_and_seed import SURGICAL_CASES, plan_surgical_cases

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pt(name, attending="DOC004", dx="x"):
    return {"id": "P-" + name.split()[0], "name": name, "attending_doctor_id": attending,
            "diagnosis": dx}


def test_cases_go_only_to_the_patients_the_table_names():
    pts = [_pt("Ananya Kapoor", "DOC001"), _pt("Arun Krishnan"), _pt("Siddharth Nair", "DOC005")]
    planned = [(p["name"], proc) for p, proc, _, _ in plan_surgical_cases(pts)]
    assert planned == [
        ("Arun Krishnan", SURGICAL_CASES["Arun Krishnan"][0]),
        ("Siddharth Nair", SURGICAL_CASES["Siddharth Nair"][0]),
    ]


def test_a_patient_with_no_attending_gets_no_case():
    assert plan_surgical_cases([_pt("Arun Krishnan", attending=None)]) == []


_SCRIPT = """
import json
from datetime import datetime, timedelta, timezone
import database
database.init_db()
from database import db_session

now = datetime.now(timezone.utc)
admit = (now - timedelta(hours=30)).isoformat()
with db_session() as db:
    for uid, name, dept in (
        ("DOC001", "Dr. Meera Sharma", "General Medicine"),
        ("DOC004", "Dr. Vikram Rathore", "General Surgery"),
        ("DOC005", "Dr. Sanjay Rao", "Orthopedics"),
        ("bot_vigil", "Orb Vigil", None),
    ):
        db.execute(
            "INSERT OR REPLACE INTO users (id, name, role, department, password_hash) "
            "VALUES (?, ?, 'doctor', ?, 'x')", (uid, name, dept))
    for pid, name, doc, dept, ward, dx, pmh in (
        ("PAT-SC-1", "Ananya Kapoor", "DOC001", "General Medicine", "GM-1",
         "Diabetic ketoacidosis", ["Type 1 DM"]),
        ("PAT-SC-2", "Arun Krishnan", "DOC004", "General Surgery", "SW-1",
         "Penetrating abdominal trauma, post-ex-lap", []),
        ("PAT-SC-3", "Siddharth Nair", "DOC005", "Orthopedics", "OW-1",
         "Open fracture tibia Gustilo IIIA", ["Smoker"]),
    ):
        db.execute(
            "INSERT INTO patients (id, name, mrn, age, sex, ward, bed, attending_doctor_id, "
            "diagnosis, admission_date, status, created_at) "
            "VALUES (?, ?, ?, 40, 'Male', ?, ?, ?, ?, ?, 'admitted', ?)",
            (pid, name, "MRN-" + pid, ward, ward + "-1", doc, dx, admit, admit))
        db.execute(
            "INSERT INTO admissions (id, patient_id, admitted_by, department, ward, bed, "
            "attending_doctor_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            ("ADM-" + pid, pid, doc, dept, ward, ward + "-1", doc, admit))
        db.execute(
            "INSERT INTO patient_profiles (patient_id, comorbidities, allergies) VALUES (?, ?, '[]')",
            (pid, json.dumps(pmh)))

from scripts.reset_db_and_seed import seed_clinical_history
seed_clinical_history()

with db_session() as db:
    rows = [dict(r) for r in db.execute(
        "SELECT s.patient_id, s.surgeon_id, s.procedure_name, s.diagnosis, s.co_morbidities, "
        "p.diagnosis AS patient_dx, p.attending_doctor_id FROM surgeries s "
        "JOIN patients p ON p.id = s.patient_id ORDER BY s.patient_id").fetchall()]
print("RESULT" + json.dumps(rows))
"""


def test_the_backfill_seeds_cases_on_surgical_patients_under_their_own_surgeon(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "ORB_DEPLOYMENT_PROFILE"}
    env["ORB_DB_PATH"] = str(tmp_path / "orb.db")
    env["ORB_DEPLOYMENT_PROFILE"] = "dev"
    env["PYTHONPATH"] = SERVER_DIR
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT], capture_output=True, text=True,
        env=env, cwd=SERVER_DIR, timeout=300,
    )
    rows = next(
        (json.loads(line[len("RESULT"):]) for line in proc.stdout.splitlines()
         if line.startswith("RESULT")),
        None,
    )
    assert rows is not None, f"no result\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

    assert [r["patient_id"] for r in rows] == ["PAT-SC-2", "PAT-SC-3"], rows
    for r in rows:
        assert r["surgeon_id"] == r["attending_doctor_id"], r
        assert r["surgeon_id"] != "bot_vigil", r
        assert r["diagnosis"] == r["patient_dx"], r
    assert json.loads(rows[1]["co_morbidities"]) == ["Smoker"]
