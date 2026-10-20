"""Orb — PROCUREMENT & NON-PHARMACY STORES.

Vendors, a non-pharmacy item master, the indent → purchase order → goods receipt
chain, stock on hand per store, reorder points, consumption by department and
expiry tracking for the items that carry it.

════════════════════════════════════════════════════════════════════════════════
 THE BOUNDARY WITH HELIX (PHARMACY). READ THIS BEFORE ADDING ANYTHING HERE.
════════════════════════════════════════════════════════════════════════════════

Orb has TWO stock systems and that is deliberate. Two stock systems that DISAGREE
about the same physical thing is a far worse outcome than one incomplete system,
so the line between them is drawn structurally rather than by convention:

  HELIX owns DRUGS.  services/helix_db.py — `helix_medication_master`,
  `helix_inventory_lots`, `helix_stock_movements`. It is DISPENSE-oriented: a lot
  moves because a pharmacist verified a prescription for a named patient, and the
  movement is tied to `helix_patient_med_orders`. Batch/expiry there exists to
  answer "which lot did this patient's dose come from", which is a recall and a
  medication-safety question.

  THE PHARMACY STORE (opt-in, ORB_PHARMACY_STORE, off by default) buys,
  receives, classifies and moves drugs. It reuses this module's indent,
  purchase order and goods receipt TABLES in the 'pharmacy' domain (see the
  Domains note below), but every drug unit it receives or moves is written to
  Helix's ledger by services/pharmacy_stock_service, never to this module's
  stock_movements. A drug's schedule and NDPS class live in Helix's drug
  master, set through the Pharmacy store; nothing here records them.

  PROCUREMENT (this module) owns EVERYTHING ELSE THE HOSPITAL BUYS. Surgical
  consumables, sutures, gloves, linen, lab reagents, CSSD items, housekeeping,
  stationery, PPE, implants, biomedical spares. It is PURCHASE-oriented: stock
  moves because a vendor delivered it against a purchase order, or because a ward
  drew it from a store. Its expiry tracking answers "which reagent lot goes off
  next", which is a stores question.

HOW A HOSPITAL TELLS WHICH SYSTEM OWNS WHAT — one question, no judgement call:

    "Is it a medicine that a pharmacist dispenses against a prescription?"
        YES → Helix / Pharmacy. It is not in this module and cannot be.
        NO  → Procurement / Stores.

That answer is enforced in two places. Migration 20 declares
`inventory_items.is_pharmacy INTEGER NOT NULL DEFAULT 0 CHECK (is_pharmacy = 0)`,
which refuses any row that DECLARES itself pharmacy, raw SQL included. It cannot
recognise a drug that arrives with the flag left at 0, which is every row the
panel sends, so `upsert_inventory_item` also refuses an item whose code or name
matches the Helix catalogue (`_helix_catalogue_match`), with a sentence a
storekeeper can act on.

The two ledgers therefore never describe the same physical unit, so they cannot
disagree. The known seam is the IV-fluids / disposables grey zone that some sites
class as pharmacy and some as stores: whichever a site picks, it must pick ONE,
and that choice is visible as the presence or absence of the item in this
module's master. Stated in docs and in the panel header, not left to folklore.

════════════════════════════════════════════════════════════════════════════════
 CONVENTIONS
════════════════════════════════════════════════════════════════════════════════

  * PURE SYNC. Every public function opens its own `db_session()`. Async callers
    go through `database.run_db`; the route module uses plain `def` handlers so
    FastAPI threadpools them. Blocking DB work inside `async def` is banned and
    tests/test_event_loop_discipline.py enforces it.
  * CONNECTION-TAKING CORES (grn-20 part a, 3 October 2026). Every write is
    split in two. `_<name>_core(db, ...)` validates and runs the SQL on a
    connection the CALLER owns: it never opens, commits or nests a session,
    never stamps a document number and never re-reads through a public getter,
    because each of those opens a session of its own. The public function keeps
    its signature and its behaviour: it opens the same session it always did,
    calls the core, and only after the commit stamps the number and re-reads
    the document. A later caller can therefore run a core together with its own
    audit row in ONE immediate transaction, and stamp the number after it. A
    core that compares a date with "today" takes it as `today=` (a date, or an
    ISO YYYY-MM-DD string): the stores wrappers here pass `date.today()`, as
    they always read it, and no core reads the server clock for a calendar date.
  * MONEY IS INTEGER PAISE and all arithmetic goes through services/money.py.
    There is no rounding rule in this file.
  * QUANTITIES ARE INTEGER `_milli` — thousandths of a unit — everywhere EXCEPT
    the two reorder columns; see `_reorder_level_milli` for why, and do not
    compare a `_qty` column against a `_milli` value anywhere.
  * NOT CONFIGURED IS NOT ZERO. An item nobody has priced has
    `last_purchase_rate_paise = NULL` and `rate_is_configured = 0`, and every
    read path returns `None` plus a display string, never `0`.
  * NO FOREIGN KEYS exist on these tables (migration 20). Referential integrity
    is this module's job; a dangling id yields "unknown", never a failed write.
  * NOTHING HERE IS A CLINICAL PATH. No function in this module writes to
    medications, vitals, alerts, orders or admissions, and no function here may
    ever block one. A store that has run out is a warning on a screen.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

# sqlcipher3.dbapi2.IntegrityError is NOT a subclass of sqlite3.IntegrityError, so
# `except sqlite3.IntegrityError` is DEAD CODE whenever ORB_DB_ENCRYPTION_KEY is
# set — i.e. in the shipping config. The idempotency replay below silently never
# fired on a real encrypted appliance. Guarded by tests/test_db_exception_aliases.
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, overload

from database import db_session
from db_backend import DBIntegrityError  # sqlcipher3 raises its OWN hierarchy:

from services.billing_masters import MAIN_FACILITY_ID, next_document_number
from services.money import (
    GST_TREATMENTS,
    QUANTITY_SCALE,
    _group_indian,
    assert_paise,
    compute_invoice,
    compute_line,
    format_inr_or_unset,
    round_to_rupee,
)

logger = logging.getLogger("orb.procurement")

__all__ = [
    "ProcurementError",
    "IllegalTransition",
    "PharmacyItemRefused",
    # state machines
    "INDENT_TRANSITIONS",
    "PO_TRANSITIONS",
    "GRN_TRANSITIONS",
    "RECEIVABLE_PO_STATUSES",
    "assert_transition",
    # domains (grn-20 part b)
    "STORES_DOMAIN",
    "PHARMACY_DOMAIN",
    "DOMAINS",
    "PHARMACY_STORE_KINDS",
    "INDENT_KINDS",
    # quantities
    "assert_quantity_milli",
    "units_to_milli",
    "format_quantity",
    # vendors
    "list_vendors",
    "get_vendor",
    "upsert_vendor",
    "set_vendor_blacklist",
    # stores + items
    "list_stores",
    "get_store",
    "upsert_store",
    "list_inventory_items",
    "get_inventory_item",
    "upsert_inventory_item",
    # indents
    "create_indent",
    "submit_indent",
    "decide_indent",
    "cancel_indent",
    "get_indent",
    "list_indents",
    # purchase orders
    "create_purchase_order",
    "submit_purchase_order",
    "approve_purchase_order",
    "issue_purchase_order",
    "cancel_purchase_order",
    "get_purchase_order",
    "list_purchase_orders",
    # goods receipt
    "create_goods_receipt",
    "post_goods_receipt",
    "cancel_goods_receipt",
    "get_goods_receipt",
    "list_goods_receipts",
    # stock
    "record_stock_movement",
    "issue_stock",
    "stock_on_hand_milli",
    "stock_on_hand",
    "list_stock_on_hand",
    "stock_ledger",
    # reports
    "low_stock",
    "consumption_report",
    "expiry_report",
    "procurement_overview",
]


# ═════════════════════════════════════════════════════════════════════════════
#  Errors
# ═════════════════════════════════════════════════════════════════════════════


class ProcurementError(ValueError):
    """Anything a caller did wrong. Carries a stable machine-readable `code` so
    the route layer maps it to an HTTP status without string-matching."""

    code = "procurement_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class IllegalTransition(ProcurementError):
    """A document was asked to move to a status it cannot reach from where it is."""

    code = "illegal_transition"


class PharmacyItemRefused(ProcurementError):
    """Somebody tried to put a drug in the non-pharmacy item master.

    This is the Helix boundary refusing at the front door rather than at the
    CHECK constraint, so the message can name the system that does own it."""

    code = "pharmacy_item_refused"


# ═════════════════════════════════════════════════════════════════════════════
#  State machines — the legal moves, in one place, as data
# ═════════════════════════════════════════════════════════════════════════════
#
# Kept as tables rather than as `if` ladders scattered across the write
# functions, because the whole point of a procurement chain is that a document
# cannot skip a step: nobody may receive goods against a purchase order that was
# never issued, and nobody may approve an indent that was never submitted.
# The tables are also what the tests assert against, so an added status has to
# be given its legal moves before anything can reach it.

INDENT_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"submitted", "cancelled"}),
    "submitted": frozenset({"approved", "partially_approved", "rejected", "cancelled"}),
    "approved": frozenset({"converted", "closed", "cancelled"}),
    "partially_approved": frozenset({"converted", "closed", "cancelled"}),
    # Terminal. A rejected indent is re-raised as a new one; it is not reopened,
    # so that the rejection stays a fact somebody can point at.
    "rejected": frozenset(),
    "converted": frozenset({"closed"}),
    "cancelled": frozenset(),
    "closed": frozenset(),
}

PO_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"pending_approval", "cancelled"}),
    # Back to draft is a legal move: an approver returning a PO for correction is
    # a real thing, and it must not require cancelling and retyping the order.
    "pending_approval": frozenset({"approved", "draft", "cancelled"}),
    "approved": frozenset({"issued", "cancelled"}),
    # Once ISSUED the order is with the vendor. It may still be cancelled (the
    # vendor is told), but only while nothing has been received against it —
    # `cancel_purchase_order` enforces that separately, because "has anything
    # arrived" is a quantity question the transition table cannot see.
    "issued": frozenset({"partially_received", "received", "cancelled", "closed"}),
    "partially_received": frozenset({"partially_received", "received", "closed"}),
    "received": frozenset({"closed"}),
    "closed": frozenset(),
    "cancelled": frozenset(),
}

GRN_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"posted", "cancelled"}),
    # A posted GRN has already moved stock. Reversing it is a returns workflow
    # with its own debit note and vendor conversation; it is NOT implemented in
    # this version, and pretending otherwise by allowing 'cancelled' here would
    # let somebody delete a receipt while the goods stay on the shelf.
    "posted": frozenset({"returned"}),
    "cancelled": frozenset(),
    "returned": frozenset(),
}

#: The only PO statuses a goods receipt may be raised against.
RECEIVABLE_PO_STATUSES: frozenset[str] = frozenset({"issued", "partially_received"})

_TRANSITION_TABLES = {
    "indent": INDENT_TRANSITIONS,
    "purchase_order": PO_TRANSITIONS,
    "goods_receipt": GRN_TRANSITIONS,
}


def assert_transition(kind: str, current: str, target: str) -> None:
    """Raise unless `current -> target` is a legal move for this document kind."""
    table = _TRANSITION_TABLES.get(kind)
    if table is None:
        raise ProcurementError(f"unknown document kind {kind!r}", code="unknown_kind")
    if current not in table:
        raise IllegalTransition(f"{kind} is in unknown status {current!r}")
    if target not in table[current]:
        allowed = sorted(table[current]) or ["nothing: this status is final"]
        raise IllegalTransition(
            f"a {kind} in status {current!r} cannot move to {target!r}; "
            f"legal moves are: {', '.join(allowed)}"
        )


# ═════════════════════════════════════════════════════════════════════════════
#  Domains: one set of document tables, two purchasing desks (grn-20 part b)
# ═════════════════════════════════════════════════════════════════════════════
#
# Migration 54 gave indents, purchase orders and goods receipts a `domain`
# column ('stores' or 'pharmacy') and the store master a `pharmacy_kind`. The
# pharmacy store reuses this module's documents, state machines, GST maths and
# numbering through the connection-taking cores below, with domain='pharmacy'.
# What keeps the two desks apart:
#
#   * EVERY CORE THAT LOADS A DOCUMENT BY ID REFUSES THE OTHER DESK'S DOCUMENT
#     with code not_found, worded exactly as a document that does not exist.
#     The stores routes call the stores wrappers, so a pharmacy id is a 404 on
#     /api/procurement whatever the caller's tier.
#   * A GOODS RECEIPT TAKES ITS DOMAIN FROM ITS PURCHASE ORDER, never from the
#     request, so a stores receipt can never be raised against a pharmacy order.
#   * THE STORE MASTER IS PARTITIONED. A row with pharmacy_kind is a pharmacy
#     store: the stores desk cannot list it, read it, edit it, raise a document
#     against it or move stock through it, and the pharmacy desk sees only
#     those rows. Neither desk can take over the other's store code.
#   * THE PHARMACY BRANCH OF A RECEIPT POST NEVER TOUCHES stock_movements OR
#     inventory_items. Drug stock has one ledger, Helix's, written only by
#     pharmacy_stock_service; a second one is the failure this module's header
#     exists to prevent.
#   * Lists, reports and the overview of the stores desk count stores documents
#     and stores stores only.
#
# Every core takes `domain` keyword-only, defaulting to 'stores', so the public
# wrappers and every existing caller behave exactly as before. A pharmacy caller
# passes domain='pharmacy' explicitly; forgetting it fails closed (its own
# documents read as not found).

STORES_DOMAIN = "stores"
PHARMACY_DOMAIN = "pharmacy"
DOMAINS = (STORES_DOMAIN, PHARMACY_DOMAIN)

#: stores.pharmacy_kind (migration 54). A store with a kind belongs to the
#: pharmacy desk; NULL is a stores store.
PHARMACY_STORE_KINDS = (
    "dispensing",
    "sub_store",
    "ndps_vault",
    "returns_quarantine",
    "ward_imprest",
    "crash_cart",
)

#: Kinds that hold drugs on a ward. Refused until ORB_PHARMACY_WARD_STOCK is on,
#: which a site turns on only after ruling GRN-RS5 (which drugs a ward may hold).
WARD_PHARMACY_STORE_KINDS = frozenset({"ward_imprest", "crash_cart"})

#: indents.indent_kind (migration 54). A purchase indent asks for something to
#: be bought (every stores indent); an issue indent asks another store for stock.
INDENT_KINDS = ("purchase", "issue")

#: The column on purchase_order_lines, goods_receipt_lines and indent_lines
#: (migration 54) that names the Helix drug a pharmacy line is for. It is an id
#: stored on a procurement row; no SQL in this module reads or writes a Helix
#: table, and test_procurement.py's structural guard enforces that by refusing
#: the text "helix_" in any SQL string here, so the column name is interpolated
#: from this constant rather than spelled inside the SQL.
_DRUG_ID_COLUMN = "helix_medication_id"


def _assert_domain(domain: str) -> str:
    if domain not in DOMAINS:
        raise ProcurementError(
            f"unknown purchasing domain {domain!r}; expected one of {list(DOMAINS)}",
            code="unknown_domain",
        )
    return domain


def _store_partition_sql(domain: str, alias: str = "") -> str:
    """The condition that keeps a store row in its own desk's half of the
    master. `alias` is a table alias from this module, never caller input."""
    column = f"{alias}.pharmacy_kind" if alias else "pharmacy_kind"
    return f"{column} IS NOT NULL" if domain == PHARMACY_DOMAIN else f"{column} IS NULL"


def _store_row(db, code_or_id: str | int, domain: str):
    """The store with this code (or id), if it belongs to `domain`; else None.

    A store of the other desk is answered exactly as a store that does not
    exist, so each caller's existing "no store with code ..." refusal covers
    it."""
    _assert_domain(domain)
    where = _store_partition_sql(domain)
    if isinstance(code_or_id, int):
        return db.execute(
            f"SELECT * FROM stores WHERE id = ? AND {where}", (code_or_id,)
        ).fetchone()
    return db.execute(
        f"SELECT * FROM stores WHERE code = ? AND {where}", (str(code_or_id).upper(),)
    ).fetchone()


def _document_in_domain(db, table: str, doc_id: int, domain: str, noun: str):
    """The document row this desk may act on, else ProcurementError not_found.

    `table` and `noun` are literals from this module. A document of the other
    desk raises the same error, with the same words, as one that does not
    exist: the stores desk must not learn a pharmacy document's status from a
    refusal."""
    _assert_domain(domain)
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (doc_id,)).fetchone()
    if not row or row["domain"] != domain:
        raise ProcurementError(f"no {noun} {doc_id}", code="not_found")
    return row


def _pharmacy_line_item(db, item_code: str, resolve_line) -> dict:
    """A pharmacy document line's drug, through the caller's resolver.

    Procurement holds no drug SQL, so the pharmacy service passes
    `resolve_line(db, item_code) -> dict | None`, answering with: code, name,
    helix_medication_id (required); uom, hsn_sac, gst_treatment, gst_rate_bp
    and units_per_pack (optional, None when not configured). None means no such
    drug, refused as unknown_item."""
    if resolve_line is None:
        raise ProcurementError(
            "a pharmacy document line needs the pharmacy catalogue to resolve it",
            code="pharmacy_lines_unresolved",
        )
    found = resolve_line(db, item_code)
    if not found:
        raise ProcurementError(
            f"no drug with code {item_code!r} in the pharmacy catalogue",
            code="unknown_item",
        )
    item = dict(found)
    for key in ("code", "name", _DRUG_ID_COLUMN):
        if not item.get(key):
            raise ProcurementError(
                f"the pharmacy catalogue answered {item_code!r} without a {key}",
                code="pharmacy_lines_unresolved",
            )
    units = item.get("units_per_pack")
    if units is not None and (isinstance(units, bool) or not isinstance(units, int) or units < 1):
        raise ProcurementError(
            f"{item['code']}: units per pack must be a whole number of at least 1",
            code="bad_pack_size",
        )
    item["id"] = None  # pharmacy lines never name an inventory_items row
    return item


# ═════════════════════════════════════════════════════════════════════════════
#  Quantities
# ═════════════════════════════════════════════════════════════════════════════


def assert_quantity_milli(value: object, name: str = "quantity_milli") -> int:
    """Quantities are integer THOUSANDTHS of a unit, for the same reason money is
    integer paise: half a pack and 2.5 metres of tubing are real, and neither may
    arrive as a float."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProcurementError(
            f"{name} must be an integer of thousandths of a unit (got "
            f"{value!r}); floats cannot represent a quantity exactly",
            code="bad_quantity",
        )
    return value


def assert_positive_quantity_milli(value: object, name: str = "quantity_milli") -> int:
    qty = assert_quantity_milli(value, name)
    if qty <= 0:
        raise ProcurementError(f"{name} must be greater than zero (got {qty})", code="bad_quantity")
    return qty


def units_to_milli(units: int) -> int:
    """Whole units → milli. Used at the reorder-column boundary and in seeds."""
    if isinstance(units, bool) or not isinstance(units, int):
        raise ProcurementError(
            f"unit quantity must be an integer (got {units!r})", code="bad_quantity"
        )
    return units * QUANTITY_SCALE


def format_quantity(milli: int | None, uom: str | None = None) -> str:
    """A quantity for a screen. `None` is "—", never "0".

    Grouped the Indian way (1,00,000), as money is: "2000 each" beside
    "₹1,37,500" read as two different systems on one line."""
    if milli is None:
        return "—"
    negative = milli < 0
    whole, frac = divmod(abs(milli), QUANTITY_SCALE)
    grouped = _group_indian(str(whole))
    body = grouped if frac == 0 else f"{grouped}.{frac:03d}".rstrip("0")
    if negative:
        body = f"-{body}"
    return f"{body} {uom}" if uom else body


def _reorder_level_milli(row: Any) -> int | None:
    """The reorder level in MILLI, from a column that is stored in WHOLE UNITS.

    `inventory_items.reorder_level_qty` (migration 20) does NOT end in `_milli`,
    and the naming convention this schema is built on means exactly what it says:
    the column is whole units. Comparing an on-hand figure in milli against it
    directly would read a reorder level of 10 boxes as 0.01 of a box — the store
    would never trip its own low-stock alert, and nobody would notice until the
    shelf was empty. Every comparison in this module goes through here.
    """
    raw = _get(row, "reorder_level_qty")
    if raw is None:
        return None
    return int(raw) * QUANTITY_SCALE


def _reorder_qty_milli(row: Any) -> int | None:
    raw = _get(row, "reorder_qty")
    if raw is None:
        return None
    return int(raw) * QUANTITY_SCALE


# ═════════════════════════════════════════════════════════════════════════════
#  Row helpers
# ═════════════════════════════════════════════════════════════════════════════


def _row(row) -> dict:
    return {k: row[k] for k in row.keys()} if row is not None else {}


def _get(row: Any, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


@overload
def _clean(text: str | None, *, field_name: str, required: Literal[True]) -> str: ...


@overload
def _clean(
    text: str | None, *, field_name: str, required: Literal[False] = False
) -> str | None: ...


@overload
def _clean(text: str | None, *, field_name: str, required: bool = False) -> str | None: ...


def _clean(text: str | None, *, field_name: str, required: bool = False) -> str | None:
    value = (text or "").strip()
    if not value:
        if required:
            raise ProcurementError(f"{field_name} is required", code="missing_field")
        return None
    return value


def _now() -> str:
    """UTC, second resolution, matching the CURRENT_TIMESTAMP format SQLite
    writes for the columns that default. Mixed formats in one column are how a
    date range silently drops rows."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _calendar_today(today: date | datetime | str) -> date:
    """The caller's "today" as a calendar date, for a core that compares one.

    Required, never defaulted: which clock decides "today" is the CALLER's
    decision (the stores wrappers pass `date.today()`; a site-timezone caller
    passes its own date), and a silent fallback to the server clock is exactly
    the defect this parameter exists to remove. Anything else is a programming
    error and fails loudly.

    A timezone-aware datetime is an INSTANT, so its calendar date is the one on
    the hospital's wall clock (services.hospital_time.hospital_timezone, the
    one resolver for "which day is it here"), never its UTC date: at 20:00 UTC
    it is already tomorrow in an IST hospital, and a lot expiring today would
    otherwise still be received tonight. A naive datetime is already a wall
    clock reading and is cut to its own date.
    """
    if isinstance(today, datetime):
        if today.tzinfo is not None and today.utcoffset() is not None:
            from services.hospital_time import hospital_timezone  # lazy: a leaf helper

            return today.astimezone(hospital_timezone()).date()
        return today.date()
    if isinstance(today, date):
        return today
    if isinstance(today, str):
        return date.fromisoformat(today.strip())
    raise TypeError(f"today must be a date or an ISO YYYY-MM-DD string, not {type(today).__name__}")


def _as_date_str(value: date | datetime | str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ProcurementError(f"{field_name} is not a date: {value!r}", code="bad_date") from exc
    return text[:10]


#: Document series stamped by this module. Table and column are LITERALS from
#: this constant and never come from a caller — they are interpolated into SQL,
#: which is safe only because of that.
#:
#: The fourth element is the domain a row must be in to take a number from
#: that series (None for a table with no domain column). The pharmacy desk
#: numbers its documents from its own series (GRN-R11, migration 54 seeds them
#: with is_configured = 0), and a stores number is never stamped on a pharmacy
#: document or the reverse.
_DOC_SERIES = {
    "indent": ("indents", "indent_number", "IND", STORES_DOMAIN),
    "purchase_order": ("purchase_orders", "po_number", "PO", STORES_DOMAIN),
    "goods_receipt": ("goods_receipts", "grn_number", "GRN", STORES_DOMAIN),
    "pharmacy_indent": ("indents", "indent_number", "PHIND", PHARMACY_DOMAIN),
    "pharmacy_purchase_order": (
        "purchase_orders",
        "po_number",
        "PHPO",
        PHARMACY_DOMAIN,
    ),
    "pharmacy_goods_receipt": (
        "goods_receipts",
        "grn_number",
        "PHGRN",
        PHARMACY_DOMAIN,
    ),
    "pharmacy_transfer": ("pharmacy_transfers", "transfer_number", "PHTRF", None),
    "pharmacy_vendor_return": (
        "pharmacy_vendor_returns",
        "return_number",
        "PHVR",
        None,
    ),
    "pharmacy_ward_return": ("pharmacy_ward_returns", "return_number", "PHRET", None),
    "pharmacy_stock_count": ("pharmacy_stock_counts", "count_number", "PHCNT", None),
}


def _stamp_document_number(kind: str, doc_id: int) -> str | None:
    """Allocate a document number and stamp it on a row that is ALREADY committed.

    WHY THIS IS A SECOND TRANSACTION rather than part of the one that created the
    document. `billing_masters.next_document_number` allocates under its own
    `db_session(immediate=True)` — it has to, because a sequence that is not
    serialised hands the same number to two clerks at once — and Orb REFUSES a
    nested immediate session (database.py:1090), correctly: a nested frame is a
    SAVEPOINT inside an already-DEFERRED transaction and cannot be upgraded to a
    write lock.

    So there is a choice, and it is a real one:

      allocate BEFORE the document's transaction → if that transaction then
        fails, the number is burned and the series has a GAP;
      allocate AFTER it → if the stamp fails, the document exists with no number.

    This module takes the second. A goods receipt whose stock is already on the
    shelf but whose number is blank is visible on the screen, explainable, and
    repairable by stamping it again; a gap in a numbered series is a question an
    auditor asks and nobody can answer. The failure is logged at ERROR with the
    row id, and it NEVER rolls back stock that has physically arrived.

    Idempotent: a row that already has a number is left alone and no number is
    drawn from the series.

    A row of the other desk's domain (a stores kind asked to number a pharmacy
    document, or the reverse) draws nothing and is logged at ERROR: a number
    from the wrong series cannot be taken back.
    """
    table, column, series, domain = _DOC_SERIES[kind]
    domain_col = ", domain" if domain else ""
    with db_session() as db:
        row = db.execute(
            f"SELECT {column} AS n{domain_col} FROM {table} WHERE id = ?", (doc_id,)
        ).fetchone()
    if row is None:
        return None
    if domain and row["domain"] != domain:
        logger.error(
            "refused to number %s %s from the %s series: it is a %s document",
            table,
            doc_id,
            series,
            row["domain"],
        )
        return None
    if row["n"]:
        return str(row["n"])
    try:
        # Facility 1's series (multisite-7a): stores, indents, orders and
        # receipts carry no facility until multisite-7b stamps one from the
        # store, so every stores and pharmacy document numbers there.
        allocated = next_document_number(series, facility_id=MAIN_FACILITY_ID)
    except Exception:
        logger.error(
            "could not allocate a %s number for %s %s — the document is saved and "
            "any stock it moved is real; it is simply unnumbered and needs "
            "stamping again",
            series,
            table,
            doc_id,
            exc_info=True,
        )
        return None
    with db_session() as db:
        db.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ? AND {column} IS NULL",
            (allocated["number"], doc_id),
        )
    return allocated["number"]


def _gst_treatment(value: str | None) -> str:
    treatment = (value or "taxable").strip().lower()
    if treatment not in GST_TREATMENTS:
        raise ProcurementError(
            f"unknown GST treatment {value!r}; expected one of {sorted(GST_TREATMENTS)}",
            code="bad_gst_treatment",
        )
    return treatment


# ═════════════════════════════════════════════════════════════════════════════
#  Vendors
# ═════════════════════════════════════════════════════════════════════════════

VENDOR_TYPES = ("supplier", "service", "amc", "contractor", "distributor")


def list_vendors(
    *,
    active_only: bool = True,
    include_blacklisted: bool = False,
    search: str | None = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT * FROM vendors WHERE 1=1"
    params: list[Any] = []
    if active_only:
        sql += " AND is_active = 1"
    if not include_blacklisted:
        sql += " AND is_blacklisted = 0"
    if search:
        sql += " AND (name LIKE ? OR code LIKE ? OR COALESCE(gstin,'') LIKE ?)"
        params.extend([f"%{search}%"] * 3)
    sql += " ORDER BY name LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        return [_row(r) for r in db.execute(sql, params).fetchall()]


def get_vendor(code_or_id: str | int) -> dict | None:
    with db_session() as db:
        if isinstance(code_or_id, int):
            row = db.execute("SELECT * FROM vendors WHERE id = ?", (code_or_id,)).fetchone()
        else:
            row = db.execute("SELECT * FROM vendors WHERE code = ?", (code_or_id,)).fetchone()
    return _row(row) if row else None


def _refuse_existing(db, table: str, code: str, noun: str) -> None:
    """The Add forms' guard. These masters are keyed on a code and written by an
    upsert, so "Add" on a code already in use overwrote that record with a form
    full of blanks. A create now refuses, naming what already holds the code.
    `table` is always one of this module's own table names, never caller input.
    """
    row = db.execute(f"SELECT name FROM {table} WHERE code = ?", (code,)).fetchone()
    if row:
        raise ProcurementError(
            f"{code} already exists as {row['name']}. Nothing was changed. Use a "
            f"different code, or edit that {noun}.",
            code="already_exists",
        )


def upsert_vendor(
    *,
    code: str,
    name: str,
    vendor_type: str = "supplier",
    gstin: str | None = None,
    pan: str | None = None,
    msme_number: str | None = None,
    state_code: str | None = None,
    address: str | None = None,
    contact_name: str | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    payment_terms_days: int | None = None,
    bank_name: str | None = None,
    account_last4: str | None = None,
    ifsc: str | None = None,
    rating: int | None = None,
    is_active: bool = True,
    notes: str | None = None,
    created_by: str | None = None,
    create_only: bool = False,
) -> dict:
    """Create or update a vendor, keyed on `code`.

    `create_only` is the Add form: a code already in the master is refused
    (`already_exists`) rather than overwritten, and on an update a blank optional
    field keeps what is on file (see `_refuse_existing`).

    `account_last4` is deliberately the LAST FOUR DIGITS ONLY (migration 20).
    This appliance holds PHI and has no vendor-payment rail; storing a full
    account number would make it a payments target for no operational gain. Four
    digits are enough to reconcile a transfer somebody else made.
    """
    with db_session() as db:
        return _upsert_vendor_core(
            db,
            code=code,
            name=name,
            vendor_type=vendor_type,
            gstin=gstin,
            pan=pan,
            msme_number=msme_number,
            state_code=state_code,
            address=address,
            contact_name=contact_name,
            contact_phone=contact_phone,
            contact_email=contact_email,
            payment_terms_days=payment_terms_days,
            bank_name=bank_name,
            account_last4=account_last4,
            ifsc=ifsc,
            rating=rating,
            is_active=is_active,
            notes=notes,
            created_by=created_by,
            create_only=create_only,
        )


def _upsert_vendor_core(
    db,
    *,
    code: str,
    name: str,
    vendor_type: str = "supplier",
    gstin: str | None = None,
    pan: str | None = None,
    msme_number: str | None = None,
    state_code: str | None = None,
    address: str | None = None,
    contact_name: str | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    payment_terms_days: int | None = None,
    bank_name: str | None = None,
    account_last4: str | None = None,
    ifsc: str | None = None,
    rating: int | None = None,
    is_active: bool = True,
    notes: str | None = None,
    created_by: str | None = None,
    create_only: bool = False,
    drug_licence_numbers: str | None = None,
    drug_licence_valid_to: date | str | None = None,
    ndps_licence_number: str | None = None,
) -> dict:
    """`upsert_vendor` on the caller's connection: validate, write, read back.

    The vendors table is shared by every purchasing desk, so this is the one
    place a vendor row is written; see `upsert_vendor` for the rules.

    The three licence fields (migration 54) are the pharmacy desk's: its
    Masters view passes them, the stores wrapper never does. Like every other
    optional field, a blank on an update keeps what is on file. Whether a
    licence is valid enough to buy from (GRN-R10) is the pharmacy service's
    rule, not this one's."""
    code = _clean(code, field_name="vendor code", required=True).upper()
    name = _clean(name, field_name="vendor name", required=True)
    vendor_type = (vendor_type or "supplier").strip().lower()
    if vendor_type not in VENDOR_TYPES:
        raise ProcurementError(
            f"unknown vendor type {vendor_type!r}; expected one of {list(VENDOR_TYPES)}",
            code="bad_vendor_type",
        )
    if account_last4:
        digits = str(account_last4).strip()
        if len(digits) > 4 or not digits.isdigit():
            raise ProcurementError(
                "account_last4 must be at most four digits. Orb deliberately "
                "does not store full bank account numbers",
                code="bad_account_last4",
            )
        account_last4 = digits
    if rating is not None and not (0 <= int(rating) <= 5):
        raise ProcurementError("vendor rating must be 0–5", code="bad_rating")
    licence_to = _as_date_str(drug_licence_valid_to, "drug_licence_valid_to")
    licence_numbers = _clean(drug_licence_numbers, field_name="drug licence numbers")
    ndps_licence = _clean(ndps_licence_number, field_name="NDPS licence number")

    if create_only:
        _refuse_existing(db, "vendors", code, "vendor")
    db.execute(
        """INSERT INTO vendors (code, name, vendor_type, gstin, pan, msme_number,
               state_code, address, contact_name, contact_phone, contact_email,
               payment_terms_days, bank_name, account_last4, ifsc, rating,
               is_active, notes, created_by, drug_licence_numbers,
               drug_licence_valid_to, ndps_licence_number)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(code) DO UPDATE SET
               name = excluded.name,
               vendor_type = excluded.vendor_type,
               gstin = COALESCE(excluded.gstin, gstin),
               pan = COALESCE(excluded.pan, pan),
               msme_number = COALESCE(excluded.msme_number, msme_number),
               state_code = COALESCE(excluded.state_code, state_code),
               address = COALESCE(excluded.address, address),
               contact_name = COALESCE(excluded.contact_name, contact_name),
               contact_phone = COALESCE(excluded.contact_phone, contact_phone),
               contact_email = COALESCE(excluded.contact_email, contact_email),
               payment_terms_days = COALESCE(excluded.payment_terms_days, payment_terms_days),
               bank_name = COALESCE(excluded.bank_name, bank_name),
               account_last4 = COALESCE(excluded.account_last4, account_last4),
               ifsc = COALESCE(excluded.ifsc, ifsc),
               rating = COALESCE(excluded.rating, rating),
               is_active = excluded.is_active,
               notes = COALESCE(excluded.notes, notes),
               drug_licence_numbers = COALESCE(excluded.drug_licence_numbers,
                                               drug_licence_numbers),
               drug_licence_valid_to = COALESCE(excluded.drug_licence_valid_to,
                                                drug_licence_valid_to),
               ndps_licence_number = COALESCE(excluded.ndps_licence_number,
                                              ndps_licence_number),
               updated_at = CURRENT_TIMESTAMP""",
        (
            code,
            name,
            vendor_type,
            gstin,
            pan,
            msme_number,
            state_code,
            address,
            contact_name,
            contact_phone,
            contact_email,
            int(payment_terms_days) if payment_terms_days is not None else None,
            bank_name,
            account_last4,
            ifsc,
            int(rating) if rating is not None else None,
            1 if is_active else 0,
            notes,
            created_by,
            licence_numbers,
            licence_to,
            ndps_licence,
        ),
    )
    row = db.execute("SELECT * FROM vendors WHERE code = ?", (code,)).fetchone()
    return _row(row)


def set_vendor_blacklist(
    code: str,
    *,
    blacklisted: bool,
    reason: str | None = None,
    actor: str | None = None,
) -> dict:
    """Blacklist / un-blacklist a vendor. A blacklisted vendor cannot take a new
    purchase order; orders already placed are untouched, because the goods may
    already be on a lorry."""
    with db_session() as db:
        row = _set_vendor_blacklist_core(db, code, blacklisted=blacklisted, reason=reason)
    logger.info("vendor %s blacklist=%s by %s", code, blacklisted, actor)
    return row


def _set_vendor_blacklist_core(
    db,
    code: str,
    *,
    blacklisted: bool,
    reason: str | None = None,
) -> dict:
    """`set_vendor_blacklist` on the caller's connection."""
    if blacklisted and not (reason or "").strip():
        raise ProcurementError("a blacklist needs a reason", code="missing_field")
    cur = db.execute(
        "UPDATE vendors SET is_blacklisted = ?, blacklist_reason = ?, "
        "updated_at = CURRENT_TIMESTAMP WHERE code = ?",
        (1 if blacklisted else 0, (reason or "").strip() or None, code),
    )
    if cur.rowcount == 0:
        raise ProcurementError(f"no vendor with code {code!r}", code="not_found")
    row = db.execute("SELECT * FROM vendors WHERE code = ?", (code,)).fetchone()
    return _row(row)


# ═════════════════════════════════════════════════════════════════════════════
#  Stores
# ═════════════════════════════════════════════════════════════════════════════

STORE_TYPES = (
    "central",
    "sub_store",
    "ot",
    "ward",
    "housekeeping",
    "biomedical",
    "linen",
)


def list_stores(*, active_only: bool = True, domain: str = STORES_DOMAIN) -> list[dict]:
    """The stores of one desk's half of the master (see the Domains note)."""
    _assert_domain(domain)
    sql = f"SELECT * FROM stores WHERE {_store_partition_sql(domain)}"
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY store_type, name"
    with db_session() as db:
        return [_row(r) for r in db.execute(sql).fetchall()]


def get_store(code_or_id: str | int, *, domain: str = STORES_DOMAIN) -> dict | None:
    """One store of this desk's half of the master, else None. The stores
    lookup is by exact code, as it always was."""
    _assert_domain(domain)
    where = _store_partition_sql(domain)
    with db_session() as db:
        if isinstance(code_or_id, int):
            row = db.execute(
                f"SELECT * FROM stores WHERE id = ? AND {where}", (code_or_id,)
            ).fetchone()
        else:
            row = db.execute(
                f"SELECT * FROM stores WHERE code = ? AND {where}", (code_or_id,)
            ).fetchone()
    return _row(row) if row else None


def upsert_store(
    *,
    code: str,
    name: str,
    store_type: str = "sub_store",
    department: str | None = None,
    ward: str | None = None,
    incharge_user_id: str | None = None,
    is_active: bool = True,
    create_only: bool = False,
) -> dict:
    with db_session() as db:
        return _upsert_store_core(
            db,
            code=code,
            name=name,
            store_type=store_type,
            department=department,
            ward=ward,
            incharge_user_id=incharge_user_id,
            is_active=is_active,
            create_only=create_only,
        )


def _upsert_store_core(
    db,
    *,
    code: str,
    name: str,
    store_type: str = "sub_store",
    department: str | None = None,
    ward: str | None = None,
    incharge_user_id: str | None = None,
    is_active: bool = True,
    create_only: bool = False,
    domain: str = STORES_DOMAIN,
    pharmacy_kind: str | None = None,
) -> dict:
    """`upsert_store` on the caller's connection: validate, write, read back.

    The store master is partitioned by desk (see the Domains note). A stores
    upsert of a code a pharmacy store holds is refused, so the stores desk
    can neither rename nor deactivate a pharmacy store; the pharmacy desk
    cannot take over a stores store's code either. A pharmacy store names its
    `pharmacy_kind` when it is created and keeps it: the kind decides whether
    its stock can be dispensed, so a store does not change kind, it is retired
    and another created. Ward kinds are refused while ORB_PHARMACY_WARD_STOCK
    is off (ruling GRN-RS5), and a second active dispensing store is refused
    (GRN-R17, enforced by migration 54's partial unique index)."""
    _assert_domain(domain)
    code = _clean(code, field_name="store code", required=True).upper()
    name = _clean(name, field_name="store name", required=True)
    store_type = (store_type or "sub_store").strip().lower()
    if store_type not in STORE_TYPES:
        raise ProcurementError(
            f"unknown store type {store_type!r}; expected one of {list(STORE_TYPES)}",
            code="bad_store_type",
        )
    kind = (pharmacy_kind or "").strip().lower() or None
    if domain == STORES_DOMAIN and kind is not None:
        raise ProcurementError(
            "a stores store has no pharmacy kind; pharmacy stores are created from "
            "the pharmacy store desk",
            code="bad_store_kind",
        )
    if domain == PHARMACY_DOMAIN:
        if kind not in PHARMACY_STORE_KINDS:
            raise ProcurementError(
                f"unknown pharmacy store kind {pharmacy_kind!r}; expected one of "
                f"{list(PHARMACY_STORE_KINDS)}",
                code="bad_store_kind",
            )
        if kind in WARD_PHARMACY_STORE_KINDS:
            import config  # lazy: read at call time, as tests patch it

            if not config.feature_enabled("pharmacy_ward_stock"):
                raise ProcurementError(
                    "ward and crash-cart drug stores are switched off on this site "
                    "(ORB_PHARMACY_WARD_STOCK). Nothing was changed.",
                    code="ward_stock_off",
                )

    existing = db.execute(
        "SELECT name, pharmacy_kind FROM stores WHERE code = ?", (code,)
    ).fetchone()
    if existing is not None:
        held_by_pharmacy = existing["pharmacy_kind"] is not None
        if domain == STORES_DOMAIN and held_by_pharmacy:
            raise ProcurementError(
                f"{code} is a pharmacy store ({existing['name']}). It is managed from "
                "the pharmacy store desk, not from Procurement. Nothing was changed.",
                code="already_exists",
            )
        if domain == PHARMACY_DOMAIN and not held_by_pharmacy:
            raise ProcurementError(
                f"{code} already exists as the stores store {existing['name']}. Nothing "
                "was changed. Use a different code for the pharmacy store.",
                code="already_exists",
            )
        if domain == PHARMACY_DOMAIN and existing["pharmacy_kind"] != kind:
            raise ProcurementError(
                f"{code} is a {existing['pharmacy_kind'].replace('_', ' ')} store and stays "
                "one. Retire it and create a new store instead. Nothing was changed.",
                code="store_kind_fixed",
            )
    if create_only:
        _refuse_existing(db, "stores", code, "store")
    try:
        db.execute(
            """INSERT INTO stores (code, name, store_type, department, ward,
                   incharge_user_id, is_active, pharmacy_kind)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                   name = excluded.name,
                   store_type = excluded.store_type,
                   department = COALESCE(excluded.department, department),
                   ward = COALESCE(excluded.ward, ward),
                   incharge_user_id = COALESCE(excluded.incharge_user_id, incharge_user_id),
                   is_active = excluded.is_active""",
            (
                code,
                name,
                store_type,
                department,
                ward,
                incharge_user_id,
                1 if is_active else 0,
                kind,
            ),
        )
    except DBIntegrityError as exc:
        if kind == "dispensing" and "pharmacy_kind" in str(exc).lower():
            raise ProcurementError(
                "this site already has an active dispensing store. Deactivate it "
                "first; one dispensing pharmacy per site. Nothing was changed.",
                code="dispensing_store_exists",
            ) from exc
        raise
    row = db.execute("SELECT * FROM stores WHERE code = ?", (code,)).fetchone()
    return _row(row)


# ═════════════════════════════════════════════════════════════════════════════
#  Inventory items (NON-pharmacy — see the Helix boundary at the top)
# ═════════════════════════════════════════════════════════════════════════════

ITEM_CATEGORIES = (
    "consumable",
    "implant",
    "equipment",
    "instrument",
    "stationery",
    "housekeeping",
    "linen",
    "reagent",
    "ppe",
    "other",
)

#: Categories that CAN carry a batch and an expiry, and therefore appear in the
#: expiry report when one is entered. A suture pack and a lab reagent go off; a
#: stethoscope and a bedsheet do not, and a bedsheet in an expiry list is noise
#: that teaches people to skim the list.
EXPIRY_TRACKED_CATEGORIES: frozenset[str] = frozenset(
    {
        "consumable",
        "implant",
        "reagent",
        "ppe",
    }
)

#: Categories where a receipt WITHOUT a batch and expiry is REFUSED. Deliberately
#: narrower than the tracked set, and the gap between the two is the point:
#:
#:   implant — a hospital must be able to answer "which lot went into which
#:     patient" for a recall. There is no acceptable blank here.
#:   reagent — a result produced with an expired reagent is not a result. The lab
#:     needs the date to invalidate it.
#:
#: Consumables and PPE are TRACKED IF ENTERED but never demanded. Sterility
#: dating is real, but forcing a date on every box of gloves at a busy receiving
#: bay produces typed-in fiction, and fiction in this column would poison exactly
#: the implant and reagent dates that have to be trusted. A hospital that wants
#: it mandatory for a specific item needs a per-item flag, which this schema does
#: not have — recorded as a follow-up rather than faked with a category.
EXPIRY_MANDATORY_CATEGORIES: frozenset[str] = frozenset({"implant", "reagent"})


def item_tracks_expiry(item: dict | None) -> bool:
    """True when this item's batches belong in the expiry report."""
    if not item:
        return False
    return str(_get(item, "category") or "") in EXPIRY_TRACKED_CATEGORIES


def item_requires_expiry(item: dict | None) -> bool:
    """True when a receipt without a batch and expiry must be refused."""
    if not item:
        return False
    return str(_get(item, "category") or "") in EXPIRY_MANDATORY_CATEGORIES


def list_inventory_items(
    *,
    category: str | None = None,
    active_only: bool = True,
    search: str | None = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT * FROM inventory_items WHERE 1=1"
    params: list[Any] = []
    if active_only:
        sql += " AND is_active = 1"
    if category:
        sql += " AND category = ?"
        params.append(category)
    if search:
        sql += " AND (name LIKE ? OR code LIKE ?)"
        params.extend([f"%{search}%"] * 2)
    sql += " ORDER BY category, name LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]
    for row in rows:
        _decorate_item(row)
    return rows


def _decorate_item(row: dict) -> dict:
    """Add the derived fields every screen needs, including the honest price.

    `rate_is_configured` is carried through as a BOOLEAN and the display string
    is computed here, so no caller can print `₹0.00` for an item nobody priced.
    """
    configured = bool(_get(row, "rate_is_configured"))
    rate = _get(row, "last_purchase_rate_paise")
    row["rate_is_configured"] = configured
    row["last_purchase_rate_paise"] = rate if configured else None
    row["last_purchase_rate_display"] = format_inr_or_unset(
        rate if configured else None, unset="Not configured"
    )
    row["tracks_expiry"] = item_tracks_expiry(row)
    row["reorder_level_milli"] = _reorder_level_milli(row)
    row["reorder_qty_milli"] = _reorder_qty_milli(row)
    return row


def get_inventory_item(code_or_id: str | int) -> dict | None:
    with db_session() as db:
        if isinstance(code_or_id, int):
            row = db.execute("SELECT * FROM inventory_items WHERE id = ?", (code_or_id,)).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM inventory_items WHERE code = ?", (code_or_id,)
            ).fetchone()
    return _decorate_item(_row(row)) if row else None


def _drug_key(text: object) -> str:
    """Lower-case, punctuation-free, and "500 mg" written as "500mg", so a
    storekeeper's spelling of a strength compares equal to the catalogue's."""
    s = re.sub(r"[^\w/%.+]+", " ", str(text or "").lower())
    s = re.sub(r"(\d)\s+(?=[a-z%])", r"\1", s)
    return " ".join(s.split())


def _product_stem(key: str) -> str:
    """The words before the first strength: "insulin aspart 100units/ml" gives
    "insulin aspart", "augmentin 625 duo tablet" gives "augmentin"."""
    words = []
    for word in key.split():
        if any(ch.isdigit() for ch in word):
            break
        words.append(word)
    return " ".join(words)


def _helix_catalogue_match(code: str, name: str) -> dict | None:
    """The Helix medication this item would duplicate, or None.

    The CHECK on `is_pharmacy` only refuses a row that admits to being a drug,
    and the panel never sends the flag, so "Amoxicillin 500 mg" went straight
    into the stores master while the screen said the database refuses it. This
    compares the item against the hospital's own formulary instead.

    A match is: the code is a Helix medication id; the name is a catalogue name
    or generic name; one name is the other plus more words; or the name is a
    generic or product stem followed by a strength ("Paracetamol 500 mg" with
    only the 650 mg tablet on the formulary). The strength requirement is what
    keeps "Insulin syringe 1 ml" and "Sodium hypochlorite" out of the net.

    Read through helix_service, the module that owns that table, so no SQL in
    this file names a Helix table. A database without the Helix tables (they
    are created at boot) has no catalogue to match; any other read failure
    refuses, because a stores write is never worth a second drug ledger.
    """
    try:
        from services.helix_service import get_medication_catalog

        catalogue = get_medication_catalog()
    except Exception as exc:
        if "no such table" in str(exc).lower():
            logger.warning("[Procurement] Helix catalogue absent, item not screened: %s", exc)
            return None
        raise ProcurementError(
            "could not check this item against the pharmacy catalogue, so it was "
            "not saved. Try again; if it keeps failing, tell the system administrator.",
            code="pharmacy_check_unavailable",
        ) from exc

    item = _drug_key(name)
    code_u = (code or "").strip().upper()
    if not item and not code_u:
        return None
    # Several entries can match one name (brand twins of one generic, a draft
    # beside its confirmed brand), so the refusal names the CLOSEST one, never
    # whichever the catalogue happens to list first. Whether the item is
    # refused does not depend on this: any match refuses it.
    best, best_rank = None, None
    for med in catalogue:
        rank = _catalogue_match_rank(med, item, code_u)
        if rank is not None and (best_rank is None or rank < best_rank):
            best, best_rank = med, rank
            if rank == 0:
                break
    return best


def _catalogue_match_rank(med: dict, item: str, code_u: str) -> int | None:
    """How closely one catalogue entry matches the item, lower is closer, or
    None for no match: 0 the code is its medication id, 1 the name is its
    catalogue name, 2 the name is its generic name, 3 one name is the other
    plus more words, 4 a generic or product stem followed by a strength."""
    if code_u and str(med.get("id") or "").strip().upper() == code_u:
        return 0
    if not item:
        return None
    cat = _drug_key(med.get("name"))
    generic = _drug_key(med.get("generic_name"))
    if cat and item == cat:
        return 1
    if generic and item == generic:
        return 2
    if cat and (item.startswith(cat + " ") or cat.startswith(item + " ")):
        return 3
    for stem in {generic, _product_stem(cat)}:
        # Four characters at least: a short stem would prefix a large,
        # arbitrary slice of the stores catalogue.
        if len(stem) >= 4 and re.match(re.escape(stem) + r"(?:\s*\d|$)", item):
            return 4
    return None


def upsert_inventory_item(
    *,
    code: str,
    name: str,
    category: str = "consumable",
    uom: str = "each",
    pack_size: int = 1,
    hsn_sac: str | None = None,
    gst_treatment: str = "taxable",
    gst_rate_bp: int = 0,
    is_chargeable: bool = False,
    service_item_id: int | None = None,
    reorder_level_qty: int | None = None,
    reorder_qty: int | None = None,
    lead_time_days: int | None = None,
    default_store_id: int | None = None,
    is_active: bool = True,
    notes: str | None = None,
    is_pharmacy: bool = False,
    create_only: bool = False,
) -> dict:
    """Create or update a NON-PHARMACY item, keyed on `code`.

    `create_only` is the Add form: a code already in the master is refused
    (`already_exists`, naming the item) instead of being overwritten.

    `is_pharmacy` exists as a parameter only so that passing it truthy fails
    LOUDLY here, naming Helix, instead of surfacing as an opaque SQLite CHECK
    violation from a layer that cannot explain itself. There is no code path
    that writes a 1 into that column, and the CHECK guarantees there never will
    be. GST DEFAULTS TO `taxable` for this master — the opposite of the service
    master's `exempt` default — because a hospital's SERVICES are GST-exempt in
    India while the gloves, reagents and implants it buys are not.
    """
    with db_session() as db:
        return _upsert_inventory_item_core(
            db,
            code=code,
            name=name,
            category=category,
            uom=uom,
            pack_size=pack_size,
            hsn_sac=hsn_sac,
            gst_treatment=gst_treatment,
            gst_rate_bp=gst_rate_bp,
            is_chargeable=is_chargeable,
            service_item_id=service_item_id,
            reorder_level_qty=reorder_level_qty,
            reorder_qty=reorder_qty,
            lead_time_days=lead_time_days,
            default_store_id=default_store_id,
            is_active=is_active,
            notes=notes,
            is_pharmacy=is_pharmacy,
            create_only=create_only,
        )


def _upsert_inventory_item_core(
    db,
    *,
    code: str,
    name: str,
    category: str = "consumable",
    uom: str = "each",
    pack_size: int = 1,
    hsn_sac: str | None = None,
    gst_treatment: str = "taxable",
    gst_rate_bp: int = 0,
    is_chargeable: bool = False,
    service_item_id: int | None = None,
    reorder_level_qty: int | None = None,
    reorder_qty: int | None = None,
    lead_time_days: int | None = None,
    default_store_id: int | None = None,
    is_active: bool = True,
    notes: str | None = None,
    is_pharmacy: bool = False,
    create_only: bool = False,
) -> dict:
    """`upsert_inventory_item` on the caller's connection.

    The Helix catalogue screen runs HERE, inside the core, so no caller can
    reach the item master without it. It reads the catalogue through
    helix_service, which takes its own (nested, read-only) session."""
    if is_pharmacy:
        # The drug's schedule and NDPS class are not Helix's: they are recorded
        # in the Pharmacy store's drug master, which buys and receives drugs
        # when that module is switched on (grn-18 corrected the old wording).
        raise PharmacyItemRefused(
            "drugs are not stocked here. Medication stock lives in Helix "
            "(pharmacy), which tracks batch, expiry and dispensing against a "
            "prescription; where the Pharmacy store is switched on, drugs are "
            "bought, received and classified (schedule and NDPS class) there. "
            "Two ledgers for the same drug is how two systems end up "
            "disagreeing about how much of a controlled substance exists. Add "
            "the medication in Helix instead."
        )
    code = _clean(code, field_name="item code", required=True).upper()
    name = _clean(name, field_name="item name", required=True)
    med = _helix_catalogue_match(code, name)
    if med:
        med_name = med.get("name") or med.get("id")
        raise PharmacyItemRefused(
            f'"{name}" matches the medication "{med_name}" in Helix '
            "(pharmacy), so it was not added. Medicines are stocked and dispensed "
            "in Helix, which tracks batch, expiry and dispensing against a "
            "prescription; a second stock record here would let the two disagree "
            "about how much of the drug exists. If this is not a medicine (for "
            "example a syringe or a device), name it by what it is, for example "
            "'Syringe, insulin, 1 ml'."
        )
    category = (category or "consumable").strip().lower()
    if category not in ITEM_CATEGORIES:
        raise ProcurementError(
            f"unknown item category {category!r}; expected one of {list(ITEM_CATEGORIES)}. "
            "Medications are not one of them, they belong to Helix (pharmacy).",
            code="bad_category",
        )
    treatment = _gst_treatment(gst_treatment)
    rate_bp = int(gst_rate_bp or 0)
    if rate_bp < 0:
        raise ProcurementError("gst_rate_bp must not be negative", code="bad_gst_rate")
    if int(pack_size or 1) < 1:
        raise ProcurementError("pack_size must be at least 1", code="bad_pack_size")
    for label, value in (
        ("reorder_level_qty", reorder_level_qty),
        ("reorder_qty", reorder_qty),
        ("lead_time_days", lead_time_days),
    ):
        if value is not None and int(value) < 0:
            raise ProcurementError(f"{label} must not be negative", code="bad_quantity")

    if create_only:
        _refuse_existing(db, "inventory_items", code, "item")
    db.execute(
        """INSERT INTO inventory_items (code, name, category, uom, pack_size,
               hsn_sac, gst_treatment, gst_rate_bp, is_chargeable, service_item_id,
               reorder_level_qty, reorder_qty, lead_time_days, default_store_id,
               is_pharmacy, is_active, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
           ON CONFLICT(code) DO UPDATE SET
               name = excluded.name,
               category = excluded.category,
               uom = excluded.uom,
               pack_size = excluded.pack_size,
               hsn_sac = COALESCE(excluded.hsn_sac, hsn_sac),
               gst_treatment = excluded.gst_treatment,
               gst_rate_bp = excluded.gst_rate_bp,
               is_chargeable = excluded.is_chargeable,
               service_item_id = COALESCE(excluded.service_item_id, service_item_id),
               reorder_level_qty = COALESCE(excluded.reorder_level_qty, reorder_level_qty),
               reorder_qty = COALESCE(excluded.reorder_qty, reorder_qty),
               lead_time_days = COALESCE(excluded.lead_time_days, lead_time_days),
               default_store_id = COALESCE(excluded.default_store_id, default_store_id),
               is_active = excluded.is_active,
               notes = COALESCE(excluded.notes, notes),
               updated_at = CURRENT_TIMESTAMP""",
        (
            code,
            name,
            category,
            (uom or "each").strip(),
            int(pack_size or 1),
            hsn_sac,
            treatment,
            rate_bp,
            1 if is_chargeable else 0,
            service_item_id,
            reorder_level_qty,
            reorder_qty,
            lead_time_days,
            default_store_id,
            1 if is_active else 0,
            notes,
        ),
    )
    row = db.execute("SELECT * FROM inventory_items WHERE code = ?", (code,)).fetchone()
    return _decorate_item(_row(row))


def _resolve_item(db, item_code: str) -> dict | None:
    row = db.execute("SELECT * FROM inventory_items WHERE code = ?", (item_code,)).fetchone()
    return _row(row) if row else None


# ═════════════════════════════════════════════════════════════════════════════
#  Indents  (a department asks the store for things)
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class IndentLineInput:
    item_code: str
    quantity_milli: int
    description: str | None = None
    uom: str | None = None
    remarks: str | None = None


def _coerce_indent_lines(lines: Sequence[Any]) -> list[IndentLineInput]:
    out: list[IndentLineInput] = []
    for raw in lines or []:
        if isinstance(raw, IndentLineInput):
            out.append(raw)
            continue
        if not isinstance(raw, dict):
            raise ProcurementError("each indent line must be an object", code="bad_line")
        out.append(
            IndentLineInput(
                item_code=str(raw.get("item_code") or "").strip().upper(),
                quantity_milli=assert_positive_quantity_milli(
                    raw.get("quantity_milli"), "line quantity_milli"
                ),
                description=raw.get("description"),
                uom=raw.get("uom"),
                remarks=raw.get("remarks"),
            )
        )
    if not out:
        raise ProcurementError("an indent needs at least one line", code="empty_document")
    return out


def create_indent(
    *,
    store_code: str | None = None,
    department: str | None = None,
    ward: str | None = None,
    priority: str = "routine",
    required_by: date | str | None = None,
    lines: Sequence[Any],
    requested_by: str | None = None,
    remarks: str | None = None,
    submit: bool = False,
) -> dict:
    """Raise an indent. Starts in `draft` unless `submit=True`.

    An indent line for an item that is not in the master is REFUSED rather than
    accepted as free text: a store cannot issue what it does not stock, and an
    indent full of invented item names is how a purchase department ends up
    buying three spellings of the same glove.
    """
    with db_session(immediate=True) as db:
        indent_id = _create_indent_core(
            db,
            store_code=store_code,
            department=department,
            ward=ward,
            priority=priority,
            required_by=required_by,
            lines=lines,
            requested_by=requested_by,
            remarks=remarks,
            submit=submit,
        )
    if submit:
        # Numbers are drawn at SUBMIT, never at draft creation: a draft somebody
        # abandons must not take a number out of a series.
        _stamp_document_number("indent", indent_id)
    return get_indent(indent_id) or {}


def _create_indent_core(
    db,
    *,
    store_code: str | None = None,
    department: str | None = None,
    ward: str | None = None,
    priority: str = "routine",
    required_by: date | str | None = None,
    lines: Sequence[Any],
    requested_by: str | None = None,
    remarks: str | None = None,
    submit: bool = False,
    domain: str = STORES_DOMAIN,
    indent_kind: str = "purchase",
    issuing_store_code: str | None = None,
    resolve_line=None,
) -> int:
    """`create_indent` on the caller's connection. Returns the new indent id.

    It draws no number: a caller that passes `submit=True` stamps the indent
    number after its transaction commits, as `create_indent` does.

    domain='pharmacy' (see the Domains note) resolves each line through the
    caller's `resolve_line` and needs pharmacy stores. An issue indent (asking
    another store to hand stock over) is a pharmacy document only and names
    the pharmacy store it asks, `issuing_store_code`; the stores desk raises
    purchase indents, as it always has."""
    _assert_domain(domain)
    if priority not in ("routine", "urgent", "emergency"):
        raise ProcurementError(f"unknown priority {priority!r}", code="bad_priority")
    if indent_kind not in INDENT_KINDS:
        raise ProcurementError(f"unknown indent kind {indent_kind!r}", code="bad_indent_kind")
    if domain == STORES_DOMAIN and (indent_kind != "purchase" or issuing_store_code):
        raise ProcurementError(
            "an issue indent is a pharmacy store document; a stores indent asks for a purchase",
            code="bad_indent_kind",
        )
    parsed = _coerce_indent_lines(lines)
    required = _as_date_str(required_by, "required_by")

    store_id = None
    if store_code:
        store = _store_row(db, store_code, domain)
        if not store:
            raise ProcurementError(f"no store with code {store_code!r}", code="not_found")
        store_id = store["id"]

    issuing_store_id = None
    if indent_kind == "issue":
        issuing = _store_row(db, issuing_store_code, domain) if issuing_store_code else None
        if not issuing:
            raise ProcurementError(
                "an issue indent must name the pharmacy store it asks for stock",
                code="not_found",
            )
        if issuing["id"] == store_id:
            raise ProcurementError(
                "a store cannot raise an issue indent on itself", code="bad_indent_kind"
            )
        issuing_store_id = issuing["id"]

    resolved: list[tuple[IndentLineInput, dict]] = []
    for line in parsed:
        assert_positive_quantity_milli(line.quantity_milli, "line quantity_milli")
        if domain == PHARMACY_DOMAIN:
            item: dict | None
            item = _pharmacy_line_item(db, line.item_code, resolve_line)
        else:
            item = _resolve_item(db, line.item_code)
        if not item:
            raise ProcurementError(
                f"no non-pharmacy item with code {line.item_code!r}. If it is "
                "a medication it belongs to Helix (pharmacy), not to stores.",
                code="unknown_item",
            )
        resolved.append((line, item))

    status = "submitted" if submit else "draft"
    cur = db.execute(
        """INSERT INTO indents (indent_number, store_id, department, ward,
               priority, status, required_by, requested_by, remarks, domain,
               indent_kind, issuing_store_id)
           VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            store_id,
            department,
            ward,
            priority,
            status,
            required,
            requested_by,
            remarks,
            domain,
            indent_kind,
            issuing_store_id,
        ),
    )
    indent_id = cur.lastrowid
    for line_no, (line, item) in enumerate(resolved, start=1):
        db.execute(
            f"""INSERT INTO indent_lines (indent_id, line_no, inventory_item_id,
                   item_code, description, uom, quantity_milli, status, remarks,
                   {_DRUG_ID_COLUMN}, units_per_pack)
               VALUES (?,?,?,?,?,?,?, 'pending', ?, ?, ?)""",
            (
                indent_id,
                line_no,
                item["id"],
                item["code"],
                line.description or item["name"],
                line.uom or item.get("uom"),
                line.quantity_milli,
                line.remarks,
                item.get(_DRUG_ID_COLUMN),
                item.get("units_per_pack"),
            ),
        )
    return int(indent_id)


def submit_indent(indent_id: int, *, actor: str | None = None) -> dict:
    with db_session(immediate=True) as db:
        _submit_indent_core(db, indent_id, actor=actor)
    _stamp_document_number("indent", indent_id)
    return get_indent(indent_id) or {}


def _submit_indent_core(
    db, indent_id: int, *, actor: str | None = None, domain: str = STORES_DOMAIN
) -> None:
    """`submit_indent` on the caller's connection. The caller stamps the number
    after commit. Another desk's indent is not found."""
    row = _document_in_domain(db, "indents", indent_id, domain, "indent")
    assert_transition("indent", row["status"], "submitted")
    db.execute(
        "UPDATE indents SET status = 'submitted', requested_by = COALESCE(?, requested_by), "
        "requested_at = ? WHERE id = ?",
        (actor, _now(), indent_id),
    )


def decide_indent(
    indent_id: int,
    *,
    approvals: dict[int, int] | None = None,
    reject: bool = False,
    reason: str | None = None,
    actor: str | None = None,
) -> dict:
    """Approve (in whole or in part) or reject a submitted indent.

    `approvals` maps `line_no -> approved_quantity_milli`. A line not named is
    approved IN FULL; a line approved for 0 is rejected. The resulting document
    status is DERIVED from the lines, never asserted by the caller:

        every line approved in full   → 'approved'
        some line cut or zeroed       → 'partially_approved'
        every line zeroed, or reject  → 'rejected'

    Approving less than was asked for is the normal case in a hospital store and
    it must be recorded as a NUMBER, not as a note: the purchase order is raised
    off the approved quantity, and the gap between asked and approved is what a
    ward sister argues about at the month end.
    """
    with db_session(immediate=True) as db:
        _decide_indent_core(
            db,
            indent_id,
            approvals=approvals,
            reject=reject,
            reason=reason,
            actor=actor,
        )
    return get_indent(indent_id) or {}


def _decide_indent_core(
    db,
    indent_id: int,
    *,
    approvals: dict[int, int] | None = None,
    reject: bool = False,
    reason: str | None = None,
    actor: str | None = None,
    domain: str = STORES_DOMAIN,
) -> None:
    """`decide_indent` on the caller's connection; see it for the rules.

    The reject branch used to re-read the indent through `get_indent` INSIDE
    the immediate transaction. That nested read is gone from the core: the
    wrapper re-reads after the commit, which is the same document on a pooled
    connection and, on an unpooled one, the committed rejection rather than the
    pre-rejection row a second connection could see. Another desk's indent is
    not found."""
    row = _document_in_domain(db, "indents", indent_id, domain, "indent")
    lines = db.execute(
        "SELECT * FROM indent_lines WHERE indent_id = ? ORDER BY line_no",
        (indent_id,),
    ).fetchall()
    if reject:
        assert_transition("indent", row["status"], "rejected")
        if not (reason or "").strip():
            raise ProcurementError("a rejection needs a reason", code="missing_field")
        db.execute(
            "UPDATE indents SET status = 'rejected', rejected_reason = ?, "
            "approved_by = ?, approved_at = ? WHERE id = ?",
            ((reason or "").strip(), actor, _now(), indent_id),
        )
        db.execute(
            "UPDATE indent_lines SET status = 'rejected', approved_quantity_milli = 0 "
            "WHERE indent_id = ?",
            (indent_id,),
        )
        return

    approvals = {int(k): v for k, v in (approvals or {}).items()}
    known = {int(line["line_no"]) for line in lines}
    unknown = set(approvals) - known
    if unknown:
        raise ProcurementError(
            f"indent {indent_id} has no line(s) {sorted(unknown)}", code="bad_line"
        )

    any_cut = False
    all_zero = True
    for line in lines:
        requested = int(line["quantity_milli"])
        approved = approvals.get(int(line["line_no"]), requested)
        approved = assert_quantity_milli(approved, "approved_quantity_milli")
        if approved < 0:
            raise ProcurementError("approved quantity must not be negative", code="bad_quantity")
        if approved > requested:
            raise ProcurementError(
                f"line {line['line_no']}: cannot approve {format_quantity(approved)} "
                f"against a request for {format_quantity(requested)}: raise a new "
                "indent for the extra rather than inflating this one",
                code="over_approval",
            )
        if approved < requested:
            any_cut = True
        if approved > 0:
            all_zero = False
        db.execute(
            "UPDATE indent_lines SET approved_quantity_milli = ?, status = ? WHERE id = ?",
            (approved, "approved" if approved > 0 else "rejected", line["id"]),
        )

    target = "rejected" if all_zero else ("partially_approved" if any_cut else "approved")
    assert_transition("indent", row["status"], target)
    db.execute(
        "UPDATE indents SET status = ?, approved_by = ?, approved_at = ?, "
        "rejected_reason = COALESCE(?, rejected_reason) WHERE id = ?",
        (target, actor, _now(), (reason or "").strip() or None, indent_id),
    )


def cancel_indent(indent_id: int, *, reason: str | None = None, actor: str | None = None) -> dict:
    with db_session(immediate=True) as db:
        _cancel_indent_core(db, indent_id, reason=reason)
    logger.info("indent %s cancelled by %s", indent_id, actor)
    return get_indent(indent_id) or {}


def _cancel_indent_core(
    db, indent_id: int, *, reason: str | None = None, domain: str = STORES_DOMAIN
) -> None:
    """`cancel_indent` on the caller's connection. Another desk's indent is
    not found."""
    row = _document_in_domain(db, "indents", indent_id, domain, "indent")
    assert_transition("indent", row["status"], "cancelled")
    db.execute(
        "UPDATE indents SET status = 'cancelled', rejected_reason = COALESCE(?, rejected_reason) "
        "WHERE id = ?",
        ((reason or "").strip() or None, indent_id),
    )
    db.execute(
        "UPDATE indent_lines SET status = 'cancelled' WHERE indent_id = ? "
        "AND status NOT IN ('received','ordered')",
        (indent_id,),
    )


def _record_indent_issue_core(
    db, indent_id: int, *, issued: dict[int, int], domain: str = STORES_DOMAIN
) -> None:
    """Record stock handed over against an approved ISSUE indent (grn-21,
    the pharmacy store's ward indents), on the caller's connection.

    `issued` maps indent_lines.id to the quantity (milli) that has just left
    the issuing store for it. Each line's issued total may never pass what was
    approved for it, and the indent moves to 'converted' (its stock has been
    sent) the first time anything is issued. Only an issue indent is issued;
    a purchase indent becomes a purchase order instead. There is no public
    wrapper: the pharmacy transfer service calls this inside the dispatch's
    own immediate transaction. Another desk's indent is not found."""
    row = _document_in_domain(db, "indents", indent_id, domain, "indent")
    if row["indent_kind"] != "issue":
        raise ProcurementError(
            "only an issue indent is issued from a store", code="bad_indent_kind"
        )
    if row["status"] != "converted":
        assert_transition("indent", row["status"], "converted")
    lines = {
        int(r["id"]): r
        for r in db.execute(
            "SELECT * FROM indent_lines WHERE indent_id = ?", (indent_id,)
        ).fetchall()
    }
    for line_id, quantity in sorted(issued.items()):
        line = lines.get(int(line_id))
        if line is None:
            raise ProcurementError(f"indent {indent_id} has no line {line_id}", code="bad_line")
        qty = assert_positive_quantity_milli(quantity, "issued quantity_milli")
        already = int(line["issued_quantity_milli"] or 0)
        approved = int(line["approved_quantity_milli"] or 0)
        if already + qty > approved:
            raise ProcurementError(
                f"line {line['line_no']}: cannot issue {format_quantity(already + qty)} "
                f"against an approval for {format_quantity(approved)}",
                code="over_issue",
            )
        db.execute(
            "UPDATE indent_lines SET issued_quantity_milli = ?, status = 'ordered' WHERE id = ?",
            (already + qty, line["id"]),
        )
    if row["status"] != "converted":
        db.execute("UPDATE indents SET status = 'converted' WHERE id = ?", (indent_id,))


def _close_indent_core(db, indent_id: int, *, domain: str = STORES_DOMAIN) -> None:
    """Close an issue indent whose stock has all been received (grn-21), on
    the caller's connection. Lines that were issued read as received. Another
    desk's indent is not found."""
    row = _document_in_domain(db, "indents", indent_id, domain, "indent")
    assert_transition("indent", row["status"], "closed")
    db.execute("UPDATE indents SET status = 'closed' WHERE id = ?", (indent_id,))
    db.execute(
        "UPDATE indent_lines SET status = 'received' WHERE indent_id = ? AND status = 'ordered'",
        (indent_id,),
    )


def get_indent(indent_id: int) -> dict | None:
    with db_session() as db:
        return _get_indent_core(db, indent_id)


def _get_indent_core(db, indent_id: int, *, domain: str = STORES_DOMAIN) -> dict | None:
    """`get_indent` on the caller's connection, so a caller can answer with the
    document from inside its own transaction. None for another desk's indent."""
    _assert_domain(domain)
    row = db.execute(
        """SELECT i.*, s.code AS store_code, s.name AS store_name
             FROM indents i LEFT JOIN stores s ON s.id = i.store_id
            WHERE i.id = ? AND i.domain = ?""",
        (indent_id, domain),
    ).fetchone()
    if not row:
        return None
    doc = _row(row)
    doc["lines"] = [
        _row(r)
        for r in db.execute(
            "SELECT * FROM indent_lines WHERE indent_id = ? ORDER BY line_no",
            (indent_id,),
        ).fetchall()
    ]
    for line in doc["lines"]:
        requested = int(line["quantity_milli"])
        approved = line.get("approved_quantity_milli")
        line["quantity_display"] = format_quantity(requested, line.get("uom"))
        line["approved_quantity_display"] = format_quantity(
            approved if approved is not None else None, line.get("uom")
        )
        # Effective = what a PO may be raised for. Before a decision, nothing is
        # approved, so it is None — NOT the requested quantity, which would let a
        # PO be raised off an indent nobody signed.
        line["effective_quantity_milli"] = approved
    doc["legal_transitions"] = sorted(INDENT_TRANSITIONS.get(doc["status"], ()))
    return doc


def list_indents(
    *,
    status: str | None = None,
    store_code: str | None = None,
    department: str | None = None,
    limit: int = 100,
    domain: str = STORES_DOMAIN,
) -> list[dict]:
    """One desk's indents (the stores desk's unless `domain` says otherwise)."""
    _assert_domain(domain)
    sql = (
        "SELECT i.*, s.code AS store_code, s.name AS store_name, "
        "(SELECT COUNT(*) FROM indent_lines l WHERE l.indent_id = i.id) AS line_count "
        "FROM indents i LEFT JOIN stores s ON s.id = i.store_id WHERE i.domain = ?"
    )
    params: list[Any] = [domain]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    if store_code:
        sql += " AND s.code = ?"
        params.append(store_code.upper())
    if department:
        sql += " AND i.department = ?"
        params.append(department)
    sql += " ORDER BY i.requested_at DESC, i.id DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        return [_row(r) for r in db.execute(sql, params).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
#  Purchase orders
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class POLineInput:
    item_code: str
    quantity_milli: int
    unit_rate_paise: int
    description: str | None = None
    uom: str | None = None
    discount_paise: int = 0
    discount_bp: int = 0
    gst_treatment: str | None = None
    gst_rate_bp: int | None = None
    indent_line_id: int | None = None
    remarks: str | None = None


def _coerce_po_lines(lines: Sequence[Any]) -> list[POLineInput]:
    out: list[POLineInput] = []
    for raw in lines or []:
        if isinstance(raw, POLineInput):
            out.append(raw)
            continue
        if not isinstance(raw, dict):
            raise ProcurementError("each PO line must be an object", code="bad_line")
        out.append(
            POLineInput(
                item_code=str(raw.get("item_code") or "").strip().upper(),
                quantity_milli=assert_positive_quantity_milli(
                    raw.get("quantity_milli"), "line quantity_milli"
                ),
                unit_rate_paise=assert_paise(raw.get("unit_rate_paise")),
                description=raw.get("description"),
                uom=raw.get("uom"),
                discount_paise=int(raw.get("discount_paise") or 0),
                discount_bp=int(raw.get("discount_bp") or 0),
                gst_treatment=raw.get("gst_treatment"),
                gst_rate_bp=raw.get("gst_rate_bp"),
                indent_line_id=raw.get("indent_line_id"),
                remarks=raw.get("remarks"),
            )
        )
    if not out:
        raise ProcurementError("a purchase order needs at least one line", code="empty_document")
    return out


def create_purchase_order(
    *,
    vendor_code: str,
    lines: Sequence[Any],
    store_code: str | None = None,
    indent_id: int | None = None,
    order_date: date | str | None = None,
    expected_date: date | str | None = None,
    place_of_supply: str | None = None,
    is_interstate: bool = False,
    other_charges_paise: int = 0,
    terms: str | None = None,
    remarks: str | None = None,
    created_by: str | None = None,
) -> dict:
    """Raise a purchase order in `draft`.

    INTERSTATE IS AN EXPLICIT INPUT, NOT A GUESS. CGST+SGST vs IGST turns on
    whether the place of supply is the hospital's own state, and Orb has no
    configured "home state" to compare a vendor's `state_code` against. Guessing
    it would put the wrong tax heads on a real GST document. So the buyer states
    it, the vendor's state code is shown next to the switch, and the default is
    intra-state because most hospital purchasing is local.
    """
    with db_session(immediate=True) as db:
        po_id = _create_purchase_order_core(
            db,
            vendor_code=vendor_code,
            lines=lines,
            store_code=store_code,
            indent_id=indent_id,
            order_date=order_date,
            expected_date=expected_date,
            place_of_supply=place_of_supply,
            is_interstate=is_interstate,
            other_charges_paise=other_charges_paise,
            terms=terms,
            remarks=remarks,
            created_by=created_by,
            today=date.today(),
        )
    return get_purchase_order(po_id) or {}


def _create_purchase_order_core(
    db,
    *,
    vendor_code: str,
    lines: Sequence[Any],
    store_code: str | None = None,
    indent_id: int | None = None,
    order_date: date | str | None = None,
    expected_date: date | str | None = None,
    place_of_supply: str | None = None,
    is_interstate: bool = False,
    other_charges_paise: int = 0,
    terms: str | None = None,
    remarks: str | None = None,
    created_by: str | None = None,
    today: date | datetime | str,
    domain: str = STORES_DOMAIN,
    resolve_line=None,
) -> int:
    """`create_purchase_order` on the caller's connection. Returns the new PO id.

    `today` is the order date used when the caller gives none; it is required
    so the caller decides which clock that is (see `_calendar_today`).

    domain='pharmacy' (see the Domains note) resolves each line through the
    caller's `resolve_line`, snapshots the drug's units per pack on the line,
    needs a pharmacy store and a pharmacy purchase indent, and refuses a line
    whose GST rate is not configured rather than taxing it at 0 per cent. The
    vendor's drug licence and the drug's classification (GRN-R10, GRN-R18) are
    the pharmacy service's checks, made before it calls this."""
    _assert_domain(domain)
    default_order_date = _calendar_today(today).isoformat()
    parsed = _coerce_po_lines(lines)
    vendor = db.execute("SELECT * FROM vendors WHERE code = ?", (vendor_code.upper(),)).fetchone()
    if not vendor:
        raise ProcurementError(f"no vendor with code {vendor_code!r}", code="not_found")
    if vendor["is_blacklisted"]:
        raise ProcurementError(
            f"vendor {vendor['name']} is blacklisted"
            + (f": {vendor['blacklist_reason']}" if vendor["blacklist_reason"] else ""),
            code="vendor_blacklisted",
        )
    if not vendor["is_active"]:
        raise ProcurementError(f"vendor {vendor['name']} is inactive", code="vendor_inactive")

    store_id = None
    if store_code:
        store = _store_row(db, store_code, domain)
        if not store:
            raise ProcurementError(f"no store with code {store_code!r}", code="not_found")
        store_id = store["id"]

    if indent_id is not None:
        indent = _document_in_domain(db, "indents", indent_id, domain, "indent")
        if indent["status"] not in ("approved", "partially_approved", "converted"):
            raise ProcurementError(
                f"indent {indent_id} is {indent['status']!r}; a purchase order may "
                "only be raised against an APPROVED indent",
                code="indent_not_approved",
            )
        if indent["indent_kind"] != "purchase":
            raise ProcurementError(
                f"indent {indent_id} asks another store for stock; it is filled by a "
                "transfer, not a purchase order",
                code="indent_is_issue",
            )

    computed: list[tuple[POLineInput, dict, Any]] = []
    for line in parsed:
        assert_positive_quantity_milli(line.quantity_milli, "line quantity_milli")
        rate = assert_paise(line.unit_rate_paise)
        if rate < 0:
            raise ProcurementError(
                "a purchase order line rate must not be negative", code="bad_rate"
            )
        if domain == PHARMACY_DOMAIN:
            item: dict | None
            item = _pharmacy_line_item(db, line.item_code, resolve_line)
            if line.gst_rate_bp is None and item.get("gst_rate_bp") is None:
                raise ProcurementError(
                    f"{item['code']} has no GST rate configured. Set it on the drug "
                    "before ordering; an unset rate is not 0 per cent.",
                    code="gst_not_configured",
                )
        else:
            item = _resolve_item(db, line.item_code)
        if not item:
            raise ProcurementError(
                f"no non-pharmacy item with code {line.item_code!r}",
                code="unknown_item",
            )
        treatment = _gst_treatment(line.gst_treatment or item.get("gst_treatment"))
        rate_bp = int(
            line.gst_rate_bp if line.gst_rate_bp is not None else item["gst_rate_bp"] or 0
        )
        totals = compute_line(
            unit_rate_paise=rate,
            quantity_milli=line.quantity_milli,
            discount_paise=line.discount_paise,
            discount_bp=line.discount_bp,
            gst_rate_bp=rate_bp,
            gst_treatment=treatment,
            interstate=bool(is_interstate),
        )
        computed.append((line, item, totals))

    # Foot the lines WITHOUT the round-off, add other charges, THEN round
    # once. Rounding before the freight line would leave a printed total
    # that is not a whole rupee, which is the one thing the Indian
    # "Round Off" line exists to prevent.
    foot = compute_invoice([t for _, _, t in computed], round_off_to_rupee=False)
    other = assert_paise(other_charges_paise or 0)
    # Other charges (freight, packing) are NOT taxed here. Freight on a
    # taxable supply is legally part of the taxable value, but which charges
    # attract which rate is a per-vendor decision Orb has no basis to make,
    # so it is carried as a separate, visible amount rather than silently
    # taxed at the last line's rate.
    before_rounding = foot.total_paise + other
    payable = round_to_rupee(before_rounding)
    rounding_adjustment = payable - before_rounding

    cur = db.execute(
        """INSERT INTO purchase_orders (po_number, vendor_id, indent_id, store_id,
               status, order_date, expected_date, place_of_supply, is_interstate,
               subtotal_paise, discount_paise, taxable_value_paise, cgst_paise,
               sgst_paise, igst_paise, tax_paise, other_charges_paise,
               rounding_adjustment_paise, total_paise, terms, created_by, remarks,
               domain)
           VALUES (NULL,?,?,?, 'draft', ?,?,?,?, ?,?,?,?,?,?,?,?,?,?, ?,?,?, ?)""",
        (
            vendor["id"],
            indent_id,
            store_id,
            _as_date_str(order_date, "order_date") or default_order_date,
            _as_date_str(expected_date, "expected_date"),
            place_of_supply or vendor["state_code"],
            1 if is_interstate else 0,
            foot.subtotal_paise,
            foot.discount_paise,
            foot.taxable_value_paise,
            foot.cgst_paise,
            foot.sgst_paise,
            foot.igst_paise,
            foot.tax_paise,
            other,
            rounding_adjustment,
            payable,
            terms,
            created_by,
            remarks,
            domain,
        ),
    )
    po_id = cur.lastrowid
    for line_no, (line, item, totals) in enumerate(computed, start=1):
        db.execute(
            f"""INSERT INTO purchase_order_lines (purchase_order_id, line_no,
                   indent_line_id, inventory_item_id, item_code, description,
                   hsn_sac, uom, quantity_milli, received_quantity_milli,
                   unit_rate_paise, gross_paise, discount_paise, gst_treatment,
                   gst_rate_bp, taxable_value_paise, cgst_paise, sgst_paise,
                   igst_paise, tax_paise, total_paise, status, remarks,
                   {_DRUG_ID_COLUMN}, units_per_pack)
               VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?, 'open', ?, ?, ?)""",
            (
                po_id,
                line_no,
                line.indent_line_id,
                item["id"],
                item["code"],
                line.description or item["name"],
                item.get("hsn_sac"),
                line.uom or item.get("uom"),
                line.quantity_milli,
                line.unit_rate_paise,
                totals.gross_paise,
                totals.discount_paise,
                totals.gst_treatment,
                totals.gst_rate_bp,
                totals.taxable_value_paise,
                totals.cgst_paise,
                totals.sgst_paise,
                totals.igst_paise,
                totals.tax_paise,
                totals.total_paise,
                line.remarks,
                item.get(_DRUG_ID_COLUMN),
                item.get("units_per_pack"),
            ),
        )
    if indent_id is not None:
        db.execute(
            "UPDATE indent_lines SET status = 'ordered' WHERE indent_id = ? "
            "AND status = 'approved'",
            (indent_id,),
        )
        indent_status = db.execute(
            "SELECT status FROM indents WHERE id = ?", (indent_id,)
        ).fetchone()["status"]
        if indent_status in ("approved", "partially_approved"):
            assert_transition("indent", indent_status, "converted")
            db.execute("UPDATE indents SET status = 'converted' WHERE id = ?", (indent_id,))
    return int(po_id)


def _move_po(
    po_id: int, target: str, *, actor: str | None = None, reason: str | None = None
) -> dict:
    with db_session(immediate=True) as db:
        _move_po_core(db, po_id, target, actor=actor, reason=reason)
    if target == "issued":
        # The number is drawn at ISSUE — the moment the document leaves the
        # building and becomes something a vendor quotes back at you.
        _stamp_document_number("purchase_order", po_id)
    return get_purchase_order(po_id) or {}


def _move_po_core(
    db,
    po_id: int,
    target: str,
    *,
    actor: str | None = None,
    reason: str | None = None,
    domain: str = STORES_DOMAIN,
) -> None:
    """`_move_po` on the caller's connection. It draws no number: a caller that
    issues a PO stamps the PO number after its transaction commits. Another
    desk's purchase order is not found."""
    row = _document_in_domain(db, "purchase_orders", po_id, domain, "purchase order")
    assert_transition("purchase_order", row["status"], target)

    if target == "cancelled":
        received = db.execute(
            "SELECT COALESCE(SUM(received_quantity_milli),0) AS q "
            "FROM purchase_order_lines WHERE purchase_order_id = ?",
            (po_id,),
        ).fetchone()["q"]
        if received:
            raise ProcurementError(
                "goods have already been received against this order; it cannot "
                "be cancelled. Short-close the outstanding lines instead, so the "
                "receipt that did happen stays on the record.",
                code="already_received",
            )
        if not (reason or "").strip():
            raise ProcurementError("a cancellation needs a reason", code="missing_field")
        db.execute(
            "UPDATE purchase_orders SET status = 'cancelled', cancelled_by = ?, "
            "cancelled_at = ?, cancel_reason = ? WHERE id = ?",
            (actor, _now(), (reason or "").strip(), po_id),
        )
        db.execute(
            "UPDATE purchase_order_lines SET status = 'cancelled' "
            "WHERE purchase_order_id = ? AND status = 'open'",
            (po_id,),
        )
    elif target == "approved":
        db.execute(
            "UPDATE purchase_orders SET status = 'approved', approved_by = ?, "
            "approved_at = ? WHERE id = ?",
            (actor, _now(), po_id),
        )
    elif target == "issued":
        db.execute(
            "UPDATE purchase_orders SET status = 'issued', issued_at = ? WHERE id = ?",
            (_now(), po_id),
        )
    else:
        db.execute("UPDATE purchase_orders SET status = ? WHERE id = ?", (target, po_id))


def submit_purchase_order(po_id: int, *, actor: str | None = None) -> dict:
    return _move_po(po_id, "pending_approval", actor=actor)


def approve_purchase_order(po_id: int, *, actor: str | None = None) -> dict:
    """Approve a PO. SEPARATION OF DUTIES is a route-layer concern (the approval
    endpoint carries a stricter permission than the create endpoint); this
    function records who did it so an audit can see both names."""
    return _move_po(po_id, "approved", actor=actor)


def issue_purchase_order(po_id: int, *, actor: str | None = None) -> dict:
    return _move_po(po_id, "issued", actor=actor)


def cancel_purchase_order(po_id: int, *, reason: str, actor: str | None = None) -> dict:
    return _move_po(po_id, "cancelled", actor=actor, reason=reason)


def _submit_purchase_order_core(
    db, po_id: int, *, actor: str | None = None, domain: str = STORES_DOMAIN
) -> None:
    """`submit_purchase_order` on the caller's connection."""
    _move_po_core(db, po_id, "pending_approval", actor=actor, domain=domain)


def _approve_purchase_order_core(
    db, po_id: int, *, actor: str | None = None, domain: str = STORES_DOMAIN
) -> None:
    """`approve_purchase_order` on the caller's connection. Like the wrapper it
    records the approver and does not compare it with the creator; a caller
    that needs separation of duties checks it before calling."""
    _move_po_core(db, po_id, "approved", actor=actor, domain=domain)


def _issue_purchase_order_core(
    db, po_id: int, *, actor: str | None = None, domain: str = STORES_DOMAIN
) -> None:
    """`issue_purchase_order` on the caller's connection. The caller stamps the
    PO number after its transaction commits."""
    _move_po_core(db, po_id, "issued", actor=actor, domain=domain)


def _cancel_purchase_order_core(
    db,
    po_id: int,
    *,
    reason: str,
    actor: str | None = None,
    domain: str = STORES_DOMAIN,
) -> None:
    """`cancel_purchase_order` on the caller's connection."""
    _move_po_core(db, po_id, "cancelled", actor=actor, reason=reason, domain=domain)


def get_purchase_order(po_id: int) -> dict | None:
    with db_session() as db:
        return _get_purchase_order_core(db, po_id)


def _get_purchase_order_core(db, po_id: int, *, domain: str = STORES_DOMAIN) -> dict | None:
    """`get_purchase_order` on the caller's connection. None for another
    desk's purchase order."""
    _assert_domain(domain)
    row = db.execute(
        """SELECT p.*, v.code AS vendor_code, v.name AS vendor_name,
                  v.gstin AS vendor_gstin, v.state_code AS vendor_state_code,
                  s.code AS store_code, s.name AS store_name
             FROM purchase_orders p
             LEFT JOIN vendors v ON v.id = p.vendor_id
             LEFT JOIN stores s ON s.id = p.store_id
            WHERE p.id = ? AND p.domain = ?""",
        (po_id, domain),
    ).fetchone()
    if not row:
        return None
    doc = _row(row)
    doc["lines"] = [
        _row(r)
        for r in db.execute(
            "SELECT * FROM purchase_order_lines WHERE purchase_order_id = ? ORDER BY line_no",
            (po_id,),
        ).fetchall()
    ]
    outstanding_total = 0
    for line in doc["lines"]:
        ordered = int(line["quantity_milli"])
        received = int(line["received_quantity_milli"] or 0)
        outstanding = max(0, ordered - received)
        line["outstanding_quantity_milli"] = outstanding
        line["outstanding_quantity_display"] = format_quantity(outstanding, line.get("uom"))
        line["quantity_display"] = format_quantity(ordered, line.get("uom"))
        line["received_quantity_display"] = format_quantity(received, line.get("uom"))
        outstanding_total += outstanding
    doc["outstanding_quantity_milli"] = outstanding_total
    doc["is_fully_received"] = outstanding_total == 0 and bool(doc["lines"])
    doc["legal_transitions"] = sorted(PO_TRANSITIONS.get(doc["status"], ()))
    doc["can_receive"] = doc["status"] in RECEIVABLE_PO_STATUSES
    return doc


def list_purchase_orders(
    *,
    status: str | None = None,
    vendor_code: str | None = None,
    open_only: bool = False,
    limit: int = 100,
    domain: str = STORES_DOMAIN,
) -> list[dict]:
    """One desk's purchase orders (the stores desk's unless `domain` says
    otherwise)."""
    _assert_domain(domain)
    sql = (
        "SELECT p.*, v.code AS vendor_code, v.name AS vendor_name, "
        "s.code AS store_code "
        "FROM purchase_orders p LEFT JOIN vendors v ON v.id = p.vendor_id "
        "LEFT JOIN stores s ON s.id = p.store_id WHERE p.domain = ?"
    )
    params: list[Any] = [domain]
    if status:
        sql += " AND p.status = ?"
        params.append(status)
    if open_only:
        sql += " AND p.status IN ('issued','partially_received')"
    if vendor_code:
        sql += " AND v.code = ?"
        params.append(vendor_code.upper())
    sql += " ORDER BY p.order_date DESC, p.id DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        return [_row(r) for r in db.execute(sql, params).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
#  Goods receipt (GRN) — where stock actually arrives
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class GRNLineInput:
    po_line_no: int | None = None
    item_code: str | None = None
    accepted_quantity_milli: int = 0
    rejected_quantity_milli: int = 0
    batch_no: str | None = None
    expiry_date: date | str | None = None
    rejection_reason: str | None = None
    unit_rate_paise: int | None = None
    uom: str | None = None
    description: str | None = None
    remarks: str | None = None
    # Pharmacy receipts only (migration 54). A stores receipt that sends any of
    # them is refused rather than having them dropped in silence.
    free_quantity_milli: int = 0
    mrp_pack_paise: int | None = None
    mfg_date: date | str | None = None


def _coerce_grn_lines(lines: Sequence[Any]) -> list[GRNLineInput]:
    out: list[GRNLineInput] = []
    for raw in lines or []:
        if isinstance(raw, GRNLineInput):
            out.append(raw)
            continue
        if not isinstance(raw, dict):
            raise ProcurementError("each receipt line must be an object", code="bad_line")
        out.append(
            GRNLineInput(
                po_line_no=raw.get("po_line_no"),
                item_code=(str(raw["item_code"]).strip().upper() if raw.get("item_code") else None),
                accepted_quantity_milli=raw.get("accepted_quantity_milli") or 0,
                rejected_quantity_milli=raw.get("rejected_quantity_milli") or 0,
                batch_no=raw.get("batch_no"),
                expiry_date=raw.get("expiry_date"),
                rejection_reason=raw.get("rejection_reason"),
                unit_rate_paise=raw.get("unit_rate_paise"),
                uom=raw.get("uom"),
                description=raw.get("description"),
                remarks=raw.get("remarks"),
                free_quantity_milli=raw.get("free_quantity_milli") or 0,
                mrp_pack_paise=raw.get("mrp_pack_paise"),
                mfg_date=raw.get("mfg_date"),
            )
        )
    if not out:
        raise ProcurementError("a goods receipt needs at least one line", code="empty_document")
    return out


def create_goods_receipt(
    *,
    purchase_order_id: int,
    lines: Sequence[Any],
    store_code: str | None = None,
    vendor_invoice_number: str | None = None,
    vendor_invoice_date: date | str | None = None,
    vendor_invoice_total_paise: int | None = None,
    qc_status: str = "passed",
    received_by: str | None = None,
    remarks: str | None = None,
    idempotency_key: str | None = None,
    post: bool = True,
) -> dict:
    """Receive goods against a purchase order. PARTIAL RECEIPT IS THE NORMAL CASE.

    What this enforces, and why each one is here:

      * The PO must be ISSUED or PARTIALLY_RECEIVED. Receiving against a draft
        order means the goods arrived before anybody approved buying them.
      * ACCEPTED + REJECTED may not exceed the line's OUTSTANDING quantity. An
        over-receipt is either a vendor error or a typo, and silently absorbing
        it puts stock on a shelf that no order and no invoice accounts for.
      * Only the ACCEPTED quantity moves into stock. Rejected goods physically
        sit in the receiving bay awaiting return; they are recorded on the GRN
        and are NOT stock.
      * An expiry-tracked item must arrive with a BATCH and an EXPIRY DATE, and
        the date must not already be in the past.

    IDEMPOTENCY. `idempotency_key` is unique in the schema. A retried POST — the
    classic double-tap on a slow connection — returns the receipt that already
    exists rather than posting the stock twice. A double-posted GRN inflates a
    store's stock by exactly one delivery, and nobody finds it until a physical
    count months later.
    """
    # Validated HERE as well as in the core, and BEFORE the replay check, so a
    # malformed retry is still refused rather than answered with the receipt.
    parsed = _coerce_grn_lines(lines)
    if qc_status not in ("pending", "passed", "failed", "partial"):
        raise ProcurementError(f"unknown qc_status {qc_status!r}", code="bad_qc_status")

    if idempotency_key:
        with db_session() as db:
            existing_id = _goods_receipt_id_for_key(db, idempotency_key)
        if existing_id is not None:
            doc = get_goods_receipt(existing_id) or {}
            doc["idempotent_replay"] = True
            return doc

    with db_session(immediate=True) as db:
        grn_id = _create_goods_receipt_core(
            db,
            purchase_order_id=purchase_order_id,
            lines=parsed,
            store_code=store_code,
            vendor_invoice_number=vendor_invoice_number,
            vendor_invoice_date=vendor_invoice_date,
            vendor_invoice_total_paise=vendor_invoice_total_paise,
            qc_status=qc_status,
            received_by=received_by,
            remarks=remarks,
            idempotency_key=idempotency_key,
            today=date.today(),
        )
    if post:
        return post_goods_receipt(grn_id, actor=received_by)
    return get_goods_receipt(grn_id) or {}


def _goods_receipt_id_for_key(
    db, idempotency_key: str, *, domain: str = STORES_DOMAIN
) -> int | None:
    """The receipt already saved under this idempotency key, or None.

    A key held by the other desk's receipt is refused (already_exists) rather
    than replayed: the replay would answer with a document this desk cannot
    see, and a fresh receipt under the same key would hit the unique index."""
    _assert_domain(domain)
    row = db.execute(
        "SELECT id, domain FROM goods_receipts WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row and row["domain"] != domain:
        raise ProcurementError(
            "this idempotency key already belongs to another receipt. Nothing was "
            "changed; retry with a new key.",
            code="already_exists",
        )
    return int(row["id"]) if row else None


def _create_goods_receipt_core(
    db,
    *,
    purchase_order_id: int,
    lines: Sequence[Any],
    store_code: str | None = None,
    vendor_invoice_number: str | None = None,
    vendor_invoice_date: date | str | None = None,
    vendor_invoice_total_paise: int | None = None,
    qc_status: str = "passed",
    received_by: str | None = None,
    remarks: str | None = None,
    idempotency_key: str | None = None,
    today: date | datetime | str,
    domain: str = STORES_DOMAIN,
) -> int:
    """`create_goods_receipt` on the caller's connection, as a DRAFT. Returns the
    new receipt id.

    Every rule in `create_goods_receipt` is enforced here. What is NOT here:
    the idempotent replay (look the key up with `_goods_receipt_id_for_key`
    first, inside the same transaction) and the post (call
    `_post_goods_receipt_core`). `today` is the date an expiry is compared
    with: "expired" means before it. It is required so the caller decides
    which clock that is (see `_calendar_today`).

    THE RECEIPT TAKES ITS DOMAIN FROM ITS PURCHASE ORDER. `domain` is the
    caller's desk, and a purchase order of the other desk is not found, so a
    stores receipt can never be raised against a pharmacy order. On a pharmacy
    receipt every accepted line needs a batch and an expiry (a drug lot is
    traced and dispensed by both), the store must be a pharmacy store, and a
    line may carry free units, the pack MRP and a manufacturing date. The
    minimum shelf life, the expiry typed twice and the MRP rules (GRN-R16,
    GRN-RS6) are the pharmacy service's, checked before it calls this."""
    _assert_domain(domain)
    receipt_today = _calendar_today(today)
    parsed = _coerce_grn_lines(lines)
    if qc_status not in ("pending", "passed", "failed", "partial"):
        raise ProcurementError(f"unknown qc_status {qc_status!r}", code="bad_qc_status")

    po = _document_in_domain(db, "purchase_orders", purchase_order_id, domain, "purchase order")
    if po["status"] not in RECEIVABLE_PO_STATUSES:
        raise ProcurementError(
            f"purchase order is {po['status']!r}; goods may only be received "
            f"against an order in {sorted(RECEIVABLE_PO_STATUSES)}",
            code="po_not_receivable",
        )
    po_lines = {
        int(r["line_no"]): _row(r)
        for r in db.execute(
            "SELECT * FROM purchase_order_lines WHERE purchase_order_id = ?",
            (purchase_order_id,),
        ).fetchall()
    }

    store_id = po["store_id"]
    if store_code:
        store = _store_row(db, store_code, domain)
        if not store:
            raise ProcurementError(f"no store with code {store_code!r}", code="not_found")
        store_id = store["id"]
    if store_id is None:
        raise ProcurementError(
            "a goods receipt needs a store, neither the purchase order nor this receipt names one",
            code="missing_store",
        )
    # The order's own store is checked too: goods land only in a store of the
    # receiving desk's half of the master. A stores order whose store id
    # dangles is received as it always was (no foreign keys here, by rule).
    other_half = db.execute(
        f"SELECT 1 FROM stores WHERE id = ? AND "
        f"{_store_partition_sql(STORES_DOMAIN if domain == PHARMACY_DOMAIN else PHARMACY_DOMAIN)}",
        (store_id,),
    ).fetchone()
    if other_half or (domain == PHARMACY_DOMAIN and _store_row(db, int(store_id), domain) is None):
        raise ProcurementError(f"no store with id {store_id}", code="not_found")

    prepared: list[dict] = []
    for line in parsed:
        if line.po_line_no is None:
            raise ProcurementError(
                "each receipt line must name the purchase-order line it is against (po_line_no)",
                code="bad_line",
            )
        po_line = po_lines.get(int(line.po_line_no))
        if not po_line:
            raise ProcurementError(
                f"purchase order {purchase_order_id} has no line {line.po_line_no}",
                code="bad_line",
            )
        if po_line["status"] in ("cancelled", "short_closed"):
            raise ProcurementError(
                f"line {line.po_line_no} is {po_line['status']!r} and cannot receive",
                code="line_closed",
            )

        accepted = assert_quantity_milli(line.accepted_quantity_milli, "accepted_quantity_milli")
        rejected = assert_quantity_milli(line.rejected_quantity_milli, "rejected_quantity_milli")
        if accepted < 0 or rejected < 0:
            raise ProcurementError("receipt quantities must not be negative", code="bad_quantity")
        if accepted == 0 and rejected == 0:
            raise ProcurementError(
                f"line {line.po_line_no} receives nothing; leave it off the receipt instead",
                code="empty_line",
            )

        ordered = int(po_line["quantity_milli"])
        already = int(po_line["received_quantity_milli"] or 0)
        outstanding = ordered - already
        if accepted + rejected > outstanding:
            raise ProcurementError(
                f"line {line.po_line_no}: receiving "
                f"{format_quantity(accepted + rejected)} against an outstanding "
                f"{format_quantity(outstanding)} would over-receive the order. "
                "Raise an amended purchase order for the excess.",
                code="over_receipt",
            )

        expiry = _as_date_str(line.expiry_date, "expiry_date")
        batch = _clean(line.batch_no, field_name="batch_no")
        free = assert_quantity_milli(line.free_quantity_milli, "free_quantity_milli")
        mfg = _as_date_str(line.mfg_date, "mfg_date")
        mrp = assert_paise(line.mrp_pack_paise) if line.mrp_pack_paise is not None else None
        item = None
        if domain == PHARMACY_DOMAIN:
            if not po_line.get(_DRUG_ID_COLUMN):
                raise ProcurementError(
                    f"line {line.po_line_no} names no drug; a pharmacy receipt is for drugs only",
                    code="bad_line",
                )
            if accepted > 0 and (not batch or not expiry):
                raise ProcurementError(
                    f"{po_line['item_code']} must be received with a batch number and "
                    "an expiry date: a drug is recalled, dispensed and expired by its "
                    "batch",
                    code="expiry_required",
                )
            if free < 0 or (mrp is not None and mrp < 0):
                raise ProcurementError(
                    "free quantity and MRP must not be negative", code="bad_quantity"
                )
            if mfg and expiry and mfg > expiry:
                raise ProcurementError(
                    f"{po_line['item_code']} batch {batch}: manufactured {mfg}, after "
                    f"its expiry {expiry}",
                    code="bad_date",
                )
        else:
            if free or mrp is not None or mfg:
                raise ProcurementError(
                    "free quantity, MRP and manufacturing date are recorded on "
                    "pharmacy receipts only",
                    code="bad_line",
                )
            if po_line.get("inventory_item_id"):
                item_row = db.execute(
                    "SELECT * FROM inventory_items WHERE id = ?",
                    (po_line["inventory_item_id"],),
                ).fetchone()
                item = _row(item_row) if item_row else None
            if item is None:
                item = _resolve_item(db, po_line["item_code"])

        if (
            accepted > 0
            and item is not None
            and item_requires_expiry(item)
            and (not batch or not expiry)
        ):
            raise ProcurementError(
                f"{po_line['item_code']} is a {item.get('category')}: it must be "
                "received with a batch number and an expiry date, because a "
                "recall or an invalidated result has to be traceable to a lot",
                code="expiry_required",
            )
        if accepted > 0 and expiry and date.fromisoformat(expiry) < receipt_today:
            # Applies to ANY item that was given a date, not only the
            # mandatory categories: if a storekeeper typed an expiry, the
            # system must act on it rather than file it.
            raise ProcurementError(
                f"{po_line['item_code']} batch {batch or '(none)'} expired on "
                f"{expiry}; expired stock must not be received into a store",
                code="expired_on_receipt",
            )
        if rejected > 0 and not (line.rejection_reason or "").strip():
            raise ProcurementError(
                f"line {line.po_line_no} rejects {format_quantity(rejected)} with no reason given",
                code="missing_field",
            )

        rate = (
            assert_paise(line.unit_rate_paise)
            if line.unit_rate_paise is not None
            else int(po_line["unit_rate_paise"] or 0)
        )
        totals = compute_line(
            unit_rate_paise=rate,
            quantity_milli=accepted,
            gst_rate_bp=int(po_line["gst_rate_bp"] or 0),
            gst_treatment=po_line["gst_treatment"],
            interstate=bool(po["is_interstate"]),
        )
        prepared.append(
            {
                "po_line": po_line,
                "item": item,
                "accepted": accepted,
                "rejected": rejected,
                "batch": batch,
                "expiry": expiry,
                "rate": rate,
                "totals": totals,
                "input": line,
                "free": free,
                "mrp": mrp,
                "mfg": mfg,
            }
        )

    foot = compute_invoice([p["totals"] for p in prepared], round_off_to_rupee=False)
    cur = db.execute(
        """INSERT INTO goods_receipts (grn_number, purchase_order_id, vendor_id,
               store_id, vendor_invoice_number, vendor_invoice_date,
               vendor_invoice_total_paise, status, qc_status, subtotal_paise,
               taxable_value_paise, cgst_paise, sgst_paise, igst_paise, tax_paise,
               total_paise, received_by, idempotency_key, remarks, domain)
           VALUES (NULL,?,?,?,?,?,?, 'draft', ?, ?,?,?,?,?,?,?, ?,?,?, ?)""",
        (
            purchase_order_id,
            po["vendor_id"],
            store_id,
            vendor_invoice_number,
            _as_date_str(vendor_invoice_date, "vendor_invoice_date"),
            assert_paise(vendor_invoice_total_paise)
            if vendor_invoice_total_paise is not None
            else None,
            qc_status,
            foot.subtotal_paise,
            foot.taxable_value_paise,
            foot.cgst_paise,
            foot.sgst_paise,
            foot.igst_paise,
            foot.tax_paise,
            foot.total_paise,
            received_by,
            idempotency_key,
            remarks,
            po["domain"],
        ),
    )
    grn_id = cur.lastrowid
    for line_no, p in enumerate(prepared, start=1):
        po_line, totals = p["po_line"], p["totals"]
        db.execute(
            f"""INSERT INTO goods_receipt_lines (goods_receipt_id, line_no,
                   purchase_order_line_id, inventory_item_id, item_code, description,
                   batch_no, expiry_date, uom, quantity_milli, accepted_quantity_milli,
                   rejected_quantity_milli, rejection_reason, unit_rate_paise,
                   gross_paise, discount_paise, gst_treatment, gst_rate_bp,
                   taxable_value_paise, cgst_paise, sgst_paise, igst_paise,
                   tax_paise, total_paise, remarks, {_DRUG_ID_COLUMN}, units_per_pack,
                   free_quantity_milli, mrp_pack_paise, mfg_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?)""",
            (
                grn_id,
                line_no,
                po_line["id"],
                po_line.get("inventory_item_id"),
                po_line["item_code"],
                p["input"].description or po_line["description"],
                p["batch"],
                p["expiry"],
                p["input"].uom or po_line["uom"],
                p["accepted"] + p["rejected"],
                p["accepted"],
                p["rejected"],
                (p["input"].rejection_reason or "").strip() or None,
                p["rate"],
                totals.gross_paise,
                totals.discount_paise,
                totals.gst_treatment,
                totals.gst_rate_bp,
                totals.taxable_value_paise,
                totals.cgst_paise,
                totals.sgst_paise,
                totals.igst_paise,
                totals.tax_paise,
                totals.total_paise,
                p["input"].remarks,
                po_line.get(_DRUG_ID_COLUMN),
                po_line.get("units_per_pack"),
                p["free"],
                p["mrp"],
                p["mfg"],
            ),
        )
    return int(grn_id)


def post_goods_receipt(grn_id: int, *, actor: str | None = None) -> dict:
    """Move a draft receipt to `posted`: stock lands, the PO advances, the item's
    last purchase rate is updated.

    All three happen in ONE `immediate` transaction. A GRN that posted stock but
    failed to advance the PO would leave the order permanently receivable, and
    the next delivery would be received against quantities that were already
    taken in.
    """
    with db_session(immediate=True) as db:
        _post_goods_receipt_core(db, grn_id, actor=actor)
    # Deliberately AFTER the commit: the stock is on the shelf either way, and a
    # blank GRN number is a repairable annoyance where a failed post would not be.
    _stamp_document_number("goods_receipt", grn_id)
    return get_goods_receipt(grn_id) or {}


def _post_goods_receipt_core(
    db, grn_id: int, *, actor: str | None = None, domain: str = STORES_DOMAIN
) -> list[dict]:
    """`post_goods_receipt` on the caller's connection. Returns the receipt's
    lines as they were posted. The caller stamps the GRN number after its
    transaction commits. Another desk's receipt is not found.

    THE PHARMACY BRANCH MOVES NO STOCK HERE. It advances the order's received
    quantities and status and marks the receipt posted, exactly as the stores
    branch does, but it never calls _insert_movement and never touches
    inventory_items: drug stock has one ledger, written only by
    pharmacy_stock_service, which the caller runs on the returned lines in
    the same transaction."""
    grn = _document_in_domain(db, "goods_receipts", grn_id, domain, "goods receipt")
    assert_transition("goods_receipt", grn["status"], "posted")
    lines = [
        _row(r)
        for r in db.execute(
            "SELECT * FROM goods_receipt_lines WHERE goods_receipt_id = ? ORDER BY line_no",
            (grn_id,),
        ).fetchall()
    ]

    for line in lines:
        accepted = int(line["accepted_quantity_milli"] or 0)
        if accepted > 0 and grn["domain"] == STORES_DOMAIN:
            _insert_movement(
                db,
                inventory_item_id=line.get("inventory_item_id"),
                item_code=line["item_code"],
                store_id=grn["store_id"],
                batch_no=line.get("batch_no"),
                expiry_date=line.get("expiry_date"),
                movement_type="receipt",
                quantity_milli=accepted,
                unit_rate_paise=line.get("unit_rate_paise"),
                reference_type="grn",
                reference_id=str(grn_id),
                moved_by=actor,
                idempotency_key=f"grn:{grn_id}:line:{line['line_no']}",
                remarks=None,
            )
            rate = line.get("unit_rate_paise")
            if rate is not None and int(rate) > 0 and line.get("inventory_item_id"):
                # LATEST PURCHASE RATE, not a weighted average. Stated
                # plainly because it matters: this is the rate the last
                # delivery came in at, and it is what stock issues are
                # valued at below. A finance department that wants moving
                # weighted average or FIFO needs a costing layer this
                # version does not have.
                db.execute(
                    "UPDATE inventory_items SET last_purchase_rate_paise = ?, "
                    "rate_is_configured = 1, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (int(rate), line["inventory_item_id"]),
                )
        if line.get("purchase_order_line_id"):
            db.execute(
                "UPDATE purchase_order_lines SET received_quantity_milli = "
                "received_quantity_milli + ? WHERE id = ?",
                (accepted, line["purchase_order_line_id"]),
            )

    db.execute(
        "UPDATE goods_receipts SET status = 'posted', posted_by = ?, posted_at = ? WHERE id = ?",
        (actor, _now(), grn_id),
    )
    if grn["purchase_order_id"]:
        _refresh_po_receipt_status(db, grn["purchase_order_id"])
    return lines


def _refresh_po_receipt_status(db, po_id: int) -> str:
    """Derive a PO's status from its line quantities after a receipt.

    DERIVED, NOT ASSERTED. The receipt does not tell the order what status to be
    — the quantities do. `issued → partially_received → received` therefore
    cannot drift out of step with the numbers on the lines, which is the failure
    that makes an "open orders" report untrustworthy.
    """
    rows = db.execute(
        "SELECT quantity_milli, received_quantity_milli, status "
        "FROM purchase_order_lines WHERE purchase_order_id = ?",
        (po_id,),
    ).fetchall()
    live = [r for r in rows if r["status"] not in ("cancelled",)]
    ordered = sum(int(r["quantity_milli"]) for r in live)
    received = sum(int(r["received_quantity_milli"] or 0) for r in live)

    for r in db.execute(
        "SELECT id, quantity_milli, received_quantity_milli, status "
        "FROM purchase_order_lines WHERE purchase_order_id = ?",
        (po_id,),
    ).fetchall():
        if r["status"] in ("cancelled", "short_closed"):
            continue
        got = int(r["received_quantity_milli"] or 0)
        want = int(r["quantity_milli"])
        line_status = "received" if got >= want else ("partially_received" if got > 0 else "open")
        db.execute(
            "UPDATE purchase_order_lines SET status = ? WHERE id = ?",
            (line_status, r["id"]),
        )

    current = db.execute("SELECT status FROM purchase_orders WHERE id = ?", (po_id,)).fetchone()[
        "status"
    ]
    target = (
        "received"
        if received >= ordered and ordered > 0
        else ("partially_received" if received > 0 else current)
    )
    if target != current:
        assert_transition("purchase_order", current, target)
        db.execute("UPDATE purchase_orders SET status = ? WHERE id = ?", (target, po_id))
    return target


def cancel_goods_receipt(grn_id: int, *, reason: str, actor: str | None = None) -> dict:
    """Cancel a DRAFT receipt. A posted one cannot be cancelled — see
    GRN_TRANSITIONS: the stock has already moved."""
    with db_session(immediate=True) as db:
        _cancel_goods_receipt_core(db, grn_id, reason=reason, actor=actor)
    return get_goods_receipt(grn_id) or {}


def _cancel_goods_receipt_core(
    db,
    grn_id: int,
    *,
    reason: str,
    actor: str | None = None,
    domain: str = STORES_DOMAIN,
) -> None:
    """`cancel_goods_receipt` on the caller's connection. Another desk's
    receipt is not found."""
    row = _document_in_domain(db, "goods_receipts", grn_id, domain, "goods receipt")
    assert_transition("goods_receipt", row["status"], "cancelled")
    if not (reason or "").strip():
        raise ProcurementError("a cancellation needs a reason", code="missing_field")
    db.execute(
        "UPDATE goods_receipts SET status = 'cancelled', cancelled_by = ?, "
        "cancelled_at = ?, cancel_reason = ? WHERE id = ?",
        (actor, _now(), reason.strip(), grn_id),
    )


def get_goods_receipt(grn_id: int) -> dict | None:
    with db_session() as db:
        return _get_goods_receipt_core(db, grn_id)


def _get_goods_receipt_core(db, grn_id: int, *, domain: str = STORES_DOMAIN) -> dict | None:
    """`get_goods_receipt` on the caller's connection. None for another desk's
    receipt."""
    _assert_domain(domain)
    row = db.execute(
        """SELECT g.*, v.code AS vendor_code, v.name AS vendor_name,
                  s.code AS store_code, s.name AS store_name, p.po_number
             FROM goods_receipts g
             LEFT JOIN vendors v ON v.id = g.vendor_id
             LEFT JOIN stores s ON s.id = g.store_id
             LEFT JOIN purchase_orders p ON p.id = g.purchase_order_id
            WHERE g.id = ? AND g.domain = ?""",
        (grn_id, domain),
    ).fetchone()
    if not row:
        return None
    doc = _row(row)
    doc["lines"] = [
        _row(r)
        for r in db.execute(
            "SELECT * FROM goods_receipt_lines WHERE goods_receipt_id = ? ORDER BY line_no",
            (grn_id,),
        ).fetchall()
    ]
    for line in doc["lines"]:
        line["accepted_quantity_display"] = format_quantity(
            line.get("accepted_quantity_milli"), line.get("uom")
        )
        line["rejected_quantity_display"] = format_quantity(
            line.get("rejected_quantity_milli"), line.get("uom")
        )
    doc["legal_transitions"] = sorted(GRN_TRANSITIONS.get(doc["status"], ()))
    return doc


def list_goods_receipts(
    *,
    purchase_order_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    domain: str = STORES_DOMAIN,
) -> list[dict]:
    """One desk's goods receipts (the stores desk's unless `domain` says
    otherwise)."""
    _assert_domain(domain)
    sql = (
        "SELECT g.*, v.name AS vendor_name, s.code AS store_code, p.po_number "
        "FROM goods_receipts g LEFT JOIN vendors v ON v.id = g.vendor_id "
        "LEFT JOIN stores s ON s.id = g.store_id "
        "LEFT JOIN purchase_orders p ON p.id = g.purchase_order_id WHERE g.domain = ?"
    )
    params: list[Any] = [domain]
    if purchase_order_id is not None:
        sql += " AND g.purchase_order_id = ?"
        params.append(purchase_order_id)
    if status:
        sql += " AND g.status = ?"
        params.append(status)
    sql += " ORDER BY g.received_at DESC, g.id DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        return [_row(r) for r in db.execute(sql, params).fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
#  Stock
# ═════════════════════════════════════════════════════════════════════════════
#
# ONE SIGNED LEDGER. `stock_movements.quantity_milli` is SIGNED: positive moves
# stock INTO a store, negative moves it OUT. On hand is therefore literally
# `SUM(quantity_milli)` — there is no separate balance table that could disagree
# with the movements that produced it, and no reconciliation job.
# `balance_after_milli` is a CONVENIENCE COPY written under the same immediate
# transaction, for reading a ledger; the SUM is the truth.

INWARD_MOVEMENTS = ("opening", "receipt", "return", "transfer_in", "adjustment")
OUTWARD_MOVEMENTS = ("issue", "consumption", "write_off", "expiry", "transfer_out")

#: Outward movements that represent goods actually USED by a department, as
#: opposed to goods that left the store for another reason (a transfer moves
#: stock between two of the hospital's own stores; a write-off is a loss).
CONSUMPTION_MOVEMENTS = ("issue", "consumption")


def _insert_movement(
    db,
    *,
    inventory_item_id: int | None,
    item_code: str,
    store_id: int | None,
    movement_type: str,
    quantity_milli: int,
    batch_no: str | None = None,
    expiry_date: str | None = None,
    unit_rate_paise: int | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    patient_id: str | None = None,
    moved_by: str | None = None,
    idempotency_key: str | None = None,
    remarks: str | None = None,
) -> int:
    """Append one movement. Caller MUST already hold an immediate transaction —
    the running balance is read here and written back."""
    signed = quantity_milli
    if movement_type in OUTWARD_MOVEMENTS and signed > 0:
        signed = -signed

    balance = db.execute(
        "SELECT COALESCE(SUM(quantity_milli), 0) AS q FROM stock_movements "
        "WHERE item_code = ? AND store_id IS ?",
        (item_code, store_id),
    ).fetchone()["q"]
    new_balance = int(balance) + signed

    value = None
    if unit_rate_paise is not None:
        # value_paise is NULL — not 0 — when the rate is unknown, so an
        # unvalued issue reads as "no value recorded" and never as "free".
        value = (assert_paise(unit_rate_paise) * abs(signed)) // QUANTITY_SCALE

    try:
        cur = db.execute(
            """INSERT INTO stock_movements (inventory_item_id, item_code, store_id,
                   batch_no, expiry_date, movement_type, quantity_milli,
                   balance_after_milli, unit_rate_paise, value_paise, reference_type,
                   reference_id, patient_id, moved_by, idempotency_key, remarks)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                inventory_item_id,
                item_code,
                store_id,
                batch_no,
                expiry_date,
                movement_type,
                signed,
                new_balance,
                int(unit_rate_paise) if unit_rate_paise is not None else None,
                value,
                reference_type,
                reference_id,
                patient_id,
                moved_by,
                idempotency_key,
                remarks,
            ),
        )
    except DBIntegrityError as exc:
        if idempotency_key and "idempotency" in str(exc).lower():
            existing = db.execute(
                "SELECT id FROM stock_movements WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return int(existing["id"])
        raise
    return int(cur.lastrowid)


def record_stock_movement(
    *,
    item_code: str,
    store_code: str,
    movement_type: str,
    quantity_milli: int,
    batch_no: str | None = None,
    expiry_date: date | str | None = None,
    unit_rate_paise: int | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    patient_id: str | None = None,
    moved_by: str | None = None,
    idempotency_key: str | None = None,
    remarks: str | None = None,
    allow_negative: bool = False,
) -> dict:
    """Record one stock movement by code. `quantity_milli` is given as a POSITIVE
    magnitude; the sign follows from `movement_type`.

    A movement that would drive a store negative is REFUSED unless
    `allow_negative=True` (which exists for a stock-take adjustment correcting a
    ledger that is already wrong). Negative stock is not a number a store can
    act on: it means the ledger and the shelf disagree, and continuing to issue
    against it hides the discrepancy instead of surfacing it.
    """
    with db_session(immediate=True) as db:
        return _record_stock_movement_core(
            db,
            item_code=item_code,
            store_code=store_code,
            movement_type=movement_type,
            quantity_milli=quantity_milli,
            batch_no=batch_no,
            expiry_date=expiry_date,
            unit_rate_paise=unit_rate_paise,
            reference_type=reference_type,
            reference_id=reference_id,
            patient_id=patient_id,
            moved_by=moved_by,
            idempotency_key=idempotency_key,
            remarks=remarks,
            allow_negative=allow_negative,
        )


def _record_stock_movement_core(
    db,
    *,
    item_code: str,
    store_code: str,
    movement_type: str,
    quantity_milli: int,
    batch_no: str | None = None,
    expiry_date: date | str | None = None,
    unit_rate_paise: int | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    patient_id: str | None = None,
    moved_by: str | None = None,
    idempotency_key: str | None = None,
    remarks: str | None = None,
    allow_negative: bool = False,
) -> dict:
    """`record_stock_movement` on the caller's connection. The on-hand check
    reads and writes in the caller's transaction, so that transaction must be
    an immediate one (it is in the wrapper)."""
    if movement_type not in INWARD_MOVEMENTS + OUTWARD_MOVEMENTS:
        raise ProcurementError(f"unknown movement type {movement_type!r}", code="bad_movement_type")
    qty = assert_quantity_milli(quantity_milli, "quantity_milli")
    if qty == 0:
        raise ProcurementError("a stock movement of zero is not a movement", code="bad_quantity")
    if movement_type != "adjustment" and qty < 0:
        raise ProcurementError(
            "pass a positive magnitude; the direction comes from movement_type "
            "(only 'adjustment' may be signed)",
            code="bad_quantity",
        )

    item = _resolve_item(db, item_code.upper())
    if not item:
        raise ProcurementError(f"no non-pharmacy item with code {item_code!r}", code="unknown_item")
    # The stores ledger moves stock through stores stores only: a pharmacy
    # store is not found here, so nothing can be issued from or received into
    # one through this ledger.
    store = _store_row(db, store_code, STORES_DOMAIN)
    if not store:
        raise ProcurementError(f"no store with code {store_code!r}", code="not_found")

    signed = -qty if (movement_type in OUTWARD_MOVEMENTS and qty > 0) else qty
    current = db.execute(
        "SELECT COALESCE(SUM(quantity_milli),0) AS q FROM stock_movements "
        "WHERE item_code = ? AND store_id IS ?",
        (item["code"], store["id"]),
    ).fetchone()["q"]
    if not allow_negative and int(current) + signed < 0:
        raise ProcurementError(
            f"{item['code']} in {store['code']} has "
            f"{format_quantity(int(current), item['uom'])} on hand; issuing "
            f"{format_quantity(abs(signed), item['uom'])} would take it negative",
            code="insufficient_stock",
        )

    rate = unit_rate_paise
    if rate is None and movement_type in OUTWARD_MOVEMENTS:
        rate = item["last_purchase_rate_paise"] if item.get("rate_is_configured") else None

    movement_id = _insert_movement(
        db,
        inventory_item_id=item["id"],
        item_code=item["code"],
        store_id=store["id"],
        movement_type=movement_type,
        quantity_milli=qty,
        batch_no=batch_no,
        expiry_date=_as_date_str(expiry_date, "expiry_date"),
        unit_rate_paise=rate,
        reference_type=reference_type,
        reference_id=reference_id,
        patient_id=patient_id,
        moved_by=moved_by,
        idempotency_key=idempotency_key,
        remarks=remarks,
    )
    row = db.execute("SELECT * FROM stock_movements WHERE id = ?", (movement_id,)).fetchone()
    return _row(row)


def issue_stock(
    *,
    item_code: str,
    store_code: str,
    quantity_milli: int,
    department: str | None = None,
    patient_id: str | None = None,
    batch_no: str | None = None,
    moved_by: str | None = None,
    idempotency_key: str | None = None,
    remarks: str | None = None,
) -> dict:
    """Issue stock out of a store to a department (and optionally against a named
    patient, for costing).

    `patient_id` is a COSTING link, not a clinical record: it says a consumable
    left the store for this patient so it can be charged. It writes nothing to
    any clinical table and nothing clinical reads it.
    """
    with db_session(immediate=True) as db:
        return _issue_stock_core(
            db,
            item_code=item_code,
            store_code=store_code,
            quantity_milli=quantity_milli,
            department=department,
            patient_id=patient_id,
            batch_no=batch_no,
            moved_by=moved_by,
            idempotency_key=idempotency_key,
            remarks=remarks,
        )


def _issue_stock_core(
    db,
    *,
    item_code: str,
    store_code: str,
    quantity_milli: int,
    department: str | None = None,
    patient_id: str | None = None,
    batch_no: str | None = None,
    moved_by: str | None = None,
    idempotency_key: str | None = None,
    remarks: str | None = None,
) -> dict:
    """`issue_stock` on the caller's connection."""
    note = remarks
    if department:
        note = f"{department}" + (f", {remarks}" if remarks else "")
    return _record_stock_movement_core(
        db,
        item_code=item_code,
        store_code=store_code,
        movement_type="issue",
        quantity_milli=quantity_milli,
        batch_no=batch_no,
        reference_type="patient_charge" if patient_id else "manual",
        patient_id=patient_id,
        moved_by=moved_by,
        idempotency_key=idempotency_key,
        remarks=note,
    )


def stock_on_hand_milli(item_code: str, store_code: str | None = None) -> int:
    """On hand, in milli. `SUM(quantity_milli)` — the single source of truth."""
    sql = (
        "SELECT COALESCE(SUM(m.quantity_milli), 0) AS q FROM stock_movements m "
        "WHERE m.item_code = ?"
    )
    params: list[Any] = [item_code.upper()]
    if store_code:
        # A pharmacy store code finds no store here, so it reads 0 (drug stock
        # is never in this ledger).
        sql += " AND m.store_id = (SELECT id FROM stores WHERE code = ? AND pharmacy_kind IS NULL)"
        params.append(store_code.upper())
    with db_session() as db:
        return int(db.execute(sql, params).fetchone()["q"])


def stock_on_hand(item_code: str, store_code: str | None = None) -> dict:
    item = get_inventory_item(item_code.upper())
    on_hand = stock_on_hand_milli(item_code, store_code)
    level = _reorder_level_milli(item) if item else None
    return {
        "item_code": item_code.upper(),
        "item_name": _get(item, "name"),
        "uom": _get(item, "uom"),
        "store_code": store_code.upper() if store_code else None,
        "on_hand_milli": on_hand,
        "on_hand_display": format_quantity(on_hand, _get(item, "uom")),
        "reorder_level_milli": level,
        "is_low": level is not None and on_hand <= level,
    }


def list_stock_on_hand(
    *,
    store_code: str | None = None,
    category: str | None = None,
    low_only: bool = False,
    include_zero: bool = True,
    limit: int = 500,
) -> list[dict]:
    """Stock on hand per item per store.

    The aggregation is always `SUM(quantity_milli)` GROUPED by item and store, so
    the total across stores is by construction the sum of the per-store figures.

    TWO SHAPES, AND THE REASON THERE ARE TWO. An empty shelf must be VISIBLE —
    "we hold none of this" is the single most actionable thing a stores view can
    say, and it is exactly the row a naive join drops because there are no
    movements to join to.

      * FILTERED TO ONE STORE → CROSS JOIN that store, so EVERY active item gets
        a row for it, at zero if nothing has ever moved there. Without this, an
        item that is stocked in the central store but has run to nil in theatre
        simply vanishes from the theatre view — which reads as "not our problem"
        when it means "you have none". Pass `include_zero=False` to suppress the
        rows for items that store does not carry.
      * ACROSS ALL STORES → LEFT JOIN, giving one row per store that has ever
        held the item, plus a single store-less row for an item nothing has ever
        moved anywhere. Cross-joining every item against every store here would
        manufacture items × stores rows, most of them meaningless.
    """
    params: list[Any] = []
    columns = """
               i.id AS inventory_item_id, i.code AS item_code, i.name AS item_name,
               i.category, i.uom, i.reorder_level_qty, i.reorder_qty,
               i.last_purchase_rate_paise, i.rate_is_configured,
               s.id AS store_id, s.code AS store_code, s.name AS store_name,
               COALESCE(SUM(m.quantity_milli), 0) AS on_hand_milli,
               MAX(m.moved_at) AS last_movement_at
    """
    if store_code:
        sql = f"""
            SELECT {columns}
              FROM inventory_items i
              CROSS JOIN stores s
              LEFT JOIN stock_movements m
                     ON m.item_code = i.code AND m.store_id = s.id
             WHERE i.is_active = 1 AND s.code = ? AND s.pharmacy_kind IS NULL
        """
        params.append(store_code.upper())
    else:
        sql = f"""
            SELECT {columns}
              FROM inventory_items i
              LEFT JOIN stock_movements m ON m.item_code = i.code
              LEFT JOIN stores s ON s.id = m.store_id
             WHERE i.is_active = 1 AND s.pharmacy_kind IS NULL
        """
    if category:
        sql += " AND i.category = ?"
        params.append(category)
    sql += " GROUP BY i.id, s.id ORDER BY i.category, i.name, s.code LIMIT ?"
    params.append(int(limit))

    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]

    out: list[dict] = []
    for row in rows:
        on_hand = int(row["on_hand_milli"] or 0)
        level = _reorder_level_milli(row)
        configured = bool(row.get("rate_is_configured"))
        rate = row.get("last_purchase_rate_paise") if configured else None
        # In the all-stores shape, an item nothing has ever moved yields a NULL
        # store from the LEFT JOIN. That row is the item, unstocked anywhere —
        # keep it, because "nobody has ever received this" is exactly what a
        # stores manager needs to see, and dropping it would make the list
        # quietly incomplete.
        entry = {
            "item_code": row["item_code"],
            "item_name": row["item_name"],
            "category": row["category"],
            "uom": row["uom"],
            "store_code": row.get("store_code"),
            "store_name": row.get("store_name"),
            "on_hand_milli": on_hand,
            "on_hand_display": format_quantity(on_hand, row["uom"]),
            "reorder_level_milli": level,
            "reorder_level_display": format_quantity(level, row["uom"])
            if level is not None
            else "Not set",
            "reorder_qty_milli": _reorder_qty_milli(row),
            "is_low": level is not None and on_hand <= level,
            "last_purchase_rate_paise": rate,
            "last_purchase_rate_display": format_inr_or_unset(rate),
            "value_paise": (rate * on_hand) // QUANTITY_SCALE if rate is not None else None,
            "value_display": format_inr_or_unset(
                (rate * on_hand) // QUANTITY_SCALE if rate is not None else None,
                unset="Rate not configured",
            ),
            "last_movement_at": row.get("last_movement_at"),
        }
        if low_only and not entry["is_low"]:
            continue
        if not include_zero and on_hand == 0:
            continue
        out.append(entry)
    return out


def stock_ledger(item_code: str, *, store_code: str | None = None, limit: int = 200) -> list[dict]:
    sql = (
        "SELECT m.*, s.code AS store_code FROM stock_movements m "
        "LEFT JOIN stores s ON s.id = m.store_id WHERE m.item_code = ? "
        "AND s.pharmacy_kind IS NULL"
    )
    params: list[Any] = [item_code.upper()]
    if store_code:
        sql += " AND s.code = ?"
        params.append(store_code.upper())
    sql += " ORDER BY m.id DESC LIMIT ?"
    params.append(int(limit))
    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]
    for row in rows:
        row["quantity_display"] = format_quantity(row["quantity_milli"])
        row["value_display"] = format_inr_or_unset(
            row.get("value_paise"), unset="No value recorded"
        )
    return rows


# ═════════════════════════════════════════════════════════════════════════════
#  Reports
# ═════════════════════════════════════════════════════════════════════════════


def low_stock(*, store_code: str | None = None, limit: int = 200) -> list[dict]:
    """Items at OR BELOW their reorder level.

    AT or below — `on_hand <= reorder_level`, not `<`. A reorder level IS the
    trigger point: "we reorder when we are down to two boxes" means you reorder
    at two, not at one. The test pins the boundary exactly (level fires, level+1
    does not), because an off-by-one here is invisible until a store runs out
    one delivery cycle earlier than anyone planned for.

    An item with NO reorder level set is never low — it is unconfigured, and
    unconfigured is not zero.
    """
    rows = list_stock_on_hand(store_code=store_code, low_only=True, limit=limit)
    for row in rows:
        level = row["reorder_level_milli"]
        row["shortfall_milli"] = max(0, (level or 0) - row["on_hand_milli"])
        row["shortfall_display"] = format_quantity(row["shortfall_milli"], row["uom"])
        row["suggested_order_milli"] = row["reorder_qty_milli"]
        row["suggested_order_display"] = (
            format_quantity(row["reorder_qty_milli"], row["uom"])
            if row["reorder_qty_milli"] is not None
            else "Not set"
        )
    rows.sort(key=lambda r: r["on_hand_milli"] - (r["reorder_level_milli"] or 0))
    return rows


def consumption_report(
    *,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    department: str | None = None,
    store_code: str | None = None,
    limit: int = 500,
) -> dict:
    """What each department used, by item, over a period.

    CONSUMPTION IS OUTWARD MOVEMENT OF TYPE issue/consumption ONLY. A transfer
    between two hospital stores is not consumption (the hospital still has it),
    and a write-off is a loss, not a use. Lumping them together is how a
    department gets billed for a flood in the basement.

    DEPARTMENT comes from the STORE the goods left — `stores.department`, falling
    back to `stores.ward`, and "Unattributed" when the store names neither. It is
    labelled honestly rather than being silently folded into a real department.

    VALUE. Each movement was valued at the item's LATEST PURCHASE RATE at the
    moment it moved; a movement of an item that had no configured rate carries no
    value at all. The report therefore returns `value_paise` alongside
    `unvalued_movement_count`, and `value_is_complete` is False whenever any line
    could not be valued — so a total is never presented as if it covered
    everything when it did not.
    """
    start = _as_date_str(date_from, "date_from")
    end = _as_date_str(date_to, "date_to")
    placeholders = ",".join("?" for _ in CONSUMPTION_MOVEMENTS)
    sql = f"""
        SELECT COALESCE(NULLIF(TRIM(COALESCE(s.department, '')), ''),
                        NULLIF(TRIM(COALESCE(s.ward, '')), ''),
                        'Unattributed') AS department,
               s.code AS store_code,
               m.item_code, i.name AS item_name, i.category, i.uom,
               SUM(-m.quantity_milli) AS quantity_milli,
               SUM(COALESCE(m.value_paise, 0)) AS value_paise,
               SUM(CASE WHEN m.value_paise IS NULL THEN 1 ELSE 0 END) AS unvalued,
               COUNT(*) AS movement_count
          FROM stock_movements m
          LEFT JOIN stores s ON s.id = m.store_id
          LEFT JOIN inventory_items i ON i.code = m.item_code
         WHERE m.movement_type IN ({placeholders})
           AND s.pharmacy_kind IS NULL
    """
    params: list[Any] = list(CONSUMPTION_MOVEMENTS)
    if start:
        sql += " AND DATE(m.moved_at) >= ?"
        params.append(start)
    if end:
        sql += " AND DATE(m.moved_at) <= ?"
        params.append(end)
    if department:
        sql += (
            " AND COALESCE(NULLIF(TRIM(COALESCE(s.department,'')),''),"
            "NULLIF(TRIM(COALESCE(s.ward,'')),''),'Unattributed') = ?"
        )
        params.append(department)
    if store_code:
        sql += " AND s.code = ?"
        params.append(store_code.upper())
    sql += " GROUP BY department, s.code, m.item_code ORDER BY department, value_paise DESC LIMIT ?"
    params.append(int(limit))

    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]

    total_value = 0
    total_unvalued = 0
    departments: dict[str, dict] = {}
    for row in rows:
        row["quantity_milli"] = int(row["quantity_milli"] or 0)
        row["quantity_display"] = format_quantity(row["quantity_milli"], row.get("uom"))
        unvalued = int(row.pop("unvalued") or 0)
        row["unvalued_movement_count"] = unvalued
        row["value_paise"] = int(row["value_paise"] or 0)
        row["value_display"] = format_inr_or_unset(
            row["value_paise"] if unvalued == 0 else None, unset="Partly unpriced"
        )
        total_value += row["value_paise"]
        total_unvalued += unvalued
        dept = departments.setdefault(
            row["department"],
            {
                "department": row["department"],
                "value_paise": 0,
                "line_count": 0,
                "unvalued_movement_count": 0,
            },
        )
        dept["value_paise"] += row["value_paise"]
        dept["line_count"] += 1
        dept["unvalued_movement_count"] += unvalued

    for dept in departments.values():
        dept["value_display"] = format_inr_or_unset(
            dept["value_paise"] if dept["unvalued_movement_count"] == 0 else None,
            unset="Partly unpriced",
        )

    return {
        "date_from": start,
        "date_to": end,
        "lines": rows,
        "departments": sorted(departments.values(), key=lambda d: -d["value_paise"]),
        "total_value_paise": total_value,
        "total_value_display": format_inr_or_unset(
            total_value if total_unvalued == 0 else None, unset="Partly unpriced"
        ),
        "unvalued_movement_count": total_unvalued,
        "value_is_complete": total_unvalued == 0,
    }


def expiry_report(
    *,
    within_days: int = 90,
    store_code: str | None = None,
    include_expired: bool = True,
    limit: int = 200,
) -> list[dict]:
    """Batches that are expiring, or already have.

    A LOT IS (item, store, batch, expiry). Its remaining quantity is the SUM of
    every movement carrying that batch — receipts positive, issues negative — so
    a batch that has been fully used nets to zero and DROPS OUT of this view.
    That exclusion is the point: an expiry list that keeps showing lots somebody
    consumed months ago is a list storekeepers learn to ignore, and then they
    ignore the one that matters.

    Only expiry-tracked categories appear at all (see EXPIRY_TRACKED_CATEGORIES).
    Linen and instruments do not expire and must not pad the list.
    """
    horizon = (date.today() + timedelta(days=int(within_days))).isoformat()
    sql = """
        SELECT m.item_code, i.name AS item_name, i.category, i.uom,
               s.code AS store_code, s.name AS store_name,
               m.batch_no, m.expiry_date,
               SUM(m.quantity_milli) AS balance_milli,
               MAX(m.unit_rate_paise) AS unit_rate_paise
          FROM stock_movements m
          LEFT JOIN stores s ON s.id = m.store_id
          LEFT JOIN inventory_items i ON i.code = m.item_code
         WHERE m.expiry_date IS NOT NULL AND m.batch_no IS NOT NULL
           AND m.expiry_date <= ? AND s.pharmacy_kind IS NULL
    """
    params: list[Any] = [horizon]
    if not include_expired:
        sql += " AND m.expiry_date >= ?"
        params.append(date.today().isoformat())
    if store_code:
        sql += " AND s.code = ?"
        params.append(store_code.upper())
    sql += (
        " GROUP BY m.item_code, s.id, m.batch_no, m.expiry_date "
        " HAVING SUM(m.quantity_milli) > 0 "
        " ORDER BY m.expiry_date ASC LIMIT ?"
    )
    params.append(int(limit))

    with db_session() as db:
        rows = [_row(r) for r in db.execute(sql, params).fetchall()]

    today = date.today()
    out: list[dict] = []
    for row in rows:
        if row.get("category") and row["category"] not in EXPIRY_TRACKED_CATEGORIES:
            continue
        balance = int(row["balance_milli"] or 0)
        expiry = date.fromisoformat(str(row["expiry_date"])[:10])
        days = (expiry - today).days
        rate = row.get("unit_rate_paise")
        out.append(
            {
                "item_code": row["item_code"],
                "item_name": row.get("item_name"),
                "category": row.get("category"),
                "uom": row.get("uom"),
                "store_code": row.get("store_code"),
                "store_name": row.get("store_name"),
                "batch_no": row["batch_no"],
                "expiry_date": row["expiry_date"],
                "days_to_expiry": days,
                "is_expired": days < 0,
                "balance_milli": balance,
                "balance_display": format_quantity(balance, row.get("uom")),
                "value_at_risk_paise": (rate * balance) // QUANTITY_SCALE if rate else None,
                "value_at_risk_display": format_inr_or_unset(
                    (rate * balance) // QUANTITY_SCALE if rate else None,
                    unset="Rate not configured",
                ),
            }
        )
    return out


def procurement_overview(*, store_code: str | None = None) -> dict:
    """The one screenful a stores manager opens on: what needs ordering, what is
    on its way, what is about to go off."""
    low = low_stock(store_code=store_code, limit=50)
    expiring = expiry_report(within_days=90, store_code=store_code, limit=50)
    open_pos = list_purchase_orders(open_only=True, limit=50)
    with db_session() as db:
        # The stores desk's documents only (see the Domains note). Vendors are
        # one shared master and are counted whole.
        pending_indents = int(
            db.execute(
                "SELECT COUNT(*) AS c FROM indents WHERE status = 'submitted' AND domain = 'stores'"
            ).fetchone()["c"]
        )
        draft_pos = int(
            db.execute(
                "SELECT COUNT(*) AS c FROM purchase_orders "
                "WHERE status IN ('draft','pending_approval') AND domain = 'stores'"
            ).fetchone()["c"]
        )
        item_count = int(
            db.execute("SELECT COUNT(*) AS c FROM inventory_items WHERE is_active = 1").fetchone()[
                "c"
            ]
        )
        vendor_count = int(
            db.execute(
                "SELECT COUNT(*) AS c FROM vendors WHERE is_active = 1 AND is_blacklisted = 0"
            ).fetchone()["c"]
        )
    outstanding = sum(int(p.get("total_paise") or 0) for p in open_pos)
    return {
        "low_stock": low,
        "low_stock_count": len(low),
        "expiring": expiring,
        "expiring_count": len(expiring),
        "expired_count": sum(1 for e in expiring if e["is_expired"]),
        "open_purchase_orders": open_pos,
        "open_purchase_order_count": len(open_pos),
        "open_purchase_order_value_paise": outstanding,
        "open_purchase_order_value_display": format_inr_or_unset(outstanding),
        "pending_indent_count": pending_indents,
        "draft_purchase_order_count": draft_pos,
        "active_item_count": item_count,
        "active_vendor_count": vendor_count,
        "pharmacy_boundary_note": (
            "Medication stock is not shown here. Drugs are held in Helix "
            "(Pharmacy), which tracks batch, expiry and dispensing against a "
            "prescription. This module covers non-pharmacy stores only."
        ),
    }
