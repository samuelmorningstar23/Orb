/**
 * Pulse: season and conditions, the client half of GET /api/pulse/season.
 *
 * Orb is strictly offline. Pulse used to call public weather, air quality, flu
 * and drug recall feeds, and the browser itself asked for the device location
 * and looked it up on a public map service. All of that is gone. The server
 * now answers from a seasonal calendar built into Orb for the hospital's
 * Indian state, so this is the only request Pulse makes, and it goes to the
 * app's own API like every other call.
 */
import { apiFetch } from './client'

export type SeasonKind = 'season' | 'disease' | 'environment'

/** Where an entry's content came from. Shown as a name only: Orb has no
 *  internet connection, so a link would lead nowhere. */
export interface SeasonSource {
  name: string
  url: string
  /** "YYYY-MM-DD", the day the source was read when the calendar was compiled. */
  accessed: string
}

interface SeasonEntryBase {
  id: string
  kind: SeasonKind
  title: string
  /** "MM-DD". A period may run over the new year ("11-01" to "02-28"). */
  start: string
  /** "MM-DD", inclusive. */
  end: string
  summary: string
  /** What usually goes up in this period. May be empty. */
  typically_rises: string[]
  sources: SeasonSource[]
}

/** An entry whose period includes today. */
export interface ActiveSeasonEntry extends SeasonEntryBase {
  in_peak: boolean
  /** "YYYY-MM-DD", the last day of this year's period. */
  ends_on: string
}

/** An entry that starts soon. */
export interface UpcomingSeasonEntry extends SeasonEntryBase {
  in_peak: false
  /** "YYYY-MM-DD". */
  starts_on: string
  days_away: number
}

export interface SeasonCalendarStatus {
  version: number
  /** "draft" until a clinician has reviewed the calendar. */
  status: 'draft' | 'reviewed'
  reviewed_by: string | null
  /** "YYYY-MM-DD". */
  reviewed_on: string | null
  /** "YYYY-MM-DD". */
  compiled_on: string
}

export interface PulseSeason {
  /** Today in the hospital's calendar, "YYYY-MM-DD". */
  date: string
  state: { code: string; name: string } | null
  zone: { id: string; name: string } | null
  /** True when the hospital's state is not set, so there is no calendar to read. */
  setup_needed: boolean
  setup_message: string | null
  calendar: SeasonCalendarStatus
  active: ActiveSeasonEntry[]
  upcoming: UpcomingSeasonEntry[]
}

/** GET /api/pulse/season (permission forecast.read). */
export function fetchPulseSeason(): Promise<PulseSeason> {
  return apiFetch<PulseSeason>('/pulse/season')
}
