/**
 * Orb — NABH accreditation evidence.
 *
 * ╔════════════════════════════════════════════════════════════════════════╗
 * ║ AN INDICATOR WITH INSUFFICIENT DATA IS NOT ZERO AND IT IS NOT 100 %.   ║
 * ╚════════════════════════════════════════════════════════════════════════╝
 *
 * This screen is read by a quality committee and, eventually, by an assessor.
 * The single worst thing it can do is print a number nobody's data supports —
 * not because the assessor catches it (they will), but because a committee that
 * reads "0 medication errors" stops looking for medication errors.
 *
 * So every indicator card renders through `indicatorResult()` (src/api/nabh.ts),
 * which returns WORDS for every non-reported state and never a digit, a dash or
 * a blank. The server has already stripped the value; this is the second,
 * independent gate, in a different language, for the same reason the shadow
 * scorecard has one.
 *
 * WHAT THE SCREEN IS ORGANISED AROUND. Not "here are your tables". A quality
 * manager opens this on three questions — what do my indicators say, what has
 * been reported and not yet closed, and can I produce the pack — so those are
 * the three views, in that order. The gap list ("what Orb does NOT collect") is
 * on the indicators view rather than hidden in a settings page, because the
 * hospital has to be able to say what is missing before the assessor says it.
 *
 * ANONYMOUS REPORTING. The statement above the checkbox is the SERVER's
 * sentence, and the panel keeps a compile-time copy so it is on screen in the
 * first paint and still there if every request fails. A promise of anonymity
 * that only appears when the network is up is not a promise. It also admits
 * what it cannot guarantee, which is why it is long.
 *
 * FACILITIES (multisite-12). With more than one facility of a hospital group on
 * the appliance the server keeps the indicators, the register and the packs to
 * the reader's facility and names it (`facility`), and lists the facilities the
 * reader may choose (`facilities`). A reader with group-wide access gets a
 * facility filter: the indicators and the register for the whole group or one
 * facility, and a pack always for one facility, because NABH accredits each
 * hospital on its own. With one facility none of this is drawn.
 */
import { type ReactNode, useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  addAction, closeIncident, completeAction, generatePack, generatePackMarkdown,
  getExports, getIncident, getIncidents, getIndicators, getMeta,
  indicatorReason, indicatorResult, NabhApiError, recordInvestigation,
  reportIncident, startInvestigation, voidIncident,
  type EvidencePack, type ExportRow, type Incident, type IndicatorDashboard,
  type Indicator, type NabhMeta,
} from '../../api/nabh'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { formatClinicalDate, formatClinicalDateTime, localDateKey } from '../../lib/datetime'
import { humaniseEnum } from '../../lib/humanise'
import './NabhPanel.css'

/* ── The anonymity statement, duplicated on purpose ──────────────────────────
 * The server sends this same sentence on /meta and on every report response,
 * and the panel prefers the server's copy once it has one. This literal exists
 * so the words are on screen in the FIRST PAINT and still there if every
 * request fails. A promise that depends on the network being up is not one. */
const ANONYMITY_FALLBACK =
  'An anonymous report stores no reporter name, id or role. That is enforced '
  + 'by a database constraint, and the reporting time is recorded only to the '
  + 'day. It is NOT untraceable: Orb’s platform audit trail records which '
  + 'account called which endpoint, so an administrator with audit access could '
  + 'correlate that with the report. If you need a channel nobody can trace, '
  + 'use one outside this system.'

type View = 'indicators' | 'register' | 'report' | 'pack'

/** A facility as the NABH answers name it (multisite-12). */
interface NabhFacility { id: number; code: string | null; name: string | null }

/** The facility keys an answer carries while more than one facility is on record. */
interface FacilityFields { facility?: NabhFacility | null; facilities?: NabhFacility[] }

/** The facility in words, for the filter and the headings. */
function facilityName(facility: NabhFacility | null | undefined): string {
  if (!facility) return 'All facilities'
  const name = facility.name || facility.code || `Facility ${facility.id}`
  return facility.code && facility.name ? `${name} (${facility.code})` : name
}

/** The facility the reader chose, as the request key the server reads. The
 *  NABH client sends every key of a query or a body as given. */
function withFacility<T extends object>(params: T, facility: number | undefined): T {
  return facility === undefined ? params : { ...params, facility }
}

const VIEWS: { id: View; label: string }[] = [
  { id: 'indicators', label: 'Quality indicators' },
  { id: 'register', label: 'Incident register' },
  { id: 'report', label: 'Report an incident' },
  { id: 'pack', label: 'Evidence pack' },
]

const STATUS_LABEL: Record<string, string> = {
  reported: 'Reported',
  under_investigation: 'Under investigation',
  actions_pending: 'Actions pending',
  closed: 'Closed',
  void: 'Void',
}

const STATUS_TONE: Record<string, string> = {
  reported: 'info',
  under_investigation: 'warn',
  actions_pending: 'warn',
  closed: 'ok',
  void: 'neutral',
}

const SEVERITY_TONE: Record<string, string> = {
  near_miss: 'info',
  no_harm: 'info',
  minor: 'neutral',
  moderate: 'warn',
  major: 'danger',
  sentinel: 'danger',
}

/** The reporter's name the server joined on, else the stored id: the register
 *  printed login ids. An anonymous report has neither. */
function reporterName(incident: { reported_by: string | null; reported_by_name?: string | null }): string | null {
  return incident.reported_by_name || incident.reported_by
}

/** Now as a datetime-local value ("2026-10-03T14:05"), in local time. */
function nowLocalInput(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${localDateKey(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function words(raw: string): string {
  return humaniseEnum(raw, raw)
}

// LOCAL calendar days. toISOString() gives the UTC date, so between 00:00
// and 05:30 IST the default "To" was yesterday and left today's incidents out.
function isoDaysAgo(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return localDateKey(d)
}

function today(): string {
  return localDateKey()
}

/* ── Small presentational pieces ─────────────────────────────────────────── */

function Badge({ text, tone }: { text: string; tone: string }) {
  return <span className={`nabh-badge nabh-badge--${tone}`}>{text}</span>
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="nabh-empty">
      <div className="nabh-empty__title">{title}</div>
      <div className="nabh-empty__detail">{detail}</div>
    </div>
  )
}

function Field({ label, children, hint }: {
  label: string; children: ReactNode; hint?: string
}) {
  return (
    <label className="nabh-field">
      <span className="nabh-field__label">{label}</span>
      {children}
      {hint ? <span className="nabh-field__hint">{hint}</span> : null}
    </label>
  )
}

/**
 * One indicator.
 *
 * The result line is the whole point of this component: `indicatorResult` gives
 * back either a measured number or the WORDS for why there isn't one, and this
 * renders exactly what it is given. There is no `|| 0`, no `?? '—'` and no
 * conditional that could fall through to an empty cell.
 */
function IndicatorCard({ indicator }: { indicator: Indicator }) {
  const result = indicatorResult(indicator)
  const reason = indicatorReason(indicator)
  const excludedEntries = Object.entries(indicator.excluded ?? {}).filter(
    ([key, value]) => key !== 'why' && typeof value === 'number' && value > 0,
  )
  return (
    <article className={`nabh-card nabh-card--${result.state}`}>
      <header className="nabh-card__head">
        <span className="nabh-card__code">{indicator.code}</span>
        {indicator.is_proxy ? <Badge text="Proxy measure" tone="warn" /> : null}
      </header>
      <h3 className="nabh-card__label">{indicator.label}</h3>
      <p className={`nabh-card__result${result.isNumber ? '' : ' nabh-card__result--absent'}`}>
        {result.text}
      </p>
      {result.isNumber ? (
        <p className="nabh-card__denominator">
          {indicator.numerator} {indicator.numerator_label} of{' '}
          {indicator.denominator} {indicator.denominator_label}
        </p>
      ) : (
        <p className="nabh-card__reason">{reason}</p>
      )}
      {excludedEntries.map(([key, value]) => (
        <p className="nabh-card__excluded" key={key}>
          Excluded: {words(key).toLowerCase()}: {String(value)}.{' '}
          {String((indicator.excluded as Record<string, unknown>).why ?? '')}
        </p>
      ))}
      <details className="nabh-card__more">
        <summary>Definition and caveats</summary>
        <p className="nabh-card__definition">{indicator.definition}</p>
        <p className="nabh-card__caveat">{indicator.caveat}</p>
        <p className="nabh-card__sources">
          Computed from: {indicator.sources.join(', ')}
        </p>
      </details>
    </article>
  )
}

/* ── The panel ───────────────────────────────────────────────────────────── */

export default function NabhPanel() {
  const [view, setView] = useState<View>('indicators')
  const [meta, setMeta] = useState<NabhMeta | null>(null)
  const [moduleOff, setModuleOff] = useState<string | null>(null)
  const [readDenied, setReadDenied] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [reloadToken, setReloadToken] = useState(0)

  const [periodStart, setPeriodStart] = useState(isoDaysAgo(90))
  const [periodEnd, setPeriodEnd] = useState(today())
  const [ward, setWard] = useState('')
  // multisite-12: the facility a group-wide reader chose (undefined: all of
  // them for the indicators and register), and the ones the server lists.
  const [facility, setFacility] = useState<number | undefined>(undefined)
  const [facilities, setFacilities] = useState<NabhFacility[]>([])
  const chooseFacility = facilities.length > 1

  const [dashboard, setDashboard] = useState<(IndicatorDashboard & FacilityFields) | null>(null)
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [selected, setSelected] = useState<Incident | null>(null)
  const [exports, setExports] = useState<ExportRow[]>([])
  const [pack, setPack] = useState<EvidencePack | null>(null)

  const reload = useCallback(() => setReloadToken((n) => n + 1), [])

  /** One place every failure lands, so a switched-off module can never be
   *  mistaken for a hospital with no incidents. */
  const handle = useCallback((err: unknown) => {
    if (err instanceof NabhApiError && err.isModuleDisabled) {
      setModuleOff(err.message)
      return
    }
    if (err instanceof NabhApiError && err.isForbidden) {
      setReadDenied(err.message)
      return
    }
    setError(err instanceof Error ? err.message : String(err))
  }, [])

  useEffect(() => {
    let live = true
    getMeta().then((m) => { if (live) setMeta(m) }).catch(handle)
    return () => { live = false }
  }, [handle])

  useEffect(() => {
    if (moduleOff || readDenied) return
    let live = true
    const params = { period_start: periodStart, period_end: periodEnd, ward: ward || undefined }
    if (view === 'indicators') {
      getIndicators(withFacility(params, facility)).then((d) => {
        if (!live) return
        const scoped = d as IndicatorDashboard & FacilityFields
        setDashboard(scoped)
        setFacilities(scoped.facilities ?? [])
      }).catch(handle)
    } else if (view === 'register') {
      getIncidents(withFacility({ ...params, include_void: true, limit: 200 }, facility))
        .then((r) => {
          if (!live) return
          setIncidents(r.incidents)
          setFacilities((r as typeof r & FacilityFields).facilities ?? [])
        }).catch(handle)
    } else if (view === 'pack') {
      getExports().then((r) => { if (live) setExports(r.exports) }).catch(handle)
    }
    return () => { live = false }
  }, [view, periodStart, periodEnd, ward, facility, reloadToken, moduleOff, readDenied, handle])

  const anonymityStatement = meta?.anonymity_statement ?? ANONYMITY_FALLBACK

  const openIncident = useCallback(async (id: number) => {
    try {
      setSelected(await getIncident(id))
    } catch (err) { handle(err) }
  }, [handle])

  const act = useCallback(async (fn: () => Promise<Incident>, message: string) => {
    setBusy(true)
    setError(null)
    try {
      setSelected(await fn())
      setFlash(message)
      reload()
    } catch (err) { handle(err) } finally { setBusy(false) }
  }, [handle, reload])

  if (moduleOff) {
    return (
      <div className="nabh-panel">
        <h1 className="nabh-title">NABH accreditation evidence</h1>
        <div className="nabh-notice nabh-notice--off">{moduleOff}</div>
      </div>
    )
  }

  return (
    <div className="nabh-panel">
      <header className="nabh-head">
        <div>
          <h1 className="nabh-title">NABH accreditation evidence</h1>
          <p className="nabh-sub">
            Orb is not accredited and does not accredit. NABH accredits the
            hospital. This is the evidence surface: the incident register, the
            indicators Orb can compute from its own records, and the pack.
          </p>
        </div>
        <div className="nabh-head__controls">
          <Field label="From">
            <input type="date" className="nabh-input" value={periodStart}
                   onChange={(e) => setPeriodStart(e.target.value)} />
          </Field>
          <Field label="To">
            <input type="date" className="nabh-input" value={periodEnd}
                   onChange={(e) => setPeriodEnd(e.target.value)} />
          </Field>
          <Field label="Ward">
            {/* A select, not free text: a typed ward refetched on every
                keystroke, and one typed in the wrong case emptied the register. */}
            <WardSelect wards={meta?.wards} value={ward} onChange={setWard}
                        emptyLabel="All wards" />
          </Field>
          {chooseFacility ? (
            <Field label="Facility">
              <select className="nabh-input" value={facility === undefined ? '' : String(facility)}
                      onChange={(e) => setFacility(e.target.value ? Number(e.target.value) : undefined)}>
                <option value="">All facilities</option>
                {facilities.map((f) => (
                  <option key={f.id} value={String(f.id)}>{facilityName(f)}</option>
                ))}
              </select>
            </Field>
          ) : null}
        </div>
      </header>
      {dashboard?.facility && !chooseFacility ? (
        <p className="nabh-sub">Figures for {facilityName(dashboard.facility)}.</p>
      ) : null}

      <nav className="nabh-tabs" role="tablist">
        {VIEWS.map((v) => (
          <button key={v.id} type="button" role="tab" aria-selected={view === v.id}
                  className={`nabh-tab${view === v.id ? ' nabh-tab--active' : ''}`}
                  onClick={() => { setView(v.id); setError(null); setFlash(null) }}>
            {v.label}
          </button>
        ))}
      </nav>

      {error ? <div className="nabh-notice nabh-notice--error">{error}</div> : null}
      {flash ? <div className="nabh-notice nabh-notice--ok">{flash}</div> : null}
      {readDenied && view !== 'report' ? (
        <div className="nabh-notice nabh-notice--warn">
          {readDenied} You can still file a report from the
          {' '}<strong>Report an incident</strong> tab.
        </div>
      ) : null}

      {view === 'indicators' ? (
        <IndicatorsView dashboard={dashboard} denied={!!readDenied} />
      ) : null}

      {view === 'register' ? (
        <RegisterView incidents={incidents} denied={!!readDenied}
                      onOpen={openIncident} />
      ) : null}

      {view === 'report' ? (
        <ReportView meta={meta} anonymityStatement={anonymityStatement}
                    defaultWard={ward}
                    onDone={(reference) => {
                      setFlash(`Reported. Reference ${reference}.`)
                      reload()
                    }}
                    onError={handle} />
      ) : null}

      {view === 'pack' ? (
        <PackView exports={exports} pack={pack} denied={!!readDenied}
                  periodStart={periodStart} periodEnd={periodEnd} ward={ward}
                  busy={busy}
                  facilityLabel={chooseFacility && facility !== undefined
                    ? facilityName(facilities.find((f) => f.id === facility)) : null}
                  needsFacility={chooseFacility && facility === undefined}
                  onGenerate={async (includeIdentifiers) => {
                    setBusy(true); setError(null)
                    try {
                      const generated = await generatePack(withFacility({
                        period_start: periodStart, period_end: periodEnd,
                        ward: ward || undefined,
                        include_patient_identifiers: includeIdentifiers,
                      }, facility))
                      setPack(generated)
                      setFlash(`Pack ${generated.reference} generated.`)
                      reload()
                    } catch (err) { handle(err) } finally { setBusy(false) }
                  }}
                  onDownloadMarkdown={async (includeIdentifiers) => {
                    setBusy(true); setError(null)
                    try {
                      const text = await generatePackMarkdown(withFacility({
                        period_start: periodStart, period_end: periodEnd,
                        ward: ward || undefined,
                        include_patient_identifiers: includeIdentifiers,
                      }, facility))
                      const blob = new Blob([text], { type: 'text/markdown' })
                      const url = URL.createObjectURL(blob)
                      const link = document.createElement('a')
                      link.href = url
                      link.download = `nabh-pack-${periodStart}-to-${periodEnd}.md`
                      link.click()
                      URL.revokeObjectURL(url)
                      reload()
                    } catch (err) { handle(err) } finally { setBusy(false) }
                  }} />
      ) : null}

      {selected ? (
        <IncidentDrawer incident={selected} busy={busy}
                        onClose={() => setSelected(null)}
                        onStart={(note) => act(
                          () => startInvestigation(selected.id, note),
                          'Investigation started.')}
                        onInvestigate={(summary, rootCause) => act(
                          () => recordInvestigation(selected.id,
                                                    { summary, root_cause: rootCause }),
                          'Investigation recorded.')}
                        onAddAction={(description) => act(
                          () => addAction(selected.id, { description }),
                          'Corrective action added.')}
                        onCompleteAction={(actionId) => act(
                          () => completeAction(selected.id, actionId),
                          'Action completed.')}
                        onCloseIncident={(note, noActionReason) => act(
                          () => closeIncident(selected.id, {
                            closure_note: note, no_action_reason: noActionReason,
                          }), 'Incident closed.')}
                        onVoid={(reason) => act(
                          () => voidIncident(selected.id, reason), 'Report voided.')} />
      ) : null}
    </div>
  )
}

/* ── Indicators ──────────────────────────────────────────────────────────── */

function IndicatorsView({ dashboard, denied }: {
  dashboard: IndicatorDashboard | null; denied: boolean
}) {
  if (denied) {
    return <Empty title="Not available to this account"
                  detail="Reading the quality indicators needs the quality-desk role." />
  }
  if (!dashboard) return <Empty title="Loading" detail="Computing indicators for this period." />

  const { summary } = dashboard
  return (
    <>
      <div className="nabh-summary">
        <span className="nabh-summary__item">{summary.reported} reported</span>
        {/* Colour only a count that is something to look at: an amber "0"
            reads as a warning about nothing. */}
        <span className={`nabh-summary__item${summary.insufficient_data > 0 ? ' nabh-summary__item--warn' : ''}`}>
          {summary.insufficient_data} insufficient data
        </span>
        <span className="nabh-summary__item nabh-summary__item--muted">
          {summary.not_collected} not collected
        </span>
        {summary.not_computable ? (
          <span className="nabh-summary__item nabh-summary__item--muted">
            {summary.not_computable} not computable
          </span>
        ) : null}
      </div>

      <p className="nabh-attestation">{dashboard.attestation}</p>

      <div className="nabh-grid">
        {dashboard.indicators.map((indicator) => (
          <IndicatorCard key={indicator.id} indicator={indicator} />
        ))}
      </div>

      <section className="nabh-section">
        <h2 className="nabh-section__title">Denominators</h2>
        <div className="nabh-denominators">
          <div>
            <div className="nabh-denominators__value">
              {dashboard.denominators.inpatient_days.inpatient_days}
            </div>
            <div className="nabh-denominators__caption">inpatient-days</div>
            <p className="nabh-denominators__method">
              {dashboard.denominators.inpatient_days.method}
            </p>
          </div>
          <div>
            <div className="nabh-denominators__value">
              {dashboard.denominators.dispositions.recorded}
            </div>
            <div className="nabh-denominators__caption">
              discharge dispositions recorded, of{' '}
              {dashboard.denominators.dispositions.discharge_events_observed}{' '}
              discharge events observed
            </div>
            <p className="nabh-denominators__method">
              Mortality and LAMA refuse to print below{' '}
              {Math.round(dashboard.denominators.dispositions.coverage_floor * 100)}%
              {' '}coverage.
            </p>
          </div>
        </div>
      </section>

      <section className="nabh-section">
        <h2 className="nabh-section__title">What Orb does not collect</h2>
        <p className="nabh-section__lead">
          Indicators an assessor may ask for that this system cannot produce, and
          what each would need. Being able to say this first is worth more than a
          number nobody can defend.
        </p>
        <ul className="nabh-gaps">
          {dashboard.not_collected_catalogue.map((gap) => (
            <li key={gap.code} className="nabh-gap">
              <span className="nabh-gap__code">{gap.code}</span>
              <span className="nabh-gap__label">{gap.label}</span>
              <p className="nabh-gap__reason">{gap.reason}</p>
              <p className="nabh-gap__needed">Needed: {gap.needed}</p>
            </li>
          ))}
        </ul>
      </section>
    </>
  )
}

/* ── Register ────────────────────────────────────────────────────────────── */

function RegisterView({ incidents, denied, onOpen }: {
  incidents: Incident[]; denied: boolean; onOpen: (id: number) => void
}) {
  if (denied) {
    return <Empty title="Not available to this account"
                  detail="Reading the register needs the quality-desk role. You can still file a report." />
  }
  if (!incidents.length) {
    return <Empty
      title="No incidents in this period"
      detail="That is not the same as a period with nothing to report. An empty register is a finding about the register. Indicators computed from it read NOT COLLECTED rather than zero." />
  }
  return (
    <div className="nabh-table-wrap">
      <table className="nabh-table">
        <thead>
          <tr>
            <th>Reference</th><th>Occurred</th><th>Type</th><th>Severity</th>
            <th>Ward</th><th>Reported by</th><th>Status</th><th>Open actions</th>
          </tr>
        </thead>
        <tbody>
          {incidents.map((incident) => (
            <tr key={incident.id} className="nabh-tr"
                onClick={() => onOpen(incident.id)}>
              <td><button type="button" className="nabh-link">{incident.reference}</button></td>
              <td>{formatClinicalDate(incident.occurred_at)}</td>
              <td>{words(incident.incident_type)}</td>
              <td>
                <Badge text={words(incident.severity)}
                       tone={SEVERITY_TONE[incident.severity] ?? 'neutral'} />
              </td>
              <td>{incident.ward ?? 'Not stated'}</td>
              <td>
                {incident.is_anonymous
                  ? <span className="nabh-anon">Anonymous</span>
                  : (reporterName(incident) ?? 'Not recorded')}
              </td>
              <td>
                <Badge text={STATUS_LABEL[incident.status] ?? incident.status}
                       tone={STATUS_TONE[incident.status] ?? 'neutral'} />
              </td>
              <td>{incident.open_action_count ?? 0}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ── Ward select ─────────────────────────────────────────────────────────── */

/** The hospital's wards from /meta. A current value the list does not hold (a
 *  report form opened with a ward chosen elsewhere, or a server too old to send
 *  the list) is kept as an option rather than silently dropped. */
function WardSelect({ wards, value, onChange, emptyLabel }: {
  wards: string[] | undefined
  value: string
  onChange: (ward: string) => void
  emptyLabel: string
}) {
  const options = wards ?? []
  const extra = value && !options.includes(value) ? [value] : []
  return (
    <select className="nabh-input" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{emptyLabel}</option>
      {[...extra, ...options].map((w) => <option key={w} value={w}>{w}</option>)}
    </select>
  )
}

/* ── Report form ─────────────────────────────────────────────────────────── */

function ReportView({ meta, anonymityStatement, defaultWard, onDone, onError }: {
  meta: NabhMeta | null
  anonymityStatement: string
  defaultWard: string
  onDone: (reference: string) => void
  onError: (err: unknown) => void
}) {
  const [incidentType, setIncidentType] = useState('medication_error')
  const [severity, setSeverity] = useState('near_miss')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [ward, setWard] = useState(defaultWard)
  const [location, setLocation] = useState('')
  // Local wall-clock time from a datetime-local input. Blank means "now": the
  // form sent no time at all, so every incident was dated when it was reported,
  // and a fall at 02:00 filed at 09:00 sat in the register at 09:00.
  const [occurredAt, setOccurredAt] = useState('')
  const [anonymous, setAnonymous] = useState(false)
  const [sending, setSending] = useState(false)

  const types = meta?.incident_types ?? ['medication_error', 'patient_fall', 'other']
  const severities = meta?.severities
    ?? ['near_miss', 'no_harm', 'minor', 'moderate', 'major', 'sentinel']

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setSending(true)
    try {
      // The browser reads a datetime-local value as LOCAL time; sent as UTC
      // ISO so the server (which reads a zone-less time as UTC) stores the
      // moment the reporter meant.
      const occurred = occurredAt ? new Date(occurredAt) : null
      const result = await reportIncident({
        incident_type: incidentType, severity, title, description,
        ward: ward || undefined, location: location || undefined,
        occurred_at: occurred && !Number.isNaN(occurred.getTime()) ? occurred.toISOString() : undefined,
        is_anonymous: anonymous,
      })
      setTitle(''); setDescription(''); setLocation(''); setOccurredAt('')
      onDone(result.incident.reference)
    } catch (err) { onError(err) } finally { setSending(false) }
  }

  return (
    <form className="nabh-form" onSubmit={submit}>
      <p className="nabh-form__lead">
        Near misses are wanted here. An event that reached nobody is the only
        free lesson a hospital gets, and the register is read to change a
        system, not to find a person.
      </p>

      <div className="nabh-form__row">
        <Field label="What kind of event">
          <select className="nabh-input" value={incidentType}
                  onChange={(e) => setIncidentType(e.target.value)}>
            {types.map((t) => <option key={t} value={t}>{words(t)}</option>)}
          </select>
        </Field>
        <Field label="Harm" hint="Near miss means it was caught before it reached the patient.">
          <select className="nabh-input" value={severity}
                  onChange={(e) => setSeverity(e.target.value)}>
            {severities.map((s) => <option key={s} value={s}>{words(s)}</option>)}
          </select>
        </Field>
      </div>

      <Field label="One line">
        <input className="nabh-input" required maxLength={200} value={title}
               onChange={(e) => setTitle(e.target.value)}
               placeholder="Two strengths of the same drug side by side" />
      </Field>

      <Field label="What happened"
             hint="Free text is NOT redacted when a pack is exported. Describe the event, not the person.">
        <textarea className="nabh-input nabh-textarea" required rows={5}
                  maxLength={8000} value={description}
                  onChange={(e) => setDescription(e.target.value)} />
      </Field>

      <div className="nabh-form__row">
        <Field label="Ward">
          <WardSelect wards={meta?.wards} value={ward} onChange={setWard}
                      emptyLabel="Not stated" />
        </Field>
        <Field label="Where exactly">
          <input className="nabh-input" value={location} placeholder="Bay 3, drug trolley"
                 onChange={(e) => setLocation(e.target.value)} />
        </Field>
      </div>

      <Field label="When it happened" hint="Leave blank if it happened just now.">
        <input className="nabh-input" type="datetime-local" value={occurredAt}
               max={nowLocalInput()}
               onChange={(e) => setOccurredAt(e.target.value)} />
      </Field>

      <div className="nabh-anon-box">
        <label className="nabh-checkbox">
          <input type="checkbox" checked={anonymous}
                 onChange={(e) => setAnonymous(e.target.checked)} />
          <span>Report this anonymously</span>
        </label>
        <p className="nabh-anon-box__statement">{anonymityStatement}</p>
      </div>

      <button type="submit" className="nabh-button nabh-button--primary" disabled={sending}>
        {sending ? 'Sending…' : 'Submit report'}
      </button>
    </form>
  )
}

/* ── Evidence pack ───────────────────────────────────────────────────────── */

function PackView({ exports, pack, denied, periodStart, periodEnd, ward, busy,
                    facilityLabel = null, needsFacility = false,
                    onGenerate, onDownloadMarkdown }: {
  exports: ExportRow[]; pack: EvidencePack | null; denied: boolean
  periodStart: string; periodEnd: string; ward: string; busy: boolean
  /** The facility a group-wide reader chose (multisite-12), for the heading. */
  facilityLabel?: string | null
  /** A group-wide reader has to choose the facility a pack is for. */
  needsFacility?: boolean
  onGenerate: (includeIdentifiers: boolean) => void
  onDownloadMarkdown: (includeIdentifiers: boolean) => void
}) {
  const [includeIdentifiers, setIncludeIdentifiers] = useState(false)

  if (denied) {
    return <Empty title="Not available to this account"
                  detail="Producing an evidence pack needs the quality-desk role." />
  }

  return (
    <>
      <section className="nabh-section">
        <h2 className="nabh-section__title">
          Produce the pack for {periodStart} to {periodEnd}
          {ward ? ` (${ward})` : ''}
          {facilityLabel ? ` at ${facilityLabel}` : ''}
        </h2>
        {needsFacility ? (
          <p className="nabh-section__lead">
            Choose the facility the pack is for. NABH accredits each hospital of
            the group on its own, so a pack covers one facility.
          </p>
        ) : null}
        <label className="nabh-checkbox">
          <input type="checkbox" checked={includeIdentifiers}
                 onChange={(e) => setIncludeIdentifiers(e.target.checked)} />
          <span>Include patient identifiers</span>
        </label>
        <p className="nabh-section__lead">
          Off by default. With it off, patient ids are stripped from the register
          rows, but free-text descriptions are not redacted and may still name a
          person. Read the pack before it leaves the hospital.
        </p>
        <div className="nabh-actions">
          <button type="button" className="nabh-button nabh-button--primary"
                  disabled={busy || needsFacility} onClick={() => onGenerate(includeIdentifiers)}>
            Generate pack
          </button>
          <button type="button" className="nabh-button"
                  disabled={busy || needsFacility}
                  onClick={() => onDownloadMarkdown(includeIdentifiers)}>
            Download as a document
          </button>
        </div>
      </section>

      {pack ? (
        <section className="nabh-section">
          <h2 className="nabh-section__title">Pack {pack.reference}</h2>
          <p className="nabh-section__lead">
            {pack.indicator_summary.reported} reported ·{' '}
            {pack.indicator_summary.insufficient_data} insufficient data ·{' '}
            {pack.indicator_summary.not_collected} not collected ·{' '}
            {pack.register.count} {pack.register.count === 1 ? 'incident' : 'incidents'}, {pack.register.anonymous_reports}{' '}
            anonymous
          </p>
          <p className="nabh-digest">SHA-256 {pack.content_sha256}</p>
          <h3 className="nabh-section__subtitle">Limitations this pack states</h3>
          <ul className="nabh-limitations">
            {pack.limitations.map((note) => <li key={note}>{note}</li>)}
          </ul>
        </section>
      ) : null}

      <section className="nabh-section">
        <h2 className="nabh-section__title">Packs produced</h2>
        {exports.length === 0 ? (
          <Empty title="No pack has been produced yet"
                 detail="Every pack is logged here with its digest, so a hospital can say which document it handed over." />
        ) : (
          <div className="nabh-table-wrap">
            <table className="nabh-table">
              <thead>
                <tr>
                  <th>Reference</th><th>Period</th><th>Generated</th>
                  <th>Reported</th><th>Withheld</th><th>Digest</th>
                </tr>
              </thead>
              <tbody>
                {exports.map((row) => (
                  <tr key={row.id}>
                    <td>{row.reference}</td>
                    <td>{row.period_start.slice(0, 10)} → {row.period_end.slice(0, 10)}</td>
                    <td>{formatClinicalDateTime(row.generated_at)}</td>
                    <td>{row.reported_count}</td>
                    <td>{row.insufficient_count + row.not_collected_count}</td>
                    <td className="nabh-digest">{row.content_sha256.slice(0, 16)}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}

/* ── Incident drawer ─────────────────────────────────────────────────────── */

function IncidentDrawer({ incident, busy, onClose, onStart, onInvestigate,
                          onAddAction, onCompleteAction, onCloseIncident, onVoid }: {
  incident: Incident
  busy: boolean
  onClose: () => void
  onStart: (note?: string) => void
  onInvestigate: (summary: string, rootCause?: string) => void
  onAddAction: (description: string) => void
  onCompleteAction: (actionId: number) => void
  onCloseIncident: (note?: string, noActionReason?: string) => void
  onVoid: (reason: string) => void
}) {
  const [summary, setSummary] = useState('')
  const [rootCause, setRootCause] = useState('')
  const [actionText, setActionText] = useState('')
  const [closureNote, setClosureNote] = useState('')
  const [noActionReason, setNoActionReason] = useState('')
  const [voidReason, setVoidReason] = useState('')

  const canStart = incident.allowed_transitions.includes('under_investigation')
  const canInvestigate = incident.allowed_transitions.includes('actions_pending')
  const canClose = incident.allowed_transitions.includes('closed')
  const canVoid = incident.allowed_transitions.includes('void')
  const openActions = (incident.actions ?? []).filter((a) => a.status === 'open')
  const drawerRef = useFocusTrap<HTMLDivElement>(onClose)

  // Portalled to document.body so the drawer is never clipped by a scrolling
  // ancestor and is scoped by its own root class, not by nesting.
  return createPortal(
    <div className="nabh-drawer-root" role="dialog" aria-modal="true"
         aria-label={`Incident ${incident.reference}`}>
      <button type="button" className="nabh-drawer__scrim" aria-label="Close"
              onClick={onClose} />
      <div className="nabh-drawer" ref={drawerRef}>
        <header className="nabh-drawer__head">
          <div>
            <span className="nabh-card__code">{incident.reference}</span>
            <h2 className="nabh-drawer__title">{incident.title}</h2>
          </div>
          <button type="button" className="nabh-button" onClick={onClose}>Close</button>
        </header>

        <div className="nabh-drawer__meta">
          <Badge text={STATUS_LABEL[incident.status] ?? incident.status}
                 tone={STATUS_TONE[incident.status] ?? 'neutral'} />
          <Badge text={words(incident.severity)}
                 tone={SEVERITY_TONE[incident.severity] ?? 'neutral'} />
          <span>{words(incident.incident_type)}</span>
          <span>{incident.ward ?? 'Ward not stated'}</span>
          <span>
            {incident.is_anonymous
              ? 'Reported anonymously: the register holds no reporter identity'
              : `Reported by ${reporterName(incident) ?? 'unknown'}`}
          </span>
        </div>

        <p className="nabh-drawer__description">{incident.description}</p>

        {incident.investigation_summary ? (
          <section className="nabh-drawer__section">
            <h3 className="nabh-section__subtitle">Investigation</h3>
            <p>{incident.investigation_summary}</p>
            {incident.root_cause ? <p>Root cause: {incident.root_cause}</p> : null}
          </section>
        ) : null}

        {(incident.actions ?? []).length ? (
          <section className="nabh-drawer__section">
            <h3 className="nabh-section__subtitle">Corrective actions</h3>
            <ul className="nabh-actions-list">
              {(incident.actions ?? []).map((action) => (
                <li key={action.id} className="nabh-action">
                  <span className={`nabh-action__status nabh-action__status--${action.status}`}>
                    {words(action.status)}
                  </span>
                  <span className="nabh-action__text">{action.description}</span>
                  {action.status === 'open' ? (
                    <button type="button" className="nabh-button" disabled={busy}
                            onClick={() => onCompleteAction(action.id)}>
                      Mark done
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {canStart ? (
          <section className="nabh-drawer__section">
            <button type="button" className="nabh-button nabh-button--primary"
                    disabled={busy} onClick={() => onStart()}>
              {incident.status === 'closed' ? 'Reopen' : 'Start investigation'}
            </button>
          </section>
        ) : null}

        {canInvestigate ? (
          <section className="nabh-drawer__section">
            <h3 className="nabh-section__subtitle">Record the investigation</h3>
            <Field label="What was found">
              <textarea className="nabh-input nabh-textarea" rows={4} value={summary}
                        onChange={(e) => setSummary(e.target.value)} />
            </Field>
            <Field label="Root cause (optional)"
                   hint="Optional on purpose. A mandatory root-cause box produces the words 'human error'.">
              <input className="nabh-input" value={rootCause}
                     onChange={(e) => setRootCause(e.target.value)} />
            </Field>
            <button type="button" className="nabh-button nabh-button--primary"
                    disabled={busy || !summary.trim()}
                    onClick={() => onInvestigate(summary, rootCause || undefined)}>
              Record investigation
            </button>
          </section>
        ) : null}

        {incident.status === 'actions_pending' ? (
          <section className="nabh-drawer__section">
            <h3 className="nabh-section__subtitle">Add a corrective action</h3>
            <Field label="What will change">
              <input className="nabh-input" value={actionText}
                     onChange={(e) => setActionText(e.target.value)} />
            </Field>
            <button type="button" className="nabh-button" disabled={busy || !actionText.trim()}
                    onClick={() => { onAddAction(actionText); setActionText('') }}>
              Add action
            </button>
          </section>
        ) : null}

        {canClose ? (
          <section className="nabh-drawer__section">
            <h3 className="nabh-section__subtitle">Close</h3>
            {openActions.length ? (
              <p className="nabh-warn-text">
                {openActions.length} {openActions.length === 1 ? 'action' : 'actions'} still open. An incident closed with
                an outstanding corrective action is the commonest finding on a
                paper register, so this will be refused.
              </p>
            ) : null}
            <Field label="Closure note">
              <input className="nabh-input" value={closureNote}
                     onChange={(e) => setClosureNote(e.target.value)} />
            </Field>
            <Field label="If nothing needed to change, say why"
                   hint="Required when the incident is closed with no corrective action.">
              <input className="nabh-input" value={noActionReason}
                     onChange={(e) => setNoActionReason(e.target.value)} />
            </Field>
            <button type="button" className="nabh-button nabh-button--primary" disabled={busy}
                    onClick={() => onCloseIncident(closureNote || undefined,
                                                   noActionReason || undefined)}>
              Close incident
            </button>
          </section>
        ) : null}

        {canVoid ? (
          <section className="nabh-drawer__section">
            <h3 className="nabh-section__subtitle">Void this report</h3>
            <p className="nabh-section__lead">
              For a duplicate, or something that turned out not to have happened.
              The row stays in the register and is excluded from the indicators.
              Nothing here is ever deleted.
            </p>
            <Field label="Reason">
              <input className="nabh-input" value={voidReason}
                     onChange={(e) => setVoidReason(e.target.value)} />
            </Field>
            <button type="button" className="nabh-button nabh-button--danger"
                    disabled={busy || !voidReason.trim()}
                    onClick={() => onVoid(voidReason)}>
              Void report
            </button>
          </section>
        ) : null}

        {(incident.events ?? []).length ? (
          <section className="nabh-drawer__section">
            <h3 className="nabh-section__subtitle">History</h3>
            <ol className="nabh-timeline">
              {(incident.events ?? []).map((event) => (
                <li key={event.id}>
                  <span className="nabh-timeline__when">
                    {formatClinicalDateTime(event.occurred_at)}
                  </span>
                  <span className="nabh-timeline__what">{words(event.event_type)}</span>
                  <span className="nabh-timeline__who">
                    {event.actor_user_id ?? 'no actor recorded'}
                  </span>
                </li>
              ))}
            </ol>
          </section>
        ) : null}
      </div>
    </div>,
    document.body,
  )
}
