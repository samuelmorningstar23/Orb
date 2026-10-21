import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  listNursingNotes,
  writeNursingNote,
  NURSING_NOTE_MAX_CHARACTERS,
  type NursingNote,
} from '../../../api/nursingNotes'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import { assertOnlineForWrite } from '../../../lib/offline/writeGuard'
import { formatClinicalDateTime } from '../../../lib/datetime'
import type { ObsSheetPatient } from './ObsEntrySheet'
import './NursingNoteSheet.css'

/**
 * The per-shift nursing note (founding ward list item 4, 21 October 2026).
 *
 * Opened from a patient card on the shift board. One box, free text, saved
 * under the shift running now with the nurse's name and the time: the server
 * stamps all three, so nothing here picks a shift or a clock. Below the box
 * the patient's recent notes, newest first, so the incoming nurse reads what
 * the last shift wrote before she writes her own. Append only: a written
 * note is never edited here, as on paper.
 *
 * Offline is a refusal, not a delay: a note that may say "chest pain at
 * 02:10" must be in the record when she walks away, so save() throws the
 * same refusal the observation sheet does and she taps again in range.
 */
interface Props {
  patient: ObsSheetPatient
  onClose: () => void
  onWritten: (note: NursingNote) => void
}

type Recent =
  | { state: 'loading' }
  | { state: 'ready'; notes: NursingNote[] }
  | { state: 'error'; message: string }

function messageFor(err: unknown, fallback: string): string {
  const raw = (err ?? {}) as { status?: unknown; message?: unknown }
  const status = typeof raw.status === 'number' ? raw.status : 0
  if (status === 404) return 'Nursing notes are switched off on this computer, or this patient is not on your ward.'
  if (status === 403) return 'Your role cannot do this.'
  if (status === 0 && !(err instanceof Error && err.message)) return 'Could not reach the server. Check the connection and try again.'
  return typeof raw.message === 'string' && raw.message.trim() ? raw.message : fallback
}

export default function NursingNoteSheet({ patient, onClose, onWritten }: Props) {
  const [content, setContent] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [recent, setRecent] = useState<Recent>({ state: 'loading' })

  useEffect(() => {
    let live = true
    listNursingNotes(patient.patient_id).then(
      body => { if (live) setRecent({ state: 'ready', notes: body.notes }) },
      (err: unknown) => { if (live) setRecent({ state: 'error', message: messageFor(err, 'The earlier notes could not be read.') }) },
    )
    return () => { live = false }
  }, [patient.patient_id])

  const requestClose = useCallback(() => { if (!saving) onClose() }, [saving, onClose])
  const modalRef = useFocusTrap<HTMLDivElement>(requestClose)

  const trimmed = content.trim()
  const tooLong = trimmed.length > NURSING_NOTE_MAX_CHARACTERS
  const canSave = trimmed.length > 0 && !tooLong && !saving

  const save = async () => {
    if (!canSave) return
    setSaving(true)
    setError('')
    try {
      assertOnlineForWrite('write this nursing note')
      const note = await writeNursingNote(patient.patient_id, trimmed)
      onWritten(note)
    } catch (e) {
      setError(messageFor(e, e instanceof Error ? e.message : 'The note could not be saved.'))
      setSaving(false)
    }
  }

  return createPortal(
    <div className="nurse-sheet-backdrop" onClick={requestClose}>
      <div
        className="nurse-sheet glass-strong"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="nursing-note-title"
        onClick={e => e.stopPropagation()}
      >
        <header className="nurse-sheet__header">
          <div>
            <h2 className="nurse-sheet__title" id="nursing-note-title">Nursing note</h2>
            <p className="nurse-sheet__subject">
              <strong>{patient.name}</strong>
              {patient.bed ? <> · Bed {patient.bed}</> : null}
              {patient.mrn ? <> · {patient.mrn}</> : null}
            </p>
          </div>
          <button
            type="button"
            className="nurse-sheet__close"
            onClick={requestClose}
            aria-label="Close nursing note"
            disabled={saving}
          >
            &times;
          </button>
        </header>

        <div className="nurse-sheet__body">
          <p className="nursing-note__hint">
            Filed under the shift running now, with your name and the time. A saved note cannot be
            edited; write a new one to add to it.
          </p>

          {error && <div className="nurse-note nurse-note--danger" role="alert">{error}</div>}

          <label className="obs-field__label" htmlFor="nursing-note-content">What happened this shift</label>
          <textarea
            id="nursing-note-content"
            className="shift-input nursing-note__text"
            rows={6}
            value={content}
            onChange={e => setContent(e.target.value)}
            disabled={saving}
            placeholder="e.g. Settled after analgesia at 14:20. Drinking well. Dressing dry."
            aria-describedby="nursing-note-count"
            aria-invalid={tooLong}
            autoFocus
          />
          <p
            id="nursing-note-count"
            className={`nursing-note__count ${tooLong ? 'nursing-note__count--over' : ''}`}
          >
            {trimmed.length} of {NURSING_NOTE_MAX_CHARACTERS} characters
            {tooLong ? '. Shorten the note to save it.' : ''}
          </p>

          <section className="nursing-note__recent" aria-labelledby="nursing-note-recent-title">
            <h3 id="nursing-note-recent-title" className="nursing-note__recent-title">Earlier notes</h3>
            {recent.state === 'loading' && <p className="nursing-note__muted" role="status">Reading the earlier notes</p>}
            {recent.state === 'error' && <p className="nurse-note nurse-note--warn" role="status">{recent.message}</p>}
            {recent.state === 'ready' && recent.notes.length === 0 && (
              <p className="nursing-note__muted">No nursing notes yet for this patient.</p>
            )}
            {recent.state === 'ready' && recent.notes.length > 0 && (
              <ul className="nursing-note__list">
                {recent.notes.map(n => (
                  <li key={n.id} className="nursing-note__item">
                    <p className="nursing-note__meta">
                      {formatClinicalDateTime(n.written_at)} · {n.shift_label} · {n.author_name || n.author_id}
                    </p>
                    <p className="nursing-note__body">{n.content}</p>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>

        <footer className="nurse-sheet__actions">
          <button type="button" className="nurse-btn nurse-btn--ghost" onClick={requestClose} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="nurse-btn nurse-btn--primary"
            onClick={save}
            disabled={!canSave}
            title={trimmed.length === 0 ? 'Write the note first' : tooLong ? 'Shorten the note' : undefined}
          >
            {saving ? 'Saving…' : 'Save note'}
          </button>
        </footer>
      </div>
    </div>,
    document.body,
  )
}
