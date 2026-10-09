/**
 * Orb, Pharmacy store API: the NDPS register (grn-13).
 *
 * The register of narcotic and psychotropic drugs: one running balance per
 * drug and store, written by the server inside every stock movement of a
 * controlled drug while the register is on. This screen reads it (balances
 * against the shelf, entries, variances, a CSV export) and makes the few
 * entries a person makes: a witnessed opening from the shelf, a check against
 * the shelf that raises variances, a witnessed reconciliation of a variance,
 * and a report to the incident register.
 *
 * Every call needs pharmacy_store.ndps; a refusal arrives as a
 * PharmacyStoreApiError with the server's code, for example `register_off`,
 * `witness_required`, `witness_pin_wrong`, `witness_locked`,
 * `already_reconciled` or `audit_unavailable`. Quantities are whole dispense
 * units; times are bare UTC from the server.
 */
import { pharmacyStoreDownload, pharmacyStoreGet, pharmacyStorePost } from './index'

export type NdpsLane = 'live' | 'non_production'
export type NdpsFailureMode = 'block' | 'record'

export type NdpsEntryType =
  | 'opening'
  | 'receipt'
  | 'dispense'
  | 'transfer_in'
  | 'transfer_out'
  | 'ward_return'
  | 'vendor_return'
  | 'wastage'
  | 'destruction'
  | 'write_off'
  | 'adjustment'
  | 'count_variance'

export type NdpsBalanceStatus = 'matches' | 'differs' | 'not_opened' | 'chain_broken'

export const NDPS_BALANCE_STATUS_LABELS: Record<NdpsBalanceStatus, string> = {
  matches: 'Matches the shelf',
  differs: 'Differs from the shelf',
  not_opened: 'Register not opened',
  chain_broken: 'Running balance broken',
}

export type NdpsVarianceKind =
  | 'register_write_failed'
  | 'negative_balance'
  | 'count_mismatch'
  | 'reconciliation_mismatch'

export type NdpsVarianceStatus = 'open' | 'reconciled'
export type NdpsVarianceFilter = NdpsVarianceStatus | 'all'

export interface NdpsStore {
  id: number
  code: string
  name: string
  pharmacy_kind: string
}

export interface NdpsDrug {
  id: string
  name: string
  strength: string | null
  form: string | null
  ndps_category: 'narcotic' | 'psychotropic'
}

export interface NdpsWitness {
  id: string
  name: string
}

export interface NdpsOptions {
  /** The register is writing: the switch and a failure mode are both set. */
  register_on: boolean
  failure_mode: NdpsFailureMode | null
  /** The site asked for the register (ORB_PHARMACY_NDPS_REGISTER). */
  requested: boolean
  lane: NdpsLane
  lane_label: string
  stores: NdpsStore[]
  drugs: NdpsDrug[]
  /** Who may witness: never the caller. */
  witnesses: NdpsWitness[]
  may_ndps: boolean
  entry_labels: Record<string, string>
  variance_labels: Record<string, string>
}

export interface NdpsBalanceRow {
  store_id: number
  store_code: string | null
  store_name: string
  store_kind: string | null
  drug_id: string
  drug_name: string
  strength: string | null
  form: string | null
  ndps_category: string | null
  controlled: boolean
  /** null when the register has not been opened here. */
  register_units: number | null
  shelf_units: number
  difference_units: number | null
  opened_at: string | null
  opening_seq: number | null
  entries_since_opening: number
  status: NdpsBalanceStatus
  status_label: string
  open_variances: number
}

export interface NdpsBalances {
  lane: NdpsLane
  lane_label: string
  rows: NdpsBalanceRow[]
  clean: boolean
  /** Controlled units on lots not yet in any pharmacy store. */
  unplaced_units: number
}

export interface NdpsEntry {
  id: number
  seq: number
  lane: NdpsLane
  lane_label: string
  store_id: number
  store_name: string | null
  drug_id: string
  drug_name: string
  strength: string | null
  form: string | null
  lot_id: string | null
  batch_no: string | null
  entry_type: NdpsEntryType
  entry_label: string
  /** Signed: units in are positive, units out negative. */
  quantity_units: number
  balance_after_units: number
  counterparty_type: string | null
  /** Words for who the units came from or went to. */
  counterparty: string
  /** Null for a patient outside the reader's department. */
  patient_id: string | null
  patient_name: string | null
  patient_mrn: string | null
  patient_in_scope: boolean | null
  helix_order_id: string | null
  prescriber_name: string | null
  document_ref: string | null
  movement_ref: string | null
  actor_id: string
  actor_name: string | null
  witness_id: string | null
  witness_name: string | null
  occurred_at: string
  recorded_at: string | null
  remarks: string | null
}

export interface NdpsRegisterPage {
  lane: NdpsLane
  lane_label: string
  entries: NdpsEntry[]
  /** Pass as before_id for the next, older page; null on the last page. */
  next_before_id: number | null
}

export interface NdpsVariance {
  id: number
  lane: NdpsLane
  lane_label: string
  store_id: number | null
  store_name: string | null
  helix_medication_id: string
  drug_name: string
  lot_id: string | null
  helix_order_id: string | null
  movement_ref: string | null
  variance_kind: NdpsVarianceKind
  variance_label: string
  expected_units: number | null
  found_units: number | null
  variance_units: number | null
  detail: string | null
  status: NdpsVarianceStatus
  raised_by: string
  raised_by_name: string | null
  raised_at: string
  reconciled_by: string | null
  reconciled_by_name: string | null
  reconciled_at: string | null
  reconciliation_note: string | null
  incident_ref: string | null
}

export interface NdpsWitnessInput {
  witness_id: string
  witness_pin: string
}

export interface NdpsOpening {
  entry_id: number
  store_id: number
  drug_id: string
  units: number
  previous_register_units: number | null
  previous_status: NdpsBalanceStatus
  /** The variance this opening raised for the part of the old register's
   *  difference from the shelf its open variances did not record, or the
   *  newest open one already recording all of it; null when the old register
   *  matched or was never opened. A reconciled variance never counts. */
  variance_id: number | null
  /** True when this opening raised that variance (false when the variances
   *  still open against the old register record its whole difference). */
  variance_raised: boolean
}

export interface NdpsRegisterFilter {
  store_id?: number | null
  drug_id?: string | null
}

export function getNdpsOptions(): Promise<NdpsOptions> {
  return pharmacyStoreGet('/ndps/options')
}

export function getNdpsBalances(filter: NdpsRegisterFilter = {}): Promise<NdpsBalances> {
  return pharmacyStoreGet('/ndps/balances', {
    store_id: filter.store_id ?? undefined,
    drug_id: filter.drug_id || undefined,
  })
}

export function getNdpsRegister(
  filter: NdpsRegisterFilter & { before_id?: number | null; limit?: number } = {},
): Promise<NdpsRegisterPage> {
  return pharmacyStoreGet('/ndps/register', {
    store_id: filter.store_id ?? undefined,
    drug_id: filter.drug_id || undefined,
    before_id: filter.before_id ?? undefined,
    limit: filter.limit,
  })
}

export function listNdpsVariances(
  status: NdpsVarianceFilter = 'open',
): Promise<{ lane: NdpsLane; variances: NdpsVariance[] }> {
  return pharmacyStoreGet('/ndps/variances', { status })
}

export function openNdpsRegister(body: NdpsWitnessInput & {
  reason: string
  store_id?: number | null
  drug_id?: string | null
}): Promise<{ opened: NdpsOpening[]; balances: NdpsBalances }> {
  return pharmacyStorePost('/ndps/openings', body)
}

export function checkNdpsAgainstShelf(): Promise<{
  raised: { variance_id: number; store_id: number; drug_id: string }[]
  balances: NdpsBalances
}> {
  return pharmacyStorePost('/ndps/reconcile')
}

export function reconcileNdpsVariance(
  id: number,
  body: NdpsWitnessInput & { note: string },
): Promise<{ variance: NdpsVariance }> {
  return pharmacyStorePost(`/ndps/variances/${id}/reconcile`, body)
}

export function reportNdpsVarianceIncident(
  id: number,
): Promise<{ incident_ref: string; incident_id: number }> {
  return pharmacyStorePost(`/ndps/variances/${id}/incident`)
}

/** The register as CSV. The server audits the export before it sends it. */
export function downloadNdpsRegister(
  filter: NdpsRegisterFilter & { from?: string | null; to?: string | null } = {},
): Promise<Blob> {
  return pharmacyStoreDownload('/ndps/export.csv', {
    store_id: filter.store_id ?? undefined,
    drug_id: filter.drug_id || undefined,
    from: filter.from || undefined,
    to: filter.to || undefined,
  })
}
