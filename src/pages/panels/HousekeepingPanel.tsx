/**
 * Orb — Housekeeping & Facilities.
 *
 * ╔════════════════════════════════════════════════════════════════════════╗
 * ║ THIS IS NOT THE CAPACITY SCREEN, AND IT SAYS SO BEFORE IT SAYS         ║
 * ║ ANYTHING ELSE.                                                         ║
 * ╚════════════════════════════════════════════════════════════════════════╝
 *
 * Orb already has a bed-availability number and a bed manager already acts on
 * it: the capacity and forecast screens compute `available` as the ward's bed
 * count (a site setting) minus admitted patients. It does not know that a bed is
 * dirty. This module does, for the beds a site has registered here — which on
 * day one is none of them.
 *
 * So this screen never uses the word "available" for its own figures, always
 * shows COVERAGE beside readiness ("4 of 12 beds registered"), and states the
 * relationship in a sentence that is on the first paint and still there if every
 * fetch fails. A hospital reading "3 ready" without "3 of 12 registered" would
 * conclude the ward has three usable beds; it has three clean REGISTERED beds
 * and nine beds nobody has told this module about. The two numbers are only safe
 * together, so they are only ever rendered together.
 *
 * WHAT THE SCREEN IS ORGANISED AROUND. Not "here are your tables". The person
 * who opens it has three questions in this order: what is late, what is dirty
 * right now, and are we getting slower — so those are the board, the ward view
 * and the turnaround report, in that order. Linen is a separate errand and is a
 * separate view.
 *
 * THE STATE MACHINE IS THE SERVER'S. Buttons come from `meta.legal_transitions`,
 * not from a table in this file. A hard-coded copy would be a second state
 * machine, and the one that is wrong is always the one the user can see.
 *
 * NO PHI. Nothing on this screen names a patient, and nothing in the API can.
 * A housekeeping board is read on a corridor trolley by staff who hold no
 * clinical grant. The only clinical fact present is "terminal clean", which a
 * housekeeper needs in order to choose PPE.
 */
import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  cancelRequest, completeCleaning, createUnit, formatMinutes, getLinen, getMeta,
  getOverview, getTasks, getTurnaround, getUnits, HousekeepingApiError,
  LINEN_DIRECTION_LABEL, PRIORITY_LABEL, raiseRequest, recordLinen, recordOccupied,
  REQUEST_TYPE_LABEL, setUnitState, startCleaning, STATE_LABEL, STATE_TONE,
  syncDischarges,
  type CleaningTask, type HousekeepingMeta, type HousekeepingOverview,
  type HousekeepingUnit, type LinenDirection, type LinenSummary, type Priority,
  type RequestType, type TurnaroundReport, type UnitState,
} from '../../api/housekeeping'
import './HousekeepingPanel.css'

/* ── The capacity sentence, duplicated on purpose ────────────────────────────
 * The server sends this same statement on /readiness and the panel prefers the
 * server's copy once it has one. This literal exists so the words are on screen
 * in the FIRST PAINT and still there if every request fails. The one message
 * that must never be missing cannot be one that depends on the network. */
const CAPACITY_NOTE =
  'Orb’s capacity and forecast screens count an available bed as the ward’s '
  + 'bed count minus admitted patients. They do not subtract beds awaiting cleaning, '
  + 'and this module does not change them. A bed shown here as needing cleaning, '
  + 'being cleaned, or out of service is NOT usable, and housekeeping can only '
  + 'speak for beds that have been registered on this screen.'

type View = 'board' | 'wards' | 'turnaround' | 'linen' | 'register'

const VIEWS: { id: View; label: string }[] = [
  { id: 'board', label: 'Cleaning board' },
  { id: 'wards', label: 'Ward readiness' },
  { id: 'turnaround', label: 'Turnaround' },
  { id: 'linen', label: 'Linen' },
  { id: 'register', label: 'Beds & rooms' },
]

/* ── Small presentational pieces ─────────────────────────────────────────── */

function StateBadge({ state }: { state: UnitState }) {
  return (
    <span className={`hk-badge hk-badge--${STATE_TONE[state]}`}>{STATE_LABEL[state]}</span>
  )
}

function PriorityChip({ priority }: { priority: Priority }) {
  return (
    <span className={`hk-chip hk-chip--${priority}`}>{PRIORITY_LABEL[priority]}</span>
  )
}

/** A terminal clean is visibly distinct — not a faded variant of a standard one.
 *  It is a different job with different PPE and a longer expectation, and the
 *  person reading the board at a distance has to be able to tell. */
function TerminalTag() {
  return (
    <span className="hk-terminal" title="Isolation / terminal clean: longer expected turnaround">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M12 3l9 16H3z" /><path d="M12 10v4" /><path d="M12 17h.01" />
      </svg>
      Terminal clean
    </span>
  )
}

function Tile({ value, caption, tone }: { value: string; caption: string; tone?: string }) {
  return (
    <div className="hk-tile">
      <div className={`hk-tile__value${tone ? ` hk-tile__value--${tone}` : ''}`}>{value}</div>
      <div className="hk-tile__caption">{caption}</div>
    </div>
  )
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="hk-empty">
      <div className="hk-empty__title">{title}</div>
      <div className="hk-empty__detail">{detail}</div>
    </div>
  )
}

function Drawer({ title, onClose, children }: {
  title: string; onClose: () => void; children: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  if (typeof document === 'undefined') return null
  // Portalled to body so the drawer is never clipped by a scrolling ancestor.
  return createPortal(
    <div className="hk-drawer-root" role="dialog" aria-modal="true" aria-label={title}>
      <button type="button" className="hk-drawer__scrim" aria-label="Close" onClick={onClose} />
      <div className="hk-drawer">
        <div className="hk-drawer__head">
          <h3 className="hk-drawer__title">{title}</h3>
          <button type="button" className="hk-btn hk-btn--quiet" onClick={onClose}>Close</button>
        </div>
        <div className="hk-drawer__body">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

/** "GM-1/03, bed 03, GM-1": the unit the way the ward knows it. */
function unitWords(u: Pick<HousekeepingUnit, 'unit_code' | 'bed_label' | 'ward'>): string {
  return [u.unit_code, u.bed_label ? `bed ${u.bed_label}` : null, u.ward]
    .filter(Boolean).join(', ')
}

/** A state change or a cancellation asked for in a drawer, never on one tap.
 *  A bed moved by mistake (occupied to needs-cleaning, say) is a bed somebody
 *  else then offers or cleans around a patient. */
function ConfirmDrawer({ title, message, confirmLabel, reasonLabel, reasonRequired, busy,
  onClose, onConfirm }: {
  title: string
  message: ReactNode
  confirmLabel: string
  reasonLabel?: string
  reasonRequired?: boolean
  busy: boolean
  onClose: () => void
  onConfirm: (reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const missing = !!reasonRequired && !reason.trim()
  return (
    <Drawer title={title} onClose={onClose}>
      <form
        className="hk-drawer__form"
        onSubmit={e => {
          e.preventDefault()
          if (!missing) onConfirm(reason.trim())
        }}
      >
        <p style={{ margin: 0, lineHeight: 1.5 }}>{message}</p>
        {reasonLabel && (
          <label className="hk-field">
            <span className="hk-field__label">
              {reasonLabel}{reasonRequired ? '' : ' (optional)'}
            </span>
            <textarea className="hk-input hk-textarea" rows={3} value={reason}
                      onChange={e => setReason(e.target.value)} />
          </label>
        )}
        <div className="hk-drawer__actions">
          <button type="button" className="hk-btn hk-btn--quiet" onClick={onClose}>Go back</button>
          <button type="submit" className="hk-btn hk-btn--primary" disabled={busy || missing}>
            {confirmLabel}
          </button>
        </div>
      </form>
    </Drawer>
  )
}

type Pending =
  | { kind: 'transition'; unit: HousekeepingUnit; to: UnitState }
  | { kind: 'occupy'; unit: HousekeepingUnit }
  | { kind: 'cancel'; task: CleaningTask }

/* ── The panel ───────────────────────────────────────────────────────────── */

export default function HousekeepingPanel() {
  const [view, setView] = useState<View>('board')
  const [meta, setMeta] = useState<HousekeepingMeta | null>(null)
  const [overview, setOverview] = useState<HousekeepingOverview | null>(null)
  const [units, setUnits] = useState<HousekeepingUnit[]>([])
  const [tasks, setTasks] = useState<CleaningTask[]>([])
  const [turnaround, setTurnaround] = useState<TurnaroundReport | null>(null)
  const [linen, setLinen] = useState<LinenSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [disabled, setDisabled] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [wardFilter, setWardFilter] = useState('')
  const [requestFor, setRequestFor] = useState<HousekeepingUnit | null>(null)
  const [pending, setPending] = useState<Pending | null>(null)

  const handle = useCallback((e: unknown) => {
    if (e instanceof HousekeepingApiError && e.isModuleDisabled) {
      setDisabled(true)
      return
    }
    setError(e instanceof Error ? e.message : 'Something went wrong')
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [m, o] = await Promise.all([getMeta(), getOverview(true)])
      setMeta(m)
      setOverview(o)
      const [u, t] = await Promise.all([getUnits(), getTasks({ limit: 300 })])
      setUnits(u)
      setTasks(t)
    } catch (e) {
      handle(e)
    } finally {
      setLoading(false)
    }
  }, [handle])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])

  useEffect(() => {
    if (view !== 'turnaround' || turnaround) return
    getTurnaround({ days: 30 }).then(setTurnaround).catch(handle)
  }, [view, turnaround, handle])

  useEffect(() => {
    if (view !== 'linen' || linen) return
    getLinen({ days: 30 }).then(setLinen).catch(handle)
  }, [view, linen, handle])

  const act = useCallback(async (key: string, fn: () => Promise<unknown>, done?: string) => {
    setBusy(key)
    setError(null)
    try {
      await fn()
      if (done) setNotice(done)
      await load()
      setTurnaround(null)
      setLinen(null)
    } catch (e) {
      handle(e)
    } finally {
      setBusy(null)
    }
  }, [handle, load])

  /* Registering refuses a unit code already on the register (409) instead of
     overwriting that bed. The refusal goes back to the form, which keeps what
     was typed; anything else goes to the notice area as usual. */
  const registerUnit = useCallback(async (body: Parameters<typeof createUnit>[0]) => {
    setBusy('unit')
    setError(null)
    try {
      await createUnit({ ...body, create_only: true })
      setNotice(`${body.unit_code} registered`)
      await load()
      return null
    } catch (e) {
      if (e instanceof HousekeepingApiError && e.status === 409) return e.message
      handle(e)
      return null
    } finally {
      setBusy(null)
    }
  }, [handle, load])

  const confirmPending = (reason: string) => {
    const p = pending
    if (!p) return
    setPending(null)
    if (p.kind === 'transition') {
      void act(`state-${p.unit.id}`, () => setUnitState(p.unit.id, p.to, reason || undefined),
        `${p.unit.unit_code}: ${STATE_LABEL[p.to]}`)
    } else if (p.kind === 'occupy') {
      void act(`occupy-${p.unit.id}`, () => recordOccupied(p.unit.id, reason),
        `${p.unit.unit_code} recorded occupied`)
    } else {
      void act(`cancel-${p.task.id}`, () => cancelRequest(p.task.id, reason), 'Request cancelled')
    }
  }

  const wards = useMemo(() => {
    const set = new Set<string>()
    for (const u of units) set.add(u.ward)
    for (const w of overview?.readiness.wards ?? []) set.add(w.ward)
    return [...set].sort()
  }, [units, overview])

  const visibleTasks = useMemo(
    () => tasks.filter(t => (!wardFilter || t.ward === wardFilter)
      && (t.status === 'open' || t.status === 'in_progress')),
    [tasks, wardFilter],
  )

  const visibleUnits = useMemo(
    () => units.filter(u => !wardFilter || u.ward === wardFilter),
    [units, wardFilter],
  )

  /* A site that has switched the module off gets a sentence, not an empty board.
     An empty cleaning board is a statement about the hospital ("nothing needs
     cleaning"); "not available at this site" is a statement about the software. */
  if (disabled) {
    return (
      <div className="hk-panel">
        <header className="hk-head">
          <div>
            <h2 className="hk-title">Housekeeping &amp; Facilities</h2>
            <p className="hk-sub">Room turnaround, cleaning requests and linen</p>
          </div>
        </header>
        <Empty
          title="Housekeeping is not switched on at this site"
          detail={'This deployment records room turnaround somewhere else. Nothing is '
            + 'missing and nothing is broken. The module is switched off for this site.'}
        />
      </div>
    )
  }

  const sweep = overview?.discharge_sweep
  const unmatched = sweep?.unmatched ?? 0

  return (
    <div className="hk-panel">
      <header className="hk-head">
        <div>
          <h2 className="hk-title">Housekeeping &amp; Facilities</h2>
          <p className="hk-sub">
            Room turnaround, cleaning requests, linen. A discharged bed is not an
            available bed until somebody has cleaned it.
          </p>
        </div>
        <div className="hk-head__controls">
          <label className="hk-field">
            <span className="hk-field__label">Ward</span>
            <select
              className="hk-select"
              value={wardFilter}
              onChange={e => setWardFilter(e.target.value)}
            >
              <option value="">All wards</option>
              {wards.map(w => <option key={w} value={w}>{w}</option>)}
            </select>
          </label>
          <button
            type="button"
            className="hk-btn"
            disabled={busy !== null}
            onClick={() => act('sync', () => syncDischarges(), 'Discharge sweep run')}
          >
            {busy === 'sync' ? 'Checking…' : 'Check for discharges'}
          </button>
        </div>
      </header>

      {/* NOT a dismissible toast and NOT a low-contrast aside. It states which
          system owns which number, and somebody who misses it offers a dirty
          bed. */}
      <div className="hk-notice">
        {overview?.readiness.capacity_basis
          ? `${overview.readiness.capacity_basis} ${overview.readiness.reconciliation_note}`
          : CAPACITY_NOTE}
      </div>

      {error && (
        <div className="hk-notice hk-notice--danger" role="alert">
          {error}
          <button type="button" className="hk-btn hk-btn--quiet" onClick={() => void load()}>
            Try again
          </button>
        </div>
      )}
      {notice && (
        <div className="hk-notice hk-notice--ok" role="status">
          {notice}
          <button type="button" className="hk-btn hk-btn--quiet" onClick={() => setNotice(null)}>
            Dismiss
          </button>
        </div>
      )}
      {unmatched > 0 && (
        <div className="hk-notice hk-notice--warn" role="status">
          {unmatched} recent discharge{unmatched === 1 ? '' : 's'} could not be matched
          to a registered bed, so no turnaround was raised for
          {unmatched === 1 ? ' it' : ' them'}. Register the ward&apos;s beds under
          &ldquo;Beds &amp; rooms&rdquo; using the same bed labels the ward admits with.
        </div>
      )}

      <nav className="hk-tabs" aria-label="Housekeeping views">
        {VIEWS.map(v => (
          <button
            key={v.id}
            type="button"
            className={`hk-tab${view === v.id ? ' is-active' : ''}`}
            aria-current={view === v.id ? 'page' : undefined}
            onClick={() => setView(v.id)}
          >
            {v.label}
          </button>
        ))}
      </nav>

      {loading && !overview ? (
        <div className="hk-loading">Loading the housekeeping board…</div>
      ) : (
        <>
          {view === 'board' && (
            <BoardView
              overview={overview}
              tasks={visibleTasks}
              busy={busy}
              onStart={t => act(`start-${t.id}`, () => startCleaning(t.id), 'Cleaning started')}
              onComplete={t => act(`done-${t.id}`, () => completeCleaning(t.id),
                'Clean recorded: the unit is ready')}
              onCancel={t => setPending({ kind: 'cancel', task: t })}
            />
          )}

          {view === 'wards' && <ReadinessView overview={overview} />}

          {view === 'turnaround' && <TurnaroundView report={turnaround} />}

          {view === 'linen' && (
            <LinenView
              summary={linen}
              wards={wards}
              busy={busy}
              onRecord={body => act('linen', () => recordLinen(body), 'Linen movement recorded')}
            />
          )}

          {view === 'register' && (
            <RegisterView
              units={visibleUnits}
              wards={wards}
              meta={meta}
              busy={busy}
              onCreate={registerUnit}
              onTransition={(u, s) => setPending({ kind: 'transition', unit: u, to: s })}
              onOccupy={u => setPending({ kind: 'occupy', unit: u })}
              onRequest={setRequestFor}
            />
          )}
        </>
      )}

      {requestFor && meta && (
        <RequestDrawer
          unit={requestFor}
          meta={meta}
          busy={busy !== null}
          onClose={() => setRequestFor(null)}
          onSubmit={body => {
            setRequestFor(null)
            return act('request', () => raiseRequest(body), 'Cleaning request raised')
          }}
        />
      )}

      {pending?.kind === 'transition' && (
        <ConfirmDrawer
          title={`Change ${pending.unit.unit_code}`}
          message={<>Move <strong>{unitWords(pending.unit)}</strong> from{' '}
            <strong>{STATE_LABEL[pending.unit.state]}</strong> to{' '}
            <strong>{STATE_LABEL[pending.to]}</strong>?
            {pending.unit.state === 'occupied' && ' The register will stop showing a patient in this bed.'}</>}
          confirmLabel={`Move to ${STATE_LABEL[pending.to]}`}
          reasonLabel="Reason"
          reasonRequired={pending.unit.state === 'occupied'}
          busy={busy !== null}
          onClose={() => setPending(null)}
          onConfirm={confirmPending}
        />
      )}
      {pending?.kind === 'occupy' && (
        <ConfirmDrawer
          title={`Occupy ${pending.unit.unit_code} without a clean`}
          message={<><strong>{unitWords(pending.unit)}</strong> has NOT had a completed
            clean. Recording it occupied skips the clean, and the reason is kept on the
            bed&apos;s history.</>}
          confirmLabel="Record occupied"
          reasonLabel="Why is it being occupied without a clean?"
          reasonRequired
          busy={busy !== null}
          onClose={() => setPending(null)}
          onConfirm={confirmPending}
        />
      )}
      {pending?.kind === 'cancel' && (
        <ConfirmDrawer
          title="Cancel cleaning request"
          message={<>Cancel the {REQUEST_TYPE_LABEL[pending.task.request_type]?.toLowerCase()
            ?? 'cleaning'} request for <strong>{[pending.task.unit_code, pending.task.ward]
            .filter(Boolean).join(', ')}</strong>?</>}
          confirmLabel="Cancel request"
          reasonLabel="Why is this request being cancelled?"
          reasonRequired
          busy={busy !== null}
          onClose={() => setPending(null)}
          onConfirm={confirmPending}
        />
      )}
    </div>
  )
}

/* ── Board ───────────────────────────────────────────────────────────────── */

function BoardView({ overview, tasks, busy, onStart, onComplete, onCancel }: {
  overview: HousekeepingOverview | null
  tasks: CleaningTask[]
  busy: string | null
  onStart: (t: CleaningTask) => void
  onComplete: (t: CleaningTask) => void
  onCancel: (t: CleaningTask) => void
}) {
  const counts = overview?.counts
  return (
    <section className="hk-section">
      <div className="hk-tiles">
        {/* Requests, not beds: one bed can carry a spillage and a turnaround,
            and a dirty bed nobody asked to clean is on no request at all, so it
            has its own tile. */}
        <Tile value={String(counts?.open ?? 0)} caption="Open cleaning requests" />
        <Tile
          value={String(counts?.dirty_without_request ?? 0)}
          caption="Dirty beds with no request"
          tone={counts?.dirty_without_request ? 'warn' : undefined}
        />
        <Tile value={String(counts?.in_progress ?? 0)} caption="Being cleaned now" />
        <Tile
          value={String(counts?.overdue ?? 0)}
          caption="Past their expected turnaround"
          tone={counts?.overdue ? 'danger' : undefined}
        />
        <Tile
          value={String(counts?.terminal_open ?? 0)}
          caption="Terminal / isolation cleans open"
          tone={counts?.terminal_open ? 'warn' : undefined}
        />
      </div>

      {tasks.length === 0 ? (
        <Empty
          title="Nothing is waiting"
          detail={'No open cleaning requests for this ward. If beds are being turned '
            + 'around without appearing here, check that they are registered under '
            + '“Beds & rooms”.'}
        />
      ) : (
        <div className="hk-table-wrap">
          <table className="hk-table">
            <thead>
              <tr>
                <th scope="col">Unit</th>
                <th scope="col">Request</th>
                <th scope="col">Priority</th>
                <th scope="col" className="hk-num">Waiting</th>
                <th scope="col" className="hk-num">Expected</th>
                <th scope="col">Status</th>
                <th scope="col" className="hk-actions-col">Action</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map(t => (
                <tr key={t.id} className={t.is_overdue ? 'hk-row--overdue' : undefined}>
                  <th scope="row" className="hk-cell-unit">
                    <span className="hk-unit-code">{t.unit_code ?? '—'}</span>
                    <span className="hk-unit-ward">{t.ward}</span>
                  </th>
                  <td>
                    <div className="hk-cell-request">
                      {REQUEST_TYPE_LABEL[t.request_type as RequestType] ?? t.request_type}
                      {t.is_terminal_clean && <TerminalTag />}
                    </div>
                    {t.description && <div className="hk-cell-note">{t.description}</div>}
                  </td>
                  <td><PriorityChip priority={t.priority} /></td>
                  <td className="hk-num">
                    <span className={t.is_overdue ? 'hk-late' : undefined}>
                      {formatMinutes(t.elapsed_minutes)}
                    </span>
                  </td>
                  <td className="hk-num">{formatMinutes(t.expected_minutes)}</td>
                  <td>
                    {t.status === 'in_progress'
                      ? <span className="hk-badge hk-badge--info">Being cleaned</span>
                      : <span className="hk-badge hk-badge--warn">Waiting</span>}
                  </td>
                  <td className="hk-actions-col">
                    {t.status === 'open' && (
                      <button
                        type="button" className="hk-btn hk-btn--primary"
                        disabled={busy !== null}
                        onClick={() => onStart(t)}
                      >
                        Start clean
                      </button>
                    )}
                    {t.status === 'in_progress' && (
                      <button
                        type="button" className="hk-btn hk-btn--primary"
                        disabled={busy !== null}
                        onClick={() => onComplete(t)}
                      >
                        Mark ready
                      </button>
                    )}
                    <button
                      type="button" className="hk-btn hk-btn--quiet"
                      disabled={busy !== null}
                      onClick={() => onCancel(t)}
                    >
                      Cancel
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

/* ── Ward readiness ──────────────────────────────────────────────────────── */

function ReadinessView({ overview }: { overview: HousekeepingOverview | null }) {
  const wards = overview?.readiness.wards ?? []
  if (wards.length === 0) {
    return (
      <Empty
        title="No beds are registered yet"
        detail={'Housekeeping can only speak for beds it knows about. Register this '
          + 'hospital’s beds under “Beds & rooms”, using the same bed '
          + 'labels the ward admits with, and readiness appears here.'}
      />
    )
  }
  return (
    <section className="hk-section">
      <div className="hk-table-wrap">
        <table className="hk-table">
          <thead>
            <tr>
              <th scope="col">Ward</th>
              <th scope="col" className="hk-num">Registered</th>
              <th scope="col" className="hk-num">Coverage</th>
              <th scope="col" className="hk-num">Ready</th>
              <th scope="col" className="hk-num">Needs cleaning</th>
              <th scope="col" className="hk-num">Being cleaned</th>
              <th scope="col" className="hk-num">Out of service</th>
              <th scope="col" className="hk-num">Capacity screen says available</th>
            </tr>
          </thead>
          <tbody>
            {wards.map(w => (
              <tr key={w.ward}>
                <th scope="row">{w.ward}</th>
                <td className="hk-num">
                  {w.registered_units}
                  {w.ward_capacity !== null && (
                    <span className="hk-of"> of {w.ward_capacity}</span>
                  )}
                </td>
                <td className="hk-num">
                  {w.coverage_pct === null
                    ? <span className="hk-unknown">Unknown</span>
                    : w.over_capacity && w.coverage_note
                      ? <span className="hk-cell-note">{w.coverage_note}</span>
                      : `${w.coverage_pct}%`}
                </td>
                <td className="hk-num hk-ok">{w.vacant_ready}</td>
                <td className="hk-num hk-warn">{w.vacant_dirty}</td>
                <td className="hk-num">{w.cleaning_in_progress}</td>
                <td className="hk-num hk-danger">{w.out_of_service}</td>
                <td className="hk-num">
                  {w.capacity_view_available === null
                    ? <span className="hk-unknown">Unknown</span>
                    : w.capacity_view_available}
                  {w.not_ready_registered > 0 && (
                    <div className="hk-cell-note">
                      {w.not_ready_registered} of these {w.not_ready_registered === 1 ? 'is' : 'are'} not yet usable
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hk-foot">
        The right-hand column is the capacity screen&apos;s own number, repeated here so
        the two can be compared without opening two tabs. It counts beds this module
        knows are dirty. Read both against coverage.
      </p>
    </section>
  )
}

/* ── Turnaround ──────────────────────────────────────────────────────────── */

function TurnaroundView({ report }: { report: TurnaroundReport | null }) {
  if (!report) return <div className="hk-loading">Loading turnaround times&#8230;</div>
  if (report.wards.length === 0) {
    return (
      <Empty
        title="No completed turnarounds yet"
        detail={'Turnaround is measured from the moment a bed is vacated to the moment '
          + 'it is marked ready. Numbers appear once cleans have been recorded end to end.'}
      />
    )
  }
  return (
    <section className="hk-section">
      <p className="hk-foot">
        Measured <strong>{report.measured_between}</strong>, from the bed being
        vacated to it being marked ready, not from when someone raised the request.
        The gap between turnaround and cleaning time is queue delay.
        Expected: {report.expected_minutes.standard}m standard,
        {' '}{report.expected_minutes.terminal}m terminal ({report.expected_minutes.source}).
      </p>
      <div className="hk-table-wrap">
        <table className="hk-table">
          <thead>
            <tr>
              <th scope="col">Ward</th>
              <th scope="col" className="hk-num">Completed</th>
              <th scope="col" className="hk-num">Median turnaround</th>
              <th scope="col" className="hk-num">90th percentile</th>
              <th scope="col" className="hk-num">Median cleaning time</th>
              <th scope="col" className="hk-num">Standard</th>
              <th scope="col" className="hk-num">Terminal</th>
              <th scope="col" className="hk-num">Over expected</th>
            </tr>
          </thead>
          <tbody>
            {report.wards.map(w => (
              <tr key={w.ward}>
                <th scope="row">{w.ward}</th>
                <td className="hk-num">
                  {w.completed}
                  {w.unmeasurable > 0 && (
                    <div className="hk-cell-note">{w.unmeasurable} could not be timed</div>
                  )}
                </td>
                <td className="hk-num">{formatMinutes(w.median_turnaround_minutes)}</td>
                <td className="hk-num">{formatMinutes(w.p90_turnaround_minutes)}</td>
                <td className="hk-num">{formatMinutes(w.median_cleaning_minutes)}</td>
                <td className="hk-num">
                  {formatMinutes(w.standard.median_minutes)}
                  <span className="hk-of"> ({w.standard.count})</span>
                </td>
                <td className="hk-num">
                  {formatMinutes(w.terminal.median_minutes)}
                  <span className="hk-of"> ({w.terminal.count})</span>
                </td>
                <td className={`hk-num${w.over_expected ? ' hk-late' : ''}`}>{w.over_expected}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

/* ── Linen ───────────────────────────────────────────────────────────────── */

function LinenView({ summary, wards, busy, onRecord }: {
  summary: LinenSummary | null
  wards: string[]
  busy: string | null
  onRecord: (body: {
    ward: string; item_code: string; direction: LinenDirection; quantity: number
  }) => void
}) {
  const [ward, setWard] = useState('')
  const [item, setItem] = useState('')
  const [direction, setDirection] = useState<LinenDirection>('issue')
  const [quantity, setQuantity] = useState('')

  const items = summary?.items ?? []
  /* The default selection is DERIVED, not synced into state by an effect: an
     unchosen dropdown shows the first item, so "what the select displays" and
     "what submit sends" come from one expression. The effect version disagreed
     with itself for a render — the select already displayed the first item
     while `item` was still '' — so a fast submit sent an empty item_code. */
  const selectedItem = item || items[0]?.code || ''

  if (!summary) return <div className="hk-loading">Loading linen&#8230;</div>

  const qty = Number.parseInt(quantity, 10)
  const canSubmit = !!ward && !!selectedItem && Number.isFinite(qty) && qty > 0 && busy === null

  return (
    <section className="hk-section">
      <form
        className="hk-form"
        onSubmit={e => {
          e.preventDefault()
          if (!canSubmit) return
          onRecord({ ward, item_code: selectedItem, direction, quantity: qty })
          setQuantity('')
        }}
      >
        <label className="hk-field">
          <span className="hk-field__label">Ward</span>
          <input
            className="hk-input" list="hk-ward-list" value={ward}
            onChange={e => setWard(e.target.value)} placeholder="Ward"
          />
          <datalist id="hk-ward-list">
            {wards.map(w => <option key={w} value={w} />)}
          </datalist>
        </label>
        <label className="hk-field">
          <span className="hk-field__label">Item</span>
          <select className="hk-select" value={selectedItem} onChange={e => setItem(e.target.value)}>
            {items.map(i => <option key={i.code} value={i.code}>{i.name}</option>)}
          </select>
        </label>
        <label className="hk-field">
          <span className="hk-field__label">Movement</span>
          <select
            className="hk-select" value={direction}
            onChange={e => setDirection(e.target.value as LinenDirection)}
          >
            {(Object.keys(LINEN_DIRECTION_LABEL) as LinenDirection[]).map(d => (
              <option key={d} value={d}>{LINEN_DIRECTION_LABEL[d]}</option>
            ))}
          </select>
        </label>
        <label className="hk-field">
          <span className="hk-field__label">Pieces</span>
          <input
            className="hk-input hk-input--num" inputMode="numeric" value={quantity}
            onChange={e => setQuantity(e.target.value)} placeholder="0"
          />
        </label>
        <button type="submit" className="hk-btn hk-btn--primary" disabled={!canSubmit}>
          Record
        </button>
      </form>

      <p className="hk-foot">{summary.definition}</p>

      {summary.lines.length === 0 ? (
        <Empty
          title="No linen movements in this window"
          detail="Record an issue or a return above and the reconciliation appears here."
        />
      ) : (
        <div className="hk-table-wrap">
          <table className="hk-table">
            <thead>
              <tr>
                <th scope="col">Ward</th>
                <th scope="col">Item</th>
                <th scope="col" className="hk-num">Issued</th>
                <th scope="col" className="hk-num">Returned soiled</th>
                <th scope="col" className="hk-num">Returned clean</th>
                <th scope="col" className="hk-num">Condemned</th>
                <th scope="col" className="hk-num">Outstanding</th>
              </tr>
            </thead>
            <tbody>
              {summary.lines.map(l => (
                <tr key={`${l.ward}-${l.item_code}`}>
                  <th scope="row">{l.ward}</th>
                  <td>{l.item_name}</td>
                  <td className="hk-num">{l.issued}</td>
                  <td className="hk-num">{l.returned_soiled}</td>
                  <td className="hk-num">{l.returned_clean}</td>
                  <td className="hk-num">{l.condemned}</td>
                  <td className={`hk-num${l.outstanding < 0 ? ' hk-warn' : ''}`}>
                    {l.outstanding}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

/* ── Register ────────────────────────────────────────────────────────────── */

function RegisterView({ units, wards, meta, busy, onCreate, onTransition, onOccupy, onRequest }: {
  units: HousekeepingUnit[]
  /** The wards this hospital already has: the capacity screen's, and those
   *  holding registered beds. */
  wards: string[]
  meta: HousekeepingMeta | null
  busy: string | null
  /** Resolves to the server's refusal (a code already registered), or null. */
  onCreate: (body: {
    unit_code: string; ward: string; bed_label?: string; is_isolation_room?: boolean
  }) => Promise<string | null>
  onTransition: (u: HousekeepingUnit, s: UnitState) => void
  onOccupy: (u: HousekeepingUnit) => void
  onRequest: (u: HousekeepingUnit) => void
}) {
  const [code, setCode] = useState('')
  const [ward, setWard] = useState('')
  const [bed, setBed] = useState('')
  const [isolation, setIsolation] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)

  return (
    <section className="hk-section">
      {refusal && <div className="hk-notice hk-notice--danger" role="alert">{refusal}</div>}
      <form
        className="hk-form"
        onSubmit={async e => {
          e.preventDefault()
          if (!code.trim() || !ward.trim()) return
          setRefusal(null)
          const refused = await onCreate({
            unit_code: code.trim(), ward: ward.trim(),
            bed_label: bed.trim() || undefined, is_isolation_room: isolation,
          })
          // Kept on a refusal so the person can correct the code, not retype it.
          if (refused) setRefusal(refused)
          else { setCode(''); setBed('') }
        }}
      >
        <label className="hk-field">
          <span className="hk-field__label">Unit code</span>
          <input className="hk-input" value={code} onChange={e => setCode(e.target.value)}
                 placeholder="GM-1/03" />
        </label>
        <label className="hk-field">
          <span className="hk-field__label">Ward</span>
          {/* Chosen from the hospital's wards, not typed: 'gm-1' typed beside
              'GM-1' registered a second, phantom ward. Free text only when the
              hospital has no ward on record yet (a site registering beds before
              its first admission); the server matches case and spacing then. */}
          {wards.length > 0 ? (
            <select className="hk-select" value={ward} onChange={e => setWard(e.target.value)}>
              <option value="">Choose a ward</option>
              {wards.map(w => <option key={w} value={w}>{w}</option>)}
            </select>
          ) : (
            <input className="hk-input" value={ward} onChange={e => setWard(e.target.value)}
                   placeholder="GM-1" />
          )}
        </label>
        <label className="hk-field">
          <span className="hk-field__label">Bed label as the ward types it</span>
          <input className="hk-input" value={bed} onChange={e => setBed(e.target.value)}
                 placeholder="03" />
        </label>
        <label className="hk-check">
          <input type="checkbox" checked={isolation}
                 onChange={e => setIsolation(e.target.checked)} />
          <span>Isolation room (always terminal-cleaned)</span>
        </label>
        <button type="submit" className="hk-btn hk-btn--primary"
                disabled={busy !== null || !code.trim() || !ward.trim()}>
          Register bed
        </button>
      </form>

      <p className="hk-foot">
        A newly registered bed starts as <strong>needs cleaning</strong>, not ready.
        Registering a bed is inventory; &ldquo;ready&rdquo; is a claim that somebody
        cleaned it, and no import may make that claim on a person&apos;s behalf.
        The bed label must match what the ward admits with, or a discharge will not
        find its bed.
      </p>

      {units.length === 0 ? (
        <Empty title="No beds registered" detail="Add this hospital's beds above." />
      ) : (
        <div className="hk-table-wrap">
          <table className="hk-table">
            <thead>
              <tr>
                <th scope="col">Unit</th>
                <th scope="col">Ward</th>
                <th scope="col">State</th>
                <th scope="col" className="hk-num">In this state</th>
                <th scope="col" className="hk-actions-col">Move to</th>
              </tr>
            </thead>
            <tbody>
              {units.map(u => {
                const legal = meta?.legal_transitions[u.state] ?? []
                return (
                  <tr key={u.id}>
                    <th scope="row" className="hk-cell-unit">
                      <span className="hk-unit-code">{u.unit_code}</span>
                      {u.is_isolation_room && <TerminalTag />}
                    </th>
                    <td>{u.ward}</td>
                    <td><StateBadge state={u.state} /></td>
                    <td className="hk-num">{formatMinutes(u.minutes_in_state)}</td>
                    <td className="hk-actions-col">
                      {/* Buttons come from the SERVER'S transition table. */}
                      {legal.map(s => (
                        <button
                          key={s} type="button" className="hk-btn hk-btn--quiet"
                          disabled={busy !== null}
                          onClick={() => onTransition(u, s)}
                        >
                          {STATE_LABEL[s]}
                        </button>
                      ))}
                      {/* The skipped-clean path, offered ONLY where the ordinary
                          one is unavailable. A ready bed already has "Occupied"
                          in its legal moves above; adding a second button with
                          the same word would put two controls that mean
                          different things under one label. */}
                      {u.state !== 'occupied' && u.state !== 'vacant_ready' && (
                        <button
                          type="button" className="hk-btn hk-btn--quiet"
                          disabled={busy !== null}
                          onClick={() => onOccupy(u)}
                        >
                          Occupied (clean skipped)
                        </button>
                      )}
                      <button
                        type="button" className="hk-btn"
                        disabled={busy !== null}
                        onClick={() => onRequest(u)}
                      >
                        Raise request
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

/* ── Raise-a-request drawer ──────────────────────────────────────────────── */

function RequestDrawer({ unit, meta, busy, onClose, onSubmit }: {
  unit: HousekeepingUnit
  meta: HousekeepingMeta
  busy: boolean
  onClose: () => void
  onSubmit: (body: {
    unit_id: string; request_type: RequestType; priority: Priority;
    clean_type: 'standard' | 'terminal'; description?: string
  }) => void
}) {
  const [type, setType] = useState<RequestType>(
    unit.state === 'occupied' ? 'discharge_turnaround' : 'routine')
  const [priority, setPriority] = useState<Priority>('routine')
  const [terminal, setTerminal] = useState(unit.is_isolation_room)
  const [description, setDescription] = useState('')

  const expected = terminal ? meta.expected_minutes.terminal : meta.expected_minutes.standard

  return (
    <Drawer title={`Cleaning request: ${unit.unit_code}`} onClose={onClose}>
      <form
        className="hk-drawer__form"
        onSubmit={e => {
          e.preventDefault()
          onSubmit({
            unit_id: unit.id, request_type: type, priority,
            clean_type: terminal ? 'terminal' : 'standard',
            description: description.trim() || undefined,
          })
        }}
      >
        <label className="hk-field">
          <span className="hk-field__label">What kind of clean</span>
          <select className="hk-select" value={type}
                  onChange={e => setType(e.target.value as RequestType)}>
            {meta.request_types.map(r => (
              <option key={r} value={r}>{REQUEST_TYPE_LABEL[r] ?? r}</option>
            ))}
          </select>
        </label>

        <fieldset className="hk-radios">
          <legend className="hk-field__label">
            How urgent: a discharge turnaround and a spillage are not the same job
          </legend>
          {meta.priorities.map(p => (
            <label key={p} className={`hk-radio hk-radio--${p}`}>
              <input
                type="radio" name="hk-priority" value={p} checked={priority === p}
                onChange={() => setPriority(p)}
              />
              <span>{PRIORITY_LABEL[p]}</span>
            </label>
          ))}
        </fieldset>

        <label className="hk-check">
          <input type="checkbox" checked={terminal}
                 onChange={e => setTerminal(e.target.checked)} />
          <span>
            Isolation / terminal clean
            <span className="hk-check__hint">
              Longer expected turnaround and different PPE. Expected: {expected} minutes.
            </span>
          </span>
        </label>

        <label className="hk-field">
          <span className="hk-field__label">Note for the cleaner (no patient details)</span>
          <textarea
            className="hk-input hk-textarea" rows={3} value={description}
            onChange={e => setDescription(e.target.value)}
            placeholder="Blood spillage by the window bay"
          />
        </label>

        <div className="hk-drawer__actions">
          <button type="button" className="hk-btn hk-btn--quiet" onClick={onClose}>Cancel</button>
          <button type="submit" className="hk-btn hk-btn--primary" disabled={busy}>
            Raise request
          </button>
        </div>
      </form>
    </Drawer>
  )
}
