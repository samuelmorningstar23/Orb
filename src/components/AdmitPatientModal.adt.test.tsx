/**
 * Admit modal with Beds and admissions (adt-5). What it must never get wrong:
 *
 * 1. THE PICKER SHOWS ONLY WHEN flags.adt IS TRUE. With the module flag off
 *    the form keeps its typed ward and bed even if the server sent bed setup,
 *    and with the flag on but no bed setup in the answer it falls back to the
 *    typed ward too.
 * 2. THE PLACE COMES FROM THE PICKER. With the picker the admission is sent
 *    with the ward and bed ids, the master's spelling of the ward and bed,
 *    and the vitals exactly as before (ACVPU and O2 still null when not
 *    assessed).
 * 3. ONE IDEMPOTENCY-KEY PER FORM. A retried send carries the same key, so
 *    the server can never record the patient twice.
 * 4. THE SERVER'S WORDS. A bed refusal is shown by the picker as sent and the
 *    beds are read again; a replayed admission says the password cannot be
 *    shown again.
 * 5. BACK-TIME. An admission time further back than the server's window
 *    cannot be reviewed.
 * 6. A SIGNING PIN IS SENT ONCE. The PIN typed by the person authorising a
 *    bed awaiting a terminal clean goes with the send and is cleared at once,
 *    so a refusal or a retry needs it typed again.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetModuleFlags, setModuleFlags } from '../shellConfig'

const admitPatient = vi.hoisted(() => vi.fn())
const apiFetch = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({
  admitPatient: (...a: unknown[]) => admitPatient(...a),
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}))
vi.mock('../App', () => ({
  useAuth: () => ({
    user: { id: 'D001', name: 'Dr. Test', role: 'doctor', department: 'General Medicine' },
  }),
}))

import AdmitPatientModal from './AdmitPatientModal'

const MASTER = {
  backtime_hours: 72,
  authorisers: [{ id: 'N001', name: 'Nurse Test', role: 'nurse', department: 'General Medicine' }],
  wards: [{
    id: 'WRD-GM', code: 'GM-1', name: 'General ward one', department: 'General Medicine',
    ward_type: 'general', cleaning_tracked: true, register_complete: false,
    beds: [
      {
        bed_id: 'BED-7', bed_label: '7', unit_code: 'GM-1/7', room: null, bed_class: null,
        isolation: false, temporary: false, status: 'available', status_words: 'Available',
        tone: 'ready', markers: [], occupied: false, selectable: true, needs: null,
        terminal_clean: false,
      },
      {
        bed_id: 'BED-8', bed_label: '8', unit_code: 'GM-1/8', room: null, bed_class: null,
        isolation: true, temporary: false, status: 'needs_cleaning', status_words: 'Needs cleaning',
        tone: 'attention', markers: [], occupied: false, selectable: true,
        needs: 'terminal_clean_authorisation', terminal_clean: true, out_of_service: false,
      },
    ],
  }],
}

const BASE_OPTIONS = {
  departments: ['Cardiology', 'General Medicine'],
  default_department: 'General Medicine',
  doctors: [{ id: 'D001', name: 'Dr. Test', department: 'General Medicine' }],
  wards: [{ ward: 'GM', departments: ['General Medicine'] }],
  beds: [],
}

const ADMITTED = {
  success: true, patient_id: 'P1', admission_id: 'ADM-1', channel_id: 'c1', mrn: 'M1',
  patient_login_id: 'PT123', temporary_password: 'x', date_of_birth_recorded: true,
  bed_warning: null, replayed: false, location_warning: null,
}

let options: Record<string, unknown> = BASE_OPTIONS
let admitReplies: Array<() => Promise<unknown>> = []

function admitCalls() {
  return apiFetch.mock.calls.filter(([path]) => path === '/admissions/admit')
}

function optionCalls() {
  return apiFetch.mock.calls.filter(([path]) => path === '/admissions/options')
}

beforeEach(() => {
  resetModuleFlags()
  options = BASE_OPTIONS
  admitReplies = []
  admitPatient.mockReset()
  admitPatient.mockResolvedValue(ADMITTED)
  apiFetch.mockReset()
  apiFetch.mockImplementation((path: string) => {
    if (path === '/admissions/options') return Promise.resolve(options)
    if (path === '/admissions/admit') {
      const next = admitReplies.shift()
      return next ? next() : Promise.resolve(ADMITTED)
    }
    return Promise.reject(new Error(`unexpected ${path}`))
  })
})

afterEach(() => {
  cleanup()
  resetModuleFlags()
})

async function fillIdentityAndOpenAdmission(container: HTMLElement) {
  const name = container.querySelector('input.admit-input[type="text"]') as HTMLInputElement
  fireEvent.change(name, { target: { value: 'Asha Menon' } })
  const age = container.querySelector('input.admit-input[type="number"]') as HTMLInputElement
  fireEvent.change(age, { target: { value: '61' } })
  fireEvent.change(container.querySelector('select.admit-input') as HTMLSelectElement, { target: { value: 'F' } })
  fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
  await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001'))
}

function fillClinical(container: HTMLElement) {
  fireEvent.click(screen.getByRole('button', { name: /^Clinical/ }))
  fireEvent.change(container.querySelector('textarea.admit-input') as HTMLTextAreaElement, {
    target: { value: 'Fever and cough' },
  })
}

function chooseBedSeven() {
  fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-GM' } })
  fireEvent.click(screen.getByRole('radio', { name: /Bed 7/ }))
}

async function reviewAndConfirm() {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
}

describe('AdmitPatientModal with Beds and admissions', () => {
  it('keeps the typed ward while the module flag is off, even when bed setup was sent', async () => {
    options = { ...BASE_OPTIONS, master: MASTER }
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillIdentityAndOpenAdmission(container)
    expect(screen.queryByRole('group', { name: /Ward and bed/ })).toBeNull()
    expect(screen.queryByText('Ward and bed')).toBeNull()
    expect(container.querySelector('#admit-ward')).not.toBeNull()
    expect(screen.queryByLabelText('Admitted at')).toBeNull()

    fireEvent.change(container.querySelector('#admit-ward') as HTMLInputElement, { target: { value: 'GM' } })
    fillClinical(container)
    await reviewAndConfirm()
    expect(admitPatient).toHaveBeenCalledTimes(1)
    expect(admitCalls()).toHaveLength(0)
    const sent = admitPatient.mock.calls[0][0]
    expect(sent.ward).toBe('GM')
    expect(sent).not.toHaveProperty('ward_id')
    expect(sent).not.toHaveProperty('bed_id')
  })

  it('falls back to the typed ward when the flag is on but no bed setup was sent', async () => {
    setModuleFlags({ adt: true })
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillIdentityAndOpenAdmission(container)
    expect(screen.queryByText('Ward and bed')).toBeNull()
    expect(container.querySelector('#admit-ward')).not.toBeNull()
  })

  it('shows the bed picker when the flag is on and sends the place, the vitals and the key', async () => {
    setModuleFlags({ adt: true })
    options = { ...BASE_OPTIONS, master: MASTER }
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillIdentityAndOpenAdmission(container)
    expect(screen.getByText('Ward and bed')).toBeInTheDocument()
    expect(container.querySelector('#admit-ward')).toBeNull()
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeDisabled()

    chooseBedSeven()
    fillClinical(container)
    fireEvent.click(screen.getByRole('button', { name: /^Vitals/ }))
    const hr = container.querySelector('.admit-modal__section input[type="number"]') as HTMLInputElement
    fireEvent.change(hr, { target: { value: '96' } })
    await reviewAndConfirm()

    expect(admitPatient).not.toHaveBeenCalled()
    expect(admitCalls()).toHaveLength(1)
    const [, init] = admitCalls()[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    const headers = init.headers as Record<string, string>
    expect(headers['Idempotency-Key']).toMatch(/\S{8,}/)
    const body = JSON.parse(String(init.body))
    expect(body).toMatchObject({ ward: 'GM-1', bed: '7', ward_id: 'WRD-GM', bed_id: 'BED-7' })
    expect(body).not.toHaveProperty('awaiting_bed')
    expect(body).not.toHaveProperty('admitted_at')
    expect(body.vitals.hr).toBe(96)
    expect(body.vitals.consciousness).toBeNull()
    expect(body.vitals.on_oxygen).toBeNull()
  })

  it('sends No bed yet as awaiting a bed on the chosen ward', async () => {
    setModuleFlags({ adt: true })
    options = { ...BASE_OPTIONS, master: MASTER }
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillIdentityAndOpenAdmission(container)
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-GM' } })
    fireEvent.click(screen.getByRole('radio', { name: /No bed yet/ }))
    fillClinical(container)
    await reviewAndConfirm()
    const body = JSON.parse(String((admitCalls()[0] as [string, RequestInit])[1].body))
    expect(body).toMatchObject({ ward: 'GM-1', bed: '', ward_id: 'WRD-GM', awaiting_bed: true })
    expect(body).not.toHaveProperty('bed_id')
  })

  it('sends the same Idempotency-Key again when a send is retried', async () => {
    setModuleFlags({ adt: true })
    options = { ...BASE_OPTIONS, master: MASTER }
    admitReplies = [
      () => Promise.reject(Object.assign(new Error('Could not reach the server'), { status: 0 })),
      () => Promise.resolve(ADMITTED),
    ]
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillIdentityAndOpenAdmission(container)
    chooseBedSeven()
    fillClinical(container)
    await reviewAndConfirm()
    expect(await screen.findByText('Could not reach the server')).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
    expect(admitCalls()).toHaveLength(2)
    const keys = admitCalls().map(([, init]) =>
      ((init as RequestInit).headers as Record<string, string>)['Idempotency-Key'])
    expect(keys[0]).toBe(keys[1])
  })

  it('shows a bed refusal in the server words by the picker and reads the beds again', async () => {
    setModuleFlags({ adt: true })
    options = { ...BASE_OPTIONS, master: MASTER }
    const words = 'Bed 7 on GM-1 already holds a patient. Choose another bed, or add an extra bed with its own label.'
    admitReplies = [() => Promise.reject(Object.assign(new Error(words), {
      status: 409, detail: { error: 'bed_occupied', message: words, extra_bed_offered: true },
    }))]
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillIdentityAndOpenAdmission(container)
    chooseBedSeven()
    fillClinical(container)
    const before = optionCalls().length
    await reviewAndConfirm()
    await waitFor(() => expect(optionCalls().length).toBeGreaterThan(before))
    fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
    const picker = screen.getByText('Ward and bed').closest('fieldset') as HTMLElement
    expect(within(picker).getByRole('alert')).toHaveTextContent(words)
  })

  it('says a replayed admission was not recorded twice and shows no password', async () => {
    setModuleFlags({ adt: true })
    options = { ...BASE_OPTIONS, master: MASTER }
    admitReplies = [() => Promise.resolve({
      ...ADMITTED, replayed: true, temporary_password: null,
    })]
    const onSuccess = vi.fn()
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={onSuccess} />)
    await fillIdentityAndOpenAdmission(container)
    chooseBedSeven()
    fillClinical(container)
    await reviewAndConfirm()
    expect(await screen.findByText(/already recorded when the form was first sent/)).toBeInTheDocument()
    expect(onSuccess).not.toHaveBeenCalled()
  })

  it('refuses an admission time further back than the server window', async () => {
    setModuleFlags({ adt: true })
    options = { ...BASE_OPTIONS, master: MASTER }
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillIdentityAndOpenAdmission(container)
    chooseBedSeven()
    fillClinical(container)
    fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
    const old = new Date(Date.now() - 80 * 3_600_000)
    const pad = (n: number) => String(n).padStart(2, '0')
    const local = `${old.getFullYear()}-${pad(old.getMonth() + 1)}-${pad(old.getDate())}T${pad(old.getHours())}:${pad(old.getMinutes())}`
    fireEvent.change(screen.getByLabelText('Admitted at'), { target: { value: local } })
    expect(screen.getByText('The admission time can be entered up to 72 hours back.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeDisabled()
  })

  it('sends the authoriser signing PIN once and clears it from the form', async () => {
    setModuleFlags({ adt: true })
    options = { ...BASE_OPTIONS, master: MASTER }
    const words = 'The signing PIN for Nurse Test is not right. Nothing was saved.'
    admitReplies = [() => Promise.reject(Object.assign(new Error(words), {
      status: 403, detail: { error: 'terminal_clean_pin_wrong', message: words },
    }))]
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillIdentityAndOpenAdmission(container)
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-GM' } })
    fireEvent.click(screen.getByRole('radio', { name: /Bed 8/ }))
    fireEvent.change(screen.getByLabelText('Authorised by'), { target: { value: 'N001' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Only isolation bed' } })
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Their signing PIN/), { target: { value: '2468' } })
    fillClinical(container)
    await reviewAndConfirm()
    const body = JSON.parse(String((admitCalls()[0] as [string, RequestInit])[1].body))
    expect(body).toMatchObject({ bed_id: 'BED-8', terminal_clean_authorised_by: 'N001',
      terminal_clean_reason: 'Only isolation bed', terminal_clean_authoriser_pin: '2468' })
    fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
    expect(screen.getByLabelText(/Their signing PIN/)).toHaveValue('')
    const picker = screen.getByText('Ward and bed').closest('fieldset') as HTMLElement
    expect(within(picker).getByRole('alert')).toHaveTextContent(words)
  })
})
