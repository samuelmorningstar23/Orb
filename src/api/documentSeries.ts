/**
 * Document numbering per facility (multisite-7a): each facility numbers its
 * own bills, receipts, claims and other documents from its own series.
 *
 *   GET /api/facilities/{id}/document-series          a facility's series
 *   PUT /api/facilities/{id}/document-series/{code}   set up or edit one
 *
 * Both answer 404 feature_disabled while the multi-site switch is off (the
 * Facilities screen shows this section only with it on). A refused prefix
 * comes back as 400 with per-field errors, read with fieldErrorsOf from
 * ./facilities.
 */
import { apiFetch } from './client'

export interface DocumentSeries {
  id: number
  facility_id: number
  series_code: string
  document_type: string
  /** What the series numbers, in words ("Receipts"). */
  label: string
  prefix: string
  suffix: string
  pad_width: number
  next_number: number
  financial_year: string | null
  /** A yearly series restarts each financial year and carries the year. */
  resets_yearly: boolean
  check_digit: string | null
  /** The hospital has said this is its real series, not a placeholder. */
  is_configured: boolean
  /** One series for the whole group (hospital numbers); never per facility. */
  group_wide: boolean
  /** The next number as it would print. Nothing is allocated by reading it. */
  next_number_preview: string
  updated_at: string | null
}

export interface SeriesToAdd {
  series_code: string
  document_type: string
  label: string
}

export interface DocumentSeriesList {
  facility_id: number
  series: DocumentSeries[]
  /** Series facility 1 has that this facility does not yet. */
  can_add: SeriesToAdd[]
  /** This account may change this facility's numbering. */
  can_edit: boolean
}

export interface DocumentSeriesChange {
  prefix?: string
  suffix?: string
  pad_width?: number
  is_configured?: boolean
}

export interface DocumentSeriesSaved {
  series: DocumentSeries
  created: boolean
  changed: string[]
}

export function listDocumentSeries(facilityId: number): Promise<DocumentSeriesList> {
  return apiFetch<DocumentSeriesList>(`/facilities/${facilityId}/document-series`)
}

export function saveDocumentSeries(
  facilityId: number, seriesCode: string, change: DocumentSeriesChange,
): Promise<DocumentSeriesSaved> {
  return apiFetch<DocumentSeriesSaved>(
    `/facilities/${facilityId}/document-series/${encodeURIComponent(seriesCode)}`,
    { method: 'PUT', body: JSON.stringify(change) },
  )
}
