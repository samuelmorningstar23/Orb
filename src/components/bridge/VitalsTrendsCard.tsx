import { useEffect, useState } from 'react'
import { getVitalsTrends, type VitalTrend } from '../../api/bridgeClient'
import { vitalTemperature, vitalWhole } from '../../lib/vitals'

// NO VERDICT (ruled 2026-10-03). This card used to call each reading "In normal
// range" / "Below normal" / "Above normal" against portal-only bands, painted in
// status fill tokens. SBP 90 (NEWS2 3 points, red on the ward) read normal, and
// a patient on oxygen at NEWS2 6 read normal on every row. It now shows the value
// as charted, when it was measured and a neutral trend, and says who reviews it.
const CARE_TEAM_LINE =
  'Your care team reviews every reading in the context of your condition. ' +
  'If you feel worse, tell your nurse straight away.'
const TREND_INK = 'var(--text-secondary)'

/** The value as charted, the same number the ward prints. Temperature keeps its
 *  tenth: the server sends 37.0, which JSON hands to JS as the number 37. */
function fmtValue(key: string, v: number | null): string {
  return key === 'temperature' ? vitalTemperature(v) : vitalWhole(v)
}
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

/** When the reading was taken: "Today 14:32" same-day, else "Aug 9, 14:32".
 *  Missing/unparseable timestamp → '' — never fabricate a time. */
function fmtMeasuredAt(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const now = new Date()
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
  return sameDay ? `Today ${hm}` : `${MONTHS[d.getMonth()]} ${d.getDate()}, ${hm}`
}

/** A compact sparkline for one vital's recent readings. */
function Sparkline({ series, color }: { series: number[]; color: string }) {
  if (!series || series.length < 2) {
    return <div style={{ height: 28, fontSize: '0.7rem', color: 'var(--text-muted)', display: 'flex', alignItems: 'center' }}>—</div>
  }
  const w = 96, h = 28, pad = 2
  const min = Math.min(...series), max = Math.max(...series)
  const span = max - min || 1
  const pts = series.map((v, i) => {
    const x = pad + (i / (series.length - 1)) * (w - pad * 2)
    const y = h - pad - ((v - min) / span) * (h - pad * 2)
    return `${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  return (
    <svg width={w} height={h} style={{ display: 'block' }} aria-hidden="true">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** Patient-facing vitals: the value as charted, when it was measured and a
 *  neutral trend sparkline. No verdict and no status colour (see the note above). */
export function VitalsTrendsCard() {
  const [trends, setTrends] = useState<Record<string, VitalTrend> | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    getVitalsTrends()
      .then(d => { if (alive) setTrends(d.trends) })
      .catch(e => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [])

  const order = ['heart_rate', 'systolic_bp', 'spo2', 'temperature', 'respiratory_rate']

  return (
    <div className="bridge__card glass-card">
      <div className="bridge__card-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
        </svg>
      </div>
      <h2 className="bridge__card-title">Your Vitals &amp; Trends</h2>

      {error && <p className="bridge__card-text">Unable to load your vitals right now.</p>}
      {!error && !trends && <p className="bridge__card-text">Loading…</p>}

      {trends && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 4 }}>
          {order.filter(k => trends[k]).map(k => {
            const t = trends[k]
            const measured = fmtMeasuredAt(t.measured_at)
            return (
              <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>{t.label}</div>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                    <span style={{ fontSize: '1.25rem', fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
                      {fmtValue(k, t.current)}
                    </span>
                    <span style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>{t.unit}</span>
                  </div>
                  <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                    {t.current === null ? 'No recent reading' : measured}
                  </div>
                </div>
                <Sparkline series={t.series} color={TREND_INK} />
              </div>
            )
          })}
          <p style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: 2 }}>
            {CARE_TEAM_LINE}
          </p>
        </div>
      )}
    </div>
  )
}
