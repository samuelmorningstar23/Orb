/**
 * A FALLING PATIENT MUST READ AS FALLING.
 *
 * WHAT WAS ON SCREEN (2026-08-14, real browser, running ward): "Trend: →
 * Stable", on every patient across ~20 chart opens — including one whose
 * systolic BP fell 108 → 65 across six observations. The server had already
 * computed the true sentence for that patient ("Systolic BP 65mmHg, down from
 * 108mmHg across the last 6 observations, NEWS2 points 1 → 3"), stored it,
 * composed a deterministic headline over it, and shipped both in the `tier1`
 * payload. `grep -rn "tier1" src/` returned nothing. Nobody read it.
 *
 * These are not rendering tests. Each is a property a nurse's escalation
 * decision rests on:
 *
 *   1. MOVEMENT IS RENDERED, AND IT IS THE SERVER'S WORDS. This component does
 *      no arithmetic. If a future edit starts deriving "down from" here, the
 *      sentence has two authors and the ward will eventually be shown both.
 *   2. "NO BASELINE" IS NEVER PRINTED AS "STABLE". The two are opposite
 *      statements about a patient, and conflating them is the defect above.
 *   3. THE CHART FALLBACK NEVER INVENTS A TREND. It has no window behind it, so
 *      it must say nothing about movement rather than something reassuring.
 *   4. AN UNAVAILABLE TIER 1 SAYS SO, IN THE SERVER'S WORDING, AND IS NOT A
 *      SPINNER. Nothing further is coming for that block.
 */
import { render, screen, fireEvent } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SageAssessmentTier1, SageAssessmentTier1Parameter } from '../../api/sageAssessment'
import News2Drivers, { tier1DriverView, chartDriverView } from './News2Drivers'

/** One parameter, shaped exactly as `_tier1_parameter` emits it. */
function param(over: Partial<SageAssessmentTier1Parameter> = {}): SageAssessmentTier1Parameter {
  return {
    parameter: 'Systolic BP',
    value: 65,
    points: 3,
    note: 'Hypotension (BP 65)',
    display: '65mmHg',
    unit: 'mmHg',
    previous_value: 82,
    previous_points: 2,
    first_value: 108,
    first_points: 1,
    value_delta: -43,
    points_delta: 2,
    points_direction: 'worsening',
    value_direction: 'down',
    movement: 'Systolic BP 65mmHg, down from 108mmHg across the last 6 observations, '
      + 'NEWS2 points 1 → 3',
    ...over,
  }
}

/** THE PATIENT FROM THE DRY RUN: systolic 108 → 65 across six observations. */
function fallingTier1(over: Partial<SageAssessmentTier1> = {}): SageAssessmentTier1 {
  return {
    available: true,
    source: 'orb_deterministic_engine',
    scoring_model: 'NEWS2',
    spo2_scale: 1,
    news2: 12,
    news2_band: 'critical',
    single_param_3: true,
    complete: true,
    missing_parameters: [],
    coverage_note: null,
    observed_at: '2026-08-14T09:32:00Z',
    observations_used: 6,
    window_from: '2026-08-14T04:02:00Z',
    window_to: '2026-08-14T09:32:00Z',
    parameters: [
      param(),
      param({
        parameter: 'Heart rate', value: 128, points: 2, note: 'Tachycardia (HR 128)',
        display: '128', unit: null, previous_value: 118, previous_points: 1,
        first_value: 96, first_points: 0, value_delta: 32, points_delta: 2,
        points_direction: 'worsening', value_direction: 'up',
        movement: 'Heart rate 128, up from 96 across the last 6 observations, NEWS2 points 0 → 2',
      }),
    ],
    non_scoring: [
      param({
        parameter: 'Temperature', value: 37.1, points: 0, note: null, display: '37.1°C',
        unit: '°C', previous_value: 37.0, previous_points: 0, first_value: 37.0,
        first_points: 0, value_delta: 0.1, points_delta: 0,
        points_direction: 'unchanged', value_direction: 'up',
        movement: 'Temperature 37.1°C, up from 37.0°C across the last 6 observations, 0 NEWS2 points',
      }),
    ],
    most_abnormal: param(),
    most_abnormal_tied_with: [],
    trend: 'worsening',
    news2_delta: 9,
    computed_at: '2026-08-14T09:32:01Z',
    headline: 'NEWS2 12 (critical), up 9 across the last 6 observations. Driven by '
      + 'Systolic BP 65mmHg (3); Heart rate 128 (2). Most abnormal: Systolic BP at 3 points.',
    ...over,
  }
}

function renderTier1(tier1: SageAssessmentTier1) {
  return render(<News2Drivers view={tier1DriverView(tier1)} id="t" />)
}

describe('a parameter that is falling', () => {
  it("renders the server's movement sentence verbatim — never 'Stable'", () => {
    const { container } = renderTier1(fallingTier1())

    // The sentence, whole. Not "Systolic BP 65" with the movement dropped, and
    // not a client-composed paraphrase of it.
    expect(screen.getByText(
      'Systolic BP 65mmHg, down from 108mmHg across the last 6 observations, NEWS2 points 1 → 3',
    )).toBeInTheDocument()
    expect(screen.getByText(
      'Heart rate 128, up from 96 across the last 6 observations, NEWS2 points 0 → 2',
    )).toBeInTheDocument()

    // THE WORD THAT WAS ON EVERY CHART. On a patient whose pressure has fallen
    // 43 mmHg it must not appear anywhere in this block, in any casing.
    expect(container.textContent).not.toMatch(/stable/i)
  })

  it('states the aggregate movement as worsening, not as unchanged', () => {
    renderTier1(fallingTier1())
    expect(screen.getByText('↘ Worsening')).toBeInTheDocument()
  })

  it("renders the server's deterministic headline", () => {
    renderTier1(fallingTier1())
    expect(screen.getByText(/NEWS2 12 \(critical\), up 9 across the last 6 observations\./))
      .toBeInTheDocument()
    expect(screen.getByText(/Most abnormal: Systolic BP at 3 points\./)).toBeInTheDocument()
  })

  it('says WHICH observations it is describing — a snapshot must not read as live', () => {
    renderTier1(fallingTier1())
    // Time is rendered in the reader's locale, so the assertion is on the
    // sentence around it, not on a formatted clock value.
    expect(screen.getByText(/The 6 observations charted up to .+, when this alert fired\./))
      .toBeInTheDocument()
  })

  it('carries the points as points, worst first, and flags the single-parameter rule', () => {
    renderTier1(fallingTier1())
    expect(screen.getByText('+3')).toBeInTheDocument()
    expect(screen.getByText('+2')).toBeInTheDocument()
    expect(screen.getByText(/single parameter scored 3/i)).toBeInTheDocument()
  })

  it('keeps the steady parameters out of the drivers until they are asked for', () => {
    renderTier1(fallingTier1())
    const steady = /Temperature 37\.1°C, up from 37\.0°C/
    // "Measured and scoring zero" must not be able to read as "driving the score".
    expect(screen.queryByText(steady)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /scoring 0/i }))
    expect(screen.getByText(steady)).toBeInTheDocument()
  })
})

describe('one observation, or no earlier one', () => {
  it("says there is nothing to compare against — and does NOT say 'Stable'", () => {
    const { container } = renderTier1(fallingTier1({
      trend: 'insufficient_history',
      news2_delta: null,
      observations_used: 1,
      parameters: [param({
        points_direction: 'unknown',
        value_direction: 'unknown',
        movement: 'Systolic BP 65mmHg, the only observation on file, 3 NEWS2 points',
      })],
      non_scoring: [],
      headline: 'NEWS2 12 (critical), no earlier observation in this window to compare against. '
        + 'Driven by Systolic BP 65mmHg (3).',
    }))

    expect(screen.getByText('Not enough earlier observations to compare')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/stable/i)
    expect(container.textContent).not.toMatch(/unchanged/i)
    // The basis line drops to the singular rather than claiming a window.
    expect(screen.getByText(/The observation charted up to .+, when this alert fired\./))
      .toBeInTheDocument()
  })
})

describe('Tier 1 that could not be computed', () => {
  it("renders the server's sentence and nothing that implies more is coming", () => {
    const { container } = renderTier1({
      available: false,
      reason: 'no_observation',
      message: 'No observations are on file for this patient, so Orb cannot show the drivers.',
      observations_used: 0,
    })

    expect(screen.getByText(
      'No observations are on file for this patient, so Orb cannot show the drivers.',
    )).toBeInTheDocument()
    // Not a spinner, not a skeleton, not a promise.
    expect(container.textContent).not.toMatch(/generating/i)
    expect(container.querySelector('.orb-skeleton')).toBeNull()
    expect(container.textContent).not.toMatch(/stable/i)
  })
})

describe('the chart fallback, when no alert draft exists', () => {
  const breakdown = [
    { parameter: 'Systolic BP', value: 65, points: 3, note: 'Hypotension (BP 65)' },
    { parameter: 'Heart rate', value: 128, points: 2, note: 'Tachycardia (HR 128)' },
    { parameter: 'Temperature', value: 37.1, points: 0, note: null },
  ]

  it('shows the live decomposition and INVENTS NO MOVEMENT', () => {
    const { container } = render(
      <News2Drivers view={chartDriverView(breakdown, 5, false)} id="c" />,
    )
    expect(screen.getByText('Why NEWS2 5')).toBeInTheDocument()
    expect(screen.getByText('Hypotension (BP 65)')).toBeInTheDocument()
    // It has no window behind it, so it says nothing about direction at all —
    // in particular it does not fall back to the reassuring word.
    expect(container.textContent).not.toMatch(/stable/i)
    expect(container.textContent).not.toMatch(/worsening|improving|unchanged/i)
    expect(container.querySelector('[class*="news2-drivers__trend"]')).toBeNull()
  })

  it('names the moment it describes, so it cannot be read as the alert', () => {
    render(<News2Drivers view={chartDriverView(breakdown, 5, false)} id="c" />)
    expect(screen.getByText('The latest observation set on this chart.')).toBeInTheDocument()
  })
})

describe('the regulatory posture', () => {
  it('is on every render, in both views', () => {
    const { unmount } = renderTier1(fallingTier1())
    expect(screen.getByText(/not a clinically-validated deterioration probability/i))
      .toBeInTheDocument()
    expect(screen.getByText(/computed by Orb, not written by a model/i)).toBeInTheDocument()
    unmount()

    render(<News2Drivers view={chartDriverView([], 0, false)} id="c" />)
    expect(screen.getByText(/not a clinically-validated deterioration probability/i))
      .toBeInTheDocument()
  })
})
