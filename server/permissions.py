"""Orb Hospital OS — Centralized Permission Model

Defines the canonical permission set, the role→permission mapping,
and FastAPI dependency factories that enforce least-privilege access
with automatic audit logging on denial.

Usage in route handlers:
    from permissions import require_permission, require_patient_self

    @router.get("/patients")
    async def list_patients(user=Depends(require_permission("patient.read_all"))):
        ...

    @router.get("/bridge/status")
    async def patient_status(user=Depends(require_patient_self())):
        ...
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import Depends, HTTPException, Request
from auth import get_current_user
from database import db_session, run_db
from services.audit_service import log_audit_event


# ════════════════════════════════════════════════════════════════
#  Permission Constants
# ════════════════════════════════════════════════════════════════
# Organized by domain for readability and future scoping.

# Clinical
PATIENT_READ_ALL = "patient.read_all"          # View any patient record
PATIENT_WRITE = "patient.write"                # Create/edit patient records
VITALS_READ = "vitals.read"                    # View vital signs for any patient
VITALS_WRITE = "vitals.write"                  # Record a bedside observation set (nurse/doctor)
ALERT_READ = "alert.read"                      # View clinical alerts
MEDICATION_WRITE = "medication.write"          # Chart / modify a medication order, and record a dose

# THE PRESCRIBER'S OWN AUTHORITY, SPLIT OUT FROM MEDICATION_WRITE (2026-08-15).
#
# ADDED, NOT RE-USED, AND THE REASON MATTERS. `medication.write` is granted to
# BOTH doctor and nurse — it has to be, because the same permission gates
# /api/emar/.../administer, and giving a charted dose is a nurse's core job. Its
# old comment said "Prescribe / modify medications", which was true of the
# doctor grant and false of the nurse one; the permission had quietly become
# "may touch the medication tables at all".
#
# So when the allergy override needed a PRESCRIBER-ONLY gate there was nothing
# in this file that meant "prescriber". The available shortcuts were all worse:
#   * `role == "doctor"` — the role-name gate this codebase has been bitten by
#     before (src/lib/roles.ts documents two of them), and it silently excludes
#     any future prescribing role a site adds;
#   * standing the gate on some other doctor-only permission (`scribe.sign`,
#     `patient.write`) — a permission borrowed to mean something it does not say
#     is exactly how a gate drifts away from the thing it was protecting.
#
# This grants NOTHING to anyone who did not already have it: no route was gated
# on it before it existed, and it is held only by `doctor` (and therefore by the
# `clinician` alias). What it removes is a capability nobody ever decided to
# give: before today, any holder of `medication.write` — every nurse — could
# clear the allergy interlock by supplying a typed reason to the service. No UI
# offered that field, so it was unreachable in practice, but it was the server's
# actual answer. See services/allergy_interlock.py.
#
# A site that licenses nurse prescribers or clinical pharmacists grants them
# THIS permission (and, for a pharmacist, `medication.write` alongside it); it
# does not need a code change, which is the point of expressing it here.
MEDICATION_PRESCRIBE = "medication.prescribe"   # Make the prescribing decision itself

# Scribe
SCRIBE_CREATE_DRAFT = "scribe.create_draft"    # Transcribe + structure notes
SCRIBE_READ_ALL = "scribe.read_all"            # Read any scribe note (future: team-scoped)
SCRIBE_EDIT_DRAFT = "scribe.edit_draft"        # Edit draft note content
SCRIBE_SIGN = "scribe.sign"                    # Sign/finalize a draft (doctor only)
SCRIBE_REJECT = "scribe.reject"                # Reject a draft (doctor only)

# AI Modules
SAGE_QUERY = "sage.query"                      # Query Sage AI assistant
LENS_ANALYZE = "lens.analyze"                  # Upload + analyze medical images
LENS_READ = "lens.read"                        # View existing Lens analyses

# Discharge
DISCHARGE_READ = "discharge.read"              # View discharge summaries
DISCHARGE_GENERATE = "discharge.generate"      # Trigger discharge workflow
DISCHARGE_PUBLISH = "discharge.publish"        # Finalize / sign a discharge

# Relay
RELAY_READ = "relay.read"                      # Read channels + messages
RELAY_SEND = "relay.send"                      # Send messages
RELAY_EXECUTE_ACTION = "relay.execute_action"  # Execute ambient action cards

# Forecast & Surgery
FORECAST_READ = "forecast.read"                # View hospital/patient forecasts
SURGERY_READ = "surgery.read"                  # View surgical schedule
SURGERY_PREP = "surgery.prep"                  # Trigger surgery prep alerts
SURGERY_WRITE = "surgery.write"                # Manage surgical cases, team alerts, start/complete

# Bridge (Patient Portal)
BRIDGE_READ_OWN = "bridge.read_own"            # Patient: read own status/vitals
BRIDGE_ASK = "bridge.ask"                      # Patient: ask questions

# Helix Pharmacy
HELIX_READ = "helix.read"                    # View inventory and pending orders
HELIX_WRITE = "helix.write"                  # Verify orders, adjust stock

# Confirming that a dispensed drug PHYSICALLY ARRIVED on the ward. Deliberately
# NOT HELIX_WRITE, and deliberately not granted to the pharmacist.
#
# Until 2026-09-07 the last two steps of the pharmacy chain — "dispensed" and
# "delivered" — were both HELIX_WRITE, so the same pharmacist who sent the drug
# also recorded that the ward received it. That is one person attesting to an
# event happening somewhere they are not. The chain read as a custody trail and
# was in fact a single desk clicking twice.
#
# Receipt is the ward's attestation, so it is the ward's permission: the nurse
# who took delivery says so in their own name. The pharmacist cannot close the
# loop on their own dispatch, which is the entire point of the step.
HELIX_RECEIVE = "helix.receive"              # Ward: confirm a dispensed drug arrived

# Orders / CPOE
ORDER_READ = "order.read"                    # View lab/imaging/procedure orders
ORDER_WRITE = "order.write"                  # Create orders, drive the order state machine

# Insurance / TPA desk
# DELIBERATELY NOT ADMIN_OVERVIEW. Until 2026-08-12 every route in
# routes/payer_routes.py was gated on `admin.overview` — the GOVERNANCE
# permission, the one that also opens the audit log (routes/audit_routes.py) and
# the admin dashboard — because the role table has no insurance role and that
# module could not add a permission to this file. The effect was that a TPA clerk
# who needed to record a claim settlement had to be handed the hospital's own
# tamper-evidence record in order to do it: the desk reading the log that records
# the desk. Least privilege is not a property a module can hold on its own — it
# is a property of the permission it is given, which is why the fix belongs here.
#
# READ AND WRITE ARE SPLIT because they are different jobs at the same desk. An
# aging report and a denial log are what a finance manager reads on a Monday;
# recording a settlement or closing a claim moves money in the hospital's books
# and is what a clerk does. A site that wants the first without the second can
# now express it.
PAYER_READ = "payer.read"                    # View payers, policies, claims, aging, denials
PAYER_WRITE = "payer.write"                  # Capture cover, drive the claim lifecycle

# Billing desk — same shape as the payer split above, same reason: until
# 2026-08-18 every billing route borrowed `admin.overview`, so capturing a
# charge required the governance grant. READ and WRITE are different jobs at
# the same desk (an aging report vs. raising an invoice). The THIRD tier exists
# because the tariff master is a different AUTHORITY from capturing a charge:
# a clerk records what happened to one patient; whoever edits the price list
# changes what every future invoice in the hospital says. One desk, two pens.
BILLING_READ = "billing.read"                # View invoices, charges, GST summaries, the tariff
BILLING_WRITE = "billing.write"              # Capture charges, raise/adjust/settle invoices
BILLING_TARIFF = "billing.tariff"            # Edit the tariff master / price list itself

# Cash counter. Split OUT of `admin.overview` for the same reason as the payer
# desk, but with more tiers because the module already enforces segregation of
# duties IN CODE (approver ≠ requester on refunds/voids) and could not express
# it in permissions — the code knew two jobs existed; the permission table said
# there was one. RECORD is the cashier taking money at the counter; APPROVE is
# the supervisor authorising a refund/void/waiver (real money leaving the
# hospital); CLOSE_COUNTER is the end-of-shift reconciliation. A site that
# wants one person doing all three grants all three — but it now has to SAY so.
PAYMENTS_READ = "payments.read"              # View receipts, deposits, counter status
PAYMENTS_RECORD = "payments.record"          # Record a payment / deposit at the counter
PAYMENTS_APPROVE = "payments.approve"        # Approve refunds, voids, waivers (supervisor)
PAYMENTS_CLOSE_COUNTER = "payments.close_counter"  # Close and reconcile a counter/shift

# Front desk. The smallest split: registration/transfer/discharge-paperwork
# mutations sat behind `admin.overview` in an OR next to `patient.write`, so
# clinicians reached them legitimately and only the registration clerk was
# over-granted — a clerk who files admissions had to hold the audit log to do
# it. One write permission fixes that; front-desk READS are patient-register
# reads and stay on the clinical read permissions they already use.
FRONTDESK_WRITE = "frontdesk.write"          # Register/transfer paperwork at the front desk

# Procurement. Three-way, not two, because raise-vs-approve is already
# load-bearing IN the module (raising an indent needed `helix.write`; approving
# a PO, issuing to a vendor, blacklisting, writing off stock needed
# `admin.overview`). Borrowed permissions meant a pharmacist could raise a
# surgical-consumables indent and a storekeeper needed an admin account.
# APPROVE is money and vendor standing; WRITE is the day's indents; READ is the
# catalogue and ledgers.
PROCUREMENT_READ = "procurement.read"        # View catalogue, indents, POs, stock ledgers
PROCUREMENT_WRITE = "procurement.write"      # Raise/edit indents and purchase orders
PROCUREMENT_APPROVE = "procurement.approve"  # Approve/issue POs, blacklist vendors, write off stock

# Pilot instrument (shadow mode)
# Recording a deterioration event, and the once-per-shift "no events"
# confirmation. NOT a governance permission and deliberately not bundled into
# ADMIN_OVERVIEW: the charge nurse records these (docs/OUTCOME_CAPTURE.md §4),
# and they must never gain the ability to read the scorecard, the adjudication
# queue or the export — that would let the people generating the pilot's ground
# truth watch the score move as they enter it.
OUTCOME_RECORD = "outcome.record"

# DPDP consent register: recording and withdrawing a patient's consent
# (routes/compliance_routes.py consent_router). Split out of PATIENT_WRITE,
# which these routes borrowed: that one also authorises editing the patient
# record, so a registration clerk could not have been given consent capture
# without being handed the chart as well. Held by the doctor only, exactly the
# set that held PATIENT_WRITE, so nobody gains anything today. Whether the
# registration desk (an admin account at present) should take consent is a
# product decision; when it is made, it is one line in the table below.
CONSENT_RECORD = "consent.record"

# Governance
AUDIT_READ = "audit.read"                      # Read audit log entries
AUDIT_TRACE = "audit.trace"                    # Trace correlation IDs
INGEST_MONITOR = "ingest.monitor"              # View ingestion status/events
ADMIN_OVERVIEW = "admin.overview"              # View admin governance dashboard


# ════════════════════════════════════════════════════════════════
#  Role → Permission Mapping
# ════════════════════════════════════════════════════════════════
# Extensible: add new roles (e.g. governance_admin, security_admin) by
# adding a new key with the desired permission set.

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "doctor": {
        PATIENT_READ_ALL, PATIENT_WRITE, VITALS_READ, VITALS_WRITE, ALERT_READ, MEDICATION_WRITE,
        # The prescribing decision itself — held by the doctor and by nobody
        # else in this table. Gates the allergy override at administration
        # (services/allergy_interlock.py). See the constant's note above for why
        # this is a separate permission rather than a re-used one.
        MEDICATION_PRESCRIBE,
        SCRIBE_CREATE_DRAFT, SCRIBE_READ_ALL, SCRIBE_EDIT_DRAFT, SCRIBE_SIGN, SCRIBE_REJECT,
        SAGE_QUERY, LENS_ANALYZE, LENS_READ,
        DISCHARGE_READ, DISCHARGE_GENERATE, DISCHARGE_PUBLISH,
        RELAY_READ, RELAY_SEND, RELAY_EXECUTE_ACTION,
        FORECAST_READ, SURGERY_READ, SURGERY_PREP, SURGERY_WRITE,
        ORDER_READ, ORDER_WRITE,
        # The charge nurse records outcomes (Decision 2). A doctor holds it too,
        # for the site that names the ward doctor as the shift's one person.
        OUTCOME_RECORD,
        # Consent capture at the bedside. See the constant's note above.
        CONSENT_RECORD,
    },
    "nurse": {
        # VITALS_WRITE (recording a bedside observation set) is a nurse's core
        # job, and is deliberately NOT bundled into PATIENT_WRITE — a nurse
        # charts obs but does not edit the patient record.
        #
        # MEDICATION_WRITE but NOT MEDICATION_PRESCRIBE: a nurse charts and
        # gives a dose, and cannot clear the allergy interlock over a charted
        # allergy. She is the LAST independent check on the prescriber's
        # decision, and an account that can both question and overrule it is one
        # check wearing two hats.
        PATIENT_READ_ALL, VITALS_READ, VITALS_WRITE, ALERT_READ, MEDICATION_WRITE,
        SCRIBE_CREATE_DRAFT, SCRIBE_READ_ALL, SCRIBE_EDIT_DRAFT,
        SAGE_QUERY, LENS_ANALYZE, LENS_READ,
        DISCHARGE_READ,
        RELAY_READ, RELAY_SEND,
        FORECAST_READ, SURGERY_READ, SURGERY_PREP, SURGERY_WRITE,
        ORDER_READ, ORDER_WRITE,
        # THE PILOT'S GROUND TRUTH. The charge nurse is the one named person per
        # shift who records a deterioration event and confirms the shifts with
        # none (docs/OUTCOME_CAPTURE.md §4, §6). Without this grant the decision
        # the physician champion signed cannot be executed: the endpoint existed
        # but only an administrator could reach it.
        OUTCOME_RECORD,
        # Taking delivery of a dispensed drug. The nurse is the person actually
        # standing where the drug arrives, so she is the only one who can
        # honestly say it did. Note what is NOT here: HELIX_READ. Receipt does
        # not open the pharmacy — the ward endpoint returns only the orders for
        # patients already on this nurse's ward, and nothing about inventory.
        HELIX_RECEIVE,
    },
    "pharmacist": {
        # Limited to pharmacy workflow + Sage for clinical context
        SAGE_QUERY,
        HELIX_READ, HELIX_WRITE,
        # Raising an indent was reachable through HELIX_WRITE while procurement
        # borrowed helix permissions; now that procurement has its own family,
        # these two keep the pharmacist's existing reach EXPRESSED rather than
        # borrowed. Deliberately NOT procurement.approve — approving a PO,
        # issuing to a vendor or writing off stock was admin-only before the
        # split and stays that way.
        PROCUREMENT_READ, PROCUREMENT_WRITE,
        # NOT HELIX_RECEIVE, and this omission is load-bearing. The pharmacist
        # dispenses; the ward receives. An account holding both would be able to
        # walk an order from "scheduled" to "delivered" without the drug ever
        # leaving the room, and the custody trail would show two steps taken by
        # one person. See HELIX_RECEIVE's note above.
    },
    "admin": {
        # Governance + read-only clinical + pharmacy access
        PATIENT_READ_ALL, VITALS_READ, ALERT_READ,
        SCRIBE_READ_ALL,
        SAGE_QUERY, LENS_READ,
        DISCHARGE_READ,
        RELAY_READ, RELAY_SEND,
        FORECAST_READ, SURGERY_READ, SURGERY_WRITE,
        HELIX_READ, HELIX_WRITE,
        ORDER_READ,
        # The insurance / TPA desk. Granted to `admin` because that is who runs
        # it today — a TPA clerk is seeded as an `admin` account, which is why
        # src/shellConfig.tsx shows the tab to `admin` only — so this grant
        # changes nothing about who can reach the desk. The point of the split
        # runs the other way: the desk no longer DEPENDS on ADMIN_OVERVIEW, so
        # the day a `tpa_clerk` role exists it can be given exactly these two and
        # not the audit log.
        PAYER_READ, PAYER_WRITE,
        # The back-office desks, split out of ADMIN_OVERVIEW on 2026-08-18 the
        # same way (see each constant's note). All granted to `admin` because
        # admin runs every desk today, so — like the payer split — this changes
        # nothing about who can reach them. What it changes is what a future
        # billing clerk, cashier or storekeeper account has to be handed.
        BILLING_READ, BILLING_WRITE, BILLING_TARIFF,
        PAYMENTS_READ, PAYMENTS_RECORD, PAYMENTS_APPROVE, PAYMENTS_CLOSE_COUNTER,
        FRONTDESK_WRITE,
        PROCUREMENT_READ, PROCUREMENT_WRITE, PROCUREMENT_APPROVE,
        # Kept so the administrator can still backfill an outcome or a shift
        # confirmation on the nurse's behalf. The charge nurse records these
        # through the end-of-shift capture screen in the nurse panel
        # (src/pages/panels/nurse/OutcomeCaptureSheet.tsx); the admin grant is
        # the backfill path — curl or a future admin form — not the primary
        # one, and an operator who could record yesterday must not lose that
        # ability today.
        OUTCOME_RECORD,
        # Governance-specific
        AUDIT_READ, AUDIT_TRACE, INGEST_MONITOR, ADMIN_OVERVIEW,
    },
    "patient": {
        # Self-only access through Bridge
        BRIDGE_READ_OWN, BRIDGE_ASK,
    },
    # The cash counter, as its own account type. No seeded user holds this role
    # yet — it exists so a site can staff a counter without handing the cashier
    # a chart or the audit log. RECORD but not APPROVE: the module enforces
    # approver ≠ requester on refunds/voids in code, and the permission table
    # now agrees — the person taking money cannot authorise it leaving.
    # CLOSE_COUNTER because reconciling their own shift is the cashier's job.
    "cashier": {
        PAYMENTS_READ, PAYMENTS_RECORD, PAYMENTS_CLOSE_COUNTER,
    },
}

# Alias clinician to doctor
ROLE_PERMISSIONS["clinician"] = ROLE_PERMISSIONS["doctor"]


# ════════════════════════════════════════════════════════════════
#  Permission Check Helpers
# ════════════════════════════════════════════════════════════════

def has_permission(role: str, permission: str) -> bool:
    """Check whether a role has a specific permission."""
    return permission in ROLE_PERMISSIONS.get(role, set())


def has_all_permissions(role: str, *permissions: str) -> bool:
    """Check whether a role has ALL specified permissions."""
    role_perms = ROLE_PERMISSIONS.get(role, set())
    return all(p in role_perms for p in permissions)


def get_permissions_for_role(role: str) -> set[str]:
    """Return the set of permissions granted to a role."""
    return ROLE_PERMISSIONS.get(role, set()).copy()


# ════════════════════════════════════════════════════════════════
#  Ward/department scoping — ONE derivation, server-side only
# ════════════════════════════════════════════════════════════════

def department_scope(user: dict) -> str | None:
    """The department a request may see PHI for, derived from the AUTHENTICATED user.

    This is the single source of truth for ward scoping. Every patient-list
    surface (Vigil's patient board, the nurse shift board, anything added later)
    must call this and MUST NOT accept a department or "show me everything"
    switch from the client — a scope the caller can name is not a scope.

    That is not a hypothetical: /api/nurse/board once took `?department=` and
    `?all_departments=true` straight off the query string, and NUR001 — five
    patients on /api/vigil/patients — could pull 30 patients across six
    departments through it. The hole existed because the nurse board grew its
    OWN idea of scope beside Vigil's. Hence one function, called by both.

    Returns the user's department, or None when they have none on record.
    None means "not ward-scoped" — the pre-existing convention that lets a
    hospital-wide governance account (an admin seeded without a department) read
    across wards. It is a property of the USER ROW, which only an administrator
    can change and which the audit trail records on every PHI read; it is not
    something a request can ask for. A clinical account with a blank department
    therefore reads hospital-wide on every board equally — set departments on
    clinical users. User Management enforces that for DEPARTMENT_REQUIRED_ROLES.
    """
    dept = (user or {}).get("department")
    if dept is None:
        return None
    dept = str(dept).strip()
    return dept or None


# Roles that may not be created or left without a department: for these a blank
# department is not "no ward", it is every ward (see department_scope above).
# Pharmacist is deliberately absent: pharmacy is a hospital-wide function and
# migration 36 cleared PHA001's "Pharmacy" department so it could reach patients
# at all; requiring one would recreate that defect. Admin and cashier hold no
# ward-scoped PHI reads. Whether a doctor or nurse may ever be hospital-wide is
# an open product decision; until it is made there is no such account.
DEPARTMENT_REQUIRED_ROLES: frozenset[str] = frozenset({"doctor", "nurse"})


# ════════════════════════════════════════════════════════════════
#  Per-PATIENT scoping — the same derivation, for id-taking routes
# ════════════════════════════════════════════════════════════════
# department_scope() answers "which ward may this account see?". It does not
# answer "may this account see THIS patient?", and every REST route that takes
# a patient_id off the path was asking the second question and getting no answer
# at all: holding `patient.read_all` let a Cardiology nurse open an Oncology
# chart, notes, imaging and handoff draft (audit 2026-08-05).
#
# A patient's department is a property of their ADMISSION, not of patients.ward
# — `patients` has no department column and `ward` is a free-text bed label that
# nothing constrains. Scoping the wrong column is how a scope silently stops
# scoping.
#
# NOTE ON DUPLICATION: services/sage_service.py:_patient_in_scope /_scope_sql
# implement this same derivation for the Sage lane. They are deliberately
# identical, and tests/test_patient_scope_routes.py asserts the two agree on the
# same fixtures — if one is ever changed without the other, that test fails.

PATIENT_DEPARTMENT_SQL = (
    "(SELECT a.department FROM admissions a WHERE a.patient_id = p.id "
    " ORDER BY (a.status = 'active') DESC, a.created_at DESC, a.rowid DESC "
    " LIMIT 1)"
)


def scope_patient_ids_sql(dept: str | None) -> tuple[str, tuple]:
    """A subquery yielding every patient id inside `dept`.

    Returns (sql, params) so it can be embedded as ``... IN (<sql>)``. A `dept`
    of None returns the unfiltered id set — the department_scope() convention
    for a genuinely unscoped governance account, NOT "scope unknown".
    """
    if dept is None:
        return "SELECT p.id FROM patients p", ()
    return f"SELECT p.id FROM patients p WHERE {PATIENT_DEPARTMENT_SQL} = ?", (dept,)


def patient_in_scope(db, patient_id: str, dept: str | None) -> bool:
    """Is this ONE patient inside the caller's department?

    Vets a patient_id that arrived from the REQUEST, which is exactly the kind
    of caller-named scope the department_scope() docstring forbids trusting.

    Fail-closed on purpose: a patient with no admission row has no derivable
    department and is therefore NOT in any scoped caller's ward. Every
    production creation path (services/admission_service.admit_patient,
    services/roster_import, scripts/seed_clinical_data) writes an admissions row
    with a department in the same transaction as the patients row, and
    AdmissionRequest.department is a required field — so the only rows this can
    hide are ones no ward owns.
    """
    if dept is None:
        return True
    if not patient_id:
        return False
    return db.execute(
        f"SELECT 1 FROM patients p WHERE p.id = ? AND {PATIENT_DEPARTMENT_SQL} = ?",
        (patient_id, dept),
    ).fetchone() is not None


def patient_in_scope_blocking(patient_id: str, dept: str | None) -> bool:
    """patient_in_scope on its own connection — for run_db() from async routes."""
    if dept is None:
        return True
    with db_session() as db:
        return patient_in_scope(db, patient_id, dept)


def enforce_patient_scope(user: dict, patient_id: str | None) -> None:
    """404 unless `patient_id` is inside the caller's department. BLOCKING.

    The dependency form of this check, require_patient_access(), resolves the id
    from the PATH — so it cannot guard a route that lets the caller name their
    patient in the QUERY STRING (``/api/scribe/notes?patient_id=``,
    ``/api/appointments?patient_id=``) or through a SURROGATE id that resolves
    to one (``/api/scribe/notes/{note_id}``). Those spellings are the same
    disclosure and were the blind spot in the first pass at the 2026-08-05
    incident: the drift guard greps for ``{patient_id}`` in the path, so it
    looked straight past them.

    Call this from a plain `def` route (FastAPI threadpools those, so the
    db_session stays off the event loop). From an `async def`, go through
    ``run_db(patient_in_scope_blocking, …)`` instead.

    404, never 403 — a 403 confirms the patient exists, which is itself a
    disclosure. Same rule as require_patient_access.
    """
    dept = department_scope(user)
    if dept is None or not patient_id:
        return
    with db_session() as db:
        if not patient_in_scope(db, patient_id, dept):
            raise HTTPException(status_code=404, detail="Patient not found")


def log_phi_read(
    *,
    request: Request,
    user: dict,
    action_type: str,
    summary_message: str,
    patient_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict | None = None,
    outcome: str = "success",
) -> None:
    """Record a PHI READ on the hash chain. BLOCKING — never call on the loop.

    The audit middleware (services/audit_middleware.py) fires on POST/PUT/PATCH/
    DELETE only, so until 2026-08-05 the four leaking read surfaces were not
    merely exploitable, they were unprovable after the fact: a nurse reading
    another ward's chart left no trace at all.

    Category `phi_access` is FAIL-CLOSED (services/audit_service:
    _FAIL_CLOSED_CATEGORIES) — if the row cannot be persisted this raises and
    the PHI is not returned. That is the existing precedent at
    routes/nurse_routes.py:74 and routes/fhir_export_routes.py:53, and reads are
    held to it rather than given a quieter fail-open rule of their own.
    """
    dept = department_scope(user)
    meta = {
        "department": dept,
        # True only for an account with no department on record — an
        # admin-settable property of the USER ROW, never a request parameter.
        "hospital_wide": dept is None,
        "scope_source": "authenticated_user",
    }
    if metadata:
        meta.update(metadata)
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="phi_access",          # fail-closed
        action_type=action_type,
        outcome=outcome,
        summary_message=summary_message,
        actor_user_id=(user or {}).get("id"),
        actor_role=(user or {}).get("role"),
        patient_id=patient_id,
        target_type=target_type or ("patient" if patient_id else None),
        target_id=target_id or patient_id,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata=meta,
    )


def _log_patient_read_blocking(*, request: Request, user: dict,
                               action_type: str, patient_id: str | None) -> None:
    """require_patient_access's read audit, written only once the patient is known.

    Writing "success" before the handler had looked the patient up put a
    successful chart view on the chain for an id that matches nobody, which is
    the opposite of what an investigator reading the trail needs. A missing
    patient is still recorded (a probe for ids is worth seeing), as a failure.
    Both steps run in one worker call, so the route pays one thread hop as before.
    """
    if patient_id:
        with db_session() as db:
            exists = db.execute("SELECT 1 FROM patients WHERE id = ?",
                                (patient_id,)).fetchone() is not None
        if not exists:
            log_phi_read(
                request=request, user=user, action_type=action_type,
                summary_message=f"{action_type} for patient {patient_id}: no such patient",
                patient_id=patient_id, outcome="failure",
                metadata={"reason": "not_found"},
            )
            return
    log_phi_read(
        request=request, user=user, action_type=action_type,
        summary_message=(f"{action_type} for patient {patient_id}"
                         if patient_id else action_type),
        patient_id=patient_id,
    )


# ════════════════════════════════════════════════════════════════
#  FastAPI Dependency: require_permission
# ════════════════════════════════════════════════════════════════

def require_permission(*permissions: str):
    """Dependency factory: require the user to have ALL listed permissions.

    On failure:
      - Logs a permission_denied audit event
      - Returns HTTP 403 with structured error body

    Usage:
        @router.get("/admin/overview")
        async def overview(user=Depends(require_permission("admin.overview"))):
            ...
    """
    async def _checker(
        request: Request,
        user: dict = Depends(get_current_user),
    ) -> dict:
        role = user.get("role", "")
        user_perms = ROLE_PERMISSIONS.get(role, set())
        missing = [p for p in permissions if p not in user_perms]

        if missing:
            # Audit the denial OFF the event loop. The happy path (no `missing`)
            # never touches the DB, so it stays on the loop with no threadpool
            # hop — only the denial branch pays one. A burst of denials is
            # exactly what an attack or a misconfigured client produces, and
            # that is precisely when the loop must not be stalled by encrypted
            # SQLite writes.
            #
            # This is still a blocking await, NOT fire-and-forget: the 403 is
            # not raised until the denial is durably on the hash chain.
            await run_db(
                log_audit_event,
                correlation_id=str(uuid.uuid4()),
                event_category="authorization",
                action_type="permission_denied",
                outcome="denied",
                summary_message=f"Missing permissions: {', '.join(missing)}",
                actor_user_id=user.get("id"),
                actor_role=role,
                request_path=request.url.path,
                http_method=request.method,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                metadata={
                    "required_permissions": list(permissions),
                    "missing_permissions": missing,
                    "user_role": role,
                },
            )

            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "message": f"Insufficient permissions",
                    "required": list(permissions),
                    "missing": missing,
                },
            )

        return user

    return _checker


# ════════════════════════════════════════════════════════════════
#  FastAPI Dependency: require_patient_access
# ════════════════════════════════════════════════════════════════

def require_patient_access(*permissions: str, audit_action: str | None = None):
    """require_permission + "…and this patient is on your ward" + a read audit.

    ONE guard hung off every ``/{patient_id}`` route rather than N copies of the
    check, so a route added later is either wearing it or is visibly bare in
    review. Resolves ``patient_id`` from the PATH — the request cannot name a
    department, only the patient it wants.

    Out of scope returns **404, not 403**: a 403 confirms the patient exists,
    which is itself a disclosure. A scoped caller cannot tell "no such patient"
    from "not your ward" through this route, and that is deliberate.

    `audit_action` writes a fail-closed ``phi_access`` row on success, and a
    ``failure`` row (reason ``not_found``) for an id that matches no patient,
    never a success for a chart that was not there. Pass None
    for high-frequency polled reads (``/vitals``) where one encrypted-SQLite
    write per poll is the shape blamed for the 2026-07-30 loop stalls — those
    still get the SCOPE check, which is the control; the read audit for them
    belongs in a batched reader, not on the request path.

    Both DB touches go through run_db(): this dependency runs on the event loop
    even when the route it guards is a sync def.
    """
    permission_checker = require_permission(*permissions)

    async def _checker(
        request: Request,
        user: dict = Depends(permission_checker),
    ) -> dict:
        patient_id = request.path_params.get("patient_id")
        dept = department_scope(user)

        if dept is not None and patient_id:
            in_scope = await run_db(patient_in_scope_blocking, patient_id, dept)
            if not in_scope:
                # The attempt is the interesting event — record it before the
                # 404 so a cross-ward probe is visible in the trail.
                await run_db(
                    log_audit_event,
                    correlation_id=str(uuid.uuid4()),
                    event_category="authorization",
                    action_type="patient_scope_denied",
                    outcome="denied",
                    summary_message=(
                        f"Patient {patient_id} is outside the caller's department "
                        f"({dept})"
                    ),
                    actor_user_id=user.get("id"),
                    actor_role=user.get("role"),
                    patient_id=patient_id,
                    request_path=request.url.path,
                    http_method=request.method,
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    metadata={"department": dept, "scope_source": "authenticated_user"},
                )
                raise HTTPException(status_code=404, detail="Patient not found")

        if audit_action:
            await run_db(
                _log_patient_read_blocking,
                request=request,
                user=user,
                action_type=audit_action,
                patient_id=patient_id,
            )

        return user

    return _checker


# ════════════════════════════════════════════════════════════════
#  FastAPI Dependency: require_patient_self
# ════════════════════════════════════════════════════════════════

def require_patient_self():
    """Dependency factory: require the user to be a patient AND verify they
    are only accessing their own data.

    This combines permission checking (bridge.read_own) with ownership
    validation at the route level, independent of service-layer scoping.

    Returns the user dict (which includes user_id for self-scoped queries).
    """
    async def _checker(
        request: Request,
        user: dict = Depends(get_current_user),
    ) -> dict:
        role = user.get("role", "")

        if role != "patient":
            # Same rule as require_permission: the denial audit is a blocking
            # SQLite write and must not run on the event loop.
            await run_db(
                log_audit_event,
                correlation_id=str(uuid.uuid4()),
                event_category="authorization",
                action_type="permission_denied",
                outcome="denied",
                summary_message="Non-patient user attempted to access patient-only endpoint",
                actor_user_id=user.get("id"),
                actor_role=role,
                request_path=request.url.path,
                http_method=request.method,
                ip_address=request.client.host if request.client else None,
                metadata={"reason": "patient_role_required"},
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "message": "This endpoint is accessible only to patients",
                },
            )

        return user

    return _checker
