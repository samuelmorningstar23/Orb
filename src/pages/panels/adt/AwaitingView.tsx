/**
 * Beds, Awaiting a bed view (adt-4a): admissions in the caller's department
 * with no bed in bed setup, as two lists that are never merged.
 *
 *   Awaiting a bed   nobody has said where the patient lies.
 *   Not linked       a bed was typed (at admission, by Scribe, from a roster
 *                    file) that is not linked to a bed in setup. The patient
 *                    lies somewhere a person wrote down; a bed manager links
 *                    it, which is how a typed bed becomes a bed on the board.
 *
 * Every line names a patient, so the server audits each read. Only a bed
 * manager (bed.master) sees 'Link to bed', and only where the typed bed
 * matches exactly one bed in setup. A link the server will not make without
 * words (a boarder on another department's ward, a bed out of service) comes
 * back with those words, and is made only after the bed manager confirms
 * them here.
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react'

import { toAdtError } from '../../../api/adt'
import {
  getAwaitingBed,
  linkAdmissionsToBeds,
  type AwaitingBed,
  type AwaitingEntry,
} from '../../../api/adt/board'
import { formatClinicalDateTime } from '../../../lib/datetime'
import type { AdtViewProps } from './viewRegistry'

export const viewId = 'awaiting'
export const viewLabel = 'Awaiting a bed'
export const viewOrder = 20
export const viewPermission = 'adt.read'

type Load =
  | { state: 'loading' }
  | { state: 'ready'; data: AwaitingBed }
  | { state: 'error'; message: string }

/** A link the server sent back for words: what it said, and what it needs. */
interface PendingLink {
  admissionId: string
  code: string
  message: string
}

export default function AwaitingView({ onModuleOff, holds }: AdtViewProps) {
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [busy, setBusy] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<string | null>(null)
  const [pending, setPending] = useState<PendingLink | null>(null)
  const [oosReason, setOosReason] = useState('')
  const canLink = holds('bed.master')

  // State changes only in the reply's callbacks, never synchronously.
  const refresh = useCallback((): Promise<void> => getAwaitingBed().then(
    (data) => { setLoad({ state: 'ready', data }) },
    (err: unknown) => {
      const failure = toAdtError(err)
      if (failure.isModuleDisabled) {
        onModuleOff()
        return
      }
      setLoad({
        state: 'error',
        message: failure.isForbidden ? 'Your role cannot see this list.' : failure.message,
      })
    },
  ), [onModuleOff])

  useEffect(() => { void refresh() }, [refresh])

  const link = useCallback(async (admissionId: string, extra: {
    boarder_acknowledged?: boolean
    out_of_service_reason?: string
  } = {}) => {
    setBusy(admissionId)
    setOutcome(null)
    try {
      const result = await linkAdmissionsToBeds({ admission_ids: [admissionId], ...extra })
      const done = result.linked.find((l) => l.admission_id === admissionId)
      const skipped = result.skipped.find((s) => s.admission_id === admissionId)
      if (done) {
        setPending(null)
        setOosReason('')
        setOutcome(`Linked to ${done.ward}, bed ${done.bed}.`)
        await refresh()
      } else if (skipped && (skipped.code === 'boarder_acknowledgement_required'
        || skipped.code === 'out_of_service_reason_required')) {
        setPending({ admissionId, code: skipped.code, message: skipped.message ?? '' })
      } else {
        setPending(null)
        setOutcome(skipped?.message || 'This admission could not be linked. Reload and try again.')
        await refresh()
      }
    } catch (err) {
      const failure = toAdtError(err)
      if (failure.isModuleDisabled) {
        onModuleOff()
        return
      }
      setOutcome(failure.message)
    } finally {
      setBusy(null)
    }
  }, [onModuleOff, refresh])

  if (load.state === 'loading') {
    return <p className="bb-status" role="status">Loading the admissions awaiting a bed</p>
  }
  if (load.state === 'error') {
    return (
      <section className="bb-notice bb-notice--error" role="alert">
        <p className="bb-notice__body">{load.message}</p>
        <button type="button" className="bb-btn" onClick={() => { void refresh() }}>Try again</button>
      </section>
    )
  }

  const { awaiting, unlinked } = load.data

  return (
    <div className="bb-lists">
      {outcome && <p className="bb-note" role="status">{outcome}</p>}

      <section className="bb-list" aria-labelledby="bb-awaiting-title">
        <h2 id="bb-awaiting-title" className="bb-list__title">
          Awaiting a bed <span className="bb-list__n">{awaiting.length}</span>
        </h2>
        {awaiting.length === 0 ? (
          <p className="bb-note">Every admission in your department has a bed recorded.</p>
        ) : (
          <ul className="bb-rows">
            {awaiting.map((entry) => <EntryRow key={entry.admission_id} entry={entry} />)}
          </ul>
        )}
      </section>

      <section className="bb-list" aria-labelledby="bb-unlinked-title">
        <h2 id="bb-unlinked-title" className="bb-list__title">
          Bed typed, not linked to bed setup <span className="bb-list__n">{unlinked.length}</span>
        </h2>
        {unlinked.length === 0 ? (
          <p className="bb-note">No typed bed is waiting to be linked.</p>
        ) : (
          <ul className="bb-rows">
            {unlinked.map((entry) => (
              <EntryRow key={entry.admission_id} entry={entry}>
                {canLink && entry.reason === 'matches_bed' && (
                  <button
                    type="button"
                    className="bb-btn bb-btn--small"
                    disabled={busy !== null}
                    onClick={() => { void link(entry.admission_id) }}
                  >
                    Link to bed {entry.master_unit_code}
                  </button>
                )}
                {pending && pending.admissionId === entry.admission_id && (
                  <div className="bb-confirm" role="group" aria-label="Confirm the link">
                    <p className="bb-confirm__words">{pending.message}</p>
                    {pending.code === 'boarder_acknowledgement_required' ? (
                      <button
                        type="button"
                        className="bb-btn bb-btn--small"
                        disabled={busy !== null}
                        onClick={() => { void link(entry.admission_id, { boarder_acknowledged: true }) }}
                      >
                        I have read this. Link as a boarder
                      </button>
                    ) : (
                      <>
                        <label className="bb-field">
                          <span className="bb-field__label">Reason the patient is in this bed</span>
                          <input
                            className="bb-input"
                            value={oosReason}
                            onChange={(e) => setOosReason(e.target.value)}
                          />
                        </label>
                        <button
                          type="button"
                          className="bb-btn bb-btn--small"
                          disabled={busy !== null || !oosReason.trim()}
                          onClick={() => {
                            void link(entry.admission_id, { out_of_service_reason: oosReason.trim() })
                          }}
                        >
                          Link with this reason
                        </button>
                      </>
                    )}
                    <button
                      type="button"
                      className="bb-btn bb-btn--small bb-btn--quiet"
                      onClick={() => { setPending(null); setOosReason('') }}
                    >
                      Cancel
                    </button>
                  </div>
                )}
              </EntryRow>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

function EntryRow({ entry, children }: { entry: AwaitingEntry; children?: ReactNode }) {
  const where = [entry.ward, entry.bed ? `bed ${entry.bed}` : null].filter(Boolean).join(', ')
  return (
    <li className="bb-row">
      <div className="bb-row__main">
        <span className="bb-row__name">{entry.name ?? 'Name not recorded'}</span>
        {entry.mrn && <span className="bb-row__mrn">{entry.mrn}</span>}
        <span className="bb-row__dept">{entry.department}</span>
      </div>
      <div className="bb-row__meta">
        {where && <span>Typed: {where}</span>}
        {entry.reason_words && <span>{entry.reason_words}</span>}
        {entry.since && <span>Admitted {formatClinicalDateTime(entry.since)}</span>}
      </div>
      {children}
    </li>
  )
}
