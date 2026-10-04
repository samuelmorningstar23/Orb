/**
 * WHY THIS NEWS2 — the ONE renderer of a per-parameter score decomposition.
 *
 * ═══ WHY THIS COMPONENT EXISTS AT ALL ═══
 * There were two of these. The Vigil chart had `News2Attribution`, a collapsed
 * table built from the chart endpoint's `risk.breakdown`; the server had been
 * computing a far richer block — Tier 1 — for every critical alert, storing it,
 * and shipping it in the assessment payload where NOTHING read it. On the ward
 * that meant a patient whose systolic BP fell 108 → 65 across six observations
 * had the sentence "Systolic BP 65mmHg, down from 108mmHg across the last 6
 * observations, NEWS2 points 1 → 3" sitting in the database while the chart
 * said "Trend: → Stable".
 *
 * Two independently-computed decompositions of the same score can disagree —
 * the service logs exactly that class of disagreement (tied observation
 * timestamps, id-tiebroken) — and Orb's house rule is that a deterministic
 * value is computed once and pinned. So this is the only component in the app
 * that renders a breakdown, and it renders whatever view it is handed.
 *
 * ═══ IT COMPOSES NO CLINICAL SENTENCES ═══
 * Every row's `text` arrives fully written. On the Tier 1 view that is the
 * server's `movement` sentence, verbatim; the headline and the coverage note
 * are the server's too. This file does arithmetic on NOTHING. If a future edit
 * finds itself deriving "down from" or recomputing a delta here, the sentence
 * has two authors and the ward will eventually be shown both.
 *
 * ═══ WHAT `basis` IS FOR ═══
 * A decomposition is a statement about ONE moment. Tier 1 is a snapshot taken
 * when the alert fired; the chart's breakdown is the patient right now. Read
 * without its timestamp, a 40-minute-old snapshot under a live gauge is a
 * contradiction the reader has to resolve on their own. `basis` is the one line
 * that says which moment this is, and it is required, not optional.
 */
import { useState } from 'react'
import type { News2DriverView, News2Trend } from './news2DriverView'
import './News2Drivers.css'

const TREND_LABEL: Record<News2Trend, string> = {
  // The arrows match the ward's existing convention on the chart (↘ is the
  // patient getting worse, ↗ is the patient getting better). They describe the
  // PATIENT, not the number — NEWS2 rising is ↘.
  worsening: '↘ Worsening',
  improving: '↗ Improving',
  unchanged: '→ Unchanged',
  // NO ARROW AND NOT THE WORD "STABLE". "We have nothing to compare against"
  // and "this patient is steady" are different clinical statements, and the
  // chart used to render the first as the second on every patient it had no
  // baseline for.
  unknown: 'Not enough earlier observations to compare',
}

function pointsClass(points: number): string {
  return points >= 3 ? 'news2-drivers__points--3'
    : points === 2 ? 'news2-drivers__points--2'
      : points === 1 ? 'news2-drivers__points--1'
        : 'news2-drivers__points--0'
}

/**
 * `id` scopes the heading so two of these can never share an `aria-labelledby`
 * — which is the state the ward is in whenever a carried draft is on screen.
 */
export default function News2Drivers({ view, id }: { view: News2DriverView; id: string }) {
  const [steadyOpen, setSteadyOpen] = useState(false)
  const titleId = `news2-drivers-title-${id}`

  return (
    <section className="news2-drivers" aria-labelledby={titleId} data-source={view.source}>
      <div className="news2-drivers__head">
        <h4 className="news2-drivers__title" id={titleId}>
          {typeof view.news2 === 'number' ? `Why NEWS2 ${view.news2}` : 'Why this score'}
        </h4>
        {view.band && <span className="news2-drivers__band">{view.band}</span>}
      </div>
      {/* Which moment this is about. Above the numbers, because it changes what
          every one of them means. */}
      <p className="news2-drivers__basis">{view.basis}</p>

      {view.unavailableMessage ? (
        /* Tier 1 could not be computed. The server's sentence, verbatim — and
           NOT a spinner: nothing further is coming for this block. */
        <p className="news2-drivers__unavailable">{view.unavailableMessage}</p>
      ) : (
        <>
          {view.headline && (
            <p className="news2-drivers__headline">{view.headline}</p>
          )}

          {view.trend && (
            <p className={`news2-drivers__trend news2-drivers__trend--${view.trend}`}>
              {TREND_LABEL[view.trend]}
            </p>
          )}

          {view.rows.length === 0 ? (
            <p className="news2-drivers__none">
              No parameter is scoring. Every measured vital is in the normal range.
            </p>
          ) : (
            <ul className="news2-drivers__rows">
              {view.rows.map(r => (
                <li className="news2-drivers__row" key={r.key}>
                  <span className={`news2-drivers__points ${pointsClass(r.points)} metric-mono`}>
                    +{r.points}
                  </span>
                  <span className="news2-drivers__text">{r.text}</span>
                </li>
              ))}
            </ul>
          )}

          {view.singleParam3 && (
            <p className="news2-drivers__single3">
              A single parameter scored 3: the NEWS2 single-parameter escalation rule applies.
            </p>
          )}

          {view.coverageNote && (
            <p className="news2-drivers__coverage">{view.coverageNote}</p>
          )}

          {view.steady.length > 0 && (
            <div className="news2-drivers__steady">
              <button
                type="button"
                className="news2-drivers__steady-toggle"
                aria-expanded={steadyOpen}
                onClick={() => setSteadyOpen(o => !o)}
              >
                {steadyOpen ? 'Hide' : 'Show'} the {view.steady.length} parameter
                {view.steady.length === 1 ? '' : 's'} scoring 0
              </button>
              {steadyOpen && (
                <ul className="news2-drivers__rows news2-drivers__rows--steady">
                  {view.steady.map(r => (
                    <li className="news2-drivers__row" key={r.key}>
                      <span className={`news2-drivers__points ${pointsClass(r.points)} metric-mono`}>
                        +{r.points}
                      </span>
                      <span className="news2-drivers__text">{r.text}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}

      {/* The regulatory posture, on every render: this is a fixed lookup table,
          not a validated deterioration probability. */}
      <p className="news2-drivers__disclaimer">
        Deterministic RCP NEWS2 scoring table, computed by Orb, not written by a model. A
        decision-support aid, <strong>not a clinically-validated deterioration probability</strong>;
        use clinical judgement.
      </p>
    </section>
  )
}
