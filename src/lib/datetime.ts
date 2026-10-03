/** Timestamp parsing for values that come back from the API.
 *
 *  SQLite's CURRENT_TIMESTAMP default writes bare UTC strings
 *  ("2026-05-15 12:41:00") with no timezone marker, and JS parses those as
 *  LOCAL time — which shifts every displayed clinical time by the viewer's UTC
 *  offset (a note charted at 19:30 IST renders as 14:00). Rows written by the
 *  application layer instead carry a tz-aware ISO string, so the SAME column
 *  can hold both dialects (clinical_documents does). parseUTC handles both:
 *  bare strings are read as UTC, tz-suffixed strings are passed through
 *  untouched — so it is safe to use on any DB-sourced timestamp, and mixed
 *  lists sort and compare correctly.
 */

// Trailing "Z" / "+05:30" / "-0500" — anything that already pins the offset.
const TZ_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/i

export function parseUTC(ts: string): Date {
  if (!ts) return new Date()
  if (TZ_SUFFIX.test(ts)) return new Date(ts)
  return new Date(ts.replace(' ', 'T') + 'Z')
}

/** Local-time renderings of a DB timestamp. Each returns '' for a missing or
 *  unparseable value rather than "Invalid Date". */
function localize(ts: string | null | undefined, render: (d: Date) => string): string {
  if (!ts) return ''
  const d = parseUTC(ts)
  return isNaN(d.getTime()) ? '' : render(d)
}

/* Clinical dates are written with the month as a WORD ("1 Oct 2026").
 *
 * A bare toLocaleString() follows the browser's locale, and most browsers ship
 * en-US, so the Sepsis Six timer read "since 10/1/2026" and the Sage draft
 * footer "Drafted 06:38 PM on 10/1/2026". India writes day first, so a nurse
 * reads 10/1 as 10 January. On a clinical record a date that two readers parse
 * two ways is a defect, not a style choice, and a month name removes the
 * ambiguity in every locale. Times keep the 12-hour "06:38 PM" form the rest of
 * the app already uses. */
// Month and weekday names come from a table, not Intl: newer en-GB data
// abbreviates September as "Sept", so the same chart read "30 Sept" on one
// tablet and "30 Sep" on another.
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

// Recent browsers put a narrow no-break space (U+202F) before AM/PM. It does
// not match a typed space in search or in tests, and some fonts draw it as a
// box, so every clinical time uses a plain space.
function plainSpaces(s: string): string {
  return s.replace(/[\u202F\u00A0]/g, ' ')
}

function clinicalTime(d: Date, seconds: boolean): string {
  return plainSpaces(d.toLocaleTimeString('en-US', {
    hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}),
  }))
}

function asDate(v: Date | string | number | null | undefined): Date | null {
  if (v === null || v === undefined || v === '') return null
  const d = v instanceof Date ? v : typeof v === 'number' ? new Date(v) : parseUTC(v)
  return isNaN(d.getTime()) ? null : d
}

/** "1 Oct 2026". Strings go through parseUTC, so a bare SQLite UTC timestamp
 *  is not shifted by the viewer's offset. Returns '' for a missing value. */
export function formatClinicalDate(v: Date | string | number | null | undefined): string {
  const d = asDate(v)
  return d ? `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}` : ''
}

/** A calendar-date key the server sends as "YYYY-MM-DD" (a business day, a
 *  policy expiry, a service due date), written "1 Oct 2026". It is a date with
 *  no time, so it is read in the viewer's calendar rather than through
 *  parseUTC, which would shift it a day for anyone west of UTC. Anything else
 *  is returned unchanged rather than dropped. */
export function formatClinicalDateKey(key: string | null | undefined): string {
  if (!key) return ''
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key.trim())
  if (!m) return key
  return formatClinicalDate(new Date(+m[1], +m[2] - 1, +m[3]))
}

/** "06:38 PM" (with `seconds`, "06:38:12 PM"), whatever the browser locale. */
export function formatClinicalTime(
  v: Date | string | number | null | undefined,
  opts: { seconds?: boolean } = {},
): string {
  const d = asDate(v)
  return d ? clinicalTime(d, !!opts.seconds) : ''
}

/** A wall-clock "HH:MM" the server sends for a slot (an appointment, a theatre
 *  booking), written as the clinical "08:00 AM". The ops screens printed the
 *  same slot as "08:00" on one board and "8:00 AM" on the next. Anything that is
 *  not a valid HH:MM is returned unchanged. */
export function formatClinicalClock(hhmm: string | null | undefined): string {
  if (!hhmm) return ''
  const m = /^(\d{1,2}):(\d{2})(?::\d{2})?$/.exec(hhmm.trim())
  if (!m || +m[1] > 23 || +m[2] > 59) return hhmm
  const h = +m[1]
  return `${String(h % 12 || 12).padStart(2, '0')}:${m[2]} ${h < 12 ? 'AM' : 'PM'}`
}

/** "Thu 1 Oct": a day on a schedule or board within the current fortnight,
 *  where the year is not in doubt. Use formatClinicalDate when it might be. */
export function formatClinicalDayShort(v: Date | string | number | null | undefined): string {
  const d = asDate(v)
  return d ? `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}` : ''
}

/** "30 Sep": a day and month, for a line whose year is not in doubt. */
export function formatClinicalDayMonth(v: Date | string | number | null | undefined): string {
  const d = asDate(v)
  return d ? `${d.getDate()} ${MONTHS[d.getMonth()]}` : ''
}

/** A day divider in a message thread: "Today", "Yesterday", "30 Sep", or
 *  "30 Sep 2025" once the year differs. Calendar days are the viewer's own. */
export function formatDayDivider(
  v: Date | string | number | null | undefined,
  now: Date = new Date(),
): string {
  const d = asDate(v)
  if (!d) return ''
  const key = localDateKey(d)
  if (key === localDateKey(now)) return 'Today'
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  if (key === localDateKey(yesterday)) return 'Yesterday'
  return d.getFullYear() === now.getFullYear() ? formatClinicalDayMonth(d) : formatClinicalDate(d)
}

/** A date typed into a search box, read day first as India writes it:
 *  "1 Oct 2026", "1 October", "Oct 1", "01/10/2026", "1-10-2026", "01102026"
 *  or "2026-10-01". The year may be left off a named-month date. Returns null
 *  for anything that is not a real calendar date, so ordinary words fall
 *  through to text search. */
export interface DateSearch { year: number | null; month: number; day: number }

export function parseDateSearch(raw: string): DateSearch | null {
  const q = raw.trim().toLowerCase().replace(/,/g, ' ').replace(/\s+/g, ' ')
  if (!q) return null
  const monthOf = (word: string): number => {
    const i = MONTHS.findIndex(m => word.startsWith(m.toLowerCase()))
    return i >= 0 && word.length >= 3 ? i + 1 : 0
  }
  let year: number | null = null
  let month = 0
  let day = 0
  let m: RegExpMatchArray | null
  if ((m = q.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/))) {
    year = +m[1]; month = +m[2]; day = +m[3]
  } else if ((m = q.match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/))) {
    day = +m[1]; month = +m[2]; year = +m[3]
  } else if ((m = q.match(/^(\d{2})(\d{2})(\d{4})$/))) {
    day = +m[1]; month = +m[2]; year = +m[3]
  } else if ((m = q.match(/^(\d{1,2}) ([a-z]+)\.?(?: (\d{4}))?$/))) {
    day = +m[1]; month = monthOf(m[2]); year = m[3] ? +m[3] : null
  } else if ((m = q.match(/^([a-z]+)\.? (\d{1,2})(?: (\d{4}))?$/))) {
    month = monthOf(m[1]); day = +m[2]; year = m[3] ? +m[3] : null
  } else {
    return null
  }
  if (month < 1 || month > 12 || day < 1) return null
  // Reject 31 Sep and friends: the date must survive a round trip.
  const probe = new Date(year ?? 2024, month - 1, day)
  if (probe.getMonth() !== month - 1 || probe.getDate() !== day) return null
  return { year, month, day }
}

/** Whether a timestamp falls on the searched day, in the viewer's calendar. */
export function matchesDateSearch(v: Date | string | number | null | undefined, q: DateSearch): boolean {
  const d = asDate(v)
  if (!d) return false
  return d.getDate() === q.day && d.getMonth() + 1 === q.month && (q.year === null || d.getFullYear() === q.year)
}

/** "1 Oct 2026, 06:38 PM" (with `seconds`, "1 Oct 2026, 06:38:12 PM"). */
export function formatClinicalDateTime(
  v: Date | string | number | null | undefined,
  opts: { seconds?: boolean } = {},
): string {
  const d = asDate(v)
  return d ? `${formatClinicalDate(d)}, ${clinicalTime(d, !!opts.seconds)}` : ''
}

export function formatUTCDateTime(ts: string | null | undefined): string {
  return localize(ts, d => formatClinicalDateTime(d, { seconds: true }))
}

export function formatUTCDate(
  ts: string | null | undefined,
  locales?: string | string[],
  options?: Intl.DateTimeFormatOptions,
): string {
  // A caller that names its own locale or format keeps it; the default is the
  // unambiguous clinical form rather than whatever the browser's locale says.
  if (locales === undefined && options === undefined) return localize(ts, formatClinicalDate)
  return localize(ts, d => d.toLocaleDateString(locales, options))
}

/** LOCAL calendar date as `YYYY-MM-DD`.
 *
 *  `new Date().toISOString().split('T')[0]` is the wrong way to ask "what day
 *  is it here" — it returns the UTC date, so in IST (UTC+5:30) every clock
 *  reading between 00:00 and 05:30 local yields YESTERDAY. That is how the
 *  Appointments board came to mark Friday as TODAY while the app header said
 *  Saturday. Any comparison against a server-side local date key must use this.
 */
export function localDateKey(d: Date = new Date()): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export function formatUTCTime(
  ts: string | null | undefined,
  options?: Intl.DateTimeFormatOptions,
): string {
  // The default is the clinical "06:38 PM". It used to follow the browser's
  // locale, so the same board read "18:38" on one tablet and "6:38 pm" on
  // the next.
  if (options === undefined) return localize(ts, d => clinicalTime(d, false))
  return localize(ts, d => plainSpaces(d.toLocaleTimeString('en-US', options)))
}
