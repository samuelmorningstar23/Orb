/**
 * Orb, Beds and admissions API: the ward clerk's lanes (adt-17b, HIS wave 9).
 *
 *   GET  /api/admissions/clerk-options              the clerk's admission form lists
 *   POST /api/admissions/clerk-admit                a registration-level admission
 *   POST /api/admissions/{id}/assessment            a doctor's admission assessment
 *   POST /api/adt/admissions/{id}/left-ward         'left the ward' (Orb Ward Record only)
 *   POST /api/adt/ordered-transfers                 a transfer a doctor ordered
 *
 * RULINGS BUILT AS DEFAULTS (Samuel to confirm; Beds and admissions stays off
 * at every site until he does):
 *   ADT-S5 (a)  The clerk admits at registration level. Every clinical field
 *               is stored as 'Not yet assessed'; no allergy and never 'no known
 *               allergies' is recorded, so the interlocks treat the patient as
 *               unknown. The bed board flags ASSESSMENT_DUE_MARKER until a
 *               doctor records the admission assessment.
 *   ADT-S3      'Left the ward' is the clerk's only on an Orb Ward Record
 *               computer: the MRN typed back, no end type guessed, no discharge
 *               summary and no patient link.
 *   ADT-S8 (b)  The clerk changes the treating department only to carry out a
 *               transfer a doctor ordered, naming that doctor.
 *
 * The admission calls are under /api/admissions, not /api/adt, so they go
 * through apiFetch directly; every refusal is still an AdtApiError with the
 * server's code.
 */
import { apiFetch } from '../client'
import { adtPost, toAdtError, type AdtApiError } from './index'
import type { AdmissionMaster } from './moves'

/** The bed board's marker for a clerk's admission still waiting for its
 *  admission assessment. The server writes the same words
 *  (services/adt/clerk.ASSESSMENT_DUE_MARKER). */
export const ASSESSMENT_DUE_MARKER = 'Admission assessment due'

/** What every clinical field of a clerk's admission reads. */
export const NOT_YET_ASSESSED = 'Not yet assessed'

/** The role that holds the clerk's lanes (server CLERK_ROLES). */
export const CLERK_ROLE = 'ward_clerk'

export function isClerkRole(role: string | null | undefined): boolean {
  return (role ?? '').toLowerCase() === CLERK_ROLE
}

/** True when the bed board says this tile's patient still needs the
 *  doctor's admission assessment. */
export function assessmentDue(markers: readonly string[] | null | undefined): boolean {
  return (markers ?? []).includes(ASSESSMENT_DUE_MARKER)
}

async function admissionsRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  try {
    return await apiFetch<T>(`/admissions${path}`, init)
  } catch (err) {
    throw toAdtError(err)
  }
}

// ── The clerk's admission ────────────────────────────────────────────────────

export interface ClerkAdmissionOptions {
  /** The clerk's own department, the only one they admit to; null when it has
   *  no clinical cover. */
  department: string | null
  departments: string[]
  default_department: string | null
  /** Active doctors of that department. */
  doctors: { id: string; name: string; department: string | null }[]
  master: AdmissionMaster & { strict_locations?: boolean }
  clinical_fields: string
  /** True when the site's registration issues the hospital number itself:
   *  the form asks for none (a typed one is refused). */
  hospital_number_issued?: boolean
}

export function getClerkAdmissionOptions(): Promise<ClerkAdmissionOptions> {
  return admissionsRequest<ClerkAdmissionOptions>('/clerk-options')
}

// ── The doctor's admission assessment ────────────────────────────────────────

export type AllergyStatus = 'allergies_recorded' | 'none_known' | 'unable_to_obtain'

export const ALLERGY_STATUS_WORDS: Readonly<Record<AllergyStatus, string>> = {
  allergies_recorded: 'Allergies recorded',
  none_known: 'No known allergies',
  unable_to_obtain: 'Could not be obtained',
}

export type CodeStatus = 'full_code' | 'dnr' | 'dni' | 'comfort'

export const CODE_STATUS_WORDS: Readonly<Record<CodeStatus, string>> = {
  full_code: 'Full code',
  dnr: 'DNR (do not resuscitate)',
  dni: 'DNI (do not intubate)',
  comfort: 'Comfort care only',
}

export interface AdmissionAssessmentRequest {
  allergy_status: AllergyStatus
  allergies?: string[]
  reason?: string
  chief_complaint: string
  initial_diagnosis?: string
  code_status: CodeStatus
  note?: string
}

export interface AdmissionAssessmentResult {
  admission_id: string
  patient_id: string
  assessment: { id: number; status: AllergyStatus; status_label: string; assessed_at: string }
  charted: string[]
  reactivated: string[]
  assessment_due: boolean
}

export function recordAdmissionAssessment(
  admissionId: string,
  request: AdmissionAssessmentRequest,
): Promise<AdmissionAssessmentResult> {
  return admissionsRequest<AdmissionAssessmentResult>(
    `/${encodeURIComponent(admissionId)}/assessment`,
    { method: 'POST', body: JSON.stringify(request) },
  )
}

// ── 'Left the ward' ──────────────────────────────────────────────────────────

export interface LeftWardRequest {
  confirm_mrn: string
  /** UTC ISO; the server uses now when it is left out. */
  occurred_at?: string
  notes?: string
}

export interface LeftWardResult {
  status: 'ended'
  replayed: boolean
  left_ward: boolean
  admission_id: string
  patient_id: string
  end_type: string | null
  discharged_at: string
  movement_id: string
  bed_freed?: string | null
  discharge_link_issued: boolean
}

export function recordLeftWard(
  admissionId: string,
  request: LeftWardRequest,
  key: string,
): Promise<LeftWardResult> {
  return adtPost<LeftWardResult>(
    `/admissions/${encodeURIComponent(admissionId)}/left-ward`, request, key)
}

// ── A transfer a doctor ordered ──────────────────────────────────────────────

export interface OrderedTransferRequest {
  admission_id: string
  to_department: string
  ordered_by: string
  reason: string
  /** The bed the screen showed (null for none). Required. */
  expected_bed_id: string | null
  /** The patient stays in the bed they lie in. */
  keep_location?: boolean
  /** The patient moves off the bed and waits for one in the new department. */
  awaiting_bed?: boolean
  boarder_acknowledged?: boolean
}

export interface OrderedTransferResult {
  replayed: boolean
  admission_id: string
  current_department?: string | null
  current_ward?: string | null
  current_bed?: string | null
  ordered_by?: { id: string; name: string | null }
}

export function orderedTransfer(
  request: OrderedTransferRequest,
  key: string,
): Promise<OrderedTransferResult> {
  return adtPost<OrderedTransferResult>('/ordered-transfers', request, key)
}

// ── Words for a refusal ──────────────────────────────────────────────────────

/** What to say when one of the clerk's writes, or the assessment, is refused.
 *  The server writes the words for every refusal it makes; these cover the
 *  replies that carry none. */
export function clerkFailureWords(err: unknown): string {
  const failure: AdtApiError = toAdtError(err)
  if (failure.isUnreachable) {
    return 'Could not reach Orb, so it is not known whether this was recorded. Try again: '
      + 'the same request is never recorded twice.'
  }
  if (failure.isModuleDisabled) {
    return 'Beds and admissions is switched off at this site, so nothing was recorded.'
  }
  if (failure.isNotFound) {
    return 'This patient is not one you can see, or is no longer on file. Nothing was recorded.'
  }
  if (failure.code === 'idempotency_key_reused') {
    return 'This screen’s request was already used for something else, so nothing was '
      + 'recorded. Close this screen and open it again.'
  }
  const detail = failure.detail as { message?: unknown } | null
  if (detail && typeof detail === 'object' && typeof detail.message === 'string'
      && detail.message.trim()) {
    return detail.message.trim()
  }
  if (failure.isForbidden) return 'Your role cannot record this. Nothing was recorded.'
  return failure.message || 'The server did not say why. Nothing was recorded.'
}
