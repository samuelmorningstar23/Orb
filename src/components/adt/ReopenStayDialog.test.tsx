/**
 * Reopen a stay recorded in error (adt-9, over adt-8b).
 *
 * What it must never get wrong:
 *
 * 1. NOTHING IS SENT until a reason is typed and the typed MRN matches the
 *    one on file; then it posts { reason, confirm_mrn } to
 *    POST /api/adt/admissions/{id}/reopen with an Idempotency-Key.
 * 2. THE SERVER'S REFUSALS ARE SHOWN IN ITS WORDS: a summary on file, an end
 *    not recorded through Beds and admissions, a read-back mismatch.
 * 3. AFTERWARDS it says whether the bed came back or the patient waits for
 *    one (and why), whether a portal login came back, and which bundles were
 *    NOT restarted.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

type FetchInit = { method?: string; body?: string; headers?: Record<string, string> }

const apiFetch = vi.fn<(path: string, init?: FetchInit) => Promise<unknown>>()

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, init?: FetchInit) => apiFetch(path, init),
  errorDetailMessage: (detail: unknown, status: number) => {
    if (typeof detail === 'string') return detail
    const message = (detail as { message?: unknown } | null)?.message
    return typeof message === 'string' ? message : `The server answered ${status}.`
  },
}))

import ReopenStayDialog from './ReopenStayDialog'

const PATIENT = { name: 'Ravi Kumar', mrn: 'MRN-0021', ward: 'GM-2', bed: '21' }

function refusal(status: number, detail: unknown) {
  return Object.assign(new Error((detail as { message?: string })?.message ?? 'refused'), { status, detail })
}

const REOPENED = {
  status: 'reopened', replayed: false, admission_id: 'ADM-0021', patient_id: 'P021', movement_id: 'MOV-10',
  corrects_movement_id: 'MOV-9', corrected_end_type: 'death', corrected_discharged_at: '2026-10-06T04:30:00Z',
  ward: 'GM-2', ward_id: 'WRD-2', bed: '21', bed_id: 'BED-21', bed_retaken: true, awaiting_bed: false,
  bed_not_retaken: null, bed_not_retaken_words: null, bed_out_of_service: false, dispositions_voided: [7],
  portal_login_restored: true, bundles_not_restored: [{ id: 3, bundle_type: 'sepsis6', status: 'cancelled' }],
}

function renderDialog(props: Partial<Parameters<typeof ReopenStayDialog>[0]> = {}) {
  const onDone = vi.fn()
  const onClose = vi.fn()
  render(
    <ReopenStayDialog admissionId="ADM-0021" patient={PATIENT} onDone={onDone} onClose={onClose} {...props} />,
  )
  return { onDone, onClose }
}

const reopenButton = () => screen.getByRole('button', { name: 'Reopen stay' })
const reasonBox = () => screen.getByLabelText('Why the end was recorded in error')
const mrnBox = () => screen.getByLabelText('Type the MRN from the wristband to confirm')

function posts() {
  return apiFetch.mock.calls.filter(([, init]) => init?.method === 'POST')
}

beforeEach(() => {
  apiFetch.mockReset()
  apiFetch.mockResolvedValue(REOPENED)
})

describe('Reopen a stay recorded in error', () => {
  it('reads the patient and the end back, and says what reopening does', () => {
    renderDialog({ endType: 'death', endedAt: '2026-10-06T04:30:00Z' })
    const readback = screen.getByText('Ward and bed').closest('dl') as HTMLElement
    expect(readback).toHaveTextContent('Ravi Kumar')
    expect(readback).toHaveTextContent('MRN-0021')
    expect(readback).toHaveTextContent('GM-2, bed 21')
    expect(readback).toHaveTextContent('Recorded as')
    expect(readback).toHaveTextContent('Death')
    expect(screen.getByText(/voided, never deleted/)).toBeInTheDocument()
    expect(screen.getByText(/are not restarted/)).toBeInTheDocument()
  })

  it('sends nothing until a reason is typed and the MRN matches, then posts the right body', async () => {
    renderDialog()
    expect(reopenButton()).toBeDisabled()
    fireEvent.change(mrnBox(), { target: { value: 'MRN-0021' } })
    expect(reopenButton()).toBeDisabled()
    fireEvent.change(reasonBox(), { target: { value: 'Death recorded on the wrong patient' } })
    fireEvent.change(mrnBox(), { target: { value: 'MRN-0012' } })
    expect(screen.getByText('This does not match the MRN on file.')).toBeInTheDocument()
    expect(reopenButton()).toBeDisabled()
    fireEvent.click(reopenButton())
    expect(posts()).toHaveLength(0)

    fireEvent.change(mrnBox(), { target: { value: 'mrn 0021' } })
    expect(reopenButton()).toBeDisabled()
    fireEvent.change(mrnBox(), { target: { value: 'mrn-0021' } })
    expect(reopenButton()).toBeEnabled()
    await act(async () => { fireEvent.click(reopenButton()) })

    const [call] = posts()
    expect(call[0]).toBe('/adt/admissions/ADM-0021/reopen')
    expect(JSON.parse(call[1]?.body ?? '{}')).toEqual({
      reason: 'Death recorded on the wrong patient', confirm_mrn: 'mrn-0021',
    })
    expect(call[1]?.headers?.['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
  })

  it.each([
    ['summary_on_file', 'A discharge summary was written for this stay. A discharge with a summary is corrected by a new admission, not reopened.'],
    ['no_recorded_end', 'This stay was not ended through Beds and admissions, so it cannot be reopened here. A discharge recorded another way is corrected by a new admission.'],
    ['read_back_mismatch', "The MRN typed does not match this patient's record. Check the wristband and type it again. Nothing was recorded."],
  ])("shows the server's %s refusal in its words", async (code, message) => {
    apiFetch.mockRejectedValue(refusal(409, { error: code, message }))
    renderDialog()
    fireEvent.change(reasonBox(), { target: { value: 'Wrong patient' } })
    fireEvent.change(mrnBox(), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(reopenButton()) })
    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(screen.queryByTestId('reopen-stay-result')).toBeNull()
  })

  it('says a doctor-only refusal and an unreachable server in plain words', async () => {
    apiFetch.mockRejectedValueOnce(refusal(403, { error: 'permission_denied' }))
    renderDialog()
    fireEvent.change(reasonBox(), { target: { value: 'Wrong patient' } })
    fireEvent.change(mrnBox(), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(reopenButton()) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Only a doctor can reopen a stay. Nothing was reopened.')

    apiFetch.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await act(async () => { fireEvent.click(reopenButton()) })
    expect(screen.getByRole('alert')).toHaveTextContent('Could not reach Orb, so it is not known whether this was reopened.')
    // The same key on the retry: never reopened twice.
    const [first, second] = posts()
    expect(second[1]?.headers?.['Idempotency-Key']).toBe(first[1]?.headers?.['Idempotency-Key'])
  })

  it("says a key the server saw for another stay in plain words, never as the header's name", async () => {
    apiFetch.mockRejectedValueOnce(refusal(409, {
      error: 'idempotency_key_reused',
      message: 'That Idempotency-Key was already used for something else',
    }))
    renderDialog()
    fireEvent.change(reasonBox(), { target: { value: 'Wrong patient' } })
    fireEvent.change(mrnBox(), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(reopenButton()) })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      "This screen's request was already used for a different stay, so nothing was reopened. "
      + 'Close this screen and open it again for this patient.')
    expect(alert).not.toHaveTextContent(/Idempotency/i)
    expect(screen.queryByTestId('reopen-stay-result')).toBeNull()
  })

  it('says the bed came back, the portal login came back and which bundles did not', async () => {
    const { onDone } = renderDialog()
    fireEvent.change(reasonBox(), { target: { value: 'Wrong patient' } })
    fireEvent.change(mrnBox(), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(reopenButton()) })
    const result = await screen.findByTestId('reopen-stay-result')
    expect(within(result).getByRole('heading', { name: 'Stay reopened' })).toBeInTheDocument()
    expect(result).toHaveTextContent('Ravi Kumar is an inpatient again.')
    expect(result).toHaveTextContent('Corrected: Death, recorded for')
    expect(result).toHaveTextContent('Back in GM-2, bed 21.')
    expect(result).toHaveTextContent("portal login is switched back on")
    expect(result).toHaveTextContent('Care bundles not restarted')
    expect(result).toHaveTextContent('Sepsis Six')
    fireEvent.click(within(result).getByRole('button', { name: 'Done' }))
    expect(onDone).toHaveBeenCalledWith(REOPENED)
  })

  it('says why the bed did not come back and that the patient waits for one', async () => {
    apiFetch.mockResolvedValue({
      ...REOPENED, bed: null, bed_id: null, bed_retaken: false, awaiting_bed: true,
      bed_not_retaken: 'bed_occupied', bed_not_retaken_words: 'Another patient now lies in that bed',
      portal_login_restored: false, bundles_not_restored: [],
    })
    renderDialog()
    fireEvent.change(reasonBox(), { target: { value: 'Wrong patient' } })
    fireEvent.change(mrnBox(), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(reopenButton()) })
    const result = await screen.findByTestId('reopen-stay-result')
    expect(result).toHaveTextContent('Another patient now lies in that bed. The patient is waiting for a bed.')
    expect(result).not.toHaveTextContent('portal login')
    expect(result).not.toHaveTextContent('not restarted')
  })

  it('Cancel closes without a write, and a patient with no MRN cannot be reopened here', () => {
    const { onClose } = renderDialog({ patient: { ...PATIENT, mrn: '' } })
    expect(screen.getByText(/No MRN is on file for this patient/)).toBeInTheDocument()
    expect(mrnBox()).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalled()
    expect(posts()).toHaveLength(0)
  })

  it('no rendered sentence carries an em dash', async () => {
    renderDialog({ endType: 'lama', endedAt: '2026-10-06T04:30:00Z' })
    expect(document.body.textContent).not.toMatch(/\u2014/)
    fireEvent.change(reasonBox(), { target: { value: 'Wrong patient' } })
    fireEvent.change(mrnBox(), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(reopenButton()) })
    await screen.findByTestId('reopen-stay-result')
    expect(document.body.textContent).not.toMatch(/\u2014/)
  })
})
