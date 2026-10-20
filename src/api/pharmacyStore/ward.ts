/**
 * Orb, Pharmacy store API: ward stock, ward indents and ward receipt (grn-21).
 *
 * Every route here sits behind the pharmacy store (ORB_PHARMACY_STORE) AND
 * ward stock (ORB_PHARMACY_WARD_STOCK), both off by default; ward stock stays
 * off until Samuel confirms ruling GRN-RS5. A ward account (ward_stock.read)
 * sees only its own department's store; the pharmacy desk sees every ward
 * store. Every call goes through the shared base in ./index, so a refusal
 * arrives as a PharmacyStoreApiError carrying the server's code, for example
 * `not_on_imprest_list`, `high_alert_barred`, `ndps_not_ward_stock`,
 * `over_par`, `already_requested`, `same_person`, `transfer_open`,
 * `witness_required`, `witness_sent_it`, `witness_pin_wrong` or
 * `audit_unavailable`.
 *
 * The server owns every rule (which drugs a ward may hold, who decides, who
 * receives). Quantities are whole dispense units. Nothing here carries
 * patient data.
 */
import { pharmacyStoreGet, pharmacyStorePost } from './index'
import type {
  ReceiptLine, Transfer, TransferResult, TransferWitness, WitnessInput,
} from './transfers'

export type WardIndentStatus =
  | 'draft'
  | 'submitted'
  | 'approved'
  | 'partially_approved'
  | 'rejected'
  | 'converted'
  | 'closed'
  | 'cancelled'

/** One indent status, or 'open' (not yet received, refused or cancelled). */
export type WardIndentFilter = WardIndentStatus | 'open' | ''

export const WARD_INDENT_STATUS_LABELS: Record<WardIndentStatus, string> = {
  draft: 'Draft',
  submitted: 'Waiting for the pharmacy',
  approved: 'Approved',
  partially_approved: 'Approved in part',
  rejected: 'Refused',
  converted: 'Sent to the ward',
  closed: 'Received',
  cancelled: 'Cancelled',
}

export type WardIndentPriority = 'routine' | 'urgent' | 'emergency'

export const WARD_INDENT_PRIORITY_LABELS: Record<WardIndentPriority, string> = {
  routine: 'Routine',
  urgent: 'Urgent',
  emergency: 'Emergency',
}

/** Why a drug cannot be ward stock, in the server's words. */
export interface WardStockProblem {
  code: string
  message: string
}

export interface WardStockLot {
  lot_id: string
  batch_no: string | null
  /** A calendar key (YYYY-MM-DD). */
  expiry_date: string | null
  units: number
  status: string
  /** The status in words: 'available', 'expired', 'quarantined' and so on. */
  status_label: string
}

export interface WardStockDrug {
  drug_id: string
  drug_name: string
  form: string | null
  strength: string | null
  dispense_unit: string | null
  /** On this store's stock list (an active par level above zero). */
  listed: boolean
  par_units: number | null
  max_units: number | null
  /** Available and in date on the hospital's date. */
  on_hand_units: number
  /** Quarantined, recalled or otherwise held off use. */
  held_units: number
  expired_units: number
  in_transit_units: number
  /** Asked for on open indents and not yet sent. */
  requested_units: number
  earliest_expiry: string | null
  below_par: boolean
  /** What a top-up would ask for now: par less on hand, on its way and asked for. */
  top_up_units: number
  /**
   * Units given from ward stock on the eMAR when the books held none (grn-17):
   * the books are below zero by this much and the ward should count the drug.
   * Absent from an older server.
   */
  below_zero_units?: number
  /** Set when the drug may not be ward stock (barred, unlisted, NDPS ...). */
  problem: WardStockProblem | null
  lots: WardStockLot[]
}

export interface WardStore {
  id: number
  code: string
  name: string
  department: string | null
  ward: string | null
  is_active: boolean
}

export interface WardStoreStock {
  store: WardStore
  drugs: WardStockDrug[]
}

export interface WardStockOverview {
  today: string
  stores: WardStoreStock[]
  may_raise: boolean
  may_receive: boolean
  may_decide: boolean
  may_issue: boolean
  priorities: WardIndentPriority[]
}

export interface WardIndentLine {
  id: number
  line_no: number
  drug_id: string
  drug_name: string
  dispense_unit: string | null
  status: string
  requested_units: number
  approved_units: number | null
  issued_units: number
  remaining_units: number
}

export interface WardIndentTransfer {
  id: number
  transfer_number: string | null
  status: string
  status_label: string
}

export interface WardIndent {
  id: number
  indent_number: string | null
  status: WardIndentStatus
  status_label: string
  priority: WardIndentPriority
  store_id: number
  store_code: string | null
  store_name: string | null
  store_department: string | null
  issuing_store_code: string | null
  issuing_store_name: string | null
  requested_by: string | null
  requested_by_name: string | null
  requested_at: string | null
  approved_by: string | null
  approved_by_name: string | null
  approved_at: string | null
  rejected_reason: string | null
  remarks: string | null
  number_series_configured: boolean
  lines: WardIndentLine[]
  transfers: WardIndentTransfer[]
  live_transfer: WardIndentTransfer | null
}

export interface IssuePlanPart {
  lot_id: string
  batch_no: string | null
  expiry_date: string | null
  units: number
}

export interface IssuePlanLine {
  line_no: number
  indent_line_id: number
  drug_id: string
  drug_name: string
  approved_units: number | null
  issued_units: number
  remaining_units: number
  usable_units: number
  plan: IssuePlanPart[]
  problem: WardStockProblem | null
}

export interface IssuePlan {
  indent: WardIndent
  today: string
  from_store: { code: string; name: string }
  lines: IssuePlanLine[]
}

export interface NewWardIndent {
  store_code: string
  lines: { helix_medication_id: string; quantity_units: number }[]
  priority?: WardIndentPriority
  remarks?: string | null
}

export interface IndentDecision {
  /** Lines not named are approved in full; 0 refuses that line. */
  approvals?: { line_no: number; units: number }[]
  reject?: boolean
  reason?: string | null
}

export interface IssueResult extends TransferResult {
  indent: WardIndent
}

export function getWardStock(storeCode?: string | null): Promise<WardStockOverview> {
  return pharmacyStoreGet('/ward/stock', { store_code: storeCode || undefined })
}

/** A transfer on its way to the ward. One that became controlled on its way
 *  (a drug classified narcotic or psychotropic after it was sent) has
 *  `witness_required` set and lists who may witness its receipt: people
 *  holding the NDPS permission with a signing PIN, never the nurse and never
 *  the person who sent it. */
export interface WardAwaitingTransfer extends Transfer {
  witnesses?: TransferWitness[]
}

export function getWardTransfersAwaiting(): Promise<{ transfers: WardAwaitingTransfer[] }> {
  return pharmacyStoreGet('/ward/transfers/awaiting')
}

export function listWardIndents(
  params: { status?: WardIndentFilter; store_code?: string | null; limit?: number } = {},
): Promise<{ indents: WardIndent[] }> {
  return pharmacyStoreGet('/ward/indents', {
    status: params.status || undefined,
    store_code: params.store_code || undefined,
    limit: params.limit,
  })
}

export function getWardIndent(id: number): Promise<{ indent: WardIndent }> {
  return pharmacyStoreGet(`/ward/indents/${id}`)
}

export function raiseWardIndent(body: NewWardIndent): Promise<{ indent: WardIndent }> {
  return pharmacyStorePost('/ward/indents', body)
}

export function decideWardIndent(id: number, body: IndentDecision): Promise<{ indent: WardIndent }> {
  return pharmacyStorePost(`/ward/indents/${id}/decision`, body)
}

export function cancelWardIndent(id: number, reason: string): Promise<{ indent: WardIndent }> {
  return pharmacyStorePost(`/ward/indents/${id}/cancel`, { reason })
}

export function getWardIssuePlan(id: number): Promise<IssuePlan> {
  return pharmacyStoreGet(`/ward/indents/${id}/issue-plan`)
}

export function issueWardIndent(
  id: number,
  body: { lines?: { line_no: number; units: number }[]; remarks?: string | null;
          idempotency_key?: string } = {},
): Promise<IssueResult> {
  return pharmacyStorePost(`/ward/indents/${id}/issue`, body)
}

/** Confirm what arrived. `witness` only for a transfer that became
 *  controlled on its way; the server ignores it on any other. */
export function receiveWardTransfer(
  id: number,
  lines: ReceiptLine[],
  witness?: WitnessInput | null,
): Promise<TransferResult> {
  return pharmacyStorePost(`/ward/transfers/${id}/receive`, { lines, ...(witness ?? {}) })
}

/** A fresh request key for one press of Issue, so a double press drafts once. */
export function newIssueRequestKey(): string {
  const cryptoApi = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (cryptoApi?.randomUUID) return `ward-issue-${cryptoApi.randomUUID()}`
  return `ward-issue-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}
