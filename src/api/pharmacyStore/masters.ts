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
 *
 * DRUG IDENTITY (grn-6). A drug's name, generic name, form and strength are
 * added as a draft that a second pharmacist confirms, corrected only through a
 * second pharmacist while nothing names the drug, and otherwise retired and
 * replaced. The screening preview (`previewDrug`) is the server's own answer
 * from the safety screens; this file never classifies a drug itself.
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
  /** Words for identity_status, from the server (grn-6). */
  identity_label?: string
  /** True only for a confirmed drug: the one state anything may order. */
  orderable_identity?: boolean
}

/** The four fields that decide how a drug is screened (grn-6). */
export interface DrugIdentity {
  name: string
  generic_name: string
  form: string
  strength: string
}

export type IdentityStatus = 'draft' | 'active' | 'retired'

/** Plain words for a drug's identity status, sentence case. */
export const IDENTITY_STATUS_LABELS: Record<IdentityStatus, string> = {
  draft: 'Waiting for a second pharmacist',
  active: 'Confirmed for ordering',
  retired: 'Retired',
}

/** Another drug a new identity would repeat. */
export interface IdentityMatch {
  id: string
  name: string
  generic_name: string | null
  form: string | null
  strength: string | null
  identity_status: IdentityStatus
}

/** What the safety screens make of an identity: the server's answer. */
export interface DrugScreening {
  screened_as: string
  classes: string[]
  class_labels: string[]
  /** null when the interaction table could not be read. */
  interaction_recognised: boolean | null
}

export interface DrugPreview {
  identity: DrugIdentity
  screening: DrugScreening
  same_name: IdentityMatch[]
  same_identity: IdentityMatch[]
}

export interface IdentityRequest extends ClassificationRequest {
  identity: DrugIdentity | null
  previous_identity: DrugIdentity | null
  screening: DrugScreening | null
  requested_by_loader?: boolean
  drug_identity_status?: IdentityStatus | null
}

export interface NewDrugInput extends DrugIdentity {
  /** The pharmacist says this is another brand of a medicine already held. */
  another_brand?: boolean
  note?: string
}

export interface NewDrugResult {
  drug: PharmacyDrug
  request: IdentityRequest
  screening: DrugScreening
}

export interface IdentityCorrectionInput extends DrugIdentity {
  reason: string
  another_brand?: boolean
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

// ── Drug identity (grn-6) ────────────────────────────────────────────────────

/** What the safety screens will make of this identity, and any drug it would
 *  repeat. Saves nothing. */
export function previewDrug(identity: DrugIdentity) {
  return pharmacyStorePost<DrugPreview>('/drugs/preview', identity)
}

/** Add a drug as a draft; a different pharmacist confirms it. */
export function addDrug(input: NewDrugInput) {
  return pharmacyStorePost<NewDrugResult>('/drugs', input)
}

export function listIdentityRequests(decision: RequestDecision | '' = 'pending') {
  return pharmacyStoreGet<{ requests: IdentityRequest[] }>('/identity-requests', { decision })
}

/** Confirming names the identity the pharmacist checked (`expected`); the
 *  server refuses if the drug has moved since. Refusing needs a reason. */
export function decideIdentityRequest(
  requestId: number,
  decision: 'approved' | 'refused',
  options: { expected?: DrugIdentity; reason?: string } = {},
) {
  return pharmacyStorePost<{ request: IdentityRequest; drug: PharmacyDrug }>(
    `/identity-requests/${requestId}/decision`, { decision, ...options },
  )
}

export function withdrawIdentityRequest(requestId: number) {
  return pharmacyStorePost<{ request: IdentityRequest; drug: PharmacyDrug }>(
    `/identity-requests/${requestId}/withdraw`,
  )
}

export function requestIdentityCorrection(drugId: string, input: IdentityCorrectionInput) {
  return pharmacyStorePost<{ request: IdentityRequest; drug: PharmacyDrug }>(
    `/drugs/${encodeURIComponent(drugId)}/identity-correction`, input,
  )
}

export function retireDrug(drugId: string, input: { reason: string; replacement_drug_id?: string }) {
  return pharmacyStorePost<{ drug: PharmacyDrug }>(
    `/drugs/${encodeURIComponent(drugId)}/retire`, input,
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
