import os
import sys
import uuid
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure server root is in path
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from config import DB_PATH
from database import _open_raw_connection, _row_factory_for

def seed_chats():
    # Connect to the SQLite database through the app's codec-aware helper: orb.db
    # may be SQLCipher-encrypted at rest, and the stdlib sqlite3 driver cannot
    # open such a file ("file is not a database"). _open_raw_connection() applies
    # PRAGMA key first and still returns a plain sqlite3 connection when no key is
    # set, so the raw defaults this seeder depends on are unchanged — notably
    # foreign_keys OFF, which the sage_actions-before-sage_action_events wipe below
    # requires.
    conn = _open_raw_connection()
    # The Row class must come from whichever driver opened the connection: stdlib
    # sqlite3.Row rejects a SQLCipher cursor with a TypeError.
    conn.row_factory = _row_factory_for(conn)
    
    # 1. Retrieve all patient case rooms
    channels = conn.execute("SELECT * FROM channels WHERE type = 'case_room'").fetchall()
    channel_ids = [c["id"] for c in channels]
    
    if not channel_ids:
        print("No patient case rooms found in the database. Run seed_clinical_data.py first.")
        conn.close()
        return

    print(f"Clearing old messages and suggested actions for {len(channel_ids)} case rooms...")
    
    # Clear old messages and actions in case rooms to prevent duplicate seeding
    placeholders = ",".join("?" for _ in channel_ids)
    conn.execute(f"DELETE FROM messages WHERE channel_id IN ({placeholders})", channel_ids)
    conn.execute(f"DELETE FROM sage_actions WHERE room_id IN ({placeholders})", channel_ids)
    conn.execute("DELETE FROM sage_action_events WHERE action_id NOT IN (SELECT id FROM sage_actions)")
    
    # Check if a nurse user exists
    nurses = conn.execute("SELECT * FROM users WHERE role = 'nurse'").fetchall()
    if not nurses:
        # Insert a default nurse if none exists
        conn.execute(
            """INSERT OR IGNORE INTO users (id, name, role, department, password_hash)
               VALUES ('NUR001', 'Priya Reddy', 'nurse', 'General Medicine', 'password_hash')"""
        )
        nurse_id = 'NUR001'
        nurse_name = 'Priya Reddy'
    else:
        nurse_id = nurses[0]["id"]
        nurse_name = nurses[0]["name"]
        
    print(f"Using Nurse: {nurse_name} ({nurse_id})")
    
    # 2. Iterate through each case room and seed staggered chat messages
    for chan in channels:
        channel_id = chan["id"]
        patient_id = chan["patient_id"]
        
        # Fetch patient profile details
        patient = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if not patient:
            continue
        patient_name = patient["name"]
        diagnosis = patient["diagnosis"] or "Observation"
        doc_id = patient["attending_doctor_id"]
        
        # Fetch chief complaint from active admission
        adm = conn.execute("SELECT chief_complaint FROM admissions WHERE patient_id = ? AND status = 'active'", (patient_id,)).fetchone()
        chief_complaint = adm["chief_complaint"] if adm and adm["chief_complaint"] else "General symptoms"
        
        # Fetch attending doctor info
        doc = conn.execute("SELECT * FROM users WHERE id = ?", (doc_id,)).fetchone()
        if not doc:
            doc_id = 'DOC001'
            doc_name = 'Dr. Meera Sharma'
        else:
            doc_name = doc["name"]
            
        # Get patient's active medications
        meds = conn.execute("SELECT * FROM medications WHERE patient_id = ? AND status = 'active'", (patient_id,)).fetchall()
        
        if not meds:
            # Fallback medication if none is loaded
            med_name = "Paracetamol"
            dosage = "650mg"
            route = "PO"
            frequency = "Q6H"
        else:
            # Select the first medication to discuss in conversation and place in suggestion card
            target_med = meds[0]
            med_name = target_med["drug_name"]
            dosage = target_med["dosage"]
            route = target_med["route"]
            frequency = target_med["frequency"]
            
            # Delete it from medications table so it acts as a pending action in the UI
            conn.execute("DELETE FROM medications WHERE id = ?", (target_med["id"],))
            
        # Stagger message timestamps slightly in the past
        now = datetime.now(timezone.utc)
        t1 = (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        t2 = (now - timedelta(minutes=12)).strftime("%Y-%m-%d %H:%M:%S")
        t3 = (now - timedelta(minutes=8)).strftime("%Y-%m-%d %H:%M:%S")
        t4 = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        
        # Craft realistic hospital conversation thread
        msg1 = f"Reviewing {patient_name} in {patient['ward'] or 'Ward'}. Admitted for {diagnosis} (CC: {chief_complaint}). Vitals are stable, but patient reports discomfort."
        msg2 = f"Thanks for the update. Let's make sure they are comfortable. Any specific requests or pain reported?"
        msg3 = f"Yes, they mentioned a mild headache and body pain. Should we start them on some medication?"
        msg4 = f"Please prescribe {med_name} {dosage} {route} {frequency} for pain management."
        
        # Insert conversation messages
        conn.execute("INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                     (channel_id, nurse_id, nurse_name, 'nurse', msg1, t1))
        conn.execute("INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                     (channel_id, doc_id, doc_name, 'doctor', msg2, t2))
        conn.execute("INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                     (channel_id, nurse_id, nurse_name, 'nurse', msg3, t3))
        
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (channel_id, sender_id, sender_name, sender_role, content, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                       (channel_id, doc_id, doc_name, 'doctor', msg4, t4))
        msg4_id = cursor.lastrowid
        
        # Insert a suggested Sage action matching the conversation
        action_id = str(uuid.uuid4())
        target_str = f"{med_name} {dosage} {route} {frequency}"
        conn.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, source_msg_id, created_at, updated_at)
               VALUES (?, ?, ?, 'prescribe', ?, ?, 0.95, 'suggested', ?, ?, ?)""",
            (action_id, channel_id, patient_id, target_str, f"Extracted from instruction by {doc_name} in case chat.", msg4_id, t4, t4)
        )
        
        # Insert suggested audit event log entry
        conn.execute(
            """INSERT INTO sage_action_events (action_id, prior_state, new_state, actor_id, actor_name, actor_role, reason, source, created_at)
               VALUES (?, NULL, 'suggested', 'system', 'Orb Sage', 'AI', 'Action extracted from room context', 'system', ?)""",
            (action_id, t4)
        )
        
        # Update room summary/memory
        summary = f"{patient_name} admitted for {diagnosis}. Attending: {doc_name}."
        discussion = f"Nurse Priya reported patient discomfort. {doc_name} instructed prescribing {med_name}."
        conn.execute(
            """INSERT OR REPLACE INTO sage_room_memory 
               (room_id, patient_id, patient_summary, discussion_summary, current_concern, risk_flags, participants, message_count, llm_call_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, '[]', ?, 4, 1, ?, ?)""",
            (channel_id, patient_id, summary, discussion, f"Prescribe {med_name}", json.dumps([nurse_name, doc_name]), t4, t4)
        )
        
        print(f"  ✓ Seeded room: {chan['name']} with suggested card for {med_name}")
        
    conn.commit()
    conn.close()
    print("\nSuccessfully seeded Relay chats and matching Sage suggested cards!")

if __name__ == "__main__":
    seed_chats()
