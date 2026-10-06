/**
 * Orb, Pharmacy store API: purchase orders (grn-7).
 *
 * Draft, submit, approve, issue and cancel pharmacy purchase orders, the
 * order form's options, and reorder suggestions turned into a draft. Every
 * call goes through the shared base in ./index, so a refusal arrives as a
 * PharmacyStoreApiError carrying the server's code, for example
 * `drug_unclassified`, `vendor_licence_expired`, `ndps_needs_vault`,
 * `same_person`, `approve_needed` or `audit_unavailable`.
 *
 * The server owns every rule (licensed vendor, classified drug with a pack
 * size and GST rate, approver differs from creator). Quantities on a line
 * are whole PACKS; rates are integer paise per pack; nothing here parses a
 * float.
 */
import { pharmacyStoreGet, pharmacyStorePost } from './index'

export type PurchaseOrderStatus =
  | 'draft'
  | 'pending_approval'
  | 'approved'
  | 'issued'
  | 'partially_received'
  | 'received'
  | 'closed'
  | 'cancelled'

export interface PurchasingProblem {
  code: string
  message: string
}

export interface PurchasingWarning {
  code: string
  message: string
}

export type LicenceStatus = 'none' | 'expired' | 'expiring' | 'valid'

export interface VendorLicence {
  status: LicenceStatus
  valid_to: string | null
  numbers: string | null
  ndps_number: string | null
  warning: PurchasingWarning | null
}

export interface PurchaseOrderLine {
  id: number
  line_no: number
  item_code: string
  description: string | null
  helix_medication_id: string | null
  uom: string | null
  quantity_milli: number
  received_quantity_milli: number
  quantity_packs: number
  received_packs: number
  units_per_pack: number | null
  quantity_units: number | null
  unit_rate_paise: number
  gross_paise: number
  discount_paise: number
  gst_rate_bp: number
  taxable_value_paise: number
  cgst_paise: number
  sgst_paise: number
  igst_paise: number
  tax_paise: number
  total_paise: number
  status: string
  remarks: string | null
}

export interface PurchaseOrder {
  id: number
  po_number: string | null
  status: PurchaseOrderStatus
  domain: 'pharmacy'
  vendor_code: string | null
  vendor_name: string | null
  vendor_state_code?: string | null
  store_code: string | null
  store_name?: string | null
  order_date: string | null
  expected_date: string | null
  is_interstate: number
  subtotal_paise: number
  discount_paise: number
  taxable_value_paise: number
  tax_paise: number
  other_charges_paise: number
  rounding_adjustment_paise: number
  total_paise: number
  created_by: string | null
  created_by_name?: string | null
  created_at?: string | null
  approved_by?: string | null
  approved_by_name?: string | null
  approved_at?: string | null
  issued_at?: string | null
  cancelled_by?: string | null
  cancelled_by_name?: string | null
  cancelled_at?: string | null
  cancel_reason?: string | null
  remarks: string | null
  lines?: PurchaseOrderLine[]
  legal_transitions?: string[]
  vendor_licence?: VendorLicence | null
  number_series_configured?: boolean
}

export interface PurchaseOrderResult {
  purchase_order: PurchaseOrder
  warnings: PurchasingWarning[]
}

export interface PurchasingVendorOption {
  code: string
  name: string
  state_code: string | null
  licence_status: LicenceStatus
  drug_licence_valid_to: string | null
  has_ndps_licence: boolean
  orderable: boolean
}

export interface PurchasingStoreOption {
  code: string
  name: string
  pharmacy_kind: string
  kind_label: string
}

export interface PurchasingDrugOption {
  id: string
  name: string
  generic_name: string | null
  form: string | null
  strength: string | null
  pack_size_units: number | null
  purchase_uom: string | null
  gst_rate_bp: number | null
  ndps: boolean
  orderable: boolean
  problems: PurchasingProblem[]
}

export interface PurchasingOptions {
  vendors: PurchasingVendorOption[]
  stores: PurchasingStoreOption[]
  drugs: PurchasingDrugOption[]
  number_series_configured: boolean
  licence_warn_days: number
}

export interface ReorderSuggestion {
  store_id: number | null
  store_code: string | null
  store_label: string
  drug_id: string
  drug_name: string
  form: string | null
  strength: string | null
  pack_size_units: number | null
  purchase_uom: string | null
  basis: 'par' | 'drug_default'
  basis_label: string
  reorder_level_units: number | null
  level_label: string
  stock_units: number
  in_transit_units: number
  on_order_units: number
  position_units: number | null
  needs_reorder: boolean
  suggested_units: number | null
  suggested_packs: number | null
  note: string | null
  last_rate_paise: number | null
  last_vendor_code: string | null
  last_ordered_on: string | null
  orderable: boolean
  problems: PurchasingProblem[]
}

export interface PurchaseOrderLineInput {
  helix_medication_id: string
  packs: number
  unit_rate_paise: number
  discount_bp?: number
  remarks?: string | null
}

export interface PurchaseOrderInput {
  vendor_code: string
  store_code: string
  lines: PurchaseOrderLineInput[]
  expected_date?: string | null
  is_interstate?: boolean
  remarks?: string | null
}

export interface SuggestionItemInput {
  helix_medication_id: string
  unit_rate_paise: number
  /** Omit to take the server's current suggestion for that store. */
  packs?: number
}

export interface FromSuggestionsInput {
  vendor_code: string
  store_code: string
  items: SuggestionItemInput[]
  expected_date?: string | null
  is_interstate?: boolean
  remarks?: string | null
}

/** Plain words for each status, sentence case. */
export const PURCHASE_ORDER_STATUS_LABELS: Record<PurchaseOrderStatus, string> = {
  draft: 'Draft',
  pending_approval: 'Awaiting approval',
  approved: 'Approved',
  issued: 'Sent to vendor',
  partially_received: 'Partly received',
  received: 'Received',
  closed: 'Closed',
  cancelled: 'Cancelled',
}

export const LICENCE_LABELS: Record<LicenceStatus, string> = {
  none: 'No drug licence on file',
  expired: 'Drug licence expired',
  expiring: 'Drug licence ending soon',
  valid: 'Drug licence valid',
}

export function listPurchaseOrders(
  params: { status?: PurchaseOrderStatus | ''; limit?: number } = {},
): Promise<{ purchase_orders: PurchaseOrder[] }> {
  return pharmacyStoreGet('/purchase-orders', params)
}

export function getPurchaseOrder(poId: number): Promise<{ purchase_order: PurchaseOrder }> {
  return pharmacyStoreGet(`/purchase-orders/${encodeURIComponent(String(poId))}`)
}

export function getPurchasingOptions(): Promise<PurchasingOptions> {
  return pharmacyStoreGet('/purchase-orders/options')
}

export function getReorderSuggestions(
  storeId?: number | null,
): Promise<{ suggestions: ReorderSuggestion[] }> {
  return pharmacyStoreGet('/purchase-orders/reorder-suggestions', { store_id: storeId ?? undefined })
}

export function createPurchaseOrder(body: PurchaseOrderInput): Promise<PurchaseOrderResult> {
  return pharmacyStorePost('/purchase-orders', body)
}

export function createPurchaseOrderFromSuggestions(
  body: FromSuggestionsInput,
): Promise<PurchaseOrderResult> {
  return pharmacyStorePost('/purchase-orders/from-suggestions', body)
}

export type PurchaseOrderAction = 'submit' | 'approve' | 'issue'

export function movePurchaseOrder(
  poId: number,
  action: PurchaseOrderAction,
): Promise<PurchaseOrderResult> {
  return pharmacyStorePost(`/purchase-orders/${encodeURIComponent(String(poId))}/${action}`)
}

export function cancelPurchaseOrder(poId: number, reason: string): Promise<PurchaseOrderResult> {
  return pharmacyStorePost(`/purchase-orders/${encodeURIComponent(String(poId))}/cancel`, { reason })
}
