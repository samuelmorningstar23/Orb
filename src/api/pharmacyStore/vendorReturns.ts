/**
 * Orb, Pharmacy store API: returns to vendor (grn-22).
 *
 * A draft names lots of one store going back to one vendor: lots held off the
 * shelf (quarantined, recalled, not of standard quality, a ward return
 * waiting for inspection), expired, or near expiry. Posting (the approve
 * tier, never the person who drafted it) takes the units off the shelf and
 * numbers the document from the PHVR series; the vendor's credit note number
 * is recorded afterwards. A narcotic or psychotropic return needs the NDPS
 * permission to draft and a witness with their signing PIN to post.
 *
 * Every call goes through the shared base in ./index, so a refusal arrives as
 * a PharmacyStoreApiError with the server's code, for example
 * `lot_not_returnable`, `exceeds_lot`, `lot_from_another_vendor`,
 * `same_person`, `witness_required`, `vendor_ndps_licence_missing` or
 * `audit_unavailable`. Quantities are whole dispense units; money is integer
 * paise. A return carries no patient data.
 */
import { pharmacyStoreGet, pharmacyStorePost } from './index'

export type VendorReturnStatus = 'draft' | 'posted' | 'credited' | 'cancelled'
export type VendorReturnFilter = VendorReturnStatus | ''

export const VENDOR_RETURN_STATUS_LABELS: Record<VendorReturnStatus, string> = {
  draft: 'Draft',
  posted: 'Sent back',
  credited: 'Credit note received',
  cancelled: 'Cancelled',
}

export interface VendorReturnLine {
  id: number
  line_no: number
  lot_id: string
  helix_medication_id: string
  drug_name: string
  strength: string | null
  form: string | null
  batch_no: string | null
  expiry_date: string | null
  quantity_units: number
  unit_cost_paise: number | null
  value_paise: number | null
  reason: string | null
  /** A narcotic or psychotropic drug. */
  ndps: boolean
  lot_status: string | null
  lot_units: number | null
}

export interface VendorReturn {
  id: number
  return_number: string | null
  vendor_id: number
  vendor_code: string | null
  vendor_name: string | null
  store_id: number
  store_code: string | null
  store_name: string | null
  store_kind: string | null
  source_receipt_ref: number | null
  status: VendorReturnStatus
  status_label: string
  reason: string | null
  vendor_credit_note_ref: string | null
  total_paise: number
  created_by: string | null
  created_by_name: string | null
  created_at: string | null
  posted_by: string | null
  posted_by_name: string | null
  posted_at: string | null
  credited_at: string | null
  cancelled_by: string | null
  cancelled_by_name: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  remarks: string | null
  number_series_configured: boolean
  line_count: number
  total_units: number
  /** A narcotic or psychotropic line. */
  controlled: boolean
  /** Posting needs a witness with their signing PIN. */
  witness_required: boolean
  /** Present on a single return, absent on list rows. */
  lines?: VendorReturnLine[]
}

export interface VendorReturnWarning {
  code: string
  message: string
}

export interface VendorReturnResult {
  vendor_return: VendorReturn
  warnings: VendorReturnWarning[]
  /** True when the server had already done this and wrote nothing new. */
  replayed: boolean
}

export interface VendorReturnStore {
  id: number
  code: string
  name: string
  pharmacy_kind: string
}

export interface VendorReturnVendor {
  code: string
  name: string
  is_active: boolean
  licence_status: 'none' | 'expired' | 'expiring' | 'valid'
  has_ndps_licence: boolean
}

export interface VendorReturnOptions {
  stores: VendorReturnStore[]
  vendors: VendorReturnVendor[]
  witnesses: { id: string; name: string }[]
  may_ndps: boolean
  near_expiry_days: number
  number_series_configured: boolean
  max_lines: number
}

export type ReturnableWhy = 'held' | 'expired' | 'near_expiry'

export interface ReturnableLot {
  lot_id: string
  drug_id: string
  drug_name: string
  strength: string | null
  form: string | null
  dispense_unit: string | null
  ndps: boolean
  batch_no: string
  expiry_date: string | null
  status: string
  quantity_units: number
  unit_cost_paise: number | null
  vendor_id: number | null
  vendor_code: string | null
  vendor_name: string | null
  why: ReturnableWhy
  why_label: string
  days_left: number | null
}

export interface ReturnableStock {
  store: VendorReturnStore
  today: string
  near_expiry_days: number
  lots: ReturnableLot[]
}

export interface NewVendorReturn {
  store_code: string
  vendor_code: string
  reason: string
  remarks?: string | null
  lines: { lot_id: string; quantity_units: number; reason?: string | null }[]
  /** One per form submission, so a double submit drafts one return. */
  idempotency_key?: string
}

export function listVendorReturns(
  params: { status?: VendorReturnFilter; limit?: number } = {},
): Promise<{ vendor_returns: VendorReturn[] }> {
  return pharmacyStoreGet('/vendor-returns', { status: params.status || undefined,
    limit: params.limit })
}

export function getVendorReturn(id: number): Promise<{ vendor_return: VendorReturn }> {
  return pharmacyStoreGet(`/vendor-returns/${id}`)
}

export function getVendorReturnOptions(): Promise<VendorReturnOptions> {
  return pharmacyStoreGet('/vendor-returns/options')
}

export function getReturnableStock(storeCode: string): Promise<ReturnableStock> {
  return pharmacyStoreGet('/vendor-returns/stock', { store_code: storeCode })
}

export function createVendorReturn(body: NewVendorReturn): Promise<VendorReturnResult> {
  return pharmacyStorePost('/vendor-returns', body)
}

export function postVendorReturn(
  id: number,
  witness?: { witness_id: string; witness_pin: string } | null,
): Promise<VendorReturnResult> {
  return pharmacyStorePost(`/vendor-returns/${id}/post`, witness ?? {})
}

export function recordVendorCredit(id: number, creditNoteRef: string): Promise<VendorReturnResult> {
  return pharmacyStorePost(`/vendor-returns/${id}/credit`, { credit_note_ref: creditNoteRef })
}

export function cancelVendorReturn(id: number, reason: string): Promise<VendorReturnResult> {
  return pharmacyStorePost(`/vendor-returns/${id}/cancel`, { reason })
}

/** A fresh request key for one submission of the new return form. */
export function newVendorReturnKey(): string {
  const cryptoApi = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (cryptoApi?.randomUUID) return `vendor-return-${cryptoApi.randomUUID()}`
  return `vendor-return-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}
