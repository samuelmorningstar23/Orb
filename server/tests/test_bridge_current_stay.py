"""R029 display isolation never changes a medicine's clinical status."""
import uuid

import pytest
from database import db_session
from services.bridge_stay import current_medications, current_summary, current_vitals


@pytest.fixture
def stay():
    pid = "BCS-" + uuid.uuid4().hex
    old, new = pid + "-old", pid + "-new"
    with db_session() as db:
        db.execute("INSERT INTO patients(id,name,mrn,ward) VALUES (?,'Current stay test',?,'General Medicine')", (pid, pid))
        for aid, status, start, end in [(old, "discharged", "2026-09-01", "2026-09-02"), (new, "active", "2026-10-10", None)]:
            db.execute("INSERT INTO admissions(id,patient_id,admitted_by,status,created_at,admitted_at,discharged_at) VALUES (?,?,'D001',?,?,?,?)", (aid, pid, status, start, start, end))
        db.execute("INSERT INTO discharge_summaries(patient_id,author_id,summary,discharged_at,admission_id) VALUES (?,'D001','Prior stay','2026-09-02',?)", (pid, old))
    return pid, old, new


def test_readmission_does_not_show_previous_summary(stay):
    pid, _, new = stay
    with db_session() as db:
        assert current_summary(db, pid) is None
        db.execute("INSERT INTO discharge_summaries(patient_id,author_id,summary,discharged_at,admission_id) VALUES (?,'D001','This stay','2026-10-11',?)", (pid, new))
        assert current_summary(db, pid)["summary"] == "This stay"


def test_legacy_medication_time_is_bounded_without_status_mutation(stay):
    pid, _, _ = stay
    with db_session() as db:
        for drug, started in [("Old active", "2026-09-01"), ("Current", "2026-10-10T00:01:00Z"), ("Unknown", "unreadable")]:
            db.execute("INSERT INTO medications(patient_id,drug_name,dosage,route,frequency,status,started_at) VALUES (?,?,'Test dose','oral','daily','active',?)", (pid, drug, started))
        assert [r["drug_name"] for r in current_medications(db, pid)] == ["Current"]
        assert db.execute("SELECT COUNT(*) FROM medications WHERE patient_id=? AND status='active'", (pid,)).fetchone()[0] == 3


def test_current_vitals_do_not_fall_back_to_previous_stay(stay):
    pid, _, _ = stay
    with db_session() as db:
        db.execute("INSERT INTO vitals(patient_id,timestamp,heart_rate) VALUES (?,'2026-09-01',150)", (pid,))
        assert current_vitals(db, pid) == []
        db.execute("INSERT INTO vitals(patient_id,timestamp,heart_rate) VALUES (?,'2026-10-10T00:01:00Z',80)", (pid,))
        db.execute("INSERT INTO vitals(patient_id,timestamp,heart_rate,voided_at) VALUES (?,'2026-10-10T00:02:00Z',160,'2026-10-10T00:03:00Z')", (pid,))
        assert current_vitals(db, pid)[0]["heart_rate"] == 80
        assert len(current_vitals(db, pid)) == 1


def test_explicit_old_order_link_wins_over_changed_projection_time(stay):
    pid, old, _ = stay
    with db_session() as db:
        mid = db.execute("INSERT INTO medications(patient_id,drug_name,dosage,frequency,status,started_at) VALUES (?,'Old linked','Test','daily','active','2026-10-10')", (pid,)).lastrowid
        db.execute("""INSERT INTO med_orders(id,patient_id,admission_id,status,source,drug_name,entered_by,
            legacy_medication_id,created_at,updated_at) VALUES (?,?,?,'prescribed','test','Old linked','D001',?,'2026-09-01','2026-10-10')""", (pid+'-order',pid,old,mid))
        assert current_medications(db,pid) == []
