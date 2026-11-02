/** Client-side permission mirror.
 *
 *  THE SERVER IS AUTHORITATIVE. Nothing here grants access — the table below is
 *  a mirror of `server/permissions.py:ROLE_PERMISSIONS`, used only to avoid
 *  offering a control whose request we already know will be refused. Its job is
 *  to keep the console clean and the UI honest, never to decide what a user may
 *  read.
 *
 *  GATE ON THE PERMISSION, NEVER ON A ROLE NAME. A role list written at a call
 *  site ("everyone except the pharmacist") drifts the instant a role is added or
 *  a grant moves, and it drifts SILENTLY: the button keeps rendering, the write
 *  keeps 403ing, and the only person who finds out is the clinician who pressed
 *  it. Both defects this file was extended for were exactly that — the admit CTA
 *  rendered for nurses and admins, neither of whom holds `patient.write`.
 *
 *  Only the permissions the UI actually gates on are mirrored; the server's set
 *  is far larger and copying all of it here would be surface that rots unread.
 *  Each entry was checked against server/permissions.py on 2026-08-11. The real
 *  end state is the server naming the caller's permissions in the login/me
 *  payload, at which point this table is deleted rather than maintained. */
import { getUser } from '../api/client'

export const PATIENT_READ_ALL = 'patient.read_all' as const
export const PATIENT_WRITE = 'patient.write' as const
export const OUTCOME_RECORD = 'outcome.record' as const
/** Resolve Sage's proposals — and, since 2026-08-14, Sage's REASONING: the
 *  approve/decline verdict on a drafted alert assessment is gated on this same
 *  permission server-side (`routes/relay_routes.py::transition_sage_assessment`).
 *  Mirrored here so a nurse — who holds `relay.read` and can read every word of
 *  the draft — is not offered two buttons whose POST is already known to 403.
 *  The alternative at the call site was "hide these unless role === 'doctor'",
 *  which is exactly the role-name gate the docstring above forbids. */
export const RELAY_EXECUTE_ACTION = 'relay.execute_action' as const
/** THE PRESCRIBING DECISION ITSELF — and, since 2026-08-15, the only permission
 *  that can clear the allergy interlock over a charted allergy
 *  (server/services/allergy_interlock.py). Mirrored here so the Administer modal
 *  does not offer a nurse an override control whose POST the server answers with
 *  403; she gets the block, the allergen and a sentence naming who can authorise
 *  it instead.
 *
 *  Deliberately NOT `medication.write`: nurse and doctor both hold that one (a
 *  nurse must be able to record a dose), so gating the control on it would be
 *  the same as not gating it. Hiding a control is never the control — the server
 *  refuses regardless — but a control that is offered and then refused teaches a
 *  clinician that the software is broken, which is its own hazard at 03:00. */
export const MEDICATION_PRESCRIBE = 'medication.prescribe' as const
/** Confirming that a dispensed drug ARRIVED ON THE WARD. Mirrored so the nurse
 *  panel's Deliveries tab is offered to the people who can actually act in it,
 *  and so it is gated on the permission rather than on `role === 'nurse'` —
 *  the role-name gate this file's docstring forbids. Deliberately absent from
 *  the pharmacist: the desk that dispensed cannot sign for its own dispatch
 *  (server/permissions.py HELIX_RECEIVE). */
export const HELIX_RECEIVE = 'helix.receive' as const
/** Discharging a patient (drafting the summary, closing the encounter) and
 *  releasing the drafted summary to the patient after reading it. Both are the
 *  prescriber's on the server; a nurse and an administrator hold only
 *  `discharge.read`. Mirrored so the chart does not offer a DISCHARGE button
 *  whose POST is already known to 403. */
export const DISCHARGE_GENERATE = 'discharge.generate' as const
export const DISCHARGE_PUBLISH = 'discharge.publish' as const
/** Recording an observation set at the bedside (nurse, doctor), and marking
 *  any recorded set "entered in error". The clerk, who copies sets from the
 *  paper sheet under VITALS_TRANSCRIBE, is offered the void action only on her
 *  own copies, the one case the server lets her strike out (Orb Ward Record,
 *  6 and 9 October 2026). */
export const VITALS_WRITE = 'vitals.write' as const
/** Copying an observation set from the paper sheet: the ward clerk only. Her
 *  entry sheet asks for the time written on the sheet and shows no score. */
export const VITALS_TRANSCRIBE = 'vitals.transcribe' as const
/** The per-shift nursing note (21 October 2026): written by the nurse on her
 *  own ward, read by the nurse and the doctor. Mirrored so the shift board
 *  offers the Nursing note button only to an account whose write will not
 *  403. Off under the Orb Ward Record profile, where nursing notes stay on
 *  paper. */
export const NURSING_NOTE_WRITE = 'nursing_note.write' as const
export const NURSING_NOTE_READ = 'nursing_note.read' as const
/** Recording (and withdrawing) a patient's consent. The doctor's on the server.
 *  Mirrored so the registration step offers the privacy notice and consent
 *  questions only to an account whose request will not 403 (opd-19). Ruling
 *  OPD-N3: the receptionist gets it once counsel agrees, never before; that day
 *  it is one line here and one in server/permissions.py. */
export const CONSENT_RECORD = 'consent.record' as const

/* ── The HIS permissions (build plan of 3 October 2026, wave 1) ─────────────
 *
 * Unlike the set above, these are mirrored IN FULL rather than only where the
 * UI already gates on them: the registration desk, the bed board, the pharmacy
 * store and the laboratory are each built over later waves by different
 * builders, and every one of them needs the same answer to "does this account
 * hold X?" without reopening this file. The strings are the server's exactly
 * (server/permissions.py). Every grant below is inert until its feature is on,
 * because every route that checks one answers 404 while its flag is off. */

/** Registration desk and the OPD (rulings OPD-N3). */
export const REGISTRATION_READ = 'registration.read' as const
export const REGISTRATION_WRITE = 'registration.write' as const
export const OPD_CONSULT = 'opd.consult' as const
export const SCHEDULE_MANAGE = 'schedule.manage' as const

/** Beds and admissions. ADT_BED never changes the treating department;
 *  ADT_TRANSFER does (a scope change). ADT_PRESENCE records leave and
 *  absconded. BED_MASTER is bed setup. */
export const ADT_READ = 'adt.read' as const
export const ADT_BED = 'adt.bed' as const
export const ADT_TRANSFER = 'adt.transfer' as const
export const ADT_PRESENCE = 'adt.presence' as const
export const BED_MASTER = 'bed.master' as const

/** Pharmacy store (rulings GRN-R7 and GRN-R8). */
export const PHARMACY_STORE_READ = 'pharmacy_store.read' as const
export const PHARMACY_STORE_WRITE = 'pharmacy_store.write' as const
export const PHARMACY_STORE_APPROVE = 'pharmacy_store.approve' as const
export const PHARMACY_STORE_NDPS = 'pharmacy_store.ndps' as const
export const WARD_STOCK_READ = 'ward_stock.read' as const
export const WARD_STOCK_RECEIVE = 'ward_stock.receive' as const
export const WARD_RETURN_RECORD = 'ward_return.record' as const
export const WARD_INDENT_RAISE = 'ward_indent.raise' as const

/** Laboratory (ruling LIS-R12). INGEST_RECONCILE attaches or discards an
 *  unmatched result, a wrong-patient decision, so it is never held by a
 *  department-scoped role. */
export const LAB_READ = 'lab.read' as const
export const LAB_COLLECT = 'lab.collect' as const
export const LAB_RECEIVE = 'lab.receive' as const
export const LAB_RESULT = 'lab.result' as const
export const LAB_AUTHORISE = 'lab.authorise' as const
export const LAB_MASTER = 'lab.master' as const
export const LAB_CRITICAL_LOG = 'lab.critical_log' as const
export const LAB_CRITICAL_ACK = 'lab.critical_ack' as const
export const LAB_INTERFACE = 'lab.interface' as const
export const INGEST_RECONCILE = 'ingest.reconcile' as const

/** Medication orders (the order store, W5-E). The pharmacist's check of an
 *  order: verify or reject it in the store's verification queue. Ruling MO-R4
 *  (a): the pharmacist only, never the administrator. */
export const MEDICATION_VERIFY = 'medication.verify' as const

/** Every HIS permission above, in one list, for tests and for later waves. */
export const HIS_PERMISSIONS = [
  REGISTRATION_READ, REGISTRATION_WRITE, OPD_CONSULT, SCHEDULE_MANAGE,
  ADT_READ, ADT_BED, ADT_TRANSFER, ADT_PRESENCE, BED_MASTER,
  PHARMACY_STORE_READ, PHARMACY_STORE_WRITE, PHARMACY_STORE_APPROVE, PHARMACY_STORE_NDPS,
  WARD_STOCK_READ, WARD_STOCK_RECEIVE, WARD_RETURN_RECORD, WARD_INDENT_RAISE,
  LAB_READ, LAB_COLLECT, LAB_RECEIVE, LAB_RESULT, LAB_AUTHORISE, LAB_MASTER,
  LAB_CRITICAL_LOG, LAB_CRITICAL_ACK, LAB_INTERFACE, INGEST_RECONCILE,
  MEDICATION_VERIFY,
] as const

export type HisPermission = (typeof HIS_PERMISSIONS)[number]

export const FEEDBACK_READ = 'feedback.read' as const
export const FEEDBACK_WRITE = 'feedback.write' as const
export const FEEDBACK_MANAGE = 'feedback.manage' as const
export const MORTUARY_READ = 'mortuary.read' as const
export const MORTUARY_WRITE = 'mortuary.write' as const
export const MORTUARY_MANAGE = 'mortuary.manage' as const
export const MORTUARY_RELEASE = 'mortuary.release' as const
export const CSSD_READ = 'cssd.read' as const
export const CSSD_WRITE = 'cssd.write' as const
export const CSSD_MANAGE = 'cssd.manage' as const
export const CSSD_RELEASE = 'cssd.release' as const
export const REPORTING_READ = 'reporting.read' as const
export const REPORTING_WRITE = 'reporting.write' as const
export const REPORTING_EXPORT = 'reporting.export' as const
export const FINANCE_TOOLS_READ = 'finance_tools.read' as const
export const FINANCE_TOOLS_WRITE = 'finance_tools.write' as const
export const FINANCE_TOOLS_APPROVE = 'finance_tools.approve' as const
export const FINANCE_TOOLS_EXPORT = 'finance_tools.export' as const
export const RIS_READ = 'ris.read' as const
export const RIS_WRITE = 'ris.write' as const
export const RIS_SIGN = 'ris.sign' as const
export const CARE_PLAN_READ = 'care_plan.read' as const
export const CARE_PLAN_WRITE = 'care_plan.write' as const
export const EMERGENCY_RECORD_READ = 'emergency_record.read' as const
export const EMERGENCY_RECORD_WRITE = 'emergency_record.write' as const
export const PERIOPERATIVE_RECORD_READ = 'perioperative_record.read' as const
export const PERIOPERATIVE_RECORD_WRITE = 'perioperative_record.write' as const
export const PERIOPERATIVE_RECORD_SIGN = 'perioperative_record.sign' as const
export const DIALYSIS_RECORD_READ = 'dialysis_record.read' as const
export const DIALYSIS_RECORD_WRITE = 'dialysis_record.write' as const
export const DIALYSIS_RECORD_PRESCRIBE = 'dialysis_record.prescribe' as const
export const RECORD_GOVERNANCE_READ = 'record_governance.read' as const
export const RECORD_GOVERNANCE_WRITE = 'record_governance.write' as const
export const RECORD_GOVERNANCE_RELEASE = 'record_governance.release' as const

export const RADIATION_SAFETY_READ = 'radiation_safety.read'
export const RADIATION_SAFETY_WRITE = 'radiation_safety.write'
export const INFECTION_SURVEILLANCE_READ = 'infection_surveillance.read'
export const INFECTION_SURVEILLANCE_WRITE = 'infection_surveillance.write'
export const INFECTION_SURVEILLANCE_MANAGE = 'infection_surveillance.manage'
export const OFFLINE_PERMISSIONS = [RADIATION_SAFETY_READ, RADIATION_SAFETY_WRITE, INFECTION_SURVEILLANCE_READ, INFECTION_SURVEILLANCE_WRITE, INFECTION_SURVEILLANCE_MANAGE,
  FEEDBACK_READ,
  FEEDBACK_WRITE,
  FEEDBACK_MANAGE,
  MORTUARY_READ,
  MORTUARY_WRITE,
  MORTUARY_MANAGE,
  MORTUARY_RELEASE,
  CSSD_READ,
  CSSD_WRITE,
  CSSD_MANAGE,
  CSSD_RELEASE,
  REPORTING_READ,
  REPORTING_WRITE,
  REPORTING_EXPORT,
  FINANCE_TOOLS_READ,
  FINANCE_TOOLS_WRITE,
  FINANCE_TOOLS_APPROVE,
  FINANCE_TOOLS_EXPORT,
  RIS_READ,
  RIS_WRITE,
  RIS_SIGN,
  CARE_PLAN_READ,
  CARE_PLAN_WRITE,
  EMERGENCY_RECORD_READ,
  EMERGENCY_RECORD_WRITE,
  PERIOPERATIVE_RECORD_READ,
  PERIOPERATIVE_RECORD_WRITE,
  PERIOPERATIVE_RECORD_SIGN,
  DIALYSIS_RECORD_READ,
  DIALYSIS_RECORD_WRITE,
  DIALYSIS_RECORD_PRESCRIBE,
  RECORD_GOVERNANCE_READ,
  RECORD_GOVERNANCE_WRITE,
  RECORD_GOVERNANCE_RELEASE,
] as const
export type OfflinePermission = (typeof OFFLINE_PERMISSIONS)[number]

export type Permission =
  | typeof PATIENT_READ_ALL
  | typeof PATIENT_WRITE
  | typeof OUTCOME_RECORD
  | typeof RELAY_EXECUTE_ACTION
  | typeof MEDICATION_PRESCRIBE
  | typeof HELIX_RECEIVE
  | typeof DISCHARGE_GENERATE
  | typeof DISCHARGE_PUBLISH
  | typeof VITALS_WRITE
  | typeof VITALS_TRANSCRIBE
  | typeof NURSING_NOTE_WRITE
  | typeof NURSING_NOTE_READ
  | typeof CONSENT_RECORD
  | HisPermission
  | OfflinePermission

/**
 * Which feature each HIS role needs (mirror of server/roles.py ROLE_FEATURE).
 *
 * A role listed here can be created in User Management only while its feature
 * is on, and while the feature is off its rail is empty and the shell says so.
 * 'adt' and 'lab' are opt-in modules (published under 'modules' on
 * /api/health); 'registration' is a feature flag (under 'features'). The shell
 * resolves which is which (shellConfig.isRoleFeatureOn).
 */
export type RoleFeatureId = 'registration' | 'adt' | 'lab'

export const ROLE_FEATURE: Readonly<Partial<Record<string, RoleFeatureId>>> = {
  receptionist: 'registration',
  ward_clerk: 'adt',
  lab_technician: 'lab',
  pathologist: 'lab',
  phlebotomist: 'lab',
}

/**
 * A role that may ALSO be staffed while a second switch is on (mirror of
 * server/roles.py ROLE_FEATURE_ALSO). The ward clerk can be made on an Orb
 * Ward Record computer (the 'ward_record' clinical switch on /api/health) with
 * beds and admissions off: the profile is sold around her copying the paper
 * rounds. Any one switch on is enough (shellConfig.canStaffRole).
 */
export const ROLE_FEATURE_ALSO: Readonly<Partial<Record<string, 'ward_record'>>> = {
  ward_clerk: 'ward_record',
}

/** Mirror of the server's role→permission map, restricted to the permissions
 *  above. A role absent from this table holds none of them — which is the right
 *  reading of an unknown role, because the server maps it to an empty set too
 *  (`ROLE_PERMISSIONS.get(role, set())`) and refuses everything. */
const ROLE_PERMISSIONS: Record<string, readonly Permission[]> = {
  // A nurse charts observations but does not edit the patient record, so no
  // PATIENT_WRITE. A nurse DOES hold OUTCOME_RECORD: the charge nurse is the
  // pilot's ground-truth recorder.
  // A nurse holds RELAY_READ but not RELAY_EXECUTE_ACTION: they read Sage's
  // cards and its drafted assessments, and a doctor resolves them.
  // A nurse holds `medication.write` (she gives the dose) but NOT
  // MEDICATION_PRESCRIBE: she is the last independent check on the prescriber's
  // decision, so she never sees the allergy-override control.
  // A nurse holds HELIX_RECEIVE and no other pharmacy permission: she signs for
  // what arrives on her ward and sees nothing of the pharmacy's order book.
  //
  // HIS (3 October 2026): the nurse collects specimens and acknowledges a lab
  // critical (LIS-R12), and raises ward indents, receives ward stock and
  // records returns for her own ward (GRN-R8). No bed-board grant: the nurse's
  // read-only board waits on ruling ADT-S7, and until then she gets nothing new.
  nurse: [PATIENT_READ_ALL, OUTCOME_RECORD, HELIX_RECEIVE, VITALS_WRITE,
          NURSING_NOTE_WRITE, NURSING_NOTE_READ,
          LAB_COLLECT, LAB_CRITICAL_ACK,
          WARD_STOCK_READ, WARD_STOCK_RECEIVE, WARD_INDENT_RAISE, WARD_RETURN_RECORD],
  // HIS: the doctor registers and consults outpatients (OPD-N3), reads the bed
  // board (moves and transfers go through the patient.write arm on the
  // server), and collects specimens and acknowledges lab criticals (LIS-R12).
  // The doctor also records consent (opd-19 offers it on the walk-in).
  doctor: [PATIENT_READ_ALL, PATIENT_WRITE, OUTCOME_RECORD, RELAY_EXECUTE_ACTION,
           MEDICATION_PRESCRIBE, DISCHARGE_GENERATE, DISCHARGE_PUBLISH, VITALS_WRITE,
           NURSING_NOTE_READ, CONSENT_RECORD,
           REGISTRATION_READ, REGISTRATION_WRITE, OPD_CONSULT,
           ADT_READ, LAB_COLLECT, LAB_CRITICAL_ACK],
  // Governance + read-only clinical. Holds OUTCOME_RECORD for backfill, but
  // never PATIENT_WRITE and never RELAY_EXECUTE_ACTION — an administrator may
  // read a case room, not resolve clinical reasoning inside it. And never
  // MEDICATION_PRESCRIBE: an administrator holds no clinical write at all.
  //
  // HIS: reads registrations but never registers (OPD-N3) and sets clinic
  // timings; is the bed manager (every ADT grant and bed setup); reads and
  // approves in the pharmacy store but no longer writes (GRN-R7); reads the lab
  // and manages its interfaces, never reconciles a result (LIS-R17).
  admin: [PATIENT_READ_ALL, OUTCOME_RECORD,
          REGISTRATION_READ, SCHEDULE_MANAGE,
          ADT_READ, ADT_BED, ADT_TRANSFER, ADT_PRESENCE, BED_MASTER,
          PHARMACY_STORE_READ, PHARMACY_STORE_APPROVE,
          LAB_READ, LAB_INTERFACE],
  // Pharmacy workflow + Sage only: no clinical read, no clinical write.
  // HIS: the pharmacy store, including the NDPS register (GRN-R7), and the
  // check of a medication order (MO-R4 (a): the pharmacist only).
  pharmacist: [PHARMACY_STORE_READ, PHARMACY_STORE_WRITE, PHARMACY_STORE_NDPS, MEDICATION_VERIFY],
  // Self-only access through Bridge.
  patient: [],
  // The cash counter: payments only, none of the mirrored set.
  cashier: [],
  // The HIS roles. Each exists only while its feature is on (ROLE_FEATURE).
  // The receptionist registers and books; no chart, no money (OPD-N3), and
  // no CONSENT_RECORD until counsel agrees (OPD-N3).
  receptionist: [REGISTRATION_READ, REGISTRATION_WRITE],
  // The ward clerk reads the board and moves a patient between beds of the
  // same department: no transfer (ADT-S8), no leave or absconded (ADT-S7).
  // The Orb Ward Record (6 October 2026): she also copies observation sets
  // from the paper sheet (VITALS_TRANSCRIBE), never VITALS_WRITE.
  ward_clerk: [ADT_READ, ADT_BED, VITALS_TRANSCRIBE],
  // The lab roles (LIS-R12): hospital-wide, none of the clinical set.
  phlebotomist: [LAB_READ, LAB_COLLECT],
  lab_technician: [LAB_READ, LAB_COLLECT, LAB_RECEIVE, LAB_RESULT, LAB_CRITICAL_LOG, INGEST_RECONCILE],
  pathologist: [LAB_READ, LAB_COLLECT, LAB_RECEIVE, LAB_RESULT, LAB_CRITICAL_LOG, INGEST_RECONCILE,
                LAB_AUTHORISE, LAB_MASTER],
}

ROLE_PERMISSIONS.admin = [...ROLE_PERMISSIONS.admin, RADIATION_SAFETY_READ, RADIATION_SAFETY_WRITE, INFECTION_SURVEILLANCE_READ, INFECTION_SURVEILLANCE_WRITE, INFECTION_SURVEILLANCE_MANAGE, FEEDBACK_READ, FEEDBACK_WRITE, FEEDBACK_MANAGE, MORTUARY_READ, MORTUARY_WRITE, MORTUARY_MANAGE, MORTUARY_RELEASE, CSSD_READ, CSSD_WRITE, CSSD_MANAGE, CSSD_RELEASE, REPORTING_READ, REPORTING_WRITE, REPORTING_EXPORT, FINANCE_TOOLS_READ, FINANCE_TOOLS_WRITE, FINANCE_TOOLS_APPROVE, FINANCE_TOOLS_EXPORT, RECORD_GOVERNANCE_READ, RECORD_GOVERNANCE_WRITE, RECORD_GOVERNANCE_RELEASE, RIS_READ, CARE_PLAN_READ, EMERGENCY_RECORD_READ, PERIOPERATIVE_RECORD_READ, DIALYSIS_RECORD_READ]
ROLE_PERMISSIONS.doctor = [...ROLE_PERMISSIONS.doctor, RIS_READ, RIS_WRITE, RIS_SIGN, CARE_PLAN_READ, CARE_PLAN_WRITE, EMERGENCY_RECORD_READ, EMERGENCY_RECORD_WRITE, PERIOPERATIVE_RECORD_READ, PERIOPERATIVE_RECORD_WRITE, PERIOPERATIVE_RECORD_SIGN, DIALYSIS_RECORD_READ, DIALYSIS_RECORD_WRITE, DIALYSIS_RECORD_PRESCRIBE]
ROLE_PERMISSIONS.nurse = [...ROLE_PERMISSIONS.nurse, CARE_PLAN_READ, CARE_PLAN_WRITE, EMERGENCY_RECORD_READ, EMERGENCY_RECORD_WRITE, PERIOPERATIVE_RECORD_READ, PERIOPERATIVE_RECORD_WRITE, DIALYSIS_RECORD_READ, DIALYSIS_RECORD_WRITE, RIS_READ]

// Same alias the server declares.
ROLE_PERMISSIONS.clinician = ROLE_PERMISSIONS.doctor

export function currentRole(): string {
  return (getUser()?.role || '').toLowerCase()
}

/** True when the named role holds `permission` on the server.
 *
 *  Pass the role from auth context where you have it; the default reads the
 *  stored session. An explicit `null` role (signed out, or a session that never
 *  carried one) holds nothing — it does NOT fall back to storage. */
export function hasPermission(
  permission: Permission,
  role: string | null | undefined = currentRole(),
): boolean {
  return (ROLE_PERMISSIONS[(role || '').toLowerCase()] ?? []).includes(permission)
}

/** True when this role may list ward patients, i.e. GET /api/vigil/patients.
 *
 *  A pharmacist holds SAGE_QUERY and the Helix permissions and nothing else, so
 *  the ward list 403s for them. Sage's mount effect used to call it regardless
 *  and threw two uncaught permission errors into the console on every
 *  pharmacist login. */
export function canSeeWardList(): boolean {
  return hasPermission(PATIENT_READ_ALL)
}
