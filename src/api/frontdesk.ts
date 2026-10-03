/**
 * Orb Hospital OS — Front Desk API
 *
 * Two workflows, one desk:
 *   * OPD  — register an outpatient, watch the token queue, call a patient in,
 *            complete the consultation (which raises the charge line).
 *   * TRANSFER — move an admitted patient between wards, with a from/to, a
 *            timestamp and a reason.
 *
 * MONEY IS NEVER FORMATTED HERE. The server returns integer paise plus a
 * pre-rendered display string; the panel renders through `src/lib/money.ts`,
 * which mirrors `server/services/money.py` exactly. A client that formats money
 * its own way draws a screen that disagrees with the bill it just created.
 *
 * "NOT CONFIGURED" IS A VALUE. `ratePaise` is `null` — not 0 — when nobody at
 * the site has priced the consultation item. Anything rendering it must go
 * through `formatInrOrUnset`, never `formatInr(x ?? 0)`.
 *
 * Routed through the shared `apiFetch` wrapper so it reuses the centralized auth
 * headers, CSRF token and 401/403 handling.
 */
import { apiFetch } from './client'

// ── OPD ──────────────────────────────────────────────────────────────────────

export type OpdVisitStatus = 'waiting' | 'in_consultation' | 'completed' | 'cancelled'
export type OpdVisitType = 'new' | 'follow_up' | 'review' | 'procedure'

/** The consultation charge raised when a visit completes.
 *  `rateIsConfigured === false` means NOBODY HAS PRICED THIS — not ₹0. */
export interface ConsultationCharge {
  charge_id: number | null
  service_code: string
  description: string
  /** Integer paise, or null when unpriced. Never a float, never 0-for-unknown. */
  rate_paise: number | null
  rate_display: string
  rate_is_configured: boolean
  total_paise: number
  total_display: string
  gst_treatment: 'exempt' | 'taxable' | 'nil_rated' | 'zero_rated'
  rate_source: string
}

export interface OpdVisit {
  id: string
  patient_id: string
  patient_name: string | null
  patient_mrn: string | null
  patient_age: number | null
  patient_sex: string | null
  department: string
  doctor_id: string | null
  doctor_name: string | null
  visit_date: string
  token_number: number
  visit_type: OpdVisitType
  chief_complaint: string | null
  status: OpdVisitStatus
  registered_at: string
  called_at: string | null
  completed_at: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  consultation_charge_id: number | null
  notes: string | null
  /** Minutes waited. NULL means "could not be measured" — render it as unknown,
   *  never as 0, or a patient who has waited an hour reads as just arrived. */
  waiting_minutes: number | null
  consultation_minutes: number | null
  consultation_charge?: ConsultationCharge | null
}

export interface OpdDoctorQueue {
  department: string
  doctor_id: string | null
  doctor_name: string
  visits: OpdVisit[]
  waiting_count: number
  in_consultation_count: number
  /** NULL when nothing is waiting. "No queue" and "no wait" are different facts. */
  longest_wait_minutes: number | null
}

export interface OpdQueueResponse {
  visit_date: string
  /** The caller's own department, as the SERVER derived it. Never sent up. */
  department: string | null
  queues: OpdDoctorQueue[]
  total: number
}

/**
 * Today's OPD queues.
 *
 * THE DEPARTMENT IS NOT A PARAMETER, deliberately. The server derives the ward
 * scope from the authenticated user (`permissions.department_scope`); a client
 * that could name a department could name someone else's.
 */
export function fetchOpdQueue(params: {
  date?: string
  doctorId?: string
  includeCompleted?: boolean
} = {}): Promise<OpdQueueResponse> {
  const qs = new URLSearchParams()
  if (params.date) qs.set('date', params.date)
  if (params.doctorId) qs.set('doctor_id', params.doctorId)
  if (params.includeCompleted) qs.set('include_completed', 'true')
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return apiFetch<OpdQueueResponse>(`/frontdesk/opd/queue${suffix}`)
}

/** A bookable consulting doctor. NOT derived from today's queue — see
 *  `frontdesk_service.list_consulting_doctors` for why that mattered. */
export interface ConsultingDoctor {
  id: string
  name: string
  department: string | null
}

/**
 * The doctors this desk may book an OPD visit with.
 *
 * The roster comes from the staff table, so it is populated on an empty
 * morning. Deriving it from `/opd/queue` (which the panel did) made the first
 * booking of the day impossible: no visits → no doctors → no way to book.
 *
 * Like every front-desk read, the department is the server's answer about the
 * caller, not a parameter.
 */
export function fetchConsultingDoctors(): Promise<{ doctors: ConsultingDoctor[] }> {
  return apiFetch<{ doctors: ConsultingDoctor[] }>('/frontdesk/opd/doctors')
}

export interface RegisterVisitInput {
  patientId: string
  /** Omit to register into the caller's own department (the normal case). */
  department?: string
  doctorId?: string
  visitType?: OpdVisitType
  chiefComplaint?: string
  notes?: string
  idempotencyKey?: string
}

export function registerOpdVisit(input: RegisterVisitInput): Promise<OpdVisit> {
  return apiFetch<OpdVisit>('/frontdesk/opd/visits', {
    method: 'POST',
    body: JSON.stringify({
      patient_id: input.patientId,
      department: input.department,
      doctor_id: input.doctorId,
      visit_type: input.visitType ?? 'new',
      chief_complaint: input.chiefComplaint,
      notes: input.notes,
      idempotency_key: input.idempotencyKey,
    }),
  })
}

export function startOpdConsultation(visitId: string): Promise<OpdVisit> {
  return apiFetch<OpdVisit>(`/frontdesk/opd/visits/${encodeURIComponent(visitId)}/start`, {
    method: 'POST',
  })
}

export function completeOpdVisit(
  visitId: string,
  options: { serviceCode?: string; notes?: string } = {},
): Promise<OpdVisit> {
  return apiFetch<OpdVisit>(`/frontdesk/opd/visits/${encodeURIComponent(visitId)}/complete`, {
    method: 'POST',
    body: JSON.stringify({
      service_code: options.serviceCode,
      notes: options.notes,
    }),
  })
}

export function cancelOpdVisit(visitId: string, reason: string): Promise<OpdVisit> {
  return apiFetch<OpdVisit>(`/frontdesk/opd/visits/${encodeURIComponent(visitId)}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

// ── Transfers ────────────────────────────────────────────────────────────────

export interface TransferDestination {
  department: string
  /** Why this department is offered: it has clinical staff. A department with no
   *  staff is not in this list at all, because transferring into it would leave
   *  the patient admitted on a ward that appears on nobody's board. */
  staff_count: number
  wards: { ward: string; occupied: number }[]
}

export interface PatientMovement {
  id: string
  patient_id: string
  admission_id: string | null
  movement_type: 'transfer' | 'admission' | 'discharge'
  from_department: string | null
  from_ward: string | null
  from_bed: string | null
  to_department: string | null
  to_ward: string | null
  to_bed: string | null
  reason: string
  moved_at: string
  moved_by: string | null
  moved_by_name: string | null
  notes: string | null
}

export interface TransferResult {
  movement: PatientMovement
  patient_id: string
  patient_name: string | null
  admission_id: string | null
  /** 'active' — proof in the response that the admission SURVIVED the move.
   *  A transfer that ends an admission would remove the patient from every
   *  clinical board while they are lying in a bed. */
  admission_status: string | null
  patient_status: string | null
  current_department: string | null
  current_ward: string | null
  current_bed: string | null
}

export function fetchTransferDestinations(): Promise<{ destinations: TransferDestination[] }> {
  return apiFetch<{ destinations: TransferDestination[] }>('/frontdesk/transfers/destinations')
}

export interface TransferInput {
  patientId: string
  toDepartment: string
  toWard?: string
  toBed?: string
  reason: string
  notes?: string
  idempotencyKey?: string
}

/**
 * Move an admitted patient to another ward.
 *
 * AFTER THIS RESOLVES THE CALLER MAY NO LONGER BE ABLE TO READ THE PATIENT.
 * Ward scope is derived live from the admission row, so a transfer out of the
 * caller's own department hands the chart over immediately. That is the intended
 * behaviour ("strict handover") and the panel says so before you confirm.
 */
export function transferPatient(input: TransferInput): Promise<TransferResult> {
  return apiFetch<TransferResult>('/frontdesk/transfers', {
    method: 'POST',
    body: JSON.stringify({
      patient_id: input.patientId,
      to_department: input.toDepartment,
      to_ward: input.toWard,
      to_bed: input.toBed,
      reason: input.reason,
      notes: input.notes,
      idempotency_key: input.idempotencyKey,
    }),
  })
}

export function fetchPatientMovements(
  patientId: string,
): Promise<{ patient_id: string; movements: PatientMovement[] }> {
  return apiFetch<{ patient_id: string; movements: PatientMovement[] }>(
    `/frontdesk/patients/${encodeURIComponent(patientId)}/movements`,
  )
}

// ── Display helpers ──────────────────────────────────────────────────────────

/**
 * A waiting time, rendered honestly.
 *
 * `null` is NOT zero. A wait the server could not measure must read as unknown;
 * printing "0 min" tells a clerk the patient just arrived when they may have
 * been sitting there since eight in the morning.
 */
export function formatWait(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || !Number.isFinite(minutes)) {
    return 'Unknown'
  }
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours}h ${rest}m` : `${hours}h`
}

/** Waiting-time bands used for the queue's status colour. Advisory only — an OPD
 *  wait is a service-quality signal, never a clinical one. */
export type WaitBand = 'unknown' | 'ok' | 'warn' | 'long'

export function waitBand(minutes: number | null | undefined): WaitBand {
  if (minutes === null || minutes === undefined || !Number.isFinite(minutes)) return 'unknown'
  if (minutes >= 60) return 'long'
  if (minutes >= 30) return 'warn'
  return 'ok'
}
