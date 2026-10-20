"""Orb Clinical Data Seeder — 30 patients across 6 departments with realistic vitals."""

import json
import os
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth import hash_password  # noqa: E402 - application imports require the path/environment bootstrap above.
from database import db_session, init_db  # noqa: E402 - application imports require the path/environment bootstrap above.
from services.clinical_time import format_utc, to_utc_iso, utc_iso_now  # noqa: E402 - application imports require the path/environment bootstrap above.

# Initialize DB schema + migrations first
init_db()


# ── Password hashes ──
# Seed with salted bcrypt (the production hasher), not the legacy unsalted
# SHA-256 path. The old seeder wrote sha256 digests of trivial demo passwords —
# if ever run against a real DB those were live, offline-crackable credentials.
# bcrypt keeps the demo logins working while removing the weak-hash liability.
def h(pw):
    return hash_password(pw)


USERS = [
    ("DOC001", "Dr. Meera Sharma", "doctor", "General Medicine", h("password")),
    ("DOC002", "Dr. Arjun Mehta", "doctor", "Cardiology", h("password")),
    ("DOC004", "Dr. Vikram Rathore", "doctor", "General Surgery", h("password")),
    # No second login for the same surgeon. A "dr_rathore" row used to sit here
    # beside DOC004, so the front desk offered "Dr. Vikram Rathore" twice and the
    # user list showed two of him. reset_db_and_seed keeps only the logins named
    # in this list, so dropping it here removes it on the next reseed.
    ("anesthetist_1", "Dr. Nandini Rao", "doctor", "Anesthesiology", h("password")),
    ("anesthetist_2", "Dr. Amit Patel", "doctor", "Anesthesiology", h("password")),
    ("DOC005", "Dr. Sanjay Rao", "doctor", "Orthopedics", h("password")),
    ("DOC006", "Dr. Anita Desai", "doctor", "Pulmonology", h("password")),
    ("DOC007", "Dr. Kiran Bhat", "doctor", "Neurology", h("password")),
    ("NUR001", "Priya Reddy", "nurse", "General Medicine", h("password")),
    # Department deliberately NULL, not "Administration" (2026-08-12).
    # permissions.department_scope() returns the user's department, and None is
    # this repo's documented convention for a HOSPITAL-WIDE governance account.
    # Seeded with "Administration" the admin was ward-scoped to a ward that has
    # no patients, which quietly crippled every governance surface: Vigil showed
    # 0 patients beside 45 named critical alerts, billing and front-desk pickers
    # were empty, and — worst — Sage Provenance review read "0 recorded turns",
    # so the incident-review screen could never see a single clinician turn.
    # A governance account that cannot see anything is not a safe default, it is
    # an invisible one.
    ("ADM001", "Rajesh Kumar", "admin", None, h("password")),
    # Department deliberately NULL for the SAME reason as ADM001 above
    # (found 2026-09-07). Seeded with "Pharmacy" this account was ward-scoped to
    # a department NOBODY IS EVER ADMITTED TO, so enforce_patient_scope compared
    # "Pharmacy" against each patient's admission department, never matched, and
    # 404'd every clinical patient: POST /api/helix/orders/approve answered
    # "Patient not found" for the entire hospital. Pharmacy is a hospital-wide
    # function, not a ward, and routes/helix_routes.py and
    # routes/payments_routes.py both already document that assumption in prose.
    # The seed was the only thing that disagreed.
    ("PHA001", "Deepa Joshi", "pharmacist", None, h("password")),
    ("P001", "Raj Patel", "patient", None, h("patient123")),
]

# Triage: emergency=1, urgent=2, standard=3
TRIAGE = {"emergency": 1, "urgent": 2, "standard": 3}

# ── 30 Patient definitions ──
PATIENTS = [
    # (name, age, sex, dept, doc_id, ward, bed, mrn, cc, dx, triage_str,
    #  comorbidities, allergies, medications, base_vitals, is_deteriorating)
    # ── General Medicine (DOC001) ──
    (
        "Ananya Kapoor",
        34,
        "Female",
        "General Medicine",
        "DOC001",
        "GM-1",
        "GM-101",
        "MRN-GM001",
        "Polyuria, vomiting, abdominal pain",
        "Diabetic ketoacidosis",
        "urgent",
        ["Type 1 DM"],
        ["Sulfa drugs"],
        [
            ("Insulin infusion", "0.1U/kg/hr", "IV", "Continuous"),
            ("Normal Saline", "1L/hr", "IV", "Continuous"),
            ("KCl", "20mEq/L", "IV", "In fluids"),
        ],
        {"hr": 118, "bp_sys": 100, "bp_dia": 65, "spo2": 97, "temperature": 37.2, "rr": 28},
        False,
    ),
    (
        "Rajesh Iyer",
        67,
        "Male",
        "General Medicine",
        "DOC001",
        "GM-1",
        "GM-102",
        "MRN-GM002",
        "Worsening dyspnea, productive cough",
        "COPD exacerbation",
        "urgent",
        ["COPD GOLD III", "Hypertension"],
        ["Penicillin"],
        [
            ("Ipratropium nebulizer", "500mcg", "Nebulizer", "Q4H"),
            ("Prednisolone", "40mg", "PO", "Daily"),
            ("O2 Nasal Cannula", "2L/min", "Inhalation", "Continuous"),
        ],
        {"hr": 102, "bp_sys": 145, "bp_dia": 88, "spo2": 88, "temperature": 37.8, "rr": 26},
        True,
    ),
    (
        "Priya Nair",
        28,
        "Female",
        "General Medicine",
        "DOC001",
        "GM-1",
        "GM-103",
        "MRN-GM003",
        "High fever, body ache, rash",
        "Dengue fever with thrombocytopenia",
        "urgent",
        [],
        [],
        [("Paracetamol", "1g", "PO", "Q6H"), ("Normal Saline", "500ml/hr", "IV", "Continuous")],
        {"hr": 108, "bp_sys": 95, "bp_dia": 60, "spo2": 96, "temperature": 39.2, "rr": 20},
        False,
    ),
    (
        "Suresh Reddy",
        55,
        "Male",
        "General Medicine",
        "DOC001",
        "GM-1",
        "GM-104",
        "MRN-GM004",
        "Fever, cough, altered sensorium",
        "CAP with early sepsis",
        "emergency",
        ["Type 2 DM", "CKD Stage 2"],
        [],
        [
            ("Piperacillin-Tazobactam", "4.5g", "IV", "Q6H"),
            ("Normal Saline bolus", "30ml/kg", "IV", "Stat"),
            ("Noradrenaline", "0.1mcg/kg/min", "IV", "If MAP<65"),
        ],
        {"hr": 112, "bp_sys": 85, "bp_dia": 52, "spo2": 91, "temperature": 39.8, "rr": 28},
        True,
    ),
    (
        "Lakshmi Devi",
        80,
        "Female",
        "General Medicine",
        "DOC001",
        "GM-1",
        "GM-105",
        "MRN-GM005",
        "Confusion, fever, dysuria",
        "UTI with delirium",
        "urgent",
        ["Hypertension", "Osteoarthritis"],
        ["Ciprofloxacin"],
        [
            ("Ceftriaxone", "1g", "IV", "Q24H"),
            ("Haloperidol", "0.5mg", "IV", "PRN"),
            ("Normal Saline", "100ml/hr", "IV", "Continuous"),
        ],
        {"hr": 96, "bp_sys": 130, "bp_dia": 78, "spo2": 94, "temperature": 38.4, "rr": 18},
        False,
    ),
    # ── Cardiology (DOC002) ──
    (
        "Vikram Singh",
        72,
        "Male",
        "Cardiology",
        "DOC002",
        "CCU",
        "CCU-01",
        "MRN-CC001",
        "Severe chest pain, diaphoresis",
        "Acute STEMI, post-PCI with DES",
        "emergency",
        ["Hypertension", "Dyslipidemia"],
        [],
        [
            ("Aspirin", "75mg", "PO", "Daily"),
            ("Ticagrelor", "90mg", "PO", "BD"),
            ("Atorvastatin", "80mg", "PO", "Nightly"),
            ("Metoprolol", "25mg", "PO", "BD"),
            ("Heparin infusion", "1000U/hr", "IV", "Continuous"),
        ],
        {"hr": 68, "bp_sys": 150, "bp_dia": 95, "spo2": 96, "temperature": 36.8, "rr": 16},
        False,
    ),
    (
        "Kavitha Menon",
        58,
        "Female",
        "Cardiology",
        "DOC002",
        "CCU",
        "CCU-02",
        "MRN-CC002",
        "Worsening breathlessness, orthopnea",
        "AF with decompensated heart failure",
        "emergency",
        ["Rheumatic heart disease", "AF"],
        [],
        [
            ("Furosemide", "40mg", "IV", "BD"),
            ("Digoxin", "0.25mg", "PO", "Daily"),
            ("Apixaban", "5mg", "PO", "BD"),
            ("Carvedilol", "3.125mg", "PO", "BD"),
        ],
        {"hr": 132, "bp_sys": 160, "bp_dia": 100, "spo2": 89, "temperature": 36.6, "rr": 28},
        True,
    ),
    (
        "Deepak Joshi",
        45,
        "Male",
        "Cardiology",
        "DOC002",
        "CCU",
        "CCU-03",
        "MRN-CC003",
        "Severe headache, blurred vision",
        "Hypertensive emergency",
        "emergency",
        [],
        [],
        [
            ("Labetalol", "20mg bolus then 2mg/min", "IV", "Infusion"),
            ("Amlodipine", "10mg", "PO", "Daily"),
        ],
        {"hr": 88, "bp_sys": 220, "bp_dia": 130, "spo2": 97, "temperature": 36.9, "rr": 18},
        False,
    ),
    (
        "Sunita Agarwal",
        63,
        "Female",
        "Cardiology",
        "DOC002",
        "CCU",
        "CCU-04",
        "MRN-CC004",
        "Recurrent chest pain at rest",
        "Unstable angina, triple-vessel CAD",
        "urgent",
        ["Type 2 DM", "Hypothyroidism"],
        [],
        [
            ("Aspirin", "150mg", "PO", "Daily"),
            ("Clopidogrel", "75mg", "PO", "Daily"),
            ("Enoxaparin", "60mg", "SC", "BD"),
            ("GTN infusion", "5mcg/min", "IV", "PRN"),
            ("Atorvastatin", "40mg", "PO", "Nightly"),
        ],
        {"hr": 78, "bp_sys": 138, "bp_dia": 82, "spo2": 95, "temperature": 36.7, "rr": 16},
        False,
    ),
    (
        "Ramesh Gupta",
        50,
        "Male",
        "Cardiology",
        "DOC002",
        "CCU",
        "CCU-05",
        "MRN-CC005",
        "Post-operative recovery",
        "Post-CABG day 2",
        "standard",
        ["Dyslipidemia"],
        [],
        [
            ("Aspirin", "150mg", "PO", "Daily"),
            ("Paracetamol", "1g", "PO", "Q6H"),
            ("Tramadol", "50mg", "PO", "Q8H PRN"),
            ("Pantoprazole", "40mg", "PO", "Daily"),
        ],
        {"hr": 82, "bp_sys": 125, "bp_dia": 72, "spo2": 97, "temperature": 37.1, "rr": 14},
        False,
    ),
    # ── General Surgery (DOC004) ──
    (
        "Fatima Begum",
        45,
        "Female",
        "General Surgery",
        "DOC004",
        "SW-1",
        "SW-101",
        "MRN-GS001",
        "RIF pain, fever",
        "Post-laparoscopic appendectomy",
        "standard",
        [],
        [],
        [
            ("Paracetamol", "1g", "PO", "Q6H"),
            ("Ondansetron", "4mg", "IV", "PRN"),
            ("Enoxaparin", "40mg", "SC", "Daily"),
        ],
        {"hr": 78, "bp_sys": 118, "bp_dia": 72, "spo2": 98, "temperature": 37.0, "rr": 14},
        False,
    ),
    (
        "Mohammed Ali",
        41,
        "Male",
        "General Surgery",
        "DOC004",
        "SW-1",
        "SW-102",
        "MRN-GS002",
        "Severe epigastric pain radiating to back",
        "Acute pancreatitis (Ranson 4)",
        "urgent",
        ["Alcohol use disorder"],
        [],
        [
            ("Morphine PCA", "1mg/bolus", "IV", "Q10min PRN"),
            ("Pantoprazole", "40mg", "IV", "BD"),
            ("Normal Saline", "200ml/hr", "IV", "Continuous"),
        ],
        {"hr": 110, "bp_sys": 105, "bp_dia": 68, "spo2": 95, "temperature": 38.6, "rr": 22},
        False,
    ),
    (
        "Arun Krishnan",
        33,
        "Male",
        "General Surgery",
        "DOC004",
        "SW-1",
        "SW-103",
        "MRN-GS003",
        "Stab wound abdomen",
        "Penetrating abdominal trauma, post-ex-lap",
        "emergency",
        [],
        [],
        [
            ("Cefazolin", "2g", "IV", "Q8H"),
            ("Metronidazole", "500mg", "IV", "Q8H"),
            ("Packed RBC", "2 units", "IV", "Transfusion"),
            ("Normal Saline", "150ml/hr", "IV", "Continuous"),
        ],
        {"hr": 118, "bp_sys": 90, "bp_dia": 55, "spo2": 93, "temperature": 37.4, "rr": 24},
        True,
    ),
    (
        "Geeta Sharma",
        52,
        "Female",
        "General Surgery",
        "DOC004",
        "SW-1",
        "SW-104",
        "MRN-GS004",
        "Post-op jaundice, fever",
        "Cholecystectomy with bile duct injury",
        "urgent",
        ["Gallstones"],
        [],
        [
            ("Piperacillin-Tazobactam", "4.5g", "IV", "Q6H"),
            ("Ursodeoxycholic acid", "300mg", "PO", "BD"),
            ("Tramadol", "50mg", "PO", "Q8H"),
        ],
        {"hr": 92, "bp_sys": 128, "bp_dia": 78, "spo2": 96, "temperature": 38.2, "rr": 18},
        False,
    ),
    (
        "Harish Patel",
        60,
        "Male",
        "General Surgery",
        "DOC004",
        "SW-1",
        "SW-105",
        "MRN-GS005",
        "Sudden severe abdominal pain, rigidity",
        "Perforated DU, post-Graham patch",
        "emergency",
        ["Chronic NSAID use", "H. pylori"],
        [],
        [
            ("Ceftriaxone", "2g", "IV", "Q12H"),
            ("Metronidazole", "500mg", "IV", "Q8H"),
            ("Pantoprazole", "80mg then 40mg", "IV", "BD"),
            ("Normal Saline", "125ml/hr", "IV", "Continuous"),
        ],
        {"hr": 104, "bp_sys": 108, "bp_dia": 65, "spo2": 94, "temperature": 38.8, "rr": 22},
        False,
    ),
    # ── Orthopedics (DOC005) ──
    (
        "Meena Kumari",
        78,
        "Female",
        "Orthopedics",
        "DOC005",
        "OW-1",
        "OW-101",
        "MRN-OR001",
        "Fall at home, hip pain",
        "Hip fracture, post-ORIF",
        "urgent",
        ["Osteoporosis", "Hypertension"],
        [],
        [
            ("Enoxaparin", "40mg", "SC", "Daily"),
            ("Paracetamol", "1g", "PO", "Q6H"),
            ("Calcium + Vit D3", "500mg+250IU", "PO", "Daily"),
        ],
        {"hr": 76, "bp_sys": 135, "bp_dia": 80, "spo2": 96, "temperature": 36.8, "rr": 14},
        False,
    ),
    (
        "Ajay Tiwari",
        25,
        "Male",
        "Orthopedics",
        "DOC005",
        "OW-1",
        "OW-102",
        "MRN-OR002",
        "MVA, multiple fractures",
        "Polytrauma - femur + tibia fractures",
        "emergency",
        [],
        [],
        [
            ("Morphine PCA", "1mg/bolus", "IV", "Q10min PRN"),
            ("Cefazolin", "2g", "IV", "Q8H"),
            ("Enoxaparin", "40mg", "SC", "Daily"),
            ("Normal Saline", "125ml/hr", "IV", "Continuous"),
        ],
        {"hr": 108, "bp_sys": 100, "bp_dia": 62, "spo2": 94, "temperature": 37.6, "rr": 20},
        False,
    ),
    (
        "Radha Venkatesh",
        65,
        "Female",
        "Orthopedics",
        "DOC005",
        "OW-1",
        "OW-103",
        "MRN-OR003",
        "Knee pain, planned surgery",
        "Total knee replacement, post-op day 1",
        "standard",
        ["Osteoarthritis", "Type 2 DM"],
        [],
        [
            ("Paracetamol", "1g", "PO", "Q6H"),
            ("Tramadol", "50mg", "PO", "Q8H PRN"),
            ("Enoxaparin", "40mg", "SC", "Daily"),
        ],
        {"hr": 80, "bp_sys": 130, "bp_dia": 78, "spo2": 97, "temperature": 37.0, "rr": 14},
        False,
    ),
    (
        "Siddharth Nair",
        30,
        "Male",
        "Orthopedics",
        "DOC005",
        "OW-1",
        "OW-104",
        "MRN-OR004",
        "Road traffic accident, open wound leg",
        "Open fracture tibia Gustilo IIIA",
        "emergency",
        [],
        ["Iodine"],
        [
            ("Cefazolin", "2g", "IV", "Q8H"),
            ("Gentamicin", "5mg/kg", "IV", "Daily"),
            ("Morphine PCA", "1mg/bolus", "IV", "Q10min PRN"),
            ("TT", "0.5ml", "IM", "Stat"),
        ],
        {"hr": 96, "bp_sys": 115, "bp_dia": 70, "spo2": 97, "temperature": 37.8, "rr": 16},
        False,
    ),
    (
        "Kamala Bai",
        82,
        "Female",
        "Orthopedics",
        "DOC005",
        "OW-1",
        "OW-105",
        "MRN-OR005",
        "Back pain after minor fall",
        "Vertebral compression fracture, osteoporosis",
        "standard",
        ["Osteoporosis", "Hypertension"],
        [],
        [
            ("Paracetamol", "1g", "PO", "Q6H"),
            ("Calcitonin nasal spray", "200IU", "Nasal", "Daily"),
            ("Calcium + Vit D3", "500mg+250IU", "PO", "Daily"),
        ],
        {"hr": 72, "bp_sys": 140, "bp_dia": 82, "spo2": 96, "temperature": 36.6, "rr": 14},
        False,
    ),
    # ── Pulmonology (DOC006) ──
    (
        "Ravi Shankar",
        70,
        "Male",
        "Pulmonology",
        "DOC006",
        "RCU",
        "RCU-01",
        "MRN-PU001",
        "Severe dyspnea, bilateral infiltrates",
        "ARDS on NIV",
        "emergency",
        ["COPD", "Type 2 DM"],
        [],
        [
            ("Meropenem", "1g", "IV", "Q8H"),
            ("Dexamethasone", "6mg", "IV", "Daily"),
            ("BiPAP", "12/6 cmH2O", "NIV", "Continuous"),
        ],
        {"hr": 115, "bp_sys": 95, "bp_dia": 58, "spo2": 88, "temperature": 38.4, "rr": 32},
        True,
    ),
    (
        "Pooja Malhotra",
        40,
        "Female",
        "Pulmonology",
        "DOC006",
        "RCU",
        "RCU-02",
        "MRN-PU002",
        "Acute wheezing, inability to speak",
        "Severe asthma exacerbation",
        "emergency",
        ["Asthma"],
        ["Aspirin"],
        [
            ("Salbutamol nebulizer", "5mg", "Nebulizer", "Q1H"),
            ("Ipratropium", "500mcg", "Nebulizer", "Q4H"),
            ("Hydrocortisone", "100mg", "IV", "Q8H"),
            ("MgSO4", "2g", "IV", "Stat"),
        ],
        {"hr": 118, "bp_sys": 135, "bp_dia": 85, "spo2": 90, "temperature": 37.2, "rr": 30},
        False,
    ),
    (
        "Ganesh Pillai",
        62,
        "Male",
        "Pulmonology",
        "DOC006",
        "RCU",
        "RCU-03",
        "MRN-PU003",
        "Persistent fever, foul-smelling sputum",
        "Lung abscess with empyema",
        "urgent",
        ["Alcohol use disorder", "Chronic smoker"],
        [],
        [
            ("Clindamycin", "600mg", "IV", "Q8H"),
            ("Ceftriaxone", "2g", "IV", "Daily"),
            ("Chest tube drainage", "28Fr", "Intercostal", "In situ"),
        ],
        {"hr": 98, "bp_sys": 125, "bp_dia": 75, "spo2": 92, "temperature": 39.4, "rr": 24},
        False,
    ),
    (
        "Nandini Das",
        48,
        "Female",
        "Pulmonology",
        "DOC006",
        "RCU",
        "RCU-04",
        "MRN-PU004",
        "Sudden pleuritic chest pain, hemoptysis",
        "Sub-massive pulmonary embolism",
        "emergency",
        ["OCP use", "Obesity"],
        [],
        [
            ("Enoxaparin", "1mg/kg", "SC", "BD"),
            ("Warfarin", "5mg", "PO", "Daily"),
            ("O2 Nasal Cannula", "4L/min", "Inhalation", "Continuous"),
        ],
        {"hr": 112, "bp_sys": 105, "bp_dia": 68, "spo2": 91, "temperature": 37.0, "rr": 26},
        True,
    ),
    (
        "Vinod Kumar",
        58,
        "Male",
        "Pulmonology",
        "DOC006",
        "RCU",
        "RCU-05",
        "MRN-PU005",
        "Progressive breathlessness, dry cough",
        "ILD acute exacerbation",
        "urgent",
        ["IPF diagnosed 2y ago"],
        [],
        [
            ("Methylprednisolone pulse", "1g", "IV", "Daily x3"),
            ("Pirfenidone", "801mg", "PO", "TDS"),
            ("O2 Face mask", "6L/min", "Inhalation", "Continuous"),
        ],
        {"hr": 104, "bp_sys": 130, "bp_dia": 82, "spo2": 86, "temperature": 37.8, "rr": 28},
        False,
    ),
    # ── Neurology (DOC007) ──
    (
        "Savitha Hegde",
        55,
        "Female",
        "Neurology",
        "DOC007",
        "NW-1",
        "NW-101",
        "MRN-NE001",
        "Sudden right-sided weakness, slurred speech",
        "Acute ischemic stroke, MCA, NIHSS 14",
        "emergency",
        ["Hypertension", "AF"],
        [],
        [
            ("Alteplase", "0.9mg/kg", "IV", "Bolus+infusion"),
            ("Aspirin", "325mg", "PO", "Daily (24h post-tPA)"),
            ("Atorvastatin", "80mg", "PO", "Daily"),
            ("Labetalol", "10-20mg", "IV", "PRN BP>185/110"),
        ],
        {"hr": 88, "bp_sys": 175, "bp_dia": 95, "spo2": 96, "temperature": 36.8, "rr": 16},
        True,
    ),
    (
        "Prakash Shetty",
        42,
        "Male",
        "Neurology",
        "DOC007",
        "NW-1",
        "NW-102",
        "MRN-NE002",
        "Witnessed seizures >30 min",
        "Status epilepticus, post-ictal",
        "emergency",
        ["Epilepsy"],
        [],
        [
            ("Levetiracetam", "1g", "IV", "BD"),
            ("Midazolam", "5mg", "IV", "PRN"),
            ("Phenytoin", "1g loading", "IV", "Over 30min"),
        ],
        {"hr": 110, "bp_sys": 145, "bp_dia": 90, "spo2": 92, "temperature": 38.0, "rr": 22},
        True,
    ),
    (
        "Usha Rani",
        68,
        "Female",
        "Neurology",
        "DOC007",
        "NW-1",
        "NW-103",
        "MRN-NE003",
        "Progressive ascending weakness",
        "Guillain-Barre syndrome",
        "urgent",
        [],
        [],
        [("IVIg", "0.4g/kg/day", "IV", "Daily x5"), ("Enoxaparin", "40mg", "SC", "Daily")],
        {"hr": 82, "bp_sys": 130, "bp_dia": 75, "spo2": 95, "temperature": 36.8, "rr": 18},
        False,
    ),
    (
        "Manoj Kulkarni",
        35,
        "Male",
        "Neurology",
        "DOC007",
        "NW-1",
        "NW-104",
        "MRN-NE004",
        "Headache, neck stiffness, fever",
        "Tuberculous meningitis",
        "urgent",
        [],
        [],
        [
            ("HRZE", "As per weight", "PO", "Daily"),
            ("Dexamethasone", "0.4mg/kg/day", "IV", "Tapering"),
            ("Mannitol 20%", "100ml", "IV", "Q8H"),
        ],
        {"hr": 94, "bp_sys": 120, "bp_dia": 72, "spo2": 96, "temperature": 38.6, "rr": 18},
        False,
    ),
    (
        "Indira Patil",
        73,
        "Female",
        "Neurology",
        "DOC007",
        "NW-1",
        "NW-105",
        "MRN-NE005",
        "Fever, cough, difficulty swallowing",
        "Parkinson's with aspiration pneumonia",
        "urgent",
        ["Parkinson's disease x8y", "Dysphagia"],
        [],
        [
            ("Levodopa-Carbidopa", "25/100mg", "PO", "TDS"),
            ("Amoxicillin-Clavulanate", "1.2g", "IV", "Q8H"),
            ("Thickened fluids", "Nectar", "PO", "All feeds"),
        ],
        {"hr": 86, "bp_sys": 118, "bp_dia": 68, "spo2": 92, "temperature": 38.2, "rr": 20},
        False,
    ),
]


def generate_vitals_history(base, hours=24, deteriorating=False):
    now = datetime.now(UTC)
    records = []
    for i in range(hours, 0, -1):
        ts = format_utc(now - timedelta(hours=i))
        det = 0.0
        if deteriorating and i <= 6:
            det = (7 - i) / 6.0

        hr = base["hr"] + random.randint(-5, 5) + int(det * 15)
        sys = base["bp_sys"] + random.randint(-8, 8) - int(det * 12)
        dia = base["bp_dia"] + random.randint(-5, 5) - int(det * 8)
        spo = base["spo2"] + random.uniform(-1, 1) - det * 4
        tmp = base["temperature"] + random.uniform(-0.3, 0.3) + det * 0.6
        rr = base["rr"] + random.randint(-2, 2) + int(det * 6)

        records.append(
            (
                ts,
                max(40, min(200, int(hr))),
                max(60, min(260, int(sys))),
                max(30, min(160, int(dia))),
                max(70, min(100, round(spo, 1))),
                round(max(35.0, min(42.0, tmp)), 1),
                max(8, min(50, int(rr))),
            )
        )
    return records


def seed():
    stats = {
        "users": 0,
        "patients": 0,
        "vitals": 0,
        "risk_scores": 0,
        "channels": 0,
        "meds": 0,
        "skipped": 0,
    }
    now = datetime.now(UTC).isoformat()

    # ── 1. Seed Users ──
    print("═══ Seeding Users ═══")
    with db_session() as db:
        for uid, name, role, dept, pw_hash in USERS:
            db.execute(
                """INSERT OR REPLACE INTO users (id, name, role, department, password_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (uid, name, role, dept, pw_hash),
            )
            stats["users"] += 1
            print(f"  + {uid}: {name} ({role}, {dept})")

    # ── 2. Create patients, admissions, channels, meds — all direct SQL to avoid FK issues ──
    print("\n═══ Admitting 30 Patients ═══")
    patient_map = []  # [(pid, name, base_vitals, is_det)]

    with db_session() as db:
        # Temporarily disable FK for bulk insert (system messages use 'system_admin')
        db.execute("PRAGMA foreign_keys = OFF")

        # Seed Department channels
        print("═══ Seeding Department Channels ═══")
        DEPARTMENTS = [
            ("dept-general", "General Medicine"),
            ("dept-cardiology", "Cardiology"),
            ("dept-surgery", "General Surgery"),
            ("dept-ortho", "Orthopedics"),
            ("dept-pulmono", "Pulmonology"),
            ("dept-neuro", "Neurology"),
            ("dept-emergency", "Emergency"),
        ]

        for dept_id, dept_name in DEPARTMENTS:
            existing_chan = db.execute(
                "SELECT id FROM channels WHERE id = ?", (dept_id,)
            ).fetchone()
            if not existing_chan:
                db.execute(
                    "INSERT INTO channels (id, name, type, created_at) VALUES (?, ?, 'department', ?)",
                    (dept_id, dept_name, now),
                )
                # SQLite's own UTC form, as every other message carries: the
                # ISO `now` sorted after them as text and pinned the welcome
                # line above real traffic for the rest of the day.
                db.execute(
                    """INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, message_type, timestamp)
                       VALUES (?, 'system', 'System', 'system', ?, 'text', ?)""",
                    (
                        dept_id,
                        f"Welcome to the {dept_name} department communication channel.",
                        datetime.fromisoformat(now).strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                stats["channels"] += 1
                print(f"  + {dept_id}: {dept_name}")

        for p in PATIENTS:
            (
                name,
                age,
                sex,
                dept,
                doc_id,
                ward,
                bed,
                mrn,
                cc,
                dx,
                triage_str,
                comorbidities,
                allergies,
                meds,
                base_vitals,
                is_det,
            ) = p

            # Skip if MRN exists
            existing = db.execute("SELECT id FROM patients WHERE mrn = ?", (mrn,)).fetchone()
            if existing:
                patient_map.append((existing["id"], name, base_vitals, is_det))
                stats["skipped"] += 1
                print(f"  ~ {name} (exists: {existing['id']})")
                continue

            pid = f"PAT-{uuid.uuid4().hex[:8].upper()}"
            adm_id = f"ADM-{uuid.uuid4().hex[:8].upper()}"
            chan_id = f"chan_{uuid.uuid4().hex[:12]}"
            triage_int = TRIAGE.get(triage_str, 3)

            # Patient
            db.execute(
                """INSERT INTO patients (id, name, mrn, age, sex, ward, bed, attending_doctor_id,
                   diagnosis, admission_date, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted', ?)""",
                (pid, name, mrn, age, sex, ward, bed, doc_id, dx, now, now),
            )

            # Admission
            db.execute(
                """INSERT INTO admissions (id, patient_id, admitted_by, admission_source, admission_type,
                   department, ward, bed, attending_doctor_id, chief_complaint, initial_diagnosis,
                   triage_severity, initial_vitals_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                (
                    adm_id,
                    pid,
                    doc_id,
                    "Emergency" if triage_str == "emergency" else "OPD Referral",
                    "Emergency" if triage_str == "emergency" else "Elective",
                    dept,
                    ward,
                    bed,
                    doc_id,
                    cc,
                    dx,
                    triage_int,
                    json.dumps(base_vitals),
                    now,
                ),
            )

            # Patient profile
            db.execute(
                """INSERT INTO patient_profiles (patient_id, comorbidities, allergies,
                   current_medications, updated_at) VALUES (?, ?, ?, ?, ?)""",
                (
                    pid,
                    json.dumps(comorbidities),
                    json.dumps(allergies),
                    json.dumps(
                        [
                            {"drug_name": m[0], "dose": m[1], "route": m[2], "frequency": m[3]}
                            for m in meds
                        ]
                    ),
                    now,
                ),
            )

            # Medications
            for drug_name, dose, route, freq in meds:
                db.execute(
                    """INSERT INTO medications (patient_id, drug_name, dosage, route, frequency,
                       status, prescribed_by) VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                    (pid, drug_name, dose, route, freq, doc_id),
                )
                stats["meds"] += 1

            # Initial vitals
            db.execute(
                """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                   diastolic_bp, spo2, temperature, respiratory_rate)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pid,
                    to_utc_iso(now),
                    base_vitals["hr"],
                    base_vitals["bp_sys"],
                    base_vitals["bp_dia"],
                    base_vitals["spo2"],
                    base_vitals["temperature"],
                    base_vitals["rr"],
                ),
            )

            # Channel (case room)
            db.execute(
                "INSERT INTO channels (id, name, type, patient_id, created_at) VALUES (?, ?, 'case_room', ?, ?)",
                (chan_id, f"Case: {name}", pid, now),
            )

            # Sage room memory
            sage_summary = f"{name}, {age}y {sex} admitted to {ward} for {dx}."
            if comorbidities:
                sage_summary += f" PMH: {', '.join(comorbidities)}."
            db.execute(
                """INSERT INTO sage_room_memory (room_id, patient_id, patient_summary,
                   discussion_summary, current_concern, risk_flags, participants,
                   message_count, llm_call_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, '[]', ?, 0, 0, ?, ?)""",
                (
                    chan_id,
                    pid,
                    sage_summary[:1000],
                    f"Patient admitted. Dx: {dx}. CC: {cc}.",
                    cc[:200],
                    json.dumps([next(u[1] for u in USERS if u[0] == doc_id)]),
                    now,
                    now,
                ),
            )

            stats["patients"] += 1
            stats["channels"] += 1
            patient_map.append((pid, name, base_vitals, is_det))
            risk_label = (
                "!" if triage_str == "emergency" else "~" if triage_str == "urgent" else "."
            )
            print(f"  {risk_label} {name} -> {pid} ({dept}, {bed})")

        db.execute("PRAGMA foreign_keys = ON")

    # ── 3. Seed Vitals History ──
    print("\n═══ Seeding 24h Vitals History ═══")
    with db_session() as db:
        for pid, name, base_vitals, is_det in patient_map:
            records = generate_vitals_history(base_vitals, hours=24, deteriorating=is_det)
            for ts, hr, sys, dia, spo, tmp, rr in records:
                db.execute(
                    """INSERT INTO vitals (patient_id, timestamp, heart_rate, systolic_bp,
                       diastolic_bp, spo2, temperature, respiratory_rate)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (pid, ts, hr, sys, dia, spo, tmp, rr),
                )
                stats["vitals"] += 1
            det_label = " (deteriorating)" if is_det else ""
            print(f"  + {name}: 24 records{det_label}")

    # ── 4. Compute Risk Scores ──
    print("\n═══ Computing Risk Scores ═══")
    try:
        from services.vigil_service import get_patient_risk

        for pid, name, _, _ in patient_map:
            try:
                risk = get_patient_risk(pid)
                score = risk.get("score", 0) if isinstance(risk, dict) else 0
                level = risk.get("level", "stable") if isinstance(risk, dict) else "stable"
                drivers = json.dumps(risk.get("drivers", []) if isinstance(risk, dict) else [])
                with db_session() as db:
                    db.execute(
                        """INSERT OR REPLACE INTO risk_scores (patient_id, score, level, drivers, timestamp)
                           VALUES (?, ?, ?, ?, ?)""",
                        (pid, score, level, drivers, utc_iso_now()),
                    )
                stats["risk_scores"] += 1
                sym = "!" if level == "critical" else "~" if level == "elevated" else "."
                print(f"  {sym} {name}: {score:.2f} ({level})")
            except Exception as e:
                print(f"  x {name}: {e}")
    except ImportError as e:
        print(f"  [SKIP] vigil_service not available: {e}")

    # ── 5. Price the OPD consultation ──
    # The front desk charges every completed OPD visit against OPD-CONSULT
    # (frontdesk_service registers the row unpriced at boot). A demo hospital
    # should also PRICE it, or every seeded walk-in ends at the billing counter
    # as "Not configured". ₹800, GST-exempt (healthcare service). A price a
    # human typed over this survives a reseed: only a still-unpriced row is
    # touched.
    print("\n═══ Pricing OPD consultation ═══")
    try:
        from services.frontdesk_service import (
            OPD_CONSULTATION_SERVICE_CODE,
            ensure_frontdesk_schema,
        )

        ensure_frontdesk_schema()
        with db_session() as db:
            db.execute(
                """UPDATE service_items
                      SET default_rate_paise = 80000, rate_is_configured = 1,
                          updated_at = CURRENT_TIMESTAMP
                    WHERE code = ? AND rate_is_configured = 0""",
                (OPD_CONSULTATION_SERVICE_CODE,),
            )
            row = db.execute(
                "SELECT default_rate_paise, rate_is_configured FROM service_items WHERE code = ?",
                (OPD_CONSULTATION_SERVICE_CODE,),
            ).fetchone()
        rate = int(row["default_rate_paise"]) if row else 0
        print(f"  + {OPD_CONSULTATION_SERVICE_CODE}: ₹{rate / 100:.2f}")
    except Exception as e:
        print(f"  x could not price the OPD consultation: {e}")

    # ── 6. Seed the eight non-clinical modules ──
    # Payer, procurement, housekeeping, workforce, dietary, biomedical, ABDM and
    # the billing price list all shipped with ZERO rows (dry run 2026-08-14), so
    # their deeper flows were unreachable rather than untested and every one of
    # them demoed as an empty shell. scripts/seed_modules.py fills them, is
    # idempotent, and REFUSES unless the deployment profile is 'dev' — which is
    # why the refusal is caught and reported rather than aborting the clinical
    # seed: a prod-profile box should still get its patients if somebody asks for
    # them, it just must never get demo vendors and demo claims.
    print("\n═══ Seeding hospital modules ═══")
    try:
        from scripts.seed_modules import SeedRefused, seed_modules

        try:
            module_stats = seed_modules()
            stats.update(module_stats)
        except SeedRefused as e:
            print(f"  [SKIP] {e}")
    except Exception as e:
        print(f"  x module seed failed: {e}")

    # ── Summary ──
    print(f"\n{'=' * 50}")
    print(f"  Users:    {stats['users']}")
    print(f"  Patients: {stats['patients']} new, {stats['skipped']} skipped")
    print(f"  Channels: {stats['channels']}")
    print(f"  Meds:     {stats['meds']}")
    print(f"  Vitals:   {stats['vitals']}")
    print(f"  Risk:     {stats['risk_scores']}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    seed()
