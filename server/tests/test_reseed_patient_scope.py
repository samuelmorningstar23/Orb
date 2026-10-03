"""What a reseed must take with the patients it deletes.

`scripts/reset_db_and_seed.patient_scoped_tables` derives the wipe set from the
LIVE SCHEMA on purpose: every table with a `patient_id`, plus the transitive
foreign-key closure onto those. Its own docstring says why, and names the failure
it was written against: "orphan sage_insights / patient_snapshots / orders /
event_log rows referencing patients that no longer exist".

THE GAP THIS FILE PINS (found 2026-09-07). That derivation keys on SCHEMA SHAPE,
so patient data that lives in FREE TEXT, with no patient_id and no foreign key
into anything that has one, is outside it by construction. Four tables were:

    sage_conversations      689 rows    the transcripts themselves
    sage_provenance_refs    650 rows    child of sage_provenance_turns, which IS wiped
    sage_provenance_blobs   431 rows    the prompt/answer bodies those refs point at
    sage_history_summaries   70 rows    cached summaries of the transcripts

So a reseed deleted every patient and kept the conversations describing them, and
in two cases produced the exact orphan shape the docstring exists to prevent: the
provenance PARENT was wiped and its children were not.

WHY THESE TESTS ASSERT AN INVARIANT AND NOT A LIST. Pinning "these four tables
are wiped" would pass forever while the next Sage table to carry a transcript
slips through exactly as these did. So the rules below are: every `sage_*` table
is wiped unless someone has deliberately declared otherwise, and a child table
joined to a wiped parent by a plain column (no FK to enforce it) must be wiped
with it. Both fail loudly on a table nobody thought about, which is the only
failure mode that matters here.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import db_session
from scripts.reset_db_and_seed import (
    EXTRA_TABLES_TO_CLEAR,
    TABLES_TO_KEEP,
    patient_scoped_tables,
)

#: Sage tables a reseed deliberately KEEPS. Empty, and that is the point: a new
#: `sage_*` table has to be argued into here rather than falling through in
#: silence. Anything added needs a comment saying why its rows may outlive the
#: patients they describe.
SAGE_TABLES_DELIBERATELY_KEPT: set[str] = set()

#: Parent/child pairs joined by a plain column with NO foreign key to enforce it,
#: so `patient_scoped_tables`'s FK closure cannot see the relationship. Each entry
#: is (parent, child, join column). The test checks the column really exists on
#: both, so a migration that renames it fails here instead of quietly unpinning
#: the pair.
UNENFORCED_PARENT_CHILD = [
    ("sage_provenance_turns", "sage_provenance_refs", "turn_id"),
    ("sage_conversations", "sage_history_summaries", "conversation_id"),
]


@pytest.fixture(scope="module", autouse=True)
def _lazy_schemas():
    """Three of the four tables here are created by their SERVICE on first use,
    not by migrations, so a fresh test DB does not have them and
    `patient_scoped_tables` skips any declared name it cannot find
    (`if t in columns`). A real box has run Sage and does have them. Build them
    first, or this file would pass by testing a schema that is missing exactly
    the tables it exists to pin."""
    from services import sage_history_service, sage_provenance_service
    sage_provenance_service.ensure_schema()
    with db_session() as db:
        sage_history_service._ensure_schema(db)


@pytest.fixture(scope="module")
def wiped():
    with db_session() as db:
        return set(patient_scoped_tables(db))


@pytest.fixture(scope="module")
def tables():
    with db_session() as db:
        return {
            r[0]: [c[1] for c in db.execute(f'PRAGMA table_info("{r[0]}")').fetchall()]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }


# ── The gap itself ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("table", [
    "sage_conversations",
    "sage_history_summaries",
    "sage_provenance_refs",
    "sage_provenance_blobs",
])
def test_sage_free_text_tables_are_wiped_with_their_patients(table, wiped, tables):
    """THE REGRESSION. Each of these held patient data in free text and survived
    every reseed, because none has a patient_id for the walk to find."""
    assert table in tables, f"{table} is gone from the schema; revisit this file"
    assert "patient_id" not in tables[table], (
        f"{table} now HAS a patient_id, so the schema walk reaches it on its own "
        f"and its entry in EXTRA_TABLES_TO_CLEAR is redundant rather than wrong"
    )
    assert table in wiped, (
        f"{table} survives a reseed, so a demo box keeps transcripts describing "
        f"patients it just deleted"
    )


# ── The invariants that keep it fixed ─────────────────────────────────────────

def test_every_sage_table_is_wiped_or_deliberately_kept(wiped, tables):
    """The rule, rather than the four names. A new Sage table that carries a
    transcript must not be able to slip through the way these did: it is either
    wiped, or someone has written down why it is not."""
    sage = {t for t in tables if t.startswith("sage_")}
    assert sage, "no sage_* tables found; the schema or this rule has moved"
    unhandled = sage - wiped - SAGE_TABLES_DELIBERATELY_KEPT
    assert not unhandled, (
        f"{sorted(unhandled)} survive a reseed and nobody has said why. Either add "
        f"them to EXTRA_TABLES_TO_CLEAR, or declare them in "
        f"SAGE_TABLES_DELIBERATELY_KEPT with a reason."
    )


@pytest.mark.parametrize("parent,child,join_col", UNENFORCED_PARENT_CHILD)
def test_a_wiped_parent_never_leaves_its_children_behind(parent, child, join_col,
                                                         wiped, tables):
    """The orphan rule, generalised past Sage. These pairs are joined by a plain
    column with no foreign key, so the FK closure is blind to them and only this
    keeps them in step. Both halves of the assertion matter: the join column has
    to still exist, or the pair is pinned in name only."""
    assert parent in tables and child in tables, f"{parent}/{child} moved"
    assert join_col in tables[parent] and join_col in tables[child], (
        f"{parent}.{join_col} -> {child}.{join_col} no longer holds; the pairing "
        f"in this file is stale and the orphan rule below is not testing anything"
    )
    if parent in wiped:
        assert child in wiped, (
            f"a reseed wipes {parent} and keeps {child}, which is the orphan shape "
            f"patient_scoped_tables' own docstring exists to prevent"
        )


def test_the_provenance_blob_store_goes_with_its_refs(wiped):
    """`sage_provenance_blobs` is content-addressed and reached only through
    `sage_provenance_refs`, so once the refs go every blob is unreachable. Wiping
    the refs and keeping the bodies would leave the prompts and answers on disk
    with nothing pointing at them."""
    assert ("sage_provenance_refs" in wiped) == ("sage_provenance_blobs" in wiped)


def test_the_audit_chain_is_still_never_wiped(wiped):
    """The guarantee this change must not have widened its way through. A reseed
    RE-SEALS the tamper-evident chain; it does not delete it."""
    assert "audit_logs" in TABLES_TO_KEEP
    assert "audit_logs" not in wiped


def test_every_declared_extra_still_exists(tables):
    """EXTRA_TABLES_TO_CLEAR is the hand-maintained half of an otherwise derived
    set, so it is the half that can rot. A name that no longer matches a table is
    silently skipped by the walk (`if t in columns`), which is exactly how a
    deletion would stop being noticed."""
    missing = [t for t in EXTRA_TABLES_TO_CLEAR if t not in tables]
    assert not missing, f"declared but not in the schema: {missing}"
