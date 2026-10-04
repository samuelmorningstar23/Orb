import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  getScorecard,
  scorecardExportUrl,
  type MetricState,
  type Period,
  type Scorecard,
  type ScorecardMetric,
} from '../../../api/scorecard'
import { AdminErrorState, AdminLoadingState } from './AdminEmptyState'
import { formatUTCDate, formatUTCDateTime } from '../../../lib/datetime'
import { humaniseEnum } from '../../../lib/humanise'
import './Scorecard.css'

/**
 * Orb — the shadow-mode composite scorecard.
 *
 * This is the pilot's only evidence-producing surface: for the whole silent
 * phase, Orb ingests two wards' data, runs the full Vigil → Sage → Relay loop,
 * and accumulates this scorecard against what clinicians actually did. The same
 * page is the clinical validation, the CIO's evidence pack, and the investor
 * asset — which is exactly why it is built to be *hard to over-read*.
 *
 * Three rules from docs/SHADOW_MODE.md §5.5 are enforced structurally, not by
 * convention:
 *
 *   1. Every number carries its denominator and its date range. `<Figure>` takes
 *      both as required props, so a bare percentage cannot be written.
 *   2. Insufficient data reads as insufficient data. A metric below its n floor
 *      renders the count and the floor, never a number — the API client
 *      downgrades the state and this component only prints a figure for
 *      `reported`.
 *   3. The export states what has been removed. The de-identification manifest
 *      is always visible, and an export that declares no manifest is not
 *      offered at all.
 *
 * The provenance strip above the metrics (source table, append-only guarantee,
 * event and episode counts, the instrument's own lag) is not decoration: it is
 * the difference between a dashboard and an artefact someone can be asked to
 * defend.
 */

/* ── Window options ──────────────────────────────────────────────────────── */

const WINDOWS: { days: number; label: string }[] = [
  { days: 7, label: '7 days' },
  { days: 14, label: '14 days' },
  { days: 30, label: '30 days' },
  { days: 90, label: '90 days' },
]

/** Event kinds that record something NOT happening. These are the rows that
 *  make the scorecard honest and the easiest ones to lose by accident, so the
 *  ledger counts them separately and says so. */
const NEGATIVE_KINDS = new Set([
  'alert.suppressed',
  'alert.dropped',
  'alert.expired',
  'sage.refused',
  'sage.failed',
  'action.declined',
  'action.abandoned',
  'outcome.none_confirmed',
])

/* ── Formatting ──────────────────────────────────────────────────────────── */

// Indian grouping (1,23,456): the way the hospital reading this writes numbers.
const nf = new Intl.NumberFormat('en-IN')
const count = (n: number) => nf.format(n)

/* "alert.fired" reads as "Alert fired": the ledger is read by a champion, not
   by the event log's author. */
const kindLabel = (kind: string) => humaniseEnum(kind.replace(/\./g, ' '))

const DATE_OPTS: Intl.DateTimeFormatOptions = { day: 'numeric', month: 'short', year: 'numeric' }

/** "14 Jul 2026 – 31 Jul 2026". Rendered next to every figure, per rule 1. */
function formatRange(p: Period): string {
  const start = formatUTCDate(p.start, 'en-GB', DATE_OPTS)
  const end = formatUTCDate(p.end, 'en-GB', DATE_OPTS)
  if (!start || !end) return `${p.days} days`
  return `${start} – ${end}`
}

function formatLag(ms: number | null): string {
  if (ms === null) return 'not reported'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

const STATE_LABEL: Record<MetricState, string> = {
  reported: 'Reported',
  insufficient_data: 'Insufficient data',
  unadjudicated: 'Not yet adjudicated',
  not_computable: 'Not computable',
}

/* ── Figure ──────────────────────────────────────────────────────────────── */

/**
 * The only component in this view allowed to print a metric's value.
 *
 * The props are a discriminated union on purpose: the `reported` arm demands a
 * non-null denominator, a denominator label and a period, so the type checker —
 * not a reviewer — is what stops a percentage from being rendered naked. Every
 * other state has no `display` field at all, so there is nothing to print.
 */
type FigureProps =
  | {
      state: 'reported'
      display: string
      unit: string | null
      numerator: number | null
      denominator: number
      denominatorLabel: string
      period: Period
      wards: string[]
    }
  | {
      state: Exclude<MetricState, 'reported'>
      nObserved: number
      nRequired: number
      denominatorLabel: string
      period: Period
      wards: string[]
      blockers: string[]
    }

function Figure(props: FigureProps) {
  const scope = props.wards.length
    ? `${props.wards.join(' + ')} · ${formatRange(props.period)}`
    : formatRange(props.period)

  if (props.state === 'reported') {
    const { numerator, denominator, denominatorLabel, unit, display } = props
    return (
      <div className="sc-figure">
        <div className="sc-figure__value tabular-nums">
          {display}
          {unit && <span className="sc-figure__unit">{unit}</span>}
        </div>
        <p className="sc-figure__denominator tabular-nums">
          {numerator !== null ? `${count(numerator)} of ${count(denominator)} ` : `${count(denominator)} `}
          {denominatorLabel}
        </p>
        <p className="sc-figure__scope">{scope}</p>
      </div>
    )
  }

  const { state, nObserved, nRequired, denominatorLabel, blockers } = props
  return (
    <div className="sc-figure sc-figure--empty">
      <div className={`sc-state sc-state--${state}`}>{STATE_LABEL[state]}</div>
      {state === 'insufficient_data' && (
        <p className="sc-figure__denominator tabular-nums">
          {count(nObserved)} {denominatorLabel} recorded
          {nRequired > 0 && ` · at least ${count(nRequired)} before a figure is reported`}
        </p>
      )}
      {state === 'unadjudicated' && (
        <p className="sc-figure__denominator tabular-nums">
          {count(nObserved)} {denominatorLabel} recorded · none reviewed by a clinician
        </p>
      )}
      {state === 'not_computable' && (
        <ul className="sc-blockers">
          {(blockers.length ? blockers : ['No prerequisite recorded for this metric.']).map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      )}
      <p className="sc-figure__scope">{scope}</p>
    </div>
  )
}

/** Narrow a normalized metric into the Figure union in one typed place. */
function figureProps(m: ScorecardMetric, period: Period, wards: string[]): FigureProps {
  if (m.state === 'reported' && m.denominator !== null && (m.display ?? m.value) !== null) {
    return {
      state: 'reported',
      display: m.display ?? String(m.value),
      unit: m.display ? null : m.unit,
      numerator: m.numerator,
      denominator: m.denominator,
      denominatorLabel: m.denominator_label,
      period,
      wards,
    }
  }
  return {
    state: m.state === 'reported' ? 'insufficient_data' : m.state,
    nObserved: m.n_observed,
    nRequired: m.n_required,
    denominatorLabel: m.denominator_label,
    period,
    wards,
    blockers: m.blockers,
  }
}

/* ── Metric band ─────────────────────────────────────────────────────────── */

function MetricBand({
  metric,
  index,
  period,
  wards,
}: {
  metric: ScorecardMetric
  index: number
  period: Period
  wards: string[]
}) {
  const adj = metric.adjudication
  // Zero of zero reviewed is UNDEFINED, not 0 %. Falling back to 0 here would
  // print "0 of 0 reviewed (0 %)" on an empty instrument — the exact bare-zero
  // this screen exists to prevent, on the metric a CIO reads first. `null` means
  // "no percentage exists", and every consumer below renders absence instead.
  const adjPct = adj && adj.total > 0 ? Math.round((adj.adjudicated / adj.total) * 100) : null

  return (
    <article className="sc-band" aria-labelledby={`sc-metric-${metric.id}`}>
      <div className="sc-band__figure">
        <p className="sc-band__eyebrow">
          Metric {index + 1} of 4
          {metric.is_proxy && <span className="sc-proxy-chip">Proxy</span>}
        </p>
        <Figure {...figureProps(metric, period, wards)} />
        {metric.spread && metric.state === 'reported' && metric.spread.p50 !== null && (
          <p className="sc-band__spread tabular-nums">
            p25 {metric.spread.p25 ?? '—'} · median {metric.spread.p50} · p75 {metric.spread.p75 ?? '—'}
          </p>
        )}
      </div>

      <div className="sc-band__body">
        <div className="sc-band__head">
          <h3 className="sc-band__title" id={`sc-metric-${metric.id}`}>
            {metric.label}
          </h3>
          <p className="sc-band__definition">{metric.definition}</p>
        </div>

        {/* Every denominator this numerator could carry, side by side, so the
            flattering one can never be quoted on its own. */}
        {metric.alternative_denominators.length > 0 && (
          <div className="sc-denoms">
            <p className="sc-subhead">The same count, per other denominators</p>
            <ul className="sc-denoms__list">
              {metric.alternative_denominators.map((d, i) => (
                <li key={i} className={d.is_primary ? 'is-primary' : undefined}>
                  <span className="sc-denoms__label">
                    {d.label}
                    {d.is_primary && <span className="sc-denoms__flag">reported figure</span>}
                  </span>
                  <span className="sc-denoms__value tabular-nums">{d.display ?? 'not reported'}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {adj && (
          <div className="sc-adjudication">
            <p className="sc-subhead">Adjudication</p>
            <div className="sc-meter" role="img"
              aria-label={
                adjPct === null
                  ? 'No episodes to adjudicate, so no proportion reviewed exists'
                  : `${adj.adjudicated} of ${adj.total} reviewed, ${adjPct} percent`
              }>
              <div className="sc-meter__fill" style={{ width: `${adjPct ?? 0}%` }} />
            </div>
            <p className="sc-note tabular-nums">
              {count(adj.adjudicated)} of {count(adj.total)} reviewed
              {adjPct === null ? '' : ` (${adjPct}%)`} ·{' '}
              {adj.adjudicators === 0
                ? 'no named adjudicator'
                : `${count(adj.adjudicators)} named adjudicator${adj.adjudicators === 1 ? '' : 's'}`}
              {adj.blinded ? ' · blinded' : ' · not blinded'}
            </p>
            {adj.sampling && <p className="sc-note">Sampling: {adj.sampling}</p>}
            {adj.total > adj.adjudicated && (
              <p className="sc-note sc-note--warn">
                {count(adj.total - adj.adjudicated)} unadjudicated, reported as unadjudicated, never as correct.
              </p>
            )}
          </div>
        )}

        {metric.disagreements !== null && (
          <p className="sc-note">
            <strong className="tabular-nums">{count(metric.disagreements)}</strong> disagreements, the
            informative rows. They need reading individually, not summarising.
          </p>
        )}

        {metric.assumptions.length > 0 && (
          <div className="sc-assumptions">
            <p className="sc-subhead">Assumptions behind this figure</p>
            <ul>
              {metric.assumptions.map((a, i) => (
                <li key={i}>
                  <span className="sc-assumptions__label">{a.label}</span>
                  <span className="sc-assumptions__value tabular-nums">{a.value}</span>
                  <span className="sc-assumptions__source">{a.source}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="sc-caveat">
          <p className="sc-caveat__label">What this cannot show</p>
          <p className="sc-caveat__text">{metric.caveat}</p>
        </div>
      </div>
    </article>
  )
}

/* ── Provenance strip ────────────────────────────────────────────────────── */

function Provenance({ card }: { card: Scorecard }) {
  const i = card.instrument
  const laneRows = Object.entries(i.lanes).sort((a, b) => b[1] - a[1])

  return (
    <section className="sc-provenance" aria-labelledby="sc-provenance-title">
      <h3 className="sc-subhead" id="sc-provenance-title">
        Where these numbers come from
      </h3>
      <dl className="sc-provenance__grid">
        <div>
          <dt>Source</dt>
          {/* i.source is the database view's name, for machines. */}
          <dd>Orb&apos;s shadow evidence log</dd>
        </div>
        <div>
          <dt>Record</dt>
          <dd>{i.append_only ? 'Append-only, enforced by the database' : 'NOT append-only'}</dd>
        </div>
        <div>
          <dt>Schema</dt>
          <dd className="tabular-nums">{i.schema_v === null ? 'not reported' : `v${i.schema_v}`}</dd>
        </div>
        <div>
          <dt>Events</dt>
          <dd className="tabular-nums">{count(i.events_total)}</dd>
        </div>
        <div>
          <dt>Episodes</dt>
          <dd className="tabular-nums">
            {count(i.episodes_total)}
            {i.episodes_total > 0 && (
              <span className="sc-provenance__sub"> · {count(i.episodes_closed)} closed</span>
            )}
          </dd>
        </div>
        <div>
          <dt>Corrections</dt>
          <dd className="tabular-nums">
            {count(i.corrections)}
            <span className="sc-provenance__sub"> · appended, never edited</span>
          </dd>
        </div>
        <div>
          <dt>First event</dt>
          <dd>{i.first_event_at ? formatUTCDateTime(i.first_event_at) : 'none recorded'}</dd>
        </div>
        <div>
          <dt>Last event</dt>
          <dd>{i.last_event_at ? formatUTCDateTime(i.last_event_at) : 'none recorded'}</dd>
        </div>
        <div>
          <dt>Instrument lag</dt>
          <dd className="tabular-nums">
            p50 {formatLag(i.lag_ms_p50)} · p95 {formatLag(i.lag_ms_p95)}
            <span className="sc-provenance__sub"> · write time after the event itself</span>
          </dd>
        </div>
      </dl>

      {laneRows.length > 0 && (
        <div className="sc-lanes">
          <p className="sc-subhead">Lanes</p>
          <ul className="sc-lanes__list">
            {laneRows.map(([lane, n]) => (
              <li key={lane} className={lane === 'simulator' ? 'is-simulator' : undefined}>
                <span>{humaniseEnum(lane)}</span>
                <span className="tabular-nums">{count(n)}</span>
              </li>
            ))}
          </ul>
          {i.lanes.simulator > 0 && (
            <p className="sc-note sc-note--warn">
              Simulator-lane rows are synthetic. They are the only rows this database permits to be
              deleted, and they are not clinical evidence.
            </p>
          )}
        </div>
      )}
    </section>
  )
}

/* ── Episode ledger ──────────────────────────────────────────────────────── */

function Ledger({ kinds }: { kinds: Record<string, number> }) {
  const entries = Object.entries(kinds)
  const positives = entries.filter(([k]) => !NEGATIVE_KINDS.has(k)).sort((a, b) => b[1] - a[1])
  const negatives = entries.filter(([k]) => NEGATIVE_KINDS.has(k)).sort((a, b) => b[1] - a[1])

  return (
    <section className="sc-ledger" aria-labelledby="sc-ledger-title">
      <h3 className="sc-subhead" id="sc-ledger-title">
        Episode ledger
      </h3>
      <p className="sc-note">
        One row per observation, appended in order. The negatives are recorded as deliberately as
        the positives. A system that logs only its successes reports a spectacular success rate.
      </p>

      {entries.length === 0 ? (
        <p className="sc-empty-line">No events recorded in this window.</p>
      ) : (
        <div className="sc-ledger__cols">
          <div>
            <p className="sc-ledger__group">Recorded</p>
            <ul className="sc-kinds">
              {positives.map(([k, n]) => (
                <li key={k}>
                  <span>{kindLabel(k)}</span>
                  <span className="tabular-nums">{count(n)}</span>
                </li>
              ))}
              {positives.length === 0 && <li className="sc-kinds__none">none</li>}
            </ul>
          </div>
          <div>
            <p className="sc-ledger__group sc-ledger__group--negative">
              Materialised negatives
            </p>
            <ul className="sc-kinds">
              {negatives.map(([k, n]) => (
                <li key={k}>
                  <span>{kindLabel(k)}</span>
                  <span className="tabular-nums">{count(n)}</span>
                </li>
              ))}
              {negatives.length === 0 && <li className="sc-kinds__none">none</li>}
            </ul>
          </div>
        </div>
      )}
    </section>
  )
}

/* ── Export ──────────────────────────────────────────────────────────────── */

function ExportPanel({ card, days, ward }: { card: Scorecard; days: number; ward: string | null }) {
  const e = card.export
  // Rule 3: an export that does not declare what it strips is not offered.
  const offerable = e.available && e.deidentification.length > 0

  return (
    <section className="sc-export" aria-labelledby="sc-export-title">
      <h3 className="sc-subhead" id="sc-export-title">
        Exportable scorecard
      </h3>
      <p className="sc-note">
        The export is the artefact that may leave this room, for the CIO, for counsel, for an
        investor. It is de-identified before it is written. The identified data never leaves the
        appliance.
      </p>

      <div className="sc-deid">
        <p className="sc-subhead">What the export removes</p>
        {e.deidentification.length === 0 ? (
          <p className="sc-empty-line">
            No de-identification manifest has been declared, so no export is offered.
          </p>
        ) : (
          <ul className="sc-deid__list">
            {e.deidentification.map((d, i) => (
              <li key={i}>
                <span className="mono sc-deid__field">{d.field}</span>
                <span className={`sc-deid__treatment sc-deid__treatment--${d.treatment}`}>
                  {d.treatment}
                </span>
                {d.note && <span className="sc-deid__note">{d.note}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>

      {e.note && <p className="sc-note">{e.note}</p>}

      <div className="sc-export__actions">
        {offerable ? (
          (e.formats.length ? e.formats : ['json']).map((fmt) => (
            <a
              key={fmt}
              className="btn-glass sc-export__btn"
              href={scorecardExportUrl({ days, ward }, fmt)}
              download
            >
              Download de-identified {fmt.toUpperCase()}
            </a>
          ))
        ) : (
          <button className="btn-glass sc-export__btn" disabled aria-disabled="true">
            Export unavailable
          </button>
        )}
        {!offerable && (
          <span className="sc-note">
            {e.available
              ? 'The server offered an export without a de-identification manifest; it is withheld until one is declared.'
              : 'No scorecard data has been recorded, so there is nothing to export.'}
          </span>
        )}
      </div>
    </section>
  )
}

/* ── Readiness ───────────────────────────────────────────────────────────── */

function Readiness({ items }: { items: Scorecard['readiness'] }) {
  const met = items.filter((i) => i.met).length
  return (
    <section className="sc-readiness" aria-labelledby="sc-readiness-title">
      <h3 className="sc-subhead" id="sc-readiness-title">
        Prerequisites
        <span className="sc-readiness__count tabular-nums">
          {met} of {items.length} met
        </span>
      </h3>
      <ul className="sc-readiness__list">
        {items.map((i) => (
          <li key={i.id} className={i.met ? 'is-met' : 'is-unmet'}>
            <span className="sc-readiness__mark" aria-hidden="true">
              {i.met ? '✓' : '✗'}
            </span>
            <span className="sc-readiness__label">
              {i.label}
              <span className="sc-sr-only">{i.met ? ', met' : ', not met'}</span>
            </span>
            {i.detail && <span className="sc-readiness__detail">{i.detail}</span>}
          </li>
        ))}
      </ul>
    </section>
  )
}

/* ── View ────────────────────────────────────────────────────────────────── */

export default function ScorecardView() {
  const [days, setDays] = useState(14)
  const [card, setCard] = useState<Scorecard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const ward: string | null = null

  const load = useCallback(
    (windowDays: number) => {
      setLoading(true)
      setError(null)
      getScorecard({ days: windowDays, ward })
        .then(setCard)
        .catch((e) => setError(e?.message || 'Failed to load the scorecard'))
        .finally(() => setLoading(false))
    },
    [ward],
  )

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    load(days)
  }, [days, load])

  const wards = useMemo(() => card?.wards ?? [], [card])

  if (loading && !card) return <AdminLoadingState message="Reading the shadow-event log…" />
  if (error && !card) return <AdminErrorState error={error} onRetry={() => load(days)} />
  if (!card) return null

  return (
    <div className="scorecard">
      {/* ── Header ── */}
      <header className="sc-head">
        <div className="sc-head__text">
          <h2 className="sc-head__title">Evaluation scorecard</h2>
          <p className="sc-head__sub">
            What Orb saw, what it said and what the ward did, one appended row per observation.
            Every figure below carries its denominator and its dates; where there is not enough
            data to report one, it says so instead of showing a zero.
          </p>
        </div>
        <div className="sc-head__controls">
          <div className="sc-window" role="group" aria-label="Observation window">
            {WINDOWS.map((w) => (
              <button
                key={w.days}
                type="button"
                className={`sc-window__btn${w.days === days ? ' is-active' : ''}`}
                aria-pressed={w.days === days}
                onClick={() => setDays(w.days)}
              >
                {w.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="btn-glass sc-refresh"
            onClick={() => load(days)}
            disabled={loading}
          >
            {loading ? 'Reading…' : 'Refresh'}
          </button>
        </div>
      </header>

      {/* ── Scope line: the date range and wards that qualify every number ── */}
      <div className="sc-scope">
        <span className="sc-scope__item">
          <span className="sc-scope__key">Window</span>
          <span className="sc-scope__val tabular-nums">{formatRange(card.period)}</span>
        </span>
        <span className="sc-scope__item">
          <span className="sc-scope__key">Wards</span>
          <span className="sc-scope__val">
            {wards.length ? wards.join(' + ') : 'none under observation'}
          </span>
        </span>
        <span className="sc-scope__item">
          <span className="sc-scope__key">Phase</span>
          <span className="sc-scope__val">{card.phase_label}</span>
        </span>
        <span className="sc-scope__item">
          <span className="sc-scope__key">Computed</span>
          <span className="sc-scope__val">
            {card.generated_at ? formatUTCDateTime(card.generated_at) : 'not computed'}
          </span>
        </span>
      </div>

      {/* ── Absence, stated plainly ── */}
      {card.unavailable && (
        <div className="sc-banner" role="status">
          <p className="sc-banner__title">No shadow data on this appliance</p>
          <p className="sc-banner__text">{card.unavailable.reason}</p>
          <p className="sc-banner__text">
            The four metrics below are shown with their definitions and their limits so the shape of
            the evidence is clear. None of them carries a measurement, because none has been made.
          </p>
        </div>
      )}

      <Provenance card={card} />

      {card.readiness.length > 0 && <Readiness items={card.readiness} />}

      {/* ── The four metrics ── */}
      <section className="sc-metrics" aria-label="Scorecard metrics">
        {card.metrics.map((m, idx) => (
          <MetricBand key={m.id} metric={m} index={idx} period={card.period} wards={wards} />
        ))}
      </section>

      <Ledger kinds={card.instrument.kinds} />

      <ExportPanel card={card} days={days} ward={ward} />

      <p className="sc-footnote">
        This scorecard cannot show that acting on Orb's flags changed an outcome, and it is not a
        controlled comparison. It shows what was recorded, over a stated period, on a stated ward,
        with the denominator visible. Read it that way.
      </p>
    </div>
  )
}
