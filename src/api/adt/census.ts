/**
 * Orb, Beds and admissions API: the daily census (adt-13a).
 *
 *   GET /api/adt/census?from=&to=&ward_id=      per ward and hospital-local day
 *   GET /api/adt/census.csv?from=&to=&ward_id=  the same as a spreadsheet
 *
 * COUNTS ONLY, NO PATIENT. Midnight census, admissions, transfers in and out,
 * ends by type, deaths, inpatient days, licensed and available beds,
 * occupancy (the NABH figure is the midnight census over available beds; the
 * other two are shown beside it), ALOS and return to intensive care.
 *
 * INSUFFICIENT DATA IS NOT ZERO. A day before a ward's ledger start comes
 * back with status 'insufficient_data' and a reason, and none of the counts:
 * a screen prints the reason, never a 0. Every answer carries `methods`, the
 * server's words for how each figure is counted, which a screen prints as
 * given.
 *
 * Dates are hospital-local day keys, 'YYYY-MM-DD', inclusive. With none given
 * the server answers the seven days ending yesterday.
 */
import { ApiIdentityChangedError, apiFetchBlob } from '../client'
import { ADT_API_PREFIX, adtGet, adtQuery, toAdtError } from './index'

export type EndType =
  | 'routine' | 'referred_out' | 'transferred_out' | 'dama' | 'lama' | 'absconded' | 'death'
  | 'not_recorded'

/** The order a screen lists ends in. */
export const CENSUS_END_TYPES: readonly EndType[] = [
  'routine', 'referred_out', 'transferred_out', 'dama', 'lama', 'absconded', 'death',
  'not_recorded',
]

export interface CensusOccupancy {
  /** Midnight census over available beds, per cent (the NABH figure). */
  nabh: number | null
  /** Midnight census over licensed beds, per cent. */
  licensed: number | null
  /** Inpatient days over available beds, per cent. */
  hours: number | null
}

export interface CensusIcu {
  transfers_out: number
  returns_48h: number
  pending: number
  /** On totals only: returns over moves out whose 48 hours have passed. */
  return_rate?: number | null
}

/** One ward (or the hospital) on one day. Counts are present only when the
 *  status is 'complete'. */
export interface CensusDay {
  date: string
  status: 'complete' | 'insufficient_data'
  reason?: string
  midnight_census?: number
  admissions?: number
  transfers_in?: number
  transfers_out?: number
  ends?: Partial<Record<EndType, number>>
  deaths?: number
  inpatient_days?: number
  stays?: number
  licensed_beds?: number
  available_beds?: number
  occupancy?: CensusOccupancy
  /** Hospital rows: patients present at midnight not on a ward in bed setup. */
  not_on_a_ward?: number
  icu?: CensusIcu
}

export interface CensusTotals {
  status: 'complete' | 'partial' | 'insufficient_data'
  complete_days: number
  insufficient_days: number
  midnight_census_total?: number
  average_midnight_census?: number
  admissions?: number
  transfers_in?: number
  transfers_out?: number
  ends?: Partial<Record<EndType, number>>
  deaths?: number
  inpatient_days?: number
  licensed_bed_days?: number
  available_bed_days?: number
  occupancy?: CensusOccupancy
  stays_ended?: number
  alos_days?: number | null
  icu?: CensusIcu
}

export interface CensusWard {
  id: string
  code: string
  name: string | null
  department: string | null
  ward_type: string | null
  is_active: boolean
  icu: boolean
  ledger_complete_from: string | null
  /** The first day this ward is counted (a day key), or null if never. */
  first_complete_day: string | null
  days: CensusDay[]
  totals: CensusTotals
}

export interface CensusWardChoice {
  id: string
  code: string
  name: string | null
  department: string | null
}

export interface Census {
  generated_at: string
  from: string
  to: string
  timezone: string
  department: string | null
  ward_id: string | null
  ward_choices: CensusWardChoice[]
  wards: CensusWard[]
  /** Present for an account with no department and no ward chosen. */
  hospital: { days: CensusDay[]; totals: CensusTotals; ended_without_time?: number } | null
  /** Why there is no hospital block, for a department account. */
  hospital_hidden?: string
  end_type_words: Record<string, string>
  methods: Record<string, string>
}

export interface CensusParams {
  from?: string | null
  to?: string | null
  wardId?: string | null
}

function params({ from, to, wardId }: CensusParams = {}) {
  return { from: from || undefined, to: to || undefined, ward_id: wardId || undefined }
}

/** The census. Throws AdtApiError; a refused window carries its code
 *  (bad_date, day_not_ended, bad_window, window_too_long). */
export function getCensus(query: CensusParams = {}): Promise<Census> {
  return adtGet<Census>('/census', params(query))
}

/** The CSV uses the shared refresh and account boundary; refusals stay typed. */
export async function downloadCensusCsv(query: CensusParams = {}): Promise<Blob> {
  try {
    return await apiFetchBlob(`${ADT_API_PREFIX}/census.csv${adtQuery(params(query))}`)
  } catch (failure) {
    if (failure instanceof ApiIdentityChangedError) throw failure
    throw toAdtError(failure)
  }
}

/** The file name the CSV is saved under. */
export function censusFileName(from: string, to: string): string {
  return `census-${from}-to-${to}.csv`
}
