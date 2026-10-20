import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../../../api/client'
import { actorLabel } from '../../../lib/actorLabel'
import { OutcomeBadge } from './AdminBadges'
import { AdminLoadingState, AdminErrorState, AdminEmptyState } from './AdminEmptyState'
import {
  AuditSummaryCell, FAILURES_AND_DENIALS, FacilityFilter, type AuditFacets,
} from './AuditLogTable'
import { facilityLabel, fetchAuditPage, type AuditFacility, type AuditLogRow } from './auditFacility'
import { humaniseEnum } from '../../../lib/humanise'
import { formatClinicalDateTime } from '../../../lib/datetime'
import { usePlatformFlags } from '../../../shellConfig'

// THREE categories, not one. Sign-ins are audited as 'authentication',
// permission denials as 'authorization', lockouts/posture changes as
// 'security'. Asking for 'auth' matched none of them, so this tab said
// "No security events" while Overview counted 39 + 3 on the same screen.
const SECURITY_CATEGORIES = 'authentication,authorization,security'

export default function SecurityActivity() {
  const [logs, setLogs] = useState<AuditLogRow[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  // Opens on failures and denials: unfiltered, the two failed sign-ins worth
  // a look sat somewhere in fifteen thousand successful ones.
  const [outcome, setOutcome] = useState(FAILURES_AND_DENIALS)
  const [action, setAction] = useState('')
  const [actions, setActions] = useState<string[]>([])
  // multisite-11: a facility column and filter while more than one facility
  // is on record (see AuditLogTable).
  const { multiFacilityData } = usePlatformFlags()
  const [facility, setFacility] = useState<number | undefined>(undefined)
  const [facilities, setFacilities] = useState<AuditFacility[]>([])
  const showFacility = multiFacilityData || facilities.length > 0
  const limit = 25

  const fetchData = useCallback(() => {
    setLoading(true)
    setError(null)
    fetchAuditPage({
      event_category: SECURITY_CATEGORIES,
      outcome: outcome || undefined,
      action_type: action || undefined,
      limit,
      offset: page * limit,
    }, facility)
      .then((res) => { setLogs(res.logs); setTotal(res.total); setFacilities(res.facilities ?? []) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [page, outcome, action, facility])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { fetchData() }, [fetchData])

  // The action list is whatever these three categories actually hold. A failed
  // read leaves it empty rather than blocking the tab.
  useEffect(() => {
    let live = true
    apiFetch<AuditFacets>(`/audit/facets?event_category=${encodeURIComponent(SECURITY_CATEGORIES)}`)
      .then((res) => { if (live) setActions(res.actions) })
      .catch(() => {})
    return () => { live = false }
  }, [])

  const totalPages = Math.ceil(total / limit)
  const filtered = Boolean(outcome || action || facility !== undefined)

  return (
    <div className="admin-security">
      <div className="admin-filter-bar glass-card" style={{ padding: '10px 16px' }}>
        <div className="admin-filter-row">
          <select
            className="admin-filter-select"
            aria-label="Outcome"
            value={outcome}
            onChange={(e) => { setOutcome(e.target.value); setPage(0) }}
          >
            <option value={FAILURES_AND_DENIALS}>Failures and denials</option>
            <option value="">All outcomes</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
            <option value="denied">Denied</option>
          </select>
          <select
            className="admin-filter-select"
            aria-label="Action"
            value={action}
            onChange={(e) => { setAction(e.target.value); setPage(0) }}
          >
            <option value="">All actions</option>
            {actions.map(a => <option key={a} value={a}>{humaniseEnum(a)}</option>)}
          </select>
          {showFacility && (
            <FacilityFilter
              facilities={facilities}
              value={facility}
              onChange={(next) => { setFacility(next); setPage(0) }}
            />
          )}
        </div>
        <div className="admin-filter-meta" style={{ width: '100%', justifyContent: 'space-between' }}>
          <span>
            {total.toLocaleString()} {filtered ? 'matching ' : ''}security event{total !== 1 ? 's' : ''}
          </span>
          {totalPages > 1 && (
            <div className="admin-pagination">
              <button className="admin-page-btn" disabled={page === 0} onClick={() => setPage(p => p - 1)}>‹</button>
              <span className="admin-page-info">{page + 1} / {totalPages}</span>
              <button className="admin-page-btn" disabled={page >= totalPages - 1} onClick={() => setPage(p => p + 1)}>›</button>
            </div>
          )}
        </div>
      </div>

      {loading ? (
        <AdminLoadingState message="Loading security events..." />
      ) : error ? (
        <AdminErrorState error={error} onRetry={fetchData} />
      ) : logs.length === 0 ? (
        <AdminEmptyState
          title={filtered ? 'No matching security events' : 'No security events'}
          message={
            outcome === FAILURES_AND_DENIALS && !action
              ? 'No failed sign-ins, refusals or denied requests. Choose All outcomes to see every event.'
              : filtered
                ? 'Nothing matches these filters.'
                : 'No authentication or permission events recorded yet'
          }
          icon={<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>}
        />
      ) : (
        <div className="admin-table-wrap glass-card" style={{ padding: 0 }}>
          <table className="admin-table">
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Action</th>
                <th>Outcome</th>
                <th>Actor</th>
                <th>Role</th>
                <th>IP Address</th>
                <th>Path</th>
                {showFacility && <th>Facility</th>}
                <th>Summary</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log) => (
                <tr key={log.id} className={log.outcome === 'failure' ? 'admin-tr--failure' : ''}>
                  <td className="admin-td--mono admin-td--small">{formatClinicalDateTime(log.timestamp, { seconds: true })}</td>
                  <td className="admin-td--small">{humaniseEnum(log.action_type)}</td>
                  <td><OutcomeBadge outcome={log.outcome} /></td>
                  <td className="admin-td--small">{actorLabel(log.actor_name, log.actor_user_id) || 'Not recorded'}</td>
                  <td className="admin-td--small">{humaniseEnum(log.actor_role)}</td>
                  <td className="admin-td--mono admin-td--small">{log.ip_address || '—'}</td>
                  <td className="admin-td--small">{log.request_path || '—'}</td>
                  {showFacility && <td className="admin-td--small">{facilityLabel(log.facility)}</td>}
                  <AuditSummaryCell text={log.summary_message} />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
