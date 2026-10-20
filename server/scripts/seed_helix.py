"""Seeder script to populate Helix master catalog and inventory lots."""

import sys
from pathlib import Path

# Add server dir to path
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from config import DB_PATH  # noqa: E402 - application imports require the path/environment bootstrap above.
from database import _open_raw_connection, _row_factory_for  # noqa: E402 - application imports require the path/environment bootstrap above.

DRUGS = [
    # High-Risk Medications
    ("MED-EPI", "Epinephrine 1mg/mL Injection", "Epinephrine", "Vial", "1mg/mL"),
    ("MED-HEP", "Heparin 5000 units/mL Injection", "Heparin", "Vial", "5000 units/mL"),
    ("MED-INS", "Insulin Aspart 100 units/mL Injection", "Insulin", "Vial", "100 units/mL"),
    ("MED-POT", "Potassium Chloride 20mEq Injection", "Potassium", "Ampoule", "20mEq"),
    # Antibiotics. generic_name is the molecule and must be the TRUE one: the
    # drug-safety screen reads it to find a cephalosporin behind a brand name,
    # and the stockout suggestion matches on it. This row and the atenolol row
    # below used to carry another drug's molecule on purpose, so a substitution
    # demo would fire, which made a cephalosporin look like amoxicillin to both.
    ("MED-AMOX-500", "Amoxicillin 500mg Oral Capsule", "Amoxicillin", "Capsule", "500mg"),
    ("MED-AMOX-250", "Amoxicillin 250mg Oral Capsule", "Amoxicillin", "Capsule", "250mg"),
    # The same molecule, strength and form as MED-AMOX-500 under another brand:
    # the only kind of row the stockout suggestion may offer.
    ("MED-MOX-500", "Mox 500mg Capsule", "Amoxicillin", "Capsule", "500mg"),
    ("MED-CEFADIN", "Cefadroxil 500mg Oral Capsule", "Cefadroxil", "Capsule", "500mg"),
    # Beta-blockers
    ("MED-METOPROLOL", "Metoprolol Succinate 50mg Oral Tablet", "Metoprolol", "Tablet", "50mg"),
    ("MED-ATENOLOL", "Atenolol 50mg Oral Tablet", "Atenolol", "Tablet", "50mg"),
    # Common General Medications
    ("MED-PARA", "Paracetamol 650mg Oral Tablet", "Paracetamol", "Tablet", "650mg"),
    ("MED-DOLO", "Dolo 650mg Tablet", "Paracetamol", "Tablet", "650mg"),
    ("MED-PANTO", "Pantoprazole 40mg Oral Tablet", "Pantoprazole", "Tablet", "40mg"),
    ("MED-MONOCEF", "Monocef 1g Injection", "Ceftriaxone", "Vial", "1g"),
    ("MED-AUGMENTIN", "Augmentin 625 DUO Tablet", "Amoxicillin/Clavulanate", "Tablet", "625mg"),
]

INVENTORY = [
    # High-Risk
    ("LOT-EPI-1", "MED-EPI", "LOTEPI101", 15, "2029-12-31"),
    ("LOT-HEP-1", "MED-HEP", "LOTHEP202", 20, "2029-12-31"),
    ("LOT-INS-1", "MED-INS", "LOTINS303", 25, "2029-12-31"),
    ("LOT-POT-1", "MED-POT", "LOTPOT404", 0, "2029-12-31"),  # Out of stock!
    # Antibiotics
    (
        "LOT-AMOX-500",
        "MED-AMOX-500",
        "LOTAMX500",
        0,
        "2029-06-30",
    ),  # Out of stock: Mox 500 is suggested
    ("LOT-AMOX-250", "MED-AMOX-250", "LOTAMX250", 0, "2029-06-30"),  # Out of stock, no equivalent
    ("LOT-MOX-500", "MED-MOX-500", "LOTMOX500", 40, "2029-12-31"),
    ("LOT-CEFADIN", "MED-CEFADIN", "LOTCEF500", 40, "2029-12-31"),
    # Beta-blockers
    (
        "LOT-METOP",
        "MED-METOPROLOL",
        "LOTMET50",
        0,
        "2028-11-30",
    ),  # Out of stock, no equivalent: the prescriber decides
    ("LOT-ATEN", "MED-ATENOLOL", "LOTATN50", 50, "2029-12-31"),
    # Common
    ("LOT-PARA-1", "MED-PARA", "LOTPAR123", 150, "2029-10-31"),
    ("LOT-DOLO-1", "MED-DOLO", "LOTDOL456", 200, "2029-10-31"),
    ("LOT-PANTO-1", "MED-PANTO", "LOTPAN789", 100, "2029-12-31"),
    ("LOT-MONOCEF-1", "MED-MONOCEF", "LOTMON999", 30, "2028-05-31"),
    ("LOT-AUGMENTIN-1", "MED-AUGMENTIN", "LOTAUG777", 60, "2029-09-30"),
]


def seed_helix():
    """Seed the formulary and its opening stock.

    THE MASTER IS UPSERTED, NOT WIPED (grn-4). A pharmacist's own entries on a
    drug (pack size, GST, schedule and NDPS class, storage, reorder levels)
    survive a reseed: only the seeded identity columns (name, generic name,
    form, strength) are rewritten. Lots and their ledger ARE replaced, and the
    new lots are opened through pharmacy_stock_service, the only writer of drug
    stock, so each lot's quantity equals its ledger from the first row and
    reconcile() is clean after a reseed. No NDPS stock is seeded (GRN-R14)."""
    print(f"═══ Connecting to DB for Helix Seeding: {DB_PATH} ═══")
    # The Helix tables are created by the server at boot, not by init_db(), so a
    # reseed of a database the server has never opened found no tables and
    # seeded no formulary. Same call main.py makes; it is additive.
    from services.helix_db import init_helix_db

    from services import pharmacy_stock_service as stock

    init_helix_db()
    # Codec-aware open: once orb.db is SQLCipher-encrypted at rest the stdlib
    # sqlite3 driver cannot read it at all ("file is not a database"), so the
    # seeder must use the app's helper — it applies PRAGMA key before any other
    # statement and degrades to plaintext sqlite3 when no key is configured.
    # _open_raw_connection() (rather than _build_connection()) keeps the raw
    # connection defaults this seeder relies on: foreign_keys stays OFF, so the
    # lot wipe below does not trip constraints from dependent tables. The row
    # class comes from the same driver, for the stock service's named reads.
    conn = _open_raw_connection()
    conn.row_factory = _row_factory_for(conn)
    cursor = conn.cursor()

    try:
        # One write transaction for the whole seed, taken before the first
        # read: the stock service writes only inside its caller's transaction.
        cursor.execute("BEGIN IMMEDIATE")
        # The ledger first, then the lots it describes. A reseed has already
        # wiped the movements (they are children of the wiped orders); a
        # standalone run must not leave rows pointing at deleted lots.
        cursor.execute("DELETE FROM helix_stock_movements")
        cursor.execute("DELETE FROM helix_inventory_lots")
        print("  Cleared old Helix inventory and its stock ledger.")

        # Seed medication catalog: upsert on id, identity columns only.
        cursor.executemany(
            """INSERT INTO helix_medication_master (id, name, generic_name, form, strength)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name = excluded.name,
                   generic_name = excluded.generic_name,
                   form = excluded.form,
                   strength = excluded.strength""",
            DRUGS,
        )
        print(f"  Upserted {len(DRUGS)} drugs into helix_medication_master.")

        # Seed inventory lots through the one stock writer: each lot opens at
        # zero and its units arrive as an 'opening' movement.
        for lot_id, medication_id, lot_number, quantity, expiry in INVENTORY:
            stock.open_or_merge_lot(
                conn,
                lot_id=lot_id,
                medication_id=medication_id,
                lot_number=lot_number,
                expiration_date=expiry,
                units=quantity,
                actor_id="system",
                movement_type="opening",
                remarks="Opening stock written by the seed",
            )
        print(f"  Opened {len(INVENTORY)} lots through the stock ledger.")

        conn.commit()
        print("═══ Helix Seeding Complete! ═══\n")
    except Exception as e:
        conn.rollback()
        print(f"  Error seeding Helix database: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    seed_helix()
