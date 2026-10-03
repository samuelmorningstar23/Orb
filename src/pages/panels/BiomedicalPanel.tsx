/**
 * Orb — Biomedical Equipment.
 *
 * ╔════════════════════════════════════════════════════════════════════════╗
 * ║ AN UNCALIBRATED DEVICE IS NOT BROKEN. IT IS WORKING, AND IT IS WRONG.  ║
 * ╚════════════════════════════════════════════════════════════════════════╝
 *
 * WHAT THIS SCREEN IS ORGANISED AROUND. Not "here is your asset register". A
 * biomedical engineer and an accreditation assessor open this on two different
 * questions, and only one of them is urgent:
 *
 *   1. WHAT IS OUT OF CALIBRATION RIGHT NOW — because Orb ingests numbers from
 *      these machines. A drifted pump delivers 88 ml where the chart says 100.
 *      An uncalibrated SpO2 monitor reads 94% where the truth is 88%, Vigil sees
 *      94%, and no alert fires. That is first on the screen, before any total,
 *      and it is the only thing on this panel rendered in a danger tone.
 *   2. Everything else — the register, contracts, downtime — which is the book
 *      an assessor asks for and can wait until after question one is answered.
 *
 * THE OVERDUE COUNT IS NEVER RENDERED AS A QUIET NUMBER. If something is
 * overdue, this panel leads with a sentence saying so. A count in a tile is a
 * thing people stop seeing after a week.
 *
 * WHAT THIS SCREEN DELIBERATELY DOES NOT DO. It does not touch a clinical path.
 * An overdue calibration warns; it never blocks a nurse, and there is no control
 * on this panel that could stop a device being used. See the module docstring in
 * server/services/biomedical_service.py for the open product question about
 * flagging readings from an out-of-calibration device — deliberately not wired
 * tonight, because that is the vitals path.
 *
 * DATES. Every compliance judgement — overdue, days late, cover state — is
 * COMPUTED ON THE SERVER against the hospital's own calendar day and read here
 * as a boolean. This file does no date arithmetic, because a browser in a
 * different timezone doing its own `new Date()` comparison is exactly how a
 * calibration due on 1 April reads as compliant on 1 April.
 *
 * MONEY. The server's own `*_display` strings. An AMC nobody has valued reads
 * "Not configured" — never "₹0.00", which is a number somebody would act on.
 */
import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  BiomedicalApiError, closeServiceCall, complianceTone, coverTone, dueLabel,
  downtimeText, formatDowntime, getAssets, getContracts, getCoverageGaps, getDueRegister,
  getMeta, getOverview, getServiceCalls, openServiceCall, overdueLabel,
  recordCompletion, saveAsset, saveSchedule,
  type Asset, type BiomedicalMeta, type Contract, type Criticality,
  type DueRegister, type Overview, type Schedule, type ScheduleType,
  type ServiceCall,
} from '../../api/biomedical'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../lib/datetime'
import { humaniseEnum, plural } from '../../lib/humanise'
import './BiomedicalPanel.css'

/* ── The sentence that must survive a total network failure ──────────────────
 * The server sends the same rule on /meta and the panel prefers the server's
 * copy once it has one. This literal exists so the rule is on screen in the
 * FIRST PAINT and still there if every request fails — the one statement that
 * must never be missing cannot be one that depends on the network. */
const DUE_RULE =
  'A device is overdue ON its due date, not the day after: the due date is the '
  + 'first day the last calibration certificate no longer covers.'

const PURPOSE_NOTE =
  'Orb reads numbers off these machines. A monitor or pump that has drifted is '
  + 'not broken: it keeps working and it reports the wrong figure, which every '
  + 'layer above it will faithfully record and alert on. The calibration register '
  + 'is the only thing that catches that.'

type View = 'overview' | 'due' | 'register' | 'calls' | 'contracts'

const VIEWS: { id: View; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'due', label: 'Calibration & PM due' },
  { id: 'register', label: 'Equipment register' },
  { id: 'calls', label: 'Breakdowns' },
  { id: 'contracts', label: 'AMC / CMC' },
]

const CRITICALITY_LABEL: Record<string, string> = {
  life_support: 'Life support', critical: 'Critical', routine: 'Routine',
}

const STATUS_LABEL: Record<string, string> = {
  in_service: 'In service', standby: 'Standby', under_maintenance: 'In maintenance',
  down: 'Down', condemned: 'Condemned',
  open: 'Open', in_progress: 'Engineer attending', awaiting_parts: 'Awaiting parts',
  closed: 'Closed', cancelled: 'Cancelled',
  active: 'Active', expiring: 'Expiring', expired: 'Expired',
  terminated: 'Terminated', renewed: 'Renewed',
}

const STATUS_TONE: Record<string, string> = {
  in_service: 'ok', standby: 'neutral', under_maintenance: 'warn',
  down: 'danger', condemned: 'neutral',
  open: 'danger', in_progress: 'warn', awaiting_parts: 'warn',
  closed: 'neutral', cancelled: 'neutral',
}

function label(value: string): string {
  return STATUS_LABEL[value] ?? humaniseEnum(value)
}

/* ── Small presentational pieces ─────────────────────────────────────────── */

function Badge({ value, tone }: { value: string; tone?: string }) {
  const t = tone ?? STATUS_TONE[value] ?? 'neutral'
  return <span className={`bio-badge bio-badge--${t}`}>{label(value)}</span>
}

function Tile({ value, caption, tone }: { value: ReactNode; caption: string; tone?: string }) {
  return (
    <div className="bio-tile">
      <div className={`bio-tile__value${tone ? ` bio-tile__value--${tone}` : ''}`}>{value}</div>
      <div className="bio-tile__caption">{caption}</div>
    </div>
  )
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="bio-empty">
      <div className="bio-empty__title">{title}</div>
      <div className="bio-empty__detail">{detail}</div>
    </div>
  )
}

function Section({ title, action, children }:
{ title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="bio-section">
      <div className="bio-section__header">
        <h3 className="bio-section__title">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  )
}

/** A modal, portalled to document.body so no ancestor's overflow or transform
 *  can clip or re-anchor it. Scoped by its own root class, never by nesting. */
function Modal({ title, onClose, children }:
{ title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return createPortal(
    <div className="bio-modal-root" role="dialog" aria-modal="true" aria-label={title}>
      <div className="bio-modal-scrim" onClick={onClose} />
      <div className="bio-modal">
        <div className="bio-modal__head">
          <h2 className="bio-modal__title">{title}</h2>
          <button type="button" className="bio-modal__close" onClick={onClose}
                  aria-label="Close">×</button>
        </div>
        <div className="bio-modal__body">{children}</div>
      </div>
    </div>,
    document.body,
  )
}

/** A form's own failure, shown inside its modal beside what was typed. It was
 *  sent to the panel's banner, which sits behind the modal's scrim, so a
 *  refused save looked like a button that did nothing. Only a module switched
 *  off still goes to the panel, which then shuts the module view. */
function useFormError(onError: (e: unknown) => void) {
  const [formError, setFormError] = useState<string | null>(null)
  const fail = (err: unknown) => {
    if (err instanceof BiomedicalApiError && err.isModuleDisabled) { onError(err); return }
    setFormError(err instanceof Error ? err.message : 'Something went wrong. Try again.')
  }
  return { formError, setFormError, fail }
}

function FormError({ message }: { message: string | null }) {
  return message ? <div className="bio-alert bio-alert--danger" role="alert">{message}</div> : null
}

function Field({ label: text, hint, children }:
{ label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="bio-field">
      <span className="bio-field__label">{text}</span>
      {children}
      {hint ? <span className="bio-field__hint">{hint}</span> : null}
    </label>
  )
}

/* ── The one row this whole module exists for ─────────────────────────────── */

function ScheduleRow({ schedule, onRecord }:
{ schedule: Schedule; onRecord?: (s: Schedule) => void }) {
  const overdue = schedule.is_overdue
  return (
    <li className={`bio-row${overdue ? ' bio-row--overdue' : ''}`}>
      <div className="bio-row__main">
        <div className="bio-row__title">
          <span className="bio-row__tag">{schedule.asset_asset_tag ?? `#${schedule.asset_id}`}</span>
          <span className="bio-row__name">{schedule.asset_name ?? ''}</span>
          {schedule.asset_criticality === 'life_support'
            ? <Badge value="Life support" tone="danger" /> : null}
        </div>
        <div className="bio-row__meta">
          <span>{schedule.schedule_type_label}</span>
          {schedule.asset_ward ? <span>Ward {schedule.asset_ward}</span> : null}
          <span className="bio-row__date">
            Due {formatClinicalDateKey(schedule.next_due_date)}
            {/* Overdue counts from the due date PLUS the grace days, so the
                grace end is shown: "Due 1 Oct" with "Overdue by 2 days" on
                5 Oct otherwise looks like an arithmetic error. */}
            {schedule.grace_days > 0 && schedule.effective_due_date
              ? `, grace to ${formatClinicalDateKey(schedule.effective_due_date)}` : ''}
          </span>
          {schedule.never_done
            ? <span className="bio-row__never">Never recorded in Orb</span> : null}
        </div>
        {schedule.measurement_at_risk ? (
          <p className="bio-row__risk">
            Orb ingests readings from this device. Its calibration has lapsed, so
            figures recorded from it since {formatClinicalDateKey(schedule.next_due_date)} are not backed
            by a current certificate.
          </p>
        ) : null}
      </div>
      <div className="bio-row__side">
        <span className={`bio-state bio-state--${complianceTone(schedule.compliance)}`}>
          {overdue ? overdueLabel(schedule) : dueLabel(schedule)}
        </span>
        {onRecord ? (
          <button type="button" className="bio-btn bio-btn--quiet"
                  onClick={() => onRecord(schedule)}>
            Record work
          </button>
        ) : null}
      </div>
    </li>
  )
}

/* ── Panel ────────────────────────────────────────────────────────────────── */

export default function BiomedicalPanel() {
  const [view, setView] = useState<View>('overview')
  const [meta, setMeta] = useState<BiomedicalMeta | null>(null)
  const [moduleOff, setModuleOff] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const [overview, setOverview] = useState<Overview | null>(null)
  const [due, setDue] = useState<DueRegister | null>(null)
  const [assets, setAssets] = useState<Asset[]>([])
  const [gaps, setGaps] = useState<Asset[]>([])
  const [calls, setCalls] = useState<ServiceCall[]>([])
  const [contracts, setContracts] = useState<Contract[]>([])

  const [wardFilter, setWardFilter] = useState('')
  const [typeFilter, setTypeFilter] = useState<ScheduleType | ''>('')
  const [search, setSearch] = useState('')

  const [assetModal, setAssetModal] = useState(false)
  const [scheduleFor, setScheduleFor] = useState<Asset | null>(null)
  const [recordFor, setRecordFor] = useState<Schedule | null>(null)
  const [breakdownFor, setBreakdownFor] = useState<Asset | null>(null)

  const handle = useCallback((err: unknown) => {
    if (err instanceof BiomedicalApiError && err.isModuleDisabled) {
      setModuleOff(true)
      setError(err.message)
      return
    }
    setError(err instanceof Error ? err.message : 'Something went wrong.')
  }, [])

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [o, d, g] = await Promise.all([
        getOverview(), getDueRegister({}), getCoverageGaps(),
      ])
      setOverview(o)
      setDue(d)
      setGaps(g.assets)
      setModuleOff(false)
    } catch (err) {
      handle(err)
    } finally {
      setLoading(false)
    }
  }, [handle])

  useEffect(() => {
    getMeta().then(setMeta).catch(handle)
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    void refresh()
  }, [handle, refresh])

  const loadRegister = useCallback(async () => {
    try {
      const res = await getAssets({ ward: wardFilter || undefined,
                                    search: search || undefined, limit: 300 })
      setAssets(res.assets)
    } catch (err) { handle(err) }
  }, [handle, search, wardFilter])

  const loadDue = useCallback(async () => {
    try {
      setDue(await getDueRegister({ ward: wardFilter || undefined,
                                    scheduleType: typeFilter || undefined }))
    } catch (err) { handle(err) }
  }, [handle, typeFilter, wardFilter])

  const loadCalls = useCallback(async () => {
    try {
      const res = await getServiceCalls({ ward: wardFilter || undefined, limit: 200 })
      setCalls(res.service_calls)
    } catch (err) { handle(err) }
  }, [handle, wardFilter])

  const loadContracts = useCallback(async () => {
    try { setContracts((await getContracts()).contracts) } catch (err) { handle(err) }
  }, [handle])

  useEffect(() => {
    if (moduleOff) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    if (view === 'register') void loadRegister()
    if (view === 'due') void loadDue()
    if (view === 'calls') void loadCalls()
    if (view === 'contracts') void loadContracts()
  }, [view, moduleOff, loadRegister, loadDue, loadCalls, loadContracts])

  const dueRule = meta?.due_rule ?? DUE_RULE

  /* The headline. Rendered ABOVE the tabs, so it is on screen whichever view
   * the engineer left the panel on. */
  const headline = useMemo(() => {
    if (!overview) return null
    const { overdue_count: overdue, measurement_at_risk_count: atRisk,
            overdue_life_support_count: lifeSupport } = overview
    if (overdue === 0) {
      return { tone: 'ok' as const,
               text: 'No equipment is overdue for calibration or maintenance.' }
    }
    const bits = [`${overdue} schedule${overdue === 1 ? '' : 's'} overdue`]
    if (lifeSupport > 0) bits.push(`${lifeSupport} on life-support equipment`)
    if (atRisk > 0) {
      bits.push(`${atRisk} on device${atRisk === 1 ? '' : 's'} Orb reads numbers from`)
    }
    return { tone: 'danger' as const, text: `${bits.join(' · ')}.` }
  }, [overview])

  if (moduleOff) {
    return (
      <div className="bio-panel">
        <header className="bio-head">
          <div>
            <h1 className="bio-title">Biomedical equipment</h1>
            <p className="bio-sub">Asset register, calibration and maintenance</p>
          </div>
        </header>
        <div className="bio-notice bio-notice--off">
          <strong>Not available at this site.</strong>{' '}
          {error || 'The biomedical equipment module is switched off for this deployment.'}
          {' '}This hospital keeps its equipment and calibration register elsewhere;
          Orb is not the system of record for it here.
        </div>
      </div>
    )
  }

  return (
    <div className="bio-panel">
      <header className="bio-head">
        <div>
          <h1 className="bio-title">Biomedical equipment</h1>
          <p className="bio-sub">
            Asset register · calibration &amp; preventive maintenance · breakdowns · AMC
          </p>
        </div>
        <div className="bio-head__controls">
          <button type="button" className="bio-btn" onClick={() => setAssetModal(true)}>
            Add equipment
          </button>
          <button type="button" className="bio-btn bio-btn--quiet" onClick={() => void refresh()}>
            Refresh
          </button>
        </div>
      </header>

      {headline ? (
        <div className={`bio-headline bio-headline--${headline.tone}`} role="status">
          <span className="bio-headline__text">{headline.text}</span>
          {headline.tone === 'danger' ? (
            <button type="button" className="bio-btn bio-btn--quiet"
                    onClick={() => setView('due')}>
              Show what is overdue
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="bio-notice">{PURPOSE_NOTE}</div>

      {error && !moduleOff ? <div className="bio-alert bio-alert--danger">{error}</div> : null}

      <nav className="bio-tabs" aria-label="Biomedical views">
        {VIEWS.map(v => (
          <button key={v.id} type="button"
                  className={`bio-tab${view === v.id ? ' bio-tab--on' : ''}`}
                  aria-current={view === v.id ? 'page' : undefined}
                  onClick={() => setView(v.id)}>
            {v.label}
          </button>
        ))}
      </nav>

      {loading && !overview ? (
        <div className="bio-loading">Loading the equipment register…</div>
      ) : null}

      {view === 'overview' && overview ? (
        <div className="bio-stack">
          <div className="bio-tiles">
            <Tile value={overview.overdue_count} caption="Overdue for calibration or PM"
                  tone={overview.overdue_count > 0 ? 'danger' : undefined} />
            <Tile value={overview.measurement_at_risk_count}
                  caption="Measuring devices past calibration"
                  tone={overview.measurement_at_risk_count > 0 ? 'danger' : undefined} />
            <Tile value={overview.assets_down} caption="Out of service now"
                  tone={overview.assets_down > 0 ? 'warn' : undefined} />
            <Tile value={overview.due_soon_count} caption="Falling due soon" />
            <Tile value={overview.contracts_expiring_count} caption="AMC expiring"
                  tone={overview.contracts_expiring_count > 0 ? 'warn' : undefined} />
            <Tile value={overview.assets_total} caption="Assets on the register" />
          </div>

          <Section title="Overdue now">
            {overview.overdue.length === 0 ? (
              <Empty title="Nothing is overdue."
                     detail="Every active schedule on the register is within its interval." />
            ) : (
              <ul className="bio-list">
                {overview.overdue.map(s => (
                  <ScheduleRow key={s.id} schedule={s} onRecord={setRecordFor} />
                ))}
              </ul>
            )}
            <p className="bio-hint">{dueRule}</p>
          </Section>

          <Section title="Equipment with no maintenance schedule at all">
            {gaps.length === 0 ? (
              <Empty title="Every active asset has a schedule."
                     detail="Nothing on the register is invisible to the overdue view." />
            ) : (
              <>
                <div className="bio-alert bio-alert--warn">
                  These assets have no schedule, so they can never appear in the
                  overdue list. The register is silent about them rather than
                  saying they are compliant.
                </div>
                <ul className="bio-list">
                  {gaps.map(a => (
                    <li key={a.id} className="bio-row">
                      <div className="bio-row__main">
                        <div className="bio-row__title">
                          <span className="bio-row__tag">{a.asset_tag}</span>
                          <span className="bio-row__name">{a.name}</span>
                        </div>
                        <div className="bio-row__meta">
                          <span>{CRITICALITY_LABEL[a.criticality] ?? a.criticality}</span>
                          {a.ward ? <span>Ward {a.ward}</span> : null}
                        </div>
                      </div>
                      <div className="bio-row__side">
                        <button type="button" className="bio-btn bio-btn--quiet"
                                onClick={() => setScheduleFor(a)}>
                          Add schedule
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </Section>

          {overview.contracts_expiring.length > 0 ? (
            <Section title="Contracts inside their notice window">
              <ul className="bio-list">
                {overview.contracts_expiring.map(c => (
                  <li key={c.id} className="bio-row">
                    <div className="bio-row__main">
                      <div className="bio-row__title">
                        <span className="bio-row__tag">{c.contract_number}</span>
                        <span className="bio-row__name">{c.vendor_name ?? 'Vendor not recorded'}</span>
                      </div>
                      <div className="bio-row__meta">
                        <span>Ends {formatClinicalDateKey(c.end_date)}</span>
                        <span>{plural(c.asset_count ?? 0, 'asset')}</span>
                        <span>{c.value_display}</span>
                      </div>
                    </div>
                    <div className="bio-row__side">
                      <span className={`bio-state bio-state--${coverTone(c.cover_state)}`}>
                        {c.days_remaining !== null && c.days_remaining >= 0
                          ? `${c.days_remaining} days left` : 'Expired'}
                      </span>
                    </div>
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {overview.downtime_top.length > 0 ? (
            <Section title="Most downtime, last 30 days">
              <ul className="bio-list">
                {overview.downtime_top.map(d => (
                  <li key={d.asset_id} className="bio-row">
                    <div className="bio-row__main">
                      <div className="bio-row__title">
                        <span className="bio-row__tag">{d.asset_tag}</span>
                        <span className="bio-row__name">{d.asset_name}</span>
                      </div>
                      <div className="bio-row__meta">
                        {d.ward ? <span>Ward {d.ward}</span> : null}
                        <span>{label(d.status)}</span>
                      </div>
                    </div>
                    <div className="bio-row__side">
                      <span className="bio-num">{formatDowntime(d.downtime_minutes)}</span>
                    </div>
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}
        </div>
      ) : null}

      {view === 'due' ? (
        <div className="bio-stack">
          <div className="bio-filters">
            <Field label="Ward">
              <input className="bio-input" value={wardFilter}
                     onChange={e => setWardFilter(e.target.value)}
                     placeholder="All wards" />
            </Field>
            <Field label="Type">
              <select className="bio-input" value={typeFilter}
                      onChange={e => setTypeFilter(e.target.value as ScheduleType | '')}>
                <option value="">All</option>
                {(meta?.schedule_types ?? []).map(t => (
                  <option key={t} value={t}>{meta?.schedule_type_labels?.[t] ?? t}</option>
                ))}
              </select>
            </Field>
            <button type="button" className="bio-btn bio-btn--quiet" onClick={() => void loadDue()}>
              Apply
            </button>
          </div>

          <Section title={`Overdue (${due?.overdue_count ?? 0})`}>
            {!due || due.overdue.length === 0 ? (
              <Empty title="Nothing overdue."
                     detail="Every active schedule matching this filter is within its interval." />
            ) : (
              <ul className="bio-list">
                {due.overdue.map(s => (
                  <ScheduleRow key={s.id} schedule={s} onRecord={setRecordFor} />
                ))}
              </ul>
            )}
          </Section>

          <Section title={`Falling due soon (${due?.due_soon_count ?? 0})`}>
            {!due || due.due_soon.length === 0 ? (
              <Empty title="Nothing due in the lookahead window."
                     detail="Schedules appear here once they are inside their lead time." />
            ) : (
              <ul className="bio-list">
                {due.due_soon.map(s => (
                  <ScheduleRow key={s.id} schedule={s} onRecord={setRecordFor} />
                ))}
              </ul>
            )}
          </Section>
          <p className="bio-hint">{dueRule}</p>
        </div>
      ) : null}

      {view === 'register' ? (
        <div className="bio-stack">
          <div className="bio-filters">
            <Field label="Search">
              <input className="bio-input" value={search}
                     onChange={e => setSearch(e.target.value)}
                     placeholder="Tag, name, model, serial" />
            </Field>
            <Field label="Ward">
              <input className="bio-input" value={wardFilter}
                     onChange={e => setWardFilter(e.target.value)} placeholder="All wards" />
            </Field>
            <button type="button" className="bio-btn bio-btn--quiet"
                    onClick={() => void loadRegister()}>Apply</button>
          </div>

          {assets.length === 0 ? (
            <Empty title="No equipment on the register yet."
                   detail="Orb ships this register empty on purpose. An equipment list with example rows is one somebody has to audit before they can trust it." />
          ) : (
            <div className="bio-table-wrap">
              <table className="bio-table">
                <thead>
                  <tr>
                    <th>Tag</th><th>Equipment</th><th>Make / model</th>
                    <th>Location</th><th>Criticality</th><th>Status</th>
                    <th>Warranty</th><th aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {assets.map(a => (
                    <tr key={a.id}>
                      <td className="bio-cell-tag">{a.asset_tag}</td>
                      <td>
                        {a.name}
                        {a.produces_clinical_readings
                          ? <span className="bio-chip">Orb reads this</span> : null}
                      </td>
                      <td className="bio-cell-quiet">
                        {[a.manufacturer, a.model].filter(Boolean).join(' ') || '—'}
                      </td>
                      <td className="bio-cell-quiet">
                        {[a.ward && `Ward ${a.ward}`, a.department].filter(Boolean).join(' · ') || '—'}
                      </td>
                      <td>{CRITICALITY_LABEL[a.criticality] ?? a.criticality}</td>
                      <td><Badge value={a.status} /></td>
                      <td className="bio-cell-quiet">
                        {a.warranty_expiry_date
                          ? (a.warranty_active ? `To ${formatClinicalDateKey(a.warranty_expiry_date)}` : 'Expired')
                          : '—'}
                      </td>
                      <td className="bio-cell-actions">
                        <button type="button" className="bio-btn bio-btn--quiet"
                                onClick={() => setScheduleFor(a)}>Schedule</button>
                        <button type="button" className="bio-btn bio-btn--quiet"
                                onClick={() => setBreakdownFor(a)}>Log fault</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ) : null}

      {view === 'calls' ? (
        <div className="bio-stack">
          <Section title="Service calls">
            {calls.length === 0 ? (
              <Empty title="No service calls logged."
                     detail="Log a fault from the equipment register to start tracking downtime." />
            ) : (
              <ul className="bio-list">
                {calls.map(c => (
                  <li key={c.id} className={`bio-row${c.is_open ? ' bio-row--overdue' : ''}`}>
                    <div className="bio-row__main">
                      <div className="bio-row__title">
                        <span className="bio-row__tag">{c.asset_tag ?? `#${c.asset_id}`}</span>
                        <span className="bio-row__name">{c.fault_description}</span>
                      </div>
                      <div className="bio-row__meta">
                        <Badge value={c.status} />
                        {c.ward ? <span>Ward {c.ward}</span> : null}
                        <span>Reported {formatClinicalDateTime(c.reported_at)}</span>
                        <span>{downtimeText(c.downtime_minutes_effective, !!c.downtime_is_running)}</span>
                      </div>
                    </div>
                    <div className="bio-row__side">
                      {c.is_open ? (
                        <button type="button" className="bio-btn bio-btn--quiet"
                                onClick={async () => {
                                  try {
                                    await closeServiceCall(c.id, { return_to_service: true })
                                    await loadCalls()
                                    await refresh()
                                  } catch (err) { handle(err) }
                                }}>
                          Back in service
                        </button>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </div>
      ) : null}

      {view === 'contracts' ? (
        <div className="bio-stack">
          <Section title="Maintenance contracts">
            {contracts.length === 0 ? (
              <Empty title="No AMC or CMC recorded."
                     detail="A contract records who is obliged to service this equipment, by when, and what it covers." />
            ) : (
              <div className="bio-table-wrap">
                <table className="bio-table">
                  <thead>
                    <tr>
                      <th>Number</th><th>Type</th><th>Vendor</th><th>Cover</th>
                      <th>Ends</th><th>Assets</th><th>Value</th><th>State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {contracts.map(c => (
                      <tr key={c.id}>
                        <td className="bio-cell-tag">{c.contract_number}</td>
                        <td>{c.contract_type.toUpperCase()}</td>
                        <td className="bio-cell-quiet">{c.vendor_name ?? '—'}</td>
                        <td className="bio-cell-quiet">
                          {c.covers_calibration ? 'Calibration' : 'Maintenance only'}
                        </td>
                        <td className="bio-cell-num">{formatClinicalDateKey(c.end_date)}</td>
                        <td className="bio-cell-num">{c.asset_count ?? 0}</td>
                        <td className={`bio-cell-num${c.value_is_configured ? '' : ' bio-cell-unset'}`}>
                          {c.value_display}
                        </td>
                        <td><Badge value={c.cover_state} tone={coverTone(c.cover_state)} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <p className="bio-hint">
              {meta?.contract_rule
                ?? 'A contract is expired when today is after its end date. The end date is the last day it covers.'}
            </p>
          </Section>
        </div>
      ) : null}

      {assetModal ? (
        <AssetForm meta={meta} onClose={() => setAssetModal(false)}
                   onSaved={async () => { setAssetModal(false); await refresh(); await loadRegister() }}
                   onError={handle} />
      ) : null}

      {scheduleFor ? (
        <ScheduleForm asset={scheduleFor} meta={meta} onClose={() => setScheduleFor(null)}
                      onSaved={async () => { setScheduleFor(null); await refresh() }}
                      onError={handle} />
      ) : null}

      {recordFor ? (
        <CompletionForm schedule={recordFor} onClose={() => setRecordFor(null)}
                        onSaved={async () => { setRecordFor(null); await refresh(); await loadDue() }}
                        onError={handle} />
      ) : null}

      {breakdownFor ? (
        <BreakdownForm asset={breakdownFor} onClose={() => setBreakdownFor(null)}
                       onSaved={async () => { setBreakdownFor(null); await refresh(); await loadCalls() }}
                       onError={handle} />
      ) : null}
    </div>
  )
}

/* ── Forms ────────────────────────────────────────────────────────────────── */

function AssetForm({ meta, onClose, onSaved, onError }: {
  meta: BiomedicalMeta | null
  onClose: () => void
  onSaved: () => Promise<void> | void
  onError: (e: unknown) => void
}) {
  const [tag, setTag] = useState('')
  const [name, setName] = useState('')
  const [category, setCategory] = useState('patient_monitor')
  const [manufacturer, setManufacturer] = useState('')
  const [model, setModel] = useState('')
  const [serial, setSerial] = useState('')
  const [ward, setWard] = useState('')
  const [department, setDepartment] = useState('')
  const [criticality, setCriticality] = useState<Criticality>('routine')
  const [purchaseDate, setPurchaseDate] = useState('')
  const [warranty, setWarranty] = useState('')
  const [reads, setReads] = useState(false)
  const [busy, setBusy] = useState(false)
  // A tag already on the register is answered here, beside the field, so the
  // form keeps what was typed and nothing on file is overwritten.
  const [duplicate, setDuplicate] = useState<string | null>(null)
  const { formError, setFormError, fail } = useFormError(onError)

  const submit = async () => {
    setBusy(true)
    setDuplicate(null)
    setFormError(null)
    try {
      await saveAsset({
        asset_tag: tag, name, category, manufacturer: manufacturer || null,
        model: model || null, serial_number: serial || null,
        ward: ward || null, department: department || null, criticality,
        purchase_date: purchaseDate || null, warranty_expiry_date: warranty || null,
        produces_clinical_readings: reads,
        create_only: true,
      })
      await onSaved()
    } catch (err) {
      if (err instanceof BiomedicalApiError && err.code === 'already_exists') setDuplicate(err.message)
      else fail(err)
    } finally { setBusy(false) }
  }

  return (
    <Modal title="Add equipment" onClose={onClose}>
      {duplicate ? <div className="bio-alert bio-alert--danger" role="alert">{duplicate}</div> : null}
      <FormError message={formError} />
      <div className="bio-form">
        <Field label="Asset tag" hint="The hospital's own label, the sticker on the machine.">
          <input className="bio-input" value={tag} onChange={e => setTag(e.target.value)} />
        </Field>
        <Field label="Equipment name">
          <input className="bio-input" value={name} onChange={e => setName(e.target.value)} />
        </Field>
        <Field label="Category">
          <select className="bio-input" value={category} onChange={e => setCategory(e.target.value)}>
            {(meta?.asset_categories ?? ['other']).map(c => (
              <option key={c} value={c}>{humaniseEnum(c)}</option>
            ))}
          </select>
        </Field>
        <Field label="Manufacturer">
          <input className="bio-input" value={manufacturer}
                 onChange={e => setManufacturer(e.target.value)} />
        </Field>
        <Field label="Model">
          <input className="bio-input" value={model} onChange={e => setModel(e.target.value)} />
        </Field>
        <Field label="Serial number">
          <input className="bio-input" value={serial} onChange={e => setSerial(e.target.value)} />
        </Field>
        <Field label="Ward">
          <input className="bio-input" value={ward} onChange={e => setWard(e.target.value)} />
        </Field>
        <Field label="Department">
          <input className="bio-input" value={department}
                 onChange={e => setDepartment(e.target.value)} />
        </Field>
        <Field label="Criticality">
          <select className="bio-input" value={criticality}
                  onChange={e => setCriticality(e.target.value as Criticality)}>
            {(meta?.criticalities ?? ['routine']).map(c => (
              <option key={c} value={c}>{CRITICALITY_LABEL[c] ?? c}</option>
            ))}
          </select>
        </Field>
        <Field label="Purchase date">
          <input className="bio-input" type="date" value={purchaseDate}
                 onChange={e => setPurchaseDate(e.target.value)} />
        </Field>
        <Field label="Warranty expires">
          <input className="bio-input" type="date" value={warranty}
                 onChange={e => setWarranty(e.target.value)} />
        </Field>
        <label className="bio-check">
          <input type="checkbox" checked={reads} onChange={e => setReads(e.target.checked)} />
          <span>
            Orb ingests readings from this device
            <span className="bio-field__hint">
              Tick this for monitors, pumps and ventilators whose numbers reach Orb.
              An overdue calibration on one of these is flagged separately, because
              the readings it produced are the ones nothing else can catch.
            </span>
          </span>
        </label>
      </div>
      <div className="bio-form__actions">
        <button type="button" className="bio-btn bio-btn--quiet" onClick={onClose}>Cancel</button>
        <button type="button" className="bio-btn" disabled={busy || !tag.trim() || !name.trim()}
                onClick={() => void submit()}>
          {busy ? 'Saving…' : 'Save equipment'}
        </button>
      </div>
    </Modal>
  )
}

function ScheduleForm({ asset, meta, onClose, onSaved, onError }: {
  asset: Asset
  meta: BiomedicalMeta | null
  onClose: () => void
  onSaved: () => Promise<void> | void
  onError: (e: unknown) => void
}) {
  const [type, setType] = useState<ScheduleType>('calibration')
  const [intervalDays, setIntervalDays] = useState('365')
  const [basis, setBasis] = useState<'last_done' | 'first_due'>('last_done')
  const [lastDone, setLastDone] = useState('')
  const [firstDue, setFirstDue] = useState('')
  const [certificate, setCertificate] = useState('')
  const [busy, setBusy] = useState(false)
  const { formError, setFormError, fail } = useFormError(onError)

  const submit = async () => {
    setFormError(null)
    // Checked here so a blank or "6 months" never reaches the server as NaN.
    const days = Number(intervalDays)
    if (!Number.isInteger(days) || days < 1) {
      setFormError('Interval must be a whole number of days, 1 or more.')
      return
    }
    setBusy(true)
    try {
      await saveSchedule({
        asset_id: asset.id, schedule_type: type,
        interval_days: days,
        last_done_date: basis === 'last_done' ? (lastDone || null) : null,
        first_due_date: basis === 'first_due' ? (firstDue || null) : null,
        certificate_reference: certificate || null,
      })
      await onSaved()
    } catch (err) { fail(err) } finally { setBusy(false) }
  }

  return (
    <Modal title={`Schedule: ${asset.asset_tag}`} onClose={onClose}>
      <FormError message={formError} />
      <div className="bio-form">
        <Field label="Type">
          <select className="bio-input" value={type}
                  onChange={e => setType(e.target.value as ScheduleType)}>
            {(meta?.schedule_types ?? ['calibration']).map(t => (
              <option key={t} value={t}>{meta?.schedule_type_labels?.[t] ?? t}</option>
            ))}
          </select>
        </Field>
        <Field label="Interval (days)"
               hint="Days, not months, “every 3 months” is a different length depending on the month, which nobody can staff around.">
          <input className="bio-input" type="number" min={1} value={intervalDays}
                 onChange={e => setIntervalDays(e.target.value)} />
        </Field>
        <Field label="Base the due date on">
          <select className="bio-input" value={basis}
                  onChange={e => setBasis(e.target.value as 'last_done' | 'first_due')}>
            <option value="last_done">When it was last done</option>
            <option value="first_due">A first due date I will set</option>
          </select>
        </Field>
        {basis === 'last_done' ? (
          <Field label="Last done on">
            <input className="bio-input" type="date" value={lastDone}
                   onChange={e => setLastDone(e.target.value)} />
          </Field>
        ) : (
          <Field label="First due on"
                 hint="Use this for equipment whose last service Orb has no record of. It will read as “never recorded in Orb”, which is the truth.">
            <input className="bio-input" type="date" value={firstDue}
                   onChange={e => setFirstDue(e.target.value)} />
          </Field>
        )}
        <Field label="Certificate reference">
          <input className="bio-input" value={certificate}
                 onChange={e => setCertificate(e.target.value)} />
        </Field>
      </div>
      <div className="bio-alert bio-alert--warn">
        Orb will not assume this machine was serviced today just because it is
        being entered today. One of the two dates above is required.
      </div>
      <div className="bio-form__actions">
        <button type="button" className="bio-btn bio-btn--quiet" onClick={onClose}>Cancel</button>
        <button type="button" className="bio-btn"
                disabled={busy || (basis === 'last_done' ? !lastDone : !firstDue)}
                onClick={() => void submit()}>
          {busy ? 'Saving…' : 'Save schedule'}
        </button>
      </div>
    </Modal>
  )
}

function CompletionForm({ schedule, onClose, onSaved, onError }: {
  schedule: Schedule
  onClose: () => void
  onSaved: () => Promise<void> | void
  onError: (e: unknown) => void
}) {
  const [performed, setPerformed] = useState('')
  const [result, setResult] = useState<'pass' | 'pass_with_adjustment' | 'fail'>('pass')
  const [certificate, setCertificate] = useState('')
  const [by, setBy] = useState('')
  const [busy, setBusy] = useState(false)
  const { formError, setFormError, fail } = useFormError(onError)

  const submit = async () => {
    setBusy(true)
    setFormError(null)
    try {
      await recordCompletion({
        schedule_id: schedule.id, performed_date: performed || undefined, result,
        certificate_reference: certificate || null, performed_by: by || null,
      })
      await onSaved()
    } catch (err) { fail(err) } finally { setBusy(false) }
  }

  return (
    <Modal title={`Record ${schedule.schedule_type_label.toLowerCase()}`} onClose={onClose}>
      <FormError message={formError} />
      <p className="bio-hint">
        {schedule.asset_asset_tag} ({schedule.asset_name}). Currently due{' '}
        {formatClinicalDateKey(schedule.next_due_date)}.
      </p>
      <div className="bio-form">
        <Field label="Performed on" hint="Cannot be in the future.">
          <input className="bio-input" type="date" value={performed}
                 onChange={e => setPerformed(e.target.value)} />
        </Field>
        <Field label="Result">
          <select className="bio-input" value={result}
                  onChange={e => setResult(e.target.value as typeof result)}>
            <option value="pass">Pass</option>
            <option value="pass_with_adjustment">Pass, after adjustment</option>
            <option value="fail">Fail</option>
          </select>
        </Field>
        <Field label="Certificate reference">
          <input className="bio-input" value={certificate}
                 onChange={e => setCertificate(e.target.value)} />
        </Field>
        <Field label="Performed by">
          <input className="bio-input" value={by} onChange={e => setBy(e.target.value)} />
        </Field>
      </div>
      {result === 'fail' ? (
        <div className="bio-alert bio-alert--danger">
          A failed calibration certifies nothing. This equipment will stay in the
          overdue list and the attempt will be recorded against it.
        </div>
      ) : null}
      <div className="bio-form__actions">
        <button type="button" className="bio-btn bio-btn--quiet" onClick={onClose}>Cancel</button>
        <button type="button" className="bio-btn" disabled={busy}
                onClick={() => void submit()}>
          {busy ? 'Saving…' : 'Record'}
        </button>
      </div>
    </Modal>
  )
}

function BreakdownForm({ asset, onClose, onSaved, onError }: {
  asset: Asset
  onClose: () => void
  onSaved: () => Promise<void> | void
  onError: (e: unknown) => void
}) {
  const [fault, setFault] = useState('')
  const [severity, setSeverity] = useState<'breakdown' | 'degraded' | 'preventive'>('breakdown')
  const [outOfService, setOutOfService] = useState(true)
  const [busy, setBusy] = useState(false)
  const { formError, setFormError, fail } = useFormError(onError)

  const submit = async () => {
    setBusy(true)
    setFormError(null)
    try {
      await openServiceCall({
        asset_id: asset.id, fault_description: fault, severity,
        took_out_of_service: outOfService, ward: asset.ward,
      })
      await onSaved()
    } catch (err) { fail(err) } finally { setBusy(false) }
  }

  return (
    <Modal title={`Log a fault: ${asset.asset_tag}`} onClose={onClose}>
      <FormError message={formError} />
      <div className="bio-form">
        <Field label="What is wrong">
          <textarea className="bio-input bio-input--area" rows={3} value={fault}
                    onChange={e => setFault(e.target.value)} />
        </Field>
        <Field label="Severity">
          <select className="bio-input" value={severity}
                  onChange={e => setSeverity(e.target.value as typeof severity)}>
            <option value="breakdown">Breakdown</option>
            <option value="degraded">Working, but degraded</option>
            <option value="preventive">Preventive visit</option>
          </select>
        </Field>
        <label className="bio-check">
          <input type="checkbox" checked={outOfService}
                 onChange={e => setOutOfService(e.target.checked)} />
          <span>
            This machine is out of service
            <span className="bio-field__hint">
              Only ticked faults accrue downtime. A noisy fan is a service call,
              not an outage, and counting it would make the availability figure
              meaningless.
            </span>
          </span>
        </label>
      </div>
      <div className="bio-form__actions">
        <button type="button" className="bio-btn bio-btn--quiet" onClick={onClose}>Cancel</button>
        <button type="button" className="bio-btn" disabled={busy || !fault.trim()}
                onClick={() => void submit()}>
          {busy ? 'Saving…' : 'Log fault'}
        </button>
      </div>
    </Modal>
  )
}
