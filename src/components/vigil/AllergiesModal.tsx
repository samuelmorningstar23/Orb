import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  ALLERGY_SEVERITIES,
  addAllergy,
  deactivateAllergy,
  listAllergies,
  type AllergySeverity,
  type ChartAllergy,
} from '../../api/allergies'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { assertOnlineForWrite } from '../../lib/offline/writeGuard'
import { formatClinicalDateTime } from '../../lib/datetime'
import { severityLabel } from './administerSafety'
import './AllergiesModal.css'

/**
 * The chart's allergy list, with add and deactivate (founding ward list
 * item 5, 21 October 2026).
 *
 * The routes and the interlock already existed; what was missing was a
 * control on the chart, so an allergy found on day three (the daughter
 * arrives and reports the penicillin reaction) could only reach the
 * structured list through the admission form. This modal reads
 * `patient_allergies`, the one list every safety check reads, and writes to
 * it through the same routes the admission form and the roster import use.
 *
 * Two rules it keeps from the Administer modal:
 *   · A FAILED read never renders as "no allergies". Those two states look
 *     the same and mean opposite things.
 *   · Severity is shown AS CHARTED (severityLabel), never re-graded here.
 *
 * Deactivating asks for a reason and sends it with the request; the server
 * writes it to the audit chain and keeps the row as inactive. The controls
 * are offered only with `canWrite` (patient.write, the doctor); a nurse sees
 * the list read-only, because the server would refuse her write anyway.
 */
interface Props {
  patientId: string
  patientName: string
  canWrite: boolean
  onClose: () => void
  /** Called after any successful add or deactivation, so the chart can
   *  re-read the list it hands to the Administer modal and the prints. */
  onChanged: () => void
}

type Load =
  | { state: 'loading' }
  | { state: 'ready'; rows: ChartAllergy[] }
  | { state: 'error'; message: string }

function messageFor(err: unknown, fallback: string): string {
  const raw = (err ?? {}) as { status?: unknown; message?: unknown }
  const status = typeof raw.status === 'number' ? raw.status : 0
  if (status === 403) return 'Your role cannot change the allergy list.'
  if (status === 404) return 'This patient is not on your ward, or the allergy is no longer on the list.'
  return typeof raw.message === 'string' && raw.message.trim() ? raw.message : fallback
}

const SEVERITY_WORDS: Record<AllergySeverity, string> = {
  mild: 'Mild',
  moderate: 'Moderate',
  severe: 'Severe',
  unknown: 'Severity not known',
}

export default function AllergiesModal({ patientId, patientName, canWrite, onClose, onChanged }: Props) {
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [substance, setSubstance] = useState('')
  const [reaction, setReaction] = useState('')
  const [severity, setSeverity] = useState<AllergySeverity>('unknown')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  /** The allergy whose deactivation reason is being typed, and the reason. */
  const [deactivating, setDeactivating] = useState<ChartAllergy | null>(null)
  const [reason, setReason] = useState('')

  /* Re-reads the list. The rows on screen stay until the new ones arrive
     (no synchronous loading flag), so a re-read after an add never blanks
     the list a nurse is looking at. */
  const refresh = useCallback(() => {
    let live = true
    listAllergies(patientId).then(
      rows => { if (live) setLoad({ state: 'ready', rows }) },
      (err: unknown) => { if (live) setLoad({ state: 'error', message: messageFor(err, 'The allergy list could not be read.') }) },
    )
    return () => { live = false }
  }, [patientId])

  useEffect(() => refresh(), [refresh])

  const requestClose = useCallback(() => { if (!saving) onClose() }, [saving, onClose])
  const modalRef = useFocusTrap<HTMLDivElement>(requestClose)

  const canAdd = canWrite && substance.trim().length > 0 && !saving

  const add = async () => {
    if (!canAdd) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      assertOnlineForWrite('record this allergy')
      const saved = await addAllergy(patientId, {
        substance: substance.trim(),
        reaction: reaction.trim() || null,
        severity,
      })
      setSubstance('')
      setReaction('')
      setSeverity('unknown')
      setNotice(`${saved.substance} added to the allergy list. Every drug check reads it from now.`)
      refresh()
      onChanged()
    } catch (e) {
      setError(messageFor(e, e instanceof Error ? e.message : 'The allergy could not be recorded.'))
    } finally {
      setSaving(false)
    }
  }

  const confirmDeactivate = async () => {
    if (!deactivating || reason.trim().length === 0 || saving) return
    setSaving(true)
    setError('')
    setNotice('')
    try {
      assertOnlineForWrite('deactivate this allergy')
      await deactivateAllergy(patientId, deactivating.id, reason.trim())
      setNotice(`${deactivating.substance} deactivated. The reason is on the audit trail.`)
      setDeactivating(null)
      setReason('')
      refresh()
      onChanged()
    } catch (e) {
      setError(messageFor(e, e instanceof Error ? e.message : 'The allergy could not be deactivated.'))
    } finally {
      setSaving(false)
    }
  }

  return createPortal(
    <div className="modal-overlay" onClick={requestClose}>
      <div
        className="modal glass-card allergies-modal"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="allergies-modal-title"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="modal__title" id="allergies-modal-title">
          Allergies
          <span className="allergies-modal__subject">{patientName}</span>
          <button
            type="button"
            className="allergies-modal__close"
            onClick={requestClose}
            aria-label="Close allergies"
            disabled={saving}
          >
            &times;
          </button>
        </h2>

        <div className="modal__body">
          {error && <div className="allergies-modal__note allergies-modal__note--danger" role="alert">{error}</div>}
          {notice && <div className="allergies-modal__note allergies-modal__note--ok" role="status">{notice}</div>}

          {load.state === 'loading' && <p className="allergies-modal__muted" role="status">Reading the allergy list</p>}
          {load.state === 'error' && (
            <p className="allergies-modal__note allergies-modal__note--danger" role="alert">
              {load.message} Check the chart before giving anything.
            </p>
          )}
          {load.state === 'ready' && load.rows.length === 0 && (
            <p className="allergies-modal__muted">No active allergies on the list.</p>
          )}
          {load.state === 'ready' && load.rows.length > 0 && (
            <ul className="allergies-modal__list" aria-label="Active allergies">
              {load.rows.map(a => (
                <li key={a.id} className="allergies-modal__item">
                  <div className="allergies-modal__row">
                    <div>
                      <p className="allergies-modal__substance">
                        {a.substance}
                        <span className={`allergies-modal__severity allergies-modal__severity--${severityLabel(a.severity) === 'severity not recorded' ? 'unknown' : severityLabel(a.severity)}`}>
                          {severityLabel(a.severity) === 'severity not recorded' ? 'severity not recorded' : severityLabel(a.severity)}
                        </span>
                      </p>
                      <p className="allergies-modal__meta">
                        {a.reaction ? `Reaction: ${a.reaction}` : 'Reaction not recorded'}
                        {a.source ? ` · ${a.source.replace(/_/g, ' ')}` : ''}
                        {a.recorded_at ? ` · ${formatClinicalDateTime(a.recorded_at)}` : ''}
                      </p>
                    </div>
                    {canWrite && deactivating?.id !== a.id && (
                      <button
                        type="button"
                        className="allergies-modal__btn allergies-modal__btn--ghost"
                        onClick={() => { setDeactivating(a); setReason(''); setError(''); setNotice('') }}
                        disabled={saving}
                      >
                        Deactivate
                      </button>
                    )}
                  </div>
                  {canWrite && deactivating?.id === a.id && (
                    <div className="allergies-modal__deactivate">
                      <label className="allergies-modal__label" htmlFor={`allergy-reason-${a.id}`}>
                        Why is {a.substance} coming off the list? The reason goes on the audit trail.
                      </label>
                      <input
                        id={`allergy-reason-${a.id}`}
                        className="allergies-modal__input"
                        type="text"
                        value={reason}
                        onChange={e => setReason(e.target.value)}
                        placeholder="e.g. tolerated amoxicillin course in 2025, de-labelled after review"
                        maxLength={500}
                        disabled={saving}
                        autoFocus
                      />
                      <div className="allergies-modal__actions">
                        <button type="button" className="allergies-modal__btn allergies-modal__btn--ghost" onClick={() => { setDeactivating(null); setReason('') }} disabled={saving}>
                          Keep it
                        </button>
                        <button
                          type="button"
                          className="allergies-modal__btn allergies-modal__btn--danger"
                          onClick={confirmDeactivate}
                          disabled={saving || reason.trim().length === 0}
                          title={reason.trim().length === 0 ? 'Give the reason first' : undefined}
                        >
                          Deactivate {a.substance}
                        </button>
                      </div>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}

          {canWrite ? (
            <form
              className="allergies-modal__add"
              aria-labelledby="allergies-modal-add-title"
              onSubmit={e => { e.preventDefault(); void add() }}
            >
              <h3 id="allergies-modal-add-title" className="allergies-modal__add-title">Add an allergy</h3>
              <div className="allergies-modal__fields">
                <div className="allergies-modal__field">
                  <label className="allergies-modal__label" htmlFor="allergy-substance">Substance</label>
                  <input
                    id="allergy-substance"
                    className="allergies-modal__input"
                    type="text"
                    value={substance}
                    onChange={e => setSubstance(e.target.value)}
                    placeholder="e.g. penicillin"
                    autoComplete="off"
                    disabled={saving}
                  />
                </div>
                <div className="allergies-modal__field">
                  <label className="allergies-modal__label" htmlFor="allergy-reaction">Reaction</label>
                  <input
                    id="allergy-reaction"
                    className="allergies-modal__input"
                    type="text"
                    value={reaction}
                    onChange={e => setReaction(e.target.value)}
                    placeholder="e.g. anaphylaxis, rash"
                    autoComplete="off"
                    disabled={saving}
                  />
                </div>
                <div className="allergies-modal__field">
                  <label className="allergies-modal__label" htmlFor="allergy-severity">Severity</label>
                  <select
                    id="allergy-severity"
                    className="allergies-modal__input"
                    value={severity}
                    onChange={e => setSeverity(e.target.value as AllergySeverity)}
                    disabled={saving}
                  >
                    {ALLERGY_SEVERITIES.map(s => <option key={s} value={s}>{SEVERITY_WORDS[s]}</option>)}
                  </select>
                </div>
              </div>
              <p className="allergies-modal__muted">
                Added to the structured list every safety check reads, from now.
              </p>
              <div className="allergies-modal__actions">
                <button type="submit" className="allergies-modal__btn allergies-modal__btn--primary" disabled={!canAdd}
                  title={substance.trim() ? undefined : 'Name the substance first'}>
                  {saving ? 'Saving…' : 'Add allergy'}
                </button>
              </div>
            </form>
          ) : (
            <p className="allergies-modal__muted">
              Read only: adding or deactivating an allergy is the prescriber's record.
            </p>
          )}
        </div>
      </div>
    </div>,
    document.body,
  )
}
