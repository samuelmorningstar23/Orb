"""Orb — Clinical Records Service.

Structured allergies and problem list. These are the coded clinical-core records
the product was missing: the free-text `patient_profiles.allergies` narrative is
retained, but `patient_allergies` is the source of truth used for order-time
allergy checking (services/drug_safety_service.py).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from database import db_session
from services.drug_safety_service import classify_drug

logger = logging.getLogger("orb.clinical_records")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Allergies typed outside the allergy form ─────────────────────────────────
# The admission chips and a roster column are free text. Before 2026-10-02 the
# admission chips were kept ONLY in the patient_profiles narrative, so the
# order-time and bedside interlocks (which read patient_allergies) screened
# every newly admitted patient as allergy-free. These helpers turn that text
# into structured rows on the CALLER's open handle, so the allergy commits or
# rolls back with the admission or import that carried it.

# Entries that mean "nothing to record". Charting one would put "nkda" on the
# chart as an allergen.
_NO_ALLERGY_ENTRIES = {
    "nkda", "nka", "nkfa", "nil", "nill", "none", "no", "na", "n/a", "-", "--",
    "unknown", "not known", "nil known", "none known", "not applicable",
    "no allergy", "no allergies", "no known allergy", "no known allergies",
    "no known drug allergy", "no known drug allergies", "nil significant",
}


def allergy_entries(raw: Any) -> list[str]:
    """Allergen names from a list of chips or one free-text cell.

    Splits on ';', ',', '|' and newlines that sit outside brackets, so
    "penicillin; sulfa" is two allergies while "amoxicillin (rash, mild)"
    stays one. Never splits on '/' or '-': "amoxicillin/clavulanate" and
    "co-trimoxazole" are single products. Drops "nothing to record" entries
    and case-insensitive duplicates, keeping the first spelling.
    """
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        depth, part, parts = 0, "", []
        for ch in item:
            if ch in "([{":
                depth += 1
            elif ch in ")]}" and depth:
                depth -= 1
            if depth == 0 and ch in ";,|\n":
                parts.append(part)
                part = ""
            else:
                part += ch
        parts.append(part)
        for p in parts:
            entry = re.sub(r"\s+", " ", p).strip(" .\t\r")
            key = entry.lower()
            if not entry or key in _NO_ALLERGY_ENTRIES or key in seen:
                continue
            seen.add(key)
            out.append(entry)
    return out


def record_allergies_on(db, patient_id: str, substances: Iterable[str], *,
                        source: str, recorded_by: str | None = None,
                        recorded_at: str | None = None) -> list[str]:
    """Write each allergen as an ACTIVE structured allergy, on ``db``.

    Runs on the caller's connection: a second connection inside an open write
    transaction is the documented WAL self-deadlock, and the allergy must not
    outlive an admission that rolled back.

    Add-only. An allergy already on the chart, active or not, is left exactly
    as it is: a clinician who graded it or marked it entered-in-error knows
    more than a free-text chip. Reaction is left blank and severity 'unknown'
    (nothing was recorded), which the interlocks treat as severe. Returns the
    normalised substances newly written.
    """
    written: list[str] = []
    stamp = recorded_at or _now()
    for substance in substances:
        substance_norm = (substance or "").strip().lower()
        if not substance_norm:
            continue
        classes = classify_drug(substance_norm)
        cur = db.execute(
            """INSERT INTO patient_allergies
                   (patient_id, substance, substance_class, reaction, severity,
                    status, source, recorded_by, recorded_at)
               VALUES (?, ?, ?, NULL, 'unknown', 'active', ?, ?, ?)
               ON CONFLICT(patient_id, substance) DO NOTHING""",
            (patient_id, substance_norm, classes[0] if classes else None,
             source, recorded_by, stamp),
        )
        if cur.rowcount:
            written.append(substance_norm)
    return written


def backfill_profile_allergies(db) -> dict:
    """Chart the admission-form allergies that never reached patient_allergies.

    For every patient whose patient_profiles.allergies holds a JSON list (the
    shape admit_patient and the seed write), each entry is added through
    record_allergies_on with source 'admission'. Free-text narratives that
    are not a list are counted and left alone: splitting a sentence such as
    "allergic to penicillin, causes rash" would chart "causes rash" as an
    allergen. Idempotent, and never touches an existing row.
    """
    rows = db.execute(
        "SELECT patient_id, allergies, updated_at FROM patient_profiles "
        "WHERE allergies IS NOT NULL AND TRIM(allergies) NOT IN ('', '[]')"
    ).fetchall()
    added, patients, skipped_free_text = 0, 0, 0
    for r in rows:
        # Positional, so this reads the same under any row factory.
        patient_id, raw, updated_at = r[0], r[1], r[2]
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if not isinstance(parsed, list):
            skipped_free_text += 1
            continue
        written = record_allergies_on(
            db, patient_id, allergy_entries(parsed),
            source="admission", recorded_by=None, recorded_at=updated_at,
        )
        if written:
            added += len(written)
            patients += 1
    if added or skipped_free_text:
        logger.info(
            "Admission allergy backfill: %d allerg(ies) charted for %d patient(s); "
            "%d free-text profile(s) left for a clinician",
            added, patients, skipped_free_text,
        )
    return {"added": added, "patients": patients,
            "skipped_free_text": skipped_free_text}


# ── Allergies ────────────────────────────────────────────────────────────────
def add_allergy(patient_id: str, substance: str, *, reaction: str | None = None,
                severity: str = "unknown", recorded_by: str | None = None,
                source: str = "clinician") -> dict:
    """Add (or reactivate/update) a structured allergy. Idempotent per
    (patient, substance) via the UNIQUE constraint — an upsert."""
    substance_norm = (substance or "").strip().lower()
    if not substance_norm:
        raise ValueError("substance is required")
    if severity not in ("mild", "moderate", "severe", "unknown"):
        severity = "unknown"
    classes = classify_drug(substance_norm)
    substance_class = classes[0] if classes else None

    with db_session() as db:
        db.execute(
            """INSERT INTO patient_allergies
                   (patient_id, substance, substance_class, reaction, severity, status, source, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
               ON CONFLICT(patient_id, substance) DO UPDATE SET
                   reaction=excluded.reaction,
                   severity=excluded.severity,
                   status='active',
                   substance_class=excluded.substance_class,
                   source=excluded.source,
                   recorded_by=excluded.recorded_by,
                   recorded_at=excluded.recorded_at""",
            (patient_id, substance_norm, substance_class, reaction, severity,
             source, recorded_by, _now()),
        )
        row = db.execute(
            "SELECT * FROM patient_allergies WHERE patient_id = ? AND substance = ?",
            (patient_id, substance_norm),
        ).fetchone()
    return dict(row)


def list_allergies(patient_id: str, include_inactive: bool = False) -> list[dict]:
    q = "SELECT * FROM patient_allergies WHERE patient_id = ?"
    if not include_inactive:
        q += " AND status = 'active'"
    q += " ORDER BY recorded_at DESC"
    with db_session() as db:
        return [dict(r) for r in db.execute(q, (patient_id,)).fetchall()]


def deactivate_allergy(allergy_id: int, patient_id: str) -> bool:
    """Deactivate one allergy, only if it belongs to ``patient_id``.

    Bound to the patient so a caller scoped to one ward cannot disarm the
    interlock on another ward's patient by naming their own patient in the path
    and someone else's allergy id.
    """
    with db_session() as db:
        cur = db.execute(
            "UPDATE patient_allergies SET status = 'inactive' WHERE id = ? AND patient_id = ?",
            (allergy_id, patient_id),
        )
        return cur.rowcount > 0


# ── Problem list ─────────────────────────────────────────────────────────────
def add_problem(patient_id: str, problem: str, *, icd10_code: str | None = None,
                snomed_code: str | None = None, onset_date: str | None = None,
                recorded_by: str | None = None, source: str = "clinician") -> dict:
    if not (problem or "").strip():
        raise ValueError("problem is required")
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO problem_list
                   (patient_id, problem, icd10_code, snomed_code, status, onset_date, source, recorded_by, recorded_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (patient_id, problem.strip(), icd10_code, snomed_code, onset_date,
             source, recorded_by, _now()),
        )
        row = db.execute("SELECT * FROM problem_list WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_problems(patient_id: str, include_resolved: bool = False) -> list[dict]:
    q = "SELECT * FROM problem_list WHERE patient_id = ?"
    if not include_resolved:
        q += " AND status = 'active'"
    q += " ORDER BY recorded_at DESC"
    with db_session() as db:
        return [dict(r) for r in db.execute(q, (patient_id,)).fetchall()]


def resolve_problem(problem_id: int, patient_id: str) -> bool:
    """Resolve one problem, only if it belongs to ``patient_id`` (see
    deactivate_allergy for why the patient binding matters)."""
    with db_session() as db:
        cur = db.execute(
            "UPDATE problem_list SET status = 'resolved', resolved_at = ? "
            "WHERE id = ? AND patient_id = ?",
            (_now(), problem_id, patient_id),
        )
        return cur.rowcount > 0
