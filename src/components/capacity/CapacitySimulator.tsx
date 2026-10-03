/**
 * What-If Capacity & Surge Simulator
 *
 * Interactive scenario modeling on Orb's own forecast baseline. Operators drag
 * knobs (surge admissions, open/close beds, admission-rate multiplier, LOS,
 * aggressive discharge) and watch projected occupancy vs capacity, hours-to-
 * overflow, discharge need, and the nurse-staffing gap update live.
 *
 * Re-simulates (debounced) on every knob change via POST /api/capacity-sim.
 */
import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ResponsiveContainer,
} from 'recharts'
import { apiFetch } from '../../api/client'
import SizedChart from '../SizedChart'
import Icon, { type IconName } from '../common/Icon'
import './CapacitySimulator.css'

// ── Types mirroring the backend contract ──
interface ProjStep {
  step: number
  hour_offset: number
  projected_occupancy: number
  capacity: number
  overflow: number
  occupancy_pct: number | null
}
interface SimResult {
  baseline: { census: number; capacity: number; admission_rate: number }
  scenario: Record<string, number>
  projection: ProjStep[]
  summary: {
    peak_occupancy: number
    peak_occupancy_pct: number | null
    peak_hour_offset: number
    hours_to_overflow: number | null
    beds_short_at_peak: number
    suggested_discharges: number
    staffing_gap_nurses: number
    nurses_required_at_peak: number
    projected_admissions: number
    projected_discharges: number
  }
  assumptions: Record<string, { value: any; source: string }>
}

interface Knobs {
  horizon_hours: number
  extra_admissions: number
  beds_delta: number
  admission_rate_multiplier: number
  los_multiplier: number
  discharge_boost: number
  nurse_patient_ratio: number
}

const DEFAULT_KNOBS: Knobs = {
  horizon_hours: 48,
  extra_admissions: 0,
  beds_delta: 0,
  admission_rate_multiplier: 1,
  los_multiplier: 1,
  discharge_boost: 0,
  nurse_patient_ratio: 4,
}

const PRESETS: { label: string; icon: IconName; knobs: Partial<Knobs> }[] = [
  { label: '20 admissions tonight', icon: 'moon', knobs: { extra_admissions: 20, horizon_hours: 24 } },
  { label: 'Close 8 beds', icon: 'bed', knobs: { beds_delta: -8 } },
  { label: 'Flu surge (1.5×)', icon: 'thermometer', knobs: { admission_rate_multiplier: 1.5, horizon_hours: 72 } },
  { label: 'Mass casualty (+30)', icon: 'siren', knobs: { extra_admissions: 30, horizon_hours: 12 } },
  { label: 'Aggressive discharge', icon: 'zap', knobs: { discharge_boost: 0.6 } },
  { label: 'Open 6 beds', icon: 'trending-up', knobs: { beds_delta: 6 } },
]

const HORIZONS = [
  { label: '12h', value: 12 },
  { label: '24h', value: 24 },
  { label: '48h', value: 48 },
  { label: '72h', value: 72 },
  { label: '7d', value: 168 },
]

export default function CapacitySimulator() {
  const [knobs, setKnobs] = useState<Knobs>(DEFAULT_KNOBS)
  const [result, setResult] = useState<SimResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reqIdRef = useRef(0)

  const runSim = useCallback(async (k: Knobs) => {
    const myReq = ++reqIdRef.current
    setBusy(true)
    try {
      const res = await apiFetch<SimResult>('/capacity-sim/', {
        method: 'POST',
        body: JSON.stringify(k),
      })
      // Ignore stale responses (a newer request has since fired).
      if (myReq === reqIdRef.current) {
        setResult(res)
        setError(null)
      }
    } catch (e: any) {
      if (myReq === reqIdRef.current) setError(e?.message || 'Simulation failed')
    } finally {
      if (myReq === reqIdRef.current) setBusy(false)
    }
  }, [])

  // Debounced re-simulation on knob change.
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => runSim(knobs), 220)
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current) }
  }, [knobs, runSim])

  const setKnob = (key: keyof Knobs, value: number) =>
    setKnobs(prev => ({ ...prev, [key]: value }))

  const applyPreset = (p: Partial<Knobs>) =>
    setKnobs({ ...DEFAULT_KNOBS, ...p })

  const s = result?.summary
  const b = result?.baseline

  const chartData = useMemo(() => (result?.projection ?? []).map(p => ({
    hour: p.hour_offset,
    occupancy: p.projected_occupancy,
    capacity: p.capacity,
    overflow: p.overflow > 0 ? p.projected_occupancy : null,
  })), [result])

  const overflowHours = s?.hours_to_overflow
  const willOverflow = overflowHours !== null && overflowHours !== undefined

  return (
    <div className="capsim">
      <div className="capsim__header">
        <div>
          <h1 className="capsim__title">
            <span className="capsim__title-icon" aria-hidden="true">
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 3v18h18" />
                <path d="M7 14l3-4 3 3 4-6" />
                <circle cx="20" cy="7" r="1.4" fill="currentColor" stroke="none" />
              </svg>
            </span>
            What-If Simulator
          </h1>
          <p className="capsim__subtitle">
            Scenario modeling on Orb's live forecast baseline: surge, bed changes, LOS &amp; discharge.
          </p>
        </div>
        {b && (
          <div className="capsim__baseline-chip" role="status">
            <div><span className="lbl">Census now</span><span className="val">{b.census}</span></div>
            <div><span className="lbl">Beds</span><span className="val">{b.capacity}</span></div>
            <div><span className="lbl">Adm / day</span><span className="val">{b.admission_rate}</span></div>
            {busy && <span className="capsim__busy"><span className="capsim__busy-dot" />simulating</span>}
          </div>
        )}
      </div>

      {error && <div className="capsim__error" role="alert">{error}</div>}

      <div className="capsim__grid">
        {/* ── Controls ── */}
        <div className="capsim__card">
          <h2 className="capsim__card-title">Scenario presets</h2>
          <div className="capsim__presets">
            {PRESETS.map(p => (
              <button key={p.label} className="capsim__preset" onClick={() => applyPreset(p.knobs)}>
                <Icon name={p.icon} size={12} style={{ marginRight: 6 }} />{p.label}
              </button>
            ))}
          </div>

          <h2 className="capsim__card-title">Horizon</h2>
          <div className="capsim__presets">
            {HORIZONS.map(h => (
              <button
                key={h.value}
                className="capsim__preset"
                style={knobs.horizon_hours === h.value
                  ? { borderColor: 'var(--accent-gold)', color: 'var(--text-primary)' } : undefined}
                onClick={() => setKnob('horizon_hours', h.value)}
              >
                {h.label}
              </button>
            ))}
          </div>

          <Knob
            label="Surge admissions" hint="One-off extra admissions across the horizon"
            value={knobs.extra_admissions} display={`+${knobs.extra_admissions}`}
            min={0} max={60} step={1}
            onChange={v => setKnob('extra_admissions', v)}
          />
          <Knob
            label="Bed change" hint="Open (+) or close (–) beds vs current capacity"
            value={knobs.beds_delta} display={`${knobs.beds_delta > 0 ? '+' : ''}${knobs.beds_delta}`}
            min={-16} max={16} step={1}
            onChange={v => setKnob('beds_delta', v)}
          />
          <Knob
            label="Admission-rate ×" hint="1.5× ≈ flu surge, 2× ≈ epidemic"
            value={knobs.admission_rate_multiplier} display={`${knobs.admission_rate_multiplier.toFixed(2)}×`}
            min={0.5} max={3} step={0.1}
            onChange={v => setKnob('admission_rate_multiplier', v)}
          />
          <Knob
            label="Length-of-stay ×" hint=">1 = sicker cohort, slower turnover"
            value={knobs.los_multiplier} display={`${knobs.los_multiplier.toFixed(2)}×`}
            min={0.5} max={2} step={0.1}
            onChange={v => setKnob('los_multiplier', v)}
          />
          <Knob
            label="Discharge boost" hint="Aggressive discharge: % faster than baseline"
            value={knobs.discharge_boost} display={`+${Math.round(knobs.discharge_boost * 100)}%`}
            min={0} max={1} step={0.05}
            onChange={v => setKnob('discharge_boost', v)}
          />
          <Knob
            label="Nurse : patient" hint="Editable staffing ratio (patients per nurse)"
            value={knobs.nurse_patient_ratio} display={`1:${knobs.nurse_patient_ratio}`}
            min={1} max={8} step={1}
            onChange={v => setKnob('nurse_patient_ratio', v)}
          />

          <button className="capsim__reset" onClick={() => setKnobs(DEFAULT_KNOBS)}>
            Reset to baseline
          </button>
        </div>

        {/* ── Results ── */}
        <div className="capsim__results">
          <div className="capsim__stats">
            <Stat
              label="Peak occupancy"
              value={s?.peak_occupancy_pct != null ? `${s.peak_occupancy_pct}%` : '—'}
              sub={s ? `${s.peak_occupancy} beds · +${s.peak_hour_offset}h` : ''}
              tone={peakTone(s?.peak_occupancy_pct)}
            />
            <Stat
              label="Hours to overflow"
              value={willOverflow ? (overflowHours === 0 ? 'Now' : `${overflowHours}h`) : 'Clear'}
              sub={willOverflow ? 'Capacity breached' : 'No overflow in horizon'}
              tone={willOverflow ? (overflowHours! <= 6 ? 'danger' : 'warn') : 'ok'}
            />
            <Stat
              label="Beds short at peak"
              value={s ? `${s.beds_short_at_peak}` : '—'}
              sub="Over effective capacity"
              tone={(s?.beds_short_at_peak ?? 0) > 0 ? 'danger' : 'ok'}
            />
            <Stat
              label="Discharges needed"
              value={s ? `${s.suggested_discharges}` : '—'}
              sub="To avoid overflow"
              tone={(s?.suggested_discharges ?? 0) > 0 ? 'warn' : 'ok'}
            />
            <Stat
              label="Extra nurses"
              value={s ? `${s.staffing_gap_nurses}` : '—'}
              sub={s ? `${s.nurses_required_at_peak} needed at peak` : ''}
              tone={(s?.staffing_gap_nurses ?? 0) > 0 ? 'warn' : 'ok'}
            />
          </div>

          <div className="capsim__chart-card">
            <div className="capsim__chart-head">
              <span className="capsim__chart-title">Projected occupancy vs capacity</span>
              <span className="capsim__legend">
                <span><i style={{ background: 'var(--accent-gold)' }} />Occupancy</span>
                <span><i style={{ background: 'var(--status-danger)' }} />Overflow</span>
                <span><i style={{ background: 'var(--text-muted)' }} />Capacity</span>
              </span>
            </div>
            <div className="capsim__chart-wrap">
              <SizedChart>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 6, right: 12, left: -12, bottom: 4 }}>
                  <defs>
                    <linearGradient id="capsimOcc" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--accent-gold)" stopOpacity={0.5} />
                      <stop offset="100%" stopColor="var(--accent-gold)" stopOpacity={0.04} />
                    </linearGradient>
                    <linearGradient id="capsimOver" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="var(--status-danger)" stopOpacity={0.55} />
                      <stop offset="100%" stopColor="var(--status-danger)" stopOpacity={0.05} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border-glass)" vertical={false} />
                  <XAxis
                    dataKey="hour" stroke="var(--text-muted)" fontSize={11}
                    tickFormatter={(h) => `+${h}h`} tickMargin={8}
                  />
                  <YAxis
                    stroke="var(--text-muted)" fontSize={11}
                    width={46} allowDecimals={false}
                  />
                  <Tooltip content={<SimTooltip />} />
                  {result && (
                    <ReferenceLine
                      y={result.scenario.effective_capacity}
                      // The Y domain is fitted to occupancy only, so whenever
                      // the ward is under capacity the line sat above the axis
                      // and Recharts discarded it: the chart titled "occupancy
                      // vs capacity" drew no capacity. Extend the axis instead.
                      ifOverflow="extendDomain"
                      stroke="var(--text-muted)"
                      strokeDasharray="6 4"
                      label={{
                        value: `Capacity ${result.scenario.effective_capacity}`,
                        position: 'insideTopRight',
                        fill: 'var(--text-secondary)',
                        fontSize: 11,
                      }}
                    />
                  )}
                  <Area
                    type="monotone" dataKey="occupancy" stroke="var(--accent-gold)"
                    strokeWidth={2.2} fill="url(#capsimOcc)" isAnimationActive={false}
                  />
                  <Area
                    type="monotone" dataKey="overflow" stroke="var(--status-danger)"
                    strokeWidth={2} fill="url(#capsimOver)" isAnimationActive={false}
                    connectNulls={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
              </SizedChart>
            </div>
          </div>

          {result && (
            <div className="capsim__card">
              <details className="capsim__assumptions">
                <summary>Model assumptions &amp; citations</summary>
                <p>
                  Baseline census, bed capacity and expected admission rate are drawn live from
                  Orb Forecast (Poisson-fit admission model). The projection is deterministic,
                  no randomness, so the same scenario always yields the same numbers.
                </p>
                <ul>
                  {Object.entries(result.assumptions).map(([k, a]) => (
                    <li key={k}>
                      <code>{k}</code> = {String(a.value)} ({a.source})
                    </li>
                  ))}
                </ul>
              </details>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Sub-components ──
function Knob(props: {
  label: string; hint: string; value: number; display: string
  min: number; max: number; step: number; onChange: (v: number) => void
}) {
  return (
    <div className="capsim__knob">
      <div className="capsim__knob-head">
        <span>
          <span className="capsim__knob-label">{props.label}</span>
          <span className="capsim__knob-hint">{props.hint}</span>
        </span>
        <span className="capsim__knob-value">{props.display}</span>
      </div>
      <input
        className="capsim__range"
        type="range"
        min={props.min} max={props.max} step={props.step}
        value={props.value}
        onChange={e => props.onChange(Number(e.target.value))}
        aria-label={props.label}
      />
    </div>
  )
}

function Stat(props: { label: string; value: string; sub?: string; tone?: 'ok' | 'warn' | 'danger' | 'neutral' }) {
  const cls = props.tone && props.tone !== 'neutral' ? ` capsim__stat--${props.tone}` : ''
  return (
    <div className={`capsim__stat${cls}`}>
      <div className="capsim__stat-label">{props.label}</div>
      <div className="capsim__stat-value">{props.value}</div>
      {props.sub && <div className="capsim__stat-sub">{props.sub}</div>}
    </div>
  )
}

function SimTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null
  const d = payload[0].payload
  const over = d.capacity != null && d.occupancy > d.capacity
  return (
    <div className="capsim__tip">
      <strong>+{d.hour}h from now</strong>
      <div className="row"><span>Occupancy</span><span>{d.occupancy} beds</span></div>
      <div className="row"><span>Capacity</span><span>{d.capacity} beds</span></div>
      {over && (
        <div className="row over">
          <span>Overflow</span><span>+{(d.occupancy - d.capacity).toFixed(1)}</span>
        </div>
      )}
    </div>
  )
}

function peakTone(pct: number | null | undefined): 'ok' | 'warn' | 'danger' | 'neutral' {
  if (pct == null) return 'neutral'
  if (pct >= 100) return 'danger'
  if (pct >= 85) return 'warn'
  return 'ok'
}
