"""Orb — DIET ORDERS, MEAL PLANS AND THE KITCHEN INDENT.

A hospital feeds people, and what it feeds them is part of their treatment. A
diabetic's carbohydrate load, a renal patient's potassium and salt, a stroke
patient's swallow-safe texture and a pre-operative patient's NIL BY MOUTH are
clinical decisions that arrive at the bedside on a tray. In most Indian
hospitals that chain runs on a paper diet sheet copied by hand into a kitchen
register, and it breaks in four predictable places. This module is built around
those four.

╔══════════════════════════════════════════════════════════════════════════════╗
║ THE FOUR THINGS THIS MODULE EXISTS TO GET RIGHT                              ║
╚══════════════════════════════════════════════════════════════════════════════╝

1. THE ALLERGY ON THE TRAY CARD IS THE LIVE ONE, OR IT SAYS IT COULD NOT BE READ.
   `patient_allergies` is the coded source of truth this codebase already uses
   for order-time drug screening (services/allergy_interlock.read_active_
   allergies). This module READS it and never copies it. There is no allergy
   column on `diet_orders`, migration 25 does not create one, and
   tests/test_dietary.py asserts its absence — because a copy taken at ordering
   time is a copy that is wrong the moment a nurse records a new reaction, and
   the tray card printed from it is confidently wrong.

   And the read is FAIL-CLOSED, exactly as the drug interlock is: if the allergy
   table cannot be read, the row carries `allergies_unavailable: True` and the
   words in ALLERGY_UNAVAILABLE_MESSAGE. It NEVER degrades to an empty list,
   because an empty allergy list and an unreadable one render identically as
   "no known allergies" and mean opposite things.

2. NPO IS TIME-BOUNDED, ATTRIBUTED, AND NEVER SILENTLY LIFTED.
   A patient who is nil by mouth for a theatre list that then gets cancelled is
   the commonest harm in this domain — they go on not eating because the paper
   said NPO and nobody owned the sentence. So:

     * `npo_until` is REQUIRED, enforced by a database CHECK (migration 25), not
       by a validation this service could forget;
     * the ordering clinician is on the row and is surfaced by name on the ward
       view — "who said this patient cannot eat" has an answer;
     * a reason is required, because an NPO nobody can review is an NPO nobody
       lifts;
     * WHEN THE WINDOW EXPIRES, THE DIET DOES NOT RESUME BY ITSELF. The order
       stays NPO and is flagged `npo_expired` for REVIEW. Auto-resuming feeding
       on a timer would be this module deciding, unprompted, that a patient may
       now eat — which is a clinical decision, and one where being wrong means
       aspiration on the way to theatre. The safe automation is to shout, not to
       act.

3. A DISCHARGED PATIENT LEAVES THE KITCHEN COUNT AUTOMATICALLY.
   Not by a hook on the discharge path — this module touches NO clinical write
   path, and a dietary trigger that could fail is a dietary trigger that could
   fail a discharge. Instead, `_active_diet_rows` re-derives the patient's
   CURRENT admission on every read (the same derivation permissions.py uses for
   ward scoping) and counts only patients who are still here. A discharge
   therefore drops out of the ward view, the meal plan and the kitchen count on
   the very next read, with nothing to update and nothing to go wrong.

   The direction of that failure is chosen deliberately. If the two sources ever
   disagree, this module stops feeding rather than keeps feeding: a tray that
   was not cooked is a phone call to the kitchen, whereas a diabetic tray cooked
   for a bed that now holds somebody else is a patient eating another patient's
   diet. Orders left behind by a discharge are not deleted either — they surface
   as `stale_orders` so a dietitian can see and close them, rather than vanishing.

4. THE COUNT ADDS UP, EXACTLY.
   `meal_plans.total_meals` is the sum of `meal_plan_lines.meal_count`, always,
   because both are computed from ONE Counter in `build_meal_plan`. And
   `total_meals + npo_count == order_count`: every active diet order this plan
   saw is either a tray or an NPO, and nothing else. A kitchen count that cannot
   be reconciled to the orders behind it is a count somebody over-cooks by 10%
   forever to be safe.

WHAT THIS MODULE IS NOT
───────────────────────
It is not a rations system. There is no food item master, no recipe, no bill of
materials, no vendor and NO MONEY ANYWHERE — not one paise column, so there is
nothing here to keep in step with services/money.py. A "kitchen indent" here is
a MEAL-COUNT requisition ("cook 14 diabetic soft trays for Ward A lunch"), which
is the thing the ward actually owes the kitchen. Costing those trays against
purchased provisions is a real and separate piece of work, recorded as a
followup rather than faked with invented quantities.

It also does not model tray delivery, feed charting, calorie targets, or
dietitian consults. Each is a genuine gap and each is named in the module's
completeness note rather than stubbed.

NOTHING HERE CAN BLOCK A CLINICAL ACTION. No function in this file is called by
medication ordering, administration, vitals, alerts, shadow capture, the audit
chain, auth or the offline queue. A missing diet order does not stop a
discharge; an unlocked meal plan does not stop anything at all.

CONVENTIONS (same as the rest of server/services)
  * Every function is PURE SYNC and opens its own ``db_session()``. Async callers
    go through ``database.run_db``; blocking DB calls inside ``async def`` are
    banned and tests/test_event_loop_discipline.py enforces it.
  * Never catch stdlib sqlite3 exception classes — sqlcipher3 raises its own
    hierarchy, so they are dead code on the shipping encrypted DB. Import
    DBIntegrityError etc. from db_backend.
  * No foreign keys on these tables (migration 25): a dangling id yields
    "unknown", never a failed clinical write.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from database import db_session
from db_backend import DBIntegrityError
from services.billing_masters import MAIN_FACILITY_ID, next_document_number
from services.hospital_time import hospital_now, hospital_today

logger = logging.getLogger("orb.dietary")

__all__ = [
    "DietaryError",
    "DietOrderError",
    "MealPlanError",
    "KitchenIndentError",
    "DIET_TYPES",
    "NPO",
    "TEXTURES",
    "MEAL_SERVICES",
    "DIET_TYPE_LABELS",
    "TEXTURE_LABELS",
    "MEAL_SERVICE_LABELS",
    "NPO_LABEL",
    "NPO_MAX_WINDOW_HOURS",
    "NPO_LONG_WINDOW_HOURS",
    "ALLERGY_SOURCE_TABLE",
    "ALLERGY_UNAVAILABLE_MESSAGE",
    "NPO_EXPIRY_NOTICE",
    "order_diet",
    "discontinue_diet",
    "get_order",
    "get_active_order",
    "patient_diet_history",
    "ward_view",
    "list_wards",
    "stale_orders",
    "build_meal_plan",
    "lock_meal_plan",
    "get_meal_plan",
    "list_meal_plans",
    "raise_kitchen_indent",
    "issue_kitchen_indent",
    "cancel_kitchen_indent",
    "get_kitchen_indent",
    "list_kitchen_indents",
    "kitchen_board",
    "dietary_summary",
]


# ══════════════════════════════════════════════════════════════════════════════
#  Vocabulary — mirrors migration 25's CHECK constraints exactly
# ══════════════════════════════════════════════════════════════════════════════

NPO = "npo"

DIET_TYPES: tuple[str, ...] = (
    "normal", "diabetic", "renal", "cardiac", "soft", "liquid", NPO,
)

TEXTURES: tuple[str, ...] = (
    "regular", "soft", "minced", "pureed", "liquid", "thickened",
)

MEAL_SERVICES: tuple[str, ...] = ("breakfast", "lunch", "dinner")

#: The words on the screen. Held here rather than in the panel so the server, the
#: kitchen print and the UI cannot drift into three vocabularies for one diet.
DIET_TYPE_LABELS: dict[str, str] = {
    "normal": "Normal / full diet",
    "diabetic": "Diabetic",
    "renal": "Renal",
    "cardiac": "Cardiac / low salt",
    "soft": "Soft diet",
    "liquid": "Liquid diet",
    NPO: "Nil by mouth (NPO)",
}

TEXTURE_LABELS: dict[str, str] = {
    "regular": "Regular",
    "soft": "Soft",
    "minced": "Minced & moist",
    "pureed": "Pureed",
    "liquid": "Liquid",
    "thickened": "Thickened liquid",
}

MEAL_SERVICE_LABELS: dict[str, str] = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "dinner": "Dinner",
}

#: Rendered in capitals wherever an NPO patient appears. NOT an abbreviation on
#: its own: "NPO" is jargon a ward assistant carrying the trolley may not know,
#: and the person who most needs to read it is exactly that person.
NPO_LABEL = "NIL BY MOUTH"

#: Sanity bound on an NPO window. 14 days is far longer than any real
#: pre-operative fast; it exists to catch a mistyped year, not to express a
#: clinical limit. Anything beyond NPO_LONG_WINDOW_HOURS is accepted but FLAGGED
#: for review, because a legitimate long fast (post-operative ileus) and a
#: fat-fingered date look identical in a database and different to a dietitian.
NPO_MAX_WINDOW_HOURS = 24 * 14
NPO_LONG_WINDOW_HOURS = 48

#: The table this module reads allergies FROM. Named as a constant so the fact
#: that there is exactly one source is greppable, and so the test that asserts
#: "no copy" can point at it.
ALLERGY_SOURCE_TABLE = "patient_allergies"

#: The exact words for an allergy list that could not be read. Mirrors the
#: fail-closed posture (and the tone) of services/allergy_interlock.failed_screen.
ALLERGY_UNAVAILABLE_MESSAGE = (
    "Allergies could not be read. This diet has NOT been checked against them. "
    "Do not assume the patient has no allergies."
)

#: What an expired NPO window means, in words, on every payload that carries one.
NPO_EXPIRY_NOTICE = (
    "This NPO window has passed. The diet has NOT resumed automatically, a "
    "clinician must review and write the next order."
)


# ══════════════════════════════════════════════════════════════════════════════
#  Errors
# ══════════════════════════════════════════════════════════════════════════════

class DietaryError(ValueError):
    """Bad dietary input — unknown diet type, unknown patient, impossible dates."""


class DietOrderError(DietaryError):
    """A diet order that cannot stand: NPO with no end, a patient who is not
    admitted, a second active order for the same patient."""


class MealPlanError(DietaryError):
    """A meal-plan operation that would change a count the kitchen is already
    cooking against, or one that refers to a plan that is not there."""


class KitchenIndentError(DietaryError):
    """An indent operation out of order — raised against an unlocked plan,
    issued twice, cancelled after issue."""


# ══════════════════════════════════════════════════════════════════════════════
#  Time helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_ts(value: Any, *, field: str) -> datetime:
    """Parse an ISO-ish timestamp into an AWARE datetime in the hospital's zone.

    A naive input is read as HOSPITAL LOCAL TIME, not UTC. That choice is the
    same one services/hospital_time.py documents for HL7 timestamps and it is
    made for the same reason: a human typing "NPO until 08:00" means eight in the
    morning where the ward is. Reading it as UTC in a UTC+05:30 hospital would
    put the end of a fast five and a half hours into the future — the exact
    timezone bug this codebase has already paid for once.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            raise DietaryError(f"{field} is required")
        text = text.replace("Z", "+00:00")
        # Accept both "2026-08-12T08:00" and "2026-08-12 08:00:00".
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise DietaryError(
                f"{field} must be an ISO timestamp (e.g. 2026-08-12T08:00), got {value!r}"
            )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=hospital_now().tzinfo)
    return dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _stored_ts(dt: datetime) -> str:
    """The form we WRITE. UTC, second precision, no offset suffix — the same
    shape the rest of this schema stores (CURRENT_TIMESTAMP is UTC), so ordering
    comparisons in SQL work against rows written by triggers and by us alike."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _read_ts(value: Any) -> Optional[datetime]:
    """Read a stored timestamp back as an AWARE UTC datetime. Returns None for
    anything unparsable rather than raising — a row with a corrupt timestamp must
    still render (as "unknown"), because refusing to draw the ward view over one
    bad string would hide every OTHER patient's diet."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("[Dietary] unparsable stored timestamp %r", value)
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_date(value: Any, *, field: str = "service_date") -> str:
    """Normalise a service date to YYYY-MM-DD. Defaults to the hospital's today."""
    if value is None or value == "":
        return hospital_today().isoformat()
    text = str(value).strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise DietaryError(f"{field} must be YYYY-MM-DD, got {value!r}")


# ══════════════════════════════════════════════════════════════════════════════
#  Allergies — READ from the existing record, never copied
# ══════════════════════════════════════════════════════════════════════════════

def _read_allergies(db, patient_ids: Iterable[str]) -> dict[str, list[dict]]:
    """Active allergies for a set of patients, from `patient_allergies`.

    ONE bulk query rather than N per-patient calls, because a ward view of 40
    beds would otherwise be 40 round trips on an encrypted SQLite file. The
    PREDICATE is deliberately identical to
    services/allergy_interlock.read_active_allergies — same table, same
    `status = 'active'` filter, same columns — so the tray card and the drug
    interlock can never disagree about what this patient is allergic to. If that
    predicate ever changes, both move together; tests/test_dietary.py pins the
    two to the same fixture.

    Raises on failure. The CALLER decides what an unreadable allergy list means,
    and in this module it always means "say so", never "assume none".
    """
    ids = [pid for pid in dict.fromkeys(patient_ids) if pid]
    if not ids:
        return {}
    out: dict[str, list[dict]] = {pid: [] for pid in ids}
    # Chunked so a large ward cannot blow SQLITE_MAX_VARIABLE_NUMBER.
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = db.execute(
            f"SELECT patient_id, id, substance, substance_class, reaction, severity "
            f"FROM {ALLERGY_SOURCE_TABLE} "
            f"WHERE patient_id IN ({placeholders}) AND status = 'active' "
            f"ORDER BY CASE severity WHEN 'severe' THEN 0 WHEN 'moderate' THEN 1 "
            f"         WHEN 'mild' THEN 2 ELSE 3 END, substance",
            tuple(chunk),
        ).fetchall()
        for row in rows:
            out.setdefault(row["patient_id"], []).append({
                "id": row["id"],
                "substance": row["substance"],
                "substance_class": row["substance_class"],
                "reaction": row["reaction"],
                "severity": row["severity"],
            })
    return out


def _attach_allergies(db, rows: list[dict]) -> None:
    """Put the LIVE allergy list on each row, or say it could not be read.

    Fail-closed as one unit: if the read throws, EVERY row is marked unavailable
    rather than some rows silently carrying an empty list. A partial success here
    would be the worst possible outcome — a screen where "no allergies" means
    "none recorded" on one bed and "the query died" on the next.
    """
    try:
        by_patient = _read_allergies(db, [r["patient_id"] for r in rows])
    except Exception as exc:  # noqa: BLE001 — deliberately broad; see docstring
        logger.error("[Dietary] allergy read FAILED: %s", exc)
        for row in rows:
            row["allergies"] = []
            row["allergies_unavailable"] = True
            row["allergies_message"] = ALLERGY_UNAVAILABLE_MESSAGE
            row["allergy_source"] = ALLERGY_SOURCE_TABLE
        return
    for row in rows:
        row["allergies"] = by_patient.get(row["patient_id"], [])
        row["allergies_unavailable"] = False
        row["allergies_message"] = None
        row["allergy_source"] = ALLERGY_SOURCE_TABLE


# ══════════════════════════════════════════════════════════════════════════════
#  The one definition of "who is being fed"
# ══════════════════════════════════════════════════════════════════════════════
#
# `_ADMITTED_JOIN` re-derives the patient's CURRENT admission exactly as
# permissions.PATIENT_DEPARTMENT_SQL does — most recent active admission, else
# most recent admission. It is written once and used by the ward view, the meal
# plan, the kitchen count and the summary, so those four can never disagree about
# whether a patient is still on the ward.
#
# This is what makes "a discharged patient leaves the count" true without a hook
# on the discharge path.

_ADMITTED_JOIN = """
    FROM diet_orders d
    JOIN patients p ON p.id = d.patient_id
    JOIN admissions a ON a.id = (
        SELECT a2.id FROM admissions a2 WHERE a2.patient_id = d.patient_id
        ORDER BY (a2.status = 'active') DESC, a2.created_at DESC, a2.rowid DESC
        LIMIT 1)
    LEFT JOIN users u ON u.id = d.ordered_by
"""

_ORDER_COLUMNS = """
    d.id, d.patient_id, d.admission_id, d.diet_type, d.texture,
    d.restriction_notes, d.notes, d.npo_reason, d.npo_until, d.status,
    d.ordered_by, d.ordered_at, d.supersedes_order_id, d.superseded_at,
    d.discontinued_by, d.discontinued_at, d.discontinue_reason,
    p.name AS patient_name, p.mrn AS patient_mrn, p.status AS patient_status,
    a.id AS current_admission_id, a.ward AS ward, a.bed AS bed,
    a.department AS department, a.status AS admission_status,
    u.name AS ordered_by_name
"""

#: Still here to be fed: the patient record does not say discharged AND their
#: current admission is active. BOTH, because they are written by the same
#: transaction (services/discharge_service) and a disagreement between them means
#: something went wrong — in which case the safe answer is to stop feeding, not
#: to keep feeding a bed that may now hold somebody else.
_STILL_ADMITTED = "a.status = 'active' AND p.status <> 'discharged'"


def _active_diet_rows(
    db,
    *,
    ward: Optional[str] = None,
    department: Optional[str] = None,
    patient_id: Optional[str] = None,
) -> list[dict]:
    """Every ACTIVE diet order belonging to a patient who is STILL ADMITTED.

    The single source of the kitchen count and of the ward view. `department`
    is the caller's ward scope (permissions.department_scope) — filtering on
    `a.department` here is the same derivation as PATIENT_DEPARTMENT_SQL because
    `a` IS that latest-admission row.
    """
    where = ["d.status = 'active'", _STILL_ADMITTED]
    params: list[Any] = []
    if ward:
        where.append("a.ward = ?")
        params.append(ward)
    if department:
        where.append("a.department = ?")
        params.append(department)
    if patient_id:
        where.append("d.patient_id = ?")
        params.append(patient_id)
    rows = db.execute(
        f"SELECT {_ORDER_COLUMNS} {_ADMITTED_JOIN} WHERE {' AND '.join(where)} "
        f"ORDER BY a.ward, a.bed, p.name",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def _decorate(row: dict, *, now: Optional[datetime] = None) -> dict:
    """Add the derived, display-facing fields — above all the NPO state.

    Everything here is computed, never stored: an `npo_expired` column would be
    a column that is wrong between the moment the window passes and the moment
    something happens to update it, which is exactly the interval in which it
    matters.
    """
    now = now or hospital_now()
    diet_type = row.get("diet_type")
    is_npo = diet_type == NPO
    until = _read_ts(row.get("npo_until")) if is_npo else None
    remaining = (until - now).total_seconds() / 3600.0 if until else None

    row["is_npo"] = is_npo
    row["diet_label"] = DIET_TYPE_LABELS.get(diet_type, diet_type or "unknown")
    row["texture_label"] = TEXTURE_LABELS.get(row.get("texture"), row.get("texture"))
    row["npo_label"] = NPO_LABEL if is_npo else None
    row["npo_until_iso"] = _iso(until)
    row["npo_hours_remaining"] = round(remaining, 2) if remaining is not None else None
    row["npo_expired"] = bool(is_npo and until is not None and until <= now)
    # An NPO whose end time we cannot parse is treated as EXPIRED-equivalent for
    # review purposes: unreviewable is not the same as fine.
    row["npo_end_unknown"] = bool(is_npo and until is None)
    row["npo_long_window"] = bool(
        remaining is not None and remaining > NPO_LONG_WINDOW_HOURS
    )
    row["npo_notice"] = (
        NPO_EXPIRY_NOTICE if (row["npo_expired"] or row["npo_end_unknown"]) else None
    )
    row["needs_review"] = bool(row["npo_expired"] or row["npo_end_unknown"])
    # "Who said this patient cannot eat" — surfaced with a fallback that is
    # honest rather than blank.
    row["ordered_by_display"] = row.get("ordered_by_name") or row.get("ordered_by") or "unknown"
    row["ordered_at_iso"] = _iso(_read_ts(row.get("ordered_at")))
    return row


# ══════════════════════════════════════════════════════════════════════════════
#  Diet orders
# ══════════════════════════════════════════════════════════════════════════════

def _current_admission(db, patient_id: str) -> Optional[dict]:
    row = db.execute(
        "SELECT a.id, a.ward, a.bed, a.department, a.status "
        "FROM admissions a WHERE a.patient_id = ? "
        "ORDER BY (a.status = 'active') DESC, a.created_at DESC, a.rowid DESC LIMIT 1",
        (patient_id,),
    ).fetchone()
    return dict(row) if row else None


def order_diet(
    *,
    patient_id: str,
    diet_type: str,
    ordered_by: str,
    texture: str = "regular",
    restriction_notes: Optional[str] = None,
    notes: Optional[str] = None,
    npo_reason: Optional[str] = None,
    npo_until: Optional[str] = None,
) -> dict:
    """Write a diet order for an ADMITTED patient, superseding any current one.

    ONE ACTIVE ORDER PER PATIENT, and the change is a SUPERSESSION, not an
    UPDATE: the previous row moves to 'superseded' and a new row is written that
    points back at it. The history of what this patient was fed, and on whose
    word, is therefore intact — which is what a dietitian asks for after somebody
    was given the wrong tray.

    The whole thing runs in ONE `db_session(immediate=True)`. The write lock is
    taken before the SELECT that finds the current order, because this is a
    read-then-decide-then-write guard and database.py's own note says that is the
    only way one holds in this codebase. Two clinicians changing a diet at the
    same second must not both read "no active order" and both insert.
    """
    patient_id = (patient_id or "").strip()
    if not patient_id:
        raise DietOrderError("patient_id is required")
    if diet_type not in DIET_TYPES:
        raise DietOrderError(
            f"unknown diet type {diet_type!r}; expected one of {', '.join(DIET_TYPES)}"
        )
    if texture not in TEXTURES:
        raise DietOrderError(
            f"unknown texture {texture!r}; expected one of {', '.join(TEXTURES)}"
        )
    ordered_by = (ordered_by or "").strip()
    if not ordered_by:
        # Not a formality. An NPO order with no author is an NPO nobody can
        # question, and that is the failure mode this module exists to prevent.
        raise DietOrderError("ordered_by is required: a diet order must name its clinician")

    is_npo = diet_type == NPO
    npo_until_stored: Optional[str] = None
    npo_reason_clean = (npo_reason or "").strip() or None

    if is_npo:
        if not npo_reason_clean:
            raise DietOrderError(
                "an NPO order must carry a reason (e.g. 'for OT 08:00', 'aspiration risk'): "
                "an NPO nobody can review is an NPO nobody lifts"
            )
        if npo_until in (None, ""):
            raise DietOrderError(
                "an NPO order must carry npo_until, nil by mouth with no end time is "
                "how a patient stays starved after a cancelled theatre list"
            )
        until = _parse_ts(npo_until, field="npo_until")
        now = hospital_now()
        if until <= now:
            raise DietOrderError(
                f"npo_until must be in the future (got {until.isoformat()}, now {now.isoformat()})"
            )
        if until > now + timedelta(hours=NPO_MAX_WINDOW_HOURS):
            raise DietOrderError(
                f"npo_until is more than {NPO_MAX_WINDOW_HOURS // 24} days away "
                f"({until.date().isoformat()}): check the date"
            )
        npo_until_stored = _stored_ts(until)
        # Texture is meaningless for a patient who is not eating; normalising it
        # keeps the kitchen count's (diet_type, texture) key from splintering
        # NPO across six rows that all mean "no tray".
        texture = "regular"
    else:
        if npo_until not in (None, "") or npo_reason_clean:
            raise DietOrderError(
                "npo_until / npo_reason belong to an NPO order only. "
                f"This order is {diet_type!r}"
            )

    with db_session(immediate=True) as db:
        patient = db.execute(
            "SELECT id, name, status FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        if patient is None:
            raise DietOrderError(f"unknown patient {patient_id!r}")

        admission = _current_admission(db, patient_id)
        if admission is None or admission["status"] != "active" or patient["status"] == "discharged":
            # A diet order for somebody who is not on a ward has no tray to
            # arrive on. Refusing is honest; accepting would put a row in the
            # table that the kitchen count would then correctly ignore, and the
            # clinician would never learn why their order had no effect.
            raise DietOrderError(
                f"{patient_id} is not currently admitted: a diet order needs an active admission"
            )

        current = db.execute(
            "SELECT id FROM diet_orders WHERE patient_id = ? AND status = 'active'",
            (patient_id,),
        ).fetchone()
        now_stored = _stored_ts(hospital_now())

        if current is not None:
            db.execute(
                "UPDATE diet_orders SET status = 'superseded', superseded_at = ? "
                "WHERE id = ? AND status = 'active'",
                (now_stored, int(current["id"])),
            )

        try:
            cur = db.execute(
                """INSERT INTO diet_orders (
                    patient_id, admission_id, diet_type, texture, restriction_notes,
                    notes, npo_reason, npo_until, status, ordered_by, ordered_at,
                    supersedes_order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (
                    patient_id, admission["id"], diet_type, texture,
                    (restriction_notes or "").strip() or None,
                    (notes or "").strip() or None,
                    npo_reason_clean, npo_until_stored, ordered_by, now_stored,
                    int(current["id"]) if current is not None else None,
                ),
            )
        except DBIntegrityError as exc:
            # The partial unique index caught a racing second order. Report it as
            # a conflict rather than a 500: the caller's screen is simply stale.
            raise DietOrderError(
                f"{patient_id} already has an active diet order. Reload and try again"
            ) from exc

        order_id = int(cur.lastrowid)
        if current is not None:
            db.execute(
                "UPDATE diet_orders SET superseded_by_order_id = ? WHERE id = ?",
                (order_id, int(current["id"])),
            )
        return _load_order(db, order_id)


def discontinue_diet(
    order_id: int, *, discontinued_by: str, reason: Optional[str] = None
) -> dict:
    """Stop a diet order without replacing it.

    Distinct from superseding on purpose. Superseded means "the diet changed";
    discontinued means "there is no diet order for this patient any more", which
    is what happens when a patient goes home or a dietitian closes an order that
    was left behind. Both leave the row in place.
    """
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM diet_orders WHERE id = ?", (int(order_id),)).fetchone()
        if row is None:
            raise DietOrderError(f"diet order {order_id} not found")
        if row["status"] != "active":
            raise DietOrderError(
                f"diet order {order_id} is already {row['status']}, nothing to discontinue"
            )
        db.execute(
            "UPDATE diet_orders SET status = 'discontinued', discontinued_by = ?, "
            "discontinued_at = ?, discontinue_reason = ? WHERE id = ? AND status = 'active'",
            (
                (discontinued_by or "").strip() or None,
                _stored_ts(hospital_now()),
                (reason or "").strip() or None,
                int(order_id),
            ),
        )
        return _load_order(db, int(order_id))


def _load_order(db, order_id: int) -> dict:
    """One order, hydrated with its patient/bed context, live allergies and NPO
    state. Used by every write so the caller gets back exactly what a read would
    have given them."""
    row = db.execute(
        f"""SELECT {_ORDER_COLUMNS}
            FROM diet_orders d
            JOIN patients p ON p.id = d.patient_id
            LEFT JOIN admissions a ON a.id = (
                SELECT a2.id FROM admissions a2 WHERE a2.patient_id = d.patient_id
                ORDER BY (a2.status = 'active') DESC, a2.created_at DESC, a2.rowid DESC
                LIMIT 1)
            LEFT JOIN users u ON u.id = d.ordered_by
            WHERE d.id = ?""",
        (int(order_id),),
    ).fetchone()
    if row is None:
        raise DietOrderError(f"diet order {order_id} not found")
    out = dict(row)
    _attach_allergies(db, [out])
    return _decorate(out)


def get_order(order_id: int) -> dict:
    """One diet order by id, whatever its status. Exists so a route can learn
    WHICH PATIENT an order belongs to before it acts on it — a ward check that
    runs after the write has already happened is not a ward check."""
    with db_session() as db:
        return _load_order(db, int(order_id))


def get_active_order(patient_id: str) -> Optional[dict]:
    """This patient's current diet order, or None. Allergies come live."""
    with db_session() as db:
        rows = _active_diet_rows(db, patient_id=patient_id)
        if not rows:
            return None
        _attach_allergies(db, rows)
        return _decorate(rows[0])


def patient_diet_history(patient_id: str, *, limit: int = 50) -> list[dict]:
    """Every diet order this patient has held, newest first — the answer to
    "what were they on last week, and who changed it"."""
    with db_session() as db:
        rows = db.execute(
            f"""SELECT {_ORDER_COLUMNS}
                FROM diet_orders d
                JOIN patients p ON p.id = d.patient_id
                LEFT JOIN admissions a ON a.id = (
                    SELECT a2.id FROM admissions a2 WHERE a2.patient_id = d.patient_id
                    ORDER BY (a2.status = 'active') DESC, a2.created_at DESC, a2.rowid DESC
                    LIMIT 1)
                LEFT JOIN users u ON u.id = d.ordered_by
                WHERE d.patient_id = ?
                ORDER BY d.id DESC LIMIT ?""",
            (patient_id, max(1, min(int(limit), 200))),
        ).fetchall()
        out = [dict(r) for r in rows]
        _attach_allergies(db, out)
        return [_decorate(r) for r in out]


# ══════════════════════════════════════════════════════════════════════════════
#  The ward view — where NPO has to be unmissable
# ══════════════════════════════════════════════════════════════════════════════

def list_wards(*, department: Optional[str] = None) -> list[dict]:
    """Wards with at least one currently-admitted patient, and how many of them
    hold a diet order. The gap between the two numbers is the useful figure: it
    is how many patients on that ward nobody has written a diet for."""
    where = ["a.status = 'active'", "p.status <> 'discharged'"]
    params: list[Any] = []
    if department:
        where.append("a.department = ?")
        params.append(department)
    with db_session() as db:
        rows = db.execute(
            f"""SELECT a.ward AS ward,
                       COUNT(*) AS admitted,
                       SUM(CASE WHEN d.id IS NOT NULL THEN 1 ELSE 0 END) AS with_orders,
                       SUM(CASE WHEN d.diet_type = 'npo' THEN 1 ELSE 0 END) AS npo
                FROM patients p
                JOIN admissions a ON a.id = (
                    SELECT a2.id FROM admissions a2 WHERE a2.patient_id = p.id
                    ORDER BY (a2.status = 'active') DESC, a2.created_at DESC, a2.rowid DESC
                    LIMIT 1)
                LEFT JOIN diet_orders d
                       ON d.patient_id = p.id AND d.status = 'active'
                WHERE {' AND '.join(where)}
                GROUP BY a.ward
                ORDER BY a.ward""",
            tuple(params),
        ).fetchall()
    out = []
    for row in rows:
        admitted = int(row["admitted"] or 0)
        with_orders = int(row["with_orders"] or 0)
        out.append({
            "ward": row["ward"] or "(no ward)",
            "admitted": admitted,
            "with_orders": with_orders,
            "without_orders": max(0, admitted - with_orders),
            "npo": int(row["npo"] or 0),
        })
    return out


def ward_view(*, ward: Optional[str] = None, department: Optional[str] = None) -> dict:
    """The nurse's and dietitian's screen for one ward.

    Returns NPO patients as their OWN list as well as inside `rows`. That
    duplication is the point: a panel that has to filter a mixed list to find who
    cannot eat is a panel that will one day render them mixed in, and NPO
    rendered as one row among thirty is NPO that gets missed at 07:00 with a
    trolley in the corridor.

    Also returns `unordered`: admitted patients on this ward with NO diet order
    at all. A ward view that shows only the patients somebody remembered to write
    an order for cannot show you the one they forgot.
    """
    with db_session() as db:
        rows = _active_diet_rows(db, ward=ward, department=department)
        _attach_allergies(db, rows)
        decorated = [_decorate(r) for r in rows]
        unordered = _patients_without_orders(db, ward=ward, department=department)

    npo_rows = [r for r in decorated if r["is_npo"]]
    meal_rows = [r for r in decorated if not r["is_npo"]]
    counts = Counter((r["diet_type"], r["texture"]) for r in meal_rows)
    allergies_unavailable = any(r["allergies_unavailable"] for r in decorated)

    return {
        "ward": ward,
        "department": department,
        "rows": decorated,
        "npo_rows": npo_rows,
        "npo_count": len(npo_rows),
        "npo_needs_review": [r for r in npo_rows if r["needs_review"]],
        "npo_needs_review_count": sum(1 for r in npo_rows if r["needs_review"]),
        "meal_count": len(meal_rows),
        "order_count": len(decorated),
        "unordered": unordered,
        "unordered_count": len(unordered),
        "counts": _counts_payload(counts),
        "allergies_unavailable": allergies_unavailable,
        "allergies_message": ALLERGY_UNAVAILABLE_MESSAGE if allergies_unavailable else None,
        "allergy_source": ALLERGY_SOURCE_TABLE,
        "npo_label": NPO_LABEL,
        "npo_expiry_notice": NPO_EXPIRY_NOTICE,
    }


def _patients_without_orders(
    db, *, ward: Optional[str] = None, department: Optional[str] = None
) -> list[dict]:
    where = ["a.status = 'active'", "p.status <> 'discharged'", "d.id IS NULL"]
    params: list[Any] = []
    if ward:
        where.append("a.ward = ?")
        params.append(ward)
    if department:
        where.append("a.department = ?")
        params.append(department)
    rows = db.execute(
        f"""SELECT p.id AS patient_id, p.name AS patient_name, p.mrn AS patient_mrn,
                   a.ward AS ward, a.bed AS bed, a.department AS department
            FROM patients p
            JOIN admissions a ON a.id = (
                SELECT a2.id FROM admissions a2 WHERE a2.patient_id = p.id
                ORDER BY (a2.status = 'active') DESC, a2.created_at DESC, a2.rowid DESC
                LIMIT 1)
            LEFT JOIN diet_orders d ON d.patient_id = p.id AND d.status = 'active'
            WHERE {' AND '.join(where)}
            ORDER BY a.ward, a.bed, p.name""",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


def stale_orders(*, department: Optional[str] = None) -> list[dict]:
    """Active diet orders whose patient is NO LONGER being fed here.

    The visible other half of "a discharged patient leaves the count". The count
    drops them silently and correctly; this list makes the drop VISIBLE, so a
    dietitian can close the order rather than wonder why a bed vanished from
    their screen. Nothing here is deleted automatically — an order that
    disappeared on its own is an order nobody can audit.
    """
    where = ["d.status = 'active'", f"NOT ({_STILL_ADMITTED})"]
    params: list[Any] = []
    if department:
        where.append("a.department = ?")
        params.append(department)
    with db_session() as db:
        rows = db.execute(
            f"SELECT {_ORDER_COLUMNS} {_ADMITTED_JOIN} WHERE {' AND '.join(where)} "
            f"ORDER BY d.id DESC",
            tuple(params),
        ).fetchall()
        out = [dict(r) for r in rows]
        _attach_allergies(db, out)
    return [_decorate(r) for r in out]


def _counts_payload(counts: Counter) -> list[dict]:
    """A Counter of (diet_type, texture) → the ordered, labelled list every
    surface renders. Sorted deterministically so a plan built twice from the same
    orders produces byte-identical lines."""
    return [
        {
            "diet_type": diet_type,
            "diet_label": DIET_TYPE_LABELS.get(diet_type, diet_type),
            "texture": texture,
            "texture_label": TEXTURE_LABELS.get(texture, texture),
            "meal_count": count,
        }
        for (diet_type, texture), count in sorted(counts.items())
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  Meal plans — the roll-up from diet orders to a kitchen count
# ══════════════════════════════════════════════════════════════════════════════

def build_meal_plan(
    *,
    ward: str,
    meal_service: str,
    generated_by: str,
    service_date: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Roll this ward's live diet orders into a count for one meal service.

    THE ARITHMETIC IS TRUE BY CONSTRUCTION, not by a later check: `total_meals`
    and the lines both come out of ONE `Counter`, so `total == sum(parts)` cannot
    be false unless someone writes a second source. `order_count` is every active
    order the plan SAW, and `total_meals + npo_count == order_count` because an
    order is either a tray or an NPO and there is no third thing.

    A discharged patient is not in `_active_diet_rows`, so they are not in the
    Counter, so they are not in the count. There is no discharge hook and no
    sweep to forget to run.

    Regenerating a DRAFT plan replaces its lines — the ward's population moves
    all morning and a count from 06:00 is not the count at 11:00. Regenerating a
    LOCKED plan is refused: the kitchen is already cooking against it.
    """
    ward = (ward or "").strip()
    if not ward:
        raise MealPlanError("ward is required")
    if meal_service not in MEAL_SERVICES:
        raise MealPlanError(
            f"unknown meal service {meal_service!r}; expected one of {', '.join(MEAL_SERVICES)}"
        )
    day = _as_date(service_date)

    with db_session(immediate=True) as db:
        rows = _active_diet_rows(db, ward=ward)
        npo_rows = [r for r in rows if r["diet_type"] == NPO]
        meal_rows = [r for r in rows if r["diet_type"] != NPO]
        counts = Counter((r["diet_type"], r["texture"]) for r in meal_rows)
        total_meals = sum(counts.values())
        basis_ids = sorted(int(r["id"]) for r in rows)

        existing = db.execute(
            "SELECT id, status FROM meal_plans "
            "WHERE service_date = ? AND meal_service = ? AND ward = ?",
            (day, meal_service, ward),
        ).fetchone()

        now_stored = _stored_ts(hospital_now())
        if existing is not None:
            if existing["status"] == "locked":
                raise MealPlanError(
                    f"the {MEAL_SERVICE_LABELS[meal_service].lower()} plan for {ward} on {day} "
                    f"is locked. The kitchen is cooking against this count. "
                    f"Cancel it and build a new one if it has to change."
                )
            plan_id = int(existing["id"])
            db.execute("DELETE FROM meal_plan_lines WHERE meal_plan_id = ?", (plan_id,))
            db.execute(
                "UPDATE meal_plans SET status = 'draft', total_meals = ?, npo_count = ?, "
                "order_count = ?, basis_order_ids_json = ?, generated_by = ?, "
                "generated_at = ?, notes = ?, cancelled_by = NULL, cancelled_at = NULL "
                "WHERE id = ?",
                (
                    total_meals, len(npo_rows), len(rows), json.dumps(basis_ids),
                    generated_by, now_stored, (notes or "").strip() or None, plan_id,
                ),
            )
        else:
            cur = db.execute(
                """INSERT INTO meal_plans (
                    service_date, meal_service, ward, status, total_meals, npo_count,
                    order_count, basis_order_ids_json, generated_by, generated_at, notes
                ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    day, meal_service, ward, total_meals, len(npo_rows), len(rows),
                    json.dumps(basis_ids), generated_by, now_stored,
                    (notes or "").strip() or None,
                ),
            )
            plan_id = int(cur.lastrowid)

        for line_no, line in enumerate(_counts_payload(counts), start=1):
            db.execute(
                "INSERT INTO meal_plan_lines (meal_plan_id, line_no, diet_type, texture, "
                "meal_count) VALUES (?, ?, ?, ?, ?)",
                (plan_id, line_no, line["diet_type"], line["texture"], line["meal_count"]),
            )

        return _load_meal_plan(db, plan_id)


def lock_meal_plan(plan_id: int, *, locked_by: str) -> dict:
    """Freeze a plan so the kitchen can cook against a count that will not move.

    Locking is a precondition of raising an indent, and that ordering is the
    whole point: an indent raised off a count that is still being regenerated is
    a number nobody can reconcile against the trays that arrived.
    """
    with db_session(immediate=True) as db:
        row = db.execute("SELECT * FROM meal_plans WHERE id = ?", (int(plan_id),)).fetchone()
        if row is None:
            raise MealPlanError(f"meal plan {plan_id} not found")
        if row["status"] == "locked":
            return _load_meal_plan(db, int(plan_id))
        if row["status"] == "cancelled":
            raise MealPlanError(f"meal plan {plan_id} is cancelled and cannot be locked")
        db.execute(
            "UPDATE meal_plans SET status = 'locked', locked_by = ?, locked_at = ? "
            "WHERE id = ? AND status = 'draft'",
            ((locked_by or "").strip() or None, _stored_ts(hospital_now()), int(plan_id)),
        )
        return _load_meal_plan(db, int(plan_id))


def _load_meal_plan(db, plan_id: int) -> dict:
    row = db.execute("SELECT * FROM meal_plans WHERE id = ?", (int(plan_id),)).fetchone()
    if row is None:
        raise MealPlanError(f"meal plan {plan_id} not found")
    plan = dict(row)
    lines = [
        dict(r) for r in db.execute(
            "SELECT * FROM meal_plan_lines WHERE meal_plan_id = ? ORDER BY line_no",
            (int(plan_id),),
        ).fetchall()
    ]
    for line in lines:
        line["diet_label"] = DIET_TYPE_LABELS.get(line["diet_type"], line["diet_type"])
        line["texture_label"] = TEXTURE_LABELS.get(line["texture"], line["texture"])
    plan["lines"] = lines
    plan["meal_service_label"] = MEAL_SERVICE_LABELS.get(
        plan["meal_service"], plan["meal_service"]
    )
    # Computed here as well as stored, so a caller can SEE that the stored total
    # and the lines agree rather than taking the stored figure on trust.
    plan["lines_total"] = sum(int(line["meal_count"]) for line in lines)
    plan["totals_agree"] = plan["lines_total"] == int(plan["total_meals"])
    plan["basis_order_ids"] = json.loads(plan.get("basis_order_ids_json") or "[]")
    plan["indent"] = _live_indent_for_plan(db, int(plan_id))
    return plan


def get_meal_plan(plan_id: int) -> dict:
    with db_session() as db:
        return _load_meal_plan(db, int(plan_id))


def list_meal_plans(
    *,
    service_date: Optional[str] = None,
    ward: Optional[str] = None,
    meal_service: Optional[str] = None,
    limit: int = 60,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if service_date:
        where.append("service_date = ?")
        params.append(_as_date(service_date))
    if ward:
        where.append("ward = ?")
        params.append(ward)
    if meal_service:
        where.append("meal_service = ?")
        params.append(meal_service)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db_session() as db:
        rows = db.execute(
            f"SELECT id FROM meal_plans {clause} "
            f"ORDER BY service_date DESC, ward, meal_service LIMIT ?",
            tuple(params) + (max(1, min(int(limit), 300)),),
        ).fetchall()
        return [_load_meal_plan(db, int(r["id"])) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  Kitchen indents
# ══════════════════════════════════════════════════════════════════════════════

def _live_indent_for_plan(db, plan_id: int) -> Optional[dict]:
    row = db.execute(
        "SELECT id, indent_number, status, total_meals FROM kitchen_indents "
        "WHERE meal_plan_id = ? AND status <> 'cancelled' ORDER BY id DESC LIMIT 1",
        (int(plan_id),),
    ).fetchone()
    return dict(row) if row else None


def raise_kitchen_indent(
    *, meal_plan_id: int, raised_by: str, notes: Optional[str] = None
) -> dict:
    """Turn a LOCKED plan into the requisition the kitchen cooks against.

    The lines are COPIED from the plan rather than recomputed from live orders.
    That is deliberate and it is the difference between an indent and a report:
    the indent is what the ward asked for at the moment it asked, and it must not
    change under the kitchen's feet because a patient was admitted at 11:40.
    """
    with db_session(immediate=True) as db:
        plan = db.execute(
            "SELECT * FROM meal_plans WHERE id = ?", (int(meal_plan_id),)
        ).fetchone()
        if plan is None:
            raise KitchenIndentError(f"meal plan {meal_plan_id} not found")
        if plan["status"] != "locked":
            raise KitchenIndentError(
                f"meal plan {meal_plan_id} is {plan['status']}: lock it first. "
                f"An indent raised off a count that is still moving is a count "
                f"nobody can reconcile against the trays that arrive."
            )
        live = _live_indent_for_plan(db, int(meal_plan_id))
        if live is not None:
            raise KitchenIndentError(
                f"this plan already has indent {live['indent_number'] or live['id']} "
                f"({live['status']}): cancel it before raising another"
            )

        lines = db.execute(
            "SELECT line_no, diet_type, texture, meal_count FROM meal_plan_lines "
            "WHERE meal_plan_id = ? ORDER BY line_no",
            (int(meal_plan_id),),
        ).fetchall()
        total = sum(int(r["meal_count"]) for r in lines)

        cur = db.execute(
            """INSERT INTO kitchen_indents (
                meal_plan_id, service_date, meal_service, ward, status, total_meals,
                raised_by, raised_at, notes
            ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?)""",
            (
                int(meal_plan_id), plan["service_date"], plan["meal_service"], plan["ward"],
                total, (raised_by or "").strip() or None, _stored_ts(hospital_now()),
                (notes or "").strip() or None,
            ),
        )
        indent_id = int(cur.lastrowid)
        for row in lines:
            db.execute(
                "INSERT INTO kitchen_indent_lines (kitchen_indent_id, line_no, diet_type, "
                "texture, meal_count) VALUES (?, ?, ?, ?, ?)",
                (indent_id, int(row["line_no"]), row["diet_type"], row["texture"],
                 int(row["meal_count"])),
            )
        return _load_indent(db, indent_id)


def issue_kitchen_indent(indent_id: int, *, issued_by: str) -> dict:
    """Hand the indent to the kitchen, and allocate its number.

    The number comes from the EXISTING allocator (billing_masters.
    next_document_number) rather than a second sequence of this module's own —
    same machinery as invoices, POs and GRNs, including the financial-year reset
    and the `is_configured` placeholder flag.

    It is allocated at ISSUE, not at draft: a number is a thing people write on
    paper and quote on the phone, and a draft that is edited or abandoned must
    not have consumed one.
    """
    with db_session() as db:
        row = db.execute(
            "SELECT id, status FROM kitchen_indents WHERE id = ?", (int(indent_id),)
        ).fetchone()
        if row is None:
            raise KitchenIndentError(f"kitchen indent {indent_id} not found")
        if row["status"] == "issued":
            return _load_indent(db, int(indent_id))
        if row["status"] != "draft":
            raise KitchenIndentError(
                f"kitchen indent {indent_id} is {row['status']} and cannot be issued"
            )

    # OUTSIDE the session above on purpose: next_document_number takes its own
    # `db_session(immediate=True)`, and nesting a second immediate write session
    # inside one we are holding is how this codebase has deadlocked before.
    # Facility 1's series (multisite-7a): a kitchen indent carries no facility
    # until multisite-7b stamps one.
    allocated = next_document_number("KIND", facility_id=MAIN_FACILITY_ID)

    with db_session(immediate=True) as db:
        # `AND status = 'draft'` makes a racing second issue a NO-OP rather than a
        # second number written over the first: the loser's UPDATE matches zero
        # rows and `_load_indent` below returns the winner's number, which is the
        # one the kitchen already has. The cost of the race is one BURNED sequence
        # number, which is visible in the series and harmless. The alternative —
        # allocating inside the same transaction as the status check — would nest
        # a second `immediate` session inside this one, which is how this codebase
        # has deadlocked before.
        db.execute(
            "UPDATE kitchen_indents SET status = 'issued', indent_number = ?, issued_by = ?, "
            "issued_at = ? WHERE id = ? AND status = 'draft'",
            (
                allocated["number"], (issued_by or "").strip() or None,
                _stored_ts(hospital_now()), int(indent_id),
            ),
        )
        out = _load_indent(db, int(indent_id))
        out["number_series_is_configured"] = bool(allocated.get("is_configured"))
        return out


def cancel_kitchen_indent(
    indent_id: int, *, cancelled_by: str, reason: Optional[str] = None
) -> dict:
    with db_session(immediate=True) as db:
        row = db.execute(
            "SELECT id, status FROM kitchen_indents WHERE id = ?", (int(indent_id),)
        ).fetchone()
        if row is None:
            raise KitchenIndentError(f"kitchen indent {indent_id} not found")
        if row["status"] == "cancelled":
            return _load_indent(db, int(indent_id))
        db.execute(
            "UPDATE kitchen_indents SET status = 'cancelled', cancelled_by = ?, "
            "cancelled_at = ?, cancel_reason = ? WHERE id = ?",
            (
                (cancelled_by or "").strip() or None, _stored_ts(hospital_now()),
                (reason or "").strip() or None, int(indent_id),
            ),
        )
        return _load_indent(db, int(indent_id))


def _load_indent(db, indent_id: int) -> dict:
    row = db.execute(
        "SELECT * FROM kitchen_indents WHERE id = ?", (int(indent_id),)
    ).fetchone()
    if row is None:
        raise KitchenIndentError(f"kitchen indent {indent_id} not found")
    indent = dict(row)
    lines = [
        dict(r) for r in db.execute(
            "SELECT * FROM kitchen_indent_lines WHERE kitchen_indent_id = ? ORDER BY line_no",
            (int(indent_id),),
        ).fetchall()
    ]
    for line in lines:
        line["diet_label"] = DIET_TYPE_LABELS.get(line["diet_type"], line["diet_type"])
        line["texture_label"] = TEXTURE_LABELS.get(line["texture"], line["texture"])
    indent["lines"] = lines
    indent["lines_total"] = sum(int(line["meal_count"]) for line in lines)
    indent["totals_agree"] = indent["lines_total"] == int(indent["total_meals"])
    indent["meal_service_label"] = MEAL_SERVICE_LABELS.get(
        indent["meal_service"], indent["meal_service"]
    )
    return indent


def get_kitchen_indent(indent_id: int) -> dict:
    with db_session() as db:
        return _load_indent(db, int(indent_id))


def list_kitchen_indents(
    *,
    service_date: Optional[str] = None,
    meal_service: Optional[str] = None,
    ward: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 60,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if service_date:
        where.append("service_date = ?")
        params.append(_as_date(service_date))
    if meal_service:
        where.append("meal_service = ?")
        params.append(meal_service)
    if ward:
        where.append("ward = ?")
        params.append(ward)
    if status:
        where.append("status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db_session() as db:
        rows = db.execute(
            f"SELECT id FROM kitchen_indents {clause} "
            f"ORDER BY service_date DESC, ward, meal_service LIMIT ?",
            tuple(params) + (max(1, min(int(limit), 300)),),
        ).fetchall()
        return [_load_indent(db, int(r["id"])) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  The kitchen's own screen — counts, and NOT ONE PATIENT NAME
# ══════════════════════════════════════════════════════════════════════════════

def kitchen_board(
    *, service_date: Optional[str] = None, meal_service: Optional[str] = None
) -> dict:
    """What the kitchen needs: how many of what, for which ward, right now.

    NO PHI. Not a name, not an MRN, not a bed, not an allergy — ward labels and
    integers only. The kitchen does not need to know who is diabetic in order to
    cook fourteen diabetic trays, and the smallest disclosure that does the job
    is the one to ship. (Tray-level cards WITH the patient's name and live
    allergies are a real requirement for the ward assistant who distributes them;
    that is a ward-side surface, and it is recorded as a followup rather than
    smuggled into the kitchen payload.)

    The `live` counts are recomputed from current orders on every call, so the
    kitchen can see when the ward has moved since the indent was raised — the
    `drift` figure is exactly the argument that otherwise happens at the hatch.
    """
    day = _as_date(service_date)
    services = (meal_service,) if meal_service in MEAL_SERVICES else MEAL_SERVICES

    with db_session() as db:
        live_rows = _active_diet_rows(db)
        plans = db.execute(
            "SELECT id FROM meal_plans WHERE service_date = ? "
            "AND meal_service IN (%s) ORDER BY ward, meal_service"
            % ",".join("?" for _ in services),
            (day,) + tuple(services),
        ).fetchall()
        plan_dicts = [_load_meal_plan(db, int(r["id"])) for r in plans]

    by_ward: dict[str, Counter] = {}
    npo_by_ward: Counter = Counter()
    for row in live_rows:
        ward = row.get("ward") or "(no ward)"
        if row["diet_type"] == NPO:
            npo_by_ward[ward] += 1
            continue
        by_ward.setdefault(ward, Counter())[(row["diet_type"], row["texture"])] += 1

    wards = []
    hospital_total = 0
    for ward in sorted(set(by_ward) | set(npo_by_ward)):
        counts = by_ward.get(ward, Counter())
        total = sum(counts.values())
        hospital_total += total
        wards.append({
            "ward": ward,
            "lines": _counts_payload(counts),
            "total_meals": total,
            "npo_count": npo_by_ward.get(ward, 0),
            "order_count": total + npo_by_ward.get(ward, 0),
        })

    plan_payload = []
    for plan in plan_dicts:
        live_total = next(
            (w["total_meals"] for w in wards if w["ward"] == plan["ward"]), 0
        )
        plan_payload.append({
            "id": plan["id"],
            "ward": plan["ward"],
            "meal_service": plan["meal_service"],
            "meal_service_label": plan["meal_service_label"],
            "status": plan["status"],
            "total_meals": plan["total_meals"],
            "npo_count": plan["npo_count"],
            "order_count": plan["order_count"],
            "lines": plan["lines"],
            "indent": plan["indent"],
            # Positive = the ward has more patients now than when this was
            # counted. The number that stops the argument at the hatch.
            "drift": live_total - int(plan["total_meals"]),
        })

    return {
        "service_date": day,
        "meal_services": list(services),
        "wards": wards,
        "hospital_total_meals": hospital_total,
        "hospital_npo_count": sum(npo_by_ward.values()),
        "plans": plan_payload,
        "contains_phi": False,
    }


def dietary_summary(*, department: Optional[str] = None) -> dict:
    """The overview tiles. Every figure is a live count, never a stored one."""
    with db_session() as db:
        rows = _active_diet_rows(db, department=department)
    decorated = [_decorate(dict(r)) for r in rows]
    npo = [r for r in decorated if r["is_npo"]]
    stale = stale_orders(department=department)
    wards = list_wards(department=department)
    return {
        "active_orders": len(decorated),
        "meals": sum(1 for r in decorated if not r["is_npo"]),
        "npo_active": len(npo),
        "npo_needs_review": sum(1 for r in npo if r["needs_review"]),
        "npo_long_window": sum(1 for r in npo if r["npo_long_window"]),
        "stale_order_count": len(stale),
        "admitted_without_order": sum(w["without_orders"] for w in wards),
        "ward_count": len(wards),
        "npo_label": NPO_LABEL,
        "npo_expiry_notice": NPO_EXPIRY_NOTICE,
    }
