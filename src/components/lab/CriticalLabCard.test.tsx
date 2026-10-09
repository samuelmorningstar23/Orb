/**
 * The ward critical card (lis-16). What it must never get wrong:
 *
 *   · With the laboratory module off and the interfaced switch off there is
 *     no card and no request at all, so the nurse and doctor homes are
 *     exactly what they were. An account without lab.critical_ack gets no
 *     card either: denial is absence.
 *   · It shows the laboratory's value, its own flag and the agreed limit (or
 *     the sending laboratory's flag and reference range), the patient's name
 *     and UHID, "Discharged" with no bed for a discharged patient, and
 *     "Preliminary, not yet authorised", and no score or severity of Orb's
 *     own.
 *   · Acknowledge is sent only with a name and the read-back ticked, and
 *     names the value the card showed; a refusal because the value changed
 *     reloads the card and says why.
 *   · A failed load is never an empty card: it says it is a display failure,
 *     not an all-clear. That holds for a refresh that failed after an empty
 *     load too (the server can only fail its ward read once there is a
 *     patient to return, so that is when a new critical may have arrived);
 *     a failed refresh under a list keeps the list and says it may be out of
 *     date.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../api/lab'
import { DATA_CHANGED_EVENT } from '../../contexts/PanelActiveContext'
import {
  criticalCardOn, type WardCritical, type WardCriticalList,
} from '../../api/lab/wardCriticals'
import {
  resetFeatureFlags, resetModuleFlags, setFeatureFlags, setModuleFlags,
} from '../../shellConfig'
import CriticalLabCard from './CriticalLabCard'

vi.mock('../../api/lab/wardCriticals', async () => {
  const actual = await vi.importActual<typeof import('../../api/lab/wardCriticals')>(
    '../../api/lab/wardCriticals')
  return { ...actual, getWardCriticals: vi.fn(), acknowledgeWardCritical: vi.fn() }
})

const api = vi.mocked(await import('../../api/lab/wardCriticals'))

const ROLES = { nurse: 'Nurse', nurse_in_charge: 'Nurse in charge', doctor: 'Doctor',
  consultant: 'Consultant', duty_doctor: 'Duty doctor', duty_medical_officer: 'Duty medical officer' }

function critical(over: Partial<WardCritical> = {}): WardCritical {
  return {
    id: 'LC-1',
    status: 'open',
    source_lane: 'lis',
    sender: 'Laboratory',
    patient: { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', bed: '4A',
      discharged: false, discharged_at: null },
    test_name: 'Potassium',
    value_display: '7.0 mmol/L',
    value_told: '7.0 mmol/L',
    withdrawn: false,
    lab_flag: { code: 'HH', text: 'Critical high' },
    critical_limit_text: 'Critical below 2.5 or above 6.5 mmol/L',
    reference_range: null,
    preliminary: true,
    state_text: 'Preliminary, not yet authorised',
    change_text: null,
    raised_at: '2026-10-09T04:00:00Z',
    acknowledged: null,
    ...over,
  }
}

function list(open: WardCritical[]): WardCriticalList {
  return {
    open,
    methods: { in_app: 'Read on the ward screen', phone: 'Phone call from the laboratory' },
    recipient_roles: ROLES,
    scope: { department: 'General Medicine', hospital_wide: false },
  }
}

function signIn(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({
    id: 'NUR001', name: 'Sister Mary Rao', role, department: 'General Medicine' }))
}

beforeEach(() => {
  vi.clearAllMocks()
  // clearAllMocks keeps queued once-replies; a test that stops early must not
  // hand its leftovers to the next one.
  api.getWardCriticals.mockReset()
  api.acknowledgeWardCritical.mockReset()
  signIn('nurse')
})

afterEach(() => {
  localStorage.clear()
  resetModuleFlags()
  resetFeatureFlags()
})

async function settle() {
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
}

describe('when the card is there at all', () => {
  it('is absent and asks nothing with the laboratory and the interfaced switch off', async () => {
    setModuleFlags({ lab: false })
    const { container } = render(<CriticalLabCard />)
    await settle()
    expect(container).toBeEmptyDOMElement()
    expect(api.getWardCriticals).not.toHaveBeenCalled()
  })

  it('appears with the laboratory module on, and with only the interfaced switch on', async () => {
    api.getWardCriticals.mockResolvedValue(list([critical()]))
    setModuleFlags({ lab: true })
    const first = render(<CriticalLabCard />)
    expect(await screen.findByText('Potassium 7.0 mmol/L')).toBeInTheDocument()
    first.unmount()
    setModuleFlags({ lab: false })
    setFeatureFlags({ lab_critical_interfaced: true })
    render(<CriticalLabCard />)
    expect(await screen.findByText('Potassium 7.0 mmol/L')).toBeInTheDocument()
    expect(criticalCardOn({ lab: false } as never, { lab_critical_interfaced: false } as never))
      .toBe(false)
  })

  it('is absent for an account without lab.critical_ack', async () => {
    signIn('lab_technician')
    setModuleFlags({ lab: true })
    const { container } = render(<CriticalLabCard />)
    await settle()
    expect(container).toBeEmptyDOMElement()
    expect(api.getWardCriticals).not.toHaveBeenCalled()
  })

  it('renders nothing when the ward has no open critical', async () => {
    api.getWardCriticals.mockResolvedValue(list([]))
    setModuleFlags({ lab: true })
    const { container } = render(<CriticalLabCard />)
    await waitFor(() => expect(api.getWardCriticals).toHaveBeenCalled())
    await settle()
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the switch went off after the shell checked', async () => {
    api.getWardCriticals.mockRejectedValue(
      new LabApiError('The laboratory module is switched off on this site.', 404,
        'module_disabled'))
    setModuleFlags({ lab: true })
    const { container } = render(<CriticalLabCard />)
    await waitFor(() => expect(api.getWardCriticals).toHaveBeenCalled())
    await settle()
    expect(container).toBeEmptyDOMElement()
  })

  it('says a failed load is a display failure, never an empty card', async () => {
    api.getWardCriticals.mockRejectedValue(new LabApiError('Server error', 500, 'http_500'))
    setModuleFlags({ lab: true })
    render(<CriticalLabCard />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load critical lab results")
    expect(alert).toHaveTextContent('This is a display failure, not an all-clear.')
  })

  it('says a failed refresh after an empty load is a display failure, never an empty card',
    async () => {
      vi.useFakeTimers()
      try {
        api.getWardCriticals
          .mockResolvedValueOnce(list([]))
          .mockRejectedValueOnce(new LabApiError(
            'The access log could not be written, so nothing was shown.', 503,
            'audit_unavailable'))
        setModuleFlags({ lab: true })
        const { container } = render(<CriticalLabCard />)
        await settle()
        expect(api.getWardCriticals).toHaveBeenCalledTimes(1)
        expect(container).toBeEmptyDOMElement()
        // The 60 second poll is the refresh that fails.
        await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
        await settle()
        expect(api.getWardCriticals).toHaveBeenCalledTimes(2)
        expect(container).not.toBeEmptyDOMElement()
        const alert = screen.getByRole('alert')
        expect(alert).toHaveTextContent("Couldn't load critical lab results")
        expect(alert).toHaveTextContent('The access log could not be written')
        expect(alert).toHaveTextContent('This is a display failure, not an all-clear.')
        expect(within(alert).getByRole('button', { name: 'Retry' })).toBeEnabled()
      } finally {
        vi.useRealTimers()
      }
    })

  it('clears the error block when a retry after an empty load finds nothing open', async () => {
    api.getWardCriticals
      .mockResolvedValueOnce(list([]))
      .mockRejectedValueOnce(new LabApiError('Server error', 500, 'http_500'))
      .mockResolvedValueOnce(list([]))
    setModuleFlags({ lab: true })
    const { container } = render(<CriticalLabCard />)
    await waitFor(() => expect(api.getWardCriticals).toHaveBeenCalledTimes(1))
    await settle()
    await act(async () => { window.dispatchEvent(new CustomEvent(DATA_CHANGED_EVENT)) })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('This is a display failure, not an all-clear.')
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(api.getWardCriticals).toHaveBeenCalledTimes(3))
    await settle()
    expect(container).toBeEmptyDOMElement()
  })

  it('keeps the list and says it may be out of date when a refresh under it fails',
    async () => {
      api.getWardCriticals
        .mockResolvedValueOnce(list([critical()]))
        .mockRejectedValueOnce(new LabApiError('Server error', 500, 'http_500'))
      setModuleFlags({ lab: true })
      render(<CriticalLabCard />)
      expect(await screen.findByText('Potassium 7.0 mmol/L')).toBeInTheDocument()
      await act(async () => { window.dispatchEvent(new CustomEvent(DATA_CHANGED_EVENT)) })
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'Could not refresh: Server error The list below may be out of date.')
      expect(screen.getByText('Potassium 7.0 mmol/L')).toBeInTheDocument()
    })
})

describe('what the card shows', () => {
  it("shows the laboratory's value, flag, agreed limit and preliminary marking, and no score",
    async () => {
      api.getWardCriticals.mockResolvedValue(list([critical()]))
      setModuleFlags({ lab: true })
      const { container } = render(<CriticalLabCard />)
      const item = (await screen.findByText('Potassium 7.0 mmol/L')).closest('li') as HTMLElement
      expect(within(item).getByText('Asha Menon · UHID MRN-0042 · Bed 4A')).toBeInTheDocument()
      expect(within(item).getByText("Laboratory's flag").nextElementSibling)
        .toHaveTextContent('Critical high')
      expect(within(item).getByText('Agreed critical limit').nextElementSibling)
        .toHaveTextContent('Critical below 2.5 or above 6.5 mmol/L')
      expect(within(item).getByText('Preliminary, not yet authorised')).toBeInTheDocument()
      expect(screen.getByRole('heading', { name: 'Critical lab results' })).toBeInTheDocument()
      const text = container.textContent ?? ''
      expect(text).not.toMatch(/score|news2|severity/i)
      expect(text).not.toContain(String.fromCharCode(0x2014))
    })

  it('shows a discharged patient without a bed', async () => {
    api.getWardCriticals.mockResolvedValue(list([critical({
      patient: { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', bed: null, discharged: true,
        discharged_at: '2026-10-09T06:00:00Z' } })]))
    setModuleFlags({ lab: true })
    render(<CriticalLabCard />)
    expect(await screen.findByText('Asha Menon · UHID MRN-0042 · Discharged')).toBeInTheDocument()
    expect(screen.queryByText(/Bed 4A/)).not.toBeInTheDocument()
  })

  it("shows the sending laboratory's flag and reference range for an interfaced result",
    async () => {
      api.getWardCriticals.mockResolvedValue(list([critical({
        source_lane: 'interfaced', sender: 'Sending laboratory', preliminary: false,
        critical_limit_text: null, reference_range: '3.5-5.1',
        state_text: 'Final result from the sending laboratory' })]))
      setFeatureFlags({ lab_critical_interfaced: true })
      render(<CriticalLabCard />)
      const item = (await screen.findByText('Potassium 7.0 mmol/L')).closest('li') as HTMLElement
      expect(within(item).getByText("Sending laboratory's flag").nextElementSibling)
        .toHaveTextContent('Critical high')
      expect(within(item).getByText('Reference range sent').nextElementSibling)
        .toHaveTextContent('3.5-5.1')
      expect(within(item).queryByText('Agreed critical limit')).not.toBeInTheDocument()
      expect(within(item).getByText(/by the sending laboratory/)).toBeInTheDocument()
    })

  it('says a change and a withdrawal in words', async () => {
    api.getWardCriticals.mockResolvedValue(list([critical({
      withdrawn: true, value_told: 'withdrawn', lab_flag: null, critical_limit_text: null,
      preliminary: false, state_text: null,
      change_text: 'Withdrawn: the laboratory says this result was entered in error and it is '
        + 'no longer on the chart. Do not act on it.' })]))
    setModuleFlags({ lab: true })
    render(<CriticalLabCard />)
    expect(await screen.findByText('Potassium: withdrawn')).toBeInTheDocument()
    expect(screen.getByText(/Do not act on it/)).toBeInTheDocument()
  })
})

describe('acknowledging', () => {
  async function openForm() {
    api.getWardCriticals.mockResolvedValue(list([critical()]))
    setModuleFlags({ lab: true })
    render(<CriticalLabCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Acknowledge' }))
    await settle()
    return screen.getByRole('form', { name: 'Acknowledge Potassium 7.0 mmol/L' })
  }

  it('sends nothing until the read-back is ticked and a name is given', async () => {
    const form = await openForm()
    const submit = within(form).getByRole('button', { name: 'Acknowledge' })
    expect(within(form).getByLabelText('Name of the person who was told'))
      .toHaveValue('Sister Mary Rao')
    expect(submit).toBeDisabled()
    fireEvent.submit(form)
    expect(api.acknowledgeWardCritical).not.toHaveBeenCalled()
    fireEvent.click(within(form).getByLabelText('Read back: Potassium 7.0 mmol/L'))
    expect(submit).toBeEnabled()
    fireEvent.change(within(form).getByLabelText('Name of the person who was told'),
      { target: { value: ' ' } })
    expect(submit).toBeDisabled()
  })

  it('sends the name, how, the read-back and the value the card showed', async () => {
    api.acknowledgeWardCritical.mockResolvedValue({ notification: critical({ status: 'acknowledged' }) })
    const form = await openForm()
    fireEvent.click(within(form).getByLabelText('Phone call from the laboratory'))
    fireEvent.change(within(form).getByLabelText('Who phoned from the laboratory (optional)'),
      { target: { value: 'Anil, City Lab' } })
    fireEvent.click(within(form).getByLabelText('Read back: Potassium 7.0 mmol/L'))
    api.getWardCriticals.mockResolvedValue(list([]))
    fireEvent.click(within(form).getByRole('button', { name: 'Acknowledge' }))
    await waitFor(() => expect(api.acknowledgeWardCritical).toHaveBeenCalledTimes(1))
    expect(api.acknowledgeWardCritical).toHaveBeenCalledWith('LC-1', {
      recipient_name: 'Sister Mary Rao', method: 'phone', caller_name: 'Anil, City Lab',
      read_back: true, value_told: '7.0 mmol/L' })
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Acknowledged: Potassium 7.0 mmol/L for Asha Menon.')
  })

  it('reloads and says why when the value changed after the card loaded', async () => {
    api.acknowledgeWardCritical.mockRejectedValue(new LabApiError(
      'The value changed after this screen loaded: the laboratory now reports Potassium 7.4 '
      + 'mmol/L. Nothing was recorded. Read the new value and acknowledge it again.', 409,
      'value_changed'))
    const form = await openForm()
    fireEvent.click(within(form).getByLabelText('Read back: Potassium 7.0 mmol/L'))
    api.getWardCriticals.mockResolvedValue(list([critical({ value_told: '7.4 mmol/L' })]))
    const calls = api.getWardCriticals.mock.calls.length
    fireEvent.click(within(form).getByRole('button', { name: 'Acknowledge' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Nothing was recorded')
    await waitFor(() => expect(api.getWardCriticals.mock.calls.length).toBeGreaterThan(calls))
    expect(await screen.findByText('Potassium 7.4 mmol/L')).toBeInTheDocument()
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
  })

  it('keeps the acknowledgement and shows the error block when a later refresh of the '
    + 'emptied list fails', async () => {
    api.acknowledgeWardCritical.mockResolvedValue({ notification: critical({ status: 'acknowledged' }) })
    const form = await openForm()
    fireEvent.click(within(form).getByLabelText('Read back: Potassium 7.0 mmol/L'))
    api.getWardCriticals.mockResolvedValueOnce(list([]))
    fireEvent.click(within(form).getByRole('button', { name: 'Acknowledge' }))
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Acknowledged: Potassium 7.0 mmol/L for Asha Menon.')
    await waitFor(() => expect(screen.queryByText('Potassium 7.0 mmol/L')).not.toBeInTheDocument())
    api.getWardCriticals.mockRejectedValueOnce(new LabApiError('Server error', 500, 'http_500'))
    await act(async () => { window.dispatchEvent(new CustomEvent(DATA_CHANGED_EVENT)) })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load critical lab results")
    expect(alert).toHaveTextContent('This is a display failure, not an all-clear.')
    expect(screen.getByRole('status')).toHaveTextContent(
      'Acknowledged: Potassium 7.0 mmol/L for Asha Menon.')
  })

  it('keeps the form and shows a refusal it cannot act on', async () => {
    api.acknowledgeWardCritical.mockRejectedValue(new LabApiError(
      'Read the result back before you acknowledge it. Nothing was recorded.', 422,
      'read_back_required'))
    const form = await openForm()
    fireEvent.click(within(form).getByLabelText('Read back: Potassium 7.0 mmol/L'))
    fireEvent.click(within(form).getByRole('button', { name: 'Acknowledge' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Nothing was recorded.')
    expect(screen.getByRole('form')).toBeInTheDocument()
  })
})
