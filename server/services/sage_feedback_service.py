"""Orb Sage — Insight feedback loop (acceptance-rate learning).

Orb already records clinician judgment on every proactive insight — `acted` vs
`dismissed` (sage_proactive_service.transition_insight). That is a labelled
dataset being generated every shift and, until now, used for nothing. This
module closes the loop: it records each outcome keyed by the insight PATTERN and
lets the proactive engine SUPPRESS patterns clinicians consistently reject, so
Sage gets quieter about low-value nudges over time without any model retraining.

Pattern = f"{insight_type}:{severity}" (e.g. "stewardship:info",
"trajectory:warning"). Acceptance rate = acted / (acted + dismissed).

SAFETY GUARDRAIL — suppression is a convenience filter, never a safety valve:
  * CRITICAL-severity insights are NEVER suppressed. You do not mute "patient is
    critical" because it is often dismissed; a dismissed critical alert is a
    clinician decision, not noise. Only 'warning' and 'info' patterns are
    eligible for suppression.
  * Suppression requires a MINIMUM SAMPLE SIZE before it engages, so a single
    early dismissal can't silence a whole pattern.
  * Suppression only lowers NEW insight creation; it never dismisses or hides an
    insight a clinician is already looking at.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from database import db_session

logger = logging.getLogger("orb.sage_feedback")

# Suppression engages only once a pattern has this many graded outcomes...
MIN_SAMPLES = 8
# ...and its acceptance rate is at/below this floor.
SUPPRESS_ACCEPT_FLOOR = 0.15
# Severities that may be suppressed. 'critical' is deliberately absent.
SUPPRESSIBLE_SEVERITIES = frozenset({"warning", "info"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pattern_of(insight_type: str, severity: str) -> str:
    # Strip + lowercase both parts so non-canonical severity ("CRITICAL ", with
    # whitespace/case) can't create a separate bucket that evades the
    # never-suppress-critical guard.
    return f"{(insight_type or 'unknown').strip().lower()}:{(severity or 'info').strip().lower()}"


def record_feedback(pattern: str, outcome: str, *, insight_id: str = None,
                    patient_id: str = None, actor_user_id: str = None,
                    db=None) -> None:
    """Record one graded outcome ('acted' | 'dismissed') for a pattern."""
    if outcome not in ("acted", "dismissed"):
        return

    def _do(conn):
        conn.execute(
            """INSERT INTO insight_feedback (pattern, insight_id, patient_id, outcome, actor_user_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (pattern, insight_id, patient_id, outcome, actor_user_id, _now()),
        )

    try:
        if db is not None:
            _do(db)
        else:
            with db_session() as conn:
                _do(conn)
    except Exception as e:
        # Feedback is best-effort telemetry — never break the transition flow.
        logger.warning("record_feedback failed for %s/%s: %s", pattern, outcome, e)


def pattern_stats(pattern: str, db=None) -> dict:
    """Return {acted, dismissed, total, acceptance_rate} for a pattern."""
    def _do(conn):
        row = conn.execute(
            """SELECT
                   SUM(CASE WHEN outcome='acted' THEN 1 ELSE 0 END) AS acted,
                   SUM(CASE WHEN outcome='dismissed' THEN 1 ELSE 0 END) AS dismissed
               FROM insight_feedback WHERE pattern = ?""",
            (pattern,),
        ).fetchone()
        acted = (row["acted"] or 0) if row else 0
        dismissed = (row["dismissed"] or 0) if row else 0
        total = acted + dismissed
        return {
            "pattern": pattern,
            "acted": acted,
            "dismissed": dismissed,
            "total": total,
            "acceptance_rate": (acted / total) if total else None,
        }

    try:
        if db is not None:
            return _do(db)
        with db_session() as conn:
            return _do(conn)
    except Exception as e:
        logger.warning("pattern_stats failed for %s: %s", pattern, e)
        return {"pattern": pattern, "acted": 0, "dismissed": 0, "total": 0, "acceptance_rate": None}


def is_pattern_suppressed(insight_type: str, severity: str, db=None) -> bool:
    """Should the proactive engine SKIP creating an insight of this pattern?

    True only when: severity is suppressible (never 'critical'), the pattern has
    at least MIN_SAMPLES graded outcomes, and its acceptance rate is at/below
    SUPPRESS_ACCEPT_FLOOR.
    """
    sev = (severity or "info").strip().lower()
    if sev not in SUPPRESSIBLE_SEVERITIES:
        return False
    stats = pattern_stats(pattern_of(insight_type, sev), db=db)
    if stats["total"] < MIN_SAMPLES or stats["acceptance_rate"] is None:
        return False
    return stats["acceptance_rate"] <= SUPPRESS_ACCEPT_FLOOR


def acceptance_overview(db=None) -> list:
    """All patterns with their acceptance stats + whether they're suppressed —
    the data behind the trust/governance 'is Sage's advice useful?' surface."""
    def _do(conn):
        rows = conn.execute(
            """SELECT pattern,
                      SUM(CASE WHEN outcome='acted' THEN 1 ELSE 0 END) AS acted,
                      SUM(CASE WHEN outcome='dismissed' THEN 1 ELSE 0 END) AS dismissed
               FROM insight_feedback GROUP BY pattern ORDER BY (acted+dismissed) DESC"""
        ).fetchall()
        out = []
        for r in rows:
            acted, dismissed = (r["acted"] or 0), (r["dismissed"] or 0)
            total = acted + dismissed
            rate = (acted / total) if total else None
            sev = r["pattern"].split(":")[-1] if ":" in r["pattern"] else "info"
            suppressed = (
                sev in SUPPRESSIBLE_SEVERITIES
                and total >= MIN_SAMPLES
                and rate is not None
                and rate <= SUPPRESS_ACCEPT_FLOOR
            )
            out.append({
                "pattern": r["pattern"], "acted": acted, "dismissed": dismissed,
                "total": total, "acceptance_rate": rate, "suppressed": suppressed,
            })
        return out

    try:
        if db is not None:
            return _do(db)
        with db_session() as conn:
            return _do(conn)
    except Exception as e:
        logger.warning("acceptance_overview failed: %s", e)
        return []
