/**
 * The times a medication order is due (medorders-19, W8-I; ruling MO-R8,
 * held for Samuel): a thin typed client for the schedule endpoints under
 * /api/medorders. Kept beside src/api/medorders.ts rather than in it, so the
 * order-store client and this one can change in the same wave.
 *
 * A prescriber may set the times an order is due, one per dose a day, on the
 * hospital's 24-hour clock. The hospital's signed dose times
 * (server/site_config/dose_times.json) are offered as the starting point; Orb
 * ships none of its own, so with no signed file there is nothing to offer and
 * the drug round keeps working each due time out from the frequency.
 *
 * Every call exists only while the server's 'med_order_store' feature is on;
 * saving also needs 'med_order_lifecycle'. The department is never a
 * parameter: an order on another ward answers 404.
 */
import { apiFetch } from './client'

export type ScheduleBasis = 'prescriber' | 'site_times'

/** The times set for one order, in clock order. */
export interface MedOrderSchedule {
  times: string[]
  /** 'site_times' when the hospital's signed times were accepted unchanged. */
  basis: ScheduleBasis
  schedule_version: number
  site_times_version: string | null
  site_times_fingerprint: string | null
  authored_by: string
  authored_at: string
}

/** One draft or live order of the patient, as the Orders screen needs it. */
export interface PatientScheduleEntry {
  med_order_id: string
  status: string
  version: number
  frequency: string | null
  /** How many doses a day the frequency means; null when it is not a fixed
   *  number every day (as needed, stat, every other day), which has no set
   *  times. */
  doses_per_day: number | null
  schedule: MedOrderSchedule | null
  /** The hospital's signed times for this frequency, or null. */
  site_times: string[] | null
}

export interface SiteDoseTimes {
  available: boolean
  version: string | null
  fingerprint: string | null
  signed: Record<string, { name: string; date: string }> | null
  times: { frequency: string; times: string[] }[]
}

export interface PatientSchedules {
  patient_id: string
  site_times: SiteDoseTimes
  orders: PatientScheduleEntry[]
}

export interface ScheduleSaveResult {
  order: { id: string; status: string; version: number; [key: string]: unknown }
  schedule: MedOrderSchedule | null
  replayed: boolean
}

const enc = encodeURIComponent

export function fetchPatientSchedules(patientId: string): Promise<PatientSchedules> {
  return apiFetch<PatientSchedules>(`/medorders/patient/${enc(patientId)}/schedules`)
}

export function fetchSiteDoseTimes(): Promise<SiteDoseTimes> {
  return apiFetch<SiteDoseTimes>('/medorders/dose-times')
}

/** Set the times an order is due; an empty list clears them. */
export function saveOrderSchedule(
  orderId: string,
  times: string[],
  expectedVersion?: number,
): Promise<ScheduleSaveResult> {
  return apiFetch<ScheduleSaveResult>(`/medorders/orders/${enc(orderId)}/schedule`, {
    method: 'PUT',
    body: JSON.stringify({
      times,
      ...(expectedVersion !== undefined ? { expected_version: expectedVersion } : {}),
    }),
  })
}

/** 'HH:MM' on the 24-hour clock, 00:00 to 23:59. */
export function isClockTime(value: string): boolean {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(value.trim())
}

/** The times in clock order, as the server stores them. */
export function inClockOrder(times: string[]): string[] {
  return [...times].map(t => t.trim()).sort()
}
