/**
 * Orb, Laboratory receipt API (lis-9): scan, receive, reject, and the
 * recollect or cancel decision for a rejected sample.
 *
 * THE CHECK DIGIT. Every accession number is checked by the server before
 * anything is looked up: a misread or mistyped digit answers 422
 * `bad_accession` with a sentence to show, and never opens another tube.
 *
 * WHAT THE SERVER DECIDES. A scanned sample comes back with `actions`, the
 * steps it offers now (receive, reject, recollect, cancel the rejected
 * tests), and `stability`, which suggests rejecting a sample past its type's
 * stability time as 'delayed'. The screen draws those; it never works them
 * out itself.
 *
 * AFTER A REJECTION the answer says whether the ward was told in the
 * patient's case room (`ward_notice`), and, for a mislabelled or unlabelled
 * sample with the NABH module on, carries a prompt to report a specimen
 * error incident (`incident_prompt`).
 *
 * Recollecting starts new work, so it answers 404 `module_disabled` while
 * the laboratory module drains; cancelling the rejected tests does not.
 */
import { labGet, labPost } from './index'
import type { CollectionPatient, LabPriority } from './specimens'

export interface SpecimenStability {
  /** The specimen type's stability time, or null when none is set. */
  stability_minutes: number | null
  /** Minutes since the sample was drawn. */
  age_minutes: number | null
  exceeded: boolean
  /** 'delayed' when exceeded, else null. */
  suggested_rejection: string | null
}

export interface ReceiptTest {
  item_id: number
  order_id: string
  test_id: string
  name: string
  short_name: string
  status: string
  status_text: string
  section_id: string | null
  /** Set for a cancelled test. `at` is a bare UTC timestamp. */
  cancelled: { by_name: string | null; at: string | null; reason: string | null } | null
}

export interface SpecimenActions {
  receive: boolean
  reject: boolean
  recollect: boolean
  cancel_rejected: boolean
}

/** One sample as the Receive screen shows it. */
export interface ReceiptSpecimen {
  id: string
  status: string
  status_text: string
  priority: LabPriority
  patient: CollectionPatient
  specimen_type_code: string
  specimen_display: string
  container: string | null
  container_colour_name: string | null
  collected_at: string | null
  collected_by_name: string | null
  received_at: string | null
  received_by_name: string | null
  receipt_note: string | null
  rejected_at: string | null
  rejected_by_name: string | null
  rejection_code: string | null
  rejection_display: string | null
  rejection_note: string | null
  recollect_of: string | null
  recollected_as: string[]
  tests: ReceiptTest[]
  /** Minutes since the collection was recorded, while the sample is in transit. */
  transit_minutes: number | null
  stability: SpecimenStability
  actions: SpecimenActions
}

/** One row of the in-transit or rejected list. */
export interface ReceivingEntry {
  id: string
  patient: CollectionPatient
  specimen_display: string
  container: string | null
  container_colour_name: string | null
  priority: LabPriority
  status: string
  collected_at: string | null
  collected_by_name: string | null
  transit_minutes: number | null
  stability: SpecimenStability
  rejected_at: string | null
  rejected_by_name: string | null
  rejection_code: string | null
  rejection_display: string | null
  rejection_note: string | null
  tests: { item_id: number; order_id: string; test_id: string; status: string; name: string; short_name: string }[]
}

export interface RejectionReason {
  code: string
  display: string
  incident_prompt: boolean
}

export interface ReceivingLists {
  in_transit: ReceivingEntry[]
  rejected: ReceivingEntry[]
  rejection_reasons: RejectionReason[]
  scope: { department: string | null; hospital_wide: boolean }
}

export interface IncidentPrompt {
  incident_type: 'specimen_error'
  reason_code: string
  reason: string
  message: string
}

export interface WardNotice {
  posted: boolean
  /** 'no_case_room', 'case_room_closed' (patient discharged) or 'failed' when not posted. */
  reason: string | null
  message_id?: number | null
  channel_id?: string | null
}

export interface OrderOutcome {
  id: string
  status: string
  conflict: boolean
}

export interface ReceiveResult {
  specimen: ReceiptSpecimen
  items_received: number
  orders: OrderOutcome[]
}

export interface RejectResult {
  specimen: ReceiptSpecimen
  rejected_items: number
  withdrawn_results: number
  orders: OrderOutcome[]
  incident_prompt: IncidentPrompt | null
  ward_notice: WardNotice
}

export interface RecollectResult {
  specimen: ReceiptSpecimen
  recollect_of: string
  recollected_as: string
  items_moved: number
  orders: OrderOutcome[]
}

export interface CancelRejectedResult {
  specimen: ReceiptSpecimen
  cancelled_items: number
  orders: OrderOutcome[]
}

function specimenPath(specimenId: string, step: string): string {
  return `/specimens/${encodeURIComponent(specimenId)}/${step}`
}

export function getReceivingLists(): Promise<ReceivingLists> {
  return labGet<ReceivingLists>('/receiving')
}

/** The number as scanned or typed; spaces are fine, the server removes them. */
export function scanSpecimen(code: string): Promise<{ specimen: ReceiptSpecimen }> {
  return labGet<{ specimen: ReceiptSpecimen }>(`/specimens/scan/${encodeURIComponent(code.trim())}`)
}

export function receiveSpecimen(
  specimenId: string,
  body: { receipt_note?: string; accept_past_stability?: boolean } = {},
): Promise<ReceiveResult> {
  return labPost<ReceiveResult>(specimenPath(specimenId, 'receive'), body)
}

export function rejectSpecimen(
  specimenId: string,
  body: { reason_code: string; note?: string },
): Promise<RejectResult> {
  return labPost<RejectResult>(specimenPath(specimenId, 'reject'), body)
}

export function recollectSpecimen(specimenId: string, note?: string): Promise<RecollectResult> {
  return labPost<RecollectResult>(specimenPath(specimenId, 'recollect'), note ? { note } : {})
}

export function cancelRejectedTests(specimenId: string, reason: string): Promise<CancelRejectedResult> {
  return labPost<CancelRejectedResult>(specimenPath(specimenId, 'cancel-rejected'), { reason })
}
