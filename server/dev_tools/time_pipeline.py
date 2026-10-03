import asyncio
import time
from database import init_db
import json

init_db()

async def measure():
    from services.scribe_service import structure_transcript_stream
    transcript = "Patient presents with a severe headache. No known allergies."
    start = time.time()
    
    gen = structure_transcript_stream(
        transcript=transcript,
        format="soap"
    )
    
    first_token_time = None
    chunks = 0
    full_output = ""
    
    async for chunk in gen:
        if first_token_time is None:
            first_token_time = time.time()
        chunks += 1
        full_output += chunk
        
    final_time = time.time()
    
    print(f"Time to first token: {first_token_time - start:.2f}s")
    print(f"Time to final stream finish: {final_time - start:.2f}s")
    print(f"Total chunks: {chunks}")

    from services.scribe_service import verify_note_content
    v_start = time.time()
    v_result = verify_note_content(transcript, full_output, "soap")
    v_end = time.time()
    print(f"Time to verify note: {v_end - v_start:.2f}s")
    
asyncio.run(measure())
