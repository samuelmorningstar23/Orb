"""Orb Hospital OS — EHR Downtime Mode Service

A durable, LOCAL, read-only snapshot of the essential clinical picture that keeps
working when the EHR, the network, or the LLM is down. Cloud-only EHRs
structurally cannot offer this: when their servers are unreachable the ward has
nothing. Orb runs on-prem, so we can freeze a safe, printable copy of each
patient's must-know data (demographics, allergies, problems, active meds, the
latest vitals + NEWS2, code status, open escalations, recent signed and chart
notes, recent lab results) into the local SQLite DB and serve
it read-only with zero external dependencies.

Design contract — EVERYTHING here must work with NO Ollama and NO network:
  * capture assembles the snapshot from pure DB reads only.
  * the risk figure is the rule-based NEWS2 score computed in-process
    (services.vigil_service.compute_news2), NOT the LLM narrative and NOT the
    optional torch/CfC overlay — so a snapshot can always be taken, even on a
    box with no models and no connectivity. That is the whole point.
"""
from __future__ import annotations

import atexit
import html
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from database import db_session
# NEWS2 is a pure, rule-based function (no network, no LLM, no torch). We call it
# directly rather than vigil_service.get_patient_risk() so capture never pulls in
# the optional CfC/torch overlay or any I/O beyond the DB.
from services.vigil_service import compute_news2, scoring_enabled
from services.drug_chart import drug_chart_enabled
from services.news2_text import news2_description, news2_qualifiers
from services.hospital_time import hospital_stamp
from services.vital_charting import charted_text
from services import lab_observations

logger = logging.getLogger("orb.downtime")

# Bump if the snapshot_json shape changes in a breaking way; readers can branch on it.
# 2: adds code_status, open_escalations, recent_documents (clinical notes),
#    recent_lab_reports, lab_results and medications[].verified_by.
SNAPSHOT_SCHEMA_VERSION = 2

# How many of each recent record type to freeze into a snapshot.
_RECENT_NOTES = 5
_RECENT_RISK = 5
_RECENT_DOCUMENTS = 5
_RECENT_LAB_REPORTS = 3
# Structured results: the newest of each analyte, capped. Rows a sender
# withdrew or marked in error, or a correction superseded, are left out by the
# one lab reader every lane shares (services/lab_observations.py).
_RECENT_LAB_RESULTS = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _build_summary(demo: dict, allergies: list, problems: list,
                   medications: list, news2: dict | None,
                   has_vitals: bool = False) -> str:
    """One-line, human-readable summary for the snapshot list / banner."""
    name = demo.get("name") or "Unknown patient"
    mrn = demo.get("mrn") or "—"
    age = demo.get("age")
    sex = (demo.get("sex") or "").strip()
    age_sex = ""
    if age is not None or sex:
        age_sex = f", {age if age is not None else '?'}{sex[:1].upper() if sex else ''}"
    loc_bits = [b for b in (demo.get("ward"), demo.get("bed")) if b]
    loc = f" ({' / '.join(loc_bits)})" if loc_bits else ""

    # Never "NKDA": an empty list is also a patient nobody has asked yet.
    allergy_note = (
        f"{len(allergies)} active allerg{'y' if len(allergies) == 1 else 'ies'}"
        if allergies else "No allergies recorded"
    )
    parts = [
        f"{name} (MRN {mrn}{age_sex}){loc}",
        allergy_note,
        f"{len(problems)} active problem{'s' if len(problems) != 1 else ''}",
    ]
    # On an Orb Ward Record computer the drug chart stays on paper: the line
    # says nothing about medicines (services/drug_chart).
    if drug_chart_enabled():
        parts.append(f"{len(medications)} active med{'s' if len(medications) != 1 else ''}")
    if news2 and news2.get("raw_news2") is not None:
        # 2026-10-02: name Scale 2, or "NEWS2 0 (stable)" reads as a Scale 1
        # score; SpO2 90 on air is 0 on Scale 2 and 3 on Scale 1. Since
        # 2026-10-03 the label is news2_text's, the same sentence the board
        # badge and the Relay alerts use, so it also says when a single
        # parameter scores 3 and when the set was incomplete (a minimum).
        parts.append(news2_description(news2))
    elif has_vitals:
        # Scoring switched off (the Orb Ward Record): the set is there, no score.
        parts.append("vitals recorded")
    else:
        parts.append("no vitals recorded")
    return ". ".join(parts) + "."


def capture_snapshot(patient_id: str, created_by: str = "auto") -> dict:
    """Assemble the essential clinical picture for one patient from the DB and
    persist it as a single patient_snapshots row.

    Pure DB reads + rule-based NEWS2 only — no Ollama, no network, no torch. Raises
    ValueError if the patient does not exist. Returns the full snapshot envelope
    (including the new row id + created_at) as a dict.
    """
    with db_session() as db:
        prow = db.execute(
            """SELECT p.id, p.name, p.mrn, p.bed, p.ward, p.admission_date,
                      p.diagnosis, p.status, p.age, p.date_of_birth, p.sex,
                      p.phone, p.emergency_contact_name, p.emergency_contact_phone,
                      p.address, p.attending_doctor_id, p.on_scale_2,
                      u.name AS attending_doctor_name
               FROM patients p
               LEFT JOIN users u ON u.id = p.attending_doctor_id
               WHERE p.id = ?""",
            (patient_id,),
        ).fetchone()
        if prow is None:
            raise ValueError(f"Patient not found: {patient_id}")
        demo = _row_to_dict(prow)
        on_scale_2 = bool(demo.pop("on_scale_2", 0) or 0)

        # Active allergies — the single most safety-critical field in a downtime view.
        allergies = [
            _row_to_dict(r)
            for r in db.execute(
                """SELECT substance, substance_class, reaction, severity, status, source
                   FROM patient_allergies
                   WHERE patient_id = ? AND status = 'active'
                   ORDER BY CASE severity
                              WHEN 'severe' THEN 0 WHEN 'moderate' THEN 1
                              WHEN 'mild' THEN 2 ELSE 3 END, substance""",
                (patient_id,),
            ).fetchall()
        ]

        # Active problems.
        problems = [
            _row_to_dict(r)
            for r in db.execute(
                """SELECT problem, icd10_code, snomed_code, status, onset_date, source
                   FROM problem_list
                   WHERE patient_id = ? AND status = 'active'
                   ORDER BY recorded_at DESC""",
                (patient_id,),
            ).fetchall()
        ]

        # Active medications. verified_by lets the downtime sheet say which
        # AI-extracted drugs no pharmacist has checked; without it they print
        # like prescriptions. None on an Orb Ward Record computer, whose drug
        # chart stays on paper (services/drug_chart).
        medications = [
            _row_to_dict(r)
            for r in db.execute(
                """SELECT drug_name, dosage, route, frequency, status, source, notes,
                          verified_by
                   FROM medications
                   WHERE patient_id = ? AND status IN ('active','prn')
                   ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'prn' THEN 1 ELSE 2 END, drug_name""",
                (patient_id,),
            ).fetchall()
        ] if drug_chart_enabled() else []

        # Latest vitals row (full row so the printout shows exactly what was recorded).
        # id DESC: on a same-timestamp tie the later write (a correction) is the latest.
        vrow = db.execute(
            """SELECT timestamp, heart_rate, systolic_bp, diastolic_bp, spo2,
                      temperature, respiratory_rate, consciousness, on_oxygen
               FROM vitals
               WHERE patient_id = ? AND voided_at IS NULL
               ORDER BY timestamp DESC, id DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
        latest_vitals = _row_to_dict(vrow) if vrow is not None else None

        # Recent signed scribe notes (last few).
        notes = [
            _row_to_dict(r)
            for r in db.execute(
                # Names travel with the ids: the printed page is read by people
                # who know Dr. Sharma, not DOC001.
                """SELECT id, note_format, structured_note, author_id,
                          signed_by, signed_at, created_at,
                          (SELECT u.name FROM users u WHERE u.id = scribe_notes.author_id) AS author_name,
                          (SELECT u.name FROM users u WHERE u.id = scribe_notes.signed_by) AS signed_by_name
                   FROM scribe_notes
                   WHERE patient_id = ? AND status = 'signed'
                   ORDER BY COALESCE(signed_at, created_at) DESC
                   LIMIT ?""",
                (patient_id, _RECENT_NOTES),
            ).fetchall()
        ]

        # Recent risk scores (deterioration trend at a glance).
        risk_scores = [
            _row_to_dict(r)
            for r in db.execute(
                """SELECT timestamp, score, level, drivers
                   FROM risk_scores
                   WHERE patient_id = ?
                   ORDER BY timestamp DESC, id DESC LIMIT ?""",
                (patient_id, _RECENT_RISK),
            ).fetchall()
        ]

        # Code status from the active admission: the first thing a crash team
        # asks, and the one fact a downtime copy must not leave out.
        crow = db.execute(
            """SELECT code_status FROM admissions
               WHERE patient_id = ? AND status = 'active'
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
        code_status = crow["code_status"] if crow is not None else None

        # Open escalation chains (raised or acknowledged, not yet resolved).
        open_escalations = [
            _row_to_dict(r)
            for r in db.execute(
                """SELECT status, trigger, news2, level, current_tier, max_tier,
                          tier_role, tier_label, raised_at, acknowledged_at
                   FROM clinical_escalations
                   WHERE patient_id = ? AND status != 'resolved'
                   ORDER BY julianday(raised_at) DESC, rowid DESC""",
                (patient_id,),
            ).fetchall()
        ]

        # Clinical notes written in the chart (clinical_documents), which the
        # signed Scribe notes above do not cover. Lab reports are kept apart.
        # julianday(): rows carry both 'T' and space separated stamps.
        documents = [
            _row_to_dict(r)
            for r in db.execute(
                """SELECT id, doc_type, title, content, author_id, created_at,
                          (SELECT u.name FROM users u WHERE u.id = clinical_documents.author_id) AS author_name
                   FROM clinical_documents
                   WHERE patient_id = ? AND doc_type != 'lab_result'
                   ORDER BY julianday(created_at) DESC, id DESC LIMIT ?""",
                (patient_id, _RECENT_DOCUMENTS),
            ).fetchall()
        ]

        # Orb's own rendered lab reports (one document per panel).
        lab_reports = [
            _row_to_dict(r)
            for r in db.execute(
                """SELECT id, title, content, author_id, created_at
                   FROM clinical_documents
                   WHERE patient_id = ? AND doc_type = 'lab_result'
                   ORDER BY julianday(created_at) DESC, id DESC LIMIT ?""",
                (patient_id, _RECENT_LAB_REPORTS),
            ).fetchall()
        ]

        # Interfaced results: the newest row of each analyte, newest first.
        # Read through services/lab_observations.py, the one reader every lab
        # lane shares (void and superseded rows left out, julianday order).
        lab_results = [
            _row_to_dict(r)
            for r in lab_observations.latest_per_analyte(
                db, patient_id, limit=_RECENT_LAB_RESULTS)
        ]

    # Scoring switched off (ORB_SCORING_ENABLED off, the Orb Ward Record): no
    # NEWS2, no stored risk rows and no escalation chain go into the snapshot,
    # so neither the downtime page nor the printed pack can show one.
    scoring = scoring_enabled()
    if not scoring:
        risk_scores = []
        open_escalations = []

    # ── NEWS2 from the latest vitals row (rule-based, in-process, offline-safe) ──
    news2: dict | None = None
    if latest_vitals is not None and scoring:
        news2 = compute_news2(
            latest_vitals.get("heart_rate"),
            latest_vitals.get("systolic_bp"),
            latest_vitals.get("respiratory_rate"),
            latest_vitals.get("temperature"),
            latest_vitals.get("spo2"),
            # Unassessed values pass THROUGH as None: the same points, and
            # `complete` then tells the pack's reader the score is a minimum.
            consciousness=latest_vitals.get("consciousness"),
            on_oxygen=(None if latest_vitals.get("on_oxygen") is None
                       else bool(latest_vitals.get("on_oxygen"))),
            on_scale_2=on_scale_2,
        )

    # Decode the JSON `drivers` column stored on each risk_scores row.
    for rs in risk_scores:
        if isinstance(rs.get("drivers"), str):
            try:
                rs["drivers"] = json.loads(rs["drivers"])
            except (json.JSONDecodeError, TypeError):
                pass

    captured_at = _now_iso()
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": captured_at,
        "patient_id": patient_id,
        "demographics": demo,
        "allergies": allergies,
        "problems": problems,
        "medications": medications,
        "latest_vitals": latest_vitals,
        "news2": news2,
        "code_status": code_status,
        "open_escalations": open_escalations,
        "recent_notes": notes,
        "recent_documents": documents,
        "recent_lab_reports": lab_reports,
        "lab_results": lab_results,
        "recent_risk_scores": risk_scores,
        "counts": {
            "allergies": len(allergies),
            "problems": len(problems),
            "medications": len(medications),
            "notes": len(notes),
            "documents": len(documents),
            "lab_reports": len(lab_reports),
            "lab_results": len(lab_results),
        },
    }
    if not drug_chart_enabled():
        snapshot = _without_drug_chart(snapshot)
    summary = _build_summary(demo, allergies, problems, medications, news2,
                             has_vitals=latest_vitals is not None)

    with db_session() as db:
        cur = db.execute(
            """INSERT INTO patient_snapshots (patient_id, snapshot_json, summary, created_by, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (patient_id, json.dumps(snapshot), summary, created_by, captured_at),
        )
        snapshot_id = cur.lastrowid
        who = db.execute("SELECT name FROM users WHERE id = ?", (created_by,)).fetchone()

    logger.info(
        "[Downtime] Captured snapshot #%s for %s by %s (%s)",
        snapshot_id, patient_id, created_by, summary,
    )
    return {
        "id": snapshot_id,
        "patient_id": patient_id,
        "summary": summary,
        "created_by": created_by,
        "created_by_name": who["name"] if who is not None else None,
        "created_at": captured_at,
        "snapshot": snapshot,
    }


def capture_all_admitted(created_by: str = "auto") -> dict:
    """Snapshot every admitted patient. This is what a scheduled pre-downtime
    drill / nightly job calls so the ward always has a fresh local copy to fall
    back to. Returns a summary with the count captured and any failures."""
    with db_session() as db:
        rows = db.execute(
            "SELECT id FROM patients WHERE status = 'admitted' ORDER BY id"
        ).fetchall()
    patient_ids = [r["id"] for r in rows]

    captured = 0
    failed: list[dict] = []
    for pid in patient_ids:
        try:
            capture_snapshot(pid, created_by=created_by)
            captured += 1
        except Exception as e:  # never let one bad patient abort the drill
            logger.warning("[Downtime] Snapshot failed for %s: %s", pid, e)
            failed.append({"patient_id": pid, "error": str(e)})

    logger.info("[Downtime] capture_all_admitted: %s/%s captured", captured, len(patient_ids))
    return {
        "count": captured,
        "total_admitted": len(patient_ids),
        "failed": failed,
        "captured_at": _now_iso(),
    }


def fhir_self_export_dir() -> "os.PathLike":
    """Directory where nightly FHIR $everything self-exports are written.

    Created 0o700 (user-only) — the filenames themselves encode patient ids, so
    the directory listing is PHI-adjacent and must not be world-enumerable.

    ORB_FHIR_EXPORT_DIR overrides the on-appliance default, and is read HERE on
    every call rather than bound to a module constant at import — the same lazy
    shape as loop_watchdog._default_dir()'s ORB_WATCHDOG_DIR. That is what lets
    tests/conftest.py redirect the whole session to a throwaway directory while
    an individual test still monkeypatches its own path on top.

    The value is stripped: systemd unit files and .env lines routinely carry
    trailing whitespace or a CRLF, and an unstripped " /var/lib/orb/exports\\r"
    silently becomes a *different*, wrongly-named directory. Blank after
    stripping is treated as unset, so `ORB_FHIR_EXPORT_DIR=` cannot resolve the
    PHI export root to the process CWD.
    """
    import os
    from config import BASE_DIR
    override = os.getenv("ORB_FHIR_EXPORT_DIR", "").strip()
    d = override or os.path.join(str(BASE_DIR), "downtime_exports")
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)  # enforce even if the dir pre-existed with looser perms
    except OSError:
        pass
    return d


# ══════════════════════════════════════════════════════════════════════════════
#  Nightly FHIR self-export — throttled, off the loop, off the shared pool
# ══════════════════════════════════════════════════════════════════════════════
#
# WHY THIS IS BUILT THE WAY IT IS
# -------------------------------
# This job was THREAD C in the 2026-08-01 hard deadlock. Measured on the pilot
# box (30 admitted patients, 61,290 vitals rows): one full run builds 30 bundles
# of ~12,300 FHIR resources each and writes 282 MB in 11.4 s, of which 8.8 s
# (77%) is json.dump + write(2). The faulthandler dump caught it in
# _io_FileIO_write -> _Py_write_impl -> take_gil: a single worker thread doing
# many seconds of unbroken serialize-and-write, reacquiring the GIL on every
# buffer flush, while the uvloop main thread sat in take_gil waiting for it.
#
# Three separate hazards, three separate mitigations:
#
#  1. It ran via asyncio.to_thread -> the DEFAULT executor, which is shared
#     process-wide with sage_routes._prepare_context, the vigil route helpers,
#     scribe ASR and the proactive sweep. An 11 s occupant of a shared 16-slot
#     pool is exactly the failure mode event_bus._DB_EXECUTOR and
#     nervous_system_triggers._WARM_EXECUTOR were created to avoid. It now has
#     its own single-thread lane (_EXPORT_EXECUTOR), same precedent.
#
#  2. It serialised each 9.4 MB bundle in one json.dump call. We now stream the
#     bundle entry by entry and sleep between 256 KB chunks, so the longest
#     unbroken GIL-hungry stretch is one chunk instead of one whole patient. A
#     sleeping thread holds NO GIL at all, which is strictly stronger than
#     relying on sys.setswitchinterval to preempt a hot C loop. Output is
#     BYTE-IDENTICAL to the old json.dump (asserted in the tests) — throttling
#     must never alter the exported record.
#
#  3. It re-ran 30 s after EVERY process restart (main.py fired it on
#     `hours % 24 == 0`, and hours starts at 0), landing a 282 MB write burst on
#     top of model loading. A persisted last-run marker now makes a restart
#     within the interval a no-op, while an appliance that was powered off
#     overnight still exports on the next boot.
#
# IN-PROCESS, NOT A CHILD PROCESS — deliberate, see the module docstring note in
# services/knowledge_worker.py for the case where a process split IS warranted.
# Here the whole job is ~11 s of CPU once a day; a spawn child would need the
# SQLCipher key handed to it and would add a second process opening a 227 MB
# encrypted DB. Throttling removes the hazard for a fraction of the risk. The
# seam is clean if that changes: _export_one_patient() is the unit of work.

# Private lane. One worker, because the job is inherently serial (one SQLite
# reader + one disk) and because the whole point is to bound its cost to exactly
# one thread no matter what else the appliance is doing.
_EXPORT_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="orb-fhir-export")
atexit.register(_EXPORT_EXECUTOR.shutdown, wait=False)

# Non-blocking guard: a second run while one is in flight is a no-op, not a
# queued duplicate. Protects against a manual/admin trigger landing on top of
# the scheduled run.
_EXPORT_LOCK = threading.Lock()

# Throttle knobs. Tuned by measurement against the live server (30 patients,
# 287 MB), sampling GET /api/health every 50 ms through a full run:
#
#   chunk / sleep      run     health p95 during run   max     >250 ms
#   ----------------------------------------------------------------------
#   (unthrottled)      11 s    24.1 ms loop lag *      58 ms*  —
#   512 KB / 15 ms     22 s    59.7 ms                 120 ms  0
#   256 KB / 25 ms     43 s     6.1 ms                 115 ms  0   <- default
#   * loop-lag harness, not the live server
#
# Doubling the run buys a 10x cut in the p95 a clinician would feel. A nightly
# job that takes 43 s instead of 11 s but cannot touch a clinical request is the
# right trade on a ward. The residual ~115 ms tail is present in the live
# process with or without the export (vitals simulator + socket.io + model
# work), so tightening further buys nothing.
_CHUNK_BYTES = int(os.getenv("ORB_FHIR_EXPORT_CHUNK_BYTES", "262144"))
_CHUNK_SLEEP_S = float(os.getenv("ORB_FHIR_EXPORT_CHUNK_SLEEP_MS", "25")) / 1000.0
_PATIENT_SLEEP_S = float(os.getenv("ORB_FHIR_EXPORT_PATIENT_SLEEP_MS", "250")) / 1000.0

# Delete exported bundles nobody has refreshed in this many days. Files for
# currently-admitted patients are rewritten every run so they never age out;
# this only reaps patients who have since been discharged. 0 disables.
_RETENTION_DAYS = float(os.getenv("ORB_FHIR_EXPORT_RETENTION_DAYS", "14"))

# Optional cap on how much observation history goes into a bundle, in days.
# UNSET BY DEFAULT ON PURPOSE: this file is a clinical escrow copy of the
# record, and silently truncating it would be a safety change, not a disk
# optimisation. Set ORB_FHIR_EXPORT_SINCE_DAYS explicitly if bundle growth
# (vitals accumulate ~2,000 rows/patient/day here) outruns the appliance disk.
_SINCE_DAYS = os.getenv("ORB_FHIR_EXPORT_SINCE_DAYS")

_LAST_RUN_MARKER = ".last_fhir_export"


def _marker_path(out_dir) -> str:
    return os.path.join(str(out_dir), _LAST_RUN_MARKER)


def _last_run_age_hours(out_dir) -> float | None:
    """Hours since the last SUCCESSFUL export, or None if there has never been one."""
    try:
        return (time.time() - os.path.getmtime(_marker_path(out_dir))) / 3600.0
    except OSError:
        return None


def _touch_marker(out_dir) -> None:
    p = _marker_path(out_dir)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(_now_iso())
    except OSError as e:
        logger.warning("[Downtime] could not write FHIR export marker: %s", e)


def _bundle_path(out_dir, pid: str) -> str:
    """Deterministic per-patient filename.

    Sanitize the id for a filename AND append a short hash of the raw id, so two
    distinct ids that sanitize to the same string (e.g. "a.b" vs "a_b") cannot
    collide and overwrite each other's bundle.
    """
    import hashlib as _hl
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(pid))
    digest = _hl.sha256(str(pid).encode("utf-8")).hexdigest()[:8]
    return os.path.join(str(out_dir), f"{safe}.{digest}.fhir.json")


def _write_bundle_throttled(bundle: dict, final: str,
                            chunk_bytes: int, chunk_sleep_s: float) -> int:
    """Serialise `bundle` to `final` atomically, in bounded chunks, yielding the
    GIL between them. Returns bytes written.

    This is the heart of the fix. json.dump(bundle, f) walks a 12,000-entry tree
    in one uninterrupted call; the encoder holds the GIL through the whole walk
    and only drops it inside each write(2). By emitting one entry at a time and
    sleeping every `chunk_bytes`, the longest unbroken GIL hold becomes one
    chunk (~10 ms of work) instead of one patient (~300 ms), and the sleep hands
    the GIL over outright rather than waiting to be preempted.
    """
    tmp = final + ".tmp"
    entries = bundle.get("entry") or []
    # Everything except the (huge) entry array, serialised once.
    head = {k: v for k, v in bundle.items() if k != "entry"}
    head_json = json.dumps(head, separators=(",", ":"))

    written = 0
    pending = 0
    # Create the temp file 0o600 FROM THE START (not chmod after rename), so the
    # PHI bundle is never world-readable even during the write window.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            # head_json is "{...}"; splice the entry array in before the closer.
            opening = head_json[:-1] + (',"entry":[' if len(head_json) > 2 else '"entry":[')
            f.write(opening)
            written += len(opening)
            for i, entry in enumerate(entries):
                s = json.dumps(entry, separators=(",", ":"))
                if i:
                    f.write(",")
                    written += 1
                f.write(s)
                written += len(s)
                pending += len(s)
                if chunk_bytes > 0 and pending >= chunk_bytes:
                    f.flush()
                    pending = 0
                    if chunk_sleep_s > 0:
                        time.sleep(chunk_sleep_s)  # hand the GIL over outright
            f.write("]}")
            written += 2
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, final)  # rename preserves the 0o600 mode
    return written


def _export_one_patient(pid: str, out_dir, chunk_bytes: int,
                        chunk_sleep_s: float, since: str | None) -> int:
    """Build and write one patient's bundle. Returns bytes written (0 if the
    patient produced no bundle). The unit of work — this is the seam if this job
    ever moves to a child process."""
    from services.fhir_export_service import patient_bundle
    bundle = patient_bundle(pid, since=since)
    if not bundle:
        return 0
    return _write_bundle_throttled(bundle, _bundle_path(out_dir, pid),
                                   chunk_bytes, chunk_sleep_s)


def prune_fhir_self_exports(out_dir=None, retention_days: float | None = None) -> dict:
    """Delete exported bundles older than `retention_days` and any orphaned .tmp
    files left by an interrupted run.

    Bundles for currently-admitted patients are rewritten on every run, so their
    mtime is always fresh and they are never reaped. This only removes patients
    who have since been discharged — which is both a disk fix (the directory had
    390 files / 296 MB against 30 admitted patients) and a PHI-minimisation fix
    (a discharged patient's full record was sitting on disk indefinitely).
    """
    out_dir = out_dir if out_dir is not None else fhir_self_export_dir()
    days = _RETENTION_DAYS if retention_days is None else retention_days
    now = time.time()
    removed = 0
    freed = 0
    tmp_removed = 0
    try:
        names = os.listdir(str(out_dir))
    except OSError:
        return {"removed": 0, "bytes_freed": 0, "tmp_removed": 0}

    for name in names:
        path = os.path.join(str(out_dir), name)
        try:
            if name.endswith(".tmp"):
                # An interrupted run's temp file. Anything older than an hour
                # cannot belong to a live run (a full run is ~30 s).
                if now - os.path.getmtime(path) > 3600:
                    os.unlink(path)
                    tmp_removed += 1
                continue
            if not name.endswith(".fhir.json"):
                continue
            if days <= 0:
                continue
            age_days = (now - os.path.getmtime(path)) / 86400.0
            if age_days > days:
                size = os.path.getsize(path)
                os.unlink(path)
                removed += 1
                freed += size
        except OSError as e:
            logger.warning("[Downtime] prune failed for %s: %s", name, e)

    if removed or tmp_removed:
        logger.info("[Downtime] FHIR export prune: removed %s stale bundle(s) (%.1f MB) "
                    "and %s orphaned .tmp file(s)", removed, freed / 1e6, tmp_removed)
    return {"removed": removed, "bytes_freed": freed, "tmp_removed": tmp_removed}


def run_nightly_fhir_self_export(created_by: str = "auto",
                                 min_interval_hours: float | None = 24.0,
                                 chunk_bytes: int | None = None,
                                 chunk_sleep_s: float | None = None,
                                 patient_sleep_s: float | None = None) -> dict:
    """Export a FHIR $everything Bundle for every admitted patient to a durable
    LOCAL directory, THROTTLED so it cannot starve the event loop.

    This is Orb's own on-prem escrow of the record: if the primary EHR is down or
    a hospital wants a portable, standards-based copy that never left the building,
    the latest per-patient FHIR bundle is already on local disk. Bundles are
    written atomically (temp file + rename) and each patient's file is overwritten
    with the freshest export. Never raises for a single bad patient.

    BLOCKING. Callers on the event loop must use run_nightly_fhir_self_export_async,
    which dispatches onto this module's private single-thread executor — never
    asyncio's shared default executor.

    `min_interval_hours` skips the run entirely if a successful export finished
    less than that long ago (pass 0/None to force). This is what stops a restart
    loop from re-writing ~287 MB every time the process comes up.
    """
    out_dir = fhir_self_export_dir()

    if not _EXPORT_LOCK.acquire(blocking=False):
        logger.info("[Downtime] FHIR self-export already running — skipping this trigger")
        return {"skipped": "already_running", "count": 0, "total_admitted": 0,
                "failed": [], "directory": str(out_dir), "exported_at": _now_iso()}
    try:
        if min_interval_hours:
            age = _last_run_age_hours(out_dir)
            if age is not None and age < min_interval_hours:
                logger.info("[Downtime] FHIR self-export skipped — last run %.1f h ago "
                            "(min interval %.1f h)", age, min_interval_hours)
                return {"skipped": "too_soon", "last_run_age_hours": round(age, 2),
                        "count": 0, "total_admitted": 0, "failed": [],
                        "directory": str(out_dir), "exported_at": _now_iso()}

        cb = _CHUNK_BYTES if chunk_bytes is None else chunk_bytes
        cs = _CHUNK_SLEEP_S if chunk_sleep_s is None else chunk_sleep_s
        ps = _PATIENT_SLEEP_S if patient_sleep_s is None else patient_sleep_s

        since = None
        if _SINCE_DAYS:
            try:
                since = (datetime.now(timezone.utc)
                         - timedelta(days=float(_SINCE_DAYS))).isoformat()
            except ValueError:
                logger.warning("[Downtime] bad ORB_FHIR_EXPORT_SINCE_DAYS=%r — exporting full history",
                               _SINCE_DAYS)

        with db_session() as db:
            rows = db.execute(
                "SELECT id FROM patients WHERE status = 'admitted' ORDER BY id"
            ).fetchall()
        patient_ids = [r["id"] for r in rows]

        exported = 0
        total_bytes = 0
        failed: list[dict] = []
        t0 = time.perf_counter()
        for i, pid in enumerate(patient_ids):
            try:
                n = _export_one_patient(pid, out_dir, cb, cs, since)
                if n:
                    exported += 1
                    total_bytes += n
            except Exception as e:
                logger.warning("[Downtime] FHIR self-export failed for %s: %s", pid, e)
                failed.append({"patient_id": pid, "error": str(e)})
            # Breathing room between patients: the next one immediately opens a
            # DB read and starts another multi-MB write, so this is where the
            # rest of the process gets its slot back.
            if ps > 0 and i + 1 < len(patient_ids):
                time.sleep(ps)
        duration = time.perf_counter() - t0

        pruned = prune_fhir_self_exports(out_dir)
        _touch_marker(out_dir)

        logger.info("[Downtime] FHIR self-export: %s/%s exported, %.1f MB in %.1fs to %s",
                    exported, len(patient_ids), total_bytes / 1e6, duration, out_dir)
        return {
            "count": exported,
            "total_admitted": len(patient_ids),
            "failed": failed,
            "directory": str(out_dir),
            "bytes_written": total_bytes,
            "duration_s": round(duration, 2),
            "pruned": pruned,
            "exported_at": _now_iso(),
        }
    finally:
        _EXPORT_LOCK.release()


async def run_nightly_fhir_self_export_async(created_by: str = "auto",
                                             min_interval_hours: float | None = 24.0) -> dict:
    """Loop-safe entry point. Runs the export on this module's PRIVATE
    single-thread executor.

    Do not replace this with asyncio.to_thread: that is the shared default
    executor, and a 40 s occupant of a pool that also serves Sage context
    preparation, the vigil route helpers and scribe ASR is precisely the
    contention this job caused on 2026-08-01.
    """
    import asyncio as _aio
    import functools as _ft
    loop = _aio.get_running_loop()
    return await loop.run_in_executor(
        _EXPORT_EXECUTOR,
        _ft.partial(run_nightly_fhir_self_export, created_by,
                    min_interval_hours=min_interval_hours),
    )


def _without_drug_chart(snapshot: dict) -> dict:
    """A snapshot as an Orb Ward Record computer sends it: no medicines and no
    medicine count, and drug_chart false so the downtime page draws no
    medicines section (services/drug_chart)."""
    counts = {k: v for k, v in (snapshot.get("counts") or {}).items() if k != "medications"}
    return {**snapshot, "medications": [], "counts": counts, "drug_chart": False}


def _envelope(row) -> dict:
    """Turn a patient_snapshots row into an API envelope with the parsed snapshot."""
    d = dict(row)
    raw = d.get("snapshot_json")
    try:
        parsed = json.loads(raw) if raw else None
    except (json.JSONDecodeError, TypeError):
        parsed = None
    summary = d.get("summary")
    if isinstance(parsed, dict) and not scoring_enabled():
        # A snapshot taken before scoring was switched off still holds its
        # score. While it is off nothing reads one back: no NEWS2, no stored
        # risk rows, no escalation chain, and the summary is said again
        # without the score.
        parsed = {**parsed, "news2": None, "recent_risk_scores": [], "open_escalations": []}
    if isinstance(parsed, dict) and not drug_chart_enabled():
        # A snapshot taken before the Orb Ward Record was switched on still
        # holds the drug chart. While it is on nothing reads one back.
        parsed = _without_drug_chart(parsed)
    if isinstance(parsed, dict) and (not scoring_enabled() or not drug_chart_enabled()):
        summary = _build_summary(parsed.get("demographics") or {}, parsed.get("allergies") or [],
                                 parsed.get("problems") or [], parsed.get("medications") or [],
                                 parsed.get("news2"), has_vitals=bool(parsed.get("latest_vitals")))
    return {
        "id": d.get("id"),
        "patient_id": d.get("patient_id"),
        "summary": summary,
        "created_by": d.get("created_by"),
        "created_by_name": d.get("created_by_name"),
        "created_at": d.get("created_at"),
        "snapshot": parsed,
    }


def get_latest_snapshot(patient_id: str) -> dict | None:
    """Return the most recent snapshot envelope for a patient, or None."""
    with db_session() as db:
        row = db.execute(
            """SELECT id, patient_id, snapshot_json, summary, created_by, created_at,
                      (SELECT u.name FROM users u WHERE u.id = patient_snapshots.created_by) AS created_by_name
               FROM patient_snapshots
               WHERE patient_id = ?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (patient_id,),
        ).fetchone()
    return _envelope(row) if row is not None else None


def list_snapshots(patient_id: str, limit: int = 50) -> list[dict]:
    """Return the snapshot history (newest first) for a patient."""
    with db_session() as db:
        rows = db.execute(
            """SELECT id, patient_id, snapshot_json, summary, created_by, created_at,
                      (SELECT u.name FROM users u WHERE u.id = patient_snapshots.created_by) AS created_by_name
               FROM patient_snapshots
               WHERE patient_id = ?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (patient_id, limit),
        ).fetchall()
    return [_envelope(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  Ward-level downtime print pack — one printable HTML artifact per ward
# ══════════════════════════════════════════════════════════════════════════════
#
# The per-patient snapshot answers "what must I know about THIS patient when the
# EHR is down". The drill (docs/DRILLS.md §2) then asked a nurse to print thirty
# of them, one at a time, at the worst possible moment. This closes that gap:
# every downtime sweep tick renders ONE self-contained HTML file per ward —
# census index first, then one page per admitted patient — into the same durable
# local directory as the FHIR self-exports.
#
# THE POINT IS THE FILE, NOT THE ROUTE. The pack exists on local disk BEFORE an
# outage, so it stays retrievable when the app is down: any browser opens
# ward-pack.<ward>.<hash>.latest.html straight off the filesystem. If the whole
# appliance is dead, only paper printed in advance helps — which is why the
# sweep keeps the file perpetually fresh for the ward to print each shift.
#
# Two additions (2026-10-03). A pack asked for through the app is rebuilt
# first when the hourly file is stale or misses a patient admitted since
# (serve_ward_pack), and anyone it still lacks is listed on the first page.
# And because a file only on the appliance dies with it, every latest pack is
# also copied to ORB_WARD_PACK_MIRROR_DIR when that is set, and
# scripts/backup.sh carries the latest packs, encrypted, in the nightly set.
#
# Assembled from the SAME stored snapshots the per-patient downtime view reads
# (get_latest_snapshot), plus two direct reads: code status from the active
# admission and the open escalation chain. The risk figure is the rule-based
# NEWS2 already frozen in each snapshot — no Ollama, no network, no new
# clinical computation. Everything dynamic is html.escape()d: names, notes and
# diagnoses are free text and must never become markup.

_WARD_PACK_PREFIX = "ward-pack"

#: Grouping label for admitted patients whose `patients.ward` is blank.
_UNASSIGNED_WARD = "Unassigned"

# Timestamped packs older than this are pruned by the same sweep that writes
# them. Hourly regeneration × 3 days ≈ 72 packs per ward — enough history to
# reconstruct what the ward was holding around any outage, small enough (tens
# of MB) never to matter on the appliance disk. 0 disables pruning.
_WARD_PACK_RETENTION_DAYS = float(os.getenv("ORB_WARD_PACK_RETENTION_DAYS", "3"))

# Paper budget per patient: how many recent signed notes, and how much of each.
_WARD_PACK_NOTES = 3
_WARD_PACK_NOTE_CHARS = 600

# A pack asked for through the app is rebuilt first when the file on disk is
# older than this (or misses an admitted patient), and any snapshot older than
# this is re-captured for it. The hourly file used to be served as it was, so
# a patient admitted at 09:05 was not in the pack printed at 09:50. Pure SQLite
# either way; the window only bounds how often a busy ward re-captures.
_WARD_PACK_MAX_AGE_S = float(os.getenv("ORB_WARD_PACK_MAX_AGE_MIN", "5")) * 60.0

# How long a pack request waits for a build already in progress before it
# serves the file on disk as it stands (with its missing patients listed).
_WARD_PACK_LOCK_WAIT_S = 30.0

# One build at a time per process: the hourly sweep and on-request rebuilds
# would otherwise capture the same ward twice and race on the same files.
_WARD_PACK_LOCK = threading.Lock()


def ward_pack_mirror_dir() -> str | None:
    """Second destination for every *.latest.html pack, or None.

    The packs exist so the ward has something when the appliance is down, and
    a file that lives only on that appliance is lost with it. Point
    ORB_WARD_PACK_MIRROR_DIR at storage that survives it (a nurse-station PC
    share, a mounted drive) and each build copies the latest pack there too.
    The copies are plaintext PHI, written 0o600, so choose the destination as
    carefully as the appliance itself. Read lazily and stripped, like
    ORB_FHIR_EXPORT_DIR; blank means unset.
    """
    value = os.getenv("ORB_WARD_PACK_MIRROR_DIR", "").strip()
    return value or None


def _ward_pack_path(out_dir, ward: str, stamp: str) -> str:
    """Predictable pack filename: sanitized ward + short hash of the raw ward
    string (same collision-proofing as _bundle_path) + a UTC timestamp, or the
    stable 'latest' stamp the route and the on-disk fallback both read."""
    import hashlib as _hl
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(ward)) or "ward"
    digest = _hl.sha256(str(ward).encode("utf-8")).hexdigest()[:8]
    return os.path.join(str(out_dir), f"{_WARD_PACK_PREFIX}.{safe}.{digest}.{stamp}.html")


def _write_private(path: str, text: str) -> None:
    """Atomic 0o600 write — the same temp-then-replace shape as the FHIR
    bundles, so a reader never sees a half-written pack and the PHI is never
    world-readable, even during the write window. The temp name is unique per
    writer, so two builds of the same ward cannot interleave into one file."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _e(value, dash: str = "—") -> str:
    """HTML-escape one dynamic value for the pack; em-dash for blank."""
    if value is None or value == "":
        return dash
    return html.escape(str(value), quote=True)


def _when(value) -> str | None:
    """A stored UTC instant as the ward's clock ('2 Oct 2026, 13:19 IST').

    The pack is read on paper during downtime, by staff who think in hospital
    time; it used to print raw UTC ISO strings, 5.5 hours off in an IST ward.
    An unreadable value is printed as stored rather than dropped.
    """
    return hospital_stamp(value) or value


# admissions.code_status as the admission form offers it (AdmitPatientModal).
# A value outside the list is printed as stored, never guessed at.
_CODE_STATUS_LABELS = {
    "full_code": "Full code",
    "dnr": "DNR (do not resuscitate)",
    "dni": "DNI (do not intubate)",
    "comfort": "Comfort care only",
}


def _code_status_text(value) -> str:
    if value is None or str(value).strip() == "":
        return "Not recorded"
    raw = str(value).strip()
    return _CODE_STATUS_LABELS.get(raw.lower(), raw.replace("_", " "))


def _person(user_id, names: dict | None) -> str | None:
    """A staff id as the person's name when the users table knows it; an
    unknown id is printed as stored rather than dropped."""
    if not user_id:
        return None
    return (names or {}).get(user_id) or user_id


def _captured_by(created_by, names: dict | None) -> str:
    # 'auto' is the hourly sweep, not a person.
    if not created_by or created_by == "auto":
        return "automatically"
    return f"by {_person(created_by, names)}"


def _staff_names(items: list[dict]) -> dict:
    """{user id: name} for everyone a ward pack names: who captured each
    snapshot and who wrote or signed each printed note. One query."""
    ids: set[str] = set()
    for item in items:
        env = item.get("envelope") or {}
        if env.get("created_by") and env.get("created_by") != "auto":
            ids.add(env["created_by"])
        snap = env.get("snapshot") or {}
        for n in (snap.get("recent_notes") or [])[:_WARD_PACK_NOTES]:
            for key in ("signed_by", "author_id"):
                if n.get(key):
                    ids.add(n[key])
        for d in (snap.get("recent_documents") or [])[:_WARD_PACK_NOTES]:
            if d.get("author_id"):
                ids.add(d["author_id"])
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    with db_session() as db:
        rows = db.execute(f"SELECT id, name FROM users WHERE id IN ({marks})",
                          tuple(ids)).fetchall()
    return {r["id"]: r["name"] for r in rows if r["name"]}


# SpO2 Scale 2 (COPD / chronic type-2 respiratory failure, target 88-92%).
# 2026-10-02: a Scale 2 patient at SpO2 90 on air printed "90%" beside "NEWS2 0
# (stable)" unmarked, and a nurse re-scoring on the default Scale 1 chart adds 3
# points. The band and the SpO2 cell now say which table the score came from.
_SCALE_2_BAND = " · SpO₂ Scale 2"
_SCALE_2_SPO2 = "<br><small>Scale 2, target 88-92%</small>"


def _band_qualifiers_html(news2: dict) -> str:
    """The news2_text clauses after a NEWS2 cell's band, escaped, minus the
    scale (the cell prints its own Scale 2 marker). A nurse re-scoring on paper
    during downtime must see "a single parameter scores 3" and, for a set
    with ACVPU or O2 never assessed, that the printed aggregate is a minimum."""
    return "".join(f" · {_e(q)}" for q in news2_qualifiers(news2, scale=False))


# Monochrome by design — the pack must photocopy and fax cleanly, exactly like
# the app's print.css. One patient per printed page (break-after), a screen-only
# toolbar (hidden by @media print) so the file also works opened from disk.
_WARD_PACK_CSS = """
  * { box-sizing: border-box; }
  body { margin: 0; color: #000; background: #fff;
         font: 13px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  .wp-screenbar { display: flex; justify-content: space-between; align-items: center;
                  gap: 12px; padding: 10px 16px; border-bottom: 2px solid #000; }
  .wp-screenbar button { font: inherit; font-weight: 700; padding: 8px 18px;
                         background: #fff; color: #000; border: 2px solid #000; cursor: pointer; }
  .wp-page { padding: 26px 30px; page-break-after: always; break-after: page; }
  .wp-page:last-child { page-break-after: auto; break-after: auto; }
  h1 { font-size: 20px; margin: 0 0 2px; }
  h2 { font-size: 15px; margin: 0; }
  h3 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin: 14px 0 4px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #000; padding: 4px 7px; text-align: left; vertical-align: top; }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
  .wp-identity { border: 2px solid #000; padding: 8px 12px; margin: 0 0 10px; }
  .wp-identity-grid { display: flex; flex-wrap: wrap; gap: 4px 26px; margin-top: 4px; }
  .wp-identity-grid span b { text-transform: uppercase; font-size: 10px; letter-spacing: 0.05em;
                            display: block; }
  .wp-allergies { border: 4px double #000; padding: 8px 12px; margin: 0 0 10px; }
  .wp-allergies .wp-nkda { font-weight: 700; }
  .wp-allergy { font-weight: 700; }
  .wp-status-line { border: 1px solid #000; padding: 6px 10px; margin: 0 0 10px; }
  .wp-note { border: 1px solid #000; padding: 6px 10px; margin: 0 0 8px; }
  .wp-note-head { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
                  border-bottom: 1px solid #000; padding-bottom: 3px; margin-bottom: 5px; }
  .wp-note-body { white-space: pre-wrap; }
  .wp-unavailable { border: 4px double #000; padding: 14px; font-weight: 700; }
  .wp-away { border: 4px double #000; padding: 8px 12px; margin: 0 0 10px; }
  .wp-meta { font-size: 11px; margin-top: 6px; }
  .wp-missing { border: 4px double #000; }
  .wp-footer { border-top: 1px solid #000; margin-top: 16px; padding-top: 6px;
               font-size: 11px; display: flex; justify-content: space-between; gap: 12px; }
  @media print { .wp-screenbar { display: none; } }
"""

# medications.source values written by AI extraction (Scribe and the agentic
# lane). Same set the downtime screen and the I-PASS sheet flag.
_AI_MED_SOURCES = ("llm_extracted", "agentic")


def _away_words(away: dict) -> str:
    """'on leave' or 'absconded' (adt-11): the one helper's words."""
    from services.adt.clinical_pause import AWAY_WORDS
    return AWAY_WORDS.get(away.get("presence_status") or "", "off the ward")


def _away_html(item: dict) -> str:
    """The NOT ON WARD box for a patient recorded on leave or absconded, or ''
    for a patient on the ward and for every patient while ORB_ADT is off (the
    item then has no 'away' key). The time is the ward's clock (_when). The
    page is kept: a patient missing from a downtime pack is the failure this
    pack exists to prevent, and the bed is still theirs."""
    away = item.get("away")
    if not away:
        return ""
    from services.adt.clinical_pause import pause_reason
    words = _away_words(away)
    since = _when(away.get("presence_since"))
    when = f" since {_e(since)}" if since else ""
    reason = pause_reason(away, medication_round=drug_chart_enabled()) or ""
    return (
        '<div class="wp-away">'
        f"<b>NOT ON WARD: {_e(words)}{when}.</b> The bed is held for them. "
        f"{_e(reason)}</div>"
    )


def _med_verification_html(m: dict) -> str:
    """A line under an AI-extracted drug saying whether a pharmacist checked
    it. On paper an unverified extraction must never pass for a prescription."""
    if str(m.get("source") or "").lower() not in _AI_MED_SOURCES:
        return ""
    if "verified_by" not in m:  # a snapshot from before verified_by was kept
        return "<br><small>AI-extracted, verification not shown in this copy: check the chart</small>"
    if m.get("verified_by"):
        return "<br><small>AI-extracted, verified by a pharmacist</small>"
    return "<br><small><b>AI-extracted, not verified by a pharmacist</b></small>"


def _clip(text, limit: int = _WARD_PACK_NOTE_CHARS) -> tuple[str, bool]:
    body = str(text or "")
    return (body[:limit], True) if len(body) > limit else (body, False)


def _render_labs(snap: dict) -> str:
    """Recent results: interfaced analytes as a table, then Orb's own lab
    reports, clipped to the paper budget."""
    if "lab_results" not in snap and "recent_lab_reports" not in snap:
        return "<p><i>Not included in this snapshot. Check the chart.</i></p>"
    results = snap.get("lab_results") or []
    reports = (snap.get("recent_lab_reports") or [])[:_WARD_PACK_NOTES]
    if not results and not reports:
        return "<p><i>No recent lab results recorded.</i></p>"
    out = ""
    if results:
        rows = ""
        for r in results:
            # The lab's own text when it sent any ('1.20', '<0.01'), else the
            # number: the one display rule (lab_observations.display_value).
            shown = lab_observations.display_value(r)
            rows += (
                f"<tr><td>{_e(r.get('observation_name'), dash='Unnamed test')}</td>"
                f"<td>{_e(shown, dash='Not recorded')} {_e(r.get('unit'), dash='')}</td>"
                f"<td>{_e(r.get('reference_range'), dash='')}</td>"
                f"<td>{_e(r.get('abnormal_flag'), dash='')}</td>"
                f"<td>{_e(_when(r.get('observed_at')), dash='Not recorded')}</td></tr>"
            )
        out += ("<table><thead><tr><th>Test</th><th>Result</th><th>Range</th>"
                f"<th>Flag</th><th>Taken</th></tr></thead><tbody>{rows}</tbody></table>")
    for rep in reports:
        body, truncated = _clip(rep.get("content"))
        out += (
            '<div class="wp-note"><div class="wp-note-head">'
            f"{_e(rep.get('title'), dash='Lab report')} · "
            f"{_e(_when(rep.get('created_at')), dash='time not recorded')}</div>"
            f'<div class="wp-note-body">{_e(body, dash="No text recorded.")}'
            f"{' … [truncated: full report in the chart]' if truncated else ''}"
            "</div></div>"
        )
    return out


def _render_documents(snap: dict, names: dict | None) -> str:
    """Recent notes written in the chart (clinical_documents)."""
    if "recent_documents" not in snap:
        return "<p><i>Not included in this snapshot. Check the chart.</i></p>"
    docs = (snap.get("recent_documents") or [])[:_WARD_PACK_NOTES]
    if not docs:
        return "<p><i>No recent chart notes.</i></p>"
    out = ""
    for d in docs:
        body, truncated = _clip(d.get("content"))
        out += (
            '<div class="wp-note"><div class="wp-note-head">'
            f"{_e(d.get('title') or d.get('doc_type'), dash='Note')} · "
            f"{_e(_person(d.get('author_id'), names), dash='author not recorded')} · "
            f"{_e(_when(d.get('created_at')), dash='time not recorded')}</div>"
            f'<div class="wp-note-body">{_e(body, dash="No text recorded.")}'
            f"{' … [truncated: full note in the per-patient snapshot]' if truncated else ''}"
            "</div></div>"
        )
    return out


def _render_vitals_news2(snap: dict) -> str:
    """The latest-vitals row + NEWS2 band as one table row."""
    lv = snap.get("latest_vitals")
    if not lv:
        return "<p><i>No vitals recorded at capture time.</i></p>"
    # Scoring switched off: no NEWS2 column at all, whatever the snapshot holds.
    scoring = scoring_enabled()
    news2 = (snap.get("news2") or {}) if scoring else {}
    band = (
        f"{_e(news2.get('raw_news2'))} ({_e(news2.get('level'))})"
        if news2.get("raw_news2") is not None else "—"
    )
    scale_2 = news2.get("spo2_scale") == 2
    if scale_2 and news2.get("raw_news2") is not None:
        band += _SCALE_2_BAND
    if news2.get("raw_news2") is not None:
        band += _band_qualifiers_html(news2)
    # AS CHARTED (services/vital_charting.py): the numbers the NEWS2 in the
    # last column was scored on. A raw "28.2" is not a value on the paper
    # chart a nurse re-scores from during downtime; "28" is.
    def _v(key, dp=0, unit=""):
        return _e(charted_text(lv.get(key), dp, unit=unit, missing=""))

    spo2 = _v("spo2", unit="%") + (" on O₂" if lv.get("on_oxygen") else "")
    if scale_2:
        spo2 += _SCALE_2_SPO2
    return (
        "<table><thead><tr><th>HR</th><th>BP</th><th>SpO₂</th><th>RR</th>"
        "<th>Temp</th><th>ACVPU</th>"
        + ("<th>NEWS2</th>" if scoring else "")
        + "<th>Recorded</th></tr></thead>"
        "<tbody><tr>"
        f"<td>{_v('heart_rate')}</td>"
        f"<td>{_v('systolic_bp')}/{_v('diastolic_bp')}</td>"
        f"<td>{spo2}</td>"
        f"<td>{_v('respiratory_rate')}</td>"
        f"<td>{_v('temperature', 1)}</td>"
        f"<td>{_e(lv.get('consciousness'))}</td>"
        + (f"<td><b>{band}</b></td>" if scoring else "")
        + f"<td>{_e(_when(lv.get('timestamp')))}</td>"
        "</tr></tbody></table>"
    )


def _render_patient_page(item: dict, index: int, total: int,
                         ward: str, generated_at: str, names: dict | None = None) -> str:
    """One admitted patient as one printed page. `item` carries the patients row
    (identity), the snapshot envelope (or None), code_status and escalation.
    `names` maps staff ids to names (_staff_names)."""
    env = item.get("envelope")
    name = _e(item.get("name"), dash="Unknown patient")
    footer = (
        f'<div class="wp-footer"><span>Ward: {_e(ward)} · Patient {index} of {total}</span>'
        f"<span>Orb downtime print pack · generated {_e(_when(generated_at))}</span></div>"
    )
    # Marks whose page this is, so a pack served from disk can be checked for
    # admitted patients it does not cover (_pack_patient_marker).
    section = f'<section class="wp-page">{_pack_patient_marker(item.get("id"))}'
    away_html = _away_html(item)

    if env is None or not env.get("snapshot"):
        return (
            f"{section}"
            f"<h2>{name} · MRN {_e(item.get('mrn'))} · Bed {_e(item.get('bed'))}</h2>"
            f"{away_html}"
            '<p class="wp-unavailable">SNAPSHOT UNAVAILABLE for this patient. '
            "Use the paper chart and the bedside record. This pack carries no "
            "clinical data for them.</p>"
            f"{footer}</section>"
        )

    snap = env["snapshot"]
    demo = snap.get("demographics") or {}
    allergies = snap.get("allergies") or []
    meds = snap.get("medications") or []
    problems = snap.get("problems") or []
    notes = (snap.get("recent_notes") or [])[:_WARD_PACK_NOTES]

    if allergies:
        allergy_html = "".join(
            f'<div class="wp-allergy">{_e(a.get("substance"))}'
            f'{", " + _e(a.get("severity")) if a.get("severity") else ""}'
            f'{", " + _e(a.get("reaction")) if a.get("reaction") else ""}</div>'
            for a in allergies
        )
    else:
        # Never "NKDA": an empty list is also a patient nobody has asked yet.
        allergy_html = '<div class="wp-nkda">No allergies recorded. Confirm with the patient.</div>'

    scoring = scoring_enabled()
    esc = item.get("escalation") if scoring else None
    if esc:
        esc_text = (
            f"OPEN ({_e(esc.get('status'))}), tier {_e(esc.get('current_tier'))}"
            f"/{_e(esc.get('max_tier'))}"
            f"{', ' + _e(esc.get('tier_label') or esc.get('tier_role')) if (esc.get('tier_label') or esc.get('tier_role')) else ''}"
            f", raised {_e(_when(esc.get('raised_at')))}"
        )
    else:
        esc_text = "none open at capture time"

    if meds:
        meds_html = (
            "<table><thead><tr><th>Drug</th><th>Dose</th><th>Route</th>"
            "<th>Frequency</th><th>Status</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{_e(m.get('drug_name'))}{_med_verification_html(m)}</td>"
                f"<td>{_e(m.get('dosage'))}</td>"
                f"<td>{_e(m.get('route'))}</td><td>{_e(m.get('frequency'))}</td>"
                f"<td>{_e(m.get('status'))}</td></tr>"
                for m in meds
            )
            + "</tbody></table>"
        )
    else:
        meds_html = "<p><i>No active medications recorded.</i></p>"

    problems_html = (
        "; ".join(
            _e(p.get("problem")) + (f" [{_e(p.get('icd10_code'))}]" if p.get("icd10_code") else "")
            for p in problems
        )
        if problems else "<i>No active problems recorded.</i>"
    )

    if notes:
        notes_html = ""
        for n in notes:
            body = str(n.get("structured_note") or "")
            truncated = len(body) > _WARD_PACK_NOTE_CHARS
            if truncated:
                body = body[:_WARD_PACK_NOTE_CHARS]
            notes_html += (
                '<div class="wp-note"><div class="wp-note-head">'
                f"{_e((n.get('note_format') or 'note').upper())} · "
                f"{_e(_person(n.get('signed_by') or n.get('author_id'), names))} · "
                f"{_e(_when(n.get('signed_at') or n.get('created_at')))}</div>"
                f'<div class="wp-note-body">{_e(body)}'
                f"{' … [truncated: full note in the per-patient snapshot]' if truncated else ''}"
                "</div></div>"
            )
    else:
        notes_html = "<p><i>No recent signed notes.</i></p>"

    return (
        f"{section}"
        '<div class="wp-identity">'
        f"<h2>{_e(demo.get('name'), dash='Unknown patient')}</h2>"
        '<div class="wp-identity-grid">'
        f"<span><b>MRN</b>{_e(demo.get('mrn'))}</span>"
        f"<span><b>Age / Sex</b>{_e(demo.get('age'))} / {_e(demo.get('sex'))}</span>"
        f"<span><b>Ward / Bed</b>{_e(demo.get('ward'))} / {_e(demo.get('bed'))}</span>"
        f"<span><b>Diagnosis</b>{_e(demo.get('diagnosis'))}</span>"
        f"<span><b>Attending</b>{_e(demo.get('attending_doctor_name'))}</span>"
        f"<span><b>Admitted</b>{_e(demo.get('admission_date'))}</span>"
        "</div></div>"
        f"{away_html}"
        f'<div class="wp-allergies"><h3>Allergies</h3>{allergy_html}</div>'
        '<div class="wp-status-line">'
        f"<b>Code status:</b> {_e(_code_status_text(item.get('code_status')))}"
        + (f" &nbsp;·&nbsp; <b>Escalation:</b> {esc_text}" if scoring else "")
        + "</div>"
        f"<h3>{'Latest vitals + NEWS2' if scoring else 'Latest vitals'}</h3>{_render_vitals_news2(snap)}"
        # On an Orb Ward Record computer the drug chart stays on paper: the
        # pack has no medicines section at all (services/drug_chart).
        + (f"<h3>Active medications ({len(meds)})</h3>{meds_html}" if drug_chart_enabled() else "")
        + f"<h3>Active problems ({len(problems)})</h3><p>{problems_html}</p>"
        f"<h3>Recent lab results</h3>{_render_labs(snap)}"
        f"<h3>Recent chart notes</h3>{_render_documents(snap, names)}"
        f"<h3>Recent signed notes</h3>{notes_html}"
        f'<div class="wp-meta">Snapshot captured {_e(_when(env.get("created_at")))} '
        f"{_e(_captured_by(env.get('created_by'), names))}. Anything after that time is NOT in this pack.</div>"
        f"{footer}</section>"
    )


def _pack_patient_marker(patient_id) -> str:
    """The comment that opens each patient page and records whose page it is.
    A comment, not an attribute: it changes nothing a reader or printer sees."""
    return f"<!-- orb-pack-patient: {_e(patient_id, dash='')} -->"


def _missing_from_pack(doc: str, admitted: list[dict]) -> list[dict]:
    """Admitted patients of the ward that have no page in this pack."""
    if "<!-- orb-pack-patient:" not in doc:
        # A pack written before the markers existed: its identity grid (or
        # its SNAPSHOT UNAVAILABLE heading) still prints the MRN.
        return [p for p in admitted
                if not p.get("mrn")
                or (f"<b>MRN</b>{_e(p['mrn'])}<" not in doc
                    and f"MRN {_e(p['mrn'])} ·" not in doc)]
    return [p for p in admitted if _pack_patient_marker(p.get("id")) not in doc]


def _with_missing_banner(doc: str, missing: list[dict]) -> str:
    """Put a NOT IN THIS PACK page ahead of the census index, listing every
    admitted patient the pack does not cover. A pack served from disk after a
    failed rebuild must say who it leaves out, never leave them out silently."""
    if not missing:
        return doc
    rows = "".join(
        f"<tr><td>{_e(p.get('bed'), dash='not recorded')}</td>"
        f"<td>{_e(p.get('name'), dash='Unknown patient')}</td>"
        f"<td>{_e(p.get('mrn'), dash='not recorded')}</td></tr>"
        for p in missing
    )
    banner = (
        '<section class="wp-page wp-missing">'
        f"<h1>NOT IN THIS PACK: {len(missing)} admitted patient(s)</h1>"
        "<p><b>These patients are on the ward now but have no page in this pack.</b> "
        "Use the paper chart and the bedside record for them.</p>"
        "<table><thead><tr><th>Bed</th><th>Patient</th><th>MRN</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></section>"
    )
    anchor = '<section class="wp-page">'
    if anchor in doc:
        return doc.replace(anchor, banner + anchor, 1)
    return doc.replace("<body>", "<body>" + banner, 1)


def _render_ward_pack(ward: str, items: list[dict], generated_at: str,
                      names: dict | None = None) -> str:
    """The full self-contained pack document for one ward: census index page,
    then one page per admitted patient. Staff are named from `names`, or
    looked up here when the caller did not."""
    total = len(items)
    if names is None:
        try:
            names = _staff_names(items)
        except Exception as e:  # a name lookup must never cost the pack
            logger.warning("[Downtime] ward pack staff names unavailable: %s", e)
            names = {}
    index_rows = ""
    scoring = scoring_enabled()
    for item in items:
        env = item.get("envelope")
        snap = (env or {}).get("snapshot") or {}
        news2 = (snap.get("news2") or {}) if scoring else {}
        band = (
            f"{_e(news2.get('raw_news2'))} ({_e(news2.get('level'))})"
            if news2.get("raw_news2") is not None else "—"
        )
        if news2.get("spo2_scale") == 2 and news2.get("raw_news2") is not None:
            band += _SCALE_2_BAND
        if news2.get("raw_news2") is not None:
            band += _band_qualifiers_html(news2)
        # adt-11: off the ward, said beside the name (only while ORB_ADT is on).
        away = item.get("away")
        away_note = f" <b>(NOT ON WARD: {_e(_away_words(away))})</b>" if away else ""
        index_rows += (
            f"<tr><td>{_e(item.get('bed'))}</td><td>{_e(item.get('name'))}{away_note}</td>"
            f"<td>{_e(item.get('mrn'))}</td>"
            + (f"<td>{band}</td>" if scoring else "")
            + 
            f"<td>{_e(_when((env or {}).get('created_at')), dash='NO SNAPSHOT')}</td></tr>"
        )

    pages = "".join(
        _render_patient_page(item, i + 1, total, ward, generated_at, names)
        for i, item in enumerate(items)
    )
    title = f"Ward downtime print pack: {_e(ward)}"
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
        f"<style>{_WARD_PACK_CSS}</style></head><body>"
        '<div class="wp-screenbar">'
        f"<div><b>{title}</b> · generated {_e(_when(generated_at))} · {total} patient(s)</div>"
        '<button onclick="window.print()">Print</button></div>'
        '<section class="wp-page">'
        f"<h1>{title}</h1>"
        f"<p>Generated {_e(_when(generated_at))} · <b>{total} admitted patient(s)</b> · "
        "point-in-time local copy. Each patient page carries its own snapshot "
        "capture time. Anything after that time is not in this pack.</p>"
        "<table><thead><tr><th>Bed</th><th>Patient</th><th>MRN</th>"
        + ("<th>NEWS2</th>" if scoring else "")
        + f"<th>Snapshot captured</th></tr></thead><tbody>{index_rows}</tbody></table>"
        "</section>"
        f"{pages}</body></html>"
    )


def prune_ward_packs(out_dir=None, retention_days: float | None = None) -> dict:
    """Delete ward packs older than the retention window.

    Timestamped packs accumulate one per ward per sweep tick; 'latest' files are
    rewritten every tick, so their mtime only ages once their ward has no
    admitted patients left — at which point they age out too, which is the
    correct PHI-minimisation outcome (same reasoning as prune_fhir_self_exports).
    """
    out_dir = out_dir if out_dir is not None else fhir_self_export_dir()
    days = _WARD_PACK_RETENTION_DAYS if retention_days is None else retention_days
    if days <= 0:
        return {"removed": 0, "bytes_freed": 0}
    now = time.time()
    removed = 0
    freed = 0
    try:
        names = os.listdir(str(out_dir))
    except OSError:
        return {"removed": 0, "bytes_freed": 0}
    for name in names:
        if not (name.startswith(_WARD_PACK_PREFIX + ".") and name.endswith(".html")):
            continue
        path = os.path.join(str(out_dir), name)
        try:
            if (now - os.path.getmtime(path)) / 86400.0 > days:
                size = os.path.getsize(path)
                os.unlink(path)
                removed += 1
                freed += size
        except OSError as e:
            logger.warning("[Downtime] ward pack prune failed for %s: %s", name, e)
    if removed:
        logger.info("[Downtime] ward pack prune: removed %s stale pack(s) (%.1f MB)",
                    removed, freed / 1e6)
    return {"removed": removed, "bytes_freed": freed}


def build_ward_print_packs(created_by: str = "auto", out_dir=None,
                           retention_days: float | None = None) -> dict:
    """Render one printable HTML pack per ward to durable local disk.

    Called by the hourly downtime sweep right after capture_all_admitted, so the
    packs are always built from snapshots captured seconds earlier. A patient
    with no stored snapshot at all is captured on the fly; if even that fails,
    they still get a page — an explicit SNAPSHOT UNAVAILABLE one, because an
    admitted patient silently missing from a downtime pack is the most dangerous
    failure this artifact can have. Never raises for a single bad patient.
    """
    out_dir = out_dir if out_dir is not None else fhir_self_export_dir()
    mirror = ward_pack_mirror_dir()
    generated_at = _now_iso()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    files: list[str] = []
    mirrored: list[str] = []
    patients = 0
    with _WARD_PACK_LOCK:
        rows, escalations = _admitted_for_packs()
        by_ward, _captured = _pack_items(rows, escalations, created_by)
        for ward, items in by_ward.items():
            try:
                written, copied = _write_ward_pack(out_dir, ward, items,
                                                   generated_at, stamp, mirror)
                files.extend(written)
                mirrored.extend(copied)
                patients += len(items)
            except Exception as e:  # never let one bad ward abort the rest
                logger.warning("[Downtime] ward pack build failed for ward %r: %s", ward, e)

    pruned = prune_ward_packs(out_dir, retention_days)
    if mirror:
        # Same retention in the mirror, so a ward that has emptied ages out
        # there too instead of leaving its last census on a share forever.
        prune_ward_packs(mirror, retention_days)
    logger.info("[Downtime] ward packs: %s ward(s), %s patient(s) -> %s",
                len(by_ward), patients, out_dir)
    return {
        "wards": len(by_ward),
        "patients": patients,
        "files": files,
        "directory": str(out_dir),
        "mirrored": mirrored,
        "mirror_directory": mirror,
        "generated_at": generated_at,
        "pruned": pruned,
    }


def _admitted_for_packs(ward: str | None = None) -> tuple[list[dict], dict]:
    """Admitted patients (every ward, or one) with their active admission's
    code status, plus the open escalation chain per patient."""
    sql = """SELECT p.id, p.name, p.mrn, p.bed, p.ward,
                    (SELECT a.code_status FROM admissions a
                      WHERE a.patient_id = p.id AND a.status = 'active'
                      ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1) AS code_status
             FROM patients p WHERE p.status = 'admitted'"""
    params: tuple = ()
    if ward is not None:
        # The same grouping as `item["ward"] or _UNASSIGNED_WARD` below.
        sql += " AND COALESCE(NULLIF(p.ward, ''), ?) = ?"
        params = (_UNASSIGNED_WARD, ward)
    sql += " ORDER BY p.ward, p.bed, p.id"
    with db_session() as db:
        rows = [_row_to_dict(r) for r in db.execute(sql, params).fetchall()]
        # adt-11 (ruling ADT-S4 (a)): a patient recorded on leave or absconded
        # keeps their page and is marked NOT ON WARD on it and in the census.
        # Only while ORB_ADT is on: with it off no row gains the key and the
        # pack is exactly as before.
        from services.adt.clinical_pause import away_by_patient
        away = away_by_patient([r["id"] for r in rows], db=db)
        if away is not None:
            for r in rows:
                r["away"] = away.get(r["id"])
        escalations = {
            r["patient_id"]: _row_to_dict(r)
            for r in db.execute(
                """SELECT patient_id, status, current_tier, max_tier,
                          tier_role, tier_label, raised_at
                   FROM clinical_escalations WHERE status != 'resolved'""",
            ).fetchall()
        }
    return rows, escalations


def _snapshot_age_s(env: dict | None) -> float | None:
    """Seconds since a snapshot envelope was captured; None if unreadable."""
    raw = (env or {}).get("created_at")
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def _pack_items(rows: list[dict], escalations: dict, created_by: str,
                refresh_older_than_s: float | None = None) -> tuple[dict, int]:
    """Group admitted patients by ward with the snapshot each page prints.

    A patient with no snapshot is captured on the fly. With
    refresh_older_than_s, a snapshot older than that is re-captured too; if
    that fails the older one is kept (its capture time is printed on the page).
    Returns (items by ward, number of snapshots captured here).
    """
    by_ward: dict[str, list[dict]] = {}
    captured = 0
    for item in rows:
        item["escalation"] = escalations.get(item["id"])
        env = get_latest_snapshot(item["id"])
        stale = False
        if env is not None and refresh_older_than_s is not None:
            age = _snapshot_age_s(env)
            stale = age is None or age > refresh_older_than_s
        if env is None or stale:
            try:
                env = capture_snapshot(item["id"], created_by=created_by)
                captured += 1
            except Exception as e:
                logger.warning("[Downtime] ward pack capture failed for %s: %s",
                               item["id"], e)
                if not stale:
                    env = None
        item["envelope"] = env
        by_ward.setdefault(item["ward"] or _UNASSIGNED_WARD, []).append(item)
    return by_ward, captured


def _write_ward_pack(out_dir, ward: str, items: list[dict], generated_at: str,
                     stamp: str, mirror: str | None) -> tuple[list[str], list[str]]:
    """Render one ward's pack and write it (timestamped + latest), then copy
    the latest to the mirror. Returns (files written, mirror copies)."""
    doc = _render_ward_pack(ward, items, generated_at)
    timestamped = _ward_pack_path(out_dir, ward, stamp)
    _write_private(timestamped, doc)
    latest = _ward_pack_path(out_dir, ward, "latest")
    _write_private(latest, doc)
    copied: list[str] = []
    if mirror:
        try:
            if os.path.realpath(mirror) != os.path.realpath(str(out_dir)):
                os.makedirs(mirror, mode=0o700, exist_ok=True)
                target = _ward_pack_path(mirror, ward, "latest")
                _write_private(target, doc)
                copied.append(target)
        except Exception as e:  # the local pack is written; a dead share must not cost it
            logger.warning("[Downtime] ward pack mirror to %s failed for ward %r: %s",
                           mirror, ward, e)
    return [timestamped, latest], copied


def rebuild_ward_pack(ward: str, created_by: str = "auto", out_dir=None,
                      refresh_older_than_s: float | None = None,
                      lock_wait_s: float = _WARD_PACK_LOCK_WAIT_S) -> dict | None:
    """Rebuild one ward's pack now, from snapshots no older than
    refresh_older_than_s (older ones are re-captured; pure SQLite, no model).

    Returns None when another build held the lock past lock_wait_s; the caller
    then serves the file on disk as it stands.
    """
    out_dir = out_dir if out_dir is not None else fhir_self_export_dir()
    if not _WARD_PACK_LOCK.acquire(timeout=lock_wait_s):
        logger.warning("[Downtime] ward pack rebuild for %r skipped: a build is running", ward)
        return None
    try:
        generated_at = _now_iso()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        rows, escalations = _admitted_for_packs(ward)
        by_ward, captured = _pack_items(rows, escalations, created_by, refresh_older_than_s)
        items = by_ward.get(ward) or []
        files: list[str] = []
        mirrored: list[str] = []
        if items:
            files, mirrored = _write_ward_pack(out_dir, ward, items, generated_at,
                                               stamp, ward_pack_mirror_dir())
        return {"ward": ward, "patients": len(items), "captured": captured,
                "files": files, "mirrored": mirrored, "generated_at": generated_at}
    finally:
        _WARD_PACK_LOCK.release()


def read_latest_ward_pack(patient_id: str) -> tuple[str, str | None]:
    """(ward, pack html) for the ward this patient occupies, exactly as it is
    on disk; html is None when no pack exists yet. Raises ValueError for an
    unknown patient id: the route maps that to the same 404 the scope
    refusal produces."""
    with db_session() as db:
        row = db.execute(
            "SELECT ward FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"Patient not found: {patient_id}")
    ward = row["ward"] or _UNASSIGNED_WARD
    path = _ward_pack_path(fhir_self_export_dir(), ward, "latest")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return ward, f.read()
    except OSError:
        return ward, None


def serve_ward_pack(patient_id: str, created_by: str = "auto",
                    max_age_s: float | None = None, *, user: dict | None = None) -> dict:
    """The ward pack for this patient's ward, current enough to print.

    The file on disk is served as it is only when it is younger than
    max_age_s (default ORB_WARD_PACK_MAX_AGE_MIN) AND has a page for every
    patient admitted to the ward right now. Otherwise the ward is rebuilt
    first. Whatever is served, any admitted patient it still lacks (a failed
    rebuild, a busy lock) is listed on a NOT IN THIS PACK page at the front.

    Returns {"ward", "html" (None when there is no pack at all), "rebuilt",
    "captured", "missing"}. Raises ValueError for an unknown patient id.
    """
    if user is not None:
        from fastapi import HTTPException
        from permissions import enforce_patient_scope
        enforce_patient_scope(user, patient_id)
        with db_session() as db:
            anchor = db.execute("SELECT ward FROM patients WHERE id = ?", (patient_id,)).fetchone()
        if anchor is None:
            raise ValueError("Patient not found")
        ward = anchor["ward"] or _UNASSIGNED_WARD
        rows, escalations = _admitted_for_packs(ward)
        allowed = []
        for row in rows:
            try:
                enforce_patient_scope(user, row["id"])
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            else:
                allowed.append(row)
        groups, captured = _pack_items(allowed, escalations, created_by,
                                       _WARD_PACK_MAX_AGE_S if max_age_s is None else max_age_s)
        items = groups.get(ward, [])
        # No disk cache shared across users. Existing durable packs stay for
        # the explicitly offline operator workflow, never this authenticated read.
        doc = _render_ward_pack(ward, items, _now_iso()) if items else None
        return {"ward": ward, "html": doc, "rebuilt": bool(items),
                "captured": captured, "missing": []}
    max_age = _WARD_PACK_MAX_AGE_S if max_age_s is None else max_age_s
    ward, doc = read_latest_ward_pack(patient_id)
    path = _ward_pack_path(fhir_self_export_dir(), ward, "latest")
    admitted, _ = _admitted_for_packs(ward)

    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        age = None
    needs_rebuild = (doc is None or age is None or age > max_age
                     or bool(_missing_from_pack(doc, admitted)))

    rebuilt = False
    captured = 0
    if needs_rebuild:
        try:
            result = rebuild_ward_pack(ward, created_by=created_by,
                                       refresh_older_than_s=max_age)
        except Exception as e:  # the file on disk is still worth serving
            logger.warning("[Downtime] ward pack rebuild failed for %r: %s", ward, e)
            result = None
        if result and result["files"]:
            rebuilt = True
            captured = result["captured"]
            _, doc = read_latest_ward_pack(patient_id)
            admitted, _ = _admitted_for_packs(ward)

    if doc is None:
        return {"ward": ward, "html": None, "rebuilt": rebuilt,
                "captured": captured, "missing": []}
    missing = _missing_from_pack(doc, admitted)
    return {"ward": ward, "html": _with_missing_banner(doc, missing),
            "rebuilt": rebuilt, "captured": captured, "missing": missing}
