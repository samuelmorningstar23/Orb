import { useState, useEffect, useCallback } from 'react'
import { apiFetch, getAuditLogs, type AuditFilters, type AuditLog } from '../../../api/client'
import { OutcomeBadge } from './AdminBadges'
import { AdminLoadingState, AdminErrorState, AdminEmptyState } from './AdminEmptyState'
import { humaniseEnum } from '../../../lib/humanise'
import { formatClinicalDateTime } from '../../../lib/datetime'
import './AuditViews.css'

interface Props {
  onTraceClick: (correlationId: string) => void
}

/** The filter lists, read from the trail itself (GET /api/audit/facets). They
 *  used to be hard-coded here and drifted: 'auth' matched no row while sign-ins
 *  are written as 'authentication', and newer actions were never offered. */
export interface AuditFacets {
  categories: string[]
  actions: string[]
  outcomes: string[]
}

/** Outcome filter value for the rows worth a second look. The server accepts a
 *  comma-separated list. */
export const FAILURES_AND_DENIALS = 'failure,denied'

/** A summary that opens to its full text. The cell used to clip at 320px with
 *  no way to read the rest, and on an audit trail the clipped part is often the
 *  part that matters. */
export function AuditSummaryCell({ text }: { text: string | null }) {
  const [open, setOpen] = useState(false)
  if (!text) return <td>—</td>
  return (
    <td className={open ? 'audit-summary-cell audit-summary-cell--open' : 'audit-summary-cell'}>
      <button
        type="button"
        className="audit-summary-toggle"
        aria-expanded={open}
        title={open ? 'Show less' : 'Show the full summary'}
        onClick={() => setOpen(o => !o)}
      >
        {text}
      </button>
    </td>
  )
}

/** Options for a facet select, keeping the current choice listed even when the
 *  narrowed facet list no longer contains it. */
function facetOptions(values: string[], current: string | undefined): string[] {
  return current && !values.includes(current) ? [current, ...values] : values
}

export default function AuditLogTable({ onTraceClick }: Props) {
  const [logs, setLogs] = useState<AuditLog[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filters, setFilters] = useState<AuditFilters>({ limit: 25, offset: 0 })
  // What is typed is a draft until Enter or the search button: fetching on
  // every keystroke ran one audit query per character of a correlation id.
  const [searchCorrelation, setSearchCorrelation] = useState('')
  const [searchPatient, setSearchPatient] = useState('')
  const [facets, setFacets] = useState<AuditFacets>({ categories: [], actions: [], outcomes: [] })

  const fetchLogs = useCallback(() => {
    setLoading(true)
    setError(null)
    getAuditLogs(filters)
      .then((res) => { setLogs(res.logs); setTotal(res.total) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [filters])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { fetchLogs() }, [fetchLogs])

  // The action list narrows to the chosen category. A failed facet read leaves
  // the lists empty rather than blocking the log itself.
  const category = filters.event_category
  useEffect(() => {
    let live = true
    const qs = category ? `?event_category=${encodeURIComponent(category)}` : ''
    apiFetch<AuditFacets>(`/audit/facets${qs}`)
      .then((res) => { if (live) setFacets(res) })
      .catch(() => {})
    return () => { live = false }
  }, [category])

  const updateFilter = (key: keyof AuditFilters, value: string) => {
    setFilters(prev => ({
      ...prev,
      [key]: value || undefined,
      // A new category can make the chosen action meaningless, so it resets.
      ...(key === 'event_category' ? { action_type: undefined } : {}),
      offset: 0,
    }))
  }

  const applySearch = () => {
    setFilters(prev => ({
      ...prev,
      correlation_id: searchCorrelation.trim() || undefined,
      patient_id: searchPatient.trim() || undefined,
      offset: 0,
    }))
  }

  const page = Math.floor((filters.offset || 0) / (filters.limit || 25))
  const totalPages = Math.ceil(total / (filters.limit || 25))

  const goPage = (p: number) => {
    setFilters(prev => ({ ...prev, offset: p * (prev.limit || 25) }))
  }

  const copyCorrelation = (id: string) => {
    navigator.clipboard.writeText(id).catch(() => {})
  }

  return (
    <div className="admin-audit-log">
      {/* ── Filter Bar ── */}
      <div className="admin-filter-bar glass-card" style={{ padding: '12px 16px' }}>
        <div className="admin-filter-row">
          <input
            className="admin-filter-input"
            placeholder="Correlation ID..."
            aria-label="Correlation ID"
            value={searchCorrelation}
            onChange={(e) => setSearchCorrelation(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && applySearch()}
          />
          <input
            className="admin-filter-input"
            placeholder="Patient ID..."
            aria-label="Patient ID"
            value={searchPatient}
            onChange={(e) => setSearchPatient(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && applySearch()}
          />
          <select className="admin-filter-select" aria-label="Category" value={filters.event_category || ''} onChange={(e) => updateFilter('event_category', e.target.value)}>
            <option value="">All categories</option>
            {facetOptions(facets.categories, filters.event_category).map(c => <option key={c} value={c}>{humaniseEnum(c)}</option>)}
          </select>
          <select className="admin-filter-select" aria-label="Action" value={filters.action_type || ''} onChange={(e) => updateFilter('action_type', e.target.value)}>
            <option value="">All actions</option>
            {facetOptions(facets.actions, filters.action_type).map(a => <option key={a} value={a}>{humaniseEnum(a)}</option>)}
          </select>
          <select className="admin-filter-select" aria-label="Outcome" value={filters.outcome || ''} onChange={(e) => updateFilter('outcome', e.target.value)}>
            <option value="">All outcomes</option>
            <option value={FAILURES_AND_DENIALS}>Failures and denials</option>
            {facets.outcomes.map(o => <option key={o} value={o}>{humaniseEnum(o)}</option>)}
          </select>
          <button className="admin-filter-btn" onClick={applySearch} title="Search" aria-label="Search">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          </button>
        </div>
        <div className="admin-filter-meta">
          <span>{total.toLocaleString()} event{total !== 1 ? 's' : ''}</span>
          {totalPages > 1 && (
            <div className="admin-pagination">
              <button className="admin-page-btn" disabled={page === 0} onClick={() => goPage(page - 1)}>‹</button>
              <span className="admin-page-info">{page + 1} / {totalPages}</span>
              <button className="admin-page-btn" disabled={page >= totalPages - 1} onClick={() => goPage(page + 1)}>›</button>
            </div>
          )}
        </div>
      </div>

      {/* ── Table ── */}
      {loading ? (
        <AdminLoadingState message="Loading audit logs..." />
      ) : error ? (
        <AdminErrorState error={error} onRetry={fetchLogs} />
      ) : logs.length === 0 ? (
        <AdminEmptyState
          title="No audit events found"
          message="Try adjusting your filters or wait for new events"
          icon={<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>}
        />
      ) : (
        <div className="admin-table-wrap glass-card" style={{ padding: 0 }}>
          <table className="admin-table">
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Correlation</th>
                <th>Category</th>
                <th>Action</th>
                <th>Outcome</th>
                <th>Actor</th>
                <th>Patient</th>
                <th>Summary</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log) => (
                <tr key={log.id}>
                  <td className="admin-td--mono admin-td--small">{formatClinicalDateTime(log.timestamp, { seconds: true })}</td>
                  <td>
                    <span className="admin-correlation-cell">
                      <button
                        className="admin-correlation-link"
                        onClick={() => onTraceClick(log.correlation_id)}
                        title="View full trace"
                      >
                        {log.correlation_id?.slice(0, 10)}…
                      </button>
                      <button
                        className="admin-copy-btn"
                        onClick={() => copyCorrelation(log.correlation_id)}
                        title="Copy correlation ID"
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                      </button>
                    </span>
                  </td>
                  {/* `Phi_access` / `Ai_inference` / `Api_mutation` were the raw
                      enum with a capitalised first letter. This is the CIO-facing
                      screen; PHI and AI are acronyms and should read like it. */}
                  <td><span className="admin-category-chip">{humaniseEnum(log.event_category)}</span></td>
                  <td className="admin-td--small">{humaniseEnum(log.action_type)}</td>
                  <td><OutcomeBadge outcome={log.outcome} /></td>
                  <td className="admin-td--small">{log.actor_user_id || '—'}</td>
                  <td className="admin-td--mono admin-td--small">{log.patient_id || '—'}</td>
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
