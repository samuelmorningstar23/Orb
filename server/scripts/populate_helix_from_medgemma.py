"""Script to populate Helix with medications for the 30 patients using MedGemma, ensuring sync with Vigil."""
import os
import sys
import json
import urllib.request
import uuid
import random
from datetime import datetime, timezone

# Add parent directory to path to allow importing from server modules if needed
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# The DB location and the driver both come from the app: orb.db may be encrypted at
# rest (SQLCipher), and only database.py's opener knows to apply PRAGMA key. Reading
# DB_PATH from config also keeps this seeder pointed at the same file as the rest of
# the pipeline when ORB_DB_PATH is overridden.
from config import DB_PATH
from database import _open_raw_connection, _row_factory_for

OLLAMA_URL = "http://localhost:11434/api/generate"

# Clinically appropriate fallback medications for each patient if Ollama times out/fails
FALLBACK_MEDS = {
    "Diabetic ketoacidosis": [
        {"drug_name": "Insulin Aspart 100 units/mL Injection", "generic_name": "Insulin", "dosage": "0.1U/kg/hr", "route": "IV", "frequency": "Continuous", "strength": "100 units/mL", "form": "Vial"},
        {"drug_name": "Normal Saline 0.9% 1000mL", "generic_name": "Normal Saline", "dosage": "1L/hr", "route": "IV", "frequency": "Continuous", "strength": "0.9%", "form": "IV fluid"},
        {"drug_name": "Potassium Chloride 20mEq Injection", "generic_name": "Potassium", "dosage": "20mEq/L", "route": "IV", "frequency": "In fluids", "strength": "20mEq", "form": "Ampoule"}
    ],
    "COPD exacerbation": [
        {"drug_name": "Ipratropium Bromide 500mcg Nebulizer", "generic_name": "Ipratropium", "dosage": "500mcg", "route": "Nebulizer", "frequency": "Q4H", "strength": "500mcg", "form": "Nebulizer solution"},
        {"drug_name": "Prednisolone 40mg Oral Tablet", "generic_name": "Prednisolone", "dosage": "40mg", "route": "PO", "frequency": "Daily", "strength": "40mg", "form": "Tablet"},
        {"drug_name": "Amoxicillin 500mg Oral Capsule", "generic_name": "Amoxicillin", "dosage": "500mg", "route": "PO", "frequency": "Q8H", "strength": "500mg", "form": "Capsule"}
    ],
    "Dengue fever with thrombocytopenia": [
        {"drug_name": "Paracetamol 650mg Oral Tablet", "generic_name": "Paracetamol", "dosage": "650mg", "route": "PO", "frequency": "Q6H", "strength": "650mg", "form": "Tablet"},
        {"drug_name": "Normal Saline 0.9% 500mL", "generic_name": "Normal Saline", "dosage": "500ml/hr", "route": "IV", "frequency": "Continuous", "strength": "0.9%", "form": "IV fluid"}
    ],
    "CAP with early sepsis": [
        {"drug_name": "Monocef 1g Injection", "generic_name": "Ceftriaxone", "dosage": "2g", "route": "IV", "frequency": "Q12H", "strength": "1g", "form": "Vial"},
        {"drug_name": "Normal Saline 0.9% 1000mL", "generic_name": "Normal Saline", "dosage": "1L/hr", "route": "IV", "frequency": "Stat", "strength": "0.9%", "form": "IV fluid"}
    ],
    "UTI with delirium": [
        {"drug_name": "Monocef 1g Injection", "generic_name": "Ceftriaxone", "dosage": "1g", "route": "IV", "frequency": "Q24H", "strength": "1g", "form": "Vial"},
        {"drug_name": "Haloperidol 0.5mg/mL Injection", "generic_name": "Haloperidol", "dosage": "0.5mg", "route": "IV", "frequency": "PRN", "strength": "0.5mg/mL", "form": "Vial"}
    ],
    "Acute STEMI, post-PCI with DES": [
        {"drug_name": "Aspirin 75mg Oral Tablet", "generic_name": "Aspirin", "dosage": "75mg", "route": "PO", "frequency": "Daily", "strength": "75mg", "form": "Tablet"},
        {"drug_name": "Metoprolol Succinate 50mg Oral Tablet", "generic_name": "Metoprolol", "dosage": "25mg", "route": "PO", "frequency": "BD", "strength": "50mg", "form": "Tablet"},
        {"drug_name": "Heparin 5000 units/mL Injection", "generic_name": "Heparin", "dosage": "1000U/hr", "route": "IV", "frequency": "Continuous", "strength": "5000 units/mL", "form": "Vial"}
    ],
    "AF with decompensated heart failure": [
        {"drug_name": "Furosemide 40mg/4mL Injection", "generic_name": "Furosemide", "dosage": "40mg", "route": "IV", "frequency": "BD", "strength": "10mg/mL", "form": "Vial"},
        {"drug_name": "Apixaban 5mg Oral Tablet", "generic_name": "Apixaban", "dosage": "5mg", "route": "PO", "frequency": "BD", "strength": "5mg", "form": "Tablet"}
    ],
    "Hypertensive emergency": [
        {"drug_name": "Labetalol 20mg bolus then 2mg/min", "generic_name": "Labetalol", "dosage": "20mg bolus", "route": "IV", "frequency": "Infusion", "strength": "5mg/mL", "form": "Vial"},
        {"drug_name": "Amlodipine 10mg Oral Tablet", "generic_name": "Amlodipine", "dosage": "10mg", "route": "PO", "frequency": "Daily", "strength": "10mg", "form": "Tablet"}
    ],
    "Unstable angina, triple-vessel CAD": [
        {"drug_name": "Aspirin 150mg Oral Tablet", "generic_name": "Aspirin", "dosage": "150mg", "route": "PO", "frequency": "Daily", "strength": "150mg", "form": "Tablet"},
        {"drug_name": "Enoxaparin 60mg Injection", "generic_name": "Enoxaparin", "dosage": "60mg", "route": "SC", "frequency": "BD", "strength": "60mg", "form": "Syringe"}
    ],
    "Post-CABG day 2": [
        {"drug_name": "Aspirin 150mg Oral Tablet", "generic_name": "Aspirin", "dosage": "150mg", "route": "PO", "frequency": "Daily", "strength": "150mg", "form": "Tablet"},
        {"drug_name": "Paracetamol 650mg Oral Tablet", "generic_name": "Paracetamol", "dosage": "1g", "route": "PO", "frequency": "Q6H", "strength": "650mg", "form": "Tablet"}
    ],
    "Post-laparoscopic appendectomy": [
        {"drug_name": "Paracetamol 650mg Oral Tablet", "generic_name": "Paracetamol", "dosage": "1g", "route": "PO", "frequency": "Q6H", "strength": "650mg", "form": "Tablet"},
        {"drug_name": "Ondansetron 4mg Injection", "generic_name": "Ondansetron", "dosage": "4mg", "route": "IV", "frequency": "PRN", "strength": "2mg/mL", "form": "Ampoule"}
    ],
    "Acute pancreatitis (Ranson 4)": [
        {"drug_name": "Pantoprazole 40mg Oral Tablet", "generic_name": "Pantoprazole", "dosage": "40mg", "route": "IV", "frequency": "BD", "strength": "40mg", "form": "Vial"},
        {"drug_name": "Normal Saline 0.9% 1000mL", "generic_name": "Normal Saline", "dosage": "200ml/hr", "route": "IV", "frequency": "Continuous", "strength": "0.9%", "form": "IV fluid"}
    ],
    "Penetrating abdominal trauma, post-ex-lap": [
        {"drug_name": "Cefazolin 2g Injection", "generic_name": "Cefazolin", "dosage": "2g", "route": "IV", "frequency": "Q8H", "strength": "2g", "form": "Vial"},
        {"drug_name": "Metronidazole 500mg Injection", "generic_name": "Metronidazole", "dosage": "500mg", "route": "IV", "frequency": "Q8H", "strength": "5mg/mL", "form": "Infusion Bag"}
    ],
    "Cholecystectomy with bile duct injury": [
        {"drug_name": "Piperacillin-Tazobactam 4.5g Injection", "generic_name": "Piperacillin-Tazobactam", "dosage": "4.5g", "route": "IV", "frequency": "Q6H", "strength": "4.5g", "form": "Vial"}
    ],
    "Perforated DU, post-Graham patch": [
        {"drug_name": "Monocef 1g Injection", "generic_name": "Ceftriaxone", "dosage": "2g", "route": "IV", "frequency": "Q12H", "strength": "1g", "form": "Vial"},
        {"drug_name": "Pantoprazole 40mg Oral Tablet", "generic_name": "Pantoprazole", "dosage": "40mg", "route": "IV", "frequency": "BD", "strength": "40mg", "form": "Vial"}
    ],
    "Hip fracture, post-ORIF": [
        {"drug_name": "Paracetamol 650mg Oral Tablet", "generic_name": "Paracetamol", "dosage": "1g", "route": "PO", "frequency": "Q6H", "strength": "650mg", "form": "Tablet"}
    ],
    "Polytrauma - femur + tibia fractures": [
        {"drug_name": "Cefazolin 2g Injection", "generic_name": "Cefazolin", "dosage": "2g", "route": "IV", "frequency": "Q8H", "strength": "2g", "form": "Vial"}
    ],
    "Total knee replacement, post-op day 1": [
        {"drug_name": "Paracetamol 650mg Oral Tablet", "generic_name": "Paracetamol", "dosage": "1g", "route": "PO", "frequency": "Q6H", "strength": "650mg", "form": "Tablet"}
    ],
    "Open fracture tibia Gustilo IIIA": [
        {"drug_name": "Cefazolin 2g Injection", "generic_name": "Cefazolin", "dosage": "2g", "route": "IV", "frequency": "Q8H", "strength": "2g", "form": "Vial"},
        {"drug_name": "Gentamicin 80mg Injection", "generic_name": "Gentamicin", "dosage": "5mg/kg", "route": "IV", "frequency": "Daily", "strength": "40mg/mL", "form": "Vial"}
    ],
    "Vertebral compression fracture, osteoporosis": [
        {"drug_name": "Paracetamol 650mg Oral Tablet", "generic_name": "Paracetamol", "dosage": "1g", "route": "PO", "frequency": "Q6H", "strength": "650mg", "form": "Tablet"}
    ],
    "ARDS on NIV": [
        {"drug_name": "Meropenem 1g Injection", "generic_name": "Meropenem", "dosage": "1g", "route": "IV", "frequency": "Q8H", "strength": "1g", "form": "Vial"},
        {"drug_name": "Dexamethasone 4mg/mL Injection", "generic_name": "Dexamethasone", "dosage": "6mg", "route": "IV", "frequency": "Daily", "strength": "4mg/mL", "form": "Vial"}
    ],
    "Severe asthma exacerbation": [
        {"drug_name": "Salbutamol 5mg Nebulizer", "generic_name": "Salbutamol", "dosage": "5mg", "route": "Nebulizer", "frequency": "Q1H", "strength": "5mg/mL", "form": "Nebulizer solution"},
        {"drug_name": "Hydrocortisone 100mg Injection", "generic_name": "Hydrocortisone", "dosage": "100mg", "route": "IV", "frequency": "Q8H", "strength": "100mg", "form": "Vial"}
    ],
    "Lung abscess with empyema": [
        {"drug_name": "Clindamycin 300mg Injection", "generic_name": "Clindamycin", "dosage": "600mg", "route": "IV", "frequency": "Q8H", "strength": "150mg/mL", "form": "Vial"},
        {"drug_name": "Monocef 1g Injection", "generic_name": "Ceftriaxone", "dosage": "2g", "route": "IV", "frequency": "Daily", "strength": "1g", "form": "Vial"}
    ],
    "Sub-massive pulmonary embolism": [
        {"drug_name": "Heparin 5000 units/mL Injection", "generic_name": "Heparin", "dosage": "1000U/hr", "route": "IV", "frequency": "Continuous", "strength": "5000 units/mL", "form": "Vial"},
        {"drug_name": "Warfarin 5mg Oral Tablet", "generic_name": "Warfarin", "dosage": "5mg", "route": "PO", "frequency": "Daily", "strength": "5mg", "form": "Tablet"}
    ],
    "ILD acute exacerbation": [
        {"drug_name": "Methylprednisolone 1g Injection", "generic_name": "Methylprednisolone", "dosage": "1g", "route": "IV", "frequency": "Daily x3", "strength": "1g", "form": "Vial"},
        {"drug_name": "Pirfenidone 267mg Oral Tablet", "generic_name": "Pirfenidone", "dosage": "801mg", "route": "PO", "frequency": "TDS", "strength": "267mg", "form": "Tablet"}
    ],
    "Acute ischemic stroke, MCA, NIHSS 14": [
        {"drug_name": "Alteplase 50mg Injection", "generic_name": "Alteplase", "dosage": "0.9mg/kg", "route": "IV", "frequency": "Infusion", "strength": "50mg", "form": "Vial"},
        {"drug_name": "Aspirin 325mg Oral Tablet", "generic_name": "Aspirin", "dosage": "325mg", "route": "PO", "frequency": "Daily", "strength": "325mg", "form": "Tablet"}
    ],
    "Status epilepticus, post-ictal": [
        {"drug_name": "Levetiracetam 500mg Oral Tablet", "generic_name": "Levetiracetam", "dosage": "1g", "route": "IV", "frequency": "BD", "strength": "500mg", "form": "Tablet"},
        {"drug_name": "Phenytoin 250mg/5mL Injection", "generic_name": "Phenytoin", "dosage": "1g loading", "route": "IV", "frequency": "Over 30min", "strength": "50mg/mL", "form": "Vial"}
    ],
    "Guillain-Barre syndrome": [
        {"drug_name": "IVIg 5g Injection", "generic_name": "IVIg", "dosage": "0.4g/kg/day", "route": "IV", "frequency": "Daily x5", "strength": "5g", "form": "Vial"}
    ],
    "Tuberculous meningitis": [
        {"drug_name": "Dexamethasone 4mg/mL Injection", "generic_name": "Dexamethasone", "dosage": "0.4mg/kg/day", "route": "IV", "frequency": "Daily", "strength": "4mg/mL", "form": "Vial"},
        {"drug_name": "Mannitol 20% 100mL Injection", "generic_name": "Mannitol", "dosage": "100ml", "route": "IV", "frequency": "Q8H", "strength": "20%", "form": "Infusion Bag"}
    ],
    "Parkinson's with aspiration pneumonia": [
        {"drug_name": "Levodopa-Carbidopa 100mg/25mg Oral Tablet", "generic_name": "Levodopa-Carbidopa", "dosage": "25/100mg", "route": "PO", "frequency": "TDS", "strength": "100mg/25mg", "form": "Tablet"}
    ]
}

def query_medgemma_for_drugs(diagnosis, chief_complaint):
    """Query the local MedGemma model via Ollama to suggest drugs for a patient's diagnosis."""
    prompt = f"""You are MedGemma, a clinical AI assistant.
A patient is admitted to the hospital.
Diagnosis: {diagnosis}
Chief Complaint: {chief_complaint}

List exactly 3 standard medications that should be prescribed for this patient's condition.
Format your response as a JSON object with a single key "medications" containing a list of objects.
Each object must have these exact keys:
- "drug_name": the exact brand or full name of the drug, capitalized (e.g., "Metoprolol Succinate 50mg Oral Tablet", "Amoxicillin 500mg Oral Capsule")
- "generic_name": the generic drug name (e.g., "Metoprolol", "Amoxicillin", "Insulin", "Ceftriaxone")
- "strength": strength and unit (e.g., "50mg", "500mg", "100 units/mL", "1g")
- "form": form of the drug (e.g., "Tablet", "Capsule", "Vial", "Ampoule", "IV fluid")
- "dosage": dosage to administer (e.g., "50mg", "500mg", "0.1 units/kg/hr", "1g", "25mg")
- "route": route of administration (e.g., "PO", "IV", "SC", "Nebulizer")
- "frequency": frequency (e.g., "Daily", "BD", "Q6H", "Continuous")

Respond ONLY with the JSON object. Do not include any explanation or extra text.
"""
    
    data = {
        "model": "medgemma:4b", # use 4b for fast local inference to avoid timeout
        "prompt": prompt,
        "stream": False,
        "format": "json"
    }
    
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(data).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    
    try:
        # 10 second timeout per call to ensure we don't hang
        with urllib.request.urlopen(req, timeout=10) as response:
            res = json.loads(response.read().decode('utf-8'))
            resp_text = res.get('response', '')
            parsed = json.loads(resp_text)
            if isinstance(parsed, dict) and "medications" in parsed:
                return parsed["medications"]
            elif isinstance(parsed, list):
                return parsed
    except Exception as e:
        print(f"  [MedGemma API] Warning: Failed to query MedGemma for '{diagnosis}': {e}. Using fallback.")
    
    # Fallback if Ollama fails
    for k, meds in FALLBACK_MEDS.items():
        if k.lower() in diagnosis.lower() or diagnosis.lower() in k.lower():
            return meds
            
    # Default catch-all fallback if no key matched
    return [
        {"drug_name": "Paracetamol 650mg Oral Tablet", "generic_name": "Paracetamol", "dosage": "650mg", "route": "PO", "frequency": "Q6H", "strength": "650mg", "form": "Tablet"},
        {"drug_name": "Pantoprazole 40mg Oral Tablet", "generic_name": "Pantoprazole", "dosage": "40mg", "route": "PO", "frequency": "Daily", "strength": "40mg", "form": "Tablet"}
    ]

def get_connection():
    # _open_raw_connection() already carries timeout=30 and applies the SQLCipher key
    # when one is configured. Its row class must come from the same driver — stdlib
    # sqlite3.Row raises TypeError on a SQLCipher cursor — and every patient["..."]
    # lookup below needs a mapping-style row.
    conn = _open_raw_connection()
    conn.row_factory = _row_factory_for(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def clean_db():
    """Clear old medications, orders, lots and their stock ledger to start fresh.

    The formulary rows are KEPT (grn-4, as seed_helix does): a pharmacist's own
    entries on a drug (pack size, GST, classification, storage, reorder levels)
    survive, and main() only adds drugs it does not already have. The ledger
    is cleared before the lots it describes."""
    print("Clearing existing medications and Helix tables for sync...")
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = OFF")
    cursor.execute("DELETE FROM medications")
    cursor.execute("DELETE FROM helix_patient_med_orders")
    cursor.execute("DELETE FROM helix_stock_movements")
    cursor.execute("DELETE FROM helix_inventory_lots")
    cursor.execute("DELETE FROM helix_transitions")
    cursor.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()

def main():
    print(f"Connecting to database: {DB_PATH}")
    clean_db()
    
    from services import pharmacy_stock_service as stock

    conn = get_connection()
    cursor = conn.cursor()
    
    # Get all admitted patients
    patients = cursor.execute("""
        SELECT p.id, p.name, p.diagnosis, a.chief_complaint, p.attending_doctor_id 
        FROM patients p
        JOIN admissions a ON p.id = a.patient_id AND a.status = 'active'
        WHERE p.status = 'admitted'
    """).fetchall()
    
    print(f"Found {len(patients)} admitted patients. Suggesting medications via MedGemma...")
    
    possible_statuses = ["pending", "reviewing", "verified", "scheduled", "dispensed", "delivered"]
    
    total_inserted = 0
    
    for idx, patient in enumerate(patients):
        pid = patient["id"]
        pname = patient["name"]
        diagnosis = patient["diagnosis"]
        cc = patient["chief_complaint"]
        attending_doc = patient["attending_doctor_id"] or "DOC001"
        
        print(f"[{idx+1}/{len(patients)}] Patient: {pname} | Diagnosis: {diagnosis}")
        
        meds = query_medgemma_for_drugs(diagnosis, cc)
        print(f"  -> Recommended {len(meds)} meds.")
        
        patient_meds_list = []
        
        for m_idx, m in enumerate(meds):
            drug_name = m.get("drug_name") or m.get("name")
            generic_name = m.get("generic_name") or drug_name
            dosage = m.get("dosage") or "TBD"
            route = m.get("route") or "Oral"
            frequency = m.get("frequency") or "Daily"
            strength = m.get("strength") or "500mg"
            form = m.get("form") or "Tablet"
            
            # 1. Insert into Helix Master Catalog if not exists
            # Generate a consistent ID for the medication
            med_id = "MED-" + drug_name.upper().replace(" ", "-").replace("/", "-").replace("%", "").replace(",", "")[:15]
            
            existing_master = cursor.execute("SELECT id FROM helix_medication_master WHERE id = ?", (med_id,)).fetchone()
            if not existing_master:
                cursor.execute(
                    "INSERT INTO helix_medication_master (id, name, generic_name, form, strength) VALUES (?, ?, ?, ?, ?)",
                    (med_id, drug_name, generic_name, form, strength)
                )
            
            # 2. Insert into Helix Inventory Lot if not exists
            lot_id = "LOT-" + med_id[4:] + "-1"
            lot_number = "LOT" + med_id[4:].replace("-", "")[:8]
            existing_lot = cursor.execute("SELECT id FROM helix_inventory_lots WHERE id = ?", (lot_id,)).fetchone()
            if not existing_lot:
                # Through the one stock writer (grn-4): the lot opens at zero
                # and its 100 units arrive as an 'opening' movement. The
                # service writes only inside its caller's transaction, so one
                # is opened here if no earlier write of this run has opened it.
                if not conn.in_transaction:
                    cursor.execute("BEGIN IMMEDIATE")
                stock.open_or_merge_lot(
                    conn, lot_id=lot_id, medication_id=med_id, lot_number=lot_number,
                    expiration_date="2030-12-31", units=100, actor_id="system",
                    movement_type="opening", remarks="Opening stock written by the seed",
                )
            
            # 3. Create a Helix patient medication order
            # Spread order statuses realistically across the pharmacy state machine
            if m_idx == 0:
                status = "delivered"
            elif m_idx == 1:
                status = "verified"
            else:
                status = "pending"
                
            order_id = str(uuid.uuid4())
            
            # Build transition timestamps if needed
            now_iso = datetime.now(timezone.utc).isoformat()
            reviewed_by = "PHA001" if status in ["verified", "scheduled", "dispensed", "delivered"] else None
            reviewed_at = now_iso if reviewed_by else None
            
            cursor.execute("""
                INSERT INTO helix_patient_med_orders (
                    id, patient_id, medication_id, status, prescribed_by, dosage, frequency, route, 
                    reviewed_by, reviewed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (order_id, pid, med_id, status, attending_doc, dosage, frequency, route, reviewed_by, reviewed_at, now_iso))
            
            # Add a mock transition log entry for audit trail
            trans_id = str(uuid.uuid4())
            cursor.execute("""
                INSERT INTO helix_transitions (id, order_id, from_status, to_status, actor_id, payload, created_at)
                VALUES (?, ?, 'pending', ?, ?, '{"seeded": true}', ?)
            """, (trans_id, order_id, status, attending_doc, now_iso))
            
            # 4. Insert into medications table (Vigil) so it is visible in Vigil
            # Status in medications is active, discontinued or prn
            vigil_status = "active"
            cursor.execute("""
                INSERT INTO medications (patient_id, drug_name, dosage, route, frequency, status, prescribed_by, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (pid, drug_name, dosage, route, frequency, vigil_status, attending_doc, now_iso))
            
            # Add to local profile list
            patient_meds_list.append({
                "drug_name": drug_name,
                "dose": dosage,
                "route": route,
                "frequency": frequency
            })
            total_inserted += 1
            
        # 5. Update current_medications in patient_profiles
        cursor.execute("""
            INSERT OR REPLACE INTO patient_profiles (patient_id, current_medications, updated_at)
            VALUES (?, ?, ?)
        """, (pid, json.dumps(patient_meds_list), datetime.now(timezone.utc).isoformat()))
        
    conn.commit()
    conn.close()
    
    print(f"\nSeeding completed successfully! Inserted {total_inserted} medications/orders across {len(patients)} patients.")

if __name__ == "__main__":
    main()
