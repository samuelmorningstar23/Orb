import './ProvenanceReview.css'
import { useState, useEffect, useCallback } from 'react'
import { apiFetch } from '../../../api/client'
import { AdminLoadingState, AdminErrorState, AdminEmptyState } from './AdminEmptyState'
import { formatClinicalDateTime } from '../../../lib/datetime'

/* ─── Types (mirror routes/sage_routes.py provenance responses) ─── */

export interface ProvenanceTurn {
  turn_id: string
  occurred_at: string
  lane: string
  outcome: string
  actor_user_id: string | null
  actor_role: string | null
  department: string | null
  hospital_wide: boolean
  patient_id: string | null
  patient_context_used: boolean
  query_scope: string | null
  model_served: string | null
  provider: string | null
  thinking: boolean | null
  safety_floor: boolean
  safety_floor_unsatisfied: boolean
  safety_floor_state: string
  retrieved_doc_count: number
  citation_basis: string | null
  confidence: string | null
  escalation: boolean
  refusal_class: string | null
  retrieval_ms: number | null
  ttft_ms: number | null
  total_ms: number | null
}

interface ProvenanceListResponse {
  turns: ProvenanceTurn[]
  count: number
  scope: { department: string | null; hospital_wide: boolean }
}

interface ProvenanceGuideline {
  document_id: string | null
  title: string | null
  source: string | null
  passage: string | null
}

export interface ProvenanceReplay {
  turn_id: string
  occurred_at: string
  lane: string
  outcome: string
  actor: { user_id: string | null; role: string | null }
  scope: {
    department: string | null
    hospital_wide: boolean
    patient_id: string | null
    patient_context_used: boolean
    query_scope: string | null
  }
  question: string
  patient_context: Record<string, unknown> | null
  guidelines: ProvenanceGuideline[]
  model: {
    requested: string | null
    served: string | null
    provider: string | null
    thinking: boolean | null
    thinking_requested: boolean | null
    thinking_tier: string | null
    thinking_reason: string | null
    safety_floor: boolean
    safety_floor_rules: string[]
    safety_floor_unsatisfied: boolean
    safety_floor_state: string
  }
  answer: {
    text: string
    cited_document_ids: string[]
    citation_basis: string | null
    confidence: string | null
    escalation: boolean
    action_status: string | null
    refusal_class: string | null
  }
  raw_model_output: string
  retrieved_document_ids: (string | null)[]
  timing: { retrieval_ms: number | null; ttft_ms: number | null; total_ms: number | null }
  integrity: { prompt_sha256: string | null; schema_v: number; missing_blobs: string[] }
}

/* ─── Safety-floor tri-state badge ───
   The distinction this page exists to show: 'not fired' is a verdict (the floor
   looked and let the turn through); 'not evaluated' means nothing ever looked
   (voice lane, pre-LLM refusal); 'unknown' is a row recorded before the store
   distinguished the two, and is never re-badged as either. */
const FLOOR_STYLES: Record<string, { bg: string; color: string; border: string; label: string }> = {
  fired:         { bg: 'color-mix(in srgb, var(--status-warn) 10%, transparent)', color: 'var(--status-warn-text)', border: 'color-mix(in srgb, var(--status-warn) 28%, transparent)', label: 'Fired' },
  not_fired:     { bg: 'color-mix(in srgb, var(--status-ok) 10%, transparent)',   color: 'var(--status-ok-text)',   border: 'color-mix(in srgb, var(--status-ok) 28%, transparent)',   label: 'Did not fire' },
  not_evaluated: { bg: 'color-mix(in srgb, var(--text-muted) 10%, transparent)',  color: 'var(--text-muted)',       border: 'color-mix(in srgb, var(--text-muted) 28%, transparent)',  label: 'Not evaluated' },
  // The operator switched the floor off on this appliance (ORB_FAST_MODE_SAFETY_FLOOR=0).
  disabled:      { bg: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', color: 'var(--status-danger-text)', border: 'color-mix(in srgb, var(--status-danger) 28%, transparent)', label: 'Switched off' },
  unknown:       { bg: 'color-mix(in srgb, var(--text-muted) 10%, transparent)',  color: 'var(--text-muted)',       border: 'color-mix(in srgb, var(--text-muted) 28%, transparent)',  label: 'Unknown (legacy)' },
}

export function FloorStateBadge({ state }: { state: string }) {
  const s = FLOOR_STYLES[state] || FLOOR_STYLES.unknown
  return (
    <span className="admin-badge" style={{ background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
      {s.label}
    </span>
  )
}

const OUTCOME_COLORS: Record<string, string> = {
  answered: 'var(--status-ok-text)',
  refused: 'var(--status-warn-text)',
  failed: 'var(--status-danger-text)',
  cancelled: 'var(--text-muted)',
}

/* ─── Detail sub-views ─── */

function DetailField({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="prov-field">
      <span className="prov-field__label">{label}</span>
      <span className={`prov-field__value ${mono ? 'prov-field__value--mono' : ''}`}>{value ?? '—'}</span>
    </div>
  )
}

function TurnDetail({ replay, onBack }: { replay: ProvenanceReplay; onBack: () => void }) {
  const m = replay.model
  return (
    <div className="prov-detail">
      <div className="prov-detail__bar">
        <button className="prov-back-btn" onClick={onBack}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
          <span>All turns</span>
        </button>
        <span className="prov-detail__id">{replay.turn_id}</span>
      </div>

      <div className="prov-section glass-card">
        <h4 className="prov-section__title">Turn</h4>
        <div className="prov-field-grid">
          <DetailField label="Time" value={formatClinicalDateTime(replay.occurred_at, { seconds: true })} mono />
          <DetailField label="Outcome" value={<span style={{ color: OUTCOME_COLORS[replay.outcome] || 'var(--text-secondary)' }}>{replay.outcome}</span>} />
          <DetailField label="Lane" value={replay.lane} />
          <DetailField label="User" value={replay.actor.user_id ? `${replay.actor.user_id} (${replay.actor.role || '?'})` : '—'} mono />
          <DetailField label="Department" value={replay.scope.hospital_wide ? 'Hospital-wide account' : replay.scope.department || '—'} />
          <DetailField label="Patient" value={replay.scope.patient_id || '—'} mono />
          <DetailField label="Query scope" value={replay.scope.query_scope || '—'} mono />
          {replay.answer.refusal_class && <DetailField label="Refusal class" value={replay.answer.refusal_class} mono />}
        </div>
      </div>

      <div className="prov-section glass-card">
        <h4 className="prov-section__title">Model decision</h4>
        <div className="prov-field-grid">
          <DetailField label="Served" value={m.served || '—'} mono />
          <DetailField label="Provider" value={m.provider || '—'} mono />
          <DetailField label="Thinking" value={m.thinking === null ? '—' : m.thinking ? 'on' : 'off'} />
          <DetailField label="Thinking requested" value={m.thinking_requested === null ? '—' : m.thinking_requested ? 'yes' : 'no'} />
          {m.thinking_reason && <DetailField label="Routing reason" value={m.thinking_reason} mono />}
          <DetailField label="Safety floor" value={<FloorStateBadge state={m.safety_floor_state} />} />
          {m.safety_floor_rules.length > 0 && (
            <DetailField label="Floor rules" value={m.safety_floor_rules.join(', ')} mono />
          )}
        </div>
      </div>

      <div className="prov-section glass-card">
        <h4 className="prov-section__title">Question, verbatim</h4>
        <pre className="prov-pre">{replay.question || '—'}</pre>
      </div>

      <div className="prov-section glass-card">
        <h4 className="prov-section__title">Answer as delivered</h4>
        <pre className="prov-pre">{replay.answer.text || '—'}</pre>
        <div className="prov-field-grid">
          <DetailField label="Cited documents" value={replay.answer.cited_document_ids.length ? replay.answer.cited_document_ids.join(', ') : '—'} mono />
          <DetailField label="Citation basis" value={replay.answer.citation_basis || '—'} mono />
          <DetailField label="Confidence" value={replay.answer.confidence || '—'} />
        </div>
      </div>

      {replay.raw_model_output && replay.raw_model_output !== replay.answer.text && (
        <div className="prov-section glass-card">
          <h4 className="prov-section__title">Raw model output (before the guardrail strip)</h4>
          <pre className="prov-pre">{replay.raw_model_output}</pre>
        </div>
      )}

      <div className="prov-section glass-card">
        <h4 className="prov-section__title">
          Retrieved guidelines ({replay.guidelines.length})
        </h4>
        {replay.guidelines.length === 0 ? (
          <p className="prov-none">No passages were retrieved for this turn.</p>
        ) : (
          replay.guidelines.map((g, i) => (
            <details key={`${g.document_id || 'doc'}-${i}`} className="prov-guideline">
              <summary className="prov-guideline__summary">
                <span className="prov-guideline__doc">{g.document_id || '—'}</span>
                <span className="prov-guideline__title">{g.title || 'Untitled'}</span>
              </summary>
              <pre className="prov-pre">{g.passage || '(passage blob missing)'}</pre>
            </details>
          ))
        )}
      </div>

      {replay.patient_context && (
        <div className="prov-section glass-card">
          <h4 className="prov-section__title">Chart snapshot, as the model saw it</h4>
          <details className="prov-guideline">
            <summary className="prov-guideline__summary">
              <span className="prov-guideline__title">Frozen patient context (stored by value at the time of the turn)</span>
            </summary>
            <pre className="prov-pre">{JSON.stringify(replay.patient_context, null, 2)}</pre>
          </details>
        </div>
      )}

      <div className="prov-section glass-card">
        <h4 className="prov-section__title">Integrity</h4>
        <div className="prov-field-grid">
          <DetailField label="Prompt SHA-256" value={replay.integrity.prompt_sha256 || '—'} mono />
          <DetailField label="Record schema" value={`v${replay.integrity.schema_v}`} mono />
          <DetailField
            label="Missing blobs"
            value={replay.integrity.missing_blobs.length
              ? <span style={{ color: 'var(--status-danger-text)' }}>{replay.integrity.missing_blobs.join(', ')}</span>
              : 'none, record is complete'}
          />
        </div>
      </div>
    </div>
  )
}

/* ─── The page ─── */

export default function ProvenanceReview() {
  const [turns, setTurns] = useState<ProvenanceTurn[]>([])
  const [scope, setScope] = useState<{ department: string | null; hospital_wide: boolean } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [replay, setReplay] = useState<ProvenanceReplay | null>(null)
  const [replayLoading, setReplayLoading] = useState(false)
  const [replayError, setReplayError] = useState<string | null>(null)

  const fetchTurns = useCallback(() => {
    setLoading(true)
    setError(null)
    apiFetch<ProvenanceListResponse>('/sage/provenance?limit=100')
      .then((res) => { setTurns(res.turns); setScope(res.scope) })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { fetchTurns() }, [fetchTurns])

  const openTurn = (turnId: string) => {
    setReplayLoading(true)
    setReplayError(null)
    apiFetch<ProvenanceReplay>(`/sage/provenance/${turnId}`)
      .then(setReplay)
      .catch((e) => setReplayError(e.message))
      .finally(() => setReplayLoading(false))
  }

  if (replayLoading) return <AdminLoadingState message="Reconstructing turn from stored evidence..." />
  if (replayError) return <AdminErrorState error={replayError} onRetry={() => { setReplayError(null) }} />
  if (replay) return <TurnDetail replay={replay} onBack={() => setReplay(null)} />

  return (
    <div className="prov-review">
      <div className="admin-filter-bar glass-card" style={{ padding: '12px 16px' }}>
        <div className="admin-filter-meta">
          <span>
            {turns.length.toLocaleString()} recorded turn{turns.length !== 1 ? 's' : ''}
            {scope && !scope.hospital_wide && scope.department ? `, scoped to ${scope.department}` : ''}
          </span>
          {/* Opening a turn reads its stored chart snapshot; the server writes a
              per-patient phi_access audit row for every replay. Said here so an
              auditor is never surprised by their own audit trail. */}
          <span className="prov-review__note">Read-only. Opening a turn reads stored PHI and is audited.</span>
        </div>
      </div>

      {loading ? (
        <AdminLoadingState message="Loading recorded turns..." />
      ) : error ? (
        <AdminErrorState error={error} onRetry={fetchTurns} />
      ) : turns.length === 0 ? (
        <AdminEmptyState
          title="No recorded turns"
          message="Sage turns appear here as clinicians use the assistant"
          icon={<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><line x1="10" y1="12" x2="14" y2="12"/></svg>}
        />
      ) : (
        <div className="admin-table-wrap glass-card" style={{ padding: 0 }}>
          <table className="admin-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Turn</th>
                <th>User</th>
                <th>Department</th>
                <th>Patient</th>
                <th>Model</th>
                <th>Safety floor</th>
                <th>Retrieval</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {turns.map((t) => (
                <tr key={t.turn_id}>
                  <td className="admin-td--mono admin-td--small">{formatClinicalDateTime(t.occurred_at, { seconds: true })}</td>
                  <td>
                    <button
                      className="admin-correlation-link"
                      onClick={() => openTurn(t.turn_id)}
                      title="Replay this turn from its stored evidence (audited)"
                    >
                      {t.turn_id}
                    </button>
                  </td>
                  <td className="admin-td--mono admin-td--small">{t.actor_user_id || '—'}</td>
                  <td className="admin-td--small">{t.hospital_wide ? 'Hospital-wide' : t.department || '—'}</td>
                  <td className="admin-td--mono admin-td--small">{t.patient_id || '—'}</td>
                  <td className="admin-td--mono admin-td--small">
                    {t.model_served || '—'}
                    {t.thinking === true ? ' +thinking' : ''}
                  </td>
                  <td><FloorStateBadge state={t.safety_floor_state} /></td>
                  <td className="admin-td--mono admin-td--small">
                    {t.retrieved_doc_count > 0 ? `${t.retrieved_doc_count} doc${t.retrieved_doc_count !== 1 ? 's' : ''}` : '—'}
                  </td>
                  <td className="admin-td--small">
                    <span style={{ color: OUTCOME_COLORS[t.outcome] || 'var(--text-secondary)' }}>{t.outcome}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
