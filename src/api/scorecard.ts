/**
 * Orb — Shadow-mode composite scorecard: the client half of the contract.
 * ============================================================================
 *
 * This module is the ONLY place the frontend encodes what the scorecard API
 * looks like. The view (pages/panels/admin/Scorecard.tsx) renders a normalized
 * model produced here and never touches a raw response, so if the server shape
 * moves, exactly one file changes.
 *
 * WHY THIS FILE READS LIKE A SPEC
 * -------------------------------
 * The Week-13 deliverable is not a dashboard, it is *evidence*: a composite
 * scorecard accumulated in shadow mode against what clinicians actually did,
 * shown to a hospital CIO and to investors. Three rules from docs/SHADOW_MODE.md
 * §5.5 are enforced in the interface itself, and they shape this contract:
 *
 *   1. Every number carries its denominator and its date range. So a metric
 *      that can report a value MUST also carry `denominator` +
 *      `denominator_label`, and the scorecard MUST carry `period`. The view's
 *      <Figure> component takes both as required props — a figure without a
 *      denominator is a type error, not a review comment.
 *   2. Insufficient data is displayed as insufficient data, never as 0 %. So a
 *      metric carries a `state`, not just a nullable value: a metric with
 *      n below its floor is `insufficient_data` and the view refuses to print
 *      a number for it even if the server sent one.
 *   3. The export states what has been removed. So the export descriptor
 *      carries an explicit, itemised de-identification manifest, and the view
 *      will not offer an export that does not declare one.
 *
 * ENDPOINTS
 * ---------
 *   GET  /api/admin/shadow/scorecard?days=<n>[&ward=<w>]            → Scorecard
 *   GET  /api/admin/shadow/export/scorecard?days=<n>&format=json|csv
 *
 * These paths were RECONCILED with the server (server/routes/shadow_routes.py),
 * which is canonical. This file previously guessed `/api/admin/scorecard` with
 * `/api/shadow/scorecard` as a 404 fallback; neither was ever served. The
 * shadow surface is seven endpoints, not one — episodes, episode detail,
 * adjudication, outcome, shift confirmation and export are inputs to the
 * scorecard rather than parts of it — so they share one `/api/admin/shadow`
 * prefix, one router and one permission.
 *
 * NO ALIASES EXIST on either side, deliberately. A 404 here is rendered as
 * `emptyScorecard()` — "this appliance has recorded nothing" — so a silent path
 * drift would present a clean, plausible, completely false zero-state. One path
 * per endpoint is the only configuration in which that cannot happen.
 *
 * ABSENCE IS A FIRST-CLASS STATE
 * ------------------------------
 * The instrument (`shadow_events`) does not exist on every appliance yet. A 404
 * from BOTH mounts is not an error to shout about — it is the honest statement
 * "this appliance has recorded nothing", and it is rendered as such via
 * `emptyScorecard()`. That model contains definitions and caveats (which are
 * documentation, quoted from the pilot design) and NO measurements whatsoever.
 * The view must never invent a value; a missing instrument yields empty state,
 * not zeroes.
 */

import { apiFetch } from './client'

/* ── Metric identity ─────────────────────────────────────────────────────── */

export type MetricId = 'detection_lead_time' | 'false_alert_rate' | 'agreement' | 'time_saved'

/** The four scorecard metrics, in the order they are argued.
 *  Order matters: lead time is the only metric that speaks to Orb's claim;
 *  the rest measure whether it is tolerable. */
export const METRIC_ORDER: MetricId[] = [
  'detection_lead_time',
  'false_alert_rate',
  'agreement',
  'time_saved',
]

/**
 * What a metric is allowed to be:
 *
 *  · `reported`          — computable, n at or above its floor. A number prints.
 *  · `insufficient_data` — computable, n below its floor. The n prints, the
 *                          number does not. This is the state that stops
 *                          "0 % false alerts" appearing on morning one.
 *  · `unadjudicated`     — the events exist but the human judgement that turns
 *                          them into a number has not happened. Never rendered
 *                          as correctness.
 *  · `not_computable`    — a prerequisite is structurally missing (e.g. no
 *                          ground-truth outcome capture). The blockers print
 *                          instead of a value.
 */
export type MetricState = 'reported' | 'insufficient_data' | 'unadjudicated' | 'not_computable'

export interface Period {
  /** ISO-8601 UTC. */
  start: string
  end: string
  days: number
}

/** An alternative denominator the same numerator could be divided by. Shown
 *  next to the primary one so the flattering denominator can never be quoted
 *  silently (SHADOW_MODE §5.2: per alert vs per patient-day vs per bed-day). */
export interface AltDenominator {
  label: string
  display: string | null
  is_primary?: boolean
}

export interface Assumption {
  label: string
  value: string
  /** Where the number came from — a measurement, or a stated assumption. */
  source: string
}

export interface Adjudication {
  adjudicated: number
  total: number
  adjudicators: number
  blinded: boolean
  /** Free text is deliberately not accepted here; this is a method note. */
  sampling: string | null
}

export interface ScorecardMetric {
  id: MetricId
  label: string
  /** One sentence: what the number IS, operationally. */
  definition: string
  state: MetricState
  /** Raw value in `unit`. Ignored by the view unless state === 'reported'. */
  value: number | null
  unit: string | null
  /** Server-formatted figure ("3 h 07 m", "1.8"). Preferred over `value` for
   *  display so unit formatting lives next to the computation. */
  display: string | null
  numerator: number | null
  denominator: number | null
  /** What the denominator counts — "occupied bed-days", "adjudicated alerts". */
  denominator_label: string
  alternative_denominators: AltDenominator[]
  /** Observations behind the figure, and the floor below which it is not
   *  reported as a number. */
  n_observed: number
  n_required: number
  /** Spread, when a median is being reported (percentiles are computed in
   *  Python — SQLite has no median). */
  spread: { p25: number | null; p50: number | null; p75: number | null } | null
  /** "What this cannot show." Always rendered, never behind a tooltip. */
  caveat: string
  /** Why it is not computable, when it is not. */
  blockers: string[]
  /** True for metrics that are explicitly a proxy (time saved). */
  is_proxy: boolean
  assumptions: Assumption[]
  adjudication: Adjudication | null
  /** Agreement only: the informative rows. */
  disagreements: number | null
}

/** Health of the instrument itself. This is what makes the scorecard evidence
 *  rather than a report: the append-only guarantee, the volume, and the
 *  instrument's own lag (recorded_at − occurred_at) are all on the page. */
export interface InstrumentStatus {
  source: string
  schema_v: number | null
  append_only: boolean
  events_total: number
  episodes_total: number
  episodes_closed: number
  first_event_at: string | null
  last_event_at: string | null
  lag_ms_p50: number | null
  lag_ms_p95: number | null
  /** Event counts by lane ('simulator' | 'ingestion' | 'chat' | …). */
  lanes: Record<string, number>
  /** Event counts by kind — including the materialised negatives. */
  kinds: Record<string, number>
  /** Superseding rows: corrections are appended, never edited in place. */
  corrections: number
}

export interface ReadinessItem {
  id: string
  label: string
  met: boolean
  detail: string
}

export interface DeidentificationItem {
  field: string
  /** 'removed' | 'hashed' | 'coarsened' | 'dropped' — stated verbatim to the
   *  reader; the server owns the vocabulary. */
  treatment: string
  note: string | null
}

export interface ExportDescriptor {
  available: boolean
  formats: string[]
  deidentification: DeidentificationItem[]
  note: string | null
}

export interface Scorecard {
  generated_at: string | null
  period: Period
  /** Wards under observation. Two, in the pilot. */
  wards: string[]
  /** Shadow-mode phase: silent / champion-only / phased access. */
  phase: string
  phase_label: string
  instrument: InstrumentStatus
  readiness: ReadinessItem[]
  metrics: ScorecardMetric[]
  export: ExportDescriptor
  /** Set by the client when no instrument answered. Never sent by the server. */
  unavailable?: { reason: string }
}

/* ── Definitions and caveats (documentation, not data) ───────────────────── */

/**
 * Definition + caveat text, used ONLY as a fallback when the server does not
 * supply its own. These are quoted from docs/SHADOW_MODE.md §5 — they are the
 * operational definitions of the metrics and the stated limits of each one.
 *
 * They are text about method, never a measurement: nothing in here can become
 * a number on screen. That distinction is what makes it safe to ship a
 * populated view against an empty instrument.
 */
export const METRIC_SPEC: Record<
  MetricId,
  { label: string; definition: string; caveat: string; denominator_label: string; is_proxy: boolean }
> = {
  detection_lead_time: {
    label: 'Detection lead time',
    definition:
      'The interval between Orb first flagging a patient and the clinical event occurring: escalation of care, rapid-response call, critical-care transfer, or death.',
    caveat:
      'It cannot show that acting on the flag would have helped. A flag three hours before a transfer is evidence Orb saw something; it is not evidence that intervening at that point would have changed the outcome.',
    denominator_label: 'episodes with a recorded clinical outcome',
    is_proxy: false,
  },
  false_alert_rate: {
    label: 'False-alert rate',
    definition:
      'The proportion of alerts that did not correspond to a real deterioration, reported per occupied bed-day. The denominator that determines how often a nurse is interrupted.',
    caveat:
      'It cannot show alert fatigue. A tolerable rate on paper can still be intolerable at 3 a.m. on a short-staffed night. Unadjudicated alerts are reported as unadjudicated, never as correct.',
    denominator_label: 'occupied bed-days',
    is_proxy: false,
  },
  agreement: {
    label: 'Agreement',
    definition:
      "How often Sage's draft matched what the clinician actually did.",
    caveat:
      'Agreement is not correctness. A high figure is equally consistent with "Sage is good" and with "Sage is a fluent restatement of the obvious." The disagreements are the informative rows and need reading individually. A rising figure may only mean clinicians have started deferring to the system.',
    denominator_label: 'episodes with both a draft and a clinician action',
    is_proxy: false,
  },
  time_saved: {
    label: 'Time saved',
    definition:
      'Intervals Orb controls end to end: a Sage answer against the same lookup performed manually, measured once under observation rather than assumed.',
    caveat:
      'It cannot measure clinical time saved; that would require a controlled comparison this pilot is not running. It is the figure most likely to be quoted back in a fundraising context and the one with the weakest foundation.',
    denominator_label: 'timed Sage answers',
    is_proxy: true,
  },
}

/* ── Normalization ───────────────────────────────────────────────────────── */

const num = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null

const int = (v: unknown, fallback = 0): number =>
  typeof v === 'number' && Number.isFinite(v) ? Math.round(v) : fallback

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null)

const strList = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []

const counts = (v: unknown): Record<string, number> => {
  if (!v || typeof v !== 'object') return {}
  const out: Record<string, number> = {}
  for (const [k, n] of Object.entries(v as Record<string, unknown>)) {
    if (typeof n === 'number' && Number.isFinite(n)) out[k] = n
  }
  return out
}

const METRIC_STATES: MetricState[] = ['reported', 'insufficient_data', 'unadjudicated', 'not_computable']

/**
 * Turn one raw metric object into a ScorecardMetric.
 *
 * The state is *derived defensively*, not merely copied: a server that says
 * `reported` while sending no denominator, or an n below its own floor, is
 * downgraded here. The honesty rules are the client's job too — this view is
 * the artefact a CIO reads, and it must not be possible for a server bug to
 * turn four observations into a headline percentage.
 */
function normalizeMetric(id: MetricId, raw: unknown): ScorecardMetric {
  const spec = METRIC_SPEC[id]
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>

  const nObserved = int(r.n_observed)
  const nRequired = int(r.n_required, 0)
  const denominator = num(r.denominator)
  const display = str(r.display)
  const value = num(r.value)
  const blockers = strList(r.blockers)

  let state: MetricState = METRIC_STATES.includes(r.state as MetricState)
    ? (r.state as MetricState)
    : 'not_computable'

  // Downgrades. Each one exists because the alternative is a misleading number.
  if (state === 'reported') {
    if (display === null && value === null) state = 'not_computable'
    else if (denominator === null || denominator <= 0) state = 'insufficient_data'
    else if (nRequired > 0 && nObserved < nRequired) state = 'insufficient_data'
  }
  if (state !== 'not_computable' && blockers.length > 0) state = 'not_computable'

  const spread = r.spread && typeof r.spread === 'object'
    ? {
        p25: num((r.spread as Record<string, unknown>).p25),
        p50: num((r.spread as Record<string, unknown>).p50),
        p75: num((r.spread as Record<string, unknown>).p75),
      }
    : null

  const adjRaw = r.adjudication
  const adjudication: Adjudication | null =
    adjRaw && typeof adjRaw === 'object'
      ? {
          adjudicated: int((adjRaw as Record<string, unknown>).adjudicated),
          total: int((adjRaw as Record<string, unknown>).total),
          adjudicators: int((adjRaw as Record<string, unknown>).adjudicators),
          blinded: (adjRaw as Record<string, unknown>).blinded === true,
          sampling: str((adjRaw as Record<string, unknown>).sampling),
        }
      : null

  const altDenominators: AltDenominator[] = Array.isArray(r.alternative_denominators)
    ? (r.alternative_denominators as unknown[])
        .filter((a): a is Record<string, unknown> => !!a && typeof a === 'object')
        .map((a) => ({
          label: str(a.label) ?? '—',
          display: str(a.display),
          is_primary: a.is_primary === true,
        }))
    : []

  const assumptions: Assumption[] = Array.isArray(r.assumptions)
    ? (r.assumptions as unknown[])
        .filter((a): a is Record<string, unknown> => !!a && typeof a === 'object')
        .map((a) => ({
          label: str(a.label) ?? '—',
          value: str(a.value) ?? '—',
          source: str(a.source) ?? 'stated assumption',
        }))
    : []

  return {
    id,
    label: str(r.label) ?? spec.label,
    definition: str(r.definition) ?? spec.definition,
    state,
    value,
    unit: str(r.unit),
    display,
    numerator: num(r.numerator),
    denominator,
    denominator_label: str(r.denominator_label) ?? spec.denominator_label,
    alternative_denominators: altDenominators,
    n_observed: nObserved,
    n_required: nRequired,
    spread,
    caveat: str(r.caveat) ?? spec.caveat,
    blockers,
    is_proxy: typeof r.is_proxy === 'boolean' ? r.is_proxy : spec.is_proxy,
    assumptions,
    adjudication,
    disagreements: num(r.disagreements),
  }
}

/** Accepts `metrics` as either a keyed object or an array of objects carrying
 *  `id` — both shapes are natural on the server side, and guessing wrong here
 *  would blank the page for a reason no one could see. */
function metricsFrom(raw: unknown): ScorecardMetric[] {
  const byId = new Map<string, unknown>()
  if (Array.isArray(raw)) {
    for (const m of raw) {
      const id = m && typeof m === 'object' ? (m as Record<string, unknown>).id : null
      if (typeof id === 'string') byId.set(id, m)
    }
  } else if (raw && typeof raw === 'object') {
    for (const [k, v] of Object.entries(raw as Record<string, unknown>)) byId.set(k, v)
  }
  return METRIC_ORDER.map((id) => normalizeMetric(id, byId.get(id)))
}

function periodFrom(raw: unknown, fallbackDays: number): Period {
  const p = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const days = int(p.days, fallbackDays)
  const end = str(p.end) ?? new Date().toISOString()
  const start =
    str(p.start) ?? new Date(new Date(end).getTime() - days * 86_400_000).toISOString()
  return { start, end, days }
}

export function normalizeScorecard(raw: unknown, requestedDays: number): Scorecard {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>
  const inst = (r.instrument && typeof r.instrument === 'object'
    ? r.instrument
    : {}) as Record<string, unknown>
  const exp = (r.export && typeof r.export === 'object' ? r.export : {}) as Record<string, unknown>

  return {
    generated_at: str(r.generated_at),
    period: periodFrom(r.period, requestedDays),
    wards: strList(r.wards),
    phase: str(r.phase) ?? 'unknown',
    phase_label: str(r.phase_label) ?? 'Phase not reported',
    instrument: {
      source: str(inst.source) ?? 'shadow_events',
      schema_v: num(inst.schema_v),
      append_only: inst.append_only !== false,
      events_total: int(inst.events_total),
      episodes_total: int(inst.episodes_total),
      episodes_closed: int(inst.episodes_closed),
      first_event_at: str(inst.first_event_at),
      last_event_at: str(inst.last_event_at),
      lag_ms_p50: num(inst.lag_ms_p50),
      lag_ms_p95: num(inst.lag_ms_p95),
      lanes: counts(inst.lanes),
      kinds: counts(inst.kinds),
      corrections: int(inst.corrections),
    },
    readiness: Array.isArray(r.readiness)
      ? (r.readiness as unknown[])
          .filter((i): i is Record<string, unknown> => !!i && typeof i === 'object')
          .map((i, idx) => ({
            id: str(i.id) ?? `readiness-${idx}`,
            label: str(i.label) ?? '—',
            met: i.met === true,
            detail: str(i.detail) ?? '',
          }))
      : [],
    metrics: metricsFrom(r.metrics),
    export: {
      available: exp.available === true,
      formats: strList(exp.formats),
      deidentification: Array.isArray(exp.deidentification)
        ? (exp.deidentification as unknown[])
            .filter((d): d is Record<string, unknown> => !!d && typeof d === 'object')
            .map((d) => ({
              field: str(d.field) ?? '—',
              treatment: str(d.treatment) ?? 'removed',
              note: str(d.note),
            }))
        : [],
      note: str(exp.note),
    },
  }
}

/**
 * The model shown when no instrument answered: definitions, no measurements.
 *
 * Everything numeric is zero-or-null and every metric is `not_computable` with
 * an explicit blocker, so the view renders the shape of the evidence and states
 * plainly that none has been collected. A CIO reading this sees what will be
 * measured and that nothing has been.
 */
export function emptyScorecard(days: number, reason: string): Scorecard {
  const end = new Date().toISOString()
  return {
    generated_at: null,
    period: { start: new Date(Date.now() - days * 86_400_000).toISOString(), end, days },
    wards: [],
    phase: 'not_started',
    phase_label: 'Shadow mode has not started on this appliance',
    instrument: {
      source: 'shadow_events',
      schema_v: null,
      append_only: true,
      events_total: 0,
      episodes_total: 0,
      episodes_closed: 0,
      first_event_at: null,
      last_event_at: null,
      lag_ms_p50: null,
      lag_ms_p95: null,
      lanes: {},
      kinds: {},
      corrections: 0,
    },
    readiness: [],
    metrics: METRIC_ORDER.map((id) =>
      normalizeMetric(id, { state: 'not_computable', blockers: [reason] }),
    ),
    export: { available: false, formats: [], deidentification: [], note: null },
    unavailable: { reason },
  }
}

/* ── Transport ───────────────────────────────────────────────────────────── */

/** The one canonical mount. See the header note on why there is no alias. */
const SCORECARD_PATHS = ['/admin/shadow/scorecard'] as const

const isNotFound = (e: unknown): boolean =>
  typeof e === 'object' && e !== null && (e as { status?: number }).status === 404

export interface ScorecardQuery {
  days: number
  ward?: string | null
}

function queryString(q: ScorecardQuery, extra?: Record<string, string>): string {
  const p = new URLSearchParams({ days: String(q.days) })
  if (q.ward) p.set('ward', q.ward)
  for (const [k, v] of Object.entries(extra ?? {})) p.set(k, v)
  return p.toString()
}

/**
 * Fetch the composite scorecard.
 *
 * A 404 from every mount resolves to `emptyScorecard()` rather than throwing:
 * "this appliance has recorded nothing" is a legitimate answer and the view
 * says so. Any other failure (500, auth, network) throws — that is a fault, and
 * a fault must never be dressed up as an empty instrument.
 */
export async function getScorecard(q: ScorecardQuery): Promise<Scorecard> {
  const qs = queryString(q)
  let lastNotFound: unknown = null
  for (const base of SCORECARD_PATHS) {
    try {
      const raw = await apiFetch<unknown>(`${base}?${qs}`)
      return normalizeScorecard(raw, q.days)
    } catch (e) {
      if (isNotFound(e)) {
        lastNotFound = e
        continue
      }
      throw e
    }
  }
  void lastNotFound
  return emptyScorecard(
    q.days,
    'The shadow-event instrument is not present on this appliance: no scorecard endpoint responded, so no episode, alert or outcome has been recorded here.',
  )
}

/** URL for the de-identified export. Kept here so the view never builds a path.
 *  Note the shape: `export/scorecard`, not `scorecard/export` — the export is a
 *  sibling projection of the shadow instrument, not a sub-resource of the
 *  scorecard. Matches server/routes/shadow_routes.py exactly. */
export function scorecardExportUrl(q: ScorecardQuery, format: string): string {
  return `/api/admin/shadow/export/scorecard?${queryString(q, { format })}`
}
