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
from collections.abc import Mapping
from typing import Any, Never

import config
from auth import get_current_user
from database import db_session, run_db
from fastapi import Depends, HTTPException, Request
from services.audit_service import log_audit_event

from services import facility_service

# ════════════════════════════════════════════════════════════════
#  Permission Constants
# ════════════════════════════════════════════════════════════════
# Organized by domain for readability and future scoping.

# Clinical
PATIENT_READ_ALL = "patient.read_all"  # View any patient record
PATIENT_WRITE = "patient.write"  # Create/edit patient records
VITALS_READ = "vitals.read"  # View vital signs for any patient
VITALS_WRITE = "vitals.write"  # Record a bedside observation set (nurse/doctor)
# Copy an observation set from the paper sheet (the Orb Ward Record's ward
# clerk). Reaches the same observations route as VITALS_WRITE, but the set is
# stored with source 'clerk_transcribed' and its time is the time written on the
# sheet. It is NOT vitals.write: the clerk cannot mark a set entered in error,
# and holds no vitals.read, so no score preview and no chart.
VITALS_TRANSCRIBE = "vitals.transcribe"
ALERT_READ = "alert.read"  # View clinical alerts
MEDICATION_WRITE = "medication.write"  # Chart / modify a medication order, and record a dose

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
MEDICATION_PRESCRIBE = "medication.prescribe"  # Make the prescribing decision itself

# THE PHARMACIST'S CHECK OF A MEDICATION ORDER (medication-order store, W5-E).
# Verifying or rejecting an order in the store's own verification queue
# (/api/medorders, built in a later wave; every route there answers 404 while
# ORB_MED_ORDER_STORE is off). Ruling MO-R4 (a): the pharmacist only. The
# administrator does not hold it, although helix.write still lets an
# administrator verify on the Helix and Verify Queue screens as before; this
# grant changes nothing that exists today. A site that wants another role to
# verify grants it this permission.
MEDICATION_VERIFY = "medication.verify"  # Verify or reject a medication order (pharmacist)

# Scribe
SCRIBE_CREATE_DRAFT = "scribe.create_draft"  # Transcribe + structure notes
SCRIBE_READ_ALL = "scribe.read_all"  # Read any scribe note (future: team-scoped)
SCRIBE_EDIT_DRAFT = "scribe.edit_draft"  # Edit draft note content
SCRIBE_SIGN = "scribe.sign"  # Sign/finalize a draft (doctor only)
SCRIBE_REJECT = "scribe.reject"  # Reject a draft (doctor only)

# AI Modules
SAGE_QUERY = "sage.query"  # Query Sage AI assistant
LENS_ANALYZE = "lens.analyze"  # Upload + analyze medical images
LENS_READ = "lens.read"  # View existing Lens analyses

# Discharge
DISCHARGE_READ = "discharge.read"  # View discharge summaries
DISCHARGE_GENERATE = "discharge.generate"  # Trigger discharge workflow
DISCHARGE_PUBLISH = "discharge.publish"  # Finalize / sign a discharge

# Relay
RELAY_READ = "relay.read"  # Read channels + messages
RELAY_SEND = "relay.send"  # Send messages
RELAY_EXECUTE_ACTION = "relay.execute_action"  # Execute ambient action cards

# Forecast & Surgery
FORECAST_READ = "forecast.read"  # View hospital/patient forecasts
SURGERY_READ = "surgery.read"  # View surgical schedule
SURGERY_PREP = "surgery.prep"  # Trigger surgery prep alerts
SURGERY_WRITE = "surgery.write"  # Manage surgical cases, team alerts, start/complete

# Bridge (Patient Portal)
BRIDGE_READ_OWN = "bridge.read_own"  # Patient: read own status/vitals
BRIDGE_ASK = "bridge.ask"  # Patient: ask questions

# Helix Pharmacy
HELIX_READ = "helix.read"  # View inventory and pending orders
HELIX_WRITE = "helix.write"  # Verify orders, adjust stock

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
HELIX_RECEIVE = "helix.receive"  # Ward: confirm a dispensed drug arrived

# Orders / CPOE
ORDER_READ = "order.read"  # View lab/imaging/procedure orders
ORDER_WRITE = "order.write"  # Create orders, drive the order state machine

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
PAYER_READ = "payer.read"  # View payers, policies, claims, aging, denials
PAYER_WRITE = "payer.write"  # Capture cover, drive the claim lifecycle

# Billing desk — same shape as the payer split above, same reason: until
# 2026-08-18 every billing route borrowed `admin.overview`, so capturing a
# charge required the governance grant. READ and WRITE are different jobs at
# the same desk (an aging report vs. raising an invoice). The THIRD tier exists
# because the tariff master is a different AUTHORITY from capturing a charge:
# a clerk records what happened to one patient; whoever edits the price list
# changes what every future invoice in the hospital says. One desk, two pens.
BILLING_READ = "billing.read"  # View invoices, charges, GST summaries, the tariff
BILLING_WRITE = "billing.write"  # Capture charges, raise/adjust/settle invoices
BILLING_TARIFF = "billing.tariff"  # Edit the tariff master / price list itself

# Cash counter. Split OUT of `admin.overview` for the same reason as the payer
# desk, but with more tiers because the module already enforces segregation of
# duties IN CODE (approver ≠ requester on refunds/voids) and could not express
# it in permissions — the code knew two jobs existed; the permission table said
# there was one. RECORD is the cashier taking money at the counter; APPROVE is
# the supervisor authorising a refund/void/waiver (real money leaving the
# hospital); CLOSE_COUNTER is the end-of-shift reconciliation. A site that
# wants one person doing all three grants all three — but it now has to SAY so.
PAYMENTS_READ = "payments.read"  # View receipts, deposits, counter status
PAYMENTS_RECORD = "payments.record"  # Record a payment / deposit at the counter
PAYMENTS_APPROVE = "payments.approve"  # Approve refunds, voids, waivers (supervisor)
PAYMENTS_CLOSE_COUNTER = "payments.close_counter"  # Close and reconcile a counter/shift

# Front desk. The smallest split: registration/transfer/discharge-paperwork
# mutations sat behind `admin.overview` in an OR next to `patient.write`, so
# clinicians reached them legitimately and only the registration clerk was
# over-granted — a clerk who files admissions had to hold the audit log to do
# it. One write permission fixes that; front-desk READS are patient-register
# reads and stay on the clinical read permissions they already use.
FRONTDESK_WRITE = "frontdesk.write"  # Register/transfer paperwork at the front desk

# Procurement. Three-way, not two, because raise-vs-approve is already
# load-bearing IN the module (raising an indent needed `helix.write`; approving
# a PO, issuing to a vendor, blacklisting, writing off stock needed
# `admin.overview`). Borrowed permissions meant a pharmacist could raise a
# surgical-consumables indent and a storekeeper needed an admin account.
# APPROVE is money and vendor standing; WRITE is the day's indents; READ is the
# catalogue and ledgers.
PROCUREMENT_READ = "procurement.read"  # View catalogue, indents, POs, stock ledgers
PROCUREMENT_WRITE = "procurement.write"  # Raise/edit indents and purchase orders
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
#
# Ruling OPD-N3 (3 October 2026): the receptionist gets it ONCE COUNSEL AGREES,
# and not before. The registration desk's notice and consent step (opd-19,
# registration_service._consent_values) checks this permission, so today it is
# the doctor's walk-in that records consent at registration, and a receptionist
# who sends the step gets 403 consent_permission_required with nothing written.
# When counsel agrees, add CONSENT_RECORD to "receptionist" below and to the
# receptionist row in src/lib/roles.ts; tests/test_role_batch.py and
# tests/test_registration_consent.py pin the current answer and change with it.
CONSENT_RECORD = "consent.record"

# ── The whole-hospital-system build (3 October 2026) ─────────────────────────
# Registered all at once in wave 1 so every later increment finds its
# permission already here and src/lib/roles.ts can mirror one stable list.
# EVERY ONE OF THESE IS INERT until its switch is on: each route that checks
# them sits behind an opt-in module or feature flag (config.py) and answers 404
# while it is off. tests/test_role_batch.py pins each grant below, so a change
# to who holds one is a deliberate, reviewed edit.

# Registration and outpatients (OPD epic, ruling OPD-N3).
REGISTRATION_READ = "registration.read"  # Registration desk reads, minimal-field patient search
REGISTRATION_WRITE = "registration.write"  # Register a patient, correct demographics
OPD_CONSULT = "opd.consult"  # Run an outpatient consultation
SCHEDULE_MANAGE = "schedule.manage"  # Clinic sessions, display boards (configuration, no PHI)

# Beds and admissions (ADT epic). No ADT permission ends a stay: end of stay
# keeps discharge.generate plus scope. Doctors reach moves and presence through
# the patient.write arm of each route, not through these grants.
ADT_READ = "adt.read"  # Bed board, availability, census, movement history
ADT_BED = "adt.bed"  # Assign, change, swap or reserve a bed; never changes the treating department
ADT_TRANSFER = "adt.transfer"  # Change the treating department (a scope change)
ADT_PRESENCE = "adt.presence"  # Record leave, absconded and return
BED_MASTER = "bed.master"  # Bed setup, reconciliation link, register complete

# Pharmacy store (GRN epic, rulings GRN-R7 and GRN-R8). helix.receive keeps its
# meaning: ward receipt of a patient's dispensed drug, nothing else.
PHARMACY_STORE_READ = "pharmacy_store.read"  # Drug stock, lots, ledgers, documents
PHARMACY_STORE_WRITE = (
    "pharmacy_store.write"  # Raise purchase orders, receive goods, transfers, counts
)
PHARMACY_STORE_APPROVE = (
    "pharmacy_store.approve"  # Approve orders, adjustments, write-offs, downgrades
)
PHARMACY_STORE_NDPS = "pharmacy_store.ndps"  # The NDPS register and its witness steps
WARD_STOCK_READ = "ward_stock.read"  # A ward's own imprest stock (department-scoped)
WARD_STOCK_RECEIVE = "ward_stock.receive"  # Receive a pharmacy transfer on the ward
WARD_RETURN_RECORD = "ward_return.record"  # Record a medicine returned from the ward
WARD_INDENT_RAISE = "ward_indent.raise"  # Raise a ward indent to the pharmacy

# Laboratory (LIS epic, rulings LIS-R12 and LIS-R17).
LAB_READ = "lab.read"  # Laboratory views, reports, worklists
LAB_COLLECT = "lab.collect"  # Collect a specimen (two-identifier check)
LAB_RECEIVE = "lab.receive"  # Receive or reject a specimen in the laboratory
LAB_RESULT = "lab.result"  # Enter and technically validate a result
LAB_AUTHORISE = "lab.authorise"  # Authorise and release, amend or withdraw a result
LAB_MASTER = "lab.master"  # Test catalogue, reference ranges, critical limits
LAB_CRITICAL_LOG = "lab.critical_log"  # Log a critical-result phone callback
LAB_CRITICAL_ACK = "lab.critical_ack"  # Acknowledge a critical result on the ward, with read-back
LAB_INTERFACE = "lab.interface"  # Analyser registry and credentials
# Attach an unmatched result to a patient, or discard it. A wrong-patient
# decision that feeds the chart and Tier 1, so laboratory roles only (LIS-R17),
# never admin and never a department-scoped role: the queue shows raw
# identifiers from every ward.
INGEST_RECONCILE = "ingest.reconcile"

# Facilities (multi-site epic, ruling M1). Configuration, no patient data:
# facility identity (legal name, address, GSTIN, HFR and NABH numbers), its
# departments and document series. Creating a second facility needs
# ORB_MULTI_SITE and a group-wide grant besides facility.manage (multisite-2's
# create gate), so holding it alone never splits a hospital. Group-wide reach
# is a named, audited grant (ruling MS-R6), never a permission here.
FACILITY_READ = "facility.read"  # Facility identity and the facilities list
FACILITY_MANAGE = "facility.manage"  # Edit facility identity, departments and series

# Governance
AUDIT_READ = "audit.read"  # Read audit log entries
AUDIT_TRACE = "audit.trace"  # Trace correlation IDs
INGEST_MONITOR = "ingest.monitor"  # View ingestion status/events
ADMIN_OVERVIEW = "admin.overview"  # View admin governance dashboard


# ════════════════════════════════════════════════════════════════
#  Role → Permission Mapping
# ════════════════════════════════════════════════════════════════
# Extensible: add new roles (e.g. governance_admin, security_admin) by
# adding a new key with the desired permission set.

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "doctor": {
        PATIENT_READ_ALL,
        PATIENT_WRITE,
        VITALS_READ,
        VITALS_WRITE,
        ALERT_READ,
        MEDICATION_WRITE,
        # The prescribing decision itself — held by the doctor and by nobody
        # else in this table. Gates the allergy override at administration
        # (services/allergy_interlock.py). See the constant's note above for why
        # this is a separate permission rather than a re-used one.
        MEDICATION_PRESCRIBE,
        SCRIBE_CREATE_DRAFT,
        SCRIBE_READ_ALL,
        SCRIBE_EDIT_DRAFT,
        SCRIBE_SIGN,
        SCRIBE_REJECT,
        SAGE_QUERY,
        LENS_ANALYZE,
        LENS_READ,
        DISCHARGE_READ,
        DISCHARGE_GENERATE,
        DISCHARGE_PUBLISH,
        RELAY_READ,
        RELAY_SEND,
        RELAY_EXECUTE_ACTION,
        FORECAST_READ,
        SURGERY_READ,
        SURGERY_PREP,
        SURGERY_WRITE,
        ORDER_READ,
        ORDER_WRITE,
        # The charge nurse records outcomes (Decision 2). A doctor holds it too,
        # for the site that names the ward doctor as the shift's one person.
        OUTCOME_RECORD,
        # Consent capture at the bedside. See the constant's note above.
        CONSENT_RECORD,
        # Hospital-system build, all inert until their switches are on.
        # Registration and outpatients (OPD-N3).
        REGISTRATION_READ,
        REGISTRATION_WRITE,
        OPD_CONSULT,
        # The bed board. Moves, transfers and presence come through the
        # patient.write arm of each ADT route, not through a grant here.
        ADT_READ,
        # Specimen collection and the ward critical-result acknowledgement.
        LAB_COLLECT,
        LAB_CRITICAL_ACK,
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
        PATIENT_READ_ALL,
        VITALS_READ,
        VITALS_WRITE,
        ALERT_READ,
        MEDICATION_WRITE,
        SCRIBE_CREATE_DRAFT,
        SCRIBE_READ_ALL,
        SCRIBE_EDIT_DRAFT,
        SAGE_QUERY,
        LENS_ANALYZE,
        LENS_READ,
        DISCHARGE_READ,
        RELAY_READ,
        RELAY_SEND,
        FORECAST_READ,
        SURGERY_READ,
        SURGERY_PREP,
        SURGERY_WRITE,
        ORDER_READ,
        ORDER_WRITE,
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
        # Hospital-system build, all inert until their switches are on.
        # Ward stock, indents and returns for her own department (GRN-R8).
        WARD_STOCK_READ,
        WARD_STOCK_RECEIVE,
        WARD_INDENT_RAISE,
        WARD_RETURN_RECORD,
        # Specimen collection and the ward critical-result acknowledgement.
        LAB_COLLECT,
        LAB_CRITICAL_ACK,
        # NO ADT permission, deliberately. Ruling ADT-S7 (who records leave and
        # absconded, and whether nurses see the bed board) is held for Samuel,
        # and the strictest option is "nurses get nothing new".
    },
    "pharmacist": {
        # Limited to pharmacy workflow + Sage for clinical context
        SAGE_QUERY,
        HELIX_READ,
        HELIX_WRITE,
        # Raising an indent was reachable through HELIX_WRITE while procurement
        # borrowed helix permissions; now that procurement has its own family,
        # these two keep the pharmacist's existing reach EXPRESSED rather than
        # borrowed. Deliberately NOT procurement.approve — approving a PO,
        # issuing to a vendor or writing off stock was admin-only before the
        # split and stays that way.
        PROCUREMENT_READ,
        PROCUREMENT_WRITE,
        # NOT HELIX_RECEIVE, and this omission is load-bearing. The pharmacist
        # dispenses; the ward receives. An account holding both would be able to
        # walk an order from "scheduled" to "delivered" without the drug ever
        # leaving the room, and the custody trail would show two steps taken by
        # one person. See HELIX_RECEIVE's note above.
        #
        # The pharmacy store desk (GRN-R7), inert until ORB_PHARMACY_STORE is on.
        # NOT approve: approving a purchase order, an adjustment or a write-off
        # stays with a second person, the same split as procurement above.
        PHARMACY_STORE_READ,
        PHARMACY_STORE_WRITE,
        PHARMACY_STORE_NDPS,
        # The pharmacist's check of a medication order in the store (MO-R4 (a):
        # the pharmacist only). Inert until ORB_MED_ORDER_STORE is on.
        MEDICATION_VERIFY,
    },
    "admin": {
        # Governance + read-only clinical + pharmacy access
        PATIENT_READ_ALL,
        VITALS_READ,
        ALERT_READ,
        SCRIBE_READ_ALL,
        SAGE_QUERY,
        LENS_READ,
        DISCHARGE_READ,
        RELAY_READ,
        RELAY_SEND,
        FORECAST_READ,
        SURGERY_READ,
        SURGERY_WRITE,
        HELIX_READ,
        HELIX_WRITE,
        ORDER_READ,
        # The insurance / TPA desk. Granted to `admin` because that is who runs
        # it today — a TPA clerk is seeded as an `admin` account, which is why
        # src/shellConfig.tsx shows the tab to `admin` only — so this grant
        # changes nothing about who can reach the desk. The point of the split
        # runs the other way: the desk no longer DEPENDS on ADMIN_OVERVIEW, so
        # the day a `tpa_clerk` role exists it can be given exactly these two and
        # not the audit log.
        PAYER_READ,
        PAYER_WRITE,
        # The back-office desks, split out of ADMIN_OVERVIEW on 2026-08-18 the
        # same way (see each constant's note). All granted to `admin` because
        # admin runs every desk today, so — like the payer split — this changes
        # nothing about who can reach them. What it changes is what a future
        # billing clerk, cashier or storekeeper account has to be handed.
        BILLING_READ,
        BILLING_WRITE,
        BILLING_TARIFF,
        PAYMENTS_READ,
        PAYMENTS_RECORD,
        PAYMENTS_APPROVE,
        PAYMENTS_CLOSE_COUNTER,
        FRONTDESK_WRITE,
        PROCUREMENT_READ,
        PROCUREMENT_WRITE,
        PROCUREMENT_APPROVE,
        # Kept so the administrator can still backfill an outcome or a shift
        # confirmation on the nurse's behalf. The charge nurse records these
        # through the end-of-shift capture screen in the nurse panel
        # (src/pages/panels/nurse/OutcomeCaptureSheet.tsx); the admin grant is
        # the backfill path — curl or a future admin form — not the primary
        # one, and an operator who could record yesterday must not lose that
        # ability today.
        OUTCOME_RECORD,
        # Governance-specific
        AUDIT_READ,
        AUDIT_TRACE,
        INGEST_MONITOR,
        ADMIN_OVERVIEW,
        # Hospital-system build, all inert until their switches are on.
        # Registration reads and clinic configuration, NOT registration.write:
        # registering patients is the desk's and the doctor's job (OPD-N3).
        REGISTRATION_READ,
        SCHEDULE_MANAGE,
        # The bed manager's set: board, moves, transfers, presence, bed setup.
        ADT_READ,
        ADT_BED,
        ADT_TRANSFER,
        ADT_PRESENCE,
        BED_MASTER,
        # Pharmacy store read and approve, NOT write (GRN-R7): the pharmacist
        # raises and receives, the administrator approves.
        PHARMACY_STORE_READ,
        PHARMACY_STORE_APPROVE,
        # Laboratory views and the analyser registry. NOT ingest.reconcile:
        # attaching a result to a patient is a wrong-patient decision (LIS-R17).
        LAB_READ,
        LAB_INTERFACE,
        # Facility identity (multi-site). Facility 1's identity works with
        # ORB_MULTI_SITE off; a second facility needs the switch and a grant.
        FACILITY_READ,
        FACILITY_MANAGE,
    },
    "patient": {
        # Self-only access through Bridge
        BRIDGE_READ_OWN,
        BRIDGE_ASK,
    },
    # The cash counter, as its own account type. No seeded user holds this role
    # yet — it exists so a site can staff a counter without handing the cashier
    # a chart or the audit log. RECORD but not APPROVE: the module enforces
    # approver ≠ requester on refunds/voids in code, and the permission table
    # now agrees — the person taking money cannot authorise it leaving.
    # CLOSE_COUNTER because reconciling their own shift is the cashier's job.
    "cashier": {
        PAYMENTS_READ,
        PAYMENTS_RECORD,
        PAYMENTS_CLOSE_COUNTER,
    },
    # ── Roles added by migration 48 (users_role_batch). Each can only be
    # created while its switch is on (roles.ROLE_FEATURE), and every grant
    # below is inert until then.
    #
    # The registration desk (OPD-N3). No chart, no money, no consent capture
    # (consent.record waits for counsel), no transfers.
    "receptionist": {
        REGISTRATION_READ,
        REGISTRATION_WRITE,
    },
    # The ward clerk (adt-17): the board and same-department bed moves only.
    # No patient.read_all (no chart), no adt.transfer (ADT-S8 held: a clerk
    # never changes who can read the chart), no adt.presence (ADT-S7 held), and
    # no admission or end of stay (ADT-S3, ADT-S5 held). Department required.
    "ward_clerk": {
        ADT_READ,
        ADT_BED,
        # The Orb Ward Record (6 October 2026): the clerk keys each round from
        # the paper observation sheet. Transcribe only, never vitals.write.
        VITALS_TRANSCRIBE,
    },
    # Laboratory roles (LIS-R12): hospital-wide, no department, and none of
    # patient.read_all, relay.* or sage.query. A technician never authorises
    # (LIS-R5); only the pathologist (any authorised signatory) does.
    "phlebotomist": {
        LAB_READ,
        LAB_COLLECT,
    },
    "lab_technician": {
        LAB_READ,
        LAB_COLLECT,
        LAB_RECEIVE,
        LAB_RESULT,
        LAB_CRITICAL_LOG,
        INGEST_RECONCILE,
    },
    "pathologist": {
        LAB_READ,
        LAB_COLLECT,
        LAB_RECEIVE,
        LAB_RESULT,
        LAB_CRITICAL_LOG,
        INGEST_RECONCILE,
        LAB_AUTHORISE,
        LAB_MASTER,
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


def department_scope(user: Mapping) -> str | None:
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
#
# ward_clerk joined on 3 October 2026 (adt-17): a clerk with no department
# would see every ward's bed board. The receptionist is absent on purpose (a
# blank department is a central desk serving every clinic, OPD-N3), and so are
# the laboratory roles, which are hospital-wide by ruling LIS-R12.
DEPARTMENT_REQUIRED_ROLES: frozenset[str] = frozenset({"doctor", "nurse", "ward_clerk"})


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


# ════════════════════════════════════════════════════════════════
#  Facility scope (multisite-5a, W8-K), layered on the department scope
# ════════════════════════════════════════════════════════════════
# Samuel ruled on 3 October 2026 that isolation between the facilities of one
# hospital group is keyed on DATA (multi-site M2): once the facilities table
# holds more than one row, facility scope applies whatever ORB_MULTI_SITE says.
# With one facility (every install today) facility_scope() answers None before
# reading anything, and every helper below runs exactly as it did: the same
# statements, no extra row, no extra metadata key.
#
# facility_scope(user) is services.facility_service.facility_scope_for: a
# frozenset of facility ids, or None for no facility limit (a group-wide admin
# by enterprise grant, a service account, a patient login). A ward-scoped
# account gets its department's facility; a department-less account (admin,
# pharmacist, cashier, the laboratory and the central desk) gets its home row's
# facility or facility 1; a department the master does not list gets the empty
# set and sees no patient.
#
# FACILITY_PATIENT_SQL is the facility of the patient's LATEST encounter
# (multisite-4, W11-A, adds the outpatient visit and the booking):
#   1. an ACTIVE admission, picked with exactly PATIENT_DEPARTMENT_SQL's
#      ordering, so for an inpatient the department and the facility always
#      come from the same row, and nothing an outpatient desk writes can take
#      a patient on a ward out of that ward's scope;
#   2. otherwise the newest of every admission, every OPD visit that was not
#      cancelled and every appointment that was not cancelled, compared by
#      julianday() of the instant each was recorded (admissions.created_at,
#      opd_visits.registered_at, appointments.created_at, which are written in
#      more than one spelling), an admission first on a tie;
#   3. otherwise the facility the patient was registered at.
# Ruling MS-R5 (a): a department-less account sees the patients whose latest
# encounter is at its facility, the same rule as ward staff, and never "any
# encounter, ever" (that would give a permanent chart key to a patient now
# admitted elsewhere). So a patient discharged at one facility and then seen in
# another facility's clinic belongs to the second. While all of a patient's
# admissions are at one facility (the attribution invariant), every admission
# in step 2 names the same facility. Only read with more than one facility on
# record; opd_visits.facility_id and appointments.facility_id are migration
# 74's.
#
# enforce_patient_scope and require_patient_access apply the facility scope
# themselves, FIRST, so none of their call sites changes. A patient outside the
# caller's facilities is a 404 and writes an authorization patient_scope_denied
# row with metadata scope_cause 'facility' from BOTH helpers, whatever the
# department says (a probe across a facility boundary is always recorded). A
# denial the department alone causes keeps today's behaviour in each helper.
# Facility denials cannot happen with one facility on record.
#
# The Sage lane keeps its pinned duplicate of the department derivation;
# multisite-10 gives it the facility argument in lockstep.

FACILITY_PATIENT_SQL = (
    "COALESCE("
    "(SELECT a.facility_id FROM admissions a WHERE a.patient_id = p.id "
    " AND a.status = 'active' ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1), "
    "(SELECT e.facility_id FROM ("
    "SELECT a.facility_id AS facility_id, julianday(a.created_at) AS at, 0 AS kind, "
    "a.rowid AS seq FROM admissions a WHERE a.patient_id = p.id "
    "UNION ALL SELECT v.facility_id, julianday(v.registered_at), 1, v.rowid "
    "FROM opd_visits v WHERE v.patient_id = p.id AND v.status != 'cancelled' "
    "UNION ALL SELECT ap.facility_id, julianday(ap.created_at), 2, ap.rowid "
    "FROM appointments ap WHERE ap.patient_id = p.id AND ap.status != 'cancelled'"
    ") e ORDER BY e.at DESC, e.kind, e.seq DESC LIMIT 1), "
    "p.registered_facility_id)"
)


def facility_scope(user) -> frozenset[int] | None:
    """The facilities whose patients `user` may see, or None for no facility
    limit. BLOCKING on a cache miss (plain-def code and run_db only); with one
    facility on record and a warm cache it reads nothing."""
    return facility_service.facility_scope_for(user)


async def facility_scope_async(user) -> frozenset[int] | None:
    """facility_scope for async code: no executor hop while one facility is on
    record and the cache is warm."""
    return await facility_service.facility_scope_for_async(user)


def _facility_params(facilities) -> tuple[str, tuple]:
    ids = tuple(sorted(int(f) for f in facilities))
    return ", ".join("?" for _ in ids), ids


def scope_patient_ids_sql(
    dept: str | None, *, facilities: frozenset[int] | None = None
) -> tuple[str, tuple]:
    """A subquery yielding every patient id inside `dept`.

    Returns (sql, params) so it can be embedded as ``... IN (<sql>)``. A `dept`
    of None returns the unfiltered id set — the department_scope() convention
    for a genuinely unscoped governance account, NOT "scope unknown".

    `facilities` is the caller's facility_scope(). None leaves the subquery
    exactly as it was. An empty set yields no patient. A set keeps only the
    patients whose latest encounter is at one of those facilities
    (FACILITY_PATIENT_SQL), on the caller's ward as well when `dept` is set.
    """
    if facilities is None:
        if dept is None:
            return "SELECT p.id FROM patients p", ()
        return f"SELECT p.id FROM patients p WHERE {PATIENT_DEPARTMENT_SQL} = ?", (dept,)
    if not facilities:
        return "SELECT p.id FROM patients p WHERE 0", ()
    marks, ids = _facility_params(facilities)
    if dept is None:
        return f"SELECT p.id FROM patients p WHERE {FACILITY_PATIENT_SQL} IN ({marks})", ids
    return (
        f"SELECT p.id FROM patients p WHERE {PATIENT_DEPARTMENT_SQL} = ? "
        f"AND {FACILITY_PATIENT_SQL} IN ({marks})",
        (dept, *ids),
    )


def patient_in_scope(
    db, patient_id: str, dept: str | None, *, facilities: frozenset[int] | None = None
) -> bool:
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

    `facilities` is the caller's facility_scope(), as in scope_patient_ids_sql:
    None asks exactly the question above; otherwise the patient's latest
    encounter must also be at one of those facilities, and a patient id that
    matches no patient is not in scope.
    """
    if facilities is None:
        if dept is None:
            return True
        if not patient_id:
            return False
        return (
            db.execute(
                f"SELECT 1 FROM patients p WHERE p.id = ? AND {PATIENT_DEPARTMENT_SQL} = ?",
                (patient_id, dept),
            ).fetchone()
            is not None
        )
    if not facilities or not patient_id:
        return False
    marks, ids = _facility_params(facilities)
    if dept is None:
        return (
            db.execute(
                f"SELECT 1 FROM patients p WHERE p.id = ? AND {FACILITY_PATIENT_SQL} IN ({marks})",
                (patient_id, *ids),
            ).fetchone()
            is not None
        )
    return (
        db.execute(
            f"SELECT 1 FROM patients p WHERE p.id = ? AND {PATIENT_DEPARTMENT_SQL} = ? "
            f"AND {FACILITY_PATIENT_SQL} IN ({marks})",
            (patient_id, dept, *ids),
        ).fetchone()
        is not None
    )


def patient_in_scope_blocking(
    patient_id: str, dept: str | None, *, facilities: frozenset[int] | None = None
) -> bool:
    """patient_in_scope on its own connection — for run_db() from async routes."""
    if dept is None and facilities is None:
        return True
    with db_session() as db:
        return patient_in_scope(db, patient_id, dept, facilities=facilities)


def facility_denial(
    db, patient_id: str | None, facilities: frozenset[int] | None
) -> tuple[bool, int | None]:
    """(denied, the patient's facility) for the facility half of the scope.

    Not denied when `facilities` is None (no facility limit) or when the id
    matches no patient: the department check and the handler then answer
    exactly as they always did, so a missing patient is never recorded as a
    facility refusal. Always denied for the empty set (an account whose
    department no facility owns). One statement, and only with more than one
    facility on record.
    """
    if facilities is None:
        return False, None
    if not facilities:
        return True, None
    if not patient_id:
        return False, None
    row = db.execute(
        f"SELECT {FACILITY_PATIENT_SQL} AS fid FROM patients p WHERE p.id = ?",
        (patient_id,),
    ).fetchone()
    if row is None:
        return False, None
    fid = None if row[0] is None else int(row[0])
    return fid not in facilities, fid


def _facility_denial_blocking(
    patient_id: str | None, facilities: frozenset[int] | None
) -> tuple[bool, int | None]:
    """facility_denial on its own connection, for run_db()."""
    with db_session() as db:
        return facility_denial(db, patient_id, facilities)


def _audit_facility_denial(
    *,
    user: dict,
    patient_id: str | None,
    dept: str | None,
    facilities: frozenset[int],
    patient_facility_id: int | None,
    helper: str,
    opd_arm: bool = False,
    request_path: str | None = None,
    http_method: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """The patient_scope_denied row for a facility-caused denial. BLOCKING.

    Same category and action as the department denial in
    require_patient_access (authorization, patient_scope_denied); the
    metadata says the facility caused it and which helper refused."""
    if facilities:
        summary = f"Patient {patient_id} is outside the caller's facility scope"
    else:
        summary = (
            f"Patient {patient_id} is out of scope: the caller's department "
            f"({dept}) belongs to no facility on record"
        )
    metadata = {
        "department": dept,
        "scope_source": "authenticated_user",
        "scope_cause": "facility",
        "facility_scope": sorted(facilities),
        "patient_facility_id": patient_facility_id,
        "scope_helper": helper,
    }
    if opd_arm:
        metadata["opd_arm"] = True
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="patient_scope_denied",
        outcome="denied",
        summary_message=summary,
        actor_user_id=(user or {}).get("id"),
        actor_role=(user or {}).get("role"),
        patient_id=patient_id,
        request_path=request_path,
        http_method=http_method,
        ip_address=ip_address,
        user_agent=user_agent,
        metadata=metadata,
    )


# ════════════════════════════════════════════════════════════════
#  The OPD arm (opd-7, W3-A): an OPT-IN second scope predicate
# ════════════════════════════════════════════════════════════════
# An outpatient has no admission, so patient_in_scope above fail-closes them
# out of every department, and that is right for every chart route. The OPD
# record view needs one more answer: "this clinic saw the patient recently".
#
# That answer is a SEPARATE predicate, deliberately not a widening of
# patient_in_scope, scope_patient_ids_sql or PATIENT_DEPARTMENT_SQL:
#
#   * require_patient_access is one guard for reads AND writes. Widening the
#     shared predicate would have opened medication add, nurse obs, allergy and
#     problem writes, bundles, discharge, the Vigil chart and FHIR export to
#     anyone whose clinic registered a visit (the design critique's blocker).
#   * Sage keeps a pinned duplicate of patient_in_scope
#     (tests/test_patient_scope_routes.py::test_the_route_scope_and_the_sage_scope_agree)
#     and its provenance lane reads scope_patient_ids_sql. Both stay untouched.
#
# patient_in_opd_scope is reached ONLY through require_patient_access(...,
# opd_arm=True) or enforce_patient_scope(..., opd_arm=True), on the whitelist
# that tests/test_opd_scope_drift.py pins with a reason per route. Nothing else
# may call it; the same test fails a direct call from any other module.
#
# The rule (ruling OPD-N2, applied 3 October 2026: 7 days, extent ii,
# self-registered visits count and are flagged and audited):
#   the ordinary scope (patient_in_scope), OR
#   the 'opd' feature is on (checked at call time), AND the patient has no
#   ACTIVE admission anywhere, AND a non-cancelled opd_visits row in the
#   caller's department (compared case-folded) is dated within the last
#   ORB_OPD_SCOPE_DAYS days of the hospital's calendar, today included.
#
# Two strict edges, chosen here and listed for Samuel:
#   * a visit dated in the FUTURE (a booking) opens nothing until its day;
#   * an active admission anywhere closes the arm completely, so a visit can
#     never launder an inpatient of another ward onto a clinic's record view.


def opd_scope_window(today=None) -> tuple[str, str]:
    """(first_day, last_day) of the OPD scope window, hospital-local YYYY-MM-DD.

    Read at call time from config.OPD_SCOPE_DAYS, so a test (and an operator's
    restart) sees the current setting. 0 means the visit's own day only.
    """
    from datetime import timedelta

    from services.hospital_time import hospital_today

    last = today or hospital_today()
    days = max(0, int(getattr(config, "OPD_SCOPE_DAYS", 7) or 0))
    return (last - timedelta(days=days)).isoformat(), last.isoformat()


def opd_scope_open() -> bool:
    """Is the OPD arm switched on right now (the 'opd' feature, prerequisites
    included)? Off means the arm answers exactly what patient_in_scope does."""
    return config.feature_enabled("opd")


def _patient_has_active_admission(db, patient_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM admissions WHERE patient_id = ? AND status = 'active' LIMIT 1",
            (patient_id,),
        ).fetchone()
        is not None
    )


def _opd_scope_visit(db, patient_id: str, dept: str) -> dict | None:
    """The newest visit that opens the OPD arm for `dept`, or None.

    Only called when the feature is on and the patient has no active admission.
    The department is compared in Python with casefold() (SQLite's lower() folds
    ASCII only); a patient has a handful of visits, read through
    idx_opd_patient (patient_id, visit_date DESC).
    """
    first_day, last_day = opd_scope_window()
    rows = db.execute(
        "SELECT id, department, visit_date, scope_self_registered FROM opd_visits "
        " WHERE patient_id = ? AND status != 'cancelled' "
        "   AND visit_date >= ? AND visit_date <= ? "
        " ORDER BY visit_date DESC, registered_at DESC, id DESC",
        (patient_id, first_day, last_day),
    ).fetchall()
    wanted = dept.strip().casefold()
    for row in rows:
        if (row["department"] or "").strip().casefold() == wanted:
            return {
                "visit_id": str(row["id"]),
                "visit_date": row["visit_date"],
                "self_registered": bool(row["scope_self_registered"]),
            }
    return None


def opd_scope_decision(db, patient_id: str, dept: str | None) -> dict | None:
    """Which arm lets a caller scoped to `dept` read this patient, or None.

    Returns {"arm": "hospital_wide"} for an unscoped account, {"arm":
    "admission"} when the ordinary scope holds, or {"arm": "opd_visit",
    "visit_id", "visit_date", "self_registered", "scope_until"} when only an
    OPD visit does. The arm is recorded on the read's audit row.
    """
    if dept is None:
        return {"arm": "hospital_wide"}
    if not patient_id:
        return None
    if patient_in_scope(db, patient_id, dept):
        return {"arm": "admission"}
    if not opd_scope_open():
        return None
    if _patient_has_active_admission(db, patient_id):
        return None
    visit = _opd_scope_visit(db, patient_id, dept)
    if visit is None:
        return None
    from datetime import date, timedelta

    days = max(0, int(getattr(config, "OPD_SCOPE_DAYS", 7) or 0))
    until = date.fromisoformat(visit["visit_date"]) + timedelta(days=days)
    return {"arm": "opd_visit", **visit, "scope_until": until.isoformat()}


def patient_in_opd_scope(db, patient_id: str, dept: str | None) -> bool:
    """patient_in_scope, OR the OPD arm (see the block comment above).

    With the 'opd' feature off this answers exactly what patient_in_scope
    answers, for every patient and every department.
    """
    return opd_scope_decision(db, patient_id, dept) is not None


def patient_in_opd_scope_blocking(patient_id: str, dept: str | None) -> bool:
    """patient_in_opd_scope on its own connection, for run_db()."""
    if dept is None:
        return True
    with db_session() as db:
        return patient_in_opd_scope(db, patient_id, dept)


def opd_scope_decision_blocking(patient_id: str, dept: str | None) -> dict | None:
    """opd_scope_decision on its own connection, for run_db()."""
    if dept is None:
        return {"arm": "hospital_wide"}
    with db_session() as db:
        return opd_scope_decision(db, patient_id, dept)


def enforce_patient_scope(user: dict, patient_id: str | None, *, opd_arm: bool = False) -> None:
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

    `opd_arm=True` asks patient_in_opd_scope instead (opd-7). Only a route on
    the whitelist in tests/test_opd_scope_drift.py may pass it, and never a
    chart route: the arm opens the OPD record view, not the inpatient chart.

    FACILITY SCOPE (multisite-5a). With more than one facility on record the
    caller's facility_scope() is checked first: a patient whose latest
    encounter is outside it is a 404 that writes an authorization
    patient_scope_denied row (metadata scope_cause 'facility'), whatever the
    department says. A denial the department alone causes still writes no
    row here. With one facility nothing changes: no statement, no row.
    """
    dept = department_scope(user)
    if not patient_id:
        return
    facilities = facility_scope(user)
    if dept is None and facilities is None:
        return
    denied, patient_facility = False, None
    allowed = True
    with db_session() as db:
        if facilities is not None:
            denied, patient_facility = facility_denial(db, patient_id, facilities)
        if not denied and dept is not None:
            allowed = (
                patient_in_opd_scope(db, patient_id, dept)
                if opd_arm
                else patient_in_scope(db, patient_id, dept)
            )
    if denied:
        assert facilities is not None
        # Written after the read session closed, before the 404.
        _audit_facility_denial(
            user=user,
            patient_id=patient_id,
            dept=dept,
            facilities=facilities,
            patient_facility_id=patient_facility,
            helper="enforce_patient_scope",
            opd_arm=opd_arm,
        )
        raise HTTPException(status_code=404, detail="Patient not found")
    if not allowed:
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

    With more than one facility on record the row also carries
    metadata.facility_scope: the reader's facility ids, or None for a
    group-wide account (multisite-5a). Set after the caller's metadata, so the
    recorded scope is always the server's. With one facility the key is
    absent and the row is exactly as before.
    """
    dept = department_scope(user)
    meta: dict[str, Any] = {
        "department": dept,
        # True only for an account with no department on record — an
        # admin-settable property of the USER ROW, never a request parameter.
        "hospital_wide": dept is None,
        "scope_source": "authenticated_user",
    }
    if metadata:
        meta.update(metadata)
    if facility_service.multi_facility_data():
        facilities = facility_scope(user)
        meta["facility_scope"] = None if facilities is None else sorted(facilities)
    log_audit_event(
        correlation_id=str(uuid.uuid4()),
        event_category="phi_access",  # fail-closed
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


def _log_patient_read_blocking(
    *,
    request: Request,
    user: dict,
    action_type: str,
    patient_id: str | None,
    metadata: dict | None = None,
) -> None:
    """require_patient_access's read audit, written only once the patient is known.

    Writing "success" before the handler had looked the patient up put a
    successful chart view on the chain for an id that matches nobody, which is
    the opposite of what an investigator reading the trail needs. A missing
    patient is still recorded (a probe for ids is worth seeing), as a failure.
    Both steps run in one worker call, so the route pays one thread hop as before.

    `metadata` is added to the row (the OPD arm records which arm let the
    reader in); None leaves the row exactly as it always was.
    """
    if patient_id:
        with db_session() as db:
            exists = (
                db.execute("SELECT 1 FROM patients WHERE id = ?", (patient_id,)).fetchone()
                is not None
            )
        if not exists:
            log_phi_read(
                request=request,
                user=user,
                action_type=action_type,
                summary_message=f"{action_type} for patient {patient_id}: no such patient",
                patient_id=patient_id,
                outcome="failure",
                metadata={"reason": "not_found", **(metadata or {})},
            )
            return
    log_phi_read(
        request=request,
        user=user,
        action_type=action_type,
        summary_message=(f"{action_type} for patient {patient_id}" if patient_id else action_type),
        patient_id=patient_id,
        metadata=metadata,
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

    _CHECKER_USER_DEFAULT = Depends(get_current_user)

    async def _checker(
        request: Request,
        user: dict = _CHECKER_USER_DEFAULT,
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
                    "message": "Insufficient permissions",
                    "required": list(permissions),
                    "missing": missing,
                },
            )

        return user

    return _checker


def require_any_permission(*permissions: str):
    """Dependency factory: require the user to hold AT LEAST ONE listed permission.

    Same denial shape as require_permission (a permission_denied audit row,
    then a 403), so a caller holding none of the arms is refused exactly as
    before. Used where two kinds of account reach one write for different
    reasons: the observations route takes vitals.write (a nurse or doctor at
    the bedside) OR vitals.transcribe (the ward clerk copying the paper sheet).
    """

    _CHECKER_USER_DEFAULT = Depends(get_current_user)

    async def _checker(
        request: Request,
        user: dict = _CHECKER_USER_DEFAULT,
    ) -> dict:
        role = user.get("role", "")
        user_perms = ROLE_PERMISSIONS.get(role, set())
        if not any(p in user_perms for p in permissions):
            await run_db(
                log_audit_event,
                correlation_id=str(uuid.uuid4()),
                event_category="authorization",
                action_type="permission_denied",
                outcome="denied",
                summary_message=f"Missing any of permissions: {', '.join(permissions)}",
                actor_user_id=user.get("id"),
                actor_role=role,
                request_path=request.url.path,
                http_method=request.method,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                metadata={
                    "required_any_of": list(permissions),
                    "user_role": role,
                },
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "message": "Insufficient permissions",
                    "required_any_of": list(permissions),
                    "missing": list(permissions),
                },
            )
        return user

    return _checker


# ════════════════════════════════════════════════════════════════
#  FastAPI Dependency: require_patient_access
# ════════════════════════════════════════════════════════════════


def require_patient_access(
    *permissions: str, audit_action: str | None = None, opd_arm: bool = False, any_of: bool = False
):
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

    `opd_arm=True` (opd-7) asks patient_in_opd_scope instead of
    patient_in_scope, records which arm let the reader in on the audit row
    (metadata scope_arm, plus the visit for the OPD arm), and leaves the
    decision on request.state.orb_patient_scope for the handler to show. ONLY a
    route on the whitelist in tests/test_opd_scope_drift.py may pass it; every
    chart route, read or write, keeps the default and stays admission-only.
    The returned dependency carries `orb_opd_arm` so that test can find it.

    `any_of=True` asks for ANY ONE of `permissions` instead of all of them
    (require_any_permission). The patient scope, the 404 and the scope-denied
    audit row below are the same either way.

    FACILITY SCOPE (multisite-5a). With more than one facility on record the
    caller's facility_scope() is checked first, in its own worker call: a
    patient whose latest encounter is outside it is a 404 with a
    patient_scope_denied row whose metadata says scope_cause 'facility'. The
    department checks below then run exactly as before. With one facility and
    a warm cache this adds no executor hop and no statement.
    """
    permission_checker = (
        require_any_permission(*permissions) if any_of else require_permission(*permissions)
    )

    _CHECKER_USER_DEFAULT = Depends(permission_checker)

    async def _checker(
        request: Request,
        user: dict = _CHECKER_USER_DEFAULT,
    ) -> dict:
        patient_id = request.path_params.get("patient_id")
        dept = department_scope(user)
        read_metadata: dict | None = None

        facilities = await facility_scope_async(user)
        if facilities is not None and patient_id:
            denied, patient_facility = await run_db(
                _facility_denial_blocking, patient_id, facilities
            )
            if denied:
                await run_db(
                    _audit_facility_denial,
                    user=user,
                    patient_id=patient_id,
                    dept=dept,
                    facilities=facilities,
                    patient_facility_id=patient_facility,
                    helper="require_patient_access",
                    opd_arm=opd_arm,
                    request_path=request.url.path,
                    http_method=request.method,
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
                raise HTTPException(status_code=404, detail="Patient not found")

        if opd_arm:
            if dept is None:
                decision = {"arm": "hospital_wide"}
            elif not patient_id:
                # Fail closed: the arm only ever scopes a patient named in the
                # path, so a scoped caller with no patient id gets nothing.
                decision = None
            else:
                decision = await run_db(opd_scope_decision_blocking, patient_id, dept)
            if decision is None:
                await run_db(
                    log_audit_event,
                    correlation_id=str(uuid.uuid4()),
                    event_category="authorization",
                    action_type="patient_scope_denied",
                    outcome="denied",
                    summary_message=(
                        f"Patient {patient_id} is outside the caller's department "
                        f"({dept}), outpatient visits included"
                    ),
                    actor_user_id=user.get("id"),
                    actor_role=user.get("role"),
                    patient_id=patient_id,
                    request_path=request.url.path,
                    http_method=request.method,
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    metadata={
                        "department": dept,
                        "scope_source": "authenticated_user",
                        "opd_arm": True,
                    },
                )
                raise HTTPException(status_code=404, detail="Patient not found")
            request.state.orb_patient_scope = dict(decision)
            read_metadata = {"scope_arm": decision.get("arm"), "opd_arm": True}
            if decision.get("arm") == "opd_visit":
                read_metadata.update(
                    {
                        "opd_visit_id": decision.get("visit_id"),
                        "opd_visit_date": decision.get("visit_date"),
                        "opd_scope_until": decision.get("scope_until"),
                        "self_registered": bool(decision.get("self_registered")),
                    }
                )
        elif dept is not None and patient_id:
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
                        f"Patient {patient_id} is outside the caller's department ({dept})"
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
            if read_metadata is None:
                await run_db(
                    _log_patient_read_blocking,
                    request=request,
                    user=user,
                    action_type=audit_action,
                    patient_id=patient_id,
                )
            else:
                await run_db(
                    _log_patient_read_blocking,
                    request=request,
                    user=user,
                    action_type=audit_action,
                    patient_id=patient_id,
                    metadata=read_metadata,
                )

        return user

    # Read by tests/test_opd_scope_drift.py, which walks every route's
    # dependency tree for this mark and pins each route that carries it.
    _checker.__dict__["orb_opd_arm"] = bool(opd_arm)
    return _checker


# ════════════════════════════════════════════════════════════════
#  FastAPI Dependency: require_registration_desk (opd-3, W2-E)
# ════════════════════════════════════════════════════════════════
# The registration desk is reached by two kinds of account that hold different
# permissions for the same job:
#
#   * a clinician or the administrator, who reach the desk through the chart
#     permissions they already hold (patient.read_all to read, patient.write or
#     frontdesk.write to register), exactly as before 3 October 2026;
#   * the receptionist (ruling OPD-N3), who holds registration.read and
#     registration.write and NOTHING on the chart.
#
# A dependency that hung off patient.read_all alone could never admit the
# receptionist without handing her the chart, so the desk asks "any one of these
# arms" instead. The registration.* arms count ONLY while the 'registration'
# feature is on (ORB_REGISTRATION, checked at request time): with it off this
# dependency is, denial audit and 403 body included, the old front-desk rule
# (require_frontdesk_read / require_frontdesk_write in routes/frontdesk_routes),
# so a site that never turns the feature on sees no change at all.
#
# WRITE IS READ PLUS A WRITE ARM. Every holder of a write arm today also holds a
# read arm (doctor and admin hold patient.read_all, the receptionist holds
# registration.read), so asking for both refuses nobody who works the desk, and
# a future role handed a write arm alone cannot register into a queue it could
# not read.
#
# What this does NOT open: any chart route (they stay on patient.read_all and
# require_patient_access), transfers (they keep require_frontdesk_write, so a
# receptionist cannot move a patient and with it the chart), money (payments.*
# and billing.* are untouched), and calling a patient in or completing a
# consultation (those stay on the front-desk write rule until opd.consult takes
# them over with the OPD feature).

REGISTRATION_DESK_READ_ARMS: tuple[str, ...] = (PATIENT_READ_ALL, REGISTRATION_READ)
REGISTRATION_DESK_WRITE_ARMS: tuple[str, ...] = (FRONTDESK_WRITE, PATIENT_WRITE, REGISTRATION_WRITE)

# What the screen says on a refusal while the feature is on. Plain words: the
# message reaches the desk's error line, and the permission names stay in
# `required` for anything that needs them.
_DESK_DENIAL_WORDS = {
    "read": "Your account cannot open the registration desk.",
    "write": "Your account cannot register or change visits at the registration desk.",
}


def registration_desk_on() -> bool:
    """Is the registration feature on right now? Read at call time, so a test
    (and an operator's restart) sees the current switch."""
    return config.feature_enabled("registration")


def registration_desk_arms(mode: str, *, desk_on: bool | None = None) -> tuple[str, ...]:
    """The permissions that open the desk in `mode` ('read' or 'write') now.

    With the registration feature off only the pre-existing arms count, so a
    receptionist account left over from a site that switched the feature off
    holds nothing here."""
    if mode not in ("read", "write"):
        raise ValueError(f"registration desk mode must be 'read' or 'write', not {mode!r}")
    arms = REGISTRATION_DESK_READ_ARMS if mode == "read" else REGISTRATION_DESK_WRITE_ARMS
    on = registration_desk_on() if desk_on is None else desk_on
    if on:
        return arms
    return tuple(p for p in arms if p not in (REGISTRATION_READ, REGISTRATION_WRITE))


def may_use_registration_desk(role: str, mode: str, *, desk_on: bool | None = None) -> bool:
    """Does `role` pass the desk check in `mode`? Write needs a read arm too."""
    on = registration_desk_on() if desk_on is None else desk_on
    can_read = any(has_permission(role, p) for p in registration_desk_arms("read", desk_on=on))
    if mode == "read" or not can_read:
        return can_read
    return any(has_permission(role, p) for p in registration_desk_arms("write", desk_on=on))


async def _deny_registration_desk(request: Request, user: dict, mode: str) -> Never:
    """The audited refusal while the feature is on. Raises 403 once the denial
    is on the hash chain (a blocking await, like require_permission's)."""
    role = user.get("role", "")
    required = list(REGISTRATION_DESK_READ_ARMS if mode == "read" else REGISTRATION_DESK_WRITE_ARMS)
    await run_db(
        log_audit_event,
        correlation_id=str(uuid.uuid4()),
        event_category="authorization",
        action_type="permission_denied",
        outcome="denied",
        summary_message=f"Registration desk {mode} requires one of: {', '.join(required)}",
        actor_user_id=user.get("id"),
        actor_role=role,
        request_path=request.url.path,
        http_method=request.method,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={
            "required_any_of": required,
            "desk_mode": mode,
            "user_role": role,
            "registration_feature": True,
        },
    )
    raise HTTPException(
        status_code=403,
        detail={
            "error": "permission_denied",
            "message": _DESK_DENIAL_WORDS[mode],
            "required": required,
        },
    )


async def _no_desk_gate() -> None:
    """The default `gate`: nothing between the read and the write check."""
    return None


def require_registration_desk(mode: str, *, gate=None):
    """Dependency factory for the registration desk: 'read' or 'write'.

    read:  patient.read_all OR registration.read.
    write: the read check, then frontdesk.write OR patient.write OR
           registration.write.
    The registration.* arms count only while the 'registration' feature is on;
    with it off the denials are the old front-desk ones, byte for byte (the
    read refusal IS require_permission(patient.read_all)'s, called directly).

    `gate` is an optional extra dependency resolved after the read check and
    before the write check, which is where the front desk's module switch always
    sat (routes/frontdesk_routes.require_frontdesk_module): a signed-in caller
    who may read gets the module's 404 before any write refusal, as before.

    Every refusal is audited before the 403 (authorization/permission_denied).
    No database work on the happy path, so the check stays on the loop.
    """
    if mode not in ("read", "write"):
        raise ValueError(f"registration desk mode must be 'read' or 'write', not {mode!r}")
    patient_read_all_checker = require_permission(PATIENT_READ_ALL)
    extra_gate = gate or _no_desk_gate

    _READ_CHECK_USER_DEFAULT = Depends(get_current_user)

    async def _read_check(request: Request, user: dict = _READ_CHECK_USER_DEFAULT) -> dict:
        role = user.get("role", "")
        if registration_desk_on():
            if may_use_registration_desk(role, "read", desk_on=True):
                return user
            await _deny_registration_desk(request, user, "read")
        # Feature off: the rule the desk always had, refusal included.
        return await patient_read_all_checker(request, user)

    if mode == "read":
        _CHECKER_USER_DEFAULT = Depends(_read_check)
        _CHECKER_GATE_DEFAULT = Depends(extra_gate)

        async def _checker_first(
            user: dict = _CHECKER_USER_DEFAULT,
            _gate: None = _CHECKER_GATE_DEFAULT,
        ) -> dict:
            return user
    else:
        _CHECKER_USER_DEFAULT = Depends(_read_check)
        _CHECKER_GATE_DEFAULT = Depends(extra_gate)

        async def _checker_second(
            request: Request,
            user: dict = _CHECKER_USER_DEFAULT,
            _gate: None = _CHECKER_GATE_DEFAULT,
        ) -> dict:
            role = user.get("role", "")
            desk_on = registration_desk_on()
            if any(
                has_permission(role, p) for p in registration_desk_arms("write", desk_on=desk_on)
            ):
                return user
            if desk_on:
                await _deny_registration_desk(request, user, "write")
            # Feature off: the front desk's own refusal, unchanged (it was
            # require_frontdesk_write's, written from a threadpooled sync def;
            # the same row is written here through run_db).
            await run_db(
                log_audit_event,
                correlation_id=str(uuid.uuid4()),
                event_category="authorization",
                action_type="permission_denied",
                outcome="denied",
                summary_message="Front-desk write requires patient.write or frontdesk.write",
                actor_user_id=user.get("id"),
                actor_role=role,
                request_path=request.url.path,
                http_method=request.method,
                ip_address=request.client.host if request.client else None,
                metadata={"required_permissions": [PATIENT_WRITE, FRONTDESK_WRITE]},
            )
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "message": "Front-desk actions require patient.write or frontdesk.write",
                    "required": [PATIENT_WRITE, FRONTDESK_WRITE],
                },
            )

    _checker = _checker_first if mode == "read" else _checker_second

    _checker.__name__ = f"require_registration_desk_{mode}"
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

    _CHECKER_USER_DEFAULT = Depends(get_current_user)

    async def _checker(
        request: Request,
        user: dict = _CHECKER_USER_DEFAULT,
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
