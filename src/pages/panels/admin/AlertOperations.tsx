import { useState, useEffect, useCallback, useRef } from 'react'
import type { CSSProperties } from 'react'
import { apiFetch } from '../../../api/client'
import { AdminLoadingState, AdminErrorState, AdminEmptyState } from './AdminEmptyState'
import { formatClinicalDateTime } from '../../../lib/datetime'

/**
 * Every alert Vigil raised, from the same source the Overview's "Alert
 * Activity" tile counts (GET /admin/alerts). This tab used to read the audit
 * log's ingestion rows, which only the device-feed lane writes, so it said
 * "No alert events" while the Overview counted dozens.
 */

interface AlertRow {
  id: number
  episode_id: string
  occurred_at: string
  lane: string
  level: string | null
  raw_news2: number | null
  repeat: boolean
  patient_id: string | null
  ward: string | null
}

interface AlertsResponse {
  known: boolean
  unknown_reason: string | null
  total: number | null
  alerts: AlertRow[]
  by_lane: Record<string, number>
  scope: string | null
  house_total: number | null
  hours: number
}

// Lanes are database keys; the screen names them in words. The simulator lane
// is labelled as simulated because its vitals are.
const LANE_LABELS: Record<string, string> = {
  simulator: 'Simulated feed',
  ingestion: 'Device feed',
  manual: 'Nurse observations',
  chat: 'Chat',
  voice: 'Voice',
  sweeper: 'Escalation sweep',
}

function laneLabel(lane: string): string {
  return LANE_LABELS[lane] ?? lane.charAt(0).toUpperCase() + lane.slice(1)
}

function levelLabel(level: string | null): string {
  if (!level) return 'Not recorded'
  return level.charAt(0).toUpperCase() + level.slice(1)
}

function levelStyle(level: string | null): CSSProperties {
  const tone = level === 'critical' ? 'danger' : level === 'elevated' ? 'warn' : null
  if (!tone) {
    return { background: 'var(--bg-glass)', color: 'var(--text-secondary)', border: '1px solid var(--border-glass)' }
  }
  return {
    background: `color-mix(in srgb, var(--status-${tone}) 10%, transparent)`,
    color: `var(--status-${tone}-text)`,
    border: `1px solid color-mix(in srgb, var(--status-${tone}) 28%, transparent)`,
  }
}

const NEUTRAL_BADGE: CSSProperties = {
  background: 'var(--bg-glass)',
  color: 'var(--text-secondary)',
  border: '1px solid var(--border-glass)',
}

const LIMIT = 25

export default function AlertOperations() {
  const [data, setData] = useState<AlertsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const [laneFilter, setLaneFilter] = useState('')
  const [levelFilter, setLevelFilter] = useState('')
  // Only the newest request may paint, so a slow page never overwrites a later filter.
  const latest = useRef(0)

  const fetchData = useCallback(() => {
    const seq = ++latest.current
    setLoading(true)
    setError(null)
    const params = new URLSearchParams({ limit: String(LIMIT), offset: String(page * LIMIT) })
    if (laneFilter) params.set('lane', laneFilter)
    if (levelFilter) params.set('level', levelFilter)
    apiFetch<AlertsResponse>(`/admin/alerts?${params.toString()}`)
      .then((res) => { if (seq === latest.current) setData(res) })
      .catch((e) => { if (seq === latest.current) setError(e instanceof Error ? e.message : 'Could not load alerts') })
      .finally(() => { if (seq === latest.current) setLoading(false) })
  }, [page, laneFilter, levelFilter])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { fetchData() }, [fetchData])

  if (loading && !data) return <AdminLoadingState message="Loading alert activity..." />
  if (error) return <AdminErrorState error={error} onRetry={fetchData} />
  if (!data) return null

  // Unknown is a different answer from none: the instrument is off, not the ward quiet.
  if (!data.known) {
    return (
      <AdminEmptyState
        title="Alert activity unknown"
        message={data.unknown_reason ?? 'The alert log cannot be read, so this list is unknown, not empty.'}
      />
    )
  }

  const total = data.total ?? 0
  const totalPages = Math.ceil(total / LIMIT)
  const lanes = Object.keys(data.by_lane).sort()
  const windowText = `the last ${data.hours} hours`

  return (
    <div className="admin-alerts">
      <div className="admin-filter-bar glass-card" style={{ padding: '10px 16px' }}>
        <div className="admin-filter-row">
          <select
            className="admin-filter-select"
            aria-label="Lane"
            value={laneFilter}
            onChange={(e) => { setLaneFilter(e.target.value); setPage(0) }}
          >
            <option value="">All lanes</option>
            {Array.from(new Set([...lanes, ...(laneFilter ? [laneFilter] : [])])).map((l) => (
              <option key={l} value={l}>{laneLabel(l)}</option>
            ))}
          </select>
          <select
            className="admin-filter-select"
            aria-label="Level"
            value={levelFilter}
            onChange={(e) => { setLevelFilter(e.target.value); setPage(0) }}
          >
            <option value="">All levels</option>
            <option value="critical">Critical</option>
            <option value="elevated">Elevated</option>
          </select>
        </div>
        <div className="admin-filter-meta">
          <span>
            {total} alert{total !== 1 ? 's' : ''} in {windowText}
            {data.scope && data.house_total != null
              ? `, ${data.scope} only (${data.house_total} house-wide)`
              : ''}
          </span>
          {totalPages > 1 && (
            <div className="admin-pagination">
              <button className="admin-page-btn" aria-label="Previous page" disabled={page === 0 || loading} onClick={() => setPage(p => p - 1)}>‹</button>
              <span className="admin-page-info">{page + 1} / {totalPages}</span>
              <button className="admin-page-btn" aria-label="Next page" disabled={page >= totalPages - 1 || loading} onClick={() => setPage(p => p + 1)}>›</button>
            </div>
          )}
        </div>
      </div>

      {data.alerts.length === 0 ? (
        <AdminEmptyState
          title={data.scope ? `No alerts in ${data.scope}` : 'No alerts'}
          message={`Vigil raised no alerts matching these filters in ${windowText}.`}
          icon={<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>}
        />
      ) : (
        <div className="admin-table-wrap glass-card" style={{ padding: 0 }}>
          <table className="admin-table" aria-label="Alerts">
            <thead>
              <tr>
                <th>Time</th>
                <th>Lane</th>
                <th>Level</th>
                <th>NEWS2</th>
                <th>Patient</th>
                <th>Alert</th>
              </tr>
            </thead>
            <tbody>
              {data.alerts.map((a) => (
                <tr key={a.id}>
                  <td className="admin-td--mono admin-td--small">{formatClinicalDateTime(a.occurred_at, { seconds: true })}</td>
                  <td><span className="admin-badge" style={NEUTRAL_BADGE}>{laneLabel(a.lane)}</span></td>
                  <td><span className="admin-badge" style={levelStyle(a.level)}>{levelLabel(a.level)}</span></td>
                  <td className="admin-td--mono">{a.raw_news2 ?? 'Not recorded'}</td>
                  <td className="admin-td--mono admin-td--small">
                    {a.patient_id || 'Not recorded'}
                    {a.ward ? <span style={{ color: 'var(--text-secondary)' }}> · {a.ward}</span> : null}
                  </td>
                  <td style={{ color: 'var(--text-secondary)' }}>{a.repeat ? 'Repeat' : 'First'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
