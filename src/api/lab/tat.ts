/**
 * Orb, Laboratory API: turnaround times (lis-18).
 *
 * The dashboard covers whole hospital-local days. The read names patients on
 * its overdue list and is audited, so a screen loads it when asked and never
 * polls. A figure the server cannot state is null, never 0: a group with no
 * target says 'No target'.
 */
import { labGet } from './index'

export interface IntervalFigure {
  n: number
  median: number | null
  p90: number | null
}

export type IntervalKey =
  | 'order_to_collection'
  | 'collection_to_receipt'
  | 'receipt_to_report'
  | 'order_to_report'

export interface TargetFigure {
  with_target: number
  within: number
  no_target: number
  unreadable: number
  percent: number | null
  state: 'reported' | 'no_target'
  text: string
}

export interface TatFigures {
  count: number
  intervals: Record<IntervalKey, IntervalFigure>
  target: TargetFigure
  out_of_order: number
}

export interface TatGroup extends TatFigures {
  section_id: string | null
  section_name: string
  priority: string
  priority_text: string
}

export interface TatDay extends TatFigures {
  day: string
}

export interface OverdueTest {
  item_id: number
  specimen_id: string
  order_id: string
  priority: string
  priority_text: string
  status: string
  test: { id: string; name: string; short_name: string }
  section_name: string
  patient: { id: string; name: string | null; mrn: string | null; ward: string | null; bed: string | null }
  received_at: string | null
  target_minutes: number
  waiting_minutes: number
  over_by_minutes: number
}

export interface CriticalSummary {
  raised: number
  acknowledged: number
  by_phone_log: number
  by_ward_read_back: number
  open: number
  oldest_open_minutes: number | null
  median_minutes: number | null
  p90_minutes: number | null
  state: 'reported' | 'open_only' | 'none'
}

export interface TatDashboard {
  period: { first_day: string; last_day: string; days: number; start: string; end: string; timezone: string }
  intervals: { key: IntervalKey; label: string }[]
  target_interval: IntervalKey
  overall: TatFigures
  by_section_priority: TatGroup[]
  by_day: TatDay[]
  simulated_excluded: number
  critical: CriticalSummary
  overdue: { items: OverdueTest[]; total: number; in_lab: number; in_lab_without_target: number }
  exceptions: { open: number; by_kind: { kind: string; label: string; count: number }[] }
  limits: { max_period_days: number; overdue_cap: number }
  scope: { department: string | null; hospital_wide: boolean }
}

export interface TatPeriod {
  from?: string
  to?: string
}

export function getLabTat(period: TatPeriod = {}): Promise<TatDashboard> {
  return labGet<TatDashboard>('/tat', { from: period.from, to: period.to })
}

export interface TatExport {
  filename: string
  content_type: string
  rows: number
  csv: string
}

/** The period as CSV text (audited as an export on the server). */
export function exportLabTat(period: TatPeriod = {}): Promise<TatExport> {
  return labGet<TatExport>('/tat/export', { from: period.from, to: period.to })
}

/** Minutes in words: '17.5 min', '45 min', '2 h 5 min', or 'Not recorded'.
 *  Under an hour the server's one decimal place is kept. */
export function minutesText(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || Number.isNaN(minutes)) return 'Not recorded'
  if (minutes < 60) {
    const tenths = Math.round(minutes * 10) / 10
    return `${Number.isInteger(tenths) ? tenths.toFixed(0) : tenths.toFixed(1)} min`
  }
  const whole = Math.round(minutes)
  const hours = Math.floor(whole / 60)
  const rest = whole % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}
