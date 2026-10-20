#!/usr/bin/env python3
"""Write one facility's patient records out of the appliance, at its console.

WHY THIS EXISTS (multisite-15, W12-D, 20 October 2026)
One Orb appliance can hold several facilities of one hospital group in one
encrypted database (ruling MS-R1 (a)). When a facility leaves the group (it is
sold, it closes, or it moves to an appliance of its own) it needs its
patients' records, and a backup cannot give it them: a backup is the whole
appliance, every facility at once, and a restore rolls every facility back
together (ruling MS-R12 (a)). This script writes a FHIR R4 extract of one
facility to a local folder.

WHAT IS IN IT
  * Every patient with an encounter at the facility (an admission there, an
    outpatient visit there that was not cancelled, or a booking there that
    was not cancelled), and every patient registered there who has no
    encounter anywhere.
  * One FHIR collection Bundle per patient
    (services/fhir_export_service.facility_extract_bundle): the Patient, the
    facility's Organization with its Health Facility Registry id, and the
    encounters at this facility only. The allergies, the problem list and the
    current medicines are always included whole: they are the patient's and
    are never filtered by facility (ruling MS-R2 (a)).
  * A patient whose every encounter is at this facility also gets their
    observations and their whole medicine history. A patient who also has an
    encounter at another facility does not: clinical rows carry no encounter
    id, so that history cannot be told apart from the other facility's, and
    the bundle is tagged group-level-only and listed so in the manifest.
  * manifest.json: the facility, when, who was at the console, the reason,
    and for each patient the file, the number of encounters and whether the
    history is full or group-level only.

HOW IT IS KEPT SAFE
  * A reason is required, and nothing is ever written over: the output folder
    must not exist yet, or must be empty.
  * Files are written 0o600 into a hidden folder beside the output (0o700).
    Then, in one transaction, one phi_access audit row per patient
    (facility_extract_patient_included) and one for the extract
    (facility_extract_written) are written. Those rows are fail-closed: if
    they cannot be written the hidden folder is deleted and nothing is left.
    Only then is the folder moved into place.
  * Strictly offline: it writes to a local folder and nowhere else.
  * --dry-run counts what would be written and writes nothing. It prints no
    patient, so it writes no audit row.

Usage:
    python server/scripts/facility_extract.py --facility SAT1 --dry-run
    python server/scripts/facility_extract.py --facility SAT1 --out /Volumes/Exit/SAT1 \\
        --reason "SAT1 moves to its own appliance"

Run it with the same ORB_DB_PATH and ORB_DB_ENCRYPTION_KEY as the server.

Exit codes:
    0  written, or counted (--dry-run)
    1  refused (the reason is printed), or the audit trail was not available
    2  the command line was not understood
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import platform
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Ensure server root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


from database import db_session  # noqa: E402

MIN_REASON_LEN = 3
SCRIPT = "facility_extract.py"
MANIFEST = "manifest.json"
PATIENT_FOLDER = "patients"
HISTORY_FULL = "full"
HISTORY_GROUP_LEVEL = "group_level_only"


class ExtractRefused(RuntimeError):
    """An extract this script will not write. The message is for the operator."""


def _operator() -> dict:
    """Who is at the console, for the audit rows. Never a credential."""
    try:
        user: str | None = getpass.getuser()
    except Exception:
        user = None
    try:
        host: str | None = platform.node() or None
    except Exception:
        host = None
    return {"os_user": user, "host": host}


def _reason(raw: str | None) -> str:
    reason = (raw or "").strip()
    if len(reason) < MIN_REASON_LEN:
        raise ExtractRefused(f"Give a reason of at least {MIN_REASON_LEN} characters (--reason).")
    return reason


def _has_table(db: Any, name: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def resolve_facility(db: Any, code: str | None) -> dict:
    """The facilities row whose code this is (ignoring case), or a refusal."""
    wanted = (code or "").strip()
    if not wanted:
        raise ExtractRefused("Name the facility by its code (--facility), e.g. --facility MAIN.")
    if not _has_table(db, "facilities"):
        raise ExtractRefused(
            "This database has no facilities table yet. Start Orb once so it is "
            "brought up to date (migration 70), then retry."
        )
    row = db.execute("SELECT * FROM facilities WHERE code = ? COLLATE NOCASE", (wanted,)).fetchone()
    if row is None:
        raise ExtractRefused(f"There is no facility with the code {wanted}.")
    return {k: row[k] for k in row.keys()}


def plan(db: Any, facility_id: int) -> list[dict]:
    """The patients one facility's extract holds, in id order:
    [{patient_id, here, elsewhere, attributable}]. here and elsewhere count
    admissions, outpatient visits and bookings (cancelled visits and bookings
    left out, as the scope rule leaves them out); attributable is True when
    every one of them is at this facility."""
    fid = int(facility_id)
    here = ["(SELECT COUNT(*) FROM admissions a WHERE a.patient_id = p.id AND a.facility_id = ?)"]
    away = ["(SELECT COUNT(*) FROM admissions a WHERE a.patient_id = p.id AND a.facility_id != ?)"]
    if _has_table(db, "opd_visits"):
        visit = (
            "(SELECT COUNT(*) FROM opd_visits v WHERE v.patient_id = p.id "
            "AND v.status != 'cancelled' AND v.facility_id {} ?)"
        )
        here.append(visit.format("="))
        away.append(visit.format("!="))
    if _has_table(db, "appointments"):
        booking = (
            "(SELECT COUNT(*) FROM appointments ap WHERE ap.patient_id = p.id "
            "AND ap.status != 'cancelled' AND ap.facility_id {} ?)"
        )
        here.append(booking.format("="))
        away.append(booking.format("!="))
    sql = (
        "SELECT p.id, p.registered_facility_id, "
        + " + ".join(here)
        + " AS here, "
        + " + ".join(away)
        + " AS elsewhere FROM patients p ORDER BY p.id"
    )
    rows = db.execute(sql, [fid] * (len(here) + len(away))).fetchall()
    out: list[dict] = []
    for r in rows:
        n_here, n_away = int(r["here"] or 0), int(r["elsewhere"] or 0)
        registered_here = int(r["registered_facility_id"] or 1) == fid
        if n_here == 0 and not (n_away == 0 and registered_here):
            continue
        out.append(
            {
                "patient_id": r["id"],
                "here": n_here,
                "elsewhere": n_away,
                "attributable": n_away == 0,
            }
        )
    return out


def _patient_file(pid: str) -> str:
    """The bundle's file name: the id made safe for a file name plus a short
    hash of the raw id, so two ids that clean to the same text never collide
    (the nightly self-export's naming)."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(pid))
    digest = hashlib.sha256(str(pid).encode("utf-8")).hexdigest()[:8]
    return f"{safe}.{digest}.fhir.json"


def _write_private(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _check_out_dir(out: Path) -> None:
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ExtractRefused(
            f"{out} already exists and is not an empty folder. An extract never writes "
            "over anything: name a new folder."
        )


def _audit(
    db: Any, facility: dict, planned: list[dict], files: list[dict], out: Path, reason: str
) -> str:
    """One phi_access row per patient and one for the extract, in the
    caller's transaction; fail-closed (AuditWriteError) like every
    disclosure of a record. Returns the correlation id."""
    from services.audit_service import log_audit_event

    correlation = f"facility-extract-{uuid.uuid4()}"
    operator = _operator()
    fid = int(facility["id"])
    by_patient = {f["patient_id"]: f for f in files}
    for p in planned:
        entry = by_patient[p["patient_id"]]
        log_audit_event(
            correlation_id=correlation,
            event_category="phi_access",
            action_type="facility_extract_patient_included",
            outcome="success",
            db=db,
            summary_message=(
                f"Patient {p['patient_id']} included in the extract of facility "
                f"{facility['code']} at the console"
            ),
            actor_user_id=None,
            actor_role="console",
            source_type="console",
            patient_id=p["patient_id"],
            target_type="facility",
            target_id=str(fid),
            metadata={
                "facility_id": fid,
                "facility_code": facility["code"],
                "history": entry["history"],
                "encounters": entry["encounters"],
                "reason": reason,
                "operator": operator,
                "script": SCRIPT,
            },
        )
    log_audit_event(
        correlation_id=correlation,
        event_category="phi_access",
        action_type="facility_extract_written",
        outcome="success",
        db=db,
        summary_message=(
            f"Extract of facility {facility['code']} written at the console: "
            f"{len(planned)} patient(s)"
        ),
        actor_user_id=None,
        actor_role="console",
        source_type="console",
        target_type="facility",
        target_id=str(fid),
        metadata={
            "facility_id": fid,
            "facility_code": facility["code"],
            "patients": len(planned),
            "full_history": sum(1 for f in files if f["history"] == HISTORY_FULL),
            "group_level_only": sum(1 for f in files if f["history"] == HISTORY_GROUP_LEVEL),
            "encounters": sum(int(f["encounters"]) for f in files),
            "directory": str(out),
            "reason": reason,
            "operator": operator,
            "script": SCRIPT,
        },
    )
    return correlation


def _summary(facility: dict, planned: list[dict]) -> dict:
    return {
        "facility": {"id": int(facility["id"]), "code": facility["code"]},
        "patients": len(planned),
        "full_history": sum(1 for p in planned if p["attributable"]),
        "group_level_only": sum(1 for p in planned if not p["attributable"]),
    }


def write_extract(
    facility_code: str | None,
    out_dir: str | os.PathLike | None,
    reason: str | None,
    *,
    dry_run: bool = False,
) -> dict:
    """Write (or with dry_run, count) one facility's extract. Returns a
    summary {facility, patients, full_history, group_level_only, directory?,
    manifest?, correlation_id?, dry_run}. Raises ExtractRefused, or
    AuditWriteError when the audit rows could not be written (then nothing
    is left on disk)."""
    from services.fhir_export_service import facility_extract_bundle

    from services import facility_service

    with db_session() as db:
        facility = resolve_facility(db, facility_code)
        planned = plan(db, int(facility["id"]))
    summary = _summary(facility, planned)
    if dry_run:
        return {**summary, "dry_run": True}

    reason_text = _reason(reason)
    if out_dir is None or not str(out_dir).strip():
        raise ExtractRefused("Name the folder to write the extract to (--out).")
    out = Path(out_dir).expanduser().absolute()
    _check_out_dir(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.parent / f".{out.name}.partial-{uuid.uuid4().hex[:8]}"
    staging.mkdir(mode=0o700)
    (staging / PATIENT_FOLDER).mkdir(mode=0o700)
    fid = int(facility["id"])
    try:
        files: list[dict] = []
        with db_session() as db:
            for p in planned:
                bundle = facility_extract_bundle(
                    p["patient_id"], fid, attributable=p["attributable"], db=db
                )
                if bundle is None:  # deleted between the plan and the write
                    raise ExtractRefused(
                        "A patient disappeared while the extract was being written. "
                        "Nothing was kept; run it again."
                    )
                name = _patient_file(p["patient_id"])
                _write_private(
                    staging / PATIENT_FOLDER / name, json.dumps(bundle, separators=(",", ":"))
                )
                files.append(
                    {
                        "patient_id": p["patient_id"],
                        "file": f"{PATIENT_FOLDER}/{name}",
                        "encounters": sum(
                            1
                            for e in bundle["entry"]
                            if e["resource"].get("resourceType") == "Encounter"
                        ),
                        "history": HISTORY_FULL if p["attributable"] else HISTORY_GROUP_LEVEL,
                    }
                )
        written_at = datetime.now(UTC).isoformat()
        manifest = {
            "kind": "orb-facility-extract",
            "format": "fhir-r4-collection-bundles",
            "facility": {
                "id": fid,
                "code": facility["code"],
                "name": facility_service.display_name(fid, facility=facility),
                "hfr_id": facility.get("hfr_id"),
            },
            "written_at": written_at,
            "reason": reason_text,
            "operator": _operator(),
            **{k: v for k, v in summary.items() if k != "facility"},
            "encounters": sum(int(f["encounters"]) for f in files),
            "history_note": (
                "group_level_only: the patient also has an encounter at another facility, so "
                "only this facility's encounters, the allergies, the problem list and the "
                "current medicines are included"
            ),
            "files": files,
        }
        _write_private(staging / MANIFEST, json.dumps(manifest, indent=2))
        with db_session() as db:
            correlation = _audit(db, facility, planned, files, out, reason_text)
        if out.exists():
            out.rmdir()  # checked empty above
        os.replace(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        **summary,
        "dry_run": False,
        "directory": str(out),
        "manifest": str(out / MANIFEST),
        "correlation_id": correlation,
    }


def main(argv: list[str] | None = None) -> int:
    from services.audit_service import AuditWriteError

    parser = argparse.ArgumentParser(
        description="Write one facility's patient records (FHIR R4) to a local folder."
    )
    parser.add_argument("--facility", required=True, help="The facility's code, e.g. SAT1.")
    parser.add_argument("--out", help="A new or empty folder to write the extract to.")
    parser.add_argument("--reason", help="Why the extract is written (required to write).")
    parser.add_argument(
        "--dry-run", action="store_true", help="Count what would be written; write nothing."
    )
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return 0 if exit_.code == 0 else 2

    try:
        result = write_extract(args.facility, args.out, args.reason, dry_run=args.dry_run)
    except ExtractRefused as refused:
        print(f"[fail] {refused}", file=sys.stderr)
        return 1
    except AuditWriteError as e:
        print(
            f"[fail] The audit trail could not record this extract, so nothing was kept: {e}",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    code = result["facility"]["code"]
    counts = (
        f"{result['patients']} patient(s): {result['full_history']} with their whole history, "
        f"{result['group_level_only']} group-level only"
    )
    if result["dry_run"]:
        print(f"[ ok ] Facility {code} would be extracted: {counts}. Nothing was written.")
    else:
        print(f"[ ok ] Facility {code} extracted to {result['directory']}: {counts}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
