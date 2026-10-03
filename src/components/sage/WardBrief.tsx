import { useEffect, useState, useCallback, useRef } from 'react'
import { getWardBrief, socket, type WardBrief } from '../../api/client'
import { openPatient } from '../../lib/orbBus'
import './WardBrief.css'

/** Situational-awareness ward briefing — the client face of the local
 *  narrator (server/services/sage_narrator_service.py). Shows the LLM-free
 *  deterministic snapshot always, plus the local-LLM narrative when present.
 *  Refreshes on the socket 'sage:ward_brief' event the narrator emits. */

/** Socket-driven refetches are coalesced over this window. Each GET writes a
 *  PHI read row, and an alert burst used to write one per event per open card. */
const WARD_BRIEF_DEBOUNCE_MS = 3000

export default function WardBriefCard() {
  const [brief, setBrief] = useState<WardBrief | null>(null)
  const [collapsed, setCollapsed] = useState(false)
  /* The brief used to swallow its error and `return null`, so a failed or
     still-loading fetch rendered the same as a quiet ward: nothing at all. A
     clinician starting a shift could not tell "no one needs attention" from
     "the situational-awareness card is broken". */
  const [failed, setFailed] = useState(false)

  /** as_of of the brief on screen, to ignore an event we have already seen. */
  const loadedAsOf = useRef<string | null>(null)

  const load = useCallback(() => {
    getWardBrief()
      .then(b => {
        loadedAsOf.current = b?.snapshot?.as_of ?? null
        setBrief(b)
        setFailed(false)
      })
      .catch(() => setFailed(true))
  }, [])

  useEffect(() => {
    load()
    let timer: ReturnType<typeof setTimeout> | null = null
    const onBrief = (payload?: { updated_at?: string }) => {
      const seen = loadedAsOf.current
      const updated = payload?.updated_at
      // Already showing this snapshot or a newer one: nothing to fetch.
      if (seen && updated && Date.parse(updated) <= Date.parse(seen)) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => { timer = null; load() }, WARD_BRIEF_DEBOUNCE_MS)
    }
    socket.on('sage:ward_brief', onBrief)
    return () => {
      socket.off('sage:ward_brief', onBrief)
      if (timer) clearTimeout(timer)
    }
  }, [load])

  const snap = brief?.snapshot

  if (!snap) {
    if (!failed) return null // first load in flight — no flash of empty chrome
    return (
      <div className="ward-brief ward-brief--failed glass-card" data-testid="ward-brief" role="status">
        <div className="ward-brief__title">
          <span className="ward-brief__dot ward-brief__dot--warning" aria-hidden />
          <span>Sage: State of the Hospital</span>
        </div>
        <p className="ward-brief__narrative ward-brief__narrative--pending">
          Ward brief unavailable: this is a display failure, not an all-clear.
          Check Vigil for the live patient list.
        </p>
        <div className="ward-brief__footer">
          <button type="button" className="ward-brief__retry" onClick={load}>Retry</button>
        </div>
      </div>
    )
  }

  const watch = snap.watchlist || []

  return (
    <div className="ward-brief glass-card" data-testid="ward-brief">
      {/* The header stays a plain div: collapse lives on the toggle button, which
          is the only thing a keyboard user should have to find. It used to work
          for the keyboard only by accident — the button had no handler of its
          own and relied on its click bubbling up to this div. */}
      <div className="ward-brief__header" onClick={() => setCollapsed(c => !c)}>
        <div className="ward-brief__title">
          <span className="ward-brief__pulse" aria-hidden />
          <span id="ward-brief-heading">Sage: State of the Hospital</span>
        </div>
        <div className="ward-brief__meta">
          {/* A scoped brief has no hospital-wide census (the server withholds
              it), so it names its scope instead of printing a count with no
              number in it. */}
          {snap.scope ? (
            <span className="ward-brief__count" title="Patients in your department only. Hospital-wide counts are not shown.">{snap.scope} only</span>
          ) : snap.census != null ? (
            <span className="ward-brief__count" title="All admitted patients across the hospital, the Vigil tiles above count only your department">{snap.census} admitted hospital-wide</span>
          ) : null}
          {(snap.active_alerts ?? 0) > 0 && (
            <span className="ward-brief__alerts">{snap.active_alerts} active</span>
          )}
          <button
            type="button"
            className="ward-brief__toggle"
            aria-label={collapsed ? 'Expand ward brief' : 'Collapse ward brief'}
            aria-expanded={!collapsed}
            onClick={e => { e.stopPropagation(); setCollapsed(c => !c) }}
          >
            {collapsed ? '▸' : '▾'}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div className="ward-brief__body">
          {brief?.narrative ? (
            <p className="ward-brief__narrative">{brief.narrative}</p>
          ) : (
            <p className="ward-brief__narrative ward-brief__narrative--pending">
              {/* Says what the snapshot actually knows. "All admitted patients
                  are currently stable" asserted something the watchlist cannot
                  support — a patient with no recent observations is absent from
                  it for the same reason a well patient is. */}
              {/* The verb agrees too: the plural 's' was pluralised and "need"
                  was not, so a single-patient ward read "1 patient need
                  attention first." */}
              {watch.length > 0
                ? `${watch.length} patient${watch.length > 1 ? 's need' : ' needs'} attention first.`
                : 'No patients on the deterioration watchlist.'}
            </p>
          )}

          {watch.length > 0 && (
            <ul className="ward-brief__watchlist" aria-label="Deterioration watchlist">
              {watch.map(w => (
                /* This is the fastest route to the sickest patients on the ward.
                   It was a bare <li onClick>, so it existed for a mouse and not
                   at all for a keyboard; a real <button> puts it back in the tab
                   order and gives it the app-wide focus ring. */
                <li key={w.patient_id} className="ward-brief__watch-item">
                  <button
                    type="button"
                    className={`ward-brief__watch ward-brief__watch--${w.level}`}
                    onClick={() => openPatient(w.patient_id, w.name)}
                    title={`Open ${w.name}'s chart`}
                  >
                    <span className={`ward-brief__dot ward-brief__dot--${w.level}`} />
                    <span className="ward-brief__name">{w.name}</span>
                    <span className="ward-brief__bed">bed {w.bed}</span>
                    <span className="ward-brief__level">{w.level}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}

          {/* Only claims narration when a narrative is on screen: a scoped
              brief withholds the prose, and the engine line used to say
              "Narrated locally" over a card with none. */}
          <div className="ward-brief__footer">
            {brief?.narrative && brief.narrative_engine
              ? `Narrated locally · ${brief.narrative_engine}`
              : 'Deterministic snapshot (local)'}
          </div>
        </div>
      )}
    </div>
  )
}
