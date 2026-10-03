from database import db_session
with db_session() as db:
    room = db.execute("SELECT id FROM channels WHERE patient_id IS NOT NULL LIMIT 1").fetchone()
    print("ROOM_ID:", room["id"])
