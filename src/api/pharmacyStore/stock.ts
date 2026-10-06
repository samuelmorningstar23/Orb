/**
 * Orb, Pharmacy store API: stock views (grn-9).
 *
 * Stock per store and drug, lots, the drug ledger paged by seq, reorder
 * against store par levels or the drug's own level, near-expiry valued at
 * cost and MRP, the overview tiles and the reconciliation check. All reads,
 * all pharmacy_store.read. Every call goes through the shared base in ./index.
 *
 * Units are whole DISPENSE UNITS; money is integer paise. A value the server
 * could not compute (a lot with no recorded cost) arrives as null with a
 * label such as 'Rate not configured', and a level nobody set as null with
 * 'No reorder level set': screens show the label, never 0.
 */
import { pharmacyStoreGet } from './index'

export interface StockRow {
  store_id: number | null
  store_code: string | null
  store_label: string
  store_kind: string | null
  store_kind_label: string | null
  store_active: boolean | null
  drug_id: string
  drug_name: string
  generic_name: string | null
  form: string | null
  strength: string | null
  dispense_unit: string | null
  on_hand_units: number
  dispensable_units: number
  usable_units: number
  expired_units: number
  undated_units: number
  quarantined_units: number
  in_transit_units: number
  other_units: number
  lot_count: number
}

export interface StockTotals {
  on_hand_units: number
  dispensable_units: number
  usable_units: number
  expired_units: number
  quarantined_units: number
  in_transit_units: number
  other_units: number
}

export interface StockReport {
  today: string
  rows: StockRow[]
  totals: StockTotals
}

export type ExpiryState = 'undated' | 'expired' | 'near_expiry' | 'in_date'

export interface StockLot {
  id: string
  drug_id: string
  drug_name: string
  form: string | null
  strength: string | null
  dispense_unit: string | null
  lot_number: string
  expiration_date: string | null
  expiry_state: ExpiryState
  quantity: number
  status: string
  status_label: string
  dispensable: boolean
  store_id: number | null
  store_code: string | null
  store_label: string
  store_kind: string | null
  units_per_pack: number | null
  unit_cost_paise: number | null
  mrp_pack_paise: number | null
  cost_value_paise: number | null
  cost_label: string | null
  mrp_value_paise: number | null
  mrp_label: string | null
}

export interface LedgerMovement {
  id: string
  seq: number
  occurred_at: string | null
  movement_type: string
  movement_label: string
  quantity_change: number
  balance_after_units: number | null
  lot_id: string | null
  lot_number: string | null
  expiration_date: string | null
  drug_id: string
  drug_name: string
  form: string | null
  store_id: number | null
  store_label: string
  actor_id: string | null
  actor_name: string | null
  reference_type: string | null
  reference_id: string | null
  reference_label: string | null
  patient_linked: boolean
  remarks: string | null
}

export interface LedgerPage {
  movements: LedgerMovement[]
  has_more: boolean
  next_before_seq: number | null
}

export interface ReorderRow {
  store_id: number | null
  store_code: string | null
  store_label: string
  store_kind: string | null
  drug_id: string
  drug_name: string
  form: string | null
  strength: string | null
  dispense_unit: string | null
  pack_size_units: number | null
  purchase_uom: string | null
  ndps: boolean
  basis: 'par' | 'drug_default' | 'none'
  basis_label: string
  reorder_level_units: number | null
  level_label: string
  target_units: number | null
  stock_units: number
  in_transit_units: number
  on_order_units: number
  position_units: number | null
  needs_reorder: boolean
  suggested_units: number | null
  suggested_packs: number | null
  note: string | null
}

export interface NearExpiryTotals {
  lots: number
  units: number
  cost_value_paise: number
  cost_unpriced_lots: number
  mrp_value_paise: number
  mrp_unpriced_lots: number
}

export interface NearExpiryReport {
  today: string
  days: number
  cutoff: string
  lots: StockLot[]
  expired: StockLot[]
  totals: { near_expiry: NearExpiryTotals; expired: NearExpiryTotals }
}

export interface StockOverview {
  today: string
  below_reorder_level: number
  without_reorder_level: number
  near_expiry_days: number
  near_expiry_lots: number
  near_expiry_units: number
  expired_on_shelf_units: number
  quarantined_units: number
  in_transit_units: number
  dispensable_units: number
  legacy_lots: number
  purchase_orders: {
    draft: number
    awaiting_approval: number
    approved: number
    open_with_vendor: number
  }
}

export interface ReconcileReport {
  clean: boolean
  lots_checked: number
  mismatched_lots: unknown[]
  negative_lots: unknown[]
  balance_breaks: unknown[]
  [key: string]: unknown
}

export function getStock(
  params: { store_id?: number; drug_id?: string; search?: string } = {},
): Promise<StockReport> {
  return pharmacyStoreGet('/stock', params)
}

export function getStockOverview(): Promise<StockOverview> {
  return pharmacyStoreGet('/stock/overview')
}

export function listStockLots(
  params: { drug_id?: string; store_id?: number | null; include_empty?: boolean } = {},
): Promise<{ today: string; near_expiry_days: number; lots: StockLot[] }> {
  return pharmacyStoreGet('/stock/lots', {
    drug_id: params.drug_id,
    store_id: params.store_id ?? undefined,
    include_empty: params.include_empty ? true : undefined,
  })
}

export function getLedgerPage(
  params: { drug_id?: string; store_id?: number; lot_id?: string; before_seq?: number | null;
    limit?: number } = {},
): Promise<LedgerPage> {
  return pharmacyStoreGet('/ledger', {
    drug_id: params.drug_id,
    store_id: params.store_id,
    lot_id: params.lot_id,
    before_seq: params.before_seq ?? undefined,
    limit: params.limit,
  })
}

export function getReorderReport(
  params: { store_id?: number; include_unset?: boolean; needs_only?: boolean } = {},
): Promise<{ today: string; rows: ReorderRow[] }> {
  return pharmacyStoreGet('/reorder', params)
}

export function getNearExpiry(
  params: { days?: number; store_id?: number } = {},
): Promise<NearExpiryReport> {
  return pharmacyStoreGet('/near-expiry', params)
}

export function getStockReconcile(): Promise<ReconcileReport> {
  return pharmacyStoreGet('/reconcile')
}
