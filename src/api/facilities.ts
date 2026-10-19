/**
 * Facility identity (multisite-2): who the hospital is on its bills, receipts
 * and printed records.
 *
 *   GET   /api/facilities                 every facility, with its identity
 *   GET   /api/facilities/{id}            one facility
 *   PATCH /api/facilities/{id}            edit identity (facility.manage)
 *   GET   /api/facilities/create-check    why a facility cannot be added
 *
 * Facility 1's identity can be read and edited whatever ORB_MULTI_SITE says.
 * The create check (and creating, closing or editing any other facility)
 * answers 404 feature_disabled while the switch is off; screens read the
 * switch from the platform flags shellConfig adopts from /api/health, never
 * from a second fetch.
 *
 * A bill or receipt that already has a number keeps the supplier details it
 * was issued with: editing identity changes only documents numbered later.
 */
import { apiFetch } from './client'

/** The identity fields an edit may change, in the order the form shows them. */
export const FACILITY_FIELDS = [
  'name', 'short_name', 'legal_name',
  'address_line1', 'address_line2', 'city', 'district', 'state', 'pincode',
  'gstin', 'pan', 'state_code',
  'phone', 'email',
  'hfr_id', 'abdm_hip_id', 'nabh_accreditation_no', 'nabh_valid_until',
  'clinical_establishment_reg_no', 'timezone',
] as const

export type FacilityField = typeof FACILITY_FIELDS[number]

/** Words for each field, matching the server's facility_identity.FIELD_LABELS. */
export const FACILITY_FIELD_LABELS: Record<FacilityField, string> = {
  name: 'Name',
  short_name: 'Short name',
  legal_name: 'Legal name',
  address_line1: 'Address line 1',
  address_line2: 'Address line 2',
  city: 'City',
  district: 'District',
  state: 'State',
  pincode: 'PIN code',
  gstin: 'GSTIN',
  pan: 'PAN',
  state_code: 'GST state code',
  phone: 'Phone',
  email: 'E-mail',
  hfr_id: 'Health facility registry ID',
  abdm_hip_id: 'ABDM health information provider ID',
  nabh_accreditation_no: 'NABH accreditation number',
  nabh_valid_until: 'NABH accreditation valid until',
  clinical_establishment_reg_no: 'Clinical establishment registration number',
  timezone: 'Timezone',
}

export type FacilityIdentityFields = { [K in FacilityField]: string | null }

export interface Facility extends FacilityIdentityFields {
  id: number
  uuid: string
  code: string
  is_active: boolean
  /** Facility 1, the site every install starts as. */
  is_main: boolean
  /** The name screens show (facility 1 falls back to the site settings name). */
  display_name: string
  /** Facility 1 only: the site settings file's name, used while Name is blank. */
  site_name: string | null
  /** The registered address on one line, or null. */
  address: string | null
  /** The name a bill prints as its supplier: the legal name, else the name. */
  supplier_name: string | null
  state_code_name: string | null
  /** Legal name (or name), address and GSTIN are all set. */
  invoice_ready: boolean
  missing_for_invoice: string[]
  /** This account may edit this facility (facility.manage, its own facility). */
  can_edit: boolean
  created_at: string | null
  created_by: string | null
  updated_at: string | null
  updated_by: string | null
  audit_epoch_id: number | null
}

export interface FacilityList {
  facilities: Facility[]
  multi_site: boolean
  multi_facility_data: boolean
}

/** One reason a facility cannot be added (or closed) yet. */
export interface FacilityReason {
  code: string
  message: string
}

export interface CreateCheck {
  allowed: boolean
  reasons: FacilityReason[]
}

/** One field the server refused, with the words to show beside it. */
export interface FacilityFieldError {
  field: string
  code: string
  message: string
}

export type FacilityIdentityPatch = Partial<Record<FacilityField, string | null>>

export function listFacilities(): Promise<FacilityList> {
  return apiFetch<FacilityList>('/facilities')
}

export function getFacility(id: number): Promise<Facility> {
  return apiFetch<Facility>(`/facilities/${id}`)
}

export function updateFacility(id: number, patch: FacilityIdentityPatch): Promise<Facility> {
  return apiFetch<Facility>(`/facilities/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })
}

export function getCreateCheck(): Promise<CreateCheck> {
  return apiFetch<CreateCheck>('/facilities/create-check')
}

function detailOf(err: unknown): Record<string, unknown> | null {
  if (!err || typeof err !== 'object') return null
  const detail = (err as { detail?: unknown }).detail
  return detail && typeof detail === 'object' && !Array.isArray(detail)
    ? detail as Record<string, unknown>
    : null
}

/** The per-field refusals of an identity edit, or [] for any other error. */
export function fieldErrorsOf(err: unknown): FacilityFieldError[] {
  const detail = detailOf(err)
  const errors = detail && Array.isArray(detail.errors) ? detail.errors : []
  return errors.flatMap(e => {
    if (!e || typeof e !== 'object') return []
    const { field, code, message } = e as Record<string, unknown>
    return typeof field === 'string' && typeof message === 'string'
      ? [{ field, code: typeof code === 'string' ? code : '', message }]
      : []
  })
}

/** True when the server answered that the multi-site switch is off. */
export function isSwitchedOff(err: unknown): boolean {
  const detail = detailOf(err)
  return (err as { status?: number } | null)?.status === 404 && detail?.error === 'feature_disabled'
}
