"""The one reader of live lab results in the ``observations`` table.

WHY ONE READER. Four places read structured results: the chart's lab card
(routes/vigil_routes.py), the downtime ward pack (services/downtime_service.py),
the Tier 1 creatinine lane (services/patient_specific_screen.py) and the chart
timeline (services/patient_timeline_service.py). Each wrote its own SQL, and
they drifted apart: the card and the pack left voided results out and ordered
by the instant, while the Tier 1 lane ordered by the raw string, read voided
rows and had no tie-break, and the timeline showed voided rows. A result a lab
withdrew could still raise a renal flag. Every rule now lives here, once.

THE RULES
  * Live rows only. A row the sender withdrew or marked in error, and a row a
    later correction superseded, is not a result anyone acts on. That is
    VOID_OBSERVATION_STATUSES, compared without case or surrounding spaces,
    with a blank status read as 'final'. A superseded row is marked by its
    status; the superseded_by pointer column arrives with the lab links
    migration and changes nothing here.
  * Time is julianday(COALESCE(observed_at, recorded_at)), never the raw
    string. Interfaced rows keep observed_at as received, with a 'T' or a
    space, an offset or none (migration 43 canonicalised only vitals and
    risk_scores), so as strings '2026-10-03T09:30:00+05:30' (04:00 UTC) sorts
    after '2026-10-03 05:00:00'. A stamp with no offset is read as UTC, as
    everywhere else in Orb.
  * Equal instants break by id DESC: the later write is the newer result.
  * Simulated rows (source 'simulated') are returned unless the caller asks
    for them to be left out, which is what the chart has always done: the
    screen labels them. Leaving them out silently would make a demo ward's
    labs, and any alert they drive, vanish without a word.
  * display_value(): value_text when it is not blank, else value_numeric. The
    text carries the lab's own reporting precision ('1.20', '<0.01'); the
    number exists for plotting and comparing.

THE GUARD. tests/test_lab_observation_readers.py fails on any read of
observations in services/ or routes/ outside this module (a FROM or JOIN
naming the table, or a declarative from_sql string naming it). The allow-list
there names the writers and the admin row counts, which are not result reads.

Every function takes an open connection and never opens or commits one, so a
caller keeps its own session and snapshot. Statuses and the simulated label
are module constants spliced into the SQL as literals, never request text, so
a fragment can be used where no bound parameters are allowed (the timeline's
count union counts its own placeholders).
"""
from __future__ import annotations

import re
from typing import Any, Optional

#: Statuses that mean "not a result to act on". HL7 OBX-11 X (cannot be
#: obtained), D (deleted) and W (wrong patient or withdrawn), the FHIR and
#: free-text spellings the importers and adapters write, and 'superseded'
#: for a result a later correction replaced.
VOID_OBSERVATION_STATUSES: tuple[str, ...] = (
    "x", "d", "w", "cancelled", "deleted", "entered-in-error",
    "entered_in_error", "wrong", "superseded",
)

#: The provenance label a simulator writes on its rows (vitals_source.base).
SIMULATED_SOURCE = "simulated"

_LITERAL_RE = re.compile(r"^[a-z_\-]+$")
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Spliced as SQL literals below, so they must stay plain words.
for _word in (*VOID_OBSERVATION_STATUSES, SIMULATED_SOURCE):
    if not _LITERAL_RE.match(_word):  # pragma: no cover - a constant typo
        raise ValueError(f"lab_observations constant is not a plain word: {_word!r}")


def _prefix(alias: str) -> str:
    if not alias:
        return ""
    if not _ALIAS_RE.match(alias):
        raise ValueError(f"not a table alias: {alias!r}")
    return f"{alias}."


def from_sql(alias: str = "ob") -> str:
    """The table and its alias, for a reader that declares its FROM clause
    as data (the chart timeline's sources)."""
    _prefix(alias)
    return f"observations {alias}" if alias else "observations"


def observed_instant_sql(alias: str = "") -> str:
    """The instant a result belongs to: when it was observed, else when it was
    recorded."""
    a = _prefix(alias)
    return f"COALESCE({a}observed_at, {a}recorded_at)"


def order_by_sql(alias: str = "") -> str:
    """Newest first by the instant, then by id: the one ordering of results."""
    a = _prefix(alias)
    return f"julianday({observed_instant_sql(alias)}) DESC, {a}id DESC"


def live_observations_sql(alias: str = "", *, exclude_simulated: bool = False) -> str:
    """A WHERE fragment keeping live results only.

    With ``exclude_simulated`` the simulator's rows are left out too. Only a
    caller that has a reason asks for that; see the module rules.
    """
    a = _prefix(alias)
    statuses = ", ".join(f"'{s}'" for s in VOID_OBSERVATION_STATUSES)
    sql = f"LOWER(TRIM(COALESCE({a}status, 'final'))) NOT IN ({statuses})"
    if exclude_simulated:
        sql += f" AND COALESCE({a}source, '') != '{SIMULATED_SOURCE}'"
    return sql


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def display_value(row: Any) -> Any:
    """What to print for a result: its text when there is any, else its number.

    Returns the stored text unchanged, or the number, or None when the row
    holds neither.
    """
    text = _get(row, "value_text")
    if text is not None and str(text).strip():
        return text
    return _get(row, "value_numeric")


# ── The readers ──────────────────────────────────────────────────────────────

def chart_lab_rows(db, patient_id: str, *, per_analyte: int, max_rows: int) -> list:
    """The chart's lab card: numeric results, the newest ``per_analyte`` of
    each analyte, cut to ``max_rows`` by recency rank so every analyte keeps
    its latest result before any keeps a longer trend. Returned oldest first,
    because the card takes the reference range from the newest row.

    An analyte is grouped as buildLabTrends groups it: name and unit.
    observed_at in the result is the instant (observed, else recorded).
    """
    return db.execute(
        f"""SELECT id, observation_name, value_numeric, unit, reference_range,
                   abnormal_flag, status, source, observed_at
            FROM (
                SELECT * FROM (
                    SELECT id, observation_name, value_numeric, unit,
                           reference_range, abnormal_flag, status, source,
                           {observed_instant_sql()} AS observed_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY LOWER(TRIM(observation_name)),
                                            TRIM(COALESCE(unit, ''))
                               ORDER BY {order_by_sql()}
                           ) AS recency
                    FROM observations
                    WHERE patient_id = ? AND value_numeric IS NOT NULL
                      AND {live_observations_sql()}
                )
                WHERE recency <= ?
                ORDER BY recency ASC, julianday(observed_at) DESC, id DESC
                LIMIT ?
            )
            ORDER BY julianday(observed_at) ASC, id ASC""",
        (patient_id, int(per_analyte), int(max_rows)),
    ).fetchall()


def latest_per_analyte(db, patient_id: str, *, limit: int) -> list:
    """The newest result of each analyte, newest first, for the ward pack.

    A result with text and no number counts (a culture, '<0.01').
    observed_at in the result is the instant (observed, else recorded).
    """
    return db.execute(
        f"""SELECT observation_name, value_numeric, value_text, unit,
                   reference_range, abnormal_flag, status, observed_at
            FROM (
                SELECT observation_name, value_numeric, value_text, unit,
                       reference_range, abnormal_flag, status,
                       {observed_instant_sql()} AS observed_at, id,
                       ROW_NUMBER() OVER (
                           PARTITION BY LOWER(TRIM(observation_name)),
                                        TRIM(COALESCE(unit, ''))
                           ORDER BY {order_by_sql()}
                       ) AS recency
                FROM observations
                WHERE patient_id = ?
                  AND (value_numeric IS NOT NULL
                       OR TRIM(COALESCE(value_text, '')) != '')
                  AND {live_observations_sql()}
            )
            WHERE recency = 1
            ORDER BY julianday(observed_at) DESC, id DESC
            LIMIT ?""",
        (patient_id, int(limit)),
    ).fetchall()


def recent_observations(db, patient_id: str, *, limit: int,
                        exclude_simulated: bool = False) -> list:
    """This patient's most recent live results, newest first, every analyte.

    observed_at and recorded_at come back as stored; the caller decides which
    to show.
    """
    return db.execute(
        f"""SELECT id, observation_code, observation_name, value_numeric,
                   value_text, unit, reference_range, abnormal_flag, status,
                   source, observed_at, recorded_at
            FROM observations
            WHERE patient_id = ?
              AND {live_observations_sql(exclude_simulated=exclude_simulated)}
            ORDER BY {order_by_sql()}
            LIMIT ?""",
        (patient_id, int(limit)),
    ).fetchall()


def previous_result(db, patient_id: str, code: str, before_iso: str, *,
                    exclude_simulated: bool = False) -> Optional[Any]:
    """The newest live result for ``code`` observed strictly before
    ``before_iso``, or None when there is none.

    Compared by julianday, so the spellings of the two stamps do not matter.
    An unparseable ``before_iso`` raises ValueError rather than answering
    "no previous result", which would quietly skip whatever check asked.
    """
    if not patient_id or not code:
        return None
    if before_iso is None or db.execute(
            "SELECT julianday(?)", (str(before_iso),)).fetchone()[0] is None:
        raise ValueError(f"unparseable time for a previous result: {before_iso!r}")
    return db.execute(
        f"""SELECT id, observation_code, observation_name, value_numeric,
                   value_text, unit, reference_range, abnormal_flag, status,
                   source, observed_at, recorded_at
            FROM observations
            WHERE patient_id = ? AND observation_code = ?
              AND {live_observations_sql(exclude_simulated=exclude_simulated)}
              AND julianday({observed_instant_sql()}) < julianday(?)
            ORDER BY {order_by_sql()}
            LIMIT 1""",
        (patient_id, code, str(before_iso)),
    ).fetchone()


# ── The opt-in lab module seam ───────────────────────────────────────────────

def lab_module_on() -> bool:
    """True when the opt-in lab module (ORB_LAB) is switched on.

    Read from config at call time, so a test or a restart sees the current
    value. Absent or anything but True reads as off: the module is opt-in.
    """
    try:
        import config
    except Exception:  # pragma: no cover - config always imports in the app
        return False
    return getattr(config, "LAB_ENABLED", False) is True


def tier1_excludes_simulated() -> bool:
    """Whether the Tier 1 creatinine lane leaves simulated results out.

    Only with the lab module on. With it off (the default) the lane reads a
    simulated creatinine as it always has, because a demo ward's labs are
    simulated and its renal flags would otherwise vanish without a word.
    The document lane's own simulated rule (migration 46) is separate and
    unchanged.
    """
    return lab_module_on()
