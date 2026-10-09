/**
 * Beds, Census view (adt-13a): the daily census of the caller's wards over a
 * period of whole hospital-local days, read from the bed movement record.
 *
 * COUNTS ONLY. Midnight census, admissions, transfers in and out, ends of
 * stay by type, deaths, inpatient days, licensed and available beds,
 * occupancy, average stay and return to intensive care. No patient is named.
 *
 * INSUFFICIENT DATA IS NEVER A ZERO. A day before a ward's bed register was
 * marked complete comes back with no counts and a reason, and the row says
 * 'Insufficient data' with that reason. Totals add the complete days only and
 * say how many were left out.
 *
 * THE SERVER WRITES THE METHOD. Every answer carries how each figure is
 * counted (ruling ADT-P4: the NABH occupancy is the midnight census over
 * available beds, shown beside the licensed-bed and hour-based figures), and
 * this view prints those words as given under 'How these figures are
 * counted'. Hospital totals appear only for an account not tied to one
 * department; a department account is told why they are not shown.
 *
 * Download CSV saves the same census as a spreadsheet file on this computer.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { toAdtError } from '../../../api/adt'
import {
  CENSUS_END_TYPES,
  censusFileName,
  downloadCensusCsv,
  getCensus,
  type Census,
  type CensusDay,
  type CensusParams,
  type CensusTotals,
  type CensusWard,
} from '../../../api/adt/census'
import { formatClinicalDateKey, formatClinicalTime, localDateKey } from '../../../lib/datetime'
import type { AdtViewProps } from './viewRegistry'
import './CensusView.css'

export const viewId = 'census'
export const viewLabel = 'Census'
export const viewOrder = 60
export const viewPermission = 'adt.read'

type Load =
  | { state: 'loading' }
  | { state: 'ready'; census: Census; notice: string | null }
  | { state: 'error'; message: string }

/** The headings for the server's method words, in reading order. */
const METHOD_LABELS: Record<string, string> = {
  days: 'Days',
  insufficient_data: 'Insufficient data',
  midnight_census: 'Midnight census',
  admissions: 'Admissions',
  transfers: 'Transfers',
  ends: 'Ends of stay',
  inpatient_days: 'Inpatient days',
  beds: 'Licensed and available beds',
  occupancy: 'Occupancy',
  alos: 'Average stay',
  return_to_icu: 'Return to intensive care within 48 hours',
  sources: 'Where the figures come from',
}

function methodLabel(key: string): string {
  if (METHOD_LABELS[key]) return METHOD_LABELS[key]
  const words = key.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? 'Not available' : `${value}%`
}

function num(value: number | null | undefined): string {
  return value === null || value === undefined ? '' : String(value)
}

/** "3: 2 Routine discharge, 1 Death", or "0". */
function endsWords(ends: CensusDay['ends'], words: Record<string, string>): string {
  if (!ends) return ''
  const parts = CENSUS_END_TYPES
    .filter((t) => (ends[t] ?? 0) > 0)
    .map((t) => `${ends[t]} ${words[t] ?? t.replace(/_/g, ' ')}`)
  const total = CENSUS_END_TYPES.reduce((n, t) => n + (ends[t] ?? 0), 0)
  return parts.length ? `${total}: ${parts.join(', ')}` : '0'
}

function yesterdayKey(): string {
  return localDateKey(new Date(Date.now() - 24 * 3600 * 1000))
}

export default function CensusView({ onModuleOff }: AdtViewProps) {
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [wardId, setWardId] = useState('')
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [downloading, setDownloading] = useState(false)
  const [downloadNote, setDownloadNote] = useState<string | null>(null)
  const latest = useRef(0)

  // State changes only in the reply's callbacks, never synchronously.
  const read = useCallback((query: CensusParams) => {
    const mine = ++latest.current
    getCensus(query).then(
      (census) => {
        if (mine !== latest.current) return
        setLoad({ state: 'ready', census, notice: null })
        setFrom(census.from)
        setTo(census.to)
      },
      (err: unknown) => {
        if (mine !== latest.current) return
        const failure = toAdtError(err)
        if (failure.isModuleDisabled) {
          onModuleOff()
          return
        }
        const message = failure.isForbidden
          ? 'Your role cannot see the census.'
          : failure.isNotFound
            ? 'That ward is not on your census.'
            : failure.message
        // A refused period keeps the census already on screen, and says why.
        setLoad((prev) => prev.state === 'ready'
          ? { ...prev, notice: message }
          : { state: 'error', message })
      },
    )
  }, [onModuleOff])

  useEffect(() => { read({}) }, [read])

  const show = () => read({ from, to, wardId })

  const download = async () => {
    setDownloading(true)
    setDownloadNote(null)
    try {
      const blob = await downloadCensusCsv({ from, to, wardId })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = censusFileName(from, to)
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      const failure = toAdtError(err)
      if (failure.isModuleDisabled) {
        onModuleOff()
        return
      }
      setDownloadNote(failure.message || 'The file could not be made. Try again.')
    } finally {
      setDownloading(false)
    }
  }

  if (load.state === 'loading') {
    return <p className="bb-status" role="status">Counting the census</p>
  }
  if (load.state === 'error') {
    return (
      <section className="bb-notice bb-notice--error" role="alert">
        <p className="bb-notice__body">{load.message}</p>
        <button type="button" className="bb-btn" onClick={() => read({})}>Try again</button>
      </section>
    )
  }

  const { census, notice } = load
  const words = census.end_type_words ?? {}
  const maxDay = yesterdayKey()

  return (
    <div className="cv">
      <div className="bb-toolbar">
        <div className="cv-controls">
          <label className="bb-field">
            <span className="bb-field__label">From</span>
            <input className="bb-input" type="date" value={from} max={to || maxDay}
              onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label className="bb-field">
            <span className="bb-field__label">To</span>
            <input className="bb-input" type="date" value={to} max={maxDay}
              onChange={(e) => setTo(e.target.value)} />
          </label>
          {census.ward_choices.length > 1 && (
            <label className="bb-field">
              <span className="bb-field__label">Ward</span>
              <select className="bb-select" value={wardId} onChange={(e) => setWardId(e.target.value)}>
                <option value="">All my wards</option>
                {census.ward_choices.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name && w.name !== w.code ? `${w.code}, ${w.name}` : w.code}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button type="button" className="bb-btn" onClick={show}>Show</button>
          <button type="button" className="bb-btn" onClick={() => { void download() }}
            disabled={downloading}>
            {downloading ? 'Preparing the file' : 'Download CSV'}
          </button>
        </div>
        <p className="bb-updated">
          {formatClinicalDateKey(census.from)} to {formatClinicalDateKey(census.to)}, hospital
          days. Counted at {formatClinicalTime(census.generated_at)}.
        </p>
      </div>

      {notice && <p className="bb-note bb-note--warn" role="alert">{notice}</p>}
      {downloadNote && <p className="bb-note bb-note--warn" role="alert">{downloadNote}</p>}

      {census.wards.length === 0 && (
        <section className="bb-empty" aria-labelledby="cv-no-wards">
          <h2 id="cv-no-wards" className="bb-empty__title">No wards to count</h2>
          <p className="bb-empty__body">
            No ward in bed setup belongs to your department yet. A bed manager sets wards and
            beds up under bed setup.
          </p>
        </section>
      )}

      {census.wards.map((ward) => (
        <WardCensus key={ward.id} ward={ward} words={words} />
      ))}

      {census.hospital ? (
        <section className="bb-ward" aria-labelledby="cv-hospital">
          <header className="bb-ward__head">
            <h2 id="cv-hospital" className="bb-ward__title">Whole hospital</h2>
            <p className="bb-ward__dept">Every ward in bed setup, and patients not on a ward</p>
          </header>
          <Totals totals={census.hospital.totals} words={words} icu />
          <DayTable days={census.hospital.days} words={words} icu hospital
            label="The whole hospital, day by day" />
        </section>
      ) : census.hospital_hidden ? (
        <p className="bb-note">{census.hospital_hidden}</p>
      ) : null}

      <details className="cv-methods">
        <summary className="cv-methods__summary">How these figures are counted</summary>
        <dl className="cv-methods__list">
          {Object.entries(census.methods).map(([key, text]) => (
            <div key={key} className="cv-methods__item">
              <dt className="cv-methods__term">{methodLabel(key)}</dt>
              <dd className="cv-methods__text">{text}</dd>
            </div>
          ))}
        </dl>
      </details>
    </div>
  )
}

function WardCensus({ ward, words }: { ward: CensusWard; words: Record<string, string> }) {
  const counted = ward.first_complete_day
    ? `Counted from ${formatClinicalDateKey(ward.first_complete_day)}`
    : 'Not counted yet: its bed register is not marked complete in bed setup'
  return (
    <section className="bb-ward" aria-labelledby={`cv-ward-${ward.id}`}>
      <header className="bb-ward__head">
        <h2 id={`cv-ward-${ward.id}`} className="bb-ward__title">
          {ward.code}
          {ward.name && ward.name !== ward.code ? <span className="bb-ward__name">{ward.name}</span> : null}
        </h2>
        <p className="bb-ward__dept">
          {ward.department}
          {ward.icu ? ', intensive care' : ''}
          {!ward.is_active ? ', switched off in bed setup' : ''}
        </p>
        <p className="bb-ward__dept">{counted}</p>
      </header>
      <Totals totals={ward.totals} words={words} icu={ward.icu} />
      <DayTable days={ward.days} words={words} icu={ward.icu}
        label={`${ward.code}, day by day`} />
    </section>
  )
}

function Totals({ totals, words, icu }: {
  totals: CensusTotals; words: Record<string, string>; icu?: boolean
}) {
  if (totals.status === 'insufficient_data') {
    return (
      <p className="bb-note" data-testid="cv-totals-insufficient">
        Insufficient data for every day of this period, so there are no totals.
      </p>
    )
  }
  return (
    <>
      {totals.status === 'partial' && (
        <p className="bb-note">
          Totals add the {totals.complete_days} complete days. {totals.insufficient_days} days
          have insufficient data and are left out.
        </p>
      )}
      <dl className="cv-totals" aria-label="Totals for the period">
        <Fact term="Average midnight census" value={num(totals.average_midnight_census)} />
        <Fact term="Admissions" value={num(totals.admissions)} />
        <Fact term="Transfers in" value={num(totals.transfers_in)} />
        <Fact term="Transfers out" value={num(totals.transfers_out)} />
        <Fact term="Ends of stay" value={endsWords(totals.ends, words)} />
        <Fact term="Deaths" value={num(totals.deaths)} />
        <Fact term="Inpatient days" value={num(totals.inpatient_days)} />
        <Fact term="Occupancy, NABH (midnight census over available beds)"
          value={pct(totals.occupancy?.nabh)} />
        <Fact term="Midnight census over licensed beds" value={pct(totals.occupancy?.licensed)} />
        <Fact term="Inpatient days over available beds" value={pct(totals.occupancy?.hours)} />
        <Fact term="Average stay"
          value={totals.alos_days === null || totals.alos_days === undefined
            ? 'No stay ended' : `${totals.alos_days} days`} />
        {icu && totals.icu && (
          <>
            <Fact term="Moved out of intensive care" value={num(totals.icu.transfers_out)} />
            <Fact term="Back in intensive care within 48 hours" value={num(totals.icu.returns_48h)} />
            <Fact term="Still inside the 48 hours" value={num(totals.icu.pending)} />
            <Fact term="Return rate" value={pct(totals.icu.return_rate)} />
          </>
        )}
      </dl>
    </>
  )
}

function Fact({ term, value }: { term: string; value: string }) {
  return (
    <div className="cv-fact">
      <dt className="cv-fact__term">{term}</dt>
      <dd className="cv-fact__value">{value}</dd>
    </div>
  )
}

function DayTable({ days, words, icu, hospital, label }: {
  days: CensusDay[]; words: Record<string, string>; icu?: boolean; hospital?: boolean
  label: string
}) {
  const columns = 10 + (hospital ? 1 : 0) + (icu ? 3 : 0)
  return (
    <div className="cv-scroll">
      <table className="cv-table" aria-label={label}>
        <thead>
          <tr>
            <th scope="col">Day</th>
            <th scope="col">Midnight census</th>
            {hospital && <th scope="col">Not on a ward</th>}
            <th scope="col">Admissions</th>
            <th scope="col">Transfers in</th>
            <th scope="col">Transfers out</th>
            <th scope="col">Ends of stay</th>
            <th scope="col">Deaths</th>
            <th scope="col">Inpatient days</th>
            <th scope="col">Licensed beds</th>
            <th scope="col">Available beds</th>
            <th scope="col">Occupancy, NABH</th>
            {icu && <th scope="col">Out of intensive care</th>}
            {icu && <th scope="col">Back within 48 hours</th>}
            {icu && <th scope="col">Still inside 48 hours</th>}
          </tr>
        </thead>
        <tbody>
          {days.map((d) => (
            <tr key={d.date} data-status={d.status}>
              <th scope="row">{formatClinicalDateKey(d.date)}</th>
              {d.status === 'complete' ? (
                <>
                  <td>{num(d.midnight_census)}</td>
                  {hospital && <td>{num(d.not_on_a_ward)}</td>}
                  <td>{num(d.admissions)}</td>
                  <td>{num(d.transfers_in)}</td>
                  <td>{num(d.transfers_out)}</td>
                  <td>{endsWords(d.ends, words)}</td>
                  <td>{num(d.deaths)}</td>
                  <td>{num(d.inpatient_days)}</td>
                  <td>{num(d.licensed_beds)}</td>
                  <td>{num(d.available_beds)}</td>
                  <td>{pct(d.occupancy?.nabh)}</td>
                  {icu && <td>{num(d.icu?.transfers_out)}</td>}
                  {icu && <td>{num(d.icu?.returns_48h)}</td>}
                  {icu && <td>{num(d.icu?.pending)}</td>}
                </>
              ) : (
                <td colSpan={columns} className="cv-table__insufficient">
                  <span className="cv-table__flag">Insufficient data.</span> {d.reason}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
