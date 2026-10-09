"""GST retention mapping for purchase documents (grn-18, W8-F, with W168).

consent_service registers purchase orders, goods receipts (both desks) and the
pharmacy's returns to vendor under the 'gst_tax_record' retention category
(CGST Act 2017 s.36: 72 months from the annual return's due date). The
category's retention_policies row is W168's to add by migration; until it
exists the review does not show the category, so nothing anyone sees today
changes. These tests pin both halves against a scratch database: the mapping's
columns exist on the real schema, the review lists the new sources once the
policy row exists, never counts them for one patient, and deletes nothing.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import database
from services import consent_service as cs

NOW = datetime(2034, 1, 1, tzinfo=timezone.utc)
LONG_AGO = "2026-04-01 09:00:00"
RECENT = "2033-12-01 09:00:00"

GST_SOURCES = {("purchase_orders", None, "created_at"),
               ("goods_receipts", None, "received_at"),
               ("pharmacy_vendor_returns", None, "created_at")}


def test_the_purchase_documents_are_registered_under_the_gst_category():
    assert cs.GST_RETENTION_CATEGORY == "gst_tax_record"
    assert set(cs._RETENTION_SOURCES[cs.GST_RETENTION_CATEGORY]) == GST_SOURCES
    # Not under a minority-protected clinical category: they are nobody's chart.
    assert cs.GST_RETENTION_CATEGORY not in cs._MINORITY_PROTECTED_CATEGORIES


def test_the_mapped_columns_exist_on_the_real_schema():
    with database.db_session() as db:
        for table, _pcol, tcol in GST_SOURCES:
            cols = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")').fetchall()}
            assert tcol in cols, f"{table} has no {tcol}"


def test_without_its_policy_row_the_review_does_not_show_the_category():
    review = cs.retention_review()
    assert cs.GST_RETENTION_CATEGORY not in {c["category"] for c in review["categories"]}
    assert review["deleted_anything"] is False


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    conn = sqlite3.connect(str(tmp_path / "gst_retention.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE retention_policies (
            category TEXT PRIMARY KEY, description TEXT NOT NULL, retention_days INTEGER,
            anchor TEXT NOT NULL, legal_basis TEXT NOT NULL, authority TEXT NOT NULL,
            erasable INTEGER NOT NULL DEFAULT 0, disposition TEXT NOT NULL);
        CREATE TABLE patients (id TEXT PRIMARY KEY, date_of_birth TEXT, age INTEGER);
        CREATE TABLE purchase_orders (id INTEGER PRIMARY KEY, created_at TEXT, domain TEXT);
        CREATE TABLE goods_receipts (id INTEGER PRIMARY KEY, received_at TEXT, domain TEXT);
        CREATE TABLE pharmacy_vendor_returns (id INTEGER PRIMARY KEY, created_at TEXT);
    """)
    # The row W168's migration will insert (its day count is W168's to set;
    # this one is only long enough for the dates below).
    conn.execute(
        "INSERT INTO retention_policies VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (cs.GST_RETENTION_CATEGORY, "GST books of account: purchase documents.", 2555,
         "record_created", "legal_obligation", "CGST Act 2017 s.36", "retain"))
    conn.executemany("INSERT INTO purchase_orders (created_at, domain) VALUES (?, ?)",
                     [(LONG_AGO, "stores"), (LONG_AGO, "pharmacy"), (RECENT, "pharmacy")])
    conn.executemany("INSERT INTO goods_receipts (received_at, domain) VALUES (?, ?)",
                     [(LONG_AGO, "pharmacy"), (RECENT, "stores")])
    conn.execute("INSERT INTO pharmacy_vendor_returns (created_at) VALUES (?)", (LONG_AGO,))
    conn.commit()

    @contextmanager
    def _session():
        yield conn
        conn.commit()

    monkeypatch.setattr(cs, "db_session", _session)
    yield conn
    conn.close()


def _gst(review):
    return {c["category"]: c for c in review["categories"]}[cs.GST_RETENTION_CATEGORY]


def test_the_review_lists_the_new_sources_once_the_policy_exists(scratch):
    review = cs.retention_review(now=NOW)
    gst = _gst(review)
    assert gst["measurable"] is True
    assert gst["by_table"] == {"purchase_orders": 2, "goods_receipts": 1,
                               "pharmacy_vendor_returns": 1}
    assert gst["overdue_count"] == 4
    assert "minority_protection" not in gst
    assert review["deleted_anything"] is False
    assert scratch.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 3


def test_a_patient_review_never_counts_purchase_documents_as_theirs(scratch):
    gst = _gst(cs.retention_review(now=NOW, patient_id="PAT-1"))
    assert gst["by_table"] == {}
    assert set(gst["not_patient_scoped"]) == {t for t, _p, _c in GST_SOURCES}
