import type { MedTask } from '../../../api/nurse'
import {
  formatClinicalDateTime, formatClinicalDayMonth, formatClinicalDayShort, formatClinicalTime, parseUTC,
} from '../../../lib/datetime'

/**
 * Relative-time wording shared across nurse mode.
 *
 * Ward language, not clock arithmetic: a nurse reads "40m late", not
 * "-40 minutes". Kept in one place so the board, the round and the task list
 * cannot drift into describing the same lateness three different ways.
 */

/** Past a day, lateness stops being a number anyone acts on: "144h 33m late"
 *  is a reading from last week, and the dated clock beside it already says
 *  when. Said in words instead, so the size of the number is not the message. */
const LATE_CAP_MINUTES = 24 * 60

/** "40m late" / "in 2h 10m" / "now" / "over 24h late". */
export function relativeMinutes(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return ''
  const abs = Math.abs(minutes)
  if (abs < 1) return 'now'
  if (minutes < 0 && abs > LATE_CAP_MINUTES) return 'over 24h late'
  const h = Math.floor(abs / 60)
  const m = abs % 60
  const span = h > 0 ? (m > 0 ? `${h}h ${m}m` : `${h}h`) : `${m}m`
  return minutes < 0 ? `${span} late` : `in ${span}`
}

/** Above this many uncharted earlier doses the exact count is noise: an hourly
 *  order started a week ago reads "168", and the action is the same at 13. */
export const MISSED_SLOTS_CAP = 12

/** "3" / "12+": the count of earlier doses not charted, as displayed. */
export function missedSlotsCount(slots: number): string {
  return slots > MISSED_SLOTS_CAP ? `${MISSED_SLOTS_CAP}+` : String(slots)
}

/**
 * "Tue 12 Aug" — the weekday and date for a home's context line.
 *
 * The weekday is there on purpose. A nurse covering a night shift reads the
 * board either side of midnight, and "12 Aug" alone does not tell her which
 * side of it she is on; "Tue" does, at a glance, without arithmetic.
 * `now` is injectable for tests only.
 */
export function shiftDate(iso: string | null | undefined, now: Date = new Date()): string {
  // Fixed clinical form, not the browser locale (which printed "Tue, Aug 12"
  // on one tablet and "Tue 12 Aug" on the next).
  const d = iso ? parseUTC(iso) : now
  return formatClinicalDayShort(isNaN(d.getTime()) ? now : d)
}

/** "Since 08:30 AM" style absolute time, for anchoring a relative label.
 *
 * Same-day moments render as a bare clock time; anything on an older calendar
 * day carries its date ("9 Aug, 02:32 PM"), and its year too once that differs.
 * A bare time on a days-old reading is
 * how "seven days ago" gets read as "an hour ago" (DEMO_READINESS §5.3).
 * `now` is injectable for tests only.
 *
 * parseUTC, not new Date: a bare SQLite "2026-10-02 13:21:00" is UTC, and
 * new Date read it as local, so a dispense at 06:51 PM showed as 01:21 PM.
 */
export function clockTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—'
  const d = parseUTC(iso)
  if (isNaN(d.getTime())) return '—'
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  if (sameDay) return formatClinicalTime(d)
  if (d.getFullYear() !== now.getFullYear()) return formatClinicalDateTime(d)
  return `${formatClinicalDayMonth(d)}, ${formatClinicalTime(d)}`
}

/**
 * How a medication's timing should read on the round.
 *
 * The schedule kinds that carry no clock (PRN, continuous, unparseable) say so
 * in words rather than borrowing the overdue vocabulary — a med we cannot time
 * must never look late.
 */
export function dueLabel(med: MedTask): string {
  switch (med.schedule_kind) {
    case 'prn':
      return 'As required'
    case 'continuous':
      // Not "Continuous": the group heading and usually the order's own
      // frequency already say that, and the row's timing slot owes the one
      // fact neither does, that there is no clock on it.
      return 'No due time'
    case 'stat':
      return med.due_basis === 'stat_given' ? 'Stat dose given' : 'Stat: give now'
    case 'unscheduled':
      return 'No fixed schedule'
    default:
      if (med.due_at === null) return 'No fixed schedule'
      return `Due ${clockTime(med.due_at)} · ${relativeMinutes(med.minutes_until_due)}`
  }
}

/** Observation timing for a patient card. */
export function obsLabel(
  lastObsAt: string | null,
  minutesUntilDue: number | null,
): string {
  if (!lastObsAt) return 'No observations recorded'
  const last = `Last ${clockTime(lastObsAt)}`
  if (minutesUntilDue === null) return last
  return `${last} · ${minutesUntilDue < 0 ? 'obs ' : 'next obs '}${relativeMinutes(minutesUntilDue)}`
}
