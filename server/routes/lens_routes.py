"""Orb Lens — Medical Image Analysis Routes"""

import asyncio
import json
import logging
import os
import uuid
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path

import anyio
from config import UPLOAD_DIR
from database import db_session, run_db
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from permissions import (
    department_scope,
    enforce_patient_scope,
    facility_scope,
    log_phi_read,
    require_permission,
    scope_patient_ids_sql,
)
from request_limits import MAX_IMAGE_BYTES
from services.audit_service import log_audit_event
from services.lens_service import (
    LENS_DISCLAIMER_BANNER,
    MAX_REVIEWED_TEXT,
    analyze_image_stream,
    decorate_analysis,
    get_lens_analyses,
    save_lens_analysis,
)

router = APIRouter(prefix="/api/lens", tags=["lens"])
logger = logging.getLogger("orb.lens")


# ── Blocking DB bodies ────────────────────────────────────────────────────────
# Module level, not nested in the async routes: keeping them out here makes it
# mechanically checkable that no `with db_session()` sits inside an `async def`,
# which is the shape that wedged the API on 2026-07-30. Each must stay PURE SYNC
# and is only ever reached through run_db().


def _load_patient_row(patient_id: str):
    with db_session() as db:
        patient = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        return patient


def _normalise_reviewed_text(reviewed_text: str | None) -> str:
    """The findings the clinician confirmed, without the investigational banner
    an older client still sends inside the box (the readers add their own)."""
    reviewed = (reviewed_text or "").strip()
    if _DISCLAIMER_LINE in reviewed:
        reviewed = reviewed.replace(_DISCLAIMER_LINE, "").rstrip(" -\n")
    return reviewed[:MAX_REVIEWED_TEXT]


def _assign_analysis(analysis_id: int, patient_id: str, user: dict, reviewed_text: str):
    """File a reviewed Lens read onto a patient's record and read back what the
    Relay notification needs, all in ONE session. The read-back must not be
    split from the UPDATE by an await: a concurrent filing of the same analysis
    would otherwise let us narrate a row we no longer own.

    The row is filed WITH the text the clinician confirmed, who confirmed it
    and when (migration 45). Before this only patient_id moved, so the chart,
    the discharge timeline, Sage and the patient portal all read the raw model
    output while the reviewed text went to the case room alone.

    Only an UNFILED read can be filed, and only by the clinician who ran it:
    an unfiled row has no patient and therefore no ward, so its author is its
    only owner (the same rule list_analyses applies). A filed row is not moved
    from one patient's record to another's here; the compare-and-swap on
    patient_id IS NULL makes a double click file it once.
    """
    with db_session() as db:
        # Verify patient exists
        patient = db.execute(
            "SELECT id, name, bed FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")

        current = db.execute(
            "SELECT id, patient_id, author_id FROM lens_analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
        if current is None or (
            current["patient_id"] is None and current["author_id"] != user["id"]
        ):
            # 404 for another clinician's unfiled read too: it is theirs alone.
            raise HTTPException(status_code=404, detail="Analysis not found")
        if current["patient_id"] is not None:
            raise HTTPException(
                status_code=409, detail="This read is already filed to a patient record."
            )

        reviewed_at = datetime.now(UTC).isoformat()
        filed = db.execute(
            "UPDATE lens_analyses SET patient_id = ?, reviewed_by = ?, reviewed_at = ?, "
            "reviewed_text = ? WHERE id = ? AND patient_id IS NULL",
            (patient_id, user["id"], reviewed_at, reviewed_text, analysis_id),
        )
        if filed.rowcount == 0:
            raise HTTPException(
                status_code=409, detail="This read is already filed to a patient record."
            )
        # Same transaction as the filing (clinical_access is fail-closed): an
        # AI imaging read never lands on a record without a named reviewer row.
        log_audit_event(
            correlation_id=f"lens-filed-{analysis_id}",
            event_category="clinical_access",
            action_type="lens_read_filed",
            outcome="success",
            db=db,
            summary_message=f"Clinician-reviewed Lens read {analysis_id} filed to the patient record",
            actor_user_id=user["id"],
            actor_role=user.get("role"),
            patient_id=patient_id,
            target_type="lens_analysis",
            target_id=str(analysis_id),
        )

        # Get analysis details for relay notification
        analysis = db.execute("SELECT * FROM lens_analyses WHERE id = ?", (analysis_id,)).fetchone()

        # Post notification to patient's Relay case room
        channel = db.execute(
            "SELECT id FROM channels WHERE type = 'case_room' AND patient_id = ?", (patient_id,)
        ).fetchone()
        patient_name = patient["name"]
        return analysis, channel, patient_name


# The banner without its markdown rule: one line for a chat message.
_DISCLAIMER_LINE = LENS_DISCLAIMER_BANNER.replace("---", "").strip()


def _filed_message(analysis, reviewed_text: str | None, filer_name: str) -> str:
    """The case-room message that files a Lens read. It ALWAYS ends with the
    investigational line, added here rather than trusted to the reviewed text:
    the panel keeps the banner out of the editable box (fixed text instead), a
    clinician can delete anything in that box, and the 1,500-character cut below
    would drop a banner that sat at the end of a long read."""
    reviewed = (reviewed_text or "").strip()
    if _DISCLAIMER_LINE in reviewed:
        # An older client still sends the banner inside the text; say it once.
        reviewed = reviewed.replace(_DISCLAIMER_LINE, "").rstrip(" -\n")
    header = (
        "🩻 Clinician-reviewed AI imaging reference"
        if reviewed
        else "🩻 AI imaging reference (investigational, not a diagnostic read)"
    )
    body = (
        f"{header}: {analysis['image_type'].upper()} scan\n"
        f"Context: {analysis['clinical_context'] or 'N/A'}\n"
        f"Reviewed & filed by {filer_name}"
    )
    if reviewed:
        body += f"\n\nClinician-confirmed findings:\n{reviewed[:1500]}"
    return f"{body}\n\n{_DISCLAIMER_LINE}"


# ── Upload validation (PHI image safety) ──
# Allowlist of accepted image extensions -> a SAFE, server-chosen extension.
# We never trust the client-supplied extension (could be .html/.svg/.js which,
# if served same-origin, becomes stored-XSS). The value is the canonical ext.
_ALLOWED_IMAGE_EXTS = {
    ".jpg": ".jpg",
    ".jpeg": ".jpg",
    ".png": ".png",
    ".webp": ".webp",
    ".gif": ".gif",
    ".bmp": ".bmp",
    ".tif": ".tiff",
    ".tiff": ".tiff",
}
# Magic-byte signatures for the formats we accept.
_IMAGE_MAGIC = [
    (b"\xff\xd8\xff", ".jpg"),  # JPEG
    (b"\x89PNG\r\n\x1a\n", ".png"),  # PNG
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),  # BMP
    (b"II*\x00", ".tiff"),
    (b"MM\x00*", ".tiff"),  # TIFF
]


def _detect_image_ext(data: bytes) -> str | None:
    """Return canonical extension if the bytes match a known image signature.

    WEBP is RIFF....WEBP. All others use a fixed prefix.
    """
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    for sig, ext in _IMAGE_MAGIC:
        if data.startswith(sig):
            return ext
    return None


_ANALYZE_IMAGE_IMAGE_DEFAULT = File(...)
_ANALYZE_IMAGE_CLINICAL_CONTEXT_DEFAULT = Form(default="")
_ANALYZE_IMAGE_PATIENT_ID_DEFAULT = Form(default="")
_ANALYZE_IMAGE_IMAGE_TYPE_DEFAULT = Form(default="other")
_ANALYZE_IMAGE_USER_DEFAULT = Depends(require_permission("lens.analyze"))


@router.post("/analyze")
async def analyze_image(
    image: UploadFile = _ANALYZE_IMAGE_IMAGE_DEFAULT,
    clinical_context: str = _ANALYZE_IMAGE_CLINICAL_CONTEXT_DEFAULT,
    patient_id: str = _ANALYZE_IMAGE_PATIENT_ID_DEFAULT,
    image_type: str = _ANALYZE_IMAGE_IMAGE_TYPE_DEFAULT,
    user: dict = _ANALYZE_IMAGE_USER_DEFAULT,
):
    """Analyze a medical image using vision LLM. Returns streaming response."""
    # ── Demo-only gate ──
    # Lens has no validated imaging model; it is disabled in production unless an
    # operator explicitly opts in, so an unvalidated vision LLM can never silently
    # read PHI images in a real deployment.
    from config import LENS_ENABLED

    if not LENS_ENABLED:
        raise HTTPException(
            status_code=403,
            detail=(
                "Lens image analysis is disabled in this deployment. It has no "
                "validated imaging model and is decision-support reference only. "
                "Set ORB_LENS_ENABLED=true to enable it explicitly."
            ),
        )

    # ── Validate upload: extension allowlist + magic bytes + size cap ──
    # Read the body (FastAPI buffers large uploads to a spooled temp file). Cap
    # the size to prevent memory-exhaustion DoS.
    content = await image.read(MAX_IMAGE_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Empty upload.")
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds maximum size of {MAX_IMAGE_BYTES // (1024 * 1024)} MB.",
        )

    client_ext = os.path.splitext(image.filename or "")[1].lower()
    if client_ext not in _ALLOWED_IMAGE_EXTS:
        raise HTTPException(
            status_code=415,
            detail="Unsupported image type. Allowed: JPG, PNG, WEBP, GIF, BMP, TIFF.",
        )

    # Verify the actual bytes match an image signature (don't trust the filename).
    detected_ext = _detect_image_ext(content)
    if detected_ext is None:
        raise HTTPException(
            status_code=415,
            detail="Upload content is not a recognized image format.",
        )

    await run_db(enforce_patient_scope, user, patient_id)

    # Build patient context if provided
    patient_context = None
    if patient_id:
        patient = await run_db(_load_patient_row, patient_id)
        if patient:
            patient_context = f"Patient: {patient['name']}, Age: {patient['age']}, Sex: {patient['sex']}, Diagnosis: {patient['diagnosis']}"

    # Use a SERVER-CHOSEN safe extension and a FULL uuid (128 bits, not the old
    # brute-forceable 32-bit [:8]) so PHI filenames can't be enumerated.
    filename = f"lens_{uuid.uuid4().hex}{detected_ext}"
    image_path = str(UPLOAD_DIR / filename)

    # Keep ownership through thread cancellation: a worker may finish its
    # atomic publication after the request was cancelled.
    from services.crypto_service import write_encrypted

    write_task = asyncio.create_task(asyncio.to_thread(write_encrypted, image_path, content))
    try:
        await asyncio.shield(write_task)
    except asyncio.CancelledError:
        try:
            with anyio.CancelScope(shield=True):
                await write_task
        finally:
            Path(image_path).unlink(missing_ok=True)
        raise
    except Exception:
        Path(image_path).unlink(missing_ok=True)
        logger.exception("Lens image upload failed", extra={"event": "lens_upload_failed"})
        raise HTTPException(status_code=500, detail="Image upload failed.") from None

    registered = False

    def cleanup_unregistered():
        if not registered:
            Path(image_path).unlink(missing_ok=True)

    async def stream_analysis():
        nonlocal registered
        full_response = ""
        try:
            # The service decrypts and encodes in a worker. JSON token frames
            # preserve newlines and distinguish model prose from control events.
            async with aclosing(
                analyze_image_stream(
                    image_path,
                    clinical_context or None,
                    patient_context,
                    patient_id=patient_id or None,
                    user_id=user["id"],
                )
            ) as chunks:
                async for chunk in chunks:
                    full_response += chunk
                    yield f"data: {json.dumps({'content': chunk})}\n\n"

            if not full_response.strip():
                raise RuntimeError("Vision model returned no analysis")

            # Always unfiled: only a completed read may be saved, and only the
            # separate clinician-review step can file it onto a patient chart.
            save_task = asyncio.create_task(
                run_db(
                    save_lens_analysis,
                    patient_id=None,
                    author_id=user["id"],
                    image_path=image_path,
                    image_type=image_type,
                    clinical_context=clinical_context or None,
                    analysis=full_response,
                )
            )
            try:
                new_id = await asyncio.shield(save_task)
            except asyncio.CancelledError:
                # The DB executor keeps running after caller cancellation.
                # Learn its durable outcome before deciding who owns the file.
                with anyio.CancelScope(shield=True):
                    await save_task
                    registered = True
                raise
            registered = True
            yield f"data: {json.dumps({'analysis_id': new_id})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception:
            # HTTP headers have already been sent. A typed terminal event must
            # fail visibly without exposing paths, prompts or model exceptions.
            logger.exception("Lens analysis stream failed", extra={"event": "lens_stream_failed"})
            yield f"data: {json.dumps({'error': 'Image analysis failed. Please retry.', 'code': 'lens_stream_failed', 'terminal': True})}\n\n"
        finally:
            cleanup_unregistered()

    # Also clean a response whose body iterator was never entered.
    from starlette.background import BackgroundTask

    return StreamingResponse(
        stream_analysis(),
        media_type="text/event-stream",
        background=BackgroundTask(cleanup_unregistered),
    )


_LIST_ANALYSES_USER_DEFAULT = Depends(require_permission("lens.read"))


@router.get("/analyses")
# Plain def on purpose: the db_session and the audit write stay in FastAPI's
# threadpool rather than on the asyncio event loop.
def list_analyses(
    request: Request,
    patient_id: str | None = None,
    user: dict = _LIST_ANALYSES_USER_DEFAULT,
) -> list:
    """Saved Lens analyses — the caller's department only.

    Called with no patient_id this returned the 50 most recent imaging analyses
    in the hospital, on `lens.read` alone (audit 2026-08-05): AI radiology
    findings for patients on any ward, to any nurse or doctor.

    Unassigned analyses (patient_id IS NULL) have no admission and therefore no
    department. They are the upload staging area — an image a clinician has just
    analysed and not yet filed — so instead of being dropped they are scoped to
    their AUTHOR, which is the only ownership an unfiled row has.

    FACILITY SCOPE (multisite-8, W12-A). With more than one facility on record
    a department-less account no longer reads the whole house: like a ward
    account it gets the filed reads of patients in its facility scope
    (permissions.scope_patient_ids_sql with facilities) and its own unfiled
    ones, never another clinician's unfiled read, which has no patient and so
    no facility to check. With one facility nothing changes.
    """
    dept = department_scope(user)
    facilities = facility_scope(user)

    if patient_id:
        # ONE guard for every caller-named patient_id, whether it arrives on the
        # path or (as here) on the query string. 404, not 403 — a 403 would
        # confirm the patient exists.
        enforce_patient_scope(user, patient_id)
        rows = get_lens_analyses(patient_id)
    elif dept is None and facilities is None:
        rows = get_lens_analyses(None)
    else:
        ids_sql, ids_params = scope_patient_ids_sql(dept, facilities=facilities)
        with db_session() as db:
            rows = [
                decorate_analysis(r)
                for r in db.execute(
                    f"""SELECT * FROM lens_analyses
                    WHERE patient_id IN ({ids_sql})
                       OR (patient_id IS NULL AND author_id = ?)
                    ORDER BY created_at DESC LIMIT 50""",
                    ids_params + (user["id"],),
                ).fetchall()
            ]

    if rows:
        log_phi_read(
            request=request,
            user=user,
            action_type="lens_analyses_viewed",
            summary_message=(
                f"Lens analyses viewed ({len(rows)}) for "
                f"{('patient ' + patient_id) if patient_id else (dept or '(all, user has no department on record)')}"
            ),
            patient_id=patient_id,
            target_type="patient" if patient_id else "department",
            target_id=patient_id or dept,
            metadata={"result_count": len(rows)},
        )

    return rows


_ASSIGN_TO_PATIENT_PATIENT_ID_DEFAULT = Form(...)
_ASSIGN_TO_PATIENT_REVIEWED_TEXT_DEFAULT = Form(None)
_ASSIGN_TO_PATIENT_USER_DEFAULT = Depends(require_permission("lens.analyze"))


@router.patch("/analyses/{analysis_id}/assign")
async def assign_to_patient(
    analysis_id: int,
    patient_id: str = _ASSIGN_TO_PATIENT_PATIENT_ID_DEFAULT,
    reviewed_text: str = _ASSIGN_TO_PATIENT_REVIEWED_TEXT_DEFAULT,
    user: dict = _ASSIGN_TO_PATIENT_USER_DEFAULT,
):
    """File a clinician-reviewed lens analysis to a patient's Vigil record."""
    # SCOPING LAW — the patient is named in a FORM FIELD (2026-08-14).
    # This is the write that files an AI imaging reference onto a patient's
    # record AND posts it into their case room; `patient_id` is Form(...), so
    # neither the path guard nor a body-model sweep could see it. 404, never
    # 403; blocking, so run_db.
    await run_db(enforce_patient_scope, user, patient_id)

    # The confirmed findings are required HERE, not only by the panel's
    # checkbox: whatever is filed is what every other reader shows.
    reviewed = _normalise_reviewed_text(reviewed_text)
    if not reviewed:
        raise HTTPException(
            status_code=422,
            detail="Enter the findings you have reviewed and confirm before filing this read.",
        )

    # Use db_session so the connection is always closed / rolled back on error
    # (the previous raw get_db() leaked the connection on the 404 branch).
    analysis, channel, patient_name = await run_db(
        _assign_analysis, analysis_id, patient_id, user, reviewed
    )

    if channel and analysis:
        from services.relay_service import send_message
        from ws.realtime import sio

        # REGULATORY: Lens is an AI imaging REFERENCE for a clinician — not a
        # radiologist read and not a diagnosis. It is filed only after a clinician
        # reviews (and may edit) it, so the record reflects clinician judgement.
        body = _filed_message(analysis, reviewed, user["name"])
        # send_message opens its own db_session: off the loop.
        msg = await run_db(
            send_message,
            channel_id=channel["id"],
            sender_id=user["id"],
            sender_name=user["name"],
            sender_role=user["role"],
            content=body,
            message_type="system",
        )
        await sio.emit("message:new", msg, room=channel["id"])

    return {"status": "ok", "patient_name": patient_name}
