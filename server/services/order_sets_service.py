"""Orb Hospital OS — Order Sets and Protocols.

A named set is an ordered list of orders (drugs, fluids, labs, imaging,
observations) that a clinician applies in ONE action instead of typing eight
things at 03:00 with a deteriorating patient in front of them. It is the one
medication feature that is genuinely buildable without a licensed drug
database, because it adds no new clinical knowledge — it only removes typing.

FOUR PROPERTIES, IN ORDER OF IMPORTANCE
───────────────────────────────────────

1. **A SET IS A CONVENIENCE, NEVER A BYPASS.** Every order a set produces goes
   through the same gates a hand-typed order does, at the moment it is written:

     * the allergy + interaction screen, through the SINGLE enforcement point
       (``services.medication_safety_gate.enforce_medication_safety``) — not a
       copy of it, not a re-implementation, the same function CPOE calls;
     * the dose guard (``services.drug_safety_service.evaluate_dose``), which
       stops the 10x slip, concentrated potassium by IV push and intrathecal
       vincristine;
     * the discharged-patient check — a protocol cannot be applied to somebody
       the ward has already sent home;
     * terminology validation on every code, through ``orders_service``.

   The failure mode this exists to prevent is the obvious one: a set becomes
   the quiet path around the interlocks, and within a month everybody uses the
   set. So the gates are not "also run" here — the set is REFUSED WHOLE if any
   item is stopped, and nothing is written at all.

2. **REAL ORDERS, NOT A BLACK BOX.** Applying a set creates individual `orders`
   rows in **draft**, one per item, each with its own event trail, each
   separately editable at apply time and separately cancellable or activatable
   afterwards through the existing state machine. There is no "the set charted
   eight things" record with nothing behind it. Draft — not active — is the
   whole point: the clinician still commits each order.

3. **CONFIGURED BY A FILE AN ENGINEER EDITS**, not by an admin screen. Clinical
   protocol is not something a ward changes through a web form during a night
   shift; it is reviewed, versioned and deployed. See
   ``server/site_config/order_sets.json``.

4. **A MALFORMED FILE STOPS THE SERVER.** Loud, at startup, naming the file,
   the line and the field. The alternative — loading nothing and carrying on —
   is far worse: the ward opens the protocol list, finds it empty, and either
   assumes Orb has no protocols or assumes theirs was deliberately removed.
   That is why the load below happens at IMPORT time and is allowed to raise.

WHAT THIS DOES NOT DO
─────────────────────
  * It does not decide WHICH set a patient needs. No auto-application, no
    trigger from a Vigil alert. A human picks the protocol.
  * It has no per-item conditional logic ("if creatinine > X, halve the dose").
    Every item in a set is either applied or excluded by the clinician.
  * It does not schedule. `frequency` is recorded on the order for the ward to
    read; Orb does not generate the future administration times from it.
  * It is not a formulary. `LOCAL` codes in the shipped file are placeholders a
    site replaces with its own catalogue codes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from fastapi import HTTPException

from database import db_session
from services import drug_safety_service, orders_service
from services.medorders import cpoe
from services.audit_service import log_audit_event
from services.medication_safety_gate import (MedicationSafetyBlocked,
                                             enforce_medication_safety)

logger = logging.getLogger("orb.order_sets")


# ══════════════════════════════════════════════════════════════════════════
#  WHERE THE FILE LIVES
# ══════════════════════════════════════════════════════════════════════════
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "site_config" / "order_sets.json"

#: Env override, for sites that keep protocols in their own config repo and for
#: tests. An absolute path.
CONFIG_PATH_ENV = "ORB_ORDER_SETS_FILE"


def config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    return Path(override) if override else _DEFAULT_CONFIG_PATH


# ══════════════════════════════════════════════════════════════════════════
#  ERRORS
# ══════════════════════════════════════════════════════════════════════════

class OrderSetConfigError(Exception):
    """The site's order-set file is unusable. Raised at import — i.e. at server
    startup — so a broken protocol file can never present as an empty one."""


class OrderSetError(Exception):
    """A request-level problem: unknown set, unknown/ineligible patient, no
    items selected. Routes map this to 400/404."""


class OrderSetBlocked(Exception):
    """A safety gate stopped at least one item, so the WHOLE set was refused.

    Carries the per-item verdicts so the caller can show the clinician exactly
    which item was stopped and why, rather than a bare refusal.
    """

    def __init__(self, verdicts: list[dict], blocked: list[dict]):
        self.verdicts = verdicts
        self.blocked = blocked
        super().__init__(
            "; ".join(f"{b['description']}: {b['block_reason']}" for b in blocked)
            or "an order in this set was stopped by a safety check"
        )

    @property
    def status_code(self) -> int:
        """428 when every block is an acknowledgeable severe alert (the Scribe
        sign-gate / CPOE contract the UI already speaks); 422 when anything
        harder is in the list — a dose-guard stop or a screen that could not
        run. A dose-guard BLOCK is deliberately NOT acknowledgeable HERE: it
        means the protocol FILE encodes a lethal dose or route, and the fix for
        that is to correct the file, not to click past it once per patient."""
        kinds = {b["block_kind"] for b in self.blocked}
        return 428 if kinds == {"drug_safety_acknowledgement_required"} else 422


# ══════════════════════════════════════════════════════════════════════════
#  CONFIG SCHEMA + VALIDATION
# ══════════════════════════════════════════════════════════════════════════
_VALID_ORDER_TYPES = {"lab", "imaging", "medication", "procedure", "nursing", "other"}
_VALID_PRIORITIES = {"stat", "urgent", "routine"}

_SET_KEYS = {"id", "name", "indication", "guideline", "items"}
_SET_REQUIRED = ("id", "name", "guideline", "items")
_GUIDELINE_KEYS = {"name", "citation", "reviewed_by", "reviewed_on", "url"}
_ITEM_KEYS = {"order_type", "description", "code", "code_system", "priority",
              "drug_name", "dose", "route", "frequency", "note"}
_MEDICATION_REQUIRED = ("drug_name", "dose", "route", "frequency")

# Mirrors orders_service.CODELESS_ORDER_TYPES rather than redefining it: if that
# set ever changes, this follows, instead of the file quietly accepting orders
# the order service will reject at apply time.
_CODELESS = orders_service.CODELESS_ORDER_TYPES


def _line_of(lines: list[str], needle: str, start: int = 1) -> Optional[int]:
    """1-based line number of the first line at or after `start` containing
    `needle`, or None."""
    for i in range(max(1, start), len(lines) + 1):
        if needle in lines[i - 1]:
            return i
    return None


class _Ctx:
    """Accumulates errors with a path and a line so ONE load reports EVERY
    problem. Reporting only the first turns fixing a hand-edited protocol file
    into a restart-per-typo loop, which is how an engineer ends up deleting the
    validation instead of the typos.

    THE LINE NUMBER. JSON's decoder gives a line and column for a SYNTAX error;
    a structural error ("this item has no dose") has none, and "sets[2].items[3]"
    is not something a person can find in a 400-line file at 2 a.m. So each
    failure carries needles — the most identifying text first — and an optional
    `after` anchor that pins the search to the right set, so a field name that
    appears in five protocols still resolves to the one that is wrong.
    """

    def __init__(self, path: Path, raw: str):
        self.path = path
        self.lines = raw.splitlines()
        self.errors: list[str] = []

    def fail(self, where: str, message: str, *needles: str, after: Optional[str] = None) -> None:
        start = 1
        if after:
            anchor = _line_of(self.lines, after)
            if anchor:
                start = anchor
        line = None
        for needle in needles:
            if not needle:
                continue
            line = _line_of(self.lines, needle, start)
            if line:
                break
        if line is None and after:
            line = _line_of(self.lines, after)
        prefix = f"{self.path.name}:{line}" if line else self.path.name
        self.errors.append(f"{prefix}: {where}: {message}")

    def raise_if_any(self) -> None:
        if not self.errors:
            return
        raise OrderSetConfigError(
            f"{len(self.errors)} problem(s) in the site order-set file {self.path}:\n  "
            + "\n  ".join(self.errors)
            + "\n\nOrb will not start with an unusable protocol file. A protocol "
              "that silently vanished would be worse than one that never existed."
        )


def _require_str(ctx: _Ctx, obj: dict, key: str, where: str, *needles: str,
                 after: Optional[str] = None) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        ctx.fail(f"{where}.{key}", f"must be a non-empty string (got {value!r})",
                 *needles, after=after)
        return ""
    return value.strip()


def _validate_item(ctx: _Ctx, item: Any, where: str, set_anchor: Optional[str]) -> Optional[dict]:
    if not isinstance(item, dict):
        ctx.fail(where, f"must be an object, got {type(item).__name__}", after=set_anchor)
        return None

    desc = item.get("description")
    anchor = desc if isinstance(desc, str) and desc.strip() else None
    unknown = sorted(set(item) - _ITEM_KEYS)
    if unknown:
        # A typo'd key is the quietest failure there is: "dosage" instead of
        # "dose" leaves the dose guard nothing to check and the order text
        # unchanged, so nothing looks wrong anywhere.
        ctx.fail(where, f"unknown field(s) {unknown}, expected only {sorted(_ITEM_KEYS)}",
                 f'"{unknown[0]}"', anchor, after=set_anchor)

    order_type = item.get("order_type")
    if order_type not in _VALID_ORDER_TYPES:
        ctx.fail(f"{where}.order_type",
                 f"{order_type!r} is not a valid order type "
                 f"(expected one of {sorted(_VALID_ORDER_TYPES)})",
                 f'"{order_type}"', anchor, '"order_type"', after=set_anchor)
        return None

    description = _require_str(ctx, item, "description", where, anchor,
                               after=set_anchor)

    priority = item.get("priority", "routine")
    if priority not in _VALID_PRIORITIES:
        ctx.fail(f"{where}.priority",
                 f"{priority!r} is not a valid priority (expected one of {sorted(_VALID_PRIORITIES)})",
                 f'"{priority}"', anchor, after=set_anchor)

    code = item.get("code")
    code_system = item.get("code_system")
    if code is not None and not (isinstance(code, str) and code.strip()):
        ctx.fail(f"{where}.code", "must be a non-empty string when present",
                 anchor, after=set_anchor)
        code = None
    if code:
        if not (isinstance(code_system, str) and code_system.strip()):
            ctx.fail(f"{where}.code_system", "is required whenever a code is given",
                     code, anchor, after=set_anchor)
    elif order_type not in _CODELESS:
        ctx.fail(f"{where}", f"order_type '{order_type}' needs a code + code_system; "
                             f"only {sorted(_CODELESS)} may be free-text",
                 anchor, after=set_anchor)

    resolved = {
        "order_type": order_type,
        "description": description,
        "code": code.strip() if isinstance(code, str) else None,
        "code_system": code_system.strip().upper() if isinstance(code_system, str) else None,
        "priority": priority if priority in _VALID_PRIORITIES else "routine",
        "note": item.get("note"),
        "drug_name": None, "dose": None, "route": None, "frequency": None,
    }

    if order_type == "medication":
        for key in _MEDICATION_REQUIRED:
            resolved[key] = _require_str(ctx, item, key, where, f'"{key}"', anchor,
                                         after=anchor or set_anchor)
        # ── The dose guard, applied to the PROTOCOL ITSELF ────────────────
        # Running it here as well as per-patient is not belt-and-braces for its
        # own sake. A dose-guard BLOCK inside a site's protocol file is not a
        # clinical judgement about one patient that a clinician might have a
        # reason to override — it is a lethal instruction sitting in a file that
        # will be applied to every future patient who gets this protocol. The
        # place to catch it is once, at startup, before anyone is on the ward.
        #
        # Pure evaluation only (no formulary/ingredient resolution): there is no
        # database at import time. The per-patient screen in `screen_items` runs
        # the fuller version with ingredients resolved.
        if resolved["drug_name"] and resolved["dose"]:
            try:
                verdict = drug_safety_service.evaluate_dose(
                    resolved["drug_name"], resolved["dose"], resolved["route"])
            except Exception as e:  # pragma: no cover — defensive
                ctx.fail(where, f"dose guard could not evaluate this item: {e}",
                         anchor, after=set_anchor)
            else:
                if verdict["decision"] == drug_safety_service.DOSE_BLOCK:
                    reasons = " ".join(
                        f["message"] for f in verdict["findings"]
                        if f.get("decision") == drug_safety_service.DOSE_BLOCK)
                    ctx.fail(where,
                             "THE DOSE GUARD STOPS THIS ITEM. It must not ship in a "
                             f"protocol. {reasons}", anchor, after=set_anchor)
    else:
        for key in _MEDICATION_REQUIRED:
            if item.get(key) is not None:
                ctx.fail(f"{where}.{key}",
                         f"only applies to a medication item, but order_type is "
                         f"'{order_type}'", f'"{key}"', anchor, after=set_anchor)

    return resolved


def _validate_set(ctx: _Ctx, raw_set: Any, index: int, seen_ids: set[str]) -> Optional[dict]:
    where = f"sets[{index}]"
    if not isinstance(raw_set, dict):
        ctx.fail(where, f"must be an object, got {type(raw_set).__name__}")
        return None

    set_id_raw = raw_set.get("id")
    anchor = (f'"id": "{set_id_raw}"'
              if isinstance(set_id_raw, str) and set_id_raw.strip() else None)

    unknown = sorted(set(raw_set) - _SET_KEYS - {"_comment"})
    if unknown:
        ctx.fail(where, f"unknown field(s) {unknown}, expected only {sorted(_SET_KEYS)}",
                 f'"{unknown[0]}"', after=anchor)

    missing = [k for k in _SET_REQUIRED if k not in raw_set]
    if missing:
        ctx.fail(where, f"missing required field(s) {missing}", after=anchor)
        return None

    set_id = _require_str(ctx, raw_set, "id", where, after=anchor)
    where = f"sets[{index}]({set_id or '?'})"
    if set_id:
        if set_id in seen_ids:
            ctx.fail(where, f"duplicate set id '{set_id}': ids must be unique, they are "
                            f"what the API and the audit trail name", anchor)
        seen_ids.add(set_id)

    name = _require_str(ctx, raw_set, "name", where, '"name"', after=anchor)

    # ── The citation is mandatory, and this is the point of the whole file ──
    # A protocol with no provenance cannot be defended to the consultant who
    # disagrees with it, cannot be reviewed when the guideline changes, and
    # cannot be shown to a regulator. "Cite a guideline per set" is a hard
    # requirement, so an absent citation is a startup failure, not a warning.
    guideline = raw_set.get("guideline")
    resolved_guideline: dict = {}
    if not isinstance(guideline, dict):
        ctx.fail(f"{where}.guideline",
                 "must be an object with at least 'name' and 'citation', every set "
                 "must cite the guideline it encodes", '"guideline"', after=anchor)
    else:
        g_unknown = sorted(set(guideline) - _GUIDELINE_KEYS)
        if g_unknown:
            ctx.fail(f"{where}.guideline",
                     f"unknown field(s) {g_unknown}, expected only {sorted(_GUIDELINE_KEYS)}",
                     f'"{g_unknown[0]}"', after=anchor)
        resolved_guideline = {
            "name": _require_str(ctx, guideline, "name", f"{where}.guideline",
                                 '"name"', after='"guideline"'),
            "citation": _require_str(ctx, guideline, "citation", f"{where}.guideline",
                                     '"citation"', after=anchor),
            "reviewed_by": guideline.get("reviewed_by"),
            "reviewed_on": guideline.get("reviewed_on"),
            "url": guideline.get("url"),
        }

    items_raw = raw_set.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        ctx.fail(f"{where}.items", "must be a non-empty list of orders",
                 '"items"', after=anchor)
        return None

    items: list[dict] = []
    for i, item in enumerate(items_raw):
        resolved = _validate_item(ctx, item, f"{where}.items[{i}]", anchor)
        if resolved is not None:
            resolved["index"] = i
            items.append(resolved)

    return {
        "id": set_id,
        "name": name,
        "indication": raw_set.get("indication"),
        "guideline": resolved_guideline,
        "items": items,
        "item_count": len(items),
    }


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def load_config(path: Optional[Path] = None) -> dict:
    """Read and fully validate the site's order-set file. PURE — no database.

    Raises :class:`OrderSetConfigError` naming the file, the line and the field
    for every problem found, all in one message.
    """
    path = Path(path) if path else config_path()

    if not path.exists():
        raise OrderSetConfigError(
            f"Site order-set file not found: {path}. Orb ships a starter file at "
            f"server/site_config/order_sets.json; set {CONFIG_PATH_ENV} to point "
            f"somewhere else. An absent protocol file is a deployment mistake, not "
            f"a site that has no protocols."
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise OrderSetConfigError(f"Cannot read site order-set file {path}: {e}") from None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        # json's own line/column, which is the whole reason this file is JSON
        # and not something that needs a third-party parser to say where it hurt.
        raise OrderSetConfigError(
            f"{path.name}:{e.lineno}:{e.colno}: the site order-set file is not valid "
            f"JSON: {e.msg}. (Full path: {path})"
        ) from None

    ctx = _Ctx(path, raw)

    if not isinstance(parsed, dict):
        ctx.fail("<root>", f"must be a JSON object, got {type(parsed).__name__}")
        ctx.raise_if_any()

    version = parsed.get("config_version")
    if version != 1:
        ctx.fail("config_version",
                 f"must be 1 (this build understands version 1); got {version!r}",
                 '"config_version"')

    unknown_root = sorted(set(parsed) - {"config_version", "sets", "_comment"})
    if unknown_root:
        ctx.fail("<root>", f"unknown field(s) {unknown_root}", f'"{unknown_root[0]}"')

    sets_raw = parsed.get("sets")
    if not isinstance(sets_raw, list) or not sets_raw:
        ctx.fail("sets", "must be a non-empty list of order sets", '"sets"')
        ctx.raise_if_any()

    seen_ids: set[str] = set()
    sets: list[dict] = []
    for i, raw_set in enumerate(sets_raw):
        resolved = _validate_set(ctx, raw_set, i, seen_ids)
        if resolved is not None:
            sets.append(resolved)

    ctx.raise_if_any()

    for s in sets:
        s["fingerprint"] = _fingerprint(s)

    return {
        "config_version": version,
        "path": str(path),
        "fingerprint": _fingerprint([s["fingerprint"] for s in sets]),
        "sets": sets,
        "sets_by_id": {s["id"]: s for s in sets},
    }


# ══════════════════════════════════════════════════════════════════════════
#  THE LOADED CONFIG
# ══════════════════════════════════════════════════════════════════════════
_CONFIG: Optional[dict] = None


def reload_config(path: Optional[Path] = None) -> dict:
    """Re-read the file and replace the process-wide config. Used at import, by
    tests, and by an operator restarting after an edit."""
    global _CONFIG
    _CONFIG = load_config(path)
    logger.info("Order sets: loaded %d set(s) from %s (fingerprint %s)",
                len(_CONFIG["sets"]), _CONFIG["path"], _CONFIG["fingerprint"])
    return _CONFIG


def get_config() -> dict:
    if _CONFIG is None:                       # pragma: no cover — import loads it
        return reload_config()
    return _CONFIG


def list_sets() -> list[dict]:
    """Summary of every configured set — no PHI, safe for any ORDER_READ user."""
    return [
        {
            "id": s["id"],
            "name": s["name"],
            "indication": s["indication"],
            "guideline": s["guideline"],
            "item_count": s["item_count"],
            "fingerprint": s["fingerprint"],
        }
        for s in get_config()["sets"]
    ]


def get_set(set_id: str) -> dict:
    """Full definition of one set. Raises OrderSetError for an unknown id."""
    s = get_config()["sets_by_id"].get(set_id)
    if s is None:
        raise OrderSetError(
            f"Unknown order set '{set_id}'. Configured sets: "
            f"{sorted(get_config()['sets_by_id'])}"
        )
    return s


def unknown_codes(set_id: str) -> list[dict]:
    """Which of this set's codes are NOT in the local `terminology` table.

    Code validity is a DATABASE question, so it cannot be answered at import
    time — but the clinician should learn about a site's un-loaded code before
    they try to apply the protocol, not from a 400 halfway through.
    """
    s = get_set(set_id)
    wanted = sorted({(i["code_system"], i["code"]) for i in s["items"] if i["code"]})
    if not wanted:
        return []
    # A handful of indexed point lookups on ONE connection, never a full read of
    # `terminology` — a site that has bulk-loaded the real LOINC and ICD-10
    # releases has millions of rows in there, and this runs on every preview.
    missing = []
    with db_session() as db:
        for code_system, code in wanted:
            hit = db.execute(
                "SELECT 1 FROM terminology WHERE code_system = ? AND code = ? "
                "AND is_active = 1 LIMIT 1", (code_system, code),
            ).fetchone()
            if hit is None:
                missing.append({"code_system": code_system, "code": code})
    return missing


# ══════════════════════════════════════════════════════════════════════════
#  APPLYING A SET TO A PATIENT
# ══════════════════════════════════════════════════════════════════════════

def resolve_items(set_id: str, selections: Optional[list[dict]] = None) -> list[dict]:
    """The items that will actually be ordered, after the clinician's edits.

    `selections` is a list of ``{"index": n, "include": bool, ...}``. Any field
    in ``description / priority / dose / route / frequency`` present and
    non-null replaces the file's value for that item. Everything absent keeps
    the protocol's value; an item with no selection is included unchanged.

    This is what "editable before commit" means at the API: the clinician tunes
    the set to the patient in front of them, and the edited values are what the
    safety gates then screen — never the file's values.
    """
    s = get_set(set_id)
    by_index: dict[int, dict] = {}
    for sel in (selections or []):
        idx = sel.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(s["items"])):
            raise OrderSetError(
                f"selection refers to item index {idx!r}, but set '{set_id}' has "
                f"{len(s['items'])} items (0-{len(s['items']) - 1})"
            )
        if idx in by_index:
            raise OrderSetError(f"item index {idx} appears twice in the selection")
        by_index[idx] = sel

    out: list[dict] = []
    for item in s["items"]:
        sel = by_index.get(item["index"], {})
        if sel.get("include") is False:
            continue
        resolved = dict(item)
        edited: list[str] = []
        for field in ("description", "priority", "dose", "route", "frequency"):
            value = sel.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise OrderSetError(f"item {item['index']}: '{field}' must be a non-empty string")
            if field == "priority" and value not in _VALID_PRIORITIES:
                raise OrderSetError(
                    f"item {item['index']}: priority '{value}' is not one of "
                    f"{sorted(_VALID_PRIORITIES)}")
            if field in ("dose", "route", "frequency") and item["order_type"] != "medication":
                raise OrderSetError(
                    f"item {item['index']}: '{field}' only applies to a medication order")
            if resolved.get(field) != value.strip():
                edited.append(field)
            resolved[field] = value.strip()
        resolved["edited_fields"] = edited
        resolved["safety_ack"] = sel.get("safety_ack")
        out.append(resolved)

    if not out:
        raise OrderSetError(
            f"every item in '{set_id}' was excluded, nothing to order. Applying an "
            f"empty set would write an audit row claiming a protocol was applied.")
    return out


def require_orderable_patient(patient_id: str) -> dict:
    """The patient must exist and must still be an inpatient.

    Same guard, same reasoning, as ``emar_service.record_administration``: a
    patient discharged through Vigil keeps ``medications.status='active'`` and
    disappears from every board, so an order written against them afterwards is
    written against someone no ward believes it still has. A protocol is exactly
    the kind of bulk action where that would go unnoticed.
    """
    if not patient_id or not str(patient_id).strip():
        raise OrderSetError("patient_id is required")
    with db_session() as db:
        row = db.execute(
            "SELECT id, name, status FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
    if row is None:
        raise OrderSetError(f"Patient {patient_id} not found")
    status = row["status"]
    if status not in (None, "", "admitted"):
        raise OrderSetError(
            f"Patient {patient_id} is not admitted (status={status}), an order set "
            f"cannot be applied to a discharged patient. Re-admit them first, or "
            f"order individually if this is deliberate follow-up."
        )
    return dict(row)


def _dose_screen(patient_id: str, item: dict) -> dict:
    """The dose guard for ONE medication item, at order time.

    ``evaluate_dose`` is pure; the ingredient resolution around it is the same
    hospital formulary the allergy interlock and the bedside dose guard use, so
    a branded protocol item ("Monocef 1 g") is judged as its ingredient rather
    than as an unknown drug.

    NOT FAIL-OPEN, NOT FAIL-CLOSED-SILENT: if the evaluation itself errors, the
    item is reported ``unavailable`` with a WARN, exactly as the bedside guard
    does — the clinician is told this item was not checked. It does not block,
    because an internal fault in this code is not evidence about the patient.
    """
    check = {
        "drug_name": item["drug_name"],
        "dose": item["dose"],
        "route": item["route"],
        "decision": drug_safety_service.DOSE_ALLOW,
        "rule": None,
        "findings": [],
        "unavailable": False,
        "unavailable_reason": None,
    }
    ingredients: list[str] = []
    try:
        with db_session() as db:
            from services import allergy_interlock
            ingredients = allergy_interlock.resolve_ingredients(db, item["drug_name"])
    except Exception as e:                    # pragma: no cover — defensive
        logger.warning("[OrderSets] formulary resolution failed for %s: %s",
                       item["drug_name"], e)
    try:
        verdict = drug_safety_service.evaluate_dose(
            item["drug_name"], item["dose"], item["route"], ingredients)
    except Exception as e:
        logger.error("[OrderSets] dose evaluation FAILED for %s (%s): %s",
                     item["drug_name"], item["dose"], e)
        check["unavailable"] = True
        check["unavailable_reason"] = (
            "the dose guard could not run for this item. It has NOT been checked")
        check["decision"] = drug_safety_service.DOSE_WARN
        return check
    check.update({
        "decision": verdict["decision"],
        "rule": verdict["rule"],
        "parsed_dose": verdict["parsed_dose"],
        "route_tags": verdict["route_tags"],
        "findings": verdict["findings"],
    })
    return check


def screen_items(
    patient_id: str,
    items: list[dict],
    *,
    actor_id: Optional[str],
    entry_point: str,
    honour_acks: bool,
    actor_role: Optional[str] = None,
) -> list[dict]:
    """Run EVERY gate on EVERY item and return one verdict per item.

    Writes nothing to `orders`. The allergy/interaction screen goes through
    ``enforce_medication_safety`` — the one enforcement point — so the screen,
    the block and any override land on the audit hash chain from here exactly as
    they do from CPOE, with ``entry_point`` naming which of preview/apply it was.

    ``honour_acks=False`` (preview) means a supplied acknowledgement is IGNORED:
    a preview must never quietly consume an override. The clinician sees the
    severe alert; the override happens on apply, once, with the order.
    """
    verdicts: list[dict] = []
    for item in items:
        verdict = {
            "index": item["index"],
            "order_type": item["order_type"],
            "description": item["description"],
            "code": item["code"],
            "code_system": item["code_system"],
            "priority": item["priority"],
            "drug_name": item["drug_name"],
            "dose": item["dose"],
            "route": item["route"],
            "frequency": item["frequency"],
            "note": item["note"],
            "edited_fields": item.get("edited_fields", []),
            "medication_safety": None,
            "dose_check": None,
            "blocked": False,
            "block_kind": None,
            "block_reason": None,
        }

        if item["order_type"] != "medication":
            verdicts.append(verdict)
            continue

        # ── 1. allergy + interaction, through the single enforcement point ──
        ack = item.get("safety_ack") if honour_acks else None
        try:
            verdict["medication_safety"] = enforce_medication_safety(
                patient_id, item["drug_name"],
                actor_id=actor_id, entry_point=entry_point, safety_ack=ack,
                # Who may clear a severe allergy: a prescriber only.
                actor_role=actor_role,
            )
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {}
            safety = detail.get("safety") or {}
            verdict["medication_safety"] = detail.get("safety")
            verdict["blocked"] = True
            verdict["block_kind"] = (
                "drug_safety_acknowledgement_required" if e.status_code == 428
                else "drug_safety_screen_unavailable")
            # Say WHICH alert stopped this item, not just that one did.
            #
            # This used to take the HTTP envelope's message — "Severe drug-safety
            # alerts must be acknowledged with a reason before this order
            # proceeds" — which describes the PROCEDURE and never names the
            # finding. On the Sepsis Six set, the single most-demoed screen in
            # the product, a ceftriaxone item stopped by a charted anaphylaxis
            # read as generic process boilerplate: the interlock did exactly its
            # job and the UI declined to say so. The alerts already carry
            # "Patient is allergic to X (documented reaction: Y)." — the
            # dose-guard branch below has always surfaced its findings this way,
            # and the two paths simply disagreed.
            specifics = [
                a["message"]
                for a in (safety.get("allergy_alerts") or []) + (safety.get("interaction_alerts") or [])
                if a.get("message")
            ]
            verdict["block_reason"] = (
                " ".join(specifics) if specifics
                else (detail.get("message") or str(e.detail))
            )
            # An acknowledgement the gate refused says why, first.
            if safety.get("ack_refused"):
                verdict["block_reason"] = f"{safety['ack_refused']} {verdict['block_reason']}"
            verdicts.append(verdict)
            continue
        except MedicationSafetyBlocked as e:   # pragma: no cover — interactive here
            verdict["medication_safety"] = e.safety
            verdict["blocked"] = True
            verdict["block_kind"] = "drug_safety_screen_unavailable"
            verdict["block_reason"] = e.reason
            verdicts.append(verdict)
            continue

        # ── 2. the dose guard ──────────────────────────────────────────────
        dose_check = _dose_screen(patient_id, item)
        verdict["dose_check"] = dose_check
        if dose_check["decision"] == drug_safety_service.DOSE_BLOCK:
            verdict["blocked"] = True
            verdict["block_kind"] = "dose_guard_block"
            verdict["block_reason"] = " ".join(
                f["message"] for f in dose_check["findings"]
                if f.get("decision") == drug_safety_service.DOSE_BLOCK
            ) or "the dose guard stopped this order"
        verdicts.append(verdict)

    return verdicts


#: How far back an earlier application of the SAME set to the SAME patient
#: counts as "recent" for the repeat warning. Env override for sites whose
#: protocols are legitimately re-run within the hour.
RECENT_APPLICATION_MINUTES_ENV = "ORB_ORDER_SET_RECENT_MINUTES"
_DEFAULT_RECENT_MINUTES = 120


def _recent_minutes() -> int:
    try:
        return max(1, int(os.environ.get(RECENT_APPLICATION_MINUTES_ENV,
                                         _DEFAULT_RECENT_MINUTES)))
    except (TypeError, ValueError):
        return _DEFAULT_RECENT_MINUTES


def recent_application(patient_id: str, set_id: str, *,
                       now: Optional[datetime] = None) -> Optional[dict]:
    """The latest application of this set to this patient inside the recent
    window, or None.

    apply_set has no duplicate guard by design (a protocol can legitimately be
    re-run), so the screen has to say so instead: a Sepsis Six applied twice in
    two minutes is two sets of STAT drafts, and the second clinician needs to
    know the first one exists before pressing Apply. A WARNING, never a block.
    """
    ts = now or datetime.now(timezone.utc)
    cutoff = ts - timedelta(minutes=_recent_minutes())
    with db_session() as db:
        row = db.execute(
            """SELECT a.id, a.applied_at, a.applied_by, a.order_count,
                      u.name AS applied_by_name
                 FROM order_set_applications a
                 LEFT JOIN users u ON u.id = a.applied_by
                WHERE a.patient_id = ? AND a.set_id = ?
                  AND julianday(a.applied_at) >= julianday(?)
                ORDER BY a.id DESC LIMIT 1""",
            (patient_id, set_id, cutoff.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
    if row is None:
        return None
    applied = _parse_utc(row["applied_at"])
    return {
        "application_id": row["id"],
        "applied_at": row["applied_at"],
        "applied_by": row["applied_by"],
        "applied_by_name": row["applied_by_name"],
        "order_count": row["order_count"],
        "minutes_ago": (max(0, int((ts - applied).total_seconds() // 60))
                        if applied else None),
    }


def _parse_utc(raw: Any) -> Optional[datetime]:
    """applied_at is SQLite CURRENT_TIMESTAMP: bare 'YYYY-MM-DD HH:MM:SS', UTC."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def patient_banner(patient_id: str) -> dict:
    """Who the gates ran for, in the form the screen must name them.

    The order-set screen hid its picker when handed a patient and then applied
    STAT orders to someone it never named. The banner comes back WITH the
    verdicts so the name on the Apply button is the patient the gates actually
    ran against, not whatever the client last had selected. The allergies are
    the same active list the interlock screens against; ``None`` means the list
    could not be read, which the screen must say rather than show as empty.
    """
    with db_session() as db:
        row = db.execute(
            "SELECT id, name, mrn, bed, ward FROM patients WHERE id = ?", (patient_id,)
        ).fetchone()
        try:
            from services import allergy_interlock
            allergies = [
                {"substance": a["substance"], "reaction": a.get("reaction"),
                 "severity": a.get("severity")}
                for a in allergy_interlock.read_active_allergies(db, patient_id)
            ]
        except Exception as e:
            logger.warning("[OrderSets] allergy list unreadable for %s: %s", patient_id, e)
            allergies = None
    banner = dict(row) if row else {"id": patient_id, "name": None, "mrn": None,
                                    "bed": None, "ward": None}
    banner["allergies"] = allergies
    return banner


def preview_set(
    patient_id: str,
    set_id: str,
    *,
    actor_id: Optional[str],
    selections: Optional[list[dict]] = None,
) -> dict:
    """What applying this set to this patient would do, and what would stop it.

    Writes no orders. Runs the same gates apply runs, so the preview and the
    apply cannot disagree about whether an item is safe.
    """
    s = get_set(set_id)
    patient = require_orderable_patient(patient_id)
    items = resolve_items(set_id, selections)
    verdicts = screen_items(patient_id, items, actor_id=actor_id,
                            entry_point="order_set.preview", honour_acks=False)
    blocked = [v for v in verdicts if v["blocked"]]
    return {
        "patient_id": patient["id"],
        "set": {"id": s["id"], "name": s["name"], "guideline": s["guideline"],
                "fingerprint": s["fingerprint"]},
        "items": verdicts,
        "blocked": blocked,
        "would_apply": not blocked,
        "unknown_codes": unknown_codes(set_id),
        "recent_application": recent_application(patient["id"], s["id"]),
        "patient": patient_banner(patient["id"]),
    }


def _linked_draft_writer(item: dict, application_id: int, actor_id: str,
                         actor_role: Optional[str]):
    """The in-transaction hook that writes a medication item's draft
    medication order beside its orders row (medorders-7, lifecycle on): the
    drug, dose, route and frequency the gates just screened, linked to the
    order and to this application. A draft reaches no ward until a
    prescriber signs it, and the signature screens it again."""
    def _write(conn, order: dict) -> None:
        cpoe.create_linked_draft_in_txn(
            conn, order, source="order_set", drug_name=item["drug_name"],
            dose=item["dose"], route=item["route"], frequency=item["frequency"],
            notes=item.get("note"), actor_id=actor_id, actor_role=actor_role,
            entry_point=cpoe.ENTRY_ORDER_SET_APPLY, lane=cpoe.LANE_ORDER_SET,
            order_set_application_id=application_id)
    return _write


def apply_set(
    patient_id: str,
    set_id: str,
    *,
    actor_id: str,
    actor_role: Optional[str] = None,
    selections: Optional[list[dict]] = None,
) -> dict:
    """Create the real, individual, DRAFT orders for a set. ALL OR NOTHING.

    Order of operations, and why:

      1. resolve the set + the clinician's edits, and check the patient is a
         patient we may order for at all;
      2. screen EVERY item — so the clinician gets one complete list of what is
         wrong, not a fix-one-restart loop;
      3. if ANYTHING is blocked, raise. No orders are written. A set that is
         partly applied is worse than one that is refused: the chart then shows
         a protocol nobody completed and nobody knows to finish;
      4. write the application ledger row FIRST, then the orders. If the process
         dies in the middle, the ledger records that the set was applied and the
         orders that exist are drafts — visible, cancellable, un-actioned. The
         opposite order would leave orders no ledger row explains;
      5. one audit event naming the set, listing every order it produced.

    HONEST LIMIT: between the screen in (2) and the write in (4) there is a
    sub-millisecond window in which another connection could chart a new allergy.
    The bedside eMAR closes that window because a dose is the last line of
    defence; an order is not administered for minutes to hours, and its own
    administration re-screens. Recorded here rather than papered over.

    WITH ORB_MED_ORDER_LIFECYCLE ON (medorders-7), each medication item's
    draft is also a draft medication order, written in the same transaction
    as its orders row (_linked_draft_writer). It reaches the ward only when a
    prescriber signs it, and the signature screens it again with the
    signer's role and re-checks the allergy and drug list under its write
    lock, which closes the window above for those drafts. Lifecycle off:
    exactly as before.
    """
    s = get_set(set_id)
    patient = require_orderable_patient(patient_id)
    items = resolve_items(set_id, selections)
    # Read once, so one application never half-links its drafts.
    lifecycle = cpoe.lifecycle_on()

    # Terminology is checked for the WHOLE set before a single order is written.
    # orders_service rejects an unknown code per order, which mid-loop would
    # leave a half-applied protocol — the one outcome step (3) exists to prevent.
    missing_codes = unknown_codes(set_id)
    if missing_codes:
        raise OrderSetError(
            f"Order set '{set_id}' names {len(missing_codes)} code(s) this deployment "
            f"cannot resolve: "
            + ", ".join(f"{c['code_system']} {c['code']}" for c in missing_codes)
            + ". Load them into the terminology table, or correct the protocol file. "
              "No orders were created."
        )

    verdicts = screen_items(patient_id, items, actor_id=actor_id,
                            entry_point="order_set.apply", honour_acks=True,
                            actor_role=actor_role)
    blocked = [v for v in verdicts if v["blocked"]]
    if blocked:
        _audit_set(outcome="denied", action_type="order_set_blocked",
                   patient_id=patient_id, set_def=s, actor_id=actor_id,
                   actor_role=actor_role, verdicts=verdicts, order_ids=[],
                   application_id=None)
        raise OrderSetBlocked(verdicts, blocked)

    by_index = {v["index"]: v for v in verdicts}

    # ── (4a) the ledger row, before a single order exists ────────────────────
    with db_session() as db:
        cur = db.execute(
            """INSERT INTO order_set_applications
               (patient_id, set_id, set_name, guideline, config_fingerprint,
                applied_by, order_count, order_ids_json, safety_json)
               VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)""",
            (patient_id, s["id"], s["name"],
             (s["guideline"] or {}).get("citation") or (s["guideline"] or {}).get("name"),
             s["fingerprint"], actor_id, json.dumps(verdicts, default=str)),
        )
        application_id = cur.lastrowid

    # ── (4b) the orders themselves, individually, through orders_service ─────
    created: list[dict] = []
    try:
        for item in items:
            detail = {
                "order_set": {
                    "application_id": application_id,
                    "set_id": s["id"],
                    "set_name": s["name"],
                    "item_index": item["index"],
                    "guideline": s["guideline"],
                    "fingerprint": s["fingerprint"],
                    "edited_fields": item.get("edited_fields", []),
                },
                "note": item["note"],
            }
            if item["order_type"] == "medication":
                detail["medication"] = {
                    "drug_name": item["drug_name"],
                    "dose": item["dose"],
                    "route": item["route"],
                    "frequency": item["frequency"],
                }
                detail["safety"] = {
                    "screen": by_index[item["index"]]["medication_safety"],
                    "dose_check": by_index[item["index"]]["dose_check"],
                }
            order = orders_service.create_order(
                patient_id=patient_id,
                order_type=item["order_type"],
                description=item["description"],
                code=item["code"],
                code_system=item["code_system"],
                priority=item["priority"],
                ordered_by=actor_id,
                source="order_set",
                detail=detail,
                status="draft",
                in_transaction=(_linked_draft_writer(item, application_id, actor_id, actor_role)
                                if lifecycle and item["order_type"] == "medication" else None),
            )
            created.append(order)
            by_index[item["index"]]["order_id"] = order["id"]
    except Exception:
        # The ledger row survives on purpose. It, plus the drafts that did get
        # written, is the only readable account of a half-finished apply — and
        # a draft order harms nobody, which is precisely why drafts are what a
        # set produces.
        with db_session() as db:
            db.execute(
                "UPDATE order_set_applications SET order_count = ?, order_ids_json = ? WHERE id = ?",
                (len(created), json.dumps([o["id"] for o in created]), application_id),
            )
        logger.error("[OrderSets] apply of '%s' for %s failed after %d order(s); "
                     "application %s left for review", set_id, patient_id,
                     len(created), application_id)
        raise

    order_ids = [o["id"] for o in created]
    with db_session() as db:
        db.execute(
            "UPDATE order_set_applications SET order_count = ?, order_ids_json = ?, "
            "safety_json = ? WHERE id = ?",
            (len(order_ids), json.dumps(order_ids), json.dumps(verdicts, default=str),
             application_id),
        )
        for oid in order_ids:
            db.execute("UPDATE orders SET order_set_application_id = ? WHERE id = ?",
                       (application_id, oid))
        if lifecycle:
            # Which drafts the order store now manages, so the route records
            # no create-time shadow action for them (the signature does).
            cpoe.annotate_orders(db, created)

    # ── (5) ONE audited event naming the set, plus its individual orders ─────
    _audit_set(outcome="success", action_type="order_set_applied",
               patient_id=patient_id, set_def=s, actor_id=actor_id,
               actor_role=actor_role, verdicts=verdicts, order_ids=order_ids,
               application_id=application_id)

    return {
        "application_id": application_id,
        "patient_id": patient_id,
        "set": {"id": s["id"], "name": s["name"], "guideline": s["guideline"],
                "fingerprint": s["fingerprint"]},
        "orders": created,
        "items": verdicts,
    }


def _audit_set(*, outcome: str, action_type: str, patient_id: str, set_def: dict,
               actor_id: Optional[str], actor_role: Optional[str],
               verdicts: list[dict], order_ids: list[str],
               application_id: Optional[int]) -> None:
    """The one event that says a PROTOCOL was applied, naming its orders.

    Deliberately in addition to — never instead of — the per-order `order_events`
    row and the per-drug clinical_safety rows the medication gate already wrote.
    A reviewer asking "why are there eight orders at 02:14?" needs the answer
    "the sepsis bundle, by Dr X, citing SSC 2021", and a reviewer asking "was
    this drug screened?" needs the drug-level row. Neither answers the other.
    """
    alerts = sum(
        len((v.get("medication_safety") or {}).get("allergy_alerts", []))
        + len((v.get("medication_safety") or {}).get("interaction_alerts", []))
        for v in verdicts
    )
    try:
        log_audit_event(
            correlation_id=str(uuid.uuid4()),
            # An established category, not a new one: `clinical_record` is what
            # every other clinical write uses, so an order set shows up in the
            # same filter a reviewer already runs. Deliberately NOT one of the
            # fail-closed categories — a failed audit row must not un-write
            # orders that are already on the chart (same call the medication
            # safety gate makes, and the reason for the try/except below).
            event_category="clinical_record",
            action_type=action_type,
            outcome=outcome,
            summary_message=(
                f"Order set '{set_def['name']}' ({set_def['id']}) {outcome} for "
                f"{patient_id}: {len(order_ids)} order(s), {alerts} safety alert(s). "
                f"Guideline: {(set_def.get('guideline') or {}).get('name')}"
            ),
            actor_user_id=actor_id,
            actor_role=actor_role,
            target_type="order_set",
            target_id=set_def["id"],
            patient_id=patient_id,
            metadata={
                "set_id": set_def["id"],
                "set_name": set_def["name"],
                "guideline": set_def.get("guideline"),
                "config_fingerprint": set_def.get("fingerprint"),
                "application_id": application_id,
                "order_ids": order_ids,
                "items": [
                    {
                        "index": v["index"],
                        "order_type": v["order_type"],
                        "description": v["description"],
                        "order_id": v.get("order_id"),
                        "drug_name": v["drug_name"],
                        "dose": v["dose"],
                        "route": v["route"],
                        "edited_fields": v.get("edited_fields", []),
                        "blocked": v["blocked"],
                        "block_kind": v["block_kind"],
                        "block_reason": v["block_reason"],
                        "max_severity": (v.get("medication_safety") or {}).get("max_severity"),
                        "dose_decision": (v.get("dose_check") or {}).get("decision"),
                    }
                    for v in verdicts
                ],
            },
        )
    except Exception as e:  # pragma: no cover — audit must not un-write orders
        logger.error("[OrderSets] audit write failed for set %s / %s: %s",
                     set_def.get("id"), patient_id, e)


def list_applications(patient_id: str, limit: int = 50) -> list[dict]:
    """Protocols applied to this patient, newest first. PHI — callers scope."""
    with db_session() as db:
        rows = db.execute(
            "SELECT * FROM order_set_applications WHERE patient_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (patient_id, max(1, min(int(limit), 200))),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["order_ids"] = json.loads(d.get("order_ids_json") or "[]")
        except (TypeError, ValueError):       # pragma: no cover — defensive
            d["order_ids"] = []
        d.pop("order_ids_json", None)
        d.pop("safety_json", None)
        out.append(d)
    return out


# ══════════════════════════════════════════════════════════════════════════
#  LOAD AT IMPORT — this is the "fail loudly at startup" mechanism
# ══════════════════════════════════════════════════════════════════════════
# routes/orders_routes.py imports this module and main.py imports that router at
# module scope, so a malformed site file raises before uvicorn ever binds a
# port. There is deliberately NO try/except here and deliberately no env switch
# to disable it: "carry on without protocols" is the exact outcome this design
# refuses.
reload_config()
