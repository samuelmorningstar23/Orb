/**
 * Orb, Pharmacy store API: ward returns of dispensed medicines (grn-15).
 *
 * A medicine dispensed for one patient comes back unused. The nurse records it
 * from the Deliveries tab for a patient on her own ward (ward_return.record),
 * or the pharmacist takes it in at the counter (pharmacy_store.write). It must
 * name a DELIVERED order and can never exceed what that order's dispense took
 * minus what has already come back. The units wait in a returns quarantine
 * store, never dispensable, until a pharmacist inspects them: write off, or
 * (only where the site has ruled it, GRN-RS3) back into the batch they came
 * from. A narcotic or psychotropic return comes back at the counter, with a
 * second pharmacist witnessing by signing PIN.
 *
 * Every call goes through the shared base in ./index, so a refusal arrives as
 * a PharmacyStoreApiError with the server's code, for example `over_return`,
 * `order_not_delivered`, `no_returns_store`, `store_required`,
 * `restock_not_allowed`, `restock_refused`, `same_person`,
 * `ndps_permission_needed`, `witness_required` or `audit_unavailable`.
 * Quantities are whole dispense units; money is integer paise. Another ward's
 * order or return answers 404, exactly like one that does not exist.
 */
import { newId } from '../../lib/id'
import { pharmacyStoreGet, pharmacyStorePost } from './index'

export type WardReturnStatus = 'pending_inspection' | 'restocked' | 'written_off'
export type WardReturnFilter = WardReturnStatus | ''
export type InspectionDecision = 'write_off' | 'restock'

export const WARD_RETURN_STATUS_LABELS: Record<WardReturnStatus, string> = {
  pending_inspection: 'Waiting for inspection',
  restocked: 'Back on the shelf',
  written_off: 'Written off',
}

/** What grn-16 (pharmacy charges at batch MRP) reads to credit the patient. */
export interface WardReturnCredit {
  units: number
  unit_mrp_paise: number | null
  /** Null where the batch has no MRP or pack size on file. */
  amount_paise: number | null
  basis: 'batch_mrp' | 'not_recorded'
  helix_order_id: string | null
  patient_id: string | null
}

/** Whether a return could go back on the shelf, and why not, in words. */
export interface RestockCheck {
  allowed: boolean
  code: string | null
  reasons: string[]
}

export interface WardReturn {
  id: number
  return_number: string | null
  status: WardReturnStatus
  status_label: string
  patient_id: string
  patient_name: string | null
  patient_mrn: string | null
  patient_bed: string | null
  patient_ward: string | null
  department: string | null
  helix_order_id: string
  helix_medication_id: string
  drug_name: string | null
  strength: string | null
  form: string | null
  dispense_unit: string | null
  batch_no: string | null
  expiry_date: string | null
  quantity_units: number
  seal_intact: boolean | null
  /** A narcotic or psychotropic drug. */
  controlled: boolean
  cold_chain: boolean
  original_lot_id: string | null
  original_lot_status: string | null
  quarantine_lot_id: string | null
  quarantine_units: number | null
  store_code: string | null
  store_name: string | null
  returned_by: string | null
  returned_by_name: string | null
  received_by: string
  received_by_name: string | null
  occurred_at: string | null
  created_at: string | null
  inspected_by: string | null
  inspected_by_name: string | null
  inspected_at: string | null
  inspection_note: string | null
  note: string | null
  credit: WardReturnCredit
  /** Present while the return waits for inspection. */
  restock: RestockCheck | null
}

/** One batch a delivered order's dispense took, with what can still come back. */
export interface ReturnableBatch {
  lot_id: string
  drug_id: string
  drug_name: string
  strength: string | null
  form: string | null
  dispense_unit: string | null
  batch_no: string | null
  expiry_date: string | null
  controlled: boolean
  dispensed_units: number
  returned_units: number
  returnable_units: number
}

export interface ReturnableOrder {
  id: string
  patient_id: string
  patient_name: string | null
  bed: string | null
  ward: string | null
  mrn: string | null
  dosage: string | null
  route: string | null
  frequency: string | null
  received_by: string | null
  received_by_name: string | null
  received_at: string | null
  created_at: string | null
  med_order_id: string | null
  batches: ReturnableBatch[]
}

export interface WardReturnOptions {
  stores: { code: string; name: string }[]
  restock_allowed: boolean
  /** RETURN_WRITE_OFF_NEEDS_SECOND_PERSON on the server: the person who took a
   *  return in may not write it off. Missing is read as true (the strict default). */
  write_off_needs_second_person?: boolean
  lane: 'pharmacy' | 'ward'
  may_record_controlled: boolean
  witnesses: { id: string; name: string }[]
  decisions: InspectionDecision[]
  status_labels: Record<WardReturnStatus, string>
  number_series: string
}

export interface NewWardReturn {
  helix_order_id: string
  quantity_units: number
  seal_intact?: boolean | null
  lot_id?: string | null
  store_code?: string | null
  note?: string | null
  witness_id?: string | null
  witness_pin?: string | null
}

export interface WardReturnResult {
  ward_return: WardReturn
  replayed?: boolean
}

export function getWardReturnOptions(): Promise<WardReturnOptions> {
  return pharmacyStoreGet<WardReturnOptions>('/ward-returns/options')
}

export function getReturnableOrders(params: { helix_order_id?: string } = {}):
Promise<{ orders: ReturnableOrder[]; window_days: number }> {
  return pharmacyStoreGet('/ward-returns/returnable', params)
}

export function listWardReturns(params: { status?: WardReturnFilter } = {}):
Promise<{ ward_returns: WardReturn[] }> {
  return pharmacyStoreGet('/ward-returns', params)
}

export function getWardReturn(returnId: number): Promise<WardReturnResult> {
  return pharmacyStoreGet(`/ward-returns/${encodeURIComponent(String(returnId))}`)
}

/** One key per return being recorded, reused by every retry of that return,
 *  so a lost reply never records the same units twice. */
export function newWardReturnKey(): string {
  return `ward-return-${newId()}`
}

export function recordWardReturn(body: NewWardReturn, idempotencyKey: string):
Promise<WardReturnResult> {
  return pharmacyStorePost('/ward-returns', { ...body, idempotency_key: idempotencyKey })
}

export function inspectWardReturn(
  returnId: number,
  body: { decision: InspectionDecision; reason: string; seal_intact?: boolean | null;
          witness_id?: string | null; witness_pin?: string | null },
): Promise<WardReturnResult> {
  return pharmacyStorePost(`/ward-returns/${encodeURIComponent(String(returnId))}/inspect`, body)
}

/** "3 tablets", "1 unit": the units in the drug's own dispense unit when known. */
export function returnUnitsWords(units: number, dispenseUnit?: string | null): string {
  const word = (dispenseUnit || '').trim().toLowerCase()
  if (word) return `${units} ${word}`
  return `${units} ${units === 1 ? 'unit' : 'units'}`
}
