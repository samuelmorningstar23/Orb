import csv
import sqlite3
import sys
from pathlib import Path

# Add server dir to path
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from services.rxnorm_corrector import metaphone_key  # noqa: E402 - application imports require the path/environment bootstrap above.

INDIAN_DRUGS = [
    {"rxcui": "IND_0001", "name": "Dolo", "tty": "BN"},
    {"rxcui": "IND_0002", "name": "Crocin", "tty": "BN"},
    {"rxcui": "IND_0003", "name": "Monocef", "tty": "BN"},
    {"rxcui": "IND_0004", "name": "Pantocid", "tty": "BN"},
    {"rxcui": "IND_0005", "name": "Augmentin", "tty": "BN"},
    {"rxcui": "IND_0006", "name": "Combiflam", "tty": "BN"},
    {"rxcui": "IND_0007", "name": "Omez", "tty": "BN"},
    {"rxcui": "IND_0008", "name": "Montek", "tty": "BN"},
    {"rxcui": "IND_0009", "name": "Calpol", "tty": "BN"},
    {"rxcui": "IND_0010", "name": "Ceftum", "tty": "BN"},
    {"rxcui": "IND_0011", "name": "Taxim", "tty": "BN"},
    {"rxcui": "IND_0012", "name": "Zifi", "tty": "BN"},
    {"rxcui": "IND_0013", "name": "Becosules", "tty": "BN"},
    {"rxcui": "IND_0014", "name": "Shelcal", "tty": "BN"},
    {"rxcui": "IND_0015", "name": "Ascoril", "tty": "BN"},
    {"rxcui": "IND_0016", "name": "Avil", "tty": "BN"},
    {"rxcui": "IND_0017", "name": "Pan-D", "tty": "BN"},
    {"rxcui": "IND_0018", "name": "Montek-LC", "tty": "BN"},
    {"rxcui": "IND_0019", "name": "Pantocid-D", "tty": "BN"},
    {"rxcui": "IND_0020", "name": "Dolo-650", "tty": "BN"},
]


def add_indian_drugs():
    csv_path = SERVER_DIR / "datasets" / "rxnorm" / "rxnorm_drugs.csv"
    db_path = SERVER_DIR / "datasets" / "rxnorm" / "rxnorm_drugs.db"

    print(f"Reading current CSV from {csv_path}...")
    existing_cuis = set()
    rows = []

    if csv_path.exists():
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                existing_cuis.add(r["rxcui"])
                rows.append(r)

    # Add new Indian drugs
    added_count = 0
    for d in INDIAN_DRUGS:
        if d["rxcui"] not in existing_cuis:
            rows.append(d)
            added_count += 1

    if added_count > 0:
        print(f"Adding {added_count} new Indian drugs to CSV...")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["rxcui", "name", "tty"])
            writer.writeheader()
            writer.writerows(rows)

    # Now rebuild the SQLite DB.
    # Stdlib sqlite3 on purpose: this is the RxNorm reference dictionary
    # (datasets/rxnorm/rxnorm_drugs.db), a rebuildable lookup table with no PHI in
    # it — NOT orb.db. It is never SQLCipher-keyed, so the codec-aware helpers in
    # database.py do not apply here and would only add a config/driver dependency.
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drugs (
            rxcui TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            tty TEXT,
            metaphone TEXT
        )
    """)

    # Insert new drugs into SQLite DB
    batch = []
    for d in INDIAN_DRUGS:
        name = d["name"]
        batch.append((d["rxcui"], name, name.lower(), d["tty"], metaphone_key(name)))

    conn.executemany(
        "INSERT OR IGNORE INTO drugs (rxcui, name, name_lower, tty, metaphone) VALUES (?, ?, ?, ?, ?)",
        batch,
    )
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM drugs").fetchone()[0]
    print(f"Database contains {count} drugs after update.")
    conn.close()
    print("Indian drugs database expansion complete!")


if __name__ == "__main__":
    add_indian_drugs()
