/**
 * The nursing note sheet (21 October 2026): reads the patient's earlier notes
 * newest first, refuses an empty or over-long note before it is sent, sends
 * the trimmed text, and hands the saved note back to the board.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const listNursingNotes = vi.fn()
const writeNursingNote = vi.fn()

vi.mock('../../../api/nursingNotes', async () => {
  const real = await vi.importActual<typeof import('../../../api/nursingNotes')>('../../../api/nursingNotes')
  return {
    ...real,
    listNursingNotes: (...a: unknown[]) => listNursingNotes(...a),
    writeNursingNote: (...a: unknown[]) => writeNursingNote(...a),
  }
})

import NursingNoteSheet from './NursingNoteSheet'
import { NURSING_NOTE_MAX_CHARACTERS } from '../../../api/nursingNotes'

const PATIENT = { patient_id: 'P77', name: 'Meena Pillai', bed: '31', mrn: 'MRN-31' }
const EARLIER = [
  { id: 2, patient_id: 'P77', author_id: 'NUR002', author_name: 'Sister Anu', shift_date: '2026-10-21',
    shift_label: '15:00 to 23:00', content: 'Second note.', written_at: '2026-10-21T10:30:00+00:00' },
  { id: 1, patient_id: 'P77', author_id: 'NUR001', author_name: null, shift_date: '2026-10-21',
    shift_label: '07:00 to 15:00', content: 'First note.', written_at: '2026-10-21T04:20:00+00:00' },
]

beforeEach(() => {
  listNursingNotes.mockReset()
  writeNursingNote.mockReset()
  listNursingNotes.mockResolvedValue({ patient_id: 'P77', notes: EARLIER })
  localStorage.setItem('orb_user', JSON.stringify({ id: 'NUR001', name: 'Nurse A', role: 'nurse' }))
})

afterEach(() => cleanup())

function renderSheet() {
  const onWritten = vi.fn()
  const onClose = vi.fn()
  render(<NursingNoteSheet patient={PATIENT} onClose={onClose} onWritten={onWritten} />)
  return { onWritten, onClose }
}

describe('the nursing note sheet', () => {
  it('lists the earlier notes newest first, naming the author or falling back to the id', async () => {
    renderSheet()
    await screen.findByText('Second note.')
    const items = Array.from(document.querySelectorAll('.nursing-note__item')).map(li => li.textContent)
    expect(items[0]).toContain('15:00 to 23:00')
    expect(items[0]).toContain('Sister Anu')
    expect(items[1]).toContain('NUR001')
    expect(items[1]).toContain('First note.')
  })

  it('refuses an empty note and an over-long one before anything is sent', async () => {
    renderSheet()
    const save = () => screen.getByRole('button', { name: 'Save note' }) as HTMLButtonElement
    expect(save().disabled).toBe(true)
    const box = screen.getByLabelText('What happened this shift') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: '   ' } })
    expect(save().disabled).toBe(true)
    fireEvent.change(box, { target: { value: 'x'.repeat(NURSING_NOTE_MAX_CHARACTERS + 1) } })
    expect(save().disabled).toBe(true)
    expect(screen.getByText(/Shorten the note to save it/)).toBeTruthy()
    await act(async () => { fireEvent.click(save()) })
    expect(writeNursingNote).not.toHaveBeenCalled()
  })

  it('sends the trimmed note and hands the saved note back', async () => {
    const saved = { id: 3, patient_id: 'P77', author_id: 'NUR001', author_name: 'Nurse A',
      shift_date: '2026-10-21', shift_label: '07:00 to 15:00', content: 'Drinking well.',
      written_at: '2026-10-21T05:00:00+00:00' }
    writeNursingNote.mockResolvedValue(saved)
    const { onWritten } = renderSheet()
    fireEvent.change(screen.getByLabelText('What happened this shift'), { target: { value: '  Drinking well.  ' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save note' })) })
    expect(writeNursingNote).toHaveBeenCalledWith('P77', 'Drinking well.')
    await waitFor(() => expect(onWritten).toHaveBeenCalledWith(saved))
  })

  it('shows the server\'s refusal and keeps the text when the write fails', async () => {
    writeNursingNote.mockRejectedValue(Object.assign(new Error('Nursing notes are switched off on this computer'), { status: 404 }))
    const { onWritten } = renderSheet()
    fireEvent.change(screen.getByLabelText('What happened this shift'), { target: { value: 'Chest pain at 02:10.' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save note' })) })
    expect(await screen.findByRole('alert')).toHaveTextContent(/switched off on this computer/)
    expect((screen.getByLabelText('What happened this shift') as HTMLTextAreaElement).value).toBe('Chest pain at 02:10.')
    expect(onWritten).not.toHaveBeenCalled()
  })

  it('says when the earlier notes could not be read, without hiding the box', async () => {
    listNursingNotes.mockRejectedValue(Object.assign(new Error('nope'), { status: 403 }))
    renderSheet()
    expect(await screen.findByText('Your role cannot do this.')).toBeTruthy()
    expect(screen.getByLabelText('What happened this shift')).toBeTruthy()
  })
})
