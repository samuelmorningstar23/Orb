import { useState, useEffect, useCallback } from 'react'
import { getIngestionStatus, type IngestionEvent } from '../../../api/client'
import { SourceBadge, ParseStatusBadge } from './AdminBadges'
import { AdminLoadingState, AdminErrorState, AdminEmptyState } from './AdminEmptyState'
import { formatClinicalDateTime } from '../../../lib/datetime'
import QuarantineQueue from '../../../components/lab/QuarantineQueue'

/* Where the vitals on this appliance come from, in words. The raw setting
   ("simulation", "hybrid") printed here as-is. */
const MODE_LABEL: Record<string, string> = {
  ingestion: 'Live monitor feed',
  hybrid: 'Monitor feed plus simulated vitals',
  simulation: 'Simulated vitals only',
}

const STATUS_WORD: Record<string, string> = {
  parsed: 'parsed', failed: 'failed', duplicate: 'duplicate',
}

export default function IngestionMonitor() {
  const [events, setEvents] = useState<IngestionEvent[]>([])
  const [mode, setMode] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<string>('')

  const fetchData = useCallback(() => {
    setLoading(true)
    setError(null)
    getIngestionStatus(50, statusFilter || undefined)
      .then((res) => { setEvents(res.events); setMode(res.ingestion_mode) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [statusFilter])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { fetchData() }, [fetchData])

  if (loading) return <AdminLoadingState message="Loading monitor data…" />
  if (error) return <AdminErrorState error={error} onRetry={fetchData} />

  return (
    <div className="admin-ingestion">
      {/* ── Filter Row ── */}
      <div className="admin-filter-bar glass-card" style={{ padding: '12px 16px' }}>
        <div className="admin-filter-row">
          <span className="admin-filter-label">Vitals source: <strong style={{ color: 'var(--accent-text)' }}>{MODE_LABEL[mode] ?? 'Not reported'}</strong></span>
          <select className="admin-filter-select" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All statuses</option>
            <option value="parsed">Parsed</option>
            <option value="failed">Failed</option>
            <option value="duplicate">Duplicate</option>
          </select>
          <button className="admin-filter-btn" onClick={fetchData} title="Refresh">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
          </button>
        </div>
        <div className="admin-filter-meta">
          <span>{events.length.toLocaleString('en-IN')} event{events.length !== 1 ? 's' : ''}</span>
        </div>
      </div>

      {events.length === 0 ? (
        <AdminEmptyState
          title="No monitor data received"
          message={statusFilter
            ? `No payloads were ${STATUS_WORD[statusFilter] ?? statusFilter} in this window.`
            // On simulated vitals nothing arrives from a device, so an empty
            // list is the expected state, not a broken feed.
            : mode === 'simulation'
              ? 'Orb is running on simulated vitals, so no monitor or device data has arrived. Payloads appear here once a monitor feed is connected.'
              : 'No payload has arrived from a monitor or device yet.'}
          icon={<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>}
        />
      ) : (
        <div className="admin-table-wrap glass-card" style={{ padding: 0 }}>
          <table className="admin-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Source</th>
                <th>Source name</th>
                <th>Status</th>
                <th>Vitals</th>
                <th>Observations</th>
                <th>Correlation</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td className="admin-td--mono admin-td--small">{formatClinicalDateTime(e.received_at, { seconds: true })}</td>
                  <td><SourceBadge source={e.source_type} /></td>
                  <td className="admin-td--small">{e.source_name || '—'}</td>
                  <td><ParseStatusBadge status={e.parse_status} /></td>
                  <td style={{ textAlign: 'center', fontVariantNumeric: 'tabular-nums' }}>{e.vitals_inserted ?? 0}</td>
                  <td style={{ textAlign: 'center', fontVariantNumeric: 'tabular-nums' }}>{e.observations_inserted ?? 0}</td>
                  <td className="admin-td--mono admin-td--small">{e.correlation_id?.slice(0, 12)}…</td>
                  <td className="admin-td--truncate" style={{ color: e.parse_error ? 'var(--status-danger)' : 'var(--text-muted)' }}>{e.parse_error || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Results that matched no patient (lis-19), read only: counts and a list
          with no identifiers or values. Laboratory staff decide them on the
          Laboratory screen. Renders nothing while the queue is off and empty. */}
      <QuarantineQueue mode="monitor" />
    </div>
  )
}
