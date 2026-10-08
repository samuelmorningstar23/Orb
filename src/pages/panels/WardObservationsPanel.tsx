/**
 * Observations: the ward clerk's own screen (Orb Ward Record, 9 October 2026).
 *
 * The clerk copies each observation round from the paper sheet. Her copy used
 * to live only inside the Beds panel (adt/ObservationsView.tsx), which needs
 * the beds module (ORB_ADT) switched on and reads the bed board. This screen
 * is her own rail entry, shown to an account that holds vitals.transcribe,
 * with the beds module on or off. Her patient list comes from
 * GET /api/nurse/transcribe/patients: her own department's admitted patients,
 * with ward, bed and admission time, and nothing clinical. It opens the same
 * observation sheet (nurse/ObsEntrySheet.tsx), which asks for the time on the
 * sheet, offers an Illegible tick beside every reading and shows no score.
 *
 * The Beds panel keeps its own Observations view for a site with the beds
 * module on; both open the same sheet.
 */
import { useCallback, useEffect, useState } from 'react'

import { getTranscribePatients, type TranscribePatient } from '../../api/nurse'
import ObsEntrySheet, { type ObsSheetPatient } from './nurse/ObsEntrySheet'
import './nurse/NursePanel.css'
import './WardObservationsPanel.css'

type Load =
  | { state: 'loading' }
  | { state: 'ready'; rows: TranscribePatient[] }
  | { state: 'error'; message: string }

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })

/** Ward, then bed, then name, so Bed 2 comes before Bed 10. */
function byWardAndBed(a: TranscribePatient, b: TranscribePatient): number {
  return collator.compare(a.ward ?? '￿', b.ward ?? '￿')
    || collator.compare(a.bed ?? '￿', b.bed ?? '￿')
    || collator.compare(a.name ?? '', b.name ?? '')
}

/** A refused or failed read, in words. */
function messageFor(err: unknown): string {
  const raw = (err ?? {}) as { status?: unknown; message?: unknown }
  const status = typeof raw.status === 'number' ? raw.status : 0
  if (status === 403) return 'Your role cannot see this list.'
  // No status: the request never got an answer (apiFetch puts the HTTP status
  // on every error it raises from a reply).
  if (status === 0) return 'Could not reach the ward list. Check the connection and try again.'
  return typeof raw.message === 'string' && raw.message.trim()
    ? raw.message
    : 'The ward list could not be loaded.'
}

/** "Admitted 7 Oct, 14:20" on this computer's clock, or nothing. */
function admittedText(iso: string | null): string | null {
  if (!iso) return null
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return null
  return `Admitted ${when.toLocaleString([], {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  })}`
}

export default function WardObservationsPanel() {
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [sheetFor, setSheetFor] = useState<ObsSheetPatient | null>(null)
  const [outcome, setOutcome] = useState<string | null>(null)

  const refresh = useCallback((): Promise<void> => getTranscribePatients().then(
    list => { setLoad({ state: 'ready', rows: [...list.patients].sort(byWardAndBed) }) },
    (err: unknown) => { setLoad({ state: 'error', message: messageFor(err) }) },
  ), [])

  useEffect(() => { void refresh() }, [refresh])

  return (
    <div className="wobs-panel">
      <header className="wobs-head">
        <h1 className="wobs-title">Observations</h1>
        <p className="wobs-note">
          Copy each round from the paper observation sheet. Use the time written on the sheet.
          If a reading cannot be read, tick Illegible. If a box on the sheet is empty, leave it
          empty or choose Not recorded on the sheet. Never guess a number.
        </p>
      </header>

      {load.state === 'loading' && (
        <p className="wobs-note" role="status">Loading the patients on your ward</p>
      )}

      {load.state === 'error' && (
        <section className="wobs-notice" role="alert">
          <p>{load.message}</p>
          <button type="button" className="wobs-btn" onClick={() => { void refresh() }}>Try again</button>
        </section>
      )}

      {load.state === 'ready' && (
        <>
          {outcome && <p className="wobs-outcome" role="status">{outcome}</p>}
          <section className="wobs-list" aria-labelledby="wobs-list-title">
            <h2 id="wobs-list-title" className="wobs-list__title">
              Patients <span className="wobs-list__n">{load.rows.length}</span>
            </h2>
            {load.rows.length === 0 ? (
              <p className="wobs-note">No patients are admitted to your ward.</p>
            ) : (
              <ul className="wobs-rows">
                {load.rows.map(row => {
                  const name = row.name || 'Name not recorded'
                  const meta = [
                    row.ward,
                    row.bed ? `Bed ${row.bed}` : null,
                    row.mrn,
                    admittedText(row.admitted_at),
                  ].filter(Boolean).join(' · ')
                  return (
                    <li key={row.patient_id} className="wobs-row">
                      <span className="wobs-row__main">
                        <span className="wobs-row__name">{name}</span>
                        {meta && <span className="wobs-row__meta">{meta}</span>}
                      </span>
                      <button
                        type="button"
                        className="wobs-btn"
                        aria-label={`Copy from sheet for ${name}`}
                        onClick={() => {
                          setOutcome(null)
                          setSheetFor({ patient_id: row.patient_id, name, bed: row.bed, mrn: row.mrn })
                        }}
                      >
                        Copy from sheet
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        </>
      )}

      {sheetFor && (
        <ObsEntrySheet
          patient={sheetFor}
          onClose={() => setSheetFor(null)}
          onRecorded={(_news2, _schedule, message) => {
            // A copy older than the live alert limit is charted but raises no
            // alert, and the server says so; she reads it here.
            setOutcome(message
              ? `Observations saved for ${sheetFor.name}. ${message.replace(/^Charted\.\s*/, '')}`
              : `Observations saved for ${sheetFor.name}.`)
            setSheetFor(null)
          }}
        />
      )}
    </div>
  )
}
