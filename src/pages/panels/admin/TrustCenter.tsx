import { useEffect, useMemo, useState } from 'react'
import { apiFetch } from '../../../api/client'
import './TrustCenter.css'
import { formatClinicalDateTime } from '../../../lib/datetime'
import { humaniseEnum } from '../../../lib/humanise'

/* ── Report shape (mirrors services/compliance_service.compliance_report) ── */
type ControlStatus = 'pass' | 'warn' | 'fail' | 'unknown'

interface Control {
  id: string
  name: string
  status: ControlStatus
  evidence: string
  remediation: string
}

interface MfaReport {
  required_roles: string[]
  by_role: Record<string, { total: number; enrolled: number }>
  total_users: number
  total_enrolled: number
  adoption_pct: number
  required_gap: string[]
}

interface RbacReport {
  roles: string[]
  permissions: string[]
  matrix: Record<string, Record<string, boolean>>
  role_count: number
  permission_count: number
}

interface AuditChain { verified: boolean; broken_at: number | null; count: number; unknown?: boolean }
interface AuditCoverage { total_events: number; by_category: Record<string, number>; categories: number }
interface Encryption {
  db_at_rest: boolean; uploads_at_rest: boolean
  db_encryption_key_set: boolean; upload_encryption_key_set: boolean
  upload_encryption_enforced?: boolean
}
interface Residency {
  host: string; loopback_only: boolean; cors_origins: string[]; cors_wildcard?: boolean
  deployment_profile: string; on_prem: boolean
}
interface Models { validated: number; total: number; unvalidated: number }

interface ComplianceReport {
  generated_at: string
  overall_score: number
  controls: Control[]
  mfa: MfaReport
  rbac_matrix: RbacReport
  audit_chain: AuditChain
  audit_coverage: AuditCoverage
  encryption: Encryption
  residency: Residency
  models: Models
}

const STATUS_LABEL: Record<ControlStatus, string> = {
  pass: 'Pass', warn: 'Warn', fail: 'Fail', unknown: 'Unknown',
}

/* The observed install type, in words. The server sends the effective profile
   key; "dev profile" in a CIO's header read as jargon. */
const PROFILE_LABEL: Record<string, string> = {
  prod: 'Production install', dev: 'Development install',
}

/* "patient.read_all" reads as "Patient: read all" in the matrix. */
function permissionLabel(perm: string): string {
  const [area, ...rest] = perm.split('.')
  const action = rest.join(' ')
  return action ? `${humaniseEnum(area)}: ${humaniseEnum(action).toLowerCase()}` : humaniseEnum(area)
}

const count = (n: number) => n.toLocaleString('en-IN')

/* Score ring color reflects posture bands (honest amber in the middle). */
function scoreColor(score: number): string {
  if (score >= 80) return 'var(--status-ok)'
  if (score >= 50) return 'var(--status-warn)'
  return 'var(--status-danger)'
}

function StatusChip({ status }: { status: ControlStatus }) {
  return (
    <span className={`tc-chip tc-chip--${status}`}>
      <span className="tc-chip__dot" />
      {STATUS_LABEL[status]}
    </span>
  )
}

/**
 * Compliance & Trust Center — a live, self-serve compliance-posture surface that
 * de-risks the enterprise sale. Aggregates REAL system state: encryption-at-rest,
 * the tamper-evident audit hash-chain, MFA adoption, the RBAC matrix, model-
 * validation honesty and an on-prem attestation into an auditor-style control
 * checklist with an overall trust score. Warn/fail statuses are shown honestly —
 * a posture surface is only trustworthy because it does not paint everything green.
 */
export default function TrustCenter() {
  const [report, setReport] = useState<ComplianceReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [rbacOpen, setRbacOpen] = useState(false)

  useEffect(() => {
    let alive = true
    apiFetch<ComplianceReport>('/admin/compliance/')
      .then(res => { if (alive) setReport(res) })
      .catch(e => { if (alive) setError(e?.message || 'Failed to load compliance report') })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [])

  const counts = useMemo(() => {
    const c = { pass: 0, warn: 0, fail: 0, unknown: 0 }
    report?.controls.forEach(ctl => { c[ctl.status] += 1 })
    return c
  }, [report])

  if (loading) return <div className="admin-empty">Loading compliance posture…</div>
  if (error) return <div className="admin-empty" role="alert">{error}</div>
  if (!report) return <div className="admin-empty">No compliance data available.</div>

  const { overall_score, controls, mfa, rbac_matrix, audit_chain, audit_coverage, encryption, residency, models } = report

  // Score ring geometry
  const R = 66
  const C = 2 * Math.PI * R
  const dash = (Math.max(0, Math.min(100, overall_score)) / 100) * C

  const auditState: 'verified' | 'broken' | 'unknown' =
    audit_chain.unknown ? 'unknown' : audit_chain.verified ? 'verified' : 'broken'

  const topCoverage = Object.entries(audit_coverage.by_category)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)

  return (
    <div className="trust-center">
      <p className="tc-intro">
        Orb's live compliance posture, aggregated from real system state, not a static
        PDF. Every control below is evaluated against the running deployment, and amber /
        red statuses are shown honestly so an auditor (or investor) can trust the greens.
      </p>

      {/* ── Hero: score ring + summary ── */}
      <div className="glass-card tc-hero">
        <div className="tc-ring" aria-label={`Overall trust score ${overall_score} of 100`}>
          <svg width="156" height="156" viewBox="0 0 156 156">
            <circle cx="78" cy="78" r={R} fill="none" stroke="var(--border-glass)" strokeWidth="12" />
            <circle
              cx="78" cy="78" r={R} fill="none" stroke={scoreColor(overall_score)} strokeWidth="12"
              strokeLinecap="round" strokeDasharray={`${dash} ${C}`}
              style={{ transition: 'stroke-dasharray 700ms cubic-bezier(0.4,0,0.2,1)' }}
            />
          </svg>
          <div className="tc-ring__center">
            <span className="tc-ring__score" style={{ color: scoreColor(overall_score) }}>{overall_score}</span>
            <span className="tc-ring__label">Trust Score</span>
          </div>
        </div>

        <div className="tc-hero__meta">
          <div>
            <h2 className="tc-hero__title">Compliance &amp; Trust Center</h2>
            <p className="tc-hero__sub">
              Generated {formatClinicalDateTime(report.generated_at)} · {PROFILE_LABEL[residency.deployment_profile] ?? 'Install type unknown'} · on-prem
            </p>
          </div>
          <div className="tc-stat-row">
            <div className="tc-stat"><span className="tc-stat__num" style={{ color: 'var(--status-ok)' }}>{counts.pass}</span><span className="tc-stat__lbl">Passing</span></div>
            <div className="tc-stat"><span className="tc-stat__num" style={{ color: 'var(--status-warn)' }}>{counts.warn}</span><span className="tc-stat__lbl">Warnings</span></div>
            <div className="tc-stat"><span className="tc-stat__num" style={{ color: 'var(--status-danger)' }}>{counts.fail}</span><span className="tc-stat__lbl">Failing</span></div>
            <div className="tc-stat"><span className="tc-stat__num">{models.validated}/{models.total}</span><span className="tc-stat__lbl">Models validated</span></div>
          </div>
        </div>
      </div>

      {/* ── Audit chain badge ── */}
      <div className="glass-card tc-section">
        <div className="tc-section__head">
          <div>
            <h3 className="tc-section__title">Tamper-evident audit log</h3>
            <p className="tc-section__hint">SHA-256 hash-chain over every audit row. Any altered or deleted row breaks the chain.</p>
          </div>
        </div>
        <div className="tc-audit">
          <span className={`tc-audit__badge tc-audit__badge--${auditState}`}>
            {auditState === 'verified' && (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/></svg>
            )}
            {auditState === 'broken' && (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="13"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            )}
            {auditState === 'verified' ? 'Verified, chain intact' : auditState === 'broken' ? 'Broken, tampering detected' : 'Unknown'}
          </span>
          <span className="tc-audit__meta">
            <b>{count(audit_chain.count)}</b> sealed entries checked
            {auditState === 'broken' && audit_chain.broken_at != null && <> · first break at entry <b>{audit_chain.broken_at}</b></>}
          </span>
        </div>
        {topCoverage.length > 0 && (
          <div className="tc-coverage">
            {topCoverage.map(([cat, n]) => (
              <span key={cat} className="tc-cov-pill"><b>{count(n)}</b> <span>{humaniseEnum(cat)}</span></span>
            ))}
          </div>
        )}
      </div>

      {/* ── Control checklist ── */}
      <div className="glass-card tc-section">
        <div className="tc-section__head">
          <h3 className="tc-section__title">Control checklist</h3>
        </div>
        <div className="tc-controls">
          {controls.map(ctl => (
            <div key={ctl.id} className="tc-control">
              <span className={`tc-control__rail tc-control__rail--${ctl.status}`} />
              <div className="tc-control__body">
                <p className="tc-control__name">{ctl.name}</p>
                <p className="tc-control__evidence">{ctl.evidence}</p>
                <p className="tc-control__remediation"><b>Remediation:</b> {ctl.remediation}</p>
              </div>
              <div className="tc-control__chip"><StatusChip status={ctl.status} /></div>
            </div>
          ))}
        </div>
      </div>

      {/* ── MFA adoption + posture mini-cards ── */}
      <div className="glass-card tc-section">
        <div className="tc-section__head">
          <div>
            <h3 className="tc-section__title">MFA adoption</h3>
            <p className="tc-section__hint">
              Second sign-in factor set up, by role · {count(mfa.total_enrolled)}/{count(mfa.total_users)} staff ({mfa.adoption_pct}%)
              {mfa.required_roles.length > 0 && <> · required for: {mfa.required_roles.map(r => humaniseEnum(r).toLowerCase()).join(', ')}</>}
            </p>
          </div>
        </div>
        <div className="tc-mfa">
          {Object.entries(mfa.by_role).map(([role, s]) => {
            const pct = s.total ? Math.round((s.enrolled / s.total) * 100) : 0
            const required = mfa.required_roles.includes(role)
            return (
              <div key={role} className="tc-mfa__row">
                <span className="tc-mfa__role">{humaniseEnum(role)}{required && <span className="tc-req">required</span>}</span>
                <span className="tc-mfa__track"><span className="tc-mfa__fill" style={{ width: `${pct}%` }} /></span>
                <span className="tc-mfa__val">{s.enrolled}/{s.total} · {pct}%</span>
              </div>
            )
          })}
          {Object.keys(mfa.by_role).length === 0 && <p className="tc-section__hint">No users to report.</p>}
        </div>

        <div className="tc-cards" style={{ marginTop: 18 }}>
          <div className="tc-mini">
            <p className="tc-mini__lbl">Encryption at rest</p>
            <p className="tc-mini__val">
              <StatusChip status={encryption.db_at_rest ? 'pass' : 'warn'} /> Database
            </p>
            <p className="tc-mini__val">
              <StatusChip status={encryption.uploads_at_rest ? 'pass' : 'warn'} /> PHI uploads
            </p>
            <p className="tc-mini__note">Database status is read from the open connection, not from settings.</p>
          </div>
          <div className="tc-mini">
            <p className="tc-mini__lbl">Model validation</p>
            <p className="tc-mini__val">{models.validated} of {models.total} validated</p>
            <p className="tc-mini__note">Orb never presents an untrained or unvalidated model as validated ({models.unvalidated} unvalidated, shown honestly).</p>
          </div>
          <div className="tc-mini">
            <p className="tc-mini__lbl">Data residency / on-prem</p>
            <p className="tc-mini__val">
              <StatusChip status={residency.loopback_only ? 'pass' : 'warn'} /> {residency.host}
            </p>
            <p className="tc-mini__note">
              Full stack (database, models, uploads) runs in-house.{' '}
              {residency.cors_wildcard
                ? 'Accepts browser requests from any web address.'
                : `Accepts browser requests from ${residency.cors_origins.length} named web address(es).`}
            </p>
          </div>
        </div>
      </div>

      {/* ── RBAC collapsible matrix ── */}
      <div className="glass-card tc-section">
        <div className="tc-section__head">
          <div>
            <h3 className="tc-section__title">RBAC matrix</h3>
            <p className="tc-section__hint">{rbac_matrix.role_count} roles × {rbac_matrix.permission_count} permissions · least-privilege, enforced per-route</p>
          </div>
          <button className="tc-rbac__toggle" onClick={() => setRbacOpen(o => !o)} aria-expanded={rbacOpen}>
            <svg className={`tc-rbac__caret ${rbacOpen ? 'tc-rbac__caret--open' : ''}`} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
            {rbacOpen ? 'Hide matrix' : 'Show matrix'}
          </button>
        </div>
        {rbacOpen && (
          <div className="tc-rbac__scroll">
            <table className="tc-matrix">
              <thead>
                <tr>
                  <th className="tc-matrix__corner tc-matrix__perm">Permission</th>
                  {rbac_matrix.roles.map(role => (
                    <th key={role} className="tc-matrix__role">{humaniseEnum(role)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rbac_matrix.permissions.map(perm => (
                  <tr key={perm}>
                    <th className="tc-matrix__perm" scope="row">{permissionLabel(perm)}</th>
                    {rbac_matrix.roles.map(role => (
                      <td key={role}>
                        {rbac_matrix.matrix[role]?.[perm]
                          ? <span className="tc-matrix__yes" aria-label="granted">✓</span>
                          : <span className="tc-matrix__no" aria-label="denied">·</span>}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
