import { useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  getPatientStoryFeed,
  markChartReviewed,
  socket,
  type StoryEvent,
  type StoryType,
} from '../../api/client'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { parseUTC } from '../../lib/datetime'
import './PatientStory.css'

/**
 * Unified Patient Story — one scrollable clinical narrative for a patient,
 * merging admission, alerts, notes, imaging, labs, meds, orders,
 * administrations, care bundles, problems, allergies and discharge into a
 * single chronological feed with type filters. Opens as a focus-trapped dialog
 * from the patient command bar and refreshes live on the
 * patient:timeline_updated socket event.
 *
 * ── SINCE YOU LAST LOOKED ────────────────────────────────────────────────────
 * The reason a doctor opens this screen at 08:00 is not "show me the admission".
 * It is "what happened overnight". So the feed carries a per-clinician
 * watermark: the events above the line landed after the last time THIS user
 * pressed "Mark reviewed", and the line says when that was.
 *
 * Three rules this obeys, each of which the alternative gets wrong:
 *
 *  1. **The watermark only moves when the clinician says so.** Advancing it on
 *     render would mean the marker answers "has this been drawn" rather than
 *     "has this been read" — and a chart glanced at from a corridor would
 *     silently clear its own badge.
 *  2. **Never reviewed shows no line at all.** Treating an absent watermark as
 *     epoch-zero would mark every event on every chart new on a clinician's
 *     first login, which teaches them within a day to ignore the marker.
 *  3. **"Mark reviewed" pins to the newest event actually RENDERED**, not to
 *     `now`. Between the render and the click, a nurse may chart something; that
 *     event must survive as new rather than being marked read by a click that
 *     never saw it.
 */
interface Props {
  patientId: string
  patientName: string
  onClose: () => void
}

// Filter vocabulary + per-type presentation (label + semantic colour token).
// EVERY StoryType must appear here: the render indexes this map directly, so a
// type without an entry is a crash, not a missing chip.
const TYPE_META: Record<StoryType, { label: string; color: string }> = {
  admission: { label: 'Admission', color: 'var(--accent-text)' },
  alert: { label: 'Alerts', color: 'var(--status-danger)' },
  vitals: { label: 'Vitals', color: 'var(--status-warn)' },
  note: { label: 'Notes', color: 'var(--text-secondary)' },
  scribe: { label: 'Scribe', color: 'var(--text-secondary)' },
  imaging: { label: 'Imaging', color: 'var(--accent-text)' },
  medication: { label: 'Meds', color: 'var(--status-ok)' },
  order: { label: 'Orders', color: 'var(--accent-text)' },
  order_event: { label: 'Order status', color: 'var(--accent-text)' },
  administration: { label: 'eMAR', color: 'var(--status-ok)' },
  observation: { label: 'Labs', color: 'var(--status-warn)' },
  bundle: { label: 'Bundles', color: 'var(--status-danger)' },
  problem: { label: 'Problems', color: 'var(--text-secondary)' },
  allergy: { label: 'Allergies', color: 'var(--status-danger)' },
  discharge: { label: 'Discharge', color: 'var(--accent-text)' },
}

function fmtTime(ts: string | null): string {
  if (!ts) return '—'
  const d = parseUTC(ts)
  if (isNaN(d.getTime())) return ts
  return d.toLocaleString([], { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
}

/** Milliseconds for an API timestamp, or null when it cannot be read.
 *  null is never coerced to 0 — an unreadable timestamp compared against a
 *  watermark as "the epoch" would mark the event permanently old. */
function msOf(ts: string | null): number | null {
  if (!ts) return null
  const t = parseUTC(ts).getTime()
  return isNaN(t) ? null : t
}

export default function PatientStory({ patientId, patientName, onClose }: Props) {
  const [events, setEvents] = useState<StoryEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [reviewedAt, setReviewedAt] = useState<string | null>(null)
  const [marking, setMarking] = useState(false)
  const [markError, setMarkError] = useState('')
  // Active type filters — empty set means "show all".
  const [active, setActive] = useState<Set<StoryType>>(new Set())
  const modalRef = useFocusTrap<HTMLDivElement>(onClose)

  useEffect(() => {
    let alive = true
    // State is only set AFTER the await, so there's no synchronous cascading
    // setState in the effect (react-hooks/set-state-in-effect).
    const run = async () => {
      try {
        const feed = await getPatientStoryFeed(patientId)
        if (alive) { setEvents(feed.events); setReviewedAt(feed.last_reviewed_at); setError('') }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : 'Failed to load patient story')
      } finally {
        if (alive) setLoading(false)
      }
    }
    run()
    const onUpdate = (d: { patient_id?: string }) => { if (!d?.patient_id || d.patient_id === patientId) run() }
    socket.on('patient:timeline_updated', onUpdate)
    return () => { alive = false; socket.off('patient:timeline_updated', onUpdate) }
  }, [patientId])

  // Which types actually occur, so we only show relevant filter chips.
  const presentTypes = useMemo(() => {
    const s = new Set<StoryType>()
    events.forEach(e => s.add(e.type))
    return (Object.keys(TYPE_META) as StoryType[]).filter(t => s.has(t))
  }, [events])

  const shown = active.size === 0 ? events : events.filter(e => active.has(e.type))

  const reviewedMs = useMemo(() => msOf(reviewedAt), [reviewedAt])

  /* Where the "new since" rule falls, IN THE CURRENTLY VISIBLE LIST. The feed is
     newest-first, so it is the count of leading events that postdate the
     watermark. Deriving it from `shown` rather than from the whole feed is what
     keeps the line honest under a type filter: filter to Labs and the line sits
     above the new labs, not above where it would have been with everything on
     screen. */
  const newInView = useMemo(() => {
    if (reviewedMs === null) return 0
    let n = 0
    for (const e of shown) {
      const t = msOf(e.timestamp)
      if (t === null || t <= reviewedMs) break
      n += 1
    }
    return n
  }, [shown, reviewedMs])

  /* The newest event we are actually SHOWING the clinician. "Mark reviewed"
     pins the watermark here rather than to `now`, so anything charted between
     this render and the click stays new. */
  const newestShownTs = useMemo(() => {
    for (const e of events) {
      if (msOf(e.timestamp) !== null) return e.timestamp
    }
    return null
  }, [events])

  const toggle = (t: StoryType) => setActive(prev => {
    const next = new Set(prev)
    if (next.has(t)) next.delete(t); else next.add(t)
    return next
  })

  const markReviewed = useCallback(async () => {
    setMarking(true)
    setMarkError('')
    try {
      const res = await markChartReviewed(patientId, newestShownTs)
      setReviewedAt(res.last_reviewed_at)
    } catch (e) {
      // Say so. A silent failure here leaves a badge the clinician believes
      // they have cleared, and they stop trusting it.
      setMarkError(e instanceof Error ? e.message : 'Could not save the review marker')
    } finally {
      setMarking(false)
    }
  }, [patientId, newestShownTs])

  return createPortal(
    <div className="story-backdrop" onClick={onClose}>
      <div
        className="story-modal glass-strong"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="story-title"
        onClick={e => e.stopPropagation()}
      >
        <div className="story-modal__header">
          <div>
            <h2 className="story-modal__title" id="story-title">Patient Story</h2>
            <p className="story-modal__subtitle">{patientName} · one clinical narrative</p>
          </div>
          <button type="button" className="story-modal__close" onClick={onClose} aria-label="Close">&times;</button>
        </div>

        {/* The "since you last looked" bar. Three distinct states, and they are
            deliberately worded differently: never-reviewed is not "0 new". */}
        {!loading && !error && (
          <div className="story-review" role="status">
            <span className="story-review__text">
              {reviewedAt === null ? (
                <>You have not marked this chart reviewed before, no “new since” line is shown.</>
              ) : newInView > 0 ? (
                <>
                  <strong className="story-review__count">{newInView} new</strong>
                  {' since you last reviewed this chart on '}
                  <span className="metric-mono">{fmtTime(reviewedAt)}</span>
                </>
              ) : (
                <>Nothing new since you reviewed this chart on <span className="metric-mono">{fmtTime(reviewedAt)}</span></>
              )}
            </span>
            <button
              type="button"
              className="story-review__btn"
              onClick={markReviewed}
              disabled={marking || events.length === 0}
              title="Records that you have read this chart up to its newest event. A personal bookmark, not a clinical sign-off."
            >
              {marking ? 'Saving…' : 'Mark reviewed'}
            </button>
          </div>
        )}
        {markError && <div className="story-empty story-empty--error" role="alert">{markError}</div>}

        {presentTypes.length > 0 && (
          <div className="story-filters" role="group" aria-label="Filter timeline by type">
            <button
              type="button"
              className={`story-chip ${active.size === 0 ? 'story-chip--on' : ''}`}
              onClick={() => setActive(new Set())}
              aria-pressed={active.size === 0}
            >All</button>
            {presentTypes.map(t => (
              <button
                key={t}
                type="button"
                className={`story-chip ${active.has(t) ? 'story-chip--on' : ''}`}
                style={{ '--chip-color': TYPE_META[t].color } as React.CSSProperties}
                onClick={() => toggle(t)}
                aria-pressed={active.has(t)}
              >
                <span className="story-chip__dot" aria-hidden="true" />
                {TYPE_META[t].label}
              </button>
            ))}
          </div>
        )}

        <div className="story-modal__body">
          {loading ? (
            <div className="story-empty">Loading patient story…</div>
          ) : error ? (
            <div className="story-empty story-empty--error" role="alert">{error}</div>
          ) : shown.length === 0 ? (
            <div className="story-empty">No events for the selected filters.</div>
          ) : (
            <ol className="story-feed">
              {shown.map((e, i) => (
                <li
                  key={i}
                  className={`story-item${i < newInView ? ' story-item--new' : ''}${
                    newInView > 0 && i === newInView ? ' story-item--boundary' : ''
                  }`}
                >
                  {/* The line itself sits BEFORE the first already-seen event,
                      so it reads as a boundary rather than as a label on a row. */}
                  {newInView > 0 && i === newInView && (
                    <span className="story-since" aria-hidden="true">
                      <span className="story-since__label">Reviewed to here · {fmtTime(reviewedAt)}</span>
                    </span>
                  )}
                  <span className="story-item__rail" style={{ background: TYPE_META[e.type].color }} aria-hidden="true" />
                  <div className="story-item__body">
                    <div className="story-item__head">
                      <span className="story-item__type" style={{ color: TYPE_META[e.type].color }}>
                        {TYPE_META[e.type].label}
                      </span>
                      <span className="story-item__title">{e.title}</span>
                      {i < newInView && <span className="story-item__new-tag">NEW</span>}
                      <time className="story-item__time metric-mono">{fmtTime(e.timestamp)}</time>
                    </div>
                    {e.detail && <p className="story-item__detail">{e.detail}</p>}
                    {e.actor && <p className="story-item__actor">{e.actor}</p>}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
