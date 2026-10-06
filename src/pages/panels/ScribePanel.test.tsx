/**
 * SIGNING AN UNVERIFIED AI NOTE IS A DECISION, NOT A CRASH.
 *
 * In the shipped default config verification is OFF, so EVERY fresh note hits
 * the server's fail-closed 428 sign gate — and until this flow existed the
 * Sign button structurally failed with a generic error the clinician could do
 * nothing about. These tests pin the contract:
 *
 *   1. The first sign attempt never carries acknowledge_unverified — the
 *      server's gate decides.
 *   2. A 428 opens an explicit acknowledgment dialog that says why the gate
 *      fired (checking switched off, not finished, or failed; never "not
 *      verified against the audio" for a typed note) and whose confirm is
 *      type-to-confirm (eMAR AdministerModal pattern): no pre-checked box,
 *      no auto-acknowledge.
 *   3. Only the typed confirmation resends WITH acknowledge_unverified.
 *
 * Plus the honest ASR state: an appliance without the transcription engine
 * says so in clinician words, with the server's reason and the runbook behind
 * "Details for IT", instead of failing generically.
 *
 * Plus Quick Admission: offered only to patient.write, doctors from the server.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

vi.mock('../../api/client', () => ({
  transcribeAudio: vi.fn(),
  structureNote: vi.fn(),
  createPatient: vi.fn(),
  querySage: vi.fn(() => Promise.resolve('{}')),
  getScribeNoteVerification: vi.fn(),
  retryScribeNoteVerification: vi.fn(),
  editScribeNoteDraft: vi.fn(),
  signScribeNote: vi.fn(),
  rejectScribeNote: vi.fn(),
  reviseScribeNote: vi.fn(),
  getScribeAsrStatus: vi.fn(() =>
    Promise.resolve({ available: true, provider: 'whisper-mlx', model: 'large-v3', reason: null, runbook: 'docs/INSTALL_RUNBOOK.md: F14 (Scribe cannot transcribe)' }),
  ),
  getUser: vi.fn(() => ({ id: 'D001', name: 'Dr Test', role: 'doctor' })),
  getPatients: vi.fn(() => Promise.resolve([])),
}))

vi.mock('../../api/scribeAdmission', () => ({
  getQuickAdmitOptions: vi.fn(() => Promise.resolve({
    doctors: [
      { id: 'D001', name: 'Dr Test', department: 'General Medicine' },
      { id: 'D777', name: 'Dr Staff Table', department: 'Cardiology' },
    ],
    departments: ['Cardiology', 'General Medicine'],
    default_department: 'General Medicine',
  })),
  quickAdmit: vi.fn(),
}))

vi.mock('../../contexts/SageRuntimeContext', () => ({
  useSageRuntime: () => ({ claimMic: () => true, releaseMic: () => {} }),
}))

vi.mock('../../hooks/useStreamingTranscription', () => ({
  useStreamingTranscription: () => ({
    isStreaming: false,
    isSpeaking: false,
    isProcessing: false,
    audioLevel: 0,
    utteranceCount: 0,
    startStreaming: vi.fn(),
    stopStreaming: vi.fn(),
  }),
}))

import { createPatient, getScribeAsrStatus, getUser, signScribeNote, structureNote } from '../../api/client'
import { getQuickAdmitOptions, quickAdmit } from '../../api/scribeAdmission'
import ScribePanel from './ScribePanel'

// jsdom has no scrollIntoView; the panel auto-scrolls the transcript rail.
HTMLElement.prototype.scrollIntoView = vi.fn()

const gate428 = () =>
  Object.assign(new Error('This note did not pass automated hallucination verification.'), {
    status: 428,
    detail: {
      error: 'verification_unconfirmed',
      message: 'This note did not pass automated hallucination verification. Re-signing requires explicit acknowledgement.',
      verification_status: 'not_requested',
      reasons: ['verified=None'],
      requires: 'acknowledge_unverified',
    },
  })

/** Dictate-by-typing, structure, and open the note modal with note id 7. */
async function openStructuredNote() {
  ;(structureNote as Mock).mockImplementation(
    async (_t: string, _f: string, _p?: string, _a?: string, _v?: boolean, onChunk?: (s: string) => void) => {
      onChunk?.('S: chest pain')
      return { id: 7, structured_note: 'S: chest pain', format: 'soap', verification: { status: 'not_requested' } }
    },
  )
  render(<ScribePanel />)
  fireEvent.change(screen.getByPlaceholderText(/Type clinical notes/), {
    target: { value: 'patient reports chest pain' },
  })
  fireEvent.click(await screen.findByRole('button', { name: /Structure Note/ }))
  return screen.findByRole('button', { name: /Sign & Finalize/ })
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(getUser as Mock).mockReturnValue({ id: 'D001', name: 'Dr Test', role: 'doctor' })
  ;(getScribeAsrStatus as Mock).mockResolvedValue({
    available: true, provider: 'whisper-mlx', model: 'large-v3', reason: null,
    runbook: 'docs/INSTALL_RUNBOOK.md: F14 (Scribe cannot transcribe)',
  })
})

describe('sign gate 428 → explicit acknowledgment → signed', () => {
  it('walks the full flow and never auto-acknowledges', async () => {
    const signBtn = await openStructuredNote()
    ;(signScribeNote as Mock)
      .mockRejectedValueOnce(gate428())
      .mockResolvedValueOnce({ ok: true, note_id: 7, status: 'signed' })

    fireEvent.click(signBtn)

    // The 428 opened the acknowledgment dialog — and the first attempt did NOT
    // carry the acknowledgment flag.
    await screen.findByText(/Automated checking is switched off on this appliance/)
    expect(signScribeNote).toHaveBeenCalledTimes(1)
    expect(signScribeNote).toHaveBeenNthCalledWith(1, 7, undefined)
    // The gate's reasons are surfaced, not hidden — and in words.
    expect(screen.getByText('No verification verdict was recorded for this note.'))
      .toBeInTheDocument()

    // Deliberate confirm: disabled until the clinician types SIGN.
    const signAnyway = screen.getByRole('button', { name: /Sign anyway/ })
    expect(signAnyway).toBeDisabled()
    const confirmInput = screen.getByPlaceholderText(/Type "SIGN" to confirm/)
    fireEvent.change(confirmInput, { target: { value: 'nope' } })
    expect(signAnyway).toBeDisabled()
    fireEvent.change(confirmInput, { target: { value: 'sign' } })
    expect(signAnyway).toBeEnabled()

    fireEvent.click(signAnyway)
    await waitFor(() => expect(signScribeNote).toHaveBeenCalledTimes(2))
    expect(signScribeNote).toHaveBeenNthCalledWith(2, 7, true)

    // Dialog closes; the note renders signed.
    await waitFor(() =>
      expect(screen.queryByText(/Automated checking is switched off on this appliance/)).toBeNull(),
    )
    await screen.findByText(/✅ Signed/)
  })

  it('cancelling the dialog sends nothing and the note stays a draft', async () => {
    const signBtn = await openStructuredNote()
    ;(signScribeNote as Mock).mockRejectedValueOnce(gate428())

    fireEvent.click(signBtn)
    await screen.findByText(/Automated checking is switched off on this appliance/)

    fireEvent.click(screen.getByRole('button', { name: /^Cancel$/ }))
    await waitFor(() =>
      expect(screen.queryByText(/Automated checking is switched off on this appliance/)).toBeNull(),
    )
    // Only the original (un-acknowledged) attempt ever went out.
    expect(signScribeNote).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/📝 Draft/)).toBeInTheDocument()
  })
})

describe('honest ASR state', () => {
  it('tells the clinician in plain words and keeps diagnostics behind Details for IT', async () => {
    ;(getScribeAsrStatus as Mock).mockResolvedValue({
      available: false,
      provider: 'whisper-mlx',
      model: 'large-v3',
      reason: 'ASR runtime (lightning_whisper_mlx) is not installed in the server environment and no warm Whisper daemon is answering — audio cannot be transcribed on this appliance',
      runbook: 'docs/INSTALL_RUNBOOK.md: F14 (Scribe cannot transcribe)',
    })
    render(<ScribePanel />)

    const banner = await screen.findByTestId('scribe-asr-unavailable')
    expect(banner).toHaveTextContent(
      'Voice dictation is not available on this system. You can still type a note and structure it.')
    // The server's reason and the runbook are for IT, inside a disclosure, not
    // in the sentence the clinician reads.
    const details = screen.getByTestId('scribe-asr-it-details')
    expect(details.tagName).toBe('DETAILS')
    expect(details).toHaveTextContent('Details for IT')
    expect(details).toHaveTextContent(/INSTALL_RUNBOOK/)
    expect(details).toHaveTextContent(/lightning_whisper_mlx/)
    const lead = banner.textContent!.replace(details.textContent!, '')
    expect(lead).not.toMatch(/runbook|lightning|whisper|INSTALL/i)
    // The landing no longer promises real-time transcription.
    const hint = screen.getByTestId('scribe-landing-hint')
    expect(hint).not.toHaveTextContent(/real-time/)
    expect(hint).toHaveTextContent(/Voice dictation is not available on this system/)
    // Both mic paths are disabled with an explanation (the local engine is the
    // only transcription path, and it is not installed here).
    const micButtons = screen.getAllByTitle('Voice dictation is not available on this system')
    expect(micButtons).toHaveLength(2)
    micButtons.forEach(btn => expect(btn).toBeDisabled())
  })

  it('does not claim unavailability when the probe says available', async () => {
    render(<ScribePanel />)
    await screen.findByPlaceholderText(/Type clinical notes/)
    expect(screen.queryByTestId('scribe-asr-unavailable')).toBeNull()
    await waitFor(() =>
      expect(screen.getByTestId('scribe-landing-hint')).toHaveTextContent(/real-time/))
  })
})

describe('sign gate lead says why it fired', () => {
  const gate = (verification_status: string, reasons: string[]) =>
    Object.assign(new Error('gate'), {
      status: 428,
      detail: { error: 'verification_unconfirmed', verification_status, reasons,
                requires: 'acknowledge_unverified' },
    })

  it('a typed note on an appliance with checking off is not described as unverified audio', async () => {
    const signBtn = await openStructuredNote()
    ;(signScribeNote as Mock).mockRejectedValueOnce(gate('not_requested', ['verified=None']))
    fireEvent.click(signBtn)
    const lead = await screen.findByTestId('scribe-sign-gate-lead')
    expect(lead).toHaveTextContent(
      'Automated checking is switched off on this appliance. Read the note against what you dictated or typed before signing.')
    const dialog = screen.getByRole('dialog', { name: /Sign without verification/i })
    expect(dialog.textContent).not.toMatch(/audio/i)
  })

  it('a note that failed the check says so', async () => {
    const signBtn = await openStructuredNote()
    ;(signScribeNote as Mock).mockRejectedValueOnce(gate('complete', ['verified=False']))
    fireEvent.click(signBtn)
    const lead = await screen.findByTestId('scribe-sign-gate-lead')
    expect(lead).toHaveTextContent('This note did not pass the automated check.')
    expect(lead.textContent).not.toMatch(/switched off/)
  })
})

describe('quick admission', () => {
  async function structureOnly() {
    ;(structureNote as Mock).mockImplementation(
      async (_t: string, _f: string, _p?: string, _a?: string, _v?: boolean, onChunk?: (s: string) => void) => {
        onChunk?.('A: community-acquired pneumonia')
        return { id: 9, structured_note: 'A: community-acquired pneumonia', format: 'soap',
                 verification: { status: 'not_requested' } }
      },
    )
    render(<ScribePanel />)
    fireEvent.change(screen.getByPlaceholderText(/Type clinical notes/), {
      target: { value: 'fever and cough' },
    })
    fireEvent.click(await screen.findByRole('button', { name: /Structure Note/ }))
    await screen.findByRole('button', { name: /Sign & Finalize|Close/ })
  }

  it('is not offered to a role without patient.write', async () => {
    ;(getUser as Mock).mockReturnValue({ id: 'N001', name: 'Nurse Test', role: 'nurse' })
    await structureOnly()
    expect(screen.queryByRole('button', { name: /Admit to Vigil/ })).toBeNull()
  })

  it('offers doctors from the server and admits through the quick-admit route', async () => {
    ;(quickAdmit as Mock).mockResolvedValue({
      success: true, patient_id: 'PAT-1', admission_id: 'ADM-1', mrn: 'MRN-1',
      patient_login_id: 'P123456', temporary_password: 'once-only', date_of_birth_recorded: false,
    })
    await structureOnly()
    fireEvent.click(screen.getByRole('button', { name: /Admit to Vigil/ }))

    const attending = await screen.findByLabelText('Attending Doctor')
    await waitFor(() => expect(attending).toHaveValue('D001'))
    expect(screen.getByRole('option', { name: /Dr Staff Table/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Meera Sharma/ })).toBeNull()
    expect(screen.getByLabelText('Department')).toHaveValue('General Medicine')

    fireEvent.change(screen.getByLabelText('Patient Name'), { target: { value: 'Asha Rao' } })
    fireEvent.change(screen.getByLabelText('Age'), { target: { value: '61' } })
    fireEvent.change(screen.getByLabelText('Sex'), { target: { value: 'F' } })
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'GM-1' } })
    fireEvent.change(screen.getByLabelText('Bed Number'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('Presenting Complaint or Working Diagnosis'),
      { target: { value: 'Fever and cough' } })
    fireEvent.change(screen.getByLabelText('Known Allergies'), { target: { value: 'penicillin' } })
    fireEvent.click(screen.getByRole('button', { name: /Confirm Admission/ }))

    await screen.findByText('once-only')
    expect(quickAdmit).toHaveBeenCalledWith(expect.objectContaining({
      name: 'Asha Rao', age: 61, sex: 'F', department: 'General Medicine', ward: 'GM-1',
      attending_doctor_id: 'D001', allergies: 'penicillin',
    }))
    expect(createPatient).not.toHaveBeenCalled()
  })

  // A typed "cardiology" or "Cardiolgy" put the patient on nobody's ward board,
  // so the department is chosen from the server's covered list, never typed.
  it('picks the department from the covered list and prefills only a listed one', async () => {
    ;(getQuickAdmitOptions as Mock).mockResolvedValueOnce({
      doctors: [{ id: 'D001', name: 'Dr Test', department: 'General Medicine' }],
      departments: ['Cardiology'],
      default_department: null,
    })
    await structureOnly()
    fireEvent.click(screen.getByRole('button', { name: /Admit to Vigil/ }))

    const attending = await screen.findByLabelText('Attending Doctor')
    await waitFor(() => expect(attending).toHaveValue('D001'))
    const department = screen.getByLabelText('Department')
    expect(department.tagName).toBe('SELECT')
    // The doctor's own department is not covered here, so nothing is prefilled.
    expect(department).toHaveValue('')
    expect(Array.from((department as HTMLSelectElement).options).map(o => o.value))
      .toEqual(['', 'Cardiology'])
    fireEvent.change(department, { target: { value: 'Cardiology' } })
    expect(department).toHaveValue('Cardiology')
  })
})

/* ──────────────────────────────────────────────────────────────────────────
 * THE CEREMONY MAY NOT SPEAK PYTHON.
 *
 * The gate builds its reasons with an f-string — `f"verified={v.get(...)}"` —
 * so an unverified note arrives as the literal `verified=None`. That string
 * was printed inside the dialog that asks a doctor to type SIGN and accept
 * legal responsibility for an unchecked AI note. `None` is not a word a
 * clinician owes anyone; read plainly it looks like a field somebody forgot to
 * fill in, not like "this note was never checked at all".
 *
 * The wire values are untouched (the hash-chained audit entry for the override
 * still records them verbatim); only the render is translated.
 * ────────────────────────────────────────────────────────────────────────── */
describe('what the signing clinician is actually shown', () => {
  const gateWith = (reasons: string[]) =>
    Object.assign(new Error('This note did not pass automated hallucination verification.'), {
      status: 428,
      detail: {
        error: 'verification_unconfirmed',
        message: 'This note did not pass automated hallucination verification.',
        verification_status: 'not_requested',
        reasons,
        requires: 'acknowledge_unverified',
      },
    })

  async function openGate(reasons: string[]) {
    const signBtn = await openStructuredNote()
    ;(signScribeNote as Mock).mockRejectedValueOnce(gateWith(reasons))
    fireEvent.click(signBtn)
    return screen.findByRole('dialog', { name: /Sign without verification/i })
  }

  it('never puts "verified=None" in front of the doctor', async () => {
    const dialog = await openGate(['verified=None'])
    expect(dialog.textContent).not.toContain('verified=None')
    expect(dialog.textContent).not.toMatch(/\bNone\b/)
    expect(dialog.textContent).toContain('No verification verdict was recorded for this note.')
  })

  it('distinguishes a note never checked from one checked and failed', async () => {
    const dialog = await openGate(['verified=False'])
    expect(dialog.textContent).not.toContain('verified=False')
    expect(dialog.textContent)
      .toContain('The automated check did not confirm this note against what was dictated or typed.')
  })

  it('reads a warning bucket as words, and leaves a sentence alone', async () => {
    const dialog = await openGate([
      'warning: unsupported_medication',
      'no verification on record',
    ])
    expect(dialog.textContent).toContain('Warning: unsupported medication')
    expect(dialog.textContent).not.toContain('unsupported_medication')
    // A reason the gate already wrote as English is not re-worded.
    expect(dialog.textContent).toContain('no verification on record')
  })

  it('leaves no snake_case or key=value token anywhere in the ceremony', async () => {
    const dialog = await openGate(['verified=None', 'omission: high'])
    const body = dialog.textContent!
    expect(body).not.toMatch(/[a-z]+_[a-z]+/)
    expect(body).not.toMatch(/[a-z]+=[A-Za-z]/)
  })
})
