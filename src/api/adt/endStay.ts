/**
 * Orb, Beds and admissions API: how a stay ended, and reopening one whose end
 * was recorded in error (adt-9, over the routes adt-8a and adt-8b built).
 *
 *   POST /api/adt/admissions/{id}/end      record LAMA, DAMA, absconded or death
 *                                          (any end type the server knows)
 *   POST /api/adt/admissions/{id}/reopen   reopen an end recorded in error
 *   POST /api/vigil/patients/{id}/discharge
 *                                          the doctor's discharge, with how the
 *                                          stay ended (routine, referred out,
 *                                          transferred out or DAMA)
 *
 * THE LAST ROUTE IS NOT UNDER /api/adt. The doctor's discharge has always
 * lived on the Vigil chart route, and while the module is off the chart still
 * calls it with no body through api/client.dischargePatient. With the module
 * on the chart sends how the stay ended, which only this file does, so this
 * file is the one place that sends that body and turns its refusals into
 * AdtApiError like every other Beds call.
 *
 * WHO MAY. Every one of these needs discharge.generate (doctors only) and the
 * patient inside the doctor's department; the server refuses everyone else.
 * Death, LAMA and DAMA need the MRN typed back from the wristband; the server
 * compares it without spaces or capital letters and refuses a mismatch with
 * 409 read_back_mismatch, writing nothing. mrnMatches below is the same
 * comparison, so a screen can hold its button until the typed MRN matches.
 *
 * EVERY WRITE TAKES AN IDEMPOTENCY KEY. A screen makes one key when it opens
 * and sends it on every attempt, so a reply lost on the ward network and a
 * second press record the end once.
 */
import { apiFetch, type DischargeResult } from '../client'
import { newId } from '../../lib/id'
import { adtPost, toAdtError, type AdtApiError } from './index'

/** Every end type the server records (migration 58's vocabulary). */
export type EndType =
  | 'routine' | 'referred_out' | 'transferred_out' | 'dama' | 'lama' | 'absconded' | 'death'

/** What the doctor's discharge route records: a summary is drafted for these. */
export const DISCHARGE_ROUTE_TYPES: readonly EndType[] = ['routine', 'referred_out', 'transferred_out', 'dama']

/** Recorded only through End of stay, with the MRN typed back and no summary. */
export const END_STAY_ONLY_TYPES: readonly EndType[] = ['lama', 'absconded', 'death']

/** What the End of stay screen offers, in the order it lists them. */
export const END_STAY_DRAWER_TYPES: readonly EndType[] = ['lama', 'dama', 'absconded', 'death']

/** The discharge route asks for the MRN typed back for these. */
export const READ_BACK_ON_DISCHARGE_ROUTE: readonly EndType[] = ['dama']

/** A typed reason is part of the record for these, on every route. */
export const REASON_REQUIRED_TYPES: readonly EndType[] = ['lama', 'dama', 'absconded']

/** How each end type is named on a screen. */
export const END_TYPE_WORDS: Readonly<Record<EndType, string>> = {
  routine: 'Routine discharge',
  referred_out: 'Referred out',
  transferred_out: 'Transferred out',
  dama: 'Discharged against medical advice (DAMA)',
  lama: 'Left against medical advice (LAMA)',
  absconded: 'Absconded',
  death: 'Death',
}

/** The end-of-stay ruling's default back-time window (ADT_BACKTIME_HOURS). The
 *  server's own setting, read from GET /api/adt/meta, wins. */
export const DEFAULT_BACKTIME_HOURS = 72

/** How far ahead of this computer's clock an end time may sit (the server
 *  allows the same, for a ward tablet whose clock runs a little fast). */
export const FUTURE_SLACK_MS = 5 * 60 * 1000

export function isEndType(value: unknown): value is EndType {
  return typeof value === 'string' && value in END_TYPE_WORDS
}

export function endTypeWords(value: string | null | undefined): string {
  return isEndType(value) ? END_TYPE_WORDS[value] : 'How the stay ended was not stated'
}

export function needsReason(endType: EndType | null | undefined): boolean {
  return !!endType && REASON_REQUIRED_TYPES.includes(endType)
}

/** The server's comparison: no spaces, no capital letters. */
export function mrnKey(value: string | null | undefined): string {
  return String(value ?? '').replace(/\s+/g, '').toLowerCase()
}

/** True only when something was typed and it is this patient's MRN. A patient
 *  with no MRN on file can never be matched, as on the server. */
export function mrnMatches(typed: string | null | undefined, mrn: string | null | undefined): boolean {
  const key = mrnKey(typed)
  return key !== '' && key === mrnKey(mrn)
}

/** A fresh Idempotency-Key for one screen's write. */
export function newWriteKey(): string {
  return newId()
}

// ── Times ────────────────────────────────────────────────────────────────────

/** A `datetime-local` value for a Date, in this computer's own time zone. */
export function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    + `T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** A `datetime-local` value read as local time; null when it is not one. */
export function fromLocalInput(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

/** Why an end time cannot be sent, in words, or null when it can. The server
 *  checks the same again and is the authority. */
export function endTimeProblem(
  when: Date | null,
  { now = new Date(), backtimeHours = DEFAULT_BACKTIME_HOURS, admittedAt = null }:
    { now?: Date; backtimeHours?: number; admittedAt?: Date | null } = {},
): string | null {
  if (!when) return 'Enter the date and time the stay ended.'
  if (when.getTime() > now.getTime() + FUTURE_SLACK_MS) return 'The time the stay ended is in the future.'
  if (admittedAt && when.getTime() < admittedAt.getTime()) {
    return 'The time the stay ended is before the patient was admitted.'
  }
  if (now.getTime() - when.getTime() > backtimeHours * 3600 * 1000) {
    return `An end of stay can be recorded up to ${backtimeHours} hours back. Older entries need an administrator.`
  }
  return null
}

// ── What the routes answer ───────────────────────────────────────────────────

/** An escalation chain still open on the patient (listed, never closed, by an
 *  end of stay: held ruling R009). */
export interface OpenEscalationRow {
  id: string
  status: string
  level?: string | null
  news2?: number | null
  peak_news2?: number | null
  current_tier?: number | null
  tier_label?: string | null
  raised_at: string
}

/** A care bundle the end of stay cancelled (F050). */
export interface CancelledBundle {
  id: number
  bundle_type: string
  status?: string
}

export interface EndStayDisposition {
  id: number
  /** The outcome the NABH register holds for this stay. */
  disposition: string
  /** The outcome this end of stay recorded. Differs when the records desk had
   *  already keyed another, which is then flagged, never overwritten. */
  end_of_stay_disposition: string
  source: string
  action: string
  needs_reconciliation: boolean
}

export interface EndStayRequest {
  end_type: EndType
  confirm_mrn: string
  /** UTC ISO; the server uses now when it is left out. */
  occurred_at?: string
  reason?: string
  notes?: string
}

export interface EndStayResult {
  status: 'ended'
  /** True when this key had already recorded the end: nothing new was written. */
  replayed: boolean
  admission_id: string
  patient_id: string
  end_type: string | null
  admission_status?: string
  discharged_at: string
  discharged_by?: string | null
  movement_id: string
  disposition?: EndStayDisposition | null
  open_escalations?: OpenEscalationRow[]
  bundles_cancelled?: CancelledBundle[]
  portal_login_deactivated?: boolean
  bed_freed?: string | null
  preconditions?: unknown[]
  discharge_link_issued: boolean
}

export interface ReopenRequest {
  reason: string
  confirm_mrn: string
}

export interface ReopenResult {
  status: 'reopened'
  replayed: boolean
  admission_id: string
  patient_id: string
  movement_id: string
  corrects_movement_id: string
  corrected_end_type?: string | null
  corrected_discharged_at?: string | null
  ward?: string | null
  ward_id?: string | null
  bed?: string | null
  bed_id?: string | null
  bed_retaken?: boolean
  awaiting_bed?: boolean
  bed_not_retaken?: string | null
  bed_not_retaken_words?: string | null
  bed_out_of_service?: boolean
  dispositions_voided?: number[]
  portal_login_restored?: boolean
  bundles_not_restored?: CancelledBundle[]
}

/** How the stay ended, sent with the doctor's discharge (module on only). */
export interface DischargeEndRequest {
  discharge_type: EndType
  /** Required for a DAMA. */
  confirm_mrn?: string
  /** Required for a DAMA. */
  reason?: string
}

export type DischargeEndResult = DischargeResult | { status: 'already_discharged'; patient_id: string }

// ── Calls ────────────────────────────────────────────────────────────────────

/** Record how a stay ended. Throws AdtApiError (read_back_mismatch,
 *  reason_required, beyond_backtime, admission_not_active and so on). */
export function recordEndOfStay(
  admissionId: string, request: EndStayRequest, key: string,
): Promise<EndStayResult> {
  return adtPost<EndStayResult>(`/admissions/${encodeURIComponent(admissionId)}/end`, request, key)
}

/** Reopen a stay whose end was recorded in error. Throws AdtApiError
 *  (summary_on_file, no_recorded_end, read_back_mismatch and so on). */
export function reopenStay(
  admissionId: string, request: ReopenRequest, key: string,
): Promise<ReopenResult> {
  return adtPost<ReopenResult>(`/admissions/${encodeURIComponent(admissionId)}/reopen`, request, key)
}

/** The doctor's discharge with how the stay ended. LAMA, absconded and death
 *  are refused by the server (409 use_end_of_stay) and must go through
 *  recordEndOfStay. Throws AdtApiError. */
export async function dischargeWithEndType(
  patientId: string, request: DischargeEndRequest,
): Promise<DischargeEndResult> {
  const body: DischargeEndRequest = { discharge_type: request.discharge_type }
  if (READ_BACK_ON_DISCHARGE_ROUTE.includes(request.discharge_type)) {
    body.confirm_mrn = request.confirm_mrn ?? ''
    body.reason = request.reason ?? ''
  }
  try {
    return await apiFetch<DischargeEndResult>(
      `/vigil/patients/${encodeURIComponent(patientId)}/discharge`,
      { method: 'POST', body: JSON.stringify(body) },
    )
  } catch (err) {
    throw toAdtError(err)
  }
}

// ── Words ────────────────────────────────────────────────────────────────────

const BUNDLE_LABELS: Readonly<Record<string, string>> = {
  sepsis6: 'Sepsis Six',
  deterioration: 'Deterioration',
}

/** A care bundle's name, as sepsis_bundle_service.bundle_label writes it. */
export function bundleWords(bundle: { bundle_type?: string | null; bundle_label?: string | null }): string {
  if (bundle.bundle_label && bundle.bundle_label.trim()) return bundle.bundle_label.trim()
  const key = String(bundle.bundle_type ?? '').trim().toLowerCase()
  if (BUNDLE_LABELS[key]) return BUNDLE_LABELS[key]
  const words = key.replace(/_/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : 'Care bundle'
}

/** What to say when an end-of-stay write is refused. The server writes the
 *  words for every refusal it makes; these cover the replies that carry none. */
export function endStayFailureWords(err: unknown, action: 'record' | 'reopen' = 'record'): string {
  const failure: AdtApiError = toAdtError(err)
  const what = action === 'reopen' ? 'reopened' : 'recorded'
  if (failure.isUnreachable) {
    return `Could not reach Orb, so it is not known whether this was ${what}. Try again: `
      + 'the same request is never recorded twice.'
  }
  if (failure.isModuleDisabled) {
    return `Beds and admissions is switched off at this site, so nothing was ${what} here.`
  }
  if (failure.isForbidden) {
    return `Only a doctor can ${action === 'reopen' ? 'reopen a stay' : 'record how a stay ended'}. Nothing was ${what}.`
  }
  if (failure.isNotFound) {
    return `This stay is not one you can see, or it is no longer on file. Nothing was ${what}.`
  }
  // The server's words for this one name its header; say what happened instead.
  if (failure.code === 'idempotency_key_reused') {
    return `This screen's request was already used for a different stay, so nothing was ${what}. `
      + 'Close this screen and open it again for this patient.'
  }
  const detail = failure.detail as { message?: unknown } | null
  if (detail && typeof detail === 'object' && typeof detail.message === 'string' && detail.message.trim()) {
    return detail.message.trim()
  }
  return failure.message || `The server did not say why. Nothing was ${what}.`
}

/** What to say when the doctor's discharge (dischargeWithEndType) is refused.
 *  That route takes no Idempotency-Key, so a reply lost on the network is not
 *  called safe to repeat: the chart is reloaded to see what happened first.
 *  (The server refuses a second discharge of a patient already discharged.) */
export function dischargeFailureWords(err: unknown): string {
  const failure: AdtApiError = toAdtError(err)
  if (failure.isUnreachable) {
    return 'Could not reach Orb, so it is not known whether the discharge was recorded. '
      + 'Reload the chart to see before you try again.'
  }
  if (failure.isForbidden) return 'Only a doctor can discharge a patient. Nothing was recorded.'
  if (failure.isNotFound) {
    return 'This patient is not one you can see, or is no longer on file. Nothing was recorded.'
  }
  const detail = failure.detail as { message?: unknown } | null
  const words = detail && typeof detail === 'object' && typeof detail.message === 'string' && detail.message.trim()
    ? detail.message.trim()
    : (failure.message || 'The server did not say why. Nothing was recorded.')
  return `The discharge did not complete. ${words}`
}
