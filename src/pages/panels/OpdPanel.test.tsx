/**
 * The Outpatients tab (opd-8).
 *
 *  1. The doctor's queue renders in the server's order: in the room, waiting
 *     (priority, then token), skipped, not yet assigned; a priority shows its
 *     label; nothing on screen is a score, a band or a coloured vital.
 *  2. The queue actions post to the right routes: call next, call in, skip,
 *     back in line, take.
 *  3. Opening a record says "no allergies recorded" without claiming no known
 *     allergies, and never publishes the patient into ActiveViewContext.
 *  4. Finish visit needs an outcome, and a reason when the note is not signed
 *     (ruling OPD-N8).
 *  5. A content-free 'opd:queue_changed' nudge refetches the queue; a
 *     switched-off feature says so.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { useContext } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ActiveViewContext } from '../../contexts/ActiveViewContext'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'
import OpdPanel from './OpdPanel'

const h = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  handlers: new Map<string, (...args: unknown[]) => void>(),
}))

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => h.apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  // The signed-in doctor: Finish visit asks the role whether it may admit.
  getUser: () => ({ id: 'DOC-1', name: 'Dr Iyer', role: 'doctor' }),
  socket: {
    connected: true,
    on: (event: string, fn: (...args: unknown[]) => void) => { h.handlers.set(event, fn) },
    off: (event: string) => { h.handlers.delete(event) },
    emit: vi.fn(),
  },
}))

const DEPT = 'Cardiology'

function visit(id: string, token: number, extra: Record<string, unknown> = {}) {
  return {
    id, patient_id: `P-${id}`, patient_name: `Patient ${id}`, patient_mrn: `UHID-${id}`,
    patient_age: 50, patient_sex: 'F', department: DEPT, doctor_id: 'DOC-1',
    doctor_name: 'Dr Iyer', visit_date: '2026-10-05', token_number: token, visit_type: 'new',
    chief_complaint: 'cough', status: 'waiting', registered_at: '2026-10-05T03:00:00+00:00',
    called_at: null, completed_at: null, cancelled_at: null, cancel_reason: null,
    consultation_charge_id: null, notes: null, waiting_minutes: 12, consultation_minutes: null,
    priority: 'normal', priority_label: 'Normal', priority_reason: null, room: null,
    call_count: 0, skipped: false, skipped_at: null, cancel_reason_code: null,
    cancel_reason_label: null, disposition: null, disposition_at: null,
    consultation_status: null,
    ...extra,
  }
}

const QUEUE = {
  visit_date: '2026-10-05',
  department: DEPT,
  doctor_id: 'DOC-1',
  current: [visit('V-NOW', 1, { status: 'in_consultation', patient_name: 'Asha Now' })],
  waiting: [
    visit('V-EMG', 7, { priority: 'emergency', priority_label: 'Emergency',
      priority_reason: 'Chest pain at the counter', patient_name: 'Bimal Emergency', room: '2' }),
    visit('V-TWO', 2, { patient_name: 'Chitra Second' }),
  ],
  skipped: [visit('V-SKIP', 3, { skipped: true, skipped_at: '2026-10-05T04:00:00+00:00',
    patient_name: 'Deepa Skipped' })],
  unassigned: [visit('V-FREE', 4, { doctor_id: null, doctor_name: null, patient_name: 'Esha Free' })],
  completed_count: 5,
  total: 5,
  priorities: [{ code: 'normal', label: 'Normal' }, { code: 'emergency', label: 'Emergency' }],
  poll_seconds: 30,
}

const RECORD = {
  patient: { id: 'P-V-TWO', name: 'Chitra Second', uhid: 'UHID-V-TWO', age: 50,
    age_recorded_on: null, sex: 'F', preferred_language: null, record_status: 'registered',
    inpatient_now: false, date_of_birth_on_file: true },
  access: { arm: 'opd_visit', visit_id: 'V-TWO', visit_date: '2026-10-05', scope_until: '2026-10-12' },
  allergies: [],
  allergy_status: 'none_recorded',
  problems: [{ problem: 'Hypertension', icd10_code: 'I10', onset_date: null }],
  opd_visits: [{ id: 'V-TWO', department: DEPT, doctor_name: 'Dr Iyer', visit_date: '2026-10-05',
    visit_type: 'new', status: 'waiting', chief_complaint: 'cough', own_clinic: true }],
  results: [],
  consultations: [],
  consultations_available: true,
}

const NOTE_VIEW = {
  visit_id: 'V-TWO', patient_id: 'P-V-TWO', visit_status: 'in_consultation',
  consultation: {
    id: 'OPC-1', visit_id: 'V-TWO', patient_id: 'P-V-TWO', department: DEPT, author_id: 'DOC-1',
    author_name: 'Dr Iyer', status: 'draft', chief_complaint: 'Cough', history: null,
    examination: null,
    vitals: { values: { heart_rate: 88, systolic_bp: 142, diastolic_bp: 90, temperature: 37.4 },
      measured_at: '2026-10-05T04:10:00+00:00', measured_by: 'DOC-1', measured_by_name: 'Dr Iyer' },
    diagnoses: [], plan: null, advice: null, follow_up_date: null, follow_up_note: null,
    referral_department: null, admission_advised: false, row_version: 2,
    created_at: '2026-10-05T04:00:00+00:00', updated_at: '2026-10-05T04:12:00+00:00',
    signed_by: null, signed_by_name: null, signed_at: null, addenda: [],
  },
  allergy_review: { status: 'not_reviewed', can_sign: false, charted_count: 0 },
  patient_admitted: false,
}

const META = {
  feature: 'opd', enabled: true,
  settings: { scope_days: 7, charge_at: 'completion', queue_poll_seconds: 30 },
  dispositions: [{ code: 'home', label: 'Home' }, { code: 'follow_up', label: 'Follow-up visit' }],
}

function routeApi(overrides: Record<string, (options?: RequestInit) => unknown> = {}) {
  h.apiFetch.mockImplementation(async (path: string, options?: RequestInit) => {
    for (const [prefix, fn] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return fn(options)
    }
    if (path === '/opd/my-queue') return QUEUE
    if (path === '/opd/meta') return META
    if (path.startsWith('/opd/patients/')) return RECORD
    if (path.endsWith('/consultation')) return NOTE_VIEW
    if (path.startsWith('/frontdesk/opd/visits/') || path.startsWith('/opd/visits/')) {
      return visit('V-TWO', 2, { status: 'in_consultation' })
    }
    throw new Error(`unrouted ${path}`)
  })
}

function posted(): string[] {
  return h.apiFetch.mock.calls
    .filter(([, options]) => options && (options as RequestInit).method === 'POST')
    .map(([path]) => path as string)
}

beforeEach(() => {
  h.apiFetch.mockReset()
  h.handlers.clear()
})

afterEach(() => cleanup())

describe('the doctor queue', () => {
  it('renders the sections in the server order, with priority and no score', async () => {
    routeApi()
    const { container } = render(<OpdPanel />)
    await screen.findByText('Bimal Emergency')
    const waiting = screen.getByRole('region', { name: 'Waiting' })
    const names = within(waiting).getAllByText(/Bimal Emergency|Chitra Second/).map(n => n.textContent)
    expect(names).toEqual(['Bimal Emergency', 'Chitra Second'])
    expect(within(waiting).getByText('Emergency')).toHaveAttribute('title', 'Chest pain at the counter')
    expect(within(screen.getByRole('region', { name: 'With you now' })).getByText('Asha Now')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Skipped' })).getByText('Deepa Skipped')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Not yet assigned' })).getByText('Esha Free')).toBeInTheDocument()
    expect(screen.getByText(/Seen today: 5/)).toBeInTheDocument()
    const text = container.textContent ?? ''
    for (const word of ['NEWS2', 'News2', 'score', 'Score', 'band']) expect(text).not.toContain(word)
  })

  it('calls the next patient by token order, and each row action posts its route', async () => {
    routeApi()
    render(<OpdPanel />)
    await screen.findByText('Bimal Emergency')
    fireEvent.click(screen.getByRole('button', { name: 'Call next (token 7)' }))
    await waitFor(() => expect(posted()).toContain('/frontdesk/opd/visits/V-EMG/call'))

    const skipped = screen.getByRole('region', { name: 'Skipped' })
    fireEvent.click(within(skipped).getByRole('button', { name: 'Back in line' }))
    await waitFor(() => expect(posted()).toContain('/frontdesk/opd/visits/V-SKIP/recall'))

    const free = screen.getByRole('region', { name: 'Not yet assigned' })
    fireEvent.click(within(free).getByRole('button', { name: 'Take' }))
    await waitFor(() => expect(posted()).toContain('/frontdesk/opd/visits/V-FREE/assign'))

    const now = screen.getByRole('region', { name: 'With you now' })
    fireEvent.click(within(now).getByRole('button', { name: 'Skip' }))
    await waitFor(() => expect(posted()).toContain('/frontdesk/opd/visits/V-NOW/skip'))
  })

  it('says so when the outpatient feature is off', async () => {
    routeApi({
      '/opd/my-queue': () => {
        throw Object.assign(new Error('off'), { status: 404, detail: { error: 'feature_disabled' } })
      },
    })
    render(<OpdPanel />)
    expect(await screen.findByText(/Outpatients is switched off on this site/)).toBeInTheDocument()
  })

  it('refetches on the content-free queue nudge', async () => {
    routeApi()
    render(<OpdPanel />)
    await screen.findByText('Bimal Emergency')
    const before = h.apiFetch.mock.calls.filter(([p]) => p === '/opd/my-queue').length
    const nudge = h.handlers.get('opd:queue_changed')
    expect(nudge).toBeTypeOf('function')
    await act(async () => {
      nudge!({ department: DEPT })
      await new Promise(r => setTimeout(r, 450))
    })
    const after = h.apiFetch.mock.calls.filter(([p]) => p === '/opd/my-queue').length
    expect(after).toBe(before + 1)
  })
})

describe('taking a patient', () => {
  it('says the token changed when the take reissued it, from the answer, not the row', async () => {
    /* The desk's token 4 clashes with the doctor's own token 4, so the server
       answers token 9 and names the old number. */
    routeApi({
      '/frontdesk/opd/visits/V-FREE/assign': () => visit('V-FREE', 9, {
        patient_name: 'Esha Free', previous_token_number: 4, token_changed: true }),
    })
    render(<OpdPanel />)
    await screen.findByText('Esha Free')
    const free = screen.getByRole('region', { name: 'Not yet assigned' })
    fireEvent.click(within(free).getByRole('button', { name: 'Take' }))
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent(
      'Token changed from 4 to 9. Tell the patient: token 4 in your queue is another patient.')
    expect(screen.queryByText(/Token 4 is now in your queue/)).toBeNull()
  })

  it('names the token the server returned when the take kept it', async () => {
    routeApi({
      '/frontdesk/opd/visits/V-FREE/assign': () => visit('V-FREE', 4, {
        patient_name: 'Esha Free', previous_token_number: null, token_changed: false }),
    })
    render(<OpdPanel />)
    await screen.findByText('Esha Free')
    const free = screen.getByRole('region', { name: 'Not yet assigned' })
    fireEvent.click(within(free).getByRole('button', { name: 'Take' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Token 4 is now in your queue.')
    expect(screen.queryByText(/Token changed/)).toBeNull()
  })

  it('marks a queued patient whose slip shows an old token', async () => {
    routeApi({
      '/opd/my-queue': () => ({
        ...QUEUE,
        waiting: [visit('V-TAKEN', 2, { patient_name: 'Farah Taken', previous_token_number: 1 })],
      }),
    })
    render(<OpdPanel />)
    await screen.findByText('Farah Taken')
    const waiting = screen.getByRole('region', { name: 'Waiting' })
    expect(within(waiting).getByText('Slip says token 1')).toBeInTheDocument()
    expect(within(waiting).getByRole('button', { name: /Farah Taken, token 2, was token 1/ }))
      .toBeInTheDocument()
    expect(screen.queryAllByText(/Slip says token/)).toHaveLength(1)
  })
})

describe('the record and finishing a visit', () => {
  function SelectionProbe() {
    const { selection } = useContext(ActiveViewContext)
    return <output data-testid="selection">{selection ? JSON.stringify(selection) : 'none'}</output>
  }

  it('shows the record without claiming no known allergies, and publishes no patient', async () => {
    routeApi()
    render(<ActiveViewProvider><OpdPanel /><SelectionProbe /></ActiveViewProvider>)
    await screen.findByText('Chitra Second')
    fireEvent.click(screen.getByRole('button', { name: /Open the record of Chitra Second/ }))
    expect(await screen.findByText(/No allergies recorded\. This is not the same as no known allergies/))
      .toBeInTheDocument()
    expect(screen.getByText(/Open to your clinic until 12 Oct 2026/)).toBeInTheDocument()
    expect(screen.getByText(/Pulse 88 per min, BP 142\/90 mmHg, Temperature 37\.4 °C/)).toBeInTheDocument()
    expect(screen.getByText(/A note can be signed once the patient.s allergies are recorded/))
      .toBeInTheDocument()
    expect(screen.getByTestId('selection').textContent).toBe('none')
  })

  it('finishing an unsigned visit needs an outcome and a reason', async () => {
    let body: Record<string, unknown> | null = null
    routeApi({
      '/opd/visits/V-TWO/complete': options => {
        body = JSON.parse(String(options?.body))
        return visit('V-TWO', 2, { status: 'completed', disposition: 'home' })
      },
    })
    render(<OpdPanel />)
    await screen.findByText('Chitra Second')
    fireEvent.click(screen.getByRole('button', { name: /Open the record of Chitra Second/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Finish visit' }))
    const dialog = await screen.findByRole('dialog')
    const finish = within(dialog).getByRole('button', { name: 'Finish visit' })
    expect(finish).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Outcome'), { target: { value: 'home' } })
    expect(finish).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Why is there no signed note?'),
      { target: { value: 'Note written on paper' } })
    expect(finish).not.toBeDisabled()
    fireEvent.click(finish)
    await waitFor(() => expect(body).toEqual({ disposition: 'home', no_signed_note_reason: 'Note written on paper' }))
    expect(await screen.findByText(/Visit of Chitra Second finished/)).toBeInTheDocument()
  })
})

describe('a slow answer for an earlier patient', () => {
  type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void }
  function deferred<T>(): Deferred<T> {
    let resolve!: (v: T) => void
    let reject!: (e: unknown) => void
    const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
    return { promise, resolve, reject }
  }

  function recordFor(visitId: string, name: string, allergies: { substance: string;
    reaction: string | null; severity: string | null }[] = []) {
    return {
      ...RECORD,
      patient: { ...RECORD.patient, id: `P-${visitId}`, name, uhid: `UHID-${visitId}` },
      access: { ...RECORD.access, visit_id: visitId },
      allergies,
      allergy_status: allergies.length ? 'recorded' : 'none_recorded',
    }
  }

  function noteFor(visitId: string, signed: boolean) {
    return {
      ...NOTE_VIEW,
      visit_id: visitId,
      patient_id: `P-${visitId}`,
      consultation: {
        ...NOTE_VIEW.consultation, visit_id: visitId, patient_id: `P-${visitId}`,
        status: signed ? 'signed' : 'draft',
        signed_by: signed ? 'DOC-1' : null, signed_by_name: signed ? 'Dr Iyer' : null,
        signed_at: signed ? '2026-10-05T04:20:00+00:00' : null,
      },
      allergy_review: signed
        ? { status: 'recorded', can_sign: true, charted_count: 1 }
        : NOTE_VIEW.allergy_review,
    }
  }

  const recordPane = () => screen.getByRole('region', { name: 'Patient record' })

  it('drops the first patient\'s record and note when they land after the second patient\'s', async () => {
    // Patient A (Chitra, V-TWO) has a penicillin allergy and a signed note.
    // Patient B (Bimal, V-EMG) has no allergies recorded and a draft note.
    const aRecord = deferred<unknown>()
    const aNote = deferred<unknown>()
    routeApi({
      '/opd/patients/P-V-TWO/record': () => aRecord.promise,
      '/opd/visits/V-TWO/consultation': () => aNote.promise,
      '/opd/patients/P-V-EMG/record': () => recordFor('V-EMG', 'Bimal Emergency'),
      '/opd/visits/V-EMG/consultation': () => noteFor('V-EMG', false),
    })
    render(<OpdPanel />)
    await screen.findByText('Chitra Second')
    fireEvent.click(screen.getByRole('button', { name: /Open the record of Chitra Second/ }))
    fireEvent.click(screen.getByRole('button', { name: /Open the record of Bimal Emergency/ }))
    expect(await within(recordPane()).findByRole('heading', { name: 'Bimal Emergency' })).toBeInTheDocument()

    // A's answers arrive last, note first and record after it.
    await act(async () => {
      aNote.resolve(noteFor('V-TWO', true))
      await Promise.resolve()
      aRecord.resolve(recordFor('V-TWO', 'Chitra Second',
        [{ substance: 'Penicillin', reaction: 'anaphylaxis', severity: 'severe' }]))
      await new Promise(r => setTimeout(r, 0))
    })

    const pane = recordPane()
    expect(within(pane).getByRole('heading', { name: 'Bimal Emergency' })).toBeInTheDocument()
    expect(within(pane).queryByText('Chitra Second')).not.toBeInTheDocument()
    expect(within(pane).queryByText(/Penicillin/)).not.toBeInTheDocument()
    expect(within(pane).getByText(/No allergies recorded/)).toBeInTheDocument()
    expect(within(pane).queryByText(/Signed by/)).not.toBeInTheDocument()
    expect(within(pane).getByText(/Draft, version 2/)).toBeInTheDocument()
    expect(within(pane).getByText(/token 7/)).toBeInTheDocument()

    // Finishing B asks for B's reason: A's signed note never decides it.
    fireEvent.click(within(pane).getByRole('button', { name: 'Finish visit' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Finish the visit of Bimal Emergency')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Why is there no signed note?')).toBeInTheDocument()
  })

  it('ignores a late failure for the first patient once the second is open', async () => {
    const aRecord = deferred<unknown>()
    routeApi({
      '/opd/patients/P-V-TWO/record': () => aRecord.promise,
      '/opd/visits/V-TWO/consultation': () => noteFor('V-TWO', true),
      '/opd/patients/P-V-EMG/record': () => recordFor('V-EMG', 'Bimal Emergency'),
      '/opd/visits/V-EMG/consultation': () => noteFor('V-EMG', false),
    })
    render(<OpdPanel />)
    await screen.findByText('Chitra Second')
    fireEvent.click(screen.getByRole('button', { name: /Open the record of Chitra Second/ }))
    fireEvent.click(screen.getByRole('button', { name: /Open the record of Bimal Emergency/ }))
    await within(recordPane()).findByRole('heading', { name: 'Bimal Emergency' })
    await act(async () => {
      aRecord.reject(Object.assign(new Error('The server is busy.'), { status: 503 }))
      await new Promise(r => setTimeout(r, 0))
    })
    expect(within(recordPane()).queryByRole('alert')).not.toBeInTheDocument()
    expect(within(recordPane()).getByRole('heading', { name: 'Bimal Emergency' })).toBeInTheDocument()
  })

  it('refuses to draw a record or note that belongs to another patient or visit', async () => {
    routeApi({
      // The server answers with Chitra's record and note for Bimal's visit.
      '/opd/patients/P-V-EMG/record': () => recordFor('V-TWO', 'Chitra Second',
        [{ substance: 'Penicillin', reaction: null, severity: null }]),
      '/opd/visits/V-EMG/consultation': () => noteFor('V-TWO', true),
    })
    render(<OpdPanel />)
    await screen.findByText('Bimal Emergency')
    fireEvent.click(screen.getByRole('button', { name: /Open the record of Bimal Emergency/ }))
    expect(await within(recordPane()).findByRole('alert'))
      .toHaveTextContent(/not this patient.s\. Open the patient again/)
    const pane = recordPane()
    expect(within(pane).queryByText('Chitra Second')).not.toBeInTheDocument()
    expect(within(pane).queryByText(/Penicillin/)).not.toBeInTheDocument()
    expect(within(pane).queryByRole('button', { name: 'Finish visit' })).not.toBeInTheDocument()
  })

  it('a queue action that returns late does not swap the open patient back', async () => {
    const skip = deferred<unknown>()
    routeApi({
      '/frontdesk/opd/visits/V-NOW/skip': () => skip.promise,
      '/opd/patients/P-V-NOW/record': () => recordFor('V-NOW', 'Asha Now'),
      '/opd/visits/V-NOW/consultation': () => noteFor('V-NOW', false),
      '/opd/patients/P-V-EMG/record': () => recordFor('V-EMG', 'Bimal Emergency'),
      '/opd/visits/V-EMG/consultation': () => noteFor('V-EMG', false),
    })
    render(<OpdPanel />)
    await screen.findByText('Asha Now')
    fireEvent.click(screen.getByRole('button', { name: /Open the record of Asha Now/ }))
    await within(recordPane()).findByRole('heading', { name: 'Asha Now' })
    const now = screen.getByRole('region', { name: 'With you now' })
    fireEvent.click(within(now).getByRole('button', { name: 'Skip' }))
    fireEvent.click(screen.getByRole('button', { name: /Open the record of Bimal Emergency/ }))
    await within(recordPane()).findByRole('heading', { name: 'Bimal Emergency' })
    await act(async () => {
      skip.resolve(visit('V-NOW', 1, { status: 'waiting', skipped: true, patient_name: 'Asha Now' }))
      await new Promise(r => setTimeout(r, 0))
    })
    const pane = recordPane()
    expect(within(pane).getByRole('heading', { name: 'Bimal Emergency' })).toBeInTheDocument()
    expect(within(pane).getByText(/token 7/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Open the record of Bimal Emergency/ }))
      .toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: /Open the record of Asha Now/ }))
      .toHaveAttribute('aria-pressed', 'false')
  })
})

describe('what the panel never touches', () => {
  it('never imports the active patient or active view contexts', () => {
    const src = readFileSync(resolve(process.cwd(), 'src/pages/panels/OpdPanel.tsx'), 'utf8')
    const imports = src.split('\n').filter(line => line.startsWith('import'))
    for (const name of ['ActiveViewContext', 'ActivePatientContext', 'useActiveView', 'useActivePatient',
      'setViewSelection', 'SageFloatingAssistant']) {
      expect(imports.join('\n')).not.toContain(name)
    }
    expect(src).not.toMatch(/setViewSelection\(|setActivePatient/)
  })
})
