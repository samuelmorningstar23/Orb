/**
 * Orb, Beds and admissions API: bed reservations (adt-12).
 *
 *   GET  /api/adt/reservations?ward_id=           beds held now
 *   POST /api/adt/reservations                    hold a free bed
 *   POST /api/adt/reservations/{id}/release       let it go, with a reason
 *
 * A bed is held for a patient on the way: an admission, a transfer, a
 * patient coming back from surgery, a planned admission, or other. It stops
 * holding the bed at its expiry (the site's setting, ruling ADT-P9: 2 hours,
 * 24 hours for planned admissions and after surgery). Only the patient it is
 * held for can be placed in it; anybody else is refused, and the refusal
 * never says who the bed is held for. A transfer, and a patient coming back
 * from surgery, name the admission.
 *
 * WHO MAY. The ward clerk and the administrator (adt.bed) and the doctors
 * (patient.write); a nurse is refused. The patient a bed is held for must be
 * inside the caller's department. The board and the list name that patient
 * only inside the caller's department.
 *
 * Reserving takes an Idempotency-Key: a screen makes one when it opens and
 * sends it on every attempt.
 */
import { formatClinicalDateTime, formatClinicalTime, parseUTC } from '../../lib/datetime'
import { ADT_BED, PATIENT_WRITE, hasPermission } from '../../lib/roles'
import { adtGet, adtPost, toAdtError, type AdtApiError } from './index'

export type ReservationPurpose = 'admission' | 'transfer' | 'post_op' | 'elective' | 'other'

/** In the order the screen offers them. */
export const RESERVATION_PURPOSES: readonly ReservationPurpose[] =
  ['admission', 'transfer', 'post_op', 'elective', 'other']

/** How each purpose is named when choosing one. */
export const PURPOSE_CHOICE_WORDS: Readonly<Record<ReservationPurpose, string>> = {
  admission: 'An admission',
  transfer: 'A transfer from another ward',
  post_op: 'A patient coming back from surgery',
  elective: 'A planned admission',
  other: 'Other',
}

/** These name the admission they are for: the patient is admitted already. */
export const NEEDS_ADMISSION: readonly ReservationPurpose[] = ['transfer', 'post_op']

/** The patient a reservation is held for, only inside the caller's scope. */
export interface ReservedFor {
  patient_id: string
  admission_id: string | null
  name: string | null
  mrn: string | null
}

/** What a bed board tile carries about the reservation holding it. */
export interface TileReservation {
  id: string
  purpose: ReservationPurpose | string
  purpose_words: string
  reserved_at: string | null
  expires_at: string | null
  patient: ReservedFor | null
  /** The bed is held for a patient the caller may not see. */
  patient_hidden: boolean
}

export interface Reservation {
  id: string
  bed_id: string
  ward_id: string
  bed_label: string | null
  unit_code: string | null
  ward_code: string | null
  ward_department: string | null
  patient_id: string | null
  admission_id: string | null
  purpose: ReservationPurpose | string
  purpose_words: string
  note: string | null
  status: 'active' | 'fulfilled' | 'released' | 'expired' | string
  reserved_by: string | null
  reserved_by_name: string | null
  reserved_at: string
  expires_at: string
  closed_at: string | null
  closed_by: string | null
  close_reason: string | null
  movement_id: string | null
  patient?: ReservedFor | null
  patient_hidden?: boolean
}

export interface ReserveRequest {
  bed_id: string
  purpose: ReservationPurpose
  admission_id?: string
  patient_id?: string
  note?: string
  expires_in_minutes?: number
}

export interface ReserveResult {
  replayed: boolean
  reservation: Reservation
}

export interface ReservationList {
  ward_id: string | null
  reservations: Reservation[]
  purposes: Record<string, string>
}

/** True when this account may reserve and release beds (the server decides;
 *  this only hides what would be refused). */
export function canReserve(): boolean {
  return hasPermission(ADT_BED) || hasPermission(PATIENT_WRITE)
}

/** The reservation a bed board tile carries, or null. */
export function reservationOf(tile: unknown): TileReservation | null {
  if (!tile || typeof tile !== 'object') return null
  const res = (tile as { reservation?: unknown }).reservation
  if (!res || typeof res !== 'object') return null
  const r = res as Partial<TileReservation>
  return typeof r.id === 'string' ? (r as TileReservation) : null
}

/** 'expires 03:40 PM' (today) or 'expires 10 Oct 2026, 03:40 PM', local time. */
export function expiryWords(expiresAt: string | null | undefined, now: Date = new Date()): string {
  if (!expiresAt) return ''
  const when = parseUTC(expiresAt)
  if (Number.isNaN(when.getTime())) return ''
  const sameDay = when.getFullYear() === now.getFullYear() && when.getMonth() === now.getMonth()
    && when.getDate() === now.getDate()
  return `expires ${sameDay ? formatClinicalTime(expiresAt) : formatClinicalDateTime(expiresAt)}`
}

// ── Calls ────────────────────────────────────────────────────────────────────

export function listReservations(wardId?: string | null): Promise<ReservationList> {
  return adtGet<ReservationList>('/reservations', { ward_id: wardId || undefined })
}

/** Hold a bed. Throws AdtApiError (bed_reserved, bed_occupied,
 *  admission_required, patient_already_reserved, bad_expiry and so on). */
export function reserveBed(request: ReserveRequest, key: string): Promise<ReserveResult> {
  const body: ReserveRequest = { bed_id: request.bed_id, purpose: request.purpose }
  if (request.admission_id) body.admission_id = request.admission_id
  if (request.patient_id) body.patient_id = request.patient_id
  if (request.note?.trim()) body.note = request.note.trim()
  if (request.expires_in_minutes) body.expires_in_minutes = request.expires_in_minutes
  return adtPost<ReserveResult>('/reservations', body, key)
}

/** Let a held bed go. Throws AdtApiError (reason_required,
 *  reservation_not_active). */
export function releaseReservation(reservationId: string, reason: string): Promise<{ reservation: Reservation }> {
  return adtPost<{ reservation: Reservation }>(
    `/reservations/${encodeURIComponent(reservationId)}/release`, { reason: reason.trim() })
}

export interface PlaceHeldOptions {
  /** Why the patient moves: required when they already have a bed. */
  reason?: string
  /** ADT-S2 (c): why the bed is used before it is recorded clean. */
  notCleanReason?: string
  /** The boarder acknowledgement the server asked for. */
  boarderAcknowledged?: boolean
}

interface HeldLocation {
  bed_id: string | null
  bed: string | null
  awaiting_bed: boolean
}

/** Place the patient a bed is held for in it, which fulfils the reservation
 *  on the server in the same transaction. An admission awaiting a bed is
 *  given the bed (POST /admissions/{id}/bed); one already in a bed is moved
 *  (POST /admissions/{id}/bed-change, with the bed it lies in now as
 *  expected_bed_id and the reason). The treating department never changes
 *  here: a transfer to another team is the Move screen's. Throws AdtApiError
 *  with the server's code (bed_not_clean_reason_required,
 *  boarder_acknowledgement_required, state_changed and so on). */
export async function placeHeldPatient(
  admissionId: string, bedId: string, options: PlaceHeldOptions, key: string,
): Promise<{ movement?: { id?: string } }> {
  const id = encodeURIComponent(admissionId)
  const where = await adtGet<HeldLocation>(`/admissions/${id}/location`)
  const body: Record<string, unknown> = { bed_id: bedId }
  if (options.notCleanReason?.trim()) body.not_clean_reason = options.notCleanReason.trim()
  if (options.boarderAcknowledged) body.boarder_acknowledged = true
  if (where.awaiting_bed) {
    return adtPost(`/admissions/${id}/bed`, body, key)
  }
  return adtPost(`/admissions/${id}/bed-change`, {
    ...body, expected_bed_id: where.bed_id ?? null,
    reason: options.reason?.trim() || 'Moved to the bed reserved for the patient',
  }, key)
}

/** What to say when a reservation write is refused. The server writes the
 *  words for every refusal it makes; these cover the replies that carry none. */
export function reservationFailureWords(err: unknown, action: 'reserve' | 'release' = 'reserve'): string {
  const failure: AdtApiError = toAdtError(err)
  const what = action === 'release' ? 'released' : 'reserved'
  if (failure.isUnreachable) {
    return `Could not reach Orb, so it is not known whether the bed was ${what}. Try again.`
  }
  if (failure.isModuleDisabled) {
    return `Beds and admissions is switched off at this site, so nothing was ${what} here.`
  }
  if (failure.isForbidden) {
    return `Your role cannot reserve or release beds. Nothing was ${what}.`
  }
  if (failure.isNotFound) {
    return `That patient or reservation is not one you can see. Nothing was ${what}.`
  }
  if (failure.code === 'idempotency_key_reused') {
    return 'This screen\'s request was already used for a different bed, so nothing was '
      + 'reserved. Close this screen and open it again.'
  }
  const detail = failure.detail as { message?: unknown } | null
  if (detail && typeof detail === 'object' && typeof detail.message === 'string' && detail.message.trim()) {
    return detail.message.trim()
  }
  return failure.message || `The server did not say why. Nothing was ${what}.`
}
