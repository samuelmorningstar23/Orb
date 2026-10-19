"""Orb Scribe — Clinical Documentation Routes

Exposes Scribe pipeline endpoints: transcription, structuring (with optional
async verification), note retrieval, retry, and pipeline configuration.
"""
import asyncio
import importlib.util
import os
import json
import logging
from contextlib import aclosing
import time
import uuid
from typing import Optional
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from permissions import department_scope, enforce_patient_scope, log_phi_read, require_permission
from services.scribe_service import (
    transcribe_audio, structure_transcript, structure_transcript_stream,
    save_scribe_note, get_scribe_notes, build_whisper_hints,
    verify_note, build_pipeline_meta,
    update_scribe_note_verification, get_scribe_note_by_id,
    update_scribe_note_content, sign_scribe_note, reject_scribe_note,
    create_revision_from_signed, record_unverified_ack, WORKER_SCRIPT,
)
from services.scribe_providers import StageTimer
from services.audit_service import AuditWriteError, log_audit_event
from services.admission_service import (
    DOOR_SCRIBE_QUICK_ADMIT,
    AdmissionInputError,
    AdmissionRefused,
    admission_choices,
    admit_patient,
    attending_doctors,
    resolve_admission_department,
)
from services.clinical_records_service import allergy_entries
from services.patient_name_rule import PatientNameError
from database import run_db
from config import UPLOAD_DIR, SCRIBE

# Scribe's model routes (transcription, drafting and the second pass, and the
# speech engine's status) answer 404 "Switched off on this computer" while the
# models are switched off (ORB_MODELS_ENABLED, or the Orb Ward Record). Reading,
# editing and signing a note stay open.
from services.model_switch import require_models_on

router = APIRouter(prefix="/api/scribe", tags=["scribe"])
logger = logging.getLogger("orb.scribe")

# Upload size cap for audio (memory-exhaustion DoS guard; mirrors Lens's image cap).
MAX_AUDIO_BYTES = 100 * 1024 * 1024  # 100 MB


# ═══════════════════════════════════════════════════
# Verification State Constants
# ═══════════════════════════════════════════════════

VERIFY_NOT_REQUESTED = "not_requested"
VERIFY_PENDING = "pending"
VERIFY_COMPLETE = "complete"
VERIFY_FAILED = "failed"

# Valid terminal states — verification cannot transition out of these
_TERMINAL_STATES = {VERIFY_COMPLETE, VERIFY_FAILED}

# Stale-pending threshold: notes pending longer than this are marked failed on startup
STALE_PENDING_THRESHOLD_MINUTES = 10


# ═══════════════════════════════════════════════════
# In-Process Verification Registry
# ═══════════════════════════════════════════════════
# Tracks note_ids with active verification tasks. Prevents duplicate dispatch
# and enables lifecycle observation. This is an in-process guard, not a
# distributed lock — adequate for single-server Orb deployments.

_verification_registry: dict[int, asyncio.Task] = {}


def _is_verification_in_flight(note_id: int) -> bool:
    """Check if a verification task is currently running for this note."""
    task = _verification_registry.get(note_id)
    if task is None:
        return False
    if task.done():
        # Stale entry — clean it up
        _verification_registry.pop(note_id, None)
        return False
    return True


def _register_verification_task(note_id: int, task: asyncio.Task):
    """Register a verification task, preventing duplicates."""
    _verification_registry[note_id] = task

    def _cleanup(t: asyncio.Task):
        _verification_registry.pop(note_id, None)

    task.add_done_callback(_cleanup)


# ═══════════════════════════════════════════════════
# Background Verification Task
# ═══════════════════════════════════════════════════

async def _run_verification_async(
    note_id: int,
    transcript: str,
    structured_note: str,
    draft_timer: StageTimer,
    patient_id: str | None = None,
    actor_id: str | None = None,
):
    """Background coroutine: runs verification and persists results.

    Called via asyncio.create_task() after the SSE stream has already sent the
    done event. Exceptions are caught — they never propagate to the caller.
    """
    correlation_id = f"scribe-verify-{note_id}"
    verify_start = time.time()
    started_at = datetime.now(timezone.utc).isoformat()

    # Audit: verification_started
    await run_db(          # off the loop; background task, so awaiting costs no response latency
        log_audit_event,
        correlation_id=correlation_id,
        event_category="scribe",
        action_type="verification_started",
        outcome="info",
        summary_message=f"Background verification started for note {note_id}",
        actor_user_id=actor_id,
        patient_id=patient_id,
        target_type="scribe_note",
        target_id=str(note_id),
        metadata={
            "model": SCRIBE["verify"]["model"],
            "note_id": note_id,
            "started_at": started_at,
        },
    )

    try:
        verification = await verify_note(transcript, structured_note,
                                         patient_id=patient_id, user_id=actor_id)
        elapsed = round(time.time() - verify_start, 3)
        completed_at = datetime.now(timezone.utc).isoformat()

        if verification is not None:
            verification["status"] = VERIFY_COMPLETE
            verification["started_at"] = started_at
            verification["completed_at"] = completed_at
            verification["elapsed_s"] = elapsed

            # Record verification timing in the pipeline meta
            draft_timer.timings["verify"] = elapsed
            meta = build_pipeline_meta(draft_timer)

            await run_db(          # off the loop; background task, so awaiting costs no response latency
                update_scribe_note_verification,
                note_id=note_id,
                verification_json=json.dumps(verification),
                pipeline_meta_json=json.dumps(meta),
            )

            # Audit: verification_completed
            await run_db(          # off the loop; background task, so awaiting costs no response latency
                log_audit_event,
                correlation_id=correlation_id,
                event_category="scribe",
                action_type="verification_completed",
                outcome="success",
                summary_message=f"Verification complete for note {note_id}, verified={verification.get('verified')}, confidence={verification.get('confidence')}",
                actor_user_id=actor_id,
                patient_id=patient_id,
                target_type="scribe_note",
                target_id=str(note_id),
                metadata={
                    "note_id": note_id,
                    "verified": verification.get("verified"),
                    "confidence": verification.get("confidence"),
                    "warnings_count": len(verification.get("warnings", [])),
                    "omissions_count": len(verification.get("omissions", [])),
                    "elapsed_s": elapsed,
                    "model": SCRIBE["verify"]["model"],
                },
            )
        else:
            # Verification returned None (model gave empty/unparseable output)
            elapsed = round(time.time() - verify_start, 3)
            failed_payload = {
                "status": VERIFY_FAILED,
                "error": "Verification model returned empty or unparseable output",
                "started_at": started_at,
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": elapsed,
            }
            await run_db(          # off the loop; background task, so awaiting costs no response latency
                update_scribe_note_verification,
                note_id=note_id,
                verification_json=json.dumps(failed_payload),
            )

            await run_db(          # off the loop; background task, so awaiting costs no response latency
                log_audit_event,
                correlation_id=correlation_id,
                event_category="scribe",
                action_type="verification_failed",
                outcome="failure",
                summary_message=f"Verification returned None for note {note_id}",
                actor_user_id=actor_id,
                patient_id=patient_id,
                target_type="scribe_note",
                target_id=str(note_id),
                metadata={"note_id": note_id, "elapsed_s": elapsed, "model": SCRIBE["verify"]["model"]},
            )

    except Exception as e:
        elapsed = round(time.time() - verify_start, 3)
        logger.exception("Scribe verification failed", extra={"event": "scribe_verification_failed"})
        failed_payload = {
            "status": VERIFY_FAILED,
            "error": "Verification could not be completed. Please retry.",
            "code": "scribe_verification_failed",
            "started_at": started_at,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": elapsed,
        }
        await run_db(          # off the loop; background task, so awaiting costs no response latency
            update_scribe_note_verification,
            note_id=note_id,
            verification_json=json.dumps(failed_payload),
        )

        # Audit: verification_failed
        await run_db(          # off the loop; background task, so awaiting costs no response latency
            log_audit_event,
            correlation_id=correlation_id,
            event_category="scribe",
            action_type="verification_failed",
            outcome="failure",
            summary_message=f"Background verification failed for note {note_id}",
            actor_user_id=actor_id,
            patient_id=patient_id,
            target_type="scribe_note",
            target_id=str(note_id),
            metadata={"note_id": note_id, "elapsed_s": elapsed, "error_type": type(e).__name__, "model": SCRIBE["verify"]["model"]},
        )


# ═══════════════════════════════════════════════════
# Startup Recovery: Reconcile Orphaned Pending Notes
# ═══════════════════════════════════════════════════

def reconcile_stale_pending_verifications():
    """Mark notes stuck in 'pending' as 'failed' on server startup.

    If the server crashed or restarted while a verification was in flight,
    the note remains in 'pending' forever. This function scans for notes
    whose verification_json contains status=pending and whose created_at
    is older than the stale threshold, and transitions them to 'failed'.

    Called once during app lifespan startup (non-async, synchronous DB).
    """
    from database import get_db
    try:
        db = get_db()
        # SQLite JSON: look for verification_json containing "pending"
        rows = db.execute(
            """SELECT id, verification_json, created_at FROM scribe_notes
               WHERE verification_json LIKE '%"pending"%'
               ORDER BY created_at DESC""",
        ).fetchall()
        db.close()

        if not rows:
            return 0

        reconciled = 0
        threshold = datetime.now(timezone.utc) - timedelta(minutes=STALE_PENDING_THRESHOLD_MINUTES)

        for row in rows:
            try:
                v = json.loads(row["verification_json"]) if row["verification_json"] else {}
            except (json.JSONDecodeError, TypeError):
                v = {}

            if v.get("status") != VERIFY_PENDING:
                continue

            # Check if the note is old enough to be considered stale
            created_str = row["created_at"]
            if created_str:
                try:
                    # SQLite CURRENT_TIMESTAMP is UTC without timezone info
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created > threshold:
                        # Too recent — might still be in flight from a previous process
                        continue
                except (ValueError, TypeError):
                    pass  # Can't parse timestamp — mark as stale

            failed_payload = json.dumps({
                "status": VERIFY_FAILED,
                "error": "Server restarted before verification completed (orphaned pending)",
                "failed_at": datetime.now(timezone.utc).isoformat(),
            })
            update_scribe_note_verification(note_id=row["id"], verification_json=failed_payload)
            reconciled += 1

            log_audit_event(
                correlation_id=f"scribe-recovery-{row['id']}",
                event_category="scribe",
                action_type="verification_reconciled",
                outcome="info",
                summary_message=f"Stale pending verification for note {row['id']} marked as failed on startup",
                target_type="scribe_note",
                target_id=str(row["id"]),
            )

        if reconciled > 0:
            print(f"  ✓ Scribe: reconciled {reconciled} stale pending verification(s)")
        return reconciled

    except Exception as e:
        print(f"  ⚠ Scribe: stale-pending reconciliation failed (non-fatal): {e}")
        return 0


# ═══════════════════════════════════════════════════
# Transcription Endpoints
# ═══════════════════════════════════════════════════

@router.post("/transcribe", dependencies=[Depends(require_models_on)])
async def transcribe(
    audio: UploadFile = File(...),
    hints: str = Form(default=""),
    skip_correction: str = Form(default=""),
    patient_id: str = Form(default=""),
    language: str = Form(default=""),
    user: dict = Depends(require_permission("scribe.create_draft")),
):
    """Upload audio file and get transcription. `language` selects the Whisper
    transcription language (e.g. 'en', 'hi', 'auto'); blank uses the configured default."""
    # SCOPING LAW — the patient is named in a FORM FIELD (2026-08-14).
    # Transcribing "against" a patient is not passive: `build_whisper_hints`
    # below reads that patient's name, diagnosis, active drug names and
    # attending doctor out of the database to bias the decoder — so an unscoped
    # id turned this into a chart read wearing an upload's clothes, and the
    # audio itself is PHI filed under that id. 404, never 403. Unconditional
    # because an empty id (dictation with no patient attached) returns
    # immediately. Blocking, so run_db — this is an `async def`.
    await run_db(enforce_patient_scope, user, patient_id)

    # Auto-enrich hints with patient-specific clinical vocabulary.
    # OFF THE LOOP: build_whisper_hints opens a db_session and this is an
    # `async def`, so calling it directly ran encrypted SQLite reads on the
    # event loop — the exact shape of the three production freezes in
    # docs/WORKLOG.md §3. tests/test_event_loop_discipline.py greps for the
    # blocking symbols BY NAME, so an indirect caller like this one was invisible
    # to it (the file says so itself: "Blocking reached INDIRECTLY … which this
    # test cannot see").
    if patient_id:
        db_hints = await run_db(build_whisper_hints, patient_id)
        if db_hints:
            hints = f"{hints}, {db_hints}" if hints else db_hints

    # Save uploaded file — never trust the client-supplied extension (could be
    # .html/.svg/.js); pick from a server-side audio allowlist.
    _allowed_audio = {"webm", "wav", "mp3", "m4a", "ogg", "flac"}
    _client_ext = (os.path.splitext(audio.filename or "")[1].lstrip(".")).lower()
    file_ext = _client_ext if _client_ext in _allowed_audio else "webm"
    file_name = f"{uuid.uuid4().hex}.{file_ext}"
    file_path = os.path.join(str(UPLOAD_DIR), file_name)

    content = await audio.read()
    # Size cap (memory-exhaustion DoS guard; AES-GCM holds plaintext+ciphertext).
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio exceeds maximum size of {MAX_AUDIO_BYTES // (1024 * 1024)} MB.",
        )
    # Encrypt the persisted PHI audio at rest (passthrough if encryption is off).
    from services.crypto_service import write_encrypted, materialize_plaintext
    write_encrypted(file_path, content)

    try:
        should_correct = skip_correction != "true"
        # Offload the blocking ASR call off the event loop so one transcription
        # doesn't stall every other request on the server. Whisper reads a path,
        # so decrypt to a temp file for the duration of the ASR call.
        def _run_asr():
            with materialize_plaintext(file_path) as read_path:
                return transcribe_audio(
                    read_path, apply_correction=should_correct,
                    hints=hints, language=(language or None),
                )
        transcript = await asyncio.to_thread(_run_asr)
        return {
            "transcript": transcript,
            "audio_path": file_path,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")


@router.post("/transcribe-chunk", dependencies=[Depends(require_models_on)])
async def transcribe_chunk(
    audio: UploadFile = File(...),
    chunk_index: int = Form(default=0),
    hints: str = Form(default=""),
    patient_id: str = Form(default=""),
    user: dict = Depends(require_permission("scribe.create_draft")),
):
    """Transcribe a short audio chunk for real-time display.
    
    The frontend sends audio segments (e.g., every 3-5 seconds).
    Each chunk is transcribed independently and the frontend
    accumulates the results.
    """
    # SCOPING LAW + EVENT LOOP — same two reasons as /transcribe above, and this
    # is the route that pays them every 3-5 seconds while someone dictates.
    await run_db(enforce_patient_scope, user, patient_id)

    # Auto-enrich hints with patient-specific clinical vocabulary (off the loop).
    if patient_id:
        db_hints = await run_db(build_whisper_hints, patient_id)
        if db_hints:
            hints = f"{hints}, {db_hints}" if hints else db_hints

    file_name = f"chunk_{uuid.uuid4().hex}.webm"
    file_path = os.path.join(str(UPLOAD_DIR), file_name)

    content = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(content) < 100:
        # Too small to transcribe (silence or noise)
        return {"text": "", "chunk_index": chunk_index}
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio exceeds maximum size of {MAX_AUDIO_BYTES // (1024 * 1024)} MB.",
        )

    # Encrypt the PHI audio chunk at rest too (0o600), consistent with /transcribe
    # — even though the chunk is deleted immediately, a crash mid-transcription
    # must not orphan cleartext PHI in uploads/. ASR reads via a decrypted temp.
    from services.crypto_service import write_encrypted, materialize_plaintext

    try:
        def _run_chunk_asr():
            # Own the entire file lifetime on the worker. Cancelling the await
            # does not stop its thread: cleanup on the event loop could run
            # before a still-writing worker publishes the file, orphaning PHI.
            try:
                write_encrypted(file_path, content)
                with materialize_plaintext(file_path) as read_path:
                    return transcribe_audio(read_path, apply_correction=False, hints=hints)
            finally:
                try:
                    os.remove(file_path)
                except OSError:
                    pass
        text = await asyncio.to_thread(_run_chunk_asr)
        return {"text": text.strip(), "chunk_index": chunk_index}
    except Exception:
        logger.exception("Scribe audio chunk failed", extra={"event": "scribe_chunk_failed"})
        raise HTTPException(status_code=500, detail="Chunk transcription failed.") from None


# ═══════════════════════════════════════════════════
# Structuring Endpoint (with async verification)
# ═══════════════════════════════════════════════════

@router.post("/structure", dependencies=[Depends(require_models_on)])
async def structure(
    data: dict,
    user: dict = Depends(require_permission("scribe.create_draft")),
):
    """Structure a transcript into a formatted clinical note (SSE streaming).
    
    Verification runs asynchronously after the draft is delivered.
    The client receives the done event immediately with verification_status="pending"
    and can poll GET /api/scribe/notes/{note_id}/verification for updates.
    """
    transcript = data.get("transcript", "")
    format_type = data.get("format", "soap")
    patient_id = data.get("patient_id")
    request_verify = data.get("verify", False)

    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript is required")

    if format_type not in ("soap", "discharge", "handoff"):
        raise HTTPException(status_code=400, detail="Invalid format. Use: soap, discharge, handoff")

    await run_db(enforce_patient_scope, user, patient_id)
    # Capture user context before the generator runs (safe to reference later)
    actor_id = user["id"]

    async def event_stream():
        timer = StageTimer()
        full_response = ""
        try:
            # ── Stage 3: Drafting (streaming) ──
            timer.start("draft")
            async with aclosing(structure_transcript_stream(
                transcript, format_type, patient_id=patient_id, user_id=actor_id,
            )) as tokens:
                async for token in tokens:
                    full_response += token
                    yield f"data: {json.dumps({'content': token})}\n\n"
            timer.stop()
            if not full_response.strip():
                raise RuntimeError("Scribe model returned no draft")

            # ── Determine verification intent ──
            will_verify = request_verify and SCRIBE["verify"]["enabled"]
            verification_status = VERIFY_PENDING if will_verify else VERIFY_NOT_REQUESTED

            # ── Save note immediately (before verification) ──
            initial_verification = json.dumps({"status": verification_status})
            meta = build_pipeline_meta(timer)

            # Off the loop: save_scribe_note opens a SQLCipher session, and this
            # runs inside the SSE generator — i.e. ON the event loop, where the
            # discipline scan cannot follow it through the service call. A note
            # is thousands of transcript characters, so the write is not cheap.
            # Contract unchanged: a failed save still raises, and the `except`
            # below still turns it into the stream's single error event.
            note_id = await run_db(
                save_scribe_note,
                patient_id=patient_id,
                author_id=actor_id,
                raw_transcript=transcript,
                structured_note=full_response,
                note_format=format_type,
                audio_path=data.get("audio_path"),
                verification_json=initial_verification,
                pipeline_meta_json=json.dumps(meta),
                scope_user=user,
            )

            # ── Send done event immediately ──
            done_payload = {
                "done": True,
                "id": note_id,
                "format": format_type,
                "status": "draft",
                "pipeline": meta,
                "verification": {"status": verification_status},
            }
            yield f"data: {json.dumps(done_payload)}\n\n"

            # ── Dispatch background verification (with dedup guard) ──
            if will_verify:
                if _is_verification_in_flight(note_id):
                    # Should never happen for a freshly-created note, but guard anyway
                    pass
                else:
                    task = asyncio.create_task(
                        _run_verification_async(
                            note_id=note_id,
                            transcript=transcript,
                            structured_note=full_response,
                            draft_timer=timer,
                            patient_id=patient_id,
                            actor_id=actor_id,
                        )
                    )
                    _register_verification_task(note_id, task)

        except Exception:
            logger.exception("Scribe drafting stream failed", extra={"event": "scribe_stream_failed"})
            yield f"data: {json.dumps({'error': 'Note drafting failed. Please retry.', 'code': 'scribe_stream_failed', 'terminal': True})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════
# Note Retrieval Endpoints
# ═══════════════════════════════════════════════════

def _note_in_scope_or_404(note_id: int, user: dict) -> dict:
    """Load a note and refuse it unless its patient is on the caller's ward.

    ``scribe_notes.id`` is a plain AUTOINCREMENT integer, so "which note" is not
    a secret — 1..N enumerates the hospital's consultation transcripts. The ward
    check therefore has to happen on the note's PATIENT, resolved server-side
    from the row, not on anything the request carries.

    A missing note and an out-of-ward note both 404, deliberately: a distinct
    status for "exists but not yours" confirms the note and, with it, the
    patient.
    """
    note = get_scribe_note_by_id(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    # A note with no patient_id is an unfiled draft with no admission and so no
    # department; it is scoped to its author, the only ownership it has.
    pid = note.get("patient_id")
    if pid:
        enforce_patient_scope(user, pid)
    elif note.get("author_id") != user.get("id") and department_scope(user) is not None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.get("/notes")
def list_notes(
    request: Request,
    patient_id: str = None,
    status: str = None,
    user: dict = Depends(require_permission("scribe.read_all")),
):
    """Get scribe notes, optionally filtered by patient_id and/or status.

    WARD SCOPING. `patient_id` arrives on the QUERY STRING, which is exactly the
    caller-named scope department_scope() forbids trusting — and being off the
    path rather than in it is the only reason this route survived the first pass
    at the 2026-08-05 PHI leak. Until then any scribe.read_all holder (nurse,
    doctor, admin) could name any patient in the hospital and receive the
    verbatim consultation transcript and the structured note.

    With no patient_id the route is author-scoped ("my drafts") and needs no
    department check: a clinician's own notes are their own on any ward.

    AUDITED. The rows carry verbatim consultation transcripts, and the audit
    middleware only covers writes — so every serve is a fail-closed
    ``phi_access`` row (log_phi_read raises if it cannot persist, and then no
    transcript leaves the process). Same precedent as routes/nurse_routes.py:74.
    """
    if patient_id:
        enforce_patient_scope(user, patient_id)
        log_phi_read(
            request=request,
            user=user,
            action_type="scribe_notes_listed",
            summary_message=f"Scribe notes (verbatim transcripts) listed for patient {patient_id}",
            patient_id=patient_id,
        )
        return get_scribe_notes(patient_id=patient_id, status=status)
    log_phi_read(
        request=request,
        user=user,
        action_type="scribe_notes_listed",
        summary_message=f"Own scribe notes (verbatim transcripts) listed by {user['id']}",
        target_type="user",
        target_id=user["id"],
    )
    return get_scribe_notes(author_id=user["id"], status=status)


@router.get("/notes/{note_id}")
def get_note(note_id: int, request: Request, user: dict = Depends(require_permission("scribe.read_all"))):
    """Get a specific scribe note — the note's own ward only.

    Serves the verbatim consultation transcript, so the read is recorded on the
    hash chain BEFORE the content is returned: ``phi_access`` is fail-closed
    (services/audit_service.py:36) — if the row cannot be persisted,
    log_phi_read raises and no transcript is served.
    """
    note = _note_in_scope_or_404(note_id, user)
    log_phi_read(
        request=request,
        user=user,
        action_type="scribe_note_viewed",
        summary_message=f"Scribe note {note_id} (verbatim transcript + structured note) viewed",
        patient_id=note.get("patient_id"),
        target_type="scribe_note",
        target_id=str(note_id),
    )
    return note


@router.get("/notes/{note_id}/verification")
def get_note_verification(note_id: int, user: dict = Depends(require_permission("scribe.read_all"))):
    """Get verification status for a specific note.

    Lightweight polling endpoint for the frontend to check async verification progress.
    Returns only verification_json and pipeline_meta_json fields.

    Carries the SAME ward check as the heavier sibling above: the verification
    report quotes the note it verified, so guarding only /notes/{id} would leave
    the text reachable through the cheaper call.

    Deliberately NOT per-request audited: the frontend polls this every 3s
    while verification runs, and one encrypted-SQLite audit write per poll is
    the shape blamed for the 2026-07-30 loop stalls (see require_patient_access
    on high-frequency polled reads). The scope check above is the control.
    """
    note = _note_in_scope_or_404(note_id, user)

    return {
        "note_id": note_id,
        "verification": note.get("verification_json"),
        "pipeline": note.get("pipeline_meta_json"),
    }


# ═══════════════════════════════════════════════════
# Retry Verification Endpoint
# ═══════════════════════════════════════════════════

@router.post("/notes/{note_id}/verify", dependencies=[Depends(require_models_on)])
async def retry_verification(
    note_id: int,
    user: dict = Depends(require_permission("scribe.edit_draft")),
):
    """Manually retry verification for a note in 'failed' state.

    Guards:
    - Only works if verification is currently 'failed'
    - Rejects if a verification task is already in flight
    - Requires the note to exist and have both transcript + structured note
    """
    if not SCRIBE["verify"]["enabled"]:
        raise HTTPException(status_code=400, detail="Verification is disabled in server config")

    note = await run_db(_note_in_scope_or_404, note_id, user)

    # Guard: only retry from failed or not_requested states
    current_v = note.get("verification_json") or {}
    current_status = current_v.get("status") if isinstance(current_v, dict) else None

    if current_status == VERIFY_COMPLETE:
        raise HTTPException(status_code=409, detail="Verification already complete")
    if current_status == VERIFY_PENDING:
        raise HTTPException(status_code=409, detail="Verification already in progress")

    # Guard: prevent duplicate dispatch
    if _is_verification_in_flight(note_id):
        raise HTTPException(status_code=409, detail="Verification task already running")

    transcript = note.get("raw_transcript", "")
    structured = note.get("structured_note", "")
    if not transcript or not structured:
        raise HTTPException(status_code=400, detail="Note missing transcript or structured note")

    # Reset to pending — off the loop (SQLCipher write inside an async handler).
    await run_db(
        update_scribe_note_verification,
        note_id=note_id,
        verification_json=json.dumps({"status": VERIFY_PENDING}),
        scope_user=user,
    )

    # Dispatch background task
    timer = StageTimer()
    task = asyncio.create_task(
        _run_verification_async(
            note_id=note_id,
            transcript=transcript,
            structured_note=structured,
            draft_timer=timer,
            patient_id=note.get("patient_id"),
            actor_id=user["id"],
        )
    )
    _register_verification_task(note_id, task)

    await run_db(          # off the loop; background task, so awaiting costs no response latency
        log_audit_event,
        correlation_id=f"scribe-verify-retry-{note_id}",
        event_category="scribe",
        action_type="verification_retry",
        outcome="info",
        summary_message=f"Manual verification retry dispatched for note {note_id}",
        actor_user_id=user["id"],
        patient_id=note.get("patient_id"),
        target_type="scribe_note",
        target_id=str(note_id),
    )

    return {"note_id": note_id, "verification": {"status": VERIFY_PENDING}}


# ═══════════════════════════════════════════════════
# Note Lifecycle Endpoints
# ═══════════════════════════════════════════════════

@router.patch("/notes/{note_id}")
def edit_note(
    note_id: int,
    data: dict,
    user: dict = Depends(require_permission("scribe.edit_draft")),
):
    """Edit draft note content. Only drafts can be edited."""
    structured_note = data.get("structured_note")
    if not structured_note:
        raise HTTPException(status_code=400, detail="structured_note is required")

    note = _note_in_scope_or_404(note_id, user)

    result = update_scribe_note_content(note_id, structured_note, user["id"], scope_user=user)
    if result.get("error"):
        raise HTTPException(status_code=409, detail=result["error"])

    log_audit_event(
        correlation_id=f"scribe-edit-{note_id}",
        event_category="scribe",
        action_type="scribe_draft_edited",
        outcome="success",
        summary_message=f"Draft note {note_id} edited by {user['id']}",
        actor_user_id=user["id"],
        patient_id=note.get("patient_id"),
        target_type="scribe_note",
        target_id=str(note_id),
        metadata={"note_id": note_id, "previous_status": "draft", "new_status": "draft"},
    )

    return {"ok": True, "note_id": note_id}


def _verification_verdict(note: dict) -> dict:
    """Derive the sign-gate verdict from a note's persisted verification.

    Returns {blocking: bool, status: str, reasons: [str]}. Blocking is True when
    the automated hallucination check did NOT pass — verified is false, the
    status is 'unverifiable', or any high-severity warning/omission is present.
    A note with no verification record is treated as blocking (fail-closed): an
    unchecked AI note should not be one-click signed either.
    """
    v = note.get("verification_json")
    if not isinstance(v, dict):
        return {"blocking": True, "status": "unverified", "reasons": ["no verification on record"]}

    reasons: list[str] = []
    status = str(v.get("verification_status") or v.get("status") or "unknown")
    if v.get("verified") is not True:
        reasons.append(f"verified={v.get('verified')}")
    if status == "unverifiable":
        reasons.append("verification could not complete")
    for bucket in ("warnings", "omissions"):
        for item in (v.get(bucket) or []):
            sev = str((item or {}).get("severity", "")).lower() if isinstance(item, dict) else ""
            if sev in ("high", "critical"):
                reasons.append(f"{bucket[:-1]}: {(item or {}).get('type', sev)}")
    return {"blocking": bool(reasons), "status": status, "reasons": reasons}


@router.post("/notes/{note_id}/sign")
def sign_note(
    note_id: int,
    body: Optional[dict] = None,
    user: dict = Depends(require_permission("scribe.sign")),
):
    """Sign/finalize a draft note. Doctor-only. Transitions draft → signed.

    SIGN-GATE: if the note's persisted hallucination-verification verdict is
    NOT verified (verified=false / status 'unverifiable' / any high-severity
    warning), the note cannot be one-click signed. The signer must explicitly
    acknowledge the unverified verdict by POSTing {"acknowledge_unverified": true}.
    This prevents a clinician from silently finalizing an AI note that failed the
    hallucination check.
    """
    note = _note_in_scope_or_404(note_id, user)

    body = body or {}
    acknowledged = bool(body.get("acknowledge_unverified"))
    verdict = _verification_verdict(note)
    if verdict["blocking"] and not acknowledged:
        raise HTTPException(
            status_code=428,  # Precondition Required
            detail={
                "error": "verification_unconfirmed",
                "message": "This note did not pass automated hallucination verification. "
                           "Re-signing requires explicit acknowledgement.",
                "verification_status": verdict["status"],
                "reasons": verdict["reasons"],
                "requires": "acknowledge_unverified",
            },
        )

    if verdict["blocking"] and acknowledged:
        # Persist WHO accepted the unverified verdict and WHEN on the note row
        # itself, BEFORE the status flips to signed. The audit metadata below
        # records the acknowledgement too, but the signed note is the clinical
        # record and must carry it without a trail lookup. Fail-closed: if the
        # acknowledgement cannot be persisted, the note is not signed.
        # Its OWN columns, not a key inside verification_json: that blob is
        # rewritten wholesale by the verifier and by retry_verification, and is
        # copied onto revision drafts — an attestation stored there could be
        # erased or inherited. `reasons` goes on the hash chain below, which is
        # where an immutable record of WHY the override was needed belongs.
        if not record_unverified_ack(
            note_id, user["id"], datetime.now(timezone.utc).isoformat(), scope_user=user
        ):
            raise HTTPException(
                status_code=500,
                detail="Could not persist the unverified-sign acknowledgement. The note was NOT signed.",
            )

    previous_status = note.get("status", "draft")
    result = sign_scribe_note(note_id, user["id"], scope_user=user)
    if result.get("error"):
        raise HTTPException(status_code=409, detail=result["error"])

    log_audit_event(
        correlation_id=f"scribe-sign-{note_id}",
        event_category="scribe",
        action_type="scribe_draft_signed",
        outcome="success",
        summary_message=f"Note {note_id} signed by {user['id']}",
        actor_user_id=user["id"],
        patient_id=note.get("patient_id"),
        target_type="scribe_note",
        target_id=str(note_id),
        metadata={
            "note_id": note_id,
            "previous_status": previous_status,
            "new_status": "signed",
            "signer_id": user["id"],
            "version": note.get("version", 1),
            "verification_status": verdict["status"],
            "signed_over_unverified": verdict["blocking"],
            "acknowledged_unverified": verdict["blocking"] and acknowledged,
            "verification_reasons": verdict["reasons"],
        },
    )

    # If signing superseded another note, audit that too
    superseded_id = result.get("superseded_note_id")
    if superseded_id:
        log_audit_event(
            correlation_id=f"scribe-supersede-{superseded_id}",
            event_category="scribe",
            action_type="scribe_note_superseded",
            outcome="success",
            summary_message=f"Note {superseded_id} superseded by note {note_id}",
            actor_user_id=user["id"],
            patient_id=note.get("patient_id"),
            target_type="scribe_note",
            target_id=str(superseded_id),
            metadata={
                "superseded_note_id": superseded_id,
                "superseding_note_id": note_id,
                "previous_status": "signed",
                "new_status": "superseded",
            },
        )

    return {"ok": True, "note_id": note_id, "status": "signed", "superseded_note_id": superseded_id}


@router.post("/notes/{note_id}/reject")
def reject_note(
    note_id: int,
    data: dict,
    user: dict = Depends(require_permission("scribe.reject")),
):
    """Reject a draft note. Doctor-only. Requires a rejection reason."""
    reason = data.get("reason", "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Rejection reason is required")

    note = _note_in_scope_or_404(note_id, user)

    previous_status = note.get("status", "draft")
    result = reject_scribe_note(note_id, user["id"], reason, scope_user=user)
    if result.get("error"):
        raise HTTPException(status_code=409, detail=result["error"])

    log_audit_event(
        correlation_id=f"scribe-reject-{note_id}",
        event_category="scribe",
        action_type="scribe_draft_rejected",
        outcome="success",
        summary_message=f"Note {note_id} rejected by {user['id']}: {reason}",
        actor_user_id=user["id"],
        patient_id=note.get("patient_id"),
        target_type="scribe_note",
        target_id=str(note_id),
        metadata={
            "note_id": note_id,
            "previous_status": previous_status,
            "new_status": "rejected",
            "rejection_reason": reason,
        },
    )

    return {"ok": True, "note_id": note_id, "status": "rejected"}


@router.post("/notes/{note_id}/revise")
def revise_note(
    note_id: int,
    user: dict = Depends(require_permission("scribe.edit_draft")),
):
    """Create a revised draft from a signed note. Original remains signed until revision is signed."""
    note = _note_in_scope_or_404(note_id, user)

    result = create_revision_from_signed(note_id, user["id"], scope_user=user)
    if result.get("error"):
        raise HTTPException(status_code=409, detail=result["error"])

    new_note_id = result["new_note_id"]
    new_version = result["version"]

    log_audit_event(
        correlation_id=f"scribe-revise-{note_id}",
        event_category="scribe",
        action_type="scribe_draft_created",
        outcome="success",
        summary_message=f"Revised draft v{new_version} (note {new_note_id}) created from signed note {note_id}",
        actor_user_id=user["id"],
        patient_id=note.get("patient_id"),
        target_type="scribe_note",
        target_id=str(new_note_id),
        metadata={
            "new_note_id": new_note_id,
            "original_note_id": note_id,
            "version": new_version,
            "supersedes_note_id": note_id,
        },
    )

    return {"ok": True, "new_note_id": new_note_id, "version": new_version, "status": "draft"}


# ═══════════════════════════════════════════════════
# Config Endpoint
# ═══════════════════════════════════════════════════

@router.get("/config")
def get_scribe_config(
    user: dict = Depends(require_permission("admin.overview")),
):
    """Return active Scribe pipeline configuration (admin-only).
    
    Exposes model names, enabled stages, and non-secret parameters.
    """
    return {
        "asr": {
            "model": SCRIBE["asr"]["model"],
            "provider": SCRIBE["asr"]["provider"],
            "batch_size": SCRIBE["asr"]["batch_size"],
        },
        "correction": {
            "enabled": SCRIBE["correction"]["enabled"],
            "model": SCRIBE["correction"]["model"],
            "provider": SCRIBE["correction"]["provider"],
        },
        "draft": {
            "model": SCRIBE["draft"]["model"],
            "provider": SCRIBE["draft"]["provider"],
        },
        "verify": {
            "enabled": SCRIBE["verify"]["enabled"],
            "model": SCRIBE["verify"]["model"],
            "provider": SCRIBE["verify"]["provider"],
        },
    }


# ═══════════════════════════════════════════════════
# ASR Availability Probe
# ═══════════════════════════════════════════════════

def _asr_runtime_importable() -> bool:
    """Can the subprocess worker's ASR runtime import at all? The worker runs on
    sys.executable — this venv — so a spec lookup here answers for it without
    spawning anything or loading a model."""
    return importlib.util.find_spec("lightning_whisper_mlx") is not None


def _asr_model_installed() -> bool:
    """Are the worker's Whisper weights on this appliance? Nothing downloads
    them (Orb is offline), so a runtime without its weights cannot transcribe."""
    from services.asr_model_files import is_installed
    return is_installed(SCRIBE["asr"]["model"], SCRIBE["asr"].get("quant"))


def _asr_daemon_responds() -> bool:
    """True only when the warm Whisper daemon answers its socket (1s budget).
    The daemon is a separate process with its own environment, so it can serve
    transcription even when the runtime is absent from this venv."""
    try:
        from config import VOICE
        if not VOICE["daemon"]["enabled"]:
            return False
        import zmq
        context = zmq.Context()
        sock = context.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 1000)
        sock.setsockopt(zmq.SNDTIMEO, 1000)
        sock.setsockopt(zmq.LINGER, 0)
        try:
            sock.connect(VOICE["daemon"]["socket"])
            sock.send_string(json.dumps({"audio_path": "/dev/null", "hints": "", "language": "en"}))
            sock.recv_string()
            return True
        except zmq.ZMQError:
            return False
        finally:
            sock.close()
            context.term()
    except Exception:
        return False


@router.get("/asr/status", dependencies=[Depends(require_models_on)])
def asr_status(user: dict = Depends(require_permission("scribe.create_draft"))):
    """Honest availability of the transcription engine on THIS appliance.

    The Scribe UI calls this once on mount so an appliance without the ASR
    runtime (the dev box ships without it) renders an explicit "transcription
    engine unavailable" state instead of a generic failure after the clinician
    has already dictated. Cheap by construction: an import-spec lookup, a file
    stat, and — only when those fail — a 1s daemon socket probe.
    """
    provider = SCRIBE["asr"]["provider"]
    reason = None
    if provider == "whisper-mlx":
        runtime_ready = _asr_runtime_importable() and os.path.exists(WORKER_SCRIPT)
        worker_ready = runtime_ready and _asr_model_installed()
        if not worker_ready and not _asr_daemon_responds():
            if runtime_ready:
                reason = (
                    "The speech model is not installed: the Whisper weights for "
                    f"'{SCRIBE['asr']['model']}' are not under server/mlx_models, Orb "
                    "never downloads them, and no warm Whisper daemon is answering. "
                    "Audio cannot be transcribed on this appliance"
                )
            else:
                reason = (
                    "ASR runtime (lightning_whisper_mlx) is not installed in the server "
                    "environment and no warm Whisper daemon is answering. Audio cannot "
                    "be transcribed on this appliance"
                )
    return {
        "available": reason is None,
        "provider": provider,
        "model": SCRIBE["asr"]["model"],
        "reason": reason,
        "runbook": "docs/INSTALL_RUNBOOK.md: F14 (Scribe cannot transcribe)",
    }


# ═══════════════════════════════════════════════════
# Quick Admission (from a structured note)
# ═══════════════════════════════════════════════════
# Until 2026-10-02 the panel's Quick Admission posted to POST /api/vigil/patients,
# which writes a patients row marked 'admitted' with NO admissions row: the
# patient appeared on no ward board, failed closed in every department scope,
# and the modal said "admitted successfully". It also offered three hard-coded
# seed doctor ids. It now goes through admission_service.admit_patient, the same
# transaction as the full admission form, with doctors read from the staff table.

_QUICK_ADMIT_SOURCES = ("er", "clinic", "transfer", "direct")


@router.get("/admission-options")
def quick_admission_options(user: dict = Depends(require_permission("patient.write"))):
    """Who a quick admission may name as attending, the departments it may
    admit to, and the caller's department as the default. The same lists as
    the full admission form (admission_service.admission_choices), so the
    default is the canonical spelling of a covered department or None. Staff
    names and ids and department names only, so no PHI read is logged."""
    choices = admission_choices(user.get("department"))
    return {
        "doctors": choices["doctors"],
        "departments": choices["departments"],
        "default_department": choices["default_department"],
    }


@router.post("/quick-admit")
def quick_admit(data: dict, user: dict = Depends(require_permission("patient.write"))):
    """Admit a patient from a structured note through admit_patient.

    Plain def: admit_patient is a multi-insert transaction and must stay off
    the event loop. Refuses (422) an attending id that is not an active doctor
    in the staff table rather than letting the foreign key fail as a 500, and a
    department no active doctor or nurse covers, which would put the patient on
    nobody's ward board (the same rule as POST /api/admissions/admit).
    """
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    def text(key: str) -> str:
        value = data.get(key)
        return value.strip() if isinstance(value, str) else ""

    department, ward = text("department"), text("ward")
    complaint = text("chief_complaint")
    attending = text("attending_doctor_id")
    source = text("admission_source") or "direct"
    missing = [label for label, value in (("patient name", text("name")),
                                          ("department", department), ("ward", ward),
                                          ("presenting complaint", complaint),
                                          ("attending doctor", attending)) if not value]
    if missing:
        raise HTTPException(status_code=422, detail=f"Required: {', '.join(missing)}.")
    if source not in _QUICK_ADMIT_SOURCES:
        raise HTTPException(status_code=422, detail="Unknown admission source.")
    if attending not in {d["id"] for d in attending_doctors()}:
        raise HTTPException(
            status_code=422,
            detail="The attending doctor is not an active doctor on this system. Choose one from the list.",
        )
    # permissions.department_scope matches admissions.department to
    # users.department exactly, so "cardiology" or "Cardiolgy" admitted a
    # patient onto no board. Stored, shown and audited in the canonical spelling.
    try:
        department = resolve_admission_department(department)
    except AdmissionInputError as e:
        raise HTTPException(status_code=422, detail=str(e))

    age = data.get("age")
    try:
        age = int(age) if age not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Age must be a whole number.")
    sex = text("sex").upper() or None
    if sex not in (None, "M", "F", "O"):
        raise HTTPException(status_code=422, detail="Sex must be M, F or O.")

    payload = {
        "name": data.get("name"),
        "age": age,
        "sex": sex,
        "admission_source": source,
        "admission_type": "inpatient",
        "department": department,
        "ward": ward,
        "bed": text("bed"),
        "attending_doctor_id": attending,
        "chief_complaint": complaint,
        "initial_diagnosis": text("initial_diagnosis") or complaint,
        # A list, as the full form sends it: admit_patient charts each entry as
        # a structured allergy that the prescribing interlock screens against.
        "allergies": allergy_entries(data.get("allergies")),
    }
    import config
    if config.feature_enabled("registration"):
        # The patient index (opd-5b, ORB_REGISTRATION): this door honours
        # ORB_UHID_SOURCE and the duplicate rule exactly as the admission form
        # does, so the hospital number and a duplicate review are read here.
        # With registration off both are ignored, as they always were (this
        # door mints the number).
        payload["mrn"] = text("mrn") or None
        payload["duplicate_review"] = data.get("duplicate_review")
    try:
        result = admit_patient(payload, user, door=DOOR_SCRIBE_QUICK_ADMIT)
    except PatientNameError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AdmissionRefused as e:
        # The patient index or the hospital number rule refused the admission
        # (ORB_REGISTRATION), or the number is already on another record.
        # Nothing was written.
        raise HTTPException(status_code=e.status, detail=e.detail())
    except ValueError as e:
        # Only with ORB_ADT on (adt-5): the bed master refused the typed place
        # (an occupied bed, a bed not recorded clean, a boarder this lane
        # cannot acknowledge), in words, with nothing written.
        from services.adt.core import AdtError, http_detail
        if not isinstance(e, AdtError):
            raise
        raise HTTPException(status_code=e.http_status, detail=http_detail(e))
    except AuditWriteError:
        # Only with ORB_ADT on: the fail-closed patient_movement record could
        # not be written, so the whole admission was rolled back.
        raise HTTPException(status_code=503, detail={
            "error": "audit_unavailable",
            "message": "The record of this admission could not be written, so the patient "
                       "was not admitted. Try again in a moment.",
        })

    log_audit_event(
        correlation_id=f"quick-admit-{result['admission_id']}",
        event_category="clinical_record",
        action_type="patient_admitted",
        outcome="success",
        summary_message=f"Patient {result['patient_id']} admitted to {department} {ward} from Scribe",
        actor_user_id=user["id"],
        patient_id=result["patient_id"],
        target_type="admission",
        target_id=result["admission_id"],
        metadata={"surface": "scribe_quick_admit", "department": department, "ward": ward,
                  "attending_doctor_id": attending,
                  "allergies_charted": len(payload["allergies"])},
    )
    return result


@router.get("/daemon/status", dependencies=[Depends(require_models_on)])
async def daemon_status(
    user: dict = Depends(require_permission("scribe.read_all")),
):
    """Check if the Whisper warm daemon is running and responsive."""
    try:
        from config import VOICE
        if not VOICE["daemon"]["enabled"]:
            return {"status": "disabled", "daemon_enabled": False}

        import zmq
        context = zmq.Context()
        sock = context.socket(zmq.REQ)
        sock.setsockopt(zmq.RCVTIMEO, 1000)
        sock.setsockopt(zmq.SNDTIMEO, 1000)
        sock.setsockopt(zmq.LINGER, 0)
        try:
            sock.connect(VOICE["daemon"]["socket"])
            sock.send_string(json.dumps({"audio_path": "/dev/null", "hints": "", "language": "en"}))
            response = sock.recv_string()
            return {"status": "running", "daemon_enabled": True, "response": json.loads(response)}
        except zmq.Again:
            return {"status": "not_running", "daemon_enabled": True, "error": "Daemon not responding"}
        except zmq.ZMQError as e:
            return {"status": "error", "daemon_enabled": True, "error": str(e)}
        finally:
            sock.close()
            context.term()
    except ImportError:
        return {"status": "error", "error": "pyzmq not installed"}

