/**
 * Orb, Pharmacy store API: masters (grn-5).
 *
 * The drug master's commercial and regulatory fields, classification with its
 * two-person downgrade, pharmacy stores, par levels and vendors with their drug
 * licences. Every call goes through the shared base in ./index, so a refusal
 * arrives as a PharmacyStoreApiError carrying the server's code (for example
 * `identity_locked`, `vault_stock`, `same_person`, `store_holds_stock`).
 *
 * The server owns every rule. This file only names the calls and their shapes:
 * whether a classification change applies now or waits for a second approver
 * is the server's answer (`applied` and `requested`), never worked out here.
 */
import {
  pharmacyStoreGet,
  pharmacyStorePatch,
  pharmacyStorePost,
  pharmacyStorePut,
} from './index'

export interface MastersOption {
  value: string
  label: string
}

export interface MastersOptions {
  drug_schedules: MastersOption[]
  ndps_categories: MastersOption[]
  storage_conditions: MastersOption[]
  store_kinds: MastersOption[]
  not_classified: string
  not_configured: string
}

export interface PharmacyDrug {
  id: string
  name: string
  generic_name: string | null
  form: string | null
  strength: string | null
  pack_size_units: number | null
  purchase_uom: string | null
  dispense_unit: string | null
  hsn_code: string | null
  gst_rate_bp: number | null
  manufacturer: string | null
  drug_schedule: string | null
  ndps_category: string | null
  is_essential_narcotic: number
  storage_condition: string | null
  reorder_level_units: number | null
  reorder_qty_units: number | null
  lead_time_days: number | null
  is_active: number
  identity_status: string
  updated_by: string | null
  updated_at: string | null
  schedule_label: string
  ndps_label: string
  storage_label: string
  gst_label: string
  pending_requests: number
}

/** The fields the write tier may change on a drug. Identity is not here. */
export interface DrugFieldChanges {
  pack_size_units?: number | null
  purchase_uom?: string | null
  dispense_unit?: string | null
  hsn_code?: string | null
  gst_rate_bp?: number | null
  manufacturer?: string | null
  storage_condition?: string | null
  reorder_level_units?: number | null
  reorder_qty_units?: number | null
  lead_time_days?: number | null
  is_active?: boolean
}

export interface ClassificationChange {
  drug_schedule?: string
  ndps_category?: string
  is_essential_narcotic?: boolean
  reason?: string
}

export type RequestDecision = 'pending' | 'approved' | 'refused' | 'withdrawn'

export interface ClassificationRequest {
  id: number
  helix_medication_id: string
  change_kind: string
  field: string
  from_value: string | null
  to_value: string | null
  reason: string | null
  requested_by: string
  requested_at: string
  decision: RequestDecision
  decided_by: string | null
  decided_at: string | null
  decision_reason: string | null
  drug_name?: string | null
  requested_by_name?: string | null
  decided_by_name?: string | null
}

export interface ClassificationResult {
  drug: PharmacyDrug
  applied: string[]
  requested: ClassificationRequest[]
}

export interface PharmacyStoreRow {
  id: number
  code: string
  name: string
  pharmacy_kind: string
  kind_label: string
  department: string | null
  ward: string | null
  incharge_user_id: string | null
  is_active: number
  units_on_hand: number
}

export interface AdoptionResult {
  store_id: number
  adopted: string[]
  merged: { from: string; into: string; units: number }[]
  conflicts: { lot_ids: string[]; reason: string }[]
}

export interface StoreInput {
  code: string
  name: string
  pharmacy_kind: string
  department?: string
  ward?: string
}

export interface StoreUpdate {
  name?: string
  department?: string
  ward?: string
  is_active?: boolean
}

export interface ParLevel {
  id: number
  store_id: number
  store_code: string
  store_name: string
  helix_medication_id: string
  drug_name: string | null
  par_units: number
  reorder_level_units: number | null
  max_units: number | null
  is_active: number
}

export interface ParLevelInput {
  store_id: number
  helix_medication_id: string
  par_units: number
  reorder_level_units?: number | null
  max_units?: number | null
}

export type LicenceStatus = 'none' | 'expired' | 'expiring' | 'valid'

export interface PharmacyVendor {
  id: number
  code: string
  name: string
  vendor_type: string
  gstin: string | null
  contact_name: string | null
  contact_phone: string | null
  is_active: number
  is_blacklisted: number
  drug_licence_numbers: string | null
  drug_licence_valid_to: string | null
  ndps_licence_number: string | null
  licence_status: LicenceStatus
}

export interface VendorInput {
  code: string
  name: string
  vendor_type?: string
  gstin?: string
  contact_name?: string
  contact_phone?: string
  drug_licence_numbers?: string
  drug_licence_valid_to?: string
  ndps_licence_number?: string
}

export interface LicenceInput {
  drug_licence_numbers?: string
  drug_licence_valid_to?: string
  ndps_licence_number?: string
}

/** Words for a vendor's drug licence status, as the screen shows them. */
export const LICENCE_STATUS_LABELS: Record<LicenceStatus, string> = {
  none: 'No drug licence on file',
  expired: 'Licence expired',
  expiring: 'Licence expiring soon',
  valid: 'Licence valid',
}

/** Words for the classification fields a request can name. */
export const CLASSIFICATION_FIELD_LABELS: Record<string, string> = {
  drug_schedule: 'Schedule',
  ndps_category: 'NDPS class',
  is_essential_narcotic: 'Essential narcotic',
}

// ── Drugs ────────────────────────────────────────────────────────────────────

export function listDrugs(params: { search?: string } = {}) {
  return pharmacyStoreGet<{ drugs: PharmacyDrug[]; options: MastersOptions }>('/drugs', params)
}

export function getDrug(drugId: string) {
  return pharmacyStoreGet<{ drug: PharmacyDrug; requests: ClassificationRequest[] }>(
    `/drugs/${encodeURIComponent(drugId)}`,
  )
}

export function updateDrug(drugId: string, changes: DrugFieldChanges) {
  return pharmacyStorePatch<{ drug: PharmacyDrug }>(`/drugs/${encodeURIComponent(drugId)}`, changes)
}

export function classifyDrug(drugId: string, change: ClassificationChange) {
  return pharmacyStorePost<ClassificationResult>(
    `/drugs/${encodeURIComponent(drugId)}/classification`, change,
  )
}

export function listClassificationRequests(decision: RequestDecision | '' = 'pending') {
  return pharmacyStoreGet<{ requests: ClassificationRequest[] }>(
    '/classification-requests', { decision },
  )
}

export function decideClassificationRequest(
  requestId: number, decision: 'approved' | 'refused', reason?: string,
) {
  return pharmacyStorePost<{ request: ClassificationRequest }>(
    `/classification-requests/${requestId}/decision`, { decision, reason },
  )
}

export function withdrawClassificationRequest(requestId: number) {
  return pharmacyStorePost<{ request: ClassificationRequest }>(
    `/classification-requests/${requestId}/withdraw`,
  )
}

// ── Stores ───────────────────────────────────────────────────────────────────

export function listPharmacyStores() {
  return pharmacyStoreGet<{ stores: PharmacyStoreRow[]; legacy_lots: number }>('/stores')
}

export function createPharmacyStore(input: StoreInput) {
  return pharmacyStorePost<{ store: PharmacyStoreRow; adoption: AdoptionResult | null }>(
    '/stores', input,
  )
}

export function updatePharmacyStore(code: string, update: StoreUpdate) {
  return pharmacyStorePut<{ store: PharmacyStoreRow; adoption: AdoptionResult | null }>(
    `/stores/${encodeURIComponent(code)}`, update,
  )
}

// ── Par levels ───────────────────────────────────────────────────────────────

export function listParLevels(storeId?: number) {
  return pharmacyStoreGet<{ par_levels: ParLevel[] }>('/par-levels', { store_id: storeId })
}

export function saveParLevel(input: ParLevelInput) {
  return pharmacyStorePut<{ par_level: ParLevel }>('/par-levels', input)
}

// ── Vendors ──────────────────────────────────────────────────────────────────

export function listPharmacyVendors(params: { search?: string } = {}) {
  return pharmacyStoreGet<{ vendors: PharmacyVendor[] }>('/vendors', params)
}

export function createPharmacyVendor(input: VendorInput) {
  return pharmacyStorePost<{ vendor: PharmacyVendor }>('/vendors', input)
}

export function setVendorDrugLicence(code: string, input: LicenceInput) {
  return pharmacyStorePut<{ vendor: PharmacyVendor }>(
    `/vendors/${encodeURIComponent(code)}/drug-licence`, input,
  )
}
