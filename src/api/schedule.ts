/**
 * Orb, clinic timings API: the /api/schedule routes (opd-15).
 *
 * A clinic session is a doctor's weekly outpatient clinic (weekday, hours in
 * HOSPITAL-LOCAL wall-clock time, slot length, room, the weeks it runs). An
 * exception is a day it does not run. Slots are computed by the server from
 * those two, in hospital-local time, and are never stored; this client only
 * asks for them.
 *
 * TIMES AND DATES ARE NOT INSTANTS. `start_time`/`end_time` are "HH:MM" wall
 * clock at the hospital and render through formatClinicalClock; `exception_date`,
 * `valid_from` and the availability `date` are hospital calendar days
 * ("YYYY-MM-DD") and render through formatClinicalDateKey. Only a slot's
 * `starts_at`/`ends_at` are UTC instants.
 *
 * Behind the 'registration' feature: every call answers 404 with
 * `detail.error === 'feature_disabled'` while it is off, and
 * `isFeatureDisabled(err)` tells a screen so.
 *
 * Writes need schedule.manage (the administrator); reads need registration.read
 * or patient.read_all. The server decides; the screen only hides what it
 * cannot do.
 */
import { apiFetch } from './client'

export type Weekday = 0 | 1 | 2 | 3 | 4 | 5 | 6

/** Monday first, as the server numbers them (Python's date.weekday()). */
export const WEEKDAY_LABELS: readonly string[] = [
  'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday',
]

export interface ClinicDoctor {
  id: string
  name: string
  department: string
}

export interface ClinicSession {
  id: string
  doctor_id: string
  doctor_name: string | null
  /** False when the doctor's account has been deactivated: the session then
   *  offers no slots until it is retired or the account is restored. */
  doctor_active: boolean | null
  department: string
  weekday: Weekday
  weekday_name: string
  start_time: string
  end_time: string
  slot_minutes: number
  capacity_per_slot: number
  slot_count: number
  room: string | null
  valid_from: string | null
  valid_to: string | null
  is_active: boolean
  notes: string | null
  row_version: number
  created_by: string | null
  created_at: string
  updated_by: string | null
  updated_at: string | null
  retired_by: string | null
  retired_at: string | null
  retired_reason: string | null
}

export interface SessionException {
  id: string
  doctor_id: string
  doctor_name: string | null
  /** Null means every session the doctor holds that day. */
  session_id: string | null
  exception_date: string
  kind: string
  reason: string
  created_by: string | null
  created_at: string
  withdrawn_by: string | null
  withdrawn_at: string | null
  withdrawn_reason: string | null
  is_withdrawn: boolean
}

export interface Slot {
  start: string
  end: string
  starts_at: string
  ends_at: string
  /** The slot started before now (hospital time). */
  past: boolean
  capacity: number
}

export interface DaySession {
  session_id: string
  doctor_id: string
  doctor_name: string | null
  department: string
  room: string | null
  start_time: string
  end_time: string
  slot_minutes: number
  capacity_per_slot: number
  cancelled: boolean
  cancel_reason: string | null
  exception_id: string | null
  slots: Slot[]
}

export interface AvailabilityDay {
  date: string
  weekday: Weekday
  weekday_name: string
  sessions: DaySession[]
}

export interface Availability {
  from: string
  to: string
  hospital_today: string
  timezone: string
  days: AvailabilityDay[]
}

export interface NewSession {
  doctor_id: string
  department?: string
  weekday: Weekday
  start_time: string
  end_time: string
  slot_minutes: number
  capacity_per_slot?: number
  room?: string | null
  valid_from?: string | null
  valid_to?: string | null
  notes?: string | null
}

export type SessionChanges = Partial<Omit<NewSession, 'doctor_id' | 'department'>>

export interface NewException {
  exception_date: string
  reason: string
  /** One session; omit (with doctor_id) for the doctor's whole day. */
  session_id?: string
  doctor_id?: string
}

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const q = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    q.set(key, String(value))
  }
  const text = q.toString()
  return text ? `?${text}` : ''
}

/** The registration feature is switched off at this site. */
export function isFeatureDisabled(err: unknown): boolean {
  const e = err as { status?: number; detail?: unknown } | null
  if (!e || e.status !== 404) return false
  const detail = e.detail as { error?: unknown } | null
  return !!detail && typeof detail === 'object' && detail.error === 'feature_disabled'
}

export function fetchClinicDoctors(department?: string): Promise<{ doctors: ClinicDoctor[] }> {
  return apiFetch(`/schedule/doctors${query({ department })}`)
}

export function fetchClinicSessions(
  opts: { doctorId?: string; department?: string; includeRetired?: boolean } = {},
): Promise<{ sessions: ClinicSession[] }> {
  return apiFetch(`/schedule/sessions${query({
    doctor_id: opts.doctorId,
    department: opts.department,
    include_retired: opts.includeRetired ? true : undefined,
  })}`)
}

export function createClinicSession(body: NewSession): Promise<ClinicSession> {
  return apiFetch('/schedule/sessions', { method: 'POST', body: JSON.stringify(body) })
}

export function updateClinicSession(
  id: string,
  rowVersion: number,
  changes: SessionChanges,
): Promise<ClinicSession> {
  return apiFetch(`/schedule/sessions/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify({ ...changes, row_version: rowVersion }),
  })
}

export function retireClinicSession(id: string, reason: string): Promise<ClinicSession> {
  return apiFetch(`/schedule/sessions/${encodeURIComponent(id)}/retire`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function fetchSessionExceptions(
  opts: { dateFrom?: string; dateTo?: string; doctorId?: string; includeWithdrawn?: boolean } = {},
): Promise<{ exceptions: SessionException[] }> {
  return apiFetch(`/schedule/exceptions${query({
    date_from: opts.dateFrom,
    date_to: opts.dateTo,
    doctor_id: opts.doctorId,
    include_withdrawn: opts.includeWithdrawn ? true : undefined,
  })}`)
}

export function addSessionException(body: NewException): Promise<SessionException> {
  return apiFetch('/schedule/exceptions', { method: 'POST', body: JSON.stringify(body) })
}

export function withdrawSessionException(id: string, reason: string): Promise<SessionException> {
  return apiFetch(`/schedule/exceptions/${encodeURIComponent(id)}/withdraw`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function fetchAvailability(
  opts: { date?: string; days?: number; doctorId?: string; department?: string } = {},
): Promise<Availability> {
  return apiFetch(`/schedule/availability${query({
    date: opts.date,
    days: opts.days,
    doctor_id: opts.doctorId,
    department: opts.department,
  })}`)
}
