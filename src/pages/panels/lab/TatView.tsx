/**
 * Turnaround times: how long the laboratory takes, and what is late now
 * (lis-18).
 *
 * FOR A PERIOD of whole hospital days (the last seven unless others are
 * chosen, at most 92):
 *   * how many tests were reported, the median and 90th percentile time from
 *     receipt to report, and the share reported within the test's own target;
 *   * the same by section and priority, and day by day (days split at midnight
 *     hospital time);
 *   * the time from a critical result to its acknowledgement (the ward's
 *     read-back or the laboratory's logged call).
 * NOW: the tests in the laboratory past their target (STAT first) and the open
 * exceptions.
 *
 * A TARGET IS THE PATHOLOGIST'S, set per test and priority in Setup; none ships.
 * A test without one says 'No target', never 0% or 100%. A figure the server
 * cannot state says 'Not recorded'. Simulated results are left out of every
 * figure and the screen says how many.
 *
 * The read names patients (the overdue list) and is audited, so it loads when
 * the view opens and when asked; nothing polls. The CSV export is audited too
 * and carries no patient name or UHID.
 */
import { useEffect, useState } from 'react'

import { toLabError } from '../../../api/lab'
import { formatAccession } from '../../../api/lab/specimens'
import {
  exportLabTat,
  getLabTat,
  minutesText,
  type TatDashboard,
  type TatFigures,
  type TatPeriod,
} from '../../../api/lab/tat'
import { formatClinicalDateKey, formatClinicalDateTime, localDateKey } from '../../../lib/datetime'
import type { LabViewProps } from './viewRegistry'
import './LabBench.css'
import './LabReports.css'

export const viewId = 'turnaround'
export const viewLabel = 'Turnaround times'
export const viewOrder = 80
export const viewPermission = 'lab.read'
export const viewWhenDraining = 'show'

type Load =
  | { state: 'loading' }
  | { state: 'ready'; board: TatDashboard }
  | { state: 'failed'; message: string }

function daysBefore(key: string, days: number): string {
  const [y, m, d] = key.split('-').map(Number)
  return localDateKey(new Date(y, m - 1, d - days))
}

function targetText(figures: TatFigures): string {
  const t = figures.target
  if (t.state === 'no_target') return t.no_target ? 'No target' : 'Not recorded'
  return `${t.text} (${t.within} of ${t.with_target})`
}

function count(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`
}

export default function TatView({ onModuleOff }: LabViewProps) {
  const today = localDateKey()
  const [from, setFrom] = useState(() => daysBefore(today, 6))
  const [to, setTo] = useState(today)
  const [period, setPeriod] = useState<TatPeriod>(() => ({ from: daysBefore(today, 6), to: today }))
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [exporting, setExporting] = useState(false)
  const [exportMessage, setExportMessage] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    getLabTat(period).then(
      (board) => { if (live) setLoad({ state: 'ready', board }) },
      (err: unknown) => {
        if (!live) return
        const failure = toLabError(err)
        if (failure.isModuleDisabled) {
          onModuleOff()
          return
        }
        setLoad({ state: 'failed', message: failure.message || 'The turnaround times could not be loaded.' })
      },
    )
    return () => { live = false }
  }, [period, onModuleOff])

  const show = () => {
    setLoad({ state: 'loading' })
    setPeriod({ from, to })
  }

  const download = async () => {
    if (exporting) return
    setExporting(true)
    setExportMessage(null)
    try {
      const file = await exportLabTat(period)
      const url = URL.createObjectURL(new Blob([file.csv], { type: 'text/csv' }))
      const a = document.createElement('a')
      a.href = url
      a.download = file.filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      setExportMessage(`Exported ${count(file.rows, 'test')}.`)
    } catch (err) {
      const failure = toLabError(err)
      if (failure.isModuleDisabled) {
        onModuleOff()
        return
      }
      setExportMessage(failure.message || 'The export could not be made.')
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="lab-bench">
      <div className="lab-bench-head">
        <div>
          <h2 className="lab-bench-title">Turnaround times</h2>
          <p className="lab-bench-sub">
            Time from receipt in the laboratory to the first report, against each test&apos;s
            own target. Targets are set per test and priority in Setup.
          </p>
        </div>
      </div>

      <form className="lab-bench-form" onSubmit={(e) => { e.preventDefault(); show() }}>
        <label className="lab-bench-field">
          First day
          <input type="date" className="lab-bench-input lab-tat-date" value={from} max={to || today}
            onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label className="lab-bench-field">
          Last day
          <input type="date" className="lab-bench-input lab-tat-date" value={to} max={today}
            onChange={(e) => setTo(e.target.value)} />
        </label>
        <button type="submit" className="lab-bench-btn lab-bench-btn--primary" disabled={!from || !to}>
          Show
        </button>
        <button type="button" className="lab-bench-btn" onClick={() => void download()}
          disabled={exporting || load.state !== 'ready'}>
          {exporting ? 'Exporting' : 'Export CSV'}
        </button>
      </form>
      {exportMessage && <p className="lab-bench-sub" role="status">{exportMessage}</p>}

      {load.state === 'loading' && <p className="lab-bench-sub" role="status">Loading turnaround times</p>}
      {load.state === 'failed' && (
        <div className="lab-bench-note lab-bench-note--error" role="alert">
          <p>{load.message}</p>
          <div className="lab-bench-actions">
            <button type="button" className="lab-bench-btn" onClick={show}>Try again</button>
          </div>
        </div>
      )}
      {load.state === 'ready' && <Board board={load.board} />}
    </div>
  )
}

function Board({ board }: { board: TatDashboard }) {
  const { overall, critical, overdue, exceptions } = board
  const tat = overall.intervals.receipt_to_report
  const periodText = board.period.first_day === board.period.last_day
    ? formatClinicalDateKey(board.period.first_day)
    : `${formatClinicalDateKey(board.period.first_day)} to ${formatClinicalDateKey(board.period.last_day)}`
  return (
    <>
      <section className="lab-bench-section" aria-labelledby="lab-tat-summary-title">
        <h3 id="lab-tat-summary-title" className="lab-bench-section-title">{periodText}</h3>
        <dl className="lab-tat-figures">
          <div className="lab-tat-figure">
            <dt>Tests reported</dt>
            <dd>{overall.count}</dd>
          </div>
          <div className="lab-tat-figure">
            <dt>Within target</dt>
            <dd>{targetText(overall)}</dd>
          </div>
          <div className="lab-tat-figure">
            <dt>Median, receipt to report</dt>
            <dd>{minutesText(tat.median)}</dd>
          </div>
          <div className="lab-tat-figure">
            <dt>90th percentile, receipt to report</dt>
            <dd>{minutesText(tat.p90)}</dd>
          </div>
          <div className={`lab-tat-figure${critical.open ? ' lab-tat-figure--warn' : ''}`}>
            <dt>Critical results acknowledged</dt>
            <dd>
              {critical.raised === 0
                ? 'None raised'
                : `${critical.acknowledged} of ${critical.raised}, median ${minutesText(critical.median_minutes)}`}
            </dd>
          </div>
          <div className={`lab-tat-figure${exceptions.open ? ' lab-tat-figure--warn' : ''}`}>
            <dt>Open exceptions</dt>
            <dd>{exceptions.open}</dd>
          </div>
        </dl>
        {(board.simulated_excluded > 0 || overall.out_of_order > 0 || critical.open > 0) && (
          <div className="lab-bench-note">
            {board.simulated_excluded > 0 && (
              <p>{count(board.simulated_excluded, 'simulated result')} left out of these figures.</p>
            )}
            {overall.out_of_order > 0 && (
              <p>
                {count(overall.out_of_order, 'test')} had a time recorded before the step it
                follows; that interval is left out.
              </p>
            )}
            {critical.open > 0 && (
              <p>
                {count(critical.open, 'critical result')} not yet acknowledged
                {critical.oldest_open_minutes !== null
                  ? `, the oldest raised ${minutesText(critical.oldest_open_minutes)} ago` : ''}.
              </p>
            )}
          </div>
        )}
      </section>

      <section className="lab-bench-section" aria-labelledby="lab-tat-overdue-title">
        <h3 id="lab-tat-overdue-title" className="lab-bench-section-title">
          Past their target now
        </h3>
        {overdue.items.length === 0 ? (
          <p className="lab-bench-empty">
            No test in the laboratory is past its target.
            {overdue.in_lab_without_target > 0
              ? ` ${count(overdue.in_lab_without_target, 'test')} in the laboratory ${overdue.in_lab_without_target === 1 ? 'has' : 'have'} no target.`
              : ''}
          </p>
        ) : (
          <div className="lab-bench-table-wrap">
            <table className="lab-bench-table" aria-labelledby="lab-tat-overdue-title">
              <thead>
                <tr>
                  <th scope="col">Priority</th>
                  <th scope="col">Test</th>
                  <th scope="col">Accession</th>
                  <th scope="col">Patient</th>
                  <th scope="col">Received</th>
                  <th scope="col">Over target by</th>
                </tr>
              </thead>
              <tbody>
                {overdue.items.map((o) => (
                  <tr key={o.item_id}>
                    <td className={o.priority === 'routine' ? undefined : 'lab-bench-stat'}>{o.priority_text}</td>
                    <td>{o.test.name}<span className="lab-bench-quiet"> · {o.section_name}</span></td>
                    <td className="lab-bench-num">{formatAccession(o.specimen_id)}</td>
                    <td>
                      {o.patient.name ?? 'Name not recorded'}
                      <span className="lab-bench-quiet">{o.patient.mrn ? ` · UHID ${o.patient.mrn}` : ''}</span>
                    </td>
                    <td className="lab-bench-num">{formatClinicalDateTime(o.received_at)}</td>
                    <td className="lab-bench-num">
                      <span className="lab-bench-flag lab-bench-flag--out">{minutesText(o.over_by_minutes)}</span>
                      <span className="lab-bench-quiet"> (target {minutesText(o.target_minutes)})</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {overdue.total > overdue.items.length && (
          <p className="lab-bench-sub">
            Showing the first {overdue.items.length} of {overdue.total}.
          </p>
        )}
      </section>

      <FiguresTable
        title="By section and priority"
        id="lab-tat-groups-title"
        empty="No test was reported in this period."
        rows={board.by_section_priority.map((g) => ({
          key: `${g.section_id ?? g.section_name}-${g.priority}`,
          label: `${g.section_name}, ${g.priority_text}`,
          figures: g,
        }))}
      />

      <FiguresTable
        title="By day"
        id="lab-tat-days-title"
        empty="No days in this period."
        rows={board.by_day.map((d) => ({ key: d.day, label: formatClinicalDateKey(d.day), figures: d }))}
      />

      {exceptions.by_kind.length > 0 && (
        <section className="lab-bench-section" aria-labelledby="lab-tat-exceptions-title">
          <h3 id="lab-tat-exceptions-title" className="lab-bench-section-title">Open exceptions</h3>
          <ul className="lab-bench-tests">
            {exceptions.by_kind.map((k) => <li key={k.kind}>{k.label}: {k.count}</li>)}
          </ul>
        </section>
      )}
    </>
  )
}

function FiguresTable({ title, id, empty, rows }: {
  title: string
  id: string
  empty: string
  rows: { key: string; label: string; figures: TatFigures }[]
}) {
  return (
    <section className="lab-bench-section" aria-labelledby={id}>
      <h3 id={id} className="lab-bench-section-title">{title}</h3>
      {rows.length === 0 ? (
        <p className="lab-bench-empty">{empty}</p>
      ) : (
        <div className="lab-bench-table-wrap">
          <table className="lab-bench-table" aria-labelledby={id}>
            <thead>
              <tr>
                <th scope="col">{title === 'By day' ? 'Day' : 'Section and priority'}</th>
                <th scope="col">Tests</th>
                <th scope="col">Receipt to report, median</th>
                <th scope="col">90th percentile</th>
                <th scope="col">Collection to receipt, median</th>
                <th scope="col">Order to report, median</th>
                <th scope="col">Within target</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ key, label, figures }) => (
                <tr key={key}>
                  <td>{label}</td>
                  <td className="lab-bench-num">{figures.count}</td>
                  <td className="lab-bench-num">{figures.count ? minutesText(figures.intervals.receipt_to_report.median) : ''}</td>
                  <td className="lab-bench-num">{figures.count ? minutesText(figures.intervals.receipt_to_report.p90) : ''}</td>
                  <td className="lab-bench-num">{figures.count ? minutesText(figures.intervals.collection_to_receipt.median) : ''}</td>
                  <td className="lab-bench-num">{figures.count ? minutesText(figures.intervals.order_to_report.median) : ''}</td>
                  <td>{figures.count ? targetText(figures) : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
