/**
 * Front Desk, Day summary (opd-18): the front office's day in counts.
 *
 * For one hospital day (today unless another is chosen) and the desk's own
 * clinic (every clinic for an account with no department, as the server
 * derives it): new against returning registrations, visits by clinic and
 * doctor, the median and longest wait to be called in, left without being
 * seen, no-shows, and patients admitted from outpatients. Export CSV writes the
 * same figures to a file on this computer.
 *
 * COUNTS ONLY. The server sends no patient's name, UHID or visit, so this
 * screen and its CSV name nobody. A wait nobody was measured for reads "Not
 * measured", never 0.
 *
 * Shown only while the 'registration' feature is on (the panel discovers this
 * file through ./viewRegistry.ts) and to registration.read holders. The server
 * answers 404 while outpatients (ORB_OPD) is off, which this screen says in
 * words rather than hiding the desk's other sections.
 */
import { useCallback, useEffect, useState } from 'react'

import {
  dailyMisCsv,
  fetchDailyMis,
  minutesText,
  type DailyMis,
} from '../../../api/frontOfficeMis'
import { featureOffOf } from '../../../api/opdBoards'
import { formatClinicalDateKey, formatClinicalDateTime, localDateKey } from '../../../lib/datetime'
import type { FrontDeskViewProps } from './viewRegistry'
import './DaySummaryView.css'

export const viewId = 'day-summary'
export const viewLabel = 'Day summary'
export const viewOrder = 40
export const viewPermission = 'registration.read'

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'The day summary could not be loaded.'
}

/** Save `text` as a file on this computer. Nothing leaves the building. */
function saveCsv(filename: string, text: string): void {
  const blob = new Blob([text], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

function Tile({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="ds-tile">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  )
}

export default function DaySummaryView({ scopedDepartment, onFeatureOff }: FrontDeskViewProps) {
  const today = localDateKey()
  /* '' asks the server for its own today (the hospital's day). */
  const [day, setDay] = useState('')
  const [mis, setMis] = useState<DailyMis | null>(null)
  const [loading, setLoading] = useState(true)
  const [opdOff, setOpdOff] = useState(false)
  const [error, setError] = useState('')

  const load = useCallback(async (which: string) => {
    setLoading(true)
    setError('')
    try {
      const answer = await fetchDailyMis(which || undefined)
      setMis(answer)
      setOpdOff(false)
    } catch (err) {
      const feature = featureOffOf(err)
      if (feature === 'registration') {
        onFeatureOff()
      } else if (feature) {
        setOpdOff(true)
      } else {
        setError(errorText(err))
      }
    } finally {
      setLoading(false)
    }
  }, [onFeatureOff])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the summary arriving from the server.
  useEffect(() => { void load(day) }, [load, day])

  if (opdOff) {
    return (
      <div className="fd-card" role="status">
        <p className="fd-empty">
          The day summary counts outpatient visits, so it needs Outpatients switched on for
          this site. An administrator can switch it on.
        </p>
      </div>
    )
  }

  const clinic = mis ? (mis.department ?? 'All clinics') : (scopedDepartment ?? 'All clinics')
  const exportCsv = () => {
    if (!mis) return
    const suffix = mis.department ? `-${mis.department.replace(/[^A-Za-z0-9]+/g, '-').toLowerCase()}` : ''
    saveCsv(`day-summary-${mis.date}${suffix}.csv`, dailyMisCsv(mis))
  }

  return (
    <div className="ds-view">
      <section className="fd-card ds-controls" aria-labelledby="ds-title">
        <h3 className="fd-card__title" id="ds-title">
          Day summary{mis ? `, ${formatClinicalDateKey(mis.date)}` : ''}
        </h3>
        <p className="fd-hint">
          {clinic}. Counts only: no patient is named here or in the export.
          {mis ? ` Made ${formatClinicalDateTime(mis.generated_at)}.` : ''}
        </p>
        <div className="fd-field">
          <label htmlFor="ds-day">Day</label>
          <input
            id="ds-day" type="date" max={today} value={day || mis?.date || today}
            onChange={e => setDay(e.target.value)}
          />
        </div>
        <div className="ds-controls__actions">
          <button type="button" className="fd-btn" disabled={loading}
            onClick={() => { void load(day) }}>
            Refresh
          </button>
          <button type="button" className="fd-btn fd-btn--primary" disabled={!mis || loading}
            onClick={exportCsv}>
            Export CSV
          </button>
        </div>
      </section>

      {error && <p className="fd-error" role="alert">{error}</p>}
      {!mis && loading && <p className="fd-empty">Loading the day summary…</p>}

      {mis && (
        <>
          <dl className="ds-tiles" aria-label="The day in numbers">
            <Tile label="New registrations" value={mis.registrations.new} />
            <Tile label="Returning patients" value={mis.registrations.returning} />
            <Tile label="Visits" value={mis.visits.total} />
            <Tile label="Waiting now" value={mis.waits.still_waiting} />
            <Tile label="Median wait to be called" value={minutesText(mis.waits.median_minutes)} />
            <Tile label="Longest wait to be called" value={minutesText(mis.waits.longest_minutes)} />
            <Tile label="Left without being seen" value={mis.left_without_being_seen} />
            <Tile label="No-shows" value={mis.no_shows.available ? mis.no_shows.total : 'Not available'} />
            <Tile label="Admitted from outpatients" value={mis.conversions} />
          </dl>
          {mis.registrations.not_known > 0 && (
            <p className="fd-hint">
              {mis.registrations.not_known === 1
                ? '1 patient has no registration time on record and is counted as neither new nor returning.'
                : `${mis.registrations.not_known} patients have no registration time on record and are counted as neither new nor returning.`}
            </p>
          )}

          <section className="fd-card" aria-labelledby="ds-clinics-title">
            <h3 className="fd-card__title" id="ds-clinics-title">By clinic and doctor</h3>
            {mis.clinics.length === 0 ? (
              <p className="fd-empty">No outpatient visit was registered on this day.</p>
            ) : (
              <div className="fd-table-scroll">
                <table className="fd-table">
                  <thead>
                    <tr>
                      <th scope="col">Clinic or doctor</th>
                      <th scope="col" className="ds-num">Visits</th>
                      <th scope="col" className="ds-num">Completed</th>
                      <th scope="col" className="ds-num">Left without being seen</th>
                      <th scope="col" className="ds-num">Cancelled</th>
                      <th scope="col" className="ds-num">Admitted</th>
                      <th scope="col" className="ds-num">Median wait</th>
                      <th scope="col" className="ds-num">Longest wait</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mis.clinics.flatMap(c => [
                      <tr key={`clinic-${c.department}`} className="ds-clinic-row">
                        <th scope="row">{c.department}</th>
                        <td className="ds-num">{c.total}</td>
                        <td className="ds-num">{c.completed}</td>
                        <td className="ds-num">{c.left_without_being_seen}</td>
                        <td className="ds-num">{c.cancelled}</td>
                        <td className="ds-num">{c.admitted}</td>
                        <td className="ds-num">{minutesText(c.wait.median_minutes)}</td>
                        <td className="ds-num">{minutesText(c.wait.longest_minutes)}</td>
                      </tr>,
                      ...c.doctors.map(d => (
                        <tr key={`doctor-${c.department}-${d.doctor_id ?? 'none'}-${d.doctor_name}`}>
                          <td>{d.doctor_name}</td>
                          <td className="ds-num">{d.total}</td>
                          <td className="ds-num">{d.completed}</td>
                          <td className="ds-num">{d.left_without_being_seen}</td>
                          <td className="ds-num">{d.cancelled}</td>
                          <td className="ds-num">{d.admitted}</td>
                          <td className="ds-num">{minutesText(d.wait.median_minutes)}</td>
                          <td className="ds-num">{minutesText(d.wait.longest_minutes)}</td>
                        </tr>
                      )),
                    ])}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <div className="ds-lists">
            <section className="fd-card" aria-labelledby="ds-types-title">
              <h3 className="fd-card__title" id="ds-types-title">Visits by type</h3>
              <ul className="ds-list">
                {mis.visits.by_type.map(t => (
                  <li key={t.code}><span>{t.label}</span><span className="ds-num">{t.count}</span></li>
                ))}
              </ul>
            </section>
            <section className="fd-card" aria-labelledby="ds-cancels-title">
              <h3 className="fd-card__title" id="ds-cancels-title">Cancelled visits by reason</h3>
              <ul className="ds-list">
                {mis.cancellations.map(c => (
                  <li key={c.code}><span>{c.label}</span><span className="ds-num">{c.count}</span></li>
                ))}
              </ul>
            </section>
            <section className="fd-card" aria-labelledby="ds-noshow-title">
              <h3 className="fd-card__title" id="ds-noshow-title">Appointments not attended</h3>
              {mis.no_shows.available ? (
                <ul className="ds-list">
                  <li><span>Marked by the desk</span><span className="ds-num">{mis.no_shows.recorded}</span></li>
                  <li>
                    <span>Not checked in by the end of their clinic</span>
                    <span className="ds-num">{mis.no_shows.computed}</span>
                  </li>
                </ul>
              ) : (
                <p className="fd-hint">Appointments with a doctor and time are not set up on this site yet.</p>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  )
}
