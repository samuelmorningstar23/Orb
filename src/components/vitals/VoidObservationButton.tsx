import { useRef, useState } from 'react'
import { voidObservation } from '../../api/nurse'
import { hasPermission, VITALS_WRITE } from '../../lib/roles'
import { assertOnlineForWrite } from '../../lib/offline/writeGuard'
import './VoidObservationButton.css'

/**
 * "Entered in error" on one recorded observation set (Orb Ward Record,
 * 6 October 2026).
 *
 * Asks for a reason, then calls POST /api/nurse/observations/{id}/void. The set
 * stays on the record, struck out, and stops being anyone's latest reading.
 * Offered only to an account holding vitals.write (nurse, doctor): the ward
 * clerk copies sets but never strikes one out, and the server refuses her
 * anyway. Hiding the control is a courtesy; the server is the gate.
 */
interface Props {
  vitalsId: number
  /** Called after the server has marked the set, so the caller can reload. */
  onVoided?: () => void
  /** What the set is, for the screen reader label, e.g. "the 14:00 observations". */
  what?: string
}

export default function VoidObservationButton({ vitalsId, onVoided, what = 'this observation set' }: Props) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const inFlight = useRef(false)

  if (!hasPermission(VITALS_WRITE)) return null

  const submit = async () => {
    if (inFlight.current) return
    if (!reason.trim()) {
      setError('Say why this set is wrong.')
      return
    }
    inFlight.current = true
    setSaving(true)
    setError('')
    try {
      assertOnlineForWrite('mark this set entered in error')
      await voidObservation(vitalsId, reason.trim())
      setOpen(false)
      setReason('')
      onVoided?.()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not mark this set entered in error')
    } finally {
      inFlight.current = false
      setSaving(false)
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        className="void-obs__trigger"
        onClick={() => setOpen(true)}
        aria-label={`Mark ${what} entered in error`}
      >
        Entered in error
      </button>
    )
  }

  return (
    <div className="void-obs__form" role="group" aria-label={`Mark ${what} entered in error`}>
      <label htmlFor={`void-obs-${vitalsId}`}>Why is this set wrong?</label>
      <textarea
        id={`void-obs-${vitalsId}`}
        value={reason}
        onChange={e => setReason(e.target.value)}
        maxLength={500}
        required
        disabled={saving}
        placeholder="For example: wrong patient, or heart rate typed as 182, sheet says 82"
      />
      {error && <span className="void-obs__error" role="alert">{error}</span>}
      <div className="void-obs__actions">
        <button type="button" className="nurse-btn nurse-btn--danger" onClick={submit} disabled={saving}>
          {saving ? 'Saving…' : 'Mark entered in error'}
        </button>
        <button
          type="button"
          className="nurse-btn nurse-btn--ghost"
          onClick={() => { setOpen(false); setReason(''); setError('') }}
          disabled={saving}
        >
          Cancel
        </button>
      </div>
    </div>
  )
}
