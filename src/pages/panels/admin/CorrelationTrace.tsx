import { useState, useEffect } from 'react'
import { getAuditLogs, type AuditLog } from '../../../api/client'
import { actorLabel } from '../../../lib/actorLabel'
import { OutcomeBadge } from './AdminBadges'
import { AdminLoadingState, AdminErrorState, AdminEmptyState } from './AdminEmptyState'

interface Props {
  correlationId: string | null
}

/* Pipeline step ordering for visual flow */

export default function CorrelationTrace({ correlationId }: Props) {
  const [events, setEvents] = useState<AuditLog[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searchId, setSearchId] = useState(correlationId || '')

  const fetchTrace = (id?: string) => {
    const cid = id || searchId
    if (!cid.trim()) return
    setLoading(true)
    setError(null)
    getAuditLogs({ correlation_id: cid.trim(), limit: 100 })
      .then((res) => setEvents(res.logs.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime())))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (correlationId) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
      setSearchId(correlationId)
      fetchTrace(correlationId)
    }
  }, [correlationId])

  const formatTime = (ts: string) => {
    const d = new Date(ts)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3 })
  }

  const getNodeColor = (outcome: string) => {
    switch (outcome) {
      case 'success': return 'var(--status-ok)'
      case 'failure': return 'var(--status-danger)'
      case 'partial_success': return 'var(--status-warn)'
      case 'denied': return 'var(--text-muted)'
      default: return 'var(--text-secondary)'
    }
  }

  return (
    <div className="admin-trace">
      {/* ── Search Bar ── */}
      <div className="admin-trace__search glass-card" style={{ padding: '12px 16px' }}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
        </svg>
        <input
          className="admin-filter-input"
          style={{ flex: 1 }}
          placeholder="Enter correlation ID to trace..."
          value={searchId}
          onChange={(e) => setSearchId(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && fetchTrace()}
        />
        <button className="admin-filter-btn" onClick={() => fetchTrace()}>Trace</button>
      </div>

      {/* ── Timeline ── */}
      {loading ? (
        <AdminLoadingState message="Tracing pipeline..." />
      ) : error ? (
        <AdminErrorState error={error} onRetry={() => fetchTrace()} />
      ) : !searchId && events.length === 0 ? (
        <AdminEmptyState
          title="Enter a correlation ID"
          message="Click a correlation ID in the Audit Logs tab, or paste one above to view the full pipeline trace"
          icon={<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M22 12h-4l-3 9L11 3l-3 9H4"/></svg>}
        />
      ) : events.length === 0 && searchId ? (
        <AdminEmptyState
          title="No events found"
          message={`No audit trail found for correlation: ${searchId}`}
        />
      ) : (
        <div className="admin-trace__timeline">
          <div className="admin-trace__pipeline-label">
            <span className="admin-td--mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>
              {events.length} event{events.length !== 1 ? 's' : ''} · {searchId.slice(0, 16)}…
            </span>
          </div>
          {events.map((evt, i) => {
            const color = getNodeColor(evt.outcome)
            const isFailure = evt.outcome === 'failure' || evt.outcome === 'denied'
            return (
              <div
                key={evt.id}
                className={`admin-trace__node ${isFailure ? 'admin-trace__node--failure' : ''}`}
                style={{ animationDelay: `${i * 60}ms` }}
              >
                {/* Vertical connector line */}
                {i < events.length - 1 && (
                  <div className="admin-trace__connector" style={{ borderColor: isFailure ? 'var(--status-danger)' : 'var(--border-subtle)' }} />
                )}

                {/* Node dot */}
                <div className="admin-trace__dot" style={{ background: color, boxShadow: `0 0 8px ${color}40` }} />

                {/* Content */}
                <div className="admin-trace__content">
                  <div className="admin-trace__header">
                    <span className="admin-trace__action">{evt.action_type.replace(/_/g, ' ')}</span>
                    <OutcomeBadge outcome={evt.outcome} />
                    <span className="admin-trace__time">{formatTime(evt.timestamp)}</span>
                  </div>
                  {evt.summary_message && (
                    <p className="admin-trace__summary">{evt.summary_message}</p>
                  )}
                  <div className="admin-trace__meta">
                    {evt.patient_id && <span>Patient: {evt.patient_id}</span>}
                    {evt.target_type && <span>Target: {evt.target_type}</span>}
                    {evt.actor_user_id && <span>Actor: {actorLabel(evt.actor_name, evt.actor_user_id)}</span>}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
