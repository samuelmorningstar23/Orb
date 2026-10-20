import asyncio
import json
import sys
import uuid
from pathlib import Path

# Add server dir to path
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from database import db_session  # noqa: E402 - application imports require the path/environment bootstrap above.
from services.sage_handoff_service import create_and_dispatch_handoff  # noqa: E402 - application imports require the path/environment bootstrap above.


async def test_transfer_flow():
    print("═══ Starting Surgical Transfer Flow E2E Test ═══")

    # Connect and find Ananya Kapoor's patient ID and channel ID
    with db_session() as db:
        patient = db.execute(
            "SELECT id, name FROM patients WHERE name = 'Ananya Kapoor'"
        ).fetchone()
        if not patient:
            print("❌ Ananya Kapoor not found in database. Run reset_db_and_seed.py first.")
            sys.exit(1)
        patient_id = patient["id"]

        channel = db.execute(
            "SELECT id FROM channels WHERE patient_id = ? AND type = 'case_room'", (patient_id,)
        ).fetchone()
        channel_id = channel["id"] if channel else None

        if not channel_id:
            print(f"❌ Case room for Ananya Kapoor ({patient_id}) not found.")
            sys.exit(1)

        print(f"Found Patient: {patient['name']} ({patient_id})")
        print(f"Found Channel: {channel_id}")

        # Verify initial department in admission
        adm = db.execute(
            "SELECT id, department FROM admissions WHERE patient_id = ? AND status = 'active'",
            (patient_id,),
        ).fetchone()
        if not adm:
            print(f"❌ No active admission found for {patient_id}")
            sys.exit(1)
        print(f"Initial Admission Department: {adm['department']}")

        # Create a mock suggested transfer action
        action_id = str(uuid.uuid4())
        action_dict = {
            "id": action_id,
            "room_id": channel_id,
            "patient_id": patient_id,
            "action_type": "transfer",
            "target": "General Surgery",
            "rationale": "Patient condition deteriorating. Transfer to General Surgery for emergency Hartmann's procedure.",
            "confidence": 0.98,
            "state": "confirmed",
            "version": 1,
        }

        db.execute(
            """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence, state, version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                action_dict["id"],
                action_dict["room_id"],
                action_dict["patient_id"],
                action_dict["action_type"],
                action_dict["target"],
                action_dict["rationale"],
                action_dict["confidence"],
                action_dict["state"],
                action_dict["version"],
            ),
        )
        print(f"Created suggested action {action_id}")

    print("\n--- Dispatching Handoff ---")
    handoff = create_and_dispatch_handoff(action_dict, "DOC004")
    print(f"Handoff Dispatch Status: {handoff.get('status')}")
    print(f"Target Module: {handoff.get('target_module')}")
    surgery_id = handoff.get("downstream_ref_id")
    print(f"Created Surgery ID: {surgery_id}")

    # Verify updated admission department
    with db_session() as db:
        updated_adm = db.execute(
            "SELECT department FROM admissions WHERE id = ?", (adm["id"],)
        ).fetchone()
        print(f"Updated Admission Department: {updated_adm['department']}")
        if updated_adm["department"] != "General Surgery":
            print("❌ Department update failed.")
            sys.exit(1)
        print("✅ Department updated to General Surgery successfully!")

        # Verify initial surgery status
        surg = db.execute(
            "SELECT status, risk_assessment, surgical_checklist FROM surgeries WHERE id = ?",
            (surgery_id,),
        ).fetchone()
        print(f"Initial Surgery Status: {surg['status']}")

    # Poll database to observe the background progressive hydration task
    print("\n--- Polling Progressive Hydration ---")
    start_time = asyncio.get_event_loop().time()
    success = False

    while asyncio.get_event_loop().time() - start_time < 6.0:
        # Give the background task time to run
        await asyncio.sleep(1.0)

        with db_session() as db:
            surg = db.execute(
                "SELECT status, risk_assessment, surgical_checklist FROM surgeries WHERE id = ?",
                (surgery_id,),
            ).fetchone()
            print(
                f"[{int(asyncio.get_event_loop().time() - start_time)}s] Surgery Status: {surg['status']}"
            )
            if surg["status"] == "emergency_ready":
                print("\n✅ Progressive hydration completed successfully!")
                print(f"Risk Assessment Preview:\n{surg['risk_assessment'][:250]}...")
                checklist = json.loads(surg["surgical_checklist"])
                print(f"Surgical Checklist items count: {len(checklist)}")
                print("Checklist items:")
                for item in checklist[:3]:
                    print(f"  - {item}")
                success = True
                break

    # Clean up the test action and surgery to keep DB clean
    with db_session() as db:
        db.execute("DELETE FROM surgeries WHERE id = ?", (surgery_id,))
        db.execute(
            "DELETE FROM sage_handoff_events WHERE handoff_id IN (SELECT id FROM sage_handoffs WHERE source_action_id = ?)",
            (action_id,),
        )
        db.execute("DELETE FROM sage_handoffs WHERE source_action_id = ?", (action_id,))
        db.execute("DELETE FROM sage_action_events WHERE action_id = ?", (action_id,))
        db.execute("DELETE FROM sage_actions WHERE id = ?", (action_id,))
        db.execute(
            "UPDATE admissions SET department = ? WHERE id = ?", (adm["department"], adm["id"])
        )

    if success:
        print("\n═══ E2E Test Completed Successfully! ═══")
    else:
        print("\n❌ E2E Test Failed: Progressive hydration did not complete.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(test_transfer_flow())
