"""Orb, crash carts: the cart's list, its seal-and-check record, its top-up
after use, expiry inside the cart, and which carts a batch went to (grn-23).

Design: docs/his_design/grn.json, increment grn-23, riding the ward lane of
grn-21 (services/pharmacy_transfer_service, WARD STOCK). Reached only through
routes/pharmacy_store/ward.py, behind ORB_PHARMACY_STORE and
ORB_PHARMACY_WARD_STOCK (ruling GRN-RS5, held for Samuel; both off by
default), and every write here checks the ward switch again, so nothing here
runs on a site that has not turned both on. No patient data is read, written
or shown: a crash cart is ward stock, and who received a dose from it is the
eMAR's record, not this one.

WHAT A CRASH CART IS HERE. A ward store of kind 'crash_cart' (Masters creates
it, with the department it serves). Everything the ward lane says of ward
imprest stock holds for it:
  * its LIST is its active par rows above 0 (GRN-RS5 (b), the per-store list).
    pharmacy_master_service refuses a crash-cart par level for any drug no
    ward store may hold (unclassified, narcotic or psychotropic, a barred
    high-alert drug or concentrated electrolyte, inactive, not confirmed), and
    the ward lane refuses to ask for, approve, plan or send a drug that is not
    on the list. There is no 'unless named' exception;
  * it is FILLED only by an issue indent from the dispensing pharmacy, decided
    by someone in the pharmacy who did not ask, sent FEFO, and received on the
    ward by a ward_stock.receive holder of the cart's department who did not
    send it;
  * its lots are NEVER dispensable by Helix (pharmacy_stock_service.
    shelf_lot_sql takes only the dispensing store and the NDPS vault), and a
    dose given from the cart still needs an active eMAR order: nothing here
    touches the eMAR.

THE SEAL-AND-CHECK RECORD. One pharmacy_stock_counts row on the cart's store,
with its lines in pharmacy_stock_count_lines (no migration: a cart check is a
count of the cart). The row's remarks hold the seal facts as a JSON object
whose key 'cart_check' names the kind, so a count row on a crash cart without
that key is not a cart check and is never read here. Two kinds:
  seal  the routine look: the number on the cart's seal and whether it was
        intact. Nothing is counted and no seal is applied. A broken or missing
        seal, or a number that is not the last one applied, makes a full
        check due.
  full  every lot in the cart counted against the books (all of them: held
        and expired lots too, because they are still in the cart), expiry
        read, and a new numbered seal applied. The check names exactly the
        lots the cart holds; if the cart changed while the sheet was open
        (a delivery arrived) it is refused as stale and counted again.
The statuses are migration 54's:
  approved   recorded and closed. A seal check, and a full check whose count
             matched the books, close at once (approved_by stays empty:
             there was nothing to confirm).
  submitted  a full check whose count differs from the books (units short or
             over), with the checker's note of why. Waiting for the pharmacy.
             One per cart at a time.
  approved   (from submitted) confirmed by someone holding pharmacy_store.write
             or .approve who did not count (403 same_person; the table's CHECK
             says the same). Units found SHORT then leave the cart's lots by
             'count_variance' movements through pharmacy_stock_service.
             post_movement (reference 'pharmacy_stock_count', key
             'pharmacy_stock_count:<check>:line:<line>'), so the ledger and the
             cart agree again.
  rejected   refused by the pharmacy with a reason (count again). Nothing
             moves, and a full check is due.
Units found OVER the books are recorded on the check and NEVER added to the
books: stock with no record of where it came from does not become stock; the
pharmacy takes it out of the cart. (Strictest reading, listed for Samuel.)

TOP-UP AFTER USE. The ward lane's top-up (par less what is usable in the cart,
less what is on its way, less what is already asked for) is the suggestion.
For a crash cart, units a full check found short that the pharmacy has not yet
confirmed are not counted as usable (pending_shortfall_by_drug), so the cart
can be topped up straight after the check, before the confirmation. Expired
and held units are never usable, so they are replaced too. request_top_up
raises an ordinary ward indent (urgent unless the caller says otherwise) for
exactly the listed drugs the suggestion names, through
pharmacy_transfer_service.raise_ward_indent, which checks the list again.

EXPIRY. The cart's overview names every lot past its expiry or undated (still
in the cart, never usable: replace it), every lot expiring within
ORB_PHARMACY_NEAR_EXPIRY_DAYS, and every held lot. The pharmacy's own
near-expiry report (pharmacy_stock_reports) already lists cart lots too, with
the store named.

DUE CHECKS (product defaults, listed for Samuel): a seal check is due when no
check of any kind is recorded in SEAL_CHECK_DUE_HOURS; a full check is due when
none is on record, the last is older than FULL_CHECK_DUE_DAYS, a later check
found the seal broken, missing or not the one applied, the pharmacy asked for a
recount, or stock was put into the cart at or after the last full check (the
cart was opened to restock it).

RECALL. cart_batch_issues(drug, batch) lists every crash cart a batch was sent
to, with the units sent, received, still in the cart, and the units that left
it with no patient recorded ('issued to <cart> stock, patients not recorded').
The full recall trace (grn-10, not built yet) should include this list.

AUDIT. Every write is one 'security' row (fail-closed, db=conn) in the same
transaction as the change: pharmacy_cart_check_recorded,
pharmacy_cart_check_confirmed, pharmacy_cart_check_refused (target_type
pharmacy_stock_count). The top-up indent writes the ward lane's own
pharmacy_ward_indent_raised row.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from database import db_session
from services import pharmacy_stock_service as stock
from services import pharmacy_transfer_service as transfers
from services.clinical_time import parse_to_utc

logger = logging.getLogger("orb.pharmacy_crash_carts")

# ═════════════════════════════════════════════════════════════════════════════
#  Words and limits
# ═════════════════════════════════════════════════════════════════════════════

CART_KIND = transfers.CRASH_CART_KIND

#: The two kinds of cart check, as the remarks JSON spells them.
CHECK_KINDS: tuple[str, ...] = ("seal", "full")
CHECK_KIND_LABELS = {"seal": "Seal check", "full": "Full check"}

#: The key in pharmacy_stock_counts.remarks that marks a row as a cart check.
CHECK_MARK = "cart_check"

#: The movement and reference a confirmed shortfall posts (the ledger reads
#: them as "Stock count difference" and "Stock count").
VARIANCE_MOVEMENT = "count_variance"
CHECK_REFERENCE = "pharmacy_stock_count"

#: Statuses a cart check uses (migration 54's pharmacy_stock_counts CHECK).
PENDING_STATUS = "submitted"
CLOSED_STATUS = "approved"
REFUSED_STATUS = "rejected"
CHECK_STATUS_LABELS = {
    PENDING_STATUS: "Waiting for the pharmacy",
    CLOSED_STATUS: "Recorded",
    REFUSED_STATUS: "Count again",
}
CONFIRMED_LABEL = "Confirmed by the pharmacy"

#: Due checks (product defaults; see the module note).
SEAL_CHECK_DUE_HOURS = 24
FULL_CHECK_DUE_DAYS = 30

#: A top-up after use asks with this priority unless the caller says otherwise.
TOP_UP_PRIORITY = "urgent"

MAX_SEAL = 40
MAX_CHECK_LINES = 200
MAX_EXTRA_LINES = 50
MAX_HISTORY = 200


class CrashCartError(transfers.TransferError):
    """A crash-cart request the caller must correct, or one the rules refuse.
    A TransferError, so the ward routes map its code to HTTP the same way."""

    code = "crash_cart_error"


# ═════════════════════════════════════════════════════════════════════════════
#  Small helpers
# ═════════════════════════════════════════════════════════════════════════════

def _seal_text(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CrashCartError(f"{name} must be text", code="bad_value")
    text = " ".join(value.split())
    if len(text) > MAX_SEAL:
        raise CrashCartError(f"{name} is at most {MAX_SEAL} characters", code="bad_value")
    return text or None


def _seal_key(value: Any) -> str:
    """A seal number compared without case or spaces."""
    return "".join(str(value or "").upper().split())


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise CrashCartError(f"{name} is yes or no", code="bad_value")
    return value


def _facts(row: Mapping) -> Optional[dict]:
    """The seal facts of a cart check row, or None when the row is not one."""
    try:
        data = json.loads(row["remarks"] or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get(CHECK_MARK) not in CHECK_KINDS:
        return None
    return data


def _instant(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parse_to_utc(str(value))
    except ValueError:
        return None


def _audit(conn, actor: Mapping, action: str, *, summary: str, check_id: int,
           metadata: dict, audit_ctx: Optional[Mapping]) -> None:
    """One 'security' row in the caller's transaction (fail-closed: a failed
    write raises AuditWriteError and the whole action rolls back)."""
    from services import audit_service

    ctx = audit_ctx or {}
    audit_service.log_audit_event(
        correlation_id=str(uuid.uuid4()), event_category="security", action_type=action,
        outcome="success", db=conn, summary_message=summary,
        actor_user_id=str(actor.get("id")) if actor.get("id") is not None else None,
        actor_role=actor.get("role"), target_type=CHECK_REFERENCE, target_id=str(check_id),
        request_path=ctx.get("request_path"), http_method=ctx.get("http_method"),
        ip_address=ctx.get("ip_address"), metadata=metadata)


def _may_check(actor: Mapping) -> bool:
    from permissions import PHARMACY_STORE_WRITE, WARD_STOCK_RECEIVE

    return transfers._holds(actor, WARD_STOCK_RECEIVE) or transfers._holds(
        actor, PHARMACY_STORE_WRITE)


def _may_confirm(actor: Mapping) -> bool:
    from permissions import PHARMACY_STORE_APPROVE, PHARMACY_STORE_WRITE

    return transfers._holds(actor, PHARMACY_STORE_WRITE) or transfers._holds(
        actor, PHARMACY_STORE_APPROVE)


def _may_raise(actor: Mapping) -> bool:
    from permissions import PHARMACY_STORE_WRITE, WARD_INDENT_RAISE

    return transfers._holds(actor, WARD_INDENT_RAISE) or transfers._holds(
        actor, PHARMACY_STORE_WRITE)


def _cart_by_code(conn, actor: Mapping, code: Any):
    """A crash cart this caller sees (the pharmacy desk every cart, a ward
    account its own department's); anything else is not found."""
    try:
        store = transfers._ward_store_by_code(conn, actor, code)
    except transfers.TransferError as exc:
        raise CrashCartError(f"no crash cart {code!r}", code=exc.code) from None
    if store["pharmacy_kind"] != CART_KIND:
        raise CrashCartError(f"no crash cart {code!r}", code="not_found")
    return store


def _visible_carts(conn, actor: Mapping) -> list[dict]:
    return [s for s in transfers._visible_ward_stores(conn, actor)
            if s["pharmacy_kind"] == CART_KIND]


# ═════════════════════════════════════════════════════════════════════════════
#  Reading checks
# ═════════════════════════════════════════════════════════════════════════════

def _check_rows(conn, store_id: int, limit: int = MAX_HISTORY) -> list[tuple[dict, dict]]:
    """This cart's checks, newest first, as (row, facts)."""
    out = []
    for row in conn.execute("SELECT * FROM pharmacy_stock_counts WHERE store_id = ? "
                            "ORDER BY id DESC LIMIT ?", (store_id, limit)).fetchall():
        facts = _facts(row)
        if facts is not None:
            out.append((dict(row), facts))
    return out


def _lines(conn, check_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT cl.*, m.name AS drug_name, m.form, m.strength, m.dispense_unit "
        "FROM pharmacy_stock_count_lines cl "
        "LEFT JOIN helix_medication_master m ON m.id = cl.helix_medication_id "
        "WHERE cl.pharmacy_stock_count_id = ? ORDER BY cl.line_no", (check_id,)).fetchall()]


def _outcome(row: Mapping, facts: Mapping, short: int, over: int) -> str:
    """The check's finding in one sentence a ward reads."""
    parts = []
    if facts.get("seal_found") is None:
        parts.append("No seal on the cart")
    elif not facts.get("seal_intact"):
        parts.append("Seal found broken")
    elif facts.get("seal_expected") and not facts.get("seal_matches"):
        parts.append("Seal number is not the last one applied "
                     f"({facts['seal_expected']})")
    elif not facts.get("seal_expected"):
        parts.append("Seal intact; no earlier seal on record to compare with")
    else:
        parts.append("Seal intact and the number matches")
    if facts.get(CHECK_MARK) == "full":
        if short or over:
            counted = []
            if short:
                counted.append(f"{short} unit{'' if short == 1 else 's'} short")
            if over:
                counted.append(f"{over} unit{'' if over == 1 else 's'} over")
            parts.append("count " + " and ".join(counted))
        else:
            parts.append("count matches the books")
        if facts.get("seal_applied"):
            parts.append(f"sealed with {facts['seal_applied']}")
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def _decorate_check(conn, row: Mapping, facts: Mapping, *, with_lines: bool = True,
                    names: Optional[Mapping[str, str]] = None) -> dict:
    cid = int(row["id"])
    lines = _lines(conn, cid)
    short = sum(max(int(ln["system_units"]) - int(ln["counted_units"] or 0), 0)
                for ln in lines if ln["counted_units"] is not None)
    over = sum(max(int(ln["counted_units"] or 0) - int(ln["system_units"]), 0) for ln in lines)
    if names is None:
        names = transfers._user_names(conn, {row["counted_by"], row["approved_by"]})
    status = row["status"]
    label = CHECK_STATUS_LABELS.get(status, status)
    if status == CLOSED_STATUS and row["approved_by"]:
        label = CONFIRMED_LABEL
    store = conn.execute("SELECT code, name, department FROM stores WHERE id = ?",
                         (row["store_id"],)).fetchone()
    doc = {
        "id": cid,
        "store_id": row["store_id"],
        "store_code": store["code"] if store else None,
        "store_name": (store["name"] or store["code"]) if store else None,
        "department": store["department"] if store else None,
        "kind": facts[CHECK_MARK],
        "kind_label": CHECK_KIND_LABELS[facts[CHECK_MARK]],
        "status": status,
        "status_label": label,
        "seal_found": facts.get("seal_found"),
        "seal_intact": facts.get("seal_intact"),
        "seal_expected": facts.get("seal_expected"),
        "seal_matches": facts.get("seal_matches"),
        "seal_applied": facts.get("seal_applied"),
        "note": facts.get("note"),
        "counted_by": row["counted_by"],
        "counted_by_name": names.get(str(row["counted_by"])) if row["counted_by"] else None,
        "checked_at": row["created_at"],
        "decided_by": row["approved_by"],
        "decided_by_name": names.get(str(row["approved_by"])) if row["approved_by"] else None,
        "decided_at": row["approved_at"] if row["approved_by"] else None,
        "decision_reason": row["decision_reason"],
        "short_units": short,
        "over_units": over,
        "outcome": _outcome(row, facts, short, over),
    }
    if with_lines:
        checked_on = str(row["created_at"] or "")[:10]
        for line in lines:
            counted = line["counted_units"]
            system = int(line["system_units"])
            expiry = str(line["expiry_date"] or "")[:10] or None
            line["drug_id"] = line["helix_medication_id"]
            line["drug_name"] = line["drug_name"] or line["helix_medication_id"]
            line["expiry_date"] = expiry
            line["short_units"] = max(system - int(counted or 0), 0) if counted is not None else 0
            line["over_units"] = max(int(counted or 0) - system, 0)
            line["found_extra"] = line["lot_id"] is None
            line["expired_at_check"] = bool(checked_on) and (not expiry or expiry < checked_on)
        doc["lines"] = lines
    return doc


def _check_row(conn, check_id: Any, actor: Mapping) -> tuple[dict, dict]:
    """A cart check on a crash cart this caller sees; anything else is not
    found, worded as one that does not exist."""
    try:
        cid = int(check_id)
    except (TypeError, ValueError):
        raise CrashCartError(f"no crash-cart check {check_id}", code="not_found") from None
    row = conn.execute(
        "SELECT c.* FROM pharmacy_stock_counts c JOIN stores s ON s.id = c.store_id "
        "WHERE c.id = ? AND s.pharmacy_kind = ?", (cid, CART_KIND)).fetchone()
    facts = _facts(row) if row is not None else None
    if row is None or facts is None or not transfers._ward_store_visible(
            conn, actor, int(row["store_id"])):
        raise CrashCartError(f"no crash-cart check {check_id}", code="not_found")
    return dict(row), facts


def get_check(check_id: Any, *, actor: Mapping) -> dict:
    with db_session() as conn:
        row, facts = _check_row(conn, check_id, actor)
        return _decorate_check(conn, row, facts)


def list_checks(*, actor: Mapping, store_code: Optional[str] = None,
                status: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Cart checks on the crash carts this caller sees, newest first.
    `status` is one of submitted, approved or rejected."""
    if status is not None and status not in CHECK_STATUS_LABELS:
        raise CrashCartError(f"unknown check status {status!r}", code="bad_value")
    limit = max(1, min(int(limit), MAX_HISTORY))
    with db_session() as conn:
        carts = _visible_carts(conn, actor)
        if store_code:
            wanted = str(store_code).strip().upper()
            carts = [c for c in carts if str(c["code"]).upper() == wanted]
            if not carts:
                raise CrashCartError(f"no crash cart {store_code!r}", code="not_found")
        if not carts:
            return []
        ids = [int(c["id"]) for c in carts]
        sql = (f"SELECT * FROM pharmacy_stock_counts WHERE store_id IN "
               f"({','.join('?' for _ in ids)})")
        args: list[Any] = list(ids)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(MAX_HISTORY * 2)
        out = []
        for row in conn.execute(sql, args).fetchall():
            facts = _facts(row)
            if facts is None:
                continue
            out.append(_decorate_check(conn, dict(row), facts, with_lines=False))
            if len(out) >= limit:
                break
        return out


# ═════════════════════════════════════════════════════════════════════════════
#  The cart against its list: expiry, seal, due checks, top-up
# ═════════════════════════════════════════════════════════════════════════════

def pending_shortfall_by_drug(conn, store_id: int, today: str) -> dict[str, int]:
    """Units a cart check found short that the pharmacy has not yet confirmed
    off the books, per drug, counted only on lots the ward lane counts as on
    hand (available, in date, still in this cart). Read-only; the ward lane's
    top-up subtracts them from what is usable."""
    out: dict[str, int] = {}
    for r in conn.execute(
            "SELECT c.remarks, cl.helix_medication_id AS drug_id, cl.system_units, "
            "cl.counted_units, l.quantity "
            "FROM pharmacy_stock_count_lines cl "
            "JOIN pharmacy_stock_counts c ON c.id = cl.pharmacy_stock_count_id "
            "JOIN helix_inventory_lots l ON l.id = cl.lot_id "
            "WHERE c.store_id = ? AND c.status = ? AND cl.counted_units IS NOT NULL "
            "AND cl.counted_units < cl.system_units AND l.store_id = c.store_id "
            "AND l.status = 'available' AND l.quantity > 0 "
            "AND date(l.expiration_date) IS NOT NULL AND date(l.expiration_date) >= ?",
            (store_id, PENDING_STATUS, today)).fetchall():
        if _facts(r) is None:
            continue
        short = min(int(r["system_units"]) - int(r["counted_units"]), int(r["quantity"] or 0))
        if short > 0:
            out[r["drug_id"]] = out.get(r["drug_id"], 0) + short
    return out


def _summary(conn, row: Mapping, facts: Mapping, names: Mapping[str, str]) -> dict:
    return _decorate_check(conn, row, facts, with_lines=False, names=names)


def _cart(conn, store: Mapping, today: str, now: datetime, near_days: int) -> dict:
    sid = int(store["id"])
    stock_doc = transfers._store_stock(conn, store, today)
    cutoff = (date.fromisoformat(today) + timedelta(days=near_days)).isoformat()
    expired, expiring, held = [], [], []
    for drug in stock_doc["drugs"]:
        for lot in drug["lots"]:
            entry = {"drug_id": drug["drug_id"], "drug_name": drug["drug_name"],
                     "lot_id": lot["lot_id"], "batch_no": lot["batch_no"],
                     "expiry_date": lot["expiry_date"], "units": lot["units"],
                     "status": lot["status"], "status_label": lot["status_label"]}
            if lot["status"] != "available":
                held.append(entry)
            elif not lot["expiry_date"] or lot["expiry_date"] < today:
                expired.append(entry)
            elif lot["expiry_date"] <= cutoff:
                entry["days_left"] = (date.fromisoformat(lot["expiry_date"])
                                      - date.fromisoformat(today)).days
                expiring.append(entry)
    expiring.sort(key=lambda e: (e["expiry_date"], str(e["drug_name"]).lower()))

    checks = _check_rows(conn, sid)
    names = transfers._user_names(conn, {r["counted_by"] for r, _ in checks}
                                  | {r["approved_by"] for r, _ in checks})
    last = checks[0] if checks else None
    full_done = [(r, f) for r, f in checks
                 if f[CHECK_MARK] == "full" and r["status"] in (CLOSED_STATUS, PENDING_STATUS)]
    last_full = full_done[0] if full_done else None
    pending = next(((r, f) for r, f in checks if r["status"] == PENDING_STATUS), None)
    sealed_by = next(((r, f) for r, f in checks if f.get("seal_applied")), None)

    reasons: list[str] = []
    if last is None:
        seal_due = True
    else:
        when = _instant(last[0]["created_at"])
        seal_due = when is None or now - when >= timedelta(hours=SEAL_CHECK_DUE_HOURS)
    if last_full is None:
        reasons.append("No full check is on record.")
    else:
        full_id = int(last_full[0]["id"])
        full_when = _instant(last_full[0]["created_at"])
        if full_when is None or now - full_when >= timedelta(days=FULL_CHECK_DUE_DAYS):
            reasons.append(f"The last full check is more than {FULL_CHECK_DUE_DAYS} days old.")
        later = [(r, f) for r, f in checks if int(r["id"]) > full_id]
        if any(f[CHECK_MARK] == "seal" and (not f.get("seal_intact") or f.get("seal_found") is None
                                            or (f.get("seal_expected")
                                                and not f.get("seal_matches")))
               for _, f in later):
            reasons.append("A later check found the seal broken, missing or not the one applied.")
        if any(r["status"] == REFUSED_STATUS for r, _ in later):
            reasons.append("The pharmacy asked for the cart to be counted again.")
        received = conn.execute(
            "SELECT MAX(received_at) FROM pharmacy_transfers WHERE to_store_id = ? "
            "AND received_at IS NOT NULL", (sid,)).fetchone()[0]
        put_in = _instant(received)
        if put_in is not None and full_when is not None and put_in >= full_when:
            reasons.append("Stock was put into the cart at or after the last full check.")
    seal_state = "none"
    if sealed_by is not None:
        seal_state = "sealed"
        for r, f in checks:
            if int(r["id"]) <= int(sealed_by[0]["id"]):
                break
            if f[CHECK_MARK] == "seal" and (not f.get("seal_intact")
                                            or f.get("seal_found") is None
                                            or not f.get("seal_matches")):
                seal_state = "broken"
                break
    top_up = [{"drug_id": d["drug_id"], "drug_name": d["drug_name"],
               "units": d["top_up_units"], "dispense_unit": d["dispense_unit"]}
              for d in stock_doc["drugs"] if d["listed"] and d["top_up_units"] > 0
              and d["problem"] is None]
    return {
        "store": stock_doc["store"],
        "drugs": stock_doc["drugs"],
        "seal": sealed_by[1].get("seal_applied") if sealed_by else None,
        "seal_state": seal_state,
        "last_check": _summary(conn, *last, names) if last else None,
        "last_full_check": _summary(conn, *last_full, names) if last_full else None,
        "pending_check": _summary(conn, *pending, names) if pending else None,
        "seal_check_due": bool(seal_due),
        "full_check_due": bool(reasons),
        "due_reasons": reasons,
        "expired": expired,
        "expiring": expiring,
        "held": held,
        "top_up": top_up,
        "below_par": sum(1 for d in stock_doc["drugs"] if d["listed"] and d["below_par"]),
        "not_allowed": [{"drug_id": d["drug_id"], "drug_name": d["drug_name"],
                         "message": d["problem"]["message"]}
                        for d in stock_doc["drugs"] if d["listed"] and d["problem"]],
    }


def crash_carts(*, actor: Mapping, store_code: Optional[str] = None,
                today: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """Every crash cart this caller sees, with its list against what it
    holds, its seal, its last checks and any waiting for the pharmacy, which
    checks are due and why, expired, expiring and held lots, and the top-up a
    request would ask for. Read-only, no patient data."""
    import config

    today = today or stock.site_today()
    now = now or datetime.now(timezone.utc)
    near_days = int(config.PHARMACY_NEAR_EXPIRY_DAYS)
    with db_session() as conn:
        carts = _visible_carts(conn, actor)
        if store_code:
            wanted = str(store_code).strip().upper()
            carts = [c for c in carts if str(c["code"]).upper() == wanted]
            if not carts:
                raise CrashCartError(f"no crash cart {store_code!r}", code="not_found")
        out = [_cart(conn, c, today, now, near_days) for c in carts]
    return {"today": today, "near_expiry_days": near_days,
            "seal_check_due_hours": SEAL_CHECK_DUE_HOURS,
            "full_check_due_days": FULL_CHECK_DUE_DAYS,
            "carts": out,
            "may_check": _may_check(actor),
            "may_confirm": _may_confirm(actor),
            "may_raise": _may_raise(actor),
            "priorities": list(transfers.PRIORITIES)}


# ═════════════════════════════════════════════════════════════════════════════
#  Recording a check
# ═════════════════════════════════════════════════════════════════════════════

def _clean_counts(raw: Any) -> dict[str, int]:
    """{lot_id: counted units} from [{lot_id, counted_units}]."""
    if raw is None:
        return {}
    if not isinstance(raw, (list, tuple)):
        raise CrashCartError("lines is a list of counted batches", code="bad_value")
    if len(raw) > MAX_CHECK_LINES:
        raise CrashCartError(f"a check counts at most {MAX_CHECK_LINES} batches",
                             code="too_many_lines")
    out: dict[str, int] = {}
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise CrashCartError("each counted batch is an object", code="bad_line")
        lot_id = item.get("lot_id")
        if not isinstance(lot_id, str) or not lot_id.strip():
            raise CrashCartError(f"line {n} names no batch", code="bad_line")
        if lot_id.strip() in out:
            raise CrashCartError("a batch is counted once", code="duplicate_line")
        out[lot_id.strip()] = transfers._whole(item.get("counted_units"),
                                               f"line {n} counted units", minimum=0)
    return out


def _clean_extras(raw: Any) -> list[dict]:
    """Units found in the cart that the books do not show there:
    [{drug_id, units, batch_no?, expiry_date?}]."""
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise CrashCartError("extra is a list of items found", code="bad_value")
    if len(raw) > MAX_EXTRA_LINES:
        raise CrashCartError(f"a check records at most {MAX_EXTRA_LINES} extra items",
                             code="too_many_lines")
    out = []
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise CrashCartError("each extra item is an object", code="bad_line")
        drug_id = item.get("drug_id")
        if not isinstance(drug_id, str) or not drug_id.strip():
            raise CrashCartError(f"extra item {n} names no drug", code="bad_line")
        batch = transfers._text(item.get("batch_no"), f"extra item {n} batch")
        try:
            expiry = stock._date_text(item.get("expiry_date"), f"extra item {n} expiry")
        except stock.StockError as exc:
            raise CrashCartError(str(exc), code="bad_value") from None
        out.append({"drug_id": drug_id.strip(), "batch_no": batch, "expiry_date": expiry,
                    "units": transfers._whole(item.get("units"), f"extra item {n} units",
                                              minimum=1)})
    return out


def _replay(conn, key: str, store_id: int, actor_id: str):
    prior = conn.execute("SELECT * FROM pharmacy_stock_counts WHERE idempotency_key = ?",
                         (key,)).fetchone()
    if prior is None:
        return None
    facts = _facts(prior)
    if facts is None or int(prior["store_id"]) != store_id or str(prior["counted_by"]) != actor_id:
        raise CrashCartError("this request key was already used for a different record. "
                             "Nothing was changed.", code="idempotency_conflict")
    return dict(prior), facts


def record_check(store_code: Any, *, actor: Mapping, kind: Any, seal_found: Any = None,
                 seal_intact: Any = None, seal_applied: Any = None, lines: Any = None,
                 extra: Any = None, note: Any = None, idempotency_key: Any = None,
                 audit_ctx: Optional[Mapping] = None) -> dict:
    """Record a seal check or a full check of a crash cart (a ward_stock.receive
    holder of the cart's department, or the pharmacy at the write tier).
    Returns {"check", "replayed"}. The rules are in the module note."""
    actor_id = transfers._actor_id(actor)
    transfers._require_ward_stock_on()
    if not _may_check(actor):
        raise CrashCartError("you cannot record a crash-cart check", code="permission_denied")
    if kind not in CHECK_KINDS:
        raise CrashCartError("a check is a seal check or a full check", code="bad_value")
    found = _seal_text(seal_found, "the seal number found")
    intact = _bool(seal_intact, "whether the seal was intact")
    if intact and found is None:
        raise CrashCartError("an intact seal has a number: enter it", code="missing_field")
    applied = _seal_text(seal_applied, "the new seal number")
    counts = _clean_counts(lines)
    extras = _clean_extras(extra)
    text = transfers._text(note, "the note")
    key = transfers._text(idempotency_key, "request key")
    if kind == "seal":
        if applied is not None:
            raise CrashCartError("a seal check applies no new seal. Record a full check to "
                                 "count the cart and seal it again.", code="bad_value")
        if counts or extras:
            raise CrashCartError("a seal check counts nothing. Record a full check to count "
                                 "the cart.", code="bad_value")
    elif applied is None:
        raise CrashCartError("a full check ends with the cart sealed: enter the new seal "
                             "number", code="missing_field")

    with db_session(immediate=True) as conn:
        cart = _cart_by_code(conn, actor, store_code)
        sid = int(cart["id"])
        if key:
            replay = _replay(conn, key, sid, actor_id)
            if replay is not None:
                return {"check": _decorate_check(conn, *replay), "replayed": True}
        if not cart["is_active"]:
            raise CrashCartError(f"{transfers._store_name(cart)} is closed. Nothing was changed.",
                                 code="store_inactive")
        history = _check_rows(conn, sid)
        sealed_by = next(((r, f) for r, f in history if f.get("seal_applied")), None)
        expected = sealed_by[1]["seal_applied"] if sealed_by else None
        matches = (bool(found) and bool(expected) and _seal_key(found) == _seal_key(expected)) \
            if expected else None
        line_rows: list[dict] = []
        short = over = 0
        if kind == "full":
            waiting = next((r for r, _ in history if r["status"] == PENDING_STATUS), None)
            if waiting is not None:
                raise CrashCartError(
                    "an earlier full check of this cart is still waiting for the pharmacy. It "
                    "is confirmed or refused before the cart is counted again. Nothing was "
                    "changed.", code="check_pending", detail={"check_id": waiting["id"]})
            lots = {r["id"]: r for r in conn.execute(
                "SELECT id, medication_id, lot_number, expiration_date, quantity, status "
                "FROM helix_inventory_lots WHERE store_id = ? AND quantity > 0 "
                "ORDER BY date(expiration_date), rowid", (sid,)).fetchall()}
            if set(counts) != set(lots):
                raise CrashCartError(
                    "what the cart holds changed since this check was opened, or a batch was "
                    "left out. Open the check again and count every batch. Nothing was changed.",
                    code="check_stale",
                    detail={"lot_ids": sorted(lots), "counted_lot_ids": sorted(counts)})
            for lot_id, lot in lots.items():
                system = int(lot["quantity"] or 0)
                counted = counts[lot_id]
                short += max(system - counted, 0)
                over += max(counted - system, 0)
                line_rows.append({"lot_id": lot_id, "drug_id": lot["medication_id"],
                                  "batch_no": lot["lot_number"],
                                  "expiry_date": str(lot["expiration_date"] or "")[:10] or None,
                                  "system_units": system, "counted_units": counted})
            for item in extras:
                drug = transfers._find_drug(conn, item["drug_id"])
                if drug is None:
                    raise CrashCartError(f"no drug {item['drug_id']!r} in the drug master",
                                         code="drug_not_found")
                over += item["units"]
                line_rows.append({"lot_id": None, "drug_id": drug["id"],
                                  "batch_no": item["batch_no"],
                                  "expiry_date": item["expiry_date"],
                                  "system_units": 0, "counted_units": item["units"]})
            if (short or over) and not text:
                raise CrashCartError("the count differs from the books: say what happened (used "
                                     "at an emergency, found in the cart ...)",
                                     code="reason_required")
        status = PENDING_STATUS if (short or over) else CLOSED_STATUS
        facts = {CHECK_MARK: kind, "seal_found": found, "seal_intact": intact,
                 "seal_expected": expected, "seal_matches": matches,
                 "seal_applied": applied if kind == "full" else None, "note": text}
        cid = conn.execute(
            "INSERT INTO pharmacy_stock_counts (store_id, is_blind, status, counted_by, "
            "submitted_at, approved_at, idempotency_key, remarks) "
            "VALUES (?, 0, ?, ?, CURRENT_TIMESTAMP, "
            f"{'CURRENT_TIMESTAMP' if status == CLOSED_STATUS else 'NULL'}, ?, ?)",
            (sid, status, actor_id, key, json.dumps(facts, sort_keys=True))).lastrowid
        for n, line in enumerate(line_rows, start=1):
            conn.execute(
                "INSERT INTO pharmacy_stock_count_lines (pharmacy_stock_count_id, line_no, "
                "lot_id, helix_medication_id, batch_no, expiry_date, system_units, "
                "counted_units, variance_units) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, n, line["lot_id"], line["drug_id"], line["batch_no"],
                 line["expiry_date"], line["system_units"], line["counted_units"],
                 line["counted_units"] - line["system_units"]))
        _audit(conn, actor, "pharmacy_cart_check_recorded",
               summary=f"{CHECK_KIND_LABELS[kind]} of {transfers._store_name(cart)} recorded",
               check_id=cid,
               metadata={"check_id": str(cid), "store_code": cart["code"], "kind": kind,
                         "status": status, "seal_found": found, "seal_intact": intact,
                         "seal_expected": expected, "seal_matches": matches,
                         "seal_applied": facts["seal_applied"], "short_units": short,
                         "over_units": over,
                         "lines": [{"lot_id": ln["lot_id"], "drug_id": ln["drug_id"],
                                    "system_units": ln["system_units"],
                                    "counted_units": ln["counted_units"]}
                                   for ln in line_rows][:MAX_CHECK_LINES]},
               audit_ctx=audit_ctx)
        row = dict(conn.execute("SELECT * FROM pharmacy_stock_counts WHERE id = ?",
                                (cid,)).fetchone())
        doc = _decorate_check(conn, row, facts)
    return {"check": doc, "replayed": False}


# ═════════════════════════════════════════════════════════════════════════════
#  The pharmacy's decision on a check that found a difference
# ═════════════════════════════════════════════════════════════════════════════

def decide_check(check_id: Any, *, actor: Mapping, confirm: Any, reason: Any = None,
                 audit_ctx: Optional[Mapping] = None) -> dict:
    """Confirm or refuse a full check waiting for the pharmacy (write or
    approve tier), never by the person who counted (403 same_person).
    Confirming takes the units found short off the cart's lots; refusing,
    with a reason, moves nothing and asks for a recount. Returns {"check"}."""
    actor_id = transfers._actor_id(actor)
    transfers._require_ward_stock_on()
    if not _may_confirm(actor):
        raise CrashCartError("only the pharmacy confirms a crash-cart check",
                             code="permission_denied")
    agreed = _bool(confirm, "confirm")
    why = transfers._text(reason, "the reason", required=not agreed)
    with db_session(immediate=True) as conn:
        row, facts = _check_row(conn, check_id, actor)
        cid = int(row["id"])
        if row["status"] != PENDING_STATUS:
            raise CrashCartError("this check is not waiting for the pharmacy",
                                 code="illegal_transition")
        if str(row["counted_by"] or "") == actor_id:
            raise CrashCartError("you counted this cart, so someone else in the pharmacy "
                                 "confirms it. Nothing was changed.", code="same_person")
        store = transfers._store_by_id(conn, row["store_id"])
        movements: list[dict] = []
        if agreed:
            for line in _lines(conn, cid):
                if line["lot_id"] is None or line["counted_units"] is None:
                    continue
                gone = int(line["system_units"]) - int(line["counted_units"])
                if gone <= 0:
                    continue
                lot = conn.execute("SELECT store_id FROM helix_inventory_lots WHERE id = ?",
                                   (line["lot_id"],)).fetchone()
                if lot is None or lot["store_id"] != row["store_id"]:
                    raise CrashCartError(
                        f"batch {line['batch_no'] or line['lot_id']} is no longer in this cart. "
                        "Ask for the cart to be counted again. Nothing was changed.",
                        code="check_stale", detail={"line_no": line["line_no"]})
                try:
                    moved = stock.post_movement(
                        conn, lot_id=line["lot_id"], movement_type=VARIANCE_MOVEMENT,
                        quantity_change=-gone, actor_id=actor_id,
                        reference_type=CHECK_REFERENCE, reference_id=str(cid),
                        idempotency_key=f"{CHECK_REFERENCE}:{cid}:line:{line['id']}",
                        remarks=f"Found short at crash-cart check {cid}")
                except stock.InsufficientStock:
                    raise CrashCartError(
                        f"batch {line['batch_no'] or line['lot_id']} now holds fewer units than "
                        "the check found missing. Ask for the cart to be counted again. Nothing "
                        "was changed.", code="insufficient_stock",
                        detail={"line_no": line["line_no"]}) from None
                except stock.StockError as exc:
                    raise CrashCartError(str(exc), code=getattr(exc, "code", None)
                                         or "stock_error") from None
                conn.execute("UPDATE pharmacy_stock_count_lines SET adjustment_movement_ref = ? "
                             "WHERE id = ?", (moved["id"], line["id"]))
                movements.append({"line_no": line["line_no"], "lot_id": line["lot_id"],
                                  "drug_id": line["helix_medication_id"], "units": gone,
                                  "movement_id": moved["id"]})
        new_status = CLOSED_STATUS if agreed else REFUSED_STATUS
        cur = conn.execute(
            "UPDATE pharmacy_stock_counts SET status = ?, approved_by = ?, "
            "approved_at = CURRENT_TIMESTAMP, decision_reason = ? WHERE id = ? AND status = ?",
            (new_status, actor_id, why, cid, PENDING_STATUS))
        if cur.rowcount != 1:
            raise CrashCartError("this check is not waiting for the pharmacy",
                                 code="illegal_transition")
        name = transfers._store_name(store) if store else str(row["store_id"])
        _audit(conn, actor,
               "pharmacy_cart_check_confirmed" if agreed else "pharmacy_cart_check_refused",
               summary=(f"Crash-cart check {cid} of {name} "
                        + ("confirmed" if agreed else "refused: count again")),
               check_id=cid,
               metadata={"check_id": str(cid), "store_code": store["code"] if store else None,
                         "counted_by": str(row["counted_by"] or ""), "reason": why,
                         "units_off_the_books": sum(m["units"] for m in movements),
                         "movements": movements},
               audit_ctx=audit_ctx)
        doc = _decorate_check(conn, dict(conn.execute(
            "SELECT * FROM pharmacy_stock_counts WHERE id = ?", (cid,)).fetchone()), facts)
    return {"check": doc}


# ═════════════════════════════════════════════════════════════════════════════
#  Top-up after use
# ═════════════════════════════════════════════════════════════════════════════

def request_top_up(store_code: Any, *, actor: Mapping, priority: Any = None,
                   remarks: Any = None, audit_ctx: Optional[Mapping] = None) -> dict:
    """Ask the dispensing pharmacy for the cart's top-up: exactly the listed
    drugs its overview suggests, as one ward indent (urgent by default).
    A drug already asked for on an open indent is left out. Returns
    {"indent", "left_out"}."""
    transfers._actor_id(actor)
    transfers._require_ward_stock_on()
    if not _may_raise(actor):
        raise CrashCartError("you cannot ask the pharmacy for a crash-cart top-up",
                             code="permission_denied")
    prio = TOP_UP_PRIORITY if priority in (None, "") else priority
    today = stock.site_today()
    with db_session() as conn:
        cart = _cart_by_code(conn, actor, store_code)
        if not cart["is_active"]:
            raise CrashCartError(f"{transfers._store_name(cart)} is closed. Nothing was changed.",
                                 code="store_inactive")
        doc = transfers._store_stock(conn, cart, today)
        lines, left_out = [], []
        for drug in doc["drugs"]:
            if not (drug["listed"] and drug["problem"] is None and drug["top_up_units"] > 0):
                continue
            if transfers._open_indent_for(conn, int(cart["id"]), drug["drug_id"]) is not None:
                left_out.append({"drug_id": drug["drug_id"], "drug_name": drug["drug_name"],
                                 "why": "already asked for on an open request"})
                continue
            lines.append({"helix_medication_id": drug["drug_id"],
                          "quantity_units": drug["top_up_units"]})
    if not lines:
        raise CrashCartError("nothing on this cart's list needs topping up now",
                             code="nothing_to_top_up", detail={"left_out": left_out})
    out = transfers.raise_ward_indent(
        store_code=cart["code"], lines=lines, actor=actor, priority=prio,
        remarks=transfers._text(remarks, "remarks") or "Crash-cart top-up after use",
        audit_ctx=audit_ctx)
    return {"indent": out["indent"], "left_out": left_out}


# ═════════════════════════════════════════════════════════════════════════════
#  Which carts a batch went to (the cart half of a recall trace)
# ═════════════════════════════════════════════════════════════════════════════

def cart_batch_issues(conn, drug_id: str, batch_no: str) -> list[dict]:
    """Every crash cart this drug's batch (compared without case or spaces)
    was sent to: units sent, received, still on their way, still in the cart,
    and the units that left the cart with no patient recorded. Takes the
    caller's connection and never writes. No patient data."""
    key = stock._batch_key(batch_no)
    if not key:
        return []
    carts: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT t.id AS transfer_id, t.transfer_number, t.status, t.to_store_id, "
            "s.code, s.name, s.department, tl.batch_no, tl.expiry_date, tl.quantity_units, "
            "tl.received_units "
            "FROM pharmacy_transfer_lines tl "
            "JOIN pharmacy_transfers t ON t.id = tl.pharmacy_transfer_id "
            "JOIN stores s ON s.id = t.to_store_id "
            "WHERE s.pharmacy_kind = ? AND tl.helix_medication_id = ? "
            "AND t.status IN ('dispatched', 'partially_received', 'received') "
            "ORDER BY t.id", (CART_KIND, drug_id)).fetchall():
        if stock._batch_key(r["batch_no"]) != key:
            continue
        cart = carts.setdefault(int(r["to_store_id"]), {
            "store_id": int(r["to_store_id"]), "store_code": r["code"],
            "store_name": r["name"] or r["code"], "department": r["department"],
            "transfers": [], "sent_units": 0, "received_units": 0, "in_transit_units": 0})
        cart["sent_units"] += int(r["quantity_units"])
        if r["received_units"] is None:
            cart["in_transit_units"] += int(r["quantity_units"])
        else:
            cart["received_units"] += int(r["received_units"])
        if r["transfer_id"] not in cart["transfers"]:
            cart["transfers"].append(r["transfer_id"])
    for sid, cart in carts.items():
        held = 0
        for lot in conn.execute("SELECT lot_number, quantity FROM helix_inventory_lots "
                                "WHERE store_id = ? AND medication_id = ?",
                                (sid, drug_id)).fetchall():
            if stock._batch_key(lot["lot_number"]) == key:
                held += int(lot["quantity"] or 0)
        cart["in_cart_units"] = held
        cart["unattributed_units"] = max(cart["received_units"] - held, 0)
        cart["label"] = f"Issued to {cart['store_name']} stock, patients not recorded"
    return sorted(carts.values(), key=lambda c: (str(c["store_name"]).lower(), c["store_id"]))


def batch_in_carts(drug_id: Any, batch_no: Any) -> dict:
    """cart_batch_issues for the pharmacy desk's screen."""
    drug_text = transfers._text(drug_id, "the drug", required=True)
    batch_text = transfers._text(batch_no, "the batch", required=True)
    with db_session() as conn:
        drug = transfers._find_drug(conn, drug_text)
        if drug is None:
            raise CrashCartError(f"no drug {drug_text!r} in the drug master",
                                 code="drug_not_found")
        carts = cart_batch_issues(conn, drug["id"], batch_text)
    return {"drug_id": drug["id"], "drug_name": transfers._drug_name(drug),
            "batch_no": batch_text, "carts": carts,
            "totals": {k: sum(c[k] for c in carts)
                       for k in ("sent_units", "received_units", "in_transit_units",
                                 "in_cart_units", "unattributed_units")}}


__all__: Sequence[str] = (
    "CART_KIND", "CHECK_KINDS", "CHECK_KIND_LABELS", "CHECK_MARK", "CHECK_REFERENCE",
    "CHECK_STATUS_LABELS", "CrashCartError", "FULL_CHECK_DUE_DAYS", "SEAL_CHECK_DUE_HOURS",
    "TOP_UP_PRIORITY", "VARIANCE_MOVEMENT", "batch_in_carts", "cart_batch_issues",
    "crash_carts", "decide_check", "get_check", "list_checks", "pending_shortfall_by_drug",
    "record_check", "request_top_up",
)
