"""Migration 78: the ward's observation interval, and the log of rounds files.

The Orb Ward Record promises the quality coordinator a monthly file showing,
shift by shift, which observation rounds were due, done, late or missed, and
which readings were left blank (docs/fundraising/SALES_PLAN_2026-10.md section
3, step 2). The due time comes from ONE fixed interval per ward, set in writing
by the nursing superintendent, never from anything Orb works out about the
patient. Two tables:

* ward_observation_settings  one row per ward: the interval and the grace
  window in minutes, who set them in writing (set_by), when they were keyed
  (set_at, UTC) and an optional note (for example the memo number). A ward
  with no row has no rounds file: the service refuses rather than guess.
* ward_record_exports  one row per rounds file produced: a reference, the
  ward and period, who produced it, when, in which format, and the SHA-256 of
  the exact document, so "is this the file we gave the quality team in
  March" can be answered.

Both CHECKs are the limits the service also enforces: an interval of 30
minutes to 24 hours, a grace window of 0 to 4 hours. Plain CREATE TABLE IF NOT
EXISTS, no foreign key, idempotent.
"""
from __future__ import annotations

from migrations import Migration

SETTINGS_DDL = """CREATE TABLE IF NOT EXISTS ward_observation_settings (
        ward TEXT PRIMARY KEY,
        interval_minutes INTEGER NOT NULL
            CHECK (interval_minutes BETWEEN 30 AND 1440),
        grace_minutes INTEGER NOT NULL DEFAULT 30
            CHECK (grace_minutes BETWEEN 0 AND 240),
        set_by TEXT NOT NULL,
        set_at TEXT NOT NULL,
        note TEXT
    )"""

EXPORTS_DDL = """CREATE TABLE IF NOT EXISTS ward_record_exports (
        reference TEXT PRIMARY KEY,
        ward TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        generated_by TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        format TEXT NOT NULL,
        sha256 TEXT NOT NULL
    )"""

EXPORTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_ward_record_exports_ward "
    "ON ward_record_exports(ward, generated_at)"
)


def apply(conn) -> None:
    conn.execute(SETTINGS_DDL)
    conn.execute(EXPORTS_DDL)
    conn.execute(EXPORTS_INDEX)


MIGRATION = Migration(78, "ward_record_rounds", apply)
