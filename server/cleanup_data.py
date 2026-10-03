"""Clean up clinical_documents and lens_analyses to keep max 2-5 per day per patient."""
import sys
from datetime import datetime
from pathlib import Path

# Ensure server root is importable when run as a bare script from anywhere.
SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from database import _open_raw_connection, _row_factory_for


def cleanup():
    # Go through the app's codec-aware helper instead of sqlite3.connect(): orb.db
    # may be SQLCipher-encrypted at rest, and the stdlib driver cannot open such a
    # file ("file is not a database"). The helper also honours ORB_DB_PATH, which
    # the previous hard-coded <server>/orb.db path did not.
    db = _open_raw_connection()
    # Row class must come from whichever driver opened the connection — stdlib
    # sqlite3.Row raises TypeError on a SQLCipher cursor.
    db.row_factory = _row_factory_for(db)

    # --- Clinical Notes / Documents ---
    # Get all clinical documents grouped by patient+day
    docs = db.execute("""
        SELECT id, patient_id, doc_type, created_at 
        FROM clinical_documents 
        ORDER BY patient_id, created_at
    """).fetchall()

    # Group by (patient_id, date_str)
    groups: dict[str, list] = {}
    for d in docs:
        day = datetime.fromisoformat(d["created_at"]).strftime("%Y-%m-%d") if d["created_at"] else "unknown"
        key = f"{d['patient_id']}_{day}"
        groups.setdefault(key, []).append(d)

    delete_doc_ids = []
    for key, items in groups.items():
        # Keep only the first 3 per patient per day (target 2-5 range)
        if len(items) > 3:
            for excess in items[3:]:
                delete_doc_ids.append(excess["id"])

    # Also enforce a global max of 20 total
    remaining = db.execute("SELECT id FROM clinical_documents ORDER BY created_at DESC").fetchall()
    remaining_ids = [r["id"] for r in remaining if r["id"] not in delete_doc_ids]
    if len(remaining_ids) > 20:
        for excess_id in remaining_ids[20:]:
            delete_doc_ids.append(excess_id)

    if delete_doc_ids:
        placeholders = ",".join("?" * len(delete_doc_ids))
        db.execute(f"DELETE FROM clinical_documents WHERE id IN ({placeholders})", delete_doc_ids)
        print(f"  Deleted {len(delete_doc_ids)} excess clinical documents")
    else:
        print("  Clinical documents already within limits")

    # --- Lens Analyses (Diagnostic Reports) ---
    reports = db.execute("""
        SELECT id, patient_id, created_at 
        FROM lens_analyses 
        ORDER BY patient_id, created_at
    """).fetchall()

    r_groups: dict[str, list] = {}
    for r in reports:
        day = datetime.fromisoformat(r["created_at"]).strftime("%Y-%m-%d") if r["created_at"] else "unknown"
        key = f"{r['patient_id']}_{day}"
        r_groups.setdefault(key, []).append(r)

    delete_report_ids = []
    for key, items in r_groups.items():
        if len(items) > 3:
            for excess in items[3:]:
                delete_report_ids.append(excess["id"])

    # Global max 20 reports
    remaining_r = db.execute("SELECT id FROM lens_analyses ORDER BY created_at DESC").fetchall()
    remaining_r_ids = [r["id"] for r in remaining_r if r["id"] not in delete_report_ids]
    if len(remaining_r_ids) > 20:
        for excess_id in remaining_r_ids[20:]:
            delete_report_ids.append(excess_id)

    if delete_report_ids:
        placeholders = ",".join("?" * len(delete_report_ids))
        db.execute(f"DELETE FROM lens_analyses WHERE id IN ({placeholders})", delete_report_ids)
        print(f"  Deleted {len(delete_report_ids)} excess diagnostic reports")
    else:
        print("  Diagnostic reports already within limits")

    db.commit()

    # Print final counts
    doc_count = db.execute("SELECT COUNT(*) FROM clinical_documents").fetchone()[0]
    report_count = db.execute("SELECT COUNT(*) FROM lens_analyses").fetchone()[0]
    print(f"\n  Final counts:")
    print(f"    Clinical documents: {doc_count}")
    print(f"    Diagnostic reports: {report_count}")

    db.close()

if __name__ == "__main__":
    print("═══ Cleaning up notes/reports ═══")
    cleanup()
    print("═══ Done ═══")
