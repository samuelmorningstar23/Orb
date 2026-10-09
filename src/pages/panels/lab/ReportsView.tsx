/**
 * Reports: find a sample's laboratory report, check it on screen and print it
 * (lis-14).
 *
 * THREE WAYS IN:
 *   1. Scan or type the accession number on the tube or the request: the
 *      server refuses a number whose check digit is wrong, so a misread digit
 *      never opens another patient's report.
 *   2. The samples reported on one hospital day (today unless another day is
 *      chosen).
 *   3. Every report of one patient by exact UHID.
 *
 * WHAT IT SHOWS is the report as it prints (LabReportPrintView), drawn inline:
 * released values only, 'Pending' for a test not yet authorised, the range
 * snapshot taken at entry, every earlier version of an amended result, the
 * signatories with their registration numbers and the Simulated label.
 * Printing asks the server for the print copy (audited as a print) and then
 * opens the browser's print dialog on that copy alone.
 *
 * Every read is audited on the server, so lists load when the view opens and
 * when asked; nothing polls. A failed load is said in words, never drawn as an
 * empty list. Reports are finished work, so this view stays while the module
 * drains.
 */
import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'

import { toLabError } from '../../../api/lab'
import {
  getLabReport,
  listLabReports,
  printLabReport,
  type LabReport,
  type ReportSummary,
} from '../../../api/lab/reports'
import { formatAccession, priorityText } from '../../../api/lab/specimens'
import LabReportPrintView from '../../../components/print/LabReportPrintView'
import { formatClinicalDateKey, formatClinicalDateTime, localDateKey } from '../../../lib/datetime'
import type { LabViewProps } from './viewRegistry'
import './LabBench.css'
import './LabReports.css'

export const viewId = 'reports'
export const viewLabel = 'Reports'
export const viewOrder = 70
export const viewPermission = 'lab.read'
export const viewWhenDraining = 'show'

type ListState =
  | { state: 'loading' }
  | { state: 'ready'; reports: ReportSummary[]; label: string }
  | { state: 'failed'; message: string }

function listLabel(day: string | null, uhid: string | null): string {
  if (uhid) return `Reports for UHID ${uhid}`
  return `Reported on ${formatClinicalDateKey(day ?? '')}`
}

export default function ReportsView({ onModuleOff }: LabViewProps) {
  const [day, setDay] = useState(() => localDateKey())
  const [uhid, setUhid] = useState('')
  const [query, setQuery] = useState<{ day?: string; uhid?: string }>(() => ({ day: localDateKey() }))
  const [list, setList] = useState<ListState>({ state: 'loading' })
  const [accession, setAccession] = useState('')
  const [report, setReport] = useState<LabReport | null>(null)
  const [opening, setOpening] = useState(false)
  const [openError, setOpenError] = useState<string | null>(null)
  const [printing, setPrinting] = useState<LabReport | null>(null)
  const [printBusy, setPrintBusy] = useState(false)
  const [printError, setPrintError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    listLabReports(query).then(
      (out) => {
        if (live) setList({ state: 'ready', reports: out.reports, label: listLabel(out.day, out.uhid) })
      },
      (err: unknown) => {
        if (!live) return
        const failure = toLabError(err)
        if (failure.isModuleDisabled) {
          onModuleOff()
          return
        }
        setList({ state: 'failed', message: failure.message || 'The reports could not be loaded.' })
      },
    )
    return () => { live = false }
  }, [query, onModuleOff])

  const showDay = () => {
    setList({ state: 'loading' })
    setQuery({ day })
  }

  const showUhid = () => {
    const wanted = uhid.trim()
    if (!wanted) return
    setList({ state: 'loading' })
    setQuery({ uhid: wanted })
  }

  const openReport = useCallback(async (wanted: string) => {
    const text = wanted.replace(/\s+/g, '')
    if (!text) {
      setOpenError('Scan or type the accession number.')
      return
    }
    setOpening(true)
    setOpenError(null)
    setPrintError(null)
    try {
      setReport(await getLabReport(text))
    } catch (err) {
      const failure = toLabError(err)
      if (failure.isModuleDisabled) {
        onModuleOff()
        return
      }
      setReport(null)
      if (failure.code === 'bad_accession') {
        setOpenError('That number is not a valid accession. Check the digits and scan again.')
      } else if (failure.isNotFound) {
        setOpenError('No sample with that accession number.')
      } else {
        setOpenError(failure.message || 'The report could not be opened.')
      }
    } finally {
      setOpening(false)
    }
  }, [onModuleOff])

  const print = async () => {
    if (!report || printBusy) return
    setPrintBusy(true)
    setPrintError(null)
    try {
      setPrinting(await printLabReport(report.accession))
    } catch (err) {
      const failure = toLabError(err)
      if (failure.isModuleDisabled) {
        onModuleOff()
        return
      }
      setPrintError(failure.message || 'The report could not be printed.')
    } finally {
      setPrintBusy(false)
    }
  }

  return (
    <div className="lab-bench">
      <div className="lab-bench-head">
        <div>
          <h2 className="lab-bench-title">Reports</h2>
          <p className="lab-bench-sub">
            Open a sample&apos;s report by its accession number, check it and print it. A
            report shows released results only; a test not yet authorised says Pending.
          </p>
        </div>
      </div>

      <form
        className="lab-bench-form"
        onSubmit={(e) => { e.preventDefault(); void openReport(accession) }}
      >
        <label className="lab-bench-field">
          Accession number
          <input
            className="lab-bench-input lab-bench-input--scan"
            value={accession}
            onChange={(e) => setAccession(e.target.value)}
            inputMode="numeric"
            autoComplete="off"
            placeholder="Scan the tube or type the number"
          />
        </label>
        <button type="submit" className="lab-bench-btn lab-bench-btn--primary" disabled={opening}>
          {opening ? 'Opening' : 'Open report'}
        </button>
      </form>
      {openError && <p className="lab-bench-error" role="alert">{openError}</p>}

      {report && (
        <section className="lab-bench-section" aria-label="Report">
          <div className="lab-bench-actions">
            <button type="button" className="lab-bench-btn lab-bench-btn--primary"
              onClick={() => void print()} disabled={printBusy}>
              {printBusy ? 'Preparing the print' : 'Print report'}
            </button>
            <button type="button" className="lab-bench-btn" onClick={() => setReport(null)}>
              Close report
            </button>
          </div>
          {printError && <p className="lab-bench-error" role="alert">{printError}</p>}
          <LabReportPrintView report={report} variant="screen" autoPrint={false} />
        </section>
      )}

      <section className="lab-bench-section" aria-labelledby="lab-reports-list-title">
        <div className="lab-bench-form">
          <label className="lab-bench-field">
            Day reported
            <input
              type="date"
              className="lab-bench-input lab-tat-date"
              value={day}
              max={localDateKey()}
              onChange={(e) => setDay(e.target.value)}
            />
          </label>
          <button type="button" className="lab-bench-btn" onClick={showDay} disabled={!day}>
            Show day
          </button>
          <label className="lab-bench-field">
            UHID
            <input
              className="lab-bench-input"
              value={uhid}
              onChange={(e) => setUhid(e.target.value)}
              autoComplete="off"
            />
          </label>
          <button type="button" className="lab-bench-btn" onClick={showUhid} disabled={!uhid.trim()}>
            Find reports
          </button>
        </div>

        {list.state === 'loading' && <p className="lab-bench-sub" role="status">Loading reports</p>}
        {list.state === 'failed' && (
          <div className="lab-bench-note lab-bench-note--error" role="alert">
            <p>{list.message}</p>
            <div className="lab-bench-actions">
              <button type="button" className="lab-bench-btn"
                onClick={() => { setList({ state: 'loading' }); setQuery({ ...query }) }}>
                Try again
              </button>
            </div>
          </div>
        )}
        {list.state === 'ready' && (
          <>
            <h3 id="lab-reports-list-title" className="lab-bench-section-title">{list.label}</h3>
            {list.reports.length === 0 ? (
              <p className="lab-bench-empty">No reports here.</p>
            ) : (
              <div className="lab-bench-table-wrap">
                <table className="lab-bench-table" aria-labelledby="lab-reports-list-title">
                  <thead>
                    <tr>
                      <th scope="col">Accession</th>
                      <th scope="col">Patient</th>
                      <th scope="col">Tests</th>
                      <th scope="col">Report</th>
                      <th scope="col">Reported</th>
                      <th scope="col"><span className="lab-bench-quiet">Open</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.reports.map((r) => (
                      <tr key={r.accession}>
                        <td className="lab-bench-num">
                          {formatAccession(r.accession)}
                          {r.priority !== 'routine' && (
                            <span className="lab-bench-stat"> {priorityText(r.priority)}</span>
                          )}
                        </td>
                        <td>
                          {r.patient.name ?? 'Name not recorded'}
                          <span className="lab-bench-quiet">
                            {r.patient.mrn ? ` · UHID ${r.patient.mrn}` : ''}
                          </span>
                        </td>
                        <td>{r.tests.join(', ')}</td>
                        <td>
                          {r.status_text}
                          {r.simulated && <span className="lab-bench-flag lab-bench-flag--out"> Simulated</span>}
                        </td>
                        <td className="lab-bench-num">{formatClinicalDateTime(r.last_reported_at)}</td>
                        <td>
                          <button type="button" className="lab-bench-btn"
                            aria-label={`Open report ${formatAccession(r.accession)}`}
                            onClick={() => { setAccession(r.accession); void openReport(r.accession) }}>
                            Open
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>

      {printing && createPortal(
        <LabReportPrintView report={printing} onAfterPrint={() => setPrinting(null)} />,
        document.body,
      )}
    </div>
  )
}
