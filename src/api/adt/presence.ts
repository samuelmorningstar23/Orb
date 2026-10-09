/**
 * Orb, Beds and admissions API: leave and absconded (adt-10).
 *
 *   POST /api/adt/admissions/{id}/leave       the patient goes on leave
 *   POST /api/adt/admissions/{id}/leave-end   back on the ward from leave
 *   POST /api/adt/admissions/{id}/absconded   gone without leave, or not back from leave
 *   POST /api/adt/admissions/{id}/returned    an absconded patient is back
 *
 * THE BED IS HELD. None of these moves the patient out of their bed: the bed
 * board says 'Occupied, patient on leave' or 'Occupied, patient absconded'.
 *
 * WHO MAY (ruling ADT-S7 held, strictest option): doctors (patient.write) and
 * the administrator (adt.presence). A nurse and a ward clerk are refused
 * (403). The patient must be inside the caller's department (404 otherwise).
 *
 * DISPLAY ONLY (adt-10). The Vigil board and the nurse board show 'Not on
 * ward since HH:MM' from presence_status and presence_since, which the server
 * adds to their rows only while ORB_ADT is on. Nothing else changes: obs and
 * medication due times, escalation and alerts are untouched until adt-11.
 *
 * Every write takes an Idempotency-Key: a screen makes one when it opens and
 * sends it on every attempt, so a reply lost on the ward network records the
 * change once.
 */
import { formatClinicalDateTime, formatClinicalTime, parseUTC } from '../../lib/datetime'
import { ADT_PRESENCE, PATIENT_WRITE, hasPermission } from '../../lib/roles'
import { adtPost, toAdtError, type AdtApiError } from './index'

/** NULL on the server means on the ward. */
export type PresenceStatus = 'on_leave' | 'absconded'

/** The four changes, by the route each one posts to. */
export type PresenceChange = 'leave' | 'leave-end' | 'absconded' | 'returned'

/** What a row says about whether the patient is on the ward. */
export interface Presence {
  status: PresenceStatus
  /** When the patient was last on the ward (UTC). */
  since: string | null
}

export const PRESENCE_WORDS: Readonly<Record<PresenceStatus, string>> = {
  on_leave: 'On leave',
  absconded: 'Absconded',
}

/** Which changes may start from each presence (null is on the ward). */
export function changesFrom(status: PresenceStatus | null): PresenceChange[] {
  if (status === 'on_leave') return ['leave-end', 'absconded']
  if (status === 'absconded') return ['returned']
  return ['leave', 'absconded']
}

export const CHANGE_WORDS: Readonly<Record<PresenceChange, string>> = {
  leave: 'Start leave',
  'leave-end': 'Back from leave',
  absconded: 'Absconded',
  returned: 'Back on the ward',
}

/** A typed reason is part of the record for these. */
export function needsReason(change: PresenceChange): boolean {
  return change === 'leave' || change === 'absconded'
}

/** True when this account may record leave and absconding (the server
 *  decides; this only hides what would be refused). */
export function canRecordPresence(): boolean {
  return hasPermission(ADT_PRESENCE) || hasPermission(PATIENT_WRITE)
}

function isStatus(value: unknown): value is PresenceStatus {
  return value === 'on_leave' || value === 'absconded'
}

/** The presence a board row carries, or null when the patient is on the ward
 *  or the row says nothing (the module is off). Reads presence_status and
 *  presence_since from any row: a Vigil row, a nurse card, a bed occupant. */
export function presenceOf(row: unknown): Presence | null {
  if (!row || typeof row !== 'object') return null
  const r = row as { presence_status?: unknown; presence_since?: unknown }
  if (!isStatus(r.presence_status)) return null
  const since = typeof r.presence_since === 'string' && r.presence_since.trim()
    ? r.presence_since : null
  return { status: r.presence_status, since }
}

function sameLocalDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate()
}

/** When the patient was last on the ward: the time today, else the date and
 *  time. Local time through src/lib/datetime. */
export function sinceWords(since: string | null, now: Date = new Date()): string {
  if (!since) return ''
  const when = parseUTC(since)
  if (Number.isNaN(when.getTime())) return ''
  return sameLocalDay(when, now) ? formatClinicalTime(since) : formatClinicalDateTime(since)
}

/** 'Not on ward since 02:05 PM (on leave)', or null when the patient is on
 *  the ward or the row says nothing. */
export function notOnWardWords(row: unknown, now: Date = new Date()): string | null {
  const presence = presenceOf(row)
  if (!presence) return null
  const kind = PRESENCE_WORDS[presence.status].toLowerCase()
  const since = sinceWords(presence.since, now)
  return since ? `Not on ward since ${since} (${kind})` : `Not on ward (${kind})`
}

// ── Calls ────────────────────────────────────────────────────────────────────

export interface PresenceRequest {
  reason?: string
  /** UTC ISO; the server uses now when it is left out. */
  occurred_at?: string
  notes?: string
  /** A leave only: when the patient is expected back (UTC ISO). */
  expected_return_at?: string
}

export interface PresenceResult {
  replayed: boolean
  admission_id: string
  patient_id: string | null
  presence_status: PresenceStatus | null
  presence_words: string
  presence_since: string | null
  movement: { id: string; movement_type: string; moved_at: string | null }
  bed_held?: { ward_id: string | null; bed_id: string | null; ward: string | null; bed: string | null }
}

/** Record one change. Throws AdtApiError (already_on_leave, not_on_leave,
 *  patient_absconded, reason_required, beyond_backtime and so on). */
export function recordPresence(
  admissionId: string, change: PresenceChange, request: PresenceRequest, key: string,
): Promise<PresenceResult> {
  const body: PresenceRequest = {}
  if (request.reason?.trim()) body.reason = request.reason.trim()
  if (request.occurred_at) body.occurred_at = request.occurred_at
  if (request.notes?.trim()) body.notes = request.notes.trim()
  if (change === 'leave' && request.expected_return_at) {
    body.expected_return_at = request.expected_return_at
  }
  return adtPost<PresenceResult>(
    `/admissions/${encodeURIComponent(admissionId)}/${change}`, body, key)
}

/** What to say when a change is refused. The server writes the words for
 *  every refusal it makes; these cover the replies that carry none. */
export function presenceFailureWords(err: unknown): string {
  const failure: AdtApiError = toAdtError(err)
  if (failure.isUnreachable) {
    return 'Could not reach Orb, so it is not known whether this was recorded. Try again: '
      + 'the same request is never recorded twice.'
  }
  if (failure.isModuleDisabled) {
    return 'Beds and admissions is switched off at this site, so nothing was recorded here.'
  }
  if (failure.isForbidden) {
    return 'Only a doctor or the administrator can record leave and absconding. Nothing was recorded.'
  }
  if (failure.isNotFound) {
    return 'This patient is not one you can see, or is no longer on file. Nothing was recorded.'
  }
  if (failure.code === 'idempotency_key_reused') {
    return 'This screen\'s request was already used for a different change, so nothing was '
      + 'recorded. Close this screen and open it again.'
  }
  const detail = failure.detail as { message?: unknown } | null
  if (detail && typeof detail === 'object' && typeof detail.message === 'string' && detail.message.trim()) {
    return detail.message.trim()
  }
  return failure.message || 'The server did not say why. Nothing was recorded.'
}
