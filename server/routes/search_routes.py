"""Orb Search — global patient search / jump-to-patient.

Router pre-registered in main.py at prefix ``/api/search``.

Exposes a single read-only endpoint used by the Cmd-K command palette to fuzzy
find ADMITTED patients by name, MRN, bed, or diagnosis (plus an exact patient-id
match). Gated on ``patient.read_all`` AND scoped to the caller's department, so
it shows exactly the cohort /api/vigil/patients already shows that user.

That parity is the whole point and it was not true until 2026-08-05: this
docstring claimed "the same permission that guards the Vigil patient list" while
omitting the one line that makes that list safe, so any ward nurse could type
two characters and enumerate the entire admitted census — name, MRN, bed, ward
and diagnosis — hospital-wide. The ids it returned were then the input to the
per-patient chart reads, making it the discovery half of a complete cross-ward
PHI read.
"""
from fastapi import APIRouter, Depends, Request

from permissions import department_scope, log_phi_read, require_permission
from database import db_session

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/patients")
# Plain def on purpose (no awaits): FastAPI runs sync routes in its threadpool,
# so the db_session and the audit write stay off the asyncio event loop.
def search_patients(
    request: Request,
    q: str = "",
    user: dict = Depends(require_permission("patient.read_all")),
):
    """Search ADMITTED patients by name / MRN / bed / diagnosis (or exact id).

    Returns up to 12 lightweight matches for the jump-to-patient palette, from
    the caller's department only. An empty or single-character query
    short-circuits to ``[]`` so we never scan the table for a trivial input.
    """
    query = (q or "").strip()
    if len(query) < 2:
        return []

    # ONE scoping derivation for the whole system — see permissions.department_scope.
    # None means a genuinely unscoped governance account, not "scope unknown".
    dept = department_scope(user)

    # Parameterized LIKE — escape SQL-LIKE wildcards in the user input so a
    # literal '%' or '_' can't turn into a match-everything pattern.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"

    # The department predicate goes in the QUERY, not in a post-filter: a filter
    # applied after `LIMIT 12` would silently drop in-scope patients whenever
    # out-of-scope ones sorted higher.
    dept_clause = "AND a.department = ?" if dept else ""
    dept_param = (dept,) if dept else ()

    from services.vigil_service import scoring_enabled
    _scoring = scoring_enabled()
    with db_session() as db:
        rows = db.execute(
            f"""SELECT p.id, p.name, p.mrn, p.bed, p.ward, p.diagnosis,
                      rs.level as risk_level
               FROM patients p
               JOIN admissions a
                    ON a.patient_id = p.id AND a.status = 'active'
               -- Latest risk row: id breaks an exact timestamp tie. The
               -- MAX(timestamp) equality matched every tied row and returned
               -- the patient twice.
               LEFT JOIN risk_scores rs ON rs.id = (
                    SELECT rs2.id FROM risk_scores rs2
                    WHERE rs2.patient_id = p.id
                    ORDER BY rs2.timestamp DESC, rs2.id DESC LIMIT 1
                )
               WHERE p.status = 'admitted'
                 {dept_clause}
                 AND (
                     p.id = ?
                     OR p.name LIKE ? ESCAPE '\\'
                     OR p.mrn LIKE ? ESCAPE '\\'
                     OR p.bed LIKE ? ESCAPE '\\'
                     OR p.diagnosis LIKE ? ESCAPE '\\'
                 )
               ORDER BY
                   CASE WHEN p.id = ? THEN 0
                        WHEN p.mrn LIKE ? ESCAPE '\\' THEN 1
                        WHEN p.name LIKE ? ESCAPE '\\' THEN 2
                        ELSE 3 END,
                   p.name
               LIMIT 12""",
            dept_param + (query, like, like, like, like, query, like, like),
        ).fetchall()

        results = [
            {
                "id": r["id"],
                "name": r["name"],
                "mrn": r["mrn"],
                "bed": r["bed"],
                "ward": r["ward"],
                # No stored band while scoring is switched off (Ward Record).
                "risk_level": r["risk_level"] if _scoring else None,
                "diagnosis": r["diagnosis"],
            }
            for r in rows
        ]

    # Audited only when PHI actually left — a two-character probe that matches
    # nobody discloses nothing, and the palette fires one request per keystroke.
    # What the trail must be able to answer is "who pulled up whom", so the
    # matched ids go in the metadata.
    if results:
        log_phi_read(
            request=request,
            user=user,
            action_type="patient_search",
            summary_message=(
                f"Patient search returned {len(results)} match(es) in "
                f"{dept or '(all, user has no department on record)'}"
            ),
            target_type="department",
            target_id=dept,
            metadata={
                "result_count": len(results),
                "patient_ids": [r["id"] for r in results],
            },
        )

    return results
