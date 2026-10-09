/**
 * Orb, the front desk's appointments (opd-16): the /api/appointments routes the
 * Appointments day view uses.
 *
 * A booking may take a slot of a doctor's clinic session (migration 57's clinic
 * timings): the server checks the slot exists, has not started and is not full,
 * and holds one place for the patient. Check-in opens the patient's OPD visit
 * with a token, after the identity check (ruling OPD-S1: what the patient
 * STATES is compared on the server, which answers match or no match only). A
 * reschedule is a new booking that points at the old one; the old one is kept.
 *
 * DATES AND TIMES ARE NOT INSTANTS. `scheduled_date` is a hospital calendar day
 * ("YYYY-MM-DD", render through formatClinicalDateKey) and `scheduled_time` a
 * hospital wall-clock time ("HH:MM", render through formatClinicalClock). Only
 * the slot's `starts_at`/`ends_at` and the stamps are UTC instants.
 *
 * Behind the 'opd' feature: the day, check-in, no-show and reschedule routes
 * answer 404 with detail.error 'feature_disabled' while it is off. The server
 * decides who may do what; a screen only hides what a role cannot do.
 * Uses the shared apiFetch, so src/api/client.ts is untouched.
 */
import { apiFetch } from './client'
import type { OpdVisit } from './frontdesk'
import type { StatedIdentity } from './registration'

export type AppointmentStatus = 'booked' | 'cancelled' | 'completed' | 'checked_in' | 'no_show'

export interface DeskAppointment {
  id: string
  patient_id: string
  patient_name: string | null
  patient_uhid: string | null
  patient_age: number | null
  patient_sex: string | null
  /** The patient has an active admission somewhere. */
  inpatient_now: boolean
  scheduled_date: string
  scheduled_time: string
  appointment_type: string
  appointment_type_key: string
  appointment_type_label: string
  reason: string | null
  status: AppointmentStatus
  /** On the day view: 'no_show' for a booking still open after its clinic
   *  session ended. Computed by the server on read; the record says 'booked'. */
  effective_status?: AppointmentStatus
  no_show_computed?: boolean
  origin: 'staff' | 'patient'
  department: string | null
  doctor_id: string | null
  doctor_name: string | null
  session_id: string | null
  slot_position: number | null
  checked_in_at: string | null
  opd_visit_id: string | null
  token_number: number | null
  visit_status: string | null
  no_show_at: string | null
  cancel_reason: string | null
  cancelled_at: string | null
  completed_at: string | null
  rescheduled_from_id: string | null
  created_at: string
}

export interface DaySlot {
  start: string
  end: string
  starts_at: string
  ends_at: string
  /** The slot started before now (hospital time). */
  past: boolean
  capacity: number
  booked: number
  free: number
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
  slots: DaySlot[]
}

export interface AppointmentsDay {
  date: string
  hospital_today: string
  /** The caller's department as the server derived it; null is every clinic. */
  department: string | null
  appointments: DeskAppointment[]
  sessions: DaySession[]
  types: { key: string; label: string }[]
}

export interface SlotBooking {
  patient_id: string
  scheduled_date: string
  scheduled_time: string
  appointment_type: string
  doctor_id?: string
  department?: string
  reason?: string | null
}

export interface CheckInResult {
  appointment: DeskAppointment
  visit: OpdVisit
  replayed: boolean
}

export interface RescheduleResult {
  appointment: DeskAppointment
  previous: DeskAppointment
  replayed: boolean
}

function query(params: Record<string, string | undefined>): string {
  const q = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value) q.set(key, value)
  }
  const text = q.toString()
  return text ? `?${text}` : ''
}

/** One hospital day of appointments, with each clinic session's slots. */
export function fetchAppointmentsDay(date?: string, doctorId?: string): Promise<AppointmentsDay> {
  return apiFetch<AppointmentsDay>(`/appointments/day${query({ date, doctor_id: doctorId })}`)
}

/** Book a slot (or, with no doctor, a clinic time). `key` makes a retry of the
 *  same request land on the same booking. */
export function bookAppointment(body: SlotBooking, key: string): Promise<DeskAppointment> {
  return apiFetch<DeskAppointment>('/appointments', {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify(body),
  })
}

/** The patient has arrived: the identity check, then their OPD visit and token. */
export function checkInAppointment(id: string, stated: StatedIdentity): Promise<CheckInResult> {
  return apiFetch<CheckInResult>(`/appointments/${encodeURIComponent(id)}/check-in`, {
    method: 'POST',
    body: JSON.stringify({ identity_check: stated }),
  })
}

/** The patient did not come (only once the appointment time has started). */
export function markNoShow(id: string): Promise<DeskAppointment> {
  return apiFetch<DeskAppointment>(`/appointments/${encodeURIComponent(id)}/no-show`, {
    method: 'POST',
  })
}

export function rescheduleAppointment(
  id: string,
  body: { scheduled_date: string; scheduled_time: string; doctor_id?: string; reason?: string },
  key: string,
): Promise<RescheduleResult> {
  return apiFetch<RescheduleResult>(`/appointments/${encodeURIComponent(id)}/reschedule`, {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify(body),
  })
}

export function cancelAppointment(id: string, reason?: string): Promise<DeskAppointment> {
  return apiFetch<DeskAppointment>(
    `/appointments/${encodeURIComponent(id)}${query({ reason: reason?.trim() || undefined })}`,
    { method: 'DELETE' },
  )
}
