/**
 * Beds, Observations view: the ward clerk's copy of the paper observation
 * sheet (Orb Ward Record, 6 October 2026).
 *
 * The clerk holds no chart (no patient.read_all, no vitals.read), so the shift
 * board is not hers. This view lists the patients of her own department from
 * the two lists she can already read, the bed board (patients in a bed) and the
 * awaiting list (admissions with no bed linked), and opens the observation
 * sheet for one of them. The sheet sees she transcribes and asks for the time
 * written on the sheet, offers an Illegible tick beside every reading, and
 * shows no score.
 *
 * viewPermission is vitals.transcribe, so only the clerk sees this tab: a
 * nurse or doctor records at the bedside from the shift board, never here, and
 * the bed manager (admin) keys no observations. It adds no rail tab and opens
 * no other clinical screen.
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch } from '../../../api/client'
import ObsEntrySheet, { type ObsSheetPatient } from '../nurse/ObsEntrySheet'
import '../nurse/NursePanel.css'
import {
  failureOf,
  mergePatients,
  type ClerkAwaiting,
  type ClerkBoard,
  type ClerkPatientRow,
  type ObservationsViewProps,
} from './clerkPatients'

/* The reads and the list are worked out in clerkPatients.ts, beside this
   file, so this one exports only the view (React fast refresh). */

export const viewId = 'observations'
export const viewLabel = 'Observations'
export const viewOrder = 30
export const viewPermission = 'vitals.transcribe'

type Load =
  | { state: 'loading' }
  | { state: 'ready'; rows: ClerkPatientRow[] }
  | { state: 'error'; message: string }

export default function ObservationsView({ onModuleOff }: ObservationsViewProps) {
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [sheetFor, setSheetFor] = useState<ObsSheetPatient | null>(null)
  const [outcome, setOutcome] = useState<string | null>(null)

  const refresh = useCallback((): Promise<void> => Promise.all([
    apiFetch<ClerkBoard>('/adt/board'),
    // The awaiting list is a second source, not a requirement: a refusal there
    // still leaves every patient in a bed reachable.
    apiFetch<ClerkAwaiting>('/adt/awaiting-bed').catch(() => null),
  ]).then(
    ([board, awaiting]) => { setLoad({ state: 'ready', rows: mergePatients(board, awaiting) }) },
    (err: unknown) => {
      const failure = failureOf(err)
      if (failure.moduleOff) {
        onModuleOff()
        return
      }
      setLoad({ state: 'error', message: failure.message })
    },
  ), [onModuleOff])

  useEffect(() => { void refresh() }, [refresh])

  if (load.state === 'loading') {
    return <p className="bb-status" role="status">Loading the patients on your ward</p>
  }
  if (load.state === 'error') {
    return (
      <section className="bb-notice bb-notice--error" role="alert">
        <p className="bb-notice__body">{load.message}</p>
        <button type="button" className="bb-btn" onClick={() => { void refresh() }}>Try again</button>
      </section>
    )
  }

  return (
    <div className="bb-lists">
      <p className="bb-note">
        Copy each round from the paper observation sheet. Use the time written on the sheet.
        If a reading cannot be read, tick Illegible. Never guess a number.
      </p>
      {outcome && <p className="bb-note" role="status">{outcome}</p>}

      <section className="bb-list" aria-labelledby="bb-obs-title">
        <h2 id="bb-obs-title" className="bb-list__title">
          Patients <span className="bb-list__n">{load.rows.length}</span>
        </h2>
        {load.rows.length === 0 ? (
          <p className="bb-note">No patients are admitted to your ward.</p>
        ) : (
          <ul className="bb-rows">
            {load.rows.map(row => (
              <li key={row.patientId} className="bb-row">
                <span className="bb-row__main">
                  <strong>{row.name}</strong>
                  {row.ward ? <> · {row.ward}</> : null}
                  {row.bed ? <> · Bed {row.bed}</> : null}
                  {row.mrn ? <> · {row.mrn}</> : null}
                </span>
                <button
                  type="button"
                  className="bb-btn"
                  onClick={() => {
                    setOutcome(null)
                    setSheetFor({ patient_id: row.patientId, name: row.name, bed: row.bed, mrn: row.mrn })
                  }}
                >
                  Copy from sheet
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {sheetFor && (
        <ObsEntrySheet
          patient={sheetFor}
          onClose={() => setSheetFor(null)}
          onRecorded={() => {
            setOutcome(`Observations saved for ${sheetFor.name}.`)
            setSheetFor(null)
          }}
        />
      )}
    </div>
  )
}
