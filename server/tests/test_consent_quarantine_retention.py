"""result_quarantine retention honours the minority protection.

consent_service pre-registers ('result_quarantine', None, 'created_at') under
clinical_record, which is minority-protected (Limitation Act 1963 s.6). With no
patient column, retention_review used to take the unprotected branch and count
every overdue quarantine row as destroyable, although an unmatched result may
be a child's. A patient-less table in a protected category is now held back in
full as age-unknown and never counted as overdue.

The table does not exist yet on a real database (the LIS migration creates
it), so these tests run retention_review against a scratch database that holds
the real retention policies, a patients table, a clinical_documents table and a
result_quarantine table.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import database
from services import consent_service as cs

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
LONG_AGO = "2001-01-01 00:00:00"
RECENT = "2026-10-01 09:00:00"


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    # The real policies, read from the suite database before patching.
    with database.db_session() as real:
        policies = [dict(r) for r in real.execute(
            "SELECT category, description, retention_days, anchor, legal_basis, "
            "authority, erasable, disposition FROM retention_policies").fetchall()]
    assert any(p["category"] == "clinical_record" and p["retention_days"] for p in policies)

    conn = sqlite3.connect(str(tmp_path / "quarantine_retention.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE retention_policies (
            category TEXT PRIMARY KEY, description TEXT NOT NULL, retention_days INTEGER,
            anchor TEXT NOT NULL, legal_basis TEXT NOT NULL, authority TEXT NOT NULL,
            erasable INTEGER NOT NULL DEFAULT 0, disposition TEXT NOT NULL);
        CREATE TABLE patients (id TEXT PRIMARY KEY, date_of_birth TEXT, age INTEGER);
        CREATE TABLE clinical_documents (
            id INTEGER PRIMARY KEY, patient_id TEXT, created_at TEXT);
        CREATE TABLE result_quarantine (
            id INTEGER PRIMARY KEY, created_at TEXT, status TEXT,
            attached_patient_id TEXT);
    """)
    conn.executemany(
        "INSERT INTO retention_policies VALUES (:category, :description, :retention_days, "
        ":anchor, :legal_basis, :authority, :erasable, :disposition)", policies)
    conn.executemany("INSERT INTO patients (id, date_of_birth, age) VALUES (?, ?, ?)",
                     [("ADULT", None, 54), ("CHILD", None, 9)])
    conn.commit()

    @contextmanager
    def _session():
        yield conn
        conn.commit()

    monkeypatch.setattr(cs, "db_session", _session)
    yield conn
    conn.close()


def _clinical(review):
    return {c["category"]: c for c in review["categories"]}["clinical_record"]


def test_registration_is_pinned_under_a_protected_category():
    assert ("result_quarantine", None, "created_at") in cs._RETENTION_SOURCES["clinical_record"]
    assert "clinical_record" in cs._MINORITY_PROTECTED_CATEGORIES
    # Not quietly moved to an unprotected category either.
    for category, rows in cs._RETENTION_SOURCES.items():
        if category != "clinical_record":
            assert all(t != "result_quarantine" for t, _, _ in rows), category


def test_overdue_quarantine_rows_are_held_back_never_listed(scratch):
    scratch.executemany(
        "INSERT INTO result_quarantine (created_at, status) VALUES (?, ?)",
        [(LONG_AGO, "open"), (LONG_AGO, "discarded"), (LONG_AGO, "open"), (RECENT, "open")])
    scratch.commit()

    clinical = _clinical(cs.retention_review(now=NOW))
    assert clinical["by_table"]["result_quarantine"] == 0
    assert clinical["overdue_count"] == 0, (
        "an unmatched result may be a child's; it must not reach a destruction list")
    mp = clinical["minority_protection"]
    # The three overdue rows are held and said; the recent one is not overdue at all.
    assert mp["held_age_unknown"] == 3
    assert mp["held_minor"] == 0
    assert mp["held_total"] == 3
    assert "HELD BACK" in clinical["note"]


def test_the_patient_linked_branch_is_unchanged_beside_it(scratch):
    scratch.executemany(
        "INSERT INTO clinical_documents (patient_id, created_at) VALUES (?, ?)",
        [("ADULT", LONG_AGO), ("CHILD", LONG_AGO)])
    scratch.execute("INSERT INTO result_quarantine (created_at, status) VALUES (?, 'open')",
                    (LONG_AGO,))
    scratch.commit()

    clinical = _clinical(cs.retention_review(now=NOW))
    assert clinical["by_table"]["clinical_documents"] == 1   # the adult's note
    assert clinical["overdue_count"] == 1
    mp = clinical["minority_protection"]
    assert mp["held_minor"] == 1          # the child's note
    assert mp["held_age_unknown"] == 1    # the quarantined result


def test_a_patient_scoped_review_skips_the_quarantine_and_says_so(scratch):
    scratch.execute("INSERT INTO result_quarantine (created_at, status) VALUES (?, 'open')",
                    (LONG_AGO,))
    scratch.commit()
    clinical = _clinical(cs.retention_review(now=NOW, patient_id="ADULT"))
    assert "result_quarantine" in clinical.get("not_patient_scoped", [])
    assert "result_quarantine" not in clinical["by_table"]
    assert clinical["minority_protection"]["held_age_unknown"] == 0


def test_the_review_still_deletes_nothing(scratch):
    scratch.execute("INSERT INTO result_quarantine (created_at, status) VALUES (?, 'open')",
                    (LONG_AGO,))
    scratch.commit()
    review = cs.retention_review(now=NOW)
    assert review["deleted_anything"] is False
    assert scratch.execute("SELECT COUNT(*) FROM result_quarantine").fetchone()[0] == 1


def test_naming_the_future_patient_link_keeps_unlinked_rows_held(scratch, monkeypatch):
    """What the registration comment promises for when LIS adds its patient
    link: attached rows are aged through the normal branch, and open or
    discarded rows (link still NULL) stay held as age-unknown."""
    sources = {k: list(v) for k, v in cs._RETENTION_SOURCES.items()}
    sources["clinical_record"] = [
        ("result_quarantine", "attached_patient_id", "created_at")
        if t == "result_quarantine" else (t, p, c)
        for t, p, c in sources["clinical_record"]]
    monkeypatch.setattr(cs, "_RETENTION_SOURCES", sources)
    scratch.executemany(
        "INSERT INTO result_quarantine (created_at, status, attached_patient_id) VALUES (?, ?, ?)",
        [(LONG_AGO, "attached", "ADULT"), (LONG_AGO, "attached", "CHILD"),
         (LONG_AGO, "open", None), (LONG_AGO, "discarded", None)])
    scratch.commit()

    clinical = _clinical(cs.retention_review(now=NOW))
    assert clinical["by_table"]["result_quarantine"] == 1
    mp = clinical["minority_protection"]
    assert mp["held_minor"] == 1
    assert mp["held_age_unknown"] == 2
