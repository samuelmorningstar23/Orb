/**
 * Orb, Pharmacy store API: crash carts (grn-23).
 *
 * A crash cart is a ward store (ward.ts fills it with ordinary ward indents).
 * These calls add its own record: the cart against its list (seal, checks due,
 * expired, expiring and held batches, the top-up after use), the seal-and-check
 * record, the pharmacy's decision on a check that found a difference, the
 * top-up request, and which carts a batch went to.
 *
 * Behind the pharmacy store (ORB_PHARMACY_STORE) AND ward stock
 * (ORB_PHARMACY_WARD_STOCK), both off by default; ward stock stays off until
 * Samuel confirms ruling GRN-RS5. The server owns every rule: who may check
 * (a ward_stock.receive holder of the cart's department, or the pharmacy at
 * the write tier), that the person who counted never confirms, that units
 * found short leave the books only on confirmation and units found over are
 * never added. A refusal arrives as a PharmacyStoreApiError carrying the
 * server's code, for example `check_stale`, `check_pending`,
 * `reason_required`, `same_person`, `nothing_to_top_up` or
 * `audit_unavailable`. Nothing here carries patient data.
 */
import { pharmacyStoreGet, pharmacyStorePost } from './index'
import type { WardIndent, WardIndentPriority, WardStockDrug } from './ward'

export type CartCheckKind = 'seal' | 'full'
export type CartCheckStatus = 'submitted' | 'approved' | 'rejected'

export const CART_CHECK_KIND_LABELS: Record<CartCheckKind, string> = {
  seal: 'Seal check',
  full: 'Full check',
}

export interface CartCheckLine {
  id: number
  line_no: number
  /** Null for an item found in the cart that the books do not show there. */
  lot_id: string | null
  drug_id: string
  drug_name: string
  batch_no: string | null
  /** A calendar key (YYYY-MM-DD). */
  expiry_date: string | null
  system_units: number
  counted_units: number | null
  short_units: number
  over_units: number
  found_extra: boolean
  expired_at_check: boolean
  adjustment_movement_ref: string | null
}

export interface CartCheck {
  id: number
  store_id: number
  store_code: string | null
  store_name: string | null
  department: string | null
  kind: CartCheckKind
  kind_label: string
  status: CartCheckStatus
  status_label: string
  seal_found: string | null
  seal_intact: boolean | null
  seal_expected: string | null
  seal_matches: boolean | null
  seal_applied: string | null
  note: string | null
  counted_by: string | null
  counted_by_name: string | null
  /** Bare UTC from the server. */
  checked_at: string | null
  decided_by: string | null
  decided_by_name: string | null
  decided_at: string | null
  decision_reason: string | null
  short_units: number
  over_units: number
  /** The finding in one sentence. */
  outcome: string
  lines?: CartCheckLine[]
}

export interface CartLotEntry {
  drug_id: string
  drug_name: string
  lot_id: string
  batch_no: string | null
  expiry_date: string | null
  units: number
  status: string
  status_label: string
  days_left?: number
}

export interface CartTopUpLine {
  drug_id: string
  drug_name: string
  units: number
  dispense_unit: string | null
}

export interface CartDrug extends WardStockDrug {
  /** Units a check found short that the pharmacy has not yet confirmed. */
  found_short_units: number
}

export interface CrashCart {
  store: {
    id: number
    code: string
    name: string
    department: string | null
    ward: string | null
    is_active: boolean
    pharmacy_kind?: string
    kind_label?: string
  }
  drugs: CartDrug[]
  /** The number of the last seal applied, or null when none is on record. */
  seal: string | null
  seal_state: 'sealed' | 'broken' | 'none'
  last_check: CartCheck | null
  last_full_check: CartCheck | null
  pending_check: CartCheck | null
  seal_check_due: boolean
  full_check_due: boolean
  due_reasons: string[]
  expired: CartLotEntry[]
  expiring: CartLotEntry[]
  held: CartLotEntry[]
  top_up: CartTopUpLine[]
  below_par: number
  not_allowed: { drug_id: string; drug_name: string; message: string }[]
}

export interface CrashCartOverview {
  today: string
  near_expiry_days: number
  seal_check_due_hours: number
  full_check_due_days: number
  carts: CrashCart[]
  may_check: boolean
  may_confirm: boolean
  may_raise: boolean
  priorities: WardIndentPriority[]
}

export interface NewCartCheck {
  kind: CartCheckKind
  /** The number read on the cart's seal; null when the cart had no seal. */
  seal_found: string | null
  seal_intact: boolean
  /** A full check only: the new seal fixed after the count. */
  seal_applied?: string | null
  /** A full check only: every batch in the cart, counted. */
  lines?: { lot_id: string; counted_units: number }[]
  /** A full check only: items found that the books do not show in the cart. */
  extra?: { drug_id: string; units: number; batch_no?: string | null }[]
  note?: string | null
  idempotency_key?: string
}

export interface CartBatchCart {
  store_id: number
  store_code: string
  store_name: string
  department: string | null
  transfers: number[]
  sent_units: number
  received_units: number
  in_transit_units: number
  in_cart_units: number
  unattributed_units: number
  label: string
}

export interface CartBatchTrace {
  drug_id: string
  drug_name: string
  batch_no: string
  carts: CartBatchCart[]
  totals: Record<'sent_units' | 'received_units' | 'in_transit_units' | 'in_cart_units'
    | 'unattributed_units', number>
}

export function getCrashCarts(storeCode?: string | null): Promise<CrashCartOverview> {
  return pharmacyStoreGet('/ward/crash-carts', { store_code: storeCode || undefined })
}

export function listCartChecks(
  params: { store_code?: string | null; status?: CartCheckStatus | ''; limit?: number } = {},
): Promise<{ checks: CartCheck[] }> {
  return pharmacyStoreGet('/ward/crash-carts/checks', {
    store_code: params.store_code || undefined,
    status: params.status || undefined,
    limit: params.limit,
  })
}

export function getCartCheck(id: number): Promise<{ check: CartCheck }> {
  return pharmacyStoreGet(`/ward/crash-carts/checks/${id}`)
}

export function recordCartCheck(
  storeCode: string, body: NewCartCheck,
): Promise<{ check: CartCheck; replayed: boolean }> {
  return pharmacyStorePost(`/ward/crash-carts/${encodeURIComponent(storeCode)}/checks`, body)
}

export function decideCartCheck(
  id: number, body: { confirm: boolean; reason?: string | null },
): Promise<{ check: CartCheck }> {
  return pharmacyStorePost(`/ward/crash-carts/checks/${id}/decision`, body)
}

export function requestCartTopUp(
  storeCode: string, body: { priority?: WardIndentPriority; remarks?: string | null } = {},
): Promise<{ indent: WardIndent; left_out: { drug_id: string; drug_name: string; why: string }[] }> {
  return pharmacyStorePost(`/ward/crash-carts/${encodeURIComponent(storeCode)}/top-up`, body)
}

export function getCartBatchTrace(drugId: string, batchNo: string): Promise<CartBatchTrace> {
  return pharmacyStoreGet('/ward/crash-carts/batch', { drug_id: drugId, batch_no: batchNo })
}

/** A fresh request key for one press of Record, so a double press records once. */
export function newCartCheckKey(): string {
  const cryptoApi = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto
  if (cryptoApi?.randomUUID) return `cart-check-${cryptoApi.randomUUID()}`
  return `cart-check-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}
