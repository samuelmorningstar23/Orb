/** Roster import — the admin surface for `server/routes/roster_import_routes.py`.
 *
 *  The backend was complete and tested; nothing in `src/` called it, so the only
 *  way to get a ward list into Orb was to admit patients one at a time. This is
 *  the three-step wizard the backend was designed for:
 *
 *      preview (sniff)  →  dry run (plan)  →  commit (apply)
 *
 *  Two rules drive the whole design and neither is cosmetic:
 *
 *  1. ALL THREE STEPS POST THE SAME FILE. The commit recomputes the plan
 *     server-side from the uploaded bytes; it never trusts a plan the client is
 *     holding. So this component keeps the File across steps and re-sends it.
 *     It must never offer an "apply what I'm showing" button that skips that.
 *
 *  2. THE DRY RUN IS THE PROMISE. Whatever the dry run shows for a given
 *     file + mapping is exactly what commit writes. So the commit button is
 *     disabled until a dry run has been seen for the CURRENT mapping — changing
 *     the mapping invalidates it, because otherwise an admin could read one
 *     plan and commit a different one.
 *
 *  `absent_from_roster` is displayed but never actionable: a patient missing
 *  from today's file has not been discharged, and offering a "discharge these"
 *  button next to that list would be the single most dangerous control in Orb.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  commitRoster,
  dryRunRoster,
  getRosterFields,
  listRosterRuns,
  previewRoster,
  type RosterCommit,
  type RosterDryRun,
  type RosterFieldCatalogue,
  type RosterPreview,
  type RosterRun,
} from '../../../api/rosterImport'
import { AdminEmptyState, AdminErrorState, AdminLoadingState } from './AdminEmptyState'
import { formatClinicalDateTime } from '../../../lib/datetime'

/** Not mapped to any Orb field. Distinct from "" so the select has a real value. */
const UNMAPPED = '__unmapped__'

function issueTone(severity: string): string {
  const s = (severity || '').toLowerCase()
  if (s === 'error' || s === 'fatal') return 'var(--status-danger-text)'
  if (s === 'warning' || s === 'warn') return 'var(--status-warn-text)'
  return 'var(--text-secondary)'
}

function actionClass(action: string): string {
  const a = (action || '').toLowerCase()
  if (a === 'create') return 'admin-stat-chip admin-stat-chip--ok'
  if (a === 'update') return 'admin-stat-chip admin-stat-chip--warn'
  return 'admin-stat-chip'
}

/* Words for the server's keys. The counts used to render as the raw keys
   ("3 total_rows", "2c · 0u · 0= · 1s") and "1 errors" was painted green. */
const COUNT_LABELS: Record<string, [string, string]> = {
  create: ['new patient', 'new patients'],
  update: ['patient updated', 'patients updated'],
  unchanged: ['patient unchanged', 'patients unchanged'],
  skip: ['row skipped', 'rows skipped'],
  total_rows: ['row in the file', 'rows in the file'],
  errors: ['error', 'errors'],
  warnings: ['warning', 'warnings'],
  admissions_create: ['admission opened', 'admissions opened'],
  admissions_update: ['admission updated', 'admissions updated'],
  admissions_close: ['admission closed', 'admissions closed'],
  allergies_add: ['allergy to chart', 'allergies to chart'],
  dates_left_blank: ['date left blank', 'dates left blank'],
  // The commit summary's keys.
  created: ['patient created', 'patients created'],
  updated: ['patient updated', 'patients updated'],
  skipped: ['row skipped', 'rows skipped'],
}

function countLabel(key: string, n: number): string {
  const words = COUNT_LABELS[key]
  if (!words) return `${n} ${key.replace(/_/g, ' ')}`
  return `${n} ${n === 1 ? words[0] : words[1]}`
}

/** Errors are danger, warnings and blanked dates are warn, and only when
 *  there is at least one. New patients read as good news. */
function countClass(key: string, n: number): string {
  if (n > 0 && key === 'errors') return 'admin-stat-chip admin-stat-chip--danger'
  if (n > 0 && (key === 'warnings' || key === 'dates_left_blank')) return 'admin-stat-chip admin-stat-chip--warn'
  if (n > 0 && (key === 'create' || key === 'created')) return 'admin-stat-chip admin-stat-chip--ok'
  return 'admin-stat-chip'
}

const ACTION_LABELS: Record<string, string> = {
  create: 'New', update: 'Update', unchanged: 'No change', skip: 'Skip',
}

const REASON_LABELS: Record<string, string> = {
  field_count_mismatch: 'Columns out of line (a comma inside a value?)',
  missing_mrn: 'No MRN',
  missing_name: 'No name',
  name_too_short: 'Name too short',
  duplicate_mrn_in_file: 'MRN repeated in the file',
  already_exists: 'Already in Orb, and updating is off',
  no_field_changed: 'Nothing changed',
}

const ADMISSION_LABELS: Record<string, string> = {
  create: 'Opens an admission',
  update: 'Updates the admission',
  unchanged: 'Already admitted',
  close: 'Closes the admission',
  none: 'None',
}

const DATE_ORDER_LABELS: Record<string, string> = {
  dmy: 'Day / Month / Year (India)',
  mdy: 'Month / Day / Year',
  auto: 'Work it out per date (ambiguous dates left blank)',
}

function humanCode(code: string | null | undefined, labels: Record<string, string>): string {
  if (!code) return ''
  return labels[code] ?? code.replace(/_/g, ' ')
}

export default function RosterImport() {
  const [catalogue, setCatalogue] = useState<RosterFieldCatalogue | null>(null)
  const [catalogueError, setCatalogueError] = useState<string | null>(null)

  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<RosterPreview | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [headerLine, setHeaderLine] = useState<number | undefined>(undefined)
  // Day first: the pilot site is in India, where exports write 03/04 for
  // 3 April. 'auto' left every such date blank.
  const [dateOrder, setDateOrder] = useState('dmy')
  /** Apply is held while the dry run would blank any date, until the admin
   *  accepts that or changes the date order (which re-runs the dry run). */
  const [blankDatesAccepted, setBlankDatesAccepted] = useState(false)
  const [updateExisting, setUpdateExisting] = useState(true)
  const [defaultDepartment, setDefaultDepartment] = useState('')
  const [note, setNote] = useState('')

  const [dryRun, setDryRun] = useState<RosterDryRun | null>(null)
  /** The mapping the displayed dry run was computed for. If the admin edits the
   *  mapping afterwards, this no longer matches and commit re-locks — see rule 2. */
  const [dryRunKey, setDryRunKey] = useState<string | null>(null)
  const [committed, setCommitted] = useState<RosterCommit | null>(null)

  const [busy, setBusy] = useState<null | 'preview' | 'dry-run' | 'commit'>(null)
  const [error, setError] = useState<string | null>(null)
  const [runs, setRuns] = useState<RosterRun[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)

  const mappingKey = useMemo(
    () => JSON.stringify({ mapping, dateOrder, updateExisting, defaultDepartment, headerLine }),
    [mapping, dateOrder, updateExisting, defaultDepartment, headerLine],
  )
  const dryRunIsCurrent = dryRun !== null && dryRunKey === mappingKey
  const datesLeftBlank = dryRun?.counts?.dates_left_blank ?? 0

  const loadRuns = useCallback(async () => {
    try {
      const r = await listRosterRuns(20)
      setRuns(r.runs || [])
    } catch {
      /* History is context, not the job — a failure here must not block an import. */
    }
  }, [])

  useEffect(() => {
    getRosterFields().then(setCatalogue).catch(e => setCatalogueError(e.message))
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    loadRuns()
  }, [loadRuns])

  /** Everything downstream of the file is invalidated when the file changes. */
  function resetFrom(step: 'file' | 'mapping') {
    setError(null)
    setCommitted(null)
    setDryRun(null)
    setDryRunKey(null)
    setBlankDatesAccepted(false)
    if (step === 'file') {
      setPreview(null)
      setMapping({})
      setHeaderLine(undefined)
    }
  }

  async function runPreview(f: File, line?: number) {
    setBusy('preview')
    setError(null)
    try {
      const p = await previewRoster(f, line)
      setPreview(p)
      setHeaderLine(p.header_line)
      // The editor is keyed by HEADER (one row per column in the file), but the
      // server's suggested_mapping is orb_field -> header. Invert it once here
      // so the rest of the component only ever thinks in header -> orb_field.
      const seeded: Record<string, string> = {}
      for (const h of p.headers) {
        if (h) seeded[h] = UNMAPPED
      }
      for (const [field, header] of Object.entries(p.suggested_mapping || {})) {
        if (header && header in seeded) seeded[header] = field
      }
      setMapping(seeded)
    } catch (e) {
      setError((e as Error).message)
      setPreview(null)
    } finally {
      setBusy(null)
    }
  }

  function onFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0]
    if (!f) return
    resetFrom('file')
    setFile(f)
    runPreview(f)
  }

  /** Send only the columns the admin actually mapped, as orb_field -> header. */
  function buildMapping(): Record<string, string> {
    const out: Record<string, string> = {}
    for (const [header, field] of Object.entries(mapping)) {
      if (!field || field === UNMAPPED) continue
      out[field] = header
    }
    return out
  }

  async function runDryRun() {
    if (!file) return
    setBusy('dry-run')
    setError(null)
    setCommitted(null)
    setBlankDatesAccepted(false)
    try {
      const key = mappingKey
      const plan = await dryRunRoster(file, buildMapping(), {
        dateOrder,
        updateExisting,
        headerLine,
        defaultDepartment: defaultDepartment || undefined,
      })
      setDryRun(plan)
      setDryRunKey(key)
    } catch (e) {
      setError((e as Error).message)
      setDryRun(null)
      setDryRunKey(null)
    } finally {
      setBusy(null)
    }
  }

  async function runCommit() {
    if (!file || !dryRunIsCurrent || (datesLeftBlank > 0 && !blankDatesAccepted)) return
    setBusy('commit')
    setError(null)
    try {
      const result = await commitRoster(file, buildMapping(), {
        dateOrder,
        updateExisting,
        headerLine,
        defaultDepartment: defaultDepartment || undefined,
        note: note || undefined,
      })
      setCommitted(result)
      loadRuns()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  if (catalogueError) return <AdminErrorState error={catalogueError} />
  if (!catalogue) return <AdminLoadingState message="Loading field catalogue…" />

  const required = catalogue.required || []
  const missingRequired = required.filter(f => !Object.values(mapping).includes(f))
  const counts = dryRun?.counts || {}
  // Every row issue, flattened, so a "3 warnings" chip always has the three
  // warnings beneath it.
  const rowIssues = (dryRun?.rows || []).flatMap(r => r.issues || [])
  const fieldNote = (fieldName?: string) =>
    catalogue.fields.find(f => f.name === fieldName)?.note || undefined
  /** Required fields are named by id in `catalogue.required`; show their labels. */
  const labelFor = (fieldName: string) =>
    catalogue.fields.find(f => f.name === fieldName)?.label || fieldName

  return (
    <div className="admin-roster">
      <div className="admin-section">
        <h3 className="admin-section__title">Roster import</h3>
        <p className="admin-section__hint">
          Load a ward list from CSV. Nothing is written until you commit, and the
          commit only ever writes what the dry run showed you.
        </p>
      </div>

      {/* ── Step 1 — the file ─────────────────────────────────────────── */}
      <div className="admin-section">
        <div className="admin-roster__step">
          <span className="admin-roster__step-num">1</span>
          <h4 className="admin-roster__step-title">Choose the roster file</h4>
        </div>
        <div className="admin-filter-row">
          {/* The native picker is unstyleable, so it is visually hidden and out
              of the tab order; the button opens it. */}
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,text/csv,text/plain"
            className="sr-only"
            tabIndex={-1}
            onChange={onFileChosen}
            aria-label="Roster CSV file"
          />
          <button
            type="button"
            className="admin-filter-btn"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy !== null}
          >
            {file ? 'Choose a different CSV file' : 'Choose CSV file'}
          </button>
          {file && (
            <span className="admin-filter-meta">
              {file.name} · {(file.size / 1024).toFixed(1)} KB
            </span>
          )}
        </div>
        <p className="admin-filter-meta">
          Limits: {catalogue.max_rows.toLocaleString()} rows,{' '}
          {(catalogue.max_bytes / 1024 / 1024).toFixed(1)} MB.
        </p>
        {busy === 'preview' && <AdminLoadingState message="Reading the file…" />}
      </div>

      {/* ── Step 2 — header + mapping ─────────────────────────────────── */}
      {preview && (
        <div className="admin-section">
          <div className="admin-roster__step">
            <span className="admin-roster__step-num">2</span>
            <h4 className="admin-roster__step-title">Map the columns</h4>
          </div>

          <p className="admin-filter-meta">
            Delimiter <code>{preview.delimiter === '\t' ? '\\t' : preview.delimiter}</code> ·{' '}
            {preview.row_count} data rows
            {preview.rejected_row_count > 0 && ` · ${preview.rejected_row_count} unreadable`}
          </p>

          {/* The sniffer asks rather than guesses when the header is ambiguous. */}
          {!preview.header_confident && (
            <div className="admin-roster__ask">
              <p className="admin-roster__ask-title">Which line holds the column names?</p>
              <div className="admin-filter-row">
                {preview.header_candidates.map(c => (
                  <button
                    key={c.line}
                    className={`admin-filter-btn ${headerLine === c.line ? 'admin-filter-btn--active' : ''}`}
                    onClick={() => { resetFrom('file'); setHeaderLine(c.line); if (file) runPreview(file, c.line) }}
                  >
                    Line {c.line}: {c.recognised_columns.length} recognised
                  </button>
                ))}
              </div>
            </div>
          )}

          {missingRequired.length > 0 && (
            <p className="admin-roster__required" style={{ color: 'var(--status-warn-text)' }}>
              Still unmapped and required: {missingRequired.map(labelFor).join(', ')}
            </p>
          )}

          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Column in file</th>
                  <th>Sample</th>
                  <th>Orb field</th>
                </tr>
              </thead>
              <tbody>
                {preview.headers.filter(Boolean).map(h => (
                  <tr key={h}>
                    <td className="admin-td--mono">{h}</td>
                    <td className="admin-td--small admin-td--truncate">
                      {preview.sample_rows.map(r => r[h]).filter(Boolean).slice(0, 2).join(' · ') || '—'}
                    </td>
                    <td>
                      {/* `title` carries the catalogue's note — the only place
                          that explains, e.g., that a wrong `department` hides
                          the patient from that department's boards. */}
                      <select
                        className="admin-filter-select"
                        value={mapping[h] ?? UNMAPPED}
                        onChange={e => {
                          setMapping(m => ({ ...m, [h]: e.target.value }))
                          resetFrom('mapping')
                        }}
                        aria-label={`Orb field for column ${h}`}
                        title={fieldNote(mapping[h])}
                      >
                        <option value={UNMAPPED}>(not imported)</option>
                        {catalogue.fields.map(f => (
                          <option key={f.name} value={f.name}>
                            {f.label}{f.required ? ' *' : ''}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="admin-filter-row">
            <label className="admin-filter-label">
              Date order
              <select
                className="admin-filter-select"
                value={dateOrder}
                onChange={e => { setDateOrder(e.target.value); resetFrom('mapping') }}
              >
                {catalogue.date_orders.map(d => <option key={d} value={d}>{DATE_ORDER_LABELS[d] ?? d}</option>)}
              </select>
            </label>
            <label className="admin-filter-label">
              Default department
              <input
                className="admin-filter-input"
                value={defaultDepartment}
                placeholder="e.g. General Medicine"
                onChange={e => { setDefaultDepartment(e.target.value); resetFrom('mapping') }}
              />
            </label>
            <label className="admin-filter-label admin-roster__checkbox">
              <input
                type="checkbox"
                checked={updateExisting}
                onChange={e => { setUpdateExisting(e.target.checked); resetFrom('mapping') }}
              />
              Update patients that already exist
            </label>
          </div>

          <button
            className="admin-filter-btn"
            onClick={runDryRun}
            disabled={busy !== null || missingRequired.length > 0}
          >
            {busy === 'dry-run' ? 'Checking…' : 'Dry run: show me what would happen'}
          </button>
        </div>
      )}

      {error && (
        <div className="admin-section">
          <AdminErrorState error={error} />
        </div>
      )}

      {/* ── Step 3 — the plan, then the commit ────────────────────────── */}
      {dryRun && (
        <div className="admin-section">
          <div className="admin-roster__step">
            <span className="admin-roster__step-num">3</span>
            <h4 className="admin-roster__step-title">Review, then apply</h4>
          </div>

          <div className="admin-filter-row" data-testid="roster-counts">
            {Object.entries(counts).map(([k, v]) => (
              <span key={k} className={countClass(k, v)}>{countLabel(k, v)}</span>
            ))}
            <span className="admin-filter-meta">nothing has been written yet</span>
          </div>

          {dryRun.file_issues.length > 0 && (
            <ul className="admin-roster__issues">
              {dryRun.file_issues.slice(0, 12).map((i, n) => (
                <li key={n} style={{ color: issueTone(i.severity) }}>
                  {i.line ? `Line ${i.line}: ` : ''}{i.message}
                </li>
              ))}
            </ul>
          )}

          {/* Dates the file gave that would be written blank. Apply stays
              held until the admin picks a date order or accepts the blanks. */}
          {datesLeftBlank > 0 && (
            <div className="admin-roster__ask" data-testid="roster-blank-dates">
              <p className="admin-roster__ask-title" style={{ color: 'var(--status-warn-text)' }}>
                {countLabel('dates_left_blank', datesLeftBlank)} in this import
              </p>
              <p className="admin-filter-meta">
                The date order is set to {DATE_ORDER_LABELS[dateOrder] ?? dateOrder}. Check the
                warnings below: if the file writes dates another way, change the date order and run
                the dry run again. A patient with no date of birth cannot open their discharge link.
              </p>
              <label className="admin-filter-label admin-roster__checkbox">
                <input
                  type="checkbox"
                  checked={blankDatesAccepted}
                  onChange={e => setBlankDatesAccepted(e.target.checked)}
                />
                Import with these dates left blank
              </label>
            </div>
          )}

          {rowIssues.length > 0 && (
            <div data-testid="roster-row-warnings">
              <p className="admin-roster__ask-title">Warnings and errors by row</p>
              <ul className="admin-roster__issues">
                {rowIssues.slice(0, 50).map((i, n) => (
                  <li key={n} style={{ color: issueTone(i.severity) }}>
                    {i.line ? `Line ${i.line}: ` : ''}{i.message}
                  </li>
                ))}
              </ul>
              {rowIssues.length > 50 && (
                <p className="admin-filter-meta">
                  Showing 50 of {rowIssues.length}. Every row's issues are also in the table below.
                </p>
              )}
            </div>
          )}

          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Line</th><th>MRN</th><th>Name</th><th>Action</th><th>Why</th><th>Admission</th><th>Issues</th>
                </tr>
              </thead>
              <tbody>
                {dryRun.rows.slice(0, 100).map(r => (
                  <tr key={r.line}>
                    <td className="admin-td--small">{r.line}</td>
                    <td className="admin-td--mono">{r.mrn || '—'}</td>
                    <td className="admin-td--truncate">{r.name || '—'}</td>
                    <td><span className={actionClass(r.action)}>{ACTION_LABELS[r.action] ?? r.action}</span></td>
                    <td className="admin-td--small admin-td--truncate">{humanCode(r.reason, REASON_LABELS)}</td>
                    <td className="admin-td--small">
                      {humanCode((r.admission?.action as string | undefined) ?? null, ADMISSION_LABELS)}
                    </td>
                    <td className="admin-td--small">
                      {(r.issues || []).length === 0 ? 'None' : (
                        <ul className="admin-roster__issues">
                          {r.issues.map((i, n) => (
                            <li key={n} style={{ color: issueTone(i.severity) }}>{i.message}</li>
                          ))}
                        </ul>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {dryRun.rows.length > 100 && (
            <p className="admin-filter-meta">
              Showing the first 100 of {dryRun.rows.length} rows. The commit applies all of them.
            </p>
          )}

          {/* Displayed, never actionable — absence from a roster is not a discharge. */}
          {dryRun.absent_from_roster && (dryRun.absent_from_roster.count || 0) > 0 && (
            <div className="admin-roster__absent">
              <p className="admin-roster__ask-title">
                {dryRun.absent_from_roster.count} patient(s) Orb holds on{' '}
                {(dryRun.absent_from_roster.wards || []).join(', ') || 'these wards'} are not in this file
              </p>
              <p className="admin-filter-meta">{dryRun.absent_from_roster.note}</p>
            </div>
          )}

          <div className="admin-filter-row">
            <label className="admin-filter-label">
              Note for the audit trail
              <input
                className="admin-filter-input"
                value={note}
                placeholder="e.g. Monday morning GM roster"
                onChange={e => setNote(e.target.value)}
              />
            </label>
          </div>

          {!dryRunIsCurrent && (
            <p className="admin-roster__required" style={{ color: 'var(--status-warn-text)' }}>
              The mapping changed since this dry run. Run it again before applying.
            </p>
          )}

          <button
            className="admin-filter-btn admin-roster__commit"
            onClick={runCommit}
            disabled={busy !== null || !dryRunIsCurrent || (datesLeftBlank > 0 && !blankDatesAccepted)}
          >
            {busy === 'commit' ? 'Applying…' : 'Apply this import'}
          </button>
        </div>
      )}

      {committed && (
        <div className="admin-section">
          <p className="admin-roster__ask-title" style={{ color: 'var(--status-ok-text)' }}>
            Import applied · run {committed.run_id}
          </p>
          <div className="admin-filter-row" data-testid="roster-committed-counts">
            {Object.entries(committed.summary || {}).map(([k, v]) => (
              <span key={k} className={countClass(k, v)}>{countLabel(k, v)}</span>
            ))}
            {Object.entries(committed.admissions || {}).map(([k, v]) => (
              <span key={k} className="admin-stat-chip">
                {countLabel(`admissions_${k === 'created' ? 'create' : k === 'updated' ? 'update' : 'close'}`, v)}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* ── History ───────────────────────────────────────────────────── */}
      <div className="admin-section">
        <h4 className="admin-roster__step-title">Applied imports</h4>
        {runs.length === 0 ? (
          <AdminEmptyState title="No imports yet" message="Applied imports appear here. Dry runs are not recorded." />
        ) : (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>When</th><th>File</th><th>By</th><th>Rows</th><th>Note</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r, n) => (
                  <tr key={String(r.id ?? n)}>
                    <td className="admin-td--small">
                      {r.started_at ? formatClinicalDateTime(r.started_at) : '—'}
                    </td>
                    <td className="admin-td--truncate">{r.source_filename || '—'}</td>
                    <td className="admin-td--mono admin-td--small">{r.actor_user_id || '—'}</td>
                    <td className="admin-td--small">
                      {r.rows_created} new, {r.rows_updated} updated, {r.rows_unchanged} unchanged, {r.rows_skipped} skipped
                    </td>
                    <td className="admin-td--truncate admin-td--small">{r.note || '—'}</td>
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
