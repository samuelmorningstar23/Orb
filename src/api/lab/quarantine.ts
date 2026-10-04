/**
 * Orb, unmatched results queue API (lis-19).
 *
 * Results whose patient identifier matched nobody, held for laboratory staff
 * while ORB_RESULT_QUARANTINE is on. The routes live under /api/ingest (they
 * belong to the interfaced-results lane, not to /api/lab), so this file calls
 * apiFetch itself and reuses the laboratory's typed error.
 *
 *  - GET  /ingest/quarantine                  the list and its counts
 *  - POST /ingest/quarantine/{id}/attach      laboratory staff only
 *  - POST /ingest/quarantine/{id}/discard     laboratory staff only
 *
 * A 404 `feature_disabled` means the queue is switched off at this site and
 * holds nothing; a screen says so in words rather than drawing an empty list.
 */
import { apiFetch } from '../client'
import { labQuery, toLabError } from './index'

export type QuarantineStatus = 'open' | 'attached' | 'discarded' | 'all'

/** One identifier check, as the attach answer reports it. */
export type IdentityCheck =
  | 'match'
  | 'no_match'
  | 'not_in_message'
  | 'differs_in_message'
  | 'not_given'
  /** A hospital number in the message now names a different patient's
   *  record. Refuses the attach outright. */
  | 'names_another_patient'
  /** Name and date of birth match, but another patient's record has the
   *  same name and date of birth (or no date of birth), and no hospital
   *  number in the message confirms this one. Refuses the attach. */
  | 'shared_with_another_patient'

export type IdentityKind = 'hospital_number' | 'name' | 'date_of_birth'

export interface QuarantineCounts {
  open: number
  attached: number
  discarded: number
  oldest_open_at: string | null
}

/** A row as everyone who may list the queue sees it. */
export interface QuarantineItemBase {
  id: number
  created_at: string
  lane: 'ingestion' | 'analyser'
  status: 'open' | 'attached' | 'discarded'
  source: string | null
  sender: string | null
  observation_name: string | null
  observed_at: string | null
  swept: boolean
  resolved_at: string | null
}

/** What laboratory staff (ingest.reconcile) also see. */
export interface QuarantineItemFull extends QuarantineItemBase {
  /** False when the message names the patient in fewer than two ways, so no
   *  attach can ever pass the two-identifier check. Absent reads as true. */
  identifiable?: boolean
  reason?: string
  observation_code?: string | null
  value?: string | null
  unit?: string | null
  abnormal_flag?: string | null
  reference_range?: string | null
  result_status?: string | null
  message_identifiers?: string[]
  message_name?: string | null
  message_date_of_birth?: string | null
  message_sex?: string | null
  resolved_by_name?: string | null
  resolution_reason?: string | null
  attached_patient_id?: string | null
}

export interface QuarantineList {
  enabled: boolean
  view: 'reconcile' | 'monitor'
  status: QuarantineStatus
  counts: QuarantineCounts
  items: QuarantineItemFull[]
}

export interface AttachRequest {
  hospital_number: string
  name?: string
  date_of_birth?: string
  reason: string
}

export interface AttachResult {
  attached: true
  id: number
  patient_id: string
  observation_id: number
  superseded: number[]
  /** Set when the result was stored already replaced by a corrected result
   *  the laboratory sent after it (it is on the chart, behind that one). */
  superseded_by?: number | null
  checks: Partial<Record<IdentityKind, IdentityCheck>>
}

const BASE = '/ingest/quarantine'

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  try {
    return await apiFetch<T>(path, init)
  } catch (err) {
    throw toLabError(err)
  }
}

export function listQuarantine(status: QuarantineStatus = 'open', limit = 100): Promise<QuarantineList> {
  return call<QuarantineList>(`${BASE}${labQuery({ status, limit })}`)
}

export function attachQuarantined(id: number, body: AttachRequest): Promise<AttachResult> {
  return call<AttachResult>(`${BASE}/${id}/attach`, { method: 'POST', body: JSON.stringify(body) })
}

export function discardQuarantined(id: number, reason: string): Promise<{ discarded: true; id: number }> {
  return call(`${BASE}/${id}/discard`, { method: 'POST', body: JSON.stringify({ reason }) })
}

/** Why an attach was refused: the identifiers did not confirm the patient;
 *  a hospital number in the message now names a different patient; another
 *  patient's record shares the name and date of birth; or a corrected result
 *  sent after this one is already on the patient's chart. */
export type AttachRefusal =
  | 'identity_not_confirmed'
  | 'message_names_another_patient'
  | 'name_and_birth_date_shared'
  | 'later_correction_on_chart'

const REFUSALS: readonly AttachRefusal[] = [
  'identity_not_confirmed',
  'message_names_another_patient',
  'name_and_birth_date_shared',
  'later_correction_on_chart',
]

/** The refusal on a 409 from attach, or null when the error is something else. */
export function attachRefusal(detail: unknown): AttachRefusal | null {
  if (!detail || typeof detail !== 'object') return null
  const error = (detail as { error?: unknown }).error
  return REFUSALS.includes(error as AttachRefusal) ? (error as AttachRefusal) : null
}

/** The per-identifier checks on a 409 attach refusal, or null. */
export function refusedChecks(detail: unknown): Partial<Record<IdentityKind, IdentityCheck>> | null {
  if (!attachRefusal(detail)) return null
  const checks = (detail as { checks?: unknown }).checks
  if (!checks || typeof checks !== 'object') return null
  return checks as Partial<Record<IdentityKind, IdentityCheck>>
}

/** The result's own status as the sending system gave it (HL7 OBX-11, or
 *  the word a JSON or FHIR feed sent), so a reconciler can tell a final from
 *  its correction. */
const RESULT_STATUS_WORD: Record<string, string> = {
  F: 'Final', FINAL: 'Final', U: 'Final',
  C: 'Corrected', CORRECTED: 'Corrected', AMENDED: 'Corrected',
  P: 'Preliminary', PRELIMINARY: 'Preliminary',
  R: 'Not yet verified', S: 'Partial', I: 'Pending', REGISTERED: 'Pending',
  X: 'Could not be obtained', CANCELLED: 'Cancelled',
  D: 'Deleted', W: 'Sent in error', 'ENTERED-IN-ERROR': 'Sent in error',
}

/** "Final result", "Corrected result", or the sender's own code when Orb has
 *  no word for it. Null when the message gave no status. */
export function resultStatusText(status: string | null | undefined): string | null {
  const s = (status ?? '').trim()
  if (!s) return null
  const word = RESULT_STATUS_WORD[s.toUpperCase()]
  return word ? `${word} result` : `Result status ${s}`
}
