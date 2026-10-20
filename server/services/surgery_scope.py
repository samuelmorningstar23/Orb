"""Orb Surgery — WARD SCOPE FOR THE SURGICAL MODULE.

═══════════════════════════════════════════════════════════════════════════════
 THE RULE, STATED. Read this before changing anything below.
═══════════════════════════════════════════════════════════════════════════════

Every other PHI surface in Orb answers "may this account see this patient?" with
one question: is the patient admitted to the caller's department
(``permissions.PATIENT_DEPARTMENT_SQL``). Surgery cannot answer it that way, and
bolting the plain ward filter on would have broken the theatre:

  * A General Medicine patient is operated on by a General Surgery surgeon. The
    surgeon is never in the patient's department, by construction.
  * An anaesthetist covers EVERY ward. Ward-scoping an anaesthetist to
    "Anesthesiology" scopes them to a department that admits nobody — they would
    see an empty list, on a screen whose whole job is the day's operating list.
  * An emergency case is visible in the queue BEFORE anyone is assigned to it.
    A rule of "only cases you are on" cannot bootstrap: nobody can be assigned
    to a case nobody can see.

So the surgical rule is a UNION of four clauses. A caller may read a surgical
case when ANY of them holds:

  (A) HOSPITAL-WIDE GOVERNANCE — ``department_scope(user) is None``. The
      pre-existing convention (permissions.department_scope): an account with no
      department on record — the seeded `admin` — is not ward-scoped anywhere,
      and surgery does not invent an exception to that.

  (B) PERIOPERATIVE STAFF — the caller's department is a theatre department
      (General Surgery, Anesthesiology, …; see ``is_perioperative_department``).
      These accounts read the WHOLE theatre list, hospital-wide, because theatre
      serves every ward. This is the judgement call in this file, so it is
      stated rather than implied:

          SURGICAL AND ANAESTHETIC STAFF ARE HOSPITAL-WIDE ON THE SURGICAL
          MODULE BY ROLE. That is a property of the USER ROW (users.department),
          which only an administrator can change, and it is now RECORDED ON
          EVERY phi_access ROW as `surgical_scope: "perioperative"`.

      Before this file the same access existed — but it was accidental and
      silent: `surgery.read` is held by *every* doctor, *every* nurse and admin
      (permissions.ROLE_PERMISSIONS), the routes carried no scope at all, and
      the reads wrote no audit row. So a Cardiology nurse read every ward's
      surgical patients — name, MRN, age, sex, bed, diagnosis, comorbidities —
      and left no trace. The fix is not to remove hospital-wide surgical access,
      which the theatre genuinely needs; it is to narrow it to the accounts
      whose job it is, and to make what remains explicit and auditable.

      NOTE THE NARROWING this achieves in the seeded hospital: hospital-wide
      surgical read goes from 12 clinical accounts to 3 (General Surgery ×2,
      Anesthesiology ×2 — minus overlap) plus the unscoped governance admin.

  (C) THE OWNING WARD — the patient is admitted to the caller's department. The
      General Medicine team must see that their own patient is going to theatre;
      losing that would be a clinical-safety regression, not a security win.
      Derived with ``permissions.scope_patient_ids_sql`` — the ONE derivation,
      not a copy of it.

  (D) THE NAMED CASE TEAM — the caller is ``surgeries.surgeon_id`` for the case,
      or has a ``surgery_team_assignments`` row on it. A cardiologist pulled
      onto a case must be able to open the case they were alerted to, and
      ``POST /{id}/team-response`` requires exactly that. Derived from server-
      side tables, never from the request.

NOT IN THE RULE, deliberately: anything the CALLER can name. There is no
`?department=`, no `?all=true`, no header. Same law as
permissions.department_scope: a scope the caller can name is not a scope.

WHERE THIS BELONGS LONG-TERM. Clause (B) is really saying "this account has a
perioperative scope", which wants to be a ROLE (`surgeon`, `anaesthetist`) or a
`users.scope` column — i.e. a change to permissions.py / ROLE_PERMISSIONS. That
file is under a pending founder ruling and is not edited here, so the rule lives
in the module it governs, expressed against the one column that already carries
the fact (``users.department``). When the role table is reopened, move clause
(B) there and delete ``_PERIOPERATIVE_TOKENS``; the union above is the spec.

FAILURE MODE OF CLAUSE (B), written down rather than left to be found: it is a
SUBSTRING match on a free-text department name. A site whose theatre department
is called something unmatched (e.g. "OT Complex") would ward-scope its own
surgeons to an empty list; a site with a post-op ward called "Surgical Ward"
would grant that ward the whole theatre list. Both are visible in the audit
trail (`surgical_scope`), and both are fixable without a deploy by setting
``ORB_PERIOPERATIVE_DEPARTMENTS`` to an exact, comma-separated allowlist, which
REPLACES the token match entirely.
"""

from __future__ import annotations

import os

from database import db_session
from fastapi import HTTPException, Request
from permissions import department_scope, facility_scope, log_phi_read, scope_patient_ids_sql

from services.facility_filter import column_clause

# ── FACILITY SCOPE (multisite-8, W12-A, 20 October 2026) ──
# With more than one facility on record every clause above is also kept to the
# caller's facilities (permissions.facility_scope): a case is read by the
# facility it is at (surgeries.facility_id, migration 74, the document's own
# facility under ruling MS-R5 (a)), so (A) and (B) become "the whole theatre
# list of my facility", (C) also needs the patient's latest encounter at one
# of those facilities, and (D) a named case team member at another facility
# does not see the case. With one facility facility_scope answers None and
# every fragment below is exactly what it was.

# ── Scope verdicts. These strings land in the audit trail; do not rename. ──
SCOPE_HOSPITAL_WIDE = "hospital_wide"  # (A) no department on the user row
SCOPE_PERIOPERATIVE = "perioperative"  # (B) theatre / anaesthesia department
SCOPE_DEPARTMENT = "department"  # (C)+(D) ward + named case team

# Lower-cased substrings that mark a department as perioperative. "surg" catches
# General Surgery, Neurosurgery, Cardiothoracic Surgery and Surgical Oncology.
# Both the American and British spellings of anaesthesia are listed because the
# seed data uses one (users "Anesthesiology") and the WHO checklist copy uses the
# other — and each is cut at "…esth", not "…esthes", so "Anaesthetics" and
# "Anesthetics" match too. That is not hypothetical tidiness: the first version
# of this tuple said "anaesthes" and silently ward-scoped a department called
# "Anaesthetics" to a ward that admits nobody.
_PERIOPERATIVE_TOKENS = (
    "surg",
    "anesth",
    "anaesth",
    "theatre",
    "theater",
    "operating room",
)

_PERIOP_ENV = "ORB_PERIOPERATIVE_DEPARTMENTS"


def is_perioperative_department(dept: str | None) -> bool:
    """Does this department name mean "works in theatre, for every ward"?

    Read from the environment at CALL time, not import time, so an operator can
    correct a mis-detected department without a redeploy and so tests can pin it.
    When ``ORB_PERIOPERATIVE_DEPARTMENTS`` is set it is an EXACT allowlist
    (case-insensitive) and the token match is not consulted at all — a site that
    needs to be precise must be able to be precise.
    """
    if not dept:
        return False
    name = str(dept).strip().lower()
    if not name:
        return False
    override = os.environ.get(_PERIOP_ENV)
    if override is not None and override.strip():
        return name in {p.strip().lower() for p in override.split(",") if p.strip()}
    return any(token in name for token in _PERIOPERATIVE_TOKENS)


def surgical_scope(user: dict) -> tuple[str, str | None]:
    """(verdict, department) for this caller. THE single derivation for surgery.

    Derived from the AUTHENTICATED user row only. Returns one of the SCOPE_*
    constants plus the department it was derived from (None for the unscoped
    governance account).
    """
    dept = department_scope(user)
    if dept is None:
        return SCOPE_HOSPITAL_WIDE, None
    if is_perioperative_department(dept):
        return SCOPE_PERIOPERATIVE, dept
    return SCOPE_DEPARTMENT, dept


def reads_whole_theatre(user: dict) -> bool:
    """True when clauses (A)/(B) apply and no facility limit does: no row-level
    filter is needed. With more than one facility on record a (A)/(B) reader
    still gets the facility filter (surgical_list_filter), so this is False."""
    if surgical_scope(user)[0] not in (SCOPE_HOSPITAL_WIDE, SCOPE_PERIOPERATIVE):
        return False
    return facility_scope(user) is None


def surgical_list_filter(user: dict, alias: str = "s") -> tuple[str, tuple]:
    """SQL to AND into a `surgeries` query so it returns only readable cases.

    Returns ``("", ())`` for a whole-theatre reader, so the hot path for surgical
    staff is byte-identical to the query that shipped before scoping existed.

    Otherwise returns a fragment implementing clauses (C) and (D). ``alias`` is
    the `surgeries` alias in the caller's query.

    The patient-id subquery comes from permissions.scope_patient_ids_sql — the
    ONE derivation. It re-uses the alias ``p``; when the outer query also aliases
    patients as ``p`` the inner FROM shadows it, and because the subquery is
    self-contained (not correlated) that is correct, not a coincidence.

    With more than one facility on record (multisite-8) the fragment also keeps
    the case's own facility (``<alias>.facility_id``) to the caller's
    facilities, for a whole-theatre reader too, and the patient-id subquery
    carries the facilities; the empty set reads no case.
    """
    scope, dept = surgical_scope(user)
    facilities = facility_scope(user)
    facility_sql, facility_params = column_clause(f"{alias}.facility_id", facilities)
    if scope in (SCOPE_HOSPITAL_WIDE, SCOPE_PERIOPERATIVE):
        return facility_sql, facility_params
    ids_sql, ids_params = scope_patient_ids_sql(dept, facilities=facilities)
    uid = (user or {}).get("id") or ""
    frag = (
        f" AND ({alias}.patient_id IN ({ids_sql})"
        f" OR {alias}.surgeon_id = ?"
        f" OR EXISTS (SELECT 1 FROM surgery_team_assignments sta"
        f"            WHERE sta.surgery_id = {alias}.id AND sta.user_id = ?))"
    )
    return frag + facility_sql, tuple(ids_params) + (uid, uid) + facility_params


def surgery_in_scope(db, user: dict, surgery_id: str | None) -> bool:
    """May this caller read THIS case? Takes an open connection."""
    if reads_whole_theatre(user):
        return True
    if not surgery_id:
        return False
    frag, params = surgical_list_filter(user)
    return (
        db.execute(
            f"SELECT 1 FROM surgeries s WHERE s.id = ?{frag}",
            (surgery_id,) + params,
        ).fetchone()
        is not None
    )


def surgery_in_scope_blocking(user: dict, surgery_id: str | None) -> bool:
    """surgery_in_scope on its own connection — for run_db() from an async route."""
    if reads_whole_theatre(user):
        return True
    with db_session() as db:
        return surgery_in_scope(db, user, surgery_id)


def enforce_surgery_scope(user: dict, surgery_id: str | None) -> None:
    """404 unless the caller may read this case. BLOCKING — never on the loop.

    404, never 403, and with the SAME detail string the routes already use for a
    genuinely unknown id ("Surgery not found"): a 403, or a different message,
    would confirm that the case — and therefore the patient — exists. Same rule
    as permissions.enforce_patient_scope.

    Call from a plain `def` route (FastAPI threadpools those). From an
    `async def`, go through ``run_db(surgery_in_scope_blocking, …)`` instead.
    """
    if reads_whole_theatre(user):
        return
    with db_session() as db:
        if not surgery_in_scope(db, user, surgery_id):
            raise HTTPException(status_code=404, detail="Surgery not found")


def surgery_patient_id(db, surgery_id: str) -> str | None:
    """The case's patient, for the audit row. Takes an open connection."""
    row = db.execute("SELECT patient_id FROM surgeries WHERE id = ?", (surgery_id,)).fetchone()
    return row["patient_id"] if row else None


def log_surgical_phi_read(
    *,
    request: Request,
    user: dict,
    action_type: str,
    summary_message: str,
    patient_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Fail-closed ``phi_access`` row for a surgical read. BLOCKING.

    Thin wrapper over permissions.log_phi_read that stamps WHICH CLAUSE of the
    rule above let the read through. Without it the trail could say a read
    happened but not why it was allowed, and clause (B) — the hospital-wide one —
    would be exactly as invisible as the leak it replaces.

    `phi_access` is fail-closed (services/audit_service._FAIL_CLOSED_CATEGORIES):
    if the row cannot be persisted this raises and no PHI is returned.
    """
    scope, dept = surgical_scope(user)
    meta = {
        "surgical_scope": scope,
        "surgical_rule": "surgery_scope.surgical_scope",
        "hospital_wide": scope in (SCOPE_HOSPITAL_WIDE, SCOPE_PERIOPERATIVE),
    }
    if metadata:
        meta.update(metadata)
    log_phi_read(
        request=request,
        user=user,
        action_type=action_type,
        summary_message=summary_message,
        patient_id=patient_id,
        target_type=target_type,
        target_id=target_id,
        metadata=meta,
    )
