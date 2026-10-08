/**
 * Orb, Pharmacy store API: goods receipts (grn-8).
 *
 * A delivery is recorded as a DRAFT against an order sent to the vendor, line
 * by line: whole packs received, free packs, the batch, the expiry typed twice,
 * the MRP printed on the pack and the optional manufacturing date. POST puts it
 * on the shelf in one transaction; a draft may be cancelled with a reason.
 *
 * The server owns every rule (expiry and minimum shelf life on the hospital's
 * date, short expiry accepted only by the approve tier from someone other than
 * the receiver, batch and MRP agreement, NDPS drugs into the vault). A refusal
 * arrives as a PharmacyStoreApiError carrying its code, for example
 * `expiry_mismatch`, `expired_on_receipt`, `approve_needed`, `over_receipt`,
 * `batch_expiry_mismatch` or `ndps_needs_vault`. Quantities are whole PACKS on
 * the way in and whole dispense UNITS on the way back; money is integer paise.
 */
import { pharmacyStoreGet, pharmacyStorePost, pharmacyStoreRequest } from './index'

export type GoodsReceiptStatus = 'draft' | 'posted' | 'cancelled' | 'returned'

export interface ReceiptWarning {
  code: string
  message: string
  line_no?: number
  difference_paise?: number
}

export interface GoodsReceiptLine {
  id: number
  line_no: number
  item_code: string
  description: string | null
  helix_medication_id: string | null
  uom: string | null
  units_per_pack: number | null
  batch_no: string | null
  expiry_date: string | null
  mfg_date: string | null
  mrp_pack_paise: number | null
  unit_rate_paise: number
  total_paise: number
  packs: number
  free_packs: number
  rejected_packs: number
  rejection_reason: string | null
  units: number
  unit_cost_paise: number | null
  days_to_expiry: number | null
  expired: boolean
  short_dated: boolean
  rate_above_mrp: boolean
  helix_lot_ref: string | null
}

export interface GoodsReceipt {
  id: number
  grn_number: string | null
  status: GoodsReceiptStatus
  purchase_order_id: number
  po_number: string | null
  vendor_code?: string | null
  vendor_name: string | null
  store_code: string | null
  store_name?: string | null
  vendor_invoice_number: string | null
  vendor_invoice_date: string | null
  vendor_invoice_total_paise: number | null
  taxable_value_paise?: number
  tax_paise?: number
  total_paise: number
  received_by: string | null
  received_by_name?: string | null
  received_at: string | null
  posted_by?: string | null
  posted_by_name?: string | null
  posted_at?: string | null
  cancelled_by_name?: string | null
  cancel_reason?: string | null
  needs_approval?: boolean
  has_expired_line?: boolean
  number_series_configured?: boolean
  min_shelf_life_days?: number
  warnings?: ReceiptWarning[]
  lines?: GoodsReceiptLine[]
}

export interface ReceiptResult {
  goods_receipt: GoodsReceipt
  warnings: ReceiptWarning[]
  /** True when the server answered an earlier identical request. */
  replayed: boolean
}

export interface ReceivableLine {
  line_no: number
  helix_medication_id: string | null
  description: string | null
  uom: string | null
  units_per_pack: number | null
  ordered_packs: number
  received_packs: number
  outstanding_packs: number
  unit_rate_paise: number | null
  gst_rate_bp: number | null
  ndps: boolean
}

export interface ReceivableOrder {
  id: number
  po_number: string | null
  status: string
  vendor_code: string | null
  vendor_name: string | null
  store_code: string | null
  store_name: string | null
  order_date: string | null
  lines: ReceivableLine[]
}

export interface Receivable {
  orders: ReceivableOrder[]
  min_shelf_life_days: number
  number_series_configured: boolean
  today: string
}

export interface ReceiptLineInput {
  po_line_no: number
  packs: number
  free_packs?: number
  rejected_packs?: number
  rejection_reason?: string
  batch_no?: string
  expiry_date?: string
  /** The expiry typed a second time; the server compares the two. */
  expiry_date_confirm?: string
  mrp_pack_paise?: number
  mfg_date?: string
}

export interface ReceiptInput {
  purchase_order_id: number
  lines: ReceiptLineInput[]
  vendor_invoice_number: string
  vendor_invoice_date: string
  vendor_invoice_total_paise?: number | null
  remarks?: string | null
}

/** Plain words for each status, sentence case. */
export const GOODS_RECEIPT_STATUS_LABELS: Record<GoodsReceiptStatus, string> = {
  draft: 'Draft, not on the shelf yet',
  posted: 'Posted to stock',
  cancelled: 'Cancelled',
  returned: 'Returned',
}

/** One key per receipt form, so a retried save never makes a second draft. */
export function newReceiptKey(): string {
  const random = globalThis.crypto?.randomUUID?.()
  return `grn-${random ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`
}

export function listGoodsReceipts(
  params: { status?: GoodsReceiptStatus | ''; purchase_order_id?: number; limit?: number } = {},
): Promise<{ goods_receipts: GoodsReceipt[] }> {
  return pharmacyStoreGet('/goods-receipts', params)
}

export function getGoodsReceipt(grnId: number): Promise<{ goods_receipt: GoodsReceipt }> {
  return pharmacyStoreGet(`/goods-receipts/${encodeURIComponent(String(grnId))}`)
}

export function getReceivableOrders(): Promise<Receivable> {
  return pharmacyStoreGet('/goods-receipts/receivable')
}

export function createGoodsReceipt(body: ReceiptInput, idempotencyKey: string): Promise<ReceiptResult> {
  return pharmacyStoreRequest('/goods-receipts', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

/** Post a draft to stock. A short-dated batch needs the approve tier and a
 *  reason, given here. */
export function postGoodsReceipt(grnId: number, shortExpiryReason?: string): Promise<ReceiptResult> {
  return pharmacyStorePost(`/goods-receipts/${encodeURIComponent(String(grnId))}/post`,
    shortExpiryReason ? { short_expiry_reason: shortExpiryReason } : {})
}

export function cancelGoodsReceipt(grnId: number, reason: string): Promise<ReceiptResult> {
  return pharmacyStorePost(`/goods-receipts/${encodeURIComponent(String(grnId))}/cancel`, { reason })
}
