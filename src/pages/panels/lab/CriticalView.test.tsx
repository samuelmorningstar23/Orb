/**
 * The Critical results view (lis-15). What it must never get wrong:
 *
 *   · It declares itself as the laboratory's critical tab, shown while the
 *     module drains.
 *   · An open critical says the value, the agreed limit, preliminary or not,
 *     the tier and when it is due, and how the case-room post went, in
 *     words; an undelivered one tells the laboratory to phone.
 *   · A discharged patient's critical stays, says "Patient discharged" and
 *     shows no bed.
 *   · A callback is sent only with the name, the role, the method and the
 *     read-back ticked; without the tick nothing is sent.
 *   · A call that reached nobody is logged as an attempt.
 *   · Each call names the value the screen showed (value_told); a refusal
 *     because that value changed meanwhile reloads the list.
 *   · A read-back ticked for one value never closes a critical for another:
 *     a reload (the timer, Refresh, or the reload after a refusal) that
 *     brings another value, or any further change, clears the form, and the
 *     call sends the value the box was ticked for. A reload with nothing
 *     changed keeps what was typed.
 *   · With no ladder set the screen says every critical is due at once.
 *   · A failed load is never an empty list.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { LabApiError } from '../../../api/lab'
import type { CriticalList, CriticalNotification } from '../../../api/lab/criticals'
import CriticalView, * as criticalModule from './CriticalView'
import { discoverViews } from './viewRegistry'

vi.mock('../../../api/lab/criticals', async () => {
  const actual = await vi.importActual<typeof import('../../../api/lab/criticals')>(
    '../../../api/lab/criticals')
  return {
    ...actual,
    getCriticals: vi.fn(),
    logCriticalCallback: vi.fn(),
    logCriticalAttempt: vi.fn(),
  }
})

const api = vi.mocked(await import('../../../api/lab/criticals'))

const ROLES = { nurse: 'Nurse', nurse_in_charge: 'Nurse in charge', doctor: 'Doctor',
  consultant: 'Consultant', duty_doctor: 'Duty doctor', duty_medical_officer: 'Duty medical officer' }
const METHODS = { phone: 'Phone', in_person: 'In person' }

function critical(over: Partial<CriticalNotification> = {}): CriticalNotification {
  return {
    id: 'LC-1',
    status: 'open',
    source_lane: 'lis',
    patient: { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', ward: 'GM-1', bed: '4A',
      discharged: false, discharged_at: null },
    test_name: 'Potassium',
    value_display: '7.0 mmol/L',
    critical_limit_text: 'Critical below 2.5 or above 6.5 mmol/L',
    preliminary: true,
    result_id: 41,
    value_told: '7.0 mmol/L',
    result_now: { status: 'tech_validated', text: 'Preliminary, not yet authorised' },
    raised_at: '2026-10-09T04:00:00Z',
    raised_by_name: 'Tech A',
    raised_in_department: 'General Medicine',
    tier: { current: 1, count: 3, label: 'Ward nurse in charge', next_label: 'Duty doctor',
      ladder_configured: true, due_at: '2099-10-09T04:15:00Z', due_now: false },
    delivery: { status: 'posted', reason: null, text: "Posted in the patient's case room.",
      message_id: 9 },
    callbacks: [],
    acknowledged: null,
    events: [],
    ...over,
  }
}

function list(over: Partial<CriticalList> = {}): CriticalList {
  return {
    open: [],
    recent: [],
    ladder: { configured: true, source: 'site.toml', tiers: [
      { tier: 1, role: 'Ward nurse in charge', label: 'Ward nurse in charge', timeout_minutes: 15 },
      { tier: 2, role: 'Duty doctor', label: 'Duty doctor', timeout_minutes: 15 },
      { tier: 3, role: 'Duty medical officer', label: 'Duty medical officer', timeout_minutes: null }] },
    recipient_roles: ROLES,
    methods: METHODS,
    recent_hours: 24,
    can_log: true,
    scope: { department: null, hospital_wide: true },
    ...over,
  }
}

const props = { meta: {}, state: 'on' as const, openView: vi.fn(), onModuleOff: vi.fn() }

/** The same critical after a signatory amended 7.0 to 4.5 (one change). */
function amendedTo45(): CriticalNotification {
  return critical({
    value_told: '4.5 mmol/L',
    value_now: '4.5 mmol/L',
    result_now: { status: 'tech_validated', changed: true,
      text: 'Preliminary, not yet authorised: now 4.5 mmol/L, not critical. It was reported as critical at 7.0 mmol/L. Phone the ward to tell them the value they were given has changed.' },
    changes: [{ at: '2026-10-09T04:20:00Z', by_name: 'Tech A', from_value: '7.0 mmol/L',
      to_value: '4.5 mmol/L', still_critical: false, reopened: false }],
  })
}

function fillCallback(name = 'Sister Mary') {
  fireEvent.change(screen.getByLabelText(/Person told/), { target: { value: name } })
  fireEvent.change(screen.getByLabelText(/Their role/), { target: { value: 'nurse_in_charge' } })
  fireEvent.click(screen.getByLabelText(/They read the value back to me/))
}

function formState() {
  return {
    name: (screen.getByLabelText(/Person told/) as HTMLInputElement).value,
    role: (screen.getByLabelText(/Their role/) as HTMLSelectElement).value,
    readBack: (screen.getByLabelText(/They read the value back to me/) as HTMLInputElement).checked,
  }
}

beforeEach(() => {
  api.getCriticals.mockResolvedValue(list())
})
afterEach(() => {
  vi.clearAllMocks()
})

describe('CriticalView', () => {
  it('declares itself as the critical results tab, kept while draining', () => {
    const [entry] = discoverViews({ './lab/CriticalView.tsx': criticalModule })
    expect(entry).toMatchObject({ id: 'critical', label: 'Critical results', order: 60,
      permission: 'lab.read', whenDraining: 'show' })
  })

  it('says an open critical in words, with its limit, tier and delivery', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical()] }))
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Potassium, Asha Menon/ })
    const text = card.textContent ?? ''
    expect(text).toContain('Potassium 7.0 mmol/L')
    expect(text).toContain('Preliminary, not yet authorised')
    expect(text).toContain('Critical below 2.5 or above 6.5 mmol/L')
    expect(text).toContain('Asha Menon · UHID MRN-0042 · GM-1, Bed 4A')
    expect(text).toContain('Tier 1 of 3: Ward nurse in charge')
    expect(text).toContain("Posted in the patient's case room.")
    expect(text).not.toMatch(/score|NEWS2/i)
    expect(text).not.toContain('\u2014')
  })

  it('tells the laboratory to phone when the post was not delivered', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical({
      delivery: { status: 'no_case_room', reason: 'no_case_room',
        text: 'Not delivered: no case room. Phone the ward.', message_id: null },
      tier: { current: 1, count: 3, label: 'Ward nurse in charge', next_label: 'Duty doctor',
        ladder_configured: true, due_at: '2020-01-01T00:00:00Z', due_now: true },
    })] }))
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Potassium/ })
    expect(card.textContent).toContain('Not delivered: no case room. Phone the ward.')
    expect(card.textContent).toContain('Due now')
    expect(screen.getByText(/Open \(1\), 1 due now/)).toBeTruthy()
  })

  it('says a value changed after it was raised, and the value as it now stands', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical({
      value_told: '4.5 mmol/L',
      value_now: '4.5 mmol/L',
      result_now: { status: 'tech_validated', changed: true,
        text: 'Preliminary, not yet authorised: now 4.5 mmol/L, not critical. It was reported as critical at 7.0 mmol/L. Phone the ward to tell them the value they were given has changed.' },
      changes: [{ at: '2026-10-09T04:20:00Z', by_name: 'Tech A', from_value: '7.0 mmol/L',
        to_value: '4.5 mmol/L', still_critical: false, reopened: false }],
    })] }))
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Potassium, Asha Menon/ })
    const text = card.textContent ?? ''
    expect(text).toContain('Value changed')
    expect(text).toContain('now 4.5 mmol/L, not critical')
    expect(text).toContain('Phone the ward to tell them the value they were given has changed.')
    expect(text).not.toContain('\u2014')
  })

  it('shows no change badge for a critical whose value stands', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical()] }))
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Potassium, Asha Menon/ })
    expect(card.textContent).not.toContain('Value changed')
  })

  it('keeps a discharged patient on the list with no bed', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical({
      patient: { id: 'P-ASHA', name: 'Asha Menon', mrn: 'MRN-0042', ward: null, bed: null,
        discharged: true, discharged_at: '2026-10-09T06:00:00Z' },
    })] }))
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Potassium/ })
    expect(card.textContent).toContain('Patient discharged')
    expect(card.textContent).not.toContain('Bed')
  })

  it('sends a callback only with the read-back ticked', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical()] }))
    api.logCriticalCallback.mockResolvedValue({ notification: critical({ status: 'acknowledged' }) })
    render(<CriticalView {...props} />)
    await screen.findByRole('article', { name: /Potassium/ })
    fireEvent.change(screen.getByLabelText(/Person told/), { target: { value: 'Sister Mary' } })
    fireEvent.change(screen.getByLabelText(/Their role/), { target: { value: 'nurse_in_charge' } })
    fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
    expect((await screen.findByRole('alert')).textContent).toContain('read the value back')
    expect(api.logCriticalCallback).not.toHaveBeenCalled()
    fireEvent.click(screen.getByLabelText(/They read the value back to me/))
    fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
    await waitFor(() => expect(api.logCriticalCallback).toHaveBeenCalledTimes(1))
    expect(api.logCriticalCallback).toHaveBeenCalledWith('LC-1', {
      recipient_name: 'Sister Mary', recipient_role: 'nurse_in_charge', method: 'phone',
      read_back: true, note: null, value_told: '7.0 mmol/L',
    })
    expect(await screen.findByText(/callback logged and closed/)).toBeTruthy()
  })

  it('names the value the ward was last sent, and sends that value with the call', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical({
      value_told: '6.9 mmol/L',
      value_now: '6.9 mmol/L',
      result_now: { status: 'tech_validated', changed: true,
        text: 'Preliminary, not yet authorised. Changed after it was raised: was 7.0 mmol/L, now 6.9 mmol/L, still critical.' },
    })] }))
    api.logCriticalCallback.mockResolvedValue({ notification: critical({ status: 'acknowledged' }) })
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Potassium/ })
    expect(card.textContent).toContain('This call is about Potassium 6.9 mmol/L, the value the ward was last sent.')
    fireEvent.change(screen.getByLabelText(/Person told/), { target: { value: 'Sister Mary' } })
    fireEvent.change(screen.getByLabelText(/Their role/), { target: { value: 'nurse_in_charge' } })
    fireEvent.click(screen.getByLabelText(/They read the value back to me/))
    fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
    await waitFor(() => expect(api.logCriticalCallback).toHaveBeenCalledTimes(1))
    expect(api.logCriticalCallback.mock.calls[0][1]).toMatchObject({ value_told: '6.9 mmol/L' })
  })

  it('lists a critical at the top of its ladder as due, first, and counted', async () => {
    // Round 3 review: the last tier is due from the moment it was reached.
    const top = critical({ id: 'LC-TOP', test_name: 'Sodium', value_display: '118 mmol/L',
      value_told: '118 mmol/L',
      tier: { current: 3, count: 3, label: 'Duty medical officer', next_label: null,
        ladder_configured: true, due_at: '2026-10-09T04:30:00Z', due_now: true } })
    api.getCriticals.mockResolvedValue(list({ open: [top, critical()] }))
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Sodium/ })
    expect(card.textContent).toContain(
      'Tier 3 of 3: Duty medical officer. Due now. Last tier: keep calling until someone reads the value back.')
    expect(card.className).toContain('lab-bench-card--stat')
    const fresh = screen.getByRole('article', { name: /Potassium/ })
    expect(fresh.className).not.toContain('lab-bench-card--stat')
    expect(screen.getByText(/Open \(2\), 1 due now/)).toBeTruthy()
    const order = screen.getAllByRole('article').map(a => a.getAttribute('aria-label'))
    expect(order[0]).toContain('Sodium')
  })

  it('makes the call about the withdrawal once the result was withdrawn', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical({
      value_told: 'withdrawn',
      value_now: null,
      preliminary: false,
      result_now: { status: 'withdrawn', changed: false,
        text: 'Withdrawn after this was raised. Tell the ward it was entered in error.' },
    })] }))
    api.logCriticalCallback.mockResolvedValue({ notification: critical({ status: 'acknowledged' }) })
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Potassium/ })
    const text = card.textContent ?? ''
    expect(text).toContain('This call is about the withdrawal of Potassium: tell the ward the result was entered in error and is no longer on the chart.')
    expect(text).not.toContain('Potassium withdrawn, the value the ward was last sent')
    expect(text).not.toContain('\u2014')
    fireEvent.change(screen.getByLabelText(/Person told/), { target: { value: 'Sister Mary' } })
    fireEvent.change(screen.getByLabelText(/Their role/), { target: { value: 'nurse_in_charge' } })
    fireEvent.click(screen.getByLabelText(/They read the value back to me/))
    fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
    await waitFor(() => expect(api.logCriticalCallback).toHaveBeenCalledTimes(1))
    expect(api.logCriticalCallback.mock.calls[0][1]).toMatchObject({ value_told: 'withdrawn' })
  })

  it('reloads the list when the value changed after it loaded', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical()] }))
    api.logCriticalCallback.mockRejectedValue(new LabApiError(
      'The value changed after this screen loaded: the ward has now been told Potassium 4.5 mmol/L. Nothing was logged.',
      409, 'value_changed'))
    render(<CriticalView {...props} />)
    await screen.findByRole('article', { name: /Potassium/ })
    expect(api.getCriticals).toHaveBeenCalledTimes(1)
    fireEvent.change(screen.getByLabelText(/Person told/), { target: { value: 'Sister Mary' } })
    fireEvent.change(screen.getByLabelText(/Their role/), { target: { value: 'nurse_in_charge' } })
    fireEvent.click(screen.getByLabelText(/They read the value back to me/))
    fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
    await waitFor(() => expect(api.getCriticals).toHaveBeenCalledTimes(2))
    expect((await screen.findByText(/The list has been reloaded/)).textContent).toContain(
      'the ward has now been told Potassium 4.5 mmol/L')
    expect(screen.queryByText(/callback logged and closed/)).toBeNull()
  })

  it('clears a read-back ticked for one value when Refresh brings another', async () => {
    // Round 4 review: the tick for 7.0 survived the reload and closed the
    // critical with a read-back of 4.5 nobody gave.
    api.getCriticals.mockResolvedValueOnce(list({ open: [critical()] }))
    api.logCriticalCallback.mockResolvedValue({ notification: critical({ status: 'acknowledged' }) })
    render(<CriticalView {...props} />)
    await screen.findByText(/This call is about Potassium 7.0 mmol\/L/)
    fillCallback()
    expect(formState()).toEqual({ name: 'Sister Mary', role: 'nurse_in_charge', readBack: true })

    api.getCriticals.mockResolvedValue(list({ open: [amendedTo45()] }))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByText(/This call is about Potassium 4.5 mmol\/L/)
    expect(formState()).toEqual({ name: '', role: '', readBack: false })

    fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Type the name')
    expect(api.logCriticalCallback).not.toHaveBeenCalled()

    fillCallback('Dr Rao')
    fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
    await waitFor(() => expect(api.logCriticalCallback).toHaveBeenCalledTimes(1))
    expect(api.logCriticalCallback).toHaveBeenCalledWith('LC-1', {
      recipient_name: 'Dr Rao', recipient_role: 'nurse_in_charge', method: 'phone',
      read_back: true, note: null, value_told: '4.5 mmol/L',
    })
  })

  it('clears a read-back when the once-a-minute reload brings another value', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    try {
      api.getCriticals.mockResolvedValueOnce(list({ open: [critical()] }))
      render(<CriticalView {...props} />)
      await screen.findByText(/This call is about Potassium 7.0 mmol\/L/)
      fillCallback()
      api.getCriticals.mockResolvedValue(list({ open: [amendedTo45()] }))
      await act(async () => { vi.advanceTimersByTime(60_000) })
      await screen.findByText(/This call is about Potassium 4.5 mmol\/L/)
      expect(api.getCriticals).toHaveBeenCalledTimes(2)
      expect(formState()).toEqual({ name: '', role: '', readBack: false })
      fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
      expect(api.logCriticalCallback).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears the form after a value_changed refusal, so one more click sends nothing', async () => {
    api.getCriticals.mockResolvedValueOnce(list({ open: [critical()] }))
    api.getCriticals.mockResolvedValue(list({ open: [amendedTo45()] }))
    api.logCriticalCallback.mockRejectedValueOnce(new LabApiError(
      'The value changed after this screen loaded: the ward has now been told Potassium 4.5 mmol/L. Nothing was logged.',
      409, 'value_changed'))
    render(<CriticalView {...props} />)
    await screen.findByText(/This call is about Potassium 7.0 mmol\/L/)
    fillCallback()
    fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
    await screen.findByText(/This call is about Potassium 4.5 mmol\/L/)
    expect(api.logCriticalCallback).toHaveBeenCalledTimes(1)
    expect(api.logCriticalCallback.mock.calls[0][1]).toMatchObject({ value_told: '7.0 mmol/L' })
    expect(formState()).toEqual({ name: '', role: '', readBack: false })
    fireEvent.click(screen.getByRole('button', { name: 'Log callback and close' }))
    expect(api.logCriticalCallback).toHaveBeenCalledTimes(1)
  })

  it('clears the form when the value changed and came back to the same value', async () => {
    api.getCriticals.mockResolvedValueOnce(list({ open: [critical()] }))
    render(<CriticalView {...props} />)
    await screen.findByText(/This call is about Potassium 7.0 mmol\/L/)
    fillCallback()
    // 7.0 to 4.5 and back to 7.0 between two reloads: the ward was sent two
    // more messages since the tick, so the tick no longer stands.
    api.getCriticals.mockResolvedValue(list({ open: [critical({
      result_now: { status: 'tech_validated', changed: true,
        text: 'Preliminary, not yet authorised. Changed after it was raised: was 4.5 mmol/L, now 7.0 mmol/L, still critical.' },
      changes: [
        { at: '2026-10-09T04:20:00Z', by_name: 'Tech A', from_value: '7.0 mmol/L',
          to_value: '4.5 mmol/L', still_critical: false, reopened: false },
        { at: '2026-10-09T04:25:00Z', by_name: 'Tech A', from_value: '4.5 mmol/L',
          to_value: '7.0 mmol/L', still_critical: true, reopened: false }],
    })] }))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(api.getCriticals).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(formState()).toEqual({ name: '', role: '', readBack: false }))
  })

  it('keeps what was typed when a reload changes nothing', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical()] }))
    render(<CriticalView {...props} />)
    await screen.findByText(/This call is about Potassium 7.0 mmol\/L/)
    fillCallback()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(api.getCriticals).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toHaveProperty('disabled', false))
    expect(formState()).toEqual({ name: 'Sister Mary', role: 'nurse_in_charge', readBack: true })
  })

  it('logs a call that reached nobody as an attempt', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical()] }))
    api.logCriticalAttempt.mockResolvedValue({ notification: critical() })
    render(<CriticalView {...props} />)
    await screen.findByRole('article', { name: /Potassium/ })
    fireEvent.change(screen.getByLabelText(/Who you tried/), { target: { value: 'Ward phone rang out' } })
    fireEvent.click(screen.getByRole('button', { name: 'Log the attempt' }))
    await waitFor(() => expect(api.logCriticalAttempt).toHaveBeenCalledWith('LC-1', {
      method: 'phone', note: 'Ward phone rang out', value_told: '7.0 mmol/L' }))
  })

  it('offers no callback form to a role that cannot log one', async () => {
    api.getCriticals.mockResolvedValue(list({ open: [critical()], can_log: false }))
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Potassium/ })
    expect(within(card).queryByRole('button', { name: 'Log callback and close' })).toBeNull()
    expect(card.textContent).toContain('Laboratory staff log the callback')
  })

  it('says every critical is due at once when no ladder is set', async () => {
    api.getCriticals.mockResolvedValue(list({
      ladder: { configured: false, tiers: [], source: null },
      open: [critical({ tier: { current: 1, count: 0, label: null, next_label: null,
        ladder_configured: false, due_at: '2026-10-09T04:00:00Z', due_now: true } })],
    }))
    render(<CriticalView {...props} />)
    expect(await screen.findByText(/No callback ladder is set for this site, so every critical/)).toBeTruthy()
    const card = screen.getByRole('article', { name: /Potassium/ })
    expect(card.textContent).toContain('due now. Phone the ward.')
  })

  it('shows a closed critical with who was told', async () => {
    api.getCriticals.mockResolvedValue(list({ recent: [critical({
      status: 'acknowledged',
      acknowledged: { at: '2026-10-09T04:10:00Z', by_name: 'Tech A', recipient_name: 'Sister Mary',
        recipient_role: 'nurse_in_charge', recipient_role_text: 'Nurse in charge', method: 'phone',
        method_text: 'Phone', read_back: true },
    })] }))
    render(<CriticalView {...props} />)
    const card = await screen.findByRole('article', { name: /Potassium/ })
    expect(card.textContent).toContain('Phone to Sister Mary (Nurse in charge), read back')
    expect(within(card).queryByRole('button', { name: 'Log callback and close' })).toBeNull()
  })

  it('never shows a failed load as an empty list', async () => {
    api.getCriticals.mockRejectedValue(new LabApiError('Server error', 500, 'http_500'))
    render(<CriticalView {...props} />)
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain("Couldn't load the critical results")
    expect(screen.queryByText(/No critical result is waiting/)).toBeNull()
  })

  it('hands a switched-off module to the panel', async () => {
    api.getCriticals.mockRejectedValue(new LabApiError('off', 404, 'module_disabled'))
    render(<CriticalView {...props} />)
    await waitFor(() => expect(props.onModuleOff).toHaveBeenCalled())
  })
})
