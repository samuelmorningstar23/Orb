import type {
  SageAssessmentTier1,
  SageAssessmentTier1Parameter,
} from '../../api/sageAssessment'
import { formatUTCTime } from '../../lib/datetime'

// The views News2Drivers renders, built from the server's Tier 1 block or the
// chart's live breakdown. Kept out of News2Drivers.tsx so that file exports
// only its component (react-refresh/only-export-components).

/** One scored (or steady) parameter, already written out. */
export interface News2DriverRow {
  key: string
  points: number
  /** A complete sentence about this parameter. Rendered verbatim. */
  text: string
}

/** Movement of the aggregate across the window this view covers.
 *  `unknown` is a first-class value: one observation is not a trend, and it is
 *  not "stable" either. */
export type News2Trend = 'worsening' | 'improving' | 'unchanged' | 'unknown'

export interface News2DriverView {
  /** Where the numbers came from. Rendered as provenance, and used by tests to
   *  prove which source won when both were available. */
  source: 'tier1' | 'chart'
  /** WHICH MOMENT this decomposition describes. Never omitted — see the header. */
  basis: string
  news2: number | null
  band?: string | null
  /** The server's one-sentence summary. Tier 1 only; rendered verbatim. */
  headline?: string | null
  trend?: News2Trend | null
  /** Parameters scoring above zero, worst first. */
  rows: News2DriverRow[]
  /** Measured and scoring zero. Behind a disclosure — "steady" must not be able
   *  to read as "driving the score". */
  steady: News2DriverRow[]
  singleParam3: boolean
  /** The server's INCOMPLETE-observation sentence, verbatim. */
  coverageNote?: string | null
  /** Set when the source could not produce a decomposition at all. The server's
   *  wording, verbatim; never composed here. */
  unavailableMessage?: string | null
}

/** The server's Tier 1 block, as a view. NOTHING is recomputed — every sentence
 *  on screen is one the server already wrote. */
export function tier1DriverView(tier1: SageAssessmentTier1, drafted?: string | null): News2DriverView {
  const row = (p: SageAssessmentTier1Parameter): News2DriverRow => ({
    key: p.parameter,
    points: p.points ?? 0,
    // VERBATIM. This is the sentence the ward was missing.
    text: p.movement,
  })
  const used = tier1.observations_used
  // ONE observation is not "the last 6": the plural is driven by the count the
  // server actually scored, never assumed from the configured window.
  const observations = `The ${used && used > 1 ? `${used} observations` : 'observation'}`
  // formatUTCTime returns '' for a missing or unparseable stamp, so the timed
  // sentence is only built when there really is a time to put in it.
  const at = formatUTCTime(tier1.window_to || tier1.observed_at || drafted || '')
  const basis = at
    ? `${observations} charted up to ${at}, when this alert fired.`
    : `${observations} this alert fired on.`

  if (!tier1.available) {
    return {
      source: 'tier1',
      basis,
      news2: null,
      rows: [],
      steady: [],
      singleParam3: false,
      unavailableMessage: tier1.message || null,
    }
  }

  const trend: News2Trend =
    tier1.trend === 'worsening' ? 'worsening'
      : tier1.trend === 'improving' ? 'improving'
        : tier1.trend === 'unchanged' ? 'unchanged'
          : 'unknown'

  return {
    source: 'tier1',
    basis,
    news2: typeof tier1.news2 === 'number' ? tier1.news2 : null,
    band: tier1.news2_band ?? null,
    headline: tier1.headline ?? null,
    trend,
    rows: (tier1.parameters || []).map(row),
    steady: (tier1.non_scoring || []).map(row),
    singleParam3: Boolean(tier1.single_param_3),
    coverageNote: tier1.coverage_note ?? null,
  }
}

/** The chart endpoint's live breakdown, as a view.
 *
 *  The FALLBACK, used only when no Tier 1 block exists for this patient (no
 *  critical alert, or the assessment feature switched off). It is deliberately
 *  poorer: it has no window behind it, so it can state a value and its points
 *  and nothing about movement. It must never grow a trend — inventing one here
 *  would be a second author for a sentence Tier 1 already writes properly.
 */
export function chartDriverView(
  breakdown: { parameter: string; value: unknown; points: number; note?: string | null }[],
  rawNews2: number | null,
  singleParam3: boolean,
): News2DriverView {
  const row = (b: { parameter: string; value: unknown; points: number; note?: string | null }): News2DriverRow => ({
    key: b.parameter,
    points: b.points ?? 0,
    // The engine's own note ("Hypotension (BP 65)") when it wrote one, else the
    // parameter and its value. No units are invented — the engine supplies them
    // inside the note, and a bare number is better than a wrong unit.
    text: b.note || `${b.parameter} ${typeof b.value === 'number' ? b.value : String(b.value ?? '—')}`,
  })
  return {
    source: 'chart',
    basis: 'The latest observation set on this chart.',
    news2: typeof rawNews2 === 'number' ? rawNews2 : null,
    rows: breakdown.filter(b => (b.points ?? 0) > 0).map(row),
    steady: breakdown.filter(b => (b.points ?? 0) === 0).map(row),
    singleParam3: Boolean(singleParam3),
  }
}
