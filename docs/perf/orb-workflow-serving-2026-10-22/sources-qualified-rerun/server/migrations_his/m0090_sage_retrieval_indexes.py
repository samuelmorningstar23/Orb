"""Indexes for latest clinical observations, retaining existing sort semantics.

The partial vitals index excludes voided sets and includes the write id used
to resolve same-timestamp corrections. The risk index has the same tie rule.
Neither changes a clinical row or introduces a cache. Both serve existing
ORDER BY timestamp DESC, id DESC LIMIT queries and department census probes.
"""

from __future__ import annotations

from typing import Any

from migrations import Migration


def apply(conn: Any) -> None:
    """Add read indexes only where the required baseline columns exist."""
    for table, columns, statement in (
        (
            "vitals",
            {"patient_id", "timestamp", "id", "voided_at"},
            (
                "CREATE INDEX IF NOT EXISTS idx_vitals_current_latest "
                "ON vitals(patient_id, timestamp DESC, id DESC) WHERE voided_at IS NULL"
            ),
        ),
        (
            "risk_scores",
            {"patient_id", "timestamp", "id"},
            (
                "CREATE INDEX IF NOT EXISTS idx_risk_latest_id "
                "ON risk_scores(patient_id, timestamp DESC, id DESC)"
            ),
        ),
    ):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if columns <= existing:
            conn.execute(statement)


MIGRATION = Migration(90, "sage_retrieval_indexes", apply)
