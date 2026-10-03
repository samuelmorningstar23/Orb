import { useState, useEffect, useCallback, useRef } from 'react'
import { getSageInsights, transitionInsight, actOnInsight, socket, type SageInsight } from '../../api/client'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import { datedDetail, raisedLine, type DatedInsight } from './insightTime'
import './SageInsightsFeed.css'

const SEV_DOT: Record<string, string> = {
  critical: 'var(--status-danger)',
  warning: 'var(--status-warn)',
  info: 'var(--status-info)',
}

/**
 * Sage Proactive — the "needs your attention" feed. Surveillance runs server-side;
 * this surfaces the insights live (socket + poll fallback) and lets the clinician
 * dismiss or one-click the suggested action into the agentic loop (a card in Relay).
 */
export default function SageInsightsFeed() {
  const [insights, setInsights] = useState<DatedInsight[]>([])
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const load = useCallback(async () => {
    try {
      const r = await getSageInsights()
      setInsights(r.insights || [])
    } catch {
      /* surveillance feed must never break the dashboard */
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    load()
    const onInsight = () => load()
    socket.on('sage:insight', onInsight)
    return () => {
      socket.off('sage:insight', onInsight)
      if (toastTimer.current) clearTimeout(toastTimer.current)
    }
  }, [load])
  // 20s poll, paused while the tab is backgrounded (socket push covers the
  // foreground; polling is only the fallback for missed events).
  useVisiblePolling(load, 20000)

  const flash = (msg: string) => {
    setToast(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 4500)
  }

  const handleDismiss = async (id: string) => {
    setBusy(id)
    setInsights(prev => prev.filter(i => i.id !== id)) // optimistic
    try { await transitionInsight(id, 'dismissed') } catch { load() }
    finally { setBusy(null) }
  }

  const handleAct = async (id: string, i: SageInsight) => {
    setBusy(id)
    try {
      await actOnInsight(id)
      setInsights(prev => prev.filter(x => x.id !== id))
      flash(`Drafted "${i.suggested_action_target || 'action'}" in ${i.patient_name || 'the patient'}'s case room: open Relay to confirm.`)
    } catch {
      load()
    } finally {
      setBusy(null)
    }
  }

  if (!insights.length && !toast) return null

  const visible = expanded ? insights : insights.slice(0, 4)

  return (
    <div className="sage-insights glass-card">
      <div className="sage-insights__header">
        <span className="sage-insights__title">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 2a7 7 0 0 0-7 7c0 2.4 1.2 4.1 2.5 5.3.8.8 1.5 1.6 1.5 2.7v1h6v-1c0-1.1.7-1.9 1.5-2.7C17.8 13.1 19 11.4 19 9a7 7 0 0 0-7-7Z" />
            <path d="M9 21h6" />
          </svg>
          Sage: needs your attention
        </span>
        {insights.length > 0 && <span className="sage-insights__count">{insights.length}</span>}
      </div>

      {toast && <div className="sage-insights__toast">✓ {toast}</div>}

      <div className="sage-insights__list">
        {visible.map(i => (
          <div key={i.id} className={`sage-insight sage-insight--${i.severity}`}>
            <span className="sage-insight__dot" style={{ background: SEV_DOT[i.severity] || SEV_DOT.info }} />
            <div className="sage-insight__body">
              <div className="sage-insight__title">{i.title}</div>
              {i.detail && <div className="sage-insight__detail">{datedDetail(i)}</div>}
              {raisedLine(i) && <div className="sage-insight__when">{raisedLine(i)}</div>}
            </div>
            <div className="sage-insight__actions">
              {/* No proactive rule suggests a drug; the server hides and refuses
                  any stored medication suggestion (sage_proactive_service). */}
              {i.suggested_action_type && (
                <button className="sage-insight__act" disabled={busy === i.id} onClick={() => handleAct(i.id, i)}>
                  ⚡ Draft action
                </button>
              )}
              <button className="sage-insight__dismiss" disabled={busy === i.id} onClick={() => handleDismiss(i.id)}>
                Dismiss
              </button>
            </div>
          </div>
        ))}
      </div>

      {insights.length > 4 && (
        <button className="sage-insights__more" onClick={() => setExpanded(e => !e)}>
          {expanded ? 'Show less' : `Show ${insights.length - 4} more`}
        </button>
      )}
    </div>
  )
}
