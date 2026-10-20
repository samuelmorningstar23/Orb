/**
 * Price lists per facility (multisite-13). A price list applies to the whole
 * group or to one facility, and each facility may have a default of its own;
 * a patient is priced on their facility's default, else the group's.
 *
 *   GET /api/billing/facilities/{id}/price-lists          what applies there
 *   PUT /api/billing/facilities/{id}/default-price-list   choose its default
 *   PUT /api/billing/tariff-plans/{id}/applies-to         one facility or the group
 *
 * All three answer 404 feature_disabled while the multi-site switch is off
 * (the Facilities screen shows the section only with it on), and 404
 * module_disabled where billing is not run. A refusal comes back as
 * {error, message} in the error's detail; show the message.
 */
import { apiFetch } from './client'

export interface PriceList {
  id: number
  code: string
  name: string
  plan_type: string
  is_default: boolean
  is_active: boolean
  /** null: the whole group. */
  facility_id: number | null
  /** "Whole group", or the facility's name. */
  applies_to: string
  /** Payers that price on this list; such a list stays with the whole group. */
  used_by_payers: number
}

/** Where a facility's default came from. */
export type DefaultSource = 'facility' | 'group' | 'first_active' | 'none'

export interface FacilityPriceLists {
  facility: { id: number; code: string | null; name: string; is_active: boolean }
  default_plan: PriceList | null
  default_source: DefaultSource
  /** The lists kept for this facility, then the group's. */
  plans: PriceList[]
  /** This account may change price lists here. */
  can_edit: boolean
  /** This account reaches the whole group (may move lists in and out of it). */
  group_wide: boolean
}

export interface FacilityPriceListsSaved extends FacilityPriceLists {
  changed: boolean
  /** A group list was kept for this facility to become its default. */
  moved: boolean
}

export function getFacilityPriceLists(facilityId: number): Promise<FacilityPriceLists> {
  return apiFetch<FacilityPriceLists>(`/billing/facilities/${facilityId}/price-lists`)
}

/** null: the facility uses the group's default. */
export function setFacilityDefaultPriceList(
  facilityId: number, tariffPlanId: number | null,
): Promise<FacilityPriceListsSaved> {
  return apiFetch<FacilityPriceListsSaved>(
    `/billing/facilities/${facilityId}/default-price-list`,
    { method: 'PUT', body: JSON.stringify({ tariff_plan_id: tariffPlanId }) },
  )
}

/** null: the whole group. */
export function setPriceListAppliesTo(
  tariffPlanId: number, facilityId: number | null,
): Promise<{ plan: PriceList; changed: boolean }> {
  return apiFetch<{ plan: PriceList; changed: boolean }>(
    `/billing/tariff-plans/${tariffPlanId}/applies-to`,
    { method: 'PUT', body: JSON.stringify({ facility_id: facilityId }) },
  )
}

/** True when the section should not show at all: the switch is off, billing
 *  is not run here, or this account may not read prices. */
export function isUnavailable(err: unknown): boolean {
  const status = (err as { status?: number } | null)?.status
  return status === 404 || status === 403
}

/** The words of a refusal ({error, message} in the detail), else `fallback`. */
export function refusalText(err: unknown, fallback: string): string {
  const detail = (err as { detail?: unknown } | null)?.detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message.trim()) return message
  }
  return err instanceof Error && err.message ? err.message : fallback
}
