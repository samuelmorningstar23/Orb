import { useEffect, useMemo, useState } from 'react'
import { apiFetch } from '../../../api/client'
import { AdminLoadingState, AdminErrorState } from './AdminEmptyState'
import styles from './ImpactDashboard.module.css'
import { formatClinicalDateTime } from '../../../lib/datetime'

/**
 * Orb Impact — the quantified ROI / outcomes dashboard (the investor money slide).
 *
 * Converts Orb's actually-recorded clinical activity (Scribe notes, Vigil NEWS2
 * escalations, sepsis bundles, pharmacist verifications of AI meds, discharge
 * reconciliations) into defensible rupee figures and prevented-harm estimates. Every
 * figure cites the tunable assumption that produced it; the Assumptions panel lets
 * a CFO re-run the whole model with their own unit economics.
 */

interface Metric {
  key: string
  label: string
  /** null = not measured yet (e.g. no completed stays), never shown as 0. */
  value: number | null
  unit: string
  amount: number
  assumption: string
  evidence_count: number
}

interface Assumption {
  value: number
  unit: string
  citation: string
}

interface Currency {
  code: string
  locale: string
}

interface ImpactModel {
  generated_at: string
  currency?: Currency
  period: { days: number; start: string; end: string }
  metrics: Metric[]
  // ICU days and adverse drug events are different units, so two totals. They
  // were once summed into one "events prevented" figure. Optional so a payload
  // from an older server renders without the figure rather than as NaN.
  totals: {
    monthly_savings: number
    annual_savings: number
    icu_days_avoided?: number
    ades_avoided?: number
  }
  /** Escalations left out because simulated vitals drove them. */
  simulated_excluded?: number
  assumptions: Record<string, Assumption>
  has_data: boolean
}

// The money unit comes from the API (`currency`), which is the only thing that
// knows what the assumptions are denominated in. This used to be a hardcoded
// en-US/USD pair, so an India deployment — whose assumptions have always been a
// per-site number, and are now rupees — rendered its ROI as dollars on the
// investor slide. The fallback matches the server default rather than USD.
//
// en-IN also gives the right digit grouping: ₹12,34,567 (lakh/crore), not
// ₹1,234,567. Compact notation is deliberately NOT used for INR — Intl renders
// "₹1.2Cr"/"₹5L" inconsistently across engines, and a wrong-by-100× money
// figure on this screen is worse than a long one.
const DEFAULT_CURRENCY: Currency = { code: 'INR', locale: 'en-IN' }

const makeMoney = (cur: Currency | undefined) => {
  const { code, locale } = cur ?? DEFAULT_CURRENCY
  return (n: number, compact = false) =>
    new Intl.NumberFormat(locale, {
      style: 'currency', currency: code,
      maximumFractionDigits: 0,
      notation: compact && code !== 'INR' && Math.abs(n) >= 10000 ? 'compact' : 'standard',
    }).format(n)
}

const num = (n: number) => new Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 }).format(n)

const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many)

/** A rate is a fraction of events: 0.15, not 15. */
const isRate = (a: Assumption | undefined) => a?.unit === 'fraction'

/** Why a draft cannot be applied, or null. The server silently falls back to
 *  the default for a bad value, which would make Apply look like it ignored
 *  the CFO; say it here instead. */
function draftProblem(draft: Record<string, string>, assumptions: Record<string, Assumption>): string | null {
  for (const [key, raw] of Object.entries(draft)) {
    if (raw.trim() === '') continue
    const v = Number(raw)
    const label = ASSUMPTION_LABELS[key] || key
    if (!Number.isFinite(v)) return `${label} must be a number.`
    if (v < 0) return `${label} cannot be negative.`
    if (isRate(assumptions[key]) && v > 1) {
      return `${label} is a fraction between 0 and 1 (0.15 means 15%).`
    }
  }
  return null
}

// Human labels for the assumption keys (keeps the API as the source of truth
// for values/citations, while giving each editable field a friendly name).
const ASSUMPTION_LABELS: Record<string, string> = {
  period_days: 'Reporting window',
  doc_minutes_saved_per_note: 'Minutes saved / note',
  clinician_hourly_cost_inr: 'Clinician hourly cost',
  icu_day_cost_inr: 'ICU day cost',
  deterioration_icu_avoidance_rate: 'ICU-day avoidance rate',
  ade_cost_inr: 'Adverse drug event cost',
  ade_avoidance_rate_per_alert: 'ADE avoidance / alert',
  ward_bed_day_cost_inr: 'Ward bed-day cost',
  los_reduction_days_per_discharge: 'LOS reduction / discharge',
}

export default function ImpactDashboard() {
  const [model, setModel] = useState<ImpactModel | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)
  // Draft values for the editable assumption fields (string for controlled input).
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [applying, setApplying] = useState(false)

  const fetchModel = (overrides?: Record<string, string>) => {
    setLoading(!model)
    setApplying(!!overrides)
    setError(null)
    const qs = overrides
      ? '?' + Object.entries(overrides)
          .filter(([, v]) => v !== '' && v != null)
          .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
          .join('&')
      : ''
    apiFetch<ImpactModel>(`/admin/impact/${qs}`)
      .then((res) => {
        setModel(res)
        // Refresh the draft fields with what the server actually used.
        const d: Record<string, string> = {}
        Object.entries(res.assumptions).forEach(([k, a]) => { d[k] = String(a.value) })
        setDraft(d)
      })
      .catch((e) => setError(e?.message || 'Failed to load impact model'))
      .finally(() => { setLoading(false); setApplying(false) })
  }

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { fetchModel() }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = useMemo(() => {
    if (!model) return false
    return Object.entries(draft).some(([k, v]) => String(model.assumptions[k]?.value) !== v)
  }, [draft, model])

  // Rebuilt only when the API's currency changes, not on every render.
  const money = useMemo(() => makeMoney(model?.currency), [model?.currency])

  if (loading && !model) return <AdminLoadingState message="Computing impact model…" />
  if (error && !model) return <AdminErrorState error={error} onRetry={() => fetchModel()} />
  if (!model) return null

  const t = model.totals
  const problem = draftProblem(draft, model.assumptions)
  const simulated = model.simulated_excluded ?? 0

  return (
    <div className={styles.wrap}>
      <p className={styles.intro}>
        Orb's recorded clinical activity, converted into defensible rupee figures and prevented harm.
        Every figure below is a <b>counted event × a named, cited assumption</b>, no magic numbers.
        Adjust the assumptions to re-run the model with your own unit economics.
      </p>

      {/* ── Hero headline ── */}
      <div className={styles.hero}>
        <div className={styles.heroLabel}>Estimated impact · last {model.period.days} days</div>
        <div className={styles.heroFigures}>
          <div className={styles.heroPrimary}>
            {money(t.monthly_savings)}<small>/ month saved</small>
          </div>
          <div className={styles.heroSecondary}>
            {money(t.annual_savings, true)} <span>/ year</span>
          </div>
          {typeof t.icu_days_avoided === 'number' && (
            <div className={styles.heroSecondary}>
              {num(t.icu_days_avoided)} <span>ICU days avoided (est.)</span>
            </div>
          )}
          {typeof t.ades_avoided === 'number' && (
            <div className={styles.heroSecondary}>
              {num(t.ades_avoided)} <span>adverse drug events avoided (est.)</span>
            </div>
          )}
        </div>
        <div className={styles.heroSub}>
          {model.has_data
            ? 'Based on activity actually recorded in this deployment.'
            : 'No qualifying activity in this window yet. Figures will populate as Orb is used.'}
          {simulated > 0 && (
            <>
              {' '}{num(simulated)} {plural(simulated, 'escalation')} driven by simulated vitals{' '}
              {plural(simulated, 'is', 'are')} left out of these figures.
            </>
          )}
        </div>
      </div>

      {!model.has_data && (
        <div className={styles.noData}>
          No data yet in the selected window. Widen the reporting window in Assumptions. The
          model shows zero rather than inventing numbers.
        </div>
      )}

      {/* ── Metric cards ── */}
      <div className={styles.grid}>
        {model.metrics.map((m) => (
          <div key={m.key} className={styles.card}>
            <div className={styles.cardLabel}>{m.label}</div>
            <div className={styles.cardDollars}>{money(m.amount)}</div>
            <div className={styles.cardValue}>
              {m.value === null || m.value === undefined
                ? <b>Not yet measured</b>
                : <><b>{num(m.value)}</b> {m.unit}</>}
            </div>
            <span className={styles.evidence}>
              <span className={styles.evidenceDot} />
              {num(m.evidence_count)} recorded {plural(m.evidence_count, 'event')}
            </span>
            <div className={styles.assumption}>{m.assumption}</div>
          </div>
        ))}
      </div>

      {/* ── Assumptions expander ── */}
      <div className={styles.assumptionsPanel}>
        <button className={styles.assumptionsHeader} onClick={() => setExpanded((e) => !e)}
          aria-expanded={expanded}>
          <span>Assumptions: tunable & cited ({Object.keys(model.assumptions).length})</span>
          <svg className={`${styles.chevron} ${expanded ? styles.chevronOpen : ''}`}
            width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>

        {expanded && (
          <div className={styles.assumptionsBody}>
            {Object.entries(model.assumptions).map(([key, a]) => (
              <div key={key} className={styles.assumptionRow}>
                <div className={styles.assumptionTop}>
                  <span className={styles.assumptionName}>{ASSUMPTION_LABELS[key] || key}</span>
                  <span className={styles.assumptionField}>
                    <input
                      type="number"
                      step="any"
                      min={0}
                      max={isRate(a) ? 1 : undefined}
                      aria-label={ASSUMPTION_LABELS[key] || key}
                      value={draft[key] ?? String(a.value)}
                      onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
                    />
                    <span className={styles.unit}>{isRate(a) ? 'fraction, 0 to 1' : a.unit}</span>
                  </span>
                </div>
                <div className={styles.citation}>{a.citation}</div>
              </div>
            ))}

            {/* Once a model is on screen the full-page error state never shows,
                so a failed recompute used to vanish without a word. */}
            {(problem || error) && (
              <div role="alert" style={{ color: 'var(--status-danger-text)', fontSize: 'var(--text-sm)' }}>
                {problem || `Could not recompute: ${error}`}
              </div>
            )}

            <div className={styles.actions}>
              <button
                className={`${styles.btn} ${styles.btnPrimary}`}
                disabled={!dirty || applying || !!problem}
                onClick={() => fetchModel(draft)}
              >
                {applying ? 'Recomputing…' : 'Apply & recompute'}
              </button>
              <button
                className={`${styles.btn} ${styles.btnGhost}`}
                onClick={() => fetchModel()}
                disabled={applying}
              >
                Reset to defaults
              </button>
            </div>
          </div>
        )}
      </div>

      <div className={styles.footNote}>
        Generated {formatClinicalDateTime(model.generated_at)} · figures are estimates for
        planning &amp; diligence, not billing.
      </div>
    </div>
  )
}
