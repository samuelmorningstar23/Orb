"""Migration 80: a Sage insight carries when the reading it describes was taken.

Sage's insight cards print "At 10:50 PM: ..." before the detail, and that time
was `updated_at`, which sage_proactive_service stamps with the sweep's clock
each time it rewrites or keeps a card. So a set the ward clerk copied in at
01:51 that the paper sheet times 22:50 read "At 01:51 AM": the time Orb read
the numbers, not the time they were taken. On an Orb Ward Record ward, where
sets are copied in hours after the round, that is the wrong time on a clinical
card.

One nullable column on sage_insights:

* observed_at  the `timestamp` of the vitals row the detail's numbers come
               from (the time on the sheet for a copied set). NULL for an
               insight that describes no reading (antibiotic stewardship) and
               for every row stored before this migration. `updated_at` stays
               as it is and still orders and ages the feed.

Plain ADD COLUMN with no default, so every existing row reads NULL. Idempotent
through PRAGMA introspection. No index: the feed reads it by row.
"""
from __future__ import annotations

from migrations import Migration


def apply(conn) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(sage_insights)").fetchall()}
    if not existing:
        # sage_insights is a baseline table that init_db creates before any
        # migration runs; a database without it has nothing to extend.
        return
    if "observed_at" not in existing:
        conn.execute("ALTER TABLE sage_insights ADD COLUMN observed_at TEXT")


MIGRATION = Migration(80, "sage_insight_observed_at", apply)
