import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../api/client'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import { useClinicalSwitches } from '../../shellConfig'
import { formatClinicalClock, formatClinicalTime } from '../../lib/datetime'
import './CommandCenter.css'

// ── Snapshot shape (mirrors server/services/command_center_service.house_snapshot) ──
interface WardCensus { ward: string; occupied: number; capacity: number; occupancy_pct: number }
/** `not_scored`: admitted with no observation on record (counted apart, never as stable). */
interface HeatCell { ward: string; critical: number; elevated: number; stable: number; not_scored?: number }
/** `news2_complete` false: an incomplete set, so the number is a minimum. */
interface Deterioration { patient_id: string; name: string | null; ward: string | null; bed: string | null; level: string; news2: number | null; news2_complete?: boolean | null }
interface ORCase { id: string; procedure_name: string | null; operating_room: string | null; scheduled_time: string | null; status: string | null; patient_name: string | null; ward: string | null }
/** A facility of the hospital group (multisite-12). */
interface CmdFacility { id: number; code: string | null; name: string | null }
/** One facility's figures in the group view, from the same patient list as the group's. */
interface FacilityTile {
  facility: CmdFacility | null
  total_admitted: number
  total_capacity: number
  occupancy_pct: number
  critical: number
  elevated: number
  stable: number
  not_scored: number
}

/** The facility in words, for the header, the selector and the tiles. */
function facilityName(facility: CmdFacility | null | undefined): string {
  if (!facility) return 'Facility not on record'
  const name = facility.name || facility.code || `Facility ${facility.id}`
  return facility.code && facility.name ? `${name} (${facility.code})` : name
}

interface HouseSnapshot {
  generated_at: string
  census: { total_admitted: number; total_capacity: number; occupancy_pct: number; by_ward: WardCensus[] }
  /** Banded from each patient's latest vitals, as the Vigil board is.
   *  `not_scored`: admitted with no observation on record. */
  acuity: { critical: number; elevated: number; stable: number; not_scored?: number; heatmap: HeatCell[] }
  deteriorations: Deterioration[]
  bundles: { active: number; compliant: number; compliance_pct: number }
  forecast: { projected_admissions_24h: number; projected_occupancy: number; projected_occupancy_pct?: number; current_occupancy?: number; total_capacity?: number }
  downtime_readiness: { patients_with_recent_snapshot: number; total: number; pct: number; oldest_snapshot_age_min: number | null }
  or_today: { count: number; cases: ORCase[] }
  // Present only for a ward-scoped viewer: the server filters the named list to
  // their department and sends the house-wide count beside it.
  deteriorations_scope?: string | null
  deteriorations_house_count?: number
  /** False with ORB_SCORING_ENABLED off (the Orb Ward Record): every patient
   *  is "not scored" and the highest-scores list is empty. */
  scoring_enabled?: boolean
  // Present only with more than one facility on record (multisite-12): which
  // facility the figures are about (null for the group view), the facilities
  // this reader may choose, and for a group-wide reader one tile per facility.
  facility?: CmdFacility | null
  facility_view?: 'facility' | 'group' | 'no_facility'
  facilities?: CmdFacility[]
  facility_tiles?: FacilityTile[]
  unplaced_capacity?: number
}

const REFRESH_MS = 20_000

function pctTone(pct: number): 'ok' | 'warn' | 'danger' {
  if (pct >= 90) return 'danger'
  if (pct >= 75) return 'warn'
  return 'ok'
}

function complianceTone(pct: number): 'ok' | 'warn' | 'danger' {
  if (pct >= 90) return 'ok'
  if (pct >= 70) return 'warn'
  return 'danger'
}

function readinessTone(pct: number): 'ok' | 'warn' | 'danger' {
  if (pct >= 80) return 'ok'
  if (pct >= 50) return 'warn'
  return 'danger'
}

/** Heat intensity 0..1 for a ward cell weighted by acuity severity. */
function heatIntensity(cell: HeatCell): number {
  const load = cell.critical * 3 + cell.elevated * 1.5 + cell.stable * 0.4
  // Normalize against a plausible busy-ward ceiling so colors read sensibly.
  return Math.max(0, Math.min(1, load / 12))
}

function ageLabel(min: number | null): string {
  if (min == null) return '—'
  if (min < 60) return `${Math.round(min)}m`
  if (min < 60 * 24) return `${(min / 60).toFixed(1)}h`
  return `${(min / 1440).toFixed(1)}d`
}

export default function CommandCenter() {
  const { scoringEnabled } = useClinicalSwitches()
  const [snap, setSnap] = useState<HouseSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [capturing, setCapturing] = useState(false)
  const [captureMsg, setCaptureMsg] = useState<string | null>(null)
  const [captureErr, setCaptureErr] = useState<string | null>(null)
  const captureMsgTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // multisite-12: the facility a group-wide reader narrowed to (null: the group).
  const [facilityId, setFacilityId] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      const path = facilityId === null ? '/command-center/' : `/command-center/?facility=${facilityId}`
      const data = await apiFetch<HouseSnapshot>(path)
      setSnap(data)
      setError(null)
      setLastUpdated(new Date())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load command center')
    } finally {
      setLoading(false)
    }
  }, [facilityId])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    load()
    return () => {
      if (captureMsgTimer.current) clearTimeout(captureMsgTimer.current)
    }
  }, [load])
  // Paused while another panel or browser tab is in front; refreshes on return.
  useVisiblePolling(load, REFRESH_MS)

  const captureAll = useCallback(async () => {
    setCapturing(true)
    setCaptureErr(null)
    setCaptureMsg(null)
    if (captureMsgTimer.current) clearTimeout(captureMsgTimer.current)
    try {
      const res = await apiFetch<{ count: number; total_admitted: number }>(
        '/downtime/capture-all',
        { method: 'POST' },
      )
      setCaptureMsg(`${res.count} snapshot${res.count === 1 ? '' : 's'} captured`)
      captureMsgTimer.current = setTimeout(() => setCaptureMsg(null), 6000)
      // Refresh the KPI so the readiness figures reflect the new snapshots.
      await load()
    } catch (e) {
      setCaptureErr(e instanceof Error ? e.message : 'Capture failed')
    } finally {
      setCapturing(false)
    }
  }, [load])

  if (loading && !snap) {
    return <div className="cmd-loading">Assembling house-wide operating picture…</div>
  }
  if (error && !snap) {
    return (
      <div className="cmd-error" role="alert">
        <p>Command Center unavailable.</p>
        <p className="cmd-error__detail">{error}</p>
        <button onClick={load}>Retry</button>
      </div>
    )
  }
  if (!snap) return null

  const { census, acuity, deteriorations, bundles, forecast, downtime_readiness, or_today } = snap
  // A scoped list must say so: an empty one would otherwise claim the whole house is stable.
  const scope = snap.deteriorations_scope || null
  const houseCount = snap.deteriorations_house_count ?? null
  // Scoring switched off (the Orb Ward Record): no band exists, so the critical
  // count, the acuity heatmap and the highest-scores list are not drawn. Either
  // the shell's switch or the server's own flag turns them off.
  const scoring = scoringEnabled && snap.scoring_enabled !== false
  // Facilities (multisite-12): only with more than one on record. A reader who
  // may see more than one facility gets a selector; everyone is told which
  // facility the figures are about.
  const facilityOptions = snap.facilities ?? []
  const canChooseFacility = facilityOptions.length > 1
  const context = snap.facility_view === 'group'
    ? 'Every facility of the group on one screen'
    : snap.facility_view === 'no_facility'
      ? 'Your department is not listed at any facility, so there is nothing to count'
      : snap.facility
        ? `${facilityName(snap.facility)} on one screen`
        : 'House-wide operations on one screen'
  const houseWords = snap.facility ? `at ${facilityName(snap.facility)}` : 'house-wide'

  return (
    <div className="cmd">
      {/* The shared page header (ClinicianShell.css). An h2: the topbar already
          carries the page's h1. */}
      <header className="orb-home__header">
        <div>
          <h2 className="orb-home__title">Command Center</h2>
          <p className="orb-home__context">{context}</p>
        </div>
        <div className="cmd__meta">
          {canChooseFacility && (
            <label className="cmd__facility">
              <span>Facility</span>
              <select
                aria-label="Facility"
                style={{ minHeight: 'var(--tap-min)' }}
                value={facilityId === null ? '' : String(facilityId)}
                onChange={(e) => setFacilityId(e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">All facilities</option>
                {facilityOptions.map(f => (
                  <option key={f.id} value={String(f.id)}>{facilityName(f)}</option>
                ))}
              </select>
            </label>
          )}
          {error && <span className="cmd__stale" title={error}>reconnecting…</span>}
          <span className="cmd__updated">
            {lastUpdated ? `Updated ${formatClinicalTime(lastUpdated, { seconds: true })}` : ''}
          </span>
          <span className="cmd__live"><span className="cmd__live-dot" aria-hidden="true" />LIVE</span>
        </div>
      </header>

      {/* ── KPI ROW ─────────────────────────────────────────────── */}
      <section className="cmd__kpis" aria-label="Key performance indicators">
        <div className={`cmd-kpi cmd-kpi--${pctTone(census.occupancy_pct)}`}>
          <span className="cmd-kpi__label">Census / Occupancy</span>
          <span className="cmd-kpi__value">{census.total_admitted}<span className="cmd-kpi__unit">/{census.total_capacity}</span></span>
          <div className="cmd-kpi__bar"><span style={{ width: `${Math.min(100, census.occupancy_pct)}%` }} /></div>
          <span className="cmd-kpi__foot">{census.occupancy_pct}% beds occupied</span>
        </div>

        {scoring && <div className={`cmd-kpi ${acuity.critical > 0 ? 'cmd-kpi--danger' : 'cmd-kpi--ok'}`}>
          <span className="cmd-kpi__label">Critical Patients</span>
          {/* A count needs its denominator: "3 of 24 admitted", not a bare 3. */}
          <span className="cmd-kpi__value">{acuity.critical}<span className="cmd-kpi__unit"> of {census.total_admitted} admitted</span></span>
          <span className="cmd-kpi__foot">
            {acuity.elevated} elevated · {acuity.stable} stable
            {acuity.not_scored ? ` · ${acuity.not_scored} not scored` : ''}
          </span>
        </div>}

        <div className={`cmd-kpi cmd-kpi--${complianceTone(bundles.compliance_pct)}`}>
          <span className="cmd-kpi__label">Sepsis Bundle Compliance</span>
          <span className="cmd-kpi__value">{bundles.compliance_pct}<span className="cmd-kpi__unit">%</span></span>
          <span className="cmd-kpi__foot">{bundles.compliant}/{bundles.active} bundles on track</span>
        </div>

        <div className={`cmd-kpi cmd-kpi--${readinessTone(downtime_readiness.pct)}`}>
          <span className="cmd-kpi__label">Downtime Readiness</span>
          <span className="cmd-kpi__value">{downtime_readiness.pct}<span className="cmd-kpi__unit">%</span></span>
          <div className="cmd-kpi__bar"><span style={{ width: `${Math.min(100, downtime_readiness.pct)}%` }} /></div>
          <span className="cmd-kpi__foot">{downtime_readiness.patients_with_recent_snapshot}/{downtime_readiness.total} snapshots · oldest {ageLabel(downtime_readiness.oldest_snapshot_age_min)}</span>
          <div className="cmd-kpi__action">
            <button
              type="button"
              className="cmd-kpi__btn"
              onClick={captureAll}
              disabled={capturing}
              aria-busy={capturing}
            >
              {capturing && <span className="cmd-kpi__spinner" aria-hidden="true" />}
              {capturing ? 'Capturing…' : 'Capture all'}
            </button>
            {captureMsg && <span className="cmd-kpi__note cmd-kpi__note--ok" role="status">{captureMsg}</span>}
            {captureErr && <span className="cmd-kpi__note cmd-kpi__note--err" role="alert">{captureErr}</span>}
          </div>
        </div>
      </section>

      {/* ── FACILITY TILES (a group-wide reader, multisite-12) ─────────── */}
      {snap.facility_tiles && snap.facility_tiles.length > 0 && (
        <section className="cmd__kpis" aria-label="Facilities of the group">
          {snap.facility_tiles.map(tile => (
            <div
              key={tile.facility?.id ?? facilityName(tile.facility)}
              className={`cmd-kpi cmd-kpi--${pctTone(tile.occupancy_pct)}`}
            >
              <span className="cmd-kpi__label">{facilityName(tile.facility)}</span>
              <span className="cmd-kpi__value">{tile.total_admitted}<span className="cmd-kpi__unit">/{tile.total_capacity}</span></span>
              <div className="cmd-kpi__bar"><span style={{ width: `${Math.min(100, tile.occupancy_pct)}%` }} /></div>
              <span className="cmd-kpi__foot">
                {tile.occupancy_pct}% beds occupied
                {scoring ? ` · ${tile.critical} critical · ${tile.elevated} elevated` : ''}
              </span>
            </div>
          ))}
          {(snap.unplaced_capacity ?? 0) > 0 && (
            <div className="cmd-kpi cmd-kpi--warn">
              <span className="cmd-kpi__label">Beds at no facility</span>
              <span className="cmd-kpi__value">{snap.unplaced_capacity}</span>
              <span className="cmd-kpi__foot">
                Beds on wards that Bed setup places at no facility. They count in the group total only.
              </span>
            </div>
          )}
        </section>
      )}

      <div className="cmd__grid">
        {/* ── ACUITY HEATMAP ───────────────────────────────────── */}
        {scoring && <section className="cmd-card cmd-card--heatmap">
          <header className="cmd-card__head">
            <h2>Ward Acuity Heatmap</h2>
            <div className="cmd-legend">
              <span><i className="cmd-dot cmd-dot--critical" />Critical</span>
              <span><i className="cmd-dot cmd-dot--elevated" />Elevated</span>
              <span><i className="cmd-dot cmd-dot--stable" />Stable</span>
              <span><i className="cmd-dot cmd-dot--unscored" />Not scored</span>
            </div>
          </header>
          <div className="cmd-heat__scroll">
            <div className="cmd-heat">
              {acuity.heatmap.map(cell => {
                const wardCensus = census.by_ward.find(w => w.ward === cell.ward)
                const intensity = heatIntensity(cell)
                // The counts were told apart by colour alone; each now carries a
                // word, and the cell reads as one sentence to a screen reader.
                // A patient with no observation on record is counted apart,
                // so the parts still add up to the beds occupied.
                const unscored = cell.not_scored ?? 0
                const summary = `${cell.ward}: ${cell.critical} critical, ${cell.elevated} elevated, ${cell.stable} stable`
                  + (unscored ? `, ${unscored} not scored` : '')
                  + (wardCensus ? `, ${wardCensus.occupied} of ${wardCensus.capacity} beds occupied` : '')
                return (
                  <div
                    key={cell.ward}
                    className={`cmd-heat__cell ${cell.critical > 0 ? 'cmd-heat__cell--crit' : ''}`}
                    style={{ '--heat': intensity } as React.CSSProperties}
                    title={summary}
                    role="group"
                    aria-label={summary}
                  >
                    <span className="cmd-heat__ward">{cell.ward}</span>
                    <span className="cmd-heat__occ">
                      {wardCensus ? `${wardCensus.occupied}/${wardCensus.capacity}` : '—'}
                    </span>
                    <div className="cmd-heat__bars">
                      <span className="cmd-heat__seg cmd-heat__seg--critical" style={{ flexGrow: cell.critical || 0 }} />
                      <span className="cmd-heat__seg cmd-heat__seg--elevated" style={{ flexGrow: cell.elevated || 0 }} />
                      <span className="cmd-heat__seg cmd-heat__seg--stable" style={{ flexGrow: cell.stable || 0 }} />
                      <span className="cmd-heat__seg cmd-heat__seg--unscored" style={{ flexGrow: unscored }} />
                    </div>
                    <div className="cmd-heat__counts" aria-hidden="true">
                      <b>{cell.critical} crit</b><span>{cell.elevated} elev</span><i>{cell.stable} stable</i>
                      {unscored > 0 && <em>{unscored} not scored</em>}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        </section>}

        {/* ── ACTIVE DETERIORATIONS ────────────────────────────── */}
        {scoring && <section className="cmd-card cmd-card--deteriorations">
          <header className="cmd-card__head">
            <div className="cmd-legend" style={{ alignItems: 'baseline', gap: '0.5rem' }}>
              <h2>Highest NEWS2 scores</h2>
              {scope && <span>{scope} only</span>}
            </div>
            <span className="cmd-count">
              {scope && houseCount != null
                ? `${deteriorations.length} of ${houseCount} ${houseWords}`
                : deteriorations.length}
            </span>
          </header>
          <div className="cmd-detlist">
            {deteriorations.length === 0 ? (
              <div className="cmd-empty">
                {scope ? `No elevated or critical NEWS2 scores in ${scope}.` : 'No elevated or critical NEWS2 scores. House is stable.'}
              </div>
            ) : (
              deteriorations.map(d => (
                <div key={d.patient_id} className={`cmd-det cmd-det--${d.level}`}>
                  <span className="cmd-det__stripe" aria-hidden="true" />
                  <div className="cmd-det__main">
                    <span className="cmd-det__name">{d.name || d.patient_id}</span>
                    <span className="cmd-det__loc">{[d.ward, d.bed].filter(Boolean).join(' · ') || '—'}</span>
                  </div>
                  <div className="cmd-det__right">
                    <span className={`cmd-det__badge cmd-det__badge--${d.level}`}>{d.level}</span>
                    <span className="cmd-det__news2">{d.news2 != null ? `NEWS2 ${d.news2_complete === false ? '≥' : ''}${d.news2}` : 'NEWS2 not recorded'}</span>
                  </div>
                </div>
              ))
            )}
          </div>
        </section>}

        {/* ── FORECAST STRIP ───────────────────────────────────── */}
        <section className="cmd-card cmd-card--forecast">
          <header className="cmd-card__head"><h2>Capacity Forecast · 24h</h2></header>
          <div className="cmd-forecast">
            <div className="cmd-forecast__stat">
              <span className="cmd-forecast__val">+{forecast.projected_admissions_24h}</span>
              <span className="cmd-forecast__lbl">projected admissions</span>
            </div>
            <div className="cmd-forecast__stat">
              <span className="cmd-forecast__val">{forecast.projected_occupancy}<span className="cmd-forecast__unit">/{forecast.total_capacity ?? census.total_capacity}</span></span>
              <span className="cmd-forecast__lbl">projected occupancy</span>
            </div>
            <div className="cmd-forecast__stat">
              <span className={`cmd-forecast__val cmd-tone--${pctTone(forecast.projected_occupancy_pct ?? census.occupancy_pct)}`}>
                {(forecast.projected_occupancy_pct ?? census.occupancy_pct)}%
              </span>
              <span className="cmd-forecast__lbl">projected utilization</span>
            </div>
          </div>
        </section>

        {/* ── OR TODAY ─────────────────────────────────────────── */}
        <section className="cmd-card cmd-card--or">
          <header className="cmd-card__head">
            <h2>Operating Room · Today</h2>
            <span className="cmd-count">{or_today.count}</span>
          </header>
          <div className="cmd-or__scroll">
            {or_today.count === 0 ? (
              <div className="cmd-empty">No cases scheduled today.</div>
            ) : (
              <table className="cmd-or">
                <thead>
                  <tr><th>Time</th><th>Procedure</th><th>Patient</th><th>OR</th><th>Status</th></tr>
                </thead>
                <tbody>
                  {or_today.cases.map(c => (
                    <tr key={c.id}>
                      <td className="cmd-or__time">{formatClinicalClock(c.scheduled_time) || '—'}</td>
                      <td>{c.procedure_name || '—'}</td>
                      <td>{c.patient_name || '—'}</td>
                      <td>{c.operating_room || '—'}</td>
                      <td><span className={`cmd-or__status cmd-or__status--${(c.status || 'scheduled').split('_')[0]}`}>{(c.status || 'scheduled').replace(/_/g, ' ')}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
