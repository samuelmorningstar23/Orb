/**
 * PulseDetail — Glassmorphic detail overlay for each Pulse section.
 * Slides in over the main panel. Shows expanded data + source attribution.
 */
import './PulseWidget.css'

interface Props {
  view: string
  data: any
  onBack: () => void
}

const SourceBadge = ({ name, url }: { name: string; url: string }) => (
  <div className="pulse-source">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" /><line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
    <span className="pulse-source__text">
      Source: <a href={url} target="_blank" rel="noopener noreferrer" className="pulse-source__link">{name}</a>
    </span>
  </div>
)

const ActionItem = ({ text }: { text: string }) => (
  <div className="pulse-detail__action">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="9 11 12 14 22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
    </svg>
    {text}
  </div>
)

const BackBtn = ({ onClick }: { onClick: () => void }) => (
  <button className="pulse-detail__back" onClick={onClick}>
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="15 18 9 12 15 6" />
    </svg>
  </button>
)

const Row = ({ label, value }: { label: string; value: string | number | undefined }) => (
  <div className="pulse-detail__row">
    <span className="pulse-detail__row-label">{label}</span>
    <span className="pulse-detail__row-value">{value ?? '—'}</span>
  </div>
)

export default function PulseDetail({ view, data, onBack }: Props) {
  if (view === 'weather') {
    const w = data?.weather
    const c = w?.current || {}
    return (
      <div className="pulse-detail">
        <div className="pulse-detail__header">
          <BackBtn onClick={onBack} />
          <span className="pulse-detail__title">Weather Details</span>
        </div>
        <div className="pulse-detail__body">
          <div className="pulse-detail__hero">
            <span className="pulse-detail__hero-value">{Math.round(c.temperature ?? 0)}°C</span>
            <span className="pulse-detail__hero-label">{c.description || 'Unknown'}</span>
            <span className="pulse-detail__hero-sub">Feels like {Math.round(c.feels_like ?? 0)}°C</span>
          </div>
          <Row label="Humidity" value={`${c.humidity ?? 0}%`} />
          <Row label="Wind Speed" value={`${c.wind_speed_kmh ?? 0} km/h`} />
          <Row label="Pressure" value={`${c.pressure_hpa ?? 0} hPa`} />
          <Row label="Coordinates" value={`${w?.location?.lat?.toFixed(3)}, ${w?.location?.lon?.toFixed(3)}`} />

          {w?.forecast?.length > 0 && (
            <>
              <div className="pulse-section-label" style={{ marginTop: 6 }}>7-Day Forecast</div>
              {w.forecast.map((d: any, i: number) => (
                <Row key={i} label={d.day_label || d.date} value={`${Math.round(d.temp_max)}° / ${Math.round(d.temp_min)}° · ${d.description}`} />
              ))}
            </>
          )}

          {w?.clinical_flags?.length > 0 && (
            <>
              <div className="pulse-section-label" style={{ marginTop: 6 }}>Clinical Recommendations</div>
              <div className="pulse-detail__actions">
                {w.clinical_flags.flatMap((f: any) =>
                  (f.recommended_actions || []).map((a: string, j: number) => <ActionItem key={j} text={a} />)
                )}
              </div>
            </>
          )}

          <SourceBadge name="Open-Meteo (ECMWF/DWD)" url="https://open-meteo.com/" />
        </div>
      </div>
    )
  }

  if (view === 'aqi') {
    const a = data?.air_quality || {}
    const p = a.pollutants || {}
    return (
      <div className="pulse-detail">
        <div className="pulse-detail__header">
          <BackBtn onClick={onBack} />
          <span className="pulse-detail__title">Air Quality Details</span>
        </div>
        <div className="pulse-detail__body">
          <div className="pulse-detail__hero">
            <span className="pulse-detail__hero-value">{a.aqi ?? '—'}</span>
            <span className="pulse-detail__hero-label">{a.category}</span>
            <span className="pulse-detail__hero-sub">US Air Quality Index</span>
          </div>
          <Row label="PM2.5" value={p.pm2_5 != null ? `${p.pm2_5.toFixed(1)} µg/m³` : '—'} />
          <Row label="PM10" value={p.pm10 != null ? `${p.pm10.toFixed(1)} µg/m³` : '—'} />
          <Row label="Ozone (O₃)" value={p.o3 != null ? `${p.o3.toFixed(1)} µg/m³` : '—'} />
          <Row label="Nitrogen Dioxide" value={p.no2 != null ? `${p.no2.toFixed(1)} µg/m³` : '—'} />
          <Row label="Carbon Monoxide" value={p.co != null ? `${p.co.toFixed(0)} µg/m³` : '—'} />

          {a.clinical_flags?.length > 0 && (
            <>
              <div className="pulse-section-label" style={{ marginTop: 6 }}>Clinical Recommendations</div>
              <div className="pulse-detail__actions">
                {a.clinical_flags.flatMap((f: any) =>
                  (f.recommended_actions || []).map((act: string, j: number) => <ActionItem key={j} text={act} />)
                )}
              </div>
            </>
          )}

          <SourceBadge name="Open-Meteo Air Quality API" url="https://air-quality-api.open-meteo.com/" />
        </div>
      </div>
    )
  }

  if (view === 'flu') {
    const f = data?.flu_surveillance || {}
    return (
      <div className="pulse-detail">
        <div className="pulse-detail__header">
          <BackBtn onClick={onBack} />
          <span className="pulse-detail__title">Flu Surveillance Details</span>
        </div>
        <div className="pulse-detail__body">
          <div className="pulse-detail__hero">
            <span className="pulse-detail__hero-value">{f.ili_percentage ?? '—'}%</span>
            <span className="pulse-detail__hero-label">Influenza-Like Illness Rate</span>
            <span className="pulse-detail__hero-sub">{(f.activity_level || '').replace('_', ' ')} activity</span>
          </div>
          <Row label="Source" value={f.source || '—'} />
          <Row label="Surveillance Week" value={f.week || '—'} />
          <Row label="Year" value={f.year || '—'} />
          {f.total_patients > 0 && <Row label="Patients Surveyed" value={f.total_patients.toLocaleString()} />}

          {f.clinical_flags?.length > 0 && (
            <>
              <div className="pulse-section-label" style={{ marginTop: 6 }}>Clinical Recommendations</div>
              <div className="pulse-detail__actions">
                {f.clinical_flags.flatMap((fl: any) =>
                  (fl.recommended_actions || []).map((a: string, j: number) => <ActionItem key={j} text={a} />)
                )}
              </div>
            </>
          )}

          <SourceBadge name="CDC FluView / Seasonal Model" url="https://www.cdc.gov/flu/weekly/" />
        </div>
      </div>
    )
  }

  if (view === 'alert') {
    const a = data?._selectedAlert
    if (!a) return null
    return (
      <div className="pulse-detail">
        <div className="pulse-detail__header">
          <BackBtn onClick={onBack} />
          <span className="pulse-detail__title">Alert Details</span>
        </div>
        <div className="pulse-detail__body">
          <div className={`pulse-alert pulse-alert--${a.severity}`} style={{ cursor: 'default' }}>
            <div className="pulse-alert__content">
              <div className="pulse-alert__title">{a.title}</div>
              <div className="pulse-alert__message">{a.message}</div>
            </div>
          </div>
          <Row label="Severity" value={a.severity?.toUpperCase()} />
          <Row label="Type" value={a.type?.replace(/_/g, ' ')} />
          {a.affected_conditions?.length > 0 && (
            <Row label="At-Risk Groups" value={a.affected_conditions.join(', ')} />
          )}
          {a.recommended_actions?.length > 0 && (
            <>
              <div className="pulse-section-label" style={{ marginTop: 6 }}>Recommended Actions</div>
              <div className="pulse-detail__actions">
                {a.recommended_actions.map((act: string, j: number) => <ActionItem key={j} text={act} />)}
              </div>
            </>
          )}
          <SourceBadge name="Orb Pulse Clinical Engine" url="https://open-meteo.com/" />
        </div>
      </div>
    )
  }

  if (view?.startsWith('recall-')) {
    const idx = parseInt(view.split('-')[1])
    const r = data?.drug_recalls?.[idx]
    if (!r) return null
    return (
      <div className="pulse-detail">
        <div className="pulse-detail__header">
          <BackBtn onClick={onBack} />
          <span className="pulse-detail__title">Drug Recall Details</span>
        </div>
        <div className="pulse-detail__body">
          <div className="pulse-detail__recall-full">
            <div className="pulse-detail__recall-product">{r.product}</div>
            {r.classification && <span className="pulse-recall-item__class">{r.classification}</span>}
          </div>
          <div className="pulse-detail__recall-field">
            <span className="pulse-detail__recall-field-label">Reason for Recall</span>
            <span className="pulse-detail__recall-field-value">{r.reason || '—'}</span>
          </div>
          <Row label="Recall Number" value={r.recall_number} />
          <Row label="Status" value={r.status} />
          <Row label="Recalling Firm" value={r.recalling_firm} />
          <Row label="Location" value={[r.city, r.state].filter(Boolean).join(', ') || '—'} />
          <Row label="Report Date" value={r.report_date} />
          <Row label="Initiation Date" value={r.recall_initiation_date} />
          <SourceBadge name="openFDA Drug Enforcement" url="https://open.fda.gov/apis/drug/enforcement/" />
        </div>
      </div>
    )
  }

  return null
}
