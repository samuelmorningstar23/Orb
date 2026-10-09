/**
 * The front office day summary (opd-18): GET /api/frontdesk/mis/daily.
 *
 * One hospital-local day for the caller's clinic (every clinic for an account
 * with no department), in counts only: the server sends no patient id, name,
 * UHID or visit, so nothing here can name a patient, and neither can the CSV
 * this module writes from it.
 */
import { apiFetch } from './client'
import { formatClinicalDateKey, formatClinicalDateTime } from '../lib/datetime'

export interface MisWait {
  measured: number
  /** Null when no visit on the day was called in: never shown as 0. */
  median_minutes: number | null
  longest_minutes: number | null
}

export interface MisOutcome {
  total: number
  waiting: number
  in_consultation: number
  completed: number
  cancelled: number
  left_without_being_seen: number
  admitted: number
}

export interface MisDoctor extends MisOutcome {
  doctor_id: string | null
  doctor_name: string
  wait: MisWait
}

export interface MisClinic extends MisOutcome {
  department: string
  wait: MisWait
  doctors: MisDoctor[]
}

export interface MisCount {
  code: string
  label: string
  count: number
}

export interface DailyMis {
  date: string
  department: string | null
  generated_at: string
  registrations: { new: number; returning: number; not_known: number; patients: number }
  visits: {
    total: number
    by_status: Record<'waiting' | 'in_consultation' | 'completed' | 'cancelled', number>
    by_type: MisCount[]
  }
  waits: MisWait & { still_waiting: number }
  left_without_being_seen: number
  cancellations: MisCount[]
  /** `available` is false until the appointments table has its slot shape. */
  no_shows: { available: boolean; recorded: number; computed: number; total: number }
  conversions: number
  clinics: MisClinic[]
}

/** The summary for `date` (YYYY-MM-DD, hospital-local); today when omitted. */
export function fetchDailyMis(date?: string): Promise<DailyMis> {
  return apiFetch<DailyMis>(
    `/frontdesk/mis/daily${date ? `?date=${encodeURIComponent(date)}` : ''}`,
  )
}

/** Minutes as a summary shows them: "Not measured" for null, never 0. */
export function minutesText(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'Not measured'
  const shown = Number.isInteger(value) ? String(value) : value.toFixed(1)
  return `${shown} min`
}

/** One CSV cell. A value a spreadsheet would run as a formula (=, +, -, @,
 *  tab, carriage return) is prefixed with an apostrophe, and anything with a
 *  comma, quote or line break is quoted. */
export function csvCell(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') return String(value)
  let text = value
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

function row(cells: (string | number | null | undefined)[]): string {
  return cells.map(csvCell).join(',')
}

/** The day summary as CSV: the day's counts, then one row per clinic and
 *  doctor. Counts only, like the answer it is made from. */
export function dailyMisCsv(mis: DailyMis): string {
  const minutes = (v: number | null) => (v === null ? 'Not measured' : v)
  const lines = [
    row(['Front office day summary']),
    row(['Day', formatClinicalDateKey(mis.date)]),
    row(['Clinic', mis.department ?? 'All clinics']),
    row(['Made', formatClinicalDateTime(mis.generated_at)]),
    '',
    row(['Measure', 'Count']),
    row(['New registrations', mis.registrations.new]),
    row(['Returning patients', mis.registrations.returning]),
    row(['Patients with no registration time on record', mis.registrations.not_known]),
    row(['Visits', mis.visits.total]),
    ...mis.visits.by_type.map(t => row([`${t.label} visits`, t.count])),
    row(['Waiting now', mis.visits.by_status.waiting]),
    row(['In consultation now', mis.visits.by_status.in_consultation]),
    row(['Completed', mis.visits.by_status.completed]),
    row(['Cancelled', mis.visits.by_status.cancelled]),
    row(['Left without being seen', mis.left_without_being_seen]),
    row(['No-shows', mis.no_shows.available ? mis.no_shows.total : 'Not available']),
    row(['Admitted from outpatients', mis.conversions]),
    row(['Visits with a measured wait', mis.waits.measured]),
    row(['Median wait to be called (minutes)', minutes(mis.waits.median_minutes)]),
    row(['Longest wait to be called (minutes)', minutes(mis.waits.longest_minutes)]),
    '',
    row(['Cancellation reason', 'Count']),
    ...mis.cancellations.map(c => row([c.label, c.count])),
    '',
    row(['Clinic', 'Doctor', 'Visits', 'Completed', 'Cancelled', 'Left without being seen',
      'Admitted', 'Median wait (minutes)', 'Longest wait (minutes)']),
    ...mis.clinics.flatMap(c => c.doctors.map(d => row([
      c.department, d.doctor_name, d.total, d.completed, d.cancelled,
      d.left_without_being_seen, d.admitted, minutes(d.wait.median_minutes),
      minutes(d.wait.longest_minutes),
    ]))),
  ]
  return `${lines.join('\r\n')}\r\n`
}
