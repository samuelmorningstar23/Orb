/**
 * Orb, Pharmacy store API: transfers between the pharmacy's own stores (grn-11).
 *
 * A draft with its FEFO issue plan, dispatch (the units go in transit,
 * counted by neither store), receipt by somebody other than the dispatcher
 * (short receipt with a reason), and cancel before dispatch. Every call goes
 * through the shared base in ./index, so a refusal arrives as a
 * PharmacyStoreApiError carrying the server's code, for example
 * `insufficient_stock`, `plan_stale`, `same_person`, `ndps_route_refused`,
 * `witness_required`, `witness_pin_wrong` or `audit_unavailable`.
 *
 * The server owns every rule. Quantities are whole dispense units; nothing
 * here parses a float. A transfer carries no patient data.
 */
import { pharmacyStoreGet, pharmacyStorePost } from './index'

export type TransferStatus =
  | 'draft'
  | 'dispatched'
  | 'partially_received'
  | 'received'
  | 'cancelled'

/** A list filter: one status, or 'open' for everything still on its way. */
export type TransferFilter = TransferStatus | 'open' | ''

export const TRANSFER_STATUS_LABELS: Record<TransferStatus, string> = {
  draft: 'Draft',
  dispatched: 'On its way',
  partially_received: 'Partly received',
  received: 'Received',
  cancelled: 'Cancelled',
}

export type TransferStoreKind = 'dispensing' | 'sub_store' | 'ndps_vault'

export const TRANSFER_STORE_KIND_LABELS: Record<TransferStoreKind, string> = {
  dispensing: 'Dispensing pharmacy',
  sub_store: 'Sub-store',
  ndps_vault: 'NDPS vault',
}

export interface TransferLine {
  id: number
  line_no: number
  helix_medication_id: string
  drug_name: string
  generic_name: string | null
  form: string | null
  strength: string | null
  dispense_unit: string | null
  from_lot_id: string | null
  to_lot_id: string | null
  batch_no: string | null
  expiry_date: string | null
  quantity_units: number
  /** null while the line is not yet received. */
  received_units: number | null
  /** Units sent that did not arrive; null until received. */
  shortfall_units: number | null
  discrepancy_reason: string | null
  /** A narcotic or psychotropic drug. */
  ndps: boolean
  /** The status now of the lot the units went into; null until received.
   *  Anything but 'available' means they were received held, off the shelf
   *  (recalled, not of standard quality or quarantined while on their way). */
  to_lot_status?: string | null
  /** That status in words. */
  to_lot_status_label?: string | null
}

export interface Transfer {
  id: number
  transfer_number: string | null
  from_store_id: number
  to_store_id: number
  from_store_code: string | null
  from_store_name: string | null
  from_store_kind: string | null
  to_store_code: string | null
  to_store_name: string | null
  to_store_kind: string | null
  status: TransferStatus
  status_label: string
  created_by: string | null
  created_by_name: string | null
  created_at: string | null
  dispatched_by: string | null
  dispatched_by_name: string | null
  dispatched_at: string | null
  received_by: string | null
  received_by_name: string | null
  received_at: string | null
  cancelled_by: string | null
  cancelled_by_name: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  remarks: string | null
  number_series_configured: boolean
  line_count: number
  total_units: number
  in_transit_units: number
  /** The NDPS vault at either end, or a narcotic or psychotropic line. */
  controlled: boolean
  /** Dispatch and receipt each need a witness with their signing PIN. */
  witness_required: boolean
  /** Present on a single transfer, absent on list rows. */
  lines?: TransferLine[]
}

export interface TransferStore {
  id: number
  code: string
  name: string
  pharmacy_kind: TransferStoreKind
}

export interface TransferWitness {
  id: string
  name: string
}

export interface TransferOptions {
  stores: TransferStore[]
  /** Who may witness a controlled transfer: never the caller, and empty for
   *  a caller without the NDPS permission. */
  witnesses: TransferWitness[]
  may_ndps: boolean
  ndps_witness_always: boolean
  number_series_configured: boolean
  max_drugs: number
}

export interface StoreStockDrug {
  drug_id: string
  drug_name: string
  form: string | null
  strength: string | null
  dispense_unit: string | null
  ndps: boolean
  /** Schedule and NDPS class both recorded; an unclassified drug does not move. */
  classified: boolean
  usable_units: number
  lot_count: number
  earliest_expiry: string | null
}

export interface StoreStock {
  store: TransferStore
  today: string
  drugs: StoreStockDrug[]
}

export interface TransferResult {
  transfer: Transfer
  /** True when the server had already done this and wrote nothing new. */
  replayed: boolean
}

export interface WitnessInput {
  witness_id: string
  witness_pin: string
}

export interface NewTransferLine {
  helix_medication_id: string
  quantity_units: number
}

export interface NewTransfer {
  from_store_code: string
  to_store_code: string
  lines: NewTransferLine[]
  remarks?: string | null
  /** One per form submission, so a double submit raises one draft. */
  idempotency_key?: string
}

export interface ReceiptLine {
  line_id: number
  received_units: number
  discrepancy_reason?: string | null
}

export function listTransfers(
  params: { status?: TransferFilter; store_id?: number | null; limit?: number } = {},
): Promise<{ transfers: Transfer[] }> {
  return pharmacyStoreGet('/transfers', {
    status: params.status || undefined,
    store_id: params.store_id ?? undefined,
    limit: params.limit,
  })
}

export function getTransfer(id: number): Promise<{ transfer: Transfer }> {
  return pharmacyStoreGet(`/transfers/${id}`)
}

export function getTransferOptions(): Promise<TransferOptions> {
  return pharmacyStoreGet('/transfers/options')
}

export function getStoreStock(storeCode: string): Promise<StoreStock> {
  return pharmacyStoreGet('/transfers/stock', { store_code: storeCode })
}

export function createTransfer(body: NewTransfer): Promise<TransferResult> {
  return pharmacyStorePost('/transfers', body)
}

export function replanTransfer(id: number): Promise<TransferResult> {
  return pharmacyStorePost(`/transfers/${id}/replan`)
}

export function dispatchTransfer(id: number, witness?: WitnessInput | null): Promise<TransferResult> {
  return pharmacyStorePost(`/transfers/${id}/dispatch`, witness ?? {})
}

export function receiveTransfer(
  id: number,
  lines: ReceiptLine[],
  witness?: WitnessInput | null,
): Promise<TransferResult> {
  return pharmacyStorePost(`/transfers/${id}/receive`, { lines, ...(witness ?? {}) })
}

export function cancelTransfer(id: number, reason: string): Promise<TransferResult> {
  return pharmacyStorePost(`/transfers/${id}/cancel`, { reason })
}

/** A fresh request key for one submission of the new transfer form. */
export function newTransferRequestKey(): string {
  const cryptoApi = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (cryptoApi?.randomUUID) return `transfer-${cryptoApi.randomUUID()}`
  return `transfer-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}
