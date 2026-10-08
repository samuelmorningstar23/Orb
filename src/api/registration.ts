/**
 * Orb, patient registration API: the /api/registration routes (opd-4, opd-5)
 * and the front desk's visit registration with the identity check (opd-6a).
 *
 * WHAT THE DESK MAY SEE. The index search is hospital-wide and minimal: UHID,
 * name, sex, age, the last 4 digits of the phone, the last visit date and
 * whether the patient is an inpatient now (ruling OPD-N4). Never a date of
 * birth, a full phone number or an address.
 *
 * WHO THE PATIENT IS (ruling OPD-S1). Before a visit is attached to a record
 * found by search, the desk types what the patient STATES (name, date of birth
 * or age, and the last 4 digits of their phone or their UHID). The server
 * compares and answers 'match' or 'no_match' and nothing else; the stored date
 * of birth never reaches this client. With the registration feature on, the
 * server refuses every visit that does not carry these identifiers, so
 * `registerVisitWithIdentity` always sends them, and a patient registered a
 * moment ago is sent with what the desk just typed and the UHID the
 * registration returned.
 *
 * NO ALLERGIES AT THE DESK (ruling OPD-S5). Nothing here takes an allergy or
 * "no known allergies": the server refuses any unknown field, and a clinician
 * records allergies at the consultation.
 *
 * Behind the 'registration' feature: every /api/registration call answers 404
 * with detail.error 'feature_disabled' while it is off (isFeatureDisabled).
 * Uses the shared apiFetch, so src/api/client.ts is untouched.
 */
import { apiFetch } from './client'
import type { OpdVisit, OpdVisitType } from './frontdesk'

// ── Settings ─────────────────────────────────────────────────────────────────

export interface RegistrationConfig {
  /** 'hospital': the desk types the hospital's own number. 'orb': Orb issues it. */
  uhid_source: 'hospital' | 'orb'
  uhid_pattern_set: boolean
  uhid_pattern_valid: boolean
  uhid_series_configured: boolean
  /** False when no patient can be registered until an administrator acts. */
  uhid_ready: boolean
  /** How a possible duplicate is handled (ruling OPD-S2). */
  duplicate_mode: 'warn' | 'block'
  /** ABHA can be recorded only while the ABDM module is on. */
  abdm_enabled: boolean
  abha_notice: string
  search_min_chars: number
  search_max_results: number
  reason_min_chars: number
  thresholds: { probable: number; possible: number }
  id_document_types: string[]
  allergies_at_desk: false
}

export function fetchRegistrationConfig(): Promise<RegistrationConfig> {
  return apiFetch<RegistrationConfig>('/registration/config')
}

// ── The patient index ────────────────────────────────────────────────────────

/** The only fields a hospital-wide reader sees about a patient. */
export interface IndexCard {
  patient_id: string
  uhid: string | null
  name: string
  sex: string | null
  age: number | null
  /** True when the age is one the patient stated, not one from a date of birth. */
  age_stated: boolean
  phone_last4: string | null
  last_visit_date: string | null
  inpatient_now: boolean
}

export interface IndexSearchResult {
  results: IndexCard[]
  truncated: boolean
  kinds: string[]
  min_chars: number
  max_results: number
}

/** Find patients by UHID, phone or name (at least 3 characters). Audited. */
export function searchPatientIndex(query: string): Promise<IndexSearchResult> {
  return apiFetch<IndexSearchResult>(`/registration/search?q=${encodeURIComponent(query)}`)
}

// ── Who the patient is ───────────────────────────────────────────────────────

/** What the patient says. A date of birth OR an age; the phone's last 4
 *  digits OR the UHID. */
export interface StatedIdentity {
  name: string
  date_of_birth?: string
  age_years?: number
  phone_last4?: string
  uhid?: string
}

/** The server's whole answer: never a stored value. */
export interface IdentityVerdict {
  result: 'match' | 'no_match'
  checked: string[]
  checked_at: string
}

function statedBody(stated: StatedIdentity): Record<string, unknown> {
  const body: Record<string, unknown> = { name: stated.name.trim() }
  if (stated.date_of_birth) body.date_of_birth = stated.date_of_birth
  if (stated.age_years !== undefined && stated.age_years !== null) body.age_years = stated.age_years
  if (stated.phone_last4) body.phone_last4 = stated.phone_last4.trim()
  if (stated.uhid) body.uhid = stated.uhid.trim()
  return body
}

/** Compare what the patient states with the record. Rate-limited per user
 *  and patient (429 'identity_check_locked'); every answer is audited. */
export function checkPatientIdentity(
  patientId: string,
  stated: StatedIdentity,
  purpose = 'identity_confirm',
): Promise<IdentityVerdict> {
  return apiFetch<IdentityVerdict>('/registration/identity-check', {
    method: 'POST',
    body: JSON.stringify({ patient_id: patientId, purpose, ...statedBody(stated) }),
  })
}

// ── Registering a new patient ────────────────────────────────────────────────

export interface DuplicateCandidate extends IndexCard {
  score: number
  band: 'hard' | 'probable' | 'possible'
  reasons: string[]
  reason_labels: string[]
}

export interface DuplicateReview {
  decision: 'register_new'
  candidate_ids: string[]
  reason: string
}

export interface NewPatientInput {
  name: string
  sex: string
  /** A date of birth or a stated age, never both. */
  date_of_birth?: string
  age_years?: number
  phone?: string
  /** Hospital mode only; Orb issues the number in Orb mode. */
  uhid?: string
  abha_number?: string
  abha_address?: string
  duplicate_review?: DuplicateReview
}

export interface RegisteredPatient {
  /** The full record on a first registration; on a replay of the same request
   *  only the id and the UHID (record_returned false). */
  patient: { patient_id: string; uhid: string | null; record_returned?: boolean
    date_of_birth_hint?: string | null; discharge_link_possible?: boolean }
  replayed: boolean
  duplicate_review: { reason: string; candidate_ids: string[]; check_incomplete: boolean } | null
}

export function registerNewPatient(input: NewPatientInput, idempotencyKey: string): Promise<RegisteredPatient> {
  const body: Record<string, unknown> = { name: input.name.trim(), sex: input.sex }
  if (input.date_of_birth) body.date_of_birth = input.date_of_birth
  if (input.age_years !== undefined) body.age_years = input.age_years
  if (input.phone) body.phone = input.phone.trim()
  if (input.uhid) body.uhid = input.uhid.trim()
  if (input.abha_number) body.abha_number = input.abha_number.trim()
  if (input.abha_address) body.abha_address = input.abha_address.trim()
  if (input.duplicate_review) body.duplicate_review = input.duplicate_review
  return apiFetch<RegisteredPatient>('/registration/patients', {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify(body),
  })
}

// ── The visit, with the identity check ───────────────────────────────────────

export interface VisitWithIdentityInput {
  patientId: string
  /** Omit to register into the caller's own clinic (the normal case). */
  department?: string
  doctorId?: string
  visitType?: OpdVisitType
  chiefComplaint?: string
  identity: StatedIdentity
  idempotencyKey: string
}

export type RegisteredVisit = OpdVisit & {
  identity_check?: IdentityVerdict
}

/** POST /api/frontdesk/opd/visits with what the patient stated. */
export function registerVisitWithIdentity(input: VisitWithIdentityInput): Promise<RegisteredVisit> {
  return apiFetch<RegisteredVisit>('/frontdesk/opd/visits', {
    method: 'POST',
    body: JSON.stringify({
      patient_id: input.patientId,
      department: input.department,
      doctor_id: input.doctorId,
      visit_type: input.visitType ?? 'new',
      chief_complaint: input.chiefComplaint,
      idempotency_key: input.idempotencyKey,
      identity_check: statedBody(input.identity),
    }),
  })
}

// ── Errors ───────────────────────────────────────────────────────────────────

/** The structured detail of a refusal, when the server sent one. */
export function errorDetail(err: unknown): Record<string, unknown> | null {
  const e = err as { detail?: unknown } | null
  return e && typeof e.detail === 'object' && e.detail !== null && !Array.isArray(e.detail)
    ? (e.detail as Record<string, unknown>)
    : null
}

/** detail.error of a refusal, if any. */
export function errorCode(err: unknown): string | null {
  const code = errorDetail(err)?.error
  return typeof code === 'string' ? code : null
}

export function errorStatus(err: unknown): number | null {
  const status = (err as { status?: unknown } | null)?.status
  return typeof status === 'number' ? status : null
}

/** The registration feature went off while the screen was open. */
export function isFeatureDisabled(err: unknown): boolean {
  return errorStatus(err) === 404 && errorCode(err) === 'feature_disabled'
}

/** The records a duplicate refusal listed (minimal fields, no date of birth). */
export function duplicateCandidates(err: unknown): DuplicateCandidate[] {
  const list = errorDetail(err)?.candidates
  return Array.isArray(list) ? (list as DuplicateCandidate[]) : []
}

/** A fresh key for one request, kept by the caller so a session-refresh
 *  retry of the same request is recognised as a replay. */
export function newRequestKey(prefix: string): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`
  return `${prefix}-${random}`
}
