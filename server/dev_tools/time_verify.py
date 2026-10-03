import asyncio
import time
import os

os.environ["ORB_SCRIBE_VERIFY_ENABLED"] = "true"

from database import init_db
import json

init_db()

async def measure():
    from services.scribe_service import verify_note
    transcript = "Patient presents with a severe headache. No known allergies."
    structured = """SUBJECTIVE:
Patient presents with a severe headache. No known allergies.

OBJECTIVE:
None.

ASSESSMENT:
Headache.

PLAN:
Follow up if symptoms persist."""
    
    start = time.time()
    v_result = await verify_note(transcript, structured)
    end = time.time()
    
    print(f"Time to verify note: {end - start:.2f}s")
    print(v_result)
    
asyncio.run(measure())
