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

export type Permission =
  | typeof PATIENT_READ_ALL
  | typeof PATIENT_WRITE
  | typeof OUTCOME_RECORD
  | typeof RELAY_EXECUTE_ACTION
  | typeof MEDICATION_PRESCRIBE
  | typeof HELIX_RECEIVE
  | typeof DISCHARGE_GENERATE
  | typeof DISCHARGE_PUBLISH

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
  nurse: [PATIENT_READ_ALL, OUTCOME_RECORD, HELIX_RECEIVE],
  doctor: [PATIENT_READ_ALL, PATIENT_WRITE, OUTCOME_RECORD, RELAY_EXECUTE_ACTION,
           MEDICATION_PRESCRIBE, DISCHARGE_GENERATE, DISCHARGE_PUBLISH],
  // Governance + read-only clinical. Holds OUTCOME_RECORD for backfill, but
  // never PATIENT_WRITE and never RELAY_EXECUTE_ACTION — an administrator may
  // read a case room, not resolve clinical reasoning inside it. And never
  // MEDICATION_PRESCRIBE: an administrator holds no clinical write at all.
  admin: [PATIENT_READ_ALL, OUTCOME_RECORD],
  // Pharmacy workflow + Sage only: no clinical read, no clinical write.
  pharmacist: [],
  // Self-only access through Bridge.
  patient: [],
}

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
