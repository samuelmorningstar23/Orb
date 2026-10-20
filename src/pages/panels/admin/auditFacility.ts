/**
 * The audit screens' facility helpers (multisite-11). The server adds a
 * facility to each audit row, and lists the facilities the reader may filter
 * by, only while more than one facility is on record; with one facility the
 * screens look exactly as before.
 */
import { apiFetch, getAuditLogs, type AuditFilters, type AuditLog } from '../../../api/client'

/** A facility as an audit row names it. */
export interface AuditFacility {
  id: number
  code: string | null
  name: string | null
}

/** An audit row with the facility it belongs to: null for a row that names
 *  none, which only an administrator with group-wide access is shown. */
export type AuditLogRow = AuditLog & { facility?: AuditFacility | null }

/** One page of the trail; `facilities` lists the ones the reader may filter by. */
export interface AuditPage {
  logs: AuditLogRow[]
  total: number
  limit: number
  offset: number
  facilities?: AuditFacility[]
}

/** The facility in words for the audit screens. */
export function facilityLabel(facility: AuditFacility | null | undefined): string {
  if (!facility) return 'No facility recorded'
  const name = facility.name || facility.code || `Facility ${facility.id}`
  return facility.code && facility.name ? `${name} (${facility.code})` : name
}

const QUERY_KEYS: (keyof AuditFilters)[] = [
  'correlation_id', 'patient_id', 'action_type', 'event_category', 'outcome',
  'start_date', 'end_date', 'limit', 'offset',
]

/** A page of the trail. Without a chosen facility this is exactly
 *  getAuditLogs; a chosen one only narrows (the server answers 404 for a
 *  facility outside the reader's). */
export function fetchAuditPage(filters: AuditFilters, facility?: number): Promise<AuditPage> {
  if (facility === undefined) return getAuditLogs(filters)
  const params = new URLSearchParams()
  for (const key of QUERY_KEYS) {
    const value = filters[key]
    if (value !== undefined && value !== null && value !== '' && value !== 0) {
      params.set(key, String(value))
    }
  }
  params.set('facility', String(facility))
  return apiFetch<AuditPage>(`/audit/logs?${params.toString()}`)
}
