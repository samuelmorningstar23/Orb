/**
 * The outpatient prescription panel (opd-12).
 *
 *  1. OFF: with the medication-order store off, or on an Orb Ward Record
 *     computer (the drug chart stays on paper), the server answers 404 and the
 *     panel shows nothing at all.
 *  2. THE ALLERGY REVIEW COMES FIRST (ruling OPD-S7): no form until the review
 *     lets the note be signed; the panel offers the review instead.
 *  3. A LINE sends the medicine, dose, route and how often (the route is never
 *     filled in), an Idempotency-Key, and nothing invented; a severe alert
 *     opens the acknowledgement and the prescriber's reason is re-sent with
 *     the same key.
 *  4. WHO: a nurse sees the list but no form and no cancel; a finished visit
 *     offers nothing new.
 *  5. CANCEL needs a reason and says when a print already listed the line.
 *  6. PRINT reads the server's print answer.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AllergyReviewState } from '../../../api/opdAllergyReview'
import type { OpdPrescription } from '../../../api/opdPrescriptions'
import PrescriptionPanel from './PrescriptionPanel'

const h = vi.hoisted(() => ({ apiFetch: vi.fn(), role: 'doctor' }))

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => h.apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  getUser: () => ({ id: 'DOC-1', name: 'Dr Iyer', role: h.role }),
  isSafetyAckRequired: (err: unknown) => err instanceof Error
    && (err as Error & { code?: string }).code === 'safety_ack_required',
  socket: { connected: true, on: () => {}, off: () => {}, emit: vi.fn() },
}))

const REVIEWED: AllergyReviewState = {
  status: 'none_known', can_sign: true, charted_count: 0, reviewed_this_visit: true,
  basis: 'this_visit',
  assessment: { status: 'none_known', status_label: 'No known allergies',
    assessed_at: '2026-10-10T04:05:00+00:00' },
}
const NOT_REVIEWED: AllergyReviewState = {
  status: 'not_reviewed', can_sign: false, charted_count: 0, reviewed_this_visit: false,
  basis: null, assessment: null,
}

function line(extra: Partial<OpdPrescription> = {}): OpdPrescription {
  return {
    id: 'MO-1', opd_visit_id: 'V-1', drug_name: 'Paracetamol', dose: '500 mg', route: 'Oral',
    frequency: 'TDS', is_prn: false, prn_indication: null, duration_days: 5, quantity: 15,
    instructions: 'After food', indication: null, status: 'prescribed', status_label: 'Prescribed',
    live: true, version: 1, prescribed_at: '2026-10-10T04:20:00+00:00',
    prescriber_name: 'Dr Iyer', course_ends_at: '2026-10-15T04:20:00+00:00', cancel_reason: null,
    ended_at: null, ended_by_name: null, safety_overridden: false,
    ...extra,
  }
}

function httpError(status: number, detail: unknown): Error {
  const err = new Error(typeof detail === 'string' ? detail : 'refused') as Error & {
    status?: number; detail?: unknown }
  err.status = status
  err.detail = detail
  return err
}

function ackError(safety: unknown): Error {
  const err = new Error('Severe drug-safety alerts must be acknowledged') as Error & {
    code?: string; status?: number; safety?: unknown }
  err.code = 'safety_ack_required'
  err.status = 428
  err.safety = safety
  return err
}

type Handler = (options?: RequestInit) => unknown
function routeApi(lines: OpdPrescription[] | Error, overrides: Record<string, Handler> = {}) {
  h.apiFetch.mockImplementation(async (path: string, options?: RequestInit) => {
    const method = options?.method ?? 'GET'
    for (const [key, fn] of Object.entries(overrides)) {
      if (`${method} ${path}`.startsWith(key)) return fn(options)
    }
    if (method === 'GET' && path === '/opd/visits/V-1/prescriptions') {
      if (lines instanceof Error) throw lines
      return { visit_id: 'V-1', patient_id: 'P-1', prescriptions: lines }
    }
    throw new Error(`unrouted ${method} ${path}`)
  })
}

function posts(prefix: string) {
  return h.apiFetch.mock.calls.filter(([p, o]) => String(p).startsWith(prefix)
    && (o as RequestInit | undefined)?.method === 'POST')
}

function bodyOf(call: unknown[]): Record<string, unknown> {
  return JSON.parse(String((call[1] as RequestInit).body))
}

function headerOf(call: unknown[], name: string): string | undefined {
  return ((call[1] as RequestInit).headers as Record<string, string> | undefined)?.[name]
}

function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } })
}

let print: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  h.apiFetch.mockReset()
  h.role = 'doctor'
  print = vi.spyOn(window, 'print').mockImplementation(() => {})
})
afterEach(() => {
  cleanup()
  print.mockRestore()
})

describe('switched off', () => {
  it('shows nothing while the medication-order store is off', async () => {
    routeApi(httpError(404, { error: 'feature_disabled', feature: 'med_order_store' }))
    const { container } = render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    await waitFor(() => expect(container).toBeEmptyDOMElement())
  })

  it('shows nothing on an Orb Ward Record computer', async () => {
    routeApi(httpError(404, 'The drug chart is not part of Orb Ward Record'))
    const { container } = render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(container.textContent).not.toMatch(/medicine/i)
  })

  it('a visit the server cannot find is a failed load, not a switched-off site', async () => {
    routeApi(httpError(404, 'OPD visit not found'))
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    expect(await screen.findByText('The prescription on this visit could not be loaded.')).toBeInTheDocument()
  })

  it('says so when the list could not be loaded, and tries again', async () => {
    routeApi(httpError(500, 'boom'))
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    expect(await screen.findByText('The prescription on this visit could not be loaded.')).toBeInTheDocument()
    routeApi([])
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('No medicine prescribed.')).toBeInTheDocument()
  })
})

describe('the list', () => {
  it('shows each line in words, and a cancelled line with its reason', async () => {
    routeApi([line(), line({ id: 'MO-2', drug_name: 'Cetirizine', dose: '10 mg', frequency: 'OD',
      duration_days: null, quantity: null, instructions: null, status: 'cancelled',
      status_label: 'Cancelled', live: false, cancel_reason: 'Not needed',
      ended_by_name: 'Dr Iyer', ended_at: '2026-10-10T04:30:00+00:00' })])
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    const list = await screen.findByRole('list', { name: 'Medicines on this visit' })
    expect(within(list).getByText('500 mg, Oral, TDS, for 5 days, quantity 15')).toBeInTheDocument()
    expect(within(list).getByText('After food')).toBeInTheDocument()
    const cancelled = within(list).getByText('Cetirizine')
    expect(cancelled.className).toContain('opd-inv__name--cancelled')
    expect(within(list).getByText(/Reason: Not needed/)).toBeInTheDocument()
    // One cancel button: the live line only.
    expect(within(list).getAllByRole('button', { name: /^Cancel / })).toHaveLength(1)
  })
})

describe('the allergy review comes first (ruling OPD-S7)', () => {
  it('offers no form until the review lets the note be signed', async () => {
    routeApi([])
    const onReview = vi.fn()
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={NOT_REVIEWED}
      onReviewAllergies={onReview} />)
    await screen.findByText('No medicine prescribed.')
    expect(screen.queryByRole('form', { name: 'Prescribe a medicine' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Prescribe' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Record the review now' }))
    expect(onReview).toHaveBeenCalledTimes(1)
  })

  it('says so when the server still refuses for want of a review', async () => {
    routeApi([], {
      'POST /opd/visits/V-1/prescriptions': () => {
        throw httpError(409, { error: 'allergy_review_required', message: 'x' })
      },
    })
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    await screen.findByRole('button', { name: 'Prescribe' })
    fill('Medicine', 'Paracetamol')
    fill('Dose', '500 mg')
    fill('Route', 'Oral')
    fill('How often', 'TDS')
    fireEvent.click(screen.getByRole('button', { name: 'Prescribe' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Record the allergy review before prescribing')
  })
})

describe('writing a line', () => {
  it('needs the dose, route and how often, and never fills in the route', async () => {
    routeApi([])
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    await screen.findByRole('button', { name: 'Prescribe' })
    expect(screen.getByLabelText('Route')).toHaveValue('')
    fill('Medicine', 'Paracetamol')
    fill('Dose', '500 mg')
    fill('How often', 'TDS')
    fireEvent.click(screen.getByRole('button', { name: 'Prescribe' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Enter the dose, the route and how often it is taken.')
    expect(posts('/opd/visits/V-1/prescriptions')).toHaveLength(0)

    fireEvent.click(screen.getByLabelText('Only when needed'))
    fill('Route', 'Oral')
    fireEvent.click(screen.getByRole('button', { name: 'Prescribe' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Say when the patient should take it')
    expect(posts('/opd/visits/V-1/prescriptions')).toHaveLength(0)
  })

  it('sends only what was typed, with a request key, and lists the line', async () => {
    routeApi([], {
      'POST /opd/visits/V-1/prescriptions': () => ({
        prescription: line(), replayed: false,
        safety: { allergy_alerts: [], interaction_alerts: [], max_severity: 'none', blocking: false,
          interaction_screened: true, coverage_note: null },
        duplicates: [{ med_order_id: 'MO-0', medication_id: null, drug_name: 'Paracetamol 650 mg',
          status: 'prescribed' }],
      }),
    })
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    await screen.findByRole('button', { name: 'Prescribe' })
    fill('Medicine', 'Paracetamol')
    fill('Dose', '500 mg')
    fill('Route', 'Oral')
    fill('How often', 'TDS')
    fill('Duration in days', '5')
    fill('Quantity', '15')
    fill('Instructions to the patient', 'After food')
    fireEvent.click(screen.getByRole('button', { name: 'Prescribe' }))
    expect(await screen.findByText(/Paracetamol prescribed\./)).toBeInTheDocument()
    expect(screen.getByText(/Already prescribed or on the chart: Paracetamol 650 mg/)).toBeInTheDocument()
    const [call] = posts('/opd/visits/V-1/prescriptions')
    expect(bodyOf(call)).toEqual({ drug_name: 'Paracetamol', dose: '500 mg', route: 'Oral',
      frequency: 'TDS', duration_days: 5, quantity: 15, instructions: 'After food' })
    expect(headerOf(call, 'Idempotency-Key')).toBeTruthy()
    expect(screen.getByText('500 mg, Oral, TDS, for 5 days, quantity 15')).toBeInTheDocument()
    // No sub-severe alert, so no review dialog.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens the acknowledgement on a severe alert and re-sends the reason with the same key', async () => {
    const safety = {
      allergy_alerts: [{ severity: 'severe', message: 'Penicillin allergy (anaphylaxis)' }],
      interaction_alerts: [], max_severity: 'severe', blocking: true, interaction_screened: true,
      coverage_note: null, override_requires_permission: 'medication.prescribe',
    }
    let attempt = 0
    routeApi([], {
      'POST /opd/visits/V-1/prescriptions': () => {
        attempt += 1
        if (attempt === 1) throw ackError(safety)
        return { prescription: line({ drug_name: 'Amoxicillin', safety_overridden: true }),
          replayed: false, safety: { ...safety, override: { by: 'DOC-1', by_name: 'Dr Iyer',
            reason: 'Tolerated it twice since' } }, duplicates: [] }
      },
    })
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    await screen.findByRole('button', { name: 'Prescribe' })
    fill('Medicine', 'Amoxicillin')
    fill('Dose', '500 mg')
    fill('Route', 'Oral')
    fill('How often', 'TDS')
    fireEvent.click(screen.getByRole('button', { name: 'Prescribe' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Penicillin allergy (anaphylaxis)')).toBeInTheDocument()
    expect(screen.queryByText('Amoxicillin prescribed.')).not.toBeInTheDocument()
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'Tolerated it twice since' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /acknowledge/i }))
    expect(await screen.findByText('Amoxicillin prescribed.')).toBeInTheDocument()
    const [first, second] = posts('/opd/visits/V-1/prescriptions')
    expect(bodyOf(first).safety_ack).toBeUndefined()
    expect(bodyOf(second).safety_ack).toEqual({ acknowledged: true, reason: 'Tolerated it twice since' })
    expect(headerOf(second, 'Idempotency-Key')).toBe(headerOf(first, 'Idempotency-Key'))
    expect(screen.getByText('A drug-safety alert was overridden with a reason.')).toBeInTheDocument()
  })
})

describe('who and when', () => {
  it('a nurse sees the list and nothing to change', async () => {
    h.role = 'nurse'
    routeApi([line()])
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    await screen.findByText('Paracetamol')
    expect(screen.queryByRole('button', { name: 'Prescribe' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Cancel / })).not.toBeInTheDocument()
    expect(screen.queryByText('Record the review now')).not.toBeInTheDocument()
  })

  it('a finished visit offers nothing new but still prints', async () => {
    routeApi([line()])
    render(<PrescriptionPanel visitId="V-1" canWrite={false} allergyReview={REVIEWED} />)
    await screen.findByText('Paracetamol')
    expect(screen.queryByRole('button', { name: 'Prescribe' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Cancel / })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Print prescription' })).toBeInTheDocument()
  })
})

describe('cancel and print', () => {
  it('cancel needs a reason, sends the version, and says to take a printed copy back', async () => {
    routeApi([line()], {
      'POST /opd/visits/V-1/prescriptions/MO-1/cancel': () => ({
        prescription: line({ status: 'cancelled', status_label: 'Cancelled', live: false,
          cancel_reason: 'Wrong strength', version: 2 }),
        printed_before: true,
      }),
    })
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel Paracetamol' }))
    const confirm = screen.getByRole('button', { name: 'Cancel the medicine' })
    expect(confirm).toBeDisabled()
    fill('Why cancel Paracetamol?', 'Wrong strength')
    fireEvent.click(confirm)
    expect(await screen.findByText(/take that copy back from the patient/)).toBeInTheDocument()
    const [call] = posts('/opd/visits/V-1/prescriptions/MO-1/cancel')
    expect(bodyOf(call)).toEqual({ reason: 'Wrong strength', expected_version: 1 })
    expect(screen.queryByRole('button', { name: 'Print prescription' })).not.toBeInTheDocument()
  })

  it('prints from the server\'s print answer', async () => {
    routeApi([line()], {
      'GET /opd/visits/V-1/prescriptions/print': () => ({
        kind: 'prescription', printed_at: '2026-10-10T05:00:00+00:00',
        visit: { id: 'V-1', visit_date: '2026-10-10', department: 'Medicine', token_number: 4 },
        patient: { name: 'Chitra Second', uhid: 'UH-77', age: 54, sex: 'F' },
        allergy_review: REVIEWED.assessment, diagnoses: [],
        prescriptions: [line()], doctors: [{ name: 'Dr Iyer', registration_number: 'KMC-1' }],
      }),
    })
    render(<PrescriptionPanel visitId="V-1" canWrite allergyReview={REVIEWED} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Print prescription' }))
    const sheet = await screen.findByTestId('opd-prescription-print')
    expect(within(sheet).getByText('Registration number KMC-1')).toBeInTheDocument()
    expect(within(sheet).getByText('Paracetamol')).toBeInTheDocument()
  })
})
