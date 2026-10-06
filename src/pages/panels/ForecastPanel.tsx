import { useState, useEffect, useMemo, useCallback, type CSSProperties } from 'react'
import ProvenanceChip from '../../components/ProvenanceChip'
import { buildHeaders } from '../../api/client'
import { localDateKey } from '../../lib/datetime'
import { rovingTabKeyDown } from '../../lib/tablist'

const API = ''

const legendItemStyle: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: '6px',
  fontSize: '0.8rem',
  color: 'var(--text-secondary)',
  whiteSpace: 'nowrap',
}

const legendDotStyle: CSSProperties = {
  display: 'inline-block',
  width: '10px',
  height: '10px',
  borderRadius: '50%',
  flexShrink: 0,
}

async function apiFetch(path: string) {
  const res = await fetch(`${API}${path}`, {
    // Cookie session (buildHeaders adds a Bearer only if a token is persisted).
    credentials: 'include',
    headers: buildHeaders('GET'),
  })
  if (!res.ok) throw new Error(`API ${res.status}`)
  return res.json()
}

interface DailyForecast {
  date: string
  day_label: string
  predicted_admissions: number
  predicted_discharges: number
  predicted_occupancy: number
  occupancy_pct: number
  ward_breakdown: Record<string, number>
  /** Server-sent day 0: the three numbers above are MEASURED, not predicted.
   *  Keyed on directly — never inferred from `date`, which the server computes
   *  in the hospital's zone and this client computes in the browser's. */
  is_current?: boolean
  actual_admissions?: number
  actual_discharges?: number
  measured_occupancy?: number
}

/** A chart point; `is_anchor` marks the synthesized day-0 census point, which
 *  carries no admission/discharge predictions. */
type ChartPoint = DailyForecast & { is_anchor?: boolean }

/** One seasonal calendar entry active today (Pulse's built-in calendar for
 *  the hospital's state). Context beside the numbers only: the server does not
 *  multiply any projection by it. */
interface SeasonContext {
  id: string
  title: string
  kind: string
  in_peak: boolean
}

interface HospitalForecast {
  current_occupancy: number
  total_capacity: number
  current_occupancy_pct: number
  daily_forecasts: DailyForecast[]
  /** Empty when the hospital's state is not set or nothing is active today. */
  season?: SeasonContext[]
  summary: {
    avg_occupancy: number
    peak_day: string
    peak_occupancy: number
    total_predicted_discharges_7d: number
    total_predicted_admissions_7d: number
    beds_available_in_48h: number
  }
}

interface DischargeRow {
  patient_id: string
  name: string
  ward: string
  bed: string
  diagnosis: string
  age: number
  sex: string
  days_admitted: number
  predicted_remaining_days: number
  discharge_probability: number
  acuity_trajectory: string
  confidence: number
  estimated_discharge_date: string
}

interface CapacityData {
  current: Record<string, { total: number; occupied: number; available: number; utilization_pct: number }>
  projected_48h: Record<string, { projected_occupied: number; projected_available: number; projected_utilization_pct: number }>
  total: { capacity: number; occupied: number; available: number }
}

interface AdmissionPatterns {
  daily_summary: { day: string; avg_admissions: number; total: number }[]
  total_30d: number
  avg_daily: number
  avg_los: number
  severity_distribution: Record<string, number>
  ward_distribution: Record<string, number>
  discharge_rate: number
}



// ── Mini Sparkline Component ──
function MiniBar({ value, max, color }: { value: number; max: number; color: string }) {
  const pct = Math.min(100, (value / max) * 100)
  return (
    <div style={{ width: '100%', height: 6, background: 'var(--bg-glass)', borderRadius: 3, overflow: 'hidden' }}>
      <div style={{
        width: `${pct}%`, height: '100%', borderRadius: 3,
        // color-mix instead of `${color}88` so var(--*) tokens work as well as hex literals
        // (appending "88" to a var() produced invalid CSS and an invisible fill).
        background: `linear-gradient(90deg, ${color}, color-mix(in srgb, ${color} 53%, transparent))`,
        transition: 'width 0.6s ease',
      }} />
    </div>
  )
}

/** "+3" admissions, "-2" discharges, and plain "0" for none: "-0" reads as
 *  a typo, or as a negative nobody can explain. */
function signedCount(n: number, sign: '+' | '-'): string {
  return n === 0 ? '0' : `${sign}${n}`
}

// ── Metric Card ──
function MetricCard({ label, value, unit, accent, subtitle }: {
  label: string; value: string | number; unit?: string; accent: string; subtitle?: string
}) {
  return (
    <div className="forecast-metric-card" style={{ '--accent': accent } as React.CSSProperties}>
      <span className="forecast-metric-label">{label}</span>
      <div className="forecast-metric-value">
        <span className="metric-mono" style={{ color: accent, fontSize: '1.8rem' }}>{value}</span>
        {unit && <span className="forecast-metric-unit">{unit}</span>}
      </div>
      {subtitle && <span className="forecast-metric-subtitle">{subtitle}</span>}
    </div>
  )
}

type Section = 'census' | 'capacity' | 'discharge' | 'patterns'
const SECTION_IDS: readonly Section[] = ['census', 'capacity', 'discharge', 'patterns']

export default function ForecastPanel() {
  const [hospitalData, setHospitalData] = useState<HospitalForecast | null>(null)
  const [dischargeBoard, setDischargeBoard] = useState<DischargeRow[]>([])
  const [capacity, setCapacity] = useState<CapacityData | null>(null)
  const [patterns, setPatterns] = useState<AdmissionPatterns | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [activeSection, setActiveSection] = useState<Section>('census')

  const fetchAll = useCallback(async () => {
    try {
      const [hosp, disc, cap, pat] = await Promise.all([
        apiFetch('/api/forecast/hospital'),
        apiFetch('/api/forecast/discharge-board'),
        apiFetch('/api/forecast/capacity'),
        apiFetch('/api/forecast/admission-patterns'),
      ])
      setHospitalData(hosp)
      setDischargeBoard(disc)
      setCapacity(cap)
      setPatterns(pat)
      setError('')
    } catch (e) {
      setError((e as { message?: string }).message || 'Failed to load forecast data')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    fetchAll()
    const interval = setInterval(fetchAll, 300000) // Refresh every 5 min (backend caches for 1h)
    return () => clearInterval(interval)
  }, [fetchAll])

  // ── Chart dimensions ──
  const chartHeight = 140
  const chartWidth = 700

  // The forecast API's series started at TOMORROW, so the chart's first point
  // silently contradicted the Current Census card and Command Center
  // (DEMO_READINESS §13.5). Anchor day 0 at the actual census so the projection
  // is continuous with what the header claims. Skipped when the server sends a
  // real day 0 — which it now does.
  const chartSeries = useMemo<ChartPoint[]>(() => {
    if (!hospitalData?.daily_forecasts?.length) return []
    const daily = hospitalData.daily_forecasts
    // FLAG FIRST, date second. The server's day key is the HOSPITAL's calendar
    // day; `localDateKey()` is the browser's. They differ whenever the two
    // zones do — and in IST they also differ for the appliance's own first 5.5
    // hours if the key is ever computed in UTC again. Keying on the date alone
    // would prepend a second "today" on top of the server's every night shift.
    if (daily[0].is_current) return daily
    const todayKey = localDateKey()
    if (daily[0].date === todayKey) return daily
    return [{
      date: todayKey,
      day_label: 'Today',
      predicted_admissions: 0,
      predicted_discharges: 0,
      predicted_occupancy: hospitalData.current_occupancy,
      occupancy_pct: Math.round(hospitalData.current_occupancy_pct),
      ward_breakdown: {},
      is_anchor: true,
    }, ...daily]
  }, [hospitalData])

  // Compute chart points for the 7-day occupancy forecast
  const occupancyPath = useMemo(() => {
    if (!chartSeries.length || !hospitalData) return ''
    const maxOcc = Math.max(hospitalData.total_capacity + 2, ...chartSeries.map(f => f.predicted_occupancy))
    const points = chartSeries.map((f, i) => {
      const x = (i / Math.max(1, chartSeries.length - 1)) * chartWidth
      const y = chartHeight - (f.predicted_occupancy / maxOcc) * (chartHeight - 20)
      return `${x},${y}`
    })
    return points.join(' ')
  }, [chartSeries, hospitalData])

  const capacityLine = useMemo(() => {
    if (!hospitalData) return ''
    const y = chartHeight - (hospitalData.total_capacity / (hospitalData.total_capacity + 5)) * (chartHeight - 20)
    return `0,${y} ${chartWidth},${y}`
  }, [hospitalData])

  if (loading) {
    return (
      <div className="orb-panel animate-fade-in">
        <div className="orb-panel-loading">
          <div className="forecast-loading-pulse" />
          Initializing Orb Forecast engine...
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="orb-panel animate-fade-in">
        <div className="orb-panel-loading" style={{ color: 'var(--status-danger)' }}>
          ⚠ {error}
          <button onClick={fetchAll} style={{ marginTop: 12, padding: '8px 16px', background: 'var(--bg-glass-hover)', border: '1px solid var(--border-glass)', borderRadius: 8, color: 'var(--text-primary)', cursor: 'pointer' }}>
            Retry
          </button>
        </div>
      </div>
    )
  }

  const summary = hospitalData?.summary
  // "Season: South-west monsoon (peak), Dengue season". One quiet line of
  // context; it changes no number on this screen.
  const seasonLine = (hospitalData?.season ?? [])
    .map(s => (s.in_peak ? `${s.title} (peak)` : s.title))
    .join(', ')

  return (
    <div className="orb-panel forecast-panel animate-fade-in">

      {/* ═══ Premium Hero Info Panel ═══ */}
      <div className="forecast-hero">
        <div className="forecast-hero-content">
          <h2 className="forecast-hero-title">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
            </svg>
            Orb Forecast Engine
            <ProvenanceChip modelKey="forecast_lstm" />
          </h2>
          {/* Says what the chip beside the title and the footer say. It used
              to claim a trained LSTM next to an "untrained" provenance chip. */}
          <p className="forecast-hero-desc">
            Rule-based projections of length of stay, discharges and acuity, with live views of
            hospital census, ward capacity and discharge readiness. The forecasting model is not yet trained.
          </p>
          {seasonLine && (
            <p className="forecast-season-line" style={{ margin: '6px 0 0', fontSize: 'var(--text-sm)', lineHeight: 1.5, color: 'var(--text-secondary)' }}>
              Season: {seasonLine}
            </p>
          )}
        </div>
      </div>

      {/* ═══ Section Navigation Pills ═══
          Scrolls sideways on a phone instead of squeezing: four nowrap labels
          at flex:1 printed over each other at 390px. */}
      <div role="tablist" aria-label="Forecast sections" onKeyDown={rovingTabKeyDown(
        SECTION_IDS, activeSection, setActiveSection, id => `forecast-tab-${id}`,
      )} style={{
        display: 'flex', gap: '8px', marginBottom: '24px', padding: '6px',
        background: 'var(--bg-glass)', borderRadius: '14px',
        border: '1px solid var(--border-glass)',
        overflowX: 'auto',
      }}>
        {([
          { id: 'census', label: 'Census Forecast', icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg> },
          { id: 'capacity', label: 'Ward Capacity', icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M3 14h18"/><path d="M8 14v5"/><path d="M16 14v5"/><path d="M5 14V6a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v8"/><path d="M10 8h4"/><path d="M12 6v4"/></svg> },
          { id: 'discharge', label: 'Discharge Board', icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg> },
          { id: 'patterns', label: 'Admission Patterns', icon: <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg> },
        ] as const).map(s => (
          <button
            key={s.id}
            id={`forecast-tab-${s.id}`}
            type="button"
            role="tab"
            aria-selected={activeSection === s.id}
            aria-controls="forecast-section"
            tabIndex={activeSection === s.id ? 0 : -1}
            onClick={() => setActiveSection(s.id)}
            style={{
              flex: '1 0 auto', padding: '12px 16px', borderRadius: '10px',
              cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
              gap: '8px', fontSize: '0.88rem', fontWeight: 600, whiteSpace: 'nowrap',
              transition: 'background var(--transition-fast), color var(--transition-fast)',
              background: activeSection === s.id ? 'var(--accent-glow)' : 'transparent',
              color: activeSection === s.id ? 'var(--accent-text)' : 'var(--text-secondary)',
              // Flat: the selected tab is a fill and a hairline, no glow.
              border: `1px solid ${activeSection === s.id ? 'var(--accent-gold)' : 'transparent'}`,
            }}
          >
            <span aria-hidden="true" style={{ display: 'flex', alignItems: 'center', flexShrink: 0 }}>{s.icon}</span>
            {s.label}
          </button>
        ))}
      </div>

      {/* ═══ SECTION 1: Hospital Census Forecast ═══ */}
      {activeSection === 'census' && (
        <div className="forecast-section animate-fade-in" id="forecast-section" role="tabpanel" aria-labelledby="forecast-tab-census">
          {/* KPI Strip */}
          <div className="forecast-kpi-strip" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '12px', marginBottom: '16px' }}>
            <MetricCard
              label="Current Census"
              value={hospitalData?.current_occupancy || 0}
              unit={`/ ${hospitalData?.total_capacity || 32}`}
              accent="var(--accent-amber)"
              subtitle={`${hospitalData?.current_occupancy_pct?.toFixed(0)}% occupied`}
            />
            <MetricCard
              label="Beds Available in 48h"
              value={summary?.beds_available_in_48h || 0}
              accent={summary && summary.beds_available_in_48h <= 2 ? 'var(--status-danger)' : 'var(--status-ok)'}
              subtitle="Projected availability"
            />
            <MetricCard
              label="7-Day Discharges"
              value={summary?.total_predicted_discharges_7d || 0}
              accent="var(--accent-amber)"
              subtitle={`${summary?.total_predicted_admissions_7d || 0} admissions predicted`}
            />
            <MetricCard
              label="Peak Day"
              value={summary?.peak_day || '—'}
              accent="var(--text-primary)"
              subtitle={`${summary?.peak_occupancy || 0} patients`}
            />
          </div>

          {/* 7-Day Occupancy Chart */}
          <div className="glass-card forecast-chart-card">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
              <h3 className="detail__section-title" style={{ margin: 0 }}>7-Day Occupancy Projection</h3>
              <div className="forecast-chart-legend" style={{ display: 'flex', flexWrap: 'wrap', gap: '16px' }}>
                <span className="forecast-legend-item" style={legendItemStyle}><span className="forecast-legend-dot" style={{ ...legendDotStyle, background: 'var(--accent-amber)' }} /> Occupancy</span>
                <span className="forecast-legend-item" style={legendItemStyle}><span className="forecast-legend-dot" style={{ ...legendDotStyle, background: 'var(--status-ok)' }} /> Admissions</span>
                <span className="forecast-legend-item" style={legendItemStyle}><span className="forecast-legend-dot" style={{ ...legendDotStyle, background: 'var(--status-info)' }} /> Discharges</span>
                <span className="forecast-legend-item" style={legendItemStyle}><span className="forecast-legend-dot" style={{ ...legendDotStyle, background: 'color-mix(in srgb, var(--status-danger) 50%, transparent)', border: '1px dashed var(--status-danger)' }} /> Capacity</span>
              </div>
            </div>

            <div style={{ display: 'flex', gap: '24px', alignItems: 'flex-start', marginInline: 'auto', width: '100%' }}>
              <div style={{ position: 'relative', flex: 1, minWidth: 0 }}>
              <svg viewBox={`-30 -10 ${chartWidth + 60} ${chartHeight + 60}`} style={{ width: '100%', height: 'auto' }}>
                {/* Grid lines */}
                {[0.25, 0.5, 0.75, 1.0].map(pct => {
                  const y = chartHeight - pct * (chartHeight - 20)
                  return <line key={pct} x1={0} y1={y} x2={chartWidth} y2={y} stroke="var(--border-subtle)" strokeDasharray="4,4" />
                })}

                {/* Capacity line (dashed red) */}
                {capacityLine && (
                  <polyline
                    points={capacityLine}
                    fill="none"
                    stroke="var(--status-danger)"
                    strokeWidth="1.5"
                    strokeDasharray="8,4"
                    opacity="0.5"
                  />
                )}

                {/* Occupancy area fill */}
                {occupancyPath && (
                  <>
                    <defs>
                      <linearGradient id="occGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="var(--accent-amber)" stopOpacity="0.3" />
                        <stop offset="100%" stopColor="var(--accent-amber)" stopOpacity="0.02" />
                      </linearGradient>
                    </defs>
                    <polygon
                      points={`0,${chartHeight} ${occupancyPath} ${chartWidth},${chartHeight}`}
                      fill="url(#occGrad)"
                    />
                    <polyline
                      points={occupancyPath}
                      fill="none"
                      stroke="var(--accent-amber)"
                      strokeWidth="2.5"
                      strokeLinejoin="round"
                    />
                  </>
                )}

                {/* Admission/Discharge bars — none on the day-0 anchor, which
                    carries a measured census, not flow predictions. */}
                {chartSeries.map((f, i) => {
                  if (f.is_anchor) return null
                  const x = (i / Math.max(1, chartSeries.length - 1)) * chartWidth
                  const barW = chartWidth / chartSeries.length * 0.3
                  const maxVal = Math.max(...chartSeries.map(ff => Math.max(ff.predicted_admissions, ff.predicted_discharges)), 1)
                  const admH = (f.predicted_admissions / maxVal) * 40
                  const disH = (f.predicted_discharges / maxVal) * 40
                  return (
                    <g key={i}>
                      {/* Admission bar */}
                      <rect x={x - barW - 1} y={chartHeight - admH} width={barW} height={admH}
                        fill="var(--status-ok)" opacity="0.6" rx="2" />
                      {/* Discharge bar — distinct hue from the occupancy line */}
                      <rect x={x + 1} y={chartHeight - disH} width={barW} height={disH}
                        fill="var(--status-info)" opacity="0.6" rx="2" />
                    </g>
                  )
                })}

                {/* Data points on occupancy line */}
                {chartSeries.map((f, i) => {
                  const maxOcc = Math.max(hospitalData!.total_capacity + 2, ...chartSeries.map(ff => ff.predicted_occupancy))
                  const x = (i / Math.max(1, chartSeries.length - 1)) * chartWidth
                  const y = chartHeight - (f.predicted_occupancy / maxOcc) * (chartHeight - 20)
                  return (
                    <g key={`dot-${i}`}>
                      <circle cx={x} cy={y} r="5" fill="var(--bg-primary)" stroke="var(--accent-amber)" strokeWidth="2" />
                      <text x={x} y={y - 12} textAnchor="middle" fill="var(--accent-amber)" fontSize="11" fontFamily="var(--font-mono)">
                        {f.predicted_occupancy}
                      </text>
                    </g>
                  )
                })}

                {/* X-axis labels */}
                {chartSeries.map((f, i) => {
                  const x = (i / Math.max(1, chartSeries.length - 1)) * chartWidth
                  return (
                    <text key={`label-${i}`} x={x} y={chartHeight + 20} textAnchor="middle" fill="var(--text-secondary)" fontSize="10" fontFamily="var(--font-mono)">
                      {/* The server sends "%a %b %d" ("Sat Aug 09"). Taking the
                          first TWO tokens dropped the day number, so every tick
                          read "Sat Aug" and a seven-day axis showed no dates at
                          all. Drop the weekday instead and keep "Aug 09".
                          Day 0 is labelled "Today" whichever side produced it —
                          its date string is the hospital's day, which is not
                          necessarily the day this browser is having. */}
                      {f.is_current || f.is_anchor
                        ? 'Today'
                        : f.day_label.split(' ').slice(1).join(' ') || f.day_label}
                    </text>
                  )
                })}
              </svg>
              </div>
            </div>

            {/* Daily breakdown table (Semantic format) */}
            <div className="forecast-daily-table" style={{ marginTop: '16px', borderRadius: '10px', overflow: 'hidden' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
                <thead style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.8px', color: 'var(--text-muted)' }}>
                  <tr>
                    <th style={{ padding: '8px 12px', fontWeight: 500 }}>Day</th>
                    <th style={{ padding: '8px 12px', fontWeight: 500 }}>Admissions</th>
                    <th style={{ padding: '8px 12px', fontWeight: 500 }}>Discharges</th>
                    <th style={{ padding: '8px 12px', fontWeight: 500 }}>Census</th>
                    <th style={{ padding: '8px 12px', fontWeight: 500 }}>Occupancy</th>
                  </tr>
                </thead>
                <tbody>
                  {chartSeries.map((f, i) => {
                    // Two kinds of day 0: the server's MEASURED one (real
                    // counts, shown as actuals) and the locally synthesized
                    // anchor used when the server sends none (census only — it
                    // must not claim flow it was never given).
                    const admissions = f.actual_admissions ?? f.predicted_admissions
                    const discharges = f.actual_discharges ?? f.predicted_discharges
                    return (
                    <tr key={i} style={{ background: f.occupancy_pct > 90 ? 'color-mix(in srgb, var(--status-danger) 12%, transparent)' : 'transparent', borderLeft: f.occupancy_pct > 90 ? '2px solid var(--status-danger)' : '2px solid transparent' }}>
                      <td style={{ padding: '8px 12px', fontSize: '0.85rem' }}>{f.is_anchor ? 'Today (current census)' : f.is_current ? 'Today (actual so far)' : f.day_label}</td>
                      <td style={{ padding: '8px 12px' }}>{f.is_anchor ? <span style={{ color: 'var(--text-muted)' }}>—</span> : <span className="metric-mono" title={f.is_current ? 'Admissions recorded so far today' : undefined} style={{ color: 'var(--status-ok-text)', background: 'color-mix(in srgb, var(--status-ok) 16%, transparent)', padding: '2px 6px', borderRadius: '4px' }}>{signedCount(admissions, '+')}</span>}</td>
                      <td style={{ padding: '8px 12px' }}>{f.is_anchor ? <span style={{ color: 'var(--text-muted)' }}>—</span> : <span className="metric-mono" title={f.is_current ? 'Discharges recorded so far today' : undefined} style={{ color: 'var(--status-info-text)', background: 'color-mix(in srgb, var(--status-info) 16%, transparent)', padding: '2px 6px', borderRadius: '4px' }}>{signedCount(discharges, '-')}</span>}</td>
                      <td style={{ padding: '8px 12px' }}><span className="metric-mono" style={{ fontWeight: 600 }}>{f.predicted_occupancy}</span></td>
                      <td style={{ padding: '8px 12px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                          <MiniBar value={f.predicted_occupancy} max={hospitalData!.total_capacity}
                            color={f.occupancy_pct > 90 ? 'var(--status-danger)' : f.occupancy_pct > 70 ? 'var(--status-warn)' : 'var(--accent-amber)'} />
                          <span className="metric-mono" style={{ fontSize: '0.7rem', marginLeft: 4 }}>{f.occupancy_pct}%</span>
                        </div>
                      </td>
                    </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* ═══ SECTION 2: Ward Capacity ═══ */}
      {activeSection === 'capacity' && capacity && (
        <div className="forecast-section animate-fade-in" id="forecast-section" role="tabpanel" aria-labelledby="forecast-tab-capacity">
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '16px' }}>
            {Object.entries(capacity.current).map(([ward, data]) => {
              const proj = capacity.projected_48h[ward]
              const isOverloaded = data.utilization_pct >= 90
              const willFreeUp = proj && proj.projected_available > data.available

              return (
                <div key={ward} className="glass-card" style={{
                  padding: '20px', display: 'flex', flexDirection: 'column', gap: '16px',
                  borderColor: isOverloaded ? 'color-mix(in srgb, var(--status-danger) 30%, transparent)' : undefined,
                  background: isOverloaded ? 'linear-gradient(145deg, color-mix(in srgb, var(--status-danger) 12%, transparent), var(--bg-surface))' : undefined,
                }}>
                  {/* Header */}
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <h4 style={{ margin: 0, fontSize: '1.2rem', fontWeight: 600, color: 'var(--text-primary)' }}>{ward}</h4>
                    <span style={{
                      fontSize: '0.75rem', fontWeight: 700, letterSpacing: '0.8px', padding: '4px 10px',
                      borderRadius: '8px', textTransform: 'uppercase' as const,
                      color: isOverloaded ? 'var(--status-danger-text)' : data.utilization_pct > 70 ? 'var(--status-warn-text)' : 'var(--status-ok-text)',
                      background: isOverloaded ? 'color-mix(in srgb, var(--status-danger) 18%, transparent)' : data.utilization_pct > 70 ? 'color-mix(in srgb, var(--status-warn) 18%, transparent)' : 'color-mix(in srgb, var(--status-ok) 18%, transparent)',
                      border: `1px solid ${isOverloaded ? 'color-mix(in srgb, var(--status-danger) 30%, transparent)' : data.utilization_pct > 70 ? 'color-mix(in srgb, var(--status-warn) 30%, transparent)' : 'color-mix(in srgb, var(--status-ok) 30%, transparent)'}`,
                    }}>
                      {isOverloaded ? '⚠ FULL' : data.utilization_pct > 70 ? 'HIGH' : 'OK'}
                    </span>
                  </div>

                  {/* Bed grid */}
                  <div style={{
                    display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(32px, 1fr))',
                    gap: '6px',
                  }}>
                    {Array.from({ length: data.total }).map((_, i) => {
                      const occupied = i < data.occupied
                      return (
                        <div key={i} title={occupied ? 'Occupied' : 'Available'} style={{
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          padding: '6px', borderRadius: '6px', aspectRatio: '1',
                          background: occupied ? 'color-mix(in srgb, var(--status-warn) 16%, transparent)' : 'color-mix(in srgb, var(--status-ok) 14%, transparent)',
                          border: `1px solid ${occupied ? 'color-mix(in srgb, var(--status-warn) 25%, transparent)' : 'color-mix(in srgb, var(--status-ok) 20%, transparent)'}`,
                        }}>
                          <svg width="16" height="16" viewBox="0 0 24 24" fill={occupied ? 'var(--status-warn)' : 'var(--status-ok)'} opacity={occupied ? 0.8 : 0.5}>
                            <path d="M7 14c1.66 0 3-1.34 3-3S8.66 8 7 8s-3 1.34-3 3 1.34 3 3 3zm0-4c.55 0 1 .45 1 1s-.45 1-1 1-1-.45-1-1 .45-1 1-1zM19 7h-8v7H3V5H1v15h2v-3h18v3h2V11c0-2.21-1.79-4-4-4zm2 8h-8V9h6c1.1 0 2 .9 2 2v4z"/>
                          </svg>
                        </div>
                      )
                    })}
                  </div>

                  {/* Stats */}
                  <div style={{
                    display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px',
                    paddingTop: '12px', borderTop: '1px solid var(--border-subtle)',
                  }}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                      <span style={{ fontSize: '0.68rem', textTransform: 'uppercase' as const, letterSpacing: '0.8px', color: 'var(--text-muted)' }}>Now</span>
                      <span className="metric-mono">{data.occupied}/{data.total}</span>
                      <MiniBar value={data.occupied} max={data.total}
                        color={isOverloaded ? 'var(--status-danger)' : 'var(--status-warn)'} />
                    </div>
                    {proj && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                        <span style={{ fontSize: '0.68rem', textTransform: 'uppercase' as const, letterSpacing: '0.8px', color: 'var(--text-muted)' }}>48h</span>
                        <span className="metric-mono" style={{ color: willFreeUp ? 'var(--status-ok)' : 'var(--text-secondary)' }}>
                          {proj.projected_occupied}/{data.total}
                          {willFreeUp && ' ↓'}
                        </span>
                        <MiniBar value={proj.projected_occupied} max={data.total}
                          color={proj.projected_utilization_pct > 90 ? 'var(--status-danger)' : 'var(--status-ok)'} />
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>

          {/* Total summary */}
          <div className="glass-card" style={{ marginTop: 16, padding: 20 }}>
            <div style={{ display: 'flex', justifyContent: 'space-around', textAlign: 'center' }}>
              <div>
                <div className="metric-mono" style={{ fontSize: '2rem', color: 'var(--accent-amber)' }}>{capacity.total.occupied}</div>
                <div style={{ color: 'var(--text-secondary)', fontSize: '0.8rem', marginTop: 4 }}>Total Occupied</div>
              </div>
              <div>
                <div className="metric-mono" style={{ fontSize: '2rem', color: 'var(--status-ok)' }}>{capacity.total.available}</div>
                <div style={{ color: 'var(--text-secondary)', fontSize: '0.8rem', marginTop: 4 }}>Available Now</div>
              </div>
              <div>
                <div className="metric-mono" style={{ fontSize: '2rem', color: 'var(--accent-amber)' }}>{capacity.total.capacity}</div>
                <div style={{ color: 'var(--text-secondary)', fontSize: '0.8rem', marginTop: 4 }}>Total Capacity</div>
              </div>
              <div>
                <div className="metric-mono" style={{ fontSize: '2rem', color: capacity.total.occupied / capacity.total.capacity > 0.85 ? 'var(--status-danger)' : 'var(--text-primary)' }}>
                  {(capacity.total.occupied / capacity.total.capacity * 100).toFixed(0)}%
                </div>
                <div style={{ color: 'var(--text-secondary)', fontSize: '0.8rem', marginTop: 4 }}>Utilization Rate</div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ═══ SECTION 3: Discharge Readiness Board ═══ */}
      {activeSection === 'discharge' && (
        <div className="forecast-section animate-fade-in" id="forecast-section" role="tabpanel" aria-labelledby="forecast-tab-discharge">
          <div className="glass-card" style={{ padding: '20px 24px' }}>
            <h3 className="detail__section-title" style={{ marginBottom: 16 }}>
              Discharge Readiness: Next 72 Hours
              <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', fontWeight: 400, marginLeft: 8 }}>
                {dischargeBoard.length} patients · Sorted by discharge likelihood
              </span>
            </h3>

            <div className="forecast-discharge-list">
              <div className="forecast-discharge-header">
                <span style={{ flex: 2 }}>Patient</span>
                <span>Ward/Bed</span>
                <span>Days In</span>
                <span>Est. Remaining</span>
                <span>Discharge Prob.</span>
                <span>Trajectory</span>
              </div>

              {dischargeBoard.map((row, i) => (
                <div key={row.patient_id} className={`forecast-discharge-row ${i < 5 ? 'forecast-discharge-row--likely' : ''}`}
                  style={{ '--acuity-color': row.acuity_trajectory === 'improving' ? 'var(--status-ok)' : row.acuity_trajectory === 'worsening' ? 'var(--status-danger)' : 'var(--accent-amber)' } as React.CSSProperties}>
                  <span style={{ flex: 2 }}>
                    <strong>{row.name}</strong>
                    <br />
                    <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{row.diagnosis?.slice(0, 35)}{(row.diagnosis?.length || 0) > 35 ? '…' : ''}</span>
                  </span>
                  <span className="metric-mono">{row.ward} · {row.bed}</span>
                  <span className="metric-mono">{row.days_admitted.toFixed(1)}d</span>
                  <span className="metric-mono" style={{ color: row.predicted_remaining_days <= 2 ? 'var(--status-ok)' : 'var(--text-primary)' }}>
                    {row.predicted_remaining_days}d
                  </span>
                  <span>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <MiniBar value={row.discharge_probability} max={1}
                        color={row.discharge_probability > 0.5 ? 'var(--status-ok)' : 'var(--accent-amber)'} />
                      <span className="metric-mono" style={{ fontSize: '0.75rem', minWidth: 40 }}>
                        {(row.discharge_probability * 100).toFixed(0)}%
                      </span>
                    </div>
                  </span>
                  <span className={`forecast-trajectory forecast-trajectory--${row.acuity_trajectory}`}>
                    {row.acuity_trajectory === 'improving' ? '↗ Improving' : row.acuity_trajectory === 'worsening' ? '↘ Worsening' : '→ Stable'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ═══ SECTION 4: Admission Patterns ═══ */}
      {activeSection === 'patterns' && patterns && (
        <div className="forecast-section animate-fade-in" id="forecast-section" role="tabpanel" aria-labelledby="forecast-tab-patterns">
          {/* Summary cards */}
          <div className="forecast-kpi-strip">
            <MetricCard label="30-Day Admissions" value={patterns.total_30d} accent="var(--accent-amber)" subtitle={`${patterns.avg_daily}/day average`} />
            <MetricCard label="Avg Length of Stay" value={patterns.avg_los} unit="days" accent="var(--accent-amber)" />
            <MetricCard label="Discharge Rate" value={`${patterns.discharge_rate}%`} accent="var(--status-ok)" subtitle="30-day average" />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 16 }}>
            {/* Day-of-week chart */}
            <div className="glass-card" style={{ padding: 20 }}>
              <h3 className="detail__section-title" style={{ marginBottom: 16 }}>Admissions by Day of Week</h3>
              <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8, height: 140, padding: '0 12px' }}>
                {patterns.daily_summary.map((d, i) => {
                  const maxAdm = Math.max(...patterns.daily_summary.map(dd => dd.avg_admissions), 1)
                  const h = (d.avg_admissions / maxAdm) * 120
                  const isWeekend = i >= 5
                  return (
                    <div key={d.day} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
                      <span className="metric-mono" style={{ fontSize: '0.7rem', color: 'var(--accent-amber)' }}>{d.avg_admissions}</span>
                      <div style={{
                        width: '100%', height: h, borderRadius: '4px 4px 0 0',
                        background: isWeekend ? 'var(--bg-glass-hover)' : 'linear-gradient(180deg, var(--accent-amber), color-mix(in srgb, var(--accent-amber) 27%, transparent))',
                        transition: 'height 0.4s ease',
                      }} />
                      <span style={{ fontSize: '0.7rem', color: isWeekend ? 'var(--text-muted)' : 'var(--text-secondary)' }}>{d.day}</span>
                    </div>
                  )
                })}
              </div>
            </div>

            {/* Severity + Ward distribution */}
            <div className="glass-card" style={{ padding: 20 }}>
              <h3 className="detail__section-title" style={{ marginBottom: 16 }}>Distribution Analysis</h3>

              <div style={{ marginBottom: 20 }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 1 }}>By Severity</span>
                {Object.entries(patterns.severity_distribution).map(([sev, count]) => (
                  <div key={sev} style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
                    <span style={{ width: 70, fontSize: '0.8rem', color: sev === 'critical' ? 'var(--status-danger)' : sev === 'elevated' ? 'var(--accent-amber)' : 'var(--status-ok)', textTransform: 'capitalize' }}>{sev}</span>
                    <MiniBar value={count} max={patterns.total_30d}
                      color={sev === 'critical' ? 'var(--status-danger)' : sev === 'elevated' ? 'var(--accent-amber)' : 'var(--status-ok)'} />
                    <span className="metric-mono" style={{ fontSize: '0.75rem' }}>{count}</span>
                  </div>
                ))}
              </div>

              <div>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 1 }}>By Ward</span>
                {Object.entries(patterns.ward_distribution).map(([ward, count]) => (
                  <div key={ward} style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
                    <span style={{ width: 70, fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{ward}</span>
                    <MiniBar value={count} max={patterns.total_30d} color="var(--text-primary)" />
                    <span className="metric-mono" style={{ fontSize: '0.75rem' }}>{count}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Model provenance footer — honest labelling.
          The forecast LSTM is NOT yet trained/validated on real clinical data;
          until a validated checkpoint is loaded, figures come from a transparent,
          rule-based heuristic. We never present these as a validated neural model. */}
      <div className="forecast-footer">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent-amber)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>
        </svg>
        <span>Decision-support estimates. The Forecast model is not yet trained or validated on real clinical data. Figures are rule-based projections for planning, not validated neural predictions. See Admin → Model Governance.</span>
      </div>
    </div>
  )
}
