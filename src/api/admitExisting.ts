/**
 * Admitting a registered outpatient from their clinic visit (opd-17a).
 *
 *   GET  /api/admissions/admit-existing/visits/{visit_id}  what the form shows
 *   POST /api/admissions/admit-existing                    the admission
 *
 * Both exist only while the server's 'opd' feature is on (404
 * feature_disabled otherwise) and answer only for a visit of the caller's own
 * clinic. The patient keeps their record and hospital number: the form sends
 * no identity field, only what the patient STATES so the server can check it
 * (ruling OPD-S1), and no observations (ruling OPD-S3: the ward charts a fresh
 * set). Outpatient observations and medicines come back for reference only and
 * are never written to the inpatient chart (rulings OPD-S3 and OPD-S4). Only
 * a visit the patient can be admitted from shows anything about the patient;
 * any other answers with the visit and the refusal alone.
 */
import { apiFetch } from './client'
import type { AdmissionResult } from './client'
import type { OpdVitals } from './opd'

export interface ConversionRefusal {
  error: string
  message: string
}

/** The visit itself, as the clinic's queue lists it: nothing about the patient. */
export interface ConversionVisitReference {
  id: string
  visit_date: string
  department: string
  doctor_name: string | null
  token_number: number
  status: string
}

/** A visit the patient cannot be admitted from now (finished, cancelled, not
 *  today's, admitted anywhere, an earlier inpatient): the visit and why, and
 *  nothing about the patient, because the clinic may no longer be inside the
 *  patient's outpatient scope. */
export interface RefusedConversionPreview {
  visit: ConversionVisitReference
  admittable: false
  refusal: ConversionRefusal
}

/** What the form shows, by whether the patient can be admitted from this visit. */
export type ConversionPreview = AdmittableConversionPreview | RefusedConversionPreview

/** A visit the patient can be admitted from now, with what the form shows. */
export interface AdmittableConversionPreview {
  visit: ConversionVisitReference
  /** Who the patient is, as the record holds it. Never the date of birth. */
  patient: {
    id: string
    name: string
    uhid: string | null
    age: number | null
    sex: string | null
    portal_login: boolean
    date_of_birth_on_file: boolean
  }
  admittable: true
  refusal: null
  note: { status: string; signed: boolean; signed_at: string | null } | null
  /** Where the form starts: the consultation's complaint and diagnoses. */
  carried: { chief_complaint: string | null; initial_diagnosis: string | null }
  /** This visit's values with their time, for reference only. */
  opd_vitals: { measured_at: string | null; measured_by_name: string | null; values: OpdVitals['values'] }[]
  /** False on an Orb Ward Record computer, whose drug chart stays on paper. */
  medicines_listed: boolean
  opd_medicines: {
    drug_name: string
    dose: string | null
    route: string | null
    frequency: string | null
    is_prn: boolean
    status: string
    prescribed_at: string | null
  }[]
  allergies: { substance: string; reaction: string | null; severity: string | null }[]
  allergy_status: 'recorded' | 'none_recorded'
}

/** What the patient states (ruling OPD-S1). */
export interface StatedIdentity {
  name: string
  date_of_birth?: string
  age_years?: number
  phone_last4?: string
  uhid?: string
}

export type ConversionResult = AdmissionResult & {
  replayed?: boolean
  portal_login_created?: boolean
  opd_visit_id?: string
  case_room_reused?: boolean
  location_warning?: string | null
}

export async function fetchConversionPreview(visitId: string): Promise<ConversionPreview> {
  return apiFetch<ConversionPreview>(
    `/admissions/admit-existing/visits/${encodeURIComponent(visitId)}`)
}

export async function admitExistingPatient(
  body: Record<string, unknown>,
  idempotencyKey: string,
): Promise<ConversionResult> {
  return apiFetch<ConversionResult>('/admissions/admit-existing', {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify(body),
  })
}

/** What the form collects from the patient, in the identity check's two choices. */
export interface StatedIdentityInput {
  name: string
  ageMode: 'dob' | 'age'
  dob: string
  age: string
  secondMode: 'phone' | 'uhid'
  last4: string
  uhid: string
}

export const EMPTY_STATED_IDENTITY: StatedIdentityInput = {
  name: '', ageMode: 'dob', dob: '', age: '', secondMode: 'phone', last4: '', uhid: '',
}

/** The fields the patient stated, as the server takes them, or null while
 *  they are not enough: a name, a date of birth or an age, and the last 4
 *  digits of their phone or their UHID. Never filled in from the record. */
export function statedIdentityPayload(input: StatedIdentityInput): StatedIdentity | null {
  const name = input.name.trim()
  if (!name) return null
  const out: StatedIdentity = { name }
  if (input.ageMode === 'dob') {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(input.dob.trim())) return null
    out.date_of_birth = input.dob.trim()
  } else {
    const age = input.age.trim() === '' ? NaN : Number(input.age)
    if (!Number.isInteger(age) || age < 0 || age > 130) return null
    out.age_years = age
  }
  if (input.secondMode === 'phone') {
    if (!/^\d{4}$/.test(input.last4.trim())) return null
    out.phone_last4 = input.last4.trim()
  } else {
    if (!input.uhid.trim()) return null
    out.uhid = input.uhid.trim()
  }
  return out
}
