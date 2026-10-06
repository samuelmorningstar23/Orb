import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import { apiFetch } from '../api/client'
import './FlightRecorder.css'
import { formatClinicalDateTime } from '../lib/datetime'
import { humaniseEnum } from '../lib/humanise'
import { actorLabel } from '../lib/actorLabel'

/**
 * A8 — Flight Recorder v1.
 *
 * A replay/forensics surface over Orb's hash-chained audit log and event bus.
 * An admin scrubs a unified timeline of everything that happened (to a patient
 * or hospital-wide) — audit events, event-bus dispatches, order transitions —
 * and can play/step through it while the hash-chain integrity is verified and
 * shown as a green ("verified") / red ("tampered at #N") badge.
 *
 * Admin-only on the server (require_permission("audit.read")); this component is
 * meant to live as a tab inside the admin panel.
 */

type EventSource = 'audit' | 'event' | 'order'

interface TimelineEvent {
  id: string | number
  ts: string
  source: EventSource
  actor: string | null
  /** The actor's name from users, read by the server now; null for a node or a user who is gone. */
  actor_name?: string | null
  action: string | null
  target: string | null
  detail: string | null
  chain_ok: boolean | null
  patient_id: string | null
  correlation_id: string | null
}

/** GET /flight-recorder/event/{source}/{id}: the raw row behind one entry. */
interface EventDetail {
  source: EventSource
  event: Record<string, unknown>
}

interface VerifyResult {
  ok: boolean
  broken_at: number | null
  count: number
}

const SOURCE_META: Record<EventSource, { label: string; color: string }> = {
  audit: { label: 'Audit', color: 'var(--status-info)' },
  event: { label: 'Event', color: 'var(--fr-lane-event)' },
  order: { label: 'Order', color: 'var(--fr-lane-order)' },
}

function fmtTs(ts: string): string {
  return formatClinicalDateTime(ts, { seconds: true }) || ts
}

// Each load re-verifies the whole audit chain on the server, so the filter is
// committed on a pause in typing (or at once on Enter/Reload), never per keystroke.
export const FILTER_DEBOUNCE_MS = 400

export default function FlightRecorder() {
  const [events, setEvents] = useState<TimelineEvent[]>([])
  const [verify, setVerify] = useState<VerifyResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [patientFilter, setPatientFilter] = useState('')
  const [selectedIdx, setSelectedIdx] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [detail, setDetail] = useState<EventDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const timerRef = useRef<number | null>(null)
  const debounceRef = useRef<number | null>(null)
  const inflight = useRef<AbortController | null>(null)

  const load = useCallback(() => {
    // Only the newest load may paint: an older hospital-wide answer arriving
    // late must never show under a patient filter.
    inflight.current?.abort()
    const ctrl = new AbortController()
    inflight.current = ctrl
    setLoading(true)
    setError(null)
    const pid = patientFilter
    const qs = pid ? `?patient_id=${encodeURIComponent(pid)}&limit=500` : '?limit=500'
    const vqs = pid ? `?patient_id=${encodeURIComponent(pid)}` : ''
    // Verify first: its full walk refreshes the server's chain cache, which the
    // timeline then reuses instead of walking the chain a second time.
    apiFetch<VerifyResult>(`/flight-recorder/verify${vqs}`, { signal: ctrl.signal })
      .then(async (v) => {
        const tl = await apiFetch<{ events: TimelineEvent[]; count: number }>(
          `/flight-recorder/timeline${qs}`, { signal: ctrl.signal })
        if (inflight.current !== ctrl) return
        setEvents(tl.events)
        setVerify(v)
        setSelectedIdx(tl.events.length ? tl.events.length - 1 : 0)
      })
      .catch((e) => {
        if (inflight.current !== ctrl || ctrl.signal.aborted) return
        setError(e?.message || 'Failed to load flight recorder')
      })
      .finally(() => { if (inflight.current === ctrl) setLoading(false) })
  }, [patientFilter])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { load() }, [load])
  useEffect(() => () => {
    inflight.current?.abort()
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
  }, [])

  const onFilterChange = (value: string) => {
    setDraft(value)
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    debounceRef.current = window.setTimeout(() => setPatientFilter(value.trim()), FILTER_DEBOUNCE_MS)
  }

  // Enter and Reload commit now. An unchanged filter still reloads.
  const commitFilter = () => {
    if (debounceRef.current) { window.clearTimeout(debounceRef.current); debounceRef.current = null }
    const next = draft.trim()
    if (next !== patientFilter) setPatientFilter(next)
    else load()
  }

  const selected = events[selectedIdx]

  // Fetch full detail when the selected event changes.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    if (!selected) { setDetail(null); return }
    let alive = true
    setDetailLoading(true)
    apiFetch<EventDetail>(`/flight-recorder/event/${selected.source}/${encodeURIComponent(String(selected.id))}`)
      .then((d) => { if (alive) setDetail(d) })
      .catch(() => { if (alive) setDetail(null) })
      .finally(() => { if (alive) setDetailLoading(false) })
    return () => { alive = false }
  }, [selected?.source, selected?.id])

  // Play/step: advance through events on a timer.
  useEffect(() => {
    if (!playing) {
      if (timerRef.current) { window.clearInterval(timerRef.current); timerRef.current = null }
      return
    }
    timerRef.current = window.setInterval(() => {
      setSelectedIdx((i) => {
        if (i >= events.length - 1) { setPlaying(false); return i }
        return i + 1
      })
    }, 900)
    return () => { if (timerRef.current) window.clearInterval(timerRef.current) }
  }, [playing, events.length])

  const step = (dir: -1 | 1) => {
    setPlaying(false)
    setSelectedIdx((i) => Math.max(0, Math.min(events.length - 1, i + dir)))
  }

  const badge = useMemo(() => {
    if (!verify) return null
    if (verify.ok) {
      return (
        <span className="fr-badge fr-badge--ok" title={`${verify.count} rows under a verified chain`}>
          <span className="fr-dot" /> Chain verified · {verify.count}
        </span>
      )
    }
    return (
      <span className="fr-badge fr-badge--bad" title="Audit hash chain integrity failure">
        <span className="fr-dot" /> Tampered at #{verify.broken_at}
      </span>
    )
  }, [verify])

  return (
    <div className="flight-recorder">
      {/* ── Header / controls ── */}
      <div className="fr-header glass-card">
        <div className="fr-header-left">
          <h3 className="fr-title">Flight Recorder</h3>
          <p className="fr-subtitle">Replay the audit + event stream and verify chain integrity.</p>
        </div>
        <div className="fr-header-right">
          {badge}
          <input
            className="fr-input"
            placeholder="Filter by patient ID…"
            aria-label="Filter by patient ID"
            value={draft}
            onChange={(e) => onFilterChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') commitFilter() }}
          />
          <button className="fr-btn" onClick={commitFilter} title="Reload">Reload</button>
        </div>
      </div>

      {loading ? (
        <div className="fr-empty">Loading flight recorder…</div>
      ) : error ? (
        <div className="fr-empty" role="alert">{error}</div>
      ) : events.length === 0 ? (
        <div className="fr-empty">No recorded events for this scope.</div>
      ) : (
        <>
          {/* ── Transport / scrubber ── */}
          <div className="fr-transport glass-card">
            <div className="fr-controls">
              <button className="fr-ctrl" onClick={() => step(-1)} disabled={selectedIdx <= 0} title="Step back">◀</button>
              <button className="fr-ctrl fr-ctrl--play" onClick={() => setPlaying((p) => !p)} title={playing ? 'Pause' : 'Play'}>
                {playing ? '❚❚' : '▶'}
              </button>
              <button className="fr-ctrl" onClick={() => step(1)} disabled={selectedIdx >= events.length - 1} title="Step forward">▶</button>
            </div>
            <input
              className="fr-scrub"
              type="range"
              min={0}
              max={events.length - 1}
              value={selectedIdx}
              onChange={(e) => { setPlaying(false); setSelectedIdx(Number(e.target.value)) }}
            />
            <div className="fr-position">
              {(selectedIdx + 1).toLocaleString('en-IN')} / {events.length.toLocaleString('en-IN')}
              {selected && <span className="fr-position-ts">{fmtTs(selected.ts)}</span>}
            </div>
          </div>

          <div className="fr-body">
            {/* ── Timeline list ── */}
            <div className="fr-timeline glass-card">
              {events.map((ev, idx) => {
                const meta = SOURCE_META[ev.source]
                const tampered = ev.chain_ok === false
                return (
                  <button
                    key={`${ev.source}-${ev.id}`}
                    className={`fr-tl-item ${idx === selectedIdx ? 'fr-tl-item--active' : ''} ${tampered ? 'fr-tl-item--tampered' : ''}`}
                    onClick={() => { setPlaying(false); setSelectedIdx(idx) }}
                  >
                    <span className="fr-tl-marker" style={{ background: meta.color }} />
                    <span className="fr-tl-source" style={{ color: meta.color }}>{meta.label}</span>
                    <span className="fr-tl-action">{humaniseEnum(ev.action)}</span>
                    <span className="fr-tl-target">{ev.target || ''}</span>
                    {/* Always one status slot, so every row has the same six grid cells
                        and the timestamp never spills onto the next row. */}
                    <span
                      className={`fr-tl-status${tampered ? ' fr-tl-flag' : ev.chain_ok === true ? ' fr-tl-ok' : ''}`}
                      title={tampered ? 'Chain break' : ev.chain_ok === true ? 'Chain verified' : undefined}
                    >
                      {tampered ? '⚠' : ev.chain_ok === true ? '✓' : ''}
                    </span>
                    <span className="fr-tl-ts">{fmtTs(ev.ts)}</span>
                  </button>
                )
              })}
            </div>

            {/* ── Detail pane ── */}
            <div className="fr-detail glass-card">
              {!selected ? (
                <div className="fr-empty">Select an event.</div>
              ) : (
                <>
                  <div className="fr-detail-head">
                    <span className="fr-chip" style={{ background: SOURCE_META[selected.source].color }}>
                      {SOURCE_META[selected.source].label}
                    </span>
                    <span className="fr-detail-action">{humaniseEnum(selected.action)}</span>
                    {selected.chain_ok === false && <span className="fr-badge fr-badge--bad">tampered</span>}
                    {selected.chain_ok === true && <span className="fr-badge fr-badge--ok">verified</span>}
                  </div>
                  <dl className="fr-kv">
                    <dt>Time</dt><dd>{fmtTs(selected.ts)}</dd>
                    <dt>Actor</dt><dd>{actorLabel(selected.actor_name, selected.actor) || '—'}</dd>
                    <dt>Target</dt><dd>{selected.target || '—'}</dd>
                    <dt>Patient</dt><dd>{selected.patient_id || '—'}</dd>
                    <dt>Correlation</dt><dd className="fr-mono">{selected.correlation_id || '—'}</dd>
                    <dt>Detail</dt><dd>{selected.detail || '—'}</dd>
                  </dl>
                  <div className="fr-raw-head">Raw record</div>
                  {detailLoading ? (
                    <div className="fr-empty">Loading…</div>
                  ) : (
                    <pre className="fr-raw">{JSON.stringify(detail?.event ?? selected, null, 2)}</pre>
                  )}
                </>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
