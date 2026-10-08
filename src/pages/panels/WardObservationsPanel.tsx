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
 *
 * Her own copies (9 October 2026): "Your copies" under a patient lists the
 * sets she copied for that patient in the last 24 hours (GET
 * /api/nurse/transcribe/patients/{id}/my-copies: sheet time, readings, struck
 * out or not), each with "Entered in error", so she corrects her own wrong
 * copy with a reason. The server lets her strike out her own copies only.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import {
  getOwnCopies, getTranscribePatients, type OwnCopy, type TranscribePatient,
} from '../../api/nurse'
import VoidObservationButton from '../../components/vitals/VoidObservationButton'
import { formatUTCDateTime } from '../../lib/datetime'
import { bloodPressure, vitalTemperature, vitalWhole } from '../../lib/vitals'
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

type Copies =
  | { state: 'loading' }
  | { state: 'ready'; rows: OwnCopy[] }
  | { state: 'error'; message: string }

/** The readings of one copy in plain words; a blank box reads "blank". */
function readingsText(r: OwnCopy['readings']): string {
  const blank = 'blank'
  const acvpu = r.consciousness
    ? r.consciousness.charAt(0).toUpperCase() + r.consciousness.slice(1)
    : 'Consciousness blank'
  const air = r.on_oxygen === null ? 'Air or oxygen blank' : r.on_oxygen ? 'On oxygen' : 'On air'
  return [
    `Pulse ${vitalWhole(r.heart_rate, blank)}`,
    `BP ${bloodPressure(r.systolic_bp, r.diastolic_bp, blank)}`,
    `Breathing ${vitalWhole(r.respiratory_rate, blank)}`,
    `Temp ${vitalTemperature(r.temperature, blank)}`,
    `SpO2 ${vitalWhole(r.spo2, blank)}`,
    acvpu,
    air,
  ].join(' · ')
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
  // The patient whose copies are open, and those copies.
  const [copiesFor, setCopiesFor] = useState<string | null>(null)
  const [copies, setCopies] = useState<Copies>({ state: 'loading' })
  // The patient whose copies were asked for last. An answer for any other
  // patient is dropped: opening a second patient before the first one's list
  // came back drew the first patient's copies, and their Entered in error
  // buttons, under the second patient's name.
  const copiesWanted = useRef<string | null>(null)

  const loadCopies = useCallback((patientId: string): Promise<void> => {
    copiesWanted.current = patientId
    setCopies({ state: 'loading' })
    return getOwnCopies(patientId).then(
      body => {
        if (copiesWanted.current !== patientId) return
        setCopies({ state: 'ready', rows: body.copies })
      },
      (err: unknown) => {
        if (copiesWanted.current !== patientId) return
        const status = (err as { status?: unknown } | null)?.status
        setCopies({
          state: 'error',
          message: status === undefined || status === 0
            ? 'Could not reach Orb. Check the connection and try again.'
            : 'Your copies could not be loaded.',
        })
      },
    )
  }, [])

  const toggleCopies = (patientId: string) => {
    if (copiesFor === patientId) {
      copiesWanted.current = null
      setCopiesFor(null)
      return
    }
    setCopiesFor(patientId)
    void loadCopies(patientId)
  }

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
                  const open = copiesFor === row.patient_id
                  return (
                    <li key={row.patient_id} className="wobs-row">
                      <span className="wobs-row__main">
                        <span className="wobs-row__name">{name}</span>
                        {meta && <span className="wobs-row__meta">{meta}</span>}
                      </span>
                      <span className="wobs-row__actions">
                        <button
                          type="button"
                          className="wobs-btn"
                          aria-expanded={open}
                          aria-label={`Your copies for ${name}`}
                          onClick={() => toggleCopies(row.patient_id)}
                        >
                          Your copies
                        </button>
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
                      </span>
                      {open && (
                        <section className="wobs-copies" aria-label={`Your copies for ${name}, last 24 hours`}>
                          {copies.state === 'loading' && <p className="wobs-note" role="status">Loading your copies</p>}
                          {copies.state === 'error' && <p className="wobs-note" role="alert">{copies.message}</p>}
                          {copies.state === 'ready' && copies.rows.length === 0 && (
                            <p className="wobs-note">You have not copied a set for {name} in the last 24 hours.</p>
                          )}
                          {copies.state === 'ready' && copies.rows.length > 0 && (
                            <ul className="wobs-copies__rows">
                              {copies.rows.map(copy => {
                                const when = formatUTCDateTime(copy.taken_at)
                                return (
                                  <li key={copy.vitals_id} className="wobs-copy">
                                    <span className="wobs-row__main">
                                      <span className={copy.voided ? 'wobs-copy__when wobs-copy__when--struck' : 'wobs-copy__when'}>
                                        Sheet time {when}
                                      </span>
                                      <span className="wobs-row__meta">{readingsText(copy.readings)}</span>
                                    </span>
                                    {copy.voided ? (
                                      <span className="wobs-copy__struck">Entered in error</span>
                                    ) : (
                                      <VoidObservationButton
                                        vitalsId={copy.vitals_id}
                                        ownCopy
                                        what={`your copy timed ${when}`}
                                        onVoided={() => {
                                          setOutcome(`Your copy timed ${when} is marked entered in error. It stays on the record.`)
                                          void loadCopies(row.patient_id)
                                        }}
                                      />
                                    )}
                                  </li>
                                )
                              })}
                            </ul>
                          )}
                        </section>
                      )}
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
            if (copiesFor === sheetFor.patient_id) void loadCopies(sheetFor.patient_id)
            setSheetFor(null)
          }}
        />
      )}
    </div>
  )
}
