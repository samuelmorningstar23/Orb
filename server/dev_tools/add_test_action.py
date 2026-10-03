from database import db_session
import uuid

with db_session() as db:
    action_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO sage_actions (id, room_id, patient_id, action_type, target, rationale, confidence)
           VALUES (?, 'case-PAT001', 'PAT001', 'prescribe', 'Aspirin 81mg', 'Chest pain', 0.95)""",
        (action_id,)
    )
    print("Added action:", action_id)
