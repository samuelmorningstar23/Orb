/**
 * Outpatients (OPD) API: the doctor's queue, the queue actions, the OPD record
 * view and the consultation note.
 *
 * Every call here exists only while the server's 'opd' feature is on (ORB_OPD,
 * which also needs registration and the front desk). With it off the server
 * answers 404 {error: 'feature_disabled'}; `isFeatureOff(err)` recognises that
 * so a screen can say so instead of showing an error.
 *
 * THE CLINIC IS NEVER A PARAMETER. The server derives it from the signed-in
 * account (permissions.department_scope); a client that could name a clinic
 * could name someone else's.
 *
 * OPD VITALS ARE PLAIN VALUES. What a consulting doctor types is kept in the
 * note with its time and is never scored, banded or coloured (rulings OPD-S3
 * and OPD-S6). Nothing in this file carries a score.
 */
import { apiFetch } from './client'
import type { OpdVisit } from './frontdesk'

// ── Shared ───────────────────────────────────────────────────────────────────

export interface CodeLabel {
  code: string
  label: string
}

/** A visit as OPD mode returns it: the front desk's visit plus the queue
 *  fields (migration 50 columns) the 'opd' feature exposes. */
export interface OpdQueueVisit extends OpdVisit {
  priority: string
  priority_label: string
  priority_reason: string | null
  room: string | null
  call_count: number
  skipped: boolean
  skipped_at: string | null
  cancel_reason_code: string | null
  cancel_reason_label: string | null
  disposition: string | null
  disposition_at: string | null
  /** The token on the patient's slip when a doctor's take gave them a new one
   *  (that number in the doctor's line belongs to someone else); null when
   *  the token never changed. */
  previous_token_number?: number | null
  /** Only on the take's answer: true when THIS take issued the new token. */
  token_changed?: boolean
  /** 'draft' | 'signed' | null (no note yet). Only on the doctor's queue. */
  consultation_status?: 'draft' | 'signed' | null
}

/** True when the visit's token is not the one on the patient's slip. */
export function tokenWasReissued(visit: Pick<OpdQueueVisit, 'token_number' | 'previous_token_number'>): boolean {
  return visit.previous_token_number != null && visit.previous_token_number !== visit.token_number
}

/** True when the server said the 'opd' feature is switched off. */
export function isFeatureOff(err: unknown): boolean {
  const e = err as { status?: number; detail?: { error?: string } } | null
  return !!e && e.status === 404 && e.detail?.error === 'feature_disabled'
}

/** The structured error code of a refusal (detail.error), if any. */
export function errorCode(err: unknown): string | null {
  const e = err as { detail?: { error?: unknown } } | null
  return typeof e?.detail?.error === 'string' ? e.detail.error : null
}

const visitPath = (visitId: string) => `/frontdesk/opd/visits/${encodeURIComponent(visitId)}`
const opdVisitPath = (visitId: string) => `/opd/visits/${encodeURIComponent(visitId)}`

// ── Settings ─────────────────────────────────────────────────────────────────

export interface OpdMeta {
  feature: 'opd'
  enabled: boolean
  settings: { scope_days: number; charge_at: string; queue_poll_seconds: number }
  dispositions: CodeLabel[]
}

export function getOpdMeta(): Promise<OpdMeta> {
  return apiFetch<OpdMeta>('/opd/meta')
}

// ── The doctor's queue ───────────────────────────────────────────────────────

export interface MyQueue {
  visit_date: string
  /** The caller's clinic as the SERVER derived it, or null (no department). */
  department: string | null
  doctor_id: string
  /** The patient in the room (normally one). */
  current: OpdQueueVisit[]
  /** Waiting, in seeing order: priority, then token. */
  waiting: OpdQueueVisit[]
  /** Called but did not come; back in the line when recalled. */
  skipped: OpdQueueVisit[]
  /** The clinic's patients no doctor has taken yet. */
  unassigned: OpdQueueVisit[]
  completed_count: number
  total: number
  priorities: CodeLabel[]
  poll_seconds: number
}

export function fetchMyQueue(): Promise<MyQueue> {
  return apiFetch<MyQueue>('/opd/my-queue')
}

/** Call the patient in (the doctor's name for the desk's "start"). */
export function callOpdVisit(visitId: string): Promise<OpdQueueVisit> {
  return apiFetch<OpdQueueVisit>(`${visitPath(visitId)}/call`, { method: 'POST' })
}

/** The patient did not come when called. */
export function skipOpdVisit(visitId: string): Promise<OpdQueueVisit> {
  return apiFetch<OpdQueueVisit>(`${visitPath(visitId)}/skip`, { method: 'POST' })
}

/** A skipped patient is back in the line at their own token. */
export function recallOpdVisit(visitId: string): Promise<OpdQueueVisit> {
  return apiFetch<OpdQueueVisit>(`${visitPath(visitId)}/recall`, { method: 'POST' })
}

/** The signed-in doctor takes an unassigned patient of their clinic. */
export function takeOpdVisit(visitId: string): Promise<OpdQueueVisit> {
  return apiFetch<OpdQueueVisit>(`${visitPath(visitId)}/assign`, { method: 'POST' })
}

export interface QueueFieldsInput {
  priority?: string
  priorityReason?: string | null
  room?: string | null
}

/** Set a visit's queue priority (a reason for anything but normal) and room.
 *  Only the fields given are sent, so only they change. */
export function updateOpdVisitQueue(visitId: string, input: QueueFieldsInput): Promise<OpdQueueVisit> {
  const body: Record<string, unknown> = {}
  if (input.priority !== undefined) body.priority = input.priority
  if (input.priorityReason !== undefined) body.priority_reason = input.priorityReason
  if (input.room !== undefined) body.room = input.room
  return apiFetch<OpdQueueVisit>(visitPath(visitId), {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

/** Cancel with a reason code. 'left_without_being_seen' is LWBS; a cancel
 *  never raises a charge. */
export function cancelOpdVisitWithCode(
  visitId: string,
  reasonCode: string,
  reason: string,
): Promise<OpdQueueVisit> {
  return apiFetch<OpdQueueVisit>(`${visitPath(visitId)}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason, reason_code: reasonCode }),
  })
}

// ── The OPD record view (opd-7) ──────────────────────────────────────────────

export interface OpdRecord {
  patient: {
    id: string
    name: string
    uhid: string | null
    age: number | null
    age_recorded_on: string | null
    sex: string | null
    preferred_language: string | null
    record_status: string | null
    inpatient_now: boolean
    date_of_birth_on_file: boolean
  }
  access: { arm?: string; visit_id?: string; visit_date?: string; scope_until?: string }
  allergies: { substance: string; reaction: string | null; severity: string | null }[]
  /** 'none_recorded' is NOT "no known allergies": nobody may have asked. */
  allergy_status: 'recorded' | 'none_recorded'
  problems: { problem: string; icd10_code: string | null; onset_date: string | null }[]
  opd_visits: {
    id: string
    department: string
    doctor_name: string | null
    visit_date: string
    visit_type: string
    status: string
    chief_complaint: string | null
    own_clinic: boolean
  }[]
  results: {
    id: string
    order_type: string
    description: string
    status: string
    result_text: string | null
    result_at: string | null
    created_at: string
  }[]
  consultations: {
    id: string
    visit_id: string
    department: string
    visit_date: string | null
    diagnoses: string[]
    plan: string | null
    advice: string | null
    follow_up_date: string | null
    signed_by_name: string | null
    signed_at: string | null
  }[]
  consultations_available: boolean
}

export function fetchOpdRecord(patientId: string): Promise<OpdRecord> {
  return apiFetch<OpdRecord>(`/opd/patients/${encodeURIComponent(patientId)}/record`)
}

// ── The consultation note (opd-10) ───────────────────────────────────────────

export interface OpdDiagnosis {
  text: string
  code_system: 'ICD10' | 'SNOMED' | null
  code: string | null
  /** 'validated' (found in the code list), 'unvalidated' (kept as typed) or 'none'. */
  code_status: 'validated' | 'unvalidated' | 'none'
  add_to_problems: boolean
}

export interface OpdVitals {
  values: Partial<Record<
    'heart_rate' | 'systolic_bp' | 'diastolic_bp' | 'respiratory_rate' | 'spo2'
    | 'temperature' | 'weight_kg' | 'height_cm', number>>
  measured_at: string
  measured_by: string
  measured_by_name: string | null
}

export interface OpdConsultation {
  id: string
  visit_id: string
  patient_id: string
  department: string
  author_id: string
  author_name: string | null
  status: 'draft' | 'signed'
  chief_complaint: string | null
  history: string | null
  examination: string | null
  vitals: OpdVitals | null
  diagnoses: OpdDiagnosis[]
  plan: string | null
  advice: string | null
  follow_up_date: string | null
  follow_up_note: string | null
  referral_department: string | null
  admission_advised: boolean
  row_version: number
  created_at: string
  updated_at: string
  signed_by: string | null
  signed_by_name: string | null
  signed_at: string | null
  addenda?: { id: number; author_id: string; author_name: string | null; body: string; created_at: string }[]
}

export interface ConsultationView {
  visit_id: string
  patient_id: string
  visit_status: string
  consultation: OpdConsultation | null
  allergy_review: { status: 'recorded' | 'not_reviewed'; can_sign: boolean; charted_count: number }
  patient_admitted: boolean
}

export function fetchConsultation(visitId: string): Promise<ConsultationView> {
  return apiFetch<ConsultationView>(`${opdVisitPath(visitId)}/consultation`)
}

export type ConsultationInput = Partial<{
  chief_complaint: string | null
  history: string | null
  examination: string | null
  vitals: { values: OpdVitals['values']; measured_at: string } | null
  diagnoses: { text: string; code?: string; code_system?: string; add_to_problems?: boolean }[]
  plan: string | null
  advice: string | null
  follow_up_date: string | null
  follow_up_note: string | null
  referral_department: string | null
  admission_advised: boolean
}>

/** Save the draft. `rowVersion` is the version the editor read (null for a
 *  new note); a stale one is refused 409 'stale_version'. */
export function saveConsultation(
  visitId: string,
  rowVersion: number | null,
  fields: ConsultationInput,
): Promise<OpdConsultation> {
  return apiFetch<OpdConsultation>(`${opdVisitPath(visitId)}/consultation`, {
    method: 'PUT',
    body: JSON.stringify({ row_version: rowVersion, ...fields }),
  })
}

/** Sign the draft. Refused 409 'allergy_review_required' until the patient's
 *  allergies are recorded (ruling OPD-S7). */
export function signConsultation(visitId: string, rowVersion: number): Promise<OpdConsultation> {
  return apiFetch<OpdConsultation>(`${opdVisitPath(visitId)}/consultation/sign`, {
    method: 'POST',
    body: JSON.stringify({ row_version: rowVersion }),
  })
}

export function addConsultationAddendum(visitId: string, body: string): Promise<OpdConsultation> {
  return apiFetch<OpdConsultation>(`${opdVisitPath(visitId)}/consultation/addenda`, {
    method: 'POST',
    body: JSON.stringify({ body }),
  })
}

/** Finish the visit. Without a signed note a reason is required (ruling
 *  OPD-N8), for example "note written on paper". */
export function completeOpdVisitWithDisposition(
  visitId: string,
  disposition: string,
  noSignedNoteReason?: string,
): Promise<OpdQueueVisit> {
  return apiFetch<OpdQueueVisit>(`${opdVisitPath(visitId)}/complete`, {
    method: 'POST',
    body: JSON.stringify({
      disposition,
      no_signed_note_reason: noSignedNoteReason || undefined,
    }),
  })
}

// ── The code lists (opd-11) ──────────────────────────────────────────────────

export type TerminologyKind = 'diagnosis' | 'investigation'

/** One code from the server's code list. An investigation also says what it
 *  orders: a laboratory test or imaging, nothing else. */
export interface TerminologyHit {
  code_system: string
  code: string
  display: string
  order_type?: 'lab' | 'imaging'
}

export interface TerminologySearch {
  kind: TerminologyKind
  results: TerminologyHit[]
  truncated: boolean
  min_chars: number
}

/** Search the diagnosis or investigation code list. No patient data. */
export function searchTerminology(query: string, kind: TerminologyKind): Promise<TerminologySearch> {
  const params = new URLSearchParams({ q: query, kind })
  return apiFetch<TerminologySearch>(`/opd/terminology/search?${params.toString()}`)
}

// ── Investigations (opd-11) ──────────────────────────────────────────────────

export type OrderPriority = 'routine' | 'urgent' | 'stat'

/** An investigation ordered on an outpatient visit. Its words come from the
 *  code list; the server never takes them from this client. */
export interface OpdOrder {
  id: string
  opd_visit_id: string | null
  order_type: 'lab' | 'imaging' | string
  code_system: string | null
  code: string | null
  description: string
  priority: OrderPriority | string
  status: 'active' | 'in_progress' | 'completed' | 'resulted' | 'cancelled' | 'draft' | string
  result_text: string | null
  result_at: string | null
  ordered_by_name: string | null
  created_at: string | null
}

export function fetchVisitOrders(visitId: string): Promise<{ visit_id: string; orders: OpdOrder[] }> {
  return apiFetch<{ visit_id: string; orders: OpdOrder[] }>(`${opdVisitPath(visitId)}/orders`)
}

/** Order a laboratory test or imaging on the visit, active at once. Only the
 *  code and the priority are sent. */
export function orderInvestigation(
  visitId: string,
  hit: Pick<TerminologyHit, 'code_system' | 'code'>,
  priority: OrderPriority = 'routine',
): Promise<OpdOrder> {
  return apiFetch<OpdOrder>(`${opdVisitPath(visitId)}/orders`, {
    method: 'POST',
    body: JSON.stringify({ code_system: hit.code_system, code: hit.code, priority }),
  })
}

export function cancelInvestigation(visitId: string, orderId: string, reason: string): Promise<OpdOrder> {
  return apiFetch<OpdOrder>(
    `${opdVisitPath(visitId)}/orders/${encodeURIComponent(orderId)}/cancel`,
    { method: 'POST', body: JSON.stringify({ reason }) },
  )
}

// ── Prints (opd-11) ──────────────────────────────────────────────────────────

export type VisitPrintKind = 'consultation' | 'investigations'

/** What a print carries, read as it is printed (the server audits it). The
 *  doctor is named with their registration number and nothing else. */
export interface VisitPrint {
  kind: VisitPrintKind
  printed_at: string
  visit: { id: string; visit_date: string | null; department: string | null; token_number: number | null }
  patient: { name: string | null; uhid: string | null; age: number | null; sex: string | null }
  consultation: {
    id: string
    chief_complaint: string | null
    history: string | null
    examination: string | null
    vitals: { values: OpdVitals['values']; measured_at: string | null } | null
    diagnoses: { text: string; code_system: string | null; code: string | null }[]
    plan: string | null
    advice: string | null
    follow_up_date: string | null
    follow_up_note: string | null
    referral_department: string | null
    signed_at: string | null
    addenda: { body: string; created_at: string | null; author_name: string | null }[]
  } | null
  investigations: {
    description: string
    order_type: string
    priority: string
    status: string
    created_at: string | null
    ordered_by_name: string | null
  }[]
  doctors: { name: string | null; registration_number: string | null }[]
}

export function fetchVisitPrint(visitId: string, kind: VisitPrintKind): Promise<VisitPrint> {
  return apiFetch<VisitPrint>(`${opdVisitPath(visitId)}/print?kind=${kind}`)
}

// ── The live nudge ───────────────────────────────────────────────────────────

/** The socket event the server pushes after any queue change in a clinic.
 *  Its payload is { department } and nothing else: refetch, never read it. */
export const QUEUE_CHANGED_EVENT = 'opd:queue_changed'
