"""Orb — HIS roster import routes (admin-only, audited).

The incumbent HIS at the pilot site exports "whatever it exports" — CSV is the
only format we can count on, and its columns are unknown until the day someone
hands us a file. These endpoints implement the three-step flow that makes that
safe:

  POST /api/admin/roster-import/preview   upload → sniffed delimiter, headers,
                                          SUGGESTED column mapping, sample rows,
                                          and the candidate header lines
  POST /api/admin/roster-import/dry-run   upload + confirmed mapping → the exact
                                          create/update/skip plan for patients AND
                                          admissions + every issue. Touches nothing.
  POST /api/admin/roster-import/commit    same inputs → executes that plan in one
                                          transaction and writes a ledger row.

Two optional form fields carry the admin's answers to the two questions this
importer refuses to guess at:

  header_line          which 1-based line holds the column names. Required only
                       when detection is not confident — reading a preamble line
                       as the header shifts every column, so /dry-run 422s and
                       asks rather than importing off a guess.
  default_department   the clinical service to file every admission under when
                       the export has no department column. Both ward boards
                       filter on admissions.department, so this decides who can
                       see the imported patients.

  GET  /api/admin/roster-import/fields    the target-field catalogue (UI mapping step)
  GET  /api/admin/roster-import/runs      import history

Auth: every endpoint is gated on ``admin.overview`` — the permission held only by
the ``admin`` role, the same gate as admin user management. A doctor cannot bulk
-create patients from a spreadsheet.

Event loop: the handlers are ``async def`` because reading a multipart upload is
async, but ALL parsing and DB work goes through ``database.run_db`` onto the
private DB pool. Nothing that can block runs on the loop (see the standing laws
in the repo README).
"""

from __future__ import annotations

import logging

from database import run_db
from db_backend import DBIntegrityError
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from permissions import ADMIN_OVERVIEW, require_permission
from routes.import_mapping import parse_mapping as _parse_mapping
from services import roster_import
from services.audit_service import log_audit_event


logger = logging.getLogger("orb.roster_import")

router = APIRouter(prefix="/api/admin/roster-import", tags=["roster-import"])


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _read_upload(file: UploadFile) -> bytes:
    """Read the upload with a hard size cap before anything else touches it."""
    content = await file.read(roster_import.MAX_CSV_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > roster_import.MAX_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {roster_import.MAX_CSV_BYTES // (1024 * 1024)} MB limit.",
        )
    return content


def _audit(
    request: Request,
    user: dict,
    action_type: str,
    outcome: str,
    summary: str,
    metadata: dict,
    target_id: str | int | None = None,
) -> None:
    log_audit_event(
        correlation_id=f"roster-import-{target_id or 'dryrun'}-{id(request):x}",
        event_category="data_management",
        action_type=action_type,
        outcome=outcome,
        summary_message=summary,
        actor_user_id=user.get("id"),
        actor_role=user.get("role"),
        target_type="roster_import",
        target_id=str(target_id) if target_id is not None else None,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        metadata=metadata,
    )


# ── Field catalogue ──────────────────────────────────────────────────────────


_GET_FIELDS_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/fields")
def get_fields(user: dict = _GET_FIELDS_USER_DEFAULT):
    """Target fields an import can populate, for the mapping UI."""
    return {
        "fields": roster_import.field_catalogue(),
        "required": list(roster_import.REQUIRED_FIELDS),
        "date_orders": ["auto", "dmy", "mdy"],
        "max_bytes": roster_import.MAX_CSV_BYTES,
        "max_rows": roster_import.MAX_ROWS,
    }


# ── Step 1: preview ──────────────────────────────────────────────────────────


_PREVIEW_FILE_DEFAULT = File(...)
_PREVIEW_HEADER_LINE_DEFAULT = Form(None)
_PREVIEW_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/preview")
async def preview(
    request: Request,
    file: UploadFile = _PREVIEW_FILE_DEFAULT,
    header_line: int | None = _PREVIEW_HEADER_LINE_DEFAULT,
    user: dict = _PREVIEW_USER_DEFAULT,
):
    """Sniff an uploaded roster: delimiter, headers, suggested mapping, samples.

    Reads nothing from the patient table and writes nothing anywhere.

    Answers even when the header row is ambiguous — ``header_confident: false``
    plus ``header_candidates`` is how the admin is asked which line holds the
    column names. Their answer comes back as ``header_line`` on this and on the
    two steps that follow.
    """
    content = await _read_upload(file)
    try:
        result = await run_db(roster_import.sniff, content, header_line=header_line)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    result["filename"] = file.filename
    return result


# ── Step 2: dry run ──────────────────────────────────────────────────────────


_DRY_RUN_FILE_DEFAULT = File(...)
_DRY_RUN_MAPPING_DEFAULT = Form(...)
_DRY_RUN_DATE_ORDER_DEFAULT = Form("auto")
_DRY_RUN_UPDATE_EXISTING_DEFAULT = Form(True)
_DRY_RUN_HEADER_LINE_DEFAULT = Form(None)
_DRY_RUN_DEFAULT_DEPARTMENT_DEFAULT = Form(None)
_DRY_RUN_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/dry-run")
async def dry_run(
    request: Request,
    file: UploadFile = _DRY_RUN_FILE_DEFAULT,
    mapping: str = _DRY_RUN_MAPPING_DEFAULT,
    date_order: str = _DRY_RUN_DATE_ORDER_DEFAULT,
    update_existing: bool = _DRY_RUN_UPDATE_EXISTING_DEFAULT,
    header_line: int | None = _DRY_RUN_HEADER_LINE_DEFAULT,
    default_department: str | None = _DRY_RUN_DEFAULT_DEPARTMENT_DEFAULT,
    user: dict = _DRY_RUN_USER_DEFAULT,
) -> dict:
    """Report exactly what an import WOULD create, update and skip. Writes nothing.

    Also reports what would happen to each patient's ADMISSION — the row both
    ward boards join to — and, under ``absent_from_roster``, which patients Orb
    still holds on these wards but this file does not mention. Nothing in that
    list is acted on; see the service docstring for why absence is not discharge.

    422s rather than guessing when the header row cannot be identified: the
    admin re-sends with ``header_line``.
    """
    content = await _read_upload(file)
    parsed_mapping = _parse_mapping(mapping)
    try:
        result = await run_db(
            roster_import.plan,
            content,
            parsed_mapping,
            date_order=date_order,
            update_existing=update_existing,
            header_line=header_line,
            default_department=default_department,
            # multisite-4: whose facilities each row is checked against, read
            # only while more than one facility is on record.
            actor_user_id=user["id"],
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    result["applied"] = False
    result["filename"] = file.filename
    _audit(
        request,
        user,
        "roster_import_dry_run",
        "success",
        f"Roster import dry run over '{file.filename}': {result['counts']}",
        {
            "filename": file.filename,
            "counts": result["counts"],
            "mapping": parsed_mapping,
            "date_order": date_order,
            "header_line": result.get("header_line"),
            "default_department": default_department,
        },
    )
    return result


# ── Step 3: commit ───────────────────────────────────────────────────────────


_COMMIT_FILE_DEFAULT = File(...)
_COMMIT_MAPPING_DEFAULT = Form(...)
_COMMIT_DATE_ORDER_DEFAULT = Form("auto")
_COMMIT_UPDATE_EXISTING_DEFAULT = Form(True)
_COMMIT_NOTE_DEFAULT = Form(None)
_COMMIT_HEADER_LINE_DEFAULT = Form(None)
_COMMIT_DEFAULT_DEPARTMENT_DEFAULT = Form(None)
_COMMIT_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.post("/commit")
async def commit(
    request: Request,
    file: UploadFile = _COMMIT_FILE_DEFAULT,
    mapping: str = _COMMIT_MAPPING_DEFAULT,
    date_order: str = _COMMIT_DATE_ORDER_DEFAULT,
    update_existing: bool = _COMMIT_UPDATE_EXISTING_DEFAULT,
    note: str | None = _COMMIT_NOTE_DEFAULT,
    header_line: int | None = _COMMIT_HEADER_LINE_DEFAULT,
    default_department: str | None = _COMMIT_DEFAULT_DEPARTMENT_DEFAULT,
    user: dict = _COMMIT_USER_DEFAULT,
):
    """Apply the import. Idempotent: re-running the same file changes nothing.

    The plan is recomputed server-side from the uploaded bytes rather than taken
    from the client, so a commit can never write something the dry run did not
    show for the same file + mapping.

    Writes the ADMISSION alongside the patient, in the same transaction. That is
    not an optional extra: a patient row with no open admission is invisible to
    Vigil and to the nurse board, so committing one without the other would be a
    half-import reporting success.
    """
    content = await _read_upload(file)
    parsed_mapping = _parse_mapping(mapping)
    try:
        result = await run_db(
            roster_import.apply_import,
            content,
            parsed_mapping,
            actor_user_id=user["id"],
            source_filename=file.filename,
            date_order=date_order,
            update_existing=update_existing,
            note=note,
            header_line=header_line,
            default_department=default_department,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except DBIntegrityError as e:
        # The whole import is one transaction, so nothing was written. Report the
        # constraint rather than a bare 500 — it is almost always a duplicate MRN
        # that appeared between the dry run and the commit.
        logger.warning("Roster import rejected by a DB constraint: %s", e)
        raise HTTPException(
            status_code=409,
            detail=f"Import rejected by a database constraint and NOTHING was written: {e}",
        ) from e

    _audit(
        request,
        user,
        "roster_import_committed",
        "success",
        f"Roster import '{file.filename}' applied: {result['summary']} "
        f"admissions={result['admissions']}",
        {
            "filename": file.filename,
            "summary": result["summary"],
            "admissions": result["admissions"],
            "mapping": parsed_mapping,
            "date_order": date_order,
            "header_line": result.get("header_line"),
            "default_department": default_department,
            "created_patient_ids": result["created_patient_ids"],
            "updated_patient_ids": result["updated_patient_ids"],
            "created_admission_ids": result["created_admission_ids"],
        },
        target_id=result["run_id"],
    )
    return result


# ── History ──────────────────────────────────────────────────────────────────


_RUNS_USER_DEFAULT = Depends(require_permission(ADMIN_OVERVIEW))


@router.get("/runs")
async def runs(
    limit: int = 50,
    user: dict = _RUNS_USER_DEFAULT,
):
    """Applied-import history, newest first. Dry runs are not recorded here."""
    return {"runs": await run_db(roster_import.list_runs, limit)}
