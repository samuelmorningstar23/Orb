/**
 * Pulse: season and conditions.
 *
 * A panel opened from the header that says which season the hospital is in and
 * what that season usually brings to the wards (the monsoon, dengue season,
 * winter smog), from a seasonal calendar built into Orb for the hospital's
 * Indian state.
 *
 * Orb is strictly offline (ruled 4 October 2026, Pulse rebuilt 6 October). The
 * old panel showed live weather, air quality, US flu and FDA recall feeds,
 * asked the browser for the device location and looked it up on a public map
 * service, kept that location in localStorage and polled every 15 minutes. All
 * of that is gone. The panel makes one request, GET /api/pulse/season, to the
 * app's own server, each time it is opened. The date only changes once a day,
 * so there is nothing to poll.
 *
 * The calendar is honest about itself: until a clinician has reviewed it, the
 * panel says so above the cards, and sources are printed as names, never as
 * links, because there is no internet to follow them to.
 */
import { useEffect, useId, useState } from 'react'
import {
  fetchPulseSeason,
  type ActiveSeasonEntry,
  type PulseSeason,
  type SeasonCalendarStatus,
  type UpcomingSeasonEntry,
} from '../api/pulse'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { formatClinicalDateKey } from '../lib/datetime'
import './PulseWidget.css'

const PANEL_ID = 'pulse-panel'

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]
// February is 29 so that "02-29" parses; "02-28" also counts as its last day.
const MONTH_DAYS = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

interface MonthDay { month: number; day: number }

function parseMonthDay(value: string): MonthDay | null {
  const m = /^(\d{2})-(\d{2})$/.exec((value || '').trim())
  if (!m) return null
  const month = +m[1]
  const day = +m[2]
  if (month < 1 || month > 12 || day < 1 || day > MONTH_DAYS[month - 1]) return null
  return { month, day }
}

const isMonthEnd = (p: MonthDay) =>
  p.day === MONTH_DAYS[p.month - 1] || (p.month === 2 && p.day === 28)

/**
 * A calendar period in words: "June to September" when it runs over whole
 * months, "15 October to 30 November" when it does not, "All year" when it
 * covers every month. A period may run over the new year ("November to
 * February"). An unreadable value is printed as sent rather than dropped.
 */
function periodInWords(start: string, end: string): string {
  const s = parseMonthDay(start)
  const e = parseMonthDay(end)
  if (!s || !e) return `${start} to ${end}`
  const month = (p: MonthDay) => MONTH_NAMES[p.month - 1]
  if (s.day === 1 && isMonthEnd(e)) {
    if (s.month === e.month) return month(s)
    // Ends in the month before the one it starts in: every month is covered.
    if (e.month === ((s.month + 10) % 12) + 1) return 'All year'
    return `${month(s)} to ${month(e)}`
  }
  if (s.month === e.month && s.day <= e.day) return `${s.day} to ${e.day} ${month(s)}`
  return `${s.day} ${month(s)} to ${e.day} ${month(e)}`
}

/** "Tomorrow", "In 12 days". */
function whenInWords(daysAway: number): string {
  if (daysAway <= 0) return 'Today'
  if (daysAway === 1) return 'Tomorrow'
  return `In ${daysAway} days`
}

/** "Maharashtra", or "Maharashtra (Konkan)" when the calendar is kept for a
 *  zone of the state rather than the whole of it. */
function placeName(data: PulseSeason): string {
  if (!data.state) return 'State not set'
  if (data.zone && data.zone.name && data.zone.name !== data.state.name) {
    return `${data.state.name} (${data.zone.name})`
  }
  return data.state.name
}

/** Anything other than "reviewed" reads as a draft: an unknown status must not
 *  lose the label. */
function CalendarStatus({ calendar }: { calendar: SeasonCalendarStatus }) {
  if (calendar.status === 'reviewed') {
    const by = calendar.reviewed_by ? ` by ${calendar.reviewed_by}` : ''
    const on = calendar.reviewed_on ? ` on ${formatClinicalDateKey(calendar.reviewed_on)}` : ''
    return <p className="pulse-calendar pulse-calendar--reviewed">Reviewed{by}{on}</p>
  }
  return (
    <p className="pulse-calendar pulse-calendar--draft" role="note">
      Draft: not yet reviewed by a clinician
    </p>
  )
}

function SeasonCard({ entry }: { entry: ActiveSeasonEntry }) {
  const titleId = useId()
  const rises = entry.typically_rises ?? []
  const sources = entry.sources ?? []
  return (
    <li className="pulse-season" data-kind={entry.kind} aria-labelledby={titleId}>
      <div className="pulse-season__head">
        <h4 id={titleId} className="pulse-season__title">{entry.title}</h4>
        {entry.in_peak && (
          <span className="orb-count-chip orb-count-chip--info pulse-season__peak">Peak now</span>
        )}
      </div>
      <p className="pulse-season__period">{periodInWords(entry.start, entry.end)}</p>
      {entry.summary && <p className="pulse-season__summary">{entry.summary}</p>}
      {rises.length > 0 && (
        <p className="pulse-season__rises">Typically rises: {rises.join(', ')}</p>
      )}
      {sources.length > 0 && (
        <p className="pulse-season__sources">
          {sources.length === 1 ? 'Source' : 'Sources'}: {sources.map(s => s.name).join('; ')}
        </p>
      )}
    </li>
  )
}

function UpcomingList({ entries }: { entries: UpcomingSeasonEntry[] }) {
  return (
    <ul className="pulse-upcoming">
      {entries.map(entry => (
        <li key={entry.id} className="pulse-upcoming__item">
          {whenInWords(entry.days_away)}: {entry.title}
        </li>
      ))}
    </ul>
  )
}

function SeasonBody({ data }: { data: PulseSeason }) {
  if (data.setup_needed) {
    return (
      <div className="orb-state orb-state--empty pulse-state" role="note">
        <div className="orb-state__content">
          <p className="orb-state__detail">
            {data.setup_message || "The hospital's state has not been set, so there is no seasonal calendar to show."}
          </p>
        </div>
      </div>
    )
  }
  const active = data.active ?? []
  const upcoming = data.upcoming ?? []
  return (
    <>
      {data.calendar && <CalendarStatus calendar={data.calendar} />}

      <section className="pulse-section" aria-labelledby="pulse-this-season">
        <h3 id="pulse-this-season" className="pulse-section-label">This season</h3>
        {active.length > 0 ? (
          <ul className="pulse-season-list">
            {active.map(entry => <SeasonCard key={entry.id} entry={entry} />)}
          </ul>
        ) : (
          <div className="orb-state orb-state--empty pulse-state">
            <div className="orb-state__content">
              <p className="orb-state__detail">Nothing in the calendar covers today.</p>
            </div>
          </div>
        )}
      </section>

      {upcoming.length > 0 && (
        <section className="pulse-section" aria-labelledby="pulse-coming-up">
          <h3 id="pulse-coming-up" className="pulse-section-label">Coming up</h3>
          <UpcomingList entries={upcoming} />
        </section>
      )}
    </>
  )
}

type Load =
  | { kind: 'loading' }
  | { kind: 'ready'; data: PulseSeason }
  | { kind: 'error'; message: string | null }

/** Mounted only while open, so the focus trap arms on open and lets go on
 *  close (useFocusTrap needs its container to exist when it mounts), and each
 *  opening reads the calendar afresh. */
function PulsePanel({ onClose }: { onClose: () => void }) {
  const panelRef = useFocusTrap<HTMLDivElement>(onClose)
  const titleId = useId()
  const subtitleId = useId()
  const [load, setLoad] = useState<Load>({ kind: 'loading' })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let live = true
    fetchPulseSeason().then(
      data => { if (live) setLoad({ kind: 'ready', data }) },
      (e: unknown) => {
        if (!live) return
        // The server's own sentence (a 503 says whether the calendar is missing
        // or refused, and who can fix it). A browser network error ("Failed to
        // fetch") has no status and says nothing a clinician can use.
        const fromServer = e instanceof Error && typeof (e as { status?: unknown }).status === 'number'
        setLoad({ kind: 'error', message: fromServer && e.message ? e.message : null })
      },
    )
    return () => { live = false }
  }, [attempt])

  const retry = () => {
    setLoad({ kind: 'loading' })
    setAttempt(n => n + 1)
  }

  const data = load.kind === 'ready' ? load.data : null

  return (
    <>
      <div className="pulse-backdrop" onClick={onClose} aria-hidden="true" />
      <div
        className="pulse-panel"
        id={PANEL_ID}
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={subtitleId}
        tabIndex={-1}
      >
        <div className="pulse-panel__header">
          <div className="pulse-panel__heading">
            <h2 id={titleId} className="pulse-panel__title">
              <svg className="pulse-panel__title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="12" cy="12" r="4" />
                <path d="M12 2v4" />
                <path d="M12 18v4" />
                <path d="M4.93 4.93l2.83 2.83" />
                <path d="M16.24 16.24l2.83 2.83" />
                <path d="M2 12h4" />
                <path d="M18 12h4" />
              </svg>
              Pulse
            </h2>
            <p id={subtitleId} className="pulse-panel__subtitle">Season and conditions</p>
          </div>
          <button type="button" className="pulse-panel__close" onClick={onClose} aria-label="Close Pulse">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M18 6 6 18" />
              <path d="M6 6l12 12" />
            </svg>
          </button>
        </div>

        {data && (
          <div className="pulse-panel__context">
            <span className="pulse-panel__place">{placeName(data)}</span>
            {data.date && (
              <time className="pulse-panel__date" dateTime={data.date}>
                {formatClinicalDateKey(data.date)}
              </time>
            )}
          </div>
        )}

        <div className="pulse-panel__body">
          {load.kind === 'loading' && (
            <div className="orb-state orb-state--loading pulse-state" role="status">
              <div className="orb-state__content">
                <p className="orb-state__detail">Loading the seasonal calendar…</p>
              </div>
            </div>
          )}
          {load.kind === 'error' && (
            <div className="orb-state orb-state--error pulse-state" role="alert">
              <div className="orb-state__content">
                <p className="orb-state__title">The seasonal calendar could not be loaded.</p>
                {load.message && <p className="orb-state__detail">{load.message}</p>}
                <div>
                  <button type="button" className="btn-glass" onClick={retry}>Try again</button>
                </div>
              </div>
            </div>
          )}
          {data && <SeasonBody data={data} />}
        </div>

        {data && !data.setup_needed && data.calendar?.compiled_on && (
          <div className="pulse-panel__footer">
            Built into Orb. Calendar compiled {formatClinicalDateKey(data.calendar.compiled_on)}.
          </div>
        )}
      </div>
    </>
  )
}

// The old Pulse saved the browser's location here. Nothing reads it now; drop it.
const LEGACY_LOCATION_KEY = 'orb_pulse_location'

export default function PulseWidget() {
  const [isOpen, setIsOpen] = useState(false)
  const close = () => setIsOpen(false)

  useEffect(() => {
    try { localStorage.removeItem(LEGACY_LOCATION_KEY) } catch { /* storage unavailable */ }
  }, [])

  return (
    <>
      <button
        type="button"
        className={`pulse-trigger ${isOpen ? 'pulse-trigger--active' : ''}`}
        onClick={() => setIsOpen(open => !open)}
        title="Pulse: season and conditions"
        aria-label="Pulse: season and conditions"
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        aria-controls={isOpen ? PANEL_ID : undefined}
        id="btn-pulse"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
        </svg>
      </button>

      {isOpen && <PulsePanel onClose={close} />}
    </>
  )
}
