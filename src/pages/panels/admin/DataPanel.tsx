import { useState, useEffect, useCallback, useRef } from 'react'
import type { CSSProperties, FormEvent } from 'react'
import { apiFetch } from '../../../api/client'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import { formatClinicalDateTime } from '../../../lib/datetime'
import { usePlatformFlags } from '../../../shellConfig'
import { AdminLoadingState, AdminErrorState, AdminEmptyState } from './AdminEmptyState'
import WardRecordSection from './WardRecordSection'

/* ── Types ── */
interface BackupFile {
  filename: string
  size_bytes: number
  modified_at: string
}

// What a restore rolls back (server/routes/backup_routes.py _restore_scope,
// ruling MS-R12 (a)): the whole appliance, every facility on it.
interface RestoreScope {
  whole_appliance: boolean
  facilities_on_record: number
  statement: string
  facilities?: { id: number; code: string | null; name: string }[]
}

// Said when the server's own words are not to hand (the screen is fenced
// while a second facility's screens are unfinished, or the list failed).
const RESTORE_EVERY_FACILITY_TEXT =
  'A backup holds every facility on this appliance in one database. Restoring one puts '
  + 'every facility back to the moment it was taken, not just one: anything recorded at '
  + 'any facility after that moment is lost and must be entered again. One facility '
  + 'cannot be restored on its own.'

/** The restore statement to show, or null on a single-site install with
 *  ORB_MULTI_SITE off (where the screen stays as it was). */
function restoreStatement(
  scope: RestoreScope | null,
  platform: { multiSite: boolean; multiFacilityData: boolean },
): string | null {
  const many = (scope?.facilities_on_record ?? 1) > 1
  if (!many && !platform.multiSite && !platform.multiFacilityData) return null
  return many && scope?.statement ? scope.statement : RESTORE_EVERY_FACILITY_TEXT
}

// Mirrors services/consent_service.py: a request is recorded, then decided
// against retention_policies, then (separately) carried out.
type ErasureStatus = 'recorded' | 'decided' | 'executed'

interface ErasureRequest {
  id: string
  patient_id: string
  requested_at: string
  status: ErasureStatus
  note?: string | null
  decided_at?: string | null
  executed_at?: string | null
}

interface ErasureDecision {
  id: string
  category: string
  disposition: 'honoured' | 'refused' | 'not_applicable'
  reason: string
  authority: string | null
  executable: boolean
}

interface ErasureExecution {
  category: string
  rows_erased: number
}

interface ErasureDetail {
  request: ErasureRequest
  decisions: ErasureDecision[]
  executions: ErasureExecution[]
}

/* ── Formatting helpers ── */
function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let val = n / 1024
  let i = 0
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024
    i++
  }
  return `${val.toFixed(val >= 10 ? 0 : 1)} ${units[i]}`
}

function formatWhen(ts: string): string {
  return formatClinicalDateTime(ts) || ts
}

// The retention categories are database keys; the screen names them in words.
const CATEGORY_LABELS: Record<string, string> = {
  clinical_record: 'Medical record',
  medico_legal_record: 'Medico-legal record',
  prescription_and_dispensing: 'Prescriptions and dispensing',
  imaging: 'Imaging',
  audit_trail: 'Access log',
  breach_record: 'Breach register entry',
  consent_record: 'Consent record',
  ai_interaction_transcript: 'AI working notes',
  ambient_audio: 'Dictation audio',
  patient_portal_account: 'Patient portal login',
  non_clinical_contact: 'Contact details outside the record',
  marketing_communications: 'Outreach list',
}

function categoryLabel(key: string): string {
  if (CATEGORY_LABELS[key]) return CATEGORY_LABELS[key]
  const words = key.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

const STATUS_LABELS: Record<ErasureStatus, string> = {
  recorded: 'Awaiting decision',
  decided: 'Decided',
  executed: 'Carried out',
}

type Tone = 'ok' | 'info' | 'muted'

function badgeStyle(tone: Tone): CSSProperties {
  if (tone === 'muted') {
    return {
      background: 'var(--bg-glass)',
      color: 'var(--text-secondary)',
      border: '1px solid var(--border-glass)',
    }
  }
  return {
    background: `color-mix(in srgb, var(--status-${tone}) 10%, transparent)`,
    color: `var(--status-${tone}-text)`,
    border: `1px solid color-mix(in srgb, var(--status-${tone}) 28%, transparent)`,
  }
}

function decisionBadge(d: ErasureDecision): { label: string; tone: Tone } {
  if (d.disposition === 'refused') return { label: 'Kept by law', tone: 'muted' }
  if (d.disposition === 'honoured' && d.executable) return { label: 'Will be erased', tone: 'ok' }
  if (d.disposition === 'honoured') return { label: 'Granted, manual step', tone: 'info' }
  return { label: 'Not applicable', tone: 'muted' }
}

// .admin-filter-btn has no disabled look of its own; use the shared tokens.
function primaryStyle(disabled: boolean): CSSProperties | undefined {
  return disabled
    ? {
        background: 'var(--btn-disabled-bg)',
        color: 'var(--btn-disabled-text)',
        borderColor: 'var(--btn-disabled-border)',
        cursor: 'not-allowed',
      }
    : undefined
}

/* ── Create-backup confirm dialog ── */
function CreateBackupDialog({ busy, onConfirm, onClose }: {
  busy: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const ref = useFocusTrap<HTMLDivElement>(busy ? undefined : onClose)
  return (
    <div className="admin-modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose() }}>
      <div ref={ref} className="admin-modal admin-modal--sm" role="dialog" aria-modal="true" aria-labelledby="data-backup-title">
        <h3 id="data-backup-title" className="admin-modal__title">Create backup?</h3>
        <p className="admin-modal__text">
          A consistent snapshot of the live database will be written to the server's
          <code> backups/ </code> directory and integrity-checked. This is safe to run
          while the system is in use.
        </p>
        <div className="admin-modal__actions">
          <button type="button" className="admin-btn-ghost" onClick={onClose} disabled={busy} autoFocus>Cancel</button>
          <button type="button" className="admin-filter-btn" onClick={onConfirm} disabled={busy} style={primaryStyle(busy)}>
            {busy ? 'Creating…' : 'Create backup'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ── Carry-out confirmation (typed) ── */
function ExecuteDialog({ patientId, categories, busy, onConfirm, onClose }: {
  patientId: string
  categories: string[]
  busy: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const [typed, setTyped] = useState('')
  const ref = useFocusTrap<HTMLDivElement>(busy ? undefined : onClose)
  const phrase = `ERASE ${patientId}`
  const armed = typed.trim() === phrase

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (armed && !busy) onConfirm()
  }

  return (
    <div className="admin-modal-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose() }}>
      <div ref={ref} className="admin-modal admin-modal--sm" role="dialog" aria-modal="true" aria-labelledby="data-erase-title">
        <h3 id="data-erase-title" className="admin-modal__title">Erase the approved records?</h3>
        <p className="admin-modal__text">
          This permanently erases, for patient <strong>{patientId}</strong>:{' '}
          <strong>{categories.map(categoryLabel).join(', ')}</strong>. Everything marked
          "Kept by law" stays, including the medical record and the access log.{' '}
          <strong>This cannot be undone.</strong>
        </p>
        <form className="admin-form" onSubmit={submit}>
          <label className="admin-field">
            <span className="admin-field__label">
              Type <code>{phrase}</code> to confirm
            </span>
            <input
              className="admin-filter-input"
              autoFocus
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={phrase}
              spellCheck={false}
              autoCapitalize="off"
              autoCorrect="off"
            />
          </label>
          <div className="admin-modal__actions">
            <button type="button" className="admin-btn-ghost" onClick={onClose} disabled={busy}>Cancel</button>
            <button type="submit" className="admin-btn-danger" disabled={!armed || busy}>
              {busy ? 'Erasing…' : 'Erase approved records'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ── One erasure request: its decision, and the separate act of carrying it out ── */
function ErasureRequestView({ detail, busy, onAdjudicate, onExecute }: {
  detail: ErasureDetail
  busy: boolean
  onAdjudicate: () => void
  onExecute: () => void
}) {
  const { request, decisions, executions } = detail
  const executable = decisions.filter((d) => d.disposition === 'honoured' && d.executable)
  const erased = executions.reduce((n, e) => n + (e.rows_erased || 0), 0)

  return (
    <div className="glass-card" style={{ padding: 16, marginTop: 12 }} aria-label="Erasure request">
      <div className="admin-field__inline" style={{ justifyContent: 'space-between' }}>
        <span className="admin-section__title">
          Request {request.id} · patient {request.patient_id}
        </span>
        <span className="admin-badge" style={badgeStyle(request.status === 'executed' ? 'ok' : 'info')}>
          {STATUS_LABELS[request.status] ?? request.status}
        </span>
      </div>
      <p className="admin-page-info" style={{ marginTop: 4 }}>
        Opened {formatWhen(request.requested_at)}
        {request.note ? ` · ${request.note}` : ''}
      </p>

      {request.status === 'recorded' && (
        <>
          <p className="admin-modal__text" style={{ marginTop: 12 }}>
            The request is recorded. Nothing has been decided or erased yet. The next
            step checks each kind of record against what the hospital must keep by law.
          </p>
          <div style={{ marginTop: 12 }}>
            <button type="button" className="admin-filter-btn" onClick={onAdjudicate} disabled={busy} style={primaryStyle(busy)}>
              {busy ? 'Checking…' : 'Check against retention rules'}
            </button>
          </div>
        </>
      )}

      {decisions.length > 0 && (
        <>
          <div className="admin-table-wrap" style={{ marginTop: 12 }}>
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Record</th>
                  <th>Decision</th>
                  <th>Why</th>
                  <th>Rule that applies</th>
                </tr>
              </thead>
              <tbody>
                {decisions.map((d) => {
                  const b = decisionBadge(d)
                  return (
                    <tr key={d.id}>
                      <td>{categoryLabel(d.category)}</td>
                      <td><span className="admin-badge" style={badgeStyle(b.tone)}>{b.label}</span></td>
                      <td style={{ color: 'var(--text-secondary)' }}>{d.reason}</td>
                      <td style={{ color: 'var(--text-secondary)' }}>{d.authority || 'None'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <p className="admin-modal__text" style={{ marginTop: 12 }}>
            The request, this decision and anything erased because of it stay on the
            hospital's tamper-evident access log, which is never erased.
          </p>
        </>
      )}

      {request.status === 'decided' && (
        executable.length > 0 ? (
          <div style={{ marginTop: 12 }}>
            <button type="button" className="admin-btn-danger" onClick={onExecute} disabled={busy}>
              Erase approved records
            </button>
          </div>
        ) : (
          <p className="admin-modal__text" style={{ marginTop: 12 }}>
            Nothing in this request can be erased automatically. Follow the reason
            given for any record marked "Granted, manual step".
          </p>
        )
      )}

      {request.status === 'executed' && (
        <p role="status" className="admin-modal__text" style={{ marginTop: 12 }}>
          Carried out {request.executed_at ? formatWhen(request.executed_at) : ''}:{' '}
          {erased} item{erased === 1 ? '' : 's'} erased
          {executions.length > 0 ? ` (${executions.map((e) => categoryLabel(e.category)).join(', ')})` : ''}.
        </p>
      )}
    </div>
  )
}

/* ── Main panel ── */
export default function DataPanel() {
  const platform = usePlatformFlags()
  const [backups, setBackups] = useState<BackupFile[]>([])
  const [restoreScope, setRestoreScope] = useState<RestoreScope | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showCreate, setShowCreate] = useState(false)
  const [creating, setCreating] = useState(false)

  const [patientId, setPatientId] = useState('')
  const [note, setNote] = useState('')
  const [requests, setRequests] = useState<ErasureRequest[]>([])
  const [active, setActive] = useState<ErasureDetail | null>(null)
  const [erasureBusy, setErasureBusy] = useState(false)
  const [erasureError, setErasureError] = useState<string | null>(null)
  const [showExecute, setShowExecute] = useState(false)

  const [toast, setToast] = useState<string | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const flash = useCallback((msg: string) => {
    setToast(msg)
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3500)
  }, [])

  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current) }, [])

  const fetchBackups = useCallback(() => {
    setLoading(true)
    setError(null)
    apiFetch<{ backups: BackupFile[]; restore_scope?: RestoreScope }>('/admin/backups')
      .then((res) => {
        setBackups(res.backups)
        setRestoreScope(res.restore_scope ?? null)
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load backups'))
      .finally(() => setLoading(false))
  }, [])

  const fetchRequests = useCallback(() => {
    apiFetch<{ requests: ErasureRequest[] }>('/admin/compliance/erasure')
      .then((res) => setRequests(res.requests))
      .catch(() => setRequests([]))
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { fetchBackups(); fetchRequests() }, [fetchBackups, fetchRequests])

  const runCreate = async () => {
    setCreating(true)
    try {
      await apiFetch('/admin/backups', { method: 'POST' })
      setShowCreate(false)
      flash('Backup created')
      fetchBackups()
    } catch (e) {
      flash(e instanceof Error ? e.message : 'Backup failed')
    } finally {
      setCreating(false)
    }
  }

  // Every step re-reads the request, so the screen shows what the server sealed
  // rather than what this tab assumes happened.
  const step = async (work: () => Promise<string>, failure: string) => {
    setErasureBusy(true)
    setErasureError(null)
    try {
      const id = await work()
      setActive(await apiFetch<ErasureDetail>(`/admin/compliance/erasure/${encodeURIComponent(id)}`))
      fetchRequests()
    } catch (e) {
      setErasureError(e instanceof Error ? e.message : failure)
    } finally {
      setErasureBusy(false)
    }
  }

  const openRequest = (e: FormEvent) => {
    e.preventDefault()
    const id = patientId.trim()
    if (!id || erasureBusy) return
    void step(async () => {
      const req = await apiFetch<ErasureRequest>('/admin/compliance/erasure', {
        method: 'POST',
        body: JSON.stringify({ patient_id: id, note: note.trim() || null }),
      })
      setPatientId('')
      setNote('')
      flash('Erasure request recorded')
      return req.id
    }, 'Could not record the request')
  }

  const showRequest = (id: string) => step(async () => id, 'Could not load the request')

  const adjudicate = () => {
    if (!active) return
    const id = active.request.id
    void step(async () => {
      await apiFetch(`/admin/compliance/erasure/${encodeURIComponent(id)}/adjudicate`, { method: 'POST' })
      return id
    }, 'Could not check the request')
  }

  const execute = () => {
    if (!active) return
    const id = active.request.id
    void step(async () => {
      await apiFetch(`/admin/compliance/erasure/${encodeURIComponent(id)}/execute`, { method: 'POST' })
      flash('Approved records erased')
      return id
    }, 'Erasure failed').finally(() => setShowExecute(false))
  }

  const restoreText = restoreStatement(restoreScope, platform)

  return (
    <div className="admin-security">
      {/* ── Backups card ── */}
      <div className="admin-filter-bar glass-card" style={{ padding: '12px 16px' }}>
        <div className="admin-filter-row">
          <span className="admin-section__title">Database Backups</span>
          <span className="admin-page-info">{backups.length} backup{backups.length === 1 ? '' : 's'}</span>
        </div>
        <button className="admin-filter-btn" onClick={() => setShowCreate(true)} disabled={creating} style={primaryStyle(creating)}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
          Create backup
        </button>
      </div>

      {loading ? (
        <AdminLoadingState message="Loading backups…" />
      ) : error ? (
        <AdminErrorState error={error} onRetry={fetchBackups} />
      ) : backups.length === 0 ? (
        <AdminEmptyState title="No backups yet" message="Create the first snapshot to protect against data loss." />
      ) : (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Filename</th>
                <th>Size</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {backups.map((b) => (
                <tr key={b.filename}>
                  <td className="admin-td--mono admin-td--small" style={{ color: 'var(--text-primary)' }}>{b.filename}</td>
                  <td>{formatBytes(b.size_bytes)}</td>
                  <td style={{ color: 'var(--text-secondary)' }}>{formatWhen(b.modified_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="admin-modal__text" style={{ marginTop: 8, color: 'var(--text-muted)', fontSize: '0.82rem' }}>
        Restores are intentionally not available here. They must be performed
        out-of-band by an operator with the server stopped.
      </p>

      {restoreText && (
        <p
          className="admin-modal__text"
          role="note"
          aria-label="What a restore rolls back"
          style={{ marginTop: 8, color: 'var(--text-secondary)', fontSize: '0.82rem' }}
        >
          <strong>A restore rolls back every facility.</strong> {restoreText}
        </p>
      )}

      {/* ── Data erasure card ── */}
      <div className="admin-filter-bar glass-card" style={{ padding: '12px 16px', marginTop: 20 }}>
        <div className="admin-filter-row">
          <span className="admin-section__title">Data Erasure</span>
          <span className="admin-page-info">DPDP Act 2023, right to erasure</span>
        </div>
      </div>

      <div className="glass-card" style={{ padding: 16, marginTop: 8 }}>
        <p className="admin-modal__text" style={{ marginTop: 0 }}>
          Record a patient's request to have their data erased. Orb then checks each
          kind of record against what the hospital must keep by law: the medical
          record, prescriptions and the access log are kept, with the rule that
          requires it, while data held only with the patient's consent can be erased.
          Nothing is erased until you confirm that as a separate step.
        </p>
        <form className="admin-field__inline" style={{ marginTop: 12 }} onSubmit={openRequest}>
          <input
            className="admin-filter-input"
            value={patientId}
            onChange={(e) => { setPatientId(e.target.value); setErasureError(null) }}
            placeholder="Patient ID (e.g. P001)"
            aria-label="Patient ID"
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
          />
          <input
            className="admin-filter-input"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="How the request was made (optional)"
            aria-label="Note"
          />
          <button
            type="submit"
            className="admin-filter-btn"
            disabled={!patientId.trim() || erasureBusy}
            style={primaryStyle(!patientId.trim() || erasureBusy)}
          >
            Open erasure request
          </button>
        </form>

        {erasureError && (
          <p className="admin-form__error" role="alert" style={{ marginTop: 12 }}>{erasureError}</p>
        )}

        {active && (
          <ErasureRequestView
            detail={active}
            busy={erasureBusy}
            onAdjudicate={adjudicate}
            onExecute={() => setShowExecute(true)}
          />
        )}

        {requests.length > 0 && (
          <div className="admin-table-wrap" style={{ marginTop: 16 }}>
            <table className="admin-table" aria-label="Erasure requests">
              <thead>
                <tr>
                  <th>Opened</th>
                  <th>Patient</th>
                  <th>Status</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {requests.map((r) => (
                  <tr key={r.id}>
                    <td style={{ color: 'var(--text-secondary)' }}>{formatWhen(r.requested_at)}</td>
                    <td className="admin-td--mono admin-td--small">{r.patient_id}</td>
                    <td>{STATUS_LABELS[r.status] ?? r.status}</td>
                    <td>
                      <button
                        type="button"
                        className="admin-btn-ghost admin-btn-ghost--sm"
                        onClick={() => { void showRequest(r.id) }}
                        disabled={erasureBusy}
                      >
                        Open
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <WardRecordSection />

      {toast && <div className="admin-toast" role="status">{toast}</div>}

      {showCreate && (
        <CreateBackupDialog busy={creating} onConfirm={runCreate} onClose={() => setShowCreate(false)} />
      )}
      {showExecute && active && (
        <ExecuteDialog
          patientId={active.request.patient_id}
          categories={active.decisions.filter((d) => d.disposition === 'honoured' && d.executable).map((d) => d.category)}
          busy={erasureBusy}
          onConfirm={execute}
          onClose={() => setShowExecute(false)}
        />
      )}
    </div>
  )
}
