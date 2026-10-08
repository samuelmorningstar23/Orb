"""Orb, pharmacy-internal transfers (grn-11).

Design: docs/his_design/grn.json, increment grn-11, sections D, F and G.
Reached only through routes/pharmacy_store/transfers.py, behind
ORB_PHARMACY_STORE (default off). Nothing here runs while the module is off,
and nothing here changes what a site without the module sees.

WHAT A TRANSFER IS. Drug stock moving between two of the pharmacy's own stores:
the dispensing pharmacy, a sub-store, or the NDPS vault. Ward stores and crash
carts are filled by ward indents (grn-21, ORB_PHARMACY_WARD_STOCK, ruling
GRN-RS5) and returns quarantine empties only by write-off or a vendor return
(GRN-RS3), so neither is a transfer end here.

THE LIFE OF ONE
  draft               a FEFO issue plan: for each drug, the usable lots of the
                      sending store, earliest expiry first, one line per lot.
                      Nothing moves; the plan is what the dispatcher picks.
  dispatched          one transfer_out movement per line. The units have left
                      the sending lot and sit in no lot at all: IN TRANSIT,
                      counted by neither store and dispensable nowhere
                      (services/pharmacy_stock_reports.in_transit_units reads
                      lines with received_units NULL on a dispatched or
                      partially received transfer, against the store they go to).
  partially_received  some lines received, the rest still in transit.
  received            every line received. Each received line put its units
                      into a lot of the same drug, batch and expiry in the
                      receiving store by one transfer_in movement.
  cancelled           a draft given up, with a reason. Only a draft: once units
                      are in transit they are received (in full, short, or as
                      nothing arrived), never cancelled into thin air.

ONE WRITER. Every stock change goes through pharmacy_stock_service.post_movement
(the transfer_out) and open_or_merge_lot (the transfer_in), on the caller's
immediate transaction, so a lot's quantity stays the sum of its ledger and
reconcile() stays clean. This module never writes helix_inventory_lots or
helix_stock_movements itself (tests/test_pharmacy_stock_single_writer.py). The
movement idempotency keys are 'pharmacy_transfer:<id>:line:<line id>:out' and
':in', so a retried dispatch or receipt posts once.

THE BALANCE OF A LINE. transfer_out + transfer_in = 0 for a line received in
full. A SHORT receipt (fewer units arrived than left) books only what arrived;
the shortfall is recorded on the line with a reason the receiver must give,
and in the fail-closed 'security' audit row of the receipt. It is never put back
into the sending lot (it is not there) and never written off by the receiver
(a write-off is the approve tier's decision, grn-10). So for every received
line: out + in + shortfall = 0, and a shortfall always carries its reason.
transfer_balance() checks exactly that.

SEPARATION OF DUTIES. The person who receives is never the person who
dispatched (403 same_person; the table's CHECK says the same).

CONTROLLED DRUGS (GRN-R14, product ruling, the architect's recommendation
built): a narcotic or psychotropic is received only into the NDPS vault and
may be dispensed from it; a site that keeps a daily dispensing cabinet fills it
from the vault by transfer. So:
  * a narcotic or psychotropic moves only between the NDPS vault and the
    dispensing pharmacy, either way (never through a sub-store);
  * the vault takes only narcotic or psychotropic drugs;
  * a transfer that carries one, or that leaves or enters the vault, is
    CONTROLLED: every action on it needs pharmacy_store.ndps as well as the
    write tier, and its dispatch and its receipt each need a WITNESS, a second
    active user holding pharmacy_store.ndps who is not the person acting, who
    confirms with their own signing PIN (services/pin_security: PIN lockout
    respected, a wrong PIN counted and audited).
The witness rule applies whether or not the NDPS register is on
(NDPS_WITNESS_ALWAYS, strictest; the increment text asks for it once the
register, grn-13, is enabled). While a transfer posts its movements,
current_transfer_leg() names the transfer, the leg and the witness, so the
register hook grn-13 registers in post_movement can write both legs with it.

WHAT MAY MOVE (strict defaults, each listed for Samuel to confirm):
  * a drug with no schedule or no NDPS class recorded is not moved at all
    (unclassified is never read as 'none', GRN-R18): classify it first;
  * a drug that is inactive or not confirmed (identity status other than
    'active', GRN-RS1) is never moved INTO the dispensing pharmacy, where it
    would become dispensable; moving it off the shelf is allowed;
  * only lots that are 'available', hold units and are in date on the
    hospital's date (site_today) are planned; quarantined, recalled, expired
    or undated stock never moves by transfer;
  * a recall or an NSQ finding is about the BATCH, not one lot of it: a lot
    whose batch (the same drug, the batch compared without case or spaces as
    pharmacy_stock_service does, any expiry, any store) has a lot recalled or
    NSQ anywhere is held too. It is not planned, and a dispatch whose plan
    names one is stale (held_batches);
  * at receipt the stock is read again, because a lot can be recalled,
    found NSQ or quarantined while its units are on their way and those units
    sit in no lot a recall could find. A line whose lot left 'available', or
    whose batch is now recalled or NSQ anywhere, never lands as available: it
    lands HELD in the receiving store (_landing_status), recalled or NSQ as
    that, anything else quarantined, so the units are counted, visible to a
    batch recall, written off or returned from there, and dispensable nowhere.
    When the receiving store already holds that batch as available stock,
    the receipt is refused (409 lot_status_conflict) and the units stay on
    their way until that lot is held as well.

AUDIT. Every action writes one 'security' row (fail-closed, db=conn) in the
same transaction as its movements: pharmacy_transfer_created, _replanned,
_dispatched, _received, _cancelled. A refused witness PIN writes
pharmacy_transfer_witness_refused. A transfer is pharmacy business, not a
patient record: no patient id is read, written or shown.

NUMBERS come from the pharmacy's own series, PHTRF (GRN-R11), drawn after the
dispatch commits, as procurement stamps its documents. Migration 54 seeds the
series unconfigured; the transfer carries number_series_configured so the
screen says the number is a placeholder until the site sets its series up.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import uuid
from typing import Any, Iterator, Mapping, Optional, Sequence

from database import db_session
from services import pharmacy_stock_service as stock

logger = logging.getLogger("orb.pharmacy_transfers")

# ═════════════════════════════════════════════════════════════════════════════
#  Words and limits
# ═════════════════════════════════════════════════════════════════════════════

#: Reference type on every movement a transfer posts (the ledger labels it
#: "Transfer between stores").
REFERENCE_TYPE = "pharmacy_transfer"

#: Number series and the procurement stamping kind for it.
NUMBER_SERIES = "PHTRF"
NUMBER_KIND = "pharmacy_transfer"

#: Pharmacy store kinds a transfer may leave from or arrive at.
TRANSFER_STORE_KINDS: tuple[str, ...] = (
    stock.DISPENSING_STORE_KIND, "sub_store", stock.NDPS_VAULT_KIND)

#: pharmacy_transfers.status, as migration 54's CHECK spells it.
STATUSES: tuple[str, ...] = (
    "draft", "dispatched", "partially_received", "received", "cancelled")
IN_TRANSIT_STATUSES: tuple[str, ...] = ("dispatched", "partially_received")

STATUS_LABELS = {
    "draft": "Draft",
    "dispatched": "On its way",
    "partially_received": "Partly received",
    "received": "Received",
    "cancelled": "Cancelled",
}

#: A list filter that is not one status: everything still on its way.
OPEN_FILTER = "open"

#: Lines on one transfer (each line is one lot of one drug).
MAX_LINES = 200
#: Distinct drugs asked for on one transfer.
MAX_DRUGS = 100
MAX_UNITS = 1_000_000
MAX_TEXT = 1000

#: The witness rule for controlled transfers. True: every controlled dispatch
#: and receipt needs a witness, register on or off (strictest). False: only
#: while the NDPS register (ORB_PHARMACY_NDPS_REGISTER, grn-13) is on.
NDPS_WITNESS_ALWAYS = True

#: Lot statuses that hold the whole batch (a recall, a Not of Standard Quality
#: finding). Both are terminal in pharmacy_stock_service.LOT_STATUS_TRANSITIONS
#: (written off or returned to the vendor, never back to the shelf).
BATCH_HOLD_STATUSES: tuple[str, ...] = ("recalled", "nsq")

#: What a received line lands as when its lot left 'available' for any other
#: reason (quarantined, written off, returned, or the lot is gone): held for a
#: pharmacist to decide, never on the shelf.
LANDING_HOLD_STATUS = "quarantined"

#: When more than one hold applies to a line, the one it lands as: a recall
#: before NSQ before quarantine (quarantine alone could one day be released,
#: ruling GRN-RS2; recalled and NSQ never are).
_HOLD_RANK = {"recalled": 0, "nsq": 1, LANDING_HOLD_STATUS: 2}

#: Lot statuses in words, for messages and the audit row.
LOT_STATUS_WORDS = {
    "available": "available",
    "quarantined": "quarantined",
    "recalled": "recalled",
    "nsq": "found not of standard quality",
    "pending_inspection": "waiting for inspection",
    "written_off": "written off",
    "returned_to_vendor": "returned to the vendor",
}


class TransferError(ValueError):
    """A transfer request the caller must correct, or one the rules refuse.
    `code` maps to HTTP in the route; `detail` carries anything a screen needs
    beyond the message."""

    code = "transfer_error"

    def __init__(self, message: str, *, code: Optional[str] = None,
                 detail: Optional[dict] = None):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail or {}


# ═════════════════════════════════════════════════════════════════════════════
#  The leg in progress (for the NDPS register hook, grn-13)
# ═════════════════════════════════════════════════════════════════════════════

_LEG: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "pharmacy_transfer_leg", default=None)


def current_transfer_leg() -> Optional[dict]:
    """While a transfer posts its stock movements: {transfer_id, leg
    ('dispatch' or 'receipt'), from_store_id, to_store_id, actor_id,
    witness_id, controlled}. None at any other time. The NDPS register hook
    runs inside post_movement and reads the witness and the other store from
    here; nothing else should."""
    leg = _LEG.get()
    return dict(leg) if leg else None


@contextlib.contextmanager
def _leg(**fields: Any) -> Iterator[None]:
    token = _LEG.set(fields)
    try:
        yield
    finally:
        _LEG.reset(token)


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════

def _actor_id(actor: Mapping) -> str:
    uid = actor.get("id") if actor else None
    if not uid:
        raise TransferError("the request has no signed-in person behind it", code="no_actor")
    return str(uid)


def _may_ndps(actor: Mapping) -> bool:
    from permissions import PHARMACY_STORE_NDPS, has_permission

    return has_permission(str(actor.get("role") or ""), PHARMACY_STORE_NDPS)


def _text(value: Any, name: str, *, required: bool = False) -> Optional[str]:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise TransferError(f"{name} must be text", code="bad_value")
    if len(text) > MAX_TEXT:
        raise TransferError(f"{name} is at most {MAX_TEXT} characters", code="bad_value")
    if required and not text:
        raise TransferError(f"give {name}", code="missing_field")
    return text or None


def _whole(value: Any, name: str, *, minimum: int, maximum: int = MAX_UNITS) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TransferError(f"{name} must be a whole number", code="bad_value")
    if value < minimum or value > maximum:
        raise TransferError(f"{name} must be from {minimum} to {maximum}", code="bad_value")
    return value


def _audit(conn, actor: Mapping, action: str, *, summary: str, transfer_id: int,
           metadata: Optional[dict] = None, audit_ctx: Optional[Mapping] = None,
           outcome: str = "success") -> None:
    """One 'security' row. With conn, in the caller's transaction (fail-closed:
    a failed write raises AuditWriteError and the whole action rolls back)."""
    from services import audit_service

    ctx = audit_ctx or {}
    audit_service.log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="security",
        action_type=action,
        outcome=outcome,
        db=conn,
        summary_message=summary,
        actor_user_id=str(actor.get("id")) if actor.get("id") is not None else None,
        actor_role=actor.get("role"),
        target_type="pharmacy_transfer",
        target_id=str(transfer_id),
        request_path=ctx.get("request_path"),
        http_method=ctx.get("http_method"),
        ip_address=ctx.get("ip_address"),
        metadata=metadata or {},
    )


def _is_ndps(drug: Mapping) -> bool:
    return drug["ndps_category"] in stock.NDPS_CONTROLLED


def _drug_name(drug: Mapping) -> str:
    return drug["name"] or drug["id"]


def _store_name(store: Mapping) -> str:
    return store["name"] or store["code"]


# ═════════════════════════════════════════════════════════════════════════════
#  Stores and drugs: what may move, from where, to where
# ═════════════════════════════════════════════════════════════════════════════

_STORE_COLUMNS = "id, code, name, pharmacy_kind, is_active"


def _store_by_code(conn, code: Any, *, end: str):
    text = (code or "").strip().upper() if isinstance(code, str) else ""
    if not text:
        raise TransferError(f"choose the store the transfer goes {end}", code="store_required")
    row = conn.execute(f"SELECT {_STORE_COLUMNS} FROM stores WHERE upper(code) = ? "
                       "AND pharmacy_kind IS NOT NULL", (text,)).fetchone()
    if row is None:
        raise TransferError(f"no pharmacy store {code!r}", code="store_not_found")
    return row


def _store_by_id(conn, store_id: Any):
    return conn.execute(f"SELECT {_STORE_COLUMNS} FROM stores WHERE id = ?",
                        (store_id,)).fetchone()


def _check_store(store: Mapping) -> None:
    if store["pharmacy_kind"] not in TRANSFER_STORE_KINDS:
        raise TransferError(
            f"{_store_name(store)} does not take part in transfers. Transfers run between the "
            "dispensing pharmacy, sub-stores and the NDPS vault.", code="store_not_transferable")
    if not store["is_active"]:
        raise TransferError(f"{_store_name(store)} is closed. Reopen it in Masters first.",
                            code="store_inactive")


def _find_drug(conn, drug_id: Any):
    text = drug_id.strip() if isinstance(drug_id, str) else ""
    if not text:
        return None
    row = conn.execute("SELECT * FROM helix_medication_master WHERE id = ?", (text,)).fetchone()
    if row is not None:
        return row
    rows = conn.execute("SELECT * FROM helix_medication_master WHERE upper(id) = upper(?)",
                        (text,)).fetchall()
    return rows[0] if len(rows) == 1 else None


def route_problem(drug: Mapping, from_store: Mapping, to_store: Mapping) -> Optional[dict]:
    """Why this drug may not move from one store to the other, as {code,
    message}; None when it may. The rules are in the module note."""
    name = _drug_name(drug)
    from_kind, to_kind = from_store["pharmacy_kind"], to_store["pharmacy_kind"]
    if drug["drug_schedule"] is None or drug["ndps_category"] is None:
        return {"code": "drug_unclassified",
                "message": f"{name} is not classified. Record its schedule and NDPS class in "
                           "Masters before moving it."}
    if _is_ndps(drug):
        if {from_kind, to_kind} != {stock.NDPS_VAULT_KIND, stock.DISPENSING_STORE_KIND}:
            return {"code": "ndps_route_refused",
                    "message": f"{name} is a narcotic or psychotropic drug. It moves only "
                               "between the NDPS vault and the dispensing pharmacy."}
    elif to_kind == stock.NDPS_VAULT_KIND:
        return {"code": "vault_is_for_ndps",
                "message": f"{name} is not a narcotic or psychotropic drug, so it does not "
                           "go into the NDPS vault."}
    if to_kind == stock.DISPENSING_STORE_KIND:
        if not drug["is_active"]:
            return {"code": "drug_inactive",
                    "message": f"{name} is marked inactive in the drug master, so it is not "
                               "moved onto the dispensing shelf."}
        if (drug["identity_status"] or "active") != "active":
            return {"code": "drug_not_confirmed",
                    "message": f"{name} is a {drug['identity_status']} drug, not confirmed for "
                               "use, so it is not moved onto the dispensing shelf."}
    return None


def _check_route(drug: Mapping, from_store: Mapping, to_store: Mapping) -> None:
    problem = route_problem(drug, from_store, to_store)
    if problem:
        raise TransferError(problem["message"], code=problem["code"],
                            detail={"drug_id": drug["id"]})


def _check_landing(drug: Optional[Mapping], drug_id: str, to_store: Mapping) -> None:
    """At receipt, the one route rule read again: nothing lands on the
    dispensing shelf, where it would become dispensable, for a drug made
    inactive or unconfirmed while it was on its way. The units stay in transit,
    visible on the transfer, until the drug master is settled. (A drug
    classified narcotic in transit is still received, as a controlled transfer
    with its witness: refusing would strand stock already in the pharmacy.)"""
    if to_store["pharmacy_kind"] != stock.DISPENSING_STORE_KIND:
        return
    if drug is None:
        raise TransferError(f"no drug {drug_id!r} in the drug master", code="drug_not_found")
    code = None
    if not drug["is_active"]:
        code, why = "drug_inactive", "is marked inactive in the drug master"
    elif (drug["identity_status"] or "active") != "active":
        code, why = "drug_not_confirmed", f"is a {drug['identity_status']} drug, not confirmed"
    if code:
        raise TransferError(
            f"{_drug_name(drug)} {why}, so it is not received onto the dispensing shelf. The "
            "units stay on their way until the drug master is settled. Nothing was changed.",
            code=code, detail={"drug_id": drug["id"]})


def _controlled(conn, transfer: Mapping, drug_ids: Sequence[str]) -> bool:
    """Does this transfer touch controlled stock: the vault at either end, or
    any narcotic or psychotropic line? Read at the moment of each action, so a
    drug classified NDPS after the draft is treated as controlled."""
    kinds = {r["pharmacy_kind"] for r in conn.execute(
        "SELECT pharmacy_kind FROM stores WHERE id IN (?, ?)",
        (transfer["from_store_id"], transfer["to_store_id"])).fetchall()}
    if stock.NDPS_VAULT_KIND in kinds:
        return True
    ids = sorted({d for d in drug_ids if d})
    if not ids:
        return False
    marks = ",".join("?" for _ in ids)
    controlled = ",".join("?" for _ in stock.NDPS_CONTROLLED)
    return conn.execute(
        f"SELECT 1 FROM helix_medication_master WHERE id IN ({marks}) "
        f"AND ndps_category IN ({controlled}) LIMIT 1",
        (*ids, *stock.NDPS_CONTROLLED)).fetchone() is not None


def witness_required(controlled: bool) -> bool:
    if not controlled:
        return False
    if NDPS_WITNESS_ALWAYS:
        return True
    import config

    return bool(config.feature_enabled("ndps_register"))


# ═════════════════════════════════════════════════════════════════════════════
#  The FEFO issue plan
# ═════════════════════════════════════════════════════════════════════════════

def _lot_status_words(status: Any) -> str:
    return LOT_STATUS_WORDS.get(str(status), str(status).replace("_", " "))


def held_batches_for(conn, drug_ids: Sequence[str]) -> dict[str, dict[str, str]]:
    """{drug id: {batch key: 'recalled' or 'nsq'}} for these drugs: every
    batch with a lot recalled or found NSQ anywhere (any store, the legacy
    shelf included, any expiry). The batch key is
    pharmacy_stock_service._batch_key, so 'b 12a' is the batch 'B12A'. A
    recall outranks NSQ when a batch has both. Read-only."""
    ids = sorted({str(d) for d in drug_ids if d})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    holds = ",".join("?" for _ in BATCH_HOLD_STATUSES)
    held: dict[str, dict[str, str]] = {}
    for r in conn.execute(
            f"SELECT medication_id, lot_number, status FROM helix_inventory_lots "
            f"WHERE medication_id IN ({marks}) AND status IN ({holds})",
            (*ids, *BATCH_HOLD_STATUSES)).fetchall():
        key = stock._batch_key(r["lot_number"])
        if not key:
            continue
        mine = held.setdefault(r["medication_id"], {})
        if key not in mine or _HOLD_RANK[r["status"]] < _HOLD_RANK[mine[key]]:
            mine[key] = r["status"]
    return held


def held_batches(conn, drug_id: str) -> dict[str, str]:
    """{batch key: 'recalled' or 'nsq'} for one drug (held_batches_for)."""
    return held_batches_for(conn, [drug_id]).get(drug_id, {})


def usable_lots(conn, store_id: int, drug_id: str, *, today: Optional[str] = None) -> list[dict]:
    """The lots of one drug a transfer may take from one store, in the order
    it takes them: available, holding units, in date on the hospital's date,
    and not of a batch recalled or found NSQ anywhere (held_batches);
    earliest expiry first (ties by the order the lots were made). The same
    order pick_fefo_lot uses on the dispensing shelf."""
    held = held_batches(conn, drug_id)
    return [dict(r) for r in conn.execute(
        "SELECT l.* FROM helix_inventory_lots l WHERE l.store_id = ? AND l.medication_id = ? "
        "AND l.status = 'available' AND l.quantity > 0 AND date(l.expiration_date) >= ? "
        "ORDER BY date(l.expiration_date) ASC, l.rowid ASC",
        (store_id, drug_id, today or stock.site_today())).fetchall()
        if stock._batch_key(r["lot_number"]) not in held]


def _landing_status(source: Optional[Mapping], line: Mapping,
                    held: Mapping[str, str]) -> tuple[str, Optional[str]]:
    """What a received line lands as in the receiving store, read at the
    receipt: ('available', None) when its lot is still available and its
    batch is held nowhere; otherwise (hold status, why in words). The lot it
    left from no longer available, or gone, holds it quarantined (recalled or
    NSQ as that); its batch recalled or NSQ anywhere holds it as that. The
    strongest hold wins (_HOLD_RANK)."""
    holds: list[tuple[str, str]] = []
    if source is None:
        holds.append((LANDING_HOLD_STATUS, "the lot it left from can no longer be found"))
    elif source["status"] != "available":
        status = source["status"] if source["status"] in _HOLD_RANK else LANDING_HOLD_STATUS
        holds.append((status, "the lot it left from is now "
                              f"{_lot_status_words(source['status'])}"))
    batches = {stock._batch_key(b): b for b in (source["lot_number"] if source else None,
                                                 line["batch_no"]) if b}
    for key, batch in batches.items():
        hold = held.get(key)
        if hold:
            holds.append((hold, f"batch {batch} is {_lot_status_words(hold)} in the pharmacy"))
    if not holds:
        return "available", None
    holds.sort(key=lambda h: _HOLD_RANK[h[0]])
    return holds[0][0], "; ".join(dict.fromkeys(why for _, why in holds))


def issue_plan(conn, *, store_id: int, drug_id: str, units: int,
               today: Optional[str] = None) -> list[dict]:
    """Which lots `units` of one drug leave by: [{lot_id, batch_no,
    expiry_date, units}], earliest expiry first, a lot taken whole before the
    next is touched. Refuses (insufficient_stock, with what is usable) when
    the store does not hold enough usable units."""
    units = _whole(units, "units", minimum=1)
    lots = usable_lots(conn, store_id, drug_id, today=today)
    usable = sum(int(lot["quantity"] or 0) for lot in lots)
    if usable < units:
        raise TransferError(
            f"the store holds {usable} usable units of this drug, fewer than the {units} asked "
            "for. Nothing was changed.", code="insufficient_stock",
            detail={"drug_id": drug_id, "usable_units": usable, "asked_units": units})
    plan: list[dict] = []
    left = units
    for lot in lots:
        if left <= 0:
            break
        take = min(left, int(lot["quantity"]))
        plan.append({"lot_id": lot["id"], "batch_no": lot["lot_number"],
                     "expiry_date": str(lot["expiration_date"])[:10], "units": take})
        left -= take
    return plan


def _clean_requests(raw_lines: Any) -> list[dict]:
    """[{drug_id, units}] from the request, one per drug."""
    if not isinstance(raw_lines, (list, tuple)) or not raw_lines:
        raise TransferError("a transfer needs at least one drug", code="empty_document")
    if len(raw_lines) > MAX_DRUGS:
        raise TransferError(f"a transfer carries at most {MAX_DRUGS} drugs", code="bad_value")
    out: list[dict] = []
    seen: set[str] = set()
    for n, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, Mapping):
            raise TransferError("each line is an object", code="bad_line")
        drug_id = raw.get("helix_medication_id")
        drug_id = drug_id.strip() if isinstance(drug_id, str) else ""
        if not drug_id:
            raise TransferError(f"line {n} names no drug", code="bad_line")
        if drug_id.upper() in seen:
            raise TransferError(f"{drug_id} is on the transfer twice; put it on one line",
                                code="duplicate_line")
        seen.add(drug_id.upper())
        out.append({"drug_id": drug_id,
                    "units": _whole(raw.get("quantity_units"), f"line {n} units", minimum=1)})
    return out


def _plan_lines(conn, requests: list[dict], from_store: Mapping, to_store: Mapping,
                today: str) -> list[dict]:
    """The transfer lines for these requests: each drug checked for the route,
    then planned FEFO. Every drug is planned before anything is refused for
    stock, so the refusal can name every short drug at once."""
    lines: list[dict] = []
    short: list[dict] = []
    for req in requests:
        drug = _find_drug(conn, req["drug_id"])
        if drug is None:
            raise TransferError(f"no drug {req['drug_id']!r} in the drug master",
                                code="drug_not_found")
        _check_route(drug, from_store, to_store)
        try:
            plan = issue_plan(conn, store_id=int(from_store["id"]), drug_id=drug["id"],
                              units=req["units"], today=today)
        except TransferError as exc:
            if exc.code != "insufficient_stock":
                raise
            short.append({**exc.detail, "drug_name": _drug_name(drug)})
            continue
        for part in plan:
            lines.append({"drug_id": drug["id"], **part})
    if short:
        names = ", ".join(f"{s['drug_name']} ({s['usable_units']} usable, {s['asked_units']} "
                          "asked)" for s in short)
        raise TransferError(
            f"{_store_name(from_store)} does not hold enough usable stock: {names}. Nothing was "
            "changed.", code="insufficient_stock", detail={"short": short})
    if len(lines) > MAX_LINES:
        raise TransferError(
            f"this transfer would take stock from {len(lines)} lots; one transfer carries at "
            f"most {MAX_LINES}. Split it into two.", code="too_many_lines")
    return lines


def _insert_lines(conn, transfer_id: int, lines: list[dict]) -> None:
    for n, line in enumerate(lines, start=1):
        conn.execute(
            "INSERT INTO pharmacy_transfer_lines (pharmacy_transfer_id, line_no, "
            "helix_medication_id, from_lot_id, batch_no, expiry_date, quantity_units) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (transfer_id, n, line["drug_id"], line["lot_id"], line["batch_no"],
             line["expiry_date"], line["units"]))


# ═════════════════════════════════════════════════════════════════════════════
#  Reading transfers
# ═════════════════════════════════════════════════════════════════════════════

def _series_configured(conn) -> bool:
    row = conn.execute("SELECT is_configured FROM document_number_series WHERE series_code = ?",
                       (NUMBER_SERIES,)).fetchone()
    return bool(row and row["is_configured"])


def _user_names(conn, ids: set) -> dict[str, str]:
    ids = {str(i) for i in ids if i}
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {r["id"]: r["name"] for r in conn.execute(
        f"SELECT id, name FROM users WHERE id IN ({marks})", tuple(ids)).fetchall()}


def _movement_units(conn, key: str) -> Optional[int]:
    row = conn.execute("SELECT quantity_change FROM helix_stock_movements "
                       "WHERE idempotency_key = ?", (key,)).fetchone()
    return int(row["quantity_change"]) if row else None


def _out_key(transfer_id: int, line_id: int) -> str:
    return f"{REFERENCE_TYPE}:{transfer_id}:line:{line_id}:out"


def _in_key(transfer_id: int, line_id: int) -> str:
    return f"{REFERENCE_TYPE}:{transfer_id}:line:{line_id}:in"


def _lines(conn, transfer_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT tl.*, m.name AS drug_name, m.generic_name, m.form, m.strength, "
        "m.dispense_unit, m.ndps_category, landed.status AS to_lot_status "
        "FROM pharmacy_transfer_lines tl "
        "LEFT JOIN helix_medication_master m ON m.id = tl.helix_medication_id "
        "LEFT JOIN helix_inventory_lots landed ON landed.id = tl.to_lot_id "
        "WHERE tl.pharmacy_transfer_id = ? ORDER BY tl.line_no", (transfer_id,)).fetchall()]


def _raw(conn, transfer_id: Any):
    try:
        tid = int(transfer_id)
    except (TypeError, ValueError):
        return None
    return conn.execute("SELECT * FROM pharmacy_transfers WHERE id = ?", (tid,)).fetchone()


def _decorate(conn, row: Mapping, *, with_lines: bool = True) -> dict:
    doc = dict(row)
    tid = int(doc["id"])
    stores = {s["id"]: s for s in conn.execute(
        f"SELECT {_STORE_COLUMNS} FROM stores WHERE id IN (?, ?)",
        (doc["from_store_id"], doc["to_store_id"])).fetchall()}
    for end in ("from", "to"):
        s = stores.get(doc[f"{end}_store_id"])
        doc[f"{end}_store_code"] = s["code"] if s else None
        doc[f"{end}_store_name"] = _store_name(s) if s else None
        doc[f"{end}_store_kind"] = s["pharmacy_kind"] if s else None
    names = _user_names(conn, {doc.get("created_by"), doc.get("dispatched_by"),
                               doc.get("received_by"), doc.get("cancelled_by")})
    for who in ("created_by", "dispatched_by", "received_by", "cancelled_by"):
        doc[f"{who}_name"] = names.get(str(doc.get(who))) if doc.get(who) else None
    doc["status_label"] = STATUS_LABELS.get(doc["status"], doc["status"])
    doc["number_series_configured"] = _series_configured(conn)
    lines = _lines(conn, tid)
    doc["line_count"] = len(lines)
    doc["total_units"] = sum(int(line["quantity_units"]) for line in lines)
    doc["in_transit_units"] = (sum(int(line["quantity_units"]) for line in lines
                                   if line["received_units"] is None)
                               if doc["status"] in IN_TRANSIT_STATUSES else 0)
    doc["controlled"] = _controlled(conn, doc, [line["helix_medication_id"] for line in lines])
    doc["witness_required"] = witness_required(doc["controlled"])
    if with_lines:
        for line in lines:
            received = line["received_units"]
            line["shortfall_units"] = (int(line["quantity_units"]) - int(received)
                                       if received is not None else None)
            line["ndps"] = line.pop("ndps_category", None) in stock.NDPS_CONTROLLED
            line["drug_name"] = line["drug_name"] or line["helix_medication_id"]
            # The status now of the lot the units went into: anything but
            # 'available' means they are held off the shelf.
            line["to_lot_status_label"] = (_lot_status_words(line["to_lot_status"])
                                           if line["to_lot_status"] else None)
        doc["lines"] = lines
    return doc


def get_transfer(transfer_id: Any) -> Optional[dict]:
    with db_session() as conn:
        row = _raw(conn, transfer_id)
        return _decorate(conn, row) if row else None


def list_transfers(*, status: Optional[str] = None, store_id: Optional[int] = None,
                   limit: int = 100) -> list[dict]:
    """Newest first. `status` is one status or 'open' (on its way: dispatched
    or partly received); `store_id` keeps transfers leaving or entering it."""
    sql = "SELECT * FROM pharmacy_transfers WHERE 1 = 1"
    params: list[Any] = []
    if status:
        if status == OPEN_FILTER:
            sql += " AND status IN (?, ?)"
            params.extend(IN_TRANSIT_STATUSES)
        elif status in STATUSES:
            sql += " AND status = ?"
            params.append(status)
        else:
            raise TransferError(f"unknown transfer status {status!r}", code="bad_value")
    if store_id is not None:
        sql += " AND (from_store_id = ? OR to_store_id = ?)"
        params.extend([store_id, store_id])
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with db_session() as conn:
        return [_decorate(conn, r, with_lines=False)
                for r in conn.execute(sql, params).fetchall()]


def transfer_balance(conn, transfer_id: int) -> dict:
    """Does every line of this transfer add up? Read-only. For each line: the
    units its transfer_out took (out_units, positive), the units its
    transfer_in brought (in_units), and its recorded shortfall. A line is
    balanced when out_units is the dispatched quantity (or 0 before dispatch),
    in_units is the received quantity (or 0 before receipt), out_units ==
    in_units + shortfall, and a shortfall carries its reason."""
    row = _raw(conn, transfer_id)
    if row is None:
        raise TransferError(f"no transfer {transfer_id}", code="not_found")
    dispatched = row["dispatched_by"] is not None
    out = []
    for line in conn.execute("SELECT * FROM pharmacy_transfer_lines WHERE "
                             "pharmacy_transfer_id = ? ORDER BY line_no",
                             (row["id"],)).fetchall():
        out_change = _movement_units(conn, _out_key(row["id"], line["id"]))
        in_change = _movement_units(conn, _in_key(row["id"], line["id"]))
        out_units = -out_change if out_change is not None else 0
        in_units = in_change if in_change is not None else 0
        qty = int(line["quantity_units"])
        received = line["received_units"]
        shortfall = qty - int(received) if received is not None else 0
        balanced = (
            out_units == (qty if dispatched else 0)
            and in_units == (int(received) if received is not None else 0)
            and (received is None or out_units == in_units + shortfall)
            and (shortfall == 0 or bool((line["discrepancy_reason"] or "").strip()))
        )
        out.append({"line_id": line["id"], "line_no": line["line_no"], "quantity_units": qty,
                    "out_units": out_units, "in_units": in_units,
                    "received_units": received, "shortfall_units": shortfall,
                    "balanced": balanced})
    return {"transfer_id": row["id"], "status": row["status"],
            "balanced": all(line["balanced"] for line in out), "lines": out}


# ═════════════════════════════════════════════════════════════════════════════
#  What the transfer screen offers
# ═════════════════════════════════════════════════════════════════════════════

def options(actor: Mapping) -> dict:
    """The stores a transfer may run between, the people who may witness a
    controlled transfer (never the caller, only active users with a signing
    PIN; empty for a caller without pharmacy_store.ndps), and the site's
    settings for the screen."""
    from permissions import PHARMACY_STORE_NDPS, ROLE_PERMISSIONS

    me = str(actor.get("id") or "")
    may_ndps = _may_ndps(actor)
    with db_session() as conn:
        marks = ",".join("?" for _ in TRANSFER_STORE_KINDS)
        stores = [{"id": s["id"], "code": s["code"], "name": _store_name(s),
                   "pharmacy_kind": s["pharmacy_kind"]}
                  for s in conn.execute(
                      f"SELECT {_STORE_COLUMNS} FROM stores WHERE is_active = 1 "
                      f"AND pharmacy_kind IN ({marks}) ORDER BY name, code",
                      TRANSFER_STORE_KINDS).fetchall()]
        witnesses: list[dict] = []
        if may_ndps:
            roles = sorted(r for r, perms in ROLE_PERMISSIONS.items()
                           if isinstance(r, str) and PHARMACY_STORE_NDPS in perms)
            if roles:
                role_marks = ",".join("?" for _ in roles)
                witnesses = [{"id": u["id"], "name": u["name"] or u["id"]}
                             for u in conn.execute(
                                 f"SELECT id, name FROM users WHERE role IN ({role_marks}) "
                                 "AND (is_active IS NULL OR is_active = 1) AND id <> ? "
                                 "AND pin_hash IS NOT NULL AND pin_hash <> '' "
                                 "ORDER BY name, id LIMIT 500", (*roles, me)).fetchall()]
        configured = _series_configured(conn)
    return {"stores": stores, "witnesses": witnesses, "may_ndps": may_ndps,
            "ndps_witness_always": NDPS_WITNESS_ALWAYS,
            "number_series_configured": configured, "max_drugs": MAX_DRUGS}


def store_stock(store_code: Any, *, today: Optional[str] = None) -> dict:
    """What a store could send now: each drug with usable units there (the
    lots usable_lots would plan, so a batch recalled or NSQ anywhere is not
    offered), with its classification and its earliest usable expiry.
    Read-only."""
    today = today or stock.site_today()
    with db_session() as conn:
        store = _store_by_code(conn, store_code, end="from")
        rows = conn.execute(
            "SELECT l.medication_id AS drug_id, l.lot_number, l.quantity, "
            "date(l.expiration_date) AS expiry, m.name AS drug_name, m.form, m.strength, "
            "m.dispense_unit, m.ndps_category, m.drug_schedule "
            "FROM helix_inventory_lots l "
            "LEFT JOIN helix_medication_master m ON m.id = l.medication_id "
            "WHERE l.store_id = ? AND l.status = 'available' AND l.quantity > 0 "
            "AND date(l.expiration_date) >= ? "
            "ORDER BY m.name, l.medication_id, l.rowid",
            (store["id"], today)).fetchall()
        held = held_batches_for(conn, [r["drug_id"] for r in rows])
        by_drug: dict[str, dict] = {}
        for r in rows:
            if stock._batch_key(r["lot_number"]) in held.get(r["drug_id"], {}):
                continue
            drug = by_drug.get(r["drug_id"])
            if drug is None:
                if len(by_drug) >= 2000:
                    continue
                drug = by_drug[r["drug_id"]] = {
                    "drug_id": r["drug_id"], "drug_name": r["drug_name"] or r["drug_id"],
                    "form": r["form"], "strength": r["strength"],
                    "dispense_unit": r["dispense_unit"],
                    "ndps": r["ndps_category"] in stock.NDPS_CONTROLLED,
                    "classified": (r["ndps_category"] is not None
                                   and r["drug_schedule"] is not None),
                    "usable_units": 0, "lot_count": 0, "earliest_expiry": r["expiry"]}
            drug["usable_units"] += int(r["quantity"] or 0)
            drug["lot_count"] += 1
            first = drug["earliest_expiry"]
            if r["expiry"] and (not first or r["expiry"] < first):
                drug["earliest_expiry"] = r["expiry"]
        drugs = list(by_drug.values())
    return {"store": {"id": store["id"], "code": store["code"], "name": _store_name(store),
                      "pharmacy_kind": store["pharmacy_kind"]},
            "today": today, "drugs": drugs}


# ═════════════════════════════════════════════════════════════════════════════
#  The witness
# ═════════════════════════════════════════════════════════════════════════════

def verify_witness(*, actor: Mapping, witness_id: Any, witness_pin: Any, transfer_id: int,
                   leg: str, audit_ctx: Optional[Mapping] = None) -> str:
    """Confirm the witness for a controlled dispatch or receipt, BEFORE the
    action's own transaction opens (verify_pin may open a session of its own to
    upgrade a legacy hash). Returns the witness's user id, or refuses:
    witness_required (422), witness_same_person, witness_not_eligible,
    witness_no_pin, witness_pin_wrong (403), witness_locked (429). A wrong PIN
    is counted toward that person's lockout and audited."""
    from permissions import PHARMACY_STORE_NDPS, has_permission
    from services import pin_security

    me = _actor_id(actor)
    wid = witness_id.strip() if isinstance(witness_id, str) else ""
    pin = witness_pin if isinstance(witness_pin, str) else ""
    if not wid or not pin:
        raise TransferError(
            "this transfer carries controlled stock, so a second pharmacist witnesses it: "
            "choose the witness and have them enter their signing PIN.",
            code="witness_required")
    if wid == me:
        raise TransferError("the witness must be somebody other than you",
                            code="witness_same_person")
    if pin_security.is_pin_locked(wid):
        raise TransferError("too many wrong PINs for this witness. PIN entry is locked for 15 "
                            "minutes.", code="witness_locked")
    with db_session() as conn:
        row = conn.execute("SELECT id, name, role, pin_hash, is_active FROM users WHERE id = ?",
                           (wid,)).fetchone()
    if (row is None or (row["is_active"] is not None and not row["is_active"])
            or not has_permission(str(row["role"] or ""), PHARMACY_STORE_NDPS)):
        raise TransferError("that person cannot witness a controlled transfer",
                            code="witness_not_eligible")
    if not row["pin_hash"]:
        raise TransferError(
            "the witness has no signing PIN yet. Ask an administrator to set one.",
            code="witness_no_pin")
    if not pin_security.verify_pin(row, pin):
        pin_security.record_pin_failure(wid)
        _audit(None, actor, "pharmacy_transfer_witness_refused",
               summary=f"Witness PIN refused on the {leg} of pharmacy transfer {transfer_id}",
               transfer_id=transfer_id, outcome="failure",
               metadata={"transfer_id": str(transfer_id), "leg": leg, "witness_id": wid},
               audit_ctx=audit_ctx)
        raise TransferError("the witness's PIN is not right. Nothing was changed.",
                            code="witness_pin_wrong")
    pin_security.clear_pin_failures(wid)
    return wid


def _require_ndps(actor: Mapping, controlled: bool) -> None:
    if controlled and not _may_ndps(actor):
        raise TransferError(
            "this transfer carries controlled stock (the NDPS vault, or a narcotic or "
            "psychotropic drug), which needs the NDPS permission. Nothing was changed.",
            code="ndps_permission_needed")


def _witness_for(actor: Mapping, transfer_id: int, leg: str, witness_id: Any, witness_pin: Any,
                 audit_ctx: Optional[Mapping]) -> tuple[bool, Optional[str]]:
    """(controlled, verified witness id or None), read before the write."""
    with db_session() as conn:
        row = _raw(conn, transfer_id)
        if row is None:
            raise TransferError(f"no transfer {transfer_id}", code="not_found")
        drug_ids = [r[0] for r in conn.execute(
            "SELECT helix_medication_id FROM pharmacy_transfer_lines "
            "WHERE pharmacy_transfer_id = ?", (row["id"],)).fetchall()]
        controlled = _controlled(conn, row, drug_ids)
    _require_ndps(actor, controlled)
    if not witness_required(controlled):
        return controlled, None
    return controlled, verify_witness(actor=actor, witness_id=witness_id,
                                      witness_pin=witness_pin, transfer_id=int(row["id"]),
                                      leg=leg, audit_ctx=audit_ctx)


def _load(conn, transfer_id: Any):
    row = _raw(conn, transfer_id)
    if row is None:
        raise TransferError(f"no transfer {transfer_id}", code="not_found")
    return row


def _stock_failure(exc: stock.StockError, *, fallback: str) -> TransferError:
    code = getattr(exc, "code", None) or fallback
    return TransferError(str(exc), code=code)


# ═════════════════════════════════════════════════════════════════════════════
#  Writes
# ═════════════════════════════════════════════════════════════════════════════

def create_transfer(*, from_store_code: Any, to_store_code: Any, lines: Any, actor: Mapping,
                    remarks: Any = None, idempotency_key: Any = None,
                    audit_ctx: Optional[Mapping] = None) -> dict:
    """Raise a draft with its FEFO issue plan (the write tier, plus the NDPS
    permission for a controlled transfer). Nothing moves until dispatch.
    Returns {"transfer", "replayed"}."""
    actor_id = _actor_id(actor)
    requests = _clean_requests(lines)
    note = _text(remarks, "remarks")
    key = _text(idempotency_key, "request key")
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        from_store = _store_by_code(conn, from_store_code, end="from")
        to_store = _store_by_code(conn, to_store_code, end="to")
        if from_store["id"] == to_store["id"]:
            raise TransferError("a transfer goes from one store to another", code="same_store")
        _check_store(from_store)
        _check_store(to_store)
        if key:
            prior = conn.execute("SELECT * FROM pharmacy_transfers WHERE idempotency_key = ?",
                                 (key,)).fetchone()
            if prior is not None:
                asked = {}
                for line in conn.execute(
                        "SELECT helix_medication_id, quantity_units FROM pharmacy_transfer_lines "
                        "WHERE pharmacy_transfer_id = ?", (prior["id"],)).fetchall():
                    did = str(line["helix_medication_id"]).upper()
                    asked[did] = asked.get(did, 0) + int(line["quantity_units"])
                wanted = {r["drug_id"].upper(): r["units"] for r in requests}
                if ((prior["from_store_id"], prior["to_store_id"], str(prior["created_by"]))
                        != (from_store["id"], to_store["id"], actor_id) or asked != wanted):
                    raise TransferError("this request key was already used for a different "
                                        "transfer. Nothing was changed.",
                                        code="idempotency_conflict")
                return {"transfer": _decorate(conn, prior), "replayed": True}
        planned = _plan_lines(conn, requests, from_store, to_store, today)
        controlled = _controlled(conn, {"from_store_id": from_store["id"],
                                        "to_store_id": to_store["id"]},
                                 [line["drug_id"] for line in planned])
        _require_ndps(actor, controlled)
        tid = conn.execute(
            "INSERT INTO pharmacy_transfers (from_store_id, to_store_id, status, created_by, "
            "idempotency_key, remarks) VALUES (?, ?, 'draft', ?, ?, ?)",
            (from_store["id"], to_store["id"], actor_id, key, note)).lastrowid
        _insert_lines(conn, tid, planned)
        _audit(conn, actor, "pharmacy_transfer_created",
               summary=f"Pharmacy transfer drafted from {_store_name(from_store)} to "
                       f"{_store_name(to_store)} ({len(requests)} drugs)",
               transfer_id=tid,
               metadata={"transfer_id": str(tid), "from_store_code": from_store["code"],
                         "to_store_code": to_store["code"], "controlled": controlled,
                         "drugs": [{"drug_id": r["drug_id"], "units": r["units"]}
                                   for r in requests],
                         "plan": [{"lot_id": p["lot_id"], "drug_id": p["drug_id"],
                                   "units": p["units"]} for p in planned]},
               audit_ctx=audit_ctx)
        doc = _decorate(conn, _load(conn, tid))
    return {"transfer": doc, "replayed": False}


def replan_transfer(transfer_id: Any, *, actor: Mapping,
                    audit_ctx: Optional[Mapping] = None) -> dict:
    """Plan a draft again from the same drugs and totals, against the stock
    there is now (a lot on the old plan was used, quarantined or expired).
    Only a draft; the old plan is kept in the audit row."""
    _actor_id(actor)
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        row = _load(conn, transfer_id)
        if row["status"] != "draft":
            raise TransferError("only a draft is planned again", code="illegal_transition")
        old = [dict(r) for r in conn.execute(
            "SELECT * FROM pharmacy_transfer_lines WHERE pharmacy_transfer_id = ? "
            "ORDER BY line_no", (row["id"],)).fetchall()]
        totals: dict[str, int] = {}
        for line in old:
            totals[line["helix_medication_id"]] = (totals.get(line["helix_medication_id"], 0)
                                                   + int(line["quantity_units"]))
        from_store = _store_by_id(conn, row["from_store_id"])
        to_store = _store_by_id(conn, row["to_store_id"])
        if from_store is None or to_store is None:
            raise TransferError("a store on this transfer no longer exists",
                                code="store_not_found")
        _check_store(from_store)
        _check_store(to_store)
        requests = [{"drug_id": d, "units": u} for d, u in totals.items()]
        planned = _plan_lines(conn, requests, from_store, to_store, today)
        _require_ndps(actor, _controlled(conn, row, [line["drug_id"] for line in planned]))
        conn.execute("DELETE FROM pharmacy_transfer_lines WHERE pharmacy_transfer_id = ?",
                     (row["id"],))
        _insert_lines(conn, row["id"], planned)
        _audit(conn, actor, "pharmacy_transfer_replanned",
               summary=f"Pharmacy transfer {row['id']} planned again against current stock",
               transfer_id=row["id"],
               metadata={"transfer_id": str(row["id"]),
                         "old_plan": [{"lot_id": line["from_lot_id"],
                                       "drug_id": line["helix_medication_id"],
                                       "units": line["quantity_units"]} for line in old],
                         "plan": [{"lot_id": p["lot_id"], "drug_id": p["drug_id"],
                                   "units": p["units"]} for p in planned]},
               audit_ctx=audit_ctx)
        doc = _decorate(conn, _load(conn, row["id"]))
    return {"transfer": doc, "replayed": False}


def _dispatch_checks(conn, row: Mapping) -> Optional[tuple]:
    """Everything a dispatch checks before any stock moves, read again inside
    the write: None when the transfer was already dispatched (a replay), else
    (lines, from_store, to_store, {drug id: drug row}). Refuses a cancelled
    transfer, a closed or unfit store, and a drug the route no longer allows
    (a drug classified narcotic after the draft, say)."""
    if row["status"] != "draft":
        if row["dispatched_by"] is not None and row["status"] != "cancelled":
            return None
        raise TransferError(f"a {STATUS_LABELS.get(row['status'], row['status']).lower()} "
                            "transfer is not dispatched", code="illegal_transition")
    lines = [dict(r) for r in conn.execute(
        "SELECT * FROM pharmacy_transfer_lines WHERE pharmacy_transfer_id = ? "
        "ORDER BY line_no", (row["id"],)).fetchall()]
    if not lines:
        raise TransferError("this transfer has no lines", code="empty_document")
    from_store = _store_by_id(conn, row["from_store_id"])
    to_store = _store_by_id(conn, row["to_store_id"])
    if from_store is None or to_store is None:
        raise TransferError("a store on this transfer no longer exists",
                            code="store_not_found")
    _check_store(from_store)
    _check_store(to_store)
    drugs: dict[str, Any] = {}
    for line in lines:
        did = line["helix_medication_id"]
        if did not in drugs:
            drug = _find_drug(conn, did)
            if drug is None:
                raise TransferError(f"no drug {did!r} in the drug master",
                                    code="drug_not_found")
            _check_route(drug, from_store, to_store)
            drugs[did] = drug
    return lines, from_store, to_store, drugs


def dispatch_transfer(transfer_id: Any, *, actor: Mapping, witness_id: Any = None,
                      witness_pin: Any = None, audit_ctx: Optional[Mapping] = None) -> dict:
    """Send a draft: one transfer_out per line, all or nothing. Each line's lot
    must still be in the sending store, available, of a batch recalled or NSQ
    nowhere (held_batches), in date and hold the planned units; otherwise
    nothing moves (409 plan_stale: plan it again).
    A dispatch already made is answered with the transfer and replayed True,
    and posts nothing. The PHTRF number is drawn after the commit."""
    actor_id = _actor_id(actor)
    with db_session() as conn:
        row = _load(conn, transfer_id)
        if _dispatch_checks(conn, row) is None:
            return {"transfer": _decorate(conn, row), "replayed": True}
    _, witness = _witness_for(actor, transfer_id, "dispatch", witness_id, witness_pin,
                              audit_ctx)
    today = stock.site_today()
    with db_session(immediate=True) as conn:
        row = _load(conn, transfer_id)
        tid = int(row["id"])
        checked = _dispatch_checks(conn, row)
        if checked is None:
            return {"transfer": _decorate(conn, row), "replayed": True}
        lines, from_store, to_store, drugs = checked
        now_controlled = _controlled(conn, row, [line["helix_medication_id"] for line in lines])
        _require_ndps(actor, now_controlled)
        if witness_required(now_controlled) and witness is None:
            raise TransferError("this transfer now carries controlled stock, so a witness is "
                                "needed. Nothing was changed.", code="witness_required")
        moved: list[dict] = []
        held = held_batches_for(conn, list(drugs))
        with _leg(transfer_id=tid, leg="dispatch", from_store_id=from_store["id"],
                  to_store_id=to_store["id"], actor_id=actor_id, witness_id=witness,
                  controlled=now_controlled):
            for line in lines:
                lot = conn.execute("SELECT * FROM helix_inventory_lots WHERE id = ?",
                                   (line["from_lot_id"],)).fetchone()
                qty = int(line["quantity_units"])
                stale = None
                batch_hold = (held.get(line["helix_medication_id"], {})
                              .get(stock._batch_key(lot["lot_number"])) if lot else None)
                if lot is None or lot["store_id"] != from_store["id"]:
                    stale = "the lot is no longer in the sending store"
                elif lot["medication_id"] != line["helix_medication_id"]:
                    stale = "the lot holds a different drug"
                elif lot["status"] != "available":
                    stale = f"the lot is now {str(lot['status']).replace('_', ' ')}"
                elif batch_hold:
                    stale = (f"batch {lot['lot_number']} is now "
                             f"{_lot_status_words(batch_hold)} in the pharmacy")
                elif not lot["expiration_date"] or str(lot["expiration_date"])[:10] < today:
                    stale = "the lot is past its expiry"
                elif int(lot["quantity"] or 0) < qty:
                    stale = f"the lot holds {int(lot['quantity'] or 0)} units, not {qty}"
                if stale:
                    raise TransferError(
                        f"line {line['line_no']} ({drugs[line['helix_medication_id']]['name']}, "
                        f"batch {line['batch_no']}): {stale}. Plan the transfer again. "
                        "Nothing was changed.", code="plan_stale",
                        detail={"line_no": line["line_no"], "lot_id": line["from_lot_id"]})
                try:
                    movement = stock.post_movement(
                        conn, lot_id=lot["id"], movement_type="transfer_out",
                        quantity_change=-qty, actor_id=actor_id, reference_type=REFERENCE_TYPE,
                        reference_id=str(tid), idempotency_key=_out_key(tid, line["id"]),
                        remarks=f"To {_store_name(to_store)}",
                        unit_cost_paise=lot["unit_cost_paise"])
                except stock.InsufficientStock as exc:
                    raise TransferError(
                        f"line {line['line_no']}: {exc} Plan the transfer again.",
                        code="plan_stale", detail={"line_no": line["line_no"]}) from exc
                except stock.StockError as exc:
                    raise _stock_failure(exc, fallback="stock_error") from exc
                moved.append({"line_no": line["line_no"], "lot_id": lot["id"],
                              "drug_id": line["helix_medication_id"],
                              "batch_no": line["batch_no"], "units": qty,
                              "movement_id": movement["id"]})
        cur = conn.execute(
            "UPDATE pharmacy_transfers SET status = 'dispatched', dispatched_by = ?, "
            "dispatched_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'draft'",
            (actor_id, tid))
        if cur.rowcount != 1:
            raise TransferError("this transfer changed while you were working on it. Nothing "
                                "was changed.", code="illegal_transition")
        _audit(conn, actor, "pharmacy_transfer_dispatched",
               summary=f"Pharmacy transfer {tid} sent from {_store_name(from_store)} to "
                       f"{_store_name(to_store)}",
               transfer_id=tid,
               metadata={"transfer_id": str(tid), "from_store_code": from_store["code"],
                         "to_store_code": to_store["code"], "controlled": now_controlled,
                         "witness_id": witness, "lines": moved,
                         "units": sum(m["units"] for m in moved)},
               audit_ctx=audit_ctx)
    from services import procurement_service as procurement

    if procurement._stamp_document_number(NUMBER_KIND, tid) is None:
        logger.error("pharmacy transfer %s was dispatched but not numbered; stamp it again", tid)
    with db_session() as conn:
        doc = _decorate(conn, _load(conn, tid))
    return {"transfer": doc, "replayed": False}


def _clean_receipts(raw: Any) -> list[dict]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise TransferError("say what arrived on at least one line", code="empty_document")
    if len(raw) > MAX_LINES:
        raise TransferError(f"a receipt covers at most {MAX_LINES} lines", code="bad_value")
    out: list[dict] = []
    seen: set[int] = set()
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise TransferError("each received line is an object", code="bad_line")
        line_id = item.get("line_id")
        if isinstance(line_id, bool) or not isinstance(line_id, int):
            raise TransferError(f"received line {n} names no transfer line", code="bad_line")
        if line_id in seen:
            raise TransferError("a line is received once per receipt", code="duplicate_line")
        seen.add(line_id)
        out.append({"line_id": line_id,
                    "received_units": _whole(item.get("received_units"),
                                             f"received units on line {n}", minimum=0),
                    "reason": _text(item.get("discrepancy_reason"), "the reason")})
    return out


def receive_transfer(transfer_id: Any, *, lines: Any, actor: Mapping, witness_id: Any = None,
                     witness_pin: Any = None, audit_ctx: Optional[Mapping] = None) -> dict:
    """Receive some or all of a dispatched transfer's lines (the write tier,
    plus the NDPS permission and a witness when controlled). Never by the
    person who dispatched it (same_person). Each received line puts what
    arrived into a lot of the same drug, batch and expiry in the receiving
    store by one transfer_in; fewer units than were sent needs a reason, and
    more is refused. Lines not named stay in transit. A line whose lot is no
    longer available, or whose batch is recalled or NSQ anywhere, lands held
    (_landing_status), never available."""
    actor_id = _actor_id(actor)
    receipts = _clean_receipts(lines)
    with db_session() as conn:
        row = _load(conn, transfer_id)
        # Refusals that need no witness come first, so a witness's PIN is
        # never asked for, or counted wrong, on a receipt that cannot happen.
        if row["status"] not in IN_TRANSIT_STATUSES:
            raise TransferError(
                f"a {STATUS_LABELS.get(row['status'], row['status']).lower()} transfer is not "
                "received", code="illegal_transition")
        if row["dispatched_by"] is not None and str(row["dispatched_by"]) == actor_id:
            raise TransferError(
                "you sent this transfer, so somebody else receives it. Nothing was changed.",
                code="same_person")
    _, witness = _witness_for(actor, transfer_id, "receipt", witness_id, witness_pin,
                              audit_ctx)
    with db_session(immediate=True) as conn:
        row = _load(conn, transfer_id)
        tid = int(row["id"])
        if row["status"] not in IN_TRANSIT_STATUSES:
            raise TransferError(
                f"a {STATUS_LABELS.get(row['status'], row['status']).lower()} transfer is not "
                "received", code="illegal_transition")
        if str(row["dispatched_by"]) == actor_id:
            raise TransferError(
                "you sent this transfer, so somebody else receives it. Nothing was changed.",
                code="same_person")
        to_store = _store_by_id(conn, row["to_store_id"])
        if to_store is None:
            raise TransferError("the receiving store no longer exists", code="store_not_found")
        if not to_store["is_active"]:
            raise TransferError(f"{_store_name(to_store)} is closed. Reopen it in Masters to "
                                "receive into it.", code="store_inactive")
        all_lines = {int(r["id"]): dict(r) for r in conn.execute(
            "SELECT * FROM pharmacy_transfer_lines WHERE pharmacy_transfer_id = ?",
            (tid,)).fetchall()}
        now_controlled = _controlled(conn, row, [line["helix_medication_id"]
                                                 for line in all_lines.values()])
        _require_ndps(actor, now_controlled)
        if witness_required(now_controlled) and witness is None:
            raise TransferError("this transfer now carries controlled stock, so a witness is "
                                "needed. Nothing was changed.", code="witness_required")
        received: list[dict] = []
        with _leg(transfer_id=tid, leg="receipt", from_store_id=row["from_store_id"],
                  to_store_id=to_store["id"], actor_id=actor_id, witness_id=witness,
                  controlled=now_controlled):
            for item in sorted(receipts, key=lambda r: all_lines.get(r["line_id"], {})
                               .get("line_no", 0)):
                line = all_lines.get(item["line_id"])
                if line is None:
                    raise TransferError(f"line {item['line_id']} is not on this transfer",
                                        code="line_not_found")
                if line["received_units"] is not None:
                    raise TransferError(f"line {line['line_no']} was already received. "
                                        "Nothing was changed.", code="line_already_received",
                                        detail={"line_no": line["line_no"]})
                qty = int(line["quantity_units"])
                got = item["received_units"]
                if got > qty:
                    raise TransferError(
                        f"line {line['line_no']}: {got} units cannot arrive when {qty} were "
                        "sent. Count again.", code="over_receipt",
                        detail={"line_no": line["line_no"]})
                reason = item["reason"]
                if got < qty and not reason:
                    raise TransferError(
                        f"line {line['line_no']}: {qty - got} of {qty} units did not arrive. "
                        "Say what happened to them.", code="discrepancy_reason_required",
                        detail={"line_no": line["line_no"]})
                to_lot_id = None
                movement_id = None
                landing, held_why = None, None
                if got > 0:
                    _check_landing(_find_drug(conn, line["helix_medication_id"]),
                                   line["helix_medication_id"], to_store)
                    source = conn.execute("SELECT * FROM helix_inventory_lots WHERE id = ?",
                                          (line["from_lot_id"],)).fetchone()
                    # Read again now: a lot recalled, found NSQ or quarantined
                    # while its units were on their way never lands available.
                    landing, held_why = _landing_status(
                        source, line, held_batches(conn, line["helix_medication_id"]))
                    try:
                        landed = stock.open_or_merge_lot(
                            conn, medication_id=line["helix_medication_id"],
                            lot_number=line["batch_no"] or (source["lot_number"] if source
                                                            else None),
                            expiration_date=line["expiry_date"], units=got, actor_id=actor_id,
                            store_id=to_store["id"], status=landing,
                            movement_type="transfer_in",
                            units_per_pack=source["units_per_pack"] if source else None,
                            mrp_pack_paise=source["mrp_pack_paise"] if source else None,
                            unit_cost_paise=source["unit_cost_paise"] if source else None,
                            mfg_date=source["mfg_date"] if source else None,
                            vendor_id=source["vendor_id"] if source else None,
                            receipt_line_ref=source["receipt_line_ref"] if source else None,
                            received_at=None,
                            reference_type=REFERENCE_TYPE, reference_id=str(tid),
                            idempotency_key=_in_key(tid, line["id"]),
                            remarks=f"From pharmacy transfer {row['transfer_number'] or tid}")
                    except stock.StockError as exc:
                        code = getattr(exc, "code", None) or "stock_error"
                        message = f"line {line['line_no']}: {exc}"
                        if held_why:
                            message += (f" These units land {_lot_status_words(landing)} "
                                        f"because {held_why}, so they stay on their way until "
                                        "that lot is held as well.")
                        raise TransferError(message, code=code,
                                            detail={"line_no": line["line_no"],
                                                    "landing_status": landing}) from exc
                    to_lot_id = landed["lot"]["id"]
                    movement_id = landed["movement"]["id"] if landed["movement"] else None
                cur = conn.execute(
                    "UPDATE pharmacy_transfer_lines SET received_units = ?, to_lot_id = ?, "
                    "discrepancy_reason = ? WHERE id = ? AND received_units IS NULL",
                    (got, to_lot_id, reason, line["id"]))
                if cur.rowcount != 1:
                    raise TransferError("this transfer changed while you were working on it. "
                                        "Nothing was changed.", code="line_already_received")
                received.append({"line_no": line["line_no"],
                                 "drug_id": line["helix_medication_id"],
                                 "batch_no": line["batch_no"], "sent_units": qty,
                                 "received_units": got, "shortfall_units": qty - got,
                                 "reason": reason, "to_lot_id": to_lot_id,
                                 "movement_id": movement_id, "landed_status": landing,
                                 "held_reason": held_why})
        pending = conn.execute(
            "SELECT COUNT(*) FROM pharmacy_transfer_lines WHERE pharmacy_transfer_id = ? "
            "AND received_units IS NULL", (tid,)).fetchone()[0]
        new_status = "received" if int(pending) == 0 else "partially_received"
        conn.execute(
            "UPDATE pharmacy_transfers SET status = ?, received_by = ?, "
            "received_at = CURRENT_TIMESTAMP WHERE id = ?", (new_status, actor_id, tid))
        shortfall = sum(r["shortfall_units"] for r in received)
        held_units = sum(r["received_units"] for r in received
                         if r["landed_status"] not in (None, "available"))
        _audit(conn, actor, "pharmacy_transfer_received",
               summary=(f"Pharmacy transfer {tid} received into {_store_name(to_store)}"
                        + (f", {shortfall} units short" if shortfall else "")
                        + (f", {held_units} units held off the shelf" if held_units else "")),
               transfer_id=tid,
               metadata={"transfer_id": str(tid), "to_store_code": to_store["code"],
                         "status": new_status, "controlled": now_controlled,
                         "witness_id": witness, "dispatched_by": str(row["dispatched_by"]),
                         "lines": received, "shortfall_units": shortfall,
                         "held_units": held_units},
               audit_ctx=audit_ctx)
        doc = _decorate(conn, _load(conn, tid))
    return {"transfer": doc, "replayed": False}


def cancel_transfer(transfer_id: Any, *, reason: Any, actor: Mapping,
                    audit_ctx: Optional[Mapping] = None) -> dict:
    """Give up a draft, with a reason. Nothing has moved, so nothing moves
    back. A transfer already on its way is received, never cancelled."""
    actor_id = _actor_id(actor)
    why = _text(reason, "a reason for cancelling", required=True)
    with db_session(immediate=True) as conn:
        row = _load(conn, transfer_id)
        tid = int(row["id"])
        if row["status"] != "draft":
            raise TransferError(
                "only a draft is cancelled. Units already on their way are received, short "
                "if some did not arrive.", code="illegal_transition")
        drug_ids = [r[0] for r in conn.execute(
            "SELECT helix_medication_id FROM pharmacy_transfer_lines "
            "WHERE pharmacy_transfer_id = ?", (tid,)).fetchall()]
        _require_ndps(actor, _controlled(conn, row, drug_ids))
        cur = conn.execute(
            "UPDATE pharmacy_transfers SET status = 'cancelled', cancelled_by = ?, "
            "cancelled_at = CURRENT_TIMESTAMP, cancel_reason = ? WHERE id = ? AND status = 'draft'",
            (actor_id, why, tid))
        if cur.rowcount != 1:
            raise TransferError("this transfer changed while you were working on it. Nothing "
                                "was changed.", code="illegal_transition")
        _audit(conn, actor, "pharmacy_transfer_cancelled",
               summary=f"Pharmacy transfer {tid} cancelled before it was sent",
               transfer_id=tid,
               metadata={"transfer_id": str(tid), "reason": why}, audit_ctx=audit_ctx)
        doc = _decorate(conn, _load(conn, tid))
    return {"transfer": doc, "replayed": False}
