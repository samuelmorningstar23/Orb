"""A shared accounting export never leaks another patient's journal entries."""
import json
import uuid
from database import db_session
from services.consent_service import data_principal_export


def test_shared_journal_is_split_by_patient_for_access_export():
    tag = "LX-" + uuid.uuid4().hex
    first, second = tag + "-P1", tag + "-P2"
    legs = [{"patient_id": first, "source_number": "FIRST", "debit_paise": 100},
            {"patient_id": second, "source_number": "PRIVATE-OTHER-PATIENT", "debit_paise": 200}]
    with db_session() as db:
        for pid in (first, second):
            db.execute("INSERT INTO patients(id,name,mrn) VALUES (?,?,?)", (pid,pid,pid))
        db.execute("""INSERT INTO finance_journal_exports
            (id,facility_id,from_day,to_day,rows_json,mappings_json,debit_paise,credit_paise,content_sha256,created_by,created_at,idempotency_key,request_hash)
            VALUES (?,1,'2026-10-01','2026-10-11',?,'{}',300,300,'test','D001','2026-10-11',?,'test')""", (tag,json.dumps(legs),tag))
        for pid in (first, second):
            db.execute("INSERT INTO finance_journal_export_patients VALUES (?,?,'2026-10-11')", (tag,pid))
    result = data_principal_export(first, include_access_history=False)
    entries = result["sections"]["finance_journal_entries"]["rows"]
    assert len(entries) == 1 and entries[0]["source_number"] == "FIRST"
    assert "PRIVATE-OTHER-PATIENT" not in json.dumps(result)
