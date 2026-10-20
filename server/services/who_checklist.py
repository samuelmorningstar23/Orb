"""Orb — WHO Surgical Safety Checklist.

The WHO Surgical Safety Checklist (2009) is the global standard for OR safety and
a hard requirement for any credible surgical module. It has three phases:

  1. Sign In      — before induction of anaesthesia
  2. Time Out     — before skin incision
  3. Sign Out     — before the patient leaves the OR

Completion state is persisted as JSON on ``surgeries.who_checklist``: a map
of item id -> {checked: bool, by: user_id, at: iso8601}. The canonical template is
version-stamped so a completed checklist records which template it was filled
against.

It has its own column since migration 76. It used to share
``surgeries.surgical_checklist`` with the AI prep package, which writes a LIST of
prep steps there: the Surgical Suite crashed on the object this module wrote, and
a prep package landing after a tick wiped the WHO record. A row still holding the
old shape (written in the moment between the migration and this code, or brought
back by an old backup) is read here as WHO state and split on its next write.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, TypedDict

from database import db_session

TEMPLATE_VERSION = "who-ssc-2009"


# Canonical WHO Surgical Safety Checklist items, grouped by phase.
class _ChecklistItem(TypedDict):
    id: str
    text: str


class _ChecklistPhase(TypedDict):
    id: str
    label: str
    when: str
    items: list[_ChecklistItem]


class _ChecklistTemplate(TypedDict):
    version: str
    phases: list[_ChecklistPhase]


WHO_CHECKLIST: _ChecklistTemplate = {
    "version": TEMPLATE_VERSION,
    "phases": [
        {
            "id": "sign_in",
            "label": "Sign In",
            "when": "Before induction of anaesthesia",
            "items": [
                {
                    "id": "si_identity",
                    "text": "Patient has confirmed identity, site, procedure, and consent",
                },
                {"id": "si_site_marked", "text": "Surgical site marked / not applicable"},
                {"id": "si_anaesthesia_check", "text": "Anaesthesia safety check completed"},
                {"id": "si_pulse_oximeter", "text": "Pulse oximeter on patient and functioning"},
                {"id": "si_allergy", "text": "Known allergy reviewed"},
                {
                    "id": "si_airway",
                    "text": "Difficult airway / aspiration risk assessed and equipment/assistance available",
                },
                {
                    "id": "si_blood_loss",
                    "text": "Risk of >500 ml blood loss (7 ml/kg in children): IV access/fluids planned",
                },
            ],
        },
        {
            "id": "time_out",
            "label": "Time Out",
            "when": "Before skin incision",
            "items": [
                {
                    "id": "to_team_intro",
                    "text": "All team members have introduced themselves by name and role",
                },
                {
                    "id": "to_confirm_patient",
                    "text": "Surgeon, anaesthetist and nurse confirm patient, site and procedure",
                },
                {
                    "id": "to_antibiotic",
                    "text": "Antibiotic prophylaxis given within the last 60 minutes / not indicated",
                },
                {
                    "id": "to_critical_events_surgeon",
                    "text": "Surgeon reviews critical/unexpected steps, duration, blood loss",
                },
                {
                    "id": "to_critical_events_anaesthesia",
                    "text": "Anaesthetist reviews patient-specific concerns",
                },
                {
                    "id": "to_critical_events_nursing",
                    "text": "Nursing team confirms sterility, equipment issues",
                },
                {"id": "to_imaging", "text": "Essential imaging displayed / not applicable"},
            ],
        },
        {
            "id": "sign_out",
            "label": "Sign Out",
            "when": "Before patient leaves operating room",
            "items": [
                {
                    "id": "so_procedure_recorded",
                    "text": "Nurse verbally confirms the name of the procedure recorded",
                },
                {"id": "so_counts", "text": "Instrument, sponge and needle counts are correct"},
                {
                    "id": "so_specimen",
                    "text": "Specimen labelled (incl. patient name) / not applicable",
                },
                {
                    "id": "so_equipment_problems",
                    "text": "Any equipment problems identified and addressed",
                },
                {
                    "id": "so_recovery_concerns",
                    "text": "Key concerns for recovery and management reviewed by whole team",
                },
            ],
        },
    ],
}

_ALL_ITEM_IDS = {item["id"] for phase in WHO_CHECKLIST["phases"] for item in phase["items"]}


def get_template() -> _ChecklistTemplate:
    """Return the canonical WHO checklist template."""
    return WHO_CHECKLIST


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_who_payload(raw) -> bool:
    try:
        parsed = json.loads(raw) if raw else None
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and parsed.get("template") == TEMPLATE_VERSION


def who_raw(row) -> str | None:
    """The persisted WHO blob for a surgeries row holding ``who_checklist`` and
    ``surgical_checklist``: the WHO column, or, while it is still empty, a WHO
    payload left in the prep column by code from before migration 76."""
    if row["who_checklist"]:
        return row["who_checklist"]
    legacy = row["surgical_checklist"]
    return legacy if _is_who_payload(legacy) else None


def summarize_state(raw: str | None) -> dict:
    """PURE: turn a persisted ``surgeries.who_checklist`` blob (see ``who_raw``)
    into the checklist state. No database, no I/O.

    Shape: {version, state: {item_id: {checked, by, at}}, completed, progress}.
    Legacy rows may hold a non-WHO checklist (a plain list from older code) —
    those are treated as empty WHO state so this is additive, never destructive.

    Split out of ``get_checklist_state`` so a caller that ALREADY holds the
    surgeries row can evaluate the checklist without opening a second session.
    routes/surgery_routes.complete_surgery is exactly that caller: it decides
    whether to allow the completion inside its own read-then-write transaction,
    and re-reading the row on another connection would both cost a round trip
    and reintroduce the read-outside-the-guard race db_session's docstring
    warns about — the checklist could be un-ticked between the check and the
    UPDATE.

    ``completed`` counts only items whose ``checked`` is truthy; unchecking an
    item deletes its key (see ``set_item``), so a stale key cannot count.
    """
    state: dict[str, Any]
    state = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("template") == TEMPLATE_VERSION:
                state = parsed.get("state", {}) or {}
        except (json.JSONDecodeError, TypeError):
            state = {}
    if not isinstance(state, dict):
        state = {}
    checked = sum(
        1
        for k, v in state.items()
        if k in _ALL_ITEM_IDS and isinstance(v, dict) and v.get("checked")
    )
    total = len(_ALL_ITEM_IDS)
    return {
        "version": TEMPLATE_VERSION,
        "state": state,
        "progress": {"checked": checked, "total": total},
        "completed": checked == total,
    }


def get_checklist_state(surgery_id: str) -> dict:
    """Return the persisted completion state for a surgery. Opens a session."""
    with db_session() as db:
        row = db.execute(
            "SELECT who_checklist, surgical_checklist FROM surgeries WHERE id = ?", (surgery_id,)
        ).fetchone()
    if not row:
        return {"error": "not_found"}
    return _with_attester_names(summarize_state(who_raw(row)))


def _with_attester_names(result: dict) -> dict:
    """Add `by_name` to each attested item: the attester's name from users,
    resolved at READ time so the stored record stays the user id (the audit
    trail's key) and a renamed account reads under its current name. The
    checklist printed "Attested by DOC004". An id no longer on file gets no
    name, and the screen falls back to the id rather than inventing one."""
    state = result.get("state") or {}
    ids = sorted({str(v.get("by")) for v in state.values() if isinstance(v, dict) and v.get("by")})
    if not ids:
        return result
    marks = ",".join("?" for _ in ids)
    with db_session() as db:
        names = {
            r["id"]: r["name"]
            for r in db.execute(f"SELECT id, name FROM users WHERE id IN ({marks})", ids).fetchall()
        }
    for v in state.values():
        if isinstance(v, dict) and v.get("by"):
            v["by_name"] = names.get(str(v["by"]))
    return result


def set_item(surgery_id: str, item_id: str, checked: bool, actor_id: str) -> dict:
    """Check/uncheck a single WHO checklist item and persist it.

    Concurrency-safe: the read-modify-write runs under a single BEGIN IMMEDIATE
    transaction so two team members checking different items at the same time do
    not clobber each other (the previous implementation read on one connection
    and wrote on another, losing the first writer's update).

    Writes ``surgeries.who_checklist`` only, so the prep steps in
    ``surgical_checklist`` are never touched. A WHO payload still sitting in the
    prep column (see the module note) is moved out on this write, and the prep
    list it carried under 'legacy' is put back where it belongs.
    """
    if item_id not in _ALL_ITEM_IDS:
        raise ValueError(f"Unknown WHO checklist item: {item_id}")
    from database import get_db

    conn = get_db()
    # Autocommit off + explicit IMMEDIATE lock so the read sees a state no other
    # writer can change before our commit.
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT who_checklist, surgical_checklist FROM surgeries WHERE id = ?", (surgery_id,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            raise ValueError("Surgery not found")
        raw = who_raw(row)
        try:
            existing = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        state = existing.get("state")
        payload: dict[str, Any] = {
            "template": TEMPLATE_VERSION,
            "state": state if isinstance(state, dict) else {},
        }
        if checked:
            payload["state"][item_id] = {"checked": True, "by": actor_id, "at": _now()}
        else:
            payload["state"].pop(item_id, None)
        if not row["who_checklist"] and raw:
            # The old shape, still in the prep column: split it here.
            legacy = existing.get("legacy")
            conn.execute(
                "UPDATE surgeries SET who_checklist = ?, surgical_checklist = ? WHERE id = ?",
                (
                    json.dumps(payload),
                    json.dumps(legacy) if legacy is not None else None,
                    surgery_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE surgeries SET who_checklist = ? WHERE id = ?",
                (json.dumps(payload), surgery_id),
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return get_checklist_state(surgery_id)
