import { useState, useEffect } from 'react'
import { getAdminOverview } from '../../../api/client'
import { OutcomeBadge, SourceBadge, ParseStatusBadge } from './AdminBadges'
import { AdminLoadingState, AdminErrorState } from './AdminEmptyState'
import { humaniseEnum } from '../../../lib/humanise'
import { formatClinicalDateTime } from '../../../lib/datetime'

/* Counts in Indian grouping (1,23,456), the way the hospital writes them. */
const count = (n: number | null | undefined) => (n ?? 0).toLocaleString('en-IN')

export default function AdminOverview() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchData = () => {
    setLoading(true)
    setError(null)
    getAdminOverview()
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { fetchData() }, [])

  if (loading) return <AdminLoadingState message="Loading overview…" />
  if (error) return <AdminErrorState error={error} onRetry={fetchData} />
  if (!data) return null

  const modeBadge = data.ingestion_mode === 'hybrid' ? 'Hybrid' : data.ingestion_mode === 'ingestion' ? 'Ingestion' : 'Simulation'
  const modeColor = data.ingestion_mode === 'hybrid' ? 'var(--status-warn)' : data.ingestion_mode === 'ingestion' ? 'var(--status-ok)' : 'var(--text-muted)'

  // ── UNKNOWN IS NOT ZERO ────────────────────────────────────────────────────
  // The server sends `total: null` for a headline it cannot know (the shadow
  // instrument switched off; the simulator's retention cap having pruned rows
  // out of the window). Rendering that as 0 would restore the exact falsehood
  // the server side was fixed to stop telling — on an admin dashboard a zero is
  // a claim that nothing happened. `??`/`||` are BOTH wrong here: `total ?? 0`
  // reintroduces the zero and `total || 0` also swallows a genuine 0. The two
  // states must render as different words.
  //
  // De-emphasis is colour + type scale (--text-muted), never opacity — house
  // rule; a translucent number on a governance page is a number a reviewer
  // squints at and then trusts anyway.
  const alerts = data.alerts ?? {}
  const alertsKnown = typeof alerts.total === 'number'
  const records = data.records_written ?? {}
  const recordsKnown = typeof records.total === 'number'
  const unknownValueStyle = { color: 'var(--text-muted)', fontSize: '1.3rem' }

  return (
    <div className="admin-overview">
      {/* ── Top Stat Cards ── */}
      <div className="admin-cards-grid">
        <div className="admin-stat-card glass-card">
          <div className="admin-stat-card__label">Ingestion Mode</div>
          <div className="admin-stat-card__value" style={{ color: modeColor }}>{modeBadge}</div>
          <div className="admin-stat-card__sub">
            Audit simulation: {data.audit_simulation_enabled ? 'On' : 'Off'}
          </div>
        </div>

        <div className="admin-stat-card glass-card">
          <div className="admin-stat-card__label">Audit Events (24h)</div>
          <div className="admin-stat-card__value">{count(data.audit_total_24h)}</div>
          <div className="admin-stat-card__sub">
            {Object.entries(data.audit_categories as Record<string, number>).map(([cat, cnt]) => (
              <span key={cat} className="admin-stat-chip">{humaniseEnum(cat)}: {count(cnt)}</span>
            ))}
          </div>
        </div>

        <div className="admin-stat-card glass-card">
          <div className="admin-stat-card__label">Ingested Payloads (24h)</div>
          <div className="admin-stat-card__value">{count(data.ingestion.total)}</div>
          <div className="admin-stat-card__sub">
            <span className="admin-stat-chip admin-stat-chip--ok">{count(data.ingestion.parsed)} parsed</span>
            {data.ingestion.failed > 0 && <span className="admin-stat-chip admin-stat-chip--danger">{count(data.ingestion.failed)} failed</span>}
            {data.ingestion.duplicates > 0 && <span className="admin-stat-chip admin-stat-chip--warn">{count(data.ingestion.duplicates)} duplicates</span>}
          </div>
        </div>

        <div className="admin-stat-card glass-card">
          <div className="admin-stat-card__label">Source Distribution</div>
          <div className="admin-stat-card__value" style={{ fontSize: '1.3rem' }}>
            {Object.keys(data.source_mix).length} source{Object.keys(data.source_mix).length !== 1 ? 's' : ''}
          </div>
          <div className="admin-stat-card__sub">
            {Object.entries(data.source_mix as Record<string, number>).map(([src, cnt]) => (
              <span key={src} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, marginRight: 8 }}>
                <SourceBadge source={src} /> {count(cnt as number)}
              </span>
            ))}
          </div>
        </div>

        <div className="admin-stat-card glass-card">
          <div className="admin-stat-card__label">Alert Activity (24h)</div>
          {alertsKnown ? (
            <div className="admin-stat-card__value" style={{ color: alerts.total > 0 ? 'var(--status-danger)' : 'var(--text-muted)' }}>
              {count(alerts.total)}
            </div>
          ) : (
            <div className="admin-stat-card__value" style={unknownValueStyle}>Unknown</div>
          )}
          <div className="admin-stat-card__sub">
            {alertsKnown ? (
              <>
                {/* Named per LANE, so a lane that produced alerts is visible
                    rather than folded into a total that could have missed it. */}
                {Object.entries((alerts.by_lane ?? {}) as Record<string, number>).map(([lane, cnt]) => (
                  <span key={lane} className="admin-stat-chip">{humaniseEnum(lane)}: {count(cnt)}</span>
                ))}
                {(alerts.by_level?.critical ?? 0) > 0 && (
                  <span className="admin-stat-chip admin-stat-chip--danger">{count(alerts.by_level.critical)} critical</span>
                )}
              </>
            ) : (
              <span className="admin-stat-chip admin-stat-chip--warn">{alerts.unknown_reason}</span>
            )}
          </div>
        </div>

        <div className="admin-stat-card glass-card">
          <div className="admin-stat-card__label">Records Written (24h)</div>
          {recordsKnown ? (
            <div className="admin-stat-card__value">{count(records.total)}</div>
          ) : (
            <div className="admin-stat-card__value" style={unknownValueStyle}>Unknown</div>
          )}
          <div className="admin-stat-card__sub">
            {recordsKnown ? (
              <>
                <span className="admin-stat-chip">{count(records.vitals_counted)} vitals</span>
                <span className="admin-stat-chip">{count(records.observations_counted)} observations</span>
              </>
            ) : (
              <>
                {/* The floor is still worth showing — but as a floor, labelled
                    "at least", never as the headline total. */}
                <span className="admin-stat-chip">at least {count(records.counted)} recorded</span>
                <span className="admin-stat-chip admin-stat-chip--warn">{records.incomplete_reason}</span>
              </>
            )}
          </div>
        </div>
      </div>

      {/* ── Recent Parse Failures ── */}
      {data.recent_failures.length > 0 && (
        <div className="admin-section">
          <h3 className="admin-section__title">Recent Parse Failures</h3>
          <div className="admin-table-wrap glass-card" style={{ padding: 0 }}>
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Source</th>
                  <th>Error</th>
                  <th>Correlation</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_failures.map((f: any) => (
                  <tr key={f.id}>
                    {/* received_at is a bare UTC timestamp; formatClinicalDateTime
                        reads it as UTC, where new Date() read it as local. */}
                    <td className="admin-td--small">{formatClinicalDateTime(f.received_at)}</td>
                    <td><SourceBadge source={f.source_type} /></td>
                    <td className="admin-td--truncate">{f.parse_error || '—'}</td>
                    <td className="admin-td--mono admin-td--small">{f.correlation_id?.slice(0, 12)}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Auth Activity Summary ── */}
      {data.auth_events.length > 0 && (
        <div className="admin-section">
          <h3 className="admin-section__title">Auth Summary (24h)</h3>
          <div className="admin-auth-summary">
            {data.auth_events.map((e: any, i: number) => (
              <div key={i} className="admin-auth-chip glass-card" style={{ padding: '12px 16px' }}>
                <span>{humaniseEnum(e.action)}</span>
                <OutcomeBadge outcome={e.outcome} />
                <span className="admin-auth-chip__count">{count(e.count)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
